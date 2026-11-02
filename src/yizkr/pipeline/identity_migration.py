"""Review current assignments suggested by verified map-bound creations.

The map proposes a target; it does not prove old source correspondence or
human consent. This bounded operation preserves existing entities and records
accepted assignments. Graph provenance publication is a separate operation.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.core.identity_assignments import record_digest
from yizkr.core.identity_migration import (
    MigrationCorrespondence,
    MigrationReceipt,
    MigrationRefused,
)
from yizkr.core.provenance import ProcessRecord, RecordRef
from yizkr.core.write_recovery import (
    CreationRegistry,
    cluster_creation_operation,
)
from yizkr.pipeline.assignment_stages import (
    accept_reviewed_assignments,
    assignment_review_scope,
)
from yizkr.pipeline.clustering import IdentityMap, identity_hash
from yizkr.pipeline.fingerprints import (
    canonical_label,
    cluster_id,
    mention_fingerprint,
    mentions_of,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_mentions import observe_mentions
from yizkr.pipeline.review_stages import (
    MentionKey,
    PreparedReview,
    ReviewProposal,
    ReviewRequest,
    prepare_review,
)
from yizkr.pipeline.run_provenance import (
    application_record,
    freeze_record,
    read_record,
)
from yizkr.pipeline.transcribe import reading_digest

_HASH = re.compile(r"^[0-9a-f]{64}$")
_METHODS = {
    "confident",
    "confident_component",
    "adjudicated",
    "ambiguous",
    "conflicting",
    "singleton",
    "unclustered",
}
_LIMITS = (
    "map fingerprints propose targets; they do not prove historical "
    "source correspondence",
    "current products do not reconstruct the map's original input products "
    "or model calls",
    "identity confirmation does not ratify prior historical assertions",
)


def validated_map(identity: IdentityMap) -> dict[str, Any]:
    """Validate the supported map format without rerunning its algorithms."""
    data = copy.deepcopy(identity.data)
    if (
        not isinstance(data, dict)
        or type(data.get("format")) is not int
        or data["format"] != 1
        or type(data.get("version")) is not int
        or data["version"] != 9
        or data.get("stage") != "clustering"
        or not isinstance(data.get("mentions"), dict)
        or not isinstance(data.get("clusters"), dict)
        or not isinstance(data.get("review"), list)
        or not isinstance(data.get("summary"), dict)
        or any(
            not isinstance(data.get(key), str)
            or not _HASH.fullmatch(data[key])
            for key in ("inputs", "config", "hash")
        )
    ):
        raise MigrationRefused("unsupported or malformed identity map")
    local_keys = set()
    for fingerprint, row in data["mentions"].items():
        if (
            not isinstance(fingerprint, str)
            or not _HASH.fullmatch(fingerprint)
            or not isinstance(row, dict)
            or not isinstance(row.get("document"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", row["document"])
            or not isinstance(row.get("local_id"), str)
            or not row["local_id"]
            or not isinstance(row.get("label"), str)
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("span"), list)
            or any(type(n) is not int or n < 1 for n in row["span"])
            or row["span"] != sorted(set(row["span"]))
            or type(row.get("occurrence")) is not int
            or row["occurrence"] < 0
            or not isinstance(row.get("method"), str)
            or row["method"] not in _METHODS
            or not isinstance(row.get("cluster"), str)
        ):
            raise MigrationRefused("invalid map mention")
        if fingerprint != mention_fingerprint(
            row["document"],
            row["label"],
            row["kind"],
            row["span"],
            row["occurrence"],
        ):
            raise MigrationRefused(
                "map mention fingerprint differs from its fields"
            )
        key = row["document"], row["local_id"]
        if key in local_keys:
            raise MigrationRefused("map repeats a source-local mention")
        local_keys.add(key)
    assigned = set()
    for key, row in data["clusters"].items():
        if (
            not isinstance(key, str)
            or not isinstance(row, dict)
            or not isinstance(row.get("method"), str)
            or row["method"] not in _METHODS
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("members"), list)
            or not row["members"]
            or any(not isinstance(member, str) for member in row["members"])
            or len(set(row["members"])) != len(row["members"])
            or key != cluster_id(row["members"])
        ):
            raise MigrationRefused("invalid map cluster")
        for member in row["members"]:
            mention = data["mentions"].get(member)
            if (
                member in assigned
                or mention is None
                or (
                    mention["cluster"] != key
                    or mention["method"] != row["method"]
                    or mention["kind"] != row["kind"]
                )
            ):
                raise MigrationRefused(
                    "map membership, kind or method disagrees"
                )
            assigned.add(member)
    if (
        assigned != data["mentions"].keys()
        or identity_hash(data["mentions"]) != data["hash"]
    ):
        raise MigrationRefused(
            "map membership or decision hash is inconsistent"
        )
    try:
        record_digest(data)
    except (TypeError, ValueError) as exc:
        raise MigrationRefused(
            "map contains unsupported non-JSON evidence"
        ) from exc
    if "provenance" in data:
        try:
            producer = ProcessRecord.model_validate(data["provenance"])
        except ValueError as exc:
            raise MigrationRefused("invalid saved map provenance") from exc
        if producer.metadata.get("identity_map_hash") != data["hash"]:
            raise MigrationRefused("saved provenance belongs to another map")
    return data


@dataclass(frozen=True)
class PreparedMapMigration:
    request: ReviewRequest
    review: PreparedReview
    identity: dict[str, Any]
    selections: tuple[MentionKey, ...]
    correspondences: tuple[MigrationCorrespondence, ...]
    products: dict[str, dict[str, str]]
    inventory_sha256: str


def _components(row):
    return (
        row["document"],
        canonical_label(row["label"]),
        row["kind"],
        tuple(row["span"]),
    )


def prepare_map_migration(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    identity: IdentityMap,
    *,
    selections: Sequence[MentionKey],
) -> PreparedMapMigration:
    """Prepare ordinary explicit assignment review, never import approval."""
    data = validated_map(identity)
    selected = tuple(sorted(selections, key=lambda key: key.key))
    if not selected or len({key.key for key in selected}) != len(selected):
        raise MigrationRefused("select distinct current source mentions")
    sources = {source.artifact_sha1: source for source in request.sources}
    if len(sources) != len(request.sources):
        raise MigrationRefused("supply one current product per source")
    old_counts = Counter(_components(row) for row in data["mentions"].values())
    candidates = {}
    products = {}
    for artifact in sorted({key.artifact_sha1 for key in selected}):
        source = sources.get(artifact)
        if source is None or source.reading is None:
            raise MigrationRefused(
                "migration requires the selected current reading"
            )
        observations = {
            o.local_id: o
            for o in observe_mentions(artifact, source.batch, source.reading)
        }
        current = mentions_of(artifact, source.batch)
        counts = Counter(
            (m.artifact, m.label, m.kind, m.span) for m in current
        )
        by_local = {m.local_id: m for m in current}
        for key in selected:
            if key.artifact_sha1 != artifact:
                continue
            observation = observations.get(key.local_id)
            mention = by_local.get(key.local_id)
            if (
                observation is None
                or observation.anchor is None
                or mention is None
            ):
                raise MigrationRefused(
                    "migration occurrence lacks unique located source evidence"
                )
            components = (
                mention.artifact,
                mention.label,
                mention.kind,
                mention.span,
            )
            if (
                not mention.span
                or counts[components] != 1
                or old_counts[components] != 1
            ):
                raise MigrationRefused(
                    "migration cannot use ordinal or ambiguous correspondence"
                )
            old = data["mentions"].get(mention.fingerprint)
            if (
                old is None
                or mention.occurrence != 0
                or old["occurrence"] != 0
            ):
                raise MigrationRefused(
                    "current occurrence has no supported map correspondence"
                )
            found = registry.lookup(
                cluster_creation_operation(data["hash"], old["cluster"])
            )
            if found is None:
                raise MigrationRefused(
                    "map cluster has no verified creation binding"
                )
            candidates[key.key] = (
                mention.fingerprint,
                old,
                registry.verify_binding(found),
            )
        products[artifact] = {
            "batch_sha256": record_digest(
                source.batch.model_dump(
                    mode="json", exclude={"extraction_run"}
                )
            ),
            "reading_sha256": reading_digest(source.reading),
        }
    # Observation correspondence belongs to the existing review store. The old
    # ordinal/map fingerprint is used only to select a candidate graph target.
    initial = prepare_review(store, request)
    proposals = list(request.proposals)
    targets = {target.entity_id: target for target in request.current_targets}
    correspondences = []
    for key in selected:
        fingerprint, old, registered = candidates[key.key]
        mention = initial.mentions[key.key]
        target = registered.binding
        held = targets.get(target.entity_id)
        if held is not None and held.model_dump(
            mode="json"
        ) != target.model_dump(mode="json"):
            raise MigrationRefused(
                "supplied target differs from verified map binding"
            )
        targets[target.entity_id] = target
        requested = [
            p
            for p in proposals
            if p.operation == "assign" and p.mentions == (key,)
        ]
        if any(
            p.target.model_dump(mode="json") != target.model_dump(mode="json")
            for p in requested
        ):
            raise MigrationRefused(
                "reviewed target differs from the map candidate"
            )
        if not requested:
            prior = next(
                (
                    p
                    for p in initial.proposals
                    if p.operation == "assign"
                    and p.mentions == (mention.id,)
                    and p.target.model_dump(mode="json")
                    == target.model_dump(mode="json")
                ),
                None,
            )
            proposals.append(
                ReviewProposal(
                    operation="assign",
                    mentions=(key,),
                    target=target,
                    evidence=copy.deepcopy(prior.basis.get("evidence", {}))
                    if prior
                    else {
                        "candidate_map_sha256": record_digest(data),
                        "candidate_cluster": old["cluster"],
                    },
                    origin=copy.deepcopy(prior.origin)
                    if prior
                    else {
                        "operation": "identity-map-migration",
                        "map_hash": data["hash"],
                        "method": old["method"],
                    },
                    before=copy.deepcopy(prior.before) if prior else {},
                )
            )
        correspondences.append(
            MigrationCorrespondence(
                artifact_sha1=key.artifact_sha1,
                local_id=key.local_id,
                mention_id=mention.id,
                mention_revision=mention.revision,
                legacy_fingerprint=fingerprint,
                cluster_id=old["cluster"],
                kind=old["kind"],
                method=old["method"],
                binding=registered,
            )
        )
    reviewed_request = ReviewRequest(
        sources=request.sources,
        proposals=tuple(proposals),
        current_targets=tuple(targets[key] for key in sorted(targets)),
        blocked=request.blocked,
        remaps=request.remaps,
    )
    review = prepare_review(store, reviewed_request)
    inventory = {
        "map": data,
        "products": products,
        "correspondences": [
            c.model_dump(mode="json") for c in correspondences
        ],
    }
    return PreparedMapMigration(
        reviewed_request,
        review,
        data,
        selected,
        tuple(correspondences),
        products,
        record_digest(inventory),
    )


def accept_map_migration(
    store: ReviewStore,
    prepared: PreparedMapMigration,
    registry: CreationRegistry,
    *,
    record_dir: Path,
) -> MigrationReceipt:
    """Accept assignments and record evidence, without historical writes."""
    fresh = prepare_map_migration(
        store,
        prepared.request,
        registry,
        IdentityMap(prepared.identity),
        selections=prepared.selections,
    )
    if fresh.inventory_sha256 != prepared.inventory_sha256:
        raise MigrationRefused(
            "migration inputs changed; prepare current evidence again"
        )
    applications = fresh.review.projection.applications
    decisions = {}
    for correspondence in fresh.correspondences:
        matching = [
            a
            for a in applications
            if a.status == "applied"
            and a.decision is not None
            and a.decision.outcome == "approve"
            and a.decision.proposal.operation == "assign"
            and a.decision.proposal.mentions == (correspondence.mention_id,)
            and a.decision.proposal.target.model_dump(mode="json")
            == correspondence.binding.binding.model_dump(mode="json")
        ]
        if len(matching) != 1:
            raise MigrationRefused(
                "migration needs one current explicit assignment approval "
                "per occurrence"
            )
    selected = {c.mention_id for c in fresh.correspondences}
    relevant = assignment_review_scope(
        fresh.review, registry.assignment_snapshot(), selected
    )
    decisions = {
        a.decision.id: a.decision.model_dump(mode="json")
        for a in applications
        if a.decision is not None
        and relevant.intersection(a.decision.proposal.mentions)
    }
    proof = {
        "map": fresh.identity,
        "map_sha256": record_digest(fresh.identity),
        "correspondences": [
            c.model_dump(mode="json") for c in fresh.correspondences
        ],
        "products": fresh.products,
        "decisions": decisions,
    }
    migration_id = record_digest(proof)
    # Pin again through receipt publication: the accepted identity and its
    # recorded human confirmation must be the same freshly observed authority.
    accepted = accept_reviewed_assignments(
        store, fresh.request, registry, scope=selected
    )
    current = prepare_review(store, fresh.request)
    if current.projection != fresh.review.projection:
        raise MigrationRefused(
            "review authority changed during migration acceptance"
        )
    with store.pin_projection(
        current.projection, list(current.mentions.values())
    ):
        prior = registry.migration_receipt(migration_id)
        if prior is not None:
            original = read_record(
                record_dir, RecordRef(sha256=prior.record_sha256)
            )
            payload = original.metadata.get("identity_migration", {})
            if any(payload.get(key) != value for key, value in proof.items()):
                raise MigrationRefused(
                    "stored migration record disagrees with current evidence"
                )
            return prior
        selected = {c.mention_id for c in fresh.correspondences}
        assignments = tuple(
            accepted.snapshot.assignments[key] for key in sorted(selected)
        )
        prefix = set()
        assignment_head = None
        required = {a.revision_id for a in assignments}
        for commit in accepted.snapshot.history:
            prefix.update(r.revision_id for r in commit.revisions)
            if required <= prefix:
                assignment_head = record_digest(commit)
                break
        if assignment_head is None or accepted.snapshot.generation is None:
            raise MigrationRefused("accepted assignment prefix is unavailable")
        limits = list(_LIMITS)
        producers = ()
        if "provenance" in fresh.identity:
            producers = (
                ProcessRecord.model_validate(fresh.identity["provenance"]),
            )
        else:
            limits.append(
                "original map producer model, prompt and run provenance "
                "unavailable"
            )
        payload = proof | {
            "request_sha256": migration_id,
            "assignment_head": assignment_head,
            "assignments": [a.model_dump(mode="json") for a in assignments],
            "limitations": limits,
        }
        record = application_record(
            producers,
            kind="identity-map-migration",
            stage="identity-migration-preparation",
            metadata={"identity_migration": payload},
        )
        record = record.model_copy(
            update={
                "limitations": tuple(
                    dict.fromkeys((*record.limitations, *limits))
                )
            }
        )
        producer_refs = tuple(freeze_record(record_dir, p) for p in producers)
        record = record.model_copy(update={"producer_refs": producer_refs})
        reference = freeze_record(record_dir, record)
        generation = accepted.snapshot.generation
        receipt = MigrationReceipt(
            migration_id=migration_id,
            deployment_id=generation.deployment_id,
            generation=generation.generation,
            map_hash=fresh.identity["hash"],
            map_sha256=record_digest(fresh.identity),
            record_sha256=reference.sha256,
            assignment_head=assignment_head,
            correspondences=fresh.correspondences,
            assignments=assignments,
        )
        return registry.record_migration(receipt)
