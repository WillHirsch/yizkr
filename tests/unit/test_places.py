"""Offline tests for the place hierarchy (ADR-0009, ADR-0038 §3 and §8.5;
#201): which existing place a container a source states means. No
instance, no LLM.

* The links a document's mention records state, link by link, each with
  the time a claim of the document gives it, or none for a link the
  record alone states.
* The worklist: a link is asked where its container names more than one
  place, or where no claim of its document gives it; the answer names a
  candidate or none. No containment verdict, no competing pair.
* Reset-stable prompts: no QID, numbered questions and candidates,
  namesakes numbered by their descriptions (decision FD).
* The answer's schema, conflicts, and a kept malformed answer asked
  again.
* The links an answer decides, the cycle guard, and the write: `part of`
  the named item, stated in the document's artifact with the claim's
  time, inferred from the run item.
* `yizkr reconcile-places`, its CSV and its `--apply`.

The fixtures are fictional (docs/EXAMPLES.md): Fläckfeld, which the 1905
marriage certificate puts in Kreis Mockburg and the 1984 family tree in
Kreis Peiltal; the Peiltal court prison, "in Peiltal", a name the town
Peiltal and Kreis Peiltal both answer to; and the worked examples of a
dated containment, "in 1941 Koshnovka belonged to Kaldovsky District"
and "Kaldovsky District, in Chadsk Oblast from 1937".
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from pathlib import Path

import pytest

from tests.unit.test_containment_claims import PlaceWiki
from tests.unit.test_registrar import FakeWiki, WriteRefused
from yizkr.claims import ClaimBatch
from yizkr.pipeline import places as hierarchy
from yizkr.pipeline.places import (
    PRESENT,
    REFERENCED,
    SAME,
    UNCERTAIN,
    WRITTEN,
    Adjudication,
    Answer,
    PlaceRecord,
    Question,
    StatedLink,
    Worklist,
    adjudicate,
    adjudication_prompt,
    approved_answers,
    attach_chains,
    build_worklist,
    ensure_places_run_item,
    export_places_csv,
    links_to_write,
    parse_answers,
    place_names,
    plan,
    question_text,
    record_links,
    stated_links,
    write_link,
)

MARRIAGE = hashlib.sha1(b"fictional 1905 marriage certificate").hexdigest()
TREE = hashlib.sha1(b"fictional 1984 family tree").hexdigest()
DEATH = hashlib.sha1(b"fictional 1941 death certificate").hexdigest()
RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-10-23T09:00:00Z",
}


def _local(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "place",
    }


def _entity(local_id, label, within=None, qid=None, aliases=()):
    entity = {
        "id": local_id,
        "label": label,
        "kind": "place",
        "aliases": list(aliases),
    }
    if within is not None:
        entity["structure"] = {"kind": "place", "within": list(within)}
    if qid:
        entity["qid"] = qid
    return entity


def _part_of(subject, container, sha1, qualifiers=()):
    return {
        "subject": _local(*subject),
        "property": "part_of",
        "value": {"type": "item", "value": _local(*container)},
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "page_number": "1",
        },
    }


def _year(key: str, year: int) -> dict:
    return {"property": key, "value": {"type": "time", "year": year}}


def _batch(entities, claims=()) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "entities": entities, "claims": list(claims)}
    )


def koshnovka_batch(*claims) -> ClaimBatch:
    """The 1941 death certificate: Koshnovka, its record placing it in
    Kaldovsky District, Chadsk Oblast."""
    return _batch(
        [
            _entity(
                "kosh", "Koshnovka", ["Kaldovsky District", "Chadsk Oblast"]
            ),
            _entity("kald", "Kaldovsky District"),
            _entity("chadsk", "Chadsk Oblast"),
        ],
        claims,
    )


IN_1941 = ("kosh", "Koshnovka"), ("kald", "Kaldovsky District")
FROM_1937 = ("kald", "Kaldovsky District"), ("chadsk", "Chadsk Oblast")
CREATED = {"kosh": "Q30", "kald": "Q31", "chadsk": "Q32"}


def times(link: StatedLink) -> list[tuple[str, int]]:
    return [
        (qualifier.property, qualifier.value.year)
        for qualifier in link.qualifier_models()
    ]


class TestStatedLinks:
    def test_a_record_alone_states_each_link_with_no_time(self):
        links = stated_links(koshnovka_batch(), CREATED, DEATH)
        assert [
            (link.place, link.index, link.container) for link in links
        ] == [
            ("Q30", 0, "Kaldovsky District"),
            ("Q30", 1, "Chadsk Oblast"),
        ]
        for link in links:
            assert link.sha1 == DEATH
            assert link.chain == ("Kaldovsky District", "Chadsk Oblast")
            assert not link.claimed
            assert link.qualifiers == ()

    def test_a_claim_gives_its_link_with_its_time(self):
        batch = koshnovka_batch(
            _part_of(*IN_1941, DEATH, [_year("point_in_time", 1941)]),
            _part_of(*FROM_1937, DEATH, [_year("start_time", 1937)]),
        )
        first, second = stated_links(batch, CREATED, DEATH)
        assert first.claimed and times(first) == [("point_in_time", 1941)]
        # the inner link: a claim from the entity the inner container names
        assert second.claimed and times(second) == [("start_time", 1937)]

    def test_an_undated_claim_gives_its_link_with_no_time(self):
        batch = koshnovka_batch(_part_of(*IN_1941, DEATH))
        first, second = stated_links(batch, CREATED, DEATH)
        assert first.claimed and first.qualifiers == ()
        assert not second.claimed

    def test_two_claims_of_one_link_give_a_link_each(self):
        batch = koshnovka_batch(
            _part_of(*IN_1941, DEATH, [_year("point_in_time", 1941)]),
            _part_of(*IN_1941, DEATH),
        )
        firsts = [
            link
            for link in stated_links(batch, CREATED, DEATH)
            if not link.index
        ]
        assert sorted(times(link) for link in firsts) == [
            [],
            [("point_in_time", 1941)],
        ]
        assert all(link.claimed for link in firsts)

    def test_a_claim_to_another_container_gives_no_link(self):
        batch = koshnovka_batch(
            _part_of(("kosh", "Koshnovka"), ("chadsk", "Chadsk Oblast"), DEATH)
        )
        links = stated_links(batch, CREATED, DEATH)
        # the shortcut is a claim of its own, never one of the record's links
        assert not any(link.claimed for link in links)

    def test_a_claim_from_another_place_gives_no_link(self):
        batch = koshnovka_batch(
            _part_of(("chadsk", "Chadsk Oblast"), IN_1941[1], DEATH)
        )
        (first, _) = stated_links(batch, CREATED, DEATH)
        assert not first.claimed

    def test_names_compare_folded(self):
        batch = _batch(
            [
                _entity("f", "Fläckfeld", ["kreis mockburg"]),
                _entity("k", "Kreis Mockburg"),
            ],
            [_part_of(("f", "Fläckfeld"), ("k", "Kreis Mockburg"), MARRIAGE)],
        )
        (link,) = stated_links(batch, {"f": "Q20"}, MARRIAGE)
        assert link.claimed

    def test_a_place_with_no_item_or_no_chain_states_nothing(self):
        batch = _batch(
            [
                _entity("f", "Fläckfeld", ["Kreis Mockburg"]),
                _entity("k", "Kreis Mockburg"),
                {"id": "p", "label": "Ida Löbig", "kind": "human"},
            ]
        )
        assert stated_links(batch, {"k": "Q21", "p": "Q1"}, MARRIAGE) == []

    def test_a_resolved_place_states_under_its_own_item(self):
        batch = _batch([_entity("f", "Fläckfeld", ["Kreis Mockburg"], "Q20")])
        (link,) = stated_links(batch, {}, MARRIAGE)
        assert link.place == "Q20"

    def test_without_a_document_the_place_s_claims_name_it(self):
        batch = _batch(
            [
                _entity("f", "Fläckfeld", ["Kreis Mockburg"]),
                _entity("k", "Kreis Mockburg"),
            ],
            [_part_of(("f", "Fläckfeld"), ("k", "Kreis Mockburg"), MARRIAGE)],
        )
        (link,) = stated_links(batch, {"f": "Q20"})
        assert link.sha1 == MARRIAGE

    def test_a_link_round_trips_through_json(self):
        batch = koshnovka_batch(
            _part_of(*IN_1941, DEATH, [_year("point_in_time", 1941)])
        )
        for link in stated_links(batch, CREATED, DEATH):
            assert StatedLink.from_json(link.as_json()) == link


def _written(directory, entities, created, sha1=None, view="batch.json"):
    directory.mkdir(parents=True)
    (directory / view).write_text(_batch(entities).model_dump_json())
    (directory / "write.json").write_text(
        json.dumps({"created_items": created})
    )
    if sha1:
        (directory / "state.json").write_text(
            json.dumps({"file": "x.png", "title": "x", "content_sha1": sha1})
        )


class TestRecordLinks:
    def test_a_run_directory_s_documents_state_their_links(self, tmp_path):
        _written(
            tmp_path / "marriage",
            [_entity("e1", "Fläckfeld", ["Kreis Mockburg", "Hesse"])],
            {"e1": "Q20"},
            MARRIAGE,
        )
        _written(
            tmp_path / "excerpts" / "tree",
            [_entity("e1", "Fläckfeld", ["Kreis Peiltal"])],
            {"e1": "Q20"},
            TREE,
            view="batch.approved.json",
        )
        # set aside, and a directory no write reached
        _written(
            tmp_path / ".marriage.superseded-1",
            [_entity("e1", "Fläckfeld", ["Kreis Rekau"])],
            {"e1": "Q20"},
            MARRIAGE,
        )
        (tmp_path / "letter").mkdir()
        (tmp_path / "letter" / "batch.json").write_text("{}")
        assert [
            (link.sha1, link.container) for link in record_links(tmp_path)
        ] == [
            (MARRIAGE, "Kreis Mockburg"),
            (MARRIAGE, "Hesse"),
            (TREE, "Kreis Peiltal"),
        ]

    @pytest.mark.parametrize("written", ["[]", "null", "not json"])
    def test_a_write_json_of_another_shape_is_passed_over(
        self, tmp_path, written
    ):
        _written(
            tmp_path / "marriage",
            [_entity("e1", "Fläckfeld", ["Kreis Mockburg"])],
            {"e1": "Q20"},
            MARRIAGE,
        )
        (tmp_path / "marriage" / "write.json").write_text(written)
        assert record_links(tmp_path) == []

    def test_a_missing_run_directory_holds_nothing(self, tmp_path):
        assert record_links(tmp_path / "absent") == []


def link(place, chain, index, sha1=MARRIAGE, claimed=False, qualifiers=()):
    return StatedLink(
        sha1=sha1,
        place=place,
        chain=tuple(chain),
        index=index,
        qualifiers=tuple(qualifiers),
        claimed=claimed,
    )


def peiltal() -> list[PlaceRecord]:
    """The Peiltal court prison, the town Peiltal in Kreis Peiltal, which
    also answers to 'Peiltal', and Hesse."""
    return [
        PlaceRecord(qid="Q40", label="Gerichtsgefängnis Peiltal"),
        PlaceRecord(qid="Q41", label="Peiltal", parents=["Q42"]),
        PlaceRecord(
            qid="Q42",
            label="Kreis Peiltal",
            aliases=["Peiltal"],
            parents=["Q43"],
        ),
        PlaceRecord(qid="Q43", label="Hesse"),
    ]


PRISON = ("Peiltal", "Hesse")


class TestWorklist:
    def test_a_container_naming_two_places_is_asked(self):
        found = peiltal()
        work = build_worklist(found, [link("Q40", PRISON, 0, claimed=True)])
        (question,) = work.questions
        assert question.key == ("Q40", PRISON, 0)
        # every namesake a candidate, in the order of their names
        assert question.candidates == ["Q42", "Q41"]
        assert [q.place for q in work.questions] == ["Q40"]

    def test_a_link_a_record_alone_states_is_asked(self):
        work = build_worklist(peiltal(), [link("Q40", PRISON, 1)])
        (question,) = work.questions
        assert question.candidates == ["Q43"]

    def test_a_claimed_link_naming_one_place_is_settled(self):
        work = build_worklist(
            peiltal(), [link("Q40", PRISON, 1, claimed=True)]
        )
        assert work.questions == []
        assert work.settled == {("Q40", PRISON, 1): "Q43"}

    def test_a_claimed_link_beside_a_record_s_alone_is_asked(self):
        work = build_worklist(
            peiltal(),
            [
                link("Q40", PRISON, 1, claimed=True),
                link("Q40", PRISON, 1, sha1=TREE),
            ],
        )
        (question,) = work.questions
        assert len(question.links) == 2

    def test_a_link_with_no_place_to_name_is_left_out(self):
        work = build_worklist(
            peiltal(),
            [
                link("Q40", ("Kreis Rekau",), 0),
                # a place that is none of the graph's
                link("Q99", PRISON, 1),
            ],
        )
        assert work.questions == [] and work.settled == {}
        assert work.unnamed == 2

    def test_a_place_is_never_its_own_candidate(self):
        work = build_worklist(peiltal(), [link("Q41", ("Peiltal",), 0)])
        (question,) = work.questions
        assert question.candidates == ["Q42"]

    def test_one_question_decides_each_document_s_link(self):
        work = build_worklist(
            peiltal(),
            [link("Q40", PRISON, 0), link("Q40", PRISON, 0, sha1=TREE)],
        )
        (question,) = work.questions
        assert [found.sha1 for found in question.links] == [MARRIAGE, TREE]

    def test_a_question_holding_more_places_comes_first(self):
        """Hesse holds two places and lies in Germany; Klurnau holds none
        and lies in nothing. Hesse's question comes first, though its
        text and its candidate's depth would put it second."""
        places = [
            PlaceRecord(qid="Q1", label="Germany"),
            PlaceRecord(qid="Q2", label="Hesse", parents=["Q1"]),
            PlaceRecord(qid="Q3", label="Kreis Peiltal", parents=["Q2"]),
            PlaceRecord(qid="Q4", label="Kreis Mockburg", parents=["Q2"]),
            PlaceRecord(qid="Q5", label="Klurnau"),
            PlaceRecord(qid="Q6", label="Fläckfeld"),
            PlaceRecord(qid="Q7", label="Brunnenring 35"),
        ]
        work = build_worklist(
            places,
            [link("Q6", ("Hesse",), 0), link("Q7", ("Klurnau",), 0)],
        )
        assert [q.candidates for q in work.questions] == [["Q2"], ["Q5"]]

    def test_spine_first(self):
        """A question whose candidates the graph puts more places inside
        comes first: Hesse holds three, Kreis Peiltal two."""
        work = build_worklist(
            peiltal(), [link("Q40", PRISON, 0), link("Q40", PRISON, 1)]
        )
        assert [q.index for q in work.questions] == [1, 0]

    def test_no_containment_verdict_and_no_pair(self):
        assert "a_within_b" not in hierarchy.ADJUDICATION_PROMPT
        assert "same" not in hierarchy.ADJUDICATION_PROMPT
        assert not hasattr(hierarchy, "fetch_competing_pairs")
        assert not hasattr(hierarchy, "VERDICTS")


