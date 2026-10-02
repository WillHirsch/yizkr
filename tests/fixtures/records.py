"""Transcription records (ADR-0037) for the tests' scripted engines: what
a vision engine answers the transcription prompt with, built from the
text of a fictional source."""

from __future__ import annotations

import json
from collections.abc import Sequence


def record_page(
    number: int,
    text: str,
    *,
    languages: Sequence[str] = ("en",),
    status: str | None = None,
    printed_page: str | None = None,
    notes: str = "",
) -> dict:
    """One page of a record: transcribed when it holds text, blank when
    it holds none, unless `status` says otherwise."""
    written = bool(text.strip())
    return {
        "page": number,
        "printed_page": printed_page,
        "status": status or ("transcribed" if written else "blank"),
        "text": text,
        "languages": list(languages) if written else [],
        "notes": notes,
    }


def record_answer(
    *texts: str,
    document_class: str = "letter",
    languages: Sequence[str] = ("en",),
) -> str:
    """The record of a document whose pages hold `texts`, in order, as
    the engine answers it: one JSON object."""
    return json.dumps(
        {
            "document_class": document_class,
            "pages": [
                record_page(number, text, languages=languages)
                for number, text in enumerate(texts, 1)
            ],
        },
        ensure_ascii=False,
    )


def page_text(text: str, page: int = 1) -> str:
    """The transcription's text of a one-page record whose page holds
    `text`: the page's marker, then its text."""
    return f"[Page {page}]\n{text}" if text else f"[Page {page}]"


def stricter_check(monkeypatch, pattern: str, name: str = "stricter") -> None:
    """Make the transcription check stricter, as a change to it would: a
    text in which `pattern` (a regular expression) is found fails, as a
    tool failure by the signature `name`, where the check passed it.
    Every signature only flags a record's text (ADR-0037 §3), so the
    stricter check stands in for any change that fails what passed
    before, a structural one included. It is patched wherever the check
    is called from."""
    import re

    from yizkr.pipeline import (
        anomalies,
        transcribe,
        transcription_check,
    )

    real = transcription_check.check_transcription
    found = re.compile(pattern)

    def stricter(text, *args, **kwargs):
        check = real(text, *args, **kwargs)
        match = found.search(text)
        if not check.passed or match is None:
            return check
        return check.model_copy(
            update={
                "passed": False,
                "failure": transcription_check.TOOL_FAILURE,
                "flagged": False,
                "signature": name,
                "matched": match.group(0),
                "reason": (
                    "the output reads as a tool or engine failure, not a "
                    f"transcription (signature {name}: {match.group(0)!r})"
                ),
            }
        )

    for module in (transcription_check, transcribe, anomalies):
        monkeypatch.setattr(module, "check_transcription", stricter)
