"""Identity proposals from clustering reach review, application and the graph.

A first increment onto a stack that holds only the bootstrapped model and
its registered source, where every cluster is new, and a second increment
that adds a source whose mention joins the first's entity. Decisions are
recorded and applied through the review store and the protected cohort
application, the production identity-provenance query reads the
published origin of the map-derived assignment, and the static site's entry
for that origin is made from the rows it returns. Readings and mention
records are authored fiction; no model is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.semantic_acceptance import review_source, source_case
from yizkr.claims import GivenName, PersonStructure, Surname
from yizkr.core import sparql
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import CreationRegistry
from yizkr.model.schema import load_manifest
from yizkr.pipeline.cluster_proposals import prepare_cluster_proposals
from yizkr.pipeline.clustering import (
    DOCUMENT,
    IdentityMap,
    ensure_identity_map,
)
from yizkr.pipeline.cohort_additions import apply_cohort_additions
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.registrar import register_artifact
from yizkr.queries.publish import load_templates, render_for_instance
from yizkr.staticsite import SHORT, SiteBuilder, identity_origins, visible

from .test_write_query import _synchronize

pytestmark = pytest.mark.live
MANIFEST = load_manifest()
WHEN = "2026-01-01T12:00:00Z"
CURATOR = "fictional cluster curator"
BIRTH = "birth-extract-1938"
NATURALISATION = "naturalisation-1952"


@dataclass(frozen=True)
class Document:
    """A document as the clustering stage reads it: its SHA-1, its kind and
    its saved batch."""

    sha1: str
    kind: str
    batch: Path


def _source(client, directory, key):
    """One of Gustav's birth sources, uploaded and registered as its
    authored text, with a mention record naming him by given name and
    surname."""
    case = source_case(key)
    path = directory / f"{key}.txt"
    path.write_bytes(case.source_bytes)
    register_artifact(
        client,
        path,
        "certificate",
        f"Cluster proposals: {case.title}",
        pages=1,
    )
    source = review_source(key)
    (entity,) = source.batch.entities
    if key == BIRTH:
        entity.label, entity.aliases = "Gustav Löbig", ["Gustav"]
        given = GivenName(written="Gustav")
        surname = Surname(written="Löbig")
    else:
        given = GivenName(written="Gustave", standard="Gustav")
        surname = Surname(written="LOBIG")
    entity.structure = PersonStructure(given=[given], surnames=[surname])
    return source


def _identity(directory, *sources):
    """The clustering stage's identity map of the sources, with its
    producer record."""
    documents = []
    for source in sources:
        path = directory / f"{source.artifact_sha1}.batch.json"
        path.write_text(source.batch.model_dump_json())
        documents.append(Document(source.artifact_sha1, DOCUMENT, path))
    folder = directory / f"map-{len(sources)}"
    folder.mkdir()
    return IdentityMap(ensure_identity_map(folder, documents).identity_map)


def _decide(store, prepared, source):
    """Approve the one choice prepared for the source's mention."""
    mention = prepared.review.mentions[source.artifact_sha1, "gustav"]
    (proposal,) = (
        p
        for p in prepared.review.proposals
        if p.mentions == (mention.id,)
        and p.operation in ("assign", "new_entity")
    )
    return mention.id, store.decide(
        proposal.id,
        proposal.revision,
        expected_digest=proposal.digest,
        outcome="approve",
        actor=CURATOR,
        time=WHEN,
    )


def _apply(store, request, registry, directory, operation):
    _synchronize(registry.client)
    return apply_cohort_additions(
        store,
        request,
        registry,
        state_path=directory / f"{operation}.capability",
        operation_id=operation,
        record_dir=directory / "records",
    )


