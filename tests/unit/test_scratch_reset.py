"""Offline tests of `yizkr scratch reset` (#199), the one command that
resets the instance and repopulates it for a scratch run (yizkr.reset),
and of the record of the instance that tells a scratch run what it may do
with it (yizkr.core.instance): the kept-state marker, the
instance-contents key and the write-phase state.

A fake docker plays the stack: its compose files, containers and
volumes, and the fresh install a start makes when LocalSettings.php is
not there. A fake wiki answers the API calls that the reset and the
record make, and loses its pages with its database volume. No test runs
docker, reaches a wiki or touches the user's record of the instance
(tests/unit/conftest.py).
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests

from tests.integration import live_lock
from yizkr import cli
from yizkr.core import instance
from yizkr.core.config import Config
from yizkr.core.instance import (
    KEPT_STATE_PAGE,
    carries_kept_marker,
    clear_contents,
    host_kept_marker,
    instance_is_kept,
    kept_path,
    mark_kept,
    read_record,
    record_contents,
    recorded_contents,
    state_root,
    watermark,
    write_state_dir,
)
from yizkr.reset import (
    CLEARED_BY,
    LOCAL_SETTINGS,
    ORIGIN_FILE,
    Compose,
    DockerFailed,
    InstanceReset,
    ResetFailed,
    ResetRefused,
    assigned_in,
    describe,
    elapsed,
    stack,
)

PROJECT = "yizkr-wikibase"
HOST = "wikibase.example"
URL = f"http://{HOST}"
CONFIG = Config(
    wikibase_url=URL,
    concept_uri=URL,
    admin_name="admin",
    admin_pass="Hutmacherei-1863",
    wdqs_url=f"http://query.{HOST}",
    sparql_url=f"http://query.{HOST}/sparql",
    env_file=None,
)
# every volume the compose files declare, by key; the reset removes the
# first four
DECLARED = (
    "mysql-data",
    "elasticsearch-data",
    "wdqs-data",
    "wikibase-image-data",
    "quickstatements-data",
)
GRAPH = tuple(f"{PROJECT}_{key}" for key in DECLARED[:4])
DATABASE = f"{PROJECT}_mysql-data"
SERVICES = ("mysql", "elasticsearch", "wikibase", "wdqs", "traefik")
STEPS = [
    "check the kept-state marker",
    "clear the write-phase state",
    "stop the stack",
    "remove the graph volumes",
    "set LocalSettings.php aside",
    "start the stack",
    "bootstrap the manifest",
    "publish the queries",
    "run the smoke test",
]
# the docker commands a reset reads with before it changes anything
READS = ["context show", "info", "config", "ps", "volume ls"]
# the id of the Docker engine that runs the stack
ENGINE = "a1b2c3d4-0000-4000-8000-000000001863"
FIRST_SETTINGS = "<?php // the install of the Löbig family archive\n"
# the settings the environment could give: the connection's, and those
# that point docker at another project or engine
SETTINGS = (
    "COMPOSE_PROJECT_NAME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "YIZKR_ENV_FILE",
    "WIKIBASE_URL",
    "WIKIBASE_PUBLIC_HOST",
    "WDQS_PUBLIC_HOST",
    "WDQS_SPARQL_URL",
    "PUBLIC_SCHEME",
    "HTTP_PORT",
    "MW_ADMIN_NAME",
    "MW_ADMIN_PASS",
)


@pytest.fixture(autouse=True)
def _settings_from_files_alone(monkeypatch):
    """The connection settings come from each test's env file, never from
    the environment the tests run in, and no setting there points docker
    at another project or engine."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


# -- the fakes ----------------------------------------------------------------


class FakeWiki:
    """The instance's API as the reset and the record read it: whether it
    answers, the server it says it is, its pages, and its newest revision
    and log entry. Its content is its database's."""

    def __init__(self) -> None:
        self.answering = True
        self.server = URL
        self.pages: set[str] = set()
        self.revision = 0
        self.log = 0
        self.reads: list[dict] = []

    def edit(self, title: str | None = None) -> None:
        self.revision += 1
        if title is not None:
            self.pages.add(title)

    def delete(self, title: str) -> None:
        self.pages.discard(title)
        self.log += 1

    def wipe(self) -> None:
        self.pages.clear()
        self.revision = self.log = 0

    def install(self) -> None:
        self.edit("Main Page")
        self.log += 1  # the admin account

    def api_get(self, params: dict) -> dict:
        self.reads.append(dict(params))
        if not self.answering:
            raise requests.ConnectionError("connection refused")
        if params.get("meta") == "siteinfo":
            return {
                "query": {
                    "general": {
                        "server": self.server,
                        "sitename": "Löbig family archive",
                    }
                }
            }
        if params.get("list") == "allrevisions":
            pages = (
                [
                    {
                        "pageid": 7,
                        "revisions": [{"revid": self.revision}],
                        "ns": 0,
                        "title": "Q7",
                    }
                ]
                if self.revision
                else []
            )
            return {"query": {"allrevisions": pages}}
        if params.get("list") == "logevents":
            entries = [{"logid": self.log}] if self.log else []
            return {"query": {"logevents": entries}}
        if "titles" in params:
            title = params["titles"]
            if title in self.pages:
                page = {"pageid": 12, "ns": 4, "title": title}
                return {"query": {"pages": {"12": page}}}
            page = {"ns": 4, "title": title, "missing": ""}
            return {"query": {"pages": {"-1": page}}}
        raise AssertionError(f"an API call the fake does not know: {params}")


class FakeClient:
    def __init__(self, config: Config, wiki: FakeWiki) -> None:
        self.config = config
        self.wiki = wiki

    def api_get(self, params: dict) -> dict:
        return self.wiki.api_get(params)

    def edit_page(self, title: str, text: str, summary: str) -> None:
        self.wiki.edit(title)


@dataclass(frozen=True)
class FakeContainer:
    """A container of the project: where and from which compose files it
    was made, its service, and its state."""

    where: str
    files: str
    service: str
    state: str = "running"


