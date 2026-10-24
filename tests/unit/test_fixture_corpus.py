"""The fixture corpus (#76), and whole scratch runs over it, offline.

The corpus is tests/fixtures/corpus: six documents of the fictional
universe (docs/EXAMPLES.md, "The fixture corpus"), the builder that makes
them, the recorded answers the recorded engine gives for them, and the
traps they set. TestTheCorpus holds the corpus to its builder and its
answers to the universe.

The rest take it through the scratch driver as far as the driver goes: the
per-document stages, the barrier and the corpus stages, the registered ones
(scratch.CORPUS_STAGES) and a probe of the test's own. The wiki refuses
every read and write (test_no_wiki_before_write.py), the recorded engine
answers behind the real LLM cache, and face detection may not run. The
tests assert what must come of each stage and each trap, through the run's
events, its states and its views, and read claims in the universe's terms
(said_in); no gold set is kept.

Past the driver, two readers of the run directory are held to the corpus
too (#380). `yizkr scratch report` (TestTheRunReport) must count the
corpus's stages, how each came about, the clean corpus's anomalies (none)
and the clusters, and report.md must say none of it by a universe name, a
file name or a document's SHA-1. The clustering stage's identity map
(TestTheTrapsInTheIdentityMap) must meet each trap TRAPS sets for it,
read in the universe's terms; each merge clustering withholds (a
conflicting cluster's confident component, and a `same` verdict a
contradiction overrules) is recorded as expected, with its reason, by a
strict xfail that asserts the merge, so it turns when clustering makes
it. The confident components of ambiguous clusters
merge (#568), which turned the two other such xfails. Clustering's
adjudication asks the recorded engine of the corpus's ambiguous pairs,
and it answers each by the verdict the corpus records for it
(tests/fixtures/corpus/verdicts.py; #587), so the traps meet the stage
as a run uses it: TestTheVerdicts holds each verdict to the universe,
and the adjudicated map to no false merge. The corpus's recorded
answers carry mention records (#407), and the export the records its
adapter fills from its tags (#408), so clustering keys and links on the
records (ADR-0035 §7; #409), and no person or place of the corpus is
recordless.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures import corpus as fc
from tests.fixtures import example_universe as u
from tests.fixtures.corpus import answers, build, verdicts
from tests.fixtures.corpus.engine import Calls, RecordedEngine
from tests.unit.test_clustering import Doc, assert_no_false_merge
from tests.unit.test_no_wiki_before_write import refusals  # noqa: F401
from tests.unit.test_scratch import (
    MODEL,
    Driver,
    closing_report,
    events,
    stages_of,
    started,
    statuses,
)
from yizkr.claims import ClaimBatch, EntityRecord, ItemValue, TimeValue
from yizkr.cli import build_parser
from yizkr.model.schema import load_manifest
from yizkr.pipeline import chunking, clustering, llm, scratch
from yizkr.pipeline.critique import entries, sheet_notes
from yizkr.pipeline.extract import run_metadata, salvage_valid_claims
from yizkr.pipeline.gedcom import parse_gedcom
from yizkr.pipeline.orchestrate import (
    BATCH_VOLATILE,
    DOCUMENT_STAGES,
    EXCERPT,
    PDF,
    document_kind,
)
from yizkr.pipeline.presentation import slide_transcript, unpack_pptx
from yizkr.pipeline.report import (
    COMPUTED,
    JSON_FILE,
    MARKDOWN_FILE,
    REPORT_DIR,
    REUSED,
)
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.rundir import (
    ALERTS,
    DONE,
    FAILED,
    NEEDS_ATTENTION,
    PENDING,
    RunDir,
    run_state,
    summarise,
)
from yizkr.pipeline.scratch import (
    BARRIER,
    CORPUS_DIR,
    EXIT_OK,
    CorpusStage,
)
from yizkr.pipeline.stages import content_hash, read_jsonl, sha1_file

SOURCES = {source.key: source for source in u.SOURCES}
# the documents the engine is asked about, and the images it reads
EXTRACTED = (
    fc.NARRATIVE,
    fc.DECK,
    fc.CERTIFICATE,
    fc.LETTER,
    fc.DEATH_CERTIFICATE,
)
IMAGES = (fc.CERTIFICATE, fc.LETTER, fc.DEATH_CERTIFICATE)
# the corpus stage of the test's own, run after the registered ones
PROBE = "probe"
# every claim that marks a person dead (ADR-0020)
DEATH_PROPERTIES = (
    "date_of_death",
    "place_of_death",
    "cause_of_death",
    "burial_place",
)


def sha1(name: str) -> str:
    return sha1_file(fc.FOLDER / name)


# -- the recorded engine, the refusing wiki and a copy of the corpus ----------


@pytest.fixture
def calls(monkeypatch, tmp_path) -> Calls:
    """The recorded engine behind the client factory, a full model id, and
    a claude CLI that is installed and logged in as far as the preflight
    can tell, as test_scratch.py's `fakes` has them: a credentials file in
    a config directory of the test's own. Returns the engine's log."""
    calls = Calls()

    def make_client(engine, *, effort=None, model=None, **kwargs):
        return RecordedEngine(calls, effort=effort, model=model)

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
    return calls


class Probe:
    """A corpus stage's work: what the barrier hands the corpus stages, the
    documents whose per-document stages all completed, by file name."""

    def __init__(self) -> None:
        self.handed: list[dict[str, object]] = []

    def __call__(self, context: scratch.CorpusContext) -> None:
        self.handed.append({doc.source.name: doc for doc in context.documents})


class Scratch:
    """Scratch runs over a copy of the corpus, each in a run directory of
    its own under one runs directory, all through one LLM cache; the
    registered corpus stages run, then the probe. `said` keeps what each
    run's driver logged, its latest invocation's last."""

    def __init__(self, tmp_path: Path, calls: Calls, refused) -> None:
        self.folder = fc.copy_to(tmp_path / "corpus")
        self.runs = tmp_path / "runs"
        self.calls = calls
        self.refused = refused
        self.probe = Probe()
        self.said: dict[str, list[str]] = {}
        # the folder each run took its corpus from
        self.folders: dict[str, Path] = {}

    def stages(self) -> list[CorpusStage]:
        return [*scratch.CORPUS_STAGES, CorpusStage(PROBE, self.probe)]

    def driver(self, name: str, **options) -> Driver:
        return Driver(
            self.folders.get(name, self.folder),
            self.runs / name,
            corpus_stages=self.stages(),
            **options,
        )

    def run(self, name: str, folder: Path | None = None) -> Path:
        """Run the run `name`, a new one or, invoked again, the same, to the
        end, over the corpus in `folder` (the copy made first, by
        default); returns its run directory."""
        if folder is not None:
            self.folders[name] = folder
        said = self.said.setdefault(name, [])

        def log(*args, **kwargs) -> None:
            said.append(" ".join(map(str, args)))

        driver = self.driver(name, log=log)
        assert driver.run() == EXIT_OK
        return driver.state_dir

    def views(self, run: Path) -> dict:
        """Each document of the run, by file name, with its views."""
        return {
            doc.source.name: doc for doc in self.driver(run.name).documents()
        }

    def batch(self, run: Path, name: str) -> ClaimBatch:
        return ClaimBatch.model_validate_json(
            self.views(run)[name].batch.read_text()
        )


@pytest.fixture
def runs(tmp_path, calls, refusals, monkeypatch) -> Scratch:  # noqa: F811
    from yizkr.pipeline import faces

    def no_faces(*args, **kwargs):
        raise AssertionError("a scratch run ran face detection")

    monkeypatch.setattr(faces, "make_detector", no_faces)
    return Scratch(tmp_path, calls, refusals)


# -- claims in the universe's terms -------------------------------------------


def export_truth(batch: ClaimBatch) -> dict[str, str]:
    """The universe key of each entity the export's batch holds: a person
    by the xref its id is made from, a place by the head of its name and
    an occupation by its label. Any other entity, such as a death the
    export states without a date, stands for itself by its kind and label,
    which is no universe key."""
    people = {
        person.gedcom_xref.strip("@").lower(): person.key
        for person in u.PEOPLE
        if person.gedcom_xref
    }
    named = {place.label: place.key for place in u.PLACES}
    named |= {entity.label: entity.key for entity in u.ENTITIES}
    known = {}
    for record in batch.entities:
        if record.id in people:
            known[record.id] = people[record.id]
        elif record.kind in ("place", "occupation_class"):
            known[record.id] = named[record.label.split(", ")[0]]
        else:
            known[record.id] = f"{record.kind}: {record.label}"
    return known


def truth(name: str, batch: ClaimBatch) -> dict[str, str]:
    """The universe key of each entity of a document's batch."""
    if name == fc.EXPORT:
        return export_truth(batch)
    return {e.id: e.truth for e in answers.RECORDED[name].entities}


def in_universe(value, known: dict[str, str]):
    """A claim value in the universe's terms: an entity's key, a date, a
    name's text or an amount."""
    if isinstance(value, ItemValue):
        ref = value.value
        return known.get(getattr(ref, "local_id", None)) or getattr(
            ref, "key", None
        )
    if isinstance(value, TimeValue):
        return u.Date(value.year, value.month, value.day)
    if value.type == "monolingual":
        return value.value.text
    if value.type == "quantity":
        return value.amount
    return value.value


# The properties the manifest retires: extraction refuses their claims
# (ADR-0036), and the recorded answers, under prompts that no longer offer
# them, make none (#407).
RETIRED = {
    key for key, spec in load_manifest().properties.items() if spec.retired
}


def said_in(name: str, batch: ClaimBatch) -> set[tuple]:
    """Every claim of a document's batch as (subject, property, value), in
    the universe's terms."""
    known = truth(name, batch)
    return {
        (
            known[claim.subject.local_id],
            claim.property,
            in_universe(claim.value, known),
        )
        for claim in batch.claims
    }


def values(said: set[tuple], subject: str, prop: str) -> set:
    return {
        value for who, what, value in said if (who, what) == (subject, prop)
    }


def death_events(name: str, batch: ClaimBatch) -> set[str]:
    """Who takes part in a death event of the batch."""
    known = truth(name, batch)
    deaths = {e.id for e in batch.entities if e.kind == "death"}
    return {
        in_universe(claim.value, known)
        for claim in batch.claims
        if claim.subject.local_id in deaths and claim.property == "participant"
    }


# -- the corpus ---------------------------------------------------------------


