"""Offline tests for the claude-code engine and the subscription's limits
(#101): a call the subscription refuses for a limit is asked again once
the limit lifts, or pauses the pass with the time it can go on, and never
fails a document; calls are paced to leave a reserve of each usage window
to development sessions; every call that reaches the engine is a `call`
event of the run, with or without the LLM cache; and the calls a planned
run is expected to make are estimated per stage.

The fake claude CLI of test_claude_code_pinning stands in for the real
one. Here it also prints what CLI 2.1.237 prints when the API refuses a
call for a limit (a rate_limit_event, the API error's assistant event and
an error result, with exit status 1), and the rate_limit_event of an
answered call. The pacer runs on a fake clock that only its fake sleep
moves on. Documents and names come from the fictional universe
(docs/EXAMPLES.md)."""

from __future__ import annotations

import inspect
import json
import math
import shutil
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.test_chunking import (
    DOC_REF,
    LINE_LENGTH,
    claim,
    entity_manifest_json,
    ndjson,
    transcription,
)
from tests.unit.test_claude_code_pinning import (
    Completed,
    FakeCli,
    install_fake_cli,
)
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import llm
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.llm import (
    BACKOFF,
    BACKOFF_CAP,
    CLOSED,
    DEFAULT_MAX_WAIT,
    DEFAULT_RESERVE,
    ENGINE_CALL,
    MAX_WAIT_ENV,
    OVERAGE,
    OVERLOADED,
    RATE_LIMIT,
    REJECTED,
    RESERVE,
    RESERVE_ENV,
    RESET_MARGIN,
    THROTTLE,
    USAGE_LIMIT,
    AttemptingLlmClient,
    ClaudeCodeClient,
    CliVersionMismatch,
    ConversationalLlmClient,
    ConversationChanged,
    PassPaused,
    Refusal,
    RejectingLlmClient,
    StageEstimate,
    UsageLimitReached,
    cached_client,
    estimate_calls,
    for_attempt,
    make_client,
    observed_client,
    open_cache,
    refusal,
    run_calls,
)
from yizkr.pipeline.orchestrate import AWAITING_CLAIMS, BATCHED, Orchestrator
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.rundir import DOCUMENT_FAILED, INTERRUPTED, run_state

PINNED = "2.1.237"
ROOT = Path(__file__).resolve().parents[2]
# a Unix time in September 2026, where every fake clock starts
START = 1_790_000_000.0
HOUR = 3600.0
# what the critic answers when it finds nothing to flag
CRITIQUE_ANSWER = '{"done": true, "findings_emitted": 0}'
# seconds a test waits for an event before it fails as hung (#437)
HANG_GUARD = 60


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


class Clock:
    """A wall clock for the pacer, which only its sleep moves on."""

    def __init__(self) -> None:
        self.now = START
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float, stop: threading.Event) -> bool:
        if stop.is_set():
            return False
        self.slept.append(round(seconds, 3))
        self.now += seconds
        return True


# -- what the CLI prints ------------------------------------------------------


def usage_window(resets_at: float, window: str = "five_hour") -> dict:
    """A call refused for a used-up usage window, as CLI 2.1.237 reports
    it: the window's rate_limit_info, and its notice."""
    return {
        "info": {
            "status": "rejected",
            "resetsAt": int(resets_at),
            "rateLimitType": window,
            "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled",
            "isUsingOverage": False,
        },
        "status": 429,
        "error": "rate_limit",
        "text": "You've hit your session limit · resets 3pm (UTC)",
    }


# a call refused with no reset time: the server limiting requests, after
# the CLI's own retries
RATE_LIMITED = {
    "info": {"status": "rejected", "isUsingOverage": False},
    "status": 429,
    "error": "rate_limit",
    "text": "API Error: Server is temporarily limiting requests (not your "
    "usage limit) · Rate limited",
}
# an overloaded API, after the CLI's own retries
OVERLOAD = {
    "info": None,
    "status": 529,
    "error": "overloaded",
    "text": "API Error: Repeated 529 Overloaded errors",
}
# an API error that is not a subscription limit: never a refusal (no
# limit status, no limit error, no rejected rate_limit_event), and never
# an answer either, whatever the exit status
API_ERROR = {
    "info": None,
    "status": 500,
    "error": None,
    "text": "API Error: Internal server error",
}


def report(
    utilization: float | None = None,
    *,
    resets_at: float,
    window: str = "five_hour",
    status: str | None = None,
    overage: bool = False,
) -> dict:
    """The rate_limit_info an answered call's stream reports: no share of
    the window below the CLI's warning thresholds, and past one the share
    used."""
    info = {
        "status": status
        or ("allowed" if utilization is None else "allowed_warning"),
        "resetsAt": int(resets_at),
        "rateLimitType": window,
        "isUsingOverage": overage,
    }
    if utilization is not None:
        info.update(utilization=utilization, surpassedThreshold=0.5)
    return info


def refused_events(step: dict, session: str) -> list[dict]:
    """A refused call's events after its init event, as CLI 2.1.237 prints
    them: the rate_limit_event, the assistant event that wraps the API
    error, and the error result."""
    events = []
    if step["info"] is not None:
        events.append(
            {
                "type": "rate_limit_event",
                "rate_limit_info": step["info"],
                "uuid": str(uuid.uuid4()),
                "session_id": session,
            }
        )
    events += [
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": step["text"]}],
            },
            "parent_tool_use_id": None,
            "session_id": session,
            "error": step["error"],
            "is_api_error_message": True,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": step["status"],
            "num_turns": 1,
            "result": step["text"],
            "session_id": session,
        },
    ]
    return events


def stream(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events)


class LimitedCli(FakeCli):
    """The fake claude CLI with the subscription's limits. `script` holds
    what each next call meets, in order: None for an answer, or a refusal
    (usage_window, RATE_LIMITED, OVERLOAD), which the process exits on
    with `exit_status`. `windows` holds the rate_limit_info each answered
    call reports, in order, the last one repeating. Any other program (the
    run manifest's git) runs as `other` runs it."""

    def __init__(self, projects: Path, other) -> None:
        super().__init__(projects)
        self.other = other
        self.script: list[dict | None] = []
        self.windows: list[dict] = []
        self.exit_status = 1

    def __call__(self, command, **kwargs):
        if command[0] != "claude":
            return self.other(command, **kwargs)
        completed = super().__call__(command, **kwargs)
        if command[1:] == ["--version"] or completed.returncode:
            return completed
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        init = events[0]
        step = self.script.pop(0) if self.script else None
        if step is not None:
            return Completed(
                stdout=stream(
                    [init, *refused_events(step, init["session_id"])]
                ),
                returncode=self.exit_status,
            )
        if self.windows:
            info = self.windows.pop(0) if self.windows[1:] else self.windows[0]
            events.insert(
                -1,
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": info,
                    "uuid": str(uuid.uuid4()),
                    "session_id": init["session_id"],
                },
            )
        return Completed(stdout=stream(events))


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """A fresh pacer on a fake clock, and no pacing settings from the
    environment."""
    clock = Clock()
    monkeypatch.setattr(
        llm, "_PACER", llm._Pacer(clock=clock, sleep=clock.sleep)
    )
    for name in (RESERVE_ENV, MAX_WAIT_ENV):
        monkeypatch.delenv(name, raising=False)
    return clock


@pytest.fixture
def cli(monkeypatch, tmp_path, clock) -> LimitedCli:
    other = subprocess.run
    plain = install_fake_cli(monkeypatch, tmp_path)
    limited = LimitedCli(plain.projects, other)
    monkeypatch.setattr(llm.subprocess, "run", limited)
    return limited


