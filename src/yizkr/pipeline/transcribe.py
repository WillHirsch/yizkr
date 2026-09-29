"""Transcription step: artifact media -> text, with pluggable engines.

Engines implement `Transcriber`. Provided:

* `LlmVisionTranscriber` — LLM-vision first (ADR-0004 W1), via any
  `yizkr.pipeline.llm.LlmClient`.
* `PdfPageTranscriber` — a PDF read page by page
  (yizkr.pipeline.textlayer): each page from its text layer where it has
  a usable one, deterministically and without a model call, and by the
  vision engine otherwise; and each image placed within a text-layer page
  read by the vision engine on its own, as a witness of its own
  (ADR-0008). It reads only a PDF with at least one such page, and leaves
  any other PDF to be read whole by `LlmVisionTranscriber`.
* `TextFileTranscriber` — reads an existing transcription from a text
  file (manual transcriptions, or output of any external tool). Keeps
  the pipeline fully runnable with no LLM configured.
* `HtrPlatformAdapter` — stub for HTR platforms (e.g. Transkribus);
  not implemented, it raises with a pointer.

Output is a `Transcription` JSON document consumed by the extraction
step. It names the artifact it transcribes by content: the SHA-1 of the
media's bytes, which the transcriber hashes from the file it is given
(ADR-0010), so a transcription is the same whatever instance the
artifact is registered on. The transcription itself is NOT written to
the graph — only the claims extracted from it are, each carrying its
fragment locator and excerpt (ADR-0003).

The vision engine answers with the transcription record (ADR-0037,
yizkr.pipeline.transcription_record): the document's class, and per page
the number printed on it, a status, its text with the editorial marks,
its languages and notes. The transcription keeps the record: its `pages`
hold each page's fields, `page` being the page's physical index and
`printed_page` the number printed on it, and its `document_class` the
class; its text opens each page with a ``[Page N]`` marker. Its language
is the one that holds most of the record's text
(language.record_language). A text file's transcription has no record,
and its language is detected from its text.

Every engine's `transcribe()` runs the transcription check
(`yizkr.pipeline.transcription_check`) on its output and raises
`TranscriptionCheckFailed` instead of returning an output that fails it:
a tool-failure message, or an answer that is not the record (a refusal,
in any language), is never handed on as a transcription. An output the
check passes with a flag carries the flag (`Transcription.flag`). The
check runs on every output, whether the engine made it in this call or
the LLM cache kept it from an earlier one (ADR-0032). The vision
transcriber rejects a failing output through its client, so that a client
that keeps answers never serves it again; when the failing output was one
the client kept, rather than the engine's answer now, it asks once more,
and the engine's answer is checked in its place, as it would have been
without the cache. An answer whose engine reports the stop reason
max_tokens (llm.at_output_limit) is cut off, and fails and is rejected
so. An engine whose own record of the call says its Read tool failed on
the input raises `EngineToolError` instead of answering
(yizkr.pipeline.llm); the transcriber checks that as a tool error, which
fails the output whatever it says. A claude CLI that gave up on its
answer at the output limit raises `OutputLimitReached`, and the
transcriber checks that as cut off. Nothing kept either answer, so it is
not asked for again: the document fails, and the next run asks the engine
again, unless the failed call was a refresh (`--refresh transcribe`). A
refreshed call that raises replaces nothing, so the answer the cache kept
and the transcription view stay, and the next run reuses them without
asking the engine (yizkr.pipeline.transcription_check).
`transcribe_unchecked()` is the transcription step alone, for a caller
that runs the check as a step of its own; an engine error, the tool error
among them, still raises from it, and so does an answer it cannot make a
transcription of (`UnreadableAnswer`: not the record, or cut off). A
caller that keeps transcriptions checks the ones it reuses again
(yizkr.pipeline.orchestrate), so a stricter check applies to every
transcription, apart from its reading of tool errors and of a cut-off
answer, which only a call records (yizkr.pipeline.transcription_check).

A PDF read page by page (`PdfPageTranscriber`) has one transcription of
its own text: its pages in order, each opened by a ``[Page N]`` marker so
that every claim read from it carries its page. An image placed within
one of its text-layer pages is marked where it sits, by the SHA-1 of its
bytes (``[Page 6, image 1: sha1:…]``), but what is read from the image is
never part of it: the image's own transcription names the image, so that
the claims read from each cite the witness that makes them (ADR-0008).
The transcription's `pages` hold, page by page, where the page's text
lies in `text`, the images placed within it and the record's fields,
which are content, and apart from them the page's provenance: the method
that read it (text-layer, vision, or none, for a page with nothing to
read), what made it, and how many annotations its text layer leaves
unread. A page read by vision takes the record's fields from its
reading; a text-layer page is transcribed, and a page with nothing to
read blank. The check runs
on the reading of each page and each image the vision engine read, as on
any vision transcription, and never on text read from a text layer: that
is the document's own text, read without an engine, which no engine
failure can have shaped.

Vision transcription of a PDF read whole needs poppler-utils on the
ingestion host: the claude-code engine's Read tool renders the pages with
``pdftoppm``, and the check counts them with ``pdfinfo``. A PDF read page
by page gives the engine images, and needs neither, but whether a PDF is
read whole is known only once its pages are read. So a caller checks the
host once for any PDF, before it transcribes anything:
`preflight_vision_transcription` for a whole run's media, which fails
fast with the install command (`require_pdf_renderer`).
"""

