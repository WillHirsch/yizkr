"""Offline tests for what a claude-code call's stream reports of the call
(#98, ADR-0032): the model it ran on, the CLI version, the session, the
tokens it used, its turns, how it ended and why its model stopped (#7),
parsed from the stream's init and result events; kept beside the answer
in the call's LLM cache entry, the stop reason included (#390), never in
its key;
carried, but for the session, by the call's event; and totalled per stage
and role from the run's events for the run report. An answer whose model
stopped at the output limit (max_tokens) is cut off, whatever it holds: a
chunk's is rejected and asked again, as one without its sentinel is (#7).

The streams are written by hand in the shape CLI 2.1.237 prints them, and
the fake claude CLI of test_engine_limits prints them for the real
engine's client. The claude CLI itself is never run. Every name, text and
number is invented (docs/EXAMPLES.md).
"""

from __future__ import annotations

import copy
import functools
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import uuid
from pathlib import Path

import pytest

from tests.fixtures.fake_anthropic import api_engine
from tests.unit.test_chunking import (
    DOC_REF,
    claim,
    entity_manifest_json,
    ndjson,
    transcription,
)
from tests.unit.test_claude_code_pinning import Completed, install_fake_cli
from tests.unit.test_critique import batch as critique_batch
from tests.unit.test_engine_limits import (
    HOUR,
    RATE_LIMITED,
    START,
    Clock,
    LimitedCli,
    recorder,
    usage_window,
)
from tests.unit.test_places import PRISON, link, peiltal
from yizkr.pipeline import anomalies, llm
from yizkr.pipeline.anomalies import AnomalyChecks, load_config
from yizkr.pipeline.chunking import ChunkedClaimExtractor, ExtractionFailed
from yizkr.pipeline.critique import run_critique
from yizkr.pipeline.extract import LlmClaimExtractor
from yizkr.pipeline.llm import (
    COMPLETE,
    ENGINE_CALL,
    MAX_WAIT_ENV,
    RESERVE_ENV,
    USAGE_TOKENS,
    ClaudeCodeClient,
    ConversationChanged,
    OutputLimitReached,
    UsageLimitReached,
    at_output_limit,
    cached_client,
    for_attempt,
    observed_client,
    open_cache,
    stream_report,
)
from yizkr.pipeline.llmcache import call_key
from yizkr.pipeline.places import adjudicate, build_worklist
from yizkr.pipeline.rundir import RunDir, usage_counts

PINNED = "2.1.237"
MODEL = "claude-sonnet-5"
# the dated model a stream reports running an undated name on
RESOLVED = "claude-sonnet-5-20260901"
SESSION = "8f14e45f-ceea-467a-9b36-0c3c4a9e1a11"
ANSWER = "Hutmacherei zum Blauen Hut, Brunnenring 35, Flörburg"

# -- streams as CLI 2.1.237 prints them ---------------------------------------

INIT = (
    '{"type":"system","subtype":"init","cwd":"/tmp/yizkr-claude-code",'
    f'"session_id":"{SESSION}","tools":["Read"],"mcp_servers":[],'
    f'"model":"{RESOLVED}","permissionMode":"default","slash_commands":[],'
    '"apiKeySource":"none","claude_code_version":"2.1.237",'
    '"output_style":"default","agents":[],"skills":[],"plugins":[],'
    '"uuid":"0b6f1a52-3c1d-4d0e-9a7e-2f5d8c1e4b90"}'
)
ASSISTANT = (
    '{"type":"assistant","message":{"model":"' + RESOLVED + '",'
    '"id":"msg_01Flrb35Hutmacherei","type":"message","role":"assistant",'
    '"content":[{"type":"text","text":"' + ANSWER + '"}],'
    '"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":3,'
    '"cache_creation_input_tokens":2048,"cache_read_input_tokens":11264,'
    '"output_tokens":24,"service_tier":"standard"}},'
    f'"parent_tool_use_id":null,"session_id":"{SESSION}",'
    '"uuid":"5e2c9d41-7a3b-4f60-8c1d-9b0e6f2a3d57"}'
)
RATE_LIMIT = (
    '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
    '"resetsAt":1790010000,"rateLimitType":"five_hour",'
    '"isUsingOverage":false},'
    f'"uuid":"c7d0e3f2-1b4a-4e8d-a6c9-3f5b7d9e1a24","session_id":"{SESSION}"}}'
)
RESULT = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"duration_ms":5120,"duration_api_ms":4870,"num_turns":2,'
    '"result":"' + ANSWER + '","stop_reason":"end_turn",'
    f'"session_id":"{SESSION}","total_cost_usd":0.0412,'
    '"usage":{"input_tokens":3,"cache_creation_input_tokens":2048,'
    '"cache_read_input_tokens":11264,"output_tokens":24,'
    '"server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},'
    '"service_tier":"standard","cache_creation":'
    '{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":2048}},'
    '"modelUsage":{"' + RESOLVED + '":{"inputTokens":3,"outputTokens":24,'
    '"cacheReadInputTokens":11264,"cacheCreationInputTokens":2048,'
    '"webSearchRequests":0,"costUSD":0.0412,"contextWindow":200000}},'
    '"permission_denials":[],"uuid":"e1a4b7c0-2d3f-4a5b-8c6d-7e8f9a0b1c2d"}'
)
# a call that ran out of turns: an error result, with no answer, and the
# stop reason of the last answer its model gave
MAX_TURNS = (
    '{"type":"result","subtype":"error_max_turns","is_error":true,'
    '"duration_ms":90210,"duration_api_ms":88004,"num_turns":30,'
    '"stop_reason":"tool_use",'
    f'"session_id":"{SESSION}","total_cost_usd":0.91,'
    '"usage":{"input_tokens":61,"cache_creation_input_tokens":8192,'
    '"cache_read_input_tokens":204800,"output_tokens":5120,'
    '"service_tier":"standard"},"permission_denials":[],"errors":[],'
    '"uuid":"9d8c7b6a-5f4e-4d3c-2b1a-0f9e8d7c6b5a"}'
)
# what the parser keeps of RESULT's usage: the four token counts alone
TOKENS = {
    "input_tokens": 3,
    "output_tokens": 24,
    "cache_creation_input_tokens": 2048,
    "cache_read_input_tokens": 11264,
}
REPORT = {
    "model": RESOLVED,
    "cli_version": PINNED,
    "session_id": SESSION,
    "usage": TOKENS,
    "num_turns": 2,
    "subtype": "success",
    "stop_reason": "end_turn",
}


def lines(*events: str) -> str:
    """A stream, as the CLI prints it: one event per line."""
    return "\n".join(events) + "\n"


def report_of(stdout: str) -> dict:
    return stream_report(llm._stream_events(stdout))


def usage(input=0, output=0, write=0, read=0) -> dict:
    """A result event's usage, as the CLI gives it: the four token counts
    and the details the engine does not keep."""
    return {
        "input_tokens": input,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": output,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
    }


def tokens(input=0, output=0, write=0, read=0) -> dict:
    """What the parser keeps of `usage(...)`."""
    return {
        "input_tokens": input,
        "output_tokens": output,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
    }


# -- parsing ------------------------------------------------------------------


