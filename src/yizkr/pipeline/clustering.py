"""Corpus clustering: every document's mentions, clustered across the
whole corpus into an identity map. It is the scratch driver's first
corpus stage (ADR-0025), and runs past the barrier over the manifests of
every document whose stages completed.

`cluster_corpus` is a pure function of the manifests and the thresholds
(clustering.yaml beside this module, load_config). It reads nothing but
its arguments: no wiki, no query service, no graph, and no model. Its
output changes only with them, with VERSION, which a change to what it
makes of the same manifests bumps, and with the source of the modules
that make it (CODE). The stage (`run_stage`) keeps that output in its
directory as `identity-map.json`, and reuses it while the documents'
manifests (by content), VERSION, that source and the thresholds stand.

**Mentions.** Every entity of every manifest is a mention, named by its
fingerprint (yizkr.pipeline.fingerprints): its artifact's SHA-1, its
canonical label, its kind and its excerpt span, never the manifest's id.
A batch without an entity manifest gives a mention per local id its
claims name. The GEDCOM export's people, places, occupations, media and
death events are mentions too. Only mentions the manifest marks for
extraction are clustered; the others are each a cluster of their own
("unclustered").

**Lifting.** Relations the manifest states between its own local ids are
lifted into mention space: kin (father, mother, child, spouse and
sibling, each seen from both ends, so a father claim and a child claim
say the same), the human participants of events, place containment
(`part_of`, `location`), typed names and nicknames, and dates; a birth or
death event's date is its participant's too, when it has only one. Only
what the manifests assert is used: no containment or kinship is read
from the graph, and none is inferred.

**Names.** A person's names are its label, its aliases and its typed
birth, married and official names, each read as a name:

- a bracketed part is a form of its own, and a marker at its start
  ("formerly", "later", "genannt") is dropped: "Gustave Lobig (formerly
  Gustav Löbig)" gives "Gustave Lobig" and "Gustav Löbig";
- a name written surname first is read given name first, and past its
  first comma only a maiden part stays: "Löbig, Ursula, Wwe." gives
  "Ursula Löbig", "LÖBIG, Ida, née SÖRNE" "Ida Löbig née Sörne";
- "X Y née Z" gives the forms "X Y" and "X Z", and "X née Z" gives "X"
  and "X Z";
- accents are folded, and titles ("Frau", "Dr"), kin and courtesy words
  ("Tante", "Dad", "Nana"), particles and initials are no name tokens.

A form of one token after a title, a placeholder ("N.N.") or an initial
names a surname alone, and so does a lone token that the person's other
forms or its kin use as a surname. A surname alone names no one: it
makes no key, no name match and no kin agreement ("Frau Löbig", "N.N.
Löbig").

**Blocking** proposes candidate pairs by keys of their own kind, never
by one shared token:

- *People*: a given-name token with another, surname token. The surname
  is any later token of the same form, or a surname of the person's
  lifted kin, taken with the first token of each form. Nicknames stand
  in for given names. Two keys match on the same surname and given names
  one of which starts with the other ("Hilde", "Hildegard").
- *Places*: each token of `min-place-token` characters or more in the
  part of a name before its first comma, but a generic head ("Kreis",
  "Lager", "Friedhof"). Region and country parts after it never key.
- *Organizations*: one name's distinctive tokens all in the other's.
- *Events* (births, deaths, marriages, migrations, deportations,
  imprisonments, releases, military service, other events and legal
  instruments): the same kind, with participants whose person keys
  match; an event without participants keys on its whole name.
- *Everything else*: the whole name, normalised.

With `gedcom-anchored`, a mention (of any kind but an event's) that
key-matches a mention of a GEDCOM export pairs with those GEDCOM anchors
only, never with other mentions, so the export is the roster documents
meet. A person named by one token only, with no kin to key a surname,
pairs only with GEDCOM people whose given name is compatible. A key that
more than `max-key-mentions` mentions of one kind share (a hub) is
blocked on by none of them: the mentions it would join stay apart, and
the summary counts them. Blocking looks keys up in indexes, so its work
grows with the mentions and the pairs it proposes, never with every pair
of mentions.

**Cannot-link.** Two ids of one manifest are kept apart (soft): the
manifest already unified what it could. The places and occupations of a
GEDCOM export are exempt, since the adapter makes one per distinct
string, and so are the refs of a batch without a manifest, whose ids are
chunk-scoped and never unified. A blocked pair the impossibility filter
rules out (`reconcile.impossible_pair`, ADR-0017), given the lifted
facts, is pruned (hard): a kin edge between the two, or one place inside
the other. The filter is reused only after the lift: a manifest's kin
are local ids until then, and it would see none. Dates never prune a
pair (ADR-0002, ADR-0017).

**Links.** Each candidate pair is a confident link or an ambiguous one,
by deterministic evidence alone:

- a person pair is confident when the names match (exactly, or by key)
  and kin that agree, or dates that agree, corroborate it;
- a place pair when the names match exactly, or one name's
  comma-separated parts are all among the other's ("Fläckfeld" and
  "Fläckfeld, Kreis Mockburg");
- an organization or a family pair when they share a name of two
  distinctive tokens or more ("Familie Löbig" has one);
- an event pair when every participant of the one with fewer is merged
  into one cluster with a participant of the other, and, but for a birth
  or a death, which a person has only one of, when their dates agree;
- any other pair when the names match exactly;
- and never when the two give dates that cannot overlap, when a mention
  has no kind, or when either matches more than `max-anchors` GEDCOM
  anchors (a place counting only the anchors it matches exactly, and
  those it nests in as one).

Dates are compared as ranges, at the precision their source states: a
decade is ten years, a circa date is widened by `circa-years`, a range's
bounds widen it, and a bound on one side only leaves the other open, so
a date is never narrowed to be compared (rule 5). Two dates agree when
their ranges overlap and both are at most `max-corroborating-years`
wide; a wider one (a century, a lone "before") agrees with nothing, and
conflicts only when it cannot overlap. Kin agree on a relation when a
kin's names match by more than a surname and their dates do not
conflict.

**Clusters.** The closure over every candidate pair gives the candidate
clusters, the units a review takes. A cluster is *conflicting* when it
joins a cannot-link pair: *hard* when the impossibility filter rules the
two out, *soft* when they are only two ids of one manifest. Otherwise it
is *ambiguous* when any of its links is, or when two of its members that
were never a pair contradict each other: dates that cannot overlap, kin
of one relation and one name whose dates conflict, or names that do not
match at all (for places, neither exactly nor nested). Blocking is not
transitive, through anchors, given-name prefixes and kin surnames, so
the closure can join what no pair of its members says. Otherwise it is
*confident*. Only confident clusters merge: their members share one
cluster. The members of ambiguous and conflicting clusters stay apart,
each a cluster of its own, and are counted. A mention with no candidate
pair is a singleton. Events are clustered after everything else, since
their links read the clusters their participants end in.

**The identity map** (`identity-map.json`, format FORMAT):

- `mentions`: fingerprint -> its `cluster` (a cluster id, the hash of the
  sorted fingerprints of its members) and its `method` (`confident`,
  `ambiguous`, `conflicting`, `singleton` or `unclustered`: what put it
  where it is), with the document's SHA-1, the manifest's local id, the
  label, the kind, the span and the occurrence it was made from;
- `hash`: the SHA-256 of every mention's cluster and method, and nothing
  else, so equal decisions hash equal whatever else the file holds;
- `clusters`: cluster id -> its kind, method and members;
- `review`: each ambiguous or conflicting candidate cluster, with its
  members, its pairs (the link and the evidence of each), and the pair
  that makes it conflict and how, or the two members never linked that
  contradict each other;
- `summary`: counts only, the adjudication-call estimate among them;
- `inputs`: the key it is reused under (VERSION, the digest of CODE's
  source, the thresholds' digest, and each document's SHA-1, source and
  batch content hash); `config`: the thresholds' digest.

The map holds labels, so it stays in the run directory, which is
local-only data. What the stage returns, and so what reaches the run's
events, is counts within labels of its own, never a document's words.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.claims import ClaimBatch, ItemValue, LocalRef, TimeValue
from yizkr.pipeline.fingerprints import (
    Mention,
    cluster_id,
    local_refs,
    mentions_of,
)
from yizkr.pipeline.orchestrate import BATCH_VOLATILE
from yizkr.pipeline.reconcile import (
    PLACE_KIND,
    PairFacts,
    PlaceContainment,
    impossible_pair,
)
from yizkr.pipeline.stages import (
    atomic_write_text,
    canonical_json,
    content_hash,
    sha256_text,
)

# what cluster_corpus makes of a set of manifests: bumped whenever the same
# manifests and thresholds would give another identity map, so that a
# stored map is made again
VERSION = 1
# the layout of identity-map.json
FORMAT = 1
STAGE = "clustering"
IDENTITY_MAP = "identity-map.json"
PACKAGE = files("yizkr.pipeline")
# the thresholds shipped with the package
DEFAULTS = PACKAGE / "clustering.yaml"
# the modules whose source makes the identity map: a map another source
# made is made again, whatever VERSION says
CODE = ("clustering.py", "fingerprints.py", "reconcile.py")

# what a manifest is the extraction of
DOCUMENT = "document"
GEDCOM = "gedcom"

# what put a mention in its cluster (a mention's `method`), which is also
# a candidate cluster's status
CONFIDENT = "confident"
AMBIGUOUS = "ambiguous"
CONFLICTING = "conflicting"
SINGLETON = "singleton"
UNCLUSTERED = "unclustered"
# how a conflicting cluster conflicts
HARD = "hard"
SOFT = "soft"

HUMAN = "human"
ORGANIZATION = "organization"
FAMILY = "family"
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
# the events a person takes part in once: their participants decide them
ONE_PER_PERSON = frozenset({"birth", "death"})
# the kinds the GEDCOM adapter makes one entity of per distinct string:
# two of them in one export never say that they differ
GEDCOM_STRING_KINDS = frozenset({PLACE_KIND, "occupation_class"})
# (the relation of the value to the subject, of the subject to the value)
# for each kinship property the impossibility filter reads
RELATION = {
    "father": ("parent", "child"),
    "mother": ("parent", "child"),
    "child": ("child", "parent"),
    "spouse": ("spouse", "spouse"),
    "sibling": ("sibling", "sibling"),
}
TYPED_NAME_PROPERTIES = ("birth_name", "married_name", "official_name")
CONTAINMENT_PROPERTIES = ("part_of", "location")
# the date properties of an event, and of the events a person is born or
# dies in
EVENT_DATE_PROPERTIES = ("point_in_time", "start_time")
_STRIP = ".,;:()\"'[]"
# titles, which stand before a surname named alone ("Frau Löbig") or a
# whole name
TITLES = frozenset(
    {
        "mr", "mrs", "ms", "miss", "mister", "dr", "doctor", "prof",
        "professor", "herr", "frau", "fraulein", "frl", "rabbi", "rev",
        "reverend", "sir", "lady", "lord", "mme", "madame", "monsieur",
        "mlle", "dame", "widow", "witwe", "wwe", "verw",
    }
)  # fmt: skip
# kin and courtesy words, which stand before a given name ("Tante Ida",
# "Nana Maisie") or for a person on their own ("Dad", "Dein Vater")
KIN_WORDS = frozenset(
    {
        "uncle", "aunt", "auntie", "tante", "onkel", "oma", "opa", "omi",
        "opi", "nana", "nan", "granny", "grandma", "grandpa", "grandmother",
        "grandfather", "great", "mother", "father", "mutter", "vater",
        "mutti", "vati", "mama", "papa", "mum", "mom", "dad", "daddy",
        "brother", "sister", "bruder", "schwester", "cousin", "cousine",
        "vetter", "son", "sohn", "daughter", "tochter", "dein", "deine",
        "mein", "meine", "unser", "unsere", "my", "your", "our",
    }
)  # fmt: skip
# particles and words a name is not known by
PARTICLES = frozenset(
    {
        "the", "and", "und", "von", "van", "der", "den", "del", "della",
        "les", "ben", "bat", "bar", "ibn", "zur", "auf", "sen", "jun",
        "senior", "junior",
    }
)  # fmt: skip
# words that start another form of the same name ("formerly Gustav
# Löbig"); the form after one is read without it
FORM_MARKERS = frozenset(
    {
        "formerly", "later", "previously", "alias", "aka", "called",
        "fruher", "vormals", "spater", "genannt", "gen",
    }
)  # fmt: skip
# a name form "X née Y" carries a second surname
MAIDEN_MARKERS = frozenset({"nee", "geb", "geborene", "born"})
NOT_A_NAME = TITLES | KIN_WORDS | PARTICLES | FORM_MARKERS | MAIDEN_MARKERS
# words no organization or family is known by
ORG_GENERIC = frozenset(
    {
        "the", "and", "und", "der", "die", "das", "des", "von", "for",
        "company", "firm", "firma", "gmbh", "inc", "ltd", "limited",
        "co", "cie", "bros", "brothers", "sons", "sohn", "sohne",
        "family", "familie",
    }
)  # fmt: skip
# the generic heads of place names, which key no place
PLACE_GENERIC = frozenset(
    {
        "kreis", "landkreis", "stadtkreis", "bezirk", "gemeinde", "stadt",
        "dorf", "lager", "friedhof", "county", "district", "province",
        "provinz", "region", "parish", "road", "street", "strasse",
        "gasse", "platz", "allee", "lane", "avenue", "camp", "cemetery",
    }
)  # fmt: skip
# the shortest token an organization's distinctive tokens hold
_MIN_ORG_TOKEN = 3
# an interval's open end
_OPEN = float("inf")

Pair = tuple[str, str]
# a key a mention is blocked on: ("key", given name, surname) for a
# person, ("lone", token) for a person of one name token, ("token",
# token) for a place or an organization, ("name", name) otherwise
BlockingKey = tuple[str, ...]


# -- the thresholds -----------------------------------------------------------


class ClusteringConfigError(ValueError):
    """A file of thresholds that cannot be read, or that sets a key the
    defaults do not have, or a value out of range."""


def _dashed(name: str) -> str:
    return name.replace("_", "-")


class _Settings(BaseModel):
    """Keys are written with dashes, as in clustering.yaml; any other key
    is refused."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=_dashed
    )


