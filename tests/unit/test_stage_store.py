"""Offline tests for the stage store, the stage-version registry and the
run directory (#87).

Keys, reuse, early cutoff, the invalidation cascade and atomic writes are
exercised on small synthetic stages, so each property is shown on its
own; tests/unit/test_orchestrate.py and test_chunking.py cover the real
stages end to end.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil

import pytest

from yizkr.pipeline import stages as stages_module
from yizkr.pipeline import stageversions
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    NEEDS_ATTENTION,
    PENDING,
    REUSED,
    RunDir,
    fold_control,
    run_state,
)
from yizkr.pipeline.stages import (
    ADOPTED,
    KEY_COMPONENTS,
    KEY_MATCH,
    DeferredClient,
    DispatchStopped,
    Recompute,
    StageFailed,
    StageResult,
    StageScope,
    StageStore,
    changed_components,
    content_hash,
    doc_scope,
    model_spec,
    rebind_artifact,
    stage_key,
)
from yizkr.pipeline.stageversions import Method, Stage

# a toy registry: a routed first stage and two downstream stages
REGISTRY = {
    "read": Stage(
        version=1,
        methods={"vision": Method(1), "text-file": Method(1)},
        volatile=("engine",),
    ),
    "extract": Stage(version=1, volatile=("run.started_at",)),
    "review": Stage(version=1),
    "parse": Stage(version=1, stored=False),
}

DOC_A = hashlib.sha1(b"a letter").hexdigest()
DOC_B = hashlib.sha1(b"a census page").hexdigest()


def key(stage="extract", registry=REGISTRY, **overrides):
    parts = {
        "inputs": {"read": "0" * 64},
        "prompts": {"extract": "Extract the claims."},
        "manifest": "1" * 64,
        "model": model_spec("claude-code", model="model-a", effort="low"),
        "params": {"members": ["e1"]},
    }
    parts.update(overrides)
    return stage_key(stage, registry=registry, **parts)


class TestKeys:
    def test_every_component_changes_the_key(self):
        base = key()
        variants = {
            "inputs": key(inputs={"read": "2" * 64}),
            "version": key(
                registry=REGISTRY
                | {
                    "extract": dataclasses.replace(
                        REGISTRY["extract"], version=2
                    )
                }
            ),
            "prompts": key(prompts={"extract": "Extract every claim."}),
            "manifest": key(manifest="3" * 64),
            "model id": key(
                model=model_spec("claude-code", model="model-b", effort="low")
            ),
            "effort": key(
                model=model_spec("claude-code", model="model-a", effort="high")
            ),
            "engine": key(
                model=model_spec("anthropic", model="model-a", effort="low")
            ),
            "params": key(params={"members": ["e1", "e2"]}),
            "depends": key(depends=["parse"]),
        }
        digests = {name: variant.digest for name, variant in variants.items()}
        assert base.digest not in digests.values()
        assert len(set(digests.values())) == len(digests)

    def test_changed_components_names_what_forced_a_recompute(self):
        before = key().components
        after = key(prompts={"extract": "Extract every claim."}).components
        assert changed_components(before, after) == ["prompts"]
        assert changed_components(before, before) == []

    def test_keys_hold_only_their_components(self):
        """Nothing is keyed on QIDs, mtimes or timestamps: a key is a pure
        function of its declared components."""
        first, second = key(), key()
        assert first.digest == second.digest
        assert set(first.components) <= {"stage", *KEY_COMPONENTS}
        assert not re.search(r"\bQ[1-9]\d*\b", first.components_json)

    def test_model_falls_back_to_the_configured_model(self, monkeypatch):
        monkeypatch.setenv("YIZKR_LLM_MODEL", "model-from-env")
        assert model_spec("claude-code")["model"] == "model-from-env"
        monkeypatch.delenv("YIZKR_LLM_MODEL")
        assert model_spec("claude-code")["model"] == "engine default"

    def test_a_routed_stage_needs_its_method(self):
        with pytest.raises(KeyError):
            stage_key("read", inputs={"media": DOC_A}, registry=REGISTRY)
        with pytest.raises(KeyError):
            stage_key(
                "read",
                method="htr",
                inputs={"media": DOC_A},
                registry=REGISTRY,
            )


class TestRouteDependentKeys:
    """A new or changed method recomputes only the documents routed to
    it: a key holds the version of its own method alone."""

    def read_key(self, method, media, registry):
        return stage_key(
            "read", method=method, inputs={"media": media}, registry=registry
        )

    def test_adding_a_method_leaves_other_documents_keys(self):
        widened = dict(REGISTRY)
        widened["read"] = dataclasses.replace(
            REGISTRY["read"],
            methods=dict(REGISTRY["read"].methods) | {"text-layer": Method(1)},
        )
        for method, media in (("vision", DOC_A), ("text-file", DOC_B)):
            assert (
                self.read_key(method, media, widened).digest
                == self.read_key(method, media, REGISTRY).digest
            )

    def test_changing_a_method_rekeys_only_its_documents(self):
        bumped = dict(REGISTRY)
        bumped["read"] = dataclasses.replace(
            REGISTRY["read"],
            methods=dict(REGISTRY["read"].methods) | {"vision": Method(2)},
        )
        assert (
            self.read_key("vision", DOC_A, bumped).digest
            != self.read_key("vision", DOC_A, REGISTRY).digest
        )
        assert (
            self.read_key("text-file", DOC_B, bumped).digest
            == self.read_key("text-file", DOC_B, REGISTRY).digest
        )

    def test_the_real_transcription_route(self, tmp_path):
        from yizkr.pipeline.orchestrate import route_transcription

        correction = tmp_path / "transcription.corrected.txt"
        assert route_transcription(tmp_path / "scan.pdf", correction) == (
            "vision"
        )
        assert route_transcription(tmp_path / "notes.txt") == "text-file"
        assert route_transcription(tmp_path / "deck.pptx") == "slide-text"
        correction.write_text("a curator's reading")
        assert route_transcription(tmp_path / "scan.pdf", correction) == (
            "correction"
        )


class TestContentHash:
    def test_volatile_fields_are_left_out(self):
        """Early cutoff: an identical recompute differs only in volatile
        fields, so its content hash, and every downstream key, stands."""
        first = {"claims": [1, 2], "run": {"started_at": "2026-01-01"}}
        second = {"claims": [1, 2], "run": {"started_at": "2026-02-02"}}
        volatile = ("run.started_at",)
        assert content_hash(first, volatile=volatile) == content_hash(
            second, volatile=volatile
        )
        assert content_hash(first) != content_hash(second)
        changed = {"claims": [1, 3], "run": {"started_at": "2026-01-01"}}
        assert content_hash(changed, volatile=volatile) != content_hash(
            first, volatile=volatile
        )

    def test_volatile_paths_fan_out_over_lists(self):
        value = {"items": [{"at": 1, "v": "x"}, {"at": 2, "v": "x"}]}
        assert content_hash(value, volatile=("items.at",)) == content_hash(
            {"items": [{"v": "x"}, {"v": "x"}]}
        )

    def test_the_artifact_qid_is_abstracted(self):
        def batch(qid):
            return {"source": {"artifact": {"kind": "qid", "qid": qid}}}

        assert content_hash(batch("Q23"), artifact_qid="Q23") == content_hash(
            batch("Q57"), artifact_qid="Q57"
        )
        assert rebind_artifact(batch("Q23"), "Q23", "Q57") == batch("Q57")
        # other QIDs are left alone
        other = {"kind": "qid", "qid": "Q9"}
        assert rebind_artifact(other, "Q23", "Q57") == other


def run_stage(scope, stage, key, output, log, **kwargs):
    def compute():
        log.append(stage)
        return StageResult(output)

    return scope.run(stage, key, compute, **kwargs)


class TestReuseAndCascade:
    """A toy three-stage pipeline per document: read -> extract ->
    review, each keyed by its upstream output's content hash."""

    def pipeline(self, store, sha1, text, log, *, engine="engine-1", run=None):
        scope = StageScope(store, doc_scope(sha1), events=run)
        read = run_stage(
            scope,
            "read",
            stage_key(
                "read",
                method="text-file",
                inputs={"media": sha1},
                params={"text": text},
                registry=REGISTRY,
            ),
            {"text": text, "engine": engine},
            log,
            volatile=("engine",),
        )
        extract = run_stage(
            scope,
            "extract",
            stage_key(
                "extract",
                inputs={"read": read.record.output_hash},
                registry=REGISTRY,
            ),
            {"claims": text.split()},
            log,
            volatile=(),
        )
        run_stage(
            scope,
            "review",
            stage_key(
                "review",
                inputs={"extract": extract.record.output_hash},
                registry=REGISTRY,
            ),
            {"findings": []},
            log,
            volatile=(),
        )

    def test_rerun_reuses_everything(self, tmp_path):
        store, log = StageStore(tmp_path), []
        self.pipeline(store, DOC_A, "Leopold 1914", log)
        self.pipeline(store, DOC_A, "Leopold 1914", log)
        assert log == ["read", "extract", "review"]

    def test_a_correction_invalidates_exactly_its_downstream(self, tmp_path):
        store, log = StageStore(tmp_path), []
        self.pipeline(store, DOC_A, "Leopold 1914", log)
        self.pipeline(store, DOC_B, "Nicholas 1950", log)
        log.clear()
        self.pipeline(
            store, DOC_A, "Leopold 29 May 1914", log
        )  # corrected reading
        self.pipeline(store, DOC_B, "Nicholas 1950", log)
        assert log == ["read", "extract", "review"]  # document A only

    def test_identical_recompute_stops_the_cascade(self, tmp_path):
        """Early cutoff: the first stage recomputes under a new engine and
        says the same thing, so nothing downstream is recomputed."""
        store, log = StageStore(tmp_path), []
        self.pipeline(store, DOC_A, "Leopold 1914", log)
        log.clear()
        # recompute the first stage (a forced recompute stands in for a
        # new method version); its output differs only in a volatile field
        scope = StageScope(store, doc_scope(DOC_A), recompute=["read"])
        read_key = stage_key(
            "read",
            method="text-file",
            inputs={"media": DOC_A},
            params={"text": "Leopold 1914"},
            registry=REGISTRY,
        )
        run_stage(
            scope,
            "read",
            read_key,
            {"text": "Leopold 1914", "engine": "engine-2"},
            log,
            volatile=("engine",),
        )
        self.pipeline(store, DOC_A, "Leopold 1914", log, engine="engine-2")
        assert log == ["read"]
        record = store.get(doc_scope(DOC_A), "read", read_key)
        assert record.output()["engine"] == "engine-2"

    def test_events_record_every_reuse_and_store(self, tmp_path):
        store, run = StageStore(tmp_path / "stages"), RunDir(tmp_path / "run")
        self.pipeline(store, DOC_A, "Leopold 1914", [], run=run)
        self.pipeline(store, DOC_A, "Leopold 1914", [], run=run)
        events = [(e["event"], e["stage"]) for e in run.events()]
        assert events == [
            ("started", "read"),
            ("stored", "read"),
            ("started", "extract"),
            ("stored", "extract"),
            ("started", "review"),
            ("stored", "review"),
            ("reused", "read"),
            ("reused", "extract"),
            ("reused", "review"),
        ]
        assert all(e["subject"] == DOC_A for e in run.events())