# -- reset-stable prompts -----------------------------------------------------


def renumbered(found: list[PlaceRecord], links, shift: int):
    """The same graph and links with every QID moved by `shift`, across
    digit boundaries, and the places given in reverse order."""

    def qid(old: str) -> str:
        return f"Q{int(old[1:]) + shift}"

    places = [
        PlaceRecord(
            qid=qid(place.qid),
            label=place.label,
            aliases=list(place.aliases),
            parents=[qid(p) for p in reversed(place.parents)],
            description=place.description,
        )
        for place in reversed(found)
    ]
    moved = [
        StatedLink(
            sha1=found_link.sha1,
            place=qid(found_link.place),
            chain=found_link.chain,
            index=found_link.index,
            qualifiers=found_link.qualifiers,
            claimed=found_link.claimed,
        )
        for found_link in reversed(links)
    ]
    return places, moved


def prompts(places, links) -> list[str]:
    attach_chains(places, links)
    work = build_worklist(places, links)
    return [
        adjudication_prompt(
            work.questions[start : start + 2], place_names(places)
        )
        for start in range(0, len(work.questions), 2)
    ]


def namesakes() -> tuple[list[PlaceRecord], list[StatedLink]]:
    """Two items for Kreis Peiltal that clustering kept apart, told apart
    by their descriptions alone, and the places the records put in it."""
    places = [
        *peiltal(),
        PlaceRecord(
            qid="Q9",
            label="Kreis Peiltal",
            parents=["Q43"],
            description="district in Hesse, as the tree gives it",
        ),
        PlaceRecord(qid="Q20", label="Fläckfeld"),
    ]
    places[2].description = "district in Hesse"
    links = [
        link("Q20", ("Kreis Peiltal", "Hesse"), 0, sha1=TREE),
        link("Q20", ("Kreis Peiltal", "Hesse"), 1, sha1=TREE),
        *[link("Q40", PRISON, index) for index in (0, 1)],
    ]
    return places, links


