"""Static publish tier (frontend ladder rung 2): live SPARQL -> site.

`yizkr publish-site` renders the graph into a self-contained static
site: person pages showing EVERY attributed value side by side with
its source label (R6/ADR-0002 — never silently pick one), a timeline
that shows beside each date the range its source states, a photo
gallery (ADR-0008 derived media included), the disagreement report, and
a places index that becomes a map once places carry coordinates.

Design constraints:

* Generates FROM THE LIVE ENDPOINT (curator decision): the site
  includes possibly-living people, so it is for LAN/private serving
  only. An internet-facing build must come from the
  possibly-living-excluded public export instead (later rung). The
  private site still marks who that export would drop, via a badge
  computed at render time from the absence of death evidence — the same
  runtime determination the export uses (ADR-0020, `core/privacy.py`).
* Self-contained output: no CDN scripts, no external fonts, no
  third-party tiles — nothing about the family leaves the LAN when a
  page is opened (privacy-first). Media are hot-linked from the wiki
  on the same host.
* Pure functions over query results: the SPARQL runner is injectable,
  so rendering is unit-testable offline.
"""

from __future__ import annotations

import datetime
import html
import json
import shutil
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property
from pathlib import Path

from yizkr import disagreement
from yizkr.claims import century_of
from yizkr.core import sparql
from yizkr.core.config import Config
from yizkr.core.privacy import (
    BIRTHS_QUERY,
    DEFAULT_CUTOFF_YEARS,
    is_possibly_living,
    latest_births,
    people_selection,
)
from yizkr.core.wiki import WikiClient
from yizkr.disagreement import Disagreement, Side
from yizkr.queries.publish import load_templates, prefix_block, render

# -- SPARQL ----------------------------------------------------------------


def cell(binding: dict, name: str) -> str:
    return binding.get(name, {}).get("value", "")


def qid_of(binding: dict, name: str) -> str:
    return cell(binding, name).rsplit("/", 1)[-1]


# people roster with labels/descriptions/aliases and a RUNTIME
# possibly-living signal (ADR-0020). The people are the items the public
# export's check assesses: ``{people}`` is the privacy module's selection,
# every item with person evidence at any rank, so a person whose `human`
# type a curator deprecated or outranked, or who has no type but other
# evidence, is listed and badged too. ?deceased is true when the graph
# holds any death-implying statement (date/place/cause of death, place of
# burial) or a death event with the person as participant — none of which
# needs a date. Births, for the birth-recency cutoff, are read statement
# by statement with the query the public export reads them with
# (core/privacy.py). No stored "living" flag is read; the badge is
# derived, like the public export.
PEOPLE_QUERY = """\
SELECT ?person ?personLabel ?personDescription ?deceased
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
{people}
  OPTIONAL {{ ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  BIND((
        EXISTS {{ ?person wdt:{date_of_death} [] }}
     || EXISTS {{ ?person wdt:{place_of_death} [] }}
     || EXISTS {{ ?person wdt:{cause_of_death} [] }}
     || EXISTS {{ ?person wdt:{burial_place} [] }}
     || EXISTS {{ ?deathEvt wdt:{participant} ?person ;
                            wdt:{instance_of} wd:{death} }}
       ) AS ?deceased)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?person ?personLabel ?personDescription ?deceased
ORDER BY ?personLabel
"""

