"""The transcription record (#389, ADR-0037): the vision engine answers a
record per page, and code reads the record instead of the wording. The
check fails an answer that is not the record, a page without a status, an
answer cut off at the output limit and a record missing pages; the
language hint comes from the record; the answer is the text after the
last tool result; and the record's page numbers are read through
`Transcription.printed_pages`.

Every text is a source of the fictional universe (docs/EXAMPLES.md), in
its English, German, Russian and Frisian, or invented engine text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.records import page_text, record_answer, record_page
from yizkr.claims import ArtifactRef
from yizkr.model.schema import load_manifest
from yizkr.pipeline.language import (
    detect_language,
    record_language,
    resolve_language,
)
from yizkr.pipeline.llm import ClaudeCodeClient, OutputLimitReached
from yizkr.pipeline.transcribe import (
    TRANSCRIPTION_PROMPT,
    LlmVisionTranscriber,
    Transcription,
)
from yizkr.pipeline.transcription_check import (
    CHECK_VERSION,
    CUT_OFF,
    NOT_A_RECORD,
    PAGES_EXTRA,
    PAGES_MISSING,
    TOO_SHORT,
    TranscriptionCheckFailed,
    check_answer,
    check_transcription,
    is_pdf,
)
from yizkr.pipeline.transcription_record import (
    EDITORIAL_MARKS,
    UNCLASSIFIED,
    RecordError,
    RecordPage,
    artifact_classes,
    document_classes,
    main_language,
    merged,
    parse_record,
    record_text,
)

SOURCES = u.by_key()
POSTCARD = SOURCES["postcard-1938"].text
FRISIAN = SOURCES["letter-to-sipkje-1952"].text
RUSSIAN = SOURCES["letter-to-leopold-1958"].text
NATURALISATION = SOURCES["naturalisation-1952"].text
CAPTION = "Wastl to Klara, July 1939."
# the postcard's greeting as a faded reading gives it
FADED = "Liebe Klara, [illegible] [illegible] [?] Dein Vater"


def a_record(*pages: dict, document_class: str = "letter") -> str:
    return json.dumps(
        {"document_class": document_class, "pages": list(pages)},
        ensure_ascii=False,
    )


class TestTheRecord:
    def test_a_record_parses_with_every_field(self):
        record = parse_record(
            a_record(
                record_page(
                    1,
                    POSTCARD,
                    languages=["de"],
                    printed_page="2",
                    notes="the stamp is torn off",
                ),
                record_page(2, "", status="blank"),
            )
        )
        assert record.document_class == "letter"
        first, second = record.pages
        # the physical index and the number printed on the page are apart
        assert (first.page, first.printed_page) == (1, "2")
        assert (first.status, first.languages) == ("transcribed", ["de"])
        assert first.text == POSTCARD
        assert first.notes == "the stamp is torn off"
        assert (second.status, second.text, second.languages) == (
            "blank",
            "",
            [],
        )

    @pytest.mark.parametrize("fence", ["", "json", "text"])
    def test_a_record_alone_in_a_fenced_block_parses(self, fence):
        answer = f"```{fence}\n" + record_answer(CAPTION) + "\n```"
        assert parse_record(answer).pages[0].text == CAPTION

    @pytest.mark.parametrize(
        "answer",
        [
            # prose, in any language: a refusal is not the record
            "I'm sorry, but I can't transcribe this document.",
            "Es tut mir leid, ich kann dieses Dokument nicht abschreiben.",
            "Извините, я не могу расшифровать этот документ.",
            # the engine's narration before the record
            "Let me read the image.\n" + record_answer(CAPTION),
            # not one object
            "[" + record_answer(CAPTION) + "]",
            # a record cut short
            record_answer(CAPTION)[:-3],
            # a field the record does not have, beside its pages
            json.dumps(
                {
                    "document_class": "letter",
                    "pages": [record_page(1, CAPTION)],
                    "confidence": 0.9,
                }
            ),
            # nesting too deep to read
            "[" * 100000 + "]" * 100000,
            # prose after the fenced record: only a block alone is read
            "```json\n" + record_answer(CAPTION) + "\n```\nHope this helps!",
            "```text\n" + record_answer(CAPTION) + "\n```\nHope this helps!",
            # a text fence still needs one complete, schema-valid record
            "```text\n" + CAPTION + "\n```",
            "```text\n" + record_answer(CAPTION)[:-3] + "\n```",
            "```text\n{}\n```",
        ],
    )
    def test_an_answer_that_is_not_one_record_object_is_refused(self, answer):
        with pytest.raises(RecordError):
            parse_record(answer)

    def test_harmless_near_misses_are_normalised(self):
        """A page number printed as a JSON number, null text, notes or
        languages, and a language code in capitals, say what the record
        says: they are normalised, not refused (ADR-0037 §1)."""
        record = parse_record(
            a_record(
                {
                    "page": 1,
                    "printed_page": 12,
                    "status": "blank",
                    "text": None,
                    "languages": None,
                    "notes": None,
                },
                {
                    "page": 2,
                    "printed_page": "iv",
                    "status": "transcribed",
                    "text": POSTCARD,
                    "languages": ["DE", "de-DE", "En"],
                    "notes": "",
                },
            )
        )
        blank, written = record.pages
        assert (blank.printed_page, blank.text, blank.notes) == ("12", "", "")
        assert blank.languages == []
        assert written.printed_page == "iv"
        # the record's language code, as a claim's, may carry a region
        # subtag, so a region-tagged code keeps it
        assert written.languages == ["de", "de-de", "en"]

    @pytest.mark.parametrize(
        "page",
        [
            {"page": "1", "status": "transcribed", "text": CAPTION},
            {"page": True, "status": "transcribed", "text": CAPTION},
            {"page": 1.5, "status": "transcribed", "text": CAPTION},
            {"page": 1, "printed_page": True, "status": "blank"},
            {"page": 1, "status": None, "text": CAPTION},
            {"page": 1, "status": "Transcribed", "text": CAPTION},
        ],
    )
    def test_bad_page_numbers_and_statuses_still_fail(self, page):
        with pytest.raises(RecordError):
            parse_record(a_record(page))

    def test_a_printed_page_number_is_written_as_printed(self):
        """A whole number given as a JSON float is the number printed
        ("12", never "12.0"); a fraction keeps its digits."""
        pages = parse_record(
            a_record(
                {"page": 1, "printed_page": 12.0, "status": "blank"},
                {"page": 2, "printed_page": 12.5, "status": "blank"},
            )
        ).pages
        assert [page.printed_page for page in pages] == ["12", "12.5"]

    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
    def test_a_printed_page_number_that_is_not_finite_fails(self, token):
        """Python's JSON reader takes these tokens as numbers; no page
        prints one."""
        answer = (
            '{"document_class": "letter", "pages": [{"page": 1, '
            f'"printed_page": {token}, "status": "blank"}}]}}'
        )
        with pytest.raises(RecordError, match="printed_page"):
            parse_record(answer)

    @pytest.mark.parametrize("languages", [[" de"], "de"])
    def test_languages_other_than_a_list_of_codes_fail(self, languages):
        """A code with a space, or a bare string in place of the list, is
        no harmless near-miss."""
        page = {"page": 1, "status": "blank", "languages": languages}
        with pytest.raises(RecordError, match="languages"):
            parse_record(a_record(page))

    @pytest.mark.parametrize(
        "page, why",
        [
            # a page with no status: the unambiguous signal the check reads
            (
                {"page": 1, "text": CAPTION, "languages": ["en"]},
                "pages.0.status",
            ),
            (
                {"page": 1, "status": "partly read", "text": CAPTION},
                "pages.0.status",
            ),
            # a page it calls transcribed must hold text
            ({"page": 1, "status": "transcribed", "text": " "}, "no text"),
            (
                {"page": 1, "status": "blank", "languages": ["english"]},
                "languages",
            ),
            (
                {"page": 1, "status": "blank", "confidence": 0.9},
                "confidence",
            ),
        ],
    )
    def test_a_page_outside_the_contract_is_refused(self, page, why):
        with pytest.raises(RecordError, match="not the transcription record"):
            parse_record(a_record(page))
        with pytest.raises(RecordError) as caught:
            parse_record(a_record(page))
        assert why in str(caught.value)
        # the reason never quotes the answer
        assert CAPTION not in str(caught.value)

    def test_the_pages_are_numbered_from_one_in_order(self):
        for numbers in ([2], [1, 3], [2, 1], [1, 1]):
            pages = [record_page(n, CAPTION) for n in numbers]
            with pytest.raises(RecordError, match="numbered"):
                parse_record(a_record(*pages))

    def test_the_class_is_one_of_the_manifests_or_unclassified(self):
        manifest = load_manifest()
        assert artifact_classes() == tuple(
            key
            for key, item in manifest.items.items()
            if item.subclass_of == "artifact"
        )
        assert {"photograph", "certificate", "letter"} <= set(
            artifact_classes()
        )
        assert document_classes() == (*artifact_classes(), UNCLASSIFIED)
        for document_class in document_classes():
            answer = record_answer(CAPTION, document_class=document_class)
            assert parse_record(answer).document_class == document_class
        for other in ("artifact", "diary", ""):
            with pytest.raises(RecordError, match="document class"):
                parse_record(record_answer(CAPTION, document_class=other))
        with pytest.raises(RecordError):
            parse_record(json.dumps({"pages": [record_page(1, CAPTION)]}))

    def test_the_text_opens_each_page_with_its_marker(self):
        record = parse_record(
            a_record(
                record_page(1, CAPTION),
                record_page(2, "", status="blank"),
                record_page(3, "[illegible]", status="illegible"),
            )
        )
        text, spans = record_text(record.pages)
        assert text == (
            f"[Page 1]\n{CAPTION}\n\n[Page 2]\n\n[Page 3]\n[illegible]"
        )
        assert [text[start:end] for start, end in spans] == [
            CAPTION,
            "",
            "[illegible]",
        ]

    def test_an_image_of_several_pages_merges_into_one_pdf_page(self):
        pages = [
            RecordPage(
                page=1,
                printed_page="12",
                status="blank",
                languages=[],
                notes="a pasted-in photograph",
            ),
            RecordPage(
                page=2,
                printed_page="13",
                status="transcribed",
                text=CAPTION,
                languages=["en", "de"],
                notes="pencil",
            ),
        ]
        one = merged(pages, 6)
        assert (one.page, one.printed_page, one.status) == (
            6,
            "12, 13",
            "transcribed",
        )
        assert one.text == CAPTION
        assert one.languages == ["en", "de"]
        assert one.notes == "a pasted-in photograph; pencil"
        blank = merged([pages[0]], 2)
        assert (blank.status, blank.printed_page) == ("blank", "12")


class TestTheViewsRecord:
    def test_a_view_whose_fields_the_record_no_longer_accepts_has_none(
        self,
    ):
        """A stored view with a class the manifest has since dropped holds
        no record, and the check fails it as holding none, so it is made
        again rather than crashing its reuse."""
        view = Transcription.from_record(
            parse_record(record_answer(CAPTION)),
            artifact=ArtifactRef(sha1="a" * 40),
            language="en",
            engine="llm-vision:fake-model",
        )
        assert view.record() is not None
        stale = view.model_copy(update={"document_class": "diary"})
        assert stale.record() is None
        check = check_transcription(
            stale.text, pages=1, record=stale.record(), expect_record=True
        )
        assert check.failure == NOT_A_RECORD


class TestTheCheckReadsTheRecord:
    def test_a_record_passes_and_is_judged_on_its_text(self):
        check, record = check_answer(
            record_answer(FRISIAN, document_class="letter", languages=["fy"]),
            pages=1,
        )
        assert check.passed and not check.flagged
        assert record.pages[0].languages == ["fy"]
        assert check.record_pages == 1

    @pytest.mark.parametrize(
        "answer",
        [
            # a refusal the English signatures never match
            "Извините, я не могу расшифровать этот документ.",
            "It spyt my, mar ik kin dit dokumint net oerskriuwe.",
            # a page without a status
            a_record({"page": 1, "text": RUSSIAN, "languages": ["ru"]}),
        ],
    )
    def test_an_answer_that_is_not_the_record_fails(self, answer):
        check, record = check_answer(answer, pages=1)
        assert record is None
        assert (check.passed, check.failure) == (False, NOT_A_RECORD)
        assert check.signature is None

    def test_a_record_missing_pages_fails(self):
        answer = record_answer(NATURALISATION, CAPTION)
        check, _record = check_answer(answer, pages=3)
        assert (check.passed, check.failure) == (False, PAGES_MISSING)
        assert "2 of the media's 3 pages" in check.reason
        assert check_answer(answer, pages=2)[0].passed

    def test_an_image_may_show_two_pages(self):
        """One photograph of an open book is two pages of the document."""
        answer = record_answer(NATURALISATION, CAPTION)
        assert check_answer(answer, pages=1)[0].passed

    def test_pages_the_record_calls_blank_need_no_text(self):
        """The floor counts the pages the record calls transcribed: blank
        and illegible pages are the engine's plain statement."""
        answer = a_record(
            record_page(1, CAPTION),
            record_page(2, "", status="blank"),
            record_page(3, "[illegible]", status="illegible"),
            record_page(4, "", status="not-a-document-page"),
        )
        check, _record = check_answer(answer, pages=4)
        assert check.passed
        assert check.min_text_chars == 3
        # a page it calls transcribed that holds only marks is too short
        marks = a_record(
            record_page(1, "[illegible]"), record_page(2, "", status="blank")
        )
        check, _record = check_answer(marks, pages=2)
        assert (check.passed, check.failure) == (False, TOO_SHORT)
        assert "1 transcribed pages" in check.reason

    def test_an_answer_cut_off_at_the_output_limit_fails(self):
        check, record = check_answer(record_answer(CAPTION), cut_off=True)
        assert (check.passed, check.failure, record) == (False, CUT_OFF, None)

    def test_a_vision_transcription_without_a_record_fails(self):
        check = check_transcription(CAPTION, pages=1, expect_record=True)
        assert (check.passed, check.failure) == (False, NOT_A_RECORD)
        assert check_transcription(CAPTION, pages=1).passed

    def test_the_wording_of_a_record_only_flags_it(self):
        answer = record_answer("I'm unable to assist with this request.")
        check, record = check_answer(answer, pages=1)
        assert (check.passed, check.flagged) == (True, True)
        assert check.signature == "refusal-request"
        assert record is not None

    def test_a_note_in_the_words_of_a_failing_signature_only_flags(self):
        """The notes are read with every signature, the fail tier's
        included, and only flag the record."""
        answer = a_record(
            record_page(
                1,
                "Liebe Klara",
                languages=["de"],
                notes="Let me take a look at the image.",
            )
        )
        check, _record = check_answer(answer, pages=1)
        assert (check.passed, check.flagged) == (True, True)
        assert check.signature == "narration-only"
        assert "page 1's notes" in check.reason

    def test_the_first_page_s_notes_give_the_flag(self):
        answer = a_record(
            record_page(1, "", status="blank", notes="No image was provided."),
            record_page(
                2,
                "Liebe Klara",
                languages=["de"],
                notes="Let me take a look at the image.",
            ),
        )
        check, _record = check_answer(answer, pages=2)
        assert check.signature == "media-missing"
        assert "page 1's notes" in check.reason

    def test_a_pdf_is_known_by_its_suffix_in_any_case(self):
        assert is_pdf(Path("FAMILY-HISTORY.PDF"))
        assert is_pdf(Path("family-history.pdf"))
        assert not is_pdf(Path("letter-excerpt-1939.png"))

    @pytest.mark.parametrize(
        "line, signature",
        [
            # a postcard's one line, in the words of an announcement
            ("I will bring the documents to Stupfield.", "narration-only"),
            ("Let me take a look at the image.", "narration-only"),
            (
                "cat: page-1.png: No such file or directory",
                "shell-error",
            ),
            ("pdftoppm: command not found", "pdf-renderer"),
        ],
    )
    def test_no_wording_fails_a_record(self, line, signature):
        """With a record parsed, a failing signature only flags its
        text (ADR-0037 §3); a text with no record still fails on it."""
        check, record = check_answer(record_answer(line), pages=1)
        assert record is not None
        assert (check.passed, check.flagged) == (True, True)
        assert check.signature == signature
        bare = check_transcription(line, pages=1)
        assert (bare.passed, bare.signature) == (False, signature)

    def test_the_notes_are_read_for_a_flag(self):
        """A note, the engine's own words about its page, is read as the
        text is, each on its own: one saying it saw no image flags the
        record, and never fails it."""
        unseen = a_record(
            record_page(1, CAPTION),
            record_page(2, "", status="blank", notes="No image was provided."),
        )
        check, _record = check_answer(unseen, pages=2)
        assert (check.passed, check.flagged) == (True, True)
        assert check.signature == "media-missing"
        assert "page 2's notes" in check.reason
        plain = a_record(
            record_page(1, CAPTION, notes="Written in pencil."),
            record_page(2, "", status="blank", notes="The back of the card."),
        )
        check, _record = check_answer(plain, pages=2)
        assert check.passed and not check.flagged
        # the signatures are English only: a German note is not matched
        german = a_record(
            record_page(
                1, "", status="blank", notes="Es wurde kein Bild übergeben."
            )
        )
        check, _record = check_answer(german, pages=1)
        assert check.passed and not check.flagged

    def test_a_whole_pdf_s_record_gives_exactly_its_pages(self):
        """A record of a PDF read whole with more pages than the PDF
        fails, as one with fewer does; an image's may give more."""
        answer = record_answer(NATURALISATION, CAPTION, CAPTION)
        check, _record = check_answer(answer, pages=2, whole_pdf=True)
        assert (check.passed, check.failure) == (False, PAGES_EXTRA)
        assert "3 pages for a PDF of 2" in check.reason
        assert check_answer(answer, pages=3, whole_pdf=True)[0].passed
        assert check_answer(answer, pages=2)[0].passed
        check, _record = check_answer(answer, pages=4, whole_pdf=True)
        assert check.failure == PAGES_MISSING

    def test_a_single_image_has_no_floor(self):
        check, _record = check_answer(record_answer("K."), pages=1)
        assert check.passed
        assert check.min_text_chars == 0

    def test_the_floor_counts_no_page_the_record_does_not_call_read(self):
        """A page that is not a document page holds letters, which do
        not count toward the floor of the pages called transcribed."""
        answer = a_record(
            record_page(1, "[illegible]"),
            record_page(
                2, "colour control patches", status="not-a-document-page"
            ),
        )
        check, _record = check_answer(answer, pages=2)
        assert (check.passed, check.failure) == (False, TOO_SHORT)

    def test_a_verdict_names_the_check_that_reads_the_record(self):
        """The check's version rose when it began reading the record, so
        a verdict tells the record's check from the wording's."""
        assert CHECK_VERSION == 3
        check, _record = check_answer(record_answer(CAPTION), pages=1)
        assert check.version == 3


