"""One client for the query service: every query travels in a POST body.

The query service refuses a request line longer than about 8 KB (HTTP
414), so a query sent in the URL of a GET fails once its text grows past
that. A form-encoded POST body has no such limit, so readers send every
query that way, whatever its length (ADR-0033).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from yizkr.core.config import USER_AGENT, Config

# A runner takes a query's text and returns its result rows: one dict per
# row, each variable bound to {"type", "value", "datatype"/"xml:lang"}, as
# the SPARQL 1.1 JSON results format gives them.
Runner = Callable[[str], list[dict]]

DEFAULT_TIMEOUT = 120.0


class SparqlError(RuntimeError):
    """The query service refused a query; the message carries its answer."""


def select(
    url: str, query: str, timeout: float = DEFAULT_TIMEOUT
) -> list[dict]:
    """Run one SELECT query at the endpoint `url`; its result rows."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(
            {"query": query, "format": "json"}
        ).encode(),
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)["results"]["bindings"]
    except urllib.error.HTTPError as error:
        # the engine explains a refused query in the body (a parse error,
        # a timeout); keep its first lines
        detail = error.read(2000).decode("utf-8", errors="replace").strip()
        raise SparqlError(
            f"the query service at {url} answered HTTP {error.code}: {detail}"
        ) from error


def runner(config: Config, timeout: float = DEFAULT_TIMEOUT) -> Runner:
    """A runner for the query service `config` names."""
    if not config.sparql_url:
        raise SparqlError(
            "no query service configured: set WDQS_PUBLIC_HOST (or "
            "WDQS_SPARQL_URL)"
        )
    url = config.sparql_url

    def run(query: str) -> list[dict]:
        return select(url, query, timeout)

    return run
