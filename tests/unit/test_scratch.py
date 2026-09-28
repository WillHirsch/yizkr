"""Offline tests for the scratch driver (#194): `yizkr scratch run`, and
`yizkr scratch watch|pause|resume|abort`.

The engine is a fake that `yizkr.pipeline.llm.make_client` returns, behind
the real LLM cache, so each run's calls go through the cache as a scratch
run's do. The claude CLI and its login are faked where the preflight
looks for them; nothing here runs the CLI or calls a model. Documents are
the fictional universe's (docs/EXAMPLES.md).

The GEDCOM adapter still cites its artifact by QID (#102's second half
moves it to the SHA-1), and a scratch run registers no artifact, so the
driver here makes a GEDCOM file's batch with a stand-in that cites the
file's SHA-1, through the same stage and its events.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_no_wiki_before_write import (  # noqa: F401
    build_deck,
    refusals,
)
from yizkr import cli
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionConfigError, ExclusionList
from yizkr.pipeline import llm, monitor, orchestrate, scratch
from yizkr.pipeline.llm import CliVersionMismatch, UsageLimitReached
from yizkr.pipeline.orchestrate import directory_names
from yizkr.pipeline.presentation import slide_transcript, unpack_pptx
from yizkr.pipeline.rundir import (
    CONTROL,
    DONE,
    RunDir,
    RunSettingsConflict,
    run_state,
)
from yizkr.pipeline.scratch import (
    BARRIER,
    CORPUS_DIR,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
    CorpusStage,
    PreflightFailed,
    ScratchDriver,
    ScratchRefused,
    ScratchRunInUse,
)
from yizkr.pipeline.stages import read_jsonl

SOURCES = {source.key: source for source in u.SOURCES}
MODEL = "claude-opus-5-5"
CLI = "9.9.9"
DOCUMENT_STAGES = {"transcription", "extraction", "critique", "gedcom"}
THREE = ("transcription", "extraction", "critique")


def quiet(*args, **kwargs) -> None:
    pass


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# -- a fictional corpus -------------------------------------------------------


class Corpus:
    """A folder of fictional documents, and each one's transcription as
    the engine and the pipeline see it, for the fake engine to tell which
    document a prompt is about."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        # file name -> the text its transcription holds
        self.texts: dict[str, str] = {}

    def text(self, name: str, key: str) -> None:
        (self.folder / name).write_text(SOURCES[key].text)
        self.texts[name] = SOURCES[key].text

    def image(self, name: str, key: str) -> None:
        """A synthetic image; the engine reads it as the source's text."""
        (self.folder / name).write_bytes(b"\x89PNG synthetic " + key.encode())
        self.texts[name] = SOURCES[key].text

    def deck(self, name: str, firm: str) -> None:
        path = build_deck(self.folder / name, firm=firm)
        self.texts[name] = slide_transcript(unpack_pptx(path))

    def gedcom(self, name: str, note: str = "") -> None:
        (self.folder / name).write_text(u.GEDCOM_FIXTURE.read_text() + note)

    def sha1(self, name: str) -> str:
        return sha1((self.folder / name).read_bytes())

    def about(self, prompt: str) -> str | None:
        """The file whose transcription the prompt holds, told by the
        longest line of it that no other document's transcription has."""
        for name, text in self.texts.items():
            others = {
                line
                for other, body in self.texts.items()
                if other != name
                for line in body.splitlines()
            }
            unique = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and line not in others
            ]
            if max(unique, key=len)[:40] in prompt:
                return name
        return None


TEXT, PHOTO, LETTER, STONE = (
    "a-postcard-1938.txt",
    "b-studio-photo-1890s.png",
    "c-marriage-cert-1905.txt",
    "d-gravestone-photo-1956.png",
)


def small(tmp_path: Path) -> Corpus:
    """Two letters and two photographs, a letter first."""
    corpus = Corpus(tmp_path / "corpus")
    corpus.text(TEXT, "postcard-1938")
    corpus.image(PHOTO, "studio-photo-1890s")
    corpus.text(LETTER, "marriage-cert-1905")
    corpus.image(STONE, "gravestone-photo-1956")
    return corpus


# -- a fake engine behind the real cache --------------------------------------


class Fakes:
    """The fake engine's corpus, its log of calls, as (what, file), and a
    hook that sees each call before it is answered."""

    def __init__(self) -> None:
        self.corpus: Corpus | None = None
        self.calls: list[tuple[str, str | None]] = []
        self.hook = None


class Engine:
    """What a run reads off an engine client, and scripted answers: a
    reading of each image, an entity manifest of one person, one claim per
    chunk that cites the document by the SHA-1 of its bytes, and a critic
    with nothing to say. It holds no conversations, so chunks see the
    document inline."""

    engine = "claude-code"
    system_prompt = "You are running inside a fictional pipeline."

    def __init__(self, fakes: Fakes, *, effort=None, model=None) -> None:
        self.fakes = fakes
        self.effort = effort
        self.model = model or os.environ.get("YIZKR_LLM_MODEL")
        self.cli_version = CLI
        self.config_hash = f"fake-{self.model}-{effort}"
        self.model_identifier = f"claude-code/{CLI}:{self.model}"

    def complete(self, prompt, image_paths=()):
        corpus = self.fakes.corpus
        if image_paths:
            what, name = "vision", Path(image_paths[0]).name
        else:
            name = corpus.about(prompt)
            if "manifest of EVERY distinct entity" in prompt:
                what = "manifest"
            elif prompt.startswith("You review a genealogical extraction"):
                what = "critique"
            else:
                what = "chunk"
        self.fakes.calls.append((what, name))
        if self.fakes.hook is not None:
            self.fakes.hook(what, name)
        if what == "vision":
            return corpus.texts[name]
        if what == "critique":
            return '{"done": true, "findings_emitted": 0}'
        person = {"id": "e1", "label": "Eugen Löbig", "kind": "human"}
        if what == "manifest":
            return json.dumps({"entities": [person | {"mention_lines": [1]}]})
        claim = {
            "subject": {"kind": "local", "local_id": "e1", "label": "Eugen"},
            "property": "date_of_birth",
            "value": {"type": "time", "year": 1836},
            "source": {
                "artifact": {"kind": "artifact", "sha1": corpus.sha1(name)}
            },
        }
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'


@pytest.fixture
def fakes(monkeypatch, tmp_path) -> Fakes:
    """The engine faked behind the client factory, a full model id, and a
    claude CLI that is installed and logged in, as far as the preflight
    can tell: a credentials file in a config directory of the test's own,
    so nothing reads the developer's own login."""
    fake = Fakes()

    def make_client(engine, *, effort=None, model=None, **kwargs):
        return Engine(fake, effort=effort, model=model)

    monkeypatch.setattr(llm, "make_client", make_client)
    monkeypatch.setattr(llm.ClaudeCodeClient, "available", lambda *a: True)
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("YIZKR_LLM_MODEL", MODEL)
    for variable in ("YIZKR_CLAUDE_RESERVE", "YIZKR_CLAUDE_MAX_WAIT"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return fake


def gedcom_batch(source_sha1: str) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "gedcom-adapter",
                "pipeline_version": "0.1.0",
                "started_at": "2026-09-28T09:00:00Z",
            },
            "claims": [
                {
                    "subject": {"kind": "local", "local_id": "I1"},
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": source_sha1}
                    },
                }
            ],
            "entities": [
                {"id": "I1", "label": "Eugen Löbig", "kind": "human"}
            ],
        }
    )


class Waits:
    """The driver's sleep and clock: each wait moves the clock on by its
    length and runs the next scripted step (None for none). A wait with no
    step left fails the test, since the driver would wait for ever."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.now = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
        self.waited: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waited.append(seconds)
        self.now += timedelta(seconds=max(seconds, 1.0))
        if not self.steps:
            raise AssertionError("the driver waited with nothing to wait for")
        step = self.steps.pop(0)
        if step is not None:
            step()


class Driver(ScratchDriver):
    """The scratch driver with the defaults a test wants: the explicit
    empty never-ingest list, a quiet log, no disk floor, a cache of the
    test's own, and waits that must be scripted (Waits)."""

    def __init__(self, folder, run_dir, *, waits: Waits | None = None, **kw):
        waits = waits or Waits()
        kw.setdefault("exclusions", ExclusionList())
        kw.setdefault("log", quiet)
        kw.setdefault("min_free", 0)
        kw.setdefault("cache_dir", Path(run_dir).parent.parent / "cache")
        kw.setdefault("clock", waits.clock)
        kw.setdefault("sleep", waits.sleep)
        kw.setdefault("poll", 1.0)
        super().__init__(folder, run_dir, **kw)

    def _step_gedcom(self, doc):
        return self._run_stage(doc, "gedcom", lambda: gedcom_batch(doc.sha1))


