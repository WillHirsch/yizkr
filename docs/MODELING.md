# Wikibase modeling conventions

## General approach

- The schema is self-describing: properties and classes are minted as
  Wikibase properties/items in-instance.
- Mirror Wikidata property *semantics* where a good equivalent exists
  (instance of, date of birth, spouse, residence, depicts, coordinate
  location, start/end time, stated in...), using local P-ids. Maintain
  a mapping table (local id – Wikidata id) as a wiki page and a YAML
  manifest in this repo (`model/manifest.yaml`, created in W1 task 2).
- Every entity gets a label and a short description; name variants go
  in aliases.

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

## Contradictions

- Competing values for the same property coexist as separate
  statements at **normal rank**, each with its own references.
- Rank changes are curator actions, logged by MediaWiki history, and
  optional forever.

## Living persons and privacy

- Property: "living person" flag (or inferred from absence of death
  data + birth recency); flagged entities are excluded from every
  public export, dump, or publish tier by default.