class TestAtomicWrites:
    def test_interrupted_put_leaves_nothing_that_looks_complete(
        self, tmp_path, monkeypatch
    ):
        store = StageStore(tmp_path)
        scope = doc_scope(DOC_A)
        k = key()

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(stages_module.os, "rename", interrupted)
        with pytest.raises(KeyboardInterrupt):
            store.put(scope, "extract", k, StageResult({"claims": [1]}))
        monkeypatch.undo()
        assert store.get(scope, "extract", k) is None
        assert store.records(scope, "extract") == []
        # no staging debris either: the failed write cleaned up after itself
        assert list(store.stage_dir(scope, "extract").iterdir()) == []

    def test_debris_of_a_killed_writer_is_ignored(self, tmp_path):
        store = StageStore(tmp_path)
        scope = doc_scope(DOC_A)
        k = key()
        stage_dir = store.stage_dir(scope, "extract")
        # a writer killed mid-write: its staging directory, and even a
        # directory under the key with no record.json, are not records
        (stage_dir / ".staging-dead").mkdir(parents=True)
        (stage_dir / ".staging-dead" / "output.json").write_text("{")
        (stage_dir / k.digest).mkdir()
        (stage_dir / k.digest / "output.json").write_text('{"claims": [')
        assert store.get(scope, "extract", k) is None
        assert store.records(scope, "extract") == []

    def test_interrupted_computation_stores_nothing(self, tmp_path):
        store = StageStore(tmp_path)
        scope = StageScope(store, doc_scope(DOC_A))

        def compute():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            scope.run("extract", key(), compute, volatile=())
        assert store.records(doc_scope(DOC_A), "extract") == []

    def test_a_forced_recompute_replaces_in_one_step(self, tmp_path):
        store = StageStore(tmp_path)
        scope = doc_scope(DOC_A)
        k = key()
        store.put(scope, "extract", k, StageResult({"claims": [1]}))
        store.put(
            scope, "extract", k, StageResult({"claims": [2]}), replace=True
        )
        (record,) = store.records(scope, "extract")
        assert record.output() == {"claims": [2]}
        leftovers = [
            path.name
            for path in store.stage_dir(scope, "extract").iterdir()
            if path.name.startswith(".")
        ]
        assert leftovers == []

    def test_a_concurrent_first_record_stands(self, tmp_path):
        store = StageStore(tmp_path)
        scope = doc_scope(DOC_A)
        k = key()
        store.put(scope, "extract", k, StageResult({"claims": [1]}))
        second = store.put(scope, "extract", k, StageResult({"claims": [2]}))
        assert second.output() == {"claims": [1]}

    def test_raw_outputs_are_stored_beside_the_parsed_output(self, tmp_path):
        store = StageStore(tmp_path)
        record = store.put(
            doc_scope(DOC_A),
            "extract",
            key(),
            StageResult(
                {"claims": [1]},
                raw=["first answer", "second answer"],
                extras={"coverage.json": "{}"},
                attention=["fell back"],
                meta={"model_identifier": "model-a"},
            ),
            artifact_qid="Q23",
        )
        assert record.raw() == ["first answer", "second answer"]
        assert record.extra("coverage.json") == "{}"
        assert record.extra("missing.json") is None
        assert record.attention == ["fell back"]
        assert record.meta == {
            "model_identifier": "model-a",
            "artifact_qid": "Q23",
        }
        assert (record.path / "raw" / "0001.txt").read_text() == "first answer"


