"""The Anthropic API engine's client over a fake `anthropic` SDK, for
unit tests: no request leaves the process, and the real SDK is never
imported."""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping

from yizkr.pipeline import llm

# the model a test names, and the dated model the fake API says answered
MODEL = "claude-sonnet-5"
RESOLVED = "claude-sonnet-5-20260901"


def api_engine(monkeypatch, *answers, by_prompt=None, resolved=RESOLVED):
    """An AnthropicClient whose fake SDK gives each next request, in order,
    its answer and the stop reason of its model, as the API puts them on
    its response, with `resolved` as the model that answered; an answer may
    be an exception the request raises instead. With `by_prompt`, a
    mapping, a request takes the answer its last text block's prompt maps
    to, whatever the order the requests come in."""
    queue = list(answers)

    class Messages:
        async def create(self, **request):
            if isinstance(by_prompt, Mapping):
                prompt = request["messages"][-1]["content"][-1]["text"]
                answer = by_prompt[prompt]
            else:
                answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            text, stop_reason = answer
            block = types.SimpleNamespace(type="text", text=text)
            return types.SimpleNamespace(
                model=resolved, content=[block], stop_reason=stop_reason
            )

    package = types.ModuleType("anthropic")

    async def close():
        pass

    package.AsyncAnthropic = lambda **kwargs: types.SimpleNamespace(
        messages=Messages(), close=close
    )
    monkeypatch.setitem(sys.modules, "anthropic", package)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test-key")
    return llm.AnthropicClient(model=MODEL)
