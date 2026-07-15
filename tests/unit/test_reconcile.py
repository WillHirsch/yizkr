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
    entity_facts,
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


def name_claim(prop: str, text: str, subject: str = "e1") -> dict:
    return {
        "subject": {"kind": "local", "local_id": subject, "label": "Rosa"},
        "property": prop,
        "value": {"type": "monolingual", "value": {"text": text, "language": "en"}},
        "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
    }


class TestTypedNameEvidence:
    """ADR-0011: typed names surface as adjudication evidence, both sides."""

    def test_roster_facts_include_typed_names(self):
        entity = RosterEntity(
            qid="Q40",
            label="Rosa Rothholz",
            kind="human",
            birth_names=["Rosa Genserowski"],
            married_names=["Rosa Rothholz"],
            nicknames=["Rosie"],
        )
        facts = entity.facts()
        assert "birth name: Rosa Genserowski" in facts
        assert "married name: Rosa Rothholz" in facts
        assert "nickname: Rosie" in facts

    def test_entity_facts_renders_monolingual_names_plainly(self):
        facts = entity_facts(batch(claims=[name_claim("birth_name", "Rosa G")]), "e1")
        assert "birth_name: Rosa G" in facts
        assert "language=" not in facts  # not a pydantic repr

    def test_batch_name_claim_reaches_the_adjudication_prompt(self):
        llm = ScriptedLlm(json.dumps({"done": True, "proposals_emitted": 0}))
        source = batch(claims=[name_claim("birth_name", "Rosa Genserowski")])
        adjudicate(llm, entities(), block_candidates(entities(), roster()), source)
        assert "Rosa Genserowski" in llm.prompts[0]


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
            RosterEntity(
                qid="Q124", label="Alexander Samuel Genserowsky", kind="human"
            ),
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
                json.dumps({"entity_id": "e1", "qid": "Q40", "verdict": "same"}),
                json.dumps({"entity_id": "e1", "qid": "Q77", "verdict": "distinct"}),
                json.dumps({"done": True, "proposals_emitted": 2}),
            ]
        )
        second = "\n".join(
            [
                json.dumps({"entity_id": "e2", "qid": "Q90", "verdict": "same"}),
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
        assert "Springfield" not in llm.prompts[0]  # e2 not in batch 1
        assert "Rosa" not in llm.prompts[1]  # e1 not in batch 2
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

    def test_aliases_visible_and_prunable_at_review(self, tmp_path):
        """An approval is also an alias contribution (the writer appends
        the document's aliases to the matched item), so the CSV must show
        them and honor the reviewer's pruning — a form's MOTHER label
        must be deletable before it pollutes the graph."""
        from yizkr.pipeline.reconcile import read_reviewed_aliases

        path = tmp_path / "equivalences.csv"
        noisy = [
            EntityRecord(
                id="e1",
                label="Rosa",
                aliases=["our great grandmother", "MOTHER"],
                kind="human",
            )
        ]
        export_equivalences_csv(self.proposals(), noisy, roster(), path)
        content = path.read_text()
        assert "our great grandmother|MOTHER" in content  # visible to reviewer
        # the reviewer prunes the role label and approves
        content = content.replace(
            "our great grandmother|MOTHER", "our great grandmother"
        )
        content = content.replace("same spouse,", "same spouse,approve")
        path.write_text(content)
        approved = read_approved_equivalences(path)
        reviewed = read_reviewed_aliases(path)
        assert reviewed == {"e1": ["our great grandmother"]}
        applied = apply_equivalences(batch(entity_list=noisy), approved, reviewed)
        (entity,) = applied.entities
        assert entity.qid == "Q40"
        assert entity.aliases == ["our great grandmother"]  # MOTHER pruned

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
        rosa = next(e for e in applied.entities if e.id == "e1")
        assert rosa.qid == "Q40"
        # original untouched
        assert isinstance(original.claims[0].subject, LocalRef)
        assert original.entities[0].qid is None

    def test_identical_claims_collapse_after_equivalence(self):
        # two document entities (duplicated records in the source) both
        # approved to one item leave byte-identical claims: collapse
        twin = dict(spouse_claim(), subject={"kind": "local", "local_id": "e3"})
        entity_list = [
            *entities(),
            EntityRecord(id="e3", label="Rosa", kind="human"),
        ]
        original = batch(claims=[spouse_claim(), twin], entity_list=entity_list)
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
