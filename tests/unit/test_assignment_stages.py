"""Current SQLite review is required before shared assignment acceptance."""

from __future__ import annotations

import copy
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from tests.unit.test_pipeline import FakeWiki
from tests.unit.test_review_stages import request as fictional_request
from yizkr.core.identity_assignments import AssignmentRefused, StaleAssignments
from yizkr.core.write_recovery import CreationRefused, CreationRegistry
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    ReviewProposal,
    ReviewRequest,
    prepare_review,
)

WHEN = "2026-01-01T12:00:00+00:00"


def binding(wiki, ordinal=1):
    item = wiki.wbi.item.new()
    item.labels.set("en", "Leopold Löbig")
    return CreationRegistry(wiki).create(
        {"kind": "fictional-reviewed-target", "ordinal": ordinal},
        item,
        summary="fixture: establish target",
    )


def assigned_request(target):
    request = fictional_request()
    for entity in request.sources[0].batch.entities:
        entity.aliases = [entity.label]
    first, second = [proposal.mentions[0] for proposal in request.proposals]
    return replace(
        request,
        proposals=(
            ReviewProposal(
                operation="assign", mentions=(first,), target=target
            ),
            ReviewProposal(operation="same", mentions=(first, second)),
        ),
        current_targets=(target,),
    )


def approve(store, request):
    prepared = prepare_review(store, request)
    for proposal in prepared.proposals:
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
        )
    return prepared


def graph_contents(wiki):
    return copy.deepcopy(
        {qid: item.get_json() for qid, item in wiki.wbi.item.store.items()}
    )


def test_explicit_scope_keeps_same_connected_occurrence_contextual(tmp_path):
    wiki = FakeWiki()
    target = binding(wiki)
    request = assigned_request(target)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = approve(store, request)
        selected, contextual = list(prepared.mentions.values())
        accepted = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki), scope={selected.id}
        )
        assert set(accepted.snapshot.assignments) == {selected.id}
        assert contextual.id not in accepted.snapshot.assignments
        evidence = accepted.snapshot.history[0].delta.evidence
        assert set(evidence.mentions) == {selected.id, contextual.id}
        assert {
            d["proposal"]["operation"] for d in evidence.decisions.values()
        } == {"assign", "same"}
        continued = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki), scope={contextual.id}
        )
        assert set(continued.snapshot.assignments) == {
            selected.id,
            contextual.id,
        }
        assert (
            continued.snapshot.assignments[selected.id]
            == accepted.snapshot.assignments[selected.id]
        )


def test_default_empty_request_retains_noop_but_empty_selection_refuses(
    tmp_path,
):
    wiki = FakeWiki()
    registry = CreationRegistry(wiki)
    before = dict(wiki._recovery_pages)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        result = accept_reviewed_assignments(
            store, ReviewRequest(sources=()), registry
        )
        assert result.snapshot == registry.assignment_snapshot()
        assert not result.awaiting_creation and not result.unassigned
        with pytest.raises(AssignmentRefused, match="scope"):
            accept_reviewed_assignments(
                store, ReviewRequest(sources=()), registry, scope=set()
            )
    assert wiki._recovery_pages == before


def test_reviewed_same_component_survives_reopen_and_harmless_observation(
    tmp_path,
):
    wiki = FakeWiki()
    target = binding(wiki)
    request = assigned_request(target)
    path = tmp_path / "review.sqlite"
    graph = graph_contents(wiki)
    with ReviewStore(path) as store:
        original = approve(store, request)
        accepted = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki)
        )
    assert set(accepted.snapshot.assignments) == {
        mention.id for mention in original.mentions.values()
    }
    assert {a.entity_id for a in accepted.snapshot.assignments.values()} == {
        target.entity_id
    }
    assert not accepted.awaiting_creation and not accepted.unassigned
    assert graph_contents(wiki) == graph

    source = request.sources[0]
    changed = source.batch.model_copy(deep=True)
    changed.entities[0].label = "Leopold"
    changed.entities.reverse()
    request = replace(
        request,
        sources=(replace(source, batch=changed),),
    )
    with ReviewStore(path) as store:
        repeated = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki)
        )
        assert repeated.snapshot == accepted.snapshot
        current = prepare_review(store, request)
        assert max(m.revision for m in current.mentions.values()) > 1
    assert graph_contents(wiki) == graph


