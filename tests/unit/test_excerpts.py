"""An image a PDF places within one of its text-layer pages, extracted as
a witness of its own (#397, #178, ADR-0008): an excerpt.

Each image the PDF's transcription reads on its own (#176) is a document
of its own once that reading is kept: its own artifact identity by the
SHA-1 of its bytes, its own directory, its own batch, whose claims cite
the image, and at the write an item of its own, `part of` each PDF that
places it. The same image in two PDFs is one excerpt; one whose bytes a
document of the corpus has is that document; one the never-ingest list
names is never one. The ingest run and the scratch driver take excerpts
as their PDFs' transcriptions find them, and the run report counts the
images each PDF places. Every PDF is fictional (tests/fixtures/pdfs.py),
its texts the universe's (docs/EXAMPLES.md): the booklet *Our Family from
Germany* sets the top of Sebastian's 1939 letter (`letter-excerpt-1939`)
beside its translation, which reads the address the letter gives as 11 as
71.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from tests.fixtures import pdfs
from tests.fixtures.records import page_text
from tests.unit.test_orchestrate import this_run
from tests.unit.test_pdf_pages import (
    CAPTION,
    COMMENT,
    EXCERPT,
    LETTER_IMAGE,
    LETTER_SHA1,
    REFUSAL,
    SOFTWARE,
    SOURCES,
    TRANSLATION,
    Raw,
    Reader,
    booklet,
    document,
    excerpt_of,
    folder,
    renderer,  # noqa: F401
    run,
    set_asides,
    transcription_of,
    typed,
)
from tests.unit.test_registrar import (
    ITEMS,
    FakeWiki,
    cited,
    label_of,
    part_of,
    strings,
)
from tests.unit.test_scratch import (  # noqa: F401
    Corpus,
    Recorder,
    a_run,
    events,
    fakes,
    started,
)
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import scratch
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    DUPLICATE,
    EXCERPTS_DIR,
    WRITTEN,
    Orchestrator,
    directory_names,
)
from yizkr.pipeline.orchestrate import (
    EXCERPT as KIND,
)
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    DERIVED_MEDIA_DIR,
    derived_transcription,
    read_derived_media,
)
from yizkr.pipeline.registrar import ExcludedMedia
from yizkr.pipeline.report import build, render_markdown
from yizkr.pipeline.review import accept_all
from yizkr.pipeline.rundir import RunDir
from yizkr.pipeline.scratch import EXIT_OK, CorpusStage
from yizkr.pipeline.textlayer import png

# the booklet's own text on page 6, as its text layer gives it
OWN = "\n".join(pdfs.lines(CAPTION, TRANSLATION, COMMENT))
# the letter's image as the PDF holds it, the bytes its SHA-1 names
LETTER_PNG = png(300, 120, 1, LETTER_IMAGE.data)
# a second image, a photograph's inscription, and what it reads as
INSCRIPTION = pdfs.gray(300, 120, shade=9)
INSCRIPTION_SHA1 = hashlib.sha1(png(300, 120, 1, INSCRIPTION.data)).hexdigest()
POSTCARD = SOURCES["postcard-1938"].text


def reprint(image: pdfs.Image = LETTER_IMAGE) -> bytes:
    """A reprint of the booklet's page 6 in a PDF of its own: the same
    image of the letter's top, with the translation after it."""
    page = pdfs.Page().image(image, 72, 520, 300, 200)
    page.text(pdfs.lines(TRANSLATION), y=500)
    return pdfs.pdf([typed(SOFTWARE), page])


def batch_of(doc) -> ClaimBatch:
    return ClaimBatch.model_validate_json(doc.batch.read_text())


def planned(orchestrator) -> dict[str, str]:
    """SHA-1 -> kind, of every document the run plan holds."""
    return {
        entry["sha1"]: entry["kind"]
        for entry in orchestrator.rundir.manifest()["documents"]
    }