class Blocking(_Settings):
    min_name_token: int = Field(ge=1)
    min_place_token: int = Field(ge=1)
    gedcom_anchored: bool
    max_key_mentions: int = Field(ge=2)


class Evidence(_Settings):
    circa_years: int = Field(ge=0)
    max_corroborating_years: int = Field(ge=0)


class Confident(_Settings):
    max_anchors: int = Field(ge=1)


class Adjudication(_Settings):
    pairs_per_call: int = Field(ge=1)


class ClusteringConfig(_Settings):
    """The thresholds of corpus clustering (clustering.yaml)."""

    version: Literal[1]
    blocking: Blocking
    evidence: Evidence
    confident: Confident
    adjudication: Adjudication

    def digest(self) -> str:
        """The SHA-256 of the thresholds, which the identity map records
        and is reused under."""
        return sha256_text(
            canonical_json(self.model_dump(mode="json", by_alias=True))
        )


def _merged(base: Any, override: Any) -> Any:
    """`override` over `base`: mappings merged key by key, anything else
    replaced whole."""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merged(base[key], value) if key in base else value
        return merged
    return override


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> ClusteringConfig:
    """The thresholds: clustering.yaml, with the file at `path` over it
    when one is named, and `overrides` over both, keys written as in
    clustering.yaml. Raises ClusteringConfigError for a file that cannot
    be read, a key the defaults do not have, or a value out of range."""
    data = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
    if path is not None:
        try:
            loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ClusteringConfigError(
                f"the clustering thresholds in {path} cannot be read "
                f"({type(exc).__name__})"
            ) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            raise ClusteringConfigError(
                f"{path} holds no mapping of clustering thresholds"
            )
        data = _merged(data, loaded)
    if overrides:
        data = _merged(data, overrides)
    try:
        return ClusteringConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors()
        )
        raise ClusteringConfigError(
            f"clustering thresholds{f' in {path}' if path else ''}: {problems}"
        ) from exc


# -- names --------------------------------------------------------------------


