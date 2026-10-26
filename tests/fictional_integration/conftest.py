"""Fail-closed configuration for the dedicated fictional Compose lane.

These fixtures never load deploy/.env or the optional live-suite fixtures.
The launcher supplies a fresh isolated network, volumes, settings and lock;
there are no endpoint overrides pointing at an existing instance.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from yizkr.core.config import Config
from yizkr.core.instance import clear_contents
from yizkr.core.wiki import WikiClient
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest


@pytest.fixture(scope="session")
def fictional_client():
    required = {
        "YIZKR_FICTIONAL_INTEGRATION": "1",
        "MW_ADMIN_PASS": "fictional-integration-only",
        "YIZKR_INSTANCE_STATE": "/state/instances",
        "YIZKR_LIVE_LOCK": "/state/live.lock",
    }
    invalid = [
        key for key, value in required.items() if os.getenv(key) != value
    ]
    if invalid:
        pytest.fail(
            "Required fictional integration setup is unavailable: "
            + ", ".join(invalid)
            + ". Run make test-integration; this test cannot skip or use "
            "an existing deployment.",
            pytrace=False,
        )
    config = Config(
        wikibase_url="http://wikibase",
        concept_uri="http://wikibase",
        admin_name="FictionalAdmin",
        admin_pass=required["MW_ADMIN_PASS"],
        wdqs_url="http://wdqs:9999",
        sparql_url="http://wdqs:9999/bigdata/namespace/wdq/sparql",
        env_file=None,
    )
    client = WikiClient(config)
    # Explicit state root: this lane never invalidates an archive's record.
    root = Path(required["YIZKR_INSTANCE_STATE"])
    lock_path = Path(required["YIZKR_LIVE_LOCK"])
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail(
                "The fictional stack is already in use by another "
                "test process; no writes were attempted.",
                pytrace=False,
            )
        clear_contents(config, "fictional integration", root=root)
        try:
            client.api_get({"action": "query", "meta": "siteinfo"})
            bootstrap_instance(client, load_manifest())
            yield client
        finally:
            clear_contents(config, "fictional integration", root=root)
