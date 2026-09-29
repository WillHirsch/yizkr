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
- Schema-level statements (e.g. the subclass hierarchy) carry
  references too (ADR-0003): a `stated in` reference to the *model
  manifest* item, which represents `model/manifest.yaml` — the document that
  asserts them.

## Core classes (items, via "instance of")

- human
- place
- time period (linked to PeriodO definitions where applicable)
- event (birth, marriage, migration, military service, ...)
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

## Reference pattern (see ADR-0003)

Every statement carries at least one reference with:
- stated in → artifact item (or artifact-fragment item)
- locator qualifier(s): page number, and/or region given as a
  percentage bounding box "x%,y%,w%,h%" (W1/W3) or a IIIF
  canvas+xywh URI (W2)
- optional short transcription excerpt supporting human verification,
  tagged with the language it is written in (BCP-47). `yizkr ingest run`
  detects each document's language from its transcription by default
  (`--language auto`, ADR-0015), so a mixed-language folder tags each
  excerpt correctly; the extractor may still tag individual excerpts in
  their own language (a quoted passage in another tongue)

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
- Registrar statements (`instance of`, `media file`, a fragment's
  `part of` + page qualifier) are referenced `stated in -> the artifact
  item itself`: an artifact asserts its own kind and composition by
  existing. Extraction-run items use the same convention for their
  `instance of`. Every registrar/writer path therefore writes only
  referenced statements (ADR-0003).
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
  registrar keys on this hash — identical bytes are one artifact item
  however they are named, and an alternate filename becomes an alias
  unless it is another item's label.
  Bit-identical media is never stored, itemised, or extracted twice.
- Labels stay unique per kind: when a title already names *different*
  content, the new artifact's label is disambiguated with a short
  content tag (`"<title> [<sha1[:8]>]"`) rather than colliding.
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
  organization has a `location`, it is not one. Typed by primary role —
  a source that treats an entity as an agent (employs, is deregistered,
  helps, hires) makes it an organization; one that treats it as a
  location (born at, died at, lives in) makes it a place. Dual-nature
  entities (a hospital that is also a birthplace) pick their dominant
  use in the document; genuine dual `instance of` is deferred.
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
  their own right.
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
  (planned: ADR-0008 as amended by #289 lists what is still to do,
  starting with #176). A compilation sets text of its own (a typed
  translation, commentary) beside images of excerpts from the originals
  it draws on: two witnesses of one text, which may disagree. An image
  that a PDF places within one of its pages is a derived item like a
  deck's: keyed by its SHA-1, `part_of` the PDF once per page it is on
  (page-number qualified), and labelled "<title> — page <n>, image <k>".
  An image that is a whole page, as a scanned page is, is that page.
  Until #176 routes PDFs by page, a PDF is read whole, and every claim
  read from it cites the PDF.
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
  - `nickname` (P1449) — informal/pet name or "aka";
  - `official name` (P1448) — a formally adopted or legally changed name.
- An *ordinary* untyped name is NOT restated as a claim — it lives in
  the label/aliases only. Typing adds structure where a source marks it;
  it never narrows or infers (a maiden name is recorded only when the
  source says so, never derived from gender + marriage).
- The GEDCOM adapter emits these from NAME TYPE, the NICK and `_MARNM`
  tags, and the universal inline convention (a quoted or parenthetical
  token is a nickname). Typed names also feed reconciliation: a married
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
shared safety net over both the LLM and GEDCOM paths: it drops
re-derivable short-forms (a bare given name, an initialism, a surname the
label already carries) and keeps compound/particle surnames whole (never
shattering `de la Cruz` / `van der Berg`), asserting no kind. Composition
and short-form display are the frontend's to *derive*, never stored.

**Relational and perspectival appellations are NOT names** (ADR-0018).
"our great grandmother", "meine Oma", "Nana Maisie", "my husband" describe
a relationship or viewpoint, so they never enter the alias bag. A literal
kinship term is a `relative` + `kinship_type` claim with the ego as
object (§Kinship); a courtesy title or family/pet name is a
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
  `mother` (P25), `spouse` (P26), `sibling` (P3373), `child` (P40).
  `sex_or_gender` (P21; `male`/`female`) is stored alongside — the
  calculus needs it to gender terms. `sibling` stays asserted-not-
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

A statement that holds for a period, such as a marriage (`spouse`), a
residence, an occupation or a membership, is dated as Wikidata dates
spouse (P26):

