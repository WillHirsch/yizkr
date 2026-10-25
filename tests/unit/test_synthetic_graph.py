"""Offline tests for the synthetic query-review graph (#251).

No Docker and no instance. rdflib is not a dependency, so the generator's
Turtle is parsed structurally: every line must be a prefix declaration or
one well-formed triple, and the shapes are then checked triple by triple.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from itertools import combinations, zip_longest
from pathlib import Path

import pytest

from yizkr.queries.publish import render_for_instance

ROOT = Path(__file__).resolve().parents[2]
QUERIES = sorted((ROOT / "src" / "yizkr" / "queries").glob("*.rq"))


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sg = load_script("synthetic_graph")
sandbox = load_script("wdqs_sandbox")

# -- a structural parser for the Turtle subset the generator writes ---------

IRI = r"<[^<>\"{}|^`\\\x00-\x20]*>"
PNAME = r"[a-z][a-z0-9]*:(?:[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)?"
STRING = r'"(?:[^"\\\n\r]|\\[tbnrf"\'\\])*"'
LITERAL = rf"{STRING}(?:@[a-z]+(?:-[a-z0-9]+)*|\^\^(?:{PNAME}|{IRI}))?"
PREFIX_LINE = re.compile(rf"@prefix ([a-z][a-z0-9]*): ({IRI}) \.")
TRIPLE = re.compile(
    rf"({IRI}|{PNAME}) ({IRI}|{PNAME}|a) ({IRI}|{PNAME}|{LITERAL}) \."
)
PLACEHOLDER = re.compile(r"\{([PQ]):([^{}\s]+)\}")
LABEL = re.compile(
    r"(Person|Place|Artifact|Event|Run) [1-9][0-9]*"
    r"|Artifact [1-9][0-9]*, page [1-9][0-9]*"
)


def prefix_of(term: str) -> str | None:
    """The prefix a term uses: its own, or its literal's datatype's."""
    if term == "a" or term.startswith("<"):
        return None
    if term.startswith('"'):
        datatype = re.search(r'\^\^([a-z][a-z0-9]*):[^"]*$', term)
        return datatype[1] if datatype else None
    return term.split(":", 1)[0]


def parse(text: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Prefixes and triples; fails on any line outside the subset, on a
    prefix used before it is declared, and on a missing final newline."""
    assert text.endswith("\n")
    prefixes: dict[str, str] = {}
    triples = []
    for line_number, line in enumerate(text[:-1].split("\n"), 1):
        if not line or line.startswith("#"):
            continue
        declared = PREFIX_LINE.fullmatch(line)
        if declared:
            prefixes[declared[1]] = declared[2][1:-1]
            continue
        match = TRIPLE.fullmatch(line)
        assert match, f"line {line_number} is not a triple: {line!r}"
        for term in match.groups():
            prefix = prefix_of(term)
            assert prefix is None or prefix in prefixes, (
                f"line {line_number}: undeclared prefix {prefix}"
            )
        triples.append(match.groups())
    return prefixes, triples


def number(literal: str) -> int:
    return int(re.fullmatch(r'"(-?\d+)"\^\^xsd:integer', literal)[1])


def iso_date(literal: str) -> str:
    """A time term's YYYY-MM-DD, comparable as a string in chronological
    order (every date here is zero-padded to the same width)."""
    return literal[1:11]