class TestAnExcerpt:
    def test_an_image_is_extracted_as_a_document_of_its_own(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        orchestrator = run(tmp_path, corpus, reader)
        doc = document(orchestrator, "booklet.pdf")
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        # its own identity, directory and title
        assert excerpt.sha1 == LETTER_SHA1
        assert excerpt.kind == KIND
        assert excerpt.dir == tmp_path / "state" / EXCERPTS_DIR / LETTER_SHA1
        assert excerpt.state.title == "booklet — page 6, image 1"
        assert excerpt.state.status == AWAITING_CLAIMS
        assert [c.dir for c in excerpt.containers] == [doc.dir]
        # its transcription is the booklet's reading of it: no second call
        assert reader.read == [(f"{LETTER_SHA1}.png", LETTER_SHA1)]
        transcription = transcription_of(excerpt)
        assert transcription.artifact.sha1 == LETTER_SHA1
        assert transcription.text == page_text(EXCERPT)
        assert transcription.language == "de"
        # its claims cite the image, and the booklet's cite the booklet
        assert {c.source.artifact.sha1 for c in batch_of(excerpt).claims} == {
            LETTER_SHA1
        }
        assert {c.source.artifact.sha1 for c in batch_of(doc).claims} == {
            doc.sha1
        }
        # its extraction reads the letter, never the translation: the two
        # witnesses give the address each its own value
        letter = [
            prompt
            for kind, prompt in reader.calls
            if kind != "vision" and "Angerring 11" in prompt
        ]
        assert letter
        assert not any("Angerring 71" in prompt for prompt in letter)
        # the run plans it once it is found, and its end counts it
        assert planned(orchestrator) == {doc.sha1: "media", LETTER_SHA1: KIND}
        (end,) = this_run(orchestrator, "run-end")
        assert end["statuses"] == {AWAITING_CLAIMS: 2}

    def test_the_same_image_in_two_pdfs_is_one_witness(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet(), reprint=reprint())
        orchestrator = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        first = document(orchestrator, "booklet.pdf")
        second = document(orchestrator, "reprint.pdf")
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        assert [c.dir for c in excerpt.containers] == [first.dir, second.dir]
        assert [path.name for path in excerpt.dir.parent.iterdir()] == [
            LETTER_SHA1
        ]
        extractions = [
            event
            for event in this_run(orchestrator, "started")
            if event["subject"] == LETTER_SHA1
            and event["stage"] == "extraction"
        ]
        assert len(extractions) == 1
        # the report counts the image once per PDF, and once in all
        images = build(tmp_path / "state")["aggregate"]["documents"][
            "pdf_images"
        ]
        assert (images["placed"], images["distinct"]) == (2, 1)

    def test_an_image_a_document_of_the_corpus_is_is_not_extracted_again(
        self, tmp_path
    ):
        """The letter's excerpt is also in the corpus as an image of its
        own, with the same bytes: that document is the witness."""
        corpus = folder(tmp_path, booklet=booklet())
        (corpus / "letter-excerpt-1939.png").write_bytes(LETTER_PNG)
        reader = Reader({LETTER_SHA1: EXCERPT, "letter-excerpt-1939": EXCERPT})
        orchestrator = run(tmp_path, corpus, reader)
        assert orchestrator._excerpts == {}
        assert not (tmp_path / "state" / EXCERPTS_DIR).exists()
        letter = document(orchestrator, "letter-excerpt-1939.png")
        assert {c.source.artifact.sha1 for c in batch_of(letter).claims} == {
            LETTER_SHA1
        }

    def test_an_image_the_never_ingest_list_names_is_no_excerpt(
        self, tmp_path
    ):
        corpus = folder(tmp_path, booklet=booklet())
        orchestrator = run(
            tmp_path,
            corpus,
            Reader({}),
            exclusions=ExclusionList.parse(f"{LETTER_SHA1}\n"),
        )
        assert orchestrator._excerpts == {}
        assert LETTER_SHA1 not in planned(orchestrator)

    def test_the_never_ingest_list_is_asked_as_excerpts_are_found(
        self, tmp_path
    ):
        """An image the list names once the PDF's directory keeps its
        reading is found no more, whatever the directory still holds."""
        corpus = folder(tmp_path, booklet=booklet())
        orchestrator = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        doc = document(orchestrator, "booklet.pdf")
        orchestrator._excerpts = {}
        assert [e.sha1 for e in orchestrator._excerpts_of(doc, ())] == [
            LETTER_SHA1
        ]
        orchestrator._excerpts = {}
        orchestrator.exclusions = ExclusionList.parse(f"{LETTER_SHA1}\n")
        assert orchestrator._excerpts_of(doc, ()) == []

    def test_an_image_without_a_reading_is_no_excerpt(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        orchestrator = run(
            tmp_path, corpus, Reader({LETTER_SHA1: Raw(REFUSAL)})
        )
        assert orchestrator._excerpts == {}
        assert not (tmp_path / "state" / EXCERPTS_DIR).exists()

    def test_a_second_run_reuses_every_view(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        again = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        reused = {
            (event["kind"], event["stage"])
            for event in this_run(again, "reused")
        }
        assert {
            (KIND, stage) for stage in ("transcription", "extraction")
        } <= (reused)
        assert this_run(again, "started") == []

    def test_serial_mode_extracts_the_excerpt_before_the_sequence(
        self, tmp_path
    ):
        corpus = folder(tmp_path, booklet=booklet())
        orchestrator = run(
            tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}), serial=True
        )
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        assert excerpt.batch.exists()
        assert planned(orchestrator)[LETTER_SHA1] == KIND
        # found twice, before the sequence and in the loop, the PDF is one
        # of the PDFs that place the image once
        doc = document(orchestrator, "booklet.pdf")
        assert [c.dir for c in excerpt.containers] == [doc.dir]

    def test_a_written_excerpt_makes_a_later_copy_a_duplicate(self, tmp_path):
        """Written in one run, the excerpt seeds the next run's content
        dedup: a file of the folder with its bytes, added since, is its
        duplicate, and is not extracted or written again (ADR-0010)."""
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT, "letter-excerpt-1939": EXCERPT})
        first = run(tmp_path, corpus, reader)
        for doc in (
            document(first, "booklet.pdf"),
            excerpt_of(first, LETTER_SHA1),
        ):
            accept_all(doc.review, doc.review_reviewed)
        second = run(tmp_path, corpus, reader)
        assert excerpt_of(second, LETTER_SHA1).state.status == WRITTEN
        (corpus / "letter-excerpt-1939.png").write_bytes(LETTER_PNG)
        third = run(tmp_path, corpus, reader)
        assert LETTER_SHA1 in third._prior_written_hashes()
        letter = document(third, "letter-excerpt-1939.png")
        assert letter.state.status == DUPLICATE
        assert letter.state.detail["duplicate_of"] == f"{LETTER_SHA1}.png"
        assert not letter.batch.exists()


