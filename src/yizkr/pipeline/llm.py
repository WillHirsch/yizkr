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
  ``ClaudeCodeClient``). A call the subscription refuses for a limit
  never fails: the engine waits for the limit to lift, or pauses the
  pass (``UsageLimitReached``), and it paces its calls, from what the
  CLI reports of each usage window, to leave a reserve of it to
  development sessions — best-effort, since the engine paces only from
  the utilization the CLI reports past its own warning thresholds (see
  "Subscription limits" below).

Pick the model for either engine with $YIZKR_LLM_MODEL.

This module is also the client factory, and the only code that imports
the LLM cache (yizkr.pipeline.llmcache, ADR-0032): `open_cache` opens
one, and `cached_client` wraps an engine's client so that a call made
before with the same key is answered from it. The pipeline is written
against the client protocols below and never against the cache, so
without the wrapper it runs unchanged, calling the engine directly.
`observed_client` reports every call that reaches the engine as an
event, with what the call's stream reported of it (the model it ran on,
the tokens it used, how it ended: `stream_report`), or, from the Anthropic
API's client, the model that answered a call and why it stopped; a run's
clients have it with or without the cache, beneath the cache when there
is one, so a run's record of its engine calls does not depend on the
cache.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# the kinds of call a client makes: one-shot, the opening turn of a
# conversation, and a fork of one
COMPLETE = "complete"
OPEN = "open"
FORK = "fork"


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

    A transcription that fails the transcription check, or an answer cut
    off, at the engine's output limit (`at_output_limit`) or before its
    terminal sentinel (a chunk's, the critic's, a single-call
    extraction's), is rejected, so that the next identical call asks the
    engine again rather than being answered with the same text. Only a
    client that keeps responses has anything to reject; see
    `reject_response`.

    `reject` returns whether the response was one the client kept from an
    earlier call, rather than the engine's answer to this one. The engine
    has then not answered the call in this run: a caller that asks once
    more gets the engine's answer, as it would have without the keeping
    client, and every rejector does so once.
    """

    def reject(self, response: str, reason: str) -> bool: ...


def reject_response(client: Any, response: str, reason: str) -> bool:
    """Reject `response`, which `client` returned, when the client can
    take a rejection (the LLM cache's client can); an engine's own client
    keeps nothing, so there is nothing to do. Returns whether the response
    was a kept answer, which the engine has not given in this run: the
    caller then asks once more (RejectingLlmClient)."""
    if isinstance(client, RejectingLlmClient):
        return bool(client.reject(response, reason))
    return False


@runtime_checkable
class AttemptingLlmClient(Protocol):
    """Optional capability: a client that keeps answers tells one attempt
    at a call from another, so that each attempt keeps its own answer and
    a later run replays the attempts in the same order; see
    `for_attempt`."""

    def attempt(self, number: int) -> Any: ...


def for_attempt(client: Any, number: int) -> Any:
    """The client to make attempt `number` (from 0) of the same call with.

    A caller that asks again when it cannot use an answer, and falls back
    to something else after the last attempt, asks through this: behind
    the LLM cache each attempt is then answered, and kept, on its own, so
    the whole sequence, fallback included, replays as it went, an attempt
    the engine gave up on at its output limit (OutputLimitReached) among
    them. Any other client makes every attempt itself."""
    if isinstance(client, AttemptingLlmClient):
        return client.attempt(number)
    return client


@runtime_checkable
class ResumableLlmClient(Protocol):
    """Optional capability: a conversation (ConversationalLlmClient) can be
    kept after it is opened, and resumed later exactly as it was opened,
    without asking the engine again. `keep_conversation` gives the files
    that hold a conversation's session, by path relative to where the
    engine keeps them (none, for an engine that needs only the turns);
    `resume_conversation` returns a handle to fork from, rebuilt from the
    opening turn and those files, and raises when they cannot give one."""

    def keep_conversation(self, handle: Any) -> dict[str, bytes]: ...

    def resume_conversation(
        self, prompt: str, response: str, files: Mapping[str, bytes]
    ) -> Any: ...


class ConversationChanged(Exception):
    """A conversation had to be opened again and its opening turn came
    back different from the answer the caller built on. Every turn made
    from the old answer is void: the caller starts over from the opening
    turn, which now gives the new answer."""


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
        """What the API reported about the calling thread's last call: the
        model that answered it (`model`) and why the model stopped
        (`stop_reason`, verbatim: end_turn, max_tokens for an answer cut
        off at MAX_TOKENS, which at_output_limit and so the LLM cache's
        callers reject as cut off, refusal, pause_turn, stop_sequence...),
        each when the response gives it as a plain token. Every call
        clears it first, so a call that gave no answer (it failed before
        its request, the API raised, or answered with no text) leaves it
        empty and nothing of an earlier call is read as its own: what an
        ObservedClient puts in a call's event is that call's or none."""
        return dict(getattr(self._reports, "value", {}))

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        self._reports.value = {}
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
        self._reports.value = {}
        response = self._client.messages.create(
            model=self.model_identifier,
            max_tokens=self.MAX_TOKENS,
            messages=messages,
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        if not text.strip():
            raise RuntimeError("the Anthropic API returned no text")
        report = {
            name: value
            for name, value in (
                ("model", _token(getattr(response, "model", None), _MODEL_ID)),
                (
                    "stop_reason",
                    _token(
                        getattr(response, "stop_reason", None), _STOP_REASON
                    ),
                ),
            )
            if value is not None
        }
        self._reports.value = report
        return text

    @staticmethod
    def _conversation(prompt: str, response: str) -> list[dict]:
        return [
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
            },
        ]

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        """Stateless conversations: the handle IS the message prefix."""
        response = self._create(self._conversation(prompt, "")[:1])
        return response, self._conversation(prompt, response)

    def keep_conversation(self, handle: Any) -> dict[str, bytes]:
        """Nothing beyond the opening turn: the handle is its messages."""
        return {}

    def resume_conversation(
        self, prompt: str, response: str, files: Mapping[str, bytes]
    ) -> Any:
        """The conversation's messages, rebuilt from its opening turn: the
        same handle `open_conversation` gave, with no request."""
        return self._conversation(prompt, response)

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


class ModelUnsupported(PassPaused):
    """The API refused a call with status 400 because the claude CLI does
    not support the model the call names (unsupported_model): no call of
    that model can succeed on this CLI, so no document fails for it. The
    pass is paused instead, with no time to go on (`resumes_at` is None):
    the operator installs a CLI that runs the model, or configures one the
    CLI runs, and runs the pass again (for a scratch run, a new one, since
    either changes the settings it recorded). `model` names the model and
    `reason` gives the CLI's own words; a pause latched by an earlier
    call (_CliPin) gives its message alone."""

    resumes_at = None

    def __init__(
        self,
        message: str = "",
        *,
        model: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.reason = reason


class UsageLimitReached(PassPaused):
    """The claude subscription cannot serve the pass's next call within
    the longest wait the engine allows (see "Subscription limits" below):
    a usage window is used up, or used up to the reserve left for
    development sessions, the CLI kept refusing calls for a rate limit, or
    a call is reported to have run on usage credits. This stops dispatch
    only once that report arrives: the reported call, and any others
    already in flight when it lands, may already have used credits —
    only turning extra usage off on the account guarantees none does.

    `reason` names the hold (``USAGE_LIMIT``, ``RESERVE``, ``RATE_LIMIT``,
    ``OVERLOADED``, ``OVERAGE`` or ``CLOSED``), `window`
    the CLI's name for the usage window when one is known (``five_hour``,
    ``seven_day``, ...), and `resumes_at` (a UTC datetime) when the pass
    can go on: the time the CLI reported that the window reopens, or,
    when it reported none that is still ahead, the time the engine would
    have tried again — None when no time is known at all (``OVERAGE``: the
    subscription does not say when it clears). No call is refused for
    good, so no document fails for it: the pass unwinds, and is run again
    after `resumes_at`, or for ``OVERAGE``, once the operator has
    confirmed that extra usage is off on the account, since nothing local
    says when that clears.

    Once one call latches a pause (raises this from `_Pacer.admit`), every
    later call to `admit`, from any thread, raises the same pause at once,
    before any further hold or wait of its own, for as long as it holds
    (until its own `resumes_at`, or forever within the process for
    ``OVERAGE``): a pass that has paused makes no further calls, even
    from a pool with more than one already queued."""

    def __init__(
        self,
        message: str = "",
        *,
        reason: str | None = None,
        window: str | None = None,
        resumes_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.window = window
        self.resumes_at = resumes_at


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
# CLAUDE_CODE_HARBOR_KITE is the CLI's internal name for its cross-session
# messaging socket, and 0 turns it off (as the pinned 2.1.309 reads it):
# without it a telemetry-off -p call listens on a socket that the user's
# other Claude Code sessions can reach.
_PINNED_ENVIRONMENT = {
    "CLAUDE_CODE_HARBOR_KITE": "0",
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
    """The JSON objects of a stream-json event stream, one per line.

    Split on ``\\n`` alone, never `str.splitlines`: a model answer can
    hold U+2028, U+2029 or U+0085, each a line boundary to `splitlines`
    but not to the newline-delimited stream, and splitting on one there
    would break a line's JSON in two, so both halves fail to parse and
    the event is silently dropped, up to the terminal result event
    itself. The per-line `strip` still removes an incidental trailing
    ``\\r`` (CRLF) or leading or trailing space, since it only touches
    each line's own ends, never a character in its middle. A line nested
    too deeply for the decoder to recurse through is dropped the same way
    as one that fails to parse outright."""
    events = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
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


# -- what a call's stream reports ---------------------------------------------
# A claude-code call's stream-json output (as the pinned 2.1.309 prints it)
# opens with a system event of subtype init and ends with a result event:
#
# * the init event names the model the CLI runs the call on (model: the id
#   --model names, or the model the CLI resolves an alias or its own default
#   to), the CLI's version (claude_code_version) and the session the call
#   runs in (session_id);
# * the result event says how the call ended (subtype: success,
#   error_max_turns, error_during_execution, ...; the CLI reports an API
#   error as a success with is_error), why the model stopped generating on
#   its final turn (stop_reason: end_turn; max_tokens when that turn hit
#   the output token limit; refusal, ...), how many turns it took
#   (num_turns; 2.1.309 counts its own retries at the output limit among
#   them) and the tokens it used (usage: input_tokens, output_tokens,
#   cache_creation_input_tokens and cache_read_input_tokens, beside details
#   this engine does not keep);
# * an assistant event that stands for an API error names its class (its
#   error: max_output_tokens, rate_limit, overloaded, ...), and the last
#   assistant event's is the call's (api_error).
#
# The stop reason is the result event's: an assistant event is printed as
# its content block completes, before the API's message_delta says why the
# model stopped, so its message's stop_reason tells nothing of it.
#
# When its model's answer passed the output token limit (at a limit of 40
# tokens), the pinned CLI handed back no answer. Its stream ended with an
# assistant event whose error is max_output_tokens (OUTPUT_LIMIT_ERROR),
# holding an API-error notice as its one text block, and a result that
# reports is_error, with the notice as its result and stop_sequence as its
# stop reason. Such a stream is read by its structure, never by the
# notice's text (output_limit_reached), and the call raises
# OutputLimitReached. A stop reason of max_tokens on a result that answers
# is read too (at_output_limit): either way the answer is cut off.
#
# A stream cut off before its result event (a timeout, a crash) reports no
# usage, turns, subtype or stop reason, and one cut off before its init
# event names no model. Each field is kept only in the form expected of it,
# since each flows into cache entries, call events and alerts: a model id,
# a subtype, a stop reason or an error class that is not a plain token, a
# count that is not a whole number of at least 0, and a session that is not
# a UUID are left out.

# the token counts of a call's usage, by the CLI's names for them
USAGE_TOKENS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# the fields of a stream report (stream_report), besides the usage
STREAM_FIELDS = (
    "model",
    "cli_version",
    "session_id",
    "num_turns",
    "subtype",
    "stop_reason",
    "api_error",
)

# a model id (claude-sonnet-5-20260901, claude-opus-5-5[1m]), a CLI version
# (2.1.309), a result subtype (error_max_turns), a stop reason (max_tokens)
# and an API error's class (max_output_tokens), as plain tokens
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_SUBTYPE = re.compile(r"[a-z0-9_]{1,64}")
_STOP_REASON = re.compile(r"[a-z0-9_]{1,64}")
_ERROR = re.compile(r"[a-z0-9_]{1,64}")

# the stop reason an engine reports of an answer it cut off at its output
# limit (at_output_limit): the API on its response, and a CLI that answers
# on its result event
OUTPUT_LIMIT = "max_tokens"
# the class of API error the claude CLI's last assistant event names when
# it gave up on an answer at the output token limit (output_limit_reached)
OUTPUT_LIMIT_ERROR = "max_output_tokens"
# what a caller says of an answer cut off at the output limit, the one it
# rejects or the one the CLI gave up on (OutputLimitReached)
CUT_OFF_AT_LIMIT = f"cut off at {OUTPUT_LIMIT}"


def _token(value: Any, pattern: re.Pattern[str]) -> str | None:
    """`value` when it is a string that `pattern` matches whole, else
    None."""
    return (
        value if isinstance(value, str) and pattern.fullmatch(value) else None
    )


def _whole(value: Any) -> int | None:
    """A whole number of at least 0 from a stream, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _session_id(value: Any) -> str | None:
    """A session id from a stream, in the UUID form the CLI files its
    transcript under, or None."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _last_event(
    events: Sequence[Mapping[str, Any]], kind: str
) -> Mapping[str, Any]:
    """The last event of type `kind` in a stream, or an empty one."""
    return next(
        (event for event in reversed(events) if event.get("type") == kind),
        {},
    )


def stream_report(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """What a call's stream-json events report of the call: from its init
    event, the `model` it ran on, the `cli_version` and the `session_id`;
    from its result event, its `usage` (each of USAGE_TOKENS the event
    gives), `num_turns`, `subtype` and `stop_reason`; and from its last
    assistant event, the class of API error it stands for (`api_error`),
    when it stands for one. A field the stream does not give, or gives in
    another form than expected, is left out; so is a usage that gives no
    token count. The session is the init event's, else the result event's.
    Of several, the first init event counts, and the last result event, the
    one the call ended on."""
    events = list(events)
    init = next(
        (
            event
            for event in events
            if event.get("type") == "system" and event.get("subtype") == "init"
        ),
        {},
    )
    result = _last_event(events, "result")
    assistant = _last_event(events, "assistant")
    usage = result.get("usage")
    tokens = {
        name: count
        for name in USAGE_TOKENS
        if isinstance(usage, dict)
        and (count := _whole(usage.get(name))) is not None
    }
    report = {
        "model": _token(init.get("model"), _MODEL_ID),
        "cli_version": _token(init.get("claude_code_version"), _VERSION),
        "session_id": _session_id(init.get("session_id"))
        or _session_id(result.get("session_id")),
        "usage": tokens or None,
        "num_turns": _whole(result.get("num_turns")),
        "subtype": _token(result.get("subtype"), _SUBTYPE),
        "stop_reason": _token(result.get("stop_reason"), _STOP_REASON),
        "api_error": _token(assistant.get("error"), _ERROR),
    }
    return {name: value for name, value in report.items() if value is not None}


def _add_report(call: dict[str, Any], attempt: Mapping[str, Any]) -> None:
    """Take one attempt's stream report (stream_report) into its call's,
    attempt by attempt: the call's fields are those of its latest attempt,
    which say what that attempt ran on and how it ended, and a field that
    attempt did not report is unknown, whatever an earlier one reported;
    the usage is summed over all of them, since each used what it
    reports."""
    usage = dict(call.get("usage") or {})
    for name, count in (attempt.get("usage") or {}).items():
        usage[name] = usage.get(name, 0) + count
    call.clear()
    call.update(
        {name: attempt[name] for name in STREAM_FIELDS if name in attempt}
    )
    if usage:
        call["usage"] = usage


def output_limit_reached(events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a call's stream-json events say the claude CLI gave up on
    its answer at the output token limit: its result reports is_error, and
    its last assistant event names the API error max_output_tokens
    (OUTPUT_LIMIT_ERROR). The structure decides, never the text of the
    notice that event holds."""
    return (
        _last_event(events, "result").get("is_error") is True
        and _last_event(events, "assistant").get("error") == OUTPUT_LIMIT_ERROR
    )


class OutputLimitReached(RuntimeError):
    """The claude CLI gave up on a call's answer at the output token limit
    (output_limit_reached), and handed back none. It is an answer cut off
    at the limit, not a failure of the engine: a caller handles it as it
    handles an answer cut off (at_output_limit), with nothing of it to
    keep, and the anomaly checks count it as a truncation, never as a
    failed call. Behind the LLM cache, an attempt asked through
    `for_attempt` keeps it under the attempt's key, as it keeps a refused
    answer, so a later run raises it again without asking the engine and
    takes the same fallback; any other call keeps nothing of it, so the
    next call asks the engine again."""

    def __init__(self, message: str = CUT_OFF_AT_LIMIT) -> None:
        super().__init__(message)


# how many clients deep at_output_limit looks for the engine's report: a
# cached client over an observed one over the engine's, and one wrapper more
_REPORTERS = 4


def at_output_limit(client: Any, response: str) -> bool:
    """Whether the engine cut `response` off at its output limit: whether
    the stop reason the engine reported of the call that made it is
    max_tokens (OUTPUT_LIMIT), as the Anthropic API says on its response
    and a CLI that answers may say on its result. `response` is the answer
    `client` has just given the calling thread, and the stop reason is what
    the engine's report of that thread's last answered call gives
    (`last_report`, which an ObservedClient passes on), read from `client`,
    else from the client it makes its calls through (its `client`, as the
    LLM cache's has), and so on down, _REPORTERS deep at most.

    Such an answer is incomplete whatever it holds, a terminal sentinel
    included: its caller handles it as an answer without its sentinel. The
    pinned claude CLI gives no such answer: the call raises
    OutputLimitReached, which its callers handle alike. An answer the LLM
    cache gave from an entry (a hit, an adoption among them) was made by no
    call of the engine's now: it is judged by the stop reason the entry
    keeps (its `stop_reason`), so a kept answer the engine cut off is cut
    off when served again, and is rejected as a fresh one is. An entry
    that keeps no stop reason, and the answer of a client that reports
    none, are judged by their text alone."""
    if getattr(response, "hit", False):
        return getattr(response, "stop_reason", None) == OUTPUT_LIMIT
    reporter = client
    for _ in range(_REPORTERS):
        last_report = getattr(reporter, "last_report", None)
        if callable(last_report):
            report = last_report()
            return (
                isinstance(report, Mapping)
                and report.get("stop_reason") == OUTPUT_LIMIT
            )
        reporter = getattr(reporter, "client", None)
    return False


# -- tool errors ------------------------------------------------------------
# A claude-code call's stream reports each tool call the model makes (a
# tool_use block of an assistant event: its id, the tool's name and its
# arguments) and each call's result (a tool_result block of a user event:
# the call's id, is_error when the tool failed, and the error's text as its
# content). The engine reads that record for the files a call is given, so
# that a Read that could not open or render one of them is found whatever
# the model then writes (ClaudeCodeClient.complete).

# the one tool the claude-code engine offers the model
READ_TOOL = "Read"

# The two kinds of error a Read reports. A request error refuses the form
# of the request before anything is read: the tool asks for a page range,
# a shorter one or one within the document, so another Read of the same
# file that succeeds makes up for it. A file error says that the file
# could not be found, opened or rendered, and nothing makes up for it.
REQUEST_ERROR = "request"
FILE_ERROR = "file"
# the class of an error no pattern below recognises, taken as a file error
UNCLASSIFIED = "unclassified"

# The Read tool's errors, recognised by the words of the claude CLI's own
# messages (as CLI 2.1.309 words them), each with the class a failure is
# named by and its kind. The first pattern that matches gives the class,
# so the request errors come first: the message refusing to read a whole
# PDF also names the PDF renderer. A request error the CLI words
# differently is unclassified, and fails the call until its words are
# added here: a stricter reading, never a looser one.
_READ_ERRORS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (name, kind, re.compile(pattern, re.IGNORECASE))
    for name, kind, pattern in (
        # a PDF of more pages than one Read takes, asked for without a range
        ("too-many-pages", REQUEST_ERROR, r"too many to read at once"),
        # a range that runs past the document's last page
        ("page-out-of-range", REQUEST_ERROR, r"is outside the document"),
        # a range longer than one Read takes
        (
            "page-range-too-long",
            REQUEST_ERROR,
            r"exceeds maximum of \d+ pages per request",
        ),
        # a pages value the tool does not take, a list of pages among them
        ("invalid-page-range", REQUEST_ERROR, r"Invalid pages parameter"),
        # a whole PDF, which the model cannot take as a document; a range is
        # rendered page by page instead
        (
            "whole-pdf-unsupported",
            REQUEST_ERROR,
            r"Reading full PDFs is not supported",
        ),
        # a text too long for one Read, which the tool offers in parts
        ("too-many-tokens", REQUEST_ERROR, r"exceeds maximum allowed tokens"),
        # arguments that do not fit the tool's schema
        ("invalid-input", REQUEST_ERROR, r"InputValidationError"),
        (
            "pdf-renderer-missing",
            FILE_ERROR,
            r"pdftoppm is not installed|requires poppler-utils",
        ),
        ("missing", FILE_ERROR, r"File does not exist|No such file|ENOENT"),
        ("permission-denied", FILE_ERROR, r"permission|EACCES|EPERM"),
        ("empty", FILE_ERROR, r"\bis empty\b"),
        (
            "too-large",
            FILE_ERROR,
            r"exceeds maximum allowed size|too large to read as an image",
        ),
        ("password-protected", FILE_ERROR, r"password-protected"),
        # a format the tool does not read (TIFF, say), or a device, pipe or
        # process file
        (
            "unsupported-format",
            FILE_ERROR,
            r"cannot read binary files|would block or produce infinite output"
            r"|names a device or process file|is a pipe or a device",
        ),
        (
            "corrupt",
            FILE_ERROR,
            r"not a valid|corrupted or invalid|reports 0 pages"
            r"|produced no output pages|not a regular file",
        ),
        (
            "render-failed",
            FILE_ERROR,
            r"Could not render PDF|pdftoppm failed|PDF extraction"
            r"|extracted PDF page",
        ),
    )
)


