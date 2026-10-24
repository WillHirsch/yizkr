"""The machine-assisted marker (ADR-0008, follow-up B; #666).

A compilation's statement about how its own text was prepared is read
once per document, by the entity manifest, or from the passage a curator
points to, and the pipeline puts `sourcing_circumstances` ->
`machine_assisted` on every claim read from the text the statement
covers. The tests read the fictional universe's compilation, *Our Family
from Germany* (docs/EXAMPLES.md): its page 2 says its translations were
done with translation software, so the claims read from its translation
of the 1939 letter carry the marker, and those read from its title page,
its caption and its commentary do not.
"""

import json
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from tests.unit.test_orchestrate import make_run
from tests.unit.test_pipeline import MEDIA_SHA1, PROPERTIES, RUN
from tests.unit.test_pipeline import FakeWiki as PipelineWiki
from tests.unit.test_run_report import (
    CERT,
    DIRECTORY,
    FILES,
    NOW,
    build,
    drilled,
    record,
    render_markdown,
)
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.verbatim import Transcript
from yizkr.model.schema import load_manifest
from yizkr.pipeline import chunking, preparation
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.estimate import _Extractor, _Keyed, _Tally
from yizkr.pipeline.extract import vocabulary_prompt_lines
from yizkr.pipeline.orchestrate import FAILED, DocumentRun, slugify
from yizkr.pipeline.transcribe import (
    PageProvenance,
    TranscribedPage,
    Transcription,
)
from yizkr.pipeline.writer import BatchWriter

SOURCES = {source.key: source for source in u.SOURCES}
BOOKLET = SOURCES["family-booklet-2011"]
LETTER = SOURCES["letter-excerpt-1939"]
MARKED = ("sourcing_circumstances", "machine_assisted")
STATEMENT = (
    "All translations in this booklet were done with translation software"
)


def line_of(source: u.Source, text: str) -> int:
    """The number of the line of the source's text that holds `text`."""
    (number,) = [
        n
        for n, line in enumerate(source.text.splitlines(), start=1)
        if text in line
    ]
    return number


TRANSLATION = line_of(BOOKLET, "Translation:")


def transcription(source: u.Source) -> Transcription:
    return Transcription(
        artifact=ArtifactRef.from_bytes(source.text.encode()),
        text=source.text,
        engine="test",
        language=source.lang,
    )


def said(source: u.Source) -> list[u.Assertion]:
    """What the universe says the source asserts, each with an excerpt."""
    return [a for a in u.ASSERTIONS if a.source == source.key and a.excerpt]


def claim(ref: ArtifactRef, excerpt: str | None, year: int, **extra) -> dict:
    """A chunk's claim read from `excerpt`; each year apart, so that no
    two claims are one."""
    source = {"artifact": ref.model_dump()}
    if excerpt is not None:
        source["transcription_excerpt"] = {"text": excerpt, "language": "en"}
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Gustav Löbig",
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": source,
    } | extra


MODEL_MARKER = {
    "property": "sourcing_circumstances",
    "value": {
        "type": "item",
        "value": {"kind": "vocab", "key": "machine_assisted"},
    },
}


def answer(claims: list[dict]) -> str:
    return "\n".join([*map(json.dumps, claims), json.dumps({"done": True})])


def manifest(reading=None, *, given: bool = True) -> str:
    """The entity manifest's answer, with its reading of the statement
    when `given`."""
    found = {
        "entities": [
            {
                "id": "e1",
                "label": "Gustav Löbig",
                "kind": "human",
                "mention_lines": [TRANSLATION],
            }
        ]
    }
    if given:
        found["preparation"] = reading
    return json.dumps(found)


class Scripted:
    """An engine without conversations: the manifest's answer, then the
    one chunk's; each prompt recorded."""

    model_identifier = "fake-model"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def extract(source, reading, claims, tmp_path, *, given=True, **options):
    """The batch of `source`, its manifest answering `reading`, its chunk
    `claims`, and the extractor."""
    engine = Scripted(manifest(reading, given=given), answer(claims))
    extractor = ChunkedClaimExtractor(
        engine,
        state_dir=tmp_path / "state",
        read_preparation=True,
        **options,
    )
    made = transcription(source)
    return extractor.extract(made, made.artifact), extractor, engine


