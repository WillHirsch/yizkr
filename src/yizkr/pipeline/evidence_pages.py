"""Where a passage of a document lies in its transcription: the page its
first line lies on, as its physical number and the number printed on it.

The place stage reads a container's evidence through it
(yizkr.pipeline.places), and preparation the statement a reading quotes
(yizkr.pipeline.preparation). It holds no place-writing code, so a stage
before write that reads it does not depend on the place writer.
"""

from __future__ import annotations

from yizkr.core import verbatim


class EvidencePages:
    """Where a document's container evidence lies: the page of
    `transcription` the first line of an evidence lies on, found as the
    extraction checked it (core.verbatim), as its physical number and
    the number printed on it (extract.printed_page_number: None where the
    page's record gives none); (None, None) for a transcription with no
    pages, or one the evidence does not occur in. The transcription is
    read once, and each evidence looked for once."""

    def __init__(self, transcription) -> None:
        self.transcription = transcription
        self._pages = (
            (transcription.pages or ()) if transcription is not None else ()
        )
        self._transcript = (
            verbatim.Transcript(transcription.text) if self._pages else None
        )
        self._printed = transcription.printed_pages() if self._pages else {}
        self._found: dict[str, tuple[str | None, str | None]] = {}

    def __call__(self, evidence: str) -> tuple[str | None, str | None]:
        if evidence not in self._found:
            self._found[evidence] = self._page(evidence)
        return self._found[evidence]

    def _page(self, evidence: str) -> tuple[str | None, str | None]:
        from yizkr.pipeline.extract import printed_page_number

        if self._transcript is None:
            return None, None
        number = self._transcript.locate(evidence)
        if number is None:
            return None, None
        page = self._transcript.page(number, self._pages)
        if page is None:
            return None, None
        return str(page.page), printed_page_number(
            str(page.page), self._printed
        )


def evidence_page(
    transcription, evidence: str
) -> tuple[str | None, str | None]:
    """The page of `transcription` the first line of `evidence` lies on
    (EvidencePages)."""
    return EvidencePages(transcription)(evidence)
