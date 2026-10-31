"""Extraction locates claims by the page numbers printed on the pages
(#424, ADR-0037): every extraction prompt shows a page's printed number
beside its physical `[Page N]` marker, on the marker's own line, where
the transcription record gives one; a claim's source keeps the physical
page as its `page number` and gains the printed one, looked up in the
record, never guessed; the writer puts it in the claim's source block,
beside the page number. An excerpt is read from its own transcription,
so it never takes its container's printed page, and its `part of` the
container stays on the container's physical page (ADR-0008).

Every example is the universe's (docs/EXAMPLES.md): the booklet *Our
Family from Germany* numbers its pages from its third, so its physical
page 6, which sets the letter's excerpt beside the translation, is
printed as page 4.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.fixtures import pdfs
from tests.fixtures.identity import write_mapped
from tests.fixtures.records import record_page
from tests.unit.test_chunking import PlainLlm, ScriptedLlm, ndjson
from tests.unit.test_excerpts import batch_of
from tests.unit.test_llmcache import pipeline_answers
from tests.unit.test_pdf_pages import (
    CAPTION,
    COMMENT,
    EXCERPT,
    EXTRACT,
    LETTER_IMAGE,
    LETTER_SHA1,
    SOFTWARE,
    TITLE,
    TRANSLATION,
    Reader,
    document,
    excerpt_of,
    folder,
    reading_location,
    renderer,  # noqa: F401
    run,
    transcription_of,
    typed,
    vision_page,
)
from tests.unit.test_pipeline import PROPERTIES, RUN, FakeWiki
from tests.unit.test_registrar import FakeWiki as RegistrarWiki
from tests.unit.test_registrar import part_of
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    SourceReference,
    validate_against_manifest,
)
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.extract import (
    LlmClaimExtractor,
    cite_artifact,
    claim_schema_prompt_text,
    printed_page_number,
)
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.transcribe import (
    TRANSCRIPTION_PROMPT,
    PageProvenance,
    TranscribedPage,
    Transcription,
)
from yizkr.pipeline.transcription_record import (
    UNCLASSIFIED,
    RecordError,
    TranscriptionRecord,
    parse_record,
    prompt_text,
)
from yizkr.pipeline.writer import BatchWriter

# the booklet's own text on its physical page 6, printed as page 4
SIX = "\n".join([CAPTION, TRANSLATION, COMMENT])
BOOKLET_REF = ArtifactRef.from_bytes(b"Our Family from Germany")
MARKED = '[Page 6] (printed page number "4")'


def booklet_record(**printed: str) -> TranscriptionRecord:
    """The booklet's record, read whole by vision: the title page, the
    note on its translations, three empty pages and page 6; `printed`
    gives a page's printed number by `p<physical page>`, and the record
    gives the rest none."""
    texts = {1: TITLE, 2: SOFTWARE, 6: SIX}
    return TranscriptionRecord.model_validate(
        {
            "document_class": UNCLASSIFIED,
            "pages": [
                record_page(
                    number,
                    texts.get(number, ""),
                    printed_page=printed.get(f"p{number}"),
                )
                for number in range(1, 7)
            ],
        }
    )


def booklet(**printed: str) -> Transcription:
    """The booklet's transcription, its page 6 printed as page 4 unless
    `printed` says otherwise."""
    return Transcription.from_record(
        booklet_record(**(printed or {"p6": "4"})),
        artifact=BOOKLET_REF,
        language="en",
        engine="llm-vision:scripted/model-a",
    )


def page_line(transcription: Transcription, page: int) -> int:
    """The 1-based line of the text a page's marker stands on."""
    return transcription.text.splitlines().index(f"[Page {page}]") + 1


def sibling(page: object = "6", **source) -> dict:
    """The booklet's commentary, as a claim: Gustl was Ursula's younger
    brother, read from `page` (left out when None)."""
    located = {} if page is None else {"page_number": page}
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Gustl",
            "instance_of": "human",
        },
        "property": "sibling",
        "value": {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": "e2",
                "label": "Ursula",
                "instance_of": "human",
            },
        },
        "source": {
            "artifact": BOOKLET_REF.model_dump(mode="json"),
            **located,
            "transcription_excerpt": {"text": COMMENT, "language": "en"},
            **source,
        },
        "confidence": 0.9,
    }


