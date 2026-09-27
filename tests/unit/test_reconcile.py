"""Offline tests for entity reconciliation (ADR-0007). No instance, no LLM."""

import json

import pytest

from yizkr.claims import ClaimBatch, EntityRecord, LocalRef, QidRef
from yizkr.pipeline.reconcile import (
    PlaceContainment,
    Proposal,
    RosterEntity,
    _batch_pair_facts,
    _roster_pair_facts,
    adjudicate,
    apply_equivalences,
    block_candidates,
    entity_facts,
    export_equivalences_csv,
    impossible_pair,
    place_containment_proposals,
    ratify_equivalence_verdicts,
    read_approved_equivalences,
)

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-13T09:00:00Z",
}


def entities() -> list[EntityRecord]:
    return [
        EntityRecord(
            id="e1",
            label="Hildegard Löbig",
            aliases=["Hilde"],
            kind="human",
            summary="married Sebastian Löbig in 1905",
        ),
        EntityRecord(id="e2", label="Flörburg", kind="place"),
    ]


def roster() -> list[RosterEntity]:
    return [
        RosterEntity(
            qid="Q40",
            label="Hildegard Laudler",
            kind="human",
            birth="1876-03-19",
            spouses=["Sebastian Löbig"],
        ),
        # a different person who shares only the surname
        RosterEntity(qid="Q77", label="Ursula Löbig", kind="human"),
        RosterEntity(qid="Q90", label="Flörburg, Germany", kind="place"),
        RosterEntity(qid="Q99", label="Unrelated Person", kind="human"),
    ]


def batch(claims=(), entity_list=None) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": list(claims),
            "entities": [e.model_dump() for e in (entity_list or entities())],
        }
    )


def spouse_claim() -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Hildegard Löbig",
        },
        "property": "residence",
        "value": {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": "e2",
                "label": "Flörburg",
            },
        },
        "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
    }


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


def name_claim(prop: str, text: str, subject: str = "e1") -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": subject,
            "label": "Hildegard Löbig",
        },
        "property": prop,
        "value": {
            "type": "monolingual",
            "value": {"text": text, "language": "en"},
        },
        "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
    }


class TestTypedNameEvidence:
    """ADR-0011: typed names surface as adjudication evidence, both sides."""

    def test_roster_facts_include_typed_names(self):
        entity = RosterEntity(
            qid="Q40",
            label="Hildegard Löbig",
            kind="human",
            birth_names=["Hildegard Laudler"],
            married_names=["Hildegard Löbig"],
            nicknames=["Hilde"],
        )
        facts = entity.facts()
        assert "birth name: Hildegard Laudler" in facts
        assert "married name: Hildegard Löbig" in facts
        assert "nickname: Hilde" in facts

    def test_entity_facts_renders_monolingual_names_plainly(self):
        facts = entity_facts(
            batch(claims=[name_claim("birth_name", "Hildegard Laudler")]),
            "e1",
        )
        assert "birth_name: Hildegard Laudler" in facts
        assert "language=" not in facts  # not a pydantic repr

    def test_batch_name_claim_reaches_the_adjudication_prompt(self):
        llm = ScriptedLlm(json.dumps({"done": True, "proposals_emitted": 0}))
        source = batch(claims=[name_claim("birth_name", "Hildegard Laudler")])
        adjudicate(
            llm, entities(), block_candidates(entities(), roster()), source
        )
        assert "Hildegard Laudler" in llm.prompts[0]