class FakeDocker:
    """The docker commands the reset runs, over a stack whose compose files
    are in `deploy`: `context show`, `compose config`, `ps`, `volume ls`,
    `compose down`, `volume rm` and `compose up`. `events` records each
    command's verb, in order, with the steps that repopulate. `ps` lists
    only running containers unless asked for all of them, as docker's
    does."""

    def __init__(self, deploy: Path, wiki: FakeWiki, events: list) -> None:
        self.deploy = deploy
        self.wiki = wiki
        self.events = events
        self.context = "default"
        self.engine = ENGINE
        self.project = PROJECT
        self.config_dir = deploy / "config"
        self.declared = {key: {"name": f"{PROJECT}_{key}"} for key in DECLARED}
        self.volumes = {
            f"{PROJECT}_{key}": (PROJECT, key)
            for key in (*DECLARED, "traefik-letsencrypt-data")
        }
        self.volumes["peiltal-photos"] = ("", "")
        files = str((deploy / "docker-compose.yml").resolve())
        self.containers = {
            f"{PROJECT}-{service}-1": FakeContainer(
                str(deploy.resolve()), files, service
            )
            for service in SERVICES
        }
        self.commands: list[list[str]] = []
        # whether the wiki answers once a start has installed it
        self.answers_after_start = True
        self.start_fails = False
        self.installs = 0
        # the verb of a command that fails, as with no engine to answer it
        self.failing: str | None = None
        self.down_fails = False

    def __call__(self, argv, *, capture=True, timeout=None) -> str:
        argv = list(argv)
        self.commands.append(argv)
        verb, rest, files = self._verb(argv)
        self.events.append(verb)
        if verb == self.failing:
            raise DockerFailed(
                f"`docker {verb}` exited with 1: Cannot connect to the Docker "
                "daemon"
            )
        if verb == "context show":
            return f"{self.context}\n"
        if verb == "info":
            return f"{self.engine}\n"
        if verb == "config":
            return json.dumps(self._config())
        if verb == "ps":
            every = "--all" in argv
            return "".join(
                f"{name}\t{c.where}\t{c.files}\t{c.service}\t{c.state}\n"
                for name, c in self.containers.items()
                if every or c.state == "running"
            )
        if verb == "volume ls":
            return "".join(
                f"{name}\t{made_for}\t{made_as}\n"
                for name, (made_for, made_as) in self.volumes.items()
            )
        if verb == "down":
            if self.down_fails:
                raise DockerFailed("`docker compose down` exited with 1")
            self.containers.clear()
            self.wiki.answering = False
            return ""
        if verb == "volume rm":
            for name in rest:
                if name not in self.volumes:
                    raise DockerFailed(f"no such volume: {name}")
                if self.containers:
                    raise DockerFailed(f"volume is in use: {name}")
            for name in rest:
                del self.volumes[name]
                if name == DATABASE:
                    self.wiki.wipe()
            return ""
        if verb == "up":
            return self._up(files)
        raise AssertionError(
            f"a docker command the fake does not know: {argv}"
        )

    def _verb(self, argv: list[str]) -> tuple[str, list[str], list[str]]:
        """(the command's verb, the arguments after it, the compose files
        it names)."""
        if argv[1] == "compose":
            rest, files = argv[2:], []
            while rest and rest[0] in ("--env-file", "-f"):
                if rest[0] == "-f":
                    files.append(rest[1])
                rest = rest[2:]
            return rest[0], rest[1:], files
        if argv[1] in ("volume", "context"):
            return f"{argv[1]} {argv[2]}", argv[3:], []
        return argv[1], argv[2:], []

    def _config(self) -> dict:
        return {
            "name": self.project,
            "services": {
                "wikibase": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.config_dir),
                            "target": "/config",
                        },
                        {
                            "type": "volume",
                            "source": "wikibase-image-data",
                            "target": "/var/www/html/images",
                        },
                    ]
                }
            },
            "volumes": self.declared,
        }

    def _up(self, files: list[str]) -> str:
        if self.start_fails:
            raise DockerFailed(
                "container yizkr-wikibase-wikibase-1 exited (1)"
            )
        made_from = ",".join(str(Path(path).resolve()) for path in files)
        where = str(Path(files[0]).resolve().parent)
        for service in SERVICES:
            self.containers[f"{self.project}-{service}-1"] = FakeContainer(
                where, made_from, service
            )
        fresh = f"{self.project}_mysql-data" not in self.volumes
        for key in DECLARED:
            self.volumes.setdefault(
                f"{self.project}_{key}", (self.project, key)
            )
        settings = self.config_dir / LOCAL_SETTINGS
        if not settings.exists():
            # the image installs MediaWiki afresh, as on a first boot
            self.installs += 1
            settings.write_text(f"<?php // install {self.installs}\n")
            self.wiki.install()
            self.wiki.answering = self.answers_after_start
        else:
            # an install's settings over an empty database: the wiki's
            # update fails, and it never answers
            self.wiki.answering = self.answers_after_start and not fresh
        return ""


class FakeClock:
    """Each reading 1.5 s after the one before."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.5
        return self.now


@dataclass
class Stack:
    """A stack in tmp_path/deploy, the fakes that play it, and the
    directory of the instance's records."""

    root: Path
    deploy: Path
    wiki: FakeWiki
    docker: FakeDocker
    records: Path
    events: list = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    repopulated: list[str] = field(default_factory=list)

    @property
    def settings(self) -> Path:
        return self.deploy / "config" / LOCAL_SETTINGS

    def client(self) -> FakeClient:
        return FakeClient(CONFIG, self.wiki)

    def reset(self, **options) -> InstanceReset:
        def step(name):
            def run(config, log, *timeout):
                assert config is CONFIG
                self.events.append(name)
                self.repopulated.append(name)
                self.wiki.edit()
                return f"{name} done"

            return run

        arguments = {
            "yes": True,
            "lock": contextlib.nullcontext,
            "runner": self.docker,
            "client_factory": lambda config: FakeClient(config, self.wiki),
            "bootstrap": step("bootstrap"),
            "publish": step("publish"),
            "smoke": step("smoke"),
            "state": self.records,
            "clock": FakeClock(),
            "sleep": lambda seconds: None,
            "api_wait": 0,
            "log": self.lines.append,
        }
        arguments.update(options)
        compose = Compose(self.deploy, self.deploy / ".env")
        return InstanceReset(compose, CONFIG, **arguments)

    def writes(self) -> list[str]:
        """The docker commands that change something."""
        return [
            event
            for event in self.events
            if event in ("down", "volume rm", "up")
        ]


@pytest.fixture
def stack_(tmp_path) -> Stack:
    deploy = tmp_path / "deploy"
    (deploy / "config").mkdir(parents=True)
    (deploy / "docker-compose.yml").write_text(f"name: {PROJECT}\n")
    (deploy / ".env").write_text(
        f"WIKIBASE_PUBLIC_HOST={HOST}\nWDQS_PUBLIC_HOST=query.{HOST}\n"
        "MW_ADMIN_NAME=admin\nMW_ADMIN_PASS=Hutmacherei-1863\n"
    )
    (deploy / "config" / LOCAL_SETTINGS).write_text(FIRST_SETTINGS)
    wiki = FakeWiki()
    wiki.install()
    events: list = []
    return Stack(
        tmp_path,
        deploy,
        wiki,
        FakeDocker(deploy, wiki, events),
        tmp_path / "records",
        events,
    )


def standing_key(stack: Stack, key: str = "a3f1") -> None:
    """Record a key, as the scratch write stage records it, and check it
    stands."""
    record_contents(stack.client(), key, "run-1", root=stack.records)
    assert recorded_contents(stack.client(), root=stack.records).key == key


def moved(stack: Stack, name: str, **changes) -> None:
    """Make the container `name` one made in another deploy directory, as
    if started from another checkout."""
    stack.docker.containers[name] = dataclasses.replace(
        stack.docker.containers[name], where="/srv/elsewhere/deploy", **changes
    )


def write_origin(stack: Stack, **changes) -> Path:
    """The last reset's record of where the stack runs from: this stack's,
    but for `changes`. An `engine` of None leaves the engine out, as a
    record made before engines were recorded does."""
    compose = Compose(stack.deploy, stack.deploy / ".env")
    record = {
        **compose.origin(),
        "project": PROJECT,
        "engine": ENGINE,
        **changes,
    }
    path = stack.records / HOST / ORIGIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({k: v for k, v in record.items() if v is not None})
    )
    return path


