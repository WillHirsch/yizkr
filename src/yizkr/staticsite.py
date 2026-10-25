"""Static publish tier (frontend ladder rung 2): live SPARQL -> site.

`yizkr publish-site` renders the graph into a self-contained static
site: person pages showing EVERY attributed value side by side with
its source label (R6/ADR-0002 — never silently pick one), a date of
death of unknown value as "died (date not stated)", beside the dated
ones, each "died by" date the shared derivation gives (yizkr.died_by,
ADR-0040 §2.4), labelled as derived, each union the person takes part
in with its stated end, or the end a view derives (§2.6), and the
artifacts that name the person; artifact pages showing what the
artifact asserts and what sources say about it, a container's captions
and the artifact's own statements about itself (ADR-0039 §7); a
timeline that shows beside each date the range its source states, a
death of unknown value beside the person's dated death, and at its foot
the deaths nothing dates; a photo gallery (ADR-0008 derived media
included), the disagreement report, and a places index that becomes a
map once places carry coordinates. A death event a curator makes that
states no date shows as "died (date not stated)" with its sources on
its principal's page and timeline (ADR-0040 Q5).

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
import logging
import re
import shutil
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from yizkr import died_by, disagreement
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
from yizkr.model.schema import Acceptance, load_acceptance, load_manifest
from yizkr.queries.publish import load_templates, prefix_block, render

logger = logging.getLogger(__name__)

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
# (date of death, dated or of unknown value, place, cause or age at death,
# place of burial), none of which needs a date; each of the privacy module's
# DEATH_EVIDENCE_PROPERTIES has its arm. A union's end by the person's
# death, curator-made death events, whose principal alone they mark
# deceased, and births, for the birth-recency cutoff, are read apart, with
# the reading the non-privileged export uses (core/privacy.py's
# read_evidence).
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
     || EXISTS {{ ?person wdt:{age_at_death} [] }}
       ) AS ?deceased)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?person ?personLabel ?personDescription ?deceased ?family
ORDER BY ?personLabel
"""

# the death statements that release each listed person, for the run
# record's releases by cause (ADR-0040 §7): every best-rank statement of
# the DEATH_EVIDENCE_PROPERTIES, as ?deceased reads them, with whether its
# value is an unknown value (?unknown: a blank node, or the skolem IRI the
# query service gives one). It returns no value's own text, which the
# count does not need; the source types are read apart
# (RELEASE_SOURCES_QUERY), keyed by ?statement.
RELEASES_QUERY = """\
SELECT ?person ?releaseKey ?statement ?unknown
WHERE {{
{people}
  VALUES (?claim ?statementValue ?releaseKey) {{
    (p:{date_of_death} ps:{date_of_death} "date_of_death")
    (p:{place_of_death} ps:{place_of_death} "place_of_death")
    (p:{cause_of_death} ps:{cause_of_death} "cause_of_death")
    (p:{burial_place} ps:{burial_place} "burial_place")
    (p:{age_at_death} ps:{age_at_death} "age_at_death")
  }}
  ?person ?claim ?statement .
  ?statement a wikibase:BestRank ;
             ?statementValue ?value .
  BIND(isBlank(?value) || CONTAINS(STR(?value), "/.well-known/genid/")
       AS ?unknown)
}}
"""

# the sources of each best-rank date of death of unknown value, for the
# releases by the type of their source: each stated-in source, with the
# artifact a page fragment belongs to (?whole); the site maps each to its
# artifact's type from ARTIFACTS_QUERY, in Python
RELEASE_SOURCES_QUERY = """\
SELECT ?statement ?source ?whole
WHERE {{
  ?person p:{date_of_death} ?statement .
  ?statement a wikibase:BestRank ;
             ps:{date_of_death} ?value .
  FILTER(isBlank(?value) || CONTAINS(STR(?value), "/.well-known/genid/"))
  ?statement prov:wasDerivedFrom/pr:{stated_in} ?source .
  OPTIONAL {{
    ?source wdt:{instance_of} wd:{artifact_fragment} ;
            wdt:{part_of} ?whole .
  }}
}}
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
SELECT ?artifact ?artifactLabel ?artifactDescription ?kind ?kindLabel ?file
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

# every death event that states no date, for the person page and the timeline
# of the one who died: only a curator makes one (ADR-0040 Q5), since a source's
# death is statements on the person. An item with a `death` type at any rank
# but deprecated (through redirects) and no `point in time`, `start time` or
# `end time` with a time value at any rank but deprecated. A row for each role
# each of its participant statements states (one row with no role when it
# states none), read through the statement node at every rank, as the privacy
# module's participants query reads them, so that ``event_principal`` can say
# whose death it is; and beside it each source the participant statement and
# the type statement are stated in, the label optional, so a source with no
# English label is still shown, by its id.
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
# date for, and a burial it gives no place for (ADR-0040 §2.2, Q3)
UNDATED_DEATH = "died (date not stated)"
UNPLACED_BURIAL = "buried (place not stated)"
# any other value its source states exists but does not give
UNSTATED_VALUE = "a value its source does not state"

# the causes the run record counts a release under (``privacy_record``),
# and the kind it gives a source with no type
RELEASE_CAUSES = (
    "date_of_death",
    "unknown_date_of_death",
    "age_at_death",
    "unknown_burial_place",
    "other_death_property",
    "ended_by_death_of",
    "death_event",
    "birth_cutoff",
)
UNSTATED_KIND = "no type stated"
OTHER_KIND = "other"

# every union, a partnership or a marriage or a class the graph makes a
# subclass of one, typed at any rank but deprecated: each partner its
# participant statements name, at any rank but deprecated, and each
# stated end, an `ended by` event or an `ended by death of` partner, at
# any rank but deprecated, with each source it is stated in. The person
# page shows each union a person takes part in with its stated end, or,
# where none is stated, the end the shared derivation gives (ADR-0040
# §2.6); the timeline shows an end event at its dates (UNION_END_DATES).
UNIONS_QUERY = """\
SELECT ?union ?unionLabel ?partner ?partnerLabel ?endProp ?end ?endLabel
       ?endStatement ?source ?sourceLabel
