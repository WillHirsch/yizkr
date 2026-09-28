"""Corpus clustering: every document's mentions, clustered across the
whole corpus into an identity map. It is the scratch driver's first
corpus stage (ADR-0025), and runs past the barrier over the manifests of
every document whose stages completed.

`cluster_corpus` is a pure function of the manifests and the thresholds
(clustering.yaml beside this module, load_config). It reads nothing but
its arguments: no wiki, no query service, no graph, and no model. Its
output changes only with them and with VERSION, which a change to what it
makes of the same manifests bumps. The stage (`run_stage`) keeps that
output in its directory as `identity-map.json`, and reuses it while the
documents' manifests (by content), VERSION and the thresholds stand.

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
(`part_of`, `location`), typed names and nicknames, and dates. Only what
the manifests assert is used: no containment or kinship is read from the
graph, and none is inferred.

**Blocking** proposes candidate pairs by keys of their own kind, never
by one shared token:

- *People*: a given-name token with a surname token. Name forms are the
  label, the aliases and the typed birth, married and official names;
  accents are folded, titles and kin words dropped, and a name written
  "X Y née Z" gives the forms "X Y" and "X Z". The surname is any later
  token of the same form, or a surname of the person's lifted kin, taken
  with the first token of each form. Nicknames stand in for given names.
  Two keys match on the same surname and given names one of which starts
  with the other ("Hilde", "Hildegard").
- *Places*: each token of `min-place-token` characters or more in the
  part of a name before its first comma. Region and country parts after
  it never key.
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
pairs only with GEDCOM people whose given name is compatible. Blocking
looks keys up in indexes, so its work grows with the mentions and the
pairs it proposes, never with every pair of mentions.

**Cannot-link.** Two ids of one manifest are kept apart (soft): the
manifest already unified what it could. The places and occupations of a
GEDCOM export are exempt, since the adapter makes one per distinct
string, and so are the refs of a batch without a manifest. A blocked pair
the impossibility filter rules out (`reconcile.impossible_pair`,
ADR-0017), given the lifted facts, is pruned (hard): a kin edge between
the two, or one place inside the other. The filter is reused only after
the lift: a manifest's kin are local ids until then, and it would see
none. Dates never prune a pair (ADR-0002, ADR-0017).

**Classification.** Each candidate pair is a confident link or an
ambiguous one, by deterministic evidence alone:

- a person pair is confident when the names match (exactly, or by key)
  and an agreeing kin name or an overlapping date corroborates it;
- a place pair when the names match exactly, or one name's
  comma-separated parts are all among the other's ("Fläckfeld" and
  "Fläckfeld, Kreis Mockburg");
- an event pair when a pair of their participants is confident;
- any other pair when the names match exactly;
- and never when the two give dates that cannot overlap, or when either
  mention matches more than `max-anchors` GEDCOM anchors.

Dates are compared at the precision their source states, as ranges: a
decade is ten years, a circa date is widened by `circa-years`, a range's
bounds widen it, and a bound on one side only leaves the other open. So
a date is never narrowed to be compared (rule 5).

**Clusters.** The closure over every candidate pair gives the candidate
clusters, the units a review takes. A cluster is *conflicting* when it
joins a cannot-link pair: *hard* when the impossibility filter rules the
two out, *soft* when they are only two ids of one manifest. Otherwise it
is *ambiguous* when any of its pairs is, and *confident* when all are.
Only confident clusters merge: their members share one cluster. The
members of ambiguous and conflicting clusters stay apart, each a cluster
of its own, and are counted. A mention with no candidate pair is a
singleton.

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
  members, its pairs (the link and the evidence of each) and, when it
  conflicts, the pair that makes it conflict and how;
- `summary`: counts only, the adjudication-call estimate among them;
- `inputs`: the key it is reused under (VERSION, the thresholds' digest
  and each document's SHA-1 and batch content hash); `config`: the
  thresholds' digest.

The map holds labels, so it stays in the run directory, which is
local-only data. What the stage returns, and so what reaches the run's
events, is counts and labels of its own, never a document's words.
"""

from __future__ import annotations

import bisect
import json
import os
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
# the thresholds shipped with the package
DEFAULTS = files("yizkr.pipeline") / "clustering.yaml"

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
# titles and kin words, which are no given names ("Frau", "Tante")
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
        "ibn", "zur", "auf", "verw", "widow", "witwe", "wwe",
    }
)  # fmt: skip
# a name form "X née Y" carries a second surname
MAIDEN_MARKERS = frozenset({"nee", "geb", "geborene", "born"})
# words no organization is known by
ORG_GENERIC = frozenset(
    {
        "the", "and", "und", "der", "die", "das", "des", "von", "for",
        "company", "firm", "firma", "gmbh", "inc", "ltd", "limited",
        "co", "cie", "bros", "brothers", "sons", "sohn", "sohne",
    }
)  # fmt: skip
# the shortest token an organization's distinctive tokens hold
_MIN_ORG_TOKEN = 3
# an interval's open end
_OPEN = float("inf")

