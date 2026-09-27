#!/usr/bin/env python3
"""Offline corpus-clustering calibration for corpus-level identity (#71).

Measures, over a cohort's extraction manifests plus a GEDCOM export, how
the corpus-level identity decision (ADR-0027) would behave BEFORE it is
built: how many candidate pairs each blocking strategy produces, what the
within-manifest cannot-link constraints and the lifted kin edges prune,
what transitive closure does with the rest, and how many adjudication
calls and owner review rows follow. No LLM calls, no graph access, no
writes except to ``--out``.

Inputs (all paths are arguments; nothing is read from the repo):

* ``--runs DIR ...``: run directories, each with a ``batch.json``
  (ClaimBatch with an entity manifest, ADR-0007) and optionally a
  ``state.json`` whose ``content_sha1`` names the artifact. Every entity
  of every manifest is one MENTION, addressed as ``<sha1>:<local id>``.
* ``--gedcom FILE``: a GEDCOM export, adapted with the repo's adapter.
  Its individuals and places are mentions too, and the anchors of the
  GEDCOM-anchored strategy. A run whose artifact is this same file is
  skipped (the current adapter's output replaces the archived batch).
* ``--verdict-runs DIR ...`` (optional): run directories of an earlier
  per-artifact pass, each with ``batch.json`` and ``equivalences.csv``.
  Their adjudicator verdicts (unreviewed machine proposals, a proxy and
  never ground truth) measure what the stronger keys would lose.

Blocking strategies (every pair is same-kind):

* ``one-token``: the current rule, reconcile.py's shared name token
  (prefix-aware), over label + aliases, for every kind.
* ``name-key``: kind-specific keys. People: a given-name token plus a
  surname token, over the label, aliases, typed birth/married/official
  names and nicknames, where the surname is any later token of the same
  name form or a surname of the mention's lifted kin (given names
  prefix-aware, accents folded); the weaker variants are measured too.
  Places: a token (four or more characters) of the name before the
  first comma; the whole first component is measured too.
  Organizations: one name's distinctive tokens contained in the
  other's. Events: same kind with a blocked pair among their
  participants. Other kinds: the whole name.
* ``gedcom-anchored``: ``name-key`` plus keyless people (one name token,
  no kin) against GEDCOM people of a compatible given name, except that
  a mention with a GEDCOM match pairs only with those GEDCOM anchors,
  never with other mentions (closure joins mentions sharing an anchor).

Constraints: two distinct ids in one manifest are cannot-link (soft; the
manifest already unified what it could), except GEDCOM places, which the
adapter mints per place string. Kin edges between local ids are lifted
into mention space so ``impossible_pair`` can fire (hard); place
containment between local ids is lifted the same way.

Classification is a deterministic PROXY for the adjudicator, used only
to size the review: a person pair is ``confident`` when the names match
exactly or by key AND a kin name or a date agrees AND no date conflicts;
a place pair when one name's comma-separated parts contain the other's;
other kinds on an exact name; anything else blocked is ``ambiguous``. A
date conflict never prunes a pair (ADR-0017: contradictory dates are a
healthy coexisting state). Closure runs over all candidate pairs and,
separately, over confident pairs only; a cluster joining two
cannot-link mentions is ``conflicting``.

Outputs under ``--out`` (chmod 700/600; refuses a path inside the repo):
``report.md`` and ``summary.json`` hold aggregates only, with examples
as ``<sha1 prefix>:<local id>``; the CSVs hold labels and stay private.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from yizkr.claims import ClaimBatch, ItemValue, LocalRef, TimeValue
from yizkr.pipeline.reconcile import (
    ADJUDICATION_BATCH_PAIRS,
    ADJUDICATION_PROMPT,
    KINSHIP_PROPERTIES,
    PLACE_KIND,
    PlaceContainment,
    RosterEntity,
    _batch_candidates,
    _batch_pair_facts,
    _name_tokens,
    _PairFacts,
    _tokens_overlap,
    block_candidates,
    entity_facts,
    impossible_pair,
)
from yizkr.pipeline.sequencing import sequence_documents

HUMAN = "human"
# kinds whose identity is anchored by who took part, not by a name
EVENT_KINDS = frozenset(
    {
        "birth",
        "death",
        "marriage",
        "migration",
        "deportation",
        "imprisonment",
        "release",
        "military_service",
        "event",
        "legal_instrument",
    }
)
ORGANIZATION = "organization"
# (relation of the value seen from the subject, of the subject seen from
# the value) for each irreflexive kinship property reconcile.py reads
RELATION = {
    "father": ("parent", "child"),
    "mother": ("parent", "child"),
    "child": ("child", "parent"),
    "spouse": ("spouse", "spouse"),
    "sibling": ("sibling", "sibling"),
}
TYPED_NAME_PROPERTIES = ("birth_name", "married_name", "official_name")
CONTAINMENT_PROPERTIES = ("part_of", "location")
_STRIP = ".,;:()\"'[]"
# titles and kin words that are not given names ("Frau", "Tante")
NOT_GIVEN = frozenset(
    {
        "mr", "mrs", "ms", "miss", "mister", "dr", "doctor", "prof",
        "professor", "herr", "frau", "fraulein", "rabbi", "rev",
        "reverend", "sir", "lady", "lord", "mme", "monsieur",
        "mlle", "dame", "uncle", "aunt", "auntie", "tante", "onkel", "oma",
        "opa", "grandpa", "grandmother", "grandfather", "great",
        "mother", "father", "mutter", "vater", "brother", "sister",
        "cousin", "sen", "jun", "senior", "junior", "the", "and", "von",
        "van", "der", "den", "del", "della", "les", "ben", "bat", "bar",
        "ibn", "zur", "auf", "verw", "widow", "witwe",
    }
)  # fmt: skip
# a name form "X née Y" carries a second surname
MAIDEN_MARKERS = frozenset({"nee", "geb", "geborene", "born"})
ORG_GENERIC = frozenset(
    {
        "the", "and", "und", "der", "die", "das", "des", "von", "for",
        "company", "firm", "firma", "gmbh", "inc", "ltd", "limited",
        "co", "cie", "bros", "brothers", "sons", "sohn", "sohne",
    }
)  # fmt: skip
# chars per token for prompt-size estimates (names and German text
# tokenize denser than English prose; a rough, stated assumption)
CHARS_PER_TOKEN = 3.5
DEFAULT_CALLS_PER_WINDOW = 50
EXAMPLES = 3

Pair = tuple[str, str]


# -- mentions -----------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """Years a stated date covers, at the source's own precision (a decade
    is ten years, never its first; rule 5)."""

    start: int
    end: int

    def overlaps(self, other: Interval) -> bool:
        return self.start <= other.end and other.start <= self.end


@dataclass
class Manifest:
    sha1: str
    batch: ClaimBatch
    source: str  # "document" | "gedcom"


@dataclass
class Mention:
    uid: str
    sha1: str
    local_id: str
    kind: str
    label: str
    aliases: list[str]
    extract: bool
    source: str
    facts: str = ""
    typed_names: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    kin: set[tuple[str, str]] = field(default_factory=set)
    participants: set[str] = field(default_factory=set)
    containers: set[str] = field(default_factory=set)
    births: list[Interval] = field(default_factory=list)
    deaths: list[Interval] = field(default_factory=list)
    when: list[Interval] = field(default_factory=list)
    # surnames of the mention's lifted kin (same manifest)
    kin_surnames: set[str] = field(default_factory=set)
    # position in its manifest (1-based)
    ordinal: int = 0
    # derived keys, computed once the mention is complete (blocking and
    # evidence read them for every pair)
    cache: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def ref(self) -> str:
        """The public, pseudonymous address: SHA-1 prefix + local id. A
        GEDCOM-adapted id derives from the export's xref, which can carry
        a genealogy service's record number, so it is replaced by the
        entity's position in the adapted manifest."""
        if self.source == "gedcom":
            return f"{self.sha1[:8]}:ged{self.ordinal}"
        return f"{self.sha1[:8]}:{self.local_id}"

    @property
    def names(self) -> list[str]:
        return [self.label, *self.aliases, *self.typed_names]

    @property
    def kin_uids(self) -> set[str]:
        return {uid for _, uid in self.kin}


def mention_uid(sha1: str, local_id: str) -> str:
    return f"{sha1}:{local_id}"


def pair_key(a: str, b: str) -> Pair:
    return (a, b) if a < b else (b, a)


def time_interval(value: TimeValue, qualifiers: Iterable = ()) -> Interval:
    """A time value's covered years, widened (never narrowed) by the
    claim's own uncertainty qualifiers."""
    span = {"century": 99, "decade": 9}.get(value.precision or "", 0)
    start, end = value.year, value.year + span
    for qualifier in qualifiers:
        if qualifier.property == "sourcing_circumstances":
            start, end = start - 5, end + 5
        elif qualifier.property == "earliest_date" and isinstance(
            qualifier.value, TimeValue
        ):
            start = min(start, qualifier.value.year)
        elif qualifier.property == "latest_date" and isinstance(
            qualifier.value, TimeValue
        ):
            end = max(end, qualifier.value.year)
    return Interval(start, end)


def _local_id(ref) -> str | None:
    return ref.local_id if isinstance(ref, LocalRef) else None