class TestFailures:
    def test_a_failure_is_logged_and_retried(self, tmp_path):
        store = StageStore(tmp_path)
        run = RunDir(tmp_path / "run")
        scope = StageScope(store, doc_scope(DOC_A), events=run)
        k = key()

        def broken():
            raise StageFailed("truncated", raw="half an answer", attempts=3)

        with pytest.raises(StageFailed):
            scope.run("extract", k, broken, volatile=())
        (failure,) = store.failures(doc_scope(DOC_A), "extract", k)
        assert failure["reason"] == "truncated"
        assert failure["raw"] == "half an answer"
        assert failure["attempts"] == 3
        assert store.get(doc_scope(DOC_A), "extract", k) is None

        outcome = scope.run(
            "extract", k, lambda: StageResult({"claims": []}), volatile=()
        )
        assert not outcome.reused
        # the reason survives the success that follows it
        assert store.failures(doc_scope(DOC_A), "extract", k) == [failure]
        events = [e["event"] for e in run.events()]
        assert events == ["started", "failed", "started", "stored"]

    def test_a_plain_exception_is_logged_with_its_type(self, tmp_path):
        store = StageStore(tmp_path)
        scope = StageScope(store, doc_scope(DOC_A))

        def broken():
            raise RuntimeError("engine down")

        with pytest.raises(RuntimeError):
            scope.run("extract", key(), broken, volatile=())
        (failure,) = store.failures(doc_scope(DOC_A), "extract")
        assert failure["reason"] == "RuntimeError: engine down"


