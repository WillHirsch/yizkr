# Testing

How the tests are layered, which runs to use while working and before a
push, and the shared tools for proving that a test guards something and
for trying queries without the live stack. Who runs what, and when, is in
[docs/AGENT_PROTOCOL.md](AGENT_PROTOCOL.md): waves (§3), the pre-review
self-check and reviews (§4), and live tests (§5). Each tool documents
itself in its own `--help`.

## Test layers

- **Unit tests** (`tests/unit`) are offline: no stack, no network, no
  model. They are the default run, `uv run pytest -q` or `make test`,
  because `pyproject.toml` deselects the `live` marker unless `-m live`
  asks for it. CI runs them, with ruff, on every push to a pull request
  and on every commit that lands on main.
  `tests/unit/conftest.py` sets their defaults, and
  `tests/unit/test_conftest.py` tests them, apart from the anomaly
  checks' one, which `tests/unit/test_anomalies.py` tests
  (`TestTheUnitTestDefaults`):
  - `CLAUDE_CONFIG_DIR` points at a directory that does not exist, so no
    unit test reads your Claude Code login.
  - `OPENBLAS_NUM_THREADS` is set to 1, unless `OPENBLAS_NUM_THREADS`,
    `OMP_NUM_THREADS` or `MKL_NUM_THREADS` is already set and not empty:
    a thread count given in the environment is kept. OpenBLAS reads the
    variable as numpy loads, so when numpy is loaded before the conftest
    is imported (by a plugin, say), pytest warns that the default came
    too late; set the variable in the environment then.
  - `os.fsync`, `os.fdatasync` and `os.sync` are no-ops, though the
    first two still check the descriptor they are given, so a call on
    one that is not open fails as the real call does. A unit test that
    needs the real sync calls is marked `@pytest.mark.real_fsync`
    (registered in `pyproject.toml`), as the durability tests in
    `tests/unit/test_stages.py` and `tests/unit/test_llmcache.py` are;
    the live tests under `tests/integration` always make them.
  - The anomaly checks (`yizkr.pipeline.anomalies`) start no thread: the
    `_no_anomaly_timers` fixture records the timer a fail-fast pause
    would start instead of starting it, and yields the recorded timers;
    it forgets the checks and the watched slices each test made; and it
    removes `$YIZKR_ANOMALIES`, so the checks run on their default
    thresholds whatever the environment names. The one test that needs a
    real timer takes the real one, which test_anomalies.py saves.
  - `$YIZKR_INSTANCE_STATE` names a folder of each test's own, which
    nothing makes until the test writes there, and a folder of the
    session's own for fixtures of a module or of the session, which set
    up before any test's (the child sessions that test_live_lock.py
    starts, say). So no unit test, and no process one starts, reads or
    clears the developer's record of the instance
    (docs/runbooks/scratch-run.md, Reset the instance).
  - No unit test reads your settings (the package-scoped
    `_no_host_settings` fixture): every `YIZKR_*` variable but
    `$YIZKR_INSTANCE_STATE`, and the stack's connection variables, are
    removed from the environment, and `resolve_env_file` finds no
    default `deploy/.env` or `.env`, as on CI, which has neither. So a
    never-ingest list or a corpus folder your `deploy/.env` names never
    reaches a unit test. A test that needs a setting gives it: in the
    environment, or in an env file it names with `$YIZKR_ENV_FILE` or
    `env_file`. A test pins the list of connection variables to the
    keys `Config.load` reads. `yizkr backup` reads `<deploy-dir>/.env`
    directly, so the unit tests always give it a temporary deploy dir.
    The live tests under `tests/integration` read the real env file;
    the fixture undoes after the last unit test.
  - The config directory, the no-op sync calls, the recorded anomaly
    timers, the removed `$YIZKR_ANOMALIES` and the instance record's
    folder are patches held by the test's own `monkeypatch`, so a test
    that calls `monkeypatch.undo()` loses them for the rest of its run. A
    test that needs a patch for part of its run scopes it with
    `with monkeypatch.context() as patch:` instead.
