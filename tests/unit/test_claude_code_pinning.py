"""Offline tests for the pinned, isolated claude-code engine (#133).

A fake ``claude`` executable stands in for the CLI. It answers
``--version``, stores each session's transcript under a temporary
$CLAUDE_CONFIG_DIR the way the real CLI does, refuses to fork a session
whose transcript is gone, and prints a stream-json event stream whose
init event reports the CLI version.
"""

from __future__ import annotations

import gc
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import llm
from yizkr.pipeline.llm import (
    ClaudeCodeClient,
    CliVersionMismatch,
    PassPaused,
    make_client,
    pinned_cli_version,
    release_transcripts,
    transcript_destination,
)
from yizkr.pipeline.orchestrate import FAILED, REGISTERED, Orchestrator

PINNED = "2.1.237"
PROJECT = "-tmp-yizkr-claude-code"


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeCli:
    """Stands in for the claude executable (subprocess.run)."""

    def __init__(self, projects: Path) -> None:
        self.projects = projects
        self.installed = PINNED  # what `claude --version` prints
        self.reports: str | None = None  # init event version, if not that
        self.returncode = 0
        self.timeout = False
        self.probes: list[dict] = []  # kwargs of each --version probe
        self.calls: list[tuple[list[str], dict]] = []

    def transcripts(self) -> set[str]:
        return {path.name for path in self.projects.glob("*/*.jsonl")}

    def __call__(self, command, **kwargs):
        if command[1:] == ["--version"]:
            self.probes.append(kwargs)
            return Completed(stdout=f"{self.installed} (Claude Code)\n")
        self.calls.append((command, kwargs))
        if "--resume" in command:
            parent = command[command.index("--resume") + 1]
            if not list(self.projects.glob(f"*/{parent}.jsonl")):
                return Completed(
                    stderr=f"No conversation found with session ID: {parent}",
                    returncode=1,
                )
            session = str(uuid.uuid4())
        else:
            session = command[command.index("--session-id") + 1]
        project = self.projects / PROJECT
        (project / session / "tool-results").mkdir(parents=True)
        (project / f"{session}.jsonl").write_text('{"type": "user"}\n')
        version = self.reports or self.installed
        events = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "claude_code_version": version,
            },
            {
                "type": "assistant",
                "session_id": session,
                "message": {"content": [{"type": "text", "text": "answer"}]},
            },
            {"type": "result", "session_id": session, "result": "answer"},
        ]
        stream = "\n".join(json.dumps(event) for event in events)
        if self.timeout:
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output=stream.encode()
            )
        return Completed(
            stdout=stream,
            stderr="boom" if self.returncode else "",
            returncode=self.returncode,
        )


@pytest.fixture
def cli(monkeypatch, tmp_path) -> FakeCli:
    config = tmp_path / "claude-config"
    fake = FakeCli(config / "projects")
    monkeypatch.setattr(llm.subprocess, "run", fake)
    # a fresh pass: nothing pinned or paused by an earlier test
    monkeypatch.setattr(llm, "_PIN", llm._CliPin())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    for name in ("YIZKR_LLM_MODEL", "YIZKR_LLM_EFFORT", llm.TRANSCRIPTS_ENV):
        monkeypatch.delenv(name, raising=False)
    return fake


class TestIsolation:
    def test_calls_load_none_of_the_owners_configuration(self, cli):
        ClaudeCodeClient().complete("transcribe")
        command, kwargs = cli.calls[0]
        assert command[command.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in command
        assert "--mcp-config" not in command
        assert "--disable-slash-commands" in command
        # forks resume stored sessions, so persistence stays on
        assert "--no-session-persistence" not in command
        assert command[command.index("--allowedTools") + 1] == "Read"

    def test_calls_run_in_a_dedicated_directory(self, cli, tmp_path):
        ClaudeCodeClient().complete("transcribe")
        cwd = Path(cli.calls[0][1]["cwd"])
        assert cwd == tmp_path / "tmp" / ClaudeCodeClient.WORKDIR_NAME
        assert cwd.is_dir() and not any(cwd.iterdir())

    def test_environment_is_cleared_and_pinned(self, cli, monkeypatch):
        # what a Claude Code session exports to the commands it runs
        inherited = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": str(uuid.uuid4()),
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_EFFORT": "xhigh",
            "ANTHROPIC_API_KEY": "sk-should-not-leak",
            "ANTHROPIC_MODEL": "some-other-model",
            "DISABLE_AUTOUPDATER": "0",
        }
        for name, value in inherited.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
        ClaudeCodeClient().complete("transcribe")
        env = cli.calls[0][1]["env"]
        for name in inherited:
            if name != "DISABLE_AUTOUPDATER":
                assert name not in env
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["DISABLE_ERROR_REPORTING"] == "1"
        assert env["DISABLE_TELEMETRY"] == "1"
        # the login's location and a subscription token are kept
        assert env["CLAUDE_CONFIG_DIR"].endswith("claude-config")
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-token"
        assert "PATH" in env
        # the version probe runs under the same environment
        assert cli.probes[0]["env"]["DISABLE_AUTOUPDATER"] == "1"
        assert "CLAUDECODE" not in cli.probes[0]["env"]


