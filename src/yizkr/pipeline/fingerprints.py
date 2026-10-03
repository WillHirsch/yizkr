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
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from yizkr.claims import ClaimBatch, ItemValue, LocalRef
from yizkr.pipeline.stages import canonical_json, sha256_text

# the leading element of the hashed list, so that a mention's fingerprint
# and a cluster's id never hash the same list
_MENTION = "mention"
_CLUSTER = "cluster"


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
