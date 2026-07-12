# Ingestion pipeline

W1 task 3 delivered `claims.py`, the claim JSON schema. The pipeline
steps around it (registrar, transcription, extraction, review export,
writer) arrive in W1 task 4.

## The claim contract (`ingest/claims.py`)

Every pipeline step exchanges `ClaimBatch` JSON: the LLM extractor
emits it, entity resolution rewrites references in it, the review
export renders it to CSV, and the writer turns it into Wikibase
statements per ADR-0003.

The non-negotiable rules are structural in the schema:

- `source` (artifact + fragment locators) is a **required** field —
  a referenceless claim cannot be represented (rule 1).
- Batches carry `extraction_run` metadata for the second, process
  reference (rule 2).
- There is **no rank field** — pipeline writes are always normal rank;
  preferring is a curator act on the wiki (rule 4).
- Time values carry explicit precision and calendar; ranges and
  "circa" are qualifiers, and inconsistent precision (a day-precise
  date labeled month-precise) is rejected rather than silently
  narrowed or padded (rule 5).

Properties and vocabulary items are referenced by **manifest key**
(`spouse`, `circa` — see `model/manifest.yaml`), never by local P/Q id:
ids are per-instance and only the writer resolves them, at write time.
`validate_against_manifest()` checks every key and datatype in a batch
against the manifest and reports all problems at once.

Example batch:

```json
{
  "schema_version": 1,
  "extraction_run": {
    "model_identifier": "example-vision-model-2026-01",
    "pipeline_version": "0.1.0",
    "software_commit": "478f9ea",
    "started_at": "2026-07-12T09:00:00Z"
  },
  "claims": [
    {
      "subject": {"kind": "local", "local_id": "person-1",
                  "label": "Person One", "instance_of": "human"},
      "property": "spouse",
      "value": {"type": "item",
                "value": {"kind": "local", "local_id": "person-2",
                          "label": "Person Two"}},
      "qualifiers": [
        {"property": "point_in_time",
         "value": {"type": "time", "year": 1933}},
        {"property": "sourcing_circumstances",
         "value": {"type": "item",
                   "value": {"kind": "vocab", "key": "circa"}}}
      ],
      "source": {
        "artifact": {"kind": "qid", "qid": "Q23"},
        "page_number": "1",
        "region": {"x": 10, "y": 42.5, "width": 60, "height": 8},
        "transcription_excerpt": {"text": "married 1933", "language": "en"}
      },
      "confidence": 0.85
    }
  ]
}
```

## CLI

```sh
python3 claims.py --json-schema           # JSON Schema, for LLM extraction prompts
python3 claims.py --validate batch.json   # schema + manifest check a file
```

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest      # from the repo root or ingest/
```
