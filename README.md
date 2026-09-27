# Family Records Knowledge Archive

A personal knowledge system for digitised historical family records:
an extensible knowledge graph of people, places, times, and artifacts,
where every unit of knowledge is an attributed assertion traceable to
the fragment of the source artifact that makes it – including when
sources contradict each other.

**Status:** Phase W1 in progress — infrastructure, model bootstrap,
claim schema, ingestion pipeline (chunked extraction, GEDCOM adapter,
reconciliation, review with corrections, folder orchestration via
`yizkr ingest run`), face proposals, query library, and backup/export
done; second ingestion trial (photos/HTR/German) next.

Start here:
- `CLAUDE.md` – standing instructions for AI-assisted development
- `docs/REQUIREMENTS.md` – the six requirements (acceptance criteria)
- `docs/adr/` – architecture decisions and their rationale
- `docs/MODELING.md` – Wikibase modeling conventions
- `docs/EXAMPLES.md` – the one fictional example universe every example
  comes from
- `src/yizkr/pipeline/README.md` – **how ingestion works end to end**: the
  `yizkr ingest run` loop and its two human review gates
- `ROADMAP.md` – phased build plan
- `deploy/` – Wikibase Suite deployment (Docker Compose + runbook)
- `src/yizkr/` – the tooling package: model manifest + bootstrap
  (`model/`), the claim JSON contract (`claims.py`), the ingestion
  pipeline (`pipeline/`), and the SPARQL query library (`queries/`)
- `tests/` – offline unit tests plus live integration tests
  (`pytest -m live`, needs the running stack)

## Working on the tooling

Python tooling is one [uv]-managed package with a single CLI:

```sh
uv sync --dev            # or: make install
uv run yizkr --help      # bootstrap / smoke-test / publish-queries / ingest
make test                # offline tests
make up bootstrap smoke  # start the stack, apply the model, verify
make test-live           # integration tests against the running stack
```

[uv]: https://docs.astral.sh/uv/

Pipeline outputs (claim batches, review CSVs, face galleries) are
runtime state, not code — keep them under `var/` (gitignored) or
outside the repo.

**Privacy:** this repository contains only infrastructure, tooling,
and documentation — no family records or personal data. Deployments
built from it, however, handle data about living people: nothing in a
deployment is published or sent to third-party services without
explicit instruction.