def manifest_json(lines: list[int]) -> str:
    """The booklet's entity manifest: Gustl, mentioned on `lines`, and
    Ursula, on the last of them."""
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": "Gustl",
                    "aliases": [],
                    "kind": "human",
                    "summary": "Ursula's younger brother",
                    "mention_lines": lines,
                },
                {
                    "id": "e2",
                    "label": "Ursula",
                    "aliases": [],
                    "kind": "human",
                    "summary": "Gustl's elder sister",
                    "mention_lines": lines[-1:],
                },
            ]
        }
    )


def segmentation_json(first: int, last: int) -> str:
    return json.dumps(
        {
            "units": [
                {
                    "id": "u1",
                    "label": "page 6",
                    "kind": "narrative section",
                    "first_line": first,
                    "last_line": last,
                }
            ]
        }
    )


class TestThePromptText:
    """Transcription.prompt_text: the text the extraction prompts carry."""

    def test_the_printed_number_stands_beside_its_page_s_marker(self):
        transcription = booklet()
        text = transcription.text
        shown = transcription.prompt_text()
        assert MARKED in shown.splitlines()
        # every other line is the text's, and every line keeps its number
        assert len(shown.splitlines()) == len(text.splitlines())
        changed = [
            (number, line)
            for number, (line, was) in enumerate(
                zip(shown.splitlines(), text.splitlines(), strict=True), 1
            )
            if line != was
        ]
        assert changed == [(page_line(transcription, 6), MARKED)]
        # the transcription, its content, is unchanged
        assert transcription.text == text
        assert "printed page number" not in text

    def test_a_text_with_no_printed_number_is_the_text_itself(self):
        transcription = booklet(p6="")
        assert transcription.printed_pages() == {}
        assert transcription.prompt_text() == transcription.text
        plain = Transcription(artifact=BOOKLET_REF, text=SIX, engine="t")
        assert plain.prompt_text() == SIX

    @pytest.mark.parametrize("blank", ["  ", "\t"])
    def test_a_printed_number_of_whitespace_alone_shows_none(self, blank):
        """No claim is ever given one, so no prompt shows one."""
        transcription = booklet(p6=blank)
        assert transcription.prompt_text() == transcription.text

    def test_a_last_page_of_its_marker_alone_shows_its_number(self):
        """The booklet's back page is blank but printed: the text ends
        with its marker, which still shows the number."""
        page = SimpleNamespace(page=1, start=8, end=8, printed_page="1")
        assert prompt_text("[Page 1]", [page]) == (
            '[Page 1] (printed page number "1")'
        )

    def test_each_page_shows_its_own_number(self):
        transcription = booklet(p3="1", p6="4")
        lines = transcription.prompt_text().splitlines()
        assert lines[page_line(transcription, 3) - 1] == (
            '[Page 3] (printed page number "1")'
        )
        assert lines[page_line(transcription, 6) - 1] == MARKED
        assert lines[page_line(transcription, 5) - 1] == "[Page 5]"

    def test_an_image_s_marker_is_left_as_it_is(self):
        """A PDF page's image markers stand after its marker, and keep
        their physical page: only the page's own marker shows the
        printed number."""
        image = f"[Page 6, image 1: sha1:{LETTER_SHA1}]"
        opening = f"[Page 6]\n{image}\n"
        text = opening + SIX
        transcription = Transcription(
            artifact=BOOKLET_REF,
            text=text,
            engine="pdf-pages:test",
            pages=[
                TranscribedPage(
                    page=6,
                    start=len(opening),
                    end=len(text),
                    images=[LETTER_SHA1],
                    printed_page="4",
                    status="transcribed",
                    provenance=PageProvenance(method="vision", engine="t"),
                )
            ],
        )
        assert transcription.prompt_text().splitlines()[:2] == [MARKED, image]

    def test_a_marker_quoted_in_a_page_s_text_is_not_the_page_s(self):
        """Page 5's text holds a line that reads as page 6's marker: the
        marker is looked for only where page 6's opening lies."""
        record = booklet_record(p6="4").model_copy(deep=True)
        pages = list(record.pages)
        pages[4] = pages[4].model_copy(
            update={"text": "[Page 6]", "status": "transcribed"}
        )
        record = TranscriptionRecord(document_class=UNCLASSIFIED, pages=pages)
        transcription = Transcription.from_record(
            record, artifact=BOOKLET_REF, language="en", engine="t"
        )
        lines = transcription.prompt_text().splitlines()
        assert lines.count("[Page 6]") == 1
        assert lines.count(MARKED) == 1
        assert lines.index("[Page 6]") < lines.index(MARKED)
        own = transcription.pages[5].start - 1  # the newline after it
        assert transcription.text[own - len("[Page 6]") : own] == "[Page 6]"

    def test_only_a_marker_on_a_line_of_its_own_is_the_page_s(self):
        """Before page 2's text, a line that holds its marker and more is
        not its marker line; the line that is only the marker is."""
        text = "[Page 1]\nTitle\n\nx [Page 2]\n[Page 2] y\n[Page 2]\nBody"
        pages = [
            SimpleNamespace(page=1, start=9, end=14, printed_page=None),
            SimpleNamespace(
                page=2, start=len(text) - 4, end=len(text), printed_page="4"
            ),
        ]
        assert prompt_text(text, pages).splitlines()[3:6] == [
            "x [Page 2]",
            "[Page 2] y",
            '[Page 2] (printed page number "4")',
        ]

    def test_a_page_whose_marker_is_not_before_its_text_shows_none(self):
        """The marker is looked for only before the page's own text: a
        page without one there shows no printed number, even where its
        text quotes its marker."""
        text = "[Page 1]\nTitle\n\nquoted:\n[Page 2]\n"
        pages = [
            SimpleNamespace(page=1, start=9, end=14, printed_page=None),
            SimpleNamespace(page=2, start=16, end=len(text), printed_page="4"),
        ]
        assert prompt_text(text, pages) == text

    @pytest.mark.parametrize(
        "printed, shown",
        [
            # an image of two pages, read as one PDF page: as given
            ("12, 13", '"12, 13"'),
            ("iv", '"iv"'),
            # a number that would break its line is escaped on it
            ("4\n5", '"4\\n5"'),
            ("4 5", '"4\\u20285"'),
            ('4 "a"', '"4 \\"a\\""'),
            ("4\x855", '"4\\u00855"'),
            ("4\u20295", '"4\\u20295"'),
            # a number in another script stands as printed, unescaped
            ("с. 4", '"с. 4"'),
        ],
    )
    def test_a_printed_number_is_shown_as_given_on_one_line(
        self, printed, shown
    ):
        transcription = booklet(p6=printed)
        lines = transcription.prompt_text().splitlines()
        assert len(lines) == len(transcription.text.splitlines())
        assert lines[page_line(transcription, 6) - 1] == (
            f"[Page 6] (printed page number {shown})"
        )


