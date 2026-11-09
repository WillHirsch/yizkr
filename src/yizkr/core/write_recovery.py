"""Server-owned item creation intents and verified instance-local bindings.

Immutable MediaWiki pages reserve a generation, identity map, operation and
single dispatch. The item creation revision carries the intent marker in its
summary. A lost response can therefore be recovered without repeating a
``new=item`` request. A dispatched intent with no visible creation remains
ambiguous: absence never proves that an in-flight request cannot still commit.

These records travel with the database. Restoring the complete database also
restores its generation; detecting an out-of-band restore requires an explicit
operational generation change, not a host-local shadow registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.pipeline.cancellation import check_cancelled

if TYPE_CHECKING:
    from yizkr.core.identity_assignments import (
        AssignmentDelta,
        AssignmentSnapshot,
        RegistryBinding,
    )
    from yizkr.core.identity_migration import MigrationReceipt
    from yizkr.core.reviewed_creation import ReviewedCreationAuthorization
    from yizkr.core.wiki import WikiPage
    from yizkr.pipeline.identity_review import Decision, MentionRevision

PREFIX = "Project:Yizkr write recovery/"
GENERATION_PAGE = PREFIX + "generation"
_HEX = r"^[0-9a-f]{32}$"
_HASH = r"^[0-9a-f]{64}$"


class CreationRefused(RuntimeError):
    """Server authority does not permit this creation or binding."""


class AmbiguousCreation(CreationRefused):
    """A dispatched creation must be reconciled, never blindly retried."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


class Generation(_Record):
    kind: Literal["generation"] = "generation"
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    instance_uri: str


class _Identity(_Record):
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    entity_id: str = Field(pattern=_HEX)
    intent_key: str = Field(pattern=_HASH)


class _Intent(_Identity):
    kind: Literal["intent"] = "intent"
    payload_hash: str = Field(pattern=_HASH)


class _Dispatch(_Identity):
    kind: Literal["dispatch"] = "dispatch"
    attempt: str = Field(pattern=_HEX)


class EntityBinding(_Identity):
    kind: Literal["receipt"] = "receipt"
    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    revision: int = Field(gt=0)
    created: bool = Field(default=False, exclude=True)


class _Map(_Record):
    kind: Literal["identity-map"] = "identity-map"
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    map_hash: str = Field(pattern=_HASH)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _dump(record: _Record) -> str:
    return _json(record.model_dump(mode="json"))


def _parse(cls, page):
    try:
        return cls.model_validate_json(page.text)
    except (ValueError, ValidationError) as exc:
        raise CreationRefused("invalid server creation record") from exc


def _identity(intent: _Identity) -> dict:
    return {
        key: getattr(intent, key)
        for key in ("deployment_id", "generation", "entity_id", "intent_key")
    }


def _marker(intent: _Identity) -> str:
    return f"[yizkr-create:{intent.intent_key}:{intent.entity_id}]"


def cluster_creation_operation(
    map_hash: str, cluster_id: str
) -> dict[str, str]:
    """The original map-bound creation key, shared with verified migration."""
    return {"kind": "cluster", "map": map_hash, "cluster": cluster_id}


def write_existing(item, *, summary: str):
    """Submit one guarded update; a conflict or uncertain result stops.

    The library's default retry loop is unsafe for a response lost after a
    successful write. A caller must reread and reconcile before another update.
    """
    revision = item.lastrevid
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
    ):
        raise CreationRefused("existing item has no verified base revision")
    check_cancelled()
    return item.write(
        summary=summary, baserevid=revision, max_retries=1, retry_after=0
    )


