"""Live containment as source claims (ADR-0038 §2, §5, §8; #515).

A containment is a source's claim, with the time its source gives. This
writes, through the running stack, a dated and an undated containment of
one hamlet in one district (`part_of` with a `point_in_time`, per a
letter that says the hamlet belonged to the district in 1941, and
without, per a certificate's district field), and the district in an
oblast from 1937, the only link to the oblast, so the path runs through
a dated claim. The containment query, the inconsistency report and the
impossibility filter's closure are checked on such edges in the required
lane (tests/fictional_integration/test_production_queries.py).

The places are the universe's Koshnovka, Kaldovsky District and Chadsk
Oblast (docs/EXAMPLES.md), with the pattern index's worked example of a
dated containment, under labels that carry the session's marker
(conftest.fixture_mark). Like every live test it writes fictional data
to the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.identity import by_label, write_mapped
from tests.integration.conftest import (
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)
from yizkr import disagreement
from yizkr.claims import ClaimBatch
from yizkr.pipeline import reconcile, writer
from yizkr.pipeline.registrar import (
    _append,
    _existing_claim_targets,
    _item_claim,
)
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0038: a hamlet in a district at a point in time per a letter and "
    "undated per a certificate, the district in an oblast from 1937, a "
    "deprecated containment in a decoy district, a run-only one in a "
    "second decoy, and three people born in them"
)
MARK = fixture_mark(FIXTURE, disagreement, reconcile, writer)
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-23T09:00:00Z",
}


def _local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": f"{label} {MARK}",
        "instance_of": kind,
    }


HAMLET = _local("hamlet", "Containment-Test Koshnovka", "place")
DISTRICT = _local("district", "Containment-Test Kaldovsky District", "place")
OBLAST = _local("oblast", "Containment-Test Chadsk Oblast", "place")
DECOY = _local("decoy", "Containment-Test Decoy District", "place")
UNSTATED = _local("unstated", "Containment-Test Second Decoy", "place")
REFINED = _local("refined", "Containment-Test Rudolf", "human")
LISTED = _local("listed", "Containment-Test Nina", "human")
RUN_ONLY = _local("run-only", "Containment-Test Pavel", "human")


def _time(year: int) -> dict:
    return {"type": "time", "year": year}


def _claim(subject, prop, value, source, excerpt, qualifiers=()) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": {"type": "item", "value": value},
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": source.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "en"},
        },
    }


def _batch(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


@pytest.fixture(scope="module")
def written(client, config):
    """Write the letter's and the certificate's claims with one identity
    map, deprecate the decoy containment as a curator would, write a
    containment in the second decoy that cites the letter's run alone, as
    the place-hierarchy pass writes one, and wait until the query service
    holds every item. Returns the QIDs by local id."""
    letter, certificate = (
        register_fictional(client, f"Containment-Test {kind} {MARK}", kind)
        for kind in ("letter", "certificate")
    )
    dated = [
        {"property": "point_in_time", "value": _time(1941)},
        {"property": "earliest_date", "value": _time(1940)},
        {"property": "latest_date", "value": _time(1941)},
    ]
    said_in_letter = _batch(
        _claim(
            HAMLET,
            "part_of",
            DISTRICT,
            letter.ref,
            "in 1941 Koshnovka belonged to Kaldovsky District",
            dated,
        ),
        _claim(HAMLET, "part_of", DECOY, letter.ref, "Koshnovka, Decoy"),
        _claim(
            DISTRICT,
            "part_of",
            OBLAST,
            letter.ref,
            "Kaldovsky District, in Chadsk Oblast from 1937",
            [{"property": "start_time", "value": _time(1937)}],
        ),
        _claim(REFINED, "place_of_birth", HAMLET, letter.ref, "Koshnovka"),
        _claim(LISTED, "place_of_birth", HAMLET, letter.ref, "Koshnovka"),
        _claim(RUN_ONLY, "place_of_birth", HAMLET, letter.ref, "Koshnovka"),
    )
    said_in_certificate = _batch(
        _claim(
            HAMLET,
            "part_of",
            DISTRICT,
            certificate.ref,
            "Koshnovka, Kaldovsky District",
        ),
        _claim(
            REFINED, "place_of_birth", OBLAST, certificate.ref, "Chadsk Oblast"
        ),
        _claim(LISTED, "place_of_birth", DECOY, certificate.ref, "Decoy"),
        _claim(
            RUN_ONLY,
            "place_of_birth",
            UNSTATED,
            certificate.ref,
            "Second Decoy",
        ),
    )
    identity = by_label(said_in_letter, said_in_certificate)
    qids: dict[str, str] = {}
    runs = []
    for batch in (said_in_letter, said_in_certificate):
        result = write_mapped(BatchWriter(client), batch, identity=identity)
        qids.update(result.created_items)
        runs.append(result.run_qid)
        # written again, the batch finds every item and statement
        again = write_mapped(BatchWriter(client), batch, identity=identity)
        assert (again.written, again.referenced) == (0, 0)
    properties, _ = client.vocabulary()
    hamlet = client.wbi.item.get(qids["hamlet"])
    (decoy,) = [
        statement
        for statement in hamlet.claims.get(properties["part_of"])
        if statement.mainsnak.datavalue["value"]["id"] == qids["decoy"]
    ]
    if decoy.rank != WikibaseRank.DEPRECATED:
        decoy.rank = WikibaseRank.DEPRECATED
        hamlet.write(summary="integration test: curator rank on a fixture")
    # a containment that cites a run alone, with no stated-in source
    hamlet = client.wbi.item.get(qids["hamlet"])
    if qids["unstated"] not in _existing_claim_targets(
        hamlet, properties["part_of"]
    ):
        _append(
            hamlet,
            _item_claim(
                properties["part_of"],
                qids["unstated"],
                properties["inferred_from_extraction_run"],
                runs[0],
            ),
        )
        hamlet.write(summary="integration test: a run-only containment")
    wait_for_query_service(
        config, client, [*qids.values(), letter.qid, certificate.qid]
    )
    return qids
