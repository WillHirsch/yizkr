"""Offline tests for a scratch run's place stage (#201; ADR-0009,
ADR-0025 §1): the place-hierarchy reconciliation, run unattended after
the write and before the site (yizkr.pipeline.scratchwrite.run_places),
on the unit tests' in-memory instance, with a fake engine behind the
run's LLM cache, and no stack.

* The driver runs it after the write and before the site; it touches the
  wiki and is the write side's one stage that calls the engine.
* Each containment proposal is applied at normal rank, with no review
  CSV: `stated in` each document whose mention record states it, where
  one does, and `inferred from` the stage's run item, which names the
  proposing model and is marked an unratified scratch run's.
* Cycles, conflicting answers and answers outside the schema are refused
  and counted, never written; the run report and the run diff count them.
* Every call goes through the LLM cache, and no prompt names a QID: a run
  on the same batches after another reset asks the engine nothing.
* The stage key is the write key with the prompt and the model; the
  instance-contents record takes it once the stage has applied every
  proposal. The stage refuses, writing nothing, unless this run's reset
  and write left the instance, and it holds the live-stack lock while it
  reads, calls and writes.
* A subscription limit pauses it; the next invocation applies what was
  proposed and asks nothing again.

Every document is fictional (docs/EXAMPLES.md): Leopold Löbig's birth
register extract, which places Flörburg in Kreis Peiltal, Hesse, Germany,
and a letter from his brother Gustav, which says Leopold was born at
Fläckfeld.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

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
from yizkr.pipeline.places import Question
from yizkr.pipeline.scratch import CORPUS_DIR
from yizkr.pipeline.scratchwrite import ScratchWriteRefused

# the driver's corpus stages keep the write side, the place stage among it
pytestmark = pytest.mark.write_side

EXTRACT = b"\x89PNG synthetic scan of Leopold's 1914 birth register extract"
LETTER = b"\x89PNG synthetic scan of Gustav's 1952 letter to Leopold"
MODEL = "claude-sonnet-5"


class PlaceWiki(Wiki):
    """The write side's in-memory instance, with the place vocabulary."""

    PROPERTIES = {**Wiki.PROPERTIES, "place_of_birth": "P76"}
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


def graph(wiki) -> tuple[list, list]:
    """What the query service would give of the in-memory instance: its
    places with the places it puts each in, and each person's place of
    birth values the graph's containment does not connect."""
    place = PlaceWiki.ITEMS["place"]
    found = [
        places.PlaceRecord(
            qid=qid,
            label=label(wiki, qid),
            parents=item_values(wiki, qid, "part_of"),
        )
        for qid in sorted(wiki.entities, key=lambda q: int(q[1:]))
        if place in item_values(wiki, qid, "instance_of")
    ]
    ancestors = places._ancestors(found)
    competing = []
    for qid in sorted(wiki.entities, key=lambda q: int(q[1:])):
        values = sorted(set(item_values(wiki, qid, "place_of_birth")))
        for index, a in enumerate(values):
            for b in values[index + 1 :]:
                if a in ancestors.get(b, ()) or b in ancestors.get(a, ()):
                    continue
                competing.append(
                    Question(
                        kind="competing",
                        qid_a=a,
                        qid_b=b,
                        context=f"both asserted as place of birth of "
                        f"{label(wiki, qid)}",
                    )
                )
    return found, competing


QUESTION = re.compile(r"^(\d+)\. A: '([^']*)'.*?  vs  B: '([^']*)'", re.M)


MIRRORED = {"a_within_b": "b_within_a", "b_within_a": "a_within_b"}


def verdict(verdicts: dict, a: str, b: str) -> str:
    """The verdict on A against B, by their labels: as given, else the
    mirror of the verdict given on B against A, else `uncertain`."""
    if (a, b) in verdicts:
        return verdicts[(a, b)]
    found = verdicts.get((b, a), "uncertain")
    return MIRRORED.get(found, found)