class TestEveryExtractionPromptShowsIt:
    """The entity manifest, the segmentation, every chunk (its document,
    its units, its mentions) and the single call: each shows page 6's
    marker with its printed number, and asks for the physical page."""

    ASKS = (
        "The page number is the N of the [Page N] (or [Slide N]) marker "
        "the text stands under, never a printed page number shown beside "
        "that marker."
    )

    def prompts(self, tier: str, conversational: bool) -> dict:
        transcription = booklet()
        six = page_line(transcription, 6)
        last = len(transcription.text.splitlines())
        if tier == "entity":
            answers = [manifest_json([six, six + 3]), ndjson([sibling()])]
        else:
            answers = [
                "no manifest here",
                "no manifest here either",
                segmentation_json(six, last),
                ndjson([sibling()]),
            ]
        llm = (ScriptedLlm if conversational else PlainLlm)(answers)
        ChunkedClaimExtractor(llm, budget=10_000).extract(
            transcription, BOOKLET_REF
        )
        steps: dict[str, list[str]] = {}
        for _method, prompt in llm.calls:
            if "manifest of EVERY distinct entity" in prompt:
                step = "manifest"
            elif prompt.startswith("You segment a document"):
                step = "segmentation"
            else:
                step = "chunk"
            steps.setdefault(step, []).append(prompt)
        return steps

    @pytest.mark.parametrize(
        "tier, conversational",
        [("entity", True), ("entity", False), ("line", True)],
        ids=["entity-conversation", "entity-inline", "line-range"],
    )
    def test_each_prompt_shows_page_6_printed_as_4(self, tier, conversational):
        transcription = booklet()
        numbered = f"{page_line(transcription, 6)}: {MARKED}"
        prompts = self.prompts(tier, conversational)
        for step in ("manifest", "segmentation"):
            for prompt in prompts.get(step, []):
                assert numbered in prompt.splitlines()
        (chunk,) = prompts["chunk"]
        assert self.ASKS in " ".join(chunk.split())
        if tier == "entity":
            # a mention on the marker's line shows it, by its number
            assert f"    {numbered}" in chunk.splitlines()
            # an inline document shows it; a fork sees the manifest's
            assert (numbered in chunk.splitlines()) is not conversational
        else:
            # the unit's text, from page 6's marker on
            assert f"  text:\n{MARKED}\n{CAPTION}" in chunk

    def test_the_single_call_prompt_shows_it(self):
        llm = ScriptedLlm([ndjson([sibling()])])
        LlmClaimExtractor(llm).extract(booklet(), BOOKLET_REF)
        ((_method, prompt),) = llm.calls
        assert f"\n{MARKED}\n{CAPTION}\n" in prompt
        assert self.ASKS in " ".join(prompt.split())

    def test_the_claim_schema_holds_no_printed_page(self):
        """The model is never asked for a printed page: the extractor sets
        it from the record."""
        assert "printed_page_number" not in claim_schema_prompt_text()


