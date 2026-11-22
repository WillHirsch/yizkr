"""The production queries' rows, read from the lane's real query service.

The views read the graph through the SPARQL templates in src/yizkr/queries
and the readers built on them: the disagreement report
(yizkr.disagreement), reconcile's roster (`fetch_roster`) and its place
containment (`fetch_place_containment`). These cases run them against the
lane's WDQS and check their rows for a fictional fixture
(docs/EXAMPLES.md).

The fixture is written once a stack, straight to the wiki through
WikibaseIntegrator, in the shapes an instance holds: a source's reading
carries a `stated in` block with its page and excerpt, and a machine
reading also the block of its source's extraction run; a curator's
statement carries the `stated in` block alone; an artifact's own kind cites
the artifact, and a page fragment's or an embedded photograph's kind and
`part of` cite its whole; a curator's rank is the rank the statement holds.
Legacy `spouse` statements and the curator-made birth and death events the
readers still read are written in the same way.

Every case but the last asserts about the fixture's own items alone, so
they share a stack with other required cases. The last asserts over its
whole stack, which holds the vocabulary and this fixture only, and runs in
a group of its own (scripts/fictional_integration.py).
"""

from __future__ import annotations

import time
import urllib.parse
from collections import defaultdict
from collections.abc import Callable

import pytest
from wikibaseintegrator.datatypes import (
    GlobeCoordinate,
    Item,
    MonolingualText,
    String,
    Time,
)
from wikibaseintegrator.models import Reference, References
from wikibaseintegrator.wbi_enums import (
    ActionIfExists,
    WikibaseRank,
    WikibaseSnakType,
)

from tests.fixtures import example_universe as u
from yizkr.claims import TimeValue
from yizkr.core import sparql
from yizkr.core.write_recovery import write_existing
from yizkr.disagreement import (
    CONTAINMENT,
    disagreements,
    place_within,
    read_containment,
)
from yizkr.pipeline.reconcile import (
    fetch_place_containment,
    fetch_roster,
    propose_graph_pairs,
)
from yizkr.queries.publish import load_templates, render_for_instance

pytestmark = pytest.mark.live
HANG_GUARD = 240
PREFERRED = WikibaseRank.PREFERRED
DEPRECATED = WikibaseRank.DEPRECATED
UNIVERSE = u.by_key()

Value = Callable[[str], object]


def time_value(
    year: int,
    month: int | None = None,
    day: int | None = None,
    precision: str | None = None,
) -> Value:
    """A time as the writer stores it (`TimeValue.to_wikibase`)."""
    stored = TimeValue(
        year=year, month=month, day=day, precision=precision
    ).to_wikibase()
    return lambda pid: Time(prop_nr=pid, **stored)


def text(value: str, language: str = "en") -> Value:
    return lambda pid: MonolingualText(
        prop_nr=pid, text=value, language=language
    )


def string(value: str) -> Value:
    return lambda pid: String(prop_nr=pid, value=value)


def unknown_time(pid: str):
    """A time its source states without giving it: an unknown value."""
    return Time(prop_nr=pid, snaktype=WikibaseSnakType.UNKNOWN_VALUE)


def coordinate(latitude: float, longitude: float) -> Value:
    return lambda pid: GlobeCoordinate(
        prop_nr=pid, latitude=latitude, longitude=longitude, precision=0.0001
    )


def between(start: Value, end: Value) -> tuple:
    """The earliest and latest dates a source gives a value."""
    return (("earliest_date", start), ("latest_date", end))


class Fixture:
    """The fixture's items: each made under its label, then given all its
    statements in one further write, so no statement is ever held without
    its references."""

    def __init__(self, client) -> None:
        self.client = client
        self.properties, self.items = client.vocabulary()
        self.qids: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self._entities: dict[str, object] = {}
        self._statements: defaultdict[str, list] = defaultdict(list)
        self.sources: list[str] = []
        self.run_keys: list[str] = []
        self._runs: dict[str, str] = {}
        self._whole: dict[str, str] = {}

    def make(self, key: str, label: str, scene: str, aliases=()) -> None:
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        # a label and a description are one item's alone
        item.descriptions.set("en", f"fictional {scene} fixture")
        if aliases:
            item.aliases.set("en", list(aliases))
        self._entities[key] = item.write(
            summary="fictional production-query fixture"
        )
        self.qids[key] = self._entities[key].id
        self.labels[key] = label

    def source(self, key: str, title: str, kind: str, whole=None) -> None:
        """An artifact whose own kind cites itself, or a part of `whole`
        (a page fragment, an embedded photograph) whose kind and `part of`
        cite its whole, as the registrar writes them."""
        self.make(key, title, "source")
        self.sources.append(key)
        self.state(key, "instance_of", self.vocab(kind), whole or key)
        if whole:
            self._whole[key] = whole
            self.state(key, "part_of", self.item(whole), whole)

    def entity(
        self, key, label, scene, kind, *sources, aliases=(), kind_rank=None
    ) -> None:
        """An item and the kind a machine reading of `sources` gives it."""
        self.make(key, label, scene, aliases)
        self.state(
            key,
            "instance_of",
            self.vocab(kind),
            *sources,
            run=True,
            rank=kind_rank,
        )

    def item(self, key: str) -> Value:
        qid = self.qids[key]
        return lambda pid: Item(prop_nr=pid, value=qid)

    def vocab(self, key: str) -> Value:
        qid = self.items[key]
        return lambda pid: Item(prop_nr=pid, value=qid)

    def state(
        self,
        subject: str,
        prop: str,
        value: Value,
        *sources: str,
        run: bool = False,
        run_only: str | None = None,
        page: str | None = None,
        excerpt: str | None = None,
        language: str = "en",
        qualifiers: tuple = (),
        rank: WikibaseRank | None = None,
    ):
        """One statement of `subject`, stated in each of `sources`, each
        block with `page` and `excerpt`, and, with `run`, each followed by
        the block of its source's extraction run. `run_only` names the
        source whose run alone the statement cites. Returns the statement,
        which `write` sends."""
        properties = self.properties
        references = References()
        for source in sources:
            block = Reference()
            block.add(
                Item(prop_nr=properties["stated_in"], value=self.qids[source])
            )
            if page:
                block.add(
                    String(prop_nr=properties["page_number"], value=page)
                )
            if excerpt:
                block.add(
                    MonolingualText(
                        prop_nr=properties["transcription_excerpt"],
                        text=excerpt,
                        language=language,
                    )
                )
            references.add(block)
            if run:
                references.add(self._run_block(source))
        if run_only:
            references.add(self._run_block(run_only))
        claim = value(properties[prop])
        claim.references = references
        for key, qualifier in qualifiers:
            claim.qualifiers.add(qualifier(properties[key]))
        if rank is not None:
            claim.rank = rank
        self._statements[subject].append(claim)
        return claim

    def reading(self, subject, prop, value, *sources, **options):
        """A machine reading of page 1 of each of `sources`."""
        return self.state(
            subject, prop, value, *sources, run=True, page="1", **options
        )

    def curated(self, subject, prop, value, *sources, **options):
        """A curator's statement citing page 1 of each of `sources`."""
        return self.state(subject, prop, value, *sources, page="1", **options)

    def cite(self, claim, source: str) -> None:
        """A curator's further `stated in` block on `claim`."""
        block = Reference()
        block.add(
            Item(prop_nr=self.properties["stated_in"], value=self.qids[source])
        )
        claim.references.add(block)

    def _run_block(self, source: str) -> Reference:
        whole = self._whole.get(source, source)
        if whole not in self._runs:
            key = f"run of {whole}"
            self.make(key, f"Extraction run over {self.labels[whole]}", "run")
            self.run_keys.append(key)
            self.state(key, "instance_of", self.vocab("extraction_run"), key)
            self._runs[whole] = self.qids[key]
        block = Reference()
        block.add(
            Item(
                prop_nr=self.properties["inferred_from_extraction_run"],
                value=self._runs[whole],
            )
        )
        return block

    def write(self) -> None:
        for key, statements in self._statements.items():
            entity = self._entities[key]
            for statement in statements:
                entity.claims.add(
                    statement, action_if_exists=ActionIfExists.FORCE_APPEND
                )
            self._entities[key] = write_existing(
                entity, summary="fictional production-query fixture"
            )
        self._statements.clear()

    def key_of(self, iri_or_qid: str) -> str:
        """The fixture key of an item, given by its IRI or QID."""
        qid = iri_or_qid.rsplit("/", 1)[-1]
        found = [key for key, value in self.qids.items() if value == qid]
        return found[0] if found else qid

    def iri(self, key: str) -> str:
        return f"{self.client.config.concept_uri}/entity/{self.qids[key]}"


