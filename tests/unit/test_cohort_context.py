"""Creation advice includes untyped approved work without asserting facts."""

from dataclasses import replace

import pytest

from tests.unit.test_cohort_matching import source
from tests.unit.test_graph_projection import graph as graph
from tests.unit.test_reconcile import fictional_births
from yizkr.core.reviewed_creation import creation_review_evidence
from yizkr.core.write_recovery import AmbiguousCreation, write_existing
from yizkr.pipeline.cohort_context import (
    CohortContextRefused,
    capture_cohort_context,
    identity_facts,
    matching_candidates,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    prepare_review,
)


def authorize(registry, directory):
    current = source()
    item = registry.client.wbi.item.new()
    item.labels.set("en", "Gustav")
    item.descriptions.set("en", "fictional approved creation")
    request = ReviewRequest(
        sources=(current,),
        proposals=(
            ReviewProposal(
                operation="new_entity",
                mentions=(
                    MentionKey(
                        artifact_sha1=current.artifact_sha1, local_id="gustav"
                    ),
                ),
                evidence={
                    "reviewed_creation": creation_review_evidence(
                        item.get_json()
                    )
                },
            ),
        ),
    )
    with ReviewStore(directory / "approval.sqlite") as store:
        prepared = prepare_review(store, request)
        (proposal,) = prepared.proposals
        decision = store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time="2026-01-01T12:00:00Z",
        )
        authorization = registry.authorize_reviewed_creation(
            decision,
            prepared.mentions[current.artifact_sha1, "gustav"],
            initial_payload=item.get_json(),
        )
    return authorization, item


def prepared(directory, sources):
    with ReviewStore(directory / "matching.sqlite") as store:
        return prepare_review(store, ReviewRequest(sources=sources))


def refresh(wiki, service):
    service.revisions = wiki.entity_revisions(wiki.list_entity_ids())


def test_pending_authorization_blocks_matching_creation_without_fake_qid(
    graph, tmp_path
):
    wiki, registry, _, _, _ = graph
    authorization, _ = authorize(registry, tmp_path)
    pages = dict(wiki._recovery_pages)
    writes = wiki.wbi.item.writes
    context = capture_cohort_context(registry)
    assert context.creations[0].authorization == authorization
    assert context.creations[0].binding is None
    sources = (source(),)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    (mention,) = result.mentions
    assert mention.pending == context.creations
    assert "pending_reviewed_creation" in mention.blockers
    assert all(
        candidate.qid.startswith("Q") for candidate in mention.candidates
    )
    assert not context.uncovered_qids
    assert pages == wiki._recovery_pages and writes == wiki.wbi.item.writes


