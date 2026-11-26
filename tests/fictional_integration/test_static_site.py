"""The static site built from the lane's real query service.

`yizkr publish-site` (`staticsite.publish_site`) reads the graph through
its own SPARQL and the query library's templates, and writes the private
site. These cases build it from the lane's query service over a fictional
fixture (docs/EXAMPLES.md) and read the pages it writes, beside the
privacy check the non-privileged export runs (`backup.exclusion_report`).

The fixture is written once, straight to the wiki through
WikibaseIntegrator (tests/fixtures/instance_shapes.py), in the shapes an
instance holds: each source's reading with its `stated in` block, page
and excerpt and its run's block, a curator's statement with its `stated
in` block alone, a curator's rank as the rank the statement holds, and the
birth and death events only a curator makes. Each artifact holds a media
file, which the site links to. It holds:

* Gustav Löbig's birth as the 1938 extract and the 1952 naturalisation
  give it, on two days, and at two resolutions: the extract's home at
  Brunnenring 35, which it puts in its street and the street in Flörburg,
  and the naturalisation's Flörburg (docs/MODELING.md, "Contradictions");
  and a person born in the 19th century, given by its first year, beside
  1900 and 1800;
* Rahel's death "after 1945" and Cäcilie's birth "c. 1833" as the 1984
  tree gives them, and Leopold's imprisonment dated by its own start and
  end time as the 1945 card gives it;
* the 1958 letter's union of Rudolf and Nina, ended by his death, with
  Nina widowed at the letter's own date, and no death statement for him
  (docs/LIFE_EVENTS.md, "Derived bounds and union endings");
* Fläckfeld in Kreis Mockburg as the 1905 certificate, which dates
  itself, gives it, and in Kreis Peiltal as the tree gives it, beside a
  misreading of the tree that puts Kreis Peiltal in Fläckfeld and one of
  the certificate's date, each deprecated by a curator; and Kaldovsky
  District in Chadsk Oblast as the letter gives it, with the worked row's
  start time of its own, 1937 (docs/PLACES.md, "Containment as an
  assertion");
* Cäcilie's death of unknown date as the certificate states it, beside
  the dated one of her death register; a death of unknown date alone, and
  one a curator deprecated; a curator's "no value"; Eugen's age at death
  alone; and Alfred's adoptive father (docs/LIFE_EVENTS.md, "Unknown
  values, missing evidence and burial");
* the 2011 booklet's caption over the image of a letter it places on its
  page 6, printed 4, the image's own dateline, address and addressee, the
  studio portrait's studio and two of the people it shows, and Rahel's
  portrait's own date of making, "um 1895", circa (docs/ARTIFACTS.md,
  "Captions, structured assertions and support");
* Sebastian's birth as the certificate gives it twice, on its first page
  and on its back, each with an excerpt of its own (docs/MODELING.md,
  "Reference pattern");
* people whose births are years before or after the privacy check's
  cutoff, at a decade's or a century's precision (a century by its first
  year), with stated ranges and ranks, of unknown value, or none, but
  not in the cutoff year or the year after; deaths by a curator's death
  event, as its principal or in another role, and births by a curator's
  birth event; and items whose person evidence is a `human` type at any
  rank, a birth without a type, or none, as a vessel's part in a
  migration, a death event's own death evidence, a family's religion, a
  label alone, an event of no person kind, a birth with no participant
  and the redirect a merge leaves (docs/PRIVACY.md, "Private site and
  filtered export");
* and two Eugen Löbigs, father and son, under one label.

The site and the check read the whole stack. These cases run on the
`queries` group's stack after the case that asserts over that stack
(scripts/fictional_integration.py), beside the production-query fixture,
so every label here carries SUFFIX, and pages that list the whole stack
(the index, the timeline and its feed, the gallery, the places tree and the
disagreement report) are read for this fixture's entries alone; a count
of the whole site is compared with the fixture only as a floor, what the
fixture adds to it, and otherwise only with the site's own pages and
counts. Births are dated from the cutoff the site and the check share
(`core.privacy.DEFAULT_CUTOFF_YEARS`): a year well clear of it, and a
decade or a century by the last year it allows, on either side of it.
The fixture bypasses the writer, so these cases establish what the site
builds from what an instance holds, not that the writer produces it.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wikibaseintegrator.datatypes import Time
from wikibaseintegrator.wbi_enums import WikibaseRank, WikibaseSnakType

from tests.fixtures import example_universe as u
from tests.fixtures.instance_shapes import (
    Fixture,
    between,
    string,
    synchronize,
    text,
    time_value,
    unknown_time,
)
from yizkr.backup import ExclusionReport, exclusion_report
from yizkr.core import sparql
from yizkr.core.privacy import DEFAULT_CUTOFF_YEARS
from yizkr.disagreement import (
    CONTAINMENT,
    FETCH,
    Containment,
    compare,
    place_within,
    read_containment,
    read_statements,
)
from yizkr.model.schema import load_manifest
from yizkr.queries.publish import load_templates, render_for_instance
from yizkr.staticsite import UNDATED_DEATH, publish_site, visible

pytestmark = pytest.mark.live
PREFERRED = WikibaseRank.PREFERRED
DEPRECATED = WikibaseRank.DEPRECATED
UNIVERSE = u.by_key()
# on every label, so that none is another fixture's on the stack
SUFFIX = "(site fixture)"
# the birth-recency cutoff the site and the check share, and a year well
# before it and one well after it
CUTOFF = datetime.now(UTC).year - DEFAULT_CUTOFF_YEARS
EARLY = CUTOFF - 53
RECENT = CUTOFF + 87
# the first year of the century that holds the cutoff, and of the one
# before it
SPANNING = (CUTOFF - 1) // 100 * 100 + 1
PASSED = SPANNING - 100
# the first year of the decade that holds the second year after the
# cutoff, so that it ends two years or more after the cutoff and, unless
# the cutoff is in its own decade's last two years, starts by it; and the
# first year of the decade before the cutoff's own, which ends before it
LATE_DECADE = (CUTOFF + 2) // 10 * 10
PASSED_DECADE = (CUTOFF // 10 - 1) * 10
# how a page marks whom the non-privileged export would drop
BADGE = '<span class="badge">'


MANIFEST = load_manifest()


def named(label: str) -> str:
    return f"{label} {SUFFIX}"


def qualified(value: object, prop: str, shown: object) -> str:
    """A value as a page shows it beside a qualifier `prop`, which it
    names by the manifest's label."""
    return f"{value} ({MANIFEST.properties[prop].label}: {shown})"


def label_of(key: str) -> str:
    """The universe's label, or a source's title, for `key`."""
    entry = UNIVERSE[key]
    return named(getattr(entry, "title", None) or entry.label)


def said(subject: str, prop: str, source: str) -> u.Assertion:
    """What `source` says of `subject`'s `prop` in the universe."""
    (assertion,) = [a for a in u.about(subject, prop) if a.source == source]
    return assertion


def when(date: u.Date):
    """A universe date at the precision it is given."""
    return time_value(date.year, date.month, date.day)


def day(date: u.Date) -> str:
    """A day-precision date as the site shows it."""
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"


def no_time(pid: str):
    """A curator's "no value": the person has no such date."""
    return Time(prop_nr=pid, snaktype=WikibaseSnakType.NO_VALUE)


# -- the sources ---------------------------------------------------------
#
# Each artifact the universe holds, by a local key, and one more for what
# the fixture states that no universe source does. Each holds a media file
# of its own, which the photo gallery and the artifact pages link to.

SOURCES = {
    "extract": "birth-extract-1938",
    "naturalisation": "naturalisation-1952",
    "gravestone": "gravestone-photo-1956",
    "tree": "family-tree-1984",
    "card": "registration-card-1945",
    "letter": "letter-to-leopold-1958",
    "certificate": "marriage-cert-1905",
    "booklet": "family-booklet-2011",
    "portrait": "studio-photo-1890s",
    "rahels portrait": "studio-photo-1895",
    "register": "death-register-1902",
    "burial": "burial-register-1909",
    "order": "adoption-order-1950",
}
HYPOTHETICAL = named("Register of hypothetical statements")
IMAGE = "letter-excerpt-1939"