def test_material_change_refuses_without_assignment_or_binding_index(tmp_path):
    wiki = FakeWiki()
    target = binding(wiki)
    request = assigned_request(target)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        approve(store, request)
        changed = request.sources[0].batch.model_copy(deep=True)
        changed.entities[0].aliases.append("Leopold")
        current = replace(
            request,
            sources=(replace(request.sources[0], batch=changed),),
        )
        pages = dict(wiki._recovery_pages)
        with pytest.raises(ReviewRefused, match="current, unblocked"):
            accept_reviewed_assignments(store, current, CreationRegistry(wiki))
        assert wiki._recovery_pages == pages


@pytest.mark.parametrize("change", ["decision", "new_scope"])
def test_review_change_between_projection_and_pin_refuses(
    tmp_path, monkeypatch, change
):
    wiki = FakeWiki()
    request = assigned_request(binding(wiki))
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        prepared = approve(store, request)
        proposal = prepared.proposals[0]
        original = store.pin_projection

        @contextmanager
        def concurrent_edit(projection, mentions):
            with ReviewStore(path) as other:
                if change == "decision":
                    other.decide(
                        proposal.id,
                        proposal.revision,
                        expected_digest=proposal.digest,
                        outcome="reject",
                        actor="curator",
                        time=WHEN,
                        expected_decision_revision=1,
                    )
                else:
                    other.propose(
                        "different",
                        list(prepared.mentions.values()),
                        basis={"evidence": "curator flagged distinct people"},
                        origin={"method": "human-proposal"},
                    )
            with original(projection, mentions):
                yield

        monkeypatch.setattr(store, "pin_projection", concurrent_edit)
        pages = dict(wiki._recovery_pages)
        with pytest.raises(ReviewRefused, match="changed before acceptance"):
            accept_reviewed_assignments(store, request, CreationRegistry(wiki))
        assert wiki._recovery_pages == pages


def test_review_import_is_excluded_until_server_acceptance_finishes(
    tmp_path, monkeypatch
):
    wiki = FakeWiki()
    request = assigned_request(binding(wiki))
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store, ReviewStore(path) as other:
        proposal = approve(store, request).proposals[0]
        other._db.execute("PRAGMA busy_timeout=0")
        registry = CreationRegistry(wiki)
        append = registry.append_assignments

        def competing_review(delta, *, expected_head):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.decide(
                    proposal.id,
                    proposal.revision,
                    expected_digest=proposal.digest,
                    outcome="reject",
                    actor="curator",
                    time=WHEN,
                    expected_decision_revision=1,
                )
            return append(delta, expected_head=expected_head)

        monkeypatch.setattr(registry, "append_assignments", competing_review)
        result = accept_reviewed_assignments(store, request, registry)
        assert len(result.snapshot.assignments) == 2
        assert other.get_decision(proposal.id).outcome == "approve"


def test_creation_approval_waits_without_allocating_entity_or_assignment(
    tmp_path,
):
    wiki = FakeWiki()
    request = fictional_request()
    pages = dict(wiki._recovery_pages)
    graph = graph_contents(wiki)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = approve(store, request)
        accepted = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki)
        )
    assert accepted.awaiting_creation == tuple(
        sorted((mention.id,) for mention in prepared.mentions.values())
    )
    assert not accepted.snapshot.assignments and not accepted.unassigned
    assert graph_contents(wiki) == graph
    assert wiki._recovery_pages == pages


