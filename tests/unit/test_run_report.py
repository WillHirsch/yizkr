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

import hashlib
import json
import os
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
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
from yizkr.pipeline import llm, report
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
    call(31, CERT, "transcribe", FIRST, 20.0),
    stage("done", 31, CERT, "transcription", FIRST),
    *alert(31, FIRST, "transcription", "language-unexpected", CERT[:12],
           "yi", "new", confidence=0.51, detail="Hildegard Laudler"),
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
    call(47, CERT, "critique", FIRST, 5.0),
    stage("done", 47, CERT, "critique", FIRST),
    stage("started", 50, DIRECTORY, "transcription", FIRST),
    stage("done", 51, DIRECTORY, "transcription", FIRST),
    stage("started", 52, DIRECTORY, "extraction", FIRST),
    cache("cache-miss", 53, DIRECTORY, "extract", FIRST, call="open",
          reason="absent"),
    call(55, DIRECTORY, "extract", FIRST, 2.0),
    cache("cache-miss", 56, DIRECTORY, "extract", FIRST, call="fork",
          reason="absent"),
    call(60, DIRECTORY, "extract", FIRST, 4.0),
    cache("cache-reject", 61, DIRECTORY, "extract", FIRST,
          reason="chunk: no terminal sentinel after 'Löbig, Ursula, Wwe.'"),
    cache("cache-miss", 62, DIRECTORY, "extract", FIRST, call="fork",
          reason="rejected"),
    call(66, DIRECTORY, "extract", FIRST, 4.0),
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
    call(3614, POSTCARD, "extract", SECOND, 3.0),
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


def coverage(chunks, claims, rejected=0, unread=0, fallback=None) -> dict:
    return {
        "strategy": "entity",
        "chunks": chunks,
        "claims": claims,
        "rejected_claims": rejected,
        "unread_objects": unread,
        "fallback": fallback or {},
        "entities_without_claims": ["e3"],
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
                {"target": "e1 alias: Hilde", "issue": "Hilde of Laudler"},
                {"target": "claim: e1 spouse", "issue": "Sebastian Löbig"},
                {"target": "e2", "issue": "Flörburg is a town"},
                {"target": "", "issue": "the Standesamt Flörburg"},
            ],
            "extraction/coverage.json": coverage(3, 3, rejected=2, unread=1),
            "detail": {"code": "de", "source": "detected", "confidence": 1},
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
            "critique.json": [],
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
            "critique.json": [{"target": "e1", "issue": "Blauen Hut"}],
            "extraction/coverage.json": coverage(1, 1),
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
            "extraction/coverage.json": coverage(1, 2),
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

    def test_every_string_the_aggregates_hold_is_vocabulary(self, made):
        found = [s for s in strings(made["aggregate"]) if leaks(s)]
        assert found == []

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

    def test_the_statuses_agree_with_run_state(self, run, made):
        assert disagreement(run, made) is None

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
            "detected": 3,
            "override": 2,
            "fallback": 1,
        }

    def test_failed_documents_each_once(self, made):
        documents = made["aggregate"]["documents"]
        assert documents["failed"] == 2
        assert documents["failed_unread"] == 1
        assert documents["failed_by_label"] == {
            "RuntimeError": 1,
            "its source cannot be read": 1,
        }
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
        assert critic["by_class"] == {
            "subject": 2,
            "predicate": 1,
            "unclassified": 1,
            "value": 1,
        }
        assert (critic["critiques"], critic["documents"]) == (4, 2)

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
        assert "Depicts proposals held: not available" in text


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


def upto(seconds: float, name: str = "events.jsonl") -> list:
    """The stream as far as the driver had written it at `seconds`."""
    return [
        entry
        for entry in STREAM
        if datetime.fromisoformat(entry[1]["at"])
        <= T0 + timedelta(seconds=seconds)
    ]


