"""The CLI's exit statuses (yizkr.core.exits): a refusal, a pause and a
failure each end with their message alone and a status of their own, not
a traceback (#256)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.identity import mapped
from tests.unit.test_faces import depicted, portrait_faces, wiki_with_portrait
from tests.unit.test_pipeline import (
    MEDIA_SHA1,
    OTHER_SHA1,
    FakeWiki,
    _citing,
)
from yizkr import cli
from yizkr.core.exits import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STOPPED,
)
from yizkr.pipeline import scratch
from yizkr.pipeline.llm import (
    CliConfigEnvironment,
    CliHomeRelative,
    CliToolsMismatch,
    CliVersionMismatch,
    ModelUnsupported,
    PassPaused,
    UsageLimitReached,
)
from yizkr.pipeline.llmcache import CacheInUse, CacheRefused
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.rundir import RunSettingsConflict

# one of each pause the engine and its cache raise, each with its message
PAUSES = [
    CliVersionMismatch("the claude CLI is 2.1.0, not 2.0.9, the pinned one"),
    CliConfigEnvironment("the claude CLI's config sets env: remove it"),
    CliToolsMismatch("a call offered the model the tool Bash"),
    CliHomeRelative("HOME is a relative path: home/archivist"),
    ModelUnsupported(
        "the claude CLI does not support claude-example-1",
        model="claude-example-1",
    ),
    UsageLimitReached("the five-hour window is used up", reason="usage"),
    CacheRefused("var/llm-cache cannot be the LLM cache: a git work tree"),
    CacheInUse(Path("var/llm-cache"), {"pid": 4711}),
]


def _subclasses(cls: type) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found |= {sub, *_subclasses(sub)}
    return found


def test_the_statuses_are_distinct():
    statuses = [EXIT_OK, EXIT_FAILED, EXIT_STOPPED, EXIT_REFUSED]
    assert len(set([*statuses, EXIT_INTERRUPTED])) == 5


def test_the_scratch_commands_use_the_same_statuses():
    assert (
        scratch.EXIT_OK,
        scratch.EXIT_FAILED,
        scratch.EXIT_STOPPED,
        scratch.EXIT_INTERRUPTED,
    ) == (EXIT_OK, EXIT_FAILED, EXIT_STOPPED, EXIT_INTERRUPTED)


def test_every_pause_is_tested():
    # a test-only subclass defined elsewhere is not the engine's
    ours = {
        cls
        for cls in _subclasses(PassPaused)
        if cls.__module__.startswith("yizkr.")
    }
    assert ours == {type(pause) for pause in PAUSES}


def test_the_module_docstring_documents_the_statuses():
    doc = cli.__doc__
    for status in (EXIT_FAILED, EXIT_STOPPED, EXIT_REFUSED, EXIT_INTERRUPTED):
        assert f"\n  {status} " in doc


class TestUsage:
    def test_a_usage_error_is_a_refusal_not_a_pause(self, capsys):
        # argparse's own status, 2, is a pause's
        assert cli.main(["ingest", "write"]) == EXIT_REFUSED
        err = capsys.readouterr().err
        assert "the following arguments are required: --batch" in err

    def test_help_exits_0(self, capsys):
        with pytest.raises(SystemExit) as raised:
            cli.main(["--help"])
        assert raised.value.code == 0
        assert "Exit status" in capsys.readouterr().out


def _no_traceback(err: str) -> None:
    assert "Traceback" not in err


class TestIngestWrite:
    def test_unapproved_depicts_is_refused_in_its_message(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = wiki_with_portrait()
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        batch = tmp_path / "faces-batch.json"
        batch.write_text(portrait_faces().model_dump_json())

        status = cli.main(["ingest", "write", "--batch", str(batch)])

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err.startswith("2 depicts claim(s) carry no per-row human")
        assert err.endswith("Nothing was written.\n")
        assert err.count("\n") == 1
        assert depicted(wiki) == {}

    def test_an_unregistered_artifact_is_refused_in_its_message(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        claims = _citing(MEDIA_SHA1, OTHER_SHA1)
        batch = tmp_path / "batch.json"
        batch.write_text(claims.model_dump_json())
        identity = tmp_path / "identity-map.json"
        identity.write_text(json.dumps(mapped(claims).data))

        status = cli.main(
            [
                *("ingest", "write", "--batch", str(batch)),
                *("--identity-map", str(identity)),
            ]
        )

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err == (
            f"1 cited artifact(s) not registered on this instance: no item "
            f"carries media SHA-1 {OTHER_SHA1[:12]}. Register the media "
            f"before writing claims that cite it.\n"
        )
        assert wiki.wbi.item.writes == 0


class TestIngestRun:
    @staticmethod
    def configure(tmp_path, monkeypatch) -> None:
        """An empty never-ingest list, and no corpus folder configured."""
        from yizkr.core.corpus import CORPUS_DIR_VAR, EXCLUDE_FILE_VAR

        listed = tmp_path / "exclude.txt"
        listed.write_text("")
        env_file = tmp_path / "test.env"
        env_file.write_text(f"{EXCLUDE_FILE_VAR}={listed}\n")
        monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
        monkeypatch.delenv(EXCLUDE_FILE_VAR, raising=False)
        monkeypatch.delenv(CORPUS_DIR_VAR, raising=False)

    def run(self, tmp_path, monkeypatch, step) -> int:
        """`ingest run` on an empty folder, its run stubbed by `step`."""
        self.configure(tmp_path, monkeypatch)
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        monkeypatch.setattr(Orchestrator, "run", lambda self: step())
        return cli.main(
            [
                *("ingest", "run", str(folder)),
                *("--state", str(tmp_path / "state"), "--no-cache"),
            ]
        )

    @pytest.mark.parametrize(
        "pause", PAUSES, ids=[type(p).__name__ for p in PAUSES]
    )
    def test_a_pause_stops_with_its_message_alone(
        self, tmp_path, monkeypatch, capsys, pause
    ):
        def step():
            raise pause

        assert self.run(tmp_path, monkeypatch, step) == EXIT_STOPPED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == f"{pause}\n"

    def test_other_settings_are_refused_with_their_message(
        self, tmp_path, monkeypatch, capsys
    ):
        conflict = RunSettingsConflict(
            tmp_path / "state",
            [("engine.cli_version", "2.0.9", "2.1.0")],
        )

        def step():
            raise conflict

        assert self.run(tmp_path, monkeypatch, step) == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == f"{conflict}\n"

    def test_ctrl_c_is_interrupted(self, tmp_path, monkeypatch, capsys):
        def step():
            raise KeyboardInterrupt

        assert self.run(tmp_path, monkeypatch, step) == EXIT_INTERRUPTED
        assert capsys.readouterr().err == "interrupted\n"

    @pytest.mark.parametrize(
        "status", [EXIT_OK, EXIT_FAILED, EXIT_STOPPED], ids=str
    )
    def test_the_runs_own_status_is_its_exit_status(
        self, tmp_path, monkeypatch, status
    ):
        assert self.run(tmp_path, monkeypatch, lambda: status) == status

    def test_its_own_refusal_is_a_refusal(self, tmp_path, monkeypatch, capsys):
        self.configure(tmp_path, monkeypatch)
        missing = tmp_path / "no-such-folder"

        status = cli.main(
            ["ingest", "run", str(missing), "--state", str(tmp_path / "s")]
        )

        assert status == EXIT_REFUSED
        assert capsys.readouterr().err == f"{missing} is not a directory\n"

    def test_an_unanticipated_error_is_a_failure(self, tmp_path, monkeypatch):
        def step():
            raise RuntimeError("an unanticipated error")

        # Python prints its traceback and exits 1, EXIT_FAILED
        with pytest.raises(RuntimeError):
            self.run(tmp_path, monkeypatch, step)


class TestAnotherCommand:
    """main is the backstop of every command, not of `ingest run` alone."""

    def test_a_pause_from_a_single_step_stops(self, monkeypatch, capsys):
        pause = CacheInUse(Path("var/llm-cache"), {"pid": 4711})

        def cmd_extract(args):
            raise pause

        monkeypatch.setattr(cli, "cmd_extract", cmd_extract)
        status = cli.main(
            ["ingest", "extract", "--transcription", "page.json"]
        )
        assert status == EXIT_STOPPED
        assert capsys.readouterr().err == f"{pause}\n"
