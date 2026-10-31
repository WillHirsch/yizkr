"""PDFs read page by page (#176): the transcription route, the images a
PDF places within its pages, the views, and the LLM cache's keys.

A PDF with usable native text or readable image sources is read page by
page. Each original image is read once under its own owner; a page showing
only that image keeps a marker without duplicating its reading or claims.
Other vision pages are rendered. A PDF with no usable text or readable
image source is read through a checked bitmap-only representation. Cache
reuse follows the submitted bytes. The orchestrator uses
wiring (tests/unit/test_llmcache.py's CachedRun), and a scripted engine
reads each file a vision call gives it. Every PDF is fictional
(tests/fixtures/pdfs.py), its texts the universe's (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs, records
from tests.fixtures.records import page_text, record_answer, record_page
from tests.unit.test_llmcache import (
    CachedRun,
    Engine,
    pipeline_answers,
)
from tests.unit.test_orchestrate import ScriptedOrchestrator, quiet, this_run
from tests.unit.test_scratch import TEXT, Corpus, a_run, fakes  # noqa: F401
from tests.unit.test_scratch_estimate import (  # noqa: F401
    Quiet,
    answer_with,
    engine,
    entries,
    estimate_of,
    per_stage,
    runs_dir,
)
from tests.unit.test_scratch_estimate import run as run_scratch
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import estimate, llm, transcribe, transcription_check
from yizkr.pipeline.estimate import Estimator, StageCalls
from yizkr.pipeline.language import detect_language
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    FAILED,
    PAGED,
    TRANSCRIPTION_VOLATILE,
    DocumentRun,
    ExcerptRun,
    Orchestrator,
)
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    derived_transcription,
)
from yizkr.pipeline.rundir import RunDir, RunSettingsConflict
from yizkr.pipeline.scratch import EXIT_OK, LATEST, runs_in
from yizkr.pipeline.stages import content_hash, sha1_file
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import (
    TRANSCRIPTION_PROMPT,
    PageProvenance,
    PdfPageTranscriber,
    TranscribedPage,
    Transcription,
)


def saved_sources(directory):
    from yizkr.pipeline.container_inventory import read_inventory
    from yizkr.pipeline.container_sources import project_sources

    inventory = read_inventory(directory)
    assert inventory is not None
    return project_sources(inventory, directory)


SOURCES = {source.key: source for source in u.SOURCES}
HISTORY = SOURCES["family-history-2008"].text
FIRST, SECOND = (
    part.strip() for part in re.split(r"\[page \d\] ", HISTORY) if part
)
EXTRACT = SOURCES["birth-extract-1938"].text
EXCERPT = SOURCES["letter-excerpt-1939"].text
BOOKLET = SOURCES["family-booklet-2011"].text
TITLE = "Our Family from Germany. Compiled by Nicholas Lobig. Stupfield, 2011."
SOFTWARE = (
    "All translations in this booklet were done with translation "
    "software; any mistakes are mine."
)
CAPTION = "Wastl to Klara, July 1939."
TRANSLATION = (
    "Translation: Flörburg, Brunnenring 35, 28 July 1939. Dear Pünktchen! "
    "Gustl finally has his visa. Hilde is sewing him shirts for the "
    "journey. Rahel now lives at Angerring 71."
)
COMMENT = "Gustl was Ursula's younger brother."
# the image of the letter's top the booklet sets beside its translation
LETTER_IMAGE = pdfs.gray(300, 120)
LETTER_SHA1 = hashlib.sha1(png(300, 120, 1, LETTER_IMAGE.data)).hexdigest()
# an engine's refusal, which the transcription check fails
REFUSAL = "I'm sorry, but I can't transcribe this document."


class Raw(str):
    """A reading the scripted engine answers as it stands, rather than as
    the record of a page holding it."""


TEXT_LAYER = "pdf-pages:pdf-text-layer/1"


@pytest.fixture(autouse=True)
def renderer(monkeypatch):
    """The PDF renderer on the host, as far as a run's preflight can tell.
    A run with a PDF refuses to start without poppler-utils, which reads a
    PDF whole for the claude-code engine; here a scripted engine reads
    every file, so no poppler tool runs (test_orchestrate.py's
    TestPreflight holds the preflight itself)."""
    monkeypatch.setattr(transcribe, "require_pdf_renderer", lambda *a: None)


def typed(*paragraphs: str, **options) -> pdfs.Page:
    return pdfs.Page().text(pdfs.lines(*paragraphs), **options)


def vision_page() -> pdfs.Page:
    """Native text whose font has no usable Unicode map needs rendering.

    It holds no original embedded image, so its reading belongs to the PDF.
    """
    return pdfs.Page().text(pdfs.lines(EXTRACT), font=pdfs.GARBLED)


def history(second: pdfs.Page | None = None) -> bytes:
    """The two typed pages of the 2008 family history; a copy may have its
    second page as `second` instead (rendered, say)."""
    return pdfs.pdf([typed(FIRST), second or typed(SECOND)])


def booklet(image: pdfs.Image = LETTER_IMAGE) -> bytes:
    """Our Family from Germany: two typed pages, three empty ones, and
    page 6, with the image of the letter's top above its caption, the
    translation and the commentary."""
    six = pdfs.Page().image(image, 72, 520, 300, 200)
    six.text(pdfs.lines(CAPTION, TRANSLATION, COMMENT), y=500)
    return pdfs.pdf(
        [typed(TITLE), typed(SOFTWARE), pdfs.Page(), pdfs.Page(), pdfs.Page()]
        + [six]
    )


def extract() -> bytes:
    """The birth register extract with an unreadable native text layer."""
    return pdfs.pdf([vision_page()])


def reading_location(prompt: str, page: str | None = None):
    """A real page and text line from the scripted engine's input reading."""
    current = None
    for line in re.findall(r"^\d+: (.*)$", prompt, re.M):
        marker = re.match(r"\[(?:Page|Slide) (\d+)\]", line)
        if marker:
            current = marker[1]
        elif (
            current is not None
            and line.strip()
            and not line.startswith("[")
            and (page is None or page == current)
        ):
            return current, line
    return None


def located_pipeline_answers(kind, prompt, opening):
    """The canned pipeline claim with support present in its input reading.

    These answers exercise pipeline wiring, not model interpretation quality.
    Unpaged fixtures retain their existing response shape.
    """
    answer = pipeline_answers(kind, prompt, opening)
    location = reading_location(opening or prompt)
    if location is None:
        return answer
    page, quote = location
    lines = []
    for line in answer.splitlines():
        value = json.loads(line) if line.startswith("{") else None
        if isinstance(value, dict) and isinstance(value.get("source"), dict):
            value["source"] |= {
                "page_number": page,
                "transcription_excerpt": {"text": quote, "language": "en"},
            }
            line = json.dumps(value)
        lines.append(line)
    return "\n".join(lines)


class Reader(Engine):
    """The scripted engine, reading the file each vision call gives it
    from `readings`, by the file's name or stem: a whole PDF by its name, a
    page by ``page-<n>``, and an image by its SHA-1. A reading given as a
    list is given in turn, its last one from then on. A reading is the
    engine's answer, the transcription record, or text, which it answers
    as the record of one page holding it. `read` logs each file
    read, by name, with the SHA-1 of its bytes."""

    def __init__(self, readings: dict[str, str | list[str]], **kwargs):
        super().__init__(located_pipeline_answers, **kwargs)
        self.readings = {
            name: list(reading) if isinstance(reading, list) else [reading]
            for name, reading in readings.items()
        }
        self.read: list[tuple[str, str]] = []

    def complete(self, prompt, image_paths=()):
        if prompt != TRANSCRIPTION_PROMPT:
            return super().complete(prompt, image_paths)
        self._log("vision", prompt)
        (path,) = (Path(path) for path in image_paths)
        self.read.append((path.name, sha1_file(path)))
        readings = self.readings.get(path.name) or self.readings[path.stem]
        reading = readings.pop(0) if len(readings) > 1 else readings[0]
        # a reading given as text is the record of one page holding it
        if isinstance(reading, Raw) or reading.startswith("{"):
            return str(reading)
        language, _confidence = detect_language(reading)
        return record_answer(reading, languages=[language or "en"])

    def names(self) -> list[str]:
        return [name for name, _sha1 in self.read]


class RefusingReader(Reader):
    """The reader with no engine behind it: any call fails the test."""

    def _log(self, kind, prompt):
        raise AssertionError(f"the engine was asked: {kind} {prompt[:60]!r}")


def folder(tmp_path: Path, **documents: bytes) -> Path:
    """A corpus folder holding `documents`, each a PDF named for its
    keyword, dashes for its underscores (`family_history_2008` is
    family-history-2008.pdf)."""
    path = tmp_path / "corpus"
    path.mkdir(exist_ok=True)
    for name, data in documents.items():
        (path / f"{name.replace('_', '-')}.pdf").write_bytes(data)
    return path


def run(
    tmp_path: Path,
    corpus: Path,
    reader: Engine,
    *,
    state: str = "state",
    **options,
) -> CachedRun:
    options.setdefault("log", quiet)
    # no identity proposals, so that each document goes on to its claims
    options.setdefault("proposals", 0)
    orchestrator = CachedRun(
        corpus, tmp_path / state, engine=reader, **options
    )
    orchestrator.run()
    return orchestrator


def document(orchestrator: CachedRun, name: str) -> DocumentRun:
    return next(
        doc for doc in orchestrator.documents() if doc.source.name == name
    )


def transcription_of(doc: DocumentRun) -> Transcription:
    return Transcription.model_validate_json(doc.transcription.read_text())


def excerpt_of(orchestrator: Orchestrator, sha1: str) -> ExcerptRun:
    """The excerpt the orchestrator's run found for an image."""
    return orchestrator._excerpts[sha1]


def page_methods(transcription: Transcription) -> list[tuple[int, str]]:
    return [
        (page.page, page.provenance.method) for page in transcription.pages
    ]


