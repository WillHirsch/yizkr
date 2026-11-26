"""Live write-time registration (#103): the write's registration of a
deck (`ScratchDriver._step_register`) registers the deck, its slides as
page fragments, and its image from the record the stages before the write
keep of it, each found or made by the SHA-1 of its media; a second write
registers nothing new.

Fictional synthetic media only (docs/EXAMPLES.md). The deck's image
carries a marker of the session's own (conftest.fixture_mark), and the
deck's title the SHA-1 prefix of the image's bytes: each session makes
items of its own, so what an earlier session wrote, a live mutant's run
included, never meets it, and a re-run within the session finds its own
items again and writes nothing. Like every live test it writes fictional
data to the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

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
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import registrar, scratch
from yizkr.pipeline.scratch import ScratchDriver

from .conftest import fictional_png, fixture_mark

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new bytes
FIXTURE = (
    "#103: the family firm's deck of three slides, the shop front on the "
    "first two"
)
SESSION = fixture_mark(FIXTURE, registrar, scratch)

# the shop front at Brunnenring 35, on two slides of the family firm's deck
SHOP_FRONT = fictional_png(f"yizkr live test #103: the shop front {SESSION}")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # deterministic ZIP bytes across runs


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


DECK_TITLE = f"Integration-test Hutmacherei deck {sha1(SHOP_FRONT)[:8]}"
SLIDES = 3


def build_deck(path):
    """The family firm's deck: three slides, the shop front on the first
    two. The same bytes throughout the session."""
    presentation = (
        "<p:presentation"
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(
            f'<p:sldId id="{255 + n}" r:id="rId{n}"/>'
            for n in range(1, SLIDES + 1)
        )
        + "</p:sldIdLst></p:presentation>"
    )

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=FIXED_TIME), data)

    with zipfile.ZipFile(path, "w") as archive:
        put(archive, "[Content_Types].xml", "<Types/>")
        put(archive, "ppt/presentation.xml", presentation)
        put(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml(
                [
                    (f"rId{n}", SLIDE_REL, f"slides/slide{n}.xml")
                    for n in range(1, SLIDES + 1)
                ]
            ),
        )
        for n in range(1, SLIDES + 1):
            put(
                archive,
                f"ppt/slides/slide{n}.xml",
                slide_xml([f"Hutmacherei zum Blauen Hut, slide {n}"]),
            )
            links = (
                [("rId7", IMAGE_REL, "../media/image1.png")] if n < 3 else []
            )
            put(
                archive, f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        put(archive, "ppt/media/image1.png", SHOP_FRONT)
    return path


def entities(client, *qids: str) -> dict[str, dict]:
    data = client.api_get(
        {"action": "wbgetentities", "ids": "|".join(qids), "props": "claims"}
    )
    return data["entities"]


def revisions(client, *qids: str) -> dict[str, int]:
    data = client.api_get(
        {"action": "wbgetentities", "ids": "|".join(qids), "props": "info"}
    )
    return {qid: data["entities"][qid]["lastrevid"] for qid in qids}


def value(snak):
    found = (snak.get("datavalue") or {}).get("value")
    return found["id"] if isinstance(found, dict) else found


def parts_of(client, whole: str) -> dict[str, dict[str, list[str]]]:
    """Every item `part of` the whole, lag-free: for fragments (items
    that are an `instance of artifact fragment`) and for the rest, page
    -> the items on it."""
    properties, items = client.vocabulary()
    found: dict[str, dict[str, list[str]]] = {"fragment": {}, "other": {}}
    for qid, entity in client.iter_entities("claims"):
        claims = entity.get("claims", {})
        kinds = {
            value(c["mainsnak"])
            for c in claims.get(properties["instance_of"], [])
        }
        kind = "fragment" if items["artifact_fragment"] in kinds else "other"
        for claim in claims.get(properties["part_of"], []):
            if value(claim["mainsnak"]) != whole:
                continue
            for snak in claim.get("qualifiers", {}).get(
                properties["page_number"], []
            ):
                found[kind].setdefault(value(snak), []).append(qid)
    return found


def cited(client, claim) -> set[str]:
    properties, _ = client.vocabulary()
    return {
        value(snak)
        for reference in claim.get("references", [])
        for snak in reference["snaks"].get(properties["stated_in"], [])
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


def test_the_write_registers_a_deck_its_slides_and_its_image(client, tmp_path):
    properties, items = client.vocabulary()

    def register(where) -> str:
        """The deck's registration by a write of a run of its own: its
        QID, once the write has said it registered the deck's image."""
        folder = where / "corpus"
        folder.mkdir(parents=True)
        build_deck(folder / f"{DECK_TITLE}.pptx")
        lines: list[str] = []
        orchestrator = ScratchDriver(
            folder,
            where / "state",
            exclusions=ExclusionList(),
            log=lambda *args, **kwargs: lines.append(" ".join(args)),
        )
        orchestrator._client = client
        (doc,) = orchestrator.documents()
        # the record of the deck's image is made when the write finds none
        deck = orchestrator._step_register(doc)
        assert "  derived media: 1 registered" in lines
        return deck

    deck = register(tmp_path / "first")

    parts = parts_of(client, deck)
    assert sorted(parts["fragment"]) == [str(n) for n in range(1, SLIDES + 1)]
    assert all(len(qids) == 1 for qids in parts["fragment"].values())
    (image,) = {qid for qids in parts["other"].values() for qid in qids}
    assert parts["other"] == {"1": [image], "2": [image]}
    claims = entities(client, image)[image]["claims"]
    (hashed,) = claims[properties["media_sha1"]]
    assert value(hashed["mainsnak"]) == sha1(SHOP_FRONT)
    # what the deck says of its image is stated in the deck (ADR-0008)
    assert deck in cited(client, hashed)
    for claim in claims[properties["part_of"]]:
        if value(claim["mainsnak"]) == deck:
            assert deck in cited(client, claim)
    kinds = {value(c["mainsnak"]) for c in claims[properties["instance_of"]]}
    assert items["photograph"] in kinds
    # its bytes are one file on the wiki, the one its item names
    (stored,) = files_with_bytes(client, sha1(SHOP_FRONT))
    names = {
        value(c["mainsnak"]).removeprefix("File:").replace(" ", "_")
        for c in claims[properties["media_file"]]
    }
    assert stored in names

    # a second write registers nothing new
    touched = [
        deck,
        image,
        *(q for qs in parts["fragment"].values() for q in qs),
    ]
    before = revisions(client, *touched)
    assert register(tmp_path / "second") == deck
    assert revisions(client, *touched) == before
    assert files_with_bytes(client, sha1(SHOP_FRONT)) == {stored}
    assert parts_of(client, deck) == parts