def test_unindexed_kindless_creation_remains_a_candidate_after_rename(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    authorization, item = authorize(registry, tmp_path)
    binding = registry.create(
        authorization.operation, item, summary="fictional creation"
    )
    entity = wiki.wbi.item.get(binding.qid)
    entity.labels.set("en", "Curator's unrelated display name")
    write_existing(entity, summary="fictional rename")
    refresh(wiki, service)
    before = dict(wiki._recovery_pages)
    context = capture_cohort_context(registry)
    sources = (source(),)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    assert binding.qid not in context.projection.bindings
    assert all(row.qid != binding.qid for row in context.projection.roster)
    (candidate,) = [
        c for c in result.mentions[0].candidates if c.qid == binding.qid
    ]
    assert candidate.binding.model_dump() == binding.model_dump()
    assert candidate.graph is None and candidate.current_facts == ()
    assert candidate.creations[0].authorization.context.label == "Gustav"
    assert not candidate.blockers and not context.uncovered_qids
    assert before == wiki._recovery_pages
    assert not wiki.wbi.item.get(binding.qid).claims.get_json()


def test_operational_and_graph_candidates_union_by_verified_qid(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    authorization, item = authorize(registry, tmp_path)
    binding = registry.create(
        authorization.operation, item, summary="fictional creation"
    )
    registry.index_binding(binding)
    service.people = (*service.people, binding)
    refresh(wiki, service)
    context = capture_cohort_context(registry)
    sources = (source(),)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    (candidate,) = [
        c for c in result.mentions[0].candidates if c.qid == binding.qid
    ]
    assert candidate.graph is not None and candidate.creations
    assert candidate.current_facts
    assert all(
        "label" not in row and "aliases" not in row
        for row in candidate.current_facts
    )


def test_unknown_untyped_item_makes_creation_coverage_incomplete(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    item = wiki.wbi.item.new()
    item.labels.set("en", "Unclassified existing identity")
    binding = registry.create(
        {"kind": "fictional-unknown-context"}, item, summary="fixture"
    )
    refresh(wiki, service)
    context = capture_cohort_context(registry)
    assert context.uncovered_qids == (binding.qid,)
    sources = (source(),)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    assert "uncovered_kindless_entities" in result.mentions[0].blockers


def test_ambiguous_dispatch_stays_pending_and_blocks_only_matching_context(
    graph, tmp_path, monkeypatch
):
    _, registry, _, _, _ = graph
    authorize(registry, tmp_path)

    def ambiguous(authorization):
        raise AmbiguousCreation(
            "dispatched creation has no unique visible effect"
        )

    monkeypatch.setattr(registry, "reviewed_creation_binding", ambiguous)
    context = capture_cohort_context(registry)
    assert context.creations[0].binding is None
    assert context.creations[0].ambiguity
    current = source()
    sources = (current,)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    assert "ambiguous_reviewed_creation" in result.mentions[0].blockers
    unrelated = current.batch.model_copy(deep=True)
    unrelated.entities[0].kind = "organization"
    unrelated.entities[0].structure = None
    sources = (replace(current, batch=unrelated),)
    result = matching_candidates(context, sources, prepared(tmp_path, sources))
    assert not result.mentions[0].pending


def test_capture_refuses_creation_authority_changed_after_graph_snapshot(
    graph, monkeypatch
):
    _, registry, _, _, _ = graph
    calls = iter([(), (object(),)])
    monkeypatch.setattr(
        registry, "reviewed_creation_authorizations", lambda: next(calls)
    )
    with pytest.raises(CohortContextRefused, match="changed"):
        capture_cohort_context(registry)


def test_previously_inspected_candidate_survives_display_changes(
    graph, tmp_path
):
    wiki, registry, registered, _, service = graph
    first = capture_cohort_context(registry)
    qid = registered.binding.qid
    facts = identity_facts(first.projection, qid)
    entity = wiki.wbi.item.get(qid)
    entity.labels.set("en", "Another chosen display name")
    write_existing(entity, summary="fictional curator edit")
    refresh(wiki, service)
    context = capture_cohort_context(registry)
    assert identity_facts(context.projection, qid) == facts
    current = source()
    unrelated = current.batch.model_copy(deep=True)
    unrelated.entities[0].label = "Unmatched source spelling"
    unrelated.entities[0].aliases = []
    unrelated.entities[0].structure = None
    sources = (replace(current, batch=unrelated),)
    review = prepared(tmp_path, sources)
    found = matching_candidates(
        context,
        sources,
        review,
        previous_qids={(current.artifact_sha1, "gustav"): (qid,)},
    )
    assert not found.graph.mentions[0].candidates
    (candidate,) = found.mentions[0].candidates
    assert candidate.qid == qid and candidate.current_facts == facts
    assert candidate.binding == registered.binding


def test_current_alias_on_kindless_creation_is_a_candidate(graph, tmp_path):
    wiki, registry, _, _, service = graph
    authorization, item = authorize(registry, tmp_path)
    binding = registry.create(
        authorization.operation, item, summary="fictional creation"
    )
    entity = wiki.wbi.item.get(binding.qid)
    entity.aliases.set("en", ["Gustl"])
    write_existing(entity, summary="fictional curator alias")
    refresh(wiki, service)
    current = source("naturalisation-1952")
    current.batch.entities[0].label = "Gustl"
    current.batch.entities[0].aliases = []
    # The matcher can only use this current display term, not old source names.
    current.batch.entities[0].structure.given = []
    current.batch.entities[0].structure.surnames = []
    context = capture_cohort_context(registry)
    creation = next(
        c for c in context.creations if c.binding.qid == binding.qid
    )
    assert "Gustl" in creation.current_terms
    assert all(r.qid != binding.qid for r in context.projection.roster)
    without_terms = context.model_copy(
        update={
            "creations": tuple(
                c.model_copy(update={"current_terms": ()})
                for c in context.creations
            )
        }
    )
    original_only = matching_candidates(
        without_terms, (current,), prepared(tmp_path, (current,))
    )
    assert binding.qid not in {
        c.qid for c in original_only.mentions[0].candidates
    }
    matches = matching_candidates(
        context, (current,), prepared(tmp_path, (current,))
    )
    assert binding.qid in {c.qid for c in matches.mentions[0].candidates}


def test_identity_facts_preserve_evidence_and_ignore_reading_order(graph):
    _, registry, registered, _, _ = graph
    projection = capture_cohort_context(registry).projection
    qid = registered.binding.qid
    readings = fictional_births(qid)

    def with_births(births):
        return projection.model_copy(
            update={
                "roster": tuple(
                    row.model_copy(update={"births": births})
                    if row.qid == qid
                    else row
                    for row in projection.roster
                )
            }
        )

    facts = identity_facts(with_births(readings), qid)
    assert len(facts[0]["births"]) == 2
    assert facts == identity_facts(with_births(tuple(reversed(readings))), qid)
    assert facts == identity_facts(with_births((*readings, readings[0])), qid)
    assert facts != identity_facts(with_births(readings[:1]), qid)
    changed_rank = readings[0].model_copy(update={"rank": "deprecated"})
    assert facts != identity_facts(
        with_births((changed_rank, readings[1])), qid
    )
