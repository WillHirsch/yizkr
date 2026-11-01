"""Durable human review over fictional source occurrences and real SQLite."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    EntityRecord,
    ExtractionRunMetadata,
)
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.identity_review import (
    DecisionInput,
    ReviewRefused,
    ReviewStore,
)
from yizkr.pipeline.review_mentions import canonical_basis, observe_mentions
from yizkr.pipeline.transcribe import Transcription

WHEN = "2026-01-01T12:00:00+00:00"
LATER = "2026-01-02T12:00:00+00:00"
HANG_GUARD = 30
NAMES = ("Leopold Löbig", "Klara Löbig", "Gustav Löbig", "Hildegard Laudler")


def reading(*, ids=("a", "b", "c", "d"), text=None, artifact=None):
    artifact = artifact or ArtifactRef.from_bytes(b"fictional family document")
    text = text or "\n".join(NAMES)
    batch = ClaimBatch(
        extraction_run=ExtractionRunMetadata(
            model_identifier="authored-fixture",
            pipeline_version="test",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        entities=[
            EntityRecord(
                id=key,
                label=name,
                aliases=[name],
                kind="human",
                mention_lines=[line],
            )
            for line, (key, name) in enumerate(zip(ids, NAMES, strict=True), 1)
        ],
    )
    transcript = Transcription(
        artifact=artifact, text=text, language="de", engine="fixture"
    )
    return batch, transcript


def observed(store, batch=None, transcript=None):
    if batch is None:
        batch, transcript = reading()
    return list(
        store.observe(
            observe_mentions(transcript.artifact.sha1, batch, transcript)
        ).values()
    )


def target(number=1):
    return EntityBinding(
        deployment_id="1" * 32,
        generation="2" * 32,
        entity_id=f"{number:032x}",
        intent_key=f"{number:064x}",
        qid=f"Q{number}",
        revision=1,
    )


def proposal(store, operation, mentions, *, basis=None, binding=None):
    return store.propose(
        operation,
        mentions,
        basis=basis or {"evidence": "authored"},
        origin={"method": "human-proposal"},
        target=binding,
    )


def decide(store, item, outcome="approve", **kwargs):
    return store.decide(
        item.id,
        item.revision,
        outcome=outcome,
        actor="curator",
        time=WHEN,
        expected_digest=item.digest,
        **kwargs,
    )


def project(store, mentions, *items, **kwargs):
    return store.project(
        mentions, bases={item.id: item.basis for item in items}, **kwargs
    )


def test_reopened_store_retains_decision_after_renumbering_and_label_edit(
    tmp_path,
):
    path = tmp_path / "review.sqlite"
    original, text = reading()
    with ReviewStore(path) as store:
        old = observed(store, original, text)
        mapping = {item.observation.local_id: item.id for item in old}
        basis = canonical_basis(original, "a", mapping)
        item = proposal(
            store, "assign", old[:1], basis=basis, binding=target()
        )
        approved = decide(store, item)
    changed = original.model_copy(deep=True)
    changed.entities[0].id = "renumbered"
    changed.entities[0].label = "Leopold"
    changed.entities.reverse()
    with ReviewStore(path) as store:
        current = observed(store, changed, text)
        by_local = {item.observation.local_id: item for item in current}
        retained = by_local["renumbered"]
        assert retained.id == old[0].id
        assert retained.revision == old[0].revision + 1
        mapping = {key: item.id for key, item in by_local.items()}
        new_basis = canonical_basis(changed, "renumbered", mapping)
        assert new_basis == basis
        again = proposal(
            store, "assign", [retained], basis=new_basis, binding=target()
        )
        assert again == item
        result = project(
            store,
            current,
            again,
            current_targets={target().entity_id: target()},
        )
        assert result.assignments == {retained.id: target()}
        assert result.decision_revisions == (approved.reference,)
        assert store.get_mention(old[0].id, 1) == old[0]


def test_ambiguous_homonyms_repeat_exact_product_but_never_follow_order(
    tmp_path,
):
    batch, text = reading()
    batch.entities = [
        EntityRecord(id=key, label=NAMES[0], kind="human", mention_lines=[1])
        for key in ("first", "second")
    ]
    with ReviewStore(tmp_path / "review.sqlite") as store:
        old = observed(store, batch, text)
        assert all(item.observation.anchor is None for item in old)
        assert observed(store, batch, text) == old
        changed = batch.model_copy(deep=True)
        changed.entities.reverse()
        new = observed(store, changed, text)
        assert {item.id for item in old}.isdisjoint(item.id for item in new)


def test_same_local_ids_in_different_sources_are_separate(tmp_path):
    batch, first = reading()
    _, second = reading(
        artifact=ArtifactRef.from_bytes(b"fictional second source")
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        found = store.observe(
            observe_mentions(first.artifact.sha1, batch, first)
            + observe_mentions(second.artifact.sha1, batch, second)
        )
        assert len(found) == 8
        assert (
            found[(first.artifact.sha1, "a")].id
            != found[(second.artifact.sha1, "a")].id
        )


def test_explicit_remapping_records_human_proof_and_refuses_owner_change(
    tmp_path,
):
    batch, text = reading()
    with ReviewStore(tmp_path / "review.sqlite") as store:
        old = observed(store, batch, text)
        corrected = text.model_copy(
            update={"text": "Family record\n" + text.text}
        )
        changed = batch.model_copy(deep=True)
        for entity in changed.entities:
            entity.mention_lines = [entity.mention_lines[0] + 1]
        observation = observe_mentions(text.artifact.sha1, changed, corrected)[
            0
        ]
        mapped = store.remap(
            old[0],
            observation,
            actor="curator",
            reason="Verified inserted heading",
            time=WHEN,
        )
        assert mapped.id == old[0].id
        assert mapped.correspondence["prior"] == old[0].reference
        assert observed(store, changed, corrected)[0] == mapped
        other = observation.model_copy(
            update={
                "artifact": "f" * 40,
                "anchor": observation.anchor.model_copy(
                    update={"owner": ArtifactRef(sha1="f" * 40)}
                ),
                "source_scope": observation.source_scope.model_copy(
                    update={"owner": ArtifactRef(sha1="f" * 40)}
                ),
            }
        )
        with pytest.raises(ReviewRefused, match="source owner"):
            store.remap(
                old[0],
                other,
                actor="curator",
                reason="Different source",
                time=WHEN,
            )


def test_changed_material_basis_reopens_only_affected_decision(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        first = proposal(store, "new_entity", mentions[:1])
        second = proposal(store, "new_entity", mentions[1:2])
        decide(store, first)
        retained = decide(store, second)
        changed = proposal(
            store,
            "new_entity",
            mentions[:1],
            basis={"evidence": "corrected source support"},
        )
        result = project(store, mentions, changed, second)
        assert {
            app.proposal_id: app.status for app in result.applications
        } == {first.id: "needs_review", second.id: "applied"}
        assert result.new_entities == (mentions[1].id,)
        assert result.decision_revisions == (retained.reference,)
        with pytest.raises(ReviewRefused, match="stale"):
            decide(store, first)


def test_rejected_match_survives_reopen_without_authorizing_new_entity(
    tmp_path,
):
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        mentions = observed(store)
        item = proposal(store, "assign", mentions[:1], binding=target())
        rejection = decide(store, item, "reject")
    with ReviewStore(path) as store:
        result = project(
            store,
            mentions,
            item,
            current_targets={target().entity_id: target()},
        )
        assert result.rejected_targets == {mentions[0].id: (target(),)}
        assert not result.assignments and not result.new_entities
        assert result.decision_revisions == (rejection.reference,)


@pytest.mark.parametrize("negative", ["same", "different"])
def test_negative_constraints_block_transitive_merges_but_not_unrelated_review(
    tmp_path, negative
):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        one = proposal(store, "same", mentions[:2])
        two = proposal(store, "same", mentions[1:3])
        no = proposal(store, negative, [mentions[0], mentions[2]])
        independent = proposal(store, "new_entity", mentions[3:])
        for item in (one, two, independent):
            decide(store, item)
        decide(store, no, "reject" if negative == "same" else "approve")
        result = project(store, mentions, one, two, no, independent)
        assert (
            len(
                [
                    item
                    for item in result.applications
                    if item.status == "blocked"
                ]
            )
            == 3
        )
        assert result.new_entities == (mentions[3].id,)
        assert not result.same


def test_rejecting_difference_does_not_approve_equivalence(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        item = proposal(store, "different", mentions[:2])
        decide(store, item, "reject")
        result = project(store, mentions, item)
        assert (
            not result.same
            and not result.different
            and not result.new_entities
        )


def test_same_assigned_entity_cannot_cross_negative_constraint(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        items = [
            proposal(store, "assign", [m], binding=target())
            for m in mentions[:2]
        ]
        no = proposal(store, "different", mentions[:2])
        for item in [*items, no]:
            decide(store, item)
        result = project(
            store,
            mentions,
            *items,
            no,
            current_targets={target().entity_id: target()},
        )
        assert not result.assignments
        assert all(app.status == "blocked" for app in result.applications)


def test_negative_target_applies_through_same_component(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        yes = proposal(store, "assign", mentions[:1], binding=target())
        no = proposal(store, "assign", mentions[1:2], binding=target())
        link = proposal(store, "same", mentions[:2])
        decide(store, yes)
        decide(store, no, "reject")
        decide(store, link)
        result = project(
            store,
            mentions,
            yes,
            no,
            link,
            current_targets={target().entity_id: target()},
        )
        assert not result.assignments and not result.same
        assert all(app.status == "blocked" for app in result.applications)


@pytest.mark.parametrize("missing_peer", [False, True])
def test_omitted_negative_basis_cannot_disappear_from_projection(
    tmp_path, missing_peer
):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        no = proposal(store, "different", mentions[:2])
        yes = proposal(store, "new_entity", mentions[:1])
        decide(store, no)
        decide(store, yes)
        current = [mentions[0]] if missing_peer else mentions
        result = project(store, current, yes)
        assert not result.new_entities
        assert (
            next(
                app for app in result.applications if app.proposal_id == yes.id
            ).status
            == "blocked"
        )


def test_current_validation_and_generation_override_old_approval(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        item = proposal(store, "assign", mentions[:1], binding=target())
        decide(store, item)
        changed = target().model_copy(update={"generation": "3" * 32})
        assert not project(
            store, mentions, item, current_targets={changed.entity_id: changed}
        ).assignments
        blocked = project(
            store,
            mentions,
            item,
            current_targets={target().entity_id: target()},
            blocked={mentions[0].id: ["invalid source support"]},
        )
        assert blocked.applications[0].status == "blocked"
        assert not blocked.assignments


def test_packet_import_is_atomic_and_revision_checked(tmp_path):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        items = [proposal(store, "new_entity", [m]) for m in mentions[:2]]
        rows = [
            DecisionInput(
                proposal_id=item.id,
                proposal_revision=item.revision,
                expected_digest=item.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
            for item in items
        ]
        with pytest.raises(ReviewRefused, match="stale"):
            store.decide_many(
                [
                    rows[0],
                    rows[1].model_copy(update={"expected_digest": "tampered"}),
                ]
            )
        assert all(store.get_decision(item.id) is None for item in items)
        old = store.decide_many(rows)
        assert (
            store.decide_many(
                [row.model_copy(update={"time": LATER}) for row in rows]
            )
            == old
        )
        with pytest.raises(ReviewRefused, match="decision changed"):
            decide(store, items[0], "reject")
        changed = decide(
            store,
            items[0],
            "reject",
            expected_decision_revision=old[0].revision,
        )
        assert (
            changed.id == old[0].id and changed.revision == old[0].revision + 1
        )
        assert changed.predecessor == old[0].reference
        assert store.get_decision(items[0].id, 1) == old[0]


def test_competing_connections_cannot_overwrite_a_reviewed_outcome(tmp_path):
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        mentions = observed(store)
        item = proposal(store, "new_entity", mentions[:1])
    start = Barrier(2)

    def import_row(outcome):
        with ReviewStore(path) as store:
            start.wait(HANG_GUARD)
            try:
                return decide(store, item, outcome)
            except ReviewRefused:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(import_row, ["approve", "reject"]))
    accepted = [item for item in results if item is not None]
    assert len(accepted) == 1
    with ReviewStore(path) as store:
        assert store.get_decision(item.id) == accepted[0]


def test_two_historical_anchors_cannot_assign_one_id_to_two_current_mentions(
    tmp_path,
):
    first_line = NAMES[0] + " — first entry."
    second_line = NAMES[0] + " — revised entry."
    batch, text = reading(text="\n".join([first_line, *NAMES[1:]]))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        old = observed(store, batch, text)
        corrected = text.model_copy(
            update={"text": text.text.replace(first_line, second_line)}
        )
        revision = observe_mentions(text.artifact.sha1, batch, corrected)[0]
        store.remap(
            old[0],
            revision,
            actor="curator",
            reason="Verified corrected reading",
            time=WHEN,
        )
        both = text.model_copy(update={"text": text.text + "\n" + second_line})
        changed = batch.model_copy(deep=True)
        changed.entities.append(
            EntityRecord(
                id="other",
                label=NAMES[0],
                aliases=[NAMES[0]],
                kind="human",
                mention_lines=[5],
            )
        )
        current = observed(store, changed, both)
        assert current[0].id != current[-1].id
        assert old[0].id not in (current[0].id, current[-1].id)
        assert current[1].id == old[1].id


@pytest.mark.parametrize("other_operation", ["assign", "new_entity"])
def test_conflicting_creation_or_target_authority_is_blocked(
    tmp_path, other_operation
):
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)
        one = proposal(store, "assign", mentions[:1], binding=target())
        two = proposal(
            store,
            other_operation,
            mentions[:1],
            binding=target(2) if other_operation == "assign" else None,
        )
        decide(store, one)
        decide(store, two)
        result = project(
            store,
            mentions,
            one,
            two,
            current_targets={
                item.entity_id: item for item in (target(), target(2))
            },
        )
        assert not result.assignments and not result.new_entities
        assert all(app.status == "blocked" for app in result.applications)


def test_projection_never_changes_persisted_review_state(tmp_path):
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        mentions = observed(store)
        item = proposal(store, "new_entity", mentions[:1])
        decide(store, item)
        before = path.read_bytes()
        result = project(store, mentions, item)
        assert result.new_entities == (mentions[0].id,)
        assert path.read_bytes() == before


def test_review_sheet_survives_reopen_and_decision_names_the_exact_snapshot(
    tmp_path,
):
    path = tmp_path / "review.sqlite"
    with ReviewStore(path) as store:
        mentions = observed(store)
        item = proposal(store, "new_entity", mentions[:1])
        rows = [
            {
                "proposal_id": item.id,
                "proposal_revision": str(item.revision),
                "digest": item.digest,
                "decision_revision": "",
                "status": "needs_review",
                "blockers": "[]",
            }
        ]
        snapshot = store.put_review_sheet(rows)
        rows[0]["blockers"] = "edited after export"
    with ReviewStore(path) as store:
        saved = store.get_review_sheet(snapshot)
        assert saved[0]["blockers"] == "[]"
        saved[0]["digest"] = "tampered returned copy"
        assert store.get_review_sheet(snapshot)[0]["digest"] == item.digest
        decision = decide(store, item, review_sheet_id=snapshot)
        assert decision.review_sheet_id == snapshot
        with pytest.raises(ReviewRefused, match="reviewed sheet"):
            decide(
                store,
                item,
                "reject",
                expected_decision_revision=1,
                review_sheet_id=snapshot,
            )
        other = proposal(store, "new_entity", mentions[1:2])
        with pytest.raises(ReviewRefused, match="reviewed sheet"):
            decide(store, other, review_sheet_id=snapshot)
        assert store.get_decision(other.id) is None


def test_ambiguous_occurrences_require_explicit_one_to_one_remapping(tmp_path):
    batch, text = reading()
    batch.entities = [
        EntityRecord(id=key, label=NAMES[0], kind="human", mention_lines=[1])
        for key in ("first", "second")
    ]
    with ReviewStore(tmp_path / "review.sqlite") as store:
        old = observed(store, batch, text)
        changed = batch.model_copy(deep=True)
        changed.entities.reverse()
        current = observe_mentions(text.artifact.sha1, changed, text)
        assert all(
            item.anchor is None and item.source_scope is not None
            for item in current
        )
        mapped = store.remap(
            old[0],
            current[0],
            actor="curator",
            reason="Reviewed source occurrence",
            time=WHEN,
        )
        assert mapped.id == old[0].id
        assert mapped.correspondence["method"] == "reviewed-remapping"
        with pytest.raises(ReviewRefused, match="one-to-one"):
            store.remap(
                old[0],
                current[1],
                actor="curator",
                reason="Invalid duplicate mapping",
                time=WHEN,
            )
        assert observed(store, changed, text)[0] == mapped