class TestBlocking:
    def test_kind_and_token_overlap(self):
        candidates = block_candidates(entities(), roster())
        assert [c.qid for c in candidates["e1"]] == ["Q40", "Q77"]
        assert [c.qid for c in candidates["e2"]] == ["Q90"]

    def test_no_candidates_no_entry(self):
        lone = [EntityRecord(id="e9", label="Zzyzx Qwx", kind="human")]
        assert block_candidates(lone, roster()) == {}

    def test_prefix_forms_block(self):
        """'Gus' must reach the adjudicator against 'Gustave …' —
        clipped forms are how family documents refer to people."""
        gus = [EntityRecord(id="e5", label="Gus", kind="human")]
        extended = roster() + [
            RosterEntity(qid="Q124", label="Gustave Lobig", kind="human")
        ]
        assert [c.qid for c in block_candidates(gus, extended)["e5"]] == [
            "Q124"
        ]

    def test_graph_pair_blocking_is_also_prefix_aware(self):
        """Same rule for roster x roster pairs: 'Gus' vs 'Gustave …'
        must be adjudicated, not silently unblocked."""
        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(qid="Q170", label="Gus", kind="human"),
            RosterEntity(qid="Q124", label="Gustave Lobig", kind="human"),
        ]
        response = "\n".join(
            [
                json.dumps(
                    {
                        "entity_id": "Q170",
                        "qid": "Q124",
                        "verdict": "uncertain",
                        "confidence": 0.5,
                        "evidence": "clipped form",
                    }
                ),
                json.dumps({"done": True, "proposals_emitted": 1}),
            ]
        )
        llm = ScriptedLlm(response)
        proposals = propose_graph_pairs(llm, pair_roster)
        assert [(p.entity_id, p.qid) for p in proposals] == [("Q170", "Q124")]
        assert "Gustave Lobig" in llm.prompts[0]


class TestAdjudication:
    def response(self, extra_lines=()):
        lines = [
            json.dumps(
                {
                    "entity_id": "e1",
                    "qid": "Q40",
                    "verdict": "same",
                    "confidence": 0.95,
                    "evidence": "same spouse Sebastian Löbig; married name",
                }
            ),
            json.dumps(
                {
                    "entity_id": "e1",
                    "qid": "Q77",
                    "verdict": "distinct",
                    "confidence": 0.9,
                    "evidence": "no relational overlap",
                }
            ),
            *extra_lines,
        ]
        lines.append(
            json.dumps({"done": True, "proposals_emitted": len(lines) - 1})
        )
        return "\n".join(lines)

    def test_valid_proposals_survive(self):
        llm = ScriptedLlm(self.response())
        proposals = adjudicate(
            llm, entities(), block_candidates(entities(), roster()), batch()
        )
        assert [(p.entity_id, p.qid, p.verdict) for p in proposals] == [
            ("e1", "Q40", "same"),
            ("e1", "Q77", "distinct"),
        ]
        # the prompt carries relational facts from the batch and roster
        assert "Sebastian" in llm.prompts[0]

    def test_rulings_on_unblocked_pairs_are_dropped(self):
        invented = json.dumps(
            {
                "entity_id": "e1",
                "qid": "Q99",
                "verdict": "same",
                "confidence": 1,
            }
        )
        llm = ScriptedLlm(self.response(extra_lines=[invented]))
        proposals = adjudicate(
            llm, entities(), block_candidates(entities(), roster()), batch()
        )
        assert all(p.qid != "Q99" for p in proposals)

    def test_missing_sentinel_raises(self):
        llm = ScriptedLlm(
            json.dumps({"entity_id": "e1", "qid": "Q40", "verdict": "same"})
        )
        with pytest.raises(ValueError, match="sentinel"):
            adjudicate(
                llm,
                entities(),
                block_candidates(entities(), roster()),
                batch(),
            )

    def test_no_candidates_no_call(self):
        llm = ScriptedLlm("must not be called")
        assert adjudicate(llm, entities(), {}, batch()) == []
        assert llm.prompts == []

    def test_batched_adjudication_bounds_each_call(self):
        """The response size is bounded like chunk output (ADR-0006, one
        level up): pairs split across calls, each with its own sentinel,
        and each prompt carries only its batch's entities."""

        class QueueLlm(ScriptedLlm):
            def __init__(self, responses):
                self.responses = list(responses)
                self.prompts = []

            def complete(self, prompt, image_paths=()):
                self.prompts.append(prompt)
                return self.responses.pop(0)

        first = "\n".join(
            [
                json.dumps(
                    {"entity_id": "e1", "qid": "Q40", "verdict": "same"}
                ),
                json.dumps(
                    {"entity_id": "e1", "qid": "Q77", "verdict": "distinct"}
                ),
                json.dumps({"done": True, "proposals_emitted": 2}),
            ]
        )
        second = "\n".join(
            [
                json.dumps(
                    {"entity_id": "e2", "qid": "Q90", "verdict": "same"}
                ),
                json.dumps({"done": True, "proposals_emitted": 1}),
            ]
        )
        llm = QueueLlm([first, second])
        proposals = adjudicate(
            llm,
            entities(),
            block_candidates(entities(), roster()),
            batch(),
            batch_pairs=2,  # e1 has 2 candidates, e2 has 1 -> two calls
        )
        assert len(llm.prompts) == 2
        assert "Flörburg" not in llm.prompts[0]  # e2 not in batch 1
        assert "Hildegard" not in llm.prompts[1]  # e1 not in batch 2
        assert [(p.entity_id, p.qid) for p in proposals] == [
            ("e1", "Q40"),
            ("e1", "Q77"),
            ("e2", "Q90"),
        ]


