"""Publish verified identity origins without rewriting historical assertions.

Immutable map evidence describes unratified placements; accepted assignment
publishers separately verify their server authority and confirmation evidence.
This shared projection adds metadata items and reference blocks to existing
source-backed kind statements. It neither assigns mentions nor changes
statement values, qualifiers, ranks or existing references.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference

from yizkr.core.identity_assignments import RegistryBinding, record_digest
from yizkr.core.instance import require_retained_code
from yizkr.core.provenance import ProcessRecord, RecordRef
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.identity_review import Decision
from yizkr.pipeline.run_provenance import read_record
from yizkr.pipeline.run_provenance import record_digest as process_digest
from yizkr.pipeline.run_statements import (
    ensure_run_statements,
    validate_run_statements,
)
from yizkr.pipeline.source_bindings import unique_source_qids

METHOD_ITEMS = {
    "confident": "identity_confident",
    "adjudicated": "identity_adjudicated",
    "confident_component": "identity_confident_component",
    "owner_override": "identity_owner_override",
    "singleton": "identity_singleton",
    "ambiguous": "identity_ambiguous",
    "conflicting": "identity_conflicting",
    "unclustered": "identity_unclustered",
}
RECORD_KIND = "identity_decision_record"


class IdentityProvenanceRefused(ValueError):
    """Saved authority or current graph evidence cannot support projection."""


@dataclass(frozen=True)
class IdentitySource:
    mention_id: str
    artifact_sha1: str
    kind: str
    binding: RegistryBinding
    method: str

    @property
    def qid(self) -> str:
        return self.binding.binding.qid


@dataclass(frozen=True)
class IdentityPublication:
    record_qid: str | None
    changed: bool
    statements: tuple[tuple[str, str], ...]


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _value(snak):
    return (snak.datavalue or {}).get("value")


def _reference_key(reference) -> str:
    # Server reference hashes and snak ordering are transport details.
    return _json(sorted(_json(snak.get_json()) for snak in reference.snaks))


def _string(properties, key, value):
    from yizkr.pipeline.writer import _faults

    if isinstance(value, str):
        # Wikibase stores NFC; the immutable record retains the authored value.
        value = unicodedata.normalize("NFC", value)
    if not isinstance(value, str) or not value or _faults(value):
        raise IdentityProvenanceRefused(
            f"identity metadata cannot represent {key}"
        )
    return String(prop_nr=properties[key], value=value)


def _read_evidence(registry, migration_id, directory):
    receipt = registry.migration_receipt(migration_id)
    if receipt is None:
        raise IdentityProvenanceRefused(
            "verified migration receipt is missing"
        )
    record = read_record(directory, RecordRef(sha256=receipt.record_sha256))
    payload = record.metadata.get("identity_migration")
    if not isinstance(payload, dict):
        raise IdentityProvenanceRefused(
            "process record has no migration evidence"
        )
    expected = {
        "request_sha256": receipt.migration_id,
        "map_sha256": receipt.map_sha256,
        "assignment_head": receipt.assignment_head,
        "correspondences": [
            c.model_dump(mode="json") for c in receipt.correspondences
        ],
        "assignments": [
            a.model_dump(mode="json") for a in receipt.assignments
        ],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise IdentityProvenanceRefused(
            "process record differs from migration receipt"
        )
    from yizkr.pipeline.clustering import IdentityMap
    from yizkr.pipeline.identity_migration import validated_map

    legacy = payload.get("map")
    if (
        not isinstance(legacy, dict)
        or legacy.get("hash") != receipt.map_hash
        or record_digest(legacy) != receipt.map_sha256
    ):
        raise IdentityProvenanceRefused(
            "original identity map does not match receipt"
        )
    legacy = validated_map(IdentityMap(legacy))
    # The receipt binds a freshly confirmed migration to exact accepted
    # assignment revisions. A retained assignment may have an earlier decision;
    # retain that origin independently of this migration's new confirmation.
    proof = {
        key: payload.get(key)
        for key in (
            "map",
            "map_sha256",
            "correspondences",
            "products",
            "decisions",
        )
    }
    if record_digest(proof) != receipt.migration_id:
        raise IdentityProvenanceRefused(
            "migration evidence differs from its identifier"
        )
    snapshot = registry.assignment_snapshot()
    supplied = payload.get("decisions")
    if not isinstance(supplied, dict):
        raise IdentityProvenanceRefused(
            "complete confirmation evidence is missing"
        )
    decisions = {}
    assignments = {a.mention_id: a for a in receipt.assignments}
    for assignment in assignments.values():
        if snapshot.assignments.get(assignment.mention_id) != assignment:
            raise IdentityProvenanceRefused(
                "accepted assignment revision has changed"
            )
    parsed = [Decision.model_validate(raw) for raw in supplied.values()]
    for correspondence in receipt.correspondences:
        matching = [
            d
            for d in parsed
            if (
                d.outcome == "approve"
                and d.proposal.operation == "assign"
                and d.proposal.mentions == (correspondence.mention_id,)
                and d.proposal.target is not None
                and d.proposal.target.model_dump(mode="json")
                == correspondence.binding.binding.model_dump(mode="json")
            )
        ]
        if len(matching) != 1:
            raise IdentityProvenanceRefused(
                "migration lacks exact affirmative confirmation"
            )
        decisions[correspondence.mention_id] = matching[0]
    for correspondence in receipt.correspondences:
        mention = legacy["mentions"].get(correspondence.legacy_fingerprint)
        cluster = legacy.get("clusters", {}).get(correspondence.cluster_id)
        if (
            correspondence.method not in METHOD_ITEMS
            or not isinstance(mention, dict)
            or not isinstance(cluster, dict)
            or mention.get("cluster") != correspondence.cluster_id
            or mention.get("method") != correspondence.method
            or cluster.get("method") != correspondence.method
            or cluster.get("kind") != correspondence.kind
            or correspondence.legacy_fingerprint
            not in cluster.get("members", [])
        ):
            raise IdentityProvenanceRefused(
                "original clustering method is unsupported"
            )
    producer = None
    if "provenance" in legacy:
        producer = ProcessRecord.model_validate(legacy["provenance"])
        producer_ref = RecordRef(sha256=process_digest(producer))
        if (
            producer_ref not in record.producer_refs
            or read_record(directory, producer_ref) != producer
        ):
            raise IdentityProvenanceRefused(
                "original producer record is unavailable or different"
            )
    return receipt, record, assignments, decisions, producer


def _targets(
    client, sources_to_locate, properties, items, *, source_qids=None
):
    if source_qids is None:
        try:
            source_qids = unique_source_qids(
                client,
                properties["media_sha1"],
                {c.artifact_sha1 for c in sources_to_locate},
            )
        except ValueError as exc:
            raise IdentityProvenanceRefused(str(exc)) from exc
    found = []
    entities = {}
    for correspondence in sources_to_locate:
        qid = correspondence.qid
        if qid not in entities:
            entities[qid] = client.wbi.item.get(qid)
        entity = entities[qid]
        source = source_qids[correspondence.artifact_sha1]
        matches = [
            statement
            for statement in entity.claims.get(properties["instance_of"]) or ()
            if isinstance(_value(statement.mainsnak), dict)
            and _value(statement.mainsnak).get("id")
            == items[correspondence.kind]
            and not list(statement.qualifiers)
            and any(
                isinstance(_value(snak), dict)
                and _value(snak).get("id") == source
                for reference in statement.references
                for snak in reference.snaks.get(properties["stated_in"]) or ()
            )
        ]
        if len(matches) != 1 or not matches[0].id:
            raise IdentityProvenanceRefused(
                "source-backed kind statement is missing or ambiguous"
            )
        found.append((correspondence, matches[0].id, source))
    return entities, found


def identity_references(
    assignment,
    decision,
    properties,
    items,
    record_qid,
    *,
    record_sha256,
    method,
    producer=None,
    migration_id=None,
):
    """Separate the assignment's origin from its affirmative confirmation."""
    origin = Reference()
    for snak in (
        Item(prop_nr=properties["identity_record"], value=record_qid),
        Item(
            prop_nr=properties["identity_method"],
            value=items[METHOD_ITEMS[method]],
        ),
        _string(
            properties, "identity_assignment_revision", assignment.revision_id
        ),
        _string(properties, "run_record_sha256", record_sha256),
    ):
        origin.add(snak)
    if migration_id is not None:
        origin.add(_string(properties, "identity_migration_id", migration_id))
    if producer is not None:
        origin.add(
            _string(
                properties,
                "identity_origin_record_sha256",
                process_digest(producer),
            )
        )
        run_id = (
            producer.metadata.get("producer_run_id") or producer.scratch_id
        )
        if run_id is not None:
            origin.add(_string(properties, "identity_origin_run", run_id))
    for decision_ref in assignment.decision_references:
        origin.add(
            _string(
                properties,
                "identity_decision_revision",
                f"{decision_ref.id}:{decision_ref.revision}",
            )
        )
    yield origin
    confirmation = Reference()
    for snak in (
        Item(prop_nr=properties["identity_record"], value=record_qid),
        _string(
            properties, "identity_assignment_revision", assignment.revision_id
        ),
        _string(
            properties,
            "identity_decision_revision",
            f"{decision.id}:{decision.revision}",
        ),
        _string(properties, "identity_confirmation_actor", decision.actor),
        _string(properties, "identity_confirmation_at", decision.time),
        _string(properties, "run_record_sha256", record_sha256),
    ):
        confirmation.add(snak)
    yield confirmation


