"""GEDCOM adapter: structured genealogy exports -> ClaimBatch.

Structured exports never ride the generative path (ADR-0006 context):
a GEDCOM file is machine-written, so its facts are extracted
deterministically — exact, complete, free, reproducible. The output is
an ordinary ClaimBatch with an entity manifest (ADR-0007), so
everything downstream is shared: manifest-id validation, reconcile,
human review, and the writer. Nothing here bypasses review.

Source-first conventions carry over unchanged:

* every claim's reference is the GEDCOM export itself, cited by the
  SHA-1 of its bytes (ADR-0010; the writer resolves it to the artifact
  item), with the record's verbatim GEDCOM lines as the transcription
  excerpt — the human reviewer sees exactly the lines that assert the
  claim;
* GEDCOM's own uncertainty vocabulary maps to the archive's structural
  uncertainty (docs/MODELING.md): ABT/EST/CAL -> `circa` sourcing
  circumstances; BEF -> `latest date`; AFT -> `earliest date`;
  BET .. AND .. -> both; missing day/month -> reduced precision. A
  marriage's or a divorce's range bounds the one end of the spouse
  statement it dates: a marriage's BEF is its `latest start date`, a
  divorce's AFT its `earliest end date` (docs/MODELING.md, "Marriages,
  residences and other periods"). A residence's or an occupation's
  date period (FROM x TO y, FROM x, TO y) is its statement's start and
  end time, each dated by its own phrase, which may be approximate or
  a range of its own: FROM BET 1938 AND 1939 is a start between 1938
  and 1939. Any other date on them is a point in time.
  A date the grammar cannot parse yields NO date claim and a warning —
  never a guess.

Every occurrence of a tag is read, not only the first. A record that
repeats an event (a second BIRT, DEAT, MARR or DIV) or a detail beneath
one (a second DATE, PLAC, CAUS, TYPE or NICK), or that gives a person's
SEX or NCHI twice, states alternatives, and each gives claims of its own,
with its own date, place and circa, so that the alternatives coexist
(ADR-0002): Gustav's birth on 7 JAN 1919 and on 1 JUL 1919 is two date of
birth claims. Where one claim holds both an event's date and its place (a
burial, a residence, an occupation, a marriage), each of the event's dates
is paired with each of its places. A family's first marriage (a date with
a place) and its first divorce date one pair of spouse claims, as a
family's only MARR and DIV do, and every other marriage or divorce dates a
pair of its own. What gives nothing to claim (`1 BIRT Y`, `1 MARR Y`, a
date the grammar cannot parse) never hides what does: it adds no claim
beside one that does, while an undated MARR that gives a place keeps it
on a pair of its own. A claim the batch holds already, from an event
that restates another, is not added again.

Supported (the fields family-tree exports actually carry):
individuals (names -> label + aliases, PLUS typed referenced name
claims: NAME TYPE birth/maiden/married/aka/official, the NICK subtag,
the non-standard _MARNM tag, and the universal inline convention where a
quoted or parenthetical token is a nickname — "Ida (Peppi) Sörne",
"Cäcilie 'Cilli' Tarle"; docs/MODELING.md "Names"), birth/death dates
and places,
cause of death (DEAT.CAUS, verbatim), burials (BURI place, dated when
the record is), occupations (OCCU, minted as occupation items with the
source's wording), number of children (NCHI), residences (place, dated
when the record is), parent-child relations, marriages (the spouse
statements' start time and place) and divorces (their end time). Source
citations (SOUR) are folded into each
claim's reference (ADR-0021): the PAGE locator becomes the reference's
page number and the cited source's own title is surfaced in the excerpt
— the upstream source is resolved, never fetched. Multimedia objects
(OBJE) become lightweight un-held media items (an `artifact`) linked to
the person with `image`; the bytes are referenced, never downloaded from
a third party (privacy rule 7). Sibling relations are deliberately NOT
derived from shared parents: the export asserts parentage, not
siblinghood (docs/MODELING.md — never derive). Dates accept the
GEDCOM-standard English abbreviations plus what hand-maintained trees
actually contain: full English and German month names, and month-first
order when the month is named. Tags the adapter does not map (e.g.
REPO, SUBM, tool-specific `_` tags) are counted and reported, not
silently dropped: the census walks every record and nesting level and
names the top-most unmapped tag in each branch, leaving an already-
handled or already-dropped subtree unitemised (ADR-0021). Whether a tag
is mapped depends on where it stands: a PLAC is read under a marriage
but not under a divorce, and a TYPE under a NAME but not under a MARR,
so such a tag is named with its parent's tag where it goes unread
(DIV.PLAC, MARR.TYPE). A DIV without a DATE is counted as DIV: it says a
marriage ended at an unknown time, and a claim has no unknown value to
state that with. A family has one husband and one wife, so the adapter
reads a family's first HUSB and first WIFE alone, and a further one is
counted with its parent's tag too (FAM.HUSB, FAM.WIFE).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from yizkr.claims import ArtifactRef, ClaimBatch, validate_against_manifest
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
    """'7 JAN 1919' / 'JAN 1919' / '1919' -> flat time value dict.

    Month-first order ('January 7 1919') is accepted too: the month is
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


