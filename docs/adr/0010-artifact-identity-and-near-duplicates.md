# ADR-0010: Artifact identity and near-duplicate handling

Status: accepted (content-hash key implemented; near-duplicate linking
deferred, see "2 & 3. Near-duplicates and versions" and #162).
Amended by #186 (2026-09-27): uploads honour MediaWiki's warnings, with a
file-naming rule for changed sources (§1, "Media files").
Amended by #225 (2026-09-27): a document directory without
`derivations.json`, or whose files have lost their `state.json`, is set
aside like a changed source's, and no held document seeds content dedup
(§1).
Amended by #103 (2026-09-29): registration is part of the write, and
finds an artifact by the SHA-1 of its media alone. The adoption of a
same-title item without a hash and the label key for registration
without media are removed, page fragments are keyed by their artifact,
a container's derived media are registered from their record, and
registration asks the never-ingest list first (§1, "Registration at
write time").
Amended by #368 (2026-09-29): an item left with a page fragment's label
and no statements is never taken for the fragment (§1, "Registration at
write time").

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

Registration (`Registrar.register_artifact` in
`yizkr.pipeline.registrar`) resolves the target item by content alone,
never by name (amended by #103, 2026-09-29):

- compute the SHA-1 of the file's bytes (before upload — the identity is
  the bytes, independent of the name MediaWiki files them under);
- look the hash up in a **lag-free reading** of the instance through the
  API (`WikiClient.iter_entities`, which `string_statement_index` reads
  too), never the search index or query service — so an artifact written
  moments earlier is found immediately, the same discipline as the label
  index;
- **hash hit →** reuse that item, whatever its title; if the incoming
  filename differs, keep it as an **alias** (name variants live in
  aliases — MODELING.md);
- **hash miss →** mint a new item. A title never finds an item: one that
  carries the title but not this hash is another artifact, whatever hash
  it carries, and even with no hash and a file that holds these bytes.
  When the title is already another item's label, the new item's label
  takes a short content tag (`"<title> [<sha1[:8]>]"`), so labels stay
  unique (the wiki client's label index; #94 would scope that rule away
  from artifacts) and homonymous files stay separate artifacts. The tag
  is a stopgap until #94, and which of two homonyms bears it depends on
  the order they are registered in, so nothing may key on an artifact's
  label: its key is its `media SHA-1`. Titles and labels are compared
  and set in the form Wikibase stores terms in (separators and control
  characters trimmed at either end, each run of control characters
  inside made one space, then NFC), so a title in another form never
  passes for a free label. A label longer than the 250 characters
  Wikibase takes, as it is or once tagged, is refused before anything is
  uploaded or written;
- **no media to hash →** refused: there is nothing to find an artifact
  by.

Amended by #50 (2026-09-27): media derived from a compound artifact
(ADR-0008) are artifacts with media too. They carry `media SHA-1`,
referenced `stated in →` the deck that embeds them, and are resolved by
it the same way, so one image embedded in several decks is one item,
`part of` each (see ADR-0008's amendment).

**Registration at write time** (amended by #103, 2026-09-29). The
stages before the write read nothing from the wiki (ADR-0025 §2), and
claims cite their artifact by its SHA-1 (#102), so nothing before the
write needs an artifact item. `ingest run` registers a document as its
write begins, just before the claims that cite it are written, and a
write that runs again (a changed review, say) registers again, finding by
SHA-1 what the earlier one registered. A reset by itself makes no write
run again, since a written document's `write.json` stays current: it is
a scratch run, which starts from fresh state directories, whose writes
register every document on the instance its reset emptied (#200).

- **One reading per registrar.** A `Registrar` reads the instance once:
  every item's label, its `media SHA-1` values and, for an `artifact
  fragment`, the artifact and page of its `part of`. It keeps what it
  read current with what it writes, so one registrar can register a whole
  corpus while nothing else writes to the instance. `ingest run` makes
  one per write.
- **Page fragments are keyed by their artifact** (#187, absorbed by
  #103). A fragment is the item that is an `instance of artifact
  fragment` and `part of` its artifact with that page's `page number`,
  whatever its label. Fragments were keyed by the label `"<title>, page
  <n>"`, so an artifact whose title another artifact shared took the
  other's fragments, which are `part of` the other only. A new fragment
  is labelled `"<artifact label>, page <n>"`, content-tagged if that
  label is taken, and is made in one write with its statements. Its
  label and description only display the artifact's label, so that is
  cut, ending in "…", where the whole would pass 250 characters. A
  container's derived medium, `part of` it with a page qualifier too, is
  never taken for a fragment, since it is no `artifact fragment` (ADR-0008,
  as amended by #289). Nor is an item with a fragment's label and no
  statements, which a registration cut short between the two writes that
  made a fragment before #103 could leave (amended by #368): the
  fragment is made beside it, content-tagged when the leftover holds its
  label. Nothing makes such an item now, and a scratch run's reset
  removes any.
- **Derived media from their record.** A deck's images are recorded
  before the write, each by its SHA-1 (ADR-0008, as amended by #128); the
  write registers the deck, then its images from the record, each by its
  own SHA-1 under the #50 rules above and the upload rules below. Their
  bytes are uploaded from the file the record keeps, under the name
  `"<deck title> <name in the archive>"`.
- **The never-ingest list** (`yizkr.core.corpus`) is asked first. A file
  it names, by name or by SHA-1, is refused before the wiki is read or
  written and before anything is uploaded; a derived medium it names is
  left out, neither registered nor uploaded, while the rest of its
  container is registered. A source whose bytes are no longer those the
  run read is refused the same way, so no item is made for bytes its
  claims do not cite.
- **Two writes, and a registration cut short.** An artifact's own
  statements cite the item itself, so it is made in two writes: first
  with its description alone, then with its label and statements. A
  registration cut short between them leaves an item with no label and
  no statement, which no lookup finds and which takes no label from the
  artifact registered after it. Adopting such an item by its title,
  which this removes, was what kept it from lingering; a curator may
  delete it, and a scratch run's reset removes it.

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
  its bytes (MediaWiki's own SHA-1 of each file). The item is found by
  its hash alone (amended by #103): an item with the title and no hash
  is never adopted, even when its file holds the same bytes, so a new
  item with its own `media SHA-1` then shares that file. A file that no
  longer holds its item's bytes (overwritten before this rule) is not
  trusted: the item gains a `media file` that does hold them, and the
  earlier statement stays (CLAUDE.md rule 3).
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
than deleting it, and the bytes it recorded seed no content dedup.
Amended by #225 (2026-09-27): a directory without `derivations.json`,
from before the records or one that lost its record, is set aside the
same way and its document starts afresh (ADR-0032), as is one whose files
have lost the `state.json` that names the artifact they were made for;
only `written` documents seed the map (docs/LLM_CACHE.md).

Together these close the "two items, one blob" leak (correctness) and
skip redundant LLM passes for renamed-identical files both within a
batch and across runs (efficiency), which were the concrete costs.

### 2 & 3. Near-duplicates and versions (deferred, designed here)

These are **not** implemented; this ADR records the intended shape so
the hash key above is built to carry them. Tracked in #162.

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
- **Cost.** A registrar enumerates the instance once, labels and
  statements in one reading (amended by #103): once per document in
  `ingest run`, whose write makes a registrar of its own and hands the
  writer the item it registered, and once in all for a registrar that
  registers a whole corpus. The orchestrator's hash dedup avoids most
  redundant registrations. A file already fully ingested under a
  different name — whether earlier in the same run or in a prior one — is
  now recognised and skipped before its transcription/extraction pass,
  not merely deduped at the writer.
- **Homonyms** (amended by #103): two files that share a title are two
  artifacts, the later one's label content-tagged, so no artifact or
  page fragment of one is ever the other's. An item left from before
  the content hash, with a title and no hash, is never adopted: its
  bytes, registered again, make an item of their own that shares its
  file. In the development stage every instance is reset (ADR-0025), so
  no such item exists there.
- **Not solved yet:** re-encodes, re-scans, crops/uncrops, and the
  corroboration-inflation they cause remain until parts 2 & 3 land. The
  inconsistency report and galleries treat them as distinct artifacts
  meanwhile — visible, not silently merged.
