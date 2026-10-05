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
photograph of the family gravestone gives the gallery an image, and
Rahel Löbig's death, which the family tree gives as "after 1945", gives
the timeline a date with a range its source states, open at one end
(#309).

Every label, title and image carries a marker, eight hex digits of a hash
over the fixture, over the writer's and the site generator's source and
over eight random bytes drawn once a session (conftest.fixture_mark), so
the fixture never reuses an item that another test or an earlier session, a
live mutant's run included, wrote. Rahel's label also carries a hash of her
claim (RAHEL). Pages that list the whole instance are read only for the
fixture's own entries, so other data on the instance neither breaks an
assertion nor appears in a failure report, and the generated site is
deleted after the module.
"""

from __future__ import annotations

import hashlib
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
from yizkr import staticsite
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.disagreement import (
    CONTAINMENT,
    FETCH,
    Containment,
    compare,
    place_within,
    read_containment,
    read_statements,
)
from yizkr.pipeline import writer
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance
from yizkr.staticsite import publish_site

from .conftest import fixture_mark, run_sparql, wait_for_query_service

pytestmark = pytest.mark.live

UNIVERSE = u.by_key()
EXTRACT = "birth-extract-1938"
NATURALISATION = "naturalisation-1952"
PHOTO = "gravestone-photo-1956"
TREE = "family-tree-1984"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "Gustav Löbig's birth as the 1938 extract and the 1952 naturalisation "
    "give it, the family gravestone's photograph, and Rahel's death after "
    "1945 as the 1984 family tree gives it"
)
MARK = f"(site test {fixture_mark(FIXTURE, writer, staticsite)})"


def marked(key: str) -> str:
    """The universe's label (or title) for `key`, with this module's
    marker: a label no other test or session writes."""
    entry = UNIVERSE[key]
    return f"{getattr(entry, 'title', None) or entry.label} {MARK}"


PERSON = marked("gustav-loebig")
ADDRESS = marked("brunnenring-35")
STREET = marked("brunnenring")
TOWN = marked("floerburg")


def said(
    source: str, prop: str, subject: str = "gustav-loebig"
) -> u.Assertion:
    """What `source` says about `subject`'s `prop` in the universe."""
    (assertion,) = [a for a in u.about(subject, prop) if a.source == source]
    return assertion


BIRTHS = {  # the two days, 7 January and 1 July 1919
    source: said(source, "date_of_birth")
    for source in (EXTRACT, NATURALISATION)
}
NATURALISED_PLACE = said(NATURALISATION, "place_of_birth")
RAHEL_DIED = said(TREE, "date_of_death", "rahel-loebig")  # after 1945
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


# Rahel's death as the tree gives it, "after 1945", written as the GEDCOM
# adapter writes it: 1945, with 1945 as its earliest date and no latest
# date.
RAHEL_DEATH = {
    "property": "date_of_death",
    "value": time_value(RAHEL_DIED.value),
    "qualifiers": [
        {"property": "earliest_date", "value": time_value(RAHEL_DIED.value)}
    ],
    "source": TREE,
    "excerpt": RAHEL_DIED.excerpt,
}
# Her label carries six hex digits of a hash over that claim, as
# write_fixture marks its items in test_live_disagreement.py. The writer
# finds her by her label, and keeps a claim with her value but other
# qualifiers as a statement of its own, so each version of the claim
# writes a Rahel of its own. The session's marker already does that
# across sessions; the hash keeps it so for the claim's own versions.
RAHEL = "{} ({})".format(
    marked("rahel-loebig"),
    hashlib.sha256(
        json.dumps(RAHEL_DEATH, sort_keys=True).encode()
    ).hexdigest()[:6],
)


def fixture_batch(sources: dict[str, ArtifactRef]) -> ClaimBatch:
    """The two sources' claims about the birth, the extract's placing of
    the address in the town, and the tree's claim about Rahel's death,
    each citing its source by the SHA-1 of its media (#102)."""
    person = local("person", PERSON, "human")
    address = local("address", ADDRESS, "place")
    street = local("street", STREET, "place")
    town = local("town", TOWN, "place")
    rahel = local("rahel", RAHEL, "human")

    def claim(subject, prop, value, source, excerpt, qualifiers=()):
        return {
            "subject": subject,
            "property": prop,
            "value": value,
            "qualifiers": list(qualifiers),
            "source": {
                "artifact": sources[source].model_dump(mode="json"),
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
        claim(
            rahel,
            RAHEL_DEATH["property"],
            RAHEL_DEATH["value"],
            RAHEL_DEATH["source"],
            RAHEL_DEATH["excerpt"],
            RAHEL_DEATH["qualifiers"],
        ),
    ]
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


@dataclass(frozen=True)
class Site:
    out: Path
    summary: dict
    person: str  # the seeded person's QID
    rahel: str  # Rahel's QID, whose death has a stated range
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
    registered, cited = {}, {}
    for key, kind in (
        (EXTRACT, "certificate"),
        (NATURALISATION, "certificate"),
        (PHOTO, "photograph"),
        (TREE, UNIVERSE[TREE].artifact_class),
    ):
        path = media / f"site-test-{key}.png"
        path.write_bytes(png(f"yizkr live site test: {key} {MARK}"))
        cited[key] = ArtifactRef.from_path(path)
        registered[key] = register_artifact(
            client,
            media_path=path,
            artifact_type=kind,
            title=marked(key),
            description="written by tests/integration; safe to delete",
        )
    sources = {key: r.artifact_qid for key, r in registered.items()}
    written = BatchWriter(client).write_batch(fixture_batch(cited))
    seeded = [
        written.created_items[local_id]
        for local_id in ("person", "address", "street", "town", "rahel")
    ]
    wait_for_query_service(config, client, [*seeded, *sources.values()])

    out = tmp_path_factory.mktemp("site")
    try:
        yield Site(
            out=out,
            summary=publish_site(config, out),
            person=written.created_items["person"],
            rahel=written.created_items["rahel"],
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

    # each dated assertion is its own entry, under its own source, and a
    # date its source states no range for shows alone. The session's
    # marker gives Gustav an item of his own, so no earlier session's
    # birth stands beside these.
    person = f'href="people/{site.person}.html"'
    timeline = re.findall(
        r'<div class="entry">(.*?)</div>', site.read("timeline.html")
    )
    dated = [entry for entry in timeline if person in entry]
    for source, birth in BIRTHS.items():
        (entry,) = [e for e in dated if e.startswith(shown(birth.value))]
        assert entry.startswith(f"{shown(birth.value)} — ")
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


def test_the_timeline_shows_a_stated_range_beside_its_date(site):
    # the tree gives Rahel's death as "after 1945": the timeline names the
    # range's one bound, under the qualifier the query read it from, and
    # never shows 1945 alone or as a closed range (#309)
    person = f'href="people/{site.rahel}.html"'
    (entry,) = [
        entry
        for entry in re.findall(
            r'<div class="entry">(.*?)</div>', site.read("timeline.html")
        )
        if person in entry
    ]
    assert entry.startswith(
        '1945 <span class="qualifiers">(earliest date: 1945)</span> — '
    )
    assert source_link(site, TREE, root="") in entry

    feed = json.loads(site.read("timeline.json"))
    (event,) = [
        event
        for event in feed["events"]
        if event["text"]["headline"].startswith(f"{html.escape(RAHEL)}: ")
    ]
    assert event["start_date"] == {
        "year": "1945",
        "display_date": "1945 (earliest date: 1945)",
    }
    assert event["text"]["text"] == f"per {html.escape(marked(TREE))}"


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
