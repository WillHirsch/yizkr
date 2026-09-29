"""Alias hygiene from mention records (ADR-0035 §8; ADR-0022 §4 as
amended).

A thin validator, not a parser. Which part of a name is which is the
record's to say (the manifest's `structure`, from extraction or from the
GEDCOM adapter's tags); this module reads nothing from an alias's wording,
word order or token shape. It keeps an entity's alias bag to forms that
add something, in their order and verbatim spelling:

* every entity: blanks, the label itself and case-insensitive duplicates
  are dropped;
* an entity whose record stands (`claims.structure_problems` finds
  nothing): an alias's parts are the record's name parts that occur in
  it, folded, as whole words (the no-addition test of ADR-0035 §5):
  - a person: an alias that gives no part names no one (a kinship term or
    a role that slipped through) and is dropped; one whose parts the
    label already gives is derivable and dropped ("Löbig, Ida" beside
    "Ida Löbig"); one that adds a part is kept ("Wastl"). Titles and
    suffixes are no name parts;
  - a place, an organisation or a family: an alias that gives no part (a
    deictic or generic reference) is dropped, and any other kept, a short
    form included, since a place is looked up by its toponym;
* an entity without a record keeps every other alias: nothing says which
  of its words are parts.

Whether a record stands is judged on the alias bag as it comes, before
this cleanup (ADR-0035 §5 before §8), and never again after it: a
dropped alias may have been the only form holding a part the record
gives, such as a suffix.

Nothing is composed and nothing is split, so a compound or a particle
surname stays whole. Idempotent: cleaning a cleaned bag changes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from yizkr.claims import (
    EntityRecord,
    FamilyStructure,
    OrganizationStructure,
    PersonStructure,
    PlaceStructure,
    _occurs,
    _words,
    structure_problems,
)


def _parts(record) -> list[tuple[str, bool]]:
    """A record's name parts, each with whether it may end inside a word
    (a family's surname does, as in "the Löbigs")."""
    if isinstance(record, PersonStructure):
        return [
            (part, False)
            for part in (
                *(name.written for name in record.given),
                *(name.written for name in record.surnames),
                *record.patronymics,
                *record.nicknames,
                *record.unsplit,
            )
        ]
    if isinstance(record, PlaceStructure):
        names = [
            part
            for name in record.names
            for part in (name.written, name.toponym)
            if part
        ]
        if record.address is not None:
            names += [
                part
                for part in (record.address.written, record.address.street)
                if part
            ]
        return [(part, False) for part in names]
    if isinstance(record, OrganizationStructure):
        return [(part, False) for part in (*record.names, *record.places)]
    if isinstance(record, FamilyStructure):
        return [(part, True) for part in record.surnames]
    return []


def _given(form: str, parts: list[tuple[str, bool]]) -> frozenset[str]:
    """The parts that occur in a form."""
    words = [_words(form)]
    return frozenset(
        part
        for part, open_end in parts
        if _occurs(part, words, open_end=open_end)
    )


def normalize_aliases(
    label: str, aliases: Iterable[str], record=None
) -> list[str]:
    """One entity's alias bag, cleaned (the module docstring says how)
    by its record, or by none for an entity without one."""
    parts = _parts(record)
    in_label = _given(label, parts)
    label_key = label.strip().lower()
    seen: set[str] = set()
    kept: list[str] = []
    for raw in aliases:
        alias = raw.strip()
        if not alias:
            continue
        key = alias.lower()
        if key == label_key or key in seen:
            continue
        if record is not None:
            given = _given(alias, parts)
            if not given:
                continue
            if isinstance(record, PersonStructure) and given <= in_label:
                continue
        seen.add(key)
        kept.append(alias)
    return kept


def _standing(entity: dict):
    """The entity's record, when it has one that stands: parsed, of the
    entity's kind, and adding nothing to its names."""
    if entity.get("structure") is None:
        return None
    try:
        parsed = EntityRecord.model_validate(entity)
    except ValidationError:
        return None
    return None if structure_problems(parsed) else parsed.structure


def normalize_entity_dict(entity: dict) -> dict:
    """`entity` with its alias bag cleaned (normalize_aliases) by the
    record it has, if that record stands."""
    return {
        **entity,
        "aliases": normalize_aliases(
            entity.get("label", "") or "",
            entity.get("aliases") or [],
            _standing(entity),
        ),
    }