from __future__ import annotations

import shutil
import tempfile
import textwrap
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_serializer,
    model_validator,
)

from yizkr.claims import ArtifactRef
from yizkr.pipeline.language import (
    LanguageResolution,
    record_language,
    resolve_language,
)
from yizkr.pipeline.llm import (
    EngineToolError,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)
from yizkr.pipeline.transcription_check import (
    TranscriptionCheck,
    TranscriptionCheckFailed,
    check_answer,
    check_transcription,
    is_pdf,
    media_page_count,
)
from yizkr.pipeline.transcription_record import (
    BLANK,
    TRANSCRIBED,
    UNCLASSIFIED,
    LanguageCode,
    PageStatus,
    RecordPage,
    TranscriptionRecord,
    artifact_classes,
    merged,
    page_marker,
    printed_pages,
    record_text,
)


def _transcription_prompt(classes: Sequence[str]) -> str:
    """The prompt that asks for the transcription record (ADR-0037),
    naming the document classes it may give: `classes` and UNCLASSIFIED."""
    listed = ", ".join(f'"{name}"' for name in classes)
    document_class = textwrap.fill(
        f'- "document_class": what the document is, one of {listed}, or '
        f'"{UNCLASSIFIED}" when none of them fits.',
        width=72,
        subsequent_indent="  ",
    )
    return f"""\
Transcribe this historical document (an image, or a PDF of one or more
pages) exactly as written, and answer with the transcription record: one
JSON object and nothing else, in this form:

{{"document_class": "...", "pages": [{{"page": 1, "printed_page": null,
"status": "transcribed", "text": "...", "languages": ["de"],
"notes": ""}}]}}

{document_class}
- "pages": one object for each page, in order. An image is one page,
  unless it shows several pages of a document (an open book): then one
  object for each page it shows.
- "page": the page's place, 1 for the first, 2 for the next, and so on.
- "printed_page": the page number printed or written on the page,
  exactly as written ("iv", "12", "3a"), or null when it shows none.
- "status": "transcribed" for a page whose text you read, in whole or in
  part; "blank" for a page with no text (a photograph without any
  writing, an empty reverse); "illegible" for a page with text of which
  nothing can be read; "not-a-document-page" for anything that is not a
  page of the document (a colour chart, a ruler, a scanner's cover).
- "text": every piece of text visible on the page, exactly as written.
  Preserve line breaks, spelling mistakes, abbreviations and uncertain
  readings; mark illegible passages with [illegible], uncertain readings
  with [?], and text that is blacked out or covered with [redacted]. Do
  not translate, do not normalise, and put no commentary in the text.
  An empty string for a page with no text.
- "languages": the language codes (ISO 639-1, such as "en", "de", "ru",
  "fy") of the languages the page's text is written in, the one that
  holds most of it first; an empty list for a page with no text.
- "notes": anything you have to say about the page (damage, a stamp, a
  hand other than the main one), or an empty string.
"""


TRANSCRIPTION_PROMPT = _transcription_prompt(artifact_classes())

