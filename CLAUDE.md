# CLAUDE.md – standing instructions

## What this project is

A self-hosted knowledge system for digitised historical family records. Core store: **self-hosted Wikibase Suite**. The system ingests artifacts (photographs, certificates, letters, census pages...), extracts assertions via an LLM pipeline with human review, links face regions in photographs to person entities, and presents everything through interactive timelines, maps, and galleries.

Read `docs/REQUIREMENTS.md` and the ADRs in `docs/adr/` before making architectural choices. `docs/MODELING.md` defines the data conventions.

## The core principle: neutral coexistence (source-first)

This database records **what sources assert**, never bare truth. Multiple contradictory assertions about the same property are a _healthy resting state_, not an error, a conflict to resolve, or a "pending" condition. Resolution (preferring one assertion) is an optional overlay that may never happen. See ADR-0002.

Acceptance test for any feature: _a property can hold zero canonical values and N source-attributed values, and everything still works._

## Non-negotiable rules

1. **Never write a statement without a reference.** Every statement carries a reference to the artifact (and fragment locator) that asserts it. See ADR-0003.
2. **Pipeline writes carry dual provenance.** Machine-written statements also reference an extraction-run item (model, date, pipeline version).
3. **Never overwrite or delete a source-attributed statement to "correct" it.** Corrections and preferences are _new_ assertions or curator rank changes – the original attributed claim remains.
4. **Ranks default to normal.** Preferred/deprecated ranks are optional curation. No feature may _require_ a preferred value to function.
5. **Uncertainty is structural, never silent.** Use date precision, earliest/latest qualifiers, and sourcing-circumstances qualifiers. Never round, guess, or narrow what a source says.
6. **Frontends are plural-by-default.** Display all attributed values with source labels (e.g. "born: 7 January 1919 per the birth register extract; 1 July 1919 per the naturalisation certificate"). Never silently pick one value.
7. **Privacy first.** Data includes living people. Nothing public by default; no personal data to third-party services without explicit instruction; self-hosted components only unless told otherwise.

## Engineering conventions

- Infrastructure: Docker Compose (Wikibase Suite and companions) in `deploy/`, which intentionally tracks upstream Wikibase Suite Deploy file-for-file.
- Python: one uv-managed package (`src/yizkr`, Python 3.11+), single `yizkr` CLI entry point, `WikibaseIntegrator` for writes, Pydantic for schemas, dependencies in `pyproject.toml` (+ `uv.lock`; heavy optional engines are extras: `llm`, `faces`).
- Tests: offline unit tests in `tests/unit` run by default; live integration tests in `tests/integration` (marked `live`) run against the deployed stack via `make test-live`. Substantial behavior changes to write paths get a live test, not just a session's manual check. Live tests run on the one instance, which scratch runs reset. `docs/TESTING.md` covers the test layers, targeted and full runs, parallel runs, the mutation harness, the query-engine sandbox and the live-stack lock.
- Scratch-diff evidence: a change to a stage before write (its code, a prompt, the manifest, a model or its parameters) is checked by a whole-corpus scratch run: its aggregate report and its diff against the latest run on main (`needs:scratch-diff`, plus the matching `stage:*` label; ADR-0025, ADR-0032). There is no gold set or synthetic eval.
- Lint/format: Ruff for Python and Prettier for Markdown (`make lint` / `make fmt`); CI runs lint + unit tests on every push to a pull request and on every commit to main. `make hooks` installs both formatters as pre-commit hooks.
- Markdown: never hard-wrap prose paragraphs or list-item prose. Let the reader's editor or viewer wrap the display. Prettier enforces `proseWrap: "never"` in `.prettierrc.json`; code blocks retain their meaningful line breaks. Where a line break within a paragraph is meaningful, use an explicit Markdown hard break (two spaces at the end of the line), which Prettier preserves. Keep substantial Markdown content in `.md` files, including prompt templates, rather than string literals in Python. Prompt templates live in `src/yizkr/pipeline/prompts/` and use Python `str.format` placeholders where interpolation is needed; literal braces in formatted templates are doubled. Reflowing a prompt changes its exact text and therefore its LLM cache key (ADR-0032).
- Reads: SPARQL against the bundled query service.
- Bulk human review: CSV artifacts consumable by OpenRefine.
- Idempotency: all bootstrap/import scripts must be safely re-runnable.
- Pipeline outputs (the LLM cache, scratch-run directories and their document views, reports and diffs, batches, review CSVs, galleries) are runtime state: keep them in `var/` (gitignored) or outside the repo. A document's views persist and are reused while their inputs are unchanged; the LLM cache keys each model response by what its call sends (prompt text, files by content hash, engine, model, effort and configuration), never by mtimes or timestamps, and no prompt names an artifact by its QID, only by the SHA-1 of its media, so a reset changes no key; stages before write never read the wiki (ADR-0025 §2, ADR-0032).
- Docs move with code: update ADRs/MODELING.md in the same PR as any behavior change; new architectural decisions get a new ADR.
- Examples: every example in code, comments, prompts, tests, docs, commits, issues and PRs comes only from the fictional universe (`docs/EXAMPLES.md`, `tests/fixtures/example_universe.py`). Never real family data, not even partially anonymised.
- Code comments: comments and docstrings in `src/yizkr/` (prompt templates included) describe how the code works now and why, never its development history: no issue or PR numbers, ROADMAP items, "previously"/"was"/"no longer" notes, or accounts of how a bug was found or which run it broke. That history belongs in commit messages, PRs, issues and ADRs. ADR references are fine, as they give the current design's rationale. Tests and development docs (READMEs, `docs/`, ADRs) may cite issues.