class TestResetStablePrompts:
    def test_no_prompt_names_a_qid(self):
        for prompt in prompts(*namesakes()):
            assert not re.search(r"\bQ\d+\b", prompt)

    @pytest.mark.parametrize("shift", [1, 91, 991, 9991])
    def test_the_same_prompts_whatever_the_qids(self, shift):
        assert prompts(*renumbered(*namesakes(), shift)) == prompts(
            *namesakes()
        )

    @pytest.mark.parametrize("shift", [1, 91, 991])
    def test_ties_go_by_their_text_whatever_the_qids(self, shift):
        """Two questions alike but for their places' names, whose QIDs
        turn round as strings when renumbered (Q9 and Q20 to Q100 and
        Q111): the same prompt."""
        places = [
            PlaceRecord(qid="Q9", label="Brunnenring 35"),
            PlaceRecord(qid="Q20", label="Angerring 11"),
            PlaceRecord(qid="Q30", label="Flörburg"),
        ]
        links = [link("Q9", ("Flörburg",), 0), link("Q20", ("Flörburg",), 0)]
        assert prompts(*renumbered(places, links, shift)) == prompts(
            places, links
        )

    def test_namesakes_read_apart_by_their_descriptions(self):
        places, links = namesakes()
        names = place_names(places)
        assert names["Q9"] == (
            "'Kreis Peiltal' (district in Hesse, as the tree gives it; "
            "within 'Hesse')"
        )
        assert (
            names["Q42"]
            == "'Kreis Peiltal' (district in Hesse; within 'Hesse')"
        )

    @pytest.mark.parametrize("order", [1, -1])
    def test_namesakes_with_one_description_are_numbered(self, order):
        """Namesakes whose descriptions and containers coincide are
        numbered by their descriptions, then their documents, whatever
        the order of the places or of their QIDs."""
        first = PlaceRecord(qid="Q20", label="Rekau")
        second = PlaceRecord(qid="Q21", label="Rekau")
        names = place_names([first, second][::order])
        assert {names["Q20"], names["Q21"]} == {"'Rekau' [1]", "'Rekau' [2]"}

    @pytest.mark.parametrize("order", [1, -1])
    def test_namesakes_with_no_description_go_by_their_documents(self, order):
        """Two Fläckfelds with no description, each placed by a document
        of its own: numbered by the stating documents, whatever the order
        of the places or of their QIDs."""
        first = PlaceRecord(qid="Q20", label="Fläckfeld")
        second = PlaceRecord(qid="Q21", label="Fläckfeld")
        first.chain_sources = [[min(MARRIAGE, TREE)]]
        second.chain_sources = [[max(MARRIAGE, TREE)]]
        names = place_names([first, second][::order])
        assert names["Q20"].endswith("[1]")
        assert names["Q21"].endswith("[2]")
        first.qid, second.qid = second.qid, first.qid
        names = place_names([first, second][::order])
        assert names["Q21"].endswith("[1]")
        assert names["Q20"].endswith("[2]")

    def test_a_name_gives_the_graph_s_containers_and_the_records(self):
        places, links = namesakes()
        attach_chains(places, links)
        names = place_names(places)
        assert names["Q41"] == "'Peiltal' (within 'Kreis Peiltal')"
        assert names["Q20"] == (
            "'Fläckfeld' (a record places it in 'Kreis Peiltal, Hesse')"
        )

    def test_a_question_names_its_chain_container_and_candidates(self):
        places = peiltal()
        work = build_worklist(places, [link("Q40", PRISON, 0)])
        text = question_text(work.questions[0], place_names(places))
        assert text == (
            "A record states 'Gerichtsgefängnis Peiltal' within 'Peiltal' "
            "within 'Hesse'. Which place does 'Peiltal', container 1 of "
            "that chain, name?\n"
            "   place 1: 'Kreis Peiltal' (within 'Hesse')\n"
            "   place 2: 'Peiltal' (within 'Kreis Peiltal')"
        )


