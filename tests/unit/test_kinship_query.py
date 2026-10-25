"""Offline contract tests for the kinship query (pytest; no instance
needed).

A row of the query reads "?relative is ?subject's ?relationship". A
kinship term a source asserts is stored with the person the term names as
the statement's subject and the ego as its value (ADR-0018): the
universe's interview records "Aunt Pünktchen" as ``Klara —relative→
Nicholas ; kinship_type = "aunt"`` (docs/EXAMPLES.md), so that statement
is Nicholas's row, with Klara as the relative (#229), and Klara gets the
inverse row, with Nicholas as her nephew (#276).

The query is a chain of the query service's named subqueries (``WITH {
... } AS %name``, ``INCLUDE %name``). These tests read that structure:
which statements each set reads and at which ranks, how the sets join,
and which relation each path of steps gives. No offline engine runs the
query, so tests/integration/test_live_kinship.py checks its rows on a
universe fixture on the running stack.
"""

import itertools
import re
from collections import Counter

import pytest

from yizkr.queries.publish import load_templates


@pytest.fixture(scope="module")
def q() -> str:
    """The kinship template's query text, without its comment lines."""
    return "\n".join(
        line
        for line in load_templates()["kinship"].splitlines()
        if not line.lstrip().startswith("#")
    )


def edges(query: str, predicate: str) -> list[tuple[str, str]]:
    """The (subject, value) variables of each triple pattern whose
    predicate is exactly ``predicate``, such as ``p:{P:relative}``."""
    return re.findall(rf"(\?\w+)\s+{re.escape(predicate)}\s+(\?\w+)", query)


def closing(text: str, start: int) -> int:
    """The index of the brace that closes the one at ``start``."""
    depth = 0
    for index in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return index
    raise ValueError("unbalanced braces")


def groups(text: str) -> list[str]:
    """The top-level ``{ ... }`` groups of ``text``, without their braces
    (the arms of a UNION)."""
    found, index = [], text.find("{")
    while index != -1:
        end = closing(text, index)
        found.append(text[index + 1 : end])
        index = text.find("{", end)
    return found


def named_sets(query: str) -> dict[str, str]:
    """Each named subquery's body (``WITH { body } AS %name``), by name."""
    sets = {}
    for match in re.finditer(r"\bWITH\s*\{", query):
        end = closing(query, match.end() - 1)
        (name,) = re.match(r"\s*AS\s+%(\w+)", query[end + 1 :]).groups()
        sets[name] = query[match.end() : end]
    return sets


@pytest.fixture(scope="module")
def sets(q) -> dict[str, str]:
    return named_sets(q)


@pytest.fixture(scope="module")
def main(q) -> str:
    """The main WHERE group, after the last named set."""
    return q[q.rindex("} AS %") :]


def includes(text: str) -> list[str]:
    return re.findall(r"INCLUDE\s+%(\w+)", text)


def kin_arms(sets: dict[str, str]) -> list[str]:
    """The arms of the UNION that makes the rows (``%kin``)."""
    body = sets["kin"]
    (where,) = groups(body[body.index("WHERE") :])
    return groups(where)


# a cousin has one term, whatever the cousin's sex
UNGENDERED = {"cousin"}


def gender_map(q: str) -> dict[str, tuple[str, str, str]]:
    """Each gendered base relation's (male, female, neutral) terms."""
    return {
        base: forms
        for base, *forms in re.findall(
            r'IF\(\?base = "([\w-]+)", IF\(\?g = "m", "([\w -]+)", '
            r'IF\(\?g = "f", "([\w -]+)", "([\w -]+)"\)\)',
            q,
        )
    }


def inverse_table(q: str) -> dict[str, str]:
    """The inverse arm's table: a term (lower case) -> its inverse base."""
    (body,) = re.findall(r"VALUES \(\?key \?inverse\) \{(.*?)\}", q, re.S)
    table = dict(re.findall(r'\("([^"]+)" "([^"]+)"\)', body))
    assert len(table) == body.count("("), "a term is listed twice"
    return table


