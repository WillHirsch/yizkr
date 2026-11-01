"""Interrupted and competing writes against the disposable fictional wiki.

Failures happen after a real server creation, before its durable receipt.
Fresh clients must recover through server evidence after curator term edits;
the fixture never supplies the captured QID to the resumed writer.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Barrier, Event

import pytest
from wikibaseintegrator.entities.item import ItemEntity
from wikibaseintegrator.wbi_exceptions import MWApiError

from tests.fixtures.identity import mapped
from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import ArtifactRef
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import (
    PREFIX,
    AmbiguousCreation,
    CreationRegistry,
    EntityBinding,
    write_existing,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live
HANG_GUARD = 120


class InterruptedWrite(BaseException):
    """Stop after dispatch, before recovery or receipt publication."""


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "info|labels|descriptions|claims",
        }
    )["entities"][qid]


def _rename(client, qid):
    item = client.wbi.item.get(qid)
    item.labels.set("en", f"Curator's fictional recovery item {qid}")
    item.descriptions.set(
        "en", "curator edited both display terms after interruption"
    )
    write_existing(
        item, summary="fictional fixture: curator edits display terms"
    )
    return _entity(client, qid)


def _fixture(tmp_path, suffix):
    case = source_case("birth-extract-1938")
    path = tmp_path / f"fictional-recovery-{suffix}.txt"
    path.write_bytes(
        case.source_bytes + f"\nFictional recovery case: {suffix}\n".encode()
    )
    artifact = ArtifactRef(sha1=hashlib.sha1(path.read_bytes()).hexdigest())
    batch = birth_batch(case.key)
    batch = batch.model_copy(
        update={
            "claims": [
                claim.model_copy(
                    update={
                        "source": claim.source.model_copy(
                            update={"artifact": artifact}
                        )
                    }
                )
                for claim in batch.claims
            ],
            "entities": [
                record.model_copy(
                    update={
                        "label": f"Recovery fixture {suffix}: {record.label}"
                    }
                )
                for record in batch.entities
            ],
            "extraction_run": batch.extraction_run.model_copy(
                update={
                    "model_identifier": f"authored-recovery-fixture:{suffix}"
                }
            ),
        }
    )
    return path, batch


def _writer_recovery_case(fictional_client, tmp_path, monkeypatch, failure):
    client = WikiClient(fictional_client.config)
    path, batch = _fixture(tmp_path, failure)
    source = register_artifact(
        client,
        path,
        artifact_type="certificate",
        title=f"Fictional recovery {failure}",
    )
    identity = mapped(batch)
    before = set(client.list_entity_ids())
    created = []
    write = ItemEntity.write
    save_page = client.create_page_once

    def interrupt(item, *args, **kwargs):
        new = item.id is None
        written = write(item, *args, **kwargs)
        if new and kwargs.get("summary", "").endswith(
            "ingest: entity proposed by extraction"
        ):
            created.append((written.id, written.lastrevid))
            if failure == "lost-response":
                raise InterruptedWrite()
        return written

    def fail_receipt(title, text, summary):
        record = json.loads(text)
        if (
            record.get("kind") == "receipt"
            and created
            and record.get("qid") == created[0][0]
        ):
            raise InterruptedWrite()
        return save_page(title, text, summary)

    with monkeypatch.context() as patch:
        patch.setattr(ItemEntity, "write", interrupt)
        if failure == "lost-receipt":
            patch.setattr(client, "create_page_once", fail_receipt)
        with pytest.raises(InterruptedWrite):
            BatchWriter(client).write_batch(
                batch, identity=identity, one_map=False
            )
    ((qid, initial_revision),) = created
    renamed = _rename(client, qid)
    fresh = WikiClient(client.config)
    result = BatchWriter(fresh).write_batch(
        batch, identity=identity, one_map=False
    )
    assert result.created_items == {"gustav": qid}
    assert result.minted == []
    assert result.written == 1 and not result.refused
    observed = _entity(fresh, qid)
    assert observed["labels"] == renamed["labels"]
    assert observed["descriptions"] == renamed["descriptions"]
    properties, _ = fresh.vocabulary()
    (born,) = observed["claims"][properties["date_of_birth"]]
    references = [reference["snaks"] for reference in born["references"]]
    assert len(references) == 2
    assert [
        snak["datavalue"]["value"]["id"]
        for block in references
        for snak in block.get(properties["stated_in"], [])
    ] == [source.artifact_qid]
    assert [
        snak["datavalue"]["value"]["id"]
        for block in references
        for snak in block.get(properties["inferred_from_extraction_run"], [])
    ] == [result.run_qid]
    (cluster,) = identity.clusters()
    binding = CreationRegistry(fresh).lookup(
        {"kind": "cluster", "map": identity.hash, "cluster": cluster}
    )
    assert binding is not None
    assert (binding.qid, binding.revision) == (qid, initial_revision)
    assert binding.entity_id and binding.deployment_id and binding.generation
    receipt = fresh.immutable_page(
        f"{PREFIX}{binding.generation}/{binding.intent_key}/receipt"
    )
    assert receipt is not None
    assert EntityBinding.model_validate_json(receipt.text) == binding
    assert set(fresh.list_entity_ids()) - before == {qid, result.run_qid}
    again = BatchWriter(WikiClient(client.config)).write_batch(
        batch, identity=identity, one_map=False
    )
    assert again.minted == [] and again.written == again.referenced == 0
    assert again.skipped == 1 and not again.refused
    assert _entity(fresh, qid) == observed
    assert set(fresh.list_entity_ids()) - before == {qid, result.run_qid}
    # Both copies read the same revision. A competing change to this same
    # field must be protected; nonconflicting stale edits may auto-merge.
    curator = WikiClient(client.config).wbi.item.get(qid)
    stale = WikiClient(client.config).wbi.item.get(qid)
    curator.descriptions.set("en", "fictional curator's newer description")
    write_existing(
        curator, summary="fictional fixture: competing curator edit"
    )
    preserved = _entity(fresh, qid)
    stale.descriptions.set(
        "en", "fictional stale writer's different description"
    )
    with pytest.raises(MWApiError):
        write_existing(
            stale, summary="fictional fixture: stale conflicting edit"
        )
    assert _entity(fresh, qid) == preserved


def test_interrupted_writer_recovers_after_curator_term_changes(
    fictional_client, tmp_path, monkeypatch
):
    for failure in ("lost-response", "lost-receipt"):
        _writer_recovery_case(fictional_client, tmp_path, monkeypatch, failure)


def test_interrupted_source_registration_recovers_without_orphan(
    fictional_client, tmp_path, monkeypatch
):
    client = WikiClient(fictional_client.config)
    path, _ = _fixture(tmp_path, "registration")
    before = set(client.list_entity_ids())
    created = []
    write = ItemEntity.write

    def interrupt(item, *args, **kwargs):
        new = item.id is None
        written = write(item, *args, **kwargs)
        if new:
            created.append(written.id)
            raise InterruptedWrite()
        return written

    def register(current):
        return register_artifact(
            current,
            path,
            artifact_type="certificate",
            title="Fictional interrupted registration",
        )

    with monkeypatch.context() as patch:
        patch.setattr(ItemEntity, "write", interrupt)
        with pytest.raises(InterruptedWrite):
            register(client)
    (qid,) = created
    properties, _ = client.vocabulary()
    assert properties["media_sha1"] not in _entity(client, qid).get(
        "claims", {}
    )
    renamed = _rename(client, qid)
    fresh = WikiClient(client.config)
    result = register(fresh)
    assert result.artifact_qid == qid
    observed = _entity(fresh, qid)
    assert observed["labels"] == renamed["labels"]
    assert observed["descriptions"] == renamed["descriptions"]
    (sha1,) = observed["claims"][properties["media_sha1"]]
    assert (
        sha1["mainsnak"]["datavalue"]["value"]
        == hashlib.sha1(path.read_bytes()).hexdigest()
    )
    assert set(fresh.list_entity_ids()) - before == {qid}
    again = register(WikiClient(client.config))
    assert again.artifact_qid == qid and not again.created
    assert _entity(fresh, qid) == observed


def test_concurrent_creation_reserves_one_dispatch(
    fictional_client, monkeypatch
):
    first = WikiClient(fictional_client.config)
    second = WikiClient(fictional_client.config)
    operation = {
        "kind": "fictional-concurrent-creation",
        "fixture": "eugen-master-hatter",
    }
    before = set(first.list_entity_ids())
    dispatched, release = Event(), Event()
    reservation_race = Barrier(2)
    write = ItemEntity.write
    reserve = WikiClient.create_page_once

    def competing(client, title, text, summary):
        if json.loads(text).get("kind") == "dispatch":
            reservation_race.wait(timeout=HANG_GUARD)
        return reserve(client, title, text, summary)

    def held(item, *args, **kwargs):
        if item.id is None and kwargs.get("summary", "").endswith(
            "fictional concurrent creation"
        ):
            dispatched.set()
            assert release.wait(HANG_GUARD), (
                "test did not release the reserved creator"
            )
        return write(item, *args, **kwargs)

    def create(client):
        item = client.wbi.item.new()
        item.labels.set("en", "Concurrent recovery fixture: Eugen Löbig")
        return CreationRegistry(client).create(
            operation, item, summary="fictional concurrent creation"
        )

    with (
        monkeypatch.context() as patch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        patch.setattr(ItemEntity, "write", held)
        patch.setattr(WikiClient, "create_page_once", competing)
        futures = {pool.submit(create, first), pool.submit(create, second)}
        try:
            assert dispatched.wait(HANG_GUARD), (
                "neither writer won the dispatch reservation"
            )
            completed, pending = wait(
                futures, timeout=HANG_GUARD, return_when=FIRST_COMPLETED
            )
            assert len(completed) == len(pending) == 1
            loser = completed.pop()
            with pytest.raises(AmbiguousCreation):
                loser.result()
            assert set(second.list_entity_ids()) == before
        finally:
            release.set()
        original = pending.pop().result(timeout=HANG_GUARD)
    recovered = create(WikiClient(first.config))
    assert recovered.qid == original.qid
    assert recovered.entity_id == original.entity_id
    assert recovered.intent_key == original.intent_key
    assert recovered.revision == original.revision
    assert not recovered.created
    assert set(first.list_entity_ids()) - before == {original.qid}
