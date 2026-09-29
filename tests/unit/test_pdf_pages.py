"""PDFs read page by page (#176): the transcription route, the images a
PDF places within its pages, the views, and the LLM cache's keys.

A PDF with a page that reads from its text layer is read page by page:
those pages without a model call, its other pages by vision, and each
image placed within a text-layer page by vision on its own, as a witness
of its own (#178, ADR-0008). A PDF with no such page is read whole, with
the call it was always read with, so the cache, and the transcription
seed, answer it as before. The orchestrator runs with its real client
wiring (tests/unit/test_llmcache.py's CachedRun), and a scripted engine
reads each file a vision call gives it. Every PDF is fictional
(tests/fixtures/pdfs.py), its texts the universe's (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs
from tests.unit.test_llmcache import (
    CachedRun,
    Engine,
    pipeline_answers,
)
from tests.unit.test_orchestrate import quiet, this_run
from tests.unit.test_scratch import Corpus, a_run, fakes  # noqa: F401
from tests.unit.test_scratch_estimate import (  # noqa: F401
    engine,
    estimate_of,
    per_stage,
    runs_dir,
)
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import transcription_check
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    FAILED,
    TRANSCRIPTION_VOLATILE,
    DocumentRun,
)
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    derived_transcription,
    read_derived_media,
)
from yizkr.pipeline.scratch import EXIT_OK
from yizkr.pipeline.stages import content_hash, sha1_file
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import (
    TRANSCRIPTION_PROMPT,
    PdfPageTranscriber,
    TranscribedPage,
    Transcription,
)

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
TEXT_LAYER = "pdf-pages:pdf-text-layer/1"


def typed(*paragraphs: str, **options) -> pdfs.Page:
    return pdfs.Page().text(pdfs.lines(*paragraphs), **options)


def scanned() -> pdfs.Page:
    """A scanned page: one JPEG covering it, with an OCR layer drawn
    invisibly over it."""
    page = pdfs.Page().image(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
    return page.text(pdfs.lines(EXTRACT), mode=pdfs.INVISIBLE)


def history(second: pdfs.Page | None = None) -> bytes:
    """The two typed pages of the 2008 family history; a copy may have its
    second page as `second` instead (scanned, say)."""
    return pdfs.pdf([typed(FIRST), second or typed(SECOND)])


def booklet(image: pdfs.Image = LETTER_IMAGE) -> bytes:
    """Our Family from Germany: two typed pages, three blank ones, and
    page 6, with the image of the letter's top above its caption, the
    translation and the commentary."""
    six = pdfs.Page().image(image, 72, 520, 300, 200)
    six.text(pdfs.lines(CAPTION, TRANSLATION, COMMENT), y=500)
    return pdfs.pdf(
        [typed(TITLE), typed(SOFTWARE), pdfs.Page(), pdfs.Page(), pdfs.Page()]
        + [six]
    )


def extract() -> bytes:
    """The birth register extract, a scan with an OCR layer."""
    return pdfs.pdf([scanned()])


class Reader(Engine):
    """The scripted engine, reading the file each vision call gives it
    from `readings`, by the file's name or stem: a whole PDF by its name, a
    page by ``page-<n>``, and an image by its SHA-1. A reading given as a
    list is given in turn, its last one from then on. `read` logs each file
    read, by name, with the SHA-1 of its bytes."""

    def __init__(self, readings: dict[str, str | list[str]], **kwargs):
        super().__init__(pipeline_answers, **kwargs)
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
        return readings.pop(0) if len(readings) > 1 else readings[0]

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


def page_methods(transcription: Transcription) -> list[tuple[int, str]]:
    return [(page.page, page.method) for page in transcription.pages]


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
            assert page.engine.startswith("pdf-text-layer/1 (pypdfium2 ")
        # its claims cite it, by the SHA-1 of its bytes
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert batch.claims
        assert {claim.source.artifact.sha1 for claim in batch.claims} == {
            doc.sha1
        }

    def test_a_scanned_pdf_is_read_whole(self, tmp_path):
        """A PDF none of whose pages reads from its text layer is read by
        the one vision call it was always read with: the PDF itself. Its
        view carries no page provenance."""
        corpus = folder(tmp_path, birth_extract_1938=extract())
        reader = Reader({"birth-extract-1938.pdf": EXTRACT})
        doc = document(run(tmp_path, corpus, reader), "birth-extract-1938.pdf")
        assert reader.names() == ["birth-extract-1938.pdf"]
        assert reader.read[0][1] == doc.sha1
        stored = json.loads(doc.transcription.read_text())
        assert stored["text"] == EXTRACT
        assert stored["engine"] == "llm-vision:scripted/model-a"
        assert "pages" not in stored
        assert not (doc.dir / DERIVED_MEDIA).exists()

    def test_a_mixed_pdf_reads_each_page_by_its_method(self, tmp_path):
        """The typed page from its text layer; the scanned one by vision,
        from the scan's JPEG as the PDF holds it."""
        corpus = folder(tmp_path, family_history=history(scanned()))
        reader = Reader({"page-2": SECOND})
        doc = document(run(tmp_path, corpus, reader), "family-history.pdf")
        assert reader.read == [
            ("page-2.jpg", hashlib.sha1(pdfs.JPEG).hexdigest())
        ]
        transcription = transcription_of(doc)
        assert page_methods(transcription) == [
            (1, "text-layer"),
            (2, "vision"),
        ]
        assert transcription.pages[1].engine == "llm-vision:scripted/model-a"
        assert transcription.page_text(transcription.pages[1]) == SECOND
        first = "\n".join(pdfs.lines(FIRST))
        assert transcription.text == f"[Page 1]\n{first}\n\n[Page 2]\n{SECOND}"
        # the scan's OCR layer is no part of it
        assert "Standesamt" not in transcription.text

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

    def test_blank_pages_are_marked_and_not_read(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        transcription = transcription_of(doc)
        assert page_methods(transcription)[2:5] == [
            (3, "blank"),
            (4, "blank"),
            (5, "blank"),
        ]
        assert "[Page 4]\n\n[Page 5]" in transcription.text
        assert reader.names() == [f"{LETTER_SHA1}.png"]

    def test_a_page_read_by_vision_that_fails_the_check_fails_it(
        self, tmp_path
    ):
        corpus = folder(tmp_path, family_history=history(scanned()))
        reader = Reader({"page-2": REFUSAL})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "family-history.pdf")
        assert doc.state.status == FAILED
        assert "transcription check" in doc.state.error
        assert not doc.transcription.exists()

    def test_a_pdf_pdfium_cannot_read_is_read_whole(self, tmp_path):
        corpus = folder(tmp_path, scan=b"%PDF-1.4 fictional")
        reader = Reader({"scan.pdf": EXTRACT})
        doc = document(run(tmp_path, corpus, reader), "scan.pdf")
        assert reader.names() == ["scan.pdf"]
        assert transcription_of(doc).pages is None


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
        assert kept.text == EXCERPT
        assert kept.language == "de"
        # and the image is recorded as the booklet's derived medium
        record = read_derived_media(doc.dir)
        assert record.source_sha1 == doc.sha1
        assert record.title == "booklet"
        medium = record.media[LETTER_SHA1]
        assert medium.names == ["page 6, image 1"]
        assert medium.slides == [6]
        assert medium.file == f"derived-media/{LETTER_SHA1}.png"

    def test_the_claims_read_from_the_booklet_cite_it_alone(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert {claim.source.artifact.sha1 for claim in batch.claims} == {
            doc.sha1
        }
        # no prompt of its extraction holds what was read from the image
        extraction = [
            prompt for kind, prompt in reader.calls if kind != "vision"
        ]
        assert extraction
        assert not any("Angerring 11" in prompt for prompt in extraction)

    def test_one_image_placed_twice_is_read_once(self, tmp_path):
        page = typed(TITLE).image(LETTER_IMAGE, 72, 400, 300, 120)
        data = pdfs.pdf(
            [page, typed(SOFTWARE).image(LETTER_IMAGE, 72, 400, 300, 120)]
        )
        corpus = folder(tmp_path, booklet=data)
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        assert reader.names() == [f"{LETTER_SHA1}.png"]
        medium = read_derived_media(doc.dir).media[LETTER_SHA1]
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
        assert read_derived_media(doc.dir).media == {}
        assert not (doc.dir / "derived-media").exists()

    def test_an_image_whose_reading_fails_needs_attention(self, tmp_path):
        """The image is left without a transcription of its own, and says
        so in the stage's attention; the booklet's own text goes on to its
        batch."""
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: REFUSAL})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "booklet.pdf")
        assert doc.state.status == AWAITING_CLAIMS
        (done,) = [
            event
            for event in this_run(orchestrator, "done")
            if event["stage"] == "transcription"
        ]
        (reason,) = done["attention"]
        assert reason.startswith(f"page 6, image 1 (sha1 {LETTER_SHA1[:12]})")
        assert "(refusal)" in reason
        assert not derived_transcription(doc.dir, LETTER_SHA1).exists()
        assert LETTER_SHA1 in read_derived_media(doc.dir).media

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
        transcription, _resolution = orchestrator._transcribe(doc, "vision")
        assert not doc.dir.exists()
        reading = transcription.reading
        (image,) = reading.images
        assert image.path.read_bytes() == png(300, 120, 1, LETTER_IMAGE.data)
        assert image.transcription.text == EXCERPT
        reading.close()
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
        (doc,) = driver.documents()
        transcription = transcription_of(doc)
        assert transcription.engine == TEXT_LAYER
        assert transcription.pages[5].images == [LETTER_SHA1]
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        assert json.loads(kept.read_text())["text"] == EXCERPT
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


