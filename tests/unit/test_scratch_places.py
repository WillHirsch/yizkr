"""Offline tests for a scratch run's place stage (#201; ADR-0009,
ADR-0038 §3 and §8.5-8.7, ADR-0025 §1): after the write and before the
site, it asks which existing place each container a written place's
mention record states means, and writes the links the sources state
(yizkr.pipeline.scratchwrite.run_places), on the unit tests' in-memory
instance, with a fake engine behind the run's LLM cache, and no stack.

* The driver runs it after the write and before the site; it touches the
  wiki and is the write side's one stage that calls the engine.
* A link a record states is asked where its container names more than
  one place, or where no claim of its document gives it. The answer
  names a candidate, or none, which writes nothing.
* Each answered link is written with no review CSV, at normal rank:
  `part of` the item the answer names, with the time its document's
  claim gives and none where the record alone states it, stated in the
  document's artifact with no page, and inferred from the stage's run
  item, which names the asking model and is marked an unratified
  scratch run's.
* Cycles, conflicting answers and answers outside the schema are refused
  and counted, never written; a refused cycle is named in the local run
  report. The run report and the run diff count them.
* Every call goes through the LLM cache, and no prompt names a QID: a run
  on the same batches after another reset asks the engine nothing.
* The stage key is the write key with the prompt and the model; the
  instance-contents record takes it once the stage has written every
  link. The stage refuses, writing nothing, unless this run's reset and
  write left the instance; it waits for a complete write; and it holds
  the live-stack lock while it reads, calls and writes.
* A subscription limit met while it asks leaves nothing recorded: the
  next invocation asks again, the batches already answered coming from
  the LLM cache. An invocation cut short after asking writes the rest
  without asking again.

Every document is fictional (docs/EXAMPLES.md): Leopold Löbig's birth
register extract, which places Flörburg in Kreis Peiltal, Hesse,
Germany; Gustav's letter, which places Fläckfeld in Kreis Peiltal; and
the 1941 death certificate with the worked examples of a dated
containment, Koshnovka in Kaldovsky District in 1941 and Kaldovsky
District in Chadsk Oblast from 1937.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import UTC, datetime

import pytest

from tests.unit.test_scratch_write import (
    CONFIG,
    RUN,
    Run,
    Wiki,
    _vocabulary,
    by_label,
    sha1,
)
from yizkr.claims import ClaimBatch
from yizkr.core.instance import clear_contents, recorded_contents
from yizkr.pipeline import places, report, rundiff, scratch, scratchwrite
from yizkr.pipeline.llm import UsageLimitReached
from yizkr.pipeline.scratch import CORPUS_DIR
from yizkr.pipeline.scratchwrite import ScratchWriteRefused

# the driver's corpus stages keep the write side, the place stage among it
pytestmark = pytest.mark.write_side

EXTRACT = b"\x89PNG synthetic scan of Leopold's 1914 birth register extract"
LETTER = b"\x89PNG synthetic scan of Gustav's 1952 letter to Leopold"
DEATH = b"\x89PNG synthetic scan of the 1941 death certificate"
MODEL = "claude-sonnet-5"


class PlaceWiki(Wiki):
    """The write side's in-memory instance, with the place vocabulary and
    the qualifiers that date a containment."""

    PROPERTIES = {
        **Wiki.PROPERTIES,
        "place_of_birth": "P76",
        "point_in_time": "P77",
        "start_time": "P78",
        "place_of_death": "P79",
    }
    ITEMS = {**Wiki.ITEMS, "place": "Q10"}

    def find_item_by_label(self, label: str) -> str | None:
        # as the wiki client's: it fails on a label two items hold, and
        # only on that one (two people of one name stay apart in a map)
        found = [
            qid
            for qid, entity in self.entities.items()
            if entity.get("labels", {}).get("en", {}).get("value") == label
        ]
        assert len(found) <= 1, f"two items are labelled {label!r}"
        return found[0] if found else None

    def wiped(self) -> None:
        fresh = PlaceWiki()
        self.entities, self.media = fresh.entities, fresh.media
        self.writes = 0


def statements(wiki, qid: str, key: str) -> list[dict]:
    return wiki.entities[qid]["claims"].get(PlaceWiki.PROPERTIES[key], [])


def cited(statement: dict, key: str) -> list[str]:
    return [
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(PlaceWiki.PROPERTIES[key], [])
    ]


def string_values(wiki, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in statements(wiki, qid, key)
    ]


def label(wiki, qid: str) -> str:
    return wiki.entities[qid]["labels"]["en"]["value"]


def item_values(wiki, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]["id"]
        for statement in statements(wiki, qid, key)
    ]


def qualifiers(statement: dict) -> dict[str, int]:
    """Each qualifier of a statement, by its key, with its year."""
    keys = {pid: key for key, pid in PlaceWiki.PROPERTIES.items()}
    return {
        keys[pid]: int(snaks[0]["datavalue"]["value"]["time"][1:5])
        for pid, snaks in statement.get("qualifiers", {}).items()
    }


def graph(wiki) -> list:
    """What the query service would give of the in-memory instance: its
    places, with their aliases, their descriptions and the places it puts
    each in."""
    place = PlaceWiki.ITEMS["place"]
    return [
        places.PlaceRecord(
            qid=qid,
            label=label(wiki, qid),
            aliases=[
                alias["value"]
                for alias in wiki.entities[qid]
                .get("aliases", {})
                .get("en", [])
            ],
            parents=item_values(wiki, qid, "part_of"),
            description=wiki.entities[qid]
            .get("descriptions", {})
            .get("en", {})
            .get("value", ""),
        )
        for qid in sorted(wiki.entities, key=lambda q: int(q[1:]))
        if place in item_values(wiki, qid, "instance_of")
    ]


def namesake(wiki, qid: str, name: str, description: str) -> str:
    """Put a place of the name `name` on the instance as an item the
    identity map kept apart would be, without a revision the write-phase
    state would take for another session's write."""
    p, place = PlaceWiki.PROPERTIES["instance_of"], PlaceWiki.ITEMS["place"]
    wiki.entities[qid] = {
        "id": qid,
        "type": "item",
        "labels": {"en": {"language": "en", "value": name}},
        "descriptions": {"en": {"language": "en", "value": description}},
        "aliases": {},
        "claims": {
            p: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "property": p,
                        "datavalue": {
                            "value": {
                                "entity-type": "item",
                                "numeric-id": int(place[1:]),
                                "id": place,
                            },
                            "type": "wikibase-entityid",
                        },
                        "datatype": "wikibase-item",
                    },
                    "type": "statement",
                    "rank": "normal",
                    "id": f"{qid}$namesake",
                }
            ]
        },
        "lastrevid": 1,
    }
    return qid