Pair = tuple[str, str]


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


class Evidence(_Settings):
    circa_years: int = Field(ge=0)


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


def fold(text: str) -> str:
    """Case- and accent-insensitive form: "Cäcilie" and "cacilie" are one."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("ß", "ss")


def _folded_tokens(name: str) -> list[str]:
    return [
        token for raw in name.split() if (token := fold(raw.strip(_STRIP)))
    ]


def normal(name: str) -> str:
    """A whole name as its folded tokens, one space apart."""
    return " ".join(_folded_tokens(name))


def _letters(token: str) -> bool:
    """A token of letters, with hyphens or apostrophes inside ("hans-otto")
    but no digit, and no dot inside (initials, "N.N.")."""
    return token.replace("-", "").replace("'", "").isalpha()


def name_forms(name: str, min_token: int) -> list[list[str]]:
    """The significant tokens of one name, as one form, or as two when the
    name carries a maiden surname ("X Y née Z" gives [X, Y] and [X, Z],
    and "X née Z" gives [X] and [X, Z]). A significant token is
    `min_token` letters or more, and no title or kin word."""
    tokens = _folded_tokens(name)
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
    forms = []
    for part in parts:
        significant = [
            token
            for token in part
            if len(token) >= min_token
            and token not in NOT_GIVEN
            and _letters(token)
        ]
        if significant:
            forms.append(significant)
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


# -- dates --------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """The years a stated date covers, at its source's own precision, with
    an open end where only one bound is stated."""

    start: float
    end: float

    def overlaps(self, other: Interval) -> bool:
        return self.start <= other.end and other.start <= self.end


def date_interval(
    value: TimeValue, qualifiers: Iterable[Any], circa_years: int
) -> Interval:
    """The years a claim's date covers: its year at its precision (a
    decade ten years, a century a hundred), widened by the claim's
    earliest and latest dates, open on the side a lone bound leaves open,
    and widened by `circa_years` each way when the source gives it as
    circa. Never narrower than what the source says."""
    span = {"century": 99, "decade": 9}.get(value.precision or "", 0)
    start: float = value.year
    end: float = value.year + span
    earliest = [
        q.value.year
        for q in qualifiers
        if q.property == "earliest_date" and isinstance(q.value, TimeValue)
    ]
    latest = [
        q.value.year + {"century": 99, "decade": 9}.get(q.value.precision, 0)
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
    # computed once every node of the corpus is lifted (_derive)
    forms: list[list[str]] = field(default_factory=list)
    keys: dict[str, frozenset[str]] = field(default_factory=dict)
    lone: frozenset[str] = frozenset()
    givens: frozenset[str] = frozenset()
    place_keys: frozenset[str] = frozenset()
    place_parts: tuple[frozenset[str], ...] = ()
    org_sets: tuple[frozenset[str], ...] = ()
    label_keys: frozenset[str] = frozenset()

    @property
    def id(self) -> str:
        return self.mention.fingerprint

    @property
    def sha1(self) -> str:
        return self.mention.artifact

    @property
    def kind(self) -> str:
        return self.mention.kind

    @property
    def label(self) -> str:
        return self.mention.label

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
        # a birth or death event's date is its participant's
        if node.kind in ("birth", "death"):
            for person_id in node.participants:
                person = by_id[person_id]
                dates = (
                    person.births if node.kind == "birth" else person.deaths
                )
                dates.extend(node.when)
    return list(by_local.values())


def _surnames(node: _Node) -> set[str]:
    """The final tokens of a person's forms of two tokens or more."""
    return {form[-1] for form in node.forms if len(form) >= 2}


