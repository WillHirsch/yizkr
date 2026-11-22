"""Document admission and review remain distinct through local resume.

Stages and review/registry records are real. Each case runs with the text
checkpoint's products and with a document's saved views, which the production
scheduler makes from the same fixed fictional answers. The unit wiki/guard
isolates the workflow's admission checks; the required integration cases
exercise registration and guarded HTTP writes against actual services.
"""

import copy
import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.fixtures.saved_products import (
    CONTEXT_GAP,
    GRAVESTONE_FILE,
    AuthoredAnswers,
    gravestone_answers,
    gravestone_claim,
    gravestone_image,
    note_attention,
    saved_document,
)
from tests.fixtures.semantic_acceptance import birth_batch, source_case
from tests.unit.test_identity_provenance import Wiki
from tests.unit.test_text_stages import ScriptedClient
from yizkr.core.corpus import ExclusionList, is_excluded
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline import source_ingestion
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    record_claim_decisions,
)
from yizkr.pipeline.document_products import (
    ProductsRefused,
    UnsupportedKind,
    load_document_products,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.registrar import RegistrationResult
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    prepare_review,
)
from yizkr.pipeline.source_ingestion import SourceIngestion
from yizkr.pipeline.text_products import TextProductsError, TextSettings

WHEN = "2026-01-01T12:00:00Z"
# Either implementation's refusal of its saved products or current inputs
PRODUCTS_REFUSED = (TextProductsError, ProductsRefused)
FINDING = {
    "entity": "missing-fictional-mention",
    "axis": "subject",
    "category": "wrong_kind",
    "issue": "Check the fictional name.",
}


def text_answers():
    batch = birth_batch("birth-extract-1938")
    claim = batch.claims[0].model_copy(deep=True)
    claim.source.page_number = None
    manifest = json.dumps(
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
    )
    return {
        "extract": [manifest, claim.model_dump_json() + '\n{"done":true}'],
        "critique": [json.dumps(FINDING) + '\n{"done":true}'],
    }


def source_and_answers(kind):
    if kind == "text":
        name = "birth-extract-1938.txt"
        data = source_case("birth-extract-1938").source_bytes
        return name, data, text_answers(), "Gustav", "gustav"
    data = gravestone_image()
    sha1 = hashlib.sha1(data).hexdigest()
    answers = gravestone_answers(
        sha1,
        claims=(gravestone_claim(sha1), gravestone_claim(sha1, "LÖBIG")),
    )
    return GRAVESTONE_FILE, data, answers, "Eugen", "eugen"


@contextmanager
def lifecycle_case(tmp_path, monkeypatch, products, kind="text"):
    """A workflow over one fictional source's complete products: the text
    checkpoint, or the saved views of a scheduler run (`document`)."""
    monkeypatch.setattr("yizkr.pipeline.run_provenance.now", lambda: WHEN)
    name, data, answers, label, mention = source_and_answers(kind)
    tmp_path.mkdir(parents=True, exist_ok=True)
    policy = tmp_path / "exclusions.txt"
    policy.write_text("")
    wiki = Wiki(detached=True)
    wiki.config.wikibase_url = wiki.config.concept_uri
    registry = CreationRegistry(wiki)
    person = wiki.wbi.item.new()
    person.labels.set("en", label)
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
            self.client = client
            self.exclusions = exclusions

        def register_artifact(self, path, kind, title, description, *, sha1):
            assert guarded
            assert not is_excluded(self.exclusions, path, sha1=sha1)
            item = self.client.wbi.item.new()
            item.labels.set("en", title)
            item.claims.add(
                String(
                    prop_nr=self.client.vocabulary()[0]["media_sha1"],
                    value=sha1,
                )
            )
            binding = CreationRegistry(self.client).create(
                {"kind": "source", "sha1": sha1},
                item,
                summary="fictional source registration",
            )
            return RegistrationResult(binding.qid, None, content_sha1=sha1)

    monkeypatch.setattr(source_ingestion, "protected_mutation", guard)
    monkeypatch.setattr(source_ingestion, "Registrar", Registrar)
    scratch = tmp_path / "scratch"
    if products == "checkpoint":
        source, document_dir, doc = tmp_path / name, None, None
        source.write_bytes(data)
    else:
        status, doc = saved_document(scratch, name, data, answers)
        assert status == 0
        source, document_dir = doc.source, doc.dir

    def rerun(changed, **options):
        """Run the scheduler again over the saved document."""
        return saved_document(
            scratch, name, data, answers | changed, **options
        )

    with ReviewStore(tmp_path / "review.sqlite") as store:

        def reopen(review_store=store):
            return SourceIngestion(
                source,
                tmp_path / "workflow",
                client=wiki,
                review_store=review_store,
                exclusions_file=policy,
                document_dir=document_dir,
            )

        workflow = reopen()
        if products == "checkpoint":
            prepared = workflow.prepare(
                settings=TextSettings(strategy="chunked", language="de"),
                extractor_client=ScriptedClient(*answers["extract"]),
                critic_client=ScriptedClient(*answers["critique"]),
            )
        else:
            prepared = workflow.prepare()
        yield SimpleNamespace(
            workflow=workflow,
            products=prepared,
            source=source,
            policy=policy,
            wiki=wiki,
            registry=registry,
            store=store,
            target=target,
            mention=mention,
            document=doc,
            reopen=reopen,
            rerun=rerun,
        )


