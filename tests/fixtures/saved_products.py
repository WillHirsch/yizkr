"""One fictional document's saved views, made by the production scheduler.

Fixed authored answers stand in for the vision reader, extractor and critic.
Discovery, stage execution, publication and the state and dependency records
are the scratch driver's own, so the views are the ones a run saves. The
answers exercise the real parsers, not model quality; no provider is called.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageDraw
from tests.fixtures import example_universe as u
from tests.fixtures.document_processing import IsolatedDriver, quiet
from tests.fixtures.records import record_answer

from yizkr.pipeline.document_state import DocumentRun

GRAVESTONE = next(
    source for source in u.SOURCES if source.key == "gravestone-photo-1956"
)
GRAVESTONE_FILE = f"{GRAVESTONE.key}.png"
# Eugen's birth, cited by the inscription's dates, which it gives once
EUGEN_BIRTH = next(
    assertion
    for assertion in u.ASSERTIONS
    if (assertion.source, assertion.subject, assertion.property)
    == (GRAVESTONE.key, "eugen-loebig", "date_of_birth")
)


class AuthoredAnswers:
    """One role's fixed answers, given in the order its stage asks; the
    last is given again to any retry."""

    model_identifier = "authored-fictional-document"

    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = list(answers)

    def complete(self, prompt, image_paths=()):
        return (
            self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        )

    def reject(self, response, reason):
        return False


class AuthoredDriver(IsolatedDriver):
    """The scratch scheduler, each model role answered from a fixed list."""

    def __init__(self, folder, state_dir, *, answers, **kwargs):
        kwargs.setdefault("log", quiet)
        super().__init__(folder, state_dir, **kwargs)
        self.answers = {
            role: AuthoredAnswers(listed) for role, listed in answers.items()
        }

    def role_client(self, role, *, events=None):
        return self.answers[role]


def gravestone_image() -> bytes:
    """A PNG with the fictional gravestone's inscription drawn on it."""
    image = Image.new("RGB", (260, 120), "white")
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(GRAVESTONE.text.splitlines()):
        draw.text((12, 8 + 21 * row), line, fill="black")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def claim_answer(*claims: dict, finished: bool = True) -> str:
    """A chunk's answer: one claim per line, then its terminal sentinel."""
    lines = [json.dumps(claim, ensure_ascii=False) for claim in claims]
    return "\n".join(lines + (['{"done":true}'] if finished else []))


def gravestone_claim(sha1: str, excerpt: str = EUGEN_BIRTH.excerpt) -> dict:
    """Eugen's birth year, cited by `excerpt` of the gravestone's reading."""
    return {
        "subject": {
            "kind": "local",
            "local_id": "eugen",
            "label": "EUGEN LÖBIG",
        },
        "property": "date_of_birth",
        "value": {"type": "time", "year": EUGEN_BIRTH.value.year},
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        },
    }


def gravestone_answers(
    sha1: str,
    *,
    claims: Sequence[dict] | None = None,
    critique: str = '{"done":true}',
) -> dict[str, list[str]]:
    """The reader's record of the inscription, a one-entity extraction of
    Eugen and the critic's answer."""
    manifest = {
        "entities": [
            {
                "id": "eugen",
                # the name as the inscription gives it, which locates the
                # mention in the reading
                "label": "EUGEN LÖBIG",
                "kind": "human",
                "mention_lines": [2],
            }
        ],
        "preparation": None,
    }
    return {
        "transcribe": [
            record_answer(
                GRAVESTONE.text,
                document_class="photograph",
                languages=("de",),
            )
        ],
        "extract": [
            json.dumps(manifest, ensure_ascii=False),
            claim_answer(*(claims or (gravestone_claim(sha1),))),
        ],
        "critique": [critique],
    }


def saved_document(
    directory: Path,
    name: str,
    data: bytes,
    answers: Mapping[str, Sequence[str]],
    **options,
) -> tuple[int, DocumentRun]:
    """Run the scheduler through critique over a folder holding `data` as
    `name`, in `directory`; (its exit status, the saved document). Running
    again with the same `directory` resumes that run's saved views."""
    folder = directory / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / name
    if not source.exists() or source.read_bytes() != data:
        source.write_bytes(data)
    options.setdefault("language", "de")
    driver = AuthoredDriver(
        folder, directory / "run", answers=answers, **options
    )
    status = driver.run()
    (doc,) = (doc for doc in driver.documents() if doc.source == source)
    return status, doc
