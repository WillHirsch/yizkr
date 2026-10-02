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
  claim. Of a family record, a claim of its union holds the lines that
  give it: the family's own line, its HUSB and WIFE, and its own
  citations for the couple's claims, or the MARR, DIV or ANUL that gives
  an event's claims. A person's image
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
  wedding's, a divorce's or an annulment's range and circa are those of
  its own date (ADR-0036 §5). A residence's or an occupation's
  date period (FROM x TO y, FROM x, TO y) is its statement's start and
  end time, each dated by its own phrase, which may be approximate or
  a range of its own: FROM BET 1938 AND 1939 is a start between 1938
  and 1939. Any other date on them is a point in time. A divorce's or an
  annulment's period is its own start and end time, and a marriage's
  period dates its wedding by the FROM and an end of unstated kind by
  the TO.
  A date the grammar cannot parse yields NO date claim and a warning —
  never a guess.

Every occurrence of an event, of a DATE, PLAC or CAUS beneath one where
the adapter reads that tag (a CAUS beneath any event but a death is
counted instead, below), of a TYPE or NICK beneath
a NAME, and of a person's SEX or NCHI, is read, not only the first. A
record that repeats an event (a second BIRT, DEAT, MARR or DIV) or a
detail (a second DATE, PLAC, CAUS, TYPE or NICK), or that gives a
person's SEX or NCHI twice, states alternatives, and each gives claims
of its own, with its own date, place and circa, so that the alternatives
coexist (ADR-0002): Gustav's birth on 7 JAN 1919 and on 1 JUL 1919 is
two date of birth claims. Where one claim holds both an event's date and
its place (a burial, a residence, an occupation), each of the event's
dates is paired with each of its places, and each DATE of a media object
dates an image claim of its own. A family's MARRs of one kind are one
wedding and its DIVs one divorce, and each date and place of each is a
claim of its own on that event. What gives nothing to claim (`1 BIRT Y`,
a date the grammar cannot parse) never hides what does: it adds no claim
beside one that does, while a DEAT that states the death with no date,
place or cause (`1 DEAT Y`) still gives a `death` event (below), beside
what another DEAT gives, and a `1 MARR Y` or a `1 DIV Y` still gives its
wedding or divorce. An event that restates another line for line, its
citation included, gives the same claim, which the batch holds once; the
same value under another citation is a claim of its own. A family's HUSB
and WIFE are still read once, and so is an object's FILE: a further one
is counted (below), its TITL with it. Every PAGE of a citation, and
every TITL of a source record, of an object or of its first FILE, is
read.

Supported (the fields family-tree exports actually carry):
individuals (names -> label + aliases, a NAME without a value read from
its parts, each person's mention record (read_name; ADR-0035 §6), PLUS
typed referenced name claims: NAME TYPE birth/maiden/married/aka/
official, the NICK subtag, the non-standard _MARNM tag, and the inline
convention where a quoted part is a nickname — "Cäcilie 'Cilli' Tarle";
a bracketed part, "Ida (Peppi) Sörne", is an alias and an unsplit form
and no nickname; docs/MODELING.md "Names"; a nickname that several NAMEs
give is a claim of each, citing its own NAME), birth/death dates and
places, a death the export states with no date, place or cause (by a Y
alone, or by a DATE the grammar cannot parse), which is a `death` event
with the person as its principal (ADR-0020, ADR-0035 §3.2),
cause of death (DEAT.CAUS, verbatim), burials (BURI place, dated when
the record is), occupations (OCCU, minted as occupation items with the
source's wording), number of children (NCHI), residences (place, dated
when the record is), parent-child relations, and unions (union_claims;
ADR-0036 §5): a family with two partners is a partnership, or a marriage
when a MARR of it states a wedding or a DIV or ANUL of it states its
event (a marriage with no wedding, counted), started by its weddings and
ended by its divorce or annulment. Source citations (SOUR) are folded into the
reference of each claim that the record they stand beneath gives
(ADR-0021): the event, fact or NAME a claim cites; the family record,
for its parent claims and its couple's; each MARR, DIV or ANUL, for its
event's claims and the union's claim that points to the event; the
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
is mapped depends on where it stands: a CAUS is read under a death but
not under a burial, and a TYPE under a NAME, and under a MARR only where
it names a civil or a religious ceremony, so such a tag is named with
its parent's tag where it goes unread (BURI.CAUS, MARR.TYPE). A SOUR
that the adapter folds into no claim's
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
A family has one husband and one wife, so the adapter reads a family's
first HUSB and first WIFE alone, and a further one is counted with its
parent's tag too (FAM.HUSB, FAM.WIFE); a family with no WIFE and two
HUSBs, or no HUSB and two WIFEs, has those two as its couple, and its
union is counted (FAM two HUSB/WIFE). A family's MARR, DIV and ANUL are
events of the union of its couple, so in a family with one partner or
none (its HUSB or its WIFE is missing, or names no person the adapter
maps) they are counted as MARR, DIV and ANUL: no claim names the other
partner, and a claim has no unknown value to name them with. A MARR, a
DIV or an ANUL states its event as a DEAT does (below); one that states
none (a MARR N, a bare DIV) is counted as MARR, DIV or ANUL, and a
family whose MARRs, DIVs and ANULs state none is a partnership; one
whose HUSB and WIFE name the same person gives no union and no parent
claim, counted. A DEAT states the death
only as GEDCOM 5.5.1 has an event asserted, by a DATE or PLAC with a
value (a DATE the grammar cannot parse, only when the DEAT's own line
has none), or else by a Y on that line: one that states none (a bare
DEAT, or one with only a SOUR, NOTE, AGE or CAUS) gives no claim, since
a death event or a cause of death would mark the person as not possibly
living (ADR-0020), and is counted as DEAT. A person's label is the
first name form a NAME gives; where none gives one, the brackets that
hold a word of a NAME's value give it, then a GIVN's quotes or
brackets, then a NICK, then a `_MARNM`. A person none of these gives a
word (a placeholder with no letter or digit, '?' or '_', gives none) is
skipped, with a warning, counted, and the family links that name them
give no claim.

The texts the adapter writes (excerpts, names, causes of death) are in
the language HEAD.LANG names, mapped to BCP 47 (GEDCOM_LANGUAGES) or
already one of the map's codes or `und`, or undetermined (`und`) where
it names none the map knows. What it maps in a way worth reporting is
counted in the outcomes (AdapterResult): each union and union event by
kind, an undated divorce or annulment, a marriage with no wedding, a
couple of two HUSBs or two WIFEs, a name part kept as an unsplit form or
counted rather than placed (one with no word among them), and an
undetermined language.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    _occurs,
    _words,
    structure_problems,
    validate_against_manifest,
)
from yizkr.pipeline.extract import run_metadata
from yizkr.pipeline.names import normalize_aliases

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


def _has_word(text: str) -> bool:
    """Whether `text` holds a letter or a digit. A part that holds none
    ('?', '-', '…', '_') is a placeholder for an unknown name or part,
    and names nothing."""
    return bool(re.search(r"[^\W_]", text))


def _bracket_pairs(text: str) -> list[tuple[int, int]]:
    """The span of each pair of brackets in `text`, a '(' and the ')' that
    closes it, nested pairs included."""
    pairs: list[tuple[int, int]] = []
    opened: list[int] = []
    for index, char in enumerate(text):
        if char == "(":
            opened.append(index)
        elif char == ")" and opened:
            pairs.append((opened.pop(), index + 1))
    return pairs