def recorder() -> tuple[list[dict], object]:
    """The events a client reports, and the hook that records them."""
    seen: list[dict] = []

    def emit(event, **fields):
        seen.append({"event": event, **fields})

    return seen, emit


# -- reading a refusal --------------------------------------------------------

INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "s1",
    "claude_code_version": PINNED,
}


class TestRefusal:
    """What the engine reads from a call's stream as a refusal for a limit
    (llm.refusal), in the shapes CLI 2.1.237 prints."""

    def test_a_used_up_window_names_the_window_and_its_reset(self):
        events = [INIT, *refused_events(usage_window(START + HOUR), "s1")]
        assert refusal(events, 1) == Refusal(
            USAGE_LIMIT, "five_hour", START + HOUR
        )

    def test_a_refusal_without_a_reset_is_a_rate_limit(self):
        events = [INIT, *refused_events(RATE_LIMITED, "s1")]
        assert refusal(events, 1) == Refusal(RATE_LIMIT)

    def test_a_window_name_that_is_not_a_plain_token_is_unknown(self):
        """rateLimitType only ever reaches a log line or a call event as
        a lowercase, underscore-separated token: anything else (an
        injected newline, say) is read as no window name at all, rather
        than sit there unescaped."""
        events = [
            INIT,
            *refused_events(usage_window(START + HOUR, "five_hour\n"), "s1"),
        ]
        assert refusal(events, 1) == Refusal(RATE_LIMIT)

    def test_an_overload(self):
        events = [INIT, *refused_events(OVERLOAD, "s1")]
        assert refusal(events, 1) == Refusal(OVERLOADED)

    @pytest.mark.parametrize(
        "signal",
        [
            {"type": "result", "is_error": True, "api_error_status": 429},
            {"type": "assistant", "error": "rate_limit", "message": {}},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected"},
            },
        ],
        ids=["api status", "assistant error", "rejected status"],
    )
    def test_each_signal_alone_is_a_refusal(self, signal):
        assert refusal([INIT, signal], 1) == Refusal(RATE_LIMIT)

    def test_a_refusal_is_one_whatever_the_exit_status(self):
        events = [INIT, *refused_events(RATE_LIMITED, "s1")]
        assert refusal(events, 0) == Refusal(RATE_LIMIT)

    @pytest.mark.parametrize(
        "reported",
        [
            # the CLI retried a refusal itself, and the window is open again
            [{"status": "rejected"}, report(resets_at=START + HOUR)],
            # the call ran on usage credits past its used-up window, which
            # the CLI reports as rejected
            [
                {
                    "status": "rejected",
                    "resetsAt": int(START + HOUR),
                    "rateLimitType": "five_hour",
                    "overageStatus": "allowed",
                    "isUsingOverage": True,
                }
            ],
        ],
        ids=["retried", "on usage credits"],
    )
    def test_an_answered_call_was_not_refused(self, reported):
        """A call that answered was not refused, whatever its stream
        reported on the way."""
        events = [
            INIT,
            *(
                {"type": "rate_limit_event", "rate_limit_info": info}
                for info in reported
            ),
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "answer"}]},
            },
            {"type": "result", "is_error": False, "result": "answer"},
        ]
        assert refusal(events, 0) is None

    def test_another_error_is_no_refusal(self):
        failed = {"type": "result", "is_error": True, "api_error_status": 500}
        assert refusal([INIT, failed], 1) is None
        assert refusal([INIT], 1) is None

    def test_a_missing_terminal_result_is_never_read_as_an_answer(self):
        """A stream that ends without its terminal result event (the
        process was killed, or crashed after the API's own refusal) is
        never taken for an answer just because the exit status is 0: a
        zero exit means an answer only once a result event says so."""
        events = [
            INIT,
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": int(START + HOUR),
                    "rateLimitType": "five_hour",
                },
            },
        ]
        assert refusal(events, 0) == Refusal(
            USAGE_LIMIT, "five_hour", START + HOUR
        )


# -- asking again ------------------------------------------------------------