def read_error(message: str) -> tuple[str, str]:
    """The class and the kind (REQUEST_ERROR or FILE_ERROR) of an error the
    Read tool reported in `message`."""
    for name, kind, pattern in _READ_ERRORS:
        if pattern.search(message):
            return name, kind
    return UNCLASSIFIED, FILE_ERROR


@dataclass(frozen=True)
class ToolFailure:
    """A tool the engine's stream reports failing on one of a call's input
    files, with nothing making up for it: the tool's name, the class of its
    error (see read_error) and the input's position among the files the
    call was given. Nothing of the error's own text is kept: a class says
    what failed, and the text can name paths."""

    tool: str
    error: str
    input: int


def describe_tool_failures(failures: Iterable[ToolFailure]) -> str:
    """Each distinct tool and error class, as "Read (corrupt)"."""
    return ", ".join(dict.fromkeys(f"{f.tool} ({f.error})" for f in failures))


class EngineToolError(RuntimeError):
    """The engine's stream reports that a tool failed on a file the call
    gave it (`failures`), with nothing making up for it. The answer is then
    not a reading of that file, whatever it says, so the call fails rather
    than return it. `response` is that answer, for a caller that records
    what it held. The message names the tools and the classes of their
    errors, never a path, an error's text or the answer."""

    def __init__(self, failures: Sequence[ToolFailure], response: str) -> None:
        self.failures = tuple(failures)
        self.response = response
        super().__init__(
            "the engine's stream reports a tool failing on the call's "
            f"input: {describe_tool_failures(self.failures)}"
        )