class TestEffectiveConfig:
    def test_lists_what_shapes_a_call(self, cli, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
        client = ClaudeCodeClient(model="claude-sonnet-5", effort="low")
        config = client.effective_config
        assert config["engine"] == "claude-code"
        assert config["cli_version"] == PINNED
        arguments = config["arguments"]
        assert arguments[0] == "-p"  # the executable's path is left out
        assert arguments[arguments.index("--model") + 1] == "claude-sonnet-5"
        assert arguments[arguments.index("--effort") + 1] == "low"
        assert "--session-id" not in arguments
        assert config["working_directory"] == str(client.workdir)
        assert config["environment"]["set"]["DISABLE_AUTOUPDATER"] == "1"
        # names of kept variables only, never their values
        assert "subscription-token" not in json.dumps(config)

    def test_hash_is_stable_and_follows_the_config(self, cli):
        low = ClaudeCodeClient(effort="low")
        assert len(low.config_hash) == 64
        assert low.config_hash == ClaudeCodeClient(effort="low").config_hash
        assert low.config_hash != ClaudeCodeClient(effort="high").config_hash
        assert (
            low.config_hash
            != ClaudeCodeClient(
                effort="low", model="claude-opus-5"
            ).config_hash
        )


class TestVersionPin:
    def test_first_client_pins_the_installed_version(self, cli):
        assert pinned_cli_version() is None
        client = ClaudeCodeClient(model="claude-sonnet-5")
        assert pinned_cli_version() == PINNED
        assert client.cli_version == PINNED
        assert (
            client.model_identifier == f"claude-code/{PINNED}:claude-sonnet-5"
        )

    def test_update_between_clients_pauses_the_pass(self, cli):
        first = ClaudeCodeClient()
        cli.installed = "2.1.240"
        with pytest.raises(CliVersionMismatch, match="2.1.240"):
            make_client("claude-code")
        # latched: nothing starts or runs in this pass any more, even if
        # the old version comes back
        cli.installed = PINNED
        with pytest.raises(CliVersionMismatch):
            ClaudeCodeClient()
        with pytest.raises(CliVersionMismatch):
            first.complete("transcribe")
        assert cli.calls == []

    def test_call_reporting_another_version_pauses_the_pass(self, cli):
        client = ClaudeCodeClient()
        client.complete("first")
        cli.reports = "2.1.240"
        # the output of an unpinned CLI is discarded, not returned
        with pytest.raises(CliVersionMismatch, match="reported by a call"):
            client.complete("second")
        cli.reports = None
        with pytest.raises(CliVersionMismatch):
            client.complete("third")
        assert len(cli.calls) == 2  # the third call never reached the CLI

    def test_required_version_is_enforced(self, cli):
        with pytest.raises(CliVersionMismatch, match="2.1.211"):
            ClaudeCodeClient(cli_version="2.1.211")

    def test_required_version_pins_when_installed(self, cli):
        client = make_client("claude-code", cli_version=PINNED)
        assert client.cli_version == pinned_cli_version() == PINNED

    def test_pause_escapes_exception_handlers(self, cli):
        """Per-document and per-chunk isolation catches Exception; a pause
        must get through it rather than become a document failure."""
        assert issubclass(CliVersionMismatch, PassPaused)
        assert not issubclass(PassPaused, Exception)
        client = ClaudeCodeClient()
        cli.reports = "2.1.240"
        with pytest.raises(PassPaused):
            try:
                client.complete("transcribe")
            except Exception:  # what isolation handlers catch
                pytest.fail("a pause was caught as an ordinary failure")

    def test_mismatch_pauses_an_orchestrated_pass(self, cli, tmp_path):
        """The orchestrator's contract: a pause stops the pass, and the
        document it stopped in is not marked failed."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a-letter.txt").write_text("A letter.")
        (folder / "b-letter.txt").write_text("Another letter.")

        class Pass(Orchestrator):
            registered: list[str] = []

            def _step_register(self, doc):
                self.registered.append(doc.source.name)
                return "Q100"

            def _step_batch(self, doc):
                cli.installed = "2.1.240"  # an update lands mid-pass
                make_client("claude-code")
                raise AssertionError("unreachable")

        ClaudeCodeClient()  # the pass pinned its version at the start
        orchestrator = Pass(
            folder,
            tmp_path / "state",
            exclusions=ExclusionList(),
            log=lambda *a, **k: None,
        )
        with pytest.raises(CliVersionMismatch):
            orchestrator.run()
        assert Pass.registered == ["a-letter.txt"]
        state = json.loads(
            (tmp_path / "state" / "a-letter" / "state.json").read_text()
        )
        assert state["status"] == REGISTERED != FAILED


class TestTranscripts:
    def test_one_shot_transcript_is_deleted_by_default(self, cli):
        ClaudeCodeClient().complete("transcribe")
        assert cli.transcripts() == set()
        assert not any((cli.projects / PROJECT).iterdir())

    def test_one_shot_transcript_moves_into_the_run_record(
        self, cli, tmp_path
    ):
        record = tmp_path / "run" / "transcripts"
        ClaudeCodeClient(transcripts=record).complete("transcribe")
        (moved,) = record.glob("*.jsonl")
        assert (record / moved.stem / "tool-results").is_dir()
        assert cli.transcripts() == set()

    def test_environment_names_the_run_record(
        self, cli, tmp_path, monkeypatch
    ):
        record = tmp_path / "record"
        monkeypatch.setenv(llm.TRANSCRIPTS_ENV, str(record))
        make_client("claude-code").complete("transcribe")
        assert len(list(record.glob("*.jsonl"))) == 1

    def test_conversation_stays_until_closed(self, cli, tmp_path):
        record = tmp_path / "record"
        client = ClaudeCodeClient(transcripts=record)
        _, handle = client.open_conversation("list the entities")
        assert cli.transcripts() == {f"{handle}.jsonl"}
        assert client.fork(handle, "chunk 1") == "answer"
        assert client.fork(handle, "chunk 2") == "answer"
        # each fork's own transcript is released as it returns
        assert cli.transcripts() == {f"{handle}.jsonl"}
        assert len(list(record.glob("*.jsonl"))) == 2
        client.close_conversation(handle)
        assert cli.transcripts() == set()
        assert (record / f"{handle}.jsonl").exists()

    def test_close_releases_open_conversations(self, cli):
        with ClaudeCodeClient() as client:
            client.open_conversation("list the entities")
            client.open_conversation("list the entities again")
            assert len(cli.transcripts()) == 2
        assert cli.transcripts() == set()

    def test_garbage_collection_releases_open_conversations(self, cli):
        client = ClaudeCodeClient()
        client.open_conversation("list the entities")
        assert len(cli.transcripts()) == 1
        del client
        gc.collect()
        assert cli.transcripts() == set()

    def test_failed_call_releases_its_transcript(self, cli):
        client = ClaudeCodeClient()
        cli.returncode = 1
        with pytest.raises(RuntimeError, match="boom"):
            client.complete("transcribe")
        with pytest.raises(RuntimeError):
            client.open_conversation("list the entities")
        assert cli.transcripts() == set()

    def test_timed_out_fork_releases_its_transcript(self, cli):
        client = ClaudeCodeClient()
        _, handle = client.open_conversation("list the entities")
        cli.timeout = True
        with pytest.raises(subprocess.TimeoutExpired):
            client.fork(handle, "chunk 1")
        # the fork's id came from its partial stream; the parent stays
        assert cli.transcripts() == {f"{handle}.jsonl"}

    def test_paused_call_releases_its_transcript(self, cli):
        client = ClaudeCodeClient()
        cli.reports = "2.1.240"
        with pytest.raises(CliVersionMismatch):
            client.complete("transcribe")
        assert cli.transcripts() == set()


class TestReleaseTranscripts:
    def test_only_uuids_select_files(self, cli):
        project = cli.projects / PROJECT
        project.mkdir(parents=True)
        keep = project / "notes.jsonl"
        keep.write_text("{}\n")
        assert release_transcripts(["*", "../notes", "notes"], None) == []
        assert keep.exists()

    def test_destination_setting(self, monkeypatch, tmp_path):
        monkeypatch.delenv(llm.TRANSCRIPTS_ENV, raising=False)
        assert transcript_destination() is None
        assert transcript_destination("delete") is None
        monkeypatch.setenv(llm.TRANSCRIPTS_ENV, " delete ")
        assert transcript_destination() is None
        monkeypatch.chdir(tmp_path)
        assert transcript_destination("record") == tmp_path / "record"
