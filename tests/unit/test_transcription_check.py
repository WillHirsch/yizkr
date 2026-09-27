"""The transcription check (#175): tool-failure and refusal outputs never
pass as transcriptions, a near-empty multi-page output fails, and short
genuine pages pass. A tool error the engine's own stream reports on the
input fails the output whatever it says, with or without the LLM cache
(#213).

Every failure and refusal output below is invented engine text. Every
genuine text is a source passage of the fictional universe
(docs/EXAMPLES.md), a generic line with no names in it, or an invented
letter or email between the universe's people. The one exception is
SURNAME_LINES: the universe has no one whose surname is also a tool's
name, so those lines invent a witness in its town.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_claude_code_pinning import (
    RENDERER_MISSING,
    TOO_MANY_PAGES,
    install_fake_cli,
    read_events,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline import transcribe, transcription_check
from yizkr.pipeline.llm import (
    ClaudeCodeClient,
    EngineToolError,
    ToolFailure,
    cached_client,
    open_cache,
)
from yizkr.pipeline.orchestrate import (
    BATCHED,
    FAILED,
    DocumentRun,
    Orchestrator,
)
from yizkr.pipeline.transcribe import (
    TRANSCRIPTION_PROMPT,
    LlmVisionTranscriber,
    MissingPrerequisite,
    TextFileTranscriber,
    preflight_vision_transcription,
    require_pdf_renderer,
)
from yizkr.pipeline.transcription_check import (
    CHECK_STAGE,
    CHECK_VERSION,
    EMPTY,
    MIN_TEXT_CHARS_PER_PAGE,
    REFUSAL,
    SHORT_OUTPUT_CHARS,
    SIGNATURES,
    TOO_SHORT,
    TOOL_ERROR,
    TOOL_FAILURE,
    TranscriptionCheck,
    TranscriptionCheckFailed,
    check_transcription,
    media_page_count,
    minimum_text_chars,
    require_valid_transcription,
    text_char_count,
)

# (signature expected to fire, invented engine output)
FAILURES = [
    (
        "pdf-renderer",
        "I wasn't able to read the PDF. The Read tool needs pdftoppm "
        "(from poppler-utils) to render PDF pages as images, and it is not "
        "installed on this system. To transcribe the document you could "
        "install poppler-utils, or convert each page to a PNG image and "
        "share those instead.",
    ),
    ("pdf-renderer", "To go on, install poppler and try again."),
    (
        "traceback",
        "Traceback (most recent call last):\n"
        '  File "render.py", line 3, in <module>\n'
        "RuntimeError: page renderer unavailable",
    ),
    ("shell-error", "cat: /tmp/page-1.png: No such file or directory"),
    (
        "engine-error",
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}',
    ),
    ("engine-error", "Claude AI usage limit reached|1790000000"),
    ("engine-error", "Error: Reached max turns (30)"),
    (
        "media-unreadable",
        "I was unable to open the attached image; it may be damaged.",
    ),
    (
        "media-unreadable",
        "The PDF could not be rendered, so there is no text.",
    ),
    ("media-unreadable", "The image file appears to be corrupted."),
    (
        "media-missing",
        "I don't see any image attached to your message. Could you share "
        "the document you would like transcribed?",
    ),
    ("media-missing", "No image was provided with this request."),
    ("narration-only", "I'll start by reading the image file."),
    ("narration-only", "Let me read the PDF first"),
    (
        "refusal-transcribe",
        "I can't transcribe this document, because it appears to contain "
        "personal information about private individuals.",
    ),
    (
        "refusal-transcribe",
        "I’m not able to provide a transcription of this image.",
    ),
    ("refusal-request", "I'm unable to assist with this request."),
    (
        "ai-disclaimer",
        "As an AI, I do not have the ability to look at pictures.",
    ),
    ("refusal-short", "I'm sorry, but I can't help with that."),
    ("refusal-short", "Sorry, I can't do that."),
    ("refusal-short", "I'm unable to complete this task."),
]

# said of page 2 after page 1's text: the page that failed is missing
PAGE_FAILURE = "I was unable to read page 2 of the PDF, so it is left out."

# ordinary letter sentences that share words with a refusal
LETTER = (
    "Dear cousin, I am sorry, but I cannot come to you this summer. "
    "Unfortunately I cannot help with that either, as the shop stays "
    "closed until the spring and every coin goes to the rent. The "
    "children are well and send their love; the youngest has started "
    "school and reads everything she can find, even the newspaper. "
    "Write soon and tell me how the new house is coming along."
)


# a letter declining a task in its own words
TASK_LETTER = (
    "Dear brother, I could not complete this task before the winter came, "
    "as father was ill and the roads were closed for weeks."
)

# a short genuine text: the studio's stamp on the universe's studio
# portrait
STAMP = "Photograph Brüsrich · Peiltal"

# an invented witness whose surname is also the PDF renderer's package
SURNAME_LINES = [
    "Witness: A. Poppler, of Flörburg, aged 40.",
    "WITNESS: A. POPPLER, OF FLÖRBURG, AGED 40.",
    "Received from Poppler, Flörburg, the sum of 40 marks.",
    "The congregation installed Poppler as its cantor.",
]

# emails and letters about family research that say what an engine's note
# says, of their own attachments: naming who sent them, what they came in,
# what they hold or where they were opened, or in the middle of a message
CORRESPONDENCE = [
    "Dear cousin, thank you for your email. I couldn't open the attachment "
    "you sent, could you send the scans again? I would like to add the "
    "postcard to the tree before the reunion. Love, Nicholas",
    "Hi Nicholas, I don't see any attachment on your last message. Best, "
    "Clara",
    "Sorry, I wasn't able to open the PDF of the census page. Can you "
    "resend? I want to check the ages against the birth extract before I "
    "write to the Standesamt Flörburg. Love, Nicholas",
    "No scans were included in the envelope.",
    "Unfortunately the image files could not be opened on my computer.",
    "Dear Nicholas, I couldn't open the PDF, so could you post a copy?",
    # opening the message, freed only by naming who sent the attachment
    "I couldn't open the attachment you sent, could you send the scans again?",
    # asking to be told is no announcement of the engine's
    "Let me know if you need the scans. I couldn't open the PDF, so could "
    "you post a copy?",
    # a page named in the writer's own plan is no page the note skipped
    "Dear Clara,\n\nThe children are well. I'll look at page 3 again "
    "tomorrow. I couldn't see it clearly. Love, Nicholas",
    "I'll send you the last two pages next week. I couldn't read them "
    "properly.",
]

# replies that decline their reader's request in an engine refusal's words
REPLIES = [
    "We regret that we are unable to comply with your request for copies "
    "of the register.",
    "I am unable to comply with your request for copies of the register, "
    "as the office is closed until the spring.",
]

# letters that decline to transcribe their own writing, which they name
TRANSCRIBING_LETTERS = [
    "I can't transcribe the old handwriting on the back of the photograph; "
    "could you?",
    "Dear Clara, I cannot transcribe the old script, so I am sending you a "
    "copy of the postcard.",
]

# engine notes on the pages they skipped, each after a page of text
PAGE_FAILURES = [
    PAGE_FAILURE,
    # opening a page section, after its heading
    "--- Page 2 ---\nThe image could not be loaded.",
    "Page 2: I could not open the image.",
    "[Page 2]\nI could not open the image.",
    "**[Page 2]**\nI could not open the image.",
    # opening a labelled line
    "Note: I was unable to open the second image, so it is not transcribed.",
    "(Note: I was unable to open the second image, so it is not transcribed.)",
    "*Note: I was unable to open the second image, so it is not transcribed.*",
    "Error: I was unable to open the second image.",
    "Warning: I was unable to open the second image.",
    "**Note**: I was unable to open the second image.",
    "*Note*: I was unable to open the second image.",
    "NB: I was unable to open the second image.",
    # naming the pages skipped
    "The PDF could not be rendered beyond page 1.",
    "I could not read the rest of the PDF.",
    "(The remaining pages could not be read.)",
    "Page 2 could not be read.",
    "I couldn't read the remaining pages.",
    "I could not read pages 3 and 4.",
    "Pages 2 and 3 could not be read.",
    "The last two pages could not be read.",
    "I couldn't read the second and third pages.",
]


def universe_texts() -> list[str]:
    return [source.text for source in u.SOURCES if source.text]


def a_page() -> str:
    return max(universe_texts(), key=len)


def a_caption() -> str:
    return min(universe_texts(), key=len)


class TestSignatures:
    @pytest.mark.parametrize(("name", "text"), FAILURES)
    def test_failure_outputs_fail(self, name, text):
        check = check_transcription(text, pages=1)
        assert not check.passed
        assert check.signature == name
        assert check.failure in (TOOL_FAILURE, REFUSAL)
        assert name in check.reason

    def test_every_signature_has_a_failing_example(self):
        # a new signature arrives with an example that exercises it
        exercised = {name for name, _ in FAILURES}
        assert exercised == {signature.name for signature in SIGNATURES}

    def test_kinds(self):
        assert check_transcription(FAILURES[0][1]).failure == TOOL_FAILURE
        refusal = "I'm unable to assist with this request."
        assert check_transcription(refusal).failure == REFUSAL

    @pytest.mark.parametrize("note", PAGE_FAILURES)
    def test_failure_partway_through_fails_the_whole_output(self, note):
        text = f"{a_page()}\n\n{note}\n"
        check = check_transcription(text, pages=2)
        assert not check.passed
        assert check.signature == "media-unreadable"

    @pytest.mark.parametrize(
        ("name", "text"),
        [
            # the claude-code engine joins its turns without a separator
            (
                "media-unreadable",
                "I'll read the PDF file.I wasn't able to open the PDF; it "
                "may be encrypted.",
            ),
            ("media-unreadable", "Error: the PDF could not be read."),
            (
                "media-missing",
                "It looks like no image was attached to your message.",
            ),
            ("refusal-request", "Sorry, but I can't help with this request."),
            # a request for a transcription is the task's own
            (
                "refusal-request",
                "I cannot fulfil your request for a transcription of these "
                "records.",
            ),
            # declining the task's document, or nothing named
            ("refusal-transcribe", "I can't transcribe it."),
            (
                "refusal-transcribe",
                "I'm unable to transcribe the handwriting in this image "
                "because it is too faint.",
            ),
            (
                "refusal-transcribe",
                "I cannot transcribe because the image is too blurry.",
            ),
            # a hedging preamble that declines the task's document
            (
                "refusal-transcribe",
                "I cannot transcribe this image fully, but here is what I "
                "can read:",
            ),
            # declining part of the task's document
            (
                "refusal-transcribe",
                "I'm unable to transcribe the rest of this document.",
            ),
            (
                "refusal-transcribe",
                "I'm unable to transcribe most of this page.",
            ),
            (
                "refusal-transcribe",
                "I'm unable to transcribe the handwritten portion of this "
                "document.",
            ),
            # nothing named: the sentence ends
            ("refusal-transcribe", "I'm not able to provide a transcription."),
            # a sentence announcing the task, naming the medium or the tool
            (
                "media-unreadable",
                "Let me take a look at the image.I'm unable to open the "
                "image file; it may be corrupted.",
            ),
            (
                "media-unreadable",
                "I'll use the Read tool to examine the image.I couldn't open "
                "the image.",
            ),
            (
                "media-missing",
                "I'll analyze the image.I don't see any image at that path.",
            ),
            # the announcement joined straight onto the first page heading
            (
                "media-unreadable",
                "I'll read the PDF file.**Page 1**\n\nI was unable to open "
                "this image.",
            ),
            (
                "refusal-transcribe",
                "Page 2 (back)\nI can't transcribe this page.",
            ),
            # words between the determiner and the medium
            ("media-unreadable", "The scanned image could not be read."),
            ("media-unreadable", "The second image could not be loaded."),
            ("media-unreadable", "The PDF file appears to be corrupted."),
            # the prompt's own wording is the task's, not a correspondent's
            ("media-unreadable", "I couldn't open the image of the document."),
            # an announcement alone: the engine stopped before transcribing
            ("narration-only", "Let me take a look at the image."),
            ("narration-only", "I'll use the Read tool to examine the image."),
            ("narration-only", "I'll analyze the image."),
            # an interjection or a heading before the note
            (
                "media-unreadable",
                "Okay, let me read the image.I couldn't open the image.",
            ),
            (
                "media-unreadable",
                "First, let me read the image.I couldn't open the image.",
            ),
            (
                "media-unreadable",
                "Let me take a look.I couldn't open the image.",
            ),
            (
                "media-unreadable",
                "## Transcription\n\nI was unable to read the PDF.",
            ),
            # a file, "it" after the announcement, the engine's "you provided"
            ("media-unreadable", "I couldn't read the file."),
            ("media-unreadable", "I'll read the image.I couldn't open it."),
            ("media-unreadable", "The image you provided could not be read."),
            # a page mark inside a line is no page heading, and does not hide
            # the label or apology before it
            ("media-unreadable", "Note: [Page 2] I couldn't open the image."),
            (
                "media-unreadable",
                "Transcription: [Page 1] I couldn't open the image.",
            ),
            (
                "refusal-transcribe",
                "I'm sorry, but [Page 1] I cannot transcribe this document.",
            ),
        ],
    )
    def test_engine_note_opening_the_output_fails(self, name, text):
        check = check_transcription(text, pages=1)
        assert not check.passed
        assert check.signature == name

    @pytest.mark.parametrize(
        "note",
        [
            "I can't transcribe page 2 because it is faint.",
            "Page 2 (back)\nI can't transcribe this page.",
            "Page 2 - back\nI can't transcribe this page.",
        ],
    )
    def test_refusal_on_a_skipped_page_fails_after_text(self, note):
        check = check_transcription(f"{a_page()}\n\n{note}", pages=2)
        assert not check.passed
        assert check.signature == "refusal-transcribe"

    def test_every_note_is_judged_not_only_the_first(self):
        # a correspondent's own note first does not hide the engine's note
        # on a later page
        text = (
            "Sorry, I wasn't able to open the PDF of the census page.\n\n"
            "Page 2: I could not open the image."
        )
        check = check_transcription(text, pages=2)
        assert not check.passed
        assert check.signature == "media-unreadable"

    @pytest.mark.parametrize(
        "text",
        ["first and " * 4000 + "x", "Let me read the image, " * 4000 + "x"],
    )
    def test_long_repetitive_text_checks_quickly(self, text):
        # no pattern backtracks across a long run of list words, or of
        # announcement-like phrases on one line. The limit is on this
        # process's CPU time, which other load on the machine does not
        # inflate, and is far above a linear check's milliseconds while
        # far below a backtracking one's minutes (#223)
        started = time.process_time()
        check_transcription(text)
        assert time.process_time() - started < 5.0

    def test_page_that_could_not_be_read_fails_even_in_a_letter(self):
        # a page named as unread fails anywhere: a silent partial
        # transcription is worse than a visible failure, so a letter that
        # says so fails too
        line = "The last page could not be read, the ink had run."
        assert not check_transcription(f"{a_page()}\n{line}", pages=2).passed

    @pytest.mark.parametrize("line", TRANSCRIBING_LETTERS)
    def test_letter_declining_its_own_writing_passes(self, line):
        # a letter names the writing it cannot transcribe; the engine
        # declines the task's document ("this document", "it")
        assert check_transcription(line, pages=1).passed
        assert check_transcription(f"{a_page()}\n{line}", pages=2).passed

    @pytest.mark.parametrize("line", CORRESPONDENCE)
    def test_correspondence_about_its_own_media_passes(self, line):
        # an email or letter names its own attachment, or says it in the
        # middle of a message; alone, or after a page of text
        assert check_transcription(line, pages=1).passed
        assert check_transcription(f"{a_page()}\n{line}", pages=2).passed

    @pytest.mark.parametrize("line", REPLIES)
    def test_reply_declining_its_readers_request_passes(self, line):
        # an archive's or a registrar's reply names the request it declines
        assert check_transcription(line, pages=1).passed
        assert check_transcription(f"{a_page()}\n{line}", pages=2).passed

    def test_same_note_mid_text_passes_unless_it_names_a_page(self):
        # in the middle of a text, a note is a source's own sentence unless
        # it names the pages it skipped
        note = "I couldn't open the PDF."
        assert not check_transcription(note, pages=1).passed
        assert check_transcription(f"{a_page()}\n{note}", pages=2).passed
        skipped = f"{a_page()}\nI couldn't open page 2 of the PDF."
        assert not check_transcription(skipped, pages=2).passed

    @pytest.mark.parametrize("text", universe_texts())
    def test_universe_source_texts_pass(self, text):
        for pages in (None, 1):
            assert check_transcription(text, pages=pages).passed

    def test_letter_with_refusal_words_passes(self):
        assert check_transcription(LETTER, pages=1).passed

    @pytest.mark.parametrize(
        "text",
        [
            "pdftoppm is not installed.",
            "Please install poppler-utils.",
            "The Poppler utils are missing.",
            "Try: brew install poppler",
            "It renders pages with a tool from poppler.",
        ],
    )
    def test_renderer_package_phrases_fail(self, text):
        check = check_transcription(text, pages=1)
        assert check.signature == "pdf-renderer"

    @pytest.mark.parametrize("line", SURNAME_LINES)
    def test_surname_that_names_a_tool_passes(self, line):
        # a record may name a person Poppler; only the package's own
        # phrases ("poppler-utils", "install poppler") are a tool's words
        assert check_transcription(line, pages=1).passed
        assert check_transcription(f"{a_page()}\n{line}", pages=2).passed

    def test_letter_declining_a_task_passes(self):
        # declining a task is ordinary prose, alone or inside a letter
        assert check_transcription(TASK_LETTER, pages=1).passed
        assert check_transcription(f"{LETTER} {TASK_LETTER}", pages=1).passed

    def test_bare_decline_fails_only_as_a_short_whole_output(self):
        decline = "I am sorry, but I cannot help with that."
        assert not check_transcription(decline).passed
        # the same words opening a long letter are the letter's words
        letter = decline + " " + LETTER
        assert len(letter) > SHORT_OUTPUT_CHARS
        assert check_transcription(letter).passed

    def test_preamble_before_text_passes(self):
        # the claude-code engine joins its turns without a separator, so a
        # sentence announcing the read can run straight into the text
        text = f"Let me read the image file.{a_caption()}"
        assert check_transcription(text, pages=1).passed

    def test_bracketed_note_is_an_editorial_mark(self):
        # the prompt asks for unreadable passages to be marked in brackets
        text = f"{a_page()} [I cannot transcribe the stamp reliably]"
        assert check_transcription(text, pages=1).passed

    def test_tool_token_in_brackets_still_fails(self):
        text = f"{a_page()} [pdftoppm failed on page 2]"
        check = check_transcription(text, pages=2)
        assert not check.passed
        assert check.signature == "pdf-renderer"


class TestPlausibility:
    def test_floor(self):
        assert minimum_text_chars(None) == 0
        assert minimum_text_chars(1) == 0
        assert minimum_text_chars(2) == 2 * MIN_TEXT_CHARS_PER_PAGE
        assert minimum_text_chars(10) == 10 * MIN_TEXT_CHARS_PER_PAGE

    def test_editorial_marks_do_not_count(self):
        assert text_char_count("Brunnen[?]ring [illegible] 35") == len(
            "Brunnenring35"
        )

    def test_near_empty_multi_page_output_fails(self):
        check = check_transcription("[illegible]\n\n[illegible]", pages=3)
        assert not check.passed
        assert check.failure == TOO_SHORT
        assert check.text_chars == 0
        assert check.min_text_chars == 3 * MIN_TEXT_CHARS_PER_PAGE
        assert "3 pages" in check.reason

    def test_single_image_has_no_floor(self):
        # a photograph may carry no text at all
        assert check_transcription("[illegible]", pages=1).passed
        assert check_transcription("[no text]", pages=1).passed

    def test_unknown_page_count_has_no_floor(self):
        assert check_transcription("[illegible]", pages=None).passed

    def test_short_but_genuine_pages_pass(self):
        # a single short page, and the only writing on a two-page scan (a
        # photograph's front and its captioned back)
        assert check_transcription(a_caption(), pages=1).passed
        assert check_transcription(a_caption(), pages=2).passed
        assert check_transcription(STAMP, pages=2).passed

    def test_at_the_floor_passes_and_below_fails(self):
        floor = minimum_text_chars(4)
        assert check_transcription("x" * floor, pages=4).passed
        assert not check_transcription("x" * (floor - 1), pages=4).passed

    def test_blank_output_fails(self):
        check = check_transcription("  \n\t ", pages=1)
        assert not check.passed
        assert check.failure == EMPTY


class TestVerdict:
    def test_step_has_its_own_name_and_version(self):
        check = check_transcription(STAMP)
        assert check.stage == CHECK_STAGE == "transcription-check"
        assert check.version == CHECK_VERSION

    def test_verdict_round_trips_as_json(self):
        check = check_transcription(FAILURES[0][1], pages=3)
        again = TranscriptionCheck.model_validate_json(check.model_dump_json())
        assert again == check

    def test_require_raises_with_the_reason(self):
        with pytest.raises(TranscriptionCheckFailed) as caught:
            require_valid_transcription(FAILURES[0][1], pages=3)
        check = caught.value.check
        assert check.signature == "pdf-renderer"
        assert check.reason in str(caught.value)
        assert f"v{CHECK_VERSION}" in str(caught.value)

    def test_require_returns_a_passing_verdict(self):
        assert require_valid_transcription(STAMP).passed

    def test_recorded_phrase_is_capped(self):
        text = "Let me read the image file" + " very carefully" * 20 + "."
        check = check_transcription(text)
        assert check.signature == "narration-only"
        assert len(check.matched) == 80
        assert repr(check.matched) in check.reason


def minimal_pdf(pages: int) -> bytes:
    """A valid, blank PDF of `pages` pages, xref offsets included."""
    kids = " ".join(f"{3 + index} 0 R" for index in range(pages))
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
    ] + [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


class FakeRun:
    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=self.stdout, stderr=""
        )


class TestMediaPageCount:
    def test_image_is_one_page(self, tmp_path):
        assert media_page_count(tmp_path / "scan.jpg") == 1
        assert media_page_count(tmp_path / "scan.TIFF") == 1

    def test_other_files_are_unknown(self, tmp_path):
        assert media_page_count(tmp_path / "interview.txt") is None

    def test_pdf_count_comes_from_pdfinfo(self, tmp_path, monkeypatch):
        fake = FakeRun(stdout="Title:  x\nPages:          3\nEncrypted: no\n")
        monkeypatch.setattr(transcription_check.subprocess, "run", fake)
        assert media_page_count(tmp_path / "scan.pdf") == 3
        assert fake.calls[0][0] == "pdfinfo"

    @pytest.mark.parametrize(
        "fake",
        [
            FakeRun(error=FileNotFoundError("pdfinfo")),
            FakeRun(returncode=1),
            FakeRun(stdout="Title: x\n"),
        ],
    )
    def test_unreadable_pdf_is_unknown(self, tmp_path, monkeypatch, fake):
        monkeypatch.setattr(transcription_check.subprocess, "run", fake)
        assert media_page_count(tmp_path / "scan.pdf") is None

    @pytest.mark.skipif(
        shutil.which("pdfinfo") is None, reason="poppler-utils not installed"
    )
    def test_real_pdfinfo(self, tmp_path):
        pdf = tmp_path / "scan.pdf"
        pdf.write_bytes(minimal_pdf(2))
        assert media_page_count(pdf) == 2


class FakeLlm:
    model_identifier = "fake-model"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, prompt, image_paths=()):
        self.calls += 1
        return self.responses.pop(0)


class KeepingLlm(FakeLlm):
    """A client that keeps answers, as the LLM cache's does: the first
    `kept` answers are ones it kept from earlier calls, and a rejection
    says whether the answer rejected was one of them."""

    def __init__(self, *responses: str, kept: int = 1) -> None:
        super().__init__(*responses)
        self.kept = kept
        self.rejected: list[tuple[str, bool]] = []

    def complete(self, prompt, image_paths=()):
        self.last_kept = self.calls < self.kept
        return super().complete(prompt, image_paths)

    def reject(self, response, reason):
        self.rejected.append((response, self.last_kept))
        return self.last_kept


def no_tools(name):
    return None


def all_tools(name):
    return f"/usr/bin/{name}"


class TestPreflight:
    def test_missing_renderer_names_the_fix(self):
        with pytest.raises(MissingPrerequisite) as caught:
            require_pdf_renderer(no_tools)
        message = str(caught.value)
        assert "pdftoppm" in message
        assert "poppler-utils" in message
        assert "apt-get install poppler-utils" in message

    def test_present_renderer_passes(self):
        require_pdf_renderer(all_tools)

    def test_run_preflight_asks_only_for_pdfs(self, tmp_path):
        asked = []

        def which(name):
            asked.append(name)
            return None

        preflight_vision_transcription([tmp_path / "a.jpg"], which)
        assert asked == []
        with pytest.raises(MissingPrerequisite):
            preflight_vision_transcription(
                [tmp_path / "a.jpg", tmp_path / "b.PDF"], which
            )

    def test_the_transcriber_leaves_the_host_check_to_the_run(
        self, tmp_path, monkeypatch
    ):
        """A run checks the host once, before any document, with
        preflight_vision_transcription (tests/unit/test_orchestrate.py,
        #225); the transcriber does not ask again for each document."""
        asked = []
        monkeypatch.setattr(transcribe.shutil, "which", asked.append)
        monkeypatch.setattr(
            transcription_check.subprocess, "run", FakeRun("Pages: 1\n")
        )
        result = LlmVisionTranscriber(FakeLlm(STAMP)).transcribe(
            tmp_path / "scan.pdf"
        )
        assert result.text == STAMP
        assert asked == []

    def test_images_need_no_renderer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe.shutil, "which", no_tools)
        llm = FakeLlm(STAMP)
        result = LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert result.text == STAMP