# The poppler-utils tools a PDF needs: the renderer the vision engine
# reads pages through, and the page counter the transcription check uses.
PDF_TOOLS = ("pdftoppm", "pdfinfo")
PDF_TOOLS_INSTALL = (
    "install poppler-utils on the ingestion host (Debian/Ubuntu: "
    "`sudo apt-get install poppler-utils`; macOS: `brew install poppler`), "
    "check it with `pdftoppm -v`, then re-run"
)


class MissingPrerequisite(RuntimeError):
    """A host tool the requested transcription needs is not installed."""


def require_pdf_renderer(
    which: Callable[[str], str | None] | None = None,
) -> None:
    """Fail fast, with the fix, when the PDF tools are not on PATH.

    `which` looks a tool up (default `shutil.which`)."""
    which = which or shutil.which
    missing = [tool for tool in PDF_TOOLS if which(tool) is None]
    if missing:
        raise MissingPrerequisite(
            "vision transcription of a PDF needs poppler-utils (pdftoppm "
            "renders the pages for the engine, pdfinfo counts them for the "
            f"transcription check), and {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not on PATH: "
            f"{PDF_TOOLS_INSTALL}"
        )


def preflight_vision_transcription(
    media_paths: Iterable[Path],
    which: Callable[[str], str | None] | None = None,
) -> None:
    """Check the host once, before a run transcribes any of
    `media_paths` by vision: a PDF among them needs the PDF renderer."""
    if any(Path(path).suffix.lower() == ".pdf" for path in media_paths):
        require_pdf_renderer(which)


# the SHA-1 of an image's bytes, as a page names it
Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
# the methods a page of a PDF read page by page is read by
# (yizkr.pipeline.textlayer); "none" reads a page with nothing to read
PageMethod = Literal["text-layer", "vision", "none"]


class PageProvenance(BaseModel):
    """What read one page of a PDF read page by page: its method, what
    made its text (the text-layer method with its version and the
    library's, or the vision client), and how many annotations the page
    shows that its text layer leaves unread (a text-layer page's)."""

    model_config = ConfigDict(extra="forbid")

    method: PageMethod
    engine: str = Field(min_length=1)
    annotations: int = Field(default=0, ge=0)


