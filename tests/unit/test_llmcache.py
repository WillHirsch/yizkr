"""Offline tests for the LLM cache (#225, ADR-0032).

Scripted engines stand in for the model. The first part exercises the
cache on its own: what a key holds, what is stored and how, rejection,
forks, the refresh switch, events and seeding. The second takes the real
pipeline through it: the orchestrator's client wiring, the chunked
extractor and the transcription check, run once against a scripted
engine and again from the cache, or without the cache at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

from tests.unit.test_orchestrate import (
    ScriptedOrchestrator,
    quiet,
)
from yizkr.pipeline import llm, llmcache, transcription_check
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.llm import (
    ConversationalLlmClient,
    PassPaused,
    RejectingLlmClient,
    cached_client,
    open_cache,
)
from yizkr.pipeline.llmcache import (
    COMPLETE,
    FORK,
    OPEN,
    CachedResponse,
    CachingClient,
    EngineCache,
    call_key,
    wrap,
)
from yizkr.pipeline.orchestrate import (
    BATCH_VOLATILE,
    FAILED,
    Orchestrator,
)
from yizkr.pipeline.rundir import RunDir
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT, Transcription


class Engine:
    """A scripted engine the cache can key: its configuration, the text
    each call sends and what a call reports, with answers from `answer`
    (kind, prompt, opening prompt of the conversation or None). Every
    call it answers is logged in `calls` as (kind, prompt)."""

    engine = "scripted"
    system_prompt = "Answer as a step of a data pipeline."

    def __init__(
        self,
        answer=None,
        *,
        model: str | None = "model-a",
        effort: str | None = "low",
        config: str = "config-1",
    ) -> None:
        self.answer = answer or (lambda kind, prompt, opening: prompt[::-1])
        self.model = model
        self.effort = effort
        self._config = config
        self.calls: list[tuple[str, str]] = []
        self.opened: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self._config.encode()).hexdigest()

    @property
    def model_identifier(self) -> str:
        return f"scripted/{self.model}"

    def call_text(self, prompt, image_paths=()) -> str:
        if not image_paths:
            return prompt
        listing = "\n".join(str(Path(p).resolve()) for p in image_paths)
        return f"Read these files:\n{listing}\n\n{prompt}"

    def last_report(self) -> dict:
        return {"model": f"{self.model}-20260901", "cli_version": "1.0.0"}

    def _log(self, kind: str, prompt: str) -> None:
        with self._lock:
            self.calls.append((kind, prompt))

    def complete(self, prompt, image_paths=()):
        self._log(COMPLETE, prompt)
        return self.answer(COMPLETE, prompt, None)

    def open_conversation(self, prompt):
        self._log(OPEN, prompt)
        with self._lock:
            session = f"session-{len(self.opened) + 1}"
            self.opened[session] = prompt
        return self.answer(OPEN, prompt, None), session

    def fork(self, handle, prompt):
        opening = self.opened[handle]  # a KeyError is a session never held
        self._log(FORK, prompt)
        return self.answer(FORK, prompt, opening)


class PlainEngine(Engine):
    """The scripted engine without conversations."""

    open_conversation = None  # type: ignore[assignment]
    fork = None  # type: ignore[assignment]


class Refusing(Engine):
    """The scripted engine's configuration with no engine behind it: any
    call fails the test, so a run of it must be answered from the
    cache."""

    def _log(self, kind: str, prompt: str) -> None:
        raise AssertionError(f"the engine was asked: {kind} {prompt[:60]!r}")


def kinds(engine: Engine) -> list[str]:
    return [kind for kind, _ in engine.calls]


def client(tmp_path, engine, *, role="extract", run=None, **cache_options):
    cache = EngineCache(tmp_path / "cache", **cache_options)
    return wrap(
        engine, cache, role=role, events=run.emit if run is not None else None
    )


def entry_files(tmp_path) -> list[Path]:
    return sorted((tmp_path / "cache").glob("*/*.json"))


def live_entries(tmp_path) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in entry_files(tmp_path)
        if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
    ]


def events(run: RunDir, *names: str) -> list[dict]:
    """The events of these names that this invocation of the run wrote."""
    return [
        event
        for event in run.events()
        if event["event"] in names and event["invocation"] == run.invocation
    ]


# -- keys -----------------------------------------------------------------


def key_of(tmp_path, *, engine=None, call=COMPLETE, prompt="Read it.",
           files=(), parent=(), epochs=None):  # fmt: skip
    caching = client(tmp_path, engine or Engine(), epochs=epochs)
    return call_key(caching.components(call, prompt, files, parent=parent))


class TestKeys:
    def test_every_component_changes_the_key(self, tmp_path):
        page = tmp_path / "page.png"
        page.write_bytes(b"page one")
        other = tmp_path / "other" / "page.png"
        other.parent.mkdir()
        other.write_bytes(b"page two")
        base = key_of(tmp_path, files=[page])
        system = Engine()
        system.system_prompt = "Answer tersely."
        variants = {
            "engine": key_of(
                tmp_path, engine=type("E", (Engine,), {"engine": "other"})()
            ),
            "model": key_of(tmp_path, engine=Engine(model="model-b")),
            "effort": key_of(tmp_path, engine=Engine(effort="high")),
            "config hash": key_of(tmp_path, engine=Engine(config="config-2")),
            "call kind": key_of(tmp_path, call=OPEN, files=[page]),
            "system prompt": key_of(tmp_path, engine=system, files=[page]),
            "prompt": key_of(tmp_path, prompt="Read it again.", files=[page]),
            "file content": key_of(tmp_path, files=[other]),
            "no file": key_of(tmp_path),
            "parent chain": key_of(
                tmp_path, call=FORK, files=[page], parent=["a" * 64]
            ),
            "other parent": key_of(
                tmp_path, call=FORK, files=[page], parent=["b" * 64]
            ),
            "epoch": key_of(tmp_path, files=[page], epochs={"extract": 1}),
        }
        assert base not in variants.values()
        assert len(set(variants.values())) == len(variants), variants

    def test_a_file_is_keyed_where_the_text_does_not_name_it(self, tmp_path):
        """The API engine sends images as blocks of their own, so its call
        text never names them: the files component alone keys them."""

        class ApiLike(Engine):
            def call_text(self, prompt, image_paths=()):
                return prompt

        page = tmp_path / "page.png"
        page.write_bytes(b"page one")
        caching = client(tmp_path, ApiLike())
        before = caching.complete_key("Read it.", [page])
        page.write_bytes(b"page two")
        assert caching.complete_key("Read it.", [page]) != before

    def test_the_components_are_what_the_call_sends(self, tmp_path):
        page = tmp_path / "page.png"
        page.write_bytes(b"page one")
        caching = client(tmp_path, Engine(), epochs={"extract": 3})
        components = caching.components(COMPLETE, "Read it.", [page])
        digest = hashlib.sha256(b"page one").hexdigest()
        sent = f"Read these files:\nsha256:{digest}\n\nRead it."
        assert components == {
            "engine": "scripted",
            "model": "model-a",
            "effort": "low",
            "config": Engine().config_hash,
            "call": COMPLETE,
            "system": hashlib.sha256(
                Engine.system_prompt.encode()
            ).hexdigest(),
            "prompt": hashlib.sha256(sent.encode()).hexdigest(),
            "files": [digest],
            "parent": [],
            "epoch": 3,
        }

    def test_keys_do_not_depend_on_where_a_file_lives(self, tmp_path):
        """The engine names each file by its absolute path; the key holds
        the file's content hash in its place, so a corpus, state directory
        or temporary file moved elsewhere keys the same."""
        first = tmp_path / "corpus" / "letter.png"
        second = tmp_path / "elsewhere" / "copy of letter.png"
        for path in (first, second):
            path.parent.mkdir()
            path.write_bytes(b"the same scan")
        engine = Engine()
        assert str(first.resolve()) in engine.call_text("Read it.", [first])
        caching = client(tmp_path, engine)
        assert caching.complete_key("Read it.", [first]) == (
            caching.complete_key("Read it.", [second])
        )
        second.write_bytes(b"another scan")
        assert caching.complete_key("Read it.", [first]) != (
            caching.complete_key("Read it.", [second])
        )

    def test_a_missing_file_cannot_be_keyed(self, tmp_path):
        caching = client(tmp_path, Engine())
        with pytest.raises(FileNotFoundError):
            caching.complete("Read it.", [tmp_path / "gone.png"])

    def test_an_engine_without_a_config_hash_is_refused(self, tmp_path):
        class Unkeyable:
            model_identifier = "unkeyable"

            def complete(self, prompt, image_paths=()):
                return prompt

        with pytest.raises(TypeError, match="config_hash"):
            client(tmp_path, Unkeyable())

    def test_the_real_engines_expose_what_a_key_needs(self, monkeypatch):
        """claude-code sends the listing of image paths ahead of the
        prompt and appends its system prompt; both are key components."""

        class Completed:
            stdout, stderr, returncode = "2.1.237\n", "", 0

        monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: Completed)
        monkeypatch.setattr(llm, "_PIN", llm._CliPin())
        engine = llm.ClaudeCodeClient(model="claude-sonnet-5", effort="low")
        caching = client(Path("/nonexistent"), engine)
        assert caching.components(COMPLETE, "x")["system"] == (
            hashlib.sha256(engine.SYSTEM_PROMPT.encode()).hexdigest()
        )
        assert engine.call_text("Transcribe.", [Path("/tmp/a.png")]) == (
            "First use the Read tool to examine these image files:\n"
            "/tmp/a.png\n\nTranscribe."
        )
        assert engine.system_prompt in engine._command()

    def test_the_api_engine_exposes_what_a_key_needs(
        self, tmp_path, monkeypatch
    ):
        """The anthropic engine sends the prompt as it is (images go as
        blocks of their own), adds no system prompt, keys its model and
        output budget, and reports the model that answered."""
        import sys
        import types

        class Messages:
            def create(self, **request):
                assert request["max_tokens"] == 4096
                block = types.SimpleNamespace(type="text", text="an answer")
                return types.SimpleNamespace(
                    model="claude-sonnet-5-20260901", content=[block]
                )

        package = types.ModuleType("anthropic")
        package.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
        monkeypatch.setitem(sys.modules, "anthropic", package)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test-key")
        engine = llm.AnthropicClient(model="claude-sonnet-5")
        assert engine.effective_config == {
            "engine": "anthropic",
            "model": "claude-sonnet-5",
            "max_tokens": 4096,
        }
        assert engine.call_text("Read it.", [tmp_path / "a.png"]) == "Read it."
        components = client(tmp_path, engine).components(COMPLETE, "Read it.")
        assert (components["engine"], components["model"]) == (
            "anthropic",
            "claude-sonnet-5",
        )
        assert components["effort"] is components["system"] is None
        assert engine.last_report() == {}
        assert engine.complete("Read it.") == "an answer"
        assert engine.last_report() == {"model": "claude-sonnet-5-20260901"}


# -- storage --------------------------------------------------------------


class TestStorage:
    def test_a_hit_calls_no_engine(self, tmp_path):
        engine = Engine()
        first = client(tmp_path, engine).complete("Read it.")
        again = client(tmp_path, Refusing()).complete("Read it.")
        assert again == first == "Read it."[::-1]
        assert isinstance(again, CachedResponse)
        assert again.key == first.key
        assert kinds(engine) == [COMPLETE]

    def test_an_entry_records_the_call_and_its_provenance(self, tmp_path):
        caching = client(tmp_path, Engine(), role="transcribe")
        response = caching.complete("Read it.")
        (entry,) = live_entries(tmp_path)
        assert entry["key"] == response.key
        assert call_key(entry["components"]) == response.key
        assert entry["response"] == "Read it."[::-1]
        assert entry["origin"] == "engine"
        provenance = entry["provenance"]
        assert provenance["role"] == "transcribe"
        assert provenance["engine"] == "scripted"
        assert provenance["model"] == "model-a-20260901"  # as reported
        assert provenance["cli_version"] == "1.0.0"
        assert provenance["config_hash"] == Engine().config_hash
        assert provenance["model_identifier"] == "scripted/model-a"
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT[\d:]{8}\+00:00", provenance["at"]
        )

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("engine down"), PassPaused("CLI updated mid-pass")],
        ids=["error", "pause"],
    )
    def test_only_an_answer_is_stored(self, tmp_path, failure):
        """An error or a pause stores nothing, and the pause still unwinds
        past `except Exception`, as the pipeline needs it to."""

        def answer(kind, prompt, opening):
            raise failure

        with pytest.raises(type(failure)):
            try:
                client(tmp_path, Engine(answer)).complete("Read it.")
            except Exception:
                if isinstance(failure, PassPaused):
                    pytest.fail("a pause was caught as an ordinary failure")
                raise
        assert entry_files(tmp_path) == []
        engine = Engine()
        client(tmp_path, engine).complete("Read it.")
        assert kinds(engine) == [COMPLETE]

    def test_parallel_calls_under_one_key_leave_one_whole_entry(
        self, tmp_path
    ):
        def slow(kind, prompt, opening):
            time.sleep(0.02)
            return f"answer from {threading.current_thread().name}"

        cache = EngineCache(tmp_path / "cache")
        answers = []

        def call():
            answers.append(wrap(Engine(slow), cache, role="r").complete("x"))

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(answers) == 8
        (path,) = entry_files(tmp_path)
        entry = json.loads(path.read_text())
        assert entry["response"] in answers
        assert not list((tmp_path / "cache").rglob(".*.tmp"))

    def test_debris_and_damage_are_misses(self, tmp_path):
        """A staging file an interrupted write left is never read as an
        entry, and a damaged entry is a miss that the answer replaces."""
        caching = client(tmp_path, Engine())
        key = caching.complete_key("Read it.")
        path = caching.cache.path(key)
        path.parent.mkdir(parents=True)
        (path.parent / f".{path.name}.0123.tmp").write_text("{")
        path.write_text('{"key": "')
        engine = Engine()
        client(tmp_path, engine).complete("Read it.")
        assert kinds(engine) == [COMPLETE]
        assert json.loads(path.read_text())["key"] == key

    def test_the_cache_is_private(self, tmp_path):
        client(tmp_path, Engine()).complete("Read it.")
        (entry,) = entry_files(tmp_path)
        for directory in (tmp_path / "cache", entry.parent):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_nothing_is_written_until_a_call_is_answered(self, tmp_path):
        open_cache(tmp_path / "cache")
        client(tmp_path, Engine())
        assert not (tmp_path / "cache").exists()


# -- rejection ------------------------------------------------------------


class TestRejection:
    def test_a_rejected_response_is_never_reused(self, tmp_path):
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, Engine(), run=run)
        response = caching.complete("Read it.")
        assert isinstance(caching, RejectingLlmClient)
        llm.reject_response(caching, response, "failed the check")
        engine = Engine(lambda kind, prompt, opening: "a second answer")
        again = client(tmp_path, engine, run=run).complete("Read it.")
        assert again == "a second answer"
        assert kinds(engine) == [COMPLETE]
        # the rejected entry is kept aside, with its reason, and no lookup
        # reads it
        (aside,) = caching.cache.rejected(response.key)
        kept = json.loads(aside.read_text())
        assert kept["response"] == "Read it."[::-1]
        assert kept["rejected"]["reason"] == "failed the check"
        assert kept["rejected"]["role"] == "extract"
        assert caching.cache.get(response.key)["response"] == "a second answer"
        (miss,) = [
            e for e in events(run, "cache-miss") if e["reason"] == "rejected"
        ]
        assert miss["key"] == response.key

    def test_rejection_holds_within_a_run(self, tmp_path):
        """A retry in the same run, with the same client, asks again."""
        answers = iter(["cut off", "whole"])
        engine = Engine(lambda kind, prompt, opening: next(answers))
        caching = client(tmp_path, engine)
        first = caching.complete("Read it.")
        caching.reject(first, "truncated")
        assert caching.complete("Read it.") == "whole"
        assert kinds(engine) == [COMPLETE, COMPLETE]

    def test_a_stale_rejection_leaves_a_newer_answer(self, tmp_path):
        """Two callers answered under one key: rejecting the answer that
        is no longer stored moves nothing."""
        first = client(tmp_path, Engine()).complete("Read it.")
        newer = client(
            tmp_path,
            Engine(lambda kind, prompt, opening: "newer"),
            refresh={"extract"},
        ).complete("Read it.")
        caching = client(tmp_path, Refusing())
        caching.reject(first, "failed the check")
        assert caching.complete("Read it.") == newer == "newer"
        assert caching.cache.rejected(first.key) == []

    def test_only_a_response_of_the_cache_can_be_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            client(tmp_path, Engine()).reject("plain text", "why")

    def test_an_engine_without_a_cache_has_nothing_to_reject(self):
        engine = Engine()
        assert not isinstance(engine, RejectingLlmClient)
        llm.reject_response(engine, "an answer", "why")  # nothing to do


# -- conversations and forks ---------------------------------------------


def conversation_answers(kind, prompt, opening):
    return f"{kind}:{prompt}" + (f" after {opening}" if opening else "")


class TestForks:
    def run_conversation(self, tmp_path, engine, forks, run=None):
        caching = client(tmp_path, engine, run=run)
        response, handle = caching.open_conversation("the document")
        return response, [caching.fork(handle, prompt) for prompt in forks]

    def test_the_wrapper_is_conversational_exactly_when_the_engine_is(
        self, tmp_path
    ):
        assert isinstance(client(tmp_path, Engine()), ConversationalLlmClient)
        plain = client(tmp_path, PlainEngine())
        assert not isinstance(plain, ConversationalLlmClient)
        assert type(plain) is CachingClient

    def test_a_fork_hit_needs_no_session(self, tmp_path):
        engine = Engine(conversation_answers)
        first = self.run_conversation(tmp_path, engine, ["c1", "c2"])
        assert kinds(engine) == [OPEN, FORK, FORK]
        again = self.run_conversation(tmp_path, Refusing(), ["c1", "c2"])
        assert again == first

    def test_a_fork_miss_replays_its_parent_once(self, tmp_path):
        """The cache answered the opening turn, so there is no session to
        fork: the turn is replayed once, for real, and the session serves
        every later fork of the conversation."""
        self.run_conversation(tmp_path, Engine(conversation_answers), ["c1"])
        engine = Engine(conversation_answers)
        run = RunDir(tmp_path / "run")
        _, forks = self.run_conversation(
            tmp_path, engine, ["c1", "c2", "c3"], run=run
        )
        assert kinds(engine) == [OPEN, FORK, FORK]
        assert forks[1:] == [
            "fork:c2 after the document",
            "fork:c3 after the document",
        ]
        (replay,) = events(run, "cache-replay")
        assert replay["matches"] is True
        assert replay["call"] == OPEN
        # the fork answered on the replayed session says so
        stored = {
            entry["response"]: entry["provenance"]
            for entry in live_entries(tmp_path)
        }
        assert stored["fork:c2 after the document"]["session"] == "replayed"
        assert stored["fork:c1 after the document"]["session"] == "original"

    def test_parallel_fork_misses_replay_once(self, tmp_path):
        self.run_conversation(tmp_path, Engine(conversation_answers), [])
        engine = Engine(conversation_answers)
        caching = client(tmp_path, engine)
        _, handle = caching.open_conversation("the document")
        threads = [
            threading.Thread(target=caching.fork, args=(handle, f"c{n}"))
            for n in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert kinds(engine).count(OPEN) == 1
        assert kinds(engine).count(FORK) == 6

    def test_a_replay_that_answers_differently_is_reported(self, tmp_path):
        self.run_conversation(tmp_path, Engine(conversation_answers), [])
        engine = Engine(lambda kind, prompt, opening: f"{kind} again")
        run = RunDir(tmp_path / "run")
        response, forks = self.run_conversation(
            tmp_path, engine, ["c1"], run=run
        )
        # the pipeline keeps the cached opening answer
        assert response == "open:the document"
        (replay,) = events(run, "cache-replay")
        assert replay["matches"] is False

    def test_a_live_conversation_needs_no_replay(self, tmp_path):
        engine = Engine(conversation_answers)
        run = RunDir(tmp_path / "run")
        self.run_conversation(tmp_path, engine, ["c1", "c2"], run=run)
        assert kinds(engine) == [OPEN, FORK, FORK]
        assert events(run, "cache-replay") == []

    def test_a_fork_takes_only_a_conversation_of_the_cache(self, tmp_path):
        with pytest.raises(TypeError):
            client(tmp_path, Engine()).fork("session-1", "c1")

    def test_closing_releases_the_engines_session(self, tmp_path):
        released = []
        engine = Engine(conversation_answers)
        engine.close_conversation = released.append
        caching = client(tmp_path, engine)
        _, handle = caching.open_conversation("the document")
        caching.close_conversation(handle)
        assert released == ["session-1"]
        # a conversation the cache answered holds no session to release
        _, cached = client(tmp_path, engine).open_conversation("the document")
        client(tmp_path, engine).close_conversation(cached)
        assert released == ["session-1"]


# -- switches -------------------------------------------------------------


class TestRefresh:
    def test_a_refreshed_role_asks_once_per_key_in_a_run(self, tmp_path):
        client(tmp_path, Engine()).complete("Read it.")
        engine = Engine(lambda kind, prompt, opening: "fresh")
        cache = EngineCache(tmp_path / "cache", refresh={"extract"})
        run = RunDir(tmp_path / "run")
        caching = wrap(engine, cache, role="extract", events=run.emit)
        assert caching.complete("Read it.") == "fresh"
        # once per key in the run: a second call is answered from the cache
        assert caching.complete("Read it.") == "fresh"
        assert wrap(Refusing(), cache, role="extract").complete(
            "Read it."
        ) == ("fresh")
        assert kinds(engine) == [COMPLETE]
        assert [e["reason"] for e in events(run, "cache-miss")] == ["refresh"]
        # the replaced answer is kept aside
        (replaced,) = (tmp_path / "cache").glob("*/*.replaced-*.json")
        assert json.loads(replaced.read_text())["response"] == "Read it."[::-1]

    def test_other_roles_are_still_served(self, tmp_path):
        client(tmp_path, Engine(), role="transcribe").complete("Read it.")
        cache = EngineCache(tmp_path / "cache", refresh={"extract"})
        assert (
            wrap(Refusing(), cache, role="transcribe").complete("Read it.")
            == ("Read it."[::-1])
        )

    def test_an_epoch_samples_a_role_afresh(self, tmp_path):
        first = client(tmp_path, Engine()).complete("Read it.")
        engine = Engine(lambda kind, prompt, opening: "resampled")
        again = client(tmp_path, engine, epochs={"extract": 1})
        assert again.complete("Read it.") == "resampled"
        # the answers of epoch 0 stay, and serve epoch 0
        assert client(tmp_path, Refusing()).complete("Read it.") == first


# -- events, seeding, warnings ------------------------------------------------


class TestEvents:
    def test_every_answer_and_rejection_is_an_event(self, tmp_path):
        run = RunDir(tmp_path / "run")
        cache = EngineCache(tmp_path / "cache")

        def emit(event, **fields):
            run.emit(event, subject="a" * 40, kind="media", **fields)

        caching = wrap(Engine(), cache, role="transcribe", events=emit)
        first = caching.complete("Read it.")
        caching.complete("Read it.")
        caching.reject(first, "failed the check")
        caching.complete("Read it.")
        seen = [
            (e["event"], e.get("reason"), e["role"], e["subject"])
            for e in run.events()
        ]
        subject = "a" * 40
        assert seen == [
            ("cache-miss", "absent", "transcribe", subject),
            ("cache-hit", "key match", "transcribe", subject),
            ("cache-reject", "failed the check", "transcribe", subject),
            ("cache-miss", "rejected", "transcribe", subject),
        ]
        assert {e["key"] for e in run.events()} == {first.key}
        assert {e["call"] for e in run.events() if "call" in e} == {COMPLETE}


class TestSeeding:
    PROVENANCE = {"engine": "archived tool", "made": "2024-05-01"}

    def test_a_seeded_answer_is_served_as_an_adoption(self, tmp_path):
        """An archived transcription stands in for the call it answers
        (#104): the hit says so, and the entry keeps the archive's own
        provenance."""
        run = RunDir(tmp_path / "run")
        scan = tmp_path / "scan.png"
        scan.write_bytes(b"a scan")
        caching = client(tmp_path, Refusing(), role="transcribe", run=run)
        key = caching.complete_key(TRANSCRIPTION_PROMPT, [scan])
        assert caching.seed(key, "archived text", self.PROVENANCE)
        assert caching.complete(TRANSCRIPTION_PROMPT, [scan]) == (
            "archived text"
        )
        (entry,) = live_entries(tmp_path)
        assert entry["origin"] == "adopted"
        assert entry["provenance"]["engine"] == "archived tool"
        assert "adopted_at" in entry["provenance"]
        assert [(e["event"], e["reason"]) for e in run.events()] == [
            ("cache-adopt", "adopted"),
            ("cache-hit", "adopted"),
        ]

    def test_seeding_never_replaces_an_answer(self, tmp_path):
        caching = client(tmp_path, Engine())
        answer = caching.complete("Read it.")
        assert not caching.cache.seed(answer.key, "archived", {})
        assert caching.cache.get(answer.key)["response"] == answer

    def test_a_seed_needs_a_key(self, tmp_path):
        with pytest.raises(ValueError):
            EngineCache(tmp_path).seed("not-a-key", "text", {})


class TestUnpinnedModel:
    def test_an_unpinned_role_is_named_once(self, tmp_path, caplog):
        cache = EngineCache(tmp_path / "cache")
        with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
            for _ in range(2):
                wrap(Engine(model=None), cache, role="transcribe")
            wrap(Engine(), cache, role="extract")
        (record,) = caplog.records
        assert "transcribe role" in record.getMessage()
        assert "$YIZKR_LLM_MODEL" in record.getMessage()


class TestFactory:
    def test_the_factory_wraps_only_when_asked(self, tmp_path):
        engine = Engine()
        cache = open_cache(tmp_path / "cache", refresh=["critique"])
        wrapped = cached_client(engine, cache, role="critique")
        assert wrapped.client is engine
        assert wrapped.cache.refresh == {"critique"}
        assert isinstance(wrapped, RejectingLlmClient)


# -- the pipeline through the cache -----------------------------------------

LETTER = "Ursula was born in 1911."


def person(prompt: str) -> tuple[str, int]:
    """The first word and year of a prompt's first numbered line."""
    match = re.search(r"^1: (.*)$", prompt, re.M)
    line = match.group(1) if match else ""
    words = re.findall(r"[A-Za-z]+", line)
    year = re.search(r"\b(1[5-9]\d\d)\b", line)
    return (words[0] if words else "Someone"), (
        int(year.group(1)) if year else 1911
    )


def pipeline_answers(kind, prompt, opening):
    """A scripted model for the whole pipeline: it reads a scan as the
    letter, answers the entity manifest and the chunks from the document
    it is shown, and the critic with no findings."""
    if prompt.startswith(TRANSCRIPTION_PROMPT):
        return LETTER
    if "manifest of EVERY distinct entity" in prompt:
        name, _ = person(prompt)
        entity = {"id": "e1", "label": name, "kind": "human"}
        return json.dumps({"entities": [entity | {"mention_lines": [1]}]})
    if prompt.startswith("You review a genealogical extraction"):
        return '{"done": true, "findings_emitted": 0}'
    name, year = person(opening or prompt)
    artifact = re.search(r"artifact QID (Q\d+)", prompt).group(1)
    claim = {
        "subject": {"kind": "local", "local_id": "e1", "label": name},
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": {"artifact": {"kind": "qid", "qid": artifact}},
    }
    return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


class CachedRun(ScriptedOrchestrator):
    """The scripted orchestrator with the real client wiring: its engine
    is a scripted model that the client factory wraps with the LLM cache
    (or not, without a cache directory)."""

    _llm = Orchestrator._llm

    def __init__(self, *args, engine, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = engine

    def _engine(self, role):
        return self.engine


def folder_of_three(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    (folder / "a-scan.png").write_bytes(b"not really a png")
    (folder / "b-letter.txt").write_text("Klara was born in 1917.")
    (folder / "c-note.txt").write_text("Gustav was born in 1919.")
    return folder


def outputs(state_dir: Path) -> dict:
    """What a run leaves in each document's views, run timestamps and
    engine builds aside."""
    found = {}
    for directory in sorted(p for p in state_dir.iterdir() if p.is_dir()):
        if directory.name.startswith("."):
            continue
        batch = json.loads((directory / "batch.json").read_text())
        found[directory.name] = {
            "transcription": json.loads(
                (directory / "transcription.json").read_text()
            )["text"],
            "batch": content_hash(batch, volatile=BATCH_VOLATILE),
            "critique": (directory / "critique.json").read_text(),
            "coverage": (
                directory / "extraction" / "coverage.json"
            ).read_text(),
        }
    return found


class TestThePipeline:
    def test_no_cache_gives_the_same_outputs_as_a_cached_replay(
        self, tmp_path
    ):
        """The same folder three ways: through the cache with the engine
        answering, from the cache with an engine that must not be called,
        and with no cache at all."""
        folder = folder_of_three(tmp_path)
        cache = tmp_path / "cache"
        runs = {
            "engine": (Engine(pipeline_answers), cache),
            "replay": (Refusing(pipeline_answers), cache),
            "no-cache": (Engine(pipeline_answers), None),
        }
        found = {}
        for name, (engine, cache_dir) in runs.items():
            state = tmp_path / name
            orchestrator = CachedRun(
                folder, state, engine=engine, cache_dir=cache_dir, log=quiet
            )
            assert orchestrator.run() == 2
            found[name] = outputs(state)
            if name == "replay":
                names = {e["event"] for e in orchestrator.rundir.events()}
                assert "cache-miss" not in names
                assert "cache-hit" in names
            if name == "no-cache":
                assert not any(
                    e["event"].startswith("cache-")
                    for e in orchestrator.rundir.events()
                )
        assert len(found["engine"]) == 3
        assert found["replay"] == found["engine"] == found["no-cache"]
        assert found["engine"]["a-scan"]["transcription"] == LETTER
        # the engine took part in both live runs, the same calls each time
        engine, uncached = runs["engine"][0], runs["no-cache"][0]
        assert sorted(engine.calls) == sorted(uncached.calls)

    def test_no_cache_reads_nothing_from_the_cache(self, tmp_path):
        folder = folder_of_three(tmp_path)
        first = CachedRun(
            folder,
            tmp_path / "first",
            engine=Engine(pipeline_answers),
            cache_dir=tmp_path / "cache",
            log=quiet,
        )
        first.run()
        engine = Engine(pipeline_answers)
        CachedRun(folder, tmp_path / "second", engine=engine, log=quiet).run()
        assert sorted(engine.calls) == sorted(first.engine.calls)

    def test_abort_and_resume_lose_no_completed_call(self, tmp_path):
        """A run interrupted mid-extraction keeps every call that
        completed; the next run asks the engine only for the rest, and for
        a session to fork from."""
        folder = folder_of_three(tmp_path)
        answered = []

        def interrupted(kind, prompt, opening):
            if len(answered) == 5:  # the second document's first chunk
                raise KeyboardInterrupt
            answered.append((kind, prompt))
            return pipeline_answers(kind, prompt, opening)

        state = tmp_path / "state"
        cache = tmp_path / "cache"
        first = CachedRun(
            folder,
            state,
            engine=Engine(interrupted),
            cache_dir=cache,
            log=quiet,
            chunk_concurrency=1,
        )
        with pytest.raises(KeyboardInterrupt):
            first.run()
        assert first.rundir.events()[-1]["event"] == "run-interrupted"
        completed = {entry["key"] for entry in live_entries(tmp_path)}
        assert len(completed) == len(answered) == 5
        engine = Engine(pipeline_answers)
        again = CachedRun(
            folder, state, engine=engine, cache_dir=cache, log=quiet
        )
        assert again.run() == 2
        # every call that completed is answered from the cache, and none is
        # asked of the engine again
        hits = {e["key"] for e in events(again.rundir, "cache-hit")}
        misses = events(again.rundir, "cache-miss")
        assert hits == completed
        assert not {e["key"] for e in misses} & completed
        # the engine answered the calls not yet made, and replayed the
        # interrupted document's opening turn once, for a session to fork
        (replay,) = events(again.rundir, "cache-replay")
        assert replay["key"] in completed
        assert len(engine.calls) == len(misses) + 1
        assert [call for call in engine.calls if call in answered] == [
            answered[-1]
        ]

    def test_a_stricter_check_applies_to_cached_transcriptions(
        self, tmp_path, monkeypatch
    ):
        """The check runs on every transcription in every run, from the
        engine or from the cache: a cached transcription a stricter check
        fails is rejected in the cache, and the next run asks again."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "scan.png").write_bytes(b"not really a png")
        cache = tmp_path / "cache"
        state = tmp_path / "state"

        def run(engine):
            orchestrator = CachedRun(
                folder, state, engine=engine, cache_dir=cache, log=quiet
            )
            orchestrator.run()
            (doc,) = orchestrator.documents()
            return orchestrator, doc

        _, doc = run(Engine(pipeline_answers))
        assert doc.state.status != FAILED
        stricter = transcription_check._signature(
            "an-invented-rule", transcription_check.REFUSAL, r"\bUrsula\b"
        )
        monkeypatch.setattr(
            transcription_check,
            "SIGNATURES",
            (*transcription_check.SIGNATURES, stricter),
        )
        replay, doc = run(Refusing(pipeline_answers))
        assert doc.state.status == FAILED
        assert "an-invented-rule" in doc.state.error
        (reject,) = events(replay.rundir, "cache-reject")
        assert reject["role"] == "transcribe"
        assert "an-invented-rule" in reject["reason"]
        # the next run asks the engine again, and its new answer passes
        engine = Engine(
            lambda kind, prompt, opening: (
                "Klara was born in 1917."
                if prompt.startswith(TRANSCRIPTION_PROMPT)
                else pipeline_answers(kind, prompt, opening)
            )
        )
        again, doc = run(engine)
        assert doc.state.status != FAILED
        (miss,) = [
            e
            for e in events(again.rundir, "cache-miss")
            if e["role"] == "transcribe"
        ]
        assert miss["reason"] == "rejected"
        assert json.loads(doc.transcription.read_text())["text"] == (
            "Klara was born in 1917."
        )


# -- the chunked extractor through the cache ---------------------------------

DOC = "1: first line\n2: second line"


def extractor_answers(truncate: set[str] = frozenset()):
    """The scripted model for a two-entity document; chunks naming an
    entity in `truncate` come back without their sentinel."""
    manifest = {
        "entities": [
            {"id": "e1", "label": "Klara Löbig", "kind": "human",
             "mention_lines": [1]},
            {"id": "e2", "label": "Gustav Löbig", "kind": "human",
             "mention_lines": [2]},
        ]
    }  # fmt: skip

    def answer(kind, prompt, opening):
        if "manifest of EVERY distinct entity" in prompt:
            return json.dumps(manifest)
        entity = "e1" if "- e1:" in prompt else "e2"
        claim = {
            "subject": {"kind": "local", "local_id": entity, "label": entity},
            "property": "date_of_birth",
            "value": {
                "type": "time",
                "year": 1917 if entity == "e1" else 1919,
            },
            "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
        }
        if entity in truncate:
            return json.dumps(claim)
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'

    return answer


def extract(tmp_path, engine, **options):
    caching = client(tmp_path, engine, **options)
    extractor = ChunkedClaimExtractor(caching, budget=10, retries=1)
    transcription = Transcription(
        text="Klara Löbig b. 1917\nGustav Löbig b. 1919",
        engine="test",
        artifact_qid="Q23",
    )
    return extractor, extractor.extract(transcription, "Q23")


class TestTheExtractor:
    def test_a_truncated_chunk_is_rejected_and_asked_again(self, tmp_path):
        attempts = []

        def answer(kind, prompt, opening):
            text = extractor_answers()(kind, prompt, opening)
            if kind == FORK and "- e1:" in prompt:
                attempts.append(prompt)
                if len(attempts) == 1:
                    return text.splitlines()[0]  # cut off
            return text

        extractor, batch = extract(tmp_path, Engine(answer))
        assert extractor.failed_chunks == []
        assert len(batch.claims) == 2
        # the cut-off answer is kept aside, rejected; the whole one serves
        (aside,) = (tmp_path / "cache").glob("*/*.rejected-*.json")
        assert (
            "sentinel" in json.loads(aside.read_text())["rejected"]["reason"]
        )
        _, again = extract(tmp_path, Refusing(answer))
        assert again.claims == batch.claims

    def test_a_failed_chunk_is_retried_in_the_next_run_alone(self, tmp_path):
        extractor, _ = extract(tmp_path, Engine(extractor_answers({"e1"})))
        assert extractor.failed_chunks == ["c001"]
        engine = Engine(extractor_answers())
        extractor, batch = extract(tmp_path, engine)
        assert extractor.failed_chunks == []
        assert len(batch.claims) == 2
        # a session for the fork, and the chunk that failed; nothing else
        assert kinds(engine) == [OPEN, FORK]
        assert "- e1:" in engine.calls[1][1]

    def test_a_fix_after_the_call_costs_no_model_call(
        self, tmp_path, monkeypatch
    ):
        """Parsing, the merge and validation run on every run, so a change
        to them applies to cached answers without asking the engine."""
        from yizkr.pipeline import chunking

        _, before = extract(tmp_path, Engine(extractor_answers()))
        normalize = chunking.normalize_to_manifest

        def fixed(claim, records):
            claim = normalize(claim, records)
            claim["confidence"] = 0.5
            return claim

        monkeypatch.setattr(chunking, "normalize_to_manifest", fixed)
        _, after = extract(tmp_path, Refusing(extractor_answers()))
        assert {c.confidence for c in before.claims} == {None}
        assert {c.confidence for c in after.claims} == {0.5}

    def test_a_rejected_manifest_is_asked_again(self, tmp_path):
        answers = iter(["no manifest here"])

        def answer(kind, prompt, opening):
            if kind == OPEN:
                return next(answers, None) or extractor_answers()(
                    kind, prompt, opening
                )
            return extractor_answers()(kind, prompt, opening)

        engine = Engine(answer)
        extractor, batch = extract(tmp_path, engine)
        assert not extractor.used_fallback_manifest
        assert kinds(engine)[:2] == [OPEN, OPEN]
        _, again = extract(tmp_path, Refusing(extractor_answers()))
        assert again.claims == batch.claims


def test_the_cache_root_holds_only_entries(tmp_path):
    """Every file under the root is an entry or an entry set aside."""
    caching = client(tmp_path, Engine())
    response = caching.complete("Read it.")
    caching.reject(response, "why")
    caching.complete("Read it.")
    names = sorted(path.name for path in (tmp_path / "cache").rglob("*"))
    shard = response.key[:2]
    assert names[0] == shard
    assert all(
        re.fullmatch(rf"{response.key}(\.rejected-[\w-]+)?\.json", name)
        for name in names[1:]
    ), names
    assert os.listdir(tmp_path / "cache") == [shard]
