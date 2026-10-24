"""Alias hygiene from mention records (ADR-0035 §8; ADR-0022 §4 as
amended).

A thin validator, not a parser. Which part of a name is which is the
record's to say (the manifest's `structure`, from extraction or from the
GEDCOM adapter's tags); this module reads nothing from an alias's wording,
word order or token shape. It keeps an entity's alias bag to forms that
add something, in their order and verbatim spelling:

* every entity: blanks, the label itself and case-insensitive duplicates
  are dropped, each compared in the form the instance keeps a term in
  (`stored_term` in yizkr.pipeline.writer: each run of control
  characters and line or paragraph separators made one space, trimmed,
  cleaned up as Wikibase cleans a term up, then in Unicode NFC), so an
  alias that differs from the label or another alias only in that form,
  such as NFD against NFC or a line break against a space, is the same
  alias;
* an entity whose record stands (`claims.structure_problems` finds
  nothing): an alias's parts are the record's name parts that occur in
  it, folded, as whole words (the no-addition test of ADR-0035 §5), a
  place's written name that is its label and one of its containers read
  as the label:
  - a person: an alias that gives no part names no one (a kinship term or
    a role that slipped through) and is dropped, unless the document
    gives it as an appellation (below); one whose parts the
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
  of its words are parts;
* a person's appellation, a form the document gives as one by a
  `referred_to_as` claim about the person (ADR-0018), is kept whatever
  the record says, so long as it holds a word that is no name part of the
  record: a kinship term alone ("Omama"), one with a name ("Tante Rahel",
  beside the label "Rahel Löbig") and a courtesy name ("Nana Maisie")
  stay. One made only of the record's name parts is judged by the rules
  above, so a derivable reordering stays dropped however it is marked.
  An appellation the alias bag lacks is added to it first, unless the
  entity is a place, an organisation or a family. Their marks count for
  nothing here: they are never persons.

**The appellation label.** A person whose label is one of the document's
appellations for them, and whose record stands with the status `name`,
takes the name the record gives as its label (`appellation_label`).
Every form, the label and each alias, competes: one made only of the
record's name parts as itself, any other with its longest run of
name-part words, never joined across a word that is none ("Rahel Löbig"
of "Frau Rahel Löbig geb. Tarle") and never holding an unbalanced
bracket or quote ("Ida Sörne" of "Frau Ida Sörne (Löbig)"). One that is
no appellation wins, then the one of most words, a whole form before a
run, most letters, least punctuation. The appellation stays an alias.
The rule reads the `referred_to_as` claims only, never a word the record
merely leaves out, so a label that holds a name part its record omits is
left alone, and a label beside no form that gives a run of name-part
words stays as it is.

Whether a record stands is judged on the alias bag as it comes, before
this cleanup (ADR-0035 §5 before §8), and never again after it: a
dropped alias may have been the only form holding a part the record
gives that no rule above keeps, such as an organisation's kind.

Nothing is composed and nothing is split, so a compound or a particle
surname stays whole. Idempotent: cleaning a cleaned bag changes nothing,
and settling a settled entity (normalize_entity_dict) changes nothing,
so long as its record stands, or was set aside, as the manifest parse
leaves it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from pydantic import ValidationError

from yizkr.claims import (
    EntityRecord,
    FamilyStructure,
    MonolingualValue,
    OrganizationStructure,
    PersonStructure,
    PlaceStructure,
    _occurs,
    _words,
    label_container,
    structure_problems,
)

# the punctuation a spelled word is taken without, at its edges
_EDGES = ".,;:()\"'[]"
# the property whose claims say a form is an appellation (ADR-0018)
APPELLATION = "referred_to_as"
# the kinds that are never a person: their marked forms are no
# appellations of theirs
NOT_PERSONS = frozenset({"place", "organization", "family"})


def _parts(record, label: str | None = None) -> list[tuple[str, bool]]:
    """A record's name parts, each with whether it may end inside a word
    (a family's surname does, as in "the Löbigs"). A place's written name
    that is the entity's `label` and one of its containers
    (`label_container`) gives the label as its part, as the no-addition
    check reads it."""
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
            for part in (
                label
                if label is not None
                and label_container(name.written, label, record.within)
                else name.written,
                name.toponym,
            )
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
    label: str,
    aliases: Iterable[str],
    record=None,
    appellations: Iterable[str] = (),
) -> list[str]:
    """One entity's alias bag, cleaned (the module docstring says how)
    by its record, or by none for an entity without one; `appellations`
    are the forms the document gives as appellations of the entity."""
    from yizkr.pipeline.writer import stored_term

    parts = _parts(record, label)
    marked = {stored_term(form).lower() for form in appellations}
    name_words = _name_words(parts)
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
            # a person's appellation holding a word that is no name part
            keep = keep or (
                isinstance(record, PersonStructure)
                and key in marked
                and not _only_names(alias, name_words)
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


def _name_words(parts: list[tuple[str, bool]]) -> frozenset[str]:
    """Every word of a record's name parts, folded."""
    return frozenset(word for part, _open in parts for word in _words(part))


def _only_names(form: str, name_words: frozenset[str]) -> bool:
    """Whether a form has words, and every one is a word of a name part."""
    words = _words(form)
    return bool(words) and set(words) <= name_words


def appellations_of(claims: Iterable) -> dict[str, list[str]]:
    """The forms a document gives as appellations, by the local id of the
    entity they name: the text of each `referred_to_as` claim whose
    subject is a local ref, in claim order, once each. A claim is a
    `Claim` or a claim dict as the extraction parses it."""
    found: dict[str, list[str]] = {}
    for claim in claims:
        if isinstance(claim, dict):
            data = claim
        else:
            if claim.property != APPELLATION or not isinstance(
                claim.value, MonolingualValue
            ):
                continue
            data = {
                "property": claim.property,
                "subject": {
                    "kind": getattr(claim.subject, "kind", None),
                    "local_id": getattr(claim.subject, "local_id", None),
                },
                "value": {"value": {"text": claim.value.value.text}},
            }
        if data.get("property") != APPELLATION:
            continue
        subject, value = data.get("subject"), data.get("value")
        if not isinstance(subject, dict) or subject.get("kind") != "local":
            continue
        local_id = subject.get("local_id")
        inner = value.get("value") if isinstance(value, dict) else None
        text = inner.get("text") if isinstance(inner, dict) else None
        if not isinstance(local_id, str) or not isinstance(text, str):
            continue
        text = text.strip()
        forms = found.setdefault(local_id, [])
        if text and text not in forms:
            forms.append(text)
    return found


def _letters(form: str) -> int:
    """How many letters and digits a form's words hold."""
    return sum(len(word) for word in _words(form))


def _punctuation(form: str, own: frozenset[str] = frozenset()) -> int:
    """How many marks a form holds, in NFC, that are neither a word
    character nor a space, save those the record's own parts spell with
    (`own`: a hyphen in "Hans-Xxx")."""
    return sum(
        mark not in own
        for mark in re.findall(r"[^\w\s]", unicodedata.normalize("NFC", form))
    )


def _category(char: str) -> str:
    return unicodedata.category(char)


def _opens(token: str) -> bool:
    """Whether a token starts with an opening bracket or quote. A
    right single quote is no opener: it is the apostrophe of "’t"."""
    return _category(token[0]) in ("Ps", "Pi") or token[0] in '"»›'


def _closes(token: str) -> bool:
    """Whether a token ends with a closing bracket or quote."""
    return _category(token[-1]) in ("Pe", "Pi", "Pf") or token[-1] == '"'


def _trimmed(text: str) -> str:
    """`text` without the punctuation and spaces at its ends."""
    start, end = 0, len(text)
    while start < end and (
        text[start].isspace() or _category(text[start]).startswith("P")
    ):
        start += 1
    while end > start and (
        text[end - 1].isspace() or _category(text[end - 1]).startswith("P")
    ):
        end -= 1
    return text[start:end]


def _bracketed(form: str) -> bool:
    """Whether a form holds a bracket, or a quote at a word's edge."""
    return any(_category(char) in ("Ps", "Pe") for char in form) or any(
        _opens(token) or _closes(token) for token in form.split()
    )


def _name_run(form: str, name_words: frozenset[str]) -> str:
    """The longest run of consecutive words of `form` that are each made
    only of name parts, as the form writes it with the punctuation at its
    ends trimmed; the first of the longest; "" when no word is. A word
    that is no name part ("geb.", "Frau") ends a run, so no run is
    composed across it. Nor does a run hold an unbalanced bracket or
    quote: one starts afresh at a word that opens one and ends after a
    word that closes one, and a word with a bracket inside it ("Hilde(gard)")
    is no run word. An apostrophe inside a word ("O’Xxx") stays in it."""
    runs: list[list[re.Match]] = [[]]
    for token in re.finditer(r"\S+", form):
        text = token.group()
        core = _trimmed(text)
        if _opens(text) and runs[-1]:
            runs.append([])
        if (
            core
            and not any(_category(char) in ("Ps", "Pe") for char in core)
            and _only_names(core, name_words)
        ):
            runs[-1].append(token)
        elif runs[-1]:
            runs.append([])
        if _closes(text) and runs[-1]:
            runs.append([])
    longest = max(runs, key=len)
    if not longest:
        return ""
    return _trimmed(form[longest[0].start() : longest[-1].end()])


def appellation_label(
    label: str, aliases: Iterable[str], record, appellations: Iterable[str]
) -> str:
    """The label a person takes when its label is one of the document's
    appellations for them and its record (`record`, one that stands) has
    the status `name` (the module docstring says how); else `label`
    itself.

    Every form, the label and each alias, competes: one made only of the
    record's name parts and free of brackets and quotes as itself, any
    other with its longest run of name-part words (`_name_run`), which
    never holds an unbalanced bracket or quote. The candidate that is no
    appellation
    comes first (a pet name the record holds as a nickname gives way to
    a name), then the one of most words, then a whole form before a
    run, then the one of most letters in its words, then of least
    punctuation, in NFC and leaving out the marks the record's own parts
    spell with (so "Rahel Löbig" before "Löbig, Rahel", while the hyphen
    of a record's "Hans-Xxx" weighs nothing), then the first. With no
    candidate, as when the record's name is written only fused with a
    bracket ("Hilde(gard)"), the label stays."""
    from yizkr.pipeline.writer import stored_term

    if not isinstance(record, PersonStructure) or record.status != "name":
        return label
    marked = {stored_term(form).lower() for form in appellations}
    if stored_term(label).lower() not in marked:
        return label
    name_words = _name_words(_parts(record))
    forms = dict.fromkeys(
        text.strip() for text in (label, *aliases) if text.strip()
    )
    candidates: list[tuple[str, bool]] = []
    for form in forms:
        if _only_names(form, name_words) and not _bracketed(form):
            candidates.append((form, True))
        elif run := _name_run(form, name_words):
            candidates.append((run, False))
    own = frozenset(
        mark
        for part, _open in _parts(record)
        for mark in re.findall(r"[^\w\s]", unicodedata.normalize("NFC", part))
    )
    if not candidates:
        return label
    best, _whole = max(
        candidates,
        key=lambda candidate: (
            stored_term(candidate[0]).lower() not in marked,
            len(_words(candidate[0])),
            candidate[1],
            _letters(candidate[0]),
            -_punctuation(candidate[0], own),
        ),
    )
    return best


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


def _unlisted(
    label: str, aliases: list[str], appellations: list[str]
) -> list[str]:
    """The appellations neither the label nor an alias holds, compared as
    the instance keeps a term, each once, in their order."""
    from yizkr.pipeline.writer import stored_term

    held = {stored_term(form).lower() for form in (label, *aliases)}
    found: list[str] = []
    for form in appellations:
        key = stored_term(form).lower()
        if key and key not in held:
            held.add(key)
            found.append(form)
    return found


def appellation_parts(entity: dict, appellations: Iterable[str]) -> int:
    """How many name parts of a person's record that stands are, whole,
    one of the document's appellations of the person, including a pet
    name the record rightly holds as a nickname."""
    from yizkr.pipeline.writer import stored_term

    record = _standing(entity)
    if not isinstance(record, PersonStructure):
        return 0
    marked = {stored_term(form).lower() for form in appellations}
    return sum(
        stored_term(part).lower() in marked for part, _open in _parts(record)
    )


def normalize_entity_dict(
    entity: dict, appellations: Iterable[str] = ()
) -> dict:
    """`entity` with its label set by `appellation_label` and its alias
    bag cleaned (normalize_aliases) by the record it has, if that record
    stands, and the forms the document gives as its `appellations`. A
    person's appellation the bag lacks joins it (or that of an entity of
    a kind that may be a person), and a label the rule replaces stays
    among the aliases. Idempotent for an entity whose record stands, or
    was set aside, as the manifest parse leaves it."""
    appellations = list(appellations)
    record = _standing(entity)
    label = entity.get("label", "") or ""
    aliases = list(entity.get("aliases") or [])
    if entity.get("kind") not in NOT_PERSONS:
        aliases += _unlisted(label, aliases, appellations)
    named = appellation_label(label, aliases, record, appellations)
    if named == label:
        return {
            **entity,
            "aliases": normalize_aliases(label, aliases, record, appellations),
        }
    return {
        **entity,
        "label": named,
        "aliases": normalize_aliases(
            named, [label, *aliases], record, appellations
        ),
    }