class TestReuse:
    """A reused transcription of a PDF read page by page is made again
    when what it marks is not kept whole beside it: its images' record
    and bytes are restored, an image whose reading failed is read again,
    and the never-ingest list is asked again about every image it
    marks."""

    @pytest.mark.parametrize("lost", ["folder", "record", "bytes"])
    def test_a_lost_record_or_image_is_restored(self, tmp_path, lost):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "booklet.pdf")
        if lost == "folder":
            shutil.rmtree(doc.dir / DERIVED_MEDIA_DIR)
        elif lost == "record":
            (doc.dir / DERIVED_MEDIA).unlink()
        else:
            (doc.dir / DERIVED_MEDIA_DIR / f"{LETTER_SHA1}.png").unlink()
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert aside["stage"] == "transcription"
        assert aside["reason"].startswith("the record of the images")
        record = read_derived_media(doc.dir)
        assert list(record.media) == [LETTER_SHA1]
        assert (doc.dir / record.media[LETTER_SHA1].file).read_bytes() == (
            LETTER_PNG
        )
        kept = derived_transcription(doc.dir, LETTER_SHA1)
        assert json.loads(kept.read_text())["text"] == page_text(EXCERPT)
        assert transcription_of(excerpt_of(again, LETTER_SHA1)).text == (
            page_text(EXCERPT)
        )

    def test_an_image_whose_reading_failed_is_read_again(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet())
        reader = Reader({LETTER_SHA1: [Raw(REFUSAL), EXCERPT]})
        first = run(tmp_path, corpus, reader)
        assert first._excerpts == {}
        again = run(tmp_path, corpus, reader)
        (aside,) = set_asides(again)
        assert aside["reason"] == (
            f"image {LETTER_SHA1[:12]} has no transcription of its own, so "
            "it is read again"
        )
        assert reader.names() == [f"{LETTER_SHA1}.png"] * 2
        excerpt = excerpt_of(again, LETTER_SHA1)
        assert {c.source.artifact.sha1 for c in batch_of(excerpt).claims} == {
            LETTER_SHA1
        }

    def test_the_never_ingest_list_is_asked_again(self, tmp_path):
        """An image the list names once its reading was kept: the PDF's
        transcription is made again without it, the record names it no
        more, and it is no excerpt."""
        corpus = folder(tmp_path, booklet=booklet())
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        reader = Reader({})
        again = run(
            tmp_path,
            corpus,
            reader,
            exclusions=ExclusionList.parse(f"{LETTER_SHA1}\n"),
        )
        doc = document(again, "booklet.pdf")
        (aside,) = set_asides(again)
        assert aside["reason"] == (
            f"the never-ingest list names image {LETTER_SHA1[:12]}, which "
            "it marks"
        )
        assert reader.read == []
        assert LETTER_SHA1 not in transcription_of(doc).text
        assert read_derived_media(doc.dir).media == {}
        assert again._excerpts == {}
        assert not [
            event
            for event in again.rundir.events()
            if event.get("subject") == LETTER_SHA1
            and event["invocation"] == again.rundir.invocation
        ]


