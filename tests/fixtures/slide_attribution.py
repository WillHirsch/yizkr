"""Authored answers through real slide reading and claim attribution.

Only renderer pixels and model responses are substituted. Production code
admits the package, binds owners, stores render representations, reads native
parts, checks model answers, and assigns claim support and process lineage.
This fixture establishes no renderer or model-quality evidence.
"""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image
from tests.fixtures import slides
from tests.fixtures.records import record_answer

from yizkr.claims import ArtifactRef, ClaimBatch, QidRef
from yizkr.pipeline import slide_renders, text_stages
from yizkr.pipeline.container_inventory import keep_inventory
from yizkr.pipeline.container_sources import (
    ContainerSources,
    project_sources,
    source_context,
)
from yizkr.pipeline.media_stages import (
    MediaReadRequest,
    PreparationRequest,
    PreparedContainer,
    prepare_container,
    read_media,
)
from yizkr.pipeline.slide_reading import SlideVisionRequest
from yizkr.pipeline.slide_renderer import RenderedSlide, SlideRenderResult
from yizkr.pipeline.transcribe import Transcription

CAPTION = "Wastl to Klara, July 1939."
SELECTED = "Sebastian wrote to Klara."
NATIVE = "The letter is dated 28 July 1939."
NOTES = "Rahel's address is mentioned in the letter."
IMAGE_TEXT = "Rahel wohnt jetzt Angerring 11"
PARENT_QUOTES = (CAPTION, SELECTED, NATIVE, NOTES)


def presentation() -> bytes:
    """Three physical slides with distinct body and speaker-note evidence."""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(
            io.BytesIO(
                slides.mixed_layout_presentation(
                    placement="repeated", notes_text=NOTES
                )
            )
        ) as original,
        zipfile.ZipFile(output, "w") as result,
    ):
        changes = {
            "ppt/slides/slide3.xml": ("Stupfield arrivals", CAPTION),
            "ppt/slides/slide1.xml": ("Fictional slide 2", SELECTED),
            "ppt/slides/slide2.xml": ("Fictional slide 3", NATIVE),
        }
        for name in original.namelist():
            data = original.read(name)
            if name in changes:
                before, after = changes[name]
                assert data.count(before.encode()) == 1
                data = data.replace(before.encode(), after.encode())
            result.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data)
    return output.getvalue()


def allow(_sha1, _names=()):
    return False


def _backend(monkeypatch, calls):
    def identity(settings):
        return {
            "renderer": "authored-slide-attribution-fixture",
            "long_edge": settings.long_edge,
        }

    def render(original, *, expected_slides, settings):
        calls.append(ArtifactRef.from_bytes(original))
        rendered = []
        for position in range(1, expected_slides + 1):
            output = io.BytesIO()
            Image.new("RGB", (20, 10), (position, 0, 0)).save(output, "PNG")
            rendered.append(RenderedSlide(position, output.getvalue(), 20, 10))
        return SlideRenderResult(tuple(rendered), identity(settings))

    monkeypatch.setattr(
        slide_renders.slide_renderer, "renderer_identity", identity
    )
    monkeypatch.setattr(slide_renders.slide_renderer, "render_slides", render)


class Reader:
    model_identifier = "authored-slide-attribution-reading"

    def __init__(self, owner, calls):
        self.owner, self.calls = owner, calls

    def complete(self, prompt, image_paths=()):
        assert len(image_paths) == 1
        path = Path(image_paths[0])
        if ArtifactRef.from_path(path) == self.owner:
            self.calls.append("image")
            return record_answer(IMAGE_TEXT, languages=("de",))
        if prompt.startswith("Read the arrangement of the supplied native"):
            self.calls.append("mixed-layout")
            assert CAPTION in prompt and IMAGE_TEXT not in prompt
            return json.dumps({"rows": [["b1"], ["b3", "b5"], ["b4", "b2"]]})
        with Image.open(path) as pixels:
            assert pixels.getpixel((0, 0)) == (2, 0, 0)
        self.calls.append("selected-vision")
        return record_answer(SELECTED)


