"""Static publish tier (frontend ladder rung 2): live SPARQL -> site.

`yizkr publish-site` renders the graph into a self-contained static
site: person pages showing EVERY attributed value side by side with
its source label (R6/ADR-0002 — never silently pick one), a timeline,
a photo gallery (ADR-0008 derived media included), the disagreement
report, and a places index that becomes a map once places carry
coordinates.

Design constraints:

* Generates FROM THE LIVE ENDPOINT (curator decision): the site
  includes living people, so it is for LAN/private serving only. An
  internet-facing build must come from the living-persons-excluded
  public export instead (later rung).
* Self-contained output: no CDN scripts, no external fonts, no
  third-party tiles — nothing about the family leaves the LAN when a
  page is opened (privacy-first). Media are hot-linked from the wiki
  on the same host.
* Pure functions over query results: the SPARQL runner is injectable,
  so rendering is unit-testable offline.
"""

from __future__ import annotations

import html
import json
import shutil
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient
from yizkr.queries.publish import load_templates, prefix_block, render

# -- SPARQL ----------------------------------------------------------------


def sparql_runner(config: Config):
    def run(query: str) -> list[dict]:
        url = (
            config.sparql_url
            + "?"
            + urllib.parse.urlencode({"query": query, "format": "json"})
        )
        with urllib.request.urlopen(url, timeout=120) as response:
            return json.load(response)["results"]["bindings"]

    return run


def cell(binding: dict, name: str) -> str:
    return binding.get(name, {}).get("value", "")


def qid_of(binding: dict, name: str) -> str:
    return cell(binding, name).rsplit("/", 1)[-1]


# people roster with labels/descriptions/aliases
PEOPLE_QUERY = """\
SELECT ?person ?personLabel ?personDescription ?living
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
WHERE {{
  ?person wdt:{instance_of} wd:{human} .
  OPTIONAL {{ ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?person wdt:{living_person} ?flag .
              ?flag rdfs:label ?living . FILTER(LANG(?living) = "en") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?person ?personLabel ?personDescription ?living
ORDER BY ?personLabel
"""

# every statement about every human: value, time precision, sources,
# qualifiers — the raw material of the plural-by-default person page
STATEMENTS_QUERY = """\
SELECT ?person ?propLabel ?st ?value ?valueLabel ?precision
       (GROUP_CONCAT(DISTINCT ?sourceLabel; separator="|") AS ?sources)
       (GROUP_CONCAT(DISTINCT ?qualifierText; separator="|") AS ?qualifierTexts)
WHERE {{
  ?person wdt:{instance_of} wd:{human} .
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
    ?source rdfs:label ?sourceLabel . FILTER(LANG(?sourceLabel) = "en")
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
SELECT ?artifact ?artifactLabel ?kindLabel ?file
WHERE {{
  ?artifact wdt:{instance_of} ?kind .
  ?kind wdt:{subclass_of}* wd:{artifact} .
  OPTIONAL {{ ?artifact wdt:{media_file} ?file . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
ORDER BY ?artifactLabel
"""

CSS = """\
:root { --ink: #222; --muted: #667; --accent: #7a4a20; --line: #ddd; --bg: #faf8f4; }
* { box-sizing: border-box; }
body { font: 16px/1.55 Georgia, 'Times New Roman', serif; color: var(--ink);
       background: var(--bg); margin: 0 auto; max-width: 60rem; padding: 1rem 1.2rem 4rem; }
a { color: var(--accent); }
nav { font-family: system-ui, sans-serif; font-size: .85rem; border-bottom: 1px solid var(--line);
      padding-bottom: .6rem; margin-bottom: 1.2rem; }
nav a { margin-right: 1rem; text-decoration: none; }
h1 { font-weight: normal; margin: .2rem 0 .1rem; }
.sub { color: var(--muted); margin: 0 0 1rem; }
.badge { font-family: system-ui, sans-serif; font-size: .7rem; background: #a33; color: #fff;
         border-radius: .6rem; padding: .1rem .55rem; vertical-align: middle; }
dl.statements dt { font-family: system-ui, sans-serif; font-size: .8rem; text-transform: uppercase;
                   letter-spacing: .05em; color: var(--muted); margin-top: 1rem; }
dl.statements dd { margin: .25rem 0 .25rem 1rem; }
.source { color: var(--muted); font-size: .85em; }
.source::before { content: " — per "; }
.qualifiers { color: var(--muted); font-size: .85em; font-style: italic; }
ul.roster { columns: 2; list-style: none; padding: 0; }
ul.roster li { margin: .15rem 0; break-inside: avoid; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; }
.gallery figure { margin: 0; }
.gallery img { width: 100%; height: 200px; object-fit: contain; background: #fff;
               border: 1px solid var(--line); }
.gallery figcaption { font-size: .8rem; color: var(--muted); }
.timeline .year { font-family: system-ui, sans-serif; font-weight: 600; margin-top: 1.2rem;
                  border-bottom: 1px solid var(--line); }
.timeline .entry { margin: .3rem 0 .3rem 1rem; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
footer { margin-top: 3rem; color: var(--muted); font-size: .8rem;
         border-top: 1px solid var(--line); padding-top: .6rem; }
"""

