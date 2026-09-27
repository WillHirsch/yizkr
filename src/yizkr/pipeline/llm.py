"""Pluggable LLM client protocol for transcription and extraction.

PRIVACY: configuring an external client sends
artifact content — which may describe living people — to that
provider. Nothing here talks to any service unless explicitly
configured; there is no default client. The pipeline runs end-to-end
without one (text-file transcription + prepared claim files).

Two Anthropic-backed engines, differing in how usage is billed:

* ``anthropic`` — the Anthropic API via the `anthropic` package.
  Token-based billing against an API key. Install the package, set
  ANTHROPIC_API_KEY; setting the key IS the explicit opt-in the
  privacy principle requires.
* ``claude-code`` — the Claude Code CLI in headless mode (``claude -p``).
  Uses the CLI's own login, so a Claude Pro/Max **subscription** covers
  usage instead of per-token billing. Install the CLI and run
  ``claude`` once to log in; being logged in is the explicit opt-in.
  A pass runs on one pinned CLI version, isolated from the owner's own
  Claude Code configuration, and the CLI's session transcripts of its
  calls are moved into the run record or deleted (see
  ``ClaudeCodeClient``).

Pick the model for either engine with $YIZKR_LLM_MODEL.

This module is also the client factory, and the only code that imports
the LLM cache (yizkr.pipeline.llmcache, ADR-0032): `open_cache` opens
one, and `cached_client` wraps an engine's client so that a call made
before with the same key is answered from it. The pipeline is written
against the client protocols below and never against the cache, so
without the wrapper it runs unchanged, calling the engine directly.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class LlmClient(Protocol):
    """Minimal completion interface pipeline steps rely on."""

    model_identifier: str

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        """Return the model's text response for a prompt (+ optional
        images)."""
        ...


@runtime_checkable
class ConversationalLlmClient(Protocol):
    """Optional capability: conversations that can be forked (ADR-0006).

    `open_conversation` sends the first turn and returns the response
    plus an opaque handle. Each `fork` continues from that SAME prefix
    independently — forks never see each other's turns. Because every
    fork shares a byte-identical prefix, prompt caching prices the
    shared context at cache-read rates. Callers must treat fork
    failures as recoverable (fall back to resending context inline).
    A ``PassPaused`` is not a fork failure: it ends the pass.
    """

    def open_conversation(self, prompt: str) -> tuple[str, Any]: ...

    def fork(self, handle: Any, prompt: str) -> str: ...


@runtime_checkable
class RejectingLlmClient(Protocol):
    """Optional capability: a caller rejects a response it cannot use.

    A transcription that fails the transcription check, a chunk cut off
    before its sentinel, or an entity manifest the extractor refuses is
    rejected, so that the next identical call asks the engine again
    rather than being answered with the same text. Only a client that
    keeps responses has anything to reject; see `reject_response`.
    """

    def reject(self, response: str, reason: str) -> None: ...


def reject_response(client: Any, response: str, reason: str) -> None:
    """Reject `response`, which `client` returned, when the client can
    take a rejection (the LLM cache's client can); an engine's own client
    keeps nothing, so there is nothing to do."""
    if isinstance(client, RejectingLlmClient):
        client.reject(response, reason)


def _config_hash(config: dict[str, Any]) -> str:
    """SHA-256 of an engine's effective configuration in canonical JSON."""
    canonical = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class AnthropicClient:
    """Anthropic API client (vision-capable). Explicit opt-in only."""

    engine = "anthropic"
    # the output budget every request asks for
    MAX_TOKENS = 4096
    # the API calls carry no system prompt and take no effort setting
    system_prompt: str | None = None
    effort: str | None = None

    def __init__(self, model: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the 'anthropic' package is not installed; "
                "pip install anthropic"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Setting it is the explicit "
                "opt-in required before artifact content leaves this "
                "system (privacy first: nothing goes to third-party "
                "services without explicit instruction)."
            )
        self._client = anthropic.Anthropic()
        self.model_identifier = model or os.environ.get(
            "YIZKR_LLM_MODEL", "claude-sonnet-5"
        )
        # what the API reported about the calling thread's last request
        self._reports = threading.local()

    @property
    def model(self) -> str:
        """The model every request names."""
        return self.model_identifier

    @property
    def effective_config(self) -> dict[str, Any]:
        """What this client sets for its requests besides the prompt."""
        return {
            "engine": self.engine,
            "model": self.model_identifier,
            "max_tokens": self.MAX_TOKENS,
        }

    @property
    def config_hash(self) -> str:
        """SHA-256 of ``effective_config`` in canonical JSON."""
        return _config_hash(self.effective_config)

    def call_text(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        """The text a one-shot call sends: the prompt itself, since the
        images travel as blocks of their own."""
        return prompt

    def last_report(self) -> dict[str, Any]:
        """What the API reported about the calling thread's last
        successful request: the model that answered it."""
        return dict(getattr(self._reports, "value", {}))

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        content: list[dict] = []
        for path in image_paths:
            media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(path.read_bytes()).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": prompt})
        return self._create([{"role": "user", "content": content}])

    def _create(self, messages: list[dict]) -> str:
        response = self._client.messages.create(
            model=self.model_identifier,
            max_tokens=self.MAX_TOKENS,
            messages=messages,
        )
        self._reports.value = {"model": getattr(response, "model", None)}
        return "".join(
            block.text for block in response.content if block.type == "text"
        )

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        """Stateless conversations: the handle IS the message prefix."""
        first_turn = {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        }
        response = self._create([first_turn])
        handle = [
            first_turn,
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
            },
        ]
        return response, handle

    def fork(self, handle: Any, prompt: str) -> str:
        """Replay the shared prefix with a cache breakpoint, add one turn.

        Every fork sends a byte-identical prefix, so the cache_control
        marker on its last block makes the document + earlier turns
        cache reads instead of fresh input tokens.
        """
        messages = copy.deepcopy(handle)
        messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        messages.append(
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        )
        return self._create(messages)


class PassPaused(BaseException):
    """The engine cannot serve the pass until an operator acts.

    It derives from BaseException, as KeyboardInterrupt does, so the
    pipeline's per-document and per-chunk ``except Exception`` isolation
    cannot turn it into a document failure or an inline-context fallback:
    it unwinds the whole pass, which then makes no further calls. The
    completed outputs are already on disk, so the pass resumes from them
    when it is run again.
    """


class CliVersionMismatch(PassPaused):
    """The claude CLI's version differs from the one the pass is pinned
    to."""


class CliConfigEnvironment(PassPaused):
    """The claude CLI's global config sets environment variables, which the
    CLI would add to every call."""


class CliToolsMismatch(PassPaused):
    """A call's CLI offered the model a tool besides the one the engine
    allows."""


class CliHomeRelative(PassPaused):
    """HOME is a relative path, which the CLI and the engine would resolve
    against different directories."""


def _version_mismatch(
    found: str, pinned: str, where: str
) -> CliVersionMismatch:
    return CliVersionMismatch(
        f"claude CLI version {found} ({where}) differs from {pinned}, the "
        "version this pass is pinned to: the CLI was updated or replaced "
        "while the pass ran, for instance by another Claude Code session's "
        "auto-update. The pass is paused and makes no further calls; its "
        "completed outputs are kept. Settle the CLI version, with "
        "auto-update disabled everywhere for the length of the pass "
        "(DISABLE_AUTOUPDATER=1), then run the pass again."
    )


class _CliPin:
    """The claude CLI version one pass runs on, and the pause that stops
    the pass.

    A pass is one process. Its first ClaudeCodeClient pins the version it
    finds installed (or the version its caller requires). Every later
    client is checked against the pin when it starts, and every call
    against the version and the tools its stream reports. A mismatch
    latches: after the first one, no client starts and no call is made in
    this process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.version: str | None = None
        self._pause: tuple[type[PassPaused], str] | None = None

    def _latch(self, pause: PassPaused) -> PassPaused:
        with self._lock:
            if self._pause is None:
                self._pause = (type(pause), str(pause))
        return pause

    def raise_if_paused(self) -> None:
        pause = self._pause
        if pause is not None:
            kind, message = pause
            raise kind(message)

    def adopt(self, installed: str, required: str | None) -> str:
        """Pin ``installed``, or refuse it when it differs from the pin or
        from ``required``; returns the pinned version."""
        self.raise_if_paused()
        with self._lock:
            expected = sorted(
                {v for v in (required, self.version) if v and v != installed}
            )
            if not expected:
                if self.version is None:
                    self.version = installed
                return self.version
        raise self._latch(
            _version_mismatch(installed, " and ".join(expected), "installed")
        )

    def check(self, reported: Any, pinned: str) -> None:
        """Assert the version a call's stream reports. A stream that
        reports none is let through: the CLI reports it in the init event
        of every call, and the next client's start checks the installed
        version regardless."""
        if reported is not None and str(reported) != pinned:
            raise self._latch(
                _version_mismatch(str(reported), pinned, "reported by a call")
            )

    def check_tools(self, reported: Any, allowed: str) -> None:
        """Assert that the tools a call's stream reports offering the model
        are ``allowed`` alone. A stream that reports none is let through,
        as for the version."""
        if not isinstance(reported, list):
            return
        others = sorted({str(tool) for tool in reported} - {allowed})
        if others:
            raise self._latch(
                CliToolsMismatch(
                    f"the claude CLI offered the model {', '.join(others)} "
                    f"besides {allowed}, although --tools {allowed} names "
                    "that tool alone: a CLI change or account data from the "
                    "server has added tools. The call's output is "
                    "discarded, and the pass is paused and makes no "
                    "further calls; its completed outputs are kept."
                )
            )


_PIN = _CliPin()


def pinned_cli_version() -> str | None:
    """The claude CLI version this process's pass is pinned to, or None
    before its first claude-code client has started."""
    return _PIN.version


# The only environment variables a call inherits: those that find and run
# the CLI, locate its config directory, place its temporary files, and
# carry the machine's proxy and certificate setup. Everything else stays
# behind. The CLI reads dozens of variables that change a call, and a new
# version can add more. Among them, MAX_THINKING_TOKENS changes the
# model's output; the DISABLE_PROMPT_CACHING, ENABLE_PROMPT_CACHING_1H and
# FORCE_PROMPT_CACHING_5M families change caching, and so the cost that
# forked extraction relies on (ADR-0006); ENABLE_TOOL_SEARCH changes how
# tools are put in the model's context; and DISABLE_COMPACT and
# DISABLE_AUTO_COMPACT change what a long call does when its context
# fills. A Claude Code session also exports its own session id, effort
# and messaging socket to the commands it runs, and a stray API key would
# silently switch this engine to per-token billing, which is exactly what
# choosing it opts out of. SHELL and the locale stay behind too: the CLI
# names the shell in the model's context and formats some numbers there
# with its runtime's default locale, and it runs without either. The
# config directory these variables locate still shapes a call: its login
# decides the account and how it is billed, and its global config can add
# variables of its own, which check_global_config refuses, and holds the
# account data the server sends (see _PINNED_ENVIRONMENT).
_PASSED_VARIABLES = frozenset(
    {
        # finds the CLI and the programs it runs
        "PATH",
        # the config directory, with the login, the global config and the
        # session store: under the home directory, or the one named
        # instead; and a subscription token used in place of the login
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # temporary files
        "TMPDIR",
        # proxies and certificate authorities on the way to the API
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
        "NODE_USE_SYSTEM_CA",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
# Set for every call. The version is pinned for the pass, so the CLI must
# not update itself. DISABLE_GROWTHBOOK turns off server-side feature
# flags, but the CLI does not run on built-in defaults alone: it also
# fetches account data from the server at every start and caches it in its
# global config, where the owner's own sessions refresh it too. That data
# can change the system-prompt text, the output-token budget, the default
# model and the auto-compact windows, and no variable keeps it out.
# CLAUDE_CODE_MAX_OUTPUT_TOKENS takes the budget out of its reach: every
# call asks for 64000 tokens, the Claude 5 default, capped at the model's
# ceiling in the CLI's built-in model catalog, so the budget follows the
# CLI version and the model alone.
# Auto-memory would carry content from one call into the next. Error
# reports and telemetry go to third parties and can describe the calls,
# such as the paths of the files read (CLAUDE.md rule 7).
_PINNED_ENVIRONMENT = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_GROWTHBOOK": "1",
    "DISABLE_TELEMETRY": "1",
}
# Passed with --settings. The CLI takes a setting from the settings sources
# it loads, --settings among them, and for a few keys falls back to its
# global config when none of those sets the key. autoCompactEnabled is one
# of them: an autoCompactEnabled: false there would otherwise stop a long
# call compacting when its context fills.
_PINNED_SETTINGS = {"autoCompactEnabled": True}

# Where the transcripts of pipeline calls go: a directory (the run
# record) to move them into, or "delete". Unset means delete.
TRANSCRIPTS_ENV = "YIZKR_CLAUDE_TRANSCRIPTS"


def _home() -> Path:
    """The home directory the CLI uses: $HOME, or the password database's
    entry when HOME is unset or empty, as in the CLI's runtime. (For an
    empty HOME, Path.home() would give the root directory.)"""
    home = os.environ.get("HOME")
    if home:
        return Path(home)
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def claude_config_dir() -> Path | None:
    """The CLI's config directory from $CLAUDE_CONFIG_DIR, as an absolute
    path, or None when that is unset or empty.

    The CLI reads the raw value: it does not expand ``~``, and it resolves
    a relative value against its own working directory. A client therefore
    normalises the value once, here, and passes the result to the CLI: a
    leading ``~`` is the home directory, and a relative value is taken from
    the current directory.
    """
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    if not value:
        return None
    if value == "~" or value.startswith("~/"):
        return Path(os.path.abspath(_home() / value[2:]))
    return Path(os.path.abspath(value))


def claude_projects_dir(config_dir: Path | None) -> Path:
    """Where the CLI stores its sessions: ``projects/`` in its config
    directory, or in ~/.claude when it has none."""
    return (config_dir or _home() / ".claude") / "projects"


def claude_global_config_path(config_dir: Path | None) -> Path:
    """The CLI's global config file: ``.config.json`` in its config
    directory (~/.claude when it has none) if that file exists, else
    ``.claude.json`` in the config directory, or in the home directory when
    it has none."""
    preferred = (config_dir or _home() / ".claude") / ".config.json"
    if preferred.exists():
        return preferred
    return (config_dir or _home()) / ".claude.json"


def check_global_config(config_dir: Path | None) -> None:
    """Refuse to go on while the CLI's global config, found from the
    config directory ``config_dir``, sets environment variables.

    At every start the CLI copies the ``env`` map of its global config into
    its own environment, past both ``--setting-sources`` and the allowlist,
    so those variables would shape every call without showing in
    ``config_hash``. Raises ``CliConfigEnvironment`` when the map is not
    empty, naming the file and the variables but never their values, or
    when the file cannot be read as JSON. A file caught mid-write is read
    once more.
    """
    path = claude_global_config_path(config_dir)
    config: Any = None
    for attempt in (1, 2):
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            if attempt == 2:
                raise CliConfigEnvironment(
                    f"the claude CLI's global config {path} cannot be read "
                    f"as JSON ({type(exc).__name__}), so the environment "
                    "variables it would add to every call cannot be "
                    "checked. The pass is paused and makes no further "
                    "calls; its completed outputs are kept. Repair the "
                    "file, then run the pass again."
                ) from None
            time.sleep(0.2)
        else:
            break
    env = config.get("env") if isinstance(config, dict) else None
    if not env:
        return
    names = (
        ", ".join(sorted(env))
        if isinstance(env, dict)
        else "an env entry that is not a map"
    )
    raise CliConfigEnvironment(
        f"the claude CLI's global config {path} sets environment variables "
        f"that the CLI adds to every call: {names}. They could change a "
        "pipeline call without changing its config_hash. Move them to the "
        "shell, or to the env block of user settings (settings.json in the "
        "CLI's config directory), which pipeline calls do not load. The "
        "pass is paused and makes no further calls; its completed outputs "
        "are kept. Run it again once that map is empty."
    )


def transcript_destination(
    setting: str | os.PathLike[str] | None = None,
) -> Path | None:
    """The directory transcripts are moved into, or None to delete them.

    ``setting`` falls back to $YIZKR_CLAUDE_TRANSCRIPTS; an empty value
    or ``delete`` means delete, and a relative directory is resolved
    against the current directory.
    """
    value = (
        os.fspath(setting)
        if setting is not None
        else os.environ.get(TRANSCRIPTS_ENV, "")
    ).strip()
    if value in ("", "delete"):
        return None
    return Path(value).expanduser().resolve()


def release_transcripts(
    session_ids: Iterable[str],
    destination: Path | None,
    config_dir: Path | None,
) -> list[Path]:
    """Move each session's stored transcript out of the CLI's projects
    directory, found from the config directory ``config_dir``, into
    ``destination``, or delete it when that is None.

    A session is its ``<id>.jsonl`` file plus the ``<id>/`` directory
    beside it when there is one (tool results and the like), in whichever
    project directory the CLI filed it under. An id that is not a UUID is
    skipped, so a malformed id can never select other files. Returns the
    paths released; a path that cannot be released is logged and left in
    place.
    """
    projects = claude_projects_dir(config_dir)
    released: list[Path] = []
    for session_id in session_ids:
        try:
            name = str(uuid.UUID(str(session_id)))
        except ValueError:
            continue
        paths = sorted(projects.glob(f"*/{name}.jsonl"))
        paths += sorted(projects.glob(f"*/{name}"))
        for path in paths:
            try:
                if destination is None:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                else:
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.move(path, destination / path.name)
            except OSError as exc:
                logger.warning(
                    "could not release claude session transcript %s: %s",
                    path,
                    exc,
                )
            else:
                released.append(path)
    return released


def _release_held(
    held: set[str],
    lock: threading.Lock,
    destination: Path | None,
    config_dir: Path | None,
) -> None:
    with lock:
        session_ids = sorted(held)
        held.clear()
    release_transcripts(session_ids, destination, config_dir)


def _stream_events(stdout: str) -> list[dict]:
    """The JSON objects of a stream-json event stream, one per line."""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _session_ids(stdout: str, arguments: list[str]) -> set[str]:
    """The sessions one invocation touched: the id it was given, and every
    id its stream reports (a fork reports the new session it created)."""
    ids = {
        event["session_id"]
        for event in _stream_events(stdout)
        if isinstance(event.get("session_id"), str)
    }
    if "--session-id" in arguments:
        ids.add(arguments[arguments.index("--session-id") + 1])
    return ids


def _as_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


class ClaudeCodeClient:
    """Claude Code CLI client (headless ``claude -p``); subscription billing.

    The CLI authenticates with its own login — including a Claude
    Pro/Max subscription — so usage draws on the subscription's limits
    instead of per-token API billing. A Messages-API key is deliberately
    NOT passed through to the child process: if you want key-based
    billing, use the ``anthropic`` engine instead.

    Vision works by pointing the CLI's Read tool at the image files, and
    tool access is locked down to Read only: it is the one tool the model
    is offered and the one tool approved.

    **Pinned.** A pass runs on one CLI version (``_CliPin``). The CLI
    never updates itself during a call; the installed version is checked
    when a client starts, and the version each call's stream reports is
    checked when it returns. A mismatch pauses the pass
    (``CliVersionMismatch``) and the call's output is discarded, as it is
    when a call's stream reports offering the model any tool but Read
    (``CliToolsMismatch``).

    **Isolated.** Calls load none of the owner's Claude Code settings
    files, MCP servers or skills, inherit only the environment variables
    on an allowlist (``_PASSED_VARIABLES``: what runs the CLI, locates its
    config directory and reaches the API), pin compaction on, and run in a
    dedicated directory. The CLI's config directory is normalised once,
    when a client starts, and that absolute path is passed to the CLI, so
    the CLI and the engine read the same one (``claude_config_dir``); a
    relative HOME is refused (``CliHomeRelative``). A client neither
    starts nor calls while the CLI's global config sets environment
    variables of its own (``check_global_config``). So no local
    configuration of the owner's, and nothing a Claude Code session that
    launched the pass exports, shapes a call without showing in
    ``config_hash``. Three things the engine does not control still shape
    calls: the login, which decides the account; machine-managed
    settings, which are machine policy; and the account data the server
    sends, which the CLI caches in its global config and which can change
    the system-prompt text, the default model and the auto-compact
    windows. ``effective_config`` lists what the engine sets for a call
    besides the prompt, and ``config_hash`` fingerprints it for the run
    record and for the LLM cache's keys (ADR-0032), beside the text a call
    sends (``call_text``, ``system_prompt``). ``last_report`` gives what a
    call's stream reported: the model that answered and the CLI version.

    **Transcripts.** The CLI stores every session under its projects
    directory, and forks resume from there, so sessions stay persistent
    while they are in use. Then each transcript is moved into the run
    record or deleted (``transcript_destination``): a one-shot call's or
    a fork's as soon as the call returns, a conversation's when
    ``close_conversation`` or ``close`` is called, or at the latest when
    the client is garbage-collected or the process exits.
    """

    engine = "claude-code"
    # Only file reading is permitted — the engine must never execute
    # commands or write files on behalf of model output. It is the only
    # tool the model is offered (--tools) and the only one approved
    # (--allowedTools).
    ALLOWED_TOOLS = "Read"
    # Appended to the CLI's own system prompt for every call. Denied tool
    # calls still consume turns in -p mode, and a planning tool grabbed
    # mid-extraction can burn the whole cap, so everything is forbidden
    # except reading the files a prompt names.
    SYSTEM_PROMPT = (
        "You are running non-interactively inside a data pipeline. "
        "Do not use any tools except Read, and only on files named in "
        "the prompt. No TodoWrite, no task tracking, no searching. "
        "Produce the requested output directly in a single response."
    )
    # The directory calls run in, under the system temporary directory:
    # it holds nothing, and the CLI files these sessions under a project
    # of their own.
    WORKDIR_NAME = "yizkr-claude-code"

    def __init__(
        self,
        model: str | None = None,
        executable: str = "claude",
        timeout: int = 1200,
        max_turns: int = 30,
        effort: str | None = None,
        *,
        cli_version: str | None = None,
        transcripts: str | os.PathLike[str] | None = None,
    ) -> None:
        self.executable = executable
        self.model = (
            model if model is not None else os.environ.get("YIZKR_LLM_MODEL")
        )
        self.timeout = timeout
        self.max_turns = max_turns
        # reasoning effort (low..max). Adaptive-reasoning models ignore
        # MAX_THINKING_TOKENS, so this is the lever that stops mechanical
        # steps (transcription, extraction) burning 100k+ thinking tokens
        # per call while judgment steps (adjudication, critique) keep high.
        self.effort = (
            effort
            if effort is not None
            else os.environ.get("YIZKR_LLM_EFFORT")
        )
        self.workdir = Path(tempfile.gettempdir()) / self.WORKDIR_NAME
        self.transcripts = transcript_destination(transcripts)
        self._pin = _PIN
        self._pin.raise_if_paused()
        home = os.environ.get("HOME")
        if home and not os.path.isabs(home):
            raise CliHomeRelative(
                f"HOME is {home!r}, a relative path: the claude CLI would "
                "take it from its own working directory and the engine from "
                "the pipeline's, so they would look for the login, the "
                "global config and the sessions in different places. The "
                "pass is paused and makes no further calls; its completed "
                "outputs are kept. Set HOME to an absolute path, or unset "
                "it, then run the pass again."
            )
        # normalised once and passed to the CLI, so that the CLI and the
        # engine read the same config directory
        self.config_dir = claude_config_dir()
        check_global_config(self.config_dir)
        try:
            installed = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                env=self._child_env(),
            ).stdout.split()[0]
        except (OSError, IndexError) as exc:
            raise RuntimeError(
                "the Claude Code CLI ('claude') is not installed or not on "
                "PATH. Install it and log in once (a Claude Pro/Max "
                "subscription works) — being logged in is the explicit "
                "opt-in before artifact content leaves this system. "
                "For API-key billing use the 'anthropic' engine instead."
            ) from exc
        # the version every call of this client must report
        self.cli_version = self._pin.adopt(installed, cli_version)
        self.model_identifier = f"claude-code/{self.cli_version}" + (
            f":{self.model}" if self.model else ""
        )
        # what the stream reported about the calling thread's last call
        self._reports = threading.local()
        # open conversations: sessions that forks resume, so their
        # transcripts stay in place until the conversation is closed
        self._conversations: set[str] = set()
        self._conversations_lock = threading.Lock()
        weakref.finalize(
            self,
            _release_held,
            self._conversations,
            self._conversations_lock,
            self.transcripts,
            self.config_dir,
        )

    def _command(self) -> list[str]:
        command = [
            self.executable,
            "-p",
            # stream-json (not text): text mode prints only the FINAL
            # assistant message, silently dropping earlier turns when a
            # long answer spans several. We reassemble the full response
            # from every assistant event in the stream.
            "--output-format",
            "stream-json",
            "--verbose",  # required by the CLI for stream-json in -p mode
            "--allowedTools",
            self.ALLOWED_TOOLS,
            # every other tool out of the model's context, the ones server
            # account data can add among them
            "--tools",
            self.ALLOWED_TOOLS,
            "--max-turns",
            str(self.max_turns),
            # isolation from the owner's configuration: no settings files
            # (user, project or local, and with them no hooks, permission
            # rules, plugins or CLAUDE.md), no MCP servers beyond the empty
            # set given here, and no skills
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            # settings the CLI would otherwise take from its global config
            "--settings",
            json.dumps(
                _PINNED_SETTINGS, sort_keys=True, separators=(",", ":")
            ),
            "--append-system-prompt",
            self.SYSTEM_PROMPT,
        ]
        if self.model:
            command += ["--model", self.model]
        if self.effort:
            command += ["--effort", self.effort]
        return command

    def _child_env(self) -> dict[str, str]:
        """The allowlisted variables of this process's environment, with
        CLAUDE_CONFIG_DIR as normalised when the client started and the
        pinned settings on top. An empty variable is left out, so the CLI
        treats it as unset, as the helpers here do."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _PASSED_VARIABLES and value
        }
        env.pop("CLAUDE_CONFIG_DIR", None)
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env.update(_PINNED_ENVIRONMENT)
        return env

    @property
    def effective_config(self) -> dict[str, Any]:
        """What this client sets for its calls besides the prompt: the
        pinned CLI version, its arguments (the pinned settings among them;
        the executable's path and the per-call session arguments aside),
        the directory it runs in, and its environment: the variables pinned
        for every call and the names of those passed through. The values
        passed through (paths on this machine, the login, the proxy) are
        never included. Not covered: the login itself, machine-managed
        settings, and the machine."""
        return {
            "engine": "claude-code",
            "cli_version": self.cli_version,
            "arguments": self._command()[1:],
            "working_directory": str(self.workdir),
            "environment": {
                "set": dict(sorted(_PINNED_ENVIRONMENT.items())),
                "passed": sorted(_PASSED_VARIABLES),
            },
        }

    @property
    def config_hash(self) -> str:
        """SHA-256 of ``effective_config`` in canonical JSON: one value
        that changes whenever anything in it does."""
        return _config_hash(self.effective_config)

    @property
    def system_prompt(self) -> str:
        """What every call appends to the CLI's own system prompt."""
        return self.SYSTEM_PROMPT

    def call_text(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        """The text a one-shot call sends the CLI: the prompt, after a line
        that points the Read tool at each image by its absolute path."""
        if not image_paths:
            return prompt
        listing = "\n".join(str(Path(p).resolve()) for p in image_paths)
        return (
            "First use the Read tool to examine these image files:\n"
            f"{listing}\n\n{prompt}"
        )

    def last_report(self) -> dict[str, Any]:
        """What the stream reported about the calling thread's last
        successful call: the model that answered it, the CLI version and
        the session."""
        return dict(getattr(self._reports, "value", {}))

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        # An explicit fresh session id names the call's transcript, so it
        # is released even when the call fails before its stream reports
        # the id.
        return self._run(
            ["--session-id", str(uuid.uuid4())],
            self.call_text(prompt, image_paths),
        )

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        """First turn of a forkable session; the handle is its session id.

        The session's transcript stays in place for its forks until
        ``close_conversation`` or ``close``."""
        session_id = str(uuid.uuid4())
        response = self._run(["--session-id", session_id], prompt, hold=True)
        return response, session_id

    def fork(self, handle: Any, prompt: str) -> str:
        """One follow-up turn on a FORK of the session — the original
        session is never mutated, so every fork branches from the same
        prefix and forks never see each other's turns."""
        return self._run(["--resume", str(handle), "--fork-session"], prompt)

    def close_conversation(self, handle: Any) -> None:
        """Release a conversation's transcript once it will not be forked
        again."""
        with self._conversations_lock:
            self._conversations.discard(str(handle))
        release_transcripts([str(handle)], self.transcripts, self.config_dir)

    def close(self) -> None:
        """Release the transcripts of every conversation still open."""
        _release_held(
            self._conversations,
            self._conversations_lock,
            self.transcripts,
            self.config_dir,
        )

    def __enter__(self) -> ClaudeCodeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _run(
        self, extra_args: list[str], prompt: str, *, hold: bool = False
    ) -> str:
        """One CLI invocation. Its sessions' transcripts are released when
        it ends, unless ``hold`` keeps a successful call's session open
        for forking."""
        self._pin.raise_if_paused()
        check_global_config(self.config_dir)
        self.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stdout = ""
        held = False
        try:
            try:
                result = subprocess.run(
                    self._command() + extra_args,
                    input=prompt,  # stdin avoids ARG_MAX limits
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self.workdir,
                    env=self._child_env(),
                )
            except subprocess.TimeoutExpired as exc:
                stdout = _as_text(exc.stdout)
                raise
            stdout = result.stdout or ""
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-500:]
                raise RuntimeError(
                    f"claude CLI exited with {result.returncode}: "
                    f"{detail or 'no output'}"
                    " (not logged in? run `claude` interactively once)"
                )
            report: dict[str, Any] = {"cli_version": self.cli_version}
            for event in _stream_events(stdout):
                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                ):
                    self._pin.check(
                        event.get("claude_code_version"), self.cli_version
                    )
                    self._pin.check_tools(
                        event.get("tools"), self.ALLOWED_TOOLS
                    )
                    report = {
                        "model": event.get("model"),
                        "cli_version": event.get("claude_code_version")
                        or self.cli_version,
                        "session_id": event.get("session_id"),
                    }
            text = self._assemble_stream(stdout)
            if not text.strip():
                raise RuntimeError("claude CLI returned no output")
            self._reports.value = report
            held = hold
            return text
        finally:
            sessions = _session_ids(stdout, extra_args)
            with self._conversations_lock:
                if held:
                    self._conversations.update(sessions)
                    sessions = set()
                else:
                    # a fork never releases the conversation it resumed
                    sessions -= self._conversations
            release_transcripts(
                sorted(sessions), self.transcripts, self.config_dir
            )

    @staticmethod
    def _assemble_stream(stdout: str) -> str:
        """Concatenate assistant text from a stream-json event stream.

        A long answer can span several assistant turns; every turn's text
        is part of the response, so all of them are joined in order. Falls
        back to the terminal result event (or raw stdout) if no assistant
        events are present.
        """
        pieces: list[str] = []
        result_text: str | None = None
        for event in _stream_events(stdout):
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        pieces.append(block["text"])
            elif event.get("type") == "result":
                result_text = event.get("result")
        if pieces:
            # Join with no separator: when a turn resumes exactly where
            # the previous one stopped (possibly mid-token), direct
            # concatenation reconstructs the original text; any inserted
            # whitespace could corrupt a seam inside a JSON string/number.
            return "".join(pieces)
        if result_text:
            return result_text
        return stdout

    @staticmethod
    def available(executable: str = "claude") -> bool:
        return shutil.which(executable) is not None