# -- the sources ---------------------------------------------------------

SOURCES = {
    "A": ("Query fixture register extract A", "certificate"),
    "B": ("Query fixture certificate B", "certificate"),
    "C": ("Query fixture certificate C", "certificate"),
    "D": ("Query fixture letter D", "letter"),
}
UNIVERSE_SOURCES = {
    "card": "registration-card-1945",
    "burial": "burial-register-1909",
    "tree": "family-tree-1984",
    "marriage": "marriage-cert-1905",
    "interview": "interview-2002",
    "directory": "directory-1912",
    "naturalisation": "naturalisation-1952",
    "booklet": "family-booklet-2011",
    "studio": "studio-photo-1890s",
}


def write_sources(f: Fixture) -> None:
    for key, (title, kind) in SOURCES.items():
        f.source(key, title, kind)
    f.source(
        "A, page 1", f"{SOURCES['A'][0]}, page 1", "artifact_fragment", "A"
    )
    for key, universe in UNIVERSE_SOURCES.items():
        source = UNIVERSE[universe]
        f.source(key, source.title, source.artifact_class or "artifact")


# -- the disagreement report (ADR-0002, ADR-0023 §4, ADR-0033) ------------
#
# A contradiction between two artifacts is listed once, each value with
# the artifacts that state it and its stated precision, even when a
# curator prefers one of them. Two readings of one artifact are no
# disagreement: a curator's correction beside the machine reading it
# corrects, and a page fragment beside its own artifact. A deprecated value
# is left out. A date whose span lies inside another's is a refinement: the
# span is the period its precision names or the range its source states,
# and a stated range can also set two dates stored alike apart. An event's
# own date is compared, and so are the start times of one residence place
# and of one spouse; two residence observations at different times are
# not. Birth names compare as text, by case-folding without their language;
# nicknames accumulate and are never compared. A document's date of making
# is single-valued.

REPORT = {
    "person": ("Disagreement Fixture Person", "human"),
    "spouse": ("Disagreement Fixture Spouse", "human"),
    "ranges": ("Disagreement Fixture Ranges", "human"),
    "same year": ("Disagreement Fixture Same Year", "human"),
    "emigration": ("Disagreement Fixture Emigration", "migration"),
    "second emigration": (
        "Disagreement Fixture Second Emigration",
        "migration",
    ),
    "town": ("Disagreement Fixture Town", "place"),
    "other town": ("Disagreement Fixture Other Town", "place"),
}
REPORT_SUBJECTS = (
    "person",
    "spouse",
    "ranges",
    "same year",
    "emigration",
    "second emigration",
    "D",
)


def write_report(f: Fixture) -> None:
    for key, (label, kind) in REPORT.items():
        f.entity(key, label, "disagreement report", kind, "A")
    read = f.reading
    # the universe's contradiction: day and month transposed, and a bare
    # year that refines both; a curator prefers the register's day
    read(
        "person", "date_of_birth", time_value(1919, 1, 7), "A", rank=PREFERRED
    )
    read("person", "date_of_birth", time_value(1919, 7, 1), "B")
    read("person", "date_of_birth", time_value(1919), "C")
    # the machine's reading, and a curator's correction from the same
    # artifact
    read("person", "date_of_death", time_value(1930), "A")
    f.curated("person", "date_of_death", time_value(1931), "A")
    # a page of an artifact beside the artifact itself
    read("person", "place_of_death", f.item("town"), "A, page 1")
    read("person", "place_of_death", f.item("other town"), "A")
    # the second value is deprecated by a curator
    read("person", "place_of_birth", f.item("town"), "A")
    read(
        "person", "place_of_birth", f.item("other town"), "B", rank=DEPRECATED
    )
    # arrived there in 1941 per one source, in 1942 per another
    read(
        "person",
        "residence",
        f.item("town"),
        "A",
        qualifiers=(("start_time", time_value(1941)),),
    )
    read(
        "person",
        "residence",
        f.item("town"),
        "B",
        qualifiers=(
            ("start_time", time_value(1942)),
            ("end_time", time_value(1952)),
        ),
    )
    # lived there in 1912 and in 1919: two observations
    for source, year in (("A", 1912), ("B", 1919)):
        read(
            "person",
            "residence",
            f.item("other town"),
            source,
            qualifiers=(("point_in_time", time_value(year)),),
        )
    # one birth name, written in two cases and languages
    read("person", "birth_name", text("Fixture Birth Name"), "A")
    read("person", "birth_name", text("FIXTURE BIRTH NAME", "de"), "B")
    # nicknames accumulate
    read("person", "nickname", text("Fixture Nick One"), "A")
    read("person", "nickname", text("Fixture Nick Two"), "B")
    # two birth names, the first in two spellings
    read("spouse", "birth_name", text("Fixture Spouse Birth Name"), "A")
    read("spouse", "birth_name", text("FIXTURE SPOUSE BIRTH NAME", "de"), "C")
    read("spouse", "birth_name", text("Fixture Spouse Maiden Name"), "B")
    # the marriage dated two ways on legacy spouse statements
    for source, start in (
        ("A", time_value(1947, 2, 8)),
        ("B", time_value(1946)),
    ):
        read(
            "spouse",
            "spouse",
            f.item("person"),
            source,
            qualifiers=(("start_time", start),),
        )
    # a stated range narrows a decade: 1899 lies outside it
    read(
        "ranges",
        "date_of_birth",
        time_value(1890, precision="decade"),
        "A",
        qualifiers=between(time_value(1890), time_value(1890)),
    )
    read("ranges", "date_of_birth", time_value(1899), "B")
    # and a year: 1 October lies outside August to September
    read(
        "ranges",
        "date_of_death",
        time_value(1939),
        "A",
        qualifiers=between(time_value(1939, 8), time_value(1939, 9)),
    )
    read("ranges", "date_of_death", time_value(1939, 10, 1), "B")
    # stored alike, set apart by a stated range: 1890 per two sources, one
    # statement citing both, and "the 1890s, between 1899 and 1899" per a
    # third
    read("same year", "date_of_birth", time_value(1890), "A", "C")
    read(
        "same year",
        "date_of_birth",
        time_value(1890, precision="decade"),
        "B",
        qualifiers=between(time_value(1899), time_value(1899)),
    )
    # and "before 1945" beside "after 1945"
    read(
        "same year",
        "date_of_death",
        time_value(1945),
        "A",
        qualifiers=(("latest_date", time_value(1945)),),
    )
    read(
        "same year",
        "date_of_death",
        time_value(1945),
        "B",
        qualifiers=(("earliest_date", time_value(1945)),),
    )
    # an event's own date: 22 June per one source and 19 August per
    # another, while "between 1938 and 1939" refines both
    read("emigration", "point_in_time", time_value(1938, 6, 22), "B")
    read(
        "emigration",
        "point_in_time",
        time_value(1938),
        "C",
        qualifiers=between(time_value(1938), time_value(1939)),
    )
    read("emigration", "point_in_time", time_value(1938, 8, 19), "A")
    # a day in the stated range's second year refines it too
    read(
        "second emigration",
        "point_in_time",
        time_value(1938),
        "C",
        qualifiers=between(time_value(1938), time_value(1939)),
    )
    read("second emigration", "point_in_time", time_value(1939, 9, 3), "A")
    # a century is a span: 1863 lies inside the 19th, given by its first
    # year
    read(
        "second emigration",
        "start_time",
        time_value(1801, precision="century"),
        "A",
    )
    read("second emigration", "start_time", time_value(1863), "B")
    # the letter's own dateline, 28 July 1939; 1940 per a curator citing
    # B, which disagrees with it, and July 1939 per one citing C, which
    # holds it and is listed only beside 1940
    read("D", "inception", time_value(1939, 7, 28), "D")
    f.curated("D", "inception", time_value(1940), "B")
    f.curated("D", "inception", time_value(1939, 7), "C")


