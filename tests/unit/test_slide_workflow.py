"""Selected slides use durable stages, real caches and read-only estimates.

Only renderer pixels, model answers and host preflight are fictional. The
driver publishes and revalidates its normal document and corpus products.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import slides
from tests.fixtures.document_processing import (
    contents,
    prepare_document,
    snapshot,
)
from tests.fixtures.records import record_answer
from tests.unit import test_scratch_estimate as estimate_tests
from tests.unit import test_slide_renders as render_tests
from tests.unit.test_scratch import Corpus, Driver, fakes  # noqa: F401
from yizkr.claims import ArtifactRef
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.container_inventory import read_inventory
from yizkr.pipeline.rundir import RunSettingsConflict
from yizkr.pipeline.scratch import ScratchRefused
from yizkr.pipeline.slide_reading import SlideVisionSelection
from yizkr.pipeline.transcribe import Transcription

engine = estimate_tests.engine
backend = render_tests.backend


@pytest.fixture
def setup(tmp_path, engine, backend):
    corpus = Corpus(tmp_path / "corpus")
    source = corpus.folder / "fictional.pptx"
    source.write_bytes(slides.native_presentation())
    corpus.texts = defaultdict(lambda: u.SOURCES[0].text)
    corpus.texts[source.name] = "Left panel\nRight panel\nFictional slide 2"
    engine.corpus = corpus
    engine.fresh = False
    state = tmp_path / "runs" / "selected"
    options = {
        "cache_dir": tmp_path / "cache",
        "until": "clustering",
        "slide_vision": {
            ArtifactRef.from_path(source).sha1: SlideVisionSelection((1,))
        },
    }

    def driver(**changes):
        return Driver(corpus.folder, state, **(options | changes))

    def estimator(**changes):
        return estimate_tests.Quiet(
            corpus.folder, state, **(options | changes)
        )

    return source, driver, estimator


def load(doc):
    return Transcription.model_validate_json(doc.transcription.read_text())


def test_selected_workflow_reuses_reading_extraction_and_cluster_products(
    setup, engine, backend
):
    _, driver, estimator = setup
    first = driver()
    assert first.run() == 0
    (doc,) = first.documents()
    reading = load(doc)
    assert [page.provenance.method for page in reading.pages] == [
        "vision",
        "slide-xml",
        "slide-xml",
    ]
    assert reading.pages[0].body_end is not None
    assert doc.batch.exists() and doc.critique.exists()
    assert (first.state_dir / "corpus/clustering/identity-map.json").is_file()
    selection = first.rundir.manifest()["config"]["slide_vision"][doc.sha1]
    assert selection["positions"] == [1] and "directory" not in selection
    inventory = read_inventory(doc.dir)
    first_body = next(
        part
        for part in inventory.parts
        if part.kind == "slide_body" and part.locator.position == 1
    )
    assert first_body.reading.state == "read"
    assert first_body.reading.product == "transcription.json"
    assert first_body.representations
    held = (doc.batch.read_bytes(), doc.critique.read_bytes())
    calls = list(engine.calls)
    count = estimator().estimate()
    assert count.stages["transcription"].reused == 1
    assert count.stages["extraction"].reused == 1
    assert count.stages["critique"].reused == 1
    assert engine.calls == calls
    assert driver().run() == 0
    assert engine.calls == calls
    assert len(backend) == 1
    assert (doc.batch.read_bytes(), doc.critique.read_bytes()) == held
    assert read_inventory(doc.dir).parts == inventory.parts


@pytest.mark.parametrize("damage", ["png", "record", "page-proof"])
def test_estimate_and_execution_repair_without_new_model_answers(
    setup, engine, backend, damage
):
    _, driver, estimator = setup
    first = driver()
    assert first.run() == 0
    (doc,) = first.documents()
    workspace = doc.dir / "slide-render-workspace"
    if damage == "png":
        next((workspace / "slide-renders").glob("*.png")).write_bytes(
            b"broken"
        )
    elif damage == "record":
        (workspace / "slide-renders.json").unlink()
    else:
        reading = load(doc)
        reading.pages[0].provenance.render_product_sha256 = "0" * 64
        doc.transcription.write_text(reading.model_dump_json(indent=2))
    held = snapshot(doc.dir, skip=())
    cache_before = contents(first.cache_dir)
    calls = list(engine.calls)
    count = estimator().estimate()
    assert count.stages["transcription"].hits == 1
    assert snapshot(doc.dir, skip=()) == held
    assert contents(first.cache_dir) == cache_before
    assert engine.calls == calls
    batch = doc.batch.read_bytes()
    critique = doc.critique.read_bytes()
    assert driver().run() == 0
    assert engine.calls == calls
    assert doc.batch.read_bytes() == batch
    assert doc.critique.read_bytes() == critique
    check = driver()
    (current,) = check.documents()
    assert check._transcription_failure(current, load(current)) is None


def test_changed_selection_is_a_fixed_run_setting(setup):
    _, driver, estimator = setup
    assert driver().run() == 0
    with pytest.raises(RunSettingsConflict):
        driver(slide_vision={}).run()
    with pytest.raises(RunSettingsConflict):
        estimator(slide_vision={}).estimate()


@pytest.mark.parametrize("kind", ["unknown", "excluded", "text"])
def test_unusable_selected_source_refuses_before_model_or_render(
    setup, engine, backend, kind
):
    source, driver, _ = setup
    selection = SlideVisionSelection((1,))
    options = {}
    if kind == "unknown":
        options["slide_vision"] = {"0" * 40: selection}
    elif kind == "excluded":
        options["exclusions"] = ExclusionList(
            sha1s={ArtifactRef.from_path(source).sha1}
        )
    elif kind == "text":
        source = source.with_suffix(".txt")
        source.write_text(u.SOURCES[0].text)
        options["slide_vision"] = {
            ArtifactRef.from_path(source).sha1: selection
        }
    with pytest.raises(ScratchRefused):
        driver(**options).run()
    assert engine.calls == [] and backend == []


def test_correction_takes_precedence_without_changing_fixed_selection(
    setup, engine, backend
):
    _, driver, estimator = setup
    first = driver(until="transcription")
    assert first.run() == 0
    (doc,) = first.documents()
    doc.correction.write_text(u.SOURCES[0].text)
    before = snapshot(doc.dir, skip=())
    calls = list(engine.calls)
    messages = []
    probe = estimator(until="transcription", log=messages.append)
    count = probe.estimate()
    assert count.stages["transcription"].engine_calls == 0
    assert snapshot(doc.dir, skip=()) == before
    assert driver(until="transcription", log=messages.append).run() == 0
    assert load(doc).text == u.SOURCES[0].text
    assert first.rundir.manifest()["config"]["slide_vision"][doc.sha1]
    assert any(
        "curator correction takes precedence" in line for line in messages
    )
    assert engine.calls == calls and len(backend) == 1


def test_route_changes_preserve_original_image_owners_and_readings(
    setup, engine, backend
):
    source, driver, _ = setup
    source.write_bytes(slides.presentation())
    first = driver(slide_vision={}, until="critique")
    assert first.run() == 0
    (doc,) = first.documents()
    before = read_inventory(doc.dir)
    owner = next(
        part.owner_binding for part in before.parts if part.owner_binding
    )
    image_reading = (
        doc.dir / "derived-media" / f"{owner.artifact.sha1}.transcription.json"
    )
    original = image_reading.read_bytes()
    selected = driver(
        slide_vision={doc.sha1: SlideVisionSelection((2,))}, until="critique"
    )
    (changed,) = selected.documents()
    # A new run may seed existing document products; exercise the stage
    # freshness decision independently of the fixed manifest guard.
    assert prepare_document(selected, changed, until="critique") != "failed"
    assert load(changed).pages[1].provenance.method == "vision"
    assert image_reading.read_bytes() == original
    current = read_inventory(changed.dir)
    assert [
        part.owner_binding for part in current.parts if part.owner_binding
    ] == [owner]
    native = driver(slide_vision={}, until="critique")
    (changed,) = native.documents()
    assert prepare_document(native, changed, until="critique") != "failed"
    assert all(
        page.provenance.method == "slide-xml" for page in load(changed).pages
    )
    assert image_reading.read_bytes() == original
    assert len(backend) == 1


@pytest.mark.parametrize("target", ["container-inventory.json", "png"])
def test_estimator_refuses_unsafe_workspace_without_mutating_it(setup, target):
    _, driver, estimator = setup
    first = driver()
    assert first.run() == 0
    (doc,) = first.documents()
    workspace = doc.dir / "slide-render-workspace"
    path = (
        next((workspace / "slide-renders").glob("*.png"))
        if target == "png"
        else workspace / target
    )
    outside = doc.dir / "saved-original"
    path.rename(outside)
    path.symlink_to(outside)
    before = snapshot(doc.dir, skip=())
    probe = estimator()
    (current,) = probe.documents()
    with pytest.raises(ValueError, match="symlink"):
        probe._transcribe(current, "paged")
    assert snapshot(doc.dir, skip=()) == before
    assert path.is_symlink()


def test_obsolete_workspace_and_correction_do_not_affect_fresh_estimate(
    setup, engine
):
    _, driver, estimator = setup
    first = driver()
    (doc,) = first.documents()
    workspace = doc.dir / "slide-render-workspace"
    workspace.mkdir(parents=True)
    (workspace / "container-inventory.json").write_text("broken")
    doc.correction.write_text("obsolete correction")
    held = snapshot(doc.dir, skip=())
    probe = estimator()
    count = probe.estimate()
    assert count.stages["transcription"].failing == 0
    assert count.stages["transcription"].known == 1
    assert engine.calls == []
    assert snapshot(doc.dir, skip=()) == held
    assert driver().run() == 0
    assert load(doc).pages[0].provenance.method == "vision"
    assert not doc.correction.exists()


def test_persisted_selected_reading_is_independent_of_response_cache(
    setup, engine
):
    _, driver, estimator = setup
    first = driver()
    assert first.run() == 0
    shutil.rmtree(first.cache_dir)
    calls = list(engine.calls)
    count = estimator().estimate()
    assert count.stages["transcription"].reused == 1
    assert driver().run() == 0
    assert engine.calls == calls


def test_blank_selected_body_is_recorded_empty_despite_native_notes(
    setup, monkeypatch
):
    _, driver, _ = setup
    complete = estimate_tests.Engine.complete

    def blank(self, prompt, image_paths=()):
        if image_paths:
            return record_answer("")
        return complete(self, prompt, image_paths)

    monkeypatch.setattr(estimate_tests.Engine, "complete", blank)
    first = driver(until="transcription")
    assert first.run() == 0
    (doc,) = first.documents()
    assert slides.NOTES in load(doc).text
    body = next(
        part
        for part in read_inventory(doc.dir).parts
        if part.kind == "slide_body" and part.locator.position == 1
    )
    assert body.reading.empty is True


def test_estimator_result_owns_reading_files_until_close(setup, monkeypatch):
    from tests.unit.test_media_stages import AuthoredReader

    _, _, estimator = setup
    probe = estimator()
    (doc,) = probe.documents()
    monkeypatch.setattr(probe, "_llm", lambda *args: AuthoredReader())
    result = probe._transcribe(doc, "paged")
    folder = Path(result.products.folder.name)
    assert list(folder.rglob("*.png"))
    assert not (doc.dir / "slide-render-workspace").exists()
    result.close()
    assert not folder.exists()


@pytest.mark.parametrize("failure", ["renderer", "admission"])
def test_selected_estimate_reports_known_inspection_refusal(
    setup, engine, backend, monkeypatch, capsys, failure
):
    from yizkr import cli
    from yizkr.pipeline import estimate, slide_renderer

    source, _, estimator = setup
    options = {}
    if failure == "renderer":

        def unavailable(_settings):
            raise slide_renderer.SlideRendererUnavailable(
                "LibreOffice executable is unavailable"
            )

        monkeypatch.setattr(slide_renderer, "renderer_identity", unavailable)
    else:
        source.write_bytes(slides.presentation())
        options["slide_vision"] = {
            ArtifactRef.from_path(source).sha1: SlideVisionSelection((1,))
        }
    with pytest.raises(
        ScratchRefused, match="cannot inspect selected slide inputs"
    ):
        estimator(**options).estimate()
    monkeypatch.setattr(cli, "_scratch_plan", lambda args: (source.parent, {}))
    monkeypatch.setattr(
        estimate,
        "scratch_estimate",
        lambda folder, **kwargs: estimator(**options).estimate(),
    )
    assert cli.cmd_scratch_estimate(None) == 1
    assert "cannot inspect selected slide inputs" in capsys.readouterr().err
    assert engine.calls == [] and backend == []