def media(key: str) -> str:
    return f"Site fixture {key}.png"


def write_sources(f: Fixture) -> None:
    for key, universe in SOURCES.items():
        f.source(key, label_of(universe), UNIVERSE[universe].artifact_class)
    f.source("hypothetical", HYPOTHETICAL, "certificate")
    # the letter's image as the booklet embeds it on its page 6
    f.source("image", label_of(IMAGE), "photograph", "booklet")
    for key in f.sources:
        f.state(
            key,
            "media_file",
            string(f"File:{media(key)}"),
            "booklet" if key == "image" else key,
        )


# -- a person page shows every value (docs/MODELING.md, "Contradictions") --
#
# The extract registers Gustav's birth on 7 January 1919 "in seiner
# Wohnung", the home of a father "wohnhaft in Flörburg, Brunnenring 35";
# the naturalisation gives 1 July 1919 at Floerburg. The extract puts the
# address in its street and the street in the town, so the two places
# differ in resolution, not in substance.

BIRTHS = {
    "extract": said("gustav-loebig", "date_of_birth", "birth-extract-1938"),
    "naturalisation": said(
        "gustav-loebig", "date_of_birth", "naturalisation-1952"
    ),
}
NATURALISED_PLACE = said(
    "gustav-loebig", "place_of_birth", "naturalisation-1952"
)
# the extract's own words for the birth at home, and for the address
HOME = (
    "wohnhaft in Flörburg, Brunnenring 35, zeigte an, daß von seiner "
    "Ehefrau Hildegard Löbig geborene Laudler zu Flörburg in seiner Wohnung"
)
ADDRESS_IN_TOWN = "wohnhaft in Flörburg, Brunnenring 35"
PLACES = {
    "address": "brunnenring-35",
    "street": "brunnenring",
    "town": "floerburg",
}


def language(source: str) -> str:
    return UNIVERSE[SOURCES[source]].lang or "en"


def write_plural(f: Fixture) -> None:
    for words in (HOME, ADDRESS_IN_TOWN):
        assert words in UNIVERSE[SOURCES["extract"]].text
    scene = "site person page"
    f.entity(
        "gustav",
        label_of("gustav-loebig"),
        scene,
        "human",
        "extract",
        "naturalisation",
    )
    for key, universe in PLACES.items():
        f.entity(key, label_of(universe), scene, "place", "extract")
    for source, birth in BIRTHS.items():
        f.reading(
            "gustav",
            "date_of_birth",
            when(birth.value),
            source,
            excerpt=birth.excerpt,
            language=language(source),
        )
    f.reading(
        "gustav",
        "place_of_birth",
        f.item("address"),
        "extract",
        excerpt=HOME,
        language="de",
    )
    f.reading(
        "gustav",
        "place_of_birth",
        f.item("town"),
        "naturalisation",
        excerpt=NATURALISED_PLACE.excerpt,
    )
    for place, container in (("address", "street"), ("street", "town")):
        f.reading(
            place,
            "part_of",
            f.item(container),
            "extract",
            excerpt=ADDRESS_IN_TOWN,
            language="de",
        )
    # the 19th century per the tree, given by its first year; its last
    # year per the card; the 18th century's last year per the extract
    f.entity(
        "century", named("Century Fixture Person"), scene, "human", "tree"
    )
    for value, source in (
        (time_value(1801, precision="century"), "tree"),
        (time_value(1900), "card"),
        (time_value(1800), "extract"),
    ):
        f.reading("century", "date_of_birth", value, source)


# -- the timeline --------------------------------------------------------

RAHEL_DIED = said("rahel-loebig", "date_of_death", "family-tree-1984")
CAECILIE_BORN = said("caecilie-loebig", "date_of_birth", "family-tree-1984")
HELD = {
    prop: said("imprisonment-1939", prop, "registration-card-1945")
    for prop in ("start_time", "end_time")
}
HELD_BY = said("imprisonment-1939", "participant", "registration-card-1945")
# "after 1945" and "c. 1833" as a page shows them, each beside the
# qualifier the query reads its range or circumstance from
AFTER = qualified(
    RAHEL_DIED.value.year, "earliest_date", RAHEL_DIED.value.year
)
CIRCA = MANIFEST.items["circa"].label
ABOUT = qualified(CAECILIE_BORN.value.year, "sourcing_circumstances", CIRCA)


def write_timeline(f: Fixture) -> None:
    scene = "site timeline"
    # "after 1945": 1945, with 1945 as its earliest date and no latest
    f.entity("rahel", label_of("rahel-loebig"), scene, "human", "tree")
    f.reading(
        "rahel",
        "date_of_death",
        when(RAHEL_DIED.value),
        "tree",
        excerpt=RAHEL_DIED.excerpt,
        qualifiers=(("earliest_date", when(RAHEL_DIED.value)),),
    )
    # "c. 1833": 1833, with circa as its sourcing circumstance
    f.entity("caecilie", label_of("caecilie-loebig"), scene, "human", "tree")
    f.reading(
        "caecilie",
        "date_of_birth",
        when(CAECILIE_BORN.value),
        "tree",
        excerpt=CAECILIE_BORN.excerpt,
        qualifiers=(("sourcing_circumstances", f.vocab("circa")),),
    )
    f.entity("leopold", label_of("leopold-loebig"), scene, "human", "card")
    f.entity(
        "held", label_of("imprisonment-1939"), scene, "imprisonment", "card"
    )
    f.reading(
        "held",
        "participant",
        f.item("leopold"),
        "card",
        excerpt=HELD_BY.excerpt,
    )
    for prop, held in HELD.items():
        f.reading("held", prop, when(held.value), "card", excerpt=held.excerpt)


# -- a union ended by a death (docs/LIFE_EVENTS.md) -------------------------
#
# Nina's letter, dated 25 December 1958, states Rudolf's death with no date
# in a phrase that is both markers ("the widow of your late brother
# Rudolf"): their marriage ended by his death, and Nina widowed at the
# letter's date. No source here states his death, so the site derives his
# "died by" from the letter's date, and never shows Nina to have died.

LETTER_DATED = said(
    "letter-to-leopold-1958", "inception", "letter-to-leopold-1958"
)
LATE = said(
    "marriage-rudolf-nina", "ended_by_death_of", "letter-to-leopold-1958"
)
WIDOWED = said("nina-sartina", "civil_status", "letter-to-leopold-1958")


def write_union(f: Fixture) -> None:
    scene = "site union"
    f.reading(
        "letter",
        "inception",
        when(LETTER_DATED.value),
        "letter",
        excerpt=LETTER_DATED.excerpt,
        language="ru",
    )
    for key, universe in (
        ("rudolf", "rudolf-loebig"),
        ("nina", "nina-sartina"),
    ):
        f.entity(key, label_of(universe), scene, "human", "letter")
    f.entity(
        "marriage",
        label_of("marriage-rudolf-nina"),
        scene,
        "marriage",
        "letter",
    )
    for partner in ("rudolf", "nina"):
        f.reading(
            "marriage",
            "participant",
            f.item(partner),
            "letter",
            excerpt=LATE.excerpt,
            language="ru",
            qualifiers=(("object_has_role", f.vocab("spouse")),),
        )
    f.reading(
        "marriage",
        "ended_by_death_of",
        f.item("rudolf"),
        "letter",
        excerpt=LATE.excerpt,
        language="ru",
    )
    (widowed_at,) = [
        date for prop, date in WIDOWED.qualifiers if prop == "point_in_time"
    ]
    f.reading(
        "nina",
        "civil_status",
        f.vocab("widowed"),
        "letter",
        excerpt=WIDOWED.excerpt,
        language="ru",
        qualifiers=(("point_in_time", when(widowed_at)),),
    )


