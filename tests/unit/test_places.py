"""Offline tests for the place-hierarchy pass (ADR-0009). No instance,
no LLM; fixtures are fictional."""

from __future__ import annotations

import csv
import json

import pytest

from yizkr.pipeline.places import (
    PlaceRecord,
    Question,
    adjudicate_places,
    build_worklist,
    comma_parent_candidates,
    export_places_csv,
    plan_containment,
)


def places() -> list[PlaceRecord]:
    return [
        PlaceRecord(qid="Q1", label="Exampleland"),
        PlaceRecord(qid="Q2", label="Springfield, Exampleland"),
        PlaceRecord(qid="Q3", label="Old Mill, Springfield, Exampleland"),
        PlaceRecord(qid="Q4", label="Shelbyville"),
        PlaceRecord(
            qid="Q5", label="Espringfeld", aliases=["Springfield, Exampleland"]
        ),
        PlaceRecord(
            qid="Q6", label="Capital City, Exampleland", parents=["Q1"]
        ),
    ]


class TestCommaCandidates:
    def test_nearest_existing_suffix_wins(self):
        questions = comma_parent_candidates(places())
        pairs = {(q.qid_a, q.qid_b) for q in questions}
        # Old Mill attaches to Springfield (nearest), not Exampleland
        assert ("Q3", "Q2") in pairs or ("Q3", "Q5") in pairs
        assert ("Q3", "Q1") not in pairs
        # Springfield attaches to Exampleland
        assert ("Q2", "Q1") in pairs

    def test_alias_matches_count_as_names(self):
        # Q5's ALIAS equals Q2's label form; suffix matching must see it
        questions = comma_parent_candidates(
            [
                PlaceRecord(qid="Q7", label="Docks, Espringfeld"),
                PlaceRecord(
                    qid="Q5", label="Some Other Name", aliases=["Espringfeld"]
                ),
            ]
        )
        assert [(q.qid_a, q.qid_b) for q in questions] == [("Q7", "Q5")]

    def test_existing_parents_are_not_re_asked(self):
        questions = comma_parent_candidates(places())
        assert not any(q.qid_a == "Q6" for q in questions)

    def test_no_commas_no_candidates(self):
        assert (
            comma_parent_candidates(
                [PlaceRecord(qid="Q4", label="Shelbyville")]
            )
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
                    "evidence": "a mill in the town",
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
        assert "Old Mill, Springfield, Exampleland" in llm.prompts[0]

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
        rows = [row("Q1", "Q3", "a_within_b")]  # country inside the mill
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
            row("Q3", "Q2", "a_within_b"),  # mill in town
            row("Q2", "Q3", "a_within_b"),  # town in mill: cyclic with above
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
                evidence="different towns",  # fmt: skip
            )
        ]
        path = tmp_path / "places.csv"
        export_places_csv(proposals, questions, places(), path)
        with open(path, newline="") as handle:
            (line,) = list(csv.DictReader(handle))
        assert line["label_a"] == "Old Mill, Springfield, Exampleland"
        assert line["question"] == "pob of X"
        assert line["decision"] == ""
