"""Transcription check: is an engine's output a reading of its pages?

A vision engine answers in text whatever happens, so a tool failure (the
PDF renderer missing on the host, a CLI error, a usage-limit notice) or a
refusal comes back shaped like a transcription. `Transcription` only asks
for a non-empty string, and language detection reads such text as
confidently as any other. This step runs after transcription and decides
whether the output may go downstream. It fails the output on either of:

* **A signature**: a phrase in the engine's own voice that no source
  document writes. Tool-specific tokens (``pdftoppm``, a Python
  traceback, an API error type) match anywhere. Phrases such as "I was
  unable to read the PDF" or "I cannot transcribe" match outside
  editorial brackets, because the transcription prompt asks the engine
  to mark what it cannot read in brackets (``[illegible]``), and a
  bracketed note is that convention at work. Any match fails the whole
  output: a multi-page reading that failed on one page is missing that
  page. Two signatures could also match an ordinary sentence, so they
  apply only to an output that is little else: a bare decline ("I can't
  help with that", "I'm unable to complete this task") opening a short
  output, and a lone sentence announcing that the engine is about to
  read the file.
* **Implausible length**: a document of two or more pages must hold at
  least `MIN_TEXT_CHARS_PER_PAGE` letters or digits per page, counted
  outside editorial brackets. The floor sits far below any written page
  (a typed page holds well over a thousand) so that sparse genuine
  pages, such as a caption on the back of a photograph, a date stamp or a
  blank reverse, pass, and only near-empty output fails. A single image
  has no floor, because a photograph may carry no text at all; the
  signatures cover a single page's failures. Judging yield against what
  a document of its kind should hold is a softer question, for alerts
  rather than this gate.

A failed output is never valid: `require_valid_transcription` and the
transcribers raise `TranscriptionCheckFailed`, whose message is the
recorded reason, and nothing is returned for a caller to keep.

This module is the single definition of a failed transcription: code that
judges a transcription, whether freshly made, stored or imported, calls
`check_transcription` rather than keeping patterns of its own. The step
has its own name and version
(`CHECK_STAGE`, `CHECK_VERSION`), apart from the transcription's: a
change to a signature, the floor or the page count bumps `CHECK_VERSION`,
so stored transcriptions are checked again rather than transcribed again.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

CHECK_STAGE = "transcription-check"
CHECK_VERSION = 1

# The fewest letters or digits per page a transcription of two or more
# pages may hold; see the module docstring for why it is this low.
MIN_TEXT_CHARS_PER_PAGE = 3

# A signature that could also open an ordinary sentence applies only to
# an output this short (characters, outside editorial brackets).
SHORT_OUTPUT_CHARS = 300

# An editorial mark: a bracketed note on one line, such as [illegible],
# [?], [stamp] or [page 2: faded]. Longer bracketed runs are left alone.
EDITORIAL_MARK = re.compile(r"\[[^\[\]\n]{0,200}\]")

# the longest matched phrase a verdict records
_QUOTE_CHARS = 80

TOOL_FAILURE = "tool-failure"
REFUSAL = "refusal"
TOO_SHORT = "too-short"
EMPTY = "empty"

Failure = Literal["tool-failure", "refusal", "too-short", "empty"]

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})


@dataclass(frozen=True)
class Signature:
    """One pattern that marks an output as something other than a
    transcription."""

    name: str
    kind: Literal["tool-failure", "refusal"]
    pattern: re.Pattern[str]
    # match against the text with editorial marks removed
    outside_marks: bool = True
    # apply only to outputs of at most SHORT_OUTPUT_CHARS characters
    short_output_only: bool = False


def _signature(
    name: str,
    kind: Literal["tool-failure", "refusal"],
    pattern: str,
    *,
    outside_marks: bool = True,
    short_output_only: bool = False,
) -> Signature:
    return Signature(
        name,
        kind,
        re.compile(pattern, re.IGNORECASE),
        outside_marks=outside_marks,
        short_output_only=short_output_only,
    )


# a straight or typographic apostrophe
_A = "['’]"
_NEGATED_MODAL = (
    rf"(?:can{_A}t|cannot|can not|could not|couldn{_A}t|won{_A}t|will not)"
)
_UNABLE = rf"(?:unable|not able|wasn{_A}t able|failed)\s+to"
_DECLINE = rf"(?:{_NEGATED_MODAL}|{_UNABLE})"
_MEDIA = r"(?:PDFs?|images?|image files?|attachments?|scans?)"

SIGNATURES: tuple[Signature, ...] = (
    # -- tool and engine failures: tokens no source document writes ------
    # Poppler is also a surname, so the bare word counts as the package
    # only in lowercase after "install" or "from" ("brew install
    # poppler"); "a letter from Poppler", or the name in a register
    # written in capitals, is a source's words.
    _signature(
        "pdf-renderer",
        TOOL_FAILURE,
        r"\bpdftoppm\b"
        r"|\bpoppler[-_ ]utils\b"
        r"|(?-i:\b(?:install(?:ed|ing)?|from)\s+poppler\b)"
        r"|\bPDF render(?:er|ing)\b",
        outside_marks=False,
    ),
    _signature(
        "traceback",
        TOOL_FAILURE,
        r"\bTraceback \(most recent call last\)",
        outside_marks=False,
    ),
    _signature(
        "shell-error",
        TOOL_FAILURE,
        r"\bcommand not found\b|\bNo such file or directory\b|\bENOENT\b",
        outside_marks=False,
    ),
    _signature(
        "engine-error",
        TOOL_FAILURE,
        r"\bAPI Error\b"
        r"|\b(?:api|authentication|invalid_request|not_found|overloaded"
        r"|permission|rate_limit)_error\b"
        r"|\b(?:usage|rate|session|weekly|\d+-hour) limit"
        r" (?:reached|exceeded)\b"
        r"|\bCredit balance is too low\b"
        r"|\bPrompt is too long\b"
        r"|\b(?:Reached max|maximum number of) turns\b",
        outside_marks=False,
    ),
    # -- the engine could not see the media ------------------------------
    _signature(
        "media-unreadable",
        TOOL_FAILURE,
        rf"\b{_DECLINE}\s+"
        r"(?:read|open|render|convert|load|process|view|access|see|display)"
        rf"\b[^.\n]{{0,40}}?\b{_MEDIA}\b"
        rf"|\b{_MEDIA}\s+(?:{_NEGATED_MODAL}|failed to)\s+be\s+"
        r"(?:read|opened|rendered|converted|loaded|processed|displayed)\b"
        rf"|\b{_MEDIA}\s+(?:appears|seems)\s+to\s+be\s+"
        r"(?:corrupt(?:ed)?|damaged|encrypted|password-protected"
        r"|unreadable)\b",
    ),
    _signature(
        "media-missing",
        TOOL_FAILURE,
        rf"\b(?:don{_A}t|do not|{_NEGATED_MODAL})\s+see\s+(?:any|an)\s+"
        rf"{_MEDIA}\b"
        rf"|\bno\s+{_MEDIA}\s+(?:was|were|has been|have been)\s+"
        r"(?:provided|attached|shared|uploaded|included)\b",
    ),
    # the whole output is one sentence announcing that the engine is about
    # to read the media: it stopped before transcribing anything. (Such a
    # sentence followed by text is an ordinary preamble and passes.)
    _signature(
        "narration-only",
        TOOL_FAILURE,
        rf"\A\W*(?:I{_A}ll|I will|Let me|I{_A}m going to)\s+"
        r"(?:start by\s+|first\s+)?"
        r"(?:read|open|examin|look at|view)(?:e|ing)?\s+"
        r"(?:the|these|this|those)\s+"
        r"(?:(?:image|PDF)\s+)?(?:images?|PDFs?|files?|documents?|scans?)\b"
        r"[^.!?\n]*[.!?:]?\s*\Z",
    ),
    # -- refusals: the engine declines, in its own voice ------------------
    _signature(
        "refusal-transcribe",
        REFUSAL,
        rf"\b{_DECLINE}\s+"
        r"(?:transcribe|provide (?:a|the|any) transcription"
        r"|(?:help|assist)(?: you)? with transcrib\w*)\b",
    ),
    # declining the request itself; declining a "task", or to "complete"
    # or "process" something, is also a letter's ordinary prose ("I could
    # not complete this task before the winter"), so those words count
    # only as a bare decline (refusal-short)
    _signature(
        "refusal-request",
        REFUSAL,
        rf"\b{_DECLINE}\s+"
        r"(?:(?:help|assist)(?: you)? with|comply with|fulfil|fulfill)"
        r"\s+(?:this|that|your)\s+request\b",
    ),
    _signature(
        "ai-disclaimer",
        REFUSAL,
        r"\bas an AI\b|\bAI (?:language )?model\b",
    ),
    # a bare decline, apology or not, opening a short output; opening a
    # longer text, the same words may belong to a letter
    _signature(
        "refusal-short",
        REFUSAL,
        rf"\A\W*(?:(?:I{_A}m|I am)\s+(?:sorry|afraid)|I apologi[sz]e|Sorry"
        r"|Unfortunately)?[,.!]?\s*(?:but\s+)?"
        rf"I(?:{_A}m|\s+am)?\s+{_DECLINE}\s+"
        r"(?:(?:help|assist)(?: you)? with|do|comply with|provide"
        r"|complete|process)"
        r"\s+(?:that|this)\b",
        short_output_only=True,
    ),
)


class TranscriptionCheck(BaseModel):
    """The check step's verdict on one transcription."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str = CHECK_STAGE
    version: int = CHECK_VERSION
    passed: bool
    failure: Failure | None = None
    signature: str | None = None
    # the matched phrase (at most 80 characters), quoted in the reason
    matched: str | None = None
    # letters and digits outside editorial marks
    text_chars: int
    pages: int | None = None
    min_text_chars: int = 0
    reason: str | None = None