# The inverse arm, step by step. Each <name> is a pattern, a list or the
# table that the test reads from the arm and applies; every other
# character is pinned, so the steps the test evaluates are the arm's own,
# on the same variables and in the same order.
INVERSE_ARM = [
    "SELECT (?named AS ?subject) (?ego AS ?relative) ?base ?degree WHERE {",
    "INCLUDE %asserted",
    'BIND(REPLACE(LCASE(?term), "<trim>", "") AS ?said)',
    'BIND(REPLACE(?said, "<side>", "") AS ?unsided)',
    'BIND(REPLACE(?unsided, "<greats>", "$1") AS ?greats)',
    "BIND(SUBSTR(?unsided, STRLEN(?greats) + 1) AS ?core)",
    "OPTIONAL { VALUES (?key ?inverse) { <table> } FILTER(?key = ?core) }",
    'BIND(BOUND(?inverse) && (?greats = "" '
    "|| ?inverse IN (<kept>)) AS ?known)",
    "BIND(IF(?known, ?inverse, "
    'IF(REGEX(?unsided, "<cousin>"), ?unsided, "relative")) AS ?base)',
    'BIND(IF(?known, REPLACE(?greats, "<step>", "great-"), "") AS ?degree)',
    "}",
]


def inverse_arm(sets: dict[str, str]) -> dict[str, str]:
    """What the inverse arm holds at each <name> of INVERSE_ARM, which
    must match the arm's text, whitespace aside."""
    (arm,) = [a for a in kin_arms(sets) if "?inverse" in a]
    parts = re.split(r"<(\w+)>", " ".join(INVERSE_ARM))
    template = "".join(
        f"(?P<{part}>.*?)" if odd else re.escape(part)
        for odd, part in zip(itertools.cycle((False, True)), parts)
    )
    found = re.fullmatch(template, " ".join(arm.split()))
    assert found, "the inverse arm is not the chain of steps INVERSE_ARM pins"
    return found.groupdict()


def pattern(literal: str) -> str:
    """A regular expression as the query's string literal writes it."""
    return literal.replace("\\\\", "\\")


class TestAssertedKinship:
    """The `relative` + `kinship_type` arms: the query's reading of a
    kinship term that a source asserts, and its inverse."""

    def test_the_named_person_is_the_relative_and_the_ego_the_subject(
        self, q, sets
    ):
        ((named, statement),) = edges(q, "p:{P:relative}")
        ((valued, ego),) = edges(q, "ps:{P:relative}")
        # one statement: its subject is the person the term names, and its
        # value is the ego
        assert valued == statement
        assert (named, ego) == ("?named", "?ego")
        (forward,) = [a for a in kin_arms(sets) if "(?term AS ?base)" in a]
        assert "SELECT (?ego AS ?subject) (?named AS ?relative)" in forward

    def test_the_named_relative_gets_the_inverse_row(self, sets):
        # the ego is the named relative's relative, so the inverse term is
        # gendered by the ego's sex
        (inverse,) = [a for a in kin_arms(sets) if "?inverse" in a]
        assert includes(inverse) == ["asserted"]
        assert "SELECT (?named AS ?subject) (?ego AS ?relative)" in inverse

    def test_the_term_is_that_statements_kinship_type(self, q):
        ((_, statement),) = edges(q, "p:{P:relative}")
        ((qualified, term),) = edges(q, "pq:{P:kinship_type}")
        assert qualified == statement
        assert f'BIND(COALESCE(STR({term}), "relative") AS ?term)' in q

    def test_a_term_is_gendered_by_the_person_it_names(self, q):
        ((person, statement),) = edges(q, "p:{P:sex_or_gender}")
        assert person == "?relative"
        assert edges(q, "ps:{P:sex_or_gender}") == [(statement, "?sex")]


