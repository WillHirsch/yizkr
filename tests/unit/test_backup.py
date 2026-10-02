"""Offline tests for backup/export logic. No stack, no docker. The
possibly-living determination itself is tested in test_privacy.py.

The non-privileged export keeps only the people the check releases (#344): it
reads person evidence from its own dump, at every rank, and drops every
item with any that the check did not release, and every item that states
no type; and it refuses to run while the query service, where the check
runs, lags the dump. It drops every event that an item it drops took
part in, and the judgement notes, and keeps every text about what it
keeps: it is a non-privileged view (#363; the owner, 2026-09-29). A
statement that names an item it drops, as its value or a qualifier's,
goes whole, and an item a participant statement's qualifier names takes
part in the event (#364)."""

import datetime
import gzip
import hashlib
import json
import re
import threading
from pathlib import Path

import pytest
import yaml

from tests.munger_blocks import blocks, misplaced, misplaced_in
from tests.unit.test_privacy import (
    BIRTHS,
    CASES,
    CONCEPT,
    DOCUMENTED_PERSON_EVENTS,
    DOCUMENTED_SUBJECT_EVIDENCE,
    DOCUMENTED_VALUE_EVIDENCE,
    EXCLUDED,
    MANIFEST,
    XSD,
    birth_rows,
    literal,
    uri,
)
from yizkr import backup
from yizkr.backup import (
    LAG_POLL,
    NOT_ASSESSED,
    PERSON_FACTS_QUERY,
    REVISIONS_QUERY,
    DumpReading,
    ExclusionReport,
    NonprivilegedExportRefused,
    QueryServiceLag,
    UnfilterableDump,
    await_query_service,
    exclusion_report,
    export_nonprivileged,
    filter_nonprivileged_export,
    nonprivileged_exclusions,
    prune_old_backups,
    read_dump,
    run_backup,
    stale_entities,
)
from yizkr.core.config import Config
from yizkr.core.privacy import BIRTHS_QUERY, people_selection
from yizkr.model.schema import load_manifest, read_manifest_data
from yizkr.queries.publish import prefix_block

# how long a run that should end at once may take before it counts as hung
HANG_GUARD = 30


@pytest.fixture(scope="module")
def vocabulary():
    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


def config() -> Config:
    return Config(
        wikibase_url=CONCEPT,
        concept_uri=CONCEPT,
        wdqs_url="http://query.example",
        sparql_url="http://query.example/sparql",
        admin_name="x",
        admin_pass="x",
        env_file=None,
    )


# One row per person of CASES, as PERSON_FACTS_QUERY gives it: a sampled
# date of death for those with death evidence, nothing for the others.
FACTS = [
    {"person": uri(qid)}
    | (
        {
            "death": {
                "type": "literal",
                "datatype": "http://www.w3.org/2001/XMLSchema#dateTime",
                "value": "1995-01-01T00:00:00Z",
            }
        }
        if has_death
        else {}
    )
    for qid, (_, _, has_death, _) in CASES.items()
]


class TestExclusionReport:
    """The non-privileged export's call (ADR-0020): every person the facts
    query lists, with the births the privacy module's query reads (#334)."""

    def report(self, vocabulary, facts=FACTS, births=BIRTHS, **options):
        calls = []

        def run(query: str) -> list[dict]:
            calls.append(query)
            if "SAMPLE(?deathDate)" in query:
                return facts
            if 'BIND("value" AS ?bound)' in query:
                return births
            raise AssertionError(f"unexpected query: {query}")

        found = exclusion_report(
            config(), run=run, vocabulary=vocabulary, **options
        )
        return found, calls

    def test_every_birth_is_read_at_the_latest_year_it_allows(
        self, vocabulary
    ):
        found, _ = self.report(vocabulary)
        assert found.excluded == EXCLUDED
        assert found.included == set(CASES) - EXCLUDED

    def test_each_person_gives_a_reason(self, vocabulary):
        found, _ = self.report(vocabulary)
        assert found.reasons["Q304"] == (
            "born as late as 1990, with no death evidence"
        )
        assert found.reasons["Q312"] == "death recorded"
        assert "unknown means excluded" in found.reasons["Q311"]

    def test_the_cutoff_moves_with_cutoff_years(self, vocabulary):
        # a cutoff in 1990 releases the births of 1990 and before
        years = datetime.datetime.now(datetime.UTC).year - 1990
        found, _ = self.report(vocabulary, cutoff_years=years)
        assert {"Q303", "Q304", "Q308"} & found.excluded == {"Q303"}

    def test_births_are_read_with_the_privacy_modules_query(self, vocabulary):
        properties, _ = vocabulary
        _, calls = self.report(vocabulary)
        assert len(calls) == 2
        assert prefix_block(CONCEPT) + BIRTHS_QUERY.format(**properties) in (
            calls
        )

    def test_the_facts_query_reads_no_birth(self):
        # births are read statement by statement, never sampled here
        assert "SAMPLE(?birthDate)" not in PERSON_FACTS_QUERY
        assert "date_of_birth" not in PERSON_FACTS_QUERY

    def test_people_are_selected_through_their_statements(self, vocabulary):
        # every item with person evidence, at every rank (#344), never the
        # truthy human type, which a deprecated or outranked type escapes
        properties, items = vocabulary
        _, calls = self.report(vocabulary)
        (facts,) = [query for query in calls if "SAMPLE(?deathDate)" in query]
        assert people_selection(properties, items) in facts
        # nor any truthy type beside the selection
        assert f"?person wdt:{properties['instance_of']} " not in facts


REF_DEAD = "a" * 40  # reference used only by the excluded person
REF_SHARED = "b" * 40  # reference used by an included person too
VALUE_DEAD = "c" * 40  # value node reachable only via REF_DEAD
VALUE_LIVE = "d" * 40  # value node reachable via REF_SHARED
REF_TEXT = "e" * 40  # a reference with a page number and an excerpt
REF_RUN = "1" * 40  # a run's reference, with a judgement note
TIME = "^^<http://www.w3.org/2001/XMLSchema#dateTime>"
W = "http://w.lan"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
ONTOLOGY = "http://wikiba.se/ontology#"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
PREF_LABEL = "<http://www.w3.org/2004/02/skos/core#prefLabel>"
ALT_LABEL = "<http://www.w3.org/2004/02/skos/core#altLabel>"
NAME = "<http://schema.org/name>"
# the three names dumpRdf.php gives an entity's label
LABELS = (RDFS_LABEL, PREF_LABEL, NAME)
DESCRIPTION = "<http://schema.org/description>"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
# the datatypes of the properties the export is made to filter
READABLE_DATATYPES = {
    "wikibase-item",
    "string",
    "monolingualtext",
    "external-id",
    "time",
    "quantity",
    "globe-coordinate",
}


def rdf_type(datatype: str) -> str:
    """The name Wikibase gives a datatype in its RDF, as a property's
    wikibase:propertyType: each word of it capitalised, run together."""
    return "".join(
        word[:1].upper() + word[1:]
        for word in datatype.replace("-", " ").split()
    )


def nt(text: str) -> str:
    """`text` as dumpRdf.php writes a literal's lexical form: a quote, a
    backslash and a control character escaped, and every character
    outside printable ASCII as \\u or \\U."""
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    return "".join(
        escapes.get(char)
        or (
            char
            if 0x20 <= ord(char) < 0x7F
            else f"\\u{ord(char):04X}"
            if ord(char) < 0x10000
            else f"\\U{ord(char):08X}"
        )
        for char in text
    )


class TestNonprivilegedFilter:
    LINES = [
        # the excluded person's own triples: entity, statement node, sitelink
        f'<http://w.lan/entity/Q76> {RDFS_LABEL} "Nicholas"@en .',
        f'<http://w.lan/entity/statement/Q76-aaaa-bbbb> <http://x/p> "1950-01-01T00:00:00Z"{TIME} .',  # noqa: E501
        "<http://w.lan/wiki/Item:Q76> <http://schema.org/about> <http://w.lan/entity/Q76> .",  # noqa: E501
        # someone else's claim ABOUT the excluded person
        "<http://w.lan/entity/Q72> <http://w.lan/prop/direct/P7> <http://w.lan/entity/Q76> .",  # noqa: E501
        # an includable person whose QID shares a prefix (Q7 vs Q76)
        f'<http://w.lan/entity/Q7> {RDFS_LABEL} "Gustav"@en .',
        # unrelated triple
        f'<http://w.lan/entity/Q100> {RDFS_LABEL} "place"@en .',
        # provenance: the excluded person's statement uses REF_DEAD
        f"<http://w.lan/entity/statement/Q76-cccc> {DERIVED_FROM} <http://w.lan/reference/{REF_DEAD}> .",  # noqa: E501
        # REF_DEAD's content quotes source text naming the living person
        f'<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/P8> "Nicholas 1950"@en .',  # noqa: E501
        f"<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_DEAD}> .",  # noqa: E501
        f'<http://w.lan/value/{VALUE_DEAD}> <http://x/time> "1950-01-01T00:00:00Z"{TIME} .',  # noqa: E501
        # provenance shared with an included person's statement
        f"<http://w.lan/entity/statement/Q7-dddd> {DERIVED_FROM} <http://w.lan/reference/{REF_SHARED}> .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/P8> \"Gustav 'Gustl' 1919\"@en .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_LIVE}> .",  # noqa: E501
        f'<http://w.lan/value/{VALUE_LIVE}> <http://x/time> "1919-01-01T00:00:00Z"{TIME} .',  # noqa: E501
    ]

    def filter(
        self, tmp_path: Path, excluded: set[str], dropped_pids=frozenset()
    ):
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        kept, dropped = filter_nonprivileged_export(
            full, public, excluded, dropped_pids
        )
        with gzip.open(public, "rt") as handle:
            return kept, dropped, handle.read()

    def test_every_mention_is_dropped(self, tmp_path):
        _, _, content = self.filter(tmp_path, {"Q76"})
        assert "Q76" not in content
        # the OTHER person's claim about Q76 is gone, but Q72 itself is
        # only trimmed, not removed wholesale
        assert "Gustav" in content and "Q100" in content

    def test_orphaned_provenance_nodes_are_swept(self, tmp_path):
        """The leak found live: a dropped statement's reference node
        still carried source text naming the living person."""
        _, _, content = self.filter(tmp_path, {"Q76"})
        assert "Nicholas 1950" not in content
        assert REF_DEAD not in content
        assert VALUE_DEAD not in content
        # shared provenance still reachable from Q7's statement survives
        assert REF_SHARED in content
        assert VALUE_LIVE in content

    def test_an_excerpt_leaves_with_the_statement_it_attests(self, tmp_path):
        # what is recorded about a person the check releases stays, the
        # source's own words included (owner, 2026-09-29; #363); the
        # excerpt of a dropped statement goes with it
        _, _, content = self.filter(tmp_path, {"Q76"})
        assert "Gustav 'Gustl' 1919" in content
        assert "Nicholas 1950" not in content

    def test_a_dropped_property_never_leaves(self, tmp_path):
        # P8 as a judgement note's property (#363; ADR-0035), on every
        # reference; the reference nodes themselves survive
        _, _, content = self.filter(tmp_path, set(), dropped_pids={"P8"})
        assert "Gustav 'Gustl' 1919" not in content
        assert "Nicholas 1950" not in content
        assert REF_SHARED in content
        assert VALUE_LIVE in content

    def test_qid_prefix_does_not_collide(self, tmp_path):
        _, _, content = self.filter(tmp_path, {"Q7"})
        assert "Gustav" not in content
        assert '"Nicholas"' in content  # Q76 untouched by excluding Q7

    def test_no_exclusions_passes_everything(self, tmp_path):
        kept, dropped, _ = self.filter(tmp_path, set())
        assert (kept, dropped) == (len(self.LINES), 0)


# an unknown value, which `statement` writes as dumpRdf.php does
UNKNOWN = "somevalue"


def statement(
    subject: str,
    node: str,
    pid: str,
    value: str | None,
    rank: str = "Normal",
    qualifiers=(),
    reference: str | None = None,
    normalized: str | None = None,
    truthy: str | None = None,
) -> list[str]:
    """A statement as dumpRdf.php writes one, under `W`: its link, types
    and rank, its value (and normalized value), its qualifiers as
    (property, predicate form, value) triples, its reference, and the
    truthy triples a best-ranked one gives its subject. A value of None
    is no value, which types the node, and so its subject, with the
    property's "no value" class, a qualifier's the node alone. A value of
    `UNKNOWN` is a blank node of the statement's own, and the truthy
    triple's is another, as dumpRdf.php labels each apart. `truthy` is
    the value of the truthy triple when it is another than `value`."""
    if value == UNKNOWN:
        value = f"_:{subject}{node}value"
        truthy = truthy or f"_:{subject}{node}direct"
    item = f"<{W}/entity/{subject}>"
    node = f"<{W}/entity/statement/{subject}-{node}>"
    best = rank != "Deprecated"

    def holds(term: str, key: str, form: str, held: str | None) -> str:
        if held is None:
            return f"{term} {RDF_TYPE} <{W}/prop/novalue/{key}> ."
        return f"{term} <{W}/prop/{form}{key}> {held} ."

    lines = [
        f"{item} <{W}/prop/{pid}> {node} .",
        f"{node} {RDF_TYPE} <{ONTOLOGY}Statement> .",
        *([f"{node} {RDF_TYPE} <{ONTOLOGY}BestRank> ."] if best else []),
        f"{node} <{ONTOLOGY}rank> <{ONTOLOGY}{rank}Rank> .",
        holds(node, pid, "statement/", value),
        *(
            [holds(node, pid, "statement/value-normalized/", normalized)]
            if normalized
            else []
        ),
        *(
            holds(node, qualifier, f"qualifier/{form}", qualified)
            for qualifier, form, qualified in qualifiers
        ),
        *(
            [f"{node} {DERIVED_FROM} <{W}/reference/{reference}> ."]
            if reference
            else []
        ),
    ]
    if best:
        lines.append(holds(item, pid, "direct/", truthy or value))
        if normalized:
            lines.append(holds(item, pid, "direct-normalized/", normalized))
    return lines


REF_WEDDING = "2" * 40  # the wedding's reference, which no kept one uses
VALUE_LETTER = "3" * 40  # a value node only that reference names
VALUE_1938 = "4" * 40  # the wedding's date, a value node only it holds
VALUE_1941 = "5" * 40  # Rudolf's death, a value node a kept one holds too
REF_INTERVIEW = "6" * 40  # a reference a kept statement shares
REF_CERTIFICATE = "7" * 40  # the reference of Rudolf's death
REF_DEPRECATED = "8" * 40  # the reference of a deprecated statement alone