class TestAskingAgain:
    """A refused call is asked again once its limit lifts, and returns the
    engine's answer: the CLI's notice is never an answer. A hold longer
    than a call waits pauses the pass, with the time it can go on."""

    def test_a_rate_limit_is_waited_out(self, cli, clock):
        client = ClaudeCodeClient()
        cli.script = [RATE_LIMITED]
        assert client.complete("Read the letter.") == "answer"
        assert clock.slept == [BACKOFF]
        assert len(cli.calls) == 2
        assert client.last_pacing() == {
            "waited": BACKOFF,
            "refused": 1,
            "held": RATE_LIMIT,
        }
        # the refused attempt's transcript is released like any other
        assert cli.transcripts() == set()

    def test_the_backoff_doubles_with_each_refusal(self, cli, clock):
        client = ClaudeCodeClient()
        cli.script = [RATE_LIMITED, OVERLOAD, RATE_LIMITED]
        assert client.complete("Read the letter.") == "answer"
        assert clock.slept == [60.0, 120.0, 240.0]
        # the backoff ended with the call: the next one waits for nothing
        assert client.complete("Read the letter.") == "answer"
        assert len(clock.slept) == 3
        assert client.last_pacing() == {}

    def test_a_notice_is_never_an_answer(self, cli, clock):
        cli.exit_status = 0
        cli.script = [RATE_LIMITED]
        assert ClaudeCodeClient().complete("Read the letter.") == "answer"
        assert len(cli.calls) == 2

    def test_a_non_limit_error_is_never_an_answer(self, cli, clock):
        """A result event that reports is_error true for a reason that is
        not a subscription limit is never assembled and returned as the
        answer, even when the process exits 0: refusal() correctly finds
        no limit here, but that is not the same as an answer."""
        cli.exit_status = 0
        cli.script = [API_ERROR]
        with pytest.raises(RuntimeError, match="not a subscription limit"):
            ClaudeCodeClient().complete("Read the letter.")
        assert len(cli.calls) == 1

    def test_a_window_that_reopens_within_the_wait_is_waited_for(
        self, cli, clock
    ):
        client = ClaudeCodeClient(max_wait=6 * HOUR)
        resets = START + 2 * HOUR
        cli.script = [usage_window(resets)]
        assert client.complete("Read the letter.") == "answer"
        assert clock.now == resets + RESET_MARGIN
        assert client.last_pacing()["held"] == USAGE_LIMIT
        # the window reopened: nothing holds the next call
        client.complete("Read the letter.")
        assert clock.now == resets + RESET_MARGIN

    def test_a_window_that_reopens_later_pauses_the_pass(self, cli, clock):
        resets = START + 2 * HOUR
        cli.script = [usage_window(resets)]
        client = ClaudeCodeClient()
        with pytest.raises(UsageLimitReached) as paused:
            client.complete("Read the letter.")
        assert isinstance(paused.value, PassPaused)
        assert paused.value.reason == USAGE_LIMIT
        assert paused.value.window == "five_hour"
        assert paused.value.resumes_at == datetime.fromtimestamp(resets, UTC)
        assert "five-hour window is used up" in str(paused.value)
        assert stamp(resets) in str(paused.value)
        assert clock.slept == []
        assert client.last_pacing() == {"refused": 1, "held": USAGE_LIMIT}
        # the hold is the process's: another role's client pauses at once,
        # without a call
        with pytest.raises(UsageLimitReached):
            ClaudeCodeClient(effort="high").complete("Critique the batch.")
        assert len(cli.calls) == 1

    def test_a_limit_mid_fork_asks_the_fork_again(self, cli, clock):
        """The fork is asked again from the same conversation, whose
        session stays held, while the refused fork's is released."""
        client = ClaudeCodeClient()
        _, handle = client.open_conversation("List the entities.")
        cli.script = [RATE_LIMITED]
        assert client.fork(handle, "Extract chunk 1.") == "answer"
        forks = [command for command, _ in cli.calls[1:]]
        assert len(forks) == 2
        assert all(
            command[-3:] == ["--resume", handle, "--fork-session"]
            for command in forks
        )
        assert cli.transcripts() == {f"{handle}.jsonl"}
        client.close()
        assert cli.transcripts() == set()

    def test_a_refused_opening_turn_is_asked_in_a_new_session(
        self, cli, clock
    ):
        client = ClaudeCodeClient()
        cli.script = [RATE_LIMITED]
        _, handle = client.open_conversation("List the entities.")
        refused, answered = (
            command[command.index("--session-id") + 1]
            for command, _ in cli.calls
        )
        assert refused != answered and handle == answered
        assert cli.transcripts() == {f"{handle}.jsonl"}

    def test_a_reset_the_clock_has_passed_backs_off(self, cli, clock):
        """A skewed clock: by the local clock the CLI's reset time has
        passed, and the window is still used up. The engine backs off,
        doubling, rather than asking again at once."""
        stale = usage_window(START - 600)
        cli.script = [stale, stale]
        assert ClaudeCodeClient().complete("Read the letter.") == "answer"
        assert clock.slept == [60.0, 120.0]

    def test_a_reset_too_far_ahead_pauses_the_pass_with_it(self, cli, clock):
        """A skewed clock the other way, or a weekly window: the pause
        gives the reset the CLI reported."""
        resets = START + 3 * 24 * HOUR
        cli.script = [usage_window(resets, "seven_day")]
        with pytest.raises(UsageLimitReached) as paused:
            ClaudeCodeClient(max_wait=6 * HOUR).complete("Read the letter.")
        assert paused.value.window == "seven_day"
        assert paused.value.resumes_at == datetime.fromtimestamp(resets, UTC)
        assert "weekly window" in str(paused.value)

    @pytest.mark.parametrize(
        ("window", "described"),
        [
            ("seven_day_opus", "weekly Opus window"),
            ("seven_day_sonnet", "weekly Sonnet window"),
            (
                "seven_day_oauth_apps",
                "weekly window for apps signed in with OAuth",
            ),
            # a codename: the CLI's own name says as much as any wording
            ("seven_day_omelette", "seven_day_omelette window"),
            ("seven_day_cowork", "weekly Cowork window"),
            ("seven_day_invented", "seven_day_invented window"),
        ],
    )
    def test_a_pause_describes_the_window(self, cli, clock, window, described):
        """Each window name CLI 2.1.288 reports is described in words; a
        name it does not know is given as the CLI names it (#452)."""
        cli.script = [usage_window(START + 3 * 24 * HOUR, window)]
        with pytest.raises(UsageLimitReached) as paused:
            ClaudeCodeClient().complete("Read the letter.")
        assert paused.value.window == window
        assert described in str(paused.value)

    def test_a_rate_limit_that_outlasts_the_wait_pauses_the_pass(
        self, cli, clock
    ):
        """No reset time: the pause says when the engine would have asked
        again."""
        cli.script = [RATE_LIMITED] * 5
        client = ClaudeCodeClient()
        with pytest.raises(UsageLimitReached) as paused:
            client.complete("Read the letter.")
        # 60 + 120 + 240 + 480 s is the backoff sequence's own sum, once
        # every doubling since has been capped (BACKOFF_CAP); the 5th
        # refusal's backoff (480 s again) is what finally takes the
        # call's wait past DEFAULT_MAX_WAIT's margin above that sum
        assert clock.slept == [60.0, 120.0, 240.0, 480.0]
        assert len(cli.calls) == 5
        assert paused.value.reason == RATE_LIMIT
        assert paused.value.window is None
        assert paused.value.resumes_at == datetime.fromtimestamp(
            clock.now + BACKOFF_CAP, UTC
        )
        assert client.last_pacing() == {
            "waited": 900.0,
            "refused": 5,
            "held": RATE_LIMIT,
        }

    def test_a_call_that_never_starts_reports_no_wait(self, cli, clock):
        """What a call waited is its own: one that a paused pass stops
        before it starts reports nothing, not the last call's wait."""
        client = ClaudeCodeClient()
        cli.script = [RATE_LIMITED]
        client.complete("Read the letter.")
        assert client.last_pacing()["refused"] == 1
        client._pin._latch(CliVersionMismatch("the CLI was replaced"))
        with pytest.raises(CliVersionMismatch):
            client.complete("Read the letter.")
        assert client.last_pacing() == {}

    def test_a_closed_client_does_not_wait(self, cli, clock):
        client = ClaudeCodeClient()
        client.close()
        cli.script = [RATE_LIMITED]
        with pytest.raises(UsageLimitReached) as paused:
            client.complete("Read the letter.")
        assert paused.value.reason == CLOSED
        assert clock.slept == []

    def test_closing_the_client_wakes_a_call_that_waits(
        self, cli, clock, monkeypatch
    ):
        """A call waiting out a rate limit waits on the client's stop
        event: the fake sleep waits on nothing else, so only close() can
        wake it, and the hang guard fails a wait it never wakes."""
        waiting = threading.Event()
        woken = []

        def sleep(seconds, stop):
            waiting.set()
            woken.append(stop.wait(HANG_GUARD))
            return not woken[-1]

        monkeypatch.setattr(
            llm, "_PACER", llm._Pacer(clock=clock, sleep=sleep)
        )
        client = ClaudeCodeClient()
        cli.script = [RATE_LIMITED]
        reasons = []

        def call():
            try:
                client.complete("Read the letter.")
            except UsageLimitReached as exc:
                reasons.append(exc.reason)

        # a daemon, so a call close() never wakes fails the test and does
        # not also keep the test process from exiting
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert waiting.wait(HANG_GUARD)
        client.close()
        thread.join(timeout=HANG_GUARD)
        assert not thread.is_alive()
        assert woken == [True]
        assert reasons == [CLOSED]

    def test_the_pacer_waits_by_default_on_the_stop_event(self):
        """The pacer's default sleep is the one close() wakes: it waits
        on the stop event, so a set event ends it at once, before any of
        its seconds pass, and an unset one lets a wait of 0 run out."""
        assert llm._Pacer()._sleep is llm._sleep
        stop = threading.Event()
        assert llm._sleep(0, stop) is True
        stop.set()
        assert llm._sleep(HANG_GUARD, stop) is False


