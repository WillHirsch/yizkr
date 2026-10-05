"""Live write-time registration (#103): artifacts, page fragments and a
deck's derived media, each found or made by the SHA-1 of its media.

* Two files that share a title and a file name, but not their bytes, are
  two artifacts, and each has page fragments of its own, `part of` it and
  of nothing else (#187).
* Registering them again, by a registrar that reads the instance afresh,
  finds every item by its media's SHA-1: it makes no item, writes no
  statement and uploads no file.
* The write's registration of a deck (`Orchestrator._step_register`)
  registers the deck, its slides as page fragments, and its image from
  the record the stages before the write keep of it; a second write
  registers nothing new.

Fictional synthetic media only (docs/EXAMPLES.md). The fixture's bytes
carry a marker of the session's own (conftest.fixture_mark), and every
title, file name and deck the SHA-1 prefix of those bytes: each session
makes items of its own, so what an earlier session wrote, a live
mutant's run included, never meets it, and a re-run within the session
finds its own items again and writes nothing. Like every live
test it writes fictional data to the one instance, so the session that
runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from tests.integration.test_live_presentation import files_with_bytes
from tests.unit.test_presentation import (
    IMAGE_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import orchestrate, registrar
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.registrar import Registrar

from .conftest import fictional_png, fixture_mark

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new bytes
FIXTURE = (
    "#103: two copies of Gustav's birth register extract under one title "
    "and file name, two pages each; the family firm's deck of three "
    "slides, the shop front on the first two"
)
SESSION = fixture_mark(FIXTURE, registrar, orchestrate)

# two copies of Gustav's birth register extract, scanned into two folders
# under one file name: two artifacts that share a title
FIRST = fictional_png(f"yizkr live test #103: the extract, first {SESSION}")
SECOND = fictional_png(f"yizkr live test #103: the extract, 2nd {SESSION}")
# the shop front at Brunnenring 35, on two slides of the family firm's deck
SHOP_FRONT = fictional_png(f"yizkr live test #103: the shop front {SESSION}")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # deterministic ZIP bytes across runs


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


MARK = sha1(FIRST + SECOND)[:8]
TITLE = f"Integration-test birth extract {MARK}"
FILE_NAME = f"integration-test-birth-extract-{MARK}.png"
DECK_TITLE = f"Integration-test Hutmacherei deck {sha1(SHOP_FRONT)[:8]}"
PAGES = 2
SLIDES = 3


def write(tmp_path, folder: str, name: str, data: bytes):
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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


def register_both(client, tmp_path):
    """The two copies, by one registrar that reads the instance once."""
    registrar = Registrar(client)
    return [
        registrar.register_artifact(
            write(tmp_path, folder, FILE_NAME, data),
            "certificate",
            TITLE,
            DESCRIPTION,
            pages=PAGES,
        )
        for folder, data in (("emigration", FIRST), ("copies", SECOND))
    ]


def test_same_titled_artifacts_get_distinct_fragments(client, tmp_path):
    properties, items = client.vocabulary()
    first, second = register_both(client, tmp_path)

    assert first.artifact_qid != second.artifact_qid
    labels = {
        qid: entity["labels"]["en"]["value"]
        for qid, entity in client.api_get(
            {
                "action": "wbgetentities",
                "ids": f"{first.artifact_qid}|{second.artifact_qid}",
                "props": "labels",
                "languages": "en",
            }
        )["entities"].items()
    }
    assert labels == {
        first.artifact_qid: TITLE,
        second.artifact_qid: f"{TITLE} [{sha1(SECOND)[:8]}]",
    }
    assert (
        sorted(first.fragment_qids)
        == sorted(second.fragment_qids)
        == [
            1,
            2,
        ]
    )
    assert set(first.fragment_qids.values()).isdisjoint(
        second.fragment_qids.values()
    )
    for result in (first, second):
        artifact = result.artifact_qid
        found = entities(client, *result.fragment_qids.values())
        for page, qid in result.fragment_qids.items():
            claims = found[qid]["claims"]
            # an artifact fragment, part of its own artifact on its page,
            # each statement stated in that artifact (ADR-0003)
            (kind,) = claims[properties["instance_of"]]
            assert value(kind["mainsnak"]) == items["artifact_fragment"]
            assert cited(client, kind) == {artifact}
            (part,) = claims[properties["part_of"]]
            assert value(part["mainsnak"]) == artifact
            assert [
                value(snak)
                for snak in part["qualifiers"][properties["page_number"]]
            ] == [str(page)]
            assert cited(client, part) == {artifact}
        # the artifact's fragments, found by its part-of statements, are
        # these and no others
        assert parts_of(client, artifact)["fragment"] == {
            str(page): [qid] for page, qid in result.fragment_qids.items()
        }


def test_re_registration_creates_no_duplicates(client, tmp_path):
    first, second = register_both(client, tmp_path / "before")
    items = [
        qid
        for result in (first, second)
        for qid in (result.artifact_qid, *result.fragment_qids.values())
    ]
    before = revisions(client, *items)
    blobs = {
        data: files_with_bytes(client, sha1(data)) for data in (FIRST, SECOND)
    }
    assert all(blobs.values())

    # a later write's registrar, reading the instance afresh
    again = register_both(client, tmp_path / "again")

    assert [r.artifact_qid for r in again] == [
        first.artifact_qid,
        second.artifact_qid,
    ]
    assert [r.fragment_qids for r in again] == [
        first.fragment_qids,
        second.fragment_qids,
    ]
    assert not any(r.created for r in again)
    assert revisions(client, *items) == before  # nothing written
    assert {
        data: files_with_bytes(client, sha1(data)) for data in (FIRST, SECOND)
    } == blobs  # nothing uploaded


def test_the_write_registers_a_deck_its_slides_and_its_image(client, tmp_path):
    properties, items = client.vocabulary()

    def register(where) -> str:
        """The deck's registration by a write of a run of its own: its
        QID, once the write has said it registered the deck's image."""
        folder = where / "corpus"
        folder.mkdir(parents=True)
        build_deck(folder / f"{DECK_TITLE}.pptx")
        lines: list[str] = []
        orchestrator = Orchestrator(
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