class TestTranscribers:
    def test_vision_failure_raises_and_returns_nothing(self, tmp_path):
        transcriber = LlmVisionTranscriber(FakeLlm(FAILURES[0][1]))
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(tmp_path / "scan.png", artifact_qid="Q23")
        assert caught.value.check.signature == "pdf-renderer"
        assert transcriber.last_check == caught.value.check

    def test_vision_pdf_uses_its_page_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe.shutil, "which", all_tools)
        monkeypatch.setattr(
            transcription_check.subprocess, "run", FakeRun("Pages: 5\n")
        )
        transcriber = LlmVisionTranscriber(FakeLlm("[illegible]"))
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(tmp_path / "scan.pdf")
        assert caught.value.check.failure == TOO_SHORT
        assert caught.value.check.pages == 5

    def test_passing_output_is_the_unchecked_output(self, tmp_path):
        # the check adds a verdict and changes nothing in the transcription
        text = a_page()
        checked = LlmVisionTranscriber(FakeLlm(text)).transcribe(
            tmp_path / "scan.png", artifact_qid="Q23"
        )
        unchecked = LlmVisionTranscriber(FakeLlm(text)).transcribe_unchecked(
            tmp_path / "scan.png", artifact_qid="Q23"
        )
        assert checked == unchecked

    def test_unchecked_step_returns_a_failing_output(self, tmp_path):
        # for a caller that runs the check as a step of its own
        result = LlmVisionTranscriber(
            FakeLlm(FAILURES[0][1])
        ).transcribe_unchecked(tmp_path / "scan.png")
        assert "pdftoppm" in result.text

    def test_a_kept_answer_that_fails_is_asked_for_once_more(self, tmp_path):
        """A failing answer the client kept from an earlier call is
        rejected, and the engine is asked in the same call, as it would be
        without the cache; its answer is checked in its place (#230)."""
        failing = FAILURES[0][1]
        llm = KeepingLlm(failing, a_page())
        result = LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert result.text == a_page()
        assert llm.calls == 2
        assert llm.rejected == [(failing, True)]

    def test_the_engines_failing_answer_is_not_asked_for_again(self, tmp_path):
        llm = KeepingLlm(FAILURES[0][1], a_page(), kept=0)
        with pytest.raises(TranscriptionCheckFailed):
            LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert llm.calls == 1
        assert llm.rejected == [(FAILURES[0][1], False)]

    def test_a_kept_answer_is_asked_for_once_more_at_most(self, tmp_path):
        failing = FAILURES[0][1]
        llm = KeepingLlm(failing, failing, a_page(), kept=2)
        with pytest.raises(TranscriptionCheckFailed):
            LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert llm.calls == 2

    def test_text_file_with_a_refusal_raises(self, tmp_path):
        text_file = tmp_path / "external-tool-output.txt"
        text_file.write_text("I'm sorry, but I can't help with that.\n")
        transcriber = TextFileTranscriber(text_file)
        with pytest.raises(TranscriptionCheckFailed):
            transcriber.transcribe(text_file)

    def test_text_file_short_genuine_text_passes(self, tmp_path):
        text_file = tmp_path / "receipt.txt"
        text_file.write_text("1937")
        result = TextFileTranscriber(text_file).transcribe(text_file)
        assert result.text == "1937"

    @pytest.mark.parametrize(
        "line", CORRESPONDENCE + REPLIES + TRANSCRIBING_LETTERS
    )
    def test_text_file_of_correspondence_passes(self, tmp_path, line):
        # an email or letter kept as a text file is checked too
        text_file = tmp_path / "email.txt"
        text_file.write_text(line)
        result = TextFileTranscriber(text_file).transcribe(text_file)
        assert result.text == line