def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def a_run(tmp_path: Path, corpus: Corpus, fakes: Fakes, **kwargs) -> Driver:
    """A driver for one run, run-1, over the corpus."""
    fakes.corpus = corpus
    return Driver(corpus.folder, runs_dir(tmp_path) / "run-1", **kwargs)


def command(tmp_path, corpus, fakes, **kwargs) -> int:
    """`yizkr scratch run`, as the command runs it (scratch_run), with the
    test's driver."""
    fakes.corpus = corpus
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return scratch.scratch_run(
        corpus.folder, runs=runs_dir(tmp_path), driver=Driver, **kwargs
    )


def events(run, *names: str, invocation: int | None = None) -> list[dict]:
    """The run's events, those named when names are given, of its
    `invocation`-th invocation (0 the first, -1 the latest) when given."""
    rundir = RunDir(run)
    found = rundir.events()
    if invocation is not None:
        ids = [
            entry["invocation"] for entry in rundir.manifest()["invocations"]
        ]
        found = [e for e in found if e.get("invocation") == ids[invocation]]
    return [e for e in found if not names or e["event"] in names]


def stages_of(run, name: str, invocation: int | None = None) -> list[tuple]:
    """(document SHA-1, stage) of each event `name` of a document."""
    return [
        (e["subject"], e["stage"])
        for e in events(run, name, invocation=invocation)
        if e.get("subject")
    ]


def started(run, invocation: int | None = None) -> list[tuple]:
    return stages_of(run, "started", invocation)


def outcome(run) -> tuple[str, str | None]:
    last = RunDir(run).manifest()["invocations"][-1]
    return last["outcome"], last.get("reason")


def statuses(run) -> set[str]:
    return {state.status for state in run_state(RunDir(run)).values()}


def request(run, action, **kwargs):
    """A control request, as the monitor writes one."""
    kwargs.setdefault("by", "monitor")
    return RunDir(run).request(action, **kwargs)


# -- the stages, and their order ----------------------------------------------


class TestOrder:
    def test_canaries_go_first_then_the_rest_stage_by_stage(
        self, tmp_path, fakes
    ):
        corpus = Corpus(tmp_path / "corpus")
        # the kinds interleave in name order
        names = {
            "text": "a-postcard-1938.txt",
            "photo": "b-studio-photo-1890s.png",
            "ged": "c-example-family.ged",
            "letter": "d-marriage-cert-1905.txt",
            "stone": "e-gravestone-photo-1956.png",
            "copy": "f-example-family-copy.ged",
            "deck": "g-Hutmacherei zum Blauen Hut.pptx",
            "book": "h-directory-1912.txt",
        }
        corpus.text(names["text"], "postcard-1938")
        corpus.image(names["photo"], "studio-photo-1890s")
        corpus.gedcom(names["ged"])
        corpus.text(names["letter"], "marriage-cert-1905")
        corpus.image(names["stone"], "gravestone-photo-1956")
        corpus.gedcom(names["copy"], "0 NOTE a copy\n")
        corpus.deck(names["deck"], "gegr. 1863, Brunnenring 35")
        corpus.text(names["book"], "directory-1912")
        s = {key: corpus.sha1(name) for key, name in names.items()}

        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        assert started(driver.state_dir) == [
            # one of each kind, through every stage, in name order
            *[(s["text"], stage) for stage in THREE],
            *[(s["photo"], stage) for stage in THREE],
            (s["ged"], "gedcom"),
            *[(s["deck"], stage) for stage in THREE],
            # then the rest, a stage at a time
            (s["letter"], "transcription"),
            (s["stone"], "transcription"),
            (s["book"], "transcription"),
            (s["letter"], "extraction"),
            (s["stone"], "extraction"),
            (s["copy"], "gedcom"),
            (s["book"], "extraction"),
            (s["letter"], "critique"),
            (s["stone"], "critique"),
            (s["book"], "critique"),
        ]
        manifest = RunDir(driver.state_dir).manifest()
        assert manifest["scratch"]["canaries"] == [
            s["text"],
            s["photo"],
            s["ged"],
            s["deck"],
        ]
        assert statuses(driver.state_dir) == {DONE}

    def test_nothing_before_the_barrier_asks_the_wiki_and_faces_never_run(
        self, tmp_path, fakes, request, monkeypatch
    ):
        asked = request.getfixturevalue("refusals")
        from yizkr.pipeline import faces

        def no_faces(*args, **kwargs):
            raise AssertionError("a scratch run ran face detection")

        monkeypatch.setattr(faces, "make_detector", no_faces)
        corpus = small(tmp_path)
        corpus.deck("e-Hutmacherei.pptx", "gegr. 1863, Brunnenring 35")
        corpus.gedcom("f-example-family.ged")
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        assert asked.asked == []
        planned = {
            stage
            for document in RunDir(driver.state_dir).manifest()["documents"]
            for stage in document["stages"]
        }
        assert planned == DOCUMENT_STAGES
        for doc in driver.documents():
            # no artifact was registered
            assert doc.state.artifact_qid is None
            assert doc.state.status == "batched"
        (deck,) = [
            doc for doc in driver.documents() if doc.kind == "presentation"
        ]
        assert (deck.dir / "derived-media.json").is_file()

    def test_until_stops_after_a_stage_and_the_same_command_goes_on(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes, until="transcription") == 0
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        assert {stage for _sha1, stage in started(run)} == {"transcription"}
        assert not scratch.is_complete(run)
        assert events(run, BARRIER) == []

        # the same command, without --until: the same run goes on
        assert command(tmp_path, corpus, fakes) == 0
        assert scratch.runs_in(runs_dir(tmp_path)) == [run]
        again = started(run, invocation=-1)
        assert {stage for _sha1, stage in again} == {"extraction", "critique"}
        reused = stages_of(run, "reused", invocation=-1)
        assert {stage for _sha1, stage in reused} == {"transcription"}
        assert scratch.is_complete(run)
        # taken to the end, so the next command starts a new run
        assert command(tmp_path, corpus, fakes) == 0
        assert len(scratch.runs_in(runs_dir(tmp_path))) == 2

    def test_identical_bytes_are_one_document(self, tmp_path, fakes):
        corpus = small(tmp_path)
        shutil.copy(corpus.folder / TEXT, corpus.folder / "z-postcard.txt")
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        by_name = {doc.source.name: doc for doc in driver.documents()}
        copy = by_name["z-postcard.txt"]
        assert copy.state.status == "duplicate"
        assert copy.state.detail["duplicate_of"] == TEXT
        assert started(driver.state_dir).count((copy.sha1, THREE[0])) == 1

    def test_a_failed_document_fails_alone(self, tmp_path, fakes):
        corpus = small(tmp_path)

        def hook(what, name):
            if what == "manifest" and name == LETTER:
                raise RuntimeError("engine down")

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_FAILED
        states = run_state(RunDir(driver.state_dir))
        failed = corpus.sha1(LETTER)
        assert states[(failed, "extraction")].status == "failed"
        # its critique is not asked for; the others go to the end
        assert states[(failed, "critique")].status == "pending"
        others = {
            state.status
            for (subject, _stage), state in states.items()
            if subject != failed
        }
        assert others == {DONE}
        assert outcome(driver.state_dir) == ("finished", None)
        (failure,) = events(driver.state_dir, "document-failed")
        assert failure["file"] == LETTER

    def test_an_unreadable_source_fails_alone(self, tmp_path, fakes):
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        corpus = small(tmp_path)
        (corpus.folder / LETTER).chmod(0)
        try:
            driver = a_run(tmp_path, corpus, fakes)
            assert driver.run() == EXIT_FAILED
        finally:
            (corpus.folder / LETTER).chmod(0o644)
        (failure,) = events(driver.state_dir, "document-failed")
        assert failure["file"] == LETTER
        assert "cannot be read" in failure["reason"]
        assert statuses(driver.state_dir) == {DONE}

    def test_a_changed_source_is_set_aside_when_the_run_resumes(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes, until="transcription") == 0
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        old = corpus.sha1(TEXT)
        corpus.text(TEXT, "directory-1912")
        assert command(tmp_path, corpus, fakes) == 0
        (aside,) = events(run, "set-aside")
        assert (aside["file"], aside["reason"]) == (TEXT, "content changed")
        assert (run / aside["kept"]).is_dir()
        new = corpus.sha1(TEXT)
        assert (new, "critique") in started(run, invocation=-1)
        planned = {d["sha1"] for d in RunDir(run).manifest()["documents"]}
        assert new in planned and old not in planned

    def test_an_empty_corpus_starts_no_run(self, tmp_path, fakes):
        driver = a_run(tmp_path, Corpus(tmp_path / "corpus"), fakes)
        assert driver.run() == EXIT_OK
        assert not driver.state_dir.exists()

    def test_no_document_takes_the_corpus_stages_directory(self, tmp_path):
        folder = tmp_path / "corpus"
        folder.mkdir()
        (folder / "corpus.txt").write_text(SOURCES["postcard-1938"].text)
        names = directory_names([folder / "corpus.txt"], tmp_path / "run")
        assert names[folder / "corpus.txt"] != CORPUS_DIR
        assert CORPUS_DIR in orchestrate._RUN_FILES