# The qualifiers that bound each end of a period, as on Wikidata's spouse
# (P26; docs/MODELING.md, "Marriages, residences and other periods"): a
# start runs from its earliest date to its latest start date, and an end
# from its earliest end date to its latest date. parse_gedcom_date names a
# range's bounds earliest_date and latest_date, so each end renames the
# bound that is its own.
PERIOD_BOUNDS = {
    "start_time": {"latest_date": "latest_start_date"},
    "end_time": {"earliest_date": "earliest_end_date"},
}


def period_end(value: dict, date_quals: list[dict], end: str) -> list[dict]:
    """The qualifiers that date one end of a period (`end`: start_time or
    end_time) from a parsed date: the date as that end, and the range it
    states as that end's bounds, never the other end's."""
    bounds = PERIOD_BOUNDS[end]
    return [{"property": end, "value": value}] + [
        {
            **qualifier,
            "property": bounds.get(
                qualifier["property"], qualifier["property"]
            ),
        }
        for qualifier in date_quals
    ]


def split_gedcom_period(text: str) -> tuple[str | None, str | None] | None:
    """A GEDCOM date period's two date phrases: FROM x TO y -> (x, y),
    FROM x -> (x, None) and TO y -> (None, y). None when `text` is no
    period, because it starts with neither FROM nor TO.

    A period says when a state began and when it ended, where a range
    (BET x AND y, BEF x, AFT x) bounds a single date. Each phrase is a
    date of its own, which may itself be approximate or a range; it is
    empty when the period names an end but gives it no date. A "to"
    inside a parenthesised date phrase ("(Aug to Sep)") is the phrase's
    own word, not the period's TO.
    """
    words = text.split()
    keys = [word.upper() for word in words]
    if not keys or keys[0] not in ("FROM", "TO"):
        return None
    if keys[0] == "TO":
        return None, " ".join(words[1:])
    depth = 0  # how deep the word stands in parentheses
    for cut, key in enumerate(keys[1:], start=1):
        if key == "TO" and depth == 0:
            return " ".join(words[1:cut]), " ".join(words[cut + 1 :])
        # a stray ")" closes nothing, so a phrase opened after it still
        # holds its own "to"
        depth = max(depth + key.count("(") - key.count(")"), 0)
    return " ".join(words[1:]), None


# ---------------------------------------------------------------------------
# adaptation
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    """GEDCOM NAME ('Eugen /Löbig/') -> display form."""
    return re.sub(r"\s+", " ", name.replace("/", " ")).strip()


def _source_title(record: Record) -> str:
    """A level-0 SOUR record's human title (TITL), else its inline value.

    Resolves the opaque ``@S..@`` pointer a citation carries to the name
    of the upstream source the tree relied on ("Adressbuch der Stadt
    Flörburg 1912"). Whitespace-collapsed; never fetched.
    """
    title = record.text("TITL").strip() or record.value.strip()
    return " ".join(title.split())


