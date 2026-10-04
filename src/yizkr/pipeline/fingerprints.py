"""Mention fingerprints and cluster ids: identity keys that outlive a run.

A *mention* is one entity of one document's entity manifest (ADR-0007),
the GEDCOM adapter's among them. Its fingerprint hashes what the document
says about it, and never the id its manifest gives it:

- the **artifact**: the SHA-1 of the document's media (ADR-0010);
- its **canonical label**: the manifest record's label (the fullest form
  the document gives), in Unicode NFC, with every run of the characters
  `str.isspace` counts as whitespace made one space and the ends trimmed.
  Case, accents and compatibility characters (a ligature, a superscript)
  are kept: they are the document's own spelling. The label stays
  verbatim whatever the entity's mention record says of it: no record
  (`EntityRecord.structure`, ADR-0035 §8) enters a fingerprint, so a new
  reading of a label moves a mention between clusters but never renames
  it;
- its **kind**: the manifest key the record is typed with;
- its **excerpt span**: the lines of the numbered transcription that the
  manifest says mention it (`EntityRecord.mention_lines`), sorted, each
  once. The GEDCOM adapter gives a person the line its INDI record starts
  on.

A manifest's ids (``e01``, ``i8``) are names the model, or the adapter,
gives in one answer: a manifest extracted again can number its entities
otherwise, and give an id to another entity. So no id is part of a
fingerprint, and a mention keeps its fingerprint across runs for as long
as its artifact, label, kind and lines stay the same.

**Stand-ins**, where a mention lacks a component:

- *No lines*: the span is empty. That is so for the places and
  occupations the GEDCOM adapter makes, one per distinct place or
  occupation string, for its media objects, for a manifest record the
  model gave no line (or only lines outside the transcription), and for
  the refs of a batch without an entity manifest.
- *No entity manifest*: a batch whose extraction fell back to line
  ranges has no manifest, and its claims name entities by refs alone.
  Each local id its claims name is a mention: its label is the first
  label a ref to it gives (empty when none does) and its kind the first
  `instance_of` (empty when none does).
- *Equal components*: two mentions of one batch whose artifact, label,
  kind and span are all equal (two unnamed refs, say) are told apart by
  their *occurrence*: 0 for the first in the batch's order (the
  manifest's, or the order the claims first name the refs in), 1 for the
  next, and so on. Every other mention's occurrence is 0.

Every component is read from the batch and the artifact's SHA-1, so a
fingerprint is stable across runs for unchanged inputs.

A **cluster id** is the hash of the sorted fingerprints of the cluster's
members: the same members always give the same id, whatever order they
are found in, and a clustering computed again with the same output keeps
every id. A cluster of one has an id of its own, apart from its member's
fingerprint.

A *claim fingerprint* hashes what a claim asserts and where, keyed on
mentions, never on clusters:

- its **subject**: the fingerprint of the mention a local ref names
  (never the id of the cluster the mention ends in), or a QID, a
  vocabulary key or the artifact itself, each tagged with its kind;
- its **property**: the manifest key;
- its **value**, canonical: an item named the way a subject is; a
  string, an external id or a monolingual text by its canonical label
  (with the language as given); a date by its year, month, day,
  precision and calendar, every one kept as stated, so a narrowed or
  widened date has another fingerprint; a coordinate and a quantity by
  their numbers, an integral float read as its integer;
- its **qualifiers**, each a property and a canonical value, sorted,
  each kept as often as it is given: the earliest and latest bounds of a
  date are qualifiers, and are never dropped or rounded;
- its **artifact**: the SHA-1 its source names;
- its **locator**: the page number, the printed page number beside it
  (each canonical, empty for none) and the region.

The excerpt, the confidence, the judgement note, the curated flag and a
row approval are not part of it: they say how the claim was read or
reviewed, not what the source asserts where. Cluster membership is not
part of it either, so a clustering computed again with other members
leaves every claim fingerprint as it is; a run diff compares membership
apart, as merges and splits. Two claims of one batch equal in every
component share a fingerprint: they are one assertion, made twice.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    CoordinateValue,
    ExternalIdValue,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    StringValue,
    TimeValue,
    VocabRef,
)
from yizkr.pipeline.stages import canonical_json, sha256_text

# the leading element of the hashed list, so that a mention's fingerprint
# and a cluster's id never hash the same list
_MENTION = "mention"
_CLUSTER = "cluster"
_CLAIM = "claim"


def canonical_label(label: str | None) -> str:
    """A label as a fingerprint takes it: NFC (never NFKC, which would
    rewrite compatibility characters), runs of `str.isspace` characters
    made one space, the ends trimmed; empty for none."""
    if not label:
        return ""
    return " ".join(unicodedata.normalize("NFC", label).split())


def excerpt_span(lines: Iterable[int]) -> tuple[int, ...]:
    """The lines a mention is on, sorted and each once; a line number
    below 1 names no line and is left out."""
    return tuple(sorted({line for line in lines if line >= 1}))


def mention_fingerprint(
    artifact: str,
    label: str | None,
    kind: str | None,
    span: Iterable[int] = (),
    occurrence: int = 0,
) -> str:
    """The fingerprint of a mention: the SHA-256 of its artifact's SHA-1,
    its canonical label, its kind, its excerpt span and its occurrence
    (the module docstring says what each is)."""
    return sha256_text(
        canonical_json(
            [
                _MENTION,
                artifact.lower(),
                canonical_label(label),
                kind or "",
                list(excerpt_span(span)),
                occurrence,
            ]
        )
    )


def cluster_id(fingerprints: Iterable[str]) -> str:
    """The id of the cluster of these mentions: the SHA-256 of their
    fingerprints, sorted."""
    return sha256_text(canonical_json([_CLUSTER, sorted(set(fingerprints))]))


@dataclass(frozen=True)
class Mention:
    """One mention of a batch: its fingerprint and the components it is
    made of, with the batch's `local_id` for it (which is never part of
    the fingerprint) and whether its manifest marks it for extraction."""

    artifact: str
    local_id: str
    label: str
    kind: str
    span: tuple[int, ...]
    occurrence: int
    extract: bool
    fingerprint: str


def local_refs(batch: ClaimBatch) -> Iterator[LocalRef]:
    """Every local ref the batch's claims name, in claim order: the
    subject, the value, then the qualifiers' values."""
    for claim in batch.claims:
        values = [claim.subject]
        if isinstance(claim.value, ItemValue):
            values.append(claim.value.value)
        values.extend(
            qualifier.value.value
            for qualifier in claim.qualifiers
            if isinstance(qualifier.value, ItemValue)
        )
        for value in values:
            if isinstance(value, LocalRef):
                yield value