class TestSettings:
    def test_the_defaults(self, cli):
        client = ClaudeCodeClient()
        assert (client.reserve, client.max_wait) == (
            DEFAULT_RESERVE,
            DEFAULT_MAX_WAIT,
        )

    def test_the_default_max_wait_has_a_margin_above_the_backoff_sum(self):
        """DEFAULT_MAX_WAIT sits above the backoff sequence's own sum (60
        + 120 + 240 + 480 s, once every doubling since has been capped),
        not equal to it: an equal value makes a pause's boundary depend
        on a coincidence with the default rather than the schedule's own
        cap (BACKOFF_CAP)."""
        backoff_sum = BACKOFF + BACKOFF * 2 + BACKOFF * 4 + BACKOFF_CAP
        assert backoff_sum == 900.0
        assert backoff_sum < DEFAULT_MAX_WAIT

    def test_the_environment_and_the_factory(self, cli, monkeypatch):
        monkeypatch.setenv(RESERVE_ENV, "0.4")
        monkeypatch.setenv(MAX_WAIT_ENV, "21600")
        client = ClaudeCodeClient()
        assert (client.reserve, client.max_wait) == (0.4, 21600.0)
        assert ClaudeCodeClient(reserve=0.1, max_wait=0).max_wait == 0
        paced = make_client("claude-code", reserve=0.2, max_wait=60)
        assert (paced.reserve, paced.max_wait) == (0.2, 60)
        monkeypatch.setenv(MAX_WAIT_ENV, "inf")
        assert ClaudeCodeClient().max_wait == math.inf

    @pytest.mark.parametrize(
        "name,value",
        [
            (RESERVE_ENV, "1"),
            (RESERVE_ENV, "-0.1"),
            (RESERVE_ENV, "a quarter"),
            (MAX_WAIT_ENV, "-1"),
            (MAX_WAIT_ENV, "nan"),
        ],
    )
    def test_a_setting_out_of_range_is_refused(
        self, cli, monkeypatch, name, value
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            ClaudeCodeClient()

    def test_pacing_changes_no_key(self, cli):
        """The reserve and the longest wait decide when calls are made,
        never what they send."""
        paced = ClaudeCodeClient(reserve=0.5, max_wait=HOUR)
        unpaced = ClaudeCodeClient(reserve=0.0, max_wait=0)
        assert paced.config_hash == unpaced.config_hash


# -- the reserve ------------------------------------------------------------


def admit(pacer, *, reserve=0.25, max_wait=math.inf) -> dict:
    """Admit one call, as a client does; returns what it waited, and why."""
    record = {"waited": 0.0, "refused": 0, "held": None}
    pacer.admit(
        record, reserve=reserve, max_wait=max_wait, stop=threading.Event()
    )
    return record


def calls(pacer, *reports: dict | None) -> None:
    """One call per report, admitted and then completed with the
    rate_limit_info its stream carried (None: it reported nothing)."""
    for info in reports:
        admit(pacer)
        pacer.done([info] if info is not None else [])


class TestReserve:
    """Calls are paced to leave a reserve of each usage window (25% by
    default) to development sessions, from what the calls' streams report
    of the window."""

    @pytest.fixture
    def pacer(self, clock):
        return llm._Pacer(clock=clock, sleep=clock.sleep)

    def test_nothing_is_paced_below_the_clis_first_warning(self, pacer, clock):
        calls(pacer, *[report(resets_at=START + HOUR)] * 20)
        assert admit(pacer) == {"waited": 0.0, "refused": 0, "held": None}
        assert clock.slept == []

    def test_unified_windows_are_not_paced_from(self, pacer, clock):
        """CLI 2.1.288 reports every window's use in unifiedWindows, past
        the warning thresholds or not (#452). The engine paces from
        rate_limit_info's utilization alone, so a report whose
        unifiedWindows say a window is nearly used up, and whose
        utilization is absent, paces nothing."""
        resets = START + HOUR
        info = report(resets_at=resets) | {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.95, "resetsAt": int(resets)},
                "seven_day": {
                    "utilization": 0.9,
                    "resetsAt": int(resets + 24 * HOUR),
                },
            },
            "limitScope": "account",
        }
        calls(pacer, *[info] * 20)
        assert admit(pacer) == {"waited": 0.0, "refused": 0, "held": None}
        assert clock.slept == []

    def test_unified_windows_never_move_the_reported_utilization(
        self, pacer, clock
    ):
        """With utilization reported too, the spacing is the one that
        utilization alone gives (as in the test below), whatever
        unifiedWindows say of the windows (#452 review)."""
        resets = START + HOUR
        unified = {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.95, "resetsAt": int(resets)},
                "seven_day": {
                    "utilization": 0.9,
                    "resetsAt": int(resets + 24 * HOUR),
                },
            },
            "limitScope": "account",
        }
        calls(
            pacer,
            report(0.50, resets_at=resets) | unified,
            *[None] * 9,
            report(0.60, resets_at=resets) | unified,
        )
        assert clock.slept == []
        record = admit(pacer)
        assert clock.slept == [pytest.approx(240.0)]
        assert record["waited"] == pytest.approx(240.0)
        assert record["held"] == THROTTLE

    def test_limit_scope_names_no_window(self, pacer, clock):
        """A report with limitScope and no rateLimitType names no window,
        so its utilization paces nothing (#452 review)."""
        info = report(0.80, resets_at=START + 2 * HOUR)
        del info["rateLimitType"]
        calls(pacer, info | {"limitScope": "five_hour"})
        assert admit(pacer) == {"waited": 0.0, "refused": 0, "held": None}
        assert clock.slept == []

    def test_calls_are_spaced_so_the_headroom_lasts_until_the_reset(
        self, pacer, clock
    ):
        resets = START + HOUR
        # the window's first report, nine calls that report nothing, and a
        # report 0.10 of the window later: 0.01 of it per call
        calls(
            pacer,
            report(0.50, resets_at=resets),
            *[None] * 9,
            report(0.60, resets_at=resets),
        )
        assert clock.slept == []
        record = admit(pacer)
        # 0.15 left above the reserve line, at 0.01 a call, over the hour
        # to the reset: a call every 240 s
        assert clock.slept == [pytest.approx(240.0)]
        assert record["waited"] == pytest.approx(240.0)
        assert record["held"] == THROTTLE

    def test_a_throttle_pause_resumes_at_its_own_next_slot(self, pacer, clock):
        """A throttle hold's own next slot (240 s away, above) is always
        short of the window's full reset (an hour away here): a pause on
        it resumes there, never at the window's raw reset, which could be
        hours away."""
        resets = START + HOUR
        calls(
            pacer,
            report(0.50, resets_at=resets),
            *[None] * 9,
            report(0.60, resets_at=resets),
        )
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=100.0)
        assert paused.value.reason == THROTTLE
        assert paused.value.resumes_at == datetime.fromtimestamp(
            clock.now + 240.0, UTC
        )

    def test_calls_in_flight_count_against_the_reserve(self, pacer, clock):
        """Five calls' worth is left above the line: each call dispatched
        and not yet answered takes its share, so the five are spread
        evenly over the hour, the last one at the reset."""
        resets = START + HOUR
        calls(
            pacer,
            report(0.60, resets_at=resets),
            *[None] * 9,
            report(0.70, resets_at=resets),
        )
        for _ in range(5):
            admit(pacer)
        assert clock.slept == [pytest.approx(720.0)] * 5
        assert clock.now == pytest.approx(resets)

    def test_dispatch_stops_at_the_reserve_line_until_the_reset(
        self, pacer, clock
    ):
        resets = START + 2 * HOUR
        calls(pacer, report(0.76, resets_at=resets))
        record = admit(pacer)
        assert record["held"] == RESERVE
        assert clock.now == resets + RESET_MARGIN

    def test_a_reserve_line_the_window_passes_later_pauses_the_pass(
        self, pacer, clock
    ):
        resets = START + 2 * HOUR
        calls(pacer, report(0.80, resets_at=resets))
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=DEFAULT_MAX_WAIT)
        assert paused.value.reason == RESERVE
        assert paused.value.resumes_at == datetime.fromtimestamp(resets, UTC)
        assert "25%" in str(paused.value)
        assert clock.slept == []

    def test_the_reserve_sets_the_line(self, pacer, clock):
        calls(pacer, report(0.60, resets_at=START + HOUR))
        admit(pacer, reserve=0.25)
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, reserve=0.5, max_wait=0)
        assert paused.value.reason == RESERVE

    def test_usage_credits_are_never_run_on(self, pacer, clock):
        """A call reported to have run on usage credits pauses the pass at
        once, whatever the reserve: the subscription does not say when
        that clears, so the pause is process-wide and holds without end
        (TestOverage covers this in full)."""
        calls(pacer, report(resets_at=START + HOUR, overage=True))
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, reserve=0.0, max_wait=0)
        assert paused.value.reason == OVERAGE
        assert paused.value.resumes_at is None

    def test_a_windows_status_and_utilization_are_monotonic_within_it(
        self, pacer, clock
    ):
        """Reports for one window (the same resetsAt) only ever raise its
        recorded status and utilization: a later report that says less —
        a lower status, or utilization at or below what is already
        recorded, including one with no utilization at all, as a report
        below the CLI's warning thresholds carries — never undoes what a
        higher-ranked report already showed. Both reports are taken in
        before either is admitted against, as two calls' completions
        would be: admitting between them would sleep through the first
        report's own hold and pass the window's reset."""
        resets = START + HOUR
        admit(pacer)
        admit(pacer)
        pacer.done([report(0.80, resets_at=resets)])
        pacer.done([report(resets_at=resets)])  # now below the warnings
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == RESERVE
        assert clock.slept == []

    def test_a_rejected_status_is_never_undone_by_a_later_report(
        self, pacer, clock
    ):
        """A rejected status, once recorded for a window, holds every
        later call for it until it reopens, whatever a later report of
        the same window says — even an ``allowed`` one, which a slower
        call dispatched before the rejection can still deliver after it,
        since reports are taken in completion order, not dispatch
        order."""
        resets = START + HOUR
        admit(pacer)
        admit(pacer)
        pacer.done([report(resets_at=resets, status=REJECTED)])
        pacer.done([report(resets_at=resets)])  # a stale "allowed" report
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == USAGE_LIMIT
        assert clock.slept == []

    def test_a_stale_reports_lower_utilization_is_never_taken(
        self, pacer, clock
    ):
        """A slower call can complete after a faster one's and report the
        same window's utilization as it stood before the faster call's
        own report: the pacer keeps the higher, live figure rather than
        stepping back to the stale, lower one."""
        resets = START + HOUR
        admit(pacer)
        admit(pacer)
        pacer.done([report(0.80, resets_at=resets)])
        pacer.done([report(0.70, resets_at=resets)])  # a stale, lower report
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == RESERVE
        assert clock.slept == []

    def test_a_window_the_clock_has_passed_starts_afresh(self, pacer, clock):
        """Once the clock passes a window's reset, the window holds
        nothing, and the use per call measured in it does not pace the next
        one."""
        resets = START + HOUR
        calls(
            pacer,
            report(0.60, resets_at=resets),
            report(0.80, resets_at=resets),
        )
        clock.now = resets + 1
        admit(pacer, max_wait=0)
        pacer.done([report(0.72, resets_at=resets + 5 * HOUR)])
        admit(pacer, max_wait=0)
        assert clock.slept == []

    def test_a_window_reported_with_a_new_reset_starts_afresh(
        self, pacer, clock
    ):
        """A new reset time names a new window even before the local clock
        passes the old one (a skewed clock)."""
        resets = START + HOUR
        for _ in range(3):
            admit(pacer)
        pacer.done([report(0.60, resets_at=resets)])
        pacer.done([report(0.70, resets_at=resets)])
        pacer.done([report(0.72, resets_at=resets + 5 * HOUR)])
        admit(pacer, max_wait=0)
        assert clock.slept == []

    def test_a_client_paces_by_what_its_calls_report(self, cli, clock):
        resets = START + HOUR
        cli.windows = [
            report(0.50, resets_at=resets),
            report(0.60, resets_at=resets),
        ]
        client = ClaudeCodeClient(max_wait=HOUR)
        client.complete("Read the first letter.")
        client.complete("Read the second letter.")
        assert clock.slept == []
        client.complete("Read the third letter.")
        # 0.10 of the window a call, 0.15 left above the line: a call every
        # 2400 s
        assert clock.slept == [pytest.approx(2400.0)]
        assert client.last_pacing()["held"] == THROTTLE