# every statement about every person the roster lists (``{people}``, as
# above): value, time precision, sources, qualifiers — the raw material
# of the plural-by-default person page
STATEMENTS_QUERY = """\
SELECT ?person ?propLabel ?st ?value ?valueLabel ?precision
       (GROUP_CONCAT(DISTINCT ?sourceLabel; separator="|") AS ?sources)
       (GROUP_CONCAT(DISTINCT ?qualifierText; separator="|")
        AS ?qualifierTexts)
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
    ?st ?pq ?qualifierValue .
    ?qualifierProp wikibase:qualifier ?pq ;
                   rdfs:label ?qualifierLabel .
    FILTER(LANG(?qualifierLabel) = "en")
    OPTIONAL {{
      ?qualifierValue rdfs:label ?qualifierValueLabel .
      FILTER(LANG(?qualifierValueLabel) = "en")
    }}
    # time qualifiers display at the SOURCE's precision — never padded
    OPTIONAL {{
      ?qualifierProp wikibase:qualifierValue ?pqv .
      ?st ?pqv ?qualifierNode .
      ?qualifierNode wikibase:timeValue ?qualifierValue ;
                     wikibase:timePrecision ?qualifierPrecision .
    }}
    BIND(
      CONCAT(?qualifierLabel, ": ",
             COALESCE(
               ?qualifierValueLabel,
               IF(?qualifierPrecision >= 11,
                  SUBSTR(STR(?qualifierValue), 1, 10),
               IF(?qualifierPrecision = 10,
                  SUBSTR(STR(?qualifierValue), 1, 7),
                  SUBSTR(STR(?qualifierValue), 1, 4))),
               STR(?qualifierValue)))
      AS ?qualifierText)
  }}
}}
GROUP BY ?person ?propLabel ?st ?value ?valueLabel ?precision
ORDER BY ?person ?propLabel
"""

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