## Agent execution & data boundary

Every change goes issue → branch → self-check → draft PR → review → ready → owner merge; see `docs/AGENT_PROTOCOL.md` (§4 has the pre-review self-check) and ADR-0024.

- **Local-only data** is read only by local sessions and never copied into the repository, GitHub or a cloud agent's session: the corpus (`import/`, or `YIZKR_CORPUS_DIR` once moved out of the tree), `var/` (the LLM cache, run directories with their document views, live status pages, `report.html`, `report.json` and detailed diffs included), `bundle/`, `~/yizkr-private/` and its archives, backups, the exclusion list, the private audit inventory, and any live or dev graph. Only a run's aggregate report and diff summary may leave the machine. Work that touches local-only data is `exec:local-agent`; cloud agents are code-only.
- **Real data stays in the private area.** Never quote, paraphrase or identify corpus content anywhere else. GitHub text cites people by QID only and gives metrics as aggregates only.
- **Self-review before anything leaves the machine.** A session that has handled real data re-reads every commit range it pushes and every issue, PR and comment text it posts, and confirms none of it quotes, paraphrases or identifies anything from the corpus.
- **Never push to main.** `make hooks` installs a pre-push hook that refuses it. Agent PRs open as drafts and are marked ready only after review passes and its fixes land; the owner merges.

## Development stage: whole-corpus scratch runs

Until the owner decides the next run is the keeper run (#159), the pipeline is developed by whole-corpus scratch runs (ADR-0025).

- A scratch run is unattended end to end, with no cluster review and no ratification. It writes to the one instance, which one command resets and repopulates per run. Rules 1, 2 and 4 hold for every write.
- Scratch graphs are development artefacts, never confirmations: never kept, published or exported. Faces and depicts are never part of a scratch run.
- Problems found in run reports, diffs and views are fixed in the pipeline and regenerated, never repaired in the graph.
- From the keeper run on, cluster review and ratification apply, rule 3 applies in full, and the reset command refuses on the kept instance.

## What NOT to do

- Do not introduce a second store for assertions (the graph is the system of record; other services may hold _media_ or _proposals_).
- Do not build anything that assumes a single canonical value per property.
- Do not auto-confirm machine proposals (extraction or faces) in the kept state; human confirmation is part of the data flow. The one exception is the development stage: unattended scratch writes to a reset, never-kept instance (ADR-0025). Faces stay human-gated everywhere: nothing writes a depicts statement without a human confirming it (ADR-0005).
