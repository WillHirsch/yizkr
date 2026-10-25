"""A document's statement about how its own text was prepared, read once
per document, and the machine-assisted marker it puts on the claims read
from that text (ADR-0008, "Machine-assisted preparation is a sourcing
circumstance").

The entity manifest, the one call of an extraction that reads the whole
transcription (ADR-0007), also reads it for one more thing
(`MANIFEST_BLOCK`): a statement in which the document says that its own
text, or part of it, was prepared with machine assistance (that its
translations were made with translation software, say). Its answer
carries a `preparation` key beside its entities, which quotes the
statement and names what it covers: all of the document's own text, or
the passages it names, each by its first and last line. A curator may
point to the statement (`preparation.pointer.txt` in the document's
directory, the statement's words as the transcription holds them): the
manifest then reads that passage only. The pointer says where the
document speaks; whether it speaks of machine assistance, and of what,
is still read from the document's words, so the marker is never a
curator's own assertion cited to the document (CLAUDE.md rule 1).

The marker is then set by the pipeline, never by the extractor: every
claim whose excerpt the transcription holds within the lines the
statement covers carries `sourcing_circumstances` -> `machine_assisted`,
and every claim of the batch does when the statement covers all of the
document's own text. The prompts never offer the item
(`ManifestItem.offered`), and a marker a chunk answer gives is dropped
before the merge, and counted. Nothing is inferred from how a text reads
(CLAUDE.md rule 5): a document that says nothing of its making gives no
marker. Each document is its own witness with its own batch, so a
compilation's statement never marks the claims of an excerpt it embeds
(ADR-0008).

A manifest answer without the key, or an extraction that fell back from
the manifest, leaves the reading unread: no claim is marked. What the
reading found and how many claims it marked are counted in the
extraction's coverage (`machine_assisted`), and the statement it quoted
is kept in the local side output `preparation.json`, never in a report.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ItemValue,
    MonolingualText,
    Qualifier,
    SourceReference,
    VocabRef,
)
from yizkr.core.verbatim import Transcript
from yizkr.pipeline.prompts import load_prompt

# the manifest item the marker names, as the sourcing circumstances
# qualifier's value
MACHINE_ASSISTED = "machine_assisted"
QUALIFIER = "sourcing_circumstances"
# the file in a document's directory in which a curator points to the
# document's statement about its preparation, by its words
POINTER = "preparation.pointer.txt"
# the key of the manifest answer that carries the reading
KEY = "preparation"

# Appended to the entity manifest's prompt, after the transcription, when
# the extraction reads the statement.
MANIFEST_BLOCK = "\n" + load_prompt("preparation-manifest.md")

POINTER_BLOCK = "\n" + load_prompt("preparation-pointer.md")


def manifest_block(pointer: str | None = None) -> str:
    """The block the entity manifest's prompt ends with, naming the
    passage a curator pointed to when one did."""
    return MANIFEST_BLOCK.format(
        pointer=POINTER_BLOCK.format(pointer=pointer.strip())
        if pointer is not None
        else ""
    )


class Passage(BaseModel):
    """The lines of a passage a statement names, from 1, both included."""

    model_config = ConfigDict(extra="forbid")

    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)


class Reading(BaseModel):
    """What the reading found: the statement, verbatim, or None; the
    lines it stands on; and what it covers, "all" or its passages."""

    model_config = ConfigDict(extra="forbid")

    statement: str | None = None
    lines: tuple[int, int] | None = None
    covers: Literal["all"] | list[Passage] | None = None
    # the words a curator pointed to, when a curator did
    pointed: str | None = None


def pointed_lines(
    pointer: str | None, transcript: Transcript
) -> tuple[int, int] | None:
    """The lines that hold the words a curator pointed to; None for no
    pointer. A pointer the transcription does not hold verbatim
    (core/verbatim.py) raises ValueError: it names the document's words,
    or nothing."""
    if pointer is None:
        return None
    lines = transcript.span(pointer)
    if lines is None:
        raise ValueError(
            f"the statement a curator pointed to ({POINTER}) is not in the "
            f"transcription"
        )
    return lines


def parse(
    answer: Any,
    transcript: Transcript,
    line_count: int,
    pointer: str | None = None,
) -> tuple[Reading | None, list[str]]:
    """(reading, problems) from the manifest answer's `preparation`
    value; the reading is None when it cannot be used. A statement the
    transcription does not hold verbatim (core/verbatim.py), a passage
    outside its lines or one whose first line follows its last, and,
    where a curator pointed to the statement (`pointer`), a statement
    that does not occur wholly within the pointer's words, compared as
    core/verbatim.py compares an excerpt with its transcription, are each
    a problem that makes it unusable."""
    pointed_lines(pointer, transcript)
    if not isinstance(answer, dict) or "statement" not in answer:
        return None, ["'preparation' is no object with a 'statement' key"]
    unknown = sorted(set(answer) - {"statement", "covers"})
    if unknown:
        return None, [f"unknown keys: {', '.join(unknown)}"]
    statement = answer["statement"]
    if statement is None:
        return Reading(pointed=pointer and pointer.strip()), []
    if not isinstance(statement, str) or not statement.strip():
        return None, ["'statement' is neither a quote nor null"]
    lines = transcript.span(statement)
    if lines is None:
        return None, ["the statement is not in the transcription"]
    if pointer is not None and Transcript(pointer).span(statement) is None:
        return None, [
            "the statement is not the passage the curator pointed to"
        ]
    covers = answer.get("covers")
    passages: list[Passage] = []
    if covers != "all":
        if not isinstance(covers, list) or not covers:
            return None, ["'covers' is neither \"all\" nor a list of passages"]
        for passage in covers:
            try:
                read = Passage.model_validate(passage)
            except ValueError:
                return None, [f"a passage is malformed: {passage!r}"]
            if read.first_line > read.last_line or (
                read.last_line > line_count
            ):
                return None, [
                    f"lines {read.first_line}-{read.last_line} are no "
                    f"passage of the transcription's {line_count} lines"
                ]
            passages.append(read)
    return (
        Reading(
            statement=statement,
            lines=lines,
            covers="all" if covers == "all" else passages,
            pointed=pointer and pointer.strip(),
        ),
        [],
    )


def _marker(qualifier: Any) -> bool:
    """Whether a qualifier, as a chunk answer gives it, is the marker."""
    if not isinstance(qualifier, dict):
        return False
    if qualifier.get("property") != QUALIFIER:
        return False
    value = qualifier.get("value")
    if isinstance(value, dict) and value.get("type") == "item":
        value = value.get("value")
    return isinstance(value, dict) and value.get("key") == MACHINE_ASSISTED


def drop_markers(claims: Iterable[dict]) -> tuple[list[dict], int]:
    """The claims of chunk answers without any marker they give, and the
    number of markers dropped: the marker is the reading's to set."""
    kept: list[dict] = []
    dropped = 0
    for claim in claims:
        qualifiers = (
            claim.get("qualifiers") if isinstance(claim, dict) else None
        )
        if isinstance(qualifiers, list) and any(map(_marker, qualifiers)):
            dropped += sum(map(_marker, qualifiers))
            claim = claim | {
                "qualifiers": [q for q in qualifiers if not _marker(q)]
            }
        kept.append(claim)
    return kept, dropped


