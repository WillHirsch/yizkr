"""Offline tests for a scratch run's write side (#200; ADR-0025 §1, §2):
the corpus stages after clustering, reset, register, write and site
(yizkr.pipeline.scratchwrite), run in order over a run's documents on the
unit tests' in-memory instance, with no stack and no engine.

* The driver registers them after clustering, in that order; each
  touches the wiki and makes no model call.
* The write takes every claim of each batch, with no approval file, at
  normal rank, with both references, through the run's one identity
  map; depicts claims are left out and counted. Each run item is marked
  an unratified scratch run's, naming the run, and records the hashes of
  the run manifest's settings and of the identity map.
* It refuses unless this run reset the instance and nothing else has
  written to it since; a batch the writer would refuse whole is refused
  before anything of its document is registered.
* A rerun whose write key is the recorded instance-contents key resets
  nothing and writes nothing, until a write the stage did not make, or
  another key, voids the record.
* The LAN site's header names the scratch run.
* A self-loop the writer refuses, as a false merge of Eugen Löbig the
  master hatter and his son of the same name makes one (#524), is kept
  in the write record by its cluster and property, and raised as the
  write's `self-reference` anomaly alert, ids and counts alone.

Every document is fictional (docs/EXAMPLES.md): Leopold Löbig's birth
register extract, a letter from his brother Gustav, a photograph of the
shop front at Brunnenring 35, and a page of the 2008 family history.
"""

from __future__ import annotations

import contextlib
import hashlib
import json

import pytest

from tests.unit.test_registration_flags import WritingWiki
from yizkr import reset as reset_module
from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.corpus import ExclusionList
from yizkr.core.instance import (
    read_record,
    recorded_contents,
    write_state_dir,
)
from yizkr.pipeline import report, scratch, scratchwrite
from yizkr.pipeline.orchestrate import DocumentRun
from yizkr.pipeline.rundir import PROGRESS
from yizkr.pipeline.scratch import CORPUS_DIR, CorpusContext, ScratchDriver
from yizkr.pipeline.scratchwrite import ScratchWriteRefused
from yizkr.pipeline.writer import REFUSAL_REASONS
from yizkr.staticsite import SiteBuilder, scratch_notice

HOST = "wikibase.example"
URL = f"http://{HOST}"
CONFIG = Config(
    wikibase_url=URL,
    concept_uri=URL,
    admin_name="admin",
    admin_pass="Hutmacherei-1863",
    wdqs_url=f"http://query.{HOST}",
    sparql_url=f"http://query.{HOST}/sparql",
    env_file=None,
)
RUN = {
    "model_identifier": "claude-sonnet-5",
    "pipeline_version": "0.1.0",
    "started_at": "2026-10-06T09:00:00Z",
}
EXTRACT = b"\x89PNG synthetic scan of Leopold's 1914 birth register extract"
LETTER = b"\x89PNG synthetic scan of Gustav's 1952 letter to Leopold"
SHOP = b"\x89PNG synthetic photograph of the shop front at Brunnenring 35"


# the steps the stand-in reset tells of, as the real one tells of its own
RESET_STEPS = (
    "stop the stack",
    "set LocalSettings.php aside",
    "start the stack",
)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def person(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "human",
    }


def claim(data: bytes, subject: dict, prop: str, value: dict) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1(data)},
            "page_number": "1",
        },
        "confidence": 0.9,
    }


def born(data: bytes, year: int, local_id: str = "leopold") -> dict:
    return claim(
        data,
        person(local_id, "Leopold Löbig"),
        "date_of_birth",
        {"type": "time", "year": year, "month": 5},
    )


def brothers(data: bytes) -> dict:
    return claim(
        data,
        person("gustav", "Gustav Löbig"),
        "sibling",
        {"type": "item", "value": person("leopold", "Leopold Löbig")},
    )


def depicts(data: bytes) -> dict:
    return claim(
        data,
        {"kind": "artifact", "sha1": sha1(data)},
        "depicts",
        {"type": "item", "value": person("leopold", "Leopold Löbig")},
    )


def batch(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "claims": list(claims)}
    )


class Wiki(WritingWiki):
    """The in-memory instance, with what the write side reads besides:
    the vocabulary of the claims these tests write and of a run item's
    marks, and the instance's newest revision and log entry (every item
    written and every file uploaded), as the instance-contents record
    reads them."""

    PROPERTIES = {
        **WritingWiki.PROPERTIES,
        "date_of_birth": "P70",
        "sibling": "P71",
        "depicts": "P72",
        "scratch_run": "P73",
        "run_manifest_hash": "P74",
        "identity_map_hash": "P75",
    }
    config = CONFIG

    def api_get(self, params):
        if params.get("list") == "allrevisions":
            revisions = [{"revid": self.writes}] if self.writes else []
            return {"query": {"allrevisions": [{"revisions": revisions}]}}
        if params.get("list") == "logevents":
            uploads = sum(len(v) for v in self.media.versions.values())
            entries = [{"logid": uploads}] if uploads else []
            return {"query": {"logevents": entries}}
        return super().api_get(params)

    def wiped(self) -> None:
        """Empty the instance, as a reset does."""
        fresh = Wiki()
        self.entities, self.media = fresh.entities, fresh.media
        self.writes = 0


class Run:
    """A scratch run's documents in a run directory, past the barrier,
    with the write side's stand-ins: the instance `wiki`; in place of
    `yizkr.reset.reset_instance`, a reset that empties it and clears what
    the real reset clears, under the lock it is given or, given none, a
    hold of its own, as the real one takes; the live-stack lock, which
    with `intruder` set lets another session write to the instance each
    time its last hold is released; the query service, which shows what
    the instance holds unless `shown` or `newest` says otherwise; and the
    site's build, each recording its use."""

    def __init__(self, tmp_path, monkeypatch, wiki=None, run_id="run-1"):
        self.tmp_path = tmp_path
        self.wiki = wiki or Wiki()
        self.resets = 0
        self.locks = 0
        self.held = 0
        self.intruder = False
        self.newest: int | None = None
        self.now = 0.0
        self.sites: list = []
        self.lines: list[str] = []
        # the scratch runs the query service shows; by default those the
        # instance's run items name, with no lag
        self.shown: list[str] | None = None
        self.folder = tmp_path / "corpus"
        self.folder.mkdir(exist_ok=True)
        self.run_dir = tmp_path / "runs" / run_id
        self.driver = ScratchDriver(
            self.folder,
            self.run_dir,
            run_id=run_id,
            cache_dir=tmp_path / "cache",
            exclusions=ExclusionList(),
            log=lambda *args, **kwargs: self.lines.append(
                " ".join(map(str, args))
            ),
        )
        self.driver._client = self.wiki
        self.driver._config = CONFIG
        self.documents: list[DocumentRun] = []
        monkeypatch.setattr(reset_module, "reset_instance", self._reset)
        monkeypatch.setattr(scratchwrite, "live_stack", self._lock)
        monkeypatch.setattr(scratchwrite, "client_of", lambda _: self.wiki)
        monkeypatch.setattr(scratchwrite, "_forget_client", lambda _: None)
        monkeypatch.setattr(scratchwrite, "build_site", self._site)
        monkeypatch.setattr(
            scratchwrite, "query_service_probe", lambda config: self._probe
        )
        monkeypatch.setattr(scratchwrite, "sleep", self._sleep)
        monkeypatch.setattr(scratchwrite, "clock", lambda: self.now)

    def _reset(self, *, env_file, yes, log, lock=None, progress=None):
        from yizkr.core.instance import clear_contents

        assert yes
        with (lock or self._lock)():
            self.resets += 1
            # the real reset's steps, told as each starts
            for number, name in enumerate(RESET_STEPS, 1):
                if progress is not None:
                    progress(number, len(RESET_STEPS), name)
            clear_contents(CONFIG, "yizkr scratch reset")
            state = write_state_dir(CONFIG)
            if state.exists():
                for path in state.iterdir():
                    path.unlink()
            self.wiki.wiped()

    @contextlib.contextmanager
    def _lock(self):
        self.locks += 1
        self.held += 1
        try:
            yield
        finally:
            self.held -= 1
            if not self.held and self.intruder:
                self.wiki.item("Brunnenring 35")

    def _probe(self) -> tuple[list[str], int]:
        return self._shown(None), (
            self.wiki.writes if self.newest is None else self.newest
        )

    def _site(self, config, out):
        self.sites.append(out)
        return {"people": 2, "artifacts": 3}

    def _shown(self, config) -> list[str]:
        if self.shown is not None:
            return list(self.shown)
        return sorted(
            {
                value
                for qid in self.wiki.entities
                for value in string_values(self.wiki, qid, "scratch_run")
            }
        )

    def advance(self, seconds):
        """A wait, on the stand-in clock."""
        self.now += seconds

    def _sleep(self, seconds):
        raise AssertionError("the site stage waited for the query service")

    def add(self, name: str, data: bytes, claims: ClaimBatch) -> DocumentRun:
        path = self.folder / name
        path.write_bytes(data)
        doc = DocumentRun(path, self.run_dir)
        doc.dir.mkdir(parents=True, exist_ok=True)
        doc.batch.write_text(claims.model_dump_json(indent=2))
        self.documents.append(doc)
        return doc

    def context(self, stage: str) -> CorpusContext:
        return CorpusContext(
            run=self.driver.rundir,
            documents=tuple(self.documents),
            directory=self.run_dir / CORPUS_DIR / stage,
            env_file=None,
            log=self.driver.log,
            driver=self.driver,
        )

    def stage(self, name: str):
        if name == "clustering":
            from yizkr.pipeline.clustering import run_stage

            return run_stage(self.context(name))
        return getattr(scratchwrite, f"run_{name}")(self.context(name))

    def past_the_barrier(self, until: str = "site") -> None:
        """The corpus stages in order, as the driver runs them."""
        for name in ("clustering", "reset", "register", "write", "site"):
            self.stage(name)
            if name == until:
                return

    def record(self, stage: str) -> dict:
        return scratchwrite.read_record(self.run_dir / CORPUS_DIR / stage)


