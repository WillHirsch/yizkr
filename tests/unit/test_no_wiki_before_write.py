"""No wiki reads or writes before the write (#128; ADR-0025 §2).

Every step a document takes before the write runs here against a wiki
that refuses. The orchestrator's wiki client and connection settings are
stubs that fail on any read or write; no wiki client can be made and no
connection settings loaded; every connection setting read from the
environment names a closed loopback port; and no name lookup or socket
connect succeeds. So a request to the wiki or its query service is
refused whether its URL comes from a client, from the settings or from
a host name, and a request to a host that cannot be resolved is refused
at its lookup, before it could fail on its own. Each refusal is recorded
before it raises: the orchestrator isolates a document's failure, and a
step could swallow one, so the tests assert that nothing was asked of
the wiki, not only that nothing raised.

The steps are those #128 lists that have code: transcription (by vision,
from a text file, from slide text and from a curator's correction) and
its check, extraction (entity manifest, chunks, critique), the GEDCOM
adapter, a deck's slide text and container inventory, the reconcile
step, the per-document adjudication (`reconcile.adjudicate`) given a roster,
corpus clustering (#105), the scratch driver's corpus stage, over the
batches those steps made, and the adjudication of its ambiguous pairs
(#106) through a fake engine. Flags have no code yet; #112 requires them
to run under this refusing wiki. One
step before the write that is outside that list still uses the wiki in
`ingest run`, and the orchestrator here replaces it: the resolve step's
exact-label read, which goes with the identity-map writer (#109).
Registration is part of the write (#103), so nothing here replaces it: a
document stops at the claim-review gate, the write is never reached, and
the refusing wiki shows that nothing is registered before it.

Every document and deck is fictional (docs/EXAMPLES.md).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import socket
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.records import record_answer
from tests.unit.test_presentation import (
    DECK_TIME,
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
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    ContainerInventory,
    read_inventory,
)
from yizkr.pipeline.container_sources import project_sources
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    BATCHED,
    DOCUMENT_STAGES,
    REUSED,
    DocumentState,
    Orchestrator,
)
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA_DIR,
    DerivedMediaRecord,
    PresentationContent,
    Slide,
    read_derived_media,
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
from yizkr.pipeline.scratch import CORPUS_STAGES, CorpusContext

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


# a loopback port nothing listens on (the discard service's)
CLOSED = "127.0.0.1:9"
# every connection setting that names the wiki or its query service
# (yizkr.core.config), set to the closed port; the environment wins over
# an env file wherever a setting is read
CLOSED_SETTINGS = {
    "WIKIBASE_URL": f"http://{CLOSED}",
    "WDQS_SPARQL_URL": f"http://{CLOSED}/sparql",
    "WIKIBASE_PUBLIC_HOST": "127.0.0.1",
    "WDQS_PUBLIC_HOST": "127.0.0.1",
    "HTTP_PORT": "9",
    "PUBLIC_SCHEME": "http",
}


@pytest.fixture
def refusals(monkeypatch) -> Refusals:
    """The wiki refuses at every layer (the module docstring says how),
    and every request it refuses is recorded in the returned Refusals."""
    refusals = Refusals()

    def refused(request: str):
        def refuse(*_args, **_kwargs):
            refusals.refuse(request)

        return refuse

    for name, value in CLOSED_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(WikiClient, "__init__", refused("WikiClient()"))
    monkeypatch.setattr(Config, "load", classmethod(refused("Config.load()")))
    for lookup in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, lookup, refused(f"socket.{lookup}()"))
    monkeypatch.setattr(socket.socket, "connect", refused("socket.connect()"))
    monkeypatch.setattr(
        socket, "create_connection", refused("socket.create_connection()")
    )
    return refusals


# -- a fictional corpus -------------------------------------------------------

DECK = "Hutmacherei zum Blauen Hut.pptx"


# synthetic image bytes: the shop front, and the studio portrait, which is
# also a document of its own in the corpus
def tiny_png(caption: str) -> bytes:
    """A 1x1 PNG carrying `caption` in a text chunk, so that each caption
    gives other bytes: decoration, which a deck's transcription does not
    read on its own (yizkr.pipeline.containers), though the deck's
    record keeps it and the write registers it."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + caption.encode())
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )


SHOP_FRONT = tiny_png("synthetic: the shop front at Brunnenring 35")
PORTRAIT = tiny_png("synthetic: Photograph Brüsrich, Peiltal")
FILM = b"synthetic film"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def build_deck(
    path: Path,
    *,
    shop_front: bytes = SHOP_FRONT,
    firm: str = "gegr. 1863, Brunnenring 35",
    media: bool = True,
) -> Path:
    """A fictional mini deck (ADR-0008) of three slides. With `media`, the
    shop front is on slides 1 and 2; the studio portrait is on slide 2 as
    image2.png and on slide 3 as image4.png, the same bytes under two
    names; and slide 3 also holds a film, which is not an image.

    Every member carries one fixed timestamp, so the same arguments make
    the same bytes, and the same SHA-1, whenever the deck is built:
    `ZipFile.writestr` given a bare name stamps the current time, which
    gives two builds either side of a 2-second boundary different bytes
    (issue #479)."""
    slides = {
        1: (["Hutmacherei zum Blauen Hut"], [firm]),
        2: (["Eugen Löbig, Hutmachermeister"], ["1836-1909"]),
        3: (["Photograph Brüsrich, Peiltal"],),
    }
    names = {
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

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=DECK_TIME), data)

    with zipfile.ZipFile(path, "w") as archive:
        put(archive, "[Content_Types].xml", "<Types/>")
        put(archive, "ppt/presentation.xml", presentation)
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
                [
                    (f"rId{10 + i}", IMAGE_REL, f"../media/{name}")
                    for i, name in enumerate(names[n])
                ]
                if media
                else []
            )
            if n == 1:
                links.append(
                    ("rId20", NOTES_REL, "../notesSlides/notesSlide1.xml")
                )
            put(
                archive, f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        put(
            archive,
            "ppt/notesSlides/notesSlide1.xml",
            slide_xml(["Adressbuch der Stadt Flörburg 1912"]),
        )
        if media:
            put(archive, "ppt/media/image1.png", shop_front)
            put(archive, "ppt/media/image2.png", PORTRAIT)
            put(archive, "ppt/media/image4.png", PORTRAIT)
            put(archive, "ppt/media/clip1.mp4", FILM)
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


def deck_alone(tmp_path: Path, **layout) -> Path:
    """A folder that holds the deck and nothing else."""
    folder = tmp_path / "decks"
    folder.mkdir()
    build_deck(folder / DECK, **layout)
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
    to critique. In a paged reading the claim also cites the first line of
    text on a page and that page, as the extraction prompt asks, so its
    source part can be located. It has no conversation capability, so
    chunks see the document inline."""

    model_identifier = "scripted-model"

    def __init__(self, source_sha1: str, person: str, year: int) -> None:
        self.source_sha1, self.person, self.year = source_sha1, person, year

    def complete(self, prompt: str, image_paths=()) -> str:
        if image_paths:
            return record_answer(
                SOURCES["studio-photo-1890s"].text,
                document_class="photograph",
            )
        if "manifest of EVERY distinct entity" in prompt:
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": self.person,
                            "kind": "human",
                            "mention_lines": [1],
                            # the mention record extraction hands over
                            # (ADR-0035 §2)
                            "structure": {
                                "kind": "human",
                                "given": [{"written": self.person.split()[0]}],
                                "surnames": [
                                    {
                                        "written": self.person.split()[1],
                                        "type": "other",
                                    }
                                ],
                            },
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
        # test_pdf_pages imports this module, by way of test_scratch
        from tests.unit.test_pdf_pages import reading_location

        location = reading_location(prompt)
        if location is not None:
            page, line = location
            claim["source"] |= {
                "page_number": page,
                "transcription_excerpt": {"text": line, "language": "en"},
            }
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


def quiet(*args, **kwargs) -> None:
    pass


class BeforeTheWrite(Orchestrator):
    """`ingest run`'s orchestrator with the wiki refusing and the engine
    scripted. The resolve step, which uses the wiki and is not a step
    #128 covers, is replaced: it leaves the batch as it is. Registration,
    part of the write, is not replaced."""

    def __init__(self, folder: Path, state_dir: Path, refusals, **kwargs):
        kwargs.setdefault("exclusions", ExclusionList())
        super().__init__(folder, state_dir, log=quiet, **kwargs)
        self._client = Refusing(refusals, "wiki")
        self._config = Refusing(refusals, "connection settings")

    def _llm(self, role, doc):
        return ScriptedEngine(doc.sha1, *PEOPLE[doc.source.name])

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


def the_deck(orchestrator: Orchestrator):
    (deck,) = [
        doc for doc in orchestrator.documents() if doc.kind == "presentation"
    ]
    return deck


def inventory_files(deck) -> dict[str, tuple[int, bytes]]:
    """The deck's inventory and each file it names: by its path in the
    deck's directory, its inode and bytes, so a file left untouched keeps
    both."""
    inventory = read_inventory(deck.dir)
    assert inventory is not None
    names = [CONTAINER_INVENTORY] + [
        part.file for part in inventory.parts if part.file is not None
    ]
    return {
        name: ((deck.dir / name).stat().st_ino, (deck.dir / name).read_bytes())
        for name in names
    }


def embedded_of(inventory: ContainerInventory) -> dict[str, str]:
    """package part -> the SHA-1 of its bytes, of each resource the deck
    embeds"""
    return {
        part.locator.package_part: part.byte_object.sha1
        for part in inventory.parts
        if part.kind == "embedded_resource"
    }


# -- the steps before the write ----------------------------------------------


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
    first = BeforeTheWrite(folder, tmp_path / "state", refusals)
    first.run()
    kept = inventory_files(the_deck(first))

    # every view stands, and the vision and text-file transcriptions are
    # checked again as they are reused
    again = BeforeTheWrite(folder, tmp_path / "state", refusals)
    assert again.run() == 2
    docs = again.documents()
    for doc in docs:
        assert stages(doc) <= this_run(again, REUSED)
    # the deck's inventory stands too, every file of it untouched
    assert inventory_files(the_deck(again)) == kept

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


def test_every_transcription_names_its_document_by_its_bytes(
    tmp_path, refusals
):
    """Whichever route makes a transcription view (vision, a text file, a
    deck's slide text or a curator's correction), it names its artifact
    by the SHA-1 of the source's bytes (#127): no QID exists before the
    write registers the document (#103)."""
    folder = corpus(tmp_path)

    def views(orchestrator) -> dict[str, tuple[str, dict]]:
        """Each transcribed document's view: the route that made it, and
        the artifact it names, by the kind of document."""
        found = {}
        for doc in orchestrator.documents():
            if doc.kind == "gedcom":
                continue
            text = doc.transcription.read_text()
            assert doc.state.artifact_qid is None
            view = json.loads(text)
            key = {"kind": "artifact", "sha1": sha1(doc.source.read_bytes())}
            found[doc.kind] = (view["engine"].split(":")[0], view["artifact"])
            assert view["artifact"] == key, doc.source.name
        return found

    first = BeforeTheWrite(folder, tmp_path / "state", refusals)
    assert first.run() == 2
    assert {kind: route for kind, (route, _) in views(first).items()} == {
        "media": "llm-vision",
        "presentation": "pptx-adapter",
        "text": "text-file",
    }
    (postcard,) = [doc for doc in first.documents() if doc.kind == "text"]
    postcard.correction.write_text(
        SOURCES["postcard-1938"].text.replace("\n", "\n\n")
    )
    corrected = BeforeTheWrite(folder, tmp_path / "state", refusals)
    assert corrected.run() == 2
    assert views(corrected)["text"][0] == "correction"
    assert refusals.asked == []


def test_serial_extraction_in_parallel_leaves_the_wiki_alone(
    tmp_path, refusals
):
    """Serial mode's extract-all pass (ADR-0019) runs documents in
    parallel, a deck's inventory recorded among them, and asks nothing of
    the wiki."""
    orchestrator = BeforeTheWrite(
        corpus(tmp_path),
        tmp_path / "state",
        refusals,
        serial=True,
        extract_concurrency=2,
    )
    assert orchestrator.run() == 2

    assert refusals.asked == []
    deck = the_deck(orchestrator)
    assert read_inventory(deck.dir).source.sha1 == deck.sha1
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


def test_a_pdf_s_excerpt_leaves_the_wiki_alone(
    tmp_path, refusals, monkeypatch
):
    """A PDF read page by page (#176) and the image it places, extracted as
    a document of its own (#397): neither asks anything of the wiki before
    the write."""
    from tests.unit.test_pdf_pages import LETTER_SHA1, booklet
    from yizkr.pipeline import transcribe
    from yizkr.pipeline.orchestrate import EXCERPT

    # a scripted engine reads the image; no poppler tool runs
    monkeypatch.setattr(transcribe, "require_pdf_renderer", lambda *a: None)
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "booklet.pdf").write_bytes(booklet())

    class Booklet(BeforeTheWrite):
        def _llm(self, role, doc):
            return ScriptedEngine(doc.sha1, *PEOPLE["postcard-1938.txt"])

    orchestrator = Booklet(folder, tmp_path / "state", refusals)
    assert orchestrator.run() == 2
    assert refusals.asked == []
    excerpt = orchestrator._excerpts[LETTER_SHA1]
    assert excerpt.kind == EXCERPT
    assert excerpt.state.status == AWAITING_CLAIMS, excerpt.state.error
    states = run_state(orchestrator.rundir)
    assert {states[stage].status for stage in stages(excerpt)} == {DONE}


def test_a_deck_s_excerpt_leaves_the_wiki_alone(tmp_path, refusals):
    """A deck read slide by slide and the scan it embeds, extracted as a
    document of its own (#502): neither asks anything of the wiki before
    the write, on a first run or a second that reuses their views."""
    from tests.unit.test_deck_excerpts import library_talk
    from tests.unit.test_pdf_pages import LETTER_SHA1
    from yizkr.pipeline.orchestrate import EXCERPT

    folder = tmp_path / "decks"
    folder.mkdir()
    library_talk(folder / "library-talk-2018.pptx")

    class Talk(BeforeTheWrite):
        def _llm(self, role, doc):
            return ScriptedEngine(doc.sha1, *PEOPLE["postcard-1938.txt"])

    for _run in range(2):
        orchestrator = Talk(folder, tmp_path / "state", refusals)
        assert orchestrator.run() == 2
        assert refusals.asked == []
        excerpt = orchestrator._excerpts[LETTER_SHA1]
        assert excerpt.kind == EXCERPT
        assert excerpt.state.status == AWAITING_CLAIMS, excerpt.state.error
        states = run_state(orchestrator.rundir)
        assert {states[stage].status for stage in stages(excerpt)} == {DONE}


def test_adjudication_given_a_roster_asks_nothing_of_the_wiki(refusals):
    """The per-document blocking and adjudication (reconcile.adjudicate)
    take the roster as an input: given one, they read nothing from the
    wiki."""
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
            aliases=["Gustl Löbig"],
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


def test_corpus_clustering_asks_nothing_of_the_wiki(tmp_path, refusals):
    """The scratch driver's clustering stage (#105) clusters the batches
    the steps before it made, and reads no graph, no query service and no
    connection setting to do it: containment and kinship come from the
    manifests alone."""
    orchestrator = BeforeTheWrite(
        corpus(tmp_path), tmp_path / "state", refusals
    )
    assert orchestrator.run() == 2
    (stage,) = [stage for stage in CORPUS_STAGES if stage.name == "clustering"]
    assert not stage.touches_wiki
    logged: list[str] = []
    context = CorpusContext(
        run=orchestrator.rundir,
        documents=tuple(orchestrator.documents()),
        directory=tmp_path / "state" / "corpus" / "clustering",
        env_file=None,
        log=lambda line, **kwargs: logged.append(line),
    )
    stage.run(context)
    # the map it made stands, and is read back
    stage.run(context)
    assert "identity map reused" in logged[-1]
    assert refusals.asked == []

    identity = json.loads(
        (context.directory / "identity-map.json").read_text()
    )
    # the portrait's Eugen and the deck's meet the export's, and the
    # postcard's Klara meets hers: each document's mention has a record
    # (ADR-0035 §2), keys on its given name and surname, and the birth
    # year corroborates it, so each merges with the export's
    merged = sorted(
        tuple(sorted(identity["mentions"][m]["label"] for m in members))
        for entry in identity["clusters"].values()
        if len(members := entry["members"]) > 1
    )
    assert merged == [
        ("Eugen Löbig", "Eugen Löbig", "Eugen Löbig"),
        ("Klara Löbig", "Klara Löbig"),
    ]
    assert identity["review"] == []
    assert identity["summary"]["clusters"]["confident"] == 2


def test_cluster_adjudication_asks_nothing_of_the_wiki(tmp_path, refusals):
    """The clustering stage's adjudication (#106) asks the engine, through
    the LLM cache, about the ambiguous pairs, and merges on its verdicts,
    reading no graph, no query service and no connection setting."""
    from tests.unit.test_adjudication import Engine, context_of, gustav, truths
    from yizkr.pipeline import adjudication

    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    engine = Engine(truths(docs))
    context = context_of(docs, tmp_path, engine)
    adjudication.run_stage(context)
    adjudication.run_stage(context)
    assert len(engine.prompts) == 1
    identity = json.loads(
        (context.directory / "identity-map.json").read_text()
    )
    assert identity["summary"]["clusters"]["adjudicated"] == 1
    assert refusals.asked == []


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


# -- a deck's container inventory --------------------------------------------


def test_a_decks_media_are_inventoried_by_sha1(tmp_path, refusals):
    orchestrator = BeforeTheWrite(
        corpus(tmp_path), tmp_path / "state", refusals
    )
    orchestrator.run()
    assert refusals.asked == []

    deck = the_deck(orchestrator)
    inventory = read_inventory(deck.dir)
    assert inventory.source.sha1 == deck.sha1
    # each resource the deck embeds is a part of its own, by its name in
    # the archive and the SHA-1 of its bytes, the film too; the portrait's
    # two names keep one file of its bytes
    assert embedded_of(inventory) == {
        "ppt/media/image1.png": sha1(SHOP_FRONT),
        "ppt/media/image2.png": sha1(PORTRAIT),
        "ppt/media/image4.png": sha1(PORTRAIT),
        "ppt/media/clip1.mp4": sha1(FILM),
    }
    for part in inventory.parts:
        if part.file is not None:
            assert sha1((deck.dir / part.file).read_bytes()) == (
                part.byte_object.sha1
            )
    # only the slides' relationships name the media, which places none of
    # them on a slide: none is a source of its own, for its reading, an
    # excerpt or the write's registration
    assert inventory.placements == []
    assert project_sources(inventory, deck.dir).sources == {}


def test_a_deck_changed_between_two_runs_is_unpacked_again(tmp_path, refusals):
    """One orchestrator runs twice, and the deck's bytes change between
    the runs: the second run inventories and transcribes the deck as it
    is now, not as the first run unpacked it."""
    folder = deck_alone(tmp_path)
    orchestrator = BeforeTheWrite(folder, tmp_path / "state", refusals)
    orchestrator.run()
    first = the_deck(orchestrator).sha1

    retouched = b"\x89PNG synthetic: the shop front, retouched"
    build_deck(
        folder / DECK,
        shop_front=retouched,
        firm="Inh. Sebastian Löbig, Brunnenring 35",
    )
    orchestrator.run()

    deck = the_deck(orchestrator)
    assert deck.sha1 != first
    inventory = read_inventory(deck.dir)
    assert inventory.source.sha1 == deck.sha1
    assert embedded_of(inventory) == {
        "ppt/media/image1.png": sha1(retouched),
        "ppt/media/image2.png": sha1(PORTRAIT),
        "ppt/media/image4.png": sha1(PORTRAIT),
        "ppt/media/clip1.mp4": sha1(FILM),
    }
    transcription = json.loads(deck.transcription.read_text())["text"]
    assert "Inh. Sebastian Löbig" in transcription
    assert "gegr. 1863" not in transcription
    assert refusals.asked == []


class TestTheRecordIsCurrentWhileItIsWhole:
    """A deck's inventory is made from the deck's bytes on each run, and
    published again exactly where the one in its directory is missing or
    damaged, was made for other bytes than the deck's, or lacks the file
    of a part it names: a regular file holding the bytes its SHA-1 names.
    A whole one stands, every file of it untouched. Never because of what
    state.json says."""

    def run_twice(self, tmp_path, refusals, between=None, **layout):
        folder = deck_alone(tmp_path, **layout)
        first = BeforeTheWrite(folder, tmp_path / "state", refusals)
        first.run()
        deck = the_deck(first)
        recorded = read_inventory(deck.dir)
        assert recorded is not None
        kept = inventory_files(deck)
        if between is not None:
            between(deck)
        second = BeforeTheWrite(folder, tmp_path / "state", refusals)
        second.run()
        assert refusals.asked == []
        return recorded, kept, deck

    @staticmethod
    def portrait(deck) -> Path:
        return deck.dir / "container-parts" / f"{sha1(PORTRAIT)}.png"

    @staticmethod
    def assert_made_again(recorded, deck) -> None:
        """The second run published the inventory again, and each file it
        names is a regular file holding its bytes."""
        again = read_inventory(deck.dir)
        assert again is not None
        assert again.model_dump() == recorded.model_dump()
        for part in again.parts:
            if part.file is not None:
                path = deck.dir / part.file
                assert not path.is_symlink()
                assert sha1(path.read_bytes()) == part.byte_object.sha1

    def test_a_whole_record_stands(self, tmp_path, refusals):
        recorded, kept, deck = self.run_twice(tmp_path, refusals)
        assert inventory_files(deck) == kept
        assert read_inventory(deck.dir).model_dump() == recorded.model_dump()

    def test_a_deleted_record_is_made_again(self, tmp_path, refusals):
        def delete(deck):
            (deck.dir / CONTAINER_INVENTORY).unlink()

        recorded, _, deck = self.run_twice(tmp_path, refusals, delete)
        self.assert_made_again(recorded, deck)

    def test_a_record_that_lost_bytes_is_made_again(self, tmp_path, refusals):
        def lose(deck):
            self.portrait(deck).unlink()

        recorded, _, deck = self.run_twice(tmp_path, refusals, lose)
        self.assert_made_again(recorded, deck)

    def test_a_truncated_medium_is_made_again(self, tmp_path, refusals):
        def truncate(deck):
            self.portrait(deck).write_bytes(b"")

        recorded, _, deck = self.run_twice(tmp_path, refusals, truncate)
        self.assert_made_again(recorded, deck)

    def test_an_overwritten_medium_is_made_again(self, tmp_path, refusals):
        def overwrite(deck):
            self.portrait(deck).write_bytes(b"\x89PNG synthetic: retouched")

        recorded, _, deck = self.run_twice(tmp_path, refusals, overwrite)
        self.assert_made_again(recorded, deck)

    def test_a_medium_linked_from_outside_is_made_again(
        self, tmp_path, refusals
    ):
        # a link to a file outside the folder, even one holding the
        # medium's own bytes, is not the medium's file: the write must
        # never upload through it
        elsewhere = tmp_path / "elsewhere.png"
        elsewhere.write_bytes(PORTRAIT)
        # the bytes a write through the link would leave are these same
        # bytes, so the file's inode and a pinned mtime show it unwritten
        os.utime(elsewhere, ns=(1_000_000_000, 1_000_000_000))
        before = elsewhere.stat()

        def link(deck):
            self.portrait(deck).unlink()
            self.portrait(deck).symlink_to(elsewhere)

        recorded, _, deck = self.run_twice(tmp_path, refusals, link)
        self.assert_made_again(recorded, deck)
        after = elsewhere.stat()  # replaced, not written through
        assert (after.st_ino, after.st_mtime_ns) == (
            before.st_ino,
            before.st_mtime_ns,
        )
        assert elsewhere.read_bytes() == PORTRAIT

    @pytest.mark.skipif(
        not hasattr(os, "geteuid") or os.geteuid() == 0,
        reason="root reads a file whatever its mode",
    )
    def test_an_unreadable_medium_is_made_again(self, tmp_path, refusals):
        def unreadable(deck):
            self.portrait(deck).chmod(0)

        recorded, _, deck = self.run_twice(tmp_path, refusals, unreadable)
        self.assert_made_again(recorded, deck)

    def test_a_record_whose_parts_name_each_others_files_is_made_again(
        self, tmp_path, refusals
    ):
        def swap(deck):
            path = deck.dir / CONTAINER_INVENTORY
            data = json.loads(path.read_text())
            one, other = [
                part
                for part in data["parts"]
                if part["kind"] == "embedded_resource"
                and part["locator"]["package_part"]
                in {"ppt/media/image1.png", "ppt/media/image2.png"}
            ]
            one["file"], other["file"] = other["file"], one["file"]
            path.write_text(json.dumps(data))

        recorded, _, deck = self.run_twice(tmp_path, refusals, swap)
        self.assert_made_again(recorded, deck)

    def test_a_part_whose_file_is_named_for_other_bytes_is_made_again(
        self, tmp_path, refusals
    ):
        # the portrait's bytes, kept under a name that is not their SHA-1
        misnamed = f"container-parts/{'0' * 40}.png"

        def rename(deck):
            self.portrait(deck).rename(deck.dir / misnamed)
            path = deck.dir / CONTAINER_INVENTORY
            data = json.loads(path.read_text())
            for part in data["parts"]:
                if part["file"] == f"container-parts/{sha1(PORTRAIT)}.png":
                    part["file"] = misnamed
            path.write_text(json.dumps(data))

        recorded, _, deck = self.run_twice(tmp_path, refusals, rename)
        self.assert_made_again(recorded, deck)

    def test_a_record_naming_another_file_is_made_again(
        self, tmp_path, refusals
    ):
        # a record whose parts point at a file of the directory that is no
        # part's, which the write must never upload
        def redirect(deck):
            path = deck.dir / CONTAINER_INVENTORY
            data = json.loads(path.read_text())
            for part in data["parts"]:
                if part["file"] is not None:
                    part["file"] = "state.json"
            path.write_text(json.dumps(data))

        recorded, _, deck = self.run_twice(tmp_path, refusals, redirect)
        self.assert_made_again(recorded, deck)

    def test_a_damaged_record_is_made_again(self, tmp_path, refusals):
        def damage(deck):
            (deck.dir / CONTAINER_INVENTORY).write_text('{"format": ')

        recorded, _, deck = self.run_twice(tmp_path, refusals, damage)
        self.assert_made_again(recorded, deck)

    def test_a_record_for_other_deck_bytes_is_made_again(
        self, tmp_path, refusals
    ):
        # whole, with every part's file, but made for another source's
        # bytes, as in a directory two sources have shared
        def another_deck(deck):
            path = deck.dir / CONTAINER_INVENTORY
            data = json.loads(path.read_text())
            data["source"]["sha1"] = sha1(b"another deck")
            path.write_text(json.dumps(data))

        recorded, _, deck = self.run_twice(tmp_path, refusals, another_deck)
        self.assert_made_again(recorded, deck)

    def test_a_directory_without_a_record_gets_one(self, tmp_path, refusals):
        """A deck directory that registered its media and holds no record
        of them: the deck is inventoried, whatever its state.json notes."""

        def registered(deck):
            (deck.dir / CONTAINER_INVENTORY).unlink()
            shutil.rmtree(deck.dir / "container-parts")
            state = DocumentState.load(deck.state_path)
            state.detail = (state.detail or {}) | {
                "derived_media": {"image1.png": "Q501", "image2.png": "Q502"}
            }
            state.save(deck.state_path)

        recorded, _, deck = self.run_twice(tmp_path, refusals, registered)
        self.assert_made_again(recorded, deck)

    def test_a_deck_without_images_is_recorded_once(self, tmp_path, refusals):
        recorded, kept, deck = self.run_twice(tmp_path, refusals, media=False)
        assert embedded_of(recorded) == {}
        assert inventory_files(deck) == kept
        assert not (deck.dir / DERIVED_MEDIA_DIR).exists()


class TestRecordDerivedMedia:
    def test_recording_again_leaves_every_file_untouched(self, tmp_path):
        deck = build_deck(tmp_path / DECK)
        content = unpack_pptx(deck)
        directory = tmp_path / "document"

        def files() -> dict[str, tuple[int, bytes]]:
            return {
                str(path.relative_to(directory)): (
                    path.stat().st_ino,
                    path.read_bytes(),
                )
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            }

        def record() -> DerivedMediaRecord:
            return record_derived_media(
                content,
                source_sha1=sha1(deck.read_bytes()),
                title="Hutmacherei zum Blauen Hut",
                directory=directory,
            )

        first = record()
        kept = files()
        assert record() == first
        assert files() == kept  # the same bytes, and the same files
        inventory = read_inventory(directory)
        assert inventory is not None
        assert inventory.source.sha1 == first.source_sha1
        assert read_derived_media(directory) == first

    def test_a_deck_without_images_records_none(self, tmp_path):
        content = PresentationContent(
            slides=[Slide(number=1, text="Hutmacherei zum Blauen Hut")],
            media=[],
            skipped_media={".mp4": 1},
        )
        record = record_derived_media(
            content,
            source_sha1=sha1(b"a deck"),
            title="Hutmacherei zum Blauen Hut",
            directory=tmp_path,
        )
        assert record.media == {}
        assert read_derived_media(tmp_path) == record
        assert not (tmp_path / DERIVED_MEDIA_DIR).exists()