# what each artifact asserts — the artifact page's evidence table
ASSERTIONS_QUERY = """\
SELECT ?artifact ?subject ?subjectLabel ?propLabel ?value ?valueLabel
       ?precision
WHERE {{
  ?st prov:wasDerivedFrom/pr:{stated_in} ?artifact .
  ?subject ?p ?st .
  ?prop wikibase:claim ?p ;
        wikibase:statementProperty ?ps ;
        rdfs:label ?propLabel .
  FILTER(LANG(?propLabel) = "en")
  ?st ?ps ?value .
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
}}
ORDER BY ?artifact ?subjectLabel ?propLabel
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


def page(title: str, body: str, root: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<link rel="stylesheet" href="{root}style.css"></head><body>'
        + NAV.format(root=root)
        + body
        + "<footer>Generated by <code>yizkr publish-site</code> from the "
        "live graph — includes possibly-living people; for private "
        "use. Every value is shown with the source that asserts it; "
        "disagreement is a healthy state, not an error."
        "</footer></body></html>"
    )


def format_time(value: str, precision: str) -> str:
    """Wikibase dateTime + precision -> what the source actually said. A
    century is the one Wikibase counts for the stored year (century_of):
    at century precision, 1801 to 1900 are each the 19th century. The
    query service gives a century's year as stored, so a century before
    the common era counts back the same way: -1800 is the 18th."""
    date = value[1:] if value.startswith(("+", "-")) else value
    date = date.split("T")[0]
    year, _, rest = date.partition("-")
    month = rest.split("-")[0] if rest else ""
    sign = "−" if value.startswith("-") else ""
    p = int(precision) if precision else 11
    if p <= 7:
        return f"{sign}{century_of(int(year))}th century"
    if p == 8:
        return f"{sign}{int(year) // 10 * 10}s"
    if p == 9:
        return f"{sign}{int(year)}"
    if p == 10:
        return f"{sign}{int(year)}-{month}"
    return f"{sign}{date}"


def range_text(bounds: Iterable[tuple[str, str, str]]) -> str:
    """The range a source states for a date, as text to show beside it.
    Each bound is (the label of the qualifier that states it, its
    xsd:dateTime, its precision) and shows at its own precision, in the
    `label: value` form the person page uses for qualifiers: "earliest
    date: 1938; latest date: 1939". The caller passes only the bounds the
    source gives, so a range open at one end names the one bound it has
    ("earliest date: 1945"), never a closed range or a bare date."""
    return "; ".join(
        f"{label}: {format_time(value, precision)}"
        for label, value, precision in bounds
    )


def qualifier_span(text: str) -> str:
    """`text` beside a value, escaped, in the muted style of the person
    page's qualifiers; nothing when there is no text."""
    if not text:
        return ""
    return f' <span class="qualifiers">({esc(text)})</span>'


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

    # -- helpers ------------------------------------------------------------

    @cached_property
    def _latest_births(self) -> dict[str, list[int | None]]:
        """The latest year each birth statement allows, by person, read
        with the query and the reading the public export uses."""
        return latest_births(self.run(self._instance_query(BIRTHS_QUERY)))

    def _possibly_living(self, person: dict) -> bool:
        """Runtime privacy determination (ADR-0020): possibly living
        unless the graph holds death evidence, or every birth it records
        lies beyond the cutoff. The same call the public export makes —
        here it only decides a display badge on the (private) site."""
        births = self._latest_births.get(qid_of(person, "person"), [])
        has_death = cell(person, "deceased") == "true"
        return is_possibly_living(births, has_death, self._cutoff_year)[0]

    def _instance_query(self, template: str) -> str:
        """`template` with this instance's ids, its ``{people}`` (where it
        has one) the privacy module's selection of people."""
        vocabulary = {**self.items, **self.properties}
        return prefix_block(self.config.concept_uri) + template.format(
            people=people_selection(self.properties, self.items),
            **vocabulary,
        )

    def _library_query(self, name: str) -> str:
        return prefix_block(self.config.concept_uri) + render(
            self.templates[name], self.properties, self.items
        )

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
        return range_text(bounds)

    # -- sections -----------------------------------------------------------

    def build(self) -> dict:
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

        by_person: dict[str, list[dict]] = defaultdict(list)
        for row in statements:
            by_person[qid_of(row, "person")].append(row)
        by_artifact: dict[str, list[dict]] = defaultdict(list)
        for row in assertions:
            by_artifact[qid_of(row, "artifact")].append(row)

        for row in people:
            qid = qid_of(row, "person")
            self._write(
                f"people/{qid}.html",
                self.person_page(row, by_person.get(qid, [])),
            )
        for row in artifacts:
            qid = qid_of(row, "artifact")
            self._write(
                f"artifacts/{qid}.html",
                self.artifact_page(row, by_artifact.get(qid, [])),
            )
        self._write("index.html", self.index_page(people, artifacts))
        self._write("timeline.html", self.timeline_page(timeline))
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
            "timeline_rows": len(timeline),
            "gallery_items": len(gallery),
            "inconsistencies": len(inconsistencies),
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

    def person_page(self, person: dict, statements: list[dict]) -> str:
        qid = qid_of(person, "person")
        label = cell(person, "personLabel") or qid
        aliases = [a for a in cell(person, "aliases").split("|") if a]
        body = [f"<h1>{esc(label)}"]
        if self._possibly_living(person):
            body.append(
                ' <span class="badge">possibly living — never in '
                "public exports</span>"
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

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in statements:
            prop = cell(row, "propLabel")
            # "instance of" is the subtitle. No "living person" flag is
            # written (ADR-0020); skip any such statement in the graph so
            # it doesn't render as data.
            if prop in ("instance of", "living person"):
                continue
            grouped[prop].append(row)
        body.append('<dl class="statements">')
        for prop in sorted(grouped):
            body.append(f"<dt>{esc(prop)}</dt>")
            for row in grouped[prop]:
                body.append(f"<dd>{self.render_value(row)}</dd>")
        body.append("</dl>")
        body.append(
            f'<p class="sub">'
            f'<a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
            f"curate {qid} in the wiki</a></p>"
        )
        return page(label, "".join(body), root="../")

    def render_value(self, row: dict) -> str:
        value = row.get("value", {})
        raw = value.get("value", "")
        if value.get("type") == "uri" and "/entity/Q" in raw:
            target = raw.rsplit("/", 1)[-1]
            text = cell(row, "valueLabel") or target
            rendered = f'<a href="{esc(target)}.html">{esc(text)}</a>'
        elif value.get("datatype", "").endswith("dateTime"):
            rendered = esc(format_time(raw, cell(row, "precision")))
        else:
            rendered = esc(cell(row, "valueLabel") or raw)
        qualifiers = [q for q in cell(row, "qualifierTexts").split("|") if q]
        if qualifiers:
            rendered += (
                f' <span class="qualifiers">'
                f"({esc('; '.join(qualifiers))})</span>"
            )
        sources = self.source_links(cell(row, "sources"))
        if sources:
            rendered += f'<span class="source">{sources}</span>'
        return rendered

    def artifact_page(self, artifact: dict, assertions: list[dict]) -> str:
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
        if assertions:
            body.append(
                f"<h2>What this artifact asserts ({len(assertions)})</h2>"
            )
            body.append(
                "<table><tr><th>About</th><th>Property</th><th>Value</th></tr>"
            )
            for row in assertions:
                subject_qid = qid_of(row, "subject")
                subject = esc(cell(row, "subjectLabel") or subject_qid)
                if subject_qid.startswith("Q"):
                    subject = (
                        f'<a href="../people/{subject_qid}.html">{subject}</a>'
                    )
                value = row.get("value", {})
                raw = value.get("value", "")
                if value.get("datatype", "").endswith("dateTime"):
                    shown = esc(format_time(raw, cell(row, "precision")))
                else:
                    shown = esc(
                        cell(row, "valueLabel")
                        or (
                            raw.rsplit("/", 1)[-1]
                            if value.get("type") == "uri"
                            else raw
                        )
                    )
                body.append(
                    f"<tr><td>{subject}</td>"
                    f"<td>{esc(cell(row, 'propLabel'))}</td>"
                    f"<td>{shown}</td></tr>"
                )
            body.append("</table>")
        else:
            body.append("<p>No statements reference this artifact yet.</p>")
        body.append(
            f'<p class="sub">'
            f'<a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
            f"curate {qid} in the wiki</a></p>"
        )
        return page(label, "".join(body), root="../")

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
        return page("Family archive", "".join(body))

    def timeline_page(self, rows: list[dict]) -> str:
        body = [
            "<h1>Timeline</h1>",
            '<p class="sub">every dated assertion, labeled by its source — '
            "the same fact from two sources appears twice, on purpose. "
            "Beside a date is the range its source states for it, if any: "
            "its earliest and latest date, or for a start or end time the "
            "bounds of that end, a start running from its earliest date to "
            "its latest start date and an end from its earliest end date to "
            "its latest date.</p>",
            '<div class="timeline">',
        ]
        current_year = None
        for row in rows:
            date = cell(row, "date")
            year = date[:5].lstrip("+") if date else "?"
            year = year.rstrip("-")
            if year != current_year:
                current_year = year
                body.append(f'<div class="year">{esc(year)}</div>')
            person = qid_of(row, "person")
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
                f"{esc(format_time(date, cell(row, 'precision')))}"
                f"{qualifier_span(self.timeline_range(row))} — "
                f'<a href="people/{esc(person)}.html">'
                f"{esc(cell(row, 'personLabel'))}</a>, "
                f"{esc(cell(row, 'what'))}" + source + "</div>"
            )
        body.append("</div>")
        return page("Timeline", "".join(body))

    def timeline_feed(self, rows: list[dict]) -> str:
        """TimelineJS-compatible JSON, if the user later wants that UI.
        TimelineJS shows an event's headline and text, and a date's
        display_date, as HTML, so each is escaped. A date its source gives
        a range for carries a display_date, the date with its range as the
        timeline page shows them; TimelineJS shows any other date from its
        year, and from its month and day when its precision reaches
        them."""
        events = []
        for row in rows:
            date = cell(row, "date")
            if not date:
                continue
            clean = date.lstrip("+").split("T")[0]
            parts = clean.split("-")
            precision = int(cell(row, "precision") or 11)
            start = {"year": parts[0]}
            if precision >= 10:
                start["month"] = parts[1]
            if precision >= 11:
                start["day"] = parts[2]
            stated = self.timeline_range(row)
            if stated:
                shown = format_time(date, cell(row, "precision"))
                start["display_date"] = esc(f"{shown} ({stated})")
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
        return page("Gallery", "".join(body))

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
        return page("Places", "".join(body))

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
            "curator deprecated is left out. Differences of resolution are "
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
                    (labels[key], time.value, str(time.precision))
                    for key, time in (
                        (low, side.value.earliest),
                        (high, side.value.latest),
                    )
                    if time is not None
                )
            )

        def rendered(row: Disagreement, side: Side) -> str:
            value = side.value
            if value.term.is_time:
                precision = "" if value.precision is None else value.precision
                shown = format_time(value.term.value, str(precision))
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
        return page("Inconsistencies", "".join(body))


def publish_site(config: Config, out_dir: Path, clean: bool = False) -> dict:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    return SiteBuilder(config, out_dir).build()