# a question of the prompt: its number, the container it asks of, and the
# candidates, each by its number and name
QUESTION = re.compile(
    r"^(\d+)\. A record states .*? Which place does '([^']*)', container "
    r"\d+ of that chain, name\?\n((?:   place \d+: .*\n?)+)",
    re.M,
)
CANDIDATE = re.compile(r"^   place (\d+): (.*)$", re.M)


class Adjudicator:
    """A fake engine that answers each question of a place prompt: the
    one candidate where there is one, else the first candidate whose name
    holds `choices[container]`, else `uncertain`; `uncertain` too where
    `choices` says so. It answers with `extra` lines besides, and records
    each prompt it was given. It checks the live-stack lock is held as it
    answers. `pause` makes its next call a subscription limit's, and
    `broken` its answers lack their terminal sentinel."""

    model = MODEL
    model_identifier = MODEL
    effort = "high"
    config_hash = "0" * 64
    cli_version = None

    def __init__(self, run, choices=None, extra=()):
        self.run = run
        self.choices = dict(choices or {})
        self.extra = list(extra)
        self.prompts: list[str] = []
        self.pause = False
        self.broken = False

    def choose(self, container: str, block: str):
        candidates = CANDIDATE.findall(block)
        wanted = self.choices.get(container)
        if wanted == places.UNCERTAIN:
            return wanted
        if wanted is None:
            return 1 if len(candidates) == 1 else places.UNCERTAIN
        for number, name in candidates:
            if wanted in name:
                return int(number)
        return places.UNCERTAIN

    def complete(self, prompt, image_paths=()):
        assert self.run.held, "the engine was called without the lock"
        if self.pause:
            self.pause = False
            raise UsageLimitReached(
                "the five-hour window is used up",
                resumes_at=datetime(2026, 10, 23, 15, tzinfo=UTC),
            )
        self.prompts.append(prompt)
        lines = [
            json.dumps(
                {
                    "question": int(number),
                    "place": self.choose(container, block),
                    "confidence": 0.9,
                    "evidence": "fictional",
                }
            )
            for number, container, block in QUESTION.findall(prompt)
        ]
        if self.broken:
            return "\n".join(lines)
        return "\n".join([*lines, *self.extra, '{"done": true}'])


def place(local_id: str, name: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": name,
        "instance_of": "place",
    }


def source(data: bytes) -> dict:
    return {
        "artifact": {"kind": "artifact", "sha1": sha1(data)},
        "page_number": "1",
    }


def born_at(data: bytes, where: dict) -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": "leopold",
            "label": "Leopold Löbig",
            "instance_of": "human",
        },
        "property": "place_of_birth",
        "value": {"type": "item", "value": where},
        "source": source(data),
        "confidence": 0.9,
    }


def part_of(data: bytes, child: dict, parent: dict, *dated) -> dict:
    return {
        "subject": child,
        "property": "part_of",
        "value": {"type": "item", "value": parent},
        "qualifiers": [
            {"property": key, "value": {"type": "time", "year": year}}
            for key, year in dated
        ],
        "source": source(data),
    }


def entity(local_id: str, name: str, within=None) -> dict:
    found = {"id": local_id, "label": name, "kind": "place"}
    if within is not None:
        found["structure"] = {"kind": "place", "within": list(within)}
    return found


LEOPOLD = {"id": "leopold", "label": "Leopold Löbig", "kind": "human"}


def extract_batch() -> ClaimBatch:
    """The extract: Leopold born at Flörburg, which its record puts in
    Kreis Peiltal, Hesse, Germany, and a claim of which puts it in Kreis
    Peiltal; and those three places."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                LEOPOLD,
                entity(
                    "florburg",
                    "Flörburg",
                    ["Kreis Peiltal", "Hesse", "Germany"],
                ),
                entity("kreis", "Kreis Peiltal"),
                entity("hesse", "Hesse"),
                entity("germany", "Germany"),
            ],
            "claims": [
                born_at(EXTRACT, place("florburg", "Flörburg")),
                born_at(EXTRACT, place("kreis", "Kreis Peiltal")),
                born_at(EXTRACT, place("hesse", "Hesse")),
                born_at(EXTRACT, place("germany", "Germany")),
                part_of(
                    EXTRACT,
                    place("florburg", "Flörburg"),
                    place("kreis", "Kreis Peiltal"),
                ),
            ],
        }
    )


def letter_batch() -> ClaimBatch:
    """The letter: Leopold born at Fläckfeld, which its record puts in
    Kreis Peiltal."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                LEOPOLD,
                entity("flackfeld", "Fläckfeld", ["Kreis Peiltal"]),
            ],
            "claims": [born_at(LETTER, place("flackfeld", "Fläckfeld"))],
        }
    )


def death_batch() -> ClaimBatch:
    """The death certificate's worked examples: Koshnovka in Kaldovsky
    District in 1941, and Kaldovsky District in Chadsk Oblast from
    1937, as claims and as its record's chain."""
    kosh = place("kosh", "Koshnovka")
    kald = place("kald", "Kaldovsky District")
    chadsk = place("chadsk", "Chadsk Oblast")
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {"id": "rudolf", "label": "Rudolf Löbig", "kind": "human"},
                entity(
                    "kosh",
                    "Koshnovka",
                    ["Kaldovsky District", "Chadsk Oblast"],
                ),
                entity("kald", "Kaldovsky District"),
                entity("chadsk", "Chadsk Oblast"),
            ],
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "rudolf",
                        "label": "Rudolf Löbig",
                        "instance_of": "human",
                    },
                    "property": "place_of_death",
                    "value": {"type": "item", "value": kosh},
                    "source": source(DEATH),
                },
                part_of(DEATH, kosh, kald, ("point_in_time", 1941)),
                part_of(DEATH, kald, chadsk, ("start_time", 1937)),
            ],
        }
    )