class Adjudicator:
    """A fake engine that answers each question of a place prompt by the
    labels of its two places (`verdicts`, by (label A, label B)), with
    `extra` lines besides, and records each prompt it was given. While
    `held` says so, it checks the live-stack lock is held as it answers.
    `pause` makes its next call a subscription limit's."""

    model = MODEL
    model_identifier = MODEL
    effort = "high"
    config_hash = "0" * 64
    cli_version = None

    def __init__(self, run, verdicts=None, extra=()):
        self.run = run
        self.verdicts = verdicts or {}
        self.extra = list(extra)
        self.prompts: list[str] = []
        self.pause = False
        # an answer without its terminal sentinel while set
        self.broken = False

    def complete(self, prompt, image_paths=()):
        assert self.run.held, "the engine was called without the lock"
        if self.pause:
            self.pause = False
            raise UsageLimitReached(
                "the five-hour window is used up",
                resumes_at=datetime(2026, 10, 6, 15, tzinfo=UTC),
            )
        self.prompts.append(prompt)
        lines = [
            json.dumps(
                {
                    "question": int(number),
                    "verdict": verdict(self.verdicts, a, b),
                    "confidence": 0.9,
                    "evidence": "fictional",
                }
            )
            for number, a, b in QUESTION.findall(prompt)
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
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1(data)},
            "page_number": "1",
        },
        "confidence": 0.9,
    }


def entity(local_id: str, name: str, within=None) -> dict:
    found = {"id": local_id, "label": name, "kind": "place"}
    if within is not None:
        found["structure"] = {"kind": "place", "within": list(within)}
    return found


