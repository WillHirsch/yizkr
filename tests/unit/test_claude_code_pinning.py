"""Offline tests for the pinned, isolated claude-code engine (#133).

A fake ``claude`` executable stands in for the CLI. It answers
``--version``, stores each session's transcript under a temporary
$CLAUDE_CONFIG_DIR the way the real CLI does, refuses to fork a session
whose transcript is gone, and prints a stream-json event stream whose
init event reports the CLI version and the tools offered to the model,
followed by any tool calls and results a test gives it (``read_events``,
in the shape CLI 2.1.237 prints them) and the answer.
"""

from __future__ import annotations

import gc
import json
import os
import pwd
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import llm
from yizkr.pipeline.llm import (
    FILE_ERROR,
    REQUEST_ERROR,
    UNCLASSIFIED,
    ClaudeCodeClient,
    CliConfigEnvironment,
    CliHomeRelative,
    CliToolsMismatch,
    CliVersionMismatch,
    EngineToolError,
    PassPaused,
    ToolFailure,
    claude_config_dir,
    claude_global_config_path,
    make_client,
    pinned_cli_version,
    read_error,
    release_transcripts,
    tool_failures,
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
        self.projects = projects  # where the fixture's config files them
        self.installed = PINNED  # what `claude --version` prints
        self.reports: str | None = None  # init event version, if not that
        # the tools the init event reports offering the model (None: no
        # tools key, as in older streams)
        self.tools: list[str] | None = ["Read"]
        # the model the init event reports answering (None: no model key)
        self.model: str | None = None
        # the answer's text, and the tool calls and results the stream
        # reports before it (read_events)
        self.answer = "answer"
        self.tool_events: list[dict] = []
        self.returncode = 0
        self.timeout = False
        self.probes: list[dict] = []  # kwargs of each --version probe
        self.calls: list[tuple[list[str], dict]] = []
        self.written: list[Path] = []  # every transcript file it wrote

    def transcripts(self) -> set[str]:
        """The transcripts it wrote that are still where it wrote them."""
        return {path.name for path in self.written if path.exists()}

    @staticmethod
    def projects_of(env: dict) -> Path:
        """Where the real CLI files sessions: its config directory, taken
        as given, or ~/.claude."""
        if env.get("CLAUDE_CONFIG_DIR"):
            return Path(env["CLAUDE_CONFIG_DIR"]) / "projects"
        home = env.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
        return Path(home) / ".claude" / "projects"

    def __call__(self, command, **kwargs):
        if command[1:] == ["--version"]:
            self.probes.append(kwargs)
            return Completed(stdout=f"{self.installed} (Claude Code)\n")
        self.calls.append((command, kwargs))
        projects = self.projects_of(kwargs["env"])
        if "--resume" in command:
            parent = command[command.index("--resume") + 1]
            if not list(projects.glob(f"*/{parent}.jsonl")):
                return Completed(
                    stderr=f"No conversation found with session ID: {parent}",
                    returncode=1,
                )
            session = str(uuid.uuid4())
        else:
            session = command[command.index("--session-id") + 1]
        project = projects / PROJECT
        (project / session / "tool-results").mkdir(parents=True)
        (project / f"{session}.jsonl").write_text('{"type": "user"}\n')
        self.written.append(project / f"{session}.jsonl")
        version = self.reports or self.installed
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "claude_code_version": version,
        }
        if self.tools is not None:
            init["tools"] = self.tools
        if self.model is not None:
            init["model"] = self.model
        events = [
            init,
            *({**event, "session_id": session} for event in self.tool_events),
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "content": [{"type": "text", "text": self.answer}]
                },
            },
            {"type": "result", "session_id": session, "result": self.answer},
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


def install_fake_cli(monkeypatch, tmp_path) -> FakeCli:
    """The fake CLI in place of the real one, for a fresh pass with a
    config directory and a temporary directory of its own."""
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


@pytest.fixture
def cli(monkeypatch, tmp_path) -> FakeCli:
    return install_fake_cli(monkeypatch, tmp_path)


def read_events(
    path, *, error: str | None = None, pages: str | None = None, content=None
) -> list[dict]:
    """A Read of `path` and its result as CLI 2.1.237's stream reports them
    (#213): an assistant event with the tool_use block, then a user event
    with the tool_result. With `error`, the result has is_error and the
    error's text as its content (or `content`, when given); without, it is
    an image."""
    call_id = f"toolu_{uuid.uuid4().hex[:24]}"
    arguments = {"file_path": str(path)}
    if pages is not None:
        arguments["pages"] = pages
    use = {
        "type": "tool_use",
        "id": call_id,
        "name": "Read",
        "input": arguments,
        "caller": {"type": "direct"},
    }
    if error is None:
        result = {
            "tool_use_id": call_id,
            "type": "tool_result",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo=",
                    },
                }
            ],
        }
        reported = {"type": "image"}
    else:
        result = {
            "type": "tool_result",
            "content": error if content is None else content,
            "is_error": True,
            "tool_use_id": call_id,
        }
        reported = f"Error: {error}"
    return [
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {"content": [use]},
        },
        {
            "type": "user",
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": [result]},
            "tool_use_result": reported,
        },
    ]