class ScriptedLlm(KeepingLlm):
    """A keeping client whose steps are answers, or errors the engine
    raises instead of answering (an EngineToolError, say)."""

    def complete(self, prompt, image_paths=()):
        self.last_kept = self.calls < self.kept
        self.calls += 1
        step = self.responses.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def tool_error(*errors: str, response: str | None = None) -> EngineToolError:
    """What the engine raises when its stream reports the Read tool failing
    on the input with each of `errors`."""
    return EngineToolError(
        [ToolFailure("Read", error, 0) for error in errors],
        a_page() if response is None else response,
    )


class TestToolErrors:
    """The engine's own record of a call says its Read tool failed on the
    input (#213): the output fails whatever it says, and the verdict names
    the tool and the class of its error, never the output's words."""

    def test_a_tool_error_fails_whatever_the_output_says(self):
        text = a_page()
        assert check_transcription(text, pages=1).passed
        failure = ToolFailure("Read", "pdf-renderer-missing", 0)
        check = check_transcription(text, pages=1, tool_failures=[failure])
        assert not check.passed
        assert check.failure == TOOL_ERROR
        assert (check.tool, check.tool_error) == (
            "Read",
            "pdf-renderer-missing",
        )
        assert check.signature is None and check.matched is None
        assert check.text_chars == text_char_count(text)
        assert check.reason.endswith("Read (pdf-renderer-missing)")
        assert text[:40] not in check.reason

    def test_the_reason_names_each_tool_error(self):
        check = check_transcription(
            a_page(),
            tool_failures=[
                ToolFailure("Read", "too-many-pages", 0),
                ToolFailure("Read", "corrupt", 0),
            ],
        )
        assert check.tool_error == "too-many-pages"
        assert check.reason.endswith("Read (too-many-pages), Read (corrupt)")

    def test_a_tool_error_comes_before_the_signatures(self):
        failure = ToolFailure("Read", "pdf-renderer-missing", 0)
        check = check_transcription(FAILURES[0][1], tool_failures=[failure])
        assert check.failure == TOOL_ERROR
        assert check.signature is None
        assert check_transcription("", tool_failures=[failure]).failure == (
            TOOL_ERROR
        )

    def test_verdict_round_trips_and_raises(self):
        failure = ToolFailure("Read", "corrupt", 0)
        check = check_transcription(a_page(), tool_failures=[failure])
        again = TranscriptionCheck.model_validate_json(check.model_dump_json())
        assert again == check
        with pytest.raises(TranscriptionCheckFailed) as caught:
            require_valid_transcription(a_page(), tool_failures=[failure])
        assert str(caught.value) == (
            f"transcription check v{CHECK_VERSION} failed (tool-error): "
            f"{check.reason}"
        )

    def test_the_transcriber_fails_on_the_engines_tool_error(self, tmp_path):
        """The engine answered this call and nothing kept its answer, so
        nothing is rejected and it is not asked for again: the document
        fails, and the next run asks the engine, unless the call was a
        refresh (test_a_refresh_that_raises_replaces_nothing)."""
        llm = ScriptedLlm(tool_error("corrupt"), a_page(), kept=0)
        transcriber = LlmVisionTranscriber(llm)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(tmp_path / "scan.png")
        check = caught.value.check
        assert (check.failure, check.tool_error) == (TOOL_ERROR, "corrupt")
        assert "(tool-error)" in str(caught.value)
        assert "Read (corrupt)" in str(caught.value)
        assert transcriber.last_check == check
        assert isinstance(caught.value.__cause__, EngineToolError)
        assert llm.calls == 1
        assert llm.rejected == []

    def test_a_kept_answer_asked_for_again_can_meet_a_tool_error(
        self, tmp_path
    ):
        failing = FAILURES[0][1]
        llm = ScriptedLlm(failing, tool_error("pdf-renderer-missing"), kept=1)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert caught.value.check.failure == TOOL_ERROR
        assert llm.calls == 2
        assert llm.rejected == [(failing, True)]

    def test_through_the_claude_code_engine(self, tmp_path, monkeypatch):
        """The signal decides whatever the answer says; the wording
        signatures stay a backstop for an answer with no tool error."""
        cli = install_fake_cli(monkeypatch, tmp_path)
        scan = tmp_path / "scan.png"
        scan.write_bytes(b"png placeholder")
        transcriber = LlmVisionTranscriber(ClaudeCodeClient())
        cli.answer = a_page()
        cli.tool_events = read_events(scan.resolve(), error=RENDERER_MISSING)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(scan)
        assert caught.value.check.tool_error == "pdf-renderer-missing"
        cli.tool_events = read_events(scan.resolve())
        cli.answer = FAILURES[0][1]
        with pytest.raises(TranscriptionCheckFailed) as caught:
            transcriber.transcribe(scan)
        assert caught.value.check.signature == "pdf-renderer"
        cli.answer = a_page()
        assert transcriber.transcribe(scan).text == a_page()