class TestInverseTerms:
    """ADR-0018: the inverse of an asserted term is derived, not stored."""

    def test_every_term_the_query_gives_has_its_inverse(self, q):
        # father, mother and parent all invert to child, whose terms invert
        # back to parent: the table and the gendered terms agree
        table, terms = inverse_table(q), gender_map(q)
        assert set(table.values()) <= set(terms) | UNGENDERED
        for base, forms in terms.items():
            (inverse,) = {table[form] for form in forms}
            assert {table[form] for form in terms[inverse]} == {base}

    @pytest.mark.parametrize(
        ("term", "inverse", "degree"),
        [
            ("aunt", "nibling", ""),
            ("Great-Aunt", "nibling", "great-"),
            ("great great grandmother", "grandchild", "great-great-"),
            ("maternal aunt", "nibling", ""),
            ("Paternal Uncle", "nibling", ""),
            ("paternal great grandmother", "grandchild", "great-"),
            ("Oma", "grandchild", ""),
            ("Urgroßmutter", "grandchild", "great-"),
            ("Tante", "nibling", ""),
            (" aunt ", "nibling", ""),
            ("Schwiegermutter", "child-in-law", ""),
            ("Vetter", "cousin", ""),
            ("second cousin", "second cousin", ""),
            ("maternal second cousin", "second cousin", ""),
            ("double cousin", "double cousin", ""),
            ("first cousin once removed", "first cousin once removed", ""),
            ("2nd cousin 3 times removed", "2nd cousin 3 times removed", ""),
            # a cousin term with any other word asserts no kind
            ("Dad's cousin", "relative", ""),
            # with a typographic apostrophe
            ("Dad\u2019s cousin", "relative", ""),
            ("my mother's cousin", "relative", ""),
            ("cousin's wife", "relative", ""),
            ("cousin of my father", "relative", ""),
            ("great-cousin", "relative", ""),
            ("Großcousin", "relative", ""),
            ("Großcousine", "relative", ""),
            ("cousin-in-law", "relative", ""),
            ("great-father", "relative", ""),
            ("godmother", "relative", ""),
            # the step and half terms (ADR-0040 §4), each with its inverse
            ("stepmother", "stepchild", ""),
            ("Stiefvater", "stepchild", ""),
            ("Stieftochter", "stepparent", ""),
            ("Stiefbruder", "stepsibling", ""),
            ("Halbbruder", "half-sibling", ""),
            ("half-sister", "half-sibling", ""),
        ],
    )
    def test_a_term_is_read_as_the_inverse_arm_reads_it(
        self, q, sets, term, inverse, degree
    ):
        """The arm's steps, as INVERSE_ARM pins them, applied in turn
        with the arm's own patterns: a side is dropped, a "great-" (or
        "Ur-") degree is kept on a grand-, aunt or uncle, niece or nephew
        term, a plain cousin term the table does not list is its own
        inverse, and any other term the table does not know gives
        "relative"."""
        arm = inverse_arm(sets)
        said = re.sub(pattern(arm["trim"]), "", term.lower())
        unsided = re.sub(pattern(arm["side"]), "", said)
        greats = re.sub(pattern(arm["greats"]), r"\1", unsided)
        core = unsided[len(greats) :]
        found = inverse_table(q).get(core)
        known = found is not None and (
            greats == "" or f'"{found}"' in arm["kept"].split(", ")
        )
        if known:
            base = found
            degrees = re.sub(pattern(arm["step"]), "great-", greats)
        else:
            cousin = re.search(pattern(arm["cousin"]), unsided)
            base, degrees = (unsided if cousin else "relative"), ""
        assert (base, degrees) == (inverse, degree)

    def test_the_inverse_terms_degree_reaches_its_row(self, sets, main):
        # the rows' set passes the arm's degree on, and the row's term is
        # that degree before the gendered base ("great-" + "nephew")
        assert re.match(
            r"\s*SELECT \?subject \?relative \?base \?degree \?through "
            r"WHERE",
            sets["kin"],
        )
        assert (
            'BIND(CONCAT(COALESCE(?degree, ""), ?kindPrefix, ?gendered, '
            "?kindSuffix) AS ?relationship)" in " ".join(main.split())
        )


class TestRanks:
    """Rules 4 and 6: every non-deprecated value is read, and no
    preferred value is required."""

    def test_no_truthy_triple_is_read(self, q):
        # a truthy triple holds only the best-ranked values, so a preferred
        # father would hide a normal-rank one
        assert "wdt:" not in q

    def test_no_value_needs_a_preferred_rank(self, q):
        assert "PreferredRank" not in q
        assert "BestRank" not in q

    def test_every_statement_read_skips_deprecated_ones(self, sets, main):
        read = 0
        for name, body in {**sets, "main": main}.items():
            for _, key, statement in re.findall(
                r"(\?\w+)\s+p:\{P:(\w+)\}\s+(\?\w+)", body
            ):
                read += 1
                (rank,) = set(
                    re.findall(
                        rf"{re.escape(statement)}\s+"
                        r"(?:ps:\{P:\w+\}\s+\S+\s*;\s*)?"
                        r"wikibase:rank\s+(\?\w+)",
                        body,
                    )
                )
                assert f"FILTER({rank} != wikibase:DeprecatedRank)" in body, (
                    f"{name} reads a deprecated {key} statement"
                )
        # parents (3), spouses (2), siblings (2), asserted terms, humans
        # and the relative's sex
        assert read == 10


