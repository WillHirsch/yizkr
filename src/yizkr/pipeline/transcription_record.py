"""The transcription record: what the vision engine fills for a document
(ADR-0037), and what code reads instead of the transcription's wording.

The engine answers the transcription prompt with one JSON object, the
record:

* **per document**, its `document_class`: one of the manifest's artifact
  classes (the items that are a `subclass_of` artifact, such as
  `photograph`, `certificate` or `letter`), or `unclassified` when none
  fits;
* **per page**, in the media's order: `page`, its place in the media from
  1 (the physical index); `printed_page`, the page number printed or
  written on it, exactly as written, or null; its `status` (`transcribed`,
  `blank`, `illegible` or `not-a-document-page`); its `text`, exactly as
  written, with the editorial marks (`EDITORIAL_MARKS`: `[illegible]`,
  `[?]`, and `[redacted]` where text is blacked out or covered); the
  `languages` its text is written in, the main one first; and `notes`, the
  engine's own words about the page, which are never part of its text.

`parse_record` reads an answer as the record, and raises `RecordError`
when it is not one: not a JSON object, a field the record does not have,
a page without a status, a status or a class outside the record's
vocabulary, page numbers that are not 1, 2, 3... in order, or a page the
record calls transcribed that holds no text (`page` is a JSON integer, so
a string or a boolean fails). The transcription check fails such an
answer (yizkr.pipeline.transcription_check): whether an answer is the
record is a question of its structure, never its words.

A few harmless near-misses of the record's shape are normalised rather
than refused, since they say the same: a `printed_page` given as a JSON
number is its string ("12"); a `text`, `notes` or `languages` given as
null is empty; and a language code is lower-cased ("DE" is "de", "de-DE"
is "de-de", since a record's language code, as a claim's, may carry a
region subtag).

A record's text (`record_text`) is its pages' texts in order, each opened
by its `[Page N]` marker, N the physical index, so that every claim read
from it can carry its page. A page with nothing to read is its marker
alone. The text the extraction prompts carry (`prompt_text`) shows each
page's printed number, where its record gives one, beside its marker on
the marker's own line, and is otherwise the text itself.

Language detection and the anomaly checks read a text outside its
editorial marks (`outside_editorial_marks`): `EDITORIAL_MARK` is any
bracketed note on one line, the page markers and the record's marks
included, so that a line such as "Liebe Klara, [illegible] [?] Dein
Vater" is read as the German it is.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# the editorial marks the record's text uses, and the prompt asks for
ILLEGIBLE = "[illegible]"
UNCERTAIN = "[?]"
REDACTED = "[redacted]"
EDITORIAL_MARKS = (ILLEGIBLE, UNCERTAIN, REDACTED)

# An editorial mark as the checks read one: a bracketed note on one line,
# such as [illegible], [?], [redacted], [stamp] or [Page 2]. Longer
# bracketed runs are left alone.
EDITORIAL_MARK = re.compile(r"\[[^\[\]\n]{0,200}\]")

# a UTF-16 surrogate standing alone, which no UTF-8 text can hold
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

TRANSCRIBED = "transcribed"
BLANK = "blank"
ILLEGIBLE_PAGE = "illegible"
NOT_A_DOCUMENT_PAGE = "not-a-document-page"
PageStatus = Literal[
    "transcribed", "blank", "illegible", "not-a-document-page"
]
PAGE_STATUSES: tuple[str, ...] = (
    TRANSCRIBED,
    BLANK,
    ILLEGIBLE_PAGE,
    NOT_A_DOCUMENT_PAGE,
)

# the class of a document the record places in none of the manifest's
# artifact classes, and of a text that has no record
UNCLASSIFIED = "unclassified"

# a language code, as claims.MonolingualValue accepts one
LanguageCode = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[a-z0-9-]+)?$")]


def outside_editorial_marks(text: str) -> str:
    """`text` with each editorial mark replaced by a space."""
    return EDITORIAL_MARK.sub(" ", text)


def letters(text: str) -> int:
    """Letters and digits in `text`, outside editorial marks."""
    return sum(ch.isalnum() for ch in outside_editorial_marks(text))


@lru_cache(maxsize=1)
def artifact_classes() -> tuple[str, ...]:
    """The manifest's artifact classes, in its order: the items that are a
    `subclass_of` artifact."""
    from yizkr.model.schema import load_manifest

    return tuple(
        key
        for key, item in load_manifest().items.items()
        if item.subclass_of == "artifact"
    )


def document_classes() -> tuple[str, ...]:
    """The classes a record may name: the artifact classes, and
    UNCLASSIFIED."""
    return (*artifact_classes(), UNCLASSIFIED)


class RecordError(ValueError):
    """An answer that is not the transcription record; the message says
    how, never what the answer holds."""


class RecordPage(BaseModel):
    """One page of the record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # a JSON integer: a page number given as a string or a boolean fails
    page: int = Field(ge=1, strict=True)
    printed_page: str | None = None
    status: PageStatus
    text: str = ""
    languages: list[LanguageCode] = Field(default_factory=list)
    notes: str = ""

    @field_validator("printed_page", mode="before")
    @classmethod
    def _a_number_as_written(cls, value: object) -> object:
        """A page number given as a JSON number is its string ("12" for 12
        or 12.0); one that is not finite (NaN, Infinity) is no number
        printed on a page."""
        if isinstance(value, int | float) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError("the printed page number is not finite")
            return str(int(value)) if float(value).is_integer() else str(value)
        if isinstance(value, str) and _LONE_SURROGATE.search(value):
            # a transcription's text refuses one, so its page number does
            raise ValueError("the printed page number holds a lone surrogate")
        return value

    @field_validator("text", "notes", mode="before")
    @classmethod
    def _null_text_is_empty(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("languages", mode="before")
    @classmethod
    def _codes_in_lower_case(cls, value: object) -> object:
        """Languages given as null are none, and each code is lower-cased."""
        if value is None:
            return []
        if isinstance(value, list):
            return [
                code.lower() if isinstance(code, str) else code
                for code in value
            ]
        return value

    @model_validator(mode="after")
    def _transcribed_holds_text(self) -> RecordPage:
        if self.status == TRANSCRIBED and not self.text.strip():
            raise ValueError(
                f"page {self.page} is transcribed but holds no text"
            )
        return self


class TranscriptionRecord(BaseModel):
    """The record of one document: its class, and its pages in order,
    numbered from 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_class: str
    pages: list[RecordPage] = Field(min_length=1)

    @model_validator(mode="after")
    def _in_the_vocabulary(self) -> TranscriptionRecord:
        if self.document_class not in document_classes():
            raise ValueError(
                f"the document class {self.document_class!r} is none of "
                f"the record's classes ({', '.join(document_classes())})"
            )
        numbers = [page.page for page in self.pages]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("the pages are not numbered 1, 2, 3... in order")
        return self

    def statuses(self) -> list[str]:
        return [page.status for page in self.pages]


# A record alone in a code block; the prompt's example uses a text fence.
_FENCED = re.compile(
    r"\A```(?:json|text)?[^\S\n]*\n(?P<body>.*)\n```\Z", re.DOTALL
)


def parse_record(answer: str) -> TranscriptionRecord:
    """The record `answer` holds: the whole answer is one JSON object (or
    one fenced code block holding it). Raises RecordError otherwise."""
    body = answer.strip()
    fenced = _FENCED.match(body)
    if fenced is not None:
        body = fenced.group("body").strip()
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise RecordError(
            "the answer is not the transcription record: it is not one "
            f"JSON object ({type(exc).__name__})"
        ) from None
    if not isinstance(data, dict):
        raise RecordError(
            "the answer is not the transcription record: it is not a JSON "
            "object"
        )
    try:
        return TranscriptionRecord.model_validate(data)
    except ValidationError as exc:
        raise RecordError(
            "the answer is not the transcription record: "
            + "; ".join(_why(error) for error in exc.errors())
        ) from None


def _why(error: dict) -> str:
    """One validation error, by where it is and what it says, without the
    value it rejected."""
    where = ".".join(str(part) for part in error.get("loc", ()))
    return f"{where or 'record'}: {error.get('msg', 'invalid')}"


def page_marker(page: int) -> str:
    return f"[Page {page}]"


def record_text(
    pages: Sequence[RecordPage],
) -> tuple[str, list[tuple[int, int]]]:
    """A record's text, and where each page's text lies in it (start,
    end): each page opened by its marker, then its text."""
    text, spans = "", []
    for page in pages:
        body = page.text.strip("\n")
        opening = ("\n\n" if text else "") + page_marker(page.page)
        opening += "\n" if body else ""
        start = len(text) + len(opening)
        text += opening + body
        spans.append((start, len(text)))
    return text, spans


def merged(pages: Sequence[RecordPage], number: int) -> RecordPage:
    """One page, numbered `number`, from the record of an image that is
    one page of a larger document, as a PDF read page by page reads a
    page by vision: its texts in order, a blank line between; the printed
    page numbers it gives, joined by ", "; the languages in order of
    first mention; the notes, joined by "; "; and its status, transcribed
    when any of its pages is, else the first page's."""
    statuses = [page.status for page in pages]
    printed = [page.printed_page for page in pages if page.printed_page]
    return RecordPage(
        page=number,
        printed_page=", ".join(printed) if printed else None,
        status=TRANSCRIBED if TRANSCRIBED in statuses else statuses[0],
        text="\n\n".join(page.text.strip("\n") for page in pages if page.text),
        languages=list(
            dict.fromkeys(code for page in pages for code in page.languages)
        ),
        notes="; ".join(page.notes for page in pages if page.notes),
    )


def main_language(pages: Iterable[RecordPage]) -> str | None:
    """The language that holds most of a record's text: each page's
    letters and digits (outside editorial marks) count toward the first
    language it lists, weighed by its primary subtag, so that "de-de" and
    "de" are one language; a tie goes to the language listed first. The
    language is given by its code as first listed. None when no page
    lists one."""
    weights: dict[str, int] = {}
    codes: dict[str, str] = {}
    for page in pages:
        if page.languages:
            first = page.languages[0]
            primary = first.split("-", 1)[0]
            codes.setdefault(primary, first)
            weights[primary] = weights.get(primary, 0) + letters(page.text)
    if not weights:
        return None
    return codes[max(weights, key=lambda primary: weights[primary])]


def printed_page_note(printed: str) -> str:
    """What a page's marker line shows beside the marker for the page
    number printed on it: the number exactly as given, quoted as a JSON
    string, so that it stays on the marker's line whatever it holds: each
    character that would break the line (str.splitlines) is escaped."""
    quoted = json.dumps(printed, ensure_ascii=False)
    for breaking in "\x85\u2028\u2029":
        quoted = quoted.replace(breaking, f"\\u{ord(breaking):04x}")
    return f" (printed page number {quoted})"


def prompt_text(text: str, pages: Iterable[object]) -> str:
    """A transcription's `text` as the extraction prompts carry it: each
    page (a transcription's page, with its `page`, `start`, `end` and
    `printed_page`) whose record gives a printed page number shows it
    beside its own `[Page N]` marker, on that marker's line
    (printed_page_note), so the prompt has as many lines as the text and
    every line number into the text holds. The marker is looked for only
    between the end of the page before and the start of the page's text,
    where its opening lies, as a whole line; the image markers there
    (`[Page N, image k: sha1:...]`) are left as they are, and a page whose
    marker is not found there shows no number. A printed page number of
    whitespace alone is none, as no claim is ever given it
    (extract.printed_page_number). With no printed page number, the
    text is returned as it is."""
    pieces, cursor, last = [], 0, 0
    for page in pages:
        printed = getattr(page, "printed_page", None)
        if printed and printed.strip():
            marker = page_marker(page.page)
            at = _marker_line(text, marker, last, page.start)
            if at is not None:
                end = at + len(marker)
                pieces += [text[cursor:end], printed_page_note(printed)]
                cursor = end
        last = page.end
    if not pieces:
        return text
    return "".join(pieces) + text[cursor:]


def _marker_line(text: str, marker: str, start: int, end: int) -> int | None:
    """Where `marker` stands as a line of its own in text[start:end], or
    None."""
    at = text.find(marker, start, end)
    while at != -1:
        after = at + len(marker)
        opens = at == 0 or text[at - 1] == "\n"
        closes = after == len(text) or text[after] == "\n"
        if opens and closes:
            return at
        at = text.find(marker, at + 1, end)
    return None


def printed_pages(pages: Iterable[object]) -> dict[int, str]:
    """The page numbers printed on a transcription's pages, by their
    physical index: each page (a RecordPage, or a transcription's page
    with the record's fields) that gives one."""
    return {
        page.page: page.printed_page
        for page in pages
        if getattr(page, "printed_page", None)
    }
