"""A real Wikibase/WDQS capture stays read-only and replays without clients."""

from __future__ import annotations

import copy

import pytest
from wikibaseintegrator.datatypes import Item

from tests.fictional_integration.test_write_query import _synchronize
from yizkr.backup import REVISIONS_QUERY
from yizkr.claims import EntityRecord
from yizkr.core import sparql
from yizkr.core.instance import watermark
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import PREFIX, CreationRegistry, write_existing
from yizkr.pipeline import graph_projection as projection
from yizkr.pipeline.reconcile import PlaceContainment, block_candidates

pytestmark = pytest.mark.live


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "info|labels|descriptions|aliases|claims",
        }
    )["entities"][qid]


def _seed(registry, label, alias):
    client = registry.client
    properties, items = client.vocabulary()
    item = client.wbi.item.new()
    item.labels.set("en", label)
    item.descriptions.set("en", "fictional graph projection fixture")
    item.aliases.set("en", [alias])
    item.claims.add(
        Item(value=items["human"], prop_nr=properties["instance_of"])
    )
    return registry.create(
        {"kind": "fictional-graph-projection", "person": label},
        item,
        summary="fictional graph projection fixture",
    )


def test_recorded_graph_projection_replays_and_refuses_stale_capture(
    fictional_client, tmp_path, monkeypatch
):
    client = WikiClient(fictional_client.config)
    registry = CreationRegistry(client)
    first = _seed(registry, "Gustav Löbig", "Gustave Lobig")
    second = _seed(registry, "Hildegard Löbig", "Hilde")
    binding = registry.index_binding(first)
    renamed = client.wbi.item.get(first.qid)
    renamed.labels.set("en", "Gustave Lobig")
    renamed.aliases.set("en", ["Gustav Löbig", "Gustl"])
    write_existing(renamed, summary="fictional curator changes display terms")

    _synchronize(client)
    protected = {qid: _entity(client, qid) for qid in (first.qid, second.qid)}
    assignments = registry.assignment_snapshot()
    recovery_pages = client.page_titles(PREFIX)
    before = watermark(client)
    recorded = projection.capture_graph_projection(registry)
    assert watermark(client) == before
    assert recorded.capture.watermark_before == before
    assert recorded.capture.watermark_after == before
    assert not recorded.capture.mutation_authority
    assert recorded.entity_revisions == client.entity_revisions(
        client.list_entity_ids()
    )
    assert any(qid.startswith("P") for qid in recorded.entity_revisions)
    assert recorded.bindings[first.qid] == binding
    assert second.qid not in recorded.bindings
    assert second.qid in recorded.unbound_qids
    assert (
        recorded.assignments == assignments == registry.assignment_snapshot()
    )
    assert client.page_titles(PREFIX) == recovery_pages
    assert {qid: _entity(client, qid) for qid in protected} == protected

    path = tmp_path / "graph-projection.json"
    projection.save_graph_projection(recorded, path)

    def offline(*args, **kwargs):
        pytest.fail("recorded matching attempted a live graph read")

    with monkeypatch.context() as patch:
        patch.setattr(WikiClient, "api_get", offline)
        patch.setattr(sparql, "runner", offline)
        patch.setattr(sparql, "select", offline)
        loaded = projection.load_graph_projection(path)
        assert loaded.digest == recorded.digest
        candidates = block_candidates(
            [EntityRecord(id="gustav", label="Gustav Löbig", kind="human")],
            list(loaded.roster),
            containment=PlaceContainment(loaded.containment),
        )
        candidate = next(
            row for row in candidates["gustav"] if row.qid == first.qid
        )
        assert candidate.label == "Gustave Lobig"
        assert {"Gustav Löbig", "Gustl"} <= set(candidate.aliases)
        assert "Gustave Lobig" in candidate.facts()
        assert "Gustav Löbig" in candidate.facts()

    actual_runner = sparql.runner(client.config)
    stale_queries = []

    def stale_index(query):
        rows = actual_runner(query)
        if query != REVISIONS_QUERY:
            return rows
        stale_queries.append(query)
        changed = copy.deepcopy(rows)
        (row,) = [
            row
            for row in changed
            if row["entity"]["value"]
            == client.config.concept_uri + "/entity/" + first.qid
        ]
        assert (
            int(row["version"]["value"])
            == recorded.entity_revisions[first.qid]
        )
        row["version"]["value"] = str(int(row["version"]["value"]) - 1)
        return changed

    with monkeypatch.context() as patch:
        patch.setattr(sparql, "runner", lambda config: stale_index)
        with pytest.raises(projection.GraphProjectionRefused):
            projection.capture_graph_projection(CreationRegistry(client))
    assert stale_queries
    assert watermark(client) == before

    curator = WikiClient(client.config)
    changed_during_capture = []

    def curator_edit(query):
        if query != REVISIONS_QUERY and not changed_during_capture:
            item = curator.wbi.item.get(first.qid)
            item.descriptions.set(
                "en", "fictional curator edit during graph capture"
            )
            write_existing(item, summary="fictional concurrent curator edit")
            changed_during_capture.append(item.lastrevid)
        return actual_runner(query)

    with monkeypatch.context() as patch:
        patch.setattr(sparql, "runner", lambda config: curator_edit)
        with pytest.raises(projection.GraphProjectionRefused):
            projection.capture_graph_projection(CreationRegistry(client))
    assert changed_during_capture
    assert (
        client.entity_revisions([first.qid])[first.qid]
        > recorded.entity_revisions[first.qid]
    )
    assert registry.assignment_snapshot() == assignments
    assert client.page_titles(PREFIX) == recovery_pages
