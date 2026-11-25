"""The assignment path's cost on a fresh fictional stack, measured on demand.

An opt-in measurement (`scripts/fictional_integration.py run --measure`),
never a required case, on a stack of its own: the benchmark's smallest
cohort goes through the real Wikibase and query service, every request
counted at the transport by the kinds the offline benchmark counts, each
operation and each wait for the query service timed. The same cohort is
then measured against the in-memory fake in this process. The JUnit suite
property `assignment_cost` keeps both reports and, for each operation, the
request and write kinds whose counts differ between them, so the offline
counts can be checked and scaled (docs/TESTING.md, "Assignment path
cost"). Readings are authored fiction; no model is called.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.fixtures.assignment_cohort import (
    REGRESSION,
    generate,
    measure,
    operation_counts,
    report,
    run_cohort,
    saved_products,
)
from tests.fixtures.fake_wikibase import RequestLog, observing
from yizkr.backup import REVISIONS_QUERY
from yizkr.core import sparql
from yizkr.core.wiki import WikiClient

pytestmark = pytest.mark.live
# Bounds one wait for the query service; never a delay under measurement.
HANG_GUARD = 300
# How often a wait asks the query service again.
POLL_SECONDS = 0.25


def wait_for_index(client: WikiClient) -> None:
    """Wait until the query service's revision index is the API's entity
    revisions exactly, as a graph capture requires."""
    run = sparql.runner(client.config)
    prefix = client.config.concept_uri + "/entity/"
    deadline = time.monotonic() + HANG_GUARD
    while True:
        wanted = client.entity_revisions(client.list_entity_ids())
        indexed = {
            row["entity"]["value"].removeprefix(prefix): int(
                row["version"]["value"]
            )
            for row in run(REVISIONS_QUERY)
        }
        if indexed == wanted:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the query service did not index {len(wanted)} entities "
                f"within {HANG_GUARD}s; check the fictional updater's logs"
            )
        time.sleep(POLL_SECONDS)


def differences(live: dict, offline: dict) -> list[dict]:
    """Each operation's request and write kinds whose counts differ
    between the two reports, as (live, offline) pairs."""
    found = []
    pairs = zip(
        operation_counts(live), operation_counts(offline), strict=False
    )
    for index, (real, fake) in enumerate(pairs):
        for part in ("requests", "writes"):
            kinds = sorted(set(real[part]) | set(fake[part]))
            moved = {
                kind: [real[part].get(kind, 0), fake[part].get(kind, 0)]
                for kind in kinds
                if real[part].get(kind) != fake[part].get(kind)
            }
            if moved:
                found.append(
                    {
                        "operation": index,
                        "kind": real["operation"],
                        "part": part,
                        "counts": moved,
                    }
                )
    return found


def test_assignment_path_cost_on_a_fresh_stack(
    fictional_client, tmp_path, record_testsuite_property
):
    cohort = generate(REGRESSION)
    saved = saved_products(cohort, tmp_path / "products")
    client = WikiClient(fictional_client.config)
    entities = len(client.list_entity_ids())
    log = RequestLog()
    with observing(log, client.config):
        run = run_cohort(
            saved,
            cohort=cohort,
            client=client,
            workspace=tmp_path / "workspace",
            log=log,
            synchronize=lambda: wait_for_index(client),
            clock=time.perf_counter,
            setup_entities=entities,
        )
    run.setup = {"entities": entities}
    live = report(run)
    offline = measure(REGRESSION, tmp_path / "offline")
    record_testsuite_property(
        "assignment_cost",
        json.dumps(
            {
                "live": live,
                "offline": offline,
                "differences": differences(live, offline),
            },
            sort_keys=True,
        ),
    )
    # the wall times the measurement exists to give, waits apart
    assert all("seconds" in operation for operation in live["operations"])
    assert live["sync_seconds_per_identity"] >= 0
    assert live["phases"]["claim-apply"]["writes"].get("item-update")
