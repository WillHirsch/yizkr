"""Offline tests for entity reconciliation (ADR-0007). No instance, no LLM."""

import json

import pytest

from yizkr.claims import ClaimBatch, EntityRecord, LocalRef, QidRef
from yizkr.pipeline.reconcile import (
    Proposal,
    RosterEntity,
    adjudicate,
    apply_equivalences,
    block_candidates,
    export_equivalences_csv,
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
            label="Rosa",
            aliases=["our great grandmother"],
            kind="human",
            summary="deported 1942 with spouse Salomon Rothholz",
        ),
        EntityRecord(id="e2", label="Springfield", kind="place"),
    ]


def roster() -> list[RosterEntity]:
    return [
        RosterEntity(
            qid="Q40",
            label="Rosa Genserowski",
            kind="human",
            birth="1874-05-02",
            spouses=["Gabor Serelman", "Salomon Isaac Rothholz"],
        ),
        RosterEntity(qid="Q77", label="Rosa Luxemburg", kind="human"),
        RosterEntity(qid="Q90", label="Springfield, USA", kind="place"),
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
        "subject": {"kind": "local", "local_id": "e1", "label": "Rosa"},
        "property": "residence",
        "value": {
            "type": "item",
            "value": {"kind": "local", "local_id": "e2", "label": "Springfield"},
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


class TestBlocking:
    def test_kind_and_token_overlap(self):
        candidates = block_candidates(entities(), roster())
        assert [c.qid for c in candidates["e1"]] == ["Q40", "Q77"]
        assert [c.qid for c in candidates["e2"]] == ["Q90"]

    def test_no_candidates_no_entry(self):
        lone = [EntityRecord(id="e9", label="Zzyzx Qwx", kind="human")]
        assert block_candidates(lone, roster()) == {}

    def test_prefix_forms_block(self):
        """'Alex' must reach the adjudicator against 'Alexander …' —
        clipped forms are how family documents refer to people."""
        alex = [EntityRecord(id="e5", label="Alex", kind="human")]
        extended = roster() + [
            RosterEntity(qid="Q124", label="Alexander Samuel Genserowsky", kind="human")
        ]
        assert [c.qid for c in block_candidates(alex, extended)["e5"]] == ["Q124"]

    def test_graph_pair_blocking_is_also_prefix_aware(self):
        """Same rule for roster x roster pairs: 'Alex' vs 'Alexander …'
        must be adjudicated, not silently unblocked."""
        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(qid="Q170", label="Alex", kind="human"),
            RosterEntity(qid="Q124", label="Alexander Samuel Genserowsky", kind="human"),
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
        assert "Alexander Samuel Genserowsky" in llm.prompts[0]


class TestAdjudication:
    def response(self, extra_lines=()):
        lines = [
            json.dumps(
                {
                    "entity_id": "e1",
                    "qid": "Q40",
                    "verdict": "same",
                    "confidence": 0.95,
                    "evidence": "same spouse Salomon Rothholz; alias subset",
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
        lines.append(json.dumps({"done": True, "proposals_emitted": len(lines) - 1}))
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
        assert "Salomon" in llm.prompts[0]

    def test_rulings_on_unblocked_pairs_are_dropped(self):
        invented = json.dumps(
            {"entity_id": "e1", "qid": "Q99", "verdict": "same", "confidence": 1}
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
            adjudicate(llm, entities(), block_candidates(entities(), roster()), batch())

    def test_no_candidates_no_call(self):
        llm = ScriptedLlm("must not be called")
        assert adjudicate(llm, entities(), {}, batch()) == []
        assert llm.prompts == []


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
            Proposal(entity_id="e1", qid="Q77", verdict="distinct", confidence=0.9),
        ]

    def test_round_trip_only_approved_rows_take_effect(self, tmp_path):
        path = tmp_path / "equivalences.csv"
        export_equivalences_csv(self.proposals(), entities(), roster(), path)
        content = path.read_text()
        assert "Rosa Genserowski" in content and "same spouse" in content
        # machine verdict alone grants nothing
        assert read_approved_equivalences(path) == {}
        text = content.replace("same spouse,", "same spouse,approve")
        path.write_text(text)
        assert read_approved_equivalences(path) == {"e1": "Q40"}

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
        rosa = next(e for e in applied.entities if e.id == "e1")
        assert rosa.qid == "Q40"
        # original untouched
        assert isinstance(original.claims[0].subject, LocalRef)
        assert original.entities[0].qid is None