class Extractor:
    model_identifier = "authored-slide-attribution-extraction"

    def __init__(self, claims):
        self.claims = claims

    def complete(self, prompt, image_paths=()):
        assert not image_paths
        for claim in self.claims:
            assert claim["source"]["transcription_excerpt"]["text"] in prompt
        return "\n".join(
            json.dumps(value)
            for value in [
                *self.claims,
                {"done": True, "claims_emitted": len(self.claims)},
            ]
        )


def _quote(text, page, language="en"):
    # The extractor replaces model-proposed ownership with verified support.
    return {
        "artifact": ArtifactRef.from_bytes(
            b"untrusted model owner"
        ).model_dump(),
        "page_number": str(page),
        "transcription_excerpt": {"text": text, "language": language},
    }


def captions(owner):
    return [
        {
            "subject": owner.model_dump(),
            "property": "caption",
            "value": {
                "type": "monolingual",
                "value": {"text": text, "language": "en"},
            },
            "source": _quote(text, page),
        }
        for text, page in zip(PARENT_QUOTES, (1, 2, 3, 1), strict=True)
    ]


def residence(person: QidRef, address: QidRef):
    return {
        "subject": person.model_dump(),
        "property": "residence",
        "value": {
            "type": "item",
            "value": address.model_dump(),
        },
        "source": _quote(IMAGE_TEXT, 1, "de"),
    }


def extract(transcription, claims):
    return text_stages.extract(
        text_stages.ExtractionRequest(
            transcription, transcription.artifact, strategy="single"
        ),
        Extractor(claims),
    ).require_complete()


@dataclass
class SlideCase:
    source: Path
    directory: Path
    prepared: PreparedContainer
    sources: ContainerSources
    request: MediaReadRequest
    native: Transcription
    transcription: Transcription
    image_transcription: Transcription
    image_owner: ArtifactRef
    parent_batch: ClaimBatch
    image_batch: ClaimBatch
    render_calls: list
    model_calls: list[str]

    def refuse_excluded(self):
        request = replace(
            self.request,
            excluded=lambda sha1, names: sha1 == self.image_owner.sha1,
        )
        return read_media(
            request, lambda: Reader(self.image_owner, self.model_calls)
        )

    def refuse_ambiguous(self):
        reading = self.transcription.model_copy(deep=True)
        reading.pages[0].provenance.native_layout = None
        reading.source_context = source_context(reading, self.sources)
        return extract(reading, captions(self.image_owner)[:1])


@contextmanager
def prepare_case(directory, monkeypatch, *, person: QidRef, address: QidRef):
    """Keep real media/claim products alive for the graph or offline check."""
    image_claim = residence(person, address)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "library-talk-attribution.pptx"
    source.write_bytes(presentation())
    products = directory / "products"
    prepared = prepare_container(
        PreparationRequest(source, existing_directory=products, excluded=allow)
    )
    assert prepared is not None
    projected = project_sources(
        prepared.inventory, products, data=prepared.data
    )
    (owner,) = projected.sources.values()
    renders, model_calls = [], []
    with monkeypatch.context() as patches:
        _backend(patches, renders)

        def client():
            return Reader(owner.owner, model_calls)

        request = MediaReadRequest(
            source, "paged", language="en", prepared=prepared, excluded=allow
        )
        with read_media(request, client) as default:
            native = default.transcription
            assert [p.provenance.method for p in native.pages] == [
                "slide-xml"
            ] * 3
            assert model_calls == ["image"]
        request = replace(
            request,
            slide_vision=SlideVisionRequest(
                (1, 2), directory / "slide-renders"
            ),
        )
        with read_media(request, client) as read:
            assert len(read.products.images) == 1
            image = read.products.images[0].transcription
            assert image is not None
            keep_inventory(prepared.inventory, prepared.data, products)
            sources = project_sources(prepared.inventory, products)
            parent = extract(read.transcription, captions(owner.owner))
            image_batch = extract(image, [image_claim])
            yield SlideCase(
                source,
                products,
                prepared,
                sources,
                request,
                native,
                read.transcription,
                image,
                owner.owner,
                parent,
                image_batch,
                renders,
                model_calls,
            )
