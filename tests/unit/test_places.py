"""Offline tests for the place-hierarchy pass (ADR-0009). No instance,
no LLM; fixtures are fictional."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

import pytest

from tests.unit.test_registrar import FakeWiki, WriteRefused
from yizkr.pipeline.places import (
    PlaceRecord,
    Question,
    adjudicate_places,
    build_worklist,
    ensure_places_run_item,
    export_places_csv,
    plan_containment,
    record_containers,
    stated_parent_candidates,
)


def places() -> list[PlaceRecord]:
    return [
        PlaceRecord(qid="Q1", label="Germany"),
        PlaceRecord(
            qid="Q2", label="Flörburg", chains=[["Kreis Peiltal", "Germany"]]
        ),
        PlaceRecord(
            qid="Q3", label="Brunnenring 35, Flörburg", chains=[["Flörburg"]]
        ),
        PlaceRecord(qid="Q4", label="Klurnau"),
        PlaceRecord(qid="Q5", label="Bad Flörburg", aliases=["Flörburg"]),
        PlaceRecord(
            qid="Q6", label="Rekau", parents=["Q1"], chains=[["Germany"]]
        ),
        PlaceRecord(qid="Q7", label="Kreis Peiltal"),
    ]


def pairs(questions) -> list[tuple[str, str]]:
    return [(q.qid_a, q.qid_b) for q in questions]


class TestStatedParentCandidates:
    def test_a_chain_is_asked_link_by_link(self):
        questions = stated_parent_candidates(places())
        # Flörburg within its nearest container, Kreis Peiltal, and Kreis
        # Peiltal within Germany: never the shortcut Flörburg within
        # Germany. Brunnenring 35 within Flörburg, which two places are
        # named (namesakes are each asked, never one guessed)
        assert pairs(questions) == [
            ("Q2", "Q7"),
            ("Q7", "Q1"),
            ("Q3", "Q2"),
            ("Q3", "Q5"),
        ]
        assert questions[0].kind == "parent"
        assert "'Kreis Peiltal'" in questions[0].context
        assert "'Kreis Peiltal' within 'Germany'" in questions[1].context

    def test_a_link_skips_a_container_no_place_is_named(self):
        # no place is named Hesse: Fläckfeld's nearest existing container
        # is Kreis Mockburg, and Kreis Mockburg's next is Germany
        found = [
            PlaceRecord(
                qid="Q20",
                label="Fläckfeld",
                chains=[
                    ["Hesse-Nassau", "Kreis Mockburg", "Hesse", "Germany"]
                ],
            ),
            PlaceRecord(qid="Q21", label="Kreis Mockburg"),
            PlaceRecord(qid="Q1", label="Germany"),
        ]
        assert pairs(stated_parent_candidates(found)) == [
            ("Q20", "Q21"),
            ("Q21", "Q1"),
        ]

    def test_each_record_s_chain_is_its_own(self):
        # two records: Kreis Mockburg alone, and Hesse alone; neither is
        # put within the other
        found = [
            PlaceRecord(
                qid="Q20",
                label="Fläckfeld",
                chains=[["Kreis Mockburg"], ["Hesse"]],
            ),
            PlaceRecord(qid="Q21", label="Kreis Mockburg"),
            PlaceRecord(qid="Q22", label="Hesse"),
        ]
        assert pairs(stated_parent_candidates(found)) == [
            ("Q20", "Q21"),
            ("Q20", "Q22"),
        ]

    def test_a_label_s_commas_propose_nothing(self):
        # "Brunnenring 35, Flörburg" states no container: only a record
        # does
        found = [
            PlaceRecord(qid="Q3", label="Brunnenring 35, Flörburg"),
            PlaceRecord(qid="Q2", label="Flörburg"),
        ]
        assert stated_parent_candidates(found) == []

    def test_names_fold_case_and_accents(self):
        found = [
            PlaceRecord(
                qid="Q3", label="Brunnenring 35", chains=[["FLORBURG"]]
            ),
            PlaceRecord(qid="Q2", label="Flörburg"),
            PlaceRecord(qid="Q8", label="Floerburg"),
        ]
        assert pairs(stated_parent_candidates(found)) == [("Q3", "Q2")]

    def test_umlauts_written_out_meet(self):
        found = [
            PlaceRecord(
                qid="Q3", label="Brunnenring 35", chains=[["Flörburg"]]
            ),
            PlaceRecord(qid="Q8", label="Floerburg"),
            PlaceRecord(qid="Q9", label="Florburg"),
        ]
        # "Flörburg" keys both ways, so each spelling is asked once
        assert pairs(stated_parent_candidates(found)) == [
            ("Q3", "Q8"),
            ("Q3", "Q9"),
        ]

    def test_a_known_container_is_not_re_asked(self):
        # Rekau is in Germany already; Klurnau states nothing
        questions = stated_parent_candidates(places())
        assert not any(q.qid_a in ("Q6", "Q4") for q in questions)

    def test_a_container_reached_through_the_graph_is_not_re_asked(self):
        found = [
            PlaceRecord(qid="Q1", label="Germany"),
            PlaceRecord(qid="Q7", label="Hesse", parents=["Q1"]),
            PlaceRecord(
                qid="Q2",
                label="Flörburg",
                parents=["Q7"],
                chains=[["Germany"]],
            ),
        ]
        assert stated_parent_candidates(found) == []

    def test_a_place_the_graph_puts_inside_is_never_its_container(self):
        # the urban district Rekau, whose record names the town Rekau
        # inside it as its container: the town is never asked, nor is the
        # district itself
        found = [
            PlaceRecord(qid="Q70", label="Rekau", parents=["Q71"]),
            PlaceRecord(qid="Q71", label="Rekau", chains=[["Rekau"]]),
        ]
        assert stated_parent_candidates(found) == []

    def test_each_pair_is_asked_once(self):
        found = [
            PlaceRecord(
                qid="Q2", label="Flörburg", chains=[["Hesse"], ["HESSE"]]
            ),
            PlaceRecord(qid="Q7", label="Hesse", aliases=["HESSE"]),
        ]
        assert pairs(stated_parent_candidates(found)) == [("Q2", "Q7")]


def _written(directory, entities, created, view="batch.approved.json"):
    directory.mkdir(parents=True)
    (directory / view).write_text(
        json.dumps(
            {
                "extraction_run": {
                    "model_identifier": "fake-model",
                    "pipeline_version": "0.1.0",
                    "started_at": "2026-07-13T09:00:00Z",
                },
                "claims": [],
                "entities": entities,
            }
        )
    )
    (directory / "write.json").write_text(
        json.dumps({"created_items": created})
    )


def _place(local_id, label, within=(), qid=None):
    entity = {
        "id": local_id,
        "label": label,
        "kind": "place",
        "structure": {"kind": "place", "within": list(within)},
    }
    if qid:
        entity["qid"] = qid
    return entity


class TestRecordContainers:
    def test_a_written_place_reaches_its_stated_containers(self, tmp_path):
        _written(
            tmp_path / "marriage",
            [
                _place("e1", "Fläckfeld", ["Kreis Mockburg"]),
                _place("e2", "Rekau", ["Hesse"], qid="Q6"),
                _place("e3", "Klurnau"),
                {"id": "e4", "label": "Ida Löbig", "kind": "human"},
                _place("e5", "Peiltal", ["Hesse"]),
            ],
            {"e1": "Q20", "e3": "Q4", "e4": "Q30"},
        )
        _written(
            tmp_path / "excerpts" / "card",
            [_place("e1", "Fläckfeld", ["Kreis Mockburg", "Germany"])],
            {"e1": "Q20"},
            view="batch.json",
        )
        # set aside, and a directory no write reached
        _written(
            tmp_path / ".marriage.superseded-1",
            [_place("e1", "Fläckfeld", ["Elsewhere"])],
            {"e1": "Q20"},
        )
        (tmp_path / "letter").mkdir()
        (tmp_path / "letter" / "batch.json").write_text("{}")
        assert record_containers(tmp_path) == {
            "Q20": [["Kreis Mockburg"], ["Kreis Mockburg", "Germany"]],
            "Q6": [["Hesse"]],
        }

    def test_one_chain_twice_is_kept_once(self, tmp_path):
        for name in ("card", "letter"):
            _written(
                tmp_path / name,
                [_place("e1", "Fläckfeld", ["Kreis Mockburg", "Hesse"])],
                {"e1": "Q20"},
            )
        assert record_containers(tmp_path) == {
            "Q20": [["Kreis Mockburg", "Hesse"]]
        }

    def test_a_place_the_writer_found_by_its_label_is_joined(self, tmp_path):
        # the writer found the existing Klurnau (Q4) under its label and
        # records it in created_items as it does a minted item
        _written(
            tmp_path / "card",
            [_place("e2", "Klurnau", ["Kreis Peiltal"])],
            {"e2": "Q4"},
        )
        assert record_containers(tmp_path) == {"Q4": [["Kreis Peiltal"]]}

    def test_the_written_batch_wins_over_earlier_views(self, tmp_path):
        directory = tmp_path / "card"
        _written(
            directory,
            [_place("e1", "Fläckfeld", ["Kreis Peiltal"])],
            {"e1": "Q20"},
        )
        # the extraction's own view and reconcile's, which a review
        # changed before the write
        _written(
            tmp_path / "other",
            [_place("e1", "Fläckfeld", ["Kreis Mockburg"])],
            {},
            view="batch.json",
        )
        for view in ("batch.json", "batch.resolved.json"):
            (directory / view).write_text(
                (tmp_path / "other" / "batch.json").read_text()
            )
        (tmp_path / "other" / "write.json").unlink()
        assert record_containers(tmp_path) == {"Q20": [["Kreis Peiltal"]]}
        (directory / "batch.approved.json").write_text("not json")
        (directory / "batch.resolved.json").write_text("not json")
        assert record_containers(tmp_path) == {"Q20": [["Kreis Mockburg"]]}

    def test_a_document_no_write_reached_states_nothing(self, tmp_path):
        # reconcile resolved its place, but the document was never written
        directory = tmp_path / "card"
        directory.mkdir()
        (directory / "batch.resolved.json").write_text(
            json.dumps(
                {
                    "extraction_run": {
                        "model_identifier": "fake-model",
                        "pipeline_version": "0.1.0",
                        "started_at": "2026-07-13T09:00:00Z",
                    },
                    "claims": [],
                    "entities": [
                        _place("e1", "Fläckfeld", ["Hesse"], qid="Q20")
                    ],
                }
            )
        )
        assert record_containers(tmp_path) == {}

    @pytest.mark.parametrize(
        "written",
        ["[]", '"x"', "3", '{"created_items": []}', '{"created_items": 3}'],
    )
    def test_a_write_json_of_another_shape_is_no_join(self, tmp_path, written):
        _written(
            tmp_path / "card",
            [
                _place("e1", "Fläckfeld", ["Hesse"]),
                _place("e2", "Rekau", ["Hesse"], qid="Q6"),
            ],
            {},
        )
        (tmp_path / "card" / "write.json").write_text(written)
        expected = {"Q6": [["Hesse"]]} if written.startswith("{") else {}
        assert record_containers(tmp_path) == expected

    def test_a_missing_run_directory_holds_nothing(self, tmp_path):
        assert record_containers(tmp_path / "absent") == {}


class TestWorklist:
    def test_spine_first_ordering_and_dedupe(self):
        competing = [
            Question(
                kind="competing", qid_a="Q3", qid_b="Q4", context="pob of X"
            ),
            Question(
                kind="competing", qid_a="Q7", qid_b="Q1", context="pod of Y"
            ),
        ]
        stated = stated_parent_candidates(places())
        worklist = build_worklist(places(), competing, stated)
        keys = [frozenset((q.qid_a, q.qid_b)) for q in worklist]
        assert len(keys) == len(set(keys))  # deduplicated across sources
        # the questions with Germany (the graph puts a place in it) come
        # first, and the places it puts nothing in after
        assert keys == [
            frozenset(("Q7", "Q1")),
            frozenset(("Q2", "Q7")),
            frozenset(("Q3", "Q2")),
            frozenset(("Q3", "Q5")),
            frozenset(("Q3", "Q4")),
        ]

    def test_a_place_holding_more_places_comes_first(self):
        # Germany holds two places, Kreis Peiltal one; both sit at the
        # same depth among their pair's places
        found = [
            PlaceRecord(qid="Q1", label="Germany"),
            PlaceRecord(qid="Q6", label="Rekau", parents=["Q1"]),
            PlaceRecord(qid="Q10", label="Hesse", parents=["Q1"]),
            PlaceRecord(qid="Q7", label="Kreis Peiltal"),
            PlaceRecord(qid="Q2", label="Flörburg", parents=["Q7"]),
            PlaceRecord(qid="Q4", label="Klurnau"),
            PlaceRecord(qid="Q9", label="Lager Klurnau"),
        ]
        smaller = Question(kind="competing", qid_a="Q7", qid_b="Q9")
        larger = Question(kind="competing", qid_a="Q1", qid_b="Q4")
        assert build_worklist(found, [smaller, larger], []) == [
            larger,
            smaller,
        ]

    def test_among_equals_a_place_placed_less_deep_comes_first(self):
        found = [
            PlaceRecord(qid="Q1", label="Germany"),
            PlaceRecord(qid="Q7", label="Hesse", parents=["Q1"]),
            PlaceRecord(qid="Q2", label="Flörburg", parents=["Q7"]),
            PlaceRecord(qid="Q8", label="Bad Flörburg", parents=["Q7"]),
            PlaceRecord(qid="Q4", label="Klurnau"),
            PlaceRecord(qid="Q9", label="Lager Klurnau"),
        ]
        deep = Question(kind="competing", qid_a="Q2", qid_b="Q8")
        shallow = Question(kind="competing", qid_a="Q4", qid_b="Q9")
        assert build_worklist(found, [deep, shallow], []) == [shallow, deep]


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


class Stopping(ScriptedLlm):
    """A scripted client whose engine reports why its model stopped
    (`last_report`, as the engines' clients do)."""

    def __init__(self, response: str, stop_reason: str) -> None:
        super().__init__(response)
        self.stop_reason = stop_reason

    def last_report(self) -> dict:
        return {"stop_reason": self.stop_reason}


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
            json.dumps({"done": True}),
        ]
        llm = ScriptedLlm("\n".join(lines))
        questions = [Question(kind="parent", qid_a="Q3", qid_b="Q2")]
        proposals = adjudicate_places(llm, questions, places())
        assert [(p.qid_a, p.qid_b, p.verdict) for p in proposals] == [
            ("Q3", "Q2", "a_within_b")
        ]
        # the prompt shows full labels — the evidence the model weighs
        assert "Brunnenring 35, Flörburg" in llm.prompts[0]

    def test_the_prompt_asks_for_a_done_line_and_no_count(self):
        """The answer ends with `{"done": true}`, whose presence is the
        gate: a count of the proposals gated nothing (#7)."""
        llm = ScriptedLlm('{"done": true}')
        question = Question(kind="parent", qid_a="Q3", qid_b="Q2")
        assert adjudicate_places(llm, [question], places()) == []
        assert 'End with exactly:\n{"done": true}\n' in llm.prompts[0]
        assert "emitted" not in llm.prompts[0]

    @pytest.mark.parametrize(
        "done", ['{"done": true}', '{"done": true, "proposals_emitted": 7}']
    )
    def test_an_answer_stopped_at_the_output_limit_raises(self, done):
        """Its engine stopped it at max_tokens: it is cut off, sentinel or
        no (#7)."""
        verdict = json.dumps({"qid_a": "Q3", "qid_b": "Q2", "verdict": "same"})
        question = Question(kind="parent", qid_a="Q3", qid_b="Q2")
        llm = Stopping(f"{verdict}\n{done}", "max_tokens")
        with pytest.raises(ValueError, match="cut off at max_tokens"):
            adjudicate_places(llm, [question], places())
        llm = Stopping(f"{verdict}\n{done}", "end_turn")
        (proposal,) = adjudicate_places(llm, [question], places())
        assert proposal.verdict == "same"

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
        assert line["label_a"] == "Brunnenring 35, Flörburg"
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
        labelled, with no kind, so the non-privileged export would leave it out
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


