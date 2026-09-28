"""Offline tests for the extraction critic (ADR-0013). No instance, no LLM."""

import json

from yizkr.claims import ClaimBatch
from yizkr.pipeline.critique import Finding, axis_of, run_critique

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-16T09:00:00Z",
}


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


def batch() -> ClaimBatch:
    # a hatters' firm mistyped as place, and a person with a MOTHER role-alias
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e01",
                        "label": "Sebastian",
                    },
                    "property": "employer",
                    "value": {
                        "type": "item",
                        "value": {"kind": "local", "local_id": "e04"},
                    },
                    "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
                }
            ],
            "entities": [
                {
                    "id": "e01",
                    "label": "Sebastian Löbig",
                    "kind": "human",
                    "aliases": ["MOTHER"],
                },  # fmt: skip
                {
                    "id": "e04",
                    "label": "Hutmacherei zum Blauen Hut",
                    "kind": "place",
                },
            ],
        }
    )


def response(lines: list[dict]) -> str:
    return "\n".join(
        [json.dumps(x) for x in lines]
        + [json.dumps({"done": True, "findings_emitted": len(lines)})]
    )


class TestCritique:
    def test_flags_parse_and_entities_reach_the_prompt(self):
        llm = ScriptedLlm(
            response(
                [
                    {
                        "target": "e04",
                        "issue": "agent typed as place",
                        "suggestion": "organization",
                    },  # fmt: skip
                    {
                        "target": "e01 alias: MOTHER",
                        "issue": "role label, not a name",
                        "suggestion": "drop",
                    },  # fmt: skip
                ]
            )
        )
        findings = run_critique(llm, batch())
        assert [f.target for f in findings] == ["e04", "e01 alias: MOTHER"]
        # the critic sees the kinds and aliases it must judge; the template
        # itself names MOTHER, so match e01's whole rendered entity row
        assert "Hutmacherei zum Blauen Hut" in llm.prompts[0]
        assert "e01 | human | Sebastian Löbig | MOTHER" in llm.prompts[0]

    def test_sound_extraction_yields_no_findings(self):
        findings = run_critique(ScriptedLlm(response([])), batch())
        assert findings == []

    def test_empty_batch_is_not_sent(self):
        llm = ScriptedLlm("unused")
        empty = ClaimBatch.model_validate({"extraction_run": RUN})
        assert run_critique(llm, empty) == []
        assert llm.prompts == []  # no wasted call

    def test_prompt_carries_appellation_routing_guidance(self):
        # ADR-0018: the critic tells the model where a relational term
        # belongs (relative / referred_to_as), not just to drop it.
        llm = ScriptedLlm(response([]))
        run_critique(llm, batch())
        prompt = llm.prompts[0]
        assert "referred_to_as" in prompt
        assert "relative" in prompt and "kinship_type" in prompt

    def test_malformed_findings_dropped_never_raise(self):
        llm = ScriptedLlm(
            "\n".join(
                [
                    json.dumps({"target": "e04"}),  # missing issue
                    json.dumps({"issue": "no target"}),
                    json.dumps({"done": True, "findings_emitted": 0}),
                ]
            )
        )
        assert run_critique(llm, batch()) == []


class Rejecting(ScriptedLlm):
    """A client that takes rejections, as the LLM cache's does (#225):
    its first `kept` answers are ones it kept from earlier calls, and a
    rejection says whether the answer rejected was one of them."""

    def __init__(
        self, *responses: str, refuse: bool = False, kept: int = 0
    ) -> None:
        super().__init__(responses[0])
        self.responses = list(responses)
        self.rejected: list[tuple[str, str]] = []
        self.refuse = refuse
        self.kept = kept
        self.asked = 0

    def complete(self, prompt, image_paths=()):
        self.response = self.responses[
            min(self.asked, len(self.responses) - 1)
        ]
        self.asked += 1
        return super().complete(prompt, image_paths)

    def reject(self, response, reason):
        if self.refuse:
            raise OSError("the cache folder is read-only")
        self.rejected.append((response, reason))
        return self.asked <= self.kept


FINDING = {"target": "e04", "issue": "agent typed as place"}


class TestUnfinishedAnswers:
    """An answer without the terminal sentinel (a refusal, one cut off, a
    usage-limit notice) still gives the findings it holds, and is rejected
    through the client, so that a client that keeps answers asks the
    critic again next time (#225). The critic never raises."""

    def test_a_whole_answer_is_kept(self):
        llm = Rejecting(response([FINDING]))
        assert [f.target for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == []

    def test_a_refusal_is_rejected(self):
        llm = Rejecting("I can't help with reviewing this document.")
        assert run_critique(llm, batch()) == []
        ((rejected, reason),) = llm.rejected
        assert rejected == "I can't help with reviewing this document."
        assert "sentinel" in reason

    def test_a_cut_off_answer_gives_its_findings_and_is_rejected(self):
        cut = json.dumps(FINDING) + '\n{"target": "e01 al'
        llm = Rejecting(cut)
        assert [f.target for f in run_critique(llm, batch())] == ["e04"]
        assert [rejected for rejected, _ in llm.rejected] == [cut]

    def test_a_kept_answer_is_asked_for_once_more(self):
        """An unfinished answer the client kept from an earlier call is
        rejected and asked for once more, and the engine's answer is used
        in its place, as it would be without the cache (#230)."""
        refusal = "I can't help with reviewing this document."
        llm = Rejecting(refusal, response([FINDING]), kept=1)
        assert [f.target for f in run_critique(llm, batch())] == ["e04"]
        assert [rejected for rejected, _ in llm.rejected] == [refusal]
        # the engine's own unfinished answer is not asked for again
        llm = Rejecting(refusal, response([FINDING]), kept=0)
        assert run_critique(llm, batch()) == []
        assert len(llm.rejected) == 1
        # nor is a second unfinished answer
        llm = Rejecting(refusal, refusal, response([FINDING]), kept=2)
        assert run_critique(llm, batch()) == []
        assert len(llm.rejected) == 2

    def test_a_failed_rejection_never_raises(self, caplog):
        llm = Rejecting("You've hit your usage limit.", refuse=True)
        assert run_critique(llm, batch()) == []
        assert "could not reject" in caplog.text


class TestAxis:
    """ADR-0023: a Finding's statement-part axis is derived from its target
    grammar — the general form of ADR-0022 §6's per-name axes."""

    def test_axis_of_each_target_form(self):
        assert axis_of("e01") == "subject"
        assert axis_of("e01 alias: MOTHER") == "value"
        assert axis_of("claim: e01 occupation") == "predicate"

    def test_axis_of_unrecognised_is_none(self):
        assert axis_of("") is None
        assert axis_of("some free text target") is None

    def test_finding_exposes_axis(self):
        f = Finding(target="claim: e01 occupation", issue="misapplied")
        assert f.axis == "predicate"
        # the computed axis serialises for the durable critique.json
        assert f.model_dump()["axis"] == "predicate"