class TestAdoption:
    def test_an_adopted_output_is_reused_visibly(self, tmp_path):
        """An output made elsewhere (an imported transcription) stands for
        the current key through a recorded equivalence, and is reported
        as adopted, never as a key match."""
        store = StageStore(tmp_path)
        run = RunDir(tmp_path / "run")
        scope = doc_scope(DOC_A)
        backfilled = stage_key(
            "read",
            method="vision",
            inputs={"media": DOC_A},
            params={"backfilled": True},
            registry=REGISTRY,
        )
        imported = store.put(
            scope, "read", backfilled, StageResult({"text": "Leopold 1914"})
        )
        current = stage_key(
            "read", method="vision", inputs={"media": DOC_A}, registry=REGISTRY
        )
        store.adopt(scope, "read", current, imported, "imported transcription")

        found, reason = store.lookup(scope, "read", current)
        assert reason == ADOPTED
        assert found.key == imported.key
        outcome = StageScope(store, scope, events=run).run(
            "read",
            current,
            lambda: pytest.fail("an adopted output is not recomputed"),
            volatile=(),
        )
        assert outcome.reused and outcome.reason == ADOPTED
        (event,) = run.events()
        assert event["reason"] == ADOPTED and event["record"] == imported.key
        assert store.adoption(scope, "read", current)["reason"] == (
            "imported transcription"
        )

    def test_a_version_bump_drops_the_equivalence(self, tmp_path):
        store = StageStore(tmp_path)
        scope = doc_scope(DOC_A)
        imported = store.put(
            scope,
            "read",
            stage_key(
                "read",
                method="vision",
                inputs={"media": DOC_A},
                params={"backfilled": True},
                registry=REGISTRY,
            ),
            StageResult({"text": "Leopold 1914"}),
        )
        current = stage_key(
            "read", method="vision", inputs={"media": DOC_A}, registry=REGISTRY
        )
        store.adopt(scope, "read", current, imported, "imported")
        bumped = dict(REGISTRY)
        bumped["read"] = dataclasses.replace(REGISTRY["read"], version=2)
        after_bump = stage_key(
            "read", method="vision", inputs={"media": DOC_A}, registry=bumped
        )
        assert store.lookup(scope, "read", after_bump) is None
        assert store.lookup(scope, "read", current)[1] == ADOPTED


