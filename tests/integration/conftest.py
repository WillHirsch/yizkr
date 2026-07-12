"""Fixtures for live integration tests (pytest -m live / make test-live).

These tests need the deploy/ stack running and deploy/.env present;
they skip with an explanation otherwise. They write to the instance
using the same deterministic labels on every run, so re-runs reuse
their entities — that reuse IS part of what they verify.
"""

from __future__ import annotations

import pytest
import requests

from yizkr.core.config import USER_AGENT, Config
from yizkr.core.wiki import WikiClient


@pytest.fixture(scope="session")
def config() -> Config:
    try:
        config = Config.load()
    except SystemExit as exc:
        pytest.skip(f"no connection configuration: {exc}")
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