# -- the answers --------------------------------------------------------------


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return (
            self.responses.pop(0)
            if len(self.responses) > 1
            else (self.responses[0])
        )


class Stopping(ScriptedLlm):
    def __init__(self, response: str, stop_reason: str) -> None:
        super().__init__(response)
        self.stop_reason = stop_reason

    def last_report(self) -> dict:
        return {"stop_reason": self.stop_reason}


class Keeping(ScriptedLlm):
    """A client that keeps answers, as the LLM cache does: its first
    answer is one it kept from an earlier invocation."""

    def __init__(self, *responses: str) -> None:
        super().__init__(*responses)
        self.rejected: list[str] = []

    def reject(self, response: str, reason: str) -> bool:
        self.rejected.append(reason)
        return len(self.rejected) == 1


def line(question, place, confidence=0.9, evidence="fictional") -> str:
    return json.dumps(
        {
            "question": question,
            "place": place,
            "confidence": confidence,
            "evidence": evidence,
        }
    )


DONE = '{"done": true}'


def two_questions() -> list[Question]:
    work = build_worklist(
        peiltal(), [link("Q40", PRISON, 0), link("Q40", PRISON, 1)]
    )
    return work.questions


class TestAnswers:
    def test_an_answer_names_a_candidate_or_none(self):
        hesse, town = two_questions()
        found = parse_answers(
            "\n".join([line(1, 1), line(2, UNCERTAIN), DONE]), [hesse, town]
        )
        assert [(a.question, a.place) for a in found.answers] == [
            (hesse, "Q43"),
            (town, None),
        ]
        assert found.rejected == 0 and found.conflicts == []

    @pytest.mark.parametrize(
        "bad",
        [
            {"question": 3, "place": 1},
            {"question": 0, "place": 1},
            {"question": True, "place": 1},
            {"question": "1", "place": 1},
            {"question": 2, "place": 3},
            {"question": 2, "place": 0},
            {"question": 2, "place": True},
            {"question": 2, "place": "Q41"},
            {"question": 2, "place": "a_within_b"},
            {"question": 2, "place": 1, "confidence": 1.5},
            {"question": 2, "place": 1, "confidence": "high"},
            {"question": 2, "place": 1, "evidence": 7},
        ],
    )
    def test_a_line_outside_the_schema_is_rejected(self, bad):
        found = parse_answers(
            "\n".join([json.dumps(bad), DONE]), two_questions()
        )
        assert found.answers == [] and found.rejected == 1

    def test_two_answers_to_one_question_are_a_conflict(self):
        questions = two_questions()
        found = parse_answers(
            "\n".join([line(2, 1), line(2, 2), line(1, 1), line(1, 1), DONE]),
            questions,
        )
        assert found.conflicts == [questions[1]]
        assert [a.question for a in found.answers] == [questions[0]]

    def test_a_cut_off_answer_raises(self):
        with pytest.raises(ValueError, match="sentinel"):
            parse_answers(line(1, 1), two_questions())
        with pytest.raises(ValueError):
            parse_answers(line(1, 1) + DONE, two_questions(), cut_off=True)

    def test_an_answer_at_the_output_limit_raises(self):
        llm = Stopping("\n".join([line(1, 1), DONE]), "max_tokens")
        with pytest.raises(ValueError):
            adjudicate(llm, two_questions(), peiltal())

    def test_batches_are_numbered_from_one(self):
        llm = ScriptedLlm("\n".join([line(1, 1), DONE]))
        found = adjudicate(llm, two_questions(), peiltal(), batch_questions=1)
        assert len(llm.prompts) == 2
        assert [a.place for a in found.answers] == ["Q43", "Q42"]
        assert all("\n1. A record states" in p for p in llm.prompts)
        assert found.responses == [llm.responses[0]] * 2

    def test_a_kept_malformed_answer_is_asked_again(self):
        llm = Keeping(line(1, 1), "\n".join([line(1, 1), line(2, 2), DONE]))
        found = adjudicate(llm, two_questions(), peiltal())
        assert len(llm.prompts) == 2
        assert [a.place for a in found.answers] == ["Q43", "Q41"]

    def test_a_malformed_retry_fails(self):
        llm = Keeping(line(1, 1), line(1, 1))
        with pytest.raises(ValueError):
            adjudicate(llm, two_questions(), peiltal())
        # the retry is rejected from the cache too
        assert len(llm.rejected) == 2

    def test_an_engine_s_malformed_answer_is_not_asked_again(self):
        llm = ScriptedLlm(line(1, 1))
        with pytest.raises(ValueError):
            adjudicate(llm, two_questions(), peiltal())
        assert len(llm.prompts) == 1


