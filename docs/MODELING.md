# Wikibase modeling conventions

## General approach

- The schema is self-describing: properties and classes are minted as
  Wikibase properties/items in-instance.
- Mirror Wikidata property *semantics* where a good equivalent exists
  (instance of, date of birth, spouse, residence, depicts, coordinate
  location, start/end time, stated in...), using local P-ids. The
  vocabulary is declared in `model/manifest.yaml` and minted by
  `model/bootstrap.py`, which maintains the mapping table
  (local id – Wikidata id) on the wiki page `Project:Model mapping`.
  Local P/Q ids are per-instance and never recorded in the repo.
- Every entity gets a label and a short description; name variants go
  in aliases. English labels are the bootstrap's idempotency key:
  unique per entity kind, and renames must happen on the wiki and in
  the manifest together.
- Wikibase keeps a label, a description or an alias trimmed, then
  cleaned up, then in NFC: each run of control characters in it (a line
  feed or a tab among them) made one space, the separators at either end
  dropped (the line and paragraph separators, U+2028 and U+2029, among
  them), then the noncharacters U+FFFE and U+FFFF made U+FFFD, and at
  most 250 characters long. It refuses a term that still holds a line or
  paragraph separator inside, or begins or ends with a Mongolian vowel
  separator (U+180E), and an item whose label and description are the
  same. The writer sends each term it makes in that form, with a line or
  paragraph separator inside made a space too (a run of them and of
  control characters is one space) and a Mongolian vowel separator at
  either end dropped, and finds an item by its label in it,
  so a label given in another form finds its item again; the registrar
  compares and sets an artifact's title by the same rule, which it takes
  from the writer (#369, #403). A term still over 250 characters in that
  form is cut as a long locator is (below), with the digest of the whole,
  so whitespace around a term never counts towards the limit, two labels
  that agree up to the cut stay two items' labels, and the run item's
  label, which holds the model's identifier, keeps its batch's
  fingerprint in the digest. An entity whose description would be its
  label gets none, with a warning (#384).
- Wikibase puts a term, and every text, in NFC by the tables of Unicode
  15.0 (ICU 72.1 in the wikibase/wikibase:7 image), and the writer by
  Python's (14.0.0 on Python 3.11). Unicode's normalization stability
  policy makes the two agree on every text whose characters Python's
  tables assign, so the writer sends no term that holds a character they
  do not, noncharacters aside, read whole, past the cut too, so that no
  cut of such a term is sent either: a combining mark assigned since,
  such as U+1E08F, the combining Cyrillic Byelorussian-Ukrainian i, may
  take another place in the instance's NFC. A description, an alias or
  a type hint that holds one is left out, never changed, and the write's
  result names it in `refused_terms`, with a warning. An entity whose label
  holds one is neither found nor minted, and each claim that mentions
  it is refused. The registrar makes no alias of a title that holds one,
  and content-tags a new item's label made from one, as it does a taken
  label, so two same-named copies stay two items whatever form the
  instance keeps the first one's label in. A description is named in
  `refused_terms` only when a claim the writer writes mentions its
  entity, as only the item such a claim mints would take it: a claim
  refused for its text, or with an entity that may join neither the
  item under its label nor the one under its tagged label (ADR-0010),
  mints nothing. A run
  item's label holding one, an extraction run's or the place-hierarchy
  pass's, or an extraction run's description, raises ValueError before
  the run item is looked up, and so before anything is written, as a
  re-run would not find the item by it.
  The interpreter's tables must be no newer than the instance's, which a
  unit test checks (writer's INSTANCE_UNICODE; ADR-0010, #431).
- Schema-level statements (e.g. the subclass hierarchy) carry
  references too (ADR-0003): a `stated in` reference to the *model
  manifest* item, which represents `model/manifest.yaml` — the document that
  asserts them.

## Core classes (items, via "instance of")

- human
- place
- time period (linked to PeriodO definitions where applicable)
- event (birth, death, migration, military service, ...; a wedding, with
  its subclasses civil wedding and religious wedding; a divorce; an
  annulment — ADR-0036)
- partnership (a couple's relationship of any kind, for two people a
  source presents as a couple without saying they married), and its
  subclass marriage (Q8445): each an entity, not an event, whose couple
  are its participants with the role `partner` or `spouse`, started by
  its wedding and ended by a divorce, an annulment or a partner's death
  (`started_by`, `ended_by`; ADR-0036)
- artifact (subclasses: photograph, certificate, letter, census page,
  newspaper clipping, presentation, genealogical report, address
  book, ...)
- organization (an agent — company, bank, committee, government body,
  school, congregation; distinct from place — see ADR-0012)
- family (a named family or household unit — ADR-0014)
- vessel (a ship; e.g. an emigration vessel — ADR-0014)
- legal instrument (a cited law, decree, gazette, or register — ADR-0014)
- military unit (a regiment, brigade — ADR-0014)
- occupation (values of the `occupation` property; labels keep the
  source's wording)
- unclassified entity (the open-schema holding class — see ADR-0013)
- artifact fragment (a page, crop, or region of an artifact)
- extraction run (one pipeline execution: model name+version, date,
  prompt/pipeline version, software commit)

**Manifest descriptions are extraction-prompt surface**, not only
documentation. Every property and item in `model/manifest.yaml` carries
a description, and the extractor is shown it verbatim beside the key: a
property's description is what tells the model which way a relation
points (`employer` takes the organization, not the employee) and what
kind of entity the value must be (`location` takes a place). Write them
for that reader as well as for a human.

Items flagged **`entity_kind: true`** are the kinds the extractor may
type a document-mentioned entity as (the ADR-0007 manifest pass), and
the prompt renders that set from the manifest rather than restating it.
Artifact types, controlled-vocabulary values (`circa`, `male`), and
infrastructure classes (`artifact_fragment`, `extraction_run`) are real
classes but not entity kinds. A new item is not offered to the
extractor until it says so.

**Controlled vocabulary values** (items, never entity kinds): `circa`;
`male` and `female`; the participant roles `principal`, `parent`,
`partner`, `spouse`, `informant`, `witness` and `officiant`
(`object_has_role`, ADR-0035 §3.2); the kinship classes `kin_parent` …
`kin_relative` and the sides `paternal` and `maternal` ("Kinship"
below); the civil statuses `unmarried` (never married), `married`,
`widowed` and `divorced` (`civil_status`, ADR-0035 §3.1); and the five
judgement note categories `judgement_kind`, `judgement_relation`,
`judgement_date_reading`, `judgement_identity` and `judgement_other`
(ADR-0035 §4). The kinship classes whose word is also a role's or a
property's (parent, child, sibling, relative) carry "(kinship class)" in
their labels, so that no two items share a label (the bootstrap's key)
and none reads as the property of the same name.

**The relation decides the kind (ADR-0031, ADR-0035 §3.5).** Every
item-valued property declares `accepts` in the manifest: the kinds its
value may be (`location` a place, `carried_out_by` an organisation,
`participant` a human or a family), each named exactly, so a subclass is
accepted only where it is listed; or `any` (`depicts`), `subject` (the
subject's own kind, for `said_to_be_the_same_as`) or `class`
(`instance_of`, `subclass_of`). A list that names an entity kind also
names `unclassified_entity` (ADR-0013). The manifest does not load when
an item-valued property declares none. Validation refuses a claim whose
value is of a kind its property does not accept, and extraction counts it
among its rejects. An existing item named by its QID has no kind in a
batch and is not checked. A vocabulary ref to an entity kind (`human`,
`place`) names the class, not an entity of it, so only `instance_of` and
`subclass_of` take one. `part_of`'s list is flat for now: it takes a place
or an artifact whatever its subject, so a place `part_of` an artifact
passes the check, which reads the value alone.

**Reference properties** (`stated_in`, `inferred_from_extraction_run`,
and the judgement note's `judgement` and `judgement_note`) are marked
`reference: true`: they are written only in a statement's references, so
validation refuses one as a claim's or a qualifier's property, and no
prompt offers it. A property may also be held back from the prompts with
`offered: false` while it stays in the manifest and the bootstrap. A
property marked `document_subject: true` (`issued_by`) takes the
claim's own source document as its subject, named by an `artifact`
reference to the source's media, and no other subject; validation
refuses such a subject on any other property, or naming another
document, and a claim of such a property with any other subject
(ADR-0035 §3.4). The writer checks the same rule itself, since `yizkr
write` takes a batch without validating it against the manifest: it
refuses such a claim before the first write, names it in its `refused`
list and warns why, as it refuses a claim whose text the instance cannot
hold.

**A retired property** stays in the instance and takes no new claim: it
accepts nothing, validation refuses every claim of it, and no prompt that
the manifest renders offers it. `spouse` is retired (ADR-0036): the spouse
link is derived from a marriage's participants. The GEDCOM adapter makes
marriages and partnerships (ADR-0036 §5, "Marriages, residences and other
periods" below), so no batch carries a spouse claim.

## Reference pattern (see ADR-0003)

Every statement carries at least one reference with:
- stated in → artifact item (or artifact-fragment item)
- locator qualifier(s): page number, and/or region given as a
  percentage bounding box "x%,y%,w%,h%" (W1/W3) or a IIIF
  canvas+xywh URI (W2)
- the printed page number, where there is one: the page number printed
  on the page the `page number` locator names, exactly as printed
  (`printed page number`, a reference property). The page number stays
  the physical page, the page's place in the media from 1, so a claim
  read from page 6 of *Our Family from Germany*, whose sixth page is
  printed as page 4, cites `page number` 6 with `printed page number` 4.
  The extractor sets it from the transcription record (ADR-0037), never
  from the model: only for a claim whose page number is one physical
  page whose record gives a printed number. A range, a page given in
  words, a page the record gives no printed number and a transcription
  with no record (a text file's, a deck's slide text) get none; none is
  ever guessed (#424). It describes the physical page: a page that
  shows a spread with one half numbered keeps that one number for
  every claim read from it, as both ways of reading a PDF give it the
  same (ADR-0037 §2). A source that gives a printed page number gives
  its page number too: validation refuses one without the other, in a
  prepared claims file as in an extractor's batch. An excerpt's claims
  read the excerpt's own transcription, so they never take the printed
  number of the page that places it, and its `part of` the container
  keeps the container's physical page (ADR-0008)
- optional short transcription excerpt supporting human verification,
  tagged with the language it is written in (BCP-47). `yizkr ingest run`
  takes each document's language from its transcription by default
  (`--language auto`, ADR-0015): from the transcription record the vision
  engine fills, which names the languages each page is written in
  (ADR-0037), and otherwise by detection from the text outside its
  editorial marks. So a mixed-language folder tags each excerpt
  correctly; the extractor may still tag individual excerpts in their own
  language (a quoted passage in another tongue)

Machine-written statements add a second reference:
- inferred from extraction run → extraction-run item

On the claims path, a value asserted more than once, by several
artifacts or by one artifact in several places (two pages, two
passages, two regions of a scan), is one statement for each set of
qualifiers. It carries a `stated in` reference for each place, with that
place's locators and excerpt, and, once, the run reference of each run
that added one; writing a batch again adds no reference (#335). The
exception is a statement with no run reference (a curator's own write,
before any machine claim joins it) or whose rank a curator changed: the
writer adds nothing to it for another place of an artifact it already
cites (the writer's module docstring, `src/yizkr/pipeline/writer.py`,
gives the rule). The kinds and type hints the writer records after the
claims, from an entity manifest or a ref, follow ADR-0007 instead.

Wikibase holds at most 400 characters in the text of a string,
external-id or monolingual text value, a reference's page number,
printed page number and region among them, and no text value with a
line break or a tab in it, or whitespace at either end; it refuses the
whole edit that sends a text it does not hold. It also cleans a text
up, making each C0 control character but the tab, the line feed and
the carriage return, and each
of the noncharacters U+FFFE and U+FFFF, U+FFFD: in a string or
external-id value before it checks and stores it (so it changes a
vertical tab or a form feed there, and refuses one in monolingual text),
and in every text its API gives back, monolingual text included. The
writer checks each text in NFC, the form the instance keeps it in, and
sends none that the instance would give back changed (#366, #384,
#403). A page number, printed page number, region or excerpt goes on
one line: whitespace at either end is dropped, a line break inside
becomes " | ", a tab a space and each other character the instance
cleans up U+FFFD, as the instance
gives it back, so a re-run finds its reference block; one of whitespace
alone is left out. One still over the limit is cut at
a break between words and ends with " … sha1 " and the first 12 hex
digits of the SHA-1 of the whole text: a locator only places the claim
in its source, as an excerpt only helps a reader check it there, and the
digest keeps two places whose locators or excerpts agree up to the cut
two references. A claim whose own text, its value or a qualifier's, is
over the limit, or holds a line break or a tab, or has whitespace at
either end, or holds a character the instance cleans up, or holds a
character Python's Unicode tables do not know (above; its page number,
printed page number, region or excerpt holding one refuses it too,
#431), is refused,
never cut, trimmed or cleaned up, since that would alter what the source
says: nothing of it is written, and the write's result
names it by its claim id (its review-sheet id, unless the review
corrected it), with a warning that says why (the writer's module
docstring). Every other claim of the batch is written. An entity's type
hint is free text, compared ignoring its spacing, so it goes with each
run of whitespace in it as one space, and each character the instance
cleans up as U+FFFD, and a re-run finds the statement written for it.

Curator-attributed statements (a value correction from the review CSV)
carry the `stated in → artifact` reference only:
process provenance is for machine writes, and a human decision's
process is the MediaWiki edit history. The pipeline marks these with
the claim contract's `curated` flag, which only the review sets: an
extractor drops the flag, and any row approval, from a model's answer,
so every claim a model gives is written with its run reference.

The pipeline's claim JSON contract implementing this pattern is
`src/yizkr/claims.py` (`ClaimBatch`): sources are required fields there,
ranks are unrepresentable, and property/item references use manifest
keys resolved to local ids only at write time. A claim cites its source
artifact by content: the SHA-1 of the artifact's media (`ArtifactRef`,
see *Artifact identity* below), which holds across instance resets. It
is the only form a source takes: a batch whose claim names its artifact
item by QID does not load (#102). The writer resolves the hash to the
artifact item carrying that `media SHA-1` at write time, and refuses a
batch that cites media no item carries before it writes anything. So an
artifact a claim cites has media: a page fragment is cited through its
artifact and a `page number` locator, and an un-held GEDCOM media item
(below) is never a claim's source. Every producer names the source
itself: an extractor sets each claim's source to the key of the document
it read, whatever the model wrote there; the GEDCOM adapter cites the
export's bytes; and a face proposal cites the photograph's bytes, while
its subject stays the photograph's item. No transcription and no
extraction prompt names an artifact by QID: a transcription names its
artifact by the same key, and the extraction prompts name it by that key
and ask the model to cite it (#127), so a reset, which gives each
artifact a new QID, changes neither.

## Artifacts and registration

- W1: media are wiki files on the instance; the artifact item points at
  its file page via the `media file` (string) property (ADR-0004).
- **A file's name is in the form MediaWiki takes as it is** (ADR-0010 as
  amended by #373, after MediaWiki 1.45.4's source). A file is uploaded
  under its own name, and a medium a container embeds under
  `"<container title> <its name>"`: every letter kept, each character
  MediaWiki refuses in a file name made `-` (`# < > [ ] { } |`, `: / \`
  and the controls other than whitespace), as is the start of each
  sequence it refuses (`%` and two hex digits, a character reference
  such as `&amp;` or `&Сартина;`, `~~~`), each run of whitespace and
  underscores one space, a `-` before a dot that would start a part
  (`.jpg` is `-.jpg`), and the dot before each part but the last that
  MediaWiki refuses as an extension made `-` (`Hutmacherei.com.pdf` is
  `Hutmacherei-com.pdf`; a refused last part, the file's type, is left,
  and MediaWiki refuses the upload). A name these rules leave nothing of
  leaves the title as the name, or, with none, is `-`: never empty. At
  most 231 bytes in UTF-8: MediaWiki's 240, less the 9 a content-tagged
  name adds. A longer name is cut, never between a letter and its
  combining marks, with "…" where the cut is: the title
  first, so the medium's own name stays whole; then the title left out,
  so a name that fits on its own is never cut; and then the name before
  its extension. A `media file` names the file as MediaWiki stores it,
  its first letter a capital.
- An artifact's description is put in the stored form, then cut,
  ending in "…" and never between a letter and its combining marks, to
  the 250 characters Wikibase takes: the one registration writes, ``"ingested
  by `yizkr ingest run` from <file name>"``, passes that for a long file
  name (ADR-0010 as amended by #373).
- Registrar statements (`instance of`, `media file`, a fragment's
  `part of` + page qualifier) are referenced `stated in -> the artifact
  item itself`: an artifact asserts its own kind and composition by
  existing. Extraction-run items use the same convention for their
  `instance of`. Every registrar/writer path therefore writes only
  referenced statements (ADR-0003).
- **A run item states its kind in a second write** (#368). Its `instance
  of → extraction run` cites the item itself, so it is written once the
  item has an id, after the write that makes the item under its label. A
  run cut short between the two leaves the item labelled but untyped, and
  the next run that finds it by its label types it: unless the item
  states `instance of → extraction run` already, at any rank and under
  any reference (a curator's deprecation stands), it gains that
  statement, stated in itself. This holds for the pipeline's extraction
  runs and for the place-hierarchy pass's runs alike. An untyped item is
  missing from the non-privileged export until it is typed ("Living
  persons and privacy"), and every statement it backs loses its run
  reference there: an extraction's claims would read as curated, and the
  place-hierarchy pass's containment, which cites the run alone, as
  unreferenced.
- **Registration is part of the write** (ADR-0025 §2; ADR-0010 as
  amended by #103). `ingest run` registers a document just before it
  writes the claims that cite it: its artifact item, its page fragments
  and a deck's derived media, each found or made by the SHA-1 of its
  media. Nothing before the write has an artifact item, and a write that
  runs again (a changed review, say) registers again, finding by SHA-1
  what the earlier write registered.
- **An item that may be an older artifact is flagged, never adopted**
  (ADR-0010 as amended by #371). An item with no hash has no `media
  SHA-1` and names a `media file`, as an artifact registered before
  content hashing does, or is an `instance of` an artifact kind, as a
  GEDCOM media object's item is (a label, no file). Each time
  registration makes or finds an artifact or derived medium while
  another item names a wiki file holding its bytes (`shared-file`), or
  while its title, or its own label before its content tag, is an
  unhashed item's label (`unhashed-title`), `ingest run` raises the
  flag as an alert of the write naming the items by QID, for a person
  to look at; it repeats on every write until a curator acts on the
  older item. A new item is made as any other, and neither item is
  changed. Registration writes `media file` and `media SHA-1` together,
  so a reset instance never raises `shared-file`; it raises
  `unhashed-title` only for an artifact or derived medium registered
  under exactly a GEDCOM media object's label, or with a page fragment
  under it, whichever is written first: a GEDCOM written after the
  artifact never joins its media object to it, or to its fragment, by
  the label, but makes it an item of its own, labelled `"<label> [media
  object]"`, and its write raises the flag, on every artifact registered
  under that label, content-tagged or not (ADR-0010).
- **An item is matched by its label on its own side only** (ADR-0010 as
  amended by #444). A media object, or any artifact-kind entity, joins
  by its label only an item with no hash that is an artifact or a part
  of one; a person, a place or any other entity never joins an artifact
  or a part of one. An entity that may not join the item under its label
  takes an item of its own under its label tagged `" [media object]"`,
  or, whatever its kind or none, `" [not an artifact]"` (one tag for
  every kind, so refs of two kinds naming one person meet on one item;
  amended by #444, 2026-10-03), its name an alias; one that may join
  neither label's item is refused, with its claims, before the write.
- **A page fragment is keyed by its artifact.** The fragment for page
  *n* is the item that is an `instance of artifact fragment` and `part
  of` the artifact with `page number` *n*, whatever its label. A new one
  is labelled `"<artifact label>, page <n>"` (content-tagged if that label
  is taken, or if it holds a character Python's Unicode tables do not
  know and bears no tag yet; the artifact's label cut to end in "…" if
  the whole would pass 250 characters) and made in one write with both
  statements, each stated in the artifact. So same-titled artifacts
  never share a fragment, and a derived medium, `part of` its container
  with a page qualifier too, is never taken for one.
- Machine-proposed entities (people, places) get their `instance of`
  statement referenced to the artifact that mentions them. A later
  source that names the entity adds its own references (the artifact and
  its extraction run) to that statement, or, when it gives a different
  kind, a statement of its own: the kinds different sources give coexist
  like any other attributed values (ADR-0002, ADR-0007). A `type hint`
  accumulates the same way, compared as free text.

## Artifact identity (see ADR-0010)

- An artifact's identity is its **content**, not its filename. Every
  artifact with media carries a `media SHA-1` statement (hex SHA-1 of
  the bytes), referenced `stated in → the artifact itself` (for media
  derived from a compound artifact: `stated in → the container`). The
  registrar keys on this hash alone — identical bytes are one artifact
  item however they are named, and an alternate filename becomes an alias
  unless it is another item's label. A title never finds an item: two
  files that share a title are two artifacts, and media without bytes to
  hash cannot be registered.
  Bit-identical media is never stored, itemised, or extracted twice.
- Labels stay unique per kind: when a title is already another item's
  label, the new artifact's label is disambiguated with a short content
  tag (`"<title> [<sha1[:8]>]"`) rather than colliding. Titles are
  compared and set in the form Wikibase stores labels in, by the rule the
  writer keeps labels by (above: trimmed, then U+FFFE and U+FFFF made
  U+FFFD, then NFC; #369, #403), an artifact's description goes in that
  form too, and a title longer than the 250 characters Wikibase takes,
  as it is or once tagged, is cut to fit, ending in "…" (#371). The tag
  is a stopgap until labels need not be unique for artifacts (#94), and
  which homonym bears it depends on registration order: nothing may key
  on an artifact's label, only on its `media SHA-1`.
- A file on the never-ingest list is never registered or uploaded, and
  neither is a medium a container embeds that the list names, by name
  or by SHA-1. The container's own file is uploaded as it is, with
  whatever it embeds.
- **Every `media file` holds the bytes its item's `media SHA-1` names.**
  A wiki file is never overwritten: bytes whose file name already holds
  different bytes are uploaded under a content-tagged file name,
  `"<stem> <sha1[:8]><extension>"`, and bytes the wiki holds already
  reuse that file (ADR-0010, "Media files").
- **Near-duplicates are not identities.** Re-encodings, re-scans, and
  crops have different bytes and are (for now) distinct artifacts;
  linking confirmed alternate versions is a curator judgement through a
  future review queue and a `version of` relation (ADR-0010), never an
  automatic merge — the same rule as person identity (ADR-0007).

## Organizations (see ADR-0012)

- An `organization` is an agent — a company, bank, committee, government
  body, school, or congregation. It is **distinct from `place`**: an
  organization has a `location`, it is not one. **The relation decides
  the kind** (ADR-0031): a relation of place (born, died or buried at,
  lives at, located at, deported to, held at) takes a place, and a
  relation of agency (`issued_by`, `carried_out_by`, `employer`,
  `educated_at`, `member_of`) an organization. So a camp, a prison, a
  hospital or a school named only as where something happened is a
  place, and one the source shows acting is an organization: the
  registration card's Peiltal court prison, where Leopold was held, is a
  place; the Landgericht Rekau, which divorces Leopold and Agnes, is an
  organization. Where one source uses one institution both ways, it
  gives two entities, a site and a body, linked by the organization's
  `location`. The extraction prompts carry the rule, and validation
  refuses a relation whose value is of a kind it does not accept.
- `issued_by` (P2378) links a document to the organization that issued
  it, such as the register office of a certificate. Its subject is the
  document itself: a claim names it by the SHA-1 of its media, as its
  source is named, and the writer writes the statement on the artifact
  item (ADR-0035 §3.4). `carried_out_by` links an event to the
  organization that carried it out, such as the court of a divorce.
- `employer` (P108) links a person to an organization, orthogonal to
  `occupation`: what someone does (occupation, an occupation item) and
  whom they work for (employer, an organization item) are independent,
  separately-referenced claims. Date employment with start/end time.
- Organization labels keep the source's wording; normalisation is a
  curator edit, never an extraction guess.

## Uncertainty and time

- Use Wikibase time precision (year / month / day / decade / century).
- Centuries and millennia are counted as Wikibase and Wikidata count
  them, from year 1: the nth century runs from year 100(n−1)+1 to year
  100n, so a date at century precision stored as any year from 1801 to
  1900 is the 19th century, and one stored as 1800 is the 18th. The nth
  millennium runs from year 1000(n−1)+1 to year 1000n (the 2nd
  millennium is 1001–2000). Before the common era the count runs back
  from 1 BCE: −1800 to −1701 is the 18th century BCE, which runs from
  1800 to 1701 BCE.
- A claim gives a century by its first year (the 19th century as 1801),
  as it gives a decade by its first year (the 1890s as 1890), and the
  writer stores that year as it is: the wiki shows it as "19. century". A
  claim at century precision whose year is not the first year of its
  century is refused, so the 19th century given as 1800, which Wikibase
  counts as the 18th, is never written. Claims have no millennium
  precision. The static site's `format_time` and the disagreement report
  count as the wiki does, whichever year of a century is stored (a
  curator who types "19. century" into the wiki stores 1900):
  `format_time` shows a century-precision date stored as 1801 or 1900 as
  the 19th century, and the report gives it the span 1801–1900
  ("Contradictions").
- "The 1800s", meaning 1800–1899, is not a century in that count, and
  is stated as a range, earliest date 1800 and latest date 1899, never
  rounded to the 19th century.
- Ranges and vagueness: earliest date / latest date qualifiers. On a
  statement dated as a period (a marriage, a residence), each end has its
  own pair: see "Marriages, residences and other periods". The static
  site's timeline and inconsistencies pages show a stated range beside
  its date, each bound under its qualifier's label and at its own
  precision, so a range open at one end names its one bound: Rahel's
  death "after 1945" per the family tree shows as "1945 (earliest date:
  1945)".
- "circa": sourcing circumstances qualifier. A compilation's statement
  that its text was prepared with machine assistance is another
  sourcing circumstance (planned: "Compound artifacts").
- Redundant-but-asserted values (e.g. "Alter: 73 Jahre" alongside a death
  date) are kept verbatim as qualifiers (`age at death`), never derived or
  dropped: sources assert them, so the archive records them.
- Ambiguous date formats (07-01-1919: 7 January or July 1?) must not be
  silently disambiguated; extraction should downgrade to the precision
  actually supported or leave the claim to a curator.
- Named periods: statements may point to time-period items rather than
  dates; period items link to PeriodO ids.
- Never narrow a source's vagueness to make data "cleaner".

## Occupations, causes, counts (trial-observed vocabulary)

- **occupation** (P106 semantics) is item-valued: occupations recur
  across people and documents, so they reconcile like places do. The
  occupation item's label keeps the source's exact wording ("Hatter",
  "Master hatter"); cross-document variants unify through
  aliases and the ordinary equivalence review, never by normalizing
  the label.
- **cause of death** (P509 semantics) is monolingual text, kept
  verbatim in the source's language. Wikidata models causes as items;
  certificates and registers assert prose ("Wassersucht"), and the
  archive records exactly that.
- **sibling** (P3373) is written only when a source asserts
  siblinghood. It is never derived from shared parents — a source
  asserting two parent relations has asserted parentage, nothing more.
- **quantity** values (first use: number of children, P1971) are
  unitless numbers for source-asserted counts. A count is a claim like
  any other: competing counts coexist, and corrections in the review
  CSV take a plain number. `age at death` stays a verbatim string
  qualifier (above) — it records wording, not arithmetic.
- **place of burial** (P119) is an ordinary place-valued property;
  burial places join the place hierarchy pass (ADR-0009) like any
  other place.

## Places

- Reconcile to GeoNames and World Historical Gazetteer where possible;
  store external ids and coordinate location.
- Historical places that no longer exist are ordinary place items with
  their own time bounds.
- Containment between places (`part_of`) is world knowledge, not a
  source assertion: proposed by `yizkr reconcile-places` (ADR-0009),
  human-approved, cycle-guarded, and referenced to the pass's run item
  (process provenance; a GeoNames-imported edge adds `stated in` → the
  GeoNames dataset item). The inconsistency report suppresses value
  pairs connected by a `(location|part_of)+` path — differences of
  resolution, not contradictions.

## Artifact coverage — cross-domain relations (planned)

Artifacts do not only assert claims about people and events; they also
*cover* regions of other domains: a map covers a geographic extent, a
census page covers a district on a date, a letter spans two places.
These cross-domain coverage relations (cf. Dublin Core "coverage") are
ordinary statements on the artifact item, and the reference
requirement (ADR-0003) applies unchanged: what an artifact shows
about itself is referenced
`stated in → the artifact itself` (the registrar convention above);
coverage computed by a tool (e.g. georeferencing software) adds an
`inferred from extraction run` reference like any machine write.

Planned first instance — **georeferenced map artifacts**:

- a `map` subclass of artifact;
- two globe-coordinate properties on the map item, `bounding box
  north-west corner` and `bounding box south-east corner` (an
  axis-aligned extent; Wikidata's extremum-point properties
  P1332–P1335 are the nearest semantic precedent), so interactive
  maps (R6) can draw the artifact's outline in place and SPARQL can
  answer "which artifacts cover this place?";
- W2 upgrade path: IIIF Georeference annotations (Allmaps-style
  ground control points) refine the flat bounding box exactly as
  IIIF canvas+xywh URIs refine percentage regions (ADR-0004) — the
  corner properties then remain as a cheap derived summary for
  query and display.

Other domains follow the same shape rather than inventing new
machinery: temporal coverage (the period a map *depicts*, distinct
from when it was made) reuses `time period` / date properties, and
subject coverage reuses `depicts`. Prefer an existing
Wikidata-mirrored property, keep values structured (never strings),
and reference the artifact itself or the producing run.

## Compound artifacts (see ADR-0008)

- A container artifact (first case: `presentation`, a slide deck)
  keeps the standard registrar shape: its slides ARE page fragments
  (`part_of` + `page number` qualifier), and claim locators reuse
  `page_number` as the slide number.
- Each embedded medium becomes its own artifact item ("<deck title> —
  <media name>"; `instance of` photograph for raster images), with one
  `part_of` → the container per slide it appears on, slide-number
  qualified. Derived items are gallery- and faces-flow material in
  their own right. The stages before the write record them by SHA-1, and
  the write registers them from that record, after the container
  (ADR-0008 as amended by #128 and #103). Bytes the archive holds under
  several names are one medium: the first name labels it, and the others
  are aliases. A deck title that would push the label past 250 characters
  is cut there, ending in "…": the label only displays it.
- Every statement a container contributes to a derived item is
  referenced `stated in → that container` — a compound artifact asserts
  its own composition, the same convention as fragments and registrar
  statements above.
- Derived items follow artifact identity: they carry `media SHA-1`, so
  one image embedded in several containers is **one** item, `part_of`
  each of them (each statement slide-qualified and stated in its own
  container). A statement that several containers make alike (`instance
  of`, `media file`, `media SHA-1`) is one statement citing each of them;
  a later container's name for the item is an alias, unless another
  item already has that label (ADR-0008 as amended by #50).
- Unpacking is deterministic (documented ZIP+XML), never generative;
  only claim extraction from the slide text uses an LLM.
- **A compilation's original excerpts are artifacts of their own**
  (ADR-0008 as amended by #289, #176 and #397). A compilation sets text
  of its own (a typed translation, commentary) beside images of excerpts
  from the originals it draws on: two witnesses of one text, which may
  disagree. An image that a PDF places within one of its pages is a
  derived item like a deck's: keyed by its SHA-1, `part_of` the PDF once
  per page it is on (page-number qualified), labelled "<title> — page
  <n>, image <k>" for its first place, and an `instance of` photograph.
  An image that is a whole page, as a scanned page is, is that page. A
  PDF with a text layer is read page by page, and each image placed
  within one of its text-layer pages has a transcription of its own
  (ADR-0008 as amended by #176), which is extracted as a document of its
  own, an excerpt, whose claims cite the image; the write registers the
  image's item, after the PDF, before any claim that cites it (ADR-0008
  as amended by #397). The same image in several PDFs is one item and
  one excerpt; an image whose bytes a document of the corpus has is that
  document. An image whose reading's record calls every page blank or
  not a document page (a photograph with no writing, a colour chart) is
  registered but is no excerpt; an illegible one is an excerpt, so that
  a reader sees it was there. An excerpt's language and class are its
  reading's record's (ADR-0037). An image placed within a page read by vision is read with
  that page, as the compilation's.
- A claim cites the witness that asserts it. A claim read from the
  compilation's own text cites the compilation, with its page as the
  locator; a claim read from an excerpt cites the excerpt, with no page
  number and optionally a region of the image. Each transcription excerpt
  is tagged with the language it is written in, so a retyped
  transcription in the compilation takes the original's language. Each
  witness has its own transcription and its own batch, and an extractor
  cites the document it read, so neither witness's claims can cite the
  other.
- **Machine-assisted preparation is a sourcing circumstance.** When a
  compilation states that its text was prepared with machine assistance
  (that its translations are machine translations, say), each claim read
  from that text carries `sourcing_circumstances` → `machine_assisted`:
  the claims read from the passages the statement names, or from all of
  its own text when it names no part. The manifest's one sourcing
  circumstances value, `circa`, says that a value is approximate and
  does not fit, so `machine_assisted` (label "machine-assisted") joins
  the manifest with the extraction change that sets it (planned). The
  marker records what the source says of its own making: it is never
  inferred from how a text reads, never set on an excerpt's claims, and
  never a rank. The pipeline's own machine work stays process provenance
  (ADR-0003, ADR-0023). Like circa, it makes the compilation's claim a
  statement of its own beside the same value from a source without it.

## Faces and depiction (see ADR-0005)

- On a photograph item: depicts → person, with qualifiers:
  - relative position within image = "x%,y%,w%,h%"
  - (W2 alternative: annotation URI → W3C/IIIF annotation)
- Reference identifies the proposer (extraction/detection run) and
  confirmation is implied by the statement existing at all – unconfirmed
  proposals live outside the graph (review queue), never in it.
- W1 implementation: `python -m ingest detect-faces / enroll-face /
  match-faces` (see `ingest/faces.py`), feeding the shared review flow.
- **Associated media vs. depiction (see ADR-0021).** `image` (P18
  semantics) links a person to a medium a source *associates* with them (a
  portrait, an obituary scan) — distinct from `depicts`, which is a
  confirmed facial depiction with an image region and is never
  auto-written. The GEDCOM adapter emits `image` for OBJE multimedia
  objects as **un-held media items** (kind `artifact`, with no `media file`
  and no `media SHA-1` — the missing content hash *is* the un-held marker):
  the bytes are referenced from the export, never fetched from a third
  party (privacy rule 7). Acquiring the bytes is a separate,
  explicitly-instructed step, after which the medium joins the SHA-1-keyed
  artifact path (ADR-0010).

## Names (see ADR-0011)

Names are modelled in **two layers**:

- **Aliases** (the entity's label + `altLabel`s) remain the flat,
  append-only, *unreferenced* index that reconciliation blocks on and
  label lookup uses. Every name form a document uses goes here.
- **Typed name statements** are the source-first record, used when a
  source states a name's *kind*. Each is an ordinary referenced claim
  (monolingual text, source's wording kept verbatim) and may be dated
  with `start time` / `point in time` (a formal change is a dated
  event):
  - `birth name` (P1477) — name/maiden name given at birth;
  - `married name` (P2562) — family name taken on marriage;
  - `nickname` (P1449) — a name the source gives as what the person is
    called, by anyone ("Peppi", "Gustl"), or an "aka"; what one person
    calls another, or a name that names the person through a relation
    but is no literal kinship term, is `referred_to_as` instead (below);
  - `official name` (P1448) — a formally adopted or legally changed name.
- An *ordinary* untyped name is NOT restated as a claim — it lives in
  the label/aliases only. Typing adds structure where a source marks it;
  it never narrows or infers (a maiden name is recorded only when the
  source says so, never derived from gender + marriage).
- The GEDCOM adapter emits these from NAME TYPE, the NICK and `_MARNM`
  tags, and the inline convention that a quoted part is a nickname
  ("Cäcilie 'Cilli' Tarle"). A bracketed part ("Ida (Peppi) Sörne") may
  hold a nickname, a maiden name or a note, so it is an alias and an
  unsplit form of the mention record, typed as nothing and counted, and
  the label leaves it out; a NAME
  without slashes, whose parts no GIVN or SURN gives, is an unsplit form
  too, and its TYPE gives no claim, and one with only a GIVN or only a
  SURN keeps its whole value as an unsplit form beside that part. A
  `_MARNM` gives a married-name claim only where its slashes hold a
  surname; otherwise it is an unsplit form too (ADR-0035 §6). A GIVN's
  quotes and brackets read as the value's do: a quoted part is a
  nickname, a bracketed part an alias and an unsplit form. A word
  that holds no letter or digit ("?", "_", "()") is a placeholder, in a
  value, a bracket, a quote, a part or a NICK: it names nothing, so it
  is counted, not placed ("Eugen /?/" is labelled "Eugen"), and a typed
  name that held one anywhere but in a NICK, a stray bracket included,
  gives no typed claim; a single dash or "&" between two words of one
  field is a connector, kept, and one against a slash, a bracket or a
  quote, a "+" or a run ("--") is a placeholder. As many SPFX slots as
  SURN slots pair by position, empty slots included. German low quotes and guillemets mark a
  nickname as other quotes do. Where no NAME gives a
  name, the label falls back, marks and all, to brackets that hold a word
  ("(Peppi)"), then a GIVN's quotes or brackets, then a NICK, then a
  `_MARNM`; a person with
  none of these words is skipped and counted, as one with an empty NAME
  is. The adapter's texts are in the export's language (HEAD.LANG,
  mapped to BCP 47), or undetermined (`und`) where it states none.
  Typed names also feed reconciliation: a married
  name matching another's birth name (a marriage name change) is strong
  identity evidence.

**Name normalization (ADR-0022).** Three axes are kept distinct:
*composition* (which token is the fixed part vs. an alternating one — the
`/` in "Hildegard Laudler/Löbig" prescribes it, so those
surnames are alternative family names for one person), *kind* (given vs.
family — undecidable without name-order convention, so **never asserted**
and never a stored property), and *type* (birth/married/nickname/official —
recorded only where a source marks it, the marker possibly contextual). The
LLM is the primary normalizer: its prompt carries these standards (compose
no permutations, keep a clean label, strip embedded annotations, type only
what is marked). A thin deterministic validator (`pipeline/names.py`) is the
shared safety net over both the LLM and GEDCOM paths: it reads an alias
through the entity's mention record, never its token shapes (ADR-0035
§8). A person's alias that gives no name part is dropped, as is one whose
parts the label already gives ("Löbig, Ida" beside "Ida Löbig"); one that
adds a part is kept ("Wastl"), as is one that spells a part otherwise
than the label ("Gustav Lobig" beside "Gustav Löbig"), and so is the
first that alone holds a title or suffix the record gives ("Frau Löbig" beside "Ida Löbig", her
record titling her Frau). The GEDCOM adapter cleans by the record it
fills, as extraction does. A place's or an organisation's alias that
gives no part (a deictic or generic reference) is dropped, and a short form
kept. An entity without a record keeps its aliases, blanks and duplicates
aside. It never splits a name, so compound/particle surnames stay whole
(`Faxridge-van 't Nijks`), and asserts no kind. Composition and short-form
display are the frontend's to *derive*, never stored.

**Mention records (ADR-0035 §2).** Extraction's entity manifest may give
each person, place, organisation and family a `structure`, what its names
are made of, read from its label and aliases as the document writes
them, by the document's own conventions and never by a rule about a
language:

- **a person:** a status (`name`, `role`, `kinship`, `courtesy`,
  `placeholder` or `description`; only a `name` keys); given names as
  written, each with the standard form a diminutive or abbreviation
  stands for where the document supports one, and marked when it is an
  initial; surnames typed `birth`, `married` or `other`, with their
  particles; patronymics, a name part of their own (the 1941
  certificate's *Лёбиг Рудольф Феликсович* gives the given name Рудольф,
  the surname Лёбиг and the patronymic Феликсович); nicknames, the names
  the person is called by anyone, each also an alias (never a
  `referred_to_as` appellation, ADR-0018); titles and
  suffixes, which never key; and `unsplit` forms whose given name and
  surname the source does not tell apart;
- **a place:** a status (`toponym`, `deictic` or `generic`); its names,
  each with its toponym and generic head where they can be told apart; an
  address, with its street and number; and the containers the source puts
  it in, in the source's order (`within`: *Snylsjum, gemeente
  Drakeradiel, Fryslân, Nederlân* is Snylsjum within the other three);
- **an organisation:** its proper names, the words naming its kind, and
  its places (*Калдовское районное бюро ЗАГС* has a kind and a place, and
  no proper name);
- **a family:** its surnames.

**A record is never written to the wiki**: it is the pipeline's reading of
a name, not something a source asserts, so ADR-0022's Ruling 1 holds for
the graph. Records live in the batch view and the LLM cache, as local-only
data. A record may divide and label a name, never add to it: every part
must occur in the label or an alias, folded, as whole words (a standard
form and an organisation's places aside). A record that fails that, or
that does not parse, is set aside and counted, and the entity keeps its
label, aliases and kind. The GEDCOM adapter fills each person's record
from the NAME's value and its GIVN, SURN, SPFX, NPFX, NSFX, NICK and
`_MARNM` (ADR-0035 §6): a part the value does not restate reaches its
field, and the name it completes (the name the parts give, or an NPFX
or NSFX beside the value's name: "Dr. Eugen Löbig") becomes an alias,
counted; in a NAME with no value, a prefix or suffix with no given name
or surname beside it is counted instead, so none is dropped silently. A
NICK and a `_MARNM` are aliases too. A place's record takes its PLAC's
first component as its name and the rest as the places it is within.
What a marker in a name asserts (a widow's
marker, "the late", a patronymic's father, a married-name marker) is a
claim: a `civil_status`, a death, a `father`, a `married_name`.

The entity-manifest prompt asks for each record, and for each label's
status, by the structure of what it reads (a surname-first register,
capitals, a maiden-name or married-name marker, a courtesy title, a
place's generic head, an institution's kind), with examples from the
universe's sources in German, English, Russian and Frisian, and lists no
marker words. A person the document does not name is labelled with the
document's own words for them (the postcard's *Dein Vater*, status
`kinship`). Any manifest entity or claim may carry a **judgement note**
(ADR-0035 §4): a category (`kind`, `relation`, `date_reading`, `identity`
or `other`) and one sentence in English, only where the extractor made a
call a careful reader could make differently. `coverage.json` counts the
records given (and by status, and the `unsplit` forms they hold),
recordless and set aside (the records, and the reasons by category), and
the notes by category.

**Relational and perspectival appellations are NOT names** (ADR-0018).
"our great grandmother", "meine Oma", "Nana Maisie", "my husband" describe
a relationship or viewpoint, so they never enter the alias bag. A literal
kinship term is a `relative` + `kinship_type` claim with the ego as
object (§Kinship); a spousal term ("my husband") gives a marriage
(ADR-0036); any other relational appellation (a name the
source gives as what one person calls another, with or without a stated
perspective, or that names the person through a relation: a courtesy
title, "Bubele", "Рудольфиха") is a
`referred_to_as` (P1810) statement on the referent — verbatim, source-
referenced — qualified by `perspective` (the person/family ego) when the
ego is a modelled entity, and unqualified otherwise. Both are
multi-valued and coexist (ADR-0002): many people may call one person many
things. Because each appellation is a statement whose *subject* is the
referent and whose ego (the `relative` object or the `perspective`
qualifier) is the other party, reciprocal naming
(a grandmother "Oma" to a grandchild who is "Bubele" to her) is just two
independent statements — no dyad object, and the kinship inverse is
derived, not stored. With the ego anchored, a later bare reference
resolves: compositional terms by kinship derivation, idiosyncratic ones
via the appellation table.

## Kinship (see ADR-0014)

Kinship is modelled in **three layers**, so the graph stays a thin
primitive network and the *same* term set describes any pair:

- **Primitive edges (stored, source-attributed):** `father` (P22),
  `mother` (P25), `sibling` (P3373), `child` (P40). `sex_or_gender`
  (P21; `male`/`female`) is stored alongside — the calculus needs it to
  gender terms. A spouse is no stored edge (ADR-0036): a marriage is an
  entity whose couple take part in it with the role `spouse`, and the
  spouse link is derived from it by a query; the `spouse` property (P26)
  is retired. A spousal term ("my husband") gives a marriage, not a
  kinship class. `sibling` stays asserted-not-
  derived (a source may assert siblinghood directly) yet is *also*
  derivable from shared parents; both coexist.
- **A derivation calculus (query/frontend layer — NOT vocabulary):**
  second-order relations (grandparent, aunt/uncle, cousin, in-law) are
  property PATHS over the primitives, mapped to a shared gendered term
  set. See `queries/kinship.rq`; the full pairwise calculus with degrees
  (great-, once-removed, half-) is a frontend build over the same
  primitives. Never store a derivable relation.
- **A source-asserted compound fallback:** `relative` (P1038) with a
  verbatim `kinship_type` qualifier holds a compound relation a source
  states but that primitives cannot yet capture unambiguously ("maternal
  aunt", connecting parent unknown; blood vs marriage undetermined —
  never narrowed). One property, not one per relation; the calculus and
  this qualifier share the same term set, so asserted and derived
  relationships describe uniformly and coexist (a match corroborates, a
  mismatch is an ordinary inconsistency).
- **A kinship class beside the term (ADR-0035 §3.3).** A `relative`
  claim keeps its verbatim `kinship_type` and gains a controlled
  `kinship_class`: `kin_parent`, `kin_child`, `kin_sibling`,
  `kin_grandparent`, `kin_grandchild`, `kin_aunt_or_uncle`,
  `kin_niece_or_nephew`, `kin_cousin`, `kin_parent_in_law`,
  `kin_child_in_law`, `kin_sibling_in_law`, or `kin_relative` where the
  term's class is unclear. Three more qualifiers hold only what the term
  states, never a guess: `side` (`paternal` or `maternal`), `degree` (how
  many "great-"s, or a cousin's number) and `removes` (a cousin's
  removes). The 1958 letter's *дядя по отцу*, "uncle on the father's
  side", is `kin_aunt_or_uncle` of degree 0 (a term other than a
  cousin's with no "great-" has degree 0) on the `paternal` side; its
  *двоюродная
  сестра*, "first cousin", is `kin_cousin` of degree 1; the 1952 letter's
  *oerbeppe*, "great-grandmother", is `kin_grandparent` of degree 1. A
  literal term that reduces to a primitive stays one: *Dein Vater* is
  `father`.
- **Civil status is a person's own state, not a relation.** A source's
  stated civil status (`unmarried`, `married`, `widowed`, `divorced`) is
  a `civil_status` claim, dated as the source dates it. It names no
  partner and makes no marriage; a marriage comes only where a source
  names the partner (ADR-0036).
- **Participant roles.** A `participant` qualified by `object_has_role`
  (P3831) says how someone took part in an event: the `principal` (the
  one born, who died, or to whom it happened), a `parent`, the
  `informant` who reported it, a `witness` or the `officiant`; in a
  partnership or marriage, a `partner` or a `spouse`. One claim may carry
  several roles, but a death's principal is never also its informant.
  The GEDCOM adapter makes each death event it makes with the person as
  its `principal`, and each union's couple its `spouse`s or `partner`s.

## Residence and co-residence (see ADR-0014)

`residence` (P551) is directed **person → place** and multi-valued: each
co-resident is the subject of their own referenced statement pointing at
the same place item, with its own date qualifiers. The place is never the
subject; there is no household statement enumerating occupants.

- **Attribution on extraction.** An address stated on a form is asserted
  for the person the field names (e.g. the informant on a birth
  certificate), not for everyone the document mentions. One person's
  stated address is never propagated onto others as their residence; a
  dependent's residence inferred from a relative's address (a newborn at
  the parents' address) is recorded only at lowered confidence.
- **Co-residence is derived, never stored.** "Lived with" is a PATH —
  two people with overlapping residences at the same place over
  overlapping time — computed in the query/frontend layer, exactly like
  kinship. No `co-resides-with` edge. (Asymmetric sheltering — "gave
  refuge to" — is the separate, non-derivable case ADR-0014 tracks.)

## Marriages, residences and other periods

ADR-0036 makes a marriage an entity bounded by events, and retires the
`spouse` statement below; its vocabulary is in the manifest ("Core
classes"). The extraction prompts ask for marriages so: the couple take
part in the marriage (or partnership) with the role `spouse` (or
`partner`), the union is `started_by` its wedding and `ended_by` a
divorce, an annulment, a partner's death, or an event of unstated kind
where the source does not say how it ended, and each of those events
carries its own date and place. Until the rest of the pipeline moves to
marriages, the readers of spouse statements (among them the disagreement
report, the timeline, the kinship query, reconciliation, clustering and
the static site) still read them; extraction refuses and counts any
spouse claim.

**The GEDCOM adapter makes unions (ADR-0036 §5).** A family with two
partners (its `HUSB` and `WIFE`, or, with no `WIFE` or no `HUSB`, two
`HUSB`s or two `WIFE`s, counted, the second a partner only and never
the children's parent) is a `marriage` when a `MARR` of it states a
wedding, or when a `DIV` or `ANUL` states its end with no such `MARR`
(a marriage with no wedding, counted), otherwise a `partnership` (the
export gives Felix and Ida no `MARR`, so theirs is a partnership); its
couple are its participants with the role `spouse` or `partner`. A family
whose `HUSB` and `WIFE` name the same person gives no union and no
parent claim, counted. A `MARR` states its
wedding as a `DIV` its divorce, by a `Y` or a `DATE` or `PLAC` with a
value: a `MARR N`, or one that states nothing, makes no wedding and is
counted. The family's `MARR`s of one kind are one wedding, which the
union is `started_by`: a `civil_wedding` or a `religious_wedding` where
the `MARR`'s `TYPE` names the ceremony (`civil`, `religious`, `church`,
and their German forms), else a `wedding`, which a `MARR` whose `TYPE`
names no known kind joins, that `TYPE` counted. Its `DIV`s are one
divorce and its `ANUL`s one annulment, which the union is `ended_by`,
each `PLAC` the event's `location`, and a `DIV Y` an undated divorce,
counted, as is one whose `DATE` gives no date. Each
date and place of each record is a claim of its own on that event: its
`point_in_time`, with the range and circa its `DATE` states. A `FROM … TO
…` on a `MARR` dates the wedding by the `FROM` and an end of unstated kind
(an `event`) by the `TO`; on a `DIV` or an `ANUL` it is the event's own
`start_time` and `end_time`, as an imprisonment is dated. No end is
inferred from a partner's `DEAT`. A `SOUR` beneath a `MARR`, `DIV` or
`ANUL` cites that event's claims and the union's claim that points to
it; the family record's own `SOUR`s cite the couple's claims.

A statement that holds for a period, such as a residence, an occupation
or a membership, is dated as Wikidata dates spouse (P26); a marriage is
dated by its events (ADR-0036), and the retired spouse statement's
dating below is the convention its readers still interpret:

- **A retired spouse statement's `start_time` dates the marriage**, and
  its `end_time` the date it ended (a divorce, an annulment, or
  widowhood, when the source dated the end itself): under that
  convention, Leopold and Agnes's marriage, from 8 February 1947 until
  the divorce of 2 January 1957 per the judgment, was a spouse statement
  with that start time and end time, each partner's carrying the dates
  its source gave. Nothing writes one now: the judgment's marriage is a
  `marriage` `started_by` its wedding and `ended_by` the divorce. A
  move in and a move out date a residence by `start_time` and
  `end_time`, and the GEDCOM adapter reads
  a residence's or an occupation's date period (`FROM 1939 TO 1952`) as
  its start and end time, each with the range its own date gives, and
  with one circa when either end is approximate.
- **`point_in_time` on a spouse or residence statement dates an
  observation**: a date at which the source presents the marriage or the
  residence as holding (a directory's listing, a census) without dating
  its start or end. It never carried a wedding date. A source that merely
  named a spouse, without dating the marriage, gave an undated spouse
  statement: the document's own date was no observation of the marriage
  (Ida's registration card of 1945 names Felix, dead since 1931, as her
  husband). Now such a source gives a union with no date, and the
  document's date is still no observation of it.
- **Each end has a range of its own.** A statement has one start and
  one end, and a source may bound either, so each end takes its own pair
  of qualifiers, as on Wikidata since `latest start date` (P8555) and
  `earliest end date` (P8554):
  - a start runs from its `earliest_date` to its `latest_start_date`;
  - an end runs from its `earliest_end_date` to its `latest_date`.

  `earliest_date` and `latest_date` are thus the outer bounds of the
  period: the earliest it can have begun and the latest it can still have
  held. A marriage "between 1946 and 1947" is a start time of 1946 (the
  first date, as a range is stored) with an earliest date
  of 1946 and a latest start date of 1947; a divorce "before 1958" is an
  end time of 1958 with a latest date of 1958. An earliest date never
  bounds an end, nor a latest date a start.

  These pairs bound start and end time *qualifiers*. An entity's own
  `start_time` or `end_time` claim, such as the firm's founding in 1863
  or the start of Leopold's detention in 1939, is a date value like a
  date of birth: `earliest_date` and `latest_date` bound it, and the
  disagreement report compares it as a single-valued date. A latest start
  date or an earliest end date never qualifies such a claim.
- **On a statement with no start or end time**, `earliest_date` and
  `latest_date` bound its point in time, as they bound a date value: a
  residence observed "between 1938 and 1939" is a point in time of 1938
  with those two dates. A point in time beside a start or end time has no
  range, since there those dates bound the period.
- **Circa** (`sourcing_circumstances`) qualifies the whole statement. On
  a spouse statement with a start and an end, it says that the source
  gives at least one of them approximately, and cannot say which, rather
  than drop it (rule 5).

The disagreement report compares a residence's or a spouse's start and
end times, each through its own range ("Contradictions" below); the
person timeline (`queries/person-timeline.rq`) lists each start, end and
point in time with the range stated for it, which the static site's
timeline page and feed show beside the date, each bound under its
qualifier's label ("1946 (earliest date: 1946; latest start date:
1947)"); and the extraction prompts give the extractor this convention
for residences and other periods, and date a marriage by its wedding and
end events instead.

## Persecution and displacement as events (see ADR-0014)

Forced removal, detention, and freeing are **events**, not relations:
`deportation`, `imprisonment`, and `release` are event subclasses that
reuse the event machinery (`participant` → person, `location` →
destination, `point_in_time`/`start_time`/`end_time`). "Deported to
Klurnau" is a deportation event, never a `deported to` property — the
same shape as the reconciled deportation events already in the graph.

These relations are therefore **not** open-schema gaps (ADR-0013): a
`deported to` or `imprisoned at` line in `gaps.csv` reports something
the vocabulary already holds, so it is an extraction mistake to fix,
not a signal to grow the schema on.

## Identity and reconciliation (ADR-0007)

- Every ingested document contributes its name variants as item
  **aliases** (append-only); matching quality compounds per document.
  Where a source *types* a name (birth, married, nickname, official),
  it is additionally recorded as a referenced statement — see "Names".
- A scratch run clusters every document's mentions across the corpus
  before any write (the clustering stage, `pipeline/clustering.py`,
  ADR-0025). It keys and judges on each mention's record, never on a
  label's wording (ADR-0035 §7): a person's given names by their standard
  forms and as written, with its surnames and its kin's; a place's names
  and address as written, its containers (its record's, and those its
  manifest's `part_of` and `location` claims give) compared as one set; an
  organisation's proper names, else its kind with its places, which agree
  only when they would link as places or one lies in the other; an umlaut
  also written out (DIN 5007-2). A person, place, organisation or family
  without a record (none given, or one extraction set aside under
  ADR-0035 §5, before the alias cleanup) meets only its exact name, and
  never merges; so a document's mention whose record was set aside never
  meets the GEDCOM export, whose people and places all have records; the
  kinds that take no record (occupations, vessels and the like) still
  merge on an exact name. A union is keyed by its couple,
  and its weddings and ends by union, kind and date (ADR-0036 §3). A
  birth's or a death's date belongs to its principal alone, and one with
  no principal, several, or one whose own date of that kind conflicts
  with it, is nobody's and merges with none.
- Machine-proposed equivalences (within or across documents) are
  reviewed by a human before they take effect — identity is never
  auto-confirmed. Approved matches resolve new claims to the existing
  item rather than minting a duplicate.
- Where equivalence of two *existing* items is itself a judgement a
  source supports, it is an ordinary referenced statement:
  `said to be the same as` (Wikidata P460), citing the artifacts whose
  content supports the identification and the reconciliation run.
  True duplicates are curator-merged instead.
- Candidate pairs the graph already **proves distinct** are pruned
  before adjudication (ADR-0017): an irreflexive kinship edge between
  them, or one place transitively inside the other. This is recall-safe
  — it drops only pairs the graph's own referenced statements
  structurally contradict, never merely dissimilar ones; unknowns stay
  live candidates. Deliberately excluded is a date disagreement: that is
  healthy coexistence (ADR-0002) to surface, not proof of distinctness.
  A rejected equivalence that is really a containment (a camp within a
  country) is handed to reconcile-places as a `part_of` proposal rather
  than discarded.

## Contradictions

- Competing values for the same property coexist as separate
  statements at **normal rank**, each with its own references.
- Rank changes are curator actions, logged by MediaWiki history, and
  optional forever.
- Disagreement *reporting* pairs values that **different artifacts**
  attribute (ADR-0023 §4). Two queries fetch what it compares, with no
  pairing of their own: `disagreement-report.rq` lists every statement of
  the compared properties with its rank, value, dates and stated-in
  sources, and `place-containment.rq` the recorded containment edges and
  merge redirects. `yizkr.disagreement` compares them in Python, one
  function per rule below (ADR-0033); the static site's inconsistencies
  page shows its rows, each value with every source that states it. Two
  readings of one artifact (a curator's correction beside the machine
  reading it corrects) are not two sources, and a page fragment counts as
  its artifact. An image that a deck or a compilation embeds is an
  artifact of its own, not a fragment, so a compilation and an original
  excerpt it embeds are two sources (ADR-0008). A statement with no
  stated-in source attributes its value to no artifact and takes no
  part. (The unreferenced-statements report lists only a statement with
  no reference at all, not one whose references lack a stated-in
  source.) A deprecated statement is left out; a preferred one is listed
  like any other, so the report never needs a preference.
- It compares values only for properties that can hold at most one true
  value per subject (dates/places of birth and death, parents, birth
  name, an event's or other entity's own point in time or start/end time,
  a place's identity properties). A name compares as text: spellings that
  differ only in letter case (a sharp s written SS among them) or
  language tag are one value, listed once with the sources of all of
  them. Properties that legitimately accumulate values over a life —
  spouse, residence, married names, nicknames, official names — are
  multi-valued by nature: two spouses asserted by one source are
  remarriages, not a contradiction (their start/end qualifiers tell them
  apart). For one residence place or one spouse (through merge
  redirects), the report compares the start and end times that different
  sources give (in Australia from 1939 per one source, from 1938 per
  another), so a return to a place a person had left shows as a
  start-time disagreement. A point in time on a residence or spouse
  statement dates an observation, not the residence or the marriage
  ("Marriages, residences and other periods"), and is not compared.
- A date whose span lies inside another's and a place the graph records
  inside the other differ in resolution, not substance, and are not
  reported. A date's span is the whole period its precision names (a
  month, a year, a decade, or a century as Wikibase counts it, which
  holds 1900 in the 19th century and 1800 in the 18th: "Uncertainty and
  time"), or the range its source states with `earliest date` and
  `latest date`, open-ended when only one is given: 1909 and 16 March
  1909 agree, as do "between 1938 and 1939" and either 22 June 1938 or
  3 September 1939, and "before 1926" and 1912, while "after 1945" and
  1941 disagree. The 19th century agrees with 1900 and with the 1890s,
  and disagrees with 1800 and with the decade 1900–1909, which reaches
  past it. A stated range is never widened back to its precision (rule
  5): 1939 stated "between September 1939 and 1 October 1939" and August
  1939 disagree. A precision coarser than a century names no span. A range
  dates only a time: on any other value, such as a place, a person or a
  name, it sets nothing apart. Two dates stored alike are compared
  through the ranges their statements state: "before 1945" and "after
  1945", both stored as 1945, disagree; with the same stated ranges, or
  none, they differ at most in resolution, as do dates stored alike whose
  spans nest: the 1890s stated as a decade, as "between 1890 and 1899" or
  as 1890, all stored as 1890. A statement that carries two earliest (or
  latest) dates states its value once per range, and each is compared
  with other statements' values; its own readings alone are one claim,
  never listed against each other, however many artifacts cite it. A
  residence's or a spouse's start and end times are compared like any
  other date, each through the range its statement states for that end:
  a start from the earliest date to the latest start date, an end from
  the earliest end date to the latest date. So a marriage begun "between
  1946 and 1947" and one begun on 8 February 1947 differ only in
  resolution, while a latest date, which bounds the end, sets no start
  apart.
- **Machine uncertainty is process, not graph data (ADR-0023).** The
  graph records the *source's* vagueness (circa, ranges, precision) but
  never the *machine's* confidence. Contentiousness surfaces ephemerally
  at review (the critic's per-concern axis and category:
  `[subject]`/`[predicate]`/`[value]`/`[existence]`, each finding a
  validated record on exactly one review row, ADR-0023 §3); reappraisal of low-scrutiny machine writes is a structural
  query (`under-witnessed-statements.rq`, the complement of the
  disagreement report), and the bulk-accept disposition lives in the
  reviewed CSV + MediaWiki edit history — never a stored flag.

## Living persons and privacy (see ADR-0020)

- **No stored "living" flag.** Whether a person is *possibly living* is
  never recorded and never a review decision; it is a **runtime
  determination** made whenever a privacy-sensitive action runs (the
  non-privileged export, an internet-facing publish), computed from the
  **absence of death evidence** in the graph. The determination lives in
  `yizkr.core.privacy` and both the export and the private static-site
  badge call it, so they always agree.
- **Death evidence** is any source-asserted signal that presupposes
  death: a `date of death`, `place of death`, or `cause of death`; a
  `place of burial` (burial presupposes death); or participation in a
  `death` event. None of these requires *both* a date and a place, so a
  person can be recorded as **deceased without a time or place of
  death** — a burial place, a cause of death, or an undated death event
  each suffices. Death is an event like any other (ADR-0014), so "died,
  nothing else known" is a death event with the person as participant.
- **No curation duty.** There is no `living` column in the review CSV
  and nothing the reviewer must decide. Privacy falls out of what
  sources already assert; a curator who knows a person is dead records
  that as an ordinary death assertion, not a flag.
- The export (`yizkr backup`'s `nonprivileged.nt.gz`, a non-privileged
  view, not a public copy) is **conservative**: a person enters it only
  on positive evidence they are not living — any death evidence above, or
  births beyond the cutoff (`--cutoff-years`, by default
  `DEFAULT_CUTOFF_YEARS` in `yizkr.core.privacy`). **Unknown means
  excluded.** Exclusion removes every mention (other people's claims
  about them, orphaned provenance nodes), and a transcription excerpt —
  a raw source quotation — leaves only with a statement it attests. A
  statement whose value, or any qualifier's value, is an item left out
  goes whole, on a kept item too, with its rank, qualifiers and
  references (ADR-0020, as amended by #364): an appellation of a
  released person whose `perspective` is a possibly living relative
  goes, its text included, and a statement naming a possibly living
  partner takes its start time and its excerpt with it. Its truthy
  triple goes too, unless a kept statement of the same item and
  property, at normal or preferred rank, gives the same value; a
  statement of unknown value that goes takes every truthy triple of
  unknown value of its item and property, as the export does not read
  which one it gave (it does not reproduce Wikibase's blank-node
  labels). A reference or value node that a kept statement uses too
  stays, its excerpt included, once, in the first kept entity's block
  that uses it, where the query service's munger reads it (ADR-0020, as
  amended by #412).
- **Who is a person fails closed too** (ADR-0020, as amended by #344).
  An item is a person when it has any person evidence, read at every
  rank, deprecated included: a `human` type; a statement of a property
  only a person has (a birth, a death or a burial, kinship, a birth or
  married name, a nickname, what they are referred to as, a sex or
  gender, a number of children, an occupation, an employer, a school, a
  citizenship or a residence); being the value of a kinship property or
  of `gave refuge to`, in a statement or a qualifier; or taking part in a
  birth, a death, a marriage, a migration, military service, a
  deportation, an imprisonment or a release. So a deprecated or outranked
  `human` type hides no one. An official name, a religion or a
  membership, a part in an event of another kind, a depiction, an
  identity judgement (said to be the same as), a perspective or any
  other qualifier, and the values of place, organization, artifact and
  vocabulary properties mark no one. A type is read through the
  redirects it names, so a class a curator merged into `human` types as
  `human`. An item typed as a vessel, a military unit or an
  organization at normal or preferred rank is taken
  at its type on its part in a person event: any other evidence makes it
  a person, a `human` type at any rank included. A family is not taken
  at its type: its label is a surname, and a person known by a surname
  alone can be typed as one. The export reads this evidence from its own
  dump and **keeps a person only once the check has released them**: an
  item the check never assessed is dropped, and a redirect goes with the
  item it redirects to.
- **What states no type is left out of the non-privileged export**, whatever
  else it holds, so no type at all hides no one either: an item none of
  whose `instance of` statements at normal or preferred rank names an
  item (an unknown or no value, or a type a curator deprecated, names
  none). The vocabulary is kept: the manifest's items, and every item
  that is the value of an `instance of` or `subclass of` statement. An
  item left untyped (a write cut short between its label and its type, a
  reference that gives no kind) is missing from the non-privileged
  export until it is typed. A run item left so is typed by the next run
  that finds it ("Artifacts and registration").
- **A family never enters the non-privileged export** (ADR-0020, as
  amended by #344). A family stands for its people, the people recorded
  as `member of` it, and is private while any of them may be living. The
  model cannot state that a family's membership is complete (there is no
  household statement enumerating occupants, above), so the export
  leaves out every item typed `family` at normal or preferred rank,
  whatever it holds, released or not, and the site badges it. #374 holds
  the full rule, with a way to mark a family's membership complete.
- **An event goes with its participants** (ADR-0020, as amended by
  #363). An event's date and place are its participants' too, and its
  label is written after them, so the export leaves out every event,
  whatever its kind, that has a `participant` statement at any rank
  whose value it leaves out, a redirect to one included, or one with a
  qualifier that names such an item, whatever the statement's value, an
  unknown one or none included (#364). An event goes, too, with any item
  that one of its statements names, as its value or a qualifier's,
  whatever the property: a divorce `carried out by` an organization left
  out for its person evidence goes with it (the orchestrator,
  2026-10-02). The rule follows only items left out to protect people:
  an untyped place, or a family (or any item named a `member of`) with
  recorded members all released, names no
  one, so a released person's birth at an untyped place stays, without
  the statement naming it; a family with a member who may be living, or
  that names no member, takes the event with it, and so does an untyped
  item that may be a person (through redirects), that the participant
  or unattended rule leaves out too, or that names an item left out to
  protect people. Such a group whose members are all released takes
  part in an event harmlessly, as a released person does, except in a
  person kind of event, which is its members' own, however the event
  names it (as a participant or, say, `carried out by`). Members are
  read through the groups among them, through redirects, ending on
  cycles: a member group with a member not released, that names no
  one, or that has person evidence and was not released by the check
  makes the outer group take events with it, and so does a
  `participant` of unknown value. A person named a `member of` (a
  household recorded under its head) is a member whose standing
  counts, and whose own members are read too; a person is never a
  released group. An event that names the released head of a
  household stays, whoever is recorded under that head: a released
  person is judged on their own standing alone, and only a group by its
  members. An untyped item with members is judged as a group, on its
  members. A class the dump makes a subclass of a person kind of
  event is a person kind too, everywhere in the export's reading of the
  dump; the check's own selection of people does not follow such
  classes, so a participant of one that the dump alone makes a person is
  left out as not assessed, failing closed. An
  event is an item typed `event`, a subclass of it or a person kind of
  event, or an item with a `participant` statement, but never a union (a
  partnership or a marriage, or a subclass of one), whose events go with
  it and not it with them, nor an item typed human at normal or
  preferred rank, unless typed a family too (for the participant rule
  too). An
  event so left out may itself be a participant, or named by another
  event, and the rule applies again. An event of a person kind that
  names no participant (none, or only one of unknown value, however
  qualified: what a qualifier names, a role included, is no person
  evidence and attends no event on that alone) is left out too: whom it
  happened to is unknown; and so is an event of any kind that has
  participant statements, every one of an unknown value or none (the
  orchestrator, 2026-10-02, failing closed as ADR-0036 §6 does for
  unions), while one of no person kind with no participant statement at
  all names no one and stays. And the events that start and end a union
  it leaves out go with the union (`started_by`, `ended_by`;
  ADR-0036 §6), a partner's own death aside.
- **Everything about a released person stays, text included** (ADR-0020,
  as amended by #363). The owner decided (2026-09-29): "I don't intend
  to make a completely public copy, so any non-privileged view can
  include all information on confirmed-deceased people." So the export
  keeps a released person's labels, descriptions, aliases, names and
  titles, their statements' values and the excerpts of the references
  those statements keep, and everything about the other items it keeps
  (a place, an organization, an artifact, the vocabulary, an event whose
  participants it all keeps); families and untyped items stay out. Such
  a text can name a possibly living relative, and it is visible in the
  non-privileged view: a consequence presented to the owner with the
  rule on 2026-09-29, which drew no objection. The extractor's
  judgement notes never leave (ADR-0035), in any form. A line the export
  cannot read as a triple is dropped, and a dump that holds a sitelink,
  whose page's own triples never name its entity, or a property of a
  type the model does not use (a URL, a media file, a shape) makes the
  export refuse to run.
- The check runs on the query service, so the export waits for the query
  service to hold the dump's revision of every item and property, and
  **refuses to run** while it does not, or when the graph changes during
  the check. The match is exact: an entity whose revision never reaches
  the query service keeps the export refusing, naming the entity, until
  that entity is edited again or the query service is reloaded from a
  full dump.
- **Births fail closed** (ADR-0020, as amended by #334). A birth releases
  a person only at the latest year it allows, and only when every
  `date of birth` statement a curator has not deprecated does so by the
  cutoff, never one value sampled from them. A statement's latest year
  is its latest date's, when it states one, as a stated range bounds a
  date ("Contradictions"); it has none when it states only an earliest
  date, since "after 1850" is open at its late end. Otherwise it is the
  last year its value allows at its precision: 1899 for the 1890s, and
  2000 for the 20th century, with centuries and millennia counted as
  Wikibase counts them, except that a round hundred is read as the
  century it opens, so 1900 at century precision allows 1999. A
  statement whose value or an earliest date lies wholly after its own
  latest date is read without that latest date, which fails closed. A
  statement allows no latest year, and keeps its person possibly living,
  when a latest date in it cannot be read, or, where no latest date
  bounds it, when its value is unknown, unreadable (one before the common
  era included) or coarser than a millennium, or it states only an
  earliest date. A readable latest date bounds even a birth whose value
  is unknown, unreadable or coarser than a millennium.
- The private static site (`yizkr publish-site`) includes everyone but
  badges each subject the export would drop ("possibly living — never
  in the non-privileged export"), using the same runtime call, except a
  person with no type, whom the export leaves out even when released;
  the site shows no badge. A family it lists is always badged. Its
  people are the items with person evidence, selected as the check
  selects them; the export also drops what the site lists as no person,
  an untyped item without person evidence, a family without person
  evidence, a redirect and an event that one it drops took part in.