class TestEquivalencesCsv:
    def proposals(self):
        return [
            Proposal(
                entity_id="e1",
                qid="Q40",
                verdict="same",
                confidence=0.95,
                evidence="same spouse",
            ),
            Proposal(
                entity_id="e1", qid="Q77", verdict="distinct", confidence=0.9
            ),
        ]

    def test_round_trip_only_approved_rows_take_effect(self, tmp_path):
        path = tmp_path / "equivalences.csv"
        export_equivalences_csv(self.proposals(), entities(), roster(), path)
        content = path.read_text()
        assert "Hildegard Laudler" in content and "same spouse" in content
        # machine verdict alone grants nothing
        assert read_approved_equivalences(path) == {}
        text = content.replace("same spouse,", "same spouse,approve")
        path.write_text(text)
        assert read_approved_equivalences(path) == {"e1": "Q40"}

    def test_aliases_visible_and_prunable_at_review(self, tmp_path):
        """An approval is also an alias contribution (the writer appends
        the document's aliases to the matched item), so the CSV must show
        them and honor the reviewer's pruning — a form's MOTHER label
        must be deletable before it pollutes the graph."""
        from yizkr.pipeline.reconcile import read_reviewed_aliases

        path = tmp_path / "equivalences.csv"
        # a multi-word alias, which review must keep whole, and a role label
        as_written = "Hildegard Löbig geborene Laudler"
        noisy = [
            EntityRecord(
                id="e1",
                label="Hildegard Löbig",
                aliases=[as_written, "MOTHER"],
                kind="human",
            )
        ]
        export_equivalences_csv(self.proposals(), noisy, roster(), path)
        content = path.read_text()
        assert f"{as_written}|MOTHER" in content  # visible to reviewer
        # the reviewer prunes the role label and approves
        content = content.replace(f"{as_written}|MOTHER", as_written)
        content = content.replace("same spouse,", "same spouse,approve")
        path.write_text(content)
        approved = read_approved_equivalences(path)
        reviewed = read_reviewed_aliases(path)
        assert reviewed == {"e1": [as_written]}
        applied = apply_equivalences(
            batch(entity_list=noisy), approved, reviewed
        )
        (entity,) = applied.entities
        assert entity.qid == "Q40"
        assert entity.aliases == [as_written]  # MOTHER pruned

    def test_old_csvs_without_aliases_column_still_apply(self, tmp_path):
        from yizkr.pipeline.reconcile import read_reviewed_aliases

        path = tmp_path / "old.csv"
        path.write_text(
            "entity_id,qid,verdict,confidence,evidence,decision\n"
            "e1,Q40,same,0.9,same spouse,approve\n"
        )
        assert read_approved_equivalences(path) == {"e1": "Q40"}
        assert read_reviewed_aliases(path) == {}

    def test_conflicting_approvals_raise(self, tmp_path):
        path = tmp_path / "equivalences.csv"
        export_equivalences_csv(self.proposals(), entities(), roster(), path)
        lines = path.read_text().splitlines()
        rows = [line + "approve" for line in lines[1:]]
        path.write_text("\n".join([lines[0], *rows]))
        with pytest.raises(ValueError, match="both"):
            read_approved_equivalences(path)


