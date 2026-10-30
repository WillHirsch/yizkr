"""Real registration/writing preserves common source identities and locators.

The fictional library talk and two PDFs place the same image. Native and
annotation-driven vision routes share its artifact, repeated placements stay
located, and slide-body versus speaker-notes support remains distinct. No
model calls are made; all bytes and statements are fictional fixtures.
"""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import pytest

from tests.fixtures import pdfs
from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.container_inventory import keep_inventory
from yizkr.pipeline.container_sources import prepare_sources, project_sources
from yizkr.pipeline.pdf_inventory import inventory_pdf
from yizkr.pipeline.presentation import unpack_pptx
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.textlayer import read_pages
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

TITLE = "Library Talk 2018"
CAPTION = "Wastl to Klara, July 1939."
IMAGE = pdfs.JPEG
OWNER = ArtifactRef.from_bytes(IMAGE)
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def relationships(*entries):
    return (
        f'<Relationships xmlns="{RELS}">'
        + "".join(
            f'<Relationship Id="{rid}" Type="{R}/{kind}" '
            f'Target="{escape(target)}"/>'
            for rid, kind, target in entries
        )
        + "</Relationships>"
    )


def body(text):
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="caption"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody>"
        "<a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>"
        + escape(text)
        + "</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def picture(rid, number):
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{number}" name="album photograph"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>"
        f'<a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
        '</p:blipFill><p:spPr><a:xfrm><a:off x="100" y="200"/>'
        '<a:ext cx="300" cy="400"/></a:xfrm></p:spPr></p:pic>'
    )


def small_image():
    """A valid 1x1 grey PNG, independent of any visible placement."""

    def chunk(kind, data):
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )


def deck(path, *, unplaced=False):
    members = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="jpg" ContentType="image/jpeg"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="'
            "application/vnd.openxmlformats-officedocument.presentationml."
            'presentation.main+xml"/>'
            '<Override PartName="/ppt/slides/slide1.xml" ContentType="'
            "application/vnd.openxmlformats-officedocument.presentationml."
            'slide+xml"/>'
            '<Override PartName="/ppt/notesSlides/notesSlide1.xml" '
            'ContentType="'
            "application/vnd.openxmlformats-officedocument.presentationml."
            'notesSlide+xml"/></Types>'
        ),
        "_rels/.rels": relationships(
            ("presentation", "officeDocument", "ppt/presentation.xml")
        ),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            '<p:sldId id="256" r:id="slide"/>'
            "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": relationships(
            ("slide", "slide", "slides/slide1.xml")
        ),
        "ppt/slides/slide1.xml": (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            "<p:cSld><p:spTree>"
            + body(CAPTION)
            + picture("image", 3)
            + picture("image", 4)
            + "</p:spTree></p:cSld></p:sld>"
        ),
        "ppt/slides/_rels/slide1.xml.rels": relationships(
            ("image", "image", "../media/album.jpg"),
            ("notes", "notesSlide", "../notesSlides/notesSlide1.xml"),
        ),
        "ppt/notesSlides/notesSlide1.xml": (
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
            + body(CAPTION)
            + "</p:spTree></p:cSld></p:notes>"
        ),
        "ppt/media/album.jpg": IMAGE,
    }
    if unplaced:
        members["ppt/media/unused.png"] = small_image()
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), payload
            )
    return path


def statements(client, qid, property):
    properties, _ = client.vocabulary()
    result = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[property],
        }
    )
    return [claim for claims in result["claims"].values() for claim in claims]


def locators(client, statements):
    properties, _ = client.vocabulary()
    return [
        json.loads(snak["datavalue"]["value"])
        for claim in statements
        for reference in claim.get("references", [])
        for snak in reference["snaks"].get(
            properties["source_part_locator"], []
        )
    ]