class TestAStatementGoesWhole:
    """A statement whose value, or a qualifier's value, is an item left out
    goes whole: its node, rank, qualifiers and references, and the truthy
    triples only it gave (#364). Its node carries its subject's QID, not
    its value's, so the token filter alone would keep all but its value.

    Rudolf Löbig (Q501, 1916-1941) married Nina Sartina (Q502, of no
    known dates, so left out) in 1938, as her letter of 1958 says, and
    his death ended the marriage; a curator deprecated a reading of that
    letter that took her brother Pavel Sartin (Q503, left out) for his;
    Q504 is a duplicate of Nina's item, merged into hers. Gustav Löbig
    (Q506, 1919-1996) was "Dad" to Nicholas Lobig (Q507, born 1950, left
    out) and "Gus" to everyone, as the interview Nicholas gave says.
    Leopold Löbig (Q505) is kept, and so is his arrest (Q508), in which
    he took part in the role of its principal (Q509).

    The properties: P26 spouse, its start and end times P580 and P582,
    P570 a date of death, P1810 an appellation, P1811 its perspective,
    P1449 a nickname, P3373 a sibling, P40 a child, P460 said to be the
    same as, P70 an identifier, P50 an author, P710 a participant, P3831
    its role, and P1855 an example of a property's use; in a reference, P8
    its excerpt, P577 its source's date and P50 its author."""

    TIME_1938 = f'"1938-01-01T00:00:00Z"{TIME}'
    TIME_1941 = f'"1941-03-06T00:00:00Z"{TIME}'
    MARRIED = nt("В 1938 году мы поженились.")
    WEDDING = [
        *statement(
            "Q501",
            "aaaa",
            "P26",
            f"<{W}/entity/Q502>",
            qualifiers=[
                ("P580", "", TIME_1938),
                ("P580", "value/", f"<{W}/value/{VALUE_1938}>"),
                ("P582", "", TIME_1941),
                ("P582", "value/", f"<{W}/value/{VALUE_1941}>"),
            ],
            reference=REF_WEDDING,
        ),
        f'<{W}/reference/{REF_WEDDING}> <{W}/prop/reference/P8> "{MARRIED}"@ru .',  # noqa: E501
        f"<{W}/reference/{REF_WEDDING}> <{W}/prop/reference/value/P577> <{W}/value/{VALUE_LETTER}> .",  # noqa: E501
        f'<{W}/value/{VALUE_LETTER}> <{ONTOLOGY}timeValue> "1958-12-25T00:00:00Z"{TIME} .',  # noqa: E501
        f"<{W}/value/{VALUE_1938}> <{ONTOLOGY}timeValue> {TIME_1938} .",
    ]
    # kept values under a qualifier naming Nicholas: "Dad", as he called
    # Gustav, and an identifier, with its normalized form
    QUALIFIED = [
        *statement(
            "Q506",
            "aaaa",
            "P1810",
            '"Dad"@en',
            qualifiers=[("P1811", "", f"<{W}/entity/Q507>")],
            reference=REF_INTERVIEW,
        ),
        *statement(
            "Q506",
            "bbbb",
            "P70",
            '"id-1"',
            qualifiers=[("P50", "", f"<{W}/entity/Q507>")],
            normalized="<http://id.example/id-1>",
        ),
    ]
    # the deprecated reading, and Nina named through Q504
    OTHERS_LEFT_OUT = [
        *statement(
            "Q501",
            "bbbb",
            "P3373",
            f"<{W}/entity/Q503>",
            rank="Deprecated",
            reference=REF_DEPRECATED,
        ),
        f'<{W}/reference/{REF_DEPRECATED}> <{W}/prop/reference/P8> "{nt("Мой брат Павел Сартин кланяется Вам.")}"@ru .',  # noqa: E501
        *statement("Q501", "cccc", "P26", f"<{W}/entity/Q504>"),
    ]
    # the excerpt of the interview, which Gustav's appellation and
    # nickname both cite, and its author, Nicholas, whom that reference
    # alone names
    INTERVIEW = (
        f"<{W}/reference/{REF_INTERVIEW}> <{W}/prop/reference/P8> "
        f'"{nt("Dad — Gus, everyone called him")}"@en .'
    )
    AUTHOR = (
        f"<{W}/reference/{REF_INTERVIEW}> <{W}/prop/reference/P50> "
        f"<{W}/entity/Q507> ."
    )
    # Gustav's nickname, under the reference his appellation uses
    NICKNAME = statement(
        "Q506", "cccc", "P1449", '"Gus"@en', reference=REF_INTERVIEW
    )
    # Leopold's part in his arrest, in the role of its principal: a
    # qualifier that names an item kept
    ARREST = statement(
        "Q508",
        "aaaa",
        "P710",
        f"<{W}/entity/Q505>",
        qualifiers=[("P3831", "", f"<{W}/entity/Q509>")],
    )
    KEPT = [
        f'<{W}/entity/Q501> {RDFS_LABEL} "{nt("Rudolf Löbig")}"@de .',
        # his death, the value node of which the marriage's end shares
        *statement(
            "Q501", "dddd", "P570", TIME_1941, reference=REF_CERTIFICATE
        ),
        f"<{W}/entity/statement/Q501-dddd> <{W}/prop/statement/value/P570> <{W}/value/{VALUE_1941}> .",  # noqa: E501
        f"<{W}/value/{VALUE_1941}> <{ONTOLOGY}timeValue> {TIME_1941} .",
        f'<{W}/reference/{REF_CERTIFICATE}> <{W}/prop/reference/P8> "{nt("умер 6 марта 1941 г.")}"@ru .',  # noqa: E501
        *NICKNAME,
        INTERVIEW,
        # the same identifier, unqualified, on another item
        *statement(
            "Q505",
            "aaaa",
            "P70",
            '"id-1"',
            normalized="<http://id.example/id-1>",
        ),
        *ARREST,
    ]
    EXCLUDED = {"Q502", "Q503", "Q504", "Q507"}

    def lines(self, *more: list[str]) -> list[str]:
        return [
            *self.WEDDING,
            *self.QUALIFIED,
            *self.OTHERS_LEFT_OUT,
            *self.KEPT,
            self.AUTHOR,
            *(line for lines in more for line in lines),
        ]

    @staticmethod
    def filter(tmp_path: Path, lines, excluded) -> tuple[int, int, list[str]]:
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt") as handle:
            handle.write("\n".join(lines) + "\n")
        kept, dropped = filter_nonprivileged_export(full, public, excluded)
        with gzip.open(public, "rt") as handle:
            return kept, dropped, handle.read().splitlines()

    def test_a_statement_whose_value_is_left_out_goes_whole(self, tmp_path):
        # its link, types and rank, its dates (the start time's value node,
        # which only it holds, included), and its reference, with the
        # excerpt and the value node only that names
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        text = "\n".join(public)
        assert "Q501-aaaa" not in text
        assert self.TIME_1938 not in text
        assert self.MARRIED not in text
        assert "1958-12-25" not in text
        for node in (REF_WEDDING, VALUE_LETTER, VALUE_1938):
            assert node not in text

    def test_a_qualifier_naming_one_left_out_takes_the_statement(
        self, tmp_path
    ):
        # its kept value goes, and the truthy triples it gave, the
        # normalized one included; another item's same value stays
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        text = "\n".join(public)
        assert "Q506-aaaa" not in text and "Q506-bbbb" not in text
        assert '"Dad"@en' not in text
        for qid, kept in (("Q506", False), ("Q505", True)):
            for truthy in (
                f'<{W}/entity/{qid}> <{W}/prop/direct/P70> "id-1" .',
                f"<{W}/entity/{qid}> <{W}/prop/direct-normalized/P70> "
                "<http://id.example/id-1> .",
            ):
                assert (truthy in public) is kept

    def test_a_deprecated_statement_or_one_naming_a_redirect_goes(
        self, tmp_path
    ):
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        text = "\n".join(public)
        assert "Q501-bbbb" not in text and "Q501-cccc" not in text
        assert REF_DEPRECATED not in text
        assert nt("Павел Сартин") not in text

    def test_a_node_a_kept_statement_uses_too_stays(self, tmp_path):
        # the reference Gustav's appellation and nickname share, and the
        # value node of Rudolf's death, which the marriage's end time
        # names too: each stays, linked from the statement kept alone
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        derived = f"{DERIVED_FROM} <{W}/reference/{REF_INTERVIEW}> ."
        assert f"<{W}/entity/statement/Q506-aaaa> {derived}" not in public
        assert f"<{W}/entity/statement/Q506-cccc> {derived}" in public
        assert self.INTERVIEW in public
        died = f"<{W}/value/{VALUE_1941}>"
        assert f"{died} <{ONTOLOGY}timeValue> {self.TIME_1941} ." in public
        assert [line for line in public if line.endswith(f" {died} .")] == [
            f"<{W}/entity/statement/Q501-dddd> "
            f"<{W}/prop/statement/value/P570> {died} ."
        ]

    def test_what_a_kept_statement_uses_stays(self, tmp_path):
        # and every kept line leaves as it is
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        assert set(public) == set(self.KEPT)

    def test_a_qualifier_naming_an_item_kept_takes_nothing(self, tmp_path):
        # Leopold's part in his arrest stays whole, the truthy triple it
        # gave included: a qualifier that names a role, or any other item
        # kept, leaves its statement as it is
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        assert set(self.ARREST) <= set(public)

    def test_an_item_named_in_a_reference_takes_that_snak_alone(
        self, tmp_path
    ):
        # Nicholas, as the interview's author: the reference loses that
        # snak, and keeps its excerpt and the nickname that cites it
        _, _, public = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        assert self.AUTHOR not in public
        assert self.INTERVIEW in public
        assert set(self.NICKNAME) <= set(public)

    def test_the_lines_may_come_in_any_order(self, tmp_path):
        # a statement's value, qualifiers, rank and truthy triple before
        # the link to its subject
        forward = self.filter(tmp_path, self.lines(), self.EXCLUDED)
        backward = self.filter(tmp_path, self.lines()[::-1], self.EXCLUDED)
        assert set(backward[2]) == set(forward[2]) == set(self.KEPT)
        assert backward[:2] == forward[:2]

    def test_a_line_it_cannot_read_ends_no_reading(self, tmp_path):
        # lines that are no triple as dumpRdf.php writes one, before every
        # statement: each goes, and the statements after them are read
        lines = [*TestWhatTheFilterReads.UNREADABLE, *self.lines()]
        _, _, public = self.filter(tmp_path, lines, self.EXCLUDED)
        assert set(public) == set(self.KEPT)

    @pytest.mark.parametrize("order", [1, -1], ids=["in-order", "reversed"])
    @pytest.mark.parametrize(
        ("twin", "stays"),
        [
            ("Normal", True),
            ("Preferred", True),
            ("Deprecated", False),
            ("another value", False),
            ("in a qualifier", False),
            ("another property", False),
        ],
        ids=[
            "normal",
            "preferred",
            "deprecated",
            "another-value",
            "in-a-qualifier",
            "another-property",
        ],
    )
    @pytest.mark.parametrize(
        ("pid", "value", "normalized", "another", "other_pid"),
        [
            # the other property: a nickname
            ("P1810", '"Dad"@en', None, ('"Gus"@en', None), "P1449"),
            # the other property: another identifier
            (
                "P70",
                '"id-1"',
                "<http://id.example/id-1>",
                ('"id-2"', "<http://id.example/id-2>"),
                "P71",
            ),
        ],
        ids=["appellation", "identifier"],
    )
    def test_a_truthy_triple_a_kept_statement_gives_stays(
        self,
        tmp_path,
        pid,
        value,
        normalized,
        another,
        other_pid,
        twin,
        stays,
        order,
    ):
        # a kept statement of the same item and property with the same
        # value gives it too, at normal or preferred rank, the normalized
        # one included; a deprecated one gives none, nor does one of
        # another value, though it holds this one in a qualifier of that
        # very property, nor one of another property with this very value
        given = statement("Q506", "9999", pid, value, normalized=normalized)
        truthy = [line for line in given if "/prop/direct" in line]
        if twin == "another property":
            kept = statement(
                "Q506", "9999", other_pid, value, normalized=normalized
            )
        elif twin in ("Normal", "Preferred", "Deprecated"):
            kept = [
                line
                for line in statement(
                    "Q506", "9999", pid, value, twin, normalized=normalized
                )
                if line not in truthy
            ]
        else:
            other, other_normalized = another
            holding = [(pid, "", value)] if twin == "in a qualifier" else []
            kept = statement(
                "Q506",
                "9999",
                pid,
                other,
                qualifiers=holding,
                normalized=other_normalized,
            )
        _, _, public = self.filter(
            tmp_path, self.lines(kept)[::order], self.EXCLUDED
        )
        assert truthy
        for line in truthy:
            assert (line in public) is stays
        assert set(kept) <= set(public)

    @pytest.mark.parametrize("order", [1, -1], ids=["in-order", "reversed"])
    @pytest.mark.parametrize(
        "twin",
        [None, "Normal", "Preferred", "Deprecated"],
        ids=["alone", "kept-twin", "preferred-twin", "deprecated-twin"],
    )
    def test_a_truthy_triple_of_unknown_value_goes_with_every_one(
        self, tmp_path, twin, order
    ):
        # a child of Gustav's of unknown value, said to be Nicholas: the
        # statement goes, and so does the truthy triple it gave. Each
        # unknown value is a blank node of its own, and the export does not
        # read which truthy one a statement gave, so a kept child of
        # unknown value loses its truthy triple too, while its statement
        # stays; a sibling of unknown value, of another property, keeps its
        said = statement(
            "Q506",
            "dddd",
            "P40",
            UNKNOWN,
            qualifiers=[("P460", "", f"<{W}/entity/Q507>")],
        )
        other = (
            statement("Q506", "eeee", "P40", UNKNOWN, rank=twin)
            if twin
            else []
        )
        sibling = statement("Q506", "ffff", "P3373", UNKNOWN)
        _, _, public = self.filter(
            tmp_path, self.lines(said, other, sibling)[::order], self.EXCLUDED
        )
        children = f"<{W}/entity/Q506> <{W}/prop/direct/P40> _:"
        assert said[-1].startswith(children)
        assert not set(said) & set(public)
        assert not [line for line in public if line.startswith(children)]
        kept = [line for line in other if not line.startswith(children)]
        assert set(public) == set(self.KEPT) | set(kept) | set(sibling)

    @pytest.mark.parametrize("order", [1, -1], ids=["in-order", "reversed"])
    def test_a_deprecated_statement_takes_no_truthy_triple(
        self, tmp_path, order
    ):
        # a deprecated reading of a child of Gustav's of unknown value,
        # said to be Nicholas: it goes, but gave no truthy triple, so the
        # kept child of unknown value keeps its own
        said = statement(
            "Q506",
            "dddd",
            "P40",
            UNKNOWN,
            rank="Deprecated",
            qualifiers=[("P460", "", f"<{W}/entity/Q507>")],
        )
        other = statement("Q506", "eeee", "P40", UNKNOWN)
        _, _, public = self.filter(
            tmp_path, self.lines(said, other)[::order], self.EXCLUDED
        )
        assert not set(said) & set(public)
        assert set(public) == set(self.KEPT) | set(other)

    @pytest.mark.parametrize("order", [1, -1], ids=["in-order", "reversed"])
    def test_a_skolem_iri_is_an_unknown_value(self, tmp_path, order):
        # unknown values written as skolem IRIs, as the query service
        # writes them: they are read as blank nodes are
        genid = "<http://www.wikidata.org/.well-known/genid/"
        said = statement(
            "Q506",
            "dddd",
            "P40",
            f"{genid}aaaa>",
            qualifiers=[("P460", "", f"<{W}/entity/Q507>")],
            truthy=f"{genid}bbbb>",
        )
        other = statement(
            "Q506", "eeee", "P40", f"{genid}cccc>", truthy=f"{genid}dddd>"
        )
        _, _, public = self.filter(
            tmp_path, self.lines(said, other)[::order], self.EXCLUDED
        )
        assert not set(said) & set(public)
        assert set(public) == set(self.KEPT) | set(other[:-1])

    @pytest.mark.parametrize("order", [1, -1], ids=["in-order", "reversed"])
    @pytest.mark.parametrize(
        ("other", "stays"),
        [
            (None, False),
            # a kept statement of no value of the same property
            (statement("Q501", "ffff", "P40", None), True),
            # a kept statement's qualifier of no value, of that property:
            # a qualifier gives its statement's subject no truthy triple
            (
                statement(
                    "Q501",
                    "ffff",
                    "P1449",
                    '"Rudi"@de',
                    qualifiers=[("P40", "", None)],
                ),
                False,
            ),
            # a kept child, Leopold, under a qualifier of no value of the
            # same property: that is no statement of no value
            (
                statement(
                    "Q501",
                    "ffff",
                    "P40",
                    f"<{W}/entity/Q505>",
                    qualifiers=[("P40", "", None)],
                ),
                False,
            ),
        ],
        ids=["alone", "kept-twin", "kept-qualifier", "kept-own-qualifier"],
    )
    def test_a_statement_of_no_value_takes_its_truthy_type(
        self, tmp_path, other, stays, order
    ):
        # Rudolf had no child, under a qualifier naming Nina: the
        # statement goes, and with it the truthy type its "no value" gives
        # him, unless a kept statement of no value of P40 gives it too
        none = statement(
            "Q501",
            "eeee",
            "P40",
            None,
            qualifiers=[("P50", "", f"<{W}/entity/Q502>")],
        )
        _, _, public = self.filter(
            tmp_path, self.lines(none, other or [])[::order], self.EXCLUDED
        )
        typed = f"<{W}/entity/Q501> {RDF_TYPE} <{W}/prop/novalue/P40> ."
        assert none[-1] == typed
        assert not set(none[:-1]) & set(public)
        assert (typed in public) is stays

    @pytest.mark.parametrize(
        "value", [f"<{W}/entity/Q501>", None], ids=["a-value", "no-value"]
    )
    def test_a_propertys_statement_goes_whole_too(self, tmp_path, value):
        # an example of the spouse property's use, Rudolf, under a
        # qualifier naming Nina: every line of it goes, the truthy triple
        # it gave the property included, as an item's statement would
        example = statement(
            "P26",
            "aaaa",
            "P1855",
            value,
            qualifiers=[("P26", "", f"<{W}/entity/Q502>")],
        )
        _, _, public = self.filter(
            tmp_path, self.lines(example), self.EXCLUDED
        )
        assert not set(example) & set(public)
        assert set(public) == set(self.KEPT)

    def test_nothing_goes_without_exclusions(self, tmp_path):
        kept, dropped, public = self.filter(tmp_path, self.lines(), set())
        assert (kept, dropped) == (len(self.lines()), 0)


class TestWhatTheFilterReads:
    """The filter reads each line as a triple dumpRdf.php writes, and drops
    a line it cannot read (#363). Text leaves as it is: whatever is
    recorded about what the export keeps, a released person's text
    included (owner, 2026-09-29). The judgement notes' properties, P21 and
    P22 here, go in any form (ADR-0035)."""

    # lines that leave as they are: a label under each of its three names,
    # a description that names a possibly living relative, an alias, a
    # name as a statement's value, a string qualifier with quotes, a
    # reference's page number and excerpt, a date dumpRdf.php could not
    # type, a literal of any other type, the header's version, blank
    # nodes, and a property whose id begins as a note's does
    READABLE = [
        f'<{W}/entity/Q300> {RDFS_LABEL} "Gustav L\\u00F6big"@de .',
        f'<{W}/entity/Q300> {PREF_LABEL} "Gustav L\\u00F6big"@de .',
        f'<{W}/entity/Q300> {NAME} "Gustav L\\u00F6big"@de .',
        f'<{W}/entity/Q300> {DESCRIPTION} "father of Nicholas Lobig, born 1950"@en .',  # noqa: E501
        f'<{W}/entity/Q300> {ALT_LABEL} "Gustl"@de-ch .',
        f'<{W}/entity/statement/Q300-bbbb> <{W}/prop/statement/P10> "Gustav L\\u00F6big"@de .',  # noqa: E501
        f'<{W}/entity/statement/Q300-bbbb> <{W}/prop/qualifier/P11> "son of \\"Wastl\\"" .',  # noqa: E501
        f"<{W}/entity/statement/Q300-bbbb> {DERIVED_FROM} <{W}/reference/{REF_TEXT}> .",  # noqa: E501
        f'<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P13> "6" .',
        f'<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P3> "Gustav, Vater von Nicholas Lobig"@de .',  # noqa: E501
        f"<{W}/entity/statement/Q300-bbbb> {DERIVED_FROM} <{W}/reference/{REF_RUN}> .",  # noqa: E501
        f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/P20> <{W}/entity/Q302> .",  # noqa: E501
        f'<{W}/reference/{REF_RUN}> <{W}/prop/reference/P220> "kept" .',
        f'<{W}/entity/Q300> <{W}/prop/direct/P5> "+1950-00-00T00:00:00Z" .',
        f'<{W}/entity/Q300> <{W}/prop/direct/P16> "x"^^<http://www.w3.org/1998/Math/MathML> .',  # noqa: E501
        '<http://wikiba.se/ontology#Dump> <http://schema.org/softwareVersion> "1.0.0" .',  # noqa: E501
        f"<{W}/prop/novalue/P10> <http://www.w3.org/2002/07/owl#complementOf> _:genid1 .",  # noqa: E501
        f"_:genid1 <http://www.w3.org/2002/07/owl#onProperty> <{W}/prop/direct/P10> .",  # noqa: E501
    ]
    # the judgement notes, in every form one could take: a category and a
    # sentence in a run's reference, as ADR-0035 writes them, a
    # qualifier, a statement's link, its value and its truthy form
    NOTES = [
        f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/P21> <{W}/entity/Q303> .",  # noqa: E501
        f'<{W}/reference/{REF_RUN}> <{W}/prop/reference/P22> "took the Nicholas of the letter for Nicholas Lobig"@en .',  # noqa: E501
        f'<{W}/entity/statement/Q300-bbbb> <{W}/prop/qualifier/P22> "a note as a qualifier" .',  # noqa: E501
        f"<{W}/entity/Q300> <{W}/prop/P22> <{W}/entity/statement/Q300-eeee> .",
        f'<{W}/entity/statement/Q300-eeee> <{W}/prop/statement/P22> "a note as a value" .',  # noqa: E501
        f'<{W}/entity/Q300> <{W}/prop/direct/P22> "a note as a value" .',
    ]
    # lines that are no triple as dumpRdf.php writes one: tab-separated,
    # spaced twice, without the closing dot, a bare text, and one line
    # that a raw carriage return runs through, read as one
    UNREADABLE = [
        f'<{W}/entity/Q300>\t{RDFS_LABEL}\t"Nicholas Lobig"@en .',
        f'<{W}/entity/Q300>  {RDFS_LABEL} "Nicholas Lobig"@en .',
        f'<{W}/entity/Q300> {RDFS_LABEL} "Nicholas Lobig"@en',
        "Nicholas Lobig",
        f'<{W}/entity/Q300> {ALT_LABEL} "Nicholas\rLobig"@en .',
    ]

    def filter(self, tmp_path: Path, dropped_pids) -> tuple[int, int, list]:
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write(
                "\n".join(self.READABLE + self.NOTES + self.UNREADABLE) + "\n"
            )
        kept, dropped = filter_nonprivileged_export(
            full, public, set(), dropped_pids
        )
        with gzip.open(public, "rt", encoding="utf-8", newline="\n") as handle:
            return kept, dropped, handle.read().split("\n")[:-1]

    @staticmethod
    def in_blocks(lines: list[str]) -> list[str]:
        """`lines` as the filter writes them, all of one block: each
        reference node's lines together at the block's end, in the order
        the nodes first come (#412)."""
        nodes: dict[str, list[str]] = {}
        rest = []
        for line in lines:
            subject = line.split(" ", 1)[0]
            if subject.startswith(f"<{W}/reference/"):
                nodes.setdefault(subject, []).append(line)
            else:
                rest.append(line)
        return rest + [line for node in nodes.values() for line in node]

    def test_text_leaves_as_it_is_and_the_notes_go(self, tmp_path):
        kept, dropped, lines = self.filter(tmp_path, {"P21", "P22"})
        assert lines == self.in_blocks(self.READABLE)
        assert (kept, dropped) == (
            len(self.READABLE),
            len(self.NOTES) + len(self.UNREADABLE),
        )

    def test_a_line_it_cannot_read_is_dropped_whatever_it_holds(
        self, tmp_path
    ):
        # with no property dropped, the notes leave, and only the lines it
        # cannot read go: the one a carriage return runs through as one
        kept, dropped, lines = self.filter(tmp_path, frozenset())
        assert lines == self.in_blocks(self.READABLE + self.NOTES)
        assert dropped == len(self.UNREADABLE)

    def test_no_property_of_the_vocabulary_takes_a_link(self):
        # the export is made to filter the types the model uses; a URL, a
        # media file or a shape would make it refuse to run
        assert {
            spec["datatype"] for spec in MANIFEST["properties"].values()
        } <= READABLE_DATATYPES

    def test_the_readable_types_are_the_manifests(self):
        # as the dump names them (wikibase:propertyType)
        assert {
            f"<{ONTOLOGY}{rdf_type(datatype)}>"
            for datatype in READABLE_DATATYPES
        } == backup._MODEL_TYPES


# -- each kept node leaves in a block that names it (#412) --------------------

CALENDAR = "<http://www.wikidata.org/entity/Q1985727>"
REF_LETTER = "9" * 40  # the 1958 letter, which Nina's and Rudolf's cite
VALUE_1958 = "a1" * 20  # the letter's date, a value node only it names
VALUE_DEATH = "b2" * 20  # Rudolf's death, 6 March 1941
REF_TALK = "c3" * 20  # the 2002 interview, which Gustav's and Leopold's cite
VALUE_2002 = "d4" * 20  # the interview's date, a value node only it names


def entity_block(qid: str, label: str) -> list[str]:
    """An entity's first lines as dumpRdf.php writes them, under `W`:
    its data node, which starts its block, its type and its label."""
    data = f"<{W}/wiki/Special:EntityData/{qid}>"
    return [
        f"{data} {RDF_TYPE} <http://schema.org/Dataset> .",
        f"{data} <http://schema.org/about> <{W}/entity/{qid}> .",
        f'{data} <http://schema.org/version> "{qid[1:]}"^^<{XSD}integer> .',
        f"<{W}/entity/{qid}> {RDF_TYPE} <{ONTOLOGY}Item> .",
        f'<{W}/entity/{qid}> {RDFS_LABEL} "{nt(label)}"@en .',
    ]


OWL = "http://www.w3.org/2002/07/owl#"
PREDICATES = {
    "claim": "prop",
    "statementProperty": "prop/statement",
    "statementValue": "prop/statement/value",
    "qualifier": "prop/qualifier",
    "qualifierValue": "prop/qualifier/value",
    "reference": "prop/reference",
    "referenceValue": "prop/reference/value",
    "directClaim": "prop/direct",
}


