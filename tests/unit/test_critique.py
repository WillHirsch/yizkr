"""Offline tests for the extraction critic (ADR-0013). No instance, no LLM."""

import json

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch, QidRef, load_vocabulary
from yizkr.pipeline.critique import (
    CATEGORIES,
    CRITIQUE_PROMPT,
    Finding,
    Invalid,
    attach,
    check,
    counterparts,
    note,
    run_critique,
)
from yizkr.pipeline.llm import OutputLimitReached
from yizkr.pipeline.resolve import resolve_batch

# the bytes of a fictional scan, which the claims below cite by
# their SHA-1 (#102)
SCAN = ArtifactRef.from_bytes(b"fictional scan: a Loebig family document")

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
                    "source": {"artifact": SCAN.model_dump()},
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
    """A whole critique answer: its findings, then the terminal sentinel
    the prompt asks for."""
    return "\n".join(
        [json.dumps(x) for x in lines] + [json.dumps({"done": True})]
    )


ROLE_ALIAS = {
    "entity": "e01",
    "alias": "MOTHER",
    "axis": "value",
    "category": "role_label_alias",
    "issue": "role label, not a name",
    "suggestion": {"note": "drop"},
}
EMPLOYER = {
    "entity": "e04",
    "claim": {"subject": "e01", "property": "employer", "value": "e04"},
    "axis": "value",
    "category": "relation_kind_mismatch",
    "issue": "an employer is an organization, not a place",
    "suggestion": {"kind": "organization"},
}


class TestCritique:
    def test_findings_are_records_and_entities_reach_the_prompt(self):
        llm = ScriptedLlm(response([FINDING, ROLE_ALIAS, EMPLOYER]))
        findings = run_critique(llm, batch())
        assert all(isinstance(f, Finding) for f in findings)
        assert [(f.entity, f.axis, f.category) for f in findings] == [
            ("e04", "subject", "wrong_kind"),
            ("e01", "value", "role_label_alias"),
            ("e04", "value", "relation_kind_mismatch"),
        ]
        assert findings[1].alias == "MOTHER"
        assert findings[2].claim.property == "employer"
        assert findings[0].suggestion.kind == "organization"
        # the critic sees the kinds and aliases it must judge; the template
        # itself names MOTHER, so match e01's whole rendered entity row
        assert "Hutmacherei zum Blauen Hut" in llm.prompts[0]
        assert "e01 | human | Sebastian Löbig | MOTHER" in llm.prompts[0]
        # a claim's item value is listed by its id, which a finding names
        assert "e01 | employer | e04" in llm.prompts[0]

    def test_the_prompt_lists_every_category_axis_and_manifest_key(self):
        """The category list is kept with the prompt, and a suggestion
        names manifest keys, so the prompt lists them (#386)."""
        llm = ScriptedLlm(response([]))
        run_critique(llm, batch())
        prompt = llm.prompts[0]
        assert all(f"- {key}: " in prompt for key in CATEGORIES)
        assert "subject | predicate | value | existence" in prompt
        properties, items = load_vocabulary()
        assert ", ".join(sorted(items)) in prompt
        assert ", ".join(sorted(properties)) in prompt

    def test_the_category_list_is_the_issues(self):
        assert list(CATEGORIES) == [
            "wrong_kind",
            "role_label_alias",
            "kinship_term_alias",
            "deictic_alias",
            "escape_hatch_misused",
            "misapplied_property",
            "office_holder_as_organization",
            "religion_as_occupation",
            "relation_kind_mismatch",
        ]
        assert "ADR-0031" in CATEGORIES["relation_kind_mismatch"]

    def test_sound_extraction_yields_no_findings(self):
        findings = run_critique(ScriptedLlm(response([])), batch())
        assert findings == []

    def test_empty_batch_is_not_sent(self):
        llm = ScriptedLlm("unused")
        empty = ClaimBatch.model_validate({"extraction_run": RUN})
        assert run_critique(llm, empty) == []
        assert llm.prompts == []  # no wasted call

    def test_the_prompt_asks_for_a_done_line_and_no_count(self):
        """The answer ends with `{"done": true}`: a count of the findings
        gated nothing (#7)."""
        llm = ScriptedLlm(response([]))
        run_critique(llm, batch())
        assert llm.prompts[0].endswith('final line:\n{"done": true}\n')
        assert "emitted" not in llm.prompts[0]

    def test_prompt_carries_appellation_routing_guidance(self):
        # ADR-0018: the critic tells the model where a relational term
        # belongs (relative / referred_to_as), not just to drop it.
        llm = ScriptedLlm(response([]))
        run_critique(llm, batch())
        prompt = llm.prompts[0]
        assert "referred_to_as" in prompt
        assert "relative" in prompt and "kinship_type" in prompt

    def test_a_finding_that_fails_its_checks_is_kept_verbatim(self):
        """A finding that fails validation is kept as given, with why,
        beside the valid ones and in the critic's order: never repaired,
        never dropped (#386)."""
        target = {"target": "e04", "issue": "agent typed as place"}
        llm = ScriptedLlm(response([target, FINDING]))
        invalid, valid = run_critique(llm, batch())
        assert isinstance(invalid, Invalid) and invalid.invalid == target
        assert invalid.why  # the reasons, for the log and the view
        assert isinstance(valid, Finding)
        stored = invalid.model_dump(mode="json")
        assert stored["invalid"] == target

    def test_an_object_with_no_finding_field_is_not_a_finding(self):
        """A claim or suggestion cut loose from a broken line is no finding,
        valid or invalid; nothing else is skipped."""
        answer = "\n".join(
            [
                '{"entity": "e04", "claim": ',
                json.dumps({"subject": "e01", "property": "employer"}),
                json.dumps({"done": True}),
            ]
        )
        assert run_critique(ScriptedLlm(answer), batch()) == []