@functools.lru_cache(maxsize=1 << 16)
def fold(text: str) -> str:
    """Case- and accent-insensitive form: "Cäcilie" and "cacilie" are one."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("ß", "ss")


def _folded_tokens(name: str) -> list[str]:
    return list(_folded(name))


@functools.lru_cache(maxsize=1 << 16)
def _folded(name: str) -> tuple[str, ...]:
    return tuple(
        token for raw in name.split() if (token := fold(raw.strip(_STRIP)))
    )


def normal(name: str) -> str:
    """A whole name as its folded tokens, one space apart."""
    return " ".join(_folded_tokens(name))


def _letters(token: str) -> bool:
    """A token of letters, with hyphens or apostrophes inside ("hans-otto")
    but no digit, and no dot inside (initials, "N.N.")."""
    return token.replace("-", "").replace("'", "").isalpha()


_BRACKETED = re.compile(r"[(\[]([^()\[\]]*)[)\]]")


def _segments(name: str) -> list[str]:
    """A name's parts: the text outside its brackets, and each bracketed
    part ("Ida (Peppi) Sörne" gives "Ida Sörne" and "Peppi")."""
    return [_BRACKETED.sub(" ", name), *_BRACKETED.findall(name)]


def _starts_maiden(part: str) -> bool:
    tokens = _folded_tokens(part)
    return bool(tokens) and tokens[0] in MAIDEN_MARKERS


def _given_first(segment: str) -> str:
    """A name written surname first ("Löbig, Ursula, Wwe."), given names
    first ("Ursula Löbig"). Past a name's first comma only a maiden part
    ("née Sörne") stays; an occupation or a title there is dropped."""
    parts = [part.strip() for part in segment.split(",")]
    if len(parts) == 1:
        return segment
    head, rest = parts[0], [part for part in parts[1:] if part]
    maiden = [part for part in rest if _starts_maiden(part)]
    if len(_folded_tokens(head)) == 1 and rest and not _starts_maiden(rest[0]):
        return " ".join([rest[0], head, *maiden])
    return " ".join([head, *maiden])


def _significant(token: str, min_token: int) -> bool:
    return (
        len(token) >= min_token and _letters(token) and token not in NOT_A_NAME
    )


def _before_a_surname(token: str) -> bool:
    """A title, a placeholder or an initial: what stands before a surname
    named alone ("Frau Löbig", "N.N. Löbig", "J. Löbig")."""
    return token in TITLES or not _letters(token) or len(token) == 1


def person_forms(
    name: str, min_token: int
) -> list[tuple[tuple[str, ...], bool]]:
    """The forms of one person's name (the module docstring says how a
    name is read): each form's significant tokens, and whether the form
    names a surname alone."""
    forms = []
    for segment in _segments(name):
        # a form marker ("formerly") is no name token, so the form after
        # one is read as it stands
        tokens = _folded_tokens(_given_first(segment))
        marker = next(
            (i for i, token in enumerate(tokens) if token in MAIDEN_MARKERS),
            None,
        )
        if marker is None:
            parts = [tokens]
        else:
            before = tokens[:marker]
            given = before[:-1] if len(before) >= 2 else before
            parts = [before, given + tokens[marker + 1 :][-1:]]
        for part in parts:
            significant = tuple(
                token for token in part if _significant(token, min_token)
            )
            if significant:
                surname = len(significant) == 1 and any(
                    _before_a_surname(token) for token in part
                )
                forms.append((significant, surname))
    return forms


def _given_match(first: str, second: str) -> bool:
    return first.startswith(second) or second.startswith(first)


def _key_map(keys: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    by_surname: dict[str, set[str]] = defaultdict(set)
    for given, surname in keys:
        by_surname[surname].add(given)
    return {surname: frozenset(g) for surname, g in by_surname.items()}


def keys_match(
    first: Mapping[str, frozenset[str]], second: Mapping[str, frozenset[str]]
) -> bool:
    """Whether two people's keys (surname -> given names) share a surname
    with given names one of which starts with the other."""
    return any(
        _given_match(one, other)
        for surname, givens in first.items()
        if surname in second
        for one in givens
        for other in second[surname]
    )


def _distinctive(name: str) -> frozenset[str]:
    """An organization's or a family's normal name, as the tokens it is
    known by."""
    return frozenset(
        token
        for token in name.split()
        if len(token) >= _MIN_ORG_TOKEN and token not in ORG_GENERIC
    )


# -- dates --------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """The years a stated date covers, at its source's own precision, with
    an open end where only one bound is stated."""

    start: float
    end: float

    def overlaps(self, other: Interval) -> bool:
        return self.start <= other.end and other.start <= self.end

    @property
    def width(self) -> float:
        return self.end - self.start


def _span(value: TimeValue) -> int:
    return {"century": 99, "decade": 9}.get(value.precision or "", 0)


def date_interval(
    value: TimeValue, qualifiers: Iterable[Any], circa_years: int
) -> Interval:
    """The years a claim's date covers: its year at its precision (a
    decade ten years, a century a hundred), widened by the claim's
    earliest and latest dates, open on the side a lone bound leaves open,
    and widened by `circa_years` each way when the source gives it as
    circa. Never narrower than what the source says."""
    start: float = value.year
    end: float = value.year + _span(value)
    earliest = [
        q.value.year
        for q in qualifiers
        if q.property == "earliest_date" and isinstance(q.value, TimeValue)
    ]
    latest = [
        q.value.year + _span(q.value)
        for q in qualifiers
        if q.property == "latest_date" and isinstance(q.value, TimeValue)
    ]
    if earliest:
        start = min(start, *earliest)
        if not latest:
            end = _OPEN
    if latest:
        end = max(end, *latest)
        if not earliest:
            start = -_OPEN
    circa = any(
        q.property == "sourcing_circumstances"
        and isinstance(q.value, ItemValue)
        and getattr(q.value.value, "key", None) == "circa"
        for q in qualifiers
    )
    if circa:
        start, end = start - circa_years, end + circa_years
    return Interval(start, end)


def _dates(first: list[Interval], second: list[Interval], widest: int) -> str:
    """Whether two lists of dates agree (two overlap, and neither is wider
    than `widest` years), conflict (none overlaps) or say nothing."""
    if not first or not second:
        return "unknown"
    overlapping = [
        (one, other)
        for one in first
        for other in second
        if one.overlaps(other)
    ]
    if not overlapping:
        return "conflict"
    if any(
        one.width <= widest and other.width <= widest
        for one, other in overlapping
    ):
        return "agree"
    return "unknown"


def _combine_dates(*results: str) -> str:
    if "conflict" in results:
        return "conflict"
    return "agree" if "agree" in results else "unknown"


# -- mentions -----------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """One document's batch, as clustering reads it: the SHA-1 of the
    document's media, its batch, and whether the document is a GEDCOM
    export (`source`)."""

    sha1: str
    batch: ClaimBatch
    source: str = DOCUMENT


@dataclass(eq=False)
class _Node:
    """A mention, with what its manifest says about it lifted into mention
    space (kin, participants and containers by fingerprint), and the keys
    blocking and evidence read."""

    mention: Mention
    source: str
    # whether its manifest keeps it apart from the manifest's other ids
    exclusive: bool
    aliases: list[str]
    typed_names: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    kin: set[tuple[str, str]] = field(default_factory=set)
    participants: set[str] = field(default_factory=set)
    containers: set[str] = field(default_factory=set)
    births: list[Interval] = field(default_factory=list)
    deaths: list[Interval] = field(default_factory=list)
    when: list[Interval] = field(default_factory=list)
    # computed once every node of the corpus is lifted (_derive): a
    # person's name forms, each with whether it names a surname alone
    forms: list[tuple[tuple[str, ...], bool]] = field(default_factory=list)
    keys: dict[str, frozenset[str]] = field(default_factory=dict)
    lone: frozenset[str] = frozenset()
    givens: frozenset[str] = frozenset()
    place_keys: frozenset[str] = frozenset()
    place_parts: tuple[frozenset[str], ...] = ()
    org_sets: tuple[frozenset[str], ...] = ()
    # every name, normalised, and those that match another's exactly (for
    # a person, those that name more than a surname)
    label_keys: frozenset[str] = frozenset()
    named_keys: frozenset[str] = frozenset()
    # the keys it is blocked on, hub keys left out (_hubs)
    blocking: frozenset[BlockingKey] = frozenset()
    # the mention's fingerprint, artifact, kind and label, read on every
    # pair
    id: str = field(init=False)
    sha1: str = field(init=False)
    kind: str = field(init=False)
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.id = self.mention.fingerprint
        self.sha1 = self.mention.artifact
        self.kind = self.mention.kind
        self.label = self.mention.label

    @property
    def names(self) -> list[str]:
        return [
            name
            for name in (self.label, *self.aliases, *self.typed_names)
            if name
        ]

    @property
    def kin_ids(self) -> set[str]:
        return {node_id for _relation, node_id in self.kin}


def _local_id(ref: Any) -> str | None:
    return ref.local_id if isinstance(ref, LocalRef) else None


def _nodes(manifest: Manifest, circa_years: int) -> list[_Node]:
    """One node per mention of the manifest, with the relations the
    manifest states between its local ids lifted into mention space."""
    batch = manifest.batch
    mentions = mentions_of(manifest.sha1, batch)
    if batch.entities:
        aliases = {
            entity.id: list(entity.aliases) for entity in batch.entities
        }
    else:
        aliases = defaultdict(list)
        for ref in local_refs(batch):
            if ref.label and ref.label not in aliases[ref.local_id]:
                aliases[ref.local_id].append(ref.label)
    by_local: dict[str, _Node] = {}
    for mention in mentions:
        by_local[mention.local_id] = _Node(
            mention=mention,
            source=manifest.source,
            exclusive=bool(batch.entities)
            and not (
                manifest.source == GEDCOM
                and mention.kind in GEDCOM_STRING_KINDS
            ),
            aliases=[
                alias
                for alias in aliases.get(mention.local_id, [])
                if alias != mention.label
            ],
        )
    for claim in batch.claims:
        subject = by_local.get(_local_id(claim.subject) or "")
        if subject is None:
            continue
        value = claim.value
        target = (
            by_local.get(_local_id(value.value) or "")
            if isinstance(value, ItemValue)
            else None
        )
        prop = claim.property
        if prop in RELATION and target is not None and target is not subject:
            forward, backward = RELATION[prop]
            subject.kin.add((forward, target.id))
            target.kin.add((backward, subject.id))
        elif prop == "participant" and target is not None:
            if target.kind == HUMAN:
                subject.participants.add(target.id)
            elif subject.kind == HUMAN and target.kind in EVENT_KINDS:
                target.participants.add(subject.id)
        elif (
            prop in CONTAINMENT_PROPERTIES
            and target is not None
            and target is not subject
            and subject.kind == PLACE_KIND
            and target.kind == PLACE_KIND
        ):
            subject.containers.add(target.id)
        elif prop in TYPED_NAME_PROPERTIES and value.type == "monolingual":
            subject.typed_names.append(value.value.text)
        elif prop == "nickname" and value.type == "monolingual":
            subject.nicknames.append(value.value.text)
        elif isinstance(value, TimeValue):
            interval = date_interval(value, claim.qualifiers, circa_years)
            if prop == "date_of_birth":
                subject.births.append(interval)
            elif prop == "date_of_death":
                subject.deaths.append(interval)
            elif prop in EVENT_DATE_PROPERTIES:
                subject.when.append(interval)
    by_id = {node.id: node for node in by_local.values()}
    for node in by_local.values():
        # a birth or death event's date is its participant's, when it has
        # one: of several (a child and a parent), none says who was born
        if node.kind not in ONE_PER_PERSON or len(node.participants) != 1:
            continue
        for person_id in node.participants:
            person = by_id[person_id]
            dates = person.births if node.kind == "birth" else person.deaths
            dates.extend(node.when)
    return list(by_local.values())


def _surnames(node: _Node) -> set[str]:
    """The surnames a person's forms give: the final token of each form of
    two tokens or more, and each surname named alone."""
    return {
        tokens[-1] for tokens, alone in node.forms if alone or len(tokens) >= 2
    }


def _derive(nodes: Sequence[_Node], config: ClusteringConfig) -> None:
    """Every key a node is blocked and compared by. A person's keys take
    the surnames of its lifted kin, so every person's forms come first."""
    min_name = config.blocking.min_name_token
    min_place = config.blocking.min_place_token
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        if node.kind == HUMAN:
            node.forms = [
                form
                for name in node.names
                for form in person_forms(name, min_name)
            ]
    for node in nodes:
        node.label_keys = frozenset(
            key for name in node.names if (key := normal(name))
        )
        node.named_keys = node.label_keys
        if node.kind == HUMAN:
            _derive_person(node, by_id, min_name)
        elif node.kind == PLACE_KIND:
            firsts = [name.split(",")[0] for name in node.names]
            node.place_keys = frozenset(
                token
                for first in firsts
                for token in _folded_tokens(first)
                if len(token) >= min_place
                and _letters(token)
                and token not in PLACE_GENERIC
            )
            node.place_parts = tuple(
                parts
                for name in node.names
                if (
                    parts := frozenset(
                        part
                        for piece in name.split(",")
                        if (part := normal(piece))
                    )
                )
            )
        elif node.kind == ORGANIZATION:
            node.org_sets = tuple(
                tokens
                for name in node.names
                if (
                    tokens := frozenset(
                        token
                        for token in _folded_tokens(name)
                        if len(token) >= _MIN_ORG_TOKEN
                        and token not in ORG_GENERIC
                    )
                )
            )


