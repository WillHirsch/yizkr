"""Persisted visual readings require current admission without publication."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.unit import test_slide_renders as render_tests
from tests.unit.test_media_stages import AuthoredReader, allowed
from tests.unit.test_slide_reading import request
from yizkr.claims import ArtifactRef
from yizkr.pipeline.media_stages import (
    PreparationRequest,
    prepare_container,
    read_media,
)
from yizkr.pipeline.slide_reading import (
    SlideVisionSelection,
    restore_slide_render_representations,
    slide_vision_dependencies,
    slide_vision_reading_failure,
)
from yizkr.pipeline.slide_renders import inspect_slide_renders
from yizkr.pipeline.transcribe import Transcription

backend = render_tests.backend


def setup(tmp_path):
    selected = request(tmp_path, positions=(1,), opaque=True)
    with read_media(selected, AuthoredReader) as result:
        stored = Transcription.model_validate_json(
            result.transcription.model_dump_json()
        )
    prepared = prepare_container(
        PreparationRequest(
            selected.source, existing_directory=selected.slide_vision.directory
        )
    )
    return selected, stored, prepared.presentation


def snapshot(directory):
    return {
        path.relative_to(directory): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }


def inspect(selected, content):
    return inspect_slide_renders(
        selected.source,
        selected.slide_vision.directory,
        settings=selected.slide_vision.settings,
        prepared=content,
        excluded=selected.excluded,
    )


def failure(selected, stored, content):
    return slide_vision_reading_failure(
        selected.source,
        stored,
        content,
        selected.slide_vision,
        excluded=selected.excluded,
    )


def dependencies(selected, content):
    return slide_vision_dependencies(
        selected.source,
        content,
        selected.slide_vision,
        excluded=selected.excluded,
    )


def test_readonly_valid_reuse_and_in_memory_representation_restore(
    tmp_path, backend
):
    selected, stored, content = setup(tmp_path)
    before = snapshot(tmp_path)
    inventory = content.inventory.model_dump()
    assert failure(selected, stored, content) is None
    product = inspect(selected, content)
    assert product is not None
    assert content.inventory.model_dump() == inventory
    assert snapshot(tmp_path) == before
    assert len(backend) == 1
    assert all(
        not p.representations
        for p in content.inventory.parts
        if p.kind == "slide_body"
    )
    owners = {p.id: p.owner_binding for p in content.inventory.parts}
    restore_slide_render_representations(
        content, product, selected.slide_vision.directory, excluded=allowed
    )
    assert {p.id: p.owner_binding for p in content.inventory.parts} == owners
    assert all(
        p.representations
        for p in content.inventory.parts
        if p.kind == "slide_body"
    )
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("damage", ["png", "record", "missing-record"])
def test_output_damage_invalidates_reuse_without_changing_generation_inputs(
    tmp_path, backend, damage
):
    selected, stored, content = setup(tmp_path)
    expected = dependencies(selected, content)
    product = inspect(selected, content)
    directory = selected.slide_vision.directory
    if damage == "png":
        (directory / product.slides[0].representation.file).write_bytes(
            b"damaged"
        )
    elif damage == "record":
        (directory / "slide-renders.json").write_text("{}")
    else:
        (directory / "slide-renders.json").unlink()
    before = snapshot(tmp_path)
    assert dependencies(selected, content) == expected
    assert "product" in failure(selected, stored, content)
    assert inspect(selected, content) is None
    assert snapshot(tmp_path) == before
    assert len(backend) == 1


def test_absent_workspace_is_observed_without_creating_it(tmp_path, backend):
    selected, stored, content = setup(tmp_path)
    selected = replace(
        selected,
        slide_vision=replace(
            selected.slide_vision, directory=tmp_path / "absent"
        ),
    )
    assert dependencies(selected, content)["positions"] == [1]
    assert inspect(selected, content) is None
    assert "product" in failure(selected, stored, content)
    assert not selected.slide_vision.directory.exists()


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_lost_owner_inventory_refuses_even_with_saved_reading(
    tmp_path, backend, damage
):
    selected, stored, content = setup(tmp_path)
    path = selected.slide_vision.directory / "container-inventory.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{}")
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="prior source bindings"):
        failure(selected, stored, content)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "change",
    [
        "selected-method",
        "extra-vision",
        "png-id",
        "product-id",
        "boundary",
        "note-text",
        "page-count",
        "source",
        "empty-body",
    ],
)
def test_saved_reading_must_match_current_selection_inputs_and_note_boundaries(
    tmp_path, backend, change
):
    selected, stored, content = setup(tmp_path)
    page = stored.pages[0]
    if change == "selected-method":
        page.provenance.method = "slide-xml"
    elif change == "extra-vision":
        stored.pages[1].provenance.method = "vision"
    elif change == "png-id":
        page.provenance.reading_input = ArtifactRef.from_bytes(b"other PNG")
    elif change == "product-id":
        page.provenance.render_product_sha256 = "0" * 64
    elif change == "boundary":
        page.body_end = None
    elif change == "note-text":
        stored.text = stored.text.replace(
            "SPEAKER NOTES ONLY", "CHANGED NOTES ONLY"
        )
    elif change == "page-count":
        stored.pages.pop()
    elif change == "source":
        stored.artifact = ArtifactRef.from_bytes(b"other source")
    else:
        page.body_end = page.start
    assert failure(selected, stored, content) is not None
    assert len(backend) == 1


def test_new_exclusions_refuse_saved_visual_reading(tmp_path, backend):
    selected, stored, content = setup(tmp_path)
    blocked = stored.pages[0].provenance.reading_input.sha1
    selected = replace(selected, excluded=lambda sha, names: sha == blocked)
    with pytest.raises(ValueError, match="excluded"):
        failure(selected, stored, content)


def test_mislabelled_product_requires_regeneration(tmp_path, backend):
    selected, stored, content = setup(tmp_path)
    directory = selected.slide_vision.directory
    source = ArtifactRef.from_bytes(b"another fictional presentation")
    record_path = directory / render_tests.renders.RENDER_RECORD
    product = json.loads(record_path.read_text())
    product["source"] = source.model_dump()
    product.pop("digest")
    product["digest"] = render_tests.renders._hash(product)
    record_path.write_text(json.dumps(product))
    inventory_path = directory / "container-inventory.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["source"] = source.model_dump()
    inventory_path.write_text(json.dumps(inventory))
    before = snapshot(tmp_path)
    assert inspect(selected, content) is None
    assert "product" in failure(selected, stored, content)
    assert snapshot(tmp_path) == before
    repaired = render_tests.renders.prepare_slide_renders(
        selected.source, directory, excluded=allowed, prepared=content
    )
    assert repaired.source == stored.artifact
    assert len(backend) == 2


def test_dependency_inspection_rechecks_policy_after_renderer_identity(
    tmp_path, backend, monkeypatch
):
    selected, stored, content = setup(tmp_path)
    blocked = set()
    selected = replace(selected, excluded=lambda sha, names: sha in blocked)
    original_identity = render_tests.renders.slide_renderer.renderer_identity

    def identify(settings):
        blocked.add(stored.artifact.sha1)
        return original_identity(settings)

    monkeypatch.setattr(
        render_tests.renders.slide_renderer, "renderer_identity", identify
    )
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="excluded"):
        dependencies(selected, content)
    assert snapshot(tmp_path) == before
    assert len(backend) == 1


@pytest.mark.parametrize("positions", [(1,), (4,)])
def test_generation_dependency_inspection_refuses_invalid_selection(
    tmp_path, backend, positions
):
    selected = request(
        tmp_path, positions=positions, unsupported=positions == (1,)
    )
    prepared = prepare_container(PreparationRequest(selected.source))
    with pytest.raises(
        ValueError, match="unsupported native slide layout|outside"
    ):
        dependencies(selected, prepared.presentation)
    assert backend == []
    assert not selected.slide_vision.directory.exists()


def test_changed_render_environment_or_settings_requires_regeneration(
    tmp_path, backend, monkeypatch
):
    selected, stored, content = setup(tmp_path)
    expected = dependencies(selected, content)
    monkeypatch.setattr(
        render_tests.renders.slide_renderer,
        "renderer_identity",
        lambda settings: {
            "renderer": "changed fictional renderer",
            "long_edge": settings.long_edge,
        },
    )
    assert dependencies(selected, content) != expected
    assert "product" in failure(selected, stored, content)
    assert len(backend) == 1


def test_path_free_selection_preserves_existing_request_construction(tmp_path):
    selection = SlideVisionSelection((3, 1))
    request = selection.request(tmp_path / "renders")
    assert request.positions == (3, 1)
    assert selection.configuration() == request.configuration()
    assert selection.configuration()["positions"] == [1, 3]
    assert "directory" not in selection.configuration()
    with pytest.raises(ValueError, match="distinct positive"):
        SlideVisionSelection((1, 1))