class Graph:
    """A generated graph, parsed and indexed for the shape checks."""

    def __init__(self, synthetic):
        self.text = synthetic.turtle
        self.vocab = synthetic.vocabulary
        self.counts = synthetic.counts
        self.prefixes, self.triples = parse(self.text)
        self.out: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for s, p, o in self.triples:
            self.out[s][p].append(o)
        self.redirects = {
            s: o for s, p, o in self.triples if p == "owl:sameAs"
        }

    def pid(self, key: str) -> str:
        return self.vocab["properties"][key]

    def item(self, key: str) -> str:
        return f"wd:{self.vocab['items'][key]}"

    def objects(self, subject: str, predicate: str) -> list[str]:
        return self.out.get(subject, {}).get(predicate, [])

    def statements(self, key: str) -> list[tuple[str, str]]:
        """(subject, statement) for every statement of a property."""
        predicate = f"p:{self.pid(key)}"
        return [(s, o) for s, p, o in self.triples if p == predicate]

    def all_statements(self) -> list[tuple[str, str, str]]:
        """(subject, property id, statement) for every statement."""
        return [
            (s, p[2:], o)
            for s, p, o in self.triples
            if p.startswith("p:") and o.startswith("wds:")
        ]

    def value(self, statement: str, key: str) -> str:
        return self.value_of(statement, self.pid(key))

    def value_of(self, statement: str, pid: str) -> str:
        (value,) = self.objects(statement, f"ps:{pid}")
        return value

    def qualifiers(self, statement: str, key: str) -> list[str]:
        return self.objects(statement, f"pq:{self.pid(key)}")

    def rank(self, statement: str) -> str:
        (rank,) = self.objects(statement, "wikibase:rank")
        return rank

    def sources(self, statement: str) -> set[str]:
        """Every stated-in value across the statement's references."""
        snak = f"pr:{self.pid('stated_in')}"
        return {
            source
            for reference in self.objects(statement, "prov:wasDerivedFrom")
            for source in self.objects(reference, snak)
        }

    def is_fragment(self, source: str) -> bool:
        instance_of = f"wdt:{self.pid('instance_of')}"
        return self.item("artifact_fragment") in self.objects(
            source, instance_of
        )

    def artifact(self, source: str) -> str:
        """The artifact a source is, or is a page fragment of."""
        if self.is_fragment(source):
            (whole,) = self.objects(source, f"wdt:{self.pid('part_of')}")
            return whole
        return source

    def precisions(self, statement: str, key: str) -> list[int]:
        return [
            number(precision)
            for node in self.objects(statement, f"psv:{self.pid(key)}")
            for precision in self.objects(node, "wikibase:timePrecision")
        ]

    def years(self, statement: str, key: str) -> tuple[int, int]:
        """The first and last year of the period a date value names."""
        (precision,) = self.precisions(statement, key)
        first = int(self.value(statement, key)[1:5])
        return first, first + {7: 99, 8: 9}.get(precision, 0)

    def labelled(self, prefix: str) -> dict[str, str]:
        """Generated entities (id under a kind's base) and their labels."""
        return {
            s: o
            for s, p, o in self.triples
            if p == "rdfs:label"
            and s.startswith(f"wd:Q{prefix}")
            and len(s) == len("wd:Q100000")
        }


# -- shape finders: each returns its evidence, empty when the shape is missing

DATES = ("date_of_birth", "date_of_death")


def dated_statements(g: Graph):
    """Each date statement, a date of death of unknown value (#687) left
    out."""
    for key in DATES:
        for subject, statement in g.statements(key):
            if not is_unknown(g.value(statement, key)):
                yield key, subject, statement


def is_unknown(term: str) -> bool:
    """Whether a term is Wikibase's "some value", a skolem IRI."""
    return "/.well-known/genid/" in term


def unknown_deaths(g: Graph) -> list[str]:
    """The dates of death of unknown value, each a statement of its own
    with no value node."""
    pid = g.pid("date_of_death")
    found = []
    for _, statement in g.statements("date_of_death"):
        if is_unknown(g.value(statement, "date_of_death")):
            assert not g.objects(statement, f"psv:{pid}")
            found.append(statement)
    return found


def date_precisions(g: Graph) -> list[int]:
    found = {
        p for key, _, st in dated_statements(g) for p in g.precisions(st, key)
    }
    return sorted(found) if found >= {7, 8, 9, 10, 11} else []


def ranges(g: Graph, both: bool) -> list[str]:
    """Dates with an earliest and a latest date, or with only one."""
    found = []
    for _, _, statement in dated_statements(g):
        bounds = [
            bool(g.qualifiers(statement, "earliest_date")),
            bool(g.qualifiers(statement, "latest_date")),
        ]
        if all(bounds) if both else sum(bounds) == 1:
            found.append(statement)
    return found


def circa(g: Graph) -> list[str]:
    return [
        statement
        for _, _, statement in dated_statements(g)
        if g.item("circa") in g.qualifiers(statement, "sourcing_circumstances")
    ]


