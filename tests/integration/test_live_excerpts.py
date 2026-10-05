"""Live excerpts (#397, ADR-0008): a born-digital PDF and the image it
places within a text-layer page, each written as a witness of its own.

`ingest run` reads the booklet page by page, reads its image on its own,
and takes the image as a document of its own, an excerpt. Once both
review sheets are accepted, the write registers the booklet, and its
image as an item of its own `part of` it on page 6, each by the SHA-1 of
its bytes, and writes each witness's claims citing that witness: the
booklet's cite the booklet, the excerpt's cite the image. A run after it
writes nothing.

The engine is scripted: it reads the image as the top of Sebastian's 1939
letter (`letter-excerpt-1939`) and names one person in each document.
Fictional data only (docs/EXAMPLES.md). The PDF's image carries the
session's marker (conftest.fixture_mark), and its title a mark from the
SHA-1 of that image, so each session makes items of its own, and a re-run
within the session finds its own items again. Like every live test it writes
fictional data to the one instance, so the session that runs it ends by
clearing the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs
from tests.fixtures.records import record_answer
from tests.integration.conftest import fixture_mark
from tests.integration.test_live_write_time_registration import (
    cited,
    entities,
    parts_of,
    revisions,
    value,
)
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import orchestrate, transcribe, writer
from yizkr.pipeline.orchestrate import EXCERPT, WRITTEN, Orchestrator
from yizkr.pipeline.review import accept_all
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT

pytestmark = pytest.mark.live

SOURCES = {source.key: source for source in u.SOURCES}
LETTER_SOURCE = SOURCES["letter-excerpt-1939"]
LETTER = LETTER_SOURCE.text
TRANSLATION = (
    "Translation: Flörburg, Brunnenring 35, 28 July 1939. Dear Pünktchen! "
    "Rahel now lives at Angerring 71."
)
WIDTH, HEIGHT = 300, 120
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#397: the booklet, its page 6 holding the top of Sebastian's 1939 "
    "letter above its translation, each naming one person"
)
# the letter's image: grey pixels of this test session's own. A live
# mutant run writes what the mutant wrote onto the items of the bytes it
# read, and statements are never removed, so each session reads new bytes;
# the test's second ingest is the re-run that must change nothing
SESSION = fixture_mark(FIXTURE, orchestrate, transcribe, writer)
PIXELS = (
    hashlib.sha256(
        f"yizkr live test #397: the letter's top {SESSION}".encode()
    ).digest()
    * (WIDTH * HEIGHT // 32 + 1)
)[: WIDTH * HEIGHT]
IMAGE = pdfs.Image(
    f"/Width {WIDTH} /Height {HEIGHT} /ColorSpace /DeviceGray "
    "/BitsPerComponent 8",
    PIXELS,
)
IMAGE_SHA1 = hashlib.sha1(png(WIDTH, HEIGHT, 1, PIXELS)).hexdigest()
MARK = IMAGE_SHA1[:8]
TITLE = f"Integration-test booklet {MARK}"
# the person each witness names, under this file's label prefix
BOOKLET_PERSON = f"Integration Test Nicholas Lobig {MARK}"
LETTER_PERSON = f"Integration Test Rahel Sänger {MARK}"


def booklet() -> bytes:
    """The booklet: a title page, and page 6 with the letter's image above
    its translation. The same bytes for the whole test session."""
    six = pdfs.Page().image(IMAGE, 72, 520, 300, 200)
    six.text(pdfs.lines(TRANSLATION), y=500)
    pages = [pdfs.Page().text(pdfs.lines(f"Our Family from Germany {MARK}"))]
    return pdfs.pdf(pages + [pdfs.Page()] * 4 + [six])


class Engine:
    """Reads the image as the letter; in each document, names the person
    it holds and gives their year of birth, citing the document by the
    SHA-1 its prompt names; and finds nothing to critique."""

    model_identifier = "scripted-model"

    def complete(self, prompt: str, image_paths=()) -> str:
        if prompt.startswith(TRANSCRIPTION_PROMPT):
            # a transcription is a record of the image's one page
            return record_answer(
                LETTER,
                document_class=LETTER_SOURCE.artifact_class,
                languages=(LETTER_SOURCE.lang,),
            )
        person, year = (
            (LETTER_PERSON, 1871)
            if "Angerring 11" in prompt
            else (BOOKLET_PERSON, 1950)
        )
        if "manifest of EVERY distinct entity" in prompt:
            entity = {"id": "e1", "label": person, "kind": "human"}
            return json.dumps({"entities": [entity | {"mention_lines": [1]}]})
        if prompt.startswith("You review a genealogical extraction"):
            return '{"done": true, "findings_emitted": 0}'
        sha1 = re.search(r'"sha1": "([0-9a-f]{40})"', prompt).group(1)
        claim = {
            "subject": {"kind": "local", "local_id": "e1", "label": person},
            "property": "date_of_birth",
            "value": {"type": "time", "year": year},
            "source": {"artifact": {"kind": "artifact", "sha1": sha1}},
        }
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


class Scripted(Orchestrator):
    def _llm(self, role, doc):
        return Engine()


def ingest(client, folder, state):
    orchestrator = Scripted(
        folder, state, exclusions=ExclusionList(), log=lambda *a, **k: None
    )
    orchestrator._client = client
    orchestrator.run()
    return orchestrator


def media_item(client, sha1: str) -> str:
    """The item that carries this media SHA-1."""
    properties, _items = client.vocabulary()
    found = [
        qid
        for qid, entity in client.iter_entities("claims")
        for claim in entity.get("claims", {}).get(properties["media_sha1"], [])
        if value(claim["mainsnak"]) == sha1
    ]
    assert len(found) == 1, found
    return found[0]


def births_cited(client, label: str) -> set[str]:
    """What the date-of-birth statements of the person labelled `label`
    are stated in."""
    properties, _items = client.vocabulary()
    (qid,) = [
        qid
        for qid, entity in client.iter_entities("labels|claims")
        if ((entity.get("labels") or {}).get("en") or {}).get("value") == label
    ]
    claims = entities(client, qid)[qid]["claims"]
    return {
        source
        for claim in claims[properties["date_of_birth"]]
        for source in cited(client, claim)
    }


def test_a_pdf_and_its_excerpt_are_written_as_two_witnesses(
    client, tmp_path, monkeypatch
):
    # the scripted engine reads every file; no poppler tool runs
    monkeypatch.setattr(transcribe, "require_pdf_renderer", lambda *a: None)
    properties, items = client.vocabulary()
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / f"{TITLE}.pdf").write_bytes(booklet())
    state = tmp_path / "state"

    first = ingest(client, folder, state)
    excerpt = first._excerpts[IMAGE_SHA1]
    assert excerpt.kind == EXCERPT
    (doc,) = first.documents()
    for reviewed in (doc, excerpt):
        accept_all(reviewed.review, reviewed.review_reviewed)
    second = ingest(client, folder, state)
    (doc,) = second.documents()
    excerpt = second._excerpts[IMAGE_SHA1]
    assert doc.state.status == WRITTEN, doc.state.error
    assert excerpt.state.status == WRITTEN, excerpt.state.error

    pdf, image = media_item(client, doc.sha1), media_item(client, IMAGE_SHA1)
    assert pdf != image
    # the image is an item of its own, part of the booklet on page 6, and
    # what the booklet says of it is stated in the booklet (ADR-0008)
    parts = parts_of(client, pdf)
    assert parts["other"] == {"6": [image]}
    claims = entities(client, image)[image]["claims"]
    # the image's one statement that it is part of the booklet, on page 6:
    # the first ingest's items are the second's too, so each write must
    # leave exactly that statement, never another beside it
    on_the_booklet = [
        claim
        for claim in claims[properties["part_of"]]
        if value(claim["mainsnak"]) == pdf
    ]
    assert [
        [
            value(snak)
            for snak in claim.get("qualifiers", {}).get(
                properties["page_number"], []
            )
        ]
        for claim in on_the_booklet
    ] == [["6"]]
    for claim in on_the_booklet:
        assert pdf in cited(client, claim)
    kinds = {value(c["mainsnak"]) for c in claims[properties["instance_of"]]}
    assert items["photograph"] in kinds
    labels = client.api_get(
        {
            "action": "wbgetentities",
            "ids": image,
            "props": "labels",
            "languages": "en",
        }
    )["entities"][image]["labels"]
    assert labels["en"]["value"] == f"{TITLE} — page 6, image 1"
    # each witness's claim cites that witness, and only it
    assert births_cited(client, LETTER_PERSON) == {image}
    assert births_cited(client, BOOKLET_PERSON) == {pdf}

    # a run after it writes nothing
    before = revisions(client, pdf, image)
    third = ingest(client, folder, state)
    assert revisions(client, pdf, image) == before
    assert third._excerpts[IMAGE_SHA1].state.status == WRITTEN
