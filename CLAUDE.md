# CLAUDE.md – standing instructions

## What this project is

A self-hosted knowledge system for historical family records. It preserves evidence and supports understanding, curation and exploration through a Wikibase graph and Python ingestion and presentation tools.

Read [REQUIREMENTS](docs/REQUIREMENTS.md) for user outcomes and [CONTRACT](docs/CONTRACT.md) for current policy. Use [MODELING](docs/MODELING.md) and the relevant architecture or operational guide for implemented details. Reading the entire ADR collection is not a prerequisite to a change. Approved requirements do not imply that all supporting capabilities are implemented.

## Evidence and human authority

- Preserve genuine source disagreement without requiring a canonical answer. Views must make disagreement visible and attributed detail accessible; honest summaries and filters are allowed.
- Historical assertions need source provenance and reliable locations; machine interpretations also need process provenance. Derived conclusions, curator attestations and schema/run metadata need provenance appropriate to their origins. Do not fabricate historical citations for bookkeeping or count machine alternatives as independent witnesses.
- Represent uncertainty explicitly without inventing precision. Separate source uncertainty from machine interpretation and identity uncertainty.
- Audited correction, retraction and supersession of machine errors are target capabilities. Preserve evidence and recoverable history. Existing retained-state correction and identity guards remain until supported replacements are verified; the contract is not permission for ad hoc deletion.
- Retained results require bounded, inspectable human approval. Identity decisions and face associations require affirmative confirmation of the explicit proposal. General run approval never silently confirms faces. Preserve approvals for unchanged results; new corroboration alone does not require reapproval.
- Keep archive data private by default. Publication and third-party disclosure require explicit authorization. A non-privileged export is not a public-safe archive; existing privacy controls and cutoffs remain.

## Engineering conventions

- Platform: self-hosted Wikibase Suite in `deploy/`, one uv-managed Python 3.11+ package in `src/yizkr`, the `yizkr` CLI, WikibaseIntegrator writes, Pydantic schemas and SPARQL reads. Existing choices do not require new generic frameworks or force all operational metadata into the graph. Keep one authority for accepted assertions.
- Idempotency: bootstrap and import operations must be safely repeatable. Persisted document products and human decisions must remain usable independently of model-response caching. See the contract for identity, snapshot and cache boundaries and their implementation gaps.
- Verification: use focused tests while iterating and broad offline CI for the reviewed revision. Select integration, model, corpus and mutation evidence according to the changed behavior and risk; no blanket full local suite, mutation-per-test or whole-corpus requirement. [TESTING](docs/TESTING.md) describes commands and current limitations. Use `needs:scratch-diff` with a reason and matching `stage:*` label when corpus evidence is needed.
- Lint/format: Ruff for Python and Prettier for Markdown (`make lint` / `make fmt`). CI runs lint and offline tests. `make hooks` installs formatters and main-branch protection.
- Markdown: keep paragraphs and list-item prose unwrapped; `.prettierrc.json` enforces `proseWrap: "never"`. Preserve meaningful code-block breaks and use explicit Markdown hard breaks where needed. Substantial Markdown belongs in `.md` files. Prompt templates in `src/yizkr/pipeline/prompts/` use `str.format` placeholders and doubled literal braces; editing their exact text changes model inputs and cache keys.
- Runtime outputs belong in gitignored `var/` or outside the repository: response caches, document views, reports, diffs, batches, review CSVs and galleries. They may contain private data.
- Documentation moves with behavior. Keep current goals, supported behavior, limitations and necessary rationale; update, consolidate or remove guidance rather than accumulating ADRs. Development history belongs in Git/GitHub. Deprecated text remaining temporarily needs a nearby open removal issue and concrete removal condition. The contract defines this policy.
- Every example in code, comments, prompts, tests, docs, commits, issues and PRs comes from the fictional universe in [EXAMPLES](docs/EXAMPLES.md) and `tests/fixtures/example_universe.py`, never real or partially anonymized family data.
- Comments and docstrings in `src/yizkr/`, including prompts, explain current behavior and rationale. Keep issue/PR numbers, retired roadmap identifiers and accounts of how changes were developed in GitHub; relevant current design references are fine. Tests and documentation may link issues for outstanding work.

## Agent execution and data boundary

Use issue → branch → self-check → draft PR → review → ready → owner merge. [AGENT_PROTOCOL](docs/AGENT_PROTOCOL.md) defines execution labels, the cloud gate, permissions and review. Never push to main or merge an agent PR. Do not change standing instructions without owner authorization.

- **Local-only data:** corpus inputs (`import/` or `YIZKR_CORPUS_DIR`), private `var/`, `bundle/`, `~/yizkr-private/`, backups, exclusions, audit inventories and corpus-derived live or development graphs stay on the local machine. Work needing them is `exec:local-agent`. Only aggregate reports and diff summaries may leave, after checking that they disclose no corpus content.
- **Fictional integration exception:** an isolated test deployment containing only repository-fictional data may run in CI/cloud under the existing dispatch gate, using separate settings, volumes, state and locks with no archive credentials, mounts or implicit archive defaults. The required repeatable lane is not yet delivered ([#722](https://github.com/WillHirsch/yizkr/issues/722)). This exception does not apply to a corpus-derived graph.
- Never quote, paraphrase or identify private corpus content in the repository, GitHub or cloud sessions. GitHub references to real people use QIDs only; artifact references follow the non-identifying forms in AGENT_PROTOCOL §7, and metrics are aggregate only.
- A session handling real data reviews every outgoing commit range and posted text for disclosure before sending it.

## Disposable and retained operation

The implemented development path is whole-corpus scratch execution against a designated disposable instance. A scratch run writes unattended, unratified proposals, excludes faces/depicts and never implicitly authorizes retention or disclosure. Fix pipeline problems and regenerate disposable output rather than patching the scratch graph.

Rebuilds and retained-database ingestion are enduring workflows. Retained additions must preserve identities and human work, and destructive reset must refuse a retained target. These requirements are not a claim that the incremental or correction workflows are complete. [CONTRACT](docs/CONTRACT.md) links the implementation tasks.

The owner authorizes the keeper transition in [#159](https://github.com/WillHirsch/yizkr/issues/159) based on safe supported workflows, review, recovery, privacy and backups, not a fixed number of unchanged extraction runs. The isolated integration lane must be runnable before the shared instance becomes retained. Until then, live checks use the designated disposable instance and current locking; unavailable evidence blocks affected persistence work, not unrelated changes.
