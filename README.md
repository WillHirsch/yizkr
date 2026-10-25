# Family Records Knowledge Archive

A personal knowledge system for digitised historical family records: an extensible knowledge graph of people, places, times, and artifacts, where every unit of knowledge is an attributed assertion traceable to the fragment of the source artifact that makes it – including when sources contradict each other.

**Status:** development by whole-corpus scratch runs (ADR-0025): each run resets the one instance, runs the pipeline over the whole corpus unattended and is judged by its report and its diff against the latest completed run on main, until the owner declares a keeper run. The plan lives in GitHub issues and the Project (ADR-0024).

Start here:

- `CLAUDE.md` – standing instructions for AI-assisted development
- `docs/AGENT_PROTOCOL.md` – how work moves from an issue to main, and how real data stays out of the repository and GitHub
- `docs/TESTING.md` – the test layers, which test runs to use when, and the shared tools: the mutation harness, the query-engine sandbox and the live-stack lock
- `docs/REQUIREMENTS.md` – the six requirements (acceptance criteria)
- [Architecture and workflow diagrams](docs/ARCHITECTURE.md) – Mermaid overviews of the implemented services, provenance, ingestion, scratch runs, cache, face review and exploration, with subsystem details
- [Scratch-run stages and data products](docs/SCRATCH_WORKFLOW.md) – detailed input/output contracts and process types for every stage
- `docs/adr/` – architecture decisions and their rationale; ADR-0025 says how the pipeline is developed: whole-corpus scratch runs, reuse of unchanged work, and run reports and diffs in place of a synthetic eval; ADR-0032 says how work is reused: persisted views, and a cache of model responses
- `docs/MODELING.md` – Wikibase modeling conventions
- `docs/EXAMPLES.md` – the one fictional example universe every example comes from
- `src/yizkr/pipeline/README.md` – **how ingestion works end to end**: the `yizkr ingest run` loop and its two human review gates
- `ROADMAP.md` – retired: the plan lives in GitHub issues and the Project (ADR-0024), and the stub maps the old item numbers to issues
- `deploy/` – Wikibase Suite deployment (Docker Compose + runbook)
- `docs/runbooks/stack-recovery.md` – recovering a stopped or crashed stack: volume archives first, then the database, the full stack, the derived indexes and a smoke test, and a backup only where the stage or a decision asks for one
- `src/yizkr/` – the tooling package: model manifest + bootstrap (`model/`), the claim JSON contract (`claims.py`), the ingestion pipeline (`pipeline/`), and the SPARQL query library (`queries/`)
- `tests/` – offline unit tests plus live integration tests (`pytest -m live`, needs the running stack)

## Working on the tooling

Python tooling is one [uv]-managed package with a single CLI:

```sh
uv sync --dev            # or: make install
make hooks               # once per clone: Ruff + Prettier on commit; protect main
uv run yizkr --help      # bootstrap / smoke-test / publish-queries / ingest
make test                # offline tests
make up bootstrap smoke  # start the stack, apply the model, verify
make test-live           # integration tests against the running stack
```

[uv]: https://docs.astral.sh/uv/

`make fmt` formats Python with Ruff and tracked Markdown with Prettier; `make lint` checks both without changing files. The pre-commit hooks use the same versions and configuration as CI. Pre-commit manages Prettier's Node environment and installs the pinned formatter on its first run.

Keep Markdown prose unwrapped: each paragraph and each list-item paragraph stays on one source line, with visual wrapping left to the editor or viewer. `.prettierrc.json` enforces `proseWrap: "never"`. Preserve meaningful line breaks in code blocks. Substantial Markdown belongs in `.md` files rather than Python strings; interpolated prompts are Markdown format templates in `src/yizkr/pipeline/prompts/`.

To format or check Markdown alone:

```sh
uv run pre-commit run prettier --all-files
uv run pre-commit run prettier-check --all-files --hook-stage manual
```

A formatter hook reports failure when it changes a file, so review and stage the formatted file before retrying the commit. `--all-files` covers tracked files; to format a new file before staging it, use `--files path/to/file.md` instead.

Pipeline outputs (the LLM cache, scratch-run directories, run reports and diffs, claim batches, review CSVs, face galleries) are runtime state, not code — keep them under `var/` (gitignored) or outside the repo. They hold corpus content, so they stay on the machine; only a run's aggregate report and diff summary may be posted (`docs/AGENT_PROTOCOL.md` §6).

Changes to a stage before write (a prompt, the manifest, a model, or a stage's code) are checked by a whole-corpus scratch run and its diff against the latest run on main (`needs:scratch-diff`, ADR-0025), not by a gold set or eval gate.

**Privacy:** deployments built from this repository handle data about living people: nothing in a deployment is published or sent to third-party services without explicit instruction. The repository itself keeps real family data out by convention: every example comes from the fictional universe in `docs/EXAMPLES.md`, and sessions that handle real data self-review everything they push or post (ADR-0024, `docs/AGENT_PROTOCOL.md`). Its commit history predates that rule and still holds some real data, so the repository stays private unless that history is rewritten (#171).