class TranscribedPage(BaseModel):
    """One page of a transcription: its place in the media (`page`, from
    1, the physical index), where its text lies in the transcription's
    `text` (`start` to `end`, after its markers), the images placed within
    it that have transcriptions of their own, by the SHA-1 of their bytes
    in their order on the page, and the transcription record's fields
    (ADR-0037): the page number printed on it (`printed_page`), its
    `status`, the `languages` its text is written in and the engine's
    `notes`. Apart from that content, its provenance."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    images: list[Sha1] = Field(default_factory=list)
    printed_page: str | None = None
    status: PageStatus
    languages: list[LanguageCode] = Field(default_factory=list)
    notes: str = ""
    provenance: PageProvenance


class Transcription(BaseModel):
    """A transcription of one artifact's media. `artifact` names the
    artifact by the SHA-1 of the media's bytes: the key its claims cite,
    and part of the transcription's content, which depends on no
    instance. `pages` are its pages in order (`TranscribedPage`): a vision
    transcription's, from its record, and a PDF read page by page's; a
    transcription with no pages (a text file's, a correction, a deck's
    slide text) holds no `pages` key. `document_class` is the class its
    record gives (a vision transcription made whole), and is absent
    otherwise. `flag` names the signature the transcription check flagged
    its wording by, when it did (yizkr.pipeline.transcription_check), and
    is absent otherwise; like the provenance, it is no part of its
    content hash."""

    model_config = ConfigDict(extra="forbid")

    artifact: ArtifactRef
    text: str = Field(min_length=1)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[a-z0-9-]+)?$")
    engine: str = Field(min_length=1)
    document_class: str | None = None
    pages: list[TranscribedPage] | None = None
    flag: str | None = None
    # what a page-by-page reading made beside the transcription, for its
    # caller to keep (PdfReading); never part of the view
    _reading: Any = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _pages_lie_in_the_text(self) -> Transcription:
        """Each page's text lies within the text, the pages in order."""
        last = 0
        for page in self.pages or ():
            if not last <= page.start <= page.end <= len(self.text):
                raise ValueError(
                    f"page {page.page}'s text does not lie in the "
                    "transcription's text, after the page before it"
                )
            last = page.end
        numbers = [page.page for page in self.pages or ()]
        if numbers != sorted(set(numbers)):
            raise ValueError("the pages are not in order, each once")
        return self

    @model_serializer(mode="wrap")
    def _without_absent_fields(self, handler: Any) -> dict[str, Any]:
        """The transcription as written: one with no pages, class or flag
        holds no key for it."""
        data = handler(self)
        for name in ("pages", "document_class", "flag"):
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    @property
    def reading(self) -> PdfReading | None:
        """What the page-by-page reading that made this transcription made
        beside it, while its caller has yet to keep it; None for any
        other transcription."""
        return self._reading

    def forget_reading(self) -> None:
        """Let go of the reading once its caller has kept it."""
        self._reading = None

    def page_text(self, page: TranscribedPage) -> str:
        """The text of one page, as `pages` places it."""
        return self.text[page.start : page.end]

    def record(self) -> TranscriptionRecord | None:
        """The transcription record this transcription was made from: its
        class and its pages, each with its text; None for one that has no
        class (no record, or a PDF read page by page), and for a stored
        one whose fields the record no longer accepts (a class the
        manifest has since dropped, say), which the check then fails as
        holding no record."""
        if self.document_class is None or not self.pages:
            return None
        try:
            return TranscriptionRecord(
                document_class=self.document_class,
                pages=[
                    RecordPage(
                        page=page.page,
                        printed_page=page.printed_page,
                        status=page.status,
                        text=self.page_text(page),
                        languages=page.languages,
                        notes=page.notes,
                    )
                    for page in self.pages
                ],
            )
        except ValidationError:
            return None

    def page_record(self, page: TranscribedPage) -> TranscriptionRecord | None:
        """The one-page record of one of its pages, as the reading of
        one image with no class: the page's text and its record's fields,
        numbered 1. A page of a PDF read page by page that the vision
        engine read is checked by it, as its reading was when the page was
        made. None when the page's fields are not a record's page (a page
        called transcribed with no text)."""
        try:
            return TranscriptionRecord(
                document_class=UNCLASSIFIED,
                pages=[
                    RecordPage(
                        page=1,
                        printed_page=page.printed_page,
                        status=page.status,
                        text=self.page_text(page),
                        languages=page.languages,
                        notes=page.notes,
                    )
                ],
            )
        except ValidationError:
            return None

    def printed_pages(self) -> dict[int, str]:
        """The page numbers printed on the transcription's pages, by their
        physical index (`page`): each page that gives one. Empty for a
        transcription without pages, or whose pages show none. A page's
        physical index is its place in the media; the number printed on
        it may differ, or be no number at all ("iv")."""
        return printed_pages(self.pages or ())

    @classmethod
    def from_record(
        cls,
        record: TranscriptionRecord,
        *,
        artifact: ArtifactRef,
        language: str,
        engine: str,
    ) -> Transcription:
        """A vision transcription made whole, from its record: its text is
        the record's (transcription_record.record_text), each page's
        fields are the record's, and each page's provenance is the vision
        engine that read it."""
        text, spans = record_text(record.pages)
        return cls(
            artifact=artifact,
            text=text,
            language=language,
            engine=engine,
            document_class=record.document_class,
            pages=[
                TranscribedPage(
                    page=page.page,
                    start=start,
                    end=end,
                    printed_page=page.printed_page,
                    status=page.status,
                    languages=page.languages,
                    notes=page.notes,
                    provenance=PageProvenance(method="vision", engine=engine),
                )
                for page, (start, end) in zip(record.pages, spans, strict=True)
            ],
        )


class Transcriber(Protocol):
    def transcribe(self, media_path: Path) -> Transcription: ...


class UnreadableAnswer(RuntimeError):
    """The engine's answer is not the transcription record, or the engine
    stopped it at its output limit: `check` is the transcription check's
    failing verdict, and `response` the answer, for its client to
    reject."""

    def __init__(self, check: TranscriptionCheck, response: str) -> None:
        self.check = check
        self.response = response
        super().__init__(check.reason)