class TestOverage:
    """An isUsingOverage report stops dispatch at once, process-wide,
    whatever else it says or leaves out of the window it names — an
    allowed status (test_usage_credits_are_never_run_on), no
    rateLimitType, no resetsAt, or one the clock has already passed.
    Each is a way a per-window hold alone would have let the report
    through: _take returns early with no rateLimitType; a window with
    no resetsAt never gets a hold in _hold; and a window whose resetsAt
    has passed is deleted there, its use forgotten. The overage latch is
    checked ahead of all of that (_Pacer._hold). This only ever stops
    dispatch once a report says a call already ran on credits: the
    reported call, and any others already in flight when the report
    lands, may already have used credits."""

    @pytest.fixture
    def pacer(self, clock):
        return llm._Pacer(clock=clock, sleep=clock.sleep)

    def test_no_rate_limit_type(self, pacer, clock):
        pacer.done([{"isUsingOverage": True}])
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == OVERAGE
        assert paused.value.resumes_at is None

    def test_no_resets_at(self, pacer, clock):
        pacer.done(
            [
                {
                    "isUsingOverage": True,
                    "rateLimitType": "five_hour",
                    "status": "rejected",
                }
            ]
        )
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == OVERAGE
        assert paused.value.resumes_at is None

    def test_a_resets_at_the_clock_has_already_passed(self, pacer, clock):
        pacer.done([report(resets_at=START - HOUR, overage=True)])
        clock.now = START
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        assert paused.value.reason == OVERAGE
        assert paused.value.resumes_at is None


class TestOverageDocs:
    """The overage guarantee, wherever it is written down, admits what
    it cannot guarantee: dispatch stops only once a call is *reported*
    to have run on usage credits, so that call, and any others already
    in flight when the report lands, may already have used them. Only
    turning extra usage off on the account guarantees none does
    (`_Pacer._take` latches the pause from the report; nothing local
    aborts a call already dispatched)."""

    CAVEAT = "already in flight"
    OVERCLAIM = "never runs on usage credits"

    @pytest.fixture
    def pacer(self, clock):
        return llm._Pacer(clock=clock, sleep=clock.sleep)

    def test_the_module_comment_states_the_caveat_and_not_the_overclaim(
        self,
    ):
        source = inspect.getsource(llm)
        bullet = source.split(
            "# * pauses dispatch process-wide the moment a report", 1
        )[1]
        bullet = bullet.split("\n#\n", 1)[0]
        assert self.CAVEAT in bullet
        assert self.OVERCLAIM not in bullet

    def test_the_docstring_states_the_caveat_and_not_the_overclaim(self):
        doc = UsageLimitReached.__doc__
        assert doc is not None
        assert self.CAVEAT in doc
        assert self.OVERCLAIM not in doc

    def test_the_pause_message_states_the_caveat_and_not_the_overclaim(
        self, pacer
    ):
        pacer.done([report(resets_at=START + HOUR, overage=True)])
        with pytest.raises(UsageLimitReached) as paused:
            admit(pacer, max_wait=0)
        message = str(paused.value)
        assert self.CAVEAT in message
        assert self.OVERCLAIM not in message

    def test_the_log_warning_states_the_caveat_and_not_the_overclaim(
        self, pacer, caplog
    ):
        # An overage hold's own `until` is math.inf, so `admit` always
        # raises it as a pause rather than waiting for it (any finite
        # max_wait is exceeded at once): `_announce` is exercised
        # directly, as the only way this hold reaches it.
        with caplog.at_level("WARNING"):
            pacer._announce(llm._Hold(math.inf, OVERAGE), reserve=0.25)
        [message] = [r.getMessage() for r in caplog.records]
        assert self.CAVEAT in message
        assert self.OVERCLAIM not in message

    def test_llm_cache_doc_states_the_caveat_and_not_the_overclaim(self):
        text = (ROOT / "docs" / "LLM_CACHE.md").read_text()
        bullet = text.split(
            "a report that a call ran on usage credits (`isUsingOverage`)",
            1,
        )[1]
        bullet = bullet.split("\n\n", 1)[0]
        assert self.CAVEAT in bullet
        assert f"the run {self.OVERCLAIM}" not in text

    def test_run_manifest_doc_states_the_caveat(self):
        text = (ROOT / "docs" / "RUN_MANIFEST.md").read_text()
        bullet = text.split(
            "except a report that a call ran on usage credits", 1
        )[1]
        assert self.CAVEAT in bullet[:400]