# -- a century, and the two ends of a period -------------------------------


def write_century_and_period(f: Fixture) -> None:
    f.entity(
        "century person", "Century Fixture Person", "century", "human", "A"
    )
    # the 19th century per A, given by its first year; its last year per
    # B; the 18th century's last year per C
    f.reading(
        "century person",
        "date_of_birth",
        time_value(1801, precision="century"),
        "A",
    )
    f.reading("century person", "date_of_birth", time_value(1900), "B")
    f.reading("century person", "date_of_birth", time_value(1800), "C")

    for key, (label, kind) in {
        "period person": ("Period Fixture Person", "human"),
        "period spouse": ("Period Fixture Spouse", "human"),
        "period town": ("Period Fixture Town", "place"),
    }.items():
        f.entity(key, label, "period", kind, "A")
    married = (
        # per A: married between 1946 and 1947, the marriage over between
        # 1956 and 1957, and observed in 1950
        (
            "A",
            ("start_time", time_value(1946)),
            ("earliest_date", time_value(1946)),
            ("latest_start_date", time_value(1947)),
            ("end_time", time_value(1956)),
            ("earliest_end_date", time_value(1956)),
            ("latest_date", time_value(1957)),
            ("point_in_time", time_value(1950)),
        ),
        # per B: 1948 to 1955, outside both of A's ranges
        (
            "B",
            ("start_time", time_value(1948)),
            ("end_time", time_value(1955)),
        ),
        # per C: 8 February 1947 to 2 January 1957, inside them
        (
            "C",
            ("start_time", time_value(1947, 2, 8)),
            ("end_time", time_value(1957, 1, 2)),
        ),
    )
    for source, *qualifiers in married:
        f.reading(
            "period person",
            "spouse",
            f.item("period spouse"),
            source,
            qualifiers=tuple(qualifiers),
        )
    # observed in the town sometime between 1938 and 1939
    f.reading(
        "period person",
        "residence",
        f.item("period town"),
        "A",
        qualifiers=(
            ("point_in_time", time_value(1938)),
            *between(time_value(1938), time_value(1939)),
        ),
    )


# -- containment as a source's claim (ADR-0038 §2, §5) --------------------
#
# The universe's Koshnovka, Kaldovsky District and Chadsk Oblast: the
# hamlet in the district in 1941 per the letter and undated per the
# certificate, and the district in the oblast from 1937, the only link to
# the oblast. A curator deprecated the hamlet's containment in a decoy, and
# one in a second decoy cites the letter's run alone, as the place stage
# writes one. Three people are born in the hamlet per the letter and in the
# oblast, the decoy or the second decoy per the certificate.

CONTAINMENT_ITEMS = {
    "hamlet": ("Koshnovka", "place"),
    "district": ("Kaldovsky District", "place"),
    "oblast": ("Chadsk Oblast", "place"),
    "decoy": ("Decoy District", "place"),
    "unstated": ("Second Decoy District", "place"),
    "refined": ("Rudolf Löbig", "human"),
    "listed": ("Nina Sartina", "human"),
    "run only": ("Pavel Sartin", "human"),
}


def write_containment(f: Fixture) -> None:
    for key, (label, kind) in CONTAINMENT_ITEMS.items():
        f.entity(key, label, "place-containment", kind, "D")
    f.reading(
        "hamlet",
        "part_of",
        f.item("district"),
        "D",
        excerpt="in 1941 Koshnovka belonged to Kaldovsky District",
        qualifiers=(
            ("point_in_time", time_value(1941)),
            *between(time_value(1940), time_value(1941)),
        ),
    )
    f.reading(
        "hamlet",
        "part_of",
        f.item("district"),
        "A",
        excerpt="Koshnovka, Kaldovsky District",
    )
    f.reading("hamlet", "part_of", f.item("decoy"), "D", rank=DEPRECATED)
    f.state("hamlet", "part_of", f.item("unstated"), run_only="D")
    f.reading(
        "district",
        "part_of",
        f.item("oblast"),
        "D",
        excerpt="Kaldovsky District, in Chadsk Oblast from 1937",
        qualifiers=(("start_time", time_value(1937)),),
    )
    for person, place in (
        ("refined", "oblast"),
        ("listed", "decoy"),
        ("run only", "unstated"),
    ):
        f.reading(person, "place_of_birth", f.item("hamlet"), "D")
        f.reading(person, "place_of_birth", f.item(place), "A")


# -- the person timeline -------------------------------------------------
#
# Leopold Löbig's imprisonment, dated by its point in time and its own
# start and end time, typed an imprisonment, an event a curator prefers and
# a military service a curator deprecated, with three participants:
# Leopold, whom a curator prefers, Ida, and Felix, whom a curator
# deprecated. Eugen Löbig's death is an event a curator made, with Eugen
# its principal and Sebastian a witness: it dates Eugen's death on his
# timeline alone.


def write_timeline(f: Fixture) -> None:
    scene = "person-timeline"
    for key in ("leopold", "ida", "felix"):
        f.entity(key, UNIVERSE[f"{key}-loebig"].label, scene, "human", "card")
    f.entity(
        "held",
        UNIVERSE["imprisonment-1939"].label,
        scene,
        "imprisonment",
        "card",
    )
    f.reading("held", "instance_of", f.vocab("event"), "card", rank=PREFERRED)
    f.reading(
        "held",
        "instance_of",
        f.vocab("military_service"),
        "card",
        rank=DEPRECATED,
    )
    for who, rank in (
        ("leopold", PREFERRED),
        ("ida", None),
        ("felix", DEPRECATED),
    ):
        f.reading("held", "participant", f.item(who), "card", rank=rank)
    for prop, value in (
        ("point_in_time", time_value(1939, 9)),
        ("start_time", time_value(1939, 9, 3)),
        ("end_time", time_value(1939, 10, 1)),
    ):
        f.reading("held", prop, value, "card")

    for key in ("eugen", "sebastian"):
        f.entity(
            key, UNIVERSE[f"{key}-loebig"].label, scene, "human", "burial"
        )
    f.make("death", "Death of Eugen Löbig", scene)
    f.curated("death", "instance_of", f.vocab("death"), "burial")
    for who, role in (("eugen", "principal"), ("sebastian", "witness")):
        f.curated(
            "death",
            "participant",
            f.item(who),
            "burial",
            qualifiers=(("object_has_role", f.vocab(role)),),
        )
    f.curated("death", "point_in_time", time_value(1909), "burial")


# -- kinship (ADR-0014, ADR-0018) -----------------------------------------
#
# A slice of the universe's family, each relation stated on one side only,
# some with a curator's rank: Sebastian's and Felix's father is Eugen, so
# the two are brothers through a shared parent, and Felix is Leopold's
# father; Sebastian's children Klara (preferred) and Gustav are stated on
# him, as `child`; his spouse Hildegard on him only, and Ursula's sibling
# Klara on Ursula only. The interview's "Aunt Pünktchen" is Klara
# —relative→ Nicholas with the term "aunt"; ADR-0018's "our great
# grandmother", with a side, is Cäcilie —relative→ Nicholas; the 1902
# register's "seine Halbschwester Cäcilie" is Cäcilie —sibling→ Christian
# with the term "Halbschwester". Two statements a curator deprecated give
# no row: Felix misread as Klara's father, and the courtesy title "Nana
# Maisie" misread as kinship.