def mentions_from_manifest(manifest: Manifest) -> list[Mention]:
    """One mention per manifest entity, with the manifest's local-id
    relations LIFTED into mention space: kin edges, event participants,
    place containment, typed names and dates."""
    batch = manifest.batch
    by_id: dict[str, Mention] = {}
    for ordinal, entity in enumerate(batch.entities, start=1):
        by_id[entity.id] = Mention(
            uid=mention_uid(manifest.sha1, entity.id),
            sha1=manifest.sha1,
            local_id=entity.id,
            kind=entity.kind,
            label=entity.label,
            aliases=list(entity.aliases),
            extract=entity.extract,
            source=manifest.source,
            facts=entity_facts(batch, entity.id),
            ordinal=ordinal,
        )
    for claim in batch.claims:
        subject = by_id.get(_local_id(claim.subject) or "")
        if subject is None:
            continue
        value = claim.value
        target = (
            by_id.get(_local_id(value.value) or "")
            if isinstance(value, ItemValue)
            else None
        )
        prop = claim.property
        if prop in KINSHIP_PROPERTIES and target and target is not subject:
            forward, backward = RELATION[prop]
            subject.kin.add((forward, target.uid))
            target.kin.add((backward, subject.uid))
        elif prop == "participant" and target:
            if target.kind == HUMAN:
                subject.participants.add(target.uid)
            elif subject.kind == HUMAN and target.kind in EVENT_KINDS:
                target.participants.add(subject.uid)
        elif (
            prop in CONTAINMENT_PROPERTIES
            and target
            and subject.kind == PLACE_KIND
            and target.kind == PLACE_KIND
            and target is not subject
        ):
            subject.containers.add(target.uid)
        elif prop in TYPED_NAME_PROPERTIES and value.type == "monolingual":
            subject.typed_names.append(value.value.text)
        elif prop == "nickname" and value.type == "monolingual":
            subject.nicknames.append(value.value.text)
        elif isinstance(value, TimeValue):
            interval = time_interval(value, claim.qualifiers)
            if prop == "date_of_birth":
                subject.births.append(interval)
            elif prop == "date_of_death":
                subject.deaths.append(interval)
            elif prop in ("point_in_time", "start_time"):
                subject.when.append(interval)
    by_uid = {mention.uid: mention for mention in by_id.values()}
    for mention in by_id.values():
        # a birth/death EVENT's date is its participant's birth/death date
        if mention.kind in ("birth", "death"):
            for uid in mention.participants:
                person = by_uid[uid]
                dates = (
                    person.births if mention.kind == "birth" else person.deaths
                )
                dates.extend(mention.when)
        if mention.kind == HUMAN:
            for _, uid in mention.kin:
                mention.kin_surnames |= surnames(by_uid[uid])
    return list(by_id.values())


# -- loading ------------------------------------------------------------------


def load_run(path: Path) -> Manifest:
    """A run directory -> its manifest, named by the artifact's SHA-1
    (``state.json``), else by the batch file's own SHA-1."""
    raw = (path / "batch.json").read_bytes()
    sha1 = ""
    state_path = path / "state.json"
    if state_path.exists():
        sha1 = json.loads(state_path.read_text()).get("content_sha1") or ""
    if not sha1:
        sha1 = hashlib.sha1(raw).hexdigest()
    return Manifest(sha1, ClaimBatch.model_validate_json(raw), "document")


def load_gedcom(path: Path) -> Manifest:
    from yizkr.pipeline.gedcom import adapt_gedcom

    raw = path.read_bytes()
    result = adapt_gedcom(raw.decode("utf-8", errors="replace"), "Q1")
    return Manifest(hashlib.sha1(raw).hexdigest(), result.batch, "gedcom")


# -- keys ---------------------------------------------------------------------


def fold(text: str) -> str:
    """Case- and accent-insensitive form (``Zoë`` == ``zoe``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("ß", "ss")


def _folded_tokens(name: str) -> list[str]:
    return [
        token for raw in name.split() if (token := fold(raw.strip(_STRIP)))
    ]


def name_forms(name: str) -> list[list[str]]:
    """Significant tokens of one name, as one form, or two when the name
    embeds a maiden surname ("X Y née Z" -> [X, Y] and [X, Z])."""
    tokens = _folded_tokens(name)
    marker = next(
        (i for i, token in enumerate(tokens) if token in MAIDEN_MARKERS),
        None,
    )
    parts = (
        [tokens[:marker], tokens[:marker][:-1] + tokens[marker + 1 :][-1:]]
        if marker is not None
        else [tokens]
    )
    forms = []
    for part in parts:
        significant = [
            token
            for token in part
            if len(token) >= 3
            and token not in NOT_GIVEN
            and not any(c.isdigit() for c in token)
        ]
        if significant:
            forms.append(significant)
    return forms


def _cached(mention: Mention, name: str, compute):
    if name not in mention.cache:
        mention.cache[name] = compute(mention)
    return mention.cache[name]


# Person-key variants, weakest first; the last is the recommended one.
HUMAN_KEY_VARIANTS = ("final-surname", "any-later-token", "kin-surnames")
DEFAULT_HUMAN_KEY = HUMAN_KEY_VARIANTS[-1]


def surnames(mention: Mention) -> set[str]:
    """Final tokens of the mention's multi-token name forms."""
    return {
        form[-1]
        for name in mention.names
        for form in name_forms(name)
        if len(form) >= 2
    }


def human_keys(
    mention: Mention, variant: str = DEFAULT_HUMAN_KEY
) -> set[tuple[str, str]]:
    return _cached(
        mention, f"human:{variant}", lambda m: _human_keys(m, variant)
    )


def _human_keys(mention: Mention, variant: str) -> set[tuple[str, str]]:
    """(given, surname) keys over every name form.

    ``final-surname``: each non-final token is a given-name candidate
    (families call people by a middle name), the final token the surname.
    ``any-later-token``: any later token may be the surname, so a maiden
    name kept in the middle ("A B C", née B) still keys on B.
    ``kin-surnames``: also the first given name with every surname of the
    mention's lifted kin (a spouse's or child's surname is often a
    married name the source omits; a parent's or sibling's the birth
    name), which also keys a one-token mention that has kin.
    Nicknames stand in for given names throughout."""
    keys: set[tuple[str, str]] = set()
    own: set[str] = set()
    firsts: set[str] = set()
    for name in mention.names:
        for form in name_forms(name):
            firsts.add(form[0])
            if len(form) < 2:
                continue
            if variant == "final-surname":
                keys.update((given, form[-1]) for given in form[:-1])
                own.add(form[-1])
                continue
            for position, given in enumerate(form[:-1]):
                for surname in form[position + 1 :]:
                    keys.add((given, surname))
                    own.add(surname)
    nicknames = {
        form[0] for nick in mention.nicknames for form in name_forms(nick)
    }
    keys.update((nick, surname) for nick in nicknames for surname in own)
    if variant == "kin-surnames":
        for surname in mention.kin_surnames:
            keys.update((given, surname) for given in firsts | nicknames)
    return keys


def given_names(mention: Mention) -> set[str]:
    """Every given-name candidate: non-final tokens, lone tokens and
    nicknames."""
    givens = {
        form[0] for nick in mention.nicknames for form in name_forms(nick)
    }
    for name in mention.names:
        for form in name_forms(name):
            givens.update(form[:-1] if len(form) >= 2 else form)
    return givens


def partial_tokens(mention: Mention) -> set[str]:
    """The lone tokens of a person named by one word only ("Ada",
    "Frau X"): under-specified for a given+surname key."""
    return {
        form[0]
        for name in mention.names
        for form in name_forms(name)
        if len(form) == 1
    }


def _given_match(first: str, second: str) -> bool:
    return first.startswith(second) or second.startswith(first)


def keys_match(
    first: set[tuple[str, str]], second: set[tuple[str, str]]
) -> bool:
    """Same surname and a prefix-compatible given name ("Theo"/"Theodore")."""
    return any(
        s1 == s2 and _given_match(g1, g2)
        for g1, s1 in first
        for g2, s2 in second
    )


def _normal(name: str) -> str:
    return " ".join(_folded_tokens(name))


# Place-key variants, stricter first; the last is the recommended one.
PLACE_KEY_VARIANTS = ("first-component", "first-component-token")
DEFAULT_PLACE_KEY = PLACE_KEY_VARIANTS[-1]


def place_keys(mention: Mention, variant: str = DEFAULT_PLACE_KEY) -> set[str]:
    """Keys from the name before the first comma ("Oakfield Camp,
    Norland" -> "oakfield camp"): the whole component, or each of its
    tokens of four or more characters (a longer or shorter form of the
    same name still shares one)."""
    return _cached(
        mention, f"place:{variant}", lambda m: _place_keys(m, variant)
    )


def _place_keys(mention: Mention, variant: str) -> set[str]:
    firsts = [name.split(",")[0] for name in mention.names]
    if variant == "first-component":
        return {key for first in firsts if (key := _normal(first))}
    return {
        token
        for first in firsts
        for token in _folded_tokens(first)
        if len(token) >= 4 and not any(c.isdigit() for c in token)
    }