class FakeClient:
    """An engine's client that gives `answers` in turn, and reports the
    stop reason its model gave each (`last_report`), as the engines'
    clients do."""

    model_identifier = "fake-model"

    def __init__(self, *answers, stop_reason: str = "end_turn") -> None:
        self.answers = list(answers)
        self.stop_reason = stop_reason
        self.prompts: list[str] = []
        self.rejected: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def last_report(self) -> dict:
        return {"stop_reason": self.stop_reason}

    def reject(self, response, reason) -> bool:
        self.rejected.append(reason)
        return False


def a_scan(tmp_path, name="postcard.png"):
    path = tmp_path / name
    path.write_bytes(b"fictional scan of the postcard")
    return path


class TestTheTranscriber:
    def test_the_prompt_asks_for_the_record(self):
        for mark in EDITORIAL_MARKS:
            assert mark in TRANSCRIPTION_PROMPT
        assert "[redacted]" in TRANSCRIPTION_PROMPT
        for field in (
            "document_class",
            "printed_page",
            "status",
            "languages",
            "notes",
        ):
            assert f'"{field}"' in TRANSCRIPTION_PROMPT
        for document_class in document_classes():
            assert f'"{document_class}"' in TRANSCRIPTION_PROMPT
        for status in ("transcribed", "blank", "illegible"):
            assert f'"{status}"' in TRANSCRIPTION_PROMPT
        assert '"not-a-document-page"' in TRANSCRIPTION_PROMPT
        # a PDF's record gives exactly its pages, a spread in one of them
        # (ADR-0037 §3)
        assert (
            "A PDF gives one object for each page of the PDF, never more: "
            "where a page of the PDF shows two pages of a document (a "
            "spread), its object holds the text of both, in reading order."
        ) in " ".join(TRANSCRIPTION_PROMPT.split())

    def test_the_prompt_lays_out_a_table_one_line_per_row(self):
        # Without a layout rule each call picked its own, and a row run
        # together lost its empty cells and with them which column each
        # value stood in (#570).
        flowing = " ".join(TRANSCRIPTION_PROMPT.split())
        # the general rule the table rule is the one exception to
        assert "Preserve line breaks" in flowing
        for rule in (
            "write every table, list in columns or form grid, ruled or "
            "not, one line per row, with the row's cells separated by "
            '" | ".',
            "The header follows the same rule: a header of several lines "
            "is written one line per header line, and a heading that spans "
            "several columns stands in the first of its cells.",
            "Keep each empty cell as an empty field, so that every row has "
            "as many cells as the table has columns and each value stays "
            "in its column.",
            "A cell whose text runs over several lines stays on its row's "
            "line, its lines joined by a space; this is the one exception "
            "to preserving line breaks.",
            # prose is carved out by what it is, not by its layout: a
            # newspaper's columns are not a table
            "Running prose is never laid out this way, even where it is "
            "printed in several columns (a newspaper's columns, a page set "
            "in two columns): write each column in turn, in reading order.",
            "A title, or a note above or under a table, is written as it "
            "stands.",
        ):
            assert rule in flowing
        introduction = (
            "For example, a register with the columns Name, Alter, ledig, "
            "verh. and verw., whose one entry is marked in the last column:"
        )
        assert introduction in flowing
        # the example is a line per row, each row as wide as its header,
        # with the empty cells kept as empty fields
        lines = TRANSCRIPTION_PROMPT.splitlines()
        header = lines.index("  Name | Alter | ledig | verh. | verw.")
        assert lines[header - 1].strip() == "```text"
        end = lines.index("  ```", header + 1)
        example = lines[header:end]
        assert example[1:] == ["  Löbig, Eugen | 73 |  |  | x"]
        columns = example[0].strip().split(" | ")
        for line in example:
            assert len(line.split(" | ")) == len(columns)
        # the introduction names the header's columns, and the entry's
        # mark is in the last of them
        assert (
            "the columns " + ", ".join(columns[:-1]) + " and " + columns[-1]
            in introduction
        )
        assert example[1].split(" | ")[-1] == "x"

    def test_the_prompts_table_example_is_from_the_universe(self):
        # Eugen's burial register entry: a widower of 73 (docs/EXAMPLES.md)
        (register,) = (
            source.text
            for source in u.SOURCES
            if source.key == "burial-register-1909"
        )
        assert "Löbig, Eugen" in register
        assert "Alter: 73 Jahre" in register
        assert "Witwer" in register
        assert "Löbig, Eugen | 73 |  |  | x" in TRANSCRIPTION_PROMPT

    def test_the_record_reaches_the_view(self, tmp_path):
        answer = a_record(
            record_page(
                1,
                POSTCARD,
                languages=["de"],
                printed_page="1",
                notes="postmark faint",
            ),
            record_page(2, "", status="blank"),
        )
        client = FakeClient(answer)
        transcription = LlmVisionTranscriber(
            client, language="auto"
        ).transcribe(a_scan(tmp_path))
        assert client.prompts == [TRANSCRIPTION_PROMPT]
        assert transcription.document_class == "letter"
        assert transcription.language == "de"
        assert transcription.text == f"[Page 1]\n{POSTCARD}\n\n[Page 2]"
        first, second = transcription.pages
        assert transcription.page_text(first) == POSTCARD
        assert (first.printed_page, first.status, first.languages) == (
            "1",
            "transcribed",
            ["de"],
        )
        assert first.notes == "postmark faint"
        assert first.provenance.method == "vision"
        assert first.provenance.engine == "llm-vision:fake-model"
        assert (second.status, transcription.page_text(second)) == (
            "blank",
            "",
        )
        assert transcription.record() == parse_record(answer)
        assert transcription.flag is None

    def test_the_record_s_page_numbers_are_read_by_their_accessor(
        self, tmp_path
    ):
        """Extraction reads the page numbers printed on the pages from the
        record, by the physical index, not from the text."""
        answer = a_record(
            record_page(1, CAPTION, printed_page="iv"),
            record_page(2, NATURALISATION, printed_page="12"),
            record_page(3, "", status="blank"),
        )
        transcription = LlmVisionTranscriber(FakeClient(answer)).transcribe(
            a_scan(tmp_path)
        )
        assert transcription.printed_pages() == {1: "iv", 2: "12"}
        text_file = Transcription(
            artifact=ArtifactRef(sha1="a" * 40), text=CAPTION, engine="x"
        )
        assert text_file.printed_pages() == {}

    @pytest.mark.parametrize(
        "text, languages, code",
        [
            (FRISIAN, ["fy"], "fy"),
            (RUSSIAN, ["ru"], "ru"),
            (FADED, ["de"], "de"),
        ],
    )
    def test_the_language_hint_comes_from_the_record(
        self, tmp_path, text, languages, code
    ):
        client = FakeClient(record_answer(text, languages=languages))
        transcriber = LlmVisionTranscriber(client, language="auto")
        assert transcriber.transcribe(a_scan(tmp_path)).language == code
        assert transcriber.last_resolution.source == "record"

    def test_an_explicit_language_is_still_forced(self, tmp_path):
        client = FakeClient(record_answer(FRISIAN, languages=["fy"]))
        transcriber = LlmVisionTranscriber(client, language="nl")
        assert transcriber.transcribe(a_scan(tmp_path)).language == "nl"

    def test_a_record_naming_no_language_falls_back(self, tmp_path):
        answer = a_record(
            record_page(1, "", status="blank"), document_class="photograph"
        )
        transcriber = LlmVisionTranscriber(
            FakeClient(answer), language="auto", fallback_language="de"
        )
        transcription = transcriber.transcribe(a_scan(tmp_path))
        assert transcription.language == "de"
        assert transcriber.last_resolution.source == "fallback"

    def test_an_answer_the_cli_gave_up_on_fails_as_cut_off(self, tmp_path):
        client = FakeClient(OutputLimitReached(), record_answer(CAPTION))
        transcriber = LlmVisionTranscriber(client)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(a_scan(tmp_path))
        assert caught.value.check.failure == CUT_OFF
        assert isinstance(caught.value.__cause__, OutputLimitReached)
        # nothing was kept, so nothing is asked for again in the call
        assert len(client.prompts) == 1

    def test_an_answer_stopped_at_max_tokens_fails_and_is_rejected(
        self, tmp_path
    ):
        """An answer whose engine reports the stop reason max_tokens is
        cut off, whatever it holds, a whole record included."""
        client = FakeClient(record_answer(CAPTION), stop_reason="max_tokens")
        with pytest.raises(TranscriptionCheckFailed) as caught:
            LlmVisionTranscriber(client).transcribe(a_scan(tmp_path))
        assert caught.value.check.failure == CUT_OFF
        assert len(client.rejected) == 1
        assert "(cut-off)" in client.rejected[0]

    def test_a_prose_answer_fails_and_is_rejected(self, tmp_path):
        client = FakeClient("Извините, я не могу расшифровать этот документ.")
        transcriber = LlmVisionTranscriber(client)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(a_scan(tmp_path))
        assert caught.value.check.failure == NOT_A_RECORD
        assert transcriber.last_check == caught.value.check
        assert caught.value.check.pages == 1
        assert len(client.rejected) == 1

    def test_a_flagged_record_goes_on_with_its_flag(self, tmp_path):
        client = FakeClient(record_answer("I can't transcribe this document."))
        transcriber = LlmVisionTranscriber(client)
        transcription = transcriber.transcribe(a_scan(tmp_path))
        assert transcription.flag == "refusal-transcribe"
        assert transcriber.last_check.flagged
        assert json.loads(transcription.model_dump_json())["flag"] == (
            "refusal-transcribe"
        )