def contradictions(g: Graph, within_one_artifact: bool) -> list[tuple]:
    """Two dates on one subject naming disjoint periods (not one inside
    the other, a difference of resolution), attributed either to different
    artifacts or to different fragments of one artifact."""
    found = []
    for key in DATES:
        by_subject = defaultdict(list)
        for found_key, subject, statement in dated_statements(g):
            if found_key == key:
                by_subject[subject].append(statement)
        for subject, statements in by_subject.items():
            for a, b in combinations(statements, 2):
                (first_a, last_a), (first_b, last_b) = (
                    g.years(a, key),
                    g.years(b, key),
                )
                if not (last_a < first_b or last_b < first_a):
                    continue
                sources_a, sources_b = g.sources(a), g.sources(b)
                if not sources_a or not sources_b:
                    continue
                whole_a = {g.artifact(s) for s in sources_a}
                whole_b = {g.artifact(s) for s in sources_b}
                if within_one_artifact:
                    fragments = all(
                        g.is_fragment(s) for s in sources_a | sources_b
                    )
                    if (
                        fragments
                        and sources_a.isdisjoint(sources_b)
                        and whole_a == whole_b
                        and len(whole_a) == 1
                    ):
                        found.append((subject, key))
                elif whole_a.isdisjoint(whole_b):
                    found.append((subject, key))
    return found


def containment_chains(g: Graph) -> list[list[str]]:
    """Places three or more location/part-of steps below another, through
    redirects."""
    steps = (f"wdt:{g.pid('location')}", f"wdt:{g.pid('part_of')}")
    place = g.item("place")
    instance_of = f"wdt:{g.pid('instance_of')}"
    parent = {}
    for s, p, o in g.triples:
        if p in steps and place in g.objects(s, instance_of):
            parent[s] = g.redirects.get(o, o)
    chains = []
    for start in parent:
        chain = [start]
        while chain[-1] in parent and len(chain) < 10:
            chain.append(parent[chain[-1]])
        if len(chain) >= 4:
            chains.append(chain)
    return chains


def dated_residences(g: Graph) -> list[str]:
    return [
        statement
        for _, statement in g.statements("residence")
        if g.qualifiers(statement, "start_time")
        and g.qualifiers(statement, "end_time")
    ]


def ranged_residences(g: Graph) -> list[str]:
    """Residences whose start and end each carry their own range
    (MODELING.md, "Marriages, residences and other periods")."""
    return [
        statement
        for _, statement in g.statements("residence")
        if g.qualifiers(statement, "earliest_date")
        and g.qualifiers(statement, "latest_start_date")
        and g.qualifiers(statement, "earliest_end_date")
        and g.qualifiers(statement, "latest_date")
    ]


def spouses(g: Graph) -> list[tuple[str, str]]:
    """Couples whose spouse statements run both ways, one of them dated."""
    pairs = defaultdict(list)
    for subject, statement in g.statements("spouse"):
        pairs[(subject, g.value(statement, "spouse"))].append(statement)
    return [
        (a, b)
        for (a, b), statements in pairs.items()
        if (b, a) in pairs
        and any(g.qualifiers(st, "start_time") for st in statements)
    ]


def ranked_dates(g: Graph, rank: str) -> list[tuple[str, str]]:
    """A person's date where a normal statement stands beside a `rank`
    one (a curator's deprecation or preference of one reading)."""
    groups = defaultdict(set)
    for key, subject, statement in dated_statements(g):
        groups[(subject, key)].add(g.rank(statement))
    wanted = {"wikibase:NormalRank", f"wikibase:{rank}Rank"}
    return [group for group, ranks in groups.items() if ranks >= wanted]


def unsourced(g: Graph, with_run: bool) -> list[str]:
    """Statements with no stated-in: a run reference only, or none at
    all."""
    found = []
    for _, _, statement in g.all_statements():
        references = g.objects(statement, "prov:wasDerivedFrom")
        if g.sources(statement):
            continue
        if bool(references) == with_run:
            found.append(statement)
    return found


