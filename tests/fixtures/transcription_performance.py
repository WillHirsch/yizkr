"""Fixed fictional inputs for measuring transcription validation."""

from dataclasses import dataclass

from tests.fixtures import example_universe as universe

from yizkr.pipeline.transcription_record import (
    RecordPage,
    TranscriptionRecord,
    record_text,
)


@dataclass(frozen=True)
class Workload:
    name: str
    arguments: dict
    expected: dict


def workloads() -> list[Workload]:
    """Build inputs before timing; expected fields assert meaningful work."""
    sources = universe.by_key()
    letters = [
        sources[key]
        for key in (
            "letter-excerpt-1939",
            "letter-to-leopold-1958",
            "letter-to-sipkje-1952",
        )
    ]
    page_count = 48
    record = TranscriptionRecord(
        document_class="letter",
        pages=[
            RecordPage(
                page=number,
                status="transcribed",
                text="\n\n".join([letters[(number - 1) % 3].text] * 4),
                languages=[letters[(number - 1) % 3].lang],
                # The final note exercises traversal through all pages;
                # an engine signature flags a structured reading.
                notes="API Error: 529" if number == page_count else "",
            )
            for number in range(1, page_count + 1)
        ],
    )
    text, _spans = record_text(record.pages)
    return [
        Workload(
            "repeated-list",
            {"text": "first and " * 20_000 + "x"},
            {
                "passed": True,
                "flagged": False,
                "failure": None,
                "signature": None,
                "pages": None,
                "record_pages": None,
            },
        ),
        Workload(
            "repeated-announcement",
            {"text": "Let me read the image, " * 20_000 + "x"},
            {
                "passed": False,
                "flagged": False,
                "failure": "tool-failure",
                "signature": "narration-only",
                "pages": None,
                "record_pages": None,
            },
        ),
        Workload(
            "structured-pages",
            {
                "text": text,
                "pages": page_count,
                "record": record,
                "expect_record": True,
                "whole_pdf": True,
            },
            {
                "passed": True,
                "flagged": True,
                "failure": None,
                "signature": "engine-error",
                "pages": page_count,
                "record_pages": page_count,
            },
        ),
    ]
