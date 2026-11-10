"""Recorded graph advice preserves source scope and human authority."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from tests.unit.test_graph_projection import graph as graph
from tests.unit.test_reconcile import ScriptedLlm, fictional_births
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.cohort_matching import (
    AdjudicationProvenance,
    MatchingRefused,
    load_cohort_match,
    match_cohort,
    save_cohort_match,
)
from yizkr.pipeline.graph_projection import capture_graph_projection
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.transcribe import Transcription

WHEN = "2026-01-01T12:00:00Z"
PROVIDER = AdjudicationProvenance(provider="authored", model="fake-model")


def source(key="birth-extract-1938"):
    case = source_case(key)
    batch = birth_batch(key)
    batch.entities[0].label = (
        "Gustav" if key == "birth-extract-1938" else "Gustave LOBIG"
    )
    batch.entities[0].mention_lines = [1]
    if key == "naturalisation-1952":
        # This occurrence is the displayed current name, not the former name.
        structure = batch.entities[0].structure
        structure.given = structure.given[:1]
    reading = Transcription(
        artifact=case.source_ref().artifact,
        text=case.text,
        language=case.language,
        engine="authored-fictional-text",
    )
    return ReviewSource(case.sha1, batch, reading)


def assignment_request(target, sources):
    return ReviewRequest(
        sources=sources,
        current_targets=(target,),
        proposals=tuple(
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(
                        artifact_sha1=s.artifact_sha1, local_id="gustav"
                    ),
                ),
                target=target,
            )
            for s in sources
        ),
    )


def decide(store, request, outcome="approve"):
    prepared = prepare_review(store, request)
    for proposal in prepared.proposals:
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome=outcome,
            actor="fictional curator",
            time=WHEN,
        )
    return prepare_review(store, request)


def answer(qid, *, verdict="same", confidence=0.9, duplicate=False):
    row = {
        "entity_id": "gustav",
        "qid": qid,
        "verdict": verdict,
        "confidence": confidence,
        "evidence": "compatible fictional source context",
    }
    return "\n".join(
        json.dumps(value)
        for value in ([row, row] if duplicate else [row]) + [{"done": True}]
    )


def test_source_local_ids_replay_separately_without_graph_or_review_writes(
    graph, tmp_path, monkeypatch
):
    wiki, registry, binding, _, _ = graph
    projection = capture_graph_projection(registry)
    sources = (source(), source("naturalisation-1952"))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
        pages = dict(wiki._recovery_pages)
        writes = wiki.wbi.item.writes

        def offline(*args, **kwargs):
            pytest.fail("matching reached a live graph boundary")

        monkeypatch.setattr(wiki, "api_get", offline)
        result = match_cohort(projection, sources, prepared_review=prepared)
        assert len(result.mentions) == 2
        assert {m.local_id for m in result.mentions} == {"gustav"}
        assert len({m.mention_id for m in result.mentions}) == 2
        assert {m.artifact_sha1 for m in result.mentions} == {
            s.artifact_sha1 for s in sources
        }
        for mention in result.mentions:
            assert not mention.issues
            (candidate,) = mention.candidates
            assert candidate.qid == binding.binding.qid
            assert candidate.kind == "human"
            assert "Gustave Lobig" in candidate.facts
            assert candidate.adjudication == "not_requested"
        assert result.proposal_only and not result.creation_authorized
        assert store.list_proposals() == []
        assert wiki._recovery_pages == pages
        assert wiki.wbi.item.writes == writes
    path = tmp_path / "cohort.json"
    save_cohort_match(result, path)
    assert load_cohort_match(path) == result
    assert (
        match_cohort(
            projection, tuple(reversed(sources)), prepared_review=prepared
        )
        == result
    )


def test_adjudication_reuses_existing_prompt_and_never_records_approval(
    graph, tmp_path
):
    _, registry, binding, _, _ = graph
    projection = capture_graph_projection(registry)
    sources = (source(), source("naturalisation-1952"))
    qid = binding.binding.qid
    projection = projection.model_copy(
        update={
            "roster": tuple(
                row.model_copy(update={"births": fictional_births(qid)})
                if row.qid == qid
                else row
                for row in projection.roster
            )
        }
    )
    client = ScriptedLlm(answer(qid))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
        result = match_cohort(
            projection,
            sources,
            prepared_review=prepared,
            client=client,
            provenance=PROVIDER,
        )
        assert store.list_proposals() == []
    assert len(client.prompts) == 2
    for prompt in client.prompts:
        assert all(
            value in prompt
            for value in (
                "1919-01-07",
                "1919-07-01",
                "Q801",
                "Q802",
            )
        )
    for mention in result.mentions:
        assert "Q801" in mention.candidates[0].facts
        assert "Q802" in mention.candidates[0].facts
    assert "Gustav" in client.prompts[0]
    assert result.adjudicator == PROVIDER
    assert all(
        c.verdict == "same" and c.adjudication == "complete"
        for m in result.mentions
        for c in m.candidates
    )
    assert not result.creation_authorized


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "cut_off", "confidence"]
)
def test_incomplete_model_pairs_remain_inspectable_and_unauthorized(
    graph, tmp_path, fault
):
    _, registry, binding, _, _ = graph
    projection = capture_graph_projection(registry)
    reply = {
        "missing": '{"done": true}',
        "duplicate": answer(binding.binding.qid, duplicate=True),
        "cut_off": answer(binding.binding.qid).splitlines()[0],
        "confidence": answer(binding.binding.qid, confidence=2),
    }[fault]
    sources = (source(),)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
    result = match_cohort(
        projection,
        sources,
        prepared_review=prepared,
        client=ScriptedLlm(reply),
        provenance=PROVIDER,
    )
    (mention,) = result.mentions
    (candidate,) = mention.candidates
    assert mention.status == "incomplete"
    assert candidate.adjudication == "incomplete"
    assert candidate.verdict is None
    assert candidate.blockers and mention.issues
    assert not result.creation_authorized


def test_unbound_candidates_and_empty_results_never_allow_creation(
    graph, tmp_path
):
    _, registry, _, _, _ = graph
    projection = capture_graph_projection(registry)
    unbound = projection.model_copy(
        update={
            "bindings": {},
            "unbound_qids": tuple(sorted({r.qid for r in projection.roster})),
        }
    )
    sources = (source(),)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
    result = match_cohort(unbound, sources, prepared_review=prepared)
    (candidate,) = result.mentions[0].candidates
    assert candidate.binding is None
    assert "durable_binding_unavailable" in candidate.blockers
    empty = projection.model_copy(update={"roster": (), "unbound_qids": ()})
    client = ScriptedLlm("must not be called without candidates")
    result = match_cohort(
        empty,
        sources,
        prepared_review=prepared,
        client=client,
        provenance=PROVIDER,
    )
    assert result.mentions[0].status == "no_candidates"
    assert not result.creation_authorized and not client.prompts


def test_accepted_identity_survives_rename_without_new_review_or_adjudication(
    graph, tmp_path
):
    _, registry, binding, _, _ = graph
    sources = (source(),)
    request = assignment_request(binding.binding, sources)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = decide(store, request)
        accepted = accept_reviewed_assignments(store, request, registry)
        before = store.list_proposals()
        projection = capture_graph_projection(registry)
        projection = projection.model_copy(
            update={
                "roster": tuple(
                    r.model_copy(
                        update={
                            "label": "Curator's reviewed person",
                            "aliases": [],
                        }
                    )
                    if r.qid == binding.binding.qid
                    else r
                    for r in projection.roster
                )
            }
        )
        client = ScriptedLlm("accepted identities do not need adjudication")
        result = match_cohort(
            projection,
            sources,
            prepared_review=prepared,
            client=client,
            provenance=PROVIDER,
        )
        (mention,) = result.mentions
        assert mention.status == "retained"
        assert (
            mention.accepted_assignment
            == accepted.snapshot.assignments[mention.mention_id]
        )
        assert mention.candidates[0].origin == "accepted_assignment"
        assert mention.candidates[0].label == "Curator's reviewed person"
        assert store.list_proposals() == before
        assert registry.assignment_snapshot() == accepted.snapshot
        assert not client.prompts


def test_current_review_target_is_visible_without_becoming_accepted(
    graph, tmp_path
):
    _, registry, binding, _, _ = graph
    sources = (source(),)
    request = assignment_request(binding.binding, sources)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = decide(store, request)
    client = ScriptedLlm("reviewed targets do not need adjudication")
    result = match_cohort(
        capture_graph_projection(registry),
        sources,
        prepared_review=prepared,
        client=client,
        provenance=PROVIDER,
    )
    (mention,) = result.mentions
    assert mention.reviewed_target.entity_id == binding.binding.entity_id
    assert mention.accepted_assignment is None
    assert mention.candidates[0].origin == "reviewed_assignment"
    assert not client.prompts and not result.creation_authorized


def test_restored_review_store_cannot_hide_a_retained_negative(
    graph, tmp_path
):
    _, registry, binding, _, _ = graph
    sources = (source(),)
    request = assignment_request(binding.binding, sources)
    path, older = tmp_path / "review.sqlite", tmp_path / "older.sqlite"
    with ReviewStore(path) as store:
        prepare_review(store, request)
    shutil.copyfile(path, older)
    with ReviewStore(path) as store:
        decide(store, request, "reject")
        accepted = accept_reviewed_assignments(store, request, registry)
    assert accepted.snapshot.constraints and not accepted.snapshot.assignments
    with ReviewStore(older) as store:
        prepared = prepare_review(store, request)
    result = match_cohort(
        capture_graph_projection(registry), sources, prepared_review=prepared
    )
    (mention,) = result.mentions
    assert mention.status == "blocked"
    assert "prior_negative_context_unavailable" in mention.issues
    assert "retained_negative_decision" in mention.candidates[0].blockers


def test_rejected_target_applies_to_a_reviewed_same_component(graph, tmp_path):
    _, registry, binding, _, _ = graph
    sources = (source(), source("naturalisation-1952"))
    keys = tuple(
        MentionKey(artifact_sha1=s.artifact_sha1, local_id="gustav")
        for s in sources
    )
    request = ReviewRequest(
        sources=sources,
        current_targets=(binding.binding,),
        proposals=(
            ReviewProposal(operation="same", mentions=keys),
            ReviewProposal(
                operation="assign", mentions=(keys[0],), target=binding.binding
            ),
        ),
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, request)
        for proposal in prepared.proposals:
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve"
                if proposal.operation == "same"
                else "reject",
                actor="fictional curator",
                time=WHEN,
            )
        prepared = prepare_review(store, request)
        assert all(
            app.status == "applied" for app in prepared.projection.applications
        ), tuple(
            (app.status, app.reasons)
            for app in prepared.projection.applications
        )
        accepted = accept_reviewed_assignments(store, request, registry)
        before = store.list_proposals()
        result = match_cohort(
            capture_graph_projection(registry),
            sources,
            prepared_review=prepared,
        )
        assert len(result.mentions) == 2
        assert all(
            "retained_negative_decision" in m.candidates[0].blockers
            for m in result.mentions
        )
        assert store.list_proposals() == before
        assert registry.assignment_snapshot() == accepted.snapshot


def test_source_structure_blocks_adjudication_without_a_stored_proposal(
    graph, tmp_path
):
    _, registry, _, _, _ = graph
    sources = (source(),)
    sources[0].batch.entities[0].structure.given[0].written = "Leopold"
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
        assert not prepared.proposals and not prepared.projection.applications
    client = ScriptedLlm("invalid source records must not be adjudicated")
    result = match_cohort(
        capture_graph_projection(registry),
        sources,
        prepared_review=prepared,
        client=client,
        provenance=PROVIDER,
    )
    (mention,) = result.mentions
    assert mention.status == "blocked"
    assert any("addition:" in issue for issue in mention.issues)
    assert not client.prompts


def test_same_component_inherits_an_accepted_target_without_reassignment(
    graph, tmp_path
):
    _, registry, binding, other, _ = graph
    registry.index_binding(other)
    sources = (source(), source("naturalisation-1952"))
    path, older = tmp_path / "review.sqlite", tmp_path / "older.sqlite"
    with ReviewStore(path) as store:
        prepare_review(store, ReviewRequest(sources=sources))
    shutil.copyfile(path, older)
    request = assignment_request(binding.binding, sources[:1])
    with ReviewStore(path) as store:
        decide(store, request)
        accept_reviewed_assignments(store, request, registry)
    same = ReviewRequest(
        sources=sources,
        proposals=(
            ReviewProposal(
                operation="same",
                mentions=tuple(
                    MentionKey(
                        artifact_sha1=s.artifact_sha1, local_id="gustav"
                    )
                    for s in sources
                ),
            ),
        ),
    )
    with ReviewStore(older) as store:
        prepared = decide(store, same)
        assert prepared.projection.same
        assert not prepared.projection.assignments
    projection = capture_graph_projection(registry)
    projection = projection.model_copy(
        update={
            "roster": tuple(
                row.model_copy(
                    update={"aliases": ["Gustav Löbig", "Gustave Lobig"]}
                )
                if row.qid == other.qid
                else row
                for row in projection.roster
            )
        }
    )
    client = ScriptedLlm("the same component already has an accepted target")
    result = match_cohort(
        projection,
        sources,
        prepared_review=prepared,
        client=client,
        provenance=PROVIDER,
    )
    added = next(
        mention
        for mention in result.mentions
        if mention.artifact_sha1 == sources[1].artifact_sha1
    )
    assert added.accepted_assignment is None
    assert added.same_component_target == binding
    candidates = {candidate.qid: candidate for candidate in added.candidates}
    assert candidates[binding.binding.qid].origin == "accepted_same_component"
    assert "same_component_target_conflict" in candidates[other.qid].blockers
    assert not client.prompts


def test_same_component_cannot_hide_conflicting_accepted_entities(
    graph, tmp_path
):
    _, registry, binding, other, _ = graph
    sources = (source(), source("naturalisation-1952"))
    path, older = tmp_path / "review.sqlite", tmp_path / "older.sqlite"
    with ReviewStore(path) as store:
        prepare_review(store, ReviewRequest(sources=sources))
    shutil.copyfile(path, older)
    request = ReviewRequest(
        sources=sources,
        current_targets=(binding.binding, other),
        proposals=tuple(
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(
                        artifact_sha1=s.artifact_sha1, local_id="gustav"
                    ),
                ),
                target=target,
            )
            for s, target in zip(
                sources, (binding.binding, other), strict=True
            )
        ),
    )
    with ReviewStore(path) as store:
        decide(store, request)
        accepted = accept_reviewed_assignments(store, request, registry)
    same = ReviewRequest(
        sources=sources,
        proposals=(
            ReviewProposal(
                operation="same",
                mentions=tuple(
                    MentionKey(
                        artifact_sha1=s.artifact_sha1, local_id="gustav"
                    )
                    for s in sources
                ),
            ),
        ),
    )
    with ReviewStore(older) as store:
        prepared = decide(store, same)
        assert prepared.projection.same
        assert all(
            app.status == "applied" for app in prepared.projection.applications
        )
    result = match_cohort(
        capture_graph_projection(registry), sources, prepared_review=prepared
    )
    assert all(mention.status == "blocked" for mention in result.mentions)
    assert all(
        "same_component_target_conflict" in mention.issues
        for mention in result.mentions
    )
    assert registry.assignment_snapshot() == accepted.snapshot


def test_mismatched_products_and_missing_provider_provenance_refuse(
    graph, tmp_path
):
    _, registry, _, _, _ = graph
    sources = (source(),)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
    projection = capture_graph_projection(registry)
    changed = replace(
        sources[0],
        reading=sources[0].reading.model_copy(
            update={"text": "Leopold Löbig."}
        ),
    )
    with pytest.raises(MatchingRefused, match="source products"):
        match_cohort(projection, (changed,), prepared_review=prepared)
    with pytest.raises(MatchingRefused, match="provenance"):
        match_cohort(
            projection,
            sources,
            prepared_review=prepared,
            client=ScriptedLlm(""),
        )


def test_saved_matching_product_refuses_tampering(graph, tmp_path):
    _, registry, _, _, _ = graph
    sources = (source(),)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, ReviewRequest(sources=sources))
    result = match_cohort(
        capture_graph_projection(registry), sources, prepared_review=prepared
    )
    path = tmp_path / "cohort.json"
    save_cohort_match(result, path)
    data = json.loads(path.read_text())
    data["result"]["mentions"][0]["label"] = "changed fictional display"
    path.write_text(json.dumps(data))
    with pytest.raises(MatchingRefused, match="digest"):
        load_cohort_match(path)
