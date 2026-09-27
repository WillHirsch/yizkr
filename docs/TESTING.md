# Testing

How the tests are layered, which runs to use while working and before a
push, and the shared tools for proving that a test guards something and
for trying queries without the live stack. Who runs what, and when, is in
[docs/AGENT_PROTOCOL.md](AGENT_PROTOCOL.md): waves (§3), the pre-review
self-check and reviews (§4), and live tests (§5). Each tool documents its
options in its own `--help`.

## Test layers

- **Unit tests** (`tests/unit`) are offline: no stack, no network, no
  model. They are the default run, `uv run pytest -q` or `make test`,
  because `pyproject.toml` deselects the `live` marker unless `-m live`
  asks for it. CI runs them, with ruff, on every push. An autouse fixture
  points `CLAUDE_CONFIG_DIR` at a directory that does not exist, so no
  unit test reads your Claude Code login.
- **Live tests** (`tests/integration`, every module marked `live`) run
  against the deployed stack (`make up` or `make up-local`) through
  `make test-live`, which is `uv run pytest -m live tests/integration -v`.
  They skip, saying why, when there is no connection configuration
  (`deploy/.env`) or the stack is unreachable. They write fictional
  fixture data under deterministic labels, so a re-run reuses its
  entities, and that reuse is part of what they check. They run on the
  one instance that scratch runs reset, one session at a time behind the
  live-stack lock (below); AGENT_PROTOCOL §5 says who runs them and how
  the session ends. A PR that changes a write path carries
  `needs:live-test`, and a substantial change to one gets a live test of
  its own (CLAUDE.md).
- **The real-model tests** (`tests/integration/test_live_chunked.py`,
  also marked `live`) call the model through the claude CLI: chunked
  extraction end to end, and the exact resumption of a conversation the
  cache kept. They go through the LLM cache at `$YIZKR_LIVE_CACHE`
  (default `var/live-cache`), so a first run makes a few subscription
  calls and later runs are answered from the cache. They need the claude
  CLI, logged in, and skip when it is not installed; they don't use the
  Wikibase stack. `make test-live` runs them with the others; on their
  own they are
  `uv run pytest -m live tests/integration/test_live_chunked.py -s`, where
  `-s` shows their cache events.

A change to a stage before write is checked by a whole-corpus scratch run
and its diff, not by a test: there is no gold set or synthetic eval
(ADR-0025, AGENT_PROTOCOL §5).

## Targeted and full runs

While iterating, run only the tests that cover the change:

```sh
uv run pytest -q tests/unit/test_names.py            # one file
uv run pytest -q tests/unit/test_names.py::<name>    # one test
uv run pytest -q tests/unit -k <expression>          # tests by name
uv run pytest -q --lf                                # the last failures
```

The full suite, `uv run pytest -q`, runs once, before the final push
(AGENT_PROTOCOL §4). It takes under a minute on an idle machine and far
longer under load (#252), so full runs repeated by many agents slow every
one of them. CI runs it on every push, and a reviewer reads CI's result
for the commit under review instead of running it again. Ruff is quick:
`uv run ruff check .` and `uv run ruff format --check .` pass before every
push, and the pre-commit hook (`make hooks`) runs ruff on every commit.

## Parallel runs (pytest-xdist)

pytest-xdist is a dev dependency (#252). CI runs the unit suite with it,
each test file in one worker. Locally it is opt-in, for when the machine
is idle:

```sh
uv run pytest -q -n auto --dist loadfile
```

`-n auto` starts a worker per CPU core, and `--dist loadfile` keeps each
test file in one worker, as CI does. While other agents are testing, the
extra workers slow everyone's runs, so run serially then (AGENT_PROTOCOL
§3, Waves). It is for unit tests only: live tests share the one instance.
A test that fails only in parallel is not parallel-safe, which is a bug:
report it.

## The mutation harness

A test earns its place by failing when the behaviour it guards breaks.
`scripts/mutate.py` (#250) shows that it does. A spec file lists
mutations, each with a file, the exact old text, the new text, an
optional name and the tests to run. For each one the harness applies the
mutation, clears that file's bytecode cache, runs the named tests,
records the mutant as killed or survived, and restores the file byte for
byte, even after an error or Ctrl-C. It prints a table, and exits
non-zero if any mutant survives, unless told otherwise.

```sh
uv run python scripts/mutate.py <spec>
uv run python scripts/mutate.py --help    # the spec format and options
```

- **Commit first.** The harness refuses to start on a file with
  uncommitted changes, so that restoring it can never lose work; a
  work-in-progress commit is fine.
- **One mutation per regression.** Put back the bug the change fixes, or
  take out the behaviour it adds, and name the tests that should catch
  it. A mutant that survives means the test does not guard that
  regression.
- **Why not a script of your own.** Python reuses a cached `.pyc` while
  its source keeps the same size and modification second, so a same-size
  edit made within a second can run the old code and make a mutant's
  result wrong. The harness clears the cache for each mutation.
- **Evidence.** The PR's mutation-evidence line gives the harness's
  table, with the spec in a collapsed block so that a reviewer can re-run
  it. The spec is scratch: keep it out of the repository.

## The query-engine sandbox

For writing or reviewing a SPARQL query without the live stack.
`scripts/synthetic_graph.py` (#251) writes a deterministic, seeded graph
in Wikibase's RDF shape, of a chosen size and with neutral placeholder
labels only. It holds persons with source-attributed dates at several
precisions, earliest/latest ranges, contradictions between artifacts,
place containment chains, residences, spouses, ranks and redirects.
`scripts/wdqs_sandbox.py` runs a throwaway query engine to load it into:
a `wikibase/wdqs:2` container with no network, capped at 768 MB, under a
unique name.

```sh
make wdqs-sandbox-up                           # start the sandbox
uv run python scripts/synthetic_graph.py --help
uv run python scripts/wdqs_sandbox.py --help   # up, load, query, down
make wdqs-sandbox-down                         # remove it
```

`load` puts a graph into the sandbox. `query` renders the repository's
query templates with the synthetic graph's property ids, runs them, and
prints the rows and the timing. The sandbox needs Docker but never
touches the live stack, so it takes no lock. Take it down when you are
done: a sandbox left running holds memory that other sessions need.

## The live-stack lock

Live test sessions serialise on one lock (#253). When pytest selects any
live test, a session-scoped fixture in `tests/integration/conftest.py`
takes an exclusive lock on `$YIZKR_LIVE_LOCK` (default
`~/.cache/yizkr/live-stack.lock`) before the first live test runs, and
releases it when the session ends; a unit run never takes it. The
default path is per user, not per worktree, so every worktree on the
machine shares it, and sessions exclude each other only when they use
the same path.

A session that finds the lock held prints the holder's pid, command and
start time, and waits; it gives up after a timeout that can be
configured. The lock has flock semantics: it is released when its holder
exits, however it exits, so a crashed session never blocks the next.

Only live test sessions take the lock. A scratch run's reset and write,
`yizkr bootstrap` and other writers to the stack do not, so the
orchestrating session still gives the stack to one agent at a time
(AGENT_PROTOCOL §3).
