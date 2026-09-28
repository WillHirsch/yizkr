"""Live static-site build (frontend ladder rung 2): the generator runs
real queries against the running stack, and the flagship plural-display
behavior holds on data this module seeds itself.

The fixture writes a slice of the example universe
(tests/fixtures/example_universe.py) through the normal write path,
registering the sources and then writing one claim batch: Gustav
Löbig's birth as two of its sources give it. The civil register's
extract and the naturalisation certificate disagree on the day, the
universe's deliberate contradiction (ADR-0002). They also give the place
at two resolutions: the extract registers a birth "in seiner Wohnung",
the home of a father "wohnhaft in Flörburg, Brunnenring 35", while the
naturalisation names only Floerburg. The extract's address is recorded
inside the town (Brunnenring 35, part of Brunnenring, part of Flörburg;
ADR-0009), so the two places differ in resolution, not in substance. A
photograph of the family gravestone gives the gallery an image.

Every label carries a marker, so the fixture never reuses an item that
another test or run wrote, and a re-run reuses and skips everything.
Pages that list the whole instance are read only for the fixture's own
entries, so other data on the instance neither breaks an assertion nor
appears in a failure report, and the generated site is deleted after the
module.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import struct
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ClaimBatch
from yizkr.disagreement import (
    CONTAINMENT,
    FETCH,
    Containment,
    compare,
    place_within,
    read_containment,
    read_statements,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance
from yizkr.staticsite import publish_site

from .conftest import run_sparql, wait_for_query_service

pytestmark = pytest.mark.live

UNIVERSE = u.by_key()
EXTRACT = "birth-extract-1938"
NATURALISATION = "naturalisation-1952"
PHOTO = "gravestone-photo-1956"
MARK = "(site test)"


def marked(key: str) -> str:
    """The universe's label (or title) for `key`, with this module's
    marker: a label no other test or run writes."""
    entry = UNIVERSE[key]
    return f"{getattr(entry, 'title', None) or entry.label} {MARK}"


PERSON = marked("gustav-loebig")
ADDRESS = marked("brunnenring-35")
STREET = marked("brunnenring")
TOWN = marked("floerburg")


def said(source: str, prop: str) -> u.Assertion:
    """What `source` says about Gustav's `prop` in the universe."""
    (assertion,) = [
        a for a in u.about("gustav-loebig", prop) if a.source == source
    ]
    return assertion


BIRTHS = {  # the two days, 7 January and 1 July 1919
    source: said(source, "date_of_birth")
    for source in (EXTRACT, NATURALISATION)
}
NATURALISED_PLACE = said(NATURALISATION, "place_of_birth")
# the extract's own words for the birth at home, and for the address
HOME = (
    "wohnhaft in Flörburg, Brunnenring 35, zeigte an, daß von seiner "
    "Ehefrau Hildegard Löbig geborene Laudler zu Flörburg in seiner Wohnung"
)
ADDRESS_IN_TOWN = "wohnhaft in Flörburg, Brunnenring 35"


RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    # fixed, so a re-run's batch has the same fingerprint and reuses its
    # extraction-run item
    "started_at": "2026-09-27T00:00:00+00:00",
}


def shown(date: u.Date) -> str:
    """A day-precision date as the site prints it."""
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"


