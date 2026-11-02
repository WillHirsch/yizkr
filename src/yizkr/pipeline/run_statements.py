"""Project immutable process records onto their graph run item.

The record remains the complete off-graph evidence. Every projected statement
references its content digest; source-document citations are never fabricated
for process bookkeeping. Equal statements keep their rank and existing
references, and gain only a missing record reference.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import ActionIfExists

from yizkr.core.provenance import CodeProvenance, ProcessRecord
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.run_provenance import record_digest

if TYPE_CHECKING:
    from yizkr.core.wiki import WikiClient


RECORD_PROPERTY = "run_record_sha256"


def _text(value: str) -> str:
    # Matching uses the same canonical Unicode form returned by Wikibase.
    return unicodedata.normalize("NFC", value)


def _boolean(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


def _at(value: datetime) -> str:
    # Native Wikibase Time supports days, not process clock timestamps.
    # ISO strings retain the observation without inventing date precision.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _statements(properties: dict, items: dict, record: ProcessRecord):
    # Reuse the writer's actual Wikibase string constraints. Import at call
    # time: the writer also calls this projection after its own validation.
    from yizkr.pipeline.writer import _faults

    def string(key: str, value: str, omitted: set[str]):
        text = _text(value)
        if not text or _faults(text):
            omitted.add(key)
            return None
        return String(prop_nr=properties[key], value=text)

    def limitations(omitted: set[str]):
        for key in sorted(omitted):
            yield String(
                prop_nr=properties["provenance_limitation"],
                value=(
                    f"{key}: full value cannot be represented as a Wikibase "
                    "string; see the immutable process record"
                ),
            )

    def code_values(code: CodeProvenance | None, omitted: set[str]):
        if code is not None:
            if code.version is not None:
                yield string("pipeline_version", code.version, omitted)
            if code.commit is not None:
                yield string("software_commit", code.commit, omitted)
        yield string(
            "software_dirty", _boolean(code.dirty if code else None), omitted
        )

    omitted: set[str] = set()
    found = [
        Item(prop_nr=properties["instance_of"], value=items["extraction_run"]),
        string("process_kind", record.kind, omitted),
        *code_values(record.code, omitted),
        string("unratified_run", _boolean(record.unratified), omitted),
    ]
    for key, value in (
        ("scratch_run", record.scratch_id),
        ("model_manifest_hash", record.manifest_hash),
        ("run_manifest_hash", record.run_manifest_hash),
        ("identity_map_hash", record.metadata.get("identity_map_hash")),
    ):
        if value is not None:
            found.append(string(key, value, omitted))
    for key, value in (
        ("process_started_at", record.started_at),
        ("process_ended_at", record.ended_at),
    ):
        if value is not None:
            found.append(string(key, _at(value), omitted))
    for limitation in record.limitations:
        found.append(string("provenance_limitation", limitation, omitted))
    for stage in record.stages:
        stage_omitted: set[str] = set()
        statement = string("run_stage", stage.stage, stage_omitted)
        if statement is None:
            omitted.update(stage_omitted)
            continue
        qualifiers = list(code_values(stage.code, stage_omitted))
        for key, value in (
            ("process_kind", stage.process),
            ("model_identifier", stage.model_identifier or stage.model),
            ("model_effort", stage.effort),
            ("cli_config_hash", stage.config_hash),
            ("provider_cli_version", stage.cli_version),
            ("stage_version", stage.semantic_version),
            (
                "model_manifest_hash",
                stage.resource_hashes.get("model/manifest.yaml"),
            ),
        ):
            if value is not None:
                qualifiers.append(string(key, str(value), stage_omitted))
        for name, digest in sorted(stage.prompt_hashes.items()):
            qualifiers.append(
                string("prompt_hash", f"{name}:{digest}", stage_omitted)
            )
        for key, value in (
            ("process_started_at", stage.started_at),
            ("process_ended_at", stage.ended_at),
        ):
            if value is not None:
                qualifiers.append(string(key, _at(value), stage_omitted))
        qualifiers.extend(
            string("provenance_limitation", value, stage_omitted)
            for value in stage.limitations
        )
        qualifiers.extend(limitations(stage_omitted))
        for qualifier in qualifiers:
            if qualifier is not None:
                statement.qualifiers.add(
                    qualifier,
                    action_if_exists=ActionIfExists.APPEND_OR_REPLACE,
                )
        found.append(statement)
    found.extend(limitations(omitted))
    return [statement for statement in found if statement is not None]


def _snak(snak) -> str:
    data = snak.get_json()
    return json.dumps(
        {
            key: data.get(key)
            for key in ("snaktype", "property", "datatype", "datavalue")
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _statement_key(statement):
    qualifiers = sorted(_snak(snak) for snak in statement.qualifiers)
    return _snak(statement.mainsnak), qualifiers


def _has_record(statement, property_id: str, digest: str) -> bool:
    return any(
        (snak.datavalue or {}).get("value") == digest
        for reference in statement.references
        for snak in reference.snaks.get(property_id) or ()
    )


def _planned(properties: dict, items: dict, record: ProcessRecord):
    try:
        record_property = properties[RECORD_PROPERTY]
        statements = list(_statements(properties, items, record))
    except KeyError as exc:
        raise ValueError(
            "process provenance needs the current model bootstrap: "
            f"{exc.args[0]}"
        ) from exc
    return record_property, statements


def validate_run_statements(client: WikiClient, record: ProcessRecord) -> None:
    """Check the projection vocabulary before creation or other mutations."""
    record = ProcessRecord.model_validate(record.model_dump(mode="json"))
    _planned(*client.vocabulary(), record)


def ensure_run_statements(
    client: WikiClient,
    qid: str,
    record: ProcessRecord,
    record_sha256: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Add missing process statements/references in one guarded update.

    The caller publishes the immutable record before mutation. A digest
    mismatch refuses before graph access. The caller also owns the existing
    run item's durable creation binding; this operation never allocates items
    or infers an identity from display terms. Returns whether an update is
    needed, including in a read-only dry run.
    """
    record = ProcessRecord.model_validate(record.model_dump(mode="json"))
    if record_digest(record) != record_sha256:
        raise ValueError("process record does not match its reference digest")
    record_property, statements = _planned(*client.vocabulary(), record)
    item = client.wbi.item.get(qid)
    changed = False
    for statement in statements:
        key = _statement_key(statement)
        matches = [
            existing
            for existing in item.claims.get(statement.mainsnak.property_number)
            or ()
            if _statement_key(existing) == key
        ]
        if any(
            _has_record(existing, record_property, record_sha256)
            for existing in matches
        ):
            continue
        if not changed:
            registry = CreationRegistry(client)
            if dry_run:
                registry.check_generation()
            else:
                registry.ensure_generation()
        changed = True
        if dry_run:
            continue
        reference = Reference()
        reference.add(String(prop_nr=record_property, value=record_sha256))
        if matches:
            matches[0].references.add(reference)
        else:
            statement.references.add(reference)
            # Matching already includes all qualifiers. Never let the library
            # replace a differently qualified statement or a curator's rank.
            item.claims.add(
                statement, action_if_exists=ActionIfExists.FORCE_APPEND
            )
    if changed and not dry_run:
        write_existing(item, summary="ingest: immutable process provenance")
    return changed
