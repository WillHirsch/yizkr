"""Offline tests for the run report (#124): `yizkr scratch report`, what a
run did per stage and per document, read from its run directory alone.

Most tests read one fictional scratch run over documents of the example
universe (docs/EXAMPLES.md), recorded as its driver would have written
it: three invocations (the first paused for the subscription, the second
stopped by an abort of a document kind, the third finished past the
barrier with the clustering stage), each document's views, the anomaly
alerts, the control requests and the clustering stage's identity map.
Every free-text field (file names, paths, labels, excerpts, reasons,
alert fields, the identity map's labels) carries names from the universe
(NAMES), and report.md holds none of them. A run of the real scratch
driver, with its fake engine behind the real LLM cache
(tests/unit/test_scratch.py), checks that the report reads what the
driver writes.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import random
import re
import shutil
import tracemalloc
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_extraction_records import (
    HILDEGARD,
    SEBASTIAN,
    extract,
)
from tests.unit.test_no_wiki_before_write import refusals  # noqa: F401
from tests.unit.test_scratch import (  # noqa: F401
    LETTER,
    PHOTO,
    STONE,
    TEXT,
    Driver,
    a_run,
    cli_run,
    fakes,
    runs_dir,
    small,
)
from yizkr.claims import (
    EntityRecord,
    _clear_empty,
    set_aside_structure,
    structure_problems,
)
from yizkr.pipeline import llm, report
from yizkr.pipeline.chunking import _set_aside_note
from yizkr.pipeline.monitor import RunMonitor
from yizkr.pipeline.orchestrate import slugify
from yizkr.pipeline.report import (
    COMPUTED,
    HELD,
    NOT_AVAILABLE,
    NOT_RUN,
    OTHER,
    REPORT_DIR,
    REUSED,
    ReportError,
    build,
    render_markdown,
    write_report,
)
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    INTERRUPTED,
    NEEDS_ATTENTION,
    PENDING,
    RUNNING,
    RunDir,
    run_state,
)

SOURCES = {source.key: source for source in u.SOURCES}


def _sha1(data: str | bytes) -> str:
    return hashlib.sha1(
        data.encode() if isinstance(data, str) else data
    ).hexdigest()


CERT = _sha1(SOURCES["marriage-cert-1905"].text)
POSTCARD = _sha1(SOURCES["postcard-1938"].text)
DIRECTORY = _sha1(SOURCES["directory-1912"].text)
CARD = _sha1(SOURCES["registration-card-1945"].text)
INTERVIEW = _sha1(SOURCES["interview-2002"].text)
DECK = _sha1("a fictional deck: Hutmacherei zum Blauen Hut")
TREE = _sha1(u.GEDCOM_FIXTURE.read_bytes())

FILES = {
    CERT: ("Hildegard Laudler marriage 1905.jpg", "media"),
    DIRECTORY: ("Flörburg address book 1912.txt", "text"),
    DECK: ("Hutmacherei zum Blauen Hut.pptx", "presentation"),
    TREE: ("Löbig family tree.ged", "gedcom"),
    POSTCARD: ("Postcard Sebastian Löbig to Klara 1938.jpg", "media"),
    CARD: ("Ida Sörne registration card 1945.txt", "text"),
    INTERVIEW: ("Nicholas Lobig interview Stupfield 2002.txt", "text"),
}
# a second file with the directory's bytes, and a source that could not
# be read, so has no SHA-1 and is never planned
COPY = "Flörburg address book 1912 copy.txt"
UNREAD = "Maisie Cutmore letters from Stupfield.pdf"
EXTRACTED = ["transcription", "extraction", "critique"]

RUN_ID = "20260929T090000Z-0a1b2c"
FIRST, SECOND, THIRD = "a1b2c3d4e5f6", "0f1e2d3c4b5a", "5a4b3c2d1e0f"
T0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=3)
CORPUS = "/archive/Löbig papers from Flörburg"
CACHE = "/srv/Peiltal cache"
EXCLUSIONS = "/private/Klurnau never-ingest.txt"
EXCLUSIONS_SHA256 = hashlib.sha256(b"a fictional list").hexdigest()

# what no aggregate may say: the universe's names as documents and
# reasons carry them, their ASCII spellings, and the paths run.json names
NAMES = (
    "Löbig",
    "Lobig",
    "Loebig",
    "Flörburg",
    "Floerburg",
    "Fl-rburg",
    "Laudler",
    "Hildegard",
    "Ursula",
    "Wwe",
    "Brunnenring",
    "Angerring",
    "Hutmacherei",
    "Blauen",
    "Peiltal",
    "Klurnau",
    "Stupfield",
    "Hawthorn",
    "Nicholas",
    "Sörne",
    "Soerne",
    "Maisie",
    "Cutmore",
    "Sebastian",
    "Klara",
    "Eugen",
    "Rekau",
    CORPUS,
    CACHE,
    EXCLUSIONS,
    EXCLUSIONS_SHA256[:12],
)

FALLBACK = (
    "entity manifest fell back to line-range segmentation: "
    "input_value='Löbig, Ursula, Wwe., Angerring 57'"
)
NO_CLAIMS = (
    "no claims extracted: every entity in the manifest is extract=false "
    "(Ida Sörne, Peiltal)"
)
DECK_FAILURE = (
    "ExtractionFailed: no claims extracted: validation rejected every "
    "claim (4); the entity manifest was refused (every attempt: "
    "'Hutmacherei zum Blauen Hut, gegr. 1863')"
)
INTERVIEW_FAILURE = (
    "RuntimeError: 2 chunks failed after retries (c1, c2; c1: Nicholas "
    "Lobig, 8 Hawthorn Road, Stupfield)"
)
UNREADABLE = (
    "its source cannot be read (PermissionError: [Errno 13] Permission "
    f"denied: '{UNREAD}')"
)
CHECK_FAILED = (
    "transcription check v2 failed (tool-failure): 'Löbig, Ursula, Wwe., "
    "Flörburg' (signature read-error: Eugen Löbig)"
)
ABORT = "the Brunnenring 35 deck looks wrong"
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5"
USED = {"input_tokens": 500, "output_tokens": 100}
CLUSTERED = [
    "ambiguous clusters not merged: 1 cluster of 2 mentions",
    "conflicting clusters not merged: Klurnau and Rekau",
]


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def event(seconds, name, invocation, **fields):
    return (
        "events.jsonl",
        {"at": at(seconds), "event": name, "invocation": invocation, **fields},
    )


def stage(name, seconds, subject, stage_, invocation, **fields):
    return event(
        seconds,
        name,
        invocation,
        subject=subject,
        kind=FILES[subject][1],
        stage=stage_,
        **fields,
    )


def cache(name, seconds, subject, role, invocation, **fields):
    key = hashlib.sha256(f"{subject}{role}{seconds}".encode()).hexdigest()
    return event(
        seconds,
        name,
        invocation,
        subject=subject,
        kind=FILES[subject][1],
        role=role,
        key=key,
        **fields,
    )


def call(seconds, subject, role, invocation, duration, outcome="ok", **more):
    stages = {"transcribe": "transcription", "extract": "extraction"}
    return event(
        seconds,
        "call",
        invocation,
        subject=subject,
        kind=FILES[subject][1],
        role=role,
        stage=stages.get(role, role),
        call="complete",
        outcome=outcome,
        duration_s=duration,
        **more,
    )


def control(seconds, action, *, invocation=None, scope="run", **fields):
    entry = {"at": at(seconds), "action": action}
    if action == "start":
        entry["invocation"] = invocation
    else:
        entry |= {"scope": scope, "by": "operator", "reason": ""} | fields
    return ("control.jsonl", entry)


def alert(seconds, invocation, stage_, check, subject, value, status, **more):
    return [
        (
            "alerts.jsonl",
            {
                "at": at(seconds),
                "invocation": invocation,
                "stage": stage_,
                "check": check,
                "subject": subject,
                "value": value,
                "status": status,
                **more,
            },
        ),
        event(
            seconds,
            "alert",
            invocation,
            stage=stage_,
            check=check,
            subject=subject,
            status=status,
        ),
    ]


def reused_views(seconds, invocation, *pairs):
    """`reused` events, as a resumed invocation dispatches the stages whose
    views an earlier one made."""
    return [
        stage("reused", seconds, subject, name, invocation, **fields)
        for subject, name, *rest in pairs
        for fields in [rest[0] if rest else {}]
    ]


# The recorded stream, in the order the driver wrote it.
STREAM = [
    # the first invocation: the canaries, then the rest, until the
    # subscription pauses the pass in the postcard's extraction
    control(0, "start", invocation=FIRST),
    event(0, "run-start", FIRST, run_id=RUN_ID),
    control(
        5, "pause", reason="checking Hildegard Laudler's certificate first"
    ),
    control(6, "resume"),
    stage("started", 10, CERT, "transcription", FIRST),
    cache("cache-miss", 11, CERT, "transcribe", FIRST, call="complete",
          reason="absent"),
    call(31, CERT, "transcribe", FIRST, 20.0, model=OPUS,
         usage={"input_tokens": 1200, "output_tokens": 300,
                "cache_read_input_tokens": 800}),
    stage("done", 31, CERT, "transcription", FIRST),
    *alert(31, FIRST, "transcription", "language-unexpected", CERT[:12],
           "yi", "new", confidence=0.51, detail="Hildegard Laudler",
           limit=["de", "en", "frr"]),
    stage("started", 32, CERT, "extraction", FIRST),
    cache("cache-hit", 33, CERT, "extract", FIRST, call="open",
          reason="key match"),
    cache("cache-hit", 34, CERT, "extract", FIRST, call="fork",
          reason="key match"),
    cache("cache-hit", 35, CERT, "extract", FIRST, call="fork",
          reason="key match"),
    stage("done", 40, CERT, "extraction", FIRST),
    stage("started", 41, CERT, "critique", FIRST),
    cache("cache-miss", 42, CERT, "critique", FIRST, call="complete",
          reason="rejected"),
    call(47, CERT, "critique", FIRST, 5.0, model=OPUS,
         usage={"input_tokens": 700, "output_tokens": 50,
                "cache_creation_input_tokens": 90}),
    stage("done", 47, CERT, "critique", FIRST),
    stage("started", 50, DIRECTORY, "transcription", FIRST),
    stage("done", 51, DIRECTORY, "transcription", FIRST),
    stage("started", 52, DIRECTORY, "extraction", FIRST),
    cache("cache-miss", 53, DIRECTORY, "extract", FIRST, call="open",
          reason="absent"),
    call(55, DIRECTORY, "extract", FIRST, 2.0, model=SONNET, usage=USED),
    cache("cache-miss", 56, DIRECTORY, "extract", FIRST, call="fork",
          reason="absent"),
    call(60, DIRECTORY, "extract", FIRST, 4.0, model=SONNET, usage=USED),
    cache("cache-reject", 61, DIRECTORY, "extract", FIRST,
          reason="chunk: no terminal sentinel after 'Löbig, Ursula, Wwe.'"),
    cache("cache-miss", 62, DIRECTORY, "extract", FIRST, call="fork",
          reason="rejected"),
    call(66, DIRECTORY, "extract", FIRST, 4.0, model=SONNET, usage=USED),
    stage("done", 70, DIRECTORY, "extraction", FIRST, attention=[FALLBACK]),
    *alert(70, FIRST, "extraction", "fallback-manifest", DIRECTORY[:12],
           True, "new"),
    stage("started", 71, DIRECTORY, "critique", FIRST),
    cache("cache-hit", 72, DIRECTORY, "critique", FIRST, call="complete",
          reason="key match"),
    stage("done", 72, DIRECTORY, "critique", FIRST),
    stage("started", 80, DECK, "transcription", FIRST),
    stage("done", 81, DECK, "transcription", FIRST),
    stage("started", 82, DECK, "extraction", FIRST),
    cache("cache-miss", 83, DECK, "extract", FIRST, call="open",
          reason="absent"),
    call(84, DECK, "extract", FIRST, 1.0),
    cache("cache-replay", 85, DECK, "extract", FIRST, call="open",
          matches=False),
    call(86, DECK, "extract", FIRST, 1.0),
    cache("cache-miss", 87, DECK, "extract", FIRST, call="fork",
          reason="absent"),
    call(99, DECK, "extract", FIRST, 12.0, outcome="error",
         error="EngineError"),
    stage("failed", 100, DECK, "extraction", FIRST, reason=DECK_FAILURE),
    event(100, "document-failed", FIRST, file=FILES[DECK][0], subject=DECK,
          kind="presentation", reason=DECK_FAILURE),
    stage("started", 101, TREE, "gedcom", FIRST),
    stage("done", 102, TREE, "gedcom", FIRST),
    stage("started", 110, POSTCARD, "transcription", FIRST),
    cache("cache-hit", 111, POSTCARD, "transcribe", FIRST, call="complete",
          reason="adopted"),
    stage("done", 111, POSTCARD, "transcription", FIRST),
    stage("started", 112, POSTCARD, "extraction", FIRST),
    cache("cache-miss", 113, POSTCARD, "extract", FIRST, call="open",
          reason="absent"),
    call(170, POSTCARD, "extract", FIRST, 57.0, outcome="paused",
         error="UsageLimitReached", held="usage limit", wait_s=50.0,
         refused=2, window="session", resumes_at=at(3500)),
    event(600, "run-end", FIRST, outcome="paused",
          reason="UsageLimitReached", statuses={"batched": 4}),
    # the second invocation: an abort of the decks holds the deck's
    # stages, and dispatch stops short of the barrier
    control(3600, "start", invocation=SECOND),
    event(3600, "run-start", SECOND, run_id=RUN_ID),
    event(3600, "document-failed", SECOND, file=UNREAD, kind="media",
          reason=UNREADABLE),
    control(3601, "abort", scope="kind:presentation", reason=ABORT),
    *reused_views(
        3602, SECOND,
        (CERT, "transcription"), (CERT, "extraction"), (CERT, "critique"),
    ),
    event(3603, "set-aside", SECOND, subject=DIRECTORY, kind="text",
          stage="transcription", reason=CHECK_FAILED,
          kept="transcription.superseded-1.json"),
    *alert(3603, SECOND, "transcription", "transcription-check",
           DIRECTORY[:12], True, "carried", failure="tool-failure",
           signature="read-error", detail="Löbig, Ursula, Wwe."),
    stage("started", 3603, DIRECTORY, "transcription", SECOND),
    stage("done", 3604, DIRECTORY, "transcription", SECOND),
    *reused_views(
        3605, SECOND,
        (DIRECTORY, "extraction", {"attention": [FALLBACK]}),
        (DIRECTORY, "critique"),
    ),
    *alert(3605, SECOND, "extraction", "fallback-manifest", DIRECTORY[:12],
           True, "carried"),
    stage("held", 3606, DECK, "transcription", SECOND,
          reason="kind:presentation aborted (Brunnenring 35)"),
    stage("held", 3606, DECK, "extraction", SECOND,
          reason="kind:presentation aborted"),
    *reused_views(3607, SECOND, (TREE, "gedcom"),
                  (POSTCARD, "transcription")),
    stage("started", 3610, POSTCARD, "extraction", SECOND),
    cache("cache-miss", 3611, POSTCARD, "extract", SECOND, call="open",
          reason="absent"),
    # a model the stream names by no model id, and a usage of no counts
    call(3614, POSTCARD, "extract", SECOND, 3.0, model="Hildegard Laudler",
         usage={"input_tokens": True, "output_tokens": -5}),
    cache("cache-hit", 3615, POSTCARD, "extract", SECOND, call="fork",
          reason="key match"),
    stage("done", 3620, POSTCARD, "extraction", SECOND),
    stage("started", 3621, POSTCARD, "critique", SECOND),
    cache("cache-miss", 3622, POSTCARD, "critique", SECOND,
          call="complete", reason="absent"),
    call(3624, POSTCARD, "critique", SECOND, 2.0),
    stage("done", 3624, POSTCARD, "critique", SECOND),
    stage("started", 3630, INTERVIEW, "transcription", SECOND),
    stage("done", 3631, INTERVIEW, "transcription", SECOND),
    stage("started", 3632, INTERVIEW, "extraction", SECOND),
    cache("cache-miss", 3633, INTERVIEW, "extract", SECOND, call="open",
          reason="absent"),
    call(3640, INTERVIEW, "extract", SECOND, 7.0),
    stage("failed", 3650, INTERVIEW, "extraction", SECOND,
          reason=INTERVIEW_FAILURE),
    event(3650, "document-failed", SECOND, file=FILES[INTERVIEW][0],
          subject=INTERVIEW, kind="text", reason=INTERVIEW_FAILURE),
    control(3651, "pause", scope="stage:extraction", by="anomaly",
            check="stage-failures",
            reason="stage-failures: 2 failed (Nicholas Lobig)"),
    control(3652, "resume", scope="stage:extraction", by="monitor"),
    *reused_views(3660, SECOND, (CARD, "transcription")),
    stage("started", 3661, CARD, "extraction", SECOND),
    cache("cache-miss", 3662, CARD, "extract", SECOND, call="open",
          reason="refresh"),
    call(3668, CARD, "extract", SECOND, 6.0),
    stage("done", 3670, CARD, "extraction", SECOND, attention=[NO_CLAIMS]),
    event(3670, "refreshed", SECOND, subject=CARD, kind="text",
          stage="extraction", role="extract"),
    *alert(3670, SECOND, "extraction", "zero-claims", CARD[:12], 312,
           "new", limit=200),
    stage("started", 3671, CARD, "critique", SECOND),
    cache("cache-hit", 3672, CARD, "critique", SECOND, call="complete",
          reason="key match"),
    stage("done", 3672, CARD, "critique", SECOND),
    event(3700, "run-end", SECOND, outcome="stopped",
          reason="kind:presentation aborted", statuses={"batched": 5}),
    # the third invocation: the deck goes through, the interview fails
    # again, and the run goes past the barrier to the clustering stage
    control(7200, "start", invocation=THIRD),
    event(7200, "run-start", THIRD, run_id=RUN_ID),
    event(7200, "document-failed", THIRD, file=UNREAD, kind="media",
          reason=UNREADABLE),
    *reused_views(
        7201, THIRD,
        (CERT, "transcription"), (CERT, "extraction"), (CERT, "critique"),
        (DIRECTORY, "transcription"),
        (DIRECTORY, "extraction", {"attention": [FALLBACK]}),
        (DIRECTORY, "critique"), (TREE, "gedcom"),
        (POSTCARD, "transcription"), (POSTCARD, "extraction"),
        (POSTCARD, "critique"), (CARD, "transcription"),
        (CARD, "extraction", {"attention": [NO_CLAIMS]}),
        (CARD, "critique"), (INTERVIEW, "transcription"),
        (DECK, "transcription"),
    ),
    stage("started", 7211, DECK, "extraction", THIRD),
    cache("cache-miss", 7212, DECK, "extract", THIRD, call="open",
          reason="absent"),
    call(7220, DECK, "extract", THIRD, 8.0),
    cache("cache-hit", 7221, DECK, "extract", THIRD, call="fork",
          reason="key match"),
    stage("done", 7230, DECK, "extraction", THIRD),
    stage("started", 7231, DECK, "critique", THIRD),
    cache("cache-miss", 7232, DECK, "critique", THIRD, call="complete",
          reason="absent"),
    call(7235, DECK, "critique", THIRD, 3.0),
    stage("done", 7235, DECK, "critique", THIRD),
    stage("started", 7240, INTERVIEW, "extraction", THIRD),
    cache("cache-hit", 7241, INTERVIEW, "extract", THIRD, call="open",
          reason="key match"),
    cache("cache-miss", 7242, INTERVIEW, "extract", THIRD, call="fork",
          reason="absent"),
    call(7258, INTERVIEW, "extract", THIRD, 16.0, outcome="error",
         error="EngineError"),
    stage("failed", 7260, INTERVIEW, "extraction", THIRD,
          reason=INTERVIEW_FAILURE),
    event(7260, "document-failed", THIRD, file=FILES[INTERVIEW][0],
          subject=INTERVIEW, kind="text", reason=INTERVIEW_FAILURE),
    *alert(7260, THIRD, "extraction", "chunk-failures", INTERVIEW[:12],
           0.5, "new", count=1, of=2),
    *alert(7260, THIRD, "extraction", "stage-failures", INTERVIEW[:12],
           0.4, "new", count=2, of=5),
    *alert(7260, THIRD, "extraction", "loebig-check", INTERVIEW[:12],
           "Nicholas Lobig", "new"),
    *alert(7260, THIRD, "run", "anomaly-config", "", True, "new",
           problem=f"anomaly thresholds in {EXCLUSIONS}: bad"),
    event(7261, "barrier", THIRD, documents=6, failed=1),
    event(7262, "started", THIRD, stage="clustering"),
    event(7270, "done", THIRD, stage="clustering", attention=CLUSTERED),
    event(7400, "run-end", THIRD, outcome="finished",
          statuses={"batched": 6, "failed": 2}),
]  # fmt: skip


def manifest() -> dict:
    documents = []
    for sha1, (name, kind) in FILES.items():
        stages = ["gedcom"] if kind == "gedcom" else EXTRACTED
        documents.append(
            {"sha1": sha1, "file": name, "kind": kind, "stages": stages}
        )
        if sha1 == DIRECTORY:
            documents.append(
                {"sha1": sha1, "file": COPY, "kind": kind, "stages": stages}
            )

    def role(model, effort, epoch=0, refresh=False):
        return {
            "model": model,
            "effort": effort,
            "epoch": epoch,
            "refresh": refresh,
            "client": {
                "model": model,
                "effort": effort,
                "config_hash": hashlib.sha256(model.encode()).hexdigest(),
                "model_identifier": f"claude-code/2.1.237:{model}",
            },
        }

    return {
        "format": 1,
        "run_id": RUN_ID,
        "mode": "scratch run",
        "project_stage": "development",
        "engine": {"name": "claude-code", "cli_version": "2.1.237"},
        "roles": {
            "transcribe": role("claude-opus-5-5", "low"),
            "extract": role("claude-sonnet-5", "low", refresh=True),
            "critique": role("claude-opus-5-5", "high", epoch=1),
        },
        "cache": CACHE,
        "corpus": {
            "folder": CORPUS,
            "exclusions": {"file": EXCLUSIONS, "sha256": EXCLUSIONS_SHA256},
        },
        "code": {
            "version": "0.1.0",
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "dirty": False,
            "diff_sha256": None,
        },
        "config": {"engine": "claude-code", "language": "auto"},
        "documents": documents,
        "pacing": {"reserve": 0.2, "max_wait": 21600},
        "scratch": {
            "until": None,
            "canaries": [CERT, DIRECTORY, DECK, TREE],
            "corpus_stages": ["clustering"],
            "past_barrier_only": False,
            "complete": True,
        },
        "started_at": at(0),
        "ended_at": at(7400),
        "invocations": [
            {
                "invocation": FIRST,
                "started_at": at(0),
                "ended_at": at(600),
                "outcome": "paused",
                "reason": "UsageLimitReached",
            },
            {
                "invocation": SECOND,
                "started_at": at(3600),
                "ended_at": at(3700),
                "outcome": "stopped",
                "reason": "kind:presentation aborted",
            },
            {
                "invocation": THIRD,
                "started_at": at(7200),
                "ended_at": at(7400),
                "outcome": "finished",
            },
        ],
    }


def entity(local, label, kind, **more):
    return {"id": local, "label": label, "kind": kind, **more}


def claim(subject, prop, excerpt, sha1, language="de"):
    return {
        "subject": {"kind": "local", "local_id": subject},
        "property": prop,
        "value": {"type": "string", "value": excerpt},
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {"text": excerpt, "language": language},
        },
    }


def finding(entity, axis, category, issue, **more):
    """A critic finding as a critique view stores it (#386)."""
    return {
        "entity": entity,
        "axis": axis,
        "category": category,
        "issue": issue,
        **more,
    }


def batch(sha1, entities, claims) -> dict:
    return {
        "schema_version": 1,
        "extraction_run": {
            "model_identifier": "claude-code/2.1.237:claude-sonnet-5",
            "pipeline_version": "0.1.0",
            "started_at": at(0),
        },
        "entities": entities,
        "claims": claims,
    }


def coverage(
    chunks,
    claims,
    rejected=0,
    unread=0,
    fallback=None,
    records=None,
    notes=None,
) -> dict:
    """An extraction's coverage.json; with no `records` and `notes`, as
    one written before it counted them (#420)."""
    counted = {} if records is None else {"records": records}
    if notes is not None:
        counted["notes"] = notes
    return {
        "strategy": "entity",
        "chunks": chunks,
        "claims": claims,
        "rejected_claims": rejected,
        "unread_objects": unread,
        "fallback": fallback or {},
        "entities_without_claims": ["e3"],
        **counted,
    }  # fmt: skip


def records(
    given,
    recordless,
    set_aside,
    by_status=None,
    set_aside_records=None,
    unsplit_forms=None,
    appellation_labels=None,
    appellation_parts=None,
    containers_checked=None,
    containers_dropped=None,
) -> dict:
    """coverage.json's records; with no `set_aside_records`,
    `unsplit_forms`, `appellation_labels` or the place containers checked
    and dropped, as one written before it counted them (#420, #499,
    #569)."""
    newer = {
        name: count
        for name, count in (
            ("set_aside_records", set_aside_records),
            ("unsplit_forms", unsplit_forms),
            ("appellation_labels", appellation_labels),
            ("appellation_parts", appellation_parts),
            ("containers_checked", containers_checked),
            ("containers_dropped", containers_dropped),
        )
        if count is not None
    }
    return {
        "given": given,
        "recordless": recordless,
        "by_status": by_status or {},
        "set_aside": set_aside,
        **newer,
    }


def transcription(sha1, text, language, engine) -> dict:
    return {
        "artifact": {"kind": "artifact", "sha1": sha1},
        "text": text,
        "language": language,
        "engine": engine,
    }


VISION = "llm-vision:claude-code/2.1.237:claude-opus-5-5"


def views() -> dict[str, dict[str, object]]:
    """Each document's files as its directory holds them, by the SHA-1 of
    its bytes."""
    hilde = entity("e1", "Hildegard Laudler", "human", aliases=["Hilde"])
    return {
        CERT: {
            "transcription.json": transcription(
                CERT, SOURCES["marriage-cert-1905"].text, "de", VISION
            ),
            "batch.json": batch(
                CERT,
                [
                    hilde,
                    entity("e2", "Flörburg", "place"),
                    entity(
                        "e3",
                        "Standesamt Flörburg",
                        "organization",
                        extract=False,
                    ),
                ],  # fmt: skip
                [
                    claim("e1", "date_of_birth", "geb. 1876 Laudler", CERT),
                    claim("e1", "place_of_birth", "in Flörburg", CERT),
                    claim("e1", "spouse", "Sebastian Löbig", CERT),
                ],
            ),
            "critique.json": [
                finding("e1", "value", "deictic_alias", "Hilde of Laudler"),
                finding(
                    "e1",
                    "predicate",
                    "misapplied_property",
                    "Sebastian Löbig",
                    claim={
                        "subject": "e1",
                        "property": "spouse",
                        "value": "e3",
                    },
                ),
                finding("e2", "subject", "wrong_kind", "Flörburg is a town"),
                {
                    "invalid": {"target": "", "issue": "Standesamt Flörburg"},
                    "why": ["entity: Field required"],
                },
            ],
            # a record that fails three checks, and three notes set aside:
            # coverage.json counts reasons, and a note set aside as one
            "extraction/coverage.json": coverage(
                3,
                3,
                rejected=2,
                unread=1,
                records=records(
                    3,
                    1,
                    {"addition": 1, "initial": 2, "note": 3},
                    {"name": 2, "toponym": 1},
                    set_aside_records=1,
                    unsplit_forms=3,
                    appellation_labels=2,
                    appellation_parts=1,
                    # #569: containers dropped alone, their records
                    # standing, never among the records set aside
                    containers_checked=4,
                    containers_dropped={
                        "no-evidence": 1,
                        "evidence-not-in-source": 2,
                    },
                ),
                notes={"identity": 2, "date_reading": 3},
            ),
            # a vision transcription's language, from its record (#389)
            "detail": {"code": "de", "source": "record", "confidence": None},
        },
        DIRECTORY: {
            "transcription.json": transcription(
                DIRECTORY, SOURCES["directory-1912"].text, "de", "text-file"
            ),
            "batch.json": batch(
                DIRECTORY,
                [
                    entity("e1", "Löbig, Ursula, Wwe.", "human"),
                    entity("e2", "Angerring 57", "place"),
                ],
                [
                    claim("e1", "residence", "Angerring 57", DIRECTORY),
                    claim("e1", "occupation", "Wwe.", DIRECTORY),
                ],
            ),
            # a critique with only a finding that failed its checks: no
            # document with findings (#386)
            "critique.json": [
                {
                    "invalid": {"entity": "e1", "issue": "Wwe."},
                    "why": ["axis: Field required"],
                }
            ],
            "extraction/coverage.json": coverage(
                2, 2, fallback={"manifest": "input_value='Ursula Löbig'"}
            ),
            "detail": {"code": "de", "source": "detected", "confidence": 1},
        },
        DECK: {
            "transcription.json": transcription(
                DECK,
                "[Slide 1]\nHutmacherei zum Blauen Hut",
                "de",
                "pptx-adapter",
            ),  # fmt: skip
            "batch.json": batch(
                DECK,
                [entity("e1", "Hutmacherei zum Blauen Hut", "organization")],
                [claim("e1", "instance_of", "Hutmacherei", DECK)],
            ),
            "critique.json": [
                finding("e1", "subject", "wrong_kind", "Blauen Hut")
            ],
            "extraction/coverage.json": coverage(
                1, 1, records=records(1, 0, {}, {"name": 1}), notes={}
            ),
            "detail": {"code": "de", "source": "override", "confidence": 1},
        },
        TREE: {
            "batch.json": batch(
                TREE,
                [
                    entity("I1", "Eugen Löbig", "human"),
                    entity("I2", "Cäcilie Tarle", "human"),
                    entity("P1", "Flörburg", "place"),
                ],
                [
                    claim("I1", "date_of_birth", "1836", TREE),
                    claim("I1", "spouse", "Cäcilie Tarle", TREE),
                    claim("I2", "nickname", "Cilli", TREE),
                ],
            ),
        },
        POSTCARD: {
            "transcription.json": transcription(
                POSTCARD, SOURCES["postcard-1938"].text, "de", VISION
            ),
            "batch.json": batch(
                POSTCARD,
                [
                    entity("e1", "Klara Löbig", "human"),
                    entity("e2", "Löbig guild of Flörburg", "loebig_guild"),
                ],
                [
                    claim("e1", "loebig_estate", "Brunnenring 35", POSTCARD),
                    claim("e1", "residence", "Stupfield", POSTCARD),
                ],
            ),
            "critique.json": [],
            "extraction/coverage.json": coverage(
                1,
                2,
                records=records(
                    0, 1, {"no toponym": 1, "note": 2}, set_aside_records=1
                ),
                notes={"kind": 1, "relation": 1},
            ),  # fmt: skip
            "detail": {"code": "de", "source": "fallback", "confidence": 0.2},
        },
        CARD: {
            "transcription.json": transcription(
                CARD,
                SOURCES["registration-card-1945"].text,
                "en",
                "text-file",
            ),
            "batch.json": batch(
                CARD,
                [entity("e1", "Ida Sörne", "human", extract=False)],
                [],
            ),
            # the critique completed, and its view cannot be read
            "critique.json": "{not json: Ida Sörne",
            "extraction/coverage.json": coverage(0, 0),
            "detail": {"code": "en", "source": "detected", "confidence": 1},
        },
        INTERVIEW: {
            # a language tag the transcription's pattern takes, whose
            # subtag names a town
            "transcription.json": transcription(
                INTERVIEW,
                SOURCES["interview-2002"].text,
                "en-stupfield",
                "text-file",
            ),
            "detail": {"code": "en", "source": "override", "confidence": 1},
        },
    }


def identity_map() -> dict:
    """The clustering stage's map (format 1): labels in its mentions and
    review, counts alone in its summary."""
    return {
        "format": 1,
        "stage": "clustering",
        "version": 1,
        "hash": "0" * 64,
        "mentions": {
            "a" * 64: {"label": "Hildegard Laudler", "kind": "human"},
            "b" * 64: {"label": "Flörburg", "kind": "place"},
        },
        "clusters": {"c" * 64: {"kind": "human", "members": ["a" * 64]}},
        "review": [{"members": ["Klurnau", "Rekau"], "pairs": []}],
        "summary": {
            "documents": 6,
            "mentions": 14,
            "clustered_mentions": 12,
            "unclustered_mentions": 2,
            "recordless_mentions": 4,
            "clustered_by_kind": {"human": 8, "place": 3, "loebig_kin": 1},
            "candidate_pairs": 5,
            "confident_pairs": 3,
            "ambiguous_pairs": 2,
            "clusters": {"confident": 2, "ambiguous": 1, "conflicting": 1},
            "cluster_mentions": {
                "confident": 5,
                "ambiguous": 2,
                "conflicting": 3,
            },
            "conflicting_hard": 1,
            "singletons": 2,
            "identity_clusters": 9,
            "hub_keys": 3,
            "hub_mentions": 40,
            "merged_mentions": 5,
            "adjudication": {
                "pairs_per_call": 40,
                "calls_ambiguous_pairs": 1,
                "calls_all_pairs": 1,
            },
        },
    }


def append(path: Path, entries) -> None:
    for name, entry in entries:
        with open(path / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_views(path: Path, sha1: str, files: dict[str, object]) -> Path:
    name, _kind = FILES[sha1]
    directory = path / slugify(Path(name).stem)
    directory.mkdir(parents=True)
    detail = {"language": files["detail"]} if "detail" in files else {}
    state = {
        "file": f"{CORPUS}/{name}",
        "title": Path(name).stem,
        "status": "batched",
        "content_sha1": sha1,
        "detail": detail,
    }
    (directory / "state.json").write_text(json.dumps(state))
    (directory / "derivations.json").write_text("{}")
    for relative, content in files.items():
        if relative == "detail":
            continue
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        target.write_text(text, encoding="utf-8")
    return directory


def record(path: Path, entries=None, *, plan: dict | None = None) -> Path:
    """The fictional run in `path`: run.json, the logs (STREAM by default),
    each document's directory, the duplicate's, a directory set aside and
    the clustering stage's identity map."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_text(
        json.dumps(plan or manifest(), ensure_ascii=False, indent=2)
    )
    append(path, STREAM if entries is None else entries)
    for sha1, files in views().items():
        write_views(path, sha1, files)
    duplicate = path / slugify(Path(COPY).stem)
    duplicate.mkdir()
    (duplicate / "state.json").write_text(
        json.dumps(
            {
                "file": f"{CORPUS}/{COPY}",
                "title": COPY,
                "status": "duplicate",
                "content_sha1": DIRECTORY,
                "detail": {"duplicate_of": FILES[DIRECTORY][0]},
            }
        )
    )
    aside = path / f".{slugify('Flörburg address book 1912')}.superseded-1"
    aside.mkdir()
    (aside / "state.json").write_text(
        json.dumps({"file": "x", "status": "batched", "content_sha1": CERT})
    )
    (aside / "batch.json").write_text(json.dumps(batch(CERT, [], [])))
    clustering = path / "corpus" / "clustering"
    clustering.mkdir(parents=True)
    (clustering / "identity-map.json").write_text(
        json.dumps(identity_map(), ensure_ascii=False)
    )
    return path


@pytest.fixture
def run(tmp_path) -> Path:
    return record(tmp_path / RUN_ID)


@pytest.fixture
def made(run) -> dict:
    return build(run, now=NOW)


def stage_row(made: dict, name: str) -> dict:
    return next(
        row for row in made["aggregate"]["stages"] if row["name"] == name
    )


def document(made: dict, sha1: str) -> dict:
    return next(d for d in made["documents"] if d["sha1"] == sha1)


def drilled(made: dict, sha1: str) -> str:
    """The document's drill-down in report.html."""
    page = report.render_html(made)
    start = page.index(f'<details id="doc-{sha1[:12]}">')
    return page[start : page.index("</details>", start)]


def upto(seconds: float) -> list:
    """The stream as far as the driver had written it at `seconds`."""
    return [
        entry
        for entry in STREAM
        if datetime.fromisoformat(entry[1]["at"])
        <= T0 + timedelta(seconds=seconds)
    ]


def without(entries, event: str, **where) -> list:
    """The entries but the `event` events whose fields match `where`."""
    return [
        entry
        for entry in entries
        if entry[1].get("event") != event
        or any(entry[1].get(key) != value for key, value in where.items())
    ]


# the fixture run in each state a report can find a run in
STATES = (
    "finished",
    "running",
    "paused",
    "stopped",
    "killed first",
    "killed latest",
)


def run_in(state: str) -> tuple[list, dict, datetime]:
    """The fixture run in `state` (STATES): its logs, its run.json, and
    when it is read. Running: the second invocation, in the postcard's
    extraction. Paused: the first invocation ended for the subscription,
    the postcard's extraction cut short. Stopped: the second invocation,
    ended by the abort of the decks. Killed first or latest: that
    invocation recorded no end, and wrote no closing event."""
    plan, entries, now = manifest(), STREAM, NOW
    invocations = plan["invocations"]
    if state == "running":
        plan["invocations"] = invocations[:2]
        invocations[1] |= {"ended_at": None, "outcome": None}
        del invocations[1]["reason"]
        plan["ended_at"] = None
        entries, now = upto(3615), T0 + timedelta(seconds=3700)
    elif state == "paused":
        plan["invocations"], plan["ended_at"] = invocations[:1], at(600)
        entries = upto(600)
    elif state == "stopped":
        plan["invocations"], plan["ended_at"] = invocations[:2], at(3700)
        plan["scratch"]["complete"] = False
        entries = upto(3700)
    elif state == "killed first":
        invocations[0] |= {"ended_at": None, "outcome": None}
        del invocations[0]["reason"]
        entries = without(STREAM, "run-end", invocation=FIRST)
    elif state == "killed latest":
        invocations[2] |= {"ended_at": None, "outcome": None}
        plan["ended_at"] = None
        entries = without(STREAM, "run-end", invocation=THIRD)
        entries = without(entries, "done", stage="clustering")
    return entries, plan, now


# -- a run that names things in every field the aggregates name --------------

HOSTILE = "Laudler"
LOWER = "laudler estate"


def hostile_stream() -> list:
    """The fixture's stream, with a universe name in every field of the
    events and requests that the aggregates name something by."""
    return [
        *STREAM,
        # an engine call: its role, stage, outcome, hold and error
        event(7300, "call", THIRD, subject=CERT, kind="media", role=HOSTILE,
              stage=HOSTILE, outcome=HOSTILE, held=HOSTILE, error=LOWER,
              duration_s=1.0, usage={"input_tokens": 1},
              stop_reason="max_tokens"),
        # the cache's answers: a hit's reason, as a stage's reuse reason
        # too, and a miss's
        event(7301, "started", THIRD, subject=CARD, kind="text",
              stage="critique"),
        event(7302, "cache-hit", THIRD, subject=CARD, kind="text",
              role="critique", reason=LOWER),
        event(7303, "done", THIRD, subject=CARD, kind="text",
              stage="critique"),
        event(7304, "cache-hit", THIRD, subject=CERT, kind="media",
              role=HOSTILE, reason=HOSTILE, gave_up=True),
        event(7305, "cache-miss", THIRD, subject=CERT, kind="media",
              role="extract", reason=HOSTILE),
        # a stage and a kind no plan knows, and an attention label that is
        # no pipeline label
        event(7306, "started", THIRD, subject=CERT, kind=HOSTILE,
              stage=HOSTILE),
        event(7307, "done", THIRD, subject=CERT, kind=HOSTILE, stage=HOSTILE,
              attention=[f"{LOWER}: Hildegard"]),
        # a corpus stage that failed with a lowercase label
        event(7308, "started", THIRD, stage="laudler"),
        event(7309, "failed", THIRD, stage="laudler",
              reason=f"{LOWER} (Hildegard Laudler)"),
        # a document that failed with a lowercase label
        event(7310, "document-failed", THIRD, file="Laudler.jpg",
              kind="media", reason=f"{LOWER} (Hildegard Laudler)"),
        ("control.jsonl", {"at": at(7311), "action": HOSTILE,
                           "scope": f"kind:{HOSTILE}", "by": HOSTILE,
                           "reason": HOSTILE}),
        ("control.jsonl", {"at": at(7312), "action": "pause",
                           "scope": HOSTILE, "by": "operator",
                           "reason": HOSTILE}),
    ]  # fmt: skip


def hostile_plan() -> dict:
    """run.json, with a universe name in every field the aggregates name
    something by."""
    plan = manifest()
    plan["mode"] = HOSTILE
    plan["project_stage"] = HOSTILE
    plan["engine"] = {"name": HOSTILE, "cli_version": HOSTILE}
    plan["code"]["version"] = HOSTILE
    plan["roles"][HOSTILE] = {"model": "Hildegard Laudler", "effort": HOSTILE}
    plan["roles"]["transcribe"]["client"]["model"] = "Hildegard Laudler"
    plan["scratch"]["until"] = HOSTILE
    plan["documents"][0]["kind"] = HOSTILE
    plan["invocations"][-1] |= {"outcome": HOSTILE, "reason": HOSTILE}
    return plan


@pytest.fixture
def hostile(tmp_path) -> Path:
    """The fixture run with a name in every field the aggregates name
    something by: its events, its requests, its run.json and the views'
    route, language resolution, fallback tier and critic finding."""
    path = record(tmp_path / "hostile", hostile_stream(), plan=hostile_plan())
    cert = path / slugify(Path(FILES[CERT][0]).stem)

    def rewrite(name: str, change) -> None:
        value = json.loads((cert / name).read_text())
        change(value)
        (cert / name).write_text(json.dumps(value))

    rewrite("transcription.json", lambda v: v.update(engine=f"{HOSTILE}:x"))
    rewrite(
        "state.json",
        lambda v: v["detail"]["language"].update(source=HOSTILE),
    )
    rewrite(
        "extraction/coverage.json",
        lambda v: v.update(
            fallback={HOSTILE: 1},
            records=records(1, 0, {HOSTILE: 1}, {HOSTILE: 1}),
            notes={HOSTILE: 1},
        ),
    )
    rewrite(
        "critique.json",
        lambda v: v.append(finding("e1", HOSTILE, HOSTILE, HOSTILE)),
    )
    return path


# -- rewriting a run's words, and nothing else --------------------------------

# the names the fixture's documents carry, longest first, and a SHA-1 for
# each document's in its place
SWAPPED_NAMES = sorted(
    {name for name in NAMES if not name.startswith("/") and len(name) > 3}
    | {"Hilde", "Cäcilie", "Tarle", "Cilli", "Ida", "Angerring"},
    key=len,
    reverse=True,
)
SWAPPED_SHA1S = {sha1: _sha1("other " + sha1) for sha1 in FILES}


def swapped(name: str) -> str:
    """A made-up word as long as `name`, in its case, the same each time."""
    digest = hashlib.sha1(name.encode()).hexdigest()
    word = "".join(chr(ord("k") + int(c, 16) % 10) for c in digest)
    word = word[: len(name)]
    return word.capitalize() if name[0].isupper() else word


def rewritten(value):
    """A JSON value with every document's name and SHA-1 in it replaced."""
    if isinstance(value, dict):
        return {rewritten(k): rewritten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rewritten(v) for v in value]
    if not isinstance(value, str):
        return value
    for old, new in SWAPPED_SHA1S.items():
        for size in (40, 12, 10, 8):
            value = value.replace(old[:size], new[:size])
    for name in SWAPPED_NAMES:
        for form in (name, name.lower(), name.upper()):
            value = re.sub(
                r"(?<![A-Za-z])" + re.escape(form) + r"(?![a-z])",
                swapped(form),
                value,
            )
    return value


def scramble(path: Path, rng, odd: list, rate: float) -> None:
    """Swap leaves of every JSON file of a run directory, and now and then
    a whole object or list, for values of other types (`odd`), each with
    the chance `rate`."""

    def mutated(value):
        if isinstance(value, dict | list) and rng.random() < rate / 3:
            return rng.choice(odd)
        if isinstance(value, dict):
            return {key: mutated(item) for key, item in value.items()}
        if isinstance(value, list):
            return [mutated(item) for item in value]
        return rng.choice(odd) if rng.random() < rate else value

    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.suffix not in (".json", ".jsonl"):
            continue
        text = file.read_text(encoding="utf-8")
        if file.suffix == ".jsonl":
            out = []
            for line in text.split("\n"):
                try:
                    out.append(json.dumps(mutated(json.loads(line))))
                except ValueError:
                    out.append(line)
            file.write_text("\n".join(out), encoding="utf-8")
        else:
            with contextlib.suppress(ValueError):
                scrambled = mutated(json.loads(text))
                file.write_text(json.dumps(scrambled), encoding="utf-8")


def rewritten_line(line: str) -> str:
    """A log's line rewritten (rewritten); one that holds no JSON as it
    is."""
    try:
        return json.dumps(rewritten(json.loads(line)))
    except ValueError:
        return line


def rewrite_run(path: Path) -> None:
    """Rewrite every JSON file of a run directory with its documents'
    names and SHA-1s replaced (rewritten), and nothing else changed."""
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.suffix not in (".json", ".jsonl"):
            continue
        text = file.read_text(encoding="utf-8")
        if file.suffix == ".jsonl":
            file.write_text(
                "\n".join(map(rewritten_line, text.split("\n"))),
                encoding="utf-8",
            )
        else:
            try:
                file.write_text(json.dumps(rewritten(json.loads(text))))
            except ValueError:
                file.write_text(rewritten(text))


def disagreement(path: Path, made: dict) -> str | None:
    """How the report's status of each document's stage differs from
    rundir.run_state's for the same files, or None: a pending stage the
    control file holds is held in the report."""
    shown = {
        (d["sha1"], name): record["status"]
        for d in made["documents"]
        for name, record in d["stages"].items()
    }
    expected = {
        key: HELD if state.status == PENDING and state.reason else state.status
        for key, state in run_state(RunDir(path)).items()
    }
    return None if shown == expected else f"{shown} != {expected}"


def strings(value) -> list[str]:
    """Every string in a JSON value, its keys included."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (k, *strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return [value] if isinstance(value, str) else []


def leaks(text: str) -> list[str]:
    """The names, paths, hashes and document SHA-1s `text` holds."""
    folded = text.casefold()
    found = [name for name in NAMES if name.casefold() in folded]
    found += [
        sha1[:8] for sha1 in FILES if sha1[:8] in text or sha1[:8] in folded
    ]
    found += [name for name, _kind in FILES.values() if name in text]
    return found + ([UNREAD] if UNREAD in text else [])


# -- report.md: aggregates alone ----------------------------------------------


class TestAggregateOnly:
    def test_report_md_holds_no_name_path_label_or_document(self, run):
        written = write_report(run, now=NOW)
        text = written["report.md"].read_text()
        assert leaks(text) == []
        # the free text is there to leak: the local report carries it
        local = written["report.json"].read_text()
        assert set(leaks(local)) >= {"Laudler", "Stupfield", UNREAD}

    @pytest.mark.parametrize("fixture", ["run", "hostile"])
    def test_no_string_the_aggregates_hold_carries_a_name(
        self, request, fixture
    ):
        made = build(request.getfixturevalue(fixture), now=NOW)
        found = [s for s in strings(made["aggregate"]) if leaks(s)]
        assert found == []

    def test_every_field_the_aggregates_name_is_checked(self, hostile):
        """A name in each field the aggregates name something by folds
        into OTHER, wherever it comes from: an event, a request, run.json
        or a view."""
        made = build(hostile, now=NOW)
        aggregate = made["aggregate"]
        assert leaks(render_markdown(aggregate)) == []
        run = aggregate["run"]
        assert (run["mode"], run["project_stage"], run["state"]) == (
            OTHER,
            OTHER,
            OTHER,
        )
        assert run["reason"] == OTHER and run["until"] == OTHER
        assert run["engine"] == {"name": OTHER, "cli_version": None}
        assert run["code"]["version"] is None
        assert run["roles"][OTHER] == {
            "model": OTHER,
            "effort": OTHER,
            "epoch": None,
            "refresh": False,
            "config": None,
        }
        assert run["roles"]["transcribe"]["model"] == OTHER
        assert aggregate["documents"]["by_kind"][OTHER] == 1
        assert aggregate["documents"]["routes"][OTHER] == 1
        assert aggregate["documents"]["language_resolved"][OTHER] == 1
        assert stage_row(made, OTHER)["attention_labels"] == {OTHER: 1}
        assert stage_row(made, "critique")["reused_by"][OTHER] == 1
        (corpus,) = [s for s in aggregate["corpus_stages"] if s["failure"]]
        assert (corpus["name"], corpus["failure"]) == (OTHER, OTHER)
        assert aggregate["documents"]["failed_by_label"][OTHER] == 1
        assert aggregate["calls"]["output_limit_stops"][OTHER] == {
            "cut_off": 1,
            "gave_up": 0,
        }
        assert aggregate["calls"]["give_ups_replayed"] == {OTHER: 1}
        content = aggregate["content"]
        assert content["fallbacks"][OTHER] == 1
        assert content["records"]["set_aside_by_reason"][OTHER] == 1
        assert content["records"]["by_status"][OTHER] == 1
        assert content["notes"]["by_category"][OTHER] == 1
        # the document's own counts fold as the aggregate's do
        view = document(made, CERT)["coverage"]
        assert view["records"]["set_aside_by_reason"] == {OTHER: 1}
        assert view["records"]["by_status"] == {OTHER: 1}
        assert view["notes"]["by_category"] == {OTHER: 1}
        page = drilled(made, CERT)
        start = page.index("Mention records given")
        line = page[start : page.index("</li>", start)]
        assert HOSTILE not in line
        assert "by status: other 1;" in line
        assert "reasons set aside: other 1;" in line
        assert "judgement notes: other 1;" in line
        assert content["critic"]["by_axis"][OTHER] == 1
        assert content["critic"]["by_category"][OTHER] == 1
        calls = aggregate["calls"]
        assert calls["hits_by_reason"][OTHER] == 2
        assert calls["misses_by_reason"][OTHER] == 1
        assert calls["by_role"][OTHER]["cached"] == 1
        engine = calls["engine_calls"]
        assert engine["outcomes"][OTHER] == 1
        assert engine["holds"][OTHER] == 1
        assert engine["errors"][OTHER] == 1
        assert engine["by_role"][OTHER] == 1
        usage = [
            (row["role"], row["calls"], row["reported"])
            for row in calls["usage"]["by_stage"]
            if row["stage"] == OTHER
        ]
        assert usage == [(OTHER, 1, 1)]
        control = aggregate["control"]
        assert control["by_action"][OTHER] == 1
        assert control["by_requester"][OTHER] == 1
        assert control["pauses"][OTHER] == 1

    def test_report_md_names_no_language(self, run):
        """The documents per language would list the corpus's languages,
        which the operator keeps private (#458): report.md says where the
        counts are and names no language code, neither a transcription's
        nor an alert's value; report.html and report.json keep them."""
        for sha1, language in ((DIRECTORY, "ru"), (POSTCARD, "fy")):
            path = run / slugify(Path(FILES[sha1][0]).stem)
            view = json.loads((path / "transcription.json").read_text())
            view["language"] = language
            (path / "transcription.json").write_text(json.dumps(view))
        written = write_report(run, now=NOW)
        made = json.loads(written["report.json"].read_text())
        codes = set(made["aggregate"]["documents"]["languages"]) - {OTHER}
        assert codes == {"de", "en", "ru", "fy"}
        # the alert on the certificate gives a language as its value, and
        # the expected languages, one of them no transcription's, as its
        # limit
        (alert,) = [
            line
            for line in map(json.loads, (run / "alerts.jsonl").open())
            if line["check"] == "language-unexpected"
        ]
        assert (alert["value"], alert["limit"]) == ("yi", ["de", "en", "frr"])
        text = written["report.md"].read_text()
        # whole words, in any case: the fixture's codes are no English
        # words, as some ISO 639-1 codes are (no, it, be, an, so, to)
        named = [
            code
            for code in sorted(codes | {"yi", "frr"})
            if re.search(rf"(?<![\w-]){code}(?![\w-])", text, re.IGNORECASE)
        ]
        assert named == []
        assert "| Language |" not in text
        assert (
            "Documents per language: in the local report alone "
            "(report.html, report.json)."
        ) in text
        page = written["report.html"].read_text()
        for code in codes:
            assert f"<td>{code}</td>" in page
        # a whole row: the code, its documents and its documents by kind
        assert (
            '<tr><td>de</td><td class="num">2</td>'
            "<td>media 1, presentation 1</td></tr>"
        ) in page
        # and each document's own drill-down names its language
        assert "Transcription: language ru, detected; route text-file" in (
            drilled(made, DIRECTORY)
        )

    def test_no_transcription_read_says_so_in_report_html(self, made):
        """With no transcription read, report.html says so in place of the
        table of documents per language; report.md names no language
        either way."""
        made["aggregate"]["documents"]["languages"] = {}
        page = report.render_html(made)
        assert "No transcription read." in page
        assert "<th>Language</th>" not in page
        assert "No transcription read." not in render_markdown(
            made["aggregate"]
        )

    def test_report_md_is_the_same_whatever_the_documents_say(self, tmp_path):
        """Every document's name and SHA-1, in every file of the run
        directory, replaced by other words and hashes: report.md comes out
        the same, byte for byte."""
        first = record(tmp_path / "first")
        second = tmp_path / "second"
        shutil.copytree(first, second)
        rewrite_run(second)
        assert render_markdown(build(first, now=NOW)["aggregate"]) == (
            render_markdown(build(second, now=NOW)["aggregate"])
        )
        # the rewriting changed what the local report holds
        assert (
            build(second, now=NOW)["documents"]
            != (build(first, now=NOW)["documents"])
        )

    def test_a_number_a_failure_quotes_does_not_reach_report_md(
        self, tmp_path
    ):
        """A failed extraction's reason can quote the model's answer,
        which can quote a document: a number in it stays local."""
        quoted = (
            "ExtractionFailed: no claims extracted: 1 of 2 chunks failed "
            "(c1: input_value='Nicholas Lobig, 8 Hawthorn Road: validation "
            "rejected every claim (4711)'); validation rejected every claim "
            "(2)"
        )
        entries = [
            (name, entry | {"reason": quoted})
            if entry.get("reason") == INTERVIEW_FAILURE
            and entry.get("invocation") == THIRD
            else (name, entry)
            for name, entry in STREAM
        ]
        made = build(record(tmp_path / "run", entries), now=NOW)
        text = render_markdown(made["aggregate"])
        assert "4,71" not in text and "4711" not in text
        assert (
            made["aggregate"]["content"]["rejects"]["failed_extractions"] == 1
        )
        assert document(made, INTERVIEW)["unusable"] == 4713
        assert "4713" in report.render_html(made)

    def test_an_exception_is_named_by_its_class_alone(self, tmp_path):
        dotted = "yizkr.pipeline.chunking.ExtractionFailed"
        entries = [
            (name, entry | {"reason": f"{dotted}: Nicholas Lobig"})
            if entry.get("reason") == INTERVIEW_FAILURE
            else (name, entry)
            for name, entry in STREAM
        ]
        entries.append(
            call(7299, CERT, "extract", THIRD, 1.0, outcome="error",
                 error="yizkr.pipeline.llm.EngineError")
        )  # fmt: skip
        plan = manifest()
        plan["invocations"][-1] |= {
            "outcome": "failed",
            "reason": "yizkr.pipeline.llm.UsageLimitReached",
        }
        made = build(record(tmp_path / "run", entries, plan=plan), now=NOW)
        aggregate = made["aggregate"]
        assert stage_row(made, "extraction")["failure_labels"] == {
            "ExtractionFailed": 1
        }
        assert aggregate["run"]["reason"] == "UsageLimitReached"
        assert aggregate["calls"]["engine_calls"]["errors"]["EngineError"] == 3
        assert "yizkr." not in render_markdown(aggregate)

    def test_the_code_says_only_whether_it_was_dirty(self, tmp_path):
        plan = manifest()
        diff = hashlib.sha256(b"a small fictional edit").hexdigest()
        plan["code"] |= {"dirty": True, "diff_sha256": diff}
        made = build(record(tmp_path / "run", plan=plan), now=NOW)
        assert made["aggregate"]["run"]["code"]["dirty"] is True
        text = render_markdown(made["aggregate"])
        assert "| Code | 0123456789ab, dirty, version 0.1.0 |" in text
        assert diff[:8] not in text
        # the local report keeps it
        assert made["run"]["code"]["diff_sha256"] == diff

    def test_names_it_does_not_know_are_counted_as_other(self, made):
        content = made["aggregate"]["content"]
        assert content["claims"]["by_property"][OTHER] == 1
        assert content["entities"]["by_kind"][OTHER] == 1
        assert content["clusters"]["by_kind"][OTHER] == 1
        anomalies = made["aggregate"]["anomalies"]["by_check"]
        assert anomalies[OTHER]["new"] == 1
        extraction = stage_row(made, "extraction")
        assert extraction["attention_labels"] == {
            "entity manifest fell back to line-range segmentation": 1,
            "no claims extracted": 1,
        }
        assert extraction["failure_labels"] == {"RuntimeError": 1}

    def test_the_never_ingest_list_and_the_paths_stay_out(self, run):
        text = write_report(run, now=NOW)["report.md"].read_text()
        assert EXCLUSIONS_SHA256[:8] not in text
        assert "/archive" not in text and "/srv" not in text

    def test_markdown_is_rendered_from_the_aggregates_alone(self, made):
        assert render_markdown(made["aggregate"]) == render_markdown(
            json.loads(json.dumps(made["aggregate"]))
        )


# -- per stage ----------------------------------------------------------------


class TestStages:
    def test_each_stage_partitions_its_documents(self, made):
        rows = {row["name"]: row for row in made["aggregate"]["stages"]}
        assert list(rows) == ["transcription", "extraction", "critique",
                              "gedcom"]  # fmt: skip
        transcription = rows["transcription"]
        assert transcription["documents"] == 6
        assert (transcription[COMPUTED], transcription[REUSED]) == (4, 2)
        assert transcription["reused_by"] == {
            "adopted": 1,
            "view current": 1,
        }
        extraction = rows["extraction"]
        assert (extraction[COMPUTED], extraction[REUSED]) == (2, 1)
        assert extraction["reused_by"] == {"key match": 1}
        assert extraction[NEEDS_ATTENTION] == 2
        assert extraction["attention_by"] == {COMPUTED: 2}
        assert extraction[FAILED] == 1
        critique = rows["critique"]
        assert (critique[COMPUTED], critique[REUSED], critique[PENDING]) == (
            3,
            2,
            1,
        )
        assert rows["gedcom"][COMPUTED] == 1

    def test_what_sent_each_computed_stage_to_the_engine(self, made):
        assert stage_row(made, "transcription")["forced"] == {
            "no call": 3,
            "absent": 1,
        }
        assert stage_row(made, "extraction")["forced"] == {
            "absent": 2,
            "absent + rejected": 1,
            "refresh": 1,
        }
        assert stage_row(made, "critique")["forced"] == {
            "absent": 2,
            "rejected": 1,
        }

    def test_a_view_a_later_invocation_reuses_counts_as_it_was_made(
        self, made
    ):
        # the certificate's transcription was computed in the first
        # invocation, and the later ones reused its view
        stages = document(made, CERT)["stages"]
        assert stages["transcription"]["how"] == COMPUTED
        assert stages["transcription"]["forced"] == "absent"
        assert stages["transcription"]["engine"] == 1
        assert stages["extraction"]["how"] == "key match"
        # the card's transcription view came before the run's events
        assert document(made, CARD)["stages"]["transcription"]["how"] == (
            "view current"
        )

    def test_calls_and_time_are_counted_per_stage(self, made):
        # over every attempt: the deck's failed one included, and the
        # postcard's that the subscription cut short
        extraction = stage_row(made, "extraction")
        assert extraction["engine_calls"] == 12
        assert extraction["cached_calls"] == 6
        assert extraction["call_events"] == 12
        # a view reused whole takes no time: only a start and its end do
        transcription = stage_row(made, "transcription")
        assert transcription["timed"] == 6
        assert transcription["seconds"] == 21 + 1 + 1 + 1 + 1 + 1


class TestCorpusStages:
    def test_the_barrier_and_the_clustering_stage(self, made):
        aggregate = made["aggregate"]
        assert aggregate["run"]["barrier"] == {"documents": 6, "failed": 1}
        (clustering,) = aggregate["corpus_stages"]
        assert clustering["name"] == "clustering"
        assert clustering["state"] == NEEDS_ATTENTION
        assert clustering["attention"] == {
            "ambiguous clusters not merged": 1,
            "conflicting clusters not merged": 1,
        }

    def test_the_clusters_come_from_the_identity_map_s_summary(self, made):
        clusters = made["aggregate"]["content"]["clusters"]
        assert clusters["map"] == "read"
        counts = clusters["counts"]
        assert counts["confident merges"] == 2
        assert counts["ambiguous, left split"] == 1
        assert counts["conflicting"] == 1
        assert counts["singletons"] == 2
        assert counts["hub keys, not blocked on"] == 3
        assert counts["mentions holding them"] == 40
        # the mentions without a record, which never link confidently
        assert counts["recordless mentions"] == 4
        assert clusters["by_kind"] == {"human": 8, "place": 3, OTHER: 1}

    def test_a_run_without_the_stage_says_not_run(self, tmp_path):
        plan = manifest()
        plan["scratch"]["corpus_stages"] = []
        entries = [e for e in STREAM if "clustering" not in json.dumps(e)]
        path = record(tmp_path / "run", entries, plan=plan)
        (path / "corpus" / "clustering" / "identity-map.json").unlink()
        aggregate = build(path, now=NOW)["aggregate"]
        assert aggregate["corpus_stages"] == []
        assert aggregate["content"]["clusters"] == {"status": NOT_RUN}
        text = render_markdown(aggregate)
        assert "No corpus stage in this run." in text
        assert "Not run: the run has no clustering stage" in text

    def test_a_map_it_cannot_read_is_said_to_be(self, run):
        (run / "corpus" / "clustering" / "identity-map.json").write_text(
            json.dumps({"format": 2, "summary": {}})
        )
        clusters = build(run, now=NOW)["aggregate"]["content"]["clusters"]
        assert clusters == {"status": NEEDS_ATTENTION, "map": "unreadable"}

    def test_the_clustering_stage_s_hub_key_label_is_known(self, tmp_path):
        hub = "hub keys not blocked on: 3 keys, held by 40 mentions"
        entries = [
            (name, entry | {"attention": [*CLUSTERED, hub]})
            if entry.get("stage") == "clustering"
            and entry.get("event") == "done"
            else (name, entry)
            for name, entry in STREAM
        ]
        made = build(record(tmp_path / "run", entries), now=NOW)
        (clustering,) = made["aggregate"]["corpus_stages"]
        assert clustering["attention"] == {
            "ambiguous clusters not merged": 1,
            "conflicting clusters not merged": 1,
            "hub keys not blocked on": 1,
        }


# -- the anomalies ------------------------------------------------------------


class TestAnomalies:
    def test_findings_by_check_new_or_carried(self, made):
        anomalies = made["aggregate"]["anomalies"]
        assert anomalies["alerts"] == 9
        by_check = anomalies["by_check"]
        # new on the first invocation, carried on its reuse: new wins
        assert by_check["fallback-manifest"] == {
            "new": 1,
            "carried": 0,
            "alerts": 2,
        }
        assert by_check["transcription-check"] == {
            "new": 0,
            "carried": 1,
            "alerts": 1,
        }
        assert by_check["anomaly-config"]["new"] == 1
        assert (anomalies["new"], anomalies["carried"]) == (7, 1)

    def test_each_alert_joins_its_document(self, made):
        alerts = document(made, DIRECTORY)["alerts"]
        assert [(a["check"], a["status"]) for a in alerts] == [
            ("fallback-manifest", "new"),
            ("transcription-check", "carried"),
            ("fallback-manifest", "carried"),
        ]


# -- the content --------------------------------------------------------------


class TestContent:
    def test_documents_by_kind_and_language(self, made):
        documents = made["aggregate"]["documents"]
        assert documents["planned"] == 7
        assert (documents["files"], documents["duplicates"]) == (8, 1)
        assert documents["by_kind"] == {
            "text": 3,
            "media": 2,
            "gedcom": 1,
            "presentation": 1,
        }
        # a language tag with a subtag of its own is counted as OTHER
        assert documents["languages"] == {
            "de": {
                "documents": 4,
                "by_kind": {"media": 2, "presentation": 1, "text": 1},
            },
            "en": {"documents": 1, "by_kind": {"text": 1}},
            OTHER: {"documents": 1, "by_kind": {"text": 1}},
        }
        assert documents["routes"] == {
            "text-file": 3,
            "llm-vision": 2,
            "pptx-adapter": 1,
        }
        assert documents["language_resolved"] == {
            "record": 1,
            "detected": 2,
            "override": 2,
            "fallback": 1,
        }

    def test_a_pdf_read_page_by_page_counts_its_pages(self, run):
        """A PDF read page by page (#176) takes its own route, and its
        pages count by the method that read each, as do its text-layer
        pages that show annotations the text layer leaves unread. A page
        whose method is no known one folds into OTHER, and one whose
        provenance cannot be read counts nothing."""
        postcard = run / slugify(Path(FILES[POSTCARD][0]).stem)
        view = json.loads((postcard / "transcription.json").read_text())
        text = view["text"]

        def page(number, method, annotations=0):
            provenance = {
                "method": method,
                "engine": "pdf-text-layer/1",
                "annotations": annotations,
            }
            return {
                "page": number,
                "start": 0,
                "end": len(text),
                "images": [],
                "provenance": provenance,
            }

        view |= {
            "engine": "pdf-pages:pdf-text-layer/1",
            "pages": [
                page(1, "text-layer", annotations=2),
                page(2, "text-layer"),
                page(3, "vision", annotations=1),
                page(4, "none"),
                page(5, HOSTILE),
                page(6, "text-layer", annotations=True),
                {"page": 7},
            ],
        }
        (postcard / "transcription.json").write_text(json.dumps(view))
        documents = build(run, now=NOW)["aggregate"]["documents"]
        assert documents["routes"] == {
            "text-file": 3,
            "llm-vision": 1,
            "pdf-pages": 1,
            "pptx-adapter": 1,
        }
        assert documents["pdf_pages"] == {
            "text-layer": 3,
            "vision": 1,
            "none": 1,
            OTHER: 1,
        }
        assert documents["pdf_pages_annotated"] == 1
        text = render_markdown(build(run, now=NOW)["aggregate"])
        assert (
            "Pages of PDFs read page by page, by method: text-layer 3"
        ) in text

    def test_a_pdf_s_images_count_per_pdf(self, run):
        """The images a PDF read page by page places and reads on its own
        (#397) count per PDF, each once however many pages place it; a
        name that is no SHA-1 counts nothing, and no image's SHA-1 reaches
        report.md."""
        postcard = run / slugify(Path(FILES[POSTCARD][0]).stem)
        view = json.loads((postcard / "transcription.json").read_text())
        text = view["text"]
        letter, inscription = "a" * 40, "b" * 40

        def page(number, images):
            provenance = {"method": "text-layer", "engine": "pdf-text-layer/1"}
            return {
                "page": number,
                "start": 0,
                "end": len(text),
                "images": images,
                "provenance": provenance,
            }

        view |= {
            "engine": "pdf-pages:pdf-text-layer/1",
            "pages": [
                page(1, [letter, HOSTILE, 5]),
                page(2, [letter, inscription]),
            ],
        }
        (postcard / "transcription.json").write_text(json.dumps(view))
        aggregate = build(run, now=NOW)["aggregate"]
        assert aggregate["documents"]["pdf_images"] == {
            "pdfs": 1,
            "placed": 2,
            "distinct": 2,
            "per_pdf": {"2": 1},
        }
        assert aggregate["documents"]["excerpts"] == 0
        text = render_markdown(aggregate)
        assert (
            "read on their own: 2 in 1 PDFs (2 distinct; images per PDF: "
            "2 in 1); images embedded in decks and read on their own: 0 in "
            "0 decks (0 distinct; images per deck: none); documents of "
            "their own (excerpts): 0"
        ) in text
        assert letter not in text

    def test_failed_documents_each_once(self, made):
        documents = made["aggregate"]["documents"]
        assert documents["failed"] == 2
        assert documents["failed_unread"] == 1
        assert documents["failed_by_label"] == {
            "RuntimeError": 1,
            "its source cannot be read": 1,
        }
        # the kind table counts the planned documents of each kind that
        # failed: the interview; a source that could not be read is never
        # planned, and counts apart, by the kind its event gives
        assert documents["failed_by_kind"] == {"text": 1}
        assert documents["failed_unplanned"] == 1
        assert documents["failed_unplanned_by_kind"] == {"media": 1}
        text = render_markdown(made["aggregate"])
        assert "| media | 2 | 0 |" in text and "| text | 3 | 1 |" in text
        assert made["failed_unread"] == [
            {"file": UNREAD, "kind": "media", "reason": UNREADABLE}
        ]

    def test_entities_claims_rejects_and_critic_findings(self, made):
        content = made["aggregate"]["content"]
        assert content["batches"] == 6
        assert content["entities"]["by_kind"] == {
            "human": 6,
            "place": 3,
            "organization": 2,
            OTHER: 1,
        }
        assert content["entities"]["not_extracted"] == 2
        assert content["claims"]["total"] == 11
        assert content["claims"]["by_property"]["residence"] == 2
        rejects = content["rejects"]
        assert (rejects["rejected_claims"], rejects["unread_objects"]) == (
            2,
            1,
        )
        assert rejects["documents"] == 1
        assert rejects["rate"] == round(3 / (3 + 8), 4)
        # the deck's first extraction failed with every claim rejected, and
        # the third made it: only a failure as the events leave it counts
        assert rejects["failed_extractions"] == 0
        assert content["zero_claims"] == {
            "documents": 1,
            "share": round(1 / 6, 4),
            "by_kind": {"text": 1},
        }
        assert content["fallbacks"] == {"manifest": 1}
        critic = content["critic"]
        # counted by axis and by category (#386); the finding that failed
        # its checks counts apart, as invalid
        assert critic["by_axis"] == {"subject": 2, "predicate": 1, "value": 1}
        assert critic["by_category"] == {
            "wrong_kind": 2,
            "deictic_alias": 1,
            "misapplied_property": 1,
        }
        assert (critic["findings"], critic["invalid"]) == (4, 2)
        assert (critic["critiques"], critic["documents"]) == (4, 2)
        text = render_markdown(made["aggregate"])
        assert "| wrong_kind | 2 |" in text and "| predicate | 1 |" in text
        # each document's own counts, in the local report
        assert document(made, CERT)["critique"] == {
            "findings": 3,
            "invalid": 1,
            "by_axis": {"value": 1, "predicate": 1, "subject": 1},
            "by_category": {
                "deictic_alias": 1,
                "misapplied_property": 1,
                "wrong_kind": 1,
            },
        }
        page = report.render_html(made)
        assert (
            "Critic findings: 3; invalid: 1; by axis: predicate 1, subject "
            "1, value 1; by category: deictic_alias 1, misapplied_property "
            "1, wrong_kind 1"
        ) in page

    def test_mention_records_and_judgement_notes(self, made):
        """Each extraction's coverage counts summed (#420): the records
        given, the mentions recordless and the reasons records were set
        aside by, and the notes by category and those set aside, which
        coverage.json counts among its reasons as `note`. The card's
        coverage predates the counts and the directory's manifest fell
        back to line ranges: both are counted apart, never as zero."""
        content = made["aggregate"]["content"]
        assert content["records"] == {
            "extractions": 3,
            "given": 4,
            "recordless": 2,
            "by_status": {"name": 3, "toponym": 1},
            "unsplit_forms": 3,
            "set_aside_records": 2,
            "appellation_labels": 2,
            "appellation_parts": 1,
            "set_aside_reasons": 4,
            "set_aside_by_reason": {
                "initial": 2,
                "addition": 1,
                "no toponym": 1,
            },
            # the certificate's containers dropped alone (#569), counted
            # apart from the records set aside
            "containers_checked": 4,
            "containers_dropped": 3,
            "containers_dropped_by_reason": {
                "evidence-not-in-source": 2,
                "no-evidence": 1,
            },
            # the deck's coverage predates the three newer counts, the
            # postcard's those of unsplit forms and the appellation counts,
            # and both predate the containers'
            "absent": {
                "predating": 1,
                "line_range": 1,
                "set_aside_records": 1,
                "unsplit_forms": 2,
                "appellation_labels": 2,
                "appellation_parts": 2,
                "containers": 2,
            },
        }
        assert content["notes"] == {
            "total": 7,
            "by_category": {
                "date_reading": 3,
                "identity": 2,
                "kind": 1,
                "relation": 1,
            },
            "set_aside": 5,
        }
        # each figure in its own place, and each table under its own head
        assert (
            "\n### Mention records and judgement notes\n\n"
            "- Extractions counted: 3; mention records given: 4; recordless "
            "mentions (no record given, or it was set aside): 2\n"
            "- Unsplit name forms in the records given: 3\n"
            "- Labels that were appellations, replaced with the name the "
            "record gives: 2; record name parts that are appellations: 1\n"
            "- Records set aside: 2; reasons records were set aside: 4 (a "
            "record can give several)\n"
            "- Place containers checked: 4; dropped alone, their records "
            "standing, so counted apart from records set aside: 3\n"
            "- Judgement notes: 7; notes set aside: 5\n"
            "- Extractions not counted: 1 whose coverage predates these "
            "counts; 1 that fell back to line ranges (all their mentions "
            "recordless)\n"
            "- Extractions counted whose coverage gives no count of it, so "
            "left out of that count: records set aside 1; unsplit name "
            "forms 2; appellation labels 2; appellation parts 2; place "
            "containers 2\n\n"
            "| Record status | Records |\n"
            "|---|---:|\n"
            "| name | 3 |\n"
            "| toponym | 1 |\n\n"
            "| Set-aside reason | Reasons |\n"
            "|---|---:|\n"
            "| initial | 2 |\n"
            "| addition | 1 |\n"
            "| no toponym | 1 |\n\n"
            "| Container dropped reason | Containers |\n"
            "|---|---:|\n"
            "| evidence-not-in-source | 2 |\n"
            "| no-evidence | 1 |\n\n"
            "| Note category | Notes |\n"
            "|---|---:|\n"
            "| date_reading | 3 |\n"
            "| identity | 2 |\n"
            "| kind | 1 |\n"
            "| relation | 1 |\n\n"
            "### Zero-claim documents\n"
        ) in render_markdown(made["aggregate"])
        # each document's own counts, in the local report
        cert = document(made, CERT)["coverage"]
        assert cert["records"] == {
            "given": 3,
            "recordless": 1,
            "by_status": {"name": 2, "toponym": 1},
            "unsplit_forms": 3,
            "set_aside_records": 1,
            "appellation_labels": 2,
            "appellation_parts": 1,
            "set_aside_by_reason": {"initial": 2, "addition": 1},
            "containers_checked": 4,
            "containers_dropped_by_reason": {
                "evidence-not-in-source": 2,
                "no-evidence": 1,
            },
        }
        assert cert["notes"] == {
            "by_category": {"identity": 2, "date_reading": 3},
            "set_aside": 3,
        }
        deck = document(made, DECK)["coverage"]
        assert deck["records"] == {
            "given": 1,
            "recordless": 0,
            "by_status": {"name": 1},
            "unsplit_forms": None,
            "set_aside_records": None,
            "appellation_labels": None,
            "appellation_parts": None,
            "set_aside_by_reason": {},
            "containers_checked": None,
            "containers_dropped_by_reason": None,
        }
        assert deck["notes"] == {"by_category": {}, "set_aside": 0}
        for absent in (CARD, DIRECTORY):
            view = document(made, absent)["coverage"]
            assert (view["records"], view["notes"]) == (None, None)
        # the drill-down lists each count the most frequent first
        assert (
            "Mention records given: 3, by status: name 2, toponym 1; "
            "unsplit name forms: 3; appellation labels: 2; appellation "
            "parts: 1; recordless: 1; "
            "records set aside: 1; reasons set aside: initial 2, addition 1; "
            "place containers checked: 4; dropped: evidence-not-in-source "
            "2, no-evidence 1; judgement notes: "
            "date_reading 3, identity 2; notes set aside: 3"
        ) in drilled(made, CERT)
        predates = "not counted (its coverage gives no count of it)"
        assert (
            "Mention records given: 1, by status: name 1; unsplit name "
            f"forms: {predates}; appellation labels: {predates}; "
            f"appellation parts: {predates}; "
            "recordless: 0; records set aside: "
            f"{predates}; reasons set aside: none; place containers "
            f"checked: {predates}; dropped: not counted; judgement notes: "
            "none; notes set aside: 0"
        ) in drilled(made, DECK)
        assert (
            "Mention records given: 0, by status: none; unsplit name forms: "
            f"{predates}; appellation labels: {predates}; appellation parts: "
            f"{predates}; recordless: 1; "
            "records set aside: 1; reasons set "
            "aside: no toponym 1; place containers checked: "
            f"{predates}; dropped: not counted; judgement notes: kind 1, "
            "relation 1; notes set aside: 2"
        ) in drilled(made, POSTCARD)
        assert (
            "Mention records and judgement notes: not counted, as its "
            "coverage predates these counts"
        ) in drilled(made, CARD)
        assert (
            "Mention records and judgement notes: not counted, as it fell "
            "back to line ranges (all its mentions recordless)"
        ) in drilled(made, DIRECTORY)

    def test_the_counts_the_extraction_writes(self, tmp_path):
        """coverage.json as the extraction writes it (ADR-0035 §5), its
        document the only one counted. Hildegard's record gives two given
        names no form of hers holds: one record set aside, two reasons,
        and one recordless mention. Her note fails too: a note set aside,
        never a record's reason. Sebastian's record stands, its two name
        forms unsplit. No name the records or notes give, and no note's
        text, reaches the report, though the batch beside the coverage
        holds Sebastian's note."""
        extracted, written = extract(
            tmp_path / "extraction",
            HILDEGARD
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [{"written": "Klara"}, {"written": "Ida"}],
                },
                "note": {"category": "guess", "text": "read as Hildegard"},
            },
            SEBASTIAN
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "unsplit": ["Sebastian Löbig", "Sebastian"],
                },
                "note": {
                    "category": "identity",
                    "text": "the register's Sebastian read as the hatter",
                },
            },
        )
        assert written["records"]["set_aside"] == {"addition": 2, "note": 1}
        assert written["records"]["set_aside_records"] == 1
        assert written["records"]["unsplit_forms"] == 2
        assert written["records"]["appellation_labels"] == 0
        path = record(tmp_path / "run")
        for sha1 in (DIRECTORY, DECK, POSTCARD, CARD):
            directory = path / slugify(Path(FILES[sha1][0]).stem)
            directory.rename(path / f".{directory.name}.superseded-9")
        # the report as the fixture's own coverage gives it, whose file
        # names and labels hold Klara and Ida
        before = build(path, now=NOW)
        cert = path / slugify(Path(FILES[CERT][0]).stem)
        (cert / "extraction" / "coverage.json").write_text(json.dumps(written))
        held = extracted.model_dump_json()
        assert "hatter" in held
        (cert / "batch.json").write_text(held)
        made = build(path, now=NOW)
        content = made["aggregate"]["content"]
        assert content["records"] == {
            "extractions": 1,
            "given": 1,
            "recordless": 1,
            "by_status": {"name": 1},
            "unsplit_forms": 2,
            "set_aside_records": 1,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_reasons": 2,
            "set_aside_by_reason": {"addition": 2},
            "containers_checked": 0,
            "containers_dropped": 0,
            "containers_dropped_by_reason": {},
            "absent": {
                "predating": 0,
                "line_range": 0,
                "set_aside_records": 0,
                "unsplit_forms": 0,
                "appellation_labels": 0,
                "appellation_parts": 0,
                "containers": 0,
            },
        }
        assert content["notes"] == {
            "total": 1,
            "by_category": {"identity": 1},
            "set_aside": 1,
        }
        markdown = render_markdown(made["aggregate"])
        assert (
            "- Extractions counted: 1; mention records given: 1; recordless "
            "mentions (no record given, or it was set aside): 1\n"
            "- Unsplit name forms in the records given: 2\n"
            "- Labels that were appellations, replaced with the name the "
            "record gives: 0; record name parts that are appellations: 0\n"
            "- Records set aside: 1; reasons records were set aside: 2 (a "
            "record can give several)\n"
            "- Place containers checked: 0; dropped alone, their records "
            "standing, so counted apart from records set aside: 0\n"
            "- Judgement notes: 1; notes set aside: 1\n"
            "- Extractions not counted: 0 whose coverage predates these "
            "counts; 0 that fell back to line ranges (all their mentions "
            "recordless)\n"
            "- Extractions counted whose coverage gives no count of it, so "
            "left out of that count: records set aside 0; unsplit name "
            "forms 0; appellation labels 0; appellation parts 0; place "
            "containers 0\n\n"
            "| Record status | Records |\n"
            "|---|---:|\n"
            "| name | 1 |\n\n"
            "| Set-aside reason | Reasons |\n"
            "|---|---:|\n"
            "| addition | 2 |\n\n"
            "| Note category | Notes |\n"
            "|---|---:|\n"
            "| identity | 1 |\n\n"
        ) in markdown
        # nothing a record or a note says reaches report.md, report.json
        # (the aggregates, and each document's own view) or report.html:
        # not in the aggregates at all, and in the local report no more
        # often than the fixture's own file names and labels hold it
        assert (
            "Mention records given: 1, by status: name 1; unsplit name "
            "forms: 2; appellation labels: 0; appellation parts: 0; "
            "recordless: 1; records set "
            "aside: 1; reasons set aside: addition 2; place containers "
            "checked: 0; dropped: none; judgement notes: "
            "identity 1; notes set aside: 1"
        ) in drilled(made, CERT)
        said = ("hatter", "Klara", "Ida", "read as Hildegard")
        for text in (
            markdown,
            json.dumps(made["aggregate"], ensure_ascii=False),
        ):
            assert [word for word in said if word in text] == []
        for local, fixture in (
            (json.dumps(made), json.dumps(before)),
            (report.render_html(made), report.render_html(before)),
        ):
            assert [local.count(word) for word in said] == [
                fixture.count(word) for word in said
            ]
            assert "hatter" not in local and "read as Hildegard" not in local

    def test_the_aggregate_lists_each_count_most_frequent_first(
        self, tmp_path
    ):
        """The summed statuses, reasons and categories are listed the
        most frequent first: an order that is neither alphabetical nor
        that of the documents they were read from, whichever comes
        first."""
        path = record(tmp_path / "run")
        name = slugify(Path(FILES[POSTCARD][0]).stem)
        target = path / name / "extraction" / "coverage.json"
        value = json.loads(target.read_text())
        value.update(
            records=records(
                3,
                1,
                {"no toponym": 3, "schema": 1, "note": 2},
                {"toponym": 3, "role": 1},
                set_aside_records=1,
            ),
            notes={"relation": 4, "kind": 1},
        )
        target.write_text(json.dumps(value))
        made = build(path, now=NOW)
        content = made["aggregate"]["content"]
        assert list(content["records"]["by_status"].items()) == [
            ("toponym", 4),
            ("name", 3),
            ("role", 1),
        ]
        assert list(content["records"]["set_aside_by_reason"].items()) == [
            ("no toponym", 3),
            ("initial", 2),
            ("addition", 1),
            ("schema", 1),
        ]
        assert list(content["notes"]["by_category"].items()) == [
            ("relation", 4),
            ("date_reading", 3),
            ("identity", 2),
            ("kind", 1),
        ]
        assert (
            "| Record status | Records |\n"
            "|---|---:|\n"
            "| toponym | 4 |\n"
            "| name | 3 |\n"
            "| role | 1 |\n\n"
            "| Set-aside reason | Reasons |\n"
            "|---|---:|\n"
            "| no toponym | 3 |\n"
            "| initial | 2 |\n"
            "| addition | 1 |\n"
            "| schema | 1 |\n\n"
            "| Container dropped reason | Containers |\n"
            "|---|---:|\n"
            "| evidence-not-in-source | 2 |\n"
            "| no-evidence | 1 |\n\n"
            "| Note category | Notes |\n"
            "|---|---:|\n"
            "| relation | 4 |\n"
            "| date_reading | 3 |\n"
            "| identity | 2 |\n"
            "| kind | 1 |\n\n"
        ) in render_markdown(made["aggregate"])

    def test_a_coverage_that_counts_zero_is_not_absent(self, tmp_path):
        """Records and notes that count nothing are counted as zero; a
        coverage with records but no notes, or notes but no records,
        counts as absent, in its own view and its drill-down as in the
        aggregates."""
        path = record(tmp_path / "run")

        def rewrite(sha1, change) -> None:
            name = slugify(Path(FILES[sha1][0]).stem)
            target = path / name / "extraction" / "coverage.json"
            value = json.loads(target.read_text())
            change(value)
            target.write_text(json.dumps(value))

        rewrite(
            CARD,
            lambda v: v.update(
                records=records(
                    0,
                    0,
                    {},
                    set_aside_records=0,
                    unsplit_forms=0,
                    appellation_labels=0,
                    appellation_parts=0,
                    containers_checked=0,
                    containers_dropped={},
                ),
                notes={},
            ),
        )
        rewrite(CERT, lambda v: v.pop("notes"))
        rewrite(POSTCARD, lambda v: v.pop("records"))
        made = build(path, now=NOW)
        content = made["aggregate"]["content"]
        # the card, counting nothing, and the deck, one record given and
        # neither newer count
        assert content["records"] == {
            "extractions": 2,
            "given": 1,
            "recordless": 0,
            "by_status": {"name": 1},
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_reasons": 0,
            "set_aside_by_reason": {},
            "containers_checked": 0,
            "containers_dropped": 0,
            "containers_dropped_by_reason": {},
            "absent": {
                "predating": 2,
                "line_range": 1,
                "set_aside_records": 1,
                "unsplit_forms": 1,
                "appellation_labels": 1,
                "appellation_parts": 1,
                "containers": 1,
            },
        }
        assert content["notes"] == {
            "total": 0,
            "by_category": {},
            "set_aside": 0,
        }
        # each figure in its own place, and no table but the statuses'
        assert (
            "\n### Mention records and judgement notes\n\n"
            "- Extractions counted: 2; mention records given: 1; recordless "
            "mentions (no record given, or it was set aside): 0\n"
            "- Unsplit name forms in the records given: 0\n"
            "- Labels that were appellations, replaced with the name the "
            "record gives: 0; record name parts that are appellations: 0\n"
            "- Records set aside: 0; reasons records were set aside: 0 (a "
            "record can give several)\n"
            "- Place containers checked: 0; dropped alone, their records "
            "standing, so counted apart from records set aside: 0\n"
            "- Judgement notes: 0; notes set aside: 0\n"
            "- Extractions not counted: 2 whose coverage predates these "
            "counts; 1 that fell back to line ranges (all their mentions "
            "recordless)\n"
            "- Extractions counted whose coverage gives no count of it, so "
            "left out of that count: records set aside 1; unsplit name "
            "forms 1; appellation labels 1; appellation parts 1; place "
            "containers 1\n\n"
            "| Record status | Records |\n"
            "|---|---:|\n"
            "| name | 1 |\n\n"
            "### Zero-claim documents\n"
        ) in render_markdown(made["aggregate"])
        card = document(made, CARD)["coverage"]
        assert card["records"] == {
            "given": 0,
            "recordless": 0,
            "by_status": {},
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_by_reason": {},
            "containers_checked": 0,
            "containers_dropped_by_reason": {},
        }
        assert (
            "Mention records given: 0, by status: none; unsplit name forms: "
            "0; appellation labels: 0; appellation parts: 0; recordless: 0; "
            "records set aside: 0;"
        ) in drilled(made, CARD)
        assert card["notes"] == {"by_category": {}, "set_aside": 0}
        for half in (CERT, POSTCARD):
            view = document(made, half)["coverage"]
            assert (view["records"], view["notes"]) == (None, None)
            assert (
                "Mention records and judgement notes: not counted, as its "
                "coverage predates these counts"
            ) in drilled(made, half)

    def test_counts_of_other_shapes_count_nothing(self, tmp_path):
        """A records object whose given or recordless is no count counts
        it as zero; a newer count that is no count is absent; a reason,
        status or note category whose count is no count is dropped, never
        a 0 row, while a genuine 0 stays; a records or notes that is no
        object is absent."""
        path = record(tmp_path / "run")

        def rewrite(sha1, change) -> None:
            name = slugify(Path(FILES[sha1][0]).stem)
            target = path / name / "extraction" / "coverage.json"
            value = json.loads(target.read_text())
            change(value)
            target.write_text(json.dumps(value))

        rewrite(
            CERT,
            lambda v: v.update(
                records={
                    "given": "3",
                    "recordless": -2,
                    "by_status": {
                        "name": "2",
                        "toponym": -1,
                        "kinship": 1,
                        "role": 0,
                    },
                    "unsplit_forms": -1,
                    "set_aside_records": True,
                    "set_aside": {
                        "note": True,
                        "schema": 2.5,
                        "kind": 1,
                        "addition": 0,
                    },
                },
                notes={"identity": None, "kind": 2, "other": False},
            ),
        )
        rewrite(DECK, lambda v: v.update(records=[1, 2]))
        rewrite(POSTCARD, lambda v: v.update(notes=[["relation", 1]]))
        made = build(path, now=NOW)
        cert = document(made, CERT)["coverage"]
        assert cert["records"] == {
            "given": 0,
            "recordless": 0,
            "by_status": {"kinship": 1, "role": 0},
            "unsplit_forms": None,
            "set_aside_records": None,
            "appellation_labels": None,
            "appellation_parts": None,
            "set_aside_by_reason": {"kind": 1, "addition": 0},
            "containers_checked": None,
            "containers_dropped_by_reason": None,
        }
        assert cert["notes"] == {"by_category": {"kind": 2}, "set_aside": 0}
        for half in (DECK, POSTCARD):
            view = document(made, half)["coverage"]
            assert (view["records"], view["notes"]) == (None, None)
        content = made["aggregate"]["content"]
        assert content["records"] == {
            "extractions": 1,
            "given": 0,
            "recordless": 0,
            "by_status": {"kinship": 1, "role": 0},
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_reasons": 1,
            "set_aside_by_reason": {"kind": 1, "addition": 0},
            "containers_checked": 0,
            "containers_dropped": 0,
            "containers_dropped_by_reason": {},
            "absent": {
                "predating": 3,
                "line_range": 1,
                "set_aside_records": 1,
                "unsplit_forms": 1,
                "appellation_labels": 1,
                "appellation_parts": 1,
                "containers": 1,
            },
        }
        assert content["notes"] == {
            "total": 2,
            "by_category": {"kind": 2},
            "set_aside": 0,
        }

    @pytest.mark.parametrize(
        "written", [None, "{not json", "[]"], ids=["missing", "json", "list"]
    )
    def test_an_extraction_without_its_coverage_counts_nowhere(
        self, tmp_path, written
    ):
        """An extraction whose coverage.json is missing, holds no JSON or
        holds no object counts in none of the records' figures, those
        counted apart included, as in none of the rejects'."""
        path = record(tmp_path / "run")
        cert = path / slugify(Path(FILES[CERT][0]).stem)
        target = cert / "extraction" / "coverage.json"
        if written is None:
            target.unlink()
        else:
            target.write_text(written)
        made = build(path, now=NOW)
        assert document(made, CERT)["coverage"] is None
        content = made["aggregate"]["content"]
        counted = content["records"]
        assert counted["extractions"] == 2
        assert counted["absent"] == {
            "predating": 1,
            "line_range": 1,
            "set_aside_records": 1,
            "unsplit_forms": 2,
            "appellation_labels": 2,
            "appellation_parts": 2,
            "containers": 2,
        }
        assert content["rejects"]["extractions"] == 4

    def test_a_run_with_no_extraction_counts_none(self, tmp_path):
        """Documents set aside or never extracted count toward neither
        the records nor what is absent."""
        path = record(tmp_path / "run")
        for sha1 in (CERT, DIRECTORY, DECK, POSTCARD, CARD):
            directory = path / slugify(Path(FILES[sha1][0]).stem)
            directory.rename(path / f".{directory.name}.superseded-9")
        made = build(path, now=NOW)
        content = made["aggregate"]["content"]
        assert content["records"] == {
            "extractions": 0,
            "given": 0,
            "recordless": 0,
            "by_status": {},
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_reasons": 0,
            "set_aside_by_reason": {},
            "containers_checked": 0,
            "containers_dropped": 0,
            "containers_dropped_by_reason": {},
            "absent": {
                "predating": 0,
                "line_range": 0,
                "set_aside_records": 0,
                "unsplit_forms": 0,
                "appellation_labels": 0,
                "appellation_parts": 0,
                "containers": 0,
            },
        }
        assert content["notes"] == {
            "total": 0,
            "by_category": {},
            "set_aside": 0,
        }
        # no status counted, so no header-only status table
        assert "| Record status |" not in render_markdown(made["aggregate"])

    def test_the_vocabularies_are_the_writers(self):
        """The reasons the record checks give, on an input that triggers
        each, are the ones the report names; the note check's is the one
        it counts as a note set aside; and the note categories and record
        statuses are those docs/RUN_REPORT.md lists, so a change to
        JudgementNote's or to a record's fails here."""
        listed = {"kind", "relation", "date_reading", "identity", "other"}
        assert listed == report.NOTE_CATEGORIES
        person = {"id": "e1", "label": "Hildegard Laudler", "kind": "human"}
        records_given = [
            {"kind": "place", "status": "toponym"},
            {
                "kind": "human",
                "status": "name",
                "given": [{"written": "Klara"}],
            },
            {
                "kind": "human",
                "status": "name",
                "given": [
                    {"written": "H.", "initial": True, "standard": "Hildegard"}
                ],
            },  # fmt: skip
            {"kind": "human", "status": "name"},
            {"kind": "human", "status": "name", "nicknames": [""]},
        ]
        reasons = set()
        for structure in records_given:
            entity = {**person, "structure": structure}
            _, found = set_aside_structure(entity)
            reasons |= {reason.split(":", 1)[0] for reason in found}
        town = {"id": "e2", "label": "Flörburg", "kind": "place"}
        place = EntityRecord.model_validate(
            {**town, "structure": {"kind": "place", "status": "toponym"}}
        )
        reasons |= {r.split(":", 1)[0] for r in structure_problems(place)}
        _, found = set_aside_structure(
            {**person, "structure": {"kind": "human", "status": 7}}
        )
        reasons |= {reason.split(":", 1)[0] for reason in found}
        assert reasons == report.SET_ASIDE_REASONS
        _, found = _set_aside_note({**person, "note": {"category": "?"}})
        assert [r.split(":", 1)[0] for r in found] == [report.NOTE_SET_ASIDE]
        # every reason label the checks' source writes, so a new one fails
        # here until the report names it
        written = {
            label
            for check in (
                structure_problems,
                set_aside_structure,
                _clear_empty,
            )
            for label in re.findall(
                r"""f?["']([a-z ]+): """, inspect.getsource(check)
            )
        }
        assert written == report.SET_ASIDE_REASONS
        # the record statuses are those the records allow, all ten
        statuses = {
            "name",
            "role",
            "kinship",
            "courtesy",
            "placeholder",
            "description",
            "toponym",
            "deictic",
            "generic",
            "adjective",
        }
        assert statuses == report.RECORD_STATUSES

    def test_a_view_that_cannot_be_read_counts_nothing_and_is_noted(
        self, made
    ):
        card = document(made, CARD)
        assert card["critique"] is None
        assert card["unread"] == {
            "critique": "critique.json cannot be read (JSONDecodeError)"
        }
        assert stage_row(made, "critique")["views_not_read"] == 1
        assert made["aggregate"]["documents"]["views_not_read"] == 1

    def test_a_directory_set_aside_or_a_duplicate_holds_no_views(
        self, tmp_path
    ):
        path = record(tmp_path / "run")
        # the postcard's directory was set aside, and no other holds its
        # views: its old ones count nothing
        postcard = path / slugify(Path(FILES[POSTCARD][0]).stem)
        postcard.rename(path / f".{postcard.name}.superseded-1")
        made = build(path, now=NOW)
        assert document(made, POSTCARD)["unread"] == {
            stage: "no directory holds its views" for stage in EXTRACTED
        }
        content = made["aggregate"]["content"]
        assert OTHER not in content["claims"]["by_property"]
        # a file with another's bytes has a directory of its own, which
        # holds no views: the first file's directory does
        directory = document(made, DIRECTORY)
        assert directory["directory"] == slugify("Flörburg address book 1912")
        assert directory["batch"]["entities"] == {"human": 1, "place": 1}

    def test_what_the_run_directory_cannot_give_is_not_available(self, made):
        content = made["aggregate"]["content"]
        for key in ("depicts", "place_hierarchy", "contradictions", "flags"):
            assert content[key]["status"] == NOT_AVAILABLE
            assert content[key]["why"]
        text = render_markdown(made["aggregate"])
        assert "Flags by tier and class: not available" in text
        assert "Depicts claims left out: not available" in text