def _within(span: tuple[int, int], passages: Sequence[Passage]) -> bool:
    return any(
        passage.first_line <= span[0] and span[1] <= passage.last_line
        for passage in passages
    )


def statement_source(
    reading: Reading | None, transcription: Any, artifact: ArtifactRef
) -> SourceReference | None:
    """Where the document makes the statement the reading quoted: the
    document, with the page its first line lies on and the number printed
    there, where the transcription's pages give them
    (places.evidence_page), and the statement as its excerpt, in the
    transcription's language. None when the reading quoted none."""
    from yizkr.pipeline.places import evidence_page

    if reading is None or reading.statement is None:
        return None
    page, printed = evidence_page(transcription, reading.statement)
    return SourceReference(
        artifact=artifact,
        page_number=page,
        printed_page_number=printed,
        transcription_excerpt=MonolingualText(
            text=reading.statement, language=transcription.language
        ),
    )


def mark(
    claims: Sequence[Claim],
    reading: Reading | None,
    transcript: Transcript,
    stated: SourceReference | None = None,
) -> tuple[list[Claim], dict[str, int]]:
    """The claims, each read from the text the reading's statement
    covers carrying the marker, beside its other qualifiers, and the
    statement's own source, `stated` (statement_source), as its
    `circumstance_source`, its own source unchanged; and the counts:
    `marked`; `straddling`, claims whose excerpt occurs both within and
    outside the passages, which are not marked; and `unlocated`, claims
    with no excerpt or one the transcription does not hold, which are not
    marked when the statement names passages. Under a statement that
    covers all of the document's own text every claim is marked."""
    counts = {"marked": 0, "straddling": 0, "unlocated": 0}
    if reading is None or reading.statement is None:
        return list(claims), counts
    marker = Qualifier(
        property=QUALIFIER,
        value=ItemValue(value=VocabRef(key=MACHINE_ASSISTED)),
    )
    marked: list[Claim] = []
    for claim in claims:
        if reading.covers == "all":
            inside = True
        else:
            excerpt = claim.source.transcription_excerpt
            spans = transcript.spans(excerpt.text) if excerpt else []
            within = [_within(span, reading.covers) for span in spans]
            inside = bool(within) and all(within)
            if not within:
                counts["unlocated"] += 1
            elif any(within) and not inside:
                counts["straddling"] += 1
        if inside:
            counts["marked"] += 1
            claim = claim.model_copy(
                update={
                    "qualifiers": [*claim.qualifiers, marker],
                    "circumstance_source": stated,
                }
            )
        marked.append(claim)
    return marked, counts


