"""Text products survive interruptions without cache or graph authority."""

from __future__ import annotations

import fcntl
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.fixtures.semantic_acceptance import source_case
from tests.unit.test_text_stages import (
    ScriptedClient,
    birth_answer,
    scripted_text_client,
)
from yizkr.core.corpus import ExclusionList, read_exclusions
from yizkr.pipeline import run_provenance, text_stages
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.text_products import (
    LOCK_FILE,
    PRODUCT_FILE,
    TextProducts,
    TextProductsError,
    TextProductsIncomplete,
    TextSettings,
    load_text_products,
    prepare_text_products,
)

SETTINGS = TextSettings(language="en", strategy="chunked")


@pytest.fixture(autouse=True)
def fixed_producer_time(monkeypatch):
    monkeypatch.setattr(
        run_provenance, "now", lambda: datetime(2026, 1, 1, 12, tzinfo=UTC)
    )


@pytest.fixture
def paths(tmp_path):
    source = tmp_path / "naturalisation-1952.txt"
    source.write_bytes(source_case("naturalisation-1952").source_bytes)
    return source, tmp_path / "products"


def prepare(paths, **kwargs):
    return prepare_text_products(
        *paths,
        exclusions=kwargs.pop("exclusions", ExclusionList()),
        settings=kwargs.pop("settings", SETTINGS),
        extractor_client=kwargs.pop(
            "extractor_client", scripted_text_client()
        ),
        critic_client=kwargs.pop(
            "critic_client", ScriptedClient('{"done":true}')
        ),
        **kwargs,
    )


def stored(directory):
    return json.loads((directory / PRODUCT_FILE).read_text())


def test_complete_products_preserve_producers_and_need_no_clients_or_cache(
    paths,
):
    extractor = scripted_text_client()
    critic = ScriptedClient('{"done":true}')
    products = prepare(paths, extractor_client=extractor, critic_client=critic)
    assert len(extractor.prompts) == 2 and len(critic.prompts) == 1
    assert not extractor.closed and not critic.closed
    assert products.reading.text == paths[0].read_text()
    assert products.reading.pages is None
    assert products.batch.claims[0].source.artifact == products.artifact
    assert products.reading.provenance is not None
    assert products.batch.extraction_run.provenance is not None
    assert products.critique.provenance is not None
    assert products.extraction_dependencies["model"] == {
        "model_identifier": extractor.model_identifier
    }
    before = (paths[1] / PRODUCT_FILE).read_bytes()
    assert (
        prepare(paths, extractor_client=None, critic_client=None) == products
    )
    assert load_text_products(*paths, exclusions=ExclusionList()) == products
    assert (paths[1] / PRODUCT_FILE).read_bytes() == before
    assert TextProducts.model_validate_json(before) == products


def test_invalid_critic_findings_remain_visible_in_completed_snapshot(paths):
    finding = {
        "entity": "missing-fictional-mention",
        "axis": "subject",
        "category": "wrong_kind",
        "issue": "Fictional unmatched mention needs review.",
    }
    products = prepare(
        paths,
        critic_client=ScriptedClient(json.dumps(finding) + '\n{"done":true}'),
    )
    (invalid,) = products.critique.findings
    assert invalid.invalid == finding
    assert invalid.why
    assert load_text_products(*paths, exclusions=ExclusionList()) == products


def test_omitted_settings_resume_the_saved_configuration(paths):
    settings = SETTINGS.model_copy(update={"fallback_language": "de"})
    with pytest.raises(TextProductsIncomplete, match="critique needs"):
        prepare(paths, settings=settings, critic_client=None)
    products = prepare_text_products(
        *paths,
        exclusions=ExclusionList(),
        critic_client=ScriptedClient('{"done":true}'),
    )
    assert products.settings == settings
    assert (
        prepare_text_products(*paths, exclusions=ExclusionList()) == products
    )


def test_interrupted_extraction_reuses_completed_reading(paths, monkeypatch):
    error = KeyboardInterrupt("fictional extractor interrupted")
    with pytest.raises(KeyboardInterrupt, match="extractor interrupted"):
        prepare(paths, extractor_client=ScriptedClient(error))
    reading = stored(paths[1])["reading"]
    assert stored(paths[1])["batch"] is None
    with pytest.raises(TextProductsIncomplete):
        load_text_products(*paths, exclusions=ExclusionList())

    def no_reading(*args, **kwargs):
        pytest.fail("completed reading must not run again")

    monkeypatch.setattr(text_stages, "read_text", no_reading)
    products = prepare(paths)
    assert products.reading.model_dump(mode="json") == reading