def place_parts(mention: Mention) -> list[frozenset[str]]:
    return _cached(
        mention,
        "place-parts",
        lambda m: [
            parts
            for name in m.names
            if (
                parts := frozenset(
                    part
                    for piece in name.split(",")
                    if (part := _normal(piece))
                )
            )
        ],
    )


def _nested_place(a: Mention, b: Mention) -> bool:
    return any(
        first <= second or second <= first
        for first in place_parts(a)
        for second in place_parts(b)
    )


def org_token_sets(mention: Mention) -> list[frozenset[str]]:
    sets = []
    for name in mention.names:
        tokens = frozenset(
            token
            for token in _folded_tokens(name)
            if len(token) >= 3 and token not in ORG_GENERIC
        )
        if tokens:
            sets.append(tokens)
    return sets


def label_keys(mention: Mention) -> set[str]:
    return _cached(
        mention,
        "label",
        lambda m: {key for name in m.names if (key := _normal(name))},
    )


def token_keys(mention: Mention) -> set[str]:
    """reconcile.py's blocking tokens (label + aliases)."""
    return _cached(
        mention, "tokens", lambda m: _name_tokens(m.label, *m.aliases)
    )


def exact_name_match(a: Mention, b: Mention) -> bool:
    return bool(label_keys(a) & label_keys(b))


# -- blocking -----------------------------------------------------------------


def _pairs_within(groups: Iterable[list[Mention]]) -> set[Pair]:
    pairs: set[Pair] = set()
    for group in groups:
        unique = list({m.uid: m for m in group}.values())
        for index, first in enumerate(unique):
            for second in unique[index + 1 :]:
                pairs.add(pair_key(first.uid, second.uid))
    return pairs


def _by_kind(mentions: list[Mention]) -> dict[str, list[Mention]]:
    grouped: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        grouped[mention.kind].append(mention)
    return grouped


def block_one_token(mentions: list[Mention]) -> set[Pair]:
    """The current rule (reconcile.py): same kind + a shared name token,
    prefix-aware, over label + aliases."""
    pairs: set[Pair] = set()
    for group in _by_kind(mentions).values():
        tokens = [token_keys(m) for m in group]
        for i, first in enumerate(group):
            for j in range(i + 1, len(group)):
                if _tokens_overlap(tokens[i], tokens[j]):
                    pairs.add(pair_key(first.uid, group[j].uid))
    return pairs


def block_humans(
    humans: list[Mention], variant: str = DEFAULT_HUMAN_KEY
) -> set[Pair]:
    by_surname: dict[str, list[tuple[Mention, set]]] = defaultdict(list)
    for mention in humans:
        keys = human_keys(mention, variant)
        for surname in {s for _, s in keys}:
            by_surname[surname].append((mention, keys))
    pairs: set[Pair] = set()
    for bucket in by_surname.values():
        for index, (first, first_keys) in enumerate(bucket):
            for second, second_keys in bucket[index + 1 :]:
                if first.uid != second.uid and keys_match(
                    first_keys, second_keys
                ):
                    pairs.add(pair_key(first.uid, second.uid))
    return pairs


def _block_by_keys(mentions: list[Mention], key_function) -> set[Pair]:
    buckets: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        for key in key_function(mention):
            buckets[key].append(mention)
    return _pairs_within(buckets.values())


def block_organizations(orgs: list[Mention]) -> set[Pair]:
    """One name's distinctive tokens contained in the other's."""
    by_token: dict[str, list[Mention]] = defaultdict(list)
    token_sets = {m.uid: org_token_sets(m) for m in orgs}
    for mention in orgs:
        for tokens in token_sets[mention.uid]:
            for token in tokens:
                by_token[token].append(mention)
    pairs: set[Pair] = set()
    for candidate in _pairs_within(by_token.values()):
        first, second = (token_sets[uid] for uid in candidate)
        if any(a <= b or b <= a for a in first for b in second):
            pairs.add(candidate)
    return pairs


def block_events(
    events: list[Mention], human_affinity: set[Pair]
) -> set[Pair]:
    """Same kind, and a blocked pair among their participants; an event
    without participants falls back to its whole name."""
    pairs: set[Pair] = set()
    for group in _by_kind(events).values():
        by_participant: dict[str, list[Mention]] = defaultdict(list)
        for event in group:
            for uid in event.participants:
                by_participant[uid].append(event)
        for first_uid, second_uid in human_affinity:
            for first in by_participant.get(first_uid, []):
                for second in by_participant.get(second_uid, []):
                    if first.uid != second.uid:
                        pairs.add(pair_key(first.uid, second.uid))
        pairs |= _block_by_keys(
            [event for event in group if not event.participants],
            label_keys,
        )
    return pairs


def block_name_key(mentions: list[Mention]) -> set[Pair]:
    """Kind-specific keys (see the module docstring)."""
    grouped = _by_kind(mentions)
    humans = block_humans(grouped.get(HUMAN, []))
    pairs = set(humans)
    events: list[Mention] = []
    for kind, group in grouped.items():
        if kind == HUMAN:
            continue
        if kind == PLACE_KIND:
            pairs |= _block_by_keys(group, place_keys)
        elif kind == ORGANIZATION:
            pairs |= block_organizations(group)
        elif kind in EVENT_KINDS:
            events.extend(group)
        else:
            pairs |= _block_by_keys(group, label_keys)
    return pairs | block_events(events, humans)


def anchor_candidates(
    pairs: set[Pair], index: dict[str, Mention]
) -> dict[str, set[str]]:
    """Mention uid -> the GEDCOM mentions it key-matches."""
    anchors: dict[str, set[str]] = defaultdict(set)
    for first_uid, second_uid in pairs:
        first, second = index[first_uid], index[second_uid]
        if first.kind in EVENT_KINDS:
            continue
        if first.source == "gedcom" and second.source != "gedcom":
            anchors[second_uid].add(first_uid)
        elif second.source == "gedcom" and first.source != "gedcom":
            anchors[first_uid].add(second_uid)
    return dict(anchors)


def partial_anchor_pairs(mentions: list[Mention]) -> set[Pair]:
    """A person with no key at all (one name token, no kin surnames)
    pairs with the GEDCOM people whose given name is prefix-compatible:
    the tree is the bounded roster that can resolve a bare "Ada"."""
    people = [m for m in mentions if m.kind == HUMAN and m.source == "gedcom"]
    pairs: set[Pair] = set()
    for mention in mentions:
        if (
            mention.kind != HUMAN
            or mention.source == "gedcom"
            or human_keys(mention)
        ):
            continue
        tokens = partial_tokens(mention)
        for person in people:
            if any(
                _given_match(token, given)
                for token in tokens
                for given in given_names(person)
            ):
                pairs.add(pair_key(mention.uid, person.uid))
    return pairs


def block_gedcom_anchored(
    mentions: list[Mention],
    name_key_pairs: set[Pair],
    index: dict[str, Mention],
) -> tuple[set[Pair], dict[str, set[str]]]:
    """``name-key`` plus keyless people against the GEDCOM, except that
    an anchored mention pairs only with its GEDCOM anchors; unanchored
    mentions keep their mention-to-mention pairs."""
    pairs = name_key_pairs | partial_anchor_pairs(mentions)
    anchors = anchor_candidates(pairs, index)
    kept = {
        (first, second)
        for first, second in pairs
        if index[first].kind in EVENT_KINDS
        or index[first].source == "gedcom"
        or index[second].source == "gedcom"
        or (first not in anchors and second not in anchors)
    }
    return kept, anchors


# -- constraints --------------------------------------------------------------


def cannot_link(a: Mention, b: Mention) -> bool:
    """Two ids of one manifest are kept apart (soft), except GEDCOM
    places: the adapter mints one place per distinct PLAC string, so
    "Oakfield" and "Oakfield, Norland" are two ids for what may be one
    place, never a statement that they differ."""
    if a.sha1 != b.sha1:
        return False
    return not (a.source == "gedcom" and a.kind == b.kind == PLACE_KIND)


def lifted_facts(mention: Mention) -> _PairFacts:
    """impossible_pair's input with local-id kin edges lifted: the
    mention uid stands in for a QID on both sides of every edge."""
    return _PairFacts(
        kind=mention.kind,
        label=mention.label,
        qid=mention.uid,
        kin_qids=mention.kin_uids,
    )


def lifted_containment(mentions: list[Mention]) -> PlaceContainment:
    """Transitive place containment between local ids, in mention space."""
    index = {m.uid: m for m in mentions}
    ancestors: dict[str, set[str]] = {}
    for mention in mentions:
        if mention.kind != PLACE_KIND or not mention.containers:
            continue
        seen: set[str] = set()
        stack = list(mention.containers)
        while stack:
            uid = stack.pop()
            if uid in seen or uid == mention.uid:
                continue
            seen.add(uid)
            if uid in index:
                stack.extend(index[uid].containers)
        ancestors[mention.uid] = seen
    labels = {m.uid: m.label for m in mentions if m.kind == PLACE_KIND}
    return PlaceContainment(ancestors, labels)


@dataclass
class ConstraintResult:
    blocked: set[Pair]
    within_manifest: set[Pair]
    kin_edge: set[Pair]
    place_containment: set[Pair]
    unlifted_pruned: int
    remaining: set[Pair]


