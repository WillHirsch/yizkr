"""GEDCOM adapter: structured genealogy exports -> ClaimBatch (item 13).

Structured exports never ride the generative path (ADR-0006 context):
a GEDCOM file is machine-written, so its facts are extracted
deterministically — exact, complete, free, reproducible. The output is
an ordinary ClaimBatch with an entity manifest (ADR-0007), so
everything downstream is shared: manifest-id validation, reconcile,
human review, and the writer. Nothing here bypasses review.

Source-first conventions carry over unchanged:

* every claim's reference is the GEDCOM artifact item, with the
  record's verbatim GEDCOM lines as the transcription excerpt — the
  human reviewer sees exactly the lines that assert the claim;
* GEDCOM's own uncertainty vocabulary maps to the archive's structural
  uncertainty (docs/MODELING.md): ABT/EST/CAL -> `circa` sourcing
  circumstances; BEF -> `latest date`; AFT -> `earliest date`;
  BET .. AND .. -> both; missing day/month -> reduced precision.
  A date the grammar cannot parse yields NO date claim and a warning —
  never a guess.

Supported today (the fields family-tree exports actually carry):
individuals (names -> label + aliases), birth/death dates and places,
parent-child relations, marriages (date, place) and divorces
(end time). Unsupported tags are counted and reported, not silently
dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline.extract import run_metadata

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

ADAPTER_IDENTIFIER = "gedcom-adapter"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@dataclass
class Record:
    level: int
    tag: str
    value: str
    xref: str | None
    line_number: int
    children: list[Record] = field(default_factory=list)

    def first(self, tag: str) -> Record | None:
        return next((child for child in self.children if child.tag == tag), None)

    def all(self, tag: str) -> list[Record]:
        return [child for child in self.children if child.tag == tag]

    def text(self, tag: str) -> str:
        child = self.first(tag)
        return child.value if child else ""

    def lines(self) -> str:
        """The record's verbatim GEDCOM lines (for excerpts)."""
        own = f"{self.level}"
        own += f" {self.xref}" if self.xref else ""
        own += f" {self.tag}"
        own += f" {self.value}" if self.value else ""
        return "\n".join([own, *(child.lines() for child in self.children)])


_LINE = re.compile(r"^(\d+)\s+(?:(@[^@]+@)\s+)?(\w+)(?:\s(.*))?$")


def parse_gedcom(text: str) -> list[Record]:
    """GEDCOM lines -> level-0 records with nested children.

    CONT/CONC continuations are folded into their parent's value.
    """
    roots: list[Record] = []
    stack: list[Record] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip("﻿").rstrip()
        if not line.strip():
            continue
        match = _LINE.match(line.strip())
        if not match:
            raise ValueError(f"unparseable GEDCOM line {number}: {raw!r}")
        level, xref, tag, value = match.groups()
        level = int(level)
        if tag in ("CONT", "CONC") and stack:
            joiner = "\n" if tag == "CONT" else ""
            stack[-1].value += joiner + (value or "")
            continue
        record = Record(level, tag, value or "", xref, number)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(record)
        else:
            roots.append(record)
        stack.append(record)
    return roots


# ---------------------------------------------------------------------------
# dates: GEDCOM grammar -> TimeValue + uncertainty qualifiers
# ---------------------------------------------------------------------------


def _parse_plain_date(text: str) -> dict | None:
    """'12 AUG 1885' / 'AUG 1885' / '1885' -> flat time value dict."""
    parts = text.strip().upper().split()
    try:
        if len(parts) == 3:
            day, month, year = int(parts[0]), MONTHS[parts[1]], int(parts[2])
            return {"type": "time", "year": year, "month": month, "day": day,
                    "precision": "day"}  # fmt: skip
        if len(parts) == 2:
            month, year = MONTHS[parts[0]], int(parts[1])
            return {"type": "time", "year": year, "month": month, "precision": "month"}
        if len(parts) == 1:
            return {"type": "time", "year": int(parts[0]), "precision": "year"}
    except (KeyError, ValueError):
        return None
    return None