def redirected_values(g: Graph) -> list[str]:
    return [
        st
        for _, pid, st in g.all_statements()
        if any(v in g.redirects for v in g.objects(st, f"ps:{pid}"))
    ]


def machine_and_curator(g: Graph) -> list[str]:
    """A subject and property with a machine statement (stated-in and a
    run) and a curator one (stated-in only)."""
    run = f"pr:{g.pid('inferred_from_extraction_run')}"
    kinds = defaultdict(set)
    for subject, pid, statement in g.all_statements():
        if not g.sources(statement):
            continue
        machine = any(
            g.objects(ref, run)
            for ref in g.objects(statement, "prov:wasDerivedFrom")
        )
        kinds[(subject, pid)].add(machine)
    return [key for key, seen in kinds.items() if seen == {True, False}]


def ranks_beyond_dates(g: Graph, wanted: set[str], only: bool) -> list:
    """Subjects and non-date properties whose statements hold exactly the
    ranks `wanted` (`only`: nothing but them; else at least them)."""
    dates = {g.pid(key) for key in DATES}
    groups = defaultdict(set)
    for subject, pid, statement in g.all_statements():
        if pid not in dates:
            groups[(subject, pid)].add(g.rank(statement))
    wanted = {f"wikibase:{rank}Rank" for rank in wanted}
    return [
        key
        for key, ranks in groups.items()
        if (ranks == wanted if only else ranks >= wanted)
    ]


SHAPES = {
    "dates at every precision": date_precisions,
    "earliest and latest ranges": lambda g: ranges(g, both=True),
    "open-ended ranges": lambda g: ranges(g, both=False),
    "circa": circa,
    "contradictions between artifacts": lambda g: contradictions(g, False),
    "contradictions within one artifact": lambda g: contradictions(g, True),
    "place containment chains": containment_chains,
    "residences with start and end": dated_residences,
    "residences with a range on each end": ranged_residences,
    "spouses": spouses,
    "a date beside a deprecated one": lambda g: ranked_dates(g, "Deprecated"),
    "a date beside a preferred one": lambda g: ranked_dates(g, "Preferred"),
    "a property stated only deprecated": lambda g: ranks_beyond_dates(
        g, {"Deprecated"}, only=True
    ),
    "preferred beside deprecated": lambda g: ranks_beyond_dates(
        g, {"Preferred", "Deprecated"}, only=False
    ),
    "run-only references": lambda g: unsourced(g, with_run=True),
    "unreferenced statements": lambda g: unsourced(g, with_run=False),
    "redirected values": redirected_values,
    "machine beside curator": machine_and_curator,
    "a date of death of unknown value": unknown_deaths,
}

PERSONS, SEED = 60, 11


@pytest.fixture(scope="module")
def graph() -> Graph:
    return Graph(sg.generate(PERSONS, SEED))