class TestApplyEquivalences:
    def test_refs_rewritten_and_qid_recorded(self):
        original = batch(claims=[spouse_claim()])
        applied = apply_equivalences(original, {"e1": "Q40"})
        claim = applied.claims[0]
        assert claim.subject == QidRef(qid="Q40")
        # e2 unapproved: stays local
        assert isinstance(claim.value.value, LocalRef)
        hildegard = next(e for e in applied.entities if e.id == "e1")
        assert hildegard.qid == "Q40"
        # original untouched
        assert isinstance(original.claims[0].subject, LocalRef)
        assert original.entities[0].qid is None

    def test_identical_claims_collapse_after_equivalence(self):
        # two document entities (duplicated records in the source) both
        # approved to one item leave byte-identical claims: collapse
        twin = dict(
            spouse_claim(), subject={"kind": "local", "local_id": "e3"}
        )
        entity_list = [
            *entities(),
            EntityRecord(id="e3", label="Hildegard Löbig", kind="human"),
        ]
        original = batch(
            claims=[spouse_claim(), twin], entity_list=entity_list
        )
        applied = apply_equivalences(original, {"e1": "Q40", "e3": "Q40"})
        assert len(applied.claims) == 1
        assert applied.claims[0].subject == QidRef(qid="Q40")

    def test_distinct_claims_survive_equivalence(self):
        # same subject item but different values must both remain
        other = dict(spouse_claim(), value={"type": "string", "value": "x"})
        applied = apply_equivalences(
            batch(claims=[spouse_claim(), other]), {"e1": "Q40"}
        )
        assert len(applied.claims) == 2


# -- impossibility filter (ADR-0017) -----------------------------------------


def rf(**kw) -> RosterEntity:
    """A roster entity with defaults; kw overrides."""
    kw.setdefault("kind", "human")
    return RosterEntity(
        qid=kw.pop("qid", "Q1"), label=kw.pop("label", "X"), **kw
    )


def _facts(entity: RosterEntity):
    return _roster_pair_facts(entity)


class TestImpossiblePair:
    def test_kinship_edge_rules_out_a_same_name_pair(self):
        # a "Jr." case: father and son share a name but a father edge on the
        # son (naming the father's QID) proves they are two people.
        son = rf(qid="Q1", label="A B", father_qids=["Q2"])
        father = rf(qid="Q2", label="A B")
        assert impossible_pair(_facts(son), _facts(father)) == "kinship-edge"
        # symmetric — direction of the edge does not matter
        assert impossible_pair(_facts(father), _facts(son)) == "kinship-edge"

    def test_spouse_edge_rules_out(self):
        a = rf(qid="Q1", label="Chris", spouse_qids=["Q2"])
        b = rf(qid="Q2", label="Chris")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"

    @pytest.mark.parametrize(
        "field_name",
        [
            "father_qids",
            "mother_qids",
            "spouse_qids",
            "child_qids",
            "sibling_qids",
        ],
    )
    def test_every_kinship_relation_feeds_the_filter(self, field_name):
        a = rf(qid="Q1", label="A B", **{field_name: ["Q2"]})
        b = rf(qid="Q2", label="A B")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"

    def test_unrelated_same_name_is_not_impossible(self):
        a = rf(qid="Q1", label="Ursula Löbig")
        b = rf(qid="Q2", label="Ursula Löbig")
        assert impossible_pair(_facts(a), _facts(b)) is None

    def test_place_containment_by_qid(self):
        # Q66 (a prison) is transitively inside Q10 (Germany)
        containment = PlaceContainment({"Q66": {"Q10"}}, {"Q10": "Germany"})
        prison = rf(qid="Q66", label="Peiltal court prison", kind="place")
        germany = rf(qid="Q10", label="Germany", kind="place")
        assert (
            impossible_pair(
                _facts(prison), _facts(germany), containment=containment
            )
            == "place-containment"
        )
        # a place NOT in the closure with the other is a live candidate
        other = rf(qid="Q11", label="Germany (other)", kind="place")
        assert (
            impossible_pair(
                _facts(prison), _facts(other), containment=containment
            )
            is None
        )

    def test_place_containment_by_label_for_unwritten_batch_entity(self):
        # a new "Germany" (no QID) vs the graph's Peiltal court prison,
        # which the graph already places inside a "Germany".
        containment = PlaceContainment({"Q66": {"Q10"}}, {"Q10": "Germany"})
        batch_germany = _batch_pair_facts(
            EntityRecord(id="e1", label="Germany", kind="place"), None
        )
        prison = _facts(
            rf(qid="Q66", label="Peiltal court prison, Germany", kind="place")
        )
        assert impossible_pair(
            batch_germany, prison, containment=containment
        ) == ("place-containment")

    def test_containment_only_applies_between_places(self):
        containment = PlaceContainment({"Q66": {"Q10"}}, {"Q10": "Germany"})
        a = rf(qid="Q66", label="X", kind="human")
        b = rf(qid="Q10", label="X", kind="human")
        # same closure, but humans — never a containment prune
        assert (
            impossible_pair(_facts(a), _facts(b), containment=containment)
            is None
        )

    def test_equal_labels_are_never_pruned_by_containment(self):
        # eponymous nesting: the town Rekau sits inside the urban district
        # Rekau. A batch "Rekau" vs a graph "Rekau" that happens to be
        # contained in a same-named place may be the SAME place — never prune.
        containment = PlaceContainment({"Q70": {"Q71"}}, {"Q71": "Rekau"})
        batch_rekau = _batch_pair_facts(
            EntityRecord(id="e1", label="Rekau", kind="place"), None
        )
        town = _facts(rf(qid="Q70", label="Rekau", kind="place"))
        assert (
            impossible_pair(batch_rekau, town, containment=containment) is None
        )

    def test_date_disagreement_is_never_impossible(self):
        # contradictory dates coexist (ADR-0002); they are surfaced to the
        # human, never pruned — the filter has no temporal rule at all.
        a = rf(qid="Q1", label="A B", birth="1900", death="1910")
        b = rf(qid="Q2", label="A B", birth="1936", death="1990")
        assert impossible_pair(_facts(a), _facts(b)) is None

    def test_symmetry_of_truthiness(self):
        son = rf(qid="Q1", label="Gustav Löbig", father_qids=["Q2"])
        father = rf(qid="Q2", label="Sebastian Löbig")
        assert bool(impossible_pair(_facts(son), _facts(father))) == bool(
            impossible_pair(_facts(father), _facts(son))
        )

    def test_predicate_does_no_io(self):
        # pure function — no client, no network; a plain call returns
        a = rf(qid="Q1", label="Gustav Löbig", father_qids=["Q2"])
        b = rf(qid="Q2", label="Sebastian Löbig")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"