# a neutral placeholder path, as the CLI's messages quote the file's
PLACEHOLDER = "/srv/intake/scan-0001.pdf"
# The Read tool's errors, worded as CLI 2.1.237 words them: (message, the
# class it is named by, its kind)
READ_ERRORS = [
    (
        "This PDF has 34 pages, which is too many to read at once. Use the "
        'pages parameter to read specific page ranges (e.g., pages: "1-5"). '
        "Maximum 20 pages per request.",
        "too-many-pages",
        REQUEST_ERROR,
    ),
    (
        "Requested pages 21-40 is outside the document (PDF has 34 pages). "
        "Use a range within 1-34, maximum 20 pages per request (e.g. pages: "
        '"1-20").',
        "page-out-of-range",
        REQUEST_ERROR,
    ),
    (
        'Page range "1-30" exceeds maximum of 20 pages per request. Please '
        "use a smaller range.",
        "page-range-too-long",
        REQUEST_ERROR,
    ),
    (
        'Invalid pages parameter: "first". Use formats like "1-5", "3", or '
        '"10-20". Pages are 1-indexed.',
        "invalid-page-range",
        REQUEST_ERROR,
    ),
    (
        "Reading full PDFs is not supported with this model. Use a newer "
        "model (Sonnet 3.5 v2 or later), or use the pages parameter to read "
        'specific page ranges (e.g., pages: "1-5", maximum 20 pages per '
        "request). Page extraction requires poppler-utils: install with "
        "`brew install poppler` on macOS or `apt-get install poppler-utils` "
        "on Debian/Ubuntu.",
        "whole-pdf-unsupported",
        REQUEST_ERROR,
    ),
    (
        "File content (41200 tokens) exceeds maximum allowed tokens (25000). "
        "Use offset and limit parameters to read specific portions of the "
        "file, or search for specific content instead of reading the whole "
        "file.",
        "too-many-tokens",
        REQUEST_ERROR,
    ),
    (
        "<tool_use_error>InputValidationError: Read failed due to the "
        "following issue:\nThe required parameter `file_path` is missing"
        "</tool_use_error>",
        "invalid-input",
        REQUEST_ERROR,
    ),
    (
        "pdftoppm is not installed. Install poppler-utils (e.g. `brew "
        "install poppler` or `apt-get install poppler-utils`) to enable PDF "
        "page rendering.",
        "pdf-renderer-missing",
        FILE_ERROR,
    ),
    (
        "<tool_use_error>File does not exist. Note: your current working "
        "directory is /tmp/yizkr-claude-code.</tool_use_error>",
        "missing",
        FILE_ERROR,
    ),
    (
        "File is in a directory that is denied by your permission settings.",
        "permission-denied",
        FILE_ERROR,
    ),
    (f"PDF file is empty: {PLACEHOLDER}", "empty", FILE_ERROR),
    ("Image file is empty: /srv/intake/scan-0001.png", "empty", FILE_ERROR),
    (
        "PDF file exceeds maximum allowed size of 20MB.",
        "too-large",
        FILE_ERROR,
    ),
    (
        "PDF is password-protected. Please provide an unprotected version.",
        "password-protected",
        FILE_ERROR,
    ),
    (
        "This tool cannot read binary files. The file appears to be a binary "
        ".tif file. Please use appropriate tools for binary file analysis.",
        "unsupported-format",
        FILE_ERROR,
    ),
    (
        f"File is not a valid PDF (missing %PDF- header): {PLACEHOLDER}",
        "corrupt",
        FILE_ERROR,
    ),
    (
        "File has an image extension but its content is not a valid "
        "PNG/JPEG/GIF/WebP. Detected: unknown.",
        "corrupt",
        FILE_ERROR,
    ),
    ("PDF file is corrupted or invalid.", "corrupt", FILE_ERROR),
    (
        "pdftoppm produced no output pages. The PDF may be invalid.",
        "corrupt",
        FILE_ERROR,
    ),
    (
        f"Could not render PDF: I/O Error: Couldn't open file '{PLACEHOLDER}'",
        "render-failed",
        FILE_ERROR,
    ),
    (
        "pdftoppm failed: Syntax Warning: unexpected operator",
        "render-failed",
        FILE_ERROR,
    ),
    ("An error no CLI words yet.", UNCLASSIFIED, FILE_ERROR),
]
RENDERER_MISSING = READ_ERRORS[7][0]
TOO_MANY_PAGES = READ_ERRORS[0][0]


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
        # Read is the one tool approved and the one tool offered
        assert command[command.index("--allowedTools") + 1] == "Read"
        assert command[command.index("--tools") + 1] == "Read"

    def test_compaction_is_pinned_on(self, cli):
        """Command-line settings outrank the global config, which the CLI
        otherwise consults for autoCompactEnabled (#206 review)."""
        client = ClaudeCodeClient()
        client.complete("transcribe")
        command = cli.calls[0][0]
        settings = command[command.index("--settings") + 1]
        assert settings == '{"autoCompactEnabled":true}'
        # an argument, so the hashed config carries it
        arguments = client.effective_config["arguments"]
        assert arguments[arguments.index("--settings") + 1] == settings

    def test_calls_run_in_a_dedicated_directory(self, cli, tmp_path):
        ClaudeCodeClient().complete("transcribe")
        cwd = Path(cli.calls[0][1]["cwd"])
        assert cwd == tmp_path / "tmp" / ClaudeCodeClient.WORKDIR_NAME
        assert cwd.is_dir() and not any(cwd.iterdir())

    def test_other_variables_stay_behind(self, cli, monkeypatch):
        inherited = {
            # what a Claude Code session exports to the commands it runs
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": str(uuid.uuid4()),
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_EFFORT": "xhigh",
            # a key would switch billing; the model is chosen per client
            "ANTHROPIC_API_KEY": "sk-should-not-leak",
            "ANTHROPIC_MODEL": "some-other-model",
            # switches CLI 2.1.237 reads outside those prefixes: thinking,
            # prompt caching, tool search, the LSP tool, compaction and
            # feature flags (#206 reviews)
            "MAX_THINKING_TOKENS": "32000",
            "DISABLE_INTERLEAVED_THINKING": "1",
            "DISABLE_PROMPT_CACHING_OPUS": "1",
            "ENABLE_PROMPT_CACHING_1H": "1",
            "FORCE_PROMPT_CACHING_5M": "1",
            "ENABLE_TOOL_SEARCH": "false",
            "ENABLE_LSP_TOOL": "1",
            "DISABLE_AUTO_COMPACT": "1",
            "DISABLE_COMPACT": "1",
            "DISABLE_GROWTHBOOK": "",
            # the shell is named in the model's context, and the locale
            # formats numbers there
            "SHELL": "/usr/bin/zsh",
            "LANG": "de_DE.UTF-8",
            # a switch no CLI version reads yet
            "ENABLE_INVENTED_FEATURE": "1",
        }
        for name, value in inherited.items():
            monkeypatch.setenv(name, value)
        ClaudeCodeClient().complete("transcribe")
        env = cli.calls[0][1]["env"]
        for name in inherited:
            # absent, or the value pinned for every call
            assert env.get(name) == llm._PINNED_ENVIRONMENT.get(name)
        assert set(env) <= llm._PASSED_VARIABLES | set(llm._PINNED_ENVIRONMENT)
        # the version probe runs under the same environment
        assert cli.probes[0]["env"] == env

    def test_allowlisted_variables_reach_the_cli(
        self, cli, monkeypatch, tmp_path
    ):
        passed = {
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path / "tmp"),
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
            "HTTPS_PROXY": "http://proxy.example:3128",
            "no_proxy": "localhost",
            "NODE_EXTRA_CA_CERTS": str(tmp_path / "proxy-ca.pem"),
        }
        for name, value in passed.items():
            monkeypatch.setenv(name, value)
        ClaudeCodeClient().complete("transcribe")
        env = cli.calls[0][1]["env"]
        for name, value in passed.items():
            assert env[name] == value
        # the search path, and the login's location set by the fixture
        assert env["PATH"] == os.environ["PATH"]
        assert env["CLAUDE_CONFIG_DIR"].endswith("claude-config")

    def test_pinned_settings_are_set(self, cli, monkeypatch):
        # an inherited value never replaces a pinned one
        monkeypatch.setenv("DISABLE_AUTOUPDATER", "0")
        monkeypatch.setenv("DISABLE_GROWTHBOOK", "")
        monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "4321")
        ClaudeCodeClient().complete("transcribe")
        for env in (cli.probes[0]["env"], cli.calls[0][1]["env"]):
            assert env["DISABLE_AUTOUPDATER"] == "1"
            assert env["DISABLE_GROWTHBOOK"] == "1"
            assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
            assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
            assert env["DISABLE_ERROR_REPORTING"] == "1"
            assert env["DISABLE_TELEMETRY"] == "1"

    def test_empty_variables_are_left_out(self, cli, monkeypatch, tmp_path):
        """An empty value is unset for the CLI and for the helpers here
        alike (#206 review): the CLI would read an empty CLAUDE_CONFIG_DIR
        as a relative directory."""
        monkeypatch.setattr(
            pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path))
        )
        for name in ("HOME", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY"):
            monkeypatch.setenv(name, "")
        ClaudeCodeClient().complete("transcribe")
        env = cli.calls[0][1]["env"]
        assert not {"HOME", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY"} & set(env)


def write_config(path: Path, config: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


class TestGlobalConfig:
    """The CLI copies the env map of its global config into its own
    environment at every start, past the allowlist (#206 review), so a
    client refuses to start or call while that map is not empty."""

    @pytest.mark.parametrize(
        ("config_dir_set", "name"),
        [
            (True, ".config.json"),
            (True, ".claude.json"),
            (False, ".claude/.config.json"),
            (False, ".claude.json"),
        ],
    )
    def test_env_map_is_refused_wherever_the_cli_reads_it(
        self, cli, monkeypatch, tmp_path, config_dir_set, name
    ):
        if config_dir_set:
            base = Path(os.environ["CLAUDE_CONFIG_DIR"])
        else:
            monkeypatch.delenv("CLAUDE_CONFIG_DIR")
            base = tmp_path / "home"
            monkeypatch.setenv("HOME", str(base))
        path = write_config(
            base / name, {"env": {"DISABLE_PROMPT_CACHING": "1"}}
        )
        assert claude_global_config_path(llm.claude_config_dir()) == path
        with pytest.raises(CliConfigEnvironment) as refused:
            make_client("claude-code")
        assert "DISABLE_PROMPT_CACHING" in str(refused.value)
        assert str(path) in str(refused.value)
        # refused before anything ran
        assert cli.probes == [] and cli.calls == []

    def test_config_json_is_read_first(self, cli):
        base = Path(os.environ["CLAUDE_CONFIG_DIR"])
        write_config(base / ".claude.json", {"env": {"DISABLE_COMPACT": "1"}})
        write_config(base / ".config.json", {"numStartups": 3})
        ClaudeCodeClient().complete("transcribe")
        assert len(cli.calls) == 1

    def test_empty_env_map_is_accepted(self, cli):
        # the CLI's own default config holds an empty map; compaction is
        # pinned by --settings rather than refused
        base = Path(os.environ["CLAUDE_CONFIG_DIR"])
        write_config(
            base / ".claude.json", {"env": {}, "autoCompactEnabled": False}
        )
        ClaudeCodeClient().complete("transcribe")
        assert len(cli.calls) == 1

    def test_refusal_names_variables_never_values(self, cli):
        path = write_config(
            Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json",
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://stub.example:8080",
                    "ANTHROPIC_API_KEY": "sk-secret-in-config",
                }
            },
        )
        with pytest.raises(CliConfigEnvironment) as refused:
            ClaudeCodeClient()
        message = str(refused.value)
        assert f"{path} sets" in message
        assert "ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL" in message
        assert "sk-secret-in-config" not in message
        assert "stub.example" not in message
        # where such variables can go instead
        assert "shell" in message and "user settings" in message

    def test_env_map_added_mid_pass_stops_the_next_call(self, cli):
        client = ClaudeCodeClient()
        client.complete("first")
        write_config(
            Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json",
            {"env": {"ENABLE_TOOL_SEARCH": "false"}},
        )
        with pytest.raises(CliConfigEnvironment):
            client.complete("second")
        assert issubclass(CliConfigEnvironment, PassPaused)
        assert len(cli.calls) == 1  # the second call never reached the CLI

    def test_unreadable_config_is_refused(self, cli, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(llm, "time", SimpleNamespace(sleep=sleeps.append))
        path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # cut short, as a file caught mid-write would be
        path.write_text('{"env": {"ANTHROPIC_API_KEY": "sk-secret-in-conf')
        with pytest.raises(CliConfigEnvironment, match="cannot be read") as e:
            ClaudeCodeClient()
        assert "sk-secret-in-conf" not in str(e.value)
        assert len(sleeps) == 1  # read once more before refusing
        assert cli.probes == []

    def test_empty_home_means_the_password_database_home(
        self, cli, monkeypatch, tmp_path
    ):
        """With HOME empty the CLI uses the password database's home, and
        so does the check; Path.home() would give the root directory
        (#206 review)."""
        home = tmp_path / "pw-home"
        monkeypatch.setattr(
            pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home))
        )
        monkeypatch.setenv("HOME", "")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        path = write_config(
            home / ".claude.json", {"env": {"DISABLE_COMPACT": "1"}}
        )
        assert claude_global_config_path(llm.claude_config_dir()) == path
        with pytest.raises(CliConfigEnvironment, match="DISABLE_COMPACT"):
            ClaudeCodeClient()


