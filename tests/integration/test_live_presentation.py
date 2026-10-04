"""Live PPTX compound-artifact flow (ADR-0008): deck registration with
slide fragments, derived-media items with deck-referenced provenance,
registered from the record the stages before the write keep of them
(#128, #103), one derived item per distinct image across decks
(ADR-0010, #50), and idempotent re-registration. No LLM involved — the
adapter is deterministic; claim extraction is covered elsewhere.

Re-runnable against a non-empty graph: the decks' titles and image carry
the session's marker (conftest.fixture_mark), so each session registers
decks and an image of its own, and what an earlier session wrote, a live
mutant's run included, never meets it. Within the session the decks are
built byte-for-byte identically (fixed ZIP timestamps), so a re-run
reuses the same deck and media items."""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from tests.unit.test_presentation import (
    IMAGE_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.pipeline import presentation, registrar
from yizkr.pipeline.presentation import record_derived_media, unpack_pptx
from yizkr.pipeline.registrar import Registrar

from .conftest import fictional_png, fixture_mark

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0008: a deck with one slide and an image on it, and a second deck "
    "of two slides with the same image on its second"
)
MARK = fixture_mark(FIXTURE, presentation, registrar)
DECK_TITLE = f"Integration-test presentation {MARK}"
SECOND_DECK_TITLE = f"Integration-test presentation, second deck {MARK}"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # deterministic ZIP bytes across runs
IMAGE = fictional_png(f"yizkr live test: the decks' image {MARK}")


def build_live_deck(path, image_slide: int = 1, slides: int = 1):
    """A synthetic deck of `slides` slides; the one test image sits on
    `image_slide`. Identical arguments give identical bytes."""
    presentation = (
        "<p:presentation"
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(
            f'<p:sldId id="{255 + n}" r:id="rId{n}"/>'
            for n in range(1, slides + 1)
        )
        + "</p:sldIdLst></p:presentation>"
    )

    def write(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=FIXED_TIME), data)

    with zipfile.ZipFile(path, "w") as archive:
        write(archive, "[Content_Types].xml", "<Types/>")
        write(archive, "ppt/presentation.xml", presentation)
        write(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml(
                [
                    (f"rId{n}", SLIDE_REL, f"slides/slide{n}.xml")
                    for n in range(1, slides + 1)
                ]
            ),
        )
        for n in range(1, slides + 1):
            write(
                archive,
                f"ppt/slides/slide{n}.xml",
                slide_xml(
                    [f"written by tests/integration, slide {n} of {slides}"]
                ),
            )
            links = (
                [("rId7", IMAGE_REL, "../media/testimage1.png")]
                if n == image_slide
                else []
            )
            write(
                archive, f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        write(archive, "ppt/media/testimage1.png", IMAGE)
    return path


def register_deck(client, tmp_path, title, **layout):
    """Register a deck as the write does: the deck with its slides, then
    its derived media from their record. Returns (the deck's registration,
    {media SHA-1 -> QID})."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    deck_path = build_live_deck(tmp_path / f"{title}.pptx", **layout)
    content = unpack_pptx(deck_path)
    registrar = Registrar(client)
    deck = registrar.register_artifact(
        deck_path,
        artifact_type="presentation",
        title=title,
        description="written by tests/integration; safe to delete",
        pages=len(content.slides),
    )
    directory = tmp_path / "state" / title
    record = record_derived_media(
        content,
        source_sha1=deck.content_sha1,
        title=title,
        directory=directory,
    )
    return deck, registrar.register_derived_media(record, directory)


def cited(claim, stated_in: str) -> set[str]:
    return {
        snak["datavalue"]["value"]["id"]
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(stated_in, [])
    }


def files_with_bytes(client, content_sha1: str) -> set[str]:
    """Every wiki file whose current bytes have this SHA-1."""
    data = client.api_get(
        {
            "action": "query",
            "list": "allimages",
            "aisha1": content_sha1,
            "ailimit": "max",
        }
    )
    return {image["name"] for image in data["query"]["allimages"]}


def test_deck_and_derived_media_provenance(client, tmp_path):
    properties, items = client.vocabulary()
    stated_in = properties["stated_in"]
    image_sha1 = hashlib.sha1(IMAGE).hexdigest()

    deck, derived = register_deck(client, tmp_path, DECK_TITLE)
    assert deck.fragment_qids[1].startswith("Q")  # slide fragment exists
    blobs = files_with_bytes(client, image_sha1)
    assert blobs  # the image is on the wiki
    # a second deck embedding the SAME image on its slide 2 (#50)
    second, second_derived = register_deck(
        client, tmp_path, SECOND_DECK_TITLE, image_slide=2, slides=2
    )
    assert second.artifact_qid != deck.artifact_qid
    # ...reuses the stored bytes: no second file for the same image
    assert files_with_bytes(client, image_sha1) == blobs

    # one distinct image -> one derived artifact item (ADR-0010)
    (media_qid,) = derived.values()
    assert second_derived == derived

    entity = client.api_get({"action": "wbgetentities", "ids": media_qid})[
        "entities"
    ][media_qid]
    claims = entity["claims"]

    # rule 1: every statement on the item carries a stated-in reference
    for statements in claims.values():
        for claim in statements:
            assert cited(claim, stated_in), claim

    # part of EACH deck with its own slide qualifier, each statement
    # stated in -> that deck (ADR-0008), and of nothing else
    parts = {}
    for claim in claims[properties["part_of"]]:
        target = claim["mainsnak"]["datavalue"]["value"]["id"]
        slides = [
            snak["datavalue"]["value"]
            for snak in claim.get("qualifiers", {}).get(
                properties["page_number"], []
            )
        ]
        assert target in cited(claim, stated_in)  # a deck asserts itself
        parts.setdefault(target, []).append(slides)
    assert parts == {deck.artifact_qid: [["1"]], second.artifact_qid: [["2"]]}

    # the deck-independent statements are single and cited by both decks
    # (an agreeing second deck adds its reference, not a duplicate)
    both = {deck.artifact_qid, second.artifact_qid}

    def statements(key):
        return claims.get(properties[key], [])

    (instance,) = [
        claim
        for claim in statements("instance_of")
        if claim["mainsnak"]["datavalue"]["value"]["id"] == items["photograph"]
    ]
    (sha1,) = statements("media_sha1")
    assert sha1["mainsnak"]["datavalue"]["value"] == image_sha1
    assert both <= cited(instance, stated_in)
    assert both <= cited(sha1, stated_in)
    # the media file both decks cite is the one holding the image's bytes
    shared_files = {
        claim["mainsnak"]["datavalue"]["value"].removeprefix("File:")
        for claim in statements("media_file")
        if both <= cited(claim, stated_in)
    }
    assert shared_files
    assert {name.replace(" ", "_") for name in shared_files} <= blobs

    # idempotent: same decks, same bytes -> same items, and nothing is
    # written at all (the media item's revision does not move)
    again = register_deck(client, tmp_path / "again", DECK_TITLE)
    again_second = register_deck(
        client, tmp_path / "again", SECOND_DECK_TITLE, image_slide=2, slides=2
    )
    assert again[0].artifact_qid == deck.artifact_qid
    assert again_second[0].artifact_qid == second.artifact_qid
    assert again[1] == derived and again_second[1] == derived
    revision = client.api_get(
        {"action": "wbgetentities", "ids": media_qid, "props": "info"}
    )["entities"][media_qid]["lastrevid"]
    assert revision == entity["lastrevid"]