def _record_parts(
    batch: ClaimBatch,
) -> list[tuple[str, str, str, tuple[int, ...], bool]]:
    """(local id, canonical label, kind, span, extract) of each mention
    of the batch, in the batch's order."""
    if batch.entities:
        return [
            (
                entity.id,
                canonical_label(entity.label),
                entity.kind,
                excerpt_span(entity.mention_lines),
                entity.extract,
            )
            for entity in batch.entities
        ]
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for ref in local_refs(batch):
        label = canonical_label(ref.label)
        if not labels.get(ref.local_id):
            labels[ref.local_id] = label
        if not kinds.get(ref.local_id):
            kinds[ref.local_id] = ref.instance_of or ""
    return [
        (local_id, label, kinds[local_id], (), True)
        for local_id, label in labels.items()
    ]


def mentions_of(artifact: str, batch: ClaimBatch) -> list[Mention]:
    """Every mention of the batch that `artifact` (its media SHA-1)
    asserts, in the batch's order: each entity of its manifest, or, for
    a batch without one, each local id its claims name."""
    seen: Counter[tuple[str, str, tuple[int, ...]]] = Counter()
    mentions = []
    for local_id, label, kind, span, extract in _record_parts(batch):
        occurrence = seen[(label, kind, span)]
        seen[(label, kind, span)] += 1
        mentions.append(
            Mention(
                artifact=artifact.lower(),
                local_id=local_id,
                label=label,
                kind=kind,
                span=span,
                occurrence=occurrence,
                extract=extract,
                fingerprint=mention_fingerprint(
                    artifact, label, kind, span, occurrence
                ),
            )
        )
    return mentions


# -- claims -------------------------------------------------------------------