class PlaceRun(Run):
    """A scratch run of the documents, with the fake engine in the place
    stage's role, behind the run's LLM cache, and the graph read from the
    in-memory instance."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        wiki=None,
        run_id="run-1",
        documents=None,
    ):
        monkeypatch.setenv("YIZKR_LLM_MODEL", MODEL)
        super().__init__(
            tmp_path, monkeypatch, wiki=wiki or PlaceWiki(), run_id=run_id
        )
        self.engine = Adjudicator(self)
        self.driver._engines[scratchwrite.PLACE_ROLE] = self.engine
        self.driver.rundir.path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(scratchwrite, "place_graph", graph)
        if documents is None:
            documents = (
                ("leopold-birth-extract.png", EXTRACT, extract_batch()),
                ("gustav-letter-1952.png", LETTER, letter_batch()),
            )
        for name, data, batch in documents:
            self.add(name, data, batch)

    def past_the_barrier(self, until: str = "site") -> None:
        for name in ("clustering", "reset", "register", "write", "places"):
            self.stage(name)
            if name == until:
                return
        self.stage("site")

    def qid(self, name: str) -> str:
        (found,) = by_label(self.wiki, name)
        return found

    def artifact(self, data: bytes) -> str:
        return self.wiki.string_statement_index(
            PlaceWiki.PROPERTIES["media_sha1"]
        )[sha1(data)]


@pytest.fixture
def run(tmp_path, monkeypatch):
    return PlaceRun(tmp_path, monkeypatch)


def containment(wiki, qid: str) -> list[dict]:
    return statements(wiki, qid, "part_of")


def place_run_items(wiki) -> list[str]:
    return [
        qid
        for qid in wiki.entities
        if label(wiki, qid).startswith("place-hierarchy run ")
    ]


def stage_written(wiki, qid: str) -> list[dict]:
    """The part_of statements of `qid` the place stage wrote: those
    inferred from its run item."""
    runs = set(place_run_items(wiki))
    return [
        statement
        for statement in containment(wiki, qid)
        if set(cited(statement, "inferred_from_extraction_run")) & runs
    ]


DEATH_DOCUMENTS = (("rudolf-death-1941.png", DEATH, death_batch()),)


def with_namesake(run, monkeypatch, qid="Q900") -> None:
    """The graph also holds a second Kaldovsky District, which the
    identity map kept apart from the certificate's, told apart by its
    description, put on the instance as the place stage reads it."""

    def read(wiki):
        if qid not in wiki.entities:
            namesake(wiki, qid, "Kaldovsky District", "a district of 1941")
        return graph(wiki)

    monkeypatch.setattr(scratchwrite, "place_graph", read)


# -- the stage among the driver's ---------------------------------------------


def test_the_place_stage_runs_after_the_write_and_before_the_site():
    names = [stage.name for stage in scratch.CORPUS_STAGES]
    assert names.index("places") == names.index("write") + 1
    assert names.index("site") == names.index("places") + 1
    (stage,) = [s for s in scratch.CORPUS_STAGES if s.name == "places"]
    assert stage.touches_wiki
    assert stage.calls_engine
    # the write side's only stage that calls the engine
    assert [s.name for s in scratch.CORPUS_STAGES if s.calls_engine] == [
        "places"
    ]


# -- what it writes -----------------------------------------------------------


class TestWritten:
    def test_each_link_a_record_alone_states_is_written(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        florburg, kreis, hesse, germany, flackfeld = (
            run.qid(name)
            for name in (
                "Flörburg",
                "Kreis Peiltal",
                "Hesse",
                "Germany",
                "Fläckfeld",
            )
        )
        assert item_values(wiki, kreis, "part_of") == [hesse]
        assert item_values(wiki, hesse, "part_of") == [germany]
        assert item_values(wiki, flackfeld, "part_of") == [kreis]
        # the link a claim gives, to a container that names one place, was
        # written by the write alone; never a shortcut
        assert item_values(wiki, florburg, "part_of") == [kreis]
        assert stage_written(wiki, florburg) == []
        assert item_values(wiki, germany, "part_of") == []

    def test_a_record_s_link_is_stated_in_its_document_with_no_page(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        (run_item,) = place_run_items(wiki)
        for name, data in (
            ("Kreis Peiltal", EXTRACT),
            ("Hesse", EXTRACT),
            ("Fläckfeld", LETTER),
        ):
            (statement,) = containment(wiki, run.qid(name))
            assert statement.get("rank", "normal") == "normal"
            # stated in the document, inferred from the stage's run item,
            # each a block of its own, with no page and no time
            assert cited(statement, "stated_in") == [run.artifact(data)]
            assert cited(statement, "inferred_from_extraction_run") == [
                run_item
            ]
            assert len(statement["references"]) == 2
            assert all(
                set(block["snaks"])
                <= {
                    PlaceWiki.PROPERTIES["stated_in"],
                    PlaceWiki.PROPERTIES["inferred_from_extraction_run"],
                }
                for block in statement["references"]
            )
            assert qualifiers(statement) == {}

    def test_a_claim_s_link_is_written_with_its_time(
        self, tmp_path, monkeypatch
    ):
        """The certificate's Kaldovsky District names two places: the
        answer names the namesake, and the link is written to it with the
        time the certificate's claim gives, 1941; the link to Chadsk
        Oblast is written by the write alone."""
        run = PlaceRun(tmp_path, monkeypatch, documents=DEATH_DOCUMENTS)
        with_namesake(run, monkeypatch)
        run.engine.choices = {"Kaldovsky District": "a district of 1941"}
        run.past_the_barrier()
        kosh = run.qid("Koshnovka")
        (statement,) = stage_written(run.wiki, kosh)
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == "Q900"
        assert qualifiers(statement) == {"point_in_time": 1941}
        assert cited(statement, "stated_in") == [run.artifact(DEATH)]
        counts = run.record("places")["counts"]
        assert (counts["written"], counts["record_only"]) == (1, 0)

    def test_an_answer_naming_the_claim_s_own_item_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        run = PlaceRun(tmp_path, monkeypatch, documents=DEATH_DOCUMENTS)
        with_namesake(run, monkeypatch)
        run.engine.choices = {"Kaldovsky District": "Chadsk Oblast"}
        run.past_the_barrier()
        counts = run.record("places")["counts"]
        assert (counts["written"], counts["present"]) == (0, 1)
        assert place_run_items(run.wiki) == []

    def test_an_uncertain_answer_writes_nothing(self, tmp_path, monkeypatch):
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.choices = {"Hesse": places.UNCERTAIN}
        run.past_the_barrier()
        assert item_values(run.wiki, run.qid("Kreis Peiltal"), "part_of") == []
        counts = run.record("places")["counts"]
        assert counts["uncertain"] == 1
        # Hesse within Germany: its inner end, Hesse, was answered
        # uncertain
        assert counts["unresolved"] == 1
        assert counts["written"] == 1

    def test_the_run_item_names_the_model_and_the_scratch_run(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        (qid,) = place_run_items(wiki)
        key = run.record("places")["key"]
        assert label(wiki, qid) == f"place-hierarchy run {MODEL} {key[:10]}"
        assert item_values(wiki, qid, "instance_of") == [
            PlaceWiki.ITEMS["extraction_run"]
        ]
        assert string_values(wiki, qid, "scratch_run") == ["run-1"]
        identity = scratchwrite.identity_map(run.context("places"))
        assert string_values(wiki, qid, "identity_map_hash") == [identity.hash]

    def test_no_review_csv(self, run):
        run.past_the_barrier()
        assert not list(run.run_dir.rglob("*.csv"))

    def test_answers_and_raw_answers_are_kept_in_the_stage_store(self, run):
        run.past_the_barrier()
        record = run.record("places")
        assert {
            (answer["chain"][answer["index"]], answer["answer"])
            for answer in record["answers"]
        } == {
            ("Hesse", run.qid("Hesse")),
            ("Germany", run.qid("Germany")),
            ("Kreis Peiltal", run.qid("Kreis Peiltal")),
        }
        assert len(record["planned"]) == 3
        kept = json.loads(
            (
                run.run_dir / CORPUS_DIR / "places" / "responses.json"
            ).read_text()
        )
        assert kept["key"] == record["key"]
        assert kept["responses"] and all(
            response.endswith('{"done": true}')
            for response in kept["responses"]
        )

    def test_a_reset_removes_the_raw_answers(self, run, monkeypatch):
        """The raw answers are kept for one reset of the instance: after a
        reset whose write fails, no answers of the reset before are left."""
        from yizkr.pipeline import writer

        run.past_the_barrier()
        kept = run.run_dir / CORPUS_DIR / "places" / "responses.json"
        assert (
            json.loads(kept.read_text())["epoch"]
            == run.record("places")["epoch"]
        )
        # another session writes to the instance: the next invocation
        # resets it, and the letter's write then fails
        run.wiki.item("Brunnenring 35")

        def write_batch(self, batch, **kwargs):
            raise RuntimeError("the write failed")

        monkeypatch.setattr(writer.BatchWriter, "write_batch", write_batch)
        for name in ("reset", "register", "write", "places"):
            run.stage(name)
        assert run.record("reset")["action"] == scratchwrite.RESET_DONE
        assert not kept.exists()
        assert run.record("places") == {}

    def test_the_site_waits_for_the_place_stage_s_revision(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="places")
        placed = run.record("places")["revision"]
        assert placed > run.record("write")["revision"]
        # the query service shows the write, but not the containment
        run.newest = placed - 1
        monkeypatch.setattr(scratchwrite, "sleep", run.advance)
        with pytest.raises(ScratchWriteRefused, match="site was not built"):
            run.stage("site")
        assert run.sites == []


# -- refusals and counts ------------------------------------------------------


def with_parents(monkeypatch, run, **parents) -> None:
    """The graph read also puts each place named in the places named."""

    def read(wiki):
        found = graph(wiki)
        for record in found:
            for child, names in parents.items():
                if record.label == child.replace("_", " "):
                    record.parents = [run.qid(name) for name in names]
        return found

    monkeypatch.setattr(scratchwrite, "place_graph", read)


class TestRefusedAndCounted:
    def test_a_cycle_through_the_graph_is_refused_counted_and_named(
        self, tmp_path, monkeypatch
    ):
        """The graph read puts Hesse in Kreis Peiltal: the extract's link
        Kreis Peiltal within Hesse would close a cycle. It is refused,
        counted, and named in the local run report."""
        run = PlaceRun(tmp_path, monkeypatch)
        with_parents(monkeypatch, run, Hesse=["Kreis Peiltal"])
        run.past_the_barrier(until="write")
        attention = run.stage("places")
        run.stage("site")
        record = run.record("places")
        assert record["counts"]["cycles"] == 1
        assert attention[0].startswith("1 place links refused")
        assert item_values(run.wiki, run.qid("Kreis Peiltal"), "part_of") == []
        (refused,) = record["refused"]
        assert (refused["child_label"], refused["parent_label"]) == (
            "Kreis Peiltal",
            "Hesse",
        )
        assert refused["sha1"] == sha1(EXTRACT)
        named = report._place_cycles(
            report._place_stage(run.run_dir),
            {sha1(EXTRACT): report.Document(sha1=sha1(EXTRACT), kind="media",
                                            files=["extract.png"])},
        )  # fmt: skip
        assert named == [
            {
                "place": "Kreis Peiltal",
                "container": "Hesse",
                "document": f"{sha1(EXTRACT)[:12]} extract.png",
            }
        ]

    def test_a_cycle_through_the_graph_and_the_batch_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The graph read puts Germany in Fläckfeld. The batch's links
        Kreis Peiltal within Hesse, Hesse within Germany and Fläckfeld
        within Kreis Peiltal close a cycle through both: the last of them
        planned is refused."""
        run = PlaceRun(tmp_path, monkeypatch)
        with_parents(monkeypatch, run, Germany=["Fläckfeld"])
        run.past_the_barrier()
        assert run.record("places")["counts"]["cycles"] == 1
        assert run.record("places")["counts"]["written"] == 2

    def test_a_container_naming_no_place_is_left_out(
        self, tmp_path, monkeypatch
    ):
        letter = letter_batch()
        letter.entities[1].structure.within = ["Kreis Rekau"]
        run = PlaceRun(
            tmp_path,
            monkeypatch,
            documents=(
                ("leopold-birth-extract.png", EXTRACT, extract_batch()),
                ("gustav-letter-1952.png", LETTER, letter),
            ),
        )
        run.past_the_barrier()
        (prompt,) = run.engine.prompts
        assert "Kreis Rekau" not in prompt
        counts = run.record("places")["counts"]
        assert counts["unnamed"] == 1
        assert counts["questions"] == 2

    def test_conflicts_and_schema_rejections_are_counted(
        self, tmp_path, monkeypatch
    ):
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.extra = [
            # a second, other answer to the first question: a conflict
            json.dumps({"question": 1, "place": "uncertain"}),
            # no question asked, and a containment verdict
            json.dumps({"question": 99, "place": 1}),
            json.dumps({"question": 2, "place": "a_within_b"}),
        ]
        run.past_the_barrier()
        record = run.record("places")
        counts = record["counts"]
        assert counts["conflicts"] == 1
        assert counts["schema_rejected"] == 2
        # the conflicting question was refused whole: two answers left
        assert len(record["answers"]) == 2

    def test_the_counts(self, run):
        run.past_the_barrier()
        assert run.record("places")["counts"] == {
            "questions": 3,
            "unnamed": 0,
            "calls": 1,
            "answered": 3,
            "uncertain": 0,
            "written": 3,
            "referenced": 0,
            "present": 0,
            "record_only": 3,
            "record_only_written": 3,
            "unresolved": 0,
            "schema_rejected": 0,
            "cycles": 0,
            "conflicts": 0,
            "unanswered": 0,
        }

    def test_the_report_and_the_diff_count_them(self, tmp_path, monkeypatch):
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.extra = [json.dumps({"question": 99, "place": 1})]
        run.past_the_barrier()
        placed = report._place_stage(run.run_dir)
        content = report._aggregate_content(
            {}, {}, {}, _vocabulary(), run.record("write"), placed
        )
        shown = content["place_hierarchy"]
        assert shown["links_written"] == 3
        assert shown["record_only_links"] == 3
        assert shown["record_only_links_written"] == 3
        assert shown["rejected_by_schema"] == 1
        assert shown["refused_cycles"] == shown["refused_conflicts"] == 0
        assert shown["contents_recorded"] is True
        (section,) = [
            s for s in report._write_sections(content) if s.title == (
                "Place hierarchy"
            )
        ]  # fmt: skip
        text = "\n".join(section.blocks[0])
        assert "Links a record states and no claim of its document " in text
        assert "gives: 3, of them 3 written, with no page" in text
        # the diff sets them side by side, as figures, and diff.md shows
        # them in a section of their own
        before = {"content": {"place_hierarchy": dict(shown, links_written=1)}}
        after = {"content": {"place_hierarchy": shown}}
        rows = {row["figure"]: row for row in rundiff.figures(before, after)}
        written = rows["content.place_hierarchy.links_written"]
        assert (written["baseline"], written["run"], written["delta"]) == (
            1,
            3,
            2,
        )
        assert "content.place_hierarchy.record_only_links" in rows
        assert "content.place_hierarchy.refused_cycles" in rows


