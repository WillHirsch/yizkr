"""Live PPTX compound-artifact flow (ADR-0008): deck registration with
slide fragments, derived-media items with deck-referenced provenance,
and idempotent re-registration. No LLM involved — the adapter is
deterministic; claim extraction is covered elsewhere."""

from __future__ import annotations

import struct
import zipfile
import zlib

import pytest

from tests.unit.test_presentation import (
    IMAGE_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.pipeline.presentation import register_derived_media, unpack_pptx
from yizkr.pipeline.registrar import register_artifact

pytestmark = pytest.mark.live

DECK_TITLE = "Integration-test presentation"


def tiny_png_bytes() -> bytes:
    def chunk(tag, data):
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload))
        )

    raw = b"".join(b"\x00" + b"\x64\x64\x64" * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def build_live_deck(path):
    presentation = (
        "<p:presentation"
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/>'
        "</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            rels_xml([("rId1", SLIDE_REL, "slides/slide1.xml")]),
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            slide_xml(["written by tests/integration; safe to delete"]),
        )
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            rels_xml([("rId7", IMAGE_REL, "../media/testimage1.png")]),
        )
        archive.writestr("ppt/media/testimage1.png", tiny_png_bytes())
    return path


def test_deck_and_derived_media_provenance(client, tmp_path):
    deck_path = build_live_deck(
        tmp_path / "integration-test-presentation.pptx"
    )
    content = unpack_pptx(deck_path)

    deck = register_artifact(
        client,
        media_path=deck_path,
        artifact_type="presentation",
        title=DECK_TITLE,
        description="written by tests/integration; safe to delete",
        pages=len(content.slides),
    )
    assert deck.fragment_qids[1].startswith("Q")  # slide fragment exists

    derived = register_derived_media(
        client,
        deck_qid=deck.artifact_qid,
        deck_title=DECK_TITLE,
        content=content,
        workdir=tmp_path / "media",
    )
    (media_qid,) = derived.values()

    properties, items = client.vocabulary()
    data = client.api_get(
        {"action": "wbgetclaims", "entity": media_qid, "format": "json"}
    )
    claims = data["claims"]
    # instance of photograph, part of the deck with the slide qualifier,
    # and EVERY statement referenced stated in -> the deck (ADR-0008)
    instance_targets = {
        claim["mainsnak"]["datavalue"]["value"]["id"]
        for claim in claims.get(properties["instance_of"], [])
    }
    assert items["photograph"] in instance_targets
    (part,) = claims[properties["part_of"]]
    assert part["mainsnak"]["datavalue"]["value"]["id"] == deck.artifact_qid
    slide_qualifiers = [
        snak["datavalue"]["value"]
        for snak in part.get("qualifiers", {}).get(
            properties["page_number"], []
        )
    ]
    assert slide_qualifiers == ["1"]
    for statements in claims.values():
        for claim in statements:
            stated = {
                snak["datavalue"]["value"]["id"]
                for ref in claim.get("references", [])
                for snak in ref["snaks"].get(properties["stated_in"], [])
            }
            assert deck.artifact_qid in stated

    # idempotent: same deck, same labels -> same items, nothing minted
    again = register_derived_media(
        client,
        deck_qid=deck.artifact_qid,
        deck_title=DECK_TITLE,
        content=content,
        workdir=tmp_path / "media2",
    )
    assert again == derived
