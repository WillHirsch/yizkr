"""No wiki reads or writes before the write (#128; ADR-0025 §2).

Every step a document takes before the write runs here against a wiki
that refuses. The orchestrator's wiki client and connection settings are
stubs that fail on any read or write, no other client or settings can be
made, and the network is closed, so a query-service request fails too.
Each refusal is recorded before it raises: the orchestrator isolates a
document's failure, so the tests assert that nothing was asked of the
wiki, not only that nothing raised.

The steps are those #128 lists that have code: transcription (by vision,
from a text file, from slide text and from a curator's correction) and
its check, extraction (entity manifest, chunks, critique), the GEDCOM
adapter, a deck's slide text and derived media, the reconcile step, and
adjudication given a roster. Corpus clustering and flags have no code
yet. Two steps before the write that are outside that list still use the
wiki in `ingest run`, and the orchestrator here replaces them:
registration, which runs first until write-time registration (#103)
takes its place, and the resolve step's exact-label read, which goes
with the identity-map writer (#109). A document stops at the
claim-review gate, so the write itself is never reached.

Every document and deck is fictional (docs/EXAMPLES.md).
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import socket
import zipfile
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_presentation import (
    IMAGE_REL,
    NOTES_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.corpus import ExclusionList
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    BATCHED,
    DOCUMENT_STAGES,
    REUSED,
    Orchestrator,
)
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    DERIVED_MEDIA_DIR,
    DerivedMediaRecord,
    PresentationContent,
    Slide,
    record_derived_media,
    unpack_pptx,
)
from yizkr.pipeline.reconcile import (
    EQUIVALENCE_FIELDS,
    RosterEntity,
    adjudicate,
    block_candidates,
)
from yizkr.pipeline.rundir import DONE, run_state

SOURCES = {source.key: source for source in u.SOURCES}
RUN = {
    "model_identifier": "scripted-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-28T09:00:00Z",
}


# -- the refusing wiki --------------------------------------------------------


class Refused(AssertionError):
    """A step before the write asked the wiki for something."""


class Refusals:
    """Every request made of the wiki, recorded before it is refused."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def refuse(self, request: str):
        self.asked.append(request)
        raise Refused(f"{request}: a step before the write asked the wiki")


class Refusing:
    """A stand-in for the wiki client or its connection settings: every
    attribute is refused, and so every read and every write."""

    def __init__(self, refusals: Refusals, what: str) -> None:
        self._refusals = refusals
        self._what = what

    def __getattr__(self, name: str):
        self._refusals.refuse(f"{self._what}.{name}")


@pytest.fixture
def refusals(monkeypatch) -> Refusals:
    """The wiki refuses at every layer: no client or connection settings
    can be made, and no socket connects, so neither the wiki's API nor
    its query service can be reached."""
    refusals = Refusals()

    def refused(request: str):
        def refuse(*_args, **_kwargs):
            refusals.refuse(request)

        return refuse

    monkeypatch.setattr(WikiClient, "__init__", refused("WikiClient()"))
    monkeypatch.setattr(Config, "load", classmethod(refused("Config.load()")))
    monkeypatch.setattr(socket.socket, "connect", refused("socket.connect()"))
    monkeypatch.setattr(
        socket, "create_connection", refused("socket.create_connection()")
    )
    return refusals


# -- a fictional corpus -------------------------------------------------------