class TestTheCorpus:
    def test_the_documents_are_what_the_builder_makes(self, tmp_path):
        made = build.documents()
        assert sorted(path.name for path in fc.FOLDER.iterdir()) == sorted(
            made
        )
        for name, content in made.items():
            assert (fc.FOLDER / name).read_bytes() == content, name
        # the images are small stand-ins, never a scan
        for name in IMAGES:
            assert made[name].startswith(b"\x89PNG\r\n\x1a\n")
            assert len(made[name]) < 4096
        # and the builder writes them where it is told
        written = build.build(tmp_path / "again")
        assert sorted(path.name for path in written) == sorted(made)

    def test_each_document_is_a_kind_the_driver_plans(self):
        assert set(fc.DOCUMENTS) == set(build.documents())
        # every kind but an excerpt, which is an image a container places,
        # found by its transcription and never a file of the folder (#397),
        # and a PDF, which tests/unit/test_pdf_pages.py builds for itself
        # (#502)
        assert {doc.kind for doc in fc.DOCUMENTS.values()} == set(
            DOCUMENT_STAGES
        ) - {EXCERPT, PDF}
        for name, doc in fc.DOCUMENTS.items():
            assert document_kind(fc.FOLDER / name) == doc.kind
            assert doc.source is None or doc.source in SOURCES

    def test_the_narrative_has_two_pages(self):
        text = (fc.FOLDER / fc.NARRATIVE).read_text()
        assert text == SOURCES["family-history-2008"].text + "\n"
        assert re.findall(r"^\[page (\d+)\]", text, re.MULTILINE) == [
            "1",
            "2",
        ]

    def test_the_deck_reads_as_its_source(self):
        content = unpack_pptx(fc.FOLDER / fc.DECK)
        assert slide_transcript(content) == SOURCES["library-talk-2018"].text
        (photograph,) = content.media
        assert (photograph.slides, content.skipped_media) == ([2], {})

    def test_the_export_is_a_slice_of_the_universes_with_its_traps(self):
        """Each record of the export is the fixture's record of the same
        xref, with lines left out (the daughter Ursula's dates, the widow's
        note, and what the Lobig line does not need), and one line added:
        Nicholas's DEAT that states no death."""

        def records(path: Path) -> dict[str, list[str]]:
            found: dict[str, list[str]] = {}
            key = None
            for line in path.read_text().splitlines():
                if line.startswith("0 "):
                    key = line
                    found[key] = []
                found[key].append(line)
            return found

        fixture = records(u.GEDCOM_FIXTURE)
        export = records(fc.FOLDER / fc.EXPORT)
        added = []
        for key, lines in export.items():
            assert key in fixture, key
            # the fixture's lines not yet matched, in order
            left = fixture[key]
            for line in lines:
                if line in left:
                    left = left[left.index(line) + 1 :]
                else:
                    added.append((key, line))
        assert added == [("0 @I15@ INDI", "1 DEAT")]
        # the added DEAT is bare: nothing beneath it
        (nicholas,) = [
            r
            for r in parse_gedcom((fc.FOLDER / fc.EXPORT).read_text())
            if r.xref == "@I15@"
        ]
        (death,) = nicholas.all("DEAT")
        assert (death.value, death.children) == ("", [])

    def test_the_recorded_answers_are_the_sources_assertions(self):
        """Every claim the recorded engine answers is an assertion the
        universe records for the document's source, in the form the
        extraction prompts ask for (#407), but for its recorded mistakes,
        which a sharper prompt mends; none of the source's assertions
        about an entity is left out; and each is a valid claim. The
        universe states a couple by a spouse assertion, a property now
        retired: the recorded model answers each as a marriage whose
        couple take part in it as spouses, started by a wedding where the
        source dates the start (ADR-0036). Every participant has a role:
        spouse in a marriage, and the principal in any other event."""
        run = run_metadata("recorded").model_dump(mode="json")
        for name, recorded in answers.RECORDED.items():
            ids = recorded.ids()
            assert len(ids) == len(recorded.entities), name
            about = {
                a.subject for a in u.ASSERTIONS if a.source == recorded.source
            }
            assert about - set(ids) <= {recorded.source}, name
            for entity in recorded.entities:
                assert answers.mention_lines(recorded, entity), entity
            entities = answers.manifest(recorded)["entities"]
            kinds = {e["id"]: e["kind"] for e in entities}
            answered = {}
            for sharper in (False, True):
                claimed = answers.claims(recorded, sha1(name), sharper=sharper)
                batch, rejects = salvage_valid_claims(
                    claimed, run, entities=entities
                )
                assert rejects == [], name
                assert not {c.property for c in batch.claims} & RETIRED
                for claim in batch.claims:
                    roles = [
                        q.value.value.key
                        for q in claim.qualifiers
                        if q.property == "object_has_role"
                    ]
                    if claim.property != "participant":
                        assert roles == [], claim
                    elif kinds[claim.subject.local_id] == "marriage":
                        assert roles == ["spouse"], claim
                    else:
                        assert roles == ["principal"], claim
                answered[sharper] = said_in(name, batch)
            stated = set()
            for a in answers.said(recorded):
                if a.property not in RETIRED:
                    stated.add((a.subject, a.property, a.value))
                    continue
                assert a.property == "spouse", a
                couple = {a.subject, a.value}
                marriage = answers.union_of(recorded, couple, "marriage")
                stated |= {
                    (marriage.truth, "participant", a.subject),
                    (marriage.truth, "participant", a.value),
                }
                for prop, when in a.qualifiers:
                    assert prop == "start_time", a
                    wedding = answers.union_of(recorded, couple, "wedding")
                    stated |= {
                        (marriage.truth, "started_by", wedding.truth),
                        (wedding.truth, "point_in_time", when),
                    }
            mistakes = answers.mistakes_in(recorded)
            says = {(m.subject, m.property, m.says) for m in mistakes}
            read_as = {(m.subject, m.property, m.read_as) for m in mistakes}
            assert answered[True] == stated, name
            assert answered[False] == stated - says | read_as, name
        # the one mistake: a birth a century early, a century the source
        # does not give, and the rule that mends it is new to the prompt
        (mistake,) = answers.MISTAKES
        assert mistake.read_as.year == mistake.says.year - 100
        assert mistake.rule not in chunking.CHUNK_PROMPT

    def test_the_traps_are_those_of_the_issue(self):
        titles = [trap.title for trap in fc.TRAPS]
        assert titles == [
            "two homonyms with identical source descriptions",
            "a cross-source contradiction",
            "mirrored kinship",
            "a death-date trap",
            "a hallucination-style early birth and a later conflicting birth",
            "a cross-source merge carrying death evidence",
            # and #425's, which the end-to-end corpus had lost
            "a hard (kin-edge) conflict",
        ]
        people = {person.key for person in u.PEOPLE}
        names = {trap.name for trap in fc.TRAPS}
        for trap in fc.TRAPS:
            assert set(trap.documents) <= set(fc.DOCUMENTS), trap.name
            assert set(trap.people) <= people, trap.name
        for trap in fc.PRIVACY_TRAPS:
            assert trap.trap in names
            assert {e.person for e in trap.evidence} == {trap.person}
            assert {e.document for e in trap.evidence} <= set(fc.DOCUMENTS)


# -- a whole scratch run ------------------------------------------------------


def corpus_events(run: Path, invocation: int | None = None) -> list[tuple]:
    """(event, stage) of each stage event that names no document."""
    return [
        (e["event"], e["stage"])
        for e in events(
            run, "started", "done", "failed", "held", invocation=invocation
        )
        if not e.get("subject")
    ]


def registered() -> list[str]:
    return [stage.name for stage in scratch.CORPUS_STAGES]


def chunks(name: str) -> int:
    """How many chunks a document's recorded entity manifest packs into."""
    recorded = answers.RECORDED[name]
    records = [
        EntityRecord.model_validate(entity)
        for entity in answers.manifest(recorded)["entities"]
    ]
    return len(chunking.pack_entities(records, recorded.text.splitlines()))


def every_stage() -> set[tuple[str, str]]:
    """(document SHA-1, stage) of every per-document stage of the corpus."""
    return {
        (sha1(name), stage)
        for name, doc in fc.DOCUMENTS.items()
        for stage in DOCUMENT_STAGES[doc.kind]
    }


class TestTheRun:
    def test_every_stage_is_done_and_the_corpus_stages_follow_the_barrier(
        self, runs
    ):
        run = runs.run("first")
        manifest = RunDir(run).manifest()
        assert {
            d["file"]: (d["kind"], d["stages"]) for d in manifest["documents"]
        } == {
            name: (doc.kind, list(DOCUMENT_STAGES[doc.kind]))
            for name, doc in fc.DOCUMENTS.items()
        }
        # one of each kind first, the first of each by name
        assert manifest["scratch"]["canaries"] == [
            sha1(name)
            for name in (
                fc.DEATH_CERTIFICATE,
                fc.NARRATIVE,
                fc.DECK,
                fc.EXPORT,
            )
        ]
        assert statuses(run) == {DONE}
        assert set(started(run)) == every_stage()
        (barrier,) = events(run, BARRIER)
        assert (barrier["documents"], barrier["failed"]) == (6, 0)
        # past the barrier, each corpus stage in turn, none failing
        names = [*registered(), PROBE]
        assert manifest["scratch"]["corpus_stages"] == names
        assert corpus_events(run) == [
            (event, name) for name in names for event in ("started", "done")
        ]
        order = events(run)
        barrier_at = order.index(barrier)
        assert all(
            at > barrier_at
            for at, e in enumerate(order)
            if e["event"] in ("started", "done") and not e.get("subject")
        )
        (handed,) = runs.probe.handed
        assert set(handed) == set(fc.DOCUMENTS)
        assert scratch.is_complete(run)
        assert manifest["invocations"][-1]["outcome"] == "finished"
        # the driver's closing report says so, stage by stage
        said = runs.said["first"]
        assert "scratch run first: finished" in said
        done: dict[str, dict[str, int]] = {}
        for doc in fc.DOCUMENTS.values():
            for stage in DOCUMENT_STAGES[doc.kind]:
                counts = done.setdefault(stage, {DONE: 0})
                counts[DONE] += 1
        assert closing_report(said) == done
        assert summarise(run_state(RunDir(run))) == done
        # and the clean corpus raises no alert
        assert read_jsonl(run / ALERTS) == []

    def test_the_wiki_is_never_asked_and_every_claim_cites_its_document(
        self, runs
    ):
        run = runs.run("first")
        assert runs.refused.asked == []
        for name, doc in runs.views(run).items():
            # no artifact was registered: every claim cites its document
            # by the SHA-1 of its bytes, and none was rejected
            assert doc.state.artifact_qid is None
            batch = ClaimBatch.model_validate_json(doc.batch.read_text())
            assert batch.claims, name
            assert {c.source.artifact.sha1 for c in batch.claims} == {
                sha1(name)
            }
            assert all(not claim.curated for claim in batch.claims)
            if name != fc.EXPORT:
                coverage = json.loads(
                    (doc.extraction / "coverage.json").read_text()
                )
                # no claim is refused, and every person, place,
                # organisation and family has its mention record
                assert coverage["rejected_claims"] == 0, name
                assert coverage["fallback"] == {}, name
                assert coverage["records"]["recordless"] == 0, name
                assert coverage["records"]["set_aside"] == {}, name
        # the deck's photograph is recorded, not registered
        deck = runs.views(run)[fc.DECK]
        assert (deck.dir / "derived-media.json").is_file()

    def test_the_engine_answers_only_what_each_document_needs(self, runs):
        run = runs.run("first")
        asked = runs.calls
        assert sorted(asked.of("vision")) == sorted(IMAGES)
        for what in ("manifest", "chunk", "critique"):
            assert set(asked.of(what)) == set(EXTRACTED), what
        # the text file and the deck are transcribed without the engine,
        # and the export is adapted
        engines = {
            name: json.loads(doc.transcription.read_text())["engine"]
            for name, doc in runs.views(run).items()
            if name != fc.EXPORT
        }
        assert engines[fc.NARRATIVE].startswith("text-file:")
        assert engines[fc.DECK] == "pptx-adapter"
        assert {engines[name].split(":")[0] for name in IMAGES} == {
            "llm-vision"
        }
        # each document's extraction opens a conversation on its entity
        # manifest and forks it for each chunk its entities pack into: two
        # for the family history, whose people are on many lines
        extraction = [e for e in events(run, "call") if e["role"] == "extract"]
        for name in EXTRACTED:
            made = [
                e["call"] for e in extraction if e["subject"] == sha1(name)
            ]
            assert made == ["open", *["fork"] * chunks(name)], name
        assert chunks(fc.NARRATIVE) > 1