@pytest.fixture(params=["checkpoint", "document"])
def lifecycle(request, tmp_path, monkeypatch):
    with lifecycle_case(tmp_path, monkeypatch, request.param) as case:
        yield case


def snapshot(case):
    return copy.deepcopy(case.wiki._recovery_pages), {
        qid: item.get_json() for qid, item in case.wiki.wbi.item.store.items()
    }


def register(case, artifact_type="certificate"):
    proposed = case.workflow.prepare_registration(
        artifact_type, "Fictional source"
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
    with pytest.raises((ReviewRefused, *PRODUCTS_REFUSED)):
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
                artifact_sha1=products.artifact.sha1, local_id=case.mention
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
        workflow = case.reopen(other)
        before = snapshot(case)
        with pytest.raises(ReviewRefused, match="original review store"):
            workflow.prepare_claims((0,))
        assert snapshot(case) == before


def test_updated_exclusions_block_an_already_registered_source(lifecycle):
    case = lifecycle
    register(case)
    case.policy.write_text(case.products.artifact.sha1)
    before = snapshot(case)
    with pytest.raises(PRODUCTS_REFUSED, match="never-ingest"):
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
    with pytest.raises(PRODUCTS_REFUSED, match="never-ingest"):
        case.workflow.apply_claims((0,))
    assert snapshot(case) == before


def test_workspace_cannot_be_used_concurrently(lifecycle):
    case = lifecycle
    with (case.workflow.directory / "lifecycle.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ReviewRefused, match="workspace is in use"):
            case.workflow.prepare()


def test_both_products_give_claim_review_the_same_warnings(
    tmp_path, monkeypatch
):
    reviewed = {}
    for products in ("checkpoint", "document"):
        with lifecycle_case(
            tmp_path / products, monkeypatch, products
        ) as case:
            register(case)
            assign(case)
            (claim,) = case.workflow.prepare_claims((0,)).claims
            reviewed[products] = (case.products, claim.selection.warnings)
    (checkpoint, warnings), (document, same) = reviewed.values()
    assert same == warnings
    assert document.extraction_attention == checkpoint.extraction_attention
    assert set(document.extraction_attention) <= set(warnings)
    assert document.extraction_attention
    assert any(FINDING["issue"] in warning for warning in warnings)


@pytest.mark.parametrize("lifecycle", ["document"], indirect=True)
def test_saved_document_products_run_no_stage(lifecycle):
    case = lifecycle
    with pytest.raises(ReviewRefused):
        case.workflow.prepare(critic_client=AuthoredAnswers(['{"done":true}']))
    assert case.workflow.prepare() == case.products


@pytest.mark.parametrize("lifecycle", ["document"], indirect=True)
def test_saved_products_changed_after_registration_refuse(lifecycle):
    case = lifecycle
    register(case)
    assign(case)
    assert case.workflow.prepare_claims((0,)).claims
    # A later run makes a different, complete critique of the same batch.
    _status, doc = case.rerun(
        {"critique": ['{"done":true}']}, refresh=("critique",)
    )
    changed = load_document_products(
        doc.source, doc.dir, exclusions=ExclusionList()
    )
    assert changed.batch == case.products.batch
    assert changed.digest != case.products.digest
    before = snapshot(case)
    for phase in (
        case.workflow.prepare_identities,
        lambda: case.workflow.apply_identities((case.mention,)),
        lambda: case.workflow.prepare_claims((0,)),
        lambda: case.workflow.apply_claims((0,)),
    ):
        with pytest.raises(ReviewRefused):
            phase()
    assert snapshot(case) == before


def test_image_review_locates_mentions_and_excerpts_in_its_reading(
    tmp_path, monkeypatch
):
    with lifecycle_case(tmp_path, monkeypatch, "document", "image") as case:
        assert case.products.kind == "media"
        register(case, "photograph")
        assign(case)
        reading = case.products.reading
        (claim,) = case.workflow.prepare_claims((0,)).claims
        located = claim.provenance["source"]
        assert reading.text[located["start"] : located["end"]] == (
            claim.claim.source.transcription_excerpt.text
        )
        assert located["physical_page"] == 1
        (occurrence,) = claim.basis["occurrences"].values()
        assert reading.text.count(occurrence["quote"]) == 1
        # The second claim cites a name the inscription gives twice.
        before = snapshot(case)
        with pytest.raises(ReviewRefused):
            case.workflow.prepare_claims((1,))
        assert snapshot(case) == before


@pytest.mark.parametrize("lifecycle", ["document"], indirect=True)
def test_products_changed_after_the_registration_proposal_refuse(lifecycle):
    case = lifecycle
    proposal = case.workflow.prepare_registration(
        "certificate", "Fictional source"
    )
    case.rerun({"critique": ['{"done":true}']}, refresh=("critique",))
    before = snapshot(case)
    with pytest.raises(ReviewRefused):
        case.workflow.register_source(
            proposal,
            expected_digest=proposal.digest,
            actor="fictional curator",
        )
    assert snapshot(case) == before


def test_saved_critique_attention_is_shown_at_claim_review(
    tmp_path, monkeypatch
):
    # A complete critique notes attention for a gap in an image's
    # interpretation context; the text checkpoint has no such context.
    with lifecycle_case(tmp_path, monkeypatch, "document", "image") as case:
        note_attention(case.document, "critique", [CONTEXT_GAP])
        register(case, "photograph")
        assign(case)
        (claim,) = case.workflow.prepare_claims((0,)).claims
        assert CONTEXT_GAP in claim.selection.warnings


GEDCOM = "0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Eugen /Löbig/\n0 TRLR\n"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("fictional-loebig-family.ged", "gedcom"),
        ("fictional-family-booklet.pdf", "pdf"),
        ("fictional-family-slides.pptx", "presentation"),
    ],
)
@pytest.mark.parametrize("lifecycle", ["checkpoint"], indirect=True)
def test_the_text_checkpoint_refuses_containers_and_gedcom_with_a_reason(
    lifecycle, tmp_path, name, kind
):
    case = lifecycle
    # The file type gives the kind, as it does in a run, whatever the bytes.
    source = tmp_path / "sources" / name
    source.parent.mkdir()
    source.write_text(GEDCOM)
    workspace = tmp_path / "other-addition"
    with pytest.raises(UnsupportedKind) as refused:
        SourceIngestion(
            source,
            workspace,
            client=case.wiki,
            review_store=case.store,
            exclusions_file=case.policy,
        )
    assert refused.value.kind == kind
    assert str(refused.value)


@pytest.mark.parametrize("lifecycle", ["checkpoint"], indirect=True)
def test_the_text_checkpoint_sends_an_image_to_its_saved_views(
    lifecycle, tmp_path
):
    case = lifecycle
    source = tmp_path / "sources" / GRAVESTONE_FILE
    source.parent.mkdir()
    source.write_bytes(gravestone_image())
    workspace = tmp_path / "other-addition"
    with pytest.raises(ProductsRefused) as refused:
        SourceIngestion(
            source,
            workspace,
            client=case.wiki,
            review_store=case.store,
            exclusions_file=case.policy,
        )
    assert "document_dir" in str(refused.value)