class TranscriptionCheckFailed(RuntimeError):
    """A transcription failed the check; the message is the reason."""

    def __init__(self, check: TranscriptionCheck) -> None:
        self.check = check
        super().__init__(
            f"transcription check v{check.version} failed "
            f"({check.failure}): {check.reason}"
        )


def outside_editorial_marks(text: str) -> str:
    """`text` with each editorial mark replaced by a space."""
    return EDITORIAL_MARK.sub(" ", text)


def text_char_count(text: str) -> int:
    """Letters and digits in `text`, outside editorial marks."""
    return sum(ch.isalnum() for ch in outside_editorial_marks(text))


def minimum_text_chars(pages: int | None) -> int:
    """The fewest letters or digits a transcription of `pages` pages may
    hold: none for a single page or an unknown count."""
    if pages is None or pages < 2:
        return 0
    return MIN_TEXT_CHARS_PER_PAGE * pages


def find_signature(text: str) -> tuple[Signature, str] | None:
    """The first signature `text` matches, with the matched phrase."""
    unmarked = outside_editorial_marks(text).strip()
    for signature in SIGNATURES:
        if signature.short_output_only and len(unmarked) > SHORT_OUTPUT_CHARS:
            continue
        subject = unmarked if signature.outside_marks else text
        match = signature.pattern.search(subject)
        if match:
            return signature, " ".join(match.group(0).split())
    return None