# -- containment as a source's claim (docs/PLACES.md) ----------------------

CERTIFICATE_DATED = said(
    "marriage-cert-1905", "inception", "marriage-cert-1905"
)
# a misreading of the certificate's "1905" as 1903, which a curator
# deprecated
MISREAD_YEAR = 1903
IN_MOCKBURG = said("flaeckfeld", "part_of", "marriage-cert-1905")
IN_PEILTAL = said("flaeckfeld", "part_of", "family-tree-1984")
DISTRICT_PLACED = said(
    "kaldovsky-district", "part_of", "letter-to-leopold-1958"
)
# the worked row's start time for it (docs/EXAMPLES.md), which no source in
# the universe states
DISTRICT_FROM = 1937
CONTAINERS = {
    "flaeckfeld": ("flaeckfeld", "certificate"),
    "mockburg": ("kreis-mockburg", "certificate"),
    "peiltal": ("kreis-peiltal", "tree"),
    "district": ("kaldovsky-district", "letter"),
    "oblast": ("chadsk-oblast", "letter"),
}


def write_places(f: Fixture) -> None:
    scene = "site places"
    dated = CERTIFICATE_DATED.value
    for year, rank in ((dated.year, None), (MISREAD_YEAR, DEPRECATED)):
        f.reading(
            "certificate",
            "inception",
            time_value(year, dated.month, dated.day),
            "certificate",
            excerpt=CERTIFICATE_DATED.excerpt,
            language="de",
            rank=rank,
        )
    for key, (universe, source) in CONTAINERS.items():
        f.entity(key, label_of(universe), scene, "place", source)
    f.reading(
        "flaeckfeld",
        "part_of",
        f.item("mockburg"),
        "certificate",
        excerpt=IN_MOCKBURG.excerpt,
        language="de",
    )
    f.reading(
        "flaeckfeld",
        "part_of",
        f.item("peiltal"),
        "tree",
        excerpt=IN_PEILTAL.excerpt,
    )
    # the misreading of the tree's words, which a curator deprecated
    f.reading(
        "peiltal",
        "part_of",
        f.item("flaeckfeld"),
        "tree",
        excerpt=IN_PEILTAL.excerpt,
        rank=DEPRECATED,
    )
    f.reading(
        "district",
        "part_of",
        f.item("oblast"),
        "letter",
        excerpt=DISTRICT_PLACED.excerpt,
        language="ru",
        qualifiers=(("start_time", time_value(DISTRICT_FROM)),),
    )


# -- deaths (docs/LIFE_EVENTS.md) -------------------------------------------

LATE_WIFE = said("caecilie-loebig", "date_of_death", "marriage-cert-1905")
DIED = said("caecilie-loebig", "date_of_death", "death-register-1902")
AGED = said("eugen-loebig", "age_at_death", "burial-register-1909")
ADOPTED = said("alfred-luthorn", "father", "adoption-order-1950")
# a hypothetical register's words for a death it does not date
LATE_FELIX = "der selige Felix"


def write_deaths(f: Fixture) -> None:
    scene = "site deaths"
    assert isinstance(LATE_WIFE.value, u.Unknown)
    f.reading(
        "caecilie",
        "date_of_death",
        unknown_time,
        "certificate",
        excerpt=LATE_WIFE.excerpt,
        language="de",
    )
    f.reading(
        "caecilie",
        "date_of_death",
        when(DIED.value),
        "register",
        excerpt=DIED.excerpt,
        language="de",
    )
    # a death of unknown date alone, which releases its person, and one a
    # curator deprecated, which releases no one
    f.entity("felix", label_of("felix-loebig"), scene, "human", "hypothetical")
    f.entity(
        "deprecated death of",
        named("Felix Löbig, his death deprecated"),
        scene,
        "human",
        "hypothetical",
    )
    for key, rank in (("felix", None), ("deprecated death of", DEPRECATED)):
        f.reading(
            key,
            "date_of_death",
            unknown_time,
            "hypothetical",
            excerpt=LATE_FELIX,
            language="de",
            rank=rank,
        )
    # a curator's "no value", which releases no one
    f.entity(
        "no death", label_of("nicholas-lobig"), scene, "human", "hypothetical"
    )
    f.curated("no death", "date_of_death", no_time, "hypothetical")
    # an age at death, his only death evidence
    f.entity("aged", label_of("eugen-loebig"), scene, "human", "burial")
    f.reading(
        "aged",
        "age_at_death",
        string(AGED.value),
        "burial",
        excerpt=AGED.excerpt,
        language="de",
    )
    # an adoptive father, with the order's own word for him
    f.entity("alfred", label_of("alfred-luthorn"), scene, "human", "order")
    stated = dict(ADOPTED.qualifiers)
    f.reading(
        "alfred",
        "father",
        f.item("leopold"),
        "order",
        excerpt=ADOPTED.excerpt,
        language="de",
        qualifiers=(
            ("parentage", f.vocab(stated["parentage"])),
            ("kinship_type", string(stated["kinship_type"])),
        ),
    )


# -- what sources say about an artifact (docs/ARTIFACTS.md) -----------------

CAPTION = said(IMAGE, "caption", "family-booklet-2011")
# the page number printed on the booklet's page 6
PRINTED = "4"
IMAGE_SAYS = {
    prop: said(IMAGE, prop, IMAGE)
    for prop in ("inception", "location_of_creation", "addressee")
}
STUDIO = said("studio-photo-1890s", "author", "studio-photo-1890s")
# two of the people the portrait shows, by the names on its reverse
SHOWS = {"rahel": "rahel-loebig", "felix": "felix-loebig"}
MADE = said("studio-photo-1895", "inception", "studio-photo-1895")


def write_sources_say(f: Fixture) -> None:
    scene = "site sources say"
    f.entity("klara", label_of("klara-loebig"), scene, "human", "image")
    f.entity(
        "studio",
        label_of("studio-bruesrich"),
        scene,
        "organization",
        "portrait",
    )
    f.state(
        "image",
        "caption",
        text(CAPTION.value),
        "booklet",
        run=True,
        page=dict(CAPTION.qualifiers)["page_number"],
        printed=PRINTED,
        excerpt=CAPTION.excerpt,
    )
    values = {
        "inception": when(IMAGE_SAYS["inception"].value),
        "location_of_creation": f.item("address"),
        "addressee": f.item("klara"),
    }
    for prop, value in values.items():
        f.state(
            "image",
            prop,
            value,
            "image",
            run=True,
            excerpt=IMAGE_SAYS[prop].excerpt,
            language="de",
        )
    f.state(
        "portrait",
        "author",
        f.item("studio"),
        "portrait",
        run=True,
        excerpt=STUDIO.excerpt,
        language="de",
    )
    for key, person in SHOWS.items():
        (shows,) = [
            a
            for a in u.about("studio-photo-1890s", "shows")
            if a.value == person
        ]
        f.state(
            "portrait",
            "shows",
            f.item(key),
            "portrait",
            run=True,
            excerpt=shows.excerpt,
            language="de",
        )
    # "um 1895": 1895, with circa as its sourcing circumstance
    f.state(
        "rahels portrait",
        "inception",
        when(MADE.value),
        "rahels portrait",
        run=True,
        excerpt=MADE.excerpt,
        language="de",
        qualifiers=(("sourcing_circumstances", f.vocab("circa")),),
    )


# -- one value asserted in two places (docs/MODELING.md) --------------------

FRONT = said("sebastian-loebig", "date_of_birth", "marriage-cert-1905")
# the registrar's note on the certificate's back, which gives the birth
# again
BACK = ("Rückseite", "geb. 15.2.1862 in Flörburg")


def write_locators(f: Fixture) -> None:
    f.entity(
        "sebastian",
        label_of("sebastian-loebig"),
        "site locators",
        "human",
        "certificate",
    )
    born = f.reading(
        "sebastian",
        "date_of_birth",
        when(FRONT.value),
        "certificate",
        excerpt=FRONT.excerpt,
        language="de",
    )
    page, excerpt = BACK
    f.cite(born, "certificate", page=page, excerpt=excerpt, language="de")