def test_cluster_choices_reach_review_application_and_graph_origins(
    fictional_client, tmp_path
):
    client = WikiClient(fictional_client.config)
    registry = CreationRegistry(client)
    properties, items = client.vocabulary()
    birth = _source(client, tmp_path, BIRTH)
    # The stack holds the bootstrapped model and the registered source:
    # a deployment generation and no assignment.
    assert registry.generation is not None
    assert not registry.assignment_snapshot().assignments
    with ReviewStore(tmp_path / "review.sqlite") as store:
        first_map = _identity(tmp_path, birth)
        _synchronize(client)
        first = prepare_cluster_proposals(
            store,
            first_map,
            (birth,),
            registry,
            increment=[birth.artifact_sha1],
        )
        assert [p.operation for p in first.request.proposals] == ["new_entity"]
        birth_id, _ = _decide(store, first.prepared, birth)
        created = _apply(store, first.request, registry, tmp_path, "first")
        entity = created.acceptance.snapshot.assignments[birth_id].entity_id

        # A second increment adds the naturalisation certificate, whose
        # mention clustering joins to the birth extract's.
        naturalisation = _source(client, tmp_path, NATURALISATION)
        second_map = _identity(tmp_path, birth, naturalisation)
        sources = (birth, naturalisation)
        increment = [naturalisation.artifact_sha1]
        _synchronize(client)
        second = prepare_cluster_proposals(
            store, second_map, sources, registry, increment=increment
        )
        (joined,) = second.request.proposals
        assert (joined.operation, joined.target.entity_id) == (
            "assign",
            entity,
        )
        assert second.report.counts["assigned"] == 1
        naturalisation_id, decision = _decide(
            store, second.prepared, naturalisation
        )
        # Deriving again from unchanged inputs reopens no decided choice.
        again = prepare_cluster_proposals(
            store, second_map, sources, registry, increment=increment
        )
        assert decision.proposal in again.prepared.review.proposals
        assert store.get_decision(decision.proposal.id) == decision
        joining = _apply(store, second.request, registry, tmp_path, "second")
        assignments = joining.acceptance.snapshot.assignments
        assert assignments[naturalisation_id].entity_id == entity
        qid = registry.binding(entity).binding.qid
        uri = client.config.concept_uri + "/entity/"
        records = {uri + p.record_qid for p in joining.publications}

        _synchronize(client)
        query = render_for_instance(
            load_templates()["identity-provenance"],
            properties,
            items,
            client.config.concept_uri,
        )
        rows = sparql.select(client.config.sparql_url, query)

    def origin(mention_id, source, identity, method):
        """The graph's origin rows of the mention's assignment: the map's
        method and cluster for it, the map's hash, and the reviewer's
        separate confirmation; and the site's entry for that origin."""
        revision = assignments[mention_id].revision_id
        found = [
            row
            for row in rows
            if row.get("assignmentRevision", {}).get("value") == revision
        ]
        (cluster,) = {
            entry["cluster"]
            for entry in identity.data["mentions"].values()
            if entry["document"] == source.artifact_sha1
        }
        assert found
        assert {row["record"]["value"] for row in found} <= records
        assert {row["entity"]["value"] for row in found} == {uri + qid}
        assert {row["method"]["value"] for row in found} == {
            uri + items[method]
        }
        assert {row["cluster"]["value"] for row in found} == {cluster}
        assert {row["mapHash"]["value"] for row in found} == {identity.hash}
        assert {row["actor"]["value"] for row in found} == {CURATOR}
        assert {row["confirmedAt"]["value"] for row in found} == {WHEN}
        assert {row["stage"]["value"] for row in found} >= {
            "clustering",
            "cohort-identity-confirmation",
        }
        # The site's entry for the origin, made from these rows, names the
        # map's method, with the confirmation beside it, and does not call
        # it unreviewed.
        (shown,) = [
            visible(SiteBuilder.origin_html(each))
            for each in identity_origins(rows)[qid]
            if each.assignment_revision == revision
        ]
        assert shown.startswith(MANIFEST.items[method].label + " — ")
        assert f"confirmed by {CURATOR} at {WHEN}" in shown
        assert "not confirmed by a person" not in shown
        assert "unreviewed machine output" not in shown
        for value in (cluster, identity.hash):
            assert value[:SHORT] + "…" in shown

    # The creation's origin is its own map's singleton cluster; the joining
    # mention's is the confident cluster of the second increment's map.
    origin(birth_id, birth, first_map, "identity_singleton")
    origin(naturalisation_id, naturalisation, second_map, "identity_confident")