class TestTurtle:
    def test_every_line_is_a_prefix_or_a_triple(self, graph):
        # parse() asserted each line; the generator counts the same triples
        assert len(graph.triples) == graph.counts["triples"] > 5000
        assert graph.prefixes["wd"] == f"{sg.CONCEPT_URI}/entity/"

    def test_no_triple_repeats(self, graph):
        assert len(set(graph.triples)) == len(graph.triples)

    def test_value_and_reference_nodes_are_defined(self, graph):
        for s, p, o in graph.triples:
            if p.startswith(("psv:", "pqv:")):
                assert graph.objects(o, "wikibase:timeValue") or graph.objects(
                    o, "wikibase:geoLatitude"
                ), (s, p, o)
            if p == "prov:wasDerivedFrom":
                assert any(q.startswith("pr:") for q in graph.out[o]), o

    def test_type_triples_as_the_updater_keeps_them(self, graph):
        """The updater (no --keepTypes) drops item, statement and
        reference types, and keeps property, best-rank and value types."""
        types = {o for _, p, o in graph.triples if p == "a"}
        assert types == {
            "wikibase:Property",
            "wikibase:BestRank",
            "wikibase:TimeValue",
            "wikibase:GlobecoordinateValue",
        }

    def test_qualifier_values_carry_their_precision(self, graph):
        """Every time qualifier has its pqv: value node, with the same time
        and a precision, as psv: has for statement values."""
        checked = 0
        for _, _, statement in graph.all_statements():
            for p, objects in graph.out[statement].items():
                if not p.startswith("pq:"):
                    continue
                for value in objects:
                    if not value.endswith("^^xsd:dateTime"):
                        continue
                    nodes = graph.objects(statement, f"pqv:{p[3:]}")
                    matching = [
                        node
                        for node in nodes
                        if graph.objects(node, "wikibase:timeValue") == [value]
                    ]
                    assert len(matching) == 1, (statement, p, value)
                    (precision,) = graph.objects(
                        matching[0], "wikibase:timePrecision"
                    )
                    assert 7 <= number(precision) <= 11
                    checked += 1
        assert checked > 100

    def test_references_are_shared_by_content(self, graph):
        """One node per distinct reference, shared by every statement that
        cites it, as Wikibase names reference nodes by their content."""
        content = defaultdict(list)
        for node, predicates in graph.out.items():
            if node.startswith("wdref:"):
                snaks = tuple(
                    sorted((p, o) for p, os in predicates.items() for o in os)
                )
                content[snaks].append(node)
        assert all(len(nodes) == 1 for nodes in content.values())
        uses = defaultdict(int)
        for _, p, o in graph.triples:
            if p == "prov:wasDerivedFrom":
                uses[o] += 1
        run = f"pr:{graph.pid('inferred_from_extraction_run')}"
        run_nodes = [n for n in uses if graph.objects(n, run)]
        assert run_nodes and all(uses[n] > 10 for n in run_nodes)
        # an artifact's registrar statements cite the artifact itself,
        # through one node
        artifact = next(iter(graph.labelled("3")))
        own = {
            ref
            for p, statements in graph.out[artifact].items()
            if p.startswith("p:")
            for statement in statements
            for ref in graph.objects(statement, "prov:wasDerivedFrom")
        }
        assert len(own) == 1

    def test_source_references_are_nearly_one_per_statement(self, graph):
        """Most machine source references carry the claim's excerpt, as
        the writer's do, so each is one statement's alone."""
        excerpt = f"pr:{graph.pid('transcription_excerpt')}"
        uses = defaultdict(int)
        for _, p, o in graph.triples:
            if p == "prov:wasDerivedFrom":
                uses[o] += 1
        excerpted = [node for node in uses if graph.objects(node, excerpt)]
        assert excerpted and all(uses[node] == 1 for node in excerpted)
        stated_in = f"pr:{graph.pid('stated_in')}"
        sources = [node for node in uses if graph.objects(node, stated_in)]
        per_node = sum(uses[n] for n in sources) / len(sources)
        assert per_node < 1.5, per_node

    def test_text_values_are_placeholders(self, graph):
        """Names and excerpts, like labels, are neutral placeholders."""
        texts = [
            re.fullmatch(r'"(.*)"@[a-z]+', o)[1]
            for _, p, o in graph.triples
            if p.startswith(("ps:", "wdt:", "pr:"))
            and re.fullmatch(r'".*"@[a-z]+', o)
        ]
        assert any(t.startswith("Excerpt ") for t in texts)
        assert any(t.startswith("Name ") for t in texts)
        for text in texts:
            assert re.fullmatch(
                r"(Name|NAME) [1-9][0-9]*(-2)?|Excerpt [1-9][0-9]*", text
            ), text

    def test_every_statement_has_a_rank_and_one_value(self, graph):
        for _, pid, statement in graph.all_statements():
            assert graph.rank(statement) in {
                f"wikibase:{rank}Rank" for rank in sg.RANKS
            }
            graph.value_of(statement, pid)

    def test_literals_are_escaped(self):
        text = 'a "quoted" back\\slash\nnewline\ttab'
        assert re.fullmatch(STRING, sg.literal(text))

    def test_a_century_is_its_first_year_as_wikibase_counts_it(self):
        # the 19th century runs from 1801 to 1900 (#241): a date drawn in
        # it, at its first or its last year too, is stored as 1801, and
        # 1800 falls in the 18th
        for year in (1801, 1836, 1900):
            assert sg.when(year, precision=sg.CENTURY) == sg.Time(
                "1801-01-01", sg.CENTURY
            )
        assert sg.when(1800, precision=sg.CENTURY).date == "1701-01-01"
        stored = sg.when(1836, precision=sg.CENTURY)
        assert sg.years_of(stored) == (1801, 1900)