@pytest.fixture
def run(tmp_path, monkeypatch):
    made = Run(tmp_path, monkeypatch)
    made.add("leopold-birth-extract.png", EXTRACT, batch(born(EXTRACT, 1914)))
    made.add(
        "gustav-letter-1952.png",
        LETTER,
        batch(brothers(LETTER), born(LETTER, 1913), depicts(LETTER)),
    )
    return made


# -- what the instance holds --------------------------------------------------


def statements(wiki, qid: str, key: str) -> list[dict]:
    return wiki.entities[qid]["claims"].get(Wiki.PROPERTIES[key], [])


def cited(statement: dict, key: str) -> list[str]:
    return [
        snak["datavalue"]["value"]
        if isinstance(snak["datavalue"]["value"], str)
        else snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(Wiki.PROPERTIES[key], [])
    ]


def string_values(wiki, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in statements(wiki, qid, key)
    ]


def run_items(wiki) -> list[str]:
    kind = Wiki.ITEMS["extraction_run"]
    return [
        qid
        for qid in wiki.entities
        if any(
            statement["mainsnak"]["datavalue"]["value"]["id"] == kind
            for statement in statements(wiki, qid, "instance_of")
        )
    ]


def by_label(wiki, label: str) -> list[str]:
    return [
        qid
        for qid, entity in wiki.entities.items()
        if entity.get("labels", {}).get("en", {}).get("value") == label
    ]


# -- the stages ---------------------------------------------------------------


@pytest.mark.write_side
class TestTheStages:
    def test_the_write_side_follows_clustering_in_order(self):
        assert [stage.name for stage in scratch.CORPUS_STAGES] == [
            "clustering",
            "reset",
            "register",
            "write",
            "places",
            "site",
        ]
        for stage in scratch.CORPUS_STAGES[1:]:
            assert stage.touches_wiki
            # the place stage alone calls the engine (#201)
            assert stage.calls_engine == (stage.name == "places")

    def test_the_driver_reads_the_kept_state_marker(self):
        assert scratch.KEPT_STATE is scratch._instance_is_kept

    def test_the_unit_tests_leave_the_write_side_out_unless_asked(self):
        assert "reset" in [stage.name for stage in scratch.CORPUS_STAGES]


def test_the_unit_tests_leave_the_write_side_out():
    assert [stage.name for stage in scratch.CORPUS_STAGES] == ["clustering"]


# -- the write ----------------------------------------------------------------


