"""The transcription check (#175): tool-failure and refusal outputs never
pass as transcriptions, a near-empty multi-page output fails, and short
genuine pages pass.

Every failure and refusal output below is invented engine text, and every
genuine text is either a source passage of the fictional universe
(docs/EXAMPLES.md) or a generic line with no names in it.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ClaimBatch
from yizkr.pipeline import transcribe, transcription_check
from yizkr.pipeline.orchestrate import (
    BATCHED,
    FAILED,
    DocumentRun,
    Orchestrator,
)
from yizkr.pipeline.transcribe import (
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

    def test_failure_partway_through_fails_the_whole_output(self):
        text = f"{a_page()}\n\n{PAGE_FAILURE}\n"
        check = check_transcription(text, pages=2)
        assert not check.passed
        assert check.signature == "media-unreadable"

    @pytest.mark.parametrize("text", universe_texts())
    def test_universe_source_texts_pass(self, text):
        for pages in (None, 1):
            assert check_transcription(text, pages=pages).passed

    def test_letter_with_refusal_words_passes(self):
        assert check_transcription(LETTER, pages=1).passed

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
        assert text_char_count("Som[?]mer [illegible] 1929") == len(
            "Sommer1929"
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
        assert check_transcription("Sommer 1929", pages=2).passed

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
        check = check_transcription("Sommer 1929")
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
        assert require_valid_transcription("Sommer 1929").passed

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
        assert media_page_count(tmp_path / "memoir.txt") is None

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

    def test_vision_pdf_fails_before_the_engine_is_called(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(transcribe.shutil, "which", no_tools)
        llm = FakeLlm("never used")
        with pytest.raises(MissingPrerequisite):
            LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.pdf")
        assert llm.calls == 0

    def test_images_need_no_renderer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe.shutil, "which", no_tools)
        llm = FakeLlm("Sommer 1929")
        result = LlmVisionTranscriber(llm).transcribe(tmp_path / "scan.png")
        assert result.text == "Sommer 1929"


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


RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-27T09:00:00Z",
}


class TestOrchestratorContract:
    """With the folder orchestrator, a failed check fails the document,
    records the reason, keeps no transcription, and the next run
    transcribes again."""

    def test_failed_check_is_not_kept_and_is_retried(
        self, tmp_path, monkeypatch
    ):
        import yizkr.pipeline.llm as llm

        engine = FakeLlm(FAILURES[0][1], a_page())
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
        folder.mkdir()
        (folder / "scan.png").write_bytes(b"not really a png")
        orch = Orch(folder, tmp_path / "state", log=lambda *a, **k: None)

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