def make_client(
    engine: str,
    *,
    effort: str | None = None,
    model: str | None = None,
    transcripts: str | os.PathLike[str] | None = None,
    cli_version: str | None = None,
) -> LlmClient:
    """CLI engine name -> client. Extend here when adding providers.

    ``effort`` is the claude-code reasoning level (low..max); the
    anthropic API path takes no effort setting, so only the claude-code
    client receives it. ``model`` overrides the model for this client (a
    per-phase override, e.g. a disciplined extraction model distinct from
    the run's main model); ``None`` inherits $YIZKR_LLM_MODEL.

    ``transcripts`` and ``cli_version`` apply to claude-code only: the
    directory its session transcripts are moved into (``None`` defers to
    $YIZKR_CLAUDE_TRANSCRIPTS, and unset that deletes them), and the CLI
    version the pass must run on (``None`` pins the version installed
    when the pass's first client starts).
    """
    if engine == "anthropic":
        return AnthropicClient(model=model)
    if engine == "claude-code":
        return ClaudeCodeClient(
            model=model,
            effort=effort,
            transcripts=transcripts,
            cli_version=cli_version,
        )
    raise ValueError(
        f"unknown LLM engine {engine!r}; available: anthropic (API key, "
        f"token billing), claude-code (CLI login, subscription billing) — "
        f"self-hosted adapters welcome: implement LlmClient"
    )