class TestLanguageOutsideTheMarks:
    def test_the_marks_are_not_read_as_words(self):
        """The faded greeting reads as the German it is: the marks are the
        transcription's, not the postcard's."""
        code, confidence = detect_language(FADED)
        assert code == "de"
        assert confidence >= 0.5
        resolution = resolve_language(FADED, "auto", fallback="en")
        assert (resolution.code, resolution.source) == ("de", "detected")

    def test_a_line_of_marks_alone_is_too_short_to_detect(self):
        line = "[illegible] [?] [redacted] [Page 2] 19.8.38 [?] [illegible]"
        assert detect_language(line) == (None, 0.0)

    def test_the_record_s_main_language_holds_most_of_its_text(self):
        pages = [
            RecordPage(
                page=1, status="transcribed", text=CAPTION, languages=["en"]
            ),
            RecordPage(
                page=2,
                status="transcribed",
                text=POSTCARD,
                languages=["de", "en"],
            ),
            RecordPage(page=3, status="blank"),
        ]
        assert main_language(pages) == "de"
        assert main_language(pages[:1]) == "en"
        assert main_language(pages[2:]) is None
        # a tie goes to the language listed first
        tie = [
            RecordPage(
                page=1, status="transcribed", text="Abc", languages=["fy"]
            ),
            RecordPage(
                page=2, status="transcribed", text="Xyz", languages=["nl"]
            ),
        ]
        assert main_language(tie) == "fy"
        assert record_language(pages, "auto").code == "de"
        assert record_language(pages, "en").source == "override"

    def test_a_language_is_weighed_by_its_primary_subtag(self):
        """German tagged "de-de" on one page and "de" on another is one
        language: either page alone holds less text than the English one,
        and the two together more. It is given by its code as first
        listed."""
        pages = [
            RecordPage(
                page=1,
                status="transcribed",
                text="Liebe Klara",
                languages=["de-de"],
            ),
            RecordPage(
                page=2,
                status="transcribed",
                text="Dein Vater",
                languages=["de"],
            ),
            RecordPage(
                page=3,
                status="transcribed",
                text="Wastl to Klara",
                languages=["en"],
            ),
        ]
        assert main_language([pages[0], pages[2]]) == "en"
        assert main_language([pages[1], pages[2]]) == "en"
        assert main_language(pages) == "de-de"


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
TOOL_RESULT = {
    "type": "user",
    "message": {
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
        ]
    },
}


