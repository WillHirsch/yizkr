"""Static publish tier (frontend ladder rung 2): live SPARQL -> site.

`yizkr publish-site` renders the graph into a self-contained static
site: person pages showing EVERY attributed value side by side with
its source label (R6/ADR-0002 — never silently pick one), each death
event of theirs that states no date, as "died (date not stated)" with
its sources, and the artifacts that name the person; artifact pages
showing what the artifact asserts and what sources say about it, a
container's captions and the artifact's own statements about itself
(ADR-0039 §7); a timeline that shows beside each date the range its
source states, and at its foot the deaths no source dates; a photo
gallery (ADR-0008 derived media included), the disagreement report, and
a places index that becomes a map once places carry coordinates.

Design constraints:

* Generates FROM THE LIVE ENDPOINT (curator decision): the site
  includes possibly-living people, so it is for LAN/private serving
  only. An internet-facing build must come from the
  possibly-living-excluded non-privileged export instead (later rung). The
  private site still marks who that export would drop, via a badge
  computed at render time from the absence of death evidence — the same
  runtime determination the export uses (ADR-0020, `core/privacy.py`) —
  except a person with no type, whom the export leaves out even when
  released: such a person has no badge. A family, which the export
  always leaves out, is always badged.
* Self-contained output: no CDN scripts, no external fonts, no
  third-party tiles — nothing about the family leaves the LAN when a
  page is opened (privacy-first). Media are hot-linked from the wiki
  on the same host.
* Pure functions over query results: the SPARQL runner is injectable,
  so rendering is unit-testable offline.
* An instance that holds a development-stage scratch run says so on
  every page (ADR-0025 §1): when a run item states `unratified scratch
  run`, each page's header names the scratch runs the instance holds,
  read from the graph like everything else, and says their writes are
  unratified machine output that the next reset discards.
* Only supported dates are shown (ADR-0034): the build first asks the
  graph for any date before the common era or in a calendar other than
  the proleptic Gregorian, as a value, a qualifier or in a reference, and
  stops with UnsupportedDate naming each item and statement that holds
  one; ``format_time`` refuses a date before the common era too.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import shutil
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from yizkr import disagreement
from yizkr.core import sparql
from yizkr.core.config import Config
from yizkr.core.dates import check_time, unsupported_in_graph
from yizkr.core.privacy import (
    DEFAULT_CUTOFF_YEARS,
    SAME_AS,
    UNKNOWN,
    LifeEvidence,
    Participation,
    event_principal,
    family_type,
    is_possibly_living,
    people_selection,
    read_evidence,
)
from yizkr.core.wiki import WikiClient
from yizkr.disagreement import Disagreement, Side
from yizkr.model.schema import Acceptance, load_acceptance
from yizkr.queries.publish import load_templates, prefix_block, render

# -- SPARQL ----------------------------------------------------------------


def cell(binding: dict, name: str) -> str:
    return binding.get(name, {}).get("value", "")


def qid_of(binding: dict, name: str) -> str:
    return cell(binding, name).rsplit("/", 1)[-1]


# people roster with labels/descriptions/aliases and a RUNTIME possibly-living
# signal (ADR-0020). The people are the items the non-privileged export's check
# assesses: ``{people}`` is the privacy module's selection, every item with
# person evidence at any rank, so a person whose `human` type a curator
# deprecated or outranked, or who has no type but other evidence, is listed and
# badged too, and an event is not listed on the death it states. ?deceased is
# true when the graph holds any death-implying statement of the person's own
# (date/place/cause of death, place of burial),
# none of which needs a date. Death events, whose principal alone they mark
# deceased, and births, for the birth-recency cutoff, are read apart, with the
# reading the non-privileged export uses (core/privacy.py's read_evidence).
# ?family is true for
# a family (``{family_type}``, the privacy module's test), which never
# enters the non-privileged export, released or not. No stored "living"
# flag is read; the badge is derived, like the non-privileged export.
PEOPLE_QUERY = """\
SELECT ?person ?personLabel ?personDescription ?deceased ?family
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
{people}
  OPTIONAL {{ ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  BIND({family_type} AS ?family)
  BIND((
        EXISTS {{ ?person wdt:{date_of_death} [] }}
     || EXISTS {{ ?person wdt:{place_of_death} [] }}
     || EXISTS {{ ?person wdt:{cause_of_death} [] }}
     || EXISTS {{ ?person wdt:{burial_place} [] }}
       ) AS ?deceased)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?person ?personLabel ?personDescription ?deceased ?family
ORDER BY ?personLabel
"""

# each qualifier of the statement ?st as one cell, for GROUP_CONCAT with
# separator "|": its property's id, its property's English label, its value
# and, for a time, its precision, separated by spaces. The label and the
# value are percent-encoded (ENCODE_FOR_URI), so no cell holds a space or a
# "|" of its own, whatever a label or a string value says; read_qualifiers
# reads them back. An item's value is its English label (its full IRI when
# it has none), a time's its
# xsd:dateTime with its precision beside it (formatted by format_time, so
# shown at the SOURCE's precision, never padded or narrowed), and any other
# value its text.
QUALIFIER_CELL = """\
    ?st ?pq ?qualifierValue .
    ?qualifierProp wikibase:qualifier ?pq ;
                   rdfs:label ?qualifierLabel .
    FILTER(LANG(?qualifierLabel) = "en")
    OPTIONAL {{
      ?qualifierValue rdfs:label ?qualifierValueLabel .
      FILTER(LANG(?qualifierValueLabel) = "en")
    }}
    OPTIONAL {{
      ?qualifierProp wikibase:qualifierValue ?pqv .
      ?st ?pqv ?qualifierNode .
      ?qualifierNode wikibase:timeValue ?qualifierValue ;
                     wikibase:timePrecision ?qualifierPrecision .
    }}
    BIND(
      CONCAT(STRAFTER(STR(?qualifierProp), "/entity/"), " ",
             ENCODE_FOR_URI(STR(?qualifierLabel)), " ",
             ENCODE_FOR_URI(COALESCE(STR(?qualifierValueLabel),
                                     STR(?qualifierValue))), " ",
             COALESCE(STR(?qualifierPrecision), ""))
      AS ?qualifierCell)
"""

# every statement about every person the roster lists (``{people}``, as
# above): value, time precision, sources, qualifiers (QUALIFIER_CELL) —
# the raw material of the plural-by-default person page
STATEMENTS_QUERY = (
    """\
SELECT ?person ?prop ?propLabel ?st ?value ?valueLabel ?precision
       (GROUP_CONCAT(DISTINCT ?sourceLabel; separator="|") AS ?sources)
       (GROUP_CONCAT(DISTINCT ?qualifierCell; separator="|")
        AS ?qualifierCells)
WHERE {{
{people}
  ?person ?p ?st .
  ?prop wikibase:claim ?p ;
        wikibase:statementProperty ?ps ;
        rdfs:label ?propLabel .
  FILTER(LANG(?propLabel) = "en")
  ?st ?ps ?value .
  OPTIONAL {{
    ?value rdfs:label ?valueLabelRaw . FILTER(LANG(?valueLabelRaw) = "en")
  }}
  BIND(?valueLabelRaw AS ?valueLabel)
  OPTIONAL {{
    ?prop wikibase:statementValue ?psv .
    ?st ?psv ?valueNode .
    ?valueNode wikibase:timePrecision ?precision .
  }}
  OPTIONAL {{
    ?st prov:wasDerivedFrom ?reference .
    ?reference pr:{stated_in} ?source .
    ?source rdfs:label ?sourceLabelRaw . FILTER(LANG(?sourceLabelRaw) = "en")
    BIND(CONCAT(STRAFTER(STR(?source), "/entity/"), "~", ?sourceLabelRaw)
         AS ?sourceLabel)
  }}
  OPTIONAL {{
"""
    + QUALIFIER_CELL
    + """\
  }}
}}
GROUP BY ?person ?prop ?propLabel ?st ?value ?valueLabel ?precision
ORDER BY ?person ?propLabel
"""
)

ARTIFACTS_QUERY = """\
SELECT ?artifact ?artifactLabel ?artifactDescription ?kindLabel ?file
WHERE {{
  ?artifact wdt:{instance_of} ?kind .
  ?kind wdt:{subclass_of}* wd:{artifact} .
  OPTIONAL {{ ?artifact wdt:{media_file} ?file . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
ORDER BY ?artifactLabel
"""

# place containment edges — the places page's tree (ADR-0009: a
# visible hierarchy is a self-auditing one)
PLACE_TREE_QUERY = """\
SELECT ?child ?childLabel ?parent
WHERE {{
  ?child wdt:{instance_of} wd:{place} .
  ?child rdfs:label ?childLabel . FILTER(LANG(?childLabel) = "en")
  OPTIONAL {{ ?child (wdt:{part_of}|wdt:{location}) ?parent .
              ?parent wdt:{instance_of} wd:{place} . }}
}}
ORDER BY ?childLabel
"""

# what each artifact asserts, and where in it — the artifact page's evidence
# table. A row for each statement and reference block that cites the
# artifact: the statement, so two same-value statements that differ by a
# qualifier stay two (ADR-0002), and the block's locators and excerpt, each
# a column of its own, so a statement that cites one artifact in two blocks
# (two pages, say) gives rows that say where. A block with two values of
# one locator gives a row for each. A statement citing a fragment of an
# artifact is listed under its whole, as the disagreement report lists it,
# with the fragment as ?source and its label. Each statement's qualifiers
# come as QUALIFIER_CELL's cells, gathered by statement in a subquery.
ASSERTIONS_QUERY = (
    """\
SELECT ?artifact ?source ?fragmentLabel ?st ?reference ?subject ?subjectLabel
       ?propLabel ?value ?valueLabel ?precision
       ?page ?printedPage ?region ?excerpt ?qualifierCells
WHERE {{
  ?st prov:wasDerivedFrom ?reference .
  ?reference pr:{stated_in} ?source .
  ?subject ?p ?st .
  ?prop wikibase:claim ?p ;
        wikibase:statementProperty ?ps ;
        rdfs:label ?propLabel .
  FILTER(LANG(?propLabel) = "en")
  ?st ?ps ?value .
  OPTIONAL {{
    ?source wdt:{instance_of} wd:{artifact_fragment} ;
            wdt:{part_of} ?whole .
    OPTIONAL {{
      ?source rdfs:label ?fragmentLabel .
      FILTER(LANG(?fragmentLabel) = "en")
    }}
  }}
  BIND(COALESCE(?whole, ?source) AS ?artifact)
  OPTIONAL {{
    ?subject rdfs:label ?subjectLabel .
    FILTER(LANG(?subjectLabel) = "en")
  }}
  OPTIONAL {{
    ?value rdfs:label ?valueLabel .
    FILTER(LANG(?valueLabel) = "en")
  }}
  OPTIONAL {{
    ?prop wikibase:statementValue ?psv .
    ?st ?psv ?valueNode .
    ?valueNode wikibase:timePrecision ?precision .
  }}
  OPTIONAL {{ ?reference pr:{page_number} ?page . }}
  OPTIONAL {{ ?reference pr:{printed_page_number} ?printedPage . }}
  OPTIONAL {{ ?reference pr:{relative_position_within_image} ?region . }}
  OPTIONAL {{ ?reference pr:{transcription_excerpt} ?excerpt . }}
  OPTIONAL {{
    SELECT ?st (GROUP_CONCAT(DISTINCT ?qualifierCell; separator="|")
                AS ?qualifierCells)
    WHERE {{
      ?st prov:wasDerivedFrom/pr:{stated_in} [] .
"""
    + QUALIFIER_CELL
    + """\
    }}
    GROUP BY ?st
  }}
}}
ORDER BY ?artifact ?subjectLabel ?propLabel ?st ?page ?printedPage ?region
         ?excerpt
"""
)

# what sources say about each artifact (ADR-0039 §7): every statement whose
# subject is the artifact on a property the manifest marks
# `placed_subject` (a container's `caption`, stated in the container with
# its page) or `document_subject` (what the artifact states of itself:
# `author`, `addressee`, `inception` and the rest), ``{source_properties}``.
# A row for each statement and reference block that cites a source, and
# one row for a statement that cites none: the statement keeps two
# same-value statements apart (ADR-0002), and the block names its source
# and the page and printed page it cites. A time
# value carries its stated precision, and each statement's qualifiers (its
# sourcing circumstances, circa among them) come as QUALIFIER_CELL's cells.
# The person page reads the same rows by their value: the document-subject
# statements that name a person.
SOURCES_SAY_QUERY = (
    """\
SELECT ?artifact ?artifactLabel ?prop ?propLabel ?st ?value ?valueLabel
       ?precision ?reference ?source ?sourceLabel ?page ?printedNumber
       ?qualifierCells
WHERE {{
  VALUES ?prop {{ {source_properties} }}
  ?prop wikibase:claim ?p ;
        wikibase:statementProperty ?ps ;
        rdfs:label ?propLabel .
  FILTER(LANG(?propLabel) = "en")
  ?artifact ?p ?st .
  ?st ?ps ?value .
  OPTIONAL {{
    ?artifact rdfs:label ?artifactLabel .
    FILTER(LANG(?artifactLabel) = "en")
  }}
  OPTIONAL {{
    ?value rdfs:label ?valueLabel .
    FILTER(LANG(?valueLabel) = "en")
  }}
  OPTIONAL {{
    ?prop wikibase:statementValue ?psv .
    ?st ?psv ?valueNode .
    ?valueNode wikibase:timePrecision ?precision .
  }}
  OPTIONAL {{
    ?st prov:wasDerivedFrom ?reference .
    ?reference pr:{stated_in} ?source .
    OPTIONAL {{
      ?source rdfs:label ?sourceLabel .
      FILTER(LANG(?sourceLabel) = "en")
    }}
    OPTIONAL {{ ?reference pr:{page_number} ?page . }}
    OPTIONAL {{ ?reference pr:{printed_page_number} ?printedNumber . }}
  }}
  OPTIONAL {{
    SELECT ?st (GROUP_CONCAT(DISTINCT ?qualifierCell; separator="|")
                AS ?qualifierCells)
    WHERE {{
      VALUES ?prop {{ {source_properties} }}
      ?prop wikibase:claim ?p .
      [] ?p ?st .
"""
    + QUALIFIER_CELL
    + """\
    }}
    GROUP BY ?st
  }}
}}
ORDER BY ?artifact ?propLabel ?st ?reference ?page ?printedNumber
"""
)

# how a person page names a document-subject statement whose value is the
# person, by the statement's property: "shown in" the portrait; a property
# not named here is shown by its label
NAMED_AS = {
    "author": "author of",
    "addressee": "addressee of",
    "shows": "shown in",
    "main_subject": "the subject of",
    "issued_by": "issuer of",
    "location_of_creation": "place of creation of",
}

# every death event that states no date, for the person page and the
# timeline of the one who died: an item with a `death` type at any rank but
# deprecated (through redirects) and no `point in time`, `start time` or
# `end time` with a time value at any rank but deprecated. A row for each
# role each of its participant statements states (one row with no role when
# it states none), read through the statement node at every rank, as the
# privacy module's participants query reads them, so that ``event_principal``
# can say whose death it is; and beside it each source the participant
# statement and the type statement are stated in, the label optional, so a
# source with no English label is still shown, by its id.
UNDATED_DEATHS_QUERY = """\
SELECT ?event ?part ?partRank ?value ?role ?principal
       ?source ?sourceLabel ?kindSource ?kindSourceLabel
WHERE {{
  ?event p:{instance_of} ?typing .
  ?typing ps:{instance_of}/{same_as}* wd:{death} ;
          wikibase:rank ?typingRank .
  FILTER(?typingRank != wikibase:DeprecatedRank)
  FILTER NOT EXISTS {{
    VALUES ?dated {{ wd:{point_in_time} wd:{start_time} wd:{end_time} }}
    ?dated wikibase:claim ?datedClaim ;
           wikibase:statementValue ?datedValue .
    ?event ?datedClaim ?dating .
    ?dating ?datedValue ?datedNode ;
            wikibase:rank ?datingRank .
    ?datedNode wikibase:timeValue [] .
    FILTER(?datingRank != wikibase:DeprecatedRank)
  }}
  ?event p:{participant} ?part .
  ?part wikibase:rank ?partRank .
  OPTIONAL {{ ?part ps:{participant} ?value . }}
  OPTIONAL {{
    ?part pq:{object_has_role} ?role .
    BIND(EXISTS {{ ?role {same_as}* wd:{principal} . }} AS ?principal)
  }}
  OPTIONAL {{
    ?part prov:wasDerivedFrom/pr:{stated_in} ?source .
    OPTIONAL {{
      ?source rdfs:label ?sourceLabel .
      FILTER(LANG(?sourceLabel) = "en")
    }}
  }}
  OPTIONAL {{
    ?typing prov:wasDerivedFrom/pr:{stated_in} ?kindSource .
    OPTIONAL {{
      ?kindSource rdfs:label ?kindSourceLabel .
      FILTER(LANG(?kindSourceLabel) = "en")
    }}
  }}
}}
"""

# how the person page and the timeline show a death its source gives no
# date for
UNDATED_DEATH = "died (date not stated)"

# the scratch runs the instance holds, by their run items' marks
SCRATCH_RUNS_QUERY = """\
SELECT DISTINCT ?run WHERE {{ ?item wdt:{scratch_run} ?run . }}
"""

# the newest revision of an entity the query service has taken in
NEWEST_REVISION_QUERY = """\
PREFIX schema: <http://schema.org/>
SELECT (MAX(?revision) AS ?version)
WHERE { ?entity schema:version ?revision . }
"""

CSS = """\
:root { --ink: #222; --muted: #667; --accent: #7a4a20; --line: #ddd;
        --bg: #faf8f4; }
* { box-sizing: border-box; }
body { font: 16px/1.55 Georgia, 'Times New Roman', serif; color: var(--ink);
       background: var(--bg); margin: 0 auto; max-width: 60rem;
       padding: 1rem 1.2rem 4rem; }
a { color: var(--accent); }
nav { font-family: system-ui, sans-serif; font-size: .85rem;
      border-bottom: 1px solid var(--line);
      padding-bottom: .6rem; margin-bottom: 1.2rem; }
nav a { margin-right: 1rem; text-decoration: none; }
h1 { font-weight: normal; margin: .2rem 0 .1rem; }
.sub { color: var(--muted); margin: 0 0 1rem; }
.badge { font-family: system-ui, sans-serif; font-size: .7rem;
         background: #a33; color: #fff;
         border-radius: .6rem; padding: .1rem .55rem; vertical-align: middle; }
dl.statements dt { font-family: system-ui, sans-serif; font-size: .8rem;
                   text-transform: uppercase;
                   letter-spacing: .05em; color: var(--muted);
                   margin-top: 1rem; }
dl.statements dd { margin: .25rem 0 .25rem 1rem; }
.source { color: var(--muted); font-size: .85em; }
.source::before { content: " — per "; }
.qualifiers { color: var(--muted); font-size: .85em; font-style: italic; }
ul.roster { columns: 2; list-style: none; padding: 0; }
ul.roster li { margin: .15rem 0; break-inside: avoid; }
.gallery { display: grid;
           grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
           gap: 1rem; }
.gallery figure { margin: 0; }
.gallery img { width: 100%; height: 200px; object-fit: contain;
               background: #fff;
               border: 1px solid var(--line); }
.gallery figcaption { font-size: .8rem; color: var(--muted); }
.timeline .year { font-family: system-ui, sans-serif; font-weight: 600;
                  margin-top: 1.2rem;
                  border-bottom: 1px solid var(--line); }
.timeline .entry { margin: .3rem 0 .3rem 1rem; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { text-align: left; padding: .35rem .6rem;
         border-bottom: 1px solid var(--line);
         vertical-align: top; }
.scratch { font-family: system-ui, sans-serif; font-size: .85rem;
           background: #fff3cd; border: 1px solid #c9a227;
           padding: .5rem .8rem; margin: 0 0 1.2rem; }
footer { margin-top: 3rem; color: var(--muted); font-size: .8rem;
         border-top: 1px solid var(--line); padding-top: .6rem; }
"""

NAV = (
    '<nav><a href="{root}index.html">People</a>'
    '<a href="{root}timeline.html">Timeline</a>'
    '<a href="{root}gallery.html">Gallery</a>'
    '<a href="{root}places.html">Places</a>'
    '<a href="{root}inconsistencies.html">Inconsistencies</a></nav>'
)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def scratch_notice(runs: list[str]) -> str:
    """The header that says the instance holds a development-stage scratch
    run, naming each one whose run items it holds; "" for none."""
    if not runs:
        return ""
    named = ", ".join(f"<code>{esc(run)}</code>" for run in runs)
    return (
        '<p class="scratch">This instance holds a development-stage '
        f"scratch run ({'runs' if len(runs) > 1 else 'run'} {named}): "
        "unratified machine output that no one has reviewed or confirmed. "
        "It is never kept, published or exported, and the next reset "
        "discards it.</p>"
    )


def page(title: str, body: str, root: str = "", notice: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<link rel="stylesheet" href="{root}style.css"></head><body>'
        + NAV.format(root=root)
        + notice
        + body
        + "<footer>Generated by <code>yizkr publish-site</code> from the "
        "live graph — includes possibly-living people; for private "
        "use. Every value is shown with the source that asserts it; "
        "disagreement is a healthy state, not an error."
        "</footer></body></html>"
    )


def ordinal(number: int) -> str:
    """`number` as an English ordinal: 1st, 2nd, 3rd, 4th, 11th, 12th,
    13th, 21st, 101st, 111th."""
    if number % 100 in (11, 12, 13):
        return f"{number}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def format_time(
    value: str, precision: str, where: str = "a date the site shows"
) -> str:
    """Wikibase dateTime + precision -> what the source actually said.
    Centuries and millennia are the ones Wikibase counts for the stored
    year, from year 1: at century precision, 1801 to 1900 are each the
    19th century, and at millennium precision 1001 to 2000 are each the
    2nd millennium. A precision coarser than a millennium (Wikibase's 0
    to 5, ten thousand years up to a billion) names no span the site has
    a word for, so it shows the stored year with the span its precision
    allows ("1900, to within 10,000 years"), never a narrower date.
    A date before the common era raises UnsupportedDate naming `where`
    (ADR-0034): it is never shown. This is a backstop: the build has
    already refused a graph holding one, naming its item and statement."""
    check_time(value, None, where)
    date = value[1:] if value.startswith("+") else value
    date = date.split("T")[0]
    year, _, rest = date.partition("-")
    month = rest.split("-")[0] if rest else ""
    p = int(precision) if precision else 11
    if p <= 5:
        return f"{int(year)}, to within {10 ** (9 - p):,} years"
    if p == 6:
        return f"{ordinal((int(year) + 999) // 1000)} millennium"
    if p == 7:
        return f"{ordinal((int(year) + 99) // 100)} century"
    if p == 8:
        return f"{int(year) // 10 * 10}s"
    if p == 9:
        return f"{int(year)}"
    if p == 10:
        return f"{int(year)}-{month}"
    return date


def range_text(
    bounds: Iterable[tuple[str, str, str]],
    where: str = "a date the site shows",
) -> str:
    """The range a source states for a date, as text to show beside it.
    Each bound is (the label of the qualifier that states it, its
    xsd:dateTime, its precision) and shows at its own precision, in the
    `label: value` form the person page uses for qualifiers: "earliest
    date: 1938; latest date: 1939". The caller passes only the bounds the
    source gives, so a range open at one end names the one bound it has
    ("earliest date: 1945"), never a closed range or a bare date."""
    return "; ".join(
        f"{label}: {format_time(value, precision, where)}"
        for label, value, precision in bounds
    )


# the order a statement's dates read in, by manifest key: a period's start
# with its bounds, then its end with its bounds (docs/MODELING.md,
# "Marriages, residences and other periods"); a value's earliest date
# before its latest. Every other qualifier follows, by label and value.
DATE_QUALIFIERS = (
    "point_in_time",
    "start_time",
    "earliest_date",
    "latest_start_date",
    "end_time",
    "earliest_end_date",
    "latest_date",
)


def natural_order(text: str) -> tuple[list[str | int], str]:
    """A sort key that reads each run of digits in `text` as its number,
    so page 2 sorts before page 10, and then `text` itself, so texts whose
    numbers are equal ("02" and "2") still keep one order."""
    runs = re.split(r"(\d+)", text)
    return [int(run) if n % 2 else run for n, run in enumerate(runs)], text


def visible(markup: str) -> str:
    """The text a reader sees of `markup`: its tags left out and its
    character references read."""
    return html.unescape(re.sub(r"<[^>]*>", "", markup))


def reading_order(entries: Iterable[tuple[str, str]]) -> list[str]:
    """Entries of (what an entry says, where it is from), each as HTML,
    joined and sorted by the text a reader sees: what it says, then where
    it is from, each in ``natural_order``, so a person sorts by their name
    and not by the link to their page, and July 1939 before 28 July 1939.
    The order lets no value stand first for any reason but its text."""
    return [
        said + source
        for said, source in sorted(
            entries,
            key=lambda entry: (
                natural_order(visible(entry[0])),
                natural_order(visible(entry[1])),
                entry,
            ),
        )
    ]


def read_qualifiers(encoded: str) -> list[tuple[str, str, str, str]]:
    """The qualifier cells of QUALIFIER_CELL, "|"-separated as
    GROUP_CONCAT gives them, as (property id, label, value, precision),
    the precision "" for a value that is not a time."""
    qualifiers = []
    for each in encoded.split("|"):
        if not each:
            continue
        pid, label, value, precision = each.split(" ")
        qualifiers.append(
            (
                pid,
                urllib.parse.unquote(label),
                urllib.parse.unquote(value),
                precision,
            )
        )
    return qualifiers


def qualifier_span(text: str) -> str:
    """`text` beside a value, escaped, in the muted style of the person
    page's qualifiers; nothing when there is no text."""
    if not text:
        return ""
    return f' <span class="qualifiers">({esc(text)})</span>'


def participation(row: dict, roles: set[tuple[str, bool]]) -> Participation:
    """A participant statement of UNDATED_DEATHS_QUERY's rows, as the
    privacy module reads one: its value's QID (``UNKNOWN`` for an unknown
    value or one that is no item, "" for none), its rank by its local
    name, and `roles`, each role it states and whether it is
    `principal`."""
    value = cell(row, "value")
    item = re.search(r"/entity/(Q[1-9][0-9]*)$", value)
    return Participation(
        value=item.group(1) if item else UNKNOWN if value else "",
        rank=cell(row, "partRank").rsplit("#", 1)[-1],
        roles=frozenset(roles),
    )


@dataclass(frozen=True)
class Citation:
    """One reference block of a statement SOURCES_SAY_QUERY reads: the
    source it is stated in, by QID and English label, and the pages and
    printed pages it cites."""

    source: str
    label: str
    pages: tuple[str, ...] = ()
    printed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Said:
    """A statement a source makes about an artifact on a placed-subject
    or document-subject property (SOURCES_SAY_QUERY): the artifact, the
    property by its manifest key and label, the statement's first row
    (its value, precision and qualifier cells) and each citation."""

    artifact: str
    artifact_label: str
    key: str
    property_label: str
    row: dict
    citations: tuple[Citation, ...]

    @property
    def value_qid(self) -> str:
        """The value's QID when it is an item, else ""."""
        value = self.row.get("value", {})
        raw = value.get("value", "")
        if value.get("type") == "uri" and "/entity/Q" in raw:
            return raw.rsplit("/", 1)[-1]
        return ""


class SiteBuilder:
    def __init__(
        self, config: Config, out_dir: Path, run=None, vocabulary=None
    ):
        self.config = config
        self.out = out_dir
        self.run = run or sparql.runner(config)
        if vocabulary is None:
            properties, items = WikiClient(config).vocabulary()
        else:
            properties, items = vocabulary
        self.properties = properties
        self.items = items
        self.templates = load_templates()
        self._cutoff_year = (
            datetime.datetime.now(datetime.UTC).year - DEFAULT_CUTOFF_YEARS
        )
        # the roster's rows by QID, so a page can link a value to the
        # person's page and badge it as the person's own page does
        self.people: dict[str, dict] = {}
        # the artifacts that have a page of their own
        self.artifact_qids: set[str] = set()

    # -- helpers ------------------------------------------------------------

    @cached_property
    def _evidence(self) -> LifeEvidence:
        """The births by person and the principals of death events, read
        with the queries and the reading the non-privileged export uses."""
        return read_evidence(
            self.run,
            prefix_block(self.config.concept_uri),
            self.properties,
            self.items,
        )

    def _possibly_living(self, person: dict) -> bool:
        """Runtime privacy determination (ADR-0020): possibly living
        unless the graph holds death evidence, or every birth it records
        lies beyond the cutoff. The same call the non-privileged export makes —
        here it only decides a display badge on the (private) site. A
        family is badged whatever its evidence, as the export leaves every
        family out: any of its people may be living."""
        if cell(person, "family") == "true":
            return True
        qid = qid_of(person, "person")
        births = self._evidence.births.get(qid, [])
        has_death = (
            cell(person, "deceased") == "true" or qid in self._evidence.died
        )
        return is_possibly_living(births, has_death, self._cutoff_year)[0]

    def _instance_query(self, template: str) -> str:
        """`template` with this instance's ids, its ``{people}`` and
        ``{family_type}`` (where it has them) the privacy module's
        selection of people and its test for a family, and its
        ``{same_as}`` the link a redirect is read through."""
        vocabulary = {**self.items, **self.properties}
        return prefix_block(self.config.concept_uri) + template.format(
            people=people_selection(self.properties, self.items),
            family_type=family_type(self.properties, self.items),
            same_as=SAME_AS,
            source_properties=" ".join(
                f"wd:{self.properties[key]}"
                for key in sorted(self._placed | self._document)
            ),
            **vocabulary,
        )

    @cached_property
    def _acceptance(self) -> Acceptance:
        return load_acceptance()

    @cached_property
    def _placed(self) -> frozenset[str]:
        """The manifest's `placed_subject` properties the instance has: a
        container's words about an artifact it places (`caption`)."""
        return self._acceptance.placed_subjects & self.properties.keys()

    @cached_property
    def _document(self) -> frozenset[str]:
        """The manifest's `document_subject` properties the instance has:
        what a source states of itself."""
        return self._acceptance.document_subjects & self.properties.keys()

    @cached_property
    def _keys_by_id(self) -> dict[str, str]:
        """Each manifest property's key, by its id on the instance."""
        return {pid: key for key, pid in self.properties.items()}

    def _library_query(self, name: str) -> str:
        return prefix_block(self.config.concept_uri) + render(
            self.templates[name], self.properties, self.items
        )

    @cached_property
    def scratch_runs(self) -> list[str]:
        """The scratch runs whose run items the instance holds, read once
        for the build (query_scratch_runs)."""
        return self.query_scratch_runs()

    def query_scratch_runs(self) -> list[str]:
        """The scratch runs whose run items the query service shows now
        (SCRATCH_RUNS_QUERY), none where the instance's vocabulary has no
        `unratified scratch run`."""
        if "scratch_run" not in self.properties:
            return []
        rows = self.run(self._instance_query(SCRATCH_RUNS_QUERY))
        return sorted({cell(row, "run") for row in rows} - {""})

    def newest_revision(self) -> int:
        """The newest revision of an entity the query service holds, by
        its `schema:version` (NEWEST_REVISION_QUERY); 0 for none."""
        rows = self.run(NEWEST_REVISION_QUERY)
        value = cell(rows[0], "version") if rows else ""
        return int(value) if value.isdigit() else 0

    def _page(self, title: str, body: str, root: str = "") -> str:
        return page(title, body, root, scratch_notice(self.scratch_runs))

    def _write(self, relative: str, text: str) -> None:
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    @cached_property
    def _property_labels_by_id(self) -> dict[str, str]:
        """The label of each manifest property, by its id on the instance."""
        labels = disagreement.property_labels()
        return {pid: labels[key] for key, pid in self.properties.items()}

    def timeline_range(self, row: dict) -> str:
        """The range a person-timeline row's source states for its date, as
        ``range_text``: each bound the query returns, named by the
        qualifier the query read it from (?low for ?earliest, ?high for
        ?latest). So a start's upper bound reads as its latest start date
        and an end's lower bound as its earliest end date (MODELING.md,
        "Marriages, residences and other periods"). A bound the row gives
        without a qualifier the site can name stops the build: shown
        unnamed, it could not say which end it bounds."""
        bounds = []
        for qualifier, bound in (("low", "earliest"), ("high", "latest")):
            if not cell(row, bound):
                continue
            pid = qid_of(row, qualifier)
            label = self._property_labels_by_id.get(pid)
            if label is None:
                raise RuntimeError(
                    f"the person-timeline query gave a row's ?{bound} "
                    f"without the qualifier that states it (?{qualifier} "
                    f"is {repr(pid) if pid else 'missing'}); it must "
                    "project ?low and ?high"
                )
            bounds.append(
                (label, cell(row, bound), cell(row, f"{bound}Precision"))
            )
        return range_text(bounds, f"{qid_of(row, 'person')} timeline date")

    @cached_property
    def _circumstances_label(self) -> str:
        """The manifest's label for the sourcing circumstances qualifier,
        as the person page names it."""
        return disagreement.property_labels()["sourcing_circumstances"]

    def timeline_qualifiers(self, row: dict) -> str:
        """What a person-timeline row shows beside its date: the range its
        source states (``timeline_range``), then each sourcing
        circumstance of its statement in the person page's `label: value`
        form ("sourcing circumstances: circa"). The query folds the
        circumstances into ?circumstances, each label URI-encoded and
        "|"-separated; they are sorted, since GROUP_CONCAT gives them in
        no set order."""
        name = self._circumstances_label
        circumstances = sorted(
            urllib.parse.unquote(each)
            for each in cell(row, "circumstances").split("|")
            if each
        )
        shown = [self.timeline_range(row)]
        shown += [f"{name}: {each}" for each in circumstances]
        return "; ".join(text for text in shown if text)

    def qualifier_text(self, encoded: str, where: str) -> str:
        """A statement's qualifier cells (QUALIFIER_CELL) as text to show
        beside its value, each in the `label: value` form, a time at its
        own precision (``format_time``, which refuses a date before the
        common era, naming `where`): "start time: 1938; latest date: 1952".
        GROUP_CONCAT gives the cells in no set order, so they are sorted:
        the dates in DATE_QUALIFIERS order, then the rest by label and
        value."""
        rank = {
            self.properties[key]: n
            for n, key in enumerate(DATE_QUALIFIERS)
            if key in self.properties
        }
        qualifiers = sorted(
            read_qualifiers(encoded),
            key=lambda q: (rank.get(q[0], len(rank)), q[1], q[2], q[3]),
        )
        return "; ".join(
            f"{label}: "
            + (format_time(value, precision, where) if precision else value)
            for _, label, value, precision in qualifiers
        )

    # -- sections -----------------------------------------------------------

    def build(self) -> dict:
        """Write the site. A graph holding a date before the common era or
        in a calendar other than the proleptic Gregorian, anywhere, raises
        UnsupportedDate naming its items and statements, before anything
        is written (ADR-0034)."""
        unsupported_in_graph(self.run, prefix_block(self.config.concept_uri))
        self.out.mkdir(parents=True, exist_ok=True)
        self._write("style.css", CSS)
        people = self.run(self._instance_query(PEOPLE_QUERY))
        statements = self.run(self._instance_query(STATEMENTS_QUERY))
        timeline = self.run(self._library_query("person-timeline"))
        gallery = self.run(self._library_query("photo-gallery"))
        inconsistencies = disagreement.disagreements(
            self.run, self.properties, self.items, self.config.concept_uri
        )
        places = self.run(self._library_query("places-map"))
        place_tree = self.run(self._instance_query(PLACE_TREE_QUERY))
        artifacts = self.run(self._instance_query(ARTIFACTS_QUERY))
        assertions = self.run(self._instance_query(ASSERTIONS_QUERY))
        sources_say = self.sources_say(
            self.run(self._instance_query(SOURCES_SAY_QUERY))
        )
        undated = self.undated_deaths(
            self.run(self._instance_query(UNDATED_DEATHS_QUERY))
        )
        self.people = {qid_of(row, "person"): row for row in people}
        self.artifact_qids = {qid_of(row, "artifact") for row in artifacts}

        by_person: dict[str, list[dict]] = defaultdict(list)
        for row in statements:
            by_person[qid_of(row, "person")].append(row)
        by_artifact: dict[str, list[dict]] = defaultdict(list)
        for row in assertions:
            by_artifact[qid_of(row, "artifact")].append(row)
        said_of: dict[str, list[Said]] = defaultdict(list)
        naming: dict[str, list[Said]] = defaultdict(list)
        for said in sources_say:
            said_of[said.artifact].append(said)
            if said.key in self._document and said.value_qid in self.people:
                naming[said.value_qid].append(said)

        # a death is shown only on the page of someone the roster lists
        undated = {
            qid: deaths
            for qid, deaths in undated.items()
            if qid in self.people
        }

        for row in people:
            qid = qid_of(row, "person")
            self._write(
                f"people/{qid}.html",
                self.person_page(
                    row,
                    by_person.get(qid, []),
                    naming.get(qid, []),
                    undated.get(qid, []),
                ),
            )
        for row in artifacts:
            qid = qid_of(row, "artifact")
            self._write(
                f"artifacts/{qid}.html",
                self.artifact_page(
                    row, by_artifact.get(qid, []), said_of.get(qid, [])
                ),
            )
        self._write("index.html", self.index_page(people, artifacts))
        self._write("timeline.html", self.timeline_page(timeline, undated))
        self._write("timeline.json", self.timeline_feed(timeline))
        self._write("gallery.html", self.gallery_page(gallery))
        self._write("places.html", self.places_page(places, place_tree))
        self._write(
            "inconsistencies.html", self.inconsistencies_page(inconsistencies)
        )
        return {
            "out": str(self.out),
            "people": len(people),
            "statements": len(statements),
            "artifacts": len(artifacts),
            "sources_say": len(sources_say),
            "undated_deaths": sum(len(each) for each in undated.values()),
            "timeline_rows": len(timeline),
            "gallery_items": len(gallery),
            "inconsistencies": len(inconsistencies),
            # the disagreements by property (its manifest key)
            "inconsistencies_by_property": disagreement.counts_by_property(
                inconsistencies
            ),
            "scratch_runs": self.scratch_runs,
        }

    def source_links(self, encoded: str, root: str = "../") -> str:
        """'Q23~label|Q29~label' -> artifact-page links."""
        pairs = (
            pair.partition("~")[::2] for pair in encoded.split("|") if pair
        )
        return self.artifact_links(pairs, root)

    def artifact_links(
        self, sources: Iterable[tuple[str, str]], root: str = "../"
    ) -> str:
        """(QID, label) pairs -> artifact-page links, '; '-separated."""
        links = []
        for qid, label in sources:
            if qid.startswith("Q") and label:
                links.append(
                    f'<a href="{root}artifacts/{esc(qid)}.html">'
                    f"{esc(label)}</a>"
                )
            else:
                links.append(esc(label or qid))
        return "; ".join(links)

    def sources_say(self, rows: list[dict]) -> list[Said]:
        """SOURCES_SAY_QUERY's rows as statements, in the order the query
        gives them: each statement's first row and each reference block
        that states it in a source, with the pages and printed pages the
        block cites. Rows are a statement's blocks, and a block's rows its
        locators' values, so the statements are counted here, never the
        rows. A statement of a property the manifest does not know is
        left out."""
        first: dict[str, dict] = {}
        blocks: dict[str, dict[str, dict]] = {}
        for row in rows:
            st = cell(row, "st")
            first.setdefault(st, row)
            by_block = blocks.setdefault(st, {})
            reference = cell(row, "reference")
            if not reference:
                continue
            block = by_block.setdefault(
                reference,
                {
                    "source": qid_of(row, "source"),
                    "label": cell(row, "sourceLabel"),
                    "pages": [],
                    "printed": [],
                },
            )
            for name, held in (
                ("page", "pages"),
                ("printedNumber", "printed"),
            ):
                text = cell(row, name)
                if text and text not in block[held]:
                    block[held].append(text)
        said = []
        for st, row in first.items():
            key = self._keys_by_id.get(qid_of(row, "prop"))
            if key is None:
                continue
            said.append(
                Said(
                    artifact=qid_of(row, "artifact"),
                    artifact_label=cell(row, "artifactLabel"),
                    key=key,
                    property_label=cell(row, "propLabel"),
                    row=row,
                    citations=tuple(
                        Citation(
                            block["source"],
                            block["label"],
                            tuple(sorted(block["pages"], key=natural_order)),
                            tuple(sorted(block["printed"], key=natural_order)),
                        )
                        for block in blocks[st].values()
                    ),
                )
            )
        return said

    def undated_deaths(
        self, rows: list[dict]
    ) -> dict[str, list[tuple[tuple[str, str], ...]]]:
        """UNDATED_DEATHS_QUERY's rows as the deaths each person is shown
        to have died, by their QID: for each death event that states no
        date, its sources as (QID, label) pairs, under its principal alone
        (``event_principal``, ADR-0035 §3.2), the one its participant
        statements name in the role `principal`, read with every
        deprecated participant statement set aside; an informant or a
        witness who takes part is never shown to have died, and an event
        that is nobody's own is shown on no one's page. The sources are
        those its type statements and its principal's own participant
        statements are stated in, in the order of their QIDs; the events
        come in the order of theirs."""
        events: dict[str, dict] = {}
        for row in rows:
            event = events.setdefault(
                qid_of(row, "event"), {"parts": {}, "kind": {}}
            )
            part = event["parts"].setdefault(
                cell(row, "part"), {"row": row, "roles": set(), "sources": {}}
            )
            if "role" in row:
                part["roles"].add(
                    (cell(row, "role"), cell(row, "principal") == "true")
                )
            for name, held in (
                ("source", part["sources"]),
                ("kindSource", event["kind"]),
            ):
                if cell(row, name):
                    held[qid_of(row, name)] = cell(row, f"{name}Label")
        deaths: dict[str, list[tuple[tuple[str, str], ...]]] = defaultdict(
            list
        )
        for qid in sorted(events, key=natural_order):
            parts = [
                (participation(part["row"], part["roles"]), part["sources"])
                for part in events[qid]["parts"].values()
            ]
            # a deprecated statement is set aside first, so that it can
            # name no second principal and hide the death
            principal = event_principal(
                (each for each, _ in parts if each.rank != "DeprecatedRank"),
                best_rank_only=False,
            )
            if principal is None:
                continue
            sources = dict(events[qid]["kind"])
            for each, cited in parts:
                if (
                    each.value == principal
                    and each.rank != "DeprecatedRank"
                    and all(is_principal for _, is_principal in each.roles)
                ):
                    sources.update(cited)
            deaths[principal].append(
                tuple(
                    sorted(
                        sources.items(),
                        key=lambda pair: natural_order(pair[0]),
                    )
                )
            )
        return dict(deaths)

    def undated_death_html(
        self, sources: Iterable[tuple[str, str]], root: str = "../"
    ) -> str:
        """An undated death, "died (date not stated)", with the links to
        its sources as ``render_value`` gives a statement's."""
        links = self.artifact_links(sources, root)
        return esc(UNDATED_DEATH) + (
            f'<span class="source">{links}</span>' if links else ""
        )

    def person_link(self, qid: str, label: str, root: str = "../") -> str:
        """`label` linked to the page of the person `qid`, badged as the
        index badges a person the non-privileged export would drop; the
        label alone for an item the roster does not list."""
        person = self.people.get(qid)
        if person is None:
            return esc(label)
        badge = (
            ' <span class="badge">possibly living</span>'
            if self._possibly_living(person)
            else ""
        )
        return (
            f'<a href="{root}people/{esc(qid)}.html">{esc(label)}</a>{badge}'
        )

    def said_value(self, said: Said, root: str = "../") -> str:
        """A statement's value as HTML, with its qualifiers: a time at its
        stated precision (``format_time``), with its range and sourcing
        circumstances (circa) beside it; an item by its label, linked to
        its page when it is a person the roster lists; a monolingual text
        (a caption) quoted, verbatim, in its language."""
        row = said.row
        value = row.get("value", {})
        raw = value.get("value", "")
        where = f"{said.artifact} statement {qid_of(row, 'st')}"
        if value.get("datatype", "").endswith("dateTime"):
            shown = esc(format_time(raw, cell(row, "precision"), where))
        elif said.value_qid:
            shown = self.person_link(
                said.value_qid,
                cell(row, "valueLabel") or said.value_qid,
                root,
            )
        elif value.get("xml:lang"):
            shown = f'<q lang="{esc(value["xml:lang"])}">{esc(raw)}</q>'
        else:
            shown = esc(raw)
        return shown + qualifier_span(
            self.qualifier_text(cell(row, "qualifierCells"), where)
        )

    def citations(self, said: Said, itself: str, root: str = "../") -> str:
        """Where a statement is stated: each source linked to its page, or
        `itself` where the source is the artifact the statement is about,
        with the page and printed page it cites ("Our Family from Germany,
        page 6 (printed 4)"), "; "-separated."""
        parts = []
        for citation in said.citations:
            if citation.source == said.artifact:
                text = esc(itself)
            else:
                text = self.artifact_links(
                    [(citation.source, citation.label)], root
                )
            if citation.pages:
                noun = "pages" if len(citation.pages) > 1 else "page"
                text += f", {noun} {esc(', '.join(citation.pages))}"
            if citation.printed:
                text += f" (printed {esc(', '.join(citation.printed))})"
            parts.append((text, ""))
        return "; ".join(reading_order(parts))

    def sources_say_html(self, said: list[Said]) -> str:
        """An artifact page's "What sources say about this artifact": its
        captions, each with the container and page that give it, then
        its own statements, each property's values with their sources,
        values that disagree listed together and none preferred (CLAUDE.md
        rule 6); "" when no source says anything of the artifact."""
        if not said:
            return ""
        grouped: dict[tuple[bool, str], list[Said]] = defaultdict(list)
        for each in said:
            grouped[
                (each.key not in self._placed, each.property_label)
            ].append(each)
        body = [
            "<h2>What sources say about this artifact</h2>",
            '<p class="sub">what each source says of this artifact, '
            "labelled by the source that says it: a container's caption "
            "with the page it stands on, and what the artifact states of "
            "itself. Values that disagree are listed together; none is "
            "preferred.</p>",
            '<dl class="statements">',
        ]
        for group in sorted(grouped):
            body.append(f"<dt>{esc(group[1])}</dt>")
            entries = reading_order(
                (
                    self.said_value(each),
                    self.source_span(self.citations(each, "this artifact")),
                )
                for each in grouped[group]
            )
            body += [f"<dd>{entry}</dd>" for entry in entries]
        body.append("</dl>")
        return "".join(body)

    @staticmethod
    def source_span(citations: str) -> str:
        """Citations in the muted "— per" style; for a statement that
        cites none, "no recorded source" in the qualifiers' muted style."""
        if not citations:
            return '<span class="qualifiers"> — no recorded source</span>'
        return f'<span class="source">{citations}</span>'

    def named_in_html(self, naming: list[Said]) -> str:
        """A person page's "Named in artifacts": each document-subject
        statement whose value is the person, as the artifact it is about
        and what it names them as ("shown in" the portrait), with its
        qualifiers and its sources; "" for none."""
        if not naming:
            return ""
        entries = reading_order(
            (
                f"{esc(NAMED_AS.get(each.key, each.property_label))} "
                + self.artifact_links(
                    [(each.artifact, each.artifact_label or each.artifact)]
                )
                + qualifier_span(
                    self.qualifier_text(
                        cell(each.row, "qualifierCells"),
                        f"{each.artifact} statement {qid_of(each.row, 'st')}",
                    )
                ),
                self.source_span(self.citations(each, "the artifact itself")),
            )
            for each in naming
        )
        return (
            "<h2>Named in artifacts</h2>"
            '<p class="sub">what sources say of an artifact that names '
            "this person, such as its author, its addressee or whom it "
            "shows, each with its source</p><ul>"
            + "".join(f"<li>{entry}</li>" for entry in entries)
            + "</ul>"
        )

    def person_page(
        self,
        person: dict,
        statements: list[dict],
        naming: list[Said] | None = None,
        undated: list[tuple[tuple[str, str], ...]] | None = None,
    ) -> str:
        """A person's page: every statement about them, each value with
        its sources, under its property, and each death event of theirs
        that states no date (``undated``, its sources as
        ``undated_deaths`` gives them) under "death", where a statement of
        a property labelled "death" would stand too; then the artifacts
        that name them."""
        qid = qid_of(person, "person")
        label = cell(person, "personLabel") or qid
        aliases = [a for a in cell(person, "aliases").split("|") if a]
        body = [f"<h1>{esc(label)}"]
        if self._possibly_living(person):
            body.append(
                ' <span class="badge">possibly living — never in the '
                "non-privileged export</span>"
            )
        body.append("</h1>")
        description = cell(person, "personDescription")
        subtitle = " · ".join(
            part
            for part in (
                esc(description),
                "also known as " + esc("; ".join(aliases)) if aliases else "",
            )
            if part
        )
        if subtitle:
            body.append(f'<p class="sub">{subtitle}</p>')

        grouped: dict[str, list[str]] = defaultdict(list)
        for row in statements:
            prop = cell(row, "propLabel")
            # `instance of` is the subtitle, known by its id on the
            # instance
            if qid_of(row, "prop") == self.properties.get("instance_of"):
                continue
            grouped[prop].append(self.render_value(row))
        for sources in undated or []:
            grouped["death"].append(self.undated_death_html(sources))
        body.append('<dl class="statements">')
        for prop in sorted(grouped):
            body.append(f"<dt>{esc(prop)}</dt>")
            body += [f"<dd>{entry}</dd>" for entry in grouped[prop]]
        body.append("</dl>")
        body.append(self.named_in_html(naming or []))
        body.append(
            f'<p class="sub">'
            f'<a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
            f"curate {qid} in the wiki</a></p>"
        )
        return self._page(label, "".join(body), root="../")

    def render_value(self, row: dict) -> str:
        value = row.get("value", {})
        raw = value.get("value", "")
        if value.get("type") == "uri" and "/entity/Q" in raw:
            target = raw.rsplit("/", 1)[-1]
            text = cell(row, "valueLabel") or target
            rendered = f'<a href="{esc(target)}.html">{esc(text)}</a>'
        elif value.get("datatype", "").endswith("dateTime"):
            where = f"{qid_of(row, 'person')} statement {qid_of(row, 'st')}"
            rendered = esc(format_time(raw, cell(row, "precision"), where))
        else:
            rendered = esc(cell(row, "valueLabel") or raw)
        rendered += qualifier_span(
            self.qualifier_text(
                cell(row, "qualifierCells"),
                f"{qid_of(row, 'person')} statement {qid_of(row, 'st')}",
            )
        )
        sources = self.source_links(cell(row, "sources"))
        if sources:
            rendered += f'<span class="source">{sources}</span>'
        return rendered

    def asserted_statements(
        self, assertions: list[dict]
    ) -> list[tuple[dict, list[str]]]:
        """ASSERTIONS_QUERY's rows for one artifact as its statements, in
        the order the query gives them: each statement's first row, and
        each place in the artifact its reference blocks cite it at, as
        ``place_html`` shows it. Rows are a statement's blocks, and a
        block's rows its locators' values, so the statements are counted
        here, never the rows."""
        blocks: dict[str, dict[str, dict[str, list]]] = {}
        first: dict[str, dict] = {}
        for row in assertions:
            st = cell(row, "st")
            first.setdefault(st, row)
            block = blocks.setdefault(st, {}).setdefault(
                cell(row, "reference"), defaultdict(list)
            )
            for name in ("fragmentLabel", "page", "printedPage", "region"):
                text = cell(row, name)
                if (
                    name == "fragmentLabel"
                    and not text
                    and cell(row, "source") != cell(row, "artifact")
                ):
                    # a fragment folded into its whole that has no English
                    # label is named by its id
                    text = qid_of(row, "source")
                if text and text not in block[name]:
                    block[name].append(text)
            excerpt = row.get("excerpt")
            if excerpt:
                shown = (excerpt.get("value", ""), excerpt.get("xml:lang", ""))
                if shown[0] and shown not in block["excerpt"]:
                    block["excerpt"].append(shown)
        return [
            (
                first[st],
                sorted(
                    (self.place_html(block) for block in by_block.values()),
                    key=natural_order,
                ),
            )
            for st, by_block in blocks.items()
        ]

    def place_html(self, block: dict[str, list]) -> str:
        """Where one reference block cites the artifact, as HTML: its
        fragment (by its English label, or its id when it has none), page
        number, printed page number and image region, each
        in the `label: value` form, then its excerpt, quoted, in a
        ``q`` element whose ``lang`` is the excerpt's language. A block
        with none of them reads "the artifact as a whole"."""
        labels = disagreement.property_labels()
        parts = [
            f"{esc(label)}: "
            + esc(", ".join(sorted(block[name], key=natural_order)))
            for name, label in (
                ("fragmentLabel", "fragment"),
                ("page", labels["page_number"]),
                ("printedPage", labels["printed_page_number"]),
                ("region", labels["relative_position_within_image"]),
            )
            if block.get(name)
        ]
        place = "; ".join(parts)
        quoted = " ".join(
            (f'<q lang="{esc(lang)}">' if lang else "<q>") + f"{esc(text)}</q>"
            for text, lang in sorted(block.get("excerpt", []))
        )
        if place and quoted:
            return f"{place}: {quoted}"
        return place or quoted or "the artifact as a whole"

    def artifact_page(
        self,
        artifact: dict,
        assertions: list[dict],
        said: list[Said] | None = None,
    ) -> str:
        qid = qid_of(artifact, "artifact")
        label = cell(artifact, "artifactLabel") or qid
        body = [f"<h1>{esc(label)}</h1>"]
        subtitle = " · ".join(
            part
            for part in (
                esc(cell(artifact, "kindLabel")),
                esc(cell(artifact, "artifactDescription")),
            )
            if part
        )
        if subtitle:
            body.append(f'<p class="sub">{subtitle}</p>')
        file_name = cell(artifact, "file")
        if file_name:
            href = (
                f"{self.config.wikibase_url}/wiki/Special:FilePath/"
                + urllib.parse.quote(file_name.removeprefix("File:"))
            )
            body.append(
                f'<p><a href="{esc(href)}">⬇ download the original '
                f"({esc(file_name.removeprefix('File:'))})</a></p>"
            )
        body.append(self.sources_say_html(said or []))
        statements = self.asserted_statements(assertions)
        if statements:
            body.append(
                f"<h2>What this artifact asserts ({len(statements)})</h2>"
            )
            body.append(
                "<table><tr><th>About</th><th>Property</th><th>Value</th>"
                "<th>Where</th></tr>"
            )
            for row, places in statements:
                subject_qid = qid_of(row, "subject")
                subject = esc(cell(row, "subjectLabel") or subject_qid)
                # a person links to their page, an artifact (an image a
                # caption is about) to its own; any other item has none
                for folder, listed in (
                    ("people", self.people),
                    ("artifacts", self.artifact_qids),
                ):
                    if subject_qid in listed:
                        subject = (
                            f'<a href="../{folder}/{subject_qid}.html">'
                            f"{subject}</a>"
                        )
                        break
                where = (
                    f"{subject_qid} statement {qid_of(row, 'st')} as "
                    f"{qid} asserts it"
                )
                value = row.get("value", {})
                raw = value.get("value", "")
                if value.get("datatype", "").endswith("dateTime"):
                    shown = esc(
                        format_time(raw, cell(row, "precision"), where)
                    )
                else:
                    shown = esc(
                        cell(row, "valueLabel")
                        or (
                            raw.rsplit("/", 1)[-1]
                            if value.get("type") == "uri"
                            else raw
                        )
                    )
                shown += qualifier_span(
                    self.qualifier_text(cell(row, "qualifierCells"), where)
                )
                body.append(
                    f"<tr><td>{subject}</td>"
                    f"<td>{esc(cell(row, 'propLabel'))}</td>"
                    f"<td>{shown}</td>"
                    f"<td>{'<br>'.join(places)}</td></tr>"
                )
            body.append("</table>")
        else:
            body.append("<p>No statements reference this artifact yet.</p>")
        body.append(
            f'<p class="sub">'
            f'<a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
            f"curate {qid} in the wiki</a></p>"
        )
        return self._page(label, "".join(body), root="../")

    def index_page(self, people: list[dict], artifacts: list[dict]) -> str:
        body = ["<h1>Family archive</h1>"]
        body.append(
            f'<p class="sub">{len(people)} people · '
            f"{len(artifacts)} artifacts · "
            "every statement traceable to the source that asserts it</p>"
        )
        body.append('<h2>People</h2><ul class="roster">')
        for row in people:
            qid = qid_of(row, "person")
            label = esc(cell(row, "personLabel") or qid)
            badge = (
                ' <span class="badge">possibly living</span>'
                if self._possibly_living(row)
                else ""
            )
            body.append(
                f'<li><a href="people/{qid}.html">{label}</a>{badge}</li>'
            )
        body.append(
            "</ul><h2>Artifacts</h2>"
            "<table><tr><th>Artifact</th><th>Kind</th></tr>"
        )
        for row in artifacts:
            qid = qid_of(row, "artifact")
            label = esc(cell(row, "artifactLabel") or qid)
            body.append(
                f'<tr><td><a href="artifacts/{qid}.html">{label}</a></td>'
                f"<td>{esc(cell(row, 'kindLabel'))}</td></tr>"
            )
        body.append("</table>")
        return self._page("Family archive", "".join(body))

    def timeline_page(
        self,
        rows: list[dict],
        undated: dict[str, list[tuple[tuple[str, str], ...]]] | None = None,
    ) -> str:
        """The timeline: every dated row of the person-timeline query,
        under its year, each with its source; then, under "No date
        stated", each death a source gives no date for (``undated``, by
        the QID of the one who died, its sources as ``undated_deaths``
        gives them), by the name of the one who died."""
        body = [
            "<h1>Timeline</h1>",
            '<p class="sub">every dated assertion, labeled by its source — '
            "the same fact from two sources appears twice, on purpose. "
            "Beside a date is the range its source states for it, if any: "
            "its earliest and latest date, or for the start or end time a "
            "statement carries (a marriage, a residence, an occupation) the "
            "bounds of that end, a start running from its earliest date to "
            "its latest start date and an end from its earliest end date to "
            "its latest date. An event's own start or end time, like any "
            "other date, runs from its earliest date to its latest date. A "
            "date its source gives as circa says so there too.</p>",
            '<div class="timeline">',
        ]
        current_year = None
        for row in rows:
            date = cell(row, "date")
            person = qid_of(row, "person")
            where = f"{person} timeline date"
            # the header names the date's year, or a coarser date as
            # format_time shows it: its decade, century or millennium, or,
            # coarser than a millennium, its stored year with the span its
            # precision allows ("1900, to within 10,000 years"), never a
            # bare stored year
            precision = min(int(cell(row, "precision") or 11), 9)
            year = format_time(date, str(precision), where) if date else "?"
            if year != current_year:
                current_year = year
                body.append(f'<div class="year">{esc(year)}</div>')
            source_qid = qid_of(row, "source")
            source_label = cell(row, "sourceLabel")
            source = (
                f'<span class="source">'
                f'<a href="artifacts/{esc(source_qid)}.html">'
                f"{esc(source_label)}</a></span>"
                if source_label and source_qid.startswith("Q")
                else ""
            )
            body.append(
                '<div class="entry">'
                f"{esc(format_time(date, cell(row, 'precision'), where))}"
                f"{qualifier_span(self.timeline_qualifiers(row))} — "
                f'<a href="people/{esc(person)}.html">'
                f"{esc(cell(row, 'personLabel'))}</a>, "
                f"{esc(cell(row, 'what'))}" + source + "</div>"
            )
        body.append("</div>")
        entries = reading_order(
            (
                f'<a href="people/{esc(qid)}.html">'
                f"{esc(cell(self.people.get(qid, {}), 'personLabel') or qid)}"
                "</a>, ",
                self.undated_death_html(sources, root=""),
            )
            for qid, deaths in (undated or {}).items()
            for sources in deaths
        )
        if entries:
            body.append(
                '<h2>No date stated</h2><p class="sub">each death a source '
                "states with no date, so that it has no place above, with "
                'its source</p><div class="timeline">'
                + "".join(
                    f'<div class="entry">{entry}</div>' for entry in entries
                )
                + "</div>"
            )
        return self._page("Timeline", "".join(body))

    def timeline_feed(self, rows: list[dict]) -> str:
        """TimelineJS-compatible JSON, if the user later wants that UI.
        TimelineJS shows an event's headline and text, and a date's
        display_date, as HTML, so each is escaped. A date its source gives
        a range or a sourcing circumstance (circa) for carries a
        display_date, the date with its range and circumstances as the
        timeline page shows them, and so does any date coarser than a year,
        which TimelineJS would show as a bare stored year: it shows as
        ``format_time`` shows it, its decade, century or millennium, or,
        coarser than a millennium, its stored year with the span its
        precision allows ("1900, to within 10,000 years");
        TimelineJS shows any other date from its year, and from its month
        and day when its precision reaches them. A date before the common
        era raises UnsupportedDate, naming its person (ADR-0034), as on the
        timeline page."""
        events = []
        for row in rows:
            date = cell(row, "date")
            if not date:
                continue
            check_time(date, None, f"{qid_of(row, 'person')} timeline date")
            clean = date.lstrip("+").split("T")[0]
            parts = clean.split("-")
            precision = int(cell(row, "precision") or 11)
            start = {"year": parts[0]}
            if precision >= 10:
                start["month"] = parts[1]
            if precision >= 11:
                start["day"] = parts[2]
            stated = self.timeline_qualifiers(row)
            if stated or precision < 9:
                shown = format_time(
                    date,
                    cell(row, "precision"),
                    f"{qid_of(row, 'person')} timeline date",
                )
                start["display_date"] = esc(
                    f"{shown} ({stated})" if stated else shown
                )
            events.append(
                {
                    "start_date": start,
                    "text": {
                        "headline": esc(
                            f"{cell(row, 'personLabel')}: {cell(row, 'what')}"
                        ),
                        "text": esc(
                            f"per {cell(row, 'sourceLabel') or 'unattributed'}"
                        ),
                    },
                }
            )
        return json.dumps(
            {
                "title": {"text": {"headline": "Family archive timeline"}},
                "events": events,
            },
            ensure_ascii=False,
            indent=2,
        )

    def gallery_page(self, rows: list[dict]) -> str:
        body = [
            "<h1>Gallery</h1>",
            '<p class="sub">photograph artifacts, including media derived '
            "from compound artifacts — each with its provenance</p>",
            '<div class="gallery">',
        ]
        for row in rows:
            image = cell(row, "image")
            label = cell(row, "photoLabel") or qid_of(row, "photo")
            container = cell(row, "containerLabel")
            caption = esc(label) + (
                f'<br><span class="source">{esc(container)}</span>'
                if container
                else ""
            )
            body.append(
                f'<figure><a href="{esc(image)}"><img src="{esc(image)}" '
                f'alt="{esc(label)}" loading="lazy"></a>'
                f"<figcaption>{caption}</figcaption></figure>"
            )
        body.append("</div>")
        if not rows:
            body.append("<p>No photographs in the graph yet.</p>")
        return self._page("Gallery", "".join(body))

    def places_page(
        self, rows: list[dict], tree_rows: list[dict] | None = None
    ) -> str:
        body = ["<h1>Places</h1>"]
        with_coordinates = [row for row in rows if cell(row, "coords")]
        if not with_coordinates:
            body.append(
                '<p class="sub">no place carries a '
                "<em>coordinate location</em> "
                "statement yet — the map lights up once places are reconciled "
                "to coordinates (GeoNames pass, see docs/MODELING.md)</p>"
            )
        body.append(self.place_tree_html(tree_rows or []))
        body.append(
            "<table><tr><th>Place</th><th>Coordinates</th>"
            "<th>Connected to</th></tr>"
        )
        seen = set()
        for row in rows:
            key = (qid_of(row, "place"), cell(row, "connectionLabel"))
            if key in seen:
                continue
            seen.add(key)
            body.append(
                f"<tr><td>{esc(cell(row, 'placeLabel'))}</td>"
                f"<td>{esc(cell(row, 'coords'))}</td>"
                f"<td>{esc(cell(row, 'connectionLabel'))}</td></tr>"
            )
        body.append("</table>")
        return self._page("Places", "".join(body))

    def place_tree_html(self, tree_rows: list[dict]) -> str:
        """Nested containment tree (ADR-0009): a visible hierarchy is a
        self-auditing one. Places with no recorded container list last."""
        if not tree_rows:
            return ""
        labels: dict[str, str] = {}
        children: dict[str, list[str]] = defaultdict(list)
        parented: set[str] = set()
        for row in tree_rows:
            child = qid_of(row, "child")
            labels[child] = cell(row, "childLabel") or child
            parent = qid_of(row, "parent")
            if parent:
                if child not in children[parent]:
                    children[parent].append(child)
                parented.add(child)

        def render(qid: str, seen: frozenset) -> str:
            if qid in seen:  # cycle safety in display
                return ""
            label = esc(labels.get(qid, qid))
            below = "".join(
                render(child, seen | {qid})
                for child in sorted(
                    children.get(qid, []), key=lambda q: labels.get(q, q)
                )
            )
            return f"<li>{label}{f'<ul>{below}</ul>' if below else ''}</li>"

        roots = sorted(
            (
                qid
                for qid in labels
                if qid not in parented and children.get(qid)
            ),
            key=lambda q: labels.get(q, q),
        )
        unplaced = sorted(
            (
                qid
                for qid in labels
                if qid not in parented and not children.get(qid)
            ),
            key=lambda q: labels.get(q, q),
        )
        parts = ["<h2>Containment</h2><ul>"]
        parts += [render(qid, frozenset()) for qid in roots]
        parts.append("</ul>")
        if unplaced:
            parts.append(
                f'<p class="sub">{len(unplaced)} place(s) with no recorded '
                "container yet: "
                + "; ".join(esc(labels[qid]) for qid in unplaced)
                + "</p>"
            )
        return "".join(parts)

    def inconsistencies_page(self, rows: list[Disagreement]) -> str:
        """The disagreement report (yizkr.disagreement) as a page: both
        values of every row, each with its sources and at its stated
        precision (ADR-0002, rule 6)."""
        body = [
            "<h1>Inconsistencies</h1>",
            '<p class="sub">properties where different sources disagree — '
            "the archive's "
            "flagship view (ADR-0002): coexistence is the healthy resting "
            "state, and this page exists so a reader can weigh the evidence. "
            "Two readings of one artifact are not two sources, and a value a "
            "curator deprecated is left out. A source that names two fathers "
            "or two mothers states them together: neither disagrees with a "
            "source naming one of them. Differences of resolution are "
            "not listed: a date whose span lies inside another's (the period "
            "its precision names, or the range its source states with "
            "earliest and latest dates), or a place the graph records as "
            "contained in the other. A residence's or a spouse's start and "
            "end each have a range of their own: a start runs from its "
            "earliest date to its latest start date, an end from its "
            "earliest end date to its latest date.</p>",
            "<table><tr><th>Who/what</th><th>Property</th>"
            "<th>Value A</th><th>per</th><th>Value B</th><th>per</th></tr>",
        ]

        def aspect(row: Disagreement) -> str:
            """The property; for a dated residence or spouse, also the
            place or person both statements name and the date compared."""
            text = esc(row.property_label)
            if row.shared:
                shared = row.shared_label or row.shared.rsplit("/", 1)[-1]
                text += f" ({esc(shared)})"
            if row.qualifier:
                text += f", {esc(row.qualifier_label)}"
            return text

        def stated_range(row: Disagreement, side: Side) -> str:
            """The range a statement gives the date compared, worded as the
            timeline words a date's (``range_text``): for a value its
            earliest and latest dates, and for a residence's or a spouse's
            start or end the bounds of that end (disagreement.BOUNDS)."""
            labels = disagreement.property_labels()
            low, high = disagreement.BOUNDS[row.qualifier]
            return qualifier_span(
                range_text(
                    (
                        (labels[key], time.value, str(time.precision))
                        for key, time in (
                            (low, side.value.earliest),
                            (high, side.value.latest),
                        )
                        if time is not None
                    ),
                    f"{row.entity_qid} {row.property}",
                )
            )

        def rendered(row: Disagreement, side: Side) -> str:
            value = side.value
            if value.term.is_time:
                precision = "" if value.precision is None else value.precision
                shown = format_time(
                    value.term.value,
                    str(precision),
                    f"{row.entity_qid} {row.property}",
                )
                return esc(shown) + stated_range(row, side)
            if value.term.kind == "uri":
                return esc(value.label or value.term.value.rsplit("/", 1)[-1])
            return esc(value.label or value.term.value)

        def sources(side: Side) -> str:
            return self.artifact_links(
                ((source.qid, source.label) for source in side.sources),
                root="",
            )

        for row in rows:
            body.append(
                f"<tr><td>{esc(row.entity_label)}</td>"
                f"<td>{aspect(row)}</td>"
                f"<td>{rendered(row, row.a)}</td>"
                f'<td class="source" style="font-style:italic">'
                f"{sources(row.a)}</td>"
                f"<td>{rendered(row, row.b)}</td>"
                f'<td class="source" style="font-style:italic">'
                f"{sources(row.b)}</td></tr>"
            )
        body.append("</table>")
        return self._page("Inconsistencies", "".join(body))


def query_service_probe(config: Config):
    """A reader of what the instance's query service shows now: called,
    it gives (the scratch runs whose run items it shows, the newest
    revision of an entity it holds). Its clients are made once."""
    builder = SiteBuilder(config, Path())

    def probe() -> tuple[list[str], int]:
        return builder.query_scratch_runs(), builder.newest_revision()

    return probe


def publish_site(config: Config, out_dir: Path, clean: bool = False) -> dict:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    return SiteBuilder(config, out_dir).build()