# -- the cache, the key and the contents record -------------------------------


def rerun(tmp_path, monkeypatch, documents=None, next_qid=94, read=graph):
    """A run, then another of the same batches after a reset that mints
    the items from `next_qid` (Q94: some below Q100 and some above) and
    reads the places in reverse order, through the first run's LLM cache:
    (first, second)."""
    first = PlaceRun(tmp_path, monkeypatch, documents=documents)
    monkeypatch.setattr(scratchwrite, "place_graph", read)
    first.past_the_barrier()
    # a write the stage did not make voids the record; the next run
    # resets, and the instance mints its items afresh
    clear_contents(CONFIG, "a live test")
    (tmp_path / "again").mkdir()
    second = PlaceRun(
        tmp_path / "again",
        monkeypatch,
        wiki=first.wiki,
        run_id="run-2",
        documents=documents,
    )
    second.wiki._next_qid = next_qid
    second.engine.choices = first.engine.choices
    monkeypatch.setattr(
        scratchwrite, "place_graph", lambda wiki: list(reversed(read(wiki)))
    )
    # the one LLM cache both runs call through
    second.driver.cache_dir = first.driver.cache_dir
    second.past_the_barrier()
    return first, second


class TestKeys:
    def test_the_run_s_events_name_the_calls_places(self, run):
        """Keyed as the extract role's calls are, the place stage's calls
        are named apart in the run's events, so the report counts them
        apart from the extraction's."""
        run.past_the_barrier()
        events = run.driver.rundir.events()
        calls = [e for e in events if e["event"] in ("call", "cache-miss")]
        assert calls
        assert {e.get("role") for e in calls} == {"places"}
        assert {e.get("stage") for e in calls if "stage" in e} <= {"places"}

    def test_the_report_counts_the_calls_under_places(self, run):
        run.past_the_barrier()
        fold = report._Fold()
        for event in run.driver.rundir.events():
            fold.fold(event)
        calls = report._aggregate_calls(fold, _vocabulary())
        assert set(calls["by_role"]) == {"places"}
        assert calls["by_role"]["places"]["engine"] == 1
        assert calls["engine_calls"]["by_role"] == {"places": 1}

    def test_a_place_is_named_by_its_records_chains(self, run):
        run.past_the_barrier()
        (prompt,) = run.engine.prompts
        assert "'Fläckfeld' (" in prompt
        assert "a record places it in 'Kreis Peiltal')" in prompt

    def test_no_prompt_names_a_qid(self, run):
        run.past_the_barrier()
        (prompt,) = run.engine.prompts
        assert not re.search(r"\bQ\d+\b", prompt)

    def test_a_run_after_another_reset_asks_the_engine_nothing(
        self, tmp_path, monkeypatch
    ):
        """The same batches after a reset make the same prompts, which
        the LLM cache answers: the items' QIDs are no part of them."""
        first, second = rerun(tmp_path, monkeypatch)
        assert len(first.engine.prompts) == 1
        assert second.resets == 1
        assert second.engine.prompts == []
        counts = second.record("places")["counts"]
        assert counts["written"] == 3
        assert counts == first.record("places")["counts"]

    def test_namesakes_ask_the_same_after_another_reset(
        self, tmp_path, monkeypatch
    ):
        """Two Kaldovsky Districts, told apart by their descriptions: the
        prompts name them by them, so they are the same after a reset that
        renumbers them and gives them in another order (decision FD)."""
        held = {"qid": "Q900"}

        def read(wiki):
            if held["qid"] not in wiki.entities:
                namesake(wiki, held["qid"], "Kaldovsky District", "a district")
            return graph(wiki)

        def swap(wiki):
            held["qid"] = "Q20"
            return read(wiki)

        first = PlaceRun(tmp_path, monkeypatch, documents=DEATH_DOCUMENTS)
        monkeypatch.setattr(scratchwrite, "place_graph", read)
        first.engine.choices = {"Kaldovsky District": "a district"}
        first.past_the_barrier()
        (prompt,) = first.engine.prompts
        assert "place 1: 'Kaldovsky District' (a district)\n" in prompt
        assert (
            "place 2: 'Kaldovsky District' (place named in an artifact; "
            "within 'Chadsk Oblast')" in prompt
        )
        clear_contents(CONFIG, "a live test")
        (tmp_path / "again").mkdir()
        second = PlaceRun(
            tmp_path / "again",
            monkeypatch,
            wiki=first.wiki,
            run_id="run-2",
            documents=DEATH_DOCUMENTS,
        )
        second.wiki._next_qid = 94
        second.engine.choices = first.engine.choices
        monkeypatch.setattr(
            scratchwrite, "place_graph", lambda w: list(reversed(swap(w)))
        )
        second.driver.cache_dir = first.driver.cache_dir
        second.past_the_barrier()
        assert second.engine.prompts == []
        (statement,) = stage_written(second.wiki, second.qid("Koshnovka"))
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == "Q20"

    def test_the_key_is_the_write_key_with_the_prompt_and_the_model(
        self, run, monkeypatch
    ):
        run.past_the_barrier()
        context = run.context("places")
        write = run.record("write")["key"]
        key = run.record("places")["key"]
        assert key == scratchwrite.place_key(context, write)
        assert key != write
        monkeypatch.setattr(
            places, "ADJUDICATION_PROMPT", places.ADJUDICATION_PROMPT + " "
        )
        assert scratchwrite.place_key(context, write) != key
        monkeypatch.undo()
        monkeypatch.setenv("YIZKR_LLM_MODEL", "claude-opus-5")
        assert scratchwrite.place_key(context, write) != key
        monkeypatch.undo()
        monkeypatch.setenv("YIZKR_LLM_MODEL", MODEL)
        assert scratchwrite.place_key(context, write) == key
        driver = run.driver
        for owner, name, other in (
            (scratchwrite, "PLACE_VERSION", scratchwrite.PLACE_VERSION + 1),
            (places, "BATCH_QUESTIONS", places.BATCH_QUESTIONS + 1),
            (driver, "extract_effort", "max"),
            (driver, "llm", "anthropic"),
        ):
            with monkeypatch.context() as changed:
                assert getattr(owner, name) != other
                changed.setattr(owner, name, other)
                assert scratchwrite.place_key(context, write) != key, name
        # the critique's effort is no part of it
        with monkeypatch.context() as changed:
            assert driver.review_effort != "max"
            changed.setattr(driver, "review_effort", "max")
            assert scratchwrite.place_key(context, write) == key

    def test_the_instance_contents_take_the_place_key(self, run):
        run.past_the_barrier()
        recorded = recorded_contents(run.wiki)
        assert recorded.key == run.record("places")["key"]
        assert run.record("places")["recorded"] is True

    def test_a_rerun_of_the_run_adds_nothing(self, run):
        run.past_the_barrier()
        writes, counts = run.wiki.writes, run.record("places")["counts"]
        run.past_the_barrier()
        assert run.resets == 1
        assert run.record("reset")["action"] == scratchwrite.SKIPPED
        assert run.wiki.writes == writes
        assert len(run.engine.prompts) == 1
        # this run made what the instance holds: its counts stand
        assert run.record("places")["counts"] == counts

    def test_without_a_place_stage_the_write_key_is_recorded(self, run):
        run.driver.corpus_stages = tuple(
            stage
            for stage in run.driver.corpus_stages
            if stage.name != "places"
        )
        run.past_the_barrier(until="write")
        assert recorded_contents(run.wiki).key == run.record("write")["key"]


