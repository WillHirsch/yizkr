"""An offline mirror of how the query service's munger reads a dump
(wikidata-query-rdf's EntityMungingRdfHandler and Munger), for the tests
of the non-privileged export (#412): `misplaced` names every line the
munger would drop as of a subject it does not recognise. Not a test
module: pytest collects nothing here.

The munger reads N-Triples entity by entity, in blocks. A line whose
subject is an entity's data node (``Special:EntityData/Q1``) starts a
new block when a line of any other subject came since the last block
began, and so does a redirect's ``owl:sameAs``; a line of the dump's
header (``wikibase:Dump``) belongs to no block; every other line belongs
to the block it falls in. Within a block, whatever the order of its
lines:

* the entity is the one its data node is ``schema:about`` (or the
  redirect's subject); its data nodes, the entity, a sitelink (a subject
  typed ``schema:Article``), a property's own nodes (``/prop/...``) and a
  blank node are recognised;
* a statement node is recognised when an entity's ``p:`` line names it;
* a reference node, when a statement node's ``prov:wasDerivedFrom``
  names it (whether or not that statement node is recognised);
* a value node, when a recognised statement node's line names it, a
  recognised reference node's ``prv:`` line names it, or any line's
  ``wikibase:quantityNormalized`` does.

Anything else is unrecognised, and the munger drops it: a statement,
value or reference node no line of its block registers, a line of
another entity, or a line before the first block."""

from __future__ import annotations

import gzip
import re
from pathlib import Path

DUMP_HEADER = "<http://wikiba.se/ontology#Dump>"
ENTITY_DATA = "/wiki/Special:EntityData/"
ABOUT = "<http://schema.org/about>"
SAME_AS = "<http://www.w3.org/2002/07/owl#sameAs>"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
ARTICLE = "<http://schema.org/Article>"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
NORMALIZED = "<http://wikiba.se/ontology#quantityNormalized>"


def _triple(line: str) -> tuple[str, str, str]:
    subject, predicate, rest = line.split(" ", 2)
    return subject, predicate, rest.removesuffix(" .")


_NODE = re.compile(r"<[^>/]*://[^/>]+/(entity/statement|reference|value)/")
_KINDS = {
    "entity/statement": "statement",
    "reference": "reference",
    "value": "value",
}


def _kind(term: str) -> str | None:
    """ "statement", "reference" or "value" for a node of that namespace:
    the namespace that directly follows the host, as the munger's
    ``startsWith`` tests read it, so a property's own predicate IRIs
    (``/prop/statement/value/P1``, ``/prop/reference/P1``) are none."""
    match = _NODE.match(term)
    return _KINDS[match.group(1)] if match else None


def _entity(term: str) -> bool:
    if not term.startswith("<") or "/entity/" not in term:
        return False
    local = term.rstrip(">").rsplit("/entity/", 1)[1]
    return "/" not in local and local[:1] in "QPL" and local[1:].isdigit()


def blocks(lines: list[str]) -> list[list[str]]:
    """`lines` split into the munger's blocks, the header's lines left
    out; a first "block" of lines that come before any data node is
    returned as it is."""
    found: list[list[str]] = [[]]
    open_ = False
    for line in lines:
        subject, predicate, _ = _triple(line)
        if subject == DUMP_HEADER:
            continue
        data = ENTITY_DATA in subject
        if data or (predicate == SAME_AS and _entity(subject)):
            if open_:
                found.append([])
            open_ = not data
        else:
            open_ = True
        found[-1].append(line)
    return found


def _registered(block: list[str]) -> set[str]:
    """The statement, value and reference nodes `block` registers."""
    triples = [_triple(line) for line in block]
    entities = {
        value
        for subject, predicate, value in triples
        if ENTITY_DATA in subject and predicate == ABOUT
    } | {
        subject
        for subject, predicate, _ in triples
        if predicate == SAME_AS and _entity(subject)
    }
    registered: set[str] = set()
    grown = True
    while grown:
        before = len(registered)
        for subject, predicate, value in triples:
            kind = _kind(subject)
            if subject in entities and _kind(value) == "statement":
                if "/prop/" in predicate:
                    registered.add(value)
            elif kind == "statement":
                if (
                    predicate == DERIVED_FROM
                    and _kind(value) == "reference"
                    or subject in registered
                    and _kind(value) == "value"
                ):
                    registered.add(value)
            elif (
                kind == "reference"
                and subject in registered
                and "/prop/reference/value" in predicate
            ):
                registered.add(value)
            if predicate == NORMALIZED and _kind(value) == "value":
                registered.add(value)
        grown = len(registered) > before
    return registered


def misplaced(lines: list[str]) -> list[str]:
    """Each line of `lines` the munger would drop as unrecognised, with
    the entity of the block it falls in: [] when it would keep them all."""
    found = []
    for number, block in enumerate(blocks(lines)):
        triples = [_triple(line) for line in block]
        about = {
            value
            for subject, predicate, value in triples
            if ENTITY_DATA in subject and predicate == ABOUT
        } | {
            subject
            for subject, predicate, _ in triples
            if predicate == SAME_AS and _entity(subject)
        }
        sitelinks = {
            subject
            for subject, predicate, value in triples
            if predicate == RDF_TYPE and value == ARTICLE
        }
        registered = _registered(block)
        where = ", ".join(sorted(about)) or f"block {number}, of no entity"
        for line, (subject, _, _) in zip(block, triples, strict=True):
            if not about:
                found.append(f"{where}: {line}")
            elif (
                ENTITY_DATA in subject
                or subject in about
                or subject in sitelinks
                or subject.startswith("_:")
            ):
                continue
            elif _kind(subject) is not None:
                if subject not in registered:
                    found.append(f"{where}: {line}")
            elif _entity(subject):
                found.append(f"{where}: {line}")
            elif "/prop/" in subject:
                continue
            else:
                found.append(f"{where}: {line}")
    return found


def misplaced_in(dump: Path) -> list[str]:
    """`misplaced` over a gzipped N-Triples dump."""
    with gzip.open(dump, "rt", encoding="utf-8", newline="\n") as handle:
        return misplaced(handle.read().split("\n")[:-1])