def parse_gedcom_date(text: str) -> tuple[dict | None, list[dict]]:
    """(time value, qualifiers) per the archive's uncertainty rules.

    Returns (None, []) when the grammar doesn't cover the input —
    the caller records a warning; nothing is guessed.
    """
    raw = text.strip()
    upper = raw.upper()
    circa = [
        {
            "property": "sourcing_circumstances",
            "value": {"type": "item", "value": {"kind": "vocab", "key": "circa"}},
        }
    ]
    if upper.startswith(("ABT ", "EST ", "CAL ")):
        value = _parse_plain_date(raw[4:])
        return (value, circa) if value else (None, [])
    if upper.startswith("BEF "):
        value = _parse_plain_date(raw[4:])
        return (value, [{"property": "latest_date", "value": value}]) if value else (
            None,
            [],
        )
    if upper.startswith("AFT "):
        value = _parse_plain_date(raw[4:])
        return (value, [{"property": "earliest_date", "value": value}]) if value else (
            None,
            [],
        )
    between = re.fullmatch(
        r"(?:BET|FROM)\s+(.+?)\s+(?:AND|TO)\s+(.+)", raw, re.IGNORECASE
    )
    if between:
        earliest = _parse_plain_date(between.group(1))
        latest = _parse_plain_date(between.group(2))
        if earliest and latest:
            return earliest, [
                {"property": "earliest_date", "value": earliest},
                {"property": "latest_date", "value": latest},
            ]
        return None, []
    value = _parse_plain_date(raw)
    return (value, []) if value else (None, [])


# ---------------------------------------------------------------------------
# adaptation
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    """GEDCOM NAME ('Samuel /Genserowski/') -> display form."""
    return re.sub(r"\s+", " ", name.replace("/", " ")).strip()


@dataclass
class AdapterResult:
    batch: ClaimBatch
    warnings: list[str]
    unsupported_tags: dict[str, int]