def _derive_person(
    node: _Node, by_id: Mapping[str, _Node], min_name: int
) -> None:
    kin_surnames = set().union(
        *(_surnames(by_id[kin_id]) for kin_id in node.kin_ids)
    )
    own = {tokens[-1] for tokens, _alone in node.forms if len(tokens) >= 2}
    # the forms that name someone: not a surname alone, nor a lone token
    # the person's forms or kin use as a surname
    named = [
        tokens
        for tokens, alone in node.forms
        if not alone
        and not (len(tokens) == 1 and tokens[0] in own | kin_surnames)
    ]
    nicknames = {
        tokens[0]
        for nickname in node.nicknames
        for tokens, alone in person_forms(nickname, min_name)
        if not alone
    }
    firsts = {tokens[0] for tokens in named} | nicknames
    keys = {
        (given, surname)
        for tokens in named
        for position, given in enumerate(tokens[:-1])
        for surname in tokens[position + 1 :]
    }
    keys.update((nick, surname) for nick in nicknames for surname in own)
    keys.update(
        (given, surname) for surname in kin_surnames for given in firsts
    )
    # a given name that is its own surname keys on one shared token
    node.keys = _key_map(
        (given, surname) for given, surname in keys if given != surname
    )
    node.named_keys = frozenset(" ".join(tokens) for tokens in named)
    node.lone = frozenset(tokens[0] for tokens in named if len(tokens) == 1)
    givens = set(nicknames)
    for tokens in named:
        givens.update(tokens[:-1] if len(tokens) >= 2 else tokens)
    node.givens = frozenset(givens)


# -- blocking -----------------------------------------------------------------


def _pair(first: str, second: str) -> Pair:
    return (first, second) if first < second else (second, first)


def _raw_keys(node: _Node, index: Mapping[str, _Node]) -> set[BlockingKey]:
    """The keys a mention would be blocked on (BlockingKey)."""
    if node.kind == HUMAN:
        return {
            ("key", given, surname)
            for surname, givens in node.keys.items()
            for given in givens
        } | {("lone", token) for token in node.lone}
    if node.kind == PLACE_KIND:
        return {("token", token) for token in node.place_keys}
    if node.kind == ORGANIZATION:
        return {
            ("token", token) for tokens in node.org_sets for token in tokens
        }
    if node.kind in EVENT_KINDS and node.participants:
        return {
            ("key", given, surname)
            for person_id in node.participants
            for surname, givens in index[person_id].keys.items()
            for given in givens
        }
    return {("name", name) for name in node.label_keys}