def apply_constraints(
    pairs: set[Pair],
    index: dict[str, Mention],
    containment: PlaceContainment,
    batches: dict[str, ClaimBatch],
) -> ConstraintResult:
    within = {p for p in pairs if cannot_link(index[p[0]], index[p[1]])}
    kin_edge: set[Pair] = set()
    place: set[Pair] = set()
    unlifted = 0
    # what impossible_pair sees WITHOUT the lift: the facts the current
    # path builds from a manifest (kin only via QidRef values)
    entities = {
        mention_uid(sha1, entity.id): (entity, batch)
        for sha1, batch in batches.items()
        for entity in batch.entities
    }
    unlifted_facts = {
        uid: _batch_pair_facts(*entities[uid])
        for uid in {uid for pair in pairs for uid in pair}
    }
    for first_uid, second_uid in pairs:
        first, second = index[first_uid], index[second_uid]
        reason = impossible_pair(
            lifted_facts(first),
            lifted_facts(second),
            containment=containment,
        )
        if reason == "kinship-edge":
            kin_edge.add((first_uid, second_uid))
        elif reason == "place-containment":
            place.add((first_uid, second_uid))
        if impossible_pair(
            unlifted_facts[first_uid], unlifted_facts[second_uid]
        ):
            unlifted += 1
    return ConstraintResult(
        blocked=pairs,
        within_manifest=within,
        # lifted kin edges join ids of one manifest: a subset of `within`
        kin_edge=kin_edge,
        # the label form of containment also reaches across manifests
        place_containment=place - within,
        unlifted_pruned=unlifted,
        remaining=pairs - within - place,
    )


# -- evidence -----------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    name: str  # exact | nested (places) | key | token | none
    dates: str  # agree | conflict | unknown
    kin: str  # agree | unknown


def _dates(first: list[Interval], second: list[Interval]) -> str:
    if not first or not second:
        return "unknown"
    if any(a.overlaps(b) for a in first for b in second):
        return "agree"
    return "conflict"


def _combine_dates(*results: str) -> str:
    if "conflict" in results:
        return "conflict"
    return "agree" if "agree" in results else "unknown"


def _kin_agree(a: Mention, b: Mention, index: dict[str, Mention]) -> bool:
    for relation, first_uid in a.kin:
        first = index.get(first_uid)
        if first is None:
            continue
        for other_relation, second_uid in b.kin:
            second = index.get(second_uid)
            if second is None or relation != other_relation:
                continue
            if exact_name_match(first, second) or keys_match(
                human_keys(first), human_keys(second)
            ):
                return True
    return False


def name_level(a: Mention, b: Mention) -> str:
    if exact_name_match(a, b):
        return "exact"
    if a.kind == HUMAN and keys_match(human_keys(a), human_keys(b)):
        return "key"
    if a.kind == PLACE_KIND and place_keys(a) & place_keys(b):
        # "Oakfield" vs "Oakfield, Norland": one name's parts inside the
        # other's is a refinement, not a rival place
        return "nested" if _nested_place(a, b) else "key"
    if _tokens_overlap(token_keys(a), token_keys(b)):
        return "token"
    return "none"


def pair_evidence(
    a: Mention, b: Mention, index: dict[str, Mention]
) -> Evidence:
    if a.kind in EVENT_KINDS:
        best = "none"
        for first_uid in a.participants:
            for second_uid in b.participants:
                if first_uid in index and second_uid in index:
                    level = name_level(index[first_uid], index[second_uid])
                    if level in ("exact", "key") and best != "exact":
                        best = level
        return Evidence(best, _dates(a.when, b.when), "unknown")
    return Evidence(
        name_level(a, b),
        _combine_dates(_dates(a.births, b.births), _dates(a.deaths, b.deaths)),
        "agree" if _kin_agree(a, b, index) else "unknown",
    )


def classify_pair(
    a: Mention, b: Mention, evidence: Evidence, index: dict[str, Mention]
) -> str:
    """The deterministic proxy: ``confident`` or ``ambiguous``."""
    if evidence.dates == "conflict":
        return "ambiguous"
    if a.kind == HUMAN:
        corroborated = evidence.kin == "agree" or evidence.dates == "agree"
        return (
            "confident"
            if evidence.name in ("exact", "key") and corroborated
            else "ambiguous"
        )
    if a.kind in EVENT_KINDS:
        for first_uid in a.participants:
            for second_uid in b.participants:
                first, second = index.get(first_uid), index.get(second_uid)
                if first is None or second is None:
                    continue
                if first.kind == second.kind == HUMAN and (
                    classify_pair(
                        first,
                        second,
                        pair_evidence(first, second, index),
                        index,
                    )
                    == "confident"
                ):
                    return "confident"
        return "ambiguous"
    if a.kind == PLACE_KIND:
        return (
            "confident"
            if evidence.name in ("exact", "nested")
            else "ambiguous"
        )
    return "confident" if evidence.name == "exact" else "ambiguous"


# -- closure ------------------------------------------------------------------


def components(uids: Iterable[str], pairs: Iterable[Pair]) -> list[list[str]]:
    """Transitive closure: connected components (singletons included)."""
    parent: dict[str, str] = {uid: uid for uid in uids}

    def find(uid: str) -> str:
        while parent[uid] != uid:
            parent[uid] = parent[parent[uid]]
            uid = parent[uid]
        return uid

    for first, second in pairs:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[max(root_first, root_second)] = min(root_first, root_second)
    grouped: dict[str, list[str]] = defaultdict(list)
    for uid in parent:
        grouped[find(uid)].append(uid)
    return sorted(
        (sorted(members) for members in grouped.values()),
        key=lambda members: (-len(members), members[0]),
    )


def conflict(
    members: list[str], index: dict[str, Mention]
) -> tuple[str | None, Pair | None]:
    """A cluster joining two ids of one manifest is conflicting: ``hard``
    when a lifted kin edge joins them, else ``soft``."""
    by_manifest: dict[str, list[Mention]] = defaultdict(list)
    for uid in members:
        by_manifest[index[uid].sha1].append(index[uid])
    soft: Pair | None = None
    for group in by_manifest.values():
        for i, first in enumerate(group):
            for second in group[i + 1 :]:
                if first.uid in second.kin_uids:
                    return "hard", pair_key(first.uid, second.uid)
                if cannot_link(first, second):
                    soft = soft or pair_key(first.uid, second.uid)
    return ("soft", soft) if soft else (None, None)


@dataclass
class ClusterRow:
    members: list[str]
    status: str  # singleton | confident | ambiguous | conflicting-hard/soft
    pairs: list[Pair]
    example: Pair | None = None


def classify_clusters(
    uids: list[str],
    pairs: set[Pair],
    pair_class: dict[Pair, str],
    index: dict[str, Mention],
) -> list[ClusterRow]:
    owner: dict[str, int] = {}
    groups = components(uids, pairs)
    for number, members in enumerate(groups):
        for uid in members:
            owner[uid] = number
    cluster_pairs: dict[int, list[Pair]] = defaultdict(list)
    for pair in pairs:
        cluster_pairs[owner[pair[0]]].append(pair)
    rows = []
    for number, members in enumerate(groups):
        own = sorted(cluster_pairs.get(number, []))
        if len(members) == 1:
            rows.append(ClusterRow(members, "singleton", own))
            continue
        kind, example = conflict(members, index)
        if kind:
            rows.append(
                ClusterRow(members, f"conflicting-{kind}", own, example)
            )
        elif any(pair_class[p] == "ambiguous" for p in own):
            rows.append(ClusterRow(members, "ambiguous", own))
        else:
            rows.append(ClusterRow(members, "confident", own))
    return rows


# -- estimates ----------------------------------------------------------------


def adjudication_chunks(
    groups: dict[str, list[Pair]], pairs_per_call: int
) -> list[dict[str, list[Pair]]]:
    """Pairs per call, keeping a cluster's pairs together (reconcile's
    ``_batch_candidates``); a cluster over the limit is split first."""
    split: dict[str, list[Pair]] = {}
    for key, pairs in groups.items():
        for start in range(0, len(pairs), pairs_per_call):
            split[f"{key}#{start}"] = pairs[start : start + pairs_per_call]
    return _batch_candidates(split, pairs_per_call)


def mention_line(mention: Mention) -> str:
    """How a mention would appear in an adjudication prompt."""
    line = f"- {mention.uid}: {mention.label}"
    if mention.aliases:
        line += f" (aliases: {', '.join(mention.aliases)})"
    line += f"; kind: {mention.kind}"
    if mention.facts:
        line += f"\n  facts: {mention.facts}"
    return line


def prompt_tokens(
    chunks: list[dict[str, list[Pair]]], index: dict[str, Mention]
) -> list[int]:
    """Estimated input tokens per call (template + both sides' facts)."""
    sizes = []
    for chunk in chunks:
        chars = len(ADJUDICATION_PROMPT)
        for pairs in chunk.values():
            for first, second in pairs:
                chars += len(mention_line(index[first]))
                chars += len(mention_line(index[second]))
        sizes.append(math.ceil(chars / CHARS_PER_TOKEN))
    return sizes


@dataclass
class SerialResult:
    order: list[str]
    rows_per_artifact: list[int]
    calls: int

    @property
    def rows(self) -> int:
        return sum(self.rows_per_artifact)