def test_partial_extraction_is_diagnostic_and_does_not_publish_a_batch(paths):
    settings = TextSettings(language="en", strategy="single")
    with pytest.raises(TextProductsIncomplete, match="terminal sentinel"):
        prepare(
            paths,
            settings=settings,
            extractor_client=ScriptedClient(birth_answer(finished=False)),
        )
    assert stored(paths[1])["batch"] is None
    diagnostic = json.loads(
        (paths[1] / "incomplete-extraction.json").read_text()
    )
    assert diagnostic["status"] == "incomplete"
    assert diagnostic["diagnostic_batch"]["claims"]
    products = prepare(
        paths,
        settings=settings,
        extractor_client=ScriptedClient(birth_answer()),
    )
    assert products.batch.claims
    assert not (paths[1] / "incomplete-extraction.json").exists()


@pytest.mark.parametrize("outcome", ["missing", "incomplete", "interrupted"])
def test_critique_resume_needs_no_extractor_and_keeps_original_batch(
    paths, outcome
):
    critic = {
        "missing": None,
        "incomplete": ScriptedClient(""),
        "interrupted": ScriptedClient(
            RuntimeError("fictional critic stopped")
        ),
    }[outcome]
    with pytest.raises((TextProductsIncomplete, RuntimeError)):
        prepare(paths, critic_client=critic)
    checkpoint = stored(paths[1])
    assert checkpoint["batch"] and checkpoint["critique"] is None
    with pytest.raises(TextProductsIncomplete):
        load_text_products(*paths, exclusions=ExclusionList())
    if outcome == "incomplete":
        diagnostic = json.loads(
            (paths[1] / "incomplete-critique.json").read_text()
        )
        assert diagnostic["status"] == "incomplete"
        assert diagnostic["incomplete_reasons"]
    products = prepare(paths, extractor_client=None)
    assert products.batch.model_dump(mode="json") == checkpoint["batch"]
    assert not (paths[1] / "incomplete-critique.json").exists()


@pytest.mark.parametrize("changed", ["source", "settings"])
def test_changed_inputs_never_replace_completed_products(paths, changed):
    prepare(paths)
    before = (paths[1] / PRODUCT_FILE).read_bytes()
    kwargs = {}
    if changed == "source":
        paths[0].write_text(paths[0].read_text() + "\nFictional amendment.\n")
    else:
        kwargs["settings"] = SETTINGS.model_copy(update={"retries": 4})
    with pytest.raises(TextProductsError, match="differ"):
        prepare(paths, extractor_client=None, critic_client=None, **kwargs)
    assert (paths[1] / PRODUCT_FILE).read_bytes() == before