# -- the links answers decide, and the cycle guard ----------------------------


def answered(work: Worklist, *places) -> list[Answer]:
    return [
        Answer(question=question, place=place)
        for question, place in zip(work.questions, places, strict=True)
    ]


class TestLinksToWrite:
    def test_each_link_from_the_link_before(self):
        """The prison within the town Peiltal, and the town (the first
        answer) within Hesse: never the prison within Hesse."""
        work = build_worklist(
            peiltal(), [link("Q40", PRISON, 0), link("Q40", PRISON, 1)]
        )
        hesse, town = work.questions
        planned = links_to_write(
            work,
            [Answer(question=town, place="Q41"), Answer(hesse, "Q43")],
        )
        assert [(c, p) for c, p, _ in planned.edges] == [
            ("Q40", "Q41"),
            ("Q41", "Q43"),
        ]
        assert planned.unresolved == 0

    def test_a_settled_link_places_the_next(self):
        work = build_worklist(
            peiltal(),
            [link("Q40", PRISON, 0, claimed=True), link("Q40", PRISON, 1)],
        )
        # the claimed link to 'Peiltal' is asked (two namesakes); Hesse,
        # named by one place but stated by the record alone, is too
        town, hesse = sorted(work.questions, key=lambda q: q.index)
        planned = links_to_write(
            work, [Answer(town, "Q42"), Answer(hesse, "Q43")]
        )
        assert sorted((c, p) for c, p, _ in planned.edges) == [
            ("Q40", "Q42"),
            ("Q42", "Q43"),
        ]

    def test_an_uncertain_answer_writes_nothing_and_leaves_the_next(self):
        work = build_worklist(
            peiltal(), [link("Q40", PRISON, 0), link("Q40", PRISON, 1)]
        )
        hesse, town = work.questions
        planned = links_to_write(
            work, [Answer(town, None), Answer(hesse, "Q43")]
        )
        assert planned.edges == []
        assert planned.unresolved == 1

    def test_a_link_carries_its_document_and_time(self):
        dated = link(
            "Q40",
            PRISON,
            0,
            claimed=True,
            qualifiers=('{"x": 1}',),
        )
        work = build_worklist(peiltal(), [dated, link("Q40", PRISON, 0, TREE)])
        (question,) = work.questions
        planned = links_to_write(work, [Answer(question, "Q41")])
        assert [found for _, _, found in planned.edges] == [
            dated,
            link("Q40", PRISON, 0, TREE),
        ]


