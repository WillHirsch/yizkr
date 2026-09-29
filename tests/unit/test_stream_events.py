"""Offline tests for what a claude-code call's stream reports of the call
(#98, ADR-0032): the model it ran on, the CLI version, the session, the
tokens it used, its turns and how it ended, parsed from the stream's init
and result events; kept beside the answer in the call's LLM cache entry,
never in its key; carried, but for the session, by the call's event; and
totalled per stage and role from the run's events for the run report.

The streams are written by hand in the shape CLI 2.1.237 prints them, and
the fake claude CLI of test_engine_limits prints them for the real
engine's client. The claude CLI itself is never run. Every name, text and
number is invented (docs/EXAMPLES.md).
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from tests.unit.test_claude_code_pinning import Completed, install_fake_cli
from tests.unit.test_engine_limits import (
    HOUR,
    RATE_LIMITED,
    START,
    Clock,
    LimitedCli,
    recorder,
    usage_window,
)
from yizkr.pipeline import anomalies, llm
from yizkr.pipeline.anomalies import AnomalyChecks, load_config
from yizkr.pipeline.llm import (
    COMPLETE,
    ENGINE_CALL,
    MAX_WAIT_ENV,
    RESERVE_ENV,
    USAGE_TOKENS,
    ClaudeCodeClient,
    ConversationChanged,
    UsageLimitReached,
    cached_client,
    observed_client,
    open_cache,
    stream_report,
)
from yizkr.pipeline.llmcache import call_key
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
    '"result":"' + ANSWER + '",'
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
# a call that ran out of turns: an error result, with no answer
MAX_TURNS = (
    '{"type":"result","subtype":"error_max_turns","is_error":true,'
    '"duration_ms":90210,"duration_api_ms":88004,"num_turns":30,'
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
        earlier = json.loads(RESULT) | {"num_turns": 1, "subtype": "other"}
        report = report_of(
            lines(INIT, json.dumps(later_init), json.dumps(earlier), RESULT)
        )
        assert (report["model"], report["num_turns"]) == (RESOLVED, 2)
        assert report["subtype"] == "success"

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
    last, and `cut_after` ends a call in a timeout after that many of its
    events, as the process killed mid-stream prints them."""

    def __init__(self, projects: Path, other) -> None:
        super().__init__(projects, other)
        self.model = RESOLVED
        self.result: dict = {
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "usage": usage(3, 24, 2048, 11264),
        }
        self.refused: dict = {"usage": usage(5)}
        self.edit = None
        self.cut_after: int | None = None

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
        if self.cut_after is not None:
            partial = "\n".join(
                json.dumps(event) for event in events[: self.cut_after]
            )
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output=partial.encode()
            )
        return Completed(stdout=text, returncode=completed.returncode)


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

    def test_a_call_cut_off_by_its_timeout(self, cli):
        """The init event came, the result never did."""
        client = ClaudeCodeClient(model=MODEL)
        cli.cut_after = 1
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

    def test_a_call_that_never_ran_reports_nothing_of_an_earlier_one(
        self, cli
    ):
        client = ClaudeCodeClient(model=MODEL)
        client.complete("Read the letter.")
        assert client.last_stream()
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
        assert event["subtype"] == "success"
        session = engine.last_report()["session_id"]
        assert "session_id" not in event
        assert session not in json.dumps(event)
        assert "replay" not in event

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
        seen, emit = recorder()
        client = observed_client(
            ClaudeCodeClient(model=MODEL), role="critique", events=emit
        )
        client.complete("Critique the batch.")
        cli.edit = lambda events: [
            e for e in events if e.get("type") == "assistant"
        ]
        client.complete("Critique the batch again.")
        assert "usage" in seen[0]
        assert not set(llm.CALL_STREAM_FIELDS) & seen[1].keys()

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
# GOLDEN_TMP, as the cache keyed it before calls' stream reports were kept
GOLDEN_TMP = "/yizkr-fixed-tmp"
GOLDEN_PROMPT = "Critique the batch of Sebastian Löbig's marriage certificate."
GOLDEN_KEY = "b8464120f73b455ba9cacf5b93e5c54fba755e044833501e28bcc70844402ff3"


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
        assert {name: provenance[name] for name in REPORT} == {
            **REPORT,
            "session_id": provenance["session_id"],
        }
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
        """Another model, session, usage or ending of the same call keys
        it the same: the second is answered from the cache."""
        cache = open_cache(tmp_path / "cache")
        engine = ClaudeCodeClient(model=MODEL)
        first = caching(engine, cache).complete("Read the letter.")
        cli.model = "claude-sonnet-5-20261101"
        cli.result = {"num_turns": 7, "usage": usage(9, 9, 9, 9)}
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
