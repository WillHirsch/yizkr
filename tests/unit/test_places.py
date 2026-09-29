"""Offline tests for the place-hierarchy pass (ADR-0009). No instance,
no LLM; fixtures are fictional."""

from __future__ import annotations

import copy
import csv
import json

import pytest

from tests.unit.test_registrar import FakeWiki, WriteRefused
from yizkr.pipeline.places import (
    PlaceRecord,
    Question,
    adjudicate_places,
    build_worklist,
    comma_parent_candidates,
    ensure_places_run_item,
    export_places_csv,
    plan_containment,
)


def places() -> list[PlaceRecord]:
    return [
        PlaceRecord(qid="Q1", label="Germany"),
        PlaceRecord(qid="Q2", label="Flörburg, Germany"),
        PlaceRecord(qid="Q3", label="Brunnenring 35, Flörburg, Germany"),
        PlaceRecord(qid="Q4", label="Klurnau"),
        PlaceRecord(
            qid="Q5", label="Bad Flörburg", aliases=["Flörburg, Germany"]
        ),
        PlaceRecord(qid="Q6", label="Rekau, Germany", parents=["Q1"]),
    ]


class TestCommaCandidates:
    def test_nearest_existing_suffix_wins(self):
        questions = comma_parent_candidates(places())
        pairs = {(q.qid_a, q.qid_b) for q in questions}
        # Brunnenring 35 attaches to Flörburg (nearest), not Germany
        assert ("Q3", "Q2") in pairs or ("Q3", "Q5") in pairs
        assert ("Q3", "Q1") not in pairs
        # Flörburg attaches to Germany
        assert ("Q2", "Q1") in pairs

    def test_alias_matches_count_as_names(self):
        # Q5's ALIAS equals Q2's label form; suffix matching must see it
        questions = comma_parent_candidates(
            [
                PlaceRecord(qid="Q7", label="Brunnenring, Bad Flörburg"),
                PlaceRecord(
                    qid="Q5", label="Some Other Name", aliases=["Bad Flörburg"]
                ),
            ]
        )
        assert [(q.qid_a, q.qid_b) for q in questions] == [("Q7", "Q5")]

    def test_existing_parents_are_not_re_asked(self):
        questions = comma_parent_candidates(places())
        assert not any(q.qid_a == "Q6" for q in questions)

    def test_no_commas_no_candidates(self):
        assert (
            comma_parent_candidates([PlaceRecord(qid="Q4", label="Klurnau")])
            == []
        )


class TestWorklist:
    def test_spine_first_ordering_and_dedupe(self):
        competing = [
            Question(
                kind="competing", qid_a="Q3", qid_b="Q4", context="pob of X"
            ),
            Question(
                kind="competing", qid_a="Q2", qid_b="Q1", context="pod of Y"
            ),
        ]
        comma = comma_parent_candidates(places())
        worklist = build_worklist(places(), competing, comma)
        keys = [frozenset((q.qid_a, q.qid_b)) for q in worklist]
        assert len(keys) == len(set(keys))  # deduplicated across sources
        # the Q2/Q1 question (country-scale) precedes the Q3 ones
        position = {key: index for index, key in enumerate(keys)}
        assert (
            position[frozenset(("Q2", "Q1"))]
            < position[frozenset(("Q3", "Q4"))]
        )


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


class TestAdjudication:
    def test_verdicts_parse_and_off_list_rulings_drop(self):
        lines = [
            json.dumps(
                {
                    "qid_a": "Q3",
                    "qid_b": "Q2",
                    "verdict": "a_within_b",
                    "confidence": 0.95,
                    "evidence": "an address in the town",
                }  # fmt: skip
            ),
            json.dumps(  # not an asked question: must be dropped
                {
                    "qid_a": "Q1",
                    "qid_b": "Q4",
                    "verdict": "same",
                    "confidence": 1,
                }
            ),
            json.dumps(  # unknown verdict: dropped
                {"qid_a": "Q3", "qid_b": "Q2", "verdict": "overlaps"}
            ),
            json.dumps({"done": True, "proposals_emitted": 3}),
        ]
        llm = ScriptedLlm("\n".join(lines))
        questions = [Question(kind="parent", qid_a="Q3", qid_b="Q2")]
        proposals = adjudicate_places(llm, questions, places())
        assert [(p.qid_a, p.qid_b, p.verdict) for p in proposals] == [
            ("Q3", "Q2", "a_within_b")
        ]
        # the prompt shows full labels — the evidence the model weighs
        assert "Brunnenring 35, Flörburg, Germany" in llm.prompts[0]

    def test_missing_sentinel_raises(self):
        llm = ScriptedLlm(
            json.dumps({"qid_a": "Q3", "qid_b": "Q2", "verdict": "distinct"})
        )
        with pytest.raises(ValueError, match="sentinel"):
            adjudicate_places(
                llm,
                [Question(kind="parent", qid_a="Q3", qid_b="Q2")],
                places(),
            )


def row(qid_a, qid_b, verdict, decision="approve"):
    return {
        "qid_a": qid_a,
        "qid_b": qid_b,
        "verdict": verdict,
        "decision": decision,
    }


