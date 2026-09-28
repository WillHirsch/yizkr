"""Fixtures for live integration tests (pytest -m live / make test-live).

These tests need the deploy/ stack running and deploy/.env present;
they skip with an explanation otherwise. They write to the instance
using the same deterministic labels on every run, so re-runs reuse
their entities — that reuse IS part of what they verify.

A session that uses the stack holds the live-stack lock (live_lock.py)
from its first stack test to its end, so live sessions take turns on
the one instance.
"""

from __future__ import annotations

import time

import pytest
import requests

from yizkr.core.config import USER_AGENT, Config
from yizkr.core.wiki import WikiClient

from .live_lock import hold_for_session


@pytest.fixture(scope="session")
def live_stack_lock(request):
    """The live-stack lock, held to the end of the session. The config
    fixture takes it, so every test that uses the stack waits its turn;
    tests that need only the claude CLI (test_live_chunked) do not."""
    with hold_for_session(request.config):
        yield


@pytest.fixture(scope="session")
def config(request) -> Config:
    try:
        config = Config.load()
    except SystemExit as exc:
        pytest.skip(f"no connection configuration: {exc}")
    # taken once the session knows which stack it uses, before it first
    # contacts it
    request.getfixturevalue("live_stack_lock")
    try:
        requests.get(
            f"{config.wikibase_url}/w/api.php",
            params={"action": "query", "meta": "siteinfo", "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as exc:
        pytest.skip(
            f"Wikibase stack not reachable at {config.wikibase_url} "
            f"(start it with `make up`): {exc}"
        )
    return config


@pytest.fixture(scope="session")
def client(config: Config) -> WikiClient:
    return WikiClient(config)


def run_sparql(config: Config, query: str) -> list[dict]:
    response = requests.get(
        config.sparql_url,
        params={"query": query},
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": USER_AGENT,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def wait_for_query_service(
    config: Config,
    client: WikiClient,
    qids: list[str],
    timeout: float = 180.0,
) -> None:
    """Block until WDQS holds each entity's CURRENT revision.

    The query service follows MediaWiki's recent changes asynchronously,
    so a query run straight after a write can read the previous graph.
    Comparing the triple store's ``schema:version`` with the API's
    ``lastrevid`` is an exact barrier: no fixed sleep, and no false pass
    against a store that has not caught up yet.
    """
    info = client.api_get(
        {"action": "wbgetentities", "ids": "|".join(qids), "props": "info"}
    )
    wanted = {qid: info["entities"][qid]["lastrevid"] for qid in qids}
    entities = " ".join(f"<{config.concept_uri}/entity/{qid}>" for qid in qids)
    query = (
        "PREFIX schema: <http://schema.org/>\n"
        "SELECT ?entity ?version WHERE {\n"
        f"  VALUES ?entity {{ {entities} }}\n"
        "  ?entity schema:version ?version .\n"
        "}"
    )
    deadline = time.monotonic() + timeout
    while True:
        seen = {
            row["entity"]["value"].rsplit("/", 1)[-1]: int(
                row["version"]["value"]
            )
            for row in run_sparql(config, query)
        }
        stale = sorted(
            qid
            for qid, revision in wanted.items()
            if seen.get(qid, -1) < revision
        )
        if not stale:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"WDQS did not catch up with {stale} within {timeout}s; "
                f"check the wdqs-updater logs"
            )
        time.sleep(2)