class TestStreamReport:
    def test_the_init_and_result_events(self):
        stdout = lines(INIT, RATE_LIMIT, ASSISTANT, RESULT)
        assert report_of(stdout) == REPORT

    def test_a_stream_with_no_init_names_no_model(self):
        """The session comes from the result event then."""
        report = report_of(lines(ASSISTANT, RESULT))
        assert "model" not in report and "cli_version" not in report
        assert report["session_id"] == SESSION
        assert report["usage"] == TOKENS

    def test_a_stream_with_no_result_reports_no_usage(self):
        report = report_of(lines(INIT, ASSISTANT))
        assert report == {
            "model": RESOLVED,
            "cli_version": PINNED,
            "session_id": SESSION,
        }

    def test_an_error_result(self):
        report = report_of(lines(INIT, MAX_TURNS))
        assert (report["subtype"], report["num_turns"]) == (
            "error_max_turns",
            30,
        )
        assert report["usage"] == tokens(61, 5120, 8192, 204800)
        assert report["stop_reason"] == "tool_use"

    def test_a_result_cut_off_at_the_output_limit(self):
        """Why the model stopped on the call's final turn: the result
        event's stop_reason, max_tokens when that turn hit the output
        limit (#7)."""
        cut = json.loads(RESULT) | {"stop_reason": "max_tokens"}
        report = report_of(lines(INIT, ASSISTANT, json.dumps(cut)))
        assert report["stop_reason"] == "max_tokens"

    def test_an_assistant_events_stop_reason_is_not_read(self):
        """An assistant event is printed as its content block completes,
        before the model's stop reason is known: the stop reason is the
        result event's alone (#7)."""
        assistant = json.loads(ASSISTANT)
        assistant["message"]["stop_reason"] = "max_tokens"
        stdout = lines(INIT, json.dumps(assistant), RESULT)
        assert report_of(stdout)["stop_reason"] == "end_turn"
        assert "stop_reason" not in report_of(
            lines(INIT, json.dumps(assistant))
        )

    def test_a_stream_cut_off_in_its_result_event(self):
        """A torn last line is dropped, as every reader of the stream drops
        it: its usage, turns and subtype are unknown, not zero."""
        stdout = lines(INIT, ASSISTANT) + RESULT[: len(RESULT) // 2]
        assert report_of(stdout) == {
            "model": RESOLVED,
            "cli_version": PINNED,
            "session_id": SESSION,
        }

    def test_a_result_with_no_usage(self):
        result = json.loads(RESULT)
        del result["usage"]
        report = report_of(lines(INIT, json.dumps(result)))
        assert "usage" not in report
        assert report["num_turns"] == 2

    @pytest.mark.parametrize(
        "value", [-1, True, 2.5, "24", None, [24]], ids=repr
    )
    def test_a_token_count_in_another_form_is_left_out(self, value):
        result = json.loads(RESULT)
        result["usage"]["output_tokens"] = value
        report = report_of(lines(INIT, json.dumps(result)))
        assert report["usage"] == {
            name: count
            for name, count in TOKENS.items()
            if name != "output_tokens"
        }

    def test_a_usage_with_no_token_count_is_none(self):
        result = json.loads(RESULT)
        result["usage"] = {"service_tier": "standard"}
        assert "usage" not in report_of(lines(INIT, json.dumps(result)))
        result["usage"] = "3 in, 24 out"
        assert "usage" not in report_of(lines(INIT, json.dumps(result)))

    @pytest.mark.parametrize(
        ("event", "field", "value"),
        [
            ("init", "model", "claude sonnet\nALERT"),
            ("init", "model", ""),
            ("init", "model", 5),
            ("init", "claude_code_version", "2.1.237 (Claude Code)"),
            ("init", "session_id", "../../projects/elsewhere"),
            ("result", "subtype", "Error: see /home/<user>/notes"),
            ("result", "num_turns", -2),
            ("result", "num_turns", False),
            ("result", "stop_reason", "max tokens\nALERT"),
            ("result", "stop_reason", "Max_Tokens"),
            ("result", "stop_reason", 7),
        ],
    )
    def test_a_field_in_another_form_is_left_out(self, event, field, value):
        """A name flows into cache entries, call events and alerts, so
        only a plain token is kept; a count must be a whole number."""
        init, result = json.loads(INIT), json.loads(RESULT)
        {"init": init, "result": result}[event][field] = value
        if field == "session_id":
            result["session_id"] = value
        report = report_of(lines(json.dumps(init), json.dumps(result)))
        name = {"claude_code_version": "cli_version"}.get(field, field)
        assert name not in report

    def test_the_first_init_and_the_last_result_count(self):
        later_init = json.loads(INIT) | {"model": "claude-haiku-5"}
        earlier = json.loads(RESULT) | {
            "num_turns": 1,
            "subtype": "other",
            "stop_reason": "max_tokens",
        }
        report = report_of(
            lines(INIT, json.dumps(later_init), json.dumps(earlier), RESULT)
        )
        assert (report["model"], report["num_turns"]) == (RESOLVED, 2)
        assert (report["subtype"], report["stop_reason"]) == (
            "success",
            "end_turn",
        )

    def test_a_selector_is_part_of_the_model_id(self):
        init = json.loads(INIT) | {"model": "claude-opus-5-5[1m]"}
        assert report_of(lines(json.dumps(init)))["model"] == (
            "claude-opus-5-5[1m]"
        )


# -- the engine's client ------------------------------------------------------


class StreamCli(LimitedCli):
    """The fake claude CLI of test_engine_limits, whose init event names
    `model` and whose result events report what CLI 2.1.237 reports of a
    call: `result` goes into an answered call's result event, `refused`
    into a refused one's. `edit`, when set, rewrites every call's events
    last, and `cuts` holds, for each next call in order, how many of its
    events it prints before its timeout kills it, as a process killed
    mid-stream prints them (None: it is not cut off). `exits` holds, for
    each next call in order, the status it exits with (None: its own)."""

    def __init__(self, projects: Path, other) -> None:
        super().__init__(projects, other)
        self.model = RESOLVED
        self.result: dict = {
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "stop_reason": "end_turn",
            "usage": usage(3, 24, 2048, 11264),
        }
        # a refused call's result gives the stop reason of the API error's
        # assistant event (test_engine_limits.refused_events)
        self.refused: dict = {
            "usage": usage(5),
            "stop_reason": "stop_sequence",
        }
        self.edit = None
        self.cuts: list[int | None] = []
        self.exits: list[int | None] = []

    def __call__(self, command, **kwargs):
        completed = super().__call__(command, **kwargs)
        if command[0] != "claude" or command[1:] == ["--version"]:
            return completed
        events = [
            json.loads(line) for line in completed.stdout.split("\n") if line
        ]
        for event in events:
            if event.get("type") == "result":
                event.update(
                    self.refused if event.get("is_error") else self.result
                )
        if self.edit is not None:
            events = self.edit(events)
        text = "\n".join(json.dumps(event) for event in events)
        cut = self.cuts.pop(0) if self.cuts else None
        if cut is not None:
            partial = "\n".join(json.dumps(event) for event in events[:cut])
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output=partial.encode()
            )
        status = self.exits.pop(0) if self.exits else None
        return Completed(
            stdout=text,
            returncode=completed.returncode if status is None else status,
        )


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
def cli(monkeypatch, tmp_path, clock) -> StreamCli:
    other = subprocess.run
    plain = install_fake_cli(monkeypatch, tmp_path)
    fake = StreamCli(plain.projects, other)
    monkeypatch.setattr(llm.subprocess, "run", fake)
    return fake


def without(kind: str):
    """An edit that drops every event of this type."""
    return lambda events: [e for e in events if e.get("type") != kind]


class TestTheClient:
    def test_an_answered_call(self, cli):
        client = ClaudeCodeClient(model=MODEL)
        assert client.last_stream() == client.last_report() == {}
        assert client.complete("Read the letter.") == "answer"
        stream = client.last_stream()
        assert stream == {
            "model": RESOLVED,
            "cli_version": PINNED,
            "session_id": stream["session_id"],
            "usage": tokens(3, 24, 2048, 11264),
            "num_turns": 2,
            "subtype": "success",
            "stop_reason": "end_turn",
        }
        assert uuid.UUID(stream["session_id"])
        assert client.last_report() == stream

    def test_an_error_result_is_reported_and_answers_nothing(self, cli):
        """A call that runs out of turns fails; what its stream reported
        is the call's, and the last answered call's report stands."""
        client = ClaudeCodeClient(model=MODEL)
        client.complete("Read the letter.")
        answered = client.last_report()
        cli.result = json.loads(MAX_TURNS)
        with pytest.raises(RuntimeError, match="not a subscription limit"):
            client.complete("Read the next letter.")
        stream = client.last_stream()
        assert (stream["subtype"], stream["num_turns"]) == (
            "error_max_turns",
            30,
        )
        assert stream["usage"] == tokens(61, 5120, 8192, 204800)
        assert client.last_report() == answered

    def test_a_stream_with_no_init_or_no_result(self, cli):
        """An answer whose stream had no result event is kept with
        nothing of the usage, turns or subtype the answer before it
        reported."""
        client = ClaudeCodeClient(model=MODEL)
        cli.edit = without("system")
        client.complete("Read the letter.")
        assert "model" not in client.last_stream()
        assert client.last_stream()["usage"] == tokens(3, 24, 2048, 11264)
        # the pinned version stands in for the one no stream reported
        assert client.last_report()["cli_version"] == PINNED
        cli.edit = without("result")
        assert client.complete("Read the letter.") == "answer"
        assert set(client.last_stream()) == {
            "model",
            "cli_version",
            "session_id",
        }
        assert set(client.last_report()) == {
            "model",
            "cli_version",
            "session_id",
        }

    def test_a_call_cut_off_by_its_timeout(self, cli):
        """The init event came, the result never did."""
        client = ClaudeCodeClient(model=MODEL)
        cli.cuts = [1]
        with pytest.raises(subprocess.TimeoutExpired):
            client.complete("Read the letter.")
        assert set(client.last_stream()) == {
            "model",
            "cli_version",
            "session_id",
        }
        assert client.last_report() == {}

    def test_usage_is_summed_over_a_calls_attempts(self, cli, clock):
        """An attempt the subscription refused used what its stream
        reports too; the rest is the answering attempt's."""
        cli.script = [RATE_LIMITED]
        client = ClaudeCodeClient(model=MODEL)
        client.complete("Read the letter.")
        stream = client.last_stream()
        assert stream["usage"] == tokens(3 + 5, 24, 2048, 11264)
        assert (stream["subtype"], stream["num_turns"]) == ("success", 2)
        assert stream["stop_reason"] == "end_turn"

    def test_a_calls_fields_are_its_final_attempts(self, cli, clock):
        """An attempt the subscription refused, then one its timeout cut
        off after its init event: how the call ended is unknown, whatever
        the refused attempt's result said, and its session is the final
        attempt's; the usage is still both attempts'."""
        cli.script = [RATE_LIMITED]
        cli.cuts = [None, 1]
        client = ClaudeCodeClient(model=MODEL)
        with pytest.raises(subprocess.TimeoutExpired):
            client.complete("List the entities.")
        stream = client.last_stream()
        assert not {"subtype", "num_turns", "stop_reason", "api_error"} & set(
            stream
        )
        assert stream["usage"] == tokens(5)
        refused, final = (
            command[command.index("--session-id") + 1]
            for command, _ in cli.calls
        )
        assert stream["session_id"] == final != refused

    def test_a_call_that_never_ran_reports_nothing_of_an_earlier_one(
        self, cli, clock
    ):
        """The earlier call was refused once, so it has a report and a
        wait to leak."""
        cli.script = [RATE_LIMITED]
        client = ClaudeCodeClient(model=MODEL)
        client.complete("Read the letter.")
        assert client.last_stream()
        assert client.last_pacing()
        with pytest.raises(TypeError):
            client.complete("Read the scan.", [object()])
        assert client.last_stream() == {}
        assert client.last_pacing() == {}

    def test_a_report_is_its_threads(self, cli):
        client = ClaudeCodeClient(model=MODEL)
        seen = []

        def call():
            client.complete("Read the letter.")
            seen.append(client.last_stream())

        thread = threading.Thread(target=call)
        thread.start()
        thread.join()
        assert seen[0]["usage"] == tokens(3, 24, 2048, 11264)
        assert client.last_stream() == {}

    def test_a_report_handed_out_cannot_change_the_clients(self, cli):
        client = ClaudeCodeClient(model=MODEL)
        client.complete("Read the letter.")
        client.last_stream()["usage"]["input_tokens"] = 10**9
        client.last_report()["usage"]["input_tokens"] = 10**9
        assert client.last_stream()["usage"]["input_tokens"] == 3
        assert client.last_report()["usage"]["input_tokens"] == 3