def _bracket_spans(text: str) -> list[tuple[int, int]]:
    """The spans of `text`'s outermost brackets, each from its '(' to the
    ')' that closes it: a bracket nested in another ('(Pep(pi))') is read
    within it, and a '(' never closed, or a ')' never opened, is none, so
    that a part of the text reads its brackets as the whole text does."""
    pairs = _bracket_pairs(text)
    return sorted(
        (start, end)
        for start, end in pairs
        if not any(
            outer < start and end < outer_end for outer, outer_end in pairs
        )
    )


def _brackets(text: str) -> list[str]:
    """What `text`'s outermost brackets hold, each less the brackets
    nested in it ('(Pep(pi))' -> 'Peppi')."""
    return [
        re.sub(r"[()]", "", text[start + 1 : end - 1])
        for start, end in _bracket_spans(text)
    ]


def _sub_brackets(text: str, replace: Callable[[str], str]) -> str:
    """`text` with each outermost bracket replaced by `replace` of what
    it holds (_brackets)."""
    out: list[str] = []
    last = 0
    for (start, end), held in zip(
        _bracket_spans(text), _brackets(text), strict=True
    ):
        out += [text[last:start], replace(held)]
        last = end
    return "".join([*out, text[last:]])


# a quote whose only content is a bracket ("'(Peppi)'"): the bracket is
# read, and the quote marks around it are no placeholder
_QUOTED_BRACKET = re.compile(r"[‘'\"“]\s*(\([^()]*\))\s*[’'\"”]")
# a word with no letter or digit that joins the words beside it ('Löbig -
# Sörne', 'Eugen & Ida'), kept as written where it stands between two
_CONNECTOR = re.compile(r"[-‐‑‒–—&+]+")


def _unquote_brackets(text: str) -> str:
    """`text` with the quote marks around a bracket that is a quote's only
    content left out ("Ida '(Peppi)'" -> 'Ida (Peppi)')."""
    return _QUOTED_BRACKET.sub(r" \1 ", text)


def _stray_brackets(text: str) -> tuple[str, int]:
    """`text` less each '(' never closed and each ')' never opened ('Ida
    (Pep' -> 'Ida Pep'), and how many it leaves out."""
    paired = {
        index
        for start, end in _bracket_pairs(text)
        for index in (start, end - 1)
    }
    stray = [
        index
        for index, char in enumerate(text)
        if char in "()" and index not in paired
    ]
    out = "".join(
        " " if index in stray else char for index, char in enumerate(text)
    )
    return out, len(stray)


def _kept_words(words: list[str]) -> list[str]:
    """`words` less each that holds no letter or digit, save a connector
    between two that do ('Löbig - Sörne')."""
    return [
        word
        for index, word in enumerate(words)
        if _has_word(word)
        or (
            _CONNECTOR.fullmatch(word)
            and 0 < index < len(words) - 1
            and _has_word(words[index - 1])
            and _has_word(words[index + 1])
        )
    ]


def _worded_count(text: str) -> tuple[str, int]:
    """`text`, cleaned, less each of its words that holds no letter or
    digit ('Eugen ?' -> 'Eugen') and each stray bracket, and how many it
    leaves out. A connector between two words stays ('Löbig - Sörne'). A
    bracket or a quote keeps its marks around the words left in it
    ('(Peppi ?)' -> '(Peppi)'), and one with none left goes whole ('(?)',
    "'?'", '()'), counted once at least; the quote marks around a bracket
    that is a quote's only content are left out, uncounted."""
    text, dropped = _stray_brackets(_unquote_brackets(text))

    def within(held: str, opening: str, closing: str) -> str:
        nonlocal dropped
        words = _clean_name(held).split()
        kept = _kept_words(words)
        if not kept:
            dropped += max(len(words), 1)
            return " "
        dropped += len(words) - len(kept)
        return f" {opening}{' '.join(kept)}{closing} "

    text = _sub_brackets(text, lambda held: within(held, "(", ")"))
    text = _QUOTED_NAME.sub(
        lambda quote: within(
            quote.group(1), quote.group(0)[0], quote.group(0)[-1]
        ),
        text,
    )
    words = _clean_name(text).split()
    kept = _kept_words(words)
    return " ".join(kept), dropped + len(words) - len(kept)


def _worded(text: str) -> str:
    """`text` less its placeholders (_worded_count)."""
    return _worded_count(text)[0]


def _name_pieces(name: Record) -> tuple[dict[str, list[str]], int]:
    """Each part tag of a NAME (_NAME_PARTS) -> its comma-separated
    pieces, cleaned and less their placeholders (_worded_count), in
    order, a piece left with no word dropped; and how many placeholders
    they held. A GIVN leaves out its quoted and bracketed parts, which
    read_name reads apart (_givn_inline), before it is split at its
    commas."""
    found: dict[str, list[str]] = {}
    dropped = 0
    for tag in _NAME_PARTS:
        found[tag] = []
        for part in name.all(tag):
            value = _unquote_brackets(part.value)
            if tag == "GIVN":
                value = _QUOTED_NAME.sub(
                    " ", _sub_brackets(value, lambda held: " ")
                )
            for piece in value.split(","):
                text, count = _worded_count(piece)
                dropped += count
                if text:
                    found[tag].append(text)
    return found, dropped


def _givn_inline(name: Record) -> tuple[list[str], list[str], int]:
    """What a NAME's GIVNs hold in quotes ("Ida 'Peppi'" -> 'Peppi') and in
    brackets ('Ida (Peppi)' -> 'Peppi'), each less its placeholders and
    kept only if a word is left, and how many placeholders they held. As
    in a NAME's value, a quoted part is a nickname, and a bracketed part,
    which may hold a nickname, a maiden or a later name or a note, is an
    unsplit form (read_name)."""
    quoted: list[str] = []
    bracketed: list[str] = []
    dropped = 0
    for part in name.all("GIVN"):
        value = _unquote_brackets(part.value)
        unbracketed = _sub_brackets(value, lambda held: " ")
        for found, inline in (
            (bracketed, _brackets(value)),
            (quoted, _QUOTED_NAME.findall(unbracketed)),
        ):
            for held in inline:
                text, count = _worded_count(held)
                if text:
                    found.append(text)
                    dropped += count
                else:
                    dropped += max(count, 1)
    return quoted, bracketed, dropped


def _parts_text(pieces: dict[str, list[str]]) -> str:
    """The name a NAME's parts give, in the order GEDCOM writes a name,
    each part's pieces in turn (a GIVN 'Eugen' and a SURN 'Löbig' give
    'Eugen Löbig'), provided a given name or a surname is among them: a
    prefix or a suffix alone ('Wwe.') names no one, and the writer, which
    finds an item by its label, would make everyone it stood for one
    item. Empty when they give none."""
    if not (pieces["GIVN"] or pieces["SURN"]):
        return ""
    return " ".join(
        [
            *pieces["NPFX"],
            *pieces["GIVN"],
            *(_with_particle(pieces) or pieces["SPFX"]),
            *pieces["NSFX"],
        ]
    )


