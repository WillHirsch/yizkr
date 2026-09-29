"""The fixture corpus (#76), and whole scratch runs over it, offline.

The corpus is tests/fixtures/corpus: five documents of the fictional
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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures import corpus as fc
from tests.fixtures import example_universe as u
from tests.fixtures.corpus import answers, build
from tests.fixtures.corpus.engine import Calls, RecordedEngine
from tests.unit.test_no_wiki_before_write import refusals  # noqa: F401
from tests.unit.test_scratch import (
    MODEL,
    Driver,
    events,
    stages_of,
    started,
    statuses,
)
from yizkr.claims import ClaimBatch, EntityRecord, ItemValue, TimeValue
from yizkr.pipeline import chunking, llm, scratch
from yizkr.pipeline.extract import run_metadata, salvage_valid_claims
from yizkr.pipeline.gedcom import parse_gedcom
from yizkr.pipeline.orchestrate import (
    BATCH_VOLATILE,
    DOCUMENT_STAGES,
    document_kind,
)
from yizkr.pipeline.presentation import slide_transcript, unpack_pptx
from yizkr.pipeline.rundir import DONE, RunDir
from yizkr.pipeline.scratch import BARRIER, EXIT_OK, CorpusStage
from yizkr.pipeline.stages import content_hash, sha1_file

SOURCES = {source.key: source for source in u.SOURCES}
# the documents the engine is asked about, and the images it reads
EXTRACTED = (fc.NARRATIVE, fc.DECK, fc.CERTIFICATE, fc.LETTER)
IMAGES = (fc.CERTIFICATE, fc.LETTER)
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
    registered corpus stages run, then the probe."""

    def __init__(self, tmp_path: Path, calls: Calls, refused) -> None:
        self.folder = fc.copy_to(tmp_path / "corpus")
        self.runs = tmp_path / "runs"
        self.calls = calls
        self.refused = refused
        self.probe = Probe()

    def stages(self) -> list[CorpusStage]:
        return [*scratch.CORPUS_STAGES, CorpusStage(PROBE, self.probe)]

    def driver(self, name: str) -> Driver:
        return Driver(
            self.folder, self.runs / name, corpus_stages=self.stages()
        )

    def run(self, name: str) -> Path:
        """Run the run `name`, a new one or, invoked again, the same, to the
        end; returns its run directory."""
        driver = self.driver(name)
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
        assert {doc.kind for doc in fc.DOCUMENTS.values()} == set(
            DOCUMENT_STAGES
        )
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
        universe records for the document's source, but for its recorded
        mistakes, which a sharper prompt mends; none of the source's
        assertions about an entity is left out; and each is a valid
        claim."""
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
            answered = {}
            for sharper in (False, True):
                claimed = answers.claims(recorded, sha1(name), sharper=sharper)
                batch, rejects = salvage_valid_claims(
                    claimed, run, entities=entities
                )
                assert rejects == [], name
                answered[sharper] = said_in(name, batch)
            stated = {
                (a.subject, a.property, a.value)
                for a in answers.said(recorded)
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
            for name in (fc.NARRATIVE, fc.DECK, fc.EXPORT, fc.CERTIFICATE)
        ]
        assert statuses(run) == {DONE}
        assert set(started(run)) == every_stage()
        (barrier,) = events(run, BARRIER)
        assert (barrier["documents"], barrier["failed"]) == (5, 0)
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
                assert coverage["rejected_claims"] == 0, name
                assert coverage["fallback"] == {}, name
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
def batches(runs) -> Batches:
    run = runs.run("first")
    return Batches({name: runs.batch(run, name) for name in fc.DOCUMENTS})


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
        # the critic flags the early one, on its claim
        findings = json.loads(
            runs.views(run)[fc.NARRATIVE].critique.read_text()
        )
        nicholas = answers.RECORDED[fc.NARRATIVE].ids()["nicholas-lobig"]
        assert [f["target"] for f in findings] == [
            f"claim: {nicholas} date_of_birth"
        ]
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
    """(document SHA-1, stage) of every call that reached the engine."""
    return {
        (e["subject"], e["stage"])
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
    def test_a_second_run_asks_the_engine_nothing(self, runs):
        """A new run starts from fresh state directories: every view is
        made again, and the LLM cache answers every call it makes."""
        first = runs.run("first")
        asked = len(runs.calls.asked)
        second = runs.run("second")
        assert len(runs.calls.asked) == asked
        assert events(second, "call") == []
        assert events(second, "cache-miss") == []
        hits = events(second, "cache-hit")
        assert {hit["reason"] for hit in hits} == {"key match"}
        assert {hit["subject"] for hit in hits} == {
            sha1(name) for name in EXTRACTED
        }
        assert events(second, "reused") == []
        assert set(started(second)) == every_stage()
        assert statuses(second) == {DONE}
        assert view_hashes(runs, second) == view_hashes(runs, first)
        assert corpus_events(second)[-2:] == [
            ("started", PROBE),
            ("done", PROBE),
        ]
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
        assert runs.refused.asked == []

    def test_a_sharper_chunk_prompt_recomputes_extraction_and_what_follows(
        self, runs, monkeypatch
    ):
        """A rule added to the chunk prompt changes every chunk call's key:
        the engine is asked again for every extraction, and, where the
        batch it makes differs (the family history, whose mistake the rule
        mends), for the critique that follows it. The transcriptions, the
        entity manifests and every other critique come from the cache."""
        first = runs.run("first")
        before = len(runs.calls.asked)
        monkeypatch.setattr(
            chunking,
            "CHUNK_PROMPT",
            chunking.CHUNK_PROMPT + answers.SHARPER_RULE + "\n",
        )
        sharper = runs.run("sharper")
        asked = runs.calls.asked[before:]
        assert {what for what, _name in asked} == {"chunk", "critique"}
        assert {name for what, name in asked if what == "chunk"} == set(
            EXTRACTED
        )
        assert [name for what, name in asked if what == "critique"] == [
            fc.NARRATIVE
        ]
        assert calls_by_stage(sharper) == {
            (sha1(name), "extraction") for name in EXTRACTED
        } | {(sha1(fc.NARRATIVE), "critique")}
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
        assert views[fc.NARRATIVE][2] == []
        assert views[fc.NARRATIVE][:2] != was[fc.NARRATIVE][:2]
        assert views[fc.NARRATIVE][0] == was[fc.NARRATIVE][0]
        for name in fc.DOCUMENTS:
            if name != fc.NARRATIVE:
                assert views[name] == was[name], name
        assert runs.refused.asked == []