# -- call events --------------------------------------------------------------


class TestCallEvents:
    def test_a_call_event_carries_the_report_but_the_session(self, cli):
        seen, emit = recorder()
        engine = ClaudeCodeClient(model=MODEL)
        observed_client(engine, role="critique", events=emit).complete(
            "Critique the batch."
        )
        (event,) = seen
        assert event["event"] == ENGINE_CALL
        assert {
            name: event[name]
            for name in ("model", "cli_version", "usage", "num_turns")
        } == {
            "model": RESOLVED,
            "cli_version": PINNED,
            "usage": tokens(3, 24, 2048, 11264),
            "num_turns": 2,
        }
        assert (event["subtype"], event["stop_reason"]) == (
            "success",
            "end_turn",
        )
        session = engine.last_report()["session_id"]
        assert "session_id" not in event
        assert session not in json.dumps(event)
        assert "replay" not in event

    def test_a_call_cut_off_at_the_output_limit_says_so(self, cli):
        """The call answered, and its event says the model stopped at the
        output limit (#7)."""
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="extract", events=emit
        )
        cli.result["stop_reason"] = "max_tokens"
        assert client.complete("Extract the claims.") == "answer"
        (event,) = seen
        assert (event["outcome"], event["stop_reason"]) == (
            "ok",
            "max_tokens",
        )

    def test_a_failed_call_carries_what_its_stream_reported(self, cli):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="extract", events=emit
        )
        cli.result = json.loads(MAX_TURNS)
        with pytest.raises(RuntimeError):
            client.complete("Extract the claims.")
        (event,) = seen
        assert (event["outcome"], event["subtype"], event["num_turns"]) == (
            "error",
            "error_max_turns",
            30,
        )
        assert event["usage"] == tokens(61, 5120, 8192, 204800)

    def test_a_paused_call_carries_its_refused_attempts_usage(
        self, cli, clock
    ):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="extract", events=emit
        )
        cli.script = [usage_window(START + 2 * HOUR)]
        with pytest.raises(UsageLimitReached):
            client.complete("List the entities.")
        (event,) = seen
        assert event["outcome"] == "paused"
        assert event["usage"] == tokens(5)

    def test_a_call_whose_stream_reports_nothing_carries_none_of_it(self, cli):
        """Nothing of the call before it, either: a fork whose stream has
        neither an init nor a result event carries no report."""
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="extract", events=emit
        )
        _, handle = client.open_conversation("List the entities.")
        client.fork(handle, "Extract chunk 1.")
        cli.edit = lambda events: [
            e for e in events if e.get("type") == "assistant"
        ]
        client.fork(handle, "Extract chunk 2.")
        client.close_conversation(handle)
        assert [event["call"] for event in seen] == ["open", "fork", "fork"]
        assert "usage" in seen[1]
        assert not set(llm.CALL_STREAM_FIELDS) & seen[2].keys()

    def test_a_call_that_never_ran_carries_nothing_of_an_earlier_one(
        self, cli
    ):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="transcribe", events=emit
        )
        client.complete("Read the letter.")
        with pytest.raises(TypeError):
            client.complete("Read the scan.", [object()])
        assert "usage" in seen[0]
        assert seen[1]["outcome"] == "error"
        assert not set(llm.CALL_STREAM_FIELDS) & seen[1].keys()


# -- the LLM cache ------------------------------------------------------------

# the key the LLM cache gives GOLDEN_PROMPT, a one-shot call of a claude-code
# client for claude-sonnet-5 at effort low on CLI 2.1.237 running in
# GOLDEN_TMP, as the cache keyed it before calls' stream reports were kept,
# and with the client's configuration of today: --permission-mode default
# and CLAUDE_CODE_HARBOR_KITE=0 are in its config hash (#452), so they gave
# the key a new value; the stream reports never did
GOLDEN_TMP = "/yizkr-fixed-tmp"
GOLDEN_PROMPT = "Critique the batch of Sebastian Löbig's marriage certificate."
GOLDEN_KEY = "80ae1e74aea58fb6e0cc9d4859b890ca65fa110dccc661784d999248653cec8b"


def caching(engine, cache, run=None, role="extract", **kwargs):
    """The engine behind the cache, with every call it makes and what the
    cache does reported to the run, as a run's clients are."""
    events = functools.partial(run.emit, **kwargs) if run is not None else None
    return cached_client(
        observed_client(engine, role=role, events=events),
        cache,
        role=role,
        events=events,
    )


def entry_of(cache_root: Path, key: str) -> dict:
    return json.loads((cache_root / key[:2] / f"{key}.json").read_text())


def names(run: RunDir) -> list[str]:
    return [event["event"] for event in run.events()]


class TestTheCache:
    def test_an_entry_keeps_the_report_beside_the_answer(self, cli, tmp_path):
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        response = caching(engine, cache).complete("Read the letter.")
        entry = entry_of(tmp_path / "cache", response.key)
        provenance = entry["provenance"]
        # all of the report, the stop reason included, which at_output_limit
        # judges an answer served from the entry by (#390)
        assert {name: provenance[name] for name in REPORT} == {
            **REPORT,
            "session_id": provenance["session_id"],
        }
        assert provenance["stop_reason"] == "end_turn"
        assert uuid.UUID(provenance["session_id"])
        # none of it is keyed: the key names the model configured, never
        # the one the stream reports
        components = entry["components"]
        assert call_key(components) == response.key
        assert components["model"] == MODEL
        assert not (set(llm.STREAM_FIELDS) - {"model"}) & set(components)
        assert "usage" not in components
        flat = json.dumps(components)
        assert RESOLVED not in flat and provenance["session_id"] not in flat

    def test_the_key_is_the_one_the_cache_gave_before(
        self, cli, tmp_path, monkeypatch
    ):
        """An entry the cache kept before stream reports were parsed is
        still served under its key: the report changes no key."""
        monkeypatch.setattr(tempfile, "tempdir", GOLDEN_TMP)
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL, effort="low")
        client = caching(engine, cache, role="critique")
        key, components = client.complete_call(GOLDEN_PROMPT)
        assert key == GOLDEN_KEY
        # the entry as it was written then, with no report beside it
        folder = tmp_path / "cache" / key[:2]
        folder.mkdir(mode=0o700)
        (folder / f"{key}.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "key": key,
                    "components": components,
                    "response": "Keine Befunde.",
                    "origin": "engine",
                    "provenance": {
                        "role": "critique",
                        "call": COMPLETE,
                        "engine": "claude-code",
                        "model": RESOLVED,
                        "cli_version": PINNED,
                        "config_hash": components["config"],
                        "model_identifier": engine.model_identifier,
                        "at": "2026-09-28T09:00:00+00:00",
                    },
                }
            )
        )
        answer = client.complete(GOLDEN_PROMPT)
        assert (answer, answer.hit) == ("Keine Befunde.", True)
        assert cli.calls == []

    def test_a_calls_report_never_reaches_its_key(self, cli, tmp_path):
        """Another model, session, usage, ending or stop reason of the same
        call keys it the same: the second is answered from the cache."""
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        first = caching(engine, cache).complete("Read the letter.")
        cli.model = "claude-sonnet-5-20261101"
        cli.result = {
            "num_turns": 7,
            "usage": usage(9, 9, 9, 9),
            "stop_reason": "max_tokens",
        }
        again = caching(engine, cache).complete("Read the letter.")
        assert again.key == first.key and again.hit
        assert len(cli.calls) == 1

    def test_a_hit_is_no_call_and_records_no_usage(self, cli, tmp_path):
        run = RunDir(tmp_path / "run")
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        first = caching(engine, cache, run).complete("Read the letter.")
        path = tmp_path / "cache" / first.key[:2] / f"{first.key}.json"
        kept = path.read_bytes()
        again = caching(engine, cache, run).complete("Read the letter.")
        assert again.hit
        assert names(run) == ["cache-miss", ENGINE_CALL, "cache-hit"]
        assert "usage" not in run.events()[-1]
        assert path.read_bytes() == kept
        assert usage_counts(run)["extraction"]["extract"]["calls"] == 1

    def test_an_adoption_is_no_call_and_records_no_usage(self, cli, tmp_path):
        """An adopted answer keeps the provenance its seeder gives, and
        nothing of the engine's last call, and serving it is no call."""
        run = RunDir(tmp_path / "run")
        cache = open_cache(tmp_path / "cache")
        client = caching(
            ClaudeCodeClient(model=MODEL), cache, run, role="transcribe"
        )
        client.complete("Read the letter.")
        key, components = client.complete_call("Read the scan.")
        assert client.seed(
            key,
            "Photograph Brüsrich, Peiltal",
            {"engine": "archived tool", "model_identifier": "archive"},
            reason="archived",
            components=components,
        )
        assert client.complete("Read the scan.").hit
        assert names(run)[-2:] == ["cache-adopt", "cache-hit"]
        assert len(cli.calls) == 1
        provenance = entry_of(tmp_path / "cache", key)["provenance"]
        assert not {"usage", "session_id", "num_turns"} & set(provenance)
        assert usage_counts(run)["transcription"]["transcribe"] == zero(
            calls=1, reported=1, **tokens(3, 24, 2048, 11264)
        )

    def test_a_replay_is_a_call_marked_as_one(self, cli, tmp_path):
        """A conversation whose kept session is gone is replayed for a
        fork's session: a call, whose event says it is a replay and
        carries what the replay used. When it answers as the cache did,
        the entry keeps the original call's report."""
        run = RunDir(tmp_path / "run")
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        opened, _ = caching(engine, cache, run).open_conversation(
            "List the entities."
        )
        original = entry_of(tmp_path / "cache", opened.key)["provenance"]
        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        client = caching(engine, cache, run)
        _, handle = client.open_conversation("List the entities.")
        cli.result = {"num_turns": 1, "usage": usage(7, 7, 0, 7)}
        client.fork(handle, "Extract chunk 1.")
        calls = [e for e in run.events() if e["event"] == ENGINE_CALL]
        assert [(e["call"], e.get("replay")) for e in calls] == [
            ("open", None),
            ("open", True),
            ("fork", None),
        ]
        assert calls[1]["usage"] == tokens(7, 7, 0, 7)
        assert "cache-replay" in names(run)
        after = entry_of(tmp_path / "cache", opened.key)["provenance"]
        assert after == original
        assert after["usage"] == tokens(3, 24, 2048, 11264)

    def test_a_replay_that_answers_differently_keeps_its_own_report(
        self, cli, tmp_path
    ):
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        opened, _ = caching(engine, cache).open_conversation(
            "List the entities."
        )
        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        client = caching(engine, cache)
        _, handle = client.open_conversation("List the entities.")
        cli.answer = "entities: Sebastian Löbig, Hildegard Laudler"
        cli.result = {"num_turns": 1, "usage": usage(7, 7, 0, 7)}
        with pytest.raises(ConversationChanged):
            client.fork(handle, "Extract chunk 1.")
        provenance = entry_of(tmp_path / "cache", opened.key)["provenance"]
        assert provenance["recomputed"] is True
        assert provenance["usage"] == tokens(7, 7, 0, 7)