def adapt_gedcom(text: str, artifact_qid: str) -> AdapterResult:
    """The whole file -> one ClaimBatch with entity manifest."""
    roots = parse_gedcom(text)
    individuals = {r.xref: r for r in roots if r.tag == "INDI" and r.xref}
    families = {r.xref: r for r in roots if r.tag == "FAM" and r.xref}
    warnings: list[str] = []
    unsupported: dict[str, int] = {}

    HANDLED_INDI = {"NAME", "SEX", "BIRT", "DEAT", "FAMC", "FAMS"}
    HANDLED_FAM = {"HUSB", "WIFE", "CHIL", "MARR", "DIV"}
    for record in individuals.values():
        for child in record.children:
            if child.tag not in HANDLED_INDI:
                unsupported[child.tag] = unsupported.get(child.tag, 0) + 1
    for record in families.values():
        for child in record.children:
            if child.tag not in HANDLED_FAM:
                unsupported[child.tag] = unsupported.get(child.tag, 0) + 1

    entities: list[dict] = []
    claims: list[dict] = []
    place_ids: dict[str, str] = {}

    def source(record: Record) -> dict:
        # monolingual text values forbid newlines; keep each GEDCOM line
        # verbatim and join visibly
        excerpt = record.lines().replace("\n", " | ")
        if len(excerpt) > 400:
            excerpt = excerpt[:397].rsplit(" | ", 1)[0] + " | …"
        return {
            "artifact": {"kind": "qid", "qid": artifact_qid},
            "transcription_excerpt": {"text": excerpt, "language": "en"},
        }

    def person_id(xref: str) -> str:
        return xref.strip("@").lower()

    def place_entity(name: str) -> str:
        if name not in place_ids:
            place_ids[name] = f"pl{len(place_ids) + 1}"
            entities.append(
                {
                    "id": place_ids[name],
                    "label": name,
                    "kind": "place",
                    "summary": "place named in the GEDCOM export",
                }
            )
        return place_ids[name]

    def local(entity_id: str) -> dict:
        return {"kind": "local", "local_id": entity_id}

    def claim(subject_id: str, prop: str, value: dict, record: Record, quals=()):
        claims.append(
            {
                "subject": local(subject_id),
                "property": prop,
                "value": value,
                "qualifiers": list(quals),
                "source": source(record),
                "confidence": 1.0,
            }
        )

    def event_claims(indi_xref: str, event: Record, date_prop: str, place_prop: str):
        date_record = event.first("DATE")
        if date_record:
            value, quals = parse_gedcom_date(date_record.value)
            if value is None:
                warnings.append(
                    f"line {date_record.line_number}: unparsed date "
                    f"{date_record.value!r} — no {date_prop} claim emitted"
                )
            else:
                claim(person_id(indi_xref), date_prop, value, event, quals)
        place = event.text("PLAC")
        if place:
            claim(
                person_id(indi_xref),
                place_prop,
                {"type": "item", "value": local(place_entity(place))},
                event,
            )

    # individuals -> entities + birth/death claims
    for xref, record in individuals.items():
        names = [_clean_name(name.value) for name in record.all("NAME") if name.value]
        if not names:
            warnings.append(f"{xref}: no NAME; skipped entirely")
            continue
        birth, death = record.first("BIRT"), record.first("DEAT")
        summary_bits = []
        if birth and birth.text("DATE"):
            summary_bits.append(f"b. {birth.text('DATE')}")
        if death and death.text("DATE"):
            summary_bits.append(f"d. {death.text('DATE')}")
        entities.append(
            {
                "id": person_id(xref),
                "label": names[0],
                "aliases": names[1:],
                "kind": "human",
                "summary": "; ".join(summary_bits),
                "mention_lines": [record.line_number],
            }
        )
        if birth:
            event_claims(xref, birth, "date_of_birth", "place_of_birth")
        if death:
            event_claims(xref, death, "date_of_death", "place_of_death")

    # families -> parentage and spouse claims
    for fam in families.values():
        husband = fam.text("HUSB")
        wife = fam.text("WIFE")
        for child in fam.all("CHIL"):
            for parent_xref, prop in ((husband, "father"), (wife, "mother")):
                if parent_xref in individuals and child.value in individuals:
                    claim(
                        person_id(child.value),
                        prop,
                        {"type": "item", "value": local(person_id(parent_xref))},
                        fam,
                    )
        if husband in individuals and wife in individuals:
            quals = []
            marriage = fam.first("MARR")
            if marriage:
                date_record = marriage.first("DATE")
                if date_record:
                    value, date_quals = parse_gedcom_date(date_record.value)
                    if value is None:
                        warnings.append(
                            f"line {date_record.line_number}: unparsed marriage "
                            f"date {date_record.value!r} — start_time omitted"
                        )
                    else:
                        quals.append({"property": "start_time", "value": value})
                        quals.extend(date_quals)
                place = marriage.text("PLAC")
                if place:
                    quals.append(
                        {
                            "property": "location",
                            "value": {"type": "item", "value": local(place_entity(place))},
                        }
                    )
            divorce = fam.first("DIV")
            if divorce:
                date_record = divorce.first("DATE")
                value = None
                if date_record:
                    value, _ = parse_gedcom_date(date_record.value)
                if value is not None:
                    quals.append({"property": "end_time", "value": value})
                elif date_record is not None:
                    warnings.append(
                        f"line {date_record.line_number}: unparsed divorce "
                        f"date {date_record.value!r} — end_time omitted"
                    )
            for subject, other in ((husband, wife), (wife, husband)):
                claim(
                    person_id(subject),
                    "spouse",
                    {"type": "item", "value": local(person_id(other))},
                    fam,
                    quals,
                )

    batch = ClaimBatch.model_validate(
        {
            "extraction_run": run_metadata(ADAPTER_IDENTIFIER).model_dump(mode="json"),
            "claims": claims,
            "entities": entities,
        }
    )
    problems = validate_against_manifest(batch)
    if problems:
        # deterministic code emitting invalid claims is a bug, not input noise
        raise ValueError(
            "gedcom adapter produced invalid claims:\n  " + "\n  ".join(problems)
        )
    return AdapterResult(batch=batch, warnings=warnings, unsupported_tags=unsupported)
