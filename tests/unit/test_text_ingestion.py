"""Document admission and review remain distinct through local resume.

Stages and review/registry records are real. The unit wiki/guard isolates the
workflow's admission checks; the required retained integration case exercises
registration and guarded HTTP writes against actual services.
"""

import copy
import fcntl
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from tests.unit.test_identity_provenance import Wiki
from tests.unit.test_text_stages import ScriptedClient
from yizkr.core.corpus import is_excluded
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline import text_ingestion
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    record_claim_decisions,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.registrar import RegistrationResult
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    prepare_review,
)
from yizkr.pipeline.text_ingestion import TextIngestion
from yizkr.pipeline.text_products import TextProductsError, TextSettings

WHEN = "2026-01-01T12:00:00Z"


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr("yizkr.pipeline.run_provenance.now", lambda: WHEN)
    source = tmp_path / "birth-extract-1938.txt"
    source.write_bytes(source_case("birth-extract-1938").source_bytes)
    policy = tmp_path / "exclusions.txt"
    policy.write_text("")
    wiki = Wiki(detached=True)
    wiki.config.wikibase_url = wiki.config.concept_uri
    registry = CreationRegistry(wiki)
    person = wiki.wbi.item.new()
    person.labels.set("en", "Gustav")
    target = registry.create(
        {"kind": "fictional-text-target"}, person, summary="fictional target"
    )
    registry.index_binding(target)
    guarded = []

    @contextmanager
    def guard(*args, **kwargs):
        guarded.append(kwargs["operation_id"])
        try:
            yield SimpleNamespace(assert_active=lambda: None)
        finally:
            guarded.pop()

    class Registrar:
        def __init__(self, client, *, exclusions):
            assert guarded
            self.exclusions = exclusions

        def register_artifact(self, path, kind, title, description, *, sha1):
            assert guarded
            assert not is_excluded(self.exclusions, path, sha1=sha1)
            item = wiki.wbi.item.new()
            item.labels.set("en", title)
            item.claims.add(
                String(prop_nr=wiki.vocabulary()[0]["media_sha1"], value=sha1)
            )
            binding = registry.create(
                {"kind": "source", "sha1": sha1},
                item,
                summary="fictional source registration",
            )
            return RegistrationResult(binding.qid, None, content_sha1=sha1)

    monkeypatch.setattr(text_ingestion, "protected_mutation", guard)
    monkeypatch.setattr(text_ingestion, "Registrar", Registrar)
    batch = birth_batch("birth-extract-1938")
    claim = batch.claims[0].model_copy(deep=True)
    claim.source.page_number = None
    extractor = ScriptedClient(
        json.dumps(
            {
                "entities": [
                    {
                        "id": "gustav",
                        "label": "Gustav",
                        "kind": "human",
                        "mention_lines": [1],
                    }
                ],
                "preparation": None,
            }
        ),
        claim.model_dump_json() + '\n{"done":true}',
    )
    finding = {
        "entity": "missing-fictional-mention",
        "axis": "subject",
        "category": "wrong_kind",
        "issue": "Check the fictional name.",
    }
    with ReviewStore(tmp_path / "review.sqlite") as store:
        workflow = TextIngestion(
            source,
            tmp_path / "workflow",
            client=wiki,
            review_store=store,
            exclusions_file=policy,
        )
        products = workflow.prepare(
            settings=TextSettings(strategy="chunked", language="de"),
            extractor_client=extractor,
            critic_client=ScriptedClient(
                json.dumps(finding) + '\n{"done":true}'
            ),
        )
        yield SimpleNamespace(
            workflow=workflow,
            products=products,
            source=source,
            policy=policy,
            wiki=wiki,
            registry=registry,
            store=store,
            target=target,
        )


def snapshot(case):
    return copy.deepcopy(case.wiki._recovery_pages), {
        qid: item.get_json() for qid, item in case.wiki.wbi.item.store.items()
    }


def register(case):
    proposed = case.workflow.prepare_registration(
        "certificate", "Fictional source"
    )
    return case.workflow.register_source(
        proposed, expected_digest=proposed.digest, actor="fictional curator"
    )


@pytest.mark.parametrize("change", ["digest", "bytes", "exclusion", "target"])
def test_changed_registration_material_has_no_graph_effect(lifecycle, change):
    case = lifecycle
    proposal = case.workflow.prepare_registration(
        "certificate", "Fictional source"
    )
    expected = proposal.digest
    if change == "digest":
        expected = "0" * 64
    elif change == "bytes":
        case.source.write_text(case.source.read_text() + "\nChanged bytes.\n")
    elif change == "exclusion":
        case.policy.write_text(case.source.name)
    else:
        case.wiki.config.wikibase_url = "https://another-fictional.invalid"
    before = snapshot(case)
    with pytest.raises((ReviewRefused, TextProductsError)):
        case.workflow.register_source(
            proposal, expected_digest=expected, actor="fictional curator"
        )
    assert snapshot(case) == before


def test_registration_retry_preserves_binding_without_approving_identity(
    lifecycle,
):
    case = lifecycle
    first = register(case)
    before = snapshot(case)
    assert register(case).artifact_qid == first.artifact_qid
    assert snapshot(case) == before
    assert not case.registry.assignment_snapshot().assignments
    with pytest.raises(ReviewRefused, match="no accepted assignment"):
        case.workflow.prepare_claims((0,))