# -- usage per stage and role -------------------------------------------------


def call(run, stage, role, used=None, **fields):
    """A call event of `role` in `stage`, whose stream reported `used`."""
    run.emit(ENGINE_CALL, stage=stage, role=role, usage=used, **fields)


def zero(**counts) -> dict:
    """A total of usage_counts: nothing, but for `counts`."""
    nothing = {"calls": 0, "reported": 0, **dict.fromkeys(USAGE_TOKENS, 0)}
    return nothing | counts


class TestUsageCounts:
    def test_usage_is_summed_by_stage_and_role(self, tmp_path):
        run = RunDir(tmp_path / "run")
        call(run, "extraction", "extract", tokens(10, 2, 100, 1000))
        call(run, "extraction", "extract", tokens(5, 1, 0, 500))
        call(run, "extraction", "extract")  # cut off: nothing reported
        call(run, "critique", "critique", tokens(7, 3))
        run.emit("cache-hit", role="extract", key="k", reason="key match")
        # a later invocation's calls count too
        later = RunDir(tmp_path / "run")
        call(later, "extraction", "extract", tokens(1, 1, 1, 1))
        assert usage_counts(run) == {
            "extraction": {
                "extract": zero(
                    calls=4,
                    reported=3,
                    input_tokens=16,
                    output_tokens=4,
                    cache_creation_input_tokens=101,
                    cache_read_input_tokens=1501,
                )
            },
            "critique": {
                "critique": zero(
                    calls=1, reported=1, input_tokens=7, output_tokens=3
                )
            },
        }

    def test_replays_are_told_apart(self, tmp_path):
        run = RunDir(tmp_path / "run")
        call(run, "extraction", "extract", tokens(10), call="open")
        call(run, "extraction", "extract", tokens(4), call="open", replay=True)
        assert usage_counts(run, replays=True)["extraction"]["extract"] == (
            zero(calls=1, reported=1, input_tokens=4)
        )
        assert usage_counts(run, replays=False)["extraction"]["extract"] == (
            zero(calls=1, reported=1, input_tokens=10)
        )
        assert usage_counts(run)["extraction"]["extract"]["calls"] == 2

    def test_what_a_call_does_not_say_counts_under_a_question_mark(
        self, tmp_path
    ):
        run = RunDir(tmp_path / "run")
        run.emit(ENGINE_CALL, usage=tokens(2))
        assert usage_counts(run) == {
            "?": {"?": zero(calls=1, reported=1, input_tokens=2)}
        }

    @pytest.mark.parametrize("value", [-3, True, 1.5, "12", None], ids=repr)
    def test_a_count_in_another_form_is_left_out(self, tmp_path, value):
        run = RunDir(tmp_path / "run")
        call(run, "critique", "critique", {"input_tokens": value})
        call(run, "critique", "critique", tokens(1) | {"output_tokens": value})
        assert usage_counts(run)["critique"]["critique"] == zero(
            calls=2, reported=1, input_tokens=1
        )

    def test_a_run_with_no_calls(self, tmp_path):
        assert usage_counts(RunDir(tmp_path / "run")) == {}

    def test_a_usage_of_zeros_is_reported(self, tmp_path):
        """A call whose stream reported using no tokens reported its
        usage: it is no call with nothing reported."""
        run = RunDir(tmp_path / "run")
        call(run, "critique", "critique", tokens())
        assert usage_counts(run)["critique"]["critique"] == zero(
            calls=1, reported=1
        )

    def test_a_runs_calls_through_the_engine(self, cli, tmp_path):
        run = RunDir(tmp_path / "run")
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        for _ in range(2):  # the second is answered from the cache
            caching(engine, cache, run, role="critique").complete(
                "Critique the batch."
            )
        client = observed_client(engine, role="transcribe", events=run.emit)
        client.complete("Read the letter.")
        counts = usage_counts(run)
        assert counts["critique"]["critique"] == zero(
            calls=1, reported=1, **tokens(3, 24, 2048, 11264)
        )
        assert counts["transcription"]["transcribe"]["calls"] == 1


# -- the model check ----------------------------------------------------------


class TestModelCheck:
    def test_a_call_on_another_model_raises_an_alert(self, cli, tmp_path):
        """From the stream to the alert: the manifest names the role's
        model, the call's stream reports another."""
        run = RunDir(tmp_path / "run")
        run.update_manifest(
            roles={"critique": {"model": MODEL, "client": {"model": MODEL}}}
        )
        checks = AnomalyChecks(run, load_config())
        cli.model = "claude-haiku-5-20260801"
        subject = "a" * 40
        client = caching(
            ClaudeCodeClient(model=MODEL),
            open_cache(tmp_path / "cache"),
            run,
            role="critique",
            subject=subject,
            kind="text",
        )
        client.complete("Critique the batch.")
        checks.poll()
        (alert,) = run.alerts()
        assert (alert["check"], alert["value"], alert["limit"]) == (
            "model-mismatch",
            "claude-haiku-5-20260801",
            MODEL,
        )
        assert (alert["role"], alert["against"], alert["status"]) == (
            "critique",
            "manifest",
            "new",
        )
        assert (alert["stage"], alert["subject"]) == ("critique", "a" * 12)
        assert anomalies.default_config().checks.model_mismatch.enabled


# -- answers cut off at the output limit --------------------------------------

# a whole chunk answer on the two people of test_chunking's document, and
# the same answer cut off in its second claim, before its sentinel
BOTH = [
    claim("e1", "Hildegard Löbig", 1876),
    claim("e2", "Sebastian Löbig", 1862),
]
WHOLE_CHUNK = ndjson(BOTH)
CUT_CHUNK = json.dumps(BOTH[0]) + "\n" + json.dumps(BOTH[1])[:40]


def answering(*answers):
    """An edit that gives each next call, in order, its answer and the stop
    reason of its model, as CLI 2.1.237 prints them: the text in the
    assistant event's text block, whose message's stop reason is null as it
    is printed, and in the result event's `result`, beside its
    `stop_reason`. An answer may be an edit of its own instead, such as
    gave_up."""
    queue = list(answers)

    def edit(events):
        answer = queue.pop(0)
        if callable(answer):
            return answer(events)
        text, stop_reason = answer
        for event in events:
            if event.get("type") == "assistant":
                event["message"] |= {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": None,
                }
            elif event.get("type") == "result":
                event.update(result=text, stop_reason=stop_reason)
        return events

    return edit


