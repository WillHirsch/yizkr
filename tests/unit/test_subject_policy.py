"""Shared subject decisions and the writer's additional storage obligations."""

import pytest

from tests.unit.test_container_claims import (
    BOOKLET_REF,
    JULY_28,
    LETTER_REF,
    PLACED,
    STRAY_REF,
    caption,
    local,
)
from tests.unit.test_document_subject_forms import (
    CERTIFICATE,
    LETTER,
    RUDOLF,
    batch,
    claim,
    enclosure,
    item,
)
from yizkr.claims import (
    ARTIFACT_SUBJECT,
    DOCUMENT_SUBJECT,
    EXCERPT_REQUIRED,
    PLACED_SUBJECT,
    reject_reasons,
    subject_policy,
    validate_against_manifest,
)
from yizkr.model.schema import load_acceptance
from yizkr.pipeline.reconcile import apply_equivalences
from yizkr.pipeline.writer import pre_plan_refusals, subject_refusal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            claim("inception", JULY_28, LETTER), None, id="placed-document"
        ),
        pytest.param(
            claim("inception", JULY_28, STRAY_REF.model_dump()),
            DOCUMENT_SUBJECT,
            id="unplaced-document",
        ),
        pytest.param(
            caption(subject=local("e1", "Wastl", "human")),
            PLACED_SUBJECT,
            id="caption-on-person",
        ),
        pytest.param(
            claim("residence", item("e2", "Brunnenring 35", "place"), LETTER),
            ARTIFACT_SUBJECT,
            id="person-property-on-artifact",
        ),
        pytest.param(
            claim("inception", JULY_28, LETTER, excerpt=None),
            EXCERPT_REQUIRED,
            id="placed-document-without-supporting-excerpt",
        ),
    ],
)
def test_shared_decision_matches_validation_and_registered_writer(
    raw, expected
):
    given = batch(raw)
    current = given.claims[0]
    decision = subject_policy(current, load_acceptance(), PLACED)
    assert (decision.problem.code if decision.problem else None) == expected
    problems = validate_against_manifest(given, placed=PLACED)
    assert reject_reasons(problems) == ([] if expected is None else [expected])
    refusals = pre_plan_refusals(
        given, {}, PLACED, {LETTER_REF.sha1, STRAY_REF.sha1}
    )
    assert bool(refusals) == (expected is not None)
    if expected is not None:
        reason, warning = refusals[0]
        assert reason == "subject_rule"
        assert decision.problem.rule in warning


@pytest.mark.parametrize("kind", ["caption", "source-document"])
def test_caption_and_source_document_do_not_acquire_an_excerpt_requirement(
    kind,
):
    if kind == "caption":
        raw = caption()
        del raw["source"]["transcription_excerpt"]
        resolved = {LETTER_REF.sha1}
    else:
        raw = claim(
            "inception", JULY_28, BOOKLET_REF.model_dump(), excerpt=None
        )
        resolved = set()
    given = batch(raw)
    decision = subject_policy(given.claims[0], load_acceptance(), PLACED)
    assert decision.problem is None
    assert not decision.requires_excerpt
    assert validate_against_manifest(given, placed=PLACED) == []
    assert pre_plan_refusals(given, {}, PLACED, resolved) == {}


def test_registration_failure_precedes_the_missing_excerpt_at_write_boundary():
    given = batch(claim("inception", JULY_28, LETTER, excerpt=None))
    current = given.claims[0]
    decision = subject_policy(current, load_acceptance(), PLACED)
    assert decision.problem.code == EXCERPT_REQUIRED
    assert decision.registration_rule is not None
    missing_registration = subject_refusal(current, PLACED, ())
    assert "no item carries" in missing_registration[0]
    assert missing_registration[1] == decision.registration_rule
    registered = subject_refusal(current, PLACED, {LETTER_REF.sha1})
    assert registered == (decision.problem.message, decision.problem.rule)


def test_writer_requires_an_excerpt_that_survives_storage_normalization():
    # Python accepts this text as nonblank; Wikibase trims it away.
    given = batch(claim("inception", JULY_28, LETTER, excerpt="\u180e"))
    decision = subject_policy(given.claims[0], load_acceptance(), PLACED)
    assert decision.problem is None
    assert decision.requires_excerpt
    assert validate_against_manifest(given, placed=PLACED) == []
    ((reason, warning),) = pre_plan_refusals(
        given, {}, PLACED, {LETTER_REF.sha1}
    ).values()
    assert reason == "subject_rule"
    assert "no excerpt" in warning


def test_confirmation_belongs_to_the_claim_not_just_its_target_item():
    given = apply_equivalences(
        batch(enclosure(), entities=[CERTIFICATE, RUDOLF]), {"u1": "Q41"}
    )
    confirmed = given.claims[0]
    decision = subject_policy(
        confirmed, load_acceptance(), confirmed="unclassified_entity"
    )
    assert decision.problem is None
    assert decision.form == "confirmed"
    assert decision.requires_excerpt
    assert pre_plan_refusals(given, {}) == {}

    # Naming that same QID directly does not carry the curator's local-subject
    # confirmation. The historical source alone cannot establish this link.
    outright = confirmed.model_copy(update={"confirmed_from": None})
    given = given.model_copy(update={"claims": [outright]})
    decision = subject_policy(outright, load_acceptance())
    assert decision.problem.code == DOCUMENT_SUBJECT
    assert reject_reasons(validate_against_manifest(given)) == [
        DOCUMENT_SUBJECT
    ]
    assert pre_plan_refusals(given, {})[0][0] == "subject_rule"
