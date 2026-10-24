"""Offline tests for a call the platform's safety classifier refuses
(#614): the claude-code engine reads it from the call's stream-json
structure and raises ClassifierRefusal, naming the refusal and its
category where the CLI gives one, with no hint at the login. It is not
asked again as a limit is, and the LLM cache keeps nothing for it. A
call a subscription limit refuses, and a call that fails because the
CLI is not logged in, are each still reported as themselves.

The fake claude CLI of test_engine_limits stands in for the real one.
Here it also prints what CLI 2.1.310 prints for a classifier refusal (its
SDK schema and its refusal frame, read from the installed CLI): an
assistant event that stands for an API error, whose message has the stop
reason refusal and the refusal's stop_details, a model_refusal_no_fallback
system event, and an error result with the stop reason refusal, with exit
status 1. Documents come from the fictional universe (docs/EXAMPLES.md)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_claude_code_pinning import Completed, install_fake_cli
from tests.unit.test_engine_limits import (
    API_ERROR,
    BAD_REQUEST,
    INIT,
    OVERLOAD,
    RATE_LIMITED,
    START,
    Clock,
    LimitedCli,
    recorder,
    refused_events,
    stream,
    usage_window,
)
from yizkr.pipeline import llm
from yizkr.pipeline.llm import (
    BACKOFF,
    MAX_WAIT_ENV,
    RATE_LIMIT,
    REFUSAL_STOP,
    RESERVE_ENV,
    SAFETY_MONITOR_BLOCKED,
    ClassifierRefusal,
    ClaudeCodeClient,
    UsageLimitReached,
    cached_client,
    classifier_refusal,
    observed_client,
    open_cache,
)

ROOT = Path(__file__).resolve().parents[2]
LOGIN_HINT = "not logged in?"
# the notice the CLI's refusal frame carries; never read by the engine
NOTICE = (
    "API Error: Claude can't help with this. Start a new session to "
    "continue. Learn more: https://www.anthropic.com/legal/aup"
)


def refusal_events(
    session: str,
    *,
    category: str | None = "cyber",
    details: bool = True,
    no_fallback: bool = True,
    result_stop: str | None = REFUSAL_STOP,
) -> list[dict]:
    """A classifier refusal's events after the init event, as CLI 2.1.310
    prints them: the refusal frame (an API-error assistant event with the
    stop reason refusal and stop_details), the model_refusal_no_fallback
    system event, and the error result."""
    message = {
        "model": "claude-opus-5-5",
        "role": "assistant",
        "stop_reason": REFUSAL_STOP,
        "content": [{"type": "text", "text": NOTICE}],
    }
    if details:
        message["stop_details"] = {
            "type": "refusal",
            "category": category,
            "explanation": "The request about the Flörburg register was "
            "flagged.",
        }
    events: list[dict] = [
        {
            "type": "assistant",
            "message": message,
            "parent_tool_use_id": None,
            "session_id": session,
            "error": "invalid_request",
            "is_api_error_message": True,
        }
    ]
    if no_fallback:
        events.append(
            {
                "type": "system",
                "subtype": "model_refusal_no_fallback",
                "original_model": "claude-opus-5-5",
                "request_id": None,
                "api_refusal_category": category,
                "api_refusal_explanation": None,
                "content": "",
                "session_id": session,
            }
        )
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 1,
            "result": NOTICE,
            "stop_reason": result_stop,
            "session_id": session,
        }
    )
    return events


def safety_monitor_events(session: str) -> list[dict]:
    """A response a server-side safety monitor blocked: an API-error
    assistant event of class safety_monitor_blocked, and an error result."""
    return [
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "API Error: blocked"}],
            },
            "session_id": session,
            "error": "unknown",
            "api_error": SAFETY_MONITOR_BLOCKED,
            "is_api_error_message": True,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 1,
            "result": "API Error: blocked",
            "session_id": session,
        },
    ]


# a call that fails because the CLI is not logged in: the API's 401 as an
# API-error assistant event of class authentication_failed and an error
# result, with exit status 1
LOGIN_FAILURE = {
    "info": None,
    "status": 401,
    "error": "authentication_failed",
    "text": "Invalid API key · Please run /login",
}


class RefusingCli(LimitedCli):
    """The fake claude CLI of test_engine_limits, whose `script` may also
    hold a classifier refusal: {"events": f(session)}, the events it
    prints after the init event, exiting with 1; or {"stderr": text}, a
    process that prints nothing on stdout and exits with 1."""

    def __call__(self, command, **kwargs):
        if command[0] == "claude" and command[1:] != ["--version"]:
            step = self.script[0] if self.script else None
            if isinstance(step, dict) and "stderr" in step:
                self.script.pop(0)
                self.calls.append((command, kwargs))
                return Completed(stderr=step["stderr"], returncode=1)
            if isinstance(step, dict) and "events" in step:
                self.script.pop(0)
                completed = super(LimitedCli, self).__call__(command, **kwargs)
                init = json.loads(completed.stdout.splitlines()[0])
                return Completed(
                    stdout=stream([init, *step["events"](init["session_id"])]),
                    returncode=1,
                )
        return super().__call__(command, **kwargs)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(
        llm, "_PACER", llm._Pacer(clock=clock, sleep=clock.sleep)
    )
    for name in (RESERVE_ENV, MAX_WAIT_ENV):
        monkeypatch.delenv(name, raising=False)
    return clock


@pytest.fixture
def cli(monkeypatch, tmp_path, clock) -> RefusingCli:
    other = subprocess.run
    plain = install_fake_cli(monkeypatch, tmp_path)
    refusing = RefusingCli(plain.projects, other)
    monkeypatch.setattr(llm.subprocess, "run", refusing)
    return refusing


# -- reading a refusal --------------------------------------------------------


class TestReading:
    """What the engine reads from a call's stream as a classifier refusal
    (llm.classifier_refusal)."""

    def test_a_refusal_with_its_category(self):
        refused = classifier_refusal([INIT, *refusal_events("s1")], 1)
        assert isinstance(refused, ClassifierRefusal)
        assert (refused.blocked_by, refused.category) == (
            REFUSAL_STOP,
            "cyber",
        )

    def test_the_category_from_the_system_event_alone(self):
        events = [INIT, *refusal_events("s1", category="bio", details=False)]
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (REFUSAL_STOP, "bio")

    def test_the_frame_alone_is_one(self):
        events = [
            INIT,
            *refusal_events("s1", no_fallback=False, result_stop=None),
        ]
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (
            REFUSAL_STOP,
            "cyber",
        )

    def test_the_system_event_alone_is_one(self):
        events = [INIT, *refusal_events("s1", result_stop=None)]
        events.pop(1)  # the frame
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (
            REFUSAL_STOP,
            "cyber",
        )

    def test_the_result_alone_is_one_with_no_category(self):
        events = [INIT, refusal_events("s1")[-1]]
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (REFUSAL_STOP, None)

    @pytest.mark.parametrize(
        "category",
        [None, "", "Cyber", "cyber; see the register", 7, "x" * 65],
        ids=["null", "empty", "capitalised", "prose", "a number", "too long"],
    )
    def test_a_category_that_is_no_plain_token_is_left_out(self, category):
        events = [INIT, *refusal_events("s1", category=category)]
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (REFUSAL_STOP, None)
        assert "category" not in str(refused)

    def test_a_safety_monitor_block(self):
        events = [INIT, *safety_monitor_events("s1")]
        refused = classifier_refusal(events, 1)
        assert (refused.blocked_by, refused.category) == (
            SAFETY_MONITOR_BLOCKED,
            None,
        )

    def test_a_refusal_a_fallback_model_answered_is_none(self):
        """The refusal frame, then the fallback model's answer: the call
        ended on an explicit non-error result."""
        events = [
            INIT,
            refusal_events("s1")[0],
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "answer",
                "stop_reason": "end_turn",
            },
        ]
        assert classifier_refusal(events, 0) is None

    def test_a_refusal_stop_on_no_api_error_event_is_none(self):
        """Only an event that stands for an API error is the refusal
        frame: the stop reason on the model's own message, before an
        unrelated error, is none."""
        frame = refusal_events("s1")[0]
        own = {
            key: value
            for key, value in frame.items()
            if key not in ("error", "is_api_error_message")
        }
        events = [INIT, own, *refused_events(API_ERROR, "s1")]
        assert classifier_refusal(events, 1) is None

    def test_the_notice_text_alone_is_none(self):
        step = {**API_ERROR, "text": NOTICE}
        events = [INIT, *refused_events(step, "s1")]
        assert classifier_refusal(events, 1) is None

    @pytest.mark.parametrize(
        "step",
        [
            usage_window(START + 7200),
            RATE_LIMITED,
            OVERLOAD,
            API_ERROR,
            BAD_REQUEST,
            LOGIN_FAILURE,
        ],
        ids=["a used-up window", "a rate limit", "an overload",
             "another API error", "a 400", "a login failure"],
    )  # fmt: skip
    def test_anything_else_is_none(self, step):
        events = [INIT, *refused_events(step, "s1")]
        assert classifier_refusal(events, 1) is None


# -- each reported as itself --------------------------------------------------


class TestReported:
    def test_a_refusal_is_its_own_error(self, cli, clock):
        cli.script = [{"events": refusal_events}]
        client = ClaudeCodeClient()
        with pytest.raises(ClassifierRefusal) as raised:
            client.complete("Read the letter.")
        message = str(raised.value)
        assert "refused" in message and REFUSAL_STOP in message
        assert "category cyber" in message
        assert LOGIN_HINT not in message
        assert "run `claude`" not in message
        # neither the notice nor the explanation is carried
        assert "Flörburg" not in message and "aup" not in message

    def test_a_safety_monitor_block_is_its_own_error(self, cli, clock):
        cli.script = [{"events": safety_monitor_events}]
        with pytest.raises(ClassifierRefusal) as raised:
            ClaudeCodeClient().complete("Read the letter.")
        assert "safety monitor" in str(raised.value)
        assert LOGIN_HINT not in str(raised.value)

    def test_a_refusal_is_not_asked_again(self, cli, clock):
        cli.script = [{"events": refusal_events}]
        client = ClaudeCodeClient()
        with pytest.raises(ClassifierRefusal):
            client.complete("Read the letter.")
        assert len(cli.calls) == 1
        assert clock.slept == []
        assert client.last_pacing() == {}
        # the next call is asked, and answered
        assert client.complete("Read the letter.") == "answer"
        assert len(cli.calls) == 2

    def test_a_limit_is_still_a_limit(self, cli, clock):
        cli.script = [RATE_LIMITED]
        client = ClaudeCodeClient()
        assert client.complete("Read the letter.") == "answer"
        assert client.last_pacing() == {
            "waited": BACKOFF,
            "refused": 1,
            "held": RATE_LIMIT,
        }

    def test_a_used_up_window_still_pauses_the_pass(self, cli, clock):
        cli.script = [usage_window(START + 10 * 3600)]
        with pytest.raises(UsageLimitReached):
            ClaudeCodeClient().complete("Read the letter.")

    @pytest.mark.parametrize(
        "step",
        [
            {"stderr": "Invalid API key · Please run /login"},
            LOGIN_FAILURE,
        ],
        ids=["on stderr alone", "as an API error"],
    )
    def test_a_login_failure_keeps_its_hint(self, cli, clock, step):
        cli.script = [step]
        with pytest.raises(RuntimeError) as raised:
            ClaudeCodeClient().complete("Read the letter.")
        assert not isinstance(raised.value, ClassifierRefusal)
        assert LOGIN_HINT in str(raised.value)
        assert "Please run /login" in str(raised.value)
        assert len(cli.calls) == 1


# -- how a stage records it ---------------------------------------------------


class TestRecorded:
    def test_its_call_event_names_it(self, cli, clock):
        cli.script = [{"events": refusal_events}]
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(), role="transcribe", events=emit
        )
        with pytest.raises(ClassifierRefusal):
            client.complete("Read the letter.")
        (event,) = seen
        assert (event["outcome"], event["error"], event["stop_reason"]) == (
            "error",
            "ClassifierRefusal",
            REFUSAL_STOP,
        )
        assert not {"wait_s", "refused", "held"} & event.keys()

    def test_the_cache_keeps_nothing(self, cli, clock, tmp_path):
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient()
        cli.script = [{"events": refusal_events}]
        with pytest.raises(ClassifierRefusal):
            cached_client(engine, cache, role="transcribe").complete(
                "Read the letter."
            )
        assert not list((tmp_path / "cache").glob("*/*.json"))
        # the next call asks the engine again
        client = cached_client(engine, cache, role="transcribe")
        assert client.complete("Read the letter.") == "answer"
        assert len(cli.calls) == 2


class TestDocs:
    def test_the_engine_section_says_so(self):
        readme = (ROOT / "src/yizkr/pipeline/README.md").read_text()
        engine = readme.split("## LLM providers")[1].split("\n## ")[0]
        flat = " ".join(engine.split())
        assert "`ClassifierRefusal`" in flat
        assert "does not ask it again" in flat
        assert "keeps nothing" in flat