class TestRegistration:
    """The write registers a PDF's images as it registers a deck's
    (Orchestrator._step_register), on the in-memory wiki."""

    @staticmethod
    def made(tmp_path, **documents):
        corpus = folder(tmp_path, **documents)
        orchestrator = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        wiki = FakeWiki()
        orchestrator._client = wiki
        return orchestrator, wiki

    def test_an_item_per_image_part_of_its_pdf(self, tmp_path):
        orchestrator, wiki = self.made(tmp_path, booklet=booklet())
        doc = document(orchestrator, "booklet.pdf")
        pdf = Orchestrator._step_register(orchestrator, doc)
        (image,) = [
            qid
            for qid in wiki.entities
            if strings(wiki, qid, "media_sha1") == [LETTER_SHA1]
        ]
        entity = wiki.entities[image]
        assert label_of(wiki, image) == "booklet — page 6, image 1"
        assert entity["descriptions"]["en"]["value"] == (
            "image placed within the PDF (page 6)"
        )
        assert part_of(entity) == {(pdf, "6"): [pdf]}
        assert strings(wiki, image, "instance_of") == [ITEMS["photograph"]]
        # every statement the PDF makes of its image is stated in the PDF
        for statements in entity["claims"].values():
            for statement in statements:
                assert cited(statement) == [pdf]
        (name,) = [n for n, sha in wiki.files.items() if sha == LETTER_SHA1]
        assert name.endswith("page 6, image 1.png")
        assert wiki.drift() == []

        # the excerpt's write finds its item, and registers nothing new
        writes = wiki.writes
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        assert Orchestrator._step_register(orchestrator, excerpt) == image
        assert wiki.writes == writes

    def test_an_excerpt_s_write_registers_each_pdf_that_places_it(
        self, tmp_path
    ):
        orchestrator, wiki = self.made(
            tmp_path, booklet=booklet(), reprint=reprint()
        )
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        image = Orchestrator._step_register(orchestrator, excerpt)
        pdfs_ = {
            doc.source.name: strings(wiki, qid, "media_sha1")
            for doc in excerpt.containers
            for qid in wiki.entities
            if strings(wiki, qid, "media_sha1") == [doc.sha1]
        }
        assert sorted(pdfs_) == ["booklet.pdf", "reprint.pdf"]
        wholes = {whole for whole, _page in part_of(wiki.entities[image])}
        assert len(wholes) == 2
        assert {page for _whole, page in part_of(wiki.entities[image])} == {
            "6",
            "2",
        }

    def test_a_record_made_for_other_bytes_registers_nothing(self, tmp_path):
        orchestrator, _wiki = self.made(tmp_path, booklet=booklet())
        doc = document(orchestrator, "booklet.pdf")
        assert orchestrator._placed_images(doc) is not None
        record = json.loads((doc.dir / DERIVED_MEDIA).read_text())
        record["source_sha1"] = "b" * 40
        (doc.dir / DERIVED_MEDIA).write_text(json.dumps(record))
        assert orchestrator._placed_images(doc) is None

    def test_an_excerpt_the_list_names_now_is_refused(self, tmp_path):
        orchestrator, wiki = self.made(tmp_path, booklet=booklet())
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        orchestrator.exclusions = ExclusionList.parse(f"{LETTER_SHA1}\n")
        with pytest.raises(ExcludedMedia):
            Orchestrator._step_register(orchestrator, excerpt)
        assert not [
            qid
            for qid in wiki.entities
            if strings(wiki, qid, "media_sha1") == [LETTER_SHA1]
        ]