def _with_particle(pieces: dict[str, list[str]]) -> list[str]:
    """Each SURN piece whole with the SPFX particle before it, unless it
    holds it already ('von' + 'Löbig, Sörne' -> 'von Löbig', 'von
    Sörne')."""
    particle = " ".join(pieces["SPFX"])
    return [
        surname
        if not particle or _restated(particle, surname)
        else f"{particle} {surname}"
        for surname in pieces["SURN"]
    ]


# The inline convention of the people who keep trees, the one heuristic
# the adapter keeps for names (ADR-0035 §6, §10): a quoted part of a NAME
# value marks a nickname ("Cäcilie 'Cilli' Tarle"), and a bracketed part
# may hold a nickname, a maiden or a later name, or a note ("Ida (Peppi)
# Sörne", "(geb. Laudler)"), so it is kept as an alias and an unsplit
# form, typed as nothing. A quote opens a word and closes one, so an
# apostrophe inside a name ("O'Neil") is no quote.
_QUOTED_NAME = re.compile(r"(?:(?<=\s)|^)[‘'\"“]([^‘’'\"“”]+)[’'\"”](?=\s|$)")
# a NAME value's given part, its surname between slashes, and what follows
_SLASHED_NAME = re.compile(r"^([^/]*)/([^/]*)/([^/]*)$")


def _name_display(value: str) -> str:
    """A NAME value's display form: cleaned, without its bracketed parts
    and its placeholders ('Ida (Peppi) /Sörne/' -> 'Ida Sörne', 'Eugen
    /?/' -> 'Eugen')."""
    return _worded(_sub_brackets(_unquote_brackets(value), lambda held: " "))


def _name_text(name: Record) -> str:
    """A NAME's display form: its value's (_name_display), or, when the
    value gives none (it is empty, '//', '?', or brackets alone), the
    name its parts give (_parts_text). Empty when neither gives a name."""
    return _name_display(name.value) or _parts_text(_name_pieces(name)[0])


def _bracket_text(name: Record) -> str:
    """The display form of a NAME whose value gives a name only in
    brackets that hold a word ('(Peppi)'): the value, cleaned, less its
    placeholders and its brackets that hold no word (_worded_count). A
    person's label falls back to it only when none of the person's NAMEs
    gives a name otherwise (_name_text), where no word of the value stands
    outside its brackets, so that a person named only so keeps an entity.
    Empty when no bracket holds a word."""
    return _worded(name.value)


# GEDCOM NAME TYPE -> the surname type of the NAME's mention record
# (ADR-0035 §6); any other TYPE, or none, gives `other`
_NAME_TYPE_SURNAME = {
    "birth_name": "birth",
    "married_name": "married",
}


@dataclass
class NameReading:
    """What one NAME, or one `_MARNM`, gives a person's mention record
    (ADR-0035 §6): the fields it fills, the name forms that carry them
    (its display form, the name its parts give where the value does not
    restate them, each bracketed part), whether its TYPEs give typed-name
    claims, and what it counts."""

    display: str = ""
    forms: list[str] = field(default_factory=list)
    given: list[str] = field(default_factory=list)
    surnames: list[tuple[str, str]] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    # the nicknames a quoted part of the value gives, and of a GIVN
    quoted: list[str] = field(default_factory=list)
    givn_quoted: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    unsplit: list[str] = field(default_factory=list)
    typed: bool = True
    counts: list[str] = field(default_factory=list)


def _restated(part: str, text: str) -> bool:
    """Whether `text` holds `part`, folded, as whole words."""
    return _occurs(part, [_words(text)])


def _prefix_length(prefix: str, words: list[str]) -> int:
    """How many of `words` the prefix `prefix` is, folded ('Dr med' is
    'Dr. med.'), from the first; 0 when they do not start with it."""
    wanted = _words(prefix)
    for length in range(1, len(words) + 1):
        if _words(" ".join(words[:length])) == wanted:
            return length
    return 0


def _stated_lead(prefixes: list[str], display: str) -> int:
    """How many of the display form's first words the prefixes already
    write, at their start ('Dr.' of 'Dr. med.' in 'Dr. Eugen Löbig') or at
    their end ('Eugen' of 'Dr. Eugen' in 'Eugen Löbig', all of 'Eugen
    Löbig' in 'Dr. Eugen Löbig'), so that the form an unrestated prefix
    completes does not write them twice."""
    wanted = _words(" ".join(prefixes))
    words = display.split()
    lead = 0
    for length in range(1, len(words) + 1):
        stated = _words(" ".join(words[:length]))
        if stated and stated in (
            wanted[: len(stated)],
            wanted[len(wanted) - len(stated) :],
        ):
            lead = length
    return lead


