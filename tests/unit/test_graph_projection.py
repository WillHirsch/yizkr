"""Recorded reconciliation inputs require complete, unchanged graph reads."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from tests.unit.test_pipeline import FakeWiki
from tests.unit.test_reconcile import fictional_births
from yizkr.backup import REVISIONS_QUERY
from yizkr.claims import EntityRecord
from yizkr.core.wiki import RecoveryEvidenceError, WikiClient, WikiPage
from yizkr.core.write_recovery import (
    GENERATION_PAGE,
    CreationRefused,
    CreationRegistry,
    write_existing,
)
from yizkr.model.schema import load_manifest
from yizkr.pipeline import graph_projection as projection
from yizkr.pipeline.reconcile import PlaceContainment, block_candidates


class ProjectionWiki(FakeWiki):
    """Existing fake writes with authoritative inventory and API info reads."""

    entity_revisions = WikiClient.entity_revisions

    def vocabulary(self):
        manifest = load_manifest()
        return (
            {key: f"P{n}" for n, key in enumerate(manifest.properties, 1)},
            {key: f"Q{n}" for n, key in enumerate(manifest.items, 1000)},
        )

    def forget_vocabulary(self):
        pass

    def api_get(self, params):
        if params.get("action") == "wbgetentities":
            assert params["props"] == "info"
            return {
                "entities": {
                    qid: {
                        "id": qid,
                        "lastrevid": self.wbi.item.revisions.get(qid, 1),
                    }
                    for qid in params["ids"].split("|")
                }
            }
        if params.get("list") == "allrevisions":
            return {
                "query": {
                    "allrevisions": [
                        {
                            "revisions": [
                                {
                                    "revid": self._recovery_revision
                                    + self.wbi.item.writes
                                }
                            ]
                        }
                    ]
                }
            }
        if params.get("list") == "logevents":
            return {"query": {"logevents": []}}
        return super().api_get(params)


def _person(registry, label):
    item = registry.client.wbi.item.new()
    item.labels.set("en", label)
    return registry.create(
        {"kind": "fictional-projection-fixture", "label": label},
        item,
        summary="fictional projection fixture",
    )


def _uri(wiki, qid):
    return {"type": "uri", "value": wiki.config.concept_uri + "/entity/" + qid}


def _literal(value):
    return {"type": "literal", "value": str(value)}


class QueryService:
    def __init__(self, wiki, people):
        self.wiki = wiki
        self.revisions = wiki.entity_revisions(wiki.list_entity_ids())
        self.people = people
        self.index_reads = 0

    def __call__(self, query):
        if query == REVISIONS_QUERY:
            self.index_reads += 1
            return [
                {"entity": _uri(self.wiki, qid), "version": _literal(revision)}
                for qid, revision in self.revisions.items()
            ]
        if "GROUP BY ?item ?itemLabel ?kind ?kindLabel" in query:
            _, items = self.wiki.vocabulary()
            return [
                {
                    "item": _uri(self.wiki, person.qid),
                    "itemLabel": _literal(
                        self.wiki.wbi.item.get(person.qid).labels.get("en")
                    ),
                    "kind": _uri(self.wiki, items["human"]),
                    "aliases": _literal("Gustav Löbig")
                    if index == 0
                    else _literal("Hilde"),
                }
                for index, person in enumerate(self.people)
            ]
        return []


@pytest.fixture
def graph(monkeypatch):
    wiki = ProjectionWiki(detached=True)
    registry = CreationRegistry(wiki)
    first = _person(registry, "Gustav Löbig")
    second = _person(registry, "Hildegard Löbig")
    registered = registry.index_binding(first)
    item = wiki.wbi.item.get(first.qid)
    item.labels.set("en", "Gustave Lobig")
    write_existing(item, summary="fictional curator rename")
    service = QueryService(wiki, (first, second))
    monkeypatch.setattr(projection.sparql, "runner", lambda config: service)
    return wiki, registry, registered, second, service


def test_capture_preserves_authority_and_replays_saved_candidate_facts(
    graph, tmp_path, monkeypatch
):
    wiki, registry, binding, unbound, service = graph
    pages = dict(wiki._recovery_pages)
    writes = wiki.wbi.item.writes
    assignments = registry.assignment_snapshot()
    recorded = projection.capture_graph_projection(registry)
    assert recorded.entity_revisions == wiki.entity_revisions(
        wiki.list_entity_ids()
    )
    assert any(qid.startswith("P") for qid in recorded.entity_revisions)
    assert recorded.bindings == {binding.binding.qid: binding}
    assert unbound.qid in recorded.unbound_qids
    assert recorded.assignments == assignments
    assert service.index_reads == 2
    assert (
        recorded.capture.watermark_before == recorded.capture.watermark_after
    )
    assert not recorded.capture.mutation_authority
    assert wiki._recovery_pages == pages
    assert wiki.wbi.item.writes == writes

    path = tmp_path / "graph-projection.json"
    projection.save_graph_projection(recorded, path)

    def offline(*args, **kwargs):
        pytest.fail("saved projection replay attempted a live graph read")

    monkeypatch.setattr(wiki, "api_get", offline)
    monkeypatch.setattr(projection.sparql, "runner", offline)
    loaded = projection.load_graph_projection(path)
    assert loaded == recorded
    assert loaded.digest == recorded.digest
    candidates = block_candidates(
        [EntityRecord(id="gustav", label="Gustav Löbig", kind="human")],
        list(loaded.roster),
        containment=PlaceContainment(loaded.containment),
    )
    (candidate,) = candidates["gustav"]
    assert candidate.qid == binding.binding.qid
    assert candidate.label == "Gustave Lobig"
    assert "Gustav Löbig" in candidate.aliases
    assert "Gustave Lobig" in candidate.facts()
    with pytest.raises(ValidationError):
        loaded.selector_version = "another-version"


@pytest.mark.parametrize("damage", ["missing", "stale", "extra", "ahead"])
@pytest.mark.parametrize("entity_kind", ["P", "Q"])
def test_capture_refuses_incomplete_or_different_query_index(
    graph, damage, entity_kind
):
    _, registry, _, _, service = graph
    qid = next(key for key in service.revisions if key.startswith(entity_kind))
    if damage == "missing":
        del service.revisions[qid]
    elif damage == "extra":
        service.revisions[entity_kind + "99999"] = 1
    elif damage == "stale":
        # A valid positive version that is older than the authoritative API.
        service.revisions[qid] = max(1, service.revisions[qid] - 1)
        registry.client.entity_revisions = lambda ids: {
            key: (revision + 1 if key == qid else revision)
            for key, revision in service.revisions.items()
        }
    else:
        service.revisions[qid] += 1
    with pytest.raises(projection.GraphProjectionRefused):
        projection.capture_graph_projection(registry)


@pytest.mark.parametrize(
    "change", ["inventory", "revision", "log", "generation", "index"]
)
def test_capture_refuses_changes_between_observations(
    graph, monkeypatch, change
):
    wiki, registry, binding, _, service = graph
    run = service.__call__
    changed = False

    def concurrent(query):
        nonlocal changed
        rows = run(query)
        if query != REVISIONS_QUERY and not changed:
            changed = True
            if change == "inventory":
                _person(registry, "Sebastian Löbig")
            elif change == "revision":
                item = wiki.wbi.item.get(binding.binding.qid)
                item.labels.set("en", "Gustl")
                write_existing(
                    item, summary="fictional concurrent curator edit"
                )
            elif change == "log":
                original = wiki.api_get

                def with_log(params):
                    if params.get("list") == "logevents":
                        return {"query": {"logevents": [{"logid": 1}]}}
                    return original(params)

                monkeypatch.setattr(wiki, "api_get", with_log)
            elif change == "generation":
                page = wiki._recovery_pages[GENERATION_PAGE]
                data = json.loads(page.text)
                data["generation"] = "3" * 32
                wiki._recovery_pages[GENERATION_PAGE] = WikiPage(
                    page.title, json.dumps(data), page.revision + 1
                )
            else:
                service.revisions[binding.binding.qid] += 1
        return rows

    monkeypatch.setattr(projection.sparql, "runner", lambda config: concurrent)
    with pytest.raises((projection.GraphProjectionRefused, CreationRefused)):
        projection.capture_graph_projection(registry)
    assert changed


def test_binding_catalog_verifies_receipts_without_adopting_unindexed_items(
    graph,
):
    wiki, registry, binding, unbound, _ = graph
    before = dict(wiki._recovery_pages)
    assert registry.indexed_bindings() == {binding.binding.qid: binding}
    assert registry.binding(unbound.entity_id) is None
    assert wiki._recovery_pages == before
    wiki._recovery_edited.add(binding.receipt_locator)
    with pytest.raises((CreationRefused, RecoveryEvidenceError)):
        registry.indexed_bindings()
    assert wiki._recovery_pages == before


def test_binding_catalog_accepts_canonical_project_namespace_titles(
    graph, monkeypatch
):
    wiki, registry, binding, _, _ = graph
    pages = dict(wiki._recovery_pages)
    original = wiki.page_titles

    def canonical_titles(prefix):
        return [
            "Fictional integration:" + title.removeprefix("Project:")
            for title in original(prefix)
        ]

    monkeypatch.setattr(wiki, "page_titles", canonical_titles)
    assert registry.indexed_bindings() == {binding.binding.qid: binding}
    assert wiki._recovery_pages == pages


def test_capture_keeps_accepted_assignments_without_publishing(
    graph, tmp_path
):
    from tests.unit.test_identity_assignments import delta, reviewed
    from tests.unit.test_identity_review import observed
    from yizkr.pipeline.identity_review import ReviewStore

    wiki, registry, registered, _, _ = graph
    binding = registered.binding
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = observed(store)[:1]
        decisions = reviewed(store, mentions, binding)
        accepted = registry.append_assignments(
            delta(mentions, decisions, [(mentions[0], binding)]),
            expected_head=None,
        )
    before = dict(wiki._recovery_pages)
    recorded = projection.capture_graph_projection(registry)
    assert recorded.assignments == accepted
    assert (
        recorded.assignments.assignments[mentions[0].id].entity_id
        == binding.entity_id
    )
    assert recorded.assignments.history
    assert wiki._recovery_pages == before


def test_recorded_containment_preserves_source_and_rank_filter(
    graph, monkeypatch
):
    wiki, registry, _, _, service = graph
    street = _person(registry, "Brunnenring 35")
    town = _person(registry, "Flörburg")
    district = _person(registry, "Kreis Peiltal")
    other = _person(registry, "Kreis Mockburg")
    service.revisions = wiki.entity_revisions(wiki.list_entity_ids())
    run = service.__call__

    def edge(item, target, *, stated=True, deprecated=False):
        return {
            "item": _uri(wiki, item.qid),
            "target": _uri(wiki, target.qid),
            "stated": _literal("true" if stated else "false"),
            "rank": {
                "type": "uri",
                "value": "http://wikiba.se/ontology#"
                + ("DeprecatedRank" if deprecated else "NormalRank"),
            },
        }

    def containment(query):
        if "SELECT ?item ?itemLabel ?link ?target" in query:
            return [
                edge(street, town),
                edge(town, district),
                edge(street, other, stated=False),
                edge(town, other, deprecated=True),
            ]
        return run(query)

    monkeypatch.setattr(
        projection.sparql, "runner", lambda config: containment
    )
    captured = projection.capture_graph_projection(registry)
    closure = PlaceContainment(captured.containment)
    assert closure.containers(street.qid) == {town.qid, district.qid}
    assert closure.containers(town.qid) == {district.qid}
    assert not closure.contains(other.qid, street.qid)


@pytest.mark.parametrize(
    "entities",
    [
        {},
        {"Q100": {"id": "Q100", "lastrevid": True}},
        {"Q100": {"id": "Q100", "lastrevid": "2"}},
        {"Q100": {"id": "Q100", "lastrevid": 0}},
        {"Q100": {"id": "Q100", "lastrevid": 2, "missing": ""}},
        {"Q100": {"id": "Q101", "lastrevid": 2}},
        {"Q100": {"id": "Q100", "lastrevid": 2, "redirects": []}},
        {"Q100": []},
        {"Q100": {"id": "Q100", "lastrevid": 2}, "Q101": {}},
    ],
)
def test_authoritative_revision_read_refuses_malformed_api_data(
    entities, monkeypatch
):
    wiki = ProjectionWiki()
    monkeypatch.setattr(wiki, "api_get", lambda params: {"entities": entities})
    with pytest.raises(RecoveryEvidenceError):
        wiki.entity_revisions(["Q100"])


@pytest.mark.parametrize(
    "damage", ["contents", "digest", "envelope-version", "selector-version"]
)
def test_saved_projection_refuses_tampering_and_unsupported_versions(
    graph, tmp_path, damage
):
    _, registry, _, _, _ = graph
    recorded = projection.capture_graph_projection(registry)
    path = tmp_path / "graph-projection.json"
    projection.save_graph_projection(recorded, path)
    saved = json.loads(path.read_text())
    if damage == "contents":
        saved["projection"]["roster"][0]["label"] = "Gustl"
    elif damage == "digest":
        saved["digest"] = "0" * 64
    elif damage == "envelope-version":
        saved["format"] = 2
    else:
        saved["projection"]["selector_version"] = "unsupported-roster"
    path.write_text(json.dumps(saved))
    with pytest.raises(projection.GraphProjectionRefused):
        projection.load_graph_projection(path)


def test_capture_requires_existing_generation_without_initializing(graph):
    wiki, _, _, _, _ = graph
    wiki._recovery_pages.clear()
    before = copy.deepcopy(wiki._recovery_pages)
    with pytest.raises(projection.GraphProjectionRefused):
        projection.capture_graph_projection(CreationRegistry(wiki))
    assert wiki._recovery_pages == before


def test_saved_projection_preserves_plural_birth_evidence(graph, tmp_path):
    _, registry, registered, _, _ = graph
    recorded = projection.capture_graph_projection(registry)
    qid = registered.binding.qid
    updated = recorded.model_copy(
        update={
            "roster": tuple(
                row.model_copy(update={"births": fictional_births(qid)})
                if row.qid == qid
                else row
                for row in recorded.roster
            )
        }
    )
    path = tmp_path / "projection.json"
    projection.save_graph_projection(updated, path)
    loaded = projection.load_graph_projection(path)
    (person,) = [row for row in loaded.roster if row.qid == qid]
    assert person.births == fictional_births(qid)
    assert loaded.digest == updated.digest != recorded.digest


def test_sampled_projection_requires_recapture(graph, tmp_path):
    _, registry, _, _, _ = graph
    path = tmp_path / "projection.json"
    projection.save_graph_projection(
        projection.capture_graph_projection(registry), path
    )
    saved = json.loads(path.read_text())
    saved["projection"]["selector_version"] = "roster-v1"
    for row in saved["projection"]["roster"]:
        row.pop("births")
        row["birth"] = "1919-01-07"
    path.write_text(json.dumps(saved))
    with pytest.raises(
        projection.GraphProjectionRefused, match="fresh projection"
    ):
        projection.load_graph_projection(path)


def test_projection_covers_local_sources_but_not_standard_calendar(graph):
    _, registry, _, _, _ = graph
    recorded = projection.capture_graph_projection(registry)
    from yizkr.core.dates import GREGORIAN

    query = projection.RecordedQuery(
        purpose="roster",
        query="fictional birth evidence",
        rows=({"timeCalendar": {"type": "uri", "value": GREGORIAN}},),
    )
    projection._covered_rows(
        query, recorded.generation.instance_uri, recorded.entity_revisions
    )
    for name, uri in (
        ("dateSource", GREGORIAN),
        ("timeCalendar", "http://www.wikidata.org/entity/Q1985786"),
        ("dateSource", recorded.generation.instance_uri + "/entity/Q99999"),
    ):
        invalid = query.model_copy(
            update={
                "rows": (
                    {
                        name: {"type": "uri", "value": uri},
                    },
                )
            }
        )
        with pytest.raises(projection.GraphProjectionRefused):
            projection._covered_rows(
                invalid,
                recorded.generation.instance_uri,
                recorded.entity_revisions,
            )
