"""Project unratified map placement evidence for verified cluster creations.

The map and its actual producer are the evidence. Publication neither creates
accepted assignments nor infers human confirmation from a writer invocation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.models import Reference

from yizkr.core.identity_assignments import record_digest as map_digest
from yizkr.core.identity_migration import MigrationRefused
from yizkr.core.provenance import ProcessRecord, RecordRef
from yizkr.core.write_recovery import (
    CreationRegistry,
    EntityBinding,
    cluster_creation_operation,
)
from yizkr.pipeline.clustering import IdentityMap
from yizkr.pipeline.identity_migration import validated_map
from yizkr.pipeline.identity_provenance import (
    METHOD_ITEMS,
    RECORD_KIND,
    IdentityProvenanceRefused,
    IdentityPublication,
    _string,
    publish_identity_record,
)
from yizkr.pipeline.run_provenance import (
    freeze_record,
    read_record,
    record_bytes,
    record_digest,
)
from yizkr.pipeline.run_statements import validate_run_statements


@dataclass(frozen=True)
class PreparedMapOrigin:
    map_hash: str
    record_sha256: str
    record_json: str

    @property
    def record(self) -> ProcessRecord:
        # JSON owns the immutable snapshot; callers cannot mutate later uses
        # through the dict fields inside an otherwise frozen ProcessRecord.
        return ProcessRecord.model_validate_json(self.record_json)


@dataclass(frozen=True)
class MapOriginSelection:
    document: str
    cluster_id: str
    artifact_sha1: str
    kind: str
    binding: EntityBinding

    @property
    def qid(self) -> str:
        return self.binding.qid


def prepare_map_origin(identity: IdentityMap) -> PreparedMapOrigin:
    """Snapshot and validate the complete map without graph or disk access."""
    try:
        data = validated_map(identity, require_uniform_kind=False)
    except MigrationRefused as exc:
        raise IdentityProvenanceRefused(str(exc)) from exc
    producer = (
        ProcessRecord.model_validate(data["provenance"])
        if "provenance" in data
        else None
    )
    limits = (
        "map placement is unratified; it does not establish an accepted "
        "assignment or human confirmation",
    )
    if producer is None:
        limits += (
            "original map producer model, prompt, run and code metadata "
            "are unavailable",
        )
    else:
        limits += producer.limitations
    record = ProcessRecord(
        kind="identity-map-origin",
        metadata={
            "identity_map_hash": data["hash"],
            "identity_map_sha256": map_digest(data),
            "identity_map": data,
        },
        producer_refs=(RecordRef(sha256=record_digest(producer)),)
        if producer is not None
        else (),
        stages=producer.stages if producer is not None else (),
        code=producer.code if producer is not None else None,
        scratch_id=producer.scratch_id if producer is not None else None,
        manifest_hash=producer.manifest_hash if producer is not None else None,
        run_manifest_hash=producer.run_manifest_hash
        if producer is not None
        else None,
        started_at=producer.started_at if producer is not None else None,
        ended_at=producer.ended_at if producer is not None else None,
        unratified=True,
        limitations=limits,
    )
    return PreparedMapOrigin(
        map_hash=data["hash"],
        record_sha256=record_digest(record),
        record_json=record_bytes(record).decode("utf-8"),
    )


def _evidence(prepared):
    record = prepared.record
    data = record.metadata.get("identity_map")
    if (
        not isinstance(data, dict)
        or prepare_map_origin(IdentityMap(data)) != prepared
    ):
        raise IdentityProvenanceRefused("prepared map evidence has changed")
    producer = (
        ProcessRecord.model_validate(data["provenance"])
        if "provenance" in data
        else None
    )
    return record, data, producer


def _references(
    prepared, producer, cluster_id, method, properties, items, record_qid
):
    origin = Reference()
    for snak in (
        Item(prop_nr=properties["identity_record"], value=record_qid),
        Item(
            prop_nr=properties["identity_method"],
            value=items[METHOD_ITEMS[method]],
        ),
        _string(properties, "identity_cluster", cluster_id),
        _string(properties, "run_record_sha256", prepared.record_sha256),
    ):
        origin.add(snak)
    if producer is not None:
        origin.add(
            _string(
                properties,
                "identity_origin_record_sha256",
                record_digest(producer),
            )
        )
        run_id = (
            producer.metadata.get("producer_run_id") or producer.scratch_id
        )
        if run_id is not None:
            origin.add(_string(properties, "identity_origin_run", run_id))
    return (origin,)


def _validated_evidence(client, prepared):
    record, data, producer = _evidence(prepared)
    properties, items = client.vocabulary()
    try:
        validate_run_statements(client, record, record_kind=RECORD_KIND)
        # Full-map validation already verifies every cluster locator. Check
        # each actual method once, rather than rebuilding equal vocabulary
        # checks for every corpus cluster on each document publication.
        methods = {
            cluster["method"]: cluster_id
            for cluster_id, cluster in data["clusters"].items()
        }
        for method, cluster_id in methods.items():
            _references(
                prepared,
                producer,
                cluster_id,
                method,
                properties,
                items,
                "Q1",
            )
    except KeyError as exc:
        raise IdentityProvenanceRefused(
            f"identity provenance needs current model bootstrap: {exc.args[0]}"
        ) from exc
    return record, data, producer


def validate_map_origin(client, prepared: PreparedMapOrigin) -> None:
    """Preflight map evidence and origin vocabulary before writer effects."""
    _validated_evidence(client, prepared)


def publish_map_origin(
    client,
    prepared: PreparedMapOrigin,
    *,
    selections: Sequence[MapOriginSelection],
    record_dir: Path,
    dry_run: bool = False,
) -> IdentityPublication:
    """Append origins only to verified map creations with current support.

    Selections distinguish the map's document from the actual cited source,
    which may be another page. A reviewed kind may likewise differ from the
    original map kind. The shared publisher verifies each actual kind/source
    statement; the creation receipt proves the map's entity placement.
    """
    record, data, producer = _validated_evidence(client, prepared)
    sources = tuple(dict.fromkeys(selections))
    if not sources:
        return IdentityPublication(None, False, ())
    registry = CreationRegistry(client)
    by_qid = {}
    for source in sources:
        cluster = data["clusters"].get(source.cluster_id)
        if cluster is None or not any(
            data["mentions"][member]["document"] == source.document
            for member in cluster["members"]
        ):
            raise IdentityProvenanceRefused(
                "selected document does not belong to the map cluster"
            )
        by_qid.setdefault(source.qid, []).append(source)

    def revalidate_bindings(selected):
        for cluster_id, binding in dict.fromkeys(
            (source.cluster_id, source.binding) for source in selected
        ):
            current = registry.lookup(
                cluster_creation_operation(prepared.map_hash, cluster_id)
            )
            if current is None or current.model_dump() != (
                binding.model_dump()
            ):
                raise IdentityProvenanceRefused(
                    "selected entity differs from its map creation receipt"
                )

    def revalidate():
        revalidate_bindings(sources)
        if not dry_run:
            if (
                read_record(
                    record_dir, RecordRef(sha256=prepared.record_sha256)
                )
                != record
            ):
                raise IdentityProvenanceRefused("map origin record changed")
            if (
                producer is not None
                and read_record(
                    record_dir, RecordRef(sha256=record_digest(producer))
                )
                != producer
            ):
                raise IdentityProvenanceRefused("map producer record changed")

    if not dry_run:
        if producer is not None:
            freeze_record(record_dir, producer)
        freeze_record(record_dir, record)

    def references(source, properties, items, record_qid):
        return _references(
            prepared,
            producer,
            source.cluster_id,
            data["clusters"][source.cluster_id]["method"],
            properties,
            items,
            record_qid,
        )

    return publish_identity_record(
        client,
        registry=registry,
        record=record,
        record_sha256=prepared.record_sha256,
        operation={
            "kind": "identity-map-origin",
            "record": prepared.record_sha256,
        },
        label=f"Identity map origin {prepared.record_sha256[:16]}",
        sources=sources,
        references=references,
        revalidate=revalidate,
        revalidate_target=lambda qid: revalidate_bindings(by_qid[qid]),
        dry_run=dry_run,
    )
