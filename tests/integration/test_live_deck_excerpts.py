"""Live deck excerpts (#502, ADR-0008): a deck and the scan it embeds on
a slide, each written as a witness of its own.

`ingest run` reads the library talk slide by slide, its text from the
slides' XML and the scan on its second slide by vision on its own, and
takes the scan as a document of its own, an excerpt. Once both review
sheets are accepted, the write registers the deck, with its slides as
fragments, and the scan as an item of its own `part of` the deck on
slide 2, each by the SHA-1 of its bytes, and writes each witness's claims
citing that witness: the deck's cite the deck, the excerpt's cite the
scan. A run after it writes nothing.

The engine is scripted: it reads the scan as the top of Sebastian's 1939
letter (`letter-excerpt-1939`) and names one person in each document.
Fictional data only (docs/EXAMPLES.md). The scan carries the session's
marker (conftest.fixture_mark), and the deck's title a mark from the
SHA-1 of that scan, so each session makes items of its own, and a re-run
within the session finds its own items again. Like every live test it
writes fictional data to the one instance, so the session that runs it
ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from io import BytesIO

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.records import record_answer
from tests.integration.conftest import fixture_mark
from tests.integration.test_live_excerpts import births_cited, media_item
from tests.integration.test_live_write_time_registration import (
    cited,
    entities,
    parts_of,
    revisions,
    value,
)
from tests.unit.test_presentation import (
    DECK_TIME,
    IMAGE_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import containers, orchestrate, presentation, transcribe
from yizkr.pipeline.orchestrate import EXCERPT, WRITTEN, Orchestrator
from yizkr.pipeline.review import accept_all
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT

pytestmark = pytest.mark.live

WIDTH, HEIGHT = 300, 120
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#502: the library talk, its second slide holding a scan of the top of "
    "Sebastian's 1939 letter, each naming one person"
)
SESSION = fixture_mark(
    FIXTURE, containers, orchestrate, presentation, transcribe
)
PIXELS = (
    hashlib.sha256(
        f"yizkr live test #502: the letter's top {SESSION}".encode()
    ).digest()
    * (WIDTH * HEIGHT // 32 + 1)
)[: WIDTH * HEIGHT]
SCAN = png(WIDTH, HEIGHT, 1, PIXELS)
SCAN_SHA1 = hashlib.sha1(SCAN).hexdigest()
MARK = SCAN_SHA1[:8]
TITLE = f"Integration-test library talk {MARK}"
SOURCES = {source.key: source for source in u.SOURCES}
LETTER_SOURCE = SOURCES["letter-excerpt-1939"]
LETTER = LETTER_SOURCE.text
# the person each witness names, under this file's label prefix
DECK_PERSON = f"Integration Test Nicholas Lobig {MARK}"
LETTER_PERSON = f"Integration Test Rahel Sänger {MARK}"


class Engine:
    """Reads the scan as the letter; in each document, names the person
    it holds and gives their year of birth, citing the document by the
    SHA-1 its prompt names; and finds nothing to critique."""

    model_identifier = "scripted-model"

    def complete(self, prompt: str, image_paths=()) -> str:
        if prompt.startswith(TRANSCRIPTION_PROMPT):
            return record_answer(
                LETTER,
                document_class=LETTER_SOURCE.artifact_class,
                languages=(LETTER_SOURCE.lang,),
            )
        person, year = (
            (LETTER_PERSON, 1871)
            if "Angerring 11" in prompt
            else (DECK_PERSON, 1950)
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
    # the one instance every live session writes to holds the writes of
    # many identity maps, their labels kept apart by their session marks
    one_map = False

    def _llm(self, role, doc):
        return Engine()


def library_talk() -> bytes:
    """The talk: a title slide, and a slide with the scan of the letter's
    top. The same bytes for the whole test session."""
    slides = {
        1: (["Where the Lobigs came from"], [f"Nicholas Lobig {MARK}"]),
        2: (["Wastl to Klara, July 1939"],),
    }
    presentation_xml = (
        "<p:presentation xmlns:p="
        '"http://schemas.openxmlformats.org/presentationml/2006/main"'
        " xmlns:r="
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in slides)
        + "</p:sldIdLst></p:presentation>"
    )
    out = BytesIO()

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=DECK_TIME), data)

    with zipfile.ZipFile(out, "w") as archive:
        put(archive, "[Content_Types].xml", "<Types/>")
        put(archive, "ppt/presentation.xml", presentation_xml)
        put(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml(
                [
                    (f"rId{n}", SLIDE_REL, f"slides/slide{n}.xml")
                    for n in slides
                ]
            ),
        )
        for n, paragraphs in slides.items():
            put(archive, f"ppt/slides/slide{n}.xml", slide_xml(*paragraphs))
            links = (
                [("rId10", IMAGE_REL, "../media/image1.png")] if n == 2 else []
            )
            put(
                archive, f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        put(archive, "ppt/media/image1.png", SCAN)
    return out.getvalue()


def ingest(client, folder, state):
    orchestrator = Scripted(
        folder, state, exclusions=ExclusionList(), log=lambda *a, **k: None
    )
    orchestrator._client = client
    orchestrator.run()
    return orchestrator


def test_a_deck_and_its_excerpt_are_written_as_two_witnesses(client, tmp_path):
    properties, items = client.vocabulary()
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / f"{TITLE}.pptx").write_bytes(library_talk())
    state = tmp_path / "state"

    first = ingest(client, folder, state)
    excerpt = first._excerpts[SCAN_SHA1]
    assert excerpt.kind == EXCERPT
    (doc,) = first.documents()
    for reviewed in (doc, excerpt):
        accept_all(reviewed.review, reviewed.review_reviewed)
    second = ingest(client, folder, state)
    (doc,) = second.documents()
    excerpt = second._excerpts[SCAN_SHA1]
    assert doc.state.status == WRITTEN, doc.state.error
    assert excerpt.state.status == WRITTEN, excerpt.state.error

    deck, scan = media_item(client, doc.sha1), media_item(client, SCAN_SHA1)
    assert deck != scan
    # the deck's slides are its fragments, and the scan an item of its
    # own, part of the deck on slide 2, stated in the deck (ADR-0008)
    parts = parts_of(client, deck)
    assert sorted(parts["fragment"]) == ["1", "2"]
    assert parts["other"] == {"2": [scan]}
    claims = entities(client, scan)[scan]["claims"]
    on_the_deck = [
        claim
        for claim in claims[properties["part_of"]]
        if value(claim["mainsnak"]) == deck
    ]
    assert [
        [
            value(snak)
            for snak in claim.get("qualifiers", {}).get(
                properties["page_number"], []
            )
        ]
        for claim in on_the_deck
    ] == [["2"]]
    for claim in on_the_deck:
        assert deck in cited(client, claim)
    kinds = {value(c["mainsnak"]) for c in claims[properties["instance_of"]]}
    assert items["photograph"] in kinds
    labels = client.api_get(
        {
            "action": "wbgetentities",
            "ids": scan,
            "props": "labels",
            "languages": "en",
        }
    )["entities"][scan]["labels"]
    assert labels["en"]["value"] == f"{TITLE} — image1.png"
    # each witness's claim is stated in that witness, and only it
    assert births_cited(client, LETTER_PERSON) == {scan}
    assert births_cited(client, DECK_PERSON) == {deck}

    # a run after it writes nothing
    before = revisions(client, deck, scan)
    third = ingest(client, folder, state)
    assert revisions(client, deck, scan) == before
    assert third._excerpts[SCAN_SHA1].state.status == WRITTEN