DECK = "Hutmacherei zum Blauen Hut.pptx"
# synthetic image bytes: the shop front, and the studio portrait, which is
# also a document of its own in the corpus
SHOP_FRONT = b"\x89PNG synthetic: the shop front at Brunnenring 35"
PORTRAIT = b"\x89PNG synthetic: " + "Photograph Brüsrich, Peiltal".encode()
FILM = b"synthetic film"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def build_deck(path: Path) -> Path:
    """A fictional mini deck (ADR-0008) of three slides: the shop front on
    slides 1 and 2; the studio portrait on slide 2 as image2.png and on
    slide 3 as image4.png, the same bytes under two names; and on slide 3
    a film, which is not an image."""
    slides = {
        1: (["Hutmacherei zum Blauen Hut"], ["gegr. 1863, Brunnenring 35"]),
        2: (["Eugen Löbig, Hutmachermeister"], ["1836-1909"]),
        3: (["Photograph Brüsrich, Peiltal"],),
    }
    media = {
        1: ["image1.png"],
        2: ["image1.png", "image2.png"],
        3: ["image4.png", "clip1.mp4"],
    }
    presentation = (
        "<p:presentation xmlns:p="
        '"http://schemas.openxmlformats.org/presentationml/2006/main"'
        " xmlns:r="
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in slides)
        + "</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            rels_xml(
                [
                    (f"rId{n}", SLIDE_REL, f"slides/slide{n}.xml")
                    for n in slides
                ]
            ),
        )
        for n, paragraphs in slides.items():
            archive.writestr(
                f"ppt/slides/slide{n}.xml", slide_xml(*paragraphs)
            )
            links = [
                (f"rId{10 + i}", IMAGE_REL, f"../media/{name}")
                for i, name in enumerate(media[n])
            ]
            if n == 1:
                links.append(
                    ("rId20", NOTES_REL, "../notesSlides/notesSlide1.xml")
                )
            archive.writestr(
                f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            slide_xml(["Adressbuch der Stadt Flörburg 1912"]),
        )
        archive.writestr("ppt/media/image1.png", SHOP_FRONT)
        archive.writestr("ppt/media/image2.png", PORTRAIT)
        archive.writestr("ppt/media/image4.png", PORTRAIT)
        archive.writestr("ppt/media/clip1.mp4", FILM)
    return path


def corpus(tmp_path: Path) -> Path:
    """One fictional document of each kind: a postcard as a text file, the
    studio portrait as an image, the family's GEDCOM export and the deck."""
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "postcard-1938.txt").write_text(SOURCES["postcard-1938"].text)
    (folder / "studio-photo-1890s.png").write_bytes(PORTRAIT)
    shutil.copy(u.GEDCOM_FIXTURE, folder / "example-family.ged")
    build_deck(folder / DECK)
    return folder


# who each document is about, to the scripted engine: one person, born in
# the year the universe gives them
PEOPLE = {
    "postcard-1938.txt": ("Klara Löbig", 1917),
    "studio-photo-1890s.png": ("Eugen Löbig", 1836),
    DECK: ("Eugen Löbig", 1836),
}


class ScriptedEngine:
    """A scripted model for one document. It reads the portrait as the
    text on its mount and reverse, names one person, emits one claim about
    them citing the document by the SHA-1 of its bytes, and finds nothing
    to critique. It has no conversation capability, so chunks see the
    document inline."""

    model_identifier = "scripted-model"

    def __init__(self, source_sha1: str, person: str, year: int) -> None:
        self.source_sha1, self.person, self.year = source_sha1, person, year

    def complete(self, prompt: str, image_paths=()) -> str:
        if image_paths:
            return SOURCES["studio-photo-1890s"].text
        if "manifest of EVERY distinct entity" in prompt:
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": self.person,
                            "kind": "human",
                            "mention_lines": [1],
                        }
                    ]
                }
            )
        if prompt.startswith("You review a genealogical extraction"):
            return '{"done": true, "findings_emitted": 0}'
        claim = {
            "subject": {
                "kind": "local",
                "local_id": "e1",
                "label": self.person,
            },
            "property": "date_of_birth",
            "value": {"type": "time", "year": self.year},
            "source": {
                "artifact": {"kind": "artifact", "sha1": self.source_sha1}
            },
        }
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


def quiet(*args, **kwargs) -> None:
    pass


class BeforeTheWrite(Orchestrator):
    """`ingest run`'s orchestrator with the wiki refusing and the engine
    scripted. Registration and the resolve step, which use the wiki and
    are not steps #128 covers, are replaced: registration names a fixed
    artifact item, and resolve leaves the batch as it is."""

    def __init__(self, folder: Path, state_dir: Path, refusals, **kwargs):
        kwargs.setdefault("exclusions", ExclusionList())
        super().__init__(folder, state_dir, log=quiet, **kwargs)
        self._client = Refusing(refusals, "wiki")
        self._config = Refusing(refusals, "connection settings")

    def _llm(self, role, doc):
        return ScriptedEngine(doc.sha1, *PEOPLE[doc.source.name])

    def _step_register(self, doc):
        return "Q23"

    def _step_resolve(self, doc, batch):
        return batch