def marked(batch: ClaimBatch) -> list[bool]:
    return [
        any(
            q.property == "sourcing_circumstances"
            and q.value.value.key == "machine_assisted"
            for q in c.qualifiers
        )
        for c in batch.claims
    ]


def side_output(tmp_path, name: str):
    path = tmp_path / "state" / name
    return json.loads(path.read_text()) if path.exists() else None


THE_TRANSLATIONS = {
    "statement": STATEMENT,
    "covers": [{"first_line": TRANSLATION, "last_line": TRANSLATION}],
}


class TestTheManifest:
    def test_the_marker_is_a_sourcing_circumstance(self):
        manifest = load_manifest()
        item = manifest.items["machine_assisted"]
        assert item.label == "machine-assisted"
        accepts = manifest.properties["sourcing_circumstances"].accepts
        assert accepts == ["circa", "machine_assisted"]

    def test_no_prompt_offers_it(self):
        # the pipeline sets it, never the extractor claim by claim
        assert not load_manifest().items["machine_assisted"].offered
        _, items = vocabulary_prompt_lines()
        listed = {key.strip() for key in items.split(",")}
        assert "circa" in listed
        assert "machine_assisted" not in listed

    def test_the_universe_plans_no_vocabulary_now(self):
        assert u.PLANNED_VOCABULARY == ()