# -- fresh state directories, through the LLM cache ---------------------------


class TestReuse:
    def test_each_run_starts_afresh_and_the_cache_answers_its_calls(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes) == EXIT_OK
        asked = len(fakes.calls)
        assert asked > 0
        assert command(tmp_path, corpus, fakes) == EXIT_OK
        first, second = scratch.runs_in(runs_dir(tmp_path))
        # a fresh state directory: every view made again, none reused, and
        # every call answered by the cache, none by the engine
        assert len(fakes.calls) == asked
        assert events(second, "reused") == []
        assert statuses(second) == {DONE}
        hits = events(second, "cache-hit")
        assert hits and {hit["reason"] for hit in hits} == {"key match"}
        assert events(second, "cache-miss") == []
        assert events(first, "cache-miss")
        cache = str((tmp_path / "cache").resolve())
        assert RunDir(second).manifest()["cache"] == cache

    def test_refresh_asks_the_engine_again_for_its_role(self, tmp_path, fakes):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes) == EXIT_OK
        fakes.calls.clear()
        assert command(tmp_path, corpus, fakes, refresh=["extract"]) == 0
        assert {what for what, _name in fakes.calls} == {"manifest", "chunk"}
        latest = scratch.runs_in(runs_dir(tmp_path))[-1]
        misses = events(latest, "cache-miss")
        assert {miss["reason"] for miss in misses} == {"refresh"}

    def test_a_scratch_run_needs_the_cache(self, tmp_path):
        with pytest.raises(ValueError, match="LLM cache"):
            ScratchDriver(tmp_path, tmp_path / "run", cache_dir=None)


# -- pause and abort, and resuming --------------------------------------------