def extracted(tier: str, claims: list[dict], transcription=None) -> list:
    """The sources of the claims an extractor of `tier` gives for the
    booklet when the model answers `claims`."""
    transcription = transcription or booklet()
    if tier == "chunked":
        last = len(transcription.text.splitlines())  # the commentary
        llm = ScriptedLlm([manifest_json([last]), ndjson(claims)])
        batch = ChunkedClaimExtractor(llm).extract(transcription, BOOKLET_REF)
    else:
        llm = ScriptedLlm([ndjson(claims)])
        batch = LlmClaimExtractor(llm).extract(transcription, BOOKLET_REF)
    return [claim.source for claim in batch.claims]


TIERS = ["chunked", "single-call"]


class TestTheLocatorIsLookedUp:
    """A claim's printed page is the record's for the one physical page it
    cites; anything else has none, whatever the model wrote."""

    @pytest.mark.parametrize("tier", TIERS)
    def test_page_6_is_printed_as_4(self, tier):
        (source,) = extracted(tier, [sibling("6")])
        assert (source.page_number, source.printed_page_number) == ("6", "4")

    def test_the_single_call_asked_again_looks_it_up_too(self):
        """A kept answer that is rejected is asked for once more; the
        answer to that call has its printed page set as the first's
        would have."""

        class KeepingLlm(ScriptedLlm):
            def reject(self, response: str, reason: str) -> bool:
                return True  # the rejected answer was a kept one

        llm = KeepingLlm(["no claims here", ndjson([sibling("6")])])
        batch = LlmClaimExtractor(llm).extract(booklet(), BOOKLET_REF)
        (claim,) = batch.claims
        assert claim.source.printed_page_number == "4"
        assert len(llm.calls) == 2

    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize(
        "page",
        [
            "5",  # a page the record gives no printed number
            "6-7",  # a range
            "6, 7",
            "p. 6",  # a page given in words
            "4",  # the printed number given as the page: page 4 has none
            "",
            None,  # no page
        ],
    )
    def test_any_other_page_has_none(self, tier, page):
        (source,) = extracted(tier, [sibling(page)])
        assert source.printed_page_number is None
        assert source.page_number == page

    @pytest.mark.parametrize("tier", TIERS)
    def test_a_page_given_with_spaces_around_it_is_that_page(self, tier):
        (source,) = extracted(tier, [sibling(" 6 ")])
        assert source.printed_page_number == "4"

    @pytest.mark.parametrize("tier", TIERS)
    def test_what_the_model_writes_is_replaced(self, tier):
        sources = extracted(
            tier,
            [
                sibling("6", printed_page_number="9"),
                sibling("5", printed_page_number="3"),
            ],
        )
        assert [(s.page_number, s.printed_page_number) for s in sources] == [
            ("6", "4"),
            ("5", None),
        ]

    @pytest.mark.parametrize("tier", TIERS)
    def test_a_transcription_without_pages_gives_none(self, tier):
        """A text file's transcription has no record: nothing is printed,
        whatever page a claim cites or the model writes."""
        plain = Transcription(artifact=BOOKLET_REF, text=SIX, engine="t")
        (source,) = extracted(
            tier, [sibling("6", printed_page_number="4")], plain
        )
        assert source.printed_page_number is None

    def test_claims_the_same_once_their_source_is_set_are_one(self):
        """A model-written printed page sets two claims apart only until
        the extractor sets it: then they are the same claim, kept once."""
        cited = cite_artifact(
            [sibling("6", printed_page_number="9"), sibling("6")],
            BOOKLET_REF,
            {6: "4"},
        )
        assert len(cited) == 1
        assert cited[0]["source"]["printed_page_number"] == "4"

    @pytest.mark.parametrize(
        "page, printed",
        [
            ("6", "4"),
            ("06", "4"),
            ("0" * 20 + "6", "4"),
            ("9" * 5000, None),  # past int()'s digit limit, and no error
            ("\t6\n", "4"),
            ("6a", None),
            ("٦", None),  # a digit, though not an ASCII one
            ("-6", None),
            (6, None),  # not text: validation refuses it
            (None, None),
        ],
    )
    def test_only_one_physical_page_is_looked_up(self, page, printed):
        assert printed_page_number(page, {6: "4"}) == printed

    def test_a_page_past_999_is_looked_up(self):
        assert printed_page_number("1000", {1000: "998"}) == "998"

    def test_the_printed_number_is_kept_as_given(self):
        """The record's number exactly as given, which the claim's id
        hashes; the writer cleans it to one line as it writes it."""
        assert printed_page_number("6", {6: " 4 "}) == " 4 "

    def test_a_printed_number_of_whitespace_alone_is_none(self):
        """The writer writes nothing for it, so the claim carries none."""
        assert printed_page_number("6", {6: "  "}) is None

    @pytest.mark.parametrize("tier", TIERS)
    def test_a_runaway_page_number_is_left_for_validation(self, tier):
        """A page number too long to be a page costs no printed number and
        never the document's extraction."""
        (source,) = extracted(tier, [sibling("9" * 5000)])
        assert source.printed_page_number is None
        assert source.page_number == "9" * 5000


