"""`yizkr scratch reset`: take the one instance back to a fresh start and
repopulate it for a scratch run (ADR-0025 §1; docs/runbooks/scratch-run.md,
Reset the instance).

The reset runs in steps, and prints how long each took:

1. check the kept-state marker, and refuse on a kept instance;
2. clear the write-phase state: the instance-contents record is cleared,
   and the write stage's state set aside (yizkr.core.instance);
3. stop the stack (`docker compose down`, which keeps every volume);
4. remove the graph volumes: the database, the search index, the query
   service, and the wiki's uploaded media, which only the database
   refers to;
5. set LocalSettings.php aside, so that the stack installs MediaWiki
   afresh as it starts, as on a first boot;
6. start the stack (`docker compose up --wait`), and wait for its API;
7. bootstrap the manifest;
8. publish the queries;
9. run the smoke test.

It takes no backup: a scratch graph is never kept (ADR-0024 §7, ADR-0025
§1). It never touches the LLM cache, a run directory or its reports, or
the corpus, which all outlive the instance.

**Guards.** Before it changes anything, the reset reads the stack's
compose files and refuses:

- compose files that do not declare the four volumes under the keys
  Wikibase Suite Deploy gives them, or that declare one external;
- a stack whose containers were made from another directory or other
  compose files: starting the stack from these would swap its config
  directory, and so its LocalSettings.php, for another's;
- a volume of one of those names that Docker Compose did not make for
  this project as that volume: the reset removes the stack's own volumes
  and nothing else;
- a reset that is not confirmed: interactively, by typing the word
  CONFIRMATION, and otherwise by `--yes`.

It then takes the live-stack lock and holds it to the end, so no live
test session uses the instance while it is reset, and reads the
kept-state marker. A kept instance is refused, and so is an instance
whose marker cannot be read: a stopped stack is started to read it, and
a stack that does not answer, or answers as another wiki than the one
these compose files serve, leaves the reset unable to tell. An instance
with no database volume holds no graph, so there is nothing to guard.

**What it deletes.** The four volumes, and nothing else. The rest of what
it clears it sets aside in the instance's record (yizkr.core.instance),
in reset/<time>/: the write stage's state, and LocalSettings.php, whose
database is gone.

**Again.** Every step is safe to repeat, so a reset that failed or was
interrupted is finished by running it again: it removes only the volumes
there are, sets aside only what is there, and the bootstrap, the queries
and the smoke test are idempotent.

**The lock's code** is the live tests' own (tests/integration/live_lock.py),
loaded from the checkout this package is imported from, as
scripts/mutate.py loads it: the reset is a tool of a checkout, which also
holds the stack's deploy directory.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yizkr.core.config import Config, resolve_env_file
from yizkr.core.instance import (
    KEPT_STATE_PAGE,
    carries_kept_marker,
    clear_contents,
    instance_dir,
    private_dir,
    write_state_dir,
)

# the stack's volumes the reset removes: each one's key in the compose
# files, and what it holds
VOLUMES = (
    ("mysql-data", "database"),
    ("elasticsearch-data", "search index"),
    ("wdqs-data", "query service"),
    ("wikibase-image-data", "uploaded media"),
)
DATABASE_VOLUME = "mysql-data"
# the service whose directory at CONFIG_TARGET holds LocalSettings.php: the
# image installs MediaWiki afresh as it starts when that file is not there
WIKIBASE_SERVICE = "wikibase"
CONFIG_TARGET = "/config"
LOCAL_SETTINGS = "LocalSettings.php"
DEFAULT_COMPOSE_FILES = ("docker-compose.yml",)
# the labels Docker Compose gives what it makes for a project
PROJECT_LABEL = "com.docker.compose.project"
VOLUME_LABEL = "com.docker.compose.volume"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
# seconds: for the stack to start and be healthy; for the smoke test's
# query-service round trip; for the wiki's API once the stack is up, and
# between two tries of it; for a docker command that only reads; for the
# stack to stop
DEFAULT_START_TIMEOUT = 900.0
DEFAULT_SMOKE_TIMEOUT = 300
API_WAIT = 120.0
API_POLL = 2.0
QUERY_TIMEOUT = 120.0
STOP_TIMEOUT = 600.0
# the word an interactive reset is confirmed by
CONFIRMATION = "reset"
# in the instance's record: where each reset sets aside what it clears
RESET_DIR = "reset"
# who a reset's clearing of the instance-contents record names
CLEARED_BY = "yizkr scratch reset"
# the live-stack lock's code, in a checkout
LOCK_MODULE = Path("tests") / "integration" / "live_lock.py"


class ResetRefused(RuntimeError):
    """The reset will not go on until what the message names changes. It
    has removed nothing."""


class ResetFailed(RuntimeError):
    """A step of the reset failed; the message names it. Every step is
    safe to repeat, so running the reset again finishes it."""


class DockerFailed(RuntimeError):
    """A docker command could not run, did not finish in time, or exited
    with an error."""


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def elapsed(seconds: float) -> str:
    """A step's time, as the reset prints it."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s"


