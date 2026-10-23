"""Corpus clustering: every document's mentions, clustered across the
whole corpus into an identity map. It is the scratch driver's first
corpus stage (ADR-0025), and runs past the barrier over the manifests of
every document whose stages completed.

`cluster_corpus` makes the map from the manifests and the thresholds
(clustering.yaml beside this module, load_config) alone: no wiki, no
query service, no graph, and no model. Beyond its arguments it reads
only what the key the map is reused under names: the source of the
modules that make it (CODE) and the Unicode database's version. Its
decisions change only with its arguments, with VERSION, which a change
to what it makes of the same manifests bumps, and with that source. The
stage (`run_stage`) keeps its output in its directory as
`identity-map.json`, and reuses it while the documents' manifests (by
content), VERSION, that source, the Unicode version and the thresholds
stand.

**Mentions.** Every entity of every manifest is a mention, named by its
fingerprint (yizkr.pipeline.fingerprints): its artifact's SHA-1, its
canonical label, its kind and its excerpt span, never the manifest's id.
A batch without an entity manifest gives a mention per local id its
claims name. The GEDCOM export's people, places, occupations, media and
death events are mentions too. Only mentions the manifest marks for
extraction are clustered; the others are each a cluster of their own
("unclustered").

**Records.** Clustering keys and judges on each mention's record, what
extraction says its names are made of (`EntityRecord.structure`,
ADR-0035 §2), never on the wording, word order or punctuation of its
label. A person, a place, an organisation or a family without a record
(none given, one extraction set aside under ADR-0035 §5, a record of
another kind, or a batch without a manifest) is *recordless*
(ADR-0035 §7, rule 1): it keys on its exact normalised name, from its
label and each alias, meets only mentions without a record, and never
links confidently. A form its document gives as an appellation of it (a
`referred_to_as` claim's value, ADR-0018: "Oma", "Tante Rahel") is no
name it keys on, as label or alias, so two people called "Oma" never
meet by it, unless it is a place, an organisation or a family, which are
never persons: a mention of any other kind, an unclassified entity or a
ref with no kind among them, may be one. The summary counts recordless
mentions by kind.

**Lifting.** Relations the manifest states between its own local ids are
lifted into mention space: kin (father, mother, child, spouse and
sibling, each seen from both ends, so a father claim and a child claim
say the same, and the couple of a union as spouses), the human
participants of events and unions with their roles (`object_has_role`),
the events a union is started and ended by, place containment
(`part_of`, `location`), dates, stated sexes and stated civil statuses.
A birth or death event is its principal's own, and its date is theirs
too, when it has exactly one principal, and that person's own date of
the kind (from their claims, never one an event passed them) does not
conflict with it (ADR-0035 §3.2). A union's own date is an observation,
never a key: its dates are its weddings'. Only what the manifests assert
is used: no containment or kinship is read from the graph, and none is
inferred.

**Parts.** Every part compares by `normal`: case and accents folded after
Unicode normalisation, each word's edge punctuation stripped, and single
spaces. A part holding an umlaut keys twice (DIN 5007-2, ADR-0035 §7):
once folded (Flörburg as `florburg`) and once with the umlaut written out
(`floerburg`); two parts match when they share a key. A two-letter
spelling is never turned back into an umlaut.

**Keys** (ADR-0035 §7, rule 1), from typed parts only:

- *A person* whose record's status is `name`: each given name, by its
  standard form and as written, and each nickname, with each of its own
  surnames and patronymics and each surname of the kin its document
  names. No key pairs two parts that share a key, and a given part that
  shares a key with any of those surnames, or with a word of the label of
  a kin without a record (a surname misread as a given name), pairs with
  no kin's surname. An `unsplit` form keys each ordered pair of its
  distinct words, never
  with a kin's surname, and a one-word one counts as a given name. With
  no surname of either kind, its given names alone (`lone`), which pair
  only with the GEDCOM export's people of that given name. Any other
  status keys nothing.
- *A place* whose status is `toponym`: each of its names as written, its
  address among them, save a short form that is another of its names'
  toponym ("Klurnau" beside "Lager Klurnau"). Its `within` never key.
- *An organisation*: each proper name; without one, each kind with each
  of its places. Its places are those its record names, or else the
  places its `location` claims point to, with every place its manifest
  puts them in; a place without a record is named by its exact names.
- *A family*: each surname.
- *Events*: the same kind (a wedding of any kind together; a divorce, an
  annulment and an end of unstated kind together), through people: a
  birth or death its principal's own through that principal, a union
  through its couple, a union's wedding or end through its union's
  couple, any other event through its participants. An event without
  any keys on its whole name.
- *Everything else*, and a recordless mention: its exact normalised name.

With `gedcom-anchored`, a mention (of any kind but an event's or a
union's) that key-matches a mention of a GEDCOM export pairs with those
GEDCOM anchors only, never with other mentions, so the export is the
roster documents meet. A key that more than `max-key-mentions` mentions
of one kind share (a hub) is blocked on by none of them: the mentions
only it would join stay apart, the summary counts the hubs and the
mentions that hold one, and `review` lists them. A key that finds a
GEDCOM mention is never a hub, whatever its count, when the export
anchors its kind by key (every kind but an organisation, an event and a
union): each mention holding it pairs with its anchors only. Nor is a
person's lone given name, which only meets the export. Blocking looks
keys up in indexes, so its work grows with the mentions and the pairs it
proposes, never with every pair of mentions.

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

- a person pair is confident when they share a key (`key`), and kin that
  agree, or dates that agree, corroborate it. A shared key counts only
  through given parts that may be one name (a given part is a given name
  or a word of an `unsplit` form, which states no standard form): not
  two given parts whose records give standard forms that share no key;
  not two nicknames whose holders' records both give given names,
  sharing no key; and not a given part against a nickname where the
  given part states a standard form that shares no key with the
  nickname holder's given names, a holder with no given names barring
  nothing. A shared key that does not count is `weak`. A key only a GEDCOM
  `unsplit` form gives (`weak`) and a lone given name (`given`) never
  make a link confident. Stated sexes that differ, or civil statuses
  that cannot both hold (`unmarried` at a date wholly after a `married`,
  `widowed` or `divorced` one), keep a pair ambiguous and never
  corroborate;
- a place pair when they share a name and their containers (a record's
  `within`, and the places its manifest's `part_of` and `location`
  claims put it in) are equal (`exact`) or one's are all among the
  other's (`nested`); a shared name with containers that differ (`key`)
  stays ambiguous;
- an organisation pair on a shared proper name of two words or more
  (`exact`); on a shared proper name of one word (`key`) only with places
  that agree, a shared kind notwithstanding; and, when neither has a
  proper name, on a shared kind with places that agree (`kind`). Places
  its `location` claims name agree when a place of one would link with a
  place of the other, or with a place that one lies in, as places do
  (`exact` or `nested`; ADR-0035 §7, rule 6); a place without a record,
  which cannot link, leaves them unknown where it shares an exact name
  with the other's. Places its record names carry no containers: as one
  list of parts, against the other's (its record's, or the places its
  location claims reach and every place they lie in, one without a
  record by its exact names), they agree when one list lies among the
  other and a part with a record is shared, and are unknown when only a
  part without one is; otherwise they differ, whether they share a part
  (a namesake in another district, or places named at different levels)
  or nothing. Places differ when both
  state places and none of that holds, and places that differ keep a
  pair ambiguous whatever its name;
- a family pair never: every pair goes to adjudication;
- a union pair when both name a couple of two and each of one's couple
  merged with one of the other's, and their weddings' dates do not
  conflict;
- a wedding or an end of a union when their unions merged, their kinds
  can be one (a civil and a religious wedding are two ceremonies; an end
  of unstated kind meets any end) and their dates agree;
- two births or two deaths, each its principal's own, when the
  principals merged, with no date needed, since a person has one of each;
  a birth or death that is nobody's own (no principal, several, or one
  whose own date conflicts with it) never, whoever else took part;
- any other event pair when every participant of the one with fewer is
  merged into one cluster with a participant of the other, and their
  dates agree;
- any other pair when the names match exactly;
- and never when the two give dates that cannot overlap, when either is
  recordless of a kind that takes a record, when a mention has no kind,
  or when either matches more than `max-anchors` GEDCOM anchors (a place
  counting only the anchors it matches exactly, and those it nests in as
  one).

Dates are compared as ranges, at the precision their source states: a
decade is ten years, a circa date is widened by `circa-years`, a range's
bounds widen it, and a bound on one side only leaves the other open, so
a date is never narrowed to be compared (rule 5). Two dates agree when
their ranges overlap and both are at most `max-corroborating-years`
wide; a wider one (a century, a lone "before") agrees with nothing, and
conflicts only when it cannot overlap. Kin agree on a relation when they
share a key, never a surname alone, and their dates do not conflict.

**Clusters.** The closure over every candidate pair gives the candidate
clusters, the units a review takes. A cluster is *conflicting* when it
joins a cannot-link pair: *hard* when the impossibility filter rules the
two out, *soft* when they are only two ids of one manifest. Otherwise it
is *ambiguous* when any of its links is, or when two of its members that
were never a pair contradict each other: dates that cannot overlap,
stated sexes or civil statuses that cannot both hold, kin of one
relation and one name whose dates conflict, event kinds that cannot be
one, or names that do not match at all (for places, neither exactly nor
nested; for organisations, places that differ). Blocking is not
transitive, through anchors and kin surnames, so the closure can join
what no pair of its members says. Otherwise it is *confident*. A
confident cluster merges: its members share one cluster.

**Confident components.** An ambiguous candidate cluster merges each of
its confident components, the closure over its confident links, with
the method `confident_component`. Each component is re-checked first as
the cluster check checks a cluster (_confident_components): it stays
apart when it joins a cannot-link pair (which a component of a cluster
that joins none never does: the check holds by construction, and is
kept as the cluster check's own), when it holds a pair an
adjudicator said `different` (a `different` is a cannot-link), or when
two of its members contradict each other as above; then each of its
members stays apart, `ambiguous`, as does a member no confident link
reaches. A chain of confident links can join what no one link says,
which the re-check guards against but cannot rule out. An ambiguous
link whose two mentions one merged component holds is settled by it
(its review pair's `inside_component`); only the ambiguous links between
components, and those inside a component kept apart, are left for
adjudication or review. This holds in every mode: keeper mode merges the
components too. A conflicting cluster merges nothing, its confident
components included: its members stay apart, each a cluster of its own,
and are counted. A mention with no candidate pair is a singleton.
Unions are clustered after people, places and organisations, and events
after unions, since their links read the clusters their people and
unions end in, components included.

**Adjudication.** Given an adjudicator's verdicts on ambiguous pairs
(`cluster_corpus` with `verdicts`, which yizkr.pipeline.adjudication
asks for), an ambiguous candidate cluster is settled when it has a
pending pair (an ambiguous pair no merged confident component holds) and
every pending pair is `same` or `different`: its `different` pairs are
cut, and the closure over its confident links and its pending `same`
pairs gives its parts, so a part is its confident components joined by
`same` pairs. A part that holds a pair contradicted does not merge, and
falls back to its confident components, each merged or apart as it is
without verdicts; the cluster counts among the ambiguous ones with the
members left apart. A pair contradicted is a `different` pair the
closure joined another way (a `different` is a cannot-link), a `same`
pair whose evidence says they differ by more than their own dates, or
two other members whose evidence says they differ (_Judge.contradicts).
A verdict excuses a date conflict on its own pair and nothing else:
never stated sexes or civil statuses that cannot both hold, kin that
conflict, a place's containers that differ, an organisation's places
that differ, or event kinds that cannot be one. Each ambiguous review
pair records whether a `same` could merge it (`same_merges`); a
candidate cluster of two whose one pair could not (cannot_merge) is not
pending in a run whose verdicts merge. Any other
part merges, with the method `adjudicated` when a `same` pair is among
its links, every member of the components it joins included, since the
verdict made the merge; and `confident_component` when its confident
links alone join it (a component a `different` cut from the rest). A
member a `different` pair left alone is a cluster of its own,
`adjudicated`, counted as `adjudicated_apart`. An ambiguous cluster with
an `uncertain` pending pair, or a pending pair without a verdict, is not
settled: its confident components merge as without verdicts, and the
rest stays apart. A conflicting cluster never merges, whatever its
verdicts. Each review pair of an ambiguous link records its verdict, or
null, under such a map; pending_pairs gives the pairs neither a merged
component nor a verdict settles yet.

**The identity map** (`identity-map.json`, format FORMAT):

- `mentions`: fingerprint -> its `cluster` (a cluster id, the hash of the
  sorted fingerprints of its members) and its `method` (`confident`,
  `confident_component`, `adjudicated`, `ambiguous`, `conflicting`,
  `singleton` or `unclustered`: what put it where it is), with the
  document's SHA-1,
  the manifest's local id, the label, the kind, the span and the
  occurrence it was made from;
- `hash`: the SHA-256 of every mention's cluster and method, and nothing
  else, so equal decisions hash equal whatever else the file holds;
- `clusters`: cluster id -> its kind, method and members;
- `review`: each ambiguous, conflicting or adjudicated candidate
  cluster, with its members, its pairs (the link and the evidence of
  each; for an ambiguous link, whether a `same` could merge it, whether
  a merged confident component holds it, and its verdict under
  verdicts), and the pair that makes it conflict and how, or the first
  two members that contradict each other; then each hub (`hub`), with
  its key and the mentions holding it;
- `summary`: counts only, the clusters and mentions each method merged
  (`confident`, `confident_component` and `adjudicated` apart) or left
  apart, the adjudication-call estimate and the recordless mentions
  among them;
- `inputs`: the key it is reused under (VERSION, the digest of CODE's
  source, the Unicode version, the thresholds' digest, each document's
  SHA-1, source and batch content hash, and the verdicts' digest when
  it was made under verdicts); `config`: the thresholds' digest.

**Its read side.** The writer and the resolve step read a map through
`IdentityMap`: which cluster each mention of a batch is in
(`clusters_of`, by the mentions' fingerprints under an artifact the
batch's claims cite, raising UnmappedMention for a batch with a mention
the map lacks), and each cluster's kind, labels and documents
(`clusters`). The writer finds or mints one item per cluster through it
alone (yizkr.pipeline.writer, step 2).

The map holds labels, so it stays in the run directory, which is
local-only data. What the stage returns, and so what reaches the run's
events, is counts within labels of its own, never a document's words.
"""