def stricter_check(monkeypatch, phrase: str) -> None:
    """Make the transcription check stricter, as a change to it would:
    `phrase` becomes a failure's signature."""
    signature = transcription_check._signature(
        "stricter", transcription_check.TOOL_FAILURE, re.escape(phrase)
    )
    monkeypatch.setattr(
        transcription_check,
        "SIGNATURES",
        (*transcription_check.SIGNATURES, signature),
    )


def set_asides(orchestrator: CachedRun) -> list[dict]:
    return this_run(orchestrator, "set-aside")


class TestReuse:
    """A reused view of a PDF read page by page is checked again where
    the vision engine read it, and never where its text layer did."""

    def test_a_stricter_check_reads_a_page_again(self, tmp_path, monkeypatch):
        corpus = folder(tmp_path, family_history=history(scanned()))
        reader = Reader({"page-2": [SECOND, "Klara left Flörburg first."]})
        run(tmp_path, corpus, reader)
        stricter_check(monkeypatch, "followed her in 1939")
        again = run(tmp_path, corpus, reader)
        doc = document(again, "family-history.pdf")
        (aside,) = set_asides(again)
        assert aside["stage"] == "transcription"
        assert aside["reason"].endswith("(page 2)")
        assert reader.names() == ["page-2.jpg", "page-2.jpg"]
        transcription = transcription_of(doc)
        assert transcription.page_text(transcription.pages[1]) == (
            "Klara left Flörburg first."
        )
        assert (doc.dir / "transcription.superseded-1.json").exists()

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
        assert json.loads(kept.read_text())["text"] == "Liebes Pünktchen!"
        superseded = kept.with_name(
            f"{LETTER_SHA1}.transcription.superseded-1.json"
        )
        assert json.loads(superseded.read_text())["text"] == EXCERPT

    def test_a_reused_view_stands_without_a_call(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        reader = RefusingReader({})
        again = run(tmp_path, corpus, reader)
        reused = [
            event
            for event in this_run(again, "reused")
            if event["stage"] == "transcription"
        ]
        assert len(reused) == 1


class TestTheView:
    def test_a_view_made_whole_is_written_as_it_always_was(self):
        """No `pages` key: every transcription made whole keeps its bytes."""
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
                method="text-layer",
                engine="m",
                start=start,
                end=end,
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
        """The batch is made from the text, its language and the artifact:
        how the pages were read is provenance, as the engine is."""
        assert "pages" in TRANSCRIPTION_VOLATILE
        from yizkr.claims import ArtifactRef

        ref = ArtifactRef(sha1="a" * 40)
        text = "[Page 1]\nRahel"
        made = [
            Transcription(
                artifact=ref,
                text=text,
                engine=made_by,
                pages=[
                    TranscribedPage(
                        page=1, method=method, engine=made_by, start=9, end=14
                    )
                ],
            )
            for method, made_by in (
                ("text-layer", TEXT_LAYER),
                ("vision", "llm-vision:scripted/model-a"),
            )
        ]
        hashes = {
            content_hash(
                t.model_dump(mode="json"), volatile=TRANSCRIPTION_VOLATILE
            )
            for t in made
        }
        assert len(hashes) == 1


# -- the keys -----------------------------------------------------------------


def read_whole(monkeypatch) -> None:
    """Read every PDF whole, as the vision route did before PDFs were read
    page by page: the baseline these tests compare against."""
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
    """Only a PDF read page by page makes its transcription, and what
    follows it, again: a scanned PDF's call, and every call after it, keeps
    its key, so the LLM cache answers them (ADR-0032)."""

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

    def test_an_adoption_for_a_scan_is_served(self, tmp_path, monkeypatch):
        """The transcription seed adopts archived readings under the key
        of the call that reads a PDF whole. A scan is still read by that
        call, so its adoption is served; a born-digital PDF no longer
        makes it, and its adoption is left unasked."""
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
            key = keys[name] = client.complete_key(
                TRANSCRIPTION_PROMPT, [corpus / name]
            )
            assert client.seed(
                key,
                reading,
                {"model_identifier": "archived/model-z", "backfilled": True},
            )
        seeder._close_engines()
        reader = Reader({LETTER_SHA1: EXCERPT})
        after = run(tmp_path, corpus, reader, cache_dir=cache, state="after")
        scan = document(after, "birth-extract-1938.pdf")
        assert calls_for(after, scan, "transcribe") == {
            "cache-hit: adopted": 1
        }
        stored = json.loads(scan.transcription.read_text())
        assert stored["text"] == EXTRACT
        assert stored["engine"] == "llm-vision:archived/model-z"
        typed_pages = document(after, "family-history-2008.pdf")
        assert calls_for(after, typed_pages, "transcribe") == {}
        assert transcription_of(typed_pages).engine == TEXT_LAYER
        asked = {
            event.get("key")
            for event in this_run(after, "cache-hit", "cache-miss")
        }
        assert keys["family-history-2008.pdf"] not in asked
        assert reader.names() == [f"{LETTER_SHA1}.png"]