def extract_batch() -> ClaimBatch:
    """The extract: Leopold born at Flörburg, which its record puts in
    Kreis Peiltal, Hesse, Germany; and those three places."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {"id": "leopold", "label": "Leopold Löbig", "kind": "human"},
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
                {"id": "leopold", "label": "Leopold Löbig", "kind": "human"},
                entity("flackfeld", "Fläckfeld", ["Kreis Peiltal"]),
            ],
            "claims": [born_at(LETTER, place("flackfeld", "Fläckfeld"))],
        }
    )


# the adjudicator's verdicts: the record's chain, link by link, and the
# letter's place set apart from the extract's
CHAIN = {
    ("Flörburg", "Kreis Peiltal"): "a_within_b",
    ("Kreis Peiltal", "Hesse"): "a_within_b",
    ("Hesse", "Germany"): "a_within_b",
    ("Fläckfeld", "Flörburg"): "distinct",
}


class PlaceRun(Run):
    """A scratch run of the two documents, with the fake engine in the
    place stage's role, behind the run's LLM cache, and the graph read
    from the in-memory instance."""

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
        self.engine = Adjudicator(self, dict(CHAIN))
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


def competing(run, monkeypatch, a: str, b: str) -> None:
    """The graph also holds the places `a` and `b` as competing values of
    one fact, which no record nests."""

    def read(wiki):
        found, pairs = graph(wiki)
        pair = Question(
            kind="competing",
            qid_a=run.qid(a),
            qid_b=run.qid(b),
            context="both asserted as place of birth of Leopold Löbig",
        )
        return found, [*pairs, pair]

    monkeypatch.setattr(scratchwrite, "place_graph", read)


def containment(wiki, qid: str) -> list[dict]:
    return statements(wiki, qid, "part_of")


def place_run_items(wiki) -> list[str]:
    return [
        qid
        for qid in wiki.entities
        if label(wiki, qid).startswith("place-hierarchy run ")
    ]


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


class TestApplied:
    def test_each_link_of_a_record_s_chain_is_applied(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        florburg, kreis, hesse, germany = (
            run.qid(name)
            for name in ("Flörburg", "Kreis Peiltal", "Hesse", "Germany")
        )
        assert item_values(wiki, florburg, "part_of") == [kreis]
        assert item_values(wiki, kreis, "part_of") == [hesse]
        assert item_values(wiki, hesse, "part_of") == [germany]
        # never the shortcut, and nothing of the place set apart
        assert item_values(wiki, run.qid("Fläckfeld"), "part_of") == []
        assert item_values(wiki, germany, "part_of") == []

    def test_dual_provenance_at_normal_rank(self, run):
        run.past_the_barrier()
        wiki = run.wiki
        (run_item,) = place_run_items(wiki)
        extract = run.artifact(EXTRACT)
        for name in ("Flörburg", "Kreis Peiltal", "Hesse"):
            (statement,) = containment(wiki, run.qid(name))
            assert statement.get("rank", "normal") == "normal"
            # stated in the extract, whose record states the chain, and
            # inferred from the stage's run item, each a block of its own
            assert cited(statement, "stated_in") == [extract]
            assert cited(statement, "inferred_from_extraction_run") == [
                run_item
            ]
            assert len(statement["references"]) == 2

    def test_a_containment_no_record_states_cites_the_run_alone(
        self, tmp_path, monkeypatch
    ):
        """A competing pair's containment rests on no document: it is
        inferred from the run item alone."""
        run = PlaceRun(tmp_path, monkeypatch)
        competing(run, monkeypatch, "Fläckfeld", "Flörburg")
        run.engine.verdicts[("Fläckfeld", "Flörburg")] = "a_within_b"
        run.past_the_barrier()
        wiki = run.wiki
        (run_item,) = place_run_items(wiki)
        (statement,) = containment(wiki, run.qid("Fläckfeld"))
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == run.qid(
            "Flörburg"
        )
        assert cited(statement, "stated_in") == []
        assert cited(statement, "inferred_from_extraction_run") == [run_item]

    def test_a_reversed_verdict_is_not_stated_in_the_record(
        self, tmp_path, monkeypatch
    ):
        """The record states Flörburg within Kreis Peiltal; a verdict that
        puts Kreis Peiltal within Flörburg rests on no document."""
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.verdicts[("Flörburg", "Kreis Peiltal")] = "b_within_a"
        run.past_the_barrier()
        (statement,) = [
            statement
            for statement in containment(run.wiki, run.qid("Kreis Peiltal"))
            if statement["mainsnak"]["datavalue"]["value"]["id"]
            == run.qid("Flörburg")
        ]
        assert cited(statement, "stated_in") == []

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

    def test_proposals_and_raw_answers_are_kept_in_the_stage_store(self, run):
        run.past_the_barrier()
        record = run.record("places")
        assert {
            (p["qid_a"], p["qid_b"], p["verdict"]) for p in record["proposals"]
        } >= {(run.qid("Flörburg"), run.qid("Kreis Peiltal"), "a_within_b")}
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


class TestRefusedAndCounted:
    def test_a_cycle_is_refused_and_counted(self, tmp_path, monkeypatch):
        run = PlaceRun(tmp_path, monkeypatch)
        # Germany within Flörburg, against the chain the record states
        run.engine.verdicts[("Flörburg", "Germany")] = "b_within_a"
        monkeypatch.setattr(
            scratchwrite,
            "place_graph",
            lambda wiki: (
                graph(wiki)[0],
                [
                    Question(
                        kind="competing",
                        qid_a=run.qid("Flörburg"),
                        qid_b=run.qid("Germany"),
                        context="both asserted as place of birth",
                    )
                ],
            ),
        )
        run.past_the_barrier()
        counts = run.record("places")["counts"]
        assert counts["cycles"] == 1
        assert item_values(run.wiki, run.qid("Germany"), "part_of") == []

    def test_a_cycle_with_the_graph_s_containment_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The graph read puts Hesse in Germany already; a verdict that
        puts Germany in Hesse is refused, though no proposal of the
        batch states the edge it would cycle with."""
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.verdicts[("Hesse", "Germany")] = "b_within_a"

        def read(wiki):
            found, _ = graph(wiki)
            hesse, germany = run.qid("Hesse"), run.qid("Germany")
            for record in found:
                if record.qid == hesse:
                    record.parents = [germany]
            pair = Question(
                kind="competing",
                qid_a=hesse,
                qid_b=germany,
                context="both asserted as place of birth",
            )
            return found, [pair]

        monkeypatch.setattr(scratchwrite, "place_graph", read)
        run.past_the_barrier()
        assert run.record("places")["counts"]["cycles"] == 1
        assert item_values(run.wiki, run.qid("Germany"), "part_of") == []

    def test_conflicts_and_schema_rejections_are_counted(
        self, tmp_path, monkeypatch
    ):
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.extra = [
            # a second verdict on the first question: a conflict
            json.dumps({"question": 1, "verdict": "distinct"}),
            # no question asked, and a verdict off the list
            json.dumps({"question": 99, "verdict": "same"}),
            json.dumps({"question": 2, "verdict": "overlaps"}),
        ]
        run.past_the_barrier()
        record = run.record("places")
        counts = record["counts"]
        assert counts["conflicts"] == 1
        assert counts["schema_rejected"] == 2
        # the conflicting question was refused whole: nothing of it
        # is among the proposals
        prompt = run.engine.prompts[0]
        first = QUESTION.search(prompt).groups()[1:]
        asked = {
            (label(run.wiki, p["qid_a"]), label(run.wiki, p["qid_b"]))
            for p in record["proposals"]
        }
        assert first not in asked
        assert run.driver.rundir is not None

    def test_the_counts(self, run):
        run.past_the_barrier()
        counts = run.record("places")["counts"]
        assert counts == {
            "questions": counts["questions"],
            "calls": 1,
            "made": counts["questions"],
            "applied": 3,
            "present": 0,
            "identity": 0,
            "not_containment": counts["questions"] - 3,
            "schema_rejected": 0,
            "cycles": 0,
            "conflicts": 0,
        }
        assert counts["questions"] >= 4

    def test_the_report_and_the_diff_count_them(self, tmp_path, monkeypatch):
        run = PlaceRun(tmp_path, monkeypatch)
        run.engine.extra = [json.dumps({"question": 99, "verdict": "same"})]
        run.past_the_barrier()
        placed = report._placed(run.run_dir)
        content = report._aggregate_content(
            {}, {}, {}, _vocabulary(), run.record("write"), placed
        )
        shown = content["place_hierarchy"]
        assert shown["applied"] == 3
        assert shown["rejected_by_schema"] == 1
        assert shown["refused_cycles"] == shown["refused_conflicts"] == 0
        assert shown["contents_recorded"] is True
        # the diff sets them side by side, as figures, and diff.md shows
        # them in a section of their own
        before = {"content": {"place_hierarchy": dict(shown, applied=1)}}
        after = {"content": {"place_hierarchy": shown}}
        rows = {row["figure"]: row for row in rundiff.figures(before, after)}
        applied = rows["content.place_hierarchy.applied"]
        assert (applied["baseline"], applied["run"], applied["delta"]) == (
            1,
            3,
            2,
        )
        assert "content.place_hierarchy.rejected_by_schema" in rows