class TestTheRoute:
    def test_a_born_digital_pdf_reads_from_its_text_layer(self, tmp_path):
        """No model reads it: each page's text is its text layer, after
        the page's marker, and the provenance says so page by page."""
        corpus = folder(tmp_path, family_history_2008=history())
        reader = Reader({})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "family-history-2008.pdf")
        assert doc.state.status == AWAITING_CLAIMS
        assert reader.read == []
        transcription = transcription_of(doc)
        first = "\n".join(pdfs.lines(FIRST))
        second = "\n".join(pdfs.lines(SECOND))
        assert transcription.text == (
            f"[Page 1]\n{first}\n\n[Page 2]\n{second}"
        )
        assert transcription.engine == TEXT_LAYER
        assert page_methods(transcription) == [
            (1, "text-layer"),
            (2, "text-layer"),
        ]
        assert [
            transcription.page_text(page) for page in transcription.pages
        ] == [first, second]
        for page in transcription.pages:
            assert page.provenance.engine.startswith(
                "pdf-text-layer/1 (pypdfium2 "
            )
        # its claims cite it, by the SHA-1 of its bytes
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert batch.claims
        assert {claim.source.artifact.sha1 for claim in batch.claims} == {
            doc.sha1
        }

    def test_a_pdf_named_in_capitals_is_read_page_by_page(self, tmp_path):
        """Its suffix, in whatever case, makes a file a PDF."""
        corpus = folder(tmp_path)
        (corpus / "FAMILY-HISTORY-2008.PDF").write_bytes(history())
        reader = Reader({})
        doc = document(
            run(tmp_path, corpus, reader), "FAMILY-HISTORY-2008.PDF"
        )
        assert reader.read == []
        assert page_methods(transcription_of(doc)) == [
            (1, "text-layer"),
            (2, "text-layer"),
        ]

    def test_the_language_is_found_in_the_pages_text_alone(
        self, tmp_path, monkeypatch
    ):
        """The page and image markers are no part of the text the
        language is found in: only what was read from the pages is."""
        given = []
        resolve = transcribe.resolve_language

        def resolving(text, *args, **kwargs):
            given.append(text)
            return resolve(text, *args, **kwargs)

        monkeypatch.setattr(transcribe, "resolve_language", resolving)
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        transcription = transcription_of(doc)
        read = [transcription.page_text(page) for page in transcription.pages]
        assert "\n\n".join(text for text in read if text) in given
        assert not any("[Page" in text for text in given)

    def test_a_page_s_reading_stands_without_blank_lines_around_it(
        self, tmp_path
    ):
        """What the engine reads of a page is the page's text, without the
        blank lines it may begin or end with, so the page's markers and
        the next page's stand where they always do."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reader = Reader({"page-2": f"\n\n{EXTRACT.strip()}\n\n"})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        transcription = transcription_of(doc)
        assert transcription.page_text(transcription.pages[1]) == (
            EXTRACT.strip()
        )
        assert transcription.text.endswith(f"[Page 2]\n{EXTRACT.strip()}")

    def test_a_page_s_unread_annotations_are_counted(self, tmp_path):
        """A text-layer page's annotations that show no text are left
        unread, and its provenance counts them for the run report."""
        page = typed(TITLE).annotate("/Subtype /Stamp /Rect [400 60 540 120]")
        corpus = folder(tmp_path, notes=pdfs.pdf([page, typed(TITLE)]))
        doc = document(run(tmp_path, corpus, Reader({})), "notes.pdf")
        transcription = transcription_of(doc)
        assert [
            (page.provenance.method, page.provenance.annotations)
            for page in transcription.pages
        ] == [("text-layer", 1), ("text-layer", 0)]

    def test_a_pdf_without_usable_text_or_images_is_read_whole(self, tmp_path):
        """A PDF none of whose pages reads from its text layer is read by
        one vision call over a checked, rendered PDF. Its source identity
        remains the original PDF; its pages are read by that call."""
        corpus = folder(tmp_path, birth_extract_1938=extract())
        reader = Reader({"birth-extract-1938.pdf": EXTRACT})
        doc = document(run(tmp_path, corpus, reader), "birth-extract-1938.pdf")
        assert reader.names() == ["birth-extract-1938.pdf"]
        assert reader.read[0][1] != doc.sha1
        stored = json.loads(doc.transcription.read_text())
        assert stored["artifact"]["sha1"] == doc.sha1
        assert stored["input_representation"] == "rendered-pdf-v1"
        assert stored["text"] == page_text(EXTRACT)
        assert stored["engine"] == "llm-vision:scripted/model-a"
        assert [
            (page["page"], page["status"], page["provenance"]["method"])
            for page in stored["pages"]
        ] == [(1, "transcribed", "vision")]
        assert not (doc.dir / DERIVED_MEDIA).exists()

    def test_repeated_scans_read_and_extract_only_the_original_image(
        self, tmp_path
    ):
        image_sha1 = hashlib.sha1(pdfs.JPEG).hexdigest()
        pages = [
            pdfs.Page()
            .image(pdfs.jpeg(), 40, 60, 500, 700)
            .text(pdfs.lines(EXTRACT), mode=pdfs.INVISIBLE)
            for _ in range(2)
        ]
        corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf(pages))
        reader = Reader({image_sha1: EXTRACT})
        first = run(tmp_path, corpus, reader)
        doc = document(first, "birth-extract-1938.pdf")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert reader.names() == [f"{image_sha1}.jpg"]
        transcription = transcription_of(doc)
        assert page_methods(transcription) == [
            (1, "image-source"),
            (2, "image-source"),
        ]
        assert all(
            not transcription.page_text(page) for page in transcription.pages
        )
        assert (
            ClaimBatch.model_validate_json(doc.batch.read_text()).claims == []
        )
        excerpt = excerpt_of(first, image_sha1)
        batch = ClaimBatch.model_validate_json(excerpt.batch.read_text())
        assert batch.claims
        assert {claim.source.artifact.sha1 for claim in batch.claims} == {
            image_sha1
        }
        assert all(
            claim.source.support is not None
            and claim.source.support.locator.kind == "image"
            and claim.source.support.placements
            and all(
                placement.locator.position in {1, 2}
                for placement in claim.source.support.placements
            )
            for claim in batch.claims
        )
        assert saved_sources(doc.dir).sources[image_sha1].slides == [1, 2]
        before = excerpt.batch.read_bytes()
        again = run(tmp_path, corpus, RefusingReader({}))
        assert excerpt_of(again, image_sha1).batch.read_bytes() == before

    def test_a_mixed_pdf_reads_each_page_by_its_method(self, tmp_path):
        """Usable native text needs no call; unusable text is rendered."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reader = Reader({"page-2": SECOND})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        assert reader.names() == ["page-2.png"]
        transcription = transcription_of(doc)
        assert page_methods(transcription) == [
            (1, "text-layer"),
            (2, "vision"),
        ]
        assert transcription.pages[1].provenance.engine == (
            "llm-vision:scripted/model-a"
        )
        assert transcription.page_text(transcription.pages[1]) == SECOND
        first = "\n".join(pdfs.lines(FIRST))
        assert transcription.text == f"[Page 1]\n{first}\n\n[Page 2]\n{SECOND}"
        # unusable native text is replaced by the rendered reading
        assert "Standesamt" not in transcription.text

    def test_each_page_carries_the_record_s_fields(self, tmp_path):
        """A page read by vision takes the fields of its reading's record
        (#389); a page read from its text layer is transcribed, with no
        printed number, languages or notes, since no engine read it."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reading = json.dumps(
            {
                "document_class": "unclassified",
                "pages": [
                    record_page(
                        1,
                        SECOND,
                        printed_page="2",
                        notes="a coffee ring in the margin",
                    )
                ],
            }
        )
        reader = Reader({"page-2": reading})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        transcription = transcription_of(doc)
        typed_page, rendered_page = transcription.pages
        assert (
            typed_page.status,
            typed_page.printed_page,
            typed_page.languages,
            typed_page.notes,
        ) == ("transcribed", None, [], "")
        assert (
            rendered_page.status,
            rendered_page.printed_page,
            rendered_page.languages,
            rendered_page.notes,
        ) == ("transcribed", "2", ["en"], "a coffee ring in the margin")
        assert transcription.printed_pages() == {2: "2"}
        # no call saw the PDF whole, so it has no class
        assert transcription.document_class is None

    def test_a_page_with_nothing_to_read_is_blank(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        pages = transcription_of(doc).pages
        assert [(p.provenance.method, p.status) for p in pages[1:5]] == [
            ("text-layer", "transcribed"),
            ("none", "blank"),
            ("none", "blank"),
            ("none", "blank"),
        ]

    def test_a_flagged_vision_page_flags_the_pdf(self, tmp_path):
        """The flag of a vision page's reading, here from its notes, is
        the transcription's."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reading = json.dumps(
            {
                "document_class": "unclassified",
                "pages": [
                    record_page(1, SECOND, notes="No image was provided.")
                ],
            }
        )
        reader = Reader({"page-2": reading})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        transcription = transcription_of(doc)
        assert transcription.flag == "media-missing"
        assert transcription.pages[1].notes == "No image was provided."
        # reused, the page's notes flag it again
        again = run(tmp_path, corpus, reader)
        assert set_asides(again) == []
        assert again._transcription(doc).flag == "media-missing"
        assert flag_alerts(again) == ["transcription-flag"]

    def test_a_garbled_text_layer_is_read_by_vision(self, tmp_path):
        """The second page's font extracts as private-use characters: the
        page is read by vision, from a rendering of it."""
        garbled = typed(SECOND, font=pdfs.GARBLED)
        corpus = folder(tmp_path, family_history=history(garbled))
        reader = Reader({"page-2": SECOND})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        assert reader.names() == ["page-2.png"]
        transcription = transcription_of(doc)
        assert page_methods(transcription) == [
            (1, "text-layer"),
            (2, "vision"),
        ]
        assert not any("\ue000" <= ch <= "\uf8ff" for ch in transcription.text)

    def test_pages_with_nothing_to_read_are_marked_and_not_read(
        self, tmp_path
    ):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        transcription = transcription_of(doc)
        assert page_methods(transcription)[2:5] == [
            (3, "none"),
            (4, "none"),
            (5, "none"),
        ]
        assert "[Page 4]\n\n[Page 5]" in transcription.text
        assert reader.names() == [f"{LETTER_SHA1}.png"]

    def test_a_page_read_by_vision_that_fails_the_check_fails_it(
        self, tmp_path
    ):
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reader = Reader({"page-2": Raw(REFUSAL)})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "family-history.pdf")
        assert doc.state.status == FAILED
        assert "transcription check" in doc.state.error
        assert not doc.transcription.exists()

    def test_a_pdf_read_from_its_text_layer_starts_no_engine(
        self, tmp_path, monkeypatch
    ):
        """The transcribe role's engine starts only for a page or an image
        to read by vision: the run manifest records no client for it."""

        class Starting(ScriptedOrchestrator):
            _llm = Orchestrator._llm

        monkeypatch.setattr(llm, "make_client", lambda *a, **k: Reader({}))
        corpus = folder(tmp_path, family_history_2008=history())
        orchestrator = Starting(
            corpus, tmp_path / "state", proposals=0, log=quiet
        )
        orchestrator.run()
        roles = orchestrator.rundir.manifest()["roles"]
        assert roles["transcribe"]["client"] is None
        assert roles["extract"]["client"] is not None

    def test_the_reading_s_scratch_folder_is_removed(
        self, tmp_path, monkeypatch
    ):
        """The images wait in a scratch folder while the PDF is read, and
        it is gone once the stage has kept them."""
        scratch = tmp_path / "tmp"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        corpus = folder(tmp_path, booklet=booklet())
        # the orchestrator keeps each view it made in the run, so what
        # rides on one lives as long as it does
        orchestrator = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        assert not list(scratch.glob("yizkr-pdf-*"))
        assert transcription_of(document(orchestrator, "booklet.pdf"))

    def test_a_page_that_hides_text_is_read_by_vision(self, tmp_path):
        """A line a black box covers is never read from the text layer, as
        the owner decided: the page goes to vision, which reads what the
        page shows, and the line reaches no transcription."""
        hidden = "Rahel wohnt jetzt Angerring 11."
        page = pdfs.Page().text([hidden, "Gustl hat endlich sein Visum."])
        page.rectangle(60, 766, 480, 14, gray=0)
        corpus = folder(tmp_path, letter=pdfs.pdf([typed(TITLE), page]))
        reader = Reader(
            {"page-2": "[redacted]\nGustl hat endlich sein Visum."}
        )
        doc = document(run(tmp_path, corpus, reader), "letter.pdf")
        transcription = transcription_of(doc)
        assert page_methods(transcription) == [
            (1, "text-layer"),
            (2, "vision"),
        ]
        assert reader.names() == ["page-2.png"]
        assert "Angerring" not in transcription.text

    def test_an_uninspectable_pdf_is_refused_before_vision(self, tmp_path):
        corpus = folder(tmp_path, scan=b"%PDF-1.4 fictional")
        reader = Reader({"scan.pdf": EXTRACT})
        doc = document(run(tmp_path, corpus, reader), "scan.pdf")
        assert doc.state.status == FAILED
        assert reader.names() == []
        assert not doc.transcription.exists()
        assert "exclusions" in doc.state.error