VOCABULARY = load_vocabulary()


def checked(**changes):
    return check(FINDING | changes, batch(), VOCABULARY)


class TestValidation:
    """Every id a finding names is the batch's and every key the
    manifest's; a finding that fails keeps its fields as given (#386)."""

    def test_a_valid_finding_passes(self):
        assert isinstance(checked(), Finding)
        assert isinstance(check(EMPLOYER, batch(), VOCABULARY), Finding)
        assert isinstance(check(ROLE_ALIAS, batch(), VOCABULARY), Finding)

    @pytest.mark.parametrize(
        ("changes", "why"),
        [
            ({"entity": "e09"}, "entity 'e09' is not in the manifest"),
            ({"axis": "identity"}, "axis 'identity' is not one of"),
            ({"category": "misread_year"}, "category 'misread_year'"),
            (
                {"suggestion": {"kind": "company"}},
                "suggested kind 'company' is not a manifest key",
            ),
            (
                {"suggestion": {"property": "works_for"}},
                "suggested property 'works_for' is not a manifest key",
            ),
            ({"alias": "Oma"}, "alias 'Oma' is not the entity's"),
            (
                {
                    "claim": {
                        "subject": "e01",
                        "property": "employer",
                        "value": "Hutmacherei zum Blauen Hut",
                    }
                },
                "no claim is listed as given",
            ),
            (
                {
                    "claim": {
                        "subject": "e01",
                        "property": "residence",
                        "value": "e04",
                    }
                },
                "no claim is listed as given",
            ),
            ({"suggestion": "organization"}, "suggestion"),
            ({"target": "e04"}, "target"),
            ({"issue": None}, "issue"),
        ],
    )
    def test_a_failing_finding_is_kept_as_given(self, changes, why):
        given = FINDING | changes
        found = check(given, batch(), VOCABULARY)
        assert isinstance(found, Invalid)
        assert found.invalid == given
        assert any(why in reason for reason in found.why), found.why

    def test_a_claim_must_name_the_entity(self):
        """A finding on a claim concerns the claim's subject or the entity
        its value names, never an entity the claim does not name."""
        other = EMPLOYER | {"entity": "e01"}
        assert isinstance(check(other, batch(), VOCABULARY), Finding)
        stray = batch().model_copy(deep=True)
        stray.entities.append(
            stray.entities[1].model_copy(update={"id": "e05"})
        )
        found = check(EMPLOYER | {"entity": "e05"}, stray, VOCABULARY)
        assert isinstance(found, Invalid)

    def test_every_axis_is_accepted(self):
        for axis in ("subject", "predicate", "value", "existence"):
            assert isinstance(checked(axis=axis), Finding)


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


class Stopping(Rejecting):
    """A client that takes rejections and reports why its model stopped on
    each answer (`last_report`, as the engines' clients do): `stops` holds
    each next answer's stop reason, in order."""

    def __init__(self, *responses: str, stops: list[str], kept: int = 0):
        super().__init__(*responses, kept=kept)
        self.stops = list(stops)
        self.report: dict = {}

    def complete(self, prompt, image_paths=()):
        self.report = {"stop_reason": self.stops.pop(0)}
        return super().complete(prompt, image_paths)

    def last_report(self):
        return dict(self.report)


FINDING = {
    "entity": "e04",
    "axis": "subject",
    "category": "wrong_kind",
    "issue": "agent typed as place",
    "suggestion": {"kind": "organization"},
}