def coverage(
    reading: Reading | None, counts: dict[str, int], dropped: int
) -> dict[str, Any]:
    """The reading as the extraction's coverage counts it: whether it was
    read, whether a curator pointed to the statement, whether there was
    one and what it covers, with the claims marked, those not marked for
    want of a place within or outside its passages, and the markers chunk
    answers gave that were dropped."""
    found = reading is not None and reading.statement is not None
    return {
        "read": reading is not None,
        "pointed": reading is not None and reading.pointed is not None,
        "statement": found,
        "covers": None
        if not found
        else "all"
        if reading.covers == "all"
        else "passages",
        "passages": len(reading.covers)
        if found and isinstance(reading.covers, list)
        else 0,
        **counts,
        "dropped": dropped,
    }


def attention(
    reading: Reading | None, reasons: Sequence[str], counts: dict[str, int]
) -> list[str]:
    """What a reader of the batch should know of the reading: why it
    could not be read, where there is a reason (an unread reading without
    one is counted in the coverage alone); that a statement a curator
    pointed to was read as no statement of machine assistance; or how
    many claims could not be placed within or outside the passages its
    statement covers."""
    if reading is None:
        if not reasons:
            return []
        return [
            "machine-assisted preparation unread, no claim marked: "
            + reasons[-1]
        ]
    if reading.statement is None:
        if reading.pointed is not None:
            return [
                "the statement a curator pointed to was read as saying "
                "nothing of machine assistance: no claim marked"
            ]
        return []
    unplaced = counts["straddling"] + counts["unlocated"]
    if reading.covers != "all" and unplaced:
        plural = "" if unplaced == 1 else "s"
        return [
            f"{unplaced} claim{plural} not placed within or outside the "
            f"passages the machine-assisted statement covers: not marked"
        ]
    return []


def side_output(reading: Reading | None) -> dict[str, Any] | None:
    """The local record of the statement the reading quoted, for
    `preparation.json`; None when there was none. It quotes the document,
    so it stays beside the batch, never in a report."""
    if reading is None or reading.statement is None:
        return None
    return {
        "statement": reading.statement,
        "lines": list(reading.lines) if reading.lines else None,
        "covers": reading.covers
        if reading.covers == "all"
        else [passage.model_dump() for passage in reading.covers or ()],
        "pointed": reading.pointed,
    }
