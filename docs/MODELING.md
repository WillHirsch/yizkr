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
- occupation (values of the `occupation` property; labels keep the
  source's wording)
- unclassified entity (the open-schema holding class — see ADR-0013)
- artifact fragment (a page, crop, or region of an artifact)
- extraction run (one pipeline execution: model name+version, date,
  prompt/pipeline version, software commit)

## Reference pattern (see ADR-0003)

Every statement carries at least one reference with:
- stated in → artifact item (or artifact-fragment item)
- locator qualifier(s): page number, and/or region given as a
  percentage bounding box "x%,y%,w%,h%" (W1/W3) or a IIIF
  canvas+xywh URI (W2)
- optional short transcription excerpt supporting human verification

Machine-written statements add a second reference:
- inferred from extraction run → extraction-run item

Curator-attributed statements (a value correction or a curation flag
from the review CSV) carry the `stated in → artifact` reference only:
process provenance is for machine writes, and a human decision's
process is the MediaWiki edit history. The pipeline marks these with
the claim contract's `curated` flag.

The pipeline's claim JSON contract implementing this pattern is
`ingest/claims.py` (`ClaimBatch`): sources are required fields there,
ranks are unrepresentable, and property/item references use manifest
keys resolved to local ids only at write time.

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
  statement referenced to the artifact that mentions them.

## Artifact identity (see ADR-0010)

- An artifact's identity is its **content**, not its filename. Every
  artifact with media carries a `media SHA-1` statement (hex SHA-1 of
  the bytes), referenced `stated in → the artifact itself`. The
  registrar keys on this hash — identical bytes are one artifact item
  however they are named, and an alternate filename becomes an alias.
  Bit-identical media is never stored, itemised, or extracted twice.
- Labels stay unique per kind: when a title already names *different*
  content, the new artifact's label is disambiguated with a short
  content tag (`"<title> [<sha1[:8]>]"`) rather than colliding.
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
- Ranges and vagueness: earliest date / latest date qualifiers.
- "circa": sourcing circumstances qualifier.
- Redundant-but-asserted values (e.g. "age: 62" alongside a death date)
  are kept verbatim as qualifiers (`age at death`), never derived or
  dropped: sources assert them, so the archive records them.
- Ambiguous date formats (03-05-1950: 3 May or March 5?) must not be
  silently disambiguated; extraction should downgrade to the precision
  actually supported or leave the claim to a curator.
- Named periods: statements may point to time-period items rather than
  dates; period items link to PeriodO ids.
- Never narrow a source's vagueness to make data "cleaner".

## Occupations, causes, counts (trial-observed vocabulary)

- **occupation** (P106 semantics) is item-valued: occupations recur
  across people and documents, so they reconcile like places do. The
  occupation item's label keeps the source's exact wording ("Cigar
  maker", "Journeyman Baker"); cross-document variants unify through
  aliases and the ordinary equivalence review, never by normalizing
  the label.
- **cause of death** (P509 semantics) is monolingual text, kept
  verbatim in the source's language. Wikidata models causes as items;
  certificates and registers assert prose ("1. Chronic nephritis
  2. Cardiac failure"), and the archive records exactly that.
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
- Every statement on a derived item is referenced `stated in → the
  container` — a compound artifact asserts its own composition, the
  same convention as fragments and registrar statements above.
- Unpacking is deterministic (documented ZIP+XML), never generative;
  only claim extraction from the slide text uses an LLM.

## Faces and depiction (see ADR-0005)

- On a photograph item: depicts → person, with qualifiers:
  - relative position within image = "x%,y%,w%,h%"
  - (W2 alternative: annotation URI → W3C/IIIF annotation)
- Reference identifies the proposer (extraction/detection run) and
  confirmation is implied by the statement existing at all – unconfirmed
  proposals live outside the graph (review queue), never in it.
- W1 implementation: `python -m ingest detect-faces / enroll-face /
  match-faces` (see `ingest/faces.py`), feeding the shared review flow.

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

## Contradictions

- Competing values for the same property coexist as separate
  statements at **normal rank**, each with its own references.
- Rank changes are curator actions, logged by MediaWiki history, and
  optional forever.
- Disagreement *reporting* applies only to properties that can hold at
  most one true value per subject (dates/places of birth and death,
  parents, a place's identity properties). Properties that legitimately
  accumulate values over a life — spouse, residence — are multi-valued
  by nature: two spouses asserted by one source are remarriages, not a
  contradiction (their start/end qualifiers tell them apart).

## Living persons and privacy

- Property: "living person" flag (or inferred from absence of death
  data + birth recency); flagged entities are excluded from every
  public export, dump, or publish tier by default.
- The flag is set during claim review: the review CSV warns when a
  subject has no death data (in the batch or the graph) and activity
  within the 100-year cutoff, and its `living` column (yes/no) writes
  the flag as a curated claim referenced to the artifact whose review
  prompted it.
- The public export (`yizkr backup`) applies this conservatively:
  a person enters it only on positive evidence they are not living —
  a death record, birth beyond a 100-year cutoff, or an explicit
  curator `living person = no`. Unknown means excluded. Exclusion
  removes every mention (other people's claims about them, orphaned
  provenance nodes), and transcription excerpts — raw source
  quotations, which can name anyone — never enter the public tier.