# -- death events and person evidence (docs/PRIVACY.md) ---------------------
#
# Eugen's death as an event a curator makes, with Eugen its principal,
# stating his cause of death and his age and dated 1909: an event is set
# aside on the death evidence it states, so it gets no person page, and it
# releases its principal. A death event that states a birth too, or whose
# only cause of death a curator deprecated, is still a person.

CAUSE = "Wassersucht"


def write_event_people(f: Fixture) -> None:
    scene = "site event people"
    assert f"Todesursache: {CAUSE}" in UNIVERSE[SOURCES["burial"]].text
    f.entity("mourned", label_of("eugen-loebig"), scene, "human", "burial")
    for key, label, source in (
        ("death", named("Death of Eugen Löbig"), "burial"),
        ("born death", named("Death stating a birth"), "hypothetical"),
        (
            "deprecated death",
            named("Death with a deprecated cause"),
            "hypothetical",
        ),
    ):
        f.make(key, label, scene)
        f.curated(key, "instance_of", f.vocab("death"), source)
    f.curated(
        "death",
        "participant",
        f.item("mourned"),
        "burial",
        qualifiers=(("object_has_role", f.vocab("principal")),),
    )
    f.curated("death", "cause_of_death", text(CAUSE, "de"), "burial")
    f.curated("death", "age_at_death", string(AGED.value), "burial")
    f.curated("death", "point_in_time", time_value(1909), "burial")
    f.curated(
        "born death", "cause_of_death", text(CAUSE, "de"), "hypothetical"
    )
    f.curated("born death", "date_of_birth", time_value(EARLY), "hypothetical")
    f.curated(
        "deprecated death",
        "cause_of_death",
        text(CAUSE, "de"),
        "hypothetical",
        rank=DEPRECATED,
    )


# People who take part in birth and death events, each with their own
# birth (RECENT, EARLY or none) and whether the check leaves them out: an
# event is its one principal's own, and nobody's with no role or two
# principals; a dated birth event is a birth of its principal, and adds
# its date to the births of a participant in one that is nobody's own.
EVENT_PEOPLE = {
    "principal": (RECENT, False),
    "informant": (RECENT, True),
    "witness": (RECENT, True),
    "sole": (RECENT, True),
    "pair one": (RECENT, True),
    "pair two": (RECENT, True),
    "co-principal one": (RECENT, True),
    "co-principal two": (RECENT, True),
    "born twice": (EARLY, True),
    "birth informant": (EARLY, False),
    "unattributed birth": (EARLY, True),
    "born by event": (None, False),
}
# each event: its kind, the year it is dated, and its participants with
# the role each takes part in, or none
EVENTS = {
    "death": (
        "death",
        None,
        (
            ("principal", "principal"),
            ("informant", "informant"),
            ("witness", "witness"),
        ),
    ),
    "lone death": ("death", None, (("sole", None),)),
    "shared death": ("death", None, (("pair one", None), ("pair two", None))),
    "double death": (
        "death",
        None,
        (("co-principal one", "principal"), ("co-principal two", "principal")),
    ),
    "birth": (
        "birth",
        RECENT,
        (("born twice", "principal"), ("birth informant", "informant")),
    ),
    "roleless birth": ("birth", RECENT, (("unattributed birth", None),)),
    "early birth": ("birth", EARLY, (("born by event", "principal"),)),
}


def taking_part(role: str) -> str:
    return f"taking part: {role}"


def write_event_privacy(f: Fixture) -> None:
    scene = "site event privacy"
    for role, (born, _) in EVENT_PEOPLE.items():
        key = taking_part(role)
        f.entity(
            key,
            named(f"Event Privacy {role.title()}"),
            scene,
            "human",
            "hypothetical",
        )
        if born is not None:
            f.reading(key, "date_of_birth", time_value(born), "hypothetical")
    for event, (kind, year, parts) in EVENTS.items():
        key = f"event: {event}"
        f.make(key, named(f"Event Privacy {event.title()}"), scene)
        f.curated(key, "instance_of", f.vocab(kind), "hypothetical")
        for role, as_role in parts:
            f.curated(
                key,
                "participant",
                f.item(taking_part(role)),
                "hypothetical",
                qualifiers=(
                    (("object_has_role", f.vocab(as_role)),) if as_role else ()
                ),
            )
        if year is not None:
            f.curated(key, "point_in_time", time_value(year), "hypothetical")


# People whose births are years before or after the cutoff, at a decade's
# or a century's precision (a century by its first year), with stated
# ranges and ranks, of unknown value or none, but not in the cutoff year
# or the year after, and whether the check leaves them out: a birth
# releases only at the latest year it allows, a decade's or a century's
# last, every statement counts but a deprecated one, a range its source
# states bounds it, and no birth at all keeps a person possibly living.
BORN = {
    "early": (((time_value(EARLY), (), None),), False),
    "recent": (((time_value(RECENT), (), None),), True),
    "late decade": (
        ((time_value(LATE_DECADE, precision="decade"), (), None),),
        True,
    ),
    "passed decade": (
        ((time_value(PASSED_DECADE, precision="decade"), (), None),),
        False,
    ),
    "spanning century": (
        ((time_value(SPANNING, precision="century"), (), None),),
        True,
    ),
    "passed century": (
        ((time_value(PASSED, precision="century"), (), None),),
        False,
    ),
    "two births": (
        ((time_value(EARLY), (), None), (time_value(RECENT), (), None)),
        True,
    ),
    "preferred early": (
        ((time_value(EARLY), (), PREFERRED), (time_value(RECENT), (), None)),
        True,
    ),
    "deprecated recent": (
        ((time_value(EARLY), (), None), (time_value(RECENT), (), DEPRECATED)),
        False,
    ),
    "deprecated only": (
        (
            (
                time_value(EARLY),
                (("latest_date", time_value(EARLY)),),
                DEPRECATED,
            ),
        ),
        True,
    ),
    "unknown beside": (
        ((time_value(EARLY), (), None), (unknown_time, (), None)),
        True,
    ),
    "before": (
        ((time_value(EARLY), (("latest_date", time_value(EARLY)),), None),),
        False,
    ),
    "after": (
        ((time_value(EARLY), (("earliest_date", time_value(EARLY)),), None),),
        True,
    ),
    "between": (
        (
            (
                time_value(EARLY),
                between(time_value(EARLY), time_value(RECENT)),
                None,
            ),
        ),
        True,
    ),
    "bounded century": (
        (
            (
                time_value(SPANNING, precision="century"),
                (("latest_date", time_value(SPANNING)),),
                None,
            ),
        ),
        False,
    ),
    "no birth": ((), True),
}


def born_as(role: str) -> str:
    return f"born: {role}"


def write_births(f: Fixture) -> None:
    scene = "site births"
    for role, (births, _) in BORN.items():
        key = born_as(role)
        f.entity(
            key,
            named(f"Privacy Fixture {role.title()}"),
            scene,
            "human",
            "hypothetical",
        )
        for value, qualifiers, rank in births:
            f.reading(
                key,
                "date_of_birth",
                value,
                "hypothetical",
                qualifiers=qualifiers,
                rank=rank,
            )
    # death evidence releases a person whatever their birth
    f.entity(
        born_as("died"),
        named("Privacy Fixture Died"),
        scene,
        "human",
        "hypothetical",
    )
    f.reading(
        born_as("died"), "date_of_birth", time_value(RECENT), "hypothetical"
    )
    f.reading(
        born_as("died"),
        "date_of_death",
        time_value(RECENT + 5),
        "hypothetical",
    )


