"""Per-document language detection for transcriptions.

`yizkr ingest run` walks a whole folder, but a family archive mixes
languages — German register entries and letters beside English
naturalisation papers and interviews. A single run-wide ``--language``
therefore mis-tags every document that isn't in that language, and the
tag is real graph data: it is the BCP-47 language of the
``transcription_excerpt`` monolingual values written as references
(ADR-0003).

So ``--language`` defaults to ``auto``. A vision transcription's
language comes from its transcription record (ADR-0037): the engine
names the languages each page is written in, and the document's language
is the one that holds most of its text (`record_language`). A text that
has no record (a text file, a curator's correction, a deck's slide text,
a PDF read page by page from its text layer) is detected from its *full*
text outside its editorial marks (``[illegible]``, ``[?]``,
``[redacted]``, page markers) — offline and deterministically, with a
statistical identifier (py3langid: pure Python, self-hosted model, no
network, honouring rule 7). The marks are the transcription's, not the
document's words, and a line thick with them would otherwise read as
another language. An explicit ``--language de`` forces that code and
skips both. Documents too short or ambiguous to identify with confidence,
and records that name no language, fall back to ``--fallback-language``
rather than guessing — unknown is never narrowed (rule 5), only
defaulted to a code the curator chose.

The extraction model still tags each *excerpt* in its own language (a
German phrase quoted in an English interview keeps its ``de`` tag); this
module sets the document-level default the model is hinted with, not a
ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from yizkr.pipeline.transcription_record import (
    RecordPage,
    main_language,
    outside_editorial_marks,
)

AUTO = "auto"

# Mirrors claims.MonolingualValue's pattern: a detected code that Wikibase
# could not accept for monolingual text must never reach a statement, so a
# malformed identifier result is treated as "no detection".
LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9-]+)?$")

# Full-document text identifies at ~1.0 (see tests); the floor only bites on
# very short inputs — a postmark, a bare name — where a wrong guess
# is worse than an honest fallback.
CONFIDENCE_FLOOR = 0.5

# py3langid needs a few characters before its verdict means anything.
MIN_CHARS = 12

_identifier = None
_identifier_unavailable = False


def _get_identifier():
    """Lazily build the singleton identifier; ``None`` if unavailable.

    py3langid is a normal dependency, but detection degrades gracefully
    (falls back) rather than crashing an ingest if the model can't load.
    """
    global _identifier, _identifier_unavailable
    if _identifier is None and not _identifier_unavailable:
        try:
            from py3langid.langid import MODEL_FILE, LanguageIdentifier

            _identifier = LanguageIdentifier.from_pickled_model(
                MODEL_FILE, norm_probs=True
            )
        except Exception:  # pragma: no cover - defensive, model always ships
            _identifier_unavailable = True
    return _identifier


def detect_language(text: str) -> tuple[str | None, float]:
    """Best-guess ISO-639 code and a ``[0, 1]`` confidence for ``text``,
    read outside its editorial marks.

    Returns ``(None, 0.0)`` when the detector is unavailable or the text
    is too short to judge, so the caller falls back instead of guessing.
    """
    stripped = outside_editorial_marks(text or "").strip()
    if len(stripped) < MIN_CHARS:
        return None, 0.0
    identifier = _get_identifier()
    if identifier is None:
        return None, 0.0
    code, confidence = identifier.classify(stripped)
    confidence = float(confidence)
    if not code or not LANG_CODE_RE.match(code):
        return None, confidence
    return code, confidence


@dataclass(frozen=True)
class LanguageResolution:
    """The chosen language code and why it was chosen (for logs/audit)."""

    code: str
    source: str  # "override" | "record" | "detected" | "fallback"
    confidence: float | None = None

    def describe(self) -> str:
        if self.source == "record":
            return f"{self.code} (from the transcription record)"
        if self.source == "detected":
            return f"{self.code} (detected, {self.confidence:.0%} confidence)"
        if self.source == "fallback":
            if self.confidence is None:
                return f"{self.code} (fallback: no language to go by)"
            return (
                f"{self.code} (fallback: best guess only "
                f"{self.confidence:.0%})"
            )
        return f"{self.code} (forced)"


def resolve_language(
    text: str,
    requested: str,
    *,
    fallback: str = "en",
    floor: float = CONFIDENCE_FLOOR,
) -> LanguageResolution:
    """Decide a document's language tag from the request and its text.

    ``requested`` is either an explicit code (forced, detection skipped)
    or ``auto`` (detect, with a confidence floor and a fallback code).
    """
    if requested and requested != AUTO:
        return LanguageResolution(requested, "override")
    code, confidence = detect_language(text)
    if code is not None and confidence >= floor:
        return LanguageResolution(code, "detected", confidence)
    return LanguageResolution(
        fallback, "fallback", confidence if code else None
    )


def record_language(
    pages: Iterable[RecordPage],
    requested: str,
    *,
    fallback: str = "en",
) -> LanguageResolution:
    """Decide a document's language tag from the request and its
    transcription record: an explicit code is forced; ``auto`` takes the
    language that holds most of the record's text
    (transcription_record.main_language), and the fallback code when the
    record names none."""
    if requested and requested != AUTO:
        return LanguageResolution(requested, "override")
    code = main_language(pages)
    if code is not None:
        return LanguageResolution(code, "record")
    return LanguageResolution(fallback, "fallback")