# -- the traps ----------------------------------------------------------------


@dataclass
class Batches:
    """Each document's batch in a run, and its claims in the universe's
    terms."""

    batches: dict[str, ClaimBatch]

    def said(self, name: str) -> set[tuple]:
        return said_in(name, self.batches[name])

    def entities(self, name: str, label: str) -> list:
        return [e for e in self.batches[name].entities if e.label == label]


@pytest.fixture
def first(runs) -> Path:
    """One whole scratch run over the corpus, the run the traps and the
    run report are read from. A test that asks for more than one of them
    invokes the driver once, so the run keeps its one invocation."""
    return runs.run("first")


@pytest.fixture
def batches(runs, first) -> Batches:
    return Batches({name: runs.batch(first, name) for name in fc.DOCUMENTS})


class TestTheTraps:
    def test_homonyms_described_alike_stay_two_people(self, batches):
        """The export describes its two Ursula Löbigs alike, by label, kind
        and summary; only their links tell them apart, and each keeps its
        own. The family history names both too."""
        ursulas = batches.entities(fc.EXPORT, "Ursula Löbig")
        assert len(ursulas) == 2
        described = {
            (e.label, e.kind, e.summary, tuple(e.aliases)) for e in ursulas
        }
        assert len(described) == 1
        said = batches.said(fc.EXPORT)
        known = export_truth(batches.batches[fc.EXPORT])
        assert {known[e.id] for e in ursulas} == {
            "ursula-loebig",
            "ursula-loebig-angerring",
        }
        assert values(said, "ursula-loebig", "father") == {"sebastian-loebig"}
        assert values(said, "ursula-loebig", "residence") == set()
        assert values(said, "ursula-loebig-angerring", "residence") == {
            "angerring-57"
        }
        assert values(said, "ursula-loebig-angerring", "father") == set()
        # one document, one label, two people
        assert len(batches.entities(fc.NARRATIVE, "Ursula Löbig")) == 2
        told = batches.said(fc.NARRATIVE)
        assert values(told, "ursula-loebig", "date_of_death") == {u.Date(1941)}
        assert values(told, "ursula-loebig-angerring", "residence") == {
            "angerring-57"
        }
        assert (
            values(told, "ursula-loebig-angerring", "date_of_death") == set()
        )

    def test_a_contradiction_across_sources_stands_in_both(self, batches):
        born = {
            name: values(batches.said(name), "gustav-loebig", "date_of_birth")
            for name in (fc.EXPORT, fc.CERTIFICATE)
        }
        assert born == {
            fc.EXPORT: {u.Date(1919, 1, 7)},
            fc.CERTIFICATE: {u.Date(1919, 7, 1)},
        }

    def test_kinship_is_stated_from_both_ends(self, batches):
        children = {"ursula-loebig", "klara-loebig", "gustav-loebig"}
        parents = {"sebastian-loebig": "father", "hildegard-loebig": "mother"}
        # from the children's end: the export, and the postcard's writer
        export = batches.said(fc.EXPORT)
        for child in children:
            for parent, prop in parents.items():
                assert values(export, child, prop) == {parent}, (child, prop)
        assert {
            (who, value) for who, what, value in export if what == "child"
        } == set()
        assert values(batches.said(fc.LETTER), "klara-loebig", "father") == {
            "sebastian-loebig"
        }
        # and from the parents': the family history and the talk
        for name in (fc.NARRATIVE, fc.DECK):
            said = batches.said(name)
            for parent in parents:
                assert values(said, parent, "child") == children, name
            for child in children:
                for prop in parents.values():
                    assert values(said, child, prop) == set(), (name, child)

    def test_a_mother_named_by_her_daughter_s_name(self, batches):
        """Florence's death certificate gives her a mother under her own
        birth name, a name the export gives Florence herself; the export
        gives her no mother."""
        certificate = batches.said(fc.DEATH_CERTIFICATE)
        assert values(certificate, "florence-lobig", "mother") == {
            "sipkje-faxridge"
        }
        assert values(certificate, "florence-lobig", "birth_name") == {
            "Florence Faxridge"
        }
        # the date refines the tree's 1963 (u.REFINEMENTS)
        assert values(certificate, "florence-lobig", "date_of_death") == {
            u.Date(1963, 7, 3)
        }
        (mother,) = batches.entities(fc.DEATH_CERTIFICATE, "Florence Faxridge")
        (florence,) = batches.entities(fc.DEATH_CERTIFICATE, "Florence Lobig")
        surnames = {
            mother.id: {"Faxridge"},
            florence.id: {"Lobig", "Faxridge"},
        }
        for record in (mother, florence):
            assert record.structure.status == "name"
            assert [n.written for n in record.structure.given] == ["Florence"]
            assert {s.written for s in record.structure.surnames} == surnames[
                record.id
            ]
        # each is mentioned on its own line of the certificate: the mother
        # on the line of her field
        recorded = answers.RECORDED[fc.DEATH_CERTIFICATE]
        lines = recorded.text.splitlines()
        by_truth = {e.truth: e for e in recorded.entities}
        (line,) = answers.mention_lines(recorded, by_truth["sipkje-faxridge"])
        assert lines[line - 1].startswith("Mother: ")
        (line,) = answers.mention_lines(recorded, by_truth["florence-lobig"])
        assert lines[line - 1].startswith("Name: ")
        export = batches.said(fc.EXPORT)
        assert values(export, "florence-lobig", "mother") == set()
        assert values(export, "florence-lobig", "birth_name") == {
            "Florence Faxridge"
        }

    def test_a_death_the_export_does_not_state_gives_no_death(self, runs):
        run = runs.run("first")
        export = runs.views(run)[fc.EXPORT]
        batch = runs.batch(run, fc.EXPORT)
        said = said_in(fc.EXPORT, batch)
        for prop in DEATH_PROPERTIES:
            assert values(said, "nicholas-lobig", prop) == set(), prop
        assert "nicholas-lobig" not in death_events(fc.EXPORT, batch)
        assert values(said, "nicholas-lobig", "date_of_birth") == {
            u.Date(1950)
        }
        # the adapter counts the DEAT it did not map
        assert export.state.detail["gedcom_unsupported"].get("DEAT", 0) >= 1
        # and what it did map worth reporting: the export's two marriages,
        # and the language it does not state (ADR-0035 §6, ADR-0036 §5)
        outcomes = export.state.detail["gedcom_outcomes"]
        assert outcomes["union: marriage"] == 2
        assert outcomes["HEAD.LANG absent: und"] == 1

    def test_an_early_birth_and_the_later_ones_stand_side_by_side(self, runs):
        run = runs.run("first")
        born = {
            name: values(
                said_in(name, runs.batch(run, name)),
                "nicholas-lobig",
                "date_of_birth",
            )
            for name in (fc.NARRATIVE, fc.EXPORT, fc.DECK)
        }
        assert born == {
            fc.NARRATIVE: {u.Date(1850)},
            fc.EXPORT: {u.Date(1950)},
            fc.DECK: {u.Date(1950)},
        }
        # the critic flags the early one, on its claim, as a record with a
        # listed category (#386)
        doc = runs.views(run)[fc.NARRATIVE]
        findings = json.loads(doc.critique.read_text())
        nicholas = answers.RECORDED[fc.NARRATIVE].ids()["nicholas-lobig"]
        ((finding),) = findings
        assert (finding["entity"], finding["category"]) == (
            nicholas,
            "implausible_value",
        )
        assert finding["claim"]["property"] == "date_of_birth"
        assert finding["claim"]["value"].startswith("1850")
        # and a review sheet of the batch shows it once, on that claim's
        # row (#113)
        batch = runs.batch(run, fc.NARRATIVE)
        sheet = run.parent / "narrative-review.csv"
        export_review_csv(
            batch,
            sheet,
            critique_notes=sheet_notes(entries(findings), batch, batch),
        )
        with open(sheet, newline="") as handle:
            rows = list(csv.DictReader(handle))
        noted = [row for row in rows if row["critique_notes"]]
        (row,) = noted
        assert row["critique_notes"].count("critique [") == 1
        assert row["property"] == "date_of_birth"
        assert row["value"].startswith("1850")
        for name in (fc.DECK, fc.CERTIFICATE, fc.LETTER):
            assert json.loads(runs.views(run)[name].critique.read_text()) == []

    def test_death_evidence_reaches_the_certificate_only_by_a_merge(
        self, batches
    ):
        certificate = batches.said(fc.CERTIFICATE)
        assert values(certificate, "gustav-loebig", "date_of_birth") == {
            u.Date(1919, 7, 1)
        }
        for prop in DEATH_PROPERTIES:
            assert values(certificate, "gustav-loebig", prop) == set(), prop
        assert "gustav-loebig" not in death_events(
            fc.CERTIFICATE, batches.batches[fc.CERTIFICATE]
        )
        for name in (fc.NARRATIVE, fc.EXPORT):
            said = batches.said(name)
            assert values(said, "gustav-loebig", "date_of_death") == {
                u.Date(1996)
            }, name
            assert values(said, "gustav-loebig", "burial_place") == {
                "stupfield"
            }, name

    def test_the_privacy_traps_evidence_is_in_the_batches(self, batches):
        """What PRIVACY_TRAPS says each document gives, for #120's tests,
        is what the run's batches hold: what its source says, but for a
        mistake of the recorded model."""
        for trap in fc.PRIVACY_TRAPS:
            for evidence in trap.evidence:
                claim = (evidence.person, evidence.property, evidence.value)
                assert claim in batches.said(evidence.document), evidence
                source = fc.DOCUMENTS[evidence.document].source
                if source is not None:
                    stated = {
                        (a.subject, a.property, a.value)
                        for a in u.ASSERTIONS
                        if a.source == source
                    }
                    assert (claim in stated) != evidence.mistaken, evidence


