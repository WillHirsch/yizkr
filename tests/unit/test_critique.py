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
        # the critic sees the kinds and aliases it must judge
        assert "Hutmacherei zum Blauen Hut" in llm.prompts[0]
        assert "MOTHER" in llm.prompts[0]

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