class TestTheAnswerIsTakenFromTheStream:
    def test_narration_before_a_tool_call_is_no_part_of_the_answer(self):
        record = record_answer(CAPTION)
        stdout = stream(
            assistant(text("Let me read the image."), TOOL_USE),
            TOOL_RESULT,
            assistant(text(record)),
            {"type": "result", "result": record},
        )
        assert ClaudeCodeClient._assemble_stream(stdout) == record

    def test_only_the_text_after_the_last_tool_result_counts(self):
        stdout = stream(
            assistant(text("I'll read the first page."), TOOL_USE),
            TOOL_RESULT,
            assistant(text("Now the second."), TOOL_USE),
            TOOL_RESULT,
            assistant(text('{"document_class": ')),
            assistant(text('"letter"}')),
        )
        # a continuation across turns still joins, with no separator
        assert ClaudeCodeClient._assemble_stream(stdout) == (
            '{"document_class": "letter"}'
        )

    def test_a_model_that_spoke_only_before_its_tool_result_gave_none(self):
        stdout = stream(
            assistant(text("Let me read the image."), TOOL_USE),
            TOOL_RESULT,
            {"type": "result", "result": "Let me read the image."},
        )
        assert ClaudeCodeClient._assemble_stream(stdout) == ""

    def test_a_text_block_that_is_no_string_is_passed_over(self):
        record = record_answer(CAPTION)
        stdout = stream(
            assistant({"type": "text", "text": None}),
            assistant(text(record)),
        )
        assert ClaudeCodeClient._assemble_stream(stdout) == record
        stdout = stream(
            assistant({"type": "text", "text": ["no"]}),
            {"type": "result", "result": "from the result"},
        )
        assert ClaudeCodeClient._assemble_stream(stdout) == "from the result"

    def test_without_assistant_text_the_result_event_answers(self):
        stdout = stream(
            assistant(TOOL_USE),
            {"type": "result", "result": "from the result"},
        )
        assert ClaudeCodeClient._assemble_stream(stdout) == "from the result"

    def test_a_record_after_narration_is_read_as_the_record(self):
        """What the check sees is the record alone, never the narration
        before the tool call."""
        record = record_answer(CAPTION)
        stdout = stream(
            assistant(text("Let me read the image.")),
            assistant(TOOL_USE),
            TOOL_RESULT,
            assistant(text(record)),
        )
        answer = ClaudeCodeClient._assemble_stream(stdout)
        check, parsed = check_answer(answer, pages=1)
        assert check.passed
        assert parsed.pages[0].text == CAPTION
        assert page_text(CAPTION) == record_text(parsed.pages)[0]