@pytest.mark.parametrize("operation", ["different", "assign"])
def test_restored_review_store_cannot_omit_server_negative(
    tmp_path, operation
):
    wiki = FakeWiki()
    target = binding(wiki)
    request = fictional_request()
    first, second = [proposal.mentions[0] for proposal in request.proposals]
    path, old_path = tmp_path / "current.sqlite", tmp_path / "earlier.sqlite"
    with ReviewStore(path) as store:
        prepare_review(store, replace(request, proposals=()))
    shutil.copyfile(path, old_path)
    negative = ReviewProposal(
        operation=operation,
        mentions=(first, second) if operation == "different" else (first,),
        target=target if operation == "assign" else None,
    )
    refused_request = replace(
        request, proposals=(negative,), current_targets=(target,)
    )
    with ReviewStore(path) as store:
        proposal = prepare_review(store, refused_request).proposals[0]
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve" if operation == "different" else "reject",
            actor="curator",
            time=WHEN,
        )
        accepted = accept_reviewed_assignments(
            store, refused_request, CreationRegistry(wiki)
        )
    assert len(accepted.snapshot.constraints) == 1
    current = replace(
        request,
        proposals=tuple(
            ReviewProposal(operation="assign", mentions=(key,), target=target)
            for key in (first, second)
        ),
        current_targets=(target,),
    )
    with ReviewStore(old_path) as store:
        approve(store, current)
        pages = dict(wiki._recovery_pages)
        with pytest.raises(AssignmentRefused, match="negative evidence"):
            accept_reviewed_assignments(store, current, CreationRegistry(wiki))
        assert wiki._recovery_pages == pages


def test_assigned_components_cannot_move_to_another_existing_entity(tmp_path):
    wiki = FakeWiki()
    first, second = binding(wiki), binding(wiki, 2)
    request = assigned_request(first)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        approve(store, request)
        accepted = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki)
        )
        changed = replace(
            request,
            proposals=(
                request.proposals[0].model_copy(update={"target": second}),
                request.proposals[1],
            ),
            current_targets=(first, second),
        )
        approve(store, changed)
        with pytest.raises((AssignmentRefused, ReviewRefused)):
            accept_reviewed_assignments(store, changed, CreationRegistry(wiki))
        assert (
            CreationRegistry(wiki).assignment_snapshot() == accepted.snapshot
        )


def test_target_generation_and_expected_head_are_verified(tmp_path):
    wiki = FakeWiki()
    target = binding(wiki)
    request = assigned_request(target)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        approve(store, request)
        accepted = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki)
        )
        with pytest.raises(StaleAssignments):
            accept_reviewed_assignments(
                store, request, CreationRegistry(wiki), expected_head="0" * 64
            )
        changed = replace(
            request,
            current_targets=(
                target.model_copy(update={"generation": "3" * 32}),
            ),
        )
        with pytest.raises(
            (ReviewRefused, CreationRefused, AssignmentRefused)
        ):
            accept_reviewed_assignments(store, changed, CreationRegistry(wiki))
        assert (
            CreationRegistry(wiki).assignment_snapshot() == accepted.snapshot
        )


def test_lost_acceptance_reply_reuses_the_committed_assignment(
    tmp_path, monkeypatch
):
    wiki = FakeWiki()
    request = assigned_request(binding(wiki))
    registry = CreationRegistry(wiki)
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        approve(store, request)
        append = registry.append_assignments

        def lost_reply(delta, *, expected_head):
            append(delta, expected_head=expected_head)
            raise ConnectionError("fictional lost reply")

        with monkeypatch.context() as patch:
            patch.setattr(registry, "append_assignments", lost_reply)
            with pytest.raises(ConnectionError, match="fictional lost reply"):
                accept_reviewed_assignments(
                    store, request, registry, expected_head=None
                )
    committed = CreationRegistry(wiki).assignment_snapshot()
    with ReviewStore(path) as store:
        repeated = accept_reviewed_assignments(
            store, request, CreationRegistry(wiki), expected_head=None
        )
    assert repeated.snapshot == committed
    assert len(committed.history) == 1