# Items by the person evidence they hold, as the non-privileged export
# reads it: a `human` type at any rank, a birth whatever the type, or
# none. A vessel's part in a migration makes it no person, but a `human`
# type a curator deprecated beside its own does. A religion is no person
# evidence, so beside a type it leaves the type all that marks a person,
# and a family with a religion alone is none, nor is an item with a label
# alone, an event of no person kind (a talk) or a birth a curator makes
# with no participant, as a claim refused or cut short leaves one. A
# duplicate a curator merges into a person once the fixture is written
# (`merge_duplicate`) leaves a redirect, which is no person: its death
# goes with its statements to the person, whom it releases.


def evidence(role: str) -> str:
    return f"evidence: {role}"


def write_person_evidence(f: Fixture) -> None:
    scene = "site person evidence"

    def kind(role, *kinds, rank=None):
        key = evidence(role)
        label = named(f"Person Evidence {role.title()}")
        if kinds:
            f.entity(
                key, label, scene, kinds[0], "hypothetical", kind_rank=rank
            )
        else:
            f.make(key, label, scene)
        return key

    deprecated = kind("deprecated type", "human", rank=DEPRECATED)
    outranked = kind("outranked type", "human")
    f.curated(
        outranked,
        "instance_of",
        f.vocab("organization"),
        "hypothetical",
        rank=PREFERRED,
    )
    for key in (deprecated, outranked):
        f.reading(key, "religion", text("lutherisch", "de"), "hypothetical")
    for key in (kind("no type"), kind("organization born", "organization")):
        f.reading(key, "date_of_birth", time_value(RECENT), "hypothetical")
    released = kind("released", "human")
    f.reading(released, "date_of_death", time_value(RECENT), "hypothetical")
    migration = kind("migration", "migration")
    vessels = (kind("vessel", "vessel"), kind("vessel typed human", "vessel"))
    f.curated(
        vessels[1],
        "instance_of",
        f.vocab("human"),
        "hypothetical",
        rank=DEPRECATED,
    )
    for vessel in vessels:
        f.reading(migration, "participant", f.item(vessel), "hypothetical")
    family = kind("family", "family")
    f.reading(family, "religion", text("lutherisch", "de"), "hypothetical")
    kind("label only")
    talk = kind("talk", "event")
    f.reading(talk, "participant", f.item(released), "hypothetical")
    unrecorded = kind("unrecorded birth")
    f.curated(unrecorded, "instance_of", f.vocab("birth"), "hypothetical")
    target = kind("merge target", "human")
    f.reading(target, "date_of_birth", time_value(RECENT), "hypothetical")
    duplicate = kind("merged away", "human")
    f.reading(duplicate, "date_of_death", time_value(RECENT), "hypothetical")


def merge_duplicate(f: Fixture) -> None:
    """A curator's merge of the duplicate into the person it repeats
    (`WikiClient.merge_items`): its item becomes a redirect to them."""
    f.client.merge_items(
        f.qids[evidence("merged away")],
        f.qids[evidence("merge target")],
        f.summary,
    )


# -- two people under one label --------------------------------------------
#
# The master hatter of the universe, and a son of the same name, a homonym
# the fixture makes up: Wikibase refuses two items with one label and
# description, never one label alone.

HOMONYMS = {
    "father": ("master hatter, 1836-1909", 1836, "tree"),
    "son": (
        "son of the master hatter, of the same name",
        1870,
        "hypothetical",
    ),
}


def write_homonyms(f: Fixture) -> None:
    for key, (description, year, source) in HOMONYMS.items():
        f.entity(
            key,
            label_of("eugen-loebig"),
            "site homonyms",
            "human",
            source,
            description=description,
        )
        f.reading(key, "date_of_birth", time_value(year), source)
    f.reading("son", "father", f.item("father"), "hypothetical")


# the fixture's items with person evidence, which the site lists and the
# check assesses: a death event that states a birth or only a deprecated
# cause of death, an organization with a birth and a vessel typed human
# among them; no other event is one (Eugen's death event is set aside on
# the death it states, and the talk is of no person kind), nor the plain
# vessel, the family, the item with a label alone, the redirect, a place,
# the studio or an artifact
PEOPLE = {
    "gustav",
    "century",
    "rahel",
    "caecilie",
    "leopold",
    "rudolf",
    "nina",
    "felix",
    "deprecated death of",
    "no death",
    "aged",
    "alfred",
    "klara",
    "sebastian",
    "mourned",
    "born death",
    "deprecated death",
    "father",
    "son",
    *map(taking_part, EVENT_PEOPLE),
    *map(born_as, [*BORN, "died"]),
    *map(
        evidence,
        (
            "deprecated type",
            "outranked type",
            "no type",
            "organization born",
            "released",
            "vessel typed human",
            "merge target",
        ),
    ),
}
# whom the check leaves out, and so the site badges, as each scene's
# reading of birth and death evidence says
BADGED = {
    "deaths": {
        "caecilie": False,
        "felix": False,
        "deprecated death of": True,
        "no death": True,
        "aged": False,
        "rudolf": False,
        "nina": True,
    },
    "death events": {
        "mourned": False,
        "born death": False,
        "deprecated death": True,
    },
    "event privacy": {
        taking_part(role): excluded
        for role, (_, excluded) in EVENT_PEOPLE.items()
    },
    "births": {
        **{born_as(role): excluded for role, (_, excluded) in BORN.items()},
        born_as("died"): False,
    },
    "person evidence": {
        evidence("deprecated type"): True,
        evidence("outranked type"): True,
        evidence("no type"): True,
        evidence("organization born"): True,
        evidence("released"): False,
        evidence("vessel typed human"): True,
        evidence("merge target"): False,
    },
}


def write_fixture(client) -> Fixture:
    f = Fixture(client, "static-site")
    write_sources(f)
    write_plural(f)
    write_timeline(f)
    write_union(f)
    write_places(f)
    write_deaths(f)
    write_sources_say(f)
    write_locators(f)
    write_event_people(f)
    write_event_privacy(f)
    write_births(f)
    write_person_evidence(f)
    write_homonyms(f)
    f.write()
    merge_duplicate(f)
    return f


@dataclass(frozen=True)
class Site:
    """The site built over the fixture, its summary, and the privacy
    check's call on the same stack."""

    graph: Fixture
    out: Path
    summary: dict
    report: ExclusionReport

    def read(self, relative: str) -> str:
        return (self.out / relative).read_text()

    def person(self, key: str) -> str:
        return self.read(f"people/{self.graph.qids[key]}.html")

    def artifact(self, key: str) -> str:
        return self.read(f"artifacts/{self.graph.qids[key]}.html")

    def listed(self, key: str) -> bool:
        return (self.out / f"people/{self.graph.qids[key]}.html").is_file()

    def label(self, key: str) -> str:
        return self.graph.labels[key]

    def cited(self, key: str, root: str = "../") -> str:
        """How a page links a value to the artifact `key` that states
        it."""
        return (
            f'<a href="{root}artifacts/{self.graph.qids[key]}.html">'
            f"{html.escape(self.label(key))}</a>"
        )

    def links(self, key: str, root: str = "../") -> str:
        """How a page links to the person `key`'s page."""
        return f'<a href="{root}people/{self.graph.qids[key]}.html">'


@pytest.fixture(scope="module")
def site(fictional_client, tmp_path_factory) -> Site:
    graph = write_fixture(fictional_client)
    synchronize(fictional_client)
    out = tmp_path_factory.mktemp("site")
    config = fictional_client.config
    return Site(
        graph=graph,
        out=out,
        summary=publish_site(config, out),
        report=exclusion_report(config),
    )


# -- reading the pages -----------------------------------------------------


def elements(markup: str, tag: str) -> list[str]:
    """The contents of every (unnested) `tag` element of `markup`."""
    return re.findall(rf"<{tag}>(.*?)</{tag}>", markup, flags=re.S)


def badged(page: str) -> bool:
    """Whether a person page's heading carries the possibly-living
    badge."""
    (heading,) = elements(page, "h1")
    return BADGE in heading