class TestUtilizationDocs:
    """`surpassedThreshold` is not part of the CLI's own client-side
    warning thresholds (the 90/75/50/25 table), so nothing here
    attributes it to that table; the field is not read by any code, so
    it is not described here at all."""

    def test_llm_py_does_not_mention_surpassed_threshold(self):
        assert "surpassedThreshold" not in inspect.getsource(llm)

    def test_llm_cache_doc_does_not_mention_surpassed_threshold(self):
        text = (ROOT / "docs" / "LLM_CACHE.md").read_text()
        assert "surpassedThreshold" not in text


class TestUnifiedWindowsDocs:
    """CLI 2.1.288 reports a window's use below its warning thresholds in
    unifiedWindows (#452), so nothing here says it reports none, and
    every description of what it reports says the engine reads neither
    unifiedWindows nor limitScope."""

    # the wordings of the claim that the CLI reports no use below its
    # warning thresholds, or that the reserve paces from whatever it sends
    OVERCLAIMS = (
        "no utilization is reported at all",
        "no `utilization` is reported at all",
        "the use of a window below the CLI's warning",
        "the CLI itself reports nothing to pace against",
        "only ever paces from a report the CLI has actually sent",
        "there is no `utilization` to pace against",
        "the CLI reports a window's use only once",
    )

    @staticmethod
    def normalised(text: str) -> str:
        return " ".join(text.split())

    def texts(self) -> dict[str, str]:
        return {
            "llm.py": self.normalised(inspect.getsource(llm)),
            "LLM_CACHE.md": self.normalised(
                (ROOT / "docs" / "LLM_CACHE.md").read_text()
            ),
            "README.md": self.normalised(
                (ROOT / "src" / "yizkr" / "pipeline" / "README.md").read_text()
            ),
        }

    def test_no_text_makes_the_old_claim(self):
        for name, text in self.texts().items():
            for overclaim in self.OVERCLAIMS:
                assert overclaim not in text, (name, overclaim)

    def test_llm_py_says_the_engine_reads_neither(self):
        source = inspect.getsource(llm)
        section = source.split("# -- subscription limits", 1)[1]
        section = self.normalised(section.split("RATE_LIMIT_EVENT =", 1)[0])
        assert "unifiedWindows" in section and "limitScope" in section
        assert "# the engine reads neither" in section
        assert "never from # unifiedWindows" in section

    def test_llm_cache_doc_says_the_engine_reads_neither(self):
        text = self.texts()["LLM_CACHE.md"]
        assert "`unifiedWindows`" in text and "`limitScope`" in text
        assert "the engine reads neither" in text

    def test_adr_0025_is_amended(self):
        text = (
            ROOT / "docs" / "adr" / "0025-development-by-scratch-runs.md"
        ).read_text()
        amendment = text.split("Amended by #452 (2026-10-04):", 1)[1]
        amendment = self.normalised(amendment.split("\n\n", 1)[0])
        assert "`unifiedWindows`" in amendment
        assert "reads neither" in amendment


class TestPinDocs:
    """The docs name 2.1.288 as the pin and say how to install it, and the
    pipeline README names both settings that keep a call as it ran on
    2.1.237 (#452 review)."""

    RUNBOOK = ROOT / "docs" / "runbooks" / "scratch-run.md"
    README = ROOT / "src" / "yizkr" / "pipeline" / "README.md"

    def test_the_runbook_installs_exactly_the_pin(self):
        text = self.RUNBOOK.read_text()
        assert "npm install -g @anthropic-ai/claude-code@2.1.288" in text
        assert "export DISABLE_AUTOUPDATER=1" in text
        flat = " ".join(text.split())
        assert "the binary an editor bundles" in flat
        assert "is no pin" in flat

    def test_no_text_names_the_old_pin(self):
        texts = {
            "llm.py": inspect.getsource(llm),
            "LLM_CACHE.md": (ROOT / "docs" / "LLM_CACHE.md").read_text(),
            "scratch-run.md": self.RUNBOOK.read_text(),
            "README.md": self.README.read_text(),
        }
        for name, text in texts.items():
            flat = " ".join(text.split())
            for old in ("pinned 2.1.237", "claude-code@2.1.237"):
                assert old not in flat, (name, old)
            assert "2.1.288" in flat, name

    def test_the_readme_names_both_settings(self):
        text = self.README.read_text()
        assert "`--permission-mode default`" in text
        assert "`CLAUDE_CODE_HARBOR_KITE=0`" in text


# -- engine calls as events ---------------------------------------------------


class Plain:
    """An engine's client with no conversations, and none of the optional
    attributes a key reads (the engine's name, model, effort, system
    prompt, call text)."""

    model_identifier = "fake-model"
    config_hash = "c" * 64

    def complete(self, prompt, image_paths=()):
        return f"an answer to {prompt}"


class TestCallEvents:
    """Every call that reaches the engine is a call event, with or without
    the LLM cache; the calls the cache answers are none."""

    def test_an_answered_call(self, cli, clock):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(), role="critique", events=emit
        )
        assert client.complete("Critique the batch.") == "answer"
        (event,) = seen
        assert event["event"] == ENGINE_CALL == "call"
        assert (
            event["role"],
            event["stage"],
            event["call"],
            event["outcome"],
        ) == ("critique", "critique", "complete", "ok")
        assert event["duration_s"] >= 0
        assert not {"wait_s", "refused", "held", "error"} & event.keys()

    def test_a_call_that_waited_says_how_long_and_why(self, cli, clock):
        cli.script = [RATE_LIMITED]
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(), role="transcribe", events=emit
        )
        client.complete("Read the letter.")
        (event,) = seen
        assert event["stage"] == "transcription"
        assert event["outcome"] == "ok"
        assert (event["wait_s"], event["refused"], event["held"]) == (
            BACKOFF,
            1,
            RATE_LIMIT,
        )

    def test_a_paused_call_says_when_the_pass_can_go_on(self, cli, clock):
        resets = START + 2 * HOUR
        cli.script = [usage_window(resets)]
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(), role="extract", events=emit
        )
        with pytest.raises(UsageLimitReached):
            client.complete("List the entities.")
        (event,) = seen
        assert (event["outcome"], event["error"]) == (
            "paused",
            "UsageLimitReached",
        )
        assert (event["held"], event["window"]) == (USAGE_LIMIT, "five_hour")
        assert event["resumes_at"] == stamp(resets)
        assert event["refused"] == 1

    def test_a_failed_call_names_its_error_never_its_message(self):
        class Failing(Plain):
            def complete(self, prompt, image_paths=()):
                raise RuntimeError("Klara Löbig was born in 1917")

        seen, emit = recorder()
        with pytest.raises(RuntimeError):
            observed_client(Failing(), role="critique", events=emit).complete(
                "Critique the batch."
            )
        (event,) = seen
        assert (event["outcome"], event["error"]) == ("error", "RuntimeError")
        assert "Klara" not in json.dumps(event)

    def test_openings_and_forks_are_calls(self, cli, clock):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(), role="extract", events=emit
        )
        assert isinstance(client, ConversationalLlmClient)
        _, handle = client.open_conversation("List the entities.")
        client.fork(handle, "Extract chunk 1.")
        client.close_conversation(handle)
        assert [event["call"] for event in seen] == ["open", "fork"]

    def test_the_wrapper_adds_no_capability(self):
        plain = observed_client(Plain(), role="critique", events=print)
        assert not isinstance(plain, ConversationalLlmClient)
        assert not isinstance(plain, RejectingLlmClient)
        assert not isinstance(plain, AttemptingLlmClient)
        assert for_attempt(plain, 1) is plain
        engine = Plain()
        assert observed_client(engine, role="critique", events=None) is engine

    def test_an_event_that_cannot_be_recorded_fails_no_call(self, caplog):
        def broken(event, **fields):
            raise OSError("disk full")

        client = observed_client(Plain(), role="critique", events=broken)
        assert client.complete("x") == "an answer to x"
        assert "could not record an engine call" in caplog.text

    @pytest.mark.parametrize("make", [Plain, "claude-code"])
    def test_the_wrapper_changes_no_key(self, cli, clock, tmp_path, make):
        engine = ClaudeCodeClient() if make == "claude-code" else make()
        scan = tmp_path / "scan.png"
        scan.write_bytes(b"png placeholder")
        cache = open_cache(tmp_path / "cache")
        bare = cached_client(engine, cache, role="transcribe")
        observed = cached_client(
            observed_client(engine, role="transcribe", events=print),
            cache,
            role="transcribe",
        )
        assert bare.complete_key("Read it.", [scan]) == observed.complete_key(
            "Read it.", [scan]
        )

    def test_a_session_held_for_the_run_is_the_engines(
        self, cli, clock, tmp_path
    ):
        """A replay that answers differently holds its session for the
        run, and the conversation opened again takes it through any client
        of the same engine, whichever document's calls that client
        reports (ADR-0032 §6)."""
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient()

        def client(seen):
            def emit(event, **fields):
                seen.append(event)

            return cached_client(
                observed_client(engine, role="extract", events=emit),
                cache,
                role="extract",
                events=emit,
            )

        cli.answer = "entities: e1"
        client([]).open_conversation("List the entities.")
        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        first = client([])
        _, handle = first.open_conversation("List the entities.")
        cli.answer = "entities: e1, e2"
        with pytest.raises(ConversationChanged):
            first.fork(handle, "Extract chunk 1.")
        seen: list[str] = []
        other = client(seen)
        _, handle = other.open_conversation("List the entities.")
        cli.answer = "chunk 1"
        assert other.fork(handle, "Extract chunk 1.") == "chunk 1"
        assert "cache-resume" not in seen and "cache-replay" not in seen


