"""Alias hygiene from mention records (ADR-0035 §8; ADR-0022 §4 as
amended).

A thin validator, not a parser. Which part of a name is which is the
record's to say (the manifest's `structure`, from extraction or from the
GEDCOM adapter's tags); this module reads nothing from an alias's wording,
word order or token shape. It keeps an entity's alias bag to forms that
add something, in their order and verbatim spelling:

* every entity: blanks, the label itself and case-insensitive duplicates
  are dropped, each compared in the form the instance keeps a term in
  (`stored_term` in yizkr.pipeline.writer: trimmed, in Unicode NFC), so
  an alias that differs from the label or another alias only in that
  form, such as NFD against NFC, is the same alias;
* an entity whose record stands (`claims.structure_problems` finds
  nothing): an alias's parts are the record's name parts that occur in
  it, folded, as whole words (the no-addition test of ADR-0035 §5):
  - a person: an alias that gives no part names no one (a kinship term or
    a role that slipped through) and is dropped; one whose parts the
    label already gives is derivable and dropped ("Löbig, Ida" beside
    "Ida Löbig"), unless it spells a part otherwise than the label does
    ("Gustav Lobig" beside "Gustav Löbig", "Hans Xxx Löbig" beside
    "Hans-Xxx Löbig"), since a spelling variant is never derivable; one
    that adds a part is kept ("Wastl"). Titles and
    suffixes are no name parts, but an alias that is the only form
    holding a title or suffix the record gives is kept ("Frau Löbig"
    beside "Ida Löbig", the record titling her Frau), one that also
    gives a name part before one that gives none ("Frau Mutter"), so
    that the record names no part its forms do not hold;
  - a place, an organisation or a family: an alias that gives no part (a
    deictic or generic reference) is dropped, and any other kept, a short
    form included, since a place is looked up by its toponym;
* an entity without a record keeps every other alias: nothing says which
  of its words are parts.

Whether a record stands is judged on the alias bag as it comes, before
this cleanup (ADR-0035 §5 before §8), and never again after it: a
dropped alias may have been the only form holding a part the record
gives that no rule above keeps, such as an organisation's kind.

Nothing is composed and nothing is split, so a compound or a particle
surname stays whole. Idempotent: cleaning a cleaned bag changes nothing.
"""

from __future__ import annotations

import unicodedata
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

# the punctuation a spelled word is taken without, at its edges
_EDGES = ".,;:()\"'[]"


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


def _spelled(text: str) -> list[str]:
    """A form's words as spelled: case folded after NFC, accents kept,
    each whitespace-separated word without its edge punctuation, so a
    hyphenated name stays one word."""
    folded = unicodedata.normalize("NFC", text).casefold()
    return [word for raw in folded.split() if (word := raw.strip(_EDGES))]


def _spelled_in(part: str, form: list[str]) -> bool:
    """Whether a part occurs in a form's spelled words, as whole words."""
    words = _spelled(part)
    size = len(words)
    return bool(words) and any(
        form[start : start + size] == words
        for start in range(len(form) - size + 1)
    )


def _spelled_given(form: str, parts: list[tuple[str, bool]]) -> frozenset[str]:
    """The parts that occur in a form as it spells them."""
    words = _spelled(form)
    return frozenset(part for part, _open in parts if _spelled_in(part, words))


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
    from yizkr.pipeline.writer import stored_term

    parts = _parts(record)
    in_label = _given(label, parts)
    spelled_in_label = _spelled_given(label, parts)
    label_key = stored_term(label).lower()
    seen: set[str] = set()
    # each distinct alias, and whether the rules keep it
    found: list[tuple[str, bool]] = []
    for raw in aliases:
        alias = raw.strip()
        key = stored_term(alias).lower()
        if not key or key == label_key or key in seen:
            continue
        seen.add(key)
        keep = True
        if record is not None:
            given = _given(alias, parts)
            # derivable: every part it gives the label gives too, and as
            # the label spells it; a spelling variant is never derivable
            keep = bool(given) and not (
                isinstance(record, PersonStructure)
                and given <= in_label
                and _spelled_given(alias, parts) <= spelled_in_label
            )
        found.append((alias, keep))
    if isinstance(record, PersonStructure):
        _rescue(label, found, (*record.titles, *record.suffixes), parts)
    return [alias for alias, keep in found if keep]


def _rescue(
    label: str,
    found: list[tuple[str, bool]],
    titles: Iterable[str],
    parts: list[tuple[str, bool]],
) -> None:
    """Keep, for each title or suffix of the record that neither the label
    nor a kept alias holds, one dropped alias holding it: the first that
    also gives a name part, else the first at all. The titles and
    suffixes are taken in the order an alias first holds them."""

    def first_holder(part: str) -> int:
        return next(
            (
                n
                for n, (alias, _keep) in enumerate(found)
                if _given_in(part, [alias])
            ),
            len(found),
        )

    for part in sorted(titles, key=first_holder):
        holders = [label, *(alias for alias, keep in found if keep)]
        if _given_in(part, holders):
            continue
        dropped = [
            position
            for position, (alias, keep) in enumerate(found)
            if not keep and _given_in(part, [alias])
        ]
        named = [p for p in dropped if _given(found[p][0], parts)]
        for position in (named or dropped)[:1]:
            found[position] = (found[position][0], True)


def _given_in(part: str, forms: list[str]) -> bool:
    """Whether a part occurs in any of the forms."""
    return any(_occurs(part, [_words(form)]) for form in forms)


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
