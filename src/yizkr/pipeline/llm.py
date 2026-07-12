"""Pluggable LLM client protocol for transcription and extraction.

PRIVACY: configuring an external client sends
artifact content — which may describe living people — to that
provider. Nothing here talks to any service unless explicitly
configured; there is no default client. The pipeline runs end-to-end
without one (text-file transcription + prepared claim files).

To use Anthropic's API: install the `anthropic` package, set
ANTHROPIC_API_KEY, and pass ``--engine llm`` to the CLI. Configuring
the key IS the explicit instruction the privacy principle requires
(no personal data to third-party services otherwise; see the README).
"""

from __future__ import annotations

import base64
import mimetypes
import os
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
    """Anthropic-backed client (vision-capable). Explicit opt-in only."""

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


def make_client(engine: str) -> LlmClient:
    """CLI engine name -> client. Extend here when adding providers."""
    if engine == "anthropic":
        return AnthropicClient()
    raise ValueError(
        f"unknown LLM engine {engine!r}; available: anthropic "
        f"(self-hosted adapters welcome — implement LlmClient)"
    )