class TestReplay:
    def test_a_limit_during_a_replay_is_no_failed_replay(
        self, cli, clock, tmp_path
    ):
        """A conversation the cache answered, whose kept session is gone, is
        replayed for a fork. A replay the subscription refuses pauses the
        pass and is not remembered as failed: once the window reopens the
        fork replays, and is answered (ADR-0032 §6)."""
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient()
        seen: list[tuple] = []

        def emit(event, **fields):
            seen.append((event, fields.get("call"), fields.get("outcome")))

        def client():
            return cached_client(
                observed_client(engine, role="extract", events=emit),
                cache,
                role="extract",
                events=emit,
            )

        client().open_conversation("List the entities.")
        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        caching = client()
        _, handle = caching.open_conversation("List the entities.")
        resets = START + 2 * HOUR
        cli.script = [usage_window(resets)]
        seen.clear()
        with pytest.raises(UsageLimitReached):
            caching.fork(handle, "Extract chunk 1.")
        assert handle.failed is None
        # no miss: the fork was never asked
        assert seen == [(ENGINE_CALL, "open", "paused")]
        assert not list((tmp_path / "cache").glob("*/*.rejected-*"))

        clock.now = resets + HOUR
        assert caching.fork(handle, "Extract chunk 1.") == "answer"
        assert [event for event, _, _ in seen[1:]] == [
            ENGINE_CALL,
            "cache-replay",
            "cache-miss",
            ENGINE_CALL,
        ]


class TestChunks:
    def test_a_limit_mid_fork_fails_no_chunk(self):
        """A pause is no chunk's failure: it ends the extraction, and the
        conversation is let go."""

        class Engine:
            model_identifier = "fake-model"

            def __init__(self):
                self.closed = []

            def complete(self, prompt, image_paths=()):  # pragma: no cover
                raise AssertionError("every chunk forks")

            def open_conversation(self, prompt):
                return entity_manifest_json(), "session-1"

            def fork(self, handle, prompt):
                if "- e2:" in prompt:
                    raise UsageLimitReached("used up", reason=USAGE_LIMIT)
                return ndjson([claim("e1", "Hildegard Löbig", 1876)])

            def close_conversation(self, handle):
                self.closed.append(handle)

        engine = Engine()
        extractor = ChunkedClaimExtractor(engine, budget=LINE_LENGTH)
        with pytest.raises(UsageLimitReached):
            extractor.extract(transcription(), DOC_REF)
        assert extractor.failed_chunks == []
        assert engine.closed == ["session-1"]

    def test_a_pool_makes_no_call_after_the_pass_has_paused(self):
        """ThreadPoolExecutor's work item catches BaseException, so once
        one chunk's fork raises UsageLimitReached, the worker moves on to
        the next queued chunk rather than stop (even at concurrency 1,
        the default): a fresh call for it starts at waited=0, and would
        not exceed its own max_wait on the very hold that just paused the
        first — unless the pacer's cross-call latch (_Pacer._latch)
        re-raises that pause for it at once, before it ever calls the
        engine."""

        class PooledEngine:
            """Forks admit through a real pacer, retrying a scripted
            count of refusals the way ClaudeCodeClient._run does: enough
            refusals for the first chunk's own accumulated wait to pause
            the pass, none for the second, which would otherwise dispatch
            once it reads the same hold fresh."""

            model_identifier = "fake-model"

            def __init__(self, pacer, refusals, *, max_wait):
                self._pacer = pacer
                self._refusals = list(refusals)
                self._max_wait = max_wait
                self.calls: list[str] = []

            def open_conversation(self, prompt):
                return entity_manifest_json(), "session-1"

            def fork(self, handle, prompt):
                left = self._refusals.pop(0) if self._refusals else 0
                record = {"waited": 0.0, "refused": 0, "held": None}
                while True:
                    self._pacer.admit(
                        record,
                        reserve=0.25,
                        max_wait=self._max_wait,
                        stop=threading.Event(),
                    )
                    if not left:
                        self.calls.append(prompt)
                        return ndjson([claim("e1", "Hildegard Löbig", 1876)])
                    left -= 1
                    record["refused"] += 1
                    self._pacer.refused(Refusal(RATE_LIMIT), record["refused"])

            def close_conversation(self, handle):
                pass

        clock = Clock()
        pacer = llm._Pacer(clock=clock, sleep=clock.sleep)
        # two refusals (60 s, then 120 s) push the first chunk's own
        # accumulated wait (60 + 120 = 180) past 150 s; the second
        # chunk's fresh call would face the same 120 s hold alone
        # (well under 150 s) and dispatch, without the latch
        engine = PooledEngine(pacer, refusals=[2, 0], max_wait=150.0)
        extractor = ChunkedClaimExtractor(engine, budget=LINE_LENGTH)
        with pytest.raises(UsageLimitReached):
            extractor.extract(transcription(), DOC_REF)
        assert engine.calls == []


# -- the pipeline -------------------------------------------------------------


def quiet(*args, **kwargs):
    pass


def a_batch(label: str, year: int) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "fake-model",
                "pipeline_version": "0.1.0",
                "started_at": "2026-07-14T09:00:00Z",
            },
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e1",
                        "label": label,
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": year},
                    "source": {"artifact": DOC_REF.model_dump()},
                }
            ],
            "entities": [],
        }
    )