class TestRecompute:
    def test_a_forced_stage_recomputes_once_per_run(self, tmp_path):
        store, log = StageStore(tmp_path), []
        k = key()
        StageScope(store, doc_scope(DOC_A)).run(
            "extract", k, lambda: StageResult({"n": 1}), volatile=()
        )
        forced = Recompute(["extract"])
        for _ in range(2):
            scope = StageScope(store, doc_scope(DOC_A), recompute=forced)
            outcome = run_stage(
                scope, "extract", k, {"n": 2}, log, volatile=()
            )
        assert log == ["extract"]
        assert outcome.reused and outcome.reason == KEY_MATCH
        (record,) = store.records(doc_scope(DOC_A), "extract")
        assert record.output() == {"n": 2}


class TestDeferredClient:
    def test_the_engine_starts_only_when_called(self):
        started = []

        class Engine:
            model_identifier = "engine-1"

            def complete(self, prompt, image_paths=()):
                return prompt.upper()

        def factory():
            started.append(True)
            return Engine()

        client = DeferredClient(factory)
        assert started == []
        assert client.complete("hello") == "HELLO"
        assert client.model_identifier == "engine-1"
        assert started == [True]


# -- the stage-version registry -----------------------------------------------


def _behaviour_v1(value):
    """Doubles."""
    return value * 2