def property_declarations(base: str, pid: str) -> list[str]:
    """A property's predicates as dumpRdf.php declares them in its block:
    each named by the property and typed an owl:ObjectProperty, and its
    "no value" class, under `base`."""
    novalue = f"<{base}/prop/novalue/{pid}>"
    return [
        *(
            f"<{base}/entity/{pid}> <{ONTOLOGY}{term}> <{base}/{path}/{pid}> ."
            for term, path in PREDICATES.items()
        ),
        f"<{base}/entity/{pid}> <{ONTOLOGY}novalue> {novalue} .",
        *(
            f"<{base}/{path}/{pid}> {RDF_TYPE} <{OWL}ObjectProperty> ."
            for path in PREDICATES.values()
        ),
        f"{novalue} {RDF_TYPE} <{OWL}Class> .",
        f"{novalue} <{OWL}complementOf> _:novalue{pid} .",
        f"_:novalue{pid} {RDF_TYPE} <{OWL}Restriction> .",
        f"_:novalue{pid} <{OWL}onProperty> <{base}/prop/direct/{pid}> .",
        f"_:novalue{pid} <{OWL}someValuesFrom> <{OWL}Thing> .",
    ]


def time_value(node: str, day: str) -> list[str]:
    """A time value node as dumpRdf.php writes one, to the day."""
    value = f"<{W}/value/{node}>"
    return [
        f"{value} {RDF_TYPE} <{ONTOLOGY}TimeValue> .",
        f'{value} <{ONTOLOGY}timeValue> "{day}T00:00:00Z"{TIME} .',
        f'{value} <{ONTOLOGY}timePrecision> "11"^^<{XSD}integer> .',
        f'{value} <{ONTOLOGY}timeTimezone> "0"^^<{XSD}integer> .',
        f"{value} <{ONTOLOGY}timeCalendarModel> {CALENDAR} .",
    ]


def source(node: str, excerpt: str, day: str, dated: str) -> list[str]:
    """A reference node with an excerpt (P8) and its source's date
    (P577), the date's value node `dated` beside it."""
    reference = f"<{W}/reference/{node}>"
    return [
        f"{reference} {RDF_TYPE} <{ONTOLOGY}Reference> .",
        f'{reference} <{W}/prop/reference/P8> "{nt(excerpt)}" .',
        f'{reference} <{W}/prop/reference/P577> "{day}T00:00:00Z"{TIME} .',
        f"{reference} <{W}/prop/reference/value/P577> <{W}/value/{dated}> .",
        *time_value(dated, day),
    ]


class TestEachNodeLeavesInABlockThatNamesIt:
    """The query service's munger reads a dump entity by entity, and keeps
    a value or reference node only in the block of an entity whose lines
    name it; dumpRdf.php writes each such node once, in the block of the
    first entity that uses it. When the export leaves that entity, or its
    statement, out, the node moves to the first block kept that names it,
    once (#412). `tests/munger_blocks.py` mirrors the munger.

    Koshnovka (Q510), where Rudolf Löbig (Q501) lived, comes first. Nina
    Sartina (Q502, of no known dates, left out) wrote to Leopold in 1958
    (REF_LETTER) as the widow of his late brother Rudolf: her spouse
    statement, ended by his death on 6 March 1941 (VALUE_DEATH), is the
    first to cite the letter and to name that day, so dumpRdf.php writes
    both in her block; Rudolf's death, from the same letter, follows in
    his. Gustav Löbig (Q506) was "Dad" to Nicholas Lobig (Q507, left
    out), as the interview Nicholas gave in 2002 (REF_TALK) says; it
    names Leopold Löbig (Q505) "Dad's cousin Leopold" too, a relative
    (P1038) of Gustav as a cousin (P1039). Gustav's appellation, whose
    perspective is Nicholas, is the first to cite the interview, and goes
    whole; Leopold's statement keeps it."""

    EXCERPT_LETTER = "вдова Вашего покойного брата Рудольфа"
    PLACE = entity_block("Q510", "Koshnovka")
    NINA = [
        *entity_block("Q502", "Nina Sartina"),
        *statement(
            "Q502",
            "aaaa",
            "P26",
            f"<{W}/entity/Q501>",
            qualifiers=[
                ("P582", "", f'"1941-03-06T00:00:00Z"{TIME}'),
                ("P582", "value/", f"<{W}/value/{VALUE_DEATH}>"),
            ],
            reference=REF_LETTER,
        ),
        *time_value(VALUE_DEATH, "1941-03-06"),
        *source(REF_LETTER, EXCERPT_LETTER, "1958-12-25", VALUE_1958),
    ]
    RUDOLF = [
        *entity_block("Q501", "Rudolf Löbig"),
        *statement(
            "Q501",
            "aaaa",
            "P570",
            f'"1941-03-06T00:00:00Z"{TIME}',
            reference=REF_LETTER,
        ),
        f"<{W}/entity/statement/Q501-aaaa> <{W}/prop/statement/value/P570> "
        f"<{W}/value/{VALUE_DEATH}> .",
    ]
    GUSTAV = [
        *entity_block("Q506", "Gustav Löbig"),
        *statement(
            "Q506",
            "aaaa",
            "P1810",
            '"Dad"@en',
            qualifiers=[("P1811", "", f"<{W}/entity/Q507>")],
            reference=REF_TALK,
        ),
        *source(REF_TALK, "Dad's cousin Leopold", "2002-01-01", VALUE_2002),
    ]
    LEOPOLD = [
        *entity_block("Q505", "Leopold Löbig"),
        *statement(
            "Q505",
            "aaaa",
            "P1038",
            f"<{W}/entity/Q506>",
            qualifiers=[("P1039", "", '"cousin"@en')],
            reference=REF_TALK,
        ),
    ]
    HEADER = [
        f"<{ONTOLOGY}Dump> {RDF_TYPE} <http://schema.org/Dataset> .",
        f'<{ONTOLOGY}Dump> <http://schema.org/softwareVersion> "1.0.0" .',
    ]
    LINES = [*HEADER, *PLACE, *NINA, *RUDOLF, *GUSTAV, *LEOPOLD]
    EXCLUDED = {"Q502", "Q507"}

    @staticmethod
    def nodes(*hashes: str) -> list[str]:
        """The lines of the provenance nodes `hashes` in the dump."""
        return [
            line
            for line in TestEachNodeLeavesInABlockThatNamesIt.LINES
            if any(
                line.split(" ", 1)[0].endswith(f"/{node}>") for node in hashes
            )
        ]

    def filter(self, tmp_path: Path) -> tuple[int, int, list[str]]:
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        kept, dropped = filter_nonprivileged_export(
            full, public, self.EXCLUDED
        )
        with gzip.open(public, "rt", encoding="utf-8", newline="\n") as handle:
            return kept, dropped, handle.read().split("\n")[:-1]

    @staticmethod
    def block_of(lines: list[str], qid: str) -> list[str]:
        (block,) = (
            block
            for block in blocks(lines)
            if f"<{W}/wiki/Special:EntityData/{qid}> " in block[0]
        )
        return block

    def test_the_dump_is_as_the_munger_reads_it(self):
        # the control: dumpRdf.php's layout, each node once, in the block
        # of the first entity that names it
        assert misplaced(self.LINES) == []
        assert len(self.nodes(VALUE_DEATH, REF_LETTER, VALUE_1958)) == 14
        assert len(self.nodes(REF_TALK, VALUE_2002)) == 9
        assert set(self.nodes(VALUE_DEATH, REF_LETTER)) <= set(self.NINA)
        assert set(self.nodes(REF_TALK, VALUE_2002)) <= set(self.GUSTAV)

    def test_the_munger_would_keep_every_line_of_the_export(self, tmp_path):
        _, _, lines = self.filter(tmp_path)
        assert misplaced(lines) == []

    def test_a_node_first_written_for_one_left_out_moves(self, tmp_path):
        # the letter, its date and Rudolf's death leave in his block, as
        # one line each, and nothing of Nina's leaves
        _, _, lines = self.filter(tmp_path)
        moved = self.nodes(VALUE_DEATH, REF_LETTER, VALUE_1958)
        rudolf = self.block_of(lines, "Q501")
        assert set(moved) <= set(rudolf)
        assert all(lines.count(line) == 1 for line in moved)
        assert not any("Q502" in line for line in lines)
        assert not any(self.EXCERPT_LETTER in line for line in self.PLACE)
        assert set(self.block_of(lines, "Q510")) == set(self.PLACE)

    def test_a_node_whose_first_statement_goes_moves_to_the_next(
        self, tmp_path
    ):
        # the interview and its date leave in Leopold's block, once, not in
        # Gustav's, whose statement citing it went whole
        _, _, lines = self.filter(tmp_path)
        moved = self.nodes(REF_TALK, VALUE_2002)
        assert set(moved) <= set(self.block_of(lines, "Q505"))
        assert not set(moved) & set(self.block_of(lines, "Q506"))
        assert all(lines.count(line) == 1 for line in moved)
        assert not any("Q506-aaaa" in line for line in lines)

    @classmethod
    def gone(cls) -> list[str]:
        """The lines the export leaves out: Nina's, and the appellation's,
        with the truthy triple it gave."""
        return [
            line for line in cls.LINES if "Q502" in line or "Q506-aaaa" in line
        ] + [f'<{W}/entity/Q506> <{W}/prop/direct/P1810> "Dad"@en .']

    def test_the_lines_kept_are_those_left_in_place(self, tmp_path):
        # only where the nodes' lines leave changes: the export keeps every
        # line but Nina's, and the appellation's, with the truthy triple it
        # gave, and counts each line once
        kept, dropped, lines = self.filter(tmp_path)
        gone = self.gone()
        assert set(gone) <= set(self.LINES)
        assert sorted(lines) == sorted(set(self.LINES) - set(gone))
        assert (kept, dropped) == (len(lines), len(self.LINES) - len(lines))

    def test_a_node_that_sits_in_a_block_that_names_it_stays_there(
        self, tmp_path
    ):
        # with no one left out, each node stays in the block it sits in,
        # at its end, and every line leaves
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        assert filter_nonprivileged_export(full, public, set()) == (
            len(self.LINES),
            0,
        )
        assert misplaced_in(public) == []
        with gzip.open(public, "rt", encoding="utf-8") as handle:
            lines = handle.read().split("\n")[:-1]
        nina = self.block_of(lines, "Q502")
        assert nina[
            -len(self.nodes(VALUE_DEATH, REF_LETTER, VALUE_1958)) :
        ] == (self.nodes(VALUE_DEATH, REF_LETTER, VALUE_1958))
        assert sorted(lines) == sorted(self.LINES)

    def test_a_node_repeated_in_the_dump_leaves_once(self, tmp_path):
        # a node's lines that the dump repeats in a later block leave once,
        # the repeats counted as dropped
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        repeated = self.nodes(REF_TALK, VALUE_2002)
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(self.LINES + repeated) + "\n")
        kept, dropped = filter_nonprivileged_export(full, public, set())
        with gzip.open(public, "rt", encoding="utf-8") as handle:
            lines = handle.read().split("\n")[:-1]
        assert (kept, dropped) == (len(self.LINES), len(repeated))
        assert misplaced(lines) == []
        assert set(repeated) <= set(self.block_of(lines, "Q506"))

    def test_a_full_dump_of_the_fixture_is_as_the_munger_reads_it(
        self, tmp_path, vocabulary
    ):
        # the control for TestExportNonprivileged, which checks each
        # export it writes
        dump = export_dump(vocabulary)
        assert misplaced_in(dump.write(tmp_path / "full.nt.gz")) == []
        # ...and the checker finds what a dump lays out wrong: a statement
        # node, and a reference, in another entity's block
        assert misplaced(dump.lines)

    def test_the_checker_finds_each_node_left_where_it_sat(self):
        # the kept lines in dump order, nothing moved: the letter, its date
        # and Rudolf's death sit in Koshnovka's block, which names none of
        # them, and the interview and its date in Gustav's, whose statement
        # citing it went; the munger would drop each of their lines
        gone = set(self.gone())
        unmoved = [line for line in self.LINES if line not in gone]
        letter = self.nodes(VALUE_DEATH, REF_LETTER, VALUE_1958)
        talk = self.nodes(REF_TALK, VALUE_2002)
        assert sorted(misplaced(unmoved)) == sorted(
            [f"<{W}/entity/Q510>: {line}" for line in letter]
            + [f"<{W}/entity/Q506>: {line}" for line in talk]
        )


class TestTheMungerBlockChecker:
    """`tests/munger_blocks.py` flags, one rule at a time, what the munger
    would drop, and keeps what it would keep (#412)."""

    VALUE = f"<{W}/value/{VALUE_1958}>"
    REFERENCE = f"<{W}/reference/{REF_LETTER}>"
    NODE = f"<{W}/entity/statement/Q501-aaaa>"

    def lines(self, first: list[str], second: list[str]) -> list[str]:
        """Rudolf's block, then Leopold's, each with lines of its own."""
        return [
            *entity_block("Q501", "Rudolf Löbig"),
            *first,
            *entity_block("Q505", "Leopold Löbig"),
            *second,
        ]

    def statement(self, *more: str) -> list[str]:
        return [
            f"<{W}/entity/Q501> <{W}/prop/P570> {self.NODE} .",
            f'{self.NODE} <{W}/prop/statement/P570> "1941-03-06T00:00:00Z"'
            f"{TIME} .",
            *more,
        ]

    def test_a_property_block_declaring_its_predicates_is_kept(self):
        lines = [
            *entity_block("P577", "publication date"),
            *property_declarations(W, "P577"),
        ]
        assert misplaced(lines) == []

    def test_a_value_named_only_by_a_reference_elsewhere_is_flagged(self):
        reference = self.statement(
            f"{self.NODE} {DERIVED_FROM} {self.REFERENCE} .",
            f"{self.REFERENCE} <{W}/prop/reference/value/P577> {self.VALUE} .",
        )
        value = time_value(VALUE_1958, "1958-12-25")
        assert misplaced(self.lines(reference, value)) == [
            f"<{W}/entity/Q505>: {line}" for line in value
        ]
        assert misplaced(self.lines(reference + value, [])) == []

    def test_a_reference_named_only_by_a_statement_elsewhere_is_flagged(
        self,
    ):
        cited = self.statement(
            f"{self.NODE} {DERIVED_FROM} {self.REFERENCE} ."
        )
        reference = [
            f"{self.REFERENCE} {RDF_TYPE} <{ONTOLOGY}Reference> .",
            f'{self.REFERENCE} <{W}/prop/reference/P8> "вдова" .',
        ]
        assert misplaced(self.lines(cited, reference)) == [
            f"<{W}/entity/Q505>: {line}" for line in reference
        ]
        assert misplaced(self.lines(cited + reference, [])) == []

    def test_a_statement_no_entity_names_is_flagged(self):
        statement = self.statement()
        assert misplaced(self.lines(statement[1:], [])) == [
            f"<{W}/entity/Q501>: {statement[1]}"
        ]

    def test_a_normalized_quantity_is_kept_beside_its_value(self):
        normalized = f"<{W}/value/{VALUE_2002}>"
        quantity = self.statement(
            f"{self.NODE} <{W}/prop/statement/value/P1100> {self.VALUE} .",
            f"{self.VALUE} {RDF_TYPE} <{ONTOLOGY}QuantityValue> .",
            f"{self.VALUE} <{ONTOLOGY}quantityNormalized> {normalized} .",
            f"{normalized} {RDF_TYPE} <{ONTOLOGY}QuantityValue> .",
        )
        assert misplaced(self.lines(quantity, [])) == []
        assert misplaced(self.lines(quantity[:-1], quantity[-1:])) == [
            f"<{W}/entity/Q505>: {quantity[-1]}"
        ]

    def test_a_reference_of_a_statement_no_entity_names_is_kept(self):
        # the munger registers a reference a statement node derives from
        # before it knows the statement; the statement alone it drops
        statement = self.statement(
            f"{self.NODE} {DERIVED_FROM} {self.REFERENCE} .",
            f"{self.REFERENCE} {RDF_TYPE} <{ONTOLOGY}Reference> .",
        )
        assert misplaced(self.lines(statement[1:], [])) == [
            f"<{W}/entity/Q501>: {line}" for line in statement[1:3]
        ]


class TestPruning:
    def test_prunes_oldest_beyond_keep(self, tmp_path):
        for stamp in (
            "20260101T000000Z",
            "20260201T000000Z",
            "20260301T000000Z",
        ):
            (tmp_path / stamp / "x").mkdir(parents=True)
        (tmp_path / "not-a-backup").mkdir()
        doomed = prune_old_backups(tmp_path, keep=2)
        assert doomed == ["20260101T000000Z"]
        assert not (tmp_path / "20260101T000000Z").exists()
        assert (tmp_path / "20260301T000000Z").exists()
        assert (tmp_path / "not-a-backup").exists()

    def test_keep_zero_prunes_nothing(self, tmp_path):
        (tmp_path / "20260101T000000Z").mkdir()
        assert prune_old_backups(tmp_path, keep=0) == []
        assert (tmp_path / "20260101T000000Z").exists()


# -- the non-privileged export keeps only released people (#344) -----------

DATA = f"{CONCEPT}/wiki/Special:EntityData/"


def entity(qid: str) -> str:
    return f"<{CONCEPT}/entity/{qid}>"


def dated(year: int) -> str:
    return f'"{year}-01-01T00:00:00Z"^^<{XSD}dateTime>'


# the entity a line's subject is of: its data node, itself or its
# statement's node
OWNER = re.compile(
    r"/(?:wiki/Special:EntityData/|entity/(?:statement/)?)([QP][1-9]\d*)\b"
)


class Dump:
    """A fictional dump in the N-Triples dumpRdf.php writes: its header;
    each entity's data node with its revision, its type, its label under
    each of the three names dumpRdf.php gives one, its description and
    aliases; each statement through its node, with its rank, value,
    qualifiers and reference (and the truthy triple of a best-ranked one);
    and each redirect as owl:sameAs its target."""

    def __init__(self, vocabulary):
        self.properties, self.items = vocabulary
        self.lines: list[str] = []
        self.revisions: dict[str, int] = {}

    def header(self) -> None:
        dump = f"<{ONTOLOGY}Dump>"
        self.lines += [
            f"{dump} {RDF_TYPE} <http://schema.org/Dataset> .",
            f'{dump} <http://schema.org/softwareVersion> "1.0.0" .',
            f"{dump} <http://schema.org/dateModified> {dated(2026)} .",
        ]

    def data_node(self, qid: str, revision: int) -> None:
        node = f"<{DATA}{qid}>"
        self.lines += [
            f"{node} {RDF_TYPE} <http://schema.org/Dataset> .",
            f"{node} <http://schema.org/about> {entity(qid)} .",
            f'{node} <http://schema.org/version> "{revision}"'
            f"^^<{XSD}integer> .",
        ]

    def entity(
        self,
        qid: str,
        label: str,
        revision: int = 1,
        description: str | None = None,
        aliases=(),
        kind: str = "Item",
    ) -> None:
        self.data_node(qid, revision)
        self.revisions[qid] = revision
        self.lines += [
            f"{entity(qid)} {RDF_TYPE} <{ONTOLOGY}{kind}> .",
            *(f'{entity(qid)} {term} "{nt(label)}"@en .' for term in LABELS),
            *(
                [f'{entity(qid)} {DESCRIPTION} "{nt(description)}"@en .']
                if description
                else []
            ),
            *(
                f'{entity(qid)} {ALT_LABEL} "{nt(alias)}"@en .'
                for alias in aliases
            ),
        ]

    def says(
        self,
        subject: str,
        key: str,
        value: str | None,
        rank="Normal",
        best=None,
        qualifiers=(),
        reference=(),
    ) -> None:
        """A statement of `key` about `subject`, with its `qualifiers` and
        the snaks of its `reference` (key, value pairs); `value` None
        states "no value", which gives the statement no ps: triple."""
        pid = self.properties[key]
        node = f"<{CONCEPT}/entity/statement/{subject}-{len(self.lines):04x}>"
        self.lines += [
            f"{entity(subject)} <{CONCEPT}/prop/{pid}> {node} .",
            f"{node} {RDF_TYPE} <{ONTOLOGY}Statement> .",
            f"{node} <{ONTOLOGY}rank> <{ONTOLOGY}{rank}Rank> .",
            f"{node} <{CONCEPT}/prop/statement/{pid}> {value} ."
            if value is not None
            else f"{node} {RDF_TYPE} <{CONCEPT}/prop/novalue/{pid}> .",
            *(
                f"{node} <{CONCEPT}/prop/qualifier/"
                f"{self.properties[qualifier]}> {qualified} ."
                for qualifier, qualified in qualifiers
            ),
        ]
        if reference:
            digest = hashlib.sha1(repr(reference).encode()).hexdigest()
            source = f"<{CONCEPT}/reference/{digest}>"
            self.lines += [
                f"{node} {DERIVED_FROM} {source} .",
                *(
                    f"{source} <{CONCEPT}/prop/reference/"
                    f"{self.properties[snak]}> {snaked} ."
                    for snak, snaked in reference
                ),
            ]
        if value is not None and (
            rank != "Deprecated" if best is None else best
        ):
            self.lines.append(
                f"{entity(subject)} <{CONCEPT}/prop/direct/{pid}> {value} ."
            )

    def redirect(self, source: str, target: str, revision: int = 1) -> None:
        self.data_node(source, revision)
        self.lines.append(
            f"{entity(source)} <http://www.w3.org/2002/07/owl#sameAs> "
            f"{entity(target)} ."
        )

    def shaped(self) -> list[str]:
        """The lines as dumpRdf.php lays them out: the header, then each
        entity's block, in the order the entities first come: its data
        node, its own lines and its statements' (#412). A reference's
        snaks go with the statement that cites it first, as one line each
        however many statements cite it."""
        header: list[str] = []
        blocks: dict[str | None, list[str]] = {}
        seen: set[str] = set()
        owner = None
        for line in self.lines:
            subject = line.split(" ", 1)[0]
            if subject == f"<{ONTOLOGY}Dump>":
                header.append(line)
                continue
            if found := OWNER.search(subject):
                owner = found.group(1)
            elif "/reference/" in subject:
                if line in seen:
                    continue
                seen.add(line)
            blocks.setdefault(owner, []).append(line)
        return header + [line for block in blocks.values() for line in block]

    def write(self, path: Path, lines=None) -> Path:
        """The dump, as `shaped` lays it out unless `lines` are given."""
        with gzip.open(path, "wt", encoding="utf-8") as sink:
            sink.write("\n".join(lines or self.shaped()) + "\n")
        return path