class TestConfigDir:
    """The CLI takes CLAUDE_CONFIG_DIR raw: it does not expand ~, and it
    reads a relative value against its own working directory (#206
    review). A client normalises the value once and passes the absolute
    path on, so the CLI and the engine use the same directory."""

    def test_tilde_value_is_the_home_directory(
        self, cli, monkeypatch, tmp_path
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cfgtilde")
        config = home / "cfgtilde"
        # the global config checked is the one there
        write_config(
            config / ".claude.json", {"env": {"DISABLE_COMPACT": "1"}}
        )
        with pytest.raises(CliConfigEnvironment) as refused:
            ClaudeCodeClient()
        assert str(config / ".claude.json") in str(refused.value)
        (config / ".claude.json").unlink()
        ClaudeCodeClient().complete("transcribe")
        # the CLI is given that directory, and its transcript is released
        assert cli.calls[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(config)
        assert cli.written[0].is_relative_to(config)
        assert cli.transcripts() == set()

    def test_relative_value_is_taken_from_the_current_directory(
        self, cli, monkeypatch, tmp_path
    ):
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        monkeypatch.chdir(pipeline)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relcfg")
        config = pipeline / "relcfg"
        client = ClaudeCodeClient()
        assert client.config_dir == config
        # normalised once: a later change of directory does not move it
        monkeypatch.chdir(tmp_path)
        _, handle = client.open_conversation("list the entities")
        client.fork(handle, "chunk 1")
        assert all(
            call[1]["env"]["CLAUDE_CONFIG_DIR"] == str(config)
            for call in cli.calls
        )
        assert all(path.is_relative_to(config) for path in cli.written)
        client.close_conversation(handle)
        assert cli.transcripts() == set()

    def test_normal_forms(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
        for value, expected in [
            ("~", tmp_path / "home"),
            ("~/a/../b", tmp_path / "home" / "b"),
            ("rel", tmp_path / "rel"),
            (str(tmp_path / "abs"), tmp_path / "abs"),
        ]:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
            assert claude_config_dir() == expected
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        assert claude_config_dir() is None

    def test_relative_home_is_refused(self, cli, monkeypatch):
        monkeypatch.setenv("HOME", "relhome")
        with pytest.raises(CliHomeRelative, match="relhome"):
            ClaudeCodeClient()
        assert issubclass(CliHomeRelative, PassPaused)
        assert cli.probes == [] and cli.calls == []


class TestEffectiveConfig:
    def test_lists_what_shapes_a_call(self, cli, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
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
        environment = config["environment"]
        assert environment["set"] == {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_GROWTHBOOK": "1",
            "DISABLE_TELEMETRY": "1",
        }
        assert environment["passed"] == [
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
            "HOME",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NODE_EXTRA_CA_CERTS",
            "NODE_USE_SYSTEM_CA",
            "NO_PROXY",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "TMPDIR",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ]
        # names of passed variables only, never their values
        dumped = json.dumps(config)
        assert "subscription-token" not in dumped
        assert "proxy.example" not in dumped

    def test_hash_covers_the_rule_not_this_machine(
        self, cli, monkeypatch, tmp_path
    ):
        before = ClaudeCodeClient().config_hash
        # passed values differ between machines; other variables are
        # dropped
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        monkeypatch.setenv("ENABLE_INVENTED_FEATURE", "1")
        assert ClaudeCodeClient().config_hash == before
        monkeypatch.setattr(
            llm, "_PASSED_VARIABLES", llm._PASSED_VARIABLES | {"SHELL"}
        )
        assert ClaudeCodeClient().config_hash != before

    def test_hash_follows_the_pinned_values(self, cli, monkeypatch):
        before = ClaudeCodeClient().config_hash
        monkeypatch.setitem(llm._PINNED_ENVIRONMENT, "DISABLE_GROWTHBOOK", "0")
        environment_changed = ClaudeCodeClient().config_hash
        assert environment_changed != before
        monkeypatch.setitem(llm._PINNED_SETTINGS, "autoCompactEnabled", False)
        assert ClaudeCodeClient().config_hash != environment_changed

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


class TestWhatACallSends:
    """What the LLM cache keys a call on and records with its answer
    (#225, ADR-0032): the text the CLI is sent, the system prompt every
    call appends, and what a call's stream reports."""

    def test_call_text_is_what_the_cli_receives(self, cli, tmp_path):
        image = tmp_path / "scan.png"
        image.write_bytes(b"png")
        client = ClaudeCodeClient()
        client.complete("Transcribe.", [image])
        _, kwargs = cli.calls[0]
        assert kwargs["input"] == client.call_text("Transcribe.", [image])
        assert str(image.resolve()) in kwargs["input"]
        assert client.call_text("Transcribe.") == "Transcribe."

    def test_the_system_prompt_is_what_every_call_appends(self, cli):
        client = ClaudeCodeClient()
        client.complete("transcribe")
        command, _ = cli.calls[0]
        appended = command[command.index("--append-system-prompt") + 1]
        assert appended == client.system_prompt
        assert client.system_prompt in client.effective_config["arguments"]

    def test_a_call_reports_its_model_and_version(self, cli):
        cli.model = "claude-sonnet-5-20260901"
        client = ClaudeCodeClient(model="claude-sonnet-5")
        assert client.last_report() == {}
        client.complete("transcribe")
        report = client.last_report()
        assert report["model"] == "claude-sonnet-5-20260901"
        assert report["cli_version"] == PINNED
        assert uuid.UUID(report["session_id"])

    def test_a_report_is_its_threads_and_a_failed_call_makes_none(self, cli):
        client = ClaudeCodeClient()
        seen = []

        def call():
            client.complete("transcribe")
            seen.append(client.last_report())

        thread = threading.Thread(target=call)
        thread.start()
        thread.join()
        assert seen[0]["cli_version"] == PINNED
        assert client.last_report() == {}  # another thread's call
        cli.returncode = 1
        with pytest.raises(RuntimeError):
            client.complete("transcribe")
        assert client.last_report() == {}


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

    def test_a_kept_conversation_resumes_exactly(self, cli):
        """The LLM cache keeps an open conversation's session files beside
        its opening turn (#225). Put back by a later client, they let it
        fork the conversation as it was opened, with no call to open it
        again, and are released as an opened conversation's are."""
        first = ClaudeCodeClient()
        _, handle = first.open_conversation("list the entities")
        kept = first.keep_conversation(handle)
        assert set(kept) == {f"{PROJECT}/{handle}.jsonl"}
        first.close_conversation(handle)
        assert cli.transcripts() == set()
        calls = len(cli.calls)
        second = ClaudeCodeClient()
        session = second.resume_conversation(
            "list the entities", "answer", kept
        )
        assert session == handle
        assert len(cli.calls) == calls
        transcript = cli.projects / PROJECT / f"{handle}.jsonl"
        assert transcript.read_bytes() == kept[f"{PROJECT}/{handle}.jsonl"]
        assert second.fork(session, "chunk 1") == "answer"
        command, _ = cli.calls[-1]
        assert command[command.index("--resume") + 1] == handle
        assert "--fork-session" in command
        second.close_conversation(session)
        assert not transcript.exists()

    @pytest.mark.parametrize(
        "names",
        [
            [],
            [
                f"{PROJECT}/{uuid.uuid4()}.jsonl",
                f"{PROJECT}/{uuid.uuid4()}.jsonl",
            ],
            [f"{PROJECT}/not-a-session.jsonl"],
            [f"../elsewhere/{uuid.uuid4()}.jsonl"],
        ],
        ids=["nothing", "two", "not-a-uuid", "outside"],
    )
    def test_unusable_kept_files_are_refused(self, cli, tmp_path, names):
        with pytest.raises(ValueError):
            ClaudeCodeClient().resume_conversation(
                "list the entities", "answer", dict.fromkeys(names, b"{}\n")
            )
        assert not (tmp_path / "claude-config" / "elsewhere").exists()

    def test_a_conversation_open_already_is_not_resumed(self, cli):
        """Resuming a session the client holds open would let the second
        holder's release pull the transcript from under the first."""
        client = ClaudeCodeClient()
        _, handle = client.open_conversation("list the entities")
        kept = client.keep_conversation(handle)
        with pytest.raises(ValueError, match="open already"):
            client.resume_conversation("list the entities", "answer", kept)
        assert cli.transcripts() == {f"{handle}.jsonl"}
        client.close_conversation(handle)
        assert cli.transcripts() == set()

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


class TestToolCheck:
    """--tools Read leaves the model that tool alone; account data from
    the server can add others (#206 review), so every call's reported
    tool list is checked."""

    def test_read_alone_passes(self, cli):
        assert ClaudeCodeClient().complete("transcribe") == "answer"

    def test_another_tool_pauses_the_pass(self, cli):
        client = ClaudeCodeClient()
        cli.tools = ["Read", "TaskCreate", "Workflow"]
        with pytest.raises(CliToolsMismatch, match="TaskCreate, Workflow"):
            client.complete("transcribe")
        assert issubclass(CliToolsMismatch, PassPaused)
        # the call's output is discarded and its transcript released
        assert cli.transcripts() == set()
        # latched: the next call never reaches the CLI
        cli.tools = ["Read"]
        with pytest.raises(CliToolsMismatch):
            client.complete("again")
        assert len(cli.calls) == 1

    def test_stream_without_a_tool_list_is_let_through(self, cli):
        cli.tools = None
        assert ClaudeCodeClient().complete("transcribe") == "answer"

    def test_mismatch_opening_a_conversation_holds_nothing(self, cli):
        client = ClaudeCodeClient()
        cli.tools = ["Read", "Bash"]
        with pytest.raises(CliToolsMismatch):
            client.open_conversation("list the entities")
        # released, not kept for forks
        assert cli.transcripts() == set()
        assert client._conversations == set()

    def test_mismatch_in_a_fork_pauses_the_pass(self, cli):
        client = ClaudeCodeClient()
        _, handle = client.open_conversation("list the entities")
        cli.tools = ["Read", "Bash"]
        with pytest.raises(CliToolsMismatch):
            client.fork(handle, "chunk 1")
        # the fork's own transcript is released; the conversation is kept
        assert cli.transcripts() == {f"{handle}.jsonl"}


class TestReleaseTranscripts:
    def test_only_uuids_select_files(self, cli):
        project = cli.projects / PROJECT
        project.mkdir(parents=True)
        keep = project / "notes.jsonl"
        keep.write_text("{}\n")
        config_dir = llm.claude_config_dir()
        assert (
            release_transcripts(["*", "../notes", "notes"], None, config_dir)
            == []
        )
        assert keep.exists()

    def test_empty_home_means_the_password_database_home(
        self, monkeypatch, tmp_path
    ):
        """With HOME empty the CLI files sessions under the password
        database's home, and release looks there (#206 review)."""
        home = tmp_path / "pw-home"
        monkeypatch.setattr(
            pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home))
        )
        monkeypatch.setenv("HOME", "")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        projects = home / ".claude" / "projects"
        assert llm.claude_projects_dir(llm.claude_config_dir()) == projects
        session = str(uuid.uuid4())
        (projects / PROJECT).mkdir(parents=True)
        transcript = projects / PROJECT / f"{session}.jsonl"
        transcript.write_text("{}\n")
        assert release_transcripts([session], None, None) == [transcript]
        assert not transcript.exists()

    def test_destination_setting(self, monkeypatch, tmp_path):
        monkeypatch.delenv(llm.TRANSCRIPTS_ENV, raising=False)
        assert transcript_destination() is None
        assert transcript_destination("delete") is None
        monkeypatch.setenv(llm.TRANSCRIPTS_ENV, " delete ")
        assert transcript_destination() is None
        monkeypatch.chdir(tmp_path)
        assert transcript_destination("record") == tmp_path / "record"


def a_scan(tmp_path: Path, name: str = "scan.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 placeholder")
    return path


class TestToolErrors:
    """A one-shot call's stream reports each Read and its result (#213). A
    Read that failed on a file the call was given fails the call, whatever
    the answer says, unless the tool only refused the form of the request
    and another Read of the same file succeeded."""

    def test_a_failed_read_of_the_input_fails_the_call(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        cli.tool_events = read_events(scan, error=RENDERER_MISSING)
        cli.answer = "Text that reads like a transcription."
        with pytest.raises(EngineToolError) as caught:
            ClaudeCodeClient().complete("Transcribe.", [scan])
        failure = ToolFailure("Read", "pdf-renderer-missing", 0)
        assert caught.value.failures == (failure,)
        # the tool and the class of its error, never the error's text, the
        # file's path or the answer
        assert str(caught.value) == (
            "the engine's stream reports a tool failing on the call's "
            "input: Read (pdf-renderer-missing)"
        )
        assert caught.value.response == cli.answer

    def test_the_recorded_stream_shape(self, cli, tmp_path):
        """The shape a real call printed (CLI 2.1.237, two invented inputs:
        a blank PNG the Read tool read, and a file that is not a PDF, which
        it could not): the result event says nothing of the error, and the
        answer says nothing either."""
        image = tmp_path / "blank.png"
        image.write_bytes(b"\x89PNG placeholder")
        scan = a_scan(tmp_path, "not-a-pdf.pdf")
        scan.write_text("placeholder\n")
        cli.tool_events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": ""}]},
            },
            *read_events(image),
            *read_events(
                scan,
                error="File is not a valid PDF (missing %PDF- header): "
                f"{scan}",
            ),
        ]
        cli.answer = "done."
        with pytest.raises(EngineToolError) as caught:
            ClaudeCodeClient().complete("Reply: done.", [image, scan])
        assert caught.value.failures == (ToolFailure("Read", "corrupt", 1),)

    def test_a_read_that_succeeds_passes(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        cli.tool_events = read_events(scan)
        client = ClaudeCodeClient()
        assert client.complete("Transcribe.", [scan]) == "answer"
        assert client.last_report()["cli_version"] == PINNED

    @pytest.mark.parametrize(("message", "name", "kind"), READ_ERRORS)
    def test_read_errors_are_classed(self, message, name, kind):
        assert read_error(message) == (name, kind)

    def test_every_class_has_an_example(self):
        classes = {name for name, _, _ in llm._READ_ERRORS} | {UNCLASSIFIED}
        assert {name for _, name, _ in READ_ERRORS} == classes

    @pytest.mark.parametrize(
        "message",
        [message for message, _, kind in READ_ERRORS if kind == REQUEST_ERROR],
    )
    def test_a_refused_request_is_made_up_for_by_a_read_of_the_file(
        self, cli, tmp_path, message
    ):
        """The tool refuses to read a PDF of more than 10 pages without a
        page range, and says so as an error: a Read of a range that
        succeeds, before or after it (reads may run side by side), means
        the file was read."""
        scan = a_scan(tmp_path)
        client = ClaudeCodeClient()
        cli.tool_events = read_events(scan, error=message) + read_events(
            scan, pages="1-20"
        )
        assert client.complete("Transcribe.", [scan]) == "answer"
        cli.tool_events = read_events(scan, pages="1-20") + read_events(
            scan, error=message
        )
        assert client.complete("Transcribe.", [scan]) == "answer"

    def test_a_refused_request_and_no_read_of_the_file_fails(
        self, cli, tmp_path
    ):
        scan = a_scan(tmp_path)
        other = a_scan(tmp_path, "other.pdf")
        client = ClaudeCodeClient()
        cli.tool_events = read_events(scan, error=TOO_MANY_PAGES)
        with pytest.raises(EngineToolError) as caught:
            client.complete("Transcribe.", [scan])
        assert caught.value.failures == (
            ToolFailure("Read", "too-many-pages", 0),
        )
        # a Read of another input makes up for nothing
        cli.tool_events = read_events(other) + read_events(
            scan, error=TOO_MANY_PAGES
        )
        with pytest.raises(EngineToolError) as caught:
            client.complete("Transcribe.", [scan, other])
        assert caught.value.failures == (
            ToolFailure("Read", "too-many-pages", 0),
        )

    @pytest.mark.parametrize(
        "message",
        [message for message, _, kind in READ_ERRORS if kind == FILE_ERROR],
    )
    def test_a_file_error_is_never_made_up_for(self, cli, tmp_path, message):
        """Pages 4-6 that could not be rendered are missing from the
        answer, however well pages 1-3 and 7-9 were read."""
        scan = a_scan(tmp_path)
        cli.tool_events = (
            read_events(scan, pages="1-3")
            + read_events(scan, pages="4-6", error=message)
            + read_events(scan, pages="7-9")
        )
        with pytest.raises(EngineToolError) as caught:
            ClaudeCodeClient().complete("Transcribe.", [scan])
        assert caught.value.failures == (
            ToolFailure("Read", read_error(message)[0], 0),
        )

    def test_reads_of_other_files_are_not_the_input(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        cli.tool_events = read_events(
            tmp_path / "elsewhere.pdf", error=RENDERER_MISSING
        ) + read_events(scan)
        assert ClaudeCodeClient().complete("Transcribe.", [scan]) == "answer"

    def test_other_tools_are_not_the_input(self, cli, tmp_path):
        """A call of a tool the model is not offered is refused before it
        touches anything; the tool check deals with tools offered."""
        scan = a_scan(tmp_path)
        events = read_events(scan, error="Error: No such tool available: X")
        events[0]["message"]["content"][0]["name"] = "Bash"
        cli.tool_events = events + read_events(scan)
        assert ClaudeCodeClient().complete("Transcribe.", [scan]) == "answer"

    def test_the_input_is_known_by_any_spelling(
        self, cli, tmp_path, monkeypatch
    ):
        scan = a_scan(tmp_path)
        link = tmp_path / "link.pdf"
        link.symlink_to(scan)
        monkeypatch.chdir(tmp_path)
        client = ClaudeCodeClient()
        spellings = [
            (Path("scan.pdf"), scan),
            (link, scan),
            (scan, link),
            (scan, f"{tmp_path}/./scan.pdf"),
        ]
        for given, reported in spellings:
            cli.tool_events = read_events(reported, error=RENDERER_MISSING)
            with pytest.raises(EngineToolError):
                client.complete("Transcribe.", [given])

    def test_error_text_in_blocks(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        cli.tool_events = read_events(
            scan,
            error="missing",
            content=[
                {
                    "type": "text",
                    "text": "<tool_use_error>File does not exist."
                    "</tool_use_error>",
                }
            ],
        )
        with pytest.raises(EngineToolError) as caught:
            ClaudeCodeClient().complete("Transcribe.", [scan])
        assert caught.value.failures == (ToolFailure("Read", "missing", 0),)

    def test_each_failure_is_named_once(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        cli.tool_events = (
            read_events(scan, error=RENDERER_MISSING)
            + read_events(scan, error=RENDERER_MISSING)
            + read_events(scan, pages="1-5", error="PDF file is empty: x")
        )
        with pytest.raises(EngineToolError) as caught:
            ClaudeCodeClient().complete("Transcribe.", [scan])
        assert caught.value.failures == (
            ToolFailure("Read", "pdf-renderer-missing", 0),
            ToolFailure("Read", "empty", 0),
        )
        assert str(caught.value).endswith(
            "Read (pdf-renderer-missing), Read (empty)"
        )

    def test_a_call_without_inputs_is_not_judged(self, cli, tmp_path):
        """Only a one-shot call is given files; a Read in a call that was
        given none is not about its input."""
        cli.tool_events = read_events(
            tmp_path / "scan.pdf", error=RENDERER_MISSING
        )
        client = ClaudeCodeClient()
        assert client.complete("Extract.") == "answer"
        response, handle = client.open_conversation("List the entities.")
        assert response == "answer"
        assert client.fork(handle, "Chunk 1.") == "answer"

    def test_a_tool_error_is_an_ordinary_failure(self, cli, tmp_path):
        """It fails the call, not the pass: the transcript is released, no
        report is made, and the next call goes ahead."""
        assert issubclass(EngineToolError, RuntimeError)
        assert not issubclass(EngineToolError, PassPaused)
        scan = a_scan(tmp_path)
        client = ClaudeCodeClient()
        cli.tool_events = read_events(scan, error=RENDERER_MISSING)
        with pytest.raises(EngineToolError):
            client.complete("Transcribe.", [scan])
        assert cli.transcripts() == set()
        assert client.last_report() == {}
        cli.tool_events = read_events(scan)
        assert client.complete("Transcribe.", [scan]) == "answer"

    def test_a_pause_comes_first(self, cli, tmp_path):
        scan = a_scan(tmp_path)
        client = ClaudeCodeClient()
        cli.tool_events = read_events(scan, error=RENDERER_MISSING)
        cli.reports = "2.1.240"
        with pytest.raises(CliVersionMismatch):
            client.complete("Transcribe.", [scan])

    def test_the_function_reads_a_stream(self, tmp_path):
        scan = a_scan(tmp_path)
        stream = "\n".join(
            json.dumps(event)
            for event in read_events(scan, error=TOO_MANY_PAGES)
        )
        assert tool_failures(stream, [scan]) == [
            ToolFailure("Read", "too-many-pages", 0)
        ]
        assert tool_failures(stream, []) == []
        assert tool_failures("not json\n{}\n[]", [scan]) == []


class TestStreamEvents:
    """The stream-json event stream is one JSON object per line
    (llm._stream_events, #271)."""

    @pytest.mark.parametrize(
        "boundary",
        [" ", " ", "\u0085"],
        ids=["U+2028", "U+2029", "U+0085"],
    )
    def test_a_line_separator_inside_an_event_is_kept_whole(self, boundary):
        """A model's answer can hold a line-separator character
        `str.splitlines` treats as a line break but the newline-delimited
        stream does not: splitting there breaks one line's JSON into two
        malformed halves, and both fail to parse, dropping the event
        (possibly the terminal result event itself). Splitting on ``\\n``
        alone reads it as the single line it is.

        ``ensure_ascii=False`` is needed to get the real character into
        the line: the default escapes it to six ASCII characters, which
        splitlines does not treat as a break either way."""
        answer = f"one{boundary}two"
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": answer}]},
        }
        result = {"type": "result", "is_error": False}
        stream = (
            f"{json.dumps(event, ensure_ascii=False)}\n"
            f"{json.dumps(result, ensure_ascii=False)}"
        )
        assert llm._stream_events(stream) == [event, result]

    def test_a_trailing_cr_is_still_stripped(self):
        """CRLF line endings: splitting on ``\\n`` alone leaves a
        trailing ``\\r`` on each line, which the per-line strip still
        removes."""
        event = {"type": "system", "subtype": "init"}
        result = {"type": "result", "is_error": False}
        stream = f"{json.dumps(event)}\r\n{json.dumps(result)}\r\n"
        assert llm._stream_events(stream) == [event, result]
