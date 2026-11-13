"""Automatic plans remain current at independent-image model boundaries."""

import pytest

from tests.fixtures import slides
from yizkr.claims import ArtifactRef
from yizkr.pipeline.media_stages import MediaReadRequest, read_media
from yizkr.pipeline.slide_reading import SlideVisionRequest


@pytest.mark.parametrize("target", ["presentation", "image"])
def test_native_image_calls_recheck_layout_admission_after_client_creation(
    tmp_path, target
):
    source = tmp_path / "fictional.pptx"
    source.write_bytes(slides.presentation())
    excluded = set()
    digest = ArtifactRef.from_path(source).sha1

    class Reader:
        model_identifier = "fictional-model"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("excluded input reached the provider")

    def client():
        if target == "presentation":
            excluded.add(digest)
        else:
            # The fixture has one original PNG, independently read while its
            # native slide keeps an explicit mixed-source coverage gap.
            from yizkr.pipeline.presentation import unpack_pptx

            content = unpack_pptx(source)
            excluded.update(
                part.byte_object.sha1
                for part in content.inventory.parts
                if part.kind == "embedded_resource"
            )
        return Reader()

    with pytest.raises(ValueError, match="excluded|contributing image"):
        read_media(
            MediaReadRequest(
                source,
                "paged",
                excluded=lambda sha1, names: sha1 in excluded,
                slide_vision=SlideVisionRequest(
                    (), tmp_path / "renders", mode="layout"
                ),
            ),
            client,
        )
    assert not (tmp_path / "renders").exists()


@pytest.mark.parametrize("damage", ["exclusion", "bytes"])
def test_unplaced_original_image_is_checked_at_actual_dispatch(
    tmp_path, monkeypatch, damage
):
    from yizkr.pipeline.transcribe import DeckTranscriber

    source = tmp_path / "fictional.pptx"
    source.write_bytes(
        slides.presentation(
            background_fill=(
                '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
            )
        )
    )
    excluded = set()
    inputs = []
    clients = []
    original = DeckTranscriber._before_vision

    def remember(self, path):
        inputs.append(path)
        return original(self, path)

    monkeypatch.setattr(DeckTranscriber, "_before_vision", remember)

    class Reader:
        model_identifier = "fictional-model"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("changed image reached the provider")

    def client():
        clients.append(True)
        path = inputs[-1]
        if damage == "exclusion":
            excluded.add(ArtifactRef.from_path(path).sha1)
        else:
            path.write_bytes(b"damaged independent image")
        return Reader()

    with pytest.raises(
        ValueError, match="excluded or changed|missing or damaged"
    ):
        read_media(
            MediaReadRequest(
                source,
                "paged",
                excluded=lambda sha1, names: sha1 in excluded,
                slide_vision=SlideVisionRequest(
                    (), tmp_path / "renders", mode="layout"
                ),
            ),
            client,
        )
    assert clients == [True]
    assert not (tmp_path / "renders").exists()