# The fictional export: items with person evidence, each read at the rank
# a curator left it; typed items without any, which the export keeps; and
# items that state no type, which it drops (#344).
PEOPLE = {
    "Q401": "typed human, with a death: the check releases them",
    "Q402": "typed human at deprecated rank only, with a religion",
    "Q403": "typed human, outranked by a preferred organization type",
    "Q404": "no type, with a birth",
    "Q405": "no type, the value of a spouse statement",
    "Q406": "no type, taking part in a death (the part deprecated)",
    "Q414": "typed as a place, with a nickname",
    "Q415": "no type, taking part in a deportation (so typed at deprecated "
    "rank only)",
    "Q424": "a vessel with a human type at deprecated rank, taking part in "
    "a migration",
    "Q425": "a vessel, so typed at deprecated rank only, taking part in a "
    "migration",
    "Q426": "an organization with a birth, taking part in a death",
    "Q427": "a military unit (preferred), the value of a spouse statement, "
    "taking part in military service",
    "Q428": "no type, taking part in a migration",
    "Q437": "a family taking part in a migration",
    "Q441": "no type, a mother's value in a qualifier",
    "Q442": "an organization, referred to by a term of address",
    "Q445": "no type, the value of a child statement",
}
OTHERS = {
    "Q408": "a place",
    "Q409": "a certificate",
    "Q411": "an organization taking part in an event of no person kind",
    "Q412": "an event of no person kind",
    "Q413": "an occupation",
    "Q423": "a vessel taking part in a migration",
    "Q436": "a place taking part in an event of no person kind",
    "Q438": "a curator's class, the type of Q439 and so a class",
    "Q439": "an item typed as a curator's class",
    "Q440": "the class Q438 is a subclass of",
    "Q466": "an artifact, A few pages of family history, by Nicholas Lobig",
}
# events that items the export drops take part in, which it drops too
# (#363): what an event records is its participants' as much as its own
EVENTS = {
    "Q407": "a death event, with Q406 (its part deprecated) and Q426 in it",
    "Q430": "a migration, with Q423 to Q425, Q428 and the family Q437 in it",
    "Q431": "a military service, with Q427 in it",
}
# each event's participants, at every rank
PARTICIPANTS = {
    "Q407": {"Q406", "Q426"},
    "Q412": {"Q411", "Q436"},
    "Q416": {"Q415"},
    "Q430": {"Q423", "Q424", "Q425", "Q428", "Q437"},
    "Q431": {"Q427"},
}
# families without person evidence, which the export drops all the same
FAMILIES = {
    "Q446": "a family, the perspective of an appellation",
    "Q461": "a family, typed through a redirect to the family class",
}
# items that state no type and carry no person evidence
UNTYPED = {
    "Q416": "a deportation, typed so at deprecated rank only",
    "Q429": "a label only",
    "Q433": "typed with an unknown value only",
    "Q434": "typed with no value only",
    "Q462": "named in an instance of qualifier, which makes no class",
    "Q463": "a family, typed so at deprecated rank only",
}
# Q410 redirects to Q402, whom the check does not release; Q418 to Q401,
# whom it does; Q435 to Q429, which states no type; Q464 to Q446, a
# family. A photograph depicts each: no person evidence of its own. Q465
# is the class a curator merged into the family class.
REDIRECTS = {"Q410": "Q402", "Q418": "Q401", "Q435": "Q429", "Q464": "Q446"}
# the families of the fixture: those above, and Q437, a person-like one
ALL_FAMILIES = set(FAMILIES) | {"Q437"}
# what the export drops when the check releases only Q401
DROPPED = (
    set(PEOPLE) - {"Q401"}
    | set(FAMILIES)
    | set(UNTYPED)
    | {"Q410", "Q435", "Q464"}
    | set(EVENTS)
)
# Texts that name Nicholas Lobig (born 1950, possibly living), whom no
# item of the fixture stands for. On what the export keeps they stay, as
# every text does there (owner, 2026-09-29; #363): a released person's
# description, an artifact's title, an event's description, a reference's
# excerpt and page number. On the family it leaves out, the description
# goes with it.
TEXTS_KEPT = [
    '"father of Nicholas Lobig, born 1950"@en',
    '"an artifact, A few pages of family history, by Nicholas Lobig"@en',
    '"a library talk by Nicholas Lobig, 2018"@en',
    '"Gustav, Vater von Nicholas"@de',
    '"2, beside Nicholas Lobig"',
]
TEXTS_DROPPED = ['"the Lobigs of Stupfield, Nicholas among them"@en']
# every item that states no type, people included
NO_TYPE = {
    "Q402",
    "Q404",
    "Q405",
    "Q406",
    "Q415",
    "Q425",
    "Q428",
    "Q441",
    "Q445",
} | set(UNTYPED)


def export_dump(vocabulary) -> Dump:
    properties, items = vocabulary
    manifest = load_manifest()
    dump = Dump(vocabulary)
    dump.header()
    # the vocabulary, with the terms and types the manifest gives it
    for key, pid in properties.items():
        spec = manifest.properties[key]
        dump.entity(
            pid,
            spec.label,
            description=spec.description,
            aliases=spec.aliases,
            kind="Property",
        )
        dump.lines += [
            f"{entity(pid)} <{ONTOLOGY}propertyType> "
            f"<{ONTOLOGY}{rdf_type(spec.datatype)}> .",
            *property_declarations(CONCEPT, pid),
        ]
    for key, qid in items.items():
        spec = manifest.items[key]
        dump.entity(
            qid, spec.label, description=spec.description, aliases=spec.aliases
        )
    # the texts that name Nicholas Lobig (TEXTS_KEPT, TEXTS_DROPPED)
    texts = {
        "Q401": {
            "description": "father of Nicholas Lobig, born 1950",
            "aliases": ["Gustave Lobig"],
        },
        "Q412": {"description": "a library talk by Nicholas Lobig, 2018"},
        "Q446": {
            "description": "the Lobigs of Stupfield, Nicholas among them"
        },
    }
    for qid, label in {
        **PEOPLE,
        **OTHERS,
        **EVENTS,
        **FAMILIES,
        **UNTYPED,
    }.items():
        dump.entity(qid, label, revision=int(qid[1:]), **texts.get(qid, {}))

    def typed(qid: str, key: str, **options) -> None:
        dump.says(qid, "instance_of", entity(items[key]), **options)

    typed("Q401", "human")
    dump.says("Q401", "date_of_death", dated(1996))
    dump.says(
        "Q401",
        "birth_name",
        '"Gustav Löbig"@de',
        reference=[
            ("stated_in", entity("Q466")),
            ("page_number", '"2, beside Nicholas Lobig"'),
            ("transcription_excerpt", '"Gustav, Vater von Nicholas"@de'),
        ],
    )
    typed("Q466", "artifact")
    dump.says(
        "Q466",
        "media_file",
        '"File:A few pages of family history, by Nicholas Lobig.png"',
    )
    dump.says("Q401", "sex_or_gender", entity(items["male"]))
    dump.says("Q401", "occupation", entity("Q413"))
    dump.says("Q401", "place_of_birth", entity("Q408"))
    dump.says("Q401", "spouse", entity("Q405"))
    dump.says("Q401", "spouse", entity("Q427"))
    dump.says("Q401", "father", "_:genid1")  # an unknown value
    dump.says(
        "Q401",
        "child",
        entity("Q445"),
        qualifiers=[("mother", entity("Q441"))],
    )
    typed("Q402", "human", rank="Deprecated")
    dump.says("Q402", "religion", '"lutherisch"@de')
    typed("Q403", "human", best=False)
    typed("Q403", "organization", rank="Preferred")
    dump.says("Q404", "date_of_birth", dated(1990))
    typed("Q407", "death")
    dump.says("Q407", "participant", entity("Q406"), rank="Deprecated")
    dump.says("Q407", "participant", entity("Q426"))
    typed("Q408", "place")
    typed("Q409", "certificate")
    for redirect in REDIRECTS:
        dump.says("Q409", "depicts", entity(redirect))
    typed("Q411", "organization")
    typed("Q412", "event")
    dump.says("Q412", "participant", entity("Q411"))
    typed(
        "Q413",
        "occupation_class",
        qualifiers=[("instance_of", entity("Q462"))],
    )
    typed("Q414", "place")
    dump.says("Q414", "nickname", '"Gus"@en')
    typed("Q416", "deportation", rank="Deprecated")
    dump.says("Q416", "participant", entity("Q415"))
    typed("Q423", "vessel")
    typed("Q424", "vessel")
    typed("Q424", "human", rank="Deprecated")
    typed("Q425", "vessel", rank="Deprecated")
    typed("Q426", "organization")
    dump.says("Q426", "date_of_birth", dated(1990))
    typed("Q427", "military_unit", rank="Preferred")
    typed("Q430", "migration")
    for participant in ("Q423", "Q424", "Q425", "Q428", "Q437"):
        dump.says("Q430", "participant", entity(participant))
    typed("Q431", "military_service")
    dump.says("Q431", "participant", entity("Q427"))
    typed("Q436", "place")
    dump.says("Q412", "participant", entity("Q436"))
    dump.says("Q438", "subclass_of", entity("Q440"))
    dump.says("Q439", "instance_of", entity("Q438"))
    typed("Q437", "family")
    typed("Q442", "organization")
    dump.says(
        "Q442",
        "referred_to_as",
        '"Nana Maisie"@en',
        qualifiers=[("perspective", entity("Q446"))],
    )
    typed("Q446", "family")
    dump.redirect("Q465", items["family"], revision=465)
    dump.says("Q461", "instance_of", entity("Q465"))
    typed("Q463", "family", rank="Deprecated")
    dump.says("Q433", "instance_of", "_:genid2")
    dump.says("Q434", "instance_of", None)
    for source, target in REDIRECTS.items():
        dump.redirect(source, target, revision=int(source[1:]))
    return dump


def evidence(vocabulary, key: str) -> str:
    """A value for a statement of `key`, by its datatype."""
    datatype = MANIFEST["properties"][key]["datatype"]
    return {
        "time": dated(1990),
        "wikibase-item": entity("Q499"),
        "monolingualtext": '"Fixture"@en',
        "string": '"Fixture"',
        "quantity": f'"+2"^^<{XSD}decimal>',
    }[datatype]


class TestReadDump:
    """The export reads person evidence from its own dump as the check's
    selection reads it at the query service: at every rank."""

    def reading(self, tmp_path, vocabulary, lines=None) -> DumpReading:
        properties, items = vocabulary
        full = export_dump(vocabulary).write(tmp_path / "full.nt.gz", lines)
        return read_dump(full, properties, items)

    def test_every_kind_of_person_evidence_at_every_rank(
        self, tmp_path, vocabulary
    ):
        found = self.reading(tmp_path, vocabulary)
        assert found.people == set(PEOPLE)

    def test_places_artifacts_events_and_vocabulary_are_no_people(
        self, tmp_path, vocabulary
    ):
        # nor is a family that carries no person evidence (an appellation's
        # perspective is none), though the export drops it as a family
        _, items = vocabulary
        found = self.reading(tmp_path, vocabulary)
        assert not found.people & (
            set(OTHERS) | set(EVENTS) | set(FAMILIES) | set(items.values())
        )

    @staticmethod
    def one(tmp_path, vocabulary, build, reverse=False) -> DumpReading:
        """What the dump `build` writes into gives, beside an item Q450
        typed as a place (so that it states a type) and the vocabulary;
        with its lines in reverse order when `reverse`."""
        properties, items = vocabulary
        dump = Dump(vocabulary)
        for key, qid in items.items():
            dump.entity(qid, key)
        for qid in ("Q450", "Q451", "Q499"):
            dump.entity(qid, qid)
        dump.says("Q450", "instance_of", entity(items["place"]))
        build(dump)
        lines = dump.lines[::-1] if reverse else None
        return read_dump(
            dump.write(tmp_path / "one.nt.gz", lines), properties, items
        )

    @pytest.mark.parametrize("key", DOCUMENTED_SUBJECT_EVIDENCE)
    def test_each_person_only_property_marks_its_subject(
        self, tmp_path, vocabulary, key
    ):
        found = self.one(
            tmp_path,
            vocabulary,
            lambda dump: dump.says("Q450", key, evidence(vocabulary, key)),
        )
        assert "Q450" in found.people

    @pytest.mark.parametrize("key", DOCUMENTED_VALUE_EVIDENCE)
    @pytest.mark.parametrize("where", ["statement", "qualifier"])
    def test_each_person_valued_property_marks_its_value(
        self, tmp_path, vocabulary, key, where
    ):
        def build(dump):
            if where == "statement":
                dump.says("Q451", key, entity("Q450"))
            else:
                dump.says(
                    "Q451",
                    "location",
                    entity("Q499"),
                    qualifiers=[(key, entity("Q450"))],
                )

        found = self.one(tmp_path, vocabulary, build)
        assert "Q450" in found.people

    @pytest.mark.parametrize("kind", DOCUMENTED_PERSON_EVENTS)
    def test_each_person_event_marks_its_participants(
        self, tmp_path, vocabulary, kind
    ):
        _, items = vocabulary

        def build(dump):
            dump.says("Q451", "instance_of", entity(items[kind]))
            dump.says("Q451", "participant", entity("Q450"))

        found = self.one(tmp_path, vocabulary, build)
        assert "Q450" in found.people

    def test_a_vessel_in_a_person_event_is_no_person_on_that_alone(
        self, tmp_path, vocabulary
    ):
        # Q423 is set aside; Q424 has a human type, if deprecated, Q425 a
        # vessel type only at deprecated rank, Q426 a birth, Q427 a
        # spouse's value; Q428 has no type, and Q437 is a family, which
        # sets no participant aside
        found = self.reading(tmp_path, vocabulary)
        assert "Q423" not in found.people
        assert {
            "Q424",
            "Q425",
            "Q426",
            "Q427",
            "Q428",
            "Q437",
        } <= found.people

    def test_items_that_state_no_type(self, tmp_path, vocabulary):
        # a type a curator deprecated, or an unknown or no value, is none;
        # the vocabulary, the classes items are typed with and redirects
        # are not counted
        _, items = vocabulary
        found = self.reading(tmp_path, vocabulary)
        assert found.untyped == NO_TYPE
        assert not found.untyped & (
            set(items.values()) | {"Q438", "Q440"} | set(REDIRECTS)
        )

    def test_the_lines_may_come_in_any_order(self, tmp_path, vocabulary):
        # a statement's value, qualifier or rank can come before the link
        # to its subject
        lines = export_dump(vocabulary).lines[::-1]
        found = self.reading(tmp_path, vocabulary, lines)
        assert found.people == set(PEOPLE)
        assert found.untyped == NO_TYPE
        assert found.participants == PARTICIPANTS

    def test_the_participants_of_each_event(self, tmp_path, vocabulary):
        # at every rank (Q406's part is deprecated), whatever the event's
        # type (Q412's is of no person kind) or whether it states one
        # (Q416's is deprecated)
        found = self.reading(tmp_path, vocabulary)
        assert found.participants == PARTICIPANTS

    def test_an_event_of_a_person_kind_that_names_no_participant(
        self, tmp_path, vocabulary
    ):
        # none, or only one of unknown value, as a claim refused, left
        # unconfirmed or cut short after the type leaves it; an event of
        # no person kind, or one that names an item, is attended (#363)
        _, items = vocabulary

        def build(dump):
            for qid in ("Q452", "Q453", "Q454", "Q455"):
                dump.entity(qid, qid)
            dump.says("Q452", "instance_of", entity(items["birth"]))
            dump.says("Q452", "point_in_time", dated(1950))
            dump.says("Q453", "instance_of", entity(items["death"]))
            dump.says("Q453", "participant", "_:genid9")
            dump.says("Q454", "instance_of", entity(items["birth"]))
            dump.says("Q454", "participant", entity("Q450"))
            dump.says("Q455", "instance_of", entity(items["event"]))

        found = self.one(tmp_path, vocabulary, build)
        assert found.unattended == {"Q452", "Q453"}
        # every person event of the fixture names someone
        assert self.reading(tmp_path, vocabulary).unattended == set()

    @pytest.mark.parametrize(
        "reverse", [False, True], ids=["in-order", "reversed"]
    )
    def test_an_item_a_participant_qualifier_names_takes_part(
        self, tmp_path, vocabulary, reverse
    ):
        # whatever the participant statement's value, an unknown one or
        # none included, and at any rank, in lines of any order; each item
        # its qualifiers name, a role beside another; an item named so is
        # no person on that alone, and one a qualifier of another
        # statement of the event names takes no part (#364)
        _, items = vocabulary
        principal = items["principal"]

        def build(dump):
            for qid in ("Q452", "Q453", "Q454", "Q455", "Q456", "Q457"):
                dump.entity(qid, qid)
            dump.says("Q452", "instance_of", entity(items["event"]))
            dump.says(
                "Q452",
                "participant",
                "_:genid9",
                qualifiers=[
                    ("perspective", entity("Q453")),
                    ("object_has_role", entity(principal)),
                ],
            )
            dump.says(
                "Q452",
                "location",
                entity("Q499"),
                qualifiers=[("perspective", entity("Q451"))],
            )
            dump.says("Q454", "instance_of", entity(items["birth"]))
            dump.says(
                "Q454",
                "participant",
                entity("Q450"),
                rank="Deprecated",
                qualifiers=[("perspective", entity("Q455"))],
            )
            dump.says("Q456", "instance_of", entity(items["event"]))
            dump.says(
                "Q456",
                "participant",
                None,
                qualifiers=[("perspective", entity("Q457"))],
            )

        found = self.one(tmp_path, vocabulary, build, reverse)
        assert found.participants == {
            "Q452": {"Q453", principal},
            "Q454": {"Q450", "Q455"},
            "Q456": {"Q457"},
        }
        assert "Q450" in found.people
        assert not {"Q451", "Q453", "Q455", "Q457", principal} & found.people

    @pytest.mark.parametrize(
        "reverse", [False, True], ids=["in-order", "reversed"]
    )
    def test_a_role_names_no_one_who_was_there(
        self, tmp_path, vocabulary, reverse
    ):
        # a person event whose one participant is of unknown value, with
        # the role it had, is still unattended, and the role no person
        # (#364)
        _, items = vocabulary
        principal = items["principal"]

        def build(dump):
            dump.entity("Q452", "Q452")
            dump.says("Q452", "instance_of", entity(items["death"]))
            dump.says(
                "Q452",
                "participant",
                "_:genid9",
                qualifiers=[("object_has_role", entity(principal))],
            )

        found = self.one(tmp_path, vocabulary, build, reverse)
        assert found.unattended == {"Q452"}
        assert found.participants == {"Q452": {principal}}
        assert principal not in found.people

    @pytest.mark.parametrize(
        "reverse", [False, True], ids=["in-order", "reversed"]
    )
    def test_an_event_whose_participants_are_all_unknown(
        self, tmp_path, vocabulary, reverse
    ):
        # an event of no person kind whose one participant is of unknown
        # value is unattended, whatever qualifies it; one that also names
        # Gustav (Q401) as a participant is not
        _, items = vocabulary

        def build(dump):
            for qid in ("Q401", "Q452", "Q453"):
                dump.entity(qid, qid)
            for event in ("Q452", "Q453"):
                dump.says(event, "instance_of", entity(items["event"]))
                dump.says(
                    event,
                    "participant",
                    "_:genid9",
                    qualifiers=[
                        ("object_has_role", entity(items["principal"]))
                    ],
                )
            dump.says("Q453", "participant", entity("Q401"))

        found = self.one(tmp_path, vocabulary, build, reverse)
        assert found.unattended == {"Q452"}

    @pytest.mark.parametrize(
        "reverse", [False, True], ids=["in-order", "reversed"]
    )
    def test_an_event_is_linked_to_what_its_statements_name(
        self, tmp_path, vocabulary, reverse
    ):
        # a talk carried out by a society (Q454), as a statement's value; a
        # wedding, a subclass of event in the manifest, at a place a
        # qualifier names (Q455); an item of a class the dump alone makes
        # a subclass of event, stated, at deprecated rank, in a book
        # (Q456); and a place, no event, part of Q455: only events are
        # linked
        properties, items = vocabulary

        def build(dump):
            for qid in ("Q401", "Q452", "Q453", "Q454", "Q455", "Q456"):
                dump.entity(qid, qid)
            dump.entity("Q457", "Q457")
            dump.says("Q457", "subclass_of", entity(items["event"]))
            dump.says("Q452", "instance_of", entity(items["event"]))
            dump.says("Q452", "carried_out_by", entity("Q454"))
            dump.says("Q453", "instance_of", entity(items["wedding"]))
            dump.says(
                "Q453",
                "point_in_time",
                dated(1938),
                qualifiers=[("location", entity("Q455"))],
            )
            dump.says("Q458", "instance_of", entity("Q457"))
            dump.says(
                "Q458",
                "stated_in",
                entity("Q456"),
                rank="Deprecated",
            )
            dump.says("Q450", "part_of", entity("Q455"))
            # a civil wedding, two subclasses down in the manifest
            dump.says("Q460", "instance_of", entity(items["civil_wedding"]))
            dump.says("Q460", "carried_out_by", entity("Q454"))
            # a class two subclasses down in the dump alone
            dump.says("Q459", "subclass_of", entity("Q457"))
            dump.says("Q461", "instance_of", entity("Q459"))
            dump.says("Q461", "carried_out_by", entity("Q454"))
            # a subclass of a redirect to `event`
            dump.redirect("Q462", items["event"])
            dump.says("Q463", "subclass_of", entity("Q462"))
            dump.says("Q464", "instance_of", entity("Q463"))
            dump.says("Q464", "carried_out_by", entity("Q454"))
            # an event typed so at deprecated rank alone
            dump.says(
                "Q465",
                "instance_of",
                entity(items["event"]),
                rank="Deprecated",
            )
            dump.says("Q465", "carried_out_by", entity("Q454"))
            # a subclass of event at deprecated rank alone
            dump.says(
                "Q466",
                "subclass_of",
                entity(items["event"]),
                rank="Deprecated",
            )
            dump.says("Q467", "instance_of", entity("Q466"))
            dump.says("Q467", "carried_out_by", entity("Q454"))
            # an event typed a marriage at deprecated rank alone is no union
            dump.says("Q468", "instance_of", entity(items["event"]))
            dump.says(
                "Q468",
                "instance_of",
                entity(items["marriage"]),
                rank="Deprecated",
            )
            dump.says("Q468", "carried_out_by", entity("Q454"))
            # a union of a class the dump alone makes a subclass of
            # partnership, and a property that is a subclass of nothing read
            dump.says("Q469", "subclass_of", entity(items["partnership"]))
            dump.says("Q470", "instance_of", entity("Q469"))
            dump.says("Q470", "participant", entity("Q401"))
            dump.says("Q470", "started_by", entity("Q453"))
            dump.says("P26", "subclass_of", entity(items["event"]))
            # a union of a class made a subclass of partnership at
            # deprecated rank alone: union classes are read at any rank
            dump.says(
                "Q471",
                "subclass_of",
                entity(items["partnership"]),
                rank="Deprecated",
            )
            dump.says("Q472", "instance_of", entity("Q471"))
            dump.says("Q472", "participant", entity("Q401"))
            dump.says("Q472", "carried_out_by", entity("Q454"))

        found = self.one(tmp_path, vocabulary, build, reverse)
        assert "Q454" in found.links["Q452"]
        assert "Q455" in found.links["Q453"]
        assert "Q456" in found.links["Q458"]
        assert "Q450" not in found.links
        for event in ("Q460", "Q461", "Q464", "Q465", "Q467", "Q468"):
            assert "Q454" in found.links[event], event
        assert "Q470" not in found.links
        assert "Q472" not in found.links

    def test_the_events_that_start_and_end_a_union(self, tmp_path, vocabulary):
        # by started_by and ended_by (ADR-0036), at every rank, once the
        # vocabulary holds them; a partner's own death is no bound of the
        # union's, as it follows the person rule (#363)
        properties, items = vocabulary
        bounded = (
            {**properties, "started_by": "P996", "ended_by": "P997"},
            items,
        )

        def build(dump):
            for qid in ("Q460", "Q461", "Q462", "Q463"):
                dump.entity(qid, qid)
            dump.says("Q460", "instance_of", entity(items["marriage"]))
            dump.says("Q460", "started_by", entity("Q461"), rank="Deprecated")
            dump.says("Q460", "ended_by", entity("Q462"))
            dump.says("Q460", "ended_by", entity("Q463"))
            dump.says("Q461", "instance_of", entity(items["event"]))
            dump.says("Q462", "instance_of", entity(items["event"]))
            dump.says("Q463", "instance_of", entity(items["death"]))

        found = self.one(tmp_path, bounded, build)
        assert found.bounds == {"Q460": {"Q461", "Q462"}}

    def test_a_line_ends_at_a_newline_alone(self, tmp_path, vocabulary):
        # a raw carriage return inside a text (dumpRdf.php escapes every
        # one) starts no line: nothing is read from within the text, a
        # forged redirect included (#363)
        forged = (
            f"{entity('Q451')} <http://www.w3.org/2002/07/owl#sameAs> "
            f"{entity('Q450')} ."
        )

        def build(dump):
            dump.lines.append(
                f'{entity("Q450")} {RDFS_LABEL} "x\r{forged}"@en .'
            )

        found = self.one(tmp_path, vocabulary, build)
        assert found.redirects == {}

    def test_property_types_and_sitelinks(self, tmp_path, vocabulary):
        # a property's type, as the dump names it, and what a sitelink's
        # page is about; an entity's data node is about it too, and is no
        # sitelink (#363)
        def build(dump):
            dump.entity("P900", "homepage", kind="Property")
            dump.lines += [
                f"{entity('P900')} <{ONTOLOGY}propertyType> <{ONTOLOGY}Url> .",
                "<https://el.example/wiki/Page> <http://schema.org/about> "
                f"{entity('Q450')} .",
            ]

        found = self.one(tmp_path, vocabulary, build)
        assert found.property_types == {"P900": f"<{ONTOLOGY}Url>"}
        assert found.sitelinks == {"Q450"}

    def test_the_fixtures_properties_are_all_readable(
        self, tmp_path, vocabulary
    ):
        properties, _ = vocabulary
        found = self.reading(tmp_path, vocabulary)
        assert set(found.property_types) == set(properties.values())
        assert backup.unfilterable(found) == []

    def test_redirects_and_revisions(self, tmp_path, vocabulary):
        _, items = vocabulary
        found = self.reading(tmp_path, vocabulary)
        assert found.redirects == {**REDIRECTS, "Q465": items["family"]}
        # the query service need not keep a redirect's revision
        assert found.revisions == export_dump(vocabulary).revisions
        assert not set(REDIRECTS) & set(found.revisions)

    def test_a_type_is_read_through_its_redirects(self, tmp_path, vocabulary):
        _, items = vocabulary

        def build(dump):
            # a curator merged a duplicate "person" class, Q452, into human
            dump.redirect("Q452", items["human"])
            dump.entity("Q453", "typed with the merged-away class")
            dump.says("Q453", "instance_of", entity("Q452"))
            # an event typed through a chain of redirects to death, whose
            # participant (the place Q450) is so a person
            dump.redirect("Q454", "Q455")
            dump.redirect("Q455", items["death"])
            dump.says("Q451", "instance_of", entity("Q454"))
            dump.says("Q451", "participant", entity("Q450"))
            # a vessel typed through a redirect, in that death: set aside
            dump.redirect("Q456", items["vessel"])
            dump.entity("Q457", "a vessel typed through a redirect")
            dump.says("Q457", "instance_of", entity("Q456"))
            dump.says("Q451", "participant", entity("Q457"))
            # a curator's class, untyped, named only through a redirect
            dump.entity("Q458", "a curator's class")
            dump.redirect("Q459", "Q458")
            dump.entity("Q460", "typed with the class's old id")
            dump.says("Q460", "instance_of", entity("Q459"))

        found = self.one(tmp_path, vocabulary, build)
        assert {"Q453", "Q450"} <= found.people
        assert "Q457" not in found.people
        assert "Q458" not in found.untyped

    def test_families(self, tmp_path, vocabulary):
        # typed at normal or preferred rank, through a redirect too; one
        # typed so only at deprecated rank states no type
        found = self.reading(tmp_path, vocabulary)
        assert found.families == ALL_FAMILIES
        assert "Q463" in found.untyped

    def test_a_propertys_revision_is_read(self, tmp_path, vocabulary):
        def build(dump):
            dump.data_node("P1", 7)

        found = self.one(tmp_path, vocabulary, build)
        assert found.revisions["P1"] == 7
        assert "P1" not in found.untyped