from __future__ import annotations

import functools
import hashlib
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

from yizkr.claims import (
    STRUCTURED_KINDS,
    ClaimBatch,
    FamilyStructure,
    ItemValue,
    LocalRef,
    OrganizationStructure,
    PersonStructure,
    PlaceStructure,
    TimeValue,
    VocabRef,
    name_form_key,
)
from yizkr.pipeline.fingerprints import (
    Mention,
    cluster_id,
    local_refs,
    mention_map,
    mentions_of,
)
from yizkr.pipeline.names import NOT_PERSONS, appellations_of
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
from yizkr.pipeline.writer import stored_term

# what cluster_corpus makes of a set of manifests: bumped whenever the same
# manifests and thresholds would give another identity map, so that a
# stored map is made again
VERSION = 5
# the layout of identity-map.json
FORMAT = 1
STAGE = "clustering"
IDENTITY_MAP = "identity-map.json"
PACKAGE = files("yizkr.pipeline")
# the thresholds shipped with the package
DEFAULTS = PACKAGE / "clustering.yaml"
# the modules whose source makes the identity map: a map another source
# made is made again, whatever VERSION says. The other yizkr modules that
# this one and fingerprints.py import reach the key through the batches'
# hashes: yizkr.claims parses the batches, orchestrate says what a hash
# leaves out, and stages makes it. writer.py gives the form the instance
# keeps a term in (stored_term), which names are compared in, and names.py
# which forms are appellations (appellations_of).
CODE = (
    "clustering.py",
    "fingerprints.py",
    "reconcile.py",
    "writer.py",
    "names.py",
)

# what a manifest is the extraction of
DOCUMENT = "document"
GEDCOM = "gedcom"

# what put a mention in its cluster (a mention's `method`), which is also
# a candidate cluster's status
CONFIDENT = "confident"
# a mention a confident component of an ambiguous candidate cluster (the
# closure over its confident links) merged with others
CONFIDENT_COMPONENT = "confident_component"
# a mention a candidate cluster settled by adjudication put where it is,
# merged with others or apart (the adjudication stage, ADR-0025)
ADJUDICATED = "adjudicated"
AMBIGUOUS = "ambiguous"
CONFLICTING = "conflicting"
SINGLETON = "singleton"
UNCLUSTERED = "unclustered"
# how a conflicting cluster conflicts
HARD = "hard"
SOFT = "soft"
# an adjudicator's verdict on an ambiguous pair: one entity, two, or the
# evidence cannot tell
SAME = "same"
DIFFERENT = "different"
UNCERTAIN = "uncertain"
VERDICTS = (SAME, DIFFERENT, UNCERTAIN)
# the methods that merge a cluster's members
MERGING = frozenset({CONFIDENT, CONFIDENT_COMPONENT, ADJUDICATED})
# the methods the summary counts clusters and their mentions by
SUMMARY_METHODS = (
    CONFIDENT,
    CONFIDENT_COMPONENT,
    ADJUDICATED,
    AMBIGUOUS,
    CONFLICTING,
)
# a review entry of the mentions holding a hub key
HUB = "hub"

HUMAN = "human"
ORGANIZATION = "organization"
FAMILY = "family"
# a couple's relationship, and a marriage, which is one (ADR-0036): its
# identity is its couple's
UNION_KINDS = frozenset({"partnership", "marriage"})
# the ceremonies that start a union, and the ends a court or an authority
# pronounces
WEDDING_KINDS = frozenset({"wedding", "civil_wedding", "religious_wedding"})
END_KINDS = frozenset({"divorce", "annulment"})
# an end of unstated kind, which meets any end
UNSTATED = "event"
# kinds whose identity is anchored by who took part, not by a name
EVENT_KINDS = (
    frozenset(
        {
            "birth",
            "death",
            "migration",
            "deportation",
            "imprisonment",
            "release",
            "military_service",
            UNSTATED,
            "legal_instrument",
        }
    )
    | WEDDING_KINDS
    | END_KINDS
)
# the events a person takes part in once: their principals decide them
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
CONTAINMENT_PROPERTIES = ("part_of", "location")
# the date properties of an event, and of the events a person is born or
# dies in
EVENT_DATE_PROPERTIES = ("point_in_time", "start_time")
# the participant roles (ADR-0035 §3.2) clustering reads: the one an event
# happens to, and a union's couple
PRINCIPAL = "principal"
COUPLE_ROLES = frozenset({"spouse", "partner"})
# a civil status that cannot hold after any of the others (ADR-0035 §7,
# rule 2)
UNMARRIED = "unmarried"
EVER_MARRIED = frozenset({"married", "widowed", "divorced"})
# the stated sex of a person
SEX = "sex_or_gender"
CIVIL_STATUS = "civil_status"
_STRIP = ".,;:()\"'[]"
# DIN 5007-2: an umlaut written out
_UMLAUTS = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}
)
# the name levels of a pair, strongest first, for an event's evidence
_LEVELS = ("exact", "key", "nested", "kind", "weak", "given", "none")
# an interval's open end
_OPEN = float("inf")

Pair = tuple[str, str]
# a key a mention is blocked on: ("key", given name, surname) for a
# person, ("lone", given name) for a person with no surname, ("place",
# name), ("org", name) or ("orgkind", kind, place), ("family", surname),
# ("name", name) for an event keyed by its own name, and ("exact", name)
# for a recordless mention and any kind that takes no record
BlockingKey = tuple[str, ...]
# a part's keys: its normal form, and its umlauts written out
Part = frozenset[str]


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


# -- parts --------------------------------------------------------------------