def open_cache(
    root: str | os.PathLike[str],
    *,
    refresh: Iterable[str] = (),
    epochs: Mapping[str, int] | None = None,
) -> Any:
    """The LLM cache under ``root``, for one run (ADR-0032).

    ``refresh`` names roles whose calls go to the engine once per key in
    this run even where the cache holds an answer, which then replaces
    it; ``epochs`` gives a role an epoch other than 0, so that its calls
    are keyed, and answered, afresh. Nothing is written until a call is
    answered."""
    from yizkr.pipeline.llmcache import EngineCache

    return EngineCache(root, refresh=refresh, epochs=epochs)


def cached_client(
    client: LlmClient,
    cache: Any,
    *,
    role: str,
    events: Callable[..., Any] | None = None,
) -> LlmClient:
    """``client`` with every call answered from ``cache`` (an `open_cache`
    result) when the same call was answered before, and otherwise made
    and kept. ``role`` names the pipeline step whose calls these are
    (transcribe, extract, critique): its epoch and refresh setting apply.
    ``events``, when given, receives an event for every hit, miss,
    rejection and adoption. The wrapper presents the conversational
    capability exactly when ``client`` has it, and adds `reject`
    (RejectingLlmClient)."""
    from yizkr.pipeline.llmcache import wrap

    return wrap(client, cache, role=role, events=events)