# -- reuse --------------------------------------------------------------------


def calls_by_stage(run: Path, invocation: int | None = None) -> set[tuple]:
    """(document SHA-1, stage) of every call that reached the engine; a
    corpus stage's names no document (None)."""
    return {
        (e.get("subject"), e.get("stage"))
        for e in events(run, "call", invocation=invocation)
    }


def view_hashes(runs: Scratch, run: Path) -> dict[str, tuple]:
    """Each document's views, by their content: the transcription's text,
    the batch without its run stamps, and the critique."""
    found = {}
    for name, doc in runs.views(run).items():
        batch = json.loads(doc.batch.read_text())
        transcription = (
            json.loads(doc.transcription.read_text())["text"]
            if doc.transcription.exists()
            else None
        )
        critique = (
            json.loads(doc.critique.read_text())
            if doc.critique.exists()
            else None
        )
        found[name] = (
            transcription,
            content_hash(batch, volatile=BATCH_VOLATILE),
            critique,
        )
    return found


class TestReuse:
    def test_a_second_run_asks_the_engine_nothing(self, runs, tmp_path):
        """A new run starts from fresh state directories: every view is
        made again, and the LLM cache answers every call it makes. It takes
        the corpus from another folder: no key depends on where the corpus
        lives, though an image's call names its path (call_text)."""
        first = runs.run("first")
        asked = len(runs.calls.asked)
        elsewhere = fc.copy_to(tmp_path / "elsewhere" / "corpus")
        second = runs.run("second", elsewhere)
        folders = {
            run: RunDir(run).manifest()["corpus"]["folder"]
            for run in (first, second)
        }
        assert folders[second] == str(elsewhere.resolve())
        assert folders[first] != folders[second]
        assert len(runs.calls.asked) == asked
        assert events(second, "call") == []
        assert events(second, "cache-miss") == []
        hits = events(second, "cache-hit")
        assert {hit["reason"] for hit in hits} == {"key match"}
        # the documents' calls, and the clustering stage's adjudication
        # (#106), which is the corpus's and names no document
        adjudicated = [hit for hit in hits if hit["role"] == "adjudication"]
        assert adjudicated
        assert all("subject" not in hit for hit in adjudicated)
        assert {
            hit["subject"] for hit in hits if hit["role"] != "adjudication"
        } == {sha1(name) for name in EXTRACTED}
        assert events(second, "reused") == []
        assert set(started(second)) == every_stage()
        assert statuses(second) == {DONE}
        assert view_hashes(runs, second) == view_hashes(runs, first)
        assert corpus_events(second)[-2:] == [
            ("started", PROBE),
            ("done", PROBE),
        ]
        assert read_jsonl(second / ALERTS) == []
        assert runs.refused.asked == []

    def test_invoking_the_run_again_reuses_every_view(self, runs):
        run = runs.run("first")
        asked = len(runs.calls.asked)
        assert runs.run("first") == run
        assert len(RunDir(run).manifest()["invocations"]) == 2
        # no stage is made again: every view stands, and no call is made
        assert started(run, invocation=-1) == []
        assert set(stages_of(run, "reused", invocation=-1)) == every_stage()
        assert len(runs.calls.asked) == asked
        assert calls_by_stage(run, invocation=-1) == set()
        # the corpus stages run in every invocation past the barrier
        names = [*registered(), PROBE]
        assert corpus_events(run, invocation=-1) == [
            (event, name) for name in names for event in ("started", "done")
        ]
        assert len(runs.probe.handed) == 2
        assert scratch.is_complete(run)
        # a reused view is checked again, and still raises no alert; the
        # closing report counts every reused stage done
        assert read_jsonl(run / ALERTS) == []
        assert closing_report(runs.said["first"]) == summarise(
            run_state(RunDir(run))
        )
        assert set(closing_report(runs.said["first"])) == {
            stage for _sha1, stage in every_stage()
        }
        assert runs.refused.asked == []

    def test_a_sharper_chunk_prompt_recomputes_extraction_and_what_follows(
        self, runs, monkeypatch
    ):
        """A rule added to the chunk prompt changes every chunk call's key:
        the engine is asked again for every extraction, and, where the
        batch it makes differs (the family history, whose mistake the rule
        mends), for the critique that follows it, and for the clustering
        stage's adjudication, whose prompts give that batch's claims as
        evidence (#106). The transcriptions, the entity manifests and every
        other critique come from the cache."""
        first = runs.run("first")
        before = len(runs.calls.asked)
        monkeypatch.setattr(
            chunking,
            "CHUNK_PROMPT",
            chunking.CHUNK_PROMPT + answers.SHARPER_RULE + "\n",
        )
        sharper = runs.run("sharper")
        asked = runs.calls.asked[before:]
        assert {what for what, _name in asked} == {
            "chunk",
            "critique",
            "adjudication",
        }
        assert {name for what, name in asked if what == "chunk"} == set(
            EXTRACTED
        )
        assert [name for what, name in asked if what == "critique"] == [
            fc.NARRATIVE
        ]
        assert calls_by_stage(sharper) == {
            (sha1(name), "extraction") for name in EXTRACTED
        } | {(sha1(fc.NARRATIVE), "critique"), (None, "clustering")}
        # each conversation the cache answered is resumed, without a call,
        # for the new chunk prompts to fork
        assert {e["subject"] for e in events(sharper, "cache-resume")} == {
            sha1(name) for name in EXTRACTED
        }
        missed = {
            (e["role"], e.get("call"), e["reason"])
            for e in events(sharper, "cache-miss")
        }
        assert missed == {
            ("extract", "fork", "absent"),
            ("critique", "complete", "absent"),
            ("adjudication", "complete", "absent"),
        }
        assert statuses(sharper) == {DONE}
        # the family history's batch is mended, and its critique has
        # nothing to flag; every other view is the first run's
        mended = runs.batch(sharper, fc.NARRATIVE)
        assert values(
            said_in(fc.NARRATIVE, mended), "nicholas-lobig", "date_of_birth"
        ) == {u.Date(1950)}
        views = view_hashes(runs, sharper)
        was = view_hashes(runs, first)
        transcription, batch, critique = views[fc.NARRATIVE]
        assert transcription == was[fc.NARRATIVE][0]
        assert batch != was[fc.NARRATIVE][1]
        assert critique == []
        for name in fc.DOCUMENTS:
            if name != fc.NARRATIVE:
                assert views[name] == was[name], name
        assert runs.refused.asked == []


# -- clustering's trap outcomes -----------------------------------------------


# the merges clustering withholds from the traps, and what it would take
# to make each: recorded as expected here (#380), not mended
HELD_APART = {
    "kin-edge": "the family history's, the talk's and the export's "
    "mentions of Florence, whose links are confident, merge",
    "merge-carrying-death": "the postcard's 'Gustl', which adjudication "
    "says is the export's Gustav, merges with Gustav's other mentions",
}


def trap(name: str) -> fc.Trap:
    (found,) = [entry for entry in fc.TRAPS if entry.name == name]
    return found


@dataclass
class Identity:
    """The clustering stage's identity map, with every mention read in
    the universe's terms: which person, place or occupation of the
    universe it names, and which document of the corpus names it. No
    document of the corpus names two mentions of one universe entity, so
    a universe key and a file name name one mention."""

    map: dict
    who: dict[str, tuple[str, str]]

    def of(self, key: str) -> dict[str, str]:
        """The fingerprint of each mention of a universe entity, by the
        name of the document that gives it."""
        return {
            document: fingerprint
            for fingerprint, (known, document) in self.who.items()
            if known == key
        }

    def cluster(self, fingerprint: str) -> str:
        return self.map["mentions"][fingerprint]["cluster"]

    def method(self, fingerprint: str) -> str:
        return self.map["mentions"][fingerprint]["method"]

    def clusters_of(self, key: str) -> set[str]:
        return {self.cluster(f) for f in self.of(key).values()}

    def methods_of(self, key: str) -> set[str]:
        return {self.method(f) for f in self.of(key).values()}

    def candidate(self, fingerprint: str) -> dict:
        """The review entry of the candidate cluster a mention is in: its
        members, its pairs and what makes it conflict. Only an ambiguous,
        an adjudicated or a conflicting cluster is reviewed, since a
        confident one merged."""
        (found,) = [
            entry
            for entry in self.map["review"]
            if entry["status"]
            in (
                clustering.AMBIGUOUS,
                clustering.ADJUDICATED,
                clustering.CONFLICTING,
            )
            and fingerprint in entry["members"]
        ]
        return found

    def pair(self, entry: dict, first: str, second: str) -> tuple[str, str]:
        """(link, evidence) of a pair of a review entry, whichever way
        round the entry holds it."""
        (found,) = [
            pair
            for pair in entry["pairs"]
            if set(pair["mentions"]) == {first, second}
        ]
        return found["link"], found["evidence"]

    def verdict(self, entry: dict, first: str, second: str) -> str | None:
        """The verdict adjudication gave on a pair of a review entry."""
        (found,) = [
            pair
            for pair in entry["pairs"]
            if set(pair["mentions"]) == {first, second}
        ]
        return found["verdict"]

    def keys_of(self, fingerprints: Iterable[str]) -> set[tuple[str, str]]:
        return {self.who[fingerprint] for fingerprint in fingerprints}

    def labels(self) -> set[str]:
        return {mention["label"] for mention in self.map["mentions"].values()}


@pytest.fixture
def identity(runs, first) -> Identity:
    """The run's identity map, as the clustering stage left it in its
    directory, with every mention in the universe's terms."""
    return identity_of(runs, first)