# A son and his father whose names block each other: one name for both (a
# "Jr." case, with tokens long enough to block), and the universe's Gustav
# and his father Sebastian, who share only the surname.
FATHER_AND_SON = pytest.mark.parametrize(
    ("son", "father"),
    [("Xxx Yyy", "Xxx Yyy"), ("Gustav Löbig", "Sebastian Löbig")],
    ids=["same-name", "shared-surname"],
)


class TestBlockingWithImpossibility:
    def test_containment_candidate_is_pruned_from_blocking(self):
        places = [EntityRecord(id="e1", label="Germany", kind="place")]
        roster_places = [
            RosterEntity(
                qid="Q66", label="Peiltal court prison, Germany", kind="place"
            )
        ]
        containment = PlaceContainment({"Q66": {"Q10"}}, {"Q10": "Germany"})
        # without the closure the tenuous "Germany" token still blocks it
        assert block_candidates(places, roster_places)["e1"][0].qid == "Q66"
        # with it, the impossible pair is pruned -> e1 has no candidates
        assert (
            block_candidates(places, roster_places, containment=containment)
            == {}
        )

    @FATHER_AND_SON
    def test_batch_kinship_qidref_prunes_candidate(self, son, father):
        # a batch entity whose father is ALREADY the graph candidate (a
        # QidRef) cannot be that same candidate — pruned before adjudication.
        entity = EntityRecord(id="e1", label=son, kind="human")
        b = batch(
            claims=[
                {
                    "subject": {"kind": "local", "local_id": "e1"},
                    "property": "father",
                    "value": {
                        "type": "item",
                        "value": {"kind": "qid", "qid": "Q84"},
                    },
                    "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
                }
            ],
            entity_list=[entity],
        )
        roster_people = [RosterEntity(qid="Q84", label=father, kind="human")]
        assert block_candidates([entity], roster_people, batch=b) == {}
        # without the batch (no resolved father) the pair survives to review
        assert block_candidates([entity], roster_people)["e1"][0].qid == "Q84"

    @FATHER_AND_SON
    def test_graph_pair_pruning_drops_impossible_pairs(self, son, father):
        # roster x roster: a father edge between two same-name items (or two
        # that share only a surname) means the graph-dedup pass never even
        # asks the model about them.
        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(
                qid="Q1", label=son, kind="human", father_qids=["Q2"]
            ),
            RosterEntity(qid="Q2", label=father, kind="human"),
        ]

        class _Boom:
            model_identifier = "fake"

            def complete(self, *a, **k):  # must never be called
                raise AssertionError(
                    "adjudicator called on an impossible pair"
                )

        assert propose_graph_pairs(_Boom(), pair_roster) == []
        # without the father edge the names still block: the pair reaches
        # the adjudicator
        unlinked = [
            RosterEntity(qid="Q1", label=son, kind="human"),
            RosterEntity(qid="Q2", label=father, kind="human"),
        ]
        llm = ScriptedLlm(json.dumps({"done": True, "proposals_emitted": 0}))
        propose_graph_pairs(llm, unlinked)
        assert len(llm.prompts) == 1