def difference(a: str, b: str) -> str:
    """Where two texts first differ, '' when they are equal: a short
    failure message where pytest would diff megabytes."""
    if a == b:
        return ""
    for line, pair in enumerate(zip_longest(a.split("\n"), b.split("\n")), 1):
        if pair[0] != pair[1]:
            return f"line {line}: {pair[0]!r} != {pair[1]!r}"
    raise AssertionError("unequal texts with equal lines")


def graph_lines(text: str) -> list[str]:
    """The graph without its header comment, which names the seed."""
    return [line for line in text.split("\n") if not line.startswith("#")]


class TestDeterminism:
    def test_same_seed_same_bytes(self):
        diff = difference(sg.generate(40, 5).turtle, sg.generate(40, 5).turtle)
        assert not diff, diff

    def test_another_seed_another_graph(self):
        first = graph_lines(sg.generate(40, 5).turtle)
        other = graph_lines(sg.generate(40, 6).turtle)
        assert first != other, "the seed does not change the graph"

    def test_bytes_do_not_depend_on_hash_randomisation(self, tmp_path):
        """Iteration over a set of strings would vary with PYTHONHASHSEED;
        two processes must still write the same files."""
        outputs = []
        for hash_seed in ("1", "2"):
            out = tmp_path / hash_seed / "g.ttl"
            env = {**os.environ, "PYTHONHASHSEED": hash_seed}
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "synthetic_graph.py"),
                    "--persons",
                    "30",
                    "--seed",
                    "4",
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                env=env,
            )
            vocab = out.with_suffix(".vocab.json")
            outputs.append((out.read_text(), vocab.read_text()))
        for first, second in zip(*outputs, strict=True):
            diff = difference(first, second)
            assert not diff, diff


class TestCounts:
    def test_exactly_the_requested_persons(self, graph):
        human = graph.item("human")
        instance_of = f"wdt:{graph.pid('instance_of')}"
        persons = {
            s for s, p, o in graph.triples if p == instance_of and o == human
        }
        assert len(persons) == PERSONS == graph.counts["person"]
        assert set(graph.labelled("1").values()) == {
            f'"Person {n}"@en' for n in range(1, PERSONS + 1)
        }

    def test_reported_counts_match_the_graph(self, graph):
        for kind, digit in (
            ("place", "2"),
            ("artifact", "3"),
            ("fragment", "4"),
            ("event", "5"),
            ("run", "6"),
        ):
            assert len(graph.labelled(digit)) == graph.counts[kind], kind
        assert len(graph.redirects) == graph.counts["redirect"]
        assert len(graph.all_statements()) == graph.counts["statements"]

    def test_size_scales_with_persons(self):
        small = sg.generate(40, 1).counts
        large = sg.generate(160, 1).counts
        for kind in ("place", "artifact", "event", "statements", "triples"):
            assert large[kind] > 2 * small[kind], kind

    def test_labels_are_neutral_placeholders(self, graph):
        manifest = {
            f"wd:{graph.vocab['properties'][k]}": v
            for k, v in sg.load_vocabulary().property_labels.items()
        } | {
            f"wd:{graph.vocab['items'][k]}": v
            for k, v in sg.load_vocabulary().item_labels.items()
        }
        for s, p, o in graph.triples:
            if p != "rdfs:label":
                continue
            text = re.fullmatch(r'"(.*)"@en', o)[1]
            if s in manifest:
                assert text == manifest[s]
            else:
                assert LABEL.fullmatch(text), (s, text)

    def test_too_few_persons_are_refused(self):
        with pytest.raises(ValueError, match="between"):
            sg.generate(sg.MIN_PERSONS - 1, 0)