# -- docker -------------------------------------------------------------------


def run_docker(
    argv: Sequence[str], *, capture: bool = True, timeout: float | None = None
) -> str:
    """Run a docker command: its standard output when `capture`, and
    otherwise its output goes to the terminal, as the command runs.
    DockerFailed when it cannot run, runs past `timeout` or fails."""
    command = shlex.join(argv)
    try:
        done = subprocess.run(
            list(argv), capture_output=capture, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise DockerFailed(
            f"{argv[0]} is not installed or not on PATH"
        ) from None
    except subprocess.TimeoutExpired:
        raise DockerFailed(
            f"`{command}` did not finish in {timeout:g} s"
        ) from None
    if done.returncode != 0:
        said = (done.stderr or "").strip() if capture else ""
        raise DockerFailed(
            f"`{command}` exited with {done.returncode}"
            + (f": {said}" if said else "")
        )
    return done.stdout if capture else ""


@dataclass(frozen=True)
class Compose:
    """The stack as `docker compose` addresses it: its deploy directory,
    the env file its compose files read, and the compose files, relative
    to the deploy directory unless absolute."""

    deploy_dir: Path
    env_file: Path
    files: tuple[str, ...] = DEFAULT_COMPOSE_FILES

    def paths(self) -> list[Path]:
        return [
            path if path.is_absolute() else self.deploy_dir / path
            for path in map(Path, self.files)
        ]

    def command(self, *arguments: str) -> list[str]:
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for path in self.paths():
            command += ["-f", str(path)]
        return [*command, *arguments]


@dataclass(frozen=True)
class Volume:
    """One of the stack's volumes the reset removes: its key in the
    compose files, its name, and what it holds."""

    key: str
    name: str
    holds: str


@dataclass(frozen=True)
class Plan:
    """What the reset read of the stack before changing anything: the
    compose project, the volumes it removes, those of them that exist,
    and the directory the wiki keeps LocalSettings.php in."""

    project: str
    volumes: tuple[Volume, ...]
    present: frozenset[str]
    config_dir: Path

    @property
    def database(self) -> Volume:
        return next(v for v in self.volumes if v.key == DATABASE_VOLUME)


def _label(name: str) -> str:
    return f'{{{{.Label "{name}"}}}}'


def read_plan(compose: Compose, runner: Callable[..., str]) -> Plan:
    """Read the stack's compose files, containers and volumes, changing
    nothing; ResetRefused for a stack the reset does not reset (the
    module docstring's guards)."""
    raw = runner(
        compose.command("config", "--format", "json"),
        capture=True,
        timeout=QUERY_TIMEOUT,
    )
    try:
        declared = json.loads(raw)
    except ValueError:
        declared = None
    if not isinstance(declared, dict) or not declared.get("name"):
        raise ResetRefused(
            f"`docker compose config` names no project for "
            f"{', '.join(map(str, compose.paths()))}"
        )
    project = str(declared["name"])
    volumes, missing, external = [], [], []
    for key, holds in VOLUMES:
        entry = (declared.get("volumes") or {}).get(key)
        if not isinstance(entry, dict) or not entry.get("name"):
            missing.append(key)
        elif entry.get("external"):
            external.append(key)
        else:
            volumes.append(Volume(key, str(entry["name"]), holds))
    if missing:
        raise ResetRefused(
            f"the compose files declare no volume {', '.join(missing)}: the "
            "reset removes the stack's database, search index, query "
            "service and media volumes, and knows them by the keys Wikibase "
            f"Suite Deploy gives them ({', '.join(k for k, _ in VOLUMES)})"
        )
    if external:
        raise ResetRefused(
            f"the compose files declare the volume {', '.join(external)} "
            "external, so it is not the stack's own, and the reset removes "
            "the stack's own volumes and nothing else"
        )
    service = (declared.get("services") or {}).get(WIKIBASE_SERVICE) or {}
    config_dirs = [
        Path(mount["source"])
        for mount in service.get("volumes") or []
        if isinstance(mount, dict)
        and mount.get("type") == "bind"
        and mount.get("target") == CONFIG_TARGET
        and mount.get("source")
    ]
    if not config_dirs:
        raise ResetRefused(
            f"the {WIKIBASE_SERVICE} service mounts no directory at "
            f"{CONFIG_TARGET}, where the wiki keeps its {LOCAL_SETTINGS}"
        )
    check_containers(compose, project, runner)
    return Plan(
        project=project,
        volumes=tuple(volumes),
        present=present_volumes(project, volumes, runner),
        config_dir=config_dirs[0],
    )


def check_containers(
    compose: Compose, project: str, runner: Callable[..., str]
) -> int:
    """How many containers the project has; ResetRefused when any of them
    was made from another directory or other compose files than
    `compose`'s, since starting the stack from these would swap its
    config directory for another's."""
    listed = runner(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            "\t".join(
                [
                    "{{.Names}}",
                    _label(WORKING_DIR_LABEL),
                    _label(CONFIG_FILES_LABEL),
                ]
            ),
        ],
        capture=True,
        timeout=QUERY_TIMEOUT,
    )
    ours = [path.resolve() for path in compose.paths()]
    here = compose.deploy_dir.resolve()
    strangers, count = [], 0
    for line in listed.splitlines():
        if not line.strip():
            continue
        count += 1
        name, working_dir, files = (line.split("\t") + ["", ""])[:3]
        theirs = [Path(path).resolve() for path in files.split(",") if path]
        if not working_dir or Path(working_dir).resolve() != here:
            strangers.append(f"{name} (made in {working_dir or '?'})")
        elif theirs != ours:
            strangers.append(f"{name} (made from {files or '?'})")
    if strangers:
        raise ResetRefused(
            f"the {project} stack was started from other compose files than "
            f"{', '.join(map(str, ours))}: {'; '.join(strangers)}. Starting "
            "it from these would swap its config directory, and so its "
            f"{LOCAL_SETTINGS}, for another's: run the reset with the ones "
            "that started it (--deploy-dir, --compose-file)"
        )
    return count