KIN = (
    "eugen-loebig",
    "sebastian-loebig",
    "felix-loebig",
    "hildegard-loebig",
    "ursula-loebig",
    "klara-loebig",
    "gustav-loebig",
    "leopold-loebig",
    "nicholas-lobig",
    "maisie-cutmore",
    "caecilie-loebig",
    "christian-tarle",
)
# (subject, property, value, kinship term, excerpt, its language, rank)
KINSHIP = (
    ("sebastian-loebig", "father", "eugen-loebig", None, None, None, None),
    ("felix-loebig", "father", "eugen-loebig", None, None, None, None),
    ("sebastian-loebig", "child", "klara-loebig", None, None, None, PREFERRED),
    ("sebastian-loebig", "child", "gustav-loebig", None, None, None, None),
    ("leopold-loebig", "father", "felix-loebig", None, None, None, None),
    ("sebastian-loebig", "spouse", "hildegard-loebig", None, None, None, None),
    ("ursula-loebig", "sibling", "klara-loebig", None, None, None, None),
    (
        "klara-loebig",
        "relative",
        "nicholas-lobig",
        "aunt",
        "Aunt Pünktchen",
        "en",
        None,
    ),
    (
        "caecilie-loebig",
        "relative",
        "nicholas-lobig",
        "paternal great grandmother",
        None,
        None,
        None,
    ),
    (
        "maisie-cutmore",
        "relative",
        "nicholas-lobig",
        "Nana",
        "Nana Maisie",
        "en",
        DEPRECATED,
    ),
    ("klara-loebig", "father", "felix-loebig", None, None, None, DEPRECATED),
    (
        "caecilie-loebig",
        "sibling",
        "christian-tarle",
        "Halbschwester",
        "seine Halbschwester Cäcilie",
        "de",
        None,
    ),
)
RELATIVES = {
    ("eugen", "sebastian", "son"),
    ("eugen", "felix", "son"),
    ("eugen", "klara", "granddaughter"),
    ("eugen", "gustav", "grandson"),
    ("eugen", "leopold", "grandson"),
    ("eugen", "hildegard", "daughter-in-law"),
    ("sebastian", "eugen", "father"),
    ("sebastian", "klara", "daughter"),
    ("sebastian", "gustav", "son"),
    ("sebastian", "felix", "brother"),
    ("sebastian", "hildegard", "wife"),
    ("sebastian", "leopold", "nephew"),
    ("felix", "eugen", "father"),
    ("felix", "leopold", "son"),
    ("felix", "sebastian", "brother"),
    ("felix", "klara", "niece"),
    ("felix", "gustav", "nephew"),
    ("felix", "hildegard", "sister-in-law"),
    ("hildegard", "sebastian", "husband"),
    ("hildegard", "eugen", "father-in-law"),
    ("hildegard", "felix", "brother-in-law"),
    # her husband's brother's son
    ("hildegard", "leopold", "nephew"),
    ("klara", "sebastian", "father"),
    ("klara", "gustav", "brother"),
    ("klara", "ursula", "sister"),
    ("klara", "eugen", "grandfather"),
    ("klara", "felix", "uncle"),
    ("klara", "leopold", "cousin"),
    # the inverse of the aunt her nephew's interview names
    ("klara", "nicholas", "nephew"),
    ("gustav", "sebastian", "father"),
    ("gustav", "klara", "sister"),
    ("gustav", "eugen", "grandfather"),
    ("gustav", "felix", "uncle"),
    ("gustav", "leopold", "cousin"),
    ("ursula", "klara", "sister"),
    ("leopold", "felix", "father"),
    ("leopold", "eugen", "grandfather"),
    ("leopold", "sebastian", "uncle"),
    # his uncle's wife
    ("leopold", "hildegard", "aunt"),
    ("leopold", "klara", "cousin"),
    ("leopold", "gustav", "cousin"),
    ("nicholas", "klara", "aunt"),
    # a term a source asserts is shown as the source gives it
    ("nicholas", "caecilie", "paternal great grandmother"),
    # its inverse drops the side, which names Nicholas's parent, and keeps
    # the degree
    ("caecilie", "nicholas", "great-grandson"),
    # a half sibling: a sibling either way, and the half term the register
    # asserts, shown as it gives it, with its inverse
    ("christian", "caecilie", "sister"),
    ("caecilie", "christian", "brother"),
    ("christian", "caecilie", "Halbschwester"),
    ("caecilie", "christian", "half-brother"),
}


def kin(key: str) -> str:
    return f"kin {key}"


def write_kinship(f: Fixture) -> None:
    for key in KIN:
        person = UNIVERSE[key]
        f.entity(kin(key), person.label, "kinship", "human", "tree")
        f.reading(kin(key), "sex_or_gender", f.vocab(person.sex), "tree")
    for subject, prop, value, term, excerpt, language, rank in KINSHIP:
        f.reading(
            kin(subject),
            prop,
            f.item(kin(value)),
            "tree",
            excerpt=excerpt,
            language=language or "en",
            qualifiers=(("kinship_type", string(term)),) if term else (),
            rank=rank,
        )


# -- the review queues (ADR-0013 §A, ADR-0023 §5) -------------------------
#
# The under-witnessed queue lists a machine statement with one stated-in
# source and no curator sibling, even beside a coexisting machine value
# from another source; an agreeing second source takes a statement out,
# and so does a curator's statement on the same subject and property,
# which is never listed itself. Two same-value statements that differ by a
# qualifier are two rows, and a time keeps its stated precision. In the
# universe's slice, the 1905 marriage certificate's reading types
# Sebastian Löbig a human, which the 1912 directory's reading witnesses
# again, and the interview holds the family firm unclassified with its
# own word for it, "hat shop", which the directory types an organization.
#
# The unclassified-entities query holds one row per type hint in lower
# case: the firm and two more entities with that hint in two spellings,
# two from the interview and one from the naturalisation certificate, are
# one row; a hint that two artifacts state, one by a curator's reference,
# is one entity and two artifacts; a deprecated hint is not counted under
# it, an entity whose unclassified kind is deprecated is not listed, and an
# entity held without a hint is counted under the empty hint.

FIRM = UNIVERSE["zum-blauen-hut"].label
HAT_SHOP = "hat shop"  # the interview's own word for the firm
# local key -> (label, type hint, source)
UNCLASSIFIED = {
    "parlour": ("Unclassified Hat Parlour", "Hat Shop", "interview"),
    "stall": ("Unclassified Hat Stall", HAT_SHOP, "naturalisation"),
    "craft": ("Unclassified Craft", "aircraft", "interview"),
    "liner": ("Unclassified Liner", "ocean liner", "naturalisation"),
    "airship": ("Unclassified Airship", "Airship", "naturalisation"),
    "unhinted": ("Unclassified Unhinted", None, "interview"),
}


def says(subject: str, source: str, prop: str) -> u.Assertion:
    (assertion,) = [a for a in u.about(subject, prop) if a.source == source]
    return assertion


