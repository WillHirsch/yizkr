"""Live reconciliation reads for the impossibility filter (ADR-0017).

The extended roster query (kinship QIDs, last activity) and the
place-containment closure are what let blocking prune pairs the graph
already forbids. This exercises both against a real stack: write a
kinship edge and a containment edge under labels carrying the session's
marker (conftest.fixture_mark), so each session meets them as a first
write, then assert `fetch_roster` / `fetch_place_containment` round-trip
them and `impossible_pair` rules the same-name pairs out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.integration.conftest import fixture_mark, wait_for_query_service
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import reconcile, writer
from yizkr.pipeline.reconcile import (
    _roster_pair_facts,
    fetch_place_containment,
    fetch_roster,
    impossible_pair,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0017: an elder, the younger whose father he is, and a town part "
    "of a country, in one letter"
)
MARK = fixture_mark(FIXTURE, reconcile, writer)
ELDER = f"Yizkr Reconcile-Test Elder {MARK}"
YOUNGER = f"Yizkr Reconcile-Test Younger {MARK}"
COUNTRY = f"Yizkr Reconcile-Test Country {MARK}"
TOWN = f"Yizkr Reconcile-Test Town {MARK}"


def _local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def _source(artifact: ArtifactRef, excerpt: str) -> dict:
    return {
        "artifact": artifact.model_dump(mode="json"),
        "page_number": "1",
        "transcription_excerpt": {"text": excerpt, "language": "en"},
    }


def _relations_batch(artifact: ArtifactRef) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "entities": [
                {"id": "e_younger", "label": YOUNGER, "kind": "human"},
                {"id": "e_elder", "label": ELDER, "kind": "human"},
                {"id": "e_town", "label": TOWN, "kind": "place"},
                {"id": "e_country", "label": COUNTRY, "kind": "place"},
            ],
            "claims": [
                {
                    "subject": _local("e_younger", YOUNGER, "human"),
                    "property": "father",
                    "value": {
                        "type": "item",
                        "value": _local("e_elder", ELDER, "human"),
                    },
                    "source": _source(artifact, "son of the elder"),
                },
                {
                    "subject": _local("e_town", TOWN, "place"),
                    "property": "part_of",
                    "value": {
                        "type": "item",
                        "value": _local("e_country", COUNTRY, "place"),
                    },
                    "source": _source(artifact, "a town in the country"),
                },
            ],
        }
    )


@pytest.fixture(scope="module")
def written(client, config, tmp_path_factory):
    """Write the kinship + containment edges once a session, and wait
    until the query service holds every item the write made. Returns the
    source's item."""
    media = tmp_path_factory.mktemp("media") / f"reconcile-fixture-{MARK}.txt"
    media.write_text(f"reconcile-test fixture {MARK}; safe to delete")
    artifact = register_artifact(
        client,
        media_path=media,
        artifact_type="letter",
        title=f"Reconcile-Test Source {MARK}",
        description="written by tests/integration; safe to delete",
        pages=1,
    )
    # the claims cite the source by the SHA-1 of its media (#102)
    batch = _relations_batch(ArtifactRef.from_path(media))
    result = BatchWriter(client).write_batch(batch)
    # written again, the batch finds every item and statement
    again = BatchWriter(client).write_batch(batch)
    assert (again.written, again.referenced) == (0, 0)
    assert again.created_items == result.created_items
    wait_for_query_service(
        config,
        client,
        [*result.created_items.values(), artifact.artifact_qid],
    )
    return artifact.artifact_qid


def _by_label(roster, label):
    matches = [entity for entity in roster if entity.label == label]
    assert matches, f"no roster entity labelled {label!r}"
    return matches[0]


def test_roster_exposes_kinship_qids(client, config, written):
    roster = fetch_roster(client, config)
    younger = _by_label(roster, YOUNGER)
    elder = _by_label(roster, ELDER)
    # the father edge round-trips as a raw QID the filter can read
    assert elder.qid in younger.father_qids
    assert elder.qid in younger.kin_qids
    # each kind maps back to its manifest key by the kind item's QID
    # (#388)
    assert younger.kind == elder.kind == "human"
    assert _by_label(roster, TOWN).kind == "place"


def test_kinship_edge_makes_the_pair_impossible(client, config, written):
    roster = fetch_roster(client, config)
    younger = _roster_pair_facts(_by_label(roster, YOUNGER))
    elder = _roster_pair_facts(_by_label(roster, ELDER))
    assert impossible_pair(younger, elder) == "kinship-edge"


def test_place_containment_closure_round_trips(client, config, written):
    roster = fetch_roster(client, config)
    town = _by_label(roster, TOWN)
    country = _by_label(roster, COUNTRY)
    containment = fetch_place_containment(client, config)
    # the town is transitively contained in the country
    assert containment.contains(country.qid, town.qid)
    assert country.qid in containment.containers(town.qid)
    # ... so the same-name pair is ruled out before adjudication
    assert (
        impossible_pair(
            _roster_pair_facts(town),
            _roster_pair_facts(country),
            containment=containment,
        )
        == "place-containment"
    )