def _path_forms(path: str | os.PathLike[str]) -> set[str]:
    """The absolute spellings of a file's path a tool call may use."""
    return {os.path.abspath(path), str(Path(path).resolve())}


def _named_input(arguments: Any, inputs: Mapping[str, int]) -> int | None:
    """The input a tool call's arguments name by its absolute path, if
    any."""
    if not isinstance(arguments, dict):
        return None
    for value in arguments.values():
        if isinstance(value, str) and os.path.isabs(value):
            for form in (os.path.normpath(value), str(Path(value).resolve())):
                if form in inputs:
                    return inputs[form]
    return None


def _result_text(content: Any) -> str:
    """The text of a tool result's content: a string, or text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def tool_failures(
    stdout: str, inputs: Sequence[str | os.PathLike[str]]
) -> list[ToolFailure]:
    """The Read tool's failures on the files a call was given (`inputs`),
    as the call's stream-json output `stdout` reports them.

    A Read that names one of the files by its path and whose result is an
    error is a failure, unless its error is a request error (read_error)
    and another Read of the same file succeeded: the tool refused the form
    of one request, and the file was read. Reads of other files, and calls
    of other tools, are not about the call's input and are left out. Each
    distinct failure is listed once, in the order of the stream."""
    targets: dict[str, int] = {}
    for index, path in enumerate(inputs):
        for form in _path_forms(path):
            targets.setdefault(form, index)
    calls: dict[str, int] = {}
    outcomes: list[tuple[int, tuple[str, str] | None]] = []
    for event in _stream_events(stdout):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            call_id = block.get("id")
            result_of = block.get("tool_use_id")
            if (
                event.get("type") == "assistant"
                and block.get("type") == "tool_use"
                and block.get("name") == READ_TOOL
                and isinstance(call_id, str)
            ):
                target = _named_input(block.get("input"), targets)
                if target is not None:
                    calls[call_id] = target
            elif (
                event.get("type") == "user"
                and block.get("type") == "tool_result"
                and isinstance(result_of, str)
                and result_of in calls
            ):
                error = (
                    read_error(_result_text(block.get("content")))
                    if block.get("is_error") is True
                    else None
                )
                outcomes.append((calls[result_of], error))
    read = {target for target, error in outcomes if error is None}
    failures = [
        ToolFailure(READ_TOOL, error[0], target)
        for target, error in outcomes
        if error is not None
        and not (error[1] == REQUEST_ERROR and target in read)
    ]
    return list(dict.fromkeys(failures))


# -- subscription limits ----------------------------------------------------
# A claude-code call draws on the subscription's usage windows: a session
# window of five hours and weekly ones, each reopening at a set time. The
# CLI (as the pinned 2.1.309 prints it) reports them in its stream-json
# output:
#
# * a rate_limit_event whenever its view of the limits changes, which in a new
#   process means with the first response of every call. Its rate_limit_info
#   gives the status (allowed, allowed_warning or rejected), the window that
#   binds (rateLimitType: five_hour, seven_day, seven_day_opus, ...), when that
#   window reopens (resetsAt, Unix seconds), whether the call ran on usage
#   credits beyond the subscription (isUsingOverage), and the fraction of the
#   window used (utilization) — but only once that fraction passes one of the
#   CLI's own warning thresholds: for the five-hour window, 90%, and only while
#   at most 72% of it has elapsed; for the weekly window, 75%, 50% or 25%,
#   early in the week. Short of that threshold, or past its elapsed-time gate,
#   rate_limit_info carries no utilization. Every rate_limit_event also carries
#   unifiedWindows, the use of the five_hour and seven_day windows
#   ({utilization, resetsAt} each) whatever the thresholds, and, when the
#   server reports one, limitScope; the engine reads neither. An
#   allowed_warning report whose window (rateLimitType) is not overage may also
#   say whether usage credits are enabled on the account (overageEnabled),
#   which the engine does not read either, nor the offers rate_limit_info
#   may carry for the CLI's own limit banner (usageCreditOffer,
#   adminRequestOffer);
# * a call the API refuses for a limit ends its turn on an API error: an
#   assistant event whose error is rate_limit (overloaded, for an
#   overload) and whose text is the CLI's notice ("You've hit your
#   session limit · resets 3pm"), then a result event with is_error true
#   and api_error_status 429 (529, for an overload); the process exits
#   with 1. A used-up window is also reported rejected, with its
#   rateLimitType and resetsAt. A refusal of another kind (the server
#   limiting requests, which the CLI has already retried with a backoff
#   of its own) reports no reset time.
#
# Not reported: the size of a window in calls or tokens, and how much of
# it other sessions use (the reported utilization includes their use).
#
# So the engine, through the process's _Pacer:
#
# * never returns a refused call's notice as an answer. It asks again:
#   after the reported reset of a used-up window, and after a doubling
#   backoff when a refusal gives no reset time, or one the local clock has
#   passed already (a skewed clock). A hold that would take a call's wait
#   past the longest the call may wait in all (max_wait,
#   $YIZKR_CLAUDE_MAX_WAIT) pauses the pass instead (UsageLimitReached),
#   with the time it can go on. A hold that one call meets holds every
#   thread's next call;
# * keeps a reserve of each window for development sessions (reserve,
#   $YIZKR_CLAUDE_RESERVE, a fraction of the window). The use of a window
#   is estimated from its last reported utilization, plus the use of each
#   call completed or in flight since, at the use per call measured
#   between the first report in the window and the latest. Dispatch
#   stops at the reserve line until the window reopens; short of it,
#   calls go at full speed, never spaced, so the work is done as soon as
#   the window allows. It reads only rate_limit_info's utilization, never from
#   unifiedWindows, so it is best-effort, not a guarantee: below
#   the CLI's first warning it has no use to go by, so the reserve gives
#   no protection there, and a first warning that arrives late (close to
#   100% already used, or past its elapsed-time gate) can give little or
#   none either. The use per call includes what other sessions used in the
#   same time, which brings the line sooner, never later;
# * pauses dispatch process-wide the moment a report says a call ran on usage
#   credits, whatever the reserve, whatever window it names or fails to, and
#   whatever else it says or leaves out. This only stops dispatch once that
#   report arrives: the reported call, and any others already in flight when it
#   lands, may already have used credits — only turning extra usage off on the
#   account guarantees none does. Nothing local says when that clears, so only
#   a fresh process resumes.
#
# The reserve and the longest wait decide only when calls are made, never
# what a call sends: they are no part of effective_config, so no cache key
# depends on them.

RATE_LIMIT_EVENT = "rate_limit_event"
# the statuses a rate_limit_event reports
ALLOWED = "allowed"
ALLOWED_WARNING = "allowed_warning"
REJECTED = "rejected"
_STATUSES = frozenset({ALLOWED, ALLOWED_WARNING, REJECTED})
# the API statuses and the assistant events' errors of a refusal for a
# limit: 429 and rate_limit, a rate or usage limit; 529 and overloaded,
# an overload
LIMIT_STATUSES = frozenset({429, 529})
LIMIT_ERRORS = frozenset({"rate_limit", "overloaded"})

# Why a call waited, or its pass paused: a used-up usage window; a window
# used up to the reserve; a refusal with no reset time the local clock has
# not passed; an overload; a report that a call ran on usage credits
# (whatever else that report said, or left out, of the window it named);
# and a wait the client's close ended.
USAGE_LIMIT = "usage limit"
RESERVE = "reserve"
RATE_LIMIT = "rate limit"
OVERLOADED = "overloaded"
OVERAGE = "usage credits"
CLOSED = "closed"

# the statuses' severity, for a window's status to move only up it: a
# later report may raise it (towards a used-up window) but never lower
# it back down
_STATUS_RANK = {ALLOWED: 0, ALLOWED_WARNING: 1, REJECTED: 2}

# the settings, and their defaults: a quarter of each window is left to
# development sessions, and a call waits up to 16 minutes in all, which
# covers a backoff and a window that reopens soon; a longer hold pauses
# the pass. The margin above the backoff sequence's own sum (60 + 120 +
# 240 + 480 = 900 s) keeps the pause on the schedule's own cap
# (BACKOFF_CAP, once every doubling since has been capped), never on an
# accidental equality with the default itself.
RESERVE_ENV = "YIZKR_CLAUDE_RESERVE"
MAX_WAIT_ENV = "YIZKR_CLAUDE_MAX_WAIT"
DEFAULT_RESERVE = 0.25
DEFAULT_MAX_WAIT = 960.0
# the backoff after a refusal with no reset time ahead: a minute, doubled
# with each refusal of the same call, up to the cap
BACKOFF = 60.0
BACKOFF_CAP = 480.0
# how long after a window's reported reset the engine asks again
RESET_MARGIN = 30.0
# reset times this close together name the same window
SAME_WINDOW = 60.0

_WINDOW_NAMES = {
    "five_hour": "five-hour window",
    "seven_day": "weekly window",
    "seven_day_opus": "weekly Opus window",
    "seven_day_sonnet": "weekly Sonnet window",
    "seven_day_oauth_apps": "weekly window for apps signed in with OAuth",
    "seven_day_cowork": "weekly Cowork window",
}


def _describe_window(name: str | None) -> str:
    if not name:
        return "usage window"
    return _WINDOW_NAMES.get(name, f"{name} window")


def _number(value: Any) -> float | None:
    """A finite number from a stream, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _epoch(value: Any) -> float | None:
    """A Unix time in seconds from a stream, or None."""
    number = _number(value)
    return number if number is not None and number > 0 else None