def write_queues(f: Fixture) -> None:
    scene = "review-queue"
    f.entity(
        "queue subject", "Queue Fixture Subject", scene, "human", "A", "B"
    )
    f.entity("queue town", "Queue Fixture Town", scene, "place", "A")
    read = f.reading
    # a coexisting machine value from another source
    read("queue subject", "date_of_birth", time_value(1911), "A")
    read("queue subject", "date_of_birth", time_value(1912), "B")
    # an agreeing second source
    read("queue subject", "date_of_death", time_value(1970), "A", "B")
    # a curator's sibling beside the machine nickname
    read("queue subject", "nickname", text("Fixie"), "A")
    f.curated("queue subject", "nickname", text("Fixy"), "A")
    read("queue subject", "married_name", text("Queue Fixture Wedded"), "A")
    # a curator-only property
    f.curated("queue subject", "birth_name", text("Queue Fixture Born"), "A")
    # same value, different qualifier: two distinct assertions
    for year in (1930, 1940):
        read(
            "queue subject",
            "residence",
            f.item("queue town"),
            "A",
            qualifiers=(("start_time", time_value(year)),),
        )

    occupation = says("sebastian-loebig", "marriage-cert-1905", "occupation")
    name = says("zum-blauen-hut", "interview-2002", "referred_to_as")
    founded = says("zum-blauen-hut", "directory-1912", "start_time")
    owner = says("sebastian-loebig", "directory-1912", "employer")
    entry = "Hutmacherei zum Blauen Hut, gegr. 1863, Inh. Sebastian Löbig"
    assert HAT_SHOP in UNIVERSE["interview-2002"].text
    assert entry in UNIVERSE["directory-1912"].text
    f.entity(
        "witnessed",
        UNIVERSE["sebastian-loebig"].label,
        scene,
        "human",
        "marriage",
        "directory",
    )
    f.entity(
        "hatter",
        UNIVERSE["hatter"].label,
        scene,
        "occupation_class",
        "marriage",
    )
    f.entity("firm", FIRM, scene, "unclassified_entity", "interview")
    read(
        "witnessed",
        "occupation",
        f.item("hatter"),
        "marriage",
        excerpt=occupation.excerpt,
        language="de",
    )
    read("firm", "type_hint", string(HAT_SHOP), "interview")
    read(
        "firm",
        "referred_to_as",
        text(name.value),
        "interview",
        excerpt=name.excerpt,
    )
    read(
        "firm",
        "instance_of",
        f.vocab("organization"),
        "directory",
        excerpt=entry,
        language="de",
    )
    read(
        "firm",
        "start_time",
        time_value(founded.value.year),
        "directory",
        excerpt=founded.excerpt,
        language="de",
    )
    owned_in = dict(owner.qualifiers)["point_in_time"]
    read(
        "witnessed",
        "employer",
        f.item("firm"),
        "directory",
        excerpt=owner.excerpt,
        language="de",
        qualifiers=(("point_in_time", time_value(owned_in.year)),),
    )

    for key, (label, hint, source) in UNCLASSIFIED.items():
        # a curator deprecated the liner's unclassified kind
        f.entity(
            key,
            label,
            "unclassified-entities",
            "unclassified_entity",
            source,
            kind_rank=DEPRECATED if key == "liner" else None,
        )
        read(key, "referred_to_as", text(label), source)
        if hint:
            # a curator deprecated the craft's hint
            rank = DEPRECATED if key == "craft" else None
            stated = read(key, "type_hint", string(hint), source, rank=rank)
            if key == "airship":
                # a curator who reads the hint in the interview too
                f.cite(stated, "interview")


# -- reconcile's roster and containment (ADR-0017, ADR-0018, ADR-0040) ----
#
# An elder, born in the 19th century and dead in the 1890s, and the
# younger whose father he is, born in the 1850s and dead in 1938 by events
# a curator made, the younger their principal; a brother whose death its
# source states without its date; a town part of a country; and two
# grandmothers, each aliased "Oma" beside a name form of her own, with a
# `referred_to_as` statement giving the appellation.

APPELLATION = "Oma"
RECONCILE = {
    "elder": ("Reconcile Fixture Elder", "human", ()),
    "younger": ("Reconcile Fixture Younger", "human", ()),
    "brother": ("Reconcile Fixture Brother", "human", ()),
    "reconcile town": ("Reconcile Fixture Town", "place", ()),
    "reconcile country": ("Reconcile Fixture Country", "place", ()),
    "grandmother a": (
        "Reconcile Fixture Grandmother A",
        "human",
        (APPELLATION, "Hilde"),
    ),
    "grandmother b": (
        "Reconcile Fixture Grandmother B",
        "human",
        (APPELLATION, "Sipkje"),
    ),
}


def write_reconcile(f: Fixture) -> None:
    for key, (label, kind, aliases) in RECONCILE.items():
        f.entity(key, label, "reconcile", kind, "D", aliases=aliases)
    read = f.reading
    read("younger", "father", f.item("elder"), "D", excerpt="son of the elder")
    read("reconcile town", "part_of", f.item("reconcile country"), "D")
    read(
        "elder",
        "date_of_birth",
        time_value(1801, precision="century"),
        "D",
        excerpt="born last century",
    )
    read(
        "elder",
        "date_of_death",
        time_value(1890, precision="decade"),
        "D",
        excerpt="died in the nineties",
    )
    read(
        "brother",
        "date_of_death",
        unknown_time,
        "D",
        excerpt="his late brother",
    )
    for key in ("grandmother a", "grandmother b"):
        read(
            key,
            "referred_to_as",
            text(APPELLATION, "de"),
            "D",
            excerpt=f"{APPELLATION} schreibt",
        )
    for key, kind, label, when in (
        (
            "birth",
            "birth",
            "Birth of the Reconcile Fixture Younger",
            time_value(1850, precision="decade"),
        ),
        (
            "reconcile death",
            "death",
            "Death of the Reconcile Fixture Younger",
            time_value(1938),
        ),
    ):
        f.make(key, label, "reconcile")
        f.curated(key, "instance_of", f.vocab(kind), "D")
        f.curated(
            key,
            "participant",
            f.item("younger"),
            "D",
            qualifiers=(("object_has_role", f.vocab("principal")),),
        )
        f.curated(key, "point_in_time", when, "D")


# -- the gallery and the map (R6) -----------------------------------------
#
# A photograph the family booklet embeds and a studio portrait held on its
# own, each with its media file; Flörburg, with coordinates, where Klara
# was born and lived and whence she emigrated, and Moshausen, with
# coordinates and nothing connected to it.

BOOKLET_IMAGE = UNIVERSE["letter-excerpt-1939"].title
MEDIA = {
    "booklet image": "Our Family from Germany page 6 image 1.png",
    "studio": "Studio portrait of the Löbig family.png",
}
COORDINATES = {"floerburg": (50.3311, 9.2631), "moshausen": (50.7017, 8.4122)}


def write_gallery_and_map(f: Fixture) -> None:
    f.source("booklet image", BOOKLET_IMAGE, "photograph", "booklet")
    for key, whole in (("booklet image", "booklet"), ("studio", "studio")):
        f.state(key, "media_file", string(f"File:{MEDIA[key]}"), whole)
    scene = "places-map"
    for key, (latitude, longitude) in COORDINATES.items():
        f.entity(key, UNIVERSE[key].label, scene, "place", "A")
        f.reading(
            key, "coordinate_location", coordinate(latitude, longitude), "A"
        )
    f.entity("klara", UNIVERSE["klara-loebig"].label, scene, "human", "A")
    f.entity(
        "klara emigrates",
        UNIVERSE["emigration-1938"].label,
        scene,
        "migration",
        "A",
    )
    f.reading("klara", "place_of_birth", f.item("floerburg"), "A")
    f.reading("klara", "residence", f.item("floerburg"), "A")
    f.reading("klara emigrates", "location", f.item("floerburg"), "A")


def write_fixture(client) -> Fixture:
    f = Fixture(client)
    write_sources(f)
    write_report(f)
    write_century_and_period(f)
    write_containment(f)
    write_timeline(f)
    write_kinship(f)
    write_queues(f)
    write_reconcile(f)
    write_gallery_and_map(f)
    f.write()
    return f


def synchronize(client) -> None:
    """Wait until WDQS holds every entity's current API revision, the
    query vocabulary's included."""
    wanted = client.entity_revisions(client.list_entity_ids())
    entity = client.config.concept_uri + "/entity/"
    query = (
        "SELECT ?entity ?version WHERE "
        "{ ?entity <http://schema.org/version> ?version . }"
    )
    deadline = time.monotonic() + HANG_GUARD
    while True:
        seen = {
            row["entity"]["value"].removeprefix(entity): int(
                row["version"]["value"]
            )
            for row in sparql.select(client.config.sparql_url, query)
        }
        stale = [
            qid
            for qid, revision in wanted.items()
            if seen.get(qid, -1) < revision
        ]
        if not stale:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{len(stale)} entities have not reached their API revision "
                f"in WDQS after {HANG_GUARD}s; check the fictional "
                "wdqs-updater logs"
            )
        time.sleep(2)


@pytest.fixture(scope="module")
def graph(fictional_client) -> Fixture:
    fixture = write_fixture(fictional_client)
    synchronize(fictional_client)
    return fixture


def run(client) -> Callable[[str], list[dict]]:
    return lambda query: sparql.select(client.config.sparql_url, query)


def select(client, template: str) -> list[dict]:
    """The rows of a query template, rendered for the instance."""
    properties, items = client.vocabulary()
    return run(client)(
        render_for_instance(
            template, properties, items, client.config.concept_uri
        )
    )


def rows_of(client, name: str) -> list[dict]:
    """The rows of production query `name`."""
    return select(client, load_templates()[name])


def cell(row: dict, name: str) -> str | None:
    return row[name]["value"] if name in row else None