def simulate_serial(
    manifests: list[Manifest],
    pairs_per_call: int,
    merged_into: dict[str, str] | None = None,
) -> SerialResult:
    """The current per-artifact path (ADR-0019 serial order): each
    artifact's entities are blocked by ``block_candidates`` against every
    earlier artifact's extracted entities, and every blocked pair is one
    equivalences-CSV row. ``merged_into`` (mention uid -> cluster id)
    folds an approved match into its existing roster item, appending the
    new aliases, as the writer does; without it nothing ever merges."""
    by_sha = {m.sha1: m for m in manifests}
    keyed = [
        (
            m.sha1,
            set().union(
                *(
                    _name_tokens(e.label, *e.aliases)
                    for e in m.batch.entities
                    if e.extract
                )
            ),
        )
        for m in manifests
    ]
    order = [str(sha) for sha in sequence_documents(keyed)]
    roster: list[RosterEntity] = []
    by_cluster: dict[str, RosterEntity] = {}
    rows: list[int] = []
    calls = 0
    for sha1 in order:
        batch = by_sha[sha1].batch
        candidates = block_candidates(batch.entities, roster, batch=batch)
        rows.append(sum(len(found) for found in candidates.values()))
        calls += len(_batch_candidates(candidates, pairs_per_call))
        for entity in batch.entities:
            if not entity.extract:
                continue
            uid = mention_uid(sha1, entity.id)
            cluster = (merged_into or {}).get(uid)
            if cluster and cluster in by_cluster:
                existing = by_cluster[cluster]
                existing.aliases.extend([entity.label, *entity.aliases])
                continue
            item = RosterEntity(
                qid=f"Q{len(roster) + 1}",
                label=entity.label,
                aliases=list(entity.aliases),
                kind=entity.kind,
            )
            roster.append(item)
            if cluster:
                by_cluster[cluster] = item
    return SerialResult(order, rows, calls)


# -- homonyms -----------------------------------------------------------------


def homonym_groups(mentions: list[Mention]) -> list[dict]:
    """Identical names (same kind) on mentions a manifest keeps apart:
    provably distinct entities an exact-label resolve would merge."""
    by_label: dict[tuple[str, str], list[Mention]] = defaultdict(list)
    for mention in mentions:
        by_label[(mention.kind, _normal(mention.label))].append(mention)
    groups = []
    for (kind, _), group in by_label.items():
        if len(group) < 2:
            continue
        status, example = conflict(
            [m.uid for m in group], {m.uid: m for m in group}
        )
        if status:
            groups.append(
                {
                    "kind": kind,
                    "members": [m.uid for m in group],
                    "status": status,
                    "example": example,
                    "gedcom": any(m.source == "gedcom" for m in group),
                }
            )
    return groups


# -- verdict calibration ------------------------------------------------------


CONFIDENCE_BINS = (0.5, 0.7, 0.8, 0.9)


def confidence_bin(raw: str) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "none"
    lower = "0"
    for edge in CONFIDENCE_BINS:
        if value < edge:
            return f"[{lower},{edge})"
        lower = str(edge)
    return f">={CONFIDENCE_BINS[-1]}"


def verdict_calibration(
    run_dirs: list[Path], anchors: list[Mention]
) -> list[dict]:
    """Earlier per-artifact adjudicator verdicts vs the stronger keys.

    Each equivalences-CSV row pairs a document entity with a graph item
    known only by its label; the item is matched to the GEDCOM mention of
    the same name when exactly one exists (the graph then held the
    GEDCOM), else it stands as a bare name without facts. One row out per
    row in, label-bearing (private)."""
    anchor_index = {m.uid: m for m in anchors}
    by_label: dict[tuple[str, str], list[Mention]] = defaultdict(list)
    for mention in anchors:
        by_label[(mention.kind, _normal(mention.label))].append(mention)
    rows_out: list[dict] = []
    for run_dir in run_dirs:
        csv_path = run_dir / "equivalences.csv"
        if not csv_path.exists() or not (run_dir / "batch.json").exists():
            continue
        manifest = load_run(run_dir)
        mentions = {m.local_id: m for m in mentions_from_manifest(manifest)}
        index = {m.uid: m for m in mentions.values()} | anchor_index
        with open(csv_path, newline="") as handle:
            for row in csv.DictReader(handle):
                doc = mentions.get(row["entity_id"])
                if doc is None:
                    continue
                found = by_label.get((doc.kind, _normal(row["qid_label"])), [])
                mapped = len(found) == 1
                other = (
                    found[0]
                    if mapped
                    else Mention(
                        uid=f"graph:{row['qid']}",
                        sha1="graph",
                        local_id=row["qid"],
                        kind=doc.kind,
                        label=row["qid_label"],
                        aliases=[],
                        extract=True,
                        source="graph",
                    )
                )
                index[other.uid] = other
                name_key = bool(block_name_key([doc, other]))
                variants = {
                    variant: doc.kind == HUMAN
                    and keys_match(
                        human_keys(doc, variant), human_keys(other, variant)
                    )
                    for variant in HUMAN_KEY_VARIANTS
                } | {
                    variant: doc.kind == PLACE_KIND
                    and bool(
                        place_keys(doc, variant) & place_keys(other, variant)
                    )
                    for variant in PLACE_KEY_VARIANTS
                }
                anchored = name_key or bool(
                    other.source == "gedcom"
                    and partial_anchor_pairs([doc, other])
                )
                evidence = pair_evidence(doc, other, index)
                rows_out.append(
                    {
                        "artifact": manifest.sha1[:8],
                        "entity_id": doc.local_id,
                        "entity_label": doc.label,
                        "qid": row["qid"],
                        "qid_label": row["qid_label"],
                        "kind": doc.kind,
                        "verdict": (row.get("verdict") or "").strip().lower(),
                        "confidence": confidence_bin(row.get("confidence")),
                        "mapped_to_gedcom": mapped,
                        "one_token": bool(block_one_token([doc, other])),
                        "name_key": name_key,
                        "gedcom_anchored": anchored,
                        **{f"key:{v}": hit for v, hit in variants.items()},
                        "evidence": f"{evidence.name}/{evidence.dates}/"
                        f"{evidence.kin}",
                        "proxy_class": classify_pair(
                            doc, other, evidence, index
                        ),
                    }
                )
    return rows_out


def summarize_verdicts(rows: list[dict]) -> dict:
    """Aggregates of the calibration rows: per kind and verdict, what each
    rule keeps; person-key variants; adjudicator confidence."""
    per_kind: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        cell = per_kind.setdefault(row["kind"], {}).setdefault(
            row["verdict"],
            {
                "rows": 0,
                "name_key": 0,
                "gedcom_anchored": 0,
                "proxy_confident": 0,
                "mapped": 0,
            },
        )
        cell["rows"] += 1
        cell["name_key"] += row["name_key"]
        cell["gedcom_anchored"] += row["gedcom_anchored"]
        cell["proxy_confident"] += row["proxy_class"] == "confident"
        cell["mapped"] += row["mapped_to_gedcom"]
    people = [row for row in rows if row["kind"] == HUMAN]
    variants = {
        variant: dict(
            sorted(
                Counter(
                    row["verdict"] for row in people if row[f"key:{variant}"]
                ).items()
            )
        )
        for variant in HUMAN_KEY_VARIANTS
    }
    variants["kin-surnames + keyless vs GEDCOM"] = dict(
        sorted(
            Counter(
                row["verdict"] for row in people if row["gedcom_anchored"]
            ).items()
        )
    )
    places = [row for row in rows if row["kind"] == PLACE_KIND]
    place_variants = {
        variant: dict(
            sorted(
                Counter(
                    row["verdict"] for row in places if row[f"key:{variant}"]
                ).items()
            )
        )
        for variant in PLACE_KEY_VARIANTS
    }
    confidence = {
        verdict: dict(
            sorted(
                Counter(
                    row["confidence"]
                    for row in rows
                    if row["verdict"] == verdict
                ).items()
            )
        )
        for verdict in sorted({row["verdict"] for row in rows})
    }
    confidence_kept = {
        verdict: dict(
            sorted(
                Counter(
                    row["confidence"]
                    for row in rows
                    if row["verdict"] == verdict and row["gedcom_anchored"]
                ).items()
            )
        )
        for verdict in sorted({row["verdict"] for row in rows})
    }
    proxy_vs_verdict = dict(
        sorted(
            Counter(
                f"{row['proxy_class']}/{row['verdict']}"
                for row in rows
                if row["gedcom_anchored"]
            ).items()
        )
    )
    return {
        "rows": len(rows),
        "one_token_reproduced": sum(row["one_token"] for row in rows),
        "people_by_verdict": dict(
            sorted(Counter(row["verdict"] for row in people).items())
        ),
        "per_kind": dict(sorted(per_kind.items())),
        "human_key_variants": variants,
        "places_by_verdict": dict(
            sorted(Counter(row["verdict"] for row in places).items())
        ),
        "place_key_variants": place_variants,
        "confidence_by_verdict": confidence,
        "confidence_by_verdict_on_anchored": confidence_kept,
        "proxy_vs_verdict_on_anchored": proxy_vs_verdict,
    }


# -- experiment ---------------------------------------------------------------