def read_name(name: Record) -> NameReading:
    """A NAME's reading for its person's mention record (ADR-0035 §6).

    * The value's part before the slashes gives given names, less an NPFX
      it starts with and less its quoted parts, which are nicknames; the
      part between them a surname, typed by the NAME's TYPE; the part
      after them a suffix, unless an NSFX gives the suffixes, and less its
      quoted parts, which are nicknames too. An NPFX is a title, as the
      export tags it, even where it is the whole given part ('Eugen
      /Löbig/' + NPFX Eugen), which is counted.
    * A value without slashes, where no GIVN or SURN gives the parts, is
      an `unsplit` form, and gives no typed-name claim; where only a GIVN
      or only a SURN gives its part, the value is an `unsplit` form beside
      it, counted.
    * GIVN gives given names, its quoted parts nicknames and its
      bracketed parts unsplit forms, as the value's do; SURN surnames
      (each whole with the SPFX particle before it), NPFX titles and NSFX
      suffixes; NICK and a quoted part give nicknames. Given names are
      separated by spaces, as the NAME value writes them, and pieces by
      commas.
    * TYPE birth or maiden types the surname `birth`, married or marriage
      `married`, and aka, nick or nickname makes the given names
      nicknames.
    * Each bracketed part that holds a word is an alias and an `unsplit`
      form, counted.
    * A part the value does not restate still reaches its field, and the
      name it completes (the name the parts give, or an NPFX or NSFX
      beside the name the value or its brackets give) becomes a name form
      of the person's, counted. A NAME with no value, whose parts give no
      given name or surname, names no one: its prefix or suffix is
      counted instead. So is a particle with no surname.
    * A word that holds no letter or digit ('?', '-', '_'), in the value,
      a bracket, a quote, a part or a NICK, is a placeholder: it names
      nothing, so it is left out and counted. A typed NAME that held one
      in its value or parts gives no typed-name claim, counted, since the
      name left would read as the whole of what the source types.
    """
    reading = NameReading()
    value, strays = _stray_brackets(_unquote_brackets(name.value))
    pieces, placeholders = _name_pieces(name)
    placeholders += strays
    props = [
        _NAME_TYPE_PROPERTY.get(kind.value.strip().lower())
        for kind in name.all("TYPE")
    ]
    types = list(
        dict.fromkeys(
            _NAME_TYPE_SURNAME[prop]
            for prop in props
            if prop in _NAME_TYPE_SURNAME
        )
    ) or ["other"]
    nickname_type = "nickname" in props

    # the placeholders of the value's brackets and the GIVN's quotes and
    # brackets, which count against a typed claim as the value's do
    unnamed = 0
    brackets: list[str] = []
    for held in _brackets(value):
        text, count = _worded_count(held)
        if text:
            brackets.append(text)
            reading.forms.append(text)
            reading.unsplit.append(text)
            reading.counts.append("NAME bracketed part")
            unnamed += count
        else:
            unnamed += max(count, 1)
    # a GIVN's quoted part is a nickname and its bracketed part an
    # unsplit form, as in the value
    givn_quoted, givn_bracketed, count = _givn_inline(name)
    unnamed += count
    reading.nicknames += givn_quoted
    reading.givn_quoted += givn_quoted
    reading.forms += givn_quoted + givn_bracketed
    reading.unsplit += givn_bracketed
    reading.counts += ["NAME bracketed part"] * len(givn_bracketed)
    bare = _sub_brackets(value, lambda held: " ")
    display, count = _worded_count(bare)
    placeholders += count
    given: list[str] = []
    surname = ""
    quoted: list[str] = []
    slashed = _SLASHED_NAME.match(bare)
    if slashed:
        before, surname, after = slashed.groups()
        quoted += _QUOTED_NAME.findall(before) + _QUOTED_NAME.findall(after)
        words = [
            word
            for word in _clean_name(_QUOTED_NAME.sub(" ", before)).split()
            if _has_word(word)
        ]
        surname = _worded(surname)
        # each run of words between the quotes after the slashes is a
        # suffix of its own ('Jr. 'Muckel' Sr.' gives 'Jr.' and 'Sr.')
        afters = [
            text
            for run in _QUOTED_NAME.sub("\0", after).split("\0")
            if (text := _worded(run))
        ]
        for prefix in pieces["NPFX"]:
            length = _prefix_length(prefix, words)
            if length == len(words) and length:
                # the export tags the whole given part as a prefix
                reading.counts.append("NPFX restated as the whole given part")
            words = words[length:]
        given += words
        if surname:
            reading.surnames += [(surname, kind) for kind in types]
        if not pieces["NSFX"]:
            reading.suffixes += afters
    else:
        quoted += _QUOTED_NAME.findall(bare)
        unsplit = _worded(_QUOTED_NAME.sub(" ", bare))
        # where a GIVN and a SURN give the parts, they say which is which;
        # where only one of them does, the value stays an unsplit form
        # beside it, so that no word the part does not place is lost
        if unsplit and not (pieces["GIVN"] and pieces["SURN"]):
            reading.unsplit.append(unsplit)
            if not _restated(unsplit, display):
                # a quoted word between its words: the form without it
                reading.forms.append(unsplit)
            if pieces["GIVN"] or pieces["SURN"]:
                reading.counts.append("NAME without slashes, one part given")
            else:
                reading.typed = False
                reading.counts.append(
                    "NAME slashes not one pair"
                    if "/" in bare
                    else "NAME without slashes"
                )

    # the parts, each a piece of its field
    parts_given = [word for piece in pieces["GIVN"] for word in piece.split()]
    particle = " ".join(pieces["SPFX"])
    parts_surnames = _with_particle(pieces)
    # a particle the value's surname holds has its surname there
    if pieces["SPFX"] and not (
        pieces["SURN"] or (surname and _restated(particle, surname))
    ):
        reading.counts.append("SPFX without a surname")
    parts_text = _parts_text(pieces)
    # each part as its field holds it, a surname with its particle
    held = [
        *pieces["NPFX"],
        *pieces["GIVN"],
        *(parts_surnames or pieces["SPFX"]),
        *pieces["NSFX"],
    ]
    unrestated = [
        piece for piece in held if not (display and _restated(piece, display))
    ]
    # the name a prefix or a suffix beside the value stands with: the
    # value's own, or else what its brackets give
    named = display or " ".join(brackets)
    if parts_text:
        given += parts_given
        reading.surnames += [
            (s, kind) for s in parts_surnames for kind in types
        ]
        reading.titles += pieces["NPFX"]
        reading.suffixes += pieces["NSFX"]
        if display and unrestated:
            reading.forms.append(parts_text)
            reading.counts.append("NAME parts not restated")
    elif named:
        # a name stands beside a prefix or a suffix, which reaches its
        # field whether the value restates it or not; the name an
        # unrestated one completes is a name form of the person's,
        # counted, less the words of the name that the prefixes already
        # write. A particle is counted as one with no surname.
        prefixes = [p for p in pieces["NPFX"] if not _restated(p, named)]
        suffixes = [p for p in pieces["NSFX"] if not _restated(p, named)]
        reading.titles += pieces["NPFX"]
        reading.suffixes += pieces["NSFX"]
        if prefixes or suffixes:
            rest = named.split()[_stated_lead(prefixes, named) :]
            reading.forms.append(" ".join([*prefixes, *rest, *suffixes]))
            reading.counts.append("NAME parts not restated")
    else:
        # a NAME with no value and no given name or surname among its
        # parts names no one: its prefix or suffix is counted, and a
        # particle as one with no surname
        for piece in unrestated:
            if piece not in pieces["SPFX"]:
                reading.counts.append("NAME part without a name")
    reading.display = display or parts_text
    # each quote's placeholders are counted with the value's
    reading.quoted = [text for text in map(_worded, quoted) if text]
    reading.nicknames += reading.quoted
    if nickname_type:
        reading.nicknames += given
    else:
        reading.given += given
    # a NICK's placeholders are counted, but a NICK is no part of the
    # name a TYPE types
    nick_placeholders = 0
    for nick in name.all("NICK"):
        text, count = _nick_count(nick.value)
        nick_placeholders += count
        if text:
            reading.nicknames.append(text)
            reading.forms.append(text)
    if (placeholders or unnamed) and any(props):
        # a placeholder anywhere in what the source types, its value, its
        # brackets and quotes or its parts ('Eugen (?) /Löbig/'), says the
        # source is unsure of the name: no typed claim narrows it
        reading.typed = False
        reading.counts.append("typed NAME with a placeholder")
    reading.counts += ["NAME part without a word"] * (
        placeholders + unnamed + nick_placeholders
    )
    return reading


# the quotes that may enclose a NICK's value, opening -> closing
_QUOTE_PAIRS = {"'": "'", '"': '"', "‘": "’", "“": "”"}


def _nick_count(value: str) -> tuple[str, int]:
    """A NICK's nickname: its value, whitespace collapsed, less one pair
    of matching quotes that encloses it ("'Cilli'" -> 'Cilli'), as a
    quoted part of a NAME value gives it, and less its placeholders
    (_worded_count), which are counted; empty when none of it is a word
    ("''", '?'), as such a NICK names nothing."""
    text = " ".join(value.split())
    if len(text) > 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1]
    if not text.strip():
        # a NICK of nothing but quote marks (' ') names nothing
        return "", 1 if value.strip() else 0
    return _worded_count(text)


def _nick_text(value: str) -> str:
    """A NICK's nickname (_nick_count)."""
    return _nick_count(value)[0]