class CreationRegistry:
    """One server authority shared by independent clients and hosts.

    Construction and lookup are read-only, including recovery from a visible
    creation revision. Explicit ensure, claim, create and reconcile operations
    publish records.
    An absent generation may initialize only on an empty or vocabulary-only
    graph; existing ingestion requires a separate verified migration.
    """

    def __init__(self, client):
        self.client = client
        self._observed: Generation | None = None

    @property
    def generation(self) -> Generation | None:
        page = self.client.immutable_page(GENERATION_PAGE)
        if page is None:
            if self._observed is not None or self.client.page_titles(PREFIX):
                raise CreationRefused(
                    "deployment generation is missing "
                    "but recovery state exists"
                )
            return None
        generation = _parse(Generation, page)
        if generation.instance_uri != self.client.config.concept_uri:
            raise CreationRefused(
                "deployment generation belongs to another instance"
            )
        if self._observed is not None and generation != self._observed:
            raise CreationRefused(
                "deployment generation changed during this operation"
            )
        self._observed = generation
        return generation

    def check_generation(self) -> Generation | None:
        """Check admission without publishing a generation or other effects."""
        generation = self.generation
        if generation is not None:
            return generation
        ids = set(self.client.list_entity_ids())
        if ids:
            properties, items = self.client.vocabulary()
            if ids - set(properties.values()) - set(items.values()):
                raise CreationRefused(
                    "existing ingested entities have no deployment "
                    "generation; "
                    "verified migration required"
                )
        return None

    def ensure_generation(self) -> Generation:
        """Establish admitted deployment authority before any mutation."""
        generation = self.check_generation()
        if generation is not None:
            return generation
        proposed = Generation(
            deployment_id=uuid.uuid4().hex,
            generation=uuid.uuid4().hex,
            instance_uri=self.client.config.concept_uri,
        )
        check_cancelled()
        self.client.create_page_once(
            GENERATION_PAGE,
            _dump(proposed),
            "ingest: establish deployment generation",
        )
        generation = self.generation
        if generation is None:
            raise CreationRefused("deployment generation was not established")
        return generation

    def claim_identity_map(self, map_hash: str) -> None:
        if not re.fullmatch(_HASH, map_hash):
            raise ValueError("identity-map hash must be SHA-256")
        generation = self.ensure_generation()
        record = _Map(
            deployment_id=generation.deployment_id,
            generation=generation.generation,
            map_hash=map_hash,
        )
        title = PREFIX + generation.generation + "/identity-map"
        page = self.client.immutable_page(title)
        if page is None:
            check_cancelled()
            page = self.client.create_page_once(
                title, _dump(record), "ingest: bind deployment identity map"
            )
        if _parse(_Map, page) != record:
            raise CreationRefused(
                "deployment is bound to another identity map"
            )
        self._assert_generation(generation)

    def check_identity_map(self, map_hash: str) -> None:
        """Check the map reservation without creating a generation or claim."""
        if not re.fullmatch(_HASH, map_hash):
            raise ValueError("identity-map hash must be SHA-256")
        generation = self.generation
        if generation is None:
            return
        title = PREFIX + generation.generation + "/identity-map"
        page = self.client.immutable_page(title)
        if page is not None:
            expected = _Map(
                deployment_id=generation.deployment_id,
                generation=generation.generation,
                map_hash=map_hash,
            )
            if _parse(_Map, page) != expected:
                raise CreationRefused(
                    "deployment is bound to another identity map"
                )
        self._assert_generation(generation)

    def _assert_generation(self, generation: Generation) -> None:
        if self.generation != generation:
            raise CreationRefused("deployment generation changed")

    def verify_binding(self, binding: EntityBinding) -> RegistryBinding:
        """Read and verify an existing creation receipt without indexing it.

        Creation revision keeps its original meaning. Registry binding
        revisions are separate records, and never modify the creation receipt.
        A creation whose receipt was lost first needs ordinary reconciliation.
        """
        binding = EntityBinding.model_validate(binding.model_dump(mode="json"))
        generation = self.generation
        if generation is None:
            raise CreationRefused(
                "binding belongs to another deployment generation"
            )
        registered = self._verify_binding(generation, binding)
        self._assert_generation(generation)
        return registered

    def _verify_binding(
        self, generation: Generation, binding: EntityBinding
    ) -> RegistryBinding:
        """Verify within a read-only pass whose caller checks generation."""
        from yizkr.core.identity_assignments import RegistryBinding

        if (
            generation.deployment_id != binding.deployment_id
            or generation.generation != binding.generation
        ):
            raise CreationRefused(
                "binding belongs to another deployment generation"
            )
        intent = self._intent(generation, binding.intent_key)
        verified, page = (
            self._read_binding(generation, intent) if intent else (None, None)
        )
        if verified is None or verified.model_dump(
            mode="json"
        ) != binding.model_dump(mode="json"):
            raise CreationRefused(
                "binding is not proved by its creation authority"
            )
        title = self._title(generation, binding.intent_key, "receipt")
        if page is None:
            raise CreationRefused(
                "creation receipt needs reconciliation before indexing"
            )
        return RegistryBinding(
            binding=binding,
            receipt_locator=title,
            receipt_sha256=hashlib.sha256(page.text.encode()).hexdigest(),
        )

    def _binding_title(self, generation: Generation, entity_id: str) -> str:
        if not re.fullmatch(_HEX, entity_id):
            raise ValueError("entity ID must be an opaque archive identifier")
        return (
            PREFIX
            + generation.generation
            + "/entities/"
            + entity_id
            + "/binding/1"
        )

    def binding(self, entity_id: str) -> RegistryBinding | None:
        """Read the stable entity index and verify its original receipt."""
        generation = self.generation
        if generation is None:
            return None
        registered = self._indexed_binding(generation, entity_id)
        self._assert_generation(generation)
        return registered

    def indexed_bindings(self) -> dict[str, RegistryBinding]:
        """Read the verified entity index by QID without adding bindings.

        Unindexed items remain unbound. A display match or an unindexed
        creation receipt never supplies an implicit index entry.
        """
        generation = self.generation
        if generation is None:
            raise CreationRefused("binding catalog requires a generation")
        prefix = PREFIX + generation.generation + "/entities/"
        local_prefix = prefix.removeprefix("Project:")
        bindings = {}
        for title in self.client.page_titles(prefix):
            # Enumeration is already restricted to namespace 4. MediaWiki
            # returns its canonical name, which can differ from Project:.
            namespace, separator, local_title = title.partition(":")
            match = re.fullmatch(
                re.escape(local_prefix) + r"([0-9a-f]{32})/binding/1",
                local_title,
            )
            if not namespace or not separator or match is None:
                raise CreationRefused("unrecognized entity binding index")
            binding = self._indexed_binding(generation, match[1])
            if binding is None:
                raise CreationRefused("enumerated binding index is missing")
            qid = binding.binding.qid
            if qid in bindings:
                raise CreationRefused(
                    "multiple indexed identities name one item"
                )
            bindings[qid] = binding
        self._assert_generation(generation)
        return bindings

    def _indexed_binding(
        self, generation: Generation, entity_id: str
    ) -> RegistryBinding | None:
        """Read each binding afresh within the caller's generation checks."""
        from yizkr.core.identity_assignments import RegistryBinding

        page = self.client.immutable_page(
            self._binding_title(generation, entity_id)
        )
        if page is None:
            return None
        registered = _parse(RegistryBinding, page)
        if (
            registered.binding.entity_id != entity_id
            or self._verify_binding(generation, registered.binding)
            != registered
        ):
            raise CreationRefused(
                "entity index differs from its verified creation binding"
            )
        return registered

    def _reviewed_creation_title(self, generation, proposal_id):
        if not re.fullmatch(_HEX, proposal_id):
            raise ValueError("creation proposal ID must be a durable ID")
        return (
            f"{PREFIX}{generation.generation}/reviewed-creations/"
            f"{proposal_id}/authorization"
        )

    def reviewed_creation_authorization(
        self, decision: Decision
    ) -> ReviewedCreationAuthorization | None:
        """Read the exact original creation authorization without effects."""
        from yizkr.core.reviewed_creation import (
            ReviewedCreationAuthorization,
            reviewed_creation_operation,
        )

        reviewed_creation_operation(decision)
        generation = self.generation
        if generation is None:
            return None
        page = self.client.immutable_page(
            self._reviewed_creation_title(generation, decision.proposal.id)
        )
        if page is None:
            self._assert_generation(generation)
            return None
        authorization = _parse(ReviewedCreationAuthorization, page)
        if (
            authorization.generation != generation
            or authorization.decision.model_dump(mode="json")
            != decision.model_dump(mode="json")
        ):
            raise CreationRefused(
                "creation authorization differs from its decision"
            )
        self._assert_generation(generation)
        return authorization

    def authorize_reviewed_creation(
        self,
        decision: Decision,
        mention: MentionRevision,
        *,
        initial_payload: dict[str, Any],
        origin: dict[str, Any] | None = None,
    ) -> ReviewedCreationAuthorization:
        """Freeze pinned review before creation; the caller owns exclusion.

        This record is no substitute for current review or the enforced graph
        mutation boundary. It grants no historical assertion and allocates no
        item. Repeated publication keeps the original observation and origin.
        """
        from yizkr.core.instance import require_retained_code
        from yizkr.core.reviewed_creation import (
            ReviewedCreationAuthorization,
            creation_payload_sha256,
            reviewed_creation_context,
            reviewed_creation_operation,
        )

        generation = self.generation
        if generation is None:
            raise CreationRefused("reviewed creation requires a generation")
        proposed = ReviewedCreationAuthorization(
            generation=generation,
            decision=decision,
            mention=mention,
            context=reviewed_creation_context(decision, mention),
            operation=reviewed_creation_operation(decision),
            initial_payload=initial_payload,
            initial_payload_sha256=creation_payload_sha256(initial_payload),
            origin=origin or {},
        )
        stored = self.reviewed_creation_authorization(decision)
        if stored is not None:
            if stored.initial_payload != proposed.initial_payload:
                raise CreationRefused(
                    "creation payload changed after authorization"
                )
            return stored
        if any(
            earlier.mention.id == mention.id
            for earlier in self.reviewed_creation_authorizations()
        ):
            raise CreationRefused(
                "mention already has another creation authorization"
            )
        key = self._key(generation, proposed.operation)
        if self._intent(generation, key) is not None:
            raise CreationRefused(
                "existing creation has no prior review authorization"
            )
        require_retained_code(self.client)
        self._assert_generation(generation)
        check_cancelled()
        self.client.create_page_once(
            self._reviewed_creation_title(generation, decision.proposal.id),
            _dump(proposed),
            "ingest: authorize reviewed entity creation",
        )
        stored = self.reviewed_creation_authorization(decision)
        if (
            stored is None
            or stored.initial_payload != proposed.initial_payload
        ):
            raise CreationRefused(
                "reviewed creation authorization was not established"
            )
        return stored

    def reviewed_creation_authorizations(
        self,
    ) -> tuple[ReviewedCreationAuthorization, ...]:
        """Read candidate context, including unfinished creations."""
        from yizkr.core.reviewed_creation import ReviewedCreationAuthorization

        generation = self.generation
        if generation is None:
            return ()
        prefix = f"{PREFIX}{generation.generation}/reviewed-creations/"
        found = {}
        for title in self.client.page_titles(prefix):
            namespace, separator, local_title = title.partition(":")
            match = re.fullmatch(
                re.escape(prefix.removeprefix("Project:"))
                + r"([0-9a-f]{32})/authorization",
                local_title,
            )
            if not namespace or not separator or match is None:
                raise CreationRefused("unrecognized reviewed creation record")
            page = self.client.immutable_page(
                self._reviewed_creation_title(generation, match[1])
            )
            if page is None or match[1] in found:
                raise CreationRefused(
                    "creation authorization is missing or repeated"
                )
            authorization = _parse(ReviewedCreationAuthorization, page)
            if (
                authorization.generation != generation
                or authorization.decision.proposal.id != match[1]
            ):
                raise CreationRefused(
                    "creation authorization differs from its locator"
                )
            found[match[1]] = authorization
        self._assert_generation(generation)
        return tuple(found[key] for key in sorted(found))

    def reviewed_creation_binding(
        self, authorization: ReviewedCreationAuthorization
    ) -> EntityBinding | None:
        """Recover the actual binding without publishing a receipt."""
        stored = self.reviewed_creation_authorization(authorization.decision)
        if stored is None or stored != authorization:
            raise CreationRefused(
                "creation authorization is unavailable or changed"
            )
        generation = stored.generation
        intent = self._intent(
            generation, self._key(generation, stored.operation)
        )
        if intent is None:
            return None
        if intent.payload_hash != stored.initial_payload_sha256:
            raise CreationRefused(
                "creation intent differs from reviewed initial payload"
            )
        binding = self._binding(generation, intent)
        self._assert_generation(generation)
        return binding

    def verify_reviewed_creation(
        self, decision: Decision, entity_id: str
    ) -> bool:
        """Whether this exact approval created the supplied stable entity."""
        if (
            decision.proposal.operation != "new_entity"
            or decision.outcome != "approve"
        ):
            return False
        authorization = self.reviewed_creation_authorization(decision)
        if authorization is None:
            return False
        binding = self.reviewed_creation_binding(authorization)
        if binding is None or binding.entity_id != entity_id:
            return False
        # A read-only recovered binding is not yet a durable receipt. Ordinary
        # create/reconcile must publish that receipt before assignment.
        self.verify_binding(binding)
        return True

    def index_binding(self, binding: EntityBinding) -> RegistryBinding:
        """Index a verified creation; never adopt an item through its terms."""
        from yizkr.core.identity_assignments import RegistryBinding
        from yizkr.core.instance import require_retained_code

        registered = self.verify_binding(binding)
        generation = self.generation
        assert generation is not None
        require_retained_code(self.client)
        title = self._binding_title(generation, binding.entity_id)
        page = self.client.immutable_page(title)
        if page is None:
            self._assert_generation(generation)
            check_cancelled()
            page = self.client.create_page_once(
                title,
                _dump(registered),
                "ingest: index verified entity binding",
            )
        if _parse(RegistryBinding, page) != registered:
            raise CreationRefused(
                "entity index already records another binding"
            )
        self._assert_generation(generation)
        return registered

    def _assignment_title(
        self, generation: Generation, head: str | None
    ) -> str:
        if head is not None and not re.fullmatch(_HASH, head):
            raise ValueError("assignment head must be SHA-256")
        return (
            PREFIX
            + generation.generation
            + "/assignments/"
            + (head or "root")
            + "/next"
        )

    def assignment_snapshot(self) -> AssignmentSnapshot:
        """Read the immutable successor chain; no missing state is created."""
        from yizkr.core.identity_assignments import (
            AssignmentCommit,
            AssignmentRefused,
            AssignmentSnapshot,
            advance,
            canonical_delta,
            validated_revisions,
        )

        generation = self.generation
        snapshot = AssignmentSnapshot(generation=generation)
        if generation is None:
            return snapshot
        seen = set()
        bindings = {}

        def bound(entity_id):
            if entity_id not in bindings:
                bindings[entity_id] = self._indexed_binding(
                    generation, entity_id
                )
            return bindings[entity_id]

        while True:
            page = self.client.immutable_page(
                self._assignment_title(generation, snapshot.head)
            )
            if page is None:
                self._assert_generation(generation)
                return snapshot
            commit = _parse(AssignmentCommit, page)
            if (
                commit.deployment_id != generation.deployment_id
                or commit.generation != generation.generation
                or commit.previous != snapshot.head
                or commit.delta != canonical_delta(commit.delta)
            ):
                raise AssignmentRefused(
                    "assignment chain does not match its generation "
                    "or predecessor"
                )
            revisions = validated_revisions(
                snapshot,
                commit.delta,
                bound,
                creation=self.verify_reviewed_creation,
            )
            if revisions != commit.revisions:
                raise AssignmentRefused(
                    "assignment revisions differ from their authority"
                )
            updated = advance(snapshot, commit)
            if updated.head in seen or (
                not revisions and updated.constraints == snapshot.constraints
            ):
                raise AssignmentRefused(
                    "assignment chain has an empty or repeated commit"
                )
            seen.add(updated.head)
            snapshot = updated

    def append_assignments(
        self, delta: AssignmentDelta, *, expected_head: str | None
    ) -> AssignmentSnapshot:
        """Publish a reviewed delta at its expected immutable successor.

        A winner owns that successor permanently. Different contenders must
        replan from current authority; an identical acknowledged or uncertain
        publication is recoverable without another assignment revision.
        """
        from yizkr.core.identity_assignments import (
            AssignmentCommit,
            AssignmentRefused,
            StaleAssignments,
            advance,
            canonical_delta,
            record_json,
            validated_revisions,
        )
        from yizkr.core.instance import require_retained_code

        delta = canonical_delta(delta)
        snapshot = self.assignment_snapshot()
        generation = snapshot.generation
        if generation is None:
            raise AssignmentRefused(
                "assignments require an established deployment generation"
            )
        self._assignment_title(generation, expected_head)
        if snapshot.head != expected_head:
            # A request lost after publication may be retried even after a
            # compatible later append. Its original complete delta is the
            # idempotency proof, not a fresh timestamp or current local IDs.
            prior = next(
                (c for c in snapshot.history if c.previous == expected_head),
                None,
            )
            if prior is not None and prior.delta == delta:
                require_retained_code(self.client)
                return snapshot
            raise StaleAssignments(
                "assignment head changed; replan before publication"
            )
        revisions = validated_revisions(
            snapshot,
            delta,
            self.binding,
            creation=self.verify_reviewed_creation,
        )
        commit = AssignmentCommit(
            deployment_id=generation.deployment_id,
            generation=generation.generation,
            previous=expected_head,
            delta=delta,
            revisions=revisions,
        )
        updated = advance(snapshot, commit)
        require_retained_code(self.client)
        if not revisions and updated.constraints == snapshot.constraints:
            self._assert_generation(generation)
            return snapshot
        title = self._assignment_title(generation, expected_head)
        self._assert_generation(generation)
        check_cancelled()
        try:
            page = self.client.create_page_once(
                title,
                record_json(commit),
                "ingest: accept reviewed mention assignments",
            )
        except Exception:
            # A custom transport may surface an exception after the durable
            # create succeeded. Read authority rather than issue another edit.
            page = self.client.immutable_page(title)
            if page is None:
                raise
        stored = _parse(AssignmentCommit, page)
        if stored != commit:
            raise StaleAssignments(
                "another writer accepted a different assignment delta"
            )
        self._assert_generation(generation)
        return self.assignment_snapshot()

    def _migration_title(
        self, generation: Generation, migration_id: str
    ) -> str:
        if not re.fullmatch(_HASH, migration_id):
            raise ValueError("migration ID must be SHA-256")
        return (
            PREFIX
            + generation.generation
            + "/migrations/"
            + migration_id
            + "/receipt"
        )

    def _verify_migration(self, receipt: MigrationReceipt) -> None:
        from yizkr.core.identity_assignments import record_digest
        from yizkr.core.identity_migration import MigrationRefused

        snapshot = self.assignment_snapshot()
        generation = snapshot.generation
        if generation is None or (
            receipt.deployment_id != generation.deployment_id
            or receipt.generation != generation.generation
        ):
            raise MigrationRefused("migration belongs to another generation")
        prefix = {}
        for commit in snapshot.history:
            prefix.update({r.mention_id: r for r in commit.revisions})
            if record_digest(commit) == receipt.assignment_head:
                break
        else:
            raise MigrationRefused(
                "migration assignment prefix is unavailable"
            )
        assignments = {r.mention_id: r for r in receipt.assignments}
        for correspondence in receipt.correspondences:
            expected = assignments[correspondence.mention_id]
            if (
                prefix.get(expected.mention_id) != expected
                or snapshot.assignments.get(expected.mention_id) != expected
                or expected.entity_id
                != correspondence.binding.binding.entity_id
            ):
                raise MigrationRefused(
                    "migration assignment is not accepted unchanged"
                )
            binding = self.lookup(
                cluster_creation_operation(
                    receipt.map_hash, correspondence.cluster_id
                )
            )
            if (
                binding is None
                or self.verify_binding(binding) != correspondence.binding
                or self.binding(binding.entity_id) != correspondence.binding
            ):
                raise MigrationRefused(
                    "migration target differs from its original creation"
                )
        self._assert_generation(generation)

    def migration_receipt(self, migration_id: str) -> MigrationReceipt | None:
        """Read accepted migration evidence; never claim graph publication."""
        from yizkr.core.identity_migration import (
            MigrationReceipt,
            MigrationRefused,
        )

        generation = self.generation
        if generation is None:
            return None
        page = self.client.immutable_page(
            self._migration_title(generation, migration_id)
        )
        if page is None:
            return None
        receipt = _parse(MigrationReceipt, page)
        if receipt.migration_id != migration_id:
            raise MigrationRefused(
                "migration receipt has another operation ID"
            )
        self._verify_migration(receipt)
        return receipt

    def record_migration(self, receipt: MigrationReceipt) -> MigrationReceipt:
        """Record assignment acceptance, separately from graph projection."""
        from yizkr.core.identity_migration import (
            MigrationReceipt,
            MigrationRefused,
        )
        from yizkr.core.instance import require_retained_code

        receipt = MigrationReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        self._verify_migration(receipt)
        generation = self.generation
        assert generation is not None
        require_retained_code(self.client)
        title = self._migration_title(generation, receipt.migration_id)
        self._assert_generation(generation)
        check_cancelled()
        try:
            page = self.client.create_page_once(
                title,
                _dump(receipt),
                "ingest: record accepted identity migration",
            )
        except Exception:
            page = self.client.immutable_page(title)
            if page is None:
                raise
        stored = _parse(MigrationReceipt, page)
        if stored != receipt:
            raise MigrationRefused(
                "another migration receipt has different evidence"
            )
        self._verify_migration(stored)
        return stored

    def _application_title(self, plan_id, claim_id=None, suffix="plan"):
        if not re.fullmatch(_HASH, plan_id) or (
            claim_id is not None and not re.fullmatch(_HASH, claim_id)
        ):
            raise ValueError("application identifiers must be SHA-256")
        generation = self.generation
        if generation is None:
            raise CreationRefused(
                "application needs an established generation"
            )
        scope = f"/{claim_id}" if claim_id is not None else ""
        return (
            f"{PREFIX}{generation.generation}/applications/"
            f"{plan_id}{scope}/{suffix}"
        )

    def _application_record(self, title, cls, proposed=None):
        """Read or publish an immutable record in this server authority."""
        from yizkr.core.instance import require_retained_code

        generation = self.generation
        if proposed is not None and (
            generation is None
            or proposed.deployment_id != generation.deployment_id
            or proposed.generation != generation.generation
        ):
            raise CreationRefused("application belongs to another generation")
        page = self.client.immutable_page(title)
        if page is None and proposed is not None:
            require_retained_code(self.client)
            self._assert_generation(generation)
            check_cancelled()
            try:
                page = self.client.create_page_once(
                    title, _dump(proposed), "ingest: reviewed application"
                )
            except Exception:
                page = self.client.immutable_page(title)
                if page is None:
                    raise
        if page is None:
            return None
        record = _parse(cls, page)
        if generation is None or (
            record.deployment_id != generation.deployment_id
            or record.generation != generation.generation
        ):
            raise CreationRefused("application belongs to another generation")
        self._assert_generation(generation)
        return record

    def application_plan(self, plan_id):
        from yizkr.core.claim_applications import ApplicationPlan

        record = self._application_record(
            self._application_title(plan_id), ApplicationPlan
        )
        if record is not None and record.plan_id != plan_id:
            raise CreationRefused("application plan differs from its locator")
        return record

    def record_application_plan(self, plan):
        """Reserve a reviewed scope, retaining its first process record."""
        from yizkr.core.claim_applications import (
            ApplicationPlan,
            same_reviewed_scope,
        )

        plan = ApplicationPlan.model_validate(plan.model_dump(mode="json"))
        self._assert_generation(self.generation)
        stored = self._application_record(
            self._application_title(plan.plan_id), ApplicationPlan, plan
        )
        # Execution times/configuration are evidence of the first attempt.
        # A concurrent or restarted attempt retains that original evidence.
        if not same_reviewed_scope(stored, plan):
            raise CreationRefused("application scope changed at its intent")
        return stored

    def application_intent(self, plan_id, claim_id):
        from yizkr.core.claim_applications import (
            SupportIntent,
            assert_plan_claim,
        )

        plan = self.application_plan(plan_id)
        if plan is None:
            return None
        claim = assert_plan_claim(plan, claim_id)
        record = self._application_record(
            self._application_title(plan_id, claim_id, "intent"), SupportIntent
        )
        if record is not None and (
            record.plan_id != plan_id
            or record.claim_id != claim_id
            or {**record.support, "process": None} != claim.support
        ):
            raise CreationRefused("support intent differs from reviewed plan")
        return record

    def record_application_intent(self, intent):
        """Keep the first base revision; never rebase an uncertain update."""
        from yizkr.core.claim_applications import (
            SupportIntent,
            assert_plan_claim,
        )

        intent = SupportIntent.model_validate(intent.model_dump(mode="json"))
        plan = self.application_plan(intent.plan_id)
        if plan is None:
            raise CreationRefused("support intent has no reviewed plan")
        claim = assert_plan_claim(plan, intent.claim_id)
        if {**intent.support, "process": None} != claim.support:
            raise CreationRefused("support intent differs from approved value")
        stored = self._application_record(
            self._application_title(intent.plan_id, intent.claim_id, "intent"),
            SupportIntent,
            intent,
        )
        if (
            stored.plan_id != intent.plan_id
            or stored.claim_id != intent.claim_id
            or stored.support != intent.support
        ):
            raise CreationRefused("support intent has incompatible evidence")
        return stored

    def application_receipt(self, plan_id, claim_id):
        from yizkr.core.claim_applications import SupportReceipt
        from yizkr.core.identity_assignments import record_digest

        intent = self.application_intent(plan_id, claim_id)
        if intent is None:
            return None
        record = self._application_record(
            self._application_title(plan_id, claim_id, "receipt"),
            SupportReceipt,
        )
        if record is not None and (
            record.plan_id != plan_id
            or record.claim_id != claim_id
            or record.intent_sha256 != record_digest(intent)
            or record.qid != intent.support["qid"]
        ):
            raise CreationRefused(
                "application receipt differs from its intent"
            )
        return record

    def reserve_support_dispatch(self, intent):
        """Only one caller can dispatch at a target revision, across plans.

        A missing effect after reservation is uncertain, including a stopped
        caller that may not have sent the request. Recovery observes support;
        it never grants another send because a query currently sees nothing.
        """
        from yizkr.core.claim_applications import SupportDispatch
        from yizkr.core.identity_assignments import record_digest

        stored = self.application_intent(intent.plan_id, intent.claim_id)
        if stored != intent:
            raise CreationRefused("dispatch lacks its exact durable intent")
        dispatch = SupportDispatch(
            deployment_id=intent.deployment_id,
            generation=intent.generation,
            qid=intent.support["qid"],
            base_revision=intent.base_revision,
            plan_id=intent.plan_id,
            claim_id=intent.claim_id,
            intent_sha256=record_digest(intent),
            attempt=uuid.uuid4().hex,
        )
        title = (
            f"{PREFIX}{intent.generation}/support-dispatch/"
            f"{dispatch.qid}/{dispatch.base_revision}"
        )
        winner = self._application_record(title, SupportDispatch, dispatch)
        if winner != dispatch:
            raise CreationRefused(
                "another dispatch reserved this item revision; "
                "reconcile its visible effect before any further write"
            )

    def record_application_receipt(self, receipt):
        """Publish exact support observed through an authoritative reread."""
        from yizkr.core.claim_applications import SupportReceipt
        from yizkr.core.identity_assignments import record_digest
        from yizkr.pipeline.assigned_support import (
            SupportPayload,
            inspect_support,
        )

        receipt = SupportReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        intent = self.application_intent(receipt.plan_id, receipt.claim_id)
        if intent is None or receipt.intent_sha256 != record_digest(intent):
            raise CreationRefused("support receipt lacks its exact intent")
        state = inspect_support(
            self.client, SupportPayload.model_validate(intent.support)
        )
        if not state.complete or (
            receipt.qid != state.qid
            or receipt.revision != state.revision
            or receipt.statement_guid != state.guid
            or receipt.primary_hash != state.primary_hash
            or receipt.circumstance_hash != state.circumstance_hash
            or receipt.primary != state.primary
            or receipt.circumstance != state.circumstance
            or receipt.observed_process != state.observed_process
        ):
            raise CreationRefused(
                "receipt does not match current exact support"
            )
        stored = self._application_record(
            self._application_title(
                receipt.plan_id, receipt.claim_id, "receipt"
            ),
            SupportReceipt,
            receipt,
        )
        if (
            stored.plan_id != receipt.plan_id
            or stored.claim_id != receipt.claim_id
            or stored.intent_sha256 != receipt.intent_sha256
            or stored.statement_guid != receipt.statement_guid
            or stored.primary != receipt.primary
            or stored.circumstance != receipt.circumstance
        ):
            raise CreationRefused("another receipt records different support")
        return stored

    def _key(self, generation: Generation, operation: Mapping | str) -> str:
        if not isinstance(operation, (Mapping, str)) or not operation:
            raise ValueError(
                "creation operation must be a nonempty stable locator"
            )
        value = (
            dict(operation) if isinstance(operation, Mapping) else operation
        )
        return hashlib.sha256(
            _json(
                {
                    "generation": generation.model_dump(mode="json"),
                    "operation": value,
                }
            ).encode()
        ).hexdigest()

    def _title(self, generation: Generation, key: str, suffix: str) -> str:
        return PREFIX + generation.generation + "/" + key + "/" + suffix

    def _intent(self, generation: Generation, key: str) -> _Intent | None:
        page = self.client.immutable_page(
            self._title(generation, key, "intent")
        )
        if page is None:
            return None
        intent = _parse(_Intent, page)
        if (
            intent.intent_key != key
            or intent.generation != generation.generation
            or intent.deployment_id != generation.deployment_id
        ):
            raise CreationRefused(
                "creation intent belongs to another operation or deployment"
            )
        return intent

    def _binding(
        self, generation: Generation, intent: _Intent
    ) -> EntityBinding | None:
        binding, _ = self._read_binding(generation, intent)
        if binding is not None:
            self._assert_generation(generation)
        return binding

    def _read_binding(
        self, generation: Generation, intent: _Intent
    ) -> tuple[EntityBinding | None, WikiPage | None]:
        """Read creation proof once; the caller checks its generation."""
        dispatch_page = self.client.immutable_page(
            self._title(generation, intent.intent_key, "dispatch")
        )
        receipt_page = self.client.immutable_page(
            self._title(generation, intent.intent_key, "receipt")
        )
        if dispatch_page is None:
            if receipt_page is not None:
                raise CreationRefused(
                    "creation receipt has no dispatch authority"
                )
            return None, None
        if _identity(_parse(_Dispatch, dispatch_page)) != _identity(intent):
            raise CreationRefused("dispatch does not match its intent")
        if receipt_page is not None:
            binding = _parse(EntityBinding, receipt_page)
            if _identity(binding) != _identity(intent):
                raise CreationRefused("receipt does not match its intent")
        else:
            matches = self.client.creation_revisions(_marker(intent))
            if len(matches) != 1:
                raise AmbiguousCreation(
                    f"dispatched creation has {len(matches)} visible initial "
                    "revisions; reconciliation required"
                )
            binding = EntityBinding(
                **_identity(intent),
                qid=matches[0].qid,
                revision=matches[0].revision,
            )
        self.client.verify_creation(
            binding.qid, binding.revision, _marker(intent)
        )
        return binding, receipt_page

    def lookup(self, operation: Mapping | str) -> EntityBinding | None:
        generation = self.generation
        if generation is None:
            return None
        intent = self._intent(generation, self._key(generation, operation))
        return self._binding(generation, intent) if intent else None

    def reconcile(self, operation: Mapping | str) -> EntityBinding | None:
        """Persist a recovered receipt without authorizing a new item."""
        generation = self.generation
        if generation is None:
            return None
        intent = self._intent(generation, self._key(generation, operation))
        if intent is None:
            return None
        binding = self._binding(generation, intent)
        if binding is None:
            return None
        return self._receipt(generation, intent, binding)

    def _receipt(
        self, generation: Generation, intent: _Intent, binding: EntityBinding
    ) -> EntityBinding:
        self.client.verify_creation(
            binding.qid, binding.revision, _marker(intent)
        )
        self._assert_generation(generation)
        title = self._title(generation, intent.intent_key, "receipt")
        page = self.client.immutable_page(title)
        if page is None:
            check_cancelled()
            page = self.client.create_page_once(
                title, _dump(binding), "ingest: record verified item binding"
            )
        if _parse(EntityBinding, page) != binding:
            raise CreationRefused("competing creation receipt disagrees")
        self._assert_generation(generation)
        return binding

    def create(
        self, operation: Mapping | str, item, *, summary: str
    ) -> EntityBinding:
        generation = self.ensure_generation()
        key = self._key(generation, operation)
        intent = self._intent(generation, key)
        if intent is not None:
            binding = self._binding(generation, intent)
            if binding is not None:
                return self._receipt(generation, intent, binding)
        if item.id:
            raise CreationRefused("creation requires a new unsaved item")
        payload_hash = hashlib.sha256(
            _json(item.get_json()).encode()
        ).hexdigest()
        if intent is None:
            proposed = _Intent(
                deployment_id=generation.deployment_id,
                generation=generation.generation,
                entity_id=uuid.uuid4().hex,
                intent_key=key,
                payload_hash=payload_hash,
            )
            check_cancelled()
            self.client.create_page_once(
                self._title(generation, key, "intent"),
                _dump(proposed),
                "ingest: reserve stable entity identity",
            )
            intent = self._intent(generation, key)
            if intent is None:
                raise CreationRefused("creation intent was not established")
            binding = self._binding(generation, intent)
            if binding is not None:
                return self._receipt(generation, intent, binding)
        if intent.payload_hash != payload_hash:
            raise CreationRefused(
                "uncompleted intent has different initial item data"
            )
        dispatch = _Dispatch(**_identity(intent), attempt=uuid.uuid4().hex)
        self._assert_generation(generation)
        check_cancelled()
        page = self.client.create_page_once(
            self._title(generation, key, "dispatch"),
            _dump(dispatch),
            "ingest: authorize one item creation attempt",
        )
        stored = _parse(_Dispatch, page)
        if stored != dispatch:
            binding = self._binding(generation, intent)
            if binding is None:
                raise AmbiguousCreation("another dispatch owns this creation")
            return self._receipt(generation, intent, binding)
        self._assert_generation(generation)
        check_cancelled()
        try:
            item.write(
                summary=f"{_marker(intent)} {summary}",
                max_retries=1,
                retry_after=0,
            )
        except Exception as exc:
            try:
                binding = self._binding(generation, intent)
            except (CreationRefused, RecoveryEvidenceError) as recovery:
                raise recovery from exc
            if binding is None:
                raise AmbiguousCreation(
                    "item creation result is unknown"
                ) from exc
            return self._receipt(generation, intent, binding)
        binding = EntityBinding(
            **_identity(intent), qid=item.id, revision=item.lastrevid
        )
        return self._receipt(generation, intent, binding).model_copy(
            update={"created": True}
        )