class TestControl:
    def test_an_abort_stops_dispatch_and_the_same_command_resumes(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        runs = runs_dir(tmp_path)
        said: list[str] = []

        def hook(what, name):
            # the operator aborts the run from the monitor while the
            # second document's transcription is under way
            if what == "vision" and name == PHOTO:
                (run,) = scratch.runs_in(runs)
                request(run, "abort", reason="the photographs look wrong")

        fakes.hook = hook
        log = lambda *args, **kwargs: said.append(" ".join(map(str, args)))  # noqa: E731
        assert command(tmp_path, corpus, fakes, log=log) == EXIT_STOPPED
        (run,) = scratch.runs_in(runs)
        assert outcome(run) == ("stopped", "run aborted")
        assert not scratch.is_complete(run)
        first = started(run)
        photo = corpus.sha1(PHOTO)
        # the stage in flight finished, and nothing was dispatched after it
        assert first[-1] == (photo, "transcription")
        assert run_state(RunDir(run))[(photo, "transcription")].status == DONE
        assert events(run)[-1]["event"] == "run-end"
        # it says how to read the run, and how to resume it
        assert f"report: yizkr stages state {run}" in said
        resume = f"yizkr scratch run --runs {runs} --resume {run.name}"
        assert f"resume: {resume}" in said

        fakes.hook = None
        asked = list(fakes.calls)
        assert command(tmp_path, corpus, fakes) == EXIT_OK
        assert scratch.runs_in(runs) == [run]
        # nothing the first invocation completed is made again: its views
        # stand, and none of its calls reaches the engine again
        assert not set(first) & set(started(run, invocation=-1))
        assert set(stages_of(run, "reused", invocation=-1)) == set(first)
        assert not set(fakes.calls[len(asked) :]) & set(asked)
        assert statuses(run) == {DONE}

    def test_a_paused_slice_waits_while_the_other_slices_go_on(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        media = {corpus.sha1(PHOTO), corpus.sha1(STONE)}
        paused_after: list[int] = []

        def hook(what, name):
            if what == "vision" and name == PHOTO:
                request(driver.state_dir, "pause", kind="media", reason="look")
                paused_after.append(len(started(driver.state_dir)))

        def resume():
            resumed_after.append(len(started(driver.state_dir)))
            request(driver.state_dir, "resume", kind="media")

        resumed_after: list[int] = []
        fakes.hook = hook
        waits = Waits(resume)
        driver = a_run(tmp_path, corpus, fakes, waits=waits)
        assert driver.run() == EXIT_OK
        order = started(driver.state_dir)
        (paused,), (resumed,) = paused_after, resumed_after
        # while the photographs were paused, the letters went on through
        # every stage, and the driver waited only once nothing else could
        while_paused = order[paused:resumed]
        assert while_paused and not {item[0] for item in while_paused} & media
        assert {stage for _sha1, stage in while_paused} == set(THREE)
        assert {item[0] for item in order[resumed:]} == media
        assert len(waits.waited) == 1
        held = events(driver.state_dir, "held")
        assert {(e["subject"], e["stage"]) for e in held} == {
            (corpus.sha1(PHOTO), "extraction"),
            (corpus.sha1(STONE), "transcription"),
        }
        assert {e["reason"] for e in held} == {"kind:media paused"}
        assert statuses(driver.state_dir) == {DONE}
        assert outcome(driver.state_dir) == ("finished", None)

    def test_an_aborted_slice_stops_short_of_the_barrier(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        ran: list[int] = []
        stage = CorpusStage("clustering", lambda context: ran.append(1))

        def hook(what, name):
            if what == "vision":
                request(driver.state_dir, "abort", stage="transcription")

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, corpus_stages=[stage])
        assert driver.run() == EXIT_STOPPED
        assert outcome(driver.state_dir) == (
            "stopped",
            "stage:transcription aborted",
        )
        states = run_state(RunDir(driver.state_dir))
        assert (
            states[(corpus.sha1(STONE), "transcription")].status == "pending"
        )
        assert events(driver.state_dir, BARRIER) == [] and ran == []
        # the documents the abort held are paused
        closing = events(driver.state_dir, "run-end")[-1]
        assert closing["statuses"] == {"batched": 2, "paused": 2}

        # invoked again, the abort no longer holds, and the run goes past
        # the barrier to its corpus stage
        fakes.hook = None
        again = Driver(corpus.folder, driver.state_dir, corpus_stages=[stage])
        assert again.run() == EXIT_OK
        assert ran == [1]

    @pytest.mark.parametrize("lifted", [False, True])
    def test_a_stage_held_as_it_starts_waits_its_turn(
        self, tmp_path, fakes, lifted
    ):
        """A request can land between the driver's check and the stage's
        own. The stage is held then, with one `held` event however long
        the pause lasts, and dispatched once a resume lets it through; a
        resume that lands at once lets it be tried again, after a
        moment."""
        corpus = small(tmp_path)

        class Raced(Driver):
            def _prepare(self, doc):
                if doc.source.name == LETTER and not raced:
                    raced.append(1)
                    request(self.state_dir, "pause", stage="transcription")
                super()._prepare(doc)

        def log(*args, **kwargs):
            if lifted and "not dispatched" in str(args[0]):
                request(driver.state_dir, "resume", stage="transcription")

        def resume():
            request(driver.state_dir, "resume", stage="transcription")

        raced: list[int] = []
        # a pause not lifted at once outlasts the moment after the hold
        waits = Waits(None) if lifted else Waits(None, resume)
        fakes.corpus = corpus
        driver = Raced(
            corpus.folder, runs_dir(tmp_path) / "run", waits=waits, log=log
        )
        assert driver.run() == EXIT_OK
        letter = corpus.sha1(LETTER)
        held = [
            e for e in events(driver.state_dir, "held")
            if e["subject"] == letter
        ]  # fmt: skip
        assert [(e["stage"], e["reason"]) for e in held] == [
            ("transcription", "stage:transcription paused")
        ]
        assert len(waits.waited) == (1 if lifted else 2)
        assert statuses(driver.state_dir) == {DONE}

    @pytest.mark.parametrize(
        "paused", [{"stage": "transcription"}, {}], ids=["a stage", "the run"]
    )
    def test_an_abort_outranks_a_pause_over_the_same_stages(
        self, tmp_path, fakes, paused
    ):
        """With the photographs aborted, a pause of the transcriptions or
        of the whole run can let none of them through: dispatch stops,
        and the driver never waits for a resume."""
        corpus = Corpus(tmp_path / "corpus")
        corpus.image(PHOTO, "studio-photo-1890s")
        corpus.image(STONE, "gravestone-photo-1956")

        def hook(what, name):
            if what == "vision" and name == PHOTO:
                request(driver.state_dir, "abort", kind="media")
                request(driver.state_dir, "pause", **paused)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_STOPPED
        assert outcome(driver.state_dir) == ("stopped", "kind:media aborted")

    def test_a_paused_run_waits_for_a_resume(self, tmp_path, fakes):
        corpus = small(tmp_path)

        def hook(what, name):
            if what == "vision" and name == PHOTO:
                request(driver.state_dir, "pause", reason="a break")

        fakes.hook = hook
        waits = Waits(None, None, lambda: request(driver.state_dir, "resume"))
        driver = a_run(tmp_path, corpus, fakes, waits=waits)
        assert driver.run() == EXIT_OK
        assert len(waits.waited) == 3
        assert statuses(driver.state_dir) == {DONE}

    def test_ctrl_c_interrupts_and_the_run_resumes(self, tmp_path, fakes):
        corpus = small(tmp_path)

        def hook(what, name):
            if what == "chunk" and name == TEXT:
                raise KeyboardInterrupt

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes)
        with pytest.raises(KeyboardInterrupt):
            driver.run()
        assert outcome(driver.state_dir) == ("interrupted", None)
        assert events(driver.state_dir)[-1]["event"] == "run-interrupted"
        text = corpus.sha1(TEXT)
        # the stage cut short counts as running, and the next invocation
        # makes it, its calls answered by the cache where they can be
        states = run_state(RunDir(driver.state_dir))
        assert states[(text, "extraction")].status == "running"
        fakes.hook = None
        before = len(fakes.calls)
        again = Driver(corpus.folder, driver.state_dir)
        assert again.run() == EXIT_OK
        manifests = [
            call for call in fakes.calls[before:] if call[0] == "manifest"
        ]
        assert ("manifest", TEXT) not in manifests
        hits = events(driver.state_dir, "cache-hit", invocation=-1)
        assert any(hit.get("subject") == text for hit in hits)
        assert statuses(driver.state_dir) == {DONE}

    def test_the_stops_in_force_are_those_since_a_start(self, tmp_path):
        run = RunDir(tmp_path / "run")
        run.request("abort", kind="gedcom")  # before the start: void
        run.start(run_id="r")
        run.request("pause", stage="extraction")
        run.request("resume", stage="extraction")
        run.request("pause", kind="media", by="anomaly", reason="short text")
        run.request("abort", kind="media", by="operator", reason="futile")
        run.request("resume", kind="media")  # an abort holds
        run.request("pause", stage="critique", by="operator", reason="later")
        run.request("pause", reason="a break")
        with open(run.path / CONTROL, "a") as control:
            # a scope that names no stage stops none, and is not carried
            control.write('{"action": "pause", "scope": "stage:"}\n')
        driver = ScratchDriver(tmp_path, run.path)

        def stops(before=None):
            return [
                (r["action"], r["scope"], r["by"], r["reason"])
                for r in driver._stops_in_force(before)
            ]

        in_force = [
            ("abort", "kind:media", "operator", "futile"),
            ("pause", "stage:critique", "operator", "later"),
            ("pause", "run", "operator", "a break"),
        ]
        assert stops() == in_force
        # a later invocation's marker voids them all, and those in force
        # just before it are the ones it carries
        later = RunDir(run.path)
        later.start(run_id="r")
        assert stops() == []
        assert stops(before=later.invocation) == in_force


# -- the subscription's limits ------------------------------------------------


def usage_limit(waits: Waits, minutes: int = 30) -> UsageLimitReached:
    return UsageLimitReached(
        "the five-hour window is used up",
        reason=llm.USAGE_LIMIT,
        window="five_hour",
        resumes_at=waits.now + timedelta(minutes=minutes),
    )


class TestSubscription:
    def test_a_limit_waits_and_invokes_the_run_again(self, tmp_path, fakes):
        corpus = small(tmp_path)
        waits = Waits(*[None] * 10)
        limited = []

        def hook(what, name):
            if what == "chunk" and name == LETTER and not limited:
                limited.append(waits.now + timedelta(minutes=30))
                # meanwhile, the operator has aborted the photographs
                request(
                    driver.state_dir, "abort", kind="media", reason="blurred"
                )
                raise usage_limit(waits)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, waits=waits, poll=600)
        assert driver.run() == EXIT_STOPPED
        first, second = RunDir(driver.state_dir).manifest()["invocations"]
        assert (first["outcome"], first["reason"]) == (
            "paused",
            "UsageLimitReached",
        )
        # it waited out the limit, a margin past it, then went on
        (limit_at,) = limited
        margin = timedelta(seconds=scratch.RESUME_MARGIN)
        assert waits.now >= limit_at + margin
        assert (second["outcome"], second["reason"]) == (
            "stopped",
            "kind:media aborted",
        )
        # the abort the first invocation had is carried into the second,
        # with who asked for it and why
        carried = [
            (r["action"], r["scope"], r["by"], r["reason"])
            for r in read_jsonl(driver.state_dir / CONTROL)
            if r.get("carried")
        ]
        assert carried == [("abort", "kind:media", "monitor", "blurred")]
        states = run_state(RunDir(driver.state_dir))
        assert states[(corpus.sha1(LETTER), "critique")].status == DONE
        (call,) = [
            e
            for e in events(driver.state_dir, "call")
            if e["outcome"] == "paused"
        ]
        assert call["resumes_at"] == limit_at.isoformat(timespec="seconds")

    @pytest.mark.parametrize("gap", ["abort of a kind", "pause of the run"])
    def test_a_request_made_as_it_gets_ready_again_is_honoured(
        self, tmp_path, fakes, gap
    ):
        """After the wait, the driver's preflight and its reading of the
        corpus come before the new invocation's start marker, which voids
        what was asked before it. A request made then is kept in force."""
        corpus = small(tmp_path)

        class Again(Driver):
            def documents(self):
                if self._readings == 1:
                    if gap == "pause of the run":
                        request(self.state_dir, "pause", reason="a break")
                    else:
                        request(self.state_dir, "abort", kind="media")
                self._readings += 1
                return super().documents()

        def resume_once_carried():
            invocations = RunDir(driver.state_dir).manifest()["invocations"]
            if len(invocations) == 2:
                request(driver.state_dir, "resume")

        limited = []

        def hook(what, name):
            if what == "chunk" and name == LETTER and not limited:
                limited.append(1)
                raise usage_limit(waits)

        fakes.hook = hook
        fakes.corpus = corpus
        waits = Waits(*[resume_once_carried] * 10)
        driver = Again(
            corpus.folder, runs_dir(tmp_path) / "run", waits=waits, poll=600
        )
        driver._readings = 0
        second = (
            ("stopped", "kind:media aborted")
            if gap == "abort of a kind"
            else ("finished", None)
        )
        assert driver.run() == (EXIT_STOPPED if second[0] == "stopped" else 0)
        assert outcome(driver.state_dir) == second
        carried = [
            (r["action"], r["scope"])
            for r in read_jsonl(driver.state_dir / CONTROL)
            if r.get("carried")
        ]
        assert carried == (
            [("abort", "kind:media")]
            if gap == "abort of a kind"
            else [("pause", "run")]
        )

    def test_a_refresh_is_not_made_again_after_a_limit(self, tmp_path, fakes):
        """A refresh makes each view of its role again once in a run: the
        views it made before a limit paused the run stand, and the driver's
        next invocation asks the engine only for the rest."""
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes) == EXIT_OK  # a warm cache
        waits = Waits(*[None] * 10)
        limited: list[int] = []

        def hook(what, name):
            if what == "manifest" and name == LETTER and not limited:
                limited.append(len(fakes.calls))
                raise usage_limit(waits)

        fakes.hook = hook
        driver = Driver(
            corpus.folder,
            runs_dir(tmp_path) / "refreshed",
            refresh=["extract"],
            cache_dir=tmp_path / "cache",
            waits=waits,
            poll=600,
        )
        assert driver.run() == EXIT_OK
        (at,) = limited
        again = {
            name
            for what, name in fakes.calls[at:]
            if what in ("manifest", "chunk")
        }
        assert again == {LETTER, STONE}
        reused = stages_of(driver.state_dir, "reused", invocation=-1)
        assert (corpus.sha1(TEXT), "extraction") in reused
        assert (corpus.sha1(PHOTO), "extraction") in reused
        refreshed = stages_of(driver.state_dir, scratch.REFRESHED)
        assert sorted(refreshed) == sorted(
            (corpus.sha1(name), "extraction") for name in corpus.texts
        )

    def test_a_pause_whose_time_has_passed_waits_the_margin(
        self, tmp_path, fakes
    ):
        """The engine can hold a pause past the time it reported. The
        driver never invokes the run again sooner than the margin after
        such a pause, so it does not spin while the pause holds."""
        corpus = small(tmp_path)
        waits = Waits(*[None] * 200)
        paused_at: list[datetime] = []

        def hook(what, name):
            if not paused_at:
                paused_at.append(waits.now)
                raise usage_limit(waits, minutes=-10)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, waits=waits)
        assert driver.run() == EXIT_OK
        (at,) = paused_at
        margin = timedelta(seconds=scratch.RESUME_MARGIN)
        assert waits.now >= at + margin
        assert len(RunDir(driver.state_dir).manifest()["invocations"]) == 2

    def test_usage_credits_end_the_driver(self, tmp_path, fakes):
        """A call that ran on usage credits pauses the pass with no time to
        go on: the driver ends, and never waits."""
        corpus = small(tmp_path)

        def hook(what, name):
            raise UsageLimitReached(
                "a call ran on usage credits",
                reason=llm.OVERAGE,
                resumes_at=None,
            )

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes)
        with pytest.raises(UsageLimitReached):
            driver.run()
        (only,) = RunDir(driver.state_dir).manifest()["invocations"]
        assert (only["outcome"], only["reason"]) == (
            "paused",
            "UsageLimitReached",
        )

    def test_an_abort_while_waiting_ends_the_run(self, tmp_path, fakes):
        corpus = small(tmp_path)
        waits = Waits(None, lambda: request(driver.state_dir, "abort"))

        def hook(what, name):
            raise usage_limit(waits)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, waits=waits)
        assert driver.run() == EXIT_STOPPED
        (only,) = RunDir(driver.state_dir).manifest()["invocations"]
        assert only["outcome"] == "paused"

    def test_ctrl_c_while_waiting_leaves_the_run_paused(self, tmp_path, fakes):
        corpus = small(tmp_path)

        def interrupt():
            raise KeyboardInterrupt

        waits = Waits(None, interrupt)

        def hook(what, name):
            raise usage_limit(waits)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, waits=waits)
        with pytest.raises(KeyboardInterrupt):
            driver.run()
        (only,) = RunDir(driver.state_dir).manifest()["invocations"]
        assert only["outcome"] == "paused"
        assert events(driver.state_dir)[-1]["event"] == "run-end"

    def test_a_pause_of_the_run_holds_it_past_the_time(self, tmp_path, fakes):
        corpus = small(tmp_path)
        waits = Waits(
            lambda: request(driver.state_dir, "pause"),
            *[None] * 3,
            lambda: request(driver.state_dir, "resume"),
            *[None] * 3,
        )
        limited = []

        def hook(what, name):
            if not limited:
                limited.append(1)
                raise usage_limit(waits, minutes=1)

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes, waits=waits, poll=60)
        assert driver.run() == EXIT_OK
        # the pause held it well past the minute the limit asked for
        assert waits.now > datetime(2026, 9, 28, 9, 4, tzinfo=UTC)
        assert len(RunDir(driver.state_dir).manifest()["invocations"]) == 2

    def test_a_pause_with_no_time_to_go_on_ends_the_driver(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)

        def hook(what, name):
            raise CliVersionMismatch("the claude CLI changed")

        fakes.hook = hook
        driver = a_run(tmp_path, corpus, fakes)
        with pytest.raises(CliVersionMismatch):
            driver.run()
        assert outcome(driver.state_dir) == ("paused", "CliVersionMismatch")
        assert events(driver.state_dir)[-1]["event"] == "run-end"


