"""Offline tests for backup/export logic. No stack, no docker. The
possibly-living determination itself is tested in test_privacy.py."""

import datetime
import gzip
from pathlib import Path

import pytest
import yaml

from tests.unit.test_privacy import BIRTHS, CASES, CONCEPT, EXCLUDED, uri
from yizkr.backup import (
    PERSON_FACTS_QUERY,
    exclusion_report,
    filter_public_export,
    prune_old_backups,
)
from yizkr.core.config import Config
from yizkr.core.privacy import BIRTHS_QUERY
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
        properties, items = vocabulary
        _, calls = self.report(vocabulary)
        assert len(calls) == 2
        assert prefix_block(CONCEPT) + BIRTHS_QUERY.format(**properties) in (
            calls
        )

    def test_the_facts_query_reads_no_birth(self):
        # births are read statement by statement, never sampled here
        assert "SAMPLE(?birthDate)" not in PERSON_FACTS_QUERY
        assert "date_of_birth" not in PERSON_FACTS_QUERY


REF_DEAD = "a" * 40  # reference used only by the excluded person
REF_SHARED = "b" * 40  # reference used by an included person too
VALUE_DEAD = "c" * 40  # value node reachable only via REF_DEAD
VALUE_LIVE = "d" * 40  # value node reachable via REF_SHARED


class TestPublicFilter:
    LINES = [
        # the excluded person's own triples: entity, statement node, sitelink
        '<http://w.lan/entity/Q76> <http://x/p> "Nicholas" .',
        '<http://w.lan/entity/statement/Q76-aaaa-bbbb> <http://x/p> "1950" .',
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
        f'<http://w.lan/value/{VALUE_DEAD}> <http://x/time> "1950" .',
        # provenance shared with an included person's statement
        f"<http://w.lan/entity/statement/Q7-dddd> <http://www.w3.org/ns/prov#wasDerivedFrom> <http://w.lan/reference/{REF_SHARED}> .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/P8> \"Gustav 'Gustl' 1919\"@en .",  # noqa: E501
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_LIVE}> .",  # noqa: E501
        f'<http://w.lan/value/{VALUE_LIVE}> <http://x/time> "1919" .',
    ]

    def filter(
        self, tmp_path: Path, excluded: set[str], excerpt_pids=frozenset()
    ):
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "public.nt.gz"
        with gzip.open(full, "wt") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        kept, dropped = filter_public_export(
            full, public, excluded, excerpt_pids
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

    def test_no_exclusions_passes_everything(self, tmp_path):
        kept, dropped, _ = self.filter(tmp_path, set())
        assert (kept, dropped) == (len(self.LINES), 0)


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