# -- the cache, the key and the contents record -------------------------------


class TestKeys:
    def test_the_run_s_events_name_the_calls_places(self, run):
        """Keyed as the extract role's calls are, the place stage's calls
        are named apart in the run's events, so the report counts them
        apart from the extraction's (#201)."""
        run.past_the_barrier()
        events = run.driver.rundir.events()
        calls = [e for e in events if e["event"] in ("call", "cache-miss")]
        assert calls
        assert {e.get("role") for e in calls} == {"places"}
        assert {e.get("stage") for e in calls if "stage" in e} <= {"places"}

    def test_no_prompt_names_a_qid(self, run):
        run.past_the_barrier()
        (prompt,) = run.engine.prompts
        assert not re.search(r"\bQ\d+\b", prompt)

    def test_a_run_after_another_reset_asks_the_engine_nothing(
        self, tmp_path, monkeypatch
    ):
        """The same batches after a reset make the same prompts, which
        the LLM cache answers: the items' QIDs are no part of them."""
        first = PlaceRun(tmp_path, monkeypatch)
        first.past_the_barrier()
        assert len(first.engine.prompts) == 1
        before = first.qid("Flörburg")
        # a write the stage did not make voids the record; the next run
        # resets, and the instance mints its items afresh
        clear_contents(CONFIG, "a live test")
        (tmp_path / "again").mkdir()
        second = PlaceRun(
            tmp_path / "again", monkeypatch, wiki=first.wiki, run_id="run-2"
        )
        # the items' QIDs cross a digit boundary (Q1xx to Q1xxx), which
        # turns round the order of the competing pairs' QIDs as strings,
        # and the query service gives its places in another order
        second.wiki._next_qid = 1000
        monkeypatch.setattr(
            scratchwrite,
            "place_graph",
            lambda wiki: (list(reversed(graph(wiki)[0])), graph(wiki)[1]),
        )
        # the one LLM cache both runs call through
        second.driver.cache_dir = first.driver.cache_dir
        second.driver._engines[scratchwrite.PLACE_ROLE] = second.engine
        second.past_the_barrier()
        assert second.resets == 1
        # other QIDs, the same prompt
        assert second.qid("Flörburg") != before
        assert second.engine.prompts == []
        counts = second.record("places")["counts"]
        assert counts["applied"] == 3
        assert counts == first.record("places")["counts"]

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

    def test_the_instance_contents_take_the_place_key(self, run):
        run.past_the_barrier()
        recorded = recorded_contents(run.wiki)
        assert recorded.key == run.record("places")["key"]
        assert run.record("places")["recorded"] is True

    def test_a_rerun_of_the_run_reconciles_nothing_again(self, run):
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