def identity_of(runs: Scratch, run: Path) -> Identity:
    """A run's identity map, with every mention in the universe's
    terms."""
    stage = run / CORPUS_DIR / clustering.STAGE
    found = json.loads((stage / clustering.IDENTITY_MAP).read_text())
    documents = {sha1(name): name for name in fc.DOCUMENTS}
    known = {
        sha1(name): truth(name, runs.batch(run, name)) for name in fc.DOCUMENTS
    }
    who = {}
    for fingerprint, mention in found["mentions"].items():
        name = documents[mention["document"]]
        entity = known[mention["document"]][mention["local_id"]]
        assert (entity, name) not in who.values(), mention["label"]
        who[fingerprint] = (entity, name)
    return Identity(found, who)


class TestTheTrapsInTheIdentityMap:
    def test_the_map_names_every_mention_of_every_document(self, identity):
        """Every entity of every batch is a mention, and each is
        clustered: the corpus marks none for extraction alone."""
        assert len(identity.who) == identity.map["summary"]["mentions"]
        assert {document for _key, document in identity.who.values()} == set(
            fc.DOCUMENTS
        )
        assert identity.map["summary"]["unclustered_mentions"] == 0
        assert clustering.UNCLUSTERED not in {
            identity.method(f) for f in identity.who
        }

    def test_the_homonym_ursulas_stay_apart(self, identity):
        """The daughter's three mentions and the widow's two make one
        candidate cluster, which the family history's two make conflict:
        one manifest told them apart, so the pair cannot link (a soft
        cannot-link). Nothing in the cluster merges, so no cluster holds
        both Ursulas and the widow takes none of the daughter's death."""
        daughter = identity.of("ursula-loebig")
        widow = identity.of("ursula-loebig-angerring")
        assert set(daughter) == {fc.NARRATIVE, fc.DECK, fc.EXPORT}
        assert set(widow) == {fc.NARRATIVE, fc.EXPORT}
        assert identity.methods_of("ursula-loebig") == {clustering.CONFLICTING}
        assert identity.methods_of("ursula-loebig-angerring") == {
            clustering.CONFLICTING
        }
        # each mention a cluster of its own, and no cluster shared
        apart = identity.clusters_of("ursula-loebig")
        others = identity.clusters_of("ursula-loebig-angerring")
        assert (len(apart), len(others)) == (3, 2)
        assert apart & others == set()
        entry = identity.candidate(daughter[fc.EXPORT])
        assert set(entry["members"]) == {*daughter.values(), *widow.values()}
        assert entry["status"] == clustering.CONFLICTING
        assert entry["conflict"]["how"] == clustering.SOFT
        assert identity.keys_of(entry["conflict"]["mentions"]) == {
            ("ursula-loebig", fc.NARRATIVE),
            ("ursula-loebig-angerring", fc.NARRATIVE),
        }
        # and no pair of them is a confident link: every mention keys on
        # (Ursula, Löbig), and the export describes its two alike, so each
        # mention outside it matches both anchors, more than max-anchors
        # allows; the daughter's kin agree with the export's daughter
        # (key/unknown/agree), which the anchors overrule
        assert {pair["link"] for pair in entry["pairs"]} == {
            clustering.AMBIGUOUS
        }
        assert {pair["evidence"] for pair in entry["pairs"]} == {
            "key/unknown/unknown",
            "key/unknown/agree",
        }
        assert identity.map["summary"]["multi_anchored_mentions"] == 3
        assert "never merges them" in trap("homonyms").expected

    def test_mirrored_kinship_is_read_as_one_relation(self, identity, batches):
        """The talk gives Klara no date and no parent: it says only that
        Sebastian and Hildegard have her as a child. The export gives her
        a father and a mother, and her birth year, 1917. Clustering lifts
        kin from both ends, so the two agree and the pair is a confident
        link on kinship alone (the evidence reads name/dates/kin); the
        family history's link to the export is corroborated by both her
        dates and her kin. The postcard states it from Klara's end too
        (her father, its writer), but names its writer only by its own
        words for him, "Dein Vater", which is his label, with the status
        kinship (ADR-0035 §2.1): no name, so his mention keys on nothing,
        stays a singleton and lends her no kin. The postcard's own
        mention of her links to the export on her name alone, so her
        candidate cluster is ambiguous, around its confident component,
        the export's, the family history's and the talk's mentions.
        Adjudication says the postcard's and the export's are the same
        (#587), so the whole cluster merges, adjudicated. Her parents,
        named by the export, the family history and the talk, merge."""
        klara = identity.of("klara-loebig")
        deck, export = batches.said(fc.DECK), batches.said(fc.EXPORT)
        assert values(deck, "sebastian-loebig", "child") >= {"klara-loebig"}
        assert values(deck, "klara-loebig", "father") == set()
        assert values(export, "klara-loebig", "father") == {"sebastian-loebig"}
        assert values(export, "sebastian-loebig", "child") == set()
        entry = identity.candidate(klara[fc.EXPORT])
        assert identity.pair(entry, klara[fc.DECK], klara[fc.EXPORT]) == (
            clustering.CONFIDENT,
            "key/unknown/agree",
        )
        assert identity.pair(entry, klara[fc.NARRATIVE], klara[fc.EXPORT]) == (
            clustering.CONFIDENT,
            "key/agree/agree",
        )
        # the postcard names her father, its writer, from her end, by a
        # kinship term: his mention is no name, pairs with no one, and so
        # takes no part in her candidate cluster
        assert values(batches.said(fc.LETTER), "klara-loebig", "father") == {
            "sebastian-loebig"
        }
        (writer,) = batches.entities(fc.LETTER, "Dein Vater")
        assert writer.structure.status == "kinship"
        father = identity.of("sebastian-loebig")[fc.LETTER]
        assert identity.method(father) == clustering.SINGLETON
        assert father not in entry["members"]
        assert entry["status"] == clustering.ADJUDICATED
        assert entry["conflict"] is None
        assert entry["contradiction"] is None
        # its own mention of her links to the export on the name alone,
        # the one pair adjudication is asked of her, and its `same` joins
        # it to the confident component of the rest
        assert identity.pair(entry, klara[fc.LETTER], klara[fc.EXPORT]) == (
            clustering.AMBIGUOUS,
            "key/unknown/unknown",
        )
        assert identity.verdict(entry, klara[fc.LETTER], klara[fc.EXPORT]) == (
            verdicts.SAME
        )
        assert [pair["link"] for pair in entry["pairs"]].count(
            clustering.AMBIGUOUS
        ) == 1
        assert set(klara) == {fc.EXPORT, fc.NARRATIVE, fc.DECK, fc.LETTER}
        assert identity.methods_of("klara-loebig") == {clustering.ADJUDICATED}
        assert len(identity.clusters_of("klara-loebig")) == 1
        # and the parents, named from both ends, merge with the export's:
        # their kin agree through their children
        for parent in ("sebastian-loebig", "hildegard-loebig"):
            mentions = identity.of(parent)
            named = {fc.NARRATIVE, fc.DECK, fc.EXPORT}
            assert named <= set(mentions), parent
            assert {identity.method(mentions[n]) for n in named} == {
                clustering.CONFIDENT
            }, parent
            assert len({identity.cluster(mentions[n]) for n in named}) == 1
        assert identity.map["summary"]["recordless_by_kind"] == {}

    def test_the_death_carrying_merge_is_a_confident_component(
        self, identity, batches
    ):
        """The certificate's Gustave Lobig links confidently to the
        export's Gustav Löbig, which carries his death, and so do the
        family history's and the talk's mentions of him. The postcard's
        lone 'Gustl', a given name with no surname, meets the export's
        Gustav, whose record has the nickname Gustl, only as a lone name
        (`given`, ADR-0035 §7, rule 1): it pairs, but never links
        confidently. That one ambiguous link makes the candidate cluster
        ambiguous. Adjudication says the two are the same (#587), but
        'Gustl' matches no name of the certificate's Gustave Lobig, a
        contradiction no verdict excuses, so the cluster falls back to
        its confident component, which merges (#568): the certificate's
        mention with those that carry his death, which reaches it by
        that merge alone. The postcard's 'Gustl' stays apart."""
        gustav = identity.of("gustav-loebig")
        # every document names him but Florence's death certificate
        assert set(gustav) == set(fc.DOCUMENTS) - {fc.DEATH_CERTIFICATE}
        entry = identity.candidate(gustav[fc.EXPORT])
        assert entry["status"] == clustering.ADJUDICATED
        assert entry["conflict"] is None
        assert identity.verdict(
            entry, gustav[fc.LETTER], gustav[fc.EXPORT]
        ) == (verdicts.SAME)
        # 'Gustl' against the certificate's 'Gustave Lobig': no name
        # match, never a pair of the cluster
        assert identity.keys_of(entry["contradiction"]["mentions"]) == {
            ("gustav-loebig", fc.LETTER),
            ("gustav-loebig", fc.CERTIFICATE),
        }
        assert entry["contradiction"]["evidence"] == "none/unknown/unknown"
        # the export is the roster the other documents meet, so every
        # pair of the cluster is a mention with its GEDCOM anchor
        assert {
            document: identity.pair(entry, gustav[fc.EXPORT], fingerprint)
            for document, fingerprint in gustav.items()
            if document != fc.EXPORT
        } == {
            fc.CERTIFICATE: (clustering.CONFIDENT, "key/agree/unknown"),
            fc.NARRATIVE: (clustering.CONFIDENT, "key/agree/agree"),
            fc.DECK: (clustering.CONFIDENT, "key/unknown/agree"),
            fc.LETTER: (clustering.AMBIGUOUS, "given/unknown/unknown"),
        }
        assert set(entry["members"]) == set(gustav.values())
        assert identity.method(gustav[fc.LETTER]) == clustering.AMBIGUOUS
        component = set(gustav) - {fc.LETTER}
        assert {identity.method(gustav[n]) for n in component} == {
            clustering.CONFIDENT_COMPONENT
        }
        assert len(identity.clusters_of("gustav-loebig")) == 2
        assert identity.cluster(gustav[fc.CERTIFICATE]) == identity.cluster(
            gustav[fc.NARRATIVE]
        )
        # the death the merge would carry is stated by the documents the
        # certificate's mention meets, and by no other
        assert (
            values(
                batches.said(fc.CERTIFICATE), "gustav-loebig", "date_of_death"
            )
            == set()
        )
        for name in (fc.NARRATIVE, fc.EXPORT):
            assert values(
                batches.said(name), "gustav-loebig", "date_of_death"
            ) == {u.Date(1996)}, name
        # merging over the confident links alone would join 17 clusters,
        # none conflicting (an event links confidently once the clusters
        # holding its people merge, and a wedding once its marriage
        # merges, adjudicated ones included); the rule merges 51
        # mentions, among them the export's Hesse and Australia,
        # jurisdictions of its PLACs, with the sources' own (#571), the
        # confident components of the clusters adjudication could not
        # merge (Gustav's and Nicholas's) and the clusters adjudication
        # merged, each counted apart; it leaves the conflicting clusters'
        # confident links unmerged
        summary = identity.map["summary"]
        assert summary["confident_links_only"] == {
            "clusters": 17,
            "mentions": 48,
            "conflicting": 0,
        }
        assert summary["merged_mentions"] == 51
        assert summary["clusters"][clustering.CONFIDENT_COMPONENT] == 2
        assert summary["cluster_mentions"][clustering.CONFIDENT_COMPONENT] == 6
        assert summary["clusters"][clustering.ADJUDICATED] == 3
        assert summary["cluster_mentions"][clustering.ADJUDICATED] == 10

    def test_a_kin_edge_makes_the_cluster_conflict_hard(self, identity):
        """The death certificate's Florence and the mother it names by
        Florence's own birth name both key on (Florence, Faxridge), and on
        (Florence, Lobig) through the surname of the kin the certificate
        names; the export's Florence has both names, so it anchors each
        route, and each mention pairs with the export's Florence alone and
        never with the other: blocking is not transitive. The
        closure joins them all the same, with the family history's and
        the talk's mentions of her, and the impossibility filter refuses
        the candidate cluster, since the certificate makes one of its
        members the other's mother: a hard conflict, the corpus's one.
        Nothing in it merges, so the mother shares no cluster with
        Florence."""
        florence = identity.of("florence-lobig")
        assert set(florence) == {
            fc.DEATH_CERTIFICATE,
            fc.EXPORT,
            fc.NARRATIVE,
            fc.DECK,
        }
        mother = identity.of("sipkje-faxridge")
        assert set(mother) == {fc.DEATH_CERTIFICATE}
        mother = mother[fc.DEATH_CERTIFICATE]
        entry = identity.candidate(florence[fc.EXPORT])
        assert set(entry["members"]) == {*florence.values(), mother}
        assert entry["status"] == clustering.CONFLICTING
        assert entry["conflict"]["how"] == clustering.HARD
        assert set(entry["conflict"]["mentions"]) == {
            florence[fc.DEATH_CERTIFICATE],
            mother,
        }
        # every pair is a mention with its GEDCOM anchor: the mother's
        # by the name alone, Florence's corroborated by her death's date
        # or by her kin
        assert {
            document: identity.pair(entry, florence[fc.EXPORT], fingerprint)
            for document, fingerprint in {
                **florence,
                "mother": mother,
            }.items()
            if document != fc.EXPORT
        } == {
            fc.DEATH_CERTIFICATE: (clustering.CONFIDENT, "key/agree/unknown"),
            fc.NARRATIVE: (clustering.CONFIDENT, "key/unknown/agree"),
            fc.DECK: (clustering.CONFIDENT, "key/unknown/agree"),
            "mother": (clustering.AMBIGUOUS, "key/unknown/unknown"),
        }
        assert len(entry["pairs"]) == 4
        # each mention a cluster of its own, the mother's among them
        assert identity.methods_of("florence-lobig") == {
            clustering.CONFLICTING
        }
        assert identity.method(mother) == clustering.CONFLICTING
        apart = identity.clusters_of("florence-lobig")
        assert len(apart) == len(florence)
        assert identity.cluster(mother) not in apart
        # the corpus's one hard conflict
        assert identity.map["summary"]["conflicting_hard"] == 1
        kin_edge = trap("kin-edge")
        assert "conflicts hard" in kin_edge.expected
        assert set(kin_edge.documents) == set(florence)
        assert set(kin_edge.people) == {"florence-lobig", "sipkje-faxridge"}
        # and no trap says the corpus lacks one
        assert not any("no hard" in entry.expected for entry in fc.TRAPS)

    def test_the_traps_the_merge_rule_cannot_meet_are_recorded(self):
        """Each trap the rule cannot meet says so where the corpus
        describes it, and the xfails of this class are those traps: one
        each, strict, and each reasoned from HELD_APART, so that dropping
        one, loosening it or reasoning it otherwise fails here rather
        than leaving the record to rot."""
        assert {
            entry.name for entry in fc.TRAPS if "(#380)" in entry.expected
        } == set(HELD_APART)
        assert "its confident component, which merges (#568)" in (
            trap("merge-carrying-death").expected
        )
        assert "the whole cluster merges, adjudicated" in (
            trap("mirrored-kinship").expected
        )
        assert "stay apart" in trap("kin-edge").expected
        assert "'Gustl' stays apart" in trap("merge-carrying-death").expected
        marks = [
            mark
            for name, member in vars(type(self)).items()
            if name.startswith("test")
            for mark in getattr(member, "pytestmark", ())
            if mark.name == "xfail"
        ]
        assert sorted(
            key
            for mark in marks
            for key, text in HELD_APART.items()
            if mark.kwargs["reason"].startswith(text + ": ")
        ) == sorted(HELD_APART)
        assert all(mark.kwargs["strict"] for mark in marks)

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=HELD_APART["kin-edge"]
        + ": the death certificate's mention of her mother, named by "
        "Florence's own birth name, joins her candidate cluster through "
        "the export's Florence and is her kin, so the cluster conflicts "
        "(hard), and the whole-cluster merge rule (#361) leaves every "
        "member of a conflicting cluster apart, the confident links among "
        "them included. A rule that merged over the confident links alone "
        "(#66) would merge these three, and the certificate's own mention "
        "of her with them; the mother's mention would stay apart either "
        "way, since its one link is ambiguous.",
    )
    def test_florences_mentions_merge(self, identity):
        florence = identity.of("florence-lobig")
        mother = identity.of("sipkje-faxridge")[fc.DEATH_CERTIFICATE]
        entry = identity.candidate(florence[fc.EXPORT])
        _premise(
            all(
                identity.pair(entry, florence[name], florence[fc.EXPORT])[0]
                == clustering.CONFIDENT
                for name in (fc.NARRATIVE, fc.DECK)
            ),
            "the family history's and the talk's mentions link confidently "
            "to the export's",
        )
        _premise(
            identity.pair(entry, mother, florence[fc.EXPORT])[0]
            == clustering.AMBIGUOUS,
            "the mother's mention links to the export ambiguously",
        )
        assert (
            identity.cluster(florence[fc.NARRATIVE])
            == identity.cluster(florence[fc.DECK])
            == identity.cluster(florence[fc.EXPORT])
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=HELD_APART["merge-carrying-death"]
        + ": its pair with the export's Gustav is the cluster's one "
        "pending pair, and adjudication says `same` of it, but the part "
        "that `same` joins holds 'Gustl' and the certificate's Gustave "
        "Lobig, whose names do not match at all (no record of the "
        "certificate's gives the nickname), and a verdict excuses only a "
        "date conflict on its own pair (#587), so the part falls back to "
        "its confident component. A contradiction check that read a lone "
        "given name through the nickname of the anchor it met would "
        "merge it.",
    )
    def test_gustl_merges_with_gustav(self, identity):
        gustav = identity.of("gustav-loebig")
        entry = identity.candidate(gustav[fc.EXPORT])
        _premise(
            identity.verdict(entry, gustav[fc.LETTER], gustav[fc.EXPORT])
            == verdicts.SAME,
            "adjudication says the postcard's Gustl is the export's Gustav",
        )
        _premise(
            not any(
                set(pair["mentions"])
                == {gustav[fc.LETTER], gustav[fc.CERTIFICATE]}
                for pair in entry["pairs"]
            ),
            "the postcard's Gustl and the certificate's mention are no "
            "pair of the cluster",
        )
        assert identity.cluster(gustav[fc.LETTER]) == identity.cluster(
            gustav[fc.EXPORT]
        )