# -- calls, time and the run --------------------------------------------------


class TestCallsAndTime:
    def test_the_cache_s_hits_and_misses_by_reason(self, made):
        calls = made["aggregate"]["calls"]
        assert calls["hits_by_reason"] == {"key match": 8, "adopted": 1}
        assert calls["misses_by_reason"] == {
            "absent": 12,
            "rejected": 2,
            "refresh": 1,
        }
        # the misses and a replay reached the engine
        assert (calls["engine"], calls["cached"]) == (16, 9)
        assert (calls["replays"], calls["replays_changed"]) == (1, 1)
        assert calls["rejections"] == 1
        assert calls["by_role"]["extract"]["engine"] == 12

    def test_what_the_calls_reported_they_used(self, made):
        usage = made["aggregate"]["calls"]["usage"]
        zero = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        assert usage["by_stage"] == [
            {"stage": "critique", "role": "critique", "calls": 3,
             "reported": 1, "input_tokens": 700, "output_tokens": 50,
             "cache_creation_input_tokens": 90,
             "cache_read_input_tokens": 0},
            {"stage": "extraction", "role": "extract", "calls": 12,
             "reported": 3, "input_tokens": 1500, "output_tokens": 300}
            | zero,
            {"stage": "transcription", "role": "transcribe", "calls": 1,
             "reported": 1, "input_tokens": 1200, "output_tokens": 300,
             "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 800},
        ]  # fmt: skip
        assert usage["total"]["reported"] == 5
        assert usage["total"]["input_tokens"] == 3400
        models = made["aggregate"]["calls"]["engine_calls"]["models"]
        assert models == {SONNET: 3, OPUS: 2, OTHER: 1}
        text = render_markdown(made["aggregate"])
        assert "| all |  | 16 | 5 | 3,400 | 650 | 90 | 800 |" in text

    def test_stops_at_the_output_limit_by_stage(self, made, tmp_path):
        """The calls stopped at the output limit, per stage, from the call
        events (#390): an answer whose stop reason is max_tokens is cut
        off; a call that raised OutputLimitReached was given up on, told by
        its error alone, never by its API error's class. An answer that
        ended otherwise is no stop."""
        stops = [
            call(7400, DIRECTORY, "extract", THIRD, 1.0,
                 stop_reason="max_tokens"),
            call(7401, DIRECTORY, "extract", THIRD, 1.0, outcome="error",
                 error="OutputLimitReached", api_error="max_output_tokens",
                 stop_reason="stop_sequence"),
            call(7402, DIRECTORY, "extract", THIRD, 1.0, outcome="error",
                 error="OutputLimitReached"),
            call(7403, CERT, "critique", THIRD, 1.0,
                 stop_reason="max_tokens"),
            # a crash after the notice: an error of another class
            call(7404, CERT, "critique", THIRD, 1.0, outcome="error",
                 error="RuntimeError", api_error="max_output_tokens"),
            call(7405, CERT, "transcribe", THIRD, 1.0,
                 stop_reason="end_turn"),
            call(7406, CERT, "transcribe", THIRD, 1.0,
                 stop_reason="stop_sequence"),
        ]  # fmt: skip
        built = build(record(tmp_path / "run", [*STREAM, *stops]), now=NOW)
        aggregate = built["aggregate"]
        assert aggregate["calls"]["output_limit_stops"] == {
            "critique": {"cut_off": 1, "gave_up": 0},
            "extraction": {"cut_off": 1, "gave_up": 2},
        }
        assert (
            "Stops at the output limit by stage: critique: 1 cut off, 0 "
            "given up; extraction: 1 cut off, 2 given up"
        ) in render_markdown(aggregate)

    def test_stops_of_the_api_engine_are_recorded(self, tmp_path, monkeypatch):
        """A run on the Anthropic API engine records why its answered calls
        stopped and the model that answered them, as its responses said:
        an answer cut off at the output limit is counted, a refusal is a
        recorded stop but no cut-off, and neither the stops nor the models
        read not recorded (#441)."""
        from tests.fixtures.fake_anthropic import api_engine

        stops: list = []

        def emit(name, **fields):
            stops.append(
                event(7400 + len(stops), name, THIRD, subject=CERT,
                      kind=FILES[CERT][1], **fields)
            )  # fmt: skip

        engine = api_engine(
            monkeypatch,
            ("Sebastian Löbig, Hutmacher", "max_tokens"),
            ("Sebastian Löbig, Hutmacher, Flörburg", "end_turn"),
            ("I cannot help with that.", "refusal"),
            ConnectionError("the network is down"),
        )
        client = llm.observed_client(engine, role="extract", events=emit)
        client.complete("Extract the claims.")
        client.complete("Extract the claims.")
        client.complete("Extract the claims.")
        with pytest.raises(ConnectionError):
            client.complete("Extract the claims.")
        built = build(record(tmp_path / "run", [*STREAM, *stops]), now=NOW)
        aggregate = built["aggregate"]
        assert aggregate["calls"]["output_limit_stops"] == {
            "extraction": {"cut_off": 1, "gave_up": 0}
        }
        assert (
            "Stops at the output limit by stage: extraction: 1 cut off, 0 "
            "given up"
        ) in render_markdown(aggregate)
        before = build(record(tmp_path / "before", STREAM), now=NOW)
        models = Counter(aggregate["calls"]["engine_calls"]["models"])
        models.subtract(before["aggregate"]["calls"]["engine_calls"]["models"])
        assert +models == {"claude-sonnet-5-20260901": 3}

    def test_stops_not_recorded_are_not_read_as_none(self, made):
        """A run whose call events carry no stop reason (one made before
        the engine recorded why a call stopped) does not say that none
        stopped at the output limit: it says the stops were not recorded,
        as its usage does (#390)."""
        calls = made["aggregate"]["calls"]
        assert calls["output_limit_stops"] == {"recorded": False}
        text = render_markdown(made["aggregate"])
        assert (
            "Stops at the output limit by stage: not recorded: the run's "
            "call events carry no stop reason"
        ) in text
        assert "Stops at the output limit by stage: none" not in text

    @pytest.mark.parametrize(
        "recorded",
        [
            {"stop_reason": "end_turn"},
            {"api_error": "overloaded_error", "outcome": "error",
             "error": "EngineError"},
        ],
    )  # fmt: skip
    def test_stops_recorded_but_none_read_as_none(self, tmp_path, recorded):
        """A run whose call events record why their calls stopped, none at
        the output limit, has no stops: "none" (#390)."""
        fields = {"outcome": "ok"} | recorded
        outcome = fields.pop("outcome")
        stream = [
            *STREAM,
            call(7400, CERT, "transcribe", THIRD, 1.0, outcome, **fields),
        ]
        aggregate = build(record(tmp_path / "run", stream), now=NOW)[
            "aggregate"
        ]
        assert aggregate["calls"]["output_limit_stops"] == {}
        assert "Stops at the output limit by stage: none" in (
            render_markdown(aggregate)
        )

    def test_a_give_up_alone_records_stops(self, tmp_path):
        """A give-up whose call event carries no stop reason and no API
        error still records the run's stops: it is counted, not read as
        "not recorded" (#390)."""
        stream = [
            *STREAM,
            call(7400, CERT, "extract", THIRD, 1.0, "error",
                 error="OutputLimitReached"),
        ]  # fmt: skip
        aggregate = build(record(tmp_path / "run", stream), now=NOW)[
            "aggregate"
        ]
        assert aggregate["calls"]["output_limit_stops"] == {
            "extraction": {"cut_off": 0, "gave_up": 1}
        }

    def test_give_ups_replayed_from_the_cache_by_stage(self, made, tmp_path):
        """A hit on a give-up the cache kept made no call, so it is no stop
        at the output limit: the cache section counts it per stage of its
        role, apart from the stops (#390). A hit whose gave_up is not true
        is no give-up."""
        assert made["aggregate"]["calls"]["give_ups_replayed"] == {}
        assert (
            "Give-ups replayed from the cache by stage (no call made): none"
        ) in render_markdown(made["aggregate"])
        hits = [
            cache("cache-hit", 7400, DIRECTORY, "extract", THIRD,
                  call="open", reason="key match", gave_up=True),
            cache("cache-hit", 7401, DIRECTORY, "extract", THIRD,
                  call="open", reason="key match", gave_up=True),
            cache("cache-hit", 7402, CERT, "critique", THIRD,
                  call="complete", reason="key match", gave_up=True),
            cache("cache-hit", 7403, CERT, "critique", THIRD,
                  call="complete", reason="key match", gave_up="true"),
            cache("cache-hit", 7404, CERT, "transcribe", THIRD,
                  call="complete", reason="key match"),
        ]  # fmt: skip
        built = build(record(tmp_path / "run", [*STREAM, *hits]), now=NOW)
        calls = built["aggregate"]["calls"]
        assert calls["give_ups_replayed"] == {"extraction": 2, "critique": 1}
        assert calls["output_limit_stops"] == {"recorded": False}
        assert calls["cached"] == made["aggregate"]["calls"]["cached"] + 5
        assert (
            "Give-ups replayed from the cache by stage (no call made): "
            "extraction 2, critique 1"
        ) in render_markdown(built["aggregate"])

    def test_a_run_without_the_cache_counts_its_call_events(self, tmp_path):
        """A run made without the LLM cache (`ingest run --no-cache`) has
        no cache events: its calls that reached the engine are its call
        events, as the monitor counts them."""
        plan = manifest()
        plan["cache"] = None
        entries = [
            entry
            for entry in STREAM
            if not str(entry[1].get("event")).startswith("cache-")
        ]
        made = build(record(tmp_path / "run", entries, plan=plan), now=NOW)
        calls = made["aggregate"]["calls"]
        assert (calls["engine"], calls["cached"], calls["cache_share"]) == (
            16,
            0,
            None,
        )
        for row in made["aggregate"]["stages"]:
            assert row["engine_calls"] == row["call_events"]
            assert row["cache_share"] is None
        assert stage_row(made, "extraction")["engine_calls"] == 12
        assert "16 calls reached the engine, as the engine's call events " in (
            render_markdown(made["aggregate"])
        )

    def test_the_engine_s_call_events(self, made):
        events = made["aggregate"]["calls"]["engine_calls"]
        assert events["count"] == 16
        assert events["outcomes"] == {"ok": 13, "error": 2, "paused": 1}
        assert events["errors"] == {"EngineError": 2, "UsageLimitReached": 1}
        assert events["holds"] == {"usage limit": 1}
        assert (events["waits"], events["refused"]) == (1, 2)
        assert events["seconds"] == 151.0

    def test_wall_time_sums_the_invocations(self, made):
        run = made["aggregate"]["run"]
        assert run["seconds"] == 600 + 100 + 200
        assert run["invocations"] == 3
        assert run["ends"] == {"finished": 1, "paused": 1, "stopped": 1}
        assert run["state"] == "finished"
        assert run["complete"] is True

    def test_the_run_s_settings(self, made):
        run = made["aggregate"]["run"]
        assert run["code"]["commit"] == "0123456789ab"
        assert run["engine"] == {
            "name": "claude-code",
            "cli_version": "2.1.237",
        }
        assert run["roles"]["extract"]["refresh"] is True
        assert run["roles"]["critique"]["epoch"] == 1
        assert run["roles"]["critique"]["effort"] == "high"

    def test_the_control_requests(self, made):
        control = made["aggregate"]["control"]
        assert control["by_action"] == {"pause": 2, "resume": 2, "abort": 1}
        assert control["aborts"] == {"kind:presentation": 1}
        assert control["by_requester"]["anomaly"] == 1


# -- runs in other states -----------------------------------------------------


def in_state(tmp_path: Path, state: str) -> tuple[Path, dict, datetime]:
    """The fixture run recorded in `state` (run_in), its report, and when
    the report was made."""
    entries, plan, now = run_in(state)
    path = record(tmp_path / state.replace(" ", "-"), entries, plan=plan)
    return path, build(path, now=now), now


def monitor_rows(path: Path, now: datetime) -> dict[str, tuple]:
    """The live monitor's stage counts, by stage, in the report's columns:
    its pending less the held and the interrupted, which it counts among
    them; and its done and reused together, as completed."""
    monitor = RunMonitor(path)
    monitor.refresh()
    return {
        stage.name: (
            stage.pending - stage.held - stage.interrupted,
            stage.held,
            stage.interrupted,
            stage.running,
            stage.done + sum(stage.reused.values()),
            stage.attention,
            stage.failed,
        )
        for stage in monitor.summary(now).stages
    }


def report_rows(made: dict) -> dict[str, tuple]:
    """The report's stage counts, by stage, as monitor_rows gives the
    monitor's: computed and reused together, as completed."""
    return {
        row["name"]: (
            row[PENDING],
            row[HELD],
            row[INTERRUPTED],
            row[RUNNING],
            row[COMPUTED] + row[REUSED],
            row[NEEDS_ATTENTION],
            row[FAILED],
        )
        for row in made["aggregate"]["stages"]
    }


class TestRunStates:
    @pytest.mark.parametrize("state", STATES)
    def test_the_statuses_agree_with_run_state(self, tmp_path, state):
        path, made, _now = in_state(tmp_path, state)
        assert disagreement(path, made) is None

    @pytest.mark.parametrize("state", STATES)
    def test_the_statuses_agree_with_the_monitor(self, tmp_path, state):
        path, made, now = in_state(tmp_path, state)
        assert report_rows(made) == monitor_rows(path, now)

    def test_a_running_run_is_a_snapshot(self, tmp_path):
        _path, made, _now = in_state(tmp_path, "running")
        aggregate = made["aggregate"]
        assert aggregate["run"]["state"] == "running"
        # to now, the running invocation's time
        assert aggregate["run"]["seconds"] == 600 + 100
        assert aggregate["run"]["ends"] == {"paused": 1, "running": 1}
        postcard = document(made, POSTCARD)["stages"]
        assert postcard["extraction"]["status"] == RUNNING
        assert postcard["critique"]["status"] == PENDING
        assert stage_row(made, "extraction")[RUNNING] == 1
        text = render_markdown(aggregate)
        assert "The run is still running" in text
        assert leaks(text) == []

    def test_a_stage_the_ended_invocation_left_started_is_interrupted(
        self, tmp_path
    ):
        _path, made, _now = in_state(tmp_path, "paused")
        postcard = document(made, POSTCARD)["stages"]["extraction"]
        assert postcard["status"] == INTERRUPTED
        assert stage_row(made, "extraction")[INTERRUPTED] == 1
        assert made["aggregate"]["run"]["state"] == "paused"
        assert made["aggregate"]["run"]["reason"] == "UsageLimitReached"

    def test_a_stage_done_as_its_invocation_ends_between_the_reads(
        self, tmp_path, monkeypatch
    ):
        """The latest invocation finishes a stage and ends just after the
        report has read the events: the stage is done, never read as cut
        short by an end that came after its done."""
        path = tmp_path / "run"
        planned = {"sha1": CERT, "file": FILES[CERT][0], "kind": "media"}
        run = RunDir(path)
        run.start(
            run_id=RUN_ID,
            mode="scratch run",
            documents=[planned | {"stages": ["transcription"]}],
        )
        where = {"subject": CERT, "kind": "media", "stage": "transcription"}
        run.emit("started", **where)
        lines, reads = report._lines, []

        def racing(log):
            yield from lines(log)
            if log.name == "events.jsonl":
                reads.append(log)
                if len(reads) == 1:
                    run.emit("done", **where)
                    run.end("finished")
                    run.emit("run-end", outcome="finished")

        monkeypatch.setattr(report, "_lines", racing)
        made = build(path, now=NOW)
        assert document(made, CERT)["stages"]["transcription"]["status"] == (
            DONE
        )
        # one reading disagreed, and the one after it agreed
        assert len(reads) == 2

    def test_an_aborted_run_holds_what_the_abort_held(self, tmp_path):
        _path, made, _now = in_state(tmp_path, "stopped")
        run = made["aggregate"]["run"]
        assert (run["state"], run["reason"]) == (
            "stopped",
            "kind:presentation aborted",
        )
        assert run["barrier"] is None
        deck = document(made, DECK)["stages"]
        assert deck["transcription"]["status"] == HELD
        assert deck["transcription"]["reason"].startswith("kind:")
        assert stage_row(made, "extraction")[HELD] == 1
        assert made["aggregate"]["corpus_stages"] == [
            {
                "name": "clustering",
                "state": NOT_RUN,
                "attention": {},
                "failure": None,
                "seconds": None,
            }
        ]
        text = render_markdown(made["aggregate"])
        assert "stopped: kind:presentation aborted" in text
        assert leaks(text) == []

    def test_a_killed_invocation_runs_to_its_last_event(self, tmp_path):
        _path, made, _now = in_state(tmp_path, "killed first")
        run = made["run"]
        assert run["invocations"][0]["end"] == "killed"
        assert run["invocations"][0]["seconds"] == 170
        assert made["aggregate"]["run"]["ends"]["killed"] == 1

    def test_a_killed_latest_invocation_reads_as_running(self, tmp_path):
        """A killed invocation records no end and has no closing event, so
        while it is the latest it reads as running, as in the monitor."""
        _path, made, _now = in_state(tmp_path, "killed latest")
        aggregate = made["aggregate"]
        assert aggregate["run"]["state"] == "running"
        assert aggregate["corpus_stages"][0]["state"] == RUNNING
        # its time runs to now, as a running invocation's does
        assert aggregate["run"]["seconds"] == 600 + 100 + (10800 - 7200)

    def test_an_empty_run(self, tmp_path):
        path = tmp_path / "run"
        RunDir(path).start(run_id=RUN_ID, mode="scratch run", documents=[])
        made = build(path, now=NOW)
        aggregate = made["aggregate"]
        assert aggregate["documents"]["planned"] == 0
        assert aggregate["stages"] == []
        assert aggregate["calls"]["usage"] == {"recorded": False}
        text = render_markdown(aggregate)
        assert "No stage planned or seen." in text
        assert "0 documents planned" in text
        assert "Usage: not recorded." in text
        assert "Calls by the model the engine reported: not recorded" in text

    def test_a_run_that_never_started(self, tmp_path):
        path = tmp_path / "run"
        path.mkdir()
        (path / "run.json").write_text(json.dumps({"run_id": RUN_ID}))
        run = build(path, now=NOW)["aggregate"]["run"]
        assert (run["state"], run["invocations"]) == ("not started", 0)


# -- reading and writing ------------------------------------------------------


class TestFiles:
    def test_the_three_files_go_into_a_private_folder_of_the_run(self, run):
        written = write_report(run, now=NOW)
        folder = run / REPORT_DIR
        assert set(written) == {"report.json", "report.html", "report.md"}
        assert all(path.parent == folder for path in written.values())
        assert os.stat(folder).st_mode & 0o777 == 0o700
        for path in written.values():
            assert os.stat(path).st_mode & 0o777 == 0o600
        loaded = json.loads(written["report.json"].read_text())
        assert loaded["aggregate"]["run"]["run_id"] == RUN_ID
        # written again, each is replaced whole
        assert write_report(run, now=NOW) == written

    def test_a_folder_there_already_is_made_private(self, run):
        (run / REPORT_DIR).mkdir(mode=0o755)
        os.chmod(run / REPORT_DIR, 0o755)
        write_report(run, now=NOW)
        assert os.stat(run / REPORT_DIR).st_mode & 0o777 == 0o700

    def test_no_document_directory_can_take_the_folder_s_name(self):
        assert REPORT_DIR.startswith(".")
        assert slugify(REPORT_DIR) != REPORT_DIR

    def test_the_html_drills_down_to_every_document(self, run):
        page = write_report(run, now=NOW)["report.html"].read_text()
        for sha1 in FILES:
            assert f'id="doc-{sha1[:12]}"' in page
        lists = {
            title: re.findall(r'href="#doc-([0-9a-f]{12})"', links)
            for title, links in re.findall(
                r"<p><strong>(.*?)</strong> \(\d+\): (.*?)</p>", page
            )
        }
        # failed now, as the aggregates count it: the deck failed in the
        # first invocation and was made in the third
        assert lists == {
            "Failed": [INTERVIEW[:12]],
            "Needs attention": [DIRECTORY[:12], CARD[:12]],
            "With alerts": [
                CERT[:12],
                DIRECTORY[:12],
                CARD[:12],
                INTERVIEW[:12],
            ],
            "With a view that could not be read": [CARD[:12]],
        }
        assert "Hutmacherei zum Blauen Hut.pptx" in page
        assert "&lt;script" not in page and "<script" not in page
        assert "critique.json cannot be read (JSONDecodeError)" in page

    def test_html_escapes_what_it_shows(self, tmp_path):
        plan = manifest()
        plan["documents"][0]["file"] = "<script>Löbig</script>.jpg"
        path = record(tmp_path / "run", plan=plan)
        page = write_report(path, now=NOW)["report.html"].read_text()
        assert "<script>" not in page
        assert "&lt;script&gt;Löbig&lt;/script&gt;.jpg" in page

    def test_a_folder_swapped_once_opened_takes_no_file(
        self, run, tmp_path, monkeypatch
    ):
        """The folder, swapped for a link to another just after it was
        opened and made private: the files go into the folder that was
        opened, and none into the one the link names."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        opened = run / ".report-opened"
        private = report._private_folder

        def then_swapped(folder):
            descriptor = private(folder)
            folder.rename(opened)
            folder.symlink_to(elsewhere)
            return descriptor

        monkeypatch.setattr(report, "_private_folder", then_swapped)
        write_report(run, now=NOW)
        assert list(elsewhere.iterdir()) == []
        assert sorted(path.name for path in opened.iterdir()) == [
            "report.html",
            "report.json",
            "report.md",
        ]

    def test_a_folder_that_is_a_link_is_refused(self, run, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o755)
        os.chmod(elsewhere, 0o755)
        (run / REPORT_DIR).symlink_to(elsewhere)
        with pytest.raises(ReportError, match="not a folder of its own"):
            write_report(run, now=NOW)
        assert list(elsewhere.iterdir()) == []
        # and the folder it names keeps its mode
        assert os.stat(elsewhere).st_mode & 0o777 == 0o755

    def test_a_report_folder_that_is_a_file_is_refused(self, run):
        (run / REPORT_DIR).write_text("a file of its own")
        with pytest.raises(ReportError, match="not a folder of its own"):
            write_report(run, now=NOW)

    def test_no_run_json_no_report(self, tmp_path):
        with pytest.raises(ReportError, match="no run.json"):
            build(tmp_path)

    @pytest.mark.parametrize(
        "text",
        ['{"run_id": "20260929T090000Z-0a1b2c", ', "[1, 2]", '"a run"'],
        ids=["torn", "a list", "a string"],
    )
    def test_a_run_json_that_holds_no_object_is_refused(self, run, text):
        (run / "run.json").write_text(text)
        with pytest.raises(ReportError, match="run.json"):
            build(run, now=NOW)

    def test_a_run_json_it_cannot_read_is_refused(self, run):
        (run / "run.json").chmod(0)
        try:
            if os.access(run / "run.json", os.R_OK):
                pytest.skip("this process reads a file whatever its mode")
            with pytest.raises(ReportError, match="cannot be read"):
                build(run, now=NOW)
        finally:
            (run / "run.json").chmod(0o600)

    def test_fields_of_other_shapes_are_passed_over(self, run):
        """A batch whose entities or claims are no list, and a run.json
        whose corpus stages are none, count nothing, and the report goes
        on."""
        directory = run / slugify(Path(FILES[CERT][0]).stem)
        batch = json.loads((directory / "batch.json").read_text())
        batch |= {"entities": 1, "claims": 7}
        (directory / "batch.json").write_text(json.dumps(batch))
        plan = json.loads((run / "run.json").read_text())
        plan["scratch"]["corpus_stages"] = 5
        (run / "run.json").write_text(json.dumps(plan))
        made = build(run, now=NOW)
        assert document(made, CERT)["batch"]["entities"] == {}
        assert document(made, CERT)["batch"]["claims"] == {}
        assert made["run"]["corpus_stages"] == []
        assert leaks(render_markdown(made["aggregate"])) == []

    def test_malformed_files_never_stop_the_report(self, tmp_path):
        """The fixture run with leaves of its files swapped for values of
        other types, at random from fixed seeds: the report is made and
        rendered every time, or refused (ReportError) when run.json itself
        holds no object."""
        base = record(tmp_path / "base")
        odd = [None, True, False, 0, -1, 1.5, float("inf"), "", "x", [],
               {}, [1], {"a": 1}, "Laudler", 10**30]  # fmt: skip
        refused = 0
        for seed in range(50):
            rng = random.Random(seed)
            path = tmp_path / f"round-{seed}"
            shutil.copytree(base, path)
            scramble(path, rng, odd, rng.choice([0.02, 0.05, 0.1]))
            try:
                made = build(path, now=NOW)
            except ReportError as exc:
                assert "run.json" in str(exc)
                refused += 1
                continue
            render_markdown(made["aggregate"])
            report.render_html(made)
            report.render_json(made)
        assert refused < 50

    def test_an_event_log_it_cannot_read_is_refused(self, run):
        (run / "events.jsonl").chmod(0)
        try:
            if os.access(run / "events.jsonl", os.R_OK):
                pytest.skip("this process reads a file whatever its mode")
            with pytest.raises(ReportError, match="cannot be read"):
                build(run, now=NOW)
        finally:
            (run / "events.jsonl").chmod(0o600)

    def test_alerts_or_requests_it_cannot_read_are_not_available(self, run):
        logs = [run / "alerts.jsonl", run / "control.jsonl"]
        for log in logs:
            log.chmod(0)
        try:
            if os.access(logs[0], os.R_OK):
                pytest.skip("this process reads a file whatever its mode")
            aggregate = build(run, now=NOW)["aggregate"]
        finally:
            for log in logs:
                log.chmod(0o600)
        assert aggregate["anomalies"] == {
            "status": NOT_AVAILABLE,
            "why": "alerts.jsonl cannot be read",
        }
        assert aggregate["control"]["status"] == NOT_AVAILABLE
        text = render_markdown(aggregate)
        assert "not available: alerts.jsonl cannot be read." in text
        assert "not available: control.jsonl cannot be read." in text
        # the stages, from the events, are there all the same
        assert stage_row({"aggregate": aggregate}, "gedcom")[COMPUTED] == 1

    def test_a_torn_last_line_and_a_foreign_one_are_skipped(self, run):
        with open(run / "events.jsonl", "a") as handle:
            handle.write("[1, 2]\n\n" + '{"event": "started", "subj')
        assert build(run, now=NOW)["run"]["events"] == len(
            [e for e in STREAM if e[0] == "events.jsonl"]
        )

    def test_a_huge_event_log_is_read_a_line_at_a_time(self, tmp_path):
        path = tmp_path / "run"
        documents = [
            {
                "sha1": _sha1(f"fictional document {n}"),
                "file": f"document {n}.txt",
                "kind": "text",
                "stages": EXTRACTED,
            }
            for n in range(200)
        ]
        RunDir(path).start(
            run_id=RUN_ID, mode="scratch run", documents=documents
        )
        invocation = RunDir(path).manifest()["invocations"][0]["invocation"]
        lines = []
        for n in range(60):
            for doc in documents:
                sha1 = doc["sha1"]
                common = {"invocation": invocation, "subject": sha1,
                          "kind": "text", "at": at(n)}  # fmt: skip
                lines.append({"event": "started", "stage": "extraction"})
                lines[-1] |= common
                lines.append(
                    common | {"event": "cache-hit", "role": "extract",
                              "call": "fork", "reason": "key match",
                              "key": "0" * 64}
                )  # fmt: skip
                lines.append(common | {"event": "done", "stage": "extraction"})
        with open(path / "events.jsonl", "a") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        size = (path / "events.jsonl").stat().st_size
        assert size > 6 * 1024**2
        tracemalloc.start()
        try:
            made = build(path, now=NOW)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # read whole, the log would take several times its size
        assert peak < size / 3
        assert made["run"]["events"] == len(lines) + 1
        assert stage_row(made, "extraction")[REUSED] == 200


# -- the command, and what it asks of nothing ---------------------------------


class TestCommand:
    def test_it_writes_the_report_of_the_run_it_names(self, tmp_path, capsys):
        runs = tmp_path / "runs"
        path = record(runs / RUN_ID)
        assert cli_run("scratch", "report", str(path)) == 0
        out = capsys.readouterr().out
        for name in ("report.md", "report.html", "report.json"):
            assert str(path / REPORT_DIR / name) in out
        assert leaks((path / REPORT_DIR / "report.md").read_text()) == []
        # by its id in the runs directory, and the latest by default
        assert cli_run("scratch", "report", RUN_ID, "--runs", str(runs)) == 0
        assert cli_run("scratch", "report", "--runs", str(runs)) == 0

    def test_a_directory_that_holds_no_run_is_refused(self, tmp_path):
        with pytest.raises(SystemExit, match="no run there"):
            cli_run("scratch", "report", str(tmp_path / "nothing"))

    def test_it_asks_nothing_of_the_wiki_or_the_engine(
        self, run, request, monkeypatch
    ):
        asked = request.getfixturevalue("refusals")

        def no_engine(*args, **kwargs):
            raise AssertionError("the report asked for an engine")

        monkeypatch.setattr(llm, "make_client", no_engine)
        write_report(run, now=NOW)
        assert asked.asked == []


# -- what the driver writes ---------------------------------------------------


class TestAScratchRun:
    def test_it_reads_what_the_driver_wrote(self, tmp_path, request):
        engine = request.getfixturevalue("fakes")
        corpus = small(tmp_path)
        first = a_run(tmp_path, corpus, engine)
        assert first.run() == 0
        made = build(first.state_dir, now=NOW)
        transcription = stage_row(made, "transcription")
        # the photographs are read by the engine; the letters are their
        # own transcriptions
        assert transcription[COMPUTED] == 4
        assert transcription["forced"] == {"absent": 2, "no call": 2}
        assert stage_row(made, "extraction")["forced"] == {"absent": 4}
        # the fake critic is asked the same of every batch, which holds
        # the same entity and claim: the cache answers all but the first
        critique = stage_row(made, "critique")
        assert (critique[COMPUTED], critique["reused_by"]) == (
            1,
            {"key match": 3},
        )
        content = made["aggregate"]["content"]
        assert content["entities"]["by_kind"] == {"human": 4}
        assert content["claims"]["by_property"] == {"date_of_birth": 4}
        # the counts of mention records and notes the extraction writes
        # (#420): read from each, none absent; the fake manifest gives
        # its person a record of status `name`, and no note
        assert content["records"] == {
            "extractions": 4,
            "given": 4,
            "recordless": 0,
            "by_status": {"name": 4},
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "appellation_labels": 0,
            "appellation_parts": 0,
            "set_aside_reasons": 0,
            "set_aside_by_reason": {},
            "containers_checked": 0,
            "containers_dropped": 0,
            "containers_dropped_by_reason": {},
            "absent": {
                "predating": 0,
                "line_range": 0,
                "set_aside_records": 0,
                "unsplit_forms": 0,
                "appellation_labels": 0,
                "appellation_parts": 0,
                "containers": 0,
            },
        }
        assert content["notes"] == {
            "total": 0,
            "by_category": {},
            "set_aside": 0,
        }
        assert made["aggregate"]["run"]["complete"] is True
        assert made["aggregate"]["run"]["barrier"] == {
            "documents": 4,
            "failed": 0,
        }
        # a second run, from fresh directories: the cache answers its calls
        second = Driver(corpus.folder, runs_dir(tmp_path) / "run-2")
        assert second.run() == 0
        written = write_report(second.state_dir, now=NOW)
        made = json.loads(written["report.json"].read_text())
        transcription = stage_row(made, "transcription")
        assert transcription[REUSED] == 2
        assert transcription["reused_by"] == {"key match": 2}
        assert stage_row(made, "extraction")["reused_by"] == {"key match": 4}
        assert made["aggregate"]["calls"]["engine"] == 0
        text = written["report.md"].read_text()
        assert "Löbig" not in text and "Eugen" not in text
        for name in (TEXT, PHOTO, LETTER, STONE):
            assert name not in text and Path(name).stem not in text


def test_the_write_section_counts_the_judgement_notes_written():
    """The write stage's notes written, summed over its documents (#410),
    in the report's write section; a document record without the count, as
    a write before it was kept gives, adds nothing."""
    written = report._aggregate_write(
        {
            "documents": {
                "a": {"written": 3, "notes_written": 2},
                "b": {"written": 1, "notes_written": 1},
                "c": {"written": 1},
            }
        }
    )
    assert written["notes_written"] == 3
    (section,) = report._write_sections(
        {
            "write": written,
            "depicts": {"left_out": 0},
            "place_hierarchy": {"status": report.NOT_AVAILABLE},
        }
    )
    ((line, _),) = section.blocks
    assert "0 items minted, 3 judgement notes written." in line


class TestWhatSourcesSayOfArtifacts:
    """The run report counts, as aggregates, the captions given, the
    claims a document gives about itself by property, and the rejects by
    reason (#456, ADR-0039 §8)."""

    @pytest.fixture
    def counted(self, tmp_path) -> dict:
        path = record(tmp_path / RUN_ID)
        directory = path / slugify(Path(FILES[CERT][0]).stem)
        view = json.loads((directory / "batch.json").read_text())
        view["claims"] += [
            claim("e1", "caption", "Wastl to Klara, July 1939.", CERT),
            claim("e1", "inception", "28. Juli 1939", CERT),
            claim("e1", "inception", "1905", CERT),
            claim("e1", "shows", "Muckel", CERT),
        ]
        (directory / "batch.json").write_text(json.dumps(view))
        covered = directory / "extraction" / "coverage.json"
        counts = json.loads(covered.read_text())
        counts["rejected_by_reason"] = {
            "placed_subject": 2,
            "schema": 1,
            HOSTILE: 1,
        }
        covered.write_text(json.dumps(counts))
        return build(path, now=NOW)

    def test_captions_and_a_document_s_own_claims(self, counted):
        content = counted["aggregate"]["content"]
        assert content["container_claims"] == {
            "captions": 1,
            "document_subject_by_property": {"inception": 2, "shows": 1},
        }

    def test_rejects_by_reason(self, counted):
        rejects = counted["aggregate"]["content"]["rejects"]
        assert rejects["by_reason"] == {
            "placed_subject": 2,
            "schema": 1,
            OTHER: 1,
        }
        # the other extractions' coverage, written before it counted
        # reasons, counts none
        assert rejects["by_reason_absent"] == rejects["extractions"] - 1

    def test_report_md_gives_them_by_name_alone(self, counted):
        text = render_markdown(counted["aggregate"])
        assert (
            "Captions given, a container's words about an image it places: "
            "1. Claims a document gives about itself, by property: "
            "inception 2, shows 1."
        ) in text
        assert "Rejects by reason: placed_subject 2, other 1, schema 1" in (
            text
        )
        assert leaks(text) == []
        assert "Wastl" not in text and HOSTILE not in text