class _CheckedTranscriber:
    """`transcribe()` = the engine's `transcribe_unchecked()`, then the
    transcription check on its output."""

    # the check behind the last transcribe(), for callers to record
    last_check: TranscriptionCheck | None = None
    # whether the engine's transcriptions carry a record, and one without
    # fails the check
    expects_record = False

    def transcribe_unchecked(self, media_path: Path) -> Transcription:
        raise NotImplementedError

    def transcribe(self, media_path: Path) -> Transcription:
        """The checked transcription of `media_path`. An output that fails
        the check is rejected; one its client kept from an earlier call,
        rather than the engine's answer now, is asked for once more, and
        the engine's answer is checked in its place. An engine that reports
        a tool failing on the input, or that the claude CLI gave up on at
        its output limit, answers nothing: the check fails it as a tool
        error or as cut off, and it is not asked for again, since nothing
        kept it. An output that passes with a flag carries the flag."""
        pages = media_page_count(media_path)
        for _ask in range(2):
            try:
                transcription = self.transcribe_unchecked(media_path)
            except EngineToolError as exc:
                self.last_check = check_transcription(
                    exc.response, pages=pages, tool_failures=exc.failures
                )
                raise TranscriptionCheckFailed(self.last_check) from exc
            except OutputLimitReached as exc:
                self.last_check = check_transcription(
                    "", pages=pages, cut_off=True
                )
                raise TranscriptionCheckFailed(self.last_check) from exc
            except UnreadableAnswer as exc:
                self.last_check = exc.check.model_copy(update={"pages": pages})
            else:
                self.last_check = check_transcription(
                    transcription.text,
                    pages=pages,
                    record=transcription.record(),
                    expect_record=self.expects_record,
                    whole_pdf=is_pdf(media_path),
                )
                if self.last_check.passed:
                    if self.last_check.flagged:
                        transcription.flag = self.last_check.signature
                    return transcription
            failed = TranscriptionCheckFailed(self.last_check)
            if not self._reject(failed):
                break
        raise failed

    def _reject(self, failed: TranscriptionCheckFailed) -> bool:
        """Nothing to reject: no engine answered for this output, so
        asking again would give the same one."""
        return False


class LlmVisionTranscriber(_CheckedTranscriber):
    expects_record = True

    def __init__(
        self,
        client: LlmClient,
        language: str = "en",
        fallback_language: str = "en",
    ) -> None:
        self.client = client
        self.language = language
        self.fallback_language = fallback_language
        # the resolution behind the last transcribe(), for callers to log
        self.last_resolution: LanguageResolution | None = None
        # the engine's answer behind the last transcription
        self._response: str | None = None

    def transcribe_unchecked(self, media_path: Path) -> Transcription:
        """The engine's reading of the media at `media_path`, named by the
        SHA-1 of its bytes, which are hashed before the engine is asked:
        the transcription its record makes (`Transcription.from_record`),
        in the language the record gives (language.record_language). Its
        `engine` names the client that made the reading: the one asked,
        or, for a reading the client's cache adopted rather than an engine
        gave (an archived transcription), the one the adoption names
        (`made_by`). Raises UnreadableAnswer for an answer the engine cut
        off at its output limit (llm.at_output_limit), or that is not the
        record."""
        self._response = None
        artifact = ArtifactRef.from_path(media_path)
        text = self.client.complete(TRANSCRIPTION_PROMPT, [media_path])
        self._response = text
        check, record = check_answer(
            text, cut_off=at_output_limit(self.client, text)
        )
        if record is None:
            raise UnreadableAnswer(check, text)
        self.last_resolution = record_language(
            record.pages, self.language, fallback=self.fallback_language
        )
        made_by = getattr(text, "made_by", None)
        return Transcription.from_record(
            record,
            artifact=artifact,
            language=self.last_resolution.code,
            engine=f"llm-vision:{made_by or self.client.model_identifier}",
        )

    def _reject(self, failed: TranscriptionCheckFailed) -> bool:
        """Reject the answer through the client, so that a client that
        keeps answers asks the engine again rather than serving it; True
        when the answer was a kept one, which the engine has not given in
        this call (llm.reject_response)."""
        if self._response is None:
            return False
        return reject_response(self.client, self._response, str(failed))