WHERE {{
  ?union p:{instance_of} ?typing .
  ?typing ps:{instance_of}/wdt:{subclass_of}* wd:{partnership} ;
          wikibase:rank ?typingRank .
  FILTER(?typingRank != wikibase:DeprecatedRank)
  ?union p:{participant} ?participation .
  ?participation ps:{participant} ?partner .
  FILTER NOT EXISTS {{ ?participation wikibase:rank wikibase:DeprecatedRank }}
  OPTIONAL {{
    ?union rdfs:label ?unionLabel . FILTER(LANG(?unionLabel) = "en")
  }}
  OPTIONAL {{
    ?partner rdfs:label ?partnerLabel . FILTER(LANG(?partnerLabel) = "en")
  }}
  OPTIONAL {{
    VALUES ?endProp {{ wd:{ended_by} wd:{ended_by_death_of} }}
    ?endProp wikibase:claim ?endClaim ;
             wikibase:statementProperty ?endValue .
    ?union ?endClaim ?endStatement .
    ?endStatement ?endValue ?end .
    FILTER NOT EXISTS {{
      ?endStatement wikibase:rank wikibase:DeprecatedRank
    }}
    OPTIONAL {{ ?end rdfs:label ?endLabel . FILTER(LANG(?endLabel) = "en") }}
    OPTIONAL {{
      ?endStatement prov:wasDerivedFrom/pr:{stated_in} ?source .
      OPTIONAL {{
        ?source rdfs:label ?sourceLabel . FILTER(LANG(?sourceLabel) = "en")
      }}
    }}
  }}
}}
"""

# every date a union's end event states (a divorce's), one row for each
# partner, each point-in-time statement of the event at any rank but
# deprecated and each source that statement is stated in, in the
# person-timeline query's form: the date at its stated precision, the
# earliest and latest dates its source states for it (named by ?low and
# ?high), its sourcing circumstances folded into ?circumstances, and the
# date statement's own source. So every attributed date shows, each with
# its own source, and none is picked (ADR-0002, rule 6). ?endedBy is the
# union's end statement; its own sources are UNIONS_QUERY's.
UNION_END_DATES_QUERY = """\
SELECT ?union ?end ?dateStatement ?person ?personLabel ?date ?precision
       ?low ?earliest ?earliestPrecision ?high ?latest ?latestPrecision
       ?circumstances ?source ?sourceLabel