class TestAtOutputLimit:
    """Whether an answer was cut off at the engine's output limit: whether
    the engine reported that its model stopped at max_tokens on the call
    that gave it (#7)."""

    def test_the_claude_code_engines_answer(self, cli):
        client = ClaudeCodeClient(model=MODEL)
        cli.result["stop_reason"] = "max_tokens"
        assert at_output_limit(client, client.complete("Extract chunk 1."))
        cli.result["stop_reason"] = "end_turn"
        answer = client.complete("Extract chunk 1.")
        assert not at_output_limit(client, answer)
        # a stream with no result event says nothing of its model's stop
        cli.result["stop_reason"] = "max_tokens"
        cli.edit = without("result")
        answer = client.complete("Extract chunk 1.")
        assert not at_output_limit(client, answer)

    def test_through_the_observed_and_the_cached_client(self, cli, tmp_path):
        engine = ClaudeCodeClient(model=MODEL)
        cli.result["stop_reason"] = "max_tokens"
        observed = observed_client(
            engine, role="extract", events=recorder()[1]
        )
        assert at_output_limit(observed, observed.complete("Extract."))
        cached = caching(engine, open_cache(tmp_path / "cache"))
        answer = cached.complete("Extract chunk 2.")
        assert not answer.hit
        assert at_output_limit(cached, answer)

    def test_an_answer_the_cache_kept_is_judged_by_its_stop_reason(
        self, cli, tmp_path
    ):
        """A hit was made by no call now: it is judged by the stop reason
        its entry keeps, never by the engine's report of its last call
        (#390)."""
        engine = ClaudeCodeClient(model=MODEL)
        cli.result["stop_reason"] = "max_tokens"
        client = caching(engine, open_cache(tmp_path / "cache"))
        client.complete("Extract chunk 1.")
        again = client.complete("Extract chunk 1.")
        assert again.hit and again.stop_reason == "max_tokens"
        assert at_output_limit(client, again)
        # an entry whose model ended its turn, whatever the last call says
        cli.result["stop_reason"] = "end_turn"
        client.complete("Extract chunk 2.")
        cli.result["stop_reason"] = "max_tokens"
        client.complete("Extract chunk 3.")
        whole = client.complete("Extract chunk 2.")
        assert whole.hit and engine.last_report()["stop_reason"] == (
            "max_tokens"
        )
        assert not at_output_limit(client, whole)

    def test_an_entry_kept_without_a_stop_reason_is_judged_by_its_text(
        self, cli, tmp_path
    ):
        """An entry stored before entries kept the stop reason is served,
        and judged by its text alone, as before (#390)."""
        engine = ClaudeCodeClient(model=MODEL)
        cli.result["stop_reason"] = "max_tokens"
        cache_root = tmp_path / "cache"
        client = caching(engine, open_cache(cache_root))
        first = client.complete("Extract chunk 1.")
        path = cache_root / first.key[:2] / f"{first.key}.json"
        entry = json.loads(path.read_text())
        del entry["provenance"]["stop_reason"]
        path.write_text(json.dumps(entry))
        again = client.complete("Extract chunk 1.")
        assert again.hit and again == first
        assert again.stop_reason is None
        assert not at_output_limit(client, again)

    def test_the_api_clients_answer(self, monkeypatch):
        """The API says on its response why its model stopped; a stop
        reason in another form than a plain token, or none, is left out."""
        stops = ["max_tokens", "end_turn", "Max Tokens", None]

        class Messages:
            def create(self, **request):
                text = types.SimpleNamespace(type="text", text="an answer")
                return types.SimpleNamespace(
                    model=RESOLVED, content=[text], stop_reason=stops.pop(0)
                )

        package = types.ModuleType("anthropic")
        package.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
        monkeypatch.setitem(sys.modules, "anthropic", package)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test-key")
        engine = llm.AnthropicClient(model=MODEL)
        answer = engine.complete("Extract the claims.")
        assert engine.last_report() == {
            "model": RESOLVED,
            "stop_reason": "max_tokens",
        }
        assert at_output_limit(engine, answer)
        answer = engine.complete("Extract the claims.")
        assert not at_output_limit(engine, answer)
        for _ in range(2):
            answer = engine.complete("Extract the claims.")
            assert engine.last_report() == {"model": RESOLVED}
            assert not at_output_limit(engine, answer)

    def test_a_client_that_reports_nothing(self):
        class Plain:
            model_identifier = "fake-model"

            def complete(self, prompt, image_paths=()):
                return "answer"

        client = Plain()
        assert not at_output_limit(client, client.complete("Extract."))


class TestCutOffChunks:
    """A chunk answer the engine gives, through the LLM cache, as a run's
    extraction asks for it: whole when it ends with its sentinel and its
    model ended its turn. One cut off, at the output limit or before its
    sentinel, is rejected in the cache with a reason the truncations
    anomaly check counts (`chunk:`), and asked again (#7)."""

    def extractor(self, cli, tmp_path, *answers):
        run = RunDir(tmp_path / "run")
        cli.edit = answering((entity_manifest_json(), "end_turn"), *answers)
        client = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache"), run
        )
        return run, ChunkedClaimExtractor(client, budget=10_000, retries=1)

    @pytest.mark.parametrize(
        ("answer", "stop_reason", "reason"),
        [
            (WHOLE_CHUNK, "max_tokens", "chunk: cut off at max_tokens"),
            (CUT_CHUNK, "max_tokens", "chunk: cut off at max_tokens"),
            (CUT_CHUNK, "end_turn", "chunk: no terminal sentinel"),
        ],
        ids=["limit-with-sentinel", "limit-without", "no-sentinel"],
    )
    def test_a_cut_off_answer_is_rejected_and_asked_again(
        self, cli, tmp_path, answer, stop_reason, reason
    ):
        run, extractor = self.extractor(
            cli, tmp_path, (answer, stop_reason), (WHOLE_CHUNK, "end_turn")
        )
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []
        (rejected,) = [e for e in run.events() if e["event"] == "cache-reject"]
        assert rejected["reason"].startswith(reason)
        calls = [e for e in run.events() if e["event"] == ENGINE_CALL]
        assert [(e["call"], e["stop_reason"]) for e in calls] == [
            ("open", "end_turn"),
            ("fork", stop_reason),
            ("fork", "end_turn"),
        ]

    def test_a_chunk_cut_off_on_every_try_fails(self, cli, tmp_path):
        run, extractor = self.extractor(
            cli,
            tmp_path,
            (WHOLE_CHUNK, "max_tokens"),
            (WHOLE_CHUNK, "max_tokens"),
        )
        with pytest.raises(
            ExtractionFailed,
            match=r"1 of 1 chunk failed \(c001: ChunkIncomplete: cut off",
        ):
            extractor.extract(transcription(), DOC_REF)
        assert extractor.failure_reasons == {
            "c001": "ChunkIncomplete: cut off at max_tokens"
        }
        rejected = [
            e["reason"] for e in run.events() if e["event"] == "cache-reject"
        ]
        assert rejected == ["chunk: cut off at max_tokens"] * 2

    def test_a_kept_answer_cut_off_is_rejected_and_asked_once_more(
        self, cli, tmp_path, monkeypatch
    ):
        """A chunk answer the cache kept whose entry records a max_tokens
        stop is cut off when served again, as a fresh one is: it is
        rejected and asked once more, without spending a retry (#390)."""
        from yizkr.pipeline import chunking

        # a first run that took the answer the model cut off as whole
        with monkeypatch.context() as patched:
            patched.setattr(chunking, "at_output_limit", lambda *_: False)
            _, extractor = self.extractor(
                cli, tmp_path, (WHOLE_CHUNK, "max_tokens")
            )
            extractor.extract(transcription(), DOC_REF)
        cli.edit = answering((WHOLE_CHUNK, "end_turn"))
        run = RunDir(tmp_path / "again")
        client = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache"), run
        )
        extractor = ChunkedClaimExtractor(client, budget=10_000, retries=0)
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2 and extractor.failed_chunks == []
        (rejected,) = [e for e in run.events() if e["event"] == "cache-reject"]
        assert rejected["reason"] == "chunk: cut off at max_tokens"
        forks = [
            e
            for e in run.events()
            if e["event"] == ENGINE_CALL and e["call"] == "fork"
        ]
        assert [e["stop_reason"] for e in forks] == ["end_turn"]


# -- the Anthropic API engine -------------------------------------------------


# the bound on a wait that a synchronisation ends, never a time waited out
HANG_GUARD = 30