def described(markup: str) -> dict[str, list[str]]:
    """A `<dl class="statements">` of a page's: its values by the label of
    their property. On an artifact page the first is what sources say
    about it; {} for a page that has none."""
    found = re.findall(r'<dl class="statements">(.*?)</dl>', markup, re.S)
    if not found:
        return {}
    return {
        prop: elements(values, "dd")
        for prop, values in re.findall(
            r"<dt>(.*?)</dt>((?:<dd>.*?</dd>)*)", found[0]
        )
    }


def shown(entries: list[str]) -> list[str]:
    """What a reader sees of each entry, sorted."""
    return sorted(map(visible, entries))


def timeline(site: Site) -> tuple[list[str], list[str]]:
    """The timeline page's dated entries, and those of its "No date
    stated" foot."""
    page = site.read("timeline.html")
    dated, _, foot = page.partition("<h2>No date stated</h2>")
    return tuple(
        re.findall(r'<div class="entry">(.*?)</div>', part)
        for part in (dated, foot)
    )


def entries_of(site: Site, key: str) -> list[str]:
    """The timeline's dated entries of the person `key`."""
    person = site.links(key, root="")
    return [entry for entry in timeline(site)[0] if person in entry]


def starting(date: u.Date) -> dict[str, str]:
    """A day-precision date as the timeline feed starts an event at it."""
    return {
        "year": f"{date.year:04d}",
        "month": f"{date.month:02d}",
        "day": f"{date.day:02d}",
    }


def feed_of(site: Site, key: str) -> list[dict]:
    """The timeline feed's events of the person `key`."""
    feed = json.loads(site.read("timeline.json"))
    headline = f"{html.escape(site.label(key))}: "
    return [
        event
        for event in feed["events"]
        if event["text"]["headline"].startswith(headline)
    ]


def table(page: str) -> list[list[str]]:
    """The cells of each row of a page's tables, header rows left out."""
    rows = [elements(row, "td") for row in elements(page, "tr")]
    return [cells for cells in rows if cells]


def asserted(site: Site, key: str) -> list[list[str]]:
    """The rows of what the artifact `key` asserts, as its page lists them;
    the page's count of its statements must be theirs."""
    page = site.artifact(key)
    (count,) = re.findall(r"What this artifact asserts \((\d+)\)", page)
    rows = table(page)
    assert int(count) == len(rows)
    return rows


def about(site: Site, rows: list[list[str]], key: str) -> list[list[str]]:
    """The rows whose subject is the fixture item `key`."""
    return [row for row in rows if site.links(key) in row[0]]


def place(page: str, excerpt: str, language: str) -> str:
    """A reference block's page and excerpt, as an artifact page shows
    where it states something."""
    return (
        f'page number: {html.escape(page)}: <q lang="{language}">'
        f"{html.escape(excerpt)}</q>"
    )


def containers(site: Site) -> dict[tuple[str, str], str]:
    """The places page's containment tree as a reader meets it, for the
    fixture's places only: by each (container, place) pair of labels, the
    markup shown beside the place where it stands under that container."""
    page = site.read("places.html")
    mine = {site.label(key) for key in (*PLACES, *CONTAINERS)}
    tree = page[page.index("<h2>Containment</h2>") :]
    found: dict[tuple[str, str], str] = {}
    path: list[str] = []
    for entry in re.finditer(r"<li>(.*?)(?=<ul>|</li>)|</li>", tree):
        if entry.group(0) == "</li>":
            path.pop()
            continue
        label, span, beside = entry.group(1).partition("<span")
        label = visible(label)
        if path and label in mine:
            found[path[-1], label] = span + beside
        path.append(label)
    return found


def select(client, name: str) -> list[dict]:
    """The rows of the query library's template `name`."""
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()[name], properties, items, client.config.concept_uri
    )
    return sparql.select(client.config.sparql_url, query)


def value(row: dict, name: str) -> str | None:
    return row[name]["value"] if name in row else None


# -- the cases -------------------------------------------------------------


def test_a_person_page_shows_every_attributed_value_and_picks_none(site):
    # the extract and the naturalisation disagree on Gustav's day of birth
    # and give his birthplace at two resolutions: the page shows every
    # value, each with the source that states it, and picks none
    values = described(site.person("gustav"))
    births = values["date of birth"]
    assert shown(births) == sorted(
        f"{day(birth.value)}{site.label(source)}"
        for source, birth in BIRTHS.items()
    )
    assert shown(values["place of birth"]) == sorted(
        [
            f"{site.label('address')}{site.label('extract')}",
            f"{site.label('town')}{site.label('naturalisation')}",
        ]
    )
    for entries in (births, values["place of birth"]):
        for source in ("extract", "naturalisation"):
            (entry,) = [e for e in entries if site.label(source) in visible(e)]
            assert site.cited(source) in entry

    # the query service's value for a century, given by its first year, as
    # the site names it, beside the century's last year and the year
    # before it
    assert shown(described(site.person("century"))["date of birth"]) == sorted(
        [
            f"19th century{site.label('tree')}",
            f"1900{site.label('card')}",
            f"1800{site.label('extract')}",
        ]
    )

    # a date's own range shows beside it, at its own precision, under its
    # qualifier's label: "after 1945" names its one bound
    (death,) = described(site.person("rahel"))["date of death"]
    assert visible(death) == f"{AFTER}{site.label('tree')}"
    assert site.cited("tree") in death

    # an adoptive father, with the kind of parenthood and the word the
    # order gives him
    (father,) = described(site.person("alfred"))["father"]
    stated = dict(ADOPTED.qualifiers)
    adoptive = MANIFEST.items[stated["parentage"]].label
    assert f'href="{site.graph.qids["leopold"]}.html"' in father
    assert f"parentage: {adoptive}" in visible(father)
    assert f"kinship type: {stated['kinship_type']}" in visible(father)
    assert site.cited("order") in father


def test_the_roster_and_badges_agree_with_the_privacy_check(site):
    graph, report = site.graph, site.report
    # the site lists exactly whom the check assesses: every item with
    # person evidence, a `human` type at any rank, a birth without a type
    # and a death event that states a birth among them, and none of the
    # events on the death they state, the vessel, the migration, the
    # family, the item with a label alone, the talk, the birth with no
    # participant or the merge's redirect
    listed = {key for key in graph.qids if site.listed(key)}
    assessed = {
        key
        for key, qid in graph.qids.items()
        if qid in report.included | report.excluded
    }
    assert listed == assessed == PEOPLE
    # a page for each person, and the index links each; the run record
    # counts the statements of every person on the stack, at least one of
    # each of the fixture's
    assert site.summary["people"] == len(list((site.out / "people").iterdir()))
    assert site.summary["statements"] >= len(PEOPLE)
    index = dict(
        re.findall(
            r'<li><a href="people/(Q[1-9][0-9]*)\.html">.*?</a>(.*?)</li>',
            site.read("index.html"),
        )
    )

    # a person page carries the possibly-living badge exactly when the
    # check leaves the person out, and so does the index
    marked = {key for key in listed if badged(site.person(key))}
    assert marked == {
        key for key in listed if graph.qids[key] in report.excluded
    }
    assert {key for key in listed if BADGE in index[graph.qids[key]]} == marked
    for scene, expected in BADGED.items():
        assert {key: key in marked for key in expected} == expected, scene

    # two people under one label: a page for each, its heading the label
    # and its subtitle its own description
    for key, (description, *_) in HOMONYMS.items():
        page = site.person(key)
        assert elements(page, "h1") == [html.escape(label_of("eugen-loebig"))]
        assert html.escape(description) in page