def _premise(holds: bool, what: str) -> None:
    """A strict xfail's premise, as its reason states it: when it no
    longer holds, the test errors, rather than xfailing for a reason
    other than its own."""
    if not holds:
        raise RuntimeError(f"the xfail's premise no longer holds: {what}")


# -- adjudication's recorded verdicts -----------------------------------------


UNIONS = ("marriage", "wedding")


def universe_keys(runs: Scratch, run: Path) -> dict[str, dict[str, str]]:
    """Each document's universe key of each of its entities, by local id.
    The export's unions and weddings stand for themselves (export_truth),
    so each is given the key of the corpus's own of one couple: a
    marriage by its two spouses, a wedding by the marriage it starts."""
    keys = {name: truth(name, runs.batch(run, name)) for name in fc.DOCUMENTS}
    couples = {
        (entity.kind, frozenset(entity.couple)): entity.truth
        for recorded in answers.RECORDED.values()
        for entity in recorded.entities
        if entity.couple
    }
    batch = runs.batch(run, fc.EXPORT)
    kinds = {entity.id: entity.kind for entity in batch.entities}
    known = keys[fc.EXPORT]
    spouses: dict[str, set[str]] = {}
    started: dict[str, str] = {}
    for claim in batch.claims:
        subject = claim.subject.local_id
        value = getattr(getattr(claim.value, "value", None), "local_id", None)
        if claim.property == "participant" and kinds[subject] == "marriage":
            spouses.setdefault(subject, set()).add(known[value])
        if claim.property == "started_by":
            started[value] = subject
    for local_id, kind in kinds.items():
        if kind in UNIONS:
            union = started.get(local_id, local_id)
            known[local_id] = couples[(kind, frozenset(spouses[union]))]
    return keys