class TestApiCallEvents:
    """A call event on the Anthropic API engine carries the model that
    answered and why it stopped, as its response said (#441): the stop
    reason verbatim, max_tokens for an answer cut off at the output limit,
    which the run report counts and the LLM cache's callers reject, as on
    the claude-code engine."""

    def test_an_answered_call_carries_its_stop_reason(self, monkeypatch):
        seen, emit = recorder()
        engine = api_engine(
            monkeypatch,
            ("Sebastian Löbig, Hutmacher", "max_tokens"),
            ("Sebastian Löbig, Hutmacher, Flörburg", "end_turn"),
        )
        client = observed_client(engine, role="extract", events=emit)
        assert client.complete("Extract the claims.") == (
            "Sebastian Löbig, Hutmacher"
        )
        client.complete("Extract the claims.")
        assert [(e["outcome"], e["stop_reason"]) for e in seen] == [
            ("ok", "max_tokens"),
            ("ok", "end_turn"),
        ]

    def test_it_carries_the_model_and_nothing_else(self, monkeypatch):
        """The event takes the model the API says answered (a dated one,
        not the name asked for) and the stop reason, and nothing else of
        the engine's report, which is no stream's."""
        seen, emit = recorder()
        engine = api_engine(monkeypatch, ("an answer", "end_turn"))
        engine.complete = functools.partial(self._reported, engine)
        observed_client(engine, role="critique", events=emit).complete(
            "Critique the batch."
        )
        (event,) = seen
        assert set(llm.CALL_STREAM_FIELDS) & event.keys() == {
            "model",
            "stop_reason",
        }
        assert (event["model"], event["stop_reason"]) == (RESOLVED, "end_turn")

    @staticmethod
    def _reported(engine, prompt, image_paths=()):
        """The engine's answer, its report widened by a field that is no
        API response's, as a stream's report would have it."""
        answer = llm.AnthropicClient.complete(engine, prompt, image_paths)
        engine._reports.value = {
            **engine._reports.value,
            "cli_version": "2.1.237",
            "num_turns": 1,
        }
        return answer

    def test_the_model_calls_are_counted_by(self, monkeypatch):
        """The model the API says answered is the one the event names, so
        the run report counts the call under it."""
        seen, emit = recorder()
        engine = api_engine(monkeypatch, ("an answer", "end_turn"))
        observed_client(engine, role="extract", events=emit).complete(
            "Extract the claims."
        )
        assert engine.last_report() == {
            "model": RESOLVED,
            "stop_reason": "end_turn",
        }
        assert seen[0]["model"] == RESOLVED != MODEL

    @pytest.mark.parametrize(
        "resolved", ["claude sonnet five", "", None, 5], ids=repr
    )
    def test_a_model_in_no_plain_form_is_left_out(self, monkeypatch, resolved):
        seen, emit = recorder()
        engine = api_engine(
            monkeypatch, ("an answer", "end_turn"), resolved=resolved
        )
        observed_client(engine, role="extract", events=emit).complete(
            "Extract the claims."
        )
        assert engine.last_report() == {"stop_reason": "end_turn"}
        assert "model" not in seen[0]
        assert seen[0]["stop_reason"] == "end_turn"

    @pytest.mark.parametrize(
        "stop_reason",
        ["end_turn", "max_tokens", "refusal", "pause_turn", "stop_sequence"],
    )
    def test_every_stop_reason_is_carried_verbatim(
        self, monkeypatch, stop_reason
    ):
        seen, emit = recorder()
        engine = api_engine(monkeypatch, ("an answer", stop_reason))
        observed_client(engine, role="extract", events=emit).complete(
            "Extract the claims."
        )
        assert seen[0]["stop_reason"] == stop_reason

    @pytest.mark.parametrize("stop_reason", ["Max Tokens", None])
    def test_a_stop_reason_in_no_plain_form_is_left_out(
        self, monkeypatch, stop_reason
    ):
        seen, emit = recorder()
        engine = api_engine(monkeypatch, ("an answer", stop_reason))
        observed_client(engine, role="extract", events=emit).complete(
            "Extract the claims."
        )
        (event,) = seen
        assert event["outcome"] == "ok" and "stop_reason" not in event

    @pytest.mark.parametrize(
        "failure",
        [ConnectionError("the network is down"), ("  ", "max_tokens")],
        ids=["raised", "no-text"],
    )
    def test_a_failed_call_carries_no_earlier_calls_stop_reason(
        self, monkeypatch, failure
    ):
        """A request that gave no answer, after one cut off at the output
        limit: its event says nothing of why a model stopped, and the
        engine's report of it is empty."""
        seen, emit = recorder()
        engine = api_engine(monkeypatch, ("an answer", "max_tokens"), failure)
        client = observed_client(engine, role="extract", events=emit)
        client.complete("Extract chunk 1.")
        with pytest.raises((ConnectionError, RuntimeError)):
            client.complete("Extract chunk 2.")
        assert engine.last_report() == {}
        assert seen[0]["stop_reason"] == "max_tokens"
        assert seen[1]["outcome"] == "error"
        assert "stop_reason" not in seen[1]

    def test_a_failed_fork_carries_no_earlier_calls_stop_reason(
        self, monkeypatch
    ):
        """A fork that gave no answer, after one cut off at the output
        limit in the same conversation: its event says nothing of why a
        model stopped, and the engine's report of it is empty."""
        seen, emit = recorder()
        engine = api_engine(
            monkeypatch,
            ("the document", "end_turn"),
            ("chunk 1", "max_tokens"),
            ConnectionError("the network is down"),
        )
        client = observed_client(engine, role="extract", events=emit)
        _, handle = client.open_conversation("Read the document.")
        client.fork(handle, "Extract chunk 1.")
        with pytest.raises(ConnectionError):
            client.fork(handle, "Extract chunk 2.")
        assert engine.last_report() == {}
        assert [(e["call"], e.get("stop_reason")) for e in seen] == [
            ("open", "end_turn"),
            ("fork", "max_tokens"),
            ("fork", None),
        ]

    def test_a_call_that_fails_before_its_request_leaves_no_report(
        self, monkeypatch, tmp_path
    ):
        """A call whose image cannot be read sends no request, and the
        engine's report keeps nothing of the call before it."""
        engine = api_engine(monkeypatch, ("an answer", "max_tokens"))
        engine.complete("Transcribe the page.")
        with pytest.raises(FileNotFoundError):
            engine.complete("Transcribe the page.", [tmp_path / "gone.png"])
        assert engine.last_report() == {}

    def test_threads_sharing_the_engine_each_get_their_own_report(
        self, monkeypatch
    ):
        """Two threads ask one engine at once, and both have their answers
        before either's event is made: each event carries its own call's
        stop reason, and only the cut-off answer is judged cut off."""
        seen, emit = recorder()
        engine = api_engine(
            monkeypatch,
            by_prompt={
                "Extract chunk 1.": ("chunk 1", "max_tokens"),
                "Extract chunk 2.": ("chunk 2", "end_turn"),
            },
        )
        both_answered = threading.Barrier(2)

        def last_pacing():
            both_answered.wait(timeout=HANG_GUARD)
            return {}

        engine.last_pacing = last_pacing
        client = observed_client(engine, role="extract", events=emit)
        judged = {}

        def ask(prompt):
            answer = client.complete(prompt)
            judged[answer] = at_output_limit(client, answer)

        threads = [
            threading.Thread(target=ask, args=(f"Extract chunk {n}.",))
            for n in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=HANG_GUARD)
        assert sorted(e["stop_reason"] for e in seen) == [
            "end_turn",
            "max_tokens",
        ]
        assert judged == {"chunk 1": True, "chunk 2": False}

    def test_a_report_in_no_plain_form_gives_nothing(self):
        """An engine with no streams whose report gives a model or a stop
        reason in no plain form: the event takes neither, and so is still
        one the event log can hold."""

        class Untidy:
            model_identifier = "fake-model"

            def complete(self, prompt, image_paths=()):
                return "answer"

            def last_report(self):
                return {"model": object(), "stop_reason": "Max Tokens"}

        seen, emit = recorder()
        observed_client(Untidy(), role="extract", events=emit).complete(
            "Extract."
        )
        assert seen[0]["outcome"] == "ok"
        assert not set(llm.CALL_STREAM_FIELDS) & seen[0].keys()

    def test_a_streams_empty_report_is_never_filled_from_the_engines(self):
        """An engine with streams whose streams reported nothing of an
        answered call: its event carries nothing, whatever the engine's
        report of its last answered call says."""

        class Streamed:
            model_identifier = "fake-model"

            def complete(self, prompt, image_paths=()):
                return "answer"

            def last_stream(self):
                return {}

            def last_report(self):
                return {"model": RESOLVED, "stop_reason": "max_tokens"}

        seen, emit = recorder()
        observed_client(Streamed(), role="extract", events=emit).complete(
            "Extract."
        )
        assert seen[0]["outcome"] == "ok"
        assert not set(llm.CALL_STREAM_FIELDS) & seen[0].keys()

    def test_a_failed_call_of_a_client_whose_report_lags_carries_none(self):
        """A client that reports only its answered calls and keeps the
        last one's report when a call fails: the failed call's event takes
        nothing from it."""

        class Lagging:
            model_identifier = "fake-model"

            def __init__(self):
                self.report = {}

            def complete(self, prompt, image_paths=()):
                if prompt == "fail":
                    raise RuntimeError("no answer")
                self.report = {"stop_reason": "max_tokens"}
                return "answer"

            def last_report(self):
                return dict(self.report)

        seen, emit = recorder()
        client = observed_client(Lagging(), role="extract", events=emit)
        client.complete("Extract.")
        with pytest.raises(RuntimeError):
            client.complete("fail")
        assert seen[0]["stop_reason"] == "max_tokens"
        assert "stop_reason" not in seen[1]

    @pytest.mark.parametrize("raised", [UsageLimitReached, KeyboardInterrupt])
    def test_a_paused_or_interrupted_call_carries_none(self, raised):
        """A call that paused its pass or was interrupted answered nothing:
        its event takes nothing of a lagging report either."""

        class Lagging:
            model_identifier = "fake-model"

            def __init__(self):
                self.report = {}

            def complete(self, prompt, image_paths=()):
                if prompt == "stop":
                    raise raised()
                self.report = {"stop_reason": "max_tokens"}
                return "answer"

            def last_report(self):
                return dict(self.report)

        seen, emit = recorder()
        client = observed_client(Lagging(), role="extract", events=emit)
        client.complete("Extract.")
        with pytest.raises(raised):
            client.complete("stop")
        assert seen[0]["stop_reason"] == "max_tokens"
        assert "stop_reason" not in seen[1]

    def test_a_report_that_is_no_mapping_gives_nothing(self):
        """An engine whose report is no mapping adds nothing to the event,
        and the call still answers."""

        class Odd:
            model_identifier = "fake-model"

            def complete(self, prompt, image_paths=()):
                return "answer"

            def last_report(self):
                return ["max_tokens"]

        seen, emit = recorder()
        client = observed_client(Odd(), role="extract", events=emit)
        assert client.complete("Extract.") == "answer"
        assert seen[0]["outcome"] == "ok"
        assert "stop_reason" not in seen[0]

    def test_a_streams_report_comes_before_the_engines(self, cli):
        """From an engine with streams, the event is what they reported,
        never its report of its last answered call: a call that failed with
        its streams' report carries that, and one that never ran carries
        nothing, though the engine's report keeps an earlier call's."""
        seen, emit = recorder()
        engine = ClaudeCodeClient(model=MODEL)
        client = observed_client(engine, role="extract", events=emit)
        cli.result["stop_reason"] = "max_tokens"
        client.complete("Extract chunk 1.")
        cli.result = json.loads(MAX_TURNS)
        with pytest.raises(RuntimeError):
            client.complete("Extract chunk 2.")
        with pytest.raises(TypeError):
            client.complete("Extract chunk 3.", [object()])
        assert engine.last_report()["stop_reason"] == "max_tokens"
        assert seen[0]["stop_reason"] == "max_tokens"
        assert (seen[1]["subtype"], seen[1]["stop_reason"]) == (
            "error_max_turns",
            "tool_use",
        )
        assert not set(llm.CALL_STREAM_FIELDS) & seen[2].keys()

    def test_a_cut_off_answer_is_judged_so_through_the_wrappers(
        self, monkeypatch, tmp_path
    ):
        engine = api_engine(
            monkeypatch,
            ("an answer", "max_tokens"),
            ("an answer", "max_tokens"),
            ("an answer", "end_turn"),
        )
        observed = observed_client(
            engine, role="extract", events=recorder()[1]
        )
        assert at_output_limit(observed, observed.complete("Extract."))
        cached = caching(engine, open_cache(tmp_path / "cache"))
        answer = cached.complete("Extract chunk 2.")
        assert not answer.hit and at_output_limit(cached, answer)
        answer = cached.complete("Extract chunk 3.")
        assert not at_output_limit(cached, answer)

    def test_a_cut_off_chunk_is_rejected_kept_and_counted(
        self, monkeypatch, tmp_path
    ):
        """A chunk answer the API cut off at the output limit is rejected
        in the cache, its entry keeping the stop reason beside the answer,
        never in its key: the chunk asked again is keyed as it was. Its
        call event says max_tokens, as the run report counts it."""
        run = RunDir(tmp_path / "run")
        cache_root = tmp_path / "cache"
        engine = api_engine(
            monkeypatch,
            (entity_manifest_json(), "end_turn"),
            (WHOLE_CHUNK, "max_tokens"),
            (WHOLE_CHUNK, "end_turn"),
        )
        client = caching(engine, open_cache(cache_root), run)
        extractor = ChunkedClaimExtractor(client, budget=10_000, retries=1)
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2 and extractor.failed_chunks == []
        events = run.events()
        (rejected,) = [e for e in events if e["event"] == "cache-reject"]
        assert rejected["reason"] == "chunk: cut off at max_tokens"
        calls = [e for e in events if e["event"] == ENGINE_CALL]
        assert [(e["call"], e["stop_reason"]) for e in calls] == [
            ("open", "end_turn"),
            ("fork", "max_tokens"),
            ("fork", "end_turn"),
        ]
        misses = [
            e["key"]
            for e in events
            if e["event"] == "cache-miss" and e["call"] == "fork"
        ]
        assert misses == [rejected["key"]] * 2
        key = rejected["key"]
        (aside,) = open_cache(cache_root).rejected(key)
        assert json.loads(aside.read_text())["provenance"]["stop_reason"] == (
            "max_tokens"
        )
        assert entry_of(cache_root, key)["provenance"]["stop_reason"] == (
            "end_turn"
        )