class TestShapes:
    @pytest.mark.parametrize("shape", SHAPES)
    def test_shape_is_present(self, graph, shape):
        assert SHAPES[shape](graph), f"no {shape} in the graph"

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_every_shape_at_the_minimum_size(self, seed):
        small = Graph(sg.generate(sg.MIN_PERSONS, seed))
        missing = [name for name, find in SHAPES.items() if not find(small)]
        assert not missing

    def test_truthy_triples_follow_the_best_rank(self, graph):
        """Per subject and property, the best rank is preferred when any
        statement has it, else normal: BestRank marks exactly those
        statements, and wdt: holds exactly their values (none for a
        property whose statements are all deprecated)."""
        groups = defaultdict(list)
        for subject, pid, statement in graph.all_statements():
            groups[(subject, pid)].append(statement)
        preferred, normal = "wikibase:PreferredRank", "wikibase:NormalRank"
        for (subject, pid), statements in groups.items():
            ranks = {graph.rank(st) for st in statements}
            best = preferred if preferred in ranks else normal
            expected = set()
            for st in statements:
                is_best = graph.rank(st) == best
                marked = "wikibase:BestRank" in graph.objects(st, "a")
                assert marked == is_best, st
                if is_best:
                    expected.add(graph.value_of(st, pid))
            assert set(graph.objects(subject, f"wdt:{pid}")) == expected

    def test_redirects_are_bare(self, graph):
        for redirect, target in graph.redirects.items():
            assert list(graph.out[redirect]) == ["owl:sameAs"]
            assert target not in graph.redirects
            assert graph.out[target], target

    def test_residence_ranges_bound_their_own_end(self, graph):
        """MODELING.md, "Marriages, residences and other periods": a
        start's range (earliest_date/latest_start_date) contains
        start_time, and an end's range (earliest_end_date/latest_date)
        contains end_time, each pair bounding only its own end."""
        found = ranged_residences(graph)
        assert found
        for statement in found:
            start = iso_date(graph.qualifiers(statement, "start_time")[0])
            end = iso_date(graph.qualifiers(statement, "end_time")[0])
            earliest = iso_date(
                graph.qualifiers(statement, "earliest_date")[0]
            )
            latest_start = iso_date(
                graph.qualifiers(statement, "latest_start_date")[0]
            )
            earliest_end = iso_date(
                graph.qualifiers(statement, "earliest_end_date")[0]
            )
            latest = iso_date(graph.qualifiers(statement, "latest_date")[0])
            assert earliest <= start <= latest_start, statement
            assert earliest_end <= end <= latest, statement


class TestVocabulary:
    def test_map_covers_every_template_placeholder(self, graph):
        assert QUERIES
        for path in QUERIES:
            for kind, key in PLACEHOLDER.findall(path.read_text()):
                table = graph.vocab["properties" if kind == "P" else "items"]
                assert key in table, f"{path.name}: {{{kind}:{key}}}"

    def test_map_ids_are_declared_in_the_graph(self, graph):
        for pid in graph.vocab["properties"].values():
            assert "wikibase:Property" in graph.objects(f"wd:{pid}", "a")
            assert graph.objects(f"wd:{pid}", "wikibase:claim") == [f"p:{pid}"]
        for qid in graph.vocab["items"].values():
            assert graph.objects(f"wd:{qid}", "rdfs:label"), qid

    def test_templates_render_alike_with_the_sandbox_adapter(self, graph):
        vocab = graph.vocab
        for path in QUERIES:
            template = path.read_text()
            args = (
                template,
                vocab["properties"],
                vocab["items"],
                vocab["concept_uri"],
            )
            rendered = render_for_instance(*args)
            assert not PLACEHOLDER.search(rendered), path.name
            assert sandbox.adapter_render(*args) == rendered, path.name

    def test_cli_writes_the_graph_and_the_map(self, tmp_path, capsys):
        out = tmp_path / "graph.ttl"
        code = sg.main(["--persons", "20", "--seed", "3", "--out", str(out)])
        expected = sg.generate(20, 3)
        assert code == 0
        diff = difference(out.read_text(), expected.turtle)
        assert not diff, diff
        vocab_path = tmp_path / "graph.vocab.json"
        assert json.loads(vocab_path.read_text()) == expected.vocabulary
        assert "20 persons" in capsys.readouterr().out