@functools.lru_cache(maxsize=1 << 16)
def fold(text: str) -> str:
    """Case- and accent-insensitive form: "Cäcilie" and "cacilie" are one."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("ß", "ss")


def _words(text: str) -> list[str]:
    """A part's words as the instance would keep the part
    (writer.stored_term: a run of control characters one space, U+FFFE
    and U+FFFF made U+FFFD, then NFC), each without its edge
    punctuation. Every key of a part is made from these words, so a
    part keys as the instance keeps it, whatever form a batch gives it
    in."""
    return [
        word
        for raw in stored_term(text).split()
        if (word := raw.strip(_STRIP))
    ]


@functools.lru_cache(maxsize=1 << 16)
def normal(name: str) -> str:
    """A whole part as its folded words, one space apart."""
    return " ".join(token for word in _words(name) if (token := fold(word)))


def umlauts_written_out(text: str) -> str:
    """A part with each umlaut written out (DIN 5007-2): "Flörburg" as
    "Floerburg". A two-letter spelling is left as it stands."""
    return unicodedata.normalize("NFC", text).translate(_UMLAUTS)


@functools.lru_cache(maxsize=1 << 16)
def part_keys(text: str) -> Part:
    """What a part keys on: its normal form, and, when it holds an
    umlaut, its normal form with the umlaut written out. Two parts match
    when they share a key."""
    return frozenset(
        key for key in (normal(text), normal(umlauts_written_out(text))) if key
    )


def _all(parts: Iterable[Part]) -> frozenset[str]:
    return frozenset(key for part in parts for key in part)


def _pairs(firsts: Sequence[Part], seconds: Sequence[Part]) -> set[Pair]:
    """Each key of a first part with each key of a second one, but for two
    parts that share a key."""
    return {
        (one, other)
        for first in firsts
        for second in seconds
        if not first & second
        for one in first
        for other in second
    }


def _distinct_words(form: str) -> list[Part]:
    """The words of an unsplit form, each once, by their keys."""
    found: list[Part] = []
    for word in _words(form):
        keys = part_keys(word)
        if keys and not any(keys & seen for seen in found):
            found.append(keys)
    return found


def _person_names(record: PersonStructure) -> tuple[list[Part], list[Part]]:
    """(given names and nicknames, surnames and patronymics) of a person's
    record, as parts: a given name keys by its standard form and as
    written."""
    givens = [
        part_keys(name.written) | part_keys(name.standard or "")
        for name in record.given
    ] + [part_keys(name) for name in record.nicknames]
    surnames = [part_keys(name.written) for name in record.surnames] + [
        part_keys(name) for name in record.patronymics
    ]
    return [p for p in givens if p], [p for p in surnames if p]


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


def _conflict(first: list[Interval], second: list[Interval]) -> bool:
    """Whether two lists of dates, both stated, have no two that overlap."""
    return (
        bool(first)
        and bool(second)
        and not any(one.overlaps(other) for one in first for other in second)
    )


def _combine_dates(*results: str) -> str:
    if "conflict" in results:
        return "conflict"
    return "agree" if "agree" in results else "unknown"


def _never_married_after(
    first: Sequence[tuple[str, Interval]],
    second: Sequence[tuple[str, Interval]],
) -> bool:
    """Whether the first states `unmarried` at a date wholly after one
    the second states `married`, `widowed` or `divorced` at."""
    return any(
        status == UNMARRIED and other in EVER_MARRIED and when.start > then.end
        for status, when in first
        for other, then in second
    )


# -- mentions -----------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """One document's batch, as clustering reads it: the SHA-1 of the
    document's media, its batch, and whether the document is a GEDCOM
    export (`source`)."""

    sha1: str
    batch: ClaimBatch
    source: str = DOCUMENT


@dataclass(frozen=True)
class _Given:
    """A given part of a person's record: its keys, the keys of the
    standard form its record gives (empty without one), and whether it is
    a nickname."""

    keys: Part
    standard: Part = frozenset()
    nickname: bool = False


@dataclass(eq=False)
class _Node:
    """A mention, with its record, what its manifest says about it lifted
    into mention space (kin, participants and containers by fingerprint),
    and the keys blocking and evidence read."""

    mention: Mention
    source: str
    # whether its manifest keeps it apart from the manifest's other ids
    exclusive: bool
    aliases: list[str]
    # for a mention that may be a person, the forms its document gives as
    # appellations of it, as name_form_key compares them: never a name it
    # keys on
    appellations: frozenset[str] = frozenset()
    # the mention record, when its entity has one of its own kind
    record: Any = None
    kin: set[tuple[str, str]] = field(default_factory=set)
    # an event's or a union's people, and the roles each took part in
    participants: set[str] = field(default_factory=set)
    roles: dict[str, set[str]] = field(default_factory=dict)
    containers: set[str] = field(default_factory=set)
    # the places an organisation is located in
    places: set[str] = field(default_factory=set)
    # a union's weddings and ends; the union an event starts or ends
    starts: set[str] = field(default_factory=set)
    ends: set[str] = field(default_factory=set)
    union: str | None = None
    births: list[Interval] = field(default_factory=list)
    deaths: list[Interval] = field(default_factory=list)
    when: list[Interval] = field(default_factory=list)
    sexes: set[str] = field(default_factory=set)
    civil: list[tuple[str, Interval]] = field(default_factory=list)
    # a birth or death event its one principal's own (_nodes), and who
    principal: str | None = None
    # computed once every node of the corpus is lifted (_derive): a
    # person's keys (every one, and those that may make a link
    # confident), its lone given names, and the given names a lone name
    # meets
    keys: frozenset[Pair] = frozenset()
    strong: frozenset[Pair] = frozenset()
    lone: frozenset[str] = frozenset()
    givens: frozenset[str] = frozenset()
    # a person's given parts (_Given), and the keys of its given names
    given_parts: tuple[_Given, ...] = ()
    given_names: frozenset[str] = frozenset()
    # a place's keyed names, its containers as its record writes them,
    # and those with every place its manifest puts it in (_contain)
    place_names: tuple[Part, ...] = ()
    within: tuple[Part, ...] = ()
    containing: tuple[Part, ...] = ()
    # an organisation's proper names (with their word counts), kinds, the
    # keys of the places it is in, and of those with every place they lie
    # in; and, for places its location claims name, the places by
    # fingerprint, and those with every place they lie in
    org_names: tuple[tuple[Part, int], ...] = ()
    org_kinds: frozenset[str] = frozenset()
    org_primary: frozenset[str] = frozenset()
    org_all: frozenset[str] = frozenset()
    org_places: tuple[str, ...] = ()
    org_closure: frozenset[str] = frozenset()
    # the places it is in as one list of parts, from its record's places
    # or from the places with records its location claims reach; and the
    # places without records they reach, each by its exact names
    org_set: tuple[Part, ...] = ()
    org_loose: tuple[Part, ...] = ()
    # a family's surnames
    family: frozenset[str] = frozenset()
    # a recordless mention's (or one of a kind without records) every
    # name, normalised
    exact: frozenset[str] = frozenset()
    # the keys it would be blocked on, and those it is, hub keys left
    # out (_hubs)
    raw: frozenset[BlockingKey] = frozenset()
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
        """Its label and aliases, save each that is an appellation."""
        return [
            name
            for name in (self.label, *self.aliases)
            if name and name_form_key(name) not in self.appellations
        ]

    @property
    def kin_ids(self) -> set[str]:
        return {node_id for _relation, node_id in self.kin}

    @property
    def recordless(self) -> bool:
        """A mention of a kind that takes a record, without one."""
        return self.kind in STRUCTURED_KINDS and self.record is None

    @property
    def couple(self) -> list[str]:
        """A union's couple: its participants as spouses or partners, or
        every participant where none has such a role."""
        paired = sorted(
            person
            for person, roles in self.roles.items()
            if roles & COUPLE_ROLES
        )
        return paired or sorted(self.participants)


def _local_id(ref: Any) -> str | None:
    return ref.local_id if isinstance(ref, LocalRef) else None


def _vocabulary(value: Any) -> str | None:
    """The vocabulary key an item value names, if it names one."""
    if isinstance(value, ItemValue) and isinstance(value.value, VocabRef):
        return value.value.key
    return None


def _nodes(manifest: Manifest, circa_years: int) -> list[_Node]:
    """One node per mention of the manifest, with its record and the
    relations the manifest states between its local ids lifted into
    mention space. A node's aliases leave out each alias the instance
    would keep as its label: the two are compared in the form the
    instance keeps a term in (writer.stored_term), so an alias that
    differs from the label only in its Unicode form is the label."""
    batch = manifest.batch
    mentions = mentions_of(manifest.sha1, batch)
    # whether a record stands (ADR-0035 §5) is extraction's verdict,
    # taken on the alias bag before the cleanup of §8; a record of another
    # kind is read as none
    records = {
        entity.id: entity.structure
        for entity in batch.entities
        if entity.structure is not None
        and entity.structure.kind == entity.kind
    }
    if batch.entities:
        aliases = {
            entity.id: list(entity.aliases) for entity in batch.entities
        }
    else:
        aliases = defaultdict(list)
        for ref in local_refs(batch):
            if ref.label and stored_term(ref.label) not in map(
                stored_term, aliases[ref.local_id]
            ):
                aliases[ref.local_id].append(ref.label)
    appellations = appellations_of(batch.claims)
    by_local: dict[str, _Node] = {}
    for mention in mentions:
        # an alias the instance would keep as the label is the label
        label = stored_term(mention.label)
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
                if stored_term(alias) != label
            ],
            record=records.get(mention.local_id),
            appellations=frozenset(
                name_form_key(form)
                for form in appellations.get(mention.local_id, ())
                if mention.kind not in NOT_PERSONS
            ),
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
        elif (
            prop == "participant"
            and target is not None
            and target.kind == HUMAN
        ):
            subject.participants.add(target.id)
            subject.roles.setdefault(target.id, set()).update(
                role
                for qualifier in claim.qualifiers
                if qualifier.property == "object_has_role"
                and (role := _vocabulary(qualifier.value))
            )
        elif prop == "started_by" and target is not None:
            subject.starts.add(target.id)
        elif prop == "ended_by" and target is not None:
            subject.ends.add(target.id)
        elif (
            prop in CONTAINMENT_PROPERTIES
            and target is not None
            and target is not subject
            and subject.kind == PLACE_KIND
            and target.kind == PLACE_KIND
        ):
            subject.containers.add(target.id)
        elif (
            prop in CONTAINMENT_PROPERTIES
            and target is not None
            and subject.kind == ORGANIZATION
            and target.kind == PLACE_KIND
        ):
            subject.places.add(target.id)
        elif prop == SEX and (sex := _vocabulary(value)):
            subject.sexes.add(sex)
        elif prop == CIVIL_STATUS and (status := _vocabulary(value)):
            subject.civil.extend(
                (status, date_interval(q.value, claim.qualifiers, circa_years))
                for q in claim.qualifiers
                if q.property == "point_in_time"
                and isinstance(q.value, TimeValue)
            )
        elif isinstance(value, TimeValue):
            interval = date_interval(value, claim.qualifiers, circa_years)
            if prop == "date_of_birth":
                subject.births.append(interval)
            elif prop == "date_of_death":
                subject.deaths.append(interval)
            elif prop in EVENT_DATE_PROPERTIES:
                subject.when.append(interval)
    by_id = {node.id: node for node in by_local.values()}
    for union in by_local.values():
        if union.kind not in UNION_KINDS:
            continue
        # the couple are each other's spouses, as a spouse claim says
        couple = union.couple
        for person in couple:
            by_id[person].kin.update(
                ("spouse", other) for other in couple if other != person
            )
        # its weddings date it, never its own date, an observation; they
        # and its ends are keyed by it, save a partner's death, which
        # stays its principal's own
        for event_id in sorted(union.starts | union.ends):
            event = by_id[event_id]
            if event.kind == "death" or event.union is not None:
                continue
            event.union = union.id
        union.when = [
            interval
            for event_id in sorted(union.starts)
            for interval in by_id[event_id].when
        ]
    # each person's own dates, from its claims, before any event's pass
    own_dates = {
        node.id: (list(node.births), list(node.deaths))
        for node in by_local.values()
    }
    for node in by_local.values():
        # a birth or death event is its principal's own, and its date is
        # theirs, when it has one principal whose own date of the kind its
        # date does not conflict with
        if node.kind not in ONE_PER_PERSON:
            continue
        principals = [
            person
            for person, roles in sorted(node.roles.items())
            if PRINCIPAL in roles
        ]
        if len(principals) != 1:
            continue
        (person_id,) = principals
        births, deaths = own_dates[person_id]
        if _conflict(node.when, births if node.kind == "birth" else deaths):
            continue
        node.principal = person_id
        person = by_id[person_id]
        dates = person.births if node.kind == "birth" else person.deaths
        dates.extend(node.when)
    return list(by_local.values())


def _derive(nodes: Sequence[_Node]) -> None:
    """Every key a node is blocked and compared by, from its record
    alone; a recordless one's exact names. An organisation's places read
    its places' keys, so every place comes first."""
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        record = node.record
        if record is None:
            node.exact = frozenset(
                key for name in node.names for key in part_keys(name)
            )
            if node.kind not in EVENT_KINDS | UNION_KINDS:
                node.raw = frozenset(("exact", name) for name in node.exact)
        elif isinstance(record, PersonStructure):
            _derive_person(node, record, by_id)
        elif isinstance(record, PlaceStructure):
            _derive_place(node, record)
        elif isinstance(record, FamilyStructure):
            node.family = _all(part_keys(name) for name in record.surnames)
            node.raw = frozenset(("family", key) for key in node.family)
    for node in nodes:
        if isinstance(node.record, PlaceStructure):
            _contain(node, by_id)
    for node in nodes:
        if isinstance(node.record, OrganizationStructure):
            _derive_organization(node, node.record, by_id)


