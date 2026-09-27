"""Transcription step: artifact media -> text, with pluggable engines.

Engines implement `Transcriber`. Provided:

* `LlmVisionTranscriber` — LLM-vision first (ADR-0004 W1), via any
  `yizkr.pipeline.llm.LlmClient`.
* `TextFileTranscriber` — reads an existing transcription from a text
  file (manual transcriptions, or output of any external tool). Keeps
  the pipeline fully runnable with no LLM configured.
* `HtrPlatformAdapter` — stub for HTR platforms (e.g. Transkribus);
  not implemented, it raises with a pointer.

Output is a `Transcription` JSON document consumed by the extraction
step. The transcription itself is NOT written to the graph — only the
claims extracted from it are, each carrying its fragment locator and
excerpt (ADR-0003).

Every engine's `transcribe()` runs the transcription check
(`yizkr.pipeline.transcription_check`) on its output and raises
`TranscriptionCheckFailed` instead of returning an output that fails it:
a tool-failure message or a refusal is never handed on as a
transcription. The check runs on every output, whether the engine made
it in this call or the LLM cache kept it from an earlier one (ADR-0032),
so a stricter check applies to every transcription; the vision
transcriber rejects a failing output through its client, so the engine
is asked again rather than served it. `transcribe_unchecked()` is the
transcription step alone, for a caller that runs the check as a step of
its own.

Vision transcription of a PDF needs poppler-utils on the ingestion host:
the claude-code engine's Read tool renders the pages with ``pdftoppm``,
and the check counts them with ``pdfinfo``. A caller checks the host
once, before it transcribes anything: `preflight_vision_transcription`
for a whole run's media, which fails fast with the install command
(`require_pdf_renderer`).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from yizkr.pipeline.language import LanguageResolution, resolve_language
from yizkr.pipeline.llm import LlmClient, reject_response
from yizkr.pipeline.transcription_check import (
    TranscriptionCheck,
    TranscriptionCheckFailed,
    check_transcription,
    media_page_count,
)

TRANSCRIPTION_PROMPT = """\
Transcribe every piece of text visible in this image of a historical
document, exactly as written. Preserve line breaks, spelling mistakes,
abbreviations, and uncertain readings; mark illegible passages with
[illegible] and uncertain readings with [?]. Do not translate, do not
normalise, do not add commentary — output the transcription only.
"""

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


class Transcription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_qid: str | None = Field(default=None, pattern=r"^Q[1-9]\d*$")
    text: str = Field(min_length=1)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[a-z0-9-]+)?$")
    engine: str = Field(min_length=1)


class Transcriber(Protocol):
    def transcribe(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription: ...


class _CheckedTranscriber:
    """`transcribe()` = the engine's `transcribe_unchecked()`, then the
    transcription check on its output."""

    # the check behind the last transcribe(), for callers to record
    last_check: TranscriptionCheck | None = None

    def transcribe_unchecked(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        raise NotImplementedError

    def transcribe(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        transcription = self.transcribe_unchecked(media_path, artifact_qid)
        self.last_check = check_transcription(
            transcription.text, pages=media_page_count(media_path)
        )
        if not self.last_check.passed:
            failed = TranscriptionCheckFailed(self.last_check)
            self._reject(failed)
            raise failed
        return transcription

    def _reject(self, failed: TranscriptionCheckFailed) -> None:
        """Nothing to reject: no engine answered for this output."""


class LlmVisionTranscriber(_CheckedTranscriber):
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

    def transcribe_unchecked(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        text = self.client.complete(TRANSCRIPTION_PROMPT, [media_path])
        self._response = text
        self.last_resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        return Transcription(
            artifact_qid=artifact_qid,
            text=text,
            language=self.last_resolution.code,
            engine=f"llm-vision:{self.client.model_identifier}",
        )

    def _reject(self, failed: TranscriptionCheckFailed) -> None:
        """Reject the engine's answer through the client, so that a client
        that keeps answers asks the engine again next time."""
        if self._response is not None:
            reject_response(self.client, self._response, str(failed))


class TextFileTranscriber(_CheckedTranscriber):
    """Wrap an existing transcription file (manual or external tool)."""

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

    def transcribe_unchecked(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        text = self.text_file.read_text()
        self.last_resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        return Transcription(
            artifact_qid=artifact_qid,
            text=text,
            language=self.last_resolution.code,
            engine=f"text-file:{self.text_file.name}",
        )


class HtrPlatformAdapter:
    """Handwritten-text-recognition platform adapter (not yet implemented)."""

    def __init__(self, platform: str = "transkribus") -> None:
        self.platform = platform

    def transcribe(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        raise NotImplementedError(
            f"HTR adapter for {self.platform!r} is a stub and not "
            f"implemented. Use the llm or textfile engines meanwhile."
        )
