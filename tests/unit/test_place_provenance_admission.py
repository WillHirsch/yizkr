"""Malformed saved origins refuse before place-application effects."""

import copy

import pytest

from tests.unit.test_places import FINGERPRINT, RunItemWiki
from tests.unit.test_scratch_places import PlaceRun
from yizkr.core.instance import recorded_contents
from yizkr.core.provenance import ProcessRecord, StageProvenance
from yizkr.core.write_recovery import CreationRefused
from yizkr.pipeline import places, scratchwrite

BAD_ORIGINS = [
    {},
    [],
    False,
    "",
    {
        "kind": "producer",
        "stages": [{"stage": "extraction", "process": "deterministic"}],
    },
]


@pytest.mark.parametrize("configured_model", [None, "fictional-configured"])
def test_unbound_legacy_place_run_still_requires_migration(
    tmp_path, configured_model
):
    wiki = RunItemWiki()
    old_model = configured_model or "curator-reviewed"
    old_qid = wiki.item(f"place-hierarchy run {old_model} {FINGERPRINT[:10]}")
    producer = (
        ProcessRecord(
            kind="producer",
            stages=(
                StageProvenance(
                    stage="places",
                    process="model",
                    model_identifier="fictional-observed-client",
                ),
            ),
        )
        if configured_model
        else None
    )
    before = copy.deepcopy(wiki.entities)
    pages = dict(wiki._recovery_pages)
    with pytest.raises(CreationRefused, match="migration"):
        places.ensure_places_run_item(
            wiki,
            FINGERPRINT,
            binding_model=configured_model,
            producer=producer,
            record_dir=tmp_path,
        )
    assert wiki.entities == before and old_qid in wiki.entities
    assert wiki._recovery_pages == pages


@pytest.mark.parametrize("origin", BAD_ORIGINS)
def test_public_place_run_refuses_invalid_origin_before_effects(
    tmp_path, origin
):
    wiki = RunItemWiki()
    before = copy.deepcopy(wiki.entities)
    pages = dict(wiki._recovery_pages)
    with pytest.raises(ValueError):
        places.ensure_places_run_item(
            wiki, FINGERPRINT, producer=origin, record_dir=tmp_path
        )
    assert wiki.entities == before
    assert wiki._recovery_pages == pages
    assert list(tmp_path.iterdir()) == []


@pytest.mark.write_side
@pytest.mark.parametrize("origin", BAD_ORIGINS)
@pytest.mark.parametrize("saved_run_item", [False, True])
def test_saved_plan_refuses_invalid_origin_before_clearing_or_writing(
    tmp_path, monkeypatch, origin, saved_run_item
):
    run = PlaceRun(tmp_path, monkeypatch)
    run.past_the_barrier(until="write")
    propose = scratchwrite._propose

    def pause_after_planning(*args, **kwargs):
        propose(*args, **kwargs)
        raise RuntimeError("fictional pause after planning")

    monkeypatch.setattr(scratchwrite, "_propose", pause_after_planning)
    with pytest.raises(RuntimeError, match="pause after planning"):
        run.stage("places")
    record = run.record("places")
    assert record["planned"] and not record.get("cleared")
    record["producer_origin"] = origin
    if saved_run_item:
        # A stored run reference must not bypass origin admission. Use an
        # existing item to make a wrong-reference write observable in the fake.
        record["run_qid"] = next(iter(run.wiki.entities))
    directory = run.context("places").directory
    scratchwrite.save_record(directory, record)
    before = copy.deepcopy(run.wiki.entities)
    pages = dict(run.wiki._recovery_pages)
    contents = recorded_contents(run.wiki)
    monkeypatch.setattr(
        scratchwrite,
        "place_client",
        lambda *_args: pytest.fail("saved plans must not call a model"),
    )
    with pytest.raises(ValueError):
        run.stage("places")
    assert run.wiki.entities == before
    assert run.wiki._recovery_pages == pages
    assert recorded_contents(run.wiki) == contents
    assert run.record("places") == record