- **A marriage's date is the `start_time` of the spouse statement**, and
  the date it ended (a divorce, an annulment, or widowhood, when the
  source dates the end itself) is its `end_time`: Leopold and Agnes's
  marriage, from 8 February 1947 until the divorce of 2 January 1957 per
  the judgment, is a spouse statement with that start time and end time.
  Each partner's spouse statement carries the dates its source gives, and
  the GEDCOM adapter writes a family's `MARR` date as the start time of
  both partners' statements and its `DIV` date as their end time. A
  family that repeats a `MARR` or a `DIV`, as GEDCOM states an
  alternative, gets a pair of statements of its own for each alternative
  date or place it gives, with its own circa, beside the pair that its
  first marriage and first divorce date: an export that dates Leopold and
  Agnes's marriage both 8 February 1947 and 1947 gives one pair that
  starts on 8 February 1947 and one that starts in 1947. A move
  in and a move out date a residence the same way, and the adapter reads
  a residence's or an occupation's date period (`FROM 1939 TO 1952`) as
  its start and end time, each with the range its own date gives, and
  with one circa when either end is approximate.
- **`point_in_time` on a spouse or residence statement dates an
  observation**: a date at which the source presents the marriage or the
  residence as holding (a directory's listing, a census) without dating
  its start or end. It never carries a wedding date. A source that merely
  names a spouse, without dating the marriage, gives an undated spouse
  statement: the document's own date is not an observation of the
  marriage (Ida's registration card of 1945 names Felix, dead since 1931,
  as her husband).
- **Each end has a range of its own.** A statement has one start and
  one end, and a source may bound either, so each end takes its own pair
  of qualifiers, as on Wikidata since `latest start date` (P8555) and
  `earliest end date` (P8554):
  - a start runs from its `earliest_date` to its `latest_start_date`;
  - an end runs from its `earliest_end_date` to its `latest_date`.

  `earliest_date` and `latest_date` are thus the outer bounds of the
  period: the earliest it can have begun and the latest it can still have
  held. A marriage "between 1946 and 1947" is a start time of 1946 (the
  first date, as the GEDCOM adapter stores a range) with an earliest date
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
  gives at least one of them approximately, and cannot say which: the
  GEDCOM adapter writes one circa when the marriage's or the divorce's
  date is approximate, rather than drop it (rule 5).

The disagreement report compares a residence's or a spouse's start and
end times, each through its own range ("Contradictions" below); the
person timeline (`queries/person-timeline.rq`) lists each start, end and
point in time with the range stated for it, which the static site's
timeline page and feed show beside the date, each bound under its
qualifier's label ("1946 (earliest date: 1946; latest start date:
1947)"); and the extraction prompt gives the extractor this convention.

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
  at review (the critic's per-concern axis: `[subject]`/`[predicate]`/
  `[value]`); reappraisal of low-scrutiny machine writes is a structural
  query (`under-witnessed-statements.rq`, the complement of the
  disagreement report), and the bulk-accept disposition lives in the
  reviewed CSV + MediaWiki edit history — never a stored flag.

## Living persons and privacy (see ADR-0020)

- **No stored "living" flag.** Whether a person is *possibly living* is
  never recorded and never a review decision; it is a **runtime
  determination** made whenever a privacy-sensitive action runs (the
  public export, an internet-facing publish), computed from the
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
- The public export (`yizkr backup`) is **conservative**: a person
  enters it only on positive evidence they are not living — any death
  evidence above, or births beyond the cutoff (`--cutoff-years`, by
  default `DEFAULT_CUTOFF_YEARS` in `yizkr.core.privacy`). **Unknown
  means excluded.** Exclusion removes every
  mention (other people's claims about them, orphaned provenance nodes),
  and transcription excerpts — raw source quotations, which can name
  anyone — never enter the public tier.
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
  vocabulary properties mark no one. An item typed as a vessel, a
  military unit or an organization at normal or preferred rank is taken
  at its type on its part in a person event: any other evidence makes it
  a person, a `human` type at any rank included. A family is not taken
  at its type: its label is a surname, and a person known by a surname
  alone can be typed as one. The export reads this evidence from its own
  dump and **keeps a person only once the check has released them**: an
  item the check never assessed is dropped, and a redirect goes with the
  item it redirects to.
- **What states no type is left out of the public export**, whatever
  else it holds, so no type at all hides no one either: an item none of
  whose `instance of` statements at normal or preferred rank names an
  item (an unknown or no value, or a type a curator deprecated, names
  none). The vocabulary is kept: the manifest's items, and every item
  that is the value of an `instance of` or `subclass of` statement. An
  item left untyped (a write cut short between its label and its type, a
  reference that gives no kind) is missing from the public copy until it
  is typed.
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
  badges each subject the export would drop ("possibly living — never in
  public exports"), using the same runtime call. Its people are the items
  with person evidence, selected as the check selects them; the export
  also drops what the site lists as no person, an untyped item without
  person evidence and a redirect.