@pytest.mark.parametrize("damage", ["checksum", "parent", "version", "torn"])
def test_damaged_checkpoint_is_refused_without_replacing_it(paths, damage):
    prepare(paths)
    path = paths[1] / PRODUCT_FILE
    data = stored(paths[1])
    if damage == "checksum":
        data["digest"] = "0" * 64
    elif damage == "parent":
        data["extraction_dependencies"]["transcription_sha256"] = "0" * 64
        data["digest"] = content_hash(
            {k: v for k, v in data.items() if k != "digest"}
        )
    elif damage == "version":
        data["format"] = 2
    path.write_text("{" if damage == "torn" else json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(TextProductsError, match="invalid"):
        load_text_products(*paths, exclusions=ExclusionList())
    with pytest.raises(TextProductsError, match="invalid"):
        prepare(paths)
    assert path.read_bytes() == before


def test_nested_product_edits_cannot_keep_the_previous_digest(paths):
    products = prepare(paths)
    data = products.model_dump(mode="json")
    data["reading"]["text"] += "\nFictional amendment."
    with pytest.raises(ValidationError, match="checksum"):
        TextProducts.model_validate(data)


@pytest.mark.parametrize("policy", [None, ExclusionList(patterns=("*.txt",))])
def test_no_model_or_product_is_created_for_missing_or_excluding_policy(
    paths, policy
):
    extractor = scripted_text_client()
    with pytest.raises(TextProductsError):
        prepare(paths, exclusions=policy, extractor_client=extractor)
    assert extractor.prompts == []
    assert not (paths[1] / PRODUCT_FILE).exists()


def test_saved_original_name_and_current_name_both_remain_excluded(paths):
    prepare(paths)
    renamed = paths[0].with_name("renamed-certificate.txt")
    paths[0].rename(renamed)
    for name in (paths[0].name, renamed.name):
        policy = ExclusionList(patterns=(name,))
        with pytest.raises(TextProductsError, match="never-ingest"):
            load_text_products(renamed, paths[1], exclusions=policy)
    assert (
        load_text_products(
            renamed, paths[1], exclusions=ExclusionList()
        ).source_name
        == paths[0].name
    )


@pytest.mark.parametrize("after", ["reading", "extraction"])
def test_policy_is_reloaded_between_stages(paths, monkeypatch, after):
    policy = paths[0].parent / "exclusions.txt"
    policy.write_text("")
    exclusions = read_exclusions(policy)
    operation = "read_text" if after == "reading" else "extract"
    original = getattr(text_stages, operation)

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        policy.write_text(paths[0].name)
        return result

    monkeypatch.setattr(text_stages, operation, changing)
    critic = ScriptedClient('{"done":true}')
    with pytest.raises(TextProductsError, match="never-ingest"):
        prepare(paths, exclusions=exclusions, critic_client=critic)
    assert critic.prompts == []
    if after == "reading":
        assert not (paths[1] / PRODUCT_FILE).exists()
    else:
        assert stored(paths[1])["batch"] is None


def test_loading_rechecks_file_policy_and_does_not_write(paths):
    prepare(paths)
    policy = paths[0].parent / "exclusions.txt"
    policy.write_text("")
    exclusions = read_exclusions(policy)
    before = {
        p.name: p.read_bytes() for p in paths[1].iterdir() if p.is_file()
    }
    load_text_products(*paths, exclusions=exclusions)
    assert before == {
        p.name: p.read_bytes() for p in paths[1].iterdir() if p.is_file()
    }
    policy.write_text(paths[0].name)
    with pytest.raises(TextProductsError, match="never-ingest"):
        load_text_products(*paths, exclusions=exclusions)


def test_source_and_policy_must_stay_outside_the_workspace(paths):
    with pytest.raises(ValueError, match="contains input"):
        prepare_text_products(
            paths[0], paths[0].parent, exclusions=ExclusionList()
        )
    paths[1].mkdir()
    policy = paths[1] / "exclusions.txt"
    policy.write_text("")
    with pytest.raises(ValueError, match="contains input"):
        prepare(paths, exclusions=read_exclusions(policy))


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_diagnostic_outputs_cannot_alias_source_bytes(paths, link):
    paths[1].mkdir()
    output = paths[1] / "extraction-answer.txt"
    getattr(output, f"{link}_to")(paths[0])
    before = paths[0].read_bytes()
    with pytest.raises(ValueError, match="overlaps input"):
        prepare(paths)
    assert paths[0].read_bytes() == before


def test_workspace_lock_refuses_concurrent_preparation_and_load(paths):
    prepare(paths)
    with (paths[1] / LOCK_FILE).open("r") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TextProductsError, match="in use"):
            prepare(paths)
        with pytest.raises(TextProductsError, match="in use"):
            load_text_products(*paths, exclusions=ExclusionList())
    assert load_text_products(*paths, exclusions=ExclusionList()).batch.claims


def test_changed_source_during_stage_cannot_publish_its_result(
    paths, monkeypatch
):
    original = text_stages.extract

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        paths[0].write_text(paths[0].read_text() + "\nFictional amendment.\n")
        return result

    monkeypatch.setattr(text_stages, "extract", changing)
    with pytest.raises(TextProductsError, match="bytes differ"):
        prepare(paths)
    assert stored(paths[1])["batch"] is None


def test_incomplete_result_cannot_be_promoted_by_a_success_status(
    paths, monkeypatch
):
    original = text_stages.critique

    def inconsistent(*args, **kwargs):
        return replace(
            original(*args, **kwargs), incomplete_reasons=["unfinished"]
        )

    monkeypatch.setattr(text_stages, "critique", inconsistent)
    with pytest.raises(ValueError, match="incomplete"):
        prepare(paths)
    assert stored(paths[1])["critique"] is None