class TestCommand:
    """`yizkr reconcile-places --run DIR` gives each graph place the
    containers its records state there, and asks about them."""

    def test_run_directories_propose_parents(self, tmp_path, monkeypatch):
        import argparse

        from yizkr import cli
        from yizkr.pipeline import llm, places

        _written(
            tmp_path / "run1" / "card",
            [_place("e1", "Fläckfeld", ["Kreis Mockburg"])],
            {"e1": "Q20"},
        )
        _written(
            tmp_path / "run2" / "card",
            [_place("e1", "Fläckfeld", ["Kreis Mockburg", "Hesse"])],
            {"e1": "Q20"},
        )
        graph = [
            PlaceRecord(qid="Q20", label="Fläckfeld"),
            PlaceRecord(qid="Q21", label="Kreis Mockburg"),
            PlaceRecord(qid="Q7", label="Hesse"),
        ]
        asked = []

        def adjudicate(client, worklist, found):
            asked.extend((q.qid_a, q.qid_b) for q in worklist)
            assert found[0].chains == [
                ["Kreis Mockburg"],
                ["Kreis Mockburg", "Hesse"],
            ]
            return []

        monkeypatch.setattr(cli.Config, "load", lambda env_file: None)
        monkeypatch.setattr(cli, "WikiClient", lambda config: None)
        monkeypatch.setattr(places, "fetch_places", lambda c, cf: graph)
        monkeypatch.setattr(places, "fetch_competing_pairs", lambda c, cf: [])
        monkeypatch.setattr(places, "adjudicate_places", adjudicate)
        monkeypatch.setattr(llm, "make_client", lambda name: None)
        args = argparse.Namespace(
            env_file=None,
            apply=None,
            llm="anthropic",
            out=tmp_path / "places.csv",
            run=[tmp_path / "run1", tmp_path / "run2"],
        )
        assert cli.cmd_reconcile_places(args) == 0
        assert asked == [("Q20", "Q21"), ("Q21", "Q7")]

    def test_run_takes_several_directories(self):
        from yizkr import cli

        args = cli.build_parser().parse_args(
            ["reconcile-places", "--run", "a", "--run", "b"]
        )
        assert args.run == [Path("a"), Path("b")]

    def test_without_a_run_nothing_is_stated(self, tmp_path, monkeypatch):
        import argparse

        from yizkr import cli
        from yizkr.pipeline import places

        monkeypatch.setattr(cli.Config, "load", lambda env_file: None)
        monkeypatch.setattr(cli, "WikiClient", lambda config: None)
        monkeypatch.setattr(
            places,
            "fetch_places",
            lambda c, cf: [PlaceRecord(qid="Q20", label="Fläckfeld")],
        )
        monkeypatch.setattr(places, "fetch_competing_pairs", lambda c, cf: [])
        args = argparse.Namespace(
            env_file=None,
            apply=None,
            llm="anthropic",
            out=tmp_path / "places.csv",
            run=None,
        )
        assert cli.cmd_reconcile_places(args) == 0
        assert not (tmp_path / "places.csv").exists()
