"""Shared server assignment authority over actual fictional review records."""

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from tests.unit.test_identity_review import decide, observed, proposal
from tests.unit.test_pipeline import FakeWiki
from yizkr.core.identity_assignments import (
    AssignmentConstraint,
    AssignmentDelta,
    AssignmentEvidence,
    AssignmentRefused,
    AssignmentTarget,
    StaleAssignments,
)
from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.core.write_recovery import (
    GENERATION_PAGE,
    CreationRefused,
    CreationRegistry,
    write_existing,
)
from yizkr.pipeline.identity_review import ReviewStore

HANG_GUARD = 30


def entity(wiki, suffix="first"):
    item = wiki.wbi.item.new()
    item.labels.set("en", f"Leopold Löbig: {suffix}")
    return CreationRegistry(wiki).create(
        {"kind": "fictional-assignment", "fixture": suffix},
        item,
        summary="fictional entity fixture",
    )


@pytest.fixture
def authority(tmp_path):
    wiki = FakeWiki(detached=True)
    registry = CreationRegistry(wiki)
    binding = entity(wiki)
    registry.index_binding(binding)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        yield wiki, registry, binding, store, mentions


def reviewed(store, mentions, binding):
    return [
        decide(store, proposal(store, "assign", [m], binding=binding))
        for m in mentions
    ]


def delta(mentions, decisions, targets=(), constraints=()):
    return AssignmentDelta(
        assignments=tuple(
            AssignmentTarget(
                mention_id=m.id,
                mention_revision=m.revision,
                entity_id=b.entity_id,
            )
            for m, b in targets
        ),
        constraints=tuple(constraints),
        evidence=AssignmentEvidence(
            mentions={m.id: m.model_dump(mode="json") for m in mentions},
            decisions={d.id: d.model_dump(mode="json") for d in decisions},
        ),
    )


def different(decision):
    return AssignmentConstraint(
        kind="different",
        mentions=tuple(sorted(decision.proposal.mentions)),
        decision_id=decision.id,
        decision_revision=decision.revision,
    )


def test_binding_index_keeps_original_receipt_and_renamed_entity():
    wiki = FakeWiki(detached=True)
    registry = CreationRegistry(wiki)
    binding = entity(wiki)
    pages = dict(wiki._recovery_pages)
    original = registry.verify_binding(binding)
    assert original.binding_revision == 1
    assert original.binding.revision == binding.revision
    assert wiki._recovery_pages == pages
    indexed = registry.index_binding(binding)
    assert all(
        wiki._recovery_pages[key] == page for key, page in pages.items()
    )
    item = wiki.wbi.item.get(binding.qid)
    item.labels.set("en", "Leopold, curator's display name")
    write_existing(item, summary="fictional curator edit")
    fresh = CreationRegistry(copy.copy(wiki))
    assert fresh.binding(binding.entity_id) == indexed
    assert fresh.binding("f" * 32) is None
    before = dict(wiki._recovery_pages)
    assert fresh.index_binding(binding) == indexed
    assert wiki._recovery_pages == before


def test_absent_generation_reads_do_not_initialize():
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    registry = CreationRegistry(wiki)
    assert registry.assignment_snapshot().generation is None
    assert registry.binding("a" * 32) is None
    assert wiki._recovery_pages == {}
    empty = AssignmentDelta(evidence=AssignmentEvidence())
    with pytest.raises(AssignmentRefused, match="established"):
        registry.append_assignments(empty, expected_head=None)
    assert wiki._recovery_pages == {}


def test_fabricated_or_changed_binding_cannot_be_indexed(authority):
    wiki, registry, binding, _, _ = authority
    before = dict(wiki._recovery_pages)
    for update in (
        {"qid": "Q9999"},
        {"entity_id": "f" * 32},
        {"revision": 9999},
    ):
        with pytest.raises(CreationRefused):
            registry.index_binding(binding.model_copy(update=update))
    assert wiki._recovery_pages == before


