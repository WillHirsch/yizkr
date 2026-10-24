# Model bootstrap

`manifest.yaml` declares the core properties, classes, and vocabulary
items from `docs/MODELING.md`. `yizkr bootstrap` mints them in the
Wikibase instance and records the local id ↔ Wikidata id mapping on
the wiki page named in the manifest (`Project:Model mapping`).

Idempotent: entities are matched by exact English label, so re-running
is safe and a second run against an up-to-date instance is a no-op.
Local P/Q ids are assigned by the instance and never stored in this
repo — the wiki mapping page is the per-instance record. The wiki
client reads the vocabulary from it (`WikiClient.vocabulary`), checking
that each id it names carries its manifest label, and reads every label
whole when the page is missing or disagrees.

## Usage

Requires a running instance (`deploy/README.md`) and its `deploy/.env`
for connection settings. From the repo root:

```sh
uv run yizkr bootstrap --dry-run   # show the plan
uv run yizkr bootstrap             # apply (or: make bootstrap)
```

## Extending the model

Add entries to `manifest.yaml` (at the end of their section, so id
numbering stays tidy) and re-run. Update `docs/MODELING.md` in the same
PR. Rules:

- Labels are the idempotency key: never re-use one, and treat renames
  as what they are — a rename must be done on the wiki *and* in the
  manifest together, otherwise the bootstrap will mint a duplicate.
- A property's datatype can never change (Wikibase limitation); the
  bootstrap aborts if the manifest disagrees with the instance. Mint a
  new property instead.
- Map to a Wikidata equivalent (`wikidata:` field) only when the
  semantics genuinely match; verify the id against wikidata.org.
- A class is marked: an item in the subclass hierarchy (one with
  `subclass_of`, or another's superclass) is `entity_kind: true` or
  `entity_class: true`, or the manifest does not load. Mark any other
  class of entities `entity_class` too (a time period, an infrastructure
  class): validation reads the mark to refuse the class named as a value
  (docs/MODELING.md, "The relation decides the kind").

## Tests

`make test` covers manifest validation offline; `make test-live`
verifies bootstrap idempotency against the running stack.