def _number(value: float) -> int | float:
    """A number as a fingerprint takes it: an integral float as its
    integer, so 2 and 2.0 hash alike."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _entity(ref: object, mentions: dict[str, str]) -> list:
    """An entity reference as a claim fingerprint takes it: a local ref
    by the fingerprint of its mention, anything else by its own key."""
    if isinstance(ref, LocalRef):
        try:
            return ["mention", mentions[ref.local_id]]
        except KeyError:
            raise ValueError(
                f"local id {ref.local_id!r} names no mention of its batch"
            ) from None
    if isinstance(ref, QidRef):
        return ["qid", ref.qid]
    if isinstance(ref, VocabRef):
        return ["vocab", ref.key]
    if isinstance(ref, ArtifactRef):
        return ["artifact", ref.sha1]
    raise TypeError(f"no fingerprint for an entity reference {ref!r}")


def canonical_value(value: object, mentions: dict[str, str]) -> list:
    """A claim's or a qualifier's value as a claim fingerprint takes it,
    tagged with its type (the module docstring says how each is read)."""
    if isinstance(value, ItemValue):
        return ["item", _entity(value.value, mentions)]
    if isinstance(value, StringValue | ExternalIdValue):
        return [value.type, canonical_label(value.value)]
    if isinstance(value, MonolingualValue):
        return [
            "monolingual",
            canonical_label(value.value.text),
            value.value.language,
        ]
    if isinstance(value, TimeValue):
        return [
            "time",
            value.year,
            value.month,
            value.day,
            value.precision,
            value.calendar,
        ]
    if isinstance(value, CoordinateValue):
        return [
            "coordinate",
            _number(value.latitude),
            _number(value.longitude),
            None if value.precision is None else _number(value.precision),
        ]
    if isinstance(value, QuantityValue):
        return ["quantity", _number(value.amount)]
    raise TypeError(f"no fingerprint for a value {value!r}")


@dataclass(frozen=True)
class ClaimPrint:
    """One claim's fingerprint and the canonical components it hashes,
    so that a diff can match claims on fewer of them (a subject and a
    property whose value changed). `subject` is the subject's canonical
    form: for a local ref, ``("mention", <its mention's fingerprint>)``."""

    artifact: str
    subject: tuple
    property: str
    value: tuple
    qualifiers: tuple
    locator: tuple
    fingerprint: str


def _frozen(value: object) -> object:
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def claim_print(claim: Claim, mentions: dict[str, str]) -> ClaimPrint:
    """The fingerprint of a claim, with its components. `mentions` maps
    each local id of the claim's batch to its mention's fingerprint
    (`mention_map`); a local ref it lacks raises ValueError."""
    source = claim.source
    region = source.region
    locator = [
        canonical_label(source.page_number),
        canonical_label(source.printed_page_number),
        None
        if region is None
        else [
            _number(region.x),
            _number(region.y),
            _number(region.width),
            _number(region.height),
        ],
    ]
    subject = _entity(claim.subject, mentions)
    value = canonical_value(claim.value, mentions)
    qualifiers = sorted(
        (
            [qualifier.property, canonical_value(qualifier.value, mentions)]
            for qualifier in claim.qualifiers
        ),
        key=canonical_json,
    )
    artifact = source.artifact.sha1
    parts = [
        _CLAIM,
        artifact,
        subject,
        claim.property,
        value,
        qualifiers,
        locator,
    ]
    return ClaimPrint(
        artifact=artifact,
        subject=_frozen(subject),
        property=claim.property,
        value=_frozen(value),
        qualifiers=_frozen(qualifiers),
        locator=_frozen(locator),
        fingerprint=sha256_text(canonical_json(parts)),
    )


def claim_fingerprint(claim: Claim, mentions: dict[str, str]) -> str:
    """The fingerprint of a claim (`claim_print` says what it takes)."""
    return claim_print(claim, mentions).fingerprint


def mention_map(artifact: str, batch: ClaimBatch) -> dict[str, str]:
    """Each local id of the batch, by the fingerprint of its mention."""
    return {
        mention.local_id: mention.fingerprint
        for mention in mentions_of(artifact, batch)
    }


def claim_prints(artifact: str, batch: ClaimBatch) -> list[ClaimPrint]:
    """The fingerprint of every claim of the batch whose mentions
    `artifact` (its media SHA-1) asserts, in claim order."""
    mentions = mention_map(artifact, batch)
    return [claim_print(claim, mentions) for claim in batch.claims]
