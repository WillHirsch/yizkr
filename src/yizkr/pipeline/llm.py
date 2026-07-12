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

Pick the model for either engine with $YIZKR_LLM_MODEL.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class LlmClient(Protocol):
    """Minimal completion interface pipeline steps rely on."""

    model_identifier: str

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        """Return the model's text response for a prompt (+ optional images)."""
        ...


class AnthropicClient:
    """Anthropic API client (vision-capable). Explicit opt-in only."""

    def __init__(self, model: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "the 'anthropic' package is not installed; pip install anthropic"
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
        response = self._client.messages.create(
            model=self.model_identifier,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class ClaudeCodeClient:
    """Claude Code CLI client (headless ``claude -p``); subscription billing.

    The CLI authenticates with its own login — including a Claude
    Pro/Max subscription — so usage draws on the subscription's limits
    instead of per-token API billing. A Messages-API key is deliberately
    NOT passed through to the child process: if you want key-based
    billing, use the ``anthropic`` engine instead.

    Vision works by pointing the CLI's Read tool at the image files.
    Tool access is locked down to Read only, and the process runs in a
    neutral temporary directory so no project CLAUDE.md or skills leak
    into the prompt.
    """

    # Only file reading is permitted — the engine must never execute
    # commands or write files on behalf of model output.
    ALLOWED_TOOLS = "Read"

    def __init__(
        self,
        model: str | None = None,
        executable: str = "claude",
        timeout: int = 1200,
        max_turns: int = 30,
    ) -> None:
        self.executable = executable
        self.model = model if model is not None else os.environ.get("YIZKR_LLM_MODEL")
        self.timeout = timeout
        self.max_turns = max_turns
        try:
            version = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.split()[0]
        except (OSError, IndexError) as exc:
            raise RuntimeError(
                "the Claude Code CLI ('claude') is not installed or not on "
                "PATH. Install it and log in once (a Claude Pro/Max "
                "subscription works) — being logged in is the explicit "
                "opt-in before artifact content leaves this system. "
                "For API-key billing use the 'anthropic' engine instead."
            ) from exc
        self.model_identifier = f"claude-code/{version}" + (
            f":{self.model}" if self.model else ""
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
            "--max-turns",
            str(self.max_turns),
            # Denied tool calls still consume turns in -p mode; a planning
            # tool grabbed mid-extraction can burn the whole cap. Forbid
            # everything except reading the files we point at.
            "--append-system-prompt",
            "You are running non-interactively inside a data pipeline. "
            "Do not use any tools except Read, and only on files named in "
            "the prompt. No TodoWrite, no task tracking, no searching. "
            "Produce the requested output directly in a single response.",
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    @staticmethod
    def _child_env() -> dict[str, str]:
        # Force the CLI onto its own login: a stray API key in the
        # environment would silently switch this engine to per-token
        # billing, which is exactly what choosing it opts out of.
        return {
            key: value
            for key, value in os.environ.items()
            if key not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        }

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        full_prompt = prompt
        if image_paths:
            listing = "\n".join(str(Path(p).resolve()) for p in image_paths)
            full_prompt = (
                "First use the Read tool to examine these image files:\n"
                f"{listing}\n\n{prompt}"
            )
        result = subprocess.run(
            self._command(),
            input=full_prompt,  # stdin avoids ARG_MAX limits on large prompts
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=tempfile.gettempdir(),
            env=self._child_env(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise RuntimeError(
                f"claude CLI exited with {result.returncode}: {detail or 'no output'}"
                " (not logged in? run `claude` interactively once)"
            )
        text = self._assemble_stream(result.stdout)
        if not text.strip():
            raise RuntimeError("claude CLI returned no output")
        return text

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
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        pieces.append(block["text"])
            elif event.get("type") == "result":
                result_text = event.get("result")
        if pieces:
            return "\n".join(pieces)
        if result_text:
            return result_text
        return stdout

    @staticmethod
    def available(executable: str = "claude") -> bool:
        return shutil.which(executable) is not None


def make_client(engine: str) -> LlmClient:
    """CLI engine name -> client. Extend here when adding providers."""
    if engine == "anthropic":
        return AnthropicClient()
    if engine == "claude-code":
        return ClaudeCodeClient()
    raise ValueError(
        f"unknown LLM engine {engine!r}; available: anthropic (API key, "
        f"token billing), claude-code (CLI login, subscription billing) — "
        f"self-hosted adapters welcome: implement LlmClient"
    )
