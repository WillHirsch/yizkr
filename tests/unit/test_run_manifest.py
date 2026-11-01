"""Offline tests for the run manifest (#96): what run.json records about
what shapes a run, how an invocation that asks for other settings is
refused in a run that keeps its settings, the refusal of model aliases,
and `yizkr stages manifest`.

The engine is a fake that `yizkr.pipeline.llm.make_client` returns, and
the wiki steps are scripted, so every run here is offline. Documents and
names come from the fictional universe (docs/EXAMPLES.md)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import rundir
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    MODEL_ROLES,
    Orchestrator,
)
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.rundir import (
    MANIFEST_FORMAT,
    ModelAliasRefused,
    RunDir,
    RunSettingsConflict,
    code_version,
    describe_manifest,
    exclusions_record,
    model_alias,
    settings_conflicts,
)

LETTER = "Klara Löbig was born in 1917 in Flörburg."
# the bytes of a fictional scan, which the claims below cite by
# their SHA-1 (#102)
SCAN = ArtifactRef.from_bytes(b"fictional scan: a Loebig family document")
CLI = "9.9.9"
FULL_MODEL = "claude-opus-5-5"


def quiet(*args, **kwargs):
    pass


def a_batch() -> ClaimBatch:
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
                        "label": "Klara Löbig",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1917},
                    "source": {"artifact": SCAN.model_dump()},
                }
            ],
            "entities": [],
        }
    )


class FakeEngine:
    """What a run's manifest reads off an engine client, and a critic's
    answer for every call."""

    engine = "claude-code"
    SYSTEM_PROMPT = "You are running inside a fictional pipeline."
    system_prompt = SYSTEM_PROMPT

    def __init__(self, *, effort=None, model=None, config="c1"):
        self.effort = effort
        self.model = model or os.environ.get("YIZKR_LLM_MODEL")
        self.cli_version = CLI
        self.config_hash = f"{config}-{self.model}-{effort}"
        self.model_identifier = f"claude-code/{CLI}:{self.model}"
        self.prompts: list[str] = []
        self.closed = False

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return '{"done": true, "findings_emitted": 0}'

    def close(self):
        self.closed = True


@pytest.fixture
def factory(monkeypatch):
    """Replace the client factory; returns the calls it saw and a setting
    that changes every later client's configuration hash."""
    from yizkr.pipeline import llm

    made: list[dict] = []
    setting = {"config": "c1"}
    engines: list[FakeEngine] = []

    def make_client(engine, *, effort=None, model=None, **kwargs):
        made.append({"engine": engine, "effort": effort, "model": model})
        made[-1].update(kwargs)
        client = FakeEngine(effort=effort, model=model, **setting)
        engines.append(client)
        return client

    monkeypatch.setattr(llm, "make_client", make_client)
    monkeypatch.setenv("YIZKR_LLM_MODEL", FULL_MODEL)
    return made, setting, engines