class TestTheSourceIsValidated:
    @pytest.mark.parametrize("page", [None, "", "  "])
    def test_a_printed_page_without_its_page_is_refused(self, page):
        """A prepared claims file cannot cite a printed page alone: it
        names the page `page_number` names."""
        located = {} if page is None else {"page_number": page}
        with pytest.raises(ValidationError, match="page number it stands"):
            SourceReference.model_validate(
                {
                    "artifact": BOOKLET_REF.model_dump(mode="json"),
                    "printed_page_number": "4",
                    **located,
                }
            )

    def test_a_prepared_batch_with_one_alone_is_refused(self):
        with pytest.raises(ValidationError, match="page number it stands"):
            booklet_batch({"printed_page_number": "4"})

    def test_a_printed_page_beside_its_page_is_kept(self):
        (claim,) = booklet_batch(
            {"page_number": "6", "printed_page_number": "4"}
        ).claims
        assert claim.source.printed_page_number == "4"


class TestTheRecordRefusesALoneSurrogate:
    def test_a_printed_page_holding_one_fails_the_record(self):
        """As page text holding one fails the transcription; the reason
        never quotes the value."""
        answer = json.dumps(
            {
                "document_class": UNCLASSIFIED,
                "pages": [record_page(1, SIX, printed_page="4\ud800")],
            }
        )
        with pytest.raises(RecordError, match="lone surrogate") as raised:
            parse_record(answer)
        assert "\ud800" not in str(raised.value)

    def test_a_printed_page_without_one_passes(self):
        answer = json.dumps(
            {
                "document_class": UNCLASSIFIED,
                "pages": [record_page(1, SIX, printed_page="4")],
            }
        )
        assert parse_record(answer).pages[0].printed_page == "4"