def register(
    client, path, inventory, data, directory, title, *, previous=None
):
    registration = Registrar(client)
    container = registration.register_artifact(
        path, "presentation" if path.suffix == ".pptx" else "artifact", title
    )
    prepared = prepare_sources(inventory, data, directory, previous=previous)
    keep_inventory(prepared, data, directory)
    projection = project_sources(prepared, directory)
    assert not projection.unresolved
    owners = registration.register_container_sources(
        projection, directory, title=title
    )
    return container, projection, owners


def test_common_sources_preserve_owners_placements_and_citations(
    fictional_client, tmp_path
):
    client = fictional_client
    source = deck(tmp_path / "library-talk-2018.pptx")
    content = unpack_pptx(source)
    container, sources, owners = register(
        client,
        source,
        content.inventory,
        content.inventory_bytes,
        tmp_path / "deck-state",
        TITLE,
    )
    image_qid = owners[OWNER.sha1]
    image_parts = sources.sources[OWNER.sha1].part_ids
    repeated = {
        p.id for p in sources.inventory.placements if p.child in image_parts
    }
    assert len(repeated) == 2
    composition = locators(client, statements(client, image_qid, "part_of"))
    assert {
        record["placement"]
        for record in composition
        if record["container"] == sources.inventory.source.sha1
    } == repeated

    claims = []
    for part in [*sources.bodies, *sources.notes]:
        claims.append(
            {
                "subject": OWNER.model_dump(),
                "property": "caption",
                "value": {
                    "type": "monolingual",
                    "value": {"text": CAPTION, "language": "en"},
                },
                "source": {
                    "artifact": sources.inventory.source.model_dump(),
                    "page_number": "1",
                    "transcription_excerpt": {
                        "text": CAPTION,
                        "language": "en",
                    },
                    "support": sources.support(part.id).model_dump(),
                },
            }
        )
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": claims,
        }
    )
    placed = {sources.inventory.source.sha1: set(owners)}
    result = write_mapped(BatchWriter(client), batch, placed=placed)
    assert not result.refused
    written = statements(client, image_qid, "caption")
    support = [
        record
        for record in locators(client, written)
        if record["container"] == sources.inventory.source.sha1
    ]
    assert {record["kind"] for record in support} == {
        "slide_body",
        "speaker_notes",
    }
    assert statements(client, container.artifact_qid, "caption") == []
    again = write_mapped(BatchWriter(client), batch, placed=placed)
    assert again.written == again.referenced == 0

    for annotated in (False, True):
        page = pdfs.Page().text([CAPTION])
        page.image(pdfs.jpeg(flated=annotated), 72, 400, 128, 96)
        if annotated:
            page.annotate(
                "/Subtype /FreeText /Rect [72 500 300 540] "
                "/Contents (Wastl and Klara) /DA (/Helv 12 Tf 0 g)"
            )
        path = tmp_path / ("annotated.pdf" if annotated else "native.pdf")
        path.write_bytes(pdfs.pdf([page]))
        plan = inventory_pdf(path.read_bytes(), lambda _: False)
        output = tmp_path / ("vision-output" if annotated else "native-output")
        output.mkdir()
        reading = read_pages(
            path, output, excluded=lambda _: False, inventory=plan
        )
        assert reading.pages[0].method == (
            "vision" if annotated else "text-layer"
        )
        directory = tmp_path / (
            "vision-state" if annotated else "native-state"
        )
        _, projected, registered = register(
            client,
            path,
            plan.inventory,
            plan.data,
            directory,
            f"{TITLE} {'annotated' if annotated else 'native'} PDF",
        )
        assert registered == owners

    # A different reading encoding cannot rebind the verified source owner.
    # A legal JPEG comment changes the file hash while retaining its pixels.
    comment = b"fictional alternate encoding"
    alternate = (
        IMAGE[:2]
        + b"\xff\xfe"
        + struct.pack(">H", len(comment) + 2)
        + comment
        + IMAGE[2:]
    )
    reference = ArtifactRef.from_bytes(alternate)
    changed = projected.inventory.model_copy(deep=True)
    for part in changed.parts:
        for representation in part.representations:
            if representation.method == "pdfium-image":
                representation.byte_object = reference
                representation.file = f"representations/{reference.sha1}.jpg"
    data = {reference.sha1: alternate}
    prepared = prepare_sources(
        changed, data, directory, previous=projected.inventory
    )
    keep_inventory(prepared, data, directory)
    refreshed = project_sources(prepared, directory)
    assert (
        refreshed.sources[OWNER.sha1].current_input.representation == reference
    )
    entities = set(client.list_entity_ids())
    owner_claims = client.fetch_claims([image_qid])[image_qid]
    assert (
        Registrar(client).register_container_sources(
            refreshed, directory, title=f"{TITLE} annotated PDF"
        )
        == owners
    )
    assert set(client.list_entity_ids()) == entities
    assert client.fetch_claims([image_qid])[image_qid] == owner_claims

    # Current exclusions remove admission without rewriting established
    # identity or erasing graph evidence from an earlier accepted write.
    before = client.fetch_claims([image_qid])[image_qid]
    denied = prepare_sources(
        refreshed.inventory,
        {},
        directory,
        previous=refreshed.inventory,
        excluded=lambda sha1, names: sha1 == OWNER.sha1,
    )
    excluded = project_sources(denied, directory)
    assert not excluded.sources
    assert (
        Registrar(client).register_container_sources(
            excluded, directory, title=f"{TITLE} annotated PDF"
        )
        == {}
    )
    assert client.fetch_claims([image_qid])[image_qid] == before

    # Generated image bytes are only a reading representation. A separate
    # artifact with those bytes is not evidence of the same original source.
    path = tmp_path / "generated-image.pdf"
    path.write_bytes(
        pdfs.pdf(
            [
                pdfs.Page()
                .text([CAPTION])
                .image(pdfs.gray(160, 100), 72, 400, 160, 100)
            ]
        )
    )
    plan = inventory_pdf(path.read_bytes(), lambda _: False)
    directory = tmp_path / "generated-state"
    prepared = prepare_sources(plan.inventory, plan.data, directory)
    keep_inventory(prepared, plan.data, directory)
    sources = project_sources(prepared, directory)
    (generated,) = sources.sources.values()
    registration = Registrar(client)
    registration.register_artifact(path, "artifact", "Library Talk image PDF")
    existing = registration.register_artifact(
        directory / generated.file,
        "photograph",
        "Library Talk generated image",
    )
    entities = set(client.list_entity_ids())
    before = client.fetch_claims([existing.artifact_qid])
    with pytest.raises(ValueError, match="original source-part binding"):
        Registrar(client).register_container_sources(
            sources, directory, title="Library Talk image PDF"
        )
    assert client.fetch_claims([existing.artifact_qid]) == before
    assert set(client.list_entity_ids()) == entities

    # Original package images remain sources even when small and unplaced.
    # Registration records containment, without inventing a slide occurrence.
    source = deck(tmp_path / "library-talk-unplaced.pptx", unplaced=True)
    content = unpack_pptx(source)
    directory = tmp_path / "unplaced-state"
    container, sources, owners = register(
        client,
        source,
        content.inventory,
        content.inventory_bytes,
        directory,
        TITLE,
    )
    tiny = ArtifactRef.from_bytes(small_image())
    assert owners[OWNER.sha1] == image_qid
    tiny_qid = owners[tiny.sha1]
    bound = sources.sources[tiny.sha1]
    assert bound.placements == []
    (composition,) = statements(client, tiny_qid, "part_of")
    assert (
        composition["mainsnak"]["datavalue"]["value"]["id"]
        == container.artifact_qid
    )
    assert not composition.get("qualifiers")
    assert locators(client, [composition]) == [
        {
            "container": sources.inventory.source.sha1,
            "part": bound.part_ids[0],
            "kind": "embedded_resource",
        }
    ]
    before = client.fetch_claims(list(owners.values()))
    entities = set(client.list_entity_ids())
    assert (
        Registrar(client).register_container_sources(
            sources, directory, title=TITLE
        )
        == owners
    )
    assert client.fetch_claims(list(owners.values())) == before
    assert set(client.list_entity_ids()) == entities