def _fraction(value: Any) -> float | None:
    """A share of a window from a stream, or None."""
    number = _number(value)
    return number if number is not None and number >= 0 else None


# every rateLimitType CLI 2.1.309 reports (five_hour, seven_day, ...) is
# a lowercase, underscore-separated token; anything else is unknown,
# since a window name flows unescaped into log lines and call events
_WINDOW_NAME = re.compile(r"[a-z0-9_]+")


def _window_name(value: Any) -> str | None:
    """A window name (rateLimitType) from a stream, or None."""
    return (
        value
        if isinstance(value, str) and _WINDOW_NAME.fullmatch(value)
        else None
    )


def _utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _stamp(seconds: float) -> str:
    return _utc(seconds).isoformat(timespec="seconds")


def _duration(seconds: float) -> str:
    """A duration for a message: "45 s", "15 min", "2 h 5 min"."""
    if seconds == math.inf:
        return "without limit"
    seconds = max(0, round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    hours, minutes = divmod(round(seconds / 60), 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h" + (f" {minutes} min" if minutes else "")


def rate_limit_infos(events: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The rate_limit_info of each rate_limit_event in a call's stream-json
    events, in their order."""
    return [
        event["rate_limit_info"]
        for event in events
        if event.get("type") == RATE_LIMIT_EVENT
        and isinstance(event.get("rate_limit_info"), dict)
    ]


@dataclass(frozen=True)
class Refusal:
    """A call the subscription refused for a limit: why (USAGE_LIMIT,
    RATE_LIMIT or OVERLOADED), and for a used-up window its name and the
    time the CLI says it reopens (Unix seconds)."""

    reason: str
    window: str | None = None
    resets_at: float | None = None


def refusal(
    events: Sequence[Mapping[str, Any]], returncode: int
) -> Refusal | None:
    """The limit that refused a call, from the call's stream-json events
    and exit status, or None when no limit refused it.

    A call that ended with an explicit non-error result (a zero exit, and
    a terminal result event that confirms is_error is false) was not
    refused, whatever its stream reported on the way: the CLI retries
    some refusals itself. A zero exit whose result event is missing or
    silent on is_error is never read as that confirmation: the stream was
    cut off before its terminal event, and falls through to the checks
    below like any other exit, so a refusal it did carry (a rejected
    rate_limit_event, a limit error) is still read as one instead of
    being assembled and cached as the model's answer. One that did was
    refused for a limit when its result has the API status 429 or 529, an
    assistant event carries a limit error, or its last rate_limit_event is
    rejected. The refusal is for a used-up window when that event names
    the window and when it reopens; otherwise for an overload (529), or a
    rate limit."""
    result = next(
        (event for event in reversed(events) if event.get("type") == "result"),
        {},
    )
    if returncode == 0 and result.get("is_error") is False:
        return None
    status = result.get("api_error_status")
    errors = {
        event.get("error")
        for event in events
        if event.get("type") == "assistant"
        and isinstance(event.get("error"), str)
    }
    infos = rate_limit_infos(events)
    last = infos[-1] if infos else {}
    rejected = last.get("status") == REJECTED
    if not (status in LIMIT_STATUSES or errors & LIMIT_ERRORS or rejected):
        return None
    window = _window_name(last.get("rateLimitType"))
    resets_at = _epoch(last.get("resetsAt"))
    if rejected and window and resets_at:
        return Refusal(USAGE_LIMIT, window, resets_at)
    if status == 529 or "overloaded" in errors:
        return Refusal(OVERLOADED)
    return Refusal(RATE_LIMIT)


# The API status of a request the API refuses as invalid, and the words
# with which it says, in such a refusal, that the claude CLI does not
# support the model the request names. Each wording is narrow, so that a
# 400 about a feature, a tool or the API is not read as one: a dotted
# version required "or newer" (the version the model requires; not one an
# SDK or the API names), the CLI as the subject of "requires a newer" or
# "does not support", with at most its version between, a newer CLI
# required by name, or the CLI required by name at a dotted version "or
# newer". Wording 1 excludes only "sdk " and "api " directly
# before "version": another dotted version said to be required ("Python
# version 3.11 or newer") still matches. That false match only pauses the
# pass, which is safe: the operator reads its reason and runs the pass
# again. No wording has an unbounded gap, so a search takes time linear
# in the text. Another 400 is an error of the call like any other.
BAD_REQUEST = 400
_UNSUPPORTED_MODEL = re.compile(
    r"(?<![\w-])(?<!sdk )(?<!api )version \d+(?:\.\d+)+ or (?:newer|later)"
    r" is required"
    r"|\b(?:claude code|cli)(?: v?\d+(?:\.\d+)+)? (?:requires? (?:a )?newer"
    r"|(?:does not|doesn't) support)"
    r"|\brequires? (?:a )?newer (?:version of )?(?:claude code|cli)\b"
    r"|\brequires? (?:claude code|cli) (?:version )?v?\d+(?:\.\d+)+ or"
    r" (?:newer|later)\b",
    re.IGNORECASE,
)


def unsupported_model(
    events: Sequence[Mapping[str, Any]], returncode: int
) -> str | None:
    """The CLI's account of why the API refused a call, when it refused it
    with status 400 because the CLI does not support the call's model;
    else None. Read from the call's stream-json events and exit status: a
    call that ended with an explicit non-error result was not refused
    (as for `refusal`); one that was carries api_error_status 400 on its
    result event, and the words _UNSUPPORTED_MODEL matches in its result
    or in the text of an assistant event that stands for the API error
    (one with an `error` or marked is_api_error_message), never in the
    model's own text, which may quote anything. The account is one line,
    of at most 300 characters."""
    result = _last_event(events, "result")
    if returncode == 0 and result.get("is_error") is False:
        return None
    if result.get("api_error_status") != BAD_REQUEST:
        return None
    texts = [result.get("result")]
    for event in events:
        message = event.get("message")
        if (
            event.get("type") == "assistant"
            and isinstance(message, dict)
            and (
                isinstance(event.get("error"), str)
                or event.get("is_api_error_message") is True
            )
        ):
            content = message.get("content")
            texts += [
                block.get("text")
                for block in (content if isinstance(content, list) else [])
                if isinstance(block, dict)
            ]
    for text in texts:
        if isinstance(text, str) and _UNSUPPORTED_MODEL.search(text):
            return " ".join(text.split())[:300]
    return None


class _Refused(Exception):
    """An attempt at a call that the subscription refused for a limit;
    ClaudeCodeClient asks again, or pauses the pass."""

    def __init__(self, refused: Refusal) -> None:
        super().__init__(refused.reason)
        self.refusal = refused


@dataclass
class _Window:
    """What the calls' streams last said of one usage window."""

    status: str | None = None
    resets_at: float | None = None
    # the share of the window used, as last reported, and the pacer's
    # count of completed calls when it was
    utilization: float | None = None
    observed_at: int = 0
    # the first share reported in the window and the count then, and the
    # use per call measured from them
    anchor: tuple[float, int] | None = None
    per_call: float | None = None


@dataclass(frozen=True)
class _Hold:
    """Until when dispatch waits (Unix seconds), why, and for which window
    and its reported reset, when a window is the reason."""

    until: float
    reason: str
    window: str | None = None
    resets_at: float | None = None


def _sleep(seconds: float, stop: threading.Event) -> bool:
    """Wait `seconds`; False when `stop` is set before or during it."""
    return not stop.wait(seconds)


class _Pacer:
    """When the next claude-code call may be dispatched, from what the
    calls' streams report of the subscription's usage windows (see
    "Subscription limits" above). One pacer serves every claude-code
    client of a process, since the windows are the account's: a hold one
    call meets holds every thread's next call. `clock` gives Unix
    seconds, and `sleep(seconds, stop)` waits, returning False when `stop`
    was set before or while it waited.

    A pause latches, as a CLI version mismatch does for `_CliPin`: once
    one call's admit raises UsageLimitReached, every later call to admit,
    from any thread, raises the very same pause at once, before it
    computes any hold or waits for one, for as long as the pause holds
    (its own `until`) — the overage latch never lifts within the
    process, since the subscription never says when it clears. Without
    this, a chunk pool whose executor isolates one work item's exception
    from the rest (as ThreadPoolExecutor's does) would let an
    already-queued chunk's call through after the pass has paused, even
    at concurrency 1."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float, threading.Event], bool] = _sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        # the windows by the CLI's name for them
        self._windows: dict[str, _Window] = {}
        # the backoff after the latest refusal, until it ends
        self._backoff: _Hold | None = None
        # latched by any report that a call ran on usage credits,
        # whatever else that report said or left out: cleared only by a
        # fresh process, since the subscription never says when it ends
        self._overage = False
        # the pause a call latched, once one has, and the Unix time it
        # holds until (math.inf for the overage latch): re-raised by
        # every later admit until then; a fresh process starts over
        self._pause: UsageLimitReached | None = None
        self._pause_until = 0.0
        self._dispatched = 0
        self._completed = 0
        # the hold the log last announced
        self._announced: tuple | None = None

    def raise_if_paused(self) -> None:
        """Re-raise the pause an earlier admit latched, from any thread,
        for as long as it holds: once the clock passes the time it was
        raised for, it lifts, and a call computes a hold of its own
        again."""
        with self._lock:
            pause = self._pause
            if pause is None:
                return
            if self._clock() < self._pause_until:
                raise pause
            self._pause = None
            self._pause_until = 0.0

    def _latch(
        self, pause: UsageLimitReached, until: float
    ) -> UsageLimitReached:
        """Latch `pause`, until Unix time `until` (math.inf: never, within
        the process), unless a pause latched already; returns `pause`
        either way, so the caller raises what it computed itself."""
        with self._lock:
            if self._pause is None:
                self._pause = pause
                self._pause_until = until
        return pause

    def admit(
        self,
        record: dict[str, Any],
        *,
        reserve: float,
        max_wait: float,
        stop: threading.Event,
    ) -> None:
        """Wait until a call may be dispatched, and count it dispatched.
        `record` gathers what the call has waited (`waited`, in seconds)
        and why (`held`) over its attempts. A hold that would take the
        call's wait past `max_wait` raises UsageLimitReached at once,
        without waiting, and so does a wait that `stop` ends; either
        latches until the hold's own `until`, so every later call to
        admit raises the same pause at once (`raise_if_paused`), instead
        of computing a hold of its own, for as long as it lasts."""
        self.raise_if_paused()
        while True:
            with self._lock:
                now = self._clock()
                hold = self._hold(now, reserve)
                if hold is None or hold.until <= now:
                    self._dispatched += 1
                    return
            delay = hold.until - now
            record["held"] = hold.reason
            if record["waited"] + delay > max_wait:
                raise self._latch(
                    _paused(
                        hold,
                        now,
                        reserve=reserve,
                        max_wait=max_wait,
                        waited=record["waited"],
                    ),
                    hold.until,
                )
            self._announce(hold, reserve)
            slept = self._sleep(delay, stop)
            record["waited"] += max(0.0, self._clock() - now)
            if not slept:
                raise self._latch(
                    UsageLimitReached(
                        "the claude-code client was closed while a call "
                        f"waited for the subscription ({hold.reason}). The "
                        "pass is paused and makes no further calls; its "
                        "completed outputs are kept.",
                        reason=CLOSED,
                        window=hold.window,
                        resumes_at=(
                            _utc(hold.until)
                            if math.isfinite(hold.until)
                            else None
                        ),
                    ),
                    hold.until,
                )

    def done(self, infos: Iterable[Mapping[str, Any]]) -> None:
        """Count a dispatched call completed, however it ended, and take in
        what its stream reported of the windows (rate_limit_infos)."""
        with self._lock:
            self._completed += 1
            for info in infos:
                self._take(info)

    def refused(self, refused: Refusal, attempt: int) -> None:
        """Hold dispatch after a call's `attempt`-th refusal (from 1): until
        the reported reset of a used-up window, and in any case for a
        backoff that doubles with each refusal of the call."""
        with self._lock:
            now = self._clock()
            if refused.window is not None and refused.resets_at is not None:
                self._window(
                    refused.window, refused.resets_at
                ).status = REJECTED
            backoff = min(BACKOFF * 2 ** min(attempt - 1, 16), BACKOFF_CAP)
            if self._backoff is None or self._backoff.until < now + backoff:
                self._backoff = _Hold(
                    now + backoff,
                    refused.reason,
                    refused.window,
                    refused.resets_at,
                )

    # -- the windows ------------------------------------------------------

    def _window(self, name: str, resets_at: float | None) -> _Window:
        """The window `name`, started afresh when `resets_at` names another
        reset than the one it had."""
        window = self._windows.get(name)
        if window is None or (
            resets_at is not None
            and window.resets_at is not None
            and abs(resets_at - window.resets_at) > SAME_WINDOW
        ):
            window = self._windows[name] = _Window()
        if resets_at is not None:
            window.resets_at = resets_at
        return window

    def _take(self, info: Mapping[str, Any]) -> None:
        """Take in one call's rate_limit_info. A window's status and
        recorded utilization only move one way within it, towards a
        used-up window: a later report that says less (a lower status, a
        lower or missing utilization) never undoes what a higher one
        already showed, since the window is not less used than it last
        reported. Overage latches process-wide, ahead of any window and
        ahead of any window's own hold (_hold): it names the account, not
        one window, so it never depends on a window's recorded
        utilization."""
        if info.get("isUsingOverage") is True:
            self._overage = True
        name = _window_name(info.get("rateLimitType"))
        if name is None:
            return
        status = (
            info.get("status") if info.get("status") in _STATUSES else None
        )
        utilization = _fraction(info.get("utilization"))
        window = self._window(name, _epoch(info.get("resetsAt")))
        if status is not None and _STATUS_RANK[status] > _STATUS_RANK.get(
            window.status, -1
        ):
            window.status = status
        if utilization is None or (
            window.utilization is not None
            and utilization <= window.utilization
        ):
            return
        anchor = window.anchor
        if anchor is None:
            window.anchor = (utilization, self._completed)
        elif self._completed > anchor[1] and utilization > anchor[0]:
            window.per_call = (utilization - anchor[0]) / (
                self._completed - anchor[1]
            )
        window.utilization = utilization
        window.observed_at = self._completed

    def _used(self, window: _Window) -> float | None:
        """The estimated share of a window used, counting the calls
        completed since its last report and those in flight, or None when
        no use is reported."""
        if window.utilization is None:
            return None
        since = (self._completed - window.observed_at) + (
            self._dispatched - self._completed
        )
        return window.utilization + (window.per_call or 0.0) * max(0, since)

    def _hold(self, now: float, reserve: float) -> _Hold | None:
        """The latest hold on dispatch at `now`, or None. A window whose
        reset has passed is forgotten: its use starts afresh. Overage is
        checked ahead of the windows, and holds without end: unlike a
        window's own hold, it does not depend on a name, a reported reset
        or the local clock, so none of the ways those can be missing or
        stale (an overage report with no rateLimitType, no resetsAt, or
        one the clock has already passed) can let a call on usage credits
        through."""
        holds = []
        if self._overage:
            holds.append(_Hold(math.inf, OVERAGE))
        if self._backoff is not None:
            if self._backoff.until > now:
                holds.append(self._backoff)
            else:
                self._backoff = None
        line = 1.0 - reserve
        for name, window in list(self._windows.items()):
            if window.resets_at is None:
                continue
            if window.resets_at <= now:
                del self._windows[name]
                continue
            reopens = window.resets_at + RESET_MARGIN
            if window.status == REJECTED:
                holds.append(
                    _Hold(reopens, USAGE_LIMIT, name, window.resets_at)
                )
                continue
            used = self._used(window)
            if used is None:
                continue
            if used >= line:
                holds.append(_Hold(reopens, RESERVE, name, window.resets_at))
        return max(holds, key=lambda hold: hold.until, default=None)

    def _announce(self, hold: _Hold, reserve: float) -> None:
        """Say in the log, once per hold, why calls wait: once for a
        window's hold, however many calls wait for it, and once for each
        backoff."""
        announced = (
            hold.reason,
            hold.window,
            hold.resets_at,
            hold.until if hold.reason in (RATE_LIMIT, OVERLOADED) else None,
        )
        with self._lock:
            if announced == self._announced:
                return
            self._announced = announced
        if hold.reason == OVERAGE:
            logger.warning(
                "the claude subscription reports a call ran on usage "
                "credits: dispatch stops now, though that call and any "
                "already in flight may already have used them"
            )
            return
        window = f"the claude subscription's {_describe_window(hold.window)}"
        if hold.reason == USAGE_LIMIT:
            why = f"{window} is used up"
        elif hold.reason == RESERVE:
            why = (
                f"{window} is used up to the reserve left to development "
                f"sessions ({reserve:.0%})"
            )
        else:
            why = f"the claude CLI refused a call ({hold.reason})"
        logger.warning("%s: calls wait until %s", why, _stamp(hold.until))


def _paused(
    hold: _Hold,
    now: float,
    *,
    reserve: float,
    max_wait: float,
    waited: float,
) -> UsageLimitReached:
    """The pause for a hold that would take a call's wait past
    `max_wait`. It resumes when the window the CLI named reopens, when that
    is still ahead, else when the engine would have asked again; for a
    call reported to have run on usage credits, the subscription says no
    time at all, so `resumes_at` is None rather than a manufactured
    far-future timestamp (`_stamp` cannot format one)."""
    if hold.reason == OVERAGE:
        return UsageLimitReached(
            "the claude subscription reports a call ran on usage "
            "credits. Dispatch stops now, though that call and any "
            "already in flight may already have used them; only turning "
            "extra usage off on the account guarantees none does. The "
            "subscription does not say when that clears, so the pass is "
            "paused and makes no further calls; its completed outputs "
            "are kept. Run it again once extra usage is off on the "
            "account.",
            reason=OVERAGE,
            resumes_at=None,
        )
    ahead = hold.resets_at is not None and hold.resets_at > now
    reopens = hold.resets_at if ahead else hold.until
    when = _stamp(reopens)
    window = f"the claude subscription's {_describe_window(hold.window)}"
    reserved = (
        f"the reserve this pass leaves to development sessions "
        f"({reserve:.0%} of it, {RESERVE_ENV})"
    )
    longest = (
        f"the longest a call waits for the subscription "
        f"({_duration(max_wait)}, {MAX_WAIT_ENV}; this one had waited "
        f"{_duration(waited)})"
    )
    if hold.reason == USAGE_LIMIT and ahead:
        what = (
            f"{window} is used up, and the CLI reports that it reopens at "
            f"{when}: later than {longest}"
        )
    elif hold.reason == RESERVE:
        what = (
            f"{window} is used up to {reserved}, and it reopens at {when}: "
            f"later than {longest}"
        )
    else:
        what = (
            f"the claude CLI keeps refusing calls ({hold.reason}"
            + (", past the reset time it reported" if hold.resets_at else "")
            + f"), and the engine would ask again at {when}: later than "
            f"{longest}"
        )
    return UsageLimitReached(
        f"{what}. The pass is paused and makes no further calls; its "
        f"completed outputs are kept. Run it again after {when}.",
        reason=hold.reason,
        window=hold.window,
        resumes_at=_utc(reopens),
    )


_PACER = _Pacer()


def _setting(value: float | str | None, variable: str) -> str | None:
    if value is not None:
        return str(value)
    return os.environ.get(variable, "").strip() or None


def _reserve_setting(value: float | None) -> float:
    """The share of each usage window left to development sessions:
    `value`, else $YIZKR_CLAUDE_RESERVE, else DEFAULT_RESERVE."""
    raw = _setting(value, RESERVE_ENV)
    if raw is None:
        return DEFAULT_RESERVE
    try:
        reserve = float(raw)
    except ValueError:
        reserve = math.nan
    if not 0 <= reserve < 1:
        raise ValueError(
            f"the reserve left to development sessions ({RESERVE_ENV}) is "
            f"{raw!r}: give a share of each usage window from 0 up to, not "
            "including, 1, such as 0.25"
        )
    return reserve


def _max_wait_setting(value: float | None) -> float:
    """The longest a call waits for the subscription in all, in seconds:
    `value`, else $YIZKR_CLAUDE_MAX_WAIT, else DEFAULT_MAX_WAIT."""
    raw = _setting(value, MAX_WAIT_ENV)
    if raw is None:
        return DEFAULT_MAX_WAIT
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not seconds >= 0:
        raise ValueError(
            f"the longest a call waits for the subscription ({MAX_WAIT_ENV}) "
            f"is {raw!r}: give a number of seconds, 0 or more"
        )
    return seconds


def _fresh_session() -> list[str]:
    """The arguments of an attempt at a call in a new session: an explicit
    id, which names its transcript even when the attempt fails before its
    stream reports it."""
    return ["--session-id", str(uuid.uuid4())]


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
    (``CliToolsMismatch``). So does a call the API refuses because this
    CLI does not support the model the call names (``ModelUnsupported``):
    no document fails for the CLI the pass runs on.

    **Tool errors.** A one-shot call's stream reports every Read the model
    makes and its result. When a Read of a file the call was given fails,
    and no other Read of it makes up for the failure (only a refusal of
    the request's form, such as a PDF read without a page range, can be
    made up for; see ``tool_failures``), the model has not read that file,
    whatever its answer says: the call raises ``EngineToolError``, naming
    the tool and the class of its error, and returns nothing. It is an
    error like any other, so the LLM cache keeps nothing for it and the
    next call asks again (ADR-0032), unless the call was a refresh
    (``--refresh transcribe``): a refreshed call that raises replaces
    nothing, so the answer the cache kept and the transcription view stay,
    and the next run reuses them (yizkr.pipeline.transcription_check).

    **Limits.** A call the subscription refuses for a limit never fails,
    and its notice is never returned as an answer: the call is asked again
    once the limit lifts, or its pass pauses. Calls are paced to leave
    ``reserve`` of each usage window to development sessions. A call waits
    for the subscription at most ``max_wait`` seconds in all; a longer
    hold pauses the pass (``UsageLimitReached``), with the time it can go
    on, and so does a wait that ``close`` ends. ``last_pacing`` gives what
    the calling thread's last call waited, and why (see "Subscription
    limits" above).

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
    sends (``call_text``, ``system_prompt``). What a call's stream reports
    (``stream_report``: the model it ran on, the CLI version, the session,
    the tokens it used, its turns, how it ended, why its model stopped and
    any API error's class) is never part of either: ``last_report`` gives
    it for the calling thread's last answered call, which the LLM cache
    keeps beside the answer but for the stop reason and the error class,
    and ``last_stream`` for its last call, answered or not, which its call
    event carries.

    **Transcripts.** The CLI stores every session under its projects
    directory, and forks resume from there, so sessions stay persistent
    while they are in use. Then each transcript is moved into the run
    record or deleted (``transcript_destination``): a one-shot call's or
    a fork's as soon as the call returns, a conversation's when
    ``close_conversation`` or ``close`` is called, or at the latest when
    the client is garbage-collected or the process exits.
    ``keep_conversation`` gives an open conversation's session files, for
    the LLM cache to keep beside the conversation's opening turn
    (ADR-0032), and ``resume_conversation`` puts them back under the
    projects directory, so that a later run forks the conversation
    exactly as it was opened; a resumed conversation is released as one
    opened here is.
    """

    engine = "claude-code"
    # Only file reading is permitted — the engine must never execute
    # commands or write files on behalf of model output. It is the only
    # tool the model is offered (--tools) and the only one approved
    # (--allowedTools).
    ALLOWED_TOOLS = READ_TOOL
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
        reserve: float | None = None,
        max_wait: float | None = None,
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
        # pacing: the share of each usage window left to development
        # sessions, and the longest a call waits for the subscription
        self.reserve = _reserve_setting(reserve)
        self.max_wait = _max_wait_setting(max_wait)
        self._pacer = _PACER
        # what the calling thread's last call waited, and why
        self._pacing = threading.local()
        # set by close(): no call waits for the subscription any more
        self._closed = threading.Event()
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
        # what the streams reported about the calling thread's last
        # answered call, and about its last call, answered or not
        self._reports = threading.local()
        self._streams = threading.local()
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
            # the permission mode -p starts in otherwise depends on the
            # CLI version and the account; naming it keeps it out of both
            "--permission-mode",
            "default",
            # isolation from the owner's configuration: no settings files
            # (user, project or local, and with them no hooks, permission
            # rules, plugins from settings or CLAUDE.md; the CLI's built-in
            # plugins still load, and add no tools, skills or commands), no
            # MCP servers beyond the empty set given here, and no skills or
            # commands
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
        """What the streams reported about the calling thread's last
        answered call (``last_stream``), with the CLI version the pass is
        pinned to when they reported none: the model that answered it, the
        CLI version, the session, the usage, the turns, the subtype, the
        stop reason and any API error's class."""
        return copy.deepcopy(getattr(self._reports, "value", {}))

    def last_stream(self) -> dict[str, Any]:
        """What the streams reported about the calling thread's last call,
        answered or not (stream_report): what the stream of its final
        attempt reported, with the usage summed over all its attempts,
        those a subscription limit refused included. Empty for a call that
        never ran the CLI, or whose streams reported nothing."""
        return copy.deepcopy(getattr(self._streams, "value", {}))

    def last_pacing(self) -> dict[str, Any]:
        """How the calling thread's last call, answered or not, waited for
        the subscription: `waited` (seconds), `refused` (how many of its
        attempts a limit refused) and `held` (why it last waited, or
        paused); each only when there is something to say."""
        return dict(getattr(self._pacing, "value", {}))

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        """The answer to a one-shot call that reads `image_paths`. Raises
        EngineToolError when the call's stream reports that the Read tool
        failed on one of them (tool_failures)."""
        # a call that fails before it runs reports nothing of an earlier
        # call's (last_stream, last_pacing)
        self._streams.value = {}
        self._pacing.value = {}
        return self._run(
            _fresh_session,
            self.call_text(prompt, image_paths),
            inputs=image_paths,
        )[0]

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        """First turn of a forkable session; the handle is its session id.

        The session's transcript stays in place for its forks until
        ``close_conversation`` or ``close``."""
        response, arguments = self._run(_fresh_session, prompt, hold=True)
        return response, arguments[arguments.index("--session-id") + 1]

    def fork(self, handle: Any, prompt: str) -> str:
        """One follow-up turn on a FORK of the session — the original
        session is never mutated, so every fork branches from the same
        prefix and forks never see each other's turns."""
        return self._run(
            lambda: ["--resume", str(handle), "--fork-session"], prompt
        )[0]

    def keep_conversation(self, handle: Any) -> dict[str, bytes]:
        """The files of a conversation's session, by path under the CLI's
        projects directory: its transcript, and the directory beside it
        when there is one. Empty when the session is no longer stored."""
        try:
            name = str(uuid.UUID(str(handle)))
        except ValueError:
            return {}
        projects = claude_projects_dir(self.config_dir)
        paths = sorted(projects.glob(f"*/{name}.jsonl"))
        for directory in sorted(projects.glob(f"*/{name}")):
            if directory.is_dir() and not directory.is_symlink():
                paths += sorted(p for p in directory.rglob("*") if p.is_file())
        return {
            path.relative_to(projects).as_posix(): path.read_bytes()
            for path in paths
        }

    def resume_conversation(
        self, prompt: str, response: str, files: Mapping[str, bytes]
    ) -> Any:
        """Resume a kept conversation exactly: its session's files go back
        under the CLI's projects directory, from which forks resume it, and
        the session is held like one opened here, until
        ``close_conversation`` or ``close`` releases its transcript. Raises
        ValueError when the files hold no single session transcript, name
        a path outside the projects directory, or belong to a session this
        client holds open already (whose release would pull its transcript
        from under the other holder)."""
        transcripts = [name for name in files if name.endswith(".jsonl")]
        if len(transcripts) != 1:
            raise ValueError("the kept files hold no single transcript")
        session_id = str(uuid.UUID(Path(transcripts[0]).stem))
        projects = claude_projects_dir(self.config_dir).resolve()
        targets = {}
        for name in files:
            target = (projects / name).resolve()
            if not target.is_relative_to(projects) or target == projects:
                raise ValueError(f"a kept file outside the projects: {name}")
            targets[name] = target
        with self._conversations_lock:
            if session_id in self._conversations:
                raise ValueError(f"the session {session_id} is open already")
            self._conversations.add(session_id)
        try:
            for name, target in targets.items():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(files[name])
        except BaseException:
            with self._conversations_lock:
                self._conversations.discard(session_id)
            raise
        return session_id

    def close_conversation(self, handle: Any) -> None:
        """Release a conversation's transcript once it will not be forked
        again."""
        with self._conversations_lock:
            self._conversations.discard(str(handle))
        release_transcripts([str(handle)], self.transcripts, self.config_dir)

    def close(self) -> None:
        """Release the transcripts of every conversation still open, and
        end every wait for the subscription: a call that waits, or would,
        pauses its pass instead (UsageLimitReached)."""
        self._closed.set()
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
        self,
        arguments: Callable[[], list[str]],
        prompt: str,
        *,
        hold: bool = False,
        inputs: Sequence[Path] = (),
    ) -> tuple[str, list[str]]:
        """One call: an invocation of the CLI (`_attempt`), made again for
        as long as the subscription refuses it for a limit. Each attempt
        waits until the process's pacer admits it, and takes the arguments
        `arguments()` gives it. Returns the answer and the arguments of the
        attempt that gave it. A hold that would take the call's wait past
        `max_wait`, or a wait that `close` ends, raises UsageLimitReached;
        `last_pacing` then says what the call waited. However the call
        ends, `last_stream` then says what its attempts' streams reported,
        and once it is answered, `last_report` too."""
        record: dict[str, Any] = {"waited": 0.0, "refused": 0, "held": None}
        stream: dict[str, Any] = {}
        try:
            self._pin.raise_if_paused()
            while True:
                self._pacer.admit(
                    record,
                    reserve=self.reserve,
                    max_wait=self.max_wait,
                    stop=self._closed,
                )
                extra_args = arguments()
                try:
                    text = self._attempt(
                        extra_args,
                        prompt,
                        hold=hold,
                        inputs=inputs,
                        stream=stream,
                    )
                except _Refused as refused:
                    record["refused"] += 1
                    record["held"] = refused.refusal.reason
                    self._pacer.refused(refused.refusal, record["refused"])
                    continue
                pinned = {"cli_version": self.cli_version}
                self._reports.value = pinned | stream
                return text, extra_args
        finally:
            self._pacing.value = {
                name: value for name, value in record.items() if value
            }
            self._streams.value = stream

    def _attempt(
        self,
        extra_args: list[str],
        prompt: str,
        *,
        hold: bool = False,
        inputs: Sequence[Path] = (),
        stream: dict[str, Any] | None = None,
    ) -> str:
        """One CLI invocation, which the pacer has admitted and is told of
        when it ends, with what its stream reported of the subscription's
        windows. However it ends, what its stream reported of the call
        (stream_report) is taken into ``stream``, the report of the call
        it is an attempt at. Its sessions' transcripts are released when it
        ends, unless ``hold`` keeps a successful call's session open for
        forking. A call the subscription refused for a limit raises
        _Refused, before any other check. A call the API refused because
        the CLI does not support its model (unsupported_model) pauses the
        pass next (ModelUnsupported), latched as a version mismatch is, so
        no later call of the process is made. A call whose stream says the CLI
        gave up on its answer at the output token limit raises
        OutputLimitReached next (output_limit_reached), whatever the exit
        status. A call whose terminal result reports another error that is
        not a limit raises RuntimeError, whatever the exit status: it is
        never assembled and returned as the answer. A call whose stream
        reports the Read tool failing on one of its ``inputs`` raises
        EngineToolError, after the version and tool checks."""
        stdout = ""
        held = False
        try:
            self._pin.raise_if_paused()
            check_global_config(self.config_dir)
            self.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            events = _stream_events(stdout)
            refused = refusal(events, result.returncode)
            if refused is not None:
                raise _Refused(refused)
            unsupported = unsupported_model(events, result.returncode)
            if unsupported is not None:
                raise self._pin._latch(
                    ModelUnsupported(
                        f"the claude CLI {self.cli_version} does not support "
                        f"the model {self.model or '(its default)'}: the API "
                        f"refused a call with status {BAD_REQUEST} "
                        f"({unsupported}). The pass is paused and makes no "
                        "further calls; its completed outputs are kept. "
                        "Install a claude CLI that runs the model, or "
                        "configure a model this one runs, then run the pass "
                        "again: for a scratch run, a new run (`yizkr "
                        "scratch run --new`), since either changes the "
                        "settings the run recorded.",
                        model=self.model,
                        reason=unsupported,
                    )
                )
            if output_limit_reached(events):
                raise OutputLimitReached()
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-500:]
                raise RuntimeError(
                    f"claude CLI exited with {result.returncode}: "
                    f"{detail or 'no output'}"
                    " (not logged in? run `claude` interactively once)"
                )
            result_event = next(
                (e for e in reversed(events) if e.get("type") == "result"),
                {},
            )
            if result_event.get("is_error") is True:
                detail = str(result_event.get("result") or "").strip()[-500:]
                raise RuntimeError(
                    "claude CLI reported an error that is not a "
                    f"subscription limit: {detail or 'no detail'}"
                )
            for event in events:
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
            text = self._assemble_stream(stdout)
            failures = tool_failures(stdout, inputs) if inputs else []
            if failures:
                raise EngineToolError(failures, text)
            if not text.strip():
                raise RuntimeError("claude CLI returned no output")
            held = hold
            return text
        finally:
            reported = _stream_events(stdout)
            self._pacer.done(rate_limit_infos(reported))
            if stream is not None:
                _add_report(stream, stream_report(reported))
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
        """The answer a stream-json event stream gives: the assistant text
        after the call's last tool result.

        What the model writes before a tool call ("Let me read the
        image.") is narration of its work, not its answer, so the stream's
        structure decides where the answer starts: after the last user
        event that carries a tool_result block. A long answer can span
        several assistant turns with no tool call between them; each
        turn's text is part of it, so they are joined in order. A model
        that wrote text only before its last tool result gave no answer.
        Falls back to the terminal result event (or raw stdout) if no
        assistant event holds any text.
        """
        pieces: list[str] = []
        spoke = False
        result_text: str | None = None
        for event in _stream_events(stdout):
            message = event.get("message")
            content = (
                message.get("content") if isinstance(message, dict) else None
            )
            blocks = content if isinstance(content, list) else []
            if event.get("type") == "assistant":
                for block in blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        spoke = True
                        pieces.append(block["text"])
            elif event.get("type") == "user" and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in blocks
            ):
                pieces = []
            elif event.get("type") == "result":
                result_text = event.get("result")
        if spoke:
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
    reserve: float | None = None,
    max_wait: float | None = None,
) -> LlmClient:
    """CLI engine name -> client. Extend here when adding providers.

    ``effort`` is the claude-code reasoning level (low..max); the
    anthropic API path takes no effort setting, so only the claude-code
    client receives it. ``model`` overrides the model for this client (a
    per-phase override, e.g. a disciplined extraction model distinct from
    the run's main model); ``None`` inherits $YIZKR_LLM_MODEL.

    ``transcripts``, ``cli_version``, ``reserve`` and ``max_wait`` apply
    to claude-code only: the directory its session transcripts are moved
    into (``None`` defers to $YIZKR_CLAUDE_TRANSCRIPTS, and unset that
    deletes them); the CLI version the pass must run on (``None`` pins the
    version installed when the pass's first client starts); and the share
    of each usage window left to development sessions and the longest a
    call waits for the subscription, in seconds (``None`` defers to
    $YIZKR_CLAUDE_RESERVE and $YIZKR_CLAUDE_MAX_WAIT; see "Subscription
    limits").
    """
    if engine == "anthropic":
        return AnthropicClient(model=model)
    if engine == "claude-code":
        return ClaudeCodeClient(
            model=model,
            effort=effort,
            transcripts=transcripts,
            cli_version=cli_version,
            reserve=reserve,
            max_wait=max_wait,
        )
    raise ValueError(
        f"unknown LLM engine {engine!r}; available: anthropic (API key, "
        f"token billing), claude-code (CLI login, subscription billing) — "
        f"self-hosted adapters welcome: implement LlmClient"
    )