class TestToolErrorsWithTheCache:
    """An answer whose engine reported a tool failing on the input is an
    error, so the LLM cache keeps nothing for it (ADR-0032): no later call
    is answered with it, and the next asks the engine again, unless the
    call was a refresh: then the answer the cache kept stays and is served
    (TestOrchestratorContract.test_a_refresh_that_raises_replaces_nothing).
    """

    @pytest.fixture
    def rig(self, tmp_path, monkeypatch):
        cli = install_fake_cli(monkeypatch, tmp_path)
        scan = tmp_path / "scan.png"
        scan.write_bytes(b"png placeholder")
        cache = open_cache(tmp_path / "cache")
        events: list[tuple[str, str | None]] = []

        def client():
            return cached_client(
                ClaudeCodeClient(),
                cache,
                role="transcribe",
                events=lambda event, **fields: events.append(
                    (event, fields.get("reason"))
                ),
            )

        return cli, scan, cache, events, client

    def test_an_answer_with_a_tool_error_is_never_kept(self, rig, tmp_path):
        cli, scan, cache, events, client = rig
        cli.answer = a_page()
        cli.tool_events = read_events(scan.resolve(), error=RENDERER_MISSING)
        wrapped = client()
        with pytest.raises(TranscriptionCheckFailed) as caught:
            LlmVisionTranscriber(wrapped).transcribe(scan)
        assert caught.value.check.failure == TOOL_ERROR
        key = wrapped.complete_key(TRANSCRIPTION_PROMPT, [scan])
        assert cache.get(key) is None
        assert not list((tmp_path / "cache").rglob("*.json"))
        assert events == [("cache-miss", "absent")]
        # the next run asks the engine again: nothing was kept, or rejected
        cli.tool_events = read_events(scan.resolve())
        assert LlmVisionTranscriber(client()).transcribe(scan).text == a_page()
        assert events[1:] == [("cache-miss", "absent")]
        assert cache.get(key)["response"] == a_page()
        # an answer the engine read its input for is served from then on
        calls = len(cli.calls)
        assert LlmVisionTranscriber(client()).transcribe(scan).text == a_page()
        assert len(cli.calls) == calls
        assert events[-1] == ("cache-hit", "key match")

    def test_a_kept_answer_is_rejected_not_reused(self, rig):
        """A kept answer the check fails is moved aside and the engine is
        asked in its place; when the engine's stream then reports a tool
        error, the document fails and nothing is kept."""
        cli, scan, cache, events, client = rig
        wrapped = client()
        key = wrapped.complete_key(TRANSCRIPTION_PROMPT, [scan])
        assert wrapped.seed(key, FAILURES[0][1], {"role": "transcribe"})
        cli.answer = a_page()
        cli.tool_events = read_events(scan.resolve(), error=TOO_MANY_PAGES)
        with pytest.raises(TranscriptionCheckFailed) as caught:
            LlmVisionTranscriber(wrapped).transcribe(scan)
        assert caught.value.check.tool_error == "too-many-pages"
        assert cache.get(key) is None
        assert len(cache.rejected(key)) == 1
        assert [event for event, _ in events] == [
            "cache-adopt",
            "cache-hit",
            "cache-reject",
            "cache-miss",
        ]
        assert events[-1] == ("cache-miss", "rejected")
        assert len(cli.calls) == 1

    def test_a_refused_request_made_up_for_is_kept(
        self, rig, tmp_path, monkeypatch
    ):
        """A PDF read first without a page range, then by ranges: the file
        was read, and the answer is kept and served like any other."""
        cli, _, cache, events, client = rig
        monkeypatch.setattr(transcribe, "media_page_count", lambda path: 34)
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF-1.4 placeholder")
        cli.answer = a_page()
        cli.tool_events = (
            read_events(scan.resolve(), error=TOO_MANY_PAGES)
            + read_events(scan.resolve(), pages="1-20")
            + read_events(scan.resolve(), pages="21-34")
        )
        assert LlmVisionTranscriber(client()).transcribe(scan).text == a_page()
        assert LlmVisionTranscriber(client()).transcribe(scan).text == a_page()
        assert len(cli.calls) == 1
        assert events == [("cache-miss", "absent"), ("cache-hit", "key match")]


RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-27T09:00:00Z",
}


class TestOrchestratorContract:
    """With the folder orchestrator, a failed check fails the document,
    records the reason, keeps no transcription, and the next run
    transcribes again. A refresh that fails is the exception: the view it
    was to replace stays, and the next run reuses it."""

    @staticmethod
    def orchestrator(tmp_path, monkeypatch, engine, state="state", **options):
        """A folder orchestrator over one scan, transcribing with `engine`
        and extracting nothing, with its state under `tmp_path / state` and
        any other `options` it takes: (the orchestrator, the scan)."""
        import yizkr.pipeline.llm as llm

        monkeypatch.setattr(llm, "make_client", lambda *a, **k: engine)

        class Orch(Orchestrator):
            def _step_register(self, doc):
                return "Q23"

            def _step_extract(self, doc, transcription):
                return ClaimBatch.model_validate(
                    {"extraction_run": RUN, "claims": [], "entities": []}
                )

            def _step_critique(self, doc, batch):
                return []

        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        (folder / "scan.png").write_bytes(b"not really a png")
        orch = Orch(
            folder, tmp_path / state, log=lambda *a, **k: None, **options
        )
        return orch, folder / "scan.png"

    def test_failed_check_is_not_kept_and_is_retried(
        self, tmp_path, monkeypatch
    ):
        engine = FakeLlm(FAILURES[0][1], a_page())
        orch, scan = self.orchestrator(tmp_path, monkeypatch, engine)
        folder = scan.parent

        doc = DocumentRun(folder / "scan.png", tmp_path / "state")
        assert orch.advance(doc, stop_after_extract=True) == FAILED
        assert "TranscriptionCheckFailed" in doc.state.error
        assert "pdf-renderer" in doc.state.error
        assert not doc.transcription.exists()
        assert not doc.batch.exists()

        doc = DocumentRun(folder / "scan.png", tmp_path / "state")
        assert orch.advance(doc, stop_after_extract=True) == BATCHED
        assert engine.calls == 2
        assert doc.transcription.exists()
        assert doc.state.error is None

    def test_a_tool_error_fails_the_document_like_a_failed_check(
        self, tmp_path, monkeypatch
    ):
        """The failure is the document's error and the run's failed event,
        as any other failed check's is (#213)."""
        engine = ScriptedLlm(
            tool_error("pdf-renderer-missing"), a_page(), kept=0
        )
        orch, scan = self.orchestrator(tmp_path, monkeypatch, engine)

        doc = DocumentRun(scan, tmp_path / "state")
        assert orch.advance(doc, stop_after_extract=True) == FAILED
        assert "TranscriptionCheckFailed" in doc.state.error
        assert "(tool-error)" in doc.state.error
        assert "Read (pdf-renderer-missing)" in doc.state.error
        assert not doc.transcription.exists()
        failed = [
            event
            for event in orch.rundir.events()
            if event["event"] == "failed"
        ]
        assert [event["stage"] for event in failed] == ["transcription"]
        assert "Read (pdf-renderer-missing)" in failed[0]["reason"]

        doc = DocumentRun(scan, tmp_path / "state")
        assert orch.advance(doc, stop_after_extract=True) == BATCHED
        assert engine.calls == 2
        assert doc.state.error is None

    def test_a_refresh_that_raises_replaces_nothing(
        self, tmp_path, monkeypatch
    ):
        """The exception the transcriber's and the engine's docstrings make
        to 'the next run asks again' (#213, #244). A transcription whose
        Read succeeded is refreshed, and the refreshed call's Read fails:
        the call raises and stores nothing, so the answer the cache kept and
        the view stay. The next run in the same state directory reuses the
        view, and one in a fresh state directory is served the kept answer;
        neither asks the engine."""
        cli = install_fake_cli(monkeypatch, tmp_path)
        engine = ClaudeCodeClient()
        cache = tmp_path / "cache"
        scan = (tmp_path / "docs" / "scan.png").resolve()

        def run(state, **options):
            orch, source = self.orchestrator(
                tmp_path,
                monkeypatch,
                engine,
                state=state,
                cache_dir=cache,
                **options,
            )
            doc = DocumentRun(source, tmp_path / state)
            # the state directory's event log keeps every earlier run's
            # events, so only those this run appends count
            seen = len(orch.rundir.events())
            status = orch.advance(doc, stop_after_extract=True)
            events = [
                (event["event"], event.get("reason") or event.get("stage"))
                for event in orch.rundir.events()[seen:]
            ]
            return status, doc, events

        cli.answer = a_page()
        cli.tool_events = read_events(scan)
        status, doc, _ = run("state")
        assert status == BATCHED
        kept = role_entries(cache, "transcribe")
        assert [entry["response"] for entry in kept.values()] == [a_page()]

        cli.tool_events = read_events(scan, error=NOT_AN_IMAGE)
        status, doc, _ = run("state", refresh=("transcribe",))
        assert status == FAILED
        assert "Read (corrupt)" in doc.state.error
        assert len(cli.calls) == 2
        assert doc.transcription.exists()
        assert role_entries(cache, "transcribe") == kept

        status, doc, events = run("state")
        assert status == BATCHED
        assert ("reused", "transcription") in events
        status, doc, events = run("fresh")
        assert status == BATCHED
        assert ("cache-hit", "key match") in events
        assert len(cli.calls) == 2

    def test_only_a_fresh_start_reaches_a_transcription_made_before(
        self, tmp_path, monkeypatch
    ):
        """The limit the transcription check's module docstring states, and
        its remedy (#213, #244). A transcription made before the engine
        read its stream's tool errors holds no record of the error its
        input meets, in its view or in the answer the cache kept. A refresh
        fails the document in its run but stores nothing, so both stand:
        the next run reuses the view. An epoch asks the engine only in its
        own runs, and a fresh state directory at epoch 0 is served the kept
        answer. Only a fresh state directory with the role's answers moved
        out of the cache asks the engine again, which fails."""
        import yizkr.pipeline.llm as llm

        cli = install_fake_cli(monkeypatch, tmp_path)
        engine = ClaudeCodeClient()
        cache = tmp_path / "cache"

        def run(state, **options):
            orch, scan = self.orchestrator(
                tmp_path,
                monkeypatch,
                engine,
                state=state,
                cache_dir=cache,
                **options,
            )
            doc = DocumentRun(scan, tmp_path / state)
            # the state directory's event log keeps every earlier run's
            # events, so only those this run appends count
            seen = len(orch.rundir.events())
            status = orch.advance(doc, stop_after_extract=True)
            events = [
                (event["event"], event.get("reason") or event.get("stage"))
                for event in orch.rundir.events()[seen:]
            ]
            return status, doc, events

        cli.answer = a_page()
        cli.tool_events = read_events(
            (tmp_path / "docs" / "scan.png").resolve(), error=NOT_AN_IMAGE
        )
        # made by an engine that did not read its stream's tool results
        with monkeypatch.context() as before:
            before.setattr(llm, "tool_failures", lambda *a, **k: [])
            status, doc, _ = run("state")
        assert status == BATCHED
        assert len(cli.calls) == 1

        status, doc, _ = run("state", refresh=("transcribe",))
        assert status == FAILED
        assert "Read (corrupt)" in doc.state.error
        assert len(cli.calls) == 2
        assert doc.transcription.exists()
        kept = role_entries(cache, "transcribe")
        assert [entry["response"] for entry in kept.values()] == [a_page()]

        # the next run reuses the view, as the check's signatures pass it
        status, doc, events = run("state")
        assert status == BATCHED
        assert ("reused", "transcription") in events
        assert len(cli.calls) == 2
        # an epoch asks the engine only in the runs it is given for
        status, doc, _ = run("epoch", epochs={"transcribe": 1})
        assert status == FAILED
        assert len(cli.calls) == 3
        # and a fresh state directory is served the kept answer
        status, doc, events = run("fresh")
        assert status == BATCHED
        assert ("cache-hit", "key match") in events
        assert len(cli.calls) == 3

        aside = tmp_path / "aside"
        for path in kept:
            target = aside / path.relative_to(cache)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)
        status, doc, events = run("fresher")
        assert status == FAILED
        assert "Read (corrupt)" in doc.state.error
        assert ("cache-miss", "absent") in events
        assert len(cli.calls) == 4
        assert not role_entries(cache, "transcribe")


# what the Read tool says of a file named as an image that holds none, as
# CLI 2.1.237 words it
NOT_AN_IMAGE = (
    "File has an image extension but its content is not a valid "
    "PNG/JPEG/GIF/WebP. Detected: unknown."
)


def role_entries(cache, role: str) -> dict:
    """The entries of the LLM cache under `cache` whose calls `role` made,
    by path, with their contents: what is moved out of the cache so that
    the role's calls ask the engine again. Entries moved aside (rejected
    or replaced) are not served, and are left where they are."""
    entries = {}
    for path in sorted(cache.glob("??/*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            continue
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("provenance", {}).get("role") == role:
            entries[path] = entry
    return entries