def test_the_timeline_dates_each_assertion_with_its_range_and_source(site):
    # each dated assertion is an entry of its own, under its own source,
    # and a date its source states no range for shows alone
    gustav = entries_of(site, "gustav")
    assert shown(gustav) == sorted(
        f"{day(birth.value)} — {site.label('gustav')}, date of birth"
        f"{site.label(source)}"
        for source, birth in BIRTHS.items()
    )
    for source in BIRTHS:
        (entry,) = [e for e in gustav if site.label(source) in visible(e)]
        assert site.cited(source, root="") in entry
    starts = [event["start_date"] for event in feed_of(site, "gustav")]
    for birth in BIRTHS.values():
        assert starting(birth.value) in starts

    # "after 1945" names its range's one bound, under the qualifier the
    # query read it from, on the page and in the feed
    (entry,) = entries_of(site, "rahel")
    assert visible(entry) == (
        f"{AFTER} — {site.label('rahel')}, date of death{site.label('tree')}"
    )
    (event,) = feed_of(site, "rahel")
    assert event["start_date"] == {
        "year": "1945",
        "display_date": AFTER,
    }
    assert event["text"]["text"] == html.escape(f"per {site.label('tree')}")

    # "c. 1833" says it is circa, on the page and in the feed
    (entry,) = [e for e in entries_of(site, "caecilie") if "1833" in e]
    assert visible(entry) == (
        f"{ABOUT} — {site.label('caecilie')}, "
        f"date of birth{site.label('tree')}"
    )
    (event,) = [
        event
        for event in feed_of(site, "caecilie")
        if event["start_date"]["year"] == "1833"
    ]
    assert event["start_date"]["display_date"] == ABOUT

    # an event dated by its own start and end time: a row for each, named
    # by the time beside the event's type, under the card
    held = entries_of(site, "leopold")
    assert shown(held) == sorted(
        f"{day(stated.value)} — {site.label('leopold')}, imprisonment "
        f"({MANIFEST.properties[prop].label}){site.label('card')}"
        for prop, stated in HELD.items()
    )
    assert all(site.cited("card", root="") in entry for entry in held)
    events = feed_of(site, "leopold")
    assert len(events) == len(HELD)
    assert {
        event["text"]["headline"]: event["start_date"] for event in events
    } == {
        html.escape(
            f"{site.label('leopold')}: imprisonment "
            f"({MANIFEST.properties[prop].label})"
        ): starting(stated.value)
        for prop, stated in HELD.items()
    }
    assert {event["text"]["text"] for event in events} == {
        html.escape(f"per {site.label('card')}")
    }
    # the run record counts the timeline's rows, each dated one an event of
    # the feed, the fixture's above among them
    feed = json.loads(site.read("timeline.json"))["events"]
    assert site.summary["timeline_rows"] >= len(feed)

    # a death its source states with no date stands beside the dated one,
    # and one dated by nothing under "No date stated"
    entries, foot = timeline(site)
    caecilie = entries_of(site, "caecilie")
    (dated,) = [e for e in caecilie if day(DIED.value) in e]
    (beside,) = [e for e in caecilie if UNDATED_DEATH in e]
    assert caecilie.index(beside) == caecilie.index(dated) + 1
    assert site.cited("register", root="") in dated
    assert site.cited("certificate", root="") in beside

    def undated(key: str) -> list[str]:
        return [e for e in foot if site.links(key, root="") in e]

    (felix,) = undated("felix")
    assert visible(felix) == (
        f"{site.label('felix')}, {UNDATED_DEATH}{site.label('hypothetical')}"
    )
    assert undated("caecilie") == []
    assert not [e for e in entries if site.links("felix", root="") in e]

    # the letter's end of the union by Rudolf's death: his derived "died
    # by" among his rows, and the end itself under "No date stated", as
    # nothing dates his death; Nina is never shown to have died
    (derived,) = [e for e in entries_of(site, "rudolf") if "(derived)" in e]
    assert visible(derived).startswith(
        f"{day(LETTER_DATED.value)} — {site.label('rudolf')}, died by "
        f"{day(LETTER_DATED.value)} (derived)"
    )
    assert site.cited("letter", root="") in derived
    (ended,) = undated("rudolf")
    assert visible(ended) == (
        f"{site.label('marriage')}, ended by the death of "
        f"{site.label('rudolf')}{site.label('letter')}"
    )
    assert site.cited("letter", root="") in ended
    assert not [e for e in entries_of(site, "nina") if "died" in e]


def test_a_person_page_shows_each_death_as_its_source_states_it(site):
    # a death its source states with no date, beside the dated one, each
    # with its source, and never the query service's blank node; the
    # certificate that states it dates itself, so the "died by" its date
    # gives shows beside them, labelled as derived
    page = site.person("caecilie")
    deaths = described(page)["date of death"]
    (unknown,) = [e for e in deaths if visible(e).startswith(UNDATED_DEATH)]
    assert visible(unknown) == f"{UNDATED_DEATH}{site.label('certificate')}"
    assert site.cited("certificate") in unknown
    (dated,) = [e for e in deaths if visible(e).startswith(day(DIED.value))]
    assert visible(dated) == f"{day(DIED.value)}{site.label('register')}"
    assert site.cited("register") in dated
    (bound,) = [e for e in deaths if "(derived)" in e]
    assert visible(bound).startswith(
        f"died by {day(CERTIFICATE_DATED.value)} (derived)"
    )
    assert site.cited("certificate") in bound
    assert len(deaths) == 3
    assert "genid" not in page

    # a union ended by Rudolf's death: his page shows the end with the
    # letter, and the "died by" its date gives, labelled as derived; no
    # source states his death, and Nina's page never shows her to have
    # died
    rudolf = site.person("rudolf")
    (died,) = described(rudolf)["date of death"]
    assert visible(died).startswith(
        f"died by {day(LETTER_DATED.value)} (derived)"
    )
    assert site.cited("letter") in died
    assert UNDATED_DEATH not in rudolf
    nina = site.person("nina")
    assert "date of death" not in described(nina)
    for page in (rudolf, nina):
        (union,) = elements(page, "li")
        assert f"ended by the death of {site.label('rudolf')}" in visible(
            union
        )
        assert site.links("rudolf") in union
        assert site.cited("letter") in union

    # a curator's death event that states no date shows on its principal's
    # page alone, never an informant's or a witness's at it
    (event,) = described(site.person(taking_part("principal")))[
        "date of death"
    ]
    assert visible(event) == f"{UNDATED_DEATH}{site.label('hypothetical')}"
    for role in ("informant", "witness"):
        assert "date of death" not in described(site.person(taking_part(role)))

    # the run record counts the releases by cause over the whole stack, so
    # each count's floor is what this fixture adds: two deaths of unknown
    # date, an age at death, a union's end and the principals of two death
    # events; the roster case checks by the badge whom each releases
    assert site.summary["unknown_deaths"] >= 2
    derived = site.summary["derived_died_by"]
    assert derived["undated death"] >= 1
    assert derived["union end"] >= 1
    releases = site.summary["privacy"]["releases"]
    assert releases["unknown_date_of_death"] >= 2
    assert releases["age_at_death"] >= 1
    assert releases["ended_by_death_of"] >= 1
    assert releases["death_event"] >= 2