@dataclass
class StrategyResult:
    name: str
    constraints: ConstraintResult
    pair_class: dict[Pair, str]
    evidence: dict[Pair, Evidence]
    clusters: list[ClusterRow]
    anchors: dict[str, set[str]] = field(default_factory=dict)
    # closure over confident links only: what accepting them unreviewed
    # would build
    confident_clusters: list[ClusterRow] = field(default_factory=list)


def run_strategy(
    name: str,
    pairs: set[Pair],
    mentions: list[Mention],
    index: dict[str, Mention],
    containment: PlaceContainment,
    batches: dict[str, ClaimBatch],
    anchors: dict[str, set[str]] | None = None,
) -> StrategyResult:
    constraints = apply_constraints(pairs, index, containment, batches)
    evidence = {
        pair: pair_evidence(index[pair[0]], index[pair[1]], index)
        for pair in constraints.remaining
    }
    pair_class = {
        pair: classify_pair(index[pair[0]], index[pair[1]], ev, index)
        for pair, ev in evidence.items()
    }
    uids = [m.uid for m in mentions]
    clusters = classify_clusters(
        uids, constraints.remaining, pair_class, index
    )
    confident = {p for p, klass in pair_class.items() if klass == "confident"}
    return StrategyResult(
        name,
        constraints,
        pair_class,
        evidence,
        clusters,
        anchors or {},
        classify_clusters(uids, confident, pair_class, index),
    )


def _kind_counts(pairs: Iterable[Pair], index: dict[str, Mention]) -> dict:
    return dict(sorted(Counter(index[p[0]].kind for p in pairs).items()))


def strategy_summary(
    result: StrategyResult,
    index: dict[str, Mention],
    pairs_per_call: int,
    calls_per_window: int,
) -> dict:
    constraints = result.constraints
    statuses = Counter(row.status for row in result.clusters)
    multi = [row for row in result.clusters if len(row.members) > 1]
    review = [
        row
        for row in result.clusters
        if row.status == "ambiguous" or row.status.startswith("conflicting")
    ]
    all_groups = {str(n): row.pairs for n, row in enumerate(multi)}
    ambiguous_groups = {
        str(n): [p for p in row.pairs if result.pair_class[p] == "ambiguous"]
        for n, row in enumerate(multi)
    }
    ambiguous_groups = {k: v for k, v in ambiguous_groups.items() if v}
    all_chunks = adjudication_chunks(all_groups, pairs_per_call)
    ambiguous_chunks = adjudication_chunks(ambiguous_groups, pairs_per_call)
    tokens = prompt_tokens(all_chunks, index)
    examples: dict[str, list[str]] = defaultdict(list)
    for row in result.clusters:
        if (
            row.status.startswith("conflicting")
            and row.example
            and len(examples[row.status]) < EXAMPLES
        ):
            examples[row.status].append(
                " + ".join(index[uid].ref for uid in row.example)
                + f" (cluster of {len(row.members)})"
            )
    ambiguous_pairs = [
        p for p, klass in result.pair_class.items() if klass == "ambiguous"
    ]
    summary = {
        "blocked_pairs": len(constraints.blocked),
        "blocked_pairs_by_kind": _kind_counts(constraints.blocked, index),
        "within_manifest_removed": len(constraints.within_manifest),
        "kin_edge_pruned_lifted": len(constraints.kin_edge),
        "place_containment_pruned_lifted": len(constraints.place_containment),
        "impossible_pair_pruned_unlifted": constraints.unlifted_pruned,
        "candidate_pairs": len(constraints.remaining),
        "candidate_pairs_by_kind": _kind_counts(constraints.remaining, index),
        "confident_pairs": len(constraints.remaining) - len(ambiguous_pairs),
        "ambiguous_pairs": len(ambiguous_pairs),
        "ambiguous_pairs_by_kind": _kind_counts(ambiguous_pairs, index),
        "evidence_mix": dict(
            sorted(
                Counter(
                    f"{e.name}/{e.dates}/{e.kin}"
                    for e in result.evidence.values()
                ).items()
            )
        ),
        "clusters": len(multi),
        "singletons": statuses["singleton"],
        "largest_cluster": max((len(r.members) for r in multi), default=0),
        "cluster_status": dict(sorted(statuses.items())),
        "review_clusters": len(review),
        "review_mentions": sum(len(r.members) for r in review),
        "review_pairs": sum(len(r.pairs) for r in review),
        "adjudication_calls_all": len(all_chunks),
        "adjudication_calls_ambiguous_only": len(ambiguous_chunks),
        "windows_all": math.ceil(len(all_chunks) / calls_per_window),
        "windows_ambiguous_only": math.ceil(
            len(ambiguous_chunks) / calls_per_window
        ),
        "prompt_tokens_mean": round(sum(tokens) / len(tokens))
        if tokens
        else 0,
        "prompt_tokens_max": max(tokens, default=0),
        "conflict_examples": dict(examples),
        "conflicting_by_kind": dict(
            sorted(
                Counter(
                    index[row.members[0]].kind
                    for row in result.clusters
                    if row.status.startswith("conflicting")
                ).items()
            )
        ),
        "confident_closure_clusters": sum(
            len(row.members) > 1 for row in result.confident_clusters
        ),
        "confident_closure_largest": max(
            (len(row.members) for row in result.confident_clusters),
            default=0,
        ),
        "confident_closure_conflicting": sum(
            row.status.startswith("conflicting")
            for row in result.confident_clusters
        ),
        "confident_closure_conflicting_hard": sum(
            row.status == "conflicting-hard"
            for row in result.confident_clusters
        ),
    }
    if result.anchors:
        counts = Counter(
            (index[uid].kind, min(len(found), 3))
            for uid, found in result.anchors.items()
        )
        summary["anchored_mentions"] = len(result.anchors)
        summary["anchored_by_candidates"] = {
            f"{kind}, {'3+' if n == 3 else n} anchor(s)": count
            for (kind, n), count in sorted(counts.items())
        }
        summary["multi_anchor_examples"] = [
            index[uid].ref
            for uid, found in sorted(result.anchors.items())
            if len(found) > 1
        ][:EXAMPLES]
    return summary


def collapse_map(result: StrategyResult) -> dict[str, str]:
    """Mention uid -> id of its confident-only cluster (a stand-in for
    what the owner would approve in the per-artifact path)."""
    confident = {
        p for p, klass in result.pair_class.items() if klass == "confident"
    }
    uids = {uid for pair in confident for uid in pair}
    return {
        uid: members[0]
        for members in components(uids, confident)
        for uid in members
    }