class Run(Orchestrator):
    """The wiki steps scripted; transcription (a text file) and the critic
    run for real, and extraction starts the role's engine and returns a
    fixed batch."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("exclusions", ExclusionList())
        kwargs.setdefault("llm", "claude-code")
        kwargs.setdefault("log", quiet)
        super().__init__(*args, **kwargs)

    def _step_register(self, doc):
        return "Q23"

    def _step_extract(self, doc, transcription):
        def extract():
            self._llm("extract", doc)  # the engine starts in its stage
            return a_batch()

        return self._run_stage(doc, "extraction", extract)

    def _step_resolve(self, doc, batch):
        return batch

    def _step_export_review(self, doc, batch):
        return export_review_csv(batch, doc.review)


def a_folder(tmp_path, *names: str) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    for name in names or ("letter.txt",):
        (folder / name).write_text(LETTER)
    return folder


def manifest_text(orchestrator) -> str:
    return (orchestrator.state_dir / "run.json").read_text()


# -- the manifest an ingest run writes ----------------------------------------


class TestIngestRunManifest:
    def test_it_records_what_shapes_the_run(self, tmp_path, factory):
        folder = a_folder(tmp_path)
        exclusions = ExclusionList.parse("# none of these\n*.tmp\n")
        orchestrator = Run(
            folder,
            tmp_path / "state",
            exclusions=exclusions,
            extract_model="claude-sonnet-5",
            epochs={"extract": 2},
        )
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS

        manifest = orchestrator.rundir.manifest()
        assert manifest["format"] == MANIFEST_FORMAT
        assert manifest["mode"] == "ingest run"
        assert manifest["project_stage"] == "development"
        assert manifest["engine"] == {
            "name": "claude-code",
            "cli_version": CLI,
        }
        assert manifest["cache"] is None
        assert manifest["corpus"] == {
            "folder": os.path.abspath(folder),
            "exclusions": exclusions_record(exclusions),
        }
        assert manifest["code"] == code_version()
        assert set(manifest["roles"]) == set(MODEL_ROLES)
        # configured: the extraction model, else $YIZKR_LLM_MODEL
        transcribe, extract, critique = (
            manifest["roles"][role] for role in MODEL_ROLES
        )
        assert (extract["model"], extract["effort"]) == (
            "claude-sonnet-5",
            "low",
        )
        assert (critique["model"], critique["effort"]) == (FULL_MODEL, "high")
        assert extract["epoch"] == 2 and critique["epoch"] == 0
        assert not any(role["refresh"] for role in manifest["roles"].values())
        # a text file's transcription calls no engine: its client never
        # started, while extraction's and the critic's did
        assert transcribe["client"] is None
        assert extract["client"] == {
            "model": "claude-sonnet-5",
            "effort": "low",
            "config_hash": "c1-claude-sonnet-5-low",
            "model_identifier": f"claude-code/{CLI}:claude-sonnet-5",
        }
        assert critique["client"]["config_hash"] == f"c1-{FULL_MODEL}-high"
        # the run's configuration block keeps its fields and gains more
        assert manifest["config"]["extract_model"] == "claude-sonnet-5"
        assert manifest["config"]["serial"] is False
        # one invocation, finished
        (invocation,) = manifest["invocations"]
        assert invocation["invocation"] == orchestrator.rundir.invocation
        assert invocation["outcome"] == "finished"
        assert "reason" not in invocation
        assert manifest["started_at"] == invocation["started_at"]
        assert manifest["ended_at"] == invocation["ended_at"]
        assert invocation["started_at"] <= invocation["ended_at"]

    def test_it_records_no_prompt_and_no_document_content(
        self, tmp_path, factory
    ):
        _made, _setting, engines = factory
        orchestrator = Run(a_folder(tmp_path), tmp_path / "state")
        orchestrator.run()
        text = manifest_text(orchestrator)
        prompts = [prompt for engine in engines for prompt in engine.prompts]
        assert prompts  # the critic was asked
        for secret in (LETTER, "Klara", "Flörburg", FakeEngine.SYSTEM_PROMPT):
            assert secret not in text
        assert not any(prompt[:40] in text for prompt in prompts)

    def test_a_later_invocation_records_its_own_settings(
        self, tmp_path, factory
    ):
        """`ingest run` may change its settings between runs over one
        state directory: the manifest describes the latest."""
        folder = a_folder(tmp_path)
        first = Run(folder, tmp_path / "state", extract_model="claude-5-x")
        first.run()
        before = first.rundir.manifest()
        second = Run(
            folder,
            tmp_path / "state",
            extract_model="claude-sonnet-5",
            refresh=["critique"],
        )
        second.run()
        after = second.rundir.manifest()
        assert after["run_id"] == before["run_id"]
        assert after["started_at"] == before["started_at"]
        assert after["roles"]["extract"]["model"] == "claude-sonnet-5"
        assert after["roles"]["critique"]["refresh"] is True
        # Changed generation settings start extraction again; the latest
        # invocation records the client it actually used.
        assert after["roles"]["extract"]["client"]["model"] == (
            "claude-sonnet-5"
        )
        assert before["roles"]["extract"]["client"]["model"] == "claude-5-x"
        assert [i["outcome"] for i in after["invocations"]] == [
            "finished",
            "finished",
        ]
        assert after["invocations"][0] == before["invocations"][0]

    def test_the_state_directory_holds_no_other_run_file(
        self, tmp_path, factory
    ):
        orchestrator = Run(a_folder(tmp_path), tmp_path / "state")
        orchestrator.run()
        # alerts.jsonl, the anomaly checks' run file (#196), is there once
        # one raises an alert, as the short letter's language check does
        assert sorted(
            path.name
            for path in orchestrator.state_dir.iterdir()
            if path.is_file() and path.name != "alerts.jsonl"
        ) == ["control.jsonl", "events.jsonl", "run.json"]


class TestOutcomes:
    def outcome(self, orchestrator) -> dict:
        return orchestrator.rundir.manifest()["invocations"][-1]

    def test_a_control_request_stops_it(self, tmp_path, factory):
        class Aborting(Run):
            def _derive(self, doc):
                self.rundir.request("abort", by="test", reason="futile")
                return super()._derive(doc)

        orchestrator = Aborting(
            a_folder(tmp_path, "a-letter.txt", "b-note.txt"),
            tmp_path / "state",
        )
        orchestrator.run()
        entry = self.outcome(orchestrator)
        assert entry["outcome"] == "stopped"
        assert entry["reason"] == "run aborted"
        events = [event["event"] for event in orchestrator.rundir.events()]
        assert events[0] == "run-start" and events[-1] == "run-end"

    @pytest.mark.parametrize("action", ["abort", "pause"])
    def test_a_stop_during_the_last_document_stops_it(
        self, tmp_path, factory, action
    ):
        """A request for the whole run that holds a stage of the last
        document leaves no document to stop the loop at: the held stage
        says the run was stopped (a rate-limit pause lands the same way)."""

        class Stopping(Run):
            def _derive(self, doc):
                self.rundir.request(action, by="test", reason="a test")
                return super()._derive(doc)

        orchestrator = Stopping(a_folder(tmp_path), tmp_path / "state")
        assert orchestrator.run() == 2
        held = [
            event
            for event in orchestrator.rundir.events()
            if event["event"] == "held"
        ]
        reason = {"abort": "run aborted", "pause": "run paused"}[action]
        assert [event["reason"] for event in held] == [reason]
        entry = self.outcome(orchestrator)
        assert entry["outcome"] == "stopped"
        assert entry["reason"] == reason

    def test_a_held_stage_or_kind_is_no_stop_of_the_run(
        self, tmp_path, factory
    ):
        """A pause of one stage holds each document at it; the run still
        goes through its documents, so it finished."""

        class Holding(Run):
            def _derive(self, doc):
                self.rundir.request("pause", stage="transcription", by="test")
                return super()._derive(doc)

        orchestrator = Holding(a_folder(tmp_path), tmp_path / "state")
        orchestrator.run()
        assert any(
            event["event"] == "held" for event in orchestrator.rundir.events()
        )
        entry = self.outcome(orchestrator)
        assert entry["outcome"] == "finished"
        assert "reason" not in entry

    @pytest.mark.parametrize(
        ("raised", "outcome"),
        [
            (KeyboardInterrupt, "interrupted"),
            ("PassPaused", "paused"),
            (RuntimeError, "failed"),
        ],
    )
    def test_an_invocation_that_does_not_finish_says_how(
        self, tmp_path, factory, raised, outcome
    ):
        from yizkr.pipeline import llm

        if raised == "PassPaused":
            raised = llm.PassPaused

        class Breaking(Run):
            def advance(self, doc, **kwargs):
                raise raised("the CLI went away")

        orchestrator = Breaking(a_folder(tmp_path), tmp_path / "state")
        with pytest.raises(raised):
            orchestrator.run()
        entry = self.outcome(orchestrator)
        assert entry["outcome"] == outcome
        assert entry["ended_at"] is not None
        if outcome != "interrupted":
            # the exception's name only: a message may quote a document
            assert entry["reason"] == raised.__name__
        assert "went away" not in manifest_text(orchestrator)


class TestModelAliases:
    @pytest.mark.parametrize(
        "name",
        [
            "sonnet",
            "opus",
            "haiku",
            "default",
            "opusplan",
            "Sonnet",
            "sonnet[1m]",
            "claude-sonnet",
            "claude-3-7-sonnet-latest",
            "claude-opus-latest",
        ],
    )
    def test_an_alias(self, name):
        assert model_alias(name)

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "  ",
            "claude-sonnet-5",
            "claude-opus-5-5",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5[1m]",
            "anthropic.claude-opus-5",
        ],
    )
    def test_not_an_alias(self, name):
        assert not model_alias(name)

    def test_the_extraction_model_is_refused_before_anything(
        self, tmp_path, factory
    ):
        made, _setting, _engines = factory
        orchestrator = Run(
            a_folder(tmp_path), tmp_path / "state", extract_model="sonnet"
        )
        with pytest.raises(ModelAliasRefused, match="--extract-model") as err:
            orchestrator.run()
        assert "'sonnet' for extract, an alias" in str(err.value)
        assert "full model id" in str(err.value)
        assert not (tmp_path / "state").exists()
        assert made == []

    def test_the_main_model_is_refused_for_every_role_it_serves(
        self, tmp_path, factory, monkeypatch
    ):
        monkeypatch.setenv("YIZKR_LLM_MODEL", "opus")
        orchestrator = Run(
            a_folder(tmp_path),
            tmp_path / "state",
            extract_model="claude-sonnet-5",
        )
        with pytest.raises(ModelAliasRefused) as err:
            orchestrator.run()
        assert "$YIZKR_LLM_MODEL names the model 'opus' for transcribe, " in (
            str(err.value)
        )
        assert "critique, an alias" in str(err.value)

    def test_no_model_is_no_alias(self, tmp_path, factory, monkeypatch):
        """The engine's own default is not keyed, which the LLM cache
        warns about; the run records it as no model."""
        monkeypatch.delenv("YIZKR_LLM_MODEL")
        orchestrator = Run(
            a_folder(tmp_path), tmp_path / "state", extract_model=None
        )
        orchestrator.run()
        roles = orchestrator.rundir.manifest()["roles"]
        assert roles["extract"]["model"] is None
        assert roles["extract"]["client"]["model"] is None

    def test_ingest_run_says_why_and_exits(self, tmp_path, monkeypatch):
        from yizkr.cli import build_parser

        exclude = tmp_path / "never-ingest.txt"
        exclude.write_text("")
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(exclude))
        monkeypatch.delenv("YIZKR_LLM_MODEL", raising=False)
        args = build_parser().parse_args(
            [
                "ingest",
                "run",
                str(a_folder(tmp_path)),
                "--state",
                str(tmp_path / "state"),
                "--llm",
                "claude-code",
                "--extract-model",
                "claude-3-7-sonnet-latest",
            ]
        )
        with pytest.raises(SystemExit, match="an alias"):
            args.func(args)
        assert not (tmp_path / "state").exists()


# -- a run that keeps its settings --------------------------------------------


class TestFixedSettings:
    def test_the_conflict_passes_per_document_isolation(self):
        assert not issubclass(RunSettingsConflict, Exception)

    def test_an_invocation_with_the_same_settings_resumes(
        self, tmp_path, factory
    ):
        made, _setting, _engines = factory
        folder = a_folder(tmp_path)
        Run(folder, tmp_path / "state", fixed_settings=True).run()
        (doc,) = Run(folder, tmp_path / "state").documents()
        doc.batch.unlink()  # the next run makes the batch again
        again = Run(
            folder,
            tmp_path / "state",
            fixed_settings=True,
            refresh=["critique"],  # a refresh belongs to its invocation
            chunk_concurrency=1,  # pacing, not what is made
        )
        again.run()
        manifest = again.rundir.manifest()
        assert [i["outcome"] for i in manifest["invocations"]] == [
            "finished",
            "finished",
        ]
        # no version is imposed on a client: the recorded CLI is checked
        # against the one the client found (TestTheRealClient)
        assert not any("cli_version" in call for call in made)
        assert manifest["engine"]["cli_version"] == CLI

    def test_other_settings_are_refused_and_change_nothing(
        self, tmp_path, factory
    ):
        folder = a_folder(tmp_path)
        Run(
            folder,
            tmp_path / "state",
            fixed_settings=True,
            extract_model="claude-sonnet-5",
        ).run()
        before = (tmp_path / "state" / "run.json").read_bytes()
        other = Run(
            folder,
            tmp_path / "state",
            fixed_settings=True,
            extract_model="claude-opus-5",
        )
        with pytest.raises(RunSettingsConflict) as err:
            other.run()
        message = str(err.value)
        assert "roles.extract.model: 'claude-sonnet-5' recorded, " in message
        assert "'claude-opus-5' now" in message
        assert "config.extract_model" in message
        assert "new run directory" in message
        assert (tmp_path / "state" / "run.json").read_bytes() == before

    def test_a_client_that_applies_other_settings_is_refused(
        self, tmp_path, factory
    ):
        """The same configuration can still reach a client that applies
        another (a changed temporary directory changes the claude CLI's
        working directory, and with it the configuration hash)."""
        made, setting, engines = factory
        folder = a_folder(tmp_path)
        Run(folder, tmp_path / "state", fixed_settings=True).run()
        (doc,) = Run(folder, tmp_path / "state").documents()
        doc.batch.unlink()
        setting["config"] = "c2"
        again = Run(folder, tmp_path / "state", fixed_settings=True)
        with pytest.raises(RunSettingsConflict, match="config_hash"):
            again.run()
        manifest = again.rundir.manifest()
        assert manifest["invocations"][-1]["outcome"] == "refused"
        assert manifest["invocations"][-1]["reason"] == "RunSettingsConflict"
        # nothing recorded from the refused client, which was closed
        extract = manifest["roles"]["extract"]["client"]
        assert extract["config_hash"].startswith("c1-")
        assert engines[-1].closed and engines[-1].prompts == []

    def test_without_it_a_client_may_change(self, tmp_path, factory):
        _made, setting, _engines = factory
        folder = a_folder(tmp_path)
        Run(folder, tmp_path / "state").run()
        (doc,) = Run(folder, tmp_path / "state").documents()
        doc.batch.unlink()
        setting["config"] = "c2"
        again = Run(folder, tmp_path / "state")
        again.run()
        client = again.rundir.manifest()["roles"]["extract"]["client"]
        assert client["config_hash"].startswith("c2-")


class TestTheRealClient:
    @pytest.fixture
    def installed(self, monkeypatch, tmp_path):
        """The claude CLI version the real client's probe finds, which a
        test may change, in a fresh process (no pinned version) with a
        config directory of its own."""
        from yizkr.pipeline import llm

        version = {"installed": "2.1.0"}

        class Completed:
            returncode = 0

            @property
            def stdout(self):
                return f"{version['installed']} (Claude Code)\n"

        def version_probe(command, **kwargs):
            assert command[1:] == ["--version"]
            return Completed()

        monkeypatch.setattr(llm, "_PIN", llm._CliPin())
        monkeypatch.setattr(llm, "_run_process", version_probe)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setenv("YIZKR_LLM_MODEL", FULL_MODEL)
        return version

    def test_a_claude_code_client_is_recorded_as_it_applies(
        self, tmp_path, installed
    ):
        """What the manifest reads off the claude-code client: its model,
        effort, configuration hash and identifier, and the pinned CLI."""
        orchestrator = Orchestrator(
            a_folder(tmp_path),
            tmp_path / "state",
            llm="claude-code",
            exclusions=ExclusionList(),
            review_effort="max",
        )
        orchestrator.rundir.start(roles={}, engine={"name": "claude-code"})
        try:
            engine = orchestrator._engine("critique")
        finally:
            orchestrator._close_engines()
        manifest = orchestrator.rundir.manifest()
        assert manifest["engine"]["cli_version"] == "2.1.0"
        assert manifest["roles"]["critique"]["client"] == {
            "model": FULL_MODEL,
            "effort": "max",
            "config_hash": engine.config_hash,
            "model_identifier": f"claude-code/2.1.0:{FULL_MODEL}",
        }

    def test_a_cli_updated_since_the_run_recorded_it_is_refused(
        self, tmp_path, installed, monkeypatch
    ):
        """A run that keeps its settings, resumed in a new process after
        the claude CLI updated itself: its first client finds the new
        version, and the run refuses it as another setting, with guidance,
        before any call."""
        from yizkr.pipeline import llm

        calls = []
        monkeypatch.setattr(
            llm.ClaudeCodeClient,
            "complete",
            lambda self, prompt, image_paths=(): (
                calls.append(prompt) or '{"done": true, "findings_emitted": 0}'
            ),
        )

        class Critic(Run):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("fixed_settings", True)
                super().__init__(*args, **kwargs)

            def _step_extract(self, doc, transcription):
                return self._run_stage(doc, "extraction", a_batch)

        folder = a_folder(tmp_path)
        Critic(folder, tmp_path / "state").run()
        recorded = RunDir(tmp_path / "state").manifest()
        assert recorded["engine"]["cli_version"] == "2.1.0"
        assert calls  # the critic was asked
        (doc,) = Critic(folder, tmp_path / "state").documents()
        doc.critique.unlink()  # the next run asks the critic again

        # a new process, after the CLI updated itself
        monkeypatch.setattr(llm, "_PIN", llm._CliPin())
        installed["installed"] = "2.1.1"
        asked = len(calls)
        again = Critic(folder, tmp_path / "state")
        with pytest.raises(RunSettingsConflict) as err:
            again.run()
        cli = err.value.conflicts[0]
        assert cli == ("engine.cli_version", "2.1.0", "2.1.1")
        assert [name for name, _, _ in err.value.conflicts] == [
            "engine.cli_version",
            "roles.critique.client.config_hash",
        ]
        message = str(err.value)
        assert "The claude CLI is 2.1.1, not 2.1.0" in message
        assert "DISABLE_AUTOUPDATER=1" in message
        assert "new run directory" in message
        assert len(calls) == asked  # refused before any call
        manifest = again.rundir.manifest()
        entry = manifest["invocations"][-1]
        assert (entry["outcome"], entry["reason"]) == (
            "refused",
            "RunSettingsConflict",
        )
        # the run still records the version it ran on
        assert manifest["engine"]["cli_version"] == "2.1.0"
        client = manifest["roles"]["critique"]["client"]
        assert client == recorded["roles"]["critique"]["client"]

    def test_a_cli_updated_during_the_process_still_pauses_it(
        self, tmp_path, installed
    ):
        """Within one process the pass's own pin holds: a client that
        starts after the CLI changed under the pass pauses it."""
        from yizkr.pipeline import llm

        orchestrator = Orchestrator(
            a_folder(tmp_path),
            tmp_path / "state",
            llm="claude-code",
            exclusions=ExclusionList(),
            fixed_settings=True,
        )
        orchestrator.rundir.start(roles={}, engine={"name": "claude-code"})
        try:
            orchestrator._engine("critique")
            installed["installed"] = "2.1.1"
            with pytest.raises(llm.CliVersionMismatch):
                orchestrator._engine("extract")
        finally:
            orchestrator._close_engines()
        assert orchestrator.rundir.manifest()["engine"]["cli_version"] == (
            "2.1.0"
        )


class TestRunDirManifest:
    SETTINGS = {
        "mode": "scratch run",
        "project_stage": "development",
        "engine": {"name": "claude-code", "cli_version": None},
        "roles": {
            "extract": {
                "model": "claude-sonnet-5",
                "effort": "low",
                "epoch": 0,
                "refresh": False,
                "client": None,
            }
        },
        "corpus": {"folder": "/corpus", "exclusions": None},
        "code": {"commit": "abc", "dirty": False},
        "config": {"refresh": [], "epochs": {}},
    }

    def settings(self, **changes) -> dict:
        settings = json.loads(json.dumps(self.SETTINGS))
        settings.update(changes)
        return settings

    def test_nothing_is_recorded_outside_an_invocation(self, tmp_path):
        run = RunDir(tmp_path / "run")
        run.record_engine("extract", {"model": "m"}, cli_version=CLI)
        assert not (tmp_path / "run").exists()

    def test_an_engine_is_recorded_within_one(self, tmp_path):
        run = RunDir(tmp_path)
        run.start(**self.settings())
        client = {"model": "claude-sonnet-5", "config_hash": "h"}
        run.record_engine("extract", client, cli_version=CLI)
        manifest = run.manifest()
        assert manifest["roles"]["extract"]["client"] == client
        assert manifest["roles"]["extract"]["model"] == "claude-sonnet-5"
        assert manifest["engine"]["cli_version"] == CLI

    def test_a_resumed_run_keeps_what_its_engines_recorded(self, tmp_path):
        RunDir(tmp_path).start(fixed_settings=True, **self.settings())
        run = RunDir(tmp_path)
        run.start(fixed_settings=True, **self.settings())
        run.record_engine("extract", {"config_hash": "h"}, cli_version=CLI)
        later = RunDir(tmp_path)
        manifest = later.start(fixed_settings=True, **self.settings())
        assert manifest["engine"]["cli_version"] == CLI
        assert manifest["roles"]["extract"]["client"] == {"config_hash": "h"}
        with pytest.raises(RunSettingsConflict, match="engine.cli_version"):
            later.record_engine(
                "extract",
                {"config_hash": "h"},
                cli_version="1",
                fixed_settings=True,
            )
        with pytest.raises(RunSettingsConflict, match="client.config_hash"):
            later.record_engine(
                "extract",
                {"config_hash": "g"},
                cli_version=CLI,
                fixed_settings=True,
            )
        # without fixed settings the same client is simply recorded
        later.record_engine("extract", {"config_hash": "g"}, cli_version="1")
        assert later.manifest()["engine"]["cli_version"] == "1"

    def test_a_run_that_may_change_starts_its_engines_afresh(self, tmp_path):
        run = RunDir(tmp_path)
        run.start(**self.settings())
        run.record_engine("extract", {"config_hash": "h"}, cli_version=CLI)
        manifest = RunDir(tmp_path).start(**self.settings())
        assert manifest["engine"]["cli_version"] is None
        assert manifest["roles"]["extract"]["client"] is None

    @pytest.mark.parametrize(
        ("change", "setting"),
        [
            ({"mode": "ingest run"}, "mode"),
            ({"project_stage": "kept"}, "project_stage"),
            ({"code": {"commit": "abd", "dirty": False}}, "code.commit"),
            ({"corpus": {"folder": "/elsewhere"}}, "corpus.folder"),
            ({"config": {"refresh": [], "epochs": {"extract": 1}}}, "config"),
        ],
    )
    def test_a_fixed_setting_that_changes_is_refused(
        self, tmp_path, change, setting
    ):
        RunDir(tmp_path).start(fixed_settings=True, **self.settings())
        with pytest.raises(RunSettingsConflict) as err:
            RunDir(tmp_path).start(
                fixed_settings=True, **self.settings(**change)
            )
        assert any(
            name.startswith(setting) for name, _, _ in err.value.conflicts
        )

    def test_what_may_change_between_invocations(self):
        recorded = self.settings()
        asked = self.settings(
            config={"refresh": ["extract"], "epochs": {}},
            engine={"name": "claude-code", "cli_version": CLI},
            cache="/another/cache",
        )
        asked["roles"]["extract"]["refresh"] = True
        asked["roles"]["extract"]["client"] = {"config_hash": "h"}
        assert settings_conflicts(recorded, asked) == []

    def test_tuples_and_lists_compare_as_json(self):
        recorded = self.settings(config={"refresh": [], "stages": ["a"]})
        asked = self.settings(config={"refresh": (), "stages": ("a",)})
        assert settings_conflicts(recorded, asked) == []

    def test_an_earlier_run_json_without_the_settings_conflicts(
        self, tmp_path
    ):
        RunDir(tmp_path).start(mode="scratch run")
        with pytest.raises(RunSettingsConflict, match="roles.extract"):
            RunDir(tmp_path).start(fixed_settings=True, **self.settings())

    def test_invocations_accumulate(self, tmp_path):
        first = RunDir(tmp_path)
        started = first.start()
        first.end("finished")
        second = RunDir(tmp_path)
        manifest = second.start()
        assert manifest["started_at"] == started["started_at"]
        assert manifest["ended_at"] is None  # the latest has not ended
        assert [entry["outcome"] for entry in manifest["invocations"]] == [
            "finished",
            None,
        ]
        second.end("stopped", reason="run paused")
        last = second.manifest()["invocations"][-1]
        assert last["outcome"] == "stopped"
        assert last["reason"] == "run paused"

    def test_an_unknown_outcome_is_refused(self, tmp_path):
        run = RunDir(tmp_path)
        run.start()
        with pytest.raises(ValueError, match="unknown outcome"):
            run.end("done")


# -- the code version and the never-ingest list -------------------------------


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=where,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestCodeVersion:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch, tmp_path):
        rundir._code_version.cache_clear()
        # no checkout above the test's own folder is found
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        yield
        rundir._code_version.cache_clear()

    def checkout(self, tmp_path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "stage.py").write_text("STAGE = 'transcribe'\n")
        git(repo, "add", "stage.py")
        git(repo, "commit", "-q", "-m", "a stage")
        return repo

    def test_a_clean_checkout(self, tmp_path):
        repo = self.checkout(tmp_path)
        version = code_version(repo)
        assert version["commit"] == git(repo, "rev-parse", "HEAD")
        assert version["dirty"] is False and version["diff_sha256"] is None
        assert version["version"]

    def test_edits_to_tracked_files_are_dirty_and_told_apart(self, tmp_path):
        repo = self.checkout(tmp_path)
        (repo / "stage.py").write_text("STAGE = 'extract'\n")
        first = code_version(repo)
        rundir._code_version.cache_clear()
        (repo / "stage.py").write_text("STAGE = 'critique'\n")
        second = code_version(repo)
        assert first["dirty"] is True and second["dirty"] is True
        assert first["diff_sha256"] != second["diff_sha256"]
        assert first["commit"] == second["commit"]

    def test_untracked_prose_does_not_count(self, tmp_path):
        repo = self.checkout(tmp_path)
        (repo / "notes.txt").write_text("a note\n")
        assert code_version(repo)["dirty"] is False

    @pytest.mark.parametrize(
        "name",
        [
            "src/prompts/read.md",
            "scripts/helper",
            "extra.py",
            "model.yaml",
            "Makefile",
        ],
    )
    def test_untracked_behavior_counts_without_disclosing_paths(
        self, tmp_path, name
    ):
        repo = self.checkout(tmp_path)
        added = repo / name
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_text("fictional behavior one")
        first = code_version(repo, fresh=True)
        added.write_text("fictional behavior two")
        second = code_version(repo, fresh=True)
        assert first["dirty"] is second["dirty"] is True
        assert first["diff_sha256"] != second["diff_sha256"]
        assert name not in json.dumps(second)

    def test_fresh_admission_sees_edits_after_cached_run_snapshot(
        self, tmp_path
    ):
        repo = self.checkout(tmp_path)
        assert code_version(repo)["dirty"] is False
        (repo / "stage.py").write_text("STAGE = 'extract'\n")
        assert code_version(repo)["dirty"] is False
        assert code_version(repo, fresh=True)["dirty"] is True

    def test_ignored_runtime_files_do_not_count(self, tmp_path):
        repo = self.checkout(tmp_path)
        (repo / ".gitignore").write_text("var/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore runtime products")
        (repo / "var").mkdir()
        (repo / "var" / "batch.json").write_text('{"fictional": true}')
        assert code_version(repo, fresh=True)["dirty"] is False

    @pytest.mark.parametrize(
        "name", ["src/ignored.py", "scripts/tool.sh", "src/prompts/read.md"]
    )
    def test_ignored_source_still_counts_but_generated_caches_do_not(
        self, tmp_path, name
    ):
        repo = self.checkout(tmp_path)
        (repo / ".gitignore").write_text("src/\nscripts/\nvar/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore generated fixture files")
        source = repo / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fictional authored behavior")
        assert code_version(repo, fresh=True)["dirty"] is True
        source.unlink()
        for name in (
            "src/__pycache__/module.cpython-311.pyc",
            "src/module.pyc",
            "var/runtime.json",
        ):
            generated = repo / name
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("fictional generated bytes")
        assert code_version(repo, fresh=True)["dirty"] is False

    @pytest.mark.parametrize("operation", ["diff", "ls-files", "ignored"])
    def test_failed_inspection_never_means_clean(
        self, tmp_path, monkeypatch, operation
    ):
        repo = self.checkout(tmp_path)
        real = rundir._git

        def inspect(where, *args):
            failed = args[0] == operation or (
                operation == "ignored" and "--ignored" in args
            )
            return None if failed else real(where, *args)

        monkeypatch.setattr(rundir, "_git", inspect)
        result = code_version(repo, fresh=True)
        assert result["commit"]
        assert result["dirty"] is None
        assert result["diff_sha256"] is None

    def test_outside_a_checkout(self, tmp_path):
        version = code_version(tmp_path)
        assert version["commit"] is None and version["dirty"] is None

    def test_it_is_measured_once_and_handed_out_as_a_copy(self, tmp_path):
        repo = self.checkout(tmp_path)
        code_version(repo)["commit"] = "changed"
        (repo / "stage.py").write_text("STAGE = 'extract'\n")
        assert code_version(repo)["commit"] != "changed"
        assert code_version(repo)["dirty"] is False  # measured before

    def test_the_running_code(self):
        here = Path(rundir.__file__).parent
        assert code_version()["commit"] == git(here, "rev-parse", "HEAD")


class TestExclusionsRecord:
    def test_the_same_entries_hash_the_same(self, tmp_path):
        sha1 = "0" * 39 + "1"
        one = ExclusionList.parse(f"# private\n{sha1}\n*.tmp\n")
        other = ExclusionList.parse(f"*.TMP\n\n{sha1}  copy.pdf\n")
        assert (
            exclusions_record(one)["sha256"]
            == (exclusions_record(other)["sha256"])
        )
        assert (
            exclusions_record(one)["sha256"]
            != (exclusions_record(ExclusionList.parse("*.tmp\n"))["sha256"])
        )

    def test_the_file_is_named_and_no_entry_is_recorded(
        self, tmp_path, monkeypatch
    ):
        from yizkr.core.corpus import read_exclusions

        monkeypatch.chdir(tmp_path)
        Path("never-ingest.txt").write_text("brunnenring-*.pdf\n")
        record = exclusions_record(read_exclusions(Path("never-ingest.txt")))
        assert record["file"] == str(tmp_path / "never-ingest.txt")
        assert "brunnenring" not in json.dumps(record)

    def test_no_list(self):
        assert exclusions_record(None) is None


# -- yizkr stages manifest ----------------------------------------------------


class TestStagesManifestCommand:
    def test_it_prints_what_shapes_the_run(self, tmp_path, factory, capsys):
        from yizkr.cli import build_parser

        orchestrator = Run(
            a_folder(tmp_path),
            tmp_path / "state",
            extract_model="claude-sonnet-5",
        )
        orchestrator.run()
        args = build_parser().parse_args(
            ["stages", "manifest", str(tmp_path / "state")]
        )
        assert args.func(args) == 0
        out = capsys.readouterr().out
        manifest = orchestrator.rundir.manifest()
        assert f"run         {manifest['run_id']} (ingest run, " in out
        assert "development stage" in out
        assert f"engine      claude-code, CLI {CLI}" in out
        assert "cache       off" in out
        assert (
            "role        extract: model claude-sonnet-5, effort low, "
            "epoch 0, config c1-claude-so\n" in out
        )
        assert (
            "role        transcribe: model claude-opus-5-5, effort low, "
            "epoch 0, not started\n" in out
        )
        assert "(finished); 1 invocation\n" in out
        assert describe_manifest(manifest) == out.splitlines()

    def test_a_directory_without_a_run_is_refused(self, tmp_path):
        from yizkr.cli import build_parser

        args = build_parser().parse_args(["stages", "manifest", str(tmp_path)])
        with pytest.raises(SystemExit, match="no run.json"):
            args.func(args)
