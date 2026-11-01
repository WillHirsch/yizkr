"""Durable, local identity decisions over verified source occurrences.

The SQLite store owns mention and decision identities, never graph entities.
Generated observations and proposals are immutable revisions. A projection
checks their current semantic basis and reports authority without writing a
graph, changing a clustering map, or treating a rejected match as a new entity.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.review_mentions import MentionObservation
from yizkr.pipeline.stages import sha256_text

Operation = Literal["same", "different", "assign", "new_entity"]
Outcome = Literal["approve", "reject"]


class ReviewRefused(ValueError):
    """The requested review cannot be attached to its claimed evidence."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MentionRevision(_Record):
    id: str
    revision: int = Field(gt=0)
    observation: MentionObservation
    correspondence: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.observation.artifact, self.observation.local_id

    @property
    def reference(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


class Proposal(_Record):
    id: str
    revision: int = Field(gt=0)
    operation: Operation
    mentions: tuple[str, ...]
    mention_revisions: tuple[dict[str, Any], ...]
    target: EntityBinding | None = None
    before: dict[str, Any] = Field(default_factory=dict)
    basis_version: Literal[1] = 1
    basis: dict[str, Any]
    origin: dict[str, Any]
    digest: str


class Decision(_Record):
    id: str
    revision: int = Field(gt=0)
    proposal: Proposal
    outcome: Outcome
    actor: str
    time: str
    predecessor: dict[str, Any] | None = None
    review_sheet_id: str | None = None

    @property
    def reference(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


class DecisionInput(_Record):
    proposal_id: str
    proposal_revision: int
    expected_digest: str
    outcome: Outcome
    actor: str
    time: str
    expected_decision_revision: int | None = None
    review_sheet_id: str | None = None


class Application(_Record):
    proposal_id: str
    proposal_revision: int
    status: Literal["applied", "needs_review", "blocked"]
    reasons: tuple[str, ...] = ()
    decision: Decision | None = None


class Projection(_Record):
    applications: tuple[Application, ...]
    assignments: dict[str, EntityBinding]
    new_entities: tuple[str, ...]
    same: tuple[tuple[str, str], ...]
    different: tuple[tuple[str, str], ...]
    rejected_targets: dict[str, tuple[EntityBinding, ...]]
    mention_revisions: tuple[dict[str, Any], ...]
    decision_revisions: tuple[dict[str, Any], ...]


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _dump(record: BaseModel) -> str:
    return _json(record.model_dump(mode="json"))


def _binding_key(binding: EntityBinding) -> tuple[str, str, str]:
    return binding.deployment_id, binding.generation, binding.entity_id


def _actor_time(actor: str, time: str) -> None:
    if not actor.strip():
        raise ReviewRefused("a review actor is required")
    try:
        parsed = datetime.fromisoformat(time)
    except (TypeError, ValueError) as exc:
        raise ReviewRefused("review time must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRefused("review time must include its timezone")


class ReviewStore:
    """An explicit local review database; each mutation is one transaction.

    Separate connections/processes serialize imports with SQLite. No cache,
    run directory or graph client is discovered implicitly. Callers own close.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        try:
            with self._transaction():
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ReviewRefused("unsupported identity review format")
                if version == 0:
                    if self._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchone():
                        raise ReviewRefused("unversioned review database")
                    self._db.execute(
                        "CREATE TABLE observations (artifact TEXT NOT NULL, "
                        "product TEXT NOT NULL, local_id TEXT NOT NULL, "
                        "mention TEXT NOT NULL, revision INTEGER NOT NULL, "
                        "anchor TEXT, data TEXT NOT NULL, "
                        "PRIMARY KEY(mention, revision), "
                        "UNIQUE(artifact, product, local_id))"
                    )
                    self._db.execute(
                        "CREATE INDEX observation_anchor "
                        "ON observations(anchor)"
                    )
                    self._db.execute(
                        "CREATE TABLE proposals (id TEXT NOT NULL, "
                        "revision INTEGER NOT NULL, scope TEXT NOT NULL, "
                        "data TEXT NOT NULL, PRIMARY KEY(id, revision), "
                        "UNIQUE(scope, revision))"
                    )
                    self._db.execute(
                        "CREATE TABLE decisions (id TEXT NOT NULL, "
                        "revision INTEGER NOT NULL, proposal TEXT NOT NULL, "
                        "data TEXT NOT NULL, PRIMARY KEY(id, revision), "
                        "UNIQUE(proposal, revision))"
                    )
                    self._db.execute("PRAGMA user_version=1")
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS review_sheets "
                    "(id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ReviewStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def get_mention(self, mention_id: str, revision: int) -> MentionRevision:
        row = self._db.execute(
            "SELECT data FROM observations WHERE mention=? AND revision=?",
            (mention_id, revision),
        ).fetchone()
        if row is None:
            raise ReviewRefused("unknown mention revision")
        return MentionRevision.model_validate_json(row[0])

    def put_review_sheet(self, rows: Sequence[dict[str, str]]) -> str:
        """Preserve displayed rows, apart from editable decisions."""
        if any(
            not isinstance(row, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in row.items()
            )
            for row in rows
        ):
            raise ReviewRefused("review sheet cells must be strings")
        sheet_id = uuid.uuid4().hex
        with self._transaction():
            self._db.execute(
                "INSERT INTO review_sheets VALUES (?,?)",
                (sheet_id, _json(list(rows))),
            )
        return sheet_id

    def get_review_sheet(self, sheet_id: str) -> list[dict[str, str]]:
        row = self._db.execute(
            "SELECT data FROM review_sheets WHERE id=?", (sheet_id,)
        ).fetchone()
        if row is None:
            raise ReviewRefused("unknown review sheet")
        return json.loads(row[0])

    def _verified(self, mention: MentionRevision) -> None:
        if self.get_mention(mention.id, mention.revision) != mention:
            raise ReviewRefused(
                "mention revision differs from stored evidence"
            )

    def _existing(
        self, observation: MentionObservation
    ) -> MentionRevision | None:
        row = self._db.execute(
            "SELECT data FROM observations WHERE artifact=? AND product=? "
            "AND local_id=?",
            (
                observation.artifact,
                observation.product_sha256,
                observation.local_id,
            ),
        ).fetchone()
        if row is None:
            return None
        existing = MentionRevision.model_validate_json(row[0])
        if existing.observation != observation:
            raise ReviewRefused(
                "immutable observation differs from its product"
            )
        return existing

    def _insert_observation(
        self,
        observation: MentionObservation,
        mention_id: str,
        anchor: str | None,
        correspondence: dict[str, Any],
    ) -> MentionRevision:
        if self._db.execute(
            "SELECT 1 FROM observations WHERE mention=? AND artifact=? "
            "AND product=?",
            (mention_id, observation.artifact, observation.product_sha256),
        ).fetchone():
            raise ReviewRefused("source correspondence must be one-to-one")
        revision = self._db.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM observations "
            "WHERE mention=?",
            (mention_id,),
        ).fetchone()[0]
        result = MentionRevision(
            id=mention_id,
            revision=revision,
            observation=observation,
            correspondence=correspondence,
        )
        self._db.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
            (
                observation.artifact,
                observation.product_sha256,
                observation.local_id,
                mention_id,
                revision,
                anchor,
                _dump(result),
            ),
        )
        return result

    def observe(
        self,
        observations: Sequence[MentionObservation],
    ) -> dict[tuple[str, str], MentionRevision]:
        """Observe one current product per source, without matching local IDs.

        An exact immutable product repeat is safe even without an anchor.
        Across products only a unique verified occurrence may inherit its ID.
        """
        keys = [(item.artifact, item.local_id) for item in observations]
        if len(keys) != len(set(keys)):
            raise ReviewRefused(
                "current observations repeat a source/local key"
            )
        products: dict[str, set[str]] = defaultdict(set)
        for item in observations:
            products[item.artifact].add(item.product_sha256)
        if any(len(values) != 1 for values in products.values()):
            raise ReviewRefused("supply one current product per source")
        anchors = Counter(item.correspondence_key for item in observations)
        result = {}
        with self._transaction():
            planned = []
            for observation in observations:
                existing = self._existing(observation)
                anchor = observation.correspondence_key
                if anchors[anchor] != 1:
                    anchor = None
                matches = (
                    self._db.execute(
                        "SELECT DISTINCT mention FROM observations "
                        "WHERE anchor=? AND artifact=?",
                        (anchor, observation.artifact),
                    ).fetchall()
                    if anchor
                    else []
                )
                candidate = (
                    existing.id
                    if existing
                    else (matches[0][0] if len(matches) == 1 else None)
                )
                planned.append((observation, existing, anchor, candidate))
            candidates = Counter(item[3] for item in planned)
            for observation, existing, anchor, candidate in planned:
                key = observation.artifact, observation.local_id
                if existing is not None:
                    result[key] = existing
                    continue
                unique = candidate is not None and candidates[candidate] == 1
                mention_id = candidate if unique else uuid.uuid4().hex
                proof = {"method": "unique-source-anchor", "anchor": anchor}
                if not unique:
                    proof = {"method": "new-occurrence"}
                result[key] = self._insert_observation(
                    observation,
                    mention_id,
                    anchor,
                    proof,
                )
        return result

    def remap(
        self,
        prior: MentionRevision,
        current: MentionObservation,
        *,
        actor: str,
        reason: str,
        time: str,
    ) -> MentionRevision:
        """Record an explicit one-to-one correspondence before observing it.

        Location changes may be reviewed; changing the source owner or native
        part cannot be described as a harmless locator remapping.
        """
        _actor_time(actor, time)
        if not reason.strip():
            raise ReviewRefused("a correspondence reason is required")
        old = prior.observation.source_scope or prior.observation.anchor
        new = current.source_scope or current.anchor
        if old is None or new is None or old.owner != new.owner:
            raise ReviewRefused(
                "remapping requires the same verified source owner"
            )
        old_part = (
            (old.support.container, old.support.part_id)
            if old.support
            else None
        )
        new_part = (
            (new.support.container, new.support.part_id)
            if new.support
            else None
        )
        if old_part != new_part:
            raise ReviewRefused(
                "remapping cannot change the native source part"
            )
        with self._transaction():
            self._verified(prior)
            existing = self._existing(current)
            if existing is not None:
                if existing.id != prior.id:
                    raise ReviewRefused(
                        "current observation already has another identity"
                    )
                return existing
            return self._insert_observation(
                current,
                prior.id,
                current.correspondence_key,
                {
                    "method": "reviewed-remapping",
                    "prior": prior.reference,
                    "actor": actor,
                    "reason": reason,
                    "time": time,
                },
            )

    def get_proposal(
        self, proposal_id: str, revision: int | None = None
    ) -> Proposal:
        query = "SELECT data FROM proposals WHERE id=?"
        args: tuple[Any, ...] = (proposal_id,)
        if revision is not None:
            query += " AND revision=?"
            args += (revision,)
        row = self._db.execute(
            query + " ORDER BY revision DESC LIMIT 1", args
        ).fetchone()
        if row is None:
            raise ReviewRefused("unknown proposal revision")
        return Proposal.model_validate_json(row[0])

    def list_proposals(self) -> list[Proposal]:
        return [
            Proposal.model_validate_json(row[0])
            for row in self._db.execute(
                "SELECT data FROM proposals AS p WHERE revision="
                "(SELECT MAX(revision) FROM proposals WHERE id=p.id) "
                "ORDER BY id"
            )
        ]

    def propose(
        self,
        operation: Operation,
        mentions: Sequence[MentionRevision],
        *,
        basis: dict[str, Any],
        origin: dict[str, Any],
        target: EntityBinding | None = None,
        before: dict[str, Any] | None = None,
    ) -> Proposal:
        if operation not in ("same", "different", "assign", "new_entity"):
            raise ReviewRefused("unsupported review operation")
        ids = tuple(sorted(item.id for item in mentions))
        if len(set(ids)) != len(ids) or len(ids) != (
            2 if operation in ("same", "different") else 1
        ):
            raise ReviewRefused("wrong mention scope for review operation")
        if (operation == "assign") != (target is not None):
            raise ReviewRefused(
                "only an assignment takes a supplied entity binding"
            )
        scope = {
            "operation": operation,
            "mentions": ids,
            "target": target.model_dump(mode="json") if target else None,
            "before": before or {},
        }
        scope_key = sha256_text(_json(scope))
        digest = sha256_text(
            _json(scope | {"basis_version": 1, "basis": basis})
        )
        with self._transaction():
            for mention in mentions:
                self._verified(mention)
            row = self._db.execute(
                "SELECT data FROM proposals WHERE scope=? "
                "ORDER BY revision DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            prior = Proposal.model_validate_json(row[0]) if row else None
            if prior is not None and prior.digest == digest:
                return prior
            proposal = Proposal(
                id=prior.id if prior else uuid.uuid4().hex,
                revision=prior.revision + 1 if prior else 1,
                operation=operation,
                mentions=ids,
                mention_revisions=tuple(
                    item.reference
                    for item in sorted(mentions, key=lambda m: m.id)
                ),
                target=target,
                before=before or {},
                basis=basis,
                origin=origin,
                digest=digest,
            )
            self._db.execute(
                "INSERT INTO proposals VALUES (?,?,?,?)",
                (
                    proposal.id,
                    proposal.revision,
                    scope_key,
                    _dump(proposal),
                ),
            )
            return proposal

    def get_decision(
        self, proposal_id: str, revision: int | None = None
    ) -> Decision | None:
        query = "SELECT data FROM decisions WHERE proposal=?"
        args: tuple[Any, ...] = (proposal_id,)
        if revision is not None:
            query += " AND revision=?"
            args += (revision,)
        row = self._db.execute(
            query + " ORDER BY revision DESC LIMIT 1", args
        ).fetchone()
        return Decision.model_validate_json(row[0]) if row else None

    def decide(
        self,
        proposal_id: str,
        revision: int,
        *,
        outcome: Outcome,
        actor: str,
        time: str,
        expected_digest: str,
        expected_decision_revision: int | None = None,
        review_sheet_id: str | None = None,
    ) -> Decision:
        return self.decide_many(
            [
                DecisionInput(
                    proposal_id=proposal_id,
                    proposal_revision=revision,
                    outcome=outcome,
                    actor=actor,
                    time=time,
                    expected_digest=expected_digest,
                    expected_decision_revision=expected_decision_revision,
                    review_sheet_id=review_sheet_id,
                )
            ]
        )[0]

    def decide_many(self, requests: Sequence[DecisionInput]) -> list[Decision]:
        """Import a packet atomically, checking optimistic revisions."""
        if len({item.proposal_id for item in requests}) != len(requests):
            raise ReviewRefused("a review packet repeats a proposal")
        result = []
        with self._transaction():
            for request in requests:
                _actor_time(request.actor, request.time)
                if request.review_sheet_id is not None:
                    sheet = self.get_review_sheet(request.review_sheet_id)
                    rows = [
                        row
                        for row in sheet
                        if row.get("proposal_id") == request.proposal_id
                    ]
                    expected = {
                        "proposal_revision": str(request.proposal_revision),
                        "digest": request.expected_digest,
                        "decision_revision": str(
                            request.expected_decision_revision
                        )
                        if request.expected_decision_revision is not None
                        else "",
                    }
                    if len(rows) != 1 or any(
                        rows[0].get(key) != value
                        for key, value in expected.items()
                    ):
                        raise ReviewRefused(
                            "decision differs from its reviewed sheet"
                        )
                proposal = self.get_proposal(request.proposal_id)
                if (
                    proposal.revision != request.proposal_revision
                    or proposal.digest != request.expected_digest
                ):
                    raise ReviewRefused("stale or altered review proposal")
                prior = self.get_decision(proposal.id)
                if (
                    prior is not None
                    and prior.proposal.digest == proposal.digest
                    and prior.outcome == request.outcome
                    and prior.actor == request.actor
                ):
                    result.append(prior)
                    continue
                if request.expected_decision_revision != (
                    prior.revision if prior else None
                ):
                    raise ReviewRefused(
                        "decision changed since the review was exported"
                    )
                decision = Decision(
                    id=prior.id if prior else uuid.uuid4().hex,
                    revision=prior.revision + 1 if prior else 1,
                    proposal=proposal,
                    outcome=request.outcome,
                    actor=request.actor,
                    time=request.time,
                    predecessor=prior.reference if prior else None,
                    review_sheet_id=request.review_sheet_id,
                )
                self._db.execute(
                    "INSERT INTO decisions VALUES (?,?,?,?)",
                    (
                        decision.id,
                        decision.revision,
                        proposal.id,
                        _dump(decision),
                    ),
                )
                result.append(decision)
        return result

    def project(
        self,
        current: Sequence[MentionRevision],
        *,
        bases: Mapping[str, dict[str, Any]],
        blocked: Mapping[str, Sequence[str]] | None = None,
        current_targets: Mapping[str, EntityBinding] | None = None,
    ) -> Projection:
        """Read authority, refusing stale basis and contradictory scopes.

        Current evidence and target bindings are supplied explicitly; no graph
        lookup, provider call, or implicit acceptance occurs during projection.
        """
        self._db.execute("BEGIN")
        try:
            return self._project(
                current,
                bases=bases,
                blocked=blocked,
                current_targets=current_targets,
            )
        finally:
            self._db.rollback()

    def _project(
        self,
        current: Sequence[MentionRevision],
        *,
        bases: Mapping[str, dict[str, Any]],
        blocked: Mapping[str, Sequence[str]] | None,
        current_targets: Mapping[str, EntityBinding] | None,
    ) -> Projection:
        for mention in current:
            self._verified(mention)
        present = {item.id for item in current}
        if len(present) != len(current):
            raise ReviewRefused("current cohort repeats a durable mention")
        blocked = blocked or {}
        applications: dict[str, Application] = {}
        usable: list[tuple[Proposal, Decision]] = []
        proposals = {item.id: item for item in self.list_proposals()}
        if set(bases) - proposals.keys():
            raise ReviewRefused("projection names an unknown proposal")
        unresolved_negative: set[str] = set()
        relevant = [
            item
            for item in proposals.values()
            if set(item.mentions) & present or item.id in bases
        ]
        for proposal in relevant:
            basis = bases.get(proposal.id)
            decision = self.get_decision(proposal.id)
            reasons = [
                reason
                for mention in proposal.mentions
                for reason in blocked.get(mention, ())
            ]
            if not set(proposal.mentions) <= present:
                reasons.append(
                    "source occurrence is unavailable or "
                    "correspondence unresolved"
                )
            if proposal.target is not None:
                target = (current_targets or {}).get(proposal.target.entity_id)
                if target is None or _dump(target) != _dump(proposal.target):
                    reasons.append(
                        "current entity binding is unavailable or changed"
                    )
            status = "blocked" if reasons else "applied"
            if not reasons and (
                proposal.basis != basis
                or decision is None
                or decision.proposal.digest != proposal.digest
            ):
                status = "needs_review"
                reasons.append(
                    "the current material basis has no applicable decision"
                )
            application = Application(
                proposal_id=proposal.id,
                proposal_revision=proposal.revision,
                status=status,
                reasons=tuple(reasons),
                decision=decision,
            )
            applications[proposal.id] = application
            if status == "applied":
                assert decision is not None
                usable.append((proposal, decision))
            elif decision is not None and (
                (
                    proposal.operation in ("same", "assign")
                    and decision.outcome == "reject"
                )
                or (
                    proposal.operation == "different"
                    and decision.outcome == "approve"
                )
            ):
                # An omitted/stale negative is not permission to cross it.
                unresolved_negative.update(set(proposal.mentions) & present)

        parent = {mention: mention for mention in present}

        def find(mention: str) -> str:
            while parent[mention] != mention:
                parent[mention] = parent[parent[mention]]
                mention = parent[mention]
            return mention

        def union(a: str, b: str) -> None:
            parent[find(b)] = find(a)

        same, different = [], []
        assigned: dict[str, list[EntityBinding]] = defaultdict(list)
        rejected: dict[str, list[EntityBinding]] = defaultdict(list)
        new = []
        by_target: dict[tuple[str, str, str], str] = {}
        for proposal, decision in usable:
            approve = decision.outcome == "approve"
            if proposal.operation in ("same", "different"):
                pair = (proposal.mentions[0], proposal.mentions[1])
                if proposal.operation == "same" and approve:
                    same.append(pair)
                    union(*pair)
                elif (proposal.operation == "same" and not approve) or (
                    proposal.operation == "different" and approve
                ):
                    different.append(pair)
            elif proposal.operation == "assign":
                assert proposal.target is not None
                mention = proposal.mentions[0]
                if approve:
                    assigned[mention].append(proposal.target)
                    key = _binding_key(proposal.target)
                    if key in by_target:
                        union(mention, by_target[key])
                    by_target[key] = mention
                else:
                    rejected[mention].append(proposal.target)
            elif approve:
                new.append(proposal.mentions[0])

        groups: dict[str, set[str]] = defaultdict(set)
        for mention in present:
            groups[find(mention)].add(mention)
        conflicts = {find(a) for a, b in different if find(a) == find(b)}
        conflicts.update(find(mention) for mention in unresolved_negative)
        group_targets: dict[str, dict[tuple[str, str, str], EntityBinding]] = (
            defaultdict(dict)
        )
        for mention, targets in assigned.items():
            for target in targets:
                group_targets[find(mention)][_binding_key(target)] = target
        for group, targets in group_targets.items():
            if len(targets) > 1:
                conflicts.add(group)
        for mention, targets in rejected.items():
            if any(
                _binding_key(target) in group_targets[find(mention)]
                for target in targets
            ):
                conflicts.add(find(mention))
        for mention in new:
            if (
                group_targets[find(mention)]
                or sum(find(other) == find(mention) for other in new) > 1
            ):
                conflicts.add(find(mention))
        for proposal, _decision in usable:
            if any(
                find(mention) in conflicts for mention in proposal.mentions
            ):
                applications[proposal.id] = applications[
                    proposal.id
                ].model_copy(
                    update={
                        "status": "blocked",
                        "reasons": ("current identity decisions conflict",),
                    }
                )

        def clear(mention: str) -> bool:
            return find(mention) not in conflicts

        assignments = {
            mention: next(iter(targets.values()))
            for group, targets in group_targets.items()
            if targets and group not in conflicts
            for mention in groups[group]
        }
        return Projection(
            applications=tuple(
                applications[key] for key in sorted(applications)
            ),
            assignments=assignments,
            new_entities=tuple(sorted(m for m in new if clear(m))),
            same=tuple(
                sorted(pair for pair in same if all(clear(m) for m in pair))
            ),
            different=tuple(
                sorted(
                    pair for pair in different if all(clear(m) for m in pair)
                )
            ),
            rejected_targets={
                m: tuple(values) for m, values in rejected.items() if clear(m)
            },
            mention_revisions=tuple(
                item.reference for item in sorted(current, key=lambda m: m.id)
            ),
            decision_revisions=tuple(
                app.decision.reference
                for app in applications.values()
                if app.status == "applied" and app.decision is not None
            ),
        )
