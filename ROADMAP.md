# Roadmap: moved to the GitHub Project

This file is retired. The plan lives in GitHub (ADR-0024): issues, epics with native sub-issues, milestones, blocked-by links, labels, and the Status, Size, Owner touchpoint and Iteration fields of the [yizkr Project](https://github.com/users/WillHirsch/projects/2). `docs/AGENT_PROTOCOL.md` §1 says how to cite work: issues by `#number`, decisions by ADR number, requirements by R-number.

Deferred work waits in the deferred-on-trigger register, #171. Each deferred item there names its source, and its trigger or the issue whose body states it.

## Old item numbers

This file once numbered its items, and commits, code and docs cited the numbers ("item 13", "W1 task 7"). The numbers are retired. The tables map each item to the ADR that records it and to the issues that carry what is left of it. Items 1 to 7, the first tasks of phase W1 (ADR-0004), were also cited as "W1 task 1" to "W1 task 7"; items 8 to 20 were the hardening items that followed the first trial ingestion. "Done" means the item was finished before the plan moved to GitHub.

| Item | Topic | Status and issues |
| --- | --- | --- |
| 1 | Wikibase Suite stack, runbook and smoke test | Done (`deploy/`, ADR-0001) |
| 2 | Model manifest and bootstrap | Done |
| 3 | Claim schema | Done (ADR-0003) |
| 4 | Ingestion pipeline skeleton | Done; the HTR platform adapter it stubbed is dropped (#171) |
| 5 | Face proposals, minimal | Done (ADR-0005), never run on a real photograph: #160; confirmer attribution and region export: #161 |
| 6 | SPARQL query library | Done |
| 7 | Backup and non-privileged export | Done (`yizkr backup`, ADR-0020); scheduled backups: #172; run-record retention: #100; the shared possibly-living module: #134 |
| 8 | Chunked extraction | Done (ADR-0006); a slimmed per-chunk schema: #90 |
| 9 | Cross-document entity reconciliation | Done (ADR-0007); corpus-level identity: epic #58 |
| 10 | Possibly-living policy at ingestion, and a vocabulary session | Done, reworked by ADR-0020; privacy under ratified runs: epic #60 |
| 11 | Corrections in the review sheet | Done; corrections as curator assertions: #139 |
| 12 | Re-ingestion of a trial document, and duplicate cleanup | Done; what it left for curation is moot, since every scratch run resets the instance (ADR-0025) |
| 13 | GEDCOM adapter | Done (ADR-0021); SOUR step B and OBJE media acquisition: #168 |
| 14 | Parallel chunks and batched adjudication | Done |
| 15 | Folder orchestration (`yizkr ingest run`) | Done; the scratch driver: #194; removing the per-document path: #119 |
| 16 | Second trial: a photograph, a handwritten certificate, a German-language document | Certificate leg done: LLM vision proved sufficient, so the HTR adapter is dropped (#171); photograph leg: #160; German-language leg: the whole-corpus scratch run, #145 |
| 17 | Compound artifacts: slide decks | Done (ADR-0008); other compound types and non-image media: #171 |
| 18 | Place hierarchy and coordinates | Hierarchy done (ADR-0009); as a scratch-run stage: #201; GeoNames and coordinates: #164 |
| 19 | Artifact identity and near-duplicates | Content-hash key done (ADR-0010); near-duplicates and versions: #162 |
| 20 | Typed, referenced names | Done (ADR-0011); name components and a referenced primary name: #171; frontend name factoring (ADR-0022): #140 |

The sections without item numbers map the same way:

| Section | Status and issues |
| --- | --- |
| Phase W2, the IIIF tier (ADR-0004) | #166 |
| Phase W3, the immich tier (ADR-0004) | #167 |
| Model extensions: georeferenced map artifacts, temporal and subject coverage | #171, with #164 and #166 |
| Frontend ladder rung 1, the query service's result views (ADR-0004) | Done |
| Frontend ladder rung 2, the static site (`yizkr publish-site`) | Done; provenance labels on values: #135; an internet-facing build from the non-privileged export: #163; map tiles: #164 |
| Frontend ladder rung 3, a plural-display app | #165 |
| Deferred by ADR: MCP or agentic graph access (ADR-0007) | #171 |
| Deferred by ADR: a slimmed per-chunk schema (ADR-0006) | #90 |
| Deferred by ADR: machine-written `said to be the same as` (ADR-0007) | Curator-only by design; only a parked experiment, #169, would try it |
| Deferred by ADR: guidance for collective manifest entities | Settled by ADR-0014 §3 (the `family` or `household` class) |
