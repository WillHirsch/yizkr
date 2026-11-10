"""Recorded graph inputs for reconciliation, without write authority.

Capture checks the query index against authoritative entity revisions on
both sides of the existing roster queries. These observations qualify a
read-only planning product; they do not lock the graph or protect an absent
candidate through a later mutation. Saved products contain private graph
evidence and belong with runtime artifacts, independently of response caches.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.backup import REVISIONS_QUERY
from yizkr.core import sparql
from yizkr.core.dates import GREGORIAN
from yizkr.core.identity_assignments import (
    AssignmentSnapshot,
    RegistryBinding,
    record_digest,
    record_json,
)
from yizkr.core.instance import watermark
from yizkr.core.write_recovery import CreationRegistry, Generation
from yizkr.pipeline.reconcile import (
    RosterEntity,
    fetch_place_containment,
    fetch_roster,
)
from yizkr.pipeline.stages import atomic_write_text

_ENTITY = re.compile(r"[PQ][1-9][0-9]*")
_ITEM = re.compile(r"Q[1-9][0-9]*")
_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
_Revision = Annotated[int, Field(gt=0, strict=True)]


class GraphProjectionRefused(ValueError):
    """The observed graph cannot supply the requested recorded projection."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordedQuery(_Record):
    purpose: Literal["index-before", "roster", "containment", "index-after"]
    query: str = Field(min_length=1)
    rows: tuple[dict[str, dict[str, str]], ...]


class ProjectionCapture(_Record):
    method: Literal["api-inventory-and-wdqs-revisions"] = (
        "api-inventory-and-wdqs-revisions"
    )
    scope: Literal["all-live-nonredirect-PQ-entities"] = (
        "all-live-nonredirect-PQ-entities"
    )
    consistency: Literal["unchanged-observed-watermarks-and-revisions"] = (
        "unchanged-observed-watermarks-and-revisions"
    )
    complete: Literal[True] = True
    mutation_authority: Literal[False] = False
    watermark_before: dict[str, int]
    watermark_after: dict[str, int]

    @model_validator(mode="after")
    def unchanged_watermark(self):
        for observed in (self.watermark_before, self.watermark_after):
            if set(observed) != {"revision", "log"} or any(
                type(value) is not int or value < 0
                for value in observed.values()
            ):
                raise ValueError(
                    "capture requires a revision and log watermark"
                )
        if self.watermark_before != self.watermark_after:
            raise ValueError("graph changed during projection capture")
        return self


class GraphProjection(_Record):
    format: Literal[1] = 1
    selector_version: Literal["roster-v2"] = "roster-v2"
    generation: Generation
    captured_at: datetime
    entity_revisions: dict[str, _Revision]
    roster: tuple[RosterEntity, ...]
    containment: dict[str, tuple[str, ...]]
    bindings: dict[str, RegistryBinding]
    unbound_qids: tuple[str, ...]
    assignments: AssignmentSnapshot
    queries: tuple[RecordedQuery, ...]
    capture: ProjectionCapture

    @model_validator(mode="after")
    def covered_inputs(self):
        if self.captured_at.utcoffset() != timedelta(0):
            raise ValueError("projection capture time must be UTC")
        if any(not _ENTITY.fullmatch(key) for key in self.entity_revisions):
            raise ValueError("projection revision inventory requires P/Q IDs")
        if self.assignments.generation != self.generation:
            raise ValueError(
                "projection assignments belong to another generation"
            )
        for entity in self.roster:
            if (
                not _ITEM.fullmatch(entity.qid)
                or entity.qid not in self.entity_revisions
                or not entity.kin_qids <= self.entity_revisions.keys()
                or any(birth.person != entity.qid for birth in entity.births)
                or (
                    _ITEM.fullmatch(entity.kind)
                    and entity.kind not in self.entity_revisions
                )
            ):
                raise ValueError("roster facts lack entity revision coverage")
        for qid, containers in self.containment.items():
            if (
                not _ITEM.fullmatch(qid)
                or qid not in self.entity_revisions
                or any(
                    not _ITEM.fullmatch(container)
                    or container not in self.entity_revisions
                    for container in containers
                )
                or tuple(sorted(set(containers))) != containers
            ):
                raise ValueError("containment lacks entity revision coverage")
        indexed = {}
        for qid, registered in self.bindings.items():
            binding = registered.binding
            if (
                qid != binding.qid
                or qid not in self.entity_revisions
                or binding.deployment_id != self.generation.deployment_id
                or binding.generation != self.generation.generation
                or binding.revision > self.entity_revisions[qid]
                or binding.entity_id in indexed
            ):
                raise ValueError("binding differs from projection authority")
            indexed[binding.entity_id] = registered
        for assignment in self.assignments.assignments.values():
            if assignment.entity_id not in indexed:
                raise ValueError("assignment lacks its indexed entity binding")
        if self.unbound_qids != tuple(
            sorted(
                {entity.qid for entity in self.roster} - self.bindings.keys()
            )
        ):
            raise ValueError("projection omits an unbound roster candidate")
        if (
            len(self.queries) < 4
            or self.queries[0].purpose != "index-before"
            or self.queries[-1].purpose != "index-after"
            or {query.purpose for query in self.queries}
            != {"index-before", "roster", "containment", "index-after"}
        ):
            raise ValueError("projection lacks its complete query provenance")
        for query in (self.queries[0], self.queries[-1]):
            if (
                query.query != REVISIONS_QUERY
                or _index_inventory(query.rows, self.generation.instance_uri)
                != self.entity_revisions
            ):
                raise ValueError(
                    "query index differs from entity revision inventory"
                )
        for query in self.queries[1:-1]:
            _covered_rows(
                query, self.generation.instance_uri, self.entity_revisions
            )
        return self

    @property
    def digest(self) -> str:
        return record_digest(self)