def write_host_marker(stack: Stack, text: str | None = None) -> Path:
    """The host-side kept-state marker alone, as keeper mode leaves it
    (or `text`, as it could be found)."""
    path = kept_path(CONFIG, stack.records)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {"kept": {"by": "keeper mode", "at": "2026-11-22T10:00:00+00:00"}}
    path.write_text(json.dumps(kept) if text is None else text)
    return path


def tree(root: Path, leave_out: tuple[Path, ...] = ()) -> dict[str, bytes]:
    """Every file under `root`, and its bytes, but those under
    `leave_out`."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(path.is_relative_to(out) for out in leave_out)
    }


# -- the record of the instance ----------------------------------------------


class TestTheRecordsPlace:
    def test_the_state_directory_setting_is_taken(self, monkeypatch, tmp_path):
        monkeypatch.setenv(instance.STATE_ENV, str(tmp_path / "records"))
        assert state_root() == tmp_path / "records"

    def test_a_relative_state_directory_is_refused(self, monkeypatch):
        """Commands run in other directories would keep other records."""
        monkeypatch.setenv(instance.STATE_ENV, "var/instance")
        with pytest.raises(ValueError, match="relative path"):
            state_root()

    def test_by_default_the_users_state_directory(self, monkeypatch, tmp_path):
        monkeypatch.delenv(instance.STATE_ENV)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        expected = tmp_path / ".local" / "state" / "yizkr" / "instances"
        assert state_root() == expected
        # a relative XDG_STATE_HOME is invalid, and ignored
        monkeypatch.setenv("XDG_STATE_HOME", "state")
        assert state_root() == expected
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert state_root() == tmp_path / "xdg" / "yizkr" / "instances"

    @pytest.mark.parametrize(
        ("concept_uri", "name"),
        [
            ("http://wikibase.example", "wikibase.example"),
            ("https://Wikibase.Example", "wikibase.example"),
            ("http://localhost:8080", "localhost_8080"),
        ],
    )
    def test_each_instance_has_a_directory_named_for_its_host(
        self, tmp_path, concept_uri, name
    ):
        config = Config(concept_uri, concept_uri, "", "", None, None, None)
        assert instance.instance_dir(config, tmp_path) == tmp_path / name

    @pytest.mark.parametrize("concept_uri", ["http://", "wikibase.example"])
    def test_a_concept_uri_with_no_host_is_refused(self, concept_uri):
        with pytest.raises(ValueError, match="names no host"):
            instance.instance_name(concept_uri)


class TestTheInstanceContentsRecord:
    def test_a_recorded_key_stands_while_nothing_writes(self, stack_):
        standing_key(stack_)
        stood = recorded_contents(stack_.client(), root=stack_.records)
        assert (stood.key, stood.run_id, stood.why) == ("a3f1", "run-1", "")

    def test_the_record_is_private(self, stack_):
        standing_key(stack_)
        directory = stack_.records / HOST
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    @pytest.mark.parametrize("write", ["an edit", "a deletion"])
    def test_any_other_write_voids_the_record(self, stack_, write):
        """An edit adds a revision, and a deletion a log entry: either
        voids the record, whoever made it, so the next scratch run
        resets first."""
        stack_.wiki.edit("Q7")
        standing_key(stack_)
        if write == "an edit":
            stack_.wiki.edit("Q8")
        else:
            stack_.wiki.delete("Q7")
        voided = recorded_contents(stack_.client(), root=stack_.records)
        assert voided.key is None
        assert voided.run_id == "run-1"
        assert "written to since the key was recorded" in voided.why

    def test_a_cleared_record_says_who_cleared_it(self, stack_):
        standing_key(stack_)
        path = clear_contents(CONFIG, "live tests", root=stack_.records)
        cleared = recorded_contents(stack_.client(), root=stack_.records)
        assert cleared.key is None
        assert cleared.why.startswith("cleared by live tests at ")
        record = json.loads(path.read_text())
        assert record["cleared"]["pid"] == os.getpid()
        assert record["instance"] == URL

    def test_clearing_needs_no_record_to_be_there(self, stack_):
        clear_contents(CONFIG, "yizkr bootstrap", root=stack_.records)
        clear_contents(CONFIG, "yizkr bootstrap", root=stack_.records)
        record = read_record(CONFIG, stack_.records)
        assert record["cleared"]["by"] == "yizkr bootstrap"

    def test_no_record_is_no_key(self, stack_):
        nothing = recorded_contents(stack_.client(), root=stack_.records)
        assert (nothing.key, nothing.why) == (None, "no key is recorded")

    def test_another_instances_record_does_not_stand(self, stack_):
        standing_key(stack_)
        path = instance.contents_path(CONFIG, stack_.records)
        record = json.loads(path.read_text())
        path.write_text(json.dumps(record | {"instance": "http://other"}))
        other = recorded_contents(stack_.client(), root=stack_.records)
        assert other.key is None
        assert "another instance" in other.why

    @pytest.mark.parametrize(
        "text", ["{not json", '["a3f1"]', '{"format": 2, "key": "a3f1"}']
    )
    def test_a_record_this_version_cannot_read_does_not_stand(
        self, stack_, text
    ):
        path = instance.contents_path(CONFIG, stack_.records)
        path.parent.mkdir(parents=True)
        path.write_text(text)
        stood = recorded_contents(stack_.client(), root=stack_.records)
        assert stood.key is None

    def test_a_record_nested_too_deep_does_not_stand(self, stack_):
        """It cannot be read, and says so, rather than raising out of the
        reader (#615)."""
        path = instance.contents_path(CONFIG, stack_.records)
        path.parent.mkdir(parents=True)
        path.write_text("[" * 100_000)
        with pytest.raises(RecursionError):
            read_record(CONFIG, stack_.records)
        stood = recorded_contents(stack_.client(), root=stack_.records)
        assert stood.key is None
        assert stood.why.startswith(f"{path} cannot be read")

    def test_a_record_names_a_key(self, stack_):
        with pytest.raises(ValueError, match="not empty"):
            record_contents(stack_.client(), "", "run-1", root=stack_.records)

    def test_the_watermark_of_an_empty_wiki(self, stack_):
        stack_.wiki.wipe()
        assert watermark(stack_.client()) == {"revision": 0, "log": 0}

    def test_an_answer_with_no_watermark_is_refused(self):
        class Odd:
            def api_get(self, params):
                return {"batchcomplete": ""}

        with pytest.raises(ValueError, match="no newest revision"):
            watermark(Odd())

    def test_the_write_phase_state_is_in_the_instances_record(self, tmp_path):
        assert write_state_dir(CONFIG, tmp_path) == tmp_path / HOST / "write"


class TestTheKeptStateMarker:
    def test_the_marker_is_a_page_on_the_instance(self, stack_):
        assert not carries_kept_marker(stack_.client())
        stack_.wiki.edit(KEPT_STATE_PAGE)
        assert carries_kept_marker(stack_.client())
        stack_.wiki.delete(KEPT_STATE_PAGE)
        assert not carries_kept_marker(stack_.client())

    @pytest.mark.parametrize(
        "answer",
        [
            {"batchcomplete": ""},
            {"query": {"pages": {}}},
            {"query": {"pages": {"-1": {"title": "x", "invalid": ""}}}},
        ],
    )
    def test_an_answer_that_does_not_say_is_refused(self, answer):
        class Odd:
            def api_get(self, params):
                return answer

        with pytest.raises(ValueError):
            carries_kept_marker(Odd())

    def test_the_driver_reads_the_env_files_instance(self, stack_):
        """instance_is_kept is the scratch driver's KEPT_STATE: a callable
        of the env file."""
        seen = []

        def client(config):
            seen.append(config.concept_uri)
            return FakeClient(config, stack_.wiki)

        env = stack_.deploy / ".env"
        records = stack_.records
        assert not instance_is_kept(env, client_factory=client, root=records)
        stack_.wiki.edit(KEPT_STATE_PAGE)
        assert instance_is_kept(env, client_factory=client, root=records)
        assert seen == [URL, URL]

    def test_settings_that_name_no_instance_cannot_tell(self, tmp_path):
        empty = tmp_path / ".env"
        empty.write_text("")
        with pytest.raises(ValueError, match="Missing configuration"):
            instance_is_kept(empty)

    def test_keeper_mode_marks_the_host_and_the_instance(self, stack_):
        assert host_kept_marker(CONFIG, stack_.records) is None
        mark_kept(stack_.client(), "keeper mode", root=stack_.records)
        said = host_kept_marker(CONFIG, stack_.records)
        assert said.startswith(f"{kept_path(CONFIG, stack_.records)}: ")
        assert "marked kept by keeper mode at " in said
        assert carries_kept_marker(stack_.client())
        directory = stack_.records / HOST
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_the_host_side_marker_is_written_first(self, stack_):
        """So a write to the instance that fails still leaves it marked."""

        class Refusing(FakeClient):
            def edit_page(self, title, text, summary):
                raise requests.ConnectionError("the wiki does not answer")

        with pytest.raises(requests.ConnectionError):
            mark_kept(
                Refusing(CONFIG, stack_.wiki),
                "keeper mode",
                root=stack_.records,
            )
        assert host_kept_marker(CONFIG, stack_.records) is not None

    @pytest.mark.parametrize(
        "text", ["", "{not json", '{"kept": "yes"}', "[" * 100_000]
    )
    def test_a_host_marker_that_cannot_be_read_still_marks_it_kept(
        self, stack_, text
    ):
        write_host_marker(stack_, text)
        said = host_kept_marker(CONFIG, stack_.records)
        assert said.endswith("which marks it kept all the same")

    def test_the_driver_takes_the_host_marker_without_asking_the_wiki(
        self, stack_
    ):
        write_host_marker(stack_)

        def client(config):
            raise AssertionError("the wiki is not asked")

        env = stack_.deploy / ".env"
        assert instance_is_kept(
            env, client_factory=client, root=stack_.records
        )


# -- the reset ----------------------------------------------------------------


class TestAReset:
    def test_every_step_runs_in_order_and_is_timed(self, stack_):
        standing_key(stack_)
        report = stack_.reset().run()
        assert [step.name for step in report.steps] == STEPS
        assert stack_.events == [
            *READS,
            *READS,
            "down",
            "volume ls",
            "volume rm",
            "up",
            "bootstrap",
            "publish",
            "smoke",
        ]
        # each step says it starts, and how long it took
        for number, name in enumerate(STEPS, 1):
            assert f"[{number}/9] {name}" in stack_.lines
        timed = [line for line in stack_.lines if line.startswith("[8/9] ")]
        assert timed == [
            "[8/9] publish the queries",
            "[8/9] 1.5 s: publish done",
        ]
        assert all(step.seconds == 1.5 for step in report.steps)
        summary = describe(report)
        assert summary[0] == (
            "The instance is reset and repopulated, in 13.5 s:"
        )
        assert [line.split()[-2] for line in summary[1:10]] == ["1.5"] * 9
        # the start waits for the stack to be healthy
        (started,) = [c for c in stack_.docker.commands if "up" in c]
        assert started[-5:] == [
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "900",
        ]

    def test_progress_is_told_of_each_step_as_it_starts(self, stack_):
        """The scratch run's reset stage tells the run's events of each
        step through `progress` (#561): before the step runs, so a step
        that waits shows as the one in hand."""
        told = []

        def progress(number, steps, name):
            told.append((number, steps, name, list(stack_.repopulated)))

        stack_.reset(progress=progress).run()
        assert [entry[:3] for entry in told] == [
            (number, 9, name) for number, name in enumerate(STEPS, 1)
        ]
        # the bootstrap is told before it runs, not after
        assert told[6][3] == []

    def test_it_removes_the_graph_volumes_and_nothing_else(self, stack_):
        stack_.reset().run()
        removed = [
            c for c in stack_.docker.commands if c[1:3] == ["volume", "rm"]
        ]
        assert [command[3:] for command in removed] == [list(GRAPH)]
        # the stack made them again as it started; the others it kept
        assert f"{PROJECT}_quickstatements-data" in stack_.docker.volumes
        assert f"{PROJECT}_traefik-letsencrypt-data" in stack_.docker.volumes
        assert "peiltal-photos" in stack_.docker.volumes

    def test_the_wiki_is_installed_afresh_and_repopulated(self, stack_):
        stack_.wiki.edit("Q7")
        report = stack_.reset().run()
        assert stack_.docker.installs == 1
        assert stack_.wiki.pages == {"Main Page"}
        assert stack_.repopulated == ["bootstrap", "publish", "smoke"]
        # the settings of the database that is gone are set aside
        aside = report.set_aside / LOCAL_SETTINGS
        assert aside.read_text() == FIRST_SETTINGS
        assert stack_.settings.read_text() == "<?php // install 1\n"
        assert report.set_aside.parent == stack_.records / HOST / "reset"
        assert stat.S_IMODE(report.set_aside.stat().st_mode) == 0o700

    def test_it_clears_the_contents_key(self, stack_):
        standing_key(stack_)
        stack_.reset().run()
        record = read_record(CONFIG, stack_.records)
        assert record["cleared"]["by"] == CLEARED_BY
        stood = recorded_contents(stack_.client(), root=stack_.records)
        assert stood.key is None

    def test_it_sets_the_write_phase_state_aside_and_touches_nothing_else(
        self, stack_
    ):
        """What outlives the instance stays as it was: the LLM cache, the
        run directories and their reports, the corpus, and whatever else
        is kept beside the instance's record."""
        write = write_state_dir(CONFIG, stack_.records)
        write.mkdir(parents=True)
        (write / "records.jsonl").write_text('{"document": "3f2a9c1"}\n')
        var = stack_.root / "var"
        for path, text in {
            var / "llm-cache" / "entries" / "ab.json": "{}",
            var / "scratch" / "run-1" / "report.html": "<p>a report</p>",
            stack_.root / "corpus" / "letter to Ida.txt": "Liebe Peppi",
            stack_.records / HOST / "notes.txt": "kept by hand",
            stack_.records / "other.example" / "contents.json": "{}",
        }.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        cleared = (
            instance.contents_path(CONFIG, stack_.records),
            write,
            stack_.records / HOST / "reset",
            stack_.records / HOST / ORIGIN_FILE,
            stack_.settings,
        )
        before = tree(stack_.root, cleared)
        report = stack_.reset().run()
        assert tree(stack_.root, cleared) == before
        assert not write.exists()
        moved = report.set_aside / "write" / "records.jsonl"
        assert moved.read_text() == '{"document": "3f2a9c1"}\n'

    def test_running_it_again_is_safe(self, stack_):
        first = stack_.reset().run()
        stack_.events.clear()
        second = stack_.reset().run()
        assert [step.name for step in second.steps] == STEPS
        assert stack_.writes() == ["down", "volume rm", "up"]
        assert stack_.docker.installs == 2
        assert second.set_aside != first.set_aside
        assert (second.set_aside / LOCAL_SETTINGS).read_text() == (
            "<?php // install 1\n"
        )

    def test_it_removes_only_the_volumes_there_are(self, stack_):
        """A reset cut short after it removed some is finished by running
        it again."""
        for name in GRAPH[1:]:
            del stack_.docker.volumes[name]
        stack_.reset().run()
        removed = [
            c for c in stack_.docker.commands if c[1:3] == ["volume", "rm"]
        ]
        assert [command[3:] for command in removed] == [[DATABASE]]

    def test_with_no_volume_there_is_nothing_to_remove(self, stack_):
        """A stack that was never started, or is gone: no volume, no
        container, no wiki answering, no settings."""
        for name in GRAPH:
            del stack_.docker.volumes[name]
        stack_.docker.containers.clear()
        stack_.wiki.answering = False
        stack_.settings.unlink()
        report = stack_.reset().run()
        assert "volume rm" not in stack_.events
        assert report.set_aside is None
        assert [step.name for step in report.steps] == STEPS

    def test_with_yes_it_still_says_what_it_removes(self, stack_):
        stack_.reset().run()
        told = "\n".join(stack_.lines)
        assert f"This resets the instance at {URL}" in told
        assert f"compose project {PROJECT}" in told
        for name in GRAPH:
            assert name in told
        assert "confirmed by --yes" in told

    def test_it_records_where_the_stack_runs_from(self, stack_):
        stack_.reset().run()
        record = json.loads((stack_.records / HOST / ORIGIN_FILE).read_text())
        assert record["deploy_dir"] == str(stack_.deploy.resolve())
        assert record["compose_files"] == [
            str((stack_.deploy / "docker-compose.yml").resolve())
        ]
        assert record["project"] == PROJECT
        assert record["engine"] == ENGINE


class TestTheKeptGuard:
    def assert_nothing_removed(self, stack_, record_before):
        assert "down" not in stack_.events
        assert "volume rm" not in stack_.events
        assert set(GRAPH) <= set(stack_.docker.volumes)
        assert stack_.settings.read_text() == FIRST_SETTINGS
        assert read_record(CONFIG, stack_.records) == record_before
        assert not (stack_.records / HOST / "reset").exists()

    def test_a_kept_instance_is_refused(self, stack_):
        stack_.wiki.edit(KEPT_STATE_PAGE)
        standing_key(stack_)
        before = read_record(CONFIG, stack_.records)
        with pytest.raises(ResetRefused, match="is kept: it carries the page"):
            stack_.reset().run()
        assert stack_.events == READS * 2
        self.assert_nothing_removed(stack_, before)
        assert stack_.repopulated == []

    def test_a_stopped_stack_is_started_to_read_the_marker(self, stack_):
        stack_.wiki.answering = False
        stack_.docker.containers.clear()
        stack_.reset().run()
        assert stack_.writes() == ["up", "down", "volume rm", "up"]
        assert any("titles" in read for read in stack_.wiki.reads)

    def test_a_volume_the_start_made_is_removed_too(self, stack_):
        """A start to read the marker makes the volumes that were not
        there; the reset removes them with the rest."""
        search_index = f"{PROJECT}_elasticsearch-data"
        del stack_.docker.volumes[search_index]
        stack_.wiki.answering = False
        stack_.docker.containers.clear()
        stack_.reset().run()
        removed = [
            c for c in stack_.docker.commands if c[1:3] == ["volume", "rm"]
        ]
        assert [command[3:] for command in removed] == [list(GRAPH)]

    def test_a_stopped_kept_instance_is_refused(self, stack_):
        stack_.wiki.edit(KEPT_STATE_PAGE)
        stack_.wiki.answering = False
        stack_.docker.containers.clear()
        before = read_record(CONFIG, stack_.records)
        with pytest.raises(ResetRefused, match="is kept: it carries the page"):
            stack_.reset().run()
        assert stack_.writes() == ["up"]
        self.assert_nothing_removed(stack_, before)

    @pytest.mark.parametrize("why", ["it does not answer", "it cannot start"])
    def test_an_instance_that_cannot_be_read_is_refused(self, stack_, why):
        stack_.wiki.answering = False
        stack_.docker.answers_after_start = False
        stack_.docker.start_fails = why == "it cannot start"
        before = read_record(CONFIG, stack_.records)
        with pytest.raises(ResetRefused, match="cannot tell whether"):
            stack_.reset().run()
        assert stack_.writes() == ["up"]
        self.assert_nothing_removed(stack_, before)

    def test_a_marker_that_cannot_be_read_is_refused(self, stack_):
        class Unsure(FakeClient):
            def api_get(self, params):
                if "titles" in params:
                    return {"batchcomplete": ""}
                return super().api_get(params)

        with pytest.raises(ResetRefused, match="cannot tell whether"):
            stack_.reset(
                client_factory=lambda config: Unsure(config, stack_.wiki)
            ).run()
        assert stack_.events == READS * 2

    def test_another_wiki_answering_is_refused(self, stack_):
        """The marker read must be the instance's that is reset."""
        stack_.wiki.server = "http://wikibase.peiltal.example"
        with pytest.raises(ResetRefused, match="served as"):
            stack_.reset().run()
        assert stack_.events == READS * 2

    def test_with_no_database_volume_there_is_nothing_to_guard(self, stack_):
        """An instance with no database holds no graph: the reset needs no
        answer from its wiki to go on."""
        del stack_.docker.volumes[DATABASE]
        stack_.wiki.answering = False
        stack_.docker.containers.clear()
        report = stack_.reset().run()
        assert not any("titles" in read for read in stack_.wiki.reads)
        assert report.steps[0].note.startswith(
            "no wiki answers and there is no database volume"
        )
        assert stack_.writes() == ["down", "volume rm", "up"]

    def test_the_host_side_marker_holds_with_nothing_else_to_read(
        self, stack_
    ):
        """No database volume here and no wiki answering, as with another
        engine behind the local socket: the host-side marker still says the
        instance is kept, before anything is asked."""
        write_host_marker(stack_)
        del stack_.docker.volumes[DATABASE]
        stack_.wiki.answering = False
        stack_.docker.containers.clear()
        with pytest.raises(ResetRefused, match="host-side kept-state marker"):
            stack_.reset(yes=False, interactive=False).run()
        assert stack_.events == READS
        assert stack_.settings.read_text() == FIRST_SETTINGS

    def test_a_host_marker_made_while_the_reset_waited_is_refused(
        self, stack_
    ):
        @contextlib.contextmanager
        def lock():
            write_host_marker(stack_)
            yield

        with pytest.raises(ResetRefused, match="host-side kept-state marker"):
            stack_.reset(lock=lock).run()
        assert stack_.events == READS * 2
        assert stack_.settings.read_text() == FIRST_SETTINGS

    @pytest.mark.parametrize(
        "lacking", ["the database volume", "a running wiki container"]
    )
    def test_a_wiki_served_from_elsewhere_is_refused(self, stack_, lacking):
        """A wiki answers at the URL, and this engine does not serve it:
        another engine behind the local socket, say. The reset cannot tell
        which instance it would reset, or whether that one is kept."""
        if lacking == "the database volume":
            for name in GRAPH:
                del stack_.docker.volumes[name]
            stack_.docker.containers.clear()
        else:
            name = f"{PROJECT}-wikibase-1"
            stack_.docker.containers[name] = dataclasses.replace(
                stack_.docker.containers[name], state="exited"
            )
        with pytest.raises(ResetRefused, match="served from elsewhere"):
            stack_.reset().run()
        assert stack_.writes() == []
        assert stack_.settings.read_text() == FIRST_SETTINGS


class TestTheOtherGuards:
    def test_a_volume_the_stack_did_not_make_is_refused(self, stack_):
        stack_.docker.volumes[f"{PROJECT}_wdqs-data"] = ("", "")
        with pytest.raises(ResetRefused, match="did not make"):
            stack_.reset().run()
        assert stack_.events == READS

    def test_a_volume_made_as_another_is_refused(self, stack_):
        stack_.docker.volumes[DATABASE] = (PROJECT, "wdqs-data")
        with pytest.raises(ResetRefused, match="did not make"):
            stack_.reset().run()
        assert stack_.events == READS

    def test_a_stack_started_from_another_directory_is_refused(self, stack_):
        """Starting it from here would swap its config directory, and its
        LocalSettings.php, for another checkout's."""
        moved(stack_, f"{PROJECT}-wikibase-1")
        with pytest.raises(ResetRefused, match="made in /srv/elsewhere"):
            stack_.reset().run()
        assert stack_.events == ["context show", "info", "config", "ps"]

    def test_a_stack_started_from_other_compose_files_is_refused(self, stack_):
        name = f"{PROJECT}-traefik-1"
        container = stack_.docker.containers[name]
        overlay = str(stack_.deploy / "docker-compose.localhost.yml")
        stack_.docker.containers[name] = dataclasses.replace(
            container, files=f"{container.files},{overlay}"
        )
        with pytest.raises(ResetRefused, match="--compose-file"):
            stack_.reset().run()

    @pytest.mark.parametrize(
        ("change", "refusal"),
        [
            ("missing", "declare no volume wdqs-data"),
            ("external", "external"),
        ],
    )
    def test_compose_files_without_the_volumes_are_refused(
        self, stack_, change, refusal
    ):
        if change == "missing":
            del stack_.docker.declared["wdqs-data"]
        else:
            stack_.docker.declared["mysql-data"]["external"] = True
        with pytest.raises(ResetRefused, match=refusal):
            stack_.reset().run()
        assert stack_.events == ["context show", "info", "config"]

    def test_a_wiki_with_no_config_directory_is_refused(self, stack_):
        config = stack_.docker._config()
        config["services"]["wikibase"]["volumes"] = []
        stack_.docker._config = lambda: config
        with pytest.raises(ResetRefused, match="mounts no directory"):
            stack_.reset().run()
        assert stack_.events == ["context show", "info", "config"]

    @pytest.mark.parametrize(
        ("where", "line"),
        [
            ("the environment", None),
            ("the env file", "COMPOSE_PROJECT_NAME=archive"),
            ("the env file", "export COMPOSE_PROJECT_NAME=archive"),
            ("the env file", "COMPOSE_PROJECT_NAME: archive"),
            ("the env file", "  export\tCOMPOSE_PROJECT_NAME = archive"),
        ],
        ids=["environment", "assigned", "exported", "colon", "spaced"],
    )
    def test_a_project_name_setting_is_refused(
        self, stack_, monkeypatch, where, line
    ):
        """The setting would make docker compose take the stack for another
        project: here an archive restored as the project `archive`, whose
        volumes are there under the same keys and whose containers are
        down. The reset would remove them. Docker compose reads each of
        these forms in an env file."""
        if line is None:
            monkeypatch.setenv("COMPOSE_PROJECT_NAME", "archive")
        else:
            with (stack_.deploy / ".env").open("a") as env:
                env.write(f"{line}\n")
        stack_.docker.project = "archive"
        stack_.docker.declared = {
            key: {"name": f"archive_{key}"} for key in DECLARED
        }
        stack_.docker.volumes.update(
            {f"archive_{key}": ("archive", key) for key in DECLARED}
        )
        stack_.docker.containers.clear()
        stack_.wiki.answering = False
        env_file = re.escape(str(stack_.deploy / ".env"))
        place = where if where == "the environment" else env_file
        with pytest.raises(
            ResetRefused, match=f"COMPOSE_PROJECT_NAME is set in {place}"
        ):
            stack_.reset().run()
        assert stack_.events == []

    @pytest.mark.parametrize(
        ("text", "assigned"),
        [
            ("COMPOSE_PROJECT_NAME=archive\n", True),
            ("export COMPOSE_PROJECT_NAME=archive\n", True),
            ("COMPOSE_PROJECT_NAME: archive\n", True),
            ("COMPOSE_PROJECT_NAME=\n", True),
            ("WIKIBASE_PUBLIC_HOST=x\nCOMPOSE_PROJECT_NAME=archive\n", True),
            ("# COMPOSE_PROJECT_NAME=archive\n", False),
            ("COMPOSE_PROJECT_NAME_BEFORE=archive\n", False),
            ("NOTE=COMPOSE_PROJECT_NAME=archive\n", False),
            ("", False),
        ],
    )
    def test_an_env_file_assigns_as_docker_compose_reads_it(
        self, text, assigned
    ):
        """Any value, an empty one too, since docker compose's own reading
        of it decides; a comment, a longer name or another setting's value
        assigns nothing."""
        name = "COMPOSE_PROJECT_NAME"
        assert assigned_in(text, name) is assigned

    def test_a_commented_out_setting_is_no_setting(self, stack_):
        with (stack_.deploy / ".env").open("a") as env:
            env.write("# COMPOSE_PROJECT_NAME=archive\n")
        report = stack_.reset().run()
        assert [step.name for step in report.steps] == STEPS

    def test_an_engine_setting_in_the_env_file_is_refused(self, stack_):
        with (stack_.deploy / ".env").open("a") as env:
            env.write("export DOCKER_HOST=ssh://kredfeld.example\n")
        env_file = re.escape(str(stack_.deploy / ".env"))
        with pytest.raises(
            ResetRefused, match=f"DOCKER_HOST is set in {env_file}"
        ):
            stack_.reset().run()
        assert stack_.events == []

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("DOCKER_HOST", "ssh://kredfeld.example"),
            ("DOCKER_CONTEXT", "desktop-linux"),
        ],
    )
    def test_another_docker_engine_is_refused(
        self, stack_, monkeypatch, name, value
    ):
        """The reset moves a file beside the compose files, so it works on
        the local engine only."""
        monkeypatch.setenv(name, value)
        with pytest.raises(
            ResetRefused, match=f"{name} is set in the environment"
        ):
            stack_.reset().run()
        assert stack_.events == []

    def test_another_docker_context_is_refused(self, stack_):
        stack_.docker.context = "desktop-linux"
        with pytest.raises(ResetRefused, match="context 'desktop-linux'"):
            stack_.reset().run()
        assert stack_.events == ["context show"]

    def test_a_volume_made_for_another_project_is_refused(self, stack_):
        """The right key, under another project's label."""
        stack_.docker.volumes[DATABASE] = ("archive", "mysql-data")
        with pytest.raises(ResetRefused, match="did not make"):
            stack_.reset().run()
        assert stack_.events == READS

    def test_a_stopped_container_from_another_directory_is_refused(
        self, stack_
    ):
        moved(stack_, f"{PROJECT}-wikibase-1", state="exited")
        with pytest.raises(ResetRefused, match="made in /srv/elsewhere"):
            stack_.reset().run()

    @pytest.mark.parametrize("verb", READS)
    def test_a_stack_that_cannot_be_read_is_refused(self, stack_, verb):
        stack_.docker.failing = verb
        with pytest.raises(ResetRefused, match="cannot be read"):
            stack_.reset().run()
        assert stack_.writes() == []

    @pytest.mark.parametrize(
        "changed", ["volumes", "config directory", "engine"]
    )
    def test_a_stack_that_changed_while_the_reset_waited_is_refused(
        self, stack_, changed
    ):
        """The reset removes what was confirmed, or nothing."""

        @contextlib.contextmanager
        def lock():
            if changed == "volumes":
                stack_.docker.declared["wdqs-data"] = {"name": "archive_wdqs"}
            elif changed == "config directory":
                stack_.docker.config_dir = stack_.deploy / "config-2"
            else:
                stack_.docker.engine = "e5f6a7b8-0000-4000-8000-000000001954"
            yield

        with pytest.raises(ResetRefused, match="changed while the reset"):
            stack_.reset(lock=lock).run()
        assert stack_.events == READS * 2

    def test_with_no_containers_it_starts_the_stack_where_it_last_ran(
        self, stack_
    ):
        stack_.reset().run()
        # removed by hand: no container, and so no wiki answering
        stack_.docker.containers.clear()
        stack_.wiki.answering = False
        stack_.reset().run()
        assert stack_.docker.installs == 2

    def test_with_no_containers_another_deploy_directory_is_refused(
        self, stack_
    ):
        """With no container to say where the stack runs from, the last
        reset's record says it."""
        elsewhere = "/srv/elsewhere/deploy"
        write_origin(
            stack_,
            deploy_dir=elsewhere,
            compose_files=[f"{elsewhere}/docker-compose.yml"],
        )
        stack_.docker.containers.clear()
        with pytest.raises(ResetRefused, match="has no containers to say"):
            stack_.reset().run()
        assert stack_.events == READS

    def test_with_no_containers_another_project_is_refused(self, stack_):
        write_origin(stack_, project="archive")
        stack_.docker.containers.clear()
        with pytest.raises(ResetRefused, match="as another project"):
            stack_.reset().run()
        assert stack_.writes() == []

    def test_another_engine_behind_the_socket_is_refused(self, stack_):
        """After a restart, another engine can answer behind the local
        socket, with none of the stack's containers or volumes, while the
        stack's own engine, and so its wiki, is down. Nothing here tells a
        kept instance from another, but the last reset's record names the
        engine that runs the stack."""
        write_origin(stack_)
        stack_.docker.engine = "e5f6a7b8-0000-4000-8000-000000001954"
        for name in GRAPH:
            del stack_.docker.volumes[name]
        stack_.docker.containers.clear()
        stack_.wiki.answering = False
        with pytest.raises(ResetRefused, match="another Docker engine"):
            stack_.reset().run()
        assert stack_.writes() == []
        assert stack_.settings.read_text() == FIRST_SETTINGS

    def test_a_record_that_names_no_engine_leaves_it_unknown(self, stack_):
        """As a record made before engines were recorded: only another
        engine than one it names is refused."""
        write_origin(stack_, engine=None)
        stack_.docker.containers.clear()
        stack_.wiki.answering = False
        stack_.reset().run()
        record = json.loads((stack_.records / HOST / ORIGIN_FILE).read_text())
        assert record["engine"] == ENGINE

    def test_an_origin_changed_while_the_reset_waited_is_refused(self, stack_):
        """With no container, the record is read again under the lock."""
        write_origin(stack_)
        stack_.docker.containers.clear()
        stack_.wiki.answering = False

        @contextlib.contextmanager
        def lock():
            write_origin(stack_, deploy_dir="/srv/elsewhere/deploy")
            yield

        with pytest.raises(ResetRefused, match="has no containers to say"):
            stack_.reset(lock=lock).run()
        assert stack_.events == READS * 2

    def test_the_origin_is_recorded_before_the_stack_stops(self, stack_):
        """A stop that fails part way can leave no container to say where
        the stack runs from."""
        stack_.docker.down_fails = True
        with pytest.raises(ResetFailed, match="stop the stack failed"):
            stack_.reset().run()
        record = json.loads((stack_.records / HOST / ORIGIN_FILE).read_text())
        assert record["deploy_dir"] == str(stack_.deploy.resolve())

    def test_an_origin_record_that_cannot_be_read_is_refused(self, stack_):
        record = stack_.records / HOST / ORIGIN_FILE
        record.parent.mkdir(parents=True)
        record.write_text("{not json")
        stack_.docker.containers.clear()
        with pytest.raises(ResetRefused, match="cannot be read") as refused:
            stack_.reset().run()
        assert "Remove it by hand" in str(refused.value)
        assert stack_.writes() == []