class TestSteps:
    """ADR-0014's primitive steps, each read from every side a source
    may state it on."""

    def test_a_parent_is_read_from_either_side(self, sets):
        parents = sets["links"]
        # father or mother on the child, or child on the parent
        for key in ("father", "mother"):
            assert edges(parents, f"p:{{P:{key}}}") == [("?child", "?st")]
            assert edges(parents, f"ps:{{P:{key}}}") == [("?st", "?parent")]
        assert edges(parents, "p:{P:child}") == [("?parent", "?st")]
        assert edges(parents, "ps:{P:child}") == [("?st", "?child")]

    @pytest.mark.parametrize(
        ("key", "name"), [("spouse", "spouses"), ("sibling", "siblings")]
    )
    def test_spouse_and_sibling_statements_are_read_both_ways(
        self, sets, key, name
    ):
        body = sets[name]
        stated = edges(body, f"p:{{P:{key}}}")
        valued = edges(body, f"ps:{{P:{key}}}")
        # ?b is ?a's spouse or sibling whichever of them the statement is on
        assert list(zip(stated, valued, strict=True)) == [
            (("?a", "?st"), ("?st", "?b")),
            (("?b", "?st"), ("?st", "?a")),
        ]

    def test_two_children_of_one_parent_are_siblings(self, sets):
        assert includes(sets["siblings"]) == ["childA", "childB"]
        for name, child in (("childA", "?a"), ("childB", "?b")):
            assert f"SELECT (?child AS {child}) ?parent" in sets[name]
            assert includes(sets[name]) == ["parents"]
        assert "FILTER(?a != ?b)" in sets["siblings"]


# A path's relation, as ADR-0014 composes it from the primitive steps.
RELATIONS = {
    "parent": "parent",
    "child": "child",
    "spouse": "spouse",
    "sibling": "sibling",
    "parent/parent": "grandparent",
    "child/child": "grandchild",
    "parent/sibling": "pibling",
    "sibling/child": "nibling",
    "parent/sibling/child": "cousin",
    # an aunt or uncle by marriage, and its inverse
    "parent/sibling/spouse": "pibling",
    "spouse/sibling/child": "nibling",
    "spouse/parent": "parent-in-law",
    "child/spouse": "child-in-law",
    "sibling/spouse": "sibling-in-law",
    "spouse/sibling": "sibling-in-law",
}

# the sets that read statements: ?to is ?from's step; a parent's kind
# (`links`, and the `parents` paths take) is a flag the paths carry, which
# the evaluation below leaves unbound
STEPS = {
    "links": ("?child", "parent", "?parent"),
    "parents": ("?child", "parent", "?parent"),
    "spouses": ("?a", "spouse", "?b"),
    "siblings": ("?a", "sibling", "?b"),
}
BACKWARDS = {"parent": "child", "spouse": "spouse", "sibling": "sibling"}
FILTERED = re.compile(r'FILTER\((\?\w+) = "(\w+)"\)')
PROJECTED = re.compile(r"\(((?:[^()]|\([^()]*\))*?)\s+AS\s+(\?\w+)\)|(\?\w+)")


class Paths:
    """Evaluates the path sets over symbolic people: a row binds each
    variable to a person (a number) or a string, and carries the steps
    between its people. Joining two rows makes a variable they share one
    person."""

    def __init__(self, sets: dict[str, str]):
        self.sets = sets
        self.people = itertools.count()

    def rows(self, name: str) -> list[tuple[dict, tuple]]:
        if name in STEPS:
            start, step, end = STEPS[name]
            a, b = next(self.people), next(self.people)
            return [({start: a, end: b}, ((a, step, b),))]
        return self.select(self.sets[name])

    def select(self, text: str) -> list[tuple[dict, tuple]]:
        projection, group = re.fullmatch(
            r"\s*SELECT\s+(?:DISTINCT\s+)?(.*?)\s+WHERE\s*\{(.*)\}\s*",
            text,
            re.S,
        ).groups()
        if group.strip().startswith("{"):
            rows = [row for arm in groups(group) for row in self.select(arm)]
        else:
            rows = [({}, ())]
            for name in includes(group):
                rows = [
                    joined
                    for left in rows
                    for right in self.rows(name)
                    if (joined := self.join(left, right))
                ]
            for var, value in re.findall(FILTERED, group):
                rows = [row for row in rows if row[0][var] == value]
        return [(self.project(projection, b), steps) for b, steps in rows]

    @staticmethod
    def join(left, right):
        (bound, steps), (other, more) = left, right
        same = {}
        for var in bound.keys() & other.keys():
            if isinstance(bound[var], int) and isinstance(other[var], int):
                same[other[var]] = bound[var]
            elif bound[var] != other[var]:
                return None

        def person(value):
            return same.get(value, value) if isinstance(value, int) else value

        return (
            {**{v: person(x) for v, x in other.items()}, **bound},
            steps + tuple((person(a), s, person(b)) for a, s, b in more),
        )

    @staticmethod
    def project(projection: str, bound: dict) -> dict:
        out = {}
        for expression, alias, plain in PROJECTED.findall(projection):
            if plain:
                out[plain] = bound.get(plain)
            elif re.fullmatch(r"\?\w+", expression):
                out[alias] = bound.get(expression)
            elif constant := re.fullmatch(r'"([\w-]+)"', expression):
                out[alias] = constant.group(1)
            elif chosen := re.fullmatch(
                r'IF\((\?\w+) = "(\w+)", "([\w-]+)", "([\w-]+)"\)',
                expression,
            ):
                var, value, then, other = chosen.groups()
                out[alias] = then if bound[var] == value else other
            else:
                # a parent's kind or an adoption flag, which the paths
                # carry and this evaluation does not model
                out[alias] = None
        return out


