"""Offline tests for the call estimate (#101): `yizkr scratch estimate`.

A fake claude-code engine behind the real LLM cache makes scratch runs,
as in test_scratch.py, and holds conversations whose sessions it keeps
and resumes as the claude CLI does. Asked again, it answers afresh, as a
model does. The estimate counts the calls the next invocation is to
make, and the tests hold its counts against the calls that invocation
then makes: the known counts exactly, the estimated ones where the fake
makes as many calls as the estimate expects, and the bounds as bounds.
The claude CLI and its login are faked where they are looked for;
nothing runs the CLI or calls a model. Documents are the fictional
universe's (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_no_wiki_before_write import (  # noqa: F401
    build_deck,
    refusals,
)
from tests.unit.test_scratch import (  # noqa: F401
    CLI,
    LETTER,
    PHOTO,
    STONE,
    TEXT,
    Corpus,
    Driver,
    fakes,
    quiet,
    runs_dir,
    small,
)
from yizkr import cli
from yizkr.core.corpus import ExclusionConfigError, ExclusionList
from yizkr.pipeline import estimate, llm, scratch
from yizkr.pipeline.estimate import FIRST_TURNS, Estimator, StageCalls
from yizkr.pipeline.rundir import RunDir, RunSettingsConflict
from yizkr.pipeline.scratch import (
    EXIT_FAILED,
    EXIT_OK,
    LATEST,
    PreflightFailed,
    ScratchRefused,
)

# what each of the fake engine's calls is made for
STAGE_OF = {
    "vision": "transcription",
    "manifest": "extraction",
    "chunk": "extraction",
    "critique": "critique",
}
# the artifact's SHA-1, as the extraction prompts name it
SHA1 = re.compile(r"sha1\W*([0-9a-f]{40})")
# a live cache entry's file name
ENTRY = re.compile(r"^[0-9a-f]{64}\.json$")


class Engine:
    """The claude-code engine as a run and the estimate see it: keyed by
    its model, effort and CLI version, and holding conversations, whose
    sessions it keeps and resumes as the claude CLI does (one transcript,
    named by its session id).

    Its answers: a reading of each image, noting what read it; an entity
    manifest of `fakes.people` people (ten to a chunk), or none while
    `fakes.refuse`, which the extractor refuses, and a segmentation of the
    whole text as one unit; a claim per chunk, whose year the
    configuration and the document pick (the configuration alone while
    `fakes.same_claims`); and a critic with one concern. While
    `fakes.fresh`, a reading, a manifest and a chunk are numbered in the
    order the engine is asked for them, so that one asked again comes back
    different, as a model's answer does."""

    engine = "claude-code"
    system_prompt = "You are running inside a fictional pipeline."

    def __init__(self, fake, *, effort=None, model=None, **kwargs) -> None:
        self.fakes = fake
        self.effort = effort
        self.model = model or os.environ.get("YIZKR_LLM_MODEL")
        self.cli_version = fake.cli
        self.config_hash = f"fake-{self.cli_version}-{self.model}-{effort}"
        self.model_identifier = f"claude-code/{self.cli_version}:{self.model}"

    def _call(self, what: str, name: str | None) -> int | None:
        """Log the call, run the hook, and number the answer, while
        `fakes.fresh`."""
        self.fakes.calls.append((what, name))
        if self.fakes.hook is not None:
            self.fakes.hook(what, name)
        return next(self.fakes.answers) if self.fakes.fresh else None

    def _document(self, prompt: str) -> str:
        sha1 = SHA1.search(prompt).group(1)
        corpus = self.fakes.corpus
        return next(n for n in corpus.texts if corpus.sha1(n) == sha1)

    def complete(self, prompt, image_paths=()):
        if image_paths:
            name = Path(image_paths[0]).name
            number = self._call("vision", name)
            return (
                self.fakes.corpus.texts[name]
                + f"\n[read by {self.config_hash}, answer {number}]"
            )
        if prompt.startswith("You review a genealogical extraction"):
            self._call("critique", None)
            return (
                '{"target": "e1", "issue": "a concern"}\n'
                '{"done": true, "findings_emitted": 1}'
            )
        return self._chunk(prompt)

    def open_conversation(self, prompt):
        number = self._call("manifest", self._document(prompt))
        note = json.dumps({"note": f"answer {number}"}) + "\n"
        if prompt.startswith("You segment a document transcription"):
            lines = re.findall(r"^(\d+): ", prompt, re.MULTILINE)
            unit = {
                "id": "u01",
                "label": "the whole text",
                "first_line": 1,
                "last_line": int(lines[-1]),
            }
            answer = {"units": [unit]}
        else:
            people = [] if self.fakes.refuse else u.PEOPLE[: self.fakes.people]
            answer = {
                "entities": [
                    {"id": f"e{n}", "label": person.label, "kind": "human"}
                    for n, person in enumerate(people, 1)
                ]
            }
        return note + json.dumps(answer), str(uuid.uuid4())

    def fork(self, handle, prompt):
        return self._chunk(prompt)

    def _chunk(self, prompt: str) -> str:
        name = self._document(prompt)
        number = self._call("chunk", name)
        seed = self.config_hash + ("" if self.fakes.same_claims else name)
        year = 1836 + int(hashlib.sha1(seed.encode()).hexdigest(), 16) % 50
        if number is not None and not self.fakes.same_claims:
            year = 1900 + number % 100
        claim = {
            "subject": {"kind": "local", "local_id": "e1", "label": "Eugen"},
            "property": "date_of_birth",
            "value": {"type": "time", "year": year},
            "source": {
                "artifact": {
                    "kind": "artifact",
                    "sha1": self.fakes.corpus.sha1(name),
                }
            },
        }
        return json.dumps(claim) + '\n{"done": true, "claims_emitted": 1}'

    def keep_conversation(self, handle):
        return {f"fictional-project/{handle}.jsonl": b"{}"}

    def resume_conversation(self, prompt, response, files):
        transcripts = [name for name in files if name.endswith(".jsonl")]
        if len(transcripts) != 1:
            raise ValueError("the kept files hold no single transcript")
        return str(uuid.UUID(Path(transcripts[0]).stem))

    def close_conversation(self, handle):
        pass

    def close(self):
        self.fakes.closed += 1


