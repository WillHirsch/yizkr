"""A real Wikibase/WDQS capture stays read-only and replays without clients."""

from __future__ import annotations

import copy

import pytest
from wikibaseintegrator.datatypes import Item, MonolingualText, String, Time
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fictional_integration.test_write_query import _synchronize
from tests.fixtures import example_universe as u
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


def _birth_fixture(registry, principal, witness):
    """Authored sources and curator overlays, independent of inference."""
    client = registry.client
    properties, items = client.vocabulary()
    sources = {}
    for source in (
        u.by_key()["birth-extract-1938"],
        u.by_key()["naturalisation-1952"],
        u.by_key()["family-tree-1984"],
        u.BIRTH_UNCERTAINTY_SCENARIO,
    ):
        item = client.wbi.item.new()
        item.labels.set("en", source.title)
        item.descriptions.set("en", "fictional graph projection source")
        item.claims.add(
            Item(value=items["artifact"], prop_nr=properties["instance_of"])
        )
        sources[source.key] = registry.create(
            {"kind": "fictional-graph-projection-source", "key": source.key},
            item,
            summary="fictional graph projection source",
        ).qid

    def reference(key, excerpt):
        source = (
            u.BIRTH_UNCERTAINTY_SCENARIO
            if key == (u.BIRTH_UNCERTAINTY_SCENARIO.key)
            else u.by_key()[key]
        )
        assert excerpt in source.text
        support = Reference()
        support.add(Item(prop_nr=properties["stated_in"], value=sources[key]))
        support.add(String(prop_nr=properties["page_number"], value="1"))
        support.add(
            MonolingualText(
                prop_nr=properties["transcription_excerpt"],
                text=excerpt,
                language=source.lang,
            )
        )
        return support

    person = client.wbi.item.get(principal)
    for key, value, precision, rank in (
        (
            "birth-extract-1938",
            "+1919-01-07T00:00:00Z",
            11,
            WikibaseRank.NORMAL,
        ),
        (
            "naturalisation-1952",
            "+1919-07-01T00:00:00Z",
            11,
            WikibaseRank.PREFERRED,
        ),
        (
            "family-tree-1984",
            "+1919-01-01T00:00:00Z",
            9,
            WikibaseRank.DEPRECATED,
        ),
    ):
        assertion = next(
            row
            for row in u.about("gustav-loebig", "date_of_birth")
            if row.source == key
        )
        birth = Time(
            prop_nr=properties["date_of_birth"],
            time=value,
            precision=precision,
            calendarmodel="http://www.wikidata.org/entity/Q1985727",
        )
        birth.rank = rank
        birth.references.add(reference(key, assertion.excerpt))
        person.claims.add(birth)
    write_existing(person, summary="fictional plural birth readings")

    source = u.BIRTH_UNCERTAINTY_SCENARIO
    support = reference(source.key, source.text)
    event = client.wbi.item.new()
    event.labels.set("en", "Fictional birth uncertainty scenario")
    event.claims.add(
        Item(value=items["birth"], prop_nr=properties["instance_of"])
    )
    for qid, role in ((principal, "principal"), (witness, "witness")):
        participant = Item(prop_nr=properties["participant"], value=qid)
        participant.qualifiers.add(
            Item(prop_nr=properties["object_has_role"], value=items[role])
        )
        participant.references.add(support)
        event.claims.add(participant)
    when = Time(
        prop_nr=properties["point_in_time"],
        time="+1919-01-01T00:00:00Z",
        precision=9,
        calendarmodel="http://www.wikidata.org/entity/Q1985727",
    )
    for key, date in (
        ("earliest_date", "+1919-01-07T00:00:00Z"),
        ("latest_date", "+1919-07-01T00:00:00Z"),
    ):
        when.qualifiers.add(
            Time(
                prop_nr=properties[key],
                time=date,
                precision=11,
                calendarmodel="http://www.wikidata.org/entity/Q1985727",
            )
        )
    when.qualifiers.add(
        Item(
            prop_nr=properties["sourcing_circumstances"],
            value=items["circa"],
        )
    )
    when.references.add(support)
    event.claims.add(when)
    event_qid = registry.create(
        {"kind": "fictional-graph-projection-birth-event"},
        event,
        summary="fictional birth event uncertainty and roles",
    ).qid
    return sources, event_qid


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
    sources, birth_event = _birth_fixture(registry, first.qid, second.qid)

    _synchronize(client)
    protected = {
        qid: _entity(client, qid)
        for qid in (first.qid, second.qid, birth_event, *sources.values())
    }
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
        assert candidate.births == next(
            row.births for row in recorded.roster if row.qid == first.qid
        )
        assert len(candidate.births) == 4
        assert len({row.statement for row in candidate.births}) == 4
        direct = {row.rank: row for row in candidate.births if not row.event}
        assert {
            rank: (row.value.value.lstrip("+"), row.value.precision)
            for rank, row in direct.items()
        } == {
            "normal": ("1919-01-07T00:00:00Z", 11),
            "preferred": ("1919-07-01T00:00:00Z", 11),
            "deprecated": ("1919-01-01T00:00:00Z", 9),
        }
        (event,) = [row for row in candidate.births if row.event]
        assert event.event == birth_event and event.rank == "normal"
        assert event.value.value.lstrip("+") == "1919-01-01T00:00:00Z"
        assert event.value.precision == 9
        assert [
            (bound.value.lstrip("+"), bound.precision)
            for bound in event.earliest
        ] == [("1919-01-07T00:00:00Z", 11)]
        assert [
            (bound.value.lstrip("+"), bound.precision)
            for bound in event.latest
        ] == [("1919-07-01T00:00:00Z", 11)]
        assert "circa" in event.circumstances
        assert event.participation.rank == "normal"
        assert event.participation.statement != event.statement
        assert {
            source.source
            for reference in event.participation.references
            for source in reference.sources
        } == {sources[u.BIRTH_UNCERTAINTY_SCENARIO.key]}
        for reading, source_key in (
            (direct["normal"], "birth-extract-1938"),
            (direct["preferred"], "naturalisation-1952"),
            (direct["deprecated"], "family-tree-1984"),
            (event, u.BIRTH_UNCERTAINTY_SCENARIO.key),
        ):
            assert reading.person == first.qid
            (support,) = reading.references
            assert {row.source for row in support.sources} == {
                sources[source_key]
            }
            assert {row.artifact for row in support.sources} == {
                sources[source_key]
            }
            assert {
                snak.value.value
                for snak in support.snaks
                if snak.property == "page_number"
            } == {"1"}
            excerpts = [
                (snak.value.value, snak.value.language)
                for snak in support.snaks
                if snak.property == "transcription_excerpt"
            ]
            if reading.event:
                expected = (u.BIRTH_UNCERTAINTY_SCENARIO.text, "en")
            else:
                expected = (
                    next(
                        row.excerpt
                        for row in u.about("gustav-loebig", "date_of_birth")
                        if row.source == source_key
                    ),
                    u.by_key()[source_key].lang,
                )
            assert excerpts == [expected]
            assert sources[source_key] in candidate.facts()
        witness = next(row for row in loaded.roster if row.qid == second.qid)
        assert not witness.births
        assert "1919-01-07" in candidate.facts()
        assert "1919-07-01" in candidate.facts()
        assert "circa" in candidate.facts()
        assert "deprecated" in candidate.facts()
        assert "not active identity evidence" in candidate.facts()

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
