"""Opt-in local office rendering; no graph, private corpus or provider."""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from PIL import Image

from tests.fixtures import slides
from yizkr.claims import ArtifactRef
from yizkr.pipeline import slide_renderer
from yizkr.pipeline.container_inventory import read_inventory
from yizkr.pipeline.slide_renders import prepare_slide_renders


def test_real_slide_order_hidden_slides_notes_and_reuse(tmp_path, monkeypatch):
    executable = os.environ.get("YIZKR_TEST_LIBREOFFICE")
    if executable is None:
        pytest.skip("set YIZKR_TEST_LIBREOFFICE to a local office executable")
    settings = slide_renderer.SlideRenderSettings(
        executable=executable, long_edge=320
    )
    source = tmp_path / "fictional.pptx"
    source.write_bytes(slides.presentation())

    def prepare(directory, *, excluded=lambda *_: False):
        return prepare_slide_renders(
            source, directory, excluded=excluded, settings=settings
        )

    def pages(product, directory):
        return [
            (directory / s.representation.file).read_bytes()
            for s in product.slides
        ]

    directory = tmp_path / "first"
    first = prepare(directory)
    images = pages(first, directory)
    assert [s.admission.position for s in first.slides] == [1, 2, 3]
    assert [(s.width, s.height) for s in first.slides] == [(320, 240)] * 3
    for png, color in zip(images, slides.COLORS, strict=True):
        with Image.open(io.BytesIO(png)) as image:
            assert image.getpixel((5, 5)) == tuple(bytes.fromhex(color))
            # The colored geometry and actual text both reach the raster.
            assert any(low < high for low, high in image.getextrema())
    inventory = read_inventory(directory)
    first_slide = first.slides[0]
    assert len(first_slide.admission.image_part_ids) == 1
    assert len(first_slide.admission.placement_ids) == 1
    image_part = next(
        p
        for p in inventory.parts
        if p.id == first_slide.admission.image_part_ids[0]
    )
    with zipfile.ZipFile(source) as package:
        original = ArtifactRef.from_bytes(
            package.read("ppt/media/background.png")
        )
    assert image_part.source_role == "independent"
    assert image_part.owner_binding.artifact == original
    assert original != first_slide.representation.byte_object
    assert [p.text for p in inventory.parts if p.kind == "speaker_notes"] == [
        slides.NOTES
    ]
    with monkeypatch.context() as patch:

        def repeated(*args, **kwargs):
            pytest.fail("a valid saved representation was rendered again")

        patch.setattr(slide_renderer, "render_slides", repeated)
        assert prepare(directory) == first
    repeated_directory = tmp_path / "repeated"
    assert pages(prepare(repeated_directory), repeated_directory) == images
    source.write_bytes(slides.presentation(notes_text="CHANGED NOTES ONLY"))
    changed_directory = tmp_path / "changed-notes"
    assert pages(prepare(changed_directory), changed_directory) == images

    # LibreOffice treats noFill as inheritance; it must not bypass an
    # excluded master picture. An opaque fill really suppresses that image.
    def excluded(sha1, names):
        return sha1 == original.sha1

    source.write_bytes(slides.presentation(background_fill="<a:noFill/>"))
    with monkeypatch.context() as patch:

        def unadmitted(*args, **kwargs):
            pytest.fail("unproved background reached the renderer")

        patch.setattr(slide_renderer, "renderer_identity", unadmitted)
        patch.setattr(slide_renderer, "render_slides", unadmitted)
        with pytest.raises(ValueError):
            prepare(tmp_path / "no-fill", excluded=excluded)
    source.write_bytes(
        slides.presentation(
            background_fill=(
                '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
            )
        )
    )
    opaque_directory = tmp_path / "opaque"
    opaque = prepare(opaque_directory, excluded=excluded)
    assert opaque.slides[0].admission.image_part_ids == []
    with Image.open(io.BytesIO(pages(opaque, opaque_directory)[0])) as image:
        assert image.getpixel((5, 5)) == (255, 255, 255)