@pytest.fixture
def engine(request, monkeypatch):
    """The fakes of test_scratch.py (a claude CLI installed and logged in,
    a full model id), with this module's engine behind the client
    factory: one person to a manifest, and so one chunk to a document."""
    fake = request.getfixturevalue("fakes")
    fake.people = 1
    fake.cli = CLI
    fake.fresh = True
    fake.refuse = False
    fake.same_claims = False
    fake.answers = itertools.count(1)
    fake.closed = 0

    def make_client(engine, *, effort=None, model=None, **kwargs):
        return Engine(fake, effort=effort, model=model)

    monkeypatch.setattr(llm, "make_client", make_client)
    return fake


class Quiet(Estimator):
    """The estimator with the defaults test_scratch.py's Driver takes."""

    def __init__(self, folder, run_dir, **kw):
        kw.setdefault("exclusions", ExclusionList())
        kw.setdefault("log", quiet)
        kw.setdefault("min_free", 0)
        super().__init__(folder, run_dir, **kw)


def estimate_of(tmp_path, corpus, fake, **kwargs) -> estimate.Estimate:
    """`yizkr scratch estimate` over the corpus, as the command counts it
    (scratch_estimate)."""
    fake.corpus = corpus
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return estimate.scratch_estimate(
        corpus.folder, runs=runs_dir(tmp_path), estimator=Quiet, **kwargs
    )


def run(tmp_path, corpus, fake, **kwargs) -> Counter:
    """`yizkr scratch run` over the corpus: the engine calls it made, per
    stage."""
    fake.corpus = corpus
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    kwargs.setdefault("driver", Driver)
    before = len(fake.calls)
    assert (
        scratch.scratch_run(corpus.folder, runs=runs_dir(tmp_path), **kwargs)
        == EXIT_OK
    )
    return calls_since(fake, before)


def calls_since(fake, before: int) -> Counter:
    return Counter(STAGE_OF[what] for what, _name in fake.calls[before:])


def per_stage(counted: estimate.Estimate, count: str) -> Counter:
    """One count of the estimate's (a StageCalls field or property), per
    stage, where it is not 0."""
    return Counter(
        {
            stage: getattr(calls, count)
            for stage, calls in counted.stages.items()
            if getattr(calls, count)
        }
    )


def one_letter(tmp_path) -> Corpus:
    corpus = Corpus(tmp_path / "corpus")
    corpus.text(TEXT, "postcard-1938")
    return corpus


def entries(cache: Path, *, role=None, call=None, attempt=None) -> list[Path]:
    """The live entries in the cache, those of `role`, `call` and
    `attempt` when they are given."""
    found = []
    for path in sorted(cache.glob("*/*.json")):
        if not ENTRY.match(path.name):
            continue
        entry = json.loads(path.read_text())
        components = entry.get("components") or {}
        if (
            (role is None or entry["provenance"]["role"] == role)
            and (call is None or components.get("call") == call)
            and (attempt is None or components.get("attempt") == attempt)
        ):
            found.append(path)
    return found


def answer_with(path: Path, response: str) -> None:
    """Give a cache entry another response, as a stricter check or parser
    finds one kept from before it."""
    entry = json.loads(path.read_text())
    entry["response"] = response
    path.write_text(json.dumps(entry))


def tree(*roots: Path) -> dict:
    """Every file and folder under `roots`: its bytes (a file's) and its
    mode."""
    found = {}
    for root in roots:
        for path in sorted([root, *root.rglob("*")]):
            data = path.read_bytes() if path.is_file() else None
            found[path] = (data, stat.S_IMODE(path.stat().st_mode))
    return found


# -- the counts, held against the calls the run makes -------------------------


