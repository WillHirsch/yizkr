"""Smoke test for a Wikibase Suite deployment (deploy/README.md).

Verifies end to end that the MediaWiki/Wikibase API answers, an item
can be created through the API, and the item becomes queryable through
the WDQS SPARQL endpoint (the wdqs-updater round-trip).

Idempotent: re-runs reuse the marker item created on the first run.
The item carries only a label and description — no statements are
written, so no reference is required (the ADR-0003 reference
requirement concerns statements).

CLI: `yizkr smoke-test [--timeout SECONDS]`.
"""

from __future__ import annotations

import time

import requests

from yizkr.core.config import USER_AGENT, Config

SMOKE_LABEL = "Wikibase Suite smoke-test item"
SMOKE_DESCRIPTION = (
    "Created by `yizkr smoke-test` to verify item creation and the "
    "SPARQL round-trip. Safe to delete."
)


def check_api(config: Config) -> str:
    response = requests.get(
        f"{config.wikibase_url}/w/api.php",
        params={"action": "query", "meta": "siteinfo", "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["query"]["general"]["sitename"]


def find_existing_item(config: Config) -> str | None:
    """Return the QID of a previous run's marker item, if any."""
    response = requests.get(
        f"{config.wikibase_url}/w/api.php",
        params={
            "action": "wbsearchentities",
            "search": SMOKE_LABEL,
            "language": "en",
            "type": "item",
            "format": "json",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    for result in response.json().get("search", []):
        if result.get("label") == SMOKE_LABEL:
            return result["id"]
    return None


def create_item(config: Config) -> str:
    from wikibaseintegrator import WikibaseIntegrator, wbi_login
    from wikibaseintegrator.wbi_config import config as wbi_config

    wbi_config["MEDIAWIKI_API_URL"] = f"{config.wikibase_url}/w/api.php"
    wbi_config["USER_AGENT"] = USER_AGENT
    login = wbi_login.Clientlogin(
        user=config.admin_name, password=config.admin_pass
    )
    wbi = WikibaseIntegrator(login=login)
    item = wbi.item.new()
    item.labels.set(language="en", value=SMOKE_LABEL)
    item.descriptions.set(language="en", value=SMOKE_DESCRIPTION)
    item.write()
    return item.id


def wait_for_sparql(config: Config, entity_uri: str, timeout: int) -> float:
    """Poll WDQS until the entity is visible; return the elapsed seconds."""
    query = f"ASK {{ <{entity_uri}> ?p ?o }}"
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(
                config.sparql_url,
                params={"query": query},
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
            response.raise_for_status()
            if response.json().get("boolean"):
                return time.monotonic() - start
            last_error = "entity not yet in the triple store"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(5)
    raise TimeoutError(
        f"Entity {entity_uri} did not appear on {config.sparql_url} within "
        f"{timeout}s (last status: {last_error}). Check the wdqs-updater "
        f"logs: docker compose logs wdqs-updater"
    )


def run_smoke(config: Config, timeout: int = 300, log=print) -> str:
    """Run the smoke test; returns the marker QID or raises."""
    if not config.sparql_url:
        raise SystemExit(
            "WDQS_PUBLIC_HOST (or WDQS_SPARQL_URL) is not configured; the "
            "smoke test needs the SPARQL endpoint."
        )
    log(f"Wikibase: {config.wikibase_url}")
    log(f"SPARQL:   {config.sparql_url}")

    log("[1/4] Checking the MediaWiki/Wikibase API ...")
    log(f"      MediaWiki API is up (sitename: {check_api(config)})")

    log("[2/4] Looking for a marker item from a previous run ...")
    qid = find_existing_item(config)
    log(f"      Found {qid} (reusing)" if qid else "      None found")

    if qid:
        log("[3/4] Item exists, skipping creation")
    else:
        log("[3/4] Creating the marker item ...")
        qid = create_item(config)
        log(f"      Created item {qid}")

    entity_uri = f"{config.concept_uri}/entity/{qid}"
    log(f"[4/4] Waiting for {qid} to appear in WDQS (SPARQL round-trip) ...")
    elapsed = wait_for_sparql(config, entity_uri, timeout)
    log(f"      Visible in WDQS after {elapsed:.0f}s")

    log(f"\nSMOKE TEST PASSED - item {qid} readable via API and SPARQL.")
    return qid
