# ADR-0010: Artifact identity and near-duplicate handling

Status: accepted (content-hash key implemented; near-duplicate linking
deferred, see "Later").
Amended by #186 (2026-09-27): uploads honour MediaWiki's warnings, with a
file-naming rule for changed sources (§1, "Media files").

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

Amended by #50 (2026-09-27): media derived from a compound artifact
(ADR-0008) are artifacts with media too. They carry `media SHA-1`,
referenced `stated in →` the deck that embeds them, and are resolved by
it the same way, so one image embedded in several decks is one item,
`part of` each (see ADR-0008's amendment).

**Media files** (amended by #186, 2026-09-27). The blob deduplication
the context above takes for granted was not in effect: the upload sent
`ignorewarnings=0`, and MediaWiki reads a boolean parameter as true
whenever it is present, so every warning was ignored. Identical bytes
under a new name were stored as a second file, and a stored name with
different bytes silently gained a new version, so an earlier item's
`media file` could show other bytes than its `media SHA-1`. The rule
now, one for the registrar and for derived media:

- **A wiki file is never overwritten, and bytes are stored once.**
  `WikiClient.upload_file` sends no `ignorewarnings` and answers each
  warning. Bytes stored already (`nochange` under the requested name,
  `duplicate` under another) reuse that file, and nothing is uploaded;
  with several such files, the first by name is used.
- **File names for changed sources.** When the requested name holds
  different bytes (`exists`), the new bytes go under a content-tagged
  name, `"<stem> <sha1[:8]><extension>"` (a re-scanned
  `portrait-1890s.png` becomes `Portrait-1890s 3f2a9c1b.png`), and the
  stored file keeps its bytes and history. MediaWiki forbids brackets in
  file names, hence a space rather than the `[…]` of content-tagged
  labels. If the tagged name also holds other bytes, the upload is
  refused.
- **Warnings about the name alone** (a camera-style prefix, a
  thumbnail-like or corrected name, a near-identical name with another
  extension, a deleted file's name) are overridden deliberately: with no
  `exists` and no `duplicate`, no file is overwritten and nothing is
  stored twice. Every other warning stops the upload for a person to
  decide, notably `duplicate-archive`: those bytes were deleted from the
  wiki, and re-uploading them would undo a curator's deletion.
- **Registration uploads only what no file of the item holds.** The
  registrar, like derived-media registration, locates the item first
  and uploads only when none of the item's `media file`s currently holds
  its bytes (MediaWiki's own SHA-1 of each file). An item with no hash
  is adopted by its label only when its file holds the same bytes, or
  it has no file yet; otherwise the title names different content and
  the new item gets a content-tagged label. A file that no longer holds
  its item's bytes (overwritten before this rule) is not trusted: the
  item gains a `media file` that does hold them, and the earlier
  statement stays (CLAUDE.md rule 3).
- **Aliases follow the derived-media rule.** An alternate title for the
  same bytes becomes an alias only when it names no other item and is
  not the plain form of the item's own content-tagged label, so a
  changed source's item never takes the title of the artifact it was
  tagged against, and a re-run writes nothing.

The orchestrator adds **content dedup** ahead of the expensive steps. It
hashes each file at the top of `advance`, and a file whose bytes were
already claimed is marked `duplicate` and is *not* transcribed,
extracted, or written. "Already claimed" spans two scopes:

- **within the run** — an earlier document in the same folder pass
  (alphabetically-first is canonical, deterministically);
- **across runs** — a document ingested to completion (`written`) in a
  prior run. `run()` pre-seeds the run's seen-hashes from persisted
  per-document state (`_prior_written_hashes`), so a renamed identical
  file added later is recognised before any LLM pass, not merely deduped
  at the graph by the writer afterwards. Only `written` documents seed
  the map: an unfinished prior run must never block a fresh attempt from
  completing. The per-document `content_sha1` lives in runtime state
  (var/), not the graph — so it degrades gracefully (re-extract, writer
  still dedups) if that state is cleared.

The persisted hash also makes the pipeline notice a **changed source**:
if a file's bytes differ from the hash last processed under that name,
its old artifact/claims/reviews describe different content (a distinct
artifact), so the document is reset and re-ingested rather than trusting
stale derived state. This closes a hole the cross-run skip would
otherwise expose — a stale hash causing a genuinely-new file to be
skipped as a "duplicate" of content no longer in the graph.

Amended by #87 (2026-09-27): the reset moves the old document directory
aside whole, as `.<slug>.superseded-<n>` in the state directory, rather
than deleting it, and the bytes it recorded seed no content dedup. A
document held for unrecorded outputs after it was written still seeds
the map (docs/STAGE_STORE.md).

Together these close the "two items, one blob" leak (correctness) and
skip redundant LLM passes for renamed-identical files both within a
batch and across runs (efficiency), which were the concrete costs.

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
  orchestrator's hash dedup avoids most redundant registrations. A file
  already fully ingested under a different name — whether earlier in the
  same run or in a prior one — is now recognised and skipped before its
  transcription/extraction pass, not merely deduped at the writer.
- **Not solved yet:** re-encodes, re-scans, crops/uncrops, and the
  corroboration-inflation they cause remain until parts 2 & 3 land. The
  inconsistency report and galleries treat them as distinct artifacts
  meanwhile — visible, not silently merged.