def run_experiment(
    run_dirs: list[Path],
    gedcom: Path | None,
    verdict_dirs: list[Path],
    *,
    include_unextracted: bool = False,
    pairs_per_call: int = ADJUDICATION_BATCH_PAIRS,
    calls_per_window: int = DEFAULT_CALLS_PER_WINDOW,
) -> tuple[dict, dict]:
    """-> (aggregate summary, private detail)."""
    manifests: list[Manifest] = []
    gedcom_manifest = load_gedcom(gedcom) if gedcom else None
    skipped_same_as_gedcom = 0
    for run_dir in run_dirs:
        if not (run_dir / "batch.json").exists():
            continue
        manifest = load_run(run_dir)
        if gedcom_manifest and manifest.sha1 == gedcom_manifest.sha1:
            skipped_same_as_gedcom += 1
            continue
        manifests.append(manifest)
    all_manifests = manifests + ([gedcom_manifest] if gedcom_manifest else [])
    batches = {m.sha1: m.batch for m in all_manifests}
    every = [
        mention
        for manifest in all_manifests
        for mention in mentions_from_manifest(manifest)
    ]
    mentions = [m for m in every if include_unextracted or m.extract]
    index = {m.uid: m for m in every}
    containment = lifted_containment(every)

    human_mentions = [m for m in mentions if m.kind == HUMAN]
    partial = [m for m in human_mentions if not human_keys(m)]
    name_key_pairs = block_name_key(mentions)
    anchored_pairs, anchors = block_gedcom_anchored(
        mentions, name_key_pairs, index
    )
    results = {
        "one-token": run_strategy(
            "one-token",
            block_one_token(mentions),
            mentions,
            index,
            containment,
            batches,
        ),
        "name-key": run_strategy(
            "name-key", name_key_pairs, mentions, index, containment, batches
        ),
        "gedcom-anchored": run_strategy(
            "gedcom-anchored",
            anchored_pairs,
            mentions,
            index,
            containment,
            batches,
            anchors,
        ),
    }
    # what partial-name people would add if they blocked on their one token
    # against full-name people (the current rule, restricted to them)
    partial_uids = {m.uid for m in partial}
    partial_pairs = {
        p
        for p in block_one_token(human_mentions)
        if (p[0] in partial_uids) != (p[1] in partial_uids)
        and index[p[0]].sha1 != index[p[1]].sha1
    }
    # cross-manifest person pairs per key variant (the verdict table
    # shows what each keeps)
    variant_pairs = {
        variant: _cross_manifest(block_humans(human_mentions, variant), index)
        for variant in HUMAN_KEY_VARIANTS
    }
    variant_pairs["kin-surnames + keyless vs GEDCOM"] = _cross_manifest(
        block_humans(human_mentions) | partial_anchor_pairs(mentions), index
    )
    place_mentions = [m for m in mentions if m.kind == PLACE_KIND]
    place_variant_pairs = {
        variant: _cross_manifest(
            _block_by_keys(
                place_mentions,
                lambda m, variant=variant: place_keys(m, variant),
            ),
            index,
        )
        for variant in PLACE_KEY_VARIANTS
    }
    homonyms = homonym_groups(mentions)
    serial_plain = simulate_serial(all_manifests, pairs_per_call)
    serial_merged = simulate_serial(
        all_manifests,
        pairs_per_call,
        collapse_map(results["gedcom-anchored"]),
    )
    verdicts = (
        verdict_calibration(
            verdict_dirs,
            [m for m in every if m.source == "gedcom"],
        )
        if verdict_dirs
        else None
    )

    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "inputs": {
            "manifests": len(manifests),
            "gedcom": bool(gedcom_manifest),
            "runs_skipped_same_as_gedcom": skipped_same_as_gedcom,
            "mentions_total": len(every),
            "mentions_clustered": len(mentions),
            "mentions_unextracted_excluded": len(every) - len(mentions),
            "mentions_by_kind": dict(
                sorted(Counter(m.kind for m in mentions).items())
            ),
            "gedcom_mentions": sum(
                1 for m in mentions if m.source == "gedcom"
            ),
            "kin_edges_lifted": sum(len(m.kin) for m in every) // 2,
            "partial_name_people": len(partial),
            "partial_name_pairs_if_one_token": len(partial_pairs),
            "human_pairs_by_key_variant": variant_pairs,
            "place_pairs_by_key_variant": place_variant_pairs,
        },
        "assumptions": {
            "pairs_per_call": pairs_per_call,
            "calls_per_window": calls_per_window,
            "chars_per_token": CHARS_PER_TOKEN,
        },
        "strategies": {
            name: strategy_summary(
                result, index, pairs_per_call, calls_per_window
            )
            for name, result in results.items()
        },
        "homonyms": {
            "groups": len(homonyms),
            "mentions": sum(len(g["members"]) for g in homonyms),
            "by_kind": dict(
                sorted(Counter(g["kind"] for g in homonyms).items())
            ),
            "kin_linked_groups": sum(g["status"] == "hard" for g in homonyms),
            "gedcom_groups": sum(g["gedcom"] for g in homonyms),
            "examples": [
                " + ".join(index[uid].ref for uid in g["example"])
                for g in homonyms[:EXAMPLES]
            ],
        },
        "per_artifact_serial": {
            "artifacts": len(serial_plain.order),
            "rows_no_merges": serial_plain.rows,
            "calls_no_merges": serial_plain.calls,
            "rows_confident_merged": serial_merged.rows,
            "calls_confident_merged": serial_merged.calls,
            "max_rows_one_artifact": max(
                serial_plain.rows_per_artifact, default=0
            ),
        },
        "verdicts": summarize_verdicts(verdicts) if verdicts else None,
    }
    detail = {
        "index": index,
        "results": results,
        "homonyms": homonyms,
        "serial": (serial_plain, serial_merged),
        "verdict_rows": verdicts or [],
    }
    return summary, detail


# -- output -------------------------------------------------------------------


def _cross_manifest(pairs: set[Pair], index: dict[str, Mention]) -> int:
    return sum(1 for a, b in pairs if index[a].sha1 != index[b].sha1)


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def render_report(summary: dict) -> str:
    """Aggregate-only markdown: counts, kinds, SHA-1-prefix examples."""
    inputs = summary["inputs"]
    strategies = summary["strategies"]
    assumptions = summary["assumptions"]
    lines = [
        "## Corpus clustering calibration (aggregates only)",
        "",
        f"Inputs: {inputs['manifests']} document manifests"
        + (" + the GEDCOM export" if inputs["gedcom"] else "")
        + f"; {inputs['mentions_clustered']} mentions clustered "
        f"({inputs['mentions_unextracted_excluded']} non-extracted "
        f"excluded); {inputs['gedcom_mentions']} GEDCOM mentions; "
        f"{inputs['kin_edges_lifted']} local-id kin edges lifted.",
        "",
        "### Blocked pairs per kind",
        "",
    ]
    kinds = sorted(
        {
            kind
            for s in strategies.values()
            for kind in s["blocked_pairs_by_kind"]
        }
    )
    header = "| kind | " + " | ".join(
        f"{name} blocked | {name} candidates" for name in strategies
    )
    lines += [header + " |", "|---" * (1 + 2 * len(strategies)) + "|"]
    for kind in kinds:
        cells = []
        for s in strategies.values():
            cells.append(str(s["blocked_pairs_by_kind"].get(kind, 0)))
            cells.append(str(s["candidate_pairs_by_kind"].get(kind, 0)))
        lines.append(f"| {kind} | " + " | ".join(cells) + " |")
    totals = []
    for s in strategies.values():
        totals += [f"**{s['blocked_pairs']}**", f"**{s['candidate_pairs']}**"]
    lines.append("| **all** | " + " | ".join(totals) + " |")
    lines += [
        "",
        "Candidates = blocked minus within-manifest pairs (soft "
        "cannot-link) and pairs `impossible_pair` rules out once local-id "
        "kin edges and place containment are lifted.",
        "",
        "### Constraints, closure and review load",
        "",
        "| | " + " | ".join(strategies) + " |",
        "|---" * (1 + len(strategies)) + "|",
    ]
    rows = [
        (
            "within-manifest pairs removed (cannot-link)",
            "within_manifest_removed",
        ),
        ("of which joined by a lifted kin edge", "kin_edge_pruned_lifted"),
        (
            "cross-manifest place-containment prunes (lifted)",
            "place_containment_pruned_lifted",
        ),
        (
            "impossible_pair prunes without the lift",
            "impossible_pair_pruned_unlifted",
        ),
        ("candidate pairs", "candidate_pairs"),
        ("confident pairs (proxy)", "confident_pairs"),
        ("ambiguous pairs (proxy)", "ambiguous_pairs"),
        ("clusters (2+ mentions)", "clusters"),
        ("singletons", "singletons"),
        ("largest cluster", "largest_cluster"),
        ("confident clusters", ("cluster_status", "confident")),
        ("ambiguous clusters", ("cluster_status", "ambiguous")),
        (
            "conflicting clusters (kin edge inside)",
            ("cluster_status", "conflicting-hard"),
        ),
        (
            "conflicting clusters (same manifest only)",
            ("cluster_status", "conflicting-soft"),
        ),
        ("review clusters (ambiguous + conflicting)", "review_clusters"),
        (
            "closure over confident links only: clusters",
            "confident_closure_clusters",
        ),
        ("... largest", "confident_closure_largest"),
        ("... conflicting", "confident_closure_conflicting"),
        (
            "... of which a kin edge inside",
            "confident_closure_conflicting_hard",
        ),
        ("mentions in review clusters", "review_mentions"),
        ("adjudication calls, all candidates", "adjudication_calls_all"),
        (
            "adjudication calls, ambiguous only",
            "adjudication_calls_ambiguous_only",
        ),
        ("est. input tokens per call (mean)", "prompt_tokens_mean"),
        ("est. input tokens per call (max)", "prompt_tokens_max"),
        ("windows, all candidates", "windows_all"),
        ("windows, ambiguous only", "windows_ambiguous_only"),
    ]
    for title, key in rows:
        cells = []
        for s in strategies.values():
            if isinstance(key, tuple):
                cells.append(str(s[key[0]].get(key[1], 0)))
            else:
                cells.append(str(s[key]))
        lines.append(f"| {title} | " + " | ".join(cells) + " |")
    anchored = strategies.get("gedcom-anchored", {})
    if "anchored_mentions" in anchored:
        by_count = anchored["anchored_by_candidates"]
        lines += [
            "",
            f"GEDCOM anchoring: {anchored['anchored_mentions']} mentions "
            "key-match a GEDCOM person or place ("
            + "; ".join(f"{k}: {v}" for k, v in by_count.items())
            + ").",
        ]
        if anchored.get("multi_anchor_examples"):
            lines.append(
                "Multi-anchor examples: "
                + ", ".join(anchored["multi_anchor_examples"])
                + "."
            )
    lines += [
        "",
        f"Keyless people (one name token and no kin surname): "
        f"{inputs['partial_name_people']}. name-key leaves them unblocked; "
        f"gedcom-anchored pairs them with GEDCOM people of a compatible "
        f"given name; the one-token rule would pair them with "
        f"{inputs['partial_name_pairs_if_one_token']} full-name people in "
        f"other manifests.",
        "",
        "### Homonyms",
        "",
    ]
    homonyms = summary["homonyms"]
    lines.append(
        f"{homonyms['groups']} identical-name groups hold mentions that one "
        f"manifest keeps apart ({homonyms['mentions']} mentions; "
        f"{homonyms['kin_linked_groups']} joined by a kin edge, e.g. a "
        f"parent and child sharing a name; {homonyms['gedcom_groups']} "
        f"inside the GEDCOM). By kind: "
        + ", ".join(f"{k} {v}" for k, v in homonyms["by_kind"].items())
        + ". Exact-label resolve would merge every one of them."
    )
    if homonyms["examples"]:
        lines.append("Examples: " + "; ".join(homonyms["examples"]) + ".")
    for name, s in strategies.items():
        if s["conflict_examples"]:
            lines.append(
                f"Conflicting-cluster examples ({name}): "
                + "; ".join(
                    f"{status}: " + ", ".join(found)
                    for status, found in s["conflict_examples"].items()
                )
                + "."
            )
    serial = summary["per_artifact_serial"]
    lines += [
        "",
        "### Per-artifact path for comparison",
        "",
        f"Simulated serial per-artifact reconcile (`block_candidates` "
        f"against every earlier artifact, greedy order, one CSV row per "
        f"blocked pair): {serial['rows_no_merges']} rows and "
        f"{serial['calls_no_merges']} calls if nothing merges; "
        f"{serial['rows_confident_merged']} rows and "
        f"{serial['calls_confident_merged']} calls if every confident "
        f"cluster merges. The largest single artifact produces "
        f"{serial['max_rows_one_artifact']} rows.",
    ]
    verdicts = summary.get("verdicts")
    if verdicts:
        lines += render_verdicts(verdicts, inputs)
    lines += [
        "",
        f"Assumptions: {assumptions['pairs_per_call']} pairs per call "
        f"(a cluster's pairs stay together); "
        f"{assumptions['calls_per_window']} adjudication calls per "
        f"subscription window; {assumptions['chars_per_token']} characters "
        f"per token.",
    ]
    return "\n".join(lines) + "\n"