class TestContainmentHandoff:
    def _proposal(self, **kw) -> Proposal:
        base = dict(
            entity_id="Q66",
            qid="Q10",
            verdict="distinct",
            evidence="prison in Germany",
        )
        base.update(kw)
        return Proposal(**base)

    def _roster(self):
        return [
            RosterEntity(
                qid="Q66", label="Peiltal court prison", kind="place"
            ),
            RosterEntity(qid="Q10", label="Germany", kind="place"),
        ]

    def test_distinct_containment_becomes_a_within_b_row(self):
        rows = place_containment_proposals(
            [self._proposal(contained_in="Q10")], self._roster()
        )
        assert len(rows) == 1
        assert rows[0]["qid_a"] == "Q66"  # child
        assert rows[0]["qid_b"] == "Q10"  # container/parent
        assert rows[0]["verdict"] == "a_within_b"
        assert rows[0]["decision"] == ""

    def test_container_can_be_the_first_member(self):
        # A contains B: entity_id=Q10 is the container
        rows = place_containment_proposals(
            [self._proposal(entity_id="Q10", qid="Q66", contained_in="Q10")],
            self._roster(),
        )
        assert rows[0]["qid_a"] == "Q66" and rows[0]["qid_b"] == "Q10"

    def test_no_containment_field_no_row(self):
        assert (
            place_containment_proposals([self._proposal()], self._roster())
            == []
        )

    def test_same_verdict_never_a_containment_row(self):
        assert (
            place_containment_proposals(
                [self._proposal(verdict="same", contained_in="Q10")],
                self._roster(),
            )
            == []
        )

    def test_container_must_be_one_of_the_pair(self):
        assert (
            place_containment_proposals(
                [self._proposal(contained_in="Q999")], self._roster()
            )
            == []
        )

    def test_non_place_pair_never_produces_a_row(self):
        # a stray contained_in on a person/org pair must not reach
        # reconcile-places (which would write a bogus part_of edge)
        people = [
            RosterEntity(qid="Q66", label="Gustav Löbig", kind="human"),
            RosterEntity(qid="Q10", label="Gustave Lobig", kind="human"),
        ]
        assert (
            place_containment_proposals(
                [self._proposal(contained_in="Q10")], people
            )
            == []
        )

    def test_unknown_qid_pair_never_produces_a_row(self):
        # both members must be known roster places
        assert (
            place_containment_proposals(
                [self._proposal(contained_in="Q10")], []
            )
            == []
        )


class TestRatifyEquivalenceVerdicts:
    """`accept-all` on an equivalences CSV ratifies the machine per row
    (same->approve, distinct->reject, uncertain->blank) — never a blanket
    merge — and leaves a self-contradicting entity blank."""

    def test_maps_each_verdict_to_its_decision(self):
        rows = [
            {
                "entity_id": "e1",
                "qid": "Q10",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e2",
                "qid": "Q20",
                "verdict": "distinct",
                "decision": "",
            },
            {
                "entity_id": "e3",
                "qid": "Q30",
                "verdict": "uncertain",
                "decision": "",
            },
        ]
        counts = ratify_equivalence_verdicts(rows)
        assert [r["decision"] for r in rows] == ["approve", "reject", ""]
        assert counts == {
            "approved": 1,
            "rejected": 1,
            "uncertain": 1,
            "conflicted": 0,
        }

    def test_same_against_two_items_is_left_blank(self):
        # a contradiction read_approved_equivalences would reject: don't merge
        rows = [
            {
                "entity_id": "e4",
                "qid": "Q40",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e4",
                "qid": "Q41",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e5",
                "qid": "Q50",
                "verdict": "same",
                "decision": "",
            },
        ]
        counts = ratify_equivalence_verdicts(rows)
        assert [r["decision"] for r in rows] == ["", "", "approve"]
        assert counts["conflicted"] == 2
        assert counts["approved"] == 1