# -- engine calls as events --------------------------------------------------

# the event that reports a call that reached the engine
ENGINE_CALL = "call"
# how an engine call ended: with an answer; with an error; with a pause of
# its pass (PassPaused, a subscription limit's among them); or interrupted
# (Ctrl-C)
OK = "ok"
ERROR = "error"
PAUSED = "paused"
INTERRUPTED = "interrupted"
# what a call event carries of its streams' report (stream_report): all of
# it but the session, which names a session of the account; of an engine
# with no streams, the model and the stop reason its report gives of an
# answered call (ENGINE_REPORT, ObservedClient)
CALL_STREAM_FIELDS = (
    "model",
    "cli_version",
    "usage",
    "num_turns",
    "subtype",
    "stop_reason",
    "api_error",
)
# what a call event carries of the report of an engine with no streams,
# each field kept only as the plain token its pattern takes whole
ENGINE_REPORT = {"model": _MODEL_ID, "stop_reason": _STOP_REASON}
# the stage each role's calls are made in (yizkr.pipeline.orchestrate)
ROLE_STAGES = {
    "transcribe": "transcription",
    "extract": "extraction",
    "critique": "critique",
}


class ObservedClient:
    """An engine's client whose every call is reported to `events`, as
    ``events("call", **fields)``, whether the call answers, fails or
    pauses its pass. The fields are the `role`, its `stage`
    (ROLE_STAGES), the kind of `call` (complete, open or fork), the
    `outcome` (ok, error, paused or interrupted), how long the call took
    end to end (`duration_s`, which includes any time it spent paced —
    `wait_s` is the part of it that was), the `error` it raised (the
    exception's name, never its message), and, from an engine that paces
    its calls (`last_pacing`), the seconds it waited for the subscription
    (`wait_s`), how many of its attempts a limit `refused` and why it was
    last `held`; a pause for a subscription limit adds its `window` and
    when the pass can go on (`resumes_at`). From an engine whose streams
    report its calls (`last_stream`), it adds what the call's streams
    reported: the `model` it ran on, the `cli_version`, its `usage`, its
    `num_turns`, its `subtype`, its `stop_reason`, which is max_tokens for
    an answer cut off at the output limit (at_output_limit), and the class
    of API error its last assistant event stands for (`api_error`), which
    is max_output_tokens when the CLI gave up on the answer at that limit
    (the call's `error` is then OutputLimitReached). From an engine that
    reports only its answered calls (`last_report`, as the Anthropic API's
    client does), it adds the `model` and the `stop_reason` its report
    gives of the call when the call answered, the stop reason verbatim
    (max_tokens for an answer cut off at the output limit), and nothing
    of the report when the call did not answer, as it may be an earlier
    call's; nothing else of that report, which is no stream's, is
    taken. The session a call ran in is never in its event: it names a
    session of the account, and the event log is what the monitor and the
    run report read; the LLM cache keeps it beside the answer. A replay of
    a conversation's opening turn is marked `replay`. A field with nothing
    to say is left out.

    It wraps the engine's own client, behind the LLM cache when there is
    one, so it reports the calls that reach the engine, the cache's
    misses and replays, and none it answers. Everything else is the
    engine's: its attributes, and the conversational capability exactly
    when the engine has it (ObservedConversationalClient)."""

    # the optional capabilities a wrapper presents by methods of its own,
    # or not at all
    _CAPABILITIES = frozenset(
        {
            "open_conversation",
            "replay_conversation",
            "fork",
            "reject",
            "attempt",
        }
    )

    def __init__(
        self, client: Any, *, role: str, events: Callable[..., Any]
    ) -> None:
        self.client = client
        self.role = role
        self._events = events

    def __getattr__(self, name: str) -> Any:
        if (
            name in self._CAPABILITIES
            or name.startswith("__")
            or name in ("client", "role", "_events")
        ):
            raise AttributeError(name)
        return getattr(self.client, name)

    def complete(
        self, prompt: str, image_paths: Sequence[str | os.PathLike[str]] = ()
    ) -> str:
        return self._observe(
            COMPLETE, lambda: self.client.complete(prompt, image_paths)
        )

    def _observe(
        self, call: str, ask: Callable[[], Any], *, replay: bool = False
    ) -> Any:
        started = time.monotonic()
        outcome, error, pause = OK, None, None
        try:
            return ask()
        except PassPaused as exc:
            outcome, error, pause = PAUSED, type(exc).__name__, exc
            raise
        except Exception as exc:
            outcome, error = ERROR, type(exc).__name__
            raise
        except BaseException as exc:
            outcome, error = INTERRUPTED, type(exc).__name__
            raise
        finally:
            self._report(
                call,
                outcome,
                time.monotonic() - started,
                error,
                pause,
                replay=replay,
            )

    def _report(
        self,
        call: str,
        outcome: str,
        duration: float,
        error: str | None,
        pause: PassPaused | None,
        *,
        replay: bool = False,
    ) -> None:
        """Emit the call's event. An event that cannot be recorded is a
        warning, never the call's failure: the answer matters more."""
        last_pacing = getattr(self.client, "last_pacing", None)
        pacing = last_pacing() if callable(last_pacing) else {}
        stream = self._stream(outcome)
        fields: dict[str, Any] = {
            "role": self.role,
            "stage": ROLE_STAGES.get(self.role),
            "call": call,
            "replay": True if replay else None,
            "outcome": outcome,
            "duration_s": round(duration, 3),
            "wait_s": round(pacing.get("waited") or 0.0, 1) or None,
            "refused": pacing.get("refused") or None,
            "held": pacing.get("held"),
            "error": error,
        }
        if isinstance(pause, UsageLimitReached):
            fields["held"] = pause.reason or fields["held"]
            fields["window"] = pause.window
            if pause.resumes_at is not None:
                fields["resumes_at"] = pause.resumes_at.isoformat(
                    timespec="seconds"
                )
        for name in CALL_STREAM_FIELDS:
            fields[name] = stream.get(name)
        try:
            self._events(
                ENGINE_CALL,
                **{
                    name: value
                    for name, value in fields.items()
                    if value is not None
                },
            )
        except Exception as exc:
            logger.warning("could not record an engine call: %s", exc)

    def _stream(self, outcome: str) -> Mapping[str, Any]:
        """What the engine reported of the call just made: its streams'
        report (`last_stream`) from an engine that has one, whatever the
        call's outcome and even when it reports nothing; else, from an
        engine that reports only its answered calls (`last_report`), the
        model and the stop reason of an answered call alone, each as a
        plain token (ENGINE_REPORT), since such a report of a call that did
        not answer may be an earlier call's. Nothing else of that report is
        taken: it is no stream's."""
        last_stream = getattr(self.client, "last_stream", None)
        if callable(last_stream):
            return last_stream()
        last_report = getattr(self.client, "last_report", None)
        if outcome != OK or not callable(last_report):
            return {}
        report = last_report()
        if not isinstance(report, Mapping):
            return {}
        return {
            name: _token(report.get(name), pattern)
            for name, pattern in ENGINE_REPORT.items()
        }


