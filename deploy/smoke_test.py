#!/usr/bin/env python3
"""Smoke test for the Wikibase Suite deployment.

Verifies end to end that:
  1. The MediaWiki/Wikibase API is up and answering.
  2. An item can be created through the API (authenticated write path).
  3. The item becomes queryable through the WDQS SPARQL endpoint, i.e. the
     wdqs-updater round-trip works.

Safe to re-run (idempotent): the first run creates one marker item; later
runs find and reuse it rather than accumulating junk. The item carries only
a label and description - no statements are written, so no reference is
required (CLAUDE.md rule 1 concerns statements).

Configuration is read from the environment, falling back to an env file
(default: .env next to this script). Uses the same variables as the
deployment: WIKIBASE_PUBLIC_HOST, WDQS_PUBLIC_HOST, PUBLIC_SCHEME,
HTTP_PORT, MW_ADMIN_NAME, MW_ADMIN_PASS. Endpoints can be overridden
directly with WIKIBASE_URL and WDQS_SPARQL_URL.

Usage:
    python3 smoke_test.py [--env-file PATH] [--timeout SECONDS]

Exit code 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

SMOKE_LABEL = "Wikibase Suite smoke-test item"
SMOKE_DESCRIPTION = (
    "Created by deploy/smoke_test.py to verify item creation and the "
    "SPARQL round-trip. Safe to delete."
)
USER_AGENT = "yizkr-smoke-test/1.0 (deploy/smoke_test.py)"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser matching the .env format used by compose."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def build_config(env_file: Path) -> dict[str, str]:
    file_values = read_env_file(env_file)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default

    scheme = get("PUBLIC_SCHEME", "http")
    wikibase_host = get("WIKIBASE_PUBLIC_HOST")
    wdqs_host = get("WDQS_PUBLIC_HOST")
    port = get("HTTP_PORT", "80")
    port_suffix = "" if port in ("80", "") or scheme == "https" else f":{port}"

    config = {
        "wikibase_url": get("WIKIBASE_URL")
        or f"{scheme}://{wikibase_host}{port_suffix}",
        "sparql_url": get("WDQS_SPARQL_URL")
        or f"{scheme}://{wdqs_host}{port_suffix}/sparql",
        # The concept URI never includes the port: it is an identifier
        # prefix, baked into the triples as scheme://host/entity/QID.
        "concept_base": f"{scheme}://{wikibase_host}",
        "admin_name": get("MW_ADMIN_NAME"),
        "admin_pass": get("MW_ADMIN_PASS"),
    }
    missing = [k for k, v in config.items() if not v]
    if wikibase_host == "" or wdqs_host == "":
        missing.append("WIKIBASE_PUBLIC_HOST/WDQS_PUBLIC_HOST")
    if missing:
        sys.exit(
            f"Missing configuration: {missing}. Set the variables in the "
            f"environment or in {env_file} (copy .env.example)."
        )
    return config


def step(n: int, total: int, message: str) -> None:
    print(f"[{n}/{total}] {message}", flush=True)


def check_api(wikibase_url: str) -> None:
    response = requests.get(
        f"{wikibase_url}/w/api.php",
        params={"action": "query", "meta": "siteinfo", "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    sitename = response.json()["query"]["general"]["sitename"]
    print(f"      MediaWiki API is up (sitename: {sitename})")


def find_existing_item(wikibase_url: str) -> str | None:
    """Return the QID of a previous run's marker item, if any."""
    response = requests.get(
        f"{wikibase_url}/w/api.php",
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


def create_item(wikibase_url: str, sparql_url: str, config: dict) -> str:
    from wikibaseintegrator import WikibaseIntegrator, wbi_login
    from wikibaseintegrator.wbi_config import config as wbi_config

    wbi_config["MEDIAWIKI_API_URL"] = f"{wikibase_url}/w/api.php"
    wbi_config["SPARQL_ENDPOINT_URL"] = sparql_url
    wbi_config["WIKIBASE_URL"] = config["concept_base"]
    wbi_config["USER_AGENT"] = USER_AGENT

    login = wbi_login.Clientlogin(
        user=config["admin_name"], password=config["admin_pass"]
    )
    wbi = WikibaseIntegrator(login=login)
    item = wbi.item.new()
    item.labels.set(language="en", value=SMOKE_LABEL)
    item.descriptions.set(language="en", value=SMOKE_DESCRIPTION)
    item.write()
    return item.id


def wait_for_sparql(sparql_url: str, entity_uri: str, timeout: int) -> float:
    """Poll WDQS until the entity is visible; return the elapsed seconds."""
    query = f"ASK {{ <{entity_uri}> ?p ?o }}"
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(
                sparql_url,
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
        f"Entity {entity_uri} did not appear on {sparql_url} within "
        f"{timeout}s (last status: {last_error}). Check the wdqs-updater "
        f"logs: docker compose logs wdqs-updater"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).parent / ".env",
        help="env file to read configuration from (default: deploy/.env)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait for the WDQS round-trip (default: 300)",
    )
    args = parser.parse_args()
    config = build_config(args.env_file)
    wikibase_url = config["wikibase_url"]
    sparql_url = config["sparql_url"]
    print(f"Wikibase: {wikibase_url}")
    print(f"SPARQL:   {sparql_url}")

    step(1, 4, "Checking the MediaWiki/Wikibase API ...")
    check_api(wikibase_url)

    step(2, 4, "Looking for a marker item from a previous run ...")
    qid = find_existing_item(wikibase_url)
    if qid:
        print(f"      Found {qid} (reusing; this run verifies the read path)")
    else:
        print("      None found")

    step(3, 4, "Creating the marker item ..." if not qid else "Item exists, skipping creation")
    if not qid:
        qid = create_item(wikibase_url, sparql_url, config)
        print(f"      Created item {qid}")

    entity_uri = f"{config['concept_base']}/entity/{qid}"
    step(4, 4, f"Waiting for {qid} to appear in WDQS (SPARQL round-trip) ...")
    elapsed = wait_for_sparql(sparql_url, entity_uri, args.timeout)
    print(f"      Visible in WDQS after {elapsed:.0f}s")

    print(f"\nSMOKE TEST PASSED - item {qid} readable via API and SPARQL.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (requests.RequestException, TimeoutError) as exc:
        print(f"\nSMOKE TEST FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