def this_run(orchestrator: Orchestrator, name: str) -> set[tuple[str, str]]:
    """(document SHA-1, stage) of each event `name` of the orchestrator's
    latest invocation."""
    return {
        (event["subject"], event["stage"])
        for event in orchestrator.rundir.events()
        if event["event"] == name
        and event["invocation"] == orchestrator.rundir.invocation
    }


def stages(doc) -> set[tuple[str, str]]:
    return {(doc.sha1, stage) for stage in DOCUMENT_STAGES[doc.kind]}


# -- the tests ----------------------------------------------------------------


def test_every_step_before_the_write_leaves_the_wiki_alone(tmp_path, refusals):
    orchestrator = BeforeTheWrite(
        corpus(tmp_path), tmp_path / "state", refusals
    )
    assert orchestrator.run() == 2  # every document waits for claim review

    assert refusals.asked == []
    docs = orchestrator.documents()
    assert sorted(doc.kind for doc in docs) == [
        "gedcom",
        "media",
        "presentation",
        "text",
    ]
    states = run_state(orchestrator.rundir)
    for doc in docs:
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert {states[stage].status for stage in stages(doc)} == {DONE}
        # the reconcile step proposed nothing, so the identity gate had
        # nothing to review, and the claims went on to their review sheet
        with open(doc.equivalences, newline="") as handle:
            assert list(csv.reader(handle)) == [EQUIVALENCE_FIELDS]
        assert doc.review.exists()


def test_rerunning_reuses_and_rechecks_the_views_without_the_wiki(
    tmp_path, refusals
):
    folder = corpus(tmp_path)
    BeforeTheWrite(folder, tmp_path / "state", refusals).run()

    # every view stands, and the vision and text-file transcriptions are
    # checked again as they are reused
    again = BeforeTheWrite(folder, tmp_path / "state", refusals)
    assert again.run() == 2
    docs = again.documents()
    for doc in docs:
        assert stages(doc) <= this_run(again, REUSED)

    # a curator's correction makes the postcard's transcription again, and
    # its batch from it
    (postcard,) = [doc for doc in docs if doc.kind == "text"]
    postcard.correction.write_text(
        SOURCES["postcard-1938"].text.replace("\n", "\n\n")
    )
    corrected = BeforeTheWrite(folder, tmp_path / "state", refusals)
    assert corrected.run() == 2
    assert {
        (postcard.sha1, "transcription"),
        (postcard.sha1, "extraction"),
    } <= this_run(corrected, "done")

    assert refusals.asked == []


def test_serial_extraction_in_parallel_leaves_the_wiki_alone(
    tmp_path, refusals
):
    """Serial mode's extract-all pass (ADR-0019) runs documents in
    parallel; the derived media are recorded without the lock that
    registration takes."""
    orchestrator = BeforeTheWrite(
        corpus(tmp_path),
        tmp_path / "state",
        refusals,
        serial=True,
        extract_concurrency=2,
    )
    assert orchestrator.run() == 2

    assert refusals.asked == []
    docs = orchestrator.documents()
    states = run_state(orchestrator.rundir)
    for doc in docs:
        assert {states[stage].status for stage in stages(doc)} == {DONE}
    # the first in sequence waits for claim review, and the rest for it
    assert sorted(doc.state.status for doc in docs) == [
        AWAITING_CLAIMS,
        BATCHED,
        BATCHED,
        BATCHED,
    ]