NAV = (
    '<nav><a href="{root}index.html">People</a>'
    '<a href="{root}timeline.html">Timeline</a>'
    '<a href="{root}gallery.html">Gallery</a>'
    '<a href="{root}places.html">Places</a>'
    '<a href="{root}disagreements.html">Disagreements</a></nav>'
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
        + "<footer>Generated by <code>yizkr publish-site</code> from the live"
        " graph — includes living people; for private use. Every value is"
        " shown with the source that asserts it; disagreement is a healthy"
        " state, not an error.</footer></body></html>"
    )


def format_time(value: str, precision: str) -> str:
    """Wikibase dateTime + precision -> what the source actually said."""
    date = value[1:] if value.startswith(("+", "-")) else value
    date = date.split("T")[0]
    year, _, rest = date.partition("-")
    month = rest.split("-")[0] if rest else ""
    sign = "−" if value.startswith("-") else ""
    p = int(precision) if precision else 11
    if p <= 7:
        return f"{sign}{int(year) // 100 + 1}th century"
    if p == 8:
        return f"{sign}{int(year) // 10 * 10}s"
    if p == 9:
        return f"{sign}{int(year)}"
    if p == 10:
        return f"{sign}{int(year)}-{month}"
    return f"{sign}{date}"


class SiteBuilder:
    def __init__(self, config: Config, out_dir: Path, run=None, vocabulary=None):
        self.config = config
        self.out = out_dir
        self.run = run or sparql_runner(config)
        if vocabulary is None:
            properties, items = WikiClient(config).vocabulary()
        else:
            properties, items = vocabulary
        self.properties = properties
        self.items = items
        self.templates = load_templates()

    # -- helpers ------------------------------------------------------------

    def _instance_query(self, template: str) -> str:
        vocabulary = {**self.items, **self.properties}
        return prefix_block(self.config.concept_uri) + template.format(**vocabulary)

    def _library_query(self, name: str) -> str:
        return prefix_block(self.config.concept_uri) + render(
            self.templates[name], self.properties, self.items
        )

    def _write(self, relative: str, text: str) -> None:
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # -- sections -----------------------------------------------------------

    def build(self) -> dict:
        self.out.mkdir(parents=True, exist_ok=True)
        self._write("style.css", CSS)
        people = self.run(self._instance_query(PEOPLE_QUERY))
        statements = self.run(self._instance_query(STATEMENTS_QUERY))
        timeline = self.run(self._library_query("person-timeline"))
        gallery = self.run(self._library_query("photo-gallery"))
        disagreements = self.run(self._library_query("disagreement-report"))
        places = self.run(self._library_query("places-map"))
        artifacts = self.run(self._instance_query(ARTIFACTS_QUERY))

        by_person: dict[str, list[dict]] = defaultdict(list)
        for row in statements:
            by_person[qid_of(row, "person")].append(row)

        for row in people:
            qid = qid_of(row, "person")
            self._write(
                f"people/{qid}.html", self.person_page(row, by_person.get(qid, []))
            )
        self._write("index.html", self.index_page(people, artifacts))
        self._write("timeline.html", self.timeline_page(timeline))
        self._write("timeline.json", self.timeline_feed(timeline))
        self._write("gallery.html", self.gallery_page(gallery))
        self._write("places.html", self.places_page(places))
        self._write("disagreements.html", self.disagreements_page(disagreements))
        return {
            "out": str(self.out),
            "people": len(people),
            "statements": len(statements),
            "timeline_rows": len(timeline),
            "gallery_items": len(gallery),
            "disagreements": len(disagreements),
        }

    def person_page(self, person: dict, statements: list[dict]) -> str:
        qid = qid_of(person, "person")
        label = cell(person, "personLabel") or qid
        aliases = [a for a in cell(person, "aliases").split("|") if a]
        living = cell(person, "living") == "yes"
        body = [f"<h1>{esc(label)}"]
        if living:
            body.append(' <span class="badge">living — never in public exports</span>')
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
            if prop in ("instance of", "living person"):
                continue  # rendered as badge/subtitle, not statements
            grouped[prop].append(row)
        body.append('<dl class="statements">')
        for prop in sorted(grouped):
            body.append(f"<dt>{esc(prop)}</dt>")
            for row in grouped[prop]:
                body.append(f"<dd>{self.render_value(row)}</dd>")
        body.append("</dl>")
        body.append(
            f'<p class="sub"><a href="{esc(self.config.wikibase_url)}/wiki/Item:{qid}">'
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
                f' <span class="qualifiers">({esc("; ".join(qualifiers))})</span>'
            )
        sources = [s for s in cell(row, "sources").split("|") if s]
        if sources:
            rendered += f'<span class="source">{esc("; ".join(sources))}</span>'
        return rendered

    def index_page(self, people: list[dict], artifacts: list[dict]) -> str:
        body = ["<h1>Family archive</h1>"]
        body.append(
            f'<p class="sub">{len(people)} people · {len(artifacts)} artifacts · '
            "every statement traceable to the source that asserts it</p>"
        )
        body.append('<h2>People</h2><ul class="roster">')
        for row in people:
            qid = qid_of(row, "person")
            label = esc(cell(row, "personLabel") or qid)
            badge = (
                ' <span class="badge">living</span>'
                if cell(row, "living") == "yes"
                else ""
            )
            body.append(f'<li><a href="people/{qid}.html">{label}</a>{badge}</li>')
        body.append(
            "</ul><h2>Artifacts</h2><table><tr><th>Artifact</th><th>Kind</th></tr>"
        )
        for row in artifacts:
            label = esc(cell(row, "artifactLabel") or qid_of(row, "artifact"))
            file_name = cell(row, "file")
            if file_name:
                href = (
                    f"{self.config.wikibase_url}/wiki/Special:FilePath/"
                    + urllib.parse.quote(file_name.removeprefix("File:"))
                )
                label = f'<a href="{esc(href)}">{label}</a>'
            body.append(
                f"<tr><td>{label}</td><td>{esc(cell(row, 'kindLabel'))}</td></tr>"
            )
        body.append("</table>")
        return page("Family archive", "".join(body))

    def timeline_page(self, rows: list[dict]) -> str:
        body = [
            "<h1>Timeline</h1>",
            '<p class="sub">every dated assertion, labeled by its source — '
            "the same fact from two sources appears twice, on purpose</p>",
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
            body.append(
                '<div class="entry">'
                f"{esc(format_time(date, cell(row, 'precision')))} — "
                f'<a href="people/{person}.html">{esc(cell(row, "personLabel"))}</a>, '
                f"{esc(cell(row, 'what'))}"
                + (
                    f'<span class="source">{esc(cell(row, "sourceLabel"))}</span>'
                    if cell(row, "sourceLabel")
                    else ""
                )
                + "</div>"
            )
        body.append("</div>")
        return page("Timeline", "".join(body))

    def timeline_feed(self, rows: list[dict]) -> str:
        """TimelineJS-compatible JSON, if the user later wants that UI."""
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
            events.append(
                {
                    "start_date": start,
                    "text": {
                        "headline": f"{cell(row, 'personLabel')}: {cell(row, 'what')}",
                        "text": f"per {cell(row, 'sourceLabel') or 'unattributed'}",
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
                f'<br><span class="source">{esc(container)}</span>' if container else ""
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

    def places_page(self, rows: list[dict]) -> str:
        body = ["<h1>Places</h1>"]
        with_coordinates = [row for row in rows if cell(row, "coords")]
        if not with_coordinates:
            body.append(
                '<p class="sub">no place carries a <em>coordinate location</em> '
                "statement yet — the map lights up once places are reconciled "
                "to coordinates (GeoNames pass, see docs/MODELING.md)</p>"
            )
        body.append(
            "<table><tr><th>Place</th><th>Coordinates</th><th>Connected to</th></tr>"
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

    def disagreements_page(self, rows: list[dict]) -> str:
        body = [
            "<h1>Disagreements</h1>",
            '<p class="sub">properties where sources disagree — the archive\'s '
            "flagship view (ADR-0002): coexistence is the healthy resting "
            "state, and this page exists so a reader can weigh the evidence</p>",
            "<table><tr><th>Who/what</th><th>Property</th>"
            "<th>Value A</th><th>per</th><th>Value B</th><th>per</th></tr>",
        ]

        def rendered(row: dict, side: str) -> str:
            value = row.get(f"value{side}", {})
            raw = value.get("value", "")
            if value.get("datatype", "").endswith("dateTime"):
                return esc(format_time(raw, cell(row, f"precision{side}")))
            label = cell(row, f"value{side}Label")
            if label.startswith("http"):
                label = raw.rsplit("/", 1)[-1]
            return esc(label or raw)

        for row in rows:
            body.append(
                f"<tr><td>{esc(cell(row, 'entityLabel'))}</td>"
                f"<td>{esc(cell(row, 'propertyLabel'))}</td>"
                f"<td>{rendered(row, 'A')}</td>"
                f'<td class="source" style="font-style:italic">{esc(cell(row, "sourceALabel"))}</td>'
                f"<td>{rendered(row, 'B')}</td>"
                f'<td class="source" style="font-style:italic">{esc(cell(row, "sourceBLabel"))}</td></tr>'
            )
        body.append("</table>")
        return page("Disagreements", "".join(body))


def publish_site(config: Config, out_dir: Path, clean: bool = False) -> dict:
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    return SiteBuilder(config, out_dir).build()