class TestTheWrite:
    def test_every_claim_is_written_with_both_references(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        assert run.resets == 1
        # the two sources give Leopold two birth years: the map keeps his
        # two mentions apart, and each is an item
        leopolds = by_label(wiki, "Leopold Löbig")
        (gustav,) = by_label(wiki, "Gustav Löbig")
        extract, letter = (
            wiki.string_statement_index(Wiki.PROPERTIES["media_sha1"])[
                sha1(data)
            ]
            for data in (EXTRACT, LETTER)
        )
        births = [
            statement
            for leopold in leopolds
            for statement in statements(wiki, leopold, "date_of_birth")
        ]
        assert len(births) == 2
        runs = set(run_items(wiki))
        assert len(runs) == 2
        for statement in births + statements(wiki, gustav, "sibling"):
            # normal rank, as every scratch write is (rule 4)
            assert statement.get("rank", "normal") == "normal"
            (artifact,) = cited(statement, "stated_in")
            assert artifact in {extract, letter}
            (run_item,) = cited(statement, "inferred_from_extraction_run")
            assert run_item in runs

    def test_depicts_claims_are_left_out_and_counted(self, run):
        run.past_the_barrier()
        assert not any(
            statements(run.wiki, qid, "depicts") for qid in run.wiki.entities
        )
        record = run.record("write")
        assert record["depicts_left_out"] == 1
        letter = run.documents[1]
        assert record["documents"][letter.sha1]["depicts_left_out"] == 1
        # the run report counts them
        content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
        assert content["depicts"] == {"left_out": 1}
        assert content["write"]["documents_written"] == 2

    def test_each_run_item_is_marked_an_unratified_scratch_run(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        identity = scratchwrite.identity_map(run.context("write"))
        settings = scratchwrite.manifest_hash(run.driver.rundir)
        for qid in run_items(wiki):
            assert string_values(wiki, qid, "scratch_run") == ["run-1"]
            assert string_values(wiki, qid, "run_manifest_hash") == [settings]
            assert string_values(wiki, qid, "identity_map_hash") == [
                identity.hash
            ]
            for key in ("scratch_run", "run_manifest_hash", "instance_of"):
                for statement in statements(wiki, qid, key):
                    # each stated in the run item itself
                    assert cited(statement, "stated_in") == [qid]

    def test_the_write_records_its_key(self, run):
        run.past_the_barrier()
        recorded = recorded_contents(run.wiki)
        assert recorded.key == run.record("reset")["key"]
        assert recorded.run_id == "run-1"
        assert run.record("write")["recorded"] is True

    def test_writing_makes_no_model_call(self, run):
        run.past_the_barrier()
        assert run.driver._engines == {}

    def test_it_writes_under_the_live_stack_lock(self, run):
        run.past_the_barrier()
        # the reset stage holds it across the reset and its state, and
        # register and write each hold it; the reset takes no hold of its
        # own under it
        assert run.locks == 3

    def test_registration_names_the_scratch_run(self, run):
        run.past_the_barrier()
        (extract,) = by_label(run.wiki, "leopold-birth-extract")
        description = run.wiki.entities[extract]["descriptions"]["en"]
        assert "yizkr scratch run" in description["value"]


class CaptionWiki(Wiki):
    """The in-memory instance, with a container's caption in its
    vocabulary."""

    PROPERTIES = {**Wiki.PROPERTIES, "caption": "P76"}


BOOKLET = b"%PDF synthetic Our Family from Germany"
TOP_OF_LETTER = b"\x89PNG synthetic top of Sebastian's 1939 letter"


def captioned(data: bytes, image: bytes) -> dict:
    return claim(
        data,
        {"kind": "artifact", "sha1": sha1(image)},
        "caption",
        {
            "type": "monolingual",
            "value": {"text": "Wastl to Klara, July 1939.", "language": "en"},
        },
    )


def test_a_caption_lands_on_the_image_its_container_registered(
    tmp_path, monkeypatch
):
    """The register stage records the derived media it registered for
    each container, and the write gives them to the writer: the booklet's
    caption over the letter's image is written on the image's item, and
    one naming an image the booklet does not place is refused (#456,
    ADR-0039 §2.1)."""
    made = Run(tmp_path, monkeypatch, wiki=CaptionWiki())
    booklet = made.add(
        "family-booklet-2011.png",
        BOOKLET,
        batch(captioned(BOOKLET, TOP_OF_LETTER), captioned(BOOKLET, SHOP)),
    )
    real = made.driver._step_register

    def registering(doc):
        qid = real(doc)
        image = made.wiki.item(
            "Our Family from Germany — page 6, image 1",
            ("media_sha1", sha1(TOP_OF_LETTER), None),
        )
        made.driver.registered_media()[doc.sha1] = {sha1(TOP_OF_LETTER): image}
        return qid

    monkeypatch.setattr(made.driver, "_step_register", registering)
    made.past_the_barrier(until="write")
    placed = made.record("register")["placed"][booklet.sha1]
    image = placed[sha1(TOP_OF_LETTER)]
    (statement,) = made.wiki.entities[image]["claims"].get(
        CaptionWiki.PROPERTIES["caption"], []
    )
    assert statement["mainsnak"]["datavalue"]["value"] == {
        "text": "Wastl to Klara, July 1939.",
        "language": "en",
    }
    assert cited(statement, "stated_in") == [
        made.record("register")["documents"][booklet.sha1]
    ]
    written = made.record("write")["documents"][booklet.sha1]
    assert (written["written"], written["refused"]) == (1, 1)


# -- the refusals -------------------------------------------------------------


class TestRefusals:
    def test_a_write_without_this_run_s_reset_is_refused(self, run):
        run.past_the_barrier(until="clustering")
        # the record says this run reset the instance, the state that
        # another run did
        run.stage("reset")
        state = scratchwrite.read_state(CONFIG)
        state["run_id"] = "run-0"
        scratchwrite.state_path(CONFIG).write_text(json.dumps(state))
        before = run.wiki.writes
        with pytest.raises(ScratchWriteRefused, match="run-0 reset it"):
            run.stage("register")
        with pytest.raises(ScratchWriteRefused, match="did not reset"):
            run.stage("write")
        assert run.wiki.writes == before

    def test_a_write_to_the_instance_since_the_reset_is_refused(self, run):
        run.past_the_barrier(until="reset")
        # a live test, say, writes to the instance
        run.wiki.item("Brunnenring 35")
        before = run.wiki.writes
        with pytest.raises(ScratchWriteRefused, match="written to since"):
            run.stage("register")
        assert run.wiki.writes == before

    def test_a_reset_the_run_s_record_does_not_name_is_refused(self, run):
        run.past_the_barrier(until="reset")
        # the instance's state names another reset of the instance than
        # the one the run's reset stage recorded
        directory = run.run_dir / CORPUS_DIR / "reset"
        record = scratchwrite.read_record(directory)
        scratchwrite.save_record(directory, {**record, "epoch": "0" * 32})
        with pytest.raises(ScratchWriteRefused, match="reset since"):
            run.stage("register")
        assert run.wiki.writes == 0

    def test_a_write_with_no_reset_stage_is_refused(self, run):
        run.past_the_barrier(until="clustering")
        with pytest.raises(ScratchWriteRefused, match="reset stage"):
            run.stage("register")
        assert run.wiki.writes == 0

    def test_a_batch_the_writer_would_refuse_registers_nothing(
        self, monkeypatch, run
    ):
        from tests.fixtures.identity import mapped

        # the identity map holds none of the shop front's mentions
        identity = mapped(
            *(scratchwrite.extracted_batch(doc) for doc in run.documents)
        )
        monkeypatch.setattr(scratchwrite, "identity_map", lambda _: identity)
        run.add(
            "brunnenring-35.png",
            SHOP,
            batch(born(SHOP, 1914, local_id="leopold-shop")),
        )
        run.stage("reset")
        run.stage("register")
        run.stage("write")
        record = run.record("register")
        assert record["refused"] == {sha1(SHOP): "UnmappedMention"}
        index = run.wiki.string_statement_index(Wiki.PROPERTIES["media_sha1"])
        assert sha1(SHOP) not in index
        assert sha1(EXTRACT) in index
        assert run.record("write")["refused"] == {
            sha1(SHOP): "UnmappedMention"
        }


# -- reuse --------------------------------------------------------------------


class TestReuse:
    def test_a_rerun_with_the_recorded_key_resets_and_writes_nothing(
        self, run, tmp_path, monkeypatch
    ):
        run.past_the_barrier()
        writes = run.wiki.writes
        again = Run(tmp_path, monkeypatch, wiki=run.wiki, run_id="run-2")
        again.documents = run.documents
        again.shown = ["run-1"]
        again.past_the_barrier()
        assert again.resets == 0
        assert run.wiki.writes == writes
        assert again.record("reset")["action"] == scratchwrite.SKIPPED
        assert again.record("write")["skipped"] is True
        # the site names the run whose writes the instance holds
        assert again.record("site")["run_id"] == "run-1"

    def test_the_run_that_wrote_keeps_its_counts_when_invoked_again(self, run):
        run.past_the_barrier()
        counts = run.record("write")
        writes = run.wiki.writes
        run.past_the_barrier()
        assert run.resets == 1
        assert run.wiki.writes == writes
        assert run.record("write") == counts
        assert run.record("reset")["action"] == scratchwrite.SKIPPED

    def test_a_write_the_stage_did_not_make_voids_the_key(
        self, run, tmp_path, monkeypatch
    ):
        run.past_the_barrier()
        run.wiki.item("Brunnenring 35")
        again = Run(tmp_path, monkeypatch, wiki=run.wiki, run_id="run-2")
        again.documents = run.documents
        again.past_the_barrier()
        assert again.resets == 1
        assert by_label(run.wiki, "Brunnenring 35") == []
        assert recorded_contents(run.wiki).run_id == "run-2"

    def test_another_batch_is_another_key(self, run, tmp_path, monkeypatch):
        run.past_the_barrier()
        key = run.record("reset")["key"]
        letter = run.documents[1]
        letter.batch.write_text(
            batch(brothers(LETTER), born(LETTER, 1914)).model_dump_json()
        )
        again = Run(tmp_path, monkeypatch, wiki=run.wiki, run_id="run-2")
        again.documents = run.documents
        again.past_the_barrier()
        assert again.resets == 1
        assert again.record("reset")["key"] != key

    def test_a_cleared_record_resets_first(self, run, tmp_path, monkeypatch):
        from yizkr.core.instance import clear_contents

        run.past_the_barrier()
        clear_contents(CONFIG, "yizkr live tests")
        again = Run(tmp_path, monkeypatch, wiki=run.wiki, run_id="run-2")
        again.documents = run.documents
        again.past_the_barrier()
        assert again.resets == 1

    def test_an_invocation_cut_short_goes_on_where_it_stopped(
        self, run, monkeypatch
    ):
        from yizkr.pipeline import writer

        real = writer.BatchWriter.write_batch
        calls = []

        def once(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(self, *args, **kwargs)

        monkeypatch.setattr(writer.BatchWriter, "write_batch", once)
        run.past_the_barrier(until="register")
        with pytest.raises(KeyboardInterrupt):
            run.stage("write")
        assert len(run_items(run.wiki)) == 1
        monkeypatch.setattr(writer.BatchWriter, "write_batch", real)
        # the next invocation: no reset, the first document not again
        run.past_the_barrier()
        assert run.resets == 1
        assert run.record("reset")["action"] == scratchwrite.RESUMED
        assert len(run_items(run.wiki)) == 2
        assert recorded_contents(run.wiki).run_id == "run-1"

    def test_a_write_cut_short_within_a_document_resets_again(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        # a revision the write-phase state does not record, as a write
        # cut short within a document leaves one
        state = scratchwrite.read_state(CONFIG)
        state["watermark"] = {"revision": 1, "log": 0}
        scratchwrite.state_path(CONFIG).write_text(json.dumps(state))
        from yizkr.core.instance import clear_contents

        clear_contents(CONFIG, "test")
        run.past_the_barrier()
        assert run.resets == 2
        assert run.record("reset")["action"] == scratchwrite.RESET_DONE


# -- the site -----------------------------------------------------------------


class TestTheSite:
    def test_the_site_waits_for_the_run_s_items(self, run, monkeypatch):
        run.past_the_barrier(until="write")
        run.shown = []
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            run.shown = ["run-1"]

        monkeypatch.setattr(scratchwrite, "sleep", sleep)
        run.stage("site")
        assert waits == [scratchwrite.SITE_POLL]
        assert run.sites == [run.run_dir / CORPUS_DIR / "site" / "site"]
        assert run.record("site")["run_id"] == "run-1"

    def test_the_site_records_its_disagreement_counts(self, run, monkeypatch):
        """The site's record keeps the disagreements its build counted on
        the written graph, as counts alone (#575), and the run report
        reads them from it."""
        from yizkr.pipeline import report

        def site(config, out):
            run.sites.append(out)
            return {
                "people": 2,
                "artifacts": 3,
                "inconsistencies": 3,
                "inconsistencies_by_property": {
                    "date_of_birth": 1,
                    "place_of_birth": 2,
                },
            }

        monkeypatch.setattr(scratchwrite, "build_site", site)
        run.past_the_barrier()
        counted = {
            "total": 3,
            "by_property": {"date_of_birth": 1, "place_of_birth": 2},
        }
        assert run.record("site")["disagreements"] == counted
        vocabulary = report._Vocabulary.of({})
        sited = report._site_stage(run.run_dir)
        assert report._aggregate_contradictions(sited, vocabulary) == {
            "different_source": 3,
            "by_property": {"place_of_birth": 2, "date_of_birth": 1},
        }

    def test_a_build_that_does_not_count_them_records_none(self, run):
        """A build whose summary has no counts leaves them out of the
        record, rather than record 0."""
        run.past_the_barrier()
        assert "disagreements" not in run.record("site")

    def test_a_query_service_that_never_shows_the_run_fails_it(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        run.shown = ["run-0"]

        def sleep(seconds):
            run.advance(seconds)
            # a wait that never gives up fails here, not by hanging
            assert run.now <= scratchwrite.SITE_WAIT + scratchwrite.SITE_POLL

        monkeypatch.setattr(scratchwrite, "sleep", sleep)
        with pytest.raises(ScratchWriteRefused, match="not caught up"):
            run.stage("site")
        assert run.sites == []
        # it gave up at the deadline, on the clock it is measured on
        assert run.now == scratchwrite.SITE_WAIT

    def test_the_site_makes_its_probe_once(self, run, monkeypatch):
        """However often the site stage polls the query service, it polls
        with one probe, made once."""
        run.past_the_barrier(until="write")
        run.shown = []
        made = []

        def probe(config):
            made.append(config)
            return run._probe

        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                run.shown = ["run-1"]

        monkeypatch.setattr(scratchwrite, "query_service_probe", probe)
        monkeypatch.setattr(scratchwrite, "sleep", sleep)
        run.stage("site")
        assert len(polls) == 2
        assert made == [CONFIG]

    def test_the_site_waits_for_the_write_s_newest_revision(
        self, run, monkeypatch
    ):
        """The query service shows the run's first run item before it has
        taken in the rest of the write: the site waits for the newest
        revision the write left."""
        run.past_the_barrier(until="write")
        revision = run.record("write")["revision"]
        assert revision == run.wiki.writes
        run.newest = revision - 1
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            run.newest = revision

        monkeypatch.setattr(scratchwrite, "sleep", sleep)
        run.stage("site")
        assert waits == [scratchwrite.SITE_POLL]
        assert len(run.sites) == 1

    def test_the_probe_reads_the_query_service_afresh(self, monkeypatch):
        """One probe, its clients made once, reads the query service anew
        at each call."""
        from yizkr import staticsite

        answers = iter(
            [
                [{"run": {"value": "run-1"}}],
                [{"version": {"value": "41"}}],
                [{"run": {"value": "run-1"}}, {"run": {"value": "run-2"}}],
                [{"version": {"value": "42"}}],
            ]
        )
        made = []

        class Client:
            def __init__(self, config):
                made.append(config)

            def vocabulary(self):
                return site_vocabulary()

        monkeypatch.setattr(staticsite, "WikiClient", Client)
        monkeypatch.setattr(
            staticsite.sparql, "runner", lambda config: lambda q: next(answers)
        )
        probe = staticsite.query_service_probe(CONFIG)
        assert probe() == (["run-1"], 41)
        assert probe() == (["run-1", "run-2"], 42)
        assert made == [CONFIG]

    def test_every_page_names_the_scratch_run(self, tmp_path):
        properties, items = site_vocabulary()

        def runner(query: str) -> list[dict]:
            if "SELECT DISTINCT ?run" in query:
                return [{"run": {"value": "20261006-090000"}}]
            return []

        out = tmp_path / "site"
        summary = SiteBuilder(
            CONFIG, out, run=runner, vocabulary=(properties, items)
        ).build()
        assert summary["scratch_runs"] == ["20261006-090000"]
        pages = sorted(out.glob("*.html"))
        assert pages
        for page in pages:
            text = page.read_text()
            assert "development-stage scratch run" in text
            assert "<code>20261006-090000</code>" in text

    def test_an_instance_with_no_scratch_run_has_no_notice(self, tmp_path):
        out = tmp_path / "site"
        SiteBuilder(
            CONFIG,
            out,
            run=lambda query: [],
            vocabulary=site_vocabulary(),
        ).build()
        assert "scratch run" not in (out / "index.html").read_text()
        assert scratch_notice([]) == ""


def site_vocabulary():
    """Every manifest key with an id, as tests/unit/test_staticsite.py
    gives them."""
    import yaml

    from yizkr.model.schema import read_manifest_data

    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


# -- the run item's marks -----------------------------------------------------


class TestTheMarks:
    def test_a_run_item_found_without_its_marks_gains_them(self):
        from yizkr.pipeline.registrar import ensure_self_typed

        wiki = Wiki()
        qid = wiki.item(
            "extraction run 2026-10-06",
            ("instance_of", Wiki.ITEMS["extraction_run"], None),
        )
        marks = {"scratch_run": "run-1", "identity_map_hash": "0" * 64}
        assert ensure_self_typed(
            wiki, qid, "extraction_run", summary="t", marks=marks
        )
        assert string_values(wiki, qid, "scratch_run") == ["run-1"]
        writes = wiki.writes
        assert not ensure_self_typed(
            wiki, qid, "extraction_run", summary="t", marks=marks
        )
        assert wiki.writes == writes


def _vocabulary():
    return report._Vocabulary.of({})


def test_the_report_says_when_no_write_ran():
    content = report._aggregate_content({}, {}, {}, _vocabulary(), None)
    assert content["depicts"]["status"] == report.NOT_AVAILABLE
    assert content["write"]["status"] == report.NOT_AVAILABLE
    assert report._write_sections(content) == []


def test_the_report_counts_a_skipped_write():
    content = report._aggregate_content(
        {}, {}, {}, _vocabulary(), {"skipped": True, "depicts_left_out": 3}
    )
    assert content["write"] == {"skipped": True}
    (section,) = report._write_sections(content)
    assert "Depicts claims left out, never written: 3." in section.blocks[0]


def test_the_record_of_the_instance_is_not_touched(run):
    run.past_the_barrier()
    record = read_record(CONFIG)
    assert record["run_id"] == "run-1"


# -- the guard's edges --------------------------------------------------------


class TestTheGuard:
    def test_a_write_as_the_reset_s_lock_is_released_is_refused(self, run):
        """Another session writes to the instance the moment the live-stack
        lock is free: the reset and the state it records are under one
        hold, so that write comes after the state, and register sees it."""
        run.past_the_barrier(until="clustering")
        run.intruder = True
        run.stage("reset")
        assert by_label(run.wiki, "Brunnenring 35")
        run.intruder = False
        with pytest.raises(ScratchWriteRefused, match="written to since"):
            run.stage("register")

    def test_a_write_stage_after_another_reset_s_register_is_refused(
        self, run
    ):
        run.past_the_barrier(until="register")
        directory = run.run_dir / CORPUS_DIR / "register"
        record = scratchwrite.read_record(directory)
        scratchwrite.save_record(directory, {**record, "epoch": "0" * 32})
        writes = run.wiki.writes
        with pytest.raises(ScratchWriteRefused, match="register stage"):
            run.stage("write")
        assert run.wiki.writes == writes

    def test_a_reset_record_of_another_epoch_resets_again(
        self, run, monkeypatch
    ):
        """An interrupted write goes on where it stopped only when the
        run's reset record names the reset the instance's state records."""
        from yizkr.pipeline import writer

        real = writer.BatchWriter.write_batch

        def interrupted(self, *args, **kwargs):
            raise KeyboardInterrupt

        run.past_the_barrier(until="register")
        monkeypatch.setattr(writer.BatchWriter, "write_batch", interrupted)
        with pytest.raises(KeyboardInterrupt):
            run.stage("write")
        monkeypatch.setattr(writer.BatchWriter, "write_batch", real)
        directory = run.run_dir / CORPUS_DIR / "reset"
        record = scratchwrite.read_record(directory)
        scratchwrite.save_record(directory, {**record, "epoch": "0" * 32})
        run.past_the_barrier()
        assert run.resets == 2
        assert run.record("reset")["action"] == scratchwrite.RESET_DONE

    def test_a_reset_clears_the_records_of_the_one_before(
        self, run, tmp_path, monkeypatch
    ):
        run.past_the_barrier()
        assert run.record("write")["recorded"]
        run.wiki.item("Brunnenring 35")
        run.past_the_barrier(until="reset")
        # what register, write and site recorded of the earlier reset is
        # gone, so no report shows it as this reset's
        for stage in ("register", "write", "site"):
            assert run.record(stage) == {}

    def test_a_document_whose_write_fails_is_retried_and_then_recorded(
        self, run, monkeypatch
    ):
        from yizkr.pipeline import writer

        real = writer.BatchWriter.write_batch
        letter = run.documents[1]

        def failing(self, batch, *args, **kwargs):
            if batch.claims[0].source.artifact.sha1 == letter.sha1:
                raise ConnectionError("the wiki went away")
            return real(self, batch, *args, **kwargs)

        monkeypatch.setattr(writer.BatchWriter, "write_batch", failing)
        run.past_the_barrier(until="write")
        record = run.record("write")
        assert record["failed"] == {letter.sha1: "ConnectionError"}
        assert "recorded" not in record
        assert recorded_contents(run.wiki).key is None
        monkeypatch.setattr(writer.BatchWriter, "write_batch", real)
        run.past_the_barrier()
        assert run.resets == 1
        assert run.record("reset")["action"] == scratchwrite.RESUMED
        record = run.record("write")
        assert record["failed"] == {}
        assert set(record["documents"]) == {d.sha1 for d in run.documents}
        assert recorded_contents(run.wiki).key == run.record("reset")["key"]

    def test_a_document_whose_registration_fails_keeps_the_key_unrecorded(
        self, run, monkeypatch
    ):
        """A document registration failed is not among those the write
        takes, yet the instance does not hold what the key makes until it
        is registered and written."""
        real = run.driver._step_register
        extract, letter = run.documents

        def failing(doc):
            if doc.sha1 == letter.sha1:
                raise ConnectionError("the wiki went away")
            return real(doc)

        monkeypatch.setattr(run.driver, "_step_register", failing)
        run.past_the_barrier(until="write")
        assert run.record("register")["failed"] == {
            letter.sha1: "ConnectionError"
        }
        record = run.record("write")
        assert set(record["documents"]) == {extract.sha1}
        assert not record.get("recorded")
        assert recorded_contents(run.wiki).key is None
        monkeypatch.setattr(run.driver, "_step_register", real)
        run.past_the_barrier()
        assert run.resets == 1
        assert run.record("register")["failed"] == {}
        assert run.record("write")["recorded"] is True
        assert recorded_contents(run.wiki).key == run.record("reset")["key"]

    def test_a_stage_record_of_another_reset_is_not_continued(self, run):
        run.past_the_barrier(until="register")
        context = run.context("register")
        record = scratchwrite.read_record(context.directory)
        assert record["documents"]
        key, epoch = record["key"], record["epoch"]
        assert scratchwrite._ongoing(context, key, epoch) == record
        other = scratchwrite._ongoing(context, key, "0" * 32)
        assert other["epoch"] == "0" * 32
        assert other["documents"] == {}


# -- the write key ------------------------------------------------------------


def _key(run, identity=None, exclusions=None) -> str:
    identity = identity or scratchwrite.identity_map(run.context("write"))
    return scratchwrite.write_key(identity, run.documents, exclusions)


class TestTheKey:
    @pytest.fixture
    def keyed(self, run):
        run.past_the_barrier(until="clustering")
        return run, _key(run, exclusions=ExclusionList())

    def test_the_same_inputs_give_the_same_key(self, keyed):
        run, key = keyed
        assert _key(run, exclusions=ExclusionList()) == key

    def test_another_map_is_another_key(self, keyed):
        from tests.fixtures.identity import joined

        run, key = keyed
        identity = scratchwrite.identity_map(run.context("write"))
        other = joined(identity, "Leopold Löbig")
        assert other.hash != identity.hash
        assert _key(run, other, ExclusionList()) != key

    def test_another_title_is_another_key(self, keyed):
        run, key = keyed
        run.documents[0].state.title = "Geburtsregister Flörburg 1914"
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_file_name_is_another_key(self, keyed, monkeypatch):
        run, key = keyed
        doc = run.documents[0]
        moved = doc.source.with_name("leopold-1914.png")
        doc.source.rename(moved)
        monkeypatch.setattr(doc, "source", moved)
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_never_ingest_list_is_another_key(self, keyed):
        run, key = keyed
        listed = ExclusionList(sha1s=frozenset({sha1(SHOP)}))
        assert _key(run, exclusions=listed) != key

    def test_another_manifest_is_another_key(
        self, keyed, tmp_path, monkeypatch
    ):
        run, key = keyed
        changed = tmp_path / "manifest.yaml"
        changed.write_bytes(scratchwrite.MANIFEST.read_bytes() + b"\n# x\n")
        monkeypatch.setattr(scratchwrite, "MANIFEST", changed)
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_writer_version_is_another_key(self, keyed, monkeypatch):
        run, key = keyed
        monkeypatch.setattr(
            scratchwrite, "WRITER_VERSION", scratchwrite.WRITER_VERSION + 1
        )
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_package_source_is_another_key(self, keyed, monkeypatch):
        run, key = keyed
        monkeypatch.setattr(scratchwrite, "code_digest", lambda: "0" * 64)
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_kind_is_another_key(self, keyed, monkeypatch):
        from yizkr.pipeline import orchestrate

        run, key = keyed
        # the same file, read as another kind of document
        monkeypatch.setattr(
            orchestrate, "document_kind", lambda source: "presentation"
        )
        assert _key(run, exclusions=ExclusionList()) != key

    def test_another_record_of_derived_media_is_another_key(self, keyed):
        from yizkr.pipeline.presentation import DERIVED_MEDIA

        run, key = keyed
        (run.documents[0].dir / DERIVED_MEDIA).write_text('{"media": []}\n')
        assert _key(run, exclusions=ExclusionList()) != key

    @pytest.mark.parametrize(
        "module",
        [
            "claims.py",
            "core/wiki.py",
            "core/dates.py",
            "model/schema.py",
            "model/manifest.yaml",
            "pipeline/writer.py",
            "pipeline/registrar.py",
            "pipeline/review.py",
            "pipeline/clustering.py",
            "pipeline/presentation.py",
            "pipeline/orchestrate.py",
            "queries/kinship.rq",
        ],
    )
    def test_a_change_to_what_writes_is_another_digest(self, tmp_path, module):
        import shutil

        package = tmp_path / "yizkr"
        shutil.copytree(
            scratchwrite.PACKAGE,
            package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        before = scratchwrite.package_digest(package)
        with open(package / module, "a") as handle:
            handle.write("\n# a change\n")
        assert scratchwrite.package_digest(package) != before

    @pytest.mark.parametrize("module", sorted(scratchwrite.READ_ONLY))
    def test_a_change_to_a_reader_is_the_same_digest(self, tmp_path, module):
        import shutil

        package = tmp_path / "yizkr"
        shutil.copytree(
            scratchwrite.PACKAGE,
            package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        before = scratchwrite.package_digest(package)
        with open(package / module, "a") as handle:
            handle.write("\n# a change\n")
        assert scratchwrite.package_digest(package) == before


# -- the driver ---------------------------------------------------------------


@pytest.mark.write_side
def test_the_driver_gives_each_corpus_stage_itself(tmp_path):
    seen = []

    def probe(context):
        seen.append(context.driver)
        return []

    driver = ScratchDriver(
        tmp_path / "corpus",
        tmp_path / "runs" / "run-1",
        cache_dir=tmp_path / "cache",
        exclusions=ExclusionList(),
        corpus_stages=[scratch.CorpusStage("probe", probe)],
        log=lambda *args, **kwargs: None,
    )
    assert driver._corpus([]) == (None, None)
    assert seen == [driver]


def test_items_found_again_are_not_counted_as_minted(run):
    from yizkr.pipeline.writer import BatchWriter

    run.past_the_barrier()
    record = run.record("write")
    # Leopold twice (his two birth years keep his mentions apart), Gustav
    assert sum(d["items_minted"] for d in record["documents"].values()) == 3
    identity = scratchwrite.identity_map(run.context("write"))
    letter = run.documents[1]
    extracted = scratchwrite.extracted_batch(letter)
    again = BatchWriter(run.wiki).write_batch(
        scratchwrite.batch_to_write(extracted)[0],
        identity=identity,
        mapped_from=extracted,
    )
    assert again.created_items
    assert again.minted == []


def test_the_record_counts_only_the_items_a_document_s_write_made(
    run, monkeypatch
):
    from tests.fixtures.identity import joined

    run.past_the_barrier(until="clustering")
    # Leopold's two mentions in one cluster: the extract's write makes his
    # item, and the letter's finds it again
    identity = joined(
        scratchwrite.identity_map(run.context("write")), "Leopold Löbig"
    )
    monkeypatch.setattr(scratchwrite, "identity_map", lambda _: identity)
    for stage in ("reset", "register", "write"):
        run.stage(stage)
    extract, letter = run.documents
    documents = run.record("write")["documents"]
    assert documents[extract.sha1]["items_minted"] == 1
    # Gustav's item alone
    assert documents[letter.sha1]["items_minted"] == 1
    assert len(by_label(run.wiki, "Leopold Löbig")) == 1


# -- a false merge's self-loops (#524) ----------------------------------------

# a page of the 2008 family history, which names Eugen Löbig the master
# hatter, born 1836, and his son of the same name, born 1870, with a
# `father` claim between them: a homonym that clustering may merge
HISTORY = b"\x89PNG synthetic scan of a page of the 2008 family history"
EUGEN = "Eugen Löbig"


class FamilyWiki(Wiki):
    """The instance, with `father` in its vocabulary."""

    PROPERTIES = {**Wiki.PROPERTIES, "father": "P76"}


def eugen_born(local_id: str, year: int) -> dict:
    return claim(
        HISTORY,
        person(local_id, EUGEN),
        "date_of_birth",
        {"type": "time", "year": year},
    )


def father_and_son() -> ClaimBatch:
    return batch(
        eugen_born("son", 1870),
        claim(
            HISTORY,
            person("son", EUGEN),
            "father",
            {"type": "item", "value": person("father", EUGEN)},
        ),
        eugen_born("father", 1836),
    )


@pytest.fixture
def eugens(tmp_path, monkeypatch):
    made = Run(tmp_path, monkeypatch, wiki=FamilyWiki())
    made.add("family-history-2008.png", HISTORY, father_and_son())
    return made


def merge_the_eugens(run, monkeypatch) -> str:
    """Cluster the run, then put both Eugens in one cluster of the map,
    as a false merge does; the merged cluster's id."""
    from tests.fixtures.identity import joined

    run.past_the_barrier(until="clustering")
    identity = joined(scratchwrite.identity_map(run.context("write")), EUGEN)
    monkeypatch.setattr(scratchwrite, "identity_map", lambda _: identity)
    (cluster,) = [
        cid
        for cid, entry in identity.data["clusters"].items()
        if len(entry["members"]) == 2
    ]
    return cluster


def self_reference_alerts(run) -> list[dict]:
    return [
        alert
        for alert in run.driver.rundir.alerts()
        if alert["check"] == "self-reference"
    ]


class TestAFalseMerge:
    """A self-loop the writer refuses points to a false merge in the
    identity map: the write record keeps each refusal's cluster, and the
    `self-reference` anomaly check, the write stage's first, raises an
    alert of the write naming the cluster by its id and the property,
    with counts alone (docs/ANOMALIES.md)."""

    def test_the_record_counts_the_refusal_by_cluster_and_property(
        self, eugens, monkeypatch
    ):
        cluster = merge_the_eugens(eugens, monkeypatch)
        for stage in ("reset", "register", "write"):
            eugens.stage(stage)
        (doc,) = eugens.documents
        written = eugens.record("write")["documents"][doc.sha1]
        assert written["self_references"] == 1
        assert written["self_reference_clusters"] == [
            {"cluster": cluster, "property": "father", "claims": 1}
        ]
        # both births land on the one item, as the merge stands
        assert written["written"] == 2

    def test_it_raises_an_alert_of_the_write(self, eugens, monkeypatch):
        cluster = merge_the_eugens(eugens, monkeypatch)
        for stage in ("reset", "register", "write"):
            eugens.stage(stage)
        (doc,) = eugens.documents
        (alert,) = self_reference_alerts(eugens)
        assert (alert["stage"], alert["subject"], alert["status"]) == (
            "write",
            doc.sha1[:12],
            "new",
        )
        assert (alert["cluster"], alert["property"], alert["value"]) == (
            cluster,
            "father",
            1,
        )
        # ids and counts alone: no label of the cluster's people
        alerts = (eugens.run_dir / "alerts.jsonl").read_text()
        assert EUGEN not in alerts and "Löbig" not in alerts

    def test_a_refusal_for_another_reason_is_no_self_loop(
        self, tmp_path, monkeypatch
    ):
        """Beside the merged `father` claim, the father's pet name with a
        line break in it, refused for its text: counted among the
        refusals, never against a cluster, and raising no alert."""
        run = Run(tmp_path, monkeypatch, wiki=FamilyWiki())
        pet_name = claim(
            HISTORY,
            person("father", EUGEN),
            "referred_to_as",
            {
                "type": "monolingual",
                "value": {"text": "Muckel\nder Alte", "language": "de"},
            },
        )
        run.add(
            "family-history-2008.png",
            HISTORY,
            batch(*father_and_son().claims, pet_name),
        )
        cluster = merge_the_eugens(run, monkeypatch)
        for stage in ("reset", "register", "write"):
            run.stage(stage)
        (doc,) = run.documents
        written = run.record("write")["documents"][doc.sha1]
        assert (written["refused"], written["self_references"]) == (2, 1)
        assert written["self_reference_clusters"] == [
            {"cluster": cluster, "property": "father", "claims": 1}
        ]
        (alert,) = self_reference_alerts(run)
        assert (alert["cluster"], alert["property"]) == (cluster, "father")

    def test_a_failure_to_count_them_fails_no_document(
        self, eugens, monkeypatch
    ):
        merge_the_eugens(eugens, monkeypatch)

        def failing(*args):
            raise RuntimeError("Eugen Löbig")

        monkeypatch.setattr(scratchwrite, "self_reference_clusters", failing)
        for stage in ("reset", "register", "write"):
            eugens.stage(stage)
        (doc,) = eugens.documents
        record = eugens.record("write")
        assert record["failed"] == {}
        written = record["documents"][doc.sha1]
        assert written["self_references"] == 1
        assert written["self_reference_clusters"] is None
        assert self_reference_alerts(eugens) == []
        assert any(
            "self-loops not counted by cluster (RuntimeError)" in line
            for line in eugens.lines
        )

    def test_two_clusters_raise_nothing(self, eugens):
        eugens.past_the_barrier(until="write")
        (doc,) = eugens.documents
        written = eugens.record("write")["documents"][doc.sha1]
        assert written["self_references"] == 0
        assert written["self_reference_clusters"] == []
        assert written["written"] == 3
        assert self_reference_alerts(eugens) == []

    def test_turned_off_it_raises_nothing_and_the_record_stands(
        self, eugens, monkeypatch, tmp_path
    ):
        thresholds = tmp_path / "thresholds.yaml"
        thresholds.write_text(
            "checks:\n  self-reference:\n    enabled: false\n"
        )
        monkeypatch.setenv("YIZKR_ANOMALIES", str(thresholds))
        cluster = merge_the_eugens(eugens, monkeypatch)
        for stage in ("reset", "register", "write"):
            eugens.stage(stage)
        (doc,) = eugens.documents
        written = eugens.record("write")["documents"][doc.sha1]
        assert written["self_reference_clusters"] == [
            {"cluster": cluster, "property": "father", "claims": 1}
        ]
        assert self_reference_alerts(eugens) == []


class TestSelfReferenceClusters:
    """scratchwrite.self_reference_clusters, on the family history's
    batch and its map."""

    def test_the_value_s_cluster_when_the_subject_has_none(self):
        """The subject a QID, as an approved equivalence gives the
        master hatter's record, and the son in its `father` value."""
        from tests.fixtures.identity import mapped
        from yizkr.pipeline.review import claim_id

        written = batch(
            claim(
                HISTORY,
                {"kind": "qid", "qid": "Q71"},
                "father",
                {"type": "item", "value": person("son", EUGEN)},
            ),
            eugen_born("son", 1870),
        )
        identity = mapped(written)
        (son,) = {m["cluster"] for m in identity.data["mentions"].values()}
        refused = [claim_id(written.claims[0])]
        assert scratchwrite.self_reference_clusters(
            written, written, identity, refused
        ) == [{"cluster": son, "property": "father", "claims": 1}]

    def test_a_qualifier_s_cluster_when_neither_value_has_one(self):
        """The master hatter by a QID, his pet name, and the son in its
        `perspective` qualifier."""
        from tests.fixtures.identity import mapped
        from yizkr.pipeline.review import claim_id

        muckel = claim(
            HISTORY,
            {"kind": "qid", "qid": "Q71"},
            "referred_to_as",
            {
                "type": "monolingual",
                "value": {"text": "Muckel", "language": "de"},
            },
        )
        muckel["qualifiers"] = [
            {
                "property": "perspective",
                "value": {"type": "item", "value": person("son", EUGEN)},
            }
        ]
        written = batch(muckel, eugen_born("son", 1870))
        identity = mapped(written)
        (son,) = {m["cluster"] for m in identity.data["mentions"].values()}
        refused = [claim_id(written.claims[0])]
        assert scratchwrite.self_reference_clusters(
            written, written, identity, refused
        ) == [{"cluster": son, "property": "referred_to_as", "claims": 1}]

    def test_the_subject_s_cluster_comes_first(self):
        """The son, and the master hatter by a QID as his `father`: the
        son's cluster, though the value names an item too."""
        from tests.fixtures.identity import mapped
        from yizkr.pipeline.review import claim_id

        written = batch(
            claim(
                HISTORY,
                person("son", EUGEN),
                "father",
                {"type": "item", "value": {"kind": "qid", "qid": "Q71"}},
            ),
            eugen_born("son", 1870),
        )
        identity = mapped(written)
        (son,) = {m["cluster"] for m in identity.data["mentions"].values()}
        refused = [claim_id(written.claims[0])]
        assert scratchwrite.self_reference_clusters(
            written, written, identity, refused
        ) == [{"cluster": son, "property": "father", "claims": 1}]

    def test_a_self_loop_no_cluster_makes(self):
        """The `human` vocabulary item named by its key and by its QID,
        a loop no cluster makes. The claims are taken as `refused` names
        them, never judged again."""
        from tests.fixtures.identity import mapped
        from yizkr.pipeline.review import claim_id

        written = batch(
            claim(
                HISTORY,
                {"kind": "vocab", "key": "human"},
                "father",
                {"type": "item", "value": {"kind": "qid", "qid": "Q9"}},
            ),
            eugen_born("son", 1870),
        )
        refused = [claim_id(written.claims[0])]
        assert scratchwrite.self_reference_clusters(
            written, written, mapped(written), refused
        ) == [{"cluster": None, "property": "father", "claims": 1}]

    def test_nothing_refused_reads_no_map(self):
        assert (
            scratchwrite.self_reference_clusters(
                father_and_son(), father_and_son(), None, []
            )
            == []
        )


def test_the_record_counts_the_judgement_notes_written(run, monkeypatch):
    """Each document's record gives the noted run blocks its write added
    (WriteResult.notes_written, #410), and the stage's log line their
    total."""
    from yizkr.pipeline.writer import BatchWriter

    original = BatchWriter.write_batch

    def noted(self, *args, **options):
        result = original(self, *args, **options)
        result.notes_written = 2
        return result

    monkeypatch.setattr(BatchWriter, "write_batch", noted)
    run.past_the_barrier()
    documents = run.record("write")["documents"].values()
    assert [d["notes_written"] for d in documents] == [2, 2]
    assert any(
        "item; 4 judgement notes written;" in line for line in run.lines
    )


def test_the_record_counts_the_claims_naming_their_own_subjects_item(
    run, monkeypatch
):
    from tests.fixtures.identity import joined

    extract, letter = run.documents
    # the letter's batch gains a claim the writer refuses for another
    # reason: a date of birth of the letter itself
    misplaced = claim(
        LETTER,
        {"kind": "artifact", "sha1": sha1(LETTER)},
        "date_of_birth",
        {"type": "time", "year": 1952},
    )
    letter.batch.write_text(
        batch(
            brothers(LETTER), born(LETTER, 1913), depicts(LETTER), misplaced
        ).model_dump_json(indent=2)
    )
    run.past_the_barrier(until="clustering")
    # the map puts Gustav and Leopold in one cluster: the letter's
    # `sibling` claim between them would make the one item its own sibling
    identity = joined(
        scratchwrite.identity_map(run.context("write")),
        ("Leopold Löbig", "Gustav Löbig"),
    )
    monkeypatch.setattr(scratchwrite, "identity_map", lambda _: identity)
    for stage in ("reset", "register", "write"):
        run.stage(stage)
    record = run.record("write")
    documents = record["documents"]
    # counted apart from the letter's other refusal
    assert documents[letter.sha1]["refused"] == 2
    assert documents[letter.sha1]["self_references"] == 1
    # and by reason, every reason in its order (#552)
    assert documents[letter.sha1]["refused_by_reason"] == {
        "retired_or_reference_property": 0,
        "subject_rule": 1,
        "text": 0,
        "side_rule": 0,
        "self_reference": 1,
    }
    assert sum(documents[extract.sha1]["refused_by_reason"].values()) == 0
    assert any(
        "2 claims refused, 1 of them for an item value that is their "
        "subject's item;" in line
        for line in run.lines
    )
    assert documents[extract.sha1]["self_references"] == 0
    assert not record["failed"]
    (leopold,) = by_label(run.wiki, "Leopold Löbig")
    assert statements(run.wiki, leopold, "sibling") == []
    # both births are written
    assert len(statements(run.wiki, leopold, "date_of_birth")) == 2
    # the run report counts them apart from extraction's rejects
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert content["write"]["claims_refused"] == 2
    assert content["write"]["claims_self_referencing"] == 1
    assert content["write"]["claims_refused_by_reason"] == {
        "retired_or_reference_property": 0,
        "subject_rule": 1,
        "text": 0,
        "side_rule": 0,
        "self_reference": 1,
    }
    (section,) = report._write_sections(content)
    assert (
        "2 refused: 0 for a retired or reference property, 1 for the "
        "subject rules, 0 for a text the instance cannot hold, 0 for an "
        "entity the side rule blocks, 1 for an item value that is the "
        "subject's item; " in section.blocks[0][0]
    )


def test_a_write_of_no_documents_counts_no_self_references():
    # a write that wrote no document counted none: 0, never absent
    record = {"depicts_left_out": 0, "documents": {}}
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert content["write"]["claims_self_referencing"] == 0
    assert content["write"]["claims_refused"] == 0
    assert content["write"]["claims_refused_by_reason"] == dict.fromkeys(
        REFUSAL_REASONS, 0
    )


def test_the_refusals_by_reason_sum_over_the_documents():
    # each document's counts, reason by reason, in the fixed order (#552)
    def counted(**given):
        return {reason: given.get(reason, 0) for reason in REFUSAL_REASONS}

    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {
                "refused": 3,
                "refused_by_reason": counted(text=2, subject_rule=1),
            },
            "b" * 40: {
                "refused": 2,
                "refused_by_reason": counted(
                    retired_or_reference_property=1, side_rule=1
                ),
            },
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    by_reason = content["write"]["claims_refused_by_reason"]
    assert list(by_reason) == list(REFUSAL_REASONS)
    assert by_reason == counted(
        retired_or_reference_property=1, subject_rule=1, text=2, side_rule=1
    )
    assert sum(by_reason.values()) == content["write"]["claims_refused"]


@pytest.mark.parametrize(
    "malformed", [[1], "text", None, 3], ids=["list", "str", "null", "int"]
)
def test_a_malformed_record_by_reason_holds_no_figure(malformed):
    # a record's counts by reason that are not a mapping: no figure, and
    # the report is still made
    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {"refused": 1, "refused_by_reason": malformed},
            "b" * 40: {
                "refused": 1,
                "refused_by_reason": dict.fromkeys(REFUSAL_REASONS, 0)
                | {"text": 1},
            },
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert "claims_refused_by_reason" not in content["write"]
    (section,) = report._write_sections(content)
    assert "2 refused; " in section.blocks[0][0]


def test_a_reason_a_record_does_not_count_is_absent_not_zero():
    # a reason one record lacks, or gives as no count, is left out: absent
    # in the run diff, never 0; the reasons every record counts remain
    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {
                "refused": 1,
                "refused_by_reason": {"text": "x", "subject_rule": 1},
            },
            "b" * 40: {
                "refused": 2,
                "refused_by_reason": dict.fromkeys(REFUSAL_REASONS, 0)
                | {"text": 1, "subject_rule": 1},
            },
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert content["write"]["claims_refused_by_reason"] == {"subject_rule": 2}
    (section,) = report._write_sections(content)
    assert "3 refused: 2 for the subject rules; " in section.blocks[0][0]


def test_an_older_record_gives_its_self_references_alone():
    # a write that counted self-references but no reasons: the line says
    # how many of the refused are self-references, as it did
    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {"refused": 2, "self_references": 1, "items_minted": 0}
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert "claims_refused_by_reason" not in content["write"]
    (section,) = report._write_sections(content)
    assert (
        "2 refused, 1 of them for an item value that is their subject's "
        "item; 0 items minted," in section.blocks[0][0]
    )


def test_a_record_some_of_whose_documents_lack_reasons_holds_none():
    # the counts would not sum to the claims refused: absent, never 0
    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {
                "refused": 1,
                "refused_by_reason": dict.fromkeys(REFUSAL_REASONS, 0)
                | {"text": 1},
            },
            "b" * 40: {"refused": 2},
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert "claims_refused_by_reason" not in content["write"]


def test_a_record_that_does_not_count_self_references_holds_no_figure():
    # a write that did not count them: the figure is absent, never 0, so
    # the run diff does not read it as a change
    record = {
        "depicts_left_out": 0,
        "documents": {
            "a" * 40: {"written": 3, "refused": 1, "items_minted": 2}
        },
    }
    content = report._aggregate_content({}, {}, {}, _vocabulary(), record)
    assert "claims_self_referencing" not in content["write"]
    assert "claims_refused_by_reason" not in content["write"]
    assert content["write"]["claims_refused"] == 1
    (section,) = report._write_sections(content)
    assert "1 refused; 2 items minted," in section.blocks[0][0]


# -- progress -----------------------------------------------------------------


def progress_events(run, stage: str | None = None) -> list[dict]:
    """The progress events the stages emitted, each without its time and
    invocation: what the monitor reads (#561)."""
    return [
        {
            key: value
            for key, value in event.items()
            if key not in ("at", "event", "invocation")
        }
        for event in run.driver.rundir.events()
        if event["event"] == PROGRESS
        and (stage is None or event["stage"] == stage)
    ]


class TestProgress:
    """Each stage tells the run's events how far it has got, with counts
    only (#561)."""

    def test_register_tells_of_each_document(self, run):
        run.past_the_barrier(until="register")
        assert progress_events(run, "register") == [
            {
                "stage": "register",
                "phase": "registering",
                "unit": "documents",
                "done": done,
                "of": 2,
                "failed": 0,
                "refused": 0,
            }
            for done in (0, 1, 2)
        ]

    def test_write_tells_of_each_document_and_its_statements(self, run):
        run.past_the_barrier(until="write")
        events = progress_events(run, "write")
        assert [(event["done"], event["of"]) for event in events] == [
            (0, 2),
            (1, 2),
            (2, 2),
        ]
        assert {event["phase"] for event in events} == {"writing"}
        documents = run.record("write")["documents"].values()
        assert events[0]["written"] == 0
        assert {
            key: events[-1][key]
            for key in ("written", "referenced", "skipped", "claims_refused")
        } == {
            "written": sum(d["written"] for d in documents),
            "referenced": sum(d["referenced"] for d in documents),
            "skipped": sum(d["skipped"] for d in documents),
            "claims_refused": sum(d["refused"] for d in documents),
        }
        assert events[-1]["written"] > 0

    def test_a_failed_document_counts_as_done_and_failed(
        self, run, monkeypatch
    ):
        from yizkr.pipeline import writer

        letter = run.documents[1]
        real = writer.BatchWriter.write_batch

        def failing(self, batch, *args, **kwargs):
            if batch.claims[0].source.artifact.sha1 == letter.sha1:
                raise ConnectionError("the wiki went away")
            return real(self, batch, *args, **kwargs)

        monkeypatch.setattr(writer.BatchWriter, "write_batch", failing)
        run.past_the_barrier(until="write")
        last = progress_events(run, "write")[-1]
        assert (last["done"], last["of"], last["failed"]) == (2, 2, 1)

    def test_an_invocation_that_goes_on_counts_what_was_done_before(
        self, run, monkeypatch
    ):
        from yizkr.pipeline import writer

        letter = run.documents[1]
        real = writer.BatchWriter.write_batch

        def failing(self, batch, *args, **kwargs):
            if batch.claims[0].source.artifact.sha1 == letter.sha1:
                raise ConnectionError("the wiki went away")
            return real(self, batch, *args, **kwargs)

        monkeypatch.setattr(writer.BatchWriter, "write_batch", failing)
        run.past_the_barrier(until="write")
        monkeypatch.setattr(writer.BatchWriter, "write_batch", real)
        before = len(progress_events(run, "write"))
        run.past_the_barrier(until="write")
        again = progress_events(run, "write")[before:]
        # the extract was written before: the letter alone is to go
        assert [(event["done"], event["failed"]) for event in again] == [
            (1, 0),
            (2, 0),
        ]

    def test_register_counts_the_refused_and_the_failed(
        self, monkeypatch, run
    ):
        from tests.fixtures.identity import mapped

        # the identity map holds none of the shop front's mentions, so its
        # batch is refused whole; the letter's registration fails
        identity = mapped(
            *(scratchwrite.extracted_batch(doc) for doc in run.documents)
        )
        monkeypatch.setattr(scratchwrite, "identity_map", lambda _: identity)
        run.add(
            "brunnenring-35.png",
            SHOP,
            batch(born(SHOP, 1914, local_id="leopold-shop")),
        )
        letter = run.documents[1]
        real = run.driver._step_register

        def register(doc):
            if doc.sha1 == letter.sha1:
                raise ConnectionError("the wiki went away")
            return real(doc)

        monkeypatch.setattr(run.driver, "_step_register", register)
        run.stage("reset")
        run.stage("register")
        events = progress_events(run, "register")
        assert [(e["done"], e["of"], e["failed"]) for e in events] == [
            (0, 2, 0),
            (1, 2, 0),
            (2, 2, 1),
        ]
        assert {e["refused"] for e in events} == {1}

    def test_write_counts_the_claims_it_refuses_apart(self, run):
        # a date of birth of the letter itself: the writer refuses it
        letter = run.documents[1]
        misplaced = claim(
            LETTER,
            {"kind": "artifact", "sha1": sha1(LETTER)},
            "date_of_birth",
            {"type": "time", "year": 1952},
        )
        letter.batch.write_text(
            batch(
                brothers(LETTER), born(LETTER, 1913), misplaced
            ).model_dump_json(indent=2)
        )
        run.past_the_barrier(until="write")
        last = progress_events(run, "write")[-1]
        documents = run.record("write")["documents"].values()
        refused = sum(d["refused"] for d in documents)
        assert refused == 1
        assert last["claims_refused"] == refused
        assert last["skipped"] == sum(d["skipped"] for d in documents)
        assert last["claims_refused"] != last["skipped"]

    def test_the_reset_is_given_its_step_by_name(self, run, monkeypatch):
        """A stand-in for the reset that takes the step callback by name
        alone, as the live tests' stand-ins may, is given it."""
        told = []

        def reset(env_file, log, **options):
            options["progress"](1, 1, "start the stack")
            told.append(options)

        monkeypatch.setattr(scratchwrite, "reset_instance", reset)
        run.past_the_barrier(until="reset")
        assert len(told) == 1
        assert progress_events(run, "reset")[0]["step"] == "start the stack"

    def test_reset_tells_of_each_step_as_it_starts(self, run):
        run.past_the_barrier(until="reset")
        assert progress_events(run, "reset") == [
            {
                "stage": "reset",
                "phase": "resetting",
                "unit": "steps",
                "done": number,
                "of": len(RESET_STEPS),
                "step": name,
            }
            for number, name in enumerate(RESET_STEPS)
        ]

    def test_a_rerun_tells_that_it_skips(self, run, tmp_path, monkeypatch):
        run.past_the_barrier()
        again = Run(tmp_path, monkeypatch, wiki=run.wiki, run_id="run-2")
        again.documents = run.documents
        again.shown = ["run-1"]
        before = len(progress_events(again))
        again.past_the_barrier()
        assert progress_events(again)[before:] == [
            {"stage": "reset", "phase": "skipped"},
            {"stage": "register", "phase": "skipped"},
            {"stage": "write", "phase": "skipped"},
            {"stage": "site", "phase": "building"},
        ]

    def test_an_invocation_that_goes_on_tells_that_it_resumed(self, run):
        run.past_the_barrier(until="reset")
        run.stage("reset")
        assert progress_events(run, "reset")[-1] == {
            "stage": "reset",
            "phase": "resumed",
        }

    def test_the_site_tells_that_it_waits_and_then_builds(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        run.shown = []
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                run.shown = ["run-1"]

        monkeypatch.setattr(scratchwrite, "sleep", sleep)
        run.stage("site")
        # one event for the wait, however often it polls
        assert progress_events(run, "site") == [
            {"stage": "site", "phase": "waiting"},
            {"stage": "site", "phase": "building"},
        ]

    def test_a_site_whose_query_service_has_caught_up_does_not_wait(self, run):
        run.past_the_barrier()
        assert progress_events(run, "site") == [
            {"stage": "site", "phase": "building"}
        ]

    def test_no_event_names_a_document(self, run):
        run.past_the_barrier()
        events = progress_events(run)
        assert events
        names = {"stage", "phase", "unit", "step"}
        for event in events:
            for key, value in event.items():
                if key in names:
                    assert value in (
                        *RESET_STEPS,
                        "reset",
                        "register",
                        "write",
                        "site",
                        "resetting",
                        "registering",
                        "writing",
                        "building",
                        "documents",
                        "steps",
                    )
                else:
                    assert type(value) is int


def test_the_monitor_reads_what_the_stages_tell(run):
    """The stages' own progress events, between the driver's start and
    end of each, as the monitor shows them (#561)."""
    from yizkr.pipeline.monitor import render_terminal, summarise_run

    events = run.driver.rundir
    for name in ("clustering", "reset", "register", "write", "site"):
        events.emit("started", stage=name)
        run.stage(name)
        events.emit("done", stage=name)
    summary = summarise_run(run.run_dir)
    shown = {stage.name: stage for stage in summary.corpus}
    assert list(shown) == ["clustering", "reset", "register", "write", "site"]
    assert {stage.state for stage in shown.values()} == {"done"}
    write = shown["write"]
    assert (write.phase, write.done, write.of) == ("writing", 2, 2)
    assert dict(write.counts)["written"] > 0
    assert shown["site"].phase == "building"
    view = render_terminal(summary, width=400)
    assert "writing: 2 of 2 documents" in view
    for document in run.documents:
        assert document.sha1[:10] not in view