def present_volumes(
    project: str, volumes: Sequence[Volume], runner: Callable[..., str]
) -> frozenset[str]:
    """The names of `volumes` that exist; ResetRefused when one of those
    names is a volume that Docker Compose did not make for `project` as
    that volume."""
    listed = runner(
        [
            "docker",
            "volume",
            "ls",
            "--format",
            "\t".join(
                ["{{.Name}}", _label(PROJECT_LABEL), _label(VOLUME_LABEL)]
            ),
        ],
        capture=True,
        timeout=QUERY_TIMEOUT,
    )
    found = {}
    for line in listed.splitlines():
        name, made_for, made_as = (line.split("\t") + ["", ""])[:3]
        if name:
            found[name] = (made_for, made_as)
    strangers = [
        f"{volume.name} (made for project {found[volume.name][0] or '?'}, "
        f"as {found[volume.name][1] or '?'})"
        for volume in volumes
        if volume.name in found and found[volume.name] != (project, volume.key)
    ]
    if strangers:
        raise ResetRefused(
            f"volumes that the {project} stack did not make: "
            f"{'; '.join(strangers)}. The reset removes the stack's own "
            "volumes and nothing else"
        )
    return frozenset(volume.name for volume in volumes if volume.name in found)


# -- the live-stack lock ------------------------------------------------------


