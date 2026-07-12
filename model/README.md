# Model bootstrap

`manifest.yaml` declares the core properties, classes, and vocabulary
items from `docs/MODELING.md`. `bootstrap.py` mints them in the Wikibase
instance and records the local id ↔ Wikidata id mapping on the wiki page
named in the manifest (`Project:Model mapping`).

Idempotent: entities are matched by exact English label, so re-running
is safe and a second run against an up-to-date instance is a no-op.
Local P/Q ids are assigned by the instance and never stored in this
repo — the wiki mapping page is the per-instance record.

## Usage

Requires a running instance (`deploy/README.md`) and its `deploy/.env`
for connection settings.

```sh
cd model
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bootstrap.py --dry-run   # show the plan
.venv/bin/python bootstrap.py            # apply
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

## Tests

Offline (no instance needed):

```sh
.venv/bin/python -m pytest
```