- **No unit test depends on real elapsed time or the real wall clock**
  (#437). Code that reads a clock or sleeps takes a seam (`clock=`,
  `now=`, `sleep=`, `monotonic=`, `timer=`) defaulting to the real one,
  and the test injects a fake it advances itself. A test of an OS
  mechanism (a lock across processes, a signal, a child) synchronises
  on an event: a pipe, a line the child prints, a joined thread, an
  Event set just before a call blocks. A bounded wait stays only as a
  hang guard, with a module-level `HANG_GUARD` as its timeout, and is
  followed by an assertion that the event happened, so a hang fails. A
  timeout of a literal 0 is a poll and waits no time.
  `tests/unit/test_no_real_time.py` enforces this: it reads every module
  in `tests/unit`, conftest.py and new modules included, with the finder
  in `tests/fixtures/real_time.py` (pinned by
  `tests/unit/test_real_time.py`), and fails, naming the file and line,
  on any use its allow-list `ALLOWED` does not name. It also fails on any
  `HANG_GUARD` that is not a module-level assignment of a number of 30
  to 600 seconds, or that a class or function binds again, so a hang
  guard is never a time a test waits out, nor so long that a missed
  event holds CI for hours. Each entry names the module, the use as the
  finder words it, optionally the test it is limited to (by its
  qualified name, `TestX.test_y`, or by its own name when that names
  only one class or function), and its reason. An entry that no longer
  matches a use, an entry limited to a name that names more than one
  class or function, and an entry naming a module that does not exist all fail, so an entry can
  only be removed, never kept past its use, and a new use always needs a
  new entry with its reason. The finder reads only the module's own
  code, and finds exactly the spellings its docstring lists: a clock
  call, by its final name whatever it is called on (`time.sleep`,
  `t.sleep` after `import time as t`, `time.monotonic`,
  `time.clock_gettime` and the rest, `datetime.now`, `date.today`,
  `threading.Timer`, `signal.alarm`, `setitimer`, `loop.call_later`,
  `call_at`), or by a name it is imported as (`T` after `from threading
  import Timer as T`); a clock function of the time module, a datetime
  or a date taken uncalled; a `time.localtime()`-style read of the
  current time; any `from time import`; a timed wait (`.wait`, `.join`,
  `.wait_for`, `.acquire`, `.get`, `.put`, `.result`, `.exception`,
  `.poll`, `threading.Barrier`, `.settimeout`, `select.select`, a
  selector's `.select`, `asyncio.timeout`) whose timeout is not
  `HANG_GUARD`, None or 0, and any `asyncio.timeout_at` given a
  deadline; a clock function named by a string (`getattr(time,
  "sleep")`, `time.__dict__["sleep"]`, `vars(time)["sleep"]`); an
  `os.utime` given no times, which sets the current time; a ZIP
  archive's `.writestr` given its member's name rather than a `ZipInfo`
  (a `ZipInfo(...)` call, or a name the module assigns one to), which
  stamps the member with the current time, so a deck built twice has
  two SHA-1s (#479); a file's time
  (`st_mtime` and the rest, `getmtime`) read in a function that also
  touches a file; and a real timer started by anomalies' timer starter
  with a wait other than 0. A `.join`'s, a `.poll`'s or a selector's
  `.select`'s lone positional argument is its timeout when it gives a
  time: a number or `HANG_GUARD`, arithmetic on one with no sequence or
  string beside it (`HANG_GUARD / 10`, but not `[name] * 4`), a
  conditional giving one, or a call that makes a number (`float("0.5")`,
  `min(HANG_GUARD, 5)`). Left to review are:
  - a `.join`, a `.poll` or a selector's `.select` given its timeout by
    another name, or by any other expression (a str.join's iterable
    looks the same);
  - a timeout given positionally to `.wait_for`, `.acquire`, `.get`,
    `.put`, `.result` or `.exception`, and a timeout given to any other
    call (`subprocess.run`, `communicate`, `urlopen`, ...);
  - `asyncio.timeout` or `timeout_at` reached through another name
    (`from asyncio import timeout`);
  - a clock function's name given to `getattr` or a subscript as
    anything but a string literal, or read through `__dict__.get`;
  - a file's time compared with the clock after any other write that
    stamps it (`write_text`, `open`), or in another function than the
    touch;
  - a clock reached through another name (`T = threading.Timer`, or
    `dt.now` taken uncalled after `from datetime import datetime as dt`)
    or through a default in `src/`;
  - a wait reached through another name: `s.select` after `import select
    as s`, `sl` after `from select import select as sl`, `B` after `from
    threading import Barrier as B`;
  - a `HANG_GUARD` bound by a `match` capture (`case HANG_GUARD:`), an
    `except ... as`, a `def` or a `class`;
  - a child's script held in a string.

  One test is bounded only by the hang
  guard where it once had a few seconds of CPU of its own:
  `test_long_repetitive_text_checks_quickly` in
  `test_transcription_check.py`. It exists to catch catastrophic
  backtracking, and still does, since a backtracking pattern takes hours
  on its text; a pattern merely a few times slower goes unnoticed.
- **The offline end-to-end scratch run**
  (`tests/unit/test_fixture_corpus.py`, a unit test like the rest) takes
  the fixture corpus (`tests/fixtures/corpus/`, docs/EXAMPLES.md) through
  whole scratch runs, as far as the scratch driver goes: every
  per-document stage, the barrier and the corpus stages. A recorded
  engine answers behind the real LLM cache, and the wiki refuses every
  read and write. It asserts what must come of each stage and of each of
  the corpus's traps, and how a second run, a run invoked again and a
  changed extraction prompt reuse the work, with no gold set. Past the
  driver it holds two readers of the run directory to the corpus too
  (#380): `yizkr scratch report` must count the corpus's stages, how each
  came about, its anomalies (none, on a clean corpus) and its clusters,
  and report.md must say none of it by a universe name, a file name or a
  document's SHA-1; and the clustering stage's identity map must meet the
  traps `TRAPS` sets for it, read in the universe's terms, with the one
  no merge rule meets (a conflicting cluster's confident component,
  #361, #568) recorded as expected by a strict `xfail` that turns when
  the rule does. The driver's write side
  (reset, register, write, places and site, #200, #201) needs a stack,
  so every unit
  test runs the driver with clustering as its only corpus stage (the
  `_no_write_side` fixture in tests/unit/conftest.py); a test marked
  `write_side` keeps them. `tests/unit/test_scratch_write.py` runs the
  write side's stages in order over a run's documents on the in-memory
  instance, with stand-ins for the reset, the live-stack lock and the
  site's build; `tests/unit/test_scratch_places.py` runs the place stage
  with a fake engine behind the run's LLM cache and the graph read from
  the in-memory instance.
- **Live tests** (`tests/integration`, every module marked `live`) run
  against the deployed stack (`make up` or `make up-local`) through
  `make test-live`, which is `uv run pytest -m live tests/integration -v`.
  They skip, saying why, when there is no connection configuration
  (`deploy/.env`) or the stack is unreachable. They write fictional
  fixture data under labels that carry the session's marker (The
  mutation harness, below), so a re-run within the session reuses its
  entities, and that reuse is part of what they check. They run on the
  one instance that scratch runs reset, and take turns on it through the
  live-stack lock (below). Since the scratch write stage does not make
  their writes, each session clears the recorded instance-contents key
  once it holds the lock, and again as it ends, so the next scratch run
  resets the instance first (docs/runbooks/scratch-run.md, Reset the
  instance). `tests/integration/test_live_scratch_reset.py` resets the
  instance, twice, so its resetting tests run only when
  `YIZKR_LIVE_RESET=1` asks for them, and fail at once in a session that
  runs other modules' tests too, and on a kept instance, which they read
  with calls of their own; a reset that fails ends the session.
  `tests/integration/test_live_scratch_write.py` runs a scratch run's
  write side after such a reset, under the same opt-in and in a session
  of its own, and ends by clearing the instance-contents key. A PR
  that changes a write path carries
  `needs:live-test`, and a substantial change to one gets a live test of
  its own (CLAUDE.md). Its live run comes as soon as the draft opens
  (AGENT_PROTOCOL §5), because the stack enforces what no unit test
  sees: the first live run of #227 failed with HTTP 414, since the query
  service refuses a GET request line longer than about 8 KB. Long SPARQL
  is POSTed, as the site build and `run_sparql` in
  `tests/integration/conftest.py` do.
- **Unsupported dates on the instance** (ADR-0034, #338). Every reader
  of the whole graph (the disagreement report, the site build, the
  non-privileged export and reconcile's roster) stops with
  `UnsupportedDate` if the instance holds a date before the common era
  or in a calendar other than the Gregorian, at any rank. Before #338,
  the live century and privacy tests wrote such dates, and the shared
  development instance still holds one (a BCE statement on Q1122) that
  no live session can remove (rule 3). A scratch reset follows #338's
  merge, subject to the owner's OK; until then the live tests that read
  the whole graph fail on that statement, as decided. No live test
  writes such a date now: `test_live_unsupported_dates.py` checks that
  the writer refuses one and writes nothing.
- **The real-model tests** (`tests/integration/test_live_chunked.py`,
  also marked `live`) call the model through the claude CLI: chunked
  extraction end to end, and the exact resumption of a conversation the
  cache kept. They go through the LLM cache at `$YIZKR_LIVE_CACHE`
  (default `var/live-cache`), so a first run makes a few subscription
  calls and later runs are answered from the cache. They need the claude
  CLI, logged in, and skip when it is not installed. They don't use the
  Wikibase stack, so they don't take the live-stack lock.
  `make test-live` runs them with the others; on their own they are
  `uv run pytest -m live tests/integration/test_live_chunked.py -s`, where
  `-s` shows their cache events.

A change to a stage before write that already runs is checked by a
whole-corpus scratch run and its diff, not by a test: there is no gold
set or synthetic eval (ADR-0025, AGENT_PROTOCOL §5). Until the first
whole-corpus scratch run (#145) starts, there is no run to diff against:
a PR that changes a stage carries only its `stage:*` label, and the first
run's baseline includes the change (the pre-baseline exception,
AGENT_PROTOCOL §2).

## Targeted and full runs

While iterating, run only the tests that cover the change:

```sh
uv run pytest -q tests/unit/test_names.py            # one file
uv run pytest -q tests/unit/test_names.py::<name>    # one test
uv run pytest -q tests/unit -k <expression>          # tests by name
uv run pytest -q --lf                                # the last failures
```

The full suite, `uv run pytest -q`, runs once before the push that opens
the PR for review, and once per fix round (AGENT_PROTOCOL §4). It takes
under a minute on an idle machine and far longer under load (#252), so
full runs repeated by many agents slow every one of them. CI runs it on
every push to a pull request, tested merged with main, and a reviewer
reads CI's result for the commit under review
instead of running it again. Ruff is quick: `uv run ruff check .` and
`uv run ruff format --check .` pass before every push, and the pre-commit
hook (`make hooks`) runs ruff on every commit.

## Parallel runs (pytest-xdist)

CI runs the unit suite as `uv run pytest -n auto --dist loadfile` (#252):
a worker per CPU, with each test file kept in one worker, so a file's
module fixtures and test order are as in a serial run. Local runs stay
serial unless `-n` is given. When the machine is idle, a parallel local
run is:

```sh
uv run pytest -q -n auto --dist loadfile
```

While other agents are testing, the extra workers slow everyone's runs,
so run serially then (AGENT_PROTOCOL §3, Waves). Parallel runs are for
unit tests only: live tests take turns on the one instance. A test that
needs several calls in flight at once holds them at a
`threading.Barrier`, as `test_llmcache.py` and `test_chunking.py` do,
never with a sleep, which stops keeping them apart under load. A test
that fails only in parallel or under load is a bug: report it.

## The mutation harness

A test earns its place by failing when the behaviour it guards breaks.
`scripts/mutate.py` (#250) shows that it does. For each mutation in a
spec it writes an exact text edit to one file, runs the tests that
should catch it and restores the file. The mutant is *killed* when the
tests fail and *survives* when they pass.

```sh
uv run python scripts/mutate.py SPEC    # a TOML spec
uv run python scripts/mutate.py --help  # the full contract
```

The spec has one `[[mutation]]` table per mutation:

```toml
[[mutation]]
name = "blank names are kept"    # optional and unique
file = "src/yizkr/example.py"    # relative to the repository root
old = 'if not name.strip():'     # exact text, found exactly once
new = 'if False:'
tests = ["tests/unit/test_example.py::test_blank_names_are_dropped"]
# count = 2   # optional: old is found this many times, all replaced
# live = true # optional: the tests are live tests (below)
```

- **Run it from your own worktree**, with `uv run` there. The harness
  runs pytest under its own interpreter, and an environment made for
  another checkout imports that checkout's `src/`, where no mutation
  lands: every mutant would falsely survive. Run it where nothing else
  writes while it runs: an editor or another session writing a file git
  lists is blamed on the test (below).
- **Commit first.** Before anything runs, it refuses a file that is
  dirty, untracked or a symlink, and `old` text that is missing, found a
  different number of times than `count`, or overlapping. It restores
  each file byte for byte, with its mode and modification time, even
  after a timeout or Ctrl-C, and git must show the file clean again.
- **Tests leave the checkout alone** (#536). Before the baseline and
  after each run (baseline, mutant and live runs alike), the harness
  lists what `git status --porcelain -z --untracked-files=all
  --ignored=no --no-renames` shows, one git call, with each path's type,
  size and modification time. A path that a run made new, changed or
  removed (a deleted untracked file, a reverted edit), outside the
  mutated file, is a stray write: a test wrote into its working
  directory, and a later commit could pick the file up. The harness
  names the run and the paths, leaves the files for you to look at, and
  stops with exit 2, even under `--keep-going`; the table's detail and
  the JSON report's `stray` list name them too (`null` when git could
  not list the checkout, which stops the run as well). A name that is
  not UTF-8 is shown with backslash escapes, such as `\xff-run.txt`.
  Ignored paths (`var/`, `import/`, `.venv/`, `__pycache__/`) are not
  listed: they are runtime state that no commit picks up, and another
  session may write under `var/` while the harness runs. Uncommitted
  changes from before the run are fine as long as no run changes them.
  A test writes under `tmp_path`.
- **Write code in literal strings.** TOML's single-quoted strings
  (`'...'`, or three quotes around several lines) keep every character
  as written. In double-quoted ones a backslash starts an escape, and in
  a multi-line one a backslash at the end of a line joins it to the next.
- **The baseline.** The tests first run on the unmutated tree, once per
  distinct test list, just as the mutants' runs will. Each run must pass,
  with at least one test passing rather than skipping. `--no-baseline`
  skips it.
- **No stale bytecode.** Python reuses a cached `.pyc` while its source
  keeps the same size and modification second, so a same-size edit can
  run the old code. The harness deletes the module's bytecode before
  each run and runs pytest with `PYTHONDONTWRITEBYTECODE=1`, which is why
  a script of your own is no substitute.
- **Live tests** (#283). A mutation with `live = true` runs its tests with
  `-m live`, which overrides `pyproject.toml`'s `-m 'not live'` as
  `make test-live` does, so its `tests` name live tests. Without it,
  pytest deselects a live test that `tests` names, and a list of live
  tests only fails its baseline with "no tests ran". With it, pytest
  deselects a unit test there. A spec with a live mutation is refused up
  front unless a stack is configured: `$YIZKR_ENV_FILE` names a file or,
  with it unset, the checkout has `deploy/.env` or `.env`. The harness
  takes the live-stack lock (below) before the baseline, waiting its turn,
  and holds it until its last run ends; each live run shares its hold.
  Live tests run centrally (AGENT_PROTOCOL §5), so for a new or changed
  live test the PR gives such a spec under Mutation evidence, and the
  central live run runs it and posts the table. Start the harness for a
  live spec where it can finish, in the background or with no short tool
  timeout: a harness killed outright (SIGKILL), as a tool's timeout may
  kill it, leaves the mutant in place and its run holding the lock
  (below).
- **A live test's fixture is the session's own** (#430). A live mutant's
  run writes what the mutant wrote onto the one instance, statements are
  never removed, and the instance is reset only per scratch run. So a
  live test's assertions must hold on an instance that earlier sessions,
  and earlier live mutant runs, have written to. Every item a live test
  writes carries the session's marker, `fixture_mark` in
  `tests/integration/conftest.py`, in its label, title, file name or
  media bytes: eight hex digits of a hash over a sentence saying what the
  fixture is, over the source of the code under test and over eight
  random bytes drawn once a session. Each session, and each mutant's
  run, which is a session of its own, then meets its fixture as a first
  write, and asserts it exactly; a rewrite within the session finds its
  own items again. A test reads only what its session wrote, so it writes
  what it reads even when run alone, never counting on another test's or
  an earlier session's writes. The checks over the whole instance are
  the exceptions, as they read what every session wrote:
  `test_live_queries.py`'s unreferenced-statements invariant;
  `test_live_backup.py`'s privacy and excerpt invariants over the
  non-privileged export; `test_live_nonprivileged_export.py`'s munge
  and block checks over the full dump and the export; and
  `WikiClient.label_index`, which the writer reads, and which
  raises on two properties that share an English label; two items under
  one label break a whole vocabulary read or the bootstrap only when it
  is a manifest vocabulary item's, and a write only when it is its run
  item's (#94). A live mutant that breaks one of these (one that drops a
  reference, mints a duplicate vocabulary or run-item label, or leaves
  an entity the munger drops) breaks every
  later session until the next scratch reset, which is the remedy; the
  fixture's marker cannot help.
- **Stop what a test detaches.** Each pytest run has a process group of
  its own, killed when the run ends or outlives `--timeout` (default
  300 s). A process that leaves the group escapes, and can hold a lock or
  a port that makes later mutants look killed, so tests stop what they
  detach, in a fixture's teardown.
- **Results.** The run stops at the first error unless `--keep-going`,
  and at the first survivor unless `--keep-going` or `--allow-survivors`.
  It prints a Markdown table, ready for the PR, and
  `--json PATH` also writes a report. The exit status is 0 when every
  mutant was killed (or survived under `--allow-survivors`), 1 for a
  survivor, 2 for a refusal or an error, and 128 + N after signal N.
- **Evidence.** The PR's mutation-evidence line gives the table, with the
  spec in a collapsed block so that a reviewer can re-run it. The spec is
  scratch: keep it out of the repository.

## The query-engine sandbox

For writing or reviewing a SPARQL query without the live stack (#251).
`scripts/synthetic_graph.py` writes a deterministic graph in the query
service's form of Wikibase data, and a vocabulary map beside it. Its
labels are neutral placeholders only ("Person 17", "Place 3").
`scripts/wdqs_sandbox.py` runs a throwaway query engine to load it into.
`DIR` is any scratch directory outside the repository:

```sh
uv run python scripts/synthetic_graph.py --persons 250 --seed 7 \
    --out DIR/g.ttl
make wdqs-sandbox-up    # or: uv run python scripts/wdqs_sandbox.py up
uv run python scripts/wdqs_sandbox.py load DIR/g.ttl
uv run python scripts/wdqs_sandbox.py query \
    src/yizkr/queries/disagreement-report.rq --repeat 3 --max-rows 20
make wdqs-sandbox-down  # or: uv run python scripts/wdqs_sandbox.py down
```

- **The graph.** `--persons` sets its size, and the same `--persons` and
  `--seed` give the same bytes on a given Python version. It holds
  persons with source-attributed dates at several precisions,
  earliest/latest ranges, contradictions between artifacts, place
  containment chains, residences, spouses, ranks and redirects.
- **Isolated.** `up` starts a `wikibase/wdqs:2` container with no
  network, no ports and no volumes, capped at 768 MB and one CPU. The
  sandbox never touches the deployed stack, so it takes no live-stack
  lock, and it acts only on a container that carries its label and name
  prefix.
- **One per checkout.** Its state file and container name are keyed on
  the checkout's top directory, so worktrees never share or remove each
  other's sandbox. One command at a time uses a sandbox, under a lock of
  its own beside the state file; a second one says it is waiting.
- **Queries.** `query` renders a template as `yizkr publish-queries`
  does, through the vocabulary map beside the graph loaded last
  (`--vocab` names another; `--raw` sends a finished query as it is). It
  prints the rows as tab-separated values and the engine's wall time.
  The engine stops each run of a query at `--timeout` (default 100 s),
  and `load` stops the emptying of `--replace`, and each file's load, at
  its own `--timeout` (default 90 s), so that at the defaults each of
  these requests ends within two minutes. A command that sends several
  requests can take longer: `query --repeat N`, and every `load`, which
  ends by counting the store's triples (after the emptying of
  `--replace`, and one request per file). So can a load cut short, which
  waits for the engine (below).
- **Stopping.** The sandbox runs each docker call in a session of its
  own, so Ctrl-C at the terminal, or a signal to the process group,
  reaches the script and none of its docker calls, and the script stops
  the command itself. Every query that `query` sends carries a marker,
  so Ctrl-C or SIGTERM during `query` cancels that query in the engine,
  and nothing else, and kills its curl. During a load, or the emptying
  of `load --replace`, it kills only the curl, since cancelling a write
  would leave the engine unable to take another until the sandbox
  restarts, and gives the engine up to 20 s to finish or abort the write
  itself. When the engine is still busy, it says "waiting up to 20 s for
  the engine to finish or abort the write". A load that runs past its
  `--timeout` ends the same way. The engine commits a load whole or not
  at all. Once a command is cleaning up, Ctrl-C and SIGTERM are ignored
  until it is done. The sandbox reads the engine's running tasks from
  its status page. When that page cannot be read whole, `query` cancels
  nothing (the engine stops the query at `--timeout`), and a load cut
  short takes the engine as still busy and goes on waiting, up to the
  20 s.
- **Timings are indicative.** One CPU makes them noisy: compare variants
  in one session with `--repeat 3` and their medians, or start the
  sandbox with `up --cpus 2`. The sandbox compares queries; it does not
  predict the deployment's times.
- **Take it down** when you are done (`down` is safe to repeat): a
  sandbox left running holds memory that other sessions need.

## The live-stack lock

Live test sessions take turns on the one instance (#253). A session
takes an exclusive lock on `$YIZKR_LIVE_LOCK` (default
`~/.cache/yizkr/live-stack.lock`) when its first test that uses the
stack sets up, and holds it to the end of the session: the `config`
fixture in `tests/integration/conftest.py` takes the `live_stack_lock`
fixture, and `tests/integration/live_lock.py` holds the code. Unit runs
never take the lock, and nor do the real-model tests, which need only the
claude CLI. The default path is per user, not per worktree, so every
worktree on the machine shares it. `$YIZKR_LIVE_LOCK` must be an absolute
path, though a leading `~` is expanded.

- **Waiting.** A session that finds the lock held says once who holds it
  (pid, command, working directory and start time), and waits. After
  `$YIZKR_LIVE_LOCK_TIMEOUT` seconds (default 7200) it gives up, once for
  the session, and every test that uses the stack then errors at setup
  with that message.
- **Crashes.** The lock is a flock, which the kernel releases when its
  holder ends, however it ends, so a crashed or killed session blocks
  nothing. The exception is a mutation harness ended during a live run
  by a signal that it does not catch: SIGKILL, say, or SIGQUIT, SIGUSR1
  or SIGSEGV (on SIGINT, SIGTERM or SIGHUP it stops the run itself). The
  run goes on, with no `--timeout` to stop it, and keeps the lock until
  its pytest process, and every process that a test forked from it
  without exec, has ended. A program that a test starts, by `os.system`
  say, does not keep it, however early the test runs (below). The holder
  records itself in the lock file and empties the record as it releases
  the lock, so a record in a free lock file was left by a crash.
- **Never delete the lock file.** A session that found no file would
  create and lock a new one, while the holder of the old one went on:
  two sessions on the stack at once.
- **The mutation harness** takes the lock for a spec with a live mutation,
  once, before its baseline, and holds it until its last run ends, so no
  other live session reaches the stack between two of its runs. Its live
  runs share that hold. Each inherits the harness's locked descriptor,
  named in `$YIZKR_LIVE_LOCK_FD`, and its `live_stack_lock` fixture holds
  the lock through that descriptor instead of waiting for the harness. A
  flock belongs to the open file description, which the harness and its
  runs share, so the kernel, not the variable, vouches for the hold: the
  variable counts only when it names a descriptor, open in the process, of
  the lock file itself, on which flock succeeds at once, as it does only
  while that descriptor's description holds the lock or nothing does;
  otherwise the session waits its turn like any other. So even a stray or
  stale variable never lets two sessions hold the lock at once. A run that
  shares the hold never releases it or writes the record, which names the
  harness. As the run's conftest imports `live_lock.py`, before any test
  runs, the module makes the descriptor close-on-exec, as a session's own
  descriptor is; it does so only when the variable names a descriptor of
  the lock file, and changes nothing else. So no program that a test
  starts (by `os.system`, a shell's background job or `close_fds=False`)
  shares the hold; a process that a test forks without exec does. If a
  signal that the harness does not catch ends it, the run's copy of the
  descriptor keeps the lock held until the run ends (Crashes, above). A
  session waiting meanwhile names the harness from the record, with its
  working directory, in the checkout where it left the mutant, and says
  that it has ended and that the lock is held by a process with no
  record of its own.
- **Live test sessions, the harness and the reset take it.**
  `yizkr scratch reset` takes the lock before it reads the kept-state
  marker and holds it to its end, waiting while a live session holds it,
  as a live session waits while a reset runs. A caller that holds the
  lock already, as a live test of the reset does, gives the reset its
  own hold (`yizkr.reset.InstanceReset`, `lock`). A scratch run's
  register and write stages take it while they write
  (`yizkr.pipeline.scratchwrite.live_stack`), and its reset stage takes
  it through the reset. `yizkr bootstrap` and other writers to the stack
  don't take it, so the
  orchestrating session still gives the stack to one agent at a time
  (AGENT_PROTOCOL §3). Live test sessions need no check by hand for
  another one: the lock makes them wait their turn.
