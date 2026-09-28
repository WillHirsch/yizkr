"""One client for the query service: every query travels in a POST body.

The query service refuses a request line longer than about 8 KB (HTTP
414), so a query sent in the URL of a GET fails once its text grows past
that. A form-encoded POST body has no such limit, so readers send every
query that way, whatever its length (ADR-0033).
"""

from __future__ import annotations

import json
import re
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

# the most of the engine's answer a SparqlError carries
DETAIL_LIMIT = 2000
# a line that opens with a Java exception's class name: the engine's own
# explanation of a refused query
_THROWN = re.compile(r"^(?:[\w$]+\.)*[\w$]*(?:Exception|Error)\b.*$", re.M)


class SparqlError(RuntimeError):
    """The query service refused a query; the message carries its answer."""


def refusal(body: str) -> str:
    """What the engine says about a query it refused. The query service
    answers with the whole query echoed first (``SPARQL-QUERY:
    queryStr=...``) and its explanation after it, a Java exception such as
    a MalformedQueryException or a TimeoutException, so that exception's
    line is kept whatever the query's length. An answer that names no
    exception (a proxy's error page, say) is kept from its start."""
    thrown = _THROWN.search(body)
    detail = thrown.group(0) if thrown else body
    return detail.strip()[:DETAIL_LIMIT]


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
        detail = refusal(error.read().decode("utf-8", errors="replace"))
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