def edge(child, parent):
    return (child, parent, link(child, ("x",), 0))


class TestCycleGuard:
    def test_a_place_within_itself_is_refused(self):
        kept, refused = plan([edge("Q1", "Q1")])
        assert kept == [] and refused == [edge("Q1", "Q1")]

    def test_a_cycle_through_the_graph_is_refused(self):
        graph = {"Q2": ["Q3"], "Q3": ["Q1"]}
        kept, refused = plan([edge("Q1", "Q2")], lambda n: graph.get(n, ()))
        assert refused == [edge("Q1", "Q2")]

    def test_a_cycle_within_the_batch_is_refused(self):
        kept, refused = plan(
            [edge("Q1", "Q2"), edge("Q2", "Q3"), edge("Q3", "Q1")]
        )
        assert [e[:2] for e in kept] == [("Q1", "Q2"), ("Q2", "Q3")]
        assert refused == [edge("Q3", "Q1")]

    def test_a_cycle_through_the_graph_and_the_batch_is_refused(self):
        """Kreis Peiltal lies in Hesse in the graph; Hesse within Fläckfeld,
        then Fläckfeld within Kreis Peiltal, closes a cycle."""
        graph = {"K": ["H"]}
        kept, refused = plan(
            [edge("H", "F"), edge("F", "K")], lambda n: graph.get(n, ())
        )
        assert kept == [edge("H", "F")] and refused == [edge("F", "K")]

    def test_a_graph_holding_a_cycle_is_walked_to_its_end(self):
        graph = {"Q2": ["Q3"], "Q3": ["Q2"]}
        kept, refused = plan([edge("Q1", "Q2")], lambda n: graph.get(n, ()))
        assert kept == [edge("Q1", "Q2")] and refused == []

    def test_a_deep_chain_is_walked(self):
        graph = {f"Q{n}": [f"Q{n + 1}"] for n in range(2, 5000)}
        kept, refused = plan([edge("Q5000", "Q2")], lambda n: graph.get(n, ()))
        assert refused == [edge("Q5000", "Q2")]


# -- the write ----------------------------------------------------------------


def _item(wiki, label: str) -> str:
    item = wiki.wbi.item.new()
    item.labels.set("en", label)
    return item.write().id


class Runs:
    """The run item, made on first need, and how often it was asked for."""

    def __init__(self, wiki) -> None:
        self.wiki = wiki
        self.calls = 0
        self.qid = None

    def __call__(self) -> str:
        self.calls += 1
        if self.qid is None:
            self.qid = _item(self.wiki, "place-hierarchy run fake-model x")
        return self.qid


def statements(wiki, qid):
    from tests.unit.test_containment_claims import VOCABULARY

    return wiki.wbi.item.get(qid).claims.get(VOCABULARY["part_of"]) or []


def cited(statement, key):
    from tests.unit.test_containment_claims import VOCABULARY

    return [
        [
            snak.datavalue["value"]["id"]
            for snak in reference.snaks.get(VOCABULARY[key]) or []
        ]
        for reference in statement.references.references
    ]


def qualifier_keys(statement):
    from tests.unit.test_containment_claims import VOCABULARY

    names = {pid: key for key, pid in VOCABULARY.items()}
    return sorted(names[pid] for pid in statement.qualifiers.qualifiers)


@pytest.fixture
def written():
    from yizkr.pipeline.writer import BatchWriter

    wiki = PlaceWiki()
    child, parent = _item(wiki, "Koshnovka"), _item(wiki, "Kaldovsky District")
    artifact = _item(wiki, "1941 death certificate")
    return wiki, BatchWriter(wiki), child, parent, artifact


def _dated(year=1941) -> StatedLink:
    (found, _) = stated_links(
        koshnovka_batch(
            _part_of(*IN_1941, DEATH, [_year("point_in_time", year)])
        ),
        CREATED,
        DEATH,
    )
    return found