class TestTheVerdicts:
    """Clustering's adjudication asks the recorded engine of the corpus's
    ambiguous pairs, and the engine answers each by the verdict the
    corpus records for it (tests/fixtures/corpus/verdicts.py): each a
    faithful model's, so `same` exactly when the two are one entity of
    the universe. Every pair asked is one of the four that make a trap's
    candidate cluster ambiguous (Klara's, Gustav's and Nicholas's people,
    and Gustav and Florence's marriage) or of Klara's emigrations, asked
    because two of the three give no date; and the namesakes never are:
    the Ursulas' and Florence's clusters conflict, and a conflicting
    cluster is never asked."""

    def test_every_pair_asked_has_its_recorded_verdict(
        self, runs, first, calls
    ):
        """Each recorded verdict is given once, in the run's one
        invocation, and none is left unasked; the map counts them all,
        and no other."""
        assert sorted(calls.verdicts, key=lambda v: sorted(v.pair)) == sorted(
            verdicts.VERDICTS, key=lambda v: sorted(v.pair)
        )
        assert calls.of("adjudication") == ["corpus"] * 3
        summary = identity_of(runs, first).map["summary"]["adjudication"]
        assert summary["asked_pairs"] == len(verdicts.VERDICTS)
        assert summary["verdicts"] == {
            verdicts.SAME: len(verdicts.VERDICTS),
            verdicts.DIFFERENT: 0,
            "uncertain": 0,
        }
        assert summary["unanswered_pairs"] == 0
        assert summary["rejected_lines"] == 0

    def test_each_verdict_is_the_universes(self, runs, first, identity):
        """`same` exactly when the two mentions are one universe entity;
        every review pair a verdict settles is a recorded one, by its
        mentions' documents and labels."""
        keys = universe_keys(runs, first)
        mentions = identity.map["mentions"]

        def named(fingerprint: str) -> verdicts.Mention:
            _key, document = identity.who[fingerprint]
            return document, mentions[fingerprint]["label"]

        def key(fingerprint: str) -> str:
            _key, document = identity.who[fingerprint]
            return keys[document][mentions[fingerprint]["local_id"]]

        settled = {}
        for entry in identity.map["review"]:
            for pair in entry["pairs"]:
                if pair.get("verdict") is not None:
                    first_, second = pair["mentions"]
                    settled[frozenset((named(first_), named(second)))] = (
                        pair["verdict"],
                        key(first_) == key(second),
                    )
        assert set(settled) == set(verdicts.BY_PAIR)
        for pair, (verdict, one) in settled.items():
            assert verdict == verdicts.BY_PAIR[pair].verdict, sorted(pair)
            assert (verdict == verdicts.SAME) == one, sorted(pair)

    def test_no_cluster_of_the_adjudicated_map_holds_two_entities(
        self, runs, first, identity
    ):
        """No cluster holds mentions of two universe entities, the export's
        unions read as the corpus's own of one couple; the merges
        adjudication made are among them."""
        keys = universe_keys(runs, first)
        docs = [
            Doc(
                name,
                clustering.Manifest(sha1(name), runs.batch(first, name)),
                keys[name],
            )
            for name in fc.DOCUMENTS
        ]
        assert_no_false_merge(docs, identity.map)
        assert identity.map["summary"]["clusters"][clustering.ADJUDICATED] == 3

    def test_the_namesakes_are_never_asked(self, identity):
        """The traps whose mentions are of two people (the homonym
        Ursulas, Florence and the mother named by her birth name) sit in
        conflicting clusters, so adjudication asks none of their pairs."""
        for key in (
            "ursula-loebig",
            "ursula-loebig-angerring",
            "florence-lobig",
            "sipkje-faxridge",
        ):
            for fingerprint in identity.of(key).values():
                entry = identity.candidate(fingerprint)
                assert entry["status"] == clustering.CONFLICTING, key
                assert all(
                    pair.get("verdict") is None for pair in entry["pairs"]
                ), key

    def test_nicholass_early_birth_keeps_his_mention_apart(self, identity):
        """The family history's Nicholas, born 1850 by the recorded
        model's misreading, and the export's: adjudication says `same`,
        which excuses the dates of that pair alone. The talk's 1950
        contradicts the early birth, so the family history's mention
        stays apart, and the talk's and the export's merge. The corpus
        cannot show the excuse itself, since the talk's conflict is met
        first either way: test_adjudication.py covers it
        (test_a_same_verdict_overrides_a_date_contradiction)."""
        nicholas = identity.of("nicholas-lobig")
        entry = identity.candidate(nicholas[fc.EXPORT])
        assert entry["status"] == clustering.ADJUDICATED
        assert identity.pair(
            entry, nicholas[fc.NARRATIVE], nicholas[fc.EXPORT]
        ) == (clustering.AMBIGUOUS, "key/conflict/agree")
        assert identity.verdict(
            entry, nicholas[fc.NARRATIVE], nicholas[fc.EXPORT]
        ) == (verdicts.SAME)
        assert identity.keys_of(entry["contradiction"]["mentions"]) == {
            ("nicholas-lobig", fc.NARRATIVE),
            ("nicholas-lobig", fc.DECK),
        }
        assert identity.method(nicholas[fc.NARRATIVE]) == clustering.AMBIGUOUS
        assert identity.method(nicholas[fc.DECK]) == (
            clustering.CONFIDENT_COMPONENT
        )
        assert identity.cluster(nicholas[fc.DECK]) == identity.cluster(
            nicholas[fc.EXPORT]
        )
        assert identity.cluster(nicholas[fc.NARRATIVE]) != identity.cluster(
            nicholas[fc.EXPORT]
        )
        assert "the family history's mention stays apart" in (
            trap("early-birth").expected
        )

    def test_a_different_verdict_keeps_the_pair_apart(
        self, runs, calls, monkeypatch
    ):
        """Were the postcard's Klara said to be another than the export's,
        the `different` would cut the cluster's one pending pair: the
        postcard's mention would be a cluster of its own, adjudicated
        apart, and the confident component of the rest would merge, and
        no cluster would hold two universe entities. Nor would any event
        have as its principals two people clustering keeps apart (#610):
        Klara's emigration, `same` on every pair, holds the postcard's
        Klara and the others', so it does not merge, and each of its
        mentions stays apart. Universe keys cannot see that merge, since
        the universe has one emigration, so it is asserted directly."""
        pair = frozenset(
            ((fc.LETTER, "Klara Löbig"), (fc.EXPORT, "Klara Löbig"))
        )
        recorded = verdicts.BY_PAIR[pair]
        monkeypatch.setitem(
            verdicts.BY_PAIR,
            pair,
            dataclasses.replace(recorded, verdict=verdicts.DIFFERENT),
        )
        run = runs.run("different")
        identity = identity_of(runs, run)
        klara = identity.of("klara-loebig")
        entry = identity.candidate(klara[fc.EXPORT])
        assert identity.verdict(entry, klara[fc.LETTER], klara[fc.EXPORT]) == (
            verdicts.DIFFERENT
        )
        assert identity.method(klara[fc.LETTER]) == clustering.ADJUDICATED
        rest = {fc.EXPORT, fc.NARRATIVE, fc.DECK}
        assert {identity.method(klara[n]) for n in rest} == {
            clustering.CONFIDENT_COMPONENT
        }
        assert len({identity.cluster(klara[n]) for n in rest}) == 1
        assert identity.cluster(klara[fc.LETTER]) != identity.cluster(
            klara[fc.EXPORT]
        )
        assert identity.map["summary"]["adjudicated_apart"] == 1
        # Klara's emigration, which every document gives with its Klara as
        # principal, never holds the postcard's with the others' (#610):
        # the part `same` joins has principals held apart, so it falls
        # back to its confident components, each mention alone
        emigration = identity.of("emigration-1938")
        assert set(emigration) == {fc.LETTER, fc.NARRATIVE, fc.DECK}
        assert len({identity.cluster(f) for f in emigration.values()}) == 3
        assert {identity.method(f) for f in emigration.values()} == {
            clustering.AMBIGUOUS
        }
        contradiction = identity.candidate(emigration[fc.DECK])[
            "contradiction"
        ]
        assert emigration[fc.LETTER] in contradiction["mentions"]
        assert contradiction["evidence"] == clustering.PRINCIPALS_APART
        keys = universe_keys(runs, run)
        docs = [
            Doc(
                name,
                clustering.Manifest(sha1(name), runs.batch(run, name)),
                keys[name],
            )
            for name in fc.DOCUMENTS
        ]
        assert_no_false_merge(docs, identity.map)


# -- the run report -----------------------------------------------------------


# the universe's names as the corpus's documents carry them, which no
# aggregate may say, whatever it counts
NAMES = (
    "Löbig",
    "Lobig",
    "Loebig",
    "Flörburg",
    "Floerburg",
    "Fläckfeld",
    "Flaeckfeld",
    "Mockburg",
    "Hesse",
    "Laudler",
    "Hildegard",
    "Hilde",
    "Ursula",
    "Sebastian",
    "Wastl",
    "Klara",
    "Clara",
    "Pünktchen",
    "Puenktchen",
    "Gustav",
    "Gustave",
    "Gustl",
    "Gus",
    "Eugen",
    "Nicholas",
    "Florence",
    "Faxridge",
    "Angerring",
    "Hawthorn",
    "Juniper",
    "Stupfield",
    "Australia",
    "Victoria",
    "Flund",
    "hatter",
)
# the folded spellings a name of the universe may also be written in
FOLDINGS = {"ö": "oe", "ä": "ae", "ü": "ue", "ß": "ss"}


def universe_names() -> str:
    """Every name the universe gives a person, a place or an entity, and
    the spelling of each with its accents folded, as one text a name is
    looked for in."""
    found = set()
    for person in u.PEOPLE:
        found.update({person.label, *(n.text for n in person.names)})
    for place in u.PLACES:
        found.update({place.label, *(n.text for n in place.names)})
    for entity in u.ENTITIES:
        found.update({entity.label, *entity.names})
    folded = set()
    for name in found:
        spelling = name.casefold()
        for accented, plain in FOLDINGS.items():
            spelling = spelling.replace(accented, plain)
        folded.add(spelling)
    return "\n".join(sorted({name.casefold() for name in found} | folded))


@dataclass
class Report:
    """A run's report, as `yizkr scratch report` writes it: report.md,
    which may be posted, and report.json, which stays local, with the
    aggregates report.md is rendered from."""

    markdown: str
    local: str
    aggregate: dict

    def stage(self, name: str) -> dict:
        (row,) = [r for r in self.aggregate["stages"] if r["name"] == name]
        return row

    def corpus_stage(self, name: str) -> dict:
        (row,) = [
            r for r in self.aggregate["corpus_stages"] if r["name"] == name
        ]
        return row


def report_of(run: Path) -> Report:
    """`yizkr scratch report` over a run, as the command runs it, and the
    two files it writes that a test reads."""
    args = build_parser().parse_args(
        ["scratch", "report", str(run), "--runs", str(run.parent)]
    )
    assert args.func(args) == 0
    folder = run / REPORT_DIR
    return Report(
        (folder / MARKDOWN_FILE).read_text(),
        (folder / JSON_FILE).read_text(),
        json.loads((folder / JSON_FILE).read_text())["aggregate"],
    )


def leaks(text: str, identity: Identity) -> list[str]:
    """What the corpus gives that an aggregate may never say: a name of
    the universe (NAMES), a label of a mention the run made, a document's
    file name or its stem, or the start of a document's SHA-1. A name and
    a label are looked for folded as clustering folds them (accents and
    case), as well as case-folded, so that a folded spelling is caught
    too."""
    lowered, folded = text.casefold(), clustering.fold(text)

    def said(name: str) -> bool:
        return name.casefold() in lowered or clustering.fold(name) in folded

    found = [name for name in NAMES if said(name)]
    found += [label for label in identity.labels() if said(label)]
    found += [
        name
        for name in fc.DOCUMENTS
        if name in text or Path(name).stem in text
    ]
    return found + [
        name
        for name in fc.DOCUMENTS
        for digest in (sha1(name),)
        if digest[:7] in text.casefold()
    ]