def report_of(included=(), excluded=()) -> ExclusionReport:
    return ExclusionReport(
        excluded=set(excluded),
        included=set(included),
        reasons={
            **{qid: "released" for qid in included},
            **{qid: f"possibly living ({qid})" for qid in excluded},
        },
    )


class TestNonprivilegedExclusions:
    """Every item with person evidence that the check did not release is
    dropped: an item it never assessed is dropped, not kept, and so is an
    item that states no type, and every family (#344), and every event
    that an item it drops takes part in (#363)."""

    reading = DumpReading(
        people=set(PEOPLE),
        untyped=NO_TYPE,
        families=ALL_FAMILIES,
        participants=PARTICIPANTS,
        # a chain to Q402, and a cycle
        redirects={
            **REDIRECTS,
            "Q420": "Q410",
            "Q421": "Q422",
            "Q422": "Q421",
        },
    )
    DROPPED = DROPPED | {"Q420"}

    def test_only_released_people_are_kept(self):
        dropped = nonprivileged_exclusions(
            self.reading, report_of({"Q401"}, set(PEOPLE) - {"Q401"})
        )
        assert set(dropped) == self.DROPPED
        assert dropped["Q404"] == "possibly living (Q404)"

    def test_a_person_the_check_never_assessed_is_dropped(self):
        # as the truthy human type selected them: none of Q402-Q406, Q415
        # or Q424-Q428
        dropped = nonprivileged_exclusions(self.reading, report_of({"Q401"}))
        assert set(dropped) == self.DROPPED
        assert dropped["Q403"] == NOT_ASSESSED
        # person evidence explains more than a missing type
        assert dropped["Q428"] == NOT_ASSESSED

    def test_a_person_only_the_check_found_is_dropped(self):
        dropped = nonprivileged_exclusions(
            DumpReading(), report_of({"Q401"}, {"Q499"})
        )
        assert dropped == {"Q499": "possibly living (Q499)"}

    def test_an_item_that_states_no_type_is_dropped(self):
        dropped = nonprivileged_exclusions(self.reading, report_of({"Q401"}))
        assert dropped["Q429"] == dropped["Q434"] == backup.NO_TYPE

    def test_one_the_check_released_too(self):
        # an untyped person with a death, say: released, and still dropped
        dropped = nonprivileged_exclusions(
            self.reading, report_of({"Q401", "Q404"})
        )
        assert dropped["Q404"] == backup.NO_TYPE

    def test_a_redirect_goes_with_the_item_it_redirects_to(self):
        dropped = nonprivileged_exclusions(
            self.reading, report_of({"Q401"}, {"Q402"})
        )
        # to a person not released, through a chain too, with no evidence
        # of its own
        assert dropped["Q410"] == "a redirect to Q402: possibly living (Q402)"
        assert dropped["Q420"] == dropped["Q410"]
        # to an item that states no type
        assert dropped["Q435"] == f"a redirect to Q429: {backup.NO_TYPE}"
        # to one released
        assert "Q418" not in dropped
        # a cycle of redirects ends
        assert not {"Q421", "Q422"} & set(dropped)

    def test_items_without_person_evidence_are_kept(self):
        dropped = nonprivileged_exclusions(self.reading, report_of({"Q401"}))
        assert not set(OTHERS) & set(dropped)

    def test_a_family_is_dropped_whatever_it_holds(self):
        # released or not, with person evidence (Q437) or none (Q446), and
        # a redirect to one goes with it
        dropped = nonprivileged_exclusions(
            self.reading, report_of({"Q401", "Q437"}, {"Q446"})
        )
        assert dropped["Q437"] == dropped["Q446"] == backup.FAMILY
        assert dropped["Q461"] == backup.FAMILY
        assert dropped["Q464"] == f"a redirect to Q446: {backup.FAMILY}"

    def test_an_event_goes_with_a_participant_left_out(self):
        # a part at deprecated rank counts; a family, left out, takes its
        # events with it; the reason names every participant left out
        dropped = nonprivileged_exclusions(
            self.reading, report_of({"Q401"}, set(PEOPLE) - {"Q401"})
        )
        left_out = backup.PARTICIPANT_LEFT_OUT.format
        assert dropped["Q407"] == left_out("Q406, Q426")
        assert dropped["Q430"] == left_out("Q424, Q425, Q428, Q437")
        assert dropped["Q431"] == left_out("Q427")
        # an event whose participants are all kept stays
        assert "Q412" not in dropped
        # an event dropped for another reason keeps it
        assert dropped["Q416"] == backup.NO_TYPE

    def test_a_person_event_that_names_no_participant_goes(self):
        # whom it happened to is unknown, and unknown means excluded
        # (#363); a redirect goes with it, and an event it takes part in
        reading = DumpReading(
            unattended={"Q493"},
            redirects={"Q494": "Q493"},
            participants={"Q495": {"Q493"}},
        )
        assert nonprivileged_exclusions(reading, report_of()) == {
            "Q493": backup.UNATTENDED,
            "Q494": f"a redirect to Q493: {backup.UNATTENDED}",
            "Q495": backup.PARTICIPANT_LEFT_OUT.format("Q493"),
        }

    def test_a_union_left_out_takes_the_events_that_bound_it(self):
        # Q490's couple is Q401, released, and Q402, not: the union goes,
        # and with it its start Q491 and its end, named through the
        # redirect Q496 to Q492, and what takes part in Q491 (#363;
        # ADR-0036 §6); the union Q497 of released partners keeps Q498
        reading = DumpReading(
            people={"Q401", "Q402", "Q403"},
            participants={
                "Q490": {"Q401", "Q402"},
                "Q495": {"Q491"},
                "Q497": {"Q401", "Q403"},
            },
            bounds={"Q490": {"Q491", "Q496"}, "Q497": {"Q498"}},
            redirects={"Q496": "Q492"},
        )
        dropped = nonprivileged_exclusions(
            reading, report_of({"Q401", "Q403"}, {"Q402"})
        )
        bound = backup.BOUND_LEFT_OUT.format("Q490")
        assert dropped == {
            "Q402": "possibly living (Q402)",
            "Q490": backup.PARTICIPANT_LEFT_OUT.format("Q402"),
            "Q491": bound,
            "Q492": bound,
            "Q496": f"a redirect to Q492: {bound}",
            "Q495": backup.PARTICIPANT_LEFT_OUT.format("Q491"),
        }

    def test_a_family_or_an_untyped_item_takes_its_event_with_it(self):
        # each the one participant left out of its event
        reading = DumpReading(
            people={"Q401"},
            families={"Q446"},
            untyped={"Q429"},
            participants={"Q480": {"Q401", "Q446"}, "Q481": {"Q429"}},
        )
        dropped = nonprivileged_exclusions(reading, report_of({"Q401"}))
        left_out = backup.PARTICIPANT_LEFT_OUT.format
        assert dropped["Q480"] == left_out("Q446")
        assert dropped["Q481"] == left_out("Q429")

    def test_an_event_of_released_people_stays(self):
        # Q401 is released; Q472 carries no person evidence
        reading = DumpReading(
            people={"Q401"}, participants={"Q471": {"Q401", "Q472"}}
        )
        assert nonprivileged_exclusions(reading, report_of({"Q401"})) == {}

    def test_a_participant_named_through_a_redirect(self):
        # Q410 redirects to Q402, whom the check does not release, and
        # Q418 to Q401, whom it does
        reading = DumpReading(
            people={"Q401", "Q402"},
            redirects={"Q410": "Q402", "Q418": "Q401"},
            participants={"Q471": {"Q410"}, "Q472": {"Q418"}},
        )
        dropped = nonprivileged_exclusions(
            reading, report_of({"Q401"}, {"Q402"})
        )
        assert dropped["Q471"] == backup.PARTICIPANT_LEFT_OUT.format("Q410")
        assert "Q472" not in dropped

    def test_what_takes_part_in_a_dropped_event_goes_too(self):
        # an event in an event that goes (Q474), through a redirect to it
        # too (Q476 names Q473, a redirect to Q471), and so on (Q475); a
        # redirect goes with an event (Q473, Q477)
        reading = DumpReading(
            people={"Q402"},
            redirects={"Q473": "Q471", "Q477": "Q475"},
            participants={
                "Q471": {"Q402"},
                "Q474": {"Q471"},
                "Q475": {"Q474"},
                "Q476": {"Q473"},
                "Q478": {"Q479"},
            },
        )
        dropped = nonprivileged_exclusions(
            reading, report_of(excluded={"Q402"})
        )
        left_out = backup.PARTICIPANT_LEFT_OUT.format
        assert dropped == {
            "Q402": "possibly living (Q402)",
            "Q471": left_out("Q402"),
            "Q473": f"a redirect to Q471: {left_out('Q402')}",
            "Q474": left_out("Q471"),
            "Q475": left_out("Q474"),
            "Q476": left_out("Q473"),
            "Q477": f"a redirect to Q475: {left_out('Q474')}",
        }


def revision_rows(revisions: dict[str, int]) -> list[dict]:
    """REVISIONS_QUERY's rows for these revisions."""
    return [
        {
            "entity": uri(qid),
            "version": {
                "type": "literal",
                "datatype": f"{XSD}integer",
                "value": str(revision),
            },
        }
        for qid, revision in revisions.items()
    ]


class Clock:
    """A monotonic clock that only a nap moves on."""

    def __init__(self):
        self.now = 0.0
        self.naps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += seconds


class TestTheQueryServiceLag:
    """The check runs on the query service and the export filters the
    dump, which comes from the database: the export needs the two to hold
    the same revision of every entity (#344)."""

    DUMPED = {"Q401": 11, "Q402": 12, "P1": 3}

    def test_the_same_revisions(self):
        run = lambda query: revision_rows(self.DUMPED)  # noqa: E731
        assert stale_entities(run, self.DUMPED) == []

    def test_an_entity_behind_ahead_missing_or_extra_is_stale(self):
        held = {"Q401": 10, "Q402": 13, "Q403": 1}  # P1 missing
        run = lambda query: revision_rows(held)  # noqa: E731
        assert stale_entities(run, self.DUMPED) == [
            "P1",
            "Q401",
            "Q402",
            "Q403",
        ]

    def test_two_revisions_of_one_entity_are_stale(self):
        rows = revision_rows(self.DUMPED) + revision_rows({"Q401": 10})
        assert stale_entities(lambda query: rows, self.DUMPED) == ["Q401"]

    def test_a_propertys_revision_is_compared_too(self):
        # the check reads a qualifier's precision through its property
        run = lambda query: revision_rows({**self.DUMPED, "P1": 4})  # noqa: E731
        assert stale_entities(run, self.DUMPED) == ["P1"]

    def test_a_version_that_is_no_integer_is_stale(self):
        rows = [
            {"entity": uri("Q401"), "version": literal("11")},
            {"entity": uri("Q402"), "version": literal("12.0")},
            {"entity": uri("P1"), "version": literal("3")},
        ]
        assert stale_entities(lambda query: rows, self.DUMPED) == ["Q402"]

    def test_the_query_leaves_redirects_out(self):
        # the dump gives a redirect a revision, the query service need not
        assert REVISIONS_QUERY.startswith("SELECT ?entity ?version WHERE {")
        assert "?entity <http://schema.org/version> ?version ." in (
            REVISIONS_QUERY
        )
        assert (
            "FILTER NOT EXISTS {\n"
            "    ?entity <http://www.w3.org/2002/07/owl#sameAs> ?target ."
        ) in REVISIONS_QUERY

    def test_the_export_waits_for_the_query_service_to_catch_up(self):
        answers = [{"Q401": 10}, {"Q401": 10}, self.DUMPED]
        clock = Clock()
        await_query_service(
            lambda query: revision_rows(answers.pop(0)),
            self.DUMPED,
            timeout=60,
            sleep=clock.sleep,
            clock=clock,
        )
        assert clock.naps == [LAG_POLL, LAG_POLL]
        assert not answers

    def test_the_export_refuses_once_it_has_waited(self):
        clock = Clock()
        refusal = r"after 60 s .* 1 entity \(Q401\)"
        with pytest.raises(QueryServiceLag, match=refusal) as refused:
            await_query_service(
                lambda query: revision_rows({**self.DUMPED, "Q401": 10}),
                self.DUMPED,
                timeout=60,
                sleep=clock.sleep,
                clock=clock,
            )
        assert clock.now == 60
        # the match is exact: a revision the query service never gets
        # stalls every export, and the refusal says how it ends
        assert (
            "An entity whose revision never reaches the query service keeps "
            "the export refusing, with the entity named here, until it is "
            "edited again or the query service is reloaded"
        ) in str(refused.value)