class TextFileTranscriber(_CheckedTranscriber):
    """Wrap an existing transcription file (manual or external tool): the
    text is the file's, and the transcription names the media it is of,
    the `media_path` given to transcribe(), by the SHA-1 of its bytes. It
    has no record: its language is detected from its text."""

    def __init__(
        self,
        text_file: Path,
        language: str = "en",
        fallback_language: str = "en",
    ) -> None:
        self.text_file = text_file
        self.language = language
        self.fallback_language = fallback_language
        self.last_resolution: LanguageResolution | None = None

    def transcribe_unchecked(self, media_path: Path) -> Transcription:
        artifact = ArtifactRef.from_path(media_path)
        text = self.text_file.read_text()
        self.last_resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        return Transcription(
            artifact=artifact,
            text=text,
            language=self.last_resolution.code,
            engine=f"text-file:{self.text_file.name}",
        )


# the route a PDF read page by page names first in its transcription's
# `engine`, before the text-layer method it reads pages with
PDF_PAGES = "pdf-pages"


@dataclass
class ImageReading:
    """An image placed within a text-layer page of a PDF read page by
    page, and its own reading: its bytes (in the reading's scratch folder
    until the reading is closed), each place it has, as (page, its place
    among that page's images read on their own), and its transcription,
    or the transcription check's failure that left it without one."""

    sha1: str
    suffix: str
    path: Path
    places: list[tuple[int, int]]
    transcription: Transcription | None = None
    failure: str | None = None

    @property
    def names(self) -> list[str]:
        """What the image is called, once for each place it has, in the
        order the PDF places it: "page 6, image 1" (ADR-0008)."""
        return [f"page {page}, image {place}" for page, place in self.places]

    @property
    def pages(self) -> list[int]:
        return sorted({page for page, _place in self.places})


@dataclass
class PdfReading:
    """What reading a PDF page by page made beside its transcription, for
    its caller to keep: each image read on its own, and notes on what
    could not be read. The images' bytes live in a scratch folder of the
    reading's own, private to its owner, until `close`."""

    images: list[ImageReading]
    notes: list[str]
    folder: tempfile.TemporaryDirectory = field(repr=False)

    def attention(self) -> list[str]:
        """What a reader should look at: each image whose reading failed
        the transcription check, and so has no transcription of its own,
        and each image that could not be read."""
        failed = [
            f"{image.names[0]} (sha1 {image.sha1[:12]}): its reading failed "
            f"the transcription check ({image.failure}), so it has no "
            "transcription of its own"
            for image in self.images
            if image.failure is not None
        ]
        return failed + list(self.notes)

    def close(self) -> None:
        """Remove the images' bytes from the scratch folder."""
        self.folder.cleanup()