class TestWriteLink:
    def test_a_record_s_link_has_no_page_and_no_time(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        bare = link(child, ("Kaldovsky District",), 0, sha1=DEATH)
        assert write_link(writer, child, parent, bare, artifact, runs) == (
            WRITTEN
        )
        (statement,) = statements(wiki, child)
        assert statement.mainsnak.datavalue["value"]["id"] == parent
        assert statement.rank.value == "normal"
        assert qualifier_keys(statement) == []
        assert cited(statement, "stated_in") == [[artifact], []]
        assert cited(statement, "inferred_from_extraction_run") == [
            [],
            [runs.qid],
        ]
        # no page, no excerpt
        assert all(
            set(reference.get_json()["snaks"]) <= {"P2", "P5"}
            for reference in statement.references.references
        )

    def test_a_claim_s_link_carries_its_time(self, written):
        wiki, writer, child, parent, artifact = written
        write_link(writer, child, parent, _dated(), artifact, Runs(wiki))
        (statement,) = statements(wiki, child)
        assert qualifier_keys(statement) == ["point_in_time"]

    def test_a_link_written_already_is_not_written_again(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        write_link(writer, child, parent, _dated(), artifact, runs)
        writes = wiki.wbi.item.writes
        assert write_link(writer, child, parent, _dated(), artifact, runs) == (
            PRESENT
        )
        assert wiki.wbi.item.writes == writes and runs.calls == 1

    def test_another_source_s_equal_statement_gains_its_references(
        self, written
    ):
        wiki, writer, child, parent, artifact = written
        other = _item(wiki, "1984 family tree")
        runs = Runs(wiki)
        write_link(writer, child, parent, _dated(), other, runs)
        assert write_link(writer, child, parent, _dated(), artifact, runs) == (
            REFERENCED
        )
        (statement,) = statements(wiki, child)
        assert [found for found in cited(statement, "stated_in") if found] == [
            [other],
            [artifact],
        ]

    def test_a_dated_and_an_undated_link_coexist(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        bare = link(child, ("Kaldovsky District",), 0, sha1=DEATH)
        write_link(writer, child, parent, _dated(), artifact, runs)
        assert write_link(writer, child, parent, bare, artifact, runs) == (
            WRITTEN
        )
        assert sorted(
            qualifier_keys(statement) for statement in statements(wiki, child)
        ) == [[], ["point_in_time"]]


# -- the CSV and the command --------------------------------------------------


def _graph_and_answers():
    places = peiltal()
    work = build_worklist(
        places, [link("Q40", PRISON, 0), link("Q40", PRISON, 1)]
    )
    hesse, town = work.questions
    found = Adjudication(
        answers=[
            Answer(hesse, "Q43", 0.9, "the state"),
            Answer(town, None, 0.4, "cannot tell"),
        ]
    )
    return places, work, found


class TestCsv:
    def test_one_row_per_answer(self, tmp_path):
        places, _, found = _graph_and_answers()
        path = tmp_path / "places.csv"
        assert export_places_csv(found, places, path) == 2
        with open(path, newline="") as handle:
            first, second = list(csv.DictReader(handle))
        assert first["qid_a"] == "Q40"
        assert first["label_a"] == "Gerichtsgefängnis Peiltal"
        assert (first["qid_b"], first["label_b"]) == ("Q43", "Hesse")
        assert first["verdict"] == SAME
        assert json.loads(first["chain"]) == list(PRISON)
        assert first["container"] == "1"
        assert first["decision"] == ""
        assert (second["qid_b"], second["verdict"]) == ("", UNCERTAIN)
        assert "a_within_b" not in path.read_text()

    def test_only_approved_same_rows_of_asked_questions_count(self, tmp_path):
        places, work, found = _graph_and_answers()
        path = tmp_path / "places.csv"
        export_places_csv(found, places, path)
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert approved_answers(rows, work) == []
        for row in rows:
            row["decision"] = "approve"
        (answer,) = approved_answers(rows, work)
        assert (answer.question, answer.place) == (work.questions[0], "Q43")
        # a row whose verdict is not `same`, though its B is a candidate
        rows[1]["qid_b"] = "Q42"
        (answer,) = approved_answers(rows, work)
        assert answer.question == work.questions[0]
        # a B that is no candidate, or a question the graph no longer asks
        rows[0]["qid_b"] = "Q41"
        assert approved_answers(rows, work) == []
        rows[0]["qid_b"], rows[0]["container"] = "Q43", "0"
        assert approved_answers(rows, work) == []


class TestCommand:
    """`yizkr reconcile-places --run DIR` asks the questions the links its
    records state give, and `--apply` writes the approved answers."""

    def _args(self, tmp_path, **given):
        import argparse

        return argparse.Namespace(
            env_file=None,
            apply=given.get("apply"),
            llm="anthropic",
            out=tmp_path / "places.csv",
            run=given.get("run"),
        )

    def _graph(self, monkeypatch, places):
        from yizkr import cli

        monkeypatch.setattr(cli.Config, "load", lambda env_file: None)
        monkeypatch.setattr(cli, "WikiClient", lambda config: None)
        monkeypatch.setattr(hierarchy, "fetch_places", lambda c, cf: places)

    def test_run_directories_give_the_questions(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli
        from yizkr.pipeline import llm

        _written(
            tmp_path / "run1" / "prison",
            [_entity("e1", "Gerichtsgefängnis Peiltal", list(PRISON))],
            {"e1": "Q40"},
            MARRIAGE,
        )
        _written(
            tmp_path / "run2" / "prison",
            [_entity("e1", "Gerichtsgefängnis Peiltal", ["Kreis Rekau"])],
            {"e1": "Q40"},
            TREE,
        )
        self._graph(monkeypatch, peiltal())
        asked = []

        def adjudicate(client, questions, found):
            asked.extend(question.key for question in questions)
            return Adjudication(conflicts=[questions[0]], rejected=2)

        monkeypatch.setattr(hierarchy, "adjudicate", adjudicate)
        monkeypatch.setattr(llm, "make_client", lambda name: None)
        args = self._args(tmp_path, run=[tmp_path / "run1", tmp_path / "run2"])
        assert cli.cmd_reconcile_places(args) == 0
        assert asked == [("Q40", PRISON, 1), ("Q40", PRISON, 0)]
        out = capsys.readouterr().out
        assert "links their records state: 3, 3 of them by a record" in out
        assert "left out, with no place to name: 1" in out
        assert (
            "left out: 1 question(s) answered twice differently, 2 answer "
            "line(s)" in out
        )

    def test_with_nothing_to_ask_it_still_counts(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli

        _written(
            tmp_path / "run" / "prison",
            [_entity("e1", "Gerichtsgefängnis Peiltal", ["Kreis Rekau"])],
            {"e1": "Q40"},
            MARRIAGE,
        )
        self._graph(monkeypatch, peiltal())
        assert (
            cli.cmd_reconcile_places(
                self._args(tmp_path, run=[tmp_path / "run"])
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "left out, with no place to name: 1; questions: 0" in out
        assert "nothing to ask" in out

    def test_without_a_run_nothing_is_stated(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli

        self._graph(monkeypatch, peiltal())
        assert cli.cmd_reconcile_places(self._args(tmp_path)) == 0
        assert "questions: 0" in capsys.readouterr().out

    def test_run_takes_several_directories(self):
        from yizkr import cli

        args = cli.build_parser().parse_args(
            ["reconcile-places", "--run", "a", "--run", "b"]
        )
        assert args.run == [Path("a"), Path("b")]


class ApplyWiki(PlaceWiki):
    """The containment tests' fake instance, with the media index the
    command finds artifacts by and the label lookup it finds its run item
    by."""

    def find_item_by_label(self, label):
        for qid, item in self.wbi.item.store.items():
            if str(item.labels.get("en") or "") == label:
                return qid
        return None


class TestApply:
    def _setup(self, tmp_path, monkeypatch, decision="approve"):
        wiki = ApplyWiki()
        qids = {
            label: _item(wiki, label)
            for label in (
                "Gerichtsgefängnis Peiltal",
                "Peiltal",
                "Kreis Peiltal",
                "Hesse",
                "marriage certificate",
            )
        }
        wiki.media = {MARRIAGE: qids["marriage certificate"]}
        found = [
            PlaceRecord(
                qid=qids["Gerichtsgefängnis Peiltal"],
                label="Gerichtsgefängnis Peiltal",
            ),
            PlaceRecord(
                qid=qids["Peiltal"],
                label="Peiltal",
                parents=[qids["Kreis Peiltal"]],
            ),
            PlaceRecord(
                qid=qids["Kreis Peiltal"],
                label="Kreis Peiltal",
                aliases=["Peiltal"],
                parents=[qids["Hesse"]],
            ),
            PlaceRecord(qid=qids["Hesse"], label="Hesse"),
        ]
        monkeypatch.setattr(hierarchy, "fetch_places", lambda c, cf: found)
        links = [
            link(qids["Gerichtsgefängnis Peiltal"], PRISON, index)
            for index in (0, 1)
        ]
        work = build_worklist(found, links)
        by_index = {q.index: q for q in work.questions}
        answers = Adjudication(
            answers=[
                Answer(by_index[0], qids["Peiltal"]),
                Answer(by_index[1], qids["Hesse"]),
            ]
        )
        path = tmp_path / "places.csv"
        export_places_csv(answers, found, path)
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row["decision"] = decision
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=hierarchy.CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return wiki, qids, links, path

    def test_approved_answers_write_the_stated_links(
        self, tmp_path, monkeypatch
    ):
        wiki, qids, links, path = self._setup(tmp_path, monkeypatch)
        summary = hierarchy.apply_answers(
            wiki, None, path, links, "curator-reviewed"
        )
        assert summary["written"] == 2 and summary["cycles"] == 0
        prison, town = qids["Gerichtsgefängnis Peiltal"], qids["Peiltal"]
        (statement,) = statements(wiki, prison)
        assert statement.mainsnak.datavalue["value"]["id"] == town
        assert cited(statement, "stated_in")[0] == [
            qids["marriage certificate"]
        ]
        assert cited(statement, "inferred_from_extraction_run")[1] == [
            summary["run_qid"]
        ]
        (statement,) = statements(wiki, town)
        assert statement.mainsnak.datavalue["value"]["id"] == qids["Hesse"]
        again = hierarchy.apply_answers(
            wiki, None, path, links, "curator-reviewed"
        )
        assert (again["written"], again["skipped"]) == (0, 2)

    def test_unapproved_rows_write_nothing(self, tmp_path, monkeypatch):
        wiki, qids, links, path = self._setup(
            tmp_path, monkeypatch, decision=""
        )
        summary = hierarchy.apply_answers(
            wiki, None, path, links, "curator-reviewed"
        )
        assert summary["written"] == 0 and summary["run_qid"] is None

    def test_a_cycle_through_the_graph_is_refused(self, tmp_path, monkeypatch):
        wiki, qids, links, path = self._setup(tmp_path, monkeypatch)
        # the graph puts Hesse inside the town: Peiltal within Hesse would
        # close a cycle
        found = hierarchy.fetch_places(None, None)
        found[3].parents = [qids["Peiltal"]]
        summary = hierarchy.apply_answers(
            wiki, None, path, links, "curator-reviewed"
        )
        assert summary["cycles"] == 1 and summary["written"] == 1
        assert any("would close a cycle" in note for note in summary["notes"])


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


class TestTheGraphQuery:
    def test_the_graph_s_places_carry_their_descriptions_and_parents(
        self, monkeypatch
    ):
        rows = [
            {
                "place": {"value": "http://x/entity/Q41"},
                "placeLabel": {"value": "Peiltal"},
                "aliases": {"value": ""},
                "parents": {"value": "Q42|Q9"},
                "description": {"value": "town in Hesse"},
            }
        ]
        sent = []
        monkeypatch.setattr(
            hierarchy, "_run_query", lambda c, q: sent.append(q) or rows
        )

        class Client:
            def vocabulary(self):
                return (
                    {"instance_of": "P1", "part_of": "P2", "location": "P3"},
                    {"place": "Q5"},
                )

        class Config:
            concept_uri = "http://x"

        (place,) = hierarchy.fetch_places(Client(), Config())
        assert (place.qid, place.parents, place.description) == (
            "Q41",
            ["Q42", "Q9"],
            "town in Hesse",
        )
        (query,) = sent
        # its English description, and every statement's value but a
        # deprecated one's, at any time
        assert "(SAMPLE(?about) AS ?description)" in query
        assert "?place <http://schema.org/description> ?about ." in query
        assert 'FILTER(LANG(?about) = "en")' in query
        assert "VALUES ?link { wd:P2 wd:P3 }" in query
        assert "?edge wikibase:rank wikibase:DeprecatedRank" in query