def checkout_root() -> Path:
    """The checkout this package is imported from."""
    import yizkr

    return Path(yizkr.__file__).resolve().parents[2]


def live_stack_lock() -> contextlib.AbstractContextManager:
    """The live-stack lock's hold(), from the live tests' lock module in
    this checkout (the module docstring): the lock at $YIZKR_LIVE_LOCK,
    waited for up to $YIZKR_LIVE_LOCK_TIMEOUT seconds, saying who holds
    it while it waits."""
    path = checkout_root() / LOCK_MODULE
    if not path.is_file():
        raise ResetRefused(
            "the reset holds the live-stack lock, whose code is "
            f"{LOCK_MODULE} in a checkout, and {path} is missing: run the "
            "reset from a checkout"
        )
    spec = importlib.util.spec_from_file_location("yizkr_live_lock", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.hold()


# -- the steps that repopulate ------------------------------------------------


def bootstrap_manifest(config: Config, log: Callable[..., None]) -> str:
    from yizkr.core.wiki import WikiClient
    from yizkr.model.bootstrap import bootstrap_instance
    from yizkr.model.schema import load_manifest

    counts = bootstrap_instance(WikiClient(config), load_manifest(), log=log)
    return (
        f"{counts['created']} created, {counts['updated']} updated, "
        f"{counts['unchanged']} already there"
    )


def publish_query_pages(config: Config, log: Callable[..., None]) -> str:
    from yizkr.core.wiki import WikiClient
    from yizkr.queries.publish import publish_queries

    pages = publish_queries(WikiClient(config), config, log=log)
    return f"{len(pages)} pages"


def smoke_test(config: Config, log: Callable[..., None], timeout: int) -> str:
    from yizkr.smoke import run_smoke

    return f"marker item {run_smoke(config, timeout=timeout, log=log)}"


# -- the reset ----------------------------------------------------------------


@dataclass(frozen=True)
class StepTime:
    name: str
    seconds: float
    note: str = ""


@dataclass
class ResetReport:
    """What a reset did: each step it finished, with its time, and where
    it set aside what it cleared."""

    plan: Plan
    steps: list[StepTime] = field(default_factory=list)
    set_aside: Path | None = None

    @property
    def seconds(self) -> float:
        return sum(step.seconds for step in self.steps)


def describe_plan(plan: Plan, compose: Compose, config: Config) -> list[str]:
    """What a reset of the stack will do, for its confirmation."""
    removed = [v for v in plan.volumes if v.name in plan.present]
    return [
        f"This resets the instance at {config.wikibase_url} for a "
        "scratch run:",
        f"  the stack: compose project {plan.project}, from "
        f"{', '.join(map(str, compose.paths()))}",
        (
            "  it removes the volumes "
            + ", ".join(f"{v.name} ({v.holds})" for v in removed)
            if removed
            else "  it finds none of the stack's graph volumes to remove"
        ),
        f"  it sets {plan.config_dir / LOCAL_SETTINGS} aside, so that the "
        "stack installs MediaWiki afresh",
        "  it takes no backup: everything written to the instance is lost",
    ]


def describe(report: ResetReport) -> list[str]:
    """A finished reset's summary: the time of each step, and of all."""
    width = max((len(step.name) for step in report.steps), default=0)
    lines = [
        f"The instance is reset and repopulated, in {elapsed(report.seconds)}:"
    ]
    lines += [
        f"  {step.name:<{width}}  {elapsed(step.seconds):>10}"
        for step in report.steps
    ]
    if report.set_aside is not None:
        lines.append(
            f"What it cleared is set aside in {report.set_aside}, to remove "
            "when you no longer want it."
        )
    return lines


class InstanceReset:
    """One reset of the stack `compose` addresses, whose wiki `config`
    names (the module docstring says how). `yes` confirms it; otherwise
    it asks, through `ask`, when `interactive` (by default, when standard
    input is a terminal), and refuses when not. `lock` gives the hold on
    the live-stack lock (by default live_stack_lock()): a caller that
    holds the lock already passes contextlib.nullcontext. The other
    arguments serve the tests: the docker runner, the wiki clients, the
    steps that repopulate, the instance record's directory, the clock and
    the log."""

    def __init__(
        self,
        compose: Compose,
        config: Config,
        *,
        yes: bool = False,
        interactive: bool | None = None,
        ask: Callable[[str], str] = input,
        lock: Callable[[], contextlib.AbstractContextManager] | None = None,
        runner: Callable[..., str] = run_docker,
        client_factory: Callable[[Config], Any] | None = None,
        bootstrap: Callable[..., str] = bootstrap_manifest,
        publish: Callable[..., str] = publish_query_pages,
        smoke: Callable[..., str] = smoke_test,
        state: Path | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        smoke_timeout: int = DEFAULT_SMOKE_TIMEOUT,
        api_wait: float = API_WAIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
        log: Callable[..., None] = print,
    ) -> None:
        self.compose = compose
        self.config = config
        self.yes = yes
        self.interactive = interactive
        self.ask = ask
        self.lock = live_stack_lock if lock is None else lock
        self.runner = runner
        if client_factory is None:
            from yizkr.core.wiki import WikiClient

            client_factory = WikiClient
        self.client_factory = client_factory
        self.bootstrap = bootstrap
        self.publish = publish
        self.smoke = smoke
        self.state = state
        self.start_timeout = start_timeout
        self.smoke_timeout = smoke_timeout
        self.api_wait = api_wait
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.report: ResetReport | None = None

    # -- the run ----------------------------------------------------------

    def run(self) -> ResetReport:
        plan = read_plan(self.compose, self.runner)
        self.confirm(plan)
        self.report = ResetReport(plan)
        steps: list[tuple[str, Callable[[], str]]] = [
            ("check the kept-state marker", lambda: self.check_not_kept(plan)),
            ("clear the write-phase state", self.clear_write_state),
            ("stop the stack", self.stop),
            ("remove the graph volumes", lambda: self.remove_volumes(plan)),
            (f"set {LOCAL_SETTINGS} aside", lambda: self.set_aside(plan)),
            ("start the stack", self.start),
            (
                "bootstrap the manifest",
                lambda: self.bootstrap(self.config, self.log),
            ),
            (
                "publish the queries",
                lambda: self.publish(self.config, self.log),
            ),
            (
                "run the smoke test",
                lambda: self.smoke(self.config, self.log, self.smoke_timeout),
            ),
        ]
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(self.lock())
            except (TimeoutError, ValueError) as exc:
                raise ResetRefused(
                    f"the live-stack lock cannot be held ({exc})"
                ) from None
            for number, (name, action) in enumerate(steps, 1):
                self._step(f"[{number}/{len(steps)}]", name, action)
        return self.report

    def _step(self, tag: str, name: str, action: Callable[[], str]) -> None:
        self.log(f"{tag} {name}")
        started = self.clock()
        try:
            note = action()
        except ResetRefused:
            raise
        except KeyboardInterrupt:
            self.log(
                f"{tag} {name}: interrupted after "
                f"{elapsed(self.clock() - started)}"
            )
            raise
        except (Exception, SystemExit) as exc:
            raise ResetFailed(
                f"{name} failed after {elapsed(self.clock() - started)} "
                f"({_why(exc)}). Every step is safe to repeat: run "
                "`yizkr scratch reset` again to finish the reset"
            ) from exc
        seconds = self.clock() - started
        self.report.steps.append(StepTime(name, seconds, note or ""))
        self.log(f"{tag} {elapsed(seconds)}" + (f": {note}" if note else ""))

    def confirm(self, plan: Plan) -> None:
        """Nothing, when the reset is confirmed; ResetRefused otherwise."""
        if self.yes:
            return
        interactive = (
            sys.stdin.isatty()
            if self.interactive is None
            else self.interactive
        )
        if not interactive:
            raise ResetRefused(
                f"the reset deletes the graph of the instance at "
                f"{self.config.wikibase_url}, and with no terminal to ask at "
                "it needs --yes to confirm it"
            )
        for line in describe_plan(plan, self.compose, self.config):
            self.log(line)
        try:
            answer = self.ask(f'Type "{CONFIRMATION}" to reset it: ')
        except EOFError:
            answer = ""
        if answer.strip() != CONFIRMATION:
            raise ResetRefused("not confirmed; nothing was changed")

    # -- the wiki ---------------------------------------------------------

    def _siteinfo(self) -> dict | str:
        """The wiki's general site information, or why it gave none."""
        try:
            data = self.client_factory(self.config).api_get(
                {"action": "query", "meta": "siteinfo"}
            )
            general = data["query"]["general"]
        except Exception as exc:
            return _why(exc)
        if not isinstance(general, dict):
            return f"its site information is {general!r}"
        return general

    def _await_api(self) -> None:
        """Wait for the wiki's API to answer, up to api_wait seconds from
        its first failure to."""
        deadline = None
        while True:
            answer = self._siteinfo()
            if isinstance(answer, dict):
                return
            now = self.clock()
            if deadline is None:
                deadline = now + self.api_wait
            if now >= deadline:
                raise TimeoutError(
                    f"the wiki's API at {self.config.wikibase_url} did not "
                    f"answer within {self.api_wait:g} s of the stack's start "
                    f"({answer})"
                )
            self.sleep(API_POLL)

    def _cannot_tell(self, why: str) -> str:
        return (
            f"the instance at {self.config.wikibase_url} cannot be read "
            f"({why}), so the reset cannot tell whether it carries the "
            f"kept-state marker, the page {KEPT_STATE_PAGE}, and a reset "
            "never touches a kept instance (ADR-0025 §6). Nothing was "
            "removed. Bring the stack up, so that its wiki answers, and run "
            "the reset again; docs/runbooks/scratch-run.md (Reset the "
            "instance) says what to do when it cannot start"
        )

    # -- the steps --------------------------------------------------------

    def check_not_kept(self, plan: Plan) -> str:
        """Refuse on a kept instance, and on one whose marker cannot be
        read; say why the reset may go on."""
        database = plan.database
        if database.name not in plan.present:
            return (
                f"there is no {database.holds} volume ({database.name}), so "
                "the instance holds no graph to guard"
            )
        general = self._siteinfo()
        if not isinstance(general, dict):
            self.log(
                f"      the wiki does not answer ({general}): starting the "
                "stack to read its kept-state marker"
            )
            try:
                self.start()
            except (DockerFailed, TimeoutError) as exc:
                raise ResetRefused(self._cannot_tell(str(exc))) from None
            general = self._siteinfo()
            if not isinstance(general, dict):
                raise ResetRefused(self._cannot_tell(general))
        served, ours = general.get("server"), self.config.concept_uri
        if _host(str(served or "")) != _host(ours):
            raise ResetRefused(
                f"the wiki that answers at {self.config.wikibase_url} is "
                f"served as {served!r}, and the stack serves {ours}: the "
                "reset reads the kept-state marker of the instance it "
                "resets, so it cannot tell whether that one is kept. "
                "Nothing was removed"
            )
        try:
            kept = carries_kept_marker(self.client_factory(self.config))
        except Exception as exc:
            raise ResetRefused(self._cannot_tell(_why(exc))) from None
        if kept:
            raise ResetRefused(
                f"the instance at {self.config.wikibase_url} carries the "
                f"kept-state marker, the page {KEPT_STATE_PAGE}: it is the "
                "kept instance, and a reset never touches a kept instance "
                "(ADR-0025 §6). Nothing was removed"
            )
        return f"no page {KEPT_STATE_PAGE}: the instance is not kept"

    def _aside(self) -> Path:
        """This reset's directory for what it sets aside, made on first
        use: reset/<time>/ in the instance's record."""
        if self.report.set_aside is None:
            parent = private_dir(
                instance_dir(self.config, self.state) / RESET_DIR
            )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for attempt in itertools.count(1):
                candidate = parent / (
                    stamp if attempt == 1 else f"{stamp}-{attempt}"
                )
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                self.report.set_aside = candidate
                break
        return self.report.set_aside

    def clear_write_state(self) -> str:
        """Clear the instance-contents record, and set the write stage's
        state aside."""
        record = clear_contents(self.config, CLEARED_BY, root=self.state)
        write = write_state_dir(self.config, self.state)
        if not os.path.lexists(write):
            return f"cleared {record}"
        target = self._aside() / write.name
        os.rename(write, target)
        return (
            f"cleared {record}; the write-phase state is set aside in {target}"
        )

    def stop(self) -> str:
        self.runner(
            self.compose.command("down"), capture=False, timeout=STOP_TIMEOUT
        )
        return ""

    def remove_volumes(self, plan: Plan) -> str:
        """Remove those of the stack's graph volumes that exist now, which
        the stack's start to read the marker may have made."""
        present = present_volumes(plan.project, plan.volumes, self.runner)
        removed = [v for v in plan.volumes if v.name in present]
        if removed:
            self.runner(
                ["docker", "volume", "rm", *(v.name for v in removed)],
                capture=False,
                timeout=QUERY_TIMEOUT,
            )
        return "removed " + (
            ", ".join(f"{v.name} ({v.holds})" for v in removed)
            or "nothing: none was there"
        )

    def set_aside(self, plan: Plan) -> str:
        settings = plan.config_dir / LOCAL_SETTINGS
        if not os.path.lexists(settings):
            return f"there is no {settings}"
        target = self._aside() / LOCAL_SETTINGS
        shutil.move(settings, target)
        return f"{settings} is set aside in {target}"

    def start(self) -> str:
        self.runner(
            self.compose.command(
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                str(round(self.start_timeout)),
            ),
            capture=False,
            timeout=self.start_timeout + QUERY_TIMEOUT,
        )
        self._await_api()
        return ""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def stack(
    env_file: Path | None = None,
    deploy_dir: Path | None = None,
    compose_files: Sequence[str] | None = None,
) -> tuple[Compose, Config]:
    """The stack a reset addresses, and its wiki's connection settings,
    both from the one env file (resolved as Config.load resolves it): the
    compose files read it, and they are in `deploy_dir`, by default the
    env file's directory."""
    env = resolve_env_file(env_file)
    if env is None or not env.is_file():
        raise ResetRefused(
            f"there are no connection settings ({env or 'deploy/.env'} is "
            "not a file): the reset reads the stack's env file, which its "
            "compose files read too; name it with --env-file or "
            "$YIZKR_ENV_FILE"
        )
    env = env.resolve()
    deploy = (Path(deploy_dir) if deploy_dir else env.parent).resolve()
    compose = Compose(
        deploy, env, tuple(compose_files or DEFAULT_COMPOSE_FILES)
    )
    missing = [str(path) for path in compose.paths() if not path.is_file()]
    if missing:
        raise ResetRefused(
            f"there is no compose file {', '.join(missing)}: name the stack's "
            "deploy directory with --deploy-dir (by default, the env file's "
            "directory)"
        )
    try:
        config = Config.load(env)
    except SystemExit as exc:  # ConfigError: a setting is missing
        raise ResetRefused(str(exc)) from None
    return compose, config


def reset_instance(
    *,
    env_file: Path | None = None,
    deploy_dir: Path | None = None,
    compose_files: Sequence[str] | None = None,
    **options: Any,
) -> ResetReport:
    """`yizkr scratch reset`: reset the stack the env file's deploy
    directory holds (stack()), with InstanceReset's options."""
    compose, config = stack(env_file, deploy_dir, compose_files)
    return InstanceReset(compose, config, **options).run()