class TestUnfinishedAnswers:
    """An answer without the terminal sentinel (a refusal, one cut off, a
    usage-limit notice) still gives the findings it holds, and is rejected
    through the client, so that a client that keeps answers asks the
    critic again next time (#225). The critic never raises."""

    def test_a_whole_answer_is_kept(self):
        llm = Rejecting(response([FINDING]))
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == []

    def test_a_refusal_is_rejected(self):
        llm = Rejecting("I can't help with reviewing this document.")
        assert run_critique(llm, batch()) == []
        ((rejected, reason),) = llm.rejected
        assert rejected == "I can't help with reviewing this document."
        assert "sentinel" in reason

    def test_a_cut_off_answer_gives_its_findings_and_is_rejected(self):
        cut = json.dumps(FINDING) + '\n{"entity": "e01", "al'
        llm = Rejecting(cut)
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert [rejected for rejected, _ in llm.rejected] == [cut]

    def test_a_kept_answer_is_asked_for_once_more(self):
        """An unfinished answer the client kept from an earlier call is
        rejected and asked for once more, and the engine's answer is used
        in its place, as it would be without the cache (#230)."""
        refusal = "I can't help with reviewing this document."
        llm = Rejecting(refusal, response([FINDING]), kept=1)
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert [rejected for rejected, _ in llm.rejected] == [refusal]
        # the engine's own unfinished answer is not asked for again
        llm = Rejecting(refusal, response([FINDING]), kept=0)
        assert run_critique(llm, batch()) == []
        assert len(llm.rejected) == 1
        # nor is a second unfinished answer
        llm = Rejecting(refusal, refusal, response([FINDING]), kept=2)
        assert run_critique(llm, batch()) == []
        assert len(llm.rejected) == 2

    def test_an_answer_stopped_at_the_output_limit_is_rejected(self):
        """An answer whose engine reports it stopped at max_tokens is
        unfinished, even with its sentinel: it gives its findings and is
        rejected, as one without its sentinel is (#7)."""
        whole = response([FINDING])
        llm = Stopping(whole, stops=["max_tokens"])
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == [(whole, "critique: cut off at max_tokens")]

    def test_a_cut_answer_stopped_at_the_output_limit_says_so(self):
        """Cut off twice over, at the output limit and before its
        sentinel: the stop reason names the rejection (#7)."""
        cut = json.dumps(FINDING) + '\n{"entity": "e01", "al'
        llm = Stopping(cut, stops=["max_tokens"])
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == [(cut, "critique: cut off at max_tokens")]

    def test_an_answer_the_engine_gave_up_on_gives_no_findings(self, caplog):
        """A call the engine gave up on at its output limit gave no
        answer: no findings, nothing to reject, and the critic still never
        raises (#7)."""

        class GivingUp(Rejecting):
            def complete(self, prompt, image_paths=()):
                super().complete(prompt, image_paths)
                raise OutputLimitReached()

        llm = GivingUp("unused")
        assert run_critique(llm, batch()) == []
        assert llm.rejected == []
        assert "critique cut off at max_tokens" in caplog.text
        # the stage is told, so that the empty critique needs attention
        attention: list[str] = []
        assert run_critique(llm, batch(), attention=attention) == []
        assert attention == ["critique cut off at max_tokens: no findings"]
        # a whole critique tells it nothing
        attention = []
        run_critique(
            Rejecting(response([FINDING])), batch(), attention=attention
        )
        assert attention == []

    def test_an_answer_that_ended_its_turn_is_whole(self):
        llm = Stopping(response([FINDING]), stops=["end_turn"])
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == []

    def test_a_kept_answer_is_judged_again_when_asked_once_more(self):
        """A kept answer without its sentinel is asked for once more, and
        the engine's answer, stopped at max_tokens, is rejected too."""
        refusal = "I can't help with reviewing this document."
        whole = response([FINDING])
        llm = Stopping(
            refusal, whole, stops=["end_turn", "max_tokens"], kept=1
        )
        assert [f.entity for f in run_critique(llm, batch())] == ["e04"]
        assert llm.rejected == [
            (refusal, "critique: no terminal sentinel"),
            (whole, "critique: cut off at max_tokens"),
        ]

    def test_a_failed_rejection_never_raises(self, caplog):
        llm = Rejecting("You've hit your usage limit.", refuse=True)
        assert run_critique(llm, batch()) == []
        assert "could not reject" in caplog.text


def resolved_batch() -> ClaimBatch:
    """The fixture batch with a second claim about the firm, whose
    subject is the firm itself (so it can resolve to a QID), a birth, an
    entity no claim names, and last a residence whose value, an address
    typed as an organization, no claim has as its subject."""
    data = batch().model_dump(mode="json")
    firm = {"kind": "local", "local_id": "e04", "label": "Blauer Hut"}
    sebastian = data["claims"][0]["subject"]
    data["claims"].append(
        {
            "subject": firm,
            "property": "instance_of",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "organization"},
            },
            "source": {"artifact": SCAN.model_dump(mode="json")},
        }
    )
    data["claims"].append(
        {
            "subject": sebastian,
            "property": "date_of_birth",
            "value": {"type": "time", "year": 1942},
            "source": {"artifact": SCAN.model_dump(mode="json")},
        }
    )
    data["entities"].append(
        {"id": "e05", "label": "Stupfield", "kind": "place"}
    )
    data["claims"].append(
        {
            "subject": sebastian,
            "property": "residence",
            "value": {
                "type": "item",
                "value": {"kind": "local", "local_id": "e07"},
            },
            "source": {"artifact": SCAN.model_dump(mode="json")},
        }
    )
    data["entities"].append(
        {"id": "e07", "label": "8 Hawthorn Road", "kind": "organization"}
    )
    return ClaimBatch.model_validate(data)