WHERE {{
  ?union p:{ended_by} ?endedBy .
  ?endedBy ps:{ended_by} ?end .
  FILTER NOT EXISTS {{ ?endedBy wikibase:rank wikibase:DeprecatedRank }}
  ?union p:{participant} ?participation .
  ?participation ps:{participant} ?person .
  FILTER NOT EXISTS {{
    ?participation wikibase:rank wikibase:DeprecatedRank
  }}
  ?end p:{point_in_time} ?dateStatement .
  FILTER NOT EXISTS {{
    ?dateStatement wikibase:rank wikibase:DeprecatedRank
  }}
  wd:{point_in_time} wikibase:statementValue ?dateLink .
  ?dateStatement ?dateLink ?dateNode .
  ?dateNode wikibase:timeValue ?date ;
            wikibase:timePrecision ?precision .
  BIND(wd:{earliest_date} AS ?low)
  BIND(wd:{latest_date} AS ?high)
  OPTIONAL {{
    wd:{earliest_date} wikibase:qualifierValue ?lowLink .
    ?dateStatement ?lowLink ?lowNode .
    ?lowNode wikibase:timeValue ?earliest ;
             wikibase:timePrecision ?earliestPrecision .
  }}
  OPTIONAL {{
    wd:{latest_date} wikibase:qualifierValue ?highLink .
    ?dateStatement ?highLink ?highNode .
    ?highNode wikibase:timeValue ?latest ;
              wikibase:timePrecision ?latestPrecision .
  }}
  OPTIONAL {{
    SELECT ?dateStatement
           (GROUP_CONCAT(DISTINCT ?circumstanceCell; separator="|")
            AS ?circumstances)
    WHERE {{
      ?dateStatement pq:{sourcing_circumstances} ?circumstance .
      OPTIONAL {{ ?circumstance rdfs:label ?circumstanceName .
                 FILTER(LANG(?circumstanceName) = "en") }}
      BIND(ENCODE_FOR_URI(COALESCE(?circumstanceName,
                                   REPLACE(STR(?circumstance), "^.*/", "")))
           AS ?circumstanceCell)
    }}
    GROUP BY ?dateStatement
  }}
  OPTIONAL {{
    ?dateStatement prov:wasDerivedFrom/pr:{stated_in} ?source .
    OPTIONAL {{
      ?source rdfs:label ?sourceLabel . FILTER(LANG(?sourceLabel) = "en")
    }}
  }}
  OPTIONAL {{
    ?person rdfs:label ?personLabel . FILTER(LANG(?personLabel) = "en")
  }}
}}
"""


def _day(stamp: str) -> str:
    """A time as the query service gives it, as "YYYY-MM-DD", for ordering
    rows by date."""
    return stamp.lstrip("+")[:10]


def unknown_value(row: dict, name: str = "value") -> bool:
    """Whether a query row's value is Wikibase's "some value", a value its
    source states exists but does not give (ADR-0040 §2.2)."""
    return died_by.unknown_term(row.get(name))


def when_time(when: died_by.When) -> tuple[str, str]:
    """A derived date as the query service gives a time: its xsd:dateTime
    and its Wikibase precision, for ``format_time``."""
    return (
        f"+{when.year:04d}-{when.month or 0:02d}-{when.day or 0:02d}"
        "T00:00:00Z",
        str(died_by.WIKIBASE_PRECISION[when.precision]),
    )


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
class UnionShown:
    """A union as the person page shows it (UNIONS_QUERY): its QID and
    label, each partner by QID with their label, and each stated end, as
    (the end's property key, the end's QID, its label, its sources as
    (QID, label) pairs); and the rows of each end event's dates
    (UNION_END_DATES_QUERY), by the event's QID."""

    qid: str
    label: str
    partners: dict[str, str]
    ends: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...]
    end_dates: dict[str, tuple[dict, ...]] = field(default_factory=dict)


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
        # the artifacts that have a page of their own, and their labels
        self.artifact_qids: set[str] = set()
        self.artifact_labels: dict[str, str] = {}
        # what the shared "died by" derivation reads from the graph, and
        # the bounds it gives (ADR-0040 §2.4)
        self._deaths = died_by.Inputs()
        self._derived: list[died_by.DiedBy] = []

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

    def privacy_record(
        self,
        people: list[dict],
        releases: list[dict],
        sources: list[dict],
        kinds: dict[str, str],
    ) -> dict:
        """The run record's possibly-living count and its releases by
        cause (ADR-0040 §7): each released person counts once under every
        cause the graph holds for them, so the causes overlap. A dated
        `date_of_death`; an unknown one, by the type of its source; an
        `age_at_death`; an unknown `burial_place`; another death property;
        a union `ended by death of` them, with the people it alone
        releases; the principal of a death event; and the birth cutoff,
        for a person released with no death evidence. Families are left
        out, as the export leaves them. ``kinds`` gives each artifact's
        type, for the derived "died by" dates by the type of the document
        each rests on, and the type of each source of an unknown date of
        death (``sources``, RELEASE_SOURCES_QUERY's rows: a fragment by its
        whole). A source's type is its manifest key, UNSTATED_KIND for none,
        and OTHER_KIND for a class the manifest does not hold."""
        causes: dict[str, set[str]] = defaultdict(set)
        unknown_by_kind: dict[str, set[str]] = defaultdict(set)
        source_kinds: dict[str, set[str]] = defaultdict(set)
        for row in sources:
            artifact = qid_of(row, "whole") or qid_of(row, "source")
            source_kinds[cell(row, "statement")].add(
                kinds.get(artifact) or UNSTATED_KIND
            )
        for row in releases:
            qid = qid_of(row, "person")
            key = cell(row, "releaseKey")
            unknown = cell(row, "unknown") == "true"
            if key == "date_of_death" and unknown:
                causes["unknown_date_of_death"].add(qid)
                for kind in source_kinds.get(cell(row, "statement")) or {
                    UNSTATED_KIND
                }:
                    unknown_by_kind[kind].add(qid)
            elif key == "burial_place" and unknown:
                causes["unknown_burial_place"].add(qid)
            elif key in ("date_of_death", "age_at_death"):
                causes[key].add(qid)
            else:
                causes["other_death_property"].add(qid)
        for qid in self._evidence.ended:
            causes["ended_by_death_of"].add(qid)
        for qid in self._evidence.death_events:
            causes["death_event"].add(qid)
        persons = [
            qid_of(row, "person")
            for row in people
            if cell(row, "family") != "true"
        ]
        living = {
            qid_of(row, "person")
            for row in people
            if cell(row, "family") != "true" and self._possibly_living(row)
        }
        released = [qid for qid in persons if qid not in living]
        found = {cause: 0 for cause in RELEASE_CAUSES}
        alone = 0
        for qid in released:
            held = {cause for cause, who in causes.items() if qid in who}
            for cause in held:
                found[cause] += 1
            if not held:
                found["birth_cutoff"] += 1
            if held == {"ended_by_death_of"}:
                alone += 1
        derived: dict[str, int] = defaultdict(int)
        for bound in self._derived:
            source = bound.source.rsplit("/", 1)[-1]
            derived[kinds.get(source) or UNSTATED_KIND] += 1
        return {
            "people": len(persons),
            "possibly_living": len(living),
            "released": len(released),
            "releases": found,
            "ended_by_death_of_alone": alone,
            "unknown_date_of_death_by_source": {
                kind: len(who) for kind, who in sorted(unknown_by_kind.items())
            },
            "derived_by_source": dict(sorted(derived.items())),
        }

    def _releases(self, people: list[dict], artifacts: list[dict]):
        """The run record's releases by cause (``privacy_record``), or None
        when the query service cannot answer their queries: they are run
        evidence, so a failure is logged and leaves them out of the
        record (the run report says they are not available), and never
        stops the site."""
        try:
            releases = self.run(self._instance_query(RELEASES_QUERY))
            sources = self.run(self._instance_query(RELEASE_SOURCES_QUERY))
        except sparql.SparqlError as error:
            logger.warning(
                "the releases by cause are not counted: the query service "
                "did not answer their query (%s)",
                str(error)[:300],
            )
            return None
        return self.privacy_record(
            people,
            releases,
            sources,
            {
                qid_of(row, "artifact"): self._kind_key(row, "kind")
                for row in artifacts
            },
        )

    def _kind_key(self, row: dict, name: str) -> str:
        """The manifest key of the class a row's ``name`` gives."""
        if not cell(row, name):
            return UNSTATED_KIND
        keys = {qid: key for key, qid in self.items.items()}
        return keys.get(qid_of(row, name), OTHER_KIND)

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

    @staticmethod
    def timeline_what(row: dict) -> str:
        """What a person-timeline row dates. An event's row carries its
        types in ?eventTypes, each label URI-encoded and "|"-separated,
        and they are sorted, ignoring case, since GROUP_CONCAT gives them
        in no set order, and joined: "event, military service". Its start
        or end time's row names the time beside them ("imprisonment (start
        time)"), and its point in time's row the types alone. Any other
        row is named by its ?what."""
        what = cell(row, "what")
        types = sorted(
            (
                urllib.parse.unquote(each)
                for each in cell(row, "eventTypes").split("|")
                if each
            ),
            key=str.casefold,
        )
        if not types:
            return what
        named = ", ".join(types)
        return f"{named} ({what})" if what else named

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
        deaths = died_by.from_rows(self.run(self._library_query("died-by")))
        unions = self.unions(
            self.run(self._instance_query(UNIONS_QUERY)),
            self.run(self._instance_query(UNION_END_DATES_QUERY)),
        )
        self.people = {qid_of(row, "person"): row for row in people}
        self.artifact_qids = {qid_of(row, "artifact") for row in artifacts}
        self.artifact_labels = {
            qid_of(row, "artifact"): cell(row, "artifactLabel")
            for row in artifacts
        }
        self._deaths = deaths
        self._derived = died_by.died_by(deaths)

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
                    [u for u in unions if qid in u.partners],
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
        self._write(
            "timeline.html",
            self.timeline_page(
                timeline, undated, self.unknown_deaths(), unions
            ),
        )
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
            # the dates of death of unknown value, and the "died by" dates
            # the shared derivation gives, by how each was derived
            "unknown_deaths": sum(
                len(each) for each in self.unknown_deaths().values()
            ),
            "derived_died_by": {
                via: sum(bound.via == via for bound in self._derived)
                for via in died_by.VIA
            },
            "unions": len(unions),
            "privacy": self._releases(people, artifacts),
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

    def unions(
        self, rows: list[dict], date_rows: list[dict] | None = None
    ) -> list[UnionShown]:
        """UNIONS_QUERY's rows as unions, in the order of their QIDs: each
        partner, and each stated end with its sources in the order of
        their QIDs. An end whose property the manifest does not know is
        left out. ``date_rows`` (UNION_END_DATES_QUERY's) give each end
        event's dates, every row kept: each is a date statement's, for
        one partner and one source."""
        dates: dict[str, dict[str, list[dict]]] = {}
        for row in date_rows or []:
            dates.setdefault(qid_of(row, "union"), {}).setdefault(
                qid_of(row, "end"), []
            ).append(row)
        found: dict[str, dict] = {}
        for row in rows:
            union = found.setdefault(
                qid_of(row, "union"),
                {"label": cell(row, "unionLabel"), "partners": {}, "ends": {}},
            )
            partner = qid_of(row, "partner")
            if partner:
                union["partners"][partner] = (
                    cell(row, "partnerLabel") or partner
                )
            statement = cell(row, "endStatement")
            key = self._keys_by_id.get(qid_of(row, "endProp"))
            if not statement or key is None:
                continue
            end = union["ends"].setdefault(
                statement,
                {
                    "key": key,
                    "end": qid_of(row, "end"),
                    "label": cell(row, "endLabel"),
                    "sources": {},
                },
            )
            if cell(row, "source"):
                end["sources"][qid_of(row, "source")] = cell(
                    row, "sourceLabel"
                )
        return [
            UnionShown(
                qid=qid,
                label=found[qid]["label"] or qid,
                partners=found[qid]["partners"],
                ends=tuple(
                    (
                        end["key"],
                        end["end"],
                        end["label"] or end["end"],
                        tuple(
                            sorted(
                                end["sources"].items(),
                                key=lambda pair: natural_order(pair[0]),
                            )
                        ),
                    )
                    for end in found[qid]["ends"].values()
                ),
                end_dates={
                    end: tuple(found_dates)
                    for end, found_dates in (dates or {}).get(qid, {}).items()
                },
            )
            for qid in sorted(found, key=natural_order)
        ]

    def _sources_of(self, documents: Iterable[str]) -> tuple:
        """Documents by IRI as (QID, label) pairs, in the order of their
        QIDs, each labelled as its artifact's page is."""
        qids = sorted(
            {document.rsplit("/", 1)[-1] for document in documents},
            key=natural_order,
        )
        return tuple((qid, self.artifact_labels.get(qid, "")) for qid in qids)

    def unknown_deaths(self) -> dict[str, list[tuple[tuple[str, str], ...]]]:
        """Each date of death of unknown value a curator has not
        deprecated, by the QID of the person, its sources as (QID, label)
        pairs: a death its source states without its date (ADR-0040
        §2.2)."""
        found: dict[str, list] = defaultdict(list)
        for death in self._deaths.deaths:
            if death.dating is None and not death.deprecated:
                found[death.person.rsplit("/", 1)[-1]].append(
                    self._sources_of(death.sources)
                )
        return dict(found)

    def derived_html(self, qid: str, root: str = "../") -> list[str]:
        """Each "died by" date the shared derivation gives the person, as
        "died by 25 December 1958 (derived)", with the document it is
        derived from (ADR-0040 §2.4): labelled as derived, never a value a
        source states."""
        return [
            self.died_by_html(bound, root)
            for bound in self._derived
            if bound.person.rsplit("/", 1)[-1] == qid
        ]

    def died_by_html(self, bound: died_by.DiedBy, root: str = "../") -> str:
        """One derived "died by" date, labelled as derived, with how it is
        derived and the document it is derived from."""
        qid = bound.person.rsplit("/", 1)[-1]
        stamp, precision = when_time(bound.by)
        text = format_time(stamp, precision, f"{qid} died-by date")
        links = self.artifact_links(self._sources_of([bound.source]), root)
        return (
            esc(f"died by {text} (derived)")
            + qualifier_span(
                "the document's own date"
                if bound.via == died_by.UNDATED_DEATH
                else "the date it presents the survivor as widowed, "
                "or else its own"
            )
            + (f'<span class="source">{links}</span>' if links else "")
        )

    def _derived_end(self, union: UnionShown) -> died_by.DerivedEnd | None:
        """The end the shared derivation gives a union from its partners'
        deaths (ADR-0040 §2.6), its partners by QID; None for none."""
        deaths, ends = self._deaths.deaths, self._deaths.ends
        iri = {
            each.rsplit("/", 1)[-1]: each
            for each in (
                *(death.person for death in deaths),
                *(end.late for end in ends),
                *(bound.person for bound in self._derived),
            )
        }
        found = died_by.derived_end(
            [iri.get(p, p) for p in union.partners],
            deaths,
            ends,
            self._derived,
        )
        if found is None:
            return None
        return died_by.DerivedEnd(
            by_death_of=found.by_death_of.rsplit("/", 1)[-1]
            if found.by_death_of
            else None,
            no_later_than=tuple(
                p.rsplit("/", 1)[-1] for p in found.no_later_than
            ),
        )

    def union_ends(self, union: UnionShown) -> list[tuple[str, str]]:
        """A union's end by a death, as the timeline shows it beside the
        death of the partner it names (ADR-0040 §2.6): each stated
        `ended_by_death_of`, with its sources, or, where no source states
        how it ended, the end the shared derivation gives, labelled as
        derived; as (that partner's QID, the entry's HTML)."""
        named = esc(union.label)
        stated = [
            (end, label, sources)
            for key, end, label, sources in union.ends
            if key == "ended_by_death_of"
        ]
        found = []
        for end, label, sources in stated:
            links = self.artifact_links(sources, root="")
            source = f'<span class="source">{links}</span>' if links else ""
            found.append(
                (
                    end,
                    f"{named}, ended by the death of "
                    f"{self.person_link(end, label, root='')}{source}",
                )
            )
        if union.ends:
            return found
        derived = self._derived_end(union)
        if derived is None:
            return []
        if derived.by_death_of:
            label = union.partners.get(derived.by_death_of, "")
            return [
                (
                    derived.by_death_of,
                    f"{named}, ended by "
                    f"{self.person_link(derived.by_death_of, label, root='')}"
                    "&#x27;s death (derived)",
                )
            ]
        found = []
        for partner in derived.no_later_than:
            label = union.partners.get(partner, "")
            found.append(
                (
                    partner,
                    f"{named}, ended no later than "
                    f"{self.person_link(partner, label, root='')}"
                    "&#x27;s death (derived)",
                )
            )
        return found

    def other_ends(self, union: UnionShown) -> list[tuple[str, dict | None]]:
        """A union's ends by an event (`ended by`: a divorce, an
        annulment), as the timeline shows them, for each listed partner:
        each row of the end event's dates (UNION_END_DATES_QUERY), each a
        date statement's with its own range, circumstances and source, or
        None where the event states no date; each row carries, under
        ``_what``, the end with the sources of the union's end statement,
        which are kept apart from the date's own; as (the partner's QID,
        the row)."""
        found = []
        for key, end, label, sources in union.ends:
            if key == "ended_by_death_of":
                continue
            links = self.artifact_links(sources, root="")
            entry = f"{esc(union.label)}, ended by {esc(label)}" + (
                f'<span class="source">{links}</span>' if links else ""
            )
            rows = union.end_dates.get(end, ())
            for partner in sorted(union.partners, key=natural_order):
                if partner not in self.people:
                    continue
                mine = [r for r in rows if qid_of(r, "person") == partner]
                if not mine:
                    found.append((partner, {"_what": entry}))
                    continue
                for row in mine:
                    found.append(
                        (
                            partner,
                            {**row, "_what": entry, "_dated": True},
                        )
                    )
        return found

    def end_dates_html(
        self, union: UnionShown, key: str, end: str, qid: str
    ) -> str:
        """An end event's dates as the person page shows them beside the
        end: every date statement of the event (UNION_END_DATES_QUERY's
        rows for this partner), each at its stated precision with the
        range and circumstances its source states and that statement's own
        source, apart from the end statement's citation; "" for an end by
        a death or an undated event."""
        if key == "ended_by_death_of":
            return ""
        shown = []
        for row in union.end_dates.get(end, ()):
            if qid_of(row, "person") != qid:
                continue
            where = f"{qid} union end date"
            source_qid = qid_of(row, "source")
            source = (
                f'<span class="source"><a href="../artifacts/'
                f'{esc(source_qid)}.html">{esc(cell(row, "sourceLabel"))}'
                "</a></span>"
                if cell(row, "sourceLabel") and source_qid.startswith("Q")
                else ""
            )
            shown.append(
                esc(
                    format_time(
                        cell(row, "date"), cell(row, "precision"), where
                    )
                )
                + qualifier_span(self.timeline_qualifiers(row))
                + source
            )
        return "; its date " + "; ".join(shown) if shown else ""

    def union_html(self, qid: str, unions: list[UnionShown]) -> str:
        """A person page's "Unions": each union the person takes part in,
        with its other partners, and each end a source states, with its
        sources; where none is stated, "no source states how it ended",
        and the end the shared derivation gives from the partners' deaths
        (ADR-0040 §2.6), labelled as derived. "" for none."""
        if not unions:
            return ""
        items = []
        for union in unions:
            others = [
                self.person_link(other, label)
                for other, label in sorted(union.partners.items())
                if other != qid
            ]
            text = esc(union.label)
            if others:
                text += " with " + ", ".join(others)
            stated = []
            for key, end, label, sources in union.ends:
                links = self.artifact_links(sources)
                what = (
                    f"ended by the death of {self.person_link(end, label)}"
                    if key == "ended_by_death_of"
                    else f"ended by {esc(label)}"
                )
                stated.append(
                    what
                    + (f'<span class="source">{links}</span>' if links else "")
                    + self.end_dates_html(union, key, end, qid)
                )
            if stated:
                text += ": " + "; ".join(sorted(stated, key=visible))
            else:
                text += ": no source states how it ended"
                found = self._derived_end(union)
                names = union.partners
                if found is not None and found.by_death_of:
                    text += esc(
                        f"; ended by {names.get(found.by_death_of)}'s "
                        "death (derived)"
                    )
                elif found is not None:
                    text += esc(
                        "; "
                        + "; ".join(
                            f"ended no later than {names.get(p)}'s death "
                            "(derived)"
                            for p in found.no_later_than
                        )
                    )
            items.append(f"<li>{text}</li>")
        return "<h2>Unions</h2><ul>" + "".join(items) + "</ul>"

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
        what sources state of what it is, its own statements and other
        sources' (a container that places it, an export whose record makes
        it) alike, each property's values with their sources, values that
        disagree listed together and none preferred (CLAUDE.md rule 6,
        ADR-0039 §10.2); "" when no source says anything of the
        artifact."""
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
            "with the page it stands on, what the artifact states of "
            "itself, and what other sources state of it, each with its "
            "page. Values that disagree are listed together; none is "
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
        qualifiers and its sources, each labelled by its source and page
        as on the artifact's page, the artifact's own statements as the
        artifact itself; "" for none. An artifact links to its page where
        it has one; a source's mention of an artifact it only writes about
        (an enclosure, ADR-0039 §10.1(c)) has none, and is named by its
        label alone."""
        if not naming:
            return ""
        entries = reading_order(
            (
                f"{esc(NAMED_AS.get(each.key, each.property_label))} "
                + (
                    self.artifact_links(
                        [(each.artifact, each.artifact_label or each.artifact)]
                    )
                    if each.artifact in self.artifact_qids
                    else esc(each.artifact_label or each.artifact)
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
            "shows, each labelled by the source that says it, with its "
            "page: the artifact itself, a container that places it, or "
            "another source that writes of it</p><ul>"
            + "".join(f"<li>{entry}</li>" for entry in entries)
            + "</ul>"
        )

    def person_page(
        self,
        person: dict,
        statements: list[dict],
        naming: list[Said] | None = None,
        undated: list[tuple[tuple[str, str], ...]] | None = None,
        unions: list[UnionShown] | None = None,
    ) -> str:
        """A person's page: every statement about them, each value with
        its sources, under its property, a date of death of unknown value
        as "died (date not stated)"; under the date of death too, each
        "died by" date the shared derivation gives (``derived_html``) and
        each death event a curator makes of theirs that states no date
        (``undated``, its sources as ``undated_deaths`` gives them); then
        the unions they take part in (``union_html``) and the artifacts
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
        died = disagreement.property_labels()["date_of_death"]
        grouped[died] += self.derived_html(qid)
        for sources in undated or []:
            grouped[died].append(self.undated_death_html(sources))
        if not grouped[died]:
            del grouped[died]
        body.append('<dl class="statements">')
        for prop in sorted(grouped):
            body.append(f"<dt>{esc(prop)}</dt>")
            body += [f"<dd>{entry}</dd>" for entry in grouped[prop]]
        body.append("</dl>")
        body.append(self.union_html(qid, unions or []))
        body.append(self.named_in_html(naming or []))
        body.append(
            f'<p class="sub">'
            f'<a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
            f"curate {qid} in the wiki</a></p>"
        )
        return self._page(label, "".join(body), root="../")

    def unknown_text(self, row: dict) -> str:
        """How a value its source states exists but does not give shows,
        by the statement's property: a death "died (date not stated)", a
        burial "buried (place not stated)" (ADR-0040 §2.2, Q3)."""
        prop = qid_of(row, "prop")
        if prop and prop == self.properties.get("date_of_death"):
            return UNDATED_DEATH
        if prop and prop == self.properties.get("burial_place"):
            return UNPLACED_BURIAL
        return UNSTATED_VALUE

    def render_value(self, row: dict) -> str:
        value = row.get("value", {})
        raw = value.get("value", "")
        if unknown_value(row):
            rendered = esc(self.unknown_text(row))
        elif value.get("type") == "uri" and "/entity/Q" in raw:
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
                if unknown_value(row):
                    # never its blank node: what its source states
                    shown = esc(
                        self.unknown_text(
                            {
                                "prop": {
                                    "value": self.prop_of_label(
                                        cell(row, "propLabel")
                                    )
                                }
                            }
                        )
                    )
                elif value.get("datatype", "").endswith("dateTime"):
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

    def prop_of_label(self, label: str) -> str:
        """The instance's id of the manifest property labelled `label`,
        or "" for none."""
        labels = disagreement.property_labels()
        return next(
            (
                self.properties[key]
                for key, shown in labels.items()
                if shown == label and key in self.properties
            ),
            "",
        )

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
        unknown: dict[str, list[tuple[tuple[str, str], ...]]] | None = None,
        unions: list[UnionShown] | None = None,
    ) -> str:
        """The timeline: every dated row of the person-timeline query, under
        its year, each with its source, and among a person's rows, at its date,
        each "died by" date the shared derivation gives them, labelled as
        derived (ADR-0040 §2.4). Each date of death of unknown value
        (``unknown``, by the QID of the one who died, its sources as
        ``unknown_deaths`` gives them) and each union's end by a partner's
        death, stated or derived (``unions``, ``union_ends``; §2.6), shows
        beside that person's first death entry: a dated date of death, or the
        point in time of a curator's death event they are the principal of
        (§6). Each union's end by an event (a divorce, ``other_ends``)
        stands among each partner's rows at the event's date, with its
        sources. Then, under "No date stated", each of those that has no
        such entry to stand beside or date, and each death event a curator
        makes that states no date (``undated``, as ``undated_deaths`` gives
        them), by the name of the one it is about."""
        beside: dict[str, list[str]] = defaultdict(list)
        for qid, deaths in (unknown or {}).items():
            for sources in deaths:
                beside[qid].append(
                    f'<a href="people/{esc(qid)}.html">'
                    f"{esc(self._name(qid))}"
                    "</a>, " + self.undated_death_html(sources, root="")
                )
        undated_ends: list[str] = []
        for union in unions or []:
            for qid, entry in self.union_ends(union):
                beside[qid].append(entry)
            for qid, row in self.other_ends(union):
                if "_dated" not in row:
                    undated_ends.append(
                        f'<a href="people/{esc(qid)}.html">'
                        f"{esc(self._name(qid))}</a>: {row['_what']}"
                    )
        died = disagreement.property_labels()["date_of_death"]
        death_type = load_manifest().items["death"].label
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
            "date its source gives as circa says so there too. A date "
            "marked derived is no source's: a view works it out from the "
            "statements it names.</p>",
            '<div class="timeline">',
        ]
        current_year = None

        def is_death(row: dict) -> bool:
            # a derived date is no source's, so nothing stands beside it;
            # nor anything beside a union's end
            if "_html" in row or "_what" in row:
                return False
            if cell(row, "what") == died:
                return True
            types = {
                urllib.parse.unquote(each)
                for each in cell(row, "eventTypes").split("|")
                if each
            }
            return death_type in types and not cell(row, "what")

        for row in self._with_derived(rows, unions or []):
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
            shown_date = esc(format_time(date, cell(row, "precision"), where))
            link = (
                f'<a href="people/{esc(person)}.html">'
                f"{esc(cell(row, 'personLabel'))}</a>, "
            )
            if "_html" in row:
                body.append(
                    f'<div class="entry">{shown_date} — {link}'
                    f"{row['_html']}</div>"
                )
            else:
                source_qid = qid_of(row, "source")
                source_label = cell(row, "sourceLabel")
                source = (
                    f'<span class="source">'
                    f'<a href="artifacts/{esc(source_qid)}.html">'
                    f"{esc(source_label)}</a></span>"
                    if source_label and source_qid.startswith("Q")
                    else ""
                )
                what = esc(self.timeline_what(row))
                if "_what" in row:
                    # a union's end, with its own sources, then its date's
                    what = row["_what"] + ("; its date" if source else "")
                body.append(
                    f'<div class="entry">{shown_date}'
                    f"{qualifier_span(self.timeline_qualifiers(row))} — "
                    f"{link}{what}" + source + "</div>"
                )
            if is_death(row):
                # a death its source states without its date, and a union
                # ended by the death, beside a dated death of the same
                # person (ADR-0040 §6)
                body += [
                    f'<div class="entry">{entry}</div>'
                    for entry in beside.pop(person, [])
                ]
        body.append("</div>")
        entries = reading_order(
            [
                *(
                    (
                        f'<a href="people/{esc(qid)}.html">'
                        f"{esc(self._name(qid))}"
                        "</a>, ",
                        self.undated_death_html(sources, root=""),
                    )
                    for qid, deaths in (undated or {}).items()
                    for sources in deaths
                ),
                *(
                    (entry, "")
                    for qid in sorted(beside, key=natural_order)
                    for entry in beside[qid]
                ),
                *((entry, "") for entry in undated_ends),
            ]
        )
        if entries:
            body.append(
                '<h2>No date stated</h2><p class="sub">each death a source '
                "states with no date, and each union's end by an event that "
                "states none, so that it has no place above, with its "
                'source</p><div class="timeline">'
                + "".join(
                    f'<div class="entry">{entry}</div>' for entry in entries
                )
                + "</div>"
            )
        return self._page("Timeline", "".join(body))

    def _name(self, qid: str) -> str:
        """A listed person's label, or their QID."""
        return cell(self.people.get(qid, {}), "personLabel") or qid

    def _with_derived(
        self, rows: list[dict], unions: list[UnionShown] = ()
    ) -> list[dict]:
        """The person-timeline rows, ordered by person and then by date,
        with a row (its HTML under ``_html``) among its person's rows at
        its date, after any row of the same date, for each derived "died
        by" date and each dated end of a union by an event (``other_ends``);
        a person the rows do not name has theirs at the end."""
        pending: dict[str, list[dict]] = defaultdict(list)

        def add(qid: str, stamp: str, precision: str, shown: str) -> None:
            pending[qid].append(
                {
                    "person": {
                        "type": "uri",
                        "value": f"{self.config.concept_uri}/entity/{qid}",
                    },
                    "personLabel": {
                        "type": "literal",
                        "value": self._name(qid),
                    },
                    "date": {"type": "literal", "value": stamp},
                    "precision": {"type": "literal", "value": precision},
                    "_html": shown,
                }
            )

        for bound in self._derived:
            qid = bound.person.rsplit("/", 1)[-1]
            if qid in self.people:
                add(
                    qid,
                    *when_time(bound.by),
                    self.died_by_html(bound, root=""),
                )
        for union in unions:
            for qid, row in self.other_ends(union):
                if "_dated" in row:
                    pending[qid].append(row)
        for found in pending.values():
            found.sort(key=lambda row: _day(cell(row, "date")))

        merged: list[dict] = []
        previous = None

        def flush(qid, before=None):
            waiting = pending.get(qid, [])
            while waiting and (
                before is None or _day(cell(waiting[0], "date")) < before
            ):
                merged.append(waiting.pop(0))

        for row in rows:
            person = qid_of(row, "person")
            if previous is not None and person != previous:
                flush(previous)
            flush(person, _day(cell(row, "date")))
            merged.append(row)
            previous = person
        if previous is not None:
            flush(previous)
        for qid in sorted(pending, key=natural_order):
            flush(qid)
        return merged

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
                            f"{cell(row, 'personLabel')}: "
                            f"{self.timeline_what(row)}"
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
                if value.derived:
                    # a "died by" date the shared derivation gives, never a
                    # value a source states (ADR-0040 §2.4)
                    return esc(f"died by {shown} (derived)")
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