def _hubs(
    nodes: Sequence[_Node], index: Mapping[str, _Node], limit: int
) -> tuple[int, int]:
    """Give each mention the keys it is blocked on: its own, but for the
    keys more than `limit` mentions of its kind share (the hubs). Returns
    the number of hub keys, and of the mentions that lost one."""
    by_kind: dict[str, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_kind[node.kind].append(node)
    hub_keys = held = 0
    for group in by_kind.values():
        own = {node.id: _raw_keys(node, index) for node in group}
        counts = Counter(key for keys in own.values() for key in keys)
        hubs = {key for key, count in counts.items() if count > limit}
        hub_keys += len(hubs)
        for node in group:
            node.blocking = frozenset(own[node.id] - hubs)
            held += bool(own[node.id] & hubs)
    return hub_keys, held


def _blocking(node: _Node, tag: str) -> list[BlockingKey]:
    return sorted(key for key in node.blocking if key[0] == tag)


def _blocking_keys(node: _Node) -> dict[str, frozenset[str]]:
    return _key_map(
        (given, surname) for _, given, surname in _blocking(node, "key")
    )


class _GivenIndex:
    """Keys (given name, surname) of nodes, looked up by a surname and a
    given name one of which starts with the other: each lookup costs the
    length of the name and the matches it finds, never a scan of every
    key."""

    def __init__(self) -> None:
        self._givens: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._sorted: dict[str, list[str]] = {}

    def add(self, node_id: str, keys: Mapping[str, Iterable[str]]) -> None:
        for surname, givens in keys.items():
            for given in givens:
                self._givens[surname][given].append(node_id)
        self._sorted = {}

    def find(self, given: str, surname: str) -> Iterator[str]:
        givens = self._givens.get(surname)
        if not givens:
            return
        # the given names this one starts with, itself included
        for end in range(1, len(given) + 1):
            yield from givens.get(given[:end], ())
        # and the longer ones that start with it
        ordered = self._sorted.get(surname)
        if ordered is None:
            ordered = self._sorted[surname] = sorted(givens)
        for other in ordered[bisect.bisect_right(ordered, given) :]:
            if not other.startswith(given):
                break
            yield from givens[other]


class _Matcher(Protocol):
    def find(self, node: _Node) -> set[str]: ...


class _People:
    """People by their keys; with `lone`, a person of one name token finds
    the people whose given names are compatible with it."""

    def __init__(self, nodes: Sequence[_Node], *, lone: bool) -> None:
        self._keys = _GivenIndex()
        self._lone = lone
        self._givens = _GivenIndex()
        for node in nodes:
            self._keys.add(node.id, _blocking_keys(node))
            if lone:
                self._givens.add(node.id, {"": node.givens})

    def find(self, node: _Node) -> set[str]:
        keys = _blocking_keys(node)
        if keys:
            found = {
                other
                for surname, givens in keys.items()
                for given in givens
                for other in self._keys.find(given, surname)
            }
        elif self._lone and node.source != GEDCOM:
            found = {
                other
                for _, token in _blocking(node, "lone")
                for other in self._givens.find(token, "")
            }
        else:
            found = set()
        found.discard(node.id)
        return found


class _Tokens:
    """Nodes by the blocking keys of one tag they must share one of."""

    def __init__(self, nodes: Sequence[_Node], tag: str) -> None:
        self._tag = tag
        self._buckets: dict[BlockingKey, list[str]] = defaultdict(list)
        for node in nodes:
            for key in _blocking(node, tag):
                self._buckets[key].append(node.id)

    def find(self, node: _Node) -> set[str]:
        found = {
            other
            for key in _blocking(node, self._tag)
            for other in self._buckets.get(key, ())
        }
        found.discard(node.id)
        return found


class _Organizations:
    """Organizations one of whose distinctive token sets holds all of the
    other's. The sets that hold every token of a set hold its rarest one,
    so a lookup reads one small bucket, never every organization that
    shares a token; of two sets one of which holds the other, the smaller
    one's lookup finds the pair."""

    def __init__(self, nodes: Sequence[_Node]) -> None:
        self._nodes = {node.id: node for node in nodes}
        self._count = Counter(
            key[1] for node in nodes for key in _blocking(node, "token")
        )
        self._holding: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for _, token in _blocking(node, "token"):
                self._holding[token].append(node.id)

    def find(self, node: _Node) -> set[str]:
        usable = {key[1] for key in _blocking(node, "token")}
        found: set[str] = set()
        for tokens in node.org_sets:
            if not tokens & usable:
                continue
            rarest = min(
                tokens & usable,
                key=lambda token: (self._count[token], token),
            )
            for other in self._holding.get(rarest, ()):
                if any(
                    tokens <= theirs for theirs in self._nodes[other].org_sets
                ):
                    found.add(other)
        found.discard(node.id)
        return found


class _Events:
    """Events of one kind whose participants' keys match; an event without
    participants, by its whole name, among those without."""

    def __init__(self, nodes: Sequence[_Node]) -> None:
        self._keys = _GivenIndex()
        for node in nodes:
            if node.participants:
                self._keys.add(node.id, _blocking_keys(node))
        self._named = _Tokens(
            [node for node in nodes if not node.participants], "name"
        )

    def find(self, node: _Node) -> set[str]:
        if not node.participants:
            return self._named.find(node)
        found = {
            other
            for surname, givens in _blocking_keys(node).items()
            for given in givens
            for other in self._keys.find(given, surname)
        }
        found.discard(node.id)
        return found


def _matcher(kind: str) -> Callable[[Sequence[_Node], bool], _Matcher]:
    """How mentions of a kind (other than an event's) find their
    candidates: (the nodes to index, whether they are GEDCOM mentions)
    -> the matcher."""
    if kind == HUMAN:
        return lambda nodes, gedcom: _People(nodes, lone=gedcom)
    if kind == PLACE_KIND:
        return lambda nodes, gedcom: _Tokens(nodes, "token")
    if kind == ORGANIZATION:
        return lambda nodes, gedcom: _Organizations(nodes)
    return lambda nodes, gedcom: _Tokens(nodes, "name")


def _within(nodes: Sequence[_Node], matcher: _Matcher) -> set[Pair]:
    return {
        _pair(node.id, other) for node in nodes for other in matcher.find(node)
    }


def _block_kind(
    nodes: Sequence[_Node],
    make: Callable[[Sequence[_Node], bool], _Matcher],
    anchored: bool,
) -> tuple[set[Pair], dict[str, frozenset[str]]]:
    """The blocked pairs of the mentions of one kind, and each GEDCOM
    anchor's mentions: with `anchored`, a mention that finds GEDCOM
    mentions pairs with them only, and the others among themselves."""
    if not anchored:
        return _within(nodes, make(nodes, False)), {}
    gedcom = [node for node in nodes if node.source == GEDCOM]
    others = [node for node in nodes if node.source != GEDCOM]
    pairs: set[Pair] = set()
    anchors: dict[str, frozenset[str]] = {}
    if gedcom:
        roster = make(gedcom, True)
        pairs |= _within(gedcom, roster)
        for node in others:
            found = roster.find(node)
            if found:
                anchors[node.id] = frozenset(found)
                pairs.update(_pair(node.id, other) for other in found)
    free = [node for node in others if node.id not in anchors]
    return pairs | _within(free, make(free, False)), anchors


def _block(
    nodes: Sequence[_Node], config: ClusteringConfig
) -> tuple[set[Pair], dict[str, frozenset[str]]]:
    """Every blocked pair of the mentions to cluster, and the GEDCOM
    anchors each anchored mention found."""
    by_kind: dict[str, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_kind[node.kind].append(node)
    pairs: set[Pair] = set()
    anchors: dict[str, frozenset[str]] = {}
    for kind, group in sorted(by_kind.items()):
        if kind in EVENT_KINDS:
            # events meet through their participants, an export's death
            # events among them, and never through an anchor
            pairs |= _within(group, _Events(group))
            continue
        found, anchored = _block_kind(
            group, _matcher(kind), config.blocking.gedcom_anchored
        )
        pairs |= found
        anchors.update(anchored)
    return pairs, anchors


# -- constraints --------------------------------------------------------------


def _facts(node: _Node) -> PairFacts:
    """The impossibility filter's input, lifted: the mention's fingerprint
    stands in for a QID, and its kin are the mentions its manifest relates
    it to."""
    return PairFacts(
        kind=node.kind, label=node.label, qid=node.id, kin_qids=node.kin_ids
    )


def _containment(nodes: Sequence[_Node]) -> PlaceContainment:
    """Transitive place containment between the manifests' own places, in
    mention space."""
    index = {node.id: node for node in nodes}
    ancestors: dict[str, set[str]] = {}
    for node in nodes:
        if node.kind != PLACE_KIND or not node.containers:
            continue
        seen: set[str] = set()
        stack = list(node.containers)
        while stack:
            other = stack.pop()
            if other in seen or other == node.id:
                continue
            seen.add(other)
            if other in index:
                stack.extend(index[other].containers)
        ancestors[node.id] = seen
    labels = {node.id: node.label for node in nodes if node.kind == PLACE_KIND}
    return PlaceContainment(ancestors, labels)


def _apart(first: _Node, second: _Node) -> bool:
    """Whether one manifest keeps the two apart (soft cannot-link)."""
    return first.sha1 == second.sha1 and first.exclusive and second.exclusive


# -- evidence -----------------------------------------------------------------


class _Judge:
    """The deterministic evidence for a pair, and the link it makes."""

    def __init__(
        self,
        nodes: Mapping[str, _Node],
        anchors: Mapping[str, frozenset[str]],
        config: ClusteringConfig,
    ) -> None:
        self._nodes = nodes
        self._anchors = anchors
        self._max_anchors = config.confident.max_anchors
        self._widest = config.evidence.max_corroborating_years
        self._counted: dict[str, int] = {}

    def life(self, a: _Node, b: _Node) -> str:
        """Whether two people's births and deaths, each compared with
        each, agree, conflict or say nothing."""
        return _combine_dates(
            _dates(a.births, b.births, self._widest),
            _dates(a.deaths, b.deaths, self._widest),
        )

    def name_level(self, a: _Node, b: _Node) -> str:
        if a.named_keys & b.named_keys:
            return "exact"
        if a.kind == HUMAN and keys_match(a.keys, b.keys):
            return "key"
        if a.kind == PLACE_KIND and a.place_keys & b.place_keys:
            nested = any(
                one <= other or other <= one
                for one in a.place_parts
                for other in b.place_parts
            )
            return "nested" if nested else "key"
        if a.kind == ORGANIZATION and any(
            one <= other or other <= one
            for one in a.org_sets
            for other in b.org_sets
        ):
            return "key"
        return "none"

    def _kin_pairs(self, a: _Node, b: _Node) -> Iterator[tuple[_Node, _Node]]:
        """Each kin of `a` and kin of `b` in one relation to them."""
        for relation, first_id in sorted(a.kin):
            first = self._nodes.get(first_id)
            if first is None:
                continue
            for other_relation, second_id in sorted(b.kin):
                second = self._nodes.get(second_id)
                if second is not None and relation == other_relation:
                    yield first, second

    def kin_agree(self, a: _Node, b: _Node) -> bool:
        """Whether a kin of each, in one relation, is named alike (by more
        than a surname), without dates that conflict."""
        return any(
            (
                first.named_keys & second.named_keys
                or keys_match(first.keys, second.keys)
            )
            and self.life(first, second) != "conflict"
            for first, second in self._kin_pairs(a, b)
        )

    def kin_conflict(self, a: _Node, b: _Node) -> bool:
        """Whether kin of one relation and one name give dates that
        conflict, while no kin agree."""
        return not self.kin_agree(a, b) and any(
            first.named_keys & second.named_keys
            and self.life(first, second) == "conflict"
            for first, second in self._kin_pairs(a, b)
        )

    def evidence(self, a: _Node, b: _Node) -> tuple[str, str, str]:
        """(name, dates, kin): how the names match (exact, nested, key or
        none), whether the dates agree, conflict or say nothing, and
        whether kin agree."""
        if a.kind in EVENT_KINDS:
            best = "none"
            for first_id in sorted(a.participants):
                for second_id in sorted(b.participants):
                    level = self.name_level(
                        self._nodes[first_id], self._nodes[second_id]
                    )
                    if level == "exact" or (level == "key" and best == "none"):
                        best = level
            return best, _dates(a.when, b.when, self._widest), "unknown"
        return (
            self.name_level(a, b),
            self.life(a, b),
            "agree" if self.kin_agree(a, b) else "unknown",
        )

    def _anchors_counted(self, node: _Node) -> int:
        """The GEDCOM anchors a mention matched: for a place, those it
        matches exactly, and those it nests in as one."""
        if node.id not in self._counted:
            found = sorted(self._anchors.get(node.id, ()))
            if node.kind == PLACE_KIND:
                levels = [
                    self.name_level(node, self._nodes[anchor])
                    for anchor in found
                ]
                count = levels.count("exact") + ("nested" in levels)
            else:
                count = len(found)
            self._counted[node.id] = count
        return self._counted[node.id]

    def link(self, a: _Node, b: _Node) -> tuple[str, str]:
        """(the link, the evidence as name/dates/kin) of a candidate pair
        of any kind but an event's."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict" or not a.kind:
            return AMBIGUOUS, described
        if any(
            self._anchors_counted(node) > self._max_anchors for node in (a, b)
        ):
            return AMBIGUOUS, described
        if a.kind == HUMAN:
            corroborated = kin == "agree" or dates == "agree"
            confident = (
                name in ("exact", "key")
                and corroborated
                and not self.kin_conflict(a, b)
            )
        elif a.kind == PLACE_KIND:
            confident = name in ("exact", "nested")
        elif a.kind in (ORGANIZATION, FAMILY):
            confident = any(
                len(_distinctive(shared)) >= 2
                for shared in a.named_keys & b.named_keys
            )
        else:
            confident = name == "exact"
        return (CONFIDENT if confident else AMBIGUOUS), described

    def event_link(
        self, a: _Node, b: _Node, merged: Callable[[str, str], bool]
    ) -> tuple[str, str]:
        """(the link, the evidence) of a candidate pair of events, given
        whether two people ended merged: confident when every participant
        of the event with fewer is merged with one of the other's (so two
        births that name one father stay apart), and, but for a birth or
        a death, their dates agree."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict":
            return AMBIGUOUS, described
        if a.kind not in ONE_PER_PERSON and dates != "agree":
            return AMBIGUOUS, described
        fewer, more = sorted(
            (sorted(a.participants), sorted(b.participants)), key=len
        )
        if fewer and all(
            any(merged(first, second) for second in more) for first in fewer
        ):
            return CONFIDENT, described
        return AMBIGUOUS, described

    def contradicts(self, a: _Node, b: _Node) -> str | None:
        """The evidence of two members of one cluster, when it says they
        differ (dates that conflict; for people, kin that conflict or no
        name match; for places, names neither exact nor nested; for other
        kinds but events, no name match); None when it does not."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict":
            return described
        if a.kind in EVENT_KINDS:
            return None
        if a.kind == PLACE_KIND:
            return None if name in ("exact", "nested") else described
        if a.kind == HUMAN and self.kin_conflict(a, b):
            return described
        return described if name == "none" else None

    def _signature(self, node: _Node) -> tuple:
        """Everything the evidence of a pair reads of one of its members:
        two members of one signature give every pair the same evidence."""

        def keys(of: _Node) -> tuple:
            return tuple(
                sorted(
                    (surname, tuple(sorted(g)))
                    for surname, g in of.keys.items()
                )
            )

        def dates(intervals: list[Interval]) -> tuple:
            return tuple(sorted((i.start, i.end) for i in intervals))

        return (
            node.named_keys,
            keys(node),
            node.place_keys,
            frozenset(node.place_parts),
            frozenset(node.org_sets),
            dates(node.births),
            dates(node.deaths),
            dates(node.when),
            frozenset(
                (
                    relation,
                    kin.named_keys,
                    keys(kin),
                    dates(kin.births),
                    dates(kin.deaths),
                )
                for relation, kin_id in node.kin
                if (kin := self._nodes.get(kin_id)) is not None
            ),
            frozenset(
                (person.named_keys, keys(person))
                for person_id in node.participants
                if (person := self._nodes.get(person_id)) is not None
            ),
        )

    def contradiction(self, members: Sequence[str]) -> dict[str, Any] | None:
        """The first two members, in order, whose evidence says they
        differ. Members of one signature pair alike with every other, so
        each signature is judged by its first member only. Two of one
        signature never differ: their names match, as any member's must
        to be blocked at all, and their dates overlap. Nor does a
        confident link."""
        firsts: dict[tuple, str] = {}
        for member in sorted(members):
            firsts.setdefault(self._signature(self._nodes[member]), member)
        ordered = sorted(firsts.values())
        for position, first in enumerate(ordered):
            for second in ordered[position + 1 :]:
                found = self.contradicts(
                    self._nodes[first], self._nodes[second]
                )
                if found is not None:
                    return {"mentions": [first, second], "evidence": found}
        return None


# -- closure ------------------------------------------------------------------


class _Components:
    """Transitive closure over pairs (union-find), singletons included."""

    def __init__(self, ids: Iterable[str]) -> None:
        self._parent = {node_id: node_id for node_id in ids}

    def find(self, node_id: str) -> str:
        parent = self._parent
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(self, first: str, second: str) -> None:
        one, other = self.find(first), self.find(second)
        if one != other:
            self._parent[max(one, other)] = min(one, other)

    def groups(self) -> list[list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for node_id in self._parent:
            grouped[self.find(node_id)].append(node_id)
        return sorted(sorted(members) for members in grouped.values())


def _components(ids: Iterable[str], pairs: Iterable[Pair]) -> _Components:
    components = _Components(ids)
    for first, second in pairs:
        components.union(first, second)
    return components


class _Conflicts:
    """Whether a cluster joins a cannot-link pair, and how."""

    def __init__(
        self, nodes: Mapping[str, _Node], containment: PlaceContainment
    ) -> None:
        self._nodes = nodes
        self._containment = containment

    def _impossible(self, first: _Node, second: _Node) -> bool:
        return (
            impossible_pair(
                _facts(first), _facts(second), containment=self._containment
            )
            is not None
        )

    def find(self, members: Sequence[str]) -> tuple[str, Pair] | None:
        """(hard or soft, the pair that makes it so) for a conflicting
        cluster, None otherwise: hard when the impossibility filter rules
        two members out (a kin edge, or a member named like a place
        containing another), soft when they are only two ids of one
        manifest."""
        inside = set(members)
        nodes = [self._nodes[member] for member in sorted(members)]
        places: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            if node.kind == PLACE_KIND:
                places[node.label].append(node.id)
        for node in nodes:
            related = node.kin_ids & inside
            related.update(
                other
                for label in self._containment.container_labels(node.id)
                for other in places.get(label, ())
            )
            for other_id in sorted(related - {node.id}):
                if self._impossible(node, self._nodes[other_id]):
                    return HARD, _pair(node.id, other_id)
        first_of: dict[str, str] = {}
        for node in nodes:
            if not node.exclusive:
                continue
            if node.sha1 in first_of:
                return SOFT, _pair(first_of[node.sha1], node.id)
            first_of[node.sha1] = node.id
        return None


@dataclass(frozen=True)
class _Settled:
    """A candidate cluster: its members, its status, its pairs, and the
    pair that makes it conflict, or the pair never linked that
    contradicts."""

    members: list[str]
    status: str
    pairs: list[Pair]
    conflict: tuple[str, Pair] | None = None
    contradiction: dict[str, Any] | None = None


def _settle(
    ids: Iterable[str],
    links: Mapping[Pair, tuple[str, str]],
    judge: _Judge,
    conflicts: _Conflicts,
) -> list[_Settled]:
    """The candidate clusters the links join among `ids`, each settled
    (the module docstring says how)."""
    candidates = _components(ids, links)
    pairs_of: dict[str, list[Pair]] = defaultdict(list)
    for pair in links:
        pairs_of[candidates.find(pair[0])].append(pair)
    settled = []
    for members in candidates.groups():
        if len(members) == 1:
            settled.append(_Settled(members, SINGLETON, []))
            continue
        own = sorted(pairs_of[candidates.find(members[0])])
        conflict = conflicts.find(members)
        if conflict is not None:
            settled.append(_Settled(members, CONFLICTING, own, conflict))
        elif any(links[pair][0] == AMBIGUOUS for pair in own):
            settled.append(_Settled(members, AMBIGUOUS, own))
        else:
            contradiction = judge.contradiction(members)
            status = AMBIGUOUS if contradiction else CONFIDENT
            settled.append(
                _Settled(members, status, own, contradiction=contradiction)
            )
    return settled


# -- the identity map ---------------------------------------------------------


def adjudication_calls(groups: Iterable[int], per_call: int) -> int:
    """The calls adjudication makes of groups of pairs this large, at
    `per_call` pairs a call: a group's pairs stay together in one call, a
    group larger than a call is split into calls' worth first, and calls
    are filled in order."""
    calls = count = 0
    for size in groups:
        for start in range(0, size, per_call):
            chunk = min(per_call, size - start)
            if count and count + chunk > per_call:
                calls += 1
                count = 0
            count += chunk
    return calls + (1 if count else 0)


def identity_hash(mentions: Mapping[str, Mapping[str, Any]]) -> str:
    """The SHA-256 of every mention's cluster and method, and nothing
    else."""
    return sha256_text(
        canonical_json(
            {
                fingerprint: [entry["cluster"], entry["method"]]
                for fingerprint, entry in mentions.items()
            }
        )
    )


def batch_hash(batch: ClaimBatch) -> str:
    """A batch's canonical content hash, its run stamps left out, as the
    orchestrator records its view."""
    return content_hash(batch.model_dump(mode="json"), volatile=BATCH_VOLATILE)


def code_digest() -> str:
    """The SHA-256 of the source of CODE, the modules that make the
    identity map, as the package holds it."""
    return sha256_text(
        canonical_json(
            {
                name: hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest()
                for name in CODE
            }
        )
    )


def _distinct(manifests: Iterable[Manifest]) -> list[Manifest]:
    """One manifest per document, in SHA-1 order: two manifests of one
    SHA-1 are one document, and the first is taken."""
    taken: dict[str, Manifest] = {}
    for manifest in manifests:
        taken.setdefault(manifest.sha1.lower(), manifest)
    return [taken[sha1] for sha1 in sorted(taken)]


def inputs_key(manifests: Sequence[Manifest], config: ClusteringConfig) -> str:
    """The key an identity map is reused under: VERSION, the digest of
    CODE's source, the thresholds' digest, and each document's SHA-1,
    source and batch content."""
    return sha256_text(
        canonical_json(
            {
                "stage": STAGE,
                "version": VERSION,
                "code": code_digest(),
                "config": config.digest(),
                "documents": [
                    [
                        manifest.sha1.lower(),
                        manifest.source,
                        batch_hash(manifest.batch),
                    ]
                    for manifest in _distinct(manifests)
                ],
            }
        )
    )


@dataclass(frozen=True)
class _Corpus:
    """The manifests, one per document in SHA-1 order, and their mentions
    lifted and keyed: every mention by fingerprint, those to cluster, and
    the hub keys none of them is blocked on."""

    manifests: list[Manifest]
    nodes: list[_Node]
    index: dict[str, _Node]
    clustered: list[_Node]
    hub_keys: int
    hub_mentions: int


def _corpus(
    manifests: Sequence[Manifest], config: ClusteringConfig
) -> _Corpus:
    ordered = _distinct(manifests)
    nodes = [
        node
        for manifest in ordered
        for node in _nodes(manifest, config.evidence.circa_years)
    ]
    _derive(nodes, config)
    index = {node.id: node for node in nodes}
    clustered = [node for node in nodes if node.mention.extract]
    hub_keys, hub_mentions = _hubs(
        clustered, index, config.blocking.max_key_mentions
    )
    return _Corpus(
        manifests=ordered,
        nodes=nodes,
        index=index,
        clustered=clustered,
        hub_keys=hub_keys,
        hub_mentions=hub_mentions,
    )


def blocked_pairs(
    manifests: Sequence[Manifest], config: ClusteringConfig | None = None
) -> set[frozenset[tuple[str, str]]]:
    """Every pair blocking proposes, before the cannot-link pairs and the
    pairs the impossibility filter rules out are taken out: each as the
    (document SHA-1, local id) of its two mentions."""
    config = config or load_config()
    corpus = _corpus(manifests, config)
    blocked, _anchors = _block(corpus.clustered, config)
    named = {
        node.id: (node.sha1, node.mention.local_id) for node in corpus.nodes
    }
    return {
        frozenset((named[first], named[second])) for first, second in blocked
    }


def cluster_corpus(
    manifests: Sequence[Manifest], config: ClusteringConfig | None = None
) -> dict[str, Any]:
    """The identity map of the manifests under the thresholds (by default
    clustering.yaml's), as identity-map.json holds it: the module
    docstring says how it is made and what it holds. Two manifests of one
    SHA-1 are one document: the first is taken."""
    config = config or load_config()
    corpus = _corpus(manifests, config)
    ordered, nodes = corpus.manifests, corpus.nodes
    index, clustered = corpus.index, corpus.clustered
    containment = _containment(nodes)

    blocked, anchors = _block(clustered, config)
    judge = _Judge(index, anchors, config)
    conflicts = _Conflicts(index, containment)
    within: set[Pair] = set()
    pruned: Counter[str] = Counter()
    within_related = 0
    candidates: list[Pair] = []
    for pair in sorted(blocked):
        first, second = index[pair[0]], index[pair[1]]
        reason = impossible_pair(
            _facts(first), _facts(second), containment=containment
        )
        if _apart(first, second):
            within.add(pair)
            within_related += reason is not None
        elif reason is not None:
            pruned[reason] += 1
        else:
            candidates.append(pair)

    # everything but events first: an event's link reads the clusters its
    # participants end in
    links = {
        pair: judge.link(index[pair[0]], index[pair[1]])
        for pair in candidates
        if index[pair[0]].kind not in EVENT_KINDS
    }
    settled = _settle(
        (node.id for node in clustered if node.kind not in EVENT_KINDS),
        links,
        judge,
        conflicts,
    )
    together = {
        member: position
        for position, entry in enumerate(settled)
        if entry.status == CONFIDENT
        for member in entry.members
    }

    def merged(first: str, second: str) -> bool:
        return first in together and together[first] == together.get(second)

    event_links = {
        pair: judge.event_link(index[pair[0]], index[pair[1]], merged)
        for pair in candidates
        if index[pair[0]].kind in EVENT_KINDS
    }
    settled += _settle(
        (node.id for node in clustered if node.kind in EVENT_KINDS),
        event_links,
        judge,
        conflicts,
    )
    links.update(event_links)

    clusters: dict[str, dict[str, Any]] = {}
    assigned: dict[str, str] = {}
    status: dict[str, str] = {}
    review: list[dict[str, Any]] = []
    ambiguous_groups: list[int] = []
    all_groups: list[int] = []
    for entry in sorted(settled, key=lambda entry: entry.members):
        merged_whole = entry.status == CONFIDENT
        for part in (
            [entry.members] if merged_whole else [[m] for m in entry.members]
        ):
            cid = cluster_id(part)
            clusters[cid] = {
                "kind": index[part[0]].kind,
                "method": entry.status,
                "members": part,
            }
            for member in part:
                assigned[member] = cid
                status[member] = entry.status
        if entry.status == SINGLETON:
            continue
        ambiguous = [
            pair for pair in entry.pairs if links[pair][0] == AMBIGUOUS
        ]
        all_groups.append(len(entry.pairs))
        if ambiguous:
            ambiguous_groups.append(len(ambiguous))
        if entry.status == CONFIDENT:
            continue
        review.append(
            {
                "status": entry.status,
                "kind": index[entry.members[0]].kind,
                "members": entry.members,
                "pairs": [
                    {
                        "mentions": list(pair),
                        "link": links[pair][0],
                        "evidence": links[pair][1],
                    }
                    for pair in entry.pairs
                ],
                "conflict": (
                    {
                        "how": entry.conflict[0],
                        "mentions": list(entry.conflict[1]),
                    }
                    if entry.conflict
                    else None
                ),
                "contradiction": entry.contradiction,
            }
        )
    for node in nodes:
        if not node.mention.extract:
            cid = cluster_id([node.id])
            clusters[cid] = {
                "kind": node.kind,
                "method": UNCLUSTERED,
                "members": [node.id],
            }
            assigned[node.id] = cid
            status[node.id] = UNCLUSTERED

    mentions: dict[str, dict[str, Any]] = {}
    for node in sorted(nodes, key=lambda node: node.id):
        mention = node.mention
        mentions[node.id] = {
            "cluster": assigned[node.id],
            "method": status[node.id],
            "document": mention.artifact,
            "local_id": mention.local_id,
            "label": mention.label,
            "kind": mention.kind,
            "span": list(mention.span),
            "occurrence": mention.occurrence,
        }

    # what merging the confident links alone would give, for the summary:
    # their closure, and how many of its clusters join a cannot-link pair
    confident_links = [
        pair for pair, (link, _evidence) in links.items() if link == CONFIDENT
    ]
    linked = _components((node.id for node in clustered), confident_links)
    linked_clusters = [m for m in linked.groups() if len(m) > 1]
    linked_conflicting = [m for m in linked_clusters if conflicts.find(m)]

    candidate_groups = [entry for entry in settled if len(entry.members) > 1]
    by_status = Counter(entry.status for entry in candidate_groups)
    mentions_by_status = Counter(
        entry.status for entry in settled for _ in entry.members
    )
    per_call = config.adjudication.pairs_per_call
    summary = {
        "documents": len(ordered),
        "gedcom_documents": sum(m.source == GEDCOM for m in ordered),
        "documents_without_manifest": sum(
            not m.batch.entities for m in ordered
        ),
        "mentions": len(nodes),
        "clustered_mentions": len(clustered),
        "unclustered_mentions": len(nodes) - len(clustered),
        "clustered_by_kind": dict(
            sorted(Counter(node.kind for node in clustered).items())
        ),
        "hub_keys": corpus.hub_keys,
        "hub_mentions": corpus.hub_mentions,
        "blocked_pairs": len(blocked),
        "blocked_by_kind": dict(
            sorted(Counter(index[pair[0]].kind for pair in blocked).items())
        ),
        "within_manifest_pairs": len(within),
        "within_manifest_kin_or_containment": within_related,
        "pruned_pairs": dict(sorted(pruned.items())),
        "candidate_pairs": len(links),
        "confident_pairs": len(confident_links),
        "ambiguous_pairs": len(links) - len(confident_links),
        "anchored_mentions": len(anchors),
        "multi_anchored_mentions": sum(
            len(found) > config.confident.max_anchors
            for found in anchors.values()
        ),
        "clusters": {
            verdict: by_status[verdict]
            for verdict in (CONFIDENT, AMBIGUOUS, CONFLICTING)
        },
        "cluster_mentions": {
            verdict: mentions_by_status[verdict]
            for verdict in (CONFIDENT, AMBIGUOUS, CONFLICTING)
        },
        "conflicting_hard": sum(
            entry["conflict"]["how"] == HARD
            for entry in review
            if entry["conflict"]
        ),
        "contradicted_clusters": sum(
            entry.contradiction is not None for entry in candidate_groups
        ),
        "singletons": mentions_by_status[SINGLETON],
        "largest_candidate_cluster": max(
            (len(entry.members) for entry in settled), default=0
        ),
        "identity_clusters": len(clusters),
        "merged_mentions": mentions_by_status[CONFIDENT],
        "confident_links_only": {
            "clusters": len(linked_clusters),
            "mentions": sum(map(len, linked_clusters)),
            "conflicting": len(linked_conflicting),
        },
        "adjudication": {
            "pairs_per_call": per_call,
            "calls_ambiguous_pairs": adjudication_calls(
                ambiguous_groups, per_call
            ),
            "calls_all_pairs": adjudication_calls(all_groups, per_call),
        },
    }
    return {
        "format": FORMAT,
        "stage": STAGE,
        "version": VERSION,
        "inputs": inputs_key(ordered, config),
        "config": config.digest(),
        "hash": identity_hash(mentions),
        "mentions": mentions,
        "clusters": dict(sorted(clusters.items())),
        "review": review,
        "summary": summary,
    }


# -- the stage ----------------------------------------------------------------


class ClusteringInputError(RuntimeError):
    """A document's batch cannot be read: the message names the document
    by the start of its SHA-1, never by its file's name."""


class _Document(Protocol):
    """What the stage reads of a document (orchestrate.DocumentRun)."""

    @property
    def sha1(self) -> str: ...

    @property
    def kind(self) -> str: ...

    batch: Path


def read_manifests(documents: Iterable[_Document]) -> list[Manifest]:
    """The manifest of each document: its batch view, as the orchestrator
    wrote it, with the SHA-1 of its media. Raises ClusteringInputError for
    a batch that is missing or cannot be read."""
    manifests = []
    for doc in documents:
        sha1 = doc.sha1
        try:
            batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        except (OSError, ValueError) as exc:
            raise ClusteringInputError(
                f"the batch of document {sha1[:12]} cannot be read "
                f"({type(exc).__name__})"
            ) from exc
        manifests.append(
            Manifest(
                sha1=sha1,
                batch=batch,
                source=GEDCOM if doc.kind == GEDCOM else DOCUMENT,
            )
        )
    return manifests


# the sections an identity map of FORMAT holds, and what each is
_SECTIONS = {
    "mentions": dict,
    "clusters": dict,
    "review": list,
    "summary": dict,
}


def read_identity_map(path: Path) -> dict[str, Any] | None:
    """The identity map at `path`, or None when there is none, when it
    cannot be read, when it is of another format or lacks a section of
    its format, or when its decisions do not hash to its hash."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    if not all(
        isinstance(data.get(name), kind) for name, kind in _SECTIONS.items()
    ):
        return None
    try:
        if identity_hash(data["mentions"]) != data.get("hash"):
            return None
    except (AttributeError, KeyError, TypeError):
        return None
    return data


@dataclass(frozen=True)
class StageOutput:
    """The identity map the stage holds, its path, and whether it was
    reused rather than made."""

    identity_map: dict[str, Any]
    path: Path
    reused: bool


def ensure_identity_map(
    directory: Path,
    documents: Iterable[_Document],
    config: ClusteringConfig | None = None,
) -> StageOutput:
    """The identity map of the documents, in `directory`: the one there
    when it was made from the same inputs (inputs_key), or one made now and
    written there whole."""
    config = config or load_config()
    manifests = read_manifests(documents)
    path = directory / IDENTITY_MAP
    stored = read_identity_map(path)
    if stored is not None and stored.get("inputs") == inputs_key(
        manifests, config
    ):
        return StageOutput(stored, path, True)
    identity = cluster_corpus(manifests, config)
    atomic_write_text(
        path, json.dumps(identity, ensure_ascii=False, indent=1) + "\n"
    )
    return StageOutput(identity, path, False)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def attention(summary: Mapping[str, Any]) -> list[str]:
    """The reasons an identity map needs attention, from its summary:
    counts within labels of its own."""
    clusters = summary["clusters"]
    mentions = summary["cluster_mentions"]
    reasons = []
    if clusters[AMBIGUOUS]:
        reasons.append(
            "ambiguous clusters not merged: "
            f"{_counted(clusters[AMBIGUOUS], 'cluster')} of "
            f"{_counted(mentions[AMBIGUOUS], 'mention')}"
        )
    if clusters[CONFLICTING]:
        reasons.append(
            "conflicting clusters not merged: "
            f"{_counted(clusters[CONFLICTING], 'cluster')} of "
            f"{_counted(mentions[CONFLICTING], 'mention')}, "
            f"{summary['conflicting_hard']} with a kin edge or a "
            "containment inside"
        )
    if summary["ambiguous_pairs"]:
        adjudication = summary["adjudication"]
        reasons.append(
            "ambiguous pairs to adjudicate: "
            f"{_counted(summary['ambiguous_pairs'], 'pair')}, about "
            f"{_counted(adjudication['calls_ambiguous_pairs'], 'call')} "
            f"of up to {adjudication['pairs_per_call']} pairs"
        )
    if summary["hub_keys"]:
        reasons.append(
            "hub keys not blocked on: "
            f"{_counted(summary['hub_keys'], 'key')}, "
            f"{_counted(summary['hub_mentions'], 'mention')} kept apart"
        )
    return reasons


def run_stage(context: Any) -> list[str]:
    """The corpus stage: the identity map of the context's documents in
    its directory, made or reused (ensure_identity_map). Returns the
    reasons it needs attention."""
    output = ensure_identity_map(context.directory, context.documents)
    summary = output.identity_map["summary"]
    adjudication = summary["adjudication"]
    context.log(
        f"{STAGE}: identity map "
        + ("reused, its inputs unchanged" if output.reused else "made")
        + f": {_counted(summary['clustered_mentions'], 'mention')} of "
        f"{_counted(summary['documents'], 'document')} in "
        f"{_counted(summary['identity_clusters'], 'cluster')}, "
        f"{_counted(summary['merged_mentions'], 'mention')} merged; "
        f"{summary['clusters'][AMBIGUOUS]} ambiguous and "
        f"{summary['clusters'][CONFLICTING]} conflicting clusters not "
        "merged; adjudication: about "
        f"{_counted(adjudication['calls_ambiguous_pairs'], 'call')} for "
        "the ambiguous pairs, "
        f"{_counted(adjudication['calls_all_pairs'], 'call')} for every "
        "candidate pair"
    )
    return attention(summary)
