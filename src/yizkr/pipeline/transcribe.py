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
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from yizkr.pipeline.language import LanguageResolution, resolve_language
from yizkr.pipeline.llm import LlmClient

TRANSCRIPTION_PROMPT = """\
Transcribe every piece of text visible in this image of a historical
document, exactly as written. Preserve line breaks, spelling mistakes,
abbreviations, and uncertain readings; mark illegible passages with
[illegible] and uncertain readings with [?]. Do not translate, do not
normalise, do not add commentary — output the transcription only.
"""


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


class LlmVisionTranscriber:
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

    def transcribe(
        self, media_path: Path, artifact_qid: str | None = None
    ) -> Transcription:
        text = self.client.complete(TRANSCRIPTION_PROMPT, [media_path])
        self.last_resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        return Transcription(
            artifact_qid=artifact_qid,
            text=text,
            language=self.last_resolution.code,
            engine=f"llm-vision:{self.client.model_identifier}",
        )


class TextFileTranscriber:
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

    def transcribe(
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
            f"HTR adapter for {self.platform!r} is a stub (ROADMAP W1 task 4 "
            f"defines it; implementation is deferred). Use the llm or "
            f"textfile engines meanwhile."
        )