class ObservedConversationalClient(ObservedClient):
    """An ObservedClient for an engine that holds conversations: opening
    one and forking it are calls like any other."""

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        return self._observe(
            OPEN, lambda: self.client.open_conversation(prompt)
        )

    def replay_conversation(self, prompt: str) -> tuple[str, Any]:
        """Open a conversation again whose opening turn the LLM cache
        answered, for a fork's session (ADR-0032 §6): an opening call like
        any other, whose event is marked a replay."""
        return self._observe(
            OPEN, lambda: self.client.open_conversation(prompt), replay=True
        )

    def fork(self, handle: Any, prompt: str) -> str:
        return self._observe(FORK, lambda: self.client.fork(handle, prompt))


def observed_client(
    client: Any, *, role: str, events: Callable[..., Any] | None
) -> Any:
    """``client``, an engine's own client, with every call it makes
    reported to ``events`` (ObservedClient) as a call of ``role``; the
    client itself when there is no ``events`` to report to."""
    if events is None:
        return client
    kind = (
        ObservedConversationalClient
        if isinstance(client, ConversationalLlmClient)
        else ObservedClient
    )
    return kind(client, role=role, events=events)


def engine_client(client: Any) -> Any:
    """The engine's own client behind an ObservedClient, which holds the
    engine's sessions: ``client`` itself when it is not observed."""
    while isinstance(client, ObservedClient):
        client = client.client
    return client


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
    are keyed, and answered, afresh. No entry is written until a call is
    answered. Every command opens its cache here, before its first call,
    and the cache refuses (llmcache.CacheRefused, a pause) a ``root`` a
    git work tree would track and one there already that is open to other
    users, whose mode it leaves alone: its entries hold corpus content."""
    from yizkr.pipeline.llmcache import EngineCache

    return EngineCache(root, refresh=refresh, epochs=epochs)


def read_cache(
    root: str | os.PathLike[str],
    *,
    refresh: Iterable[str] = (),
    epochs: Mapping[str, int] | None = None,
) -> Any:
    """The LLM cache under ``root`` as a run with this ``refresh`` and
    these ``epochs`` would be served it, read as it is (ADR-0032 §3):
    without its lock, which a running pass may hold, and with nothing
    written. A client `cached_client` makes over it asks its engine for
    what the cache cannot answer; what the calls change, an answer
    rejected or one stored, is kept in memory. A kept session comes back
    as the names of its files only, with no bytes: whether they can
    resume a conversation is all a reader asks. It comes back whole
    however a run holding the cache keeps that session meanwhile, and
    never empty while one is kept (llmcache.CacheAsIs.kept_session). A
    folder the cache would refuse to open, one a git work tree would
    track, one open to other users, or one this user cannot write in or
    make, is refused here too (llmcache.CacheRefused, a pause). The call
    estimate counts a planned run's calls through it
    (yizkr.pipeline.estimate)."""
    from yizkr.pipeline.llmcache import CacheAsIs

    return CacheAsIs(root, refresh=refresh, epochs=epochs)


def cache_holder(root: str | os.PathLike[str]) -> str | None:
    """The process that holds the LLM cache under ``root`` now, described
    (its id, host, command and start), as the cache's lock file records
    it, read without taking the lock; None when none runs that holds it
    (One process per cache, docs/LLM_CACHE.md)."""
    from yizkr.pipeline.llmcache import held_by

    return held_by(root)


def cached_client(
    client: LlmClient,
    cache: Any,
    *,
    role: str,
    events: Callable[..., Any] | None = None,
) -> LlmClient:
    """``client`` with every call answered from ``cache`` (an `open_cache`
    or `read_cache` result) when the same call was answered before, and
    otherwise made and kept. ``client`` is an engine's client, or one
    `observed_client` made of it, whose calls are those that reach the
    engine: the misses and the replays. ``role`` names the pipeline step
    whose calls these are (transcribe, extract, critique): its epoch and
    refresh setting apply. ``events``, when given, receives an event for
    every hit, miss, resume, replay, rejection and adoption. The wrapper
    presents the conversational capability exactly when ``client`` has
    it, and adds `reject` (RejectingLlmClient) and `attempt`
    (AttemptingLlmClient)."""
    from yizkr.pipeline.llmcache import wrap

    return wrap(client, cache, role=role, events=events)


# -- expected calls ----------------------------------------------------------

# the events that count a call in a run's events.jsonl, one the LLM cache
# answered or missed, and the one that says a stage made its view
_CACHE_CALLS = frozenset({"cache-hit", "cache-miss"})
_STAGE_DONE = "done"


@dataclass(frozen=True)
class StageEstimate:
    """The stale work of one stage of a planned run: the `documents` whose
    view the stage is to make, the `calls` they are expected to make, and
    how many of those documents take their count from the baseline run
    (`measured`); the others take a typical count (estimate_calls)."""

    documents: int = 0
    calls: int = 0
    measured: int = 0


def run_calls(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], int]:
    """(document SHA-1, stage) -> the calls a run made for the document in
    the stage, from the run's events (events.jsonl). In an invocation with
    the LLM cache, a call is one the cache answered or missed; without
    it, one that reached the engine (ENGINE_CALL). A stage made in
    several invocations of the run (one that failed or paused, then the
    one that made it) counts its latest, and a stage made without a call
    counts 0."""
    tallies: dict[tuple[str, str, Any], dict[str, int]] = {}
    latest: dict[tuple[str, str], Any] = {}
    for event in events:
        name = event.get("event")
        subject = event.get("subject")
        if name == _STAGE_DONE:
            stage = event.get("stage")
        elif name in _CACHE_CALLS or name == ENGINE_CALL:
            stage = ROLE_STAGES.get(event.get("role"))
        else:
            continue
        if not isinstance(subject, str) or not isinstance(stage, str):
            continue
        invocation = event.get("invocation")
        tally = tallies.setdefault(
            (subject, stage, invocation), {"cache": 0, "engine": 0}
        )
        if name in _CACHE_CALLS:
            tally["cache"] += 1
        elif name == ENGINE_CALL:
            tally["engine"] += 1
        latest[(subject, stage)] = invocation
    counts = {}
    for (subject, stage), invocation in latest.items():
        tally = tallies[(subject, stage, invocation)]
        counts[(subject, stage)] = tally["cache"] or tally["engine"]
    return counts


def estimate_calls(
    documents: Iterable[Mapping[str, Any]],
    *,
    current: Collection[tuple[str, str]] = (),
    baseline: Mapping[tuple[str, str], int] | None = None,
    sizes: Mapping[str, int] | None = None,
) -> dict[str, StageEstimate]:
    """The calls a planned run is expected to make for its stale work,
    per stage. `documents` are the run manifest's planned documents
    ({sha1, kind, stages}), and the stale work is every stage the plan
    gives a document but the (SHA-1, stage) pairs in `current`, whose
    views stand and are reused without a call; a scratch run, which
    starts from fresh state directories, has none.

    A document's calls in a stage are those the `baseline` run made
    (run_calls), when it has them. Otherwise they are the baseline's mean
    for the planned documents of the same kind in the stage, when it has
    any, and else typical: one vision call to transcribe a scan (none for
    a text file or slide text), one critique, and for extraction the
    entity manifest and its chunks, about one per DEFAULT_CHUNK_BUDGET
    characters of the text when `sizes` gives its length (SHA-1 to
    characters), else one; a GEDCOM file calls no engine.

    These are the pipeline's calls, counted without their keys. The LLM
    cache answers those whose keys it holds, so the engine is asked no
    more often, but for the replays of conversations whose kept session
    is gone (ADR-0032 §6). The scratch run's call estimate keys what it
    can, with the cache as it is (yizkr.pipeline.estimate), and counts by
    this only the calls whose keys wait on an answer the engine has yet
    to give."""
    from yizkr.pipeline.chunking import DEFAULT_CHUNK_BUDGET

    documents = list(documents)
    known = dict(baseline or {})
    reused = set(current)
    lengths = dict(sizes or {})
    means: dict[tuple[Any, str], list[int]] = {}
    for document in documents:
        for stage in document.get("stages") or ():
            if (document["sha1"], stage) in known:
                means.setdefault((document.get("kind"), stage), []).append(
                    known[(document["sha1"], stage)]
                )
    estimates: dict[str, StageEstimate] = {}
    for document in documents:
        sha1, kind = document["sha1"], document.get("kind")
        for stage in document.get("stages") or ():
            if (sha1, stage) in reused:
                continue
            measured = (sha1, stage) in known
            if measured:
                calls = known[(sha1, stage)]
            elif (kind, stage) in means:
                counts = means[(kind, stage)]
                calls = round(sum(counts) / len(counts))
            else:
                calls = _typical_calls(
                    kind, stage, lengths.get(sha1), DEFAULT_CHUNK_BUDGET
                )
            before = estimates.get(stage, StageEstimate())
            estimates[stage] = StageEstimate(
                before.documents + 1,
                before.calls + calls,
                before.measured + int(measured),
            )
    return estimates


def _typical_calls(
    kind: Any, stage: str, size: int | None, budget: int
) -> int:
    """The calls a document of `kind` typically makes in `stage`, with no
    run to measure them in (estimate_calls)."""
    if stage == "transcription":
        return 1 if kind in ("media", "pdf") else 0
    if stage == "critique":
        return 1
    if stage == "extraction":
        chunks = math.ceil(size / budget) if size else 1
        return 1 + max(1, chunks)
    return 0
