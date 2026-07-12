#!/usr/bin/env python3
"""Render the SPARQL query library and publish it to wiki pages.

The checked-in ``*.rq`` files are templates: they reference the model
vocabulary by manifest key (``wdt:{P:instance_of}``, ``wd:{Q:human}``)
because local P/Q ids are per-instance and never live in this repo.
This script resolves the keys against the target instance (same
label-matching machinery the pipeline uses) and publishes each query to
``Project:Queries/<name>`` with the rendered text, a "run" link into
the query service UI, and an embeddable result view link (ADR-0004 W1
exploration tier), plus a ``Project:Queries`` index page.

Idempotent: pages are fully regenerated on every run.

Usage:
    python3 publish.py [--env-file ../deploy/.env] [--dry-run]

--dry-run renders every query and prints where each would go, without
touching the wiki. Rendering fails loudly on unknown manifest keys.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.wiki import (  # noqa: E402
    DEFAULT_ENV_FILE,
    WikiClient,
    WikiConfig,
    read_env_file,
)

QUERY_DIR = Path(__file__).resolve().parent
INDEX_PAGE = "Project:Queries"
PLACEHOLDER = re.compile(r"\{([PQ]):([a-z][a-z0-9_]*)\}")


def load_templates(directory: Path = QUERY_DIR) -> dict[str, str]:
    return {
        path.stem: path.read_text()
        for path in sorted(directory.glob("*.rq"))
    }


def placeholder_keys(template: str) -> tuple[set[str], set[str]]:
    """(property keys, item keys) referenced by a template."""
    properties, items = set(), set()
    for kind, key in PLACEHOLDER.findall(template):
        (properties if kind == "P" else items).add(key)
    return properties, items


def render(template: str, properties: dict[str, str],
           items: dict[str, str]) -> str:
    def substitute(match: re.Match) -> str:
        kind, key = match.groups()
        table = properties if kind == "P" else items
        if key not in table:
            raise KeyError(
                f"template references unknown manifest "
                f"{'property' if kind == 'P' else 'item'} key {key!r}"
            )
        return table[key]

    return PLACEHOLDER.sub(substitute, template)


def prefix_block(concept_uri: str) -> str:
    """Explicit prefixes bound to THIS instance's concept URI.

    The WDQS build ships built-in wd:/wdt:/... prefixes pointing at
    wikidata.org; queries against a self-hosted instance must redeclare
    them, or every pattern silently matches nothing.
    """
    return "\n".join(
        [
            f"PREFIX wd: <{concept_uri}/entity/>",
            f"PREFIX wdt: <{concept_uri}/prop/direct/>",
            f"PREFIX p: <{concept_uri}/prop/>",
            f"PREFIX ps: <{concept_uri}/prop/statement/>",
            f"PREFIX pq: <{concept_uri}/prop/qualifier/>",
            f"PREFIX pr: <{concept_uri}/prop/reference/>",
            "PREFIX wikibase: <http://wikiba.se/ontology#>",
            "PREFIX prov: <http://www.w3.org/ns/prov#>",
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
            "PREFIX bd: <http://www.bigdata.com/rdf#>",
            "",
        ]
    )


def render_for_instance(template: str, properties: dict[str, str],
                        items: dict[str, str], concept_uri: str) -> str:
    return prefix_block(concept_uri) + render(template, properties, items)


def description_of(template: str) -> str:
    """Leading comment block, minus display directives like #defaultView."""
    lines = []
    for line in template.splitlines():
        if not line.startswith("#"):
            break
        if line.startswith("#defaultView"):
            continue
        lines.append(line.lstrip("# ").rstrip())
    return " ".join(lines).strip()


def _env_get(env_file: Path, key: str, default: str = "") -> str:
    import os

    return os.environ.get(key) or read_env_file(env_file).get(key) or default


def wdqs_frontend_url(env_file: Path) -> str:
    host = _env_get(env_file, "WDQS_PUBLIC_HOST")
    if not host:
        sys.exit(f"WDQS_PUBLIC_HOST is not set (looked in {env_file})")
    scheme = _env_get(env_file, "PUBLIC_SCHEME", "http")
    port = _env_get(env_file, "HTTP_PORT", "80")
    suffix = "" if port in ("80", "") or scheme == "https" else f":{port}"
    return f"{scheme}://{host}{suffix}"


def concept_uri(env_file: Path) -> str:
    """Entity URI base: matches MW_WG_SERVER / WIKIBASE_CONCEPT_URI (no port)."""
    host = _env_get(env_file, "WIKIBASE_PUBLIC_HOST")
    if not host:
        sys.exit(f"WIKIBASE_PUBLIC_HOST is not set (looked in {env_file})")
    scheme = _env_get(env_file, "PUBLIC_SCHEME", "http")
    return f"{scheme}://{host}"


def page_title(name: str) -> str:
    return f"{INDEX_PAGE}/{name}"


def page_wikitext(name: str, template: str, rendered: str,
                  frontend: str) -> str:
    encoded = urllib.parse.quote(rendered, safe="")
    return "\n".join(
        [
            f"''Generated by <code>queries/publish.py</code> from "
            f"<code>queries/{name}.rq</code> — do not edit by hand; re-run "
            f"the publisher to refresh.''",
            "",
            description_of(template),
            "",
            f"'''[{frontend}/#{encoded} ▶ Run this query]''' · "
            f"[{frontend}/embed.html#{encoded} embeddable result view]",
            "",
            '<syntaxhighlight lang="sparql">',
            rendered.rstrip(),
            "</syntaxhighlight>",
            "",
        ]
    )


def index_wikitext(templates: dict[str, str]) -> str:
    lines = [
        "''Generated by <code>queries/publish.py</code> — do not edit by "
        "hand.'' The query library lives in the project repository under "
        "<code>queries/</code>; queries reference the model vocabulary "
        "resolved for this instance (see [[Project:Model mapping]]).",
        "",
    ]
    for name, template in templates.items():
        lines.append(f"* [[{page_title(name)}|{name}]] — "
                     f"{description_of(template)}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    templates = load_templates()
    if not templates:
        sys.exit(f"no *.rq templates found in {QUERY_DIR}")
    client = WikiClient(WikiConfig.load(args.env_file))
    properties, items = client.vocabulary()
    frontend = wdqs_frontend_url(args.env_file)
    concept = concept_uri(args.env_file)

    for name, template in templates.items():
        rendered = render_for_instance(template, properties, items, concept)
        if args.dry_run:
            print(f"--- {page_title(name)} (dry run) ---")
            print(rendered)
            continue
        client.edit_page(
            page_title(name),
            page_wikitext(name, template, rendered, frontend),
            summary="query library: refresh from repo",
        )
        print(f"published {page_title(name)}")

    if not args.dry_run:
        client.edit_page(
            INDEX_PAGE,
            index_wikitext(templates),
            summary="query library: refresh index",
        )
        print(f"published {INDEX_PAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