def publish_identity_provenance(
    client, migration_id: str, *, record_dir: Path, dry_run: bool = False
) -> IdentityPublication:
    """Append graph-readable origin and confirmation for a saved migration.

    All source/kind matching and metadata vocabulary are checked before any
    mutation. A partial publication is recoverable by reading the same receipt
    again. Success is returned only after every expected block is read back
    from the authoritative entity API; no separate completion receipt is made.
    A dry run performs no publication.
    """
    check_cancelled()
    registry = CreationRegistry(client)
    receipt, record, assignments, decisions, producer = _read_evidence(
        registry, migration_id, record_dir
    )
    sources = tuple(
        IdentitySource(
            c.mention_id, c.artifact_sha1, c.kind, c.binding, c.method
        )
        for c in receipt.correspondences
    )

    def references(source, properties, items, qid):
        return identity_references(
            assignments[source.mention_id],
            decisions[source.mention_id],
            properties,
            items,
            qid,
            record_sha256=receipt.record_sha256,
            method=source.method,
            producer=producer,
            migration_id=migration_id,
        )

    return publish_identity_record(
        client,
        registry=registry,
        record=record,
        record_sha256=receipt.record_sha256,
        operation={
            "kind": "identity-provenance",
            "migration": migration_id,
            "record": receipt.record_sha256,
        },
        label=f"Identity decision record {migration_id[:16]}",
        sources=sources,
        references=references,
        revalidate=lambda: _read_evidence(registry, migration_id, record_dir),
        dry_run=dry_run,
    )