class TestTheSourceSerialisesAsBefore:
    def test_a_source_without_a_printed_page_has_no_key(self):
        """A batch with no printed page dumps, and so hashes, as it would
        if the field did not exist."""
        source = SourceReference(artifact=BOOKLET_REF, page_number="6")
        assert "printed_page_number" not in source.model_dump(mode="json")
        assert "printed_page_number" not in source.model_dump_json()
        printed = source.model_copy(update={"printed_page_number": "4"})
        assert printed.model_dump(mode="json")["printed_page_number"] == "4"
        assert '"printed_page_number":"4"' in printed.model_dump_json()

    def test_a_batch_dump_has_no_key_for_a_source_without_one(self):
        """The key is left out of a source nested in a claim batch too,
        in both dump paths."""
        batch = ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": [sibling("6")]}
        )
        dumped = batch.model_dump(mode="json")["claims"][0]["source"]
        assert "printed_page_number" not in dumped
        assert "printed_page_number" not in batch.model_dump_json()

    def test_a_claim_without_a_printed_page_keeps_its_id(self):
        batch = ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": [sibling("6")]}
        )
        (claim,) = batch.claims
        again = ClaimBatch.model_validate_json(batch.model_dump_json())
        assert claim_id(again.claims[0]) == claim_id(claim)
        printed = claim.model_copy(
            update={
                "source": claim.source.model_copy(
                    update={"printed_page_number": "4"}
                )
            }
        )
        assert claim_id(printed) != claim_id(claim)

    def test_validation_refuses_it_as_a_claim_s_property(self):
        """It is a reference property: no claim or qualifier states it."""
        claim = dict(
            sibling("6"),
            property="printed_page_number",
            value={"type": "string", "value": "4"},
        )
        batch = ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": [claim]}
        )
        (problem,) = validate_against_manifest(batch)
        assert "'printed_page_number' is a reference property" in problem


def block(statement) -> dict[str, list]:
    """The statement's source block, the one with `stated in`, by
    manifest key -> its values (an excerpt by its text)."""
    keys = {pid: key for key, pid in PROPERTIES.items()}
    (source,) = [
        reference
        for reference in statement.references.references
        if reference.snaks.get(PROPERTIES["stated_in"])
    ]
    found: dict[str, list] = {}
    for pid, snaks in source.snaks.snaks.items():
        for snak in snaks:
            value = snak.datavalue["value"]
            if isinstance(value, dict):
                value = value.get("id") or value.get("text")
            found.setdefault(keys[pid], []).append(value)
    return found


def booklet_batch(*sources: dict) -> ClaimBatch:
    """The booklet's batch: its commentary's sibling claim once with each
    of `sources`' locators."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                dict(
                    sibling(None),
                    source={
                        "artifact": BOOKLET_REF.model_dump(mode="json"),
                        **source,
                    },
                )
                for source in sources
            ],
        }
    )


class TestTheWriter:
    """The printed page number is a snak of the claim's source block,
    beside the physical page number (ADR-0003, ADR-0037)."""

    def test_the_block_holds_the_page_and_its_printed_number(self):
        wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
        result = write_mapped(
            BatchWriter(wiki),
            booklet_batch({"page_number": "6", "printed_page_number": "4"}),
        )
        gustl = result.created_items["e1"]
        (statement,) = wiki.wbi.item.get(gustl).claims.get(
            PROPERTIES["sibling"]
        )
        assert block(statement) == {
            "stated_in": ["Q23"],
            "page_number": ["6"],
            "printed_page_number": ["4"],
        }

    def test_a_block_without_one_has_no_snak(self):
        wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
        result = write_mapped(
            BatchWriter(wiki), booklet_batch({"page_number": "5"})
        )
        (statement,) = wiki.wbi.item.get(
            result.created_items["e1"]
        ).claims.get(PROPERTIES["sibling"])
        assert block(statement) == {
            "stated_in": ["Q23"],
            "page_number": ["5"],
        }

    def test_the_printed_number_goes_on_one_line(self):
        """It is a locator like the page number: cleaned up to one line
        (writer._locator_text), so a re-run finds its block."""
        wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
        result = write_mapped(
            BatchWriter(wiki),
            booklet_batch(
                {"page_number": "6", "printed_page_number": " 4\n5 "}
            ),
        )
        (statement,) = wiki.wbi.item.get(
            result.created_items["e1"]
        ).claims.get(PROPERTIES["sibling"])
        assert block(statement)["printed_page_number"] == ["4 | 5"]

    def test_a_rewrite_adds_nothing(self):
        wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
        batch = booklet_batch({"page_number": "6", "printed_page_number": "4"})
        first = write_mapped(BatchWriter(wiki), batch)
        (statement,) = wiki.wbi.item.get(first.created_items["e1"]).claims.get(
            PROPERTIES["sibling"]
        )
        blocks = len(statement.references.references)
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        (statement,) = wiki.wbi.item.get(first.created_items["e1"]).claims.get(
            PROPERTIES["sibling"]
        )
        assert len(statement.references.references) == blocks


# the booklet as a PDF: its title page and the note on its translations
# typed, page 5 rendered native text with printed number 3, and page 6
# typed, with the image of the letter's top above its own text
def booklet_pdf() -> bytes:
    six = pdfs.Page().image(LETTER_IMAGE, 72, 520, 300, 200)
    six.text(pdfs.lines(CAPTION, TRANSLATION, COMMENT), y=500)
    return pdfs.pdf(
        [
            typed(TITLE),
            typed(SOFTWARE),
            pdfs.Page(),
            pdfs.Page(),
            vision_page(),
        ]
        + [six]
    )


FOLIO = json.dumps(
    {
        "document_class": UNCLASSIFIED,
        "pages": [record_page(1, EXTRACT, printed_page="3")],
    }
)


def on_page(page: str):
    """Cite the chosen container page or the image's own first page."""

    def answer(kind, prompt, opening):
        given = pipeline_answers(kind, prompt, opening)
        if prompt.startswith(TRANSCRIPTION_PROMPT):
            return given
        location = reading_location(opening or prompt, page)
        if location is None:
            location = reading_location(opening or prompt)
        lines = []
        for line in given.splitlines():
            found = json.loads(line) if line.startswith("{") else None
            if isinstance(found, dict) and "source" in found:
                assert location is not None
                physical_page, quote = location
                found["source"] |= {
                    "page_number": physical_page,
                    "printed_page_number": "99",
                    "transcription_excerpt": {"text": quote, "language": "en"},
                }
                line = json.dumps(found)
            lines.append(line)
        return "\n".join(lines)

    return answer