# -- answers the CLI gives up on at the output limit --------------------------

# the notice CLI 2.1.237 prints when it gives up on an answer at the output
# token limit, as it printed it at a limit of 40 tokens
LIMIT_NOTICE = (
    "API Error: Claude's response exceeded the 40 output token maximum. To "
    "configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS "
    "environment variable."
)


def gave_up(events: list[dict]) -> list[dict]:
    """A call's events as CLI 2.1.237 prints them when it gives up on the
    answer at the output token limit, after its init event: four assistant
    events that hold thinking alone, their stop reason null; an assistant
    event whose `error` is max_output_tokens, whose one text block is the
    notice and whose stop reason is stop_sequence; and a result of subtype
    success that reports is_error, gives no API error status, holds the
    notice and has stop_sequence as its stop reason. No answer."""
    init = events[0]
    session = init["session_id"]
    thinking = [
        {
            "type": "assistant",
            "message": {
                "id": f"msg_01Flrb35Thinking{n}",
                "type": "message",
                "role": "assistant",
                "model": RESOLVED,
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "The letter names Hildegard Löbig.",
                        "signature": "c2lnbmF0dXJl",
                    }
                ],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 10},
            },
            "parent_tool_use_id": None,
            "session_id": session,
            "uuid": str(uuid.uuid4()),
        }
        for n in range(4)
    ]
    notice = {
        "type": "assistant",
        "message": {
            "id": str(uuid.uuid4()),
            "type": "message",
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": LIMIT_NOTICE}],
            "stop_reason": "stop_sequence",
            "stop_sequence": "",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
        "parent_tool_use_id": None,
        "session_id": session,
        "uuid": str(uuid.uuid4()),
        "error": "max_output_tokens",
    }
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": None,
        "duration_ms": 6120,
        "duration_api_ms": 5980,
        "num_turns": 1,
        "result": LIMIT_NOTICE,
        "stop_reason": "stop_sequence",
        "session_id": session,
        "total_cost_usd": 0.0031,
        "usage": usage(3, 40, 0, 11264),
        "permission_denials": [],
        "uuid": str(uuid.uuid4()),
    }
    return [init, *thinking, notice, result]


def reshaped(change):
    """gave_up's events, then `change` applied to them."""

    def edit(events):
        events = gave_up(events)
        change(events)
        return events

    return edit


