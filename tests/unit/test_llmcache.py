"""Offline tests for the LLM cache (#225, ADR-0032).

Scripted engines stand in for the model. The first part exercises the
cache on its own: what a key holds, what is stored and how, the lock that
keeps a cache to one process (#236), rejection, forks, the refresh switch,
events and seeding. The second takes the real pipeline through it: the
orchestrator's client wiring, the chunked extractor and the transcription
check, run once against a scripted engine and again from the cache, or
without the cache at all.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import logging
import os
import pickle
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.unit.test_orchestrate import (
    ScriptedOrchestrator,
    approve_all_claims,
    approve_all_equivalences,
    quiet,
    this_run,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline import llm, llmcache, transcription_check
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.extract import LlmClaimExtractor
from yizkr.pipeline.llm import (
    ConversationalLlmClient,
    ConversationChanged,
    PassPaused,
    RejectingLlmClient,
    cached_client,
    for_attempt,
    open_cache,
)
from yizkr.pipeline.llmcache import (
    COMPLETE,
    FORK,
    LOCK_FILE,
    OPEN,
    CachedResponse,
    CacheInUse,
    CachingClient,
    EngineCache,
    call_key,
    wrap,
)
from yizkr.pipeline.orchestrate import (
    AWAITING_EQUIVALENCES,
    BATCH_VOLATILE,
    FAILED,
    WRITTEN,
    Orchestrator,
)
from yizkr.pipeline.rundir import RunDir, run_state
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


class ResumableEngine(Engine):
    """The scripted engine with sessions it can keep and resume exactly
    (llm.ResumableLlmClient): a session's one file holds the conversation's
    opening prompt, and a resumed session answers forks as the session
    opened with it does."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resumed: list[dict[str, bytes]] = []

    def keep_conversation(self, handle):
        return {f"sessions/{handle}.jsonl": self.opened[handle].encode()}

    def resume_conversation(self, prompt, response, files):
        self.resumed.append(dict(files))
        (name,) = files  # nothing kept, or too much, cannot be resumed
        with self._lock:
            session = f"resumed-{len(self.resumed)}"
            self.opened[session] = files[name].decode()
        return session


class AnswerKeepingEngine(Engine):
    """The scripted engine with sessions it can keep and resume, as a real
    engine's are: each opening turn answers afresh (`opening answer #n`,
    counting from `first`), a session's one kept file holds the opening
    answer, as a transcript does, and a fork answers from the answer its
    session holds. So a fork made on another opening answer than the one
    its key names shows in its text. `keeps=False` keeps nothing, as for
    a session no longer stored."""

    def __init__(self, *args, first: int = 1, keeps: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.count = first - 1
        self.keeps = keeps
        self.answers: dict[str, str] = {}
        self.resumed: list[dict[str, bytes]] = []

    def open_conversation(self, prompt):
        self._log(OPEN, prompt)
        with self._lock:
            self.count += 1
            session = f"session-{self.count}"
            self.opened[session] = prompt
            self.answers[session] = f"opening answer #{self.count}"
        return self.answers[session], session

    def fork(self, handle, prompt):
        answer = self.answers[handle]  # a KeyError is a session never held
        self._log(FORK, prompt)
        return f"fork {prompt} made on [{answer}]"

    def keep_conversation(self, handle):
        if not self.keeps:
            return {}
        return {f"sessions/{handle}.jsonl": self.answers[handle].encode()}

    def resume_conversation(self, prompt, response, files):
        self.resumed.append(dict(files))
        (name,) = files  # nothing kept, or too much, cannot be resumed
        with self._lock:
            session = f"resumed-{len(self.resumed)}"
            self.answers[session] = files[name].decode()
        return session


def made_on(fork: str) -> str:
    """The opening answer an AnswerKeepingEngine fork was made on."""
    return re.search(r"made on \[(.*)\]$", fork).group(1)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
           files=(), parent=(), opening=None, attempt=0,
           epochs=None):  # fmt: skip
    caching = client(tmp_path, engine or Engine(), epochs=epochs)
    caching = for_attempt(caching, attempt)
    return call_key(
        caching.components(call, prompt, files, parent=parent, opening=opening)
    )


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
            "opening answer": key_of(
                tmp_path,
                call=FORK,
                files=[page],
                parent=["a" * 64],
                opening="c" * 64,
            ),
            "attempt": key_of(tmp_path, files=[page], attempt=1),
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
            "opening": None,
            "attempt": 0,
            "epoch": 3,
        }

    def test_an_attempt_is_a_client_of_its_own(self, tmp_path):
        """A caller that asks again after an answer it cannot use, and then
        falls back, asks each attempt through llm.for_attempt: the cache
        keys, and keeps, each attempt apart (#225)."""
        caching = client(tmp_path, Engine())
        second = for_attempt(caching, 1)
        assert for_attempt(caching, 0) is caching
        assert second is not caching and second.cache is caching.cache
        assert second.complete_key("Read it.") != (
            caching.complete_key("Read it.")
        )
        assert caching.components(COMPLETE, "Read it.")["attempt"] == 0
        engine = Engine()
        assert for_attempt(engine, 1) is engine  # nothing keeps its answers

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

    def test_the_real_engines_expose_what_a_key_needs(
        self, tmp_path, monkeypatch
    ):
        """claude-code sends the listing of image paths ahead of the
        prompt and appends its system prompt; both are key components."""

        class Completed:
            stdout, stderr, returncode = "2.1.237\n", "", 0

        monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: Completed)
        monkeypatch.setattr(llm, "_PIN", llm._CliPin())
        engine = llm.ClaudeCodeClient(model="claude-sonnet-5", effort="low")
        caching = client(tmp_path, engine)
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

    @pytest.mark.parametrize("answer", ["", " \n\t"], ids=["empty", "blank"])
    def test_an_empty_answer_is_never_stored(self, tmp_path, answer):
        """An answer with no text in it is returned to the caller, whose
        checks see it, and never kept (#225)."""
        engine = Engine(lambda kind, prompt, opening: answer)
        assert client(tmp_path, engine).complete("Read it.") == answer
        assert entry_files(tmp_path) == []
        client(tmp_path, engine).complete("Read it.")
        assert kinds(engine) == [COMPLETE, COMPLETE]

    @pytest.mark.parametrize("blocks", [[], ["  \n"]], ids=["none", "blank"])
    def test_the_api_engine_refuses_an_answer_without_text(
        self, monkeypatch, blocks
    ):
        import sys
        import types

        class Messages:
            def create(self, **request):
                content = [
                    types.SimpleNamespace(type="text", text=text)
                    for text in blocks
                ]
                return types.SimpleNamespace(model="m", content=content)

        package = types.ModuleType("anthropic")
        package.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
        monkeypatch.setitem(sys.modules, "anthropic", package)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test-key")
        engine = llm.AnthropicClient(model="claude-sonnet-5")
        with pytest.raises(RuntimeError, match="no text"):
            engine.complete("Read it.")
        assert engine.last_report() == {}

    def test_an_entry_is_served_only_under_its_own_key(self, tmp_path):
        """An entry copied, or moved, under another key's name is a miss,
        and the answer for that key displaces it."""
        caching = client(tmp_path, Engine())
        first = caching.complete("Read it.")
        other = caching.complete_key("Read it again.")
        path = caching.cache.path(other)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(caching.cache.path(first.key).read_bytes())
        assert caching.cache.get(other) is None
        engine = Engine()
        assert (
            client(tmp_path, engine).complete("Read it again.")
            == ("Read it again."[::-1])
        )
        assert kinds(engine) == [COMPLETE]
        assert caching.cache.get(other)["key"] == other

    def test_a_displaced_entry_is_kept_aside(self, tmp_path):
        """A newer entry under a key (a refresh's, a racing call's, one
        displacing a damaged entry) never overwrites the one there: that
        one is moved aside, as replaced."""
        cache = EngineCache(tmp_path / "cache")
        key = "ab" + "0" * 62
        cache.put(key, {"key": key, "response": "first"})
        cache.put(key, {"key": key, "response": "second"})
        assert cache.get(key)["response"] == "second"
        (aside,) = cache.path(key).parent.glob(f"{key}.replaced-*.json")
        kept = json.loads(aside.read_text())
        assert kept["response"] == "first"
        assert "at" in kept["replaced"]

    @pytest.mark.real_fsync
    @pytest.mark.parametrize("stored", ["answer", "adoption"])
    def test_an_entry_is_fsynced_whole_before_it_is_linked_into_place(
        self, tmp_path, monkeypatch, stored
    ):
        """A crash just after the link must not leave the key naming a
        file whose bytes never reached the disk: an entry is written to a
        staging file, which is fsynced, with all of the entry in it,
        before os.link publishes it under the key (#254). The unit tests
        skip fsync unless marked real_fsync (tests/unit/conftest.py)."""
        calls: list[tuple] = []
        real_fsync, real_link = os.fsync, os.link

        def file_id(info: os.stat_result) -> tuple[int, int]:
            return info.st_dev, info.st_ino

        def fsync(fd):
            info = os.fstat(fd)
            calls.append(("fsync", file_id(info), info.st_size))
            return real_fsync(fd)

        def link(src, dst, *args, **kwargs):
            calls.append(("link", file_id(os.stat(src)), os.fspath(dst)))
            return real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "link", link)
        caching = client(tmp_path, Engine())
        if stored == "answer":
            key = caching.complete("Read it.").key
        else:
            key = caching.complete_key("Read it.")
            assert caching.seed(key, "archived text", {"engine": "archive"})
        path = caching.cache.path(key)
        (linked,) = [n for n, call in enumerate(calls) if call[0] == "link"]
        _, published, destination = calls[linked]
        assert destination == os.fspath(path)
        assert file_id(path.stat()) == published
        synced_sizes = [
            size
            for name, synced, size in calls[:linked]
            if name == "fsync" and synced == published
        ]
        assert synced_sizes, f"the entry was not fsynced first: {calls}"
        assert synced_sizes[-1] == path.stat().st_size

    def test_parallel_calls_under_one_key_leave_one_whole_entry(
        self, tmp_path
    ):
        # The engine answers no call until all eight are with it, so all
        # eight miss the cache and then store at once, however loaded the
        # machine (a sleep would let a late thread hit the first answer).
        # Calls kept apart under one key would break the barrier at its
        # timeout and fail the test.
        in_flight = threading.Barrier(8, timeout=30)

        def answer(kind, prompt, opening):
            in_flight.wait()
            return f"answer from {threading.current_thread().name}"

        cache = EngineCache(tmp_path / "cache")
        answers = []

        def call():
            answers.append(wrap(Engine(answer), cache, role="r").complete("x"))

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(answers) == 8
        # one live entry, whole; every answer it displaced is kept aside
        (entry,) = live_entries(tmp_path)
        assert entry["response"] in answers
        kept = [
            json.loads(path.read_text())["response"]
            for path in entry_files(tmp_path)
        ]
        assert sorted(kept) == sorted(answers)
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

    def test_an_existing_cache_folder_is_made_private(self, tmp_path):
        caching = client(tmp_path, Engine())
        root = tmp_path / "cache"
        shard = caching.cache.path(caching.complete_key("Read it.")).parent
        shard.mkdir(parents=True)
        for directory in (root, shard):
            os.chmod(directory, 0o755)
        caching.complete("Read it.")
        for directory in (root, shard):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_response_copies_and_pickles_with_its_key(self, tmp_path):
        client(tmp_path, Engine()).complete("Read it.")
        response = client(tmp_path, Engine()).complete("Read it.")
        assert response.hit
        for twin in (
            copy.copy(response),
            copy.deepcopy(response),
            pickle.loads(pickle.dumps(response)),
        ):
            assert isinstance(twin, CachedResponse)
            assert twin == response
            assert twin.key == response.key
            assert twin.hit

    def test_nothing_is_stored_until_a_call_is_answered(self, tmp_path):
        """Opening a cache makes its root, private, and locks it (#236);
        it stores nothing until a call is answered."""
        open_cache(tmp_path / "cache")
        client(tmp_path, Engine())
        root = tmp_path / "cache"
        assert os.listdir(root) == [LOCK_FILE]
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / LOCK_FILE).stat().st_mode) == 0o600