class TestConfirmation:
    def test_without_a_terminal_it_needs_yes(self, stack_):
        with pytest.raises(ResetRefused, match="--yes"):
            stack_.reset(yes=False, interactive=False).run()
        assert stack_.events == READS

    def test_at_a_terminal_it_asks_for_the_word(self, stack_):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return "reset\n"

        stack_.reset(yes=False, interactive=True, ask=ask).run()
        assert asked == ['Type "reset" to reset it: ']
        told = "\n".join(stack_.lines)
        assert f"This resets the instance at {URL}" in told
        assert f"{DATABASE} (database)" in told
        assert "it takes no backup" in told

    @pytest.mark.parametrize("answer", ["y", "yes", "", None])
    def test_any_other_answer_is_no(self, stack_, answer):
        def ask(prompt):
            if answer is None:
                raise EOFError
            return answer

        with pytest.raises(ResetRefused, match="not confirmed"):
            stack_.reset(yes=False, interactive=True, ask=ask).run()
        assert stack_.events == READS


class TestTheLiveStackLock:
    @pytest.fixture
    def lock_file(self, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "cache" / "live-stack.lock"
        monkeypatch.setenv(live_lock.LOCK_ENV, str(path))
        monkeypatch.setenv(live_lock.TIMEOUT_ENV, "0")
        monkeypatch.delenv(live_lock.SHARED_ENV, raising=False)
        return path

    def test_a_reset_waits_its_turn_and_changes_nothing(
        self, stack_, lock_file
    ):
        standing_key(stack_)
        before = read_record(CONFIG, stack_.records)
        with (
            live_lock.hold(lock_file, timeout=0),
            pytest.raises(ResetRefused, match="live-stack lock"),
        ):
            stack_.reset(lock=None).run()
        assert stack_.events == READS
        assert read_record(CONFIG, stack_.records) == before

    def test_the_reset_holds_the_lock_while_it_runs(self, stack_, lock_file):
        held = []

        def bootstrap(config, log):
            descriptor = os.open(lock_file, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(json.loads(lock_file.read_text())["pid"])
            finally:
                os.close(descriptor)
            return ""

        stack_.reset(lock=None, bootstrap=bootstrap).run()
        assert held == [os.getpid()]
        # and lets it go at the end
        with live_lock.hold(lock_file, timeout=0):
            pass

    def test_the_lock_is_held_before_the_marker_is_read(self, stack_):
        """So no live session writes between the read and the removal."""
        order = []

        @contextlib.contextmanager
        def lock():
            order.append("lock")
            yield

        stack_.wiki.api_get = _recording(stack_.wiki.api_get, order)
        stack_.reset(lock=lock).run()
        assert order.index("lock") < order.index("read")

    def test_the_stack_is_read_again_once_the_lock_is_held(self, stack_):
        """The reset can wait long for the lock, and the stack can change
        meanwhile: here it is started again from another checkout."""

        @contextlib.contextmanager
        def lock():
            moved(stack_, f"{PROJECT}-wikibase-1")
            yield

        with pytest.raises(ResetRefused, match="made in /srv/elsewhere"):
            stack_.reset(lock=lock).run()
        assert stack_.events == [
            *READS,
            "context show",
            "info",
            "config",
            "ps",
        ]
        assert stack_.writes() == []


def _recording(api_get, order):
    def read(params):
        order.append("read")
        return api_get(params)

    return read


class TestFailures:
    def test_a_failed_step_says_which_and_how_to_finish(self, stack_):
        def smoke(config, log, timeout):
            raise TimeoutError("the item never reached the query service")

        reset = stack_.reset(smoke=smoke)
        with pytest.raises(ResetFailed) as failed:
            reset.run()
        assert str(failed.value).startswith(
            "run the smoke test failed after 1.5 s (TimeoutError: the item "
            "never reached the query service)"
        )
        assert "run `yizkr scratch reset` again" in str(failed.value)
        assert len(reset.report.steps) == 8

    def test_an_interrupted_step_says_so(self, stack_):
        def bootstrap(config, log):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            stack_.reset(bootstrap=bootstrap).run()
        assert stack_.lines[-1] == (
            "[7/9] bootstrap the manifest: interrupted after 1.5 s"
        )

    def test_a_wiki_that_never_answers_after_the_start_fails_it(self, stack_):
        stack_.docker.answers_after_start = False
        with pytest.raises(ResetFailed, match="start the stack failed"):
            stack_.reset().run()


class TestTheStack:
    def test_the_env_files_directory_is_the_deploy_directory(self, stack_):
        compose, config = stack(stack_.deploy / ".env")
        assert compose.deploy_dir == stack_.deploy.resolve()
        assert compose.env_file == (stack_.deploy / ".env").resolve()
        assert config.concept_uri == URL
        assert compose.command("down") == [
            "docker",
            "compose",
            "--env-file",
            str(compose.env_file),
            "-f",
            str(stack_.deploy.resolve() / "docker-compose.yml"),
            "down",
        ]

    def test_no_env_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ResetRefused, match="no connection settings"):
            stack()

    def test_a_deploy_directory_with_no_compose_file_is_refused(
        self, stack_, tmp_path
    ):
        with pytest.raises(ResetRefused, match="--deploy-dir"):
            stack(stack_.deploy / ".env", deploy_dir=tmp_path)

    def test_settings_the_reset_needs_are_asked_for(self, stack_):
        (stack_.deploy / ".env").write_text(f"WIKIBASE_PUBLIC_HOST={HOST}\n")
        with pytest.raises(ResetRefused, match="MW_ADMIN_PASS"):
            stack(stack_.deploy / ".env")

    def test_the_reset_loads_the_checkouts_lock(self):
        from yizkr.reset import LOCK_MODULE, checkout_root

        assert (checkout_root() / LOCK_MODULE).is_file()

    @pytest.mark.parametrize(
        ("seconds", "shown"),
        [
            (0.04, "0.0 s"),
            (12.25, "12.2 s"),
            (59.9, "59.9 s"),
            (372, "6 min 12 s"),
        ],
    )
    def test_times_are_shown_in_seconds_or_minutes(self, seconds, shown):
        assert elapsed(seconds) == shown


class TestTheCommands:
    def test_the_reset_command_prints_each_steps_time(
        self, stack_, monkeypatch, capsys
    ):
        report = stack_.reset().run()
        taken = {}

        def reset_instance(**options):
            taken.update(options)
            return report

        monkeypatch.setattr("yizkr.reset.reset_instance", reset_instance)
        args = cli.build_parser().parse_args(
            ["scratch", "reset", "--yes", "--compose-file", "a.yml"]
        )
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "The instance is reset and repopulated, in 13.5 s:" in out
        assert taken["yes"] is True
        assert taken["compose_files"] == ["a.yml"]
        assert taken["start_timeout"] == 900
        assert taken["smoke_timeout"] == 300

    @pytest.mark.parametrize(
        ("raised", "status", "said"),
        [
            (ResetRefused("the instance is kept"), 1, "refused"),
            (ResetFailed("start the stack failed"), 1, "failed"),
            (KeyboardInterrupt(), 130, "again finishes the reset"),
        ],
    )
    def test_a_refused_or_failed_reset_says_so(
        self, monkeypatch, capsys, raised, status, said
    ):
        def reset_instance(**options):
            raise raised

        monkeypatch.setattr("yizkr.reset.reset_instance", reset_instance)
        args = cli.build_parser().parse_args(["scratch", "reset"])
        assert args.func(args) == status
        assert said in capsys.readouterr().err

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_bootstrap_outside_a_reset_clears_the_key(
        self, stack_, monkeypatch, dry_run
    ):
        """It writes, and the scratch write stage does not make its writes;
        a dry run writes nothing, and clears nothing."""
        import yizkr.model.bootstrap

        monkeypatch.setenv(instance.STATE_ENV, str(stack_.records))
        standing_key(stack_)
        applied = []
        monkeypatch.setattr(
            yizkr.model.bootstrap,
            "bootstrap_instance",
            lambda client, manifest, dry_run: applied.append(dry_run),
        )
        monkeypatch.setattr(cli, "WikiClient", lambda config: None)
        argv = ["--env-file", str(stack_.deploy / ".env"), "bootstrap"]
        args = cli.build_parser().parse_args(
            argv + (["--dry-run"] if dry_run else [])
        )
        assert args.func(args) == 0
        assert applied == [dry_run]
        stood = recorded_contents(stack_.client(), root=stack_.records)
        if dry_run:
            assert stood.key == "a3f1"
        else:
            assert stood.why.startswith("cleared by yizkr bootstrap")

    def test_a_bootstrap_that_cannot_clear_the_key_writes_nothing(
        self, stack_, monkeypatch
    ):
        import yizkr.model.bootstrap

        monkeypatch.setenv(instance.STATE_ENV, "var/instance-records")
        applied = []
        monkeypatch.setattr(
            yizkr.model.bootstrap,
            "bootstrap_instance",
            lambda client, manifest, dry_run: applied.append(dry_run),
        )
        monkeypatch.setattr(cli, "WikiClient", lambda config: None)
        args = cli.build_parser().parse_args(
            ["--env-file", str(stack_.deploy / ".env"), "bootstrap"]
        )
        with pytest.raises(SystemExit, match="writes nothing"):
            args.func(args)
        assert applied == []