# -- the barrier and the corpus stages ----------------------------------------


class Recorder:
    """A corpus stage's work: it records what it was given, and the run's
    events as it began."""

    def __init__(self, fail: bool = False) -> None:
        self.contexts: list[scratch.CorpusContext] = []
        self.seen: list[list[dict]] = []
        self.fail = fail

    def __call__(self, context):
        self.contexts.append(context)
        self.seen.append(context.run.events())
        if self.fail:
            raise RuntimeError("cannot cluster")
        return ["2 ambiguous clusters"]


def corpus_events(run) -> list[tuple[str, str]]:
    """(event, stage) of each stage event that names no document."""
    return [
        (e["event"], e["stage"])
        for e in events(run, "started", "done", "failed", "held")
        if not e.get("subject")
    ]


class TestCorpusStages:
    def test_they_run_once_past_the_barrier_over_the_completed_documents(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)

        def hook(what, name):
            if what == "manifest" and name == LETTER:
                raise RuntimeError("engine down")

        fakes.hook = hook
        clustering, flags = Recorder(), Recorder()
        stages = [
            CorpusStage("clustering", clustering),
            CorpusStage("flags", flags),
        ]
        driver = a_run(tmp_path, corpus, fakes, corpus_stages=stages)
        assert driver.run() == EXIT_FAILED  # a document failed
        (context,) = clustering.contexts
        assert len(flags.contexts) == 1
        names = sorted(doc.source.name for doc in context.documents)
        assert names == [TEXT, PHOTO, STONE]
        assert (
            context.directory == driver.state_dir / CORPUS_DIR / "clustering"
        )
        assert context.run.path == driver.state_dir
        (barrier,) = events(driver.state_dir, BARRIER)
        assert (barrier["documents"], barrier["failed"]) == (3, 1)
        # it began just past the barrier, and every document's stages had
        # ended before it
        seen = clustering.seen[0]
        assert [e["event"] for e in seen[-2:]] == [BARRIER, "started"]
        assert all(
            e.get("subject") is None
            for e in events(driver.state_dir)[len(seen) - 2 :]
            if e["event"] in ("started", "done", "reused", "failed")
        )
        assert corpus_events(driver.state_dir) == [
            ("started", "clustering"),
            ("done", "clustering"),
            ("started", "flags"),
            ("done", "flags"),
        ]
        (done,) = [
            e
            for e in events(driver.state_dir, "done")
            if e["stage"] == "clustering"
        ]
        assert done["attention"] == ["2 ambiguous clusters"]
        assert outcome(driver.state_dir) == ("finished", None)
        assert scratch.is_complete(driver.state_dir)

    def test_until_a_corpus_stage_stops_after_it(self, tmp_path, fakes):
        clustering, flags = Recorder(), Recorder()
        stages = [
            CorpusStage("clustering", clustering),
            CorpusStage("flags", flags),
        ]
        driver = a_run(
            tmp_path,
            small(tmp_path),
            fakes,
            corpus_stages=stages,
            until="clustering",
        )
        assert driver.run() == EXIT_OK
        assert (len(clustering.contexts), len(flags.contexts)) == (1, 0)
        assert not scratch.is_complete(driver.state_dir)

    def test_a_failing_corpus_stage_fails_the_invocation(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        broken, flags = Recorder(fail=True), Recorder()
        stages = [
            CorpusStage("clustering", broken),
            CorpusStage("flags", flags),
        ]
        driver = a_run(tmp_path, corpus, fakes, corpus_stages=stages)
        assert driver.run() == EXIT_FAILED
        assert outcome(driver.state_dir) == ("failed", "RuntimeError")
        assert corpus_events(driver.state_dir) == [
            ("started", "clustering"),
            ("failed", "clustering"),
        ]
        assert flags.contexts == []
        assert not scratch.is_complete(driver.state_dir)
        # invoked again, the documents' views stand, and it is run again
        broken.fail = False
        again = Driver(corpus.folder, driver.state_dir, corpus_stages=stages)
        assert again.run() == EXIT_OK
        assert len(broken.contexts) == 2 and len(flags.contexts) == 1

    def test_a_paused_corpus_stage_waits_and_an_abort_stops_it(
        self, tmp_path, fakes
    ):
        clustering = Recorder()
        stages = [CorpusStage("clustering", clustering)]

        def hook(what, name):
            if what == "vision" and name == STONE:
                request(driver.state_dir, "pause", stage="clustering")

        fakes.hook = hook
        waits = Waits(
            None,
            lambda: request(driver.state_dir, "abort", stage="clustering"),
        )
        driver = a_run(
            tmp_path, small(tmp_path), fakes, corpus_stages=stages, waits=waits
        )
        assert driver.run() == EXIT_STOPPED
        assert clustering.contexts == []
        assert outcome(driver.state_dir) == (
            "stopped",
            "stage:clustering aborted",
        )
        held = [
            e["reason"]
            for e in events(driver.state_dir, "held")
            if not e.get("subject")
        ]
        assert held == ["stage:clustering paused", "stage:clustering aborted"]

    def test_a_run_at_the_barrier_goes_past_it_on_other_code(
        self, tmp_path, fakes, monkeypatch
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes) == EXIT_OK
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        recorded = RunDir(run).manifest()["code"]
        assert scratch.is_complete(run)

        # a corpus stage is registered since, which is other code
        other = dict(recorded, commit="f" * 40, dirty=False)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        clustering = Recorder()
        stages = [CorpusStage("clustering", clustering)]
        asked = len(fakes.calls)
        resumed = command(
            tmp_path, corpus, fakes, resume=run.name, corpus_stages=stages
        )
        assert resumed == EXIT_OK
        # no document's stage was dispatched, and the corpus stage ran
        assert len(fakes.calls) == asked
        assert started(run, invocation=-1) == []
        assert events(run, "reused", invocation=-1) == []
        assert len(clustering.contexts[0].documents) == 4
        manifest = RunDir(run).manifest()
        assert manifest["code"] == recorded
        assert manifest["scratch"]["code"] == other
        assert manifest["scratch"]["past_barrier_only"] is True
        assert scratch.is_complete(run)

        # --refresh would make views again: refused
        with pytest.raises(ScratchRefused, match="--refresh"):
            command(
                tmp_path,
                corpus,
                fakes,
                resume=run.name,
                corpus_stages=stages,
                refresh=["extract"],
            )

    def test_other_code_is_refused_before_the_barrier(
        self, tmp_path, fakes, monkeypatch
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes, until="extraction") == 0
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        before = (run / "run.json").read_bytes()
        other = dict(RunDir(run).manifest()["code"], commit="f" * 40)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        with pytest.raises(RunSettingsConflict, match="code.commit"):
            command(tmp_path, corpus, fakes)
        assert (run / "run.json").read_bytes() == before

    def test_an_abort_of_a_corpus_stage_outranks_a_pause_of_the_run(
        self, tmp_path, fakes
    ):
        clustering = Recorder()

        def flags(context):
            request(context.run.path, "pause")
            request(context.run.path, "abort", stage="clustering")

        stages = [
            CorpusStage("flags", flags),
            CorpusStage("clustering", clustering),
        ]
        driver = a_run(tmp_path, small(tmp_path), fakes, corpus_stages=stages)
        assert driver.run() == EXIT_STOPPED
        assert outcome(driver.state_dir) == (
            "stopped",
            "stage:clustering aborted",
        )
        assert clustering.contexts == []

    def test_every_invocation_past_the_barrier_runs_them(
        self, tmp_path, fakes
    ):
        """A corpus stage runs in each invocation that goes past the
        barrier, and reuses its own outputs while their inputs stand."""
        corpus = small(tmp_path)
        clustering = Recorder()
        stages = [CorpusStage("clustering", clustering)]
        assert command(tmp_path, corpus, fakes, corpus_stages=stages) == 0
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        again = command(
            tmp_path, corpus, fakes, resume=run.name, corpus_stages=stages
        )
        assert again == EXIT_OK
        assert len(clustering.contexts) == 2
        assert started(run, invocation=-1) == []

    def test_a_failed_document_keeps_no_run_from_other_code(
        self, tmp_path, fakes, monkeypatch
    ):
        """A document that failed leaves its later stages undispatched;
        it has ended all the same, so a run at the barrier goes past it
        on other code."""
        corpus = small(tmp_path)

        def hook(what, name):
            if what == "manifest" and name == LETTER:
                raise RuntimeError("engine down")

        fakes.hook = hook
        assert command(tmp_path, corpus, fakes) == EXIT_FAILED
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        letter = corpus.sha1(LETTER)
        assert run_state(RunDir(run))[(letter, "critique")].status == "pending"
        other = dict(RunDir(run).manifest()["code"], commit="f" * 40)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        clustering = Recorder()
        stages = [CorpusStage("clustering", clustering)]
        # the failed document's state still says so
        again = command(
            tmp_path, corpus, fakes, resume=run.name, corpus_stages=stages
        )
        assert again == EXIT_FAILED
        assert started(run, invocation=-1) == []
        assert len(clustering.contexts[0].documents) == 3
        assert RunDir(run).manifest()["scratch"]["past_barrier_only"] is True

    def test_corpus_stages_need_names_of_their_own(self, tmp_path):
        stage = CorpusStage("critique", lambda context: None)
        run = tmp_path / "runs" / "r"
        with pytest.raises(ValueError, match="critique"):
            Driver(tmp_path, run, corpus_stages=[stage])
        with pytest.raises(ValueError, match="no stage 'clustring'"):
            Driver(tmp_path, run, until="clustring")


class TestTheWiki:
    """A stage that touches the wiki runs only on a reachable stack and an
    instance that carries no kept-state marker."""

    def stage(self, ran: list) -> CorpusStage:
        return CorpusStage(
            "write", lambda ctx: ran.append(1), touches_wiki=True
        )

    def test_nothing_can_tell_whether_the_instance_is_kept(
        self, tmp_path, fakes, monkeypatch
    ):
        monkeypatch.setattr(scratch, "stack_unreachable", lambda env: None)
        ran: list = []
        stages = [self.stage(ran)]
        driver = a_run(tmp_path, small(tmp_path), fakes, corpus_stages=stages)
        with pytest.raises(PreflightFailed, match="kept-state marker"):
            driver.run()
        assert not driver.state_dir.exists() and ran == []

    def test_a_kept_instance_is_refused(self, tmp_path, fakes, monkeypatch):
        monkeypatch.setattr(scratch, "stack_unreachable", lambda env: None)
        ran: list = []
        driver = a_run(
            tmp_path,
            small(tmp_path),
            fakes,
            corpus_stages=[self.stage(ran)],
            kept_state=lambda env: True,
        )
        with pytest.raises(PreflightFailed, match="never touches a kept"):
            driver.run()
        assert ran == []

    def test_an_unreachable_stack_is_refused(
        self, tmp_path, fakes, monkeypatch
    ):
        def unreachable(env):
            return "ConnectionError: refused"

        monkeypatch.setattr(scratch, "stack_unreachable", unreachable)
        driver = a_run(
            tmp_path,
            small(tmp_path),
            fakes,
            corpus_stages=[self.stage([])],
            kept_state=lambda env: False,
        )
        with pytest.raises(PreflightFailed, match="stack cannot be reached"):
            driver.run()

    def test_the_stack_is_not_asked_for_when_no_stage_to_run_touches_it(
        self, tmp_path, fakes, monkeypatch
    ):
        def unreachable(env):
            raise AssertionError("the stack was asked for")

        monkeypatch.setattr(scratch, "stack_unreachable", unreachable)
        driver = a_run(
            tmp_path,
            small(tmp_path),
            fakes,
            corpus_stages=[self.stage([])],
            until="critique",
        )
        assert driver.run() == EXIT_OK

    def test_it_runs_on_an_instance_not_kept_checked_again_before_it(
        self, tmp_path, fakes, monkeypatch
    ):
        monkeypatch.setattr(scratch, "stack_unreachable", lambda env: None)
        ran: list = []
        answers = iter([False, False])
        driver = a_run(
            tmp_path,
            small(tmp_path),
            fakes,
            corpus_stages=[self.stage(ran)],
            kept_state=lambda env: next(answers),
        )
        assert driver.run() == EXIT_OK
        assert ran == [1]
        # kept by the time the stage is to run: refused, and not run
        answers = iter([False, True])
        again = Driver(
            driver.folder,
            driver.state_dir,
            corpus_stages=[self.stage(ran)],
            kept_state=lambda env: next(answers),
        )
        with pytest.raises(ScratchRefused, match="write not run"):
            again.run()
        assert ran == [1]
        assert outcome(driver.state_dir) == ("failed", "ScratchRefused")


# -- preflight ----------------------------------------------------------------


class TestPreflight:
    def test_no_never_ingest_list_no_run(self, tmp_path, fakes):
        driver = a_run(tmp_path, small(tmp_path), fakes)
        driver.exclusions = None
        with pytest.raises(ExclusionConfigError):
            driver.run()
        assert not runs_dir(tmp_path).exists()

    def test_every_problem_is_listed_before_anything_is_written(
        self, tmp_path, fakes, monkeypatch
    ):
        monkeypatch.setenv("YIZKR_LLM_MODEL", "opus")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
        monkeypatch.setenv("YIZKR_CLAUDE_RESERVE", "2")

        def disk_usage(path):
            return shutil._ntuple_diskusage(100, 99, 1)

        monkeypatch.setattr(scratch.shutil, "disk_usage", disk_usage)
        driver = a_run(tmp_path, small(tmp_path), fakes, min_free=1024**3)
        with pytest.raises(PreflightFailed) as refused:
            driver.run()
        problems = refused.value.problems
        assert any("'opus'" in problem for problem in problems)
        assert any("YIZKR_CLAUDE_RESERVE" in problem for problem in problems)
        assert any("no login" in problem for problem in problems)
        assert sum("GiB free" in problem for problem in problems) == 2
        assert not driver.state_dir.exists()
        assert fakes.calls == []

    def test_every_role_needs_a_model(self, tmp_path, fakes, monkeypatch):
        monkeypatch.delenv("YIZKR_LLM_MODEL")
        driver = a_run(
            tmp_path, small(tmp_path), fakes, extract_model="claude-sonnet-5"
        )
        with pytest.raises(PreflightFailed, match="transcribe, critique"):
            driver.run()

    def test_the_claude_cli_must_be_installed(
        self, tmp_path, fakes, monkeypatch
    ):
        monkeypatch.setattr(llm.ClaudeCodeClient, "available", lambda *a: 0)
        driver = a_run(tmp_path, small(tmp_path), fakes)
        with pytest.raises(PreflightFailed, match="not installed"):
            driver.run()

    @pytest.mark.parametrize("login", ["token", "account"])
    def test_other_logins_count(self, tmp_path, fakes, monkeypatch, login):
        config = tmp_path / "other-config"
        config.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        assert not scratch.claude_logged_in()
        if login == "token":
            monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fictional")
        else:
            (config / ".claude.json").write_text('{"oauthAccount": {"x": 1}}')
        assert scratch.claude_logged_in()

    def test_a_pdf_needs_the_renderer(self, tmp_path, fakes, monkeypatch):
        from yizkr.pipeline import transcribe

        corpus = small(tmp_path)
        (corpus.folder / "e-card-1945.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(transcribe.shutil, "which", lambda tool: None)
        driver = a_run(tmp_path, corpus, fakes)
        with pytest.raises(PreflightFailed, match="poppler"):
            driver.run()
        assert not driver.state_dir.exists()


# -- the manifest -------------------------------------------------------------


class TestManifest:
    def test_it_records_a_scratch_run(self, tmp_path, fakes, monkeypatch):
        monkeypatch.setenv("YIZKR_CLAUDE_RESERVE", "0.4")
        corpus = small(tmp_path)
        driver = a_run(tmp_path, corpus, fakes, until="critique")
        assert driver.run() == EXIT_OK
        manifest = RunDir(driver.state_dir).manifest()
        assert manifest["run_id"] == "run-1"
        assert (manifest["mode"], manifest["project_stage"]) == (
            "scratch run",
            "development",
        )
        assert manifest["engine"] == {
            "name": "claude-code",
            "cli_version": CLI,
        }
        assert manifest["pacing"] == {"reserve": 0.4, "max_wait": 960.0}
        assert manifest["scratch"] == {
            "until": "critique",
            "canaries": [corpus.sha1(TEXT), corpus.sha1(PHOTO)],
            "corpus_stages": [],
            "past_barrier_only": False,
            "complete": True,
        }
        files = sorted(d["file"] for d in manifest["documents"])
        assert files == sorted(corpus.texts)
        assert manifest["roles"]["extract"]["client"]["model"] == MODEL
        assert manifest["invocations"][-1]["outcome"] == "finished"

    def test_an_invocation_with_other_settings_is_refused(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        assert command(tmp_path, corpus, fakes, until="transcription") == 0
        (run,) = scratch.runs_in(runs_dir(tmp_path))
        before = (run / "run.json").read_bytes()
        with pytest.raises(RunSettingsConflict, match="config.review_effort"):
            command(tmp_path, corpus, fakes, review_effort="max")
        assert (run / "run.json").read_bytes() == before
        # what may change between invocations may change
        assert command(tmp_path, corpus, fakes, chunk_concurrency=1) == 0


# -- the runs directory -------------------------------------------------------


class TestRunsDirectory:
    def test_one_driver_at_a_time(self, tmp_path):
        runs = runs_dir(tmp_path)
        with (
            scratch.hold(runs),
            pytest.raises(ScratchRunInUse, match='"pid"'),
            scratch.hold(runs),
        ):
            pass
        assert (runs / scratch.LOCK_FILE).read_text() == ""
        assert (runs.stat().st_mode & 0o777) == 0o700
        with scratch.hold(runs):
            pass

    def test_the_command_waits_its_turn_behind_another_driver(
        self, tmp_path, fakes
    ):
        corpus = small(tmp_path)
        runs = runs_dir(tmp_path)
        with scratch.hold(runs), pytest.raises(ScratchRunInUse):
            command(tmp_path, corpus, fakes)
        assert scratch.runs_in(runs) == []
        assert [path.name for path in runs.iterdir()] == [scratch.LOCK_FILE]
        assert fakes.calls == []

    def test_a_runs_directory_it_cannot_use_is_refused(self, tmp_path):
        a_file = tmp_path / "a-file"
        a_file.write_text("")
        for runs in (a_file, a_file / "runs"):
            with (
                pytest.raises(ScratchRefused, match="cannot be the runs"),
                scratch.hold(runs),
            ):
                pass
        closed = tmp_path / "closed"
        closed.mkdir()
        closed.chmod(0o500)
        try:
            if os.access(closed, os.W_OK):
                pytest.skip("this user writes to any directory")
            with (
                pytest.raises(ScratchRefused, match="cannot be the runs"),
                scratch.hold(closed),
            ):
                pass
        finally:
            closed.chmod(0o700)

    def test_a_runs_directory_open_to_others_is_refused_and_left_alone(
        self, tmp_path
    ):
        """The driver makes private only the runs directory it makes: one
        that exists keeps its mode, and is refused when others can read or
        write it (a home directory, say)."""
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o750)
        with (
            pytest.raises(ScratchRefused, match="open to other users"),
            scratch.hold(shared),
        ):
            pass
        assert stat.S_IMODE(shared.stat().st_mode) == 0o750
        assert list(shared.iterdir()) == []
        private = tmp_path / "private"
        private.mkdir()
        private.chmod(0o700)
        with scratch.hold(private):
            pass
        made = tmp_path / "made" / "runs"
        with scratch.hold(made):
            pass
        assert stat.S_IMODE(made.stat().st_mode) == 0o700

    def test_a_runs_directory_git_would_track_is_refused(
        self, tmp_path, monkeypatch
    ):
        if shutil.which("git") is None:
            pytest.skip("no git")
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text("var/\n")
        monkeypatch.chdir(repo)
        for runs in (Path("."), repo / "runs", repo / "var-scratch"):
            with (
                pytest.raises(ScratchRefused, match="git work tree"),
                scratch.hold(runs),
            ):
                pass
        assert sorted(p.name for p in repo.iterdir()) == [".git", ".gitignore"]
        with scratch.hold(Path("var/scratch")):
            pass
        assert (repo / "var" / "scratch" / scratch.LOCK_FILE).is_file()

    def test_the_command_resumes_the_latest_run_unless_it_went_to_the_end(
        self, tmp_path
    ):
        runs = runs_dir(tmp_path)
        runs.mkdir()
        assert scratch.choose_run(runs).parent == runs  # a new one
        older = runs / "20260927T090000Z-0000aa"
        newer = runs / "20260928T090000Z-0000aa"
        for path, complete in ((older, False), (newer, True)):
            RunDir(path).update_manifest(
                run_id=path.name,
                mode="scratch run",
                scratch={"complete": complete},
            )
        chosen = scratch.choose_run(runs)
        assert chosen not in (older, newer) and chosen.name > newer.name
        RunDir(newer).update_manifest(scratch={"complete": False})
        assert scratch.choose_run(runs) == newer
        assert scratch.choose_run(runs, resume=older.name) == older
        assert scratch.choose_run(runs, resume=str(older)) == older
        assert scratch.choose_run(runs, resume=scratch.LATEST) == newer
        assert scratch.choose_run(runs, new=True) not in (older, newer)
        with pytest.raises(ScratchRefused, match="no run there"):
            scratch.choose_run(runs, resume="20260101T000000Z-000000")
        # the latest directory there is no scratch run: the command takes
        # the latest scratch run, and --resume refuses the other
        state = runs / "20260929T090000Z-0000aa"
        RunDir(state).update_manifest(run_id="x", mode="ingest run")
        assert scratch.choose_run(runs) == newer
        with pytest.raises(ScratchRefused, match="not a scratch run"):
            scratch.choose_run(runs, resume=state.name)

    def test_a_run_is_resumed_only_from_its_own_runs_directory(self, tmp_path):
        """One driver at a time holds a runs directory, so a run in another
        is refused, and the refusal names the runs directory to hold."""
        runs, elsewhere = runs_dir(tmp_path), tmp_path / "elsewhere"
        run = elsewhere / "20260928T090000Z-0000aa"
        RunDir(run).update_manifest(run_id=run.name, mode="scratch run")
        with pytest.raises(ScratchRefused) as refused:
            scratch.choose_run(runs, resume=str(run))
        assert f"--runs {elsewhere} --resume {run.name}" in str(refused.value)
        assert scratch.choose_run(elsewhere, resume=str(run)) == run

    def test_new_run_ids_sort_as_the_runs_start(self, tmp_path):
        runs = runs_dir(tmp_path)
        ids = []
        for _ in range(5):
            ids.append(scratch.new_run_id(runs))
            (runs / ids[-1]).mkdir(parents=True)
        assert ids == sorted(ids) and len(set(ids)) == 5
        assert all(re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", i) for i in ids)


# -- the command line ---------------------------------------------------------


def cli_run(*argv: str) -> int:
    args = cli.build_parser().parse_args(list(argv))
    return args.func(args)


class TestCommandLine:
    def a_run_dir(self, tmp_path) -> Path:
        run = runs_dir(tmp_path) / "20260928T090000Z-0000aa"
        document = {"sha1": "a" * 40, "file": "a.png", "kind": "media"}
        RunDir(run).start(
            run_id=run.name,
            mode="scratch run",
            documents=[document | {"stages": ["transcription"]}],
        )
        return run

    def test_watch_follows_the_latest_run(self, tmp_path, monkeypatch):
        run = self.a_run_dir(tmp_path)
        seen = {}

        def watch(path, **kwargs):
            seen.update(kwargs, path=Path(path))
            return 0

        monkeypatch.setattr(monitor, "watch", watch)
        runs = str(runs_dir(tmp_path))
        assert cli_run("scratch", "watch", "--runs", runs, "--once") == 0
        assert seen == {
            "path": run,
            "interval": 2.0,
            "once": True,
            "page": True,
            "serve": None,
        }
        assert cli_run("scratch", "watch", str(run), "--no-page") == 0
        assert seen["page"] is False
        with pytest.raises(SystemExit, match="no run there"):
            cli_run("scratch", "watch", "nothing", "--runs", runs)
        seen.clear()
        serve = ("--interval", "5", "--serve", "127.0.0.1:0")
        assert cli_run("scratch", "watch", str(run), *serve) == 0
        assert seen == {
            "path": run,
            "interval": 5.0,
            "once": False,
            "page": True,
            "serve": "127.0.0.1:0",
        }

    def test_pause_resume_and_abort_write_the_control_file(
        self, tmp_path, capsys
    ):
        run = self.a_run_dir(tmp_path)
        runs = str(runs_dir(tmp_path))
        reason = "the photographs look wrong"
        pause = ("scratch", "pause", "--runs", runs, "--kind", "media")
        assert cli_run(*pause, "--reason", reason) == 0
        out = capsys.readouterr().out
        # with no run named, it says which run it wrote to
        assert f"pause requested for kind:media of run {run.name}: " in out
        assert "now paused" in out
        assert cli_run("scratch", "abort", str(run), "--kind", "media") == 0
        again = f"yizkr scratch run --runs {run.parent} --resume {run.name}"
        assert again in capsys.readouterr().out
        # a resume does not lift an abort, so none is written
        assert cli_run("scratch", "resume", str(run), "--kind", "media") == 1
        out = capsys.readouterr().out
        assert "invoke the run again" in out and again in out
        written = [
            (r["action"], r["scope"], r["by"], r["reason"])
            for r in read_jsonl(run / CONTROL)
            if r["action"] != "start"
        ]
        assert written == [
            ("pause", "kind:media", "operator", reason),
            ("abort", "kind:media", "operator", ""),
        ]
        assert cli_run("scratch", "pause", str(run), "--stage", "flags") == 0
        out = capsys.readouterr().out
        assert "no document the run knows has this slice" in out
        with pytest.raises(SystemExit, match="no spaces"):
            cli_run("scratch", "pause", str(run), "--stage", "a b")

    def test_run_takes_the_corpus_and_the_never_ingest_list(
        self, tmp_path, monkeypatch
    ):
        # the default runs directory and cache are relative: keep them here
        monkeypatch.chdir(tmp_path)
        folder = tmp_path / "corpus"
        folder.mkdir()
        exclusions = tmp_path / "never-ingest.txt"
        exclusions.write_text("# none\n")
        monkeypatch.setenv("YIZKR_CORPUS_DIR", str(folder))
        monkeypatch.delenv("YIZKR_EXCLUDE_FILE", raising=False)
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))
        seen = {}

        def scratch_run(corpus, **options):
            seen.update(options, corpus=corpus)
            return 0

        monkeypatch.setattr(scratch, "scratch_run", scratch_run)
        # no never-ingest list: refused before the driver is asked
        with pytest.raises(SystemExit, match="never-ingest list"):
            cli_run("scratch", "run")
        assert seen == {}
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(exclusions))
        argv = ["--until", "extraction", "--refresh", "extract", "--resume"]
        argv += ["--extract-model", "inherit", "--min-free", "0.5"]
        assert cli_run("scratch", "run", *argv) == 0
        assert seen["corpus"] == folder
        assert (seen["resume"], seen["new"]) == (scratch.LATEST, False)
        assert (seen["until"], seen["refresh"]) == ("extraction", ["extract"])
        assert seen["extract_model"] is None
        assert seen["min_free"] == 1024**3 // 2
        assert seen["exclusions"].source == exclusions
        assert seen["runs"] == scratch.DEFAULT_RUNS
        assert seen["cache_dir"] == Path("var/llm-cache")

    def test_run_passes_on_every_option(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        folder = tmp_path / "corpus"
        folder.mkdir()
        exclusions = tmp_path / "never-ingest.txt"
        exclusions.write_text("# none\n")
        # the never-ingest list comes from the env file --env-file names
        env = tmp_path / "fictional.env"
        env.write_text(f"YIZKR_EXCLUDE_FILE={exclusions}\n")
        monkeypatch.delenv("YIZKR_EXCLUDE_FILE", raising=False)
        monkeypatch.delenv("YIZKR_CORPUS_DIR", raising=False)
        seen = {}

        def scratch_run(corpus, **options):
            seen.update(options, corpus=corpus)
            return 0

        monkeypatch.setattr(scratch, "scratch_run", scratch_run)
        argv = [
            "--env-file", str(env), "scratch", "run",
            "--corpus", str(folder), "--runs", "my-runs", "--new",
            "--until", "critique", "--refresh", "transcribe",
            "--refresh", "critique", "--cache", "my-cache",
            "--language", "de", "--fallback-language", "de",
            "--chunk-concurrency", "5", "--extract-effort", "medium",
            "--review-effort", "max", "--extract-model", "claude-opus-5-5",
            "--min-free", "3",
        ]  # fmt: skip
        assert cli_run(*argv) == 0
        options = dict(seen)
        assert options.pop("exclusions").source == exclusions
        assert options == {
            "corpus": folder,
            "runs": Path("my-runs"),
            "resume": None,
            "new": True,
            "until": "critique",
            "refresh": ["transcribe", "critique"],
            "cache_dir": Path("my-cache"),
            "env_file": env,
            "language": "de",
            "fallback_language": "de",
            "chunk_concurrency": 5,
            "extract_effort": "medium",
            "review_effort": "max",
            "extract_model": "claude-opus-5-5",
            "min_free": 3 * 1024**3,
        }

    @pytest.mark.parametrize("size", ["nan", "inf", "-1", "two"])
    def test_min_free_is_a_finite_size(self, tmp_path, capsys, size):
        with pytest.raises(SystemExit) as refused:
            cli_run("scratch", "run", "--min-free", size)
        assert refused.value.code == 2
        assert "finite and 0 or more" in capsys.readouterr().err

    def test_run_says_why_it_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        folder = tmp_path / "corpus"
        folder.mkdir()
        exclusions = tmp_path / "never-ingest.txt"
        exclusions.write_text("")
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(exclusions))
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))

        def refused(corpus, **options):
            raise PreflightFailed(["the claude CLI has no login"])

        monkeypatch.setattr(scratch, "scratch_run", refused)
        assert cli_run("scratch", "run", "--corpus", str(folder)) == 1
        assert "no login" in capsys.readouterr().err

        def paused(corpus, **options):
            raise CliVersionMismatch("the claude CLI changed")

        monkeypatch.setattr(scratch, "scratch_run", paused)
        assert cli_run("scratch", "run", "--corpus", str(folder)) == 2
        assert "CLI changed" in capsys.readouterr().err

        def conflict(corpus, **options):
            raise RunSettingsConflict(folder, [("code.commit", "a", "b")])

        monkeypatch.setattr(scratch, "scratch_run", conflict)
        assert cli_run("scratch", "run", "--corpus", str(folder)) == 1
        assert "yizkr scratch run --new" in capsys.readouterr().err
