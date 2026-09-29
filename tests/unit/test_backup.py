"""Offline tests for backup/export logic. No stack, no docker. The
possibly-living determination itself is tested in test_privacy.py.

The public export keeps only the people the check releases (#344): it
reads person evidence from its own dump, at every rank, and drops every
item with any that the check did not release, and every item that states
no type; and it refuses to run while the query service, where the check
runs, lags the dump. It drops every event that an item it drops took
part in, and carries no free text but the vocabulary's (#363): a label,
a description or a title can name a person it leaves out."""

import datetime
import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

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
    QueryServiceLag,
    await_query_service,
    exclusion_report,
    export_public,
    filter_public_export,
    prune_old_backups,
    public_exclusions,
    read_dump,
    run_backup,
    stale_entities,
)
from yizkr.core.config import Config
from yizkr.core.privacy import BIRTHS_QUERY, people_selection
from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import prefix_block


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
    """The public export's call (ADR-0020): every person the facts query
    lists, with the births the privacy module's query reads (#334)."""

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
# a time, typed as dumpRdf.php types one: no free text (#363)
TIME = "^^<http://www.w3.org/2001/XMLSchema#dateTime>"


class TestPublicFilter:
    LINES = [
        # the excluded person's own triples: entity, statement node, sitelink
        '<http://w.lan/entity/Q76> <http://x/p> "Nicholas" .',
        f'<http://w.lan/entity/statement/Q76-aaaa-bbbb> <http://x/p> "1950-01-01T00:00:00Z"{TIME} .',  # noqa: E501
        "<http://w.lan/wiki/Item:Q76> <http://schema.org/about> <http://w.lan/entity/Q76> .",  # noqa: E501
        # someone else's claim ABOUT the excluded person
        "<http://w.lan/entity/Q72> <http://w.lan/prop/direct/P7> <http://w.lan/entity/Q76> .",  # noqa: E501
        # an includable person whose QID shares a prefix (Q7 vs Q76)
        '<http://w.lan/entity/Q7> <http://x/p> "Gustav" .',
        # unrelated triple
        '<http://w.lan/entity/Q100> <http://x/p> "place" .',
        # provenance: the excluded person's statement uses REF_DEAD
        f"<http://w.lan/entity/statement/Q76-cccc> <http://www.w3.org/ns/prov#wasDerivedFrom> <http://w.lan/reference/{REF_DEAD}> .",  # noqa: E501
        # REF_DEAD's content quotes source text naming the living person
        f'<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/P8> "Nicholas 1950"@en .',  # noqa: E501
        f"<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_DEAD}> .",  # noqa: E501
        f'<http://w.lan/value/{VALUE_DEAD}> <http://x/time> "1950-01-01T00:00:00Z"{TIME} .',  # noqa: E501
        # provenance shared with an included person's statement
        f"<http://w.lan/entity/statement/Q7-dddd> <http://www.w3.org/ns/prov#wasDerivedFrom> <http://w.lan/reference/{REF_SHARED}> .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/P8> \"Gustav 'Gustl' 1919\"@en .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_LIVE}> .",  # noqa: E501
        f'<http://w.lan/value/{VALUE_LIVE}> <http://x/time> "1919-01-01T00:00:00Z"{TIME} .',  # noqa: E501
    ]
    # The entities whose own text these tests keep, as the export keeps
    # the vocabulary's, so that each rule here is seen on its own; the
    # free text has tests of its own (TestTheFreeText). An excerpt is text
    # on a reference, which no vocabulary holds.
    TEXT_KEPT = {"Q7", "Q72", "Q76", "Q100"}

    def filter(
        self, tmp_path: Path, excluded: set[str], excerpt_pids=frozenset()
    ):
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "public.nt.gz"
        with gzip.open(full, "wt") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        kept, dropped = filter_public_export(
            full, public, excluded, excerpt_pids, vocabulary=self.TEXT_KEPT
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

    def test_excerpts_never_enter_the_public_export(self, tmp_path):
        _, _, content = self.filter(tmp_path, set(), excerpt_pids={"P8"})
        assert (
            "Gustav 'Gustl' 1919" not in content
        )  # excerpt gone even for the dead
        assert REF_SHARED in content  # the reference node itself survives
        assert VALUE_LIVE in content

    def test_qid_prefix_does_not_collide(self, tmp_path):
        _, _, content = self.filter(tmp_path, {"Q7"})
        assert "Gustav" not in content
        assert '"Nicholas"' in content  # Q76 untouched by excluding Q7

    def test_no_exclusions_passes_everything_but_free_text(self, tmp_path):
        # the two excerpts are text on a reference, which leaves at no time
        kept, dropped, content = self.filter(tmp_path, set())
        assert (kept, dropped) == (len(self.LINES) - 2, 2)
        assert "Nicholas 1950" not in content
        assert "Gustav 'Gustl' 1919" not in content


W = "http://w.lan"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
PREF_LABEL = "<http://www.w3.org/2004/02/skos/core#prefLabel>"
ALT_LABEL = "<http://www.w3.org/2004/02/skos/core#altLabel>"
NAME = "<http://schema.org/name>"
# the three names dumpRdf.php gives an entity's label
LABELS = (RDFS_LABEL, PREF_LABEL, NAME)
DESCRIPTION = "<http://schema.org/description>"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
INTEGER = "^^<http://www.w3.org/2001/XMLSchema#integer>"
REF_TEXT = "e" * 40  # a reference with a page number and an excerpt
REF_RUN = "1" * 40  # a run's reference, with a note of the model's
VALUE_TIME = "f" * 40  # a time's value node
VALUE_PLACE = "0" * 40  # a coordinate's value node


class TestTheFreeText:
    """The public export carries no free text but the vocabulary's (#363):
    a label, a description, an alias, a name, a title, or a reference's
    excerpt, page number or note can name a person the export leaves
    out, and no wording is read to tell. A time, a number or a coordinate
    is no free text. The vocabulary here is the class Q1 and the property
    P5; Q300 is a released person, Q301 an artifact and Q302 an
    extraction run, all kept."""

    # the dump's header, with the format version loaders read
    HEADER = [
        "<http://wikiba.se/ontology#Dump> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://schema.org/Dataset> .",  # noqa: E501
        '<http://wikiba.se/ontology#Dump> <http://schema.org/softwareVersion> "1.0.0" .',  # noqa: E501
        f'<http://wikiba.se/ontology#Dump> <http://schema.org/dateModified> "2026-09-29T03:15:00Z"{TIME} .',  # noqa: E501
    ]
    # the vocabulary's own terms, as the model manifest gives them
    TERMS = [
        f'<{W}/entity/Q1> {RDFS_LABEL} "human"@en .',
        f'<{W}/entity/Q1> {PREF_LABEL} "human"@en .',
        f'<{W}/entity/Q1> {NAME} "human"@en .',
        f'<{W}/entity/Q1> {DESCRIPTION} "person known to the archive"@en .',
        f'<{W}/entity/Q1> {ALT_LABEL} "person"@en .',
        f'<{W}/entity/P5> {RDFS_LABEL} "date of birth"@en .',
        f'<{W}/entity/P5> {DESCRIPTION} "date on which the subject was born"@en .',  # noqa: E501
    ]
    # the kept items and their statements: every node, and the values that
    # are a time, a number or a coordinate
    STRUCTURE = [
        f"<{W}/entity/Q1> <{W}/prop/P9> <{W}/entity/statement/Q1-aaaa> .",
        f"<{W}/entity/Q300> <{W}/prop/P10> <{W}/entity/statement/Q300-bbbb> .",  # noqa: E501
        f"<{W}/entity/statement/Q300-bbbb> <http://wikiba.se/ontology#rank> <http://wikiba.se/ontology#NormalRank> .",  # noqa: E501
        f"<{W}/entity/statement/Q300-bbbb> {DERIVED_FROM} <{W}/reference/{REF_TEXT}> .",  # noqa: E501
        f"<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P12> <{W}/entity/Q301> .",  # noqa: E501
        f"<{W}/entity/statement/Q300-bbbb> {DERIVED_FROM} <{W}/reference/{REF_RUN}> .",  # noqa: E501
        f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/P20> <{W}/entity/Q302> .",  # noqa: E501
        f"<{W}/entity/Q301> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://wikiba.se/ontology#Item> .",  # noqa: E501
        f'<{W}/entity/Q300> <{W}/prop/direct/P5> "1919-01-07T00:00:00Z"{TIME} .',  # noqa: E501
        f"<{W}/entity/Q300> <{W}/prop/P5> <{W}/entity/statement/Q300-cccc> .",
        f"<{W}/entity/statement/Q300-cccc> <{W}/prop/statement/value/P5> <{W}/value/{VALUE_TIME}> .",  # noqa: E501
        f'<{W}/value/{VALUE_TIME}> <http://wikiba.se/ontology#timeValue> "1919-01-07T00:00:00Z"{TIME} .',  # noqa: E501
        f'<{W}/value/{VALUE_TIME}> <http://wikiba.se/ontology#timePrecision> "11"{INTEGER} .',  # noqa: E501
        f'<{W}/entity/Q300> <{W}/prop/direct/P14> "+2"^^<http://www.w3.org/2001/XMLSchema#decimal> .',  # noqa: E501
        f'<{W}/entity/Q301> <{W}/prop/direct/P15> "Point(8.7 50.1)"^^<http://www.opengis.net/ont/geosparql#wktLiteral> .',  # noqa: E501
        f"<{W}/entity/Q301> <{W}/prop/P15> <{W}/entity/statement/Q301-dddd> .",
        f"<{W}/entity/statement/Q301-dddd> <{W}/prop/statement/value/P15> <{W}/value/{VALUE_PLACE}> .",  # noqa: E501
        f'<{W}/value/{VALUE_PLACE}> <http://wikiba.se/ontology#geoLatitude> "50.1"^^<http://www.w3.org/2001/XMLSchema#double> .',  # noqa: E501
    ]
    KEPT = HEADER + TERMS + STRUCTURE
    # what goes: every other literal, typed or not
    TEXT = [
        # a released person's terms, as the extraction wrote them
        f'<{W}/entity/Q300> {RDFS_LABEL} "Gustav Löbig"@de .',
        f'<{W}/entity/Q300> {PREF_LABEL} "Gustav Löbig"@de .',
        f'<{W}/entity/Q300> {NAME} "Gustav Löbig"@de .',
        f'<{W}/entity/Q300> {DESCRIPTION} "father of Nicholas Lobig, born 1950"@en .',  # noqa: E501
        f'<{W}/entity/Q300> {ALT_LABEL} "Gustave Lobig"@en .',
        f'<{W}/entity/Q300> {ALT_LABEL} "Gustl"@de-ch .',
        # a name as a statement's value, and its truthy form
        f'<{W}/entity/statement/Q300-bbbb> <{W}/prop/statement/P10> "Gustav Löbig"@de .',  # noqa: E501
        f'<{W}/entity/Q300> <{W}/prop/direct/P10> "Gustav Löbig"@de .',
        # a string qualifier, quoting within its quotes
        f'<{W}/entity/statement/Q300-bbbb> <{W}/prop/qualifier/P11> "son of \\"Wastl\\", father of Nicholas ." .',  # noqa: E501
        # a reference's page number and excerpt, and a note of the model's
        # in a run's reference, of any reference property
        f'<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P13> "6, the entry for Nicholas Lobig" .',  # noqa: E501
        f'<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P3> "Gustav, Vater von Nicholas Lobig"@de .',  # noqa: E501
        f'<{W}/reference/{REF_RUN}> <{W}/prop/reference/P21> "identity" .',
        f'<{W}/reference/{REF_RUN}> <{W}/prop/reference/P22> "took the Nicholas of the letter for Nicholas Lobig"@en .',  # noqa: E501
        # a vocabulary item's statement is no term of its own
        f'<{W}/entity/statement/Q1-aaaa> <{W}/prop/statement/P9> "Nicholas Lobig" .',  # noqa: E501
        # a date dumpRdf.php cannot type, which it writes as a string
        f'<{W}/entity/Q300> <{W}/prop/direct/P5> "+1950-00-00T00:00:00Z" .',
        # a literal of any other type
        f'<{W}/entity/Q300> <{W}/prop/direct/P16> "Nicholas Lobig"^^<http://www.w3.org/2001/XMLSchema#string> .',  # noqa: E501
        f'<{W}/entity/Q300> <{W}/prop/direct/P16> "Nicholas"^^<http://www.w3.org/1998/Math/MathML> .',  # noqa: E501
        # an artifact's title and its media file's name
        f'<{W}/entity/Q301> {RDFS_LABEL} "A few pages of family history, by Nicholas Lobig"@en .',  # noqa: E501
        f'<{W}/entity/Q301> <{W}/prop/direct/P18> "File:Nicholas Lobig family history.png" .',  # noqa: E501
    ]

    def filter(self, tmp_path: Path, vocabulary) -> tuple[int, int, list]:
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "public.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(self.TEXT + self.KEPT) + "\n")
        kept, dropped = filter_public_export(
            full, public, set(), vocabulary=vocabulary
        )
        with gzip.open(public, "rt", encoding="utf-8") as handle:
            return kept, dropped, handle.read().splitlines()

    def test_only_the_vocabularys_text_leaves(self, tmp_path):
        kept, dropped, lines = self.filter(tmp_path, {"Q1", "P5"})
        assert lines == self.KEPT
        assert (kept, dropped) == (len(self.KEPT), len(self.TEXT))

    def test_no_text_leaves_by_default_but_the_headers(self, tmp_path):
        # with no vocabulary named, even the vocabulary's terms go
        _, _, lines = self.filter(tmp_path, frozenset())
        assert lines == self.HEADER + self.STRUCTURE

    def test_no_text_of_a_reference_leaves(self, tmp_path):
        # an excerpt that names a person the export leaves out goes with
        # no excerpt property named, and so do a page number and a note
        # in a run's reference, whatever their property; the references
        # keep what they cite
        _, _, lines = self.filter(tmp_path, {"Q1", "P5"})
        assert [line for line in lines if line.startswith(f"<{W}/ref")] == [
            f"<{W}/reference/{REF_TEXT}> <{W}/prop/reference/P12> <{W}/entity/Q301> .",  # noqa: E501
            f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/P20> <{W}/entity/Q302> .",  # noqa: E501
        ]
        assert not [line for line in lines if "Nicholas" in line]

    @pytest.mark.parametrize(
        "term",
        [
            '"Nicholas Lobig" .',
            '"Nicholas Lobig"@en .',
            '"Gustl"@de-ch .',
            '"say \\"Nicholas\\" ." .',
            '"Nicholas"^^<http://www.w3.org/2001/XMLSchema#string> .',
            '"Nicholas"^^<http://example.org/any-other-type> .',
            '"+1950-00-00T00:00:00Z" .',
            '"Nicholas Lobig"@en.',
            '"Nicholas Lobig" .\n',
        ],
    )
    def test_a_literal_is_free_text(self, term):
        assert backup._free_text(term)

    @pytest.mark.parametrize(
        "term",
        [
            f'"1950-01-01T00:00:00Z"{TIME} .',
            f'"11"{INTEGER} .\n',
            '"+2"^^<http://www.w3.org/2001/XMLSchema#decimal> .',
            '"50.1"^^<http://www.w3.org/2001/XMLSchema#double> .',
            '"Point(8.7 50.1)"^^<http://www.opengis.net/ont/geosparql#wktLiteral> .',  # noqa: E501
            f'"1950"{INTEGER}.',
            "<http://w.lan/entity/Q76> .",
            "_:genid1 .",
        ],
    )
    def test_a_time_a_number_a_coordinate_or_a_node_is_no_free_text(
        self, term
    ):
        assert not backup._free_text(term)


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


# -- the public export keeps only released people (#344) --------------------

RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
ONTOLOGY = "http://wikiba.se/ontology#"
DATA = f"{CONCEPT}/wiki/Special:EntityData/"


def entity(qid: str) -> str:
    return f"<{CONCEPT}/entity/{qid}>"


def dated(year: int) -> str:
    return f'"{year}-01-01T00:00:00Z"^^<{XSD}dateTime>'


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
            *(f'{entity(qid)} {term} "{label}"@en .' for term in LABELS),
            *(
                [f'{entity(qid)} {DESCRIPTION} "{description}"@en .']
                if description
                else []
            ),
            *(
                f'{entity(qid)} {ALT_LABEL} "{alias}"@en .'
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

    def write(self, path: Path, lines=None) -> Path:
        with gzip.open(path, "wt", encoding="utf-8") as sink:
            sink.write("\n".join(lines or self.lines) + "\n")
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
# item of the fixture stands for: a released person's description, an
# artifact's title and its file's name, the description of an event he is
# not recorded as taking part in, a family's description, and a page
# number and an excerpt. None leaves, as no free text does but the
# vocabulary's (#363).
NAMED = "Nicholas"
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
    dump = Dump(vocabulary)
    dump.header()
    for key, pid in properties.items():
        dump.entity(pid, key, kind="Property")
    for key, qid in items.items():
        dump.entity(qid, key)
    # the texts that name Nicholas Lobig (NAMED)
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
    def one(tmp_path, vocabulary, build) -> DumpReading:
        """What the dump `build` writes into gives, beside an item Q450
        typed as a place (so that it states a type) and the vocabulary."""
        properties, items = vocabulary
        dump = Dump(vocabulary)
        for key, qid in items.items():
            dump.entity(qid, key)
        for qid in ("Q450", "Q451", "Q499"):
            dump.entity(qid, qid)
        dump.says("Q450", "instance_of", entity(items["place"]))
        build(dump)
        return read_dump(dump.write(tmp_path / "one.nt.gz"), properties, items)

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


class TestPublicExclusions:
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
        dropped = public_exclusions(
            self.reading, report_of({"Q401"}, set(PEOPLE) - {"Q401"})
        )
        assert set(dropped) == self.DROPPED
        assert dropped["Q404"] == "possibly living (Q404)"

    def test_a_person_the_check_never_assessed_is_dropped(self):
        # as the truthy human type selected them: none of Q402-Q406, Q415
        # or Q424-Q428
        dropped = public_exclusions(self.reading, report_of({"Q401"}))
        assert set(dropped) == self.DROPPED
        assert dropped["Q403"] == NOT_ASSESSED
        # person evidence explains more than a missing type
        assert dropped["Q428"] == NOT_ASSESSED

    def test_a_person_only_the_check_found_is_dropped(self):
        dropped = public_exclusions(
            DumpReading(), report_of({"Q401"}, {"Q499"})
        )
        assert dropped == {"Q499": "possibly living (Q499)"}

    def test_an_item_that_states_no_type_is_dropped(self):
        dropped = public_exclusions(self.reading, report_of({"Q401"}))
        assert dropped["Q429"] == dropped["Q434"] == backup.NO_TYPE

    def test_one_the_check_released_too(self):
        # an untyped person with a death, say: released, and still dropped
        dropped = public_exclusions(self.reading, report_of({"Q401", "Q404"}))
        assert dropped["Q404"] == backup.NO_TYPE

    def test_a_redirect_goes_with_the_item_it_redirects_to(self):
        dropped = public_exclusions(
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
        dropped = public_exclusions(self.reading, report_of({"Q401"}))
        assert not set(OTHERS) & set(dropped)

    def test_a_family_is_dropped_whatever_it_holds(self):
        # released or not, with person evidence (Q437) or none (Q446), and
        # a redirect to one goes with it
        dropped = public_exclusions(
            self.reading, report_of({"Q401", "Q437"}, {"Q446"})
        )
        assert dropped["Q437"] == dropped["Q446"] == backup.FAMILY
        assert dropped["Q461"] == backup.FAMILY
        assert dropped["Q464"] == f"a redirect to Q446: {backup.FAMILY}"

    def test_an_event_goes_with_a_participant_left_out(self):
        # a part at deprecated rank counts; a family, left out, takes its
        # events with it; the reason names every participant left out
        dropped = public_exclusions(
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

    def test_an_event_of_released_people_stays(self):
        # Q401 is released; Q472 carries no person evidence
        reading = DumpReading(
            people={"Q401"}, participants={"Q471": {"Q401", "Q472"}}
        )
        assert public_exclusions(reading, report_of({"Q401"})) == {}

    def test_a_participant_named_through_a_redirect(self):
        # Q410 redirects to Q402, whom the check does not release, and
        # Q418 to Q401, whom it does
        reading = DumpReading(
            people={"Q401", "Q402"},
            redirects={"Q410": "Q402", "Q418": "Q401"},
            participants={"Q471": {"Q410"}, "Q472": {"Q418"}},
        )
        dropped = public_exclusions(reading, report_of({"Q401"}, {"Q402"}))
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
        dropped = public_exclusions(reading, report_of(excluded={"Q402"}))
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
    """The query service for export_public: the check's facts and births,
    and each answer to REVISIONS_QUERY in turn (the last one again once
    they run out). `selected` are the people the facts query selects."""

    def __init__(self, dumped, selected, revisions=None):
        self.selected = selected
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
            return [facts_row(qid, qid == "Q401") for qid in self.selected]
        if 'BIND("value" AS ?bound)' in query:
            return birth_rows("Q404", [(("1990-01-01T00:00:00Z", 9), [], [])])
        raise AssertionError(f"unexpected query: {query}")


class TestExportPublic:
    """The public export end to end, offline: a fictional dump, and the
    query service's answers."""

    def export(
        self,
        tmp_path,
        vocabulary,
        selected=tuple(PEOPLE),
        answering=None,
        **run,
    ):
        """export_public over the fictional dump, answered by `answering`
        (ExportRun unless given), on a clock that only its naps move on."""
        dump = export_dump(vocabulary)
        dump.write(tmp_path / "full.nt.gz")
        clock = Clock()
        runner = (answering or ExportRun)(dump.revisions, selected, **run)
        counts = export_public(
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
        with gzip.open(tmp_path / "public.nt.gz", "rt") as handle:
            text = handle.read()
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

    def test_no_free_text_but_the_vocabularys_leaves(
        self, tmp_path, vocabulary
    ):
        properties, items = vocabulary
        self.export(tmp_path, vocabulary)
        text, _ = self.public(tmp_path)
        # nothing names Nicholas Lobig: not a released person's description,
        # an artifact's title or its file's name, an event's or a family's
        # description, a page number or an excerpt
        assert NAMED not in text
        # every plain string and text in a language left is a term of the
        # vocabulary's items and properties, or the dump's header's version
        vocabulary_terms = {
            entity(qid) for qid in [*properties.values(), *items.values()]
        }
        texts = {
            line.split(" ", 1)[0]
            for line in text.splitlines()
            if re.search(r'"(@[a-z-]+)? \.$', line)
        }
        assert texts == vocabulary_terms | {f"<{ONTOLOGY}Dump>"}
        # a released person keeps no label, description or alias; the
        # statement of a name keeps its node and reference, not its text
        for term in (*LABELS, DESCRIPTION, ALT_LABEL):
            assert f"{entity('Q401')} {term} " not in text
        assert '"Gustav Löbig"@de' not in text
        stated_in = f"<{CONCEPT}/prop/reference/{properties['stated_in']}>"
        assert f"{stated_in} {entity('Q466')} ." in text
        # a time is no free text
        date_of_death = properties["date_of_death"]
        assert (
            f"{entity('Q401')} <{CONCEPT}/prop/direct/{date_of_death}> "
            f"{dated(1996)} ."
        ) in text

    def test_an_event_goes_with_a_participant_left_out(
        self, tmp_path, vocabulary
    ):
        self.export(tmp_path, vocabulary)
        _, mentioned = self.public(tmp_path)
        assert not set(EVENTS) & mentioned
        assert "Q412" in mentioned
        exclusions = json.loads(
            (tmp_path / "public-exclusions.json").read_text()
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
            (tmp_path / "public-exclusions.json").read_text()
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
            (tmp_path / "public-exclusions.json").read_text()
        )
        assert exclusions["excluded"]["Q402"] == NOT_ASSESSED

    def test_the_exclusions_say_who_and_why(self, tmp_path, vocabulary):
        counts, _ = self.export(tmp_path, vocabulary)
        exclusions = json.loads(
            (tmp_path / "public-exclusions.json").read_text()
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
            (tmp_path / "public-exclusions.json").read_text()
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
        assert not (tmp_path / "public.nt.gz").exists()
        assert not (tmp_path / "public-exclusions.json").exists()

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
        assert not (tmp_path / "public.nt.gz").exists()


class TestTheBackupsPublicExport:
    """`yizkr backup` writes its public export as export_public does, with
    the dumps and the query service stood in for."""

    @pytest.fixture
    def backed_up(self, tmp_path, vocabulary, monkeypatch):
        dump = export_dump(vocabulary)

        def dumped(name):
            def write(deploy_dir, out_path, compose_files=None):
                if name == "rdf":
                    dump.write(out_path)
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

        def backed_up(revisions=None, keep=0):
            runner = ExportRun(dump.revisions, ["Q401"], revisions)
            monkeypatch.setattr(backup.sparql, "runner", lambda config: runner)
            return run_backup(
                config(), tmp_path, deploy_dir=tmp_path, keep=keep
            )

        return backed_up

    def test_the_backup_writes_the_export_of_released_people(
        self, tmp_path, backed_up
    ):
        manifest = backed_up()
        out_dir = tmp_path / manifest["timestamp"]
        export = manifest["components"]["public_export"]
        assert export["included_persons"] == 1
        assert export["excluded_persons"] == len(DROPPED)
        _, mentioned = TestExportPublic.public(out_dir)
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
        assert "Q404" in manifest["public_export_refused"]
        assert manifest["pruned"] == ["20260101T000000Z"]
        assert not (out_dir / "public.nt.gz").exists()
        # the refusal carries the manifest, for the command to report
        assert refused.value.manifest == manifest


class TestTheBackupCommand:
    """`yizkr backup` reports a refused public export on one line, after
    the dumps it kept, and exits non-zero (#344)."""

    def test_a_refusal_is_one_line_after_the_dumps_it_kept(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli

        refusal = QueryServiceLag("the query service lags (Q404)")
        refusal.manifest = {
            "timestamp": "20260929T031500Z",
            "components": {
                "database": {"file": "database.sql.gz", "bytes": 9},
                "rdf": {"file": "full.nt.gz", "bytes": 7},
            },
            "public_export_refused": str(refusal),
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
        assert err == "public export refused: the query service lags (Q404)\n"
