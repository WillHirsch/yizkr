"""Fixtures for live integration tests (pytest -m live / make test-live).

These tests need the deploy/ stack running and deploy/.env present;
they skip with an explanation otherwise.

The instance is reset only per scratch run, and statements are never
removed, so a live test meets whatever earlier sessions wrote, a live
mutant's run included (#430). Every item a test writes carries a
marker of its session's own (`fixture_mark`) in its label, title, file
name or media bytes, so each session meets its fixture as a first
write, and a rewrite within the session finds its own items again: that
reuse IS part of what they verify.

A session that uses the stack holds the live-stack lock (live_lock.py)
from its first stack test to its end, so live sessions take turns on
the one instance.

The session writes to the instance, and the scratch write stage does not
make those writes, so the session clears the recorded instance-contents
key (yizkr.core.instance) once it holds the lock, and again as it ends:
the next scratch run resets the instance first (#199).

A claim cites its artifact by the SHA-1 of the artifact's media (#102),
so every artifact a test's claims cite is registered with media of its
own: `register_fictional` gives it a small PNG whose bytes its title
decides, so a re-run registers the same bytes as the same artifact, and
a title carrying the session's marker gives the session bytes of its own.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
import requests

from yizkr.claims import ArtifactRef
from yizkr.core import sparql
from yizkr.core.config import USER_AGENT, Config
from yizkr.core.instance import clear_contents
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.registrar import register_artifact

from .live_lock import hold_for_session

# who a live session's clearing of the instance-contents record names
LIVE_TESTS = "live tests"

# eight random bytes drawn once a session: a mutation harness's run of a
# mutant is a session of its own, so it never writes onto the items a
# later session reads
SESSION_BYTES = os.urandom(8)


def fixture_mark(fixture: str, *modules: ModuleType) -> str:
    """Eight hex digits of a hash over `fixture`, a sentence saying what a
    test's fixture is, over the source of `modules`, the code under test,
    and over this session's random bytes. A test puts it in every label,
    title, file name and media byte its items are found by, so each
    session, and each version of the fixture and of that code, meets the
    fixture as a first write: what an earlier session wrote, a live
    mutant's run included, stays on items no later session finds. Within
    the session a rewrite finds its own items again."""
    return hashlib.sha256(
        fixture.encode()
        + b"".join(Path(module.__file__).read_bytes() for module in modules)
        + SESSION_BYTES
    ).hexdigest()[:8]


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
    # before any test writes, and again at the session's end, while the
    # lock is still held
    clear_contents(config, LIVE_TESTS)
    request.addfinalizer(lambda: clear_contents(config, LIVE_TESTS))
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
    # through the client the site uses, which sends the query in the
    # request body: the query service refuses a request line longer than
    # about 8 KB
    return sparql.select(config.sparql_url, query)


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


def fictional_png(marker: str) -> bytes:
    """A valid 4x4 PNG whose text chunk makes its bytes, and so the
    content key of the artifact they are registered as (ADR-0010), this
    marker's alone: the same marker always gives the same bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    pixels = b"".join(b"\x00" + b"\x5c\x7a\x3e" * 4 for _ in range(4))
    text = marker.encode("ascii", "backslashreplace")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + text)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class Registered:
    """A fictional artifact on the instance: its item, and the key its
    claims cite it by, the SHA-1 of its media."""

    qid: str
    ref: ArtifactRef
    fragment_qids: dict[int, str] = field(default_factory=dict)


def register_fictional(
    client: WikiClient, title: str, artifact_type: str, pages: int = 0
) -> Registered:
    """Register a fictional artifact titled `title`, with a PNG of its own
    as its media (fictional_png), and optionally `pages` page fragments.
    Idempotent: the same title gives the same bytes, and so the same
    item."""
    media = fictional_png(f"yizkr live test: {title}")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{slug or 'artifact'}.png"
        path.write_bytes(media)
        result = register_artifact(
            client,
            media_path=path,
            artifact_type=artifact_type,
            title=title,
            description="written by tests/integration; safe to delete",
            pages=pages,
        )
    return Registered(
        result.artifact_qid,
        ArtifactRef.from_bytes(media),
        result.fragment_qids,
    )