def report(client) -> list:
    properties, items = client.vocabulary()
    return disagreements(
        run(client), properties, items, client.config.concept_uri
    )


def time_shown(value: str, precision: int) -> str:
    """A time at its stated precision: a century as Wikibase counts it for
    its year, a decade as its first year."""
    day = value.lstrip("+")[:10]
    year = int(day[:4])
    if precision <= 7:
        return f"{(year + 99) // 100}th century"
    if precision == 8:
        return f"{year // 10 * 10}s"
    return day[: {9: 4, 10: 7}.get(precision, 10)]


def shown(graph: Fixture, side) -> str:
    """A report value as its sources state it: a time at its stated
    precision, an item as its fixture key, a name as its text in lower
    case (the row shows one of the spellings that differ only in case),
    each with the range its source states, if any."""
    value = side.value
    if value.precision is not None:
        stated = time_shown(value.term.value, value.precision)
    elif value.term.kind == "uri":
        stated = graph.key_of(value.term.value)
    else:
        stated = value.term.value.lower()
    if value.earliest is None and value.latest is None:
        return stated
    earliest, latest = (
        time_shown(bound.value, bound.precision) if bound else ""
        for bound in (value.earliest, value.latest)
    )
    return f"{stated} [{earliest}..{latest}]"


def sources(graph: Fixture, side) -> set[str]:
    return {graph.key_of(source.qid) for source in side.sources}


def dated(graph: Fixture, side) -> str:
    """A value's day, with the days of its stated range, and its sources."""
    value = side.value
    day = value.term.value.lstrip("+")[:10]
    if value.earliest or value.latest:
        day += " [{}..{}]".format(
            *(
                bound.value[:10] if bound else ""
                for bound in (value.earliest, value.latest)
            )
        )
    return f"{day} per {','.join(sorted(sources(graph, side)))}"


REPORTED = {
    (
        "person",
        "date_of_birth",
        "",
        "",
        frozenset({"1919-01-07", "1919-07-01"}),
    ),
    ("person", "residence", "town", "start_time", frozenset({"1941", "1942"})),
    (
        "spouse",
        "birth_name",
        "",
        "",
        frozenset({"fixture spouse birth name", "fixture spouse maiden name"}),
    ),
    (
        "spouse",
        "spouse",
        "person",
        "start_time",
        frozenset({"1946", "1947-02-08"}),
    ),
    (
        "ranges",
        "date_of_birth",
        "",
        "",
        frozenset({"1890s [1890..1890]", "1899"}),
    ),
    (
        "ranges",
        "date_of_death",
        "",
        "",
        frozenset({"1939 [1939-08..1939-09]", "1939-10-01"}),
    ),
    (
        "same year",
        "date_of_birth",
        "",
        "",
        frozenset({"1890", "1890s [1899..1899]"}),
    ),
    (
        "same year",
        "date_of_death",
        "",
        "",
        frozenset({"1945 [..1945]", "1945 [1945..]"}),
    ),
    (
        "emigration",
        "point_in_time",
        "",
        "",
        frozenset({"1938-06-22", "1938-08-19"}),
    ),
    ("D", "inception", "", "", frozenset({"1939-07-28", "1940"})),
    ("D", "inception", "", "", frozenset({"1939-07", "1940"})),
}
# each value the report lists, with the sources that state it
STATED_BY = {
    "1919-01-07": {"A"},
    "1919-07-01": {"B"},
    "1941": {"A"},
    "1942": {"B"},
    # both spellings of one name, listed together
    "fixture spouse birth name": {"A", "C"},
    "fixture spouse maiden name": {"B"},
    "1947-02-08": {"A"},
    "1946": {"B"},
    "1890s [1890..1890]": {"A"},
    "1899": {"B"},
    "1939 [1939-08..1939-09]": {"A"},
    "1939-10-01": {"B"},
    "1890": {"A", "C"},
    "1890s [1899..1899]": {"B"},
    "1945 [..1945]": {"A"},
    "1945 [1945..]": {"B"},
    "1938-06-22": {"B"},
    "1938-08-19": {"A"},
    "1939-07-28": {"D"},
    "1939-07": {"C"},
    "1940": {"B"},
}


def test_the_disagreement_report_pairs_what_different_artifacts_state(
    fictional_client, graph
):
    client = fictional_client
    properties, _ = client.vocabulary()
    found = report(client)
    subjects = {graph.qids[key] for key in REPORT_SUBJECTS}
    rows = [row for row in found if row.entity_qid in subjects]

    def key(row) -> tuple:
        return (
            graph.key_of(row.entity),
            row.property,
            graph.key_of(row.shared) if row.shared else "",
            row.qualifier,
            frozenset({shown(graph, row.a), shown(graph, row.b)}),
        )

    # each disagreement listed once, and nothing else about the subjects
    assert sorted(map(key, rows), key=repr) == sorted(REPORTED, key=repr)
    for row in rows:
        for side in (row.a, row.b):
            assert sources(graph, side) == STATED_BY[shown(graph, side)]
    # a year-precision date is never shown as 1 January
    (married,) = [row for row in rows if row.property == "spouse"]
    assert {
        shown(graph, side): side.value.precision
        for side in (married.a, married.b)
    } == {"1946": 9, "1947-02-08": 11}
    # each pair stored alike is listed once
    alike = [row for row in rows if graph.key_of(row.entity) == "same year"]
    assert len({row.property for row in alike}) == len(alike) == 2

    # what the report leaves out is in the graph: the correction from the
    # same artifact, the page beside its own artifact, the deprecated
    # place, one birth name in two spellings, and the nicknames
    claims = client.fetch_claims([graph.qids["person"]])[graph.qids["person"]]
    for prop in (
        "date_of_death",
        "place_of_death",
        "place_of_birth",
        "birth_name",
        "nickname",
    ):
        assert len(claims[properties[prop]]) == 2, prop
        assert not [
            row
            for row in rows
            if graph.key_of(row.entity) == "person" and row.property == prop
        ], prop
    assert len(claims[properties["residence"]]) == 4
    # refinements: a span never faces a date inside it
    assert not [
        row for row in rows if graph.key_of(row.entity) == "second emigration"
    ]
    assert not {
        shown(graph, side) for row in rows for side in (row.a, row.b)
    } & {"1919", "1938", "19th century", "1863"}

    # a century spans the years Wikibase counts in it: 1900 lies inside
    # the 19th, and 1800 does not
    assert {
        frozenset({dated(graph, row.a), dated(graph, row.b)})
        for row in found
        if row.entity_qid == graph.qids["century person"]
    } == {
        frozenset({"1801-01-01 per A", "1800-01-01 per C"}),
        frozenset({"1900-01-01 per B", "1800-01-01 per C"}),
    }

    # each end of a period is compared through its own range: A's ranges
    # hold C's days, which differ from them only in resolution
    assert {
        (
            row.property,
            row.qualifier,
            frozenset({dated(graph, row.a), dated(graph, row.b)}),
        )
        for row in found
        if row.entity_qid == graph.qids["period person"]
    } == {
        (
            "spouse",
            "start_time",
            frozenset(
                {
                    "1946-01-01 [1946-01-01..1947-01-01] per A",
                    "1948-01-01 per B",
                }
            ),
        ),
        (
            "spouse",
            "start_time",
            frozenset({"1947-02-08 per C", "1948-01-01 per B"}),
        ),
        (
            "spouse",
            "end_time",
            frozenset(
                {
                    "1956-01-01 [1956-01-01..1957-01-01] per A",
                    "1955-01-01 per B",
                }
            ),
        ),
        (
            "spouse",
            "end_time",
            frozenset({"1955-01-01 per B", "1957-01-02 per C"}),
        ),
    }

    # the place of birth is compared through containment at any time,
    # never through a deprecated edge or one no source states
    born = {
        row.entity_qid: frozenset(
            graph.key_of(side.value.term.value) for side in (row.a, row.b)
        )
        for row in found
        if row.property == "place_of_birth"
        and row.entity_qid
        in {graph.qids[key] for key in ("refined", "listed", "run only")}
    }
    assert born == {
        graph.qids["listed"]: frozenset({"hamlet", "decoy"}),
        graph.qids["run only"]: frozenset({"hamlet", "unstated"}),
    }


