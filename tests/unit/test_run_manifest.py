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

from tests.fixtures.document_processing import IsolatedDriver
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import rundir
from yizkr.pipeline.rundir import (
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


class Run(IsolatedDriver):
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


def a_folder(tmp_path, *names: str) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    for name in names or ("letter.txt",):
        (folder / name).write_text(LETTER)
    return folder


def manifest_text(orchestrator) -> str:
    return (orchestrator.state_dir / "run.json").read_text()


# -- the manifest a scratch run writes ----------------------------------------


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


# -- a run that keeps its settings --------------------------------------------


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
        assert f"run         {manifest['run_id']} (scratch run, " in out
        assert "development stage" in out
        assert f"engine      claude-code, CLI {CLI}" in out
        assert f"cache       {orchestrator.cache_dir.resolve()}" in out
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
