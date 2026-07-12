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
- Schema-level statements (e.g. the subclass hierarchy) follow rule 1
  too: they carry a `stated in` reference to the *model manifest*
  item, which represents `model/manifest.yaml` — the document that
  asserts them.

## Core classes (items, via "instance of")

- human
- place
- time period (linked to PeriodO definitions where applicable)
- event (birth, marriage, migration, military service, ...)
- artifact (subclasses: photograph, certificate, letter, census page,
  newspaper clipping, ...)
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
  `instance of`. Rule 1 therefore holds for every registrar/writer path.
- Machine-proposed entities (people, places) get their `instance of`
  statement referenced to the artifact that mentions them.

## Uncertainty and time

- Use Wikibase time precision (year / month / day / decade / century).
- Ranges and vagueness: earliest date / latest date qualifiers.
- "circa": sourcing circumstances qualifier.
- Named periods: statements may point to time-period items rather than
  dates; period items link to PeriodO ids.
- Never narrow a source's vagueness to make data "cleaner".

## Places

- Reconcile to GeoNames and World Historical Gazetteer where possible;
  store external ids and coordinate location.
- Historical places that no longer exist are ordinary place items with
  their own time bounds.

## Faces and depiction (see ADR-0005)

- On a photograph item: depicts → person, with qualifiers:
  - relative position within image = "x%,y%,w%,h%"
  - (W2 alternative: annotation URI → W3C/IIIF annotation)
- Reference identifies the proposer (extraction/detection run) and
  confirmation is implied by the statement existing at all – unconfirmed
  proposals live outside the graph (review queue), never in it.
- W1 implementation: `python -m ingest detect-faces / enroll-face /
  match-faces` (see `ingest/faces.py`), feeding the shared review flow.

## Contradictions

- Competing values for the same property coexist as separate
  statements at **normal rank**, each with its own references.
- Rank changes are curator actions, logged by MediaWiki history, and
  optional forever.

## Living persons and privacy

- Property: "living person" flag (or inferred from absence of death
  data + birth recency); flagged entities are excluded from every
  public export, dump, or publish tier by default.