def _derive_person(
    node: _Node, record: PersonStructure, by_id: Mapping[str, _Node]
) -> None:
    if record.status != "name":
        return
    givens, surnames = _person_names(record)
    kin_surnames = [
        part
        for kin_id in sorted(node.kin_ids)
        if isinstance(kin := by_id[kin_id].record, PersonStructure)
        for part in _person_names(kin)[1]
    ]
    # the words of a kin's label where it has no record, which may be its
    # surname: they only keep a given part from pairing
    unread = [
        part_keys(word)
        for kin_id in sorted(node.kin_ids)
        if by_id[kin_id].record is None and by_id[kin_id].kind == HUMAN
        for word in _words(by_id[kin_id].label)
    ]
    # a given part that folds like any surname, its own or a kin's (a
    # surname misread as a given name), pairs with no kin's surname
    surname_keys = _all(surnames + kin_surnames + unread)
    typed = _pairs(givens, surnames) | _pairs(
        [part for part in givens if not part & surname_keys], kin_surnames
    )
    unsplit: set[Pair] = set()
    alone: list[Part] = []
    for form in record.unsplit:
        words = _distinct_words(form)
        if len(words) == 1:
            alone.extend(words)
        for position, first in enumerate(words):
            unsplit |= _pairs(
                [first], words[:position] + words[position + 1 :]
            )
    # the export does not say which word of an unsplit form is which, and
    # some may be markers: its keys pair but never make a link confident
    strong = typed if node.source == GEDCOM else typed | unsplit
    node.keys = frozenset(typed | unsplit)
    node.strong = frozenset(strong)
    node.given_parts = tuple(
        [
            _Given(
                part_keys(name.written) | part_keys(name.standard or ""),
                part_keys(name.standard or ""),
            )
            for name in record.given
        ]
        + [_Given(part_keys(name), nickname=True) for name in record.nicknames]
        + [
            _Given(word)
            for form in record.unsplit
            for word in _distinct_words(form)
        ]
    )
    # the given names a nickname is weighed against: the given names and
    # the words of the unsplit forms
    node.given_names = _all(
        part.keys
        for part in node.given_parts
        if part.keys and not part.nickname
    )
    node.givens = _all(givens + alone)
    node.lone = frozenset() if node.keys else node.givens
    node.raw = frozenset(
        {("key", given, surname) for given, surname in node.keys}
        | {("lone", given) for given in node.lone}
    )


def _derive_place(node: _Node, record: PlaceStructure) -> None:
    if record.status != "toponym":
        return
    # a short form that is another of its names' toponym keys nothing
    toponyms = _all(
        part_keys(name.toponym)
        for name in record.names
        if name.toponym and normal(name.toponym) != normal(name.written)
    )
    names = [
        keys
        for name in record.names
        if (keys := part_keys(name.written)) and not keys & toponyms
    ]
    if record.address is not None and (
        keys := part_keys(record.address.written)
    ):
        names.append(keys)
    within: list[Part] = []
    for container in record.within:
        keys = part_keys(container)
        if keys and not any(keys & seen for seen in within):
            within.append(keys)
    node.place_names = tuple(names)
    node.within = tuple(within)
    node.raw = frozenset(("place", key) for key in _all(names))


def _closure(start: Iterable[str], by_id: Mapping[str, _Node]) -> set[str]:
    """The places given, and every place their manifest puts them in."""
    seen: set[str] = set()
    stack = sorted(start)
    while stack:
        place_id = stack.pop()
        if place_id in seen:
            continue
        seen.add(place_id)
        stack.extend(sorted(by_id[place_id].containers))
    return seen


def _contain(node: _Node, by_id: Mapping[str, _Node]) -> None:
    """A place's containers for rule 5: its record's `within`, and each
    place its manifest's `part_of` and `location` claims put it in, as
    one part each (a recordless one by its exact names)."""
    containing = list(node.within)
    for place_id in sorted(_closure(node.containers, by_id) - {node.id}):
        keys = _place_names(by_id[place_id])
        if keys and not any(keys & seen for seen in containing):
            containing.append(keys)
    node.containing = tuple(containing)


def _place_names(node: _Node) -> frozenset[str]:
    """The keys a place is named by: a recordless one's exact names."""
    return _all(node.place_names) or node.exact


def _place_keys(node: _Node) -> frozenset[str]:
    """The keys a place is named by, and those of the places its record
    puts it in; a recordless one's exact names."""
    return (_all(node.place_names) | _all(node.within)) or node.exact


def _derive_organization(
    node: _Node, record: OrganizationStructure, by_id: Mapping[str, _Node]
) -> None:
    node.org_names = tuple(
        (keys, len(normal(name).split()))
        for name in record.names
        if (keys := part_keys(name))
    )
    node.org_kinds = _all(part_keys(kind) for kind in record.kinds)
    if record.places:
        node.org_set = tuple(
            keys for place in record.places if (keys := part_keys(place))
        )
        node.org_primary = node.org_all = _all(node.org_set)
    else:
        # the places its location claims name, and every place its
        # manifest puts one of them inside
        seen = _closure(node.places, by_id)
        node.org_set = tuple(
            part
            for place_id in sorted(seen)
            if by_id[place_id].record is not None
            for part in (*by_id[place_id].place_names, *by_id[place_id].within)
        )
        node.org_loose = tuple(
            by_id[place_id].exact
            for place_id in sorted(seen)
            if by_id[place_id].record is None and by_id[place_id].exact
        )
        node.org_places = tuple(sorted(node.places))
        node.org_closure = frozenset(seen)
        node.org_primary = frozenset(
            key
            for place_id in node.places
            for key in _place_names(by_id[place_id])
        )
        node.org_all = frozenset(
            key for place_id in seen for key in _place_keys(by_id[place_id])
        )
    keys: set[BlockingKey] = {
        ("org", key) for name, _count in node.org_names for key in name
    }
    if not node.org_names:
        keys |= {
            ("orgkind", kind, place)
            for kind in node.org_kinds
            for place in node.org_all
        }
    node.raw = frozenset(keys)


# -- blocking -----------------------------------------------------------------


def _pair(first: str, second: str) -> Pair:
    return (first, second) if first < second else (second, first)


def _group(node: _Node) -> str:
    """The kinds a mention is blocked and counted with: its own, but a
    wedding of any kind together, a union of any class together, and a
    divorce, an annulment and an end of unstated kind together."""
    if node.kind in WEDDING_KINDS:
        return "wedding"
    if node.kind in UNION_KINDS:
        return "union"
    if node.kind in END_KINDS or (
        node.kind == UNSTATED and node.union is not None
    ):
        return "end"
    return node.kind


def _timed(node: _Node) -> bool:
    """A union or an event: clustered after what it takes place between."""
    return node.kind in EVENT_KINDS or node.kind in UNION_KINDS


def _people(node: _Node, index: Mapping[str, _Node]) -> list[str]:
    """The people an event or a union is keyed through: a union's couple,
    the couple of the union a wedding or an end belongs to, a birth's or
    a death's principal when it is theirs, or else every participant."""
    if node.kind in UNION_KINDS:
        return node.couple
    if node.union is not None:
        return index[node.union].couple
    if node.principal is not None:
        return [node.principal]
    return sorted(node.participants)


def _raw_keys(node: _Node, index: Mapping[str, _Node]) -> set[BlockingKey]:
    """The keys a mention would be blocked on (BlockingKey): an event's
    and a union's are its people's, and without people its own name."""
    if not _timed(node):
        return set(node.raw)
    people = _people(node, index)
    if not people:
        return {("name", name) for name in node.exact}
    return {
        key
        for person in people
        for key in index[person].raw
        if key[0] != "lone"
    }