class TestImages:
    """An image a compilation places within a page is a witness of its
    own (#178): it is read on its own, its transcription names it, and
    nothing read from it is the compilation's text."""

    def test_an_image_is_read_on_its_own(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        assert reader.read == [(f"{LETTER_SHA1}.png", LETTER_SHA1)]
        # the booklet's transcription holds its own text, and marks where
        # the image sits, by its SHA-1
        transcription = transcription_of(doc)
        own = "\n".join(pdfs.lines(CAPTION, TRANSLATION, COMMENT))
        assert transcription.text.endswith(
            f"[Page 6]\n[Page 6, image 1: sha1:{LETTER_SHA1}]\n{own}"
        )
        assert "Angerring 11" not in transcription.text
        assert "Angerring 71" in transcription.text
        assert transcription.pages[5].images == [LETTER_SHA1]
        assert transcription.artifact.sha1 == doc.sha1
        # the image's own transcription names the image
        kept = Transcription.model_validate_json(
            derived_transcription(doc.dir, LETTER_SHA1).read_text()
        )
        assert kept.artifact.sha1 == LETTER_SHA1
        assert kept.text == page_text(EXCERPT)
        assert kept.language == "de"
        # and the image is recorded as the booklet's derived medium
        record = saved_sources(doc.dir)
        assert record.inventory.source.sha1 == doc.sha1
        medium = record.sources[LETTER_SHA1]
        assert medium.names == ["page 6, image 1"]
        assert medium.slides == [6]
        assert medium.file == f"representations/{LETTER_SHA1}.png"

    def test_the_claims_read_from_the_booklet_cite_it_alone(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert {claim.source.artifact.sha1 for claim in batch.claims} == {
            doc.sha1
        }
        # no prompt of its extraction holds what was read from the image:
        # the prompts that hold the translation are the booklet's
        extraction = [
            prompt
            for kind, prompt in reader.calls
            if kind != "vision" and "Angerring 71" in prompt
        ]
        assert extraction
        assert not any("Angerring 11" in prompt for prompt in extraction)

    def test_one_image_placed_twice_is_read_once(self, tmp_path):
        page = typed(TITLE).image(SHARED_LETTER_IMAGE, 72, 400, 300, 120)
        data = pdfs.pdf(
            [
                page,
                typed(SOFTWARE).image(SHARED_LETTER_IMAGE, 72, 400, 300, 120),
            ]
        )
        corpus = folder(tmp_path, booklet=data)
        reader = Reader({SHARED_LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        assert reader.names() == [f"{SHARED_LETTER_SHA1}.jpg"]
        medium = saved_sources(doc.dir).sources[SHARED_LETTER_SHA1]
        assert medium.names == ["page 1, image 1", "page 2, image 1"]
        assert medium.slides == [1, 2]

    def test_an_image_the_never_ingest_list_names_is_never_read(
        self, tmp_path
    ):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({})
        doc = document(
            run(
                tmp_path,
                corpus,
                reader,
                exclusions=ExclusionList.parse(f"{LETTER_SHA1}\n"),
            ),
            "booklet.pdf",
        )
        assert doc.state.status == AWAITING_CLAIMS
        assert reader.read == []
        transcription = transcription_of(doc)
        assert LETTER_SHA1 not in transcription.text
        assert transcription.pages[5].images == []
        assert saved_sources(doc.dir).sources == {}
        assert not (doc.dir / "derived-media").exists()

    def test_an_image_whose_reading_fails_needs_attention(self, tmp_path):
        """The image is left without a transcription of its own, and says
        so in the stage's attention; the booklet's own text goes on to its
        batch."""
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: Raw(REFUSAL)})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "booklet.pdf")
        assert doc.state.status == AWAITING_CLAIMS
        (done,) = [
            event
            for event in this_run(orchestrator, "done")
            if event["stage"] == "transcription"
        ]
        (reason,) = done["attention"]
        assert reason.startswith(
            "image reading failed the check: page 6, image 1 (sha1 "
            f"{LETTER_SHA1[:12]})"
        )
        assert "(not-a-record)" in reason
        assert not derived_transcription(doc.dir, LETTER_SHA1).exists()
        assert LETTER_SHA1 in saved_sources(doc.dir).sources

    def test_an_image_that_cannot_be_read_needs_attention(self, tmp_path):
        broken = pdfs.Image(
            "/Width 100 /Height 100 /ColorSpace /DeviceGray "
            "/BitsPerComponent 8 /Filter /Fictional",
            b"no image stream PDFium can read",
        )
        corpus = folder(tmp_path, booklet=booklet(broken))
        orchestrator = run(tmp_path, corpus, Reader({}))
        doc = document(orchestrator, "booklet.pdf")
        assert doc.state.status == AWAITING_CLAIMS
        (done,) = [
            event
            for event in this_run(orchestrator, "done")
            if event["stage"] == "transcription"
        ]
        (attention,) = done["attention"]
        assert attention.startswith("image not read on its own:")
        assert "PDFium image preparation failed" in attention

    def test_nothing_is_written_until_the_stage_keeps_it(self, tmp_path):
        """The route writes nothing into the document's directory, so a
        caller that only counts calls (the call estimate) keeps nothing:
        the image and its reading wait in a scratch folder of their own,
        which the reading removes when it is closed."""
        corpus = folder(tmp_path, booklet=booklet())
        orchestrator = CachedRun(
            corpus,
            tmp_path / "state",
            engine=Reader({LETTER_SHA1: EXCERPT}),
            log=quiet,
            exclusions=ExclusionList(),
        )
        (doc,) = orchestrator.documents()
        with orchestrator._transcribe(doc, PAGED) as result:
            assert not doc.dir.exists()
            assert result.transcription.reading is None
            (image,) = result.products.images
            assert image.path.read_bytes() == png(
                300, 120, 1, LETTER_IMAGE.data
            )
            assert image.transcription.text == page_text(EXCERPT)
        assert not image.path.exists()


class TestAScratchRun:
    def test_a_scratch_run_reads_a_pdf_page_by_page(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        """The driver's transcription stage is the orchestrator's: the
        booklet's own text from its text layer, and its image read on its
        own and kept beside it, before the extraction and the critique."""
        corpus = Corpus(tmp_path / "corpus")
        (corpus.folder / "booklet.pdf").write_bytes(booklet())
        # the engine reads the image as the letter, and tells the
        # booklet's prompts by the translation's lines
        corpus.texts["booklet.pdf"] = "\n".join(
            pdfs.lines(CAPTION, TRANSLATION, COMMENT)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        assert ("vision", f"{LETTER_SHA1}.png") in fakes.calls
        assert ("vision", "booklet.pdf") not in fakes.calls
        # the folder's documents are the booklet alone; its image is an
        # excerpt the booklet's transcription found
        (doc,) = driver.documents()
        transcription = transcription_of(doc)
        assert transcription.engine == TEXT_LAYER
        assert transcription.pages[5].images == [LETTER_SHA1]
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        assert json.loads(kept.read_text())["text"] == page_text(EXCERPT)
        assert doc.critique.exists()

    def test_the_call_estimate_counts_it_and_writes_nothing(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The estimate takes the transcription stage through the same
        code, and so counts the image's call, and writes nothing: the
        reading waits in a scratch folder the estimate never keeps."""
        corpus = Corpus(tmp_path / "corpus")
        (corpus.folder / "booklet.pdf").write_bytes(booklet())
        corpus.texts["booklet.pdf"] = TRANSLATION
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        counted = estimate_of(tmp_path, corpus, engine)
        assert per_stage(counted, "known")["transcription"] == 1
        assert not runs_dir(tmp_path).exists()
        assert engine.calls == []

    def test_the_call_estimate_counts_every_page_and_image(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A PDF read page by page makes a call for each image and each
        page vision reads: the estimate counts every one, the cache unable
        to answer any, and leaves the extraction after them to the
        engine."""
        three = pdfs.Page()
        for shade, x in ((1, 72), (2, 300)):
            three.image(pdfs.gray(300, 120, shade=shade), x, 600, 200, 80)
        three.image(pdfs.gray(300, 120, shade=3), 72, 480, 200, 80)
        three.text(pdfs.lines(CAPTION, TRANSLATION), y=400)
        scans = [
            pdfs.Page().image(
                pdfs.gray(60, 80, shade=shade), 0, 0, pdfs.WIDTH, pdfs.HEIGHT
            )
            for shade in (4, 5, 6)
        ]
        corpus = Corpus(tmp_path / "corpus")
        (corpus.folder / "booklet.pdf").write_bytes(
            pdfs.pdf([typed(TITLE), three])
        )
        # its suffix in capitals, as a PDF's may be
        (corpus.folder / "history.PDF").write_bytes(
            pdfs.pdf([typed(FIRST), *scans])
        )
        corpus.texts["booklet.pdf"] = TRANSLATION
        corpus.texts["history.PDF"] = FIRST
        counted = estimate_of(tmp_path, corpus, engine)
        assert per_stage(counted, "known")["transcription"] == 6
        assert per_stage(counted, "known").get("extraction", 0) == 0
        assert per_stage(counted, "estimated")["extraction"] > 0
        assert engine.calls == []

    def test_the_call_estimate_keeps_nothing_of_a_reading(
        self,
        tmp_path,
        engine,  # noqa: F811
        monkeypatch,
    ):
        """A reading the cache answers whole is closed as the estimate
        counts it, and its scratch folder goes, however long the reading
        itself is kept."""
        corpus = Corpus(tmp_path / "corpus")
        (corpus.folder / "booklet.pdf").write_bytes(booklet())
        corpus.texts["booklet.pdf"] = "\n".join(
            pdfs.lines(CAPTION, TRANSLATION, COMMENT)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        run_scratch(tmp_path, corpus, engine)
        scratch = tmp_path / "tmp"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        # every reading made is kept here, so that its folder goes only if
        # it is closed, never as the reading is let go
        made = []

        class Kept(transcribe.PagedReading):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)

        monkeypatch.setattr(transcribe, "PagedReading", Kept)
        counted = estimate_of(tmp_path, corpus, engine)
        assert per_stage(counted, "hits")["transcription"] == 1
        assert len(made) == 1
        assert not list(scratch.glob("yizkr-pdf-*"))


# The two compilations share these original JPEG bytes, not merely a decoded
# PNG. Original-media identity establishes their common letter owner.
SHARED_LETTER_IMAGE = pdfs.jpeg()
SHARED_LETTER_SHA1 = hashlib.sha1(SHARED_LETTER_IMAGE.data).hexdigest()


def album() -> bytes:
    """A second compilation: a typed title page, and a page that places
    the letter's top, as the booklet does, beside two other images."""
    page = pdfs.Page().image(SHARED_LETTER_IMAGE, 72, 600, 200, 80)
    for shade, x in ((1, 300), (2, 72)):
        page.image(pdfs.gray(300, 120, shade=shade), x, 480, 200, 80)
    page.text(pdfs.lines(CAPTION, TRANSLATION), y=400)
    return pdfs.pdf([typed(TITLE), page])


def gray_sha1(shade: int) -> str:
    return hashlib.sha1(
        png(300, 120, 1, pdfs.gray(300, 120, shade=shade).data)
    ).hexdigest()


def compilations(tmp_path: Path) -> Corpus:
    """The booklet and the album, the engine reading each image as the
    letter excerpt and each compilation as its translation."""
    corpus = Corpus(tmp_path / "corpus")
    (corpus.folder / "booklet.pdf").write_bytes(booklet(SHARED_LETTER_IMAGE))
    corpus.texts["booklet.pdf"] = "\n".join(
        pdfs.lines(CAPTION, TRANSLATION, COMMENT)
    )
    corpus.texts[f"{SHARED_LETTER_SHA1}.jpg"] = EXCERPT
    (corpus.folder / "album.pdf").write_bytes(album())
    corpus.texts["album.pdf"] = "\n".join(pdfs.lines(TITLE))
    for sha1 in (gray_sha1(1), gray_sha1(2)):
        corpus.texts[f"{sha1}.png"] = EXCERPT
    return corpus


def the_booklet(tmp_path: Path) -> Corpus:
    """The booklet alone, its image read as the letter excerpt."""
    corpus = Corpus(tmp_path / "corpus")
    (corpus.folder / "booklet.pdf").write_bytes(booklet())
    corpus.texts["booklet.pdf"] = "\n".join(
        pdfs.lines(CAPTION, TRANSLATION, COMMENT)
    )
    corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
    return corpus


def excerpt_line(counted: estimate.Estimate) -> str | None:
    lines = [
        line
        for line in estimate.describe(counted)
        if "more for excerpts" in line
    ]
    assert len(lines) <= 1
    return lines[0] if lines else None


def made_run(
    runs: Path, name: str, documents: list | dict, events: list[dict]
) -> Path:
    """A run directory in `runs` whose manifest plans `documents` and
    whose events are `events`, as a run that measured them leaves it."""
    path = runs / name
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps({"documents": documents}))
    (path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    return path


ROLES = {
    "transcription": "transcribe",
    "extraction": "extract",
    "critique": "critique",
}


def finishing(sha1: str, stage: str, calls: int, invocation: str) -> list:
    """The events of an invocation that makes `calls` calls for the
    document in the stage and finishes it."""
    call = {
        "event": "cache-miss",
        "subject": sha1,
        "role": ROLES[stage],
        "invocation": invocation,
    }
    done = {
        "event": "done",
        "subject": sha1,
        "stage": stage,
        "invocation": invocation,
    }
    return [call] * calls + [done]


def an_excerpt(sha1: str) -> dict:
    return {"sha1": sha1, "kind": "excerpt", "stages": ["extraction"]}


def measures_in(tmp_path: Path) -> dict:
    """The excerpt measures an estimator of a new run in the runs
    directory reads."""
    (tmp_path / "corpus").mkdir(exist_ok=True)
    runs = runs_dir(tmp_path)
    return Quiet(tmp_path / "corpus", runs / "zz-new")._excerpt_measures()


class TestTheCallEstimateCountsExcerpts:
    """The images a PDF places within its text-layer pages become
    excerpts only as its transcription reads them (#466): the estimate
    counts them from the file, as the transcription stage reads them, and
    gives their calls as up to so many more."""

    def test_each_image_once_from_the_file(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """No run measured an excerpt: each image's calls are typical, a
        transcription that reuses the PDF's reading (none), an entity
        manifest and a chunk, and a critique. The letter's top, which both
        compilations place, is one image."""
        counted = estimate_of(tmp_path, compilations(tmp_path), engine)
        excerpts = counted.excerpts
        assert (excerpts.images, excerpts.containers) == (3, 2)
        assert excerpts.measured_stages == {
            "transcription": 0,
            "extraction": 0,
            "critique": 0,
        }
        assert excerpts.calls == {
            "transcription": 0,
            "extraction": 6,
            "critique": 3,
        }
        assert excerpts.total == 9
        lines = estimate.describe(counted)
        more = counted.total().more + 9
        assert any(f", and up to {more} more." in line for line in lines)
        assert excerpt_line(counted).startswith(
            "- Up to 9 more for excerpts: 3 images that 2 PDFs or decks "
            "place on their pages or slides"
        )
        assert (
            "Their calls: 0 transcription, as typical, no run having "
            "measured it; 6 extraction, as typical, no run having measured "
            "it; 3 critique, as typical, no run having measured it."
        ) in excerpt_line(counted)
        assert engine.calls == []

    @pytest.mark.parametrize("held", [False, True], ids=["new", "held"])
    def test_an_image_a_corpus_document_has_is_no_excerpt(
        self,
        tmp_path,
        engine,  # noqa: F811
        held,
    ):
        """Whether the compilations are transcribed in this invocation, or
        an earlier one stopped after the transcription and holds the
        letter's excerpt: the corpus document is the witness (ADR-0010)."""
        corpus = compilations(tmp_path)
        if held:
            run_scratch(tmp_path, corpus, engine, until="transcription")
            assert len(list(runs_dir(tmp_path).glob("*/excerpts/*"))) == 3
        (corpus.folder / "letter.jpg").write_bytes(SHARED_LETTER_IMAGE.data)
        corpus.texts["letter.jpg"] = EXCERPT
        excerpts = estimate_of(tmp_path, corpus, engine).excerpts
        # the booklet places the letter's top alone, so only the album
        # places an image that may become an excerpt
        assert (excerpts.images, excerpts.containers) == (2, 1)

    def test_an_image_the_never_ingest_list_names_is_no_excerpt(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        excerpts = estimate_of(
            tmp_path,
            compilations(tmp_path),
            engine,
            exclusions=ExclusionList.parse(f"{gray_sha1(1)}\n"),
        ).excerpts
        assert (excerpts.images, excerpts.containers) == (2, 2)

    def test_the_mean_for_excerpts_over_the_runs_measured(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A run measured the letter's excerpt: eleven people make its
        extraction an entity manifest and two chunks. The next run's
        excerpts are counted by that mean, not as typical, and the
        compilations, measured too, count for none of it."""
        engine.people = 11
        corpus = the_booklet(tmp_path)
        before = len(engine.calls)
        run_scratch(tmp_path, corpus, engine)
        read = [
            what
            for what, name in engine.calls[before:]
            if name == f"{LETTER_SHA1}.png" and what != "vision"
        ]
        assert len(read) == 3
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        excerpts = counted.excerpts
        assert excerpts.images == 1
        assert excerpts.measured_stages == {
            "transcription": 1,
            "extraction": 1,
            "critique": 1,
        }
        assert excerpts.calls == {
            "transcription": 0,
            "extraction": 3,
            "critique": 1,
        }
        line = excerpt_line(counted)
        assert line.startswith(
            "- Up to 4 more for excerpts: 1 image that 1 PDF or deck places "
            "on its pages or slides"
        )
        assert line.endswith(
            "Their calls: 0 transcription, by the mean over 1 excerpt the "
            "runs measured; 3 extraction, by the mean over 1 excerpt the "
            "runs measured; 1 critique, by the mean over 1 excerpt the runs "
            "measured. Up to, since an image the engine reads blank or as "
            "no document page makes no excerpt, and the cache may answer an "
            "excerpt's calls."
        )

    def test_a_stage_no_run_measured_is_typical(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A run that stopped after the transcription measured the
        excerpt's transcription alone: a new run's line counts it by the
        mean, and its extraction and critique as typical."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine, until="transcription")
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert counted.excerpts.measured_stages == {
            "transcription": 1,
            "extraction": 0,
            "critique": 0,
        }
        assert counted.excerpts.calls == {
            "transcription": 0,
            "extraction": 2,
            "critique": 1,
        }
        assert excerpt_line(counted).endswith(
            "Their calls: 0 transcription, by the mean over 1 excerpt the "
            "runs measured; 2 extraction, as typical, no run having "
            "measured it; 1 critique, as typical, no run having measured "
            "it. Up to, since an image the engine reads blank or as no "
            "document page makes no excerpt, and the cache may answer an "
            "excerpt's calls."
        )

    def test_the_mean_is_rounded_up(
        self,
        tmp_path,
        engine,  # noqa: F811
        monkeypatch,
    ):
        """Two excerpts measured at 1 and 2 extraction calls: three images
        come to 4.5 calls, given as 5; three measured at 1, 2 and 2 make
        one image's 5/3 calls 2."""
        measures = {("a" * 40, "extraction"): 1, ("b" * 40, "extraction"): 2}
        monkeypatch.setattr(
            Estimator, "_excerpt_measures", lambda self: dict(measures)
        )
        counted = estimate_of(tmp_path, compilations(tmp_path), engine)
        assert counted.excerpts.calls["extraction"] == 5
        assert counted.excerpts.measured_stages["extraction"] == 2
        assert (
            "5 extraction, by the mean over 2 excerpts the runs measured"
            in excerpt_line(counted)
        )
        measures[("c" * 40, "extraction")] = 2
        (tmp_path / "corpus" / "album.pdf").unlink()
        counted = estimate_of(tmp_path, the_booklet(tmp_path), engine)
        assert counted.excerpts.images == 1
        assert counted.excerpts.calls["extraction"] == 2

    def test_a_newer_run_without_excerpts_leaves_the_measure(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """After the run that measured the letter's excerpt, a new run of
        the 1938 postcard alone, which plans no excerpt, is the newest:
        the excerpt is still measured, in the run before it."""
        engine.people = 11
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        postcard = Corpus(tmp_path / "postcards")
        postcard.text(TEXT, "postcard-1938")
        run_scratch(tmp_path, postcard, engine, new=True)
        excerpts = estimate_of(tmp_path, corpus, engine, new=True).excerpts
        assert excerpts.measured_stages["extraction"] == 1
        assert excerpts.calls["extraction"] == 3

    def test_the_newest_run_measures_an_excerpt(self, tmp_path):
        """Two runs measured the same excerpt: the newer's count is its
        measure, as the newer of two runs measures any document
        (_baseline); and of two invocations of one run that both finished
        the stage, the later's."""
        runs = runs_dir(tmp_path)
        sha1, other = gray_sha1(1), gray_sha1(2)
        made_run(
            runs,
            "a-older",
            [an_excerpt(sha1), an_excerpt(other)],
            finishing(sha1, "extraction", 5, "i1")
            + finishing(other, "extraction", 4, "i1"),
        )
        made_run(
            runs,
            "b-newer",
            [an_excerpt(sha1)],
            finishing(sha1, "extraction", 2, "i1")
            + finishing(sha1, "extraction", 3, "i2"),
        )
        assert measures_in(tmp_path) == {
            (sha1, "extraction"): 3,
            (other, "extraction"): 4,
        }
        # the baseline reads back to the older run for the other excerpt,
        # and keeps the newer's measure of this one
        estimator = Quiet(tmp_path / "corpus", runs / "zz-new")
        needed = {(sha1, "extraction"), (other, "extraction")}
        assert estimator._baseline(needed) == {
            (sha1, "extraction"): 3,
            (other, "extraction"): 4,
        }
        # of the newer run's two invocations, the later's
        assert estimate._finished(RunDir(runs / "b-newer")) == {
            (sha1, "extraction"): 3
        }

    def test_a_damaged_manifest_in_another_run_is_passed_over(self, tmp_path):
        runs = runs_dir(tmp_path)
        sha1 = gray_sha1(1)
        made_run(
            runs,
            "a-measured",
            [an_excerpt(sha1)],
            finishing(sha1, "extraction", 4, "i1"),
        )
        made_run(
            runs,
            "b-documents-not-a-list",
            {sha1: an_excerpt(sha1)},
            finishing(sha1, "extraction", 9, "i1"),
        )
        for name, documents in (("c-a-number", 7), ("d-none", None)):
            made_run(
                runs,
                name,
                documents,
                finishing(sha1, "extraction", 9, "i1"),
            )
        made_run(
            runs,
            "e-sha1-not-a-string",
            [{"sha1": [sha1], "kind": "excerpt"}, "an entry", None],
            finishing(sha1, "extraction", 9, "i1"),
        )
        listed = made_run(
            runs,
            "f-a-list",
            [],
            finishing(sha1, "extraction", 9, "i1"),
        )
        (listed / "run.json").write_text(json.dumps([an_excerpt(sha1)]))
        assert measures_in(tmp_path) == {(sha1, "extraction"): 4}

    def test_an_event_line_no_writer_makes_is_passed_over(self, tmp_path):
        """An invocation or a subject that is a list or an object, in
        another run's events, neither stops the estimate nor counts."""
        runs = runs_dir(tmp_path)
        sha1 = gray_sha1(1)
        odd = [
            {**event, "invocation": ["i1"]}
            for event in finishing(sha1, "extraction", 9, "i1")
        ] + [
            {**event, "subject": {"sha1": sha1}}
            for event in finishing(sha1, "extraction", 9, "i2")
        ]
        made_run(
            runs,
            "a-measured",
            [an_excerpt(sha1)],
            finishing(sha1, "extraction", 4, "i1"),
        )
        made_run(runs, "b-odd-events", [an_excerpt(sha1)], odd)
        assert estimate._finished(RunDir(runs / "b-odd-events")) == {}
        assert measures_in(tmp_path) == {(sha1, "extraction"): 4}

    def test_up_to_more_for_excerpts_alone(self, tmp_path):
        """No extraction may be refused, so the excerpts are all the up to
        more there is."""
        counted = estimate.Estimate(
            run=tmp_path / "runs" / "run",
            resumed=False,
            cache=tmp_path / "cache",
            stages={"transcription": StageCalls(made=1, known=1)},
            excerpts=estimate.ExcerptCalls(
                images=1,
                containers=1,
                calls={"extraction": 2, "critique": 1},
                measured_stages={"extraction": 0, "critique": 0},
            ),
        )
        lines = estimate.describe(counted)
        assert (
            "Engine calls: 1, 1 known and 0 estimated, and up to 3 more."
            in (lines)
        )
        assert excerpt_line(counted).startswith("- Up to 3 more for excerpts")
        assert not any(line.startswith("- Up to 3 more,") for line in lines)

    @pytest.mark.parametrize(
        "reading",
        [
            record_answer(""),
            json.dumps(
                {
                    "document_class": "unclassified",
                    "pages": [
                        record_page(1, "", status="not-a-document-page")
                    ],
                }
            ),
        ],
        ids=["blank", "not-a-document-page"],
    )
    def test_an_image_the_cache_reads_as_no_page_is_no_excerpt(
        self,
        tmp_path,
        engine,  # noqa: F811
        reading,
    ):
        """Its kept reading, which the cache answers, calls its page blank
        or not a document page: the run makes no excerpt of it, and the
        estimate counts none."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine, until="transcription")
        (entry,) = entries(tmp_path / "cache", role="transcribe")
        answer_with(entry, reading)
        excerpts = estimate_of(tmp_path, corpus, engine, new=True).excerpts
        assert (excerpts.images, excerpts.total) == (0, 0)
        # read as the letter, it may be one
        answer_with(entry, record_answer(EXCERPT))
        excerpts = estimate_of(tmp_path, corpus, engine, new=True).excerpts
        assert excerpts.images == 1

    def test_an_image_whose_kept_reading_fails_the_check_is_counted(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The cache serves no reading the check fails: the run reads the
        image again, so it may be an excerpt."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine, until="transcription")
        (entry,) = entries(tmp_path / "cache", role="transcribe")
        answer_with(entry, REFUSAL)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert counted.excerpts.images == 1
        assert per_stage(counted, "known")["transcription"] == 1

    def test_until_counts_the_excerpt_stages_up_to_it(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        counted = estimate_of(
            tmp_path, compilations(tmp_path), engine, until="transcription"
        )
        assert counted.excerpts.images == 3
        assert counted.excerpts.calls == {"transcription": 0}
        assert excerpt_line(counted) is None

    def test_an_excerpt_held_from_an_earlier_invocation_is_counted(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """Invoked again, the run reuses the booklet's transcription, and
        takes the excerpt it found: the stages whose views do not stand
        are counted, by the same mean; once every view stands, none is."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine, until="transcription")
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.resumed
        assert counted.stages["transcription"].reused == 1
        excerpts = counted.excerpts
        assert (excerpts.images, excerpts.containers) == (1, 1)
        # its transcription stands; its extraction and critique are to
        # make, neither measured
        assert excerpts.calls == {
            "transcription": 0,
            "extraction": 2,
            "critique": 1,
        }
        assert excerpts.measured_stages["extraction"] == 0
        run_scratch(tmp_path, corpus, engine)
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.resumed
        assert counted.stages["extraction"].reused == 1
        assert (counted.excerpts.images, counted.excerpts.total) == (0, 0)

    def test_an_excerpt_whose_transcription_does_not_stand_counts_on(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A held excerpt whose transcription view is gone, while the
        booklet's reading of its image now reads otherwise, makes it again
        with other content: every stage after it is made too, though their
        views stand, each by the resumed run's own measure (eleven people
        make the extraction an entity manifest and two chunks, where
        typical is one chunk), as the driver makes them (#494)."""
        engine.people = 11
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = (runs_dir(tmp_path)).glob("*/excerpts/*")
        transcription = excerpt / "transcription.json"
        assert transcription.is_file()
        reread_the_letter(tmp_path, corpus, engine)
        transcription.unlink()
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.stages["transcription"].reused == 1
        assert counted.excerpts.images == 1
        assert counted.excerpts.calls == {
            "transcription": 0,
            "extraction": 3,
            "critique": 1,
        }
        assert counted.excerpts.measured_stages["extraction"] == 1
        before = len(engine.calls)
        run_scratch(tmp_path, corpus, engine, resume=LATEST)
        assert excerpt_calls(engine, before) == [
            "manifest",
            "chunk",
            "chunk",
            "critique",
        ]

    def test_an_excerpt_transcription_made_again_unchanged(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A held excerpt whose transcription view is gone makes it again
        from the booklet's reading, with the content its batch was made
        from: its extraction and critique stand, as the driver reuses
        them, and no engine starts for it, so another CLI is no refusal,
        whatever --until says (#494)."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
        (excerpt / "transcription.json").unlink()
        engine.cli = "9.9.10"
        closed = engine.closed
        for until in ("transcription", None):
            counted = estimate_of(
                tmp_path, corpus, engine, resume=LATEST, until=until
            )
            assert counted.excerpts.stale["transcription"] == 1
            assert counted.cli_version is None
        assert counted.excerpts.stale == {
            "transcription": 1,
            "extraction": 0,
            "critique": 0,
        }
        assert counted.excerpts.total == 0
        assert engine.closed == closed

    def test_two_excerpts_of_one_container_with_other_stages_left(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """Of the album's two gray images, one has its critique left to
        make and the other its extraction: the estimate starts the
        extract and the critique roles' clients (#494). The letter's
        excerpt, which the booklet places too, has its critique left, so
        that each container has a stage left to make."""
        corpus = compilations(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        held = runs_dir(tmp_path).glob("*/excerpts/*")
        excerpts = {path.name: path for path in held}
        for sha1 in (gray_sha1(1), SHARED_LETTER_SHA1):
            for path in excerpts[sha1].glob("critique*"):
                path.unlink()
        (excerpts[gray_sha1(2)] / "batch.json").unlink()
        closed = engine.closed
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.excerpts.stale == {
            "transcription": 0,
            "extraction": 1,
            "critique": 3,
        }
        assert engine.closed - closed == 2

    @pytest.mark.parametrize("measured", [True, False])
    def test_a_held_excerpt_counts_only_the_stages_left(
        self,
        tmp_path,
        engine,  # noqa: F811
        monkeypatch,
        measured,
    ):
        """A full run made every view of the letter's excerpt, and its
        critique is lost: the next invocation makes the critique alone,
        by the run's measure or as typical, both one call; the line names
        that stage alone."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
        lost = list(excerpt.glob("critique*"))
        assert lost
        for path in lost:
            path.unlink()
        if not measured:
            monkeypatch.setattr(Estimator, "_excerpt_measures", lambda s: {})
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.excerpts.calls == {
            "transcription": 0,
            "extraction": 0,
            "critique": 1,
        }
        how = (
            "by the mean over 1 excerpt the runs measured"
            if measured
            else "as typical, no run having measured it"
        )
        assert f"Their calls: 1 critique, {how}. Up to" in (
            excerpt_line(counted)
        )

    def test_held_excerpts_whose_extraction_stands(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """After a run that stopped after the extraction, each of the
        three held excerpts has its critique left to make."""
        corpus = compilations(tmp_path)
        run_scratch(tmp_path, corpus, engine, until="extraction")
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.resumed
        assert counted.excerpts.calls == {
            "transcription": 0,
            "extraction": 0,
            "critique": 3,
        }

    def test_a_held_excerpt_the_driver_sets_aside_counts_every_stage(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The excerpt's state is lost while its views remain: the driver
        sets its directory aside and makes every view again."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
        (excerpt / "state.json").unlink()
        assert (excerpt / "derivations.json").is_file()
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.excerpts.calls == {
            "transcription": 0,
            "extraction": 2,
            "critique": 1,
        }

    def test_a_held_excerpts_stage_starts_its_engine(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """Every view of the booklet stands and the letter's excerpt has
        its critique left to make: the estimate starts the critique role's
        engine client for it, as the run does, names the CLI it runs, and
        closes it (#494)."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        lose_the_excerpts_critique(tmp_path)
        closed = engine.closed
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert per_stage(counted, "reused") == {
            "transcription": 1,
            "extraction": 1,
            "critique": 1,
        }
        assert counted.excerpts.stale["critique"] == 1
        assert counted.cli_version == "9.9.9"
        assert engine.closed - closed == 1

    def test_a_claude_cli_updated_refuses_a_held_excerpts_stage(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """Another CLI than the run recorded, with every view of the
        booklet standing: the excerpt's critique left to make starts an
        engine, so the estimate refuses, as the run would (#494); with no
        excerpt stage left within --until, it does not."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        lose_the_excerpts_critique(tmp_path)
        engine.cli = "9.9.10"
        with pytest.raises(RunSettingsConflict, match="engine.cli_version"):
            estimate_of(tmp_path, corpus, engine, resume=LATEST)
        counted = estimate_of(
            tmp_path, corpus, engine, resume=LATEST, until="extraction"
        )
        assert counted.excerpts.images == 0
        assert counted.cli_version is None

    def test_newly_read_images_start_their_stages_engines(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The booklet's transcription view is lost, and made again from
        the cache, while its extraction and critique stand: the image it
        reads counts every excerpt stage, and the estimate starts the
        extract and critique roles' clients for them, besides the
        transcribe role's (#494)."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (container,) = runs_dir(tmp_path).glob("*/booklet")
        (container / "transcription.json").unlink()
        closed = engine.closed
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert per_stage(counted, "reused") == {
            "extraction": 1,
            "critique": 1,
        }
        assert counted.excerpts.stale == {
            "transcription": 1,
            "extraction": 1,
            "critique": 1,
        }
        assert engine.closed - closed == 3

    def test_a_held_excerpts_transcription_the_check_fails_now(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The letter's excerpt holds a vision transcription with no
        record, which the check fails: the driver sets it aside and makes
        it again from the booklet's reading, with the content its batch was
        made from, so its extraction and critique stand; the estimate
        counts the transcription alone (#494)."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
        view = excerpt / "transcription.json"
        kept = json.loads(view.read_text())
        view.write_text(json.dumps(dict(kept, pages=None)))
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.excerpts.stale == {
            "transcription": 1,
            "extraction": 0,
            "critique": 0,
        }
        before = len(engine.calls)
        run_scratch(tmp_path, corpus, engine, resume=LATEST)
        assert set_aside_stages(tmp_path) == [("excerpt", "transcription")]
        assert excerpt_calls(engine, before) == []

    def test_a_critique_of_an_earlier_format_is_made_again(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The booklet's critique and the letter excerpt's hold a finding
        in an earlier format, neither a finding nor one that failed its
        checks: the driver sets each aside and makes it again, and the
        estimate counts both (#494)."""
        corpus = the_booklet(tmp_path)
        run_scratch(tmp_path, corpus, engine)
        (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
        (container,) = runs_dir(tmp_path).glob("*/booklet")
        earlier = json.dumps([{"target": "e1", "issue": "a concern"}])
        for doc in (container, excerpt):
            (doc / "critique.json").write_text(earlier)
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert per_stage(counted, "made") == {"critique": 1}
        assert counted.excerpts.stale == {
            "transcription": 0,
            "extraction": 0,
            "critique": 1,
        }
        run_scratch(tmp_path, corpus, engine, resume=LATEST)
        assert sorted(set_aside_stages(tmp_path)) == [
            ("excerpt", "critique"),
            ("pdf", "critique"),
        ]


def reread_the_letter(tmp_path: Path, corpus: Corpus, fake) -> None:
    """Refresh the real stage with a different fictional model reading.

    Its inventory outcome and located context must be recorded by the same
    path as normal readings; editing a saved product would damage integrity.
    """
    name = f"{LETTER_SHA1}.png"
    assert corpus.texts[name].count("Angerring 11") == 1
    corpus.texts[name] = corpus.texts[name].replace(
        "Angerring 11", "Angerring 71"
    )
    run_scratch(
        tmp_path,
        corpus,
        fake,
        resume=LATEST,
        refresh=["transcribe"],
        until="transcription",
    )


def excerpt_calls(fake, before: int) -> list[str]:
    """What the engine was asked for an excerpt since `before` calls:
    its extraction's calls name its image, and a critique names none."""
    return [
        what
        for what, name in fake.calls[before:]
        if (name or "").endswith(".png") or what == "critique"
    ]


def lose_the_excerpts_critique(tmp_path: Path) -> None:
    """Remove the critique view of the one excerpt a run holds."""
    (excerpt,) = runs_dir(tmp_path).glob("*/excerpts/*")
    lost = list(excerpt.glob("critique*"))
    assert lost
    for path in lost:
        path.unlink()


def set_aside_stages(tmp_path: Path) -> list[tuple[str, str]]:
    """(kind, stage) of each set-aside event of the latest run."""
    run_dir = runs_in(runs_dir(tmp_path))[-1]
    return [
        (event["kind"], event["stage"])
        for event in RunDir(run_dir).events()
        if event.get("event") == "set-aside"
    ]


def stricter_check(monkeypatch, phrase: str) -> None:
    """Make the transcription check stricter, as a change to it would:
    a text holding `phrase` fails (records.stricter_check)."""
    records.stricter_check(monkeypatch, re.escape(phrase))


def without_signature(monkeypatch, name: str) -> None:
    """The transcription check without the signature `name`, as a
    change to it would leave it."""
    monkeypatch.setattr(
        transcription_check,
        "SIGNATURES",
        tuple(s for s in transcription_check.SIGNATURES if s.name != name),
    )


def flag_alerts(orchestrator: CachedRun) -> list[str]:
    """The transcription-flag alerts of this run."""
    return [
        event["check"]
        for event in this_run(orchestrator, "alert")
        if event.get("check") == "transcription-flag"
    ]


def unseen(text: str = SECOND) -> str:
    """The record of one page holding `text`, whose note says the
    engine saw no image: flagged media-missing."""
    return json.dumps(
        {
            "document_class": "unclassified",
            "pages": [record_page(1, text, notes="No image was provided.")],
        }
    )


def set_asides(orchestrator: CachedRun, kind: str = "pdf") -> list[dict]:
    """The set-aside events of this run for documents of a kind: the
    PDFs' by default, since an excerpt's transcription is its image's
    reading, set aside with it."""
    return [
        event
        for event in this_run(orchestrator, "set-aside")
        if event.get("kind") == kind
    ]


class TestReuse:
    """A reused view of a PDF read page by page is checked again where
    the vision engine read it, and never where its text layer did."""

    def test_a_stricter_check_reads_a_page_again(self, tmp_path, monkeypatch):
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reader = Reader({"page-2": [SECOND, "Klara left Flörburg first."]})
        run(tmp_path, corpus, reader)
        stricter_check(monkeypatch, "followed her in 1939")
        again = run(tmp_path, corpus, reader)
        doc = document(again, "family-history.pdf")
        (aside,) = set_asides(again)
        assert aside["stage"] == "transcription"
        assert aside["reason"].endswith("(page 2)")
        assert reader.names() == ["page-2.png", "page-2.png"]
        transcription = transcription_of(doc)
        assert transcription.page_text(transcription.pages[1]) == (
            "Klara left Flörburg first."
        )
        assert (doc.dir / "transcription.superseded-1.json").exists()

    @pytest.mark.parametrize(
        "status", ["blank", "illegible", "not-a-document-page"]
    )
    def test_a_vision_page_with_nothing_to_read_is_reused(
        self, tmp_path, status
    ):
        """A page the record of its reading calls blank, illegible or not
        a document page needs no text when it is made, and is checked on
        reuse as it was then: three runs read it once, and set nothing
        aside."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reading = json.dumps(
            {
                "document_class": "unclassified",
                "pages": [record_page(1, "", status=status)],
            }
        )
        reader = Reader({"page-2": reading})
        for _run in range(3):
            again = run(tmp_path, corpus, reader)
            assert set_asides(again) == []
        assert reader.names() == ["page-2.png"]
        doc = document(again, "family-history.pdf")
        assert transcription_of(doc).pages[1].status == status
        assert not (doc.dir / "transcription.superseded-1.json").exists()

    def test_a_record_s_wording_fails_no_reused_page(self, tmp_path):
        """A vision page whose record's text reads as an announcement is
        a source's sentence when made and on every reuse: three runs read
        it once and set nothing aside."""
        corpus = folder(tmp_path, family_history=history(vision_page()))
        line = "I will bring the documents to Stupfield."
        reader = Reader({"page-2": record_answer(line)})
        for _run in range(3):
            again = run(tmp_path, corpus, reader)
            assert set_asides(again) == []
        assert reader.names() == ["page-2.png"]
        doc = document(again, "family-history.pdf")
        assert again._transcription(doc).flag == "narration-only"

    @pytest.mark.parametrize("whole", [False, True])
    def test_a_stored_flag_the_check_raises_no_more_is_dropped(
        self, tmp_path, monkeypatch, whole
    ):
        """Made flagged, a view reused once the check no longer raises
        its flag carries none, and raises no alert, in a run's reuse and
        in the anomaly recheck alike."""
        from yizkr.pipeline.anomalies import recheck

        if whole:
            read_whole(monkeypatch)
        corpus = folder(tmp_path, family_history=history(vision_page()))
        whole_record = json.dumps(
            {
                "document_class": "unclassified",
                "pages": [
                    record_page(1, FIRST),
                    record_page(2, SECOND, notes="No image was provided."),
                ],
            }
        )
        reader = Reader(
            {"page-2": unseen(), "family-history.pdf": whole_record}
        )
        first = run(tmp_path, corpus, reader)
        doc = document(first, "family-history.pdf")
        assert transcription_of(doc).flag == "media-missing"
        assert flag_alerts(first) == ["transcription-flag"]

        def rechecked():
            return [
                alert["check"]
                for alert in recheck(tmp_path / "state")
                if alert["check"] == "transcription-flag"
            ]

        assert rechecked() == ["transcription-flag"]
        without_signature(monkeypatch, "media-missing")
        again = run(tmp_path, corpus, reader)
        assert set_asides(again) == []
        assert (
            again._transcription(document(again, doc.source.name)).flag is None
        )
        assert flag_alerts(again) == []
        assert rechecked() == []
        assert len(reader.names()) == 1

    def test_the_first_flagged_vision_page_flags_the_pdf(self, tmp_path):
        """Two vision pages flagged by two signatures: the first page's
        flag is the PDF's, when made and on reuse."""
        corpus = folder(
            tmp_path,
            scans=pdfs.pdf([typed(FIRST), vision_page(), vision_page()]),
        )
        reader = Reader(
            {
                "page-2": unseen(),
                "page-3": record_answer("Let me take a look at the image."),
            }
        )
        made = run(tmp_path, corpus, reader)
        assert transcription_of(document(made, "scans.pdf")).flag == (
            "media-missing"
        )
        again = run(tmp_path, corpus, reader)
        assert set_asides(again) == []
        assert again._transcription(document(again, "scans.pdf")).flag == (
            "media-missing"
        )

    def test_a_text_file_s_flag_is_the_check_s_made_now(
        self, tmp_path, monkeypatch
    ):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "reply.txt").write_text(
            "I'm sorry, but I can't help with that.\n"
        )
        reader = Reader({})
        made = run(tmp_path, corpus, reader)
        assert transcription_of(document(made, "reply.txt")).flag == (
            "refusal-short"
        )
        without_signature(monkeypatch, "refusal-short")
        again = run(tmp_path, corpus, reader)
        assert set_asides(again, "text") == []
        assert again._transcription(document(again, "reply.txt")).flag is None
        assert flag_alerts(again) == []

    @pytest.mark.parametrize("whole", [False, True])
    def test_the_flag_is_the_one_the_check_raises_now(
        self, tmp_path, monkeypatch, whole
    ):
        """A reused transcription carries the flag of the check made now:
        one the check now raises is added, and one it raises no more is
        dropped. The flag is no part of the content, so nothing is made
        again, and the transcription-flag alert follows it."""
        if whole:
            read_whole(monkeypatch)
        corpus = folder(tmp_path, family_history=history(vision_page()))
        reader = Reader(
            {
                "page-2": SECOND,
                "family-history.pdf": record_answer(FIRST, SECOND),
            }
        )

        def flags(orchestrator):
            doc = document(orchestrator, "family-history.pdf")
            alerts = [
                event["check"]
                for event in this_run(orchestrator, "alert")
                if event.get("check") == "transcription-flag"
            ]
            return orchestrator._transcription(doc).flag, alerts

        assert flags(run(tmp_path, corpus, reader)) == (None, [])
        with monkeypatch.context() as patch:
            flagging = transcription_check._signature(
                "flagged-now",
                transcription_check.TOOL_FAILURE,
                r"followed her in 1939",
            )
            patch.setattr(
                transcription_check,
                "SIGNATURES",
                (*transcription_check.SIGNATURES, flagging),
            )
            again = run(tmp_path, corpus, reader)
            assert set_asides(again) == []
            assert flags(again) == ("flagged-now", ["transcription-flag"])
        again = run(tmp_path, corpus, reader)
        assert set_asides(again) == []
        assert flags(again) == (None, [])
        assert len(reader.names()) == 1

    def test_an_image_reading_kept_without_its_record_is_made_again(
        self, tmp_path
    ):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        view = json.loads(kept.read_text())
        del view["document_class"]
        kept.write_text(json.dumps(view))
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert aside["reason"] == (
            f"the recorded transcription of image {LETTER_SHA1[:12]} "
            "has changed"
        )
        assert "document_class" in json.loads(kept.read_text())

    def test_a_pdf_read_whole_is_checked_for_its_page_count_on_reuse(
        self, tmp_path, monkeypatch
    ):
        """A reused record of a PDF read whole that gives more pages than
        the PDF has fails the check, as it would when made."""
        read_whole(monkeypatch)
        corpus = folder(tmp_path, family_history=history())
        reading = record_answer(FIRST, SECOND, SECOND)
        reader = Reader(
            {"family-history.pdf": [reading, record_answer(FIRST, SECOND)]}
        )
        # transcribe holds its own name for the count: both are set, so
        # the count is unknown when the reading is made even where
        # pdfinfo is installed
        for module in (transcription_check, transcribe):
            monkeypatch.setattr(module, "media_page_count", lambda path: None)
        run(tmp_path, corpus, reader)
        for module in (transcription_check, transcribe):
            monkeypatch.setattr(module, "media_page_count", lambda path: 2)
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert "(pages-extra)" in aside["reason"]

    def test_text_read_from_a_text_layer_is_never_checked(
        self, tmp_path, monkeypatch
    ):
        """A typed page that holds what the check would fail in an
        engine's answer is the document's own text: it is transcribed, and
        stands when it is reused under a check stricter still."""
        note = typed("Nicholas: " + REFUSAL + " The ink has faded too far.")
        corpus = folder(tmp_path, note=pdfs.pdf([typed(FIRST), note]))
        reader = Reader({})
        doc = document(run(tmp_path, corpus, reader), "note.pdf")
        assert doc.state.status == AWAITING_CLAIMS
        assert REFUSAL in transcription_of(doc).text
        stricter_check(monkeypatch, "The ink has faded")
        again = run(tmp_path, corpus, reader)
        assert set_asides(again) == []
        assert reader.read == []

    def test_a_stricter_check_reads_an_image_again(
        self, tmp_path, monkeypatch
    ):
        """The image's transcription that the check now fails is moved
        aside, never deleted, and the image is read again."""
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: [EXCERPT, "Liebes Pünktchen!"]})
        run(tmp_path, corpus, reader)
        stricter_check(monkeypatch, "Rahel wohnt jetzt")
        again = run(tmp_path, corpus, reader)
        doc = document(again, "booklet.pdf")
        (aside,) = set_asides(again)
        assert aside["reason"].endswith(f"(image {LETTER_SHA1[:12]})")
        assert reader.names() == [f"{LETTER_SHA1}.png"] * 2
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        assert json.loads(kept.read_text())["text"] == page_text(
            "Liebes Pünktchen!"
        )
        superseded = kept.with_name(
            f"{LETTER_SHA1}.transcription.superseded-1.json"
        )
        assert json.loads(superseded.read_text())["text"] == page_text(EXCERPT)
        # the excerpt's transcription, the image's reading, is made again
        # from the new one
        assert any(
            event.get("stage") == "transcription"
            and event.get("subject") == LETTER_SHA1
            for event in this_run(again, "started")
        )
        excerpt = excerpt_of(again, LETTER_SHA1)
        assert transcription_of(excerpt).text == page_text("Liebes Pünktchen!")

    def test_every_image_on_a_page_is_checked_again(
        self, tmp_path, monkeypatch
    ):
        """The second image a page places is checked again as its first
        is."""
        other = pdfs.gray(300, 120, shade=9)
        other_sha1 = hashlib.sha1(png(300, 120, 1, other.data)).hexdigest()
        page = pdfs.Page().image(LETTER_IMAGE, 72, 600, 200, 80)
        page.image(other, 300, 600, 200, 80)
        page.text(pdfs.lines(CAPTION, TRANSLATION), y=500)
        corpus = folder(tmp_path, booklet=pdfs.pdf([typed(TITLE), page]))
        reader = Reader({LETTER_SHA1: EXCERPT, other_sha1: ["Wastl", "Klara"]})
        run(tmp_path, corpus, reader)
        stricter_check(monkeypatch, "Wastl")
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert aside["reason"].endswith(f"(image {other_sha1[:12]})")

    @pytest.mark.parametrize(
        "damage, reason",
        [
            ("another image", "names another image"),
            ("not a transcription", "cannot be read"),
        ],
    )
    def test_a_damaged_image_transcription_is_made_again(
        self, tmp_path, damage, reason
    ):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        if damage == "another image":
            view = json.loads(kept.read_text())
            view["artifact"]["sha1"] = "b" * 40
            kept.write_text(json.dumps(view))
        else:
            kept.write_text("{")
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert reason in aside["reason"]
        assert json.loads(kept.read_text())["artifact"]["sha1"] == (
            LETTER_SHA1
        )
        assert kept.with_name(
            f"{LETTER_SHA1}.transcription.superseded-1.json"
        ).exists()

    def test_a_reused_view_stands_without_a_call(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        reader = RefusingReader({})
        again = run(tmp_path, corpus, reader)
        reused = [
            event["kind"]
            for event in this_run(again, "reused")
            if event["stage"] == "transcription"
        ]
        assert reused == ["pdf", "excerpt"]


class TestTheView:
    def test_a_view_without_pages_is_written_as_it_always_was(self):
        """No `pages`, `document_class` or `flag` key: a transcription
        without a record (a text file's, say) keeps its bytes."""
        from yizkr.claims import ArtifactRef

        transcription = Transcription(
            artifact=ArtifactRef(sha1="a" * 40), text=EXTRACT, engine="x"
        )
        assert set(json.loads(transcription.model_dump_json())) == {
            "artifact",
            "text",
            "language",
            "engine",
        }

    def test_the_pages_must_lie_in_the_text_in_order(self):
        from yizkr.claims import ArtifactRef

        def page(number, start, end):
            return TranscribedPage(
                page=number,
                start=start,
                end=end,
                status="transcribed",
                provenance=PageProvenance(method="text-layer", engine="m"),
            )

        text = "[Page 1]\nRahel\n\n[Page 2]\nKlara"
        ref = ArtifactRef(sha1="a" * 40)
        Transcription(
            artifact=ref,
            text=text,
            engine=TEXT_LAYER,
            pages=[page(1, 9, 14), page(2, 25, 30)],
        )
        for pages in (
            [page(1, 9, 31)],
            [page(2, 25, 30), page(1, 9, 14)],
            [page(1, 9, 14), page(1, 25, 30)],
        ):
            with pytest.raises(ValueError):
                Transcription(
                    artifact=ref, text=text, engine=TEXT_LAYER, pages=pages
                )

    def test_provenance_is_no_part_of_the_content(self):
        """How a page was read is provenance, as the engine is, and is left
        out of the content hash, as is the check's flag; where a page's text
        lies, the images it names and the record's fields are content, as
        the text is (#389 hashes what extraction reads of a page)."""
        from yizkr.claims import ArtifactRef

        ref = ArtifactRef(sha1="a" * 40)
        text = "[Page 1]\nRahel"

        def made(
            method, made_by, *, end=14, annotations=0, flag=None, **record
        ):
            return Transcription(
                artifact=ref,
                text=text,
                engine=made_by,
                flag=flag,
                pages=[
                    TranscribedPage(
                        page=1,
                        start=9,
                        end=end,
                        status=record.get("status", "transcribed"),
                        printed_page=record.get("printed_page"),
                        languages=record.get("languages", []),
                        notes=record.get("notes", ""),
                        provenance=PageProvenance(
                            method=method,
                            engine=made_by,
                            annotations=annotations,
                        ),
                    )
                ],
            )

        def hashed(transcription):
            return content_hash(
                transcription.model_dump(mode="json"),
                volatile=TRANSCRIPTION_VOLATILE,
            )

        read = made("text-layer", TEXT_LAYER)
        assert hashed(read) == hashed(
            made("vision", "llm-vision:scripted/model-a", annotations=2)
        )
        assert hashed(read) != hashed(made("text-layer", TEXT_LAYER, end=13))
        assert hashed(read) == hashed(
            made("text-layer", TEXT_LAYER, flag="refusal-short")
        )
        for field in (
            {"printed_page": "12"},
            {"status": "illegible"},
            {"languages": ["de"]},
            {"notes": "water damage"},
        ):
            assert hashed(read) != hashed(
                made("text-layer", TEXT_LAYER, **field)
            )
        # the document's class is content too
        letter = read.model_copy(update={"document_class": "letter"})
        certificate = read.model_copy(update={"document_class": "certificate"})
        assert len({hashed(read), hashed(letter), hashed(certificate)}) == 3


# -- the keys -----------------------------------------------------------------


def read_whole(monkeypatch) -> None:
    """Force whole-document vision to exercise that supported route."""
    monkeypatch.setattr(
        PdfPageTranscriber, "transcribe", lambda self, path: None
    )


def calls_for(orchestrator: CachedRun, doc: DocumentRun, role: str) -> dict:
    """How the cache answered the document's calls in `role`: hits and
    misses, each by reason."""
    counts: dict[str, int] = {}
    for event in this_run(orchestrator, "cache-hit", "cache-miss"):
        if event.get("subject") == doc.sha1 and event.get("role") == role:
            key = f"{event['event']}: {event.get('reason')}"
            counts[key] = counts.get(key, 0) + 1
    return counts


class TestKeys:
    """Reuse follows the submitted content. Checked whole-PDF inputs
    reuse compatible responses; original-byte adoptions remain inert."""

    CORPUS = {
        "birth_extract_1938": extract,
        "family_history_2008": history,
        "booklet": booklet,
    }
    READINGS = {
        "birth-extract-1938.pdf": EXTRACT,
        "family-history-2008.pdf": HISTORY,
        "booklet.pdf": BOOKLET,
        LETTER_SHA1: EXCERPT,
    }

    def corpus(self, tmp_path) -> Path:
        return folder(
            tmp_path, **{name: make() for name, make in self.CORPUS.items()}
        )

    def test_only_pdfs_read_page_by_page_are_made_again(
        self, tmp_path, monkeypatch
    ):
        corpus = self.corpus(tmp_path)
        cache = tmp_path / "cache"
        with monkeypatch.context() as patch:
            read_whole(patch)
            before = run(
                tmp_path, corpus, Reader(self.READINGS), cache_dir=cache,
                state="before",
            )  # fmt: skip
        reader = Reader(self.READINGS)
        after = run(tmp_path, corpus, reader, cache_dir=cache, state="after")
        scan = document(after, "birth-extract-1938.pdf")
        typed_pages = document(after, "family-history-2008.pdf")
        compilation = document(after, "booklet.pdf")
        # the scan: its transcription and every call after it from the
        # cache, and its views as the baseline's
        assert calls_for(after, scan, "transcribe") == {
            "cache-hit: key match": 1
        }
        extract_calls = calls_for(after, scan, "extract")
        assert extract_calls and set(extract_calls) == {"cache-hit: key match"}
        assert set(calls_for(after, scan, "critique")) == {
            "cache-hit: key match"
        }
        was = document(before, "birth-extract-1938.pdf")
        assert (
            scan.transcription.read_bytes() == was.transcription.read_bytes()
        )
        assert content_hash(
            json.loads(scan.batch.read_text()),
            volatile=("extraction_run.started_at",),
        ) == content_hash(
            json.loads(was.batch.read_text()),
            volatile=("extraction_run.started_at",),
        )
        # the family history: no transcription call at all, and its
        # extraction asked afresh, since its text is its text layer now
        assert calls_for(after, typed_pages, "transcribe") == {}
        assert "cache-miss: absent" in calls_for(after, typed_pages, "extract")
        # the booklet: its image read afresh, on its own
        assert calls_for(after, compilation, "transcribe") == {
            "cache-miss: absent": 1
        }
        assert reader.names() == [f"{LETTER_SHA1}.png"]

    @pytest.mark.parametrize("checked", [False, True])
    def test_only_adoptions_for_checked_pdf_inputs_are_served(
        self, tmp_path, checked
    ):
        """An adoption is usable only for the actual submitted bytes.

        The scan uses a checked bitmap-only PDF. An archive reading keyed
        by its original bytes cannot answer that call; a matching checked
        input can. The typed PDF makes no whole-document vision call.
        """
        from yizkr.pipeline.textlayer import prepare_pdf_vision

        corpus = self.corpus(tmp_path)
        cache = tmp_path / "cache"
        seeder = CachedRun(
            corpus,
            tmp_path / "seeder",
            engine=RefusingReader({}),
            cache_dir=cache,
            log=quiet,
        )
        client = seeder.role_client("transcribe")
        keys = {}
        for name, reading in (
            ("birth-extract-1938.pdf", EXTRACT),
            ("family-history-2008.pdf", HISTORY),
        ):
            media = corpus / name
            if checked:
                prepared = tmp_path / (name + "-prepared")
                prepared.mkdir()
                media = prepare_pdf_vision(media, prepared, lambda sha1: False)
            key = keys[name] = client.complete_key(
                TRANSCRIPTION_PROMPT, [media]
            )
            assert client.seed(
                key,
                record_answer(reading, document_class="certificate"),
                {"model_identifier": "archived/model-z", "backfilled": True},
            )
        seeder._close_engines()
        reader = Reader(
            {LETTER_SHA1: EXCERPT, "birth-extract-1938.pdf": EXTRACT}
        )
        after = run(tmp_path, corpus, reader, cache_dir=cache, state="after")
        scan = document(after, "birth-extract-1938.pdf")
        assert calls_for(after, scan, "transcribe") == {
            "cache-hit: adopted" if checked else "cache-miss: absent": 1
        }
        stored = json.loads(scan.transcription.read_text())
        assert stored["text"] == page_text(EXTRACT)
        expected_model = "archived/model-z" if checked else "scripted/model-a"
        assert stored["engine"] == f"llm-vision:{expected_model}"
        typed_pages = document(after, "family-history-2008.pdf")
        assert calls_for(after, typed_pages, "transcribe") == {}
        assert transcription_of(typed_pages).engine == TEXT_LAYER
        asked = {
            event.get("key")
            for event in this_run(after, "cache-hit", "cache-miss")
        }
        assert keys["family-history-2008.pdf"] not in asked
        assert (keys["birth-extract-1938.pdf"] in asked) is checked
        expected_calls = [f"{LETTER_SHA1}.png"]
        if not checked:
            expected_calls.append("birth-extract-1938.pdf")
        assert sorted(reader.names()) == sorted(expected_calls)