def test_an_artifact_page_lists_what_it_asserts_and_where(site):
    # every artifact has a page, which links its original file, and the run
    # record counts the fixture's among the stack's artifacts
    wiki = site.graph.client.config.wikibase_url
    assert site.summary["artifacts"] >= len(site.graph.sources)
    for key in site.graph.sources:
        file = urllib.parse.quote(media(key))
        assert f'href="{wiki}/wiki/Special:FilePath/{file}"' in (
            site.artifact(key)
        )

    # each birth Gustav's sources state is one statement, one row, with
    # the page and excerpt it cites
    for source, birth in BIRTHS.items():
        (row,) = [
            row
            for row in about(site, asserted(site, source), "gustav")
            if row[1] == "date of birth"
        ]
        assert row[2:] == [
            day(birth.value),
            place("1", birth.excerpt, language(source)),
        ]
    # a date with the range its source states, and where it says so
    (row,) = [
        row
        for row in about(site, asserted(site, "tree"), "rahel")
        if row[1] == "date of death"
    ]
    assert visible(row[2]) == AFTER
    assert row[3] == place("1", RAHEL_DIED.excerpt, "en")

    # a value the certificate gives on its first page and on its back is
    # one statement, one row, with both places
    places = sorted([("1", FRONT.excerpt), BACK])
    (row,) = [
        row
        for row in about(site, asserted(site, "certificate"), "sebastian")
        if row[1] == "date of birth"
    ]
    assert row[2:] == [
        day(FRONT.value),
        "<br>".join(place(page, text, "de") for page, text in places),
    ]
    # as the artifact-assertions query gives them: the statement once, a
    # row for each place
    concept = site.graph.client.config.concept_uri
    listed = [
        row
        for row in select(site.graph.client, "artifact-assertions")
        if value(row, "artifact") == site.graph.iri("certificate")
        and value(row, "entity") == site.graph.iri("sebastian")
        and value(row, "propertyLabel") == "date of birth"
    ]
    (statement,) = {value(row, "statement") for row in listed}
    assert statement.startswith(f"{concept}/entity/statement/")
    assert (
        sorted((value(row, "page"), value(row, "excerpt")) for row in listed)
        == places
    )


ITSELF = "this artifact"


def test_an_artifact_page_shows_what_sources_say_of_it(site):
    excluded = {
        key
        for key, qid in site.graph.qids.items()
        if qid in site.report.excluded
    }

    def person(entry: str, key: str) -> None:
        """`entry` names the person `key`, linked to their page and badged
        as their page is."""
        assert site.links(key) in entry
        assert (BADGE in entry) is (key in excluded)
        assert visible(entry).startswith(site.label(key))

    # the image the booklet places: the booklet's caption, with its page
    # and printed page, and what the image states of itself
    says = described(site.artifact("image"))
    assert sorted(says) == sorted(
        ["caption", "addressee", "inception", "location of creation"]
    )
    (caption,) = says["caption"]
    assert visible(caption) == (
        f"{CAPTION.value}{site.label('booklet')}, page "
        f"{dict(CAPTION.qualifiers)['page_number']} (printed {PRINTED})"
    )
    assert f'<q lang="en">{html.escape(CAPTION.value)}</q>' in caption
    assert site.cited("booklet") in caption
    (addressee,) = says["addressee"]
    person(addressee, "klara")
    assert visible(addressee).endswith(ITSELF)
    assert shown(says["inception"]) == [
        f"{day(IMAGE_SAYS['inception'].value)}{ITSELF}"
    ]
    assert shown(says["location of creation"]) == [
        f"{site.label('address')}{ITSELF}"
    ]

    # the portrait's studio, and every person it shows
    says = described(site.artifact("portrait"))
    assert sorted(says) == ["author", "shows"]
    assert shown(says["author"]) == [f"{site.label('studio')}{ITSELF}"]
    assert len(says["shows"]) == len(SHOWS)
    for key in SHOWS:
        (entry,) = [e for e in says["shows"] if site.links(key) in e]
        person(entry, key)
        assert visible(entry).endswith(ITSELF)

    # a date its artifact gives as circa: its year, read at the
    # statement's precision, and circa beside it
    says = described(site.artifact("rahels portrait"))
    assert sorted(says) == ["inception"]
    assert shown(says["inception"]) == [
        qualified(MADE.value.year, "sourcing_circumstances", CIRCA) + ITSELF
    ]

    # the caption is the image's, never the booklet's
    assert described(site.artifact("booklet")) == {}

    # each person page lists the artifacts that name the person
    itself = "the artifact itself"
    for key, (prop, artifact) in {
        "klara": ("addressee of", "image"),
        "rahel": ("shown in", "portrait"),
        "felix": ("shown in", "portrait"),
    }.items():
        (named_in,) = re.findall(
            r"<h2>Named in artifacts</h2>.*?<ul>(.*?)</ul>",
            site.person(key),
            flags=re.S,
        )
        (entry,) = elements(named_in, "li")
        assert visible(entry) == f"{prop} {site.label(artifact)}{itself}"
        assert site.cited(artifact) in entry


def test_the_places_page_labels_each_container_by_its_source(site):
    # the certificate, which states its own date, and the tree, which
    # states none, give Fläckfeld two containers: it stands under each,
    # the certificate's read as of the certificate's date and the tree's
    # labelled by the tree alone, and neither is picked; the misread
    # date's statement is deprecated, so the certificate is read as of its
    # one date
    tree = containers(site)
    label = site.label
    certificate = tree[label("mockburg"), label("flaeckfeld")]
    assert site.cited("certificate", root="") in certificate
    assert visible(certificate) == (
        f"{label('certificate')}, as of {day(CERTIFICATE_DATED.value)}"
    )
    family_tree = tree[label("peiltal"), label("flaeckfeld")]
    assert site.cited("tree", root="") in family_tree
    assert visible(family_tree) == label("tree")
    # the misreading's statement is deprecated, so it gives Kreis Peiltal
    # no container, and Kreis Peiltal, which contains Fläckfeld, heads the
    # tree
    assert (label("flaeckfeld"), label("peiltal")) not in tree
    assert label("peiltal") not in {place for _, place in tree}
    # the letter's statement carries a start time of its own: that time
    # shows, and the letter's date is not used
    district = tree[label("oblast"), label("district")]
    assert site.cited("letter", root="") in district
    assert visible(district) == (
        qualified(label("letter"), "start_time", DISTRICT_FROM)
    )
    # the extract, which states no date of its own, places the address in
    # the street and the street in the town
    for container, place in (("town", "street"), ("street", "address")):
        entry = tree[label(container), label(place)]
        assert site.cited("extract", root="") in entry
        assert visible(entry) == label("extract")


def test_the_gallery_and_the_disagreement_report_show_the_fixture(
    site, fictional_client
):
    # each photograph with its media file, and an embedded one with the
    # artifact it is part of
    concept = fictional_client.config.concept_uri
    figures = elements(site.read("gallery.html"), "figure")
    for key, container in (("gravestone", None), ("image", "booklet")):
        (figure,) = [
            figure
            for figure in figures
            if f'alt="{html.escape(site.label(key))}"' in figure
        ]
        image = f"{concept}/wiki/Special:FilePath/" + urllib.parse.quote(
            media(key), safe=""
        )
        assert f'<img src="{image}"' in figure
        if container:
            assert (
                f'<span class="source">{html.escape(site.label(container))}'
                "</span>"
            ) in figure
    assert site.summary["gallery_items"] >= 4

    # the two days are a genuine disagreement: it stays, each value with
    # its sources; Brunnenring 35 is recorded inside Flörburg, a
    # difference of resolution, which the report leaves out
    gustav = [
        row
        for row in elements(site.read("inconsistencies.html"), "tr")
        if row.startswith(f"<td>{html.escape(site.label('gustav'))}</td>")
    ]
    (births,) = [
        row
        for row in gustav
        if all(day(b.value) in row for b in BIRTHS.values())
    ]
    for source in BIRTHS:
        assert site.cited(source, root="") in births
    assert not [
        row for row in gustav if html.escape(site.label("address")) in row
    ]
    summary = site.summary
    by_property = summary["inconsistencies_by_property"]
    assert sum(by_property.values()) == summary["inconsistencies"] >= 1
    assert by_property["date_of_birth"] >= 1

    # ... and only the recorded containment leaves the pair out: the
    # containment query returns the extract's path, and the report's
    # statements compared without it list the two birthplaces
    client = fictional_client
    properties, _ = client.vocabulary()
    iri = site.graph.iri
    containment = read_containment(select(client, CONTAINMENT))
    assert place_within(containment, iri("address"), iri("town"))
    statements = read_statements(select(client, FETCH), properties)
    compared = {
        frozenset((row.a.value.term.value, row.b.value.term.value))
        for row in compare(statements, Containment())
        if row.entity == iri("gustav")
    }
    assert frozenset((iri("address"), iri("town"))) in compared