class TestAScratchRun:
    @staticmethod
    def corpus(tmp_path, **documents: bytes) -> Corpus:
        corpus = Corpus(tmp_path / "corpus")
        for name, data in documents.items():
            (corpus.folder / name).write_bytes(data)
        corpus.texts["a-booklet.pdf"] = OWN
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        return corpus

    def test_the_driver_extracts_the_excerpt_after_its_pdf_s_reading(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        """The first excerpt found is the excerpts' canary: it takes its
        stages as soon as its PDF's transcription has found it."""
        corpus = self.corpus(tmp_path, **{"a-booklet.pdf": booklet()})
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        pdf = corpus.sha1("a-booklet.pdf")
        assert started(driver.state_dir) == [
            (pdf, "transcription"),
            (LETTER_SHA1, "transcription"),
            (LETTER_SHA1, "extraction"),
            (LETTER_SHA1, "critique"),
            (pdf, "extraction"),
            (pdf, "critique"),
        ]
        # the image is read once, by the PDF's transcription
        assert fakes.calls.count(("vision", f"{LETTER_SHA1}.png")) == 1
        manifest = RunDir(driver.state_dir).manifest()
        assert {d["sha1"]: d["kind"] for d in manifest["documents"]} == {
            pdf: "media",
            LETTER_SHA1: KIND,
        }
        assert manifest["scratch"]["canaries"] == [pdf, LETTER_SHA1]
        excerpt = driver.state_dir / EXCERPTS_DIR / LETTER_SHA1
        batch = ClaimBatch.model_validate_json(
            (excerpt / "batch.json").read_text()
        )
        assert {c.source.artifact.sha1 for c in batch.claims} == {LETTER_SHA1}
        # the barrier takes both documents to the corpus stages
        (barrier,) = events(driver.state_dir, "barrier")
        assert barrier["documents"] == 2

        # the report counts the images each PDF places, as aggregates
        report = build(driver.state_dir)
        documents = report["aggregate"]["documents"]
        assert documents["by_kind"] == {"media": 1, KIND: 1}
        assert documents["pdf_images"] == {
            "pdfs": 1,
            "placed": 1,
            "distinct": 1,
            "per_pdf": {"1": 1},
            "excerpts": 1,
        }
        assert documents["views_not_read"] == 0
        text = render_markdown(report["aggregate"])
        assert (
            "Images placed within PDFs read page by page and read on their "
            "own: 1 in 1 PDFs (1 distinct; images per PDF: 1 in 1); "
            "documents of their own (excerpts): 1"
        ) in text
        assert LETTER_SHA1 not in text

        # invoked again, the run plans the excerpt from its directory, and
        # makes nothing again
        calls = list(fakes.calls)
        assert a_run(tmp_path, corpus, fakes).run() == EXIT_OK
        assert fakes.calls == calls
        manifest = RunDir(driver.state_dir).manifest()
        planned = [d["sha1"] for d in manifest["documents"]]
        assert sorted(planned) == sorted([pdf, LETTER_SHA1])
        # the invocation's end counts both documents
        end = events(driver.state_dir, "run-end", invocation=-1)[-1]
        assert sum(end["statuses"].values()) == 2

    def test_past_the_barrier_alone_the_excerpt_goes_with_its_pdf(
        self,
        tmp_path,
        fakes,  # noqa: F811
        monkeypatch,
    ):
        """An invocation on other code that only takes the run past the
        barrier dispatches no stage, and still plans the excerpt from its
        directory and gives it to the corpus stages."""
        corpus = self.corpus(tmp_path, **{"a-booklet.pdf": booklet()})
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        run_dir = driver.state_dir
        recorded = RunDir(run_dir).manifest()["code"]
        other = dict(recorded, commit="f" * 40, dirty=False)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        clustering = Recorder()
        again = a_run(
            tmp_path,
            corpus,
            fakes,
            corpus_stages=[CorpusStage("clustering", clustering)],
        )
        assert again.run() == EXIT_OK
        manifest = RunDir(run_dir).manifest()
        assert manifest["scratch"]["past_barrier_only"] is True
        assert started(run_dir, invocation=-1) == []
        (context,) = clustering.contexts
        assert {doc.sha1 for doc in context.documents} == {
            corpus.sha1("a-booklet.pdf"),
            LETTER_SHA1,
        }
        assert LETTER_SHA1 in {d["sha1"] for d in manifest["documents"]}

    def test_an_image_a_document_of_the_corpus_is_is_no_excerpt(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        """The booklet's image is also a file of the corpus, with the same
        bytes: that document is the witness, and the driver takes no
        excerpt for it."""
        corpus = Corpus(tmp_path / "corpus")
        (corpus.folder / "a-booklet.pdf").write_bytes(booklet())
        (corpus.folder / "letter-excerpt-1939.png").write_bytes(LETTER_PNG)
        corpus.texts["a-booklet.pdf"] = OWN
        # the two readings of the same bytes differ by a line, so that the
        # fake engine tells the prompts apart; the cache answers the second
        # reading with the first either way
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT + "\nOne reading."
        corpus.texts["letter-excerpt-1939.png"] = EXCERPT + "\nAnother."
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        assert driver._excerpts == {}
        assert not (driver.state_dir / EXCERPTS_DIR).exists()
        kinds = {
            d["kind"] for d in RunDir(driver.state_dir).manifest()["documents"]
        }
        assert kinds == {"media"}

    def test_excerpts_is_no_document_s_directory(self, tmp_path):
        """A file of the folder named for the excerpts' directory gets a
        directory of another name."""
        (tmp_path / "corpus").mkdir()
        source = tmp_path / "corpus" / f"{EXCERPTS_DIR}.txt"
        source.write_text(SOURCES["postcard-1938"].text)
        names = directory_names([source], tmp_path / "state")
        assert names[source] != EXCERPTS_DIR

    def test_the_other_excerpts_take_their_stages_rank_by_rank(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        corpus = self.corpus(
            tmp_path,
            **{
                "a-booklet.pdf": booklet(),
                "b-reprint.pdf": reprint(INSCRIPTION),
            },
        )
        corpus.texts["b-reprint.pdf"] = "\n".join(pdfs.lines(SOFTWARE))
        corpus.texts[f"{INSCRIPTION_SHA1}.png"] = POSTCARD
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        second = corpus.sha1("b-reprint.pdf")
        order = started(driver.state_dir)[6:]
        assert order == [
            (second, "transcription"),
            (INSCRIPTION_SHA1, "transcription"),
            (second, "extraction"),
            (INSCRIPTION_SHA1, "extraction"),
            (second, "critique"),
            (INSCRIPTION_SHA1, "critique"),
        ]
        report = build(driver.state_dir)["aggregate"]["documents"]
        assert report["pdf_images"]["per_pdf"] == {"1": 2}
        assert report["pdf_images"]["excerpts"] == 2