def render_verdicts(verdicts: dict, inputs: dict) -> list[str]:
    lines = [
        "",
        "### Earlier adjudicator verdicts vs the stronger keys",
        "",
        f"{verdicts['rows']} equivalence rows from an earlier per-artifact "
        f"pass against a graph holding the GEDCOM (unreviewed machine "
        f"verdicts: a proxy, not ground truth). The one-token rule "
        f"reproduces {verdicts['one_token_reproduced']} of them from names "
        f"alone.",
        "",
        "| kind | verdict | rows | kept by name-key | kept by "
        "gedcom-anchored | proxy confident | other side found in GEDCOM |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, by_verdict in verdicts["per_kind"].items():
        for verdict, counts in by_verdict.items():
            lines.append(
                f"| {kind} | {verdict} | {counts['rows']} | "
                f"{counts['name_key']} "
                f"({_pct(counts['name_key'], counts['rows'])}) | "
                f"{counts['gedcom_anchored']} "
                f"({_pct(counts['gedcom_anchored'], counts['rows'])}) | "
                f"{counts['proxy_confident']} | {counts['mapped']} |"
            )
    people = verdicts["people_by_verdict"]
    corpus = inputs.get("human_pairs_by_key_variant", {})
    lines += [
        "",
        "Person-key variants: earlier verdict rows each keeps, and the "
        "cross-manifest person pairs it blocks in this corpus.",
        "",
        "| person key | "
        + " | ".join(f"{v} kept (of {n})" for v, n in people.items())
        + " | corpus pairs |",
        "|---" * (2 + len(people)) + "|",
    ]
    for variant, kept in verdicts["human_key_variants"].items():
        lines.append(
            f"| {variant} | "
            + " | ".join(
                f"{kept.get(v, 0)} ({_pct(kept.get(v, 0), n)})"
                for v, n in people.items()
            )
            + f" | {corpus.get(variant, '')} |"
        )
    places = verdicts["places_by_verdict"]
    place_corpus = inputs.get("place_pairs_by_key_variant", {})
    lines += [
        "",
        "| place key | "
        + " | ".join(f"{v} kept (of {n})" for v, n in places.items())
        + " | corpus pairs |",
        "|---" * (2 + len(places)) + "|",
    ]
    for variant, kept in verdicts["place_key_variants"].items():
        lines.append(
            f"| {variant} | "
            + " | ".join(
                f"{kept.get(v, 0)} ({_pct(kept.get(v, 0), n)})"
                for v, n in places.items()
            )
            + f" | {place_corpus.get(variant, '')} |"
        )
    lines += [
        "",
        "Adjudicator confidence by verdict: "
        + "; ".join(
            f"{verdict} " + ", ".join(f"{b}: {n}" for b, n in bins.items())
            for verdict, bins in verdicts["confidence_by_verdict"].items()
        )
        + ".",
        "The same, on pairs gedcom-anchored keeps: "
        + "; ".join(
            f"{verdict} " + ", ".join(f"{b}: {n}" for b, n in bins.items())
            for verdict, bins in verdicts[
                "confidence_by_verdict_on_anchored"
            ].items()
            if bins
        )
        + ".",
        "Proxy class vs verdict on pairs gedcom-anchored keeps: "
        + ", ".join(
            f"{key} {n}"
            for key, n in verdicts["proxy_vs_verdict_on_anchored"].items()
        )
        + ".",
    ]
    return lines


def _private_file(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", newline="") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _csv_text(fieldnames: list[str], rows: Iterable[dict]) -> str:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def write_outputs(out: Path, summary: dict, detail: dict) -> None:
    """Aggregates plus label-bearing detail, all private (700/600)."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out, 0o700)
    index: dict[str, Mention] = detail["index"]
    _private_file(out / "summary.json", json.dumps(summary, indent=2) + "\n")
    _private_file(out / "report.md", render_report(summary))
    for name, result in detail["results"].items():
        constraints = result.constraints
        pair_rows = []
        for pair in sorted(constraints.blocked):
            first, second = index[pair[0]], index[pair[1]]
            if pair in constraints.within_manifest:
                stage = "within-manifest"
            elif pair in constraints.kin_edge:
                stage = "kin-edge"
            elif pair in constraints.place_containment:
                stage = "place-containment"
            else:
                stage = "candidate"
            evidence = result.evidence.get(pair)
            pair_rows.append(
                {
                    "a": first.ref,
                    "a_label": first.label,
                    "b": second.ref,
                    "b_label": second.label,
                    "kind": first.kind,
                    "stage": stage,
                    "evidence": (
                        f"{evidence.name}/{evidence.dates}/{evidence.kin}"
                        if evidence
                        else ""
                    ),
                    "class": result.pair_class.get(pair, ""),
                }
            )
        _private_file(
            out / f"pairs-{name}.csv",
            _csv_text(list(pair_rows[0]) if pair_rows else ["a"], pair_rows),
        )
        for suffix, clusters in (
            ("", result.clusters),
            ("-confident-only", result.confident_clusters),
        ):
            _private_file(
                out / f"clusters-{name}{suffix}.csv",
                _csv_text(
                    ["cluster", "size", "status", "pairs", "members"],
                    (
                        {
                            "cluster": number,
                            "size": len(row.members),
                            "status": row.status,
                            "pairs": len(row.pairs),
                            "members": " | ".join(
                                f"{index[uid].ref}={index[uid].label}"
                                for uid in row.members
                            ),
                        }
                        for number, row in enumerate(clusters)
                        if len(row.members) > 1
                    ),
                ),
            )
    _private_file(
        out / "homonyms.csv",
        _csv_text(
            ["kind", "status", "members"],
            (
                {
                    "kind": group["kind"],
                    "status": group["status"],
                    "members": " | ".join(
                        f"{index[uid].ref}={index[uid].label}"
                        for uid in group["members"]
                    ),
                }
                for group in detail["homonyms"]
            ),
        ),
    )
    serial_plain, serial_merged = detail["serial"]
    _private_file(
        out / "serial.csv",
        _csv_text(
            ["position", "artifact", "rows_no_merges", "rows_merged"],
            (
                {
                    "position": position,
                    "artifact": sha1[:8],
                    "rows_no_merges": plain,
                    "rows_merged": merged,
                }
                for position, (sha1, plain, merged) in enumerate(
                    zip(
                        serial_plain.order,
                        serial_plain.rows_per_artifact,
                        serial_merged.rows_per_artifact,
                        strict=True,
                    ),
                    start=1,
                )
            ),
        ),
    )
    if detail["verdict_rows"]:
        rows = detail["verdict_rows"]
        _private_file(out / "verdicts.csv", _csv_text(list(rows[0]), rows))


def _inside_repo(path: Path) -> bool:
    repo = Path(__file__).resolve().parents[2]
    resolved = path.resolve()
    return resolved == repo or repo in resolved.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--gedcom", type=Path)
    parser.add_argument("--verdict-runs", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--pairs-per-call", type=int, default=ADJUDICATION_BATCH_PAIRS
    )
    parser.add_argument(
        "--calls-per-window",
        type=int,
        default=DEFAULT_CALLS_PER_WINDOW,
        help="assumed adjudication calls per subscription usage window",
    )
    parser.add_argument(
        "--include-unextracted",
        action="store_true",
        help="also cluster entities the manifest marks extract=false",
    )
    args = parser.parse_args(argv)
    if _inside_repo(args.out):
        parser.error(
            "--out must be outside the repository: the detail files carry "
            "names from the corpus"
        )
    summary, detail = run_experiment(
        [path for path in args.runs if path.is_dir()],
        args.gedcom,
        [path for path in args.verdict_runs if path.is_dir()],
        include_unextracted=args.include_unextracted,
        pairs_per_call=args.pairs_per_call,
        calls_per_window=args.calls_per_window,
    )
    write_outputs(args.out, summary, detail)
    print(render_report(summary))
    print(f"detail written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