# -- the whole write --------------------------------------------------------


class TestWholeWrite:
    def test_the_stage_waits_for_a_complete_write(self, run, monkeypatch):
        """The letter's write fails, then succeeds on the next invocation:
        the place stage asks nothing over the part written, and then
        asks every question of the whole write (decision FB)."""
        from yizkr.pipeline import writer

        real = writer.BatchWriter.write_batch
        failed = []

        def write_batch(self, batch, **kwargs):
            if not failed and any(
                e.label == "Fläckfeld" for e in batch.entities
            ):
                failed.append(True)
                raise RuntimeError("the letter's write failed")
            return real(self, batch, **kwargs)

        monkeypatch.setattr(writer.BatchWriter, "write_batch", write_batch)
        run.past_the_barrier(until="write")
        assert run.record("write").get("recorded") is None
        attention = run.stage("places")
        assert attention == [
            "the write is not complete: no place was reconciled"
        ]
        assert run.engine.prompts == []
        assert run.record("places") == {}
        # the next invocation: the letter is written, and every question
        # of the whole write is asked
        for name in ("reset", "register", "write", "places"):
            run.stage(name)
        assert run.record("write")["recorded"] is True
        (prompt,) = run.engine.prompts
        assert "'Fläckfeld'" in prompt
        assert len(QUESTION.findall(prompt)) == 3
        assert run.record("places")["counts"]["questions"] == 3


