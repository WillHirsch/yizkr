"""Transcription check: is an engine's output a reading of its pages?

A vision engine answers in text whatever happens, so a tool failure (the
PDF renderer missing on the host, a CLI error, a usage-limit notice) or a
refusal comes back shaped like a transcription. `Transcription` only asks
for a non-empty string, and language detection reads such text as
confidently as any other. This step runs after transcription and decides
whether the output may go downstream. It fails the output on either of:

* **A signature**: a phrase in the engine's own voice that no source
  document writes. Tool-specific tokens (``pdftoppm``, a Python
  traceback, an API error type) match anywhere. Phrases such as "I
  cannot transcribe" or "As an AI" match outside editorial brackets,
  because the transcription prompt asks the engine to mark what it
  cannot read in brackets (``[illegible]``), and a bracketed note is
  that convention at work. Any match fails the whole output: a
  multi-page reading that failed on one page is missing that page.

  Some engine phrases are also the words of a letter or an email, such
  as correspondence about family research, so those signatures count
  only where the engine speaks for itself. A note about the media or the
  request ("I couldn't open the PDF", "No image was provided", "I'm
  unable to assist with this request") counts where it opens the output
  or a page section, or where it names the pages it skipped ("I was
  unable to read page 2 of the PDF"), and only while it names the task's
  medium or request plainly. Correspondence names its own by what it
  holds ("the PDF of the census page", "your request for copies of the
  register"), who sent it ("the attachment you sent"), what it came in
  ("in the envelope") or where it was opened ("on my computer"); such a
  note is the source's own sentence, as is one in the middle of a text
  that names no page. A bare decline ("I can't help with that", "I'm
  unable to complete this task") counts only opening a short output, and
  a sentence announcing that the engine is about to read the file only
  as the whole output.
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
    # count a match only where the engine speaks about its own task (see
    # in_engine_voice)
    engine_voice: bool = False


def _signature(
    name: str,
    kind: Literal["tool-failure", "refusal"],
    pattern: str,
    *,
    outside_marks: bool = True,
    short_output_only: bool = False,
    engine_voice: bool = False,
) -> Signature:
    return Signature(
        name,
        kind,
        re.compile(pattern, re.IGNORECASE),
        outside_marks=outside_marks,
        short_output_only=short_output_only,
        engine_voice=engine_voice,
    )


# a straight or typographic apostrophe
_A = "['’]"
_NEGATED_MODAL = (
    rf"(?:can{_A}t|cannot|can not|could not|couldn{_A}t|won{_A}t|will not)"
)
_UNABLE = rf"(?:unable|not able|wasn{_A}t able|failed)\s+to"
_DECLINE = rf"(?:{_NEGATED_MODAL}|{_UNABLE})"
# the engine as the subject of its note: "I", "I'm", "I was"
_I = rf"(?:I(?:{_A}m|{_A}ve been|\s+am|\s+was|\s+have been)?\s+)"
_MEDIA = r"(?:PDFs?|images?|image files?|attachments?|scans?)"
# the words between a verb and its medium: within one sentence, and short
# of any other medium, so that the first medium named is the object
_GAP = rf"(?:(?!\b{_MEDIA}\b)[^.\n]){{0,40}}?"
# what may stand before a medium that is a sentence's subject ("the
# attached PDF")
_THE = (
    r"(?:(?:the|this|these|those|that|your)\s+)?"
    r"(?:(?:attached|uploaded|provided|shared)\s+)?"
)
# the pages a note says were skipped, named by number or place: "page 2",
# "pages 3-4", "the second page", "the remaining pages", "the rest of the
# PDF"
_PAGE = (
    r"\b(?:pages?\s+\d+"
    r"|(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth"
    r"|tenth|last|final|remaining|other|later|following|subsequent)"
    r"\s+pages?"
    r"|rest\s+of\s+the\s+(?:PDF|document|file|pages))\b"
)
# a sentence announcing that the engine is about to read the media
_NARRATION = (
    rf"(?:I{_A}ll|I will|Let me|I{_A}m going to)\s+"
    r"(?:start by\s+|first\s+)?"
    r"(?:read|open|examin|look at|view)(?:e|ing)?\s+"
    r"(?:the|these|this|those)\s+"
    r"(?:(?:image|PDF)\s+)?(?:images?|PDFs?|files?|documents?|scans?)\b"
    r"[^.!?\n]*[.!?:]?"
)

# -- the engine's own voice ---------------------------------------------
# A note about the media or the request is also a letter's or an email's
# sentence ("I couldn't open the attachment you sent"). The engine's note
# opens its output or a page section, or names the page it skipped, and
# names the task's medium and request plainly; correspondence names its
# own.

# what may precede an engine's note that opens the output or a page
# section: a sentence announcing the read (the claude-code engine joins
# its turns without a separator), a label ("Error:"), an apology, a hedge
_LEAD_IN = (
    rf"(?:{_NARRATION}\W*)?"
    r"(?:(?:note|error|warning|transcription)\s*:\W*)?"
    rf"(?:(?:(?:I{_A}m|I am)\s+(?:sorry|afraid)|I apologi[sz]e|Apologies"
    r"|Sorry|Unfortunately)\W*(?:but\s+)?)?"
    r"(?:it\s+(?:seems|looks|appears)(?:\s+like|\s+that)?\s+)?"
)
# a page heading opening a line: "Page 2", "--- Page 2 ---", "Page 2 of 3:"
_PAGE_HEADING = r"(?:\A|\n)[^\w\n]*page\s+\d+(?:\s+of\s+\d+)?[^\w\n]*"
# the text before a note that opens the output or a page section
_OPENING = re.compile(rf"(?:\A|{_PAGE_HEADING})\W*{_LEAD_IN}\Z", re.IGNORECASE)
# how far back from a note its opening is looked for (characters); a page
# heading and a lead-in are far shorter
_OPENING_REACH = 400
_PAGE_NAMED = re.compile(_PAGE, re.IGNORECASE)
# pages named just after the note: "the PDF's second page", "the image of
# page 3", "the PDF beyond page 1"
_PAGE_AFTER = re.compile(
    rf"(?:{_A}s\s+|\s+(?:of|for|beyond|after|past|from)\s+(?:the\s+)?)"
    rf"{_PAGE}",
    re.IGNORECASE,
)
# the words after a note that make its medium or request the
# correspondents' own: what it holds, who sent it, what it came in, or the
# device it was opened on
_THEIRS = re.compile(
    # what it holds: "the PDF of the census page"; the prompt's "image of
    # a historical document", and a page, are the task's
    r"\s+of\s+(?!(?:(?:a|an|the|this|that)\s+)?"
    rf"(?:(?:historical\s+)?(?:document|PDF|file|image|scan)s?\b|{_PAGE}))"
    # "your request for copies of the register"; a request for a
    # transcription is the task
    r"|(?<=request)\s+(?:for|regarding|concerning|about|dated)\s+"
    r"(?!(?:(?:a|an|the)\s+)?transcri)"
    # who sent it: "the attachment you sent"
    r"|\s+(?:that\s+|which\s+)?(?:you|he|she|they|we)"
    rf"(?:{_A}ve|\s+have)?\s+(?:\w+ly\s+)?"
    r"(?:sent|posted|mailed|e-?mailed|forwarded|enclosed)\b"
    # what it came in: "in the envelope", "attached to your email", "on
    # your last message"; the engine's "your message" is the task's
    r"|\s+(?:(?:attached|included|enclosed|sent|posted)\s+)?"
    r"(?:in|on|with|from|inside|to|by|via)\s+"
    rf"(?:(?:the|your|my|our|his|her|their|this|that|\w+{_A}s)\s+)?"
    r"(?:(?:(?:last|previous|earlier|latest|recent|other)\s+)?"
    r"(?:envelope|parcel|package|packet|post|letter|card|e-?mail|mail"
    r"|reply)s?"
    r"|(?:last|previous|earlier|latest|recent|other)\s+messages?)\b"
    # the device it was opened on: "on my computer"
    r"|\s+(?:on|with|in|using|from)\s+(?:my|our)\s+(?:\w+\s+)?"
    r"(?:computer|laptop|phone|tablet|machine|PC|Mac|screen|printer"
    r"|scanner|device|desktop)s?\b",
    re.IGNORECASE,
)


def in_engine_voice(subject: str, match: re.Match[str]) -> bool:
    """Whether `match`, a note about the media or the request found in
    `subject`, is the engine speaking about its own task.

    It is when the note opens the output or a page section, after at most
    a sentence announcing the read, a label, an apology and a hedge, or
    when it names the pages it skipped; and never when the words after it
    make its medium or request the correspondents' own.
    """
    if _THEIRS.match(subject, match.end()):
        return False
    start = match.start()
    if _OPENING.search(subject, max(0, start - _OPENING_REACH), start):
        return True
    return bool(
        _PAGE_NAMED.search(match.group(0))
        or _PAGE_AFTER.match(subject, match.end())
    )


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
    # engine-voice notes (in_engine_voice): letters and emails say the same
    # of their own attachments
    _signature(
        "media-unreadable",
        TOOL_FAILURE,
        # "I couldn't open the attached image", "I was unable to read page
        # 2 of the PDF"
        rf"\b{_I}?{_DECLINE}\s+"
        r"(?:read|open|render|convert|load|process|view|access|see|display)"
        rf"\b{_GAP}\b{_MEDIA}\b"
        # "The PDF could not be rendered", "Page 2 of the PDF could not be
        # read"
        rf"|(?:{_PAGE}\s+of\s+)?\b{_THE}{_MEDIA}\s+"
        rf"(?:{_NEGATED_MODAL}|failed to)\s+be\s+"
        r"(?:read|opened|rendered|converted|loaded|processed|displayed)\b"
        # "The image file appears to be corrupted"
        rf"|\b{_THE}{_MEDIA}\s+(?:appears|seems)\s+to\s+be\s+"
        r"(?:corrupt(?:ed)?|damaged|encrypted|password-protected"
        r"|unreadable)\b",
        engine_voice=True,
    ),
    _signature(
        "media-missing",
        TOOL_FAILURE,
        rf"\b{_I}?(?:don{_A}t|do not|{_NEGATED_MODAL})\s+see\s+(?:any|an)\s+"
        rf"{_MEDIA}\b"
        rf"|\bno\s+{_MEDIA}\s+(?:was|were|has been|have been)\s+"
        r"(?:provided|attached|shared|uploaded|included)\b",
        engine_voice=True,
    ),
    # the whole output is one sentence announcing that the engine is about
    # to read the media: it stopped before transcribing anything. (Such a
    # sentence followed by text is an ordinary preamble and passes.)
    _signature(
        "narration-only",
        TOOL_FAILURE,
        rf"\A\W*{_NARRATION}\s*\Z",
    ),
    # -- refusals: the engine declines, in its own voice ------------------
    _signature(
        "refusal-transcribe",
        REFUSAL,
        rf"\b{_DECLINE}\s+"
        r"(?:transcribe|provide (?:a|the|any) transcription"
        r"|(?:help|assist)(?: you)? with transcrib\w*)\b",
    ),
    # declining the request itself, as an engine-voice note
    # (in_engine_voice): a reply letter declines its reader's request in
    # the same words. Declining a "task", or to "complete" or "process"
    # something, is also a letter's ordinary prose ("I could not complete
    # this task before the winter"), so those words count only as a bare
    # decline (refusal-short)
    _signature(
        "refusal-request",
        REFUSAL,
        rf"\b{_I}?{_DECLINE}\s+"
        r"(?:(?:help|assist)(?: you)? with|comply with|fulfil|fulfill)"
        r"\s+(?:this|that|your)\s+request\b",
        engine_voice=True,
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
    """The first signature `text` matches, with the matched phrase. An
    engine-voice signature matches only where `in_engine_voice` holds."""
    unmarked = outside_editorial_marks(text).strip()
    for signature in SIGNATURES:
        if signature.short_output_only and len(unmarked) > SHORT_OUTPUT_CHARS:
            continue
        subject = unmarked if signature.outside_marks else text
        for match in signature.pattern.finditer(subject):
            if not signature.engine_voice or in_engine_voice(subject, match):
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
