"""Structured process metadata using real Wikibase datatype objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from wikibaseintegrator.datatypes import String
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

from tests.unit.test_pipeline import FakeWiki
from yizkr.core.provenance import (
    CodeProvenance,
    ProcessRecord,
    StageProvenance,
)
from yizkr.core.write_recovery import GENERATION_PAGE, CreationRefused
from yizkr.model.schema import load_manifest
from yizkr.pipeline.run_provenance import freeze_record, record_digest
from yizkr.pipeline.run_statements import (
    ensure_run_statements,
    validate_run_statements,
)

MANIFEST = load_manifest()
PROPERTIES = {
    key: f"P{number}" for number, key in enumerate(MANIFEST.properties, 1000)
}
ITEMS = {key: f"Q{number}" for number, key in enumerate(MANIFEST.items, 1000)}
START = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


class ProvenanceWiki(FakeWiki):
    def vocabulary(self):
        return dict(PROPERTIES), dict(ITEMS)


@pytest.fixture
def wiki():
    return ProvenanceWiki(detached=True)


def run_item(wiki):
    item = wiki.wbi.item.new()
    item.labels.set("en", "Fictional authored process")
    return item.write().id


def record():
    code = CodeProvenance(version="0.1.0", commit="a" * 40, dirty=False)
    return ProcessRecord(
        kind="batch-write-context",
        code=code,
        unratified=True,
        scratch_id="fictional-run",
        manifest_hash="b" * 64,
        run_manifest_hash="c" * 64,
        metadata={"identity_map_hash": "d" * 64},
        stages=(
            StageProvenance(
                stage="extraction",
                process="model",
                model="fictional-extractor",
                model_identifier="scripted:fictional-extractor",
                effort="low",
                prompt_hashes={"manifest": "d" * 64, "claims": "e" * 64},
                config_hash="f" * 64,
                resource_hashes={"model/manifest.yaml": "a" * 64},
                semantic_version=2,
                code=code,
                cli_version="scripted-cli-1.0",
                started_at=START,
                ended_at=START + timedelta(seconds=5),
            ),
            StageProvenance(
                stage="write-preparation",
                process="deterministic",
                semantic_version=1,
                code=code.model_copy(update={"commit": "b" * 40}),
            ),
        ),
    )


def statements(wiki, qid, key):
    return wiki.wbi.item.get(qid).claims.get(PROPERTIES[key]) or []


def value(statement):
    return statement.mainsnak.datavalue["value"]


def qualifiers(statement, key):
    return [
        snak.datavalue["value"]
        for snak in statement.qualifiers.get(PROPERTIES[key]) or []
    ]


def record_refs(statement):
    return [
        snak.datavalue["value"]
        for reference in statement.references
        for snak in reference.snaks.get(PROPERTIES["run_record_sha256"]) or []
    ]


def test_structured_stages_keep_actual_models_versions_times_and_record(
    wiki, tmp_path
):
    qid = run_item(wiki)
    provenance = record()
    ref = freeze_record(tmp_path, provenance)
    assert ensure_run_statements(wiki, qid, provenance, ref.sha256)
    stages = {
        value(statement): statement
        for statement in statements(wiki, qid, "run_stage")
    }
    assert set(stages) == {"extraction", "write-preparation"}
    extracted = stages["extraction"]
    assert qualifiers(extracted, "process_kind") == ["model"]
    assert qualifiers(extracted, "model_identifier") == [
        "scripted:fictional-extractor"
    ]
    assert qualifiers(extracted, "model_effort") == ["low"]
    assert qualifiers(extracted, "prompt_hash") == [
        "claims:" + "e" * 64,
        "manifest:" + "d" * 64,
    ]
    assert qualifiers(extracted, "cli_config_hash") == ["f" * 64]
    assert qualifiers(extracted, "provider_cli_version") == [
        "scripted-cli-1.0"
    ]
    assert qualifiers(extracted, "stage_version") == ["2"]
    assert qualifiers(extracted, "model_manifest_hash") == ["a" * 64]
    assert (
        qualifiers(extracted, "process_started_at")[0]
        == "2026-09-29T09:00:00Z"
    )
    assert qualifiers(extracted, "process_ended_at") == [
        "2026-09-29T09:00:05Z"
    ]
    applied = stages["write-preparation"]
    assert qualifiers(applied, "process_kind") == ["deterministic"]
    assert not qualifiers(applied, "model_identifier")
    assert not qualifiers(applied, "process_ended_at")
    assert qualifiers(applied, "software_commit") == ["b" * 40]
    assert qualifiers(extracted, "software_commit") == ["a" * 40]
    assert [
        value(item) for item in statements(wiki, qid, "model_manifest_hash")
    ] == ["b" * 64]
    assert [
        value(item) for item in statements(wiki, qid, "run_manifest_hash")
    ] == ["c" * 64]
    assert [
        value(item) for item in statements(wiki, qid, "unratified_run")
    ] == ["true"]
    assert not statements(wiki, qid, "process_ended_at")
    assert [
        value(item) for item in statements(wiki, qid, "identity_map_hash")
    ] == ["d" * 64]
    all_statements = list(wiki.wbi.item.get(qid).claims)
    assert all(record_refs(item) == [ref.sha256] for item in all_statements)
    assert all(
        not reference.snaks.get(PROPERTIES["stated_in"])
        for item in all_statements
        for reference in item.references
    )


def test_repeated_projection_does_not_write_or_replace_statement_ids(wiki):
    qid = run_item(wiki)
    provenance = record()
    digest = record_digest(provenance)
    ensure_run_statements(wiki, qid, provenance, digest)
    before = wiki.wbi.item.get(qid).get_json()
    writes = wiki.wbi.item.writes
    assert not ensure_run_statements(wiki, qid, provenance, digest)
    assert wiki.wbi.item.writes == writes
    assert wiki.wbi.item.get(qid).get_json() == before


def test_equal_curator_statement_keeps_rank_and_foreign_reference(wiki):
    qid = run_item(wiki)
    item = wiki.wbi.item.get(qid)
    curator = String(prop_nr=PROPERTIES["pipeline_version"], value="0.1.0")
    curator.rank = WikibaseRank.DEPRECATED
    reference = Reference()
    reference.add(
        String(
            prop_nr=PROPERTIES["provenance_limitation"],
            value="Authored fixture annotation",
        )
    )
    curator.references.add(reference)
    item.claims.add(curator, action_if_exists=ActionIfExists.FORCE_APPEND)
    item.write()
    (before,) = statements(wiki, qid, "pipeline_version")
    foreign = before.references.get_json()[0]
    provenance = record()
    digest = record_digest(provenance)
    assert ensure_run_statements(wiki, qid, provenance, digest)
    (after,) = statements(wiki, qid, "pipeline_version")
    assert after.id == before.id
    assert after.rank == WikibaseRank.DEPRECATED
    assert after.references.get_json()[0] == foreign
    assert record_refs(after) == [digest]
    assert not ensure_run_statements(wiki, qid, provenance, digest)


def test_different_stage_qualifiers_coexist_instead_of_merging(wiki):
    qid = run_item(wiki)
    first = record()
    ensure_run_statements(wiki, qid, first, record_digest(first))
    later = first.model_copy(
        update={
            "stages": (
                first.stages[0].model_copy(update={"effort": "high"}),
                first.stages[1],
            )
        }
    )
    ensure_run_statements(wiki, qid, later, record_digest(later))
    extraction = [
        item
        for item in statements(wiki, qid, "run_stage")
        if value(item) == "extraction"
    ]
    assert len(extraction) == 2
    by_effort = {
        qualifiers(item, "model_effort")[0]: item for item in extraction
    }
    assert record_refs(by_effort["low"]) == [record_digest(first)]
    assert record_refs(by_effort["high"]) == [record_digest(later)]
    (version,) = statements(wiki, qid, "pipeline_version")
    assert set(record_refs(version)) == {
        record_digest(first),
        record_digest(later),
    }


def test_dry_run_leaves_even_shared_client_objects_unmodified():
    wiki = ProvenanceWiki(detached=False)
    qid = run_item(wiki)
    before = wiki.wbi.item.get(qid).get_json()
    writes = wiki.wbi.item.writes
    provenance = record()
    assert ensure_run_statements(
        wiki, qid, provenance, record_digest(provenance), dry_run=True
    )
    assert wiki.wbi.item.writes == writes
    assert wiki.wbi.item.get(qid).get_json() == before


def test_wrong_record_reference_refuses_before_graph_access():
    class NoAccess:
        def vocabulary(self):
            pytest.fail("mismatched record must not access graph")

    with pytest.raises(ValueError, match="reference digest"):
        ensure_run_statements(NoAccess(), "Q1", record(), "0" * 64)


def test_unknowns_are_explicit_and_do_not_use_current_configuration(wiki):
    qid = run_item(wiki)
    provenance = ProcessRecord(
        kind="legacy-unknown",
        stages=(
            StageProvenance(
                stage="extraction",
                process="model",
                limitations=("Legacy producer configuration unavailable",),
            ),
        ),
        limitations=("Legacy commit unavailable",),
    )
    ensure_run_statements(wiki, qid, provenance, record_digest(provenance))
    assert not statements(wiki, qid, "software_commit")
    assert not statements(wiki, qid, "process_started_at")
    assert [
        value(item) for item in statements(wiki, qid, "software_dirty")
    ] == ["unknown"]
    assert [
        value(item) for item in statements(wiki, qid, "unratified_run")
    ] == ["unknown"]
    (stage,) = statements(wiki, qid, "run_stage")
    assert not qualifiers(stage, "model_identifier")
    assert qualifiers(stage, "provenance_limitation") == [
        "Legacy producer configuration unavailable"
    ]
    assert [
        value(item) for item in statements(wiki, qid, "provenance_limitation")
    ] == ["Legacy commit unavailable"]


def test_record_timezone_projects_utc_without_inventing_subsecond_precision(
    wiki,
):
    qid = run_item(wiki)
    stamp = START.astimezone(timezone(timedelta(hours=2))).replace(
        microsecond=123456
    )
    provenance = record().model_copy(update={"started_at": stamp})
    ensure_run_statements(wiki, qid, provenance, record_digest(provenance))
    (started,) = statements(wiki, qid, "process_started_at")
    assert value(started) == "2026-09-29T09:00:00.123456Z"
    assert provenance.started_at.microsecond == 123456


def test_provenance_vocabulary_is_not_offered_as_historical_extraction():
    for name in (
        "run_stage",
        "process_kind",
        "model_effort",
        "prompt_hash",
        "stage_version",
        "process_started_at",
        "process_ended_at",
        "cli_config_hash",
        "provider_cli_version",
        "model_manifest_hash",
        "software_dirty",
        "unratified_run",
        "provenance_limitation",
        "run_record_sha256",
    ):
        assert not MANIFEST.properties[name].in_prompts
    assert MANIFEST.properties["run_record_sha256"].reference


def test_missing_vocabulary_preflight_cannot_read_or_mutate_an_item():
    class MissingVocabulary:
        def vocabulary(self):
            properties = dict(PROPERTIES)
            del properties["prompt_hash"]
            return properties, ITEMS

    with pytest.raises(ValueError, match="model bootstrap: prompt_hash"):
        validate_run_statements(MissingVocabulary(), record())


def test_generation_refusal_precedes_changes_to_shared_item_objects():
    wiki = ProvenanceWiki(detached=False)
    qid = run_item(wiki)
    wiki._recovery_pages.pop(GENERATION_PAGE)
    before = wiki.wbi.item.get(qid).get_json()
    writes = wiki.wbi.item.writes
    provenance = record()
    with pytest.raises(CreationRefused):
        ensure_run_statements(wiki, qid, provenance, record_digest(provenance))
    assert wiki.wbi.item.writes == writes
    assert wiki.wbi.item.get(qid).get_json() == before


def test_successful_update_with_lost_response_is_reconciled_on_retry(
    wiki, monkeypatch
):
    qid = run_item(wiki)
    provenance = record()
    digest = record_digest(provenance)
    get = wiki.wbi.item.get
    item = get(qid)
    write = item.write

    def interrupted(**kwargs):
        assert kwargs["baserevid"] == item.lastrevid
        assert kwargs["max_retries"] == 1
        assert kwargs["retry_after"] == 0
        write(**kwargs)
        raise ConnectionError("fictional response lost after update")

    item.write = interrupted
    with monkeypatch.context() as patch:
        patch.setattr(wiki.wbi.item, "get", lambda _: item)
        with pytest.raises(ConnectionError, match="response lost"):
            ensure_run_statements(wiki, qid, provenance, digest)
    persisted = get(qid).get_json()
    writes = wiki.wbi.item.writes
    assert not ensure_run_statements(wiki, qid, provenance, digest)
    assert wiki.wbi.item.writes == writes
    assert get(qid).get_json() == persisted


@pytest.mark.parametrize(
    "identifier", ["fictional-model" * 40, "fictional-model\nv2"]
)
def test_unrepresentable_model_is_kept_exact_only_in_record(
    wiki, tmp_path, identifier
):
    qid = run_item(wiki)
    original = record()
    provenance = original.model_copy(
        update={
            "stages": (
                original.stages[0].model_copy(
                    update={"model_identifier": identifier}
                ),
            )
        }
    )
    ref = freeze_record(tmp_path, provenance)
    from yizkr.pipeline.run_provenance import read_record

    assert read_record(tmp_path, ref).stages[0].model_identifier == identifier
    ensure_run_statements(wiki, qid, provenance, ref.sha256)
    (stage,) = statements(wiki, qid, "run_stage")
    assert not qualifiers(stage, "model_identifier")
    assert qualifiers(stage, "provenance_limitation") == [
        "model_identifier: full value cannot be represented as a Wikibase "
        "string; see the immutable process record"
    ]
    assert record_refs(stage) == [ref.sha256]
    assert not ensure_run_statements(wiki, qid, provenance, ref.sha256)