def read_married_name(marnm: Record) -> NameReading:
    """A `_MARNM`'s reading (ADR-0035 §6): the part between its slashes is
    a `married` surname; a value without them, or with no word between
    them, is `unsplit`, counted, and gives no typed-name claim, as a NAME
    without slashes gives none. Either way its display form, its brackets
    left out but not what they hold ('(Peppi)' -> 'Peppi'), is a name
    form of the person's. Its placeholders ('?') are left out and
    counted; one whose slashes still hold a surname but that held a
    placeholder gives no typed-name claim, counted (`typed NAME with a
    placeholder`), while one whose surname was a placeholder ('Agnes
    /?/') is unsplit, counted as without a surname."""
    unbracketed = _sub_brackets(marnm.value, lambda held: f" {held} ")
    display, placeholders = _worded_count(unbracketed)
    reading = NameReading(display=display)
    reading.counts += ["NAME part without a word"] * placeholders
    if not reading.display:
        return reading
    reading.forms.append(reading.display)
    slashed = _SLASHED_NAME.match(unbracketed)
    if slashed and (surname := _worded(slashed.group(2))):
        reading.surnames.append((surname, "married"))
        if placeholders:
            reading.typed = False
            reading.counts.append("typed NAME with a placeholder")
    else:
        reading.unsplit.append(reading.display)
        reading.typed = False
        reading.counts.append(
            "_MARNM without a surname" if slashed else "_MARNM without slashes"
        )
    return reading


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

# GEDCOM 5.5.1's language names (LANGUAGE_ID) -> BCP 47 codes, for the
# language of the adapter's texts (ADR-0035 §6); a HEAD.LANG that is
# already one of these codes is taken as it is. A name left out here
# (Braj, Dogri, Lahnda, Mewari, Pahari, Prakrit, Rajasthani, Wendic) has no
# code the instance is known to take for a text, so it gives UNDETERMINED,
# counted, as an absent HEAD.LANG does.
GEDCOM_LANGUAGES = {
    "afrikaans": "af", "albanian": "sq", "anglo-saxon": "ang",
    "catalan": "ca", "catalan_spn": "ca", "czech": "cs", "danish": "da",
    "dutch": "nl", "english": "en", "esperanto": "eo", "estonian": "et",
    "faroese": "fo", "finnish": "fi", "french": "fr", "german": "de",
    "hawaiian": "haw", "hungarian": "hu", "icelandic": "is",
    "indonesian": "id", "italian": "it", "latvian": "lv",
    "lithuanian": "lt", "navaho": "nv", "norwegian": "no", "polish": "pl",
    "portuguese": "pt", "romanian": "ro", "serbo_croa": "sh",
    "slovak": "sk", "slovene": "sl", "spanish": "es", "swedish": "sv",
    "turkish": "tr", "amharic": "am", "arabic": "ar", "armenian": "hy",
    "assamese": "as", "belorusian": "be", "bengali": "bn",
    "bulgarian": "bg", "burmese": "my", "cantonese": "yue",
    "church-slavic": "cu", "georgian": "ka", "greek": "el",
    "gujarati": "gu", "hebrew": "he", "hindi": "hi", "japanese": "ja",
    "kannada": "kn", "khmer": "km", "konkani": "kok", "korean": "ko",
    "lao": "lo", "macedonian": "mk", "maithili": "mai", "malayalam": "ml",
    "mandrin": "zh", "manipuri": "mni", "marathi": "mr", "nepali": "ne",
    "oriya": "or", "pali": "pi", "panjabi": "pa", "persian": "fa",
    "pusto": "ps", "russian": "ru", "sanskrit": "sa", "serb": "sr",
    "tagalog": "tl", "tamil": "ta", "telugu": "te", "thai": "th",
    "tibetan": "bo", "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi",
    "yiddish": "yi",
}  # fmt: skip
# the language of a text whose language the export does not state
UNDETERMINED = "und"

# A MARR TYPE that names a civil or a religious ceremony -> the wedding's
# kind (ADR-0036 §5). Any other TYPE stays in the excerpt and is counted
# (MARR.TYPE), and the wedding's kind stays unstated.
WEDDING_TYPES = {
    "civil": "civil_wedding",
    "civil ceremony": "civil_wedding",
    "civil marriage": "civil_wedding",
    "standesamt": "civil_wedding",
    "standesamtlich": "civil_wedding",
    "religious": "religious_wedding",
    "religious ceremony": "religious_wedding",
    "religious marriage": "religious_wedding",
    "church": "religious_wedding",
    "church wedding": "religious_wedding",
    "kirchlich": "religious_wedding",
}


def wedding_type(marriage: Record) -> Record | None:
    """The TYPE of a MARR that gives its wedding's kind: the first that
    names a civil or a religious ceremony (WEDDING_TYPES), or None."""
    return next(
        (
            kind
            for kind in marriage.all("TYPE")
            if " ".join(kind.value.split()).lower() in WEDDING_TYPES
        ),
        None,
    )


# each union event's kind -> the words its label starts with; an end of
# unstated kind (an `event`) is labelled as the end of its union
EVENT_WORDS = {
    "wedding": "Wedding",
    "civil_wedding": "Civil wedding",
    "religious_wedding": "Religious wedding",
    "divorce": "Divorce",
    "annulment": "Annulment",
}


@dataclass
class AdapterResult:
    """The batch, the warnings, the census of what the adapter does not
    map (`unsupported_tags`), and the count of each outcome it maps in a
    way worth reporting (`outcomes`): a union and each of its events by
    kind, a name part kept as an unsplit form or counted, and a text
    language the export does not state."""

    batch: ClaimBatch
    warnings: list[str]
    unsupported_tags: dict[str, int]
    outcomes: dict[str, int] = field(default_factory=dict)


