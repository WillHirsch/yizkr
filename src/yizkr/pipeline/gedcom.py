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
  claim. Of a family record, a pair of spouse claims holds the lines
  that give it: the family's own line, its HUSB and WIFE, its own
  citations, and the MARRs and DIVs that give the pair. A person's image
  claim holds the object's lines, after those of the person's OBJE link
  when the link holds any beneath it. An excerpt is given in NFC, the
  form the instance stores text in, and holds at most 400 characters,
  as a Wikibase text value does: a longer one keeps every line of the
  citations folded into its claim (below) and the titles they resolve
  to, then, where it fits, the line each citation stands beneath, then
  each of its other lines, in order, that still fits, marking each run it
  leaves out with `…`; its first line, which names its record, is cut to
  the room left rather than left out. It ends with `clipped, sha1` and
  the first 12 hex digits of the whole excerpt's SHA-1, so that clipping
  never makes one claim of two that the whole excerpts keep apart, such
  as two citations of one fact. A page number holds at most 400
  characters too: a longer one keeps the pages that fit and ends with
  ` ; …`;
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

Every occurrence of an event, of a DATE, PLAC or CAUS beneath one where
the adapter reads that tag (a divorce's PLAC and a CAUS beneath any
event but a death are counted instead, below), of a TYPE or NICK beneath
a NAME, and of a person's SEX or NCHI, is read, not only the first. A
record that repeats an event (a second BIRT, DEAT, MARR or DIV) or a
detail (a second DATE, PLAC, CAUS, TYPE or NICK), or that gives a
person's SEX or NCHI twice, states alternatives, and each gives claims
of its own, with its own date, place and circa, so that the alternatives
coexist (ADR-0002): Gustav's birth on 7 JAN 1919 and on 1 JUL 1919 is
two date of birth claims. Where one claim holds both an event's date and
its place (a burial, a residence, an occupation, a marriage), each of
the event's dates is paired with each of its places, and each DATE of a
media object dates an image claim of its own. A family's first marriage
(a date with a place) and its first divorce date one pair of spouse
claims, as a family's only MARR and DIV do, and every other marriage or
divorce dates a pair of its own. What gives nothing to claim
(`1 BIRT Y`, `1 MARR Y`, a date the grammar cannot parse) never hides
what does: it adds no claim beside one that does, while an undated MARR
that gives a place keeps it on a pair of its own, and a DEAT that states
the death with no date, place or cause (`1 DEAT Y`) still gives a
`death` event (below), beside what another DEAT gives. An event that
restates another line for line, its citation included, gives the same
claim, which the batch holds once; the same value under another citation
is a claim of its own. So a MARR or DIV that restates another's date and
place under the same citations gives no pair of spouse claims of its
own, and under other citations gives a pair of its own, which cites
them. A family's HUSB and WIFE are still read once, and so is an
object's FILE: a further one is counted (below), its TITL with it. Every
PAGE of a citation, and every TITL of a source record, of an object or
of its first FILE, is read.