def _hubs(
    nodes: Sequence[_Node],
    index: Mapping[str, _Node],
    limit: int,
    anchored: bool,
) -> list[tuple[str, BlockingKey, list[str]]]:
    """Give each mention the keys it is blocked on: its own, but for the
    keys more than `limit` mentions of its group (_group) share (the
    hubs). A lone given name is never a hub, since it only ever meets the
    GEDCOM export; and with `anchored` nor is a key that a GEDCOM mention
    of a group the export anchors by key holds (anything but an
    organisation, an event or a union), since every mention holding it
    pairs with the export's anchors only, a pair each. Returns each hub:
    its group, the key, and the mentions holding it."""
    by_group: dict[str, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_group[_group(node)].append(node)
    hubs_found: list[tuple[str, BlockingKey, list[str]]] = []
    for group, members in sorted(by_group.items()):
        own = {node.id: _raw_keys(node, index) for node in members}
        counts = Counter(key for keys in own.values() for key in keys)
        held: set[BlockingKey] = (
            {
                key
                for node in members
                if node.source == GEDCOM
                for key in own[node.id]
            }
            if anchored
            and group != ORGANIZATION
            and not any(_timed(node) for node in members)
            else set()
        )
        hubs = {
            key
            for key, count in counts.items()
            if count > limit and key[0] != "lone" and key not in held
        }
        holders: dict[BlockingKey, list[str]] = defaultdict(list)
        for node in members:
            node.blocking = frozenset(own[node.id] - hubs)
            for key in own[node.id] & hubs:
                holders[key].append(node.id)
        hubs_found.extend(
            (group, key, sorted(holders[key])) for key in sorted(hubs)
        )
    return hubs_found


class _Keys:
    """Mentions by the keys they are blocked on, each lookup reading the
    buckets of its own keys; with `lone`, a person with no surname finds
    the people of its given name too."""

    def __init__(self, nodes: Sequence[_Node], *, lone: bool) -> None:
        self._lone = lone
        self._buckets: dict[BlockingKey, list[str]] = defaultdict(list)
        self._givens: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for key in node.blocking:
                if key[0] != "lone":
                    self._buckets[key].append(node.id)
            if lone:
                for given in node.givens:
                    self._givens[given].append(node.id)

    def find(self, node: _Node) -> set[str]:
        found = {
            other
            for key in node.blocking
            if key[0] != "lone"
            for other in self._buckets.get(key, ())
        }
        if self._lone and node.source != GEDCOM:
            found.update(
                other
                for key in node.blocking
                if key[0] == "lone"
                for other in self._givens.get(key[1], ())
            )
        found.discard(node.id)
        return found


def _within(nodes: Sequence[_Node], matcher: _Keys) -> set[Pair]:
    return {
        _pair(node.id, other) for node in nodes for other in matcher.find(node)
    }


def _block_group(
    nodes: Sequence[_Node], anchored: bool
) -> tuple[set[Pair], dict[str, frozenset[str]]]:
    """The blocked pairs of the mentions of one group, and each GEDCOM
    anchor's mentions: with `anchored`, a mention that finds GEDCOM
    mentions pairs with them only, and the others among themselves."""
    if not anchored:
        return _within(nodes, _Keys(nodes, lone=False)), {}
    gedcom = [node for node in nodes if node.source == GEDCOM]
    others = [node for node in nodes if node.source != GEDCOM]
    pairs: set[Pair] = set()
    anchors: dict[str, frozenset[str]] = {}
    if gedcom:
        roster = _Keys(gedcom, lone=True)
        pairs |= _within(gedcom, roster)
        for node in others:
            found = roster.find(node)
            if found:
                anchors[node.id] = frozenset(found)
                pairs.update(_pair(node.id, other) for other in found)
    free = [node for node in others if node.id not in anchors]
    return pairs | _within(free, _Keys(free, lone=False)), anchors


def _block(
    nodes: Sequence[_Node], config: ClusteringConfig
) -> tuple[set[Pair], dict[str, frozenset[str]]]:
    """Every blocked pair of the mentions to cluster, and the GEDCOM
    anchors each anchored mention found."""
    by_group: dict[str, list[_Node]] = defaultdict(list)
    for node in nodes:
        by_group[_group(node)].append(node)
    pairs: set[Pair] = set()
    anchors: dict[str, frozenset[str]] = {}
    for _group_name, members in sorted(by_group.items()):
        if any(_timed(node) for node in members):
            # events and unions meet through their people, an export's
            # death events among them, and never through an anchor
            pairs |= _within(members, _Keys(members, lone=False))
            continue
        found, anchored = _block_group(
            members, config.blocking.gedcom_anchored
        )
        pairs |= found
        anchors.update(anchored)
    return pairs, anchors


# -- constraints --------------------------------------------------------------


def _facts(node: _Node) -> PairFacts:
    """The impossibility filter's input, lifted: the mention's fingerprint
    stands in for a QID, and its kin are the mentions its manifest relates
    it to."""
    return PairFacts(kind=node.kind, qid=node.id, kin_qids=node.kin_ids)


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
    return PlaceContainment(ancestors)


def _apart(first: _Node, second: _Node) -> bool:
    """Whether one manifest keeps the two apart (soft cannot-link)."""
    return first.sha1 == second.sha1 and first.exclusive and second.exclusive


# -- evidence -----------------------------------------------------------------


def _one_given(a: _Node, one: _Given, b: _Node, other: _Given) -> bool:
    """Whether two given parts that share a key may be one name
    (ADR-0035 §7, rules 1 and 3). A given part is a given name or a word
    of an `unsplit` form, which states no standard form. Not two given
    parts whose records give standard forms that share no key; not two
    nicknames whose holders' records both give given names, sharing no
    key; and not a given part against a nickname where the given part
    states a standard form that shares no key with the nickname holder's
    given names. A nickname holder with no given names bars nothing."""
    if one.nickname and other.nickname:
        return not (
            a.given_names
            and b.given_names
            and not a.given_names & b.given_names
        )
    if one.nickname or other.nickname:
        # a given part against the other's nickname: it counts unless
        # it states a standard form the nickname holder's given names
        # share no key with
        given, named = (other, a) if one.nickname else (one, b)
        return not (
            given.standard
            and named.given_names
            and not given.standard & named.given_names
        )
    return not (
        one.standard and other.standard and not one.standard & other.standard
    )


def _strong_shared(a: _Node, b: _Node) -> bool:
    """Whether two people share a key that may make a link confident,
    through given parts that may be one name (_one_given)."""
    return any(
        _one_given(a, one, b, other)
        for given, _surname in sorted(a.strong & b.strong)
        for one in a.given_parts
        if given in one.keys
        for other in b.given_parts
        if given in other.keys
    )


def _one_event(first: str, second: str) -> bool:
    """Whether two event or union kinds can be one: the same kind, or
    two unions of any class; a wedding
    of unstated kind and a civil or a religious one, but never a civil
    and a religious one, which are two ceremonies; an end of unstated
    kind and any end."""
    kinds = {first, second}
    if first == second or kinds <= UNION_KINDS:
        return True
    if kinds <= WEDDING_KINDS:
        return "wedding" in kinds
    return kinds <= END_KINDS | {UNSTATED} and UNSTATED in kinds


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

    def node(self, node_id: str) -> _Node:
        return self._nodes[node_id]

    def life(self, a: _Node, b: _Node) -> str:
        """Whether two people's births and deaths, each compared with
        each, agree, conflict or say nothing."""
        return _combine_dates(
            _dates(a.births, b.births, self._widest),
            _dates(a.deaths, b.deaths, self._widest),
        )

    def name_level(self, a: _Node, b: _Node) -> str:
        """How two mentions' names match: for a recordless mention, or one
        of a kind without records, `exact` on a shared normalised name;
        for people `key`, `weak` (a shared key that may not make a link
        confident: one only a GEDCOM unsplit form gives, or one through
        given parts that cannot be one name, _one_given) or `given` (a
        lone given name); for places `exact`, `nested` or
        `key` (a shared name, containers that differ); for organisations
        `exact`, `key` or `kind` (_organizations); for families `key`;
        else `none`."""
        if a.record is None or b.record is None:
            return "exact" if a.exact & b.exact else "none"
        if a.kind == HUMAN:
            if _strong_shared(a, b):
                return "key"
            if a.keys & b.keys:
                return "weak"
            if a.lone & b.givens or b.lone & a.givens:
                return "given"
            return "none"
        if a.kind == PLACE_KIND:
            return self._places(a, b)
        if a.kind == ORGANIZATION:
            return self._organizations(a, b)[0]
        return "key" if a.family & b.family else "none"

    @staticmethod
    def _places(a: _Node, b: _Node) -> str:
        """Two places sharing a name: `exact` when their containers (its
        record's `within` and the places its manifest puts it in) are one
        set, `nested` when one's are all among the other's, `key`
        otherwise; `none` without a shared name."""
        if not any(
            one & other for one in a.place_names for other in b.place_names
        ):
            return "none"
        among = [
            all(any(one & other for other in theirs) for one in ours)
            for ours, theirs in (
                (a.containing, b.containing),
                (b.containing, a.containing),
            )
        ]
        if all(among):
            return "exact"
        return "nested" if any(among) else "key"

    def _located(self, a: _Node, b: _Node) -> str:
        """Whether the places two organisations' location claims name
        agree (ADR-0035 §7, rule 6): a place of one would link with a place
        of the other, or with a place it lies in, under rule 5 (`exact` or
        `nested`). A place is one of its own manifest's nodes, and two
        organisations of one manifest are never a candidate pair, so a
        place shared by node needs no test of its own. They are `unknown`
        when neither holds but a recordless place shares a name with the
        other's, and differ otherwise."""
        unknown = False
        for ours, theirs in ((a, b), (b, a)):
            for place_id in ours.org_places:
                one = self._nodes[place_id]
                for other_id in sorted(theirs.org_closure):
                    other = self._nodes[other_id]
                    if one.record is None or other.record is None:
                        unknown = unknown or bool(
                            _place_names(one) & _place_names(other)
                        )
                    elif self._places(one, other) in ("exact", "nested"):
                        return "agree"
        return "unknown" if unknown else "differ"

    @staticmethod
    def _listed(a: _Node, b: _Node) -> str:
        """Whether two organisations' places agree where a record names
        one's places (ADR-0035 §7, rule 6, as rule 5 would let them): as
        one list of parts, with the places a location claim reaches and
        every place they lie in, a place without a record by its exact
        names. When one list lies among the other, as a place nests, they
        agree if a part with a record is shared, and are `unknown` if only
        a part without one is. Otherwise they differ: a namesake in
        another district, or nothing shared."""
        ours = a.org_set + a.org_loose
        theirs = b.org_set + b.org_loose
        among = [
            bool(one) and all(any(p & q for q in two) for p in one)
            for one, two in ((ours, theirs), (theirs, ours))
        ]
        if not any(among):
            return "differ"
        if any(p & q for p in a.org_set for q in b.org_set):
            return "agree"
        return "unknown"

    def _organizations(self, a: _Node, b: _Node) -> tuple[str, str]:
        """(name, places) of two organisations. The name is `exact` for a
        shared proper name of two words or more, `key` for one of one
        word, `kind` for a shared kind where neither has a proper name
        and their places agree, else `none`. The places are `unknown`
        when either states none. Places both name by location claims
        agree as `_located` says; otherwise as `_listed` says."""
        if not a.org_primary or not b.org_primary:
            places = "unknown"
        elif a.org_places and b.org_places:
            places = self._located(a, b)
        else:
            places = self._listed(a, b)
        shared = [
            words
            for name, words in a.org_names
            if any(name & other for other, _words in b.org_names)
        ]
        if shared:
            return ("exact" if max(shared) >= 2 else "key"), places
        if (
            not a.org_names
            and not b.org_names
            and a.org_kinds & b.org_kinds
            and places == "agree"
        ):
            return "kind", places
        return "none", places

    @staticmethod
    def blocked(a: _Node, b: _Node) -> str | None:
        """What keeps two people apart whatever else agrees: stated sexes
        that differ, or civil statuses that cannot both hold."""
        if a.sexes and b.sexes and not a.sexes & b.sexes:
            return "sex"
        if _never_married_after(a.civil, b.civil) or _never_married_after(
            b.civil, a.civil
        ):
            return "civil status"
        return None

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
        """Whether a kin of each, in one relation, shares a key that may
        make a link confident (never a surname alone), without dates that
        conflict."""
        return any(
            _strong_shared(first, second)
            and self.life(first, second) != "conflict"
            for first, second in self._kin_pairs(a, b)
        )

    def kin_conflict(self, a: _Node, b: _Node) -> bool:
        """Whether kin of one relation and one name (a key, or an exact
        name) give dates that conflict, while no kin agree."""
        return not self.kin_agree(a, b) and any(
            (first.keys & second.keys or first.exact & second.exact)
            and self.life(first, second) == "conflict"
            for first, second in self._kin_pairs(a, b)
        )

    def evidence(self, a: _Node, b: _Node) -> tuple[str, str, str]:
        """(name, dates, kin): how the names match (name_level; for events
        and unions, the best match of their people), whether the dates
        agree, conflict or say nothing, and whether kin agree (for
        organisations, whether their places agree or differ)."""
        if _timed(a):
            levels = {
                self.name_level(self._nodes[first], self._nodes[second])
                for first in _people(a, self._nodes)
                for second in _people(b, self._nodes)
            }
            best = next(
                (level for level in _LEVELS if level in levels), "none"
            )
            return best, _dates(a.when, b.when, self._widest), "unknown"
        if (
            a.kind == ORGANIZATION
            and a.record is not None
            and b.record is not None
        ):
            name, places = self._organizations(a, b)
            return name, self.life(a, b), places
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
        """(the link, the evidence as name/dates/kin, and what blocks the
        pair when something does) of a candidate pair of any kind but an
        event's or a union's."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        blocked = self.blocked(a, b) if a.kind == HUMAN else None
        if blocked:
            return AMBIGUOUS, f"{described}/{blocked}"
        if dates == "conflict" or not a.kind:
            return AMBIGUOUS, described
        if a.recordless or b.recordless:
            return AMBIGUOUS, described
        if any(
            self._anchors_counted(node) > self._max_anchors for node in (a, b)
        ):
            return AMBIGUOUS, described
        if a.kind == HUMAN:
            corroborated = kin == "agree" or dates == "agree"
            confident = (
                name == "key" and corroborated and not self.kin_conflict(a, b)
            )
        elif a.kind == PLACE_KIND:
            confident = name in ("exact", "nested")
        elif a.kind == ORGANIZATION:
            # a one-word name (a kind with a surname, as in "Firma
            # Löbig", may be any Löbig's), like a kind alone, links only
            # on places that agree
            confident = kin != "differ" and (
                name == "exact" or (name in ("key", "kind") and kin == "agree")
            )
        elif a.kind == FAMILY:
            # a surname names many families: every pair is adjudicated
            confident = False
        else:
            confident = name == "exact"
        return (CONFIDENT if confident else AMBIGUOUS), described

    def event_link(
        self, a: _Node, b: _Node, merged: Callable[[str, str], bool]
    ) -> tuple[str, str]:
        """(the link, the evidence) of a candidate pair of events or of
        unions, given whether two mentions ended merged (the module
        docstring says when each is confident)."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict" or not _one_event(a.kind, b.kind):
            return AMBIGUOUS, described
        if a.kind in UNION_KINDS:
            first, second = a.couple, b.couple
            confident = len(first) == len(second) == 2 and (
                (merged(first[0], second[0]) and merged(first[1], second[1]))
                or (
                    merged(first[0], second[1]) and merged(first[1], second[0])
                )
            )
        elif a.union is not None or b.union is not None:
            confident = (
                a.union is not None
                and b.union is not None
                and merged(a.union, b.union)
                and dates == "agree"
            )
        elif a.kind in ONE_PER_PERSON or b.kind in ONE_PER_PERSON:
            # a birth or death with no principal, or several, is nobody's
            # own, and meets nothing through its other participants
            confident = (
                a.principal is not None
                and b.principal is not None
                and merged(a.principal, b.principal)
            )
        elif dates != "agree":
            confident = False
        else:
            fewer, more = sorted(
                (sorted(a.participants), sorted(b.participants)), key=len
            )
            confident = bool(fewer) and all(
                any(merged(one, other) for other in more) for one in fewer
            )
        return (CONFIDENT if confident else AMBIGUOUS), described

    def contradicts(
        self, a: _Node, b: _Node, *, dates_count: bool = True
    ) -> str | None:
        """The evidence of two members of one cluster, when it says they
        differ (dates that conflict; for events, kinds that cannot be one;
        for people, what blocks a pair, kin that conflict or no name
        match; for places, names neither exact nor nested; for
        organisations, places that differ or no name match; for other
        kinds but events and unions, no name match); None when it does
        not. Without `dates_count`, the two's own dates that conflict are
        no contradiction: what an adjudicator's `same` on the pair
        excuses."""
        name, dates, kin = self.evidence(a, b)
        described = f"{name}/{dates}/{kin}"
        if dates == "conflict" and dates_count:
            return described
        if _timed(a):
            return None if _one_event(a.kind, b.kind) else described
        if a.kind == PLACE_KIND:
            return None if name in ("exact", "nested") else described
        if a.kind == HUMAN:
            blocked = self.blocked(a, b)
            if blocked:
                return f"{described}/{blocked}"
            if self.kin_conflict(a, b):
                return described
        if kin == "differ":
            return described
        return described if name == "none" else None

    def _signature(self, node: _Node) -> tuple:
        """Everything the evidence of a pair reads of one of its members:
        two members of one signature give every pair the same evidence."""

        def dates(intervals: list[Interval]) -> tuple:
            return tuple(sorted((i.start, i.end) for i in intervals))

        def person(of: _Node) -> tuple:
            return (
                of.record is None,
                of.exact,
                of.keys,
                of.strong,
                of.lone,
                of.givens,
                of.given_parts,
                of.given_names,
                dates(of.births),
                dates(of.deaths),
            )

        return (
            node.kind,
            person(node),
            frozenset(node.place_names),
            frozenset(node.within),
            frozenset(node.containing),
            frozenset(node.org_names),
            node.org_kinds,
            node.org_primary,
            node.org_all,
            node.org_places,
            node.org_closure,
            node.org_set,
            node.org_loose,
            node.family,
            frozenset(node.sexes),
            tuple(sorted((s, i.start, i.end) for s, i in node.civil)),
            dates(node.when),
            frozenset(
                (relation, person(kin))
                for relation, kin_id in node.kin
                if (kin := self._nodes.get(kin_id)) is not None
            ),
            frozenset(
                person(self._nodes[person_id])
                for person_id in _people(node, self._nodes)
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
        two members out (a kin edge, or a member place containing
        another), soft when they are only two ids of one manifest."""
        inside = set(members)
        nodes = [self._nodes[member] for member in sorted(members)]
        for node in nodes:
            related = node.kin_ids & inside
            related |= self._containment.containers(node.id) & inside
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
    pair that makes it conflict, or the two members that contradict. An
    ambiguous one, and one adjudication settled (ADJUDICATED), holds the
    clusters it ends in (`parts`), each with its method, and the
    ambiguous pairs a merged confident component holds (`inside`)."""

    members: list[str]
    status: str
    pairs: list[Pair]
    conflict: tuple[str, Pair] | None = None
    contradiction: dict[str, Any] | None = None
    parts: list[tuple[list[str], str]] | None = None
    inside: frozenset[Pair] = frozenset()

    def clusters(self) -> list[tuple[list[str], str]]:
        """The clusters its members end in, each with its method: one of
        them all when it is confident, its parts (its confident
        components, or what adjudication made of it), or else each
        member alone."""
        if self.parts is not None:
            return self.parts
        if self.status == CONFIDENT:
            return [(self.members, CONFIDENT)]
        return [([member], self.status) for member in self.members]


def _confident_components(
    members: Sequence[str],
    own: Sequence[Pair],
    links: Mapping[Pair, tuple[str, str]],
    judge: _Judge,
    conflicts: _Conflicts,
    verdicts: Mapping[Pair, str],
) -> tuple[list[tuple[list[str], str]], dict[str, Any] | None]:
    """The confident components of an ambiguous candidate cluster (the
    closure over its confident links), each re-checked as the cluster
    check checks a cluster, and the first contradiction found. A component
    of two or more merges, CONFIDENT_COMPONENT, unless it joins a
    cannot-link pair, holds a pair said `different` (a cannot-link too),
    or holds two members whose evidence says they differ
    (_Judge.contradiction); then each of its members stays apart,
    `ambiguous`, as does a member no confident link reaches."""
    confident = [pair for pair in own if links[pair][0] == CONFIDENT]
    parts: list[tuple[list[str], str]] = []
    contradiction = None
    for component in _components(members, confident).groups():
        if len(component) > 1:
            inside = set(component)
            said_different = sorted(
                pair
                for pair in own
                if verdicts.get(pair) == DIFFERENT
                and pair[0] in inside
                and pair[1] in inside
            )
            conflict = conflicts.find(component)
            if conflict is not None:
                found = {
                    "mentions": list(conflict[1]),
                    "evidence": conflict[0],
                }
            elif said_different:
                found = {
                    "mentions": list(said_different[0]),
                    "evidence": DIFFERENT,
                }
            else:
                found = judge.contradiction(component)
            if found is None:
                parts.append((component, CONFIDENT_COMPONENT))
                continue
            contradiction = contradiction or found
        parts.extend(([member], AMBIGUOUS) for member in component)
    return parts, contradiction


def _inside(
    pairs: Iterable[Pair],
    links: Mapping[Pair, tuple[str, str]],
    components: Sequence[tuple[list[str], str]],
) -> frozenset[Pair]:
    """The ambiguous pairs whose two mentions one merged confident
    component holds: the component settles them."""
    merged = {
        member: position
        for position, (part, method) in enumerate(components)
        if method == CONFIDENT_COMPONENT
        for member in part
    }
    return frozenset(
        pair
        for pair in pairs
        if links[pair][0] == AMBIGUOUS
        and pair[0] in merged
        and merged[pair[0]] == merged.get(pair[1])
    )


def _adjudicated(
    members: Sequence[str],
    own: Sequence[Pair],
    links: Mapping[Pair, tuple[str, str]],
    judge: _Judge,
    verdicts: Mapping[Pair, str],
    components: Sequence[tuple[list[str], str]],
    inside: frozenset[Pair],
) -> _Settled | None:
    """An ambiguous candidate cluster as adjudication settles it, or None
    while it has no pending pair (an ambiguous pair no merged confident
    component holds, `inside`), or any of them has no verdict, or
    `uncertain`. A `different` pair is cut; the closure over the
    confident links and the pending `same` pairs gives its parts. A part
    merges unless it holds a pair contradicted: a `different` pair the
    closure joined another way, a `same` pair whose evidence says they
    differ by more than their own dates (stated sexes, civil statuses,
    kin), or any other two members whose evidence says they differ; then
    it falls back to its confident `components`, each merged or apart as
    it is without verdicts. A merged part's method is `adjudicated` when
    a `same` pair is among its links, `confident_component` when its
    confident links alone join it; a member a `different` pair left alone
    is `adjudicated`."""
    ambiguous = [pair for pair in own if links[pair][0] == AMBIGUOUS]
    pending = [pair for pair in ambiguous if pair not in inside]
    if not pending or not all(
        verdicts.get(pair) in (SAME, DIFFERENT) for pair in pending
    ):
        return None
    said_same = {pair for pair in pending if verdicts.get(pair) == SAME}
    joined = [
        pair
        for pair in own
        if links[pair][0] == CONFIDENT or pair in said_same
    ]
    said_different = {
        pair for pair in ambiguous if verdicts.get(pair) == DIFFERENT
    }

    def differ(first: str, second: str) -> dict[str, Any] | None:
        pair = (first, second)
        if pair in said_different:
            return {"mentions": list(pair), "evidence": DIFFERENT}
        evidence = judge.contradicts(
            judge.node(first),
            judge.node(second),
            dates_count=pair not in said_same,
        )
        return (
            {"mentions": list(pair), "evidence": evidence}
            if evidence
            else None
        )

    def first_difference(part: Sequence[str]) -> dict[str, Any] | None:
        for position, first in enumerate(part):
            for second in part[position + 1 :]:
                found = differ(first, second)
                if found is not None:
                    return found
        return None

    parts: list[tuple[list[str], str]] = []
    contradiction = None
    for part in _components(members, joined).groups():
        found = first_difference(part)
        if found is not None:
            contradiction = contradiction or found
            within = set(part)
            parts.extend(
                component
                for component in components
                if component[0][0] in within
            )
        elif len(part) > 1 and not any(
            pair[0] in part and pair[1] in part for pair in said_same
        ):
            parts.append((part, CONFIDENT_COMPONENT))
        else:
            parts.append((part, ADJUDICATED))
    return _Settled(
        list(members),
        ADJUDICATED,
        list(own),
        contradiction=contradiction,
        parts=parts,
        inside=inside,
    )


def _settle(
    ids: Iterable[str],
    links: Mapping[Pair, tuple[str, str]],
    judge: _Judge,
    conflicts: _Conflicts,
    verdicts: Mapping[Pair, str],
) -> list[_Settled]:
    """The candidate clusters the links join among `ids`, each settled
    (the module docstring says how): an ambiguous one by its confident
    components (_confident_components), and by the adjudicator's
    `verdicts` where they settle it (_adjudicated)."""
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
            components, contradiction = _confident_components(
                members, own, links, judge, conflicts, verdicts
            )
            inside = _inside(own, links, components)
            settled.append(
                _adjudicated(
                    members, own, links, judge, verdicts, components, inside
                )
                or _Settled(
                    members,
                    AMBIGUOUS,
                    own,
                    contradiction=contradiction,
                    parts=components,
                    inside=inside,
                )
            )
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


def stage_group(kind: str | None) -> int:
    """The order a kind's clusters are settled in: people, places and
    everything else (0), then unions (1), then events (2), since a
    union's link reads the clusters its couple end in, and an event's
    those of its people or its union."""
    if kind in UNION_KINDS:
        return 1
    return 2 if kind in EVENT_KINDS else 0


def cannot_merge(entry: Mapping[str, Any]) -> bool:
    """Whether a review entry is a candidate cluster of two members whose
    one pair no `same` verdict could merge (`same_merges` false): every
    verdict on it leaves the two apart, so in a run whose verdicts merge
    asking it changes no cluster. A pair in a larger cluster is asked
    still, since its `different` can settle the cluster."""
    pairs = entry.get("pairs") or ()
    return (
        len(entry.get("members") or ()) == 2
        and len(pairs) == 1
        and pairs[0].get("same_merges") is False
    )


def pending_pairs(
    review: Iterable[Mapping[str, Any]], *, merging: bool = False
) -> list[tuple[int, list[Pair]]]:
    """The ambiguous pairs of each ambiguous candidate cluster of a map's
    review that neither a merged confident component (`inside_component`)
    nor a verdict settles yet (none given, or an answer that gave none),
    with the cluster's stage_group, in the review's order. A conflicting
    cluster never merges, so none of its pairs is pending; nor, when the
    verdicts merge (`merging`), is a cluster no verdict could merge
    (cannot_merge)."""
    found = []
    for entry in review:
        if entry.get("status") != AMBIGUOUS:
            continue
        if merging and cannot_merge(entry):
            continue
        pairs = [
            _pair(*pair["mentions"])
            for pair in entry.get("pairs") or ()
            if pair.get("link") == AMBIGUOUS
            and pair.get("verdict") is None
            and not pair.get("inside_component")
        ]
        if pairs:
            found.append((stage_group(entry.get("kind")), pairs))
    return found


def pending_sizes(review: Iterable[Mapping[str, Any]]) -> dict[int, list[int]]:
    """How many pairs are pending (pending_pairs) in each cluster, by its
    stage group, in a run whose verdicts merge."""
    sizes: dict[int, list[int]] = defaultdict(list)
    for group, pairs in pending_pairs(review, merging=True):
        sizes[group].append(len(pairs))
    return dict(sizes)


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


def verdicts_digest(verdicts: Mapping[Pair, str]) -> str:
    """The SHA-256 of the verdicts an identity map was made under."""
    return sha256_text(
        canonical_json(
            sorted([*pair, verdict] for pair, verdict in verdicts.items())
        )
    )


def inputs_key(
    manifests: Sequence[Manifest],
    config: ClusteringConfig,
    verdicts: Mapping[Pair, str] | None = None,
) -> str:
    """The key an identity map is reused under: VERSION, the digest of
    CODE's source, the version of the Unicode database that normalises and
    folds names, the thresholds' digest, and each document's SHA-1,
    source and batch content; and, for a map made under an adjudicator's
    verdicts, their digest."""
    inputs = {
        "stage": STAGE,
        "version": VERSION,
        "code": code_digest(),
        "unicode": unicodedata.unidata_version,
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
    if verdicts is not None:
        inputs["verdicts"] = verdicts_digest(verdicts)
    return sha256_text(canonical_json(inputs))


@dataclass(frozen=True)
class _Corpus:
    """The manifests, one per document in SHA-1 order, and their mentions
    lifted and keyed: every mention by fingerprint, those to cluster, and
    the hubs none of them is blocked on (_hubs)."""

    manifests: list[Manifest]
    nodes: list[_Node]
    index: dict[str, _Node]
    clustered: list[_Node]
    hubs: list[tuple[str, BlockingKey, list[str]]]


def _corpus(
    manifests: Sequence[Manifest], config: ClusteringConfig
) -> _Corpus:
    ordered = _distinct(manifests)
    nodes = [
        node
        for manifest in ordered
        for node in _nodes(manifest, config.evidence.circa_years)
    ]
    _derive(nodes)
    index = {node.id: node for node in nodes}
    clustered = [node for node in nodes if node.mention.extract]
    hubs = _hubs(
        clustered,
        index,
        config.blocking.max_key_mentions,
        config.blocking.gedcom_anchored,
    )
    return _Corpus(
        manifests=ordered,
        nodes=nodes,
        index=index,
        clustered=clustered,
        hubs=hubs,
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
    manifests: Sequence[Manifest],
    config: ClusteringConfig | None = None,
    verdicts: Mapping[Pair, str] | None = None,
) -> dict[str, Any]:
    """The identity map of the manifests under the thresholds (by default
    clustering.yaml's), as identity-map.json holds it: the module
    docstring says how it is made and what it holds. Two manifests of one
    SHA-1 are one document: the first is taken. With `verdicts`, an
    adjudicator's on ambiguous pairs (each pair its two fingerprints,
    sorted), each ambiguous candidate cluster they settle is settled by
    them (the module docstring, "Adjudication")."""
    config = config or load_config()
    settling: Mapping[Pair, str] = verdicts or {}
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

    # people, places and everything else first, then unions, then
    # events: a union's link reads the clusters its couple end in, and an
    # event's those of its people or its union
    links: dict[Pair, tuple[str, str]] = {}
    settled: list[_Settled] = []
    together: dict[str, str] = {}

    def merged(first: str, second: str) -> bool:
        return first in together and together[first] == together.get(second)

    def stage(of: Callable[[_Node], bool], link: Callable) -> None:
        found = {
            pair: link(index[pair[0]], index[pair[1]])
            for pair in candidates
            if of(index[pair[0]])
        }
        entries = _settle(
            (node.id for node in clustered if of(node)),
            found,
            judge,
            conflicts,
            settling,
        )
        links.update(found)
        settled.extend(entries)
        together.update(
            (member, part[0])
            for entry in entries
            for part, method in entry.clusters()
            if method in MERGING
            for member in part
        )

    def event_link(a: _Node, b: _Node) -> tuple[str, str]:
        return judge.event_link(a, b, merged)

    stage(lambda node: not _timed(node), judge.link)
    stage(lambda node: node.kind in UNION_KINDS, event_link)
    stage(lambda node: node.kind in EVENT_KINDS, event_link)

    clusters: dict[str, dict[str, Any]] = {}
    assigned: dict[str, str] = {}
    status: dict[str, str] = {}
    review: list[dict[str, Any]] = []
    ambiguous_groups: list[int] = []
    all_groups: list[int] = []
    for entry in sorted(settled, key=lambda entry: entry.members):
        for part, method in entry.clusters():
            cid = cluster_id(part)
            clusters[cid] = {
                "kind": index[part[0]].kind,
                "method": method,
                "members": part,
            }
            for member in part:
                assigned[member] = cid
                status[member] = method
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
                        **(
                            {
                                # whether a `same` verdict could merge it
                                "same_merges": judge.contradicts(
                                    index[pair[0]],
                                    index[pair[1]],
                                    dates_count=False,
                                )
                                is None,
                                # whether a merged confident component
                                # holds both its mentions, and so settles it
                                "inside_component": pair in entry.inside,
                            }
                            if links[pair][0] == AMBIGUOUS
                            else {}
                        ),
                        **(
                            {"verdict": settling.get(pair)}
                            if verdicts is not None
                            and links[pair][0] == AMBIGUOUS
                            else {}
                        ),
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
    # each hub key, with the mentions holding it: no pair was blocked on it
    review.extend(
        {
            "status": HUB,
            "kind": kind,
            "members": members,
            "key": list(key),
            "pairs": [],
            "conflict": None,
            "contradiction": None,
        }
        for kind, key, members in corpus.hubs
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
    # the clusters each method merged, with their mentions; a candidate
    # cluster that left any member apart counts among the ambiguous or
    # conflicting ones, with those members; and the members a `different`
    # left alone
    by_status: Counter[str] = Counter()
    mentions_by_status: Counter[str] = Counter()
    adjudicated_apart = 0
    for entry in settled:
        if entry.status == SINGLETON:
            mentions_by_status[SINGLETON] += 1
            continue
        apart: Counter[str] = Counter()
        for part, method in entry.clusters():
            if method in MERGING and len(part) > 1:
                by_status[method] += 1
                mentions_by_status[method] += len(part)
            elif method == ADJUDICATED:
                adjudicated_apart += 1
            else:
                apart[method] += 1
        for method, left in apart.items():
            by_status[method] += 1
            mentions_by_status[method] += left
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
        "recordless_mentions": sum(node.recordless for node in nodes),
        "recordless_by_kind": dict(
            sorted(Counter(n.kind for n in nodes if n.recordless).items())
        ),
        "hub_keys": len(corpus.hubs),
        "hub_mentions": len(
            {
                member
                for _kind, _key, members in corpus.hubs
                for member in members
            }
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
            verdict: by_status[verdict] for verdict in SUMMARY_METHODS
        },
        "cluster_mentions": {
            verdict: mentions_by_status[verdict] for verdict in SUMMARY_METHODS
        },
        # the candidate clusters adjudication settled, merged or apart
        "adjudicated_candidate_clusters": sum(
            entry.status == ADJUDICATED for entry in candidate_groups
        ),
        # the members a `different` verdict left alone
        "adjudicated_apart": adjudicated_apart,
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
        "merged_mentions": sum(
            mentions_by_status[method] for method in MERGING
        ),
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
            # the calls the adjudication stage asks at most, of the
            # ambiguous pairs of the ambiguous clusters still unsettled,
            # each group of kinds packed apart (stage_group)
            "calls_ambiguous_clusters": sum(
                adjudication_calls(sizes, per_call)
                for sizes in pending_sizes(review).values()
            ),
        },
    }
    return {
        "format": FORMAT,
        "stage": STAGE,
        "version": VERSION,
        "inputs": inputs_key(ordered, config, verdicts),
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


# -- the identity map's read side ---------------------------------------------


class UnmappedMention(LookupError):
    """Mentions of a batch that the identity map it is written or resolved
    with does not hold, or a batch with mentions and no identity map: the
    message names the local ids, never a label."""

    def __init__(self, local_ids: Sequence[str], reason: str = "") -> None:
        self.local_ids = list(local_ids)
        named = ", ".join(repr(local_id) for local_id in self.local_ids)
        super().__init__(
            f"{len(self.local_ids)} mention(s) of the batch are not in the "
            f"identity map: {named}"
            + (f" ({reason})" if reason else "")
            + ". The identity map is the only way an entity finds or mints "
            "its item: cluster the batch's document with the others "
            "(yizkr.pipeline.clustering) and write it with that map. "
            "Nothing was written."
        )


@dataclass(frozen=True)
class Cluster:
    """One cluster of an identity map, as the writer reads it: its id, its
    kind (empty for none), the canonical label of each of its members in
    the order of their fingerprints, and the SHA-1s of the documents its
    members come from, each once, sorted."""

    id: str
    kind: str
    labels: tuple[str, ...]
    documents: tuple[str, ...]


class IdentityMap:
    """An identity map (cluster_corpus; the module docstring), as the
    writer and the resolve step read it: which cluster each mention of a
    batch is in (`clusters_of`), and each cluster's kind, labels and
    documents (`cluster`). It reads the map alone, never the wiki, and
    keeps what it derives from it (`memo`) for as long as it lives."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = data
        self._clusters: dict[str, Cluster] | None = None
        self._memo: dict[str, Any] = {}

    @classmethod
    def read(cls, path: Path) -> IdentityMap:
        """The identity map at `path` (read_identity_map); raises
        ClusteringInputError when there is none, or it cannot be read, is of
        another format or its decisions do not hash to its hash."""
        data = read_identity_map(path)
        if data is None:
            raise ClusteringInputError(
                f"{path.name} is no identity map this code reads: it is "
                f"missing, unreadable, of another format than {FORMAT}, or "
                f"its decisions do not hash to its hash"
            )
        return cls(data)

    @classmethod
    def of(
        cls,
        manifests: Sequence[Manifest],
        config: ClusteringConfig | None = None,
    ) -> IdentityMap:
        """The identity map of the manifests (cluster_corpus)."""
        return cls(cluster_corpus(manifests, config))

    @property
    def hash(self) -> str:
        """The map's hash: the SHA-256 of every mention's cluster and
        method (identity_hash)."""
        return self.data["hash"]

    def memo(self, name: str, compute: Callable[[], Any]) -> Any:
        """What `compute` gives, made once for this map under `name`."""
        if name not in self._memo:
            self._memo[name] = compute()
        return self._memo[name]

    def clusters(self) -> dict[str, Cluster]:
        """Every cluster of the map, by its id (Cluster)."""
        if self._clusters is None:
            mentions = self.data["mentions"]
            self._clusters = {
                cid: Cluster(
                    id=cid,
                    kind=entry.get("kind") or "",
                    labels=tuple(
                        mentions[member]["label"]
                        for member in sorted(entry["members"])
                    ),
                    documents=tuple(
                        sorted(
                            {
                                mentions[member]["document"]
                                for member in entry["members"]
                            }
                        )
                    ),
                )
                for cid, entry in self.data["clusters"].items()
            }
        return self._clusters

    def cluster(self, cid: str) -> Cluster:
        return self.clusters()[cid]

    def clusters_of(
        self, batch: ClaimBatch, documents: Iterable[str]
    ) -> tuple[str, dict[str, str]]:
        """The document the mentions of `batch` are mapped under, and the
        cluster of each of them, local id -> cluster id. Each SHA-1 of
        `documents`, the artifacts the batch's claims cite, is tried in
        turn: a mention's fingerprint (yizkr.pipeline.fingerprints) is
        made of its document's SHA-1, its label, kind, lines and
        occurrence, never its local id. The first under which the map holds
        every mention of the batch is its document. Raises UnmappedMention,
        naming the local ids it lacks under the first SHA-1, when there is
        none."""
        mentions = self.data["mentions"]
        missing: list[str] | None = None
        doubled = False
        for sha1 in documents:
            try:
                found = mention_map(sha1, batch)
            except ValueError:
                doubled = True
                continue
            lacking = [
                local_id
                for local_id, fingerprint in found.items()
                if fingerprint not in mentions
            ]
            if not lacking:
                return sha1.lower(), {
                    local_id: mentions[fingerprint]["cluster"]
                    for local_id, fingerprint in found.items()
                }
            if missing is None:
                missing = lacking
        if missing is not None:
            raise UnmappedMention(missing)
        local_ids = list(
            dict.fromkeys(
                [entity.id for entity in batch.entities]
                + [ref.local_id for ref in local_refs(batch)]
            )
        )
        raise UnmappedMention(
            local_ids,
            "two of its mentions share a local id"
            if doubled
            else "its claims cite no artifact",
        )


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


def attention(
    summary: Mapping[str, Any], *, adjudicated: bool = False
) -> list[str]:
    """The reasons an identity map needs attention, from its summary:
    counts within labels of its own. A map the adjudication stage made
    (`adjudicated`) gives no estimate of the calls it would take."""
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
    if summary["ambiguous_pairs"] and not adjudicated:
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
            f"held by {_counted(summary['hub_mentions'], 'mention')}"
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
        f"{_counted(summary['merged_mentions'], 'mention')} merged, "
        f"{summary['cluster_mentions'][CONFIDENT_COMPONENT]} of them by the "
        "confident components of ambiguous clusters; "
        f"{summary['clusters'][AMBIGUOUS]} ambiguous and "
        f"{summary['clusters'][CONFLICTING]} conflicting clusters not "
        "merged; adjudication: about "
        f"{_counted(adjudication['calls_ambiguous_pairs'], 'call')} for "
        "the ambiguous pairs, "
        f"{_counted(adjudication['calls_all_pairs'], 'call')} for every "
        "candidate pair"
    )
    return attention(summary)
