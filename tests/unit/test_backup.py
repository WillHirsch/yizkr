"""Offline tests for backup/export logic. No stack, no docker. The
possibly-living determination itself is tested in test_privacy.py.

The public export keeps only the people the check releases (#344): it
reads person evidence from its own dump, at every rank, and drops every
item with any that the check did not release, and it refuses to run
while the query service, where the check runs, lags the dump."""

import datetime
import gzip
import json
import re
from pathlib import Path

import pytest
import yaml

from tests.unit.test_privacy import (
    BIRTHS,
    CASES,
    CONCEPT,
    EXCLUDED,
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
    stale_items,
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
        assert (
            f"?person wdt:{properties['instance_of']} wd:{items['human']}"
            not in facts
        )


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


# -- the public export keeps only released people (#344) --------------------

RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
ONTOLOGY = "http://wikiba.se/ontology#"
DATA = f"{CONCEPT}/wiki/Special:EntityData/"


def entity(qid: str) -> str:
    return f"<{CONCEPT}/entity/{qid}>"


def dated(year: int) -> str:
    return f'"{year}-01-01T00:00:00Z"^^<{XSD}dateTime>'


class Dump:
    """A fictional dump in the N-Triples dumpRdf.php writes: each entity's
    data node with its revision, its type and label, each statement
    through its node, with its rank and value (and the truthy triple of a
    best-ranked one), and each redirect as owl:sameAs its target."""

    def __init__(self, vocabulary):
        self.properties, self.items = vocabulary
        self.lines: list[str] = []
        self.revisions: dict[str, int] = {}

    def data_node(self, qid: str, revision: int) -> None:
        node = f"<{DATA}{qid}>"
        self.lines += [
            f"{node} {RDF_TYPE} <http://schema.org/Dataset> .",
            f"{node} <http://schema.org/about> {entity(qid)} .",
            f'{node} <http://schema.org/version> "{revision}"'
            f"^^<{XSD}integer> .",
        ]

    def entity(self, qid: str, label: str, revision: int = 1) -> None:
        self.data_node(qid, revision)
        self.revisions[qid] = revision
        self.lines += [
            f"{entity(qid)} {RDF_TYPE} <{ONTOLOGY}Item> .",
            f"{entity(qid)} <http://www.w3.org/2000/01/rdf-schema#label> "
            f'"{label}"@en .',
        ]

    def says(
        self, subject: str, key: str, value: str, rank="Normal", best=None
    ) -> None:
        pid = self.properties[key]
        node = f"<{CONCEPT}/entity/statement/{subject}-{len(self.lines):04x}>"
        self.lines += [
            f"{entity(subject)} <{CONCEPT}/prop/{pid}> {node} .",
            f"{node} {RDF_TYPE} <{ONTOLOGY}Statement> .",
            f"{node} <{ONTOLOGY}rank> <{ONTOLOGY}{rank}Rank> .",
            f"{node} <{CONCEPT}/prop/statement/{pid}> {value} .",
        ]
        if rank != "Deprecated" if best is None else best:
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
# a curator left it, and items without any, which the export keeps.
PEOPLE = {
    "Q401": "typed human, with a death: the check releases them",
    "Q402": "typed human at deprecated rank only",
    "Q403": "typed human, outranked by a preferred organization type",
    "Q404": "no type, with a birth",
    "Q405": "no type, the value of a spouse statement",
    "Q406": "no type, taking part in a death (the part deprecated)",
    "Q415": "no type, taking part in a deportation (so typed at deprecated "
    "rank only)",
}
OTHERS = {
    "Q407": "a death event",
    "Q408": "a place",
    "Q409": "a certificate",
    "Q411": "an organization taking part in an event of no person kind",
    "Q412": "an event of no person kind",
    "Q413": "an occupation",
    "Q414": "an item with only a nickname",
    "Q416": "a deportation",
}
# Q410 redirects to Q402, whom the check does not release; Q418 to Q401,
# whom it does. A photograph depicts each: no person evidence of its own.
REDIRECTS = {"Q410": "Q402", "Q418": "Q401"}


def export_dump(vocabulary) -> Dump:
    _, items = vocabulary
    dump = Dump(vocabulary)
    for key, qid in items.items():
        dump.entity(qid, key)
    for qid, description in {**PEOPLE, **OTHERS}.items():
        dump.entity(qid, description, revision=int(qid[1:]))

    def typed(qid: str, key: str, **options) -> None:
        dump.says(qid, "instance_of", entity(items[key]), **options)

    typed("Q401", "human")
    dump.says("Q401", "date_of_death", dated(1996))
    dump.says("Q401", "sex_or_gender", entity(items["male"]))
    dump.says("Q401", "occupation", entity("Q413"))
    dump.says("Q401", "place_of_birth", entity("Q408"))
    dump.says("Q401", "spouse", entity("Q405"))
    dump.says("Q401", "father", "_:genid1")  # an unknown value
    typed("Q402", "human", rank="Deprecated")
    dump.says("Q402", "nickname", '"Wastl"@de')
    typed("Q403", "human", best=False)
    typed("Q403", "organization", rank="Preferred")
    dump.says("Q404", "date_of_birth", dated(1990))
    typed("Q407", "death")
    dump.says("Q407", "participant", entity("Q406"), rank="Deprecated")
    typed("Q408", "place")
    typed("Q409", "certificate")
    dump.says("Q409", "depicts", entity("Q410"))
    dump.says("Q409", "depicts", entity("Q418"))
    typed("Q411", "organization")
    typed("Q412", "event")
    dump.says("Q412", "participant", entity("Q411"))
    typed("Q413", "occupation_class")
    dump.says("Q414", "nickname", '"Gus"@en')
    typed("Q416", "deportation", rank="Deprecated")
    dump.says("Q416", "participant", entity("Q415"))
    for source, target in REDIRECTS.items():
        dump.redirect(source, target, revision=int(source[1:]))
    return dump


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
        _, items = vocabulary
        found = self.reading(tmp_path, vocabulary)
        assert not found.people & (set(OTHERS) | set(items.values()))

    def test_the_lines_may_come_in_any_order(self, tmp_path, vocabulary):
        # a statement's value can come before the link to its subject
        lines = export_dump(vocabulary).lines[::-1]
        found = self.reading(tmp_path, vocabulary, lines)
        assert found.people == set(PEOPLE)

    def test_redirects_and_revisions(self, tmp_path, vocabulary):
        found = self.reading(tmp_path, vocabulary)
        assert found.redirects == REDIRECTS
        # the query service keeps no revision for a redirect
        assert found.revisions == export_dump(vocabulary).revisions
        assert not set(REDIRECTS) & set(found.revisions)


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
    dropped: an item it never assessed is dropped, not kept (#344)."""

    reading = DumpReading(
        people=set(PEOPLE),
        # a chain to Q402, and a cycle
        redirects={
            **REDIRECTS,
            "Q420": "Q410",
            "Q421": "Q422",
            "Q422": "Q421",
        },
    )

    def test_only_released_people_are_kept(self):
        dropped = public_exclusions(
            self.reading, report_of({"Q401"}, set(PEOPLE) - {"Q401"})
        )
        assert set(dropped) == set(PEOPLE) - {"Q401"} | {"Q410", "Q420"}
        assert dropped["Q404"] == "possibly living (Q404)"

    def test_a_person_the_check_never_assessed_is_dropped(self):
        # as the truthy human type selected them: none of Q402-Q406 or Q415
        dropped = public_exclusions(self.reading, report_of({"Q401"}))
        assert set(dropped) == set(PEOPLE) - {"Q401"} | {"Q410", "Q420"}
        assert dropped["Q403"] == NOT_ASSESSED

    def test_a_person_only_the_check_found_is_dropped(self):
        dropped = public_exclusions(
            DumpReading(), report_of({"Q401"}, {"Q499"})
        )
        assert dropped == {"Q499": "possibly living (Q499)"}

    def test_a_redirect_goes_with_the_item_it_redirects_to(self):
        dropped = public_exclusions(
            self.reading, report_of({"Q401"}, {"Q402"})
        )
        # to a person not released, through a chain too, with no evidence
        # of its own
        assert dropped["Q410"] == "a redirect to Q402: possibly living (Q402)"
        assert dropped["Q420"] == dropped["Q410"]
        # to one released
        assert "Q418" not in dropped
        # a cycle of redirects ends
        assert not {"Q421", "Q422"} & set(dropped)

    def test_items_without_person_evidence_are_kept(self):
        dropped = public_exclusions(self.reading, report_of({"Q401"}))
        assert not set(OTHERS) & set(dropped)


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

    DUMPED = {"Q401": 11, "Q402": 12, "Q9": 3}

    def test_the_same_revisions(self):
        run = lambda query: revision_rows(self.DUMPED)  # noqa: E731
        assert stale_items(run, self.DUMPED) == []

    def test_an_item_behind_ahead_missing_or_extra_is_stale(self):
        held = {"Q401": 10, "Q402": 13, "Q403": 1}  # Q9 missing
        run = lambda query: revision_rows(held)  # noqa: E731
        assert stale_items(run, self.DUMPED) == ["Q9", "Q401", "Q402", "Q403"]

    def test_two_revisions_of_one_item_are_stale(self):
        rows = revision_rows(self.DUMPED) + revision_rows({"Q401": 10})
        assert stale_items(lambda query: rows, self.DUMPED) == ["Q401"]

    def test_only_items_are_compared(self):
        # a property's revision is not the check's to hold
        rows = revision_rows(self.DUMPED) + revision_rows({"P1": 3})
        assert stale_items(lambda query: rows, self.DUMPED) == []

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
        refusal = r"after 60 s .* 1 item \(Q401\)"
        with pytest.raises(QueryServiceLag, match=refusal):
            await_query_service(
                lambda query: revision_rows({**self.DUMPED, "Q401": 10}),
                self.DUMPED,
                timeout=60,
                sleep=clock.sleep,
                clock=clock,
            )
        assert clock.now == 60


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

    def export(self, tmp_path, vocabulary, selected=tuple(PEOPLE), **run):
        dump = export_dump(vocabulary)
        dump.write(tmp_path / "full.nt.gz")
        clock = Clock()
        runner = ExportRun(dump.revisions, selected, **run)
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
        text, mentioned = self.public(tmp_path)
        assert "Q401" in mentioned and PEOPLE["Q401"] in text
        assert not (set(PEOPLE) - {"Q401"}) & mentioned
        # a redirect goes with its target
        assert "Q418" in mentioned and "Q410" not in mentioned
        # places, artifacts, events, occupations and vocabulary stay
        assert set(OTHERS) | set(items.values()) <= mentioned

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
        assert set(exclusions["excluded"]) == (
            set(PEOPLE) - {"Q401"} | {"Q410"}
        )
        assert "born as late as 1990" in exclusions["excluded"]["Q404"]
        assert exclusions["included_persons"] == ["Q401"]
        assert counts["excluded_persons"] == len(PEOPLE)
        assert counts["included_persons"] == 1
        assert counts["triples_dropped"] > 0

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
        assert export["excluded_persons"] == len(PEOPLE)
        _, mentioned = TestExportPublic.public(out_dir)
        assert not (set(PEOPLE) - {"Q401"}) & mentioned

    def test_a_refused_export_leaves_the_backup_recorded(
        self, tmp_path, vocabulary, backed_up
    ):
        (tmp_path / "20260101T000000Z").mkdir()  # an older run
        behind = {**export_dump(vocabulary).revisions, "Q404": 1}
        with pytest.raises(QueryServiceLag, match="Q404"):
            backed_up(revisions=[behind], keep=1)
        (out_dir,) = tmp_path.iterdir()
        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert set(manifest["components"]) == {"database", "media", "rdf"}
        assert "Q404" in manifest["public_export_refused"]
        assert manifest["pruned"] == ["20260101T000000Z"]
        assert not (out_dir / "public.nt.gz").exists()