def test_multi_mention_commit_is_atomic_and_recovers_repeat(
    authority, monkeypatch
):
    wiki, registry, binding, store, mentions = authority
    request = delta(
        mentions,
        reviewed(store, mentions[:2], binding),
        [(m, binding) for m in mentions[:2]],
    )
    old_items = {
        key: value.get_json() for key, value in wiki.wbi.item.store.items()
    }
    reached, release = Event(), Event()
    create = wiki.create_page_once

    def held(title, text, summary):
        if "/assignments/" in title:
            reached.set()
            assert release.wait(HANG_GUARD)
        return create(title, text, summary)

    monkeypatch.setattr(wiki, "create_page_once", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            registry.append_assignments, request, expected_head=None
        )
        try:
            assert reached.wait(HANG_GUARD)
            assert (
                CreationRegistry(copy.copy(wiki))
                .assignment_snapshot()
                .assignments
                == {}
            )
        finally:
            release.set()
        accepted = future.result(timeout=HANG_GUARD)
    assert set(accepted.assignments) == {m.id for m in mentions[:2]}
    assert len(accepted.history) == 1
    before = dict(wiki._recovery_pages)
    assert (
        CreationRegistry(wiki).append_assignments(request, expected_head=None)
        == accepted
    )
    assert (
        registry.append_assignments(request, expected_head=accepted.head)
        == accepted
    )
    assert wiki._recovery_pages == before
    assert {
        key: value.get_json() for key, value in wiki.wbi.item.store.items()
    } == old_items


@pytest.mark.parametrize("failure", [ConnectionError, KeyboardInterrupt])
def test_lost_publication_response_and_fresh_retry(
    authority, monkeypatch, failure
):
    wiki, registry, binding, store, mentions = authority
    request = delta(
        mentions,
        reviewed(store, mentions[:1], binding),
        [(mentions[0], binding)],
    )
    create = wiki.create_page_once

    def lost(title, text, summary):
        page = create(title, text, summary)
        if "/assignments/" in title:
            raise failure("fictional lost acknowledgement")
        return page

    monkeypatch.setattr(wiki, "create_page_once", lost)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            registry.append_assignments(request, expected_head=None)
        accepted = CreationRegistry(copy.copy(wiki)).assignment_snapshot()
    else:
        accepted = registry.append_assignments(request, expected_head=None)
    assert len(accepted.history) == 1
    assert (
        CreationRegistry(copy.copy(wiki)).append_assignments(
            request, expected_head=None
        )
        == accepted
    )


def test_two_clients_from_same_head_accept_only_one_whole_delta(
    authority, monkeypatch
):
    wiki, registry, binding, store, mentions = authority
    decisions = reviewed(store, mentions[:2], binding)
    requests = [
        delta(mentions, [d], [(m, binding)])
        for m, d in zip(mentions[:2], decisions, strict=True)
    ]
    rendezvous = Barrier(2)
    create = wiki.create_page_once

    def compete(title, text, summary):
        if "/assignments/" in title:
            rendezvous.wait(HANG_GUARD)
        return create(title, text, summary)

    monkeypatch.setattr(wiki, "create_page_once", compete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                CreationRegistry(copy.copy(wiki)).append_assignments,
                request,
                expected_head=None,
            )
            for request in requests
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=HANG_GUARD))
            except StaleAssignments:
                outcomes.append(None)
    assert sum(value is not None for value in outcomes) == 1
    saved = registry.assignment_snapshot()
    assert len(saved.assignments) == len(saved.history) == 1


def test_same_entity_rereading_preserves_original_assignment_revision(
    authority,
):
    wiki, registry, binding, store, mentions = authority
    decisions = reviewed(store, mentions[:1], binding)
    request = delta(mentions, decisions, [(mentions[0], binding)])
    first = registry.append_assignments(request, expected_head=None)
    newer = mentions[0].model_copy(
        update={"revision": mentions[0].revision + 1}
    )
    reread = delta([newer], decisions, [(newer, binding)])
    before = dict(wiki._recovery_pages)
    assert (
        registry.append_assignments(reread, expected_head=first.head) == first
    )
    assert wiki._recovery_pages == before


def test_reassignment_and_same_decision_merging_existing_entities_refuse(
    authority,
):
    wiki, registry, binding, store, mentions = authority
    other = entity(wiki, "second")
    registry.index_binding(other)
    first = registry.append_assignments(
        delta(
            mentions,
            reviewed(store, mentions[:1], binding),
            [(mentions[0], binding)],
        ),
        expected_head=None,
    )
    different_target = delta(
        mentions, reviewed(store, mentions[:1], other), [(mentions[0], other)]
    )
    before = dict(wiki._recovery_pages)
    with pytest.raises(AssignmentRefused, match="reassignment"):
        registry.append_assignments(different_target, expected_head=first.head)
    assert wiki._recovery_pages == before
    second = registry.append_assignments(
        delta(
            mentions,
            reviewed(store, mentions[1:2], other),
            [(mentions[1], other)],
        ),
        expected_head=first.head,
    )
    same = decide(store, proposal(store, "same", mentions[:2]))
    before = dict(wiki._recovery_pages)
    with pytest.raises(AssignmentRefused, match="merge established"):
        registry.append_assignments(
            delta(mentions, [same]), expected_head=second.head
        )
    assert wiki._recovery_pages == before