class TestCounts:
    def test_a_first_run_is_counted_as_it_runs(self, tmp_path, engine):
        """An empty cache: every call reaches the engine. The images'
        readings and the letters' entity manifests are known calls; what
        follows them is estimated, by the letters' length and a scan's
        typical count, as the run then makes it. The manifest tier's
        fallback is the bound: three more calls at most for each."""
        corpus = small(tmp_path)
        counted = estimate_of(tmp_path, corpus, engine)
        assert not counted.resumed
        assert counted.cli_version == CLI
        assert per_stage(counted, "known") == {
            "transcription": 2,
            "extraction": 2,
        }
        assert per_stage(counted, "estimated_documents") == {
            "extraction": 4,
            "critique": 4,
        }
        assert per_stage(counted, "more") == {"extraction": 4 * 3}
        assert counted.total().hits == 0
        made = run(tmp_path, corpus, engine)
        assert made == {"transcription": 2, "extraction": 8, "critique": 4}
        assert per_stage(counted, "engine_calls") == made

    def test_a_run_the_cache_answers_asks_the_engine_nothing(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert counted.total().engine_calls == 0
        assert counted.total().more == 0
        assert per_stage(counted, "hits") == {
            "transcription": 2,
            "extraction": 8,
            "critique": 4,
        }
        assert per_stage(counted, "made") == {
            "transcription": 4,
            "extraction": 4,
            "critique": 4,
        }
        assert run(tmp_path, corpus, engine, new=True) == Counter()

    def test_another_effort_is_known_to_each_call_it_changes(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        counted = estimate_of(
            tmp_path, corpus, engine, new=True, review_effort="max"
        )
        assert per_stage(counted, "known") == {"critique": 4}
        assert counted.total().estimated == 0
        assert counted.total().more == 0
        made = run(tmp_path, corpus, engine, new=True, review_effort="max")
        assert made == {"critique": 4}

    def test_another_extraction_model_is_known_to_its_manifests(
        self, tmp_path, engine
    ):
        """The readings stand in the cache; each entity manifest is the
        engine's to answer, and the chunks after it are counted as the
        run before measured them."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        other = {"new": True, "extract_model": "claude-sonnet-5"}
        counted = estimate_of(tmp_path, corpus, engine, **other)
        assert per_stage(counted, "known") == {"extraction": 4}
        assert per_stage(counted, "hits") == {"transcription": 2}
        extraction = counted.stages["extraction"]
        assert (extraction.estimated, extraction.measured) == (4, 4)
        assert extraction.more == 4 * (FIRST_TURNS - 1)
        made = run(tmp_path, corpus, engine, **other)
        assert made == {"extraction": 8, "critique": 4}
        assert per_stage(counted, "engine_calls") == made

    def test_a_claude_cli_updated_misses_every_key(self, tmp_path, engine):
        corpus = small(tmp_path)
        first = run(tmp_path, corpus, engine)
        engine.cli = "9.9.10"
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert counted.cli_version == "9.9.10"
        assert counted.total().hits == 0
        # every extraction as the run before measured it
        assert counted.stages["extraction"].measured == 4
        assert "each as the latest run to make it measured it" in "\n".join(
            estimate.describe(counted)
        )
        assert per_stage(counted, "engine_calls") == first
        assert run(tmp_path, corpus, engine, new=True) == first

    def test_a_critique_the_documents_share_is_asked_once(
        self, tmp_path, engine
    ):
        """Two batches alike ask the critic the same: the run asks the
        engine once, and its cache answers the other."""
        engine.same_claims = True
        corpus = Corpus(tmp_path / "corpus")
        corpus.text(TEXT, "postcard-1938")
        corpus.text(LETTER, "marriage-cert-1905")
        run(tmp_path, corpus, engine)
        counted = estimate_of(
            tmp_path, corpus, engine, new=True, review_effort="max"
        )
        critique = counted.stages["critique"]
        assert (critique.known, critique.hits) == (1, 1)
        assert run(
            tmp_path, corpus, engine, new=True, review_effort="max"
        ) == {"critique": 1}

    def test_an_extraction_cut_short_is_known_to_its_last_chunk(
        self, tmp_path, engine
    ):
        """Twelve people, two chunks; Ctrl-C at the second. Invoked again,
        the manifest and the first chunk are the cache's, and the second
        chunk forks the conversation the cache kept, with no call."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        cut = cut_short(tmp_path, corpus, engine)
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.resumed
        assert counted.run == cut
        assert per_stage(counted, "reused") == {"transcription": 1}
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (2, 1)
        assert (extraction.estimated, extraction.more) == (0, 0)
        # the batch holds a chunk the engine answers: its critique waits
        assert counted.stages["critique"].estimated == 1
        made = run(tmp_path, corpus, engine)
        assert made == {"extraction": 1, "critique": 1}
        assert per_stage(counted, "engine_calls") == made

    @pytest.mark.parametrize("fresh", [False, True])
    def test_a_conversation_whose_session_is_gone_is_replayed(
        self, tmp_path, engine, fresh
    ):
        """No session kept: the second chunk replays the manifest, a
        known call. A replay that answers the same serves the fork; one
        that answers differently starts the extraction over, and its
        chunks the cache answered are asked too: the bound."""
        engine.people, engine.fresh = 12, fresh
        corpus = one_letter(tmp_path)
        cut_short(tmp_path, corpus, engine)
        for kept in (tmp_path / "cache").rglob("*.session"):
            shutil.rmtree(kept)
        counted = estimate_of(tmp_path, corpus, engine)
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (2, 2)
        assert extraction.more == 1
        made = run(tmp_path, corpus, engine)
        assert made["extraction"] == 2 + fresh
        assert extraction.known <= made["extraction"]
        assert made["extraction"] <= extraction.known + extraction.more

    def test_a_kept_critique_the_run_rejects_is_asked_again(
        self, tmp_path, engine
    ):
        """A critique the cache kept without its sentinel, as a stricter
        parser finds one: a hit the run rejects, then a call. The
        estimate moves nothing aside."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        for critique in entries(cache, role="critique"):
            answer_with(critique, '{"target": "e1", "issue": "cut off"}')
        before = tree(cache)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert tree(cache) == before
        critique = counted.stages["critique"]
        assert (critique.hits, critique.known) == (4, 4)
        assert counted.total().engine_calls == 4
        assert run(tmp_path, corpus, engine, new=True) == {"critique": 4}

    def test_a_kept_reading_the_check_fails_now_is_asked_again(
        self, tmp_path, engine
    ):
        """A reading the cache kept that the transcription check fails:
        a hit the run rejects, then a call, which a new reading follows."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        for reading in entries(cache, role="transcribe"):
            answer_with(reading, "As an AI model I cannot read this.")
        before = tree(cache)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert tree(cache) == before
        transcription = counted.stages["transcription"]
        assert (transcription.hits, transcription.known) == (2, 2)
        # the images' extractions follow a reading the engine makes
        assert counted.stages["extraction"].estimated_documents == 2
        made = run(tmp_path, corpus, engine, new=True)
        assert made["transcription"] == 2
        assert per_stage(counted, "engine_calls") == made

    def test_a_refused_manifest_the_cache_kept_leaves_fewer_first_turns(
        self, tmp_path, engine
    ):
        """The manifest's first attempt is the cache's, and refused; the
        second is the engine's: at most two more first turns, the
        segmentation's."""
        engine.refuse = True
        corpus = one_letter(tmp_path)
        run(tmp_path, corpus, engine)
        (second,) = entries(tmp_path / "cache", call="open", attempt=1)
        second.unlink()
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (1, 1)
        assert extraction.more == FIRST_TURNS - 2

    def test_a_refresh_is_known_to_every_call_of_its_role(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        refresh = {"resume": LATEST, "refresh": ["critique"]}
        counted = estimate_of(tmp_path, corpus, engine, **refresh)
        assert per_stage(counted, "known") == {"critique": 4}
        assert per_stage(counted, "reused") == {
            "transcription": 4,
            "extraction": 4,
        }
        assert run(tmp_path, corpus, engine, **refresh) == {"critique": 4}
        # made once in the run: invoked again with it, they stand
        again = estimate_of(tmp_path, corpus, engine, **refresh)
        assert per_stage(again, "reused") == {
            "transcription": 4,
            "extraction": 4,
            "critique": 4,
        }
        assert again.total().engine_calls == 0

    def test_a_resumed_run_counts_its_stale_work_alone(self, tmp_path, engine):
        """Taken to its transcriptions only, then invoked again: the
        readings stand, and each extraction's manifest is a known call."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.resumed
        assert per_stage(counted, "reused") == {"transcription": 4}
        assert per_stage(counted, "known") == {"extraction": 4}
        made = run(tmp_path, corpus, engine)
        assert made == {"extraction": 8, "critique": 4}
        assert per_stage(counted, "engine_calls") == made

    def test_until_counts_the_stages_up_to_it(self, tmp_path, engine):
        corpus = small(tmp_path)
        counted = estimate_of(tmp_path, corpus, engine, until="transcription")
        assert list(counted.stages) == ["transcription"]
        assert per_stage(counted, "engine_calls") == {"transcription": 2}

    def test_decks_gedcom_files_and_duplicates(self, tmp_path, engine):
        """A deck's slide text and a GEDCOM file make no call of their
        own; identical bytes are one document; a file that cannot be
        read takes no stage."""
        corpus = Corpus(tmp_path / "corpus")
        corpus.text(TEXT, "postcard-1938")
        corpus.deck("e-Hutmacherei.pptx", "gegr. 1863, Brunnenring 35")
        corpus.gedcom("f-example-family.ged")
        shutil.copy(corpus.folder / TEXT, corpus.folder / "g-copy.txt")
        unreadable = corpus.folder / "h-unreadable.txt"
        unreadable.write_text("Flörburg, 1912")
        unreadable.chmod(0)
        try:
            counted = estimate_of(tmp_path, corpus, engine)
        finally:
            unreadable.chmod(0o600)
        assert (counted.duplicates, counted.unreadable) == (1, 1)
        assert counted.stages["gedcom"] == StageCalls(made=1)
        transcription = counted.stages["transcription"]
        assert (transcription.made, transcription.engine_calls) == (2, 0)
        assert per_stage(counted, "known") == {"extraction": 2}

    def test_the_mean_for_a_kind_counts_a_document_no_run_measured(
        self, tmp_path, engine
    ):
        """Twelve people, two chunks a letter. A letter added since the
        run before takes its chunks from the mean for letters there, not
        from its length, which would give one."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        run(tmp_path, corpus, engine)
        corpus.text(LETTER, "marriage-cert-1905")
        other = {"new": True, "extract_model": "claude-sonnet-5"}
        counted = estimate_of(tmp_path, corpus, engine, **other)
        extraction = counted.stages["extraction"]
        assert (extraction.known, extraction.estimated) == (2, 4)
        assert extraction.measured == 1
        assert run(tmp_path, corpus, engine, **other)["extraction"] == 6
        assert (
            "1 as the latest run to make them measured them, the others by "
            "the mean for their kind there or by their text's length"
        ) in "\n".join(estimate.describe(counted))

    def test_a_reading_that_fails_the_check_now_is_made_again(
        self, tmp_path, engine
    ):
        """A transcription view the check fails is not reused: the run
        makes it again, from the reading the cache kept, and the batch
        made from that reading stands."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        view = run_dir / "b-studio-photo-1890s" / "transcription.json"
        failing = json.loads(view.read_text())
        failing["text"] = "As an AI model I cannot read this."
        view.write_text(json.dumps(failing))
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        transcription = counted.stages["transcription"]
        assert (transcription.made, transcription.reused) == (1, 3)
        assert (transcription.hits, transcription.known) == (1, 0)
        assert per_stage(counted, "reused") == {
            "transcription": 3,
            "extraction": 4,
            "critique": 4,
        }

    def test_a_correction_makes_the_reading_and_what_follows_again(
        self, tmp_path, engine
    ):
        """A curator's correction: the transcription is made from it, with
        no call, and the batch made from the old reading no longer
        stands, so its entity manifest is a known call."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        corrected = run_dir / "b-studio-photo-1890s"
        (corrected / "transcription.corrected.txt").write_text(
            u.SOURCES[0].text + "\nPeiltal"
        )
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        transcription = counted.stages["transcription"]
        assert (transcription.made, transcription.engine_calls) == (1, 0)
        extraction = counted.stages["extraction"]
        assert (extraction.made, extraction.known) == (1, 1)
        made = run(tmp_path, corpus, engine, resume=LATEST)
        assert made == {"extraction": 2, "critique": 1}
        assert per_stage(counted, "engine_calls") == made

    def test_a_batch_made_again_the_same_keeps_its_critique(
        self, tmp_path, engine
    ):
        """A batch view gone, and made again from the cache's answers
        with the same content: the critique made from it stands."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        (run_dir / "c-marriage-cert-1905" / "batch.json").unlink()
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        extraction = counted.stages["extraction"]
        assert (extraction.made, extraction.hits) == (1, 2)
        assert per_stage(counted, "reused")["critique"] == 4
        assert counted.total().engine_calls == 0

    def test_a_batch_made_again_otherwise_makes_its_critique_again(
        self, tmp_path, engine
    ):
        """A batch view gone, and the chunk the cache kept for it answered
        otherwise since: the batch made again differs, and its critique,
        asked of the engine, does not stand."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        (run_dir / "c-marriage-cert-1905" / "batch.json").unlink()
        letter = corpus.sha1(LETTER)
        for path in entries(tmp_path / "cache", role="extract", call="fork"):
            entry = json.loads(path.read_text())
            if letter in entry["response"]:
                answer_with(path, re.sub(r'"year": \d+', '"year": 1837',
                                         entry["response"]))  # fmt: skip
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        critique = counted.stages["critique"]
        assert (critique.made, critique.reused, critique.known) == (1, 3, 1)
        assert run(tmp_path, corpus, engine, resume=LATEST) == {"critique": 1}

    def test_a_letter_s_length_counts_its_chunks(self, tmp_path, engine):
        """No run to measure it: a letter of some 3000 characters is
        counted two chunks, as its eighteen people make two."""
        engine.people = len(u.PEOPLE)
        corpus = Corpus(tmp_path / "corpus")
        long = "\n".join(source.text for source in u.SOURCES)[:3000]
        (corpus.folder / TEXT).write_text(long)
        corpus.texts[TEXT] = long
        counted = estimate_of(tmp_path, corpus, engine)
        extraction = counted.stages["extraction"]
        assert (extraction.known, extraction.estimated) == (1, 2)
        assert run(tmp_path, corpus, engine)["extraction"] == 3

    def test_a_changed_source_is_made_afresh(self, tmp_path, engine):
        """A source whose bytes changed has its directory set aside by the
        run, and so has one whose records are gone, so none of their
        views stands."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        corpus.text(LETTER, "directory-1912")
        # and a directory whose state.json is gone: nothing names the
        # artifact its files were made for
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        (run_dir / "a-postcard-1938" / "state.json").unlink()
        counted = estimate_of(tmp_path, corpus, engine, until="transcription")
        transcription = counted.stages["transcription"]
        assert (transcription.made, transcription.reused) == (2, 2)

    def test_a_stage_that_fails_as_the_cache_answers_it(
        self, tmp_path, engine
    ):
        """A text whose transcription the check fails fails its document:
        no extraction or critique follows it, in the run or the count."""
        corpus = small(tmp_path)
        (corpus.folder / "e-note.txt").write_text(
            "As an AI model I cannot read this."
        )
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.stages["transcription"].failing == 1
        assert counted.stages["extraction"].made == 4
        assert "as the cache answers it: 1 transcription." in "\n".join(
            estimate.describe(counted)
        )

    def test_a_stage_a_run_did_not_finish_measures_nothing(
        self, tmp_path, engine
    ):
        """Ctrl-C at the entity manifest: the stage cut short made one
        call, which is not its measure. Its chunks are counted by the
        letter's length, as the run then makes them."""

        def hook(what, name):
            if what == "manifest":
                raise KeyboardInterrupt

        engine.hook, engine.corpus = hook, one_letter(tmp_path)
        corpus = engine.corpus
        with pytest.raises(KeyboardInterrupt):
            scratch.scratch_run(
                corpus.folder,
                runs=runs_dir(tmp_path),
                driver=Driver,
                cache_dir=tmp_path / "cache",
            )
        engine.hook = None
        counted = estimate_of(tmp_path, corpus, engine)
        extraction = counted.stages["extraction"]
        assert (extraction.known, extraction.estimated) == (1, 1)
        assert extraction.measured == 0
        made = run(tmp_path, corpus, engine)
        assert made["extraction"] == 2

    def test_chunks_asked_inline_are_known_too(self, tmp_path, request):
        """An engine that holds no conversations asks each chunk with the
        document inline (test_scratch.py's): a chunk the cache lost is a
        known call, and the chunks after it are keyed too."""
        plain = request.getfixturevalue("fakes")
        corpus = small(tmp_path)
        plain.corpus = corpus
        driver_calls = len(plain.calls)
        assert (
            scratch.scratch_run(
                corpus.folder,
                runs=runs_dir(tmp_path),
                driver=Driver,
                cache_dir=tmp_path / "cache",
            )
            == EXIT_OK
        )
        assert len(plain.calls) > driver_calls
        for path in entries(tmp_path / "cache", role="extract"):
            if '"entities"' not in json.loads(path.read_text())["response"]:
                path.unlink()
        counted = estimate_of(tmp_path, corpus, plain, new=True)
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (4, 4)
        assert extraction.estimated == 0
        before = len(plain.calls)
        assert (
            scratch.scratch_run(
                corpus.folder,
                runs=runs_dir(tmp_path),
                driver=Driver,
                cache_dir=tmp_path / "cache",
                new=True,
            )
            == EXIT_OK
        )
        chunks = [call for call in plain.calls[before:] if call[0] == "chunk"]
        assert len(chunks) == extraction.known

    def test_a_gedcom_batch_stands_unless_refreshed(self, tmp_path, engine):
        corpus = one_letter(tmp_path)
        corpus.gedcom("f-example-family.ged")
        run(tmp_path, corpus, engine)
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.stages["gedcom"] == StageCalls(reused=1)
        refreshed = estimate_of(
            tmp_path, corpus, engine, resume=LATEST, refresh=["extract"]
        )
        assert refreshed.stages["gedcom"] == StageCalls(made=1)


def cut_short(tmp_path, corpus, fake) -> Path:
    """A run cut short by Ctrl-C at the letter's second chunk, one chunk
    at a time; its run directory."""
    chunks = []

    def hook(what, name):
        if what == "chunk":
            chunks.append(name)
            if len(chunks) == 2:
                raise KeyboardInterrupt

    fake.hook, fake.corpus = hook, corpus
    with pytest.raises(KeyboardInterrupt):
        scratch.scratch_run(
            corpus.folder,
            runs=runs_dir(tmp_path),
            driver=Driver,
            cache_dir=tmp_path / "cache",
            chunk_concurrency=1,
        )
    fake.hook = None
    return scratch.runs_in(runs_dir(tmp_path))[-1]


# -- what the estimate does not do --------------------------------------------


class TestNothingTouched:
    def test_it_writes_nothing_and_asks_nothing(
        self, tmp_path, engine, request
    ):
        """After a run cut short, the estimate reads the run and the cache
        and writes neither, and asks the engine and the wiki nothing. It
        makes the run's engine clients, for what their keys name, and
        closes them."""
        asked = request.getfixturevalue("refusals")
        engine.people = 12
        corpus = one_letter(tmp_path)
        cut_short(tmp_path, corpus, engine)
        runs, cache = runs_dir(tmp_path), tmp_path / "cache"
        before, calls = tree(runs, cache), len(engine.calls)
        closed = engine.closed
        counted = estimate_of(tmp_path, corpus, engine)
        assert tree(runs, cache) == before
        assert counted.stages["extraction"].known == 1
        assert len(engine.calls) == calls
        assert asked.asked == []
        assert engine.closed - closed == 3

    def test_it_holds_nothing_a_running_driver_holds(
        self, tmp_path, engine, holder
    ):
        """A process of its own holds the runs directory and the cache, as
        a running driver does: a driver would be refused, and the
        estimate counts all the same, naming the holder."""
        runs, cache = runs_dir(tmp_path), tmp_path / "cache"
        for folder in (runs, cache):
            folder.mkdir(mode=0o700)
        pid = holder(runs / scratch.LOCK_FILE, cache / ".lock")
        with pytest.raises(scratch.ScratchRunInUse), scratch.hold(runs):
            pass
        counted = estimate_of(tmp_path, small(tmp_path), engine)
        assert counted.total().engine_calls == 14
        assert [line.split(" by ")[0] for line in counted.holders] == [
            f"the runs directory {runs}",
            f"the LLM cache {cache}",
        ]
        assert all(f"process {pid} " in line for line in counted.holders)

    def test_an_estimator_never_runs_the_run(self, tmp_path, engine):
        corpus = small(tmp_path)
        engine.corpus = corpus
        estimator = Quiet(
            corpus.folder, runs_dir(tmp_path) / "r", cache_dir=tmp_path / "c"
        )
        with pytest.raises(TypeError, match="estimate"):
            estimator.run()
        assert not runs_dir(tmp_path).exists()
        assert engine.calls == []

    def test_it_counts_the_cache_s_own_events(self):
        """The events the estimate counts are the cache's, by name."""
        from yizkr.pipeline import llmcache

        assert (estimate.HIT, estimate.MISS, estimate.REPLAY) == (
            llmcache.HIT,
            llmcache.MISS,
            llmcache.REPLAY,
        )

    def test_a_new_run_makes_no_directory(self, tmp_path, engine):
        corpus = small(tmp_path)
        counted = estimate_of(tmp_path, corpus, engine)
        assert not runs_dir(tmp_path).exists()
        assert not (tmp_path / "cache").exists()
        assert counted.run.parent == runs_dir(tmp_path)
        assert counted.holders == []


# a process that holds lock files as a scratch driver and the LLM cache do:
# flock's lock on each, and a record of itself in it
HOLD = """
import fcntl, json, os, socket, sys, time
held = []
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {"pid": os.getpid(), "host": socket.gethostname(),
              "command": "a fictional driver", "since": "2026-09-28T09:00"}
    os.ftruncate(fd, 0)
    os.pwrite(fd, (json.dumps(record) + "\\n").encode(), 0)
    held.append(fd)
print("held", flush=True)
time.sleep(600)
"""


@pytest.fixture
def holder():
    """Start a process that holds the lock files given, until the test
    ends, and return its pid."""
    started = []

    def hold(*locks: Path) -> int:
        process = subprocess.Popen(
            [sys.executable, "-c", HOLD, *map(str, locks)],
            stdout=subprocess.PIPE,
            text=True,
        )
        started.append(process)
        assert process.stdout.readline().strip() == "held"
        return process.pid

    yield hold
    for process in started:
        process.kill()
        process.wait()
        process.stdout.close()


# -- refusals -----------------------------------------------------------------


class TestRefusals:
    def test_no_never_ingest_list(self, tmp_path, engine):
        with pytest.raises(ExclusionConfigError):
            estimate_of(tmp_path, small(tmp_path), engine, exclusions=None)

    def test_a_model_the_keys_cannot_name(self, tmp_path, engine):
        with pytest.raises(PreflightFailed, match="alias"):
            estimate_of(
                tmp_path, small(tmp_path), engine, extract_model="sonnet"
            )

    def test_other_settings_for_a_resumed_run(self, tmp_path, engine):
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        with pytest.raises(RunSettingsConflict, match="review_effort"):
            estimate_of(tmp_path, corpus, engine, review_effort="max")

    def test_a_claude_cli_updated_during_a_run(self, tmp_path, engine):
        """Another CLI than the run recorded: the run is refused as its
        first stage starts an engine, unless no stage is left to."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        engine.cli = "9.9.10"
        with pytest.raises(RunSettingsConflict, match="engine.cli_version"):
            estimate_of(tmp_path, corpus, engine)
        counted = estimate_of(tmp_path, corpus, engine, until="transcription")
        assert per_stage(counted, "reused") == {"transcription": 4}

    def test_a_run_past_its_barrier_on_other_code(
        self, tmp_path, engine, monkeypatch
    ):
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        run_dir = scratch.runs_in(runs_dir(tmp_path))[-1]
        other = dict(RunDir(run_dir).manifest()["code"], commit="f" * 40)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.past_barrier_only
        assert counted.stages == {}
        with pytest.raises(ScratchRefused, match="--refresh"):
            estimate_of(
                tmp_path, corpus, engine, resume=LATEST, refresh=["extract"]
            )

    def test_a_pdf_without_the_renderer(self, tmp_path, engine, monkeypatch):
        from yizkr.pipeline import transcribe

        corpus = small(tmp_path)
        (corpus.folder / "e-card-1945.pdf").write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(transcribe.shutil, "which", lambda tool: None)
        with pytest.raises(PreflightFailed, match="poppler"):
            estimate_of(tmp_path, corpus, engine)

    def test_an_engine_client_that_cannot_start(
        self, tmp_path, engine, monkeypatch
    ):
        def no_cli(*args, **kwargs):
            raise RuntimeError("the Claude Code CLI ('claude') is not found")

        monkeypatch.setattr(llm, "make_client", no_cli)
        with pytest.raises(ScratchRefused, match="cannot start"):
            estimate_of(tmp_path, small(tmp_path), engine)


# -- the report ---------------------------------------------------------------


class TestReport:
    def test_a_line_per_stage_the_total_and_what_is_known(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        lines = estimate.describe(estimate_of(tmp_path, corpus, engine))
        assert lines[0] == f"Estimate of a new run in {runs_dir(tmp_path)}"
        assert lines[1] == (
            f"Keyed as the run keys its calls: claude CLI {CLI}, the LLM "
            f"cache {tmp_path / 'cache'}"
        )
        table = lines[lines.index("") + 1 :]
        assert table[0].split() == [
            "stage",
            "made",
            "reused",
            "cache",
            "hits",
            "engine",
            "calls",
            "estimated",
        ]
        assert table[1].split() == ["transcription", "4", "0", "0", "2", "0"]
        assert table[2].split() == ["extraction", "4", "0", "0", "8", "6"]
        assert table[3].split() == ["critique", "4", "0", "0", "4", "4"]
        assert table[4].split() == ["total", "12", "0", "0", "14", "10"]
        text = "\n".join(lines)
        assert (
            "Engine calls: 14, 4 known and 10 estimated, and at most 12 more."
        ) in text
        assert (
            "6 extraction calls for 4 documents, by the mean for their kind "
            "in the latest run, or by their text's length; 4 critiques, one "
            "each."
        ) in text
        assert "- Not counted:" in text

    def test_holders_failures_and_what_the_preflight_would_refuse(
        self, tmp_path, engine, monkeypatch
    ):
        counted = estimate.Estimate(
            run=tmp_path / "runs" / "r",
            resumed=True,
            cache=tmp_path / "cache",
            stages={"transcription": StageCalls(made=2, failing=1)},
            duplicates=2,
            unreadable=1,
            corpus_stages=["clusters"],
            holders=["the runs directory x by process 7"],
            preflight=["the claude CLI has no login"],
        )
        text = "\n".join(estimate.describe(counted))
        assert f"run r in {tmp_path / 'runs'}, invoked again" in text
        assert "as the cache answers it: 1 transcription." in text
        assert "- No stage for 2 duplicates and 1 unreadable." in text
        assert "- Not estimated: the corpus stages clusters." in text
        assert "- Held now: the runs directory x by process 7." in text
        assert "would refuse it now: the claude CLI has no login" in text

    def test_what_the_run_would_want_is_noted(
        self, tmp_path, engine, monkeypatch
    ):
        """No login and a pacing setting the engine would refuse: the run's
        preflight would refuse it, and the estimate counts it all the
        same."""
        monkeypatch.setattr(scratch, "claude_logged_in", lambda: False)
        monkeypatch.setenv("YIZKR_CLAUDE_RESERVE", "2")
        counted = estimate_of(tmp_path, small(tmp_path), engine)
        assert len(counted.preflight) == 2
        assert "YIZKR_CLAUDE_RESERVE" in counted.preflight[0]
        assert "no login" in counted.preflight[1]
        assert counted.total().engine_calls == 14


# -- the command line ---------------------------------------------------------


def parsed(*argv: str):
    return cli.build_parser().parse_args(["scratch", *argv])


class TestCommandLine:
    def test_it_takes_the_options_of_scratch_run(self):
        def options(action):
            return {
                key: value
                for key, value in vars(parsed(action)).items()
                if key not in ("func", "action")
            }

        assert options("estimate") == options("run")
        assert parsed("estimate").func is cli.cmd_scratch_estimate

    def test_it_passes_them_on_and_prints_the_estimate(
        self, tmp_path, monkeypatch, capsys
    ):
        env = tmp_path / "test.env"
        env.write_text(f"YIZKR_EXCLUDE_FILE={tmp_path / 'never.txt'}\n")
        (tmp_path / "never.txt").write_text("")
        folder = tmp_path / "corpus"
        folder.mkdir()
        seen = {}

        def scratch_estimate(corpus, **options):
            seen.update(options, corpus=corpus)
            return estimate.Estimate(
                run=tmp_path / "runs" / "r", resumed=False, cache=Path("c")
            )

        monkeypatch.setattr(estimate, "scratch_estimate", scratch_estimate)
        args = cli.build_parser().parse_args(
            [
                "--env-file", str(env), "scratch", "estimate",
                "--corpus", str(folder), "--runs", "my-runs",
                "--resume", "r1", "--until", "extraction",
                "--refresh", "extract", "--cache", "my-cache",
                "--language", "de", "--fallback-language", "fr",
                "--review-effort", "max", "--extract-model", "inherit",
            ]
        )  # fmt: skip
        assert args.func(args) == EXIT_OK
        assert seen["corpus"] == folder
        assert (seen["runs"], seen["resume"], seen["new"]) == (
            Path("my-runs"),
            "r1",
            False,
        )
        assert seen["until"] == "extraction"
        assert seen["refresh"] == ["extract"]
        assert seen["cache_dir"] == Path("my-cache")
        assert (seen["language"], seen["fallback_language"]) == ("de", "fr")
        assert seen["review_effort"] == "max"
        assert seen["extract_model"] is None
        assert seen["env_file"] == env
        assert isinstance(seen["exclusions"], ExclusionList)
        out = capsys.readouterr().out
        assert out.startswith("Estimate of a new run in ")

    @pytest.mark.parametrize(
        "refusal",
        [
            ScratchRefused("no scratch run in my-runs"),
            RunSettingsConflict(Path("r"), [("config.language", "de", "en")]),
            llm.PassPaused("the CLI's global config sets variables"),
        ],
        ids=["refused", "settings", "paused"],
    )
    def test_a_refusal_is_said_and_exits_1(
        self, tmp_path, monkeypatch, capsys, refusal
    ):
        env = tmp_path / "test.env"
        env.write_text(f"YIZKR_EXCLUDE_FILE={tmp_path / 'never.txt'}\n")
        (tmp_path / "never.txt").write_text("")
        folder = tmp_path / "corpus"
        folder.mkdir()

        def scratch_estimate(corpus, **options):
            raise refusal

        monkeypatch.setattr(estimate, "scratch_estimate", scratch_estimate)
        args = cli.build_parser().parse_args(
            ["--env-file", str(env), "scratch", "estimate", "--corpus",
             str(folder)]
        )  # fmt: skip
        assert args.func(args) == EXIT_FAILED
        assert str(refusal).splitlines()[0] in capsys.readouterr().err
