# Family Records Knowledge Archive

A personal knowledge system for digitised historical family records:
an extensible knowledge graph of people, places, times, and artifacts,
where every unit of knowledge is an attributed assertion traceable to
the fragment of the source artifact that makes it – including when
sources contradict each other.

**Status:** Phase W1 in progress — infrastructure, model bootstrap,
claim schema, ingestion skeleton, face proposals, and query library
done; backup/export next.

Start here:
- `CLAUDE.md` – standing instructions and non-negotiable rules
- `docs/REQUIREMENTS.md` – the six requirements (acceptance criteria)
- `docs/adr/` – architecture decisions and their rationale
- `docs/MODELING.md` – Wikibase modeling conventions
- `ROADMAP.md` – phased build plan
- `deploy/` – Wikibase Suite deployment (Docker Compose + runbook)
- `model/` – schema manifest and idempotent bootstrap script
- `ingest/` – ingestion pipeline (register → transcribe → extract →
  review → write) and the claim JSON contract
- `queries/` – SPARQL query library, published to wiki pages

**Privacy:** this repository contains only infrastructure, tooling,
and documentation — no family records or personal data. Deployments
built from it, however, handle data about living people: nothing in a
deployment is published or sent to third-party services without
explicit instruction.