class _Envelope(_Record):
    format: Literal[1] = 1
    projection: GraphProjection
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def intact(self):
        if self.digest != self.projection.digest:
            raise ValueError(
                "graph projection digest does not match its contents"
            )
        return self


def _index_inventory(rows, concept_uri: str) -> dict[str, int]:
    prefix = concept_uri + "/entity/"
    inventory = {}
    for row in rows:
        entity = row.get("entity", {})
        version = row.get("version", {})
        uri = entity.get("value", "")
        qid = uri.removeprefix(prefix)
        raw_revision = version.get("value", "")
        if (
            entity.get("type") != "uri"
            or not uri.startswith(prefix)
            or not _ENTITY.fullmatch(qid)
            or version.get("type") not in ("literal", "typed-literal")
            or not re.fullmatch(r"[1-9][0-9]*", raw_revision)
            or qid in inventory
        ):
            raise GraphProjectionRefused(
                "query index revision inventory is invalid"
            )
        inventory[qid] = int(raw_revision)
    return inventory


def _covered_rows(query, concept_uri, revisions):
    """Require live revision coverage for entity facts the readers consume."""
    prefix = concept_uri + "/entity/"
    for row in query.rows:
        # The shared containment reader ignores redirect rows completely.
        if (
            query.purpose == "containment"
            and row.get("link", {}).get("value") == _SAME_AS
        ):
            continue
        for name, cell in row.items():
            if cell.get("type") != "uri":
                continue
            uri = cell.get("value", "")
            # Wikibase calendars name an external standard, not an entity
            # whose local revision supplies the historical assertion.
            if name.endswith("Calendar") and uri == GREGORIAN:
                continue
            tail = uri.rsplit("/entity/", 1)[-1]
            if _ENTITY.fullmatch(tail) and (
                not uri.startswith(prefix) or tail not in revisions
            ):
                raise GraphProjectionRefused(
                    "query fact lacks local entity revision coverage"
                )


def capture_graph_projection(registry: CreationRegistry) -> GraphProjection:
    """Capture existing shared graph inputs; never establish write authority.

    Index lag, extra indexed entities and observed changes refuse immediately.
    No waiting, creation, indexing or assignment acceptance occurs here.
    """
    client = registry.client
    generation = registry.generation
    if generation is None:
        raise GraphProjectionRefused(
            "graph projection requires an existing generation"
        )
    before = watermark(client)
    ids = client.list_entity_ids()
    revisions = client.entity_revisions(ids)
    client.forget_vocabulary()
    run = sparql.runner(client.config)
    recorded = []

    def runner(purpose):
        def query(text):
            rows = run(text)
            observation = RecordedQuery(purpose=purpose, query=text, rows=rows)
            recorded.append(observation)
            _covered_rows(observation, generation.instance_uri, revisions)
            return rows

        return query

    if (
        _index_inventory(
            runner("index-before")(REVISIONS_QUERY), generation.instance_uri
        )
        != revisions
    ):
        raise GraphProjectionRefused(
            "query index differs from authoritative revisions"
        )
    roster = fetch_roster(client, client.config, runner=runner("roster"))
    closure = fetch_place_containment(
        client, client.config, runner=runner("containment")
    )
    containment = {
        qid: tuple(sorted(containers))
        for qid in revisions
        if (containers := closure.containers(qid))
    }
    bindings = registry.indexed_bindings()
    assignments = registry.assignment_snapshot()
    if (
        _index_inventory(
            runner("index-after")(REVISIONS_QUERY), generation.instance_uri
        )
        != revisions
    ):
        raise GraphProjectionRefused(
            "query index changed during projection capture"
        )
    current = client.entity_revisions(client.list_entity_ids())
    if current != revisions or registry.generation != generation:
        raise GraphProjectionRefused(
            "entity revisions or generation changed during capture"
        )
    after = watermark(client)
    if after != before:
        raise GraphProjectionRefused("graph changed during projection capture")
    return GraphProjection(
        generation=generation,
        captured_at=datetime.now(UTC),
        entity_revisions=revisions,
        roster=tuple(
            sorted(roster, key=lambda entity: (entity.qid, entity.kind))
        ),
        containment=containment,
        bindings=bindings,
        unbound_qids=tuple(
            sorted({entity.qid for entity in roster} - bindings.keys())
        ),
        assignments=assignments,
        queries=tuple(recorded),
        capture=ProjectionCapture(
            watermark_before=before, watermark_after=after
        ),
    )


def save_graph_projection(projection: GraphProjection, path: Path) -> None:
    """Save the complete private planning product with its content digest."""
    envelope = _Envelope(
        projection=GraphProjection.model_validate(
            projection.model_dump(mode="json")
        ),
        digest=projection.digest,
    )
    atomic_write_text(path, record_json(envelope) + "\n")


def load_graph_projection(path: Path) -> GraphProjection:
    """Validate saved content without any wiki, query service or model call."""
    try:
        contents = json.loads(path.read_text())
        if (
            contents.get("projection", {}).get("selector_version")
            != "roster-v2"
        ):
            raise GraphProjectionRefused(
                "saved graph projection uses an unsupported roster selector; "
                "capture a fresh projection before matching"
            )
        return _Envelope.model_validate(contents).projection
    except GraphProjectionRefused:
        raise
    except (ValueError, AttributeError) as exc:
        raise GraphProjectionRefused(
            "saved graph projection is invalid"
        ) from exc