def _behaviour_v1_commented(value):
    """Doubles, and says so at length."""
    # the comment and the docstring are not behaviour
    return value * 2


def _behaviour_v2(value):
    return value * 3


def _registry(source: str, version: int = 1) -> dict:
    return {"toy": Stage(version=version, sources=(f"{__name__}:{source}",))}


class TestStageVersions:
    def test_the_registry_matches_its_lock(self):
        """Fails when a stage's source changes without a version bump or a
        recorded functionally neutral entry. To fix it, bump the stage's
        version in yizkr.pipeline.stageversions and run `yizkr stages lock
        <stage>`, or run `yizkr stages lock <stage> --neutral '<why>'`."""
        assert stageversions.check_versions() == []

    def test_every_stored_stage_with_a_version_has_sources(self):
        for name, stage in stageversions.STAGES.items():
            assert stage.sources or stage.methods, name
            for method in stage.methods.values():
                assert method.sources, name

    def test_comments_and_docstrings_do_not_change_a_fingerprint(self):
        assert stageversions.normalize_source(
            "def f(x):\n    '''doc'''\n    # note\n    return x  # why\n"
        ) == stageversions.normalize_source("def f(x):\n    return x\n")
        first = stageversions.fingerprint([f"{__name__}:_behaviour_v1"])
        # the same body under another name is another source reference
        assert first != stageversions.fingerprint(
            [f"{__name__}:_behaviour_v1_commented"]
        )

    def test_a_source_change_needs_a_bump_or_a_neutral_entry(self):
        lock = stageversions.lock_versions(
            ["toy"], registry=_registry("_behaviour_v1"), lock={}
        )
        assert (
            stageversions.check_versions(_registry("_behaviour_v1"), lock)
            == []
        )
        # the source changes and the version does not: the check fails
        changed = _registry("_behaviour_v2")
        (problem,) = stageversions.check_versions(changed, lock)
        assert "sources changed" in problem
        with pytest.raises(ValueError, match="bump the version"):
            stageversions.lock_versions(["toy"], registry=changed, lock=lock)

        # recorded as functionally neutral, it passes
        neutral = stageversions.lock_versions(
            ["toy"],
            registry=changed,
            lock=json.loads(json.dumps(lock)),
            neutral="a refactor",
        )
        assert stageversions.check_versions(changed, neutral) == []
        assert neutral["toy"]["neutral"][0]["note"] == "a refactor"

        # a bump must be locked before the check passes, and clears the
        # neutral entries of the old version
        bumped = _registry("_behaviour_v2", version=2)
        (problem,) = stageversions.check_versions(bumped, neutral)
        assert "version 2 is declared" in problem
        relocked = stageversions.lock_versions(
            ["toy"], registry=bumped, lock=neutral
        )
        assert relocked["toy"] == {
            "version": 2,
            "fingerprint": stageversions.fingerprint(
                [f"{__name__}:_behaviour_v2"]
            ),
        }
        assert stageversions.check_versions(bumped, relocked) == []

    def test_undeclared_and_unlocked_entries_are_named(self):
        registry = _registry("_behaviour_v1")
        (problem,) = stageversions.check_versions(registry, {})
        assert "not in the lock" in problem
        lock = stageversions.lock_versions(["toy"], registry=registry, lock={})
        lock["gone"] = {"version": 1, "fingerprint": "x"}
        (problem,) = stageversions.check_versions(registry, lock)
        assert problem.startswith("gone: in the lock but not declared")

    def test_methods_are_versioned_separately(self):
        entries = stageversions.entries()
        assert "transcription:vision" in entries
        assert "transcription:text-file" in entries
        assert (
            entries["transcription:vision"][1]
            != entries["transcription:text-file"][1]
        )


