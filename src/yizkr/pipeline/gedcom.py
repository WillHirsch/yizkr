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
individuals (names -> label + aliases, PLUS typed referenced name
claims: NAME TYPE birth/maiden/married/aka/official, the NICK subtag,
the non-standard _MARNM tag, and the universal inline convention where a
quoted or parenthetical token is a nickname — "Frederick (Fred) George",
"Gerhard 'Bubi' Weissrock"; docs/MODELING.md "Names"), birth/death dates
and places,
cause of death (DEAT.CAUS, verbatim), burials (BURI place, dated when
the record is), occupations (OCCU, minted as occupation items with the
source's wording), number of children (NCHI), residences (place, dated
when the record is), parent-child relations, marriages (date, place)
and divorces (end time). Sibling relations are deliberately NOT
derived from shared parents: the export asserts parentage, not
siblinghood (docs/MODELING.md — never derive). Dates accept the
GEDCOM-standard English abbreviations plus what hand-maintained trees
actually contain: full English and German month names, and month-first
order when the month is named. Unsupported tags are counted and
reported, not silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline.extract import run_metadata

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    # real exports carry hand-typed dates beyond the GEDCOM standard's
    # English abbreviations; full names are just as unambiguous
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPT": 9, "SEPTEMBER": 9, "OCTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
    # German month names (they name the month; nothing is guessed)
    "JANUAR": 1, "FEBRUAR": 2, "MÄRZ": 3, "MAERZ": 3, "MAI": 5,
    "JUNI": 6, "JULI": 7, "OKTOBER": 10, "DEZEMBER": 12,
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
        return next(
            (child for child in self.children if child.tag == tag), None
        )

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
    """'12 AUG 1885' / 'AUG 1885' / '1885' -> flat time value dict.

    Month-first order ('January 1 1860') is accepted too: the month is
    named, not numbered, so there is nothing ambiguous to guess.
    """
    parts = text.strip().upper().split()
    try:
        if len(parts) == 3:
            if parts[0] in MONTHS:
                month, day, year = (
                    MONTHS[parts[0]],
                    int(parts[1]),
                    int(parts[2]),
                )
            else:
                day, month, year = (
                    int(parts[0]),
                    MONTHS[parts[1]],
                    int(parts[2]),
                )
            return {"type": "time", "year": year, "month": month, "day": day,
                    "precision": "day"}  # fmt: skip
        if len(parts) == 2:
            month, year = MONTHS[parts[0]], int(parts[1])
            return {
                "type": "time",
                "year": year,
                "month": month,
                "precision": "month",
            }
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
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "circa"},
            },
        }
    ]
    if upper.startswith(("ABT ", "EST ", "CAL ")):
        value = _parse_plain_date(raw[4:])
        return (value, circa) if value else (None, [])
    if upper.startswith("BEF "):
        value = _parse_plain_date(raw[4:])
        return (
            (value, [{"property": "latest_date", "value": value}])
            if value
            else (
                None,
                [],
            )
        )
    if upper.startswith("AFT "):
        value = _parse_plain_date(raw[4:])
        return (
            (value, [{"property": "earliest_date", "value": value}])
            if value
            else (
                None,
                [],
            )
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


# GEDCOM NAME TYPE (5.5.1 §, plus common tool values) -> typed-name
# property. Only types the source states are recorded; an absent or
# unrecognised TYPE leaves the name as a plain label/alias, never guessed.
_NAME_TYPE_PROPERTY = {
    "birth": "birth_name",
    "maiden": "birth_name",
    "married": "married_name",
    "marriage": "married_name",
    "aka": "nickname",
    "nick": "nickname",
    "nickname": "nickname",
    "official": "official_name",
    "legal": "official_name",
    "change": "official_name",
    "immigrant": "official_name",
    "adopted": "official_name",
}

# a quoted or single-word parenthetical token inside a name is the near-
# universal genealogy convention for a nickname ("Frederick (Fred) George",
# "Gerhard 'Bubi' Weissrock") — the only way this data expresses them.
_QUOTED_NAME = re.compile(r"[‘'\"“]([^‘’'\"“”]+)[’'\"”]")
_PAREN_NAME = re.compile(r"\(([^)]+)\)")


def _inline_nicknames(name: str) -> list[str]:
    """Quoted / parenthetical nickname tokens within a display name."""
    found = [m.group(1).strip() for m in _QUOTED_NAME.finditer(name)]
    found += [m.group(1).strip() for m in _PAREN_NAME.finditer(name)]
    # conservative: a real nickname is short and not a date/note like
    # "(born 1900)" — at most two words, no digits
    return [
        token
        for token in found
        if token
        and len(token.split()) <= 2
        and not any(c.isdigit() for c in token)
    ]


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

    HANDLED_INDI = {
        "NAME", "_MARNM", "SEX", "BIRT", "DEAT", "BURI", "OCCU", "NCHI",
        "RESI", "FAMC", "FAMS",
    }  # fmt: skip
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
    occupation_ids: dict[str, str] = {}

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

    def occupation_entity(name: str) -> str:
        if name not in occupation_ids:
            occupation_ids[name] = f"oc{len(occupation_ids) + 1}"
            entities.append(
                {
                    "id": occupation_ids[name],
                    "label": name,
                    "kind": "occupation_class",
                    "summary": "occupation named in the GEDCOM export",
                }
            )
        return occupation_ids[name]

    def local(entity_id: str) -> dict:
        return {"kind": "local", "local_id": entity_id}

    def claim(
        subject_id: str, prop: str, value: dict, record: Record, quals=()
    ):
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

    def dated_qualifiers(event: Record, context: str) -> list[dict]:
        """point_in_time (+uncertainty) qualifiers from an event's DATE."""
        quals: list[dict] = []
        date_record = event.first("DATE")
        if date_record:
            value, date_quals = parse_gedcom_date(date_record.value)
            if value is None:
                warnings.append(
                    f"line {date_record.line_number}: unparsed {context} "
                    f"date {date_record.value!r} — point_in_time omitted"
                )
            else:
                quals.append({"property": "point_in_time", "value": value})
                quals.extend(date_quals)
        return quals

    def event_claims(
        indi_xref: str, event: Record, date_prop: str, place_prop: str
    ):
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

    def monolingual(text: str) -> dict:
        return {
            "type": "monolingual",
            "value": {"text": text, "language": "en"},
        }

    def name_claims(xref: str, record: Record) -> None:
        """Typed, referenced name claims (docs/MODELING.md "Names").

        The item's label/aliases remain the flat match index; this adds
        the source's OWN name typing (NAME TYPE, NICK, _MARNM) plus the
        universal inline-nickname convention, each attributed to the
        GEDCOM record that states it.
        """
        emitted_nicknames: set[str] = set()

        def add_nickname(text: str, name_record: Record) -> None:
            if text and text not in emitted_nicknames:
                emitted_nicknames.add(text)
                claim(
                    person_id(xref), "nickname", monolingual(text), name_record
                )

        for name_record in record.all("NAME"):
            if not name_record.value:
                continue
            cleaned = _clean_name(name_record.value)
            prop = _NAME_TYPE_PROPERTY.get(
                name_record.text("TYPE").strip().lower()
            )
            if prop == "nickname":
                add_nickname(
                    cleaned, name_record
                )  # the whole aka IS the nickname
            elif prop:
                claim(person_id(xref), prop, monolingual(cleaned), name_record)
            add_nickname(name_record.text("NICK").strip(), name_record)
            for inline in _inline_nicknames(cleaned):
                add_nickname(inline, name_record)
        # non-standard married-name tag some tools emit at the INDI level
        for marnm in record.all("_MARNM"):
            if marnm.value:
                claim(
                    person_id(xref),
                    "married_name",
                    monolingual(_clean_name(marnm.value)),
                    marnm,
                )

    # individuals -> entities + birth/death claims
    for xref, record in individuals.items():
        names = [
            _clean_name(name.value)
            for name in record.all("NAME")
            if name.value
        ]
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
        name_claims(xref, record)
        # SEX -> sex_or_gender (the kinship calculus needs it for gendered
        # terms, ADR-0014); only the unambiguous M/F are mapped
        sex = record.text("SEX").strip().upper()
        sex_value = {"M": "male", "F": "female"}.get(sex)
        if sex_value:
            claim(
                person_id(xref),
                "sex_or_gender",
                {"type": "item", "value": {"kind": "vocab", "key": sex_value}},
                record.first("SEX"),
            )
        if birth:
            event_claims(xref, birth, "date_of_birth", "place_of_birth")
        if death:
            event_claims(xref, death, "date_of_death", "place_of_death")
            # cause of death stays verbatim prose (docs/MODELING.md);
            # "en" matches the export's language, like the excerpts
            cause = death.text("CAUS")
            if cause:
                claim(
                    person_id(xref),
                    "cause_of_death",
                    {
                        "type": "monolingual",
                        "value": {"text": cause, "language": "en"},
                    },
                    death,
                )
        for burial in record.all("BURI"):
            place = burial.text("PLAC")
            if not place:
                warnings.append(
                    f"line {burial.line_number}: BURI without PLAC — "
                    "no burial claim emitted"
                )
                continue
            claim(
                person_id(xref),
                "burial_place",
                {"type": "item", "value": local(place_entity(place))},
                burial,
                dated_qualifiers(burial, "burial"),
            )
        for occupation in record.all("OCCU"):
            name = occupation.value.strip()
            if not name:
                warnings.append(
                    f"line {occupation.line_number}: OCCU without a value — "
                    "no occupation claim emitted"
                )
                continue
            quals = dated_qualifiers(occupation, "occupation")
            place = occupation.text("PLAC")
            if place:
                quals.append(
                    {
                        "property": "location",
                        "value": {
                            "type": "item",
                            "value": local(place_entity(place)),
                        },
                    }
                )
            claim(
                person_id(xref),
                "occupation",
                {"type": "item", "value": local(occupation_entity(name))},
                occupation,
                quals,
            )
        nchi = record.first("NCHI")
        if nchi:
            try:
                count = int(nchi.value.strip())
            except ValueError:
                warnings.append(
                    f"line {nchi.line_number}: NCHI {nchi.value!r} is not a "
                    "number — no number_of_children claim emitted"
                )
            else:
                claim(
                    person_id(xref),
                    "number_of_children",
                    {"type": "quantity", "amount": count},
                    nchi,
                )
        for residence in record.all("RESI"):
            place = residence.text("PLAC")
            if not place:
                warnings.append(
                    f"line {residence.line_number}: RESI without PLAC — "
                    "no residence claim emitted"
                )
                continue
            claim(
                person_id(xref),
                "residence",
                {"type": "item", "value": local(place_entity(place))},
                residence,
                dated_qualifiers(residence, "residence"),
            )

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
                        {
                            "type": "item",
                            "value": local(person_id(parent_xref)),
                        },
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
                            f"line {date_record.line_number}: unparsed "
                            f"marriage date {date_record.value!r} "
                            "— start_time omitted"
                        )
                    else:
                        quals.append(
                            {"property": "start_time", "value": value}
                        )
                        quals.extend(date_quals)
                place = marriage.text("PLAC")
                if place:
                    quals.append(
                        {
                            "property": "location",
                            "value": {
                                "type": "item",
                                "value": local(place_entity(place)),
                            },
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
            "extraction_run": run_metadata(ADAPTER_IDENTIFIER).model_dump(
                mode="json"
            ),
            "claims": claims,
            "entities": entities,
        }
    )
    problems = validate_against_manifest(batch)
    if problems:
        # deterministic code emitting invalid claims is a bug, not input noise
        raise ValueError(
            "gedcom adapter produced invalid claims:\n  "
            + "\n  ".join(problems)
        )
    return AdapterResult(
        batch=batch, warnings=warnings, unsupported_tags=unsupported
    )