def _cap(phrase: str) -> str:
    if len(phrase) > _QUOTE_CHARS:
        return phrase[: _QUOTE_CHARS - 1] + "\u2026"
    return phrase


def check_transcription(
    text: str, pages: int | None = None
) -> TranscriptionCheck:
    """Judge one transcription of a document of `pages` pages.

    `pages` is None when the page count is unknown; the length floor
    then does not apply, and the signatures still do.
    """
    chars = text_char_count(text)
    floor = minimum_text_chars(pages)
    verdict = {"text_chars": chars, "pages": pages, "min_text_chars": floor}
    if not text.strip():
        return TranscriptionCheck(
            passed=False,
            failure=EMPTY,
            reason="the output is blank",
            **verdict,
        )
    found = find_signature(text)
    if found is not None:
        signature, phrase = found
        phrase = _cap(phrase)
        what = (
            "a tool or engine failure"
            if signature.kind == TOOL_FAILURE
            else "a refusal"
        )
        return TranscriptionCheck(
            passed=False,
            failure=signature.kind,
            signature=signature.name,
            matched=phrase,
            reason=(
                f"the output reads as {what}, not a transcription "
                f"(signature {signature.name}: {phrase!r})"
            ),
            **verdict,
        )
    if chars < floor:
        return TranscriptionCheck(
            passed=False,
            failure=TOO_SHORT,
            reason=(
                f"implausibly short: {chars} letters or digits for "
                f"{pages} pages, below the floor of {floor} "
                f"({MIN_TEXT_CHARS_PER_PAGE} per page)"
            ),
            **verdict,
        )
    return TranscriptionCheck(passed=True, **verdict)


def require_valid_transcription(
    text: str, pages: int | None = None
) -> TranscriptionCheck:
    """`check_transcription`, raising `TranscriptionCheckFailed` on a
    failed verdict."""
    check = check_transcription(text, pages)
    if not check.passed:
        raise TranscriptionCheckFailed(check)
    return check


def media_page_count(media_path: Path) -> int | None:
    """Pages in an artifact's media, for the length floor.

    An image is one page. A PDF's count comes from ``pdfinfo``
    (poppler-utils, the package that also provides the PDF renderer the
    vision engine uses). None when the count is unknown: another kind of
    file, or a PDF that pdfinfo cannot read or is not installed to read.
    """
    suffix = media_path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return 1
    if suffix != ".pdf":
        return None
    try:
        result = subprocess.run(
            ["pdfinfo", str(media_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None