# -- the direction a document states -------------------------------------


class TestDirection:
    def test_a_containment_cites_only_what_states_its_direction(
        self, tmp_path, monkeypatch
    ):
        """Document X states Fläckfeld within Kreis Mockburg, document Y
        Kreis Mockburg within Fläckfeld. A containment written is stated
        in the document that states it in that direction alone (#201)."""
        x = b"\x89PNG synthetic scan of a 1931 parish card from Flaeckfeld"
        y = b"\x89PNG synthetic scan of a 1931 district list of Mockburg"

        def stating(data, child, parent, child_id, parent_id):
            return ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "entities": [
                        {
                            "id": "leopold",
                            "label": "Leopold Löbig",
                            "kind": "human",
                        },
                        entity(child_id, child, [parent]),
                        entity(parent_id, parent),
                    ],
                    "claims": [
                        born_at(data, place(child_id, child)),
                        born_at(data, place(parent_id, parent)),
                    ],
                }
            )

        for verdict, cited_doc in (("a_within_b", x), ("b_within_a", y)):
            base = tmp_path / verdict
            base.mkdir()
            run = PlaceRun(base, monkeypatch, documents=())
            run.add(
                "parish-card.png",
                x,
                stating(x, "Fläckfeld", "Kreis Mockburg", "f", "k"),
            )
            run.add(
                "district-list.png",
                y,
                stating(y, "Kreis Mockburg", "Fläckfeld", "k", "f"),
            )
            run.engine.verdicts = {("Fläckfeld", "Kreis Mockburg"): verdict}
            run.past_the_barrier()
            child, parent = (
                ("Fläckfeld", "Kreis Mockburg")
                if verdict == "a_within_b"
                else ("Kreis Mockburg", "Fläckfeld")
            )
            # one item of each name in each document: the map keeps a
            # document's places apart from another's, so look at the
            # statements whatever item holds them
            written = [
                statement
                for qid in by_label(run.wiki, child)
                for statement in containment(run.wiki, qid)
            ]
            assert written, verdict
            other = y if cited_doc is x else x
            cites = [cited(statement, "stated_in") for statement in written]
            # the document stating the other direction is never cited; the
            # one stating this direction is, where its record names both
            # places (a pair only the other document's record joins cites
            # no document)
            assert all(run.artifact(other) not in c for c in cites), verdict
            assert [run.artifact(cited_doc)] in cites, verdict
            clear_contents(CONFIG, "the next case")


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
        asked = run.record("places")["counts"]["questions"]
        (prompt,) = run.engine.prompts
        assert "'Fläckfeld'" in prompt
        assert len(QUESTION.findall(prompt)) == asked
        whole = PlaceRun(tmp_path_of(run) / "whole", monkeypatch)
        clear_contents(CONFIG, "the whole write, for comparison")
        whole.past_the_barrier()
        assert whole.record("places")["counts"]["questions"] == asked