QID = QidRef(qid="Q17")


def rendered(attached: dict[int, list[Finding]]) -> list[tuple[int, str]]:
    return sorted(
        (row, finding.issue)
        for row, findings in attached.items()
        for finding in findings
    )


class TestAttach:
    """#113: every finding attaches to its claim or entity, resolved-QID
    and value-position entities included, and renders exactly once."""

    FINDINGS = [
        Finding.model_validate(FINDING),
        Finding.model_validate(ROLE_ALIAS),
        Finding.model_validate(EMPLOYER),
        Finding.model_validate(
            FINDING | {"entity": "e05", "issue": "no claim names it"}
        ),
        Finding.model_validate(
            {
                "entity": "e01",
                "claim": {
                    "subject": "e01",
                    "property": "date_of_birth",
                    "value": "1942",
                },
                "axis": "existence",
                "category": "misapplied_property",
                "issue": "a wedding year read as a birth",
            }
        ),
        Finding.model_validate(
            FINDING | {"entity": "e07", "issue": "an address, not a body"}
        ),
    ]

    def test_each_finding_renders_once_on_its_row(self):
        extracted = resolved_batch()
        attached = attach(self.FINDINGS, extracted, extracted)
        assert rendered(attached) == [
            (0, "an employer is an organization, not a place"),
            (0, "no claim names it"),
            (0, "role label, not a name"),
            (1, "agent typed as place"),
            (2, "a wedding year read as a birth"),
            # named only as a value: on that claim's row, not the first
            (3, "an address, not a body"),
        ]

    def test_resolved_entities_keep_their_rows(self):
        """The firm and Sebastian resolved to QIDs: the review rows name
        no local id, and every finding still reaches its row."""
        extracted = resolved_batch()
        rows = resolve_batch(
            extracted,
            {"Sebastian": "Q17", "Blauer Hut": "Q23"},
        )
        assert rows.claims[1].subject.qid == "Q23"
        assert counterparts(extracted, rows) == [0, 1, 2, 3]
        assert rendered(attach(self.FINDINGS, extracted, rows)) == rendered(
            attach(self.FINDINGS, extracted, extracted)
        )

    def test_a_collapsed_claim_keeps_its_finding(self):
        """Two claims resolution makes identical are one row: a finding on
        the dropped one goes on the row kept."""
        extracted = resolved_batch()
        data = extracted.model_dump(mode="json")
        twin = json.loads(json.dumps(data["claims"][2]))
        twin["subject"] = {"kind": "local", "local_id": "e06", "label": "S."}
        data["claims"].append(twin)
        data["entities"].append({"id": "e06", "label": "S.", "kind": "human"})
        extracted = ClaimBatch.model_validate(data)
        rows = extracted.model_copy(deep=True)
        for claim in rows.claims:
            if getattr(claim.subject, "local_id", None) in ("e01", "e06"):
                claim.subject = QID
        rows.claims.pop()  # the twin, identical once both are Q17
        assert counterparts(extracted, rows) == [0, 1, 2, 3, 2]
        twin_finding = Finding.model_validate(
            {
                "entity": "e06",
                "claim": {
                    "subject": "e06",
                    "property": "date_of_birth",
                    "value": "1942",
                },
                "axis": "value",
                "category": "misapplied_property",
                "issue": "the twin's year",
            }
        )
        assert rendered(attach([twin_finding], extracted, rows)) == [
            (2, "the twin's year")
        ]

    def test_no_rows_attach_nothing(self):
        empty = ClaimBatch.model_validate({"extraction_run": RUN})
        assert attach(self.FINDINGS, empty, empty) == {}

    def test_a_note_names_axis_category_entity_and_suggestion(self):
        assert note(Finding.model_validate(ROLE_ALIAS)) == (
            "critique [value, role_label_alias] e01 alias 'MOTHER': "
            "role label, not a name (suggests drop)"
        )
        assert note(Finding.model_validate(FINDING)) == (
            "critique [subject, wrong_kind] e04: agent typed as place "
            "(suggests kind organization)"
        )


def test_the_prompt_is_the_one_the_fixture_engines_recognise():
    assert CRITIQUE_PROMPT.startswith("You review a genealogical extraction")