class TestPlanContainment:
    def test_directions_and_gating(self):
        rows = [
            row("Q3", "Q2", "a_within_b"),
            row("Q1", "Q2", "b_within_a"),  # Q2 within Q1
            row("Q4", "Q2", "distinct"),
            row("Q5", "Q2", "same"),
            row("Q9", "Q8", "a_within_b", decision=""),  # undecided: skipped
        ]
        edges, notes = plan_containment(rows, creates_cycle=lambda c, p: False)
        assert edges == [("Q3", "Q2"), ("Q2", "Q1")]
        assert any("curator-executed" in note for note in notes)

    def test_live_cycle_guard(self):
        rows = [row("Q1", "Q3", "a_within_b")]  # country inside the address
        edges, notes = plan_containment(
            rows,
            creates_cycle=lambda child, parent: (
                (child, parent) == ("Q1", "Q3")
            ),
        )
        assert edges == []
        assert any("cycle" in note for note in notes)

    def test_batch_internal_cycle_guard(self):
        rows = [
            row("Q3", "Q2", "a_within_b"),  # address in town
            row("Q2", "Q3", "a_within_b"),  # town in address: cyclic
        ]
        edges, notes = plan_containment(rows, creates_cycle=lambda c, p: False)
        assert edges == [("Q3", "Q2")]
        assert any("cycle" in note for note in notes)

    def test_self_containment_refused(self):
        edges, notes = plan_containment(
            [row("Q3", "Q3", "a_within_b")], creates_cycle=lambda c, p: False
        )
        assert edges == []
        assert any("itself" in note for note in notes)


class TestCsv:
    def test_round_trip_fields(self, tmp_path):
        from yizkr.pipeline.places import PlaceProposal

        questions = [
            Question(
                kind="competing", qid_a="Q3", qid_b="Q4", context="pob of X"
            )
        ]
        proposals = [
            PlaceProposal(
                qid_a="Q3",
                qid_b="Q4",
                verdict="distinct",
                confidence=0.9,
                evidence="different places",  # fmt: skip
            )
        ]
        path = tmp_path / "places.csv"
        export_places_csv(proposals, questions, places(), path)
        with open(path, newline="") as handle:
            (line,) = list(csv.DictReader(handle))
        assert line["label_a"] == "Brunnenring 35, Flörburg, Germany"
        assert line["question"] == "pob of X"
        assert line["decision"] == ""


# -- the pass's run item (#368) -----------------------------------------------


class RunItemWiki(FakeWiki):
    """The registrar tests' in-memory wiki, with the lag-free label lookup
    the pass finds its run item by. Like the wiki client's label index, it
    fails on a label two items share."""

    def find_item_by_label(self, label):
        found = [
            qid
            for qid, entity in self.entities.items()
            if entity["labels"].get("en", {}).get("value") == label
        ]
        assert len(found) <= 1, f"two items are labelled {label!r}"
        return found[0] if found else None


FINGERPRINT = "0a9afd5746"
RUN_LABEL = f"place-hierarchy run fake-model {FINGERPRINT}"
P, ITEMS = FakeWiki.PROPERTIES, FakeWiki.ITEMS


def run_item(wiki) -> str:
    return ensure_places_run_item(wiki, FINGERPRINT, "fake-model")


def kinds(wiki, qid: str) -> list[tuple[str, str, list[str]]]:
    """(kind, rank, the items it is stated in) of each `instance of`
    statement of an item."""
    return [
        (
            statement["mainsnak"]["datavalue"]["value"]["id"],
            statement["rank"],
            [
                snak["datavalue"]["value"]["id"]
                for reference in statement.get("references", [])
                for snak in reference["snaks"].get(P["stated_in"], [])
            ],
        )
        for statement in wiki.entities[qid]["claims"].get(P["instance_of"], [])
    ]


class TestPlacesRunItem:
    """The pass's run item states its kind, `extraction run`, in itself, in
    a write after the one that makes it (registrar.ensure_self_typed)."""

    def test_a_new_run_item_states_its_kind_in_itself(self):
        wiki = RunItemWiki()

        qid = run_item(wiki)

        assert wiki.entities[qid]["labels"]["en"]["value"] == RUN_LABEL
        assert kinds(wiki, qid) == [(ITEMS["extraction_run"], "normal", [qid])]
        assert wiki.writes == 2

    def test_a_pass_cut_short_is_typed_by_the_next(self):
        """A pass cut short between the run item's two writes leaves it
        labelled, with no kind, so the public export would leave it out
        and the containment it backs would lose its only reference there.
        The next pass finds it by its label and types it: one run item,
        typed."""
        wiki = RunItemWiki()
        wiki.refuse = lambda data: bool(data.get("claims"))
        with pytest.raises(WriteRefused):
            run_item(wiki)
        (left,) = wiki.entities
        assert wiki.entities[left]["labels"]["en"]["value"] == RUN_LABEL
        assert kinds(wiki, left) == []

        wiki.refuse = lambda data: False
        qid = run_item(wiki)

        assert qid == left
        assert list(wiki.entities) == [qid]
        assert kinds(wiki, qid) == [(ITEMS["extraction_run"], "normal", [qid])]

    def test_a_run_item_with_its_kind_gets_no_write(self):
        wiki = RunItemWiki()
        first = run_item(wiki)
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        assert run_item(wiki) == first

        assert wiki.writes == writes
        assert wiki.entities == before

    def test_a_kind_a_curator_deprecated_is_left_alone(self):
        wiki = RunItemWiki()
        qid = run_item(wiki)
        (statement,) = wiki.entities[qid]["claims"][P["instance_of"]]
        statement["rank"] = "deprecated"
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        assert run_item(wiki) == qid

        assert wiki.writes == writes
        assert wiki.entities == before
        assert kinds(wiki, qid) == [
            (ITEMS["extraction_run"], "deprecated", [qid])
        ]
