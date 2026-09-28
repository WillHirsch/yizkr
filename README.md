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
- `docs/AGENT_PROTOCOL.md` – how work moves from an issue to main, and
  how real data stays out of the repository and GitHub
- `docs/REQUIREMENTS.md` – the six requirements (acceptance criteria)
- `docs/adr/` – architecture decisions and their rationale; ADR-0025
  says how the pipeline is developed: whole-corpus scratch runs, reuse of
  unchanged work, and run reports and diffs in place of a synthetic eval;
  ADR-0032 says how work is reused: persisted views, and a cache of model
  responses
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
make hooks               # once per clone: ruff on commit, no pushes to main
uv run yizkr --help      # bootstrap / smoke-test / publish-queries / ingest
make test                # offline tests
make up bootstrap smoke  # start the stack, apply the model, verify
make test-live           # integration tests against the running stack
```

[uv]: https://docs.astral.sh/uv/

Pipeline outputs (the LLM cache, scratch-run directories, run reports
and diffs, claim batches, review CSVs, face galleries) are runtime state,
not code — keep them under `var/` (gitignored) or outside the repo. They
hold corpus content, so they stay on the machine; only a run's aggregate
report and diff summary may be posted (`docs/AGENT_PROTOCOL.md` §6).

Changes to a stage before write (a prompt, the manifest, a model, or a
stage's code) are checked by a whole-corpus scratch run and its diff
against the latest run on main (`needs:scratch-diff`, ADR-0025), not by a
gold set or eval gate.

**Privacy:** deployments built from this repository handle data about
living people: nothing in a deployment is published or sent to
third-party services without explicit instruction. The repository itself
keeps real family data out by convention: every example comes from the
fictional universe in `docs/EXAMPLES.md`, and sessions that handle real
data self-review everything they push or post (ADR-0024,
`docs/AGENT_PROTOCOL.md`). Its commit history predates that rule and
still holds some real data, so the repository stays private unless that
history is rewritten (#171).
