# ADR-0010: Artifact identity and near-duplicate handling

Status: accepted (content-hash key implemented; near-duplicate linking
deferred, see "Later")

## Context

A real archive is ingested in messy batches. The same underlying source
arrives more than once: a folder holds a file already ingested last
month, plus a renamed copy, plus a re-encode (JPEG re-save, PNG→JPG), a
re-scan at a different resolution, and a crop (or an uncrop) of an image
already present. The system must decide, for each incoming file, whether
it is a new artifact, a bit-identical copy of one it already has, or a
*version* of one — and do so without wasting storage, LLM spend, or
minting duplicate items and duplicate assertions.

Before this ADR the guarantees were uneven:

- **Blob storage** was already deduplicated: MediaWiki hashes uploaded
  files (SHA-1) and refuses to store identical bytes twice, returning
  the canonical `File:` page even under a different upload name
  (`WikiClient.upload_file`).
- **Entities** (people, places) were deduplicated by reconciliation
  (ADR-0007), and **statements** by the writer's value+qualifier
  idempotency, with an agreeing second source appending a reference
  rather than duplicating a statement.
- **Artifact items**, however, were keyed only by their human title (the
  filename stem). Identical bytes under two filenames produced **two
  artifact items sharing one blob** — and, because extraction then ran
  against each, the two were recorded as independent corroborating
  sources of the same facts, inflating apparent agreement.

Three distinct problems hide under "deduplication", and they have
different answers:

1. **bit-identical** content — decidable exactly and cheaply (a hash);
2. **near-identical** content (re-encode, re-scan, crop) — decidable
   only approximately, and identity is a **curator** judgement, never an
   automatic merge (the same principle as person identity, ADR-0007);
3. **confirmed alternate versions** — once a human says "these two
   artifacts are the same source", the graph must stop treating them as
   independent corroboration.

## Decision

Anchor artifact identity on the **content hash**, and stage the harder
problems behind it rather than conflating them.

### 1. Content hash is the artifact identity key (implemented)

A new property, **`media SHA-1`** (`P4092` semantics, a hex SHA-1 of the
media bytes), is stored on every artifact that has media, referenced
`stated in → the artifact itself` like the other registrar statements
(ADR-0003).

Registration (`register_artifact`) resolves the target item by content,
not by name:

- compute the SHA-1 of the file's bytes (before upload — the identity is
  the bytes, independent of the name MediaWiki files them under);
- look the hash up in a **lag-free index** built by enumerating the
  instance via the API (`WikiClient.string_statement_index`), never the
  search index or query service — so an artifact written moments earlier
  is found immediately, the same discipline as the label index;
- **hash hit →** reuse that item, whatever its title; if the incoming
  filename differs, keep it as an **alias** (name variants live in
  aliases — MODELING.md);
- **hash miss, but a same-title item exists with no hash yet →** adopt
  it and backfill the hash (a pre-ADR-0010 artifact);
- **hash miss, same-title item already carries a different hash →**
  genuinely different content under a colliding title: mint a new item
  and disambiguate its label with a short content tag (`"<title>
  [<sha1[:8]>]"`), so labels stay unique (the wiki's idempotency
  convention) and the two remain separate artifacts;
- **no media to hash** (re-registration of an existing artifact) → fall
  back to the label key, unchanged.

The orchestrator adds a **per-run content dedup** ahead of the expensive
steps: it hashes each file at the top of `advance`, and a file whose
bytes were already claimed by an earlier document in the run is marked
`duplicate` and is *not* transcribed, extracted, or written. The check
runs on every pass (before the register guard) so the canonical keeps
its claim across re-runs and the decision is stable; the
alphabetically-first file is canonical, deterministically.

Together these close the "two items, one blob" leak (correctness) and
skip redundant LLM passes for renamed-identical files in a batch
(efficiency), which were the two concrete costs.

### 2 & 3. Near-duplicates and versions (deferred, designed here)

These are **not** implemented; this ADR records the intended shape so
the hash key above is built to carry them.

- **Perceptual detection.** A perceptual hash (pHash/dHash) computed at
  scan time clusters near-neighbours across re-encodes, re-scans, and
  crops — cases the exact SHA-1 cannot see. Candidates are *proposed*,
  never merged.
- **Curator adjudication.** Proposed artifact equivalences go through a
  review queue mirroring the entity-equivalence flow (ADR-0007): a human
  confirms "same source / different source / one is a crop of the
  other". Identity is never auto-confirmed — the same rule as people.
- **Version vocabulary.** Confirmed relations are ordinary referenced
  statements between artifact items: a `digital representation of` /
  `version of` property (and, for crops, a region-qualified relation).
  This is the hook that fixes the inflated-corroboration semantics:
  when two artifacts are marked versions of one source, the writer's
  agreeing-source logic must count them as **one** source, not two.
- **Transcript-level near-duplicates** (identical OCR/HTR text, no
  faces) are a special case the version pass can exploit: equal
  normalized transcripts are strong evidence of the same source even
  when the bytes differ.

## Consequences

- Bit-identical duplicates cost one blob, one artifact item, and (within
  a run) one LLM pass — regardless of filename. The leak is closed and
  verified live (renamed-identical → one item; same-title-different-bytes
  → distinct items).
- The content hash is queryable graph data, so the deferred perceptual
  and version work has a stable anchor and can be added without
  reshaping registration.
- **Cost.** `string_statement_index` enumerates the instance per
  registration (like the existing label index); at archive scale this is
  the same order as work the registrar already does, and the
  orchestrator's per-run hash dedup avoids most redundant registrations.
  A cross-run "already fully ingested under a new name" file is still
  correct (the hash key reuses the item and the writer dedups the
  claims) but is not yet *skipped* before its LLM pass — a future
  optimization, not a correctness gap.
- **Not solved yet:** re-encodes, re-scans, crops/uncrops, and the
  corroboration-inflation they cause remain until parts 2 & 3 land. The
  inconsistency report and galleries treat them as distinct artifacts
  meanwhile — visible, not silently merged.