class TestAnExcerpt:
    """An excerpt's transcription is its image's own reading, so its
    claims never take the page number printed on the container's page;
    its `part of` the container keeps the container's physical page."""

    def made(self, tmp_path):
        corpus = folder(tmp_path, booklet=booklet_pdf())
        reader = Reader({LETTER_SHA1: EXCERPT, "page-5": FOLIO})
        reader.answer = on_page("5")
        return run(tmp_path, corpus, reader), reader

    def test_its_claims_never_take_the_container_s_printed_page(
        self, tmp_path
    ):
        orchestrator, reader = self.made(tmp_path)
        doc = document(orchestrator, "booklet.pdf")
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        # the booklet's page 5 is printed as 3; the image shows no number
        assert transcription_of(doc).printed_pages() == {5: "3"}
        assert transcription_of(excerpt).printed_pages() == {}
        # Each reading uses its own physical page and printed-page lookup.
        assert {
            (c.source.page_number, c.source.printed_page_number)
            for c in batch_of(doc).claims
        } == {("5", "3")}
        assert {
            (c.source.page_number, c.source.printed_page_number)
            for c in batch_of(excerpt).claims
        } == {("1", None)}
        # only the booklet's extraction prompts show a printed number,
        # beside page 5 (a critique prompt never holds its document's own
        # SHA-1, though it carries the numbered transcription, image
        # markers and all, as the extraction prompts do, #607)
        shown = [
            prompt
            for kind, prompt in reader.calls
            if kind != "vision"
            and 'printed page number "' in prompt
            and not prompt.startswith("You review a genealogical extraction")
        ]
        assert shown
        assert all(doc.sha1 in prompt for prompt in shown)
        assert all(
            '[Page 5] (printed page number "3")' in prompt for prompt in shown
        )
        # the image's marker keeps its physical page
        assert all(
            f"[Page 6, image 1: sha1:{LETTER_SHA1}]" in prompt
            for prompt in shown
            if "[Page 6]" in prompt
        )

    def test_its_part_of_keeps_the_physical_page(self, tmp_path):
        orchestrator, _reader = self.made(tmp_path)
        wiki = RegistrarWiki()
        orchestrator._client = wiki
        doc = document(orchestrator, "booklet.pdf")
        pdf = Orchestrator._step_register(orchestrator, doc)
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        image = Orchestrator._step_register(orchestrator, excerpt)
        assert part_of(wiki.entities[image]) == {(pdf, "6"): [pdf]}