def test_interrupted_registration_resumes_before_identity_phase(
    lifecycle, monkeypatch
):
    case = lifecycle
    verify = case.workflow._verify_source
    with monkeypatch.context() as patch:
        patch.setattr(
            case.workflow,
            "_verify_source",
            lambda _: (_ for _ in ()).throw(
                OSError("fictional registration receipt acknowledgement lost")
            ),
        )
        with pytest.raises(OSError, match="acknowledgement lost"):
            register(case)
    binding = case.registry.lookup(
        {"kind": "source", "sha1": case.products.artifact.sha1}
    )
    with pytest.raises(ReviewRefused, match="resume the interrupted"):
        case.workflow.prepare_identities()
    assert register(case).artifact_qid == binding.qid
    verify(case.products)


def assign(case):
    products = case.workflow._registered()
    request = case.workflow._request(products)
    choice = ReviewProposal(
        operation="assign",
        mentions=(
            MentionKey(
                artifact_sha1=products.artifact.sha1, local_id="gustav"
            ),
        ),
        target=case.target,
    )
    request = replace(request, proposals=(choice,))
    prepared = prepare_review(case.store, request)
    for proposal in prepared.proposals:
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional identity curator",
            time=WHEN,
        )
    return accept_reviewed_assignments(case.store, request, case.registry)


def test_critique_is_visible_in_claim_consent_and_identity_is_insufficient(
    lifecycle,
):
    case = lifecycle
    register(case)
    assign(case)
    prepared = case.workflow.prepare_claims((0,))
    (claim,) = prepared.claims
    assert claim.decision is None
    assert any(
        "Check the fictional name" in note for note in claim.selection.warnings
    )
    assert claim.basis["warnings"] == sorted(set(claim.selection.warnings))
    before = snapshot(case)
    with pytest.raises(ReviewRefused, match="explicit approval"):
        case.workflow.apply_claims((0,))
    assert snapshot(case) == before
    record_claim_decisions(
        case.store,
        prepared,
        [
            ClaimDecisionInput(
                proposal_id=claim.proposal.id,
                expected_digest=claim.proposal.digest,
                outcome="approve",
                actor="fictional claim curator",
                time=WHEN,
            )
        ],
    )
    written = case.workflow.apply_claims((0,))
    before = snapshot(case)
    assert case.workflow.apply_claims((0,)) == written
    assert snapshot(case) == before


def test_resume_requires_original_review_correspondence(lifecycle, tmp_path):
    case = lifecycle
    register(case)
    assign(case)
    with ReviewStore(tmp_path / "wrong-review.sqlite") as other:
        workflow = TextIngestion(
            case.source,
            case.workflow.directory,
            client=case.wiki,
            review_store=other,
            exclusions_file=case.policy,
        )
        before = snapshot(case)
        with pytest.raises(ReviewRefused, match="original review store"):
            workflow.prepare_claims((0,))
        assert snapshot(case) == before


def test_updated_exclusions_block_an_already_registered_source(lifecycle):
    case = lifecycle
    register(case)
    case.policy.write_text(case.products.artifact.sha1)
    before = snapshot(case)
    with pytest.raises(TextProductsError, match="never-ingest"):
        case.workflow.prepare_identities()
    assert snapshot(case) == before


def test_legacy_hash_owner_refuses_before_any_registration_effect(lifecycle):
    case = lifecycle
    legacy = case.wiki.wbi.item.new()
    legacy.labels.set("en", "Fictional legacy source")
    legacy.claims.add(
        String(
            prop_nr=case.wiki.vocabulary()[0]["media_sha1"],
            value=case.products.artifact.sha1,
        )
    )
    legacy.write()
    before = snapshot(case)
    with pytest.raises(ReviewRefused, match="verified migration"):
        register(case)
    assert snapshot(case) == before


def test_registration_cannot_create_an_unreviewed_person(lifecycle):
    case = lifecycle
    before = snapshot(case)
    with pytest.raises(ValueError, match="artifact kind"):
        case.workflow.prepare_registration("human", "Gustav")
    assert snapshot(case) == before


def test_claim_application_rechecks_exclusions_after_guarded_preparation(
    lifecycle, monkeypatch
):
    case = lifecycle
    register(case)
    assign(case)
    prepared = case.workflow.prepare_claims((0,))
    claim = prepared.claims[0]
    record_claim_decisions(
        case.store,
        prepared,
        [
            ClaimDecisionInput(
                proposal_id=claim.proposal.id,
                expected_digest=claim.proposal.digest,
                outcome="approve",
                actor="fictional curator",
                time=WHEN,
            )
        ],
    )
    from yizkr.pipeline import assignment_writes

    original = assignment_writes.prepare_claim_review

    def edited_policy(*args, **kwargs):
        result = original(*args, **kwargs)
        case.policy.write_text(case.products.artifact.sha1)
        return result

    monkeypatch.setattr(
        assignment_writes, "prepare_claim_review", edited_policy
    )
    before = snapshot(case)
    with pytest.raises(TextProductsError, match="never-ingest"):
        case.workflow.apply_claims((0,))
    assert snapshot(case) == before


def test_workspace_cannot_be_used_concurrently(lifecycle):
    case = lifecycle
    with (case.workflow.directory / "lifecycle.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ReviewRefused, match="workspace is in use"):
            case.workflow.prepare()