# -- one process per cache -----------------------------------------------

# a second process: it opens the cache named by its argument, says so, and
# holds it until its stdin closes
HOLDER = """
import sys
from yizkr.pipeline.llm import open_cache
open_cache(sys.argv[1])
print("held", flush=True)
sys.stdin.read()
"""


@contextlib.contextmanager
def another_process(root: Path):
    """A second process holding the cache under `root`, until the block
    ends (a clean exit) or the test kills it."""
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline() == "held\n", process.stderr.read()
        yield process
    finally:
        if process.poll() is None:
            process.stdin.close()
        process.wait(timeout=60)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def opened_elsewhere(root: Path) -> subprocess.CompletedProcess:
    """A second process that tries to open the cache under `root`."""
    return subprocess.run(
        [sys.executable, "-c", HOLDER, str(root)],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )


def lock_record(root: Path) -> dict:
    return json.loads((root / LOCK_FILE).read_text())


class TestOneProcess:
    """One process at a time uses a cache (#236): opening one locks it for
    the rest of the process, a second process is refused with a message
    naming the holder, and a holder that crashed blocks nothing."""

    def test_a_second_process_is_refused(self, tmp_path, caplog):
        root = tmp_path / "cache"
        with another_process(root) as holder:
            with pytest.raises(CacheInUse) as refused:
                EngineCache(root)
            message = str(refused.value)
            assert f"process {holder.pid} " in message
            assert str(root) in message
            assert refused.value.holder["pid"] == holder.pid
            # a pause of the pass, which no per-document isolation catches
            assert isinstance(refused.value, PassPaused)
            assert not isinstance(refused.value, Exception)
            # the refused process leaves the holder's record as it was
            assert lock_record(root)["pid"] == holder.pid
        # the holder ended cleanly: the cache opens, with nothing to report
        with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
            EngineCache(root)
        assert caplog.records == []
        assert lock_record(root)["pid"] == os.getpid()

    def test_a_process_is_refused_while_this_one_holds_it(self, tmp_path):
        root = tmp_path / "cache"
        EngineCache(root)
        refused = opened_elsewhere(root)
        assert refused.returncode != 0
        assert "CacheInUse" in refused.stderr
        assert f"process {os.getpid()} " in refused.stderr

    def test_a_crashed_holders_lock_does_not_block(self, tmp_path, caplog):
        """A holder killed outright releases the lock with its life; the
        next process to open the cache finds the record it left, says
        whose it was, and replaces it."""
        root = tmp_path / "cache"
        with another_process(root) as holder:
            holder.kill()
            holder.wait(timeout=60)
            assert lock_record(root)["pid"] == holder.pid
            with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
                cache = EngineCache(root)
        (warning,) = caplog.records
        assert f"process {holder.pid} " in warning.getMessage()
        assert "ended without clearing its record" in warning.getMessage()
        assert lock_record(root)["pid"] == os.getpid()
        # the cache is this process's now, and serves it
        caching = wrap(Engine(), cache, role="extract")
        assert caching.complete("Read it.") == ".ti daeR"

    def test_a_process_holds_its_cache_once(self, tmp_path, caplog):
        """A process opens its cache as often as it likes, under any
        spelling of the root, and holds it once."""
        root = tmp_path / "cache"
        EngineCache(root)
        record = lock_record(root)
        (tmp_path / "link").symlink_to(root)
        with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
            for spelling in (root, tmp_path / "link", root / ".." / "cache"):
                EngineCache(spelling)
        assert caplog.records == []
        assert lock_record(root) == record
        assert opened_elsewhere(root).returncode != 0

    def test_a_removed_lock_file_is_locked_again(self, tmp_path, caplog):
        root = tmp_path / "cache"
        EngineCache(root)
        (root / LOCK_FILE).unlink()
        with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
            EngineCache(root)
        assert "removed or replaced" in caplog.text
        assert lock_record(root)["pid"] == os.getpid()
        assert f"process {os.getpid()} " in opened_elsewhere(root).stderr


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

    def test_a_rejection_says_whether_the_cache_gave_the_answer(
        self, tmp_path
    ):
        """Rejecting a hit returns True: the engine has not answered the
        call in this run, and a caller asks once more (#230). Rejecting
        the engine's own answer returns False, as an engine's client
        does."""
        caching = client(tmp_path, Engine())
        assert not caching.complete("Read it.").hit
        kept = client(tmp_path, Engine()).complete("Read it.")
        assert kept.hit
        assert llm.reject_response(caching, kept, "why") is True
        answered = caching.complete("Read it.")
        assert not answered.hit
        assert llm.reject_response(caching, answered, "why") is False
        assert llm.reject_response(Engine(), "an answer", "why") is False

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
        is no longer stored moves nothing, and is no event."""
        first = client(tmp_path, Engine()).complete("Read it.")
        newer = client(
            tmp_path,
            Engine(lambda kind, prompt, opening: "newer"),
            refresh={"extract"},
        ).complete("Read it.")
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, Refusing(), run=run)
        caching.reject(first, "failed the check")
        assert caching.complete("Read it.") == newer == "newer"
        assert caching.cache.rejected(first.key) == []
        assert events(run, "cache-reject") == []

    def test_an_answer_stored_as_the_rejection_starts_stays(
        self, tmp_path, monkeypatch
    ):
        """A newer answer stored under the key just before a rejection
        takes the entry is taken instead, found to be another answer and
        put back: nothing is rejected (#225)."""
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, Engine(), run=run)
        stale = caching.complete("Read it.")
        rename = os.rename

        def interleaved(source, target):
            monkeypatch.setattr(llmcache.os, "rename", rename)
            caching.cache.put(
                stale.key,
                {"format": 1, "key": stale.key, "response": "newer"},
            )
            return rename(source, target)

        monkeypatch.setattr(llmcache.os, "rename", interleaved)
        caching.reject(stale, "failed the check")
        assert caching.cache.get(stale.key)["response"] == "newer"
        assert caching.cache.rejected(stale.key) == []
        assert events(run, "cache-reject") == []
        # the stale answer was displaced by the newer one, and is kept
        (aside,) = caching.cache.path(stale.key).parent.glob(
            "*.replaced-*.json"
        )
        assert json.loads(aside.read_text())["response"] == stale

    @pytest.mark.parametrize("rejected", ["held", "older"])
    def test_an_answer_stored_while_a_rejection_holds_the_entry_stays(
        self, tmp_path, monkeypatch, rejected
    ):
        """While a rejection holds the entry under its private name,
        another caller stores an answer under the key. The new answer
        stays live whatever the held entry is: the rejected answer goes
        aside as rejected, and any other as replaced (#225)."""
        caching = client(tmp_path, Engine())
        held = caching.complete("Read it.")
        read = llmcache._read

        def interleaved(path, key):
            monkeypatch.setattr(llmcache, "_read", read)
            caching.cache.put(
                key, {"format": 1, "key": key, "response": "newest"}
            )
            return read(path, key)

        monkeypatch.setattr(llmcache, "_read", interleaved)
        answer = str(held) if rejected == "held" else "an older answer"
        moved = caching.cache.reject(held.key, answer, "failed the check")
        assert moved is (rejected == "held")
        assert caching.cache.get(held.key)["response"] == "newest"
        folder = caching.cache.path(held.key).parent
        kind = "rejected" if rejected == "held" else "replaced"
        (aside,) = folder.glob(f"*.{kind}-*.json")
        assert json.loads(aside.read_text())["response"] == held
        assert len(list(folder.glob("*.json"))) == 2

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
        assert stored["fork:c1 after the document"]["session"] == "live"
        # every fork records the opening answer it was made on
        opening = hashlib.sha256(b"open:the document").hexdigest()
        assert {
            provenance["opening_sha256"]
            for response, provenance in stored.items()
            if response.startswith("fork:")
        } == {opening}

    def test_a_kept_session_resumes_exactly(self, tmp_path):
        """An engine that can resume a conversation (llm.
        ResumableLlmClient) is given the session files the cache kept when
        the conversation was opened: its forks start from the very turn
        the cache answered, and nothing opens it again (#225)."""
        self.run_conversation(
            tmp_path, ResumableEngine(conversation_answers), ["c1"]
        )
        (kept,) = (tmp_path / "cache").glob("*/*.session")
        # the kept session is as private as the entries
        assert {
            stat.S_IMODE(folder.stat().st_mode)
            for folder in (tmp_path / "cache").rglob("*")
            if folder.is_dir()
        } == {0o700}
        engine = ResumableEngine(conversation_answers)
        run = RunDir(tmp_path / "run")
        response, forks = self.run_conversation(
            tmp_path, engine, ["c1", "c2", "c3"], run=run
        )
        assert kinds(engine) == [FORK, FORK]
        assert engine.resumed == [
            {"sessions/session-1.jsonl": b"the document"}
        ]
        assert kept.name == f"{response.key}.session"
        (resume,) = events(run, "cache-resume")
        assert (resume["key"], resume["call"]) == (response.key, OPEN)
        assert events(run, "cache-replay") == []
        stored = {
            entry["response"]: entry["provenance"]
            for entry in live_entries(tmp_path)
        }
        provenance = stored["fork:c2 after the document"]
        assert provenance["session"] == "resumed"
        assert provenance["opening_sha256"] == (
            hashlib.sha256(response.encode()).hexdigest()
        )

    def test_a_kept_session_is_private(self, tmp_path):
        """A session's files are kept as privately as the entries, in
        folders made private on the way down from the root, and published
        whole."""
        cache = EngineCache(tmp_path / "cache")
        key, opening = "ab" + "0" * 62, "cd" + "1" * 62
        cache.keep_session(key, opening, {"p/s.jsonl": b"a session"})
        assert cache.kept_session(key, opening) == {"p/s.jsonl": b"a session"}
        assert cache.kept_session(key, "cd" + "2" * 62) == {}
        folders = [p for p in (tmp_path / "cache").rglob("*") if p.is_dir()]
        assert {folder.name for folder in folders} == {
            "ab",
            f"{key}.session",
            opening,
            "p",
        }
        assert {stat.S_IMODE(f.stat().st_mode) for f in folders} == {0o700}
        with pytest.raises(ValueError):
            cache.kept_session(key, "not an answer's hash")

    def test_an_unusable_kept_session_falls_back_to_a_replay(self, tmp_path):
        self.run_conversation(
            tmp_path, ResumableEngine(conversation_answers), []
        )
        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        engine = ResumableEngine(conversation_answers)
        run = RunDir(tmp_path / "run")
        self.run_conversation(tmp_path, engine, ["c1"], run=run)
        assert kinds(engine) == [OPEN, FORK]
        assert events(run, "cache-resume") == []
        (replay,) = events(run, "cache-replay")
        assert replay["matches"] is True
        # the replay's session is kept for the next run to resume
        assert len(list((tmp_path / "cache").glob("*/*.session"))) == 1

    def test_parallel_fork_misses_replay_once(self, tmp_path):
        """Forks that miss at once share one replay: the conversation's
        lock holds the others while it takes a moment, as a real
        engine's does."""
        self.run_conversation(tmp_path, Engine(conversation_answers), [])

        def slow(kind, prompt, opening):
            if kind == OPEN:
                time.sleep(0.05)
            return conversation_answers(kind, prompt, opening)

        engine = Engine(slow)
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

    def test_a_replay_that_answers_differently_restarts_it(self, tmp_path):
        """A replay whose answer differs from the cached one makes the
        whole conversation a miss (#225): the replay's answer becomes the
        opening turn's entry, the fork raises ConversationChanged, and the
        conversation opened again forks from the replay's session, keyed
        on the new answer. The replay's session is kept, under the new
        answer, for the next run to resume."""
        self.run_conversation(tmp_path, Engine(conversation_answers), ["c1"])

        def answer(kind, prompt, opening):
            if kind == OPEN:
                return "open again"
            return conversation_answers(kind, prompt, opening)

        engine = ResumableEngine(answer)
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, engine, run=run)
        response, handle = caching.open_conversation("the document")
        assert response == "open:the document"
        with pytest.raises(ConversationChanged):
            caching.fork(handle, "c2")
        (replay,) = events(run, "cache-replay")
        assert replay["matches"] is False
        assert caching.cache.get(response.key)["response"] == "open again"
        (replaced,) = (tmp_path / "cache").glob("*/*.replaced-*.json")
        assert json.loads(replaced.read_text())["response"] == response
        # the caller starts over: the opening turn is answered from the
        # cache, with the replay's session, and its forks are new calls
        again, handle = caching.open_conversation("the document")
        assert again == "open again"
        assert caching.fork(handle, "c1") == "fork:c1 after the document"
        assert kinds(engine) == [OPEN, FORK]
        (entry,) = [
            entry
            for entry in live_entries(tmp_path)
            if entry["provenance"].get("session") == "recomputed"
        ]
        assert entry["provenance"]["opening_sha256"] == (
            hashlib.sha256(b"open again").hexdigest()
        )
        # the session kept is the replay's, under the answer it gave, and
        # a later run resumes it for its forks
        assert caching.cache.kept_session(
            response.key, sha256("open again")
        ) == {"sessions/session-1.jsonl": b"the document"}
        later = ResumableEngine(answer)
        later_run = RunDir(tmp_path / "later")
        _, handle = client(tmp_path, later, run=later_run).open_conversation(
            "the document"
        )
        client(tmp_path, later).fork(handle, "c3")
        assert kinds(later) == [FORK]
        assert later.resumed == [{"sessions/session-1.jsonl": b"the document"}]

    def test_racing_openings_each_keep_their_own_session(
        self, tmp_path, monkeypatch
    ):
        """Two callers that miss the same opening turn at once each store
        their answer and keep their session, in either order: the entry
        that stays live is the later one, and a fork resumes the session
        kept under that answer, never the other's (#230)."""
        root = tmp_path / "cache"
        first = wrap(AnswerKeepingEngine(), EngineCache(root), role="extract")
        second = wrap(
            AnswerKeepingEngine(first=101), EngineCache(root), role="extract"
        )
        keep = EngineCache.keep_session
        raced = []

        def racing(cache, key, opening, files):
            # the first caller has stored its answer; before it keeps its
            # session, the second, which missed too, opens the
            # conversation whole
            if not raced:
                raced.append(key)
                monkeypatch.setattr(second.cache, "get", lambda key: None)
                second.open_conversation("the document")
            return keep(cache, key, opening, files)

        monkeypatch.setattr(EngineCache, "keep_session", racing)
        first.open_conversation("the document")
        monkeypatch.setattr(EngineCache, "keep_session", keep)
        (key,) = raced
        assert EngineCache(root).get(key)["response"] == "opening answer #101"
        for answer in ("opening answer #1", "opening answer #101"):
            assert EngineCache(root).kept_session(key, sha256(answer)) == {
                f"sessions/session-{answer.split('#')[1]}.jsonl": (
                    answer.encode()
                )
            }
        # the next run: the opening turn is answered from the cache, and
        # the fork resumes the session made on that answer
        engine = AnswerKeepingEngine(first=201)
        run = RunDir(tmp_path / "run")
        caching = wrap(
            engine, EngineCache(root), role="extract", events=run.emit
        )
        response, handle = caching.open_conversation("the document")
        fork = caching.fork(handle, "c1")
        assert made_on(fork) == response == "opening answer #101"
        assert kinds(engine) == [FORK]
        assert len(events(run, "cache-resume")) == 1
        assert events(run, "cache-replay") == []
        (entry,) = [e for e in live_entries(tmp_path) if e["response"] == fork]
        assert entry["provenance"]["session"] == "resumed"
        assert entry["provenance"]["opening_sha256"] == sha256(response)

    @pytest.mark.parametrize("failure", ["keep raises", "nothing to keep"])
    def test_a_changed_replay_whose_session_is_not_kept_resumes_nothing(
        self, tmp_path, monkeypatch, failure
    ):
        """A replay that answers differently stores its answer; when its
        session cannot be kept (the cache cannot write it, or the engine
        no longer holds it), the session kept for the old answer stays,
        and is never resumed for the new one: the next run replays the
        opening turn instead (#230)."""
        root = tmp_path / "cache"
        wrap(
            AnswerKeepingEngine(), EngineCache(root), role="extract"
        ).open_conversation("the document")
        # the next run cannot resume, so it replays, and the replay
        # answers differently; its session is not kept
        engine = AnswerKeepingEngine(first=51, keeps=failure == "keep raises")

        def unusable(prompt, response, files):
            raise ValueError("an engine that cannot resume this session")

        engine.resume_conversation = unusable
        with monkeypatch.context() as patch:
            if failure == "keep raises":

                def full(cache, key, opening, files):
                    raise OSError("no space left on the device")

                patch.setattr(EngineCache, "keep_session", full)
            caching = wrap(engine, EngineCache(root), role="extract")
            response, handle = caching.open_conversation("the document")
            assert response == "opening answer #1"
            with pytest.raises(ConversationChanged):
                caching.fork(handle, "c1")
        key = response.key
        assert EngineCache(root).get(key)["response"] == "opening answer #51"
        # the old answer's session is kept aside from the new answer's
        assert EngineCache(root).kept_session(key, sha256(response))
        assert (
            EngineCache(root).kept_session(key, sha256("opening answer #51"))
            == {}
        )
        # a later run: the new answer is served, and its fork is never
        # made on the old answer's session
        later = AnswerKeepingEngine(first=201)
        run = RunDir(tmp_path / "run")
        caching = wrap(
            later, EngineCache(root), role="extract", events=run.emit
        )
        served, handle = caching.open_conversation("the document")
        assert served == "opening answer #51"
        with pytest.raises(ConversationChanged):
            caching.fork(handle, "c2")
        assert later.resumed == [{}]
        assert events(run, "cache-resume") == []
        again, handle = caching.open_conversation("the document")
        fork = caching.fork(handle, "c2")
        assert made_on(fork) == again == "opening answer #201"
        assert all(
            made_on(entry["response"])
            == next(
                opening["response"]
                for opening in live_entries(tmp_path)
                if sha256(opening["response"])
                == entry["components"]["opening"]
            )
            for entry in live_entries(tmp_path)
            if entry["components"]["call"] == FORK
        )

    @pytest.mark.parametrize("kept", ["unusable", "nothing at first"])
    def test_a_held_session_serves_only_the_answer_it_was_made_on(
        self, tmp_path, kept
    ):
        """Two cache objects on one root in one process (two processes
        cannot share a cache, #236) each replay a conversation's opening
        turn, because the session kept for the cached answer is unusable
        or none was kept, and the two replays answer differently. Each
        stores its answer and holds its session in memory; the second's
        answer is the one left stored. When the first starts over, the
        cache serves it the second's answer, so the session it holds, made
        on its own answer, is released and never forked: the fork resumes
        the session kept for the served answer, or replays again (#230).
        Every fork either stores is made on the answer its key names."""
        root = tmp_path / "cache"
        wrap(
            AnswerKeepingEngine(keeps=kept == "unusable"),
            EngineCache(root),
            role="extract",
        ).open_conversation("the document")
        first = AnswerKeepingEngine(first=51)
        second = AnswerKeepingEngine(first=81)
        released = {first: [], second: []}
        for engine in (first, second):
            engine.close_conversation = released[engine].append
            if kept == "unusable":

                def unusable(prompt, response, files):
                    raise ValueError("an engine that cannot resume it")

                engine.resume_conversation = unusable
        run1 = wrap(first, EngineCache(root), role="extract")
        run2 = wrap(second, EngineCache(root), role="extract")
        _, handle1 = run1.open_conversation("the document")
        _, handle2 = run2.open_conversation("the document")
        with pytest.raises(ConversationChanged):
            run1.fork(handle1, "c1")  # stores #51, holds session-51
        with pytest.raises(ConversationChanged):
            run2.fork(handle2, "c1")  # stores #81, holds session-81
        key = handle1.chain[0]
        assert EngineCache(root).get(key)["response"] == "opening answer #81"

        def start_over(caching):
            """Start over from the opening turn, as the extractor does."""
            for _pass in range(3):
                served, handle = caching.open_conversation("the document")
                try:
                    return served, handle, caching.fork(handle, "c1")
                except ConversationChanged:
                    continue
            raise AssertionError("the conversation never settled")

        served, handle, fork = start_over(run1)
        assert made_on(fork) == served
        # the session held for #51 is released, not forked on #81
        assert released[first] == ["session-51"]
        (entry,) = [e for e in live_entries(tmp_path) if e["response"] == fork]
        if kept == "unusable":
            # nothing kept can be resumed: the first run replays again,
            # stores #52 and forks on the session it holds for it
            assert served == "opening answer #52"
            assert entry["provenance"]["session"] == "recomputed"
        else:
            # the second run kept its replay's session under #81, and the
            # first run's fork resumes it
            assert served == "opening answer #81"
            assert entry["provenance"]["session"] == "resumed"
            assert first.resumed[-1] == {
                "sessions/session-81.jsonl": b"opening answer #81"
            }
        # the second run starts over too, after the first's last store
        served, handle, fork = start_over(run2)
        assert made_on(fork) == served
        # its held session is released only when the answer served is not
        # the one it was made on
        assert released[second] == (
            ["session-81"] if kept == "unusable" else []
        )
        # every fork stored is made on the answer its key and its
        # provenance name
        forks = [
            entry
            for entry in live_entries(tmp_path)
            if entry["components"]["call"] == FORK
        ]
        assert forks
        for entry in forks:
            made = sha256(made_on(entry["response"]))
            assert entry["components"]["opening"] == made
            assert entry["provenance"]["opening_sha256"] == made

    def test_a_replay_without_text_changes_nothing(self, tmp_path):
        """A replay that answers with no text is a failed call: the fork
        fails, the opening turn's entry stands, and the replay's session
        is released. The conversation's later forks fail at once."""
        self.run_conversation(tmp_path, Engine(conversation_answers), [])
        released = []

        def answer(kind, prompt, opening):
            return "" if kind == OPEN else "a fork"

        engine = Engine(answer)
        engine.close_conversation = released.append
        caching = client(tmp_path, engine)
        response, handle = caching.open_conversation("the document")
        with pytest.raises(RuntimeError, match="no text"):
            caching.fork(handle, "c1")
        with pytest.raises(RuntimeError, match="failed earlier"):
            caching.fork(handle, "c2")
        assert caching.cache.get(response.key)["response"] == response
        assert released == ["session-1"]
        assert kinds(engine) == [OPEN]

    def test_a_failed_replay_is_remembered_for_the_pass(self, tmp_path):
        """A replay the engine fails fails the fork, and every later fork
        of the conversation at once, without asking the engine again: a
        failing engine is asked once per pass, not once per chunk (#236).
        None of those forks is a miss, since none is asked. The
        conversation opened again, for a new pass, replays again."""
        self.run_conversation(tmp_path, Engine(conversation_answers), [])

        class Down(Engine):
            def open_conversation(self, prompt):
                self._log(OPEN, prompt)
                raise RuntimeError("engine down")

        engine = Down(conversation_answers)
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, engine, run=run)
        _, handle = caching.open_conversation("the document")
        with pytest.raises(RuntimeError, match="engine down"):
            caching.fork(handle, "c1")
        for prompt in ("c1", "c2", "c3"):
            with pytest.raises(RuntimeError, match="failed earlier.*down"):
                caching.fork(handle, prompt)
        assert kinds(engine) == [OPEN]
        assert events(run, "cache-miss", "cache-replay") == []
        _, handle = caching.open_conversation("the document")
        with pytest.raises(RuntimeError, match="engine down"):
            caching.fork(handle, "c1")
        assert kinds(engine) == [OPEN, OPEN]

    def test_a_fork_misses_only_when_it_is_asked(self, tmp_path):
        """A fork the cache cannot answer is a miss once the engine holds
        its session, just before the fork is asked; a fork that ends for
        want of one, as the conversation changed, is none (#236)."""
        self.run_conversation(
            tmp_path, ResumableEngine(conversation_answers), ["c1"]
        )
        run = RunDir(tmp_path / "resumed")
        self.run_conversation(
            tmp_path, ResumableEngine(conversation_answers), ["c2"], run=run
        )
        assert [(e["event"], e["call"]) for e in run.events()] == [
            ("cache-hit", OPEN),
            ("cache-resume", OPEN),
            ("cache-miss", FORK),
        ]

        def changed(kind, prompt, opening):
            if kind == OPEN:
                return "open again"
            return conversation_answers(kind, prompt, opening)

        for kept in (tmp_path / "cache").glob("*/*.session"):
            shutil.rmtree(kept)
        run = RunDir(tmp_path / "changed")
        caching = client(tmp_path, Engine(changed), run=run)
        _, handle = caching.open_conversation("the document")
        with pytest.raises(ConversationChanged):
            caching.fork(handle, "c3")
        assert [e["event"] for e in run.events()] == [
            "cache-hit",
            "cache-replay",
        ]
        _, handle = caching.open_conversation("the document")
        caching.fork(handle, "c3")
        assert [e["event"] for e in run.events()][2:] == [
            "cache-hit",
            "cache-miss",
        ]

    def test_a_held_session_is_taken_once(self, tmp_path):
        """A session held after a replay is handed out once, and only for
        the engine client that holds it."""
        cache = EngineCache(tmp_path / "cache")
        engine, other = Engine(), Engine()
        key, opening = "ab" + "0" * 62, sha256("opening answer #51")
        cache.remember_session(key, engine, "session-51", opening)
        assert cache.take_session(key, other) is None
        assert cache.take_session(key, engine) == ("session-51", opening)
        assert cache.take_session(key, engine) is None

    def test_a_displaced_held_session_is_released(self, tmp_path):
        """A session held for a key that a later replay's session
        displaces will never be taken: it is released through its
        engine's close_conversation (#236). The same session held again
        is not."""
        cache = EngineCache(tmp_path / "cache")
        released = {"first": [], "second": []}
        first, second = Engine(), Engine()
        first.close_conversation = released["first"].append
        second.close_conversation = released["second"].append
        key = "ab" + "0" * 62
        cache.remember_session(key, first, "session-51", sha256("#51"))
        cache.remember_session(key, first, "session-51", sha256("#51"))
        assert released == {"first": [], "second": []}
        cache.remember_session(key, second, "session-81", sha256("#81"))
        assert released == {"first": ["session-51"], "second": []}
        assert cache.take_session(key, second) == ("session-81", sha256("#81"))

    def test_racing_replays_release_the_session_they_displace(self, tmp_path):
        """Two callers in one run fork the same conversation, opened from
        the cache; nothing kept can be resumed, and each replays it, with
        different answers. The second replay's session displaces the
        first's, which is released; whoever starts over first takes the
        session held for the answer served, and the other resumes the
        session kept for it. Every fork is made on the answer served."""
        root = tmp_path / "cache"
        wrap(
            AnswerKeepingEngine(keeps=False), EngineCache(root), role="extract"
        ).open_conversation("the document")
        engine = AnswerKeepingEngine(first=51)
        released = []
        engine.close_conversation = released.append
        caching = wrap(engine, EngineCache(root), role="extract")
        _, one = caching.open_conversation("the document")
        _, two = caching.open_conversation("the document")
        with pytest.raises(ConversationChanged):
            caching.fork(one, "c1")  # stores #51, holds session-51
        assert released == []
        with pytest.raises(ConversationChanged):
            caching.fork(two, "c1")  # stores #52, holds session-52
        assert released == ["session-51"]
        forks = []
        for _caller in range(2):
            served, handle = caching.open_conversation("the document")
            assert served == "opening answer #52"
            forks.append(caching.fork(handle, f"c{len(forks) + 2}"))
        assert [made_on(fork) for fork in forks] == ["opening answer #52"] * 2
        # nothing was kept for the first answer; the second caller resumes
        # the session kept for the answer served
        assert engine.resumed == [
            {},
            {},
            {"sessions/session-52.jsonl": b"opening answer #52"},
        ]
        assert released == ["session-51"]

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

    def test_each_run_refreshes_once(self, tmp_path):
        """A refresh belongs to one run: every run that names the role
        asks the engine again, once per key, and a run that does not is
        served (#225)."""
        client(tmp_path, Engine()).complete("Read it.")
        answers = []
        for n in range(2):
            engine = Engine(lambda kind, prompt, opening, n=n: f"fresh {n}")
            caching = client(tmp_path, engine, refresh={"extract"})
            answers += [caching.complete("Read it."), caching.complete("x")]
            assert kinds(engine) == [COMPLETE, COMPLETE]
        assert answers == ["fresh 0", "fresh 0", "fresh 1", "fresh 1"]
        assert client(tmp_path, Refusing()).complete("Read it.") == "fresh 1"

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

    def test_a_rejection_that_moves_nothing_is_no_event(self, tmp_path):
        run = RunDir(tmp_path / "run")
        caching = client(tmp_path, Engine(), run=run)
        first = caching.complete("Read it.")
        caching.reject(first, "failed the check")
        caching.reject(first, "failed the check")  # aside already
        assert len(events(run, "cache-reject")) == 1


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

    def test_a_rejected_answer_is_never_adopted(self, tmp_path):
        caching = client(tmp_path, Engine())
        answer = caching.complete("Read it.")
        caching.reject(answer, "failed the check")
        assert not caching.seed(answer.key, str(answer), self.PROVENANCE)
        assert caching.cache.get(answer.key) is None
        assert caching.seed(answer.key, "a corrected text", self.PROVENANCE)

    def test_an_adoption_cannot_pass_for_the_engine(self, tmp_path):
        caching = client(tmp_path, Engine())
        key = caching.complete_key("Read it.")
        with pytest.raises(ValueError, match="engine"):
            caching.seed(key, "text", self.PROVENANCE, reason="engine")
        assert entry_files(tmp_path) == []


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

    def test_an_empty_model_name_is_no_model(self, tmp_path, caplog):
        """An empty $YIZKR_LLM_MODEL names no model either: the engine
        runs its own default, keyed as none, with the warning."""
        cache = EngineCache(tmp_path / "cache")
        with caplog.at_level(logging.WARNING, logger=llmcache.__name__):
            empty = wrap(Engine(model=""), cache, role="critique")
        assert "critique role" in caplog.text
        unnamed = wrap(Engine(model=None), cache, role="critique")
        assert empty.complete_key("Read it.") == unnamed.complete_key(
            "Read it."
        )


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