LETTERS = {
    "a-letter.txt": ("Klara Löbig", 1917),
    "b-letter.txt": ("Gustav Löbig", 1919),
}


def a_folder(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    for name, (label, year) in LETTERS.items():
        (folder / name).write_text(f"{label} was born in {year} in Flörburg.")
    return folder


class Run(Orchestrator):
    """The wiki steps scripted, a text transcription, a fixed batch for
    each letter, and the critic for real, through the claude-code engine
    on the fake CLI."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("exclusions", ExclusionList())
        kwargs.setdefault("llm", "claude-code")
        kwargs.setdefault("log", quiet)
        super().__init__(*args, **kwargs)

    def _step_register(self, doc):
        return "Q23"

    def _step_extract(self, doc, transcription):
        return self._run_stage(
            doc, "extraction", lambda: a_batch(*LETTERS[doc.source.name])
        )

    def _step_resolve(self, doc, batch):
        return batch

    def _step_export_review(self, doc, batch):
        return export_review_csv(batch, doc.review)


class TestNoDocumentFails:
    """A limit never fails a document: the run pauses, and the next run
    goes on from where it stopped."""

    @pytest.mark.parametrize(
        "cached", [False, True], ids=["no cache", "cache"]
    )
    def test_a_limit_on_the_last_document_pauses_the_run(
        self, cli, clock, tmp_path, cached
    ):
        cli.answer = CRITIQUE_ANSWER
        resets = START + 2 * HOUR
        # the first letter's critique is answered, the last one's refused
        cli.script = [None, usage_window(resets)]
        options = {"cache_dir": tmp_path / "cache" if cached else None}
        run = Run(a_folder(tmp_path), tmp_path / "state", **options)
        with pytest.raises(UsageLimitReached):
            run.run()
        first, last = run.documents()
        assert first.state.status == AWAITING_CLAIMS
        assert last.state.status == BATCHED
        assert last.state.error is None
        events = run.rundir.events()
        assert not [event for event in events if event["event"] == "failed"]
        assert not [
            event for event in events if event["event"] == DOCUMENT_FAILED
        ]
        (invocation,) = run.rundir.manifest()["invocations"]
        assert (invocation["outcome"], invocation["reason"]) == (
            "paused",
            "UsageLimitReached",
        )
        # the engine's calls, with or without the cache
        made = [event for event in events if event["event"] == ENGINE_CALL]
        assert [(e["subject"], e["role"], e["outcome"]) for e in made] == [
            (first.sha1, "critique", "ok"),
            (last.sha1, "critique", "paused"),
        ]
        assert made[-1]["kind"] == "text"
        assert made[-1]["resumes_at"] == stamp(resets)
        # the stage was cut short, not failed: the paused invocation has
        # ended, so the stage reads as interrupted (#300), and the next run
        # makes it
        state = run_state(run.rundir)[(last.sha1, "critique")]
        assert state.status == INTERRUPTED

        clock.now = resets + HOUR
        again = Run(a_folder(tmp_path), tmp_path / "state", **options)
        assert again.run() == 2
        first, last = again.documents()
        assert last.state.status == AWAITING_CLAIMS
        # only the last letter's critique is asked again
        assert len(cli.calls) == 3

    def test_calls_the_cache_answers_are_no_engine_calls(
        self, cli, clock, tmp_path
    ):
        cli.answer = CRITIQUE_ANSWER
        cache = tmp_path / "cache"
        Run(a_folder(tmp_path), tmp_path / "state", cache_dir=cache).run()
        fresh = Run(a_folder(tmp_path), tmp_path / "fresh", cache_dir=cache)
        fresh.run()
        names = [event["event"] for event in fresh.rundir.events()]
        assert names.count("cache-hit") == 2
        assert ENGINE_CALL not in names
        assert len(cli.calls) == 2


# -- the estimate -------------------------------------------------------------

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
EXTRACTED = ["transcription", "extraction", "critique"]


class TestEstimate:
    """The calls a planned run is expected to make for its stale work, per
    stage (`yizkr scratch estimate`)."""

    def test_a_runs_calls_are_counted_from_its_events(self):
        def event(name, subject, invocation="i1", **fields):
            return {
                "event": name,
                "subject": subject,
                "invocation": invocation,
                **fields,
            }

        events = [
            # with the cache: its hits and misses, and a miss's engine
            # call is not counted again
            event("cache-hit", A, role="extract"),
            event("cache-miss", A, role="extract"),
            event(ENGINE_CALL, A, role="extract"),
            event("done", A, stage="extraction"),
            # without it: the engine's calls
            event(ENGINE_CALL, B, role="critique"),
            event(ENGINE_CALL, B, role="critique"),
            event("done", B, stage="critique"),
            # a stage made without a call
            event("done", B, stage="transcription"),
            # a stage that paused, then was made in the next invocation
            event(ENGINE_CALL, C, role="critique", outcome="paused"),
            event(ENGINE_CALL, C, "i2", role="critique"),
            event(ENGINE_CALL, C, "i2", role="critique"),
            event("done", C, "i2", stage="critique"),
            # a view reused later keeps the count it was made with
            event("reused", B, "i2", stage="critique"),
        ]
        assert run_calls(events) == {
            (A, "extraction"): 2,
            (B, "critique"): 2,
            (B, "transcription"): 0,
            (C, "critique"): 2,
        }

    def test_the_stale_work_per_stage(self):
        plan = [
            {"sha1": A, "kind": "media", "stages": EXTRACTED},
            {"sha1": B, "kind": "text", "stages": EXTRACTED},
            {"sha1": C, "kind": "gedcom", "stages": ["gedcom"]},
            {"sha1": D, "kind": "media", "stages": EXTRACTED},
        ]
        baseline = {
            (A, "transcription"): 1,
            (A, "extraction"): 7,
            (A, "critique"): 1,
            (B, "extraction"): 4,
        }
        # B's transcription stands; D takes the baseline's mean for a scan
        estimate = estimate_calls(
            plan, current={(B, "transcription")}, baseline=baseline
        )
        assert estimate == {
            "transcription": StageEstimate(documents=2, calls=2, measured=1),
            "extraction": StageEstimate(documents=3, calls=18, measured=2),
            "critique": StageEstimate(documents=3, calls=3, measured=1),
            "gedcom": StageEstimate(documents=1, calls=0, measured=0),
        }

    def test_typical_calls_without_a_baseline(self):
        plan = [
            {"sha1": A, "kind": "text", "stages": EXTRACTED},
            {"sha1": B, "kind": "media", "stages": EXTRACTED},
            {"sha1": C, "kind": "presentation", "stages": EXTRACTED},
        ]
        estimate = estimate_calls(plan, sizes={A: 5000})
        # a scan's vision call; the manifest and a chunk per 2000
        # characters (three for A), else one; a critique each
        assert estimate == {
            "transcription": StageEstimate(documents=3, calls=1),
            "extraction": StageEstimate(documents=3, calls=4 + 2 + 2),
            "critique": StageEstimate(documents=3, calls=3),
        }

    def test_a_run_estimates_its_own_rerun(self, cli, clock, tmp_path):
        cli.answer = CRITIQUE_ANSWER
        run = Run(a_folder(tmp_path), tmp_path / "state")
        run.run()
        estimate = estimate_calls(
            run.rundir.manifest()["documents"],
            baseline=run_calls(run.rundir.events()),
        )
        assert estimate == {
            "transcription": StageEstimate(documents=2, calls=0, measured=2),
            "extraction": StageEstimate(documents=2, calls=0, measured=2),
            "critique": StageEstimate(documents=2, calls=2, measured=2),
        }