def test_a_decks_derived_media_are_recorded_by_sha1(tmp_path, refusals):
    orchestrator = BeforeTheWrite(
        corpus(tmp_path), tmp_path / "state", refusals
    )
    orchestrator.run()
    assert refusals.asked == []

    docs = orchestrator.documents()
    (deck,) = [doc for doc in docs if doc.kind == "presentation"]
    record = DerivedMediaRecord.model_validate_json(
        (deck.dir / DERIVED_MEDIA).read_text()
    )
    # each distinct image once, by its bytes, with every name and slide;
    # the film is not an image, so it is not recorded
    assert record.source_sha1 == deck.sha1
    assert {
        key: (medium.names, medium.slides)
        for key, medium in record.media.items()
    } == {
        sha1(SHOP_FRONT): (["image1.png"], [1, 2]),
        sha1(PORTRAIT): (["image2.png", "image4.png"], [2, 3]),
    }
    for key, medium in record.media.items():
        assert sha1((deck.dir / medium.file).read_bytes()) == key
    assert deck.state.detail["derived_media"] == {
        "image1.png": sha1(SHOP_FRONT),
        "image2.png": sha1(PORTRAIT),
        "image4.png": sha1(PORTRAIT),
    }
    # the portrait is also a document of its own: the same key, so the
    # same artifact when both are registered (ADR-0010)
    (portrait,) = [doc for doc in docs if doc.kind == "media"]
    assert portrait.sha1 in record.media


class TestRecordDerivedMedia:
    def test_recording_again_writes_the_same_files(self, tmp_path):
        deck = build_deck(tmp_path / DECK)
        content = unpack_pptx(deck)
        directory = tmp_path / "document"

        def files() -> dict[str, bytes]:
            return {
                str(path.relative_to(directory)): path.read_bytes()
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            }

        first = record_derived_media(
            content, source_sha1=sha1(deck.read_bytes()), directory=directory
        )
        kept = files()
        again = record_derived_media(
            content, source_sha1=sha1(deck.read_bytes()), directory=directory
        )
        assert again == first
        assert files() == kept
        assert sorted(kept) == sorted(
            [DERIVED_MEDIA, *(medium.file for medium in first.media.values())]
        )
        assert DerivedMediaRecord.model_validate_json(kept[DERIVED_MEDIA]) == (
            first
        )

    def test_a_deck_without_images_records_none(self, tmp_path):
        content = PresentationContent(
            slides=[Slide(number=1, text="Hutmacherei zum Blauen Hut")],
            media=[],
            skipped_media={".mp4": 1},
        )
        record = record_derived_media(
            content, source_sha1=sha1(b"a deck"), directory=tmp_path
        )
        assert record.media == {}
        assert (
            DerivedMediaRecord.model_validate_json(
                (tmp_path / DERIVED_MEDIA).read_text()
            )
            == record
        )
        assert not (tmp_path / DERIVED_MEDIA_DIR).exists()


class ScriptedAdjudicator:
    """Adjudicates the one pair it is shown as the same person."""

    model_identifier = "scripted-model"

    def complete(self, prompt: str, image_paths=()) -> str:
        assert prompt.startswith("You adjudicate entity identity")
        proposal = {
            "entity_id": "e1",
            "qid": "Q31",
            "verdict": "same",
            "confidence": 0.9,
            "evidence": "Gustl is Gustav's diminutive; both born in 1919",
        }
        return (
            json.dumps(proposal) + '\n{"done": true, "proposals_emitted": 1}'
        )


def test_adjudication_given_a_roster_asks_nothing_of_the_wiki(refusals):
    """Blocking and adjudication take the roster as an input: given one,
    they read nothing from the wiki."""
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [],
            "entities": [
                {
                    "id": "e1",
                    "label": "Gustl Löbig",
                    "kind": "human",
                    "summary": "son of Sebastian, born 1919",
                }
            ],
        }
    )
    roster = [
        RosterEntity(
            qid="Q31",
            label="Gustav Löbig",
            kind="human",
            birth="1919-01-07",
            fathers=["Sebastian Löbig (Q30)"],
        )
    ]
    candidates = block_candidates(batch.entities, roster, batch=batch)
    proposals = adjudicate(
        ScriptedAdjudicator(), batch.entities, candidates, batch
    )
    assert [(p.entity_id, p.qid, p.verdict) for p in proposals] == [
        ("e1", "Q31", "same")
    ]
    assert refusals.asked == []