# -- a malformed answer the cache kept --------------------------------------


class TestKeptAnswer:
    def test_a_malformed_engine_answer_does_not_block_the_next_invocation(
        self, run
    ):
        """The engine's answer lacks its terminal sentinel: the stage
        fails, and the answer is rejected from the LLM cache. The next
        invocation asks the engine afresh and takes its answer."""
        run.past_the_barrier(until="write")
        run.engine.broken = True
        with pytest.raises(ValueError, match="sentinel"):
            run.stage("places")
        assert len(run.engine.prompts) == 1
        events = [e["event"] for e in run.driver.rundir.events()]
        assert events.count("cache-miss") == 1
        assert "cache-hit" not in events
        run.engine.broken = False
        run.stage("places")
        assert len(run.engine.prompts) == 2
        assert run.record("places")["counts"]["written"] == 3

    @staticmethod
    def kept_malformed_answer(run) -> int:
        """Leave a malformed answer in the LLM cache, as an earlier
        invocation whose rejection was lost would, and return the event
        count after it."""
        run.past_the_barrier(until="write")
        run.engine.broken = True
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                places, "reject_response", lambda client, response, why: False
            )
            with pytest.raises(ValueError, match="sentinel"):
                run.stage("places")
        return len(run.driver.rundir.events())

    @staticmethod
    def cache_events(run, start: int) -> list[str]:
        return [
            e["event"]
            for e in run.driver.rundir.events()[start:]
            if e["event"]
            in ("cache-miss", "cache-hit", "call", "cache-reject")
        ]

    def test_a_kept_malformed_answer_is_asked_again_at_once(self, run):
        """The cache answers with the malformed answer it kept: it is
        rejected, and the same call asked of the engine at once."""
        start = self.kept_malformed_answer(run)
        run.engine.broken = False
        run.stage("places")
        assert self.cache_events(run, start) == [
            "cache-hit",
            "cache-reject",
            "cache-miss",
            "call",
        ]
        assert len(run.engine.prompts) == 2
        assert run.record("places")["counts"]["written"] == 3

    def test_a_malformed_retry_fails_and_is_not_kept(self, run):
        start = self.kept_malformed_answer(run)
        with pytest.raises(ValueError, match="sentinel"):
            run.stage("places")
        assert len(run.engine.prompts) == 2
        assert "planned" not in run.record("places")
        again = len(run.driver.rundir.events())
        run.engine.broken = False
        run.stage("places")
        assert self.cache_events(run, again)[:2] == ["cache-miss", "call"]
        assert start < again

    def test_a_retry_cut_off_at_the_output_limit_fails(self, run):
        self.kept_malformed_answer(run)
        run.engine.broken = False
        run.engine.last_report = lambda: {"stop_reason": "max_tokens"}
        with pytest.raises(ValueError, match="cut off"):
            run.stage("places")
        assert "planned" not in run.record("places")


