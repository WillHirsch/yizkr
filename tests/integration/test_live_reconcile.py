"""Live reconciliation reads for the impossibility filter (ADR-0017).

The extended roster query (kinship QIDs, last activity) and the
place-containment closure are what let blocking prune pairs the graph
already forbids. This exercises both against a real stack: write a
kinship edge and a containment edge with deterministic labels, then
assert `fetch_roster` / `fetch_place_containment` round-trip them and
`impossible_pair` rules the same-name pairs out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yizkr.claims import ClaimBatch
from yizkr.pipeline.reconcile import (
    _roster_pair_facts,
    fetch_place_containment,
    fetch_roster,
    impossible_pair,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

ELDER = "Yizkr Reconcile-Test Elder"
YOUNGER = "Yizkr Reconcile-Test Younger"
COUNTRY = "Yizkr Reconcile-Test Country"
TOWN = "Yizkr Reconcile-Test Town"


def _local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def _source(artifact_qid: str, excerpt: str) -> dict:
    return {
        "artifact": {"kind": "qid", "qid": artifact_qid},
        "page_number": "1",
        "transcription_excerpt": {"text": excerpt, "language": "en"},
    }


def _relations_batch(artifact_qid: str) -> ClaimBatch:
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
                    "source": _source(artifact_qid, "son of the elder"),
                },
                {
                    "subject": _local("e_town", TOWN, "place"),
                    "property": "part_of",
                    "value": {
                        "type": "item",
                        "value": _local("e_country", COUNTRY, "place"),
                    },
                    "source": _source(artifact_qid, "a town in the country"),
                },
            ],
        }
    )


@pytest.fixture(scope="module")
def written(client, tmp_path_factory):
    """Write the kinship + containment edges once (deterministic labels ->
    re-runs reuse the same items, per the live-test idempotency contract).
    Returns the label->qid map from the roster after the write."""
    media = tmp_path_factory.mktemp("media") / "reconcile-fixture.txt"
    media.write_text("reconcile-test fixture; safe to delete")
    artifact = register_artifact(
        client,
        media_path=media,
        artifact_type="letter",
        title="Reconcile-Test Source",
        description="written by tests/integration; safe to delete",
        pages=1,
    )
    BatchWriter(client).write_batch(_relations_batch(artifact.artifact_qid))
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
    assert country.label in containment.container_labels(town.qid)
    # ... so the same-name pair is ruled out before adjudication
    assert (
        impossible_pair(
            _roster_pair_facts(town),
            _roster_pair_facts(country),
            containment=containment,
        )
        == "place-containment"
    )