def tmp_path_of(run) -> Path:
    path = run.tmp_path
    path.joinpath("whole").mkdir(exist_ok=True)
    return path


# -- a malformed answer the cache kept --------------------------------------


class TestKeptAnswer:
    def test_a_malformed_answer_kept_is_asked_again(self, run):
        """The engine's answer lacks its terminal sentinel: the stage
        fails. Invoked again, the LLM cache's kept answer is rejected and
        asked once more, and the engine's new answer is taken (#201)."""
        run.past_the_barrier(until="write")
        run.engine.broken = True
        with pytest.raises(ValueError, match="sentinel"):
            run.stage("places")
        assert len(run.engine.prompts) == 1
        run.engine.broken = False
        run.stage("places")
        assert len(run.engine.prompts) == 2
        assert run.record("places")["counts"]["applied"] == 3

    def test_a_malformed_answer_from_the_engine_fails_the_stage(self, run):
        run.past_the_barrier(until="write")
        run.engine.broken = True
        with pytest.raises(ValueError, match="sentinel"):
            run.stage("places")
        # asked once: an answer the engine gave in this run is no kept
        # one, so it is not asked again, of the engine or of the cache
        assert len(run.engine.prompts) == 1
        events = [e["event"] for e in run.driver.rundir.events()]
        assert events.count("cache-miss") == 1
        assert "cache-hit" not in events


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
        assert "proposals" not in run.record("places")
        # the next invocation asks and applies
        run.stage("places")
        assert run.record("places")["counts"]["applied"] == 3

    def test_a_pause_mid_way_resumes_where_it_stopped(self, run, monkeypatch):
        """Paused after its first containment is written, the run is
        invoked again: the reset finds the instance holding this run's
        writes alone and goes on, the write records no key over the
        containment written, and the place stage applies the rest of
        what it proposed without reading the graph or asking again."""
        run.past_the_barrier(until="write")
        real = scratchwrite._containment
        made = []

        def containment(*args, **kwargs):
            made.append(args)
            if len(made) == 2:
                raise UsageLimitReached(
                    "the five-hour window is used up",
                    resumes_at=datetime(2026, 10, 6, 15, tzinfo=UTC),
                )
            return real(*args, **kwargs)

        monkeypatch.setattr(scratchwrite, "_containment", containment)
        with pytest.raises(UsageLimitReached):
            run.stage("places")
        assert len(run.record("places")["applied"]) == 1
        proposals = run.record("places")["proposals"]
        # the next invocation
        for name in ("reset", "register", "write"):
            run.stage(name)
        assert run.record("reset")["action"] == scratchwrite.RESUMED
        # the instance holds more than the write key makes
        assert recorded_contents(run.wiki).key is None
        run.stage("places")
        record = run.record("places")
        assert record["counts"]["applied"] == 3
        assert record["proposals"] == proposals
        assert len(run.engine.prompts) == 1
        assert recorded_contents(run.wiki).key == record["key"]

    def test_an_invocation_after_a_pause_asks_nothing_again(
        self, run, monkeypatch
    ):
        run.past_the_barrier(until="write")
        real = places.ensure_places_run_item

        def paused(*args, **kwargs):
            raise UsageLimitReached(
                "the five-hour window is used up",
                resumes_at=datetime(2026, 10, 6, 15, tzinfo=UTC),
            )

        monkeypatch.setattr(places, "ensure_places_run_item", paused)
        with pytest.raises(UsageLimitReached):
            run.stage("places")
        assert len(run.engine.prompts) == 1
        monkeypatch.setattr(places, "ensure_places_run_item", real)
        run.stage("places")
        assert len(run.engine.prompts) == 1
        assert run.record("places")["counts"]["applied"] == 3