Supported (the fields family-tree exports actually carry):
individuals (names -> label + aliases, a NAME without a value read from
its parts, PLUS typed referenced name claims: NAME TYPE
birth/maiden/married/aka/official, the NICK subtag, the non-standard
_MARNM tag, and the universal inline convention where a quoted or
parenthetical token is a nickname — "Ida (Peppi) Sörne", "Cäcilie
'Cilli' Tarle"; docs/MODELING.md "Names"; a nickname that several NAMEs
give is a claim of each, citing its own NAME), birth/death dates and
places, a death the export states with no date, place or cause (by a Y
alone, or by a DATE the grammar cannot parse), which is a `death` event
with the person as participant (ADR-0020),
cause of death (DEAT.CAUS, verbatim), burials (BURI place, dated when
the record is), occupations (OCCU, minted as occupation items with the
source's wording), number of children (NCHI), residences (place, dated
when the record is), parent-child relations, marriages (the spouse
statements' start time and place) and divorces (their end time). Source
citations (SOUR) are folded into the reference of each claim that the
record they stand beneath gives (ADR-0021): the event, fact or NAME a
claim cites; the family record, for its parent and spouse claims, and,
for a pair of spouse claims, the MARRs and DIVs that date or place it
(each MARR, when none dates or places the family's one pair); the
object, for its image claims, and the person's OBJE link to it. Each
PAGE of a citation is a locator of the reference's page number, and the
cited source's title, each TITL it gives, is surfaced in the excerpt —
the upstream source is resolved, never fetched. Multimedia objects (OBJE) of
an individual become lightweight un-held media items (an `artifact`)
linked to the person with `image`, each standing for the object's first
FILE and labelled by the object's first title, its other titles being
aliases; the bytes are referenced, never downloaded from a third party
(privacy rule 7). Sibling relations are deliberately NOT
derived from shared parents: the export asserts parentage, not
siblinghood (docs/MODELING.md — never derive). Dates accept the
GEDCOM-standard English abbreviations plus what hand-maintained trees
actually contain: full English and German month names, and month-first
order when the month is named. A PLAC of nothing but commas and spaces
(', , ,', a place whose jurisdictions a tool left empty) names no place.
Tags the adapter does not map (e.g.
REPO, SUBM, tool-specific `_` tags) are counted and reported, not
silently dropped: the census walks every record and nesting level and
names the top-most unmapped tag in each branch, leaving an already-
handled or already-dropped subtree unitemised (ADR-0021). Whether a tag
is mapped depends on where it stands: a PLAC is read under a marriage
but not under a divorce, and a TYPE under a NAME but not under a MARR,
so such a tag is named with its parent's tag where it goes unread
(DIV.PLAC, MARR.TYPE). A SOUR that the adapter folds into no claim's
reference is counted so too: one directly beneath a person (INDI.SOUR),
beneath a DATE, PLAC, HUSB, WIFE, CHIL, FAMC or FAMS (DATE.SOUR,
CHIL.SOUR), or beneath an event or a NAME that gives no claim
(BIRT.SOUR, NAME.SOUR). So is an OBJE that it maps to no media item,
beneath a family or an event (FAM.OBJE, DEAT.OBJE), save a person's
link to an object the export lacks, which is counted as OBJE, as a
level-0 OBJE record that no image claim cites is. A level-0 SOUR record
that no folded citation cites is counted as SOUR, and each FILE of an
object after its first as OBJE.FILE. Within a citation the adapter
folds, a source record a folded citation cites and an object it maps,
which the census takes whole, a SOUR it folds into no claim and an OBJE
it maps to no media item are counted as well (SOUR.OBJE for a scan
beneath a citation, FILE.SOUR for a citation beneath an object's FILE).
A DIV without a DATE is counted as DIV: it says a marriage ended at an
unknown time, and a claim has no unknown value to state that with. A
family has one husband and one wife, so the adapter reads a family's
first HUSB and first WIFE alone, and a further one is counted with its
parent's tag too (FAM.HUSB, FAM.WIFE). A family's MARR
and DIV date the spouse claims between its HUSB and its WIFE, so in a
family with one partner or none (its HUSB or its WIFE is missing, or
names no person the adapter maps) they are counted as MARR and DIV: no
claim names the other partner, and a claim has no unknown value to name
them with. A DEAT states the death only as GEDCOM 5.5.1 has an event
asserted, by a DATE or PLAC with a value (a DATE the grammar cannot
parse, only when the DEAT's own line has none), or else by a Y on that
line: one that states none (a bare DEAT, or one with only a SOUR, NOTE,
AGE or CAUS) gives no claim, since a death event or a cause of death
would mark the person as not possibly living (ADR-0020), and is counted
as DEAT. A person none of whose NAMEs gives a name is skipped, with a
warning, and the family links that name them give no claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
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

    def own_line(self) -> str:
        """The record's own verbatim GEDCOM line, without the records
        beneath it; a CONT or CONC continuation stays in its value."""
        own = f"{self.level}"
        own += f" {self.xref}" if self.xref else ""
        own += f" {self.tag}"
        own += f" {self.value}" if self.value else ""
        return own

    def walk(self) -> list[Record]:
        """The record and every record beneath it, in the file's order."""
        return [
            self,
            *(node for child in self.children for node in child.walk()),
        ]

    def lines(self) -> str:
        """The record's verbatim GEDCOM lines (for excerpts)."""
        return "\n".join(node.own_line() for node in self.walk())


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


# A NAME's parts in the order GEDCOM writes a name: prefix, given name,
# surname prefix, surname and suffix ("Fräulein Klara /Löbig/", "Ursula
# /Löbig/ Wwe.").
_NAME_PARTS = ("NPFX", "GIVN", "SPFX", "SURN", "NSFX")


def _name_text(name: Record) -> str:
    """A NAME's display form: its value, cleaned ('Eugen /Löbig/' ->
    'Eugen Löbig'), which its parts are taken to restate. When the value
    gives none (it is empty, or '//'), the parts give it, in the order
    GEDCOM writes a name, each part's comma-separated pieces in turn (a
    GIVN 'Eugen' and a SURN 'Löbig' give 'Eugen Löbig'), provided a given
    name or a surname is among them: a prefix or a suffix alone ('Wwe.')
    names no one, and the writer, which finds an item by its label, would
    make everyone it stood for one item. Empty when neither gives a
    name."""
    pieces = {
        tag: [
            text
            for part in name.all(tag)
            for piece in part.value.split(",")
            if (text := _clean_name(piece))
        ]
        for tag in _NAME_PARTS
    }
    if not (pieces["GIVN"] or pieces["SURN"]):
        return _clean_name(name.value)
    return _clean_name(name.value) or " ".join(
        text for tag in _NAME_PARTS for text in pieces[tag]
    )


def _names_a_place(plac: str) -> bool:
    """Whether a PLAC's value names a place: one of nothing but commas and
    spaces (', , ,', the jurisdictions of a place a tool leaves empty)
    names none, and so neither gives a place item nor states an event."""
    return bool(plac.replace(",", " ").strip())


def _source_title(record: Record) -> str:
    """A level-0 SOUR record's human title (TITL), else its inline value.

    Resolves the opaque ``@S..@`` pointer a citation carries to the name
    of the upstream source the tree relied on ("Adressbuch der Stadt
    Flörburg 1912"). GEDCOM gives a source one TITL: a record that gives
    several names the one source each way, so each distinct one is read,
    joined by " / " ("Adressbuch der Stadt Flörburg 1912 / Flörburg
    address book 1912"). Whitespace-collapsed; never fetched.
    """
    titles = dict.fromkeys(
        title
        for tag in record.all("TITL")
        if (title := " ".join(tag.value.split()))
    )
    return " / ".join(titles) or " ".join(record.value.split())


def _media_names(obje: Record) -> tuple[str, list[str]]:
    """A GEDCOM multimedia object's display label, and its aliases.

    The object's media item stands for its first FILE: GEDCOM 5.5.1
    allows several, grouping files of one context (a photograph and a
    sound clip, say), and each further one is a medium of its own, which
    the census counts (OBJE.FILE). The label is the object's first title:
    each TITL of that FILE, where real exports nest it, then each of the
    object's own; every other distinct title is an alias, as a person's
    further NAME is. An object with no title is labelled by its first
    file's name or URL, then its own value, then a generic fallback —
    labels must be non-empty (EntityRecord.label).
    """
    files = obje.all("FILE")
    first = files[0] if files else None
    titles = list(
        dict.fromkeys(
            cleaned
            for title in [
                *(first.all("TITL") if first else []),
                *obje.all("TITL"),
            ]
            if (cleaned := " ".join(title.value.split()))
        )
    )
    if titles:
        return titles[0], titles[1:]
    for candidate in (first.value if first else "", obje.value):
        if cleaned := " ".join(candidate.split()):
            return cleaned, []
    return "media object", []


# Wikibase caps a text value, the transcription excerpt and the page
# number among them, at 400 characters
EXCERPT_LIMIT = 400
# the mark of a run of lines an excerpt leaves out, or of the pages a page
# number leaves out
ELIDED = "…"
# How a clipped excerpt keeps a line (_excerpt): a line of a citation
# folded into the claim is kept, the line a folded citation stands beneath
# next, and any other line after those, in order, while it fits.
CITATION, CITING, OTHER = range(3)
# a GEDCOM line's level, its xref if it has one, and its tag, before a value
_LINE_HEAD = re.compile(r"\d+ (?:@[^@]+@ )?\S+ ")


def _excerpt(segments: list[tuple[str, int]], cited: str) -> str:
    """A claim's transcription excerpt from its GEDCOM lines, `segments`,
    each a line (or a CONT line within one) with how a clip keeps it
    (CITATION, CITING or OTHER), and `cited`, the titles the folded
    citations resolve to (" | cited: ..."), or "".

    The lines are joined by " | ", as monolingual text forbids newlines,
    with `cited` after them, in NFC, the form the instance stores text in,
    so that the length counted here is the length the writer counts. An
    excerpt longer than EXCERPT_LIMIT keeps every line of the folded
    citations and `cited`, then, where it fits, the line each folded
    citation stands beneath, so that the citation is read as that line's,
    then each of its other lines, in order, that still fits; each run of
    lines it leaves out is marked ELIDED. Its first line, which names its
    record, is cut to the room left at its turn when it does not fit
    whole, ending with ELIDED, provided the cut keeps its level and tag
    and something of its value. Should the citations not fit whole, it
    keeps as many of all its lines, from the first, as fit, cut back to a
    whole line where it can. Either way it ends with "clipped, sha1" and
    the first 12 hex digits of the SHA-1 of the whole excerpt: two claims
    the whole excerpts keep apart stay apart once clipped, so two
    citations of one fact are never one claim, while a claim restated
    line for line is the same claim.
    """
    texts = [unicodedata.normalize("NFC", text) for text, _ in segments]
    cited = unicodedata.normalize("NFC", cited)
    whole = " | ".join(texts) + cited
    if len(whole) <= EXCERPT_LIMIT:
        return whole
    digest = hashlib.sha1(whole.encode()).hexdigest()[:12]
    tail = f" | clipped, sha1 {digest}"
    room = EXCERPT_LIMIT - len(tail)
    kept = [keep == CITATION for _, keep in segments]
    length = len(_kept(texts, kept)) + len(cited)
    if length > room:
        cut = whole[: room - len(f" | {ELIDED}")]
        if " | " in cut:
            cut = cut.rsplit(" | ", 1)[0]
        return f"{cut} | {ELIDED}{tail}"
    for wanted in (CITING, OTHER):
        for index, (_, keep) in enumerate(segments):
            if keep != wanted:
                continue
            # keeping a line of a run left out splits the run in two, ends
            # it on one side, or takes the place of its mark
            before = index > 0 and not kept[index - 1]
            after = index + 1 < len(kept) and not kept[index + 1]
            marks = 7 if before and after else 3 if before or after else -1
            if length + len(texts[index]) + marks <= room:
                kept[index] = True
                length += len(texts[index]) + marks
            elif index == 0 and (head := _LINE_HEAD.match(texts[0])):
                # the passage's first line names its record: one too long
                # for the room left is cut to it, provided it keeps its
                # level and tag and something of its value
                spare = room - length - marks - len(ELIDED)
                cut = texts[0][: max(spare, 0)]
                if len(cut.rstrip()) > head.end():
                    texts[0] = cut.rstrip() + ELIDED
                    kept[0] = True
                    length += len(texts[0]) + marks
    return _kept(texts, kept) + cited + tail


def _kept(texts: list[str], kept: list[bool]) -> str:
    """The `texts` that `kept` keeps, joined by " | ", with each run of
    texts left out marked ELIDED."""
    out: list[str] = []
    for index, text in enumerate(texts):
        if kept[index]:
            out.append(text)
        elif index == 0 or kept[index - 1]:
            out.append(ELIDED)
    return " | ".join(out)


def _page_number(pages: list[str]) -> str:
    """A reference's page number from the PAGEs of the citations folded
    into its claim: each distinct one, in NFC, joined by " ; ". One longer
    than EXCERPT_LIMIT, which a Wikibase string cannot hold, keeps the
    pages that fit, cut back to a whole page where it can, and ends with
    " ; " and ELIDED; the excerpt still gives the citations whole, or its
    clip's key, so two claims whose pages differ past the cut stay two."""
    page = " ; ".join(
        dict.fromkeys(unicodedata.normalize("NFC", page) for page in pages)
    )
    if len(page) <= EXCERPT_LIMIT:
        return page
    page = page[: EXCERPT_LIMIT - len(f" ; {ELIDED}")]
    if " ; " in page:
        page = page.rsplit(" ; ", 1)[0]
    return f"{page} ; {ELIDED}"


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
    # each person's names, from the NAMEs that give one; a person with none
    # is skipped, so `people` holds the xrefs of those the batch holds
    names_of = {
        xref: [
            text for name in record.all("NAME") if (text := _name_text(name))
        ]
        for xref, record in individuals.items()
    }
    people = {xref for xref, names in names_of.items() if names}
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
    # surfaces. A SOUR citation folded into a claim's reference, and an
    # OBJE mapped to a media item, is incorporated wholesale (`folded`,
    # `mapped`): it is not counted, and within it only a SOUR or an OBJE
    # that is itself neither folded nor mapped is (`within`), as is
    # within a source record a folded citation cites. The rest of a
    # citation or an object stands in an excerpt, and the rest of a source
    # record is left to step B (ADR-0021). Whether one is depends on where
    # it stands and on what the record it stands beneath gives, so the
    # census runs once the claims are made: a SOUR directly beneath a
    # person, beneath a DATE, PLAC, HUSB, WIFE, CHIL, FAMC or FAMS, or
    # beneath an event that gives no claim, is folded into none, and an
    # OBJE beneath a family or an event is mapped to no media item, and
    # each is counted. So is a level-0 SOUR record that no folded citation
    # cites, a level-0 OBJE record that no image claim cites, and each FILE
    # of an object after its first, which its media item does not stand
    # for.
    # The envelope is structure, not data. Everything else is counted once
    # and its subtree left untouched, so an already-dropped branch is not
    # itemised down to its PAGE/_APID leaves. What remains reported is the
    # genuine tail (REPO, SUBM, tool-specific `_` tags), and what a mapped
    # record carries that the model has no property for (a divorce's PLAC,
    # a marriage's TYPE). A tag read where it stands can still go unread
    # for what one occurrence says (`unread`): it is counted then too.
    reads = {
        "INDI": HANDLED_INDI,
        "FAM": HANDLED_FAM,
        # a NAME's parts give its name when it has no value, and are taken
        # as restated by the value that is read when it has one
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
    # a SOUR is folded beneath the records that claims cite and the events
    # that date a family's spouse claims, and an OBJE is mapped beneath a
    # person alone, so one that is not is named with its parent's tag
    # (DATE.SOUR, FAM.OBJE) where the other tags hold it
    read_beneath["SOUR"] = {
        "NAME", "_MARNM", "SEX", "BIRT", "DEAT", "BURI", "OCCU", "NCHI",
        "RESI", "OBJE", "FAM", "MARR", "DIV",
    }  # fmt: skip
    read_beneath["OBJE"] = {"INDI"}
    envelope = {"HEAD", "TRLR"}  # GEDCOM structure, not genealogy
    # the line numbers of the SOUR citations folded into a claim's
    # reference and of the OBJE mapped to a media item (a person's link,
    # and the object it points to), and the xrefs of the level-0 SOUR
    # records a folded citation cites: what the census takes as read
    folded: set[int] = set()
    mapped: set[int] = set()
    resolved: set[str] = set()

    def count(tag: str) -> None:
        unsupported[tag] = unsupported.get(tag, 0) + 1

    def coupled(fam: Record) -> bool:
        """Whether a family's first HUSB and first WIFE each name a person
        the batch holds, so that its spouse claims name the one to the
        other."""
        return fam.text("HUSB") in people and fam.text("WIFE") in people

    def states_death(death: Record) -> bool:
        """Whether a DEAT states that the person died. GEDCOM 5.5.1 has an
        event asserted by a DATE or a PLAC with a value, or else by a Y
        on the event's own line, and lets a SOUR or a NOTE be attached to
        a DEAT without asserting the death. A DATE the grammar parses, or
        a PLAC, states it whatever that line says, as each gives a claim
        of its own; a DATE the grammar cannot parse states it only when
        the line has no value, so that a date the adapter cannot read
        never overrules another value, such as an N denying the death.
        What else a DEAT holds (a CAUS, an AGE, a SOUR, a NOTE) states
        nothing: a death event or a cause of death is death evidence,
        which marks the person as not possibly living (ADR-0020)."""
        value = death.value.strip()
        dates = [
            date.value for date in death.all("DATE") if date.value.strip()
        ]
        return (
            value.upper() == "Y"
            or any(_names_a_place(place.value) for place in death.all("PLAC"))
            or any(parse_gedcom_date(date)[0] is not None for date in dates)
            or (not value and bool(dates))
        )

    def unread(parent: Record, child: Record) -> bool:
        """Whether `child`, of a tag the adapter reads beneath `parent`'s,
        goes unread for what it says there."""
        if child.tag == "DIV" and child.first("DATE") is None:
            # a DIV dates the marriage's end only by its DATE: an undated
            # one says it ended at an unknown time, and a claim has no
            # unknown value to state that with
            return True
        if child.tag == "DEAT":
            # a DEAT that states no death gives no claim
            return not states_death(child)
        # a family's MARR and DIV date the spouse claims between its two
        # partners: with one or none, no claim names the other, and a claim
        # has no unknown value to name them with
        return child.tag in ("MARR", "DIV") and not coupled(parent)

    def within(record: Record, whole: Sequence[Record] = ()) -> None:
        """Count, beneath `record`, which the census takes as incorporated
        wholesale (a citation folded into a claim's reference, a source
        record a folded citation cites, an OBJE mapped to a media item),
        each SOUR folded into no claim and each OBJE mapped to no media
        item, named with its parent's tag (SOUR.OBJE, FILE.SOUR): its line
        stands in an excerpt, or in none, and no claim holds what it cites
        or shows. What `whole` holds is counted whole elsewhere."""
        for child in record.children:
            if any(child is counted for counted in whole):
                continue
            if child.tag == "SOUR" and child.line_number in folded:
                within(child)
            elif child.tag in ("SOUR", "OBJE"):
                count(f"{record.tag}.{child.tag}")
            else:
                within(child)

    def mapped_object(obje: Record) -> None:
        """Count, of an object an image claim maps, each FILE after its
        first, which its media item does not stand for (_media_names),
        and what else within it the adapter neither folds nor maps."""
        further = obje.all("FILE")[1:]
        for _ in further:
            count("OBJE.FILE")
        within(obje, further)

    def census(record: Record) -> None:
        seen: set[str] = set()
        for child in record.children:
            if child.tag == "SOUR" and child.line_number in folded:
                within(child)
                continue
            if child.tag == "OBJE" and child.line_number in mapped:
                # a link to a level-0 object holds no file of the object's
                if child.value.strip().startswith("@"):
                    within(child)
                else:
                    mapped_object(child)
                continue
            # only a family's first HUSB and first WIFE are read
            further = child.tag in seen and child.tag in reads_once.get(
                record.tag, set()
            )
            seen.add(child.tag)
            if (
                child.tag in reads.get(record.tag, set())
                and not further
                and not unread(record, child)
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

    entities: list[dict] = []
    claims: list[dict] = []
    place_ids: dict[str, str] = {}
    occupation_ids: dict[str, str] = {}
    media_ids: dict[str, str] = {}

    def cited_sources(
        citations: Sequence[Record],
    ) -> tuple[list[str], list[str]]:
        """(PAGE locators, resolved upstream-source titles) from the SOUR
        citations folded into one claim — ADR-0021 step A.

        A GEDCOM SOUR names the upstream source the tree relied on; we
        fold it into the claim's existing reference rather than drop it.
        Each PAGE of a citation is a locator. The upstream @S..@ is
        resolved to its title, never fetched.
        """
        pages: list[str] = []
        titles: list[str] = []
        for citation in citations:
            folded.add(citation.line_number)
            pages += [
                page
                for tag in citation.all("PAGE")
                if (page := tag.value.strip())
            ]
            pointer = citation.value.strip()
            target = sources.get(pointer)
            if target is not None:
                resolved.add(pointer)
                if title := _source_title(target):
                    titles.append(title)
        return pages, titles

    def source(
        record: Record,
        passage: Sequence[Record] | None = None,
        events: Sequence[Record] = (),
    ) -> dict:
        """The reference of a claim that `record` gives: the export, with
        the lines of `passage` (by default `record` and every line beneath
        it) as the excerpt, and the SOUR citations directly beneath
        `record` or one of `events` folded in. Only a citation that the
        passage holds is folded in, so that the excerpt shows it: a clip
        keeps the folded citations' lines in preference to any other
        (_excerpt)."""
        passage = record.walk() if passage is None else passage
        folding = {
            id(citation)
            for holder in (record, *events)
            for citation in holder.all("SOUR")
        }
        citations = [node for node in passage if id(node) in folding]
        folded_in = {id(citation) for citation in citations}
        in_citations = {
            id(node) for citation in citations for node in citation.walk()
        }
        # the records the folded citations stand directly beneath
        holders = {
            id(node)
            for node in passage
            if any(id(child) in folded_in for child in node.children)
        }
        # monolingual text values forbid newlines: each GEDCOM line, and
        # each CONT line within one, is kept verbatim and joined visibly
        segments = [
            (
                text,
                CITATION
                if id(node) in in_citations
                else CITING
                if id(node) in holders and not position
                else OTHER,
            )
            for node in passage
            for position, text in enumerate(node.own_line().split("\n"))
        ]
        pages, titles = cited_sources(citations)
        # the verbatim lines carry only the @S..@ pointer; name the cited
        # source(s) so the reviewer can read the citation
        cited = (
            " | cited: " + "; ".join(dict.fromkeys(titles)) if titles else ""
        )
        reference: dict = {
            "artifact": artifact.model_dump(mode="json"),
            "transcription_excerpt": {
                "text": _excerpt(segments, cited),
                "language": "en",
            },
        }
        if pages:
            # the citation's PAGE is the reference's locator (ADR-0003)
            reference["page_number"] = _page_number(pages)
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
            label, aliases = _media_names(obje)
            entities.append(
                {
                    "id": media_ids[key],
                    "label": label,
                    "aliases": aliases,
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

    # every claim the batch holds, as its JSON, source included: an event
    # that restates another line for line, its citation included, gives
    # the same claim again, which is not added twice, while the same value
    # under another citation has a source of its own, and so is a claim of
    # its own (a MARR or DIV that restates another's date and place under
    # the same citations gives no pair of spouse claims of its own; see
    # spouse_pairs)
    made: set[str] = set()

    def claim(
        subject_id: str,
        prop: str,
        value: dict,
        record: Record,
        quals=(),
        passage: Sequence[Record] | None = None,
        events: Sequence[Record] = (),
    ):
        """A claim that `record` gives, cited as `source` has it."""
        new = {
            "subject": local(subject_id),
            "property": prop,
            "value": value,
            "qualifiers": list(quals),
            "source": source(record, passage, events),
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
                place.value
                for place in event.all("PLAC")
                if _names_a_place(place.value)
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
    ) -> bool:
        """A birth's or a death's claims: a date claim for each DATE and a
        place claim for each PLAC, each with its own qualifiers. Whether
        the event gave any."""
        gave = False
        for date_record in event.all("DATE"):
            value, quals = parse_gedcom_date(date_record.value)
            if value is None:
                warnings.append(
                    f"line {date_record.line_number}: unparsed date "
                    f"{date_record.value!r} — no {date_prop} claim emitted"
                )
                continue
            gave = True
            claim(person_id(indi_xref), date_prop, value, event, quals)
        for place in places(event):
            gave = True
            claim(person_id(indi_xref), place_prop, place_item(place), event)
        return gave

    # person xref -> the local id of their death event
    death_ids: dict[str, str] = {}

    def death_event(xref: str, label: str, death: Record) -> str:
        """The death event of the person `xref`, labelled with the
        person's `label`: one for each person, however many DEATs state
        the death."""
        if xref not in death_ids:
            death_ids[xref] = f"{person_id(xref)}-death"
            entities.append(
                {
                    "id": death_ids[xref],
                    "label": f"Death of {label}",
                    "kind": "death",
                    "summary": "death stated in the GEDCOM export",
                    "mention_lines": [death.line_number],
                }
            )
        return death_ids[xref]

    def died(xref: str, label: str, death: Record) -> None:
        """A DEAT that states the death (`states_death`) but gives no
        date, place or cause, by its Y alone or by a DATE the grammar
        cannot parse, says that the person died and nothing else: a
        `death` event with the person as participant (ADR-0020 §2,
        docs/MODELING.md "Living persons and privacy"), which the
        participant claim states, citing this DEAT."""
        claim(
            death_event(xref, label, death),
            "participant",
            {"type": "item", "value": local(person_id(xref))},
            death,
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
        GEDCOM record that states it. A nickname that several NAMEs give is
        thus a claim of each, with its own citation, while one NAME that
        gives it twice (a NICK and its inline form) gives the one claim.
        """

        def add_nickname(text: str, name_record: Record) -> None:
            if text:
                claim(
                    person_id(xref), "nickname", monolingual(text), name_record
                )

        for name_record in record.all("NAME"):
            cleaned = _name_text(name_record)
            # a further TYPE types the name a further way, a claim of its
            # own; a NAME that gives no name has none to type
            for name_type in name_record.all("TYPE") if cleaned else []:
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
            if married := _clean_name(marnm.value):
                claim(
                    person_id(xref),
                    "married_name",
                    monolingual(married),
                    marnm,
                )

    def spouse_pairs(fam: Record) -> list[tuple[list[dict], list[Record]]]:
        """The pairs of a family's spouse claims, each as its qualifiers and
        the MARRs and DIVs that give them. A MARR's date is the claims'
        start time and its place their location, and a DIV's date is their
        end time (docs/MODELING.md, "Marriages, residences and other
        periods"); the SOUR citations of the MARRs and DIVs that give a
        pair are folded into its claims' reference.

        Each MARR gives a start for each of its dates with each of its
        places, and each DIV an end for each of its dates. The family's
        first start and first end date one pair of claims, as its only MARR
        and DIV do; every other start or end is an alternative the record
        states, and dates a pair of its own, with its own circa (ADR-0002).
        A MARR that neither dates nor places anything, or a DIV that dates
        nothing, gives no start or end, and so no pair beside one that
        does: the family's claims state the marriage already, and its
        citations are folded into none. When no MARR or DIV gives one, each
        MARR gives the family's one pair, which none dates or places. A
        start or an end that restates another under the same citations,
        line for line, gives no pair of its own, and its MARR or DIV joins
        the other's; under other citations, it is a pair of its own,
        citing them, since the same value under another citation is a
        claim of its own."""

        def cites(event: Record) -> str:
            """An event's SOUR citations, line for line."""
            return "\n".join(c.lines() for c in event.all("SOUR"))

        def given(
            found: list[tuple[list[dict], str, list[Record]]],
            quals: list[dict],
            event: Record,
        ) -> None:
            """Note that `event` gives the start or end `quals`."""
            for known, citing, events in found:
                if known == quals and citing == cites(event):
                    if not any(other is event for other in events):
                        events.append(event)
                    return
            found.append((quals, cites(event), [event]))

        starts: list[tuple[list[dict], str, list[Record]]] = []
        for marriage in fam.all("MARR"):
            dates = each_date(marriage, dated_end, "start_time", "marriage")
            locations = [[located(place)] for place in places(marriage)]
            for location in locations or [[]]:
                for quals in dates:
                    if start := quals + location:
                        given(starts, start, marriage)
        ends: list[tuple[list[dict], str, list[Record]]] = []
        for divorce in fam.all("DIV"):
            for end in each_date(divorce, dated_end, "end_time", "divorce"):
                if end:
                    given(ends, end, divorce)
        if not starts and not ends:
            return [([], fam.all("MARR"))]
        first, events = (starts[0][0], starts[0][2]) if starts else ([], [])
        if ends:
            # a circa the marriage's date gave the statement already stands
            # for the divorce's too
            first = first + [q for q in ends[0][0] if q not in first]
            events = [*events, *ends[0][2]]
        return [
            (first, events),
            *((quals, given_by) for quals, _, given_by in starts[1:]),
            *((quals, given_by) for quals, _, given_by in ends[1:]),
        ]

    # individuals -> entities + birth/death claims
    for xref, record in individuals.items():
        names = names_of[xref]
        if not names:
            warnings.append(
                f"{xref}: no NAME gives a name; skipped entirely, with the "
                "family links that name it"
            )
            continue
        # every date the record gives a birth or a death, as it gives them,
        # save a DEAT's that states no death
        summary_bits = []
        for tag, mark in (("BIRT", "b."), ("DEAT", "d.")):
            dates = [
                date.value
                for event in record.all(tag)
                if tag != "DEAT" or states_death(event)
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
            # a DEAT that states no death gives no claim, not even its
            # cause, and the census counts it
            if not states_death(death):
                continue
            gave = event_claims(xref, death, "date_of_death", "place_of_death")
            # cause of death stays verbatim prose (docs/MODELING.md);
            # "en" matches the export's language, like the excerpts
            for cause in death.all("CAUS"):
                if cause.value:
                    gave = True
                    claim(
                        person_id(xref),
                        "cause_of_death",
                        monolingual(cause.value),
                        death,
                    )
            # a DEAT that states the death and gives no claim gives the
            # death event, beside what any other DEAT gives
            if not gave:
                died(xref, names[0], death)
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
            # the object's lines are the excerpt, and its citations are
            # folded in; a link that holds lines of its own (a citation of
            # the link, say) gives its lines ahead of the object's, and its
            # citations are folded in too
            link: list[Record] = []
            if target.startswith("@"):
                obje = media_records.get(target)
                if obje is None:
                    warnings.append(
                        f"line {obje_ptr.line_number}: OBJE points to unknown "
                        f"object {target!r} — no media claim emitted"
                    )
                    continue
                if obje_ptr.children:
                    link = [obje_ptr]
            else:
                obje = obje_ptr  # media embedded inline under the person
            mapped.update({obje_ptr.line_number, obje.line_number})
            media = {"type": "item", "value": local(media_entity(obje))}
            passage = [node for held in (*link, obje) for node in held.walk()]
            for quals in each_date(obje, dated_qualifiers, "media"):
                claim(
                    person_id(xref),
                    "image",
                    media,
                    obje,
                    quals,
                    passage=passage,
                    events=link,
                )

    # families -> parentage and spouse claims, between people the batch
    # holds: a HUSB, WIFE or CHIL that names no one it holds gives none
    for fam in families.values():
        husband = fam.text("HUSB")
        wife = fam.text("WIFE")
        for child in fam.all("CHIL"):
            for parent_xref, prop in ((husband, "father"), (wife, "mother")):
                if parent_xref in people and child.value in people:
                    claim(
                        person_id(child.value),
                        prop,
                        {
                            "type": "item",
                            "value": local(person_id(parent_xref)),
                        },
                        fam,
                    )
        # a family with one partner or none gives no spouse claims, and the
        # census counts its MARR and DIV
        if coupled(fam):
            partners = (fam.first("HUSB"), fam.first("WIFE"))
            for quals, events in spouse_pairs(fam):
                # of the family record, a pair's excerpt holds the lines
                # that give it: the family's own, its HUSB and WIFE, its
                # own citations, and the MARRs and DIVs that give the pair,
                # each whole. Another pair's MARR, with its citations,
                # would blur which citations are this pair's
                passage = [
                    fam,
                    *(
                        node
                        for part in fam.children
                        if part.tag == "SOUR"
                        or any(part is held for held in (*partners, *events))
                        for node in part.walk()
                    ),
                ]
                for subject, other in ((husband, wife), (wife, husband)):
                    claim(
                        person_id(subject),
                        "spouse",
                        {"type": "item", "value": local(person_id(other))},
                        fam,
                        quals,
                        passage=passage,
                        events=events,
                    )

    # the census, once the claims show which citations are folded and
    # which objects mapped
    for root in roots:
        if root.tag in envelope:
            continue
        if root.tag in ("INDI", "FAM"):
            census(root)
        elif root.tag == "SOUR" and root.xref in resolved:
            # a cited source, resolved to its title (ADR-0021), is read no
            # further, save for an object or a citation within it
            within(root)
        elif root.tag == "OBJE" and root.line_number in mapped:
            mapped_object(root)
        else:
            count(root.tag)

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
    # The adapter states a couple as a pair of spouse claims, a property
    # ADR-0036 retires; it makes no marriage entity (ADR-0036 §5), so its
    # own check lets those claims through.
    problems = validate_against_manifest(
        batch, allow_retired=frozenset({"spouse"})
    )
    if problems:
        # deterministic code emitting invalid claims is a bug, not input noise
        raise ValueError(
            "gedcom adapter produced invalid claims:\n  "
            + "\n  ".join(problems)
        )
    return AdapterResult(
        batch=batch, warnings=warnings, unsupported_tags=unsupported
    )
