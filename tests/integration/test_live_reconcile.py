"""Live reconciliation reads for the impossibility filter (ADR-0017).

The extended roster query (kinship QIDs, last activity) is what lets
blocking prune pairs the graph already forbids. This writes a kinship
edge under labels carrying the session's marker (conftest.fixture_mark),
so each session meets it as a first write.

The fixture also holds dates at century and decade precision, curator-made
events (ADR-0040 Q5; conftest.curator_event_writes), a death of unknown
value, a containment edge and two grandmothers' appellations. The roster's
reading of such data and the containment closure are checked in the
required lane (tests/fictional_integration/test_production_queries.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import (
    curator_event_writes,
    fixture_mark,
    wait_for_query_service,
)
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import reconcile, writer
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0017: an elder, the younger whose father he is, and a town part "
    "of a country, in one letter; #337: the elder born in the 19th century "
    "and dead in the 1890s, the younger's birth an event of the 1850s and his "
    "death an event of 1938; #523: two grandmothers each aliased 'Oma' "
    "with a referred_to_as statement giving it, and a name form each; "
    "#687: the events curator-made, the younger their principal, and a "
    "brother's death of unknown date"
)
MARK = fixture_mark(FIXTURE, reconcile, writer)
ELDER = f"Yizkr Reconcile-Test Elder {MARK}"
YOUNGER = f"Yizkr Reconcile-Test Younger {MARK}"
COUNTRY = f"Yizkr Reconcile-Test Country {MARK}"
TOWN = f"Yizkr Reconcile-Test Town {MARK}"
BIRTH = f"Yizkr Reconcile-Test Birth {MARK}"
DEATH = f"Yizkr Reconcile-Test Death {MARK}"
BROTHER = f"Yizkr Reconcile-Test Brother {MARK}"
GRANDMOTHER_A = f"Yizkr Reconcile-Test Grandmother A {MARK}"
GRANDMOTHER_B = f"Yizkr Reconcile-Test Grandmother B {MARK}"
APPELLATION = "Oma"
# each grandmother's name form among her aliases, beside the appellation
NAME_A = f"Hilde {MARK}"
NAME_B = f"Sipkje {MARK}"


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


# the younger's part in each event: its principal
PRINCIPAL = [
    {
        "property": "object_has_role",
        "value": {
            "type": "item",
            "value": {"kind": "vocab", "key": "principal"},
        },
    }
]


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
                {"id": "e_birth", "label": BIRTH, "kind": "birth"},
                {"id": "e_death", "label": DEATH, "kind": "death"},
                {"id": "e_brother", "label": BROTHER, "kind": "human"},
                {
                    "id": "e_grandmother_a",
                    "label": GRANDMOTHER_A,
                    "aliases": [APPELLATION, NAME_A],
                    "kind": "human",
                },
                {
                    "id": "e_grandmother_b",
                    "label": GRANDMOTHER_B,
                    "aliases": [APPELLATION, NAME_B],
                    "kind": "human",
                },
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
                {
                    "subject": _local("e_elder", ELDER, "human"),
                    "property": "date_of_birth",
                    "value": {
                        "type": "time",
                        "year": 1801,
                        "precision": "century",
                    },
                    "source": _source(artifact, "born last century"),
                },
                {
                    "subject": _local("e_elder", ELDER, "human"),
                    "property": "date_of_death",
                    "value": {
                        "type": "time",
                        "year": 1890,
                        "precision": "decade",
                    },
                    "source": _source(artifact, "died in the nineties"),
                },
                {
                    "subject": _local("e_birth", BIRTH, "birth"),
                    "property": "participant",
                    "value": {
                        "type": "item",
                        "value": _local("e_younger", YOUNGER, "human"),
                    },
                    "qualifiers": PRINCIPAL,
                    "source": _source(artifact, "born in the fifties"),
                },
                {
                    "subject": _local("e_birth", BIRTH, "birth"),
                    "property": "point_in_time",
                    "value": {
                        "type": "time",
                        "year": 1850,
                        "precision": "decade",
                    },
                    "source": _source(artifact, "born in the fifties"),
                },
                {
                    "subject": _local("e_death", DEATH, "death"),
                    "property": "participant",
                    "value": {
                        "type": "item",
                        "value": _local("e_younger", YOUNGER, "human"),
                    },
                    "qualifiers": PRINCIPAL,
                    "source": _source(artifact, "the son, died in 1938"),
                },
                {
                    "subject": _local("e_death", DEATH, "death"),
                    "property": "point_in_time",
                    "value": {"type": "time", "year": 1938},
                    "source": _source(artifact, "the son, died in 1938"),
                },
                {
                    "subject": _local("e_brother", BROTHER, "human"),
                    "property": "date_of_death",
                    "value": {"type": "unknown"},
                    "source": _source(artifact, "his late brother"),
                },
                *(
                    {
                        "subject": _local(local_id, label, "human"),
                        "property": "referred_to_as",
                        "value": {
                            "type": "monolingual",
                            "value": {"text": APPELLATION, "language": "de"},
                        },
                        "source": _source(artifact, f"{APPELLATION} schreibt"),
                    }
                    for local_id, label in (
                        ("e_grandmother_a", GRANDMOTHER_A),
                        ("e_grandmother_b", GRANDMOTHER_B),
                    )
                ),
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
    with curator_event_writes():
        result = write_mapped(BatchWriter(client), batch)
        # written again, the batch finds every item and statement
        again = write_mapped(BatchWriter(client), batch)
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