def _derive(nodes: Sequence[_Node], config: ClusteringConfig) -> None:
    """Every key a node is blocked and compared by. A person's keys take
    the surnames of its lifted kin, so every node's forms come first."""
    min_name = config.blocking.min_name_token
    min_place = config.blocking.min_place_token
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        node.forms = [
            form for name in node.names for form in name_forms(name, min_name)
        ]
    for node in nodes:
        node.label_keys = frozenset(
            key for name in node.names if (key := normal(name))
        )
        if node.kind == HUMAN:
            _derive_person(node, by_id, min_name)
        elif node.kind == PLACE_KIND:
            firsts = [name.split(",")[0] for name in node.names]
            node.place_keys = frozenset(
                token
                for first in firsts
                for token in _folded_tokens(first)
                if len(token) >= min_place and _letters(token)
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
    keys: set[tuple[str, str]] = set()
    own: set[str] = set()
    firsts: set[str] = set()
    for form in node.forms:
        firsts.add(form[0])
        for position, given in enumerate(form[:-1]):
            for surname in form[position + 1 :]:
                keys.add((given, surname))
                own.add(surname)
    nicknames = {
        form[0]
        for nickname in node.nicknames
        for form in name_forms(nickname, min_name)
    }
    keys.update((nick, surname) for nick in nicknames for surname in own)
    kin_surnames = set().union(
        *(_surnames(by_id[kin_id]) for kin_id in node.kin_ids)
    )
    keys.update(
        (given, surname)
        for surname in kin_surnames
        for given in firsts | nicknames
    )
    node.keys = _key_map(keys)
    node.lone = frozenset(form[0] for form in node.forms if len(form) == 1)
    givens = set(nicknames)
    for form in node.forms:
        givens.update(form[:-1] if len(form) >= 2 else form)
    node.givens = frozenset(givens)


# -- blocking -----------------------------------------------------------------


def _pair(first: str, second: str) -> Pair:
    return (first, second) if first < second else (second, first)


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
            self._keys.add(node.id, node.keys)
            if lone:
                self._givens.add(node.id, {"": node.givens})

    def find(self, node: _Node) -> set[str]:
        if node.keys:
            found = {
                other
                for surname, givens in node.keys.items()
                for given in givens
                for other in self._keys.find(given, surname)
            }
        elif self._lone and node.source != GEDCOM:
            found = {
                other
                for token in node.lone
                for other in self._givens.find(token, "")
            }
        else:
            found = set()
        found.discard(node.id)
        return found


class _Tokens:
    """Nodes by keys they must share one of."""

    def __init__(
        self,
        nodes: Sequence[_Node],
        keys: Callable[[_Node], Iterable[str]],
    ) -> None:
        self._keys = keys
        self._buckets: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for key in keys(node):
                self._buckets[key].append(node.id)

    def find(self, node: _Node) -> set[str]:
        found = {
            other
            for key in self._keys(node)
            for other in self._buckets.get(key, ())
        }
        found.discard(node.id)
        return found


def _contained(first: _Node, second: _Node) -> bool:
    return any(
        one <= other or other <= one
        for one in first.org_sets
        for other in second.org_sets
    )


class _Organizations:
    """Organizations one of whose distinctive token sets holds all of the
    other's."""

    def __init__(self, nodes: Sequence[_Node]) -> None:
        self._nodes = {node.id: node for node in nodes}
        self._tokens = _Tokens(nodes, lambda node: set().union(*node.org_sets))

    def find(self, node: _Node) -> set[str]:
        return {
            other
            for other in self._tokens.find(node)
            if _contained(node, self._nodes[other])
        }


class _Events:
    """Events of one kind whose participants' keys match; an event without
    participants, by its whole name, among those without."""

    def __init__(
        self, nodes: Sequence[_Node], people: Mapping[str, _Node]
    ) -> None:
        self._people = people
        self._keys = _GivenIndex()
        for node in nodes:
            for person_id in node.participants:
                self._keys.add(node.id, people[person_id].keys)
        self._named = _Tokens(
            [node for node in nodes if not node.participants],
            lambda node: node.label_keys,
        )

    def find(self, node: _Node) -> set[str]:
        if not node.participants:
            return self._named.find(node)
        found = {
            other
            for person_id in node.participants
            for surname, givens in self._people[person_id].keys.items()
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
        return lambda nodes, gedcom: _Tokens(
            nodes, lambda node: node.place_keys
        )
    if kind == ORGANIZATION:
        return lambda nodes, gedcom: _Organizations(nodes)
    return lambda nodes, gedcom: _Tokens(nodes, lambda node: node.label_keys)


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
    nodes: Sequence[_Node],
    index: Mapping[str, _Node],
    config: ClusteringConfig,
) -> tuple[set[Pair], dict[str, frozenset[str]]]:
    """Every blocked pair of the mentions to cluster, and the GEDCOM
    anchors each anchored mention found. `index` holds every mention, an
    event's participants among them, whether they are clustered or not."""
    by_kind: dict[str, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_kind[node.kind].append(node)
    pairs: set[Pair] = set()
    anchors: dict[str, frozenset[str]] = {}
    for kind, group in sorted(by_kind.items()):
        if kind in EVENT_KINDS:
            # events meet through their participants, an export's death
            # events among them, and never through an anchor
            pairs |= _within(group, _Events(group, index))
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

    def name_level(self, a: _Node, b: _Node) -> str:
        if a.label_keys & b.label_keys:
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
        if a.kind == ORGANIZATION and _contained(a, b):
            return "key"
        return "none"

    def kin_agree(self, a: _Node, b: _Node) -> bool:
        for relation, first_id in a.kin:
            first = self._nodes.get(first_id)
            if first is None:
                continue
            for other_relation, second_id in b.kin:
                second = self._nodes.get(second_id)
                if second is None or relation != other_relation:
                    continue
                if first.label_keys & second.label_keys or keys_match(
                    first.keys, second.keys
                ):
                    return True
        return False

    def evidence(self, a: _Node, b: _Node) -> tuple[str, str, str]:
        """(name, dates, kin): how the names match (exact, nested, key or
        none), whether the dates agree, conflict or are unknown, and
        whether a kin name agrees."""
        if a.kind in EVENT_KINDS:
            best = "none"
            for first_id in sorted(a.participants):
                for second_id in sorted(b.participants):
                    level = self.name_level(
                        self._nodes[first_id], self._nodes[second_id]
                    )
                    if level == "exact" or (level == "key" and best == "none"):
                        best = level
            return best, _dates(a.when, b.when), "unknown"
        return (
            self.name_level(a, b),
            _combine_dates(
                _dates(a.births, b.births), _dates(a.deaths, b.deaths)
            ),
            "agree" if self.kin_agree(a, b) else "unknown",
        )

    def _person_link(self, a: _Node, b: _Node) -> str:
        name, dates, kin = self.evidence(a, b)
        if dates == "conflict":
            return AMBIGUOUS
        corroborated = kin == "agree" or dates == "agree"
        if name in ("exact", "key") and corroborated:
            return CONFIDENT
        return AMBIGUOUS

    def link(self, a: _Node, b: _Node) -> tuple[str, str]:
        """(the link, the evidence as name/dates/kin) of a candidate
        pair."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict":
            return AMBIGUOUS, described
        if any(
            len(self._anchors.get(node.id, ())) > self._max_anchors
            for node in (a, b)
        ):
            return AMBIGUOUS, described
        if a.kind == HUMAN:
            return self._person_link(a, b), described
        if a.kind in EVENT_KINDS:
            for first_id in sorted(a.participants):
                for second_id in sorted(b.participants):
                    first = self._nodes[first_id]
                    second = self._nodes[second_id]
                    if (
                        first.kind == second.kind == HUMAN
                        and self._person_link(first, second) == CONFIDENT
                    ):
                        return CONFIDENT, described
            return AMBIGUOUS, described
        if a.kind == PLACE_KIND:
            return (
                CONFIDENT if name in ("exact", "nested") else AMBIGUOUS
            ), described
        return (CONFIDENT if name == "exact" else AMBIGUOUS), described


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
        two members out, soft when they are only two ids of one
        manifest."""
        inside = set(members)
        nodes = [self._nodes[member] for member in sorted(members)]
        places: dict[str, list[_Node]] = defaultdict(list)
        for node in nodes:
            if node.kind == PLACE_KIND:
                places[node.label].append(node)
        for node in nodes:
            related = (node.kin_ids | node.containers) & inside
            related |= {
                other.id
                for label in self._containment.container_labels(node.id)
                for other in places.get(label, ())
            }
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


def _distinct(manifests: Iterable[Manifest]) -> list[Manifest]:
    """One manifest per document, in SHA-1 order: two manifests of one
    SHA-1 are one document, and the first is taken."""
    taken: dict[str, Manifest] = {}
    for manifest in manifests:
        taken.setdefault(manifest.sha1.lower(), manifest)
    return [taken[sha1] for sha1 in sorted(taken)]


def inputs_key(manifests: Sequence[Manifest], config: ClusteringConfig) -> str:
    """The key an identity map is reused under: VERSION, the thresholds'
    digest, and each document's SHA-1, source and batch content."""
    return sha256_text(
        canonical_json(
            {
                "stage": STAGE,
                "version": VERSION,
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
    lifted and keyed: every mention by fingerprint, and those to cluster."""

    manifests: list[Manifest]
    nodes: list[_Node]
    index: dict[str, _Node]
    clustered: list[_Node]


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
    return _Corpus(
        manifests=ordered,
        nodes=nodes,
        index={node.id: node for node in nodes},
        clustered=[node for node in nodes if node.mention.extract],
    )


def blocked_pairs(
    manifests: Sequence[Manifest], config: ClusteringConfig | None = None
) -> set[frozenset[tuple[str, str]]]:
    """Every pair blocking proposes, before the cannot-link pairs and the
    pairs the impossibility filter rules out are taken out: each as the
    (document SHA-1, local id) of its two mentions."""
    config = config or load_config()
    corpus = _corpus(manifests, config)
    blocked, _anchors = _block(corpus.clustered, corpus.index, config)
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

    blocked, anchors = _block(clustered, index, config)
    judge = _Judge(index, anchors, config)
    within: set[Pair] = set()
    pruned: Counter[str] = Counter()
    within_related = 0
    links: dict[Pair, tuple[str, str]] = {}
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
            links[pair] = judge.link(first, second)

    candidates = _components((node.id for node in clustered), links)
    conflicts = _Conflicts(index, containment)
    status: dict[str, str] = {}
    review: list[dict[str, Any]] = []
    pairs_of: dict[str, list[Pair]] = defaultdict(list)
    for pair in links:
        pairs_of[candidates.find(pair[0])].append(pair)
    groups = candidates.groups()
    ambiguous_groups: list[int] = []
    all_groups: list[int] = []
    for members in groups:
        if len(members) == 1:
            status[members[0]] = SINGLETON
            continue
        own = sorted(pairs_of[candidates.find(members[0])])
        conflict = conflicts.find(members)
        ambiguous = [pair for pair in own if links[pair][0] == AMBIGUOUS]
        if conflict is not None:
            verdict = CONFLICTING
        elif ambiguous:
            verdict = AMBIGUOUS
        else:
            verdict = CONFIDENT
        for member in members:
            status[member] = verdict
        all_groups.append(len(own))
        if ambiguous:
            ambiguous_groups.append(len(ambiguous))
        if verdict == CONFIDENT:
            continue
        review.append(
            {
                "status": verdict,
                "kind": index[members[0]].kind,
                "members": members,
                "pairs": [
                    {
                        "mentions": list(pair),
                        "link": links[pair][0],
                        "evidence": links[pair][1],
                    }
                    for pair in own
                ],
                "conflict": (
                    {"how": conflict[0], "mentions": list(conflict[1])}
                    if conflict
                    else None
                ),
            }
        )

    clusters: dict[str, dict[str, Any]] = {}
    assigned: dict[str, str] = {}
    for members in groups:
        merged = status[members[0]] == CONFIDENT
        for part in [members] if merged else [[m] for m in members]:
            cid = cluster_id(part)
            clusters[cid] = {
                "kind": index[part[0]].kind,
                "method": status[part[0]],
                "members": part,
            }
            for member in part:
                assigned[member] = cid
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
        p for p, (link, _) in links.items() if link == CONFIDENT
    ]
    linked = _components((node.id for node in clustered), confident_links)
    linked_clusters = [m for m in linked.groups() if len(m) > 1]
    linked_conflicting = [m for m in linked_clusters if conflicts.find(m)]

    by_status = Counter(
        status[members[0]] for members in groups if len(members) > 1
    )
    mentions_by_status = Counter(
        status[members[0]] for members in groups for _ in members
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
        "singletons": mentions_by_status[SINGLETON],
        "largest_candidate_cluster": max(map(len, groups), default=0),
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


def read_identity_map(path: Path) -> dict[str, Any] | None:
    """The identity map at `path`, or None when there is none, or one of
    another format, or one whose decisions do not hash to its hash."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    mentions = data.get("mentions")
    try:
        if identity_hash(mentions) != data.get("hash"):
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
    counts and labels only."""
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
    return reasons


def run_stage(context: Any) -> list[str]:
    """The corpus stage: the identity map of the context's documents in
    its directory, made or reused (ensure_identity_map). Returns the
    reasons it needs attention."""
    output = ensure_identity_map(context.directory, context.documents)
    summary = output.identity_map["summary"]
    context.log(
        f"{STAGE}: identity map "
        + ("reused, its inputs unchanged" if output.reused else "made")
        + f": {summary['clustered_mentions']} mentions of "
        f"{summary['documents']} documents in "
        f"{summary['identity_clusters']} clusters, "
        f"{summary['merged_mentions']} mentions merged; "
        f"{summary['clusters'][AMBIGUOUS]} ambiguous and "
        f"{summary['clusters'][CONFLICTING]} conflicting clusters not "
        "merged; adjudication: about "
        f"{summary['adjudication']['calls_ambiguous_pairs']} calls for the "
        f"ambiguous pairs, {summary['adjudication']['calls_all_pairs']} "
        "for every candidate pair"
    )
    return attention(summary)