def path(subject: int, relative: int, steps: tuple) -> str:
    """The steps from the subject to the relative, as "parent/sibling"."""
    words, here, left = [], subject, list(steps)
    while left:
        for step in left:
            start, word, end = step
            if here in (start, end):
                words.append(word if here == start else BACKWARDS[word])
                here = end if here == start else start
                left.remove(step)
                break
        else:
            raise AssertionError(f"steps {steps} do not start at {subject}")
    assert here == relative
    return "/".join(words)


@pytest.fixture(scope="module")
def derived(sets) -> set[tuple[str, str]]:
    """(path, base relation) for each row the derived arms can give."""
    paths = Paths(sets)
    found = set()
    for arm in kin_arms(sets):
        if "%asserted" in arm:
            continue
        for bound, steps in paths.select(arm):
            found.add(
                (
                    path(bound["?subject"], bound["?relative"], steps),
                    bound["?base"],
                )
            )
    return found


class TestPaths:
    """Each path of steps gives the relation ADR-0014 defines for it."""

    def test_each_path_gives_its_relation(self, derived):
        assert derived == set(RELATIONS.items())

    def test_each_relation_has_gendered_terms(self, q, derived):
        assert {base for _, base in derived} <= set(gender_map(q)) | UNGENDERED


class TestJoins:
    """How the named sets join, which decides whether the engine can run
    the query in time, or at all."""

    def test_every_set_is_included(self, q, sets):
        # the engine refuses a query with a set it never includes
        assert set(sets) - set(includes(q)) == set()

    def test_sets_rename_in_their_select_line(self, q):
        # a variable a BIND renames joins without a hash index, comparing
        # every row with every row
        assert not re.search(r"BIND\(\s*\?\w+\s+AS\s+\?\w+\s*\)", q)

    def test_a_set_takes_part_in_one_join_at_most(self, q):
        # the engine fails a query in which one set starts two joins
        joined = [
            name
            for group in re.findall(r"(?:INCLUDE\s+%\w+\s+){2,}", q + " ")
            for name in includes(group)
        ]
        assert joined
        assert [n for n, c in Counter(joined).items() if c > 1] == []


class TestKindsOfParenthood:
    """ADR-0040 §4: a parent's kind is its statement's `parentage`, set only
    where a source states it; an adoptive step is labelled, and a path
    through one says so; a foster step is labelled on its own and no
    sibling or longer path takes it."""

    def test_a_parent_statement_gives_its_kind(self, sets):
        links = " ".join(sets["links"].split())
        assert "OPTIONAL { ?st pq:{P:parentage} ?parentage . }" in links
        assert '?parentage = wd:{Q:parentage_adoptive}, "adoptive"' in links
        assert '?parentage = wd:{Q:parentage_foster}, "foster"' in links

    def test_no_path_takes_a_foster_parent(self, sets):
        parents = " ".join(sets["parents"].split())
        assert includes(sets["parents"]) == ["links"]
        assert 'FILTER(?kind != "foster")' in parents
        # every path and sibling set reads `parents`, never `links`
        for name, body in sets.items():
            if name not in ("links", "parents", "kin"):
                assert "links" not in includes(body), name

    def test_a_one_step_parent_carries_its_kind(self, sets):
        arms = kin_arms(sets)
        one_step = [arm for arm in arms if "%links" in arm]
        assert len(one_step) == 2
        assert all("(?kind AS ?through)" in arm for arm in one_step)

    def test_the_terms_say_the_kind(self, main):
        flat = " ".join(main.split())
        assert 'IF(?kindOf = "foster", "foster ",' in flat
        assert 'IF(?base = "child", "adopted ", "adoptive ")' in flat
        assert 'IF(?kindOf = "adoption", " through adoption", "")' in flat

    def test_a_path_through_an_adoption_says_so(self, sets):
        paths = [arm for arm in kin_arms(sets) if "%afterParent" in arm]
        (arm,) = paths
        assert 'IF(?first || ?second, "adoption", "")' in arm
