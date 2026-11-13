"""Explicit slide vision preserves owners and admission boundaries."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from tests.fixtures import slides
from tests.unit import test_slide_renders as render_tests
from tests.unit.test_media_stages import AuthoredReader, allowed, no_client
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    MediaOwnerBinding,
    read_inventory,
)
from yizkr.pipeline.media_stages import (
    MediaReadRequest,
    PreparationRequest,
    prepare_container,
    read_media,
)
from yizkr.pipeline.slide_reading import SlideVisionRequest
from yizkr.pipeline.transcribe import Transcription

backend = render_tests.backend


def request(tmp_path, positions=(2,), *, opaque=False, unsupported=False):
    source = tmp_path / "fictional.pptx"
    source.write_bytes(
        slides.presentation(
            background_fill=(
                '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
                if opaque
                else None
            )
        )
    )
    if unsupported:
        output = io.BytesIO()
        with (
            zipfile.ZipFile(source) as original,
            zipfile.ZipFile(output, "w") as changed,
        ):
            for info in original.infolist():
                data = original.read(info.filename)
                if info.filename == "ppt/slides/slide3.xml":
                    data = data.replace(b"<a:xfrm>", b'<a:xfrm rot="60000">')
                changed.writestr(
                    zipfile.ZipInfo(info.filename, (2020, 1, 1, 0, 0, 0)), data
                )
        source.write_bytes(output.getvalue())
    return MediaReadRequest(
        source,
        "paged",
        language="en",
        excluded=allowed,
        slide_vision=SlideVisionRequest(positions, tmp_path / "renders"),
    )


def test_selected_page_uses_render_and_original_images_keep_their_owners(
    tmp_path, backend
):
    selected = request(tmp_path)
    client = AuthoredReader()
    with read_media(selected, lambda: client) as result:
        reading = result.transcription
        assert [p.provenance.method for p in reading.pages] == [
            "slide-xml",
            "vision",
            "slide-xml",
        ]
        native, vision, _ = reading.pages
        inventory = read_inventory(selected.slide_vision.directory)
        image = next(p for p in inventory.parts if p.owner_binding)
        assert native.images == [image.owner_binding.artifact.sha1]
        assert len(result.products.images) == 1
        assert (
            result.products.images[0].sha1 == image.owner_binding.artifact.sha1
        )
        actual = client.inputs[0][1][0]
        assert vision.provenance.reading_input == ArtifactRef.from_bytes(
            actual
        )
        assert vision.provenance.reading_input != image.owner_binding.artifact
        assert (
            len(client.inputs) == 2
        )  # one selected slide, one original image
        assert all(
            not payload.startswith(b"PK")
            for _, inputs in client.inputs
            for payload in inputs
        )
        assert result.dependencies["slide_vision"]["positions"] == [2]
        assert (
            vision.provenance.render_product_sha256
            == result.dependencies["slide_vision"]["render_product_sha256"]
        )
        configuration = reading.provenance.stages[-1].configuration
        assert configuration["slide_vision"]["positions"] == [2]
        assert "directory" not in configuration["slide_vision"]
        restored = Transcription.model_validate_json(reading.model_dump_json())
        assert restored.pages[1].provenance == vision.provenance
        assert all(
            span.state == "attributed"
            for span in reading.source_context.readings
        )
    assert len(backend) == 1


def test_selected_notes_stay_native_and_outside_vision_record(
    tmp_path, backend
):
    selected = request(tmp_path, positions=(1,), opaque=True)
    client = AuthoredReader()
    with read_media(selected, lambda: client) as result:
        reading = result.transcription
        page = reading.pages[0]
        assert slides.NOTES in reading.text
        assert slides.NOTES not in reading.page_record(page).pages[0].text
        notes = next(
            span
            for span in reading.source_context.readings
            if span.support.locator.kind == "speaker_notes"
        )
        assert reading.text[notes.start : notes.end] == slides.NOTES
        assert notes.owner == reading.artifact


def test_native_default_never_starts_renderer(tmp_path, backend):
    native = replace(request(tmp_path), slide_vision=None)
    client = AuthoredReader()
    with read_media(native, lambda: client) as result:
        assert all(
            p.provenance.method == "slide-xml"
            for p in result.transcription.pages
        )
        assert "slide_vision" not in result.dependencies["configuration"]
        assert (
            "reading_input"
            not in result.transcription.pages[0].provenance.model_dump()
        )
    assert backend == []
    assert not (tmp_path / "renders").exists()


@pytest.mark.parametrize("positions", [(), (0,), (-1,), (True,), (1, 1), [1]])
def test_invalid_selection_is_not_a_silent_native_request(tmp_path, positions):
    with pytest.raises(ValueError, match="distinct positive"):
        SlideVisionRequest(positions, tmp_path / "renders")


@pytest.mark.parametrize(
    "failure",
    [
        "missing-policy",
        "whole-vision",
        "pdf",
        "out-of-range",
        "unsupported-mixed",
    ],
)
def test_invalid_or_unsupported_selection_refuses_before_render_or_client(
    tmp_path, backend, failure
):
    selected = request(tmp_path, unsupported=failure == "unsupported-mixed")
    if failure == "missing-policy":
        selected = replace(selected, excluded=None)
    elif failure == "whole-vision":
        selected = replace(selected, route="vision")
    elif failure == "pdf":
        other = selected.source.with_suffix(".pdf")
        other.write_bytes(selected.source.read_bytes())
        selected = replace(selected, source=other)
    else:
        selected = replace(
            selected,
            slide_vision=replace(
                selected.slide_vision,
                positions=(4,) if failure == "out-of-range" else (1,),
            ),
        )
    with pytest.raises(ValueError):
        read_media(selected, no_client)
    assert backend == []


def test_render_reuse_keeps_selection_in_reading_configuration(
    tmp_path, backend
):
    selected = request(tmp_path)
    with read_media(selected, AuthoredReader) as result:
        first = result.dependencies["slide_vision"]
    with read_media(
        replace(
            selected,
            slide_vision=replace(selected.slide_vision, positions=(3,)),
        ),
        AuthoredReader,
    ) as result:
        second = result.dependencies["slide_vision"]
        assert result.transcription.pages[2].provenance.method == "vision"
    assert first["render_product_sha256"] == second["render_product_sha256"]
    assert first["positions"] == [2] and second["positions"] == [3]
    assert len(backend) == 1


@pytest.mark.parametrize("change", ["source", "input", "exclusion"])
def test_changes_during_provider_call_refuse_the_completed_reading(
    tmp_path, backend, change
):
    selected = request(tmp_path)
    blocked = set()
    selected = replace(selected, excluded=lambda sha, names: sha in blocked)
    inputs = []

    class ChangingReader(AuthoredReader):
        def complete(self, prompt, image_paths=()):
            answer = super().complete(prompt, image_paths)
            if not inputs:
                path = Path(image_paths[0])
                inputs.append(path)
                if change == "source":
                    selected.source.write_bytes(b"changed")
                elif change == "input":
                    path.write_bytes(b"changed")
                else:
                    blocked.add(hashlib.sha1(path.read_bytes()).hexdigest())
            return answer

    with pytest.raises(ValueError, match="changed|damaged|excluded"):
        read_media(selected, ChangingReader)
    assert inputs and not inputs[0].exists()


def test_newly_excluded_next_slide_never_reaches_provider(tmp_path, backend):
    selected = request(tmp_path, positions=(2, 3))
    blocked = set()
    selected = replace(selected, excluded=lambda sha, names: sha in blocked)

    class ChangingReader(AuthoredReader):
        def complete(self, prompt, image_paths=()):
            answer = super().complete(prompt, image_paths)
            inventory = read_inventory(selected.slide_vision.directory)
            next_slide = next(
                part
                for part in inventory.parts
                if part.kind == "slide_body" and part.locator.position == 3
            )
            blocked.add(next_slide.representations[0].byte_object.sha1)
            return answer

    client = ChangingReader()
    with pytest.raises(ValueError, match="excluded"):
        read_media(selected, lambda: client)
    assert len(client.inputs) == 1


def test_rejected_cached_answer_rechecks_policy_before_retry(
    tmp_path, backend
):
    selected = request(tmp_path)
    blocked = set()
    selected = replace(selected, excluded=lambda sha, names: sha in blocked)

    class CachedReader(AuthoredReader):
        def reject(self, answer, reason):
            blocked.add(hashlib.sha1(self.inputs[0][1][0]).hexdigest())
            return True

    client = CachedReader("not a transcription record")
    with pytest.raises(ValueError, match="excluded"):
        read_media(selected, lambda: client)
    assert len(client.inputs) == 1


@pytest.mark.parametrize("change", ["exclusion", "input"])
def test_unplaced_original_is_rechecked_before_its_provider_call(
    tmp_path, backend, change
):
    selected = request(tmp_path, opaque=True)
    blocked = set()
    selected = replace(selected, excluded=lambda sha, names: sha in blocked)

    class ChangingReader(AuthoredReader):
        def complete(self, prompt, image_paths=()):
            answer = super().complete(prompt, image_paths)
            inventory = read_inventory(selected.slide_vision.directory)
            image = next(
                part for part in inventory.parts if part.owner_binding
            )
            assert not any(p.child == image.id for p in inventory.placements)
            owner = image.owner_binding.artifact.sha1
            if change == "exclusion":
                blocked.add(owner)
            else:
                directory = Path(image_paths[0]).parent.parent
                (directory / f"{owner}.png").write_bytes(b"changed")
            return answer

    client = ChangingReader()
    with pytest.raises(ValueError, match="excluded|damaged"):
        read_media(selected, lambda: client)
    assert len(client.inputs) == 1


def legacy_binding(prepared):
    image = next(p for p in prepared.inventory.parts if p.owner_binding)
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(output, "PNG")
    data = output.getvalue()
    owner = ArtifactRef.from_bytes(data)
    prepared.data[owner.sha1] = data
    image.owner_binding = MediaOwnerBinding(
        artifact=owner,
        file=f"representations/{owner.sha1}.png",
        media_type="image/png",
        basis="verified-legacy-reading",
    )
    return owner


def test_render_preparation_preserves_supplied_canonical_owner(
    tmp_path, backend
):
    selected = request(tmp_path)
    prepared = prepare_container(PreparationRequest(selected.source))
    # Bind once so the test supplies established ownership, then replace that
    # binding with a verified legacy owner distinct from current raw bytes.
    from yizkr.pipeline.container_sources import prepare_sources

    prepared.presentation.inventory = prepare_sources(
        prepared.inventory, prepared.data, tmp_path / "prior"
    )
    owner = legacy_binding(prepared)
    with read_media(
        replace(selected, prepared=prepared), AuthoredReader
    ) as result:
        assert result.products.images[0].sha1 == owner.sha1
        assert result.transcription.pages[0].images == [owner.sha1]
    saved = read_inventory(selected.slide_vision.directory)
    assert (
        next(p.owner_binding.artifact for p in saved.parts if p.owner_binding)
        == owner
    )

    # A caller cannot replace that workspace's established owner by freshly
    # inspecting the same original package without the prior binding.
    with pytest.raises(ValueError, match="different prepared source bindings"):
        read_media(selected, no_client)