@pytest.fixture
def report(first) -> Report:
    return report_of(first)


class TestTheRunReport:
    def test_the_report_counts_every_stage_the_corpus_plans(self, report):
        """Every document of the corpus went through its stages in one
        invocation, and none was reused, held or failed: the run's first
        report says so per stage, and its rows count the documents the
        plan gives each stage."""
        assert report.aggregate["run"]["complete"] is True
        assert report.aggregate["run"]["invocations"] == 1
        assert report.aggregate["run"]["barrier"] == {
            "documents": len(fc.DOCUMENTS),
            "failed": 0,
        }
        assert report.aggregate["documents"]["planned"] == len(fc.DOCUMENTS)
        assert report.aggregate["documents"]["by_kind"] == {
            "media": 3,
            "gedcom": 1,
            "presentation": 1,
            "text": 1,
        }
        planned = {
            stage: sum(
                stage in DOCUMENT_STAGES[doc.kind]
                for doc in fc.DOCUMENTS.values()
            )
            for _sha1, stage in every_stage()
        }
        rows = {row["name"]: row for row in report.aggregate["stages"]}
        assert set(rows) == set(planned)
        for name, row in rows.items():
            assert row["documents"] == planned[name], name
            assert row[COMPUTED] == planned[name], name
            assert (row[REUSED], row[PENDING], row[FAILED]) == (0, 0, 0), name
            assert row[NEEDS_ATTENTION] == 0, name
        # what sent each computed stage to the engine, and what calls it
        assert rows["transcription"]["forced"] == {"absent": 3, "no call": 2}
        assert rows["extraction"]["forced"] == {"absent": 5}
        assert rows["critique"]["forced"] == {"absent": 5}
        assert rows["gedcom"]["forced"] == {"no call": 1}
        # and report.md's stage table is those rows
        assert "| transcription | 5 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 |" in (
            report.markdown
        )
        assert "| gedcom | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |" in (
            report.markdown
        )
        assert (
            "| transcription | none | absent 3, no call 2 | none | none |"
        ) in report.markdown

    def test_the_corpus_stages_are_the_registered_ones_and_the_probe(
        self, report
    ):
        """Past the barrier the clustering stage ran and asked for a
        look: the corpus leaves clusters it cannot merge on its own. The
        probe, the test's own stage, ran after it and wants nothing."""
        assert [row["name"] for row in report.aggregate["corpus_stages"]] == [
            *registered(),
            PROBE,
        ]
        clustering_row = report.corpus_stage(clustering.STAGE)
        assert clustering_row["state"] == NEEDS_ATTENTION
        assert clustering_row["attention"] == [
            "ambiguous clusters not merged",
            "conflicting clusters not merged",
        ]
        assert clustering_row["failure"] is None
        assert report.corpus_stage(PROBE) == {
            "name": PROBE,
            "state": DONE,
            "attention": [],
            "failure": None,
            "seconds": report.corpus_stage(PROBE)["seconds"],
        }

    def test_the_clean_corpus_raises_no_anomaly(self, report, first):
        """No check alerted on the run, and none failed: the corpus is
        clean, and the report agrees with the alert log."""
        assert report.aggregate["anomalies"] == {
            "alerts": 0,
            "findings": 0,
            "new": 0,
            "carried": 0,
            "by_check": {},
            "by_stage": {},
            "check_errors": 0,
            "rechecks": 0,
        }
        assert read_jsonl(first / ALERTS) == []
        assert (
            "0 alerts, of 0 findings (a document or the run, a stage and a "
            "check): 0 new and 0 carried. Checks that failed: 0; rechecks: 0."
        ) in report.markdown

    def test_the_clusters_are_the_identity_maps_counts(self, report, identity):
        """The report reads the clustering stage's identity map, so every
        count it shows is that map's summary, key for key, and the
        corpus's own invariants hold over both: one mention per entity of
        every batch, none unclustered, and no hub in a corpus this
        small. The counts themselves are the map's to change."""
        clusters = report.aggregate["content"]["clusters"]
        summary = identity.map["summary"]
        assert clusters["map"] == "read"
        assert clusters["status"] == NEEDS_ATTENTION
        assert clusters["counts"] == {
            "documents": summary["documents"],
            "mentions": summary["mentions"],
            "clustered mentions": summary["clustered_mentions"],
            "unclustered mentions": summary["unclustered_mentions"],
            "recordless mentions": summary["recordless_mentions"],
            "identity clusters": summary["identity_clusters"],
            "confident merges": summary["clusters"]["confident"],
            "mentions merged": summary["cluster_mentions"]["confident"],
            "confident component merges": summary["clusters"][
                "confident_component"
            ],
            "mentions merged by confident component": summary[
                "cluster_mentions"
            ]["confident_component"],
            "adjudicated merges": summary["clusters"]["adjudicated"],
            "mentions merged by adjudication": summary["cluster_mentions"][
                "adjudicated"
            ],
            "members adjudicated apart": summary["adjudicated_apart"],
            "ambiguous, left split": summary["clusters"]["ambiguous"],
            "ambiguous mentions": summary["cluster_mentions"]["ambiguous"],
            "conflicting": summary["clusters"]["conflicting"],
            "conflicting mentions": summary["cluster_mentions"]["conflicting"],
            "conflicting, hard": summary["conflicting_hard"],
            "singletons": summary["singletons"],
            "candidate pairs": summary["candidate_pairs"],
            "confident pairs": summary["confident_pairs"],
            "ambiguous pairs": summary["ambiguous_pairs"],
            "adjudication calls": summary["adjudication"][
                "calls_ambiguous_pairs"
            ],
            "pairs adjudicated": summary["adjudication"]["asked_pairs"],
            "pairs not asked, a same cannot merge": summary["adjudication"][
                "unmergeable_pairs_not_asked"
            ],
            "adjudication calls made": summary["adjudication"]["calls"],
            **{
                f"verdicts {verdict}": count
                for verdict, count in summary["adjudication"][
                    "verdicts"
                ].items()
            },
            "pairs without a verdict": summary["adjudication"][
                "unanswered_pairs"
            ],
            "adjudication lines refused": summary["adjudication"][
                "rejected_lines"
            ],
            "adjudication calls refused": summary["adjudication"][
                "refused_calls"
            ],
            "adjudication answers in conflict": summary["adjudication"][
                "conflicting_answers"
            ],
            "hub keys, not blocked on": summary["hub_keys"],
            "mentions holding them": summary["hub_mentions"],
        }
        # the critic's one finding, counted by its axis and category
        # (#386)
        critic = report.aggregate["content"]["critic"]
        assert (critic["findings"], critic["invalid"]) == (1, 0)
        assert critic["by_axis"] == {"value": 1}
        assert critic["by_category"] == {"implausible_value": 1}
        # every entity of every batch is a mention, all of them clustered
        counts = clusters["counts"]
        entities = report.aggregate["content"]["entities"]
        assert counts["documents"] == len(fc.DOCUMENTS)
        assert counts["mentions"] == entities["total"]
        assert counts["clustered mentions"] == counts["mentions"]
        assert counts["unclustered mentions"] == 0
        # the corpus is small, so no key is a hub and nothing is held
        # back from blocking
        assert (counts["hub keys, not blocked on"], summary["hub_keys"]) == (
            0,
            0,
        )
        assert counts["mentions holding them"] == 0
        # and the clusters and the entities count the same kinds
        assert clusters["by_kind"] == entities["by_kind"]
        assert set(clusters["by_kind"]) == {
            "human",
            "place",
            "marriage",
            "wedding",
            "migration",
            "occupation_class",
            "vessel",
        }
        # the counts report.md shows are those, rendered
        for label, count in counts.items():
            assert f"| {label} | {count} |" in report.markdown, label

    def test_a_second_run_is_reported_reused_by_key_match(
        self, runs, tmp_path
    ):
        """A second run makes every view again from fresh state
        directories, and the LLM cache answers every call it makes: the
        report counts each stage that calls the engine reused, by the
        reason the cache gave, and the stages that call it for nothing
        (a text file, a deck, the GEDCOM adapter) computed with no call.
        Invoked again, the run keeps how each view came about."""
        first = runs.run("first")
        made = report_of(first).aggregate["calls"]["engine"]
        second = runs.run(
            "second", fc.copy_to(tmp_path / "elsewhere" / "corpus")
        )
        report = report_of(second)
        rows = {row["name"]: row for row in report.aggregate["stages"]}
        assert {name: row["reused_by"] for name, row in rows.items()} == {
            "transcription": {"key match": 3},
            "extraction": {"key match": 5},
            "critique": {"key match": 5},
            "gedcom": {},
        }
        assert (rows["transcription"][COMPUTED], rows["gedcom"][COMPUTED]) == (
            2,
            1,
        )
        assert rows["transcription"]["forced"] == {"no call": 2}
        assert rows["extraction"][COMPUTED] == 0
        # every call the first run made of the engine, the cache answers
        assert report.aggregate["calls"]["engine"] == 0
        assert report.aggregate["calls"]["hits_by_reason"] == {
            "key match": made
        }
        assert len(events(second, "cache-hit")) == made
        assert report.aggregate["calls"]["misses_by_reason"] == {}
        assert report.aggregate["anomalies"]["alerts"] == 0
        assert "| transcription | key match 3 | no call 2 | none | none |" in (
            report.markdown
        )
        assert "| extraction | key match 5 | none | none | none |" in (
            report.markdown
        )
        # invoked again, every view stands and no stage is made again:
        # each keeps the way the invocation that made it came by it
        again = report_of(runs.run("second"))
        assert again.aggregate["run"]["invocations"] == 2
        assert {
            row["name"]: row["reused_by"] for row in again.aggregate["stages"]
        } == {name: row["reused_by"] for name, row in rows.items()}

    def test_no_universe_name_and_no_sha1_reaches_report_md(
        self, report, identity
    ):
        """report.md is rendered from the aggregates alone, so it says
        nothing the corpus gives: no name of the universe, no label of a
        mention the run made, no file name and no document's SHA-1. The
        local report carries them, so there is something to leak."""
        assert leaks(report.markdown, identity) == []
        assert set(leaks(report.local, identity)) >= set(fc.DOCUMENTS)

    def test_every_name_report_md_may_not_say_is_one_the_universe_gives(self):
        """NAMES is the universe's, not a list of its own: each name is
        part of a form the universe gives a person, a place or an entity,
        or of that form with its accents folded."""
        known = universe_names()
        assert [name for name in NAMES if name.casefold() not in known] == []
