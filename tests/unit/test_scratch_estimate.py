"""Offline tests for the call estimate (#101): `yizkr scratch estimate`.

A fake claude-code engine behind the real LLM cache makes scratch runs,
as in test_scratch.py, and holds conversations whose sessions it keeps
and resumes as the claude CLI does. Asked again, it answers afresh, as a
model does. The estimate counts the calls the next invocation is to
make, and each test of TestCounts holds its counts against the calls
that invocation then makes: the known counts exactly, the estimated ones
where the fake makes as many calls as the estimate expects, and where
only the engine's answers decide, the calls within the estimate and its
bound. The claude CLI and its login are faked where they are looked
for; nothing runs the CLI or calls a model. Documents are the fictional
universe's (docs/EXAMPLES.md).
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.records import record_answer
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
from yizkr.pipeline.chunking import parse_chunk_response, parse_chunk_unread
from yizkr.pipeline.estimate import FIRST_TURNS, Estimator, StageCalls
from yizkr.pipeline.llmcache import CacheRefused
from yizkr.pipeline.orchestrate import (
    CHECKED_ROUTES,
    EXCERPT,
    PAGED,
    Orchestrator,
)
from yizkr.pipeline.rundir import RunDir, RunSettingsConflict
from yizkr.pipeline.scratch import (
    EXIT_FAILED,
    EXIT_OK,
    LATEST,
    PreflightFailed,
    ScratchRefused,
)

# the clustering stage's adjudication prompt, and a pair it asks of
ADJUDICATION_PROMPT = "You adjudicate IDENTITY for a family archive"
ADJUDICATED_PAIR = re.compile(r"^(\d+)\. clustering's comparison", re.M)
# what each of the fake engine's calls is made for
STAGE_OF = {
    "vision": "transcription",
    "manifest": "extraction",
    "chunk": "extraction",
    "critique": "critique",
    "adjudication": "clustering",
}
# the artifact's SHA-1, as the extraction prompts name it
SHA1 = re.compile(r"sha1\W*([0-9a-f]{40})")
# a live cache entry's file name
ENTRY = re.compile(r"^[0-9a-f]{64}\.json$")
# a stamp, as the cache names what it moves aside
REPLACED = "20260929T090000Z-1a2b3c4d"
# a lock record nested too deep for json to decode: RecursionError
# decoding it, never written by a holder (#533)
NESTED = "[" * 100_000


class Engine:
    """The claude-code engine as a run and the estimate see it: keyed by
    its model, effort and CLI version, and holding conversations, whose
    sessions it keeps and resumes as the claude CLI does (one transcript,
    named by its session id).

    Its answers: a reading of each image, noting what read it; an entity
    manifest of `fakes.people` people (ten to a chunk; the universe's, over
    again once they run out), or while `fakes.refuse` one of a single
    entity of a kind the manifest vocabulary lacks, which the extractor
    refuses (a manifest of no entity it accepts), and a segmentation of the
    whole text as one unit; a claim per chunk, whose year the
    configuration and the document pick (the configuration alone while
    `fakes.same_claims`); a critic with one concern; and an adjudicator
    of the clustering stage's ambiguous pairs (#106), uncertain of each,
    since the manifests' people have no mention records. While
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
        if prompt == scratch.PREFLIGHT_PROMPT and not image_paths:
            # the run's preflight (#455), which the estimate never makes
            self.fakes.preflights.append((self.model, self.effort))
            return "ready"
        if image_paths:
            name = Path(image_paths[0]).name
            number = self._call("vision", name)
            return record_answer(
                self.fakes.corpus.texts[name]
                + f"\n[read by {self.config_hash}, answer {number}]"
            )
        if prompt.startswith("You review a genealogical extraction"):
            self._call("critique", None)
            return (
                '{"target": "e1", "issue": "a concern"}\n'
                '{"done": true, "findings_emitted": 1}'
            )
        if prompt.startswith(ADJUDICATION_PROMPT):
            self._call("adjudication", None)
            return "\n".join(
                [
                    *(
                        json.dumps(
                            {
                                "pair": int(n),
                                "verdict": "uncertain",
                                "reason": "-",
                            }
                        )
                        for n in ADJUDICATED_PAIR.findall(prompt)
                    ),
                    '{"done": true}',
                ]
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
            people = [
                u.PEOPLE[n % len(u.PEOPLE)] for n in range(self.fakes.people)
            ]
            answer = {
                "entities": [
                    {"id": f"e{n}", "label": person.label, "kind": "human"}
                    for n, person in enumerate(people, 1)
                ]
                if not self.fakes.refuse
                else [
                    {"id": "e1", "label": "the reading room", "kind": "room"}
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


def run(tmp_path, corpus, fake, status=EXIT_OK, **kwargs) -> Counter:
    """`yizkr scratch run` over the corpus, ending with `status`: the
    engine calls it made, per stage."""
    fake.corpus = corpus
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    kwargs.setdefault("driver", Driver)
    before = len(fake.calls)
    assert (
        scratch.scratch_run(corpus.folder, runs=runs_dir(tmp_path), **kwargs)
        == status
    )
    return calls_since(fake, before)


def calls_since(fake, before: int) -> Counter:
    """The engine calls of the per-document stages since `before`, per
    stage: the clustering stage's adjudication is counted apart
    (adjudication_calls)."""
    return Counter(
        STAGE_OF[what]
        for what, _name in fake.calls[before:]
        if what != "adjudication"
    )


def adjudication_calls(fake) -> int:
    return sum(what == "adjudication" for what, _name in fake.calls)


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


def small_letters(tmp_path) -> Corpus:
    """Three letters."""
    corpus = one_letter(tmp_path)
    corpus.text(LETTER, "marriage-cert-1905")
    corpus.text("e-directory-1912.txt", "directory-1912")
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

    def test_the_clustering_stage_s_adjudication_is_counted(
        self, tmp_path, engine
    ):
        """The clustering stage calls the engine to adjudicate its
        ambiguous pairs (#106). Before any run has an identity map, the
        estimate says it is not estimated; once one has, it counts the
        calls that map planned, as up to so many."""
        corpus = small(tmp_path)
        counted = estimate_of(tmp_path, corpus, engine)
        assert (counted.callless_stages, counted.corpus_stages) == (
            [],
            ["clustering"],
        )
        assert counted.adjudication is None
        text = "\n".join(estimate.describe(counted))
        assert "- Not estimated: the corpus stages clustering." in text
        run(tmp_path, corpus, engine)
        asked = adjudication_calls(engine)
        assert asked >= 1
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert counted.corpus_stages == []
        calls, source = counted.adjudication
        assert calls >= asked
        text = "\n".join(estimate.describe(counted))
        assert f"- Up to {calls} adjudication call" in text
        assert f"identity map of run {source}" in text
        assert "Not estimated" not in text

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

    def test_alike_batches_of_two_transcriptions_ask_two_critiques(
        self, tmp_path, engine
    ):
        """Two batches alike, of documents whose transcriptions differ:
        each critique carries its own transcription (#607), so the run
        asks the engine twice, and the estimate knows both calls."""
        engine.same_claims = True
        corpus = Corpus(tmp_path / "corpus")
        corpus.text(TEXT, "postcard-1938")
        corpus.text(LETTER, "marriage-cert-1905")
        run(tmp_path, corpus, engine)
        counted = estimate_of(
            tmp_path, corpus, engine, new=True, review_effort="max"
        )
        critique = counted.stages["critique"]
        assert (critique.known, critique.hits) == (2, 0)
        assert run(
            tmp_path, corpus, engine, new=True, review_effort="max"
        ) == {"critique": 2}

    def test_a_critique_made_again_alone_is_the_driver_s_prompt(
        self, tmp_path, engine
    ):
        """Every critique view gone, the cache holding the answers: the
        estimate builds the driver's prompt, transcription and all, so
        each is a hit, and the run asks nothing (#607)."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        for view in run_dir.glob("*/critique.json"):
            view.unlink()
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        critique = counted.stages["critique"]
        assert (critique.made, critique.hits, critique.known) == (4, 4, 0)
        assert counted.total().engine_calls == 0
        assert run(tmp_path, corpus, engine, resume=LATEST) == Counter()

    def test_a_transcription_changed_under_an_alike_batch_is_critiqued(
        self, tmp_path, engine
    ):
        """A curator's correction that leaves the batch alike: the
        critique, which reads the transcription, does not stand (#607)."""
        engine.fresh = False
        engine.same_claims = True
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        (run_dir,) = scratch.runs_in(runs_dir(tmp_path))
        corrected = run_dir / "b-studio-photo-1890s"
        (corrected / "transcription.corrected.txt").write_text(
            u.SOURCES[0].text + "\nPeiltal"
        )
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        critique = counted.stages["critique"]
        assert (critique.made, critique.reused) == (1, 3)
        made = run(tmp_path, corpus, engine, resume=LATEST)
        assert made == {"extraction": 2, "critique": 1}
        assert per_stage(counted, "engine_calls") == made
        # the correction taken back: the batch the cache answers is alike
        # again, but the transcription changed, so the critique the cache
        # kept for the first reading is asked for, a hit
        (corrected / "transcription.corrected.txt").unlink()
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        extraction = counted.stages["extraction"]
        assert (extraction.made, extraction.hits) == (1, 2)
        critique = counted.stages["critique"]
        assert (critique.made, critique.reused, critique.hits) == (1, 3, 1)
        assert counted.total().engine_calls == 0
        assert run(tmp_path, corpus, engine, resume=LATEST) == Counter()

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

    def test_a_keep_cut_off_between_its_renames_is_resumed(
        self, tmp_path, engine
    ):
        """A keep publishes a session by moving the folder it finds at the
        answer's path aside and renaming its own in. One cut off between
        the two, as a killed process leaves it, leaves that path with no
        folder and the session in the folder it moved aside: both readers
        find it there, so the second chunk forks the conversation with no
        call and the estimate counts what the run then makes, rather than
        one counting a resume and the other replaying (#395)."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        cut = cut_short(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        for folder in sorted(cache.glob("*/*.session/*")):
            folder.rename(
                folder.with_name(f"{folder.name}.replaced-{REPLACED}")
            )
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.resumed
        assert counted.run == cut
        assert not per_stage(counted, "replays")
        made = run(tmp_path, corpus, engine)
        assert made == {"extraction": 1, "critique": 1}
        assert per_stage(counted, "engine_calls") == made

    @pytest.mark.parametrize("fresh", [False, True])
    def test_a_conversation_whose_session_is_gone_is_replayed(
        self, tmp_path, engine, fresh
    ):
        """No session kept: the second chunk replays the manifest, the one
        known call. The fork after it is estimated: a replay that answers
        the same serves it, one call; one that answers differently starts
        the extraction over, and asks the chunk the cache answered too,
        within the bound."""
        engine.people, engine.fresh = 12, fresh
        corpus = one_letter(tmp_path)
        cut_short(tmp_path, corpus, engine)
        for kept in (tmp_path / "cache").rglob("*.session"):
            shutil.rmtree(kept)
        counted = estimate_of(tmp_path, corpus, engine)
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (2, 1)
        assert (extraction.estimated, extraction.replays) == (1, 1)
        assert extraction.more == 1 + FIRST_TURNS - 1
        made = run(tmp_path, corpus, engine)
        assert made["extraction"] == 2 + fresh
        assert extraction.known <= made["extraction"]
        assert made["extraction"] <= (
            extraction.engine_calls + extraction.more
        )

    @pytest.mark.parametrize("people", [12, 25, 5])
    def test_a_replay_can_answer_another_number_of_chunks(
        self, tmp_path, engine, people
    ):
        """Three letters of two chunks each, their forks and sessions
        gone from the cache: each replays its manifest, and the replay
        answers two chunks, three or one. The replays alone are known,
        a floor; the run's calls stay within the estimate and its bound.
        """
        engine.people = 12
        corpus = small_letters(tmp_path)
        run(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        for fork in entries(cache, call="fork"):
            fork.unlink()
        for kept in cache.rglob("*.session"):
            shutil.rmtree(kept)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        extraction = counted.stages["extraction"]
        assert extraction.known == extraction.replays == 3
        assert (extraction.estimated, extraction.replay_calls) == (6, 6)
        assert extraction.more == 3 * (FIRST_TURNS - 1)
        engine.people = people
        made = run(tmp_path, corpus, engine, new=True)
        assert extraction.known <= made["extraction"]
        assert made["extraction"] <= (
            extraction.engine_calls + extraction.more
        )
        text = "\n".join(estimate.describe(counted))
        assert "6 chunk calls after 3 replayed conversations" in text

    def test_the_report_tells_the_forks_after_a_replay_apart(
        self, tmp_path, engine
    ):
        """A letter whose conversation is replayed, and a photograph whose
        reading is the engine's: the forks after the replay are counted
        apart from the extraction llm.estimate_calls counts."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        corpus.image(PHOTO, "studio-photo-1890s")
        run(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        for fork in entries(cache, call="fork"):
            if corpus.sha1(TEXT) in json.loads(fork.read_text())["response"]:
                fork.unlink()
        for kept in cache.rglob("*.session"):
            shutil.rmtree(kept)
        for reading in entries(cache, role="transcribe"):
            reading.unlink()
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        text = "\n".join(estimate.describe(counted))
        assert "2 chunk calls after 1 replayed conversation" in text
        assert "3 extraction calls for 1 document" in text
        made = run(tmp_path, corpus, engine, new=True)
        assert per_stage(counted, "engine_calls") == made

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
        # the run before measured the stage: both attempts, the
        # segmentation and its chunk; the two first turns keyed are off it
        assert (extraction.measured, extraction.estimated) == (1, 2)
        made = run(tmp_path, corpus, engine, new=True)
        assert extraction.known <= made["extraction"]
        assert made["extraction"] <= (
            extraction.engine_calls + extraction.more
        )

    def test_a_give_up_the_cache_kept_is_a_hit(self, tmp_path, engine):
        """The manifest's first attempt is a give-up the cache kept
        (#390): a hit, which the extraction takes as a refused answer, as
        the run does, so the second attempt is the engine's."""
        corpus = one_letter(tmp_path)
        run(tmp_path, corpus, engine)
        cache = tmp_path / "cache"
        (first,) = entries(cache, call="open", attempt=0)
        entry = json.loads(first.read_text())
        entry |= {"response": "", "gave_up": True}
        first.write_text(json.dumps(entry))
        before = tree(cache)
        counted = estimate_of(tmp_path, corpus, engine, new=True)
        assert tree(cache) == before
        extraction = counted.stages["extraction"]
        assert (extraction.hits, extraction.known) == (1, 1)
        assert extraction.more == FIRST_TURNS - 2
        made = run(tmp_path, corpus, engine, new=True)
        assert extraction.known <= made["extraction"]
        assert made["extraction"] <= (
            extraction.engine_calls + extraction.more
        )

    def test_the_empty_chunk_is_the_sentinel_alone(self):
        """The answer the estimate gives a chunk the cache cannot answer
        is the chunk prompt's terminal sentinel and nothing else (#390)."""
        assert json.loads(estimate.EMPTY_CHUNK) == {"done": True}
        assert parse_chunk_response(estimate.EMPTY_CHUNK) == []
        assert parse_chunk_unread(estimate.EMPTY_CHUNK) == []

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
        assert run(tmp_path, corpus, engine, **refresh) == Counter()

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
        made = run(tmp_path, corpus, engine, until="transcription")
        assert per_stage(counted, "engine_calls") == made

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
            if os.access(unreadable, os.R_OK):
                pytest.skip("this user reads any file")
            counted = estimate_of(tmp_path, corpus, engine)
            # the unreadable file fails the run's document
            made = run(tmp_path, corpus, engine, status=EXIT_FAILED)
        finally:
            unreadable.chmod(0o600)
        assert (counted.duplicates, counted.unreadable) == (1, 1)
        assert counted.stages["gedcom"] == StageCalls(made=1)
        transcription = counted.stages["transcription"]
        assert (transcription.made, transcription.engine_calls) == (2, 0)
        assert per_stage(counted, "known") == {"extraction": 2}
        assert per_stage(counted, "engine_calls") == made

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
            "the mean for their kind over the runs measured, or by their "
            "text's length"
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
        assert run(tmp_path, corpus, engine, resume=LATEST) == Counter()

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
        assert run(tmp_path, corpus, engine, resume=LATEST) == Counter()

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
        engine.people = 18
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
        made = run(tmp_path, corpus, engine, until="transcription")
        assert per_stage(counted, "engine_calls") == made

    def test_a_stage_that_fails_as_the_cache_answers_it(
        self, tmp_path, engine
    ):
        """A text whose transcription the check fails fails its document:
        no extraction or critique follows it, in the run or the count."""
        corpus = small(tmp_path)
        (corpus.folder / "e-note.txt").write_text(
            "pdftoppm: command not found"
        )
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.stages["transcription"].failing == 1
        assert counted.stages["extraction"].made == 4
        assert "as the cache answers it: 1 transcription." in "\n".join(
            estimate.describe(counted)
        )
        made = run(tmp_path, corpus, engine, status=EXIT_FAILED)
        assert per_stage(counted, "engine_calls") == made

    def test_a_measure_is_looked_for_past_the_newest_run(
        self, tmp_path, engine
    ):
        """Two chunks a letter. The newest run went no further than the
        readings, so the letter's extraction is measured by the run
        before it."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        run(tmp_path, corpus, engine)
        run(tmp_path, corpus, engine, new=True, until="transcription")
        other = {"new": True, "extract_model": "claude-sonnet-5"}
        counted = estimate_of(tmp_path, corpus, engine, **other)
        extraction = counted.stages["extraction"]
        assert extraction.measured == 1
        assert (extraction.known, extraction.estimated) == (1, 2)
        assert run(tmp_path, corpus, engine, **other)["extraction"] == 3

    def test_a_refresh_cut_short_leaves_the_measure_whole(
        self, tmp_path, engine
    ):
        """The letter's extraction finished (a manifest and two chunks),
        then a refresh of it was cut short at its manifest, one call: the
        measure is the finished one's, three."""
        engine.people = 12
        corpus = one_letter(tmp_path)
        run(tmp_path, corpus, engine)

        def hook(what, name):
            if what == "manifest":
                raise KeyboardInterrupt

        engine.hook = hook
        with pytest.raises(KeyboardInterrupt):
            scratch.scratch_run(
                corpus.folder,
                runs=runs_dir(tmp_path),
                driver=Driver,
                cache_dir=tmp_path / "cache",
                resume=LATEST,
                refresh=["extract"],
                chunk_concurrency=1,
            )
        engine.hook = None
        other = {"new": True, "extract_model": "claude-sonnet-5"}
        counted = estimate_of(tmp_path, corpus, engine, **other)
        extraction = counted.stages["extraction"]
        assert extraction.measured == 1
        assert (extraction.known, extraction.estimated) == (1, 2)
        assert run(tmp_path, corpus, engine, **other)["extraction"] == 3

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
        # every chunk is keyed, and the manifests are the cache's: nothing
        # is estimated, and no fallback is left to bound
        assert (extraction.estimated, extraction.more) == (0, 0)
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
        made = run(tmp_path, corpus, engine, resume=LATEST)
        assert per_stage(counted, "engine_calls") == made
        refresh = {"resume": LATEST, "refresh": ["extract"]}
        refreshed = estimate_of(tmp_path, corpus, engine, **refresh)
        assert refreshed.stages["gedcom"] == StageCalls(made=1)
        made = run(tmp_path, corpus, engine, **refresh)
        assert per_stage(refreshed, "engine_calls") == made


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


# -- the cache as it is -------------------------------------------------------

KEY = "a" * 64
OPENING = "b" * 64
# another opening answer's session, kept under the same key
OTHER = "c" * 64
SESSION = {
    "fictional-project/0c4f6a52-3d1e-4b8a-9f00-5a1e2b3c4d5e.jsonl": b"{}"
}
LISTED = dict.fromkeys(SESSION, b"")


def a_session(marker: str) -> dict[str, bytes]:
    """A session of one transcript, told from another by `marker`: what a
    keep of the same answer left before or after this one."""
    transcript = f"0c4f6a52-3d1e-4b8a-9f00-5a1e2b3c4d{marker}.jsonl"
    return {f"fictional-project/{transcript}": b"{}"}


def between_renames(monkeypatch, folder, read):
    """Have `read` called at the one moment a keep that replaces the
    session folder `folder` leaves nothing at its path: after it has
    renamed the folder it found aside, before it renames its own in.
    Patches os.rename itself, so the interleaving is the writer's own,
    at the exact point of it."""
    rename = os.rename

    def watched(src, dst, **kwargs):
        rename(src, dst, **kwargs)
        if Path(src) == folder:
            read()

    monkeypatch.setattr(os, "rename", watched)


class TestCacheAsIs:
    """The lock-free reader against a run that keeps sessions as it reads
    (#395)."""

    def session_dir(self, cache: Path) -> Path:
        from yizkr.pipeline.llmcache import EngineCache

        return EngineCache(cache)._session_dir(KEY, OPENING)

    def test_a_kept_session_replaced_meanwhile_is_read_whole(
        self, tmp_path, monkeypatch
    ):
        """A keep that replaces the session's folder renames the folder it
        finds aside and its own in, so for the moment between the two
        renames the answer's path holds no folder. A reader without the
        lock, listing it at exactly that moment, still sees the session's
        files: it reads the folder the keep moved aside (#395)."""
        from yizkr.pipeline.llmcache import EngineCache

        cache = tmp_path / "cache"
        writer = EngineCache(cache)
        writer.keep_session(KEY, OPENING, SESSION)
        # another answer of the same key, kept twice so that it has a
        # folder moved aside of its own: no read of this answer takes it
        for _ in range(2):
            writer.keep_session(KEY, OTHER, a_session("09"))
        as_is = llm.read_cache(cache)
        folder = self.session_dir(cache)
        seen = []
        between_renames(
            monkeypatch,
            folder,
            lambda: seen.append(
                (
                    folder.exists(),
                    as_is.kept_session(KEY, OPENING),
                )
            ),
        )
        writer.keep_session(KEY, OPENING, SESSION)
        # the reader ran where nothing was there to list, and read whole
        assert seen == [(False, LISTED)]
        assert as_is.kept_session(KEY, OPENING) == LISTED
        assert as_is.kept_session(KEY, OTHER) == dict.fromkeys(
            a_session("09"), b""
        )

    def test_the_first_session_kept_is_read_once_it_is_whole(
        self, tmp_path, monkeypatch
    ):
        """The first keep for an answer renames its folder in over
        nothing, so a reader that lists the path before that rename finds
        no session, and there is none: nothing kept is not an answer's
        session read empty (#395)."""
        from yizkr.pipeline.llmcache import EngineCache

        cache = tmp_path / "cache"
        as_is = llm.read_cache(cache)
        folder = self.session_dir(cache)
        seen = []
        rename = os.rename

        def watched(src, dst, **kwargs):
            if Path(dst) == folder:
                seen.append(as_is.kept_session(KEY, OPENING))
            rename(src, dst, **kwargs)

        monkeypatch.setattr(os, "rename", watched)
        EngineCache(cache).keep_session(KEY, OPENING, SESSION)
        assert seen == [{}]
        assert as_is.kept_session(KEY, OPENING) == LISTED

    def test_the_folder_fallen_back_on_is_the_newest_by_its_stamp(
        self, tmp_path, monkeypatch
    ):
        """A cache holds every folder a keep has moved aside, so an answer
        kept again and again has several, and their stamps need not follow
        the order they were moved in: two of one second, or a clock set
        back between them. The reader falls back to the newest by stamp,
        whichever that is of them (#395)."""
        from yizkr.pipeline import llmcache
        from yizkr.pipeline.llmcache import EngineCache

        stamps = iter(
            [
                "20260929T090100Z-1a2b3c4d",  # the session kept first
                "20260929T090300Z-5e6f7a8b",  # the second: the newest stamp
                "20260929T090200Z-9c0d1e2f",  # the third, a second behind
            ]
        )
        monkeypatch.setattr(llmcache, "_stamp", lambda: next(stamps))
        cache = tmp_path / "cache"
        writer = EngineCache(cache)
        for marker in ("01", "02", "03"):
            writer.keep_session(KEY, OPENING, a_session(marker))
        as_is = llm.read_cache(cache)
        seen = []
        between_renames(
            monkeypatch,
            self.session_dir(cache),
            lambda: seen.append(as_is.kept_session(KEY, OPENING)),
        )
        writer.keep_session(KEY, OPENING, SESSION)
        # the folders moved aside hold 01, 02 and 03, in that order, and
        # 02's stamp is the newest: the order they were moved in decides
        # nothing
        assert seen == [dict.fromkeys(a_session("02"), b"")]
        assert as_is.kept_session(KEY, OPENING) == LISTED

    def test_no_name_left_to_move_the_folder_aside_under(
        self, tmp_path, monkeypatch
    ):
        """Every stamp a keep is given names a folder moved aside already:
        it gives up with an OSError, which the caller that keeps a session
        warns about and goes on from, and the session kept stays where it
        was for both readers (#395)."""
        from yizkr.pipeline import llmcache
        from yizkr.pipeline.llmcache import EngineCache

        monkeypatch.setattr(llmcache, "_stamp", lambda: REPLACED)
        cache = tmp_path / "cache"
        writer = EngineCache(cache)
        writer.keep_session(KEY, OPENING, a_session("01"))
        writer.keep_session(KEY, OPENING, SESSION)
        with pytest.raises(OSError):
            writer.keep_session(KEY, OPENING, a_session("03"))
        folder = self.session_dir(cache)
        assert writer.kept_session(KEY, OPENING) == SESSION
        assert llm.read_cache(cache).kept_session(KEY, OPENING) == LISTED
        # the folder moved aside stands, and the keep left nothing behind
        assert sorted(path.name for path in folder.parent.iterdir()) == [
            OPENING,
            f"{OPENING}.replaced-{REPLACED}",
        ]

    def test_a_stamp_taken_already_moves_the_folder_aside_again(
        self, tmp_path, monkeypatch
    ):
        """A folder is moved aside under a name of its own: given a stamp
        that the folder moved aside by an earlier keep has taken, the keep
        is stamped again rather than failing, so no folder moved aside is
        replaced by another, and the reader that falls back on one finds it
        whole (#395)."""
        from yizkr.pipeline import llmcache
        from yizkr.pipeline.llmcache import EngineCache

        taken = "20260929T090000Z-1a2b3c4d"
        stamps = iter([taken, taken, "20260929T090000Z-5e6f7a8b"])
        monkeypatch.setattr(llmcache, "_stamp", lambda: next(stamps))
        writer = EngineCache(tmp_path / "cache")
        for _ in range(3):
            writer.keep_session(KEY, OPENING, SESSION)
        folder = self.session_dir(tmp_path / "cache")
        aside = sorted(folder.parent.glob(f"{folder.name}.replaced-*"))
        assert [path.name.rsplit("-", 1)[-1] for path in aside] == [
            "1a2b3c4d",
            "5e6f7a8b",
        ]
        assert writer.kept_session(KEY, OPENING) == SESSION
        for kept in [folder, *aside]:
            assert {
                path.relative_to(kept).as_posix()
                for path in kept.rglob("*")
                if path.is_file()
            } == set(SESSION)

    def test_a_reader_beside_a_run_that_keeps_sessions(self, tmp_path):
        """The same guarantee under a writer that keeps the session again
        and again in its own thread: every read is the whole session,
        never empty and never an error. Where the reads above hold it
        cannot fail, whatever the interleaving, and it costs a moment
        because no read waits for a folder (#395)."""
        from yizkr.pipeline.llmcache import EngineCache

        cache = tmp_path / "cache"
        EngineCache(cache).keep_session(KEY, OPENING, SESSION)
        as_is = llm.read_cache(cache)
        stop = threading.Event()
        failed = []

        def keep_again() -> None:
            writer = EngineCache(cache)
            try:
                while not stop.is_set():
                    writer.keep_session(KEY, OPENING, SESSION)
            except Exception as exc:  # a keep of its own that failed
                failed.append(exc)

        writer = threading.Thread(target=keep_again)
        writer.start()
        try:
            seen = [as_is.kept_session(KEY, OPENING) for _ in range(500)]
        finally:
            stop.set()
            writer.join()
        assert failed == []
        assert seen == [LISTED] * 500

    def test_a_session_kept_for_no_answer_under_the_key(self, tmp_path):
        as_is = llm.read_cache(tmp_path / "cache")
        assert as_is.kept_session(KEY, OPENING) == {}
        assert not (tmp_path / "cache").exists()

    def test_a_holder_that_has_ended_holds_nothing(self, tmp_path):
        """A lock file whose record names a process no longer running, as
        a holder that crashed leaves it: no holder is named."""
        ended = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            check=True,
        )
        record = {
            "pid": int(ended.stdout),
            "host": __import__("socket").gethostname(),
            "command": "a fictional driver",
            "since": "2026-09-28T09:00",
        }
        runs, cache = tmp_path / "runs", tmp_path / "cache"
        for folder in (runs, cache):
            folder.mkdir()
            (folder / ".lock").write_text(json.dumps(record) + "\n")
        assert scratch.runs_holder(runs) is None
        assert llm.cache_holder(cache) is None
        record["pid"] = os.getpid()
        for folder in (runs, cache):
            (folder / ".lock").write_text(json.dumps(record) + "\n")
        assert f"process {os.getpid()} " in scratch.runs_holder(runs)
        assert f"process {os.getpid()} " in llm.cache_holder(cache)

    def test_a_lock_record_nested_too_deep_holds_nothing(self, tmp_path):
        """A lock file whose record nests too deep for json to decode, as
        no writer makes it, raises RecursionError decoding: it names no
        holder, for the runs directory as for the cache (#533)."""
        runs, cache = tmp_path / "runs", tmp_path / "cache"
        for folder in (runs, cache):
            folder.mkdir()
            (folder / ".lock").write_text(NESTED)
        with pytest.raises(RecursionError):
            json.loads(NESTED)
        assert scratch.runs_holder(runs) is None
        assert llm.cache_holder(cache) is None

    @pytest.mark.parametrize("record", ["[]", "null", "7", '"a driver"'])
    def test_a_lock_record_that_is_no_object_holds_nothing(
        self, tmp_path, record
    ):
        """A lock record that decodes to no JSON object names no
        holder, for the runs directory as for the cache."""
        runs, cache = tmp_path / "runs", tmp_path / "cache"
        for folder in (runs, cache):
            folder.mkdir()
            (folder / ".lock").write_text(record + "\n")
        assert scratch.runs_holder(runs) is None
        assert llm.cache_holder(cache) is None

    def test_a_runs_lock_naming_no_holder_still_holds(self, tmp_path):
        """A nested record names no holder, yet the lock is flock's, not
        the record's: while it is held a driver is refused, and the next
        driver to hold it replaces the record with its own (#533)."""
        runs = tmp_path / "runs"
        runs.mkdir(mode=0o700)
        lock = runs / scratch.LOCK_FILE
        lock.write_text(NESTED)
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert scratch.runs_holder(runs) is None
            with pytest.raises(scratch.ScratchRunInUse), scratch.hold(runs):
                pass
        finally:
            os.close(fd)
        assert lock.read_text() == NESTED
        with scratch.hold(runs):
            assert f"process {os.getpid()} " in scratch.runs_holder(runs)
        assert lock.read_text() == ""


class TestNothingTouched:
    def test_it_writes_nothing_and_asks_nothing(
        self, tmp_path, engine, request
    ):
        """After a run cut short, the estimate reads the run and the cache
        and writes neither, and asks the engine and the wiki nothing: not
        for the letter's last chunk, a fork, nor for the photograph's
        reading, one-shot. It makes the run's engine clients, one a role,
        for what their keys name, and closes them."""
        asked = request.getfixturevalue("refusals")
        engine.people = 12
        corpus = one_letter(tmp_path)
        corpus.image(PHOTO, "studio-photo-1890s")
        cut_short(tmp_path, corpus, engine)
        runs, cache = runs_dir(tmp_path), tmp_path / "cache"
        before, calls = tree(runs, cache), len(engine.calls)
        closed = engine.closed
        counted = estimate_of(tmp_path, corpus, engine)
        assert tree(runs, cache) == before
        assert per_stage(counted, "known") == {
            "transcription": 1,
            "extraction": 1,
        }
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
        # nor does it make the run's preflight calls (#455)
        assert engine.preflights == []
        assert [line.split(" by ")[0] for line in counted.holders] == [
            f"the runs directory {runs}",
            f"the LLM cache {cache}",
        ]
        assert all(f"process {pid} " in line for line in counted.holders)

    def test_a_runs_lock_nested_too_deep_names_no_holder(
        self, tmp_path, engine
    ):
        """The runs directory's lock file holds a record nested too deep
        to decode: `yizkr scratch estimate` counts all the same and names
        no holder (#533)."""
        runs = runs_dir(tmp_path)
        runs.mkdir(mode=0o700)
        (runs / scratch.LOCK_FILE).write_text(NESTED)
        counted = estimate_of(tmp_path, small(tmp_path), engine)
        assert counted.total().engine_calls == 14
        assert counted.holders == []
        assert (runs / scratch.LOCK_FILE).read_text() == NESTED

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


class _Looked(Exception):
    """Raised by a stand-in for the lookup of a transcription view, once
    it has noted whether the view is to be checked again."""


class TestCheckedRoutes:
    """The estimate checks a reused transcription again on exactly the
    routes the driver does (#616): Estimator._transcription and
    Estimator._stale_stages as Orchestrator._transcription, one list,
    CHECKED_ROUTES. Each looks the view up through a stand-in that notes
    whether it was handed a recheck; nothing is read or run."""

    # every route the driver transcribes by (Orchestrator._transcribe)
    ROUTES = ("vision", PAGED, "text-file", "correction", EXCERPT)

    def test_the_checked_routes_are_the_transcribers_that_check(self):
        """Every route but a curator's correction checks its output."""
        assert set(self.ROUTES) - CHECKED_ROUTES == {"correction"}
        assert set(self.ROUTES) >= CHECKED_ROUTES

    @pytest.mark.parametrize("route", ROUTES)
    def test_the_estimate_rechecks_where_the_driver_does(
        self, monkeypatch, route
    ):
        rechecked: dict[str, bool] = {}

        def looked(name):
            def look(self, doc, view, load, recheck=None):
                rechecked[name] = recheck is not None
                raise _Looked

            return look

        monkeypatch.setattr(Orchestrator, "_route", lambda self, doc: route)
        monkeypatch.setattr(
            estimate, "route_transcription", lambda source, correction: route
        )
        monkeypatch.setattr(Orchestrator, "_reused_view", looked("driver"))
        doc = SimpleNamespace(
            sha1="a" * 40,
            source=Path("a-letter.txt"),
            correction=None,
            transcription=Path("transcription.json"),
            batch=Path("batch.json"),
            critique=Path("critique.json"),
        )
        driver = object.__new__(Orchestrator)
        driver._views = {}
        with pytest.raises(_Looked):
            driver._transcription(doc)

        estimator = object.__new__(Estimator)
        monkeypatch.setattr(Estimator, "_stands", looked("estimate"))
        with pytest.raises(_Looked):
            estimator._transcription(doc, False, None)
        monkeypatch.setattr(Estimator, "_fresh", lambda self, doc: False)
        monkeypatch.setattr(Estimator, "_stands", looked("excerpt"))
        with pytest.raises(_Looked):
            estimator._stale_stages(doc, ["transcription"])

        checked = route in CHECKED_ROUTES
        assert rechecked == {
            "driver": checked,
            "estimate": checked,
            "excerpt": checked,
        }


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

    def test_a_pacing_setting_the_engine_refuses(
        self, tmp_path, engine, monkeypatch
    ):
        """The engine's client cannot start with it, and the run's
        preflight refuses it: so does the estimate, first."""
        monkeypatch.setenv("YIZKR_CLAUDE_RESERVE", "2")
        with pytest.raises(PreflightFailed, match="YIZKR_CLAUDE_RESERVE"):
            estimate_of(tmp_path, small(tmp_path), engine)

    @pytest.mark.parametrize(
        "thresholds, why",
        [
            (b"chars-per-pages: 900\n", "chars-per-pages"),
            (b"chars-per-page: 0\n", "greater than or equal to 1"),
            (b"\xff\xfe not a text\n", "cannot be read"),
            (
                b"checks: " + b"[" * 1000 + b"]" * 1000 + b"\n",
                "cannot be read",
            ),
        ],
        ids=["unknown key", "out of range", "unreadable", "too deep"],
    )
    def test_anomaly_thresholds_the_checks_cannot_load(
        self, tmp_path, engine, monkeypatch, thresholds, why
    ):
        """The run's preflight refuses a file `$YIZKR_ANOMALIES` names that
        the checks cannot use, with its other problems, and so does the
        estimate, first: before it starts any engine client (#327)."""
        overrides = tmp_path / "anomalies.yaml"
        overrides.write_bytes(thresholds)
        monkeypatch.setenv("YIZKR_ANOMALIES", str(overrides))
        monkeypatch.setenv("YIZKR_CLAUDE_RESERVE", "2")
        with pytest.raises(PreflightFailed) as refused:
            estimate_of(tmp_path, small(tmp_path), engine)
        problems = refused.value.problems
        assert len(problems) == 2
        (problem,) = [p for p in problems if str(overrides) in p]
        assert why in problem and "\n" not in problem
        assert engine.closed == 0 and engine.calls == []
        assert not runs_dir(tmp_path).exists()

    def test_anomaly_thresholds_are_refused_for_a_corpus_with_no_document(
        self, tmp_path, engine, monkeypatch
    ):
        """No document is no call, but the thresholds are refused all the
        same, as the run's preflight refuses them before it looks for a
        document (#327)."""
        overrides = tmp_path / "anomalies.yaml"
        overrides.write_text("chars-per-pages: 900\n")
        monkeypatch.setenv("YIZKR_ANOMALIES", str(overrides))
        with pytest.raises(PreflightFailed, match="chars-per-pages"):
            estimate_of(tmp_path, Corpus(tmp_path / "empty"), engine)

    def test_a_runs_directory_the_run_refuses(self, tmp_path, engine):
        """A runs directory open to other users, a file, a path under
        one, a link to nothing, and a folder that cannot be written: the
        run refuses each (scratch.hold), and so does the estimate, making
        and changing nothing."""
        corpus = small(tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        a_file = tmp_path / "a-file"
        a_file.write_text("")
        dangling = tmp_path / "dangling"
        dangling.symlink_to(tmp_path / "nowhere", target_is_directory=True)
        for runs, reason in (
            (shared, "open to other users"),
            (a_file, "not a directory"),
            (a_file / "runs", "not a directory"),
            (dangling, "not a directory"),
        ):
            with pytest.raises(ScratchRefused, match=reason):
                estimate.scratch_estimate(
                    corpus.folder, runs=runs, estimator=Quiet,
                    cache_dir=tmp_path / "cache",
                )  # fmt: skip
            with (
                pytest.raises(ScratchRefused, match=reason),
                scratch.hold(runs),
            ):
                pass
        assert stat.S_IMODE(shared.stat().st_mode) == 0o755
        assert list(shared.iterdir()) == []
        closed = tmp_path / "closed"
        closed.mkdir(mode=0o500)
        try:
            if os.access(closed, os.W_OK):
                pytest.skip("this user writes to any directory")
            for runs in (closed, closed / "runs"):
                with pytest.raises(ScratchRefused, match="cannot be written"):
                    estimate.scratch_estimate(
                        corpus.folder, runs=runs, estimator=Quiet,
                        cache_dir=tmp_path / "cache",
                    )  # fmt: skip
        finally:
            closed.chmod(0o700)

    def test_a_closed_runs_directory_with_its_lock_is_resumed(
        self, tmp_path, engine
    ):
        """A private runs directory closed to writing (mode 500) that holds
        its run and its lock file: the driver opens the lock that is there,
        makes nothing else in it, and resumes the run; the estimate counts
        that invocation, as it makes it."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        runs = runs_dir(tmp_path)
        assert (runs / scratch.LOCK_FILE).is_file()
        runs.chmod(0o500)
        try:
            if os.access(runs, os.W_OK):
                pytest.skip("this user writes to any directory")
            assert scratch.runs_refusal(runs) is None
            counted = estimate_of(tmp_path, corpus, engine)
            assert counted.resumed
            made = run(tmp_path, corpus, engine)
        finally:
            runs.chmod(0o700)
        assert per_stage(counted, "engine_calls") == made

    @pytest.mark.parametrize("lock", ["unwritable", "a folder"])
    def test_a_lock_file_it_cannot_open_is_refused(
        self, tmp_path, engine, lock
    ):
        """A lock file this user cannot open to read and write (one a run
        under another user left, say), or a folder in its place, in a runs
        directory or a cache folder it can write in: the run refuses each
        as it opens the lock, and so does the estimate, before it."""
        corpus = small(tmp_path)
        runs, cache = runs_dir(tmp_path), tmp_path / "cache"
        for folder in (runs, cache):
            folder.mkdir(mode=0o700)
            folder.chmod(0o700)
            if lock == "a folder":
                (folder / ".lock").mkdir()
            else:
                (folder / ".lock").write_text("")
                (folder / ".lock").chmod(0o400)

        def usable(path):
            if lock == "a folder":
                shutil.rmtree(path)
            else:
                path.chmod(0o600)

        try:
            if lock == "unwritable" and os.access(runs / ".lock", os.W_OK):
                pytest.skip("this user writes to any file")
            with pytest.raises(ScratchRefused, match="cannot open its lock"):
                estimate_of(tmp_path, corpus, engine)
            with (
                pytest.raises(ScratchRefused, match="cannot be the runs"),
                scratch.hold(runs),
            ):
                pass
            usable(runs / ".lock")
            with pytest.raises(PreflightFailed, match="cannot open its lock"):
                estimate_of(tmp_path, corpus, engine)
            with pytest.raises(CacheRefused, match="cannot open its lock"):
                llm.open_cache(cache)
        finally:
            for folder in (runs, cache):
                if lock == "unwritable" and (folder / ".lock").exists():
                    (folder / ".lock").chmod(0o600)
        assert list(cache.iterdir()) == [cache / ".lock"]

    def test_a_closed_cache_folder_with_its_lock_is_refused(
        self, tmp_path, engine
    ):
        """A cache folder closed to writing, even with its lock file in it:
        the cache writes its entries there, so the run refuses it at its
        preflight, and so does the estimate."""
        corpus = small(tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir(mode=0o700)
        (cache / ".lock").write_text("")
        cache.chmod(0o500)
        try:
            if os.access(cache, os.W_OK):
                pytest.skip("this user writes to any folder")
            with pytest.raises(PreflightFailed, match="cannot write in it"):
                estimate_of(tmp_path, corpus, engine)
            with pytest.raises(CacheRefused, match="cannot write in it"):
                llm.open_cache(cache)
        finally:
            cache.chmod(0o700)

    def test_a_runs_directory_git_would_track(
        self, tmp_path, engine, monkeypatch
    ):
        if shutil.which("git") is None:
            pytest.skip("no git")
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        corpus = small(tmp_path)
        monkeypatch.chdir(repo)
        with pytest.raises(ScratchRefused, match="git work tree"):
            estimate.scratch_estimate(
                corpus.folder, runs=repo / "runs", estimator=Quiet,
                cache_dir=tmp_path / "cache",
            )  # fmt: skip
        assert sorted(path.name for path in repo.iterdir()) == [".git"]

    def test_a_cache_folder_the_cache_refuses(self, tmp_path, engine):
        """A cache folder open to other users, one this user cannot write
        in, and one under a folder it cannot make it in: the run refuses
        each at its preflight, and so does the estimate, leaving the
        folder as it was (#327)."""
        corpus = small(tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o750)
        shared.chmod(0o750)
        with pytest.raises(PreflightFailed, match="open to other users"):
            estimate_of(tmp_path, corpus, engine, cache_dir=shared)
        assert stat.S_IMODE(shared.stat().st_mode) == 0o750
        assert list(shared.iterdir()) == []
        closed = tmp_path / "closed"
        closed.mkdir(mode=0o700)
        closed.chmod(0o500)
        try:
            if os.access(closed, os.W_OK):
                pytest.skip("this user writes to any folder")
            for cache, why in (
                (closed, "cannot write in it"),
                (closed / "cache", "cannot make it in"),
            ):
                with pytest.raises(PreflightFailed, match=why):
                    estimate_of(tmp_path, corpus, engine, cache_dir=cache)
        finally:
            closed.chmod(0o700)

    def test_a_cache_folder_the_cache_refuses_on_a_resume(
        self, tmp_path, engine
    ):
        """A resume with nothing stale is refused for a cache folder the
        cache refuses, as the run's preflight refuses it, and so is one
        whose documents' states cannot be read (#327)."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o750)
        shared.chmod(0o750)
        with pytest.raises(PreflightFailed, match="open to other users"):
            estimate_of(
                tmp_path, corpus, engine, resume=LATEST, cache_dir=shared
            )
        run_dir = scratch.runs_in(runs_dir(tmp_path))[-1]
        states = sorted(run_dir.rglob("state.json"))
        assert states
        for state in states:
            state.write_text("{")
        with pytest.raises(PreflightFailed, match="open to other users"):
            estimate_of(
                tmp_path, corpus, engine, resume=LATEST, cache_dir=shared
            )
        assert list(shared.iterdir()) == []

    @pytest.mark.parametrize("calls_engine", [False, True])
    def test_past_the_barrier_only_the_cache_folder_is_checked_for_the_engine(
        self, tmp_path, engine, monkeypatch, calls_engine
    ):
        """An invocation past the barrier only reads no cache unless a
        corpus stage in range may call the engine: the folder is refused
        then, as the run's preflight refuses it, and otherwise left alone
        (#327)."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)
        run_dir = scratch.runs_in(runs_dir(tmp_path))[-1]
        other = dict(RunDir(run_dir).manifest()["code"], commit="f" * 40)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o750)
        shared.chmod(0o750)
        options = {
            "resume": LATEST,
            "cache_dir": shared,
            "corpus_stages": [
                scratch.CorpusStage(
                    "clustering", lambda context: None,
                    calls_engine=calls_engine,
                )
            ],
        }  # fmt: skip
        if calls_engine:
            with pytest.raises(PreflightFailed, match="open to other users"):
                estimate_of(tmp_path, corpus, engine, **options)
        else:
            counted = estimate_of(tmp_path, corpus, engine, **options)
            assert counted.past_barrier_only
        assert list(shared.iterdir()) == []

    def test_a_cache_folder_git_would_track(
        self, tmp_path, engine, monkeypatch
    ):
        if shutil.which("git") is None:
            pytest.skip("no git")
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        corpus = small(tmp_path)
        monkeypatch.chdir(repo)
        with pytest.raises(PreflightFailed, match="git work tree"):
            estimate_of(tmp_path, corpus, engine, cache_dir=repo / "cache")
        assert sorted(path.name for path in repo.iterdir()) == [".git"]

    @pytest.mark.parametrize("how", ["emptied", "excluded"])
    def test_a_corpus_with_no_document_is_no_call(
        self, tmp_path, engine, how, monkeypatch
    ):
        """No document to take: the run finishes at once, other settings
        or not, and so the estimate counts no call and refuses nothing,
        as it counts past the barrier with --refresh."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine, until="transcription")
        exclusions = ExclusionList()
        if how == "emptied":
            for path in corpus.folder.iterdir():
                path.unlink()
        else:
            exclusions = ExclusionList(
                sha1s=frozenset(corpus.sha1(name) for name in corpus.texts)
            )
        counted = estimate_of(
            tmp_path, corpus, engine, review_effort="max",
            exclusions=exclusions,
        )  # fmt: skip
        assert counted.resumed and counted.stages == {}
        assert "No document to take: no call." in estimate.describe(counted)
        run_dir = scratch.runs_in(runs_dir(tmp_path))[-1]
        other = dict(RunDir(run_dir).manifest()["code"], commit="f" * 40)
        monkeypatch.setattr(scratch, "code_version", lambda: dict(other))
        counted = estimate_of(
            tmp_path, corpus, engine, refresh=["extract"],
            exclusions=exclusions,
        )  # fmt: skip
        assert counted.stages == {}

    def test_a_pause_as_a_client_starts(self, tmp_path, engine, monkeypatch):
        """A client pauses the pass as it starts (the claude CLI's global
        config sets variables, say): nothing stale starts one, as in the
        run, and a stale stage stops the estimate as it stops the run."""
        corpus = small(tmp_path)
        run(tmp_path, corpus, engine)

        def paused(*args, **kwargs):
            raise llm.CliConfigEnvironment("the global config sets env")

        monkeypatch.setattr(llm, "make_client", paused)
        counted = estimate_of(tmp_path, corpus, engine, resume=LATEST)
        assert counted.total().engine_calls == 0
        assert counted.cli_version is None
        with pytest.raises(llm.CliConfigEnvironment):
            estimate_of(tmp_path, corpus, engine, new=True)

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
            "Engine calls: 14, 4 known and 10 estimated, and up to 12 more."
        ) in text
        assert (
            "- Known: keyed as the run keys them, from the same prompts, "
            "files, models, efforts and engine configuration, and answered "
            "by the cache or by the engine."
        ) in text
        assert (
            "- Up to 12 more, where only the engine's answers decide: an "
            "entity manifest the engine answers may be refused"
        ) in text
        assert "an estimate, not a ceiling." in text
        assert (
            "6 extraction calls for 4 documents, by the mean for their kind "
            "over the runs measured, or by their text's length; 4 critiques, "
            "one each."
        ) in text
        assert "- Not counted:" in text
        assert (
            "the preflight's one trivial call per (model, effort) pair of the "
            "roles within the invocation's --until range, which every "
            "invocation whose range holds a stage that may call the engine "
            "makes, a resume that reuses every view included"
        ) in text

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
            callless_stages=["clustering", "flags"],
            holders=["the runs directory x by process 7"],
            preflight=["the claude CLI has no login"],
        )
        text = "\n".join(estimate.describe(counted))
        assert f"run r in {tmp_path / 'runs'}, invoked again" in text
        assert "as the cache answers it: 1 transcription." in text
        assert "- No stage for 2 duplicates and 1 unreadable." in text
        assert "- Not estimated: the corpus stages clusters." in text
        assert "- No call: the corpus stages clustering, flags make none." in (
            text
        )
        assert "- Held now: the runs directory x by process 7." in text
        assert "would refuse it now: the claude CLI has no login" in text

    def test_what_the_run_would_want_is_noted(
        self, tmp_path, engine, monkeypatch
    ):
        """No login, and less free space than asked for: the run's
        preflight would refuse it, and the estimate counts it all the
        same, and says so."""
        monkeypatch.setattr(scratch, "claude_logged_in", lambda: False)
        counted = estimate_of(
            tmp_path, small(tmp_path), engine, min_free=1 << 62
        )
        assert len(counted.preflight) == 3
        assert "no login" in counted.preflight[0]
        assert all("GiB free" in note for note in counted.preflight[1:])
        assert counted.total().engine_calls == 14
        text = "\n".join(estimate.describe(counted))
        assert "would refuse it now: the claude CLI has no login" in text
        assert text.endswith("a run asks for.")

    def test_the_past_barrier_and_no_document_lines(self, tmp_path):
        past = estimate.Estimate(
            run=tmp_path / "runs" / "r",
            resumed=True,
            cache=tmp_path / "cache",
            past_barrier_only=True,
        )
        lines = estimate.describe(past)
        assert lines[2].startswith("The code differs from the run's")
        assert "No document to take" not in "\n".join(lines)
        none = estimate.Estimate(
            run=tmp_path / "runs" / "r", resumed=False, cache=tmp_path
        )
        assert estimate.describe(none)[2] == "No document to take: no call."


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
            CacheRefused("the LLM cache is open to other users"),
        ],
        ids=["refused", "settings", "paused", "cache"],
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
        said = capsys.readouterr().err
        assert str(refusal).splitlines()[0] in said
        if isinstance(refusal, RunSettingsConflict):
            assert said.rstrip().endswith("yizkr scratch estimate --new")

    def test_ctrl_c_is_said_and_exits_130(self, tmp_path, monkeypatch, capsys):
        env = tmp_path / "test.env"
        env.write_text(f"YIZKR_EXCLUDE_FILE={tmp_path / 'never.txt'}\n")
        (tmp_path / "never.txt").write_text("")
        folder = tmp_path / "corpus"
        folder.mkdir()

        def interrupted(corpus, **options):
            raise KeyboardInterrupt

        monkeypatch.setattr(estimate, "scratch_estimate", interrupted)
        args = cli.build_parser().parse_args(
            ["--env-file", str(env), "scratch", "estimate", "--corpus",
             str(folder)]
        )  # fmt: skip
        assert args.func(args) == scratch.EXIT_INTERRUPTED == 130
        assert capsys.readouterr().err.strip() == "interrupted"

    def test_its_help_says_what_it_counts(self, capsys):
        with pytest.raises(SystemExit):
            parsed("estimate", "--help")
        said = " ".join(capsys.readouterr().out.split())
        assert "counts the engine calls" in said
        assert "Takes the options of `scratch run`" in said
