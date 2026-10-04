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
Amended by #397 (2026-09-29): a written excerpt, an image a PDF places
read as a document of its own (ADR-0008), seeds content dedup across
runs as a written document does (§1).
Amended by #369 (2026-09-29): titles are compared and set by the one rule
the writer keeps labels by, which the registrar imports from it, and an
artifact's description is sent in that form too (§1).
Amended by #403 (2026-09-29): that rule cleans a term up as Wikibase
does after trimming it, U+FFFE and U+FFFF made U+FFFD (§1). It follows
UtfNormal 4.0.0, which MediaWiki pins: a Wikibase Suite upgrade that
brings a newer UtfNormal must check the rule again, as UtfNormal's later
code also puts U+25CC before a combining mark with no base character
before it (prependIsolatedCombining).
Amended by #370 (2026-10-04): the resolve step matches a ref's label in
the form the writer looks a label up in (trimmed, one line, NFC, cut to
250 characters with its digest), so the review sheet shows the item the
writer will reuse for a label given in NFD, with whitespace around it or
longer than the instance holds, not NEW; a label holding a character
Python's Unicode tables do not know resolves to nothing, as the writer
finds no item by it. The place-hierarchy pass finds and labels its run
item in that form too.
Amended by #373 (2026-10-02): a file is uploaded under a name in the form
MediaWiki takes as it is, every letter kept, within MediaWiki's 240-byte
limit with room for the content tag, and the description registration
writes is cut to the 250 characters Wikibase takes (§1, "Two writes" and
"File names MediaWiki takes").
Amended by #371 (2026-10-02): an item whose bytes a file another item
names holds, or whose title is the label of an item with no hash, is
flagged for review as an alert of the write, and still never adopted
(§1, "Registration at write time", and "Media files"). The owner's
decisions: a flag is raised on every write that registers the item,
made or found, while its condition holds, until a curator acts (Z1); an
`ingest run` that finds a document's write current skips it, so
registers nothing and raises nothing then (a scratch run's state is
fresh, so it always writes); the run report's aggregates count the
flags by check and under the stage `write` (Z2); shared-file names
every item whose `media file` holds the bytes, by MediaWiki's list of
the files of its SHA-1 (Z3); and a GEDCOM media object's item, an
artifact kind with no file and no hash, is an item with no hash, so a
scratch run raises unhashed-title for an artifact or derived medium
registered under exactly its label, in either order of the writes (Z4,
as amended by #444). An artifact's label longer than Wikibase takes
is cut to fit, as a derived medium's is.
Amended by #431 (2026-10-02): the rule puts a term in NFC by Python's
Unicode tables, 14.0.0 on the Python 3.11 the repository pins, while the
instance does so by ICU's: Wikibase's StringNormalizer::trimToNFC and
cleanupToNFC reach UtfNormal 4.0.0's Validator::cleanUp, which MediaWiki
1.45.4 pins and which calls PHP intl's normalizer_normalize, not its own
tables, and the PHP 8.3 of wikibase/wikibase:7 (7.1.0, on Debian 12)
builds intl against libicu72, ICU 72.1, whose tables are Unicode 15.0.
Unicode's normalization stability policy (strong normalization
stability, since 4.1) makes two versions put a text in the same form
when every character in it is assigned in both, so the two differ only
on text holding a character assigned after 14.0.0; ten such have a
combining class of their own, U+1E08F among them, which the instance
puts in another order in NFC. The writer refuses such text rather than
normalise it by tables of its own: a text that holds a character
Python's tables leave unassigned (category Cn), noncharacters aside, is
not sent. The rule is broader than it need be, deliberately: of the
characters Unicode 15.0 adds, only ten can change a text's NFC, but it
refuses every character Python's tables leave unassigned, which stays
sound for any newer instance, and what it refuses beyond need shows in
`refused` and `refused_terms`. A claim whose text, or whose source
block's locator, holds one is refused whole; a description, an alias or
a type hint holding one is left out and counted (`refused_terms`, a
description only when a claim the writer writes mentions its entity);
an entity whose label holds one is neither found nor minted, and the
claims that mention it are refused. Each text is read whole, past the
cut to TERM_LIMIT or TEXT_LIMIT too, so no cut of such a text is sent:
the cuts read characters by Python's tables too, which take such a mark
for no mark. The registrar makes no alias of a title holding one, but
still sets a new artifact's label from it. It cannot tell whether such
a label is free, as the instance keeps labels in its own NFC, so a
marked title always takes the media's content tag, as a taken one does:
two same-named copies with different bytes stay two items with labels
apart, and Wikibase never refuses the second for sharing the first's
label and description, which would leave an unlabelled item behind on
every registration. A re-registration finds each by its SHA-1, as ever.
Such a title may still miss an unhashed-title flag (§1). Refusal needs
no second set of tables kept in step with a floating `:7` tag, whose
next build may bring another ICU; it holds while the interpreter's
tables are no newer than the instance's, which the writer records as
INSTANCE_UNICODE ("15.0.0") and a unit test checks, so an interpreter
with newer tables (Python 3.13's 15.1.0, 3.14's 16.0.0) fails the suite
rather than send text the instance keeps in another form (§1).
Amended by #444 (2026-10-02): a media object, or any entity of an
artifact kind, is never joined by its label to an item registration
made: an artifact, which carries a `media SHA-1`, or one of its page
fragments. Written after the artifact, the GEDCOM makes the media
object an item of its own under its label with the tag " [media
object]", and the write raises unhashed-title on every artifact
registered under that label, as it is or content-tagged (a scan and a
re-scan under one title), or holding it as a fragment's label, as
registration does when it comes second; registration compares a title,
and each of its fragments' labels, with the plain and the tagged label,
so the flag repeats on every later registration, and, the media
object's item tagged, on every later write of the GEDCOM. Resolve's
exact-label step never resolves an artifact-kind ref, nor any ref to an
artifact or a part of one. The owner's decisions on the review (AK to
AO), each in §1 ("What is still matched by label"): a media object whose
label and tagged label registered items both hold is refused, with the
claims that name it, before the first write, each artifact registered
under its label raised as an alert and any other refusal warned of, the
rest of the batch written (AK); the alert counts that depend on
which of the two writes again are accepted (AL); an artifact-kind entity
joins by its label only an item with no hash that is itself an artifact
or a part of one, never a person, a place or another item (AM); a
person, a place or any other entity never joins by its label an
artifact or a part of one, and takes an item of its own under its label
tagged (AN); and a first dry run reports the pair a write would flag
against the media object's placeholder (AO).
On the second review (amended by #444, 2026-10-03): every entity not
on the artifact side (of an artifact kind or of artifact fragment) takes
one shared tag, `" [not an artifact]"`, whatever its kind or none, so
refs of two kinds naming one person meet on one item, as they do under a label no artifact holds
(AZ); an `instance of` counts whatever its rank, as a `media SHA-1`
does (AY); an entity of the kind artifact fragment is on the artifact
side, in the writer and in resolve, as its item is an artifact's part;
and only an artifact's own content tag makes a label one of its
title's.
With the rule of #431 (amended by #444, 2026-10-03): the plan is made
without the claims refused before it (for their text, or as a
misplaced document subject, ADR-0035 §3.4), so an entity whose label
holds a character Python's tables do not know is never placed nor its
label looked up, and a media object named only by claims refused
before the plan is never placed either, so it is neither blocked nor
flagged; a description is named in `refused_terms` only when a claim
written mentions its entity, not a claim refused before the plan nor
one refused with an entity the plan blocks; a manifest record that only
such refused claims name by a local ref has nothing of it sent, so its
aliases and type hint are not named in `refused_terms` either, nor is
it matched to an item by its label; and the kind of a ref without a
manifest record is warned of, when the vocabulary lacks it, only when a
written claim mentions it, while a record's kind is warned of whatever
its claims.
The kinds still matched by label, and why, are listed in §1 ("What is
still matched by label").

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
  Such an item is flagged, never adopted (amended by #371,
  2026-10-02): when another item names as its `media file` a wiki file
  that holds its bytes, any MediaWiki lists for its SHA-1
  (`shared-file`), or its title, or its own label before its content
  tag, is the label of an item with no hash (`unhashed-title`). An item
  with no hash has no `media SHA-1` and names a `media file`, as an
  artifact registered before content hashing does, or is an `instance
  of` an artifact kind, as a GEDCOM media object's item is (ADR-0021: a
  label, no file); a fragment, a run item or a person naming no file is
  never one. `ingest
  run` raises each flag as an alert of the write, naming the item and
  the others by QID alone (docs/ANOMALIES.md), for a person to decide
  whether they are one artifact, on every write that registers the
  item, made or found, while the condition holds: it repeats until a
  curator hashes, merges or relabels the older item. The flag changes
  nothing registration finds or writes. Registration writes `media
  file` and `media SHA-1` together, and nothing else writes `media
  file`, so on a reset instance `shared-file` is never raised; a
  scratch run imports the GEDCOM, so `unhashed-title` is raised there
  for an artifact or derived medium whose title (a derived medium's
  first label), or own label before its content tag, is exactly a media
  object's label: its title, or without one a fallback such as its
  file's name (ADR-0021), whichever of the two is written first
  (amended by #444, 2026-10-02): written after the artifact, the
  GEDCOM's write makes the media object an item of its own, labelled
  `"<label> [media object]"` (the writer's `media_object_label`), never
  the artifact, and raises the flag on the artifact against it, its
  `medium` the artifact's SHA-1, as registration does; registration
  compares a title with that tagged label too. The same holds for every
  artifact registered under that label with a content tag (a re-scan
  under the scan's title), and for an artifact one of whose page
  fragments has the media object's label (`"<title>, page <n>"`): the
  media object is never joined to the fragment, and the flag is on the
  fragment's artifact.
  When the title is already another item's label, the new item's label
  takes a short content tag (`"<title> [<sha1[:8]>]"`), so labels stay
  unique (the wiki client's label index; #94 would scope that rule away
  from artifacts) and homonymous files stay separate artifacts. The tag
  is a stopgap until #94, and which of two homonyms bears it depends on
  the order they are registered in, so nothing may key on an artifact's
  label: its key is its `media SHA-1`. Titles and labels are compared
  and set in the form Wikibase stores terms in, by the rule the writer
  keeps labels by (its `stored_term`): each run of control characters
  and line or paragraph separators made one space, the separators and
  the Mongolian vowel separator trimmed at either end, then U+FFFE and
  U+FFFF made U+FFFD, then NFC. So a title in another form never passes
  for a free label, and a title and a label the instance keeps alike are
  alike to the registrar and the writer, save a title that holds a
  character Python's Unicode tables do not know, which the instance may
  keep in another form (amended by #431): such a title never becomes an
  alias, and is still a new artifact's label, always content-tagged, as
  whether another item holds it cannot be told, but it may miss an
  unhashed-title flag. An artifact's description goes in that form
  too. A label longer than the 250 characters Wikibase takes, as it is
  or once tagged, is cut to fit, ending in "…", before its tag (amended
  by #371), as a derived medium's is;
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
  label is taken, or if it holds a character Python's Unicode tables do
  not know and bears no tag yet (#431), and is made in one write with
  its statements. Its
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
  `"<deck title> <name in the archive>"`, in the form MediaWiki takes
  (amended by #373; "File names MediaWiki takes" below).
- **The never-ingest list** (`yizkr.core.corpus`) is asked first. A file
  it names, by name or by SHA-1, is refused before the wiki is read or
  written and before anything is uploaded; a derived medium it names is
  left out, neither registered nor uploaded, while the rest of its
  container is registered. A source whose bytes are no longer those the
  run read is refused the same way, so no item is made for bytes its
  claims do not cite.
- **Two writes, and a registration cut short.** An artifact's own
  statements cite the item itself, so it is made in two writes: first
  with its description alone, then with its label and statements. The
  description is put in the stored form, then cut, ending in "…" and
  never between a letter and its combining marks, to the 250 characters
  Wikibase takes (amended by #373): the one `ingest run` writes, ``"ingested by
  `yizkr ingest run` from <file name>"``, passes that for a file name of
  more than 214 characters, and Wikibase refused it, so no item was
  made. A registration cut short between the two writes leaves an item
  with no label and no statement, which no lookup finds and which takes
  no label from the artifact registered after it. Adopting such an item
  by its title, which this removes, was what kept it from lingering; a
  curator may delete it, and a scratch run's reset removes it.

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
  item with its own `media SHA-1` then shares that file, and is flagged
  for it on every registration while the other item names it (amended
  by #371, 2026-10-02). A file that no
  longer holds its item's bytes (overwritten before this rule) is not
  trusted: the item gains a `media file` that does hold them, and the
  earlier statement stays (CLAUDE.md rule 3).
- **Aliases follow the derived-media rule.** An alternate title for the
  same bytes becomes an alias only when it names no other item and is
  not the plain form of the item's own content-tagged label, so a
  changed source's item never takes the title of the artifact it was
  tagged against, and a re-run writes nothing.

**File names MediaWiki takes** (amended by #373, 2026-10-02). An
artifact's file was uploaded under its corpus file's name as it was, and
a derived medium's under a name whose title kept only its ASCII letters,
digits and ` _.-`: a deck titled "Familie Löbig" uploaded `Familie Lbig
image1.png`, and a title in Russian left only the container's kind. A
name longer than MediaWiki takes, or holding a sequence a title refuses,
was refused. A file is now uploaded under its own name (an artifact) or
`"<title> <name>"` (a derived medium: its container's title and its own
first name, with its file's suffix when that has none), each part in the
form MediaWiki takes as it is. The rules follow the source of MediaWiki
1.45.4, the release the deployed `wikibase/wikibase:7` image runs, and of
UtfNormal 4.0.0, the release it pins. Of the settings they read,
deploy/config changes none (`$wgLegalTitleChars`, `$wgIllegalFileChars`,
`$wgProhibitedFileExtensions`); it adds file types to
`$wgFileExtensions`, which decides only a name's last part, its type.

- **Every letter is kept.** `$wgLegalTitleChars`
  (includes/MainConfigSchema.php) takes every byte from 0x80 to 0xFF, so
  every character beyond ASCII. The name is sent in NFC, the form the
  request's normalisation gives every parameter
  (includes/Request/WebRequest.php, `normalizeUnicode`, through
  UtfNormal's `Validator::cleanUp`).
- **Each character a file name cannot hold becomes `-`**, as MediaWiki's
  upload makes it (includes/GlobalFunctions.php,
  `wfStripIllegalFilenameChars`): the ASCII outside the legal title
  characters, `# < > [ ] { } |` and the controls (a tab or a line feed,
  whitespace, becomes a space, as below), and `: / \`
  (`$wgIllegalFileChars`). So do the replacement character U+FFFD, and
  U+FFFE and U+FFFF, which the request's normalisation makes it
  (UtfNormal) and a title refuses; the controls beyond ASCII, which show
  as nothing; and a lone surrogate, the form a byte that is no UTF-8
  takes in a corpus file's name. Of the ASCII controls, MediaWiki itself
  replaces only tab, line feed, carriage return and DEL: the request's
  normalisation makes the others U+FFFD, and the upload is refused.
- **The start of each sequence a title refuses becomes `-`**
  (includes/title/TitleParser.php, `getTitleInvalidRegex` and
  `splitTitleString`): `%` and two hex digits, a character reference
  (`&amp;`, and `&Сартина;` too: the pattern takes every byte beyond
  ASCII among its letters), and three tildes.
- **What a title changes is changed first:** its bidirectional marks are
  left out, before the name is put in NFC, each run of whitespace and
  underscores is one space (a title's `_`), and no part starts or ends
  with a space.
- **No part starts with a dot: a `-` goes before one, and the dot
  stays.** A name starting with a dot would make the file a hidden one
  on the wiki's disk, and MediaWiki refuses a name with nothing before
  its extension's dot (`getTitle`, `MIN_LENGTH_PARTNAME`,
  `filename-tooshort`). The dot may be the extension's: `..png` under a
  title is `-..png`, `.jpg` alone `-.jpg`, and `_.png`, a title's
  `.png`, is `-.png`.
- **A name is never empty.** A medium's name of which these rules leave
  nothing (whitespace, underscores and bidirectional marks alone) lets
  the title stand as the name, and the container's kind stands for a
  title of which they leave nothing. With neither, the name is `-`:
  `upload_file` would read an empty name as the corpus file's own name,
  as it is.
- **The dot before a part MediaWiki refuses as an extension becomes
  `-`, but for the last part.** MediaWiki splits the name, as its title
  holds it (each space a `_`), at every dot, and refuses the upload
  (`filetype-banned`) when any part after the first, in ASCII lower case
  (`strtolower`), is in `$wgProhibitedFileExtensions` (`getTitle`,
  `splitExtensions`, `checkFileExtensionList`; the default list, which
  deploy/config leaves as it is): `Hutmacherei zum Blauen Hut.com.pdf`
  is refused for its `com`, and a title `Flörburg St.Pl.Dok` for its
  `Pl`. They become `Hutmacherei zum Blauen Hut-com.pdf` and `Flörburg
  St-Pl.Dok`; `Hutmacherei.com .pdf` is taken as it is, its part `com_`.
  The parts are those of the whole name as it is uploaded, cut: a cut
  that ends a title at `St.Pl` leaves `Pl… image1`, which MediaWiki
  takes. A `-` takes the dot's one byte, so no name grows. The last part
  is the file's extension, its type: one MediaWiki refuses (`Brief.exe`)
  is a matter of the file's type, which no name changes, and is left as
  it is, so the upload is refused.
- **It takes at most 231 bytes in UTF-8.** MediaWiki refuses a name of
  more than 240 (includes/upload/UploadBase.php, `getTitle`,
  `FILENAME_TOO_LONG`: an archived version's name adds a timestamp and
  `!` within 255 bytes), counted on the name asked for, and the
  content-tagged name `upload_file` asks for when the name holds other
  bytes is 9 bytes longer. A longer name is cut, counted in bytes,
  never between a letter and its combining marks (a stressed Cyrillic
  vowel is a letter and U+0301: the cut steps back, and the whole letter
  goes), with `…` where the cut is. The same rule keeps these sequences
  whole, and no others: a character and any combining mark (category M)
  after it; a character and a zero-width non-joiner or joiner (U+200C,
  U+200D) after it, and any character after a joiner (an emoji family);
  an emoji and its skin tone (U+1F3FB to U+1F3FF); a flag's regional
  indicators (U+1F1E6 to U+1F1FF), in pairs, and a subdivision's flag
  and its tag characters (U+E0020 to U+E007F); a halfwidth kana and its
  sound mark (U+FF9E, U+FF9F); an old Hangul syllable, whose jamo NFC
  leaves apart: a character and a vowel or final jamo after it (U+1160
  to U+11FF, U+D7B0 to U+D7FF), and a leading jamo (U+1100 to U+115F,
  U+A960 to U+A97F) and a leading jamo or a precomposed syllable
  (U+AC00 to U+D7A3) after it; and an Indic conjunct, a virama
  (canonical combining class 9) and a letter after it. It is not
  Unicode's full grapheme cluster rule (UAX #29), and keeps only these
  sequences whole. It is the writer's rule (`writer.extends`,
  `writer.half_flag`), by which the writer also cuts a label or another
  term it sends, so labels and file names are cut alike. The cut is
  made: the title first, as far
  as `"… <name>"`, so the medium's own name stays whole; past that, the
  name alone, the title left out, when it fits, so a name that fits on
  its own is never cut; a longer name before its extension; and, should
  the extension leave no room, the name at its end.

The name depends on the title and the name alone, so a re-run asks for
the same one, and MediaWiki takes it with no warning about its form
(`badfilename`). A name with no dot at all, such as `-`, is the one
exception: MediaWiki adds the extension of the file's type, and warns
`badfilename`, which `upload_file` answers by uploading under the name
MediaWiki gives ("Media files" above). A name with no extension but a
dot before its end, such as a corpus file's `Bd. 2` with no suffix, or
a title with a dot before a medium's name with none, is not made
legal: MediaWiki reads its last part as the file's type and refuses
the upload (`filetype-missing` or `filetype-banned`), a file-type
matter, as it did before. A derived medium's name ends in its file's
suffix whenever the record's file has one. The file page a `media file`
names is the name MediaWiki stores, its first letter a capital. A file
uploaded before keeps its name: registration uploads only bytes no file
of the item holds.

Two titles that differ only past the cut ask for one name. The first
registered takes it; the second's bytes are other bytes under that name,
so `upload_file` uploads them under its content-tagged form, which the 9
bytes kept free let fit. Nothing is overwritten, which deck takes the
plain name depends on the order they are registered in, and a re-run
finds both files by their SHA-1. These rules are copied from MediaWiki
1.45.4, the version the deploy's `wikibase/wikibase:7` image runs: when
the image's MediaWiki changes, they are checked against its source again
(the title characters, the file name limit, the sequences a title
refuses and `$wgProhibitedFileExtensions`).

The orchestrator adds **content dedup** ahead of the expensive steps. It
hashes each file at the top of `advance`, and a file whose bytes were
already claimed is marked `duplicate` and is *not* transcribed,
extracted, or written. "Already claimed" spans two scopes:

- **within the run** — an earlier document in the same folder pass
  (alphabetically-first is canonical, deterministically);
- **across runs** — a document ingested to completion (`written`) in a
  prior run, an excerpt among them (ADR-0008 as amended by #397, whose
  directories are in the state directory's `excerpts/`): a file added
  later with an excerpt's bytes is that excerpt's duplicate. `run()`
  pre-seeds the run's seen-hashes from persisted
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

**What is still matched by label** (amended by #444, 2026-10-02). No
artifact-kind entity is matched to a registered artifact, no other
entity to an artifact or a part of one, and no artifact is adopted, by
its title or label. Registration finds an artifact by its SHA-1 alone.
Resolve's exact-label step never resolves a ref on the artifact side
(of an artifact kind, artifact or a class the manifest makes a
`subclass_of` it, or of artifact fragment, as in the writer's
`is_artifact_kind`; amended by #444, 2026-10-03), and
resolves no ref to an artifact or a part of one: it matches against the
instance's labels without those items' (`resolvable_labels`). The
writer, finding the item a manifest record names after resolve replaced
its refs (its `_found_entities`), pairs a record and an item by label on
the same terms, a record that no claim names as much as any other: a
person's record never names a media object's item, nor a media object's
record a registered scan (amended by #444, 2026-10-03). What
the two read stays bounded by the batch (amended by #444, 2026-10-03):
resolve reads the instance's labels once a batch and the statements of
only the items its refs' labels name (`WikiClient.fetch_claims`), and a
write reads the labels once, with the vocabulary known (a fresh client
reads them for the vocabulary too, when the writer is made), for its
plan, the artifacts registered under a label, looked up in an index of
the labels made once a write, and every item it finds by label (the
writer's `_FoundLabels`, with the items it makes), and the statements
of only the items it finds. Neither label-matching read reads every item's
statements; the writer's lookup of a cited artifact by its SHA-1
(`resolve_artifacts`), when the caller does not supply that artifact,
still reads the media SHA-1 index, the statements of every item. A
write's one label snapshot assumes one process works on the instance at
a time: an item another process makes under a label during the write is
not seen until the next write. An
artifact or a part of one is an item that carries a `media SHA-1`,
names a `media file`, or is an `instance of` an artifact kind or of
artifact fragment (the writer's `is_artifact_item`). A statement counts
whatever its rank (AY, amended by #444, 2026-10-03), as a `media SHA-1`
does wherever the writer and the registrar read one: a curator's
deprecated `instance of photograph` on a person's item still makes it
an artifact item until the statement is removed. The writer finds an
item by its label before it makes one, on the entity's own side only,
an entity of an artifact kind or of artifact fragment (the writer's
`is_artifact_kind`) being on the artifact side:

* an artifact-kind entity, such as a GEDCOM media object, never joins an
  item registration made, one that carries a `media SHA-1` or a page
  fragment `part of` one (above), nor any item that is no artifact or
  part of one: a person, a place, a run item or an unclassified item
  under its title (AM). A portrait titled with its sitter's name is thus
  an item of its own, never the person's;
* any other entity, a person, a place or one with no kind, never joins
  an artifact or a part of one, registered or not (AN);
* an entity that may not join the item under its label is found, or
  made, under its tagged label (the writer's `tagged_label`): an
  artifact-kind entity's ends in `" [media object]"`
  (`media_object_label`), any other's in `" [not an artifact]"`, one
  tag whatever its kind or none (AZ, amended by #444, 2026-10-03), its
  own label an alias of that item. The tag depends on the label and the
  side alone, so a re-run finds the item again, and nothing about the
  item under the plain label changes. Labels stay unique, as the wiki client's label
  index fails on two items with one English label (and Wikibase refuses
  two with one label and description), so the entity cannot take the
  plain label while another item holds it; the artifact keeps its title,
  and a person keeps its name as an alias, the closest to its name the
  wiki allows. Which side bears the tag depends on the order of the
  writes, as a content tag does: a person written before an artifact
  registered under its name keeps the plain label, and registration
  content-tags the artifact (a person is no item with no hash, so
  nothing is flagged); written after, the person is tagged. Within one
  batch the order of its refs does not decide it: the writer places
  every entity that is not of an artifact kind before any that is, so
  of a person and a media object of one batch under a label no item
  holds, the person takes it and the media object its tagged label,
  whichever the batch names first. The tag is one for every kind, so
  one person named under a label an artifact holds by refs of two kinds
  (a GEDCOM's `human` and an extraction's ref with no kind, say) is one
  item, `"<name> [not an artifact]"`, as without the clash the two meet
  on the item under the plain name; a tag naming the kind would split
  them into two items until a curator merged them. A media
  object that does not take its label raises unhashed-title against its
  item on every artifact registered under that label, whatever item
  holds it: the artifact under it, and every artifact under it with one
  content tag in it (a re-scan, `"<title> [<sha1[:8]>]"`, or its
  fragment, `"<title> [<sha1[:8]>], page <n>"`; a tag is one when
  taking it out leaves the title, and only the artifact's own tag, its
  SHA-1's first eight hex digits, is one, as the registrar takes out
  only its own: a title with a bracketed date of eight digits is
  compared whole, and another artifact registered under
  `"<title> [18950312]"` is not flagged), in the order of their items. Under a
  person's name the artifact is content-tagged, as registration came
  after the person, and is flagged all the same, as registration flags
  it against the media object's item in the other order; with no
  artifact registered under the label, nothing is raised. A first dry
  run reports that flag against the media object's placeholder
  (`(new:<id>)`), as the write would raise it (AO);
* an entity that may join neither the item under its label nor the one
  under its tagged label, as when registered items hold both a media
  object's title and that title with `" [media object]"`, is refused,
  with every claim that names it, before the first write, in a dry run
  too, and the rest of the batch is written (AK): the write's `refused`
  names the claims, a warning names the entity by its local id, its
  claims by their claim ids and the two items by QID alone, and each
  artifact registered under the media object's label is raised as
  unhashed-title against the item under its tagged label: for such a
  flag `others` is that item, a registered one, not an item with no
  hash. The writer settles where each entity's item goes before its
  first write, with the labels the batch itself makes items under, so no
  entity is refused partway. The refusal holds on every write until a
  curator relabels one of the two items. A refusal with no such pair
  raises no alert, only the warning and the step's `refused` count: a
  media object with no artifact registered under its label, or with no
  item yet under its tagged label (another entity of the batch takes
  it), and a person, place or other entity whose name and tagged label
  artifacts both hold. None is an artifact registered under the title of
  an item that may be the same object, the question unhashed-title puts
  to a curator; each is a clash of labels that no registration on a
  reset instance makes (it needs an item titled literally with a tag),
  and the warning names its items by QID.

Labels still match, until the identity map (#109), for:

* every entity on its side, in resolve's exact-label step (the refs an
  extraction labels; not an artifact-kind ref, and not to an artifact)
  and in the writer: people, places, organisations, families, vessels,
  legal instruments, military units, occupation classes, events
  (births, deaths, weddings, divorces and the rest), partnerships and
  marriages, and unclassified entities, each with an item that is no
  artifact or part of one. It is the interim identity of a named entity,
  so a re-run never duplicates one and two documents naming it alike
  meet on one item (ADR-0021 relies on it for the GEDCOM import);
* a media object, or another artifact-kind entity, and an item with no
  hash that is an artifact or a part of one: two exports' media objects
  under one title, two objects of one export under one title (both are
  the one item, tagged or not), a media object and an artifact
  registered before content hashing, as neither carries a hash to tell
  them apart, and, on an instance from before content hashing, a media
  object and a page fragment of an artifact with no hash (`"<title>,
  page <n>"`), which the media object joins as it joins that artifact;
  registration flags such an item when hashed bytes come under its
  label. A fragment of an artifact with a hash is never found by its
  label;
* the extraction run's item, which the writer finds by a deterministic
  label that holds a fingerprint of its batch, and the place hierarchy's
  run item, which `places.ensure_places_run_item` finds the same way, so
  the label is their key by construction;
* an entity an approved equivalence names (ADR-0007): reconcile's
  blocking may propose a media object against an artifact on the roster,
  and an equivalence a curator approves sets the item (`record.qid`),
  which the writer takes before anything above. It is a human decision,
  not a label match;
* the manifest's vocabulary, its properties and classes, which the wiki
  client maps from their manifest labels.

One case is a label join between two titles: an object titled literally
`"<title> [media object]"` takes that label as its own when no item holds
it, and an object titled `"<title>"`, written once a registered artifact
holds `"<title>"`, finds that item under its tagged label and joins it.
The tag cannot tell the two apart, and an export's title of that form is
not expected; the flag on the artifact names the joined item.

The alert counts depend on which side writes again (AL): every later
registration of the artifact raises unhashed-title again, and, with the
artifact first, every later write of the GEDCOM does too; with the
GEDCOM first, a later write of the GEDCOM raises nothing. A scratch run
writes each document once on a reset instance, so where one write
registers the artifact its counts do not depend on the order. Where N
writes register it, as excerpts of two PDFs that place one image do,
the GEDCOM first gives N flags, and k of them before the GEDCOM give
N - k + 1; that dependence is accepted too, as a count above zero still
says the pair exists. Whether a media object and an artifact are one
item never depends on the order. Nor does whether refs naming one person, of
whatever kinds, meet on one item (AN, above): written before an
artifact takes the name, the person keeps the plain label and later
refs join it there; written after, every ref joins the item under the
tagged label. Only which label that item bears depends on the order.

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
  no such item exists there. On a kept instance such a registration is
  flagged, as is an item under the older item's title (amended by #371,
  2026-10-02): each write that registers the item raises alerts that
  list both items for a person to look at, and the graph holds both until a curator links or ranks them.
- **Not solved yet:** re-encodes, re-scans, crops/uncrops, and the
  corroboration-inflation they cause remain until parts 2 & 3 land. The
  inconsistency report and galleries treat them as distinct artifacts
  meanwhile — visible, not silently merged.