def test_the_containment_edges_carry_their_rank_and_source(
    fictional_client, graph
):
    rows = rows_of(fictional_client, CONTAINMENT)
    edges = sorted(
        (
            graph.key_of(row["target"]["value"]),
            row["rank"]["value"].rsplit("#", 1)[-1],
            row["stated"]["value"],
        )
        for row in rows
        if row["item"]["value"] == graph.iri("hamlet")
    )
    assert edges == sorted(
        [
            ("district", "NormalRank", "true"),
            ("district", "NormalRank", "true"),
            ("decoy", "DeprecatedRank", "true"),
            ("unstated", "NormalRank", "false"),
        ]
    )
    # the report follows the path through the dated edge to the oblast,
    # and never a deprecated edge or one no source states
    containment = read_containment(rows)
    hamlet = graph.iri("hamlet")
    assert place_within(containment, hamlet, graph.iri("oblast"))
    assert not place_within(containment, hamlet, graph.iri("decoy"))
    assert not place_within(containment, hamlet, graph.iri("unstated"))

    # the impossibility filter's closure reads the same edges
    closure = fetch_place_containment(
        fictional_client, fictional_client.config
    )
    q = graph.qids
    assert closure.containers(q["hamlet"]) == {q["district"], q["oblast"]}
    assert closure.contains(q["oblast"], q["hamlet"])
    assert closure.containers(q["reconcile town"]) == {q["reconcile country"]}
    assert closure.contains(q["reconcile country"], q["reconcile town"])


def event_types(row: dict) -> tuple[str, ...]:
    """An event row's types, folded into one URI-encoded cell."""
    folded = cell(row, "eventTypes")
    if folded is None:
        return ()
    return tuple(sorted(urllib.parse.unquote(t) for t in folded.split("|")))


def test_the_person_timeline_dates_events_and_periods(fictional_client, graph):
    rows = rows_of(fictional_client, "person-timeline")

    def of(key: str) -> list[dict]:
        return [
            row for row in rows if row["person"]["value"] == graph.iri(key)
        ]

    # each participant but a deprecated one gets each of the event's rows
    # once: a preferred participant or type hides no other, a second type
    # adds no row, and a deprecated type is not named
    held = [
        (
            key,
            event_types(row),
            cell(row, "what"),
            row["date"]["value"][:10],
            row["precision"]["value"],
            graph.key_of(row["source"]["value"]),
        )
        for key in ("leopold", "ida", "felix")
        for row in of(key)
    ]
    types = ("event", "imprisonment")
    assert sorted(held, key=repr) == sorted(
        (
            (key, types, what, date, precision, "card")
            for key in ("leopold", "ida")
            for what, date, precision in (
                (None, "1939-09-01", "10"),
                ("start time", "1939-09-03", "11"),
                ("end time", "1939-10-01", "11"),
            )
        ),
        key=repr,
    )

    # a death event dates its principal's death, and no other
    # participant's timeline
    assert [
        (
            event_types(row),
            row["date"]["value"][:10],
            row["precision"]["value"],
        )
        for row in of("eugen")
    ] == [(("death",), "1909-01-01", "9")]
    assert of("sebastian") == []

    # each end of a period with its own range, a point in time beside them
    # with none, and a residence's point in time with its statement's
    # range; each date names the qualifiers that bound it
    properties, _ = fictional_client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}

    def bound(name: str, row: dict) -> str:
        return key_of[row[name]["value"].rsplit("/", 1)[-1]]

    period = of("period person")
    spouse = graph.labels["period spouse"]
    town = graph.labels["period town"]
    start, end = (
        f"spouse: {spouse} (start time)",
        f"spouse: {spouse} (end time)",
    )
    observed = f"spouse: {spouse} (point in time)"
    lived = f"residence: {town} (point in time)"

    def day(name: str, row: dict) -> str | None:
        return row[name]["value"][:10] if name in row else None

    assert {
        (
            cell(row, "what"),
            day("date", row),
            row["precision"]["value"],
            day("earliest", row),
            day("latest", row),
            graph.key_of(row["source"]["value"]),
        )
        for row in period
    } == {
        (start, "1946-01-01", "9", "1946-01-01", "1947-01-01", "A"),
        (end, "1956-01-01", "9", "1956-01-01", "1957-01-01", "A"),
        (observed, "1950-01-01", "9", None, None, "A"),
        (start, "1948-01-01", "9", None, None, "B"),
        (end, "1955-01-01", "9", None, None, "B"),
        (start, "1947-02-08", "11", None, None, "C"),
        (end, "1957-01-02", "11", None, None, "C"),
        (lived, "1938-01-01", "9", "1938-01-01", "1939-01-01", "A"),
    }
    assert {
        (
            cell(row, "what"),
            bound("low", row),
            bound("high", row),
        )
        for row in period
    } == {
        (start, "earliest_date", "latest_start_date"),
        (end, "earliest_end_date", "latest_date"),
        (observed, "earliest_date", "latest_date"),
        (lived, "earliest_date", "latest_date"),
    }


def test_the_kinship_rows_follow_the_stated_steps_at_their_ranks(
    fictional_client, graph
):
    # the fixture holds the curator's ranks the rows depend on
    properties, _ = fictional_client.vocabulary()
    subjects = [graph.qids[kin(key)] for key in KIN]
    claims = fictional_client.fetch_claims(subjects)
    for subject, prop, value, *_, rank in KINSHIP:
        (statement,) = [
            statement
            for statement in claims[graph.qids[kin(subject)]][properties[prop]]
            if statement["mainsnak"]["datavalue"]["value"]["id"]
            == graph.qids[kin(value)]
        ]
        assert statement["rank"] == (rank or WikibaseRank.NORMAL).value

    people = {graph.iri(kin(key)): key.split("-")[0] for key in KIN}
    rows = {
        (
            people[row["subject"]["value"]],
            people.get(row["relative"]["value"], row["relative"]["value"]),
            row["relationship"]["value"],
        )
        for row in rows_of(fictional_client, "kinship")
        if row["subject"]["value"] in people
    }
    assert rows == RELATIVES


def statement_iri(client, guid: str) -> str:
    """An API statement GUID (``Q5$uuid``) as its RDF node."""
    concept = client.config.concept_uri
    return f"{concept}/entity/statement/{guid.replace('$', '-', 1)}"