# a scan read with a service notice after its text, which the check
# passes until a stricter one knows the notice
NOTICE = LETTER + (
    "\nNotice: the reading service will be offline for maintenance."
)
READ_AGAIN = "Klara was born in 1917."


def reading(text: str):
    """pipeline_answers, with the scan read as `text`."""

    def answer(kind, prompt, opening):
        if prompt.startswith(TRANSCRIPTION_PROMPT):
            return text
        return pipeline_answers(kind, prompt, opening)

    return answer


def stricter_check(monkeypatch) -> None:
    """Make the transcription check stricter, as a change to it would: a
    signature for the service notice."""
    signature = transcription_check._signature(
        "reading-service-notice",
        transcription_check.TOOL_FAILURE,
        r"reading service will be offline",
    )
    monkeypatch.setattr(
        transcription_check,
        "SIGNATURES",
        (*transcription_check.SIGNATURES, signature),
    )


def transcriptions(engine: Engine) -> int:
    """How many transcriptions the engine was asked for."""
    return sum(
        prompt.startswith(TRANSCRIPTION_PROMPT) for _, prompt in engine.calls
    )


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
        completed; the next run reuses the views the first one finished,
        and asks the engine only for the calls not yet made, and for a
        session to fork from."""
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
        # the scan's views were finished, and stand
        scan = next(d for d in again.documents() if d.kind == "media")
        assert {
            event["stage"]
            for event in events(again.rundir, "reused")
            if event["subject"] == scan.sha1
        } == {"transcription", "extraction", "critique"}
        # every call that completed and is made again is answered from the
        # cache, and none is asked of the engine again
        hits = {e["key"] for e in events(again.rundir, "cache-hit")}
        misses = events(again.rundir, "cache-miss")
        assert hits and hits <= completed
        assert not {e["key"] for e in misses} & completed
        # the engine answered the calls not yet made, and replayed the
        # interrupted document's opening turn once, for a session to fork
        (replay,) = events(again.rundir, "cache-replay")
        assert replay["key"] in completed
        assert len(engine.calls) == len(misses) + 1
        assert [call for call in engine.calls if call in answered] == [
            answered[-1]
        ]

    @pytest.mark.parametrize("cached", [True, False], ids=["cache", "none"])
    def test_a_reused_transcription_is_checked_again(
        self, tmp_path, monkeypatch, cached
    ):
        """A transcription view that stands is checked again whenever it
        is reused (#230). When a stricter check fails it, it is moved
        aside, never deleted, with a `set-aside` event giving the reason;
        the cached answer behind it is rejected; the engine is asked, as
        without the cache; and the new transcription is checked and made
        the view. The batch made from it differs, so the reviews made on
        the old one are moved aside."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "scan.png").write_bytes(b"not really a png")

        def run(engine):
            orchestrator = CachedRun(
                folder,
                tmp_path / "state",
                engine=engine,
                cache_dir=tmp_path / "cache" if cached else None,
                log=quiet,
            )
            orchestrator.run()
            (doc,) = orchestrator.documents()
            return orchestrator, doc

        _, doc = run(Engine(reading(NOTICE)))
        approve_all_equivalences(doc)
        _, doc = run(Engine(reading(NOTICE)))
        approve_all_claims(doc)
        _, doc = run(Engine(reading(NOTICE)))
        assert doc.state.status == WRITTEN
        stricter_check(monkeypatch)
        engine = Engine(reading(READ_AGAIN))
        again, doc = run(engine)
        (aside,) = this_run(again, "set-aside")
        assert aside["stage"] == "transcription"
        assert "reading-service-notice" in aside["reason"]
        assert aside["kept"] == "transcription.superseded-1.json"
        kept = json.loads((doc.dir / aside["kept"]).read_text())
        assert kept["text"] == NOTICE
        assert "transcription" not in {
            event["stage"] for event in this_run(again, "reused")
        }
        # the engine was asked once for the scan, and its answer is the view
        assert transcriptions(engine) == 1
        assert json.loads(doc.transcription.read_text())["text"] == READ_AGAIN
        if cached:
            (reject,) = this_run(again, "cache-reject")
            assert reject["role"] == "transcribe"
            assert "reading-service-notice" in reject["reason"]
            (miss,) = [
                event
                for event in this_run(again, "cache-miss")
                if event["role"] == "transcribe"
            ]
            assert miss["reason"] == "rejected"
        else:
            assert not any(
                event["event"].startswith("cache-")
                for event in again.rundir.events()
            )
        # the new batch moved the reviews of the old one aside
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert sorted(
            path.name for path in doc.dir.glob("*.superseded-*")
        ) == [
            "equivalences.reviewed.superseded-1.csv",
            "review.reviewed.superseded-1.csv",
            "transcription.superseded-1.json",
        ]
        # a view that passes the check stands, and is checked each time
        again, doc = run(Refusing(reading(READ_AGAIN)))
        assert this_run(again, "set-aside") == []
        assert "transcription" in {
            event["stage"] for event in this_run(again, "reused")
        }

    def test_a_stale_cached_transcription_is_asked_again_in_its_run(
        self, tmp_path, monkeypatch
    ):
        """In a fresh state directory, as every scratch run starts from, a
        cached transcription that a stricter check now fails is rejected
        and asked again in the same run (#230): the run ends as one
        without the cache does, with one transcription asked of the
        engine in each."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "scan.png").write_bytes(b"not really a png")
        cache = tmp_path / "cache"
        CachedRun(
            folder,
            tmp_path / "baseline",
            engine=Engine(reading(NOTICE)),
            cache_dir=cache,
            log=quiet,
        ).run()
        stricter_check(monkeypatch)
        found = {}
        for name, cache_dir in (("cache", cache), ("no-cache", None)):
            engine = Engine(reading(READ_AGAIN))
            orchestrator = CachedRun(
                folder,
                tmp_path / name,
                engine=engine,
                cache_dir=cache_dir,
                log=quiet,
            )
            code = orchestrator.run()
            (doc,) = orchestrator.documents()
            found[name] = (
                code,
                doc.state.status,
                json.loads(doc.transcription.read_text())["text"],
                transcriptions(engine),
            )
            if cache_dir is not None:
                transcribe = [
                    (event["event"], event.get("reason"))
                    for event in this_run(
                        orchestrator, "cache-hit", "cache-miss", "cache-reject"
                    )
                    if event["role"] == "transcribe"
                ]
                assert [event for event, _ in transcribe] == [
                    "cache-hit",
                    "cache-reject",
                    "cache-miss",
                ]
                assert "reading-service-notice" in transcribe[1][1]
                assert transcribe[2][1] == "rejected"
        assert (
            found["cache"]
            == found["no-cache"]
            == (
                2,
                AWAITING_EQUIVALENCES,
                READ_AGAIN,
                1,
            )
        )

    def test_a_text_file_that_now_fails_the_check_fails_its_document(
        self, tmp_path, monkeypatch
    ):
        """A text file's transcription is checked again when it is reused,
        as a vision transcription is: one a stricter check fails is set
        aside and made again from the file, which fails it too (#230)."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "letter.txt").write_text(NOTICE)

        def run():
            orchestrator = CachedRun(
                folder,
                tmp_path / "state",
                engine=Engine(pipeline_answers),
                log=quiet,
            )
            code = orchestrator.run()
            (doc,) = orchestrator.documents()
            return orchestrator, code, doc

        _, code, _ = run()
        assert code == 2
        stricter_check(monkeypatch)
        again, code, doc = run()
        assert code == 1
        assert doc.state.status == FAILED
        assert "reading-service-notice" in doc.state.error
        (aside,) = this_run(again, "set-aside")
        assert aside["stage"] == "transcription"
        assert not doc.transcription.exists()
        kept = json.loads((doc.dir / aside["kept"]).read_text())
        assert kept["text"] == NOTICE
        # nothing is recorded for the view set aside
        record = json.loads(doc.derivations.path.read_text())
        assert "transcription.json" not in record["derived"]
        assert "transcription.json" not in record["content"]

    def test_a_correction_is_not_checked_again(self, tmp_path, monkeypatch):
        """A curator's correction is the transcription as written, and is
        never checked: the view made from it stands (#230)."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "scan.png").write_bytes(b"not really a png")

        def run():
            orchestrator = CachedRun(
                folder,
                tmp_path / "state",
                engine=Engine(pipeline_answers),
                log=quiet,
            )
            orchestrator.run()
            (doc,) = orchestrator.documents()
            return orchestrator, doc

        _, doc = run()
        doc.correction.write_text(NOTICE)
        run()
        stricter_check(monkeypatch)
        again, doc = run()
        assert this_run(again, "set-aside") == []
        assert "transcription" in {
            event["stage"] for event in this_run(again, "reused")
        }
        assert json.loads(doc.transcription.read_text())["text"] == NOTICE

    def test_an_engine_that_cannot_start_fails_its_stage(self, tmp_path):
        """The engine starts inside the stage that needs it, so one that
        cannot start fails that stage, with its events (#225)."""

        class NoCli(CachedRun):
            def _engine(self, role):
                raise RuntimeError(
                    "the Claude Code CLI ('claude') is not installed"
                )

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "letter.txt").write_text(LETTER)
        orchestrator = NoCli(
            folder,
            tmp_path / "state",
            engine=None,
            cache_dir=tmp_path / "cache",
            log=quiet,
        )
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert "not installed" in doc.state.error
        assert [
            (event["event"], event["stage"])
            for event in this_run(orchestrator, "started", "failed")
        ] == [
            ("started", "transcription"),
            ("started", "extraction"),
            ("failed", "extraction"),
        ]
        extraction = run_state(orchestrator.rundir)[(doc.sha1, "extraction")]
        assert extraction.status == FAILED
        assert "not installed" in extraction.reason

    def test_a_run_whose_cache_is_held_stops_before_any_call(self, tmp_path):
        """A run whose cache another process holds is refused when it
        first opens the cache, and stops there, as a pause does: no
        document fails for it, and the engine is never asked (#236)."""
        folder = folder_of_three(tmp_path)
        cache = tmp_path / "cache"
        engine = Engine(pipeline_answers)
        orchestrator = CachedRun(
            folder, tmp_path / "state", engine=engine, cache_dir=cache,
            log=quiet,
        )  # fmt: skip
        with (
            another_process(cache) as holder,
            pytest.raises(CacheInUse) as refused,
        ):
            orchestrator.run()
        assert f"process {holder.pid} " in str(refused.value)
        assert engine.calls == []
        assert this_run(orchestrator, "failed") == []
        assert FAILED not in {
            doc.state.status for doc in orchestrator.documents()
        }
        # once the other process has ended, the run goes on
        assert orchestrator.run() == 2
        assert engine.calls


class TestRoleClient:
    """The helper a tool that adopts answers for a run's calls takes its
    client from (#104): its keys are the ones the run's calls are served
    under."""

    def test_it_keys_a_call_as_the_run_does(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        scan = folder / "scan.png"
        scan.write_bytes(b"not really a png")
        options = {
            "engine": Engine(pipeline_answers),
            "cache_dir": tmp_path / "cache",
            "epochs": {"transcribe": 2},
            "log": quiet,
        }
        orchestrator = CachedRun(folder, tmp_path / "state", **options)
        orchestrator.run()
        (miss,) = [
            event
            for event in events(orchestrator.rundir, "cache-miss")
            if event["role"] == "transcribe"
        ]
        seeder = CachedRun(folder, tmp_path / "state", **options)
        key = seeder.role_client("transcribe").complete_key(
            TRANSCRIPTION_PROMPT, [scan]
        )
        assert key == miss["key"]
        # the role's epoch is keyed
        other = CachedRun(
            folder, tmp_path / "state", **(options | {"epochs": {}})
        )
        assert (
            other.role_client("transcribe").complete_key(
                TRANSCRIPTION_PROMPT, [scan]
            )
            != miss["key"]
        )

    def test_without_a_cache_it_is_the_engine(self, tmp_path):
        engine = Engine()
        orchestrator = CachedRun(
            tmp_path, tmp_path / "state", engine=engine, log=quiet
        )
        assert orchestrator.role_client("extract") is engine


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


YEARS = {"Klara Löbig": 1917, "Gustav Löbig": 1919}


class SessionModel(Engine):
    """A conversational model whose chunk answers depend on its session:
    it names each person by the id its own manifest answer in that
    session gave, as a real model does from its context. `orders` gives
    the order of the people in each manifest it is asked for; chunks for
    a person in `truncate` come back without their sentinel."""

    def __init__(self, orders, truncate=()):
        super().__init__()
        self.orders = list(orders)
        self.truncate = set(truncate)
        self.manifests: dict[str, dict[str, str]] = {}

    def open_conversation(self, prompt):
        self._log(OPEN, prompt)
        order = self.orders.pop(0)
        lines = {"Klara Löbig": 1, "Gustav Löbig": 2}
        entities = [
            {"id": f"e{n}", "label": label, "kind": "human",
             "mention_lines": [lines[label]]}
            for n, label in enumerate(order, 1)
        ]  # fmt: skip
        with self._lock:
            session = f"session-{len(self.manifests) + 1}"
            self.manifests[session] = {e["label"]: e["id"] for e in entities}
        return json.dumps({"entities": entities}), session

    def fork(self, handle, prompt):
        ids = self.manifests[handle]  # a KeyError is a session never held
        self._log(FORK, prompt)
        label = re.search(r"^- e\d+: ([^;]+);", prompt, re.M).group(1)
        claim = {
            "subject": {"kind": "local", "local_id": ids[label],
                        "label": label},
            "property": "date_of_birth",
            "value": {"type": "time", "year": YEARS[label]},
            "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
        }  # fmt: skip
        if label in self.truncate:
            return json.dumps(claim)
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


UNITS = {
    "units": [
        {"id": "u1", "label": "Klara", "first_line": 1, "last_line": 1},
        {"id": "u2", "label": "Gustav", "first_line": 2, "last_line": 2},
    ]
}


def segmenting(manifest_usable: bool, units: dict = UNITS):
    """The scripted model for the same document, with the manifest refused
    unless `manifest_usable`, the segmentation answered with `units`, and
    each unit's chunk answered with its person's birth."""

    def answer(kind, prompt, opening):
        if "manifest of EVERY distinct entity" in prompt:
            if manifest_usable:
                return extractor_answers()(kind, prompt, opening)
            return "no manifest here"
        if "segment a document" in prompt:
            return json.dumps(units)
        name, year = (
            ("Klara Löbig", 1917)
            if "unit u1" in prompt
            else ("Gustav Löbig", 1919)
        )
        claim = {
            "subject": {"kind": "local", "local_id": "p1", "label": name},
            "property": "date_of_birth",
            "value": {"type": "time", "year": year},
            "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
        }
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

    def test_a_refused_manifest_replays_as_it_went(self, tmp_path):
        """The manifest's two attempts are keyed apart, and a refused
        answer is kept, not rejected (#225): a later run is answered
        attempt by attempt from the cache and takes the same path."""
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
        assert list((tmp_path / "cache").glob("*/*.rejected-*")) == []
        again, replayed = extract(tmp_path, Refusing(extractor_answers()))
        assert not again.used_fallback_manifest
        assert replayed.claims == batch.claims

    def test_a_fallback_replays_as_it_went(self, tmp_path):
        """A manifest refused in both attempts falls back to segmentation.
        A later run is answered from the cache attempt by attempt, whatever
        the engine would answer now, and gives the same batch (#225)."""
        first, batch = extract(tmp_path, Engine(segmenting(False)))
        assert first.used_fallback_manifest
        assert len(batch.claims) == 2
        for manifest_usable in (False, True):
            engine = Engine(segmenting(manifest_usable))
            again, replayed = extract(tmp_path, engine)
            assert again.used_fallback_manifest
            assert engine.calls == []
            assert replayed.claims == batch.claims

    def test_a_replay_with_other_ids_does_not_mix_them(self, tmp_path):
        """A fork misses behind a cached manifest (here a chunk cut off in
        the last run), and the replay of the manifest numbers the people
        the other way round. The extraction starts over from the replay's
        manifest, so every claim keeps its person, and a later run gives
        the same batch from the cache alone (#225)."""
        klara, gustav = "Klara Löbig", "Gustav Löbig"

        def facts(batch):
            return sorted(
                (claim.subject.label, claim.value.year)
                for claim in batch.claims
            )

        extractor, _ = extract(
            tmp_path, SessionModel([[klara, gustav]], truncate={gustav})
        )
        assert extractor.failed_chunks == ["c002"]
        engine = SessionModel([[gustav, klara]])
        run = RunDir(tmp_path / "run")
        extractor, batch = extract(tmp_path, engine, run=run)
        assert extractor.failed_chunks == []
        assert facts(batch) == [(gustav, 1919), (klara, 1917)]
        # one replay, which answered differently; then both chunks again,
        # forked from the replay's session
        assert kinds(engine) == [OPEN, FORK, FORK]
        (replay,) = events(run, "cache-replay")
        assert replay["matches"] is False
        _, again = extract(tmp_path, Refusing())
        assert facts(again) == facts(batch)

    def test_a_failing_replay_is_asked_once_per_pass(self, tmp_path):
        """Every chunk's fork misses behind a cached manifest, and the
        engine fails the replay its session needs. The engine is asked
        once, not once per chunk: each chunk drops to inline context,
        here answered from the cache, and no fork is a miss (#236)."""

        class NoForks(Engine):
            def fork(self, handle, prompt):
                raise RuntimeError("no fork support")

        class Down(Engine):
            def open_conversation(self, prompt):
                self._log(OPEN, prompt)
                raise RuntimeError("engine down")

        first, batch = extract(tmp_path, NoForks(extractor_answers()))
        assert first.context_mode == "fork"
        engine = Down(extractor_answers())
        run = RunDir(tmp_path / "run")
        extractor, again = extract(tmp_path, engine, run=run)
        assert extractor.failed_chunks == []
        assert again.claims == batch.claims
        assert kinds(engine) == [OPEN]
        assert events(run, "cache-miss") == []

    @pytest.mark.parametrize("turn", ["manifest", "segmentation"])
    def test_a_stored_answer_a_stricter_parser_refuses_is_replayed(
        self, tmp_path, monkeypatch, turn
    ):
        """An entity manifest or segmentation the cache kept, which a
        parser made stricter since now refuses, is replayed, not rejected
        (#236, ADR-0032 §5): it spends its attempt, and the next attempt,
        never asked before, goes to the engine. A later run replays the
        same attempts and gives the same batch."""
        if turn == "manifest":
            answers = extractor_answers
            parse = "_parse_entity_manifest"

            def stricter_answer(kind, prompt, opening):
                text = answers()(kind, prompt, opening)
                if "manifest of EVERY distinct entity" not in prompt:
                    return text
                manifest = json.loads(text)
                for entity in manifest["entities"]:
                    entity["summary"] = f"{entity['label']}, born here"
                return json.dumps(manifest)

            def missing(parsed):
                return [e.id for e in parsed if not e.summary]

        else:
            answers = functools.partial(segmenting, False)
            parse = "_parse_segmentation"
            described = {
                "units": [
                    unit | {"context": f"the lines on {unit['label']}"}
                    for unit in UNITS["units"]
                ]
            }
            stricter_answer = segmenting(False, described)

            def missing(parsed):
                return [unit.id for unit in parsed.units if not unit.context]

        first, batch = extract(tmp_path, Engine(answers()))
        assert first.used_fallback_manifest is (turn == "segmentation")
        # the parser now asks for what the stored answer lacks
        original = getattr(ChunkedClaimExtractor, parse)

        def stricter(response, line_count):
            parsed, problems = original(response, line_count)
            if parsed is not None:
                problems += [
                    f"{name}: undescribed" for name in missing(parsed)
                ]
            return parsed, problems

        monkeypatch.setattr(
            ChunkedClaimExtractor, parse, staticmethod(stricter)
        )
        engine = Engine(stricter_answer)
        run = RunDir(tmp_path / "run")
        extractor, again = extract(tmp_path, engine, run=run)
        assert not extractor.used_fallback_segmentation
        assert extractor.used_fallback_manifest is (turn == "segmentation")
        assert again.claims == batch.claims
        # the stored answer was served and refused, never rejected: the
        # engine answered the next attempt, and the chunks made on it
        assert list((tmp_path / "cache").glob("*/*.rejected-*")) == []
        assert kinds(engine) == [OPEN, FORK, FORK]
        opened = [e for e in run.events() if e.get("call") == OPEN]
        assert [(e["event"], e.get("reason")) for e in opened][-2:] == [
            ("cache-hit", "key match"),
            ("cache-miss", "absent"),
        ]
        _, replayed = extract(tmp_path, Refusing())
        assert replayed.claims == batch.claims


# -- the single-call extractor through the cache --------------------------

KLARA = {
    "subject": {"kind": "local", "local_id": "e1", "label": "Klara Löbig"},
    "property": "date_of_birth",
    "value": {"type": "time", "year": 1917},
    "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
}
WHOLE = json.dumps(KLARA) + '\n{"done": true, "claims_emitted": 1}'


def answering(text):
    return lambda kind, prompt, opening: text


class TestTheSingleCallExtractor:
    """`ingest extract --strategy single` behind the cache (#225): an
    answer the extractor cannot fully use is rejected, so that the next
    run asks the engine again rather than being served it."""

    def extract(self, tmp_path, engine):
        transcription = Transcription(
            text="Klara Löbig b. 1917", engine="test", artifact_qid="Q23"
        )
        return LlmClaimExtractor(client(tmp_path, engine)).extract(
            transcription, "Q23"
        )

    def rejected(self, tmp_path) -> list[str]:
        return [
            json.loads(path.read_text())["rejected"]["reason"]
            for path in (tmp_path / "cache").glob("*/*.rejected-*.json")
        ]

    @pytest.mark.parametrize(
        "answer, reason",
        [
            ("not json at all", "no claim JSON"),
            ('{"claims": []}', "no valid claim"),
        ],
        ids=["unparsed", "no-claim"],
    )
    def test_an_unusable_answer_fails_and_is_rejected(
        self, tmp_path, answer, reason
    ):
        with pytest.raises(ValueError):
            self.extract(tmp_path, Engine(answering(answer)))
        (rejected,) = self.rejected(tmp_path)
        assert reason in rejected
        engine = Engine(answering(WHOLE))
        assert len(self.extract(tmp_path, engine).claims) == 1
        assert kinds(engine) == [COMPLETE]

    def test_an_answer_cut_off_is_used_once(self, tmp_path):
        batch = self.extract(tmp_path, Engine(answering(json.dumps(KLARA))))
        assert len(batch.claims) == 1
        (rejected,) = self.rejected(tmp_path)
        assert "sentinel" in rejected
        engine = Engine(answering(WHOLE))
        self.extract(tmp_path, engine)
        assert kinds(engine) == [COMPLETE]

    @pytest.mark.parametrize(
        "answer, reason",
        [
            ("not json at all", "no claim JSON"),
            ('{"claims": []}', "no valid claim"),
            (json.dumps(KLARA), "no terminal sentinel"),
        ],
        ids=["unparsed", "no-claim", "no-sentinel"],
    )
    def test_a_kept_answer_it_rejects_is_asked_for_once_more(
        self, tmp_path, answer, reason
    ):
        """An answer the cache kept, which the extractor now rejects (as
        after a stricter parser), is asked for once more in the same run,
        and the engine's answer is used in its place (#230)."""
        transcription = Transcription(
            text="Klara Löbig b. 1917", engine="test", artifact_qid="Q23"
        )
        # an earlier extractor kept the answer: it rejected nothing
        lenient = client(tmp_path, Engine(answering(answer)))
        lenient.reject = lambda response, why: False
        with contextlib.suppress(ValueError):
            LlmClaimExtractor(lenient).extract(transcription, "Q23")
        assert self.rejected(tmp_path) == []
        engine = Engine(answering(WHOLE))
        batch = self.extract(tmp_path, engine)
        assert len(batch.claims) == 1
        assert kinds(engine) == [COMPLETE]
        (rejected,) = self.rejected(tmp_path)
        assert reason in rejected

    def test_a_kept_answer_is_asked_for_once_more_at_most(self):
        """However often a client answers with a kept answer, the
        extractor asks once more, then fails as without a cache."""

        class AlwaysKept:
            model_identifier = "scripted/model-a"
            asked = 0

            def complete(self, prompt, image_paths=()):
                self.asked += 1
                return "not json at all"

            def reject(self, response, reason):
                return True

        engine = AlwaysKept()
        transcription = Transcription(
            text="Klara Löbig b. 1917", engine="test", artifact_qid="Q23"
        )
        with pytest.raises(ValueError, match="no claim JSON"):
            LlmClaimExtractor(engine).extract(transcription, "Q23")
        assert engine.asked == 2

    def test_a_whole_answer_is_served_again(self, tmp_path):
        first = self.extract(tmp_path, Engine(answering(WHOLE)))
        assert self.rejected(tmp_path) == []
        assert self.extract(tmp_path, Refusing()).claims == first.claims

    def test_ingest_extract_calls_through_its_cache(
        self, tmp_path, monkeypatch
    ):
        """The command's --cache wiring: its second run is answered from
        the cache, with an engine that must not be asked."""
        from yizkr.cli import build_parser

        transcription = tmp_path / "transcription.json"
        transcription.write_text(
            Transcription(
                text="Klara Löbig b. 1917", engine="test", artifact_qid="Q23"
            ).model_dump_json()
        )
        engines = iter([Engine(answering(WHOLE)), Refusing()])
        monkeypatch.setattr(llm, "make_client", lambda *a, **k: next(engines))
        batches = []
        for n in range(2):
            out = tmp_path / f"batch-{n}.json"
            args = build_parser().parse_args(
                [
                    "ingest",
                    "extract",
                    "--transcription",
                    str(transcription),
                    "--artifact",
                    "Q23",
                    "--engine",
                    "llm",
                    "--strategy",
                    "single",
                    "--cache",
                    str(tmp_path / "cache"),
                    "--out",
                    str(out),
                ]
            )
            assert args.func(args) == 0
            batches.append(ClaimBatch.model_validate_json(out.read_text()))
        assert batches[1].claims == batches[0].claims
        assert len(live_entries(tmp_path)) == 1


def test_the_cache_root_holds_only_entries(tmp_path):
    """Every file under the root is its lock file, an entry or an entry set
    aside."""
    caching = client(tmp_path, Engine())
    response = caching.complete("Read it.")
    caching.reject(response, "why")
    caching.complete("Read it.")
    shard = response.key[:2]
    names = sorted(
        path.name for path in (tmp_path / "cache" / shard).rglob("*")
    )
    assert all(
        re.fullmatch(rf"{response.key}(\.rejected-[\w-]+)?\.json", name)
        for name in names
    ), names
    assert sorted(os.listdir(tmp_path / "cache")) == [LOCK_FILE, shard]