class TestTheCliGivingUp:
    """CLI 2.1.237 hands back no answer its model stopped at the output
    token limit: its last assistant event is an API-error notice whose
    `error` is max_output_tokens, and its result reports is_error (the
    shape the pinned CLI printed at an output limit of 40). The call
    raises OutputLimitReached, read from that structure, never from the
    notice's text (#7)."""

    @pytest.mark.parametrize("status", [1, 0], ids=["exit-1", "exit-0"])
    def test_the_call_raises_output_limit_reached(self, cli, status):
        client = ClaudeCodeClient(model=MODEL)
        cli.edit = gave_up
        cli.exits = [status]
        with pytest.raises(
            OutputLimitReached, match="^cut off at max_tokens$"
        ) as raised:
            client.complete("Extract the claims.")
        assert isinstance(raised.value, RuntimeError)
        stream = client.last_stream()
        assert (
            stream["subtype"],
            stream["stop_reason"],
            stream["api_error"],
        ) == ("success", "stop_sequence", "max_output_tokens")
        assert client.last_report() == {}  # nothing was answered

    def test_the_notice_without_its_error_is_an_error_like_any_other(
        self, cli
    ):
        """The notice's text alone decides nothing."""
        cli.edit = reshaped(lambda events: events[-2].pop("error"))
        with pytest.raises(RuntimeError, match="not a subscription limit"):
            ClaudeCodeClient(model=MODEL).complete("Extract the claims.")

    def test_another_api_error_is_an_error_like_any_other(self, cli):
        cli.edit = reshaped(
            lambda events: events[-2].update(error="server_error")
        )
        with pytest.raises(RuntimeError) as raised:
            ClaudeCodeClient(model=MODEL).complete("Extract the claims.")
        assert not isinstance(raised.value, OutputLimitReached)

    def test_the_last_assistant_event_decides(self, cli):
        """A notice of the output limit before another error's is not the
        call's error."""

        def later(events):
            other = copy.deepcopy(events[-2]) | {"error": "server_error"}
            events.insert(-1, other)

        cli.edit = reshaped(later)
        with pytest.raises(RuntimeError) as raised:
            ClaudeCodeClient(model=MODEL).complete("Extract the claims.")
        assert not isinstance(raised.value, OutputLimitReached)

    @pytest.mark.parametrize("status", [1, 0], ids=["exit-1", "exit-0"])
    def test_a_stream_that_ends_after_the_notice_is_no_give_up(
        self, cli, status
    ):
        """The notice with no result after it (a crash): nothing says the
        CLI gave up, so the call is what its exit makes it, a failure, or
        at a zero exit an answer holding the notice, for its caller to
        judge. Its report still names the class of API error (#7)."""
        cli.edit = reshaped(lambda events: events.pop())
        cli.exits = [status]
        client = ClaudeCodeClient(model=MODEL)
        if status:
            with pytest.raises(RuntimeError) as raised:
                client.complete("Extract the claims.")
            assert not isinstance(raised.value, OutputLimitReached)
        else:
            assert client.complete("Extract the claims.") == LIMIT_NOTICE
        assert client.last_stream()["api_error"] == "max_output_tokens"

    def test_a_result_that_reports_no_error_answers(self, cli):
        """The notice beside a result that reports no error: the call
        answers with what its stream holds, for its caller to judge."""
        cli.edit = reshaped(lambda events: events[-1].update(is_error=False))
        client = ClaudeCodeClient(model=MODEL)
        assert client.complete("Extract the claims.") == LIMIT_NOTICE

    def test_an_api_errors_class_is_the_last_assistant_events(self):
        notice = json.loads(ASSISTANT) | {"error": "max_output_tokens"}
        report = report_of(lines(INIT, json.dumps(notice), RESULT))
        assert report["api_error"] == "max_output_tokens"
        # an assistant event after it that names none leaves none
        report = report_of(lines(INIT, json.dumps(notice), ASSISTANT, RESULT))
        assert "api_error" not in report
        # nor does a class in another form than a plain token
        odd = json.loads(ASSISTANT) | {"error": "Max Output Tokens"}
        assert "api_error" not in report_of(lines(INIT, json.dumps(odd)))

    def test_its_call_event_says_so(self, cli):
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="extract", events=emit
        )
        cli.edit = gave_up
        with pytest.raises(OutputLimitReached):
            client.complete("Extract the claims.")
        (event,) = seen
        assert {
            name: event.get(name)
            for name in ("outcome", "error", "api_error", "stop_reason")
        } == {
            "outcome": "error",
            "error": "OutputLimitReached",
            "api_error": "max_output_tokens",
            "stop_reason": "stop_sequence",
        }

    def test_the_cache_keeps_nothing_of_a_call_not_attempted(
        self, cli, tmp_path
    ):
        """A call not asked as an attempt (a chunk's, whose retries share
        one key) stores nothing: the next call asks again (#390)."""
        run = RunDir(tmp_path / "run")
        client = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache"), run
        )
        cli.edit = answering(gave_up, gave_up)
        for _ in range(2):
            with pytest.raises(OutputLimitReached):
                client.complete("Extract the claims.")
        assert not list((tmp_path / "cache").glob("*/*.json"))
        assert [
            (event["event"], event.get("reason"))
            for event in run.events()
            if event["event"] != ENGINE_CALL
        ] == [("cache-miss", "absent")] * 2

    def test_an_attempt_keeps_its_give_up(self, cli, tmp_path):
        """An attempt (llm.for_attempt) the CLI gave up on is kept under
        its key, with what its stream reported, the stop reason and the API
        error among them; the next run raises it again from the cache
        without running the CLI, and a refresh runs it again (#390)."""
        run = RunDir(tmp_path / "run")
        client = for_attempt(
            caching(
                ClaudeCodeClient(model=MODEL),
                open_cache(tmp_path / "cache"),
                run,
            ),
            1,
        )
        cli.edit = gave_up
        with pytest.raises(OutputLimitReached):
            client.complete("Extract the claims.")
        (path,) = (tmp_path / "cache").glob("*/*.json")
        entry = json.loads(path.read_text())
        assert entry["gave_up"] is True and entry["response"] == ""
        assert entry["components"]["attempt"] == 1
        assert call_key(entry["components"]) == path.stem
        provenance = entry["provenance"]
        assert (
            provenance["stop_reason"],
            provenance["api_error"],
            provenance["subtype"],
        ) == ("stop_sequence", "max_output_tokens", "success")
        cli.calls.clear()
        with pytest.raises(OutputLimitReached, match="cut off at max_tokens"):
            client.complete("Extract the claims.")
        assert cli.calls == []
        assert [
            (event["event"], event.get("reason"))
            for event in run.events()
            if event["event"] != ENGINE_CALL
        ] == [("cache-miss", "absent"), ("cache-hit", "key match")]
        refreshing = for_attempt(
            caching(
                ClaudeCodeClient(model=MODEL),
                open_cache(tmp_path / "cache", refresh=["extract"]),
            ),
            1,
        )
        cli.edit = None
        assert refreshing.complete("Extract the claims.") == "answer"
        assert len(cli.calls) == 1
        (aside,) = (tmp_path / "cache").glob("*/*.replaced-*.json")
        assert json.loads(aside.read_text())["gave_up"] is True

    def test_a_report_three_wrappers_down(self, cli, tmp_path):
        """at_output_limit reads the engine's report through wrappers that
        pass their calls on to a `client` of theirs."""

        class Wrapper:
            def __init__(self, client) -> None:
                self.client = client

        cli.result["stop_reason"] = "max_tokens"
        cached = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache")
        )
        answer = cached.complete("Extract chunk 1.")
        assert at_output_limit(Wrapper(Wrapper(cached)), answer)


class TestCallersOfAnAnswerTheCliGaveUpOn:
    """Each caller handles a call the CLI gave up on at the output limit as
    it handles an answer cut off: through the real client and the LLM
    cache, with CLI 2.1.237's streams written by hand (#7)."""

    def test_a_chunk_is_asked_again_and_counted_as_a_truncation(
        self, cli, tmp_path
    ):
        """A chunk the CLI gave up on three times, then answered: the
        extraction has its claims, no answer was rejected, since none came,
        and the anomaly checks count three truncations of four chunk
        answers, and neither a chunk failure nor a failed call."""
        run = RunDir(tmp_path / "run")
        subject = DOC_REF.sha1
        cli.edit = answering(
            (entity_manifest_json(), "end_turn"),
            gave_up,
            gave_up,
            gave_up,
            (WHOLE_CHUNK, "end_turn"),
        )
        client = caching(
            ClaudeCodeClient(model=MODEL),
            open_cache(tmp_path / "cache"),
            run,
            subject=subject,
            kind="text",
        )
        checks = AnomalyChecks(run, load_config())
        run.emit("started", subject=subject, kind="text", stage="extraction")
        extractor = ChunkedClaimExtractor(client, budget=10_000, retries=3)
        batch = extractor.extract(transcription(), DOC_REF)
        run.emit("done", subject=subject, kind="text", stage="extraction")
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []
        assert "cache-reject" not in names(run)
        calls = [e for e in run.events() if e["event"] == ENGINE_CALL]
        assert [
            (e["call"], e["outcome"], e.get("error"), e.get("api_error"))
            for e in calls
        ] == [
            ("open", "ok", None, None),
            *[("fork", "error", "OutputLimitReached", "max_output_tokens")]
            * 3,
            ("fork", "ok", None, None),
        ]
        checks.poll()
        raised = {alert["check"]: alert for alert in run.alerts()}
        alert = raised["truncations"]
        assert (alert["value"], alert["count"], alert["of"]) == (0.75, 3, 4)
        assert not {"chunk-failures", "call-failures"} & raised.keys()

    def test_a_chunk_the_cli_gave_up_on_every_time_fails_as_cut_off(
        self, cli, tmp_path
    ):
        cli.edit = answering(
            (entity_manifest_json(), "end_turn"), gave_up, gave_up
        )
        client = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache")
        )
        extractor = ChunkedClaimExtractor(client, budget=10_000, retries=1)
        with pytest.raises(
            ExtractionFailed,
            match=r"\(c001: ChunkIncomplete: cut off at max_tokens\)",
        ):
            extractor.extract(transcription(), DOC_REF)

    def test_the_critic_gives_no_findings_and_never_raises(
        self, cli, tmp_path, caplog
    ):
        run = RunDir(tmp_path / "run")
        client = caching(
            ClaudeCodeClient(model=MODEL),
            open_cache(tmp_path / "cache"),
            run,
            role="critique",
        )
        cli.edit = gave_up
        assert run_critique(client, critique_batch()) == []
        assert "critique cut off at max_tokens" in caplog.text
        assert not list((tmp_path / "cache").glob("*/*.json"))
        (call,) = [e for e in run.events() if e["event"] == ENGINE_CALL]
        assert (call["outcome"], call["api_error"]) == (
            "error",
            "max_output_tokens",
        )

    def test_a_single_call_extraction_fails_as_cut_off(self, cli, tmp_path):
        """No answer came, so there is no claim to keep."""
        client = caching(
            ClaudeCodeClient(model=MODEL), open_cache(tmp_path / "cache")
        )
        cli.edit = gave_up
        with pytest.raises(OutputLimitReached, match="cut off at max_tokens"):
            LlmClaimExtractor(client).extract(transcription(), DOC_REF)
        assert not list((tmp_path / "cache").glob("*/*.json"))

    def test_a_place_adjudication_raises_as_cut_off(self, cli):
        cli.edit = gave_up
        work = build_worklist(peiltal(), [link("Q40", PRISON, 0)])
        with pytest.raises(OutputLimitReached, match="cut off at max_tokens"):
            adjudicate(
                ClaudeCodeClient(model=MODEL), work.questions, peiltal()
            )