# -- the run directory --------------------------------------------------------


class TestRunDir:
    def test_nothing_is_written_until_something_happens(self, tmp_path):
        run = RunDir(tmp_path / "run")
        assert run.events() == [] and run.manifest() == {}
        assert not (tmp_path / "run").exists()

    def test_a_torn_last_line_is_skipped(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("reused", stage="read")
        with open(tmp_path / "events.jsonl", "a") as handle:
            handle.write('{"event": "stor')  # a writer killed mid-line
        assert [event["event"] for event in run.events()] == ["reused"]

    def test_the_manifest_is_merged_and_the_run_id_kept(self, tmp_path):
        run = RunDir(tmp_path)
        first = run.start(store="var/stages")
        second = RunDir(tmp_path).start(documents=[])
        assert second["run_id"] == first["run_id"]
        assert second["store"] == "var/stages"

    def test_alerts_are_logged_and_announced(self, tmp_path):
        run = RunDir(tmp_path)
        run.alert(
            stage="transcription",
            check="tool-failure text",
            subject=DOC_A[:10],
            value=1,
            carried=True,
        )
        (alert,) = run.alerts()
        assert alert["status"] == "carried"
        assert run.events()[-1]["event"] == "alert"


class TestControl:
    def test_pause_resume_and_abort(self):
        state = fold_control(
            [
                {"action": "pause", "scope": "stage:critique"},
                {"action": "pause", "scope": "kind:media"},
                {"action": "resume", "scope": "kind:media"},
                {"action": "abort", "scope": "kind:gedcom"},
                {"action": "resume", "scope": "kind:gedcom"},
            ]
        )
        assert state.stopped(stage="critique") == "stage:critique paused"
        assert state.stopped(kind="media") is None
        # an abort holds for the rest of the invocation
        assert state.stopped(kind="gedcom") == "kind:gedcom aborted"
        assert state.stopped(stage="chunk", kind="text") is None

    def test_a_new_invocation_starts_clear(self, tmp_path):
        run = RunDir(tmp_path)
        run.request("abort", by="monitor", reason="futile")
        assert run.control().stopped() == "run aborted"
        RunDir(tmp_path).start()
        assert run.control().stopped() is None

    def test_a_request_names_one_slice(self, tmp_path):
        run = RunDir(tmp_path)
        with pytest.raises(ValueError):
            run.request("pause", stage="chunk", kind="media")
        with pytest.raises(ValueError):
            run.request("stop")

    def test_a_held_stage_is_not_dispatched(self, tmp_path):
        store, run = StageStore(tmp_path / "stages"), RunDir(tmp_path / "run")
        run.request("pause", stage="extract", by="anomaly check")
        scope = StageScope(store, doc_scope(DOC_A), events=run, kind="text")
        with pytest.raises(DispatchStopped):
            scope.run(
                "extract",
                key(),
                lambda: pytest.fail("a held stage is not computed"),
                volatile=(),
            )
        # reuse is not dispatch: a stored output is still served
        store.put(doc_scope(DOC_A), "extract", key(), StageResult({"n": 1}))
        assert scope.run("extract", key(), None, volatile=()).reused


class TestRunState:
    def plan(self, run, *stages_):
        run.update_manifest(
            documents=[
                {"sha1": DOC_A, "kind": "text", "stages": list(stages_)}
            ]
        )

    def test_planned_stages_start_pending(self, tmp_path):
        run, store = RunDir(tmp_path / "run"), StageStore(tmp_path / "stages")
        self.plan(run, "read", "extract")
        states = run_state(run, store)
        assert {s.status for s in states.values()} == {PENDING}

    def test_states_follow_events_and_the_store(self, tmp_path):
        run, store = RunDir(tmp_path / "run"), StageStore(tmp_path / "stages")
        self.plan(run, "read", "extract", "review")
        scope = StageScope(store, doc_scope(DOC_A), events=run)
        scope.run("read", key("read", method="vision"), lambda: StageResult(1))
        scope.run(
            "extract",
            key(),
            lambda: StageResult(2, attention=["fell back"]),
            volatile=(),
        )
        with pytest.raises(RuntimeError):
            scope.run("review", key("review"), _raise, volatile=())
        states = run_state(run, store)
        assert states[(DOC_A, "read", None)].status == DONE
        assert states[(DOC_A, "extract", None)].status == NEEDS_ATTENTION
        assert states[(DOC_A, "extract", None)].reason == "fell back"
        review = states[(DOC_A, "review", None)]
        assert review.status == FAILED and "boom" in review.reason

        # a second invocation reuses read and retries review
        again = StageScope(store, doc_scope(DOC_A), events=run)
        again.run("read", key("read", method="vision"), None)
        again.run("review", key("review"), lambda: StageResult(3), volatile=())
        states = run_state(run, store)
        assert states[(DOC_A, "read", None)].status == REUSED
        assert states[(DOC_A, "read", None)].reason == KEY_MATCH
        assert states[(DOC_A, "review", None)].status == DONE

    def test_the_store_has_the_last_word(self, tmp_path):
        run, store = RunDir(tmp_path / "run"), StageStore(tmp_path / "stages")
        self.plan(run, "extract")
        scope = StageScope(store, doc_scope(DOC_A), events=run)
        outcome = scope.run(
            "extract", key(), lambda: StageResult(1), volatile=()
        )
        # the output is removed from the store: the stage is pending again
        shutil.rmtree(outcome.record.path)
        state = run_state(run, store)[(DOC_A, "extract", None)]
        assert state.status == PENDING

    def test_items_replace_the_stage_placeholder(self, tmp_path):
        run, store = RunDir(tmp_path / "run"), StageStore(tmp_path / "stages")
        self.plan(run, "extract")
        scope = StageScope(store, doc_scope(DOC_A), events=run)
        for item, members in (("c001", ["e1"]), ("c002", ["e2"])):
            scope.run(
                "extract",
                key(params={"members": members}),
                lambda: StageResult(1),
                item=item,
                volatile=(),
            )
        states = run_state(run, store)
        assert set(states) == {
            (DOC_A, "extract", "c001"),
            (DOC_A, "extract", "c002"),
        }


def _raise():
    raise RuntimeError("boom")


def test_store_paths_reject_foreign_names(tmp_path):
    store = StageStore(tmp_path)
    with pytest.raises(ValueError):
        store.stage_dir("doc/../../etc", "read")
    with pytest.raises(ValueError):
        store.stage_dir(doc_scope(DOC_A), "../read")
    assert store.get(doc_scope(DOC_A), "read", "not-a-digest") is None


def test_a_directory_without_a_record_is_replaced(tmp_path):
    store = StageStore(tmp_path)
    k = key()
    debris = store.stage_dir(doc_scope(DOC_A), "extract") / k.digest
    debris.mkdir(parents=True)
    record = store.put(doc_scope(DOC_A), "extract", k, StageResult({"n": 1}))
    assert record.output() == {"n": 1}


def test_corpus_stages_have_a_scope_of_their_own(tmp_path):
    """Corpus-level outputs (an identity map, flags) sit beside the
    per-document ones, keyed the same way."""
    store = StageStore(tmp_path)
    run = RunDir(tmp_path / "run")
    scope = StageScope(store, "corpus", events=run)
    k = key(inputs={"manifests": ["a" * 64, "b" * 64]})
    scope.run("extract", k, lambda: StageResult({"clusters": 2}), volatile=())
    assert store.get("corpus", "extract", k).output() == {"clusters": 2}
    assert (tmp_path / "corpus" / "extract" / k.digest).is_dir()
    assert run.events()[-1]["subject"] == "corpus"