# -- the guard ----------------------------------------------------------------


class TestGuard:
    def test_a_write_to_the_instance_since_the_write_is_refused(self, run):
        run.past_the_barrier(until="write")
        run.wiki.item("Brunnenring 35")
        before = run.wiki.writes
        with pytest.raises(ScratchWriteRefused, match="written to since"):
            run.stage("places")
        assert run.wiki.writes == before
        assert run.engine.prompts == []

    def test_no_write_stage_for_this_reset_is_refused(self, run):
        run.past_the_barrier(until="register")
        with pytest.raises(ScratchWriteRefused, match="write stage"):
            run.stage("places")
        assert run.engine.prompts == []

    def test_it_holds_the_lock_while_it_reads_calls_and_writes(self, run):
        run.past_the_barrier(until="write")
        locks = run.locks
        run.stage("places")
        # one hold for the whole stage; the engine checks it is held
        assert run.locks == locks + 1
        assert run.engine.prompts

    def test_it_waits_for_the_query_service_to_show_the_write(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        writes = run.wiki.writes
        run.newest = run.record("write")["revision"] - 1
        monkeypatch.setattr(scratchwrite, "sleep", run.advance)
        with pytest.raises(
            ScratchWriteRefused, match="no place was reconciled"
        ):
            run.stage("places")
        assert run.engine.prompts == []
        assert "planned" not in run.record("places")
        assert run.wiki.writes == writes

    def test_a_link_whose_document_has_no_artifact_writes_nothing(self, run):
        run.past_the_barrier(until="write")
        registered = run.run_dir / CORPUS_DIR / "register" / "record.json"
        record = json.loads(registered.read_text())
        record["documents"].pop(sha1(LETTER))
        registered.write_text(json.dumps(record))
        writes = run.wiki.writes
        with pytest.raises(
            ScratchWriteRefused, match="no artifact is registered"
        ):
            run.stage("places")
        assert run.wiki.writes == writes
        assert place_run_items(run.wiki) == []

    def test_a_stage_with_nothing_planned_records_the_place_key(
        self, tmp_path, monkeypatch
    ):
        """The certificate's links are all its claims', each to a
        container that names one place: nothing is asked or written, and
        the instance holds what the place key makes."""
        run = PlaceRun(tmp_path, monkeypatch, documents=DEATH_DOCUMENTS)
        run.past_the_barrier()
        record = run.record("places")
        assert record["planned"] == [] and run.engine.prompts == []
        assert recorded_contents(run.wiki).key == record["key"]

    def test_the_contents_record_is_cleared_before_the_first_write(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        assert recorded_contents(run.wiki).key == run.record("write")["key"]
        seen = []
        real = places.ensure_places_run_item

        def ensure(*args, **kwargs):
            seen.append(recorded_contents(run.wiki).key)
            return real(*args, **kwargs)

        monkeypatch.setattr(places, "ensure_places_run_item", ensure)
        run.stage("places")
        assert seen == [None]


# -- pauses -------------------------------------------------------------------


class TestPauses:
    def test_a_subscription_limit_pauses_it_with_nothing_written(self, run):
        run.past_the_barrier(until="write")
        writes = run.wiki.writes
        run.engine.pause = True
        with pytest.raises(UsageLimitReached):
            run.stage("places")
        assert run.wiki.writes == writes
        assert "planned" not in run.record("places")
        # the next invocation asks and writes
        run.stage("places")
        assert run.record("places")["counts"]["written"] == 3

    def test_a_stage_cut_short_mid_way_resumes_where_it_stopped(
        self, run, monkeypatch
    ):
        """Cut short as its second link is written (Ctrl-C, say), the run
        is invoked again: the reset finds the instance holding this run's
        writes alone and goes on, the write records no key over the link
        written, and the place stage writes the rest of what it planned
        without reading the graph or asking again."""
        run.past_the_barrier(until="write")
        real = places.write_link
        made = []

        def write_link(*args, **kwargs):
            made.append(args)
            if len(made) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        monkeypatch.setattr(places, "write_link", write_link)
        with pytest.raises(KeyboardInterrupt):
            run.stage("places")
        assert len(run.record("places")["outcomes"]) == 1
        planned = run.record("places")["planned"]
        read = []
        monkeypatch.setattr(
            scratchwrite,
            "place_graph",
            lambda wiki: read.append(1) or graph(wiki),
        )
        # the next invocation
        for name in ("reset", "register", "write"):
            run.stage(name)
        assert run.record("reset")["action"] == scratchwrite.RESUMED
        # the instance holds more than the write key makes
        assert recorded_contents(run.wiki).key is None
        run.stage("places")
        record = run.record("places")
        assert record["counts"]["written"] == 3
        assert record["planned"] == planned
        assert len(run.engine.prompts) == 1
        assert read == []
        assert recorded_contents(run.wiki).key == record["key"]

    def test_a_stage_cut_short_after_asking_asks_nothing_again(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        real = places.ensure_places_run_item

        def failing(*args, **kwargs):
            raise RuntimeError("the run item's write failed")

        monkeypatch.setattr(places, "ensure_places_run_item", failing)
        with pytest.raises(RuntimeError):
            run.stage("places")
        assert len(run.engine.prompts) == 1
        monkeypatch.setattr(places, "ensure_places_run_item", real)
        run.stage("places")
        assert len(run.engine.prompts) == 1
        assert run.record("places")["counts"]["written"] == 3

    def test_a_limit_met_while_asking_asks_again_from_the_cache(
        self, run, monkeypatch
    ):
        """In batches of one question, a subscription limit meets the
        second batch: nothing is recorded. The next invocation reads the
        graph and asks again; the first batch comes from the LLM cache,
        and the engine is asked only the batches it had not answered."""
        monkeypatch.setattr(
            places,
            "adjudicate",
            functools.partial(places.adjudicate, batch_questions=1),
        )
        run.past_the_barrier(until="write")
        real = run.engine.complete
        calls = []

        def complete(prompt, image_paths=()):
            calls.append(prompt)
            if len(calls) == 2:
                run.engine.pause = True
            return real(prompt, image_paths)

        run.engine.complete = complete
        with pytest.raises(UsageLimitReached):
            run.stage("places")
        assert "planned" not in run.record("places")
        answered = list(run.engine.prompts)
        assert len(answered) == 1
        run.stage("places")
        record = run.record("places")
        assert record["counts"]["written"] == 3
        assert record["counts"]["calls"] == 3
        # the first batch came from the cache: the engine answered each
        # batch once, over the two invocations
        assert run.engine.prompts[0] == answered[0]
        assert len(run.engine.prompts) == 3


# -- the live test's scenario, offline ---------------------------------------


class TestTheLiveScenario:
    """tests/integration/test_live_scratch_places.py's documents and fake,
    through the in-memory instance and the real clustering and writer: a
    namesake the manifest kept apart is asked of and named with the
    claim's time, a name the town and the district answer to is left
    uncertain, a record's link is written with no time, and a cycle is
    refused."""

    def test_it_asks_and_writes_as_the_live_test_says(
        self, tmp_path, monkeypatch
    ):
        from tests.integration import test_live_scratch_places as live

        run = PlaceRun(tmp_path, monkeypatch, documents=live.documents())
        engine = live.Adjudicator()
        run.driver._engines[scratchwrite.PLACE_ROLE] = engine
        run.past_the_barrier(until="write")
        documents = run.record("write")["documents"]

        def created(index: int, local_id: str) -> str:
            found = documents[run.documents[index].sha1]
            return found["created_items"][local_id]

        def description(qid: str) -> str:
            return run.wiki.entities[qid]["descriptions"]["en"]["value"]

        namesake = created(1, "d2")
        assert description(namesake) != description(created(1, "d"))
        engine.choices = {
            live.KALDOVSKY: f"({description(namesake)};",
            live.PEILTAL: places.UNCERTAIN,
        }
        run.stage("places")
        record = run.record("places")
        (run_item,) = place_run_items(run.wiki)
        (statement,) = stage_written(run.wiki, created(0, "m"))
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == created(
            0, "h"
        )
        assert qualifiers(statement) == {}
        (statement,) = stage_written(run.wiki, created(1, "k"))
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == namesake
        assert qualifiers(statement) == {"point_in_time": 1941}
        assert stage_written(run.wiki, created(2, "p")) == []
        assert containment(run.wiki, created(3, "s")) == []
        (refused,) = record["refused"]
        assert (refused["child"], refused["parent"]) == (
            created(3, "s"),
            created(3, "r"),
        )
        counts = record["counts"]
        assert (
            counts["written"],
            counts["record_only"],
            counts["record_only_written"],
            counts["uncertain"],
            counts["cycles"],
        ) == (2, 2, 1, 1, 1)
        assert run_item == record["run_qid"]


# -- the run report and the run diff ------------------------------------------


def report_of(run) -> dict:
    """The run report of a unit run, its plan written as run.json."""
    (run.run_dir / "run.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "sha1": doc.sha1,
                        "file": doc.source.name,
                        "kind": "media",
                        "stages": [],
                    }
                    for doc in run.documents
                ]
            }
        )
    )
    return report.build(run.run_dir)


