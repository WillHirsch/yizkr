# CLAUDE.md – standing instructions

## What this project is

A self-hosted knowledge system for digitised historical family records.
Core store: **self-hosted Wikibase Suite**. The system ingests artifacts
(photographs, certificates, letters, census pages...), extracts
assertions via an LLM pipeline with human review, links face regions in
photographs to person entities, and presents everything through
interactive timelines, maps, and galleries.

Read `docs/REQUIREMENTS.md` and the ADRs in `docs/adr/` before making
architectural choices. `docs/MODELING.md` defines the data conventions.

## The core principle: neutral coexistence (source-first)

This database records **what sources assert**, never bare truth.
Multiple contradictory assertions about the same property are a
*healthy resting state*, not an error, a conflict to resolve, or a
"pending" condition. Resolution (preferring one assertion) is an
optional overlay that may never happen. See ADR-0002.

Acceptance test for any feature: *a property can hold zero canonical
values and N source-attributed values, and everything still works.*

## Non-negotiable rules

1. **Never write a statement without a reference.** Every statement
   carries a reference to the artifact (and fragment locator) that
   asserts it. See ADR-0003.
2. **Pipeline writes carry dual provenance.** Machine-written
   statements also reference an extraction-run item (model, date,
   pipeline version).
3. **Never overwrite or delete a source-attributed statement to
   "correct" it.** Corrections and preferences are *new* assertions or
   curator rank changes – the original attributed claim remains.
4. **Ranks default to normal.** Preferred/deprecated ranks are optional
   curation. No feature may *require* a preferred value to function.
5. **Uncertainty is structural, never silent.** Use date precision,
   earliest/latest qualifiers, and sourcing-circumstances qualifiers.
   Never round, guess, or narrow what a source says.
6. **Frontends are plural-by-default.** Display all attributed values
   with source labels (e.g. "moved to France: 1929 per source A;
   1930–1935 per source B"). Never silently pick one value.
7. **Privacy first.** Data includes living people. Nothing public by
   default; no personal data to third-party services without explicit
   instruction; self-hosted components only unless told otherwise.

## Engineering conventions

- Infrastructure: Docker Compose (Wikibase Suite and companions) in
  `deploy/`, which intentionally tracks upstream Wikibase Suite Deploy
  file-for-file.
- Python: one uv-managed package (`src/yizkr`, Python 3.11+), single
  `yizkr` CLI entry point, `WikibaseIntegrator` for writes, Pydantic
  for schemas, dependencies in `pyproject.toml` (+ `uv.lock`; heavy
  optional engines are extras: `llm`, `faces`).
- Tests: offline unit tests in `tests/unit` run by default; live
  integration tests in `tests/integration` (marked `live`) run against
  the deployed stack via `make test-live`. Substantial behavior changes
  to write paths get a live test, not just a session's manual check.
- Lint/format: ruff (`make lint` / `make fmt`); CI runs lint + unit
  tests on every push.
- Reads: SPARQL against the bundled query service.
- Bulk human review: CSV artifacts consumable by OpenRefine.
- Idempotency: all bootstrap/import scripts must be safely re-runnable.
- Pipeline outputs (batches, review CSVs, galleries) are runtime state:
  keep them in `var/` (gitignored) or outside the repo.
- Docs move with code: update ADRs/MODELING.md in the same PR as any
  behavior change; new architectural decisions get a new ADR.
- Examples: every example in code, comments, prompts, tests, docs,
  commits, issues and PRs comes only from the fictional universe
  (`docs/EXAMPLES.md`, `tests/fixtures/example_universe.py`). Never
  real family data, not even partially anonymised.
- Code comments: comments and docstrings in `src/yizkr/` (prompt
  templates included) describe how the code works now and why, never
  its development history: no issue or PR numbers, ROADMAP items,
  "previously"/"was"/"no longer" notes, or accounts of how a bug was
  found or which run it broke. That history belongs in commit messages,
  PRs, issues and ADRs. ADR references are fine, as they give the
  current design's rationale. Tests and development docs (READMEs,
  `docs/`, ADRs) may cite issues.

## Agent execution & data boundary

Every change goes issue → branch → draft PR → review → ready → owner
merge; see `docs/AGENT_PROTOCOL.md` and ADR-0024.

- **Local-only data** is read only by local sessions and never copied
  into the repository, GitHub or a cloud agent's session: the corpus
  (`import/`, or `YIZKR_CORPUS_DIR` once moved out of the tree), `var/`,
  `bundle/`, `~/yizkr-private/` and its archives, the gold set, backups,
  the exclusion list, the private audit inventory, and any live or dev
  graph. Work that touches them is `exec:local-agent`; cloud agents are
  code-only.
- **Real data stays in the private area.** Never quote, paraphrase or
  identify corpus content anywhere else. GitHub text cites people by
  QID only and gives metrics as aggregates only.
- **Self-review before anything leaves the machine.** A session that
  has handled real data re-reads every commit range it pushes and every
  issue, PR and comment text it posts, and confirms none of it quotes,
  paraphrases or identifies anything from the corpus.
- **Never push to main.** `make hooks` installs a pre-push hook that
  refuses it. Agent PRs open as drafts and are marked ready only after
  review passes and its fixes land; the owner merges.

## What NOT to do

- Do not introduce a second store for assertions (the graph is the
  system of record; other services may hold *media* or *proposals*).
- Do not build anything that assumes a single canonical value per
  property.
- Do not auto-confirm machine proposals (extraction or faces); human
  confirmation is part of the data flow.