def publish_identity_record(
    client,
    *,
    registry,
    record,
    record_sha256,
    operation,
    label,
    sources,
    references,
    revalidate,
    revalidate_target=None,
    dry_run=False,
):
    """Publish one verified identity record and append exact kind references.

    The caller supplies immutable authority and revalidates it before effects.
    This common projection does not establish assignments or historical kinds.
    """
    revalidate()
    properties, items = client.vocabulary()
    try:
        validate_run_statements(client, record, record_kind=RECORD_KIND)
        entities, targets = _targets(client, sources, properties, items)
        # A harmless placeholder validates all reference values and vocabulary
        # before the process item can be created.
        for correspondence, _, _ in targets:
            list(references(correspondence, properties, items, "Q1"))
    except KeyError as exc:
        raise IdentityProvenanceRefused(
            f"identity provenance needs current model bootstrap: {exc.args[0]}"
        ) from exc
    binding = registry.lookup(operation)
    statements = tuple(sorted({(c.qid, guid) for c, guid, _ in targets}))
    if dry_run and binding is None:
        return IdentityPublication(None, True, statements)
    created = False
    if not dry_run:
        require_retained_code(client)
        item = client.wbi.item.new()
        item.labels.set("en", label)
        binding = registry.create(
            operation, item, summary="identity: verified decision provenance"
        )
        created = binding.created
    assert binding is not None
    changed = (
        ensure_run_statements(
            client,
            binding.qid,
            record,
            record_sha256,
            dry_run=dry_run,
            record_kind=RECORD_KIND,
        )
        or created
    )
    # Creation/metadata writes may have been interrupted or overlapped curator
    # work. Recheck authority and use fresh items, preserving current ranks.
    revalidate()
    entities, current_targets = _targets(client, sources, properties, items)
    if targets != current_targets:
        raise IdentityProvenanceRefused(
            "source-backed kind statement changed during publication"
        )
    expected = {}
    blocks = {}
    for correspondence, guid, _ in current_targets:
        qid = correspondence.qid
        for reference in references(
            correspondence, properties, items, binding.qid
        ):
            key = _reference_key(reference)
            expected.setdefault((qid, guid), set()).add(key)
            blocks.setdefault((qid, guid), {})[key] = reference
    for qid in sorted({qid for qid, _ in blocks}):
        if not dry_run:
            check_cancelled()
            require_retained_code(client)
            if revalidate_target is None:
                revalidate()
            else:
                revalidate_target(qid)
            # The source inventory is checked before publication, after
            # metadata creation and at completion. Each individual update
            # only needs a fresh target and its kind/source references;
            # rescanning the entire graph for every item is quadratic.
            selected = [t for t in current_targets if t[0].qid == qid]
            entities, refreshed = _targets(
                client,
                [source for source, _, _ in selected],
                properties,
                items,
                source_qids={
                    source.artifact_sha1: artifact
                    for source, _, artifact in selected
                },
            )
            if refreshed != selected:
                raise IdentityProvenanceRefused(
                    "source-backed kind statement changed during publication"
                )
        item = entities[qid]
        altered = False
        for (target, guid), reference_blocks in blocks.items():
            if target != qid:
                continue
            statement = next(s for s in item.claims if s.id == guid)
            present = {_reference_key(r) for r in statement.references}
            for key, reference in reference_blocks.items():
                if key not in present:
                    altered = True
                    if not dry_run:
                        statement.references.add(reference)
        if altered:
            changed = True
            if not dry_run:
                write_existing(
                    item,
                    summary="identity: append origin and confirmation",
                )
    if not dry_run:
        revalidate()
        entities, refreshed = _targets(client, sources, properties, items)
        if refreshed != current_targets:
            raise IdentityProvenanceRefused(
                "source-backed kind statement changed during publication"
            )
        for (qid, guid), expected_keys in expected.items():
            item = entities[qid]
            matches = [s for s in item.claims if s.id == guid]
            if len(matches) != 1 or not expected_keys <= {
                _reference_key(r) for r in matches[0].references
            }:
                raise IdentityProvenanceRefused(
                    "identity provenance publication is incomplete"
                )
    return IdentityPublication(binding.qid, changed, statements)