class TestReports:
    def test_a_refused_cycle_is_named_in_the_local_report_alone(
        self, tmp_path, monkeypatch
    ):
        run = PlaceRun(tmp_path, monkeypatch)
        with_parents(monkeypatch, run, Hesse=["Kreis Peiltal"])
        run.past_the_barrier()
        built = report_of(run)
        assert built["place_cycles"] == [
            {
                "place": "Kreis Peiltal",
                "container": "Hesse",
                "document": f"{sha1(EXTRACT)[:12]} leopold-birth-extract.png",
            }
        ]
        html = report.render_html(built)
        assert "Place links refused as cycles" in html
        assert "Kreis Peiltal within Hesse" in html
        markdown = report.render_markdown(built["aggregate"])
        assert "1 links that would close a cycle" in markdown
        for label in ("Kreis Peiltal", "Hesse", "Flörburg"):
            assert label not in markdown

    def test_the_contents_key_not_recorded_is_said(self):
        shown = report._aggregate_places({"counts": {}})
        assert shown["contents_recorded"] is False

    def test_diff_md_sets_the_counts_side_by_side(self, run):
        run.past_the_barrier()
        report_of(run)
        summary = rundiff.render_markdown(
            rundiff.build(run.run_dir, run.run_dir)["aggregate"]
        )
        section = summary.split("## Place hierarchy")[1].split("\n## ")[0]
        assert "| links_written | 3 | 3 | 0 |" in section
        assert "| record_only_links | 3 | 3 | 0 |" in section
        assert "| refused_cycles | 0 | 0 | 0 |" in section

    def test_diff_md_says_when_neither_run_counted(self, run):
        run.past_the_barrier(until="write")
        report_of(run)
        summary = rundiff.render_markdown(
            rundiff.build(run.run_dir, run.run_dir)["aggregate"]
        )
        section = summary.split("## Place hierarchy")[1].split("\n## ")[0]
        assert (
            "Not available: neither run's place stage has counted its "
            "questions." in section
        )
