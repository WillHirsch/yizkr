"""Reviewed assignments commit on the server without rewriting graph evidence.

The fixture alone creates a historical statement. Assignment indexing and
acceptance operate through real MediaWiki pages, independent clients and real
SQLite review stores; no model or configured archive is used.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures import example_universe as u
from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    EntityRecord,
    ExtractionRunMetadata,
    QidRef,
)
from yizkr.core.identity_assignments import StaleAssignments
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import (
    CreationRegistry,
    write_existing,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live
HANG_GUARD = 120
REVIEW_TIME = "2026-01-01T12:00:00Z"
OCCURRENCES = (
    ("birth-extract-1938", "Gustav", 1),
    ("naturalisation-1952", "Gustave LOBIG", 1),
    ("family-tree-1984", "Gustav", 3),
    ("family-history-2008", "Gustav", 4),
    ("library-talk-2018", "Gustav", 7),
)


class LostCommitResponse(BaseException):
    """Interrupt only after the real server has published the commit page."""


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "info|labels|descriptions|aliases|claims",
        }
    )["entities"][qid]


def _seed(client, tmp_path):
    case = source_case("birth-extract-1938")
    source = tmp_path / "fictional-assignment-birth.txt"
    source.write_bytes(
        b"Fictional assignment authority fixture\n" + case.source_bytes
    )
    artifact = ArtifactRef.from_path(source)
    assert artifact.sha1 != case.sha1
    registered = register_artifact(
        client,
        source,
        "certificate",
        "Assignment fixture: " + case.title,
        pages=1,
    )
    assert registered.created
    operation = {
        "kind": "fictional-assignment-authority-subject",
        "source": artifact.sha1,
    }
    registry = CreationRegistry(client)
    item = client.wbi.item.new()
    item.labels.set("en", "Assignment fixture: Gustav Löbig")
    binding = registry.create(
        operation, item, summary="fictional assignment fixture"
    )
    batch = birth_batch(case.key, subject=QidRef(qid=binding.qid))
    for claim in batch.claims:
        claim.source = claim.source.model_copy(update={"artifact": artifact})
    result = BatchWriter(
        client, record_dir=tmp_path / "process-records"
    ).write_batch(batch)
    assert result.written == 1 and not result.refused
    # Read the durable receipt rather than carrying a creation-only flag.
    binding = registry.lookup(operation)
    assert binding is not None
    indexed = registry.index_binding(binding)
    # A curator changes the label and rank after indexing. Neither is a
    # binding key, and subsequent assignments must preserve both exactly.
    edited = client.wbi.item.get(binding.qid)
    edited.labels.set("en", "Curator's assignment fixture: Gustav Löbig")
    edited.descriptions.set("en", "fictional curator-edited display terms")
    properties, _ = client.vocabulary()
    (statement,) = edited.claims.get(properties["date_of_birth"])
    statement.rank = WikibaseRank.DEPRECATED
    write_existing(edited, summary="fictional fixture: preserve curator edits")
    reopened = CreationRegistry(WikiClient(client.config))
    assert reopened.binding(binding.entity_id) == indexed
    entity = _entity(client, binding.qid)
    (statement,) = entity["claims"][properties["date_of_birth"]]
    assert statement["rank"] == "deprecated"
    referenced = {
        prop
        for reference in statement["references"]
        for prop in reference["snaks"]
    }
    assert {
        properties["stated_in"],
        properties["inferred_from_extraction_run"],
    } <= referenced
    protected = {
        qid: _entity(client, qid)
        for qid in (binding.qid, registered.artifact_qid, result.run_qid)
    }
    return binding, protected


def _review(path, binding, occurrences):
    sources, proposals = [], []
    for key, label, line in occurrences:
        source = u.by_key()[key]
        artifact = ArtifactRef.from_bytes(source.text.encode())
        assert label in source.text.splitlines()[line - 1]
        batch = ClaimBatch(
            extraction_run=ExtractionRunMetadata(
                model_identifier="authored-assignment-fixture",
                pipeline_version="fixture",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            entities=[
                EntityRecord(
                    id="person",
                    label=label,
                    kind="human",
                    mention_lines=[line],
                )
            ],
        )
        reading = Transcription(
            artifact=artifact,
            text=source.text,
            language=source.lang,
            engine="authored-fictional-text",
        )
        sources.append(ReviewSource(artifact.sha1, batch, reading))
        proposals.append(
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(artifact_sha1=artifact.sha1, local_id="person"),
                ),
                target=binding,
                origin={"method": "human-proposal"},
            )
        )
    request = ReviewRequest(
        sources=tuple(sources),
        proposals=tuple(proposals),
        current_targets=(binding,),
    )
    with ReviewStore(path) as store:
        prepared = prepare_review(store, request)
        for proposal in prepared.proposals:
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="fictional curator",
                time=REVIEW_TIME,
            )
        current = prepare_review(store, request)
        mentions = set(current.projection.assignments)
        assert len(mentions) == len(occurrences)
        assert all(
            app.status == "applied" for app in current.projection.applications
        )
    return request, mentions


def _accept(config, path, request, expected_head):
    from yizkr.pipeline.assignment_stages import accept_reviewed_assignments

    with ReviewStore(path) as store:
        return accept_reviewed_assignments(
            store,
            request,
            CreationRegistry(WikiClient(config)),
            expected_head=expected_head,
        )


def _successor(title, head):
    return title.endswith(f"/assignments/{head or 'root'}/next")


def test_reviewed_assignments_are_atomic_recoverable_and_graph_preserving(
    fictional_client, tmp_path, monkeypatch
):
    client = WikiClient(fictional_client.config)
    binding, protected = _seed(client, tmp_path)
    entity_ids = set(client.list_entity_ids())
    observer = CreationRegistry(WikiClient(client.config))
    before = observer.assignment_snapshot()
    first_path = tmp_path / "initial.sqlite"
    first_request, first_mentions = _review(
        first_path, binding, OCCURRENCES[:2]
    )
    ready, release = Event(), Event()
    create_page = WikiClient.create_page_once

    def held(instance, title, text, summary):
        if _successor(title, before.head):
            ready.set()
            assert release.wait(HANG_GUARD), (
                "assignment commit was not released"
            )
        return create_page(instance, title, text, summary)

    with (
        monkeypatch.context() as patch,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        patch.setattr(WikiClient, "create_page_once", held)
        future = pool.submit(
            _accept, client.config, first_path, first_request, before.head
        )
        try:
            assert ready.wait(HANG_GUARD), "assignment commit was not reached"
            pending = observer.assignment_snapshot()
            assert pending == before
            assert first_mentions.isdisjoint(pending.assignments)
        finally:
            release.set()
        accepted = future.result(timeout=HANG_GUARD)
    assert not accepted.awaiting_creation
    assert not accepted.unassigned
    current = observer.assignment_snapshot()
    assert current == accepted.snapshot
    assert set(current.assignments) == set(before.assignments) | first_mentions
    assert len(current.history) == len(before.history) + 1
    assert all(
        current.assignments[mention].entity_id == binding.entity_id
        and current.assignments[mention].decision_references
        for mention in first_mentions
    )

    competitors = []
    for index in (2, 3):
        path = tmp_path / f"competitor-{index}.sqlite"
        request, mentions = _review(
            path, binding, OCCURRENCES[index : index + 1]
        )
        competitors.append((path, request, mentions))
    race = Barrier(2)

    def competing(instance, title, text, summary):
        if _successor(title, current.head):
            race.wait(timeout=HANG_GUARD)
        return create_page(instance, title, text, summary)

    with (
        monkeypatch.context() as patch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        patch.setattr(WikiClient, "create_page_once", competing)
        futures = [
            pool.submit(_accept, client.config, path, request, current.head)
            for path, request, _ in competitors
        ]
        winners, stale = [], []
        for index, future in enumerate(futures):
            try:
                winners.append((index, future.result(timeout=HANG_GUARD)))
            except StaleAssignments as exc:
                stale.append((index, exc))
    assert len(winners) == len(stale) == 1
    winner, result = winners[0]
    loser, _ = stale[0]
    assert not result.awaiting_creation and not result.unassigned
    settled = observer.assignment_snapshot()
    assert settled == result.snapshot
    assert (
        set(settled.assignments)
        == set(current.assignments) | competitors[winner][2]
    )
    assert competitors[loser][2].isdisjoint(settled.assignments)
    assert all(
        settled.assignments[mention] == assignment
        for mention, assignment in current.assignments.items()
    )

    retry_path = tmp_path / "lost-response.sqlite"
    retry_request, retry_mentions = _review(
        retry_path, binding, OCCURRENCES[4:]
    )
    committed = []

    def lose_response(instance, title, text, summary):
        page = create_page(instance, title, text, summary)
        if _successor(title, settled.head):
            committed.append(page)
            raise LostCommitResponse()
        return page

    with monkeypatch.context() as patch:
        patch.setattr(WikiClient, "create_page_once", lose_response)
        with pytest.raises(LostCommitResponse):
            _accept(client.config, retry_path, retry_request, settled.head)
    assert len(committed) == 1
    published = observer.assignment_snapshot()
    assert (
        set(published.assignments) == set(settled.assignments) | retry_mentions
    )
    assert all(
        published.assignments[mention] == assignment
        for mention, assignment in settled.assignments.items()
    )
    recovered = _accept(client.config, retry_path, retry_request, settled.head)
    assert recovered.snapshot == published
    assert not recovered.awaiting_creation and not recovered.unassigned
    assert observer.assignment_snapshot() == published
    assert set(client.list_entity_ids()) == entity_ids
    assert {qid: _entity(client, qid) for qid in protected} == protected