def test_the_review_queues_list_what_needs_a_reviewer(fictional_client, graph):
    client = fictional_client
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}

    def queue(*keys: str, scope=()) -> dict[str, dict]:
        template = load_templates()["under-witnessed-statements"]
        if scope:
            # the header's documented scope by property
            values = " ".join(f"wd:{{P:{key}}}" for key in scope)
            template = template.replace(
                "WHERE {", f"WHERE {{\n  VALUES ?prop {{ {values} }}", 1
            )
        wanted = {graph.iri(key) for key in keys}
        return {
            row["statement"]["value"]: row
            for row in select(client, template)
            if row["subject"]["value"] in wanted
        }

    def statements(key: str) -> dict[str, list[dict]]:
        qid = graph.qids[key]
        return {
            key_of[pid]: held
            for pid, held in client.fetch_claims([qid])[qid].items()
        }

    def value(statement: dict) -> object:
        held = statement["mainsnak"]["datavalue"]["value"]
        if isinstance(held, dict) and "time" in held:
            return held["time"][1:5]
        if isinstance(held, dict) and "text" in held:
            return held["text"]
        return held

    subject = statements("queue subject")

    def pick(prop: str, wanted) -> dict:
        (statement,) = [s for s in subject[prop] if value(s) == wanted]
        return statement

    rows = queue("queue subject")
    listed = [
        pick("date_of_birth", "1911"),
        pick("date_of_birth", "1912"),
        pick("married_name", "Queue Fixture Wedded"),
        *subject["residence"],
    ]
    # one row a statement: the two same-value residences are two
    assert len(subject["residence"]) == 2
    assert set(rows) == {statement_iri(client, s["id"]) for s in listed}
    # the kind two sources witness, the agreeing second source, the machine
    # value beside a curator's sibling, the curator's statement and a
    # curator-only property are in the graph and not in the queue
    (kind,) = subject["instance_of"]
    for left_out in (
        kind,
        pick("date_of_death", "1970"),
        pick("nickname", "Fixie"),
        pick("nickname", "Fixy"),
        pick("birth_name", "Queue Fixture Born"),
    ):
        assert statement_iri(client, left_out["id"]) not in rows
    # each row names its single source, and a time its stated precision
    for year, source in (("1911", "A"), ("1912", "B")):
        row = rows[statement_iri(client, pick("date_of_birth", year)["id"])]
        assert graph.key_of(row["source"]["value"]) == source
        assert row["precision"]["value"] == "9"
    # the documented scope for names: the nickname has a curator's sibling,
    # and the birth name is a curator's alone
    scoped = queue(
        "queue subject",
        scope=("birth_name", "married_name", "nickname", "official_name"),
    )
    assert set(scoped) == {
        statement_iri(
            client, pick("married_name", "Queue Fixture Wedded")["id"]
        )
    }

    # a later source witnesses Sebastian's kind, so the queue leaves it
    # out; every other statement of the slice has one source and no
    # curator's sibling, the firm's two kinds among them
    slice_ = {key: statements(key) for key in ("witnessed", "hatter", "firm")}
    assert {
        key: {prop: len(held) for prop, held in by_prop.items()}
        for key, by_prop in slice_.items()
    } == {
        "witnessed": {"instance_of": 1, "occupation": 1, "employer": 1},
        "hatter": {"instance_of": 1},
        "firm": {
            "instance_of": 2,
            "type_hint": 1,
            "referred_to_as": 1,
            "start_time": 1,
        },
    }
    (witnessed,) = slice_["witnessed"]["instance_of"]
    assert set(queue("witnessed", "hatter", "firm")) == {
        statement_iri(client, statement["id"])
        for by_prop in slice_.values()
        for held in by_prop.values()
        for statement in held
        if statement["id"] != witnessed["id"]
    }
    # an artifact's or a run's own kind is no machine reading
    assert queue(*graph.sources, *graph.run_keys) == {}
    # and no statement of the fixture is unreferenced
    everything = {graph.iri(key) for key in graph.qids}
    assert not [
        row
        for row in rows_of(client, "unreferenced-statements")
        if row["entity"]["value"] in everything
    ]

    # the unclassified entities, one row a type hint in lower case
    unclassified: dict[str, dict] = {}
    found = rows_of(client, "unclassified-entities")
    for row in found:
        hint = cell(row, "typeHint")
        assert hint is not None, row
        assert hint not in unclassified, hint
        unclassified[hint] = {
            name: data["value"] for name, data in row.items()
        }
    shop = unclassified[HAT_SHOP]
    assert (shop["entities"], shop["artifacts"]) == ("3", "2")
    assert graph.key_of(shop["example"]) in {"firm", "parlour", "stall"}
    assert shop["exampleLabel"] == graph.labels[graph.key_of(shop["example"])]
    # one entity, its hint stated in two artifacts, one by a curator
    airship = unclassified["airship"]
    assert (airship["entities"], airship["artifacts"]) == ("1", "2")
    assert airship["example"] == graph.iri("airship")
    assert airship["exampleLabel"] == graph.labels["airship"]
    # a deprecated hint and a deprecated kind are not read
    assert "aircraft" not in unclassified
    assert "ocean liner" not in unclassified
    # the unhinted entity and the craft, under the empty hint
    assert int(unclassified[""]["entities"]) >= 2
    # ordered by how many entities carry the hint
    counts = [int(row["entities"]["value"]) for row in found]
    assert counts == sorted(counts, reverse=True)
    hints = [cell(row, "typeHint") for row in found]
    assert hints.index(HAT_SHOP) < hints.index("airship")


class NoCall:
    """An LLM client that records each prompt and proposes nothing."""

    model_identifier = "fictional-integration"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return '{"done": true}'


def test_the_reconcile_roster_reads_kinship_dates_and_appellations(
    fictional_client, graph
):
    roster = fetch_roster(fictional_client, fictional_client.config)

    def entity(key: str):
        (found,) = [row for row in roster if row.qid == graph.qids[key]]
        return found

    elder, younger = entity("elder"), entity("younger")
    # the father edge as a QID the impossibility filter reads, and each
    # kind as its manifest key
    assert elder.qid in younger.father_qids
    assert elder.qid in younger.kin_qids
    assert (younger.kind, elder.kind) == ("human", "human")
    assert entity("reconcile town").kind == "place"

    # direct and principal-event readings at the precision each states
    (direct,) = elder.births
    (event,) = younger.births
    assert (direct.value.text(), elder.death) == (
        "the 19th century",
        "the 1890s",
    )
    assert direct.event is None
    assert (event.value.text(), younger.death) == ("the 1850s", "1938")
    assert event.event == graph.qids["birth"]
    for reading in (direct, event):
        assert reading.rank == "normal"
        assert graph.qids["D"] in {
            source.source
            for reference in reading.references
            for source in reference.sources
        }
    assert "the 19th century" in elder.facts()
    assert "the 1850s" in younger.facts()
    # a death its source states without its date
    assert entity("brother").death == "date not stated"

    # each grandmother's appellation read from her `referred_to_as`
    # statement beside her aliases: the "Oma" they share keys neither, so
    # the two are never put to the adjudicator, though read without their
    # appellations they would be
    grandmothers = [entity("grandmother a"), entity("grandmother b")]
    for grandmother, key in zip(
        grandmothers, ("grandmother a", "grandmother b"), strict=True
    ):
        assert set(grandmother.aliases) == set(RECONCILE[key][2])
        assert grandmother.appellations == [APPELLATION]
    llm = NoCall()
    assert propose_graph_pairs(llm, grandmothers) == []
    assert llm.prompts == []
    propose_graph_pairs(
        llm,
        [
            grandmother.model_copy(update={"appellations": []})
            for grandmother in grandmothers
        ],
    )
    assert len(llm.prompts) == 1


def test_the_gallery_and_the_map_name_media_and_connections(
    fictional_client, graph
):
    concept = fictional_client.config.concept_uri
    photos = {
        graph.key_of(row["photo"]["value"]): (
            cell(row, "photoLabel"),
            cell(row, "image"),
            cell(row, "containerLabel"),
        )
        for row in rows_of(fictional_client, "photo-gallery")
        if graph.key_of(row["photo"]["value"]) in MEDIA
    }
    file_path = f"{concept}/wiki/Special:FilePath/"
    assert photos == {
        "booklet image": (
            BOOKLET_IMAGE,
            file_path + urllib.parse.quote(MEDIA["booklet image"], safe=""),
            graph.labels["booklet"],
        ),
        "studio": (
            graph.labels["studio"],
            file_path + urllib.parse.quote(MEDIA["studio"], safe=""),
            None,
        ),
    }

    places = [
        row
        for row in rows_of(fictional_client, "places-map")
        if graph.key_of(row["place"]["value"]) in COORDINATES
    ]
    assert {
        (
            graph.key_of(row["place"]["value"]),
            graph.key_of(row["connection"]["value"])
            if "connection" in row
            else None,
        )
        for row in places
    } == {
        ("floerburg", "klara"),
        ("floerburg", "klara emigrates"),
        ("moshausen", None),
    }
    for row in places:
        point = row["coords"]["value"]
        assert point.startswith("Point(") and point.endswith(")")
        longitude, latitude = map(float, point[6:-1].split())
        expected = COORDINATES[graph.key_of(row["place"]["value"])]
        assert (latitude, longitude) == pytest.approx(expected)


def test_every_production_query_runs_and_no_statement_is_unreferenced(
    fictional_client, graph
):
    """Over the whole stack: every production query runs, and every query
    but two gives rows for the fixture. The identity-provenance query lists
    accepted and overridden identity origins, which the fixture holds none
    of (the write/query case checks its rows), and no statement on the
    stack, the vocabulary's and the fixture's, lacks a reference."""
    templates = load_templates()
    for name in templates:
        rows = rows_of(fictional_client, name)
        if name == "unreferenced-statements":
            assert rows == []
        elif name != "identity-provenance":
            assert rows, name