def png(marker: str) -> bytes:
    """A valid 4x4 PNG whose text chunk makes its bytes, and so its
    artifact's content key (ADR-0010), unique to this module."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    pixels = b"".join(b"\x00" + b"\x8c\x6e\x4b" * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + marker.encode("ascii"))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def item(ref: dict) -> dict:
    return {"type": "item", "value": ref}


def time_value(date: u.Date) -> dict:
    parts = {"year": date.year, "month": date.month, "day": date.day}
    return {"type": "time", **{k: v for k, v in parts.items() if v}}


def birth_batch(sources: dict[str, str]) -> ClaimBatch:
    """The two sources' claims about the birth, and the extract's
    placing of the address in the town."""
    person = local("person", PERSON, "human")
    address = local("address", ADDRESS, "place")
    street = local("street", STREET, "place")
    town = local("town", TOWN, "place")

    def claim(subject, prop, value, source, excerpt):
        return {
            "subject": subject,
            "property": prop,
            "value": value,
            "source": {
                "artifact": {"kind": "qid", "qid": sources[source]},
                "page_number": "1",
                "transcription_excerpt": {
                    "text": excerpt,
                    "language": UNIVERSE[source].lang,
                },
            },
        }

    extract_birth = BIRTHS[EXTRACT]
    naturalised_birth = BIRTHS[NATURALISATION]
    claims = [
        claim(
            person,
            "date_of_birth",
            time_value(extract_birth.value),
            EXTRACT,
            extract_birth.excerpt,
        ),
        claim(person, "place_of_birth", item(address), EXTRACT, HOME),
        claim(address, "part_of", item(street), EXTRACT, ADDRESS_IN_TOWN),
        claim(street, "part_of", item(town), EXTRACT, ADDRESS_IN_TOWN),
        claim(
            person,
            "date_of_birth",
            time_value(naturalised_birth.value),
            NATURALISATION,
            naturalised_birth.excerpt,
        ),
        claim(
            person,
            "place_of_birth",
            item(town),
            NATURALISATION,
            NATURALISED_PLACE.excerpt,
        ),
    ]
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


@dataclass(frozen=True)
class Site:
    out: Path
    summary: dict
    person: str  # the seeded person's QID
    places: dict[str, str]  # "address", "street", "town" -> QID
    sources: dict[str, str]  # universe source key -> artifact QID
    photo_file: str  # the gravestone photograph's file page

    def read(self, relative: str) -> str:
        return (self.out / relative).read_text()


@pytest.fixture(scope="module")
def site(client, config, tmp_path_factory):
    for text in (HOME, ADDRESS_IN_TOWN):
        assert text in UNIVERSE[EXTRACT].text  # quoted, never paraphrased
    media = tmp_path_factory.mktemp("media")
    registered = {}
    for key, kind in (
        (EXTRACT, "certificate"),
        (NATURALISATION, "certificate"),
        (PHOTO, "photograph"),
    ):
        path = media / f"site-test-{key}.png"
        path.write_bytes(png(f"yizkr live site test: {key}"))
        registered[key] = register_artifact(
            client,
            media_path=path,
            artifact_type=kind,
            title=marked(key),
            description="written by tests/integration; safe to delete",
        )
    sources = {key: r.artifact_qid for key, r in registered.items()}
    written = BatchWriter(client).write_batch(birth_batch(sources))
    seeded = [
        written.created_items[local_id]
        for local_id in ("person", "address", "street", "town")
    ]
    wait_for_query_service(config, client, [*seeded, *sources.values()])

    out = tmp_path_factory.mktemp("site")
    try:
        yield Site(
            out=out,
            summary=publish_site(config, out),
            person=written.created_items["person"],
            places={
                local_id: written.created_items[local_id]
                for local_id in ("address", "street", "town")
            },
            sources=sources,
            photo_file=registered[PHOTO].file_page,
        )
    finally:
        # the site renders the whole instance, not only this fixture
        shutil.rmtree(out, ignore_errors=True)


def elements(page: str, tag: str) -> list[str]:
    """The contents of every (unnested) `tag` element on a page."""
    return re.findall(rf"<{tag}>(.*?)</{tag}>", page, flags=re.S)


def source_link(site: Site, source: str, root: str) -> str:
    """How a page links a value to the artifact that asserts it."""
    return (
        f'<a href="{root}artifacts/{site.sources[source]}.html">'
        f"{html.escape(marked(source))}</a>"
    )


def test_summary_counts_are_plausible(site):
    # one person per page: a person whose sources disagree on a date is
    # still one person, not one per value
    summary = site.summary
    pages = list((site.out / "people").glob("*.html"))
    assert summary["people"] == len(pages)
    assert (site.out / f"people/{site.person}.html") in pages
    for qid in site.sources.values():
        assert (site.out / f"artifacts/{qid}.html").exists()
    # the counts cover the whole instance, so the fixture is a floor
    assert summary["artifacts"] >= 3
    assert summary["statements"] >= 5  # its kind, two births, two places
    assert summary["gallery_items"] >= 1
    assert summary["timeline_rows"] >= 2
    assert summary["inconsistencies"] >= 1


def test_person_pages_show_coexisting_values_with_sources(site):
    # the sources disagree on the day and give the place at two
    # resolutions: the page shows every value, each labelled with the
    # source that asserts it, and never silently picks one
    values = elements(site.read(f"people/{site.person}.html"), "dd")
    for value, source in (
        (shown(BIRTHS[EXTRACT].value), EXTRACT),
        (shown(BIRTHS[NATURALISATION].value), NATURALISATION),
        (html.escape(ADDRESS), EXTRACT),
        (html.escape(TOWN), NATURALISATION),
    ):
        (entry,) = [dd for dd in values if value in dd]
        assert source_link(site, source, root="../") in entry

    listed = f'href="people/{site.person}.html"' in site.read("index.html")
    assert listed, "the index does not link the seeded person"


def test_gallery_and_timeline_render(site):
    alt = f'alt="{html.escape(marked(PHOTO))}"'
    figures = elements(site.read("gallery.html"), "figure")
    (figure,) = [f for f in figures if alt in f]
    image = "/wiki/Special:FilePath/" + urllib.parse.quote(
        site.photo_file.removeprefix("File:")
    )
    assert '<img src="' in figure and image in figure

    # each dated assertion is its own entry, under its own source
    person = f'href="people/{site.person}.html"'
    timeline = re.findall(
        r'<div class="entry">(.*?)</div>', site.read("timeline.html")
    )
    dated = [entry for entry in timeline if person in entry]
    for source, birth in BIRTHS.items():
        (entry,) = [e for e in dated if e.startswith(shown(birth.value))]
        assert source_link(site, source, root="") in entry

    feed = json.loads(site.read("timeline.json"))
    starts = [
        event["start_date"]
        for event in feed["events"]
        if event["text"]["headline"].startswith(f"{PERSON}: ")
    ]
    for birth in BIRTHS.values():
        year, month, day = shown(birth.value).split("-")
        assert {"year": year, "month": month, "day": day} in starts


def test_sources_hyperlink_to_artifact_pages_with_downloads(site):
    person = site.read(f"people/{site.person}.html")
    for source, birth in BIRTHS.items():
        qid = site.sources[source]
        assert f'"../artifacts/{qid}.html"' in person  # labels are links
        # the artifact page offers the raw file and lists what the
        # artifact asserts, the person's birth among it
        artifact = site.read(f"artifacts/{qid}.html")
        assert "download the original" in artifact
        assert "What this artifact asserts" in artifact
        about_person = [
            row
            for row in elements(artifact, "tr")
            if f'href="../people/{site.person}.html"' in row
        ]
        assert any(shown(birth.value) in row for row in about_person)


def test_inconsistencies_suppress_recorded_containment(site, client, config):
    rows = [
        row
        for row in elements(site.read("inconsistencies.html"), "tr")
        if row.startswith(f"<td>{html.escape(PERSON)}</td>")
    ]
    # the two days are a genuine disagreement: it stays, with both
    # values linked to their sources
    days = [shown(birth.value) for birth in BIRTHS.values()]
    births = [row for row in rows if all(day in row for day in days)]
    assert births
    for source in BIRTHS:
        assert all(source_link(site, source, root="") in r for r in births)
    # Brunnenring 35 is recorded as part of Flörburg: a difference of
    # resolution, which the report leaves out
    assert not [row for row in rows if html.escape(ADDRESS) in row]

    # ... and only the recorded containment leaves the pair out: the
    # containment query returns the seeded path, and the report's
    # statements compared without it list the two birthplaces, so the
    # report does compare the places (#266)
    properties, items = client.vocabulary()
    templates = load_templates()

    def fetch(name: str) -> list[dict]:
        query = render_for_instance(
            templates[name], properties, items, config.concept_uri
        )
        return run_sparql(config, query)

    entity = f"{config.concept_uri}/entity/"
    address, town = (entity + site.places[key] for key in ("address", "town"))
    containment = read_containment(fetch(CONTAINMENT))
    assert place_within(containment, address, town), (
        "the containment query does not return the seeded path"
    )
    statements = read_statements(fetch(FETCH), properties)
    compared = {
        frozenset((row.a.value.term.value, row.b.value.term.value))
        for row in compare(statements, Containment())
        if row.entity == entity + site.person
    }
    places = frozenset((address, town))
    assert places in compared

    # and the places page shows the recorded containment
    chain = "".join(
        (
            f"<li>{html.escape(TOWN)}<ul>",
            f"<li>{html.escape(STREET)}<ul>",
            f"<li>{html.escape(ADDRESS)}</li>",
            "</ul></li></ul></li>",
        )
    )
    recorded = chain in site.read("places.html")
    assert recorded, "the places page does not show the seeded containment"
