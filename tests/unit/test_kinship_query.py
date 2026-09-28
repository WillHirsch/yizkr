"""Offline contract tests for the kinship query (pytest; no instance
needed).

A row of the query reads "?relative is ?subject's ?relationship". A
kinship term a source asserts is stored with the person the term names as
the statement's subject and the ego as its value (ADR-0018): the
universe's interview records "Aunt Pünktchen" as ``Klara —relative→
Nicholas ; kinship_type = "aunt"`` (docs/EXAMPLES.md), so that statement
is Nicholas's row, with Klara as the relative (#229).
tests/integration/test_live_kinship.py checks the same rows on the
running stack.
"""

import re

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


class TestAssertedKinship:
    """The `relative` + `kinship_type` arm: the query's reading of a
    kinship term that a source asserts."""

    def test_the_named_person_is_the_relative_and_the_ego_the_subject(self, q):
        ((named, statement),) = edges(q, "p:{P:relative}")
        ((valued, ego),) = edges(q, "ps:{P:relative}")
        # one statement: its subject is the person the term names, and its
        # value is the ego
        assert valued == statement
        assert (named, ego) == ("?relative", "?subject")

    def test_the_term_is_that_statements_kinship_type(self, q):
        ((_, statement),) = edges(q, "p:{P:relative}")
        ((qualified, term),) = edges(q, "pq:{P:kinship_type}")
        assert qualified == statement
        assert f'BIND(COALESCE({term}, "relative") AS ?base)' in q

    def test_a_term_is_gendered_by_the_person_it_names(self, q):
        assert edges(q, "wdt:{P:sex_or_gender}") == [("?relative", "?sex")]