class TestTheBooklet:
    def test_the_translation_alone_carries_the_marker(self, tmp_path):
        # every claim the universe has the booklet make, as its chunk
        # gives it: marked exactly where the universe marks it
        assertions = said(BOOKLET)
        ref = transcription(BOOKLET).artifact
        claims = [
            claim(ref, a.excerpt, 1900 + n) for n, a in enumerate(assertions)
        ]
        batch, extractor, engine = extract(
            BOOKLET, THE_TRANSLATIONS, claims, tmp_path
        )
        assert len(batch.claims) == len(assertions)
        by_year = {
            c.value.year: m
            for c, m in zip(batch.claims, marked(batch), strict=True)
        }
        expected = {
            1900 + n: MARKED in a.qualifiers for n, a in enumerate(assertions)
        }
        assert by_year == expected
        assert any(expected.values()) and not all(expected.values())
        assert extractor.attention == []
        # one reading, in the manifest's one call: no call of its own
        assert len(engine.prompts) == 2
        assert preparation.MANIFEST_BLOCK.splitlines()[1] in engine.prompts[0]
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert coverage == {
            "read": True,
            "pointed": False,
            "statement": True,
            "covers": "passages",
            "passages": 1,
            "marked": sum(expected.values()),
            "straddling": 0,
            "unlocated": 0,
            "dropped": 0,
        }
        # the statement is kept locally, beside the batch
        assert side_output(tmp_path, "preparation.json") == {
            "statement": STATEMENT,
            "lines": [2, 2],
            "covers": [{"first_line": TRANSLATION, "last_line": TRANSLATION}],
            "pointed": None,
        }

    def test_a_statement_that_names_no_part_marks_every_claim(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [
            claim(ref, "Stupfield, 2011", 1901),
            claim(ref, "Wastl to Klara, July 1939.", 1902),
            claim(ref, None, 1903),
        ]
        batch, extractor, _ = extract(
            BOOKLET,
            {"statement": STATEMENT, "covers": "all"},
            claims,
            tmp_path,
        )
        assert marked(batch) == [True, True, True]
        assert extractor.attention == []

    def test_no_statement_marks_nothing(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, extractor, _ = extract(
            BOOKLET, {"statement": None}, claims, tmp_path
        )
        assert marked(batch) == [False]
        assert extractor.attention == []
        assert side_output(tmp_path, "preparation.json") is None
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert (coverage["read"], coverage["statement"]) == (True, False)

    def test_a_marker_a_chunk_gives_is_dropped_and_counted(self, tmp_path):
        # the commentary is no translation: the chunk's marker on it goes,
        # and the reading's marker on the translation stands, once
        ref = transcription(BOOKLET).artifact
        claims = [
            claim(
                ref,
                "Gustl was Ursula's younger brother",
                1901,
                qualifiers=[MODEL_MARKER],
            ),
            claim(
                ref,
                "Gustl finally has his visa",
                1902,
                qualifiers=[MODEL_MARKER],
            ),
        ]
        batch, _, _ = extract(BOOKLET, THE_TRANSLATIONS, claims, tmp_path)
        assert marked(batch) == [False, True]
        assert [len(c.qualifiers) for c in batch.claims] == [0, 1]
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert (coverage["marked"], coverage["dropped"]) == (1, 2)

    def test_a_claim_not_placed_in_or_out_is_not_marked(self, tmp_path):
        # "Gustl" stands in the translation and in the commentary, and a
        # claim with no excerpt stands nowhere: neither is marked, and the
        # batch says why
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Gustl", 1901), claim(ref, None, 1902)]
        batch, extractor, _ = extract(
            BOOKLET, THE_TRANSLATIONS, claims, tmp_path
        )
        assert marked(batch) == [False, False]
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert (coverage["straddling"], coverage["unlocated"]) == (1, 1)
        (why,) = extractor.attention
        assert why.startswith("2 claims not placed")

    def test_the_letter_s_own_claims_never_carry_it(self, tmp_path):
        # the letter is a witness of its own, with its own batch: the
        # booklet's statement is not in its text, and marks none of them
        assert STATEMENT not in LETTER.text
        ref = transcription(LETTER).artifact
        assertions = said(LETTER)
        claims = [
            claim(ref, a.excerpt, 1900 + n) for n, a in enumerate(assertions)
        ]
        batch, _, _ = extract(LETTER, {"statement": None}, claims, tmp_path)
        assert not any(marked(batch))
        assert not any(MARKED in a.qualifiers for a in assertions)


class TestAnUnreadStatement:
    def test_an_answer_without_the_reading_marks_nothing(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Gustl finally has his visa", 1901)]
        batch, extractor, _ = extract(
            BOOKLET, None, claims, tmp_path, given=False
        )
        assert marked(batch) == [False]
        assert extractor.attention == []
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert coverage["read"] is False

    @pytest.mark.parametrize(
        "reading, problem",
        [
            # never inferred: the statement must be the document's words
            (
                {"statement": "translated by machine", "covers": "all"},
                "the statement is not in the transcription",
            ),
            (
                {
                    "statement": STATEMENT,
                    "covers": [{"first_line": 4, "last_line": 3}],
                },
                "lines 4-3 are no passage",
            ),
            (
                {
                    "statement": STATEMENT,
                    "covers": [{"first_line": 4, "last_line": 99}],
                },
                "lines 4-99 are no passage",
            ),
            ({"statement": STATEMENT}, "'covers' is neither"),
            ({"statement": STATEMENT, "covers": []}, "'covers' is neither"),
            ({"statement": STATEMENT, "covers": "all", "why": "-"}, "unknown"),
            ("yes", "no object with a 'statement' key"),
            ({"statement": ""}, "neither a quote nor null"),
        ],
    )
    def test_an_unusable_reading_marks_nothing_and_says_why(
        self, tmp_path, reading, problem
    ):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Gustl finally has his visa", 1901)]
        batch, extractor, _ = extract(BOOKLET, reading, claims, tmp_path)
        assert marked(batch) == [False]
        (why,) = extractor.attention
        assert why.startswith("machine-assisted preparation unread")
        assert problem in why
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert coverage["read"] is False

    def test_an_extractor_that_reads_none_asks_nothing(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Gustl", 1901, qualifiers=[MODEL_MARKER])]
        engine = Scripted(manifest(THE_TRANSLATIONS), answer(claims))
        extractor = ChunkedClaimExtractor(engine, state_dir=tmp_path / "state")
        made = transcription(BOOKLET)
        batch = extractor.extract(made, made.artifact)
        assert (
            preparation.MANIFEST_BLOCK.splitlines()[1]
            not in (engine.prompts[0])
        )
        # the claims as the chunk gave them
        assert marked(batch) == [True]
        assert "machine_assisted" not in side_output(tmp_path, "coverage.json")


class TestACuratorsPointer:
    def test_the_manifest_reads_the_passage_pointed_to(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, extractor, engine = extract(
            BOOKLET,
            THE_TRANSLATIONS,
            claims,
            tmp_path,
            pointer=STATEMENT + "\n",
        )
        assert marked(batch) == [True]
        assert f"\n{STATEMENT}\n" in engine.prompts[0]
        assert (
            "A curator has pointed to the passage below" in (engine.prompts[0])
        )
        coverage = side_output(tmp_path, "coverage.json")["machine_assisted"]
        assert coverage["pointed"] is True
        assert side_output(tmp_path, "preparation.json")["pointed"] == (
            STATEMENT
        )

    def test_a_statement_elsewhere_than_the_pointer_is_unusable(
        self, tmp_path
    ):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, extractor, _ = extract(
            BOOKLET,
            {"statement": "Translation: Flörburg", "covers": "all"},
            claims,
            tmp_path,
            pointer=STATEMENT,
        )
        assert marked(batch) == [False]
        (why,) = extractor.attention
        assert "not the passage the curator pointed to" in why

    def test_a_pointer_read_as_no_statement_says_so(self, tmp_path):
        # the curator only points: the document's words decide
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, extractor, _ = extract(
            BOOKLET,
            {"statement": None},
            claims,
            tmp_path,
            pointer="any mistakes are mine",
        )
        assert marked(batch) == [False]
        (why,) = extractor.attention
        assert "read as saying nothing of machine assistance" in why

    def test_a_pointer_the_answer_leaves_unread_says_so(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        _, extractor, _ = extract(
            BOOKLET, None, claims, tmp_path, given=False, pointer=STATEMENT
        )
        (why,) = extractor.attention
        assert "gave no reading of the statement" in why

    def test_a_pointer_to_words_the_text_does_not_hold_fails(self, tmp_path):
        engine = Scripted()
        extractor = ChunkedClaimExtractor(
            engine,
            read_preparation=True,
            pointer="translated by machine",
        )
        made = transcription(BOOKLET)
        with pytest.raises(ValueError, match="not in the transcription"):
            extractor.extract(made, made.artifact)
        assert engine.prompts == []


class TestTheParse:
    def test_the_statement_s_lines_are_where_it_stands(self):
        reading, problems = preparation.parse(
            {"statement": STATEMENT, "covers": "all"},
            Transcript(BOOKLET.text),
            len(BOOKLET.text.splitlines()),
        )
        assert problems == []
        assert reading.lines == (2, 2)

    def test_a_statement_read_as_the_text_reads_it(self):
        # compared as core/verbatim.py compares an excerpt: case and
        # whitespace aside
        reading, problems = preparation.parse(
            {
                "statement": "all  translations IN this booklet",
                "covers": "all",
            },
            Transcript(BOOKLET.text),
            len(BOOKLET.text.splitlines()),
        )
        assert problems == [] and reading.statement is not None


class TestTheDriver:
    def test_the_pointer_file_is_the_module_s(self, tmp_path):
        doc = DocumentRun(tmp_path / "letter.txt", tmp_path / "state")
        assert doc.pointer.name == preparation.POINTER
        assert doc.pointed() is None
        doc.dir.mkdir(parents=True)
        doc.pointer.write_text("Rahel was born", encoding="utf-8")
        assert doc.pointed() == "Rahel was born"

    def test_the_extraction_reads_it_with_the_pointer(
        self, tmp_path, monkeypatch
    ):
        made: list[dict] = []
        original = ChunkedClaimExtractor.__init__

        def recorded(self, *args, **kwargs):
            made.append(kwargs)
            original(self, *args, **kwargs)

        monkeypatch.setattr(
            chunking.ChunkedClaimExtractor, "__init__", recorded
        )
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert [(k["read_preparation"], k["pointer"]) for k in made] == [
            (True, None)
        ]
        # a pointer is an input of the batch: it is made again with it
        doc.pointer.write_text("Rahel was born", encoding="utf-8")
        make_run(tmp_path).run()
        assert [(k["read_preparation"], k["pointer"]) for k in made][1:] == [
            (True, "Rahel was born")
        ]

    def test_a_pointer_the_text_does_not_hold_fails_the_document(
        self, tmp_path
    ):
        make_run(tmp_path).run()
        orchestrator = make_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.pointer.write_text("translated by machine", encoding="utf-8")
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert doc.state.status == FAILED
        assert "not in the transcription" in doc.state.error

    def test_the_estimate_keys_the_manifest_as_the_driver_asks_it(self):
        class Client:
            config_hash = "-"

        extractor = _Extractor(Client(), _Keyed(Client()), _Tally())
        assert extractor.read_preparation is True


class TestTheRunReport:
    def test_the_readings_and_markers_are_counted(self, tmp_path):
        """The run report counts each extraction's reading and the claims
        it marked, and counts apart, never as zero, the extractions whose
        coverage gives none; it names no statement."""
        path = record(tmp_path / "run")
        before = build(path, now=NOW)["aggregate"]["content"]
        extractions = before["rejects"]["extractions"]
        assert before["machine_assisted"] == dict.fromkeys(
            (
                "extractions",
                "unread",
                "statements",
                "pointed",
                "all",
                "marked",
                "straddling",
                "unlocated",
                "dropped",
            ),
            0,
        ) | {"absent": extractions}
        readings = {
            CERT: {
                "read": True,
                "pointed": True,
                "statement": True,
                "covers": "passages",
                "passages": 1,
                "marked": 5,
                "straddling": 1,
                "unlocated": 2,
                "dropped": 1,
            },
            DIRECTORY: {
                "read": False,
                "pointed": False,
                "statement": False,
                "covers": None,
                "passages": 0,
                "marked": 0,
                "straddling": 0,
                "unlocated": 0,
                "dropped": 0,
            },
        }
        for sha1, reading in readings.items():
            covered = (
                path
                / slugify(Path(FILES[sha1][0]).stem)
                / "extraction"
                / "coverage.json"
            )
            written = json.loads(covered.read_text())
            covered.write_text(
                json.dumps(written | {"machine_assisted": reading})
            )
        made = build(path, now=NOW)
        assert made["aggregate"]["content"]["machine_assisted"] == {
            "extractions": 2,
            "unread": 1,
            "statements": 1,
            "pointed": 1,
            "all": 0,
            "marked": 5,
            "straddling": 1,
            "unlocated": 2,
            "dropped": 1,
            "absent": extractions - 2,
        }
        assert (
            "Statements of machine-assisted preparation: 1 (covering all "
            "the text: 0; pointed to by a curator: 1), marking 5 claims; "
            "not placed: 3; markers given by chunks and dropped: 1; "
            "extractions unread: 1 (extractions whose coverage predates "
            f"the count: {extractions - 2})."
        ) in render_markdown(made["aggregate"])
        assert (
            "Machine-assisted preparation: a statement covering passages, "
            "pointed to by a curator; claims marked: 5, not placed: 3; "
            "markers given by chunks and dropped: 1"
        ) in drilled(made, CERT)
        assert "Machine-assisted preparation: unread" in drilled(
            made, DIRECTORY
        )

        # two readings unread are two
        covered = (
            path
            / slugify(Path(FILES[CERT][0]).stem)
            / "extraction"
            / "coverage.json"
        )
        written = json.loads(covered.read_text())
        covered.write_text(
            json.dumps(written | {"machine_assisted": readings[DIRECTORY]})
        )
        again = build(path, now=NOW)["aggregate"]["content"]
        assert again["machine_assisted"]["unread"] == 2
        assert again["machine_assisted"]["statements"] == 0


def paged(source: u.Source) -> Transcription:
    """The source's text read page by page, each `[page N]` the marker of
    a page, its line the page's own; the booklet's page 6 prints 4."""
    text, pages = "", []
    printed = {6: "4"} if source is BOOKLET else {}
    for line in source.text.splitlines():
        if line.startswith("[page "):
            number = int(line[len("[page ") : line.index("]")])
            body = line[line.index("]") + 2 :]
            opening = ("\n" if text else "") + f"[Page {number}]\n"
            start = len(text) + len(opening)
            text += opening + body
            pages.append(
                TranscribedPage(
                    page=number,
                    start=start,
                    end=len(text),
                    printed_page=printed.get(number),
                    status="transcribed",
                    provenance=PageProvenance(method="text-layer", engine="t"),
                )
            )
        else:
            text += "\n" + line
            pages[-1] = pages[-1].model_copy(update={"end": len(text)})
    return Transcription(
        artifact=ArtifactRef.from_bytes(source.text.encode()),
        text=text,
        engine="pdf-pages:test",
        language=source.lang,
        pages=pages,
    )


def line_in(text: str, part: str) -> int:
    (number,) = [
        n for n, line in enumerate(text.splitlines(), 1) if part in line
    ]
    return number


class TestTheStatementIsCited:
    """The marker's evidence is written: each marked claim carries where
    the document makes its statement, beside its own citation."""

    def extract(self, tmp_path, claims, covers=None):
        made = paged(BOOKLET)
        translation = line_in(made.text, "Translation:")
        reading = {
            "statement": STATEMENT,
            "covers": covers
            or [{"first_line": translation, "last_line": translation}],
        }
        engine = Scripted(manifest(reading), answer(claims))
        extractor = ChunkedClaimExtractor(
            engine, state_dir=tmp_path / "state", read_preparation=True
        )
        return extractor.extract(made, made.artifact), made

    def test_a_marked_claim_cites_the_statement_on_its_page(self, tmp_path):
        ref = paged(BOOKLET).artifact
        when = {
            "property": "point_in_time",
            "value": {"type": "time", "year": 1939, "month": 7, "day": 28},
        }
        translated = claim(
            ref, "Rahel now lives at Angerring 71", 1901, qualifiers=[when]
        )
        translated["source"]["page_number"] = "6"
        claims = [
            translated,
            claim(ref, "Gustl was Ursula's younger brother", 1902),
        ]
        batch, made = self.extract(tmp_path, claims)
        translated, commentary = batch.claims
        assert marked(batch) == [True, False]
        # its own qualifier and citation stand beside the marker
        assert [q.property for q in translated.qualifiers] == [
            "point_in_time",
            "sourcing_circumstances",
        ]
        assert translated.source.page_number == "6"
        assert translated.source.printed_page_number == "4"
        assert translated.source.transcription_excerpt.text == (
            "Rahel now lives at Angerring 71"
        )
        stated = translated.circumstance_source
        assert stated.artifact == made.artifact
        assert (stated.page_number, stated.printed_page_number) == ("2", None)
        assert stated.transcription_excerpt.text == STATEMENT
        assert stated.transcription_excerpt.language == "en"
        assert commentary.circumstance_source is None
        # it survives the batch's serialisation; an unmarked claim's
        # serialisation names no such field
        again = ClaimBatch.model_validate_json(batch.model_dump_json())
        assert again.claims[0].circumstance_source == stated
        assert "circumstance_source" not in (
            commentary.model_dump(mode="json")
        )

    def test_every_passage_the_statement_names_is_marked(self, tmp_path):
        made = paged(BOOKLET)
        ref = made.artifact
        covers = [
            {"first_line": n, "last_line": n}
            for n in (
                line_in(made.text, "Translation:"),
                line_in(made.text, "Gustl was Ursula"),
            )
        ]
        claims = [
            claim(ref, "Gustl was Ursula's younger brother", 1901),
            claim(ref, "Stupfield, 2011", 1902),
        ]
        batch, _ = self.extract(tmp_path, claims, covers)
        assert marked(batch) == [True, False]

    def test_dropping_a_chunk_s_marker_keeps_its_other_qualifiers(
        self, tmp_path
    ):
        ref = paged(BOOKLET).artifact
        when = {
            "property": "point_in_time",
            "value": {"type": "time", "year": 1939},
        }
        claims = [
            claim(
                ref,
                "Gustl was Ursula's younger brother",
                1901,
                qualifiers=[MODEL_MARKER, when],
            )
        ]
        batch, _ = self.extract(tmp_path, claims)
        (kept,) = batch.claims
        assert [q.property for q in kept.qualifiers] == ["point_in_time"]

    def test_a_circumstance_cites_the_claim_s_own_artifact(self):
        ref = paged(BOOKLET).artifact
        other = transcription(LETTER).artifact
        given = claim(ref, "Gustl finally has his visa", 1901) | {
            "circumstance_source": {"artifact": other.model_dump()}
        }
        with pytest.raises(ValueError, match="another artifact"):
            ClaimBatch.model_validate(
                {"extraction_run": RUN, "claims": [given]}
            )


class Wiki(PipelineWiki):
    """The pipeline tests' wiki, with the marker in its vocabulary."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        return (
            properties | {"sourcing_circumstances": "P90"},
            items | {"machine_assisted": "Q90"},
        )


def written(excerpt: str, marked_: bool) -> ClaimBatch:
    source = {
        "artifact": {"kind": "artifact", "sha1": MEDIA_SHA1},
        "page_number": "6",
        "transcription_excerpt": {"text": excerpt, "language": "en"},
    }
    found = {
        "subject": {"kind": "local", "local_id": "e1", "label": "Gustav"},
        "property": "nickname",
        "value": {
            "type": "monolingual",
            "value": {"text": "Gustl", "language": "de"},
        },
        "source": source,
    }
    if marked_:
        found |= {
            "qualifiers": [MODEL_MARKER],
            "circumstance_source": {
                "artifact": source["artifact"],
                "page_number": "2",
                "transcription_excerpt": {
                    "text": STATEMENT,
                    "language": "en",
                },
            },
        }
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [found],
            "entities": [{"id": "e1", "label": "Gustav", "kind": "human"}],
        }
    )


def blocks(statement) -> list[tuple]:
    """Each reference block's (stated in, page number, excerpt)."""

    def first(block, key):
        snaks = block.snaks.get(PROPERTIES[key]) or []
        if not snaks:
            return None
        value = snaks[0].datavalue["value"]
        return (
            value.get("id") or value.get("text")
            if isinstance(value, dict)
            else value
        )

    return [
        (
            first(block, "stated_in"),
            first(block, "page_number"),
            first(block, "transcription_excerpt"),
        )
        for block in statement.references.references
    ]


class TestTheWriter:
    def test_the_statement_is_a_reference_block_of_its_own(self):
        wiki = Wiki({MEDIA_SHA1: "Q23"})
        batch = written("Gustl finally has his visa", True)
        result = write_mapped(BatchWriter(wiki), batch)
        assert result.written == 1
        person = wiki.wbi.item.get(result.created_items["e1"])
        (statement,) = person.claims.get(PROPERTIES["nickname"])
        assert [b for b in blocks(statement) if b[0] is not None] == [
            ("Q23", "6", "Gustl finally has his visa"),
            ("Q23", "2", STATEMENT),
        ]
        # a re-run adds nothing
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.written, again.skipped) == (0, 1)
        (statement,) = person.claims.get(PROPERTIES["nickname"])
        assert len(statement.references.references) == 3

    def test_a_statement_that_lacks_the_block_gains_it(self):
        # the statement carries the claim's own source block already, and
        # not yet the block of the statement its marker stands on
        wiki = Wiki({MEDIA_SHA1: "Q23"})
        batch = written("Gustl finally has his visa", True)
        bare = batch.model_copy(
            update={
                "claims": [
                    batch.claims[0].model_copy(
                        update={"circumstance_source": None}
                    )
                ]
            }
        )
        first = write_mapped(BatchWriter(wiki), bare)
        again = write_mapped(BatchWriter(wiki), batch)
        assert again.referenced == 1
        person = wiki.wbi.item.get(first.created_items["e1"])
        (statement,) = person.claims.get(PROPERTIES["nickname"])
        assert ("Q23", "2", STATEMENT) in blocks(statement)

    def test_identical_values_are_kept_apart_by_the_marker(self):
        wiki = Wiki({MEDIA_SHA1: "Q23"})
        marked_ = written("Gustl finally has his visa", True)
        plain = written("Gustl was Ursula's younger brother", False)
        assert marked_.claims[0].value == plain.claims[0].value
        first = write_mapped(BatchWriter(wiki), marked_)
        write_mapped(BatchWriter(wiki), plain)
        person = wiki.wbi.item.get(first.created_items["e1"])
        statements = person.claims.get(PROPERTIES["nickname"])
        assert len(statements) == 2


class TestTheSamePointedLine:
    def test_a_statement_beside_the_pointed_words_is_refused(self, tmp_path):
        # the curator points to "any mistakes are mine"; the statement
        # about the translation software stands on the same line, outside
        # the pointed words
        assert line_of(BOOKLET, "any mistakes are mine") == line_of(
            BOOKLET, STATEMENT
        )
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, extractor, _ = extract(
            BOOKLET,
            THE_TRANSLATIONS,
            claims,
            tmp_path,
            pointer="any mistakes are mine",
        )
        assert marked(batch) == [False]
        (why,) = extractor.attention
        assert "not the passage the curator pointed to" in why

    def test_a_statement_within_the_pointed_words_is_taken(self, tmp_path):
        ref = transcription(BOOKLET).artifact
        claims = [claim(ref, "Rahel now lives at Angerring 71", 1901)]
        batch, _, _ = extract(
            BOOKLET,
            THE_TRANSLATIONS,
            claims,
            tmp_path,
            pointer=STATEMENT + "; any mistakes are mine.",
        )
        assert marked(batch) == [True]