def test_negative_evidence_cannot_be_omitted_or_crossed(authority):
    wiki, registry, binding, store, mentions = authority
    decision = decide(store, proposal(store, "different", mentions[:2]))
    constraint = different(decision)
    first = registry.append_assignments(
        delta(mentions, [decision], constraints=[constraint]),
        expected_head=None,
    )
    positive = reviewed(store, mentions[:1], binding)
    with pytest.raises(AssignmentRefused, match="negative evidence"):
        registry.append_assignments(
            delta(mentions, positive, [(mentions[0], binding)]),
            expected_head=first.head,
        )
    accepted = registry.append_assignments(
        delta(mentions, [decision, *positive], [(mentions[0], binding)]),
        expected_head=first.head,
    )
    with pytest.raises(AssignmentRefused, match="retained different"):
        registry.append_assignments(
            delta(
                mentions,
                [decision, *reviewed(store, mentions[1:2], binding)],
                [(mentions[1], binding)],
            ),
            expected_head=accepted.head,
        )
    same = decide(store, proposal(store, "same", mentions[:2]))
    with pytest.raises(AssignmentRefused, match="retained different"):
        registry.append_assignments(
            delta(mentions, [decision, same]), expected_head=accepted.head
        )


def test_rejected_target_survives_later_import(authority):
    wiki, registry, binding, store, mentions = authority
    rejected = decide(
        store,
        proposal(store, "assign", mentions[:1], binding=binding),
        outcome="reject",
    )
    constraint = AssignmentConstraint(
        kind="rejected_target",
        mentions=(mentions[0].id,),
        entity_id=binding.entity_id,
        decision_id=rejected.id,
        decision_revision=rejected.revision,
    )
    saved = registry.append_assignments(
        delta(mentions, [rejected], constraints=[constraint]),
        expected_head=None,
    )
    same = decide(store, proposal(store, "same", mentions[:2]))
    positive = reviewed(store, mentions[1:2], binding)
    with pytest.raises(AssignmentRefused, match="rejected target"):
        registry.append_assignments(
            delta(
                mentions,
                [rejected, same, *positive],
                [(mentions[0], binding), (mentions[1], binding)],
            ),
            expected_head=saved.head,
        )


def test_new_entity_decision_alone_never_assigns_an_existing_entity(authority):
    _, registry, binding, store, mentions = authority
    create = decide(store, proposal(store, "new_entity", mentions[:1]))
    with pytest.raises(AssignmentRefused, match="affirmative"):
        registry.append_assignments(
            delta(mentions, [create], [(mentions[0], binding)]),
            expected_head=None,
        )


def test_edited_chain_or_changed_generation_refuses(authority):
    wiki, registry, binding, store, mentions = authority
    request = delta(
        mentions,
        reviewed(store, mentions[:1], binding),
        [(mentions[0], binding)],
    )
    registry.append_assignments(request, expected_head=None)
    title = next(key for key in wiki._recovery_pages if "/assignments/" in key)
    wiki._recovery_edited.add(title)
    with pytest.raises(RecoveryEvidenceError):
        CreationRegistry(wiki).assignment_snapshot()
    wiki._recovery_edited.clear()
    wiki._recovery_edited.add(GENERATION_PAGE)
    with pytest.raises(RecoveryEvidenceError):
        registry.assignment_snapshot()


def test_retained_admission_refuses_before_index_or_assignment_mutation(
    authority, monkeypatch
):
    wiki, registry, binding, store, mentions = authority
    another = entity(wiki, "another")
    request = delta(
        mentions,
        reviewed(store, mentions[:1], binding),
        [(mentions[0], binding)],
    )
    before = dict(wiki._recovery_pages)

    def refuse(_):
        raise ValueError("retained code is dirty or unknown")

    monkeypatch.setattr("yizkr.core.instance.require_retained_code", refuse)
    with pytest.raises(ValueError, match="dirty or unknown"):
        registry.index_binding(another)
    with pytest.raises(ValueError, match="dirty or unknown"):
        registry.append_assignments(request, expected_head=None)
    assert wiki._recovery_pages == before