def facts_row(qid: str, died: bool = False) -> dict:
    """PERSON_FACTS_QUERY's row for one person."""
    return {"person": uri(qid)} | (
        {"death": literal("1996-01-01T00:00:00Z")} if died else {}
    )


class ExportRun:
    """The query service for export_nonprivileged: the check's facts and
    births, and each answer to REVISIONS_QUERY in turn (the last one again once
    they run out). `selected` are the people the facts query selects, and
    `released` those of them it gives a death."""

    def __init__(self, dumped, selected, revisions=None, released=("Q401",)):
        self.selected = selected
        self.released = set(released)
        self.revisions = list(revisions or [dumped])
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[dict]:
        self.queries.append(query)
        if query == REVISIONS_QUERY:
            answer = self.revisions[0]
            if len(self.revisions) > 1:
                self.revisions.pop(0)
            return revision_rows(answer)
        if "SAMPLE(?deathDate)" in query:
            return [
                facts_row(qid, qid in self.released) for qid in self.selected
            ]
        if 'BIND("value" AS ?bound)' in query:
            return birth_rows("Q404", [(("1990-01-01T00:00:00Z", 9), [], [])])
        raise AssertionError(f"unexpected query: {query}")


class TestExportNonprivileged:
    """The non-privileged export end to end, offline: a fictional dump, and the
    query service's answers."""

    def export(
        self,
        tmp_path,
        vocabulary,
        selected=tuple(PEOPLE),
        answering=None,
        **run,
    ):
        """export_nonprivileged over the fictional dump, answered by
        `answering` (ExportRun unless given), on a clock that only its naps
        move on."""
        dump = export_dump(vocabulary)
        dump.write(tmp_path / "full.nt.gz")
        clock = Clock()
        runner = (answering or ExportRun)(dump.revisions, selected, **run)
        counts = export_nonprivileged(
            config(),
            tmp_path,
            run=runner,
            vocabulary=vocabulary,
            lag_timeout=60,
            sleep=clock.sleep,
            clock=clock,
        )
        return counts, runner

    @staticmethod
    def public(tmp_path) -> tuple[str, set[str]]:
        """The export's text and the QIDs it mentions; and the query
        service's munger would keep every line of it (#412)."""
        with gzip.open(tmp_path / "nonprivileged.nt.gz", "rt") as handle:
            text = handle.read()
        assert misplaced(text.split("\n")[:-1]) == []
        return text, set(re.findall(r"Q[1-9]\d*\b", text))

    def test_only_released_people_leave(self, tmp_path, vocabulary):
        _, items = vocabulary
        self.export(tmp_path, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert "Q401" in mentioned
        assert not (set(PEOPLE) - {"Q401"}) & mentioned
        # a family, with person evidence or none, never leaves
        assert not set(FAMILIES) & mentioned
        # a redirect goes with its target
        assert "Q418" in mentioned
        assert not {"Q410", "Q435", "Q464"} & mentioned
        # nor does an event that one of them took part in (#363)
        assert not set(EVENTS) & mentioned
        # places, artifacts, an event of kept participants, occupations, a
        # vessel in a migration, a curator's classes and the vocabulary
        # stay
        assert set(OTHERS) | set(items.values()) <= mentioned

    def test_what_is_kept_leaves_with_its_text(self, tmp_path, vocabulary):
        # every text on what the export keeps stays, one naming a possibly
        # living relative included (owner, 2026-09-29; #363): a released
        # person's labels, description, alias and name, an artifact's
        # title, an event's description, a reference's excerpt and page
        # number, and the vocabulary's terms as dumpRdf.php writes them
        properties, items = vocabulary
        self.export(tmp_path, vocabulary)
        text, _ = self.public(tmp_path)
        for kept in TEXTS_KEPT:
            assert kept in text
        for term in (*LABELS, DESCRIPTION, ALT_LABEL):
            assert f"{entity('Q401')} {term} " in text
        assert '"Gustav Löbig"@de' in text
        family = load_manifest().items["family"]
        assert (
            f"{entity(items['family'])} {DESCRIPTION} "
            f'"{nt(family.description)}"@en .'
        ) in text
        # what is recorded about an item left out goes with it: the
        # description of a family, and the labels of the people left out
        for dropped in TEXTS_DROPPED:
            assert dropped not in text
        for qid in DROPPED:
            assert f"{entity(qid)} {RDFS_LABEL} " not in text

    @staticmethod
    def export_of(tmp_path, dump, vocabulary, released=()) -> None:
        """export_nonprivileged over `dump`, with the check selecting the
        fixture's people and releasing Q401, and selecting and releasing
        `released` too, on a clock that only its naps move on."""
        dump.write(tmp_path / "full.nt.gz")
        clock = Clock()
        export_nonprivileged(
            config(),
            tmp_path,
            run=ExportRun(
                dump.revisions,
                (*PEOPLE, *released),
                released=("Q401", *released),
            ),
            vocabulary=vocabulary,
            lag_timeout=60,
            sleep=clock.sleep,
            clock=clock,
        )

    def test_a_person_event_that_names_no_participant_goes(
        self, tmp_path, vocabulary
    ):
        # a birth whose participant's claim was refused or cut short: its
        # label and date are of someone unknown, who may be living (#363)
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q470", "Birth of Nicholas Lobig", revision=470)
        dump.says("Q470", "instance_of", entity(items["birth"]))
        dump.says("Q470", "point_in_time", dated(1950))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q470" not in mentioned
        assert "Birth of Nicholas Lobig" not in text
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q470"] == backup.UNATTENDED

    def test_a_statement_naming_one_left_out_goes_whole(
        self, tmp_path, vocabulary
    ):
        # Gustav (Q401, released) married Florence (Q405, who states no
        # type, so the export leaves her out) in 1942, as Nicholas's family
        # history (Q466) says: the statement goes with its date and its
        # reference, though its node carries Gustav's QID; so does the
        # occupation type of Q413, whose qualifier names Q462, left out,
        # and the truthy type it gave (#364)
        properties, _ = vocabulary
        dump = export_dump(vocabulary)
        reference = [
            ("stated_in", entity("Q466")),
            (
                "transcription_excerpt",
                '"Dad married Florence Faxridge in 1942"@en',
            ),
        ]
        dump.says(
            "Q401",
            "spouse",
            entity("Q405"),
            qualifiers=[("start_time", dated(1942))],
            reference=reference,
        )
        self.export_of(tmp_path, dump, vocabulary)
        text, _ = self.public(tmp_path)
        (wedding,) = {
            line.split(" ")[0]
            for line in dump.lines
            if line.endswith(f" {dated(1942)} .")
        }
        assert wedding.startswith(f"<{CONCEPT}/entity/statement/Q401-")
        assert wedding not in text
        assert dated(1942) not in text
        assert "Dad married Florence Faxridge" not in text
        digest = hashlib.sha1(repr(reference).encode()).hexdigest()
        assert f"<{CONCEPT}/reference/{digest}>" not in text
        # none of Gustav's spouse statements names one the export keeps
        spouse = f"{entity('Q401')} <{CONCEPT}/prop/{properties['spouse']}> "
        assert spouse not in text
        typing = (
            f"{entity('Q413')} <{CONCEPT}/prop/direct/"
            f"{properties['instance_of']}> "
        )
        assert typing not in text
        assert f"{entity('Q413')} {RDFS_LABEL} " in text

    @pytest.mark.parametrize("order", [1, -1], ids=["role-first", "role-last"])
    def test_an_event_whose_participant_is_named_in_a_qualifier_goes(
        self, tmp_path, vocabulary, order
    ):
        # an event of no person kind that Gustav (Q401, released) took
        # part in, and another participant of unknown value, in the role of
        # its principal, said by a qualifier to be Florence (Q405, left
        # out): it goes with its label and its date, as it would were she
        # its participant's value, whichever qualifier comes first; the
        # role, vocabulary, stays (#364)
        _, items = vocabulary
        principal = items["principal"]
        dump = export_dump(vocabulary)
        label = "an event, its participant of unknown value"
        dump.entity("Q470", label, revision=470)
        dump.says("Q470", "instance_of", entity(items["event"]))
        dump.says("Q470", "point_in_time", dated(1938))
        dump.says("Q470", "participant", entity("Q401"))
        dump.says(
            "Q470",
            "participant",
            "_:genid7",
            qualifiers=[
                ("object_has_role", entity(principal)),
                ("said_to_be_the_same_as", entity("Q405")),
            ][::order],
        )
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q470" not in mentioned
        assert label not in text
        assert dated(1938) not in text
        assert principal in mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q470"] == (
            backup.PARTICIPANT_LEFT_OUT.format("Q405")
        )

    def test_a_role_attends_no_person_event(self, tmp_path, vocabulary):
        # a death whose one participant is of unknown value, in the role
        # of its principal: whom it happened to is unknown, so it goes,
        # and the role, which is vocabulary, stays (#364)
        _, items = vocabulary
        principal = items["principal"]
        dump = export_dump(vocabulary)
        label = "a death, its participant of unknown value"
        dump.entity("Q470", label, revision=470)
        dump.says("Q470", "instance_of", entity(items["death"]))
        dump.says(
            "Q470",
            "participant",
            "_:genid7",
            qualifiers=[("object_has_role", entity(principal))],
        )
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q470" not in mentioned
        assert label not in text
        assert principal in mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q470"] == backup.UNATTENDED
        assert principal not in exclusions["excluded"]

    def test_an_event_whose_participants_are_all_unknown_goes(
        self, tmp_path, vocabulary
    ):
        # an event of no person kind whose one participant is of unknown
        # value: whom it involved is unknown, so it goes with its date
        _, items = vocabulary
        dump = export_dump(vocabulary)
        label = "an event, its one participant of unknown value"
        dump.entity("Q470", label, revision=470)
        dump.says("Q470", "instance_of", entity(items["event"]))
        dump.says("Q470", "point_in_time", dated(1938))
        dump.says("Q470", "participant", "_:genid7")
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q470" not in mentioned
        assert dated(1938) not in text
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q470"] == backup.UNATTENDED

    def test_an_event_goes_with_an_item_any_statement_names(
        self, tmp_path, vocabulary
    ):
        # a divorce that Gustav (Q401, released) took part in, carried out
        # by an organization with a birth (Q426, left out): it goes with
        # its date, and so does an event part of the divorce; an event
        # carried out by a kept organization (Q411) stays
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q470", "a divorce", revision=470)
        dump.says("Q470", "instance_of", entity(items["divorce"]))
        dump.says("Q470", "point_in_time", dated(1957))
        dump.says("Q470", "participant", entity("Q401"))
        dump.says("Q470", "carried_out_by", entity("Q426"))
        dump.entity("Q471", "a notice of the divorce", revision=471)
        dump.says("Q471", "instance_of", entity(items["event"]))
        dump.says("Q471", "part_of", entity("Q470"))
        dump.entity("Q472", "a hearing", revision=472)
        dump.says("Q472", "instance_of", entity(items["event"]))
        dump.says("Q472", "carried_out_by", entity("Q411"))
        # a hearing carried out by Florence (Q405), who states no type but
        # has person evidence: left out as one who may be a person, she
        # takes it with her
        dump.entity("Q473", "another hearing", revision=473)
        dump.says("Q473", "instance_of", entity(items["event"]))
        dump.says("Q473", "carried_out_by", entity("Q405"))
        dump.says("Q473", "location", entity("Q429"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert not {"Q470", "Q471", "Q473"} & mentioned
        assert dated(1957) not in text
        assert "Q472" in mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q470"] == (
            backup.LINKED_LEFT_OUT.format("Q426")
        )
        assert exclusions["excluded"]["Q471"] == (
            backup.LINKED_LEFT_OUT.format("Q470")
        )
        # only Florence shields the hearing: the untyped place does not
        assert exclusions["excluded"]["Q473"] == (
            backup.LINKED_LEFT_OUT.format("Q405")
        )
        assert "Q472" not in exclusions["excluded"]

    @pytest.mark.parametrize(
        "case",
        [
            "unknown-participant",
            "stray-participant",
            "deprecated-event-type",
            "participant-names-left-out",
            "participant-names-untyped",
            "participant-qualifier-names-left-out",
            "participant-names-family",
        ],
    )
    def test_a_person_is_never_an_event(self, tmp_path, vocabulary, case):
        # Gustav (Q401, typed human, released) with a participant statement
        # of unknown value, a stray participant statement and a sibling
        # Florence (Q405, left out), or a type `event` a curator deprecated
        # and that sibling: he is no event, so he stays, and only the
        # statement naming Florence goes whole
        properties, items = vocabulary
        dump = export_dump(vocabulary)
        if case == "unknown-participant":
            dump.says("Q401", "participant", "_:genid8")
        elif case == "participant-names-left-out":
            dump.says("Q401", "participant", entity("Q405"))
        elif case == "participant-names-untyped":
            dump.says("Q401", "participant", entity("Q429"))
        elif case == "participant-names-family":
            dump.says("Q401", "participant", entity("Q446"))
        elif case == "participant-qualifier-names-left-out":
            dump.says(
                "Q401",
                "participant",
                "_:genid8",
                qualifiers=[("said_to_be_the_same_as", entity("Q405"))],
            )
        else:
            dump.says("Q401", "sibling", entity("Q405"))
        if case == "stray-participant":
            dump.says("Q401", "participant", entity("Q412"))
        if case == "deprecated-event-type":
            dump.says(
                "Q401",
                "instance_of",
                entity(items["event"]),
                rank="Deprecated",
            )
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q401" in mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert "Q401" not in exclusions["excluded"]
        sibling = f"<{CONCEPT}/prop/{properties['sibling']}>"
        assert f"{entity('Q401')} {sibling} " not in text
        if case.startswith("participant-"):
            part = f"<{CONCEPT}/prop/{properties['participant']}>"
            assert f"{entity('Q401')} {part} " not in text

    def test_an_item_left_out_as_untyped_or_a_family_takes_no_event(
        self, tmp_path, vocabulary
    ):
        # Gustav's birth (Q401, released) at a place with no type (Q429),
        # and a reunion of his carried out by a family (Q446) whose one
        # member is Gustav: untyped items and families of released members
        # are left out, but protect no one, so the events stay; the
        # statements naming them go whole. A migration the family carried
        # out is its members' own, and goes (decision AC)
        properties, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q480", "a birth", revision=480)
        dump.says("Q480", "instance_of", entity(items["birth"]))
        dump.says("Q480", "participant", entity("Q401"))
        dump.says("Q480", "location", entity("Q429"))
        dump.entity("Q481", "a reunion", revision=481)
        dump.says("Q481", "instance_of", entity(items["event"]))
        dump.says("Q481", "participant", entity("Q401"))
        dump.says("Q481", "carried_out_by", entity("Q446"))
        dump.says("Q401", "member_of", entity("Q446"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert {"Q480", "Q481"} <= mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert not {"Q480", "Q481"} & set(exclusions["excluded"])
        location = f"<{CONCEPT}/prop/{properties['location']}>"
        carried = f"<{CONCEPT}/prop/{properties['carried_out_by']}>"
        assert f"{entity('Q480')} {location} " not in text
        assert f"{entity('Q481')} {carried} " not in text

    @pytest.mark.parametrize(
        "case",
        [
            "deportation",
            "unattended",
            "family-with-a-participant",
            "deprecated-type-carried-out",
            "untyped-carried-out",
            "untyped-location",
            "untyped-with-a-released-participant",
            "released-family-carried-out",
            "redirect-to-the-deportation",
            "untyped-with-an-untyped-participant",
            "redirect-to-an-untyped-with-an-untyped-participant",
            "two-untyped-hops",
            "redirect-to-an-untyped-carried-out",
        ],
    )
    def test_an_untyped_or_family_event_of_someone_left_out_shields(
        self, tmp_path, vocabulary, case
    ):
        # an item left out as untyped or as a family that is itself an event
        # the participant or unattended rule leaves out: the deportation
        # typed so at deprecated rank only (Q416), whose participant is left
        # out; an untyped item whose one participant is of unknown value;
        # or a family Florence (Q405, left out) took part in. So does one
        # that names Florence by any other statement: carried out by her,
        # typed an event at deprecated rank only or not at all, or at her
        # as a location, beside a released participant, or a family whose
        # one member is released. Or a redirect to the deportation (Q498),
        # or an untyped item an untyped place (Q429) takes part in. An
        # event part of it goes with it, its date with it
        _, items = vocabulary
        dump = export_dump(vocabulary)
        part = "Q416"
        if case == "redirect-to-the-deportation":
            part = "Q498"
            dump.redirect("Q498", "Q416", revision=498)
        if case == "two-untyped-hops":
            part = "Q491"
            dump.entity("Q492", "Florence's hearing", revision=492)
            dump.says("Q492", "carried_out_by", entity("Q405"))
            dump.entity("Q491", "a session of it", revision=491)
            dump.says("Q491", "part_of", entity("Q492"))
        if case == "redirect-to-an-untyped-carried-out":
            part = "Q498"
            dump.entity("Q491", "Florence's hearing", revision=491)
            dump.says("Q491", "carried_out_by", entity("Q405"))
            dump.redirect("Q498", "Q491", revision=498)
        if case == "redirect-to-an-untyped-with-an-untyped-participant":
            part = "Q498"
            dump.entity("Q491", "an untyped item", revision=491)
            dump.says("Q491", "participant", entity("Q429"))
            dump.redirect("Q498", "Q491", revision=498)
        if case in (
            "deprecated-type-carried-out",
            "untyped-carried-out",
            "untyped-location",
            "untyped-with-a-released-participant",
            "released-family-carried-out",
            "untyped-with-an-untyped-participant",
        ):
            part = "Q491"
            dump.entity("Q491", "Florence's hearing", revision=491)
        if case == "deprecated-type-carried-out":
            dump.says(
                "Q491",
                "instance_of",
                entity(items["event"]),
                rank="Deprecated",
            )
        if case in (
            "deprecated-type-carried-out",
            "untyped-carried-out",
            "untyped-with-a-released-participant",
            "released-family-carried-out",
        ):
            dump.says("Q491", "carried_out_by", entity("Q405"))
        if case == "untyped-location":
            dump.says("Q491", "location", entity("Q405"))
        if case == "untyped-with-a-released-participant":
            dump.says("Q491", "participant", entity("Q401"))
        if case == "released-family-carried-out":
            dump.says("Q491", "instance_of", entity(items["family"]))
            dump.says("Q401", "member_of", entity("Q491"))
        if case == "untyped-with-an-untyped-participant":
            dump.says("Q491", "participant", entity("Q429"))
        if case == "unattended":
            part = "Q491"
            dump.entity("Q491", "an untyped item", revision=491)
            dump.says("Q491", "participant", "_:genid9")
        if case == "family-with-a-participant":
            part = "Q491"
            dump.entity("Q491", "a family", revision=491)
            dump.says("Q491", "instance_of", entity(items["family"]))
            dump.says("Q491", "participant", entity("Q405"))
        dump.entity("Q490", "a gathering", revision=490)
        dump.says("Q490", "instance_of", entity(items["event"]))
        dump.says("Q490", "part_of", entity(part))
        dump.says("Q490", "point_in_time", dated(1961))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q490" not in mentioned
        assert dated(1961) not in text
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q490"] == (
            backup.LINKED_LEFT_OUT.format(part)
        )

    @pytest.mark.parametrize(
        ("case", "shields"),
        [
            ("member-through-a-redirect", False),
            ("one-released-one-unassessed", True),
            ("one-released-one-possibly-living", True),
            ("released-and-deprecated-possibly-living", True),
            ("own-participant-released", False),
            ("own-participant-with-a-role", False),
            ("own-participant-at-a-place", False),
            ("released-member-but-a-person", True),
            ("a-property-a-member", True),
        ],
    )
    def test_a_familys_members_are_read_from_its_statements(
        self, tmp_path, vocabulary, case, shields
    ):
        # "Departure of the Lobigs", carried out by the family Q446: its
        # members are the items named a member of it, at any rank, through
        # redirects (Q418 is a redirect to Gustav, Q401), and the values of
        # its own participant statements, never a role or a place its
        # qualifiers name. Released, all of them, the event stays; one
        # unassessed (Q429) or possibly living (Florence, Q405), even at
        # deprecated rank, and it goes. A family that is itself named a
        # sibling of Gustav's has person evidence, and goes as a person;
        # a property's member_of statement names no member
        _, items = vocabulary
        dump = export_dump(vocabulary)
        family = entity("Q446")
        if case == "member-through-a-redirect":
            dump.says("Q418", "member_of", family)
        if (
            case.startswith("one-released")
            or case == "released-member-but-a-person"
        ):
            dump.says("Q401", "member_of", family)
        if case == "one-released-one-unassessed":
            dump.says("Q429", "member_of", family)
        if case == "one-released-one-possibly-living":
            dump.says("Q405", "member_of", family)
        if case == "released-and-deprecated-possibly-living":
            dump.says("Q401", "member_of", family)
            dump.says("Q405", "member_of", family, rank="Deprecated")
        if case == "own-participant-released":
            dump.says("Q446", "participant", entity("Q401"))
        if case == "own-participant-with-a-role":
            dump.says(
                "Q446",
                "participant",
                entity("Q401"),
                qualifiers=[("object_has_role", entity(items["principal"]))],
            )
        if case == "own-participant-at-a-place":
            dump.says(
                "Q446",
                "participant",
                entity("Q401"),
                qualifiers=[("location", entity("Q408"))],
            )
        if case == "released-member-but-a-person":
            dump.says("Q401", "sibling", family)
        if case == "a-property-a-member":
            dump.says("P26", "member_of", family)
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", "carried_out_by", family)
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not shields
        assert (dated(1975) in text) is not shields

    @pytest.mark.parametrize(
        "participant",
        ["Q405", "Q429", "_:genid9"],
        ids=["left-out", "untyped", "unknown"],
    )
    def test_a_family_typed_human_keeps_its_participants(
        self, tmp_path, vocabulary, participant
    ):
        # the Lobig household (Q498), typed family and human, released by
        # the check, whose member is Gustav (Q401), that Florence (Q405,
        # left out), an untyped item (Q429) or someone unknown took part
        # in: a family stays a family, for the participant and unattended
        # rules too, so that participant is among its members, and
        # "Departure of the Lobigs" goes with it
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q498", "the Lobig household", revision=498)
        dump.says("Q498", "instance_of", entity(items["family"]))
        dump.says("Q498", "instance_of", entity(items["human"]))
        dump.says("Q498", "date_of_death", dated(1980))
        dump.says(
            "Q498",
            "participant",
            participant
            if participant.startswith("_:")
            else entity(participant),
        )
        dump.says("Q401", "member_of", entity("Q498"))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", "carried_out_by", entity("Q498"))
        self.export_of(tmp_path, dump, vocabulary, released=("Q498",))
        text, mentioned = self.public(tmp_path)
        assert "Q497" not in mentioned
        assert dated(1975) not in text
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q497"] == (
            backup.LINKED_LEFT_OUT.format("Q498")
        )

    def test_a_family_typed_human_and_a_death_naming_no_one_is_unattended(
        self, tmp_path, vocabulary
    ):
        # the Lobig household (Q498), typed family, human and death, with
        # Gustav (Q401) its one member and no participant statement, both
        # released by the check: a family is no person, so as a death that
        # names no participant it is unattended, protects people, and
        # "Departure of the Lobigs" goes with it, though its members are
        # all released
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q498", "the Lobig household", revision=498)
        dump.says("Q498", "instance_of", entity(items["family"]))
        dump.says("Q498", "instance_of", entity(items["human"]))
        dump.says("Q498", "instance_of", entity(items["death"]))
        dump.says("Q401", "member_of", entity("Q498"))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", "carried_out_by", entity("Q498"))
        self.export_of(tmp_path, dump, vocabulary, released=("Q401", "Q498"))
        text, mentioned = self.public(tmp_path)
        assert "Q497" not in mentioned
        assert dated(1975) not in text

    @pytest.mark.parametrize(
        ("group", "member", "shields"),
        [
            ("Q429", "Q405", True),
            ("Q463", "Q405", True),
            ("Q429", "Q401", False),
        ],
        ids=[
            "untyped-possibly-living",
            "deprecated-family",
            "untyped-released",
        ],
    )
    def test_whatever_is_named_a_member_of_is_a_group(
        self, tmp_path, vocabulary, group, member, shields
    ):
        # an item named a member of is a group, whatever its type: the
        # label-only Q429, or Q463, typed a family at deprecated rank only.
        # With Florence (Q405, possibly living) a member, an event it
        # carried out goes; with Gustav (Q401, released) alone, it stays
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says(member, "member_of", entity(group))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", "carried_out_by", entity(group))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not shields
        assert (dated(1975) in text) is not shields

    @pytest.mark.parametrize(
        ("case", "goes"),
        [
            ("released", False),
            ("possibly-living", True),
            ("no-member", True),
            ("through-a-redirect", False),
            ("an-untyped-group", False),
            ("person-kind", True),
            ("death-the-check-released", True),
            ("migration-the-check-released", True),
            ("migration-of-a-family-organization", True),
            ("migration-named-in-a-qualifier", True),
        ],
    )
    def test_a_released_group_takes_part_harmlessly(
        self, tmp_path, vocabulary, case, goes
    ):
        # "Emigration of the Lobigs", that Gustav (Q401, released) and the
        # family Q446 took part in: a family whose members are all released
        # takes part as harmlessly as a released person, named through its
        # redirect (Q464) too, and so does an untyped group (Q429) of his;
        # the event stays. One with a member who may be living, or that
        # names none, takes it with it. In a person kind of event (a
        # migration, a death) a group takes part as its members do, and the
        # event goes: unassessed, released by the check itself on a death,
        # set aside as an organization too, or named only in a qualifier
        _, items = vocabulary
        dump = export_dump(vocabulary)
        kind = "event"
        group = "Q446"
        released = ()
        if case not in ("possibly-living", "no-member"):
            dump.says("Q401", "member_of", entity("Q446"))
        if case == "possibly-living":
            dump.says("Q405", "member_of", entity("Q446"))
        if case == "through-a-redirect":
            group = "Q464"
        if case == "an-untyped-group":
            group = "Q429"
            dump.says("Q401", "member_of", entity("Q429"))
        if case in (
            "person-kind",
            "migration-the-check-released",
            "migration-of-a-family-organization",
            "migration-named-in-a-qualifier",
        ):
            kind = "migration"
        if case == "death-the-check-released":
            kind = "death"
        if case in (
            "death-the-check-released",
            "migration-the-check-released",
        ):
            released = ("Q446",)
            dump.entity("Q496", "a death", revision=496)
            dump.says("Q496", "instance_of", entity(items["death"]))
            dump.says("Q496", "participant", entity("Q446"))
        if case == "migration-of-a-family-organization":
            dump.says("Q446", "instance_of", entity(items["organization"]))
        dump.entity("Q497", "Emigration of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items[kind]))
        dump.says("Q497", "point_in_time", dated(1975))
        if case == "migration-named-in-a-qualifier":
            dump.says(
                "Q497",
                "participant",
                entity("Q401"),
                qualifiers=[("said_to_be_the_same_as", entity(group))],
            )
        else:
            dump.says("Q497", "participant", entity("Q401"))
            dump.says("Q497", "participant", entity(group))
        self.export_of(tmp_path, dump, vocabulary, released=released)
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not goes
        assert (dated(1975) in text) is not goes
        if goes:
            exclusions = json.loads(
                (tmp_path / "nonprivileged-exclusions.json").read_text()
            )
            assert exclusions["excluded"]["Q497"] == (
                backup.PARTICIPANT_LEFT_OUT.format(group)
            )

    def test_a_harmless_group_is_not_named_among_those_who_take_an_event(
        self, tmp_path, vocabulary
    ):
        # an event Florence (Q405, left out) and the Lobigs (Q446, whose
        # one member, Gustav, is released) took part in goes for her alone
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says("Q401", "member_of", entity("Q446"))
        dump.entity("Q497", "a gathering", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "participant", entity("Q405"))
        dump.says("Q497", "participant", entity("Q446"))
        self.export_of(tmp_path, dump, vocabulary)
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q497"] == (
            backup.PARTICIPANT_LEFT_OUT.format("Q405")
        )

    @pytest.mark.parametrize("via", ["carried_out_by", "participant"])
    @pytest.mark.parametrize(
        ("case", "shields"),
        [
            ("an-unknown-participant", True),
            ("nested-released", False),
            ("nested-possibly-living", True),
            ("a-cycle-of-no-one", True),
            ("a-cycle-with-a-released-member", False),
            ("nested-family-not-released", True),
            ("nested-family-of-no-one", True),
            ("a-person-named-a-group", True),
            ("nested-family-unknown-participant", True),
            ("nested-only-an-unknown-participant", True),
            ("nested-family-participant-possibly-living", True),
            ("nested-family-of-its-participant", False),
            ("a-released-person-named-a-group", True),
            ("a-released-person-named-a-group-nested", True),
            ("a-released-person-named-a-group-through-a-redirect", True),
            ("a-released-person-named-a-group-deprecated", True),
            ("a-released-person-named-a-group-all-released", False),
            ("a-released-person-named-back", False),
        ],
    )
    def test_a_groups_members_are_read_through_its_groups(
        self, tmp_path, vocabulary, case, shields, via
    ):
        # "Departure of the Lobigs", that the family Q446 carried out or
        # took part in: one whose own participants are Gustav (Q401) and
        # someone unknown is not confirmed released, and takes the event
        # with it. Its members are read through the groups among them
        # (Q491, a member of Q446), however deep, and a cycle of groups
        # ends: released through them, the event stays; with Florence
        # (Q405) a member of Q491, or no one at all, it goes. Each group so
        # read must be released too: Q491 a family the check never
        # assessed (it took part in a migration), one that names no one,
        # or one with a participant of unknown value, takes the event with
        # it. Florence named a group (Gustav a member of her) is a member
        # all the same, whose standing counts; and Gustav, released, named a
        # group (Florence a member of him, a household under its head, in
        # Q446 or in Q491 within it, through his redirect Q418, or at
        # deprecated rank), has his own members read too, so Florence keeps
        # the family unreleased; with Florence released too, or with the
        # Lobigs named a member of Gustav in turn, he counts as the released
        # member he is, and the event stays. A family known by its type and
        # its participant alone, Gustav, is released
        _, items = vocabulary
        dump = export_dump(vocabulary)
        family = entity("Q446")
        if case == "an-unknown-participant":
            dump.says("Q446", "participant", entity("Q401"))
            dump.says("Q446", "participant", "_:genid9")
        elif case == "a-person-named-a-group":
            dump.says("Q405", "member_of", family)
            dump.says("Q401", "member_of", entity("Q405"))
        elif case == "a-released-person-named-back":
            dump.says("Q401", "member_of", family)
            dump.says("Q446", "member_of", entity("Q401"))
        elif case.startswith("a-released-person-named-a-group"):
            if case.endswith("-nested"):
                dump.entity("Q491", "the Lobig cousins", revision=491)
                dump.says("Q491", "instance_of", entity(items["family"]))
                dump.says("Q491", "member_of", family)
                dump.says("Q401", "member_of", entity("Q491"))
            else:
                dump.says("Q401", "member_of", family)
            head = "Q418" if case.endswith("-redirect") else "Q401"
            rank = "Deprecated" if case.endswith("-deprecated") else "Normal"
            dump.says("Q405", "member_of", entity(head), rank=rank)
        else:
            dump.entity("Q491", "the Lobig cousins", revision=491)
            dump.says("Q491", "member_of", family)
        if case.startswith("nested-family") or case.startswith("nested-only"):
            dump.says("Q491", "instance_of", entity(items["family"]))
        if case in (
            "nested-released",
            "a-cycle-with-a-released-member",
            "nested-family-not-released",
        ):
            dump.says("Q401", "member_of", entity("Q491"))
        if case == "nested-possibly-living":
            dump.says("Q401", "member_of", entity("Q491"))
            dump.says("Q405", "member_of", entity("Q491"))
        if case.startswith("a-cycle"):
            dump.says("Q446", "member_of", entity("Q491"))
        if case == "nested-family-not-released":
            dump.entity("Q492", "a migration", revision=492)
            dump.says("Q492", "instance_of", entity(items["migration"]))
            dump.says("Q492", "participant", entity("Q491"))
        if case in (
            "nested-family-of-no-one",
            "nested-only-an-unknown-participant",
            "nested-family-participant-possibly-living",
        ):
            dump.says("Q401", "member_of", family)
        if case == "nested-family-unknown-participant":
            dump.says("Q491", "participant", entity("Q401"))
            dump.says("Q491", "participant", "_:genid9")
        if case == "nested-only-an-unknown-participant":
            dump.says("Q491", "participant", "_:genid9")
        if case == "nested-family-participant-possibly-living":
            dump.says("Q491", "participant", entity("Q405"))
        if case == "nested-family-of-its-participant":
            dump.says("Q491", "participant", entity("Q401"))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", via, family)
        released = ("Q405",) if case.endswith("-all-released") else ()
        # a cycle of groups must end: a hang fails, rather than stalls
        failures = []

        def export():
            try:
                self.export_of(tmp_path, dump, vocabulary, released=released)
            except BaseException as error:  # noqa: BLE001 - re-raised below
                failures.append(error)

        worker = threading.Thread(target=export, daemon=True)
        worker.start()
        worker.join(HANG_GUARD)
        assert not worker.is_alive(), "the members walk did not end"
        if failures:
            raise failures[0]
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not shields
        assert (dated(1975) in text) is not shields

    @pytest.mark.parametrize("via", ["participant", "carried_out_by"])
    def test_a_released_head_is_judged_on_their_own_standing(
        self, tmp_path, vocabulary, via
    ):
        # "Departure of Gustav" (Q497), attributed to Gustav (Q401,
        # released) himself, with Florence (Q405, who may be living)
        # recorded under him: a released person is judged on their own
        # standing alone, so the event stays (decision AE)
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says("Q405", "member_of", entity("Q401"))
        dump.entity("Q497", "Departure of Gustav", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", via, entity("Q401"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q497" in mentioned
        assert dated(1975) in text

    @pytest.mark.parametrize("via", ["participant", "carried_out_by"])
    def test_a_person_named_a_group_is_never_a_released_group(
        self, tmp_path, vocabulary, via
    ):
        # Q495, who states no type, released by the check (a death of
        # theirs, Q496), with Gustav (Q401) a member of them: a person is
        # never a released group, so "Departure of the Lobigs", that names
        # them as a participant or as who carried it out, goes
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q495", "Q495", revision=495)
        dump.entity("Q496", "a death", revision=496)
        dump.says("Q496", "instance_of", entity(items["death"]))
        dump.says("Q496", "participant", entity("Q495"))
        dump.says("Q401", "member_of", entity("Q495"))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", via, entity("Q495"))
        self.export_of(tmp_path, dump, vocabulary, released=("Q495",))
        text, mentioned = self.public(tmp_path)
        assert "Q497" not in mentioned
        assert dated(1975) not in text

    @pytest.mark.parametrize(
        "case", ["naming-no-one", "a-participant", "carried-out-by-a-group"]
    )
    def test_a_curator_subclass_of_a_person_kind_is_one_everywhere(
        self, tmp_path, vocabulary, case
    ):
        # "Passage of Nina Lobig" (Q494), typed Q495, a class a curator made
        # a subclass of Q492, a redirect to Q493, a subclass of migration:
        # a person kind, two hops and a redirect down. Naming no
        # participant, it is unattended; its participant Q498, typed only
        # with a curator's class (Q499), has person evidence the check never
        # assessed, and is left out with it. Carried out by the Lobigs
        # (Q446), Gustav their one member, it is theirs, and goes too
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q493", "a passage", revision=493)
        dump.says("Q493", "subclass_of", entity(items["migration"]))
        dump.redirect("Q492", "Q493")
        dump.entity("Q495", "a sea passage", revision=495)
        dump.says("Q495", "subclass_of", entity("Q492"))
        dump.entity("Q494", "Passage of Nina Lobig", revision=494)
        dump.says("Q494", "instance_of", entity("Q495"))
        dump.says("Q494", "point_in_time", dated(1975))
        if case == "a-participant":
            dump.entity("Q499", "a traveller", revision=499)
            dump.entity("Q498", "Q498", revision=498)
            dump.says("Q498", "instance_of", entity("Q499"))
            dump.says("Q494", "participant", entity("Q498"))
        if case == "carried-out-by-a-group":
            dump.says("Q401", "member_of", entity("Q446"))
            dump.says("Q494", "participant", entity("Q401"))
            dump.says("Q494", "carried_out_by", entity("Q446"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q494" not in mentioned
        assert dated(1975) not in text
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )["excluded"]
        if case == "naming-no-one":
            assert exclusions["Q494"] == backup.UNATTENDED
        elif case == "carried-out-by-a-group":
            assert exclusions["Q494"] == (
                backup.LINKED_LEFT_OUT.format("Q446")
            )
        else:
            assert "Q498" not in mentioned
            assert exclusions["Q498"] == NOT_ASSESSED

    @pytest.mark.parametrize(
        ("case", "goes"),
        [
            ("carried-out-by-a-released-family", True),
            ("carried-out-by-a-family-with-a-member-left-out", True),
            ("a-curator-subclass-of-migration", True),
            ("carried-out-by-a-released-family-not-a-person-kind", False),
        ],
    )
    def test_a_person_kind_of_event_goes_with_any_group_it_names(
        self, tmp_path, vocabulary, case, goes
    ):
        # "Emigration of the Lobigs", a migration of Gustav's (Q401)
        # carried out by the family Q446: the event is its members' own,
        # so it goes, the family's members all released (Gustav) or not
        # (Florence, Q405, too). So does one typed with a class a curator
        # made a subclass of migration (Q493), that the family took part
        # in. An event of no person kind it carried out stays
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says("Q401", "member_of", entity("Q446"))
        if case == "carried-out-by-a-family-with-a-member-left-out":
            dump.says("Q405", "member_of", entity("Q446"))
        kind = entity(items["migration"])
        if case == "a-curator-subclass-of-migration":
            dump.entity("Q493", "a passage", revision=493)
            dump.says("Q493", "subclass_of", kind)
            kind = entity("Q493")
        if case.endswith("not-a-person-kind"):
            kind = entity(items["event"])
        dump.entity("Q497", "Emigration of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", kind)
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says("Q497", "participant", entity("Q401"))
        if case == "a-curator-subclass-of-migration":
            dump.says("Q497", "participant", entity("Q446"))
        else:
            dump.says("Q497", "carried_out_by", entity("Q446"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not goes
        assert (dated(1975) in text) is not goes
        if goes:
            exclusions = json.loads(
                (tmp_path / "nonprivileged-exclusions.json").read_text()
            )
            reason = (
                backup.PARTICIPANT_LEFT_OUT
                if case == "a-curator-subclass-of-migration"
                else backup.LINKED_LEFT_OUT
            )
            assert exclusions["excluded"]["Q497"] == reason.format("Q446")

    @pytest.mark.parametrize("participant", ["Q446", "Q401", "Q464"])
    def test_an_untyped_event_of_a_released_group_shields_nothing(
        self, tmp_path, vocabulary, participant
    ):
        # an untyped gathering (Q491) that the Lobigs (Q446, Gustav their
        # one member, or Q464, a redirect to them) or Gustav himself took
        # part in: neither protects anyone, so an event part of it stays,
        # without its statement
        properties, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says("Q401", "member_of", entity("Q446"))
        dump.entity("Q491", "a gathering", revision=491)
        dump.says("Q491", "participant", entity(participant))
        dump.entity("Q490", "a dance", revision=490)
        dump.says("Q490", "instance_of", entity(items["event"]))
        dump.says("Q490", "part_of", entity("Q491"))
        self.export_of(tmp_path, dump, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert "Q490" in mentioned

    def test_an_untyped_place_naming_another_shields_nothing(
        self, tmp_path, vocabulary
    ):
        # Gustav's birth (Q401, released) at an untyped place (Q429) that
        # is part of another untyped place (Q434): neither protects anyone,
        # so the birth stays
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.says("Q429", "part_of", entity("Q434"))
        dump.entity("Q480", "a birth", revision=480)
        dump.says("Q480", "instance_of", entity(items["birth"]))
        dump.says("Q480", "participant", entity("Q401"))
        dump.says("Q480", "location", entity("Q429"))
        self.export_of(tmp_path, dump, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert "Q480" in mentioned

    @pytest.mark.parametrize("named", ["redirect", "target"])
    @pytest.mark.parametrize("kind", ["untyped", "family"])
    def test_a_redirect_that_may_be_a_person_shields_its_target(
        self, tmp_path, vocabulary, kind, named
    ):
        # Gustav (Q401) is married to Q495, a redirect to an untyped item
        # (Q429) or a family (Q446): Q495 may be a person, so the item it
        # stands for is not left out only as untyped or as a family, and an
        # event that names either goes
        _, items = vocabulary
        target = {"untyped": "Q429", "family": "Q446"}[kind]
        dump = export_dump(vocabulary)
        dump.redirect("Q495", target, revision=495)
        dump.says("Q401", "spouse", entity("Q495"))
        dump.entity("Q496", "an emigration", revision=496)
        dump.says("Q496", "instance_of", entity(items["event"]))
        dump.says("Q496", "point_in_time", dated(1957))
        dump.says(
            "Q496",
            "carried_out_by",
            entity("Q495" if named == "redirect" else target),
        )
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q496" not in mentioned
        assert dated(1957) not in text

    def test_a_redirect_with_no_person_evidence_shields_nothing(
        self, tmp_path, vocabulary
    ):
        # an event carried out by Q435, a redirect to the untyped Q429, with
        # no person evidence of its own: it protects no one, so the event
        # stays, without the statement naming it
        properties, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q496", "a fire", revision=496)
        dump.says("Q496", "instance_of", entity(items["event"]))
        dump.says("Q496", "carried_out_by", entity("Q435"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q496" in mentioned
        carried = f"<{CONCEPT}/prop/{properties['carried_out_by']}>"
        assert f"{entity('Q496')} {carried} " not in text

    @pytest.mark.parametrize(
        "released", [False, True], ids=["unassessed", "released"]
    )
    def test_an_untyped_person_shields_unless_released(
        self, tmp_path, vocabulary, released
    ):
        # an event carried out by Q492, untyped, with a birth in the dump:
        # one the check never assessed may be a person, and takes the event
        # with it; one it released is left out only as untyped, and does not
        dump_items = vocabulary[1]
        dump = export_dump(vocabulary)
        dump.entity("Q492", "Q492", revision=492)
        dump.says("Q492", "date_of_birth", dated(1890))
        dump.entity("Q493", "a hearing", revision=493)
        dump.says("Q493", "instance_of", entity(dump_items["event"]))
        dump.says("Q493", "carried_out_by", entity("Q492"))
        self.export_of(
            tmp_path, dump, vocabulary, released=("Q492",) if released else ()
        )
        _, mentioned = self.public(tmp_path)
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert "Q492" in exclusions["excluded"]
        assert ("Q493" in mentioned) is released

    def test_a_deprecated_human_type_makes_no_person_of_an_event(
        self, tmp_path, vocabulary
    ):
        # Q494, typed event and, at deprecated rank, human, released by the
        # check, whose one participant is of unknown value: it is no person
        # at normal or preferred rank, so it is unattended and goes
        _, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q494", "a gathering", revision=494)
        dump.says("Q494", "instance_of", entity(items["event"]))
        dump.says(
            "Q494", "instance_of", entity(items["human"]), rank="Deprecated"
        )
        dump.says("Q494", "participant", "_:genid9")
        self.export_of(tmp_path, dump, vocabulary, released=("Q494",))
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q494"] == backup.UNATTENDED

    @pytest.mark.parametrize("named", ["family", "redirect"])
    @pytest.mark.parametrize(
        ("member", "shields"),
        [("Q405", True), ("Q401", False), (None, True)],
        ids=["possibly-living", "released", "none"],
    )
    def test_a_family_shields_unless_its_members_are_released(
        self, tmp_path, vocabulary, member, shields, named
    ):
        # "Departure of the Lobigs", carried out by the family Q446, named
        # so or through its redirect Q464: a member who may be living
        # (Florence, Q405, a member of it) takes the event with the family,
        # and so does a family that names no member, whose members cannot
        # be known released; one whose members are all released (Gustav,
        # Q401) is left out only as a family, and the event stays
        _, items = vocabulary
        dump = export_dump(vocabulary)
        if member:
            dump.says(member, "member_of", entity("Q464"))
        dump.entity("Q497", "Departure of the Lobigs", revision=497)
        dump.says("Q497", "instance_of", entity(items["event"]))
        dump.says("Q497", "point_in_time", dated(1975))
        dump.says(
            "Q497",
            "carried_out_by",
            entity("Q446" if named == "family" else "Q464"),
        )
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert ("Q497" in mentioned) is not shields
        assert (dated(1975) in text) is not shields
        if shields:
            exclusions = json.loads(
                (tmp_path / "nonprivileged-exclusions.json").read_text()
            )
            family = "Q446" if named == "family" else "Q464"
            assert exclusions["excluded"]["Q497"] == (
                backup.LINKED_LEFT_OUT.format(family)
            )

    @pytest.mark.parametrize("kind", ["marriage", "partnership"])
    def test_a_union_is_no_event_its_wedding_takes(
        self, tmp_path, vocabulary, kind
    ):
        # Gustav's (Q401, released) union, started by a wedding Florence
        # (Q405, left out) took part in and ended by a divorce of his: the
        # wedding goes, and the union's statement naming it goes whole,
        # but the union is no event and stays, and so does its divorce
        properties, items = vocabulary
        dump = export_dump(vocabulary)
        dump.entity("Q480", "a union", revision=480)
        dump.says("Q480", "instance_of", entity(items[kind]))
        dump.says("Q480", "participant", entity("Q401"))
        dump.says("Q480", "started_by", entity("Q481"))
        dump.says("Q480", "ended_by", entity("Q482"))
        dump.entity("Q481", "a wedding", revision=481)
        dump.says("Q481", "instance_of", entity(items["wedding"]))
        dump.says("Q481", "participant", entity("Q405"))
        dump.entity("Q482", "a divorce", revision=482)
        dump.says("Q482", "instance_of", entity(items["divorce"]))
        dump.says("Q482", "participant", entity("Q401"))
        self.export_of(tmp_path, dump, vocabulary)
        text, mentioned = self.public(tmp_path)
        assert "Q481" not in mentioned
        assert {"Q480", "Q482"} <= mentioned
        started = f"<{CONCEPT}/prop/{properties['started_by']}>"
        assert f"{entity('Q480')} {started} " not in text

    def test_the_judgement_notes_never_leave(self, tmp_path, vocabulary):
        # the extractor's remark on its own reading, in a run's reference
        # (ADR-0035), goes; the reference keeps what it cites (#363)
        properties, items = vocabulary
        noted = {**properties, "judgement": "P998", "judgement_note": "P999"}
        dump = export_dump(vocabulary)
        dump.properties = noted
        dump.says(
            "Q401",
            "occupation",
            entity("Q413"),
            reference=[
                ("stated_in", entity("Q466")),
                ("judgement", entity(items["circa"])),
                ("judgement_note", '"the postmark read as 1938"@en'),
            ],
        )
        self.export_of(tmp_path, dump, (noted, items))
        text, _ = self.public(tmp_path)
        assert "the postmark read as 1938" not in text
        assert f"/prop/reference/P998> {entity(items['circa'])} ." not in text
        stated_in = f"<{CONCEPT}/prop/reference/{properties['stated_in']}>"
        assert f"{stated_in} {entity('Q466')} ." in text

    def test_an_event_goes_with_a_participant_left_out(
        self, tmp_path, vocabulary
    ):
        self.export(tmp_path, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert not set(EVENTS) & mentioned
        assert "Q412" in mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q431"] == (
            backup.PARTICIPANT_LEFT_OUT.format("Q427")
        )

    def test_an_item_that_states_no_type_is_left_out(
        self, tmp_path, vocabulary
    ):
        self.export(tmp_path, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert not set(UNTYPED) & mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q429"] == backup.NO_TYPE

    def test_a_person_the_check_never_selected_is_dropped(
        self, tmp_path, vocabulary
    ):
        # as the truthy human type selected them: a type deprecated or
        # outranked, or none, hid Q402-Q406 and Q415 from the check
        self.export(tmp_path, vocabulary, selected=["Q401"])
        _, mentioned = self.public(tmp_path)
        assert not (set(PEOPLE) - {"Q401"}) & mentioned
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q402"] == NOT_ASSESSED

    def test_the_exclusions_say_who_and_why(self, tmp_path, vocabulary):
        counts, _ = self.export(tmp_path, vocabulary)
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert set(exclusions["excluded"]) == DROPPED
        assert "born as late as 1990" in exclusions["excluded"]["Q404"]
        assert exclusions["excluded"]["Q446"] == backup.FAMILY
        assert exclusions["included_persons"] == ["Q401"]
        assert counts["excluded_persons"] == len(DROPPED)
        assert counts["included_persons"] == 1
        assert counts["triples_dropped"] > 0

    def test_a_released_person_left_out_is_not_counted_in(
        self, tmp_path, vocabulary
    ):
        # Q404 states no type: released by the check, and still left out
        class Released(ExportRun):
            def __call__(self, query: str) -> list[dict]:
                rows = super().__call__(query)
                if "SAMPLE(?deathDate)" in query:
                    rows = [
                        facts_row(qid, qid in {"Q401", "Q404"})
                        for qid in self.selected
                    ]
                return rows

        counts, _ = self.export(tmp_path, vocabulary, answering=Released)
        exclusions = json.loads(
            (tmp_path / "nonprivileged-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q404"] == backup.NO_TYPE
        assert exclusions["included_persons"] == ["Q401"]
        assert counts["included_persons"] == 1

    def test_the_check_runs_once_the_query_service_holds_the_dump(
        self, tmp_path, vocabulary
    ):
        dumped = export_dump(vocabulary).revisions
        behind = {**dumped, "Q404": 1}
        _, runner = self.export(
            tmp_path, vocabulary, revisions=[behind, behind, dumped]
        )
        # two waits, then the check, then one more look
        assert [query == REVISIONS_QUERY for query in runner.queries] == [
            True,
            True,
            True,
            False,
            False,
            True,
        ]

    def test_the_export_refuses_while_the_query_service_lags(
        self, tmp_path, vocabulary
    ):
        behind = {**export_dump(vocabulary).revisions, "Q404": 1}
        with pytest.raises(QueryServiceLag, match="Q404"):
            self.export(tmp_path, vocabulary, revisions=[behind])
        assert not (tmp_path / "nonprivileged.nt.gz").exists()
        assert not (tmp_path / "nonprivileged-exclusions.json").exists()

    @pytest.mark.parametrize(
        ("line", "named"),
        [
            (
                f"{entity('P900')} <{ONTOLOGY}propertyType> <{ONTOLOGY}Url> .",
                "P900 (Url)",
            ),
            (
                f"{entity('P901')} <{ONTOLOGY}propertyType> "
                f"<{ONTOLOGY}CommonsMedia> .",
                "P901 (CommonsMedia)",
            ),
            (
                "<https://el.example/wiki/Page> <http://schema.org/about> "
                f"{entity('Q401')} .",
                "Q401 (a sitelink)",
            ),
        ],
    )
    def test_a_dump_with_a_link_is_refused(
        self, tmp_path, vocabulary, line, named
    ):
        # a property whose values are links, or a sitelink: an IRI that
        # can carry text, which the filter does not read (#363)
        dump = export_dump(vocabulary)
        dump.write(tmp_path / "full.nt.gz", [*dump.lines, line])
        runner = ExportRun(dump.revisions, tuple(PEOPLE))
        clock = Clock()
        with pytest.raises(backup.UnfilterableDump, match=re.escape(named)):
            export_nonprivileged(
                config(),
                tmp_path,
                run=runner,
                vocabulary=vocabulary,
                lag_timeout=60,
                sleep=clock.sleep,
                clock=clock,
            )
        # refused before the query service is asked, and nothing written
        assert runner.queries == []
        assert not (tmp_path / "nonprivileged.nt.gz").exists()
        assert not (tmp_path / "nonprivileged-exclusions.json").exists()

    def test_the_export_refuses_when_the_graph_changes_meanwhile(
        self, tmp_path, vocabulary
    ):
        dumped = export_dump(vocabulary).revisions
        with pytest.raises(QueryServiceLag, match=r"changed .* \(Q401\)"):
            self.export(
                tmp_path,
                vocabulary,
                revisions=[dumped, {**dumped, "Q401": 999}],
            )
        assert not (tmp_path / "nonprivileged.nt.gz").exists()


class TestTheBackupsNonprivilegedExport:
    """`yizkr backup` writes its non-privileged export as
    export_nonprivileged does, with the dumps and the query service stood
    in for."""

    @pytest.fixture
    def backed_up(self, tmp_path, vocabulary, monkeypatch):
        dump = export_dump(vocabulary)
        extra: list[str] = []  # lines the dump holds beside the fixture's

        def dumped(name):
            def write(deploy_dir, out_path, compose_files=None):
                if name == "rdf":
                    dump.write(out_path, dump.shaped() + extra)
                else:
                    out_path.write_bytes(b"fictional")

            return write

        for name in ("database", "media", "rdf"):
            monkeypatch.setattr(backup, f"dump_{name}", dumped(name))

        class Wiki:
            def __init__(self, config):
                pass

            def vocabulary(self):
                return vocabulary

        monkeypatch.setattr("yizkr.core.wiki.WikiClient", Wiki)
        # the backup waits on a clock that only its naps move on
        clock, wait = Clock(), backup.await_query_service
        monkeypatch.setattr(
            backup,
            "await_query_service",
            lambda run, revisions, **options: wait(
                run, revisions, timeout=60, sleep=clock.sleep, clock=clock
            ),
        )

        def backed_up(revisions=None, keep=0, lines=(), command=False):
            extra[:] = lines
            runner = ExportRun(dump.revisions, ["Q401"], revisions)
            monkeypatch.setattr(backup.sparql, "runner", lambda config: runner)
            if command:  # through `yizkr backup`, which returns its exit code
                from yizkr import cli

                monkeypatch.setattr(
                    cli.Config, "load", lambda env_file=None: config()
                )
                args = cli.build_parser().parse_args(
                    [
                        "backup",
                        "--out",
                        str(tmp_path),
                        "--deploy-dir",
                        str(tmp_path),
                    ]
                )
                return args.func(args)
            return run_backup(
                config(), tmp_path, deploy_dir=tmp_path, keep=keep
            )

        return backed_up

    def test_the_backup_writes_the_export_of_released_people(
        self, tmp_path, backed_up
    ):
        manifest = backed_up()
        out_dir = tmp_path / manifest["timestamp"]
        export = manifest["components"]["nonprivileged_export"]
        assert export["included_persons"] == 1
        assert export["excluded_persons"] == len(DROPPED)
        _, mentioned = TestExportNonprivileged.public(out_dir)
        assert not (set(PEOPLE) - {"Q401"}) & mentioned

    def test_a_refused_export_leaves_the_backup_recorded(
        self, tmp_path, vocabulary, backed_up
    ):
        (tmp_path / "20260101T000000Z").mkdir()  # an older run
        behind = {**export_dump(vocabulary).revisions, "Q404": 1}
        with pytest.raises(QueryServiceLag, match="Q404") as refused:
            backed_up(revisions=[behind], keep=1)
        (out_dir,) = tmp_path.iterdir()
        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert set(manifest["components"]) == {"database", "media", "rdf"}
        assert "Q404" in manifest["nonprivileged_export_refused"]
        assert manifest["pruned"] == ["20260101T000000Z"]
        assert not (out_dir / "nonprivileged.nt.gz").exists()
        # the refusal carries the manifest, for the command to report
        assert refused.value.manifest == manifest

    def test_an_unfilterable_dump_leaves_the_backup_recorded(
        self, tmp_path, backed_up
    ):
        # a sitelink on Q401: the export refuses, as it does for a lag (#363)
        sitelink = (
            "<https://el.example/wiki/Page> <http://schema.org/about> "
            f"{entity('Q401')} ."
        )
        with pytest.raises(backup.UnfilterableDump) as refused:
            backed_up(lines=[sitelink])
        (out_dir,) = tmp_path.iterdir()
        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert set(manifest["components"]) == {"database", "media", "rdf"}
        assert "Q401 (a sitelink)" in manifest["nonprivileged_export_refused"]
        assert not (out_dir / "nonprivileged.nt.gz").exists()
        assert refused.value.manifest == manifest

    def test_the_command_reports_an_unfilterable_dump_without_a_traceback(
        self, tmp_path, backed_up, capsys
    ):
        # the sitelink refusal from run_backup itself, through `yizkr backup`
        sitelink = (
            "<https://el.example/wiki/Page> <http://schema.org/about> "
            f"{entity('Q401')} ."
        )
        assert backed_up(lines=[sitelink], command=True) == 1
        out, err = capsys.readouterr()
        (out_dir,) = tmp_path.iterdir()
        assert out.splitlines() == [
            f"{name}: {out_dir / file}, "
            f"bytes={(out_dir / file).stat().st_size}"
            for name, file in (
                ("database", "database.sql.gz"),
                ("media", "media.tar.gz"),
                ("rdf", "full.nt.gz"),
            )
        ]
        assert err.startswith("non-privileged export refused: ")
        assert "Q401 (a sitelink)" in err
        assert err.count("\n") == 1
        assert "Traceback" not in err


class TestTheBackupCommand:
    """`yizkr backup` reports any refused non-privileged export on one line,
    after the dumps it kept, and exits non-zero without a traceback (#344,
    #412)."""

    @pytest.mark.parametrize(
        "refusal",
        [
            QueryServiceLag("the query service lags (Q404)"),
            UnfilterableDump(
                "the dump holds what the export is not made to filter: "
                "Q401 (a sitelink)"
            ),
        ],
        ids=["lag", "unfilterable"],
    )
    def test_a_refusal_is_one_line_after_the_dumps_it_kept(
        self, tmp_path, monkeypatch, capsys, refusal
    ):
        from yizkr import cli

        refusal.manifest = {
            "timestamp": "20260929T031500Z",
            "components": {
                "database": {"file": "database.sql.gz", "bytes": 9},
                "rdf": {"file": "full.nt.gz", "bytes": 7},
            },
            "nonprivileged_export_refused": str(refusal),
            "pruned": ["20260101T000000Z"],
        }

        def refused(*args, **options):
            raise refusal

        monkeypatch.setattr(backup, "run_backup", refused)
        monkeypatch.setattr(cli.Config, "load", lambda env_file=None: None)
        args = cli.build_parser().parse_args(
            ["backup", "--out", str(tmp_path)]
        )
        assert args.func(args) == 1
        out, err = capsys.readouterr()
        run = tmp_path / "20260929T031500Z"
        assert out.splitlines() == [
            f"database: {run / 'database.sql.gz'}, bytes=9",
            f"rdf: {run / 'full.nt.gz'}, bytes=7",
            "pruned old backups: 20260101T000000Z",
        ]
        assert err == f"non-privileged export refused: {refusal}\n"
        assert "Traceback" not in err

    def test_a_refusal_before_any_manifest_is_still_one_line(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli

        def refused(*args, **options):
            raise UnfilterableDump("Q401 (a sitelink)")

        monkeypatch.setattr(backup, "run_backup", refused)
        monkeypatch.setattr(cli.Config, "load", lambda env_file=None: None)
        args = cli.build_parser().parse_args(
            ["backup", "--out", str(tmp_path)]
        )
        assert args.func(args) == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err == "non-privileged export refused: Q401 (a sitelink)\n"

    @pytest.mark.parametrize(
        "flag", ["--no-nonprivileged-export", "--no-public-export"]
    )
    def test_the_export_can_be_skipped(self, tmp_path, monkeypatch, flag):
        from yizkr import cli

        asked = {}

        def ran(config, **options):
            asked.update(options)
            return {
                "timestamp": "20260929T031500Z",
                "components": {},
                "pruned": [],
            }

        monkeypatch.setattr(backup, "run_backup", ran)
        monkeypatch.setattr(cli.Config, "load", lambda env_file=None: None)
        args = cli.build_parser().parse_args(
            ["backup", "--out", str(tmp_path), flag]
        )
        assert args.func(args) == 0
        assert asked["nonprivileged_export"] is False

    def test_the_export_runs_by_default(self, tmp_path, monkeypatch):
        from yizkr import cli

        asked = {}

        def ran(config, **options):
            asked.update(options)
            return {
                "timestamp": "20260929T031500Z",
                "components": {},
                "pruned": [],
            }

        monkeypatch.setattr(backup, "run_backup", ran)
        monkeypatch.setattr(cli.Config, "load", lambda env_file=None: None)
        args = cli.build_parser().parse_args(
            ["backup", "--out", str(tmp_path)]
        )
        assert args.func(args) == 0
        assert asked["nonprivileged_export"] is True


class TestTheFormerNames:
    """The export's former Python names resolve to the new objects, with a
    DeprecationWarning naming the new name, for one release (#412)."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("export_public", "export_nonprivileged"),
            ("filter_public_export", "filter_nonprivileged_export"),
            ("public_exclusions", "nonprivileged_exclusions"),
            ("PublicExportRefused", "NonprivilegedExportRefused"),
        ],
    )
    def test_an_old_name_warns_and_resolves(self, old, new):
        with pytest.warns(
            DeprecationWarning, match=f"yizkr.backup.{new}"
        ) as caught:
            resolved = getattr(backup, old)
        assert resolved is getattr(backup, new)
        # the warning points at the caller's line, not at backup.py
        assert caught[0].filename == __file__

    def test_an_old_import_warns_and_resolves(self):
        with pytest.warns(DeprecationWarning, match="export_nonprivileged"):
            from yizkr.backup import export_public
        assert export_public is export_nonprivileged

    def test_the_old_refusal_still_catches_every_refusal(self):
        with pytest.warns(DeprecationWarning):
            old = backup.PublicExportRefused
        assert old is NonprivilegedExportRefused
        assert issubclass(QueryServiceLag, old)
        assert issubclass(UnfilterableDump, old)

    def test_an_unknown_name_is_still_an_attribute_error(self):
        with pytest.raises(AttributeError, match="no_such_name"):
            backup.no_such_name  # noqa: B018