def format_unsupported_tags(unsupported: dict[str, int]) -> str:
    """One-line census for logs: ``EDUC ×1, NOTE ×1``, for the unsupported
    tags or, alike, the outcomes.

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
    # is skipped, so `people` holds the xrefs of those the batch holds.
    # Where no NAME gives one, the next of these that gives a word does,
    # as the source writes it, quote marks and brackets kept, so that a
    # person with any word in any name keeps an entity and meets no one
    # whose name is that word unmarked: the brackets of a NAME's value
    # ('(Peppi)'), its GIVN ("'Peppi'"), its NICKs, then the person's
    # `_MARNM`s (ADR-0035 §6).
    def named(record: Record) -> list[str]:
        names = record.all("NAME")
        return (
            [text for name in names if (text := _name_text(name))]
            or [text for name in names if (text := _bracket_text(name))]
            or [
                text
                for name in names
                for part in name.all("GIVN")
                if (text := _worded(part.value))
            ]
            or [
                text
                for name in names
                for nick in name.all("NICK")
                if (text := _worded(nick.value))
            ]
            or [
                text
                for marnm in record.all("_MARNM")
                if (text := _worded(marnm.value))
            ]
        )

    names_of = {xref: named(record) for xref, record in individuals.items()}
    people = {xref for xref, names in names_of.items() if names}
    warnings: list[str] = []
    unsupported: dict[str, int] = {}
    outcomes: dict[str, int] = {}

    def outcome(key: str) -> None:
        outcomes[key] = outcomes.get(key, 0) + 1

    # the language of the adapter's texts: HEAD.LANG's, mapped to BCP 47,
    # or taken as it is where it is already one of the map's codes ('de')
    # or `und`, or undetermined where the export states none the map knows
    head = next((root for root in roots if root.tag == "HEAD"), None)
    stated_language = " ".join(head.text("LANG").split()) if head else ""
    codes = {*GEDCOM_LANGUAGES.values(), UNDETERMINED}
    language = GEDCOM_LANGUAGES.get(stated_language.lower()) or (
        stated_language.lower()
        if stated_language.lower() in codes
        else UNDETERMINED
    )
    if not stated_language:
        outcome("HEAD.LANG absent: und")
    elif language == UNDETERMINED and stated_language.lower() != language:
        outcome("HEAD.LANG unmapped: und")

    HANDLED_INDI = {
        "NAME", "_MARNM", "SEX", "BIRT", "DEAT", "BURI", "OCCU", "NCHI",
        "RESI", "FAMC", "FAMS",
    }  # fmt: skip
    HANDLED_FAM = {"HUSB", "WIFE", "CHIL", "MARR", "DIV", "ANUL"}
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
    # record carries that the model has no property for (a birth's AGE, a
    # family's EVEN). A tag read where it stands can still go unread for
    # what one occurrence says (`unread`): it is counted then too.
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
        "MARR": {"DATE", "PLAC", "TYPE"},
        "DIV": {"DATE", "PLAC"},
        "ANUL": {"DATE", "PLAC"},
    }
    # the tags the adapter reads only the first of beneath a tag: a family
    # has one husband and one wife, save one whose couple are two HUSBs or
    # two WIFEs (partners), whose second is read too
    reads_once = {"FAM": {"HUSB", "WIFE"}}
    # the tags each tag is read beneath
    read_beneath: dict[str, set[str]] = {}
    for parent, tags in reads.items():
        for tag in tags:
            read_beneath.setdefault(tag, set()).add(parent)
    # a SOUR is folded beneath the records that claims cite, a family's
    # union events among them, and an OBJE is mapped beneath a person
    # alone, so one that is not is named with its parent's tag (DATE.SOUR,
    # FAM.OBJE) where the other tags hold it
    read_beneath["SOUR"] = {
        "NAME", "_MARNM", "SEX", "BIRT", "DEAT", "BURI", "OCCU", "NCHI",
        "RESI", "OBJE", "FAM", "MARR", "DIV", "ANUL",
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

    def partners(fam: Record) -> list[Record]:
        """The HUSB and WIFE lines that give a family's union its couple:
        its first HUSB and first WIFE, or, in a family with no WIFE (or no
        HUSB), its first two HUSBs (or WIFEs). Empty unless they name two
        people, each a person the batch holds."""
        husbands, wives = fam.all("HUSB"), fam.all("WIFE")
        pair = (
            [husbands[0], wives[0]]
            if husbands and wives
            else (husbands or wives)[:2]
        )
        if len(pair) < 2 or pair[0].value == pair[1].value:
            return []
        return pair if all(part.value in people for part in pair) else []

    def coupled(fam: Record) -> bool:
        """Whether a family's union has a couple (partners)."""
        return bool(partners(fam))

    def states_event(death: Record) -> bool:
        """Whether an event record (a DEAT, a MARR, a DIV, an ANUL) states that
        the event happened. GEDCOM 5.5.1 has an event asserted by a DATE or a
        PLAC with a value, or else by a Y on the event's own line, and lets a
        SOUR or a NOTE be attached to an event without asserting it. A DATE the
        grammar parses, or a PLAC, states it whatever that line says, as each
        gives a claim of its own; a DATE the grammar cannot parse states it
        only when the line has no value, so that a date the adapter cannot read
        never overrules another value, such as an N denying the event. What
        else it holds (a CAUS, an AGE, a SOUR, a NOTE) states nothing: a death
        event or a cause of death is death evidence, which marks the person as
        not possibly living (ADR-0020), a wedding makes a marriage, and a
        divorce or an annulment ends a union."""
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
        if child.tag in ("DEAT", "MARR", "DIV", "ANUL") and not states_event(
            child
        ):
            # an event record that states no event gives no claim
            return True
        if parent.tag == "MARR" and child.tag == "TYPE":
            # a MARR's TYPE is read where it gives the wedding's kind
            return wedding_type(parent) is not child
        # a family's MARR, DIV and ANUL are events of its union: with one
        # partner or none, it has no couple, and a claim has no unknown
        # value to name the other with
        return child.tag in ("MARR", "DIV", "ANUL") and not coupled(parent)

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
        # the lines of a family's couple, each read where it stands
        couple = (
            {id(part) for part in partners(record)}
            if (record.tag == "FAM")
            else set()
        )
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
            # only a family's first HUSB and first WIFE are read, or its
            # couple's two HUSBs or two WIFEs
            further = (
                child.tag in seen
                and child.tag in reads_once.get(record.tag, set())
                and id(child) not in couple
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
    # the ids the batch's entities hold: a person's is its xref's, and
    # every other entity takes one none of them holds (fresh), so that an
    # xref shaped like another entity's id ('@F1-WEDDING@', '@PL1@')
    # never puts one entity's claims on another
    taken: set[str] = {xref.strip("@").lower() for xref in people}

    def fresh(candidate: str) -> str:
        """`candidate`, or, where an entity holds it, the first of
        `candidate-2`, `candidate-3`... that none does."""
        local_id, number = candidate, 1
        while local_id in taken:
            number += 1
            local_id = f"{candidate}-{number}"
        taken.add(local_id)
        return local_id

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
                "language": language,
            },
        }
        if pages:
            # the citation's PAGE is the reference's locator (ADR-0003)
            reference["page_number"] = _page_number(pages)
        return reference

    def person_id(xref: str) -> str:
        return xref.strip("@").lower()

    def place_entity(name: str) -> str:
        """A place's entity, labelled by its PLAC. Its mention record
        (ADR-0035 §6) takes the PLAC's first non-empty component as the
        place's name and the rest as the places it is within, each as
        written: GEDCOM lists jurisdictions from the lowest."""
        if name not in place_ids:
            place_ids[name] = fresh(f"pl{len(place_ids) + 1}")
            own, *within = [
                part for piece in name.split(",") if (part := piece.strip())
            ]
            entities.append(
                {
                    "id": place_ids[name],
                    "label": name,
                    "kind": "place",
                    "summary": "place named in the GEDCOM export",
                    "structure": {
                        "kind": "place",
                        "names": [{"written": own}],
                        "within": within,
                    },
                }
            )
        return place_ids[name]

    def occupation_entity(name: str) -> str:
        if name not in occupation_ids:
            occupation_ids[name] = fresh(f"oc{len(occupation_ids) + 1}")
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
            media_ids[key] = fresh(f"md{len(media_ids) + 1}")
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
    # its own
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
                # one circa stands for both ends, as on Wikidata's spouse
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
            death_ids[xref] = fresh(f"{person_id(xref)}-death")
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

    def role(key: str) -> dict:
        """The object_has_role qualifier of a participant role."""
        return {
            "property": "object_has_role",
            "value": {"type": "item", "value": {"kind": "vocab", "key": key}},
        }

    def died(xref: str, label: str, death: Record) -> None:
        """A DEAT that states the death (`states_event`) but gives no
        date, place or cause, by its Y alone or by a DATE the grammar
        cannot parse, says that the person died and nothing else: a
        `death` event with the person as its principal (ADR-0020 §2,
        ADR-0035 §3.2, docs/MODELING.md "Living persons and privacy"),
        which the participant claim states, citing this DEAT."""
        claim(
            death_event(xref, label, death),
            "participant",
            {"type": "item", "value": local(person_id(xref))},
            death,
            [role("principal")],
        )

    def monolingual(text: str) -> dict:
        return {
            "type": "monolingual",
            "value": {"text": text, "language": language},
        }

    def name_claims(xref: str, record: Record) -> None:
        """Typed, referenced name claims (docs/MODELING.md "Names").

        The item's label/aliases remain the flat match index; this adds
        the source's OWN name typing (NAME TYPE, NICK, _MARNM) plus the
        inline convention's quoted nickname, each attributed to the
        GEDCOM record that states it. A nickname that several NAMEs give is
        thus a claim of each, with its own citation, while one NAME that
        gives it twice (a NICK and its quoted form) gives the one claim. A
        NAME without slashes, whose parts no GIVN or SURN gives, is an
        unsplit form, and its TYPE gives no claim; a bracketed part gives
        none either (read_name), and a `_MARNM` gives a married-name claim
        only where its slashes hold a surname (read_married_name).
        """

        def add_nickname(text: str, name_record: Record) -> None:
            if text:
                claim(
                    person_id(xref), "nickname", monolingual(text), name_record
                )

        for name_record in record.all("NAME"):
            reading = read_name(name_record)
            cleaned = reading.display
            # a further TYPE types the name a further way, a claim of its
            # own; a NAME that gives no name has none to type
            typing = (
                name_record.all("TYPE") if cleaned and reading.typed else []
            )
            for name_type in typing:
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
                add_nickname(_nick_text(nick.value), name_record)
            for quoted in reading.quoted:
                add_nickname(quoted, name_record)
            # a GIVN's quoted part is a nickname as a NICK is; its
            # bracketed part, an unsplit form, gives no claim
            for quoted in reading.givn_quoted:
                add_nickname(quoted, name_record)
        # non-standard married-name tag some tools emit at the INDI level
        for marnm in record.all("_MARNM"):
            married = read_married_name(marnm)
            if married.display and married.typed:
                claim(
                    person_id(xref),
                    "married_name",
                    monolingual(married.display),
                    marnm,
                )

    def person_entity(xref: str, record: Record, summary: str) -> dict:
        """A person's entity: its label, the first name form its NAMEs
        give; its aliases, every other name form of its NAMEs and
        `_MARNM`s (their display forms, the names their parts give where
        a value does not restate them, their bracketed parts and NICKs),
        hygiened as the LLM path's are (ADR-0022 §4) save a form that
        alone carries a part of its record; and its mention record
        (ADR-0035 §6), whose counts join the outcomes. A part that holds
        no letter or digit ('?' in 'Eugen /?/') names nothing, so it is
        dropped and counted; a record left with no given name, surname,
        nickname or unsplit form is no record (None)."""
        readings = [read_name(name) for name in record.all("NAME")]
        readings += [read_married_name(m) for m in record.all("_MARNM")]
        names = names_of[xref]
        forms = list(
            dict.fromkeys(
                [
                    *names,
                    *(
                        form
                        for reading in readings
                        for form in reading.forms
                        if _has_word(form)
                    ),
                ]
            )
        )
        label, aliases = forms[0], forms[1:]

        def once(values, word=lambda value: value) -> list:
            """Each distinct value once, less those that hold no word,
            each of which is counted."""
            kept = []
            for value in dict.fromkeys(values):
                if _has_word(word(value)):
                    kept.append(value)
                else:
                    outcome("NAME part without a word")
            return kept

        structure: dict | None = {
            "kind": "human",
            "given": [
                {"written": given, "standard": given}
                for given in once(g for r in readings for g in r.given)
            ],
            "surnames": [
                {"written": written, "type": kind}
                for written, kind in once(
                    (pair for r in readings for pair in r.surnames),
                    word=lambda pair: pair[0],
                )
            ],
            "nicknames": once(n for r in readings for n in r.nicknames),
            "titles": once(t for r in readings for t in r.titles),
            "suffixes": once(t for r in readings for t in r.suffixes),
            "unsplit": once(u for r in readings for u in r.unsplit),
        }
        for reading in readings:
            for key in reading.counts:
                outcome(key)
        parts = [
            *(given["written"] for given in structure["given"]),
            *(surname["written"] for surname in structure["surnames"]),
            *structure["nicknames"],
            *structure["titles"],
            *structure["suffixes"],
            *structure["unsplit"],
        ]
        if not any(
            structure[key]
            for key in ("given", "surnames", "nicknames", "unsplit")
        ):
            structure = None
        # the alias hygiene drops a form that adds no match token to the
        # label; one that alone carries a part of the record stays, so
        # that the record divides the names and adds none (ADR-0035 §5)
        kept = normalize_aliases(label, aliases)
        for alias in aliases:
            carried = [label, *kept]
            if alias not in kept and any(
                _restated(part, alias)
                and not any(_restated(part, form) for form in carried)
                for part in parts
            ):
                kept.append(alias)
        return {
            "id": person_id(xref),
            "label": label,
            "aliases": kept,
            "kind": "human",
            "summary": summary,
            "mention_lines": [record.line_number],
            "structure": structure,
        }

    # the labels of the unions and their events
    union_labels: set[str] = set()
    # (a family's line, the entity's key) -> the entity's id
    union_ids: dict[tuple[int, str], str] = {}

    def union_entity(
        fam: Record, kind: str, candidate: str, label: str, record: Record
    ) -> str:
        """An entity of a family's union (the union itself or one of its
        events) of kind `kind`, made once for each family and kind, with
        the id `candidate` unless another entity holds it (fresh), and a
        label no other of them holds: the writer finds an item by its
        label, so two unions of one couple, or their events, are told
        apart by a number."""
        if (fam.line_number, kind) in union_ids:
            return union_ids[(fam.line_number, kind)]
        local_id = union_ids[(fam.line_number, kind)] = fresh(candidate)
        unique, number = label, 1
        while unique in union_labels:
            number += 1
            unique = f"{label} ({number})"
        union_labels.add(unique)
        entities.append(
            {
                "id": local_id,
                "label": unique,
                "kind": kind,
                "summary": f"{kind.replace('_', ' ')} stated in the GEDCOM "
                "export",
                "mention_lines": [record.line_number],
            }
        )
        outcome(f"union: {kind.replace('_', ' ')}")
        return local_id

    def event_date(
        event_id: str,
        date_record: Record,
        phrase: str,
        prop: str,
        record: Record,
        passage: Sequence[Record],
    ) -> bool:
        """A claim of `prop` (point_in_time, start_time or end_time) on an
        event, from one date `phrase` of `date_record`, with the range and
        circa the phrase states as the claim's own qualifiers, citing the
        event record `record`. A phrase the grammar cannot parse, a period
        among them, gives no claim and a warning, and an empty one neither.
        Whether it gave the claim."""
        if not phrase.strip():
            return False
        value, quals = (
            (None, [])
            if split_gedcom_period(phrase) is not None
            else parse_gedcom_date(phrase)
        )
        if value is None:
            warnings.append(
                f"line {date_record.line_number}: unparsed date "
                f"{date_record.value!r} — no {prop} claim emitted"
            )
            return False
        claim(event_id, prop, value, record, quals, passage=passage)
        return True

    def union_claims(fam: Record) -> None:
        """A coupled family's union and its events (ADR-0036 §5).

        * The union is a `marriage` when the family has a MARR that states
          a wedding (states_event), or a DIV or an ANUL that states its
          event, which only a marriage has (with no wedding, counted),
          otherwise a `partnership`; its couple
          (partners) take part in it with the role `spouse` or `partner`,
          each claim citing the family record. A couple of two HUSBs or
          two WIFEs is counted.
        * The MARRs that state a wedding, of one kind (civil or religious,
          where a TYPE names it, else unstated, which a TYPE of no known
          kind joins), are one wedding, which the union is `started_by`;
          each date and place of each is a claim of its own on the
          wedding, citing its MARR. A MARR N, or one that states nothing,
          gives none, and the census counts it.
        * The family's DIVs that state a divorce are one divorce, and its
          ANULs one annulment, which the union is `ended_by`, each PLAC the
          event's `location`; a DIV Y, or one whose DATE gives no date,
          gives an undated divorce, counted.
        * A DATE's range and circa go on that event's own date claim. A
          date period (FROM … TO …) on a MARR gives the wedding the FROM
          and an end of unstated kind (an `event`) the TO; on a DIV or an
          ANUL it is the event's own start and end time.
        * A partner's DEAT gives no end: GEDCOM does not say that the
          union lasted until the death.

        Each claim's excerpt holds the family's own line, its HUSB and
        WIFE, and the lines of the record that gives the claim; the SOUR
        citations of that record, the family's for the couple and each
        event's for its claims, are folded into its reference."""
        couple = partners(fam)
        if couple[0].tag == couple[1].tag:
            outcome("FAM two HUSB/WIFE")
        family_passage = [
            fam,
            *(node for part in couple for node in part.walk()),
        ]
        family_citations = [
            node for part in fam.all("SOUR") for node in part.walk()
        ]
        names = " and ".join(names_of[part.value][0] for part in couple)
        marriages = [
            marriage for marriage in fam.all("MARR") if states_event(marriage)
        ]
        # a divorce or an annulment is the end of a marriage, stated or not
        ended = any(
            states_event(record)
            for tag in ("DIV", "ANUL")
            for record in fam.all(tag)
        )
        kind, partner_role = (
            ("marriage", "spouse")
            if marriages or ended
            else ("partnership", "partner")
        )
        if ended and not marriages:
            outcome("union: marriage without a wedding")
        union = union_entity(
            fam,
            kind,
            person_id(fam.xref or f"@fam{fam.line_number}@"),
            f"{kind.title()} of {names}",
            fam,
        )
        for part in couple:
            claim(
                union,
                "participant",
                {"type": "item", "value": local(person_id(part.value))},
                fam,
                [role(partner_role)],
                passage=[*family_passage, *family_citations],
            )

        def event(key: str, record: Record) -> str:
            """The union's event of kind `key`, made once."""
            label = (
                f"End of the {kind} of {names}"
                if key == "event"
                else f"{EVENT_WORDS[key]} of {names}"
            )
            return union_entity(
                fam, key, f"{union}-{key.replace('_', '-')}", label, record
            )

        def linked(prop: str, event_id: str, record: Record, passage) -> None:
            claim(
                union,
                prop,
                {"type": "item", "value": local(event_id)},
                record,
                passage=passage,
            )

        for marriage in marriages:
            passage = [*family_passage, *marriage.walk()]
            typed = wedding_type(marriage)
            wedding = event(
                WEDDING_TYPES[" ".join(typed.value.split()).lower()]
                if typed
                else "wedding",
                marriage,
            )
            linked("started_by", wedding, marriage, passage)
            for date_record in marriage.all("DATE"):
                phrases = split_gedcom_period(date_record.value)
                if phrases is None:
                    event_date(
                        wedding,
                        date_record,
                        date_record.value,
                        "point_in_time",
                        marriage,
                        passage,
                    )
                    continue
                start, end = phrases
                if start is not None:
                    event_date(
                        wedding,
                        date_record,
                        start,
                        "point_in_time",
                        marriage,
                        passage,
                    )
                if end is not None:
                    ending = event("event", marriage)
                    linked("ended_by", ending, marriage, passage)
                    event_date(
                        ending,
                        date_record,
                        end,
                        "point_in_time",
                        marriage,
                        passage,
                    )
            for place in places(marriage):
                claim(
                    wedding,
                    "location",
                    place_item(place),
                    marriage,
                    passage=passage,
                )
        for tag, key in (("DIV", "divorce"), ("ANUL", "annulment")):
            for ended in fam.all(tag):
                if not states_event(ended):
                    continue  # the census counts it
                passage = [*family_passage, *ended.walk()]
                end_event = event(key, ended)
                linked("ended_by", end_event, ended, passage)
                dated = False
                for date_record in ended.all("DATE"):
                    phrases = split_gedcom_period(date_record.value)
                    phrased = (
                        [(date_record.value, "point_in_time")]
                        if phrases is None
                        else [
                            (phrase, prop)
                            for phrase, prop in zip(
                                phrases,
                                ("start_time", "end_time"),
                                strict=True,
                            )
                            if phrase is not None
                        ]
                    )
                    for phrase, prop in phrased:
                        dated |= event_date(
                            end_event,
                            date_record,
                            phrase,
                            prop,
                            ended,
                            passage,
                        )
                if not dated:
                    outcome(f"union: undated {key}")
                for place in places(ended):
                    claim(
                        end_event,
                        "location",
                        place_item(place),
                        ended,
                        passage=passage,
                    )

    # individuals -> entities + birth/death claims
    for xref, record in individuals.items():
        names = names_of[xref]
        if not names:
            warnings.append(
                f"{xref}: no NAME gives a name; skipped entirely, with the "
                "family links that name it"
            )
            outcome("person without a name")
            continue
        # every date the record gives a birth or a death, as it gives them,
        # save a DEAT's that states no death
        summary_bits = []
        for tag, mark in (("BIRT", "b."), ("DEAT", "d.")):
            dates = [
                date.value
                for event in record.all(tag)
                if tag != "DEAT" or states_event(event)
                for date in event.all("DATE")
                if date.value
            ]
            if dates:
                summary_bits.append(
                    f"{mark} " + " or ".join(dict.fromkeys(dates))
                )
        entities.append(person_entity(xref, record, "; ".join(summary_bits)))
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
            if not states_event(death):
                continue
            gave = event_claims(xref, death, "date_of_death", "place_of_death")
            # cause of death stays verbatim prose (docs/MODELING.md), in
            # the export's language, like the excerpts
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

    # families -> parentage claims and unions, between people the batch
    # holds: a HUSB, WIFE or CHIL that names no one it holds gives none
    for fam in families.values():
        husband = fam.text("HUSB")
        wife = fam.text("WIFE")
        if husband and husband == wife:
            # one person as both father and mother is no reading the
            # record can mean, so neither is guessed, and no union is made
            outcome("FAM one person as HUSB and WIFE")
            continue
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
        # a family with one partner or none gives no union, and the census
        # counts its MARR, DIV and ANUL
        if coupled(fam):
            union_claims(fam)

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
    problems += [
        f"entity {entity.id!r}: {problem}"
        for entity in batch.entities
        for problem in structure_problems(entity)
    ]
    if problems:
        # deterministic code emitting invalid claims is a bug, not input noise
        raise ValueError(
            "gedcom adapter produced invalid claims:\n  "
            + "\n  ".join(problems)
        )
    return AdapterResult(
        batch=batch,
        warnings=warnings,
        unsupported_tags=unsupported,
        outcomes=outcomes,
    )