class PdfPageTranscriber:
    """A PDF read page by page (yizkr.pipeline.textlayer): each page from
    its text layer where it has a usable one, by the vision engine where
    it does not, and not at all where it holds nothing to read; and each
    image placed within a text-layer page read by the vision engine on its
    own, as a witness of its own (ADR-0008).

    `transcribe` gives the PDF's transcription, with the page markers and
    the provenance the module docstring describes, and the images'
    readings as its `reading`, for the caller to keep and then close. It
    gives None for a PDF none of whose pages reads from its text layer, or
    that PDFium cannot read: the caller reads that PDF whole, in one
    vision call that gives the engine the PDF itself. `client` gives the
    vision
    engine's client, and is called only once a page or an image is to be
    read by vision, so a PDF read wholly from its text layer starts no
    engine. `excluded` says whether the never-ingest list names an image
    by the SHA-1 of its bytes: such an image is never read, kept or
    marked.

    The reading of each vision page is checked, as any vision
    transcription is, and one that fails raises: the PDF's transcription
    fails with it. An image's reading that fails the check leaves the
    image without a transcription, and its reason in `reading.attention`,
    since the PDF's own text does not hold it."""

    def __init__(
        self,
        client: Callable[[], LlmClient],
        language: str = "en",
        fallback_language: str = "en",
        excluded: Callable[[str], bool] | None = None,
    ) -> None:
        self._client = client
        self.language = language
        self.fallback_language = fallback_language
        self.excluded = excluded or (lambda sha1: False)
        # the resolution behind the last transcribe(), for callers to log
        self.last_resolution: LanguageResolution | None = None
        self._vision: LlmVisionTranscriber | None = None

    def _read_by_vision(self, path: Path) -> Transcription:
        """The checked vision transcription of one page's or one image's
        file."""
        if self._vision is None:
            self._vision = LlmVisionTranscriber(
                self._client(),
                language=self.language,
                fallback_language=self.fallback_language,
            )
        return self._vision.transcribe(path)

    def transcribe(self, media_path: Path) -> Transcription | None:
        """The PDF at `media_path` read page by page, or None when it is
        to be read whole."""
        from yizkr.pipeline import textlayer

        folder = tempfile.TemporaryDirectory(prefix="yizkr-pdf-")
        try:
            read = textlayer.read_pages(
                media_path, Path(folder.name), excluded=self.excluded
            )
            if read is None or not read.by_text_layer:
                folder.cleanup()
                return None
            transcription = self._transcription(
                ArtifactRef.from_path(media_path), read
            )
            reading = PdfReading(
                [
                    ImageReading(
                        image.sha1, image.suffix, image.path, image.places
                    )
                    for image in read.images.values()
                ],
                list(read.notes),
                folder,
            )
            for image in reading.images:
                try:
                    image.transcription = self._read_by_vision(image.path)
                except TranscriptionCheckFailed as failed:
                    image.failure = failed.check.failure
        except BaseException:
            folder.cleanup()
            raise
        transcription._reading = reading
        return transcription

    def _transcription(
        self, artifact: ArtifactRef, read: Any
    ) -> Transcription:
        """The PDF's own text (`read`, textlayer.PdfPages), page by page
        in order: each page opened by its marker and the markers of the
        images placed within it, then its text, read by its method. A page
        with nothing to read is its markers alone, read by no method: what
        made it is the text-layer method, which found nothing on it, and
        its status is blank. A page read by vision takes its text and the
        record's fields from the record of its reading, merged into one
        page (transcription_record.merged); a page read from its text
        layer is transcribed, with no printed page number, languages or
        notes, since no engine read it. The transcription carries the
        flag of the first vision page the check flagged."""
        from yizkr.pipeline import textlayer

        method = f"{textlayer.METHOD}/{textlayer.VERSION}"
        by_text_layer = f"{method} ({textlayer.LIBRARY})"
        text, pages, bodies = "", [], []
        flag = None
        for page in read.pages:
            marks = [page_marker(page.number)] + [
                f"[Page {page.number}, image {place}: sha1:{sha1}]"
                for place, sha1 in page.images
            ]
            fields: dict[str, Any] = {
                "status": TRANSCRIBED if page.text else BLANK
            }
            if page.method == textlayer.VISION:
                reading = self._read_by_vision(page.vision)
                record = reading.record()
                one = merged(record.pages, page.number)
                body, engine = one.text.strip(), reading.engine
                fields = one.model_dump(exclude={"page", "text"})
                flag = flag or reading.flag
            else:
                body, engine = page.text, by_text_layer
            opening = ("\n\n" if text else "") + "\n".join(marks)
            opening += "\n" if body else ""
            start = len(text) + len(opening)
            text += opening + body
            bodies.append(body)
            pages.append(
                TranscribedPage(
                    page=page.number,
                    start=start,
                    end=len(text),
                    images=[sha1 for _place, sha1 in page.images],
                    **fields,
                    provenance=PageProvenance(
                        method=page.method,
                        engine=engine,
                        annotations=page.annotations,
                    ),
                )
            )
        self.last_resolution = resolve_language(
            "\n\n".join(body for body in bodies if body),
            self.language,
            fallback=self.fallback_language,
        )
        return Transcription(
            artifact=artifact,
            text=text,
            language=self.last_resolution.code,
            engine=f"{PDF_PAGES}:{method}",
            pages=pages,
            flag=flag,
        )


class HtrPlatformAdapter:
    """Handwritten-text-recognition platform adapter: a stub that raises.

    LLM vision is sufficient for handwriting, so no HTR platform is
    integrated; the `llm` and `textfile` engines transcribe instead.
    """

    def __init__(self, platform: str = "transkribus") -> None:
        self.platform = platform

    def transcribe(self, media_path: Path) -> Transcription:
        raise NotImplementedError(
            f"HTR adapter for {self.platform!r} is a stub and not "
            f"implemented. Use the llm or textfile engine instead."
        )