def _media_label(obje: Record) -> str:
    """A GEDCOM multimedia object's display label.

    Prefers the object's title (TITL, which real exports nest under
    FILE), then its file name/URL, then a generic fallback — labels must
    be non-empty (EntityRecord.label).
    """
    file_record = obje.first("FILE")
    for candidate in (
        file_record.text("TITL") if file_record else "",
        obje.text("TITL"),
        file_record.value if file_record else "",
        obje.value,
    ):
        cleaned = " ".join(candidate.split()) if candidate else ""
        if cleaned:
            return cleaned
    return "media object"


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
# universal genealogy convention for a nickname ("Ida (Peppi) Sörne",
# "Cäcilie 'Cilli' Tarle") — the only way this data expresses them.
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


def format_unsupported_tags(unsupported: dict[str, int]) -> str:
    """One-line census for logs: ``EDUC ×1, NOTE ×1``.

    The count is kept visibly apart from the tag (a ``×`` behind a space)
    so it never reads as part of the tag name: ``EDUCx1`` would look like
    a corrupt tag rather than one EDUC record. Single formatter, shared
    by the CLI and the orchestrator, so the two cannot drift.
    """
    return ", ".join(
        f"{tag} ×{count}" for tag, count in sorted(unsupported.items())
    )


def adapt_gedcom(text: str, artifact: ArtifactRef) -> AdapterResult:
    """The whole file -> one ClaimBatch with entity manifest, every claim
    citing `artifact`, the export's content key: the SHA-1 of the file's
    bytes, of which `text` is the decoded form."""
    roots = parse_gedcom(text)
    individuals = {r.xref: r for r in roots if r.tag == "INDI" and r.xref}
    families = {r.xref: r for r in roots if r.tag == "FAM" and r.xref}
    # level-0 records a person/event may point at by xref: SOUR = the
    # upstream source a citation cites; OBJE = a multimedia object (ADR-0021)
    sources = {r.xref: r for r in roots if r.tag == "SOUR" and r.xref}
    media_records = {r.xref: r for r in roots if r.tag == "OBJE" and r.xref}
    warnings: list[str] = []
    unsupported: dict[str, int] = {}

    HANDLED_INDI = {
        "NAME", "_MARNM", "SEX", "BIRT", "DEAT", "BURI", "OCCU", "NCHI",
        "RESI", "FAMC", "FAMS",
    }  # fmt: skip
    HANDLED_FAM = {"HUSB", "WIFE", "CHIL", "MARR", "DIV"}
    # Unsupported-tag census (ADR-0021). Name the top-most tag the adapter
    # does not map in EVERY branch of EVERY record — not merely the direct
    # children of INDI/FAM. `reads` holds what the adapter reads beneath
    # each tag it maps, every occurrence of it, save the tags in
    # `reads_once`; a tag it maps with nothing read beneath (SEX, FAMC,
    # HUSB, DATE, PLAC...) has no entry. A child read where it stands is
    # descended into, so an unmapped tag nested under a mapped event still
    # surfaces. `handled` tags the adapter incorporates wholesale — SOUR
    # (folded into claim references) and OBJE (minted as un-held media
    # items) — are skipped subtree and all: neither counted nor descended.
    # The envelope is structure, not data. Everything else is counted once
    # and its subtree left untouched, so an already-dropped branch is not
    # itemised down to its PAGE/_APID leaves. What remains reported is the
    # genuine tail (REPO, SUBM, tool-specific `_` tags), and what a mapped
    # record carries that the model has no property for (a divorce's PLAC,
    # a marriage's TYPE).
    reads = {
        "INDI": HANDLED_INDI,
        "FAM": HANDLED_FAM,
        # name parts are read, or restated by the NAME value that is read
        "NAME": {"TYPE", "NICK", "GIVN", "SURN", "NPFX", "NSFX", "SPFX"},
        "BIRT": {"DATE", "PLAC"},
        "DEAT": {"DATE", "PLAC", "CAUS"},
        "BURI": {"DATE", "PLAC"},
        "OCCU": {"DATE", "PLAC"},
        "RESI": {"DATE", "PLAC"},
        "MARR": {"DATE", "PLAC"},
        "DIV": {"DATE"},
    }
    # the tags the adapter reads only the first of beneath a tag: a family
    # has one husband and one wife
    reads_once = {"FAM": {"HUSB", "WIFE"}}
    # the tags each tag is read beneath
    read_beneath: dict[str, set[str]] = {}
    for parent, tags in reads.items():
        for tag in tags:
            read_beneath.setdefault(tag, set()).add(parent)
    handled = {"SOUR", "OBJE"}  # incorporated wholesale (ADR-0021)
    envelope = {"HEAD", "TRLR"}  # GEDCOM structure, not genealogy

    def count(tag: str) -> None:
        unsupported[tag] = unsupported.get(tag, 0) + 1

    def census(record: Record) -> None:
        seen: set[str] = set()
        for child in record.children:
            if child.tag in handled or child.tag in envelope:
                continue
            # only a family's first HUSB and first WIFE are read
            further = child.tag in seen and child.tag in reads_once.get(
                record.tag, set()
            )
            seen.add(child.tag)
            if (
                child.tag in reads.get(record.tag, set())
                and not further
                and not (
                    # a DIV dates the marriage's end only by its DATE: an
                    # undated one says it ended at an unknown time, and a
                    # claim has no unknown value to state that with
                    child.tag == "DIV" and child.first("DATE") is None
                )
            ):
                census(child)  # descend: an unmapped tag may sit deeper
            # otherwise the top-most unmapped tag stands for its whole
            # branch, named with its parent's tag (DIV.PLAC) when it is
            # read beneath other tags, or beneath this one in its first
            # occurrence (FAM.HUSB): a bare PLAC would read as though
            # every PLAC went unmapped
            elif further or read_beneath.get(child.tag, set()) - {record.tag}:
                count(f"{record.tag}.{child.tag}")
            else:
                count(child.tag)

    for root in roots:
        if root.tag in handled or root.tag in envelope:
            continue
        if root.tag in ("INDI", "FAM"):
            census(root)
        else:
            count(root.tag)

    entities: list[dict] = []
    claims: list[dict] = []
    place_ids: dict[str, str] = {}
    occupation_ids: dict[str, str] = {}
    media_ids: dict[str, str] = {}

    def cited_sources(record: Record) -> tuple[list[str], list[str]]:
        """(PAGE locators, resolved upstream-source titles) from a
        record's own SOUR citations — ADR-0021 step A.

        A GEDCOM SOUR child names the upstream source the tree relied on;
        we fold it into the claim's existing reference rather than drop
        it. The upstream @S..@ is resolved to its title, never fetched.
        """
        pages: list[str] = []
        titles: list[str] = []
        for citation in record.all("SOUR"):
            page = citation.text("PAGE").strip()
            if page:
                pages.append(page)
            target = sources.get(citation.value.strip())
            if target and (title := _source_title(target)):
                titles.append(title)
        return pages, titles

    def source(record: Record) -> dict:
        # monolingual text values forbid newlines; keep each GEDCOM line
        # verbatim and join visibly
        excerpt = record.lines().replace("\n", " | ")
        pages, titles = cited_sources(record)
        if titles:
            # the verbatim lines carry only the @S..@ pointer; name the
            # cited source(s) so the reviewer can read the citation
            excerpt += " | cited: " + "; ".join(dict.fromkeys(titles))
        if len(excerpt) > 400:
            excerpt = excerpt[:397].rsplit(" | ", 1)[0] + " | …"
        reference: dict = {
            "artifact": artifact.model_dump(mode="json"),
            "transcription_excerpt": {"text": excerpt, "language": "en"},
        }
        if pages:
            # the citation's PAGE is the reference's locator (ADR-0003)
            reference["page_number"] = " ; ".join(dict.fromkeys(pages))
        return reference

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

    def media_entity(obje: Record) -> str:
        """A lightweight un-held media item for a GEDCOM OBJE (ADR-0021).

        Kind `artifact`, not `photograph`: we hold none of the bytes and
        cannot vouch it is a photo (many OBJE are document scans), and an
        un-held stub with no `media file`/`media SHA-1` stays out of the
        gallery/faces flows, which key on held media. Deduped by xref so
        several people can point at the one object.
        """
        key = obje.xref or f"@inline{obje.line_number}@"
        if key not in media_ids:
            media_ids[key] = f"md{len(media_ids) + 1}"
            entities.append(
                {
                    "id": media_ids[key],
                    "label": _media_label(obje),
                    "kind": "artifact",
                    "summary": (
                        "media referenced from the GEDCOM export; bytes not "
                        "held and never fetched from a third party (ADR-0021)"
                    ),
                }
            )
        return media_ids[key]

    def local(entity_id: str) -> dict:
        return {"kind": "local", "local_id": entity_id}

    def place_item(name: str) -> dict:
        """An item value naming the place `name`."""
        return {"type": "item", "value": local(place_entity(name))}

    def located(name: str) -> dict:
        """A location qualifier naming the place `name`."""
        return {"property": "location", "value": place_item(name)}

    # every claim the batch holds, as its JSON: one that an event restating
    # another gives again is not added twice
    made: set[str] = set()

    def claim(
        subject_id: str, prop: str, value: dict, record: Record, quals=()
    ):
        new = {
            "subject": local(subject_id),
            "property": prop,
            "value": value,
            "qualifiers": list(quals),
            "source": source(record),
            "confidence": 1.0,
        }
        key = json.dumps(new, sort_keys=True)
        if key not in made:
            made.add(key)
            claims.append(new)

    def places(event: Record) -> list[str]:
        """The places an event's PLACs name, each once and in their order:
        a further PLAC is an alternative, placed by a claim of its own."""
        return list(
            dict.fromkeys(
                place.value for place in event.all("PLAC") if place.value
            )
        )

    def each_date(
        event: Record, qualifiers: Callable[..., list[dict]], *args: str
    ) -> list[list[dict]]:
        """The qualifiers that each DATE of `event` gives, from
        `qualifiers(date, *args)`, each distinct list once: a further DATE
        is an alternative, dated by a claim of its own. [[]] when no DATE
        gives any, so that the event's claim goes undated; a DATE that gives
        none (one the grammar cannot parse) adds no undated claim beside a
        dated one."""
        found: list[list[dict]] = []
        for date_record in event.all("DATE"):
            quals = qualifiers(date_record, *args)
            if quals and quals not in found:
                found.append(quals)
        return found or [[]]

    def dated_qualifiers(date_record: Record, context: str) -> list[dict]:
        """point_in_time (+uncertainty) qualifiers from an event's DATE."""
        value, date_quals = parse_gedcom_date(date_record.value)
        if value is None:
            warnings.append(
                f"line {date_record.line_number}: unparsed {context} "
                f"date {date_record.value!r} — point_in_time omitted"
            )
            return []
        return [{"property": "point_in_time", "value": value}, *date_quals]

    def dated_end(date_record: Record, end: str, context: str) -> list[dict]:
        """The qualifiers that date one end of a period, `end` (start_time
        or end_time), from an event's DATE: a marriage's date is the
        spouse statements' start, a divorce's their end."""
        value, date_quals = parse_gedcom_date(date_record.value)
        if value is None:
            warnings.append(
                f"line {date_record.line_number}: unparsed {context} "
                f"date {date_record.value!r} — {end} omitted"
            )
            return []
        return period_end(value, date_quals, end)

    def period_qualifiers(date_record: Record, context: str) -> list[dict]:
        """The qualifiers that date a statement which holds for a period (a
        residence, an occupation) from its event's DATE. A date period is
        the statement's start and end time, each dated by its own phrase
        with the range that phrase states (docs/MODELING.md, "Marriages,
        residences and other periods"); any other date is a point in time
        at which the source observes the statement."""
        phrases = split_gedcom_period(date_record.value)
        if phrases is None:
            return dated_qualifiers(date_record, context)
        quals: list[dict] = []
        for end, phrase in zip(
            ("start_time", "end_time"), phrases, strict=True
        ):
            if phrase is None:
                continue  # the period names no such end
            # a period within a period is no date
            value, date_quals = (
                (None, [])
                if split_gedcom_period(phrase) is not None
                else parse_gedcom_date(phrase)
            )
            if value is None:
                warnings.append(
                    f"line {date_record.line_number}: unparsed {context} "
                    f"date {date_record.value!r} — {end} omitted"
                )
                continue
            for qualifier in period_end(value, date_quals, end):
                # one circa stands for both ends, as on a spouse statement
                if qualifier not in quals:
                    quals.append(qualifier)
        return quals

    def event_claims(
        indi_xref: str, event: Record, date_prop: str, place_prop: str
    ):
        """A birth's or a death's claims: a date claim for each DATE and a
        place claim for each PLAC, each with its own qualifiers."""
        for date_record in event.all("DATE"):
            value, quals = parse_gedcom_date(date_record.value)
            if value is None:
                warnings.append(
                    f"line {date_record.line_number}: unparsed date "
                    f"{date_record.value!r} — no {date_prop} claim emitted"
                )
                continue
            claim(person_id(indi_xref), date_prop, value, event, quals)
        for place in places(event):
            claim(person_id(indi_xref), place_prop, place_item(place), event)

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
            # a further TYPE types the name a further way, a claim of its own
            for name_type in name_record.all("TYPE"):
                prop = _NAME_TYPE_PROPERTY.get(name_type.value.strip().lower())
                if prop == "nickname":
                    add_nickname(
                        cleaned, name_record
                    )  # the whole aka IS the nickname
                elif prop:
                    claim(
                        person_id(xref),
                        prop,
                        monolingual(cleaned),
                        name_record,
                    )
            for nick in name_record.all("NICK"):
                add_nickname(nick.value.strip(), name_record)
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

    def spouse_qualifiers(fam: Record) -> list[list[dict]]:
        """The qualifiers of a family's spouse claims, a list for each pair
        of claims. A MARR's date is the claims' start time and its place
        their location, and a DIV's date is their end time (docs/MODELING.md,
        "Marriages, residences and other periods").

        Each MARR gives a start for each of its dates with each of its
        places, and each DIV an end for each of its dates. The family's
        first start and first end date one pair of claims, as its only MARR
        and DIV do; every other start or end is an alternative the record
        states, and dates a pair of its own, with its own circa (ADR-0002).
        A MARR that neither dates nor places anything, or a DIV that dates
        nothing, gives no start or end, and so no pair beside one that
        does: the family's claims state the marriage already. A start or
        an end that restates another gives none either."""
        starts: list[list[dict]] = []
        for marriage in fam.all("MARR"):
            dates = each_date(marriage, dated_end, "start_time", "marriage")
            locations = [[located(place)] for place in places(marriage)]
            for location in locations or [[]]:
                for quals in dates:
                    start = quals + location
                    if start and start not in starts:
                        starts.append(start)
        ends: list[list[dict]] = []
        for divorce in fam.all("DIV"):
            for end in each_date(divorce, dated_end, "end_time", "divorce"):
                if end and end not in ends:
                    ends.append(end)
        first = starts[0] if starts else []
        # a circa the marriage's date gave the statement already stands for
        # the divorce's too
        first = first + [
            q for q in (ends[0] if ends else []) if q not in first
        ]
        return [first, *starts[1:], *ends[1:]]

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
        # every date the record gives a birth or a death, as it gives them
        summary_bits = []
        for tag, mark in (("BIRT", "b."), ("DEAT", "d.")):
            dates = [
                date.value
                for event in record.all(tag)
                for date in event.all("DATE")
                if date.value
            ]
            if dates:
                summary_bits.append(
                    f"{mark} " + " or ".join(dict.fromkeys(dates))
                )
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
        # terms, ADR-0014); only the unambiguous M/F are mapped, and a
        # second SEX that differs coexists with the first (ADR-0002)
        for sex in record.all("SEX"):
            sex_value = {"M": "male", "F": "female"}.get(
                sex.value.strip().upper()
            )
            if sex_value:
                claim(
                    person_id(xref),
                    "sex_or_gender",
                    {
                        "type": "item",
                        "value": {"kind": "vocab", "key": sex_value},
                    },
                    sex,
                )
        for birth in record.all("BIRT"):
            event_claims(xref, birth, "date_of_birth", "place_of_birth")
        for death in record.all("DEAT"):
            event_claims(xref, death, "date_of_death", "place_of_death")
            # cause of death stays verbatim prose (docs/MODELING.md);
            # "en" matches the export's language, like the excerpts
            for cause in death.all("CAUS"):
                if cause.value:
                    claim(
                        person_id(xref),
                        "cause_of_death",
                        monolingual(cause.value),
                        death,
                    )
        for burial in record.all("BURI"):
            burial_places = places(burial)
            if not burial_places:
                warnings.append(
                    f"line {burial.line_number}: BURI without PLAC — "
                    "no burial claim emitted"
                )
                continue
            dates = each_date(burial, dated_qualifiers, "burial")
            for place in burial_places:
                for quals in dates:
                    claim(
                        person_id(xref),
                        "burial_place",
                        place_item(place),
                        burial,
                        quals,
                    )
        for occupation in record.all("OCCU"):
            name = occupation.value.strip()
            if not name:
                warnings.append(
                    f"line {occupation.line_number}: OCCU without a value — "
                    "no occupation claim emitted"
                )
                continue
            dates = each_date(occupation, period_qualifiers, "occupation")
            locations = [[located(place)] for place in places(occupation)]
            held = {"type": "item", "value": local(occupation_entity(name))}
            for location in locations or [[]]:
                for quals in dates:
                    claim(
                        person_id(xref),
                        "occupation",
                        held,
                        occupation,
                        quals + location,
                    )
        # competing counts coexist (docs/MODELING.md)
        for nchi in record.all("NCHI"):
            try:
                number = int(nchi.value.strip())
            except ValueError:
                warnings.append(
                    f"line {nchi.line_number}: NCHI {nchi.value!r} is not a "
                    "number — no number_of_children claim emitted"
                )
                continue
            claim(
                person_id(xref),
                "number_of_children",
                {"type": "quantity", "amount": number},
                nchi,
            )
        for residence in record.all("RESI"):
            residence_places = places(residence)
            if not residence_places:
                warnings.append(
                    f"line {residence.line_number}: RESI without PLAC — "
                    "no residence claim emitted"
                )
                continue
            dates = each_date(residence, period_qualifiers, "residence")
            for place in residence_places:
                for quals in dates:
                    claim(
                        person_id(xref),
                        "residence",
                        place_item(place),
                        residence,
                        quals,
                    )
        # OBJE -> an un-held media item the person `image`s (ADR-0021).
        # A pointer resolves to its level-0 OBJE record; some tools embed
        # the object inline. Reference the media, never fetch its bytes.
        for obje_ptr in record.all("OBJE"):
            target = obje_ptr.value.strip()
            if target.startswith("@"):
                obje = media_records.get(target)
                if obje is None:
                    warnings.append(
                        f"line {obje_ptr.line_number}: OBJE points to unknown "
                        f"object {target!r} — no media claim emitted"
                    )
                    continue
            else:
                obje = obje_ptr  # media embedded inline under the person
            media = {"type": "item", "value": local(media_entity(obje))}
            for quals in each_date(obje, dated_qualifiers, "media"):
                claim(person_id(xref), "image", media, obje, quals)

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
            for quals in spouse_qualifiers(fam):
                for subject, other in ((husband, wife), (wife, husband)):
                    claim(
                        person_id(subject),
                        "spouse",
                        {"type": "item", "value": local(person_id(other))},
                        fam,
                        quals,
                    )

    # ADR-0022 §4: the same alias hygiene the LLM path runs — GEDCOM's
    # structured NAME/TYPE tags already do the typing; this only drops
    # derivable short-forms and keeps compound surnames whole.
    from yizkr.pipeline.names import normalize_entity_dict

    entities = [normalize_entity_dict(entity) for entity in entities]
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