class TestRunStates:
    def test_a_running_run_is_a_snapshot(self, tmp_path):
        plan = manifest()
        plan["invocations"][1] |= {"ended_at": None, "outcome": None}
        del plan["invocations"][1]["reason"]
        plan["invocations"] = plan["invocations"][:2]
        plan["ended_at"] = None
        path = record(tmp_path / "run", upto(3615), plan=plan)
        made = build(path, now=T0 + timedelta(seconds=3700))
        aggregate = made["aggregate"]
        assert aggregate["run"]["state"] == "running"
        # to now, the running invocation's time
        assert aggregate["run"]["seconds"] == 600 + 100
        assert aggregate["run"]["ends"] == {"paused": 1, "running": 1}
        postcard = document(made, POSTCARD)["stages"]
        assert postcard["extraction"]["status"] == RUNNING
        assert postcard["critique"]["status"] == PENDING
        assert stage_row(made, "extraction")[RUNNING] == 1
        assert disagreement(path, made) is None
        text = render_markdown(aggregate)
        assert "The run is still running" in text
        assert leaks(text) == []

    def test_a_stage_the_ended_invocation_left_started_is_interrupted(
        self, tmp_path
    ):
        plan = manifest()
        plan["invocations"] = plan["invocations"][:1]
        plan["ended_at"] = at(600)
        path = record(tmp_path / "run", upto(600), plan=plan)
        made = build(path, now=NOW)
        postcard = document(made, POSTCARD)["stages"]["extraction"]
        assert postcard["status"] == INTERRUPTED
        assert stage_row(made, "extraction")[INTERRUPTED] == 1
        assert made["aggregate"]["run"]["state"] == "paused"
        assert made["aggregate"]["run"]["reason"] == "UsageLimitReached"
        assert disagreement(path, made) is None

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
        plan = manifest()
        plan["invocations"] = plan["invocations"][:2]
        plan["ended_at"] = at(3700)
        plan["scratch"]["complete"] = False
        path = record(tmp_path / "run", upto(3700), plan=plan)
        made = build(path, now=NOW)
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
        assert disagreement(path, made) is None

    def test_a_killed_invocation_runs_to_its_last_event(self, tmp_path):
        plan = manifest()
        plan["invocations"][0] |= {"ended_at": None, "outcome": None}
        del plan["invocations"][0]["reason"]
        entries = [e for e in STREAM if e[1].get("event") != "run-end"
                   or e[1]["invocation"] != FIRST]  # fmt: skip
        path = record(tmp_path / "run", entries, plan=plan)
        made = build(path, now=NOW)
        run = made["run"]
        assert run["invocations"][0]["end"] == "killed"
        assert run["invocations"][0]["seconds"] == 170
        assert made["aggregate"]["run"]["ends"]["killed"] == 1
        assert disagreement(path, made) is None

    def test_an_empty_run(self, tmp_path):
        path = tmp_path / "run"
        RunDir(path).start(run_id=RUN_ID, mode="scratch run", documents=[])
        made = build(path, now=NOW)
        aggregate = made["aggregate"]
        assert aggregate["documents"]["planned"] == 0
        assert aggregate["stages"] == []
        text = render_markdown(aggregate)
        assert "No stage planned or seen." in text
        assert "0 documents planned" in text

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
        loaded = json.loads(written["report.json"].read_text())
        assert loaded["aggregate"]["run"]["run_id"] == RUN_ID
        # written again, each is replaced whole
        assert write_report(run, now=NOW) == written

    def test_no_document_directory_can_take_the_folder_s_name(self):
        assert REPORT_DIR.startswith(".")
        assert slugify(REPORT_DIR) != REPORT_DIR

    def test_the_html_drills_down_to_every_document(self, run):
        page = write_report(run, now=NOW)["report.html"].read_text()
        for sha1 in FILES:
            assert f'id="doc-{sha1[:12]}"' in page
            assert f'href="#doc-{sha1[:12]}"' in page or sha1 not in (
                DECK,
                INTERVIEW,
                DIRECTORY,
                CARD,
            )
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

    def test_a_folder_that_is_a_link_is_refused(self, run, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (run / REPORT_DIR).symlink_to(elsewhere)
        with pytest.raises(ReportError, match="not a folder of its own"):
            write_report(run, now=NOW)
        assert list(elsewhere.iterdir()) == []

    def test_no_run_json_no_report(self, tmp_path):
        with pytest.raises(ReportError, match="no run.json"):
            build(tmp_path)

    def test_an_event_log_it_cannot_read_is_refused(self, run):
        (run / "events.jsonl").chmod(0)
        try:
            if os.access(run / "events.jsonl", os.R_OK):
                pytest.skip("this process reads a file whatever its mode")
            with pytest.raises(ReportError, match="cannot be read"):
                build(run, now=NOW)
        finally:
            (run / "events.jsonl").chmod(0o600)

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
