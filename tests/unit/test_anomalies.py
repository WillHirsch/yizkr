"""Offline tests for the anomaly checks (#196, ADR-0025 §3).

Every check meets fictional outputs from the example universe
(docs/EXAMPLES.md): what makes it fire, and what keeps it quiet. The
checks read a run's events as the orchestrator, the LLM cache and the
engine's call reports write them, so the tests write those events through
the same RunDir. Then the thresholds, new and carried alerts, the
fail-fast pauses and their timeout into an abort, however a driver's
carry of a pause falls (#196's review), the orchestrator's hooks (a
carried alert on a reused output, a paused kind while another goes on,
outputs no alert alters), a recheck of stored outputs under new
thresholds, which recomputes nothing, and the unit tests' own default
for the checks' timers (tests/unit/conftest.py).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.fixtures import example_universe as u
from tests.fixtures.real_time import real_time_uses
from tests.fixtures.records import record_answer
from tests.unit.conftest import RecordedTimer
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import anomalies, rundir, transcription_check
from yizkr.pipeline.anomalies import (
    AnomalyChecks,
    AnomalyConfigError,
    check_failure,
    document_pages,
    load_config,
    recheck,
)
from yizkr.pipeline.chunking import (
    ChunkedClaimExtractor,
    ChunkIncomplete,
    ExtractionFailed,
    parse_chunk_response,
)
from yizkr.pipeline.critique import Finding
from yizkr.pipeline.language import resolve_language
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.rundir import RunDir, fold_control
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.transcription_check import (
    TranscriptionCheckFailed,
    check_answer,
    check_transcription,
)
from yizkr.pipeline.transcription_record import UNCLASSIFIED, parse_record

SOURCES = u.by_key()
# German, 302 letters and digits, detected as `de` with full confidence
MARRIAGE = SOURCES["marriage-cert-1905"].text
# English, detected as `en` with full confidence
NATURALISATION = SOURCES["naturalisation-1952"].text
# five short lines, 46 letters and digits, that the detector reads as
# another language with little confidence
GRAVESTONE = SOURCES["gravestone-photo-1956"].text
# a photograph's mount and reverse: 51 letters and digits, which the
# detector reads confidently as another language
STUDIO = SOURCES["studio-photo-1890s"].text

ROLES = {
    "transcription": "transcribe",
    "extraction": "extract",
    "critique": "critique",
}
RUN_META = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-28T09:00:00Z",
}
REPO = Path(__file__).resolve().parents[2]


# the checks' real pause timer, taken as the tests are collected: in every
# test, tests/unit/conftest.py records the timers the checks start instead
START_TIMER = anomalies._start_timer
# the bound, in seconds, on a wait for a thread or an event that the test
# itself ends: a guard against a hang, never a time the test waits out
HANG_GUARD = 60


def sha1_of(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()


@dataclass
class Doc:
    """What the checks read of a document: a stand-in for
    orchestrate.DocumentRun, its views under `dir`."""

    dir: Path
    name: str
    kind: str = "text"
    detail: dict = field(default_factory=dict)

    @property
    def sha1(self) -> str:
        return sha1_of(self.name)

    @property
    def source(self) -> Path:
        return self.dir / self.name

    @property
    def state(self):
        return SimpleNamespace(detail=self.detail)

    @property
    def transcription(self) -> Path:
        return self.dir / "transcription.json"

    @property
    def batch(self) -> Path:
        return self.dir / "batch.json"

    @property
    def critique(self) -> Path:
        return self.dir / "critique.json"

    @property
    def extraction(self) -> Path:
        return self.dir / "extraction"


def a_doc(tmp_path, name, kind="text", text=None, **detail) -> Doc:
    """A document, with the language resolution the run records for
    `text` when one is given."""
    directory = tmp_path / "docs" / name.replace(".", "-")
    directory.mkdir(parents=True, exist_ok=True)
    doc = Doc(directory, name, kind, detail)
    if text is not None and "language" not in detail:
        resolution = resolve_language(text, "auto")
        doc.detail["language"] = {
            "code": resolution.code,
            "source": resolution.source,
            "confidence": resolution.confidence,
        }
    return doc


class Clock:
    """The checks' clocks, moved on by hand: the wall clock, which the
    `clock` fixture makes the run directory's too, to stamp the control
    file's requests, and the monotonic clock (`steady`), which times
    the pauses out and which the fake Timers wait on. Both start at a
    fixed instant, never the real clock's. `advance` moves both; `step`
    moves the wall clock alone, as a wall clock that is set or corrected
    moves while the monotonic clock goes on."""

    def __init__(self) -> None:
        self.at = datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0

    def __call__(self) -> datetime:
        return self.at

    def steady(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds

    def step(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(
        rundir, "_now", lambda: clock.at.isoformat(timespec="seconds")
    )
    return clock


class Timers:
    """The timers the checks start, recorded instead of started. Each
    waits on the monotonic clock of `clock`, the checks' clock once checks
    are made with these timers (make_checks, checks_of): `fire` runs each
    one neither run nor cancelled yet whose wait has run out on it, as a
    real timer runs once when its time has come, and `waiting` lists them.
    While `refuse` is above 0, a timer cannot start, as when no thread
    can, and each refusal counts it down."""

    def __init__(self, clock=None) -> None:
        self.clock = clock
        self.started: list[RecordedTimer] = []
        self._due: dict[int, float] = {}
        self._run: set[int] = set()
        self.refuse = 0

    def __call__(self, seconds, callback):
        if self.refuse:
            self.refuse -= 1
            raise RuntimeError("can't start new thread")
        self.started.append(RecordedTimer(seconds, callback))
        if self.clock is not None:
            self._due[id(self.started[-1])] = self.clock.steady() + seconds
        return self.started[-1]

    def waiting(self) -> list[RecordedTimer]:
        return [
            timer
            for timer in self.started
            if not timer.cancelled and id(timer) not in self._run
        ]

    def fire(self) -> None:
        for timer in self.waiting():
            due = self._due.get(id(timer))
            if due is None or due <= self.clock.steady():
                self._run.add(id(timer))
                timer.callback()

    def on(self, clock):
        """These timers, waiting on `clock` unless they wait on another."""
        if self.clock is None:
            self.clock = clock
        return self


def make_checks(tmp_path, overrides=None, pages=None, clock=None, timers=None):
    run = RunDir(tmp_path / "run")
    checks = AnomalyChecks(
        run,
        load_config(overrides=overrides or {}),
        monotonic=(clock := clock or Clock()).steady,
        timer=(timers if timers is not None else Timers()).on(clock),
        page_count=pages or (lambda source: None),
    )
    return run, checks


def attempt(
    run: RunDir,
    doc: Doc,
    stage: str,
    *,
    failed: str | None = None,
    calls=(),
    hits: int = 0,
    misses: int = 0,
    rejects=(),
    call_kind: str = "fork",
    gave_up: int = 0,
) -> None:
    """The events of one stage attempt, as the stage runner, the LLM cache
    and the engine's call reports write them: its start, the cache's hits
    and misses, a call event per outcome in `calls`, `gave_up` calls whose
    answer the claude CLI gave up on at the output limit, the cache's
    rejections, and its end, done or failed with `failed` as the
    reason."""
    where = {"subject": doc.sha1, "kind": doc.kind, "stage": stage}
    about = {"subject": doc.sha1, "kind": doc.kind, "role": ROLES[stage]}
    run.emit("started", **where)
    for _ in range(hits):
        run.emit(
            "cache-hit", call=call_kind, key="k", reason="key match", **about
        )
    for _ in range(misses):
        run.emit(
            "cache-miss", call=call_kind, key="k", reason="absent", **about
        )
    for outcome in calls:
        run.emit("call", call=call_kind, stage=stage, outcome=outcome, **about)
    for _ in range(gave_up):
        run.emit(
            "call",
            call=call_kind,
            stage=stage,
            **GAVE_UP,
            **about,
        )
    for reason in rejects:
        run.emit("cache-reject", key="k", reason=reason, **about)
    if failed is None:
        run.emit("done", **where)
    else:
        run.emit("failed", reason=failed[:500], **where)


# what a call event says of a call whose answer the claude CLI gave up on
# at the output token limit (llm.OutputLimitReached): no answer, its
# error, and the class of API error its stream's last assistant event
# names, beside the stop reason the CLI reports (#7)
GAVE_UP = {
    "outcome": "error",
    "error": "OutputLimitReached",
    "api_error": "max_output_tokens",
    "stop_reason": "stop_sequence",
}

# the scan every transcription the checks are shown is of
SCAN = ArtifactRef.from_bytes(b"fictional scan: a Loebig letter")


def vision(text: str, language: str = "de") -> Transcription:
    return Transcription(
        artifact=SCAN,
        text=text,
        language=language,
        engine="llm-vision:fake-model",
    )


def a_claim(language: str | None = "de", year: int = 1862) -> dict:
    source: dict = {"artifact": {"kind": "artifact", "sha1": "a" * 40}}
    if language is not None:
        source["transcription_excerpt"] = {
            "text": f"geboren am 15. Februar {year}",
            "language": language,
        }
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Sebastian Löbig",
        },
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": source,
    }


def a_batch(*languages: str | None, entities: int = 1) -> ClaimBatch:
    """A batch with a claim per excerpt language (None: no excerpt)."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN_META,
            "claims": [a_claim(language) for language in languages],
            "entities": [
                {"id": f"e{n}", "label": "Sebastian Löbig", "kind": "human"}
                for n in range(1, entities + 1)
            ],
        }
    )


def coverage(doc: Doc, **fields) -> None:
    """The extraction's coverage side output, as the extractor writes it."""
    doc.extraction.mkdir(parents=True, exist_ok=True)
    report = {
        "chunks": 2,
        "claims": 0,
        "rejected_claims": 0,
        "unread_objects": 0,
        "fallback": {},
    } | fields
    (doc.extraction / "coverage.json").write_text(json.dumps(report))


def checks_raised(run: RunDir) -> list[str]:
    return [alert["check"] for alert in run.alerts()]


def only(run: RunDir, check: str) -> dict:
    (alert,) = [a for a in run.alerts() if a["check"] == check]
    return alert


def verdict_reason(text: str, pages: int | None = None) -> str:
    """The reason a stage's failed event gives for a transcription the
    check fails (the stage runner writes `<exception>: <message>`)."""
    check = check_transcription(text, pages=pages)
    assert not check.passed
    return f"TranscriptionCheckFailed: {TranscriptionCheckFailed(check)}"


# -- the thresholds -----------------------------------------------------------


class TestThresholds:
    def test_the_defaults_are_one_documented_config(self):
        config = load_config()
        assert config == anomalies.default_config()
        shipped = yaml.safe_load(anomalies.DEFAULTS.read_text())
        doc = (REPO / "docs" / "ANOMALIES.md").read_text()
        documented = set(re.findall(r"`([a-z]+(?:-[a-z]+)*)`", doc))
        assert set(shipped["checks"]) <= documented
        for settings in shipped["checks"].values():
            assert set(settings) <= documented
        assert {"version", "pause-timeout-s", "chars-per-page"} <= documented

    def test_a_file_named_in_the_variable_overrides_the_defaults(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "thresholds.yaml"
        path.write_text(
            "pause-timeout-s: 60\n"
            "checks:\n"
            "  language-unexpected:\n"
            "    expected: [de, en]\n"
        )
        monkeypatch.setenv(anomalies.CONFIG_VARIABLE, str(path))
        config = load_config()
        assert config.pause_timeout_s == 60
        assert config.checks.language_unexpected.expected == ["de", "en"]
        # everything else keeps its default
        assert config.checks.kind_failures == (
            anomalies.default_config().checks.kind_failures
        )
        assert config.digest() != anomalies.default_config().digest()

    @pytest.mark.parametrize(
        "text",
        [
            "checks:\n  zero-claims:\n    min_chars: 10\n",  # misspelt
            "checks:\n  zero-claim:\n    min-chars: 10\n",  # no such check
            "checks:\n  call-failures:\n    min-count: 20\n",  # > window
            "checks:\n  kind-failures:\n    max-share: 1.5\n",
            "checks:\n  kind-failures:\n    min-documents: 0\n",
            "pause-timeout-s: -1\n",
            "pause-timeout-s: .inf\n",  # would stop the timer's thread
            "pause-timeout-s: 1.0e+10\n",  # beyond threading.TIMEOUT_MAX
            "pause-timeout-s: .nan\n",
            "- a list\n",
            "checks: [\n",  # not YAML
        ],
    )
    def test_a_file_the_checks_cannot_use_is_refused(self, tmp_path, text):
        path = tmp_path / "thresholds.yaml"
        path.write_text(text)
        with pytest.raises(AnomalyConfigError):
            load_config(path)

    def test_a_run_with_unusable_thresholds_keeps_the_defaults_and_says_so(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "thresholds.yaml"
        path.write_text("checks:\n  zero-claims:\n    min_chars: 10\n")
        monkeypatch.setenv(anomalies.CONFIG_VARIABLE, str(path))
        run = RunDir(tmp_path / "run")
        checks = anomalies.checks_for(run)
        assert checks.config == anomalies.default_config()
        alert = only(run, "anomaly-config")
        assert (alert["stage"], alert["status"]) == ("run", "new")
        # the thresholds in force are named, as on every alert
        assert alert["config"] == anomalies.default_config().digest()
        assert "min_chars" in alert["problem"]
        # made once per invocation
        assert anomalies.checks_for(run) is checks

    def test_an_invocation_has_one_set_of_checks(self, tmp_path):
        run = RunDir(tmp_path / "run")
        checks = anomalies.checks_for(run)
        again = RunDir(tmp_path / "run", invocation=run.invocation)
        assert anomalies.checks_for(again) is checks
        later = RunDir(tmp_path / "run")
        assert anomalies.checks_for(later) is not checks
        attached = anomalies.attach(
            later, load_config(overrides={"pause-timeout-s": 5})
        )
        assert anomalies.checks_for(later) is attached
        assert attached.config.pause_timeout_s == 5

    def test_every_alert_names_the_thresholds_it_was_raised_under(
        self, tmp_path
    ):
        run, checks = make_checks(
            tmp_path, {"checks": {"zero-claims": {"min-chars": 5}}}
        )
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch())
        alert = only(run, "zero-claims")
        assert alert["config"] == checks.config.digest()
        assert alert["config"] != anomalies.default_config().digest()


# -- transcription ------------------------------------------------------------


class TestTranscription:
    def test_a_transcription_the_check_fails_is_a_new_alert(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "letter.png", kind="media")
        reason = verdict_reason("Traceback (most recent call last):")
        attempt(run, doc, "transcription", calls=["ok"], failed=reason)
        checks.failed(doc, "transcription")
        alert = only(run, "transcription-check")
        assert alert["stage"] == "transcription"
        assert alert["subject"] == doc.sha1[: anomalies.SUBJECT_PREFIX]
        assert (alert["value"], alert["status"]) == (True, "new")
        assert (alert["failure"], alert["signature"]) == (
            "tool-failure",
            "traceback",
        )
        assert alert["kind"] == "media"

    def test_an_answer_that_is_not_the_record_is_a_verdict(self, tmp_path):
        """A refusal in any language comes as an answer that is not the
        record, and fails as that, naming no signature."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "letter.png", kind="media")
        check, record = check_answer(
            "Es tut mir leid, ich kann dieses Dokument nicht abschreiben."
        )
        assert record is None
        reason = f"TranscriptionCheckFailed: {TranscriptionCheckFailed(check)}"
        attempt(run, doc, "transcription", calls=["ok"], failed=reason)
        checks.failed(doc, "transcription")
        alert = only(run, "transcription-check")
        assert alert["failure"] == "not-a-record"
        assert "signature" not in alert

    def test_a_flagged_transcription_is_an_alert_and_goes_on(self, tmp_path):
        """The check's flag, as the stage output carries it, is an alert
        of its own; the transcription is no failure."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "letter.png", kind="media", text=MARRIAGE)
        flagged = vision(MARRIAGE)
        flagged.flag = "refusal-request"
        checks.stored(doc, "transcription", flagged)
        alert = only(run, "transcription-flag")
        assert (alert["value"], alert["signature"]) == (
            True,
            "refusal-request",
        )
        assert "transcription-check" not in checks_raised(run)

    def test_the_flags_can_be_switched_off(self, tmp_path):
        run, checks = make_checks(
            tmp_path, {"checks": {"transcription-flag": {"enabled": False}}}
        )
        doc = a_doc(tmp_path, "letter.png", kind="media", text=MARRIAGE)
        flagged = vision(MARRIAGE)
        flagged.flag = "refusal-request"
        checks.stored(doc, "transcription", flagged)
        assert "transcription-flag" not in checks_raised(run)

    def test_a_reused_transcription_set_aside_is_a_carried_alert(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "letter.png", kind="media")
        check = check_transcription("pdftoppm: command not found")
        run.emit(
            "set-aside",
            subject=doc.sha1,
            kind=doc.kind,
            stage="transcription",
            reason=str(TranscriptionCheckFailed(check))[:500],
            kept="transcription.superseded-1.json",
        )
        checks.poll()
        alert = only(run, "transcription-check")
        assert alert["status"] == "carried"
        assert (alert["failure"], alert["signature"]) == (
            "tool-failure",
            "pdf-renderer",
        )

    @pytest.mark.parametrize(
        "text, pages, failure",
        [
            ("Let me take a look at the image.", 1, "tool-failure"),
            ("pdftoppm: command not found", 1, "tool-failure"),
            (" \n ", 1, "empty"),
            ("E L", 3, "too-short"),
        ],
    )
    def test_the_verdicts_are_read_from_the_check_s_own_reasons(
        self, text, pages, failure
    ):
        check = check_transcription(text, pages=pages)
        for reason in (
            f"TranscriptionCheckFailed: {TranscriptionCheckFailed(check)}",
            str(TranscriptionCheckFailed(check)),
        ):
            finding = check_failure(reason)
            assert finding.check == "transcription-check"
            assert finding.fields["failure"] == failure

    def test_a_transcription_that_fails_for_another_reason_is_no_verdict(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "letter.png", kind="media")
        attempt(
            run,
            doc,
            "transcription",
            calls=["error"],
            failed="RuntimeError: transcription check v2 failed (refusal)",
        )
        checks.failed(doc, "transcription")
        assert "transcription-check" not in checks_raised(run)

    def test_the_verdicts_can_be_switched_off(self, tmp_path):
        run, checks = make_checks(
            tmp_path, {"checks": {"transcription-check": {"enabled": False}}}
        )
        doc = a_doc(tmp_path, "letter.png", kind="media")
        reason = verdict_reason("Traceback (most recent call last):")
        attempt(run, doc, "transcription", calls=["ok"], failed=reason)
        checks.failed(doc, "transcription")
        assert run.alerts() == []

    def test_a_vision_transcription_sparse_for_its_pages(self, tmp_path):
        run, checks = make_checks(tmp_path, pages=lambda source: 3)
        doc = a_doc(tmp_path, "grave.pdf", kind="media", text=GRAVESTONE)
        checks.stored(doc, "transcription", vision(GRAVESTONE))
        alert = only(run, "transcription-sparse")
        assert (alert["value"], alert["limit"], alert["pages"]) == (
            15.3,
            50,
            3,
        )

    @pytest.mark.parametrize(
        "text, pages, engine",
        [
            (MARRIAGE, 3, "llm-vision:fake-model"),  # 100 a page
            (GRAVESTONE, 1, "llm-vision:fake-model"),  # a single page
            (GRAVESTONE, None, "llm-vision:fake-model"),  # count unknown
            (GRAVESTONE, 3, "correction"),  # a curator's text
        ],
    )
    def test_a_transcription_full_enough_for_its_pages_is_quiet(
        self, tmp_path, text, pages, engine
    ):
        run, checks = make_checks(tmp_path, pages=lambda source: pages)
        doc = a_doc(tmp_path, "grave.pdf", kind="media", text=text)
        checks.stored(
            doc,
            "transcription",
            Transcription(
                artifact=SCAN, text=text, language="de", engine=engine
            ),
        )
        assert "transcription-sparse" not in checks_raised(run)


# -- language -----------------------------------------------------------------


class TestLanguage:
    def test_a_language_outside_the_expected_ones(self, tmp_path):
        run, checks = make_checks(
            tmp_path,
            {"checks": {"language-unexpected": {"expected": ["en"]}}},
        )
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        alert = only(run, "language-unexpected")
        assert (alert["value"], alert["limit"]) == ("de", ["en"])

    @pytest.mark.parametrize(
        "expected, resolution",
        [
            ([], None),  # the default: no language is unexpected
            (["de-AT", "en"], None),  # compared by the primary subtag
            (["en"], {"code": "de", "source": "override"}),  # told
            (["en"], {"code": "de", "source": "fallback"}),  # defaulted
        ],
    )
    def test_an_expected_or_chosen_language_is_quiet(
        self, tmp_path, expected, resolution
    ):
        run, checks = make_checks(
            tmp_path,
            {"checks": {"language-unexpected": {"expected": expected}}},
        )
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        if resolution is not None:
            doc.detail["language"] = resolution
        checks.stored(doc, "transcription", vision(MARRIAGE))
        assert "language-unexpected" not in checks_raised(run)

    def test_a_language_the_record_gives_can_be_unexpected(self, tmp_path):
        """The record's language is the engine's reading, not a choice
        the run made, so it is compared with the expected ones."""
        run, checks = make_checks(
            tmp_path,
            {"checks": {"language-unexpected": {"expected": ["en"]}}},
        )
        doc = a_doc(
            tmp_path,
            "letter.png",
            kind="media",
            language={"code": "ru", "source": "record", "confidence": None},
        )
        checks.stored(doc, "transcription", vision(MARRIAGE, "ru"))
        alert = only(run, "language-unexpected")
        assert (alert["value"], alert["limit"]) == ("ru", ["en"])

    def test_a_language_detected_with_low_confidence(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "grave.png", kind="media", text=GRAVESTONE)
        assert doc.detail["language"]["source"] == "detected"
        checks.stored(doc, "transcription", vision(GRAVESTONE))
        alert = only(run, "language-low-confidence")
        assert alert["value"] < 0.9
        assert (alert["limit"], alert["resolved"]) == (0.9, "detected")
        # a low guess on short text is no confident detection
        assert "language-short-text" not in checks_raised(run)

    def test_a_fallback_on_a_guess_the_detector_could_not_stand_by(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(
            tmp_path,
            "grave.png",
            kind="media",
            language={"code": "en", "source": "fallback", "confidence": 0.31},
        )
        checks.stored(doc, "transcription", vision(GRAVESTONE, "en"))
        alert = only(run, "language-low-confidence")
        assert (alert["value"], alert["resolved"]) == (0.31, "fallback")

    def test_a_fallback_for_want_of_any_guess_is_quiet(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(
            tmp_path,
            "stamp.png",
            kind="media",
            language={"code": "en", "source": "fallback", "confidence": None},
        )
        checks.stored(doc, "transcription", vision("1938", "en"))
        assert run.alerts() == []

    def test_a_confident_detection_on_very_short_text(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "photo.png", kind="media", text=STUDIO)
        checks.stored(doc, "transcription", vision(STUDIO))
        alert = only(run, "language-short-text")
        assert alert["value"] < 100 and alert["confidence"] >= 0.9

    def test_a_confident_detection_on_a_full_text_is_quiet(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "cert.png", kind="media", text=NATURALISATION)
        checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
        assert run.alerts() == []

    def test_excerpts_mostly_in_another_language(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch("en", "en", "en", "de"))
        alert = only(run, "language-mismatch")
        assert (alert["value"], alert["count"], alert["of"]) == (0.75, 3, 4)
        assert alert["language"] == "de" and alert["stage"] == "extraction"

    @pytest.mark.parametrize(
        "languages",
        [
            ("en", "en", "de", "de"),  # half is no majority
            ("en", "en", "en"),  # too few excerpts to judge
            ("de-at", "de", "de", "de"),  # a regional tag is the language
            (None, None, None, None, "en"),  # claims without excerpts
        ],
    )
    def test_excerpts_mostly_in_the_transcription_s_language_are_quiet(
        self, tmp_path, languages
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch(*languages))
        assert "language-mismatch" not in checks_raised(run)


# -- extraction ---------------------------------------------------------------


class TestExtraction:
    def test_no_entities_and_no_claims_from_a_full_transcription(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch(entities=0))
        assert sorted(checks_raised(run)) == ["zero-claims", "zero-entities"]
        alert = only(run, "zero-claims")
        assert (alert["value"], alert["limit"]) == (302, 200)

    def test_entities_without_claims_are_zero_claims_alone(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch(entities=2))
        assert checks_raised(run) == ["zero-claims"]

    def test_nothing_from_a_short_transcription_is_quiet(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "grave.png", kind="media")
        checks.stored(doc, "transcription", vision(GRAVESTONE))
        checks.stored(doc, "extraction", a_batch(entities=0))
        assert run.alerts() == []

    def test_the_transcription_is_read_from_its_view_when_not_seen(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        doc.transcription.write_text(vision(MARRIAGE).model_dump_json())
        checks.stored(doc, "extraction", a_batch(entities=0))
        assert sorted(checks_raised(run)) == ["zero-claims", "zero-entities"]

    def test_a_fallback_manifest_and_segmentation(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        coverage(
            doc,
            claims=3,
            fallback={
                "manifest": "no JSON object in the answer",
                "segmentation": "units e1 and e2 overlap",
            },
        )
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch("de", "de", "de"))
        assert sorted(checks_raised(run)) == [
            "fallback-manifest",
            "fallback-segmentation",
        ]
        assert only(run, "fallback-manifest")["value"] is True

    def test_claims_rejected_or_unread_above_the_share(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        coverage(doc, claims=6, rejected_claims=3, unread_objects=1)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch(*["de"] * 6))
        alert = only(run, "schema-rejects")
        assert (alert["value"], alert["count"], alert["of"]) == (0.4, 4, 10)

    @pytest.mark.parametrize(
        "claims, rejected", [(9, 1), (1, 2)]
    )  # a small share; too few claims to judge
    def test_few_rejects_are_quiet(self, tmp_path, claims, rejected):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        coverage(doc, claims=claims, rejected_claims=rejected)
        checks.stored(doc, "transcription", vision(MARRIAGE))
        checks.stored(doc, "extraction", a_batch(*["de"] * claims))
        assert "schema-rejects" not in checks_raised(run)

    @pytest.mark.parametrize("shape", ["rejected", "unread"])
    def test_an_extraction_that_could_use_none_of_its_claims(
        self, tmp_path, shape
    ):
        """The reason a failed extraction gives, as the extractor words it,
        for every claim rejected by validation or unread by the parser."""
        from tests.unit.test_chunking import (
            DOC_REF,
            LINE_LENGTH,
            ScriptedLlm,
            claim,
            entity_manifest_json,
            ndjson,
            transcription,
            wrapped,
        )

        people = [claim("e1", "e1", 1917), claim("e2", "e2", 1917)]
        if shape == "rejected":
            answers = [
                ndjson([dict(person, property="shoe_size")])
                for person in people
            ]
        else:
            answers = [wrapped([person]) for person in people]
        extractor = ChunkedClaimExtractor(
            ScriptedLlm([entity_manifest_json(), *answers]),
            state_dir=tmp_path / "chunks",
            budget=LINE_LENGTH,
        )
        with pytest.raises(ExtractionFailed) as failed:
            extractor.extract(transcription(), DOC_REF)
        run, checks = make_checks(
            tmp_path, {"checks": {"schema-rejects": {"min-count": 2}}}
        )
        doc = a_doc(tmp_path, "brief.txt")
        reason = f"ExtractionFailed: {failed.value}"
        attempt(run, doc, "extraction", calls=["ok"] * 3, failed=reason)
        checks.failed(doc, "extraction")
        alert = only(run, "schema-rejects")
        assert (alert["value"], alert["count"], alert["of"]) == (1.0, 2, 2)
        assert alert["status"] == "new"

    def test_chunk_calls_failing_above_the_share(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        # the manifest's opening turn is no chunk call
        run.emit("started", subject=doc.sha1, kind="text", stage="extraction")
        run.emit(
            "call",
            subject=doc.sha1,
            kind="text",
            role="extract",
            stage="extraction",
            call="open",
            outcome="error",
        )
        for outcome in ("ok", "error", "error", "ok"):
            run.emit(
                "call",
                subject=doc.sha1,
                kind="text",
                role="extract",
                stage="extraction",
                call="fork",
                outcome=outcome,
            )
        run.emit("done", subject=doc.sha1, kind="text", stage="extraction")
        checks.poll()
        alert = only(run, "chunk-failures")
        assert (alert["value"], alert["count"], alert["of"]) == (0.5, 2, 4)

    def test_an_extraction_whose_chunks_all_failed(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        attempt(
            run,
            doc,
            "extraction",
            calls=["error"] * 6,
            failed="RuntimeError: 2 chunks failed after retries (c001, c002)",
        )
        checks.failed(doc, "extraction")
        assert only(run, "chunk-failures")["value"] == 1.0

    @pytest.mark.parametrize(
        "calls", [["ok", "ok", "ok", "error"], ["error", "error", "ok"]]
    )  # a quarter is no more than the share; three are too few to judge
    def test_few_chunk_failures_are_quiet(self, tmp_path, calls):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        attempt(run, doc, "extraction", calls=calls)
        checks.poll()
        assert "chunk-failures" not in checks_raised(run)

    @pytest.mark.parametrize(
        "stopped", [False, True], ids=["no-sentinel", "stop-reason"]
    )
    def test_chunk_answers_cut_off_above_the_share(self, tmp_path, stopped):
        """Cut off before its sentinel, or, whole as it looks, stopped at
        the output limit (#7): either rejection counts."""
        answer = json.dumps(a_claim())
        if stopped:
            answer += '\n{"done": true}'
        with pytest.raises(ChunkIncomplete) as cut:
            parse_chunk_response(answer, cut_off=stopped)
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        # two kept answers cut off, rejected and asked again of the engine
        attempt(
            run,
            doc,
            "extraction",
            hits=4,
            misses=2,
            calls=["ok", "ok"],
            rejects=[f"chunk: {cut.value}"] * 2,
        )
        checks.poll()
        alert = only(run, "truncations")
        assert (alert["value"], alert["count"], alert["of"]) == (0.333, 2, 6)
        assert alert["status"] == "new"

    def test_chunk_calls_the_engine_gave_up_on_are_truncations(self, tmp_path):
        """A chunk call whose answer the claude CLI gave up on at the
        output limit is an answer cut off, with nothing to reject: a
        truncation, never a chunk failure (#7)."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        attempt(run, doc, "extraction", misses=6, calls=["ok"] * 2, gave_up=4)
        checks.poll()
        alert = only(run, "truncations")
        assert (alert["value"], alert["count"], alert["of"]) == (0.667, 4, 6)
        assert "chunk-failures" not in checks_raised(run)

    @pytest.mark.parametrize(
        ("outcome", "error", "raised"),
        [
            ("error", "RuntimeError", "chunk-failures"),
            ("ok", None, None),
        ],
        ids=["failed-after-the-notice", "answered"],
    )
    def test_the_class_of_api_error_alone_is_no_give_up(
        self, tmp_path, outcome, error, raised
    ):
        """Only a call the engine raised OutputLimitReached for is a
        give-up. One that crashed or timed out after the CLI printed its
        notice names max_output_tokens too, and failed; an answered one
        that names it is an answer (#7)."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        about = dict(subject=doc.sha1, kind="text", role="extract")
        run.emit("started", subject=doc.sha1, kind="text", stage="extraction")
        for _ in range(4):
            run.emit(
                "call",
                call="fork",
                stage="extraction",
                outcome=outcome,
                api_error="max_output_tokens",
                **({"error": error} if error else {}),
                **about,
            )
        run.emit("done", subject=doc.sha1, kind="text", stage="extraction")
        checks.poll()
        assert "truncations" not in checks_raised(run)
        assert checks_raised(run) == ([raised] if raised else [])

    def test_rejections_of_other_answers_are_no_truncations(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        attempt(
            run,
            doc,
            "extraction",
            hits=4,
            rejects=["transcription check v2 failed (refusal)"] * 3,
        )
        checks.poll()
        assert "truncations" not in checks_raised(run)

    def test_a_batch_whose_transcription_is_unknown_is_not_weighed(
        self, tmp_path
    ):
        """The zero checks weigh a batch against its transcription's
        length: with its view unreadable, nothing says how long it was,
        and they say nothing."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        doc.transcription.write_text('{"text": "Standesamt Flörb')
        checks.stored(doc, "extraction", a_batch(entities=0))
        assert run.alerts() == []


# -- implausible dates --------------------------------------------------------


def a_time(year, precision=None, month=None, day=None) -> dict:
    value = {"type": "time", "year": year}
    for name, given in (
        ("precision", precision),
        ("month", month),
        ("day", day),
    ):
        if given is not None:
            value[name] = given
    return value


def a_dated_claim(who: str, prop: str, value: dict, qualifiers=()) -> dict:
    """A claim about the entity `who`, with `qualifiers` given as
    (property, time value) pairs."""
    claim = {
        "subject": {"kind": "local", "local_id": who},
        "property": prop,
        "value": value,
        "source": {"artifact": {"kind": "artifact", "sha1": "a" * 40}},
    }
    if qualifiers:
        claim["qualifiers"] = [
            {"property": name, "value": time} for name, time in qualifiers
        ]
    return claim


def a_dated_batch(*claims: dict) -> ClaimBatch:
    names = {
        "ursula": "Ursula Löbig",
        "gustav": "Gustav Löbig",
        "nicholas": "Nicholas Löbig",
        "cäcilie": "Cäcilie Löbig",
        "rahel": "Rahel Löbig",
        "eugen": "Eugen Löbig",
        "flörburg": "Flörburg",
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN_META,
            "claims": list(claims),
            "entities": [
                {
                    "id": key,
                    "label": label,
                    "kind": "place" if key == "flörburg" else "human",
                }
                for key, label in names.items()
            ],
        }
    )


FLÖRBURG = {"type": "item", "value": {"kind": "local", "local_id": "flörburg"}}
CIRCA = {"type": "item", "value": {"kind": "vocab", "key": "circa"}}
URSULA_BORN = a_dated_claim("ursula", "date_of_birth", a_time(1911))
# the family history's "I was born in '50", read in the wrong century
NICHOLAS_BORN = a_dated_claim("nicholas", "date_of_birth", a_time(1850))


def lived(who: str, *qualifiers) -> dict:
    """A residence in Flörburg, dated by `qualifiers`."""
    return a_dated_claim(who, "residence", FLÖRBURG, qualifiers)


def dates_found(batch: ClaimBatch, overrides=None) -> list:
    return anomalies.date_findings(
        load_config(overrides=overrides), batch.claims
    )


def counted(batch: ClaimBatch, overrides=None) -> tuple[int, int] | None:
    """(before the birth, after it), or None when nothing is counted."""
    found = dates_found(batch, overrides)
    if not found:
        return None
    (finding,) = found
    return finding.fields["before_birth"], finding.fields["after_birth"]


class TestImplausibleDate:
    def test_a_claim_dated_before_the_birth_is_counted(self):
        """A 1930s residence read as the 1st century lies wholly before
        Ursula's birth in 1911."""
        batch = a_dated_batch(
            URSULA_BORN,
            lived("ursula", ("point_in_time", a_time(1, "century"))),
        )
        (finding,) = dates_found(batch)
        assert (finding.check, finding.value, finding.limit) == (
            "implausible-date",
            1,
            None,
        )
        assert finding.fields == {
            "before_birth": 1,
            "after_birth": 0,
            "max_years_after_birth": 120,
        }

    def test_a_claim_dated_too_long_after_the_birth_is_counted(self):
        """Nicholas's birth read as 1850 puts his 2008 family history 158
        years after it."""
        batch = a_dated_batch(
            NICHOLAS_BORN, lived("nicholas", ("start_time", a_time(2008)))
        )
        assert counted(batch) == (0, 1)

    def test_a_claim_dated_on_both_sides_is_counted_once(self):
        batch = a_dated_batch(
            URSULA_BORN,
            lived(
                "ursula",
                ("start_time", a_time(1801)),
                ("end_time", a_time(2040)),
            ),
        )
        (finding,) = dates_found(batch)
        assert finding.value == 1
        assert counted(batch) == (1, 0)

    def test_the_threshold_is_the_last_plausible_year(self):
        def after(years: int) -> ClaimBatch:
            return a_dated_batch(
                URSULA_BORN,
                a_dated_claim("ursula", "date_of_death", a_time(1911 + years)),
            )

        assert dates_found(after(120)) == []
        assert dates_found(after(121))[0].value == 1
        later = {
            "checks": {"implausible-date": {"max-years-after-birth": 130}}
        }
        assert dates_found(after(121), later) == []

    def test_a_negative_threshold_is_refused(self):
        with pytest.raises(AnomalyConfigError):
            load_config(
                overrides={
                    "checks": {
                        "implausible-date": {"max-years-after-birth": -1}
                    }
                }
            )

    def test_a_date_s_precision_is_never_narrowed(self):
        """The 1910s and the 20th century both hold 1911, and a date in
        the birth year is never before it, whatever its day."""
        batch = a_dated_batch(
            a_dated_claim(
                "gustav", "date_of_birth", a_time(1919, month=7, day=1)
            ),
            lived("gustav", ("start_time", a_time(1919, month=1, day=7))),
            URSULA_BORN,
            lived("ursula", ("point_in_time", a_time(1910, "decade"))),
            lived("ursula", ("point_in_time", a_time(1901, "century"))),
        )
        assert dates_found(batch) == []

    @pytest.mark.parametrize(
        ("birth", "date", "expected"),
        [
            # the 1900s end in 1909, before a birth in 1910
            (a_time(1910), a_time(1900, "decade"), (1, 0)),
            # the 1910s end in 1919, a birth's own year
            (a_time(1919), a_time(1910, "decade"), None),
            # the 19th century, 1801 to 1900, holds 1900
            (a_time(1900), a_time(1801, "century"), None),
            # a birth in the 1910s may be as late as 1919
            (a_time(1910, "decade"), a_time(1912), None),
            # a birth in the 20th century may be as late as 2000
            (a_time(1901, "century"), a_time(2030), None),
            # the 2030s begin 119 years after 1911
            (a_time(1911), a_time(2030, "decade"), None),
            # a decade stored by another of its years is counted as
            # Wikibase counts it: 1905 names the 1900s, before 1911
            (a_time(1911), a_time(1905, "decade"), (1, 0)),
            # a century stored by another of its years: 1850 names the
            # 19th century, 1801 to 1900, which ends before 1911
            (a_time(1911), a_time(1850, "century"), (1, 0)),
        ],
    )
    def test_spans_are_counted_as_wikibase_counts_them(
        self, birth, date, expected
    ):
        """Raw stored claims, as a batch.json may hold them, a century
        stored by a year other than its first included."""
        claims = [
            a_dated_claim("ursula", "date_of_birth", birth),
            lived("ursula", ("start_time", date)),
        ]
        found = anomalies.date_findings(load_config(), claims)
        assert [
            (finding.fields["before_birth"], finding.fields["after_birth"])
            for finding in found
        ] == ([expected] if expected else [])

    def test_a_range_s_bounds_widen_a_date(self):
        """Ursula's residence "after 1890" may run on past her birth in
        1911, and Rahel, born between 1870 and 1871, may be 120 in
        1991."""
        after_1890 = lived(
            "ursula",
            ("point_in_time", a_time(1890)),
            ("earliest_date", a_time(1890)),
        )
        assert counted(a_dated_batch(URSULA_BORN, after_1890)) is None
        rahel_born = a_dated_claim(
            "rahel",
            "date_of_birth",
            a_time(1870),
            [("earliest_date", a_time(1870)), ("latest_date", a_time(1871))],
        )

        def rahel_in(year: int) -> ClaimBatch:
            return a_dated_batch(
                rahel_born, lived("rahel", ("point_in_time", a_time(year)))
            )

        assert counted(rahel_in(1991)) is None
        assert counted(rahel_in(1992)) == (0, 1)

    def test_a_lone_bound_opens_its_side(self):
        """Eugen's birth read as the 18th century, and his death "before
        1926": the death may lie at any time up to 1926, so it is not
        judged after the birth. Cäcilie born "before 1834" may be born
        long before 1801, but not long before 1960."""
        eugen = a_dated_batch(
            a_dated_claim("eugen", "date_of_birth", a_time(1701, "century")),
            a_dated_claim(
                "eugen",
                "date_of_death",
                a_time(1926),
                [("latest_date", a_time(1926))],
            ),
        )
        assert counted(eugen) is None
        before_1834 = a_dated_claim(
            "cäcilie",
            "date_of_birth",
            a_time(1834),
            [("latest_date", a_time(1834))],
        )
        assert (
            counted(
                a_dated_batch(
                    before_1834, lived("cäcilie", ("start_time", a_time(1801)))
                )
            )
            is None
        )
        assert counted(
            a_dated_batch(
                before_1834,
                a_dated_claim("cäcilie", "date_of_death", a_time(1960)),
            )
        ) == (0, 1)

    def test_a_circa_date_is_judged_on_neither_side(self):
        """Cäcilie born c. 1833 may be born years either side of it; a
        residence c. 1801 may lie years either side of that."""
        circa_1833 = a_dated_claim(
            "cäcilie",
            "date_of_birth",
            a_time(1833),
            [("sourcing_circumstances", CIRCA)],
        )
        batch = a_dated_batch(
            circa_1833,
            lived("cäcilie", ("start_time", a_time(1801))),
            a_dated_claim("cäcilie", "date_of_death", a_time(1960)),
        )
        assert counted(batch) is None
        batch = a_dated_batch(
            a_dated_claim("cäcilie", "date_of_birth", a_time(1833)),
            lived(
                "cäcilie",
                ("start_time", a_time(1801)),
                ("sourcing_circumstances", CIRCA),
            ),
        )
        assert counted(batch) is None

    def test_each_end_takes_its_own_bounds(self):
        """A start runs to its latest start date, not to the latest date,
        which bounds the end; an end runs from its earliest end date."""
        start = ("start_time", a_time(1890))
        assert counted(
            a_dated_batch(
                URSULA_BORN,
                lived("ursula", start, ("latest_date", a_time(1915))),
            )
        ) == (1, 0)
        assert (
            counted(
                a_dated_batch(
                    URSULA_BORN,
                    lived(
                        "ursula", start, ("latest_start_date", a_time(1915))
                    ),
                )
            )
            is None
        )
        end = ("end_time", a_time(2040))
        assert counted(
            a_dated_batch(
                URSULA_BORN,
                lived("ursula", end, ("earliest_date", a_time(1920))),
            )
        ) == (0, 1)
        assert (
            counted(
                a_dated_batch(
                    URSULA_BORN,
                    lived("ursula", end, ("earliest_end_date", a_time(1920))),
                )
            )
            is None
        )

    def test_every_birth_counts_and_none_is_preferred(self):
        """Two contradictory births coexist (ADR-0002): a date between
        them is plausible for one, and is not counted."""
        batch = a_dated_batch(
            URSULA_BORN,
            a_dated_claim("ursula", "date_of_birth", a_time(1921)),
            lived("ursula", ("start_time", a_time(1915))),
            a_dated_claim("ursula", "date_of_death", a_time(2035)),
        )
        assert dates_found(batch) == []

    def test_undated_people_are_not_judged(self):
        # the batch gives Gustav no birth
        batch = a_dated_batch(
            URSULA_BORN,
            a_dated_claim("gustav", "date_of_death", a_time(1066)),
        )
        assert dates_found(batch) == []

    def test_a_birth_is_not_judged_against_another(self):
        """Births a millennium apart coexist; neither is counted against
        the other."""
        batch = a_dated_batch(
            URSULA_BORN,
            a_dated_claim("ursula", "date_of_birth", a_time(1, "century")),
        )
        assert dates_found(batch) == []

    def test_a_person_named_by_qid_is_judged(self):
        ursula = {"kind": "qid", "qid": "Q42"}
        claims = [
            {**URSULA_BORN, "subject": ursula},
            {
                **a_dated_claim("ursula", "date_of_death", a_time(1801)),
                "subject": ursula,
            },
        ]
        assert counted(a_dated_batch(*claims)) == (1, 0)

    @pytest.mark.parametrize(
        "birth",
        [
            {"type": "time", "year": True},
            {"type": "time", "year": 0},
            {"type": "time", "year": "1911"},
            {"type": "time", "year": 1911.0},
            {"type": "time", "year": None},
            {"type": "time", "year": 1911, "precision": "millennium"},
            {"type": "quantity", "amount": 1911, "year": 1911},
            {"type": "string", "value": "1911"},
        ],
    )
    def test_a_birth_that_is_no_dated_year_is_not_judged(self, birth):
        """Raw stored claims, as a batch.json may hold them: a birth that
        gives no year of a known precision dates nothing."""
        claims = [
            {**URSULA_BORN, "value": birth},
            a_dated_claim("ursula", "date_of_death", a_time(1801)),
        ]
        config = load_config()
        assert anomalies.date_findings(config, claims) == []

    def test_disabled_it_finds_nothing(self):
        batch = a_dated_batch(
            URSULA_BORN,
            a_dated_claim("ursula", "date_of_death", a_time(1801)),
        )
        assert dates_found(batch)
        off = {"checks": {"implausible-date": {"enabled": False}}}
        assert dates_found(batch, off) == []

    def test_a_stored_batch_raises_one_alert_and_is_left_as_it_is(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "history.txt", text=NATURALISATION)
        checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
        batch = a_dated_batch(
            URSULA_BORN,
            NICHOLAS_BORN,
            a_dated_claim("ursula", "date_of_death", a_time(1801)),
            # within 120 years of the misread birth, so not counted
            lived("nicholas", ("start_time", a_time(1950))),
            lived("nicholas", ("end_time", a_time(2008))),
        )
        before = batch.model_dump_json()
        checks.stored(doc, "extraction", batch)
        alert = only(run, "implausible-date")
        assert alert["value"] == 2
        assert alert.get("limit") is None
        assert (
            alert["before_birth"],
            alert["after_birth"],
            alert["max_years_after_birth"],
        ) == (1, 1, 120)
        assert alert["stage"] == "extraction"
        assert batch.model_dump_json() == before


# -- distributions ------------------------------------------------------------


def a_text_document(tmp_path, checks, name, claims, *, findings=None):
    """A one-page text document stored with `claims` claims, and its
    critique with `findings` findings when given."""
    doc = a_doc(tmp_path, name, text=NATURALISATION)
    checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
    checks.stored(doc, "extraction", a_batch(*["en"] * claims))
    if findings is not None:
        checks.stored(
            doc,
            "critique",
            [
                {
                    "entity": "e1",
                    "axis": "value",
                    "category": "misapplied_property",
                    "issue": "doubtful",
                }
            ]
            * findings,
        )
    return doc


class TestDistributions:
    SMALL = {
        "checks": {
            "claim-yield": {"min-samples": 3},
            "entity-yield": {"min-samples": 3},
            "critic-rate": {"min-samples": 3},
        }
    }

    @staticmethod
    def a_classed_document(
        tmp_path, checks, name, claims, document_class, kind="media"
    ):
        """A one-page scan whose transcription record gives its class,
        stored with `claims` claims."""
        doc = a_doc(tmp_path, name, kind=kind, text=NATURALISATION)
        transcription = vision(NATURALISATION, "en")
        transcription.document_class = document_class
        checks.stored(doc, "transcription", transcription)
        checks.stored(doc, "extraction", a_batch(*["en"] * claims))
        return doc

    def test_yields_are_compared_within_a_document_class(self, tmp_path):
        """Captions and certificates yield very differently (#389): a
        certificate is measured against the certificates alone, never
        against the photographs beside it in the run."""
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            self.a_classed_document(
                tmp_path, checks, f"photo{n}.png", 1, "photograph"
            )
        for n in range(3):
            self.a_classed_document(
                tmp_path, checks, f"cert{n}.png", 30, "certificate"
            )
        assert "claim-yield" not in checks_raised(run)
        self.a_classed_document(
            tmp_path, checks, "cert3.png", 1, "certificate"
        )
        alert = only(run, "claim-yield")
        assert alert["subject"] == sha1_of("cert3.png")[:12]
        assert (alert["document_class"], alert["class_source"]) == (
            "certificate",
            "record",
        )

    def test_a_class_is_one_sample_whatever_the_kind(self, tmp_path):
        """A certificate an excerpt shows is measured against the scanned
        certificates: the class decides, not the kind."""
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            self.a_classed_document(
                tmp_path, checks, f"cert{n}.png", 30, "certificate"
            )
        self.a_classed_document(
            tmp_path, checks, "excerpt.png", 1, "certificate", "excerpt"
        )
        alert = only(run, "claim-yield")
        assert alert["subject"] == sha1_of("excerpt.png")[:12]
        assert (alert["document_class"], alert["samples"]) == (
            "certificate",
            3,
        )

    def test_the_unclassified_of_a_kind_are_one_sample(self, tmp_path):
        """A scan its record calls unclassified and one read with no
        record are measured together, within their kind."""
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            self.a_classed_document(
                tmp_path, checks, f"scan{n}.png", 30, UNCLASSIFIED
            )
        doc = a_doc(tmp_path, "pages.pdf", kind="media", text=NATURALISATION)
        checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
        checks.stored(doc, "extraction", a_batch("en"))
        alert = only(run, "claim-yield")
        assert alert["subject"] == sha1_of("pages.pdf")[:12]
        assert (alert["document_class"], alert["class_source"]) == (
            UNCLASSIFIED,
            "none",
        )
        assert alert["samples"] == 3

    def test_a_document_without_a_record_is_unclassified_and_says_so(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"tree{n}.txt", 40)
        a_text_document(tmp_path, checks, "card.txt", 1)
        alert = only(run, "claim-yield")
        assert (alert["document_class"], alert["class_source"]) == (
            UNCLASSIFIED,
            "none",
        )

    def test_a_claim_yield_far_above_the_run_s(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"card{n}.txt", 5)
        assert run.alerts() == []
        outlier = a_text_document(tmp_path, checks, "tree.txt", 60)
        alert = only(run, "claim-yield")
        assert alert["subject"] == outlier.sha1[:12]
        assert (alert["value"], alert["median"], alert["samples"]) == (
            60.0,
            5.0,
            3,
        )
        assert alert["limit"] == 8

    def test_a_claim_yield_far_below_the_run_s(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"tree{n}.txt", 40)
        a_text_document(tmp_path, checks, "card.txt", 1)
        assert only(run, "claim-yield")["value"] == 1.0

    def test_a_yield_near_the_run_s_is_quiet(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for claims in (5, 6, 4, 30):
            a_text_document(tmp_path, checks, f"card{claims}.txt", claims)
        assert "claim-yield" not in checks_raised(run)

    def test_a_batch_without_claims_is_no_yield_outlier(self, tmp_path):
        """None at all is the zero checks' finding, and joins no yield's
        distribution."""
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"card{n}.txt", 5)
        a_text_document(tmp_path, checks, "blank.txt", 0)
        assert "claim-yield" not in checks_raised(run)
        assert (
            checks._samples[("claim-yield", UNCLASSIFIED, "text")] == [5.0] * 3
        )

    def test_no_document_is_judged_before_enough_samples(self, tmp_path):
        run, checks = make_checks(tmp_path)  # ten samples by default
        for n in range(5):
            a_text_document(tmp_path, checks, f"card{n}.txt", 5)
        a_text_document(tmp_path, checks, "tree.txt", 200)
        assert "claim-yield" not in checks_raised(run)

    def test_an_entity_yield_far_above_the_run_s(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            doc = a_doc(tmp_path, f"card{n}.txt", text=NATURALISATION)
            checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
            checks.stored(doc, "extraction", a_batch("en", entities=2))
        doc = a_doc(tmp_path, "tree.txt", text=NATURALISATION)
        checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
        checks.stored(doc, "extraction", a_batch("en", entities=40))
        assert only(run, "entity-yield")["value"] == 40.0

    def test_kinds_are_judged_apart(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"card{n}.txt", 5)
        doc = a_doc(tmp_path, "tree.png", kind="media", text=NATURALISATION)
        checks.stored(doc, "transcription", vision(NATURALISATION, "en"))
        checks.stored(doc, "extraction", a_batch(*["en"] * 60))
        assert "claim-yield" not in checks_raised(run)

    def test_a_critic_finding_rate_far_above_the_run_s(self, tmp_path):
        run, checks = make_checks(tmp_path, self.SMALL)
        for n in range(3):
            a_text_document(tmp_path, checks, f"card{n}.txt", 4, findings=0)
        a_text_document(tmp_path, checks, "tree.txt", 4, findings=4)
        alert = only(run, "critic-rate")
        assert (alert["stage"], alert["value"], alert["median"]) == (
            "critique",
            0.8,
            0.0,
        )

    def test_the_outlier_rule(self):
        settings = load_config().checks.claim_yield
        others = [4.0, 5.0, 6.0] * 4
        assert anomalies.outlier("claim-yield", settings, 45.0, others)
        assert not anomalies.outlier("claim-yield", settings, 35.0, others)
        assert anomalies.outlier("claim-yield", settings, 0.1, others)
        assert not anomalies.outlier("claim-yield", settings, 1.0, others)
        # a median of none: the smoothing keeps a small value quiet
        zeros = [0.0] * 12
        assert not anomalies.outlier("claim-yield", settings, 3.0, zeros)
        assert anomalies.outlier("claim-yield", settings, 4.0, zeros)


# -- rates --------------------------------------------------------------------


def outcomes(run, stage, kind, pattern, start=0):
    """A stage attempt per letter of `pattern`: `.` done, `x` failed."""
    for n, mark in enumerate(pattern, start=start):
        doc = Doc(Path("."), f"{kind}-{n}", kind)
        attempt(
            run,
            doc,
            stage,
            failed="RuntimeError: engine down" if mark == "x" else None,
        )


def requests(run: RunDir) -> list[dict]:
    return [
        request
        for request in (
            json.loads(line)
            for line in (run.path / "control.jsonl").read_text().splitlines()
        )
        if request["action"] != "start"
    ]


class TestRates:
    def test_a_stage_failing_above_the_share_over_its_window(self, tmp_path):
        run, checks = make_checks(tmp_path)
        outcomes(run, "extraction", "text", "......." + "xxx")
        checks.poll()
        alert = only(run, "stage-failures")
        assert (alert["value"], alert["count"], alert["of"]) == (0.3, 3, 10)
        assert alert["status"] == "new"
        # not fail-fast by default
        assert not (run.path / "control.jsonl").exists()

    def test_it_fires_again_only_once_the_share_has_fallen_back(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        outcomes(run, "extraction", "text", "......." + "xxx" + "x")
        checks.poll()
        assert checks_raised(run) == ["stage-failures"]
        outcomes(run, "extraction", "text", "." * 10, start=11)
        checks.poll()
        assert checks_raised(run) == ["stage-failures"]
        outcomes(run, "extraction", "text", "xx", start=21)
        checks.poll()
        assert checks_raised(run) == ["stage-failures", "stage-failures"]

    def test_too_few_dispatches_are_not_judged(self, tmp_path):
        run, checks = make_checks(tmp_path)
        outcomes(run, "extraction", "text", "xxxxxxxxx")
        checks.poll()
        assert "stage-failures" not in checks_raised(run)

    def test_failures_all_from_cached_answers_are_carried(self, tmp_path):
        """Extractions that fail the same way from the answers the cache
        kept, as a run replaying an earlier one's calls does."""
        run, checks = make_checks(tmp_path)
        outcomes(run, "extraction", "text", ".......")
        for n in range(3):
            attempt(
                run,
                Doc(tmp_path, f"replayed-{n}"),
                "extraction",
                hits=2,
                failed="ExtractionFailed: no claims extracted: 2 of 2 "
                "chunks failed (c001: RuntimeError: engine down)",
            )
        checks.poll()
        assert only(run, "stage-failures")["status"] == "carried"

    def test_a_fail_fast_stage_failure_rate_pauses_the_stage(self, tmp_path):
        run, checks = make_checks(
            tmp_path, {"checks": {"stage-failures": {"fail-fast": True}}}
        )
        outcomes(run, "critique", "text", "......." + "xxx")
        checks.poll()
        (pause,) = requests(run)
        assert (pause["action"], pause["scope"], pause["by"]) == (
            "pause",
            "stage:critique",
            "anomaly",
        )
        assert only(run, "stage-failures")["pause"] == "stage:critique"

    def test_most_of_the_first_documents_of_a_kind_failing(self, tmp_path):
        run, checks = make_checks(tmp_path)
        outcomes(run, "transcription", "text", "...")
        outcomes(run, "transcription", "media", "xxxxx")
        checks.poll()
        assert "kind-failures" not in checks_raised(run)
        outcomes(run, "transcription", "media", "x", start=5)
        checks.poll()
        alert = only(run, "kind-failures")
        assert (alert["value"], alert["limit"], alert["of"]) == (6, 5.0, 6)
        assert alert["status"] == "new"
        assert (alert["kind"], alert["pause"]) == ("media", "kind:media")
        (pause,) = requests(run)
        assert (pause["action"], pause["scope"], pause["by"]) == (
            "pause",
            "kind:media",
            "anomaly",
        )
        assert pause["check"] == "kind-failures"
        # the other kind goes on
        assert run.control().stopped(kind="text") is None
        assert run.control().stopped(kind="media") == "kind:media paused"
        # once per invocation
        outcomes(run, "transcription", "media", "x", start=6)
        checks.poll()
        assert checks_raised(run).count("kind-failures") == 1

    def test_a_kind_with_fewer_documents_than_the_first_ten(self, tmp_path):
        run, checks = make_checks(tmp_path)
        run.start(
            documents=[
                {"sha1": sha1_of(f"media-{n}"), "kind": "media"}
                for n in range(3)
            ]
        )
        outcomes(run, "transcription", "media", "x")
        checks.poll()
        assert "kind-failures" not in checks_raised(run)
        outcomes(run, "transcription", "media", "x", start=1)
        checks.poll()
        assert only(run, "kind-failures")["limit"] == 1.5

    def test_documents_after_the_first_ten_are_not_counted(self, tmp_path):
        run, checks = make_checks(tmp_path)
        outcomes(run, "transcription", "media", "xxxxx.....xxxxxx")
        checks.poll()
        assert "kind-failures" not in checks_raised(run)

    def test_a_kind_the_run_plans_one_document_of_is_never_paused(
        self, tmp_path
    ):
        """A single deck that fails leaves no other deck for a pause to
        protect (#376): the kind check leaves it alone, and the failure
        still shows in stage-failures."""
        run, checks = make_checks(
            tmp_path,
            {"checks": {"stage-failures": {"window": 1, "min-count": 1}}},
        )
        run.start(
            documents=[
                {"sha1": sha1_of("presentation-0"), "kind": "presentation"},
                *(
                    {"sha1": sha1_of(f"text-{n}"), "kind": "text"}
                    for n in range(3)
                ),
            ]
        )
        outcomes(run, "transcription", "presentation", "x")
        checks.poll()
        assert "kind-failures" not in checks_raised(run)
        assert requests(run) == []
        assert only(run, "stage-failures")["kind"] == "presentation"

    def test_a_kind_the_run_plans_two_documents_of_is_still_paused(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        run.start(
            documents=[
                {"sha1": sha1_of(f"presentation-{n}"), "kind": "presentation"}
                for n in range(2)
            ]
        )
        outcomes(run, "transcription", "presentation", "xx")
        checks.poll()
        alert = only(run, "kind-failures")
        assert (alert["value"], alert["limit"], alert["pause"]) == (
            2,
            1.0,
            "kind:presentation",
        )
        (pause,) = requests(run)
        assert pause["scope"] == "kind:presentation"

    def test_the_fewest_documents_judged_is_a_threshold(self, tmp_path):
        """With min-documents 1, a kind of one document that fails is
        paused."""
        run, checks = make_checks(
            tmp_path, {"checks": {"kind-failures": {"min-documents": 1}}}
        )
        run.start(
            documents=[
                {"sha1": sha1_of("presentation-0"), "kind": "presentation"}
            ]
        )
        outcomes(run, "transcription", "presentation", "x")
        checks.poll()
        assert only(run, "kind-failures")["pause"] == "kind:presentation"

    def test_most_recent_engine_calls_failing(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = Doc(tmp_path, "letter.png", "media")
        about = dict(subject=doc.sha1, kind="media", role="transcribe")
        # calls paused or interrupted are no failures, nor counted at all
        for outcome in ["error"] * 6 + ["paused", "interrupted"] * 2:
            run.emit("call", stage="transcription", outcome=outcome, **about)
        checks.poll()
        assert "call-failures" not in checks_raised(run)
        for outcome in ["ok"] * 3 + ["error"]:
            run.emit("call", stage="transcription", outcome=outcome, **about)
        checks.poll()
        alert = only(run, "call-failures")
        assert (alert["value"], alert["count"], alert["of"]) == (0.7, 7, 10)
        assert alert["status"] == "new"
        assert alert["pause"] == "stage:transcription"
        (pause,) = requests(run)
        assert pause["scope"] == "stage:transcription"

    def test_calls_the_engine_gave_up_on_never_pause_a_stage(self, tmp_path):
        """The engine answered those calls, past its output limit: they
        are no engine failures, so they never enter the window whose step
        rule pauses the stage (#7). Calls failing otherwise still do."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        about = dict(subject=doc.sha1, kind="text", role="extract")
        for _ in range(12):
            run.emit("call", stage="extraction", **GAVE_UP, **about)
        checks.poll()
        assert "call-failures" not in checks_raised(run)
        for _ in range(10):
            run.emit("call", stage="extraction", outcome="error", **about)
        checks.poll()
        alert = only(run, "call-failures")
        assert (alert["value"], alert["count"], alert["of"]) == (1.0, 10, 10)

    def test_a_call_that_failed_after_the_notice_is_a_failed_call(
        self, tmp_path
    ):
        """A call that crashed or timed out after the CLI printed its
        output-limit notice names max_output_tokens, but the engine raised
        no OutputLimitReached for it: it failed, and counts (#7)."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        about = dict(subject=doc.sha1, kind="text", role="extract")
        for _ in range(10):
            run.emit(
                "call",
                stage="extraction",
                outcome="error",
                error="RuntimeError",
                api_error="max_output_tokens",
                **about,
            )
        checks.poll()
        alert = only(run, "call-failures")
        assert (alert["value"], alert["count"], alert["of"]) == (1.0, 10, 10)

    def test_half_the_recent_calls_failing_is_not_most(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = Doc(tmp_path, "letter.png", "media")
        about = dict(subject=doc.sha1, kind="media", role="transcribe")
        for outcome in ["ok", "error"] * 10:
            run.emit("call", stage="transcription", outcome=outcome, **about)
        checks.poll()
        assert "call-failures" not in checks_raised(run)


# -- the engine's model -------------------------------------------------------


def a_call(run: RunDir, doc: Doc, model: str | None, **fields) -> None:
    """An engine call of the extraction, with the model its stream
    reported (None: its stream named none)."""
    about = {
        "subject": doc.sha1,
        "kind": doc.kind,
        "role": "extract",
        "stage": "extraction",
        "call": "fork",
        "outcome": "ok",
        "model": model,
    }
    run.emit("call", **(about | fields))


def models_manifest(run: RunDir, model=None, client=None) -> None:
    """The run manifest's extract role: the model the run configures, and
    the one its engine client asks for once it has started."""
    run.update_manifest(
        roles={
            "extract": {
                "model": model,
                "client": None if client is None else {"model": client},
            }
        }
    )


class TestModel:
    def test_a_call_on_the_role_s_model_is_quiet(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-sonnet-5")
        checks.poll()
        assert run.alerts() == []

    def test_a_call_on_another_model_than_the_manifest_s(self, tmp_path):
        """An undated name the engine ran on a dated snapshot, say."""
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-sonnet-5-20260901")
        checks.poll()
        alert = only(run, "model-mismatch")
        assert (alert["value"], alert["limit"]) == (
            "claude-sonnet-5-20260901",
            "claude-sonnet-5",
        )
        assert (alert["role"], alert["against"]) == ("extract", "manifest")
        assert (alert["stage"], alert["kind"], alert["status"]) == (
            "extraction",
            "text",
            "new",
        )
        assert alert["subject"] == doc.sha1[:12]

    def test_the_model_the_client_asks_for_comes_first(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", client="claude-opus-5-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-opus-5-5")
        a_call(run, doc, "claude-sonnet-5")
        checks.poll()
        alert = only(run, "model-mismatch")
        assert (alert["value"], alert["limit"]) == (
            "claude-sonnet-5",
            "claude-opus-5-5",
        )

    def test_the_configured_model_stands_until_a_client_starts(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-opus-5-5")
        checks.poll()
        assert only(run, "model-mismatch")["limit"] == "claude-sonnet-5"

    def test_a_context_window_selector_is_no_other_model(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-opus-5-5[1m]", "claude-opus-5-5[1m]")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-opus-5-5")
        a_call(run, doc, "claude-opus-5-5[1m]")
        checks.poll()
        assert run.alerts() == []

    @pytest.mark.parametrize("configured", [None, "sonnet", "opus-latest"])
    def test_a_role_with_no_model_id_keeps_its_first_resolution(
        self, tmp_path, configured
    ):
        """The engine's default, or an alias, names no model to compare
        with: the role's first call reports the one it resolved to, and a
        later call on another model is off it."""
        run, checks = make_checks(tmp_path)
        models_manifest(run, configured, configured)
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-sonnet-5-20260901")
        a_call(run, doc, "claude-sonnet-5-20260901")
        checks.poll()
        assert run.alerts() == []
        a_call(run, doc, "claude-sonnet-5-20261101")
        checks.poll()
        alert = only(run, "model-mismatch")
        assert (alert["value"], alert["limit"], alert["against"]) == (
            "claude-sonnet-5-20261101",
            "claude-sonnet-5-20260901",
            "first call",
        )

    def test_a_role_the_manifest_does_not_know(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-sonnet-5")
        a_call(run, doc, "claude-opus-5-5")
        checks.poll()
        assert only(run, "model-mismatch")["against"] == "first call"

    def test_once_per_role_and_model(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        for name in ("brief.txt", "karte.txt"):
            doc = a_doc(tmp_path, name)
            a_call(run, doc, "claude-opus-5-5")
            a_call(run, doc, "claude-haiku-5")
        checks.poll()
        alerts = [a["value"] for a in run.alerts()]
        assert alerts == ["claude-opus-5-5", "claude-haiku-5"]

    def test_a_model_with_a_selector_is_the_same_model_once(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-haiku-5")
        a_call(run, doc, "claude-haiku-5[1m]")
        checks.poll()
        assert [a["value"] for a in run.alerts()] == ["claude-haiku-5"]

    def test_each_role_has_its_own_model_and_its_own_alert(self, tmp_path):
        run, checks = make_checks(tmp_path)
        run.update_manifest(
            roles={
                "extract": {
                    "model": "claude-sonnet-5",
                    "client": {"model": "claude-sonnet-5"},
                },
                "critique": {
                    "model": "claude-opus-5-5",
                    "client": {"model": "claude-opus-5-5"},
                },
            }
        )
        doc = a_doc(tmp_path, "brief.txt")
        critique = {"role": "critique", "stage": "critique"}
        a_call(run, doc, "claude-sonnet-5")
        a_call(run, doc, "claude-opus-5-5", **critique)
        checks.poll()
        assert run.alerts() == []
        a_call(run, doc, "claude-haiku-5")
        a_call(run, doc, "claude-haiku-5", **critique)
        checks.poll()
        assert sorted(
            (a["role"], a["value"], a["limit"]) for a in run.alerts()
        ) == [
            ("critique", "claude-haiku-5", "claude-opus-5-5"),
            ("extract", "claude-haiku-5", "claude-sonnet-5"),
        ]

    def test_each_role_keeps_its_own_first_resolution(self, tmp_path):
        """Roles the manifest names no model for each take their own
        first call's model."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        critique = {"role": "critique", "stage": "critique"}
        a_call(run, doc, "claude-sonnet-5")
        a_call(run, doc, "claude-opus-5-5", **critique)
        a_call(run, doc, "claude-opus-5-5", **critique)
        checks.poll()
        assert run.alerts() == []

    def test_a_call_whose_stream_named_no_model(self, tmp_path):
        """It neither raises an alert nor becomes the role's model."""
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, None, outcome="error")
        a_call(run, doc, "claude-sonnet-5")
        a_call(run, doc, "claude-sonnet-5")
        checks.poll()
        assert run.alerts() == []

    def test_a_failed_or_paused_call_s_model_counts(self, tmp_path):
        run, checks = make_checks(tmp_path)
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        doc = a_doc(tmp_path, "brief.txt")
        a_call(run, doc, "claude-opus-5-5", outcome="paused")
        checks.poll()
        assert only(run, "model-mismatch")["value"] == "claude-opus-5-5"

    def test_it_can_be_switched_off(self, tmp_path):
        run, checks = make_checks(
            tmp_path, {"checks": {"model-mismatch": {"enabled": False}}}
        )
        models_manifest(run, "claude-sonnet-5", "claude-sonnet-5")
        a_call(run, a_doc(tmp_path, "brief.txt"), "claude-opus-5-5")
        checks.poll()
        assert run.alerts() == []


# -- fail-fast pauses ---------------------------------------------------------


def checks_of(run: RunDir, clock, timers=None) -> AnomalyChecks:
    """The checks of `run`'s invocation, on the test's clock."""
    return AnomalyChecks(
        run,
        load_config(),
        monotonic=clock.steady,
        timer=(timers if timers is not None else Timers()).on(clock),
        page_count=lambda source: None,
    )


def paused_media(tmp_path, **options):
    """Checks that have paused the media kind: six of its first
    documents failed their transcription."""
    run, checks = make_checks(tmp_path, **options)
    outcomes(run, "transcription", "media", "xxxxxx")
    checks.poll()
    assert run.control().stopped(kind="media") == "kind:media paused"
    return run, checks


def standing(run: RunDir) -> list[dict]:
    """The requests that stop a stage or a kind now, as a driver that
    carries them into its next invocation reads them (the scratch driver's
    `_stops_in_force`, #194): since the latest start marker, the abort
    that holds a slice, or the pause that does."""
    held: dict[str, dict] = {}
    for request in requests_since_start(run):
        scope = request.get("scope") or "run"
        if (held.get(scope) or {}).get("action") == "abort":
            continue
        if request["action"] in ("pause", "abort"):
            held[scope] = request
        elif request["action"] == "resume":
            held.pop(scope, None)
    return [r for s, r in held.items() if s.startswith(("stage:", "kind:"))]


def requests_since_start(run: RunDir) -> list[dict]:
    found: list[dict] = []
    for request in rundir.read_jsonl(run.path / "control.jsonl"):
        if request.get("action") == "start":
            found = []
        else:
            found.append(request)
    return found


def carry(run: RunDir, carried: list[dict]) -> None:
    """Ask again for the stops `carried`, as that driver does after its
    start marker: with who asked for them and why, at a new time, and
    marked carried."""
    for request in carried:
        which, _, name = request["scope"].partition(":")
        run.request(
            request["action"],
            stage=name if which == "stage" else None,
            kind=name if which == "kind" else None,
            by=request.get("by") or "operator",
            reason=request.get("reason") or "",
            carried=True,
        )


def reinvoke(run: RunDir) -> None:
    """Invoke the run again as the scratch driver does after a wait for a
    subscription limit (#194, #297): a start marker, and after it the
    requests in force just before it, asked for again."""
    carried = standing(run)
    run.start()
    carry(run, carried)


def failing_once(locked, error: Exception):
    """A stand-in for anomalies._locked_control whose first call raises
    `error`, as a control file that cannot be opened or read does; the
    calls after it read the file."""
    errors = [error]

    def call(path):
        if errors:
            raise errors.pop()
        return locked(path)

    return call


def held_elsewhere(path: Path) -> str:
    """Whether another process can take the lock a control file's
    appenders take: `held` or `free`."""
    probe = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    print('held')\n"
        "else:\n"
        "    print('free')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


class TestFailFastPauses:
    def test_a_pause_nobody_resumes_times_out_into_an_abort(
        self, tmp_path, clock
    ):
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        (timer,) = timers.started
        assert timer.seconds > 1800
        clock.advance(1802)
        timers.fire()
        pause, abort = requests(run)
        assert (abort["action"], abort["scope"], abort["by"]) == (
            "abort",
            "kind:media",
            "anomaly",
        )
        assert abort["reason"] == "kind-failures: not resumed within 1800 s"
        # the driver's fold sees the slice aborted, and nothing else
        assert run.control().stopped(kind="media") == "kind:media aborted"
        assert run.control().stopped(kind="text") is None
        assert fold_control(requests(run)).scopes == {"kind:media": "aborted"}

    def test_a_later_check_aborts_it_too(self, tmp_path, clock):
        run, checks = paused_media(tmp_path, clock=clock)
        clock.advance(1700)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(102)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_resumed_slice_is_not_aborted_and_is_counted_afresh(
        self, tmp_path, clock
    ):
        timers = Timers()
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(600)
        run.request("resume", kind="media", reason="renderer installed")
        clock.advance(1202)
        timers.fire()
        assert run.control().stopped(kind="media") is None
        assert [r["action"] for r in requests(run)] == ["pause", "resume"]
        # what comes after the resume is counted on its own
        outcomes(run, "transcription", "media", "xxxxx", start=6)
        checks.poll()
        assert checks_raised(run).count("kind-failures") == 1
        outcomes(run, "transcription", "media", "x", start=11)
        checks.poll()
        assert checks_raised(run).count("kind-failures") == 2
        # the new pause stands for a timeout of its own
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(1700)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(102)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_pause_the_operator_also_asked_for_is_theirs(
        self, tmp_path, clock
    ):
        timers = Timers()
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        run.request("pause", kind="media", reason="looking into it")
        clock.advance(7200)
        timers.fire()
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"

    def test_a_slice_stopped_already_is_not_paused_again(self, tmp_path):
        timers = Timers()
        run, checks = make_checks(tmp_path, timers=timers)
        run.request("pause", kind="media", reason="looking into it")
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        assert [r["by"] for r in requests(run)] == ["operator"]
        assert timers.started == []
        assert "pause" not in only(run, "kind-failures")

    def test_no_timeout_holds_the_pause_until_someone_resumes(
        self, tmp_path, clock
    ):
        timers = Timers()
        run, checks = paused_media(
            tmp_path,
            overrides={"pause-timeout-s": None},
            clock=clock,
            timers=timers,
        )
        assert timers.started == []
        clock.advance(86400)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"

    def test_a_control_line_the_driver_cannot_fold_is_left_out(
        self, tmp_path, clock
    ):
        run = RunDir(tmp_path / "run")
        run.path.mkdir(parents=True)
        (run.path / "control.jsonl").write_text(
            '{"action": "pause", "scope": ["kind:media"], "by": "someone"}\n'
        )
        run, checks = make_checks(tmp_path, clock=clock)
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        clock.advance(1802)
        checks.poll()
        assert [r["action"] for r in requests(run)] == [
            "pause",
            "pause",
            "abort",
        ]

    def test_a_new_invocation_starts_with_no_pause(self, tmp_path):
        run, _checks = paused_media(tmp_path)
        RunDir(run.path).start()
        assert run.control().stopped(kind="media") is None

    def test_a_later_invocation_s_pause_is_timed_from_its_own_request(
        self, tmp_path, clock
    ):
        """A pause an earlier invocation left standing is no part of the
        next one's: the start marker clears it, and the next pause stands
        for a timeout of its own."""
        paused_media(tmp_path, clock=clock)
        clock.advance(1000)
        run = RunDir(tmp_path / "run")
        run.start()
        checks = checks_of(run, clock)
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(1000)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(802)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_later_invocation_s_pause_times_out_after_an_earlier_abort(
        self, tmp_path, clock
    ):
        """An abort holds for the invocation it stopped; the same slice
        paused in the next one times out into an abort of its own."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(1802)
        timers.fire()
        run = RunDir(tmp_path / "run")
        assert run.control().stopped(kind="media") == "kind:media aborted"
        run.start()
        checks = checks_of(run, clock)
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(1802)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    @pytest.mark.parametrize("lag", [1, 600, 1801, 3600])
    def test_a_pause_times_out_on_the_monotonic_clock_whatever_the_wall_clock(
        self, tmp_path, clock, lag
    ):
        """The timer waits on the monotonic clock, and its look judges the
        pause's age on that clock too: once the timeout has passed on it,
        the slice is aborted even when the wall clock has fallen behind or
        stepped back meanwhile (#432). After a timer's abort, past the
        time the checks' request reached the timeout, the next look comes
        a timeout and a second later on the same clock."""
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        (first,) = timers.waiting()
        assert first.seconds == 1801
        clock.advance(1801)
        clock.step(-lag)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        abort = requests(run)[-1]
        assert (abort["action"], abort["by"], abort["check"]) == (
            "abort",
            "anomaly",
            "kind-failures",
        )
        # the slice stays watched, by one timer
        (pending,) = timers.waiting()
        assert pending.seconds == 1801

    @pytest.mark.parametrize("ahead", [600, 3600])
    def test_a_wall_clock_ahead_aborts_nothing_early(
        self, tmp_path, clock, ahead
    ):
        """A wall clock that reads ahead while the pause stands, at a
        check's look or as a timer is armed, and is then corrected back,
        neither aborts the slice before the timeout has passed on the
        monotonic clock nor moves its timer."""
        timers = Timers()
        clock.step(ahead)
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        (first,) = timers.waiting()
        assert first.seconds == 1801
        clock.step(ahead)
        clock.advance(1799)
        checks.poll()
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media paused"
        assert timers.waiting() == [first]
        clock.step(-2 * ahead)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(2)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_stale_carry_after_an_abort_on_a_lagging_wall_clock(
        self, tmp_path, clock
    ):
        """The timer aborts the pause while the wall clock lags, and a
        driver carries the pause from a reading older than the abort,
        stamped on the lagging clock: the next look, the new invocation's
        check, aborts it again at once."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        run = RunDir(tmp_path / "run")
        carried = standing(run)
        clock.advance(1801)
        clock.step(-600)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        run.start()
        carry(run, carried)
        assert run.control().stopped(kind="media") == "kind:media paused"
        checks_of(run, clock, timers).poll()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_the_timeout_has_passed_at_its_last_instant(self, tmp_path, clock):
        """A pause has stood its timeout once the monotonic clock reaches
        the timeout since the checks asked for it: short of it the pause
        stands, at it a check's look aborts it."""
        timers = Timers()
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(1799.5)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(0.5)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_timer_that_runs_early_aborts_nothing(self, tmp_path, clock):
        """A timer whose look comes before its wait has run out on the
        monotonic clock, as a wait cut short would, is not trusted for the
        time: the look judges on the clock, so the pause stands, and the
        next look comes when the checks' request reaches the timeout,
        whether the pause still stands or a start marker has cleared
        it."""
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        (first,) = timers.waiting()
        clock.advance(1000)
        first.callback()
        assert run.control().stopped(kind="media") == "kind:media paused"
        # called directly, the timers stay recorded as waiting
        _first, second = timers.started
        assert second.seconds == 801
        run.start()
        second.callback()
        assert run.control().stopped(kind="media") is None
        _first, _second, third = timers.started
        assert third.seconds == 801

    def test_a_fake_timer_runs_only_once_its_wait_has_run_out(
        self, tmp_path, clock
    ):
        """The fake Timers run a timer only when its wait has run out on
        the monotonic clock, as a real one runs, never just before."""
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        (first,) = timers.waiting()
        clock.advance(first.seconds - 0.5)
        timers.fire()
        assert timers.waiting() == [first]
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(0.5)
        timers.fire()
        assert first not in timers.waiting()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    # the wall clock's offset from the real one: none, stepped back an hour
    # or a year, and far ahead
    @pytest.mark.parametrize(
        "offset", [0, -3600, -365 * 86400, 10 * 365 * 86400]
    )
    def test_the_timer_runs_on_a_thread_of_its_own(
        self, tmp_path, clock, offset
    ):
        """With the real timer, started with no wait, and the checks' fake
        clocks: an abort that no later check makes. The pause stands when
        the check that asked for it ends, and the timer's thread, of a
        daemon, aborts it in its own time, once its wait has run out on
        the fake monotonic clock; the slice stays watched, by the next
        timer. The test waits on the thread (join), never on a time, and
        reads no real clock, so the wall clock frozen, stepped back or far
        ahead changes nothing."""
        clock.step(offset)
        asked: list[float] = []
        started: list[threading.Timer] = []
        seen = threading.Event()

        def start(seconds, callback):
            # the first timer runs at once, once the test has moved the
            # monotonic clock to its due; the next ones are started with
            # nothing to do, their waits only recorded
            asked.append(seconds)
            if started:
                started.append(START_TIMER(0, lambda: None))
            else:

                def released():
                    # a hang guard only: the test sets it
                    assert seen.wait(timeout=HANG_GUARD)
                    callback()

                started.append(START_TIMER(0, released))
            return started[-1]

        run = RunDir(tmp_path / "run")
        checks = AnomalyChecks(
            run,
            load_config(overrides={"pause-timeout-s": 2}),
            monotonic=clock.steady,
            timer=start,
        )
        try:
            outcomes(run, "transcription", "media", "xxxxxx")
            checks.poll()
            assert run.control().stopped(kind="media") == "kind:media paused"
            first = started[0]
            # due a timeout and a second on, on the monotonic clock
            assert asked[0] == 3
            clock.advance(asked[0])
            seen.set()
            # a hang guard only: the thread runs at once
            first.join(timeout=HANG_GUARD)
            assert not first.is_alive()
            assert first.daemon
            assert run.control().stopped(kind="media") == "kind:media aborted"
            assert len(started) == 2 and started[1].daemon
            # the next look a full timeout and a second later
            assert asked[1] == 3
        finally:
            seen.set()
            for timer in started:
                timer.cancel()
                timer.join(timeout=HANG_GUARD)

    def test_a_timer_waits_at_most_the_longest_wait_a_thread_can(
        self, monkeypatch
    ):
        """The real timer's starter clamps its wait to the longest a thread
        can make, and starts a daemon timer; here the timer is made but its
        thread never starts."""
        made: list[threading.Timer] = []

        class Unstarted(threading.Timer):
            def start(self) -> None:
                made.append(self)

        monkeypatch.setattr(threading, "Timer", Unstarted)
        timer = START_TIMER(threading.TIMEOUT_MAX + 1, lambda: None)
        assert made == [timer]
        assert timer.interval == threading.TIMEOUT_MAX
        assert timer.daemon

    def test_a_carried_pause_times_out_from_the_checks_request(
        self, tmp_path, clock
    ):
        """A driver that invokes the run again after a subscription limit
        asks again, after its start marker, for the requests in force
        before it, with who asked and why and a new time, as the scratch
        driver (#194, #297) does. The checks keep watching the slice, and
        the carried pause times out from their own request, with no check
        made in the new invocation."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(600)
        run = RunDir(tmp_path / "run")
        reinvoke(run)
        clock.advance(1199)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(2)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        abort = requests(run)[-1]
        assert (abort["action"], abort["by"], abort["check"]) == (
            "abort",
            "anomaly",
            "kind-failures",
        )

    def test_re_invocations_do_not_restart_a_pause_s_timeout(
        self, tmp_path, clock
    ):
        """The carry stamps each request it asks for again with a new time,
        so a timeout measured from the carried request would start again
        at every re-invocation (#297's review). Re-invoked every 700 s,
        across the timeout, the pause is aborted once the checks' own
        request is 1800 s old, though the carried one is 401 s old."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        run = RunDir(tmp_path / "run")
        for _ in range(2):
            clock.advance(700)
            reinvoke(run)
            (carried,) = requests_since_start(run)
            assert carried["at"] == clock.at.isoformat(timespec="seconds")
            timers.fire()
            assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(401)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    @pytest.mark.parametrize("delay", [0, 4000])
    def test_a_stale_carry_of_an_aborted_pause_is_aborted_again(
        self, tmp_path, clock, delay
    ):
        """A driver that reads the pauses to carry before its next
        invocation's preflight, as the scratch driver once did (#297), can
        carry a pause the checks aborted in between. The slice stays
        watched, and the stale pause, past the timeout since the checks
        asked for it, is aborted at the next look, however long the
        preflight took."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        run = RunDir(tmp_path / "run")
        carried = standing(run)
        clock.advance(1802)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        # the look that aborted, past the checks' request's timeout, looks
        # again a timeout later
        (pending,) = timers.waiting()
        assert pending.seconds == 1801
        # a preflight that outlasts the timeout: the slice is looked at
        # again meanwhile, and stays watched
        for _ in range(delay // 1801):
            clock.advance(1801)
            timers.fire()
        clock.advance(delay % 1801)
        run.start()
        carry(run, carried)
        assert run.control().stopped(kind="media") == "kind:media paused"
        # the next look, the pending timer's
        clock.advance(1801 - delay % 1801)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        assert [r["action"] for r in requests(run)] == [
            "pause",
            "abort",
            "pause",
            "abort",
        ]
        assert len(timers.waiting()) == 1

    def test_a_pause_carried_after_a_look_between_start_and_carry(
        self, tmp_path, clock
    ):
        """The new invocation's checks look just after the driver's start
        marker has cleared the pause and just before the carry asks for it
        again: the slice stays watched, its next look comes when the
        checks' request reaches the timeout, and the carried pause is
        aborted then."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        (first,) = timers.waiting()
        clock.advance(600)
        run = RunDir(tmp_path / "run")
        carried = standing(run)
        run.start()
        checks_of(run, clock, timers).poll()
        assert run.control().stopped(kind="media") is None
        # the look keeps the pending timer, due when the checks' request
        # reaches its timeout, not one a timeout from this look
        assert timers.waiting() == [first]
        carry(run, carried)
        clock.advance(1199)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(2)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_timer_whose_look_fails_looks_again_a_timeout_later(
        self, tmp_path, clock, monkeypatch
    ):
        """A look the timer makes that fails, here on a full disk as it
        appends the abort, is an `anomaly-error` event under the watch's
        own invocation, and the slice is looked at again a timeout later,
        so that it never goes unwatched."""
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        watch = anomalies._watch_of(run.path)

        def full(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        with monkeypatch.context() as patch:
            patch.setattr(watch.run, "request", full)
            clock.advance(1802)
            timers.fire()
            assert run.control().stopped(kind="media") == "kind:media paused"
            (error,) = [
                event
                for event in run.events()
                if event["event"] == "anomaly-error"
            ]
            assert (error["error"], error["hook"]) == ("OSError", "timer")
            assert error["invocation"].startswith("watch-")
            (pending,) = timers.waiting()
            assert pending.seconds == 1801
        # the disk has room again, and the next look aborts
        clock.advance(1801)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_check_whose_look_fails_leaves_a_timer_a_timeout_later(
        self, tmp_path, clock, monkeypatch
    ):
        """The first look at a slice the checks have just paused is the
        check's own, at the end of its read of the run's events. When it
        fails, here as the control file cannot be opened, the check is an
        `anomaly-error` event, and the slice still has a timer, a timeout
        later, whose look aborts the pause."""
        timers = Timers()
        run = RunDir(tmp_path / "run")
        anomalies.attach(
            run,
            load_config(),
            monotonic=clock.steady,
            timer=timers.on(clock),
            page_count=lambda source: None,
        )
        monkeypatch.setattr(
            anomalies,
            "_locked_control",
            failing_once(
                anomalies._locked_control,
                OSError(errno.EMFILE, "Too many open files"),
            ),
        )
        outcomes(run, "transcription", "media", "xxxxxx")
        anomalies.failed(
            run, Doc(tmp_path, "media-5", "media"), "transcription"
        )
        assert run.control().stopped(kind="media") == "kind:media paused"
        (error,) = [
            event
            for event in run.events()
            if event["event"] == "anomaly-error"
        ]
        assert (error["error"], error["hook"]) == ("OSError", "failed")
        (pending,) = timers.waiting()
        assert pending.seconds == 1801
        clock.advance(1802)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_failed_look_stops_no_other_slice(
        self, tmp_path, clock, monkeypatch
    ):
        """A check looks at every watched slice; one whose look fails leaves
        the others to be looked at, and each slice has its timer."""
        timers = Timers()
        run = RunDir(tmp_path / "run")
        run.request("pause", kind="media", by="anomaly", reason="kind")
        run.request("pause", stage="critique", by="anomaly", reason="calls")
        watch = anomalies._watch_of(run.path)
        watch.watch(
            "kind:media", "kind-failures", 1800.0, clock.steady, timers
        )
        watch.watch(
            "stage:critique", "call-failures", 1800.0, clock.steady, timers
        )
        monkeypatch.setattr(
            anomalies,
            "_locked_control",
            failing_once(
                anomalies._locked_control,
                OSError(errno.EIO, "Input/output error"),
            ),
        )
        with pytest.raises(OSError):
            watch.look()
        # the failed look's timer comes a timeout and a second from now; the
        # other's when its pause reaches the timeout, and a second
        assert [timer.seconds for timer in timers.waiting()] == [1801, 1801]

    def test_a_wall_clock_that_steps_moves_no_look(self, tmp_path, clock):
        """The looks agree on when a slice is to be looked at again, on the
        monotonic clock: a wall clock stepped back or ahead keeps the
        pending timer."""
        timers = Timers()
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(1801)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        (idle,) = timers.waiting()
        for step in (-3600, 7200):
            clock.step(step)
            checks.poll()
            assert timers.waiting() == [idle]
            assert not idle.cancelled

    def late_failed_look(self, tmp_path, clock, timers, monkeypatch):
        """A slice watched from a pause of the checks, whose first look
        comes 600 s later and fails, leaving a timer a timeout and a
        second from then: later than the look the pause needs."""
        run = RunDir(tmp_path / "run")
        run.request("pause", kind="media", by="anomaly", reason="kind")
        watch = anomalies._watch_of(run.path)
        watch.watch(
            "kind:media", "kind-failures", 1800.0, clock.steady, timers
        )
        clock.advance(600)
        monkeypatch.setattr(
            anomalies,
            "_locked_control",
            failing_once(
                anomalies._locked_control,
                OSError(errno.EIO, "Input/output error"),
            ),
        )
        with pytest.raises(OSError):
            watch.look()
        (late,) = timers.waiting()
        assert late.seconds == 1801
        return watch, late

    def test_a_look_after_a_late_failed_one_brings_the_next_look_forward(
        self, tmp_path, clock, monkeypatch
    ):
        """A look that wants the slice looked at sooner than the pending
        timer is due, as after a failed look's timer, starts one for the
        look the slice needs, and cancels the pending one."""
        timers = Timers().on(clock)
        watch, late = self.late_failed_look(
            tmp_path, clock, timers, monkeypatch
        )
        watch.look()
        (sooner,) = timers.waiting()
        assert late.cancelled
        # when the pause reaches its timeout, and a second
        assert sooner.seconds == 1201

    def test_a_timer_that_cannot_start_leaves_the_pending_one(
        self, tmp_path, clock, monkeypatch
    ):
        """A sooner timer that cannot start, as when no thread can, leaves
        the pending one as it was, and the look raises."""
        timers = Timers().on(clock)
        watch, late = self.late_failed_look(
            tmp_path, clock, timers, monkeypatch
        )
        timers.refuse = 1
        with pytest.raises(RuntimeError):
            watch.look()
        assert timers.waiting() == [late]
        assert not late.cancelled

    def test_a_slice_has_one_pending_timer(self, tmp_path, clock):
        """A look keeps the pending timer when it is due soon enough, and
        a timer put in its place cancels it."""
        timers = Timers()
        run, checks = paused_media(tmp_path, clock=clock, timers=timers)
        for _ in range(3):
            clock.advance(10)
            checks.poll()
        assert len(timers.started) == 1
        run.request("resume", kind="media", reason="renderer installed")
        checks.poll()
        outcomes(run, "transcription", "media", "xxxxxx", start=6)
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        first, second = timers.started
        assert first.cancelled and not second.cancelled
        # a timer whose call began as another took its place does nothing
        first.callback()
        assert timers.waiting() == [second]
        timers.fire()
        assert len(timers.waiting()) == 1

    def test_an_abort_is_appended_under_the_control_file_s_lock(
        self, tmp_path, clock, monkeypatch
    ):
        """The look that aborts a slice holds the lock the control file's
        appenders take from reading it to appending the abort, so that no
        other process appends a resume between the two."""
        run, checks = paused_media(tmp_path, clock=clock)
        watch = anomalies._watch_of(run.path)
        request = watch.run.request
        probes = []

        def probed(*args, **kwargs):
            probes.append(held_elsewhere(run.path / "control.jsonl"))
            return request(*args, **kwargs)

        monkeypatch.setattr(watch.run, "request", probed)
        clock.advance(1802)
        checks.poll()
        assert probes == ["held"]
        assert run.control().stopped(kind="media") == "kind:media aborted"
        # and once the look is over, the lock is free
        assert held_elsewhere(run.path / "control.jsonl") == "free"


# -- new and carried ----------------------------------------------------------


class TestNewAndCarried:
    def sparse(self, tmp_path, **events):
        run, checks = make_checks(tmp_path, pages=lambda source: 3)
        doc = a_doc(tmp_path, "grave.pdf", kind="media", text=MARRIAGE)
        if events:
            attempt(run, doc, "transcription", **events)
        checks.stored(doc, "transcription", vision(GRAVESTONE))
        return only(run, "transcription-sparse")["status"]

    def test_an_output_the_cache_answered_whole_is_carried(self, tmp_path):
        assert self.sparse(tmp_path, hits=1, call_kind="complete") == (
            "carried"
        )

    def test_an_output_the_engine_answered_is_new(self, tmp_path):
        """One of its calls kept, one asked of the engine."""
        assert self.sparse(tmp_path, hits=1, misses=1, calls=["ok"]) == "new"

    def test_an_output_answered_without_the_cache_is_new(self, tmp_path):
        assert self.sparse(tmp_path, calls=["ok"]) == "new"

    def test_an_output_made_without_a_call_is_new(self, tmp_path):
        assert self.sparse(tmp_path, calls=[]) == "new"

    def test_an_output_made_from_adopted_answers_is_carried(self, tmp_path):
        run, checks = make_checks(tmp_path, pages=lambda source: 3)
        doc = a_doc(tmp_path, "grave.pdf", kind="media", text=MARRIAGE)
        where = dict(subject=doc.sha1, kind="media", stage="transcription")
        run.emit("started", **where)
        run.emit(
            "cache-hit",
            subject=doc.sha1,
            kind="media",
            role="transcribe",
            call="complete",
            key="k",
            reason="adopted",
        )
        run.emit("done", **where)
        checks.stored(doc, "transcription", vision(GRAVESTONE))
        assert only(run, "transcription-sparse")["status"] == "carried"

    def test_a_reused_output_is_carried(self, tmp_path):
        run, checks = make_checks(tmp_path, pages=lambda source: 3)
        doc = a_doc(tmp_path, "grave.pdf", kind="media", text=MARRIAGE)
        checks.reused(doc, "transcription", vision(GRAVESTONE))
        assert only(run, "transcription-sparse")["status"] == "carried"

    def test_an_alert_names_stage_check_document_value_and_status(
        self, tmp_path
    ):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        checks.reused(doc, "transcription", vision(MARRIAGE))
        checks.reused(doc, "extraction", a_batch())
        (alert,) = run.alerts()
        assert {
            key: alert[key]
            for key in ("stage", "check", "subject", "value", "status")
        } == {
            "stage": "extraction",
            "check": "zero-claims",
            "subject": doc.sha1[:12],
            "value": 302,
            "status": "carried",
        }
        (event,) = [e for e in run.events() if e["event"] == "alert"]
        assert (event["check"], event["status"]) == ("zero-claims", "carried")

    def test_an_output_is_alerted_once_an_invocation(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = a_doc(tmp_path, "brief.txt", text=MARRIAGE)
        for _ in range(2):
            checks.reused(doc, "transcription", vision(MARRIAGE))
            checks.reused(doc, "extraction", a_batch())
        assert checks_raised(run) == ["zero-claims"]


# -- the orchestrator's hooks -------------------------------------------------


def quiet(*args, **kwargs):
    pass


class Refusing:
    """A vision engine that refuses every page."""

    model_identifier = "fake-model"

    def complete(self, prompt, image_paths=()):
        return "I'm unable to assist with this request."


class Run(Orchestrator):
    """The wiki steps scripted; transcription runs for real (a text file
    as itself, an image through `vision`); extraction returns a fixed
    batch through its stage, and the critic `findings` findings for the
    source file of that name (none by default)."""

    def __init__(
        self, *args, batch=None, vision=None, findings=None, **kwargs
    ):
        kwargs.setdefault("exclusions", ExclusionList())
        kwargs.setdefault("log", quiet)
        super().__init__(*args, **kwargs)
        self.batch = batch if batch is not None else a_batch(entities=1)
        self.vision = vision
        self.findings = findings or {}

    def _llm(self, role, doc):
        return self.vision

    def _step_register(self, doc):
        return "Q23"

    def _step_extract(self, doc, transcription):
        return self._run_stage(doc, "extraction", lambda: self.batch)

    def _step_critique(self, doc, batch):
        found = [
            Finding(
                entity="e1",
                axis="subject",
                category="wrong_kind",
                issue="an organisation typed as a place",
            )
        ] * self.findings.get(doc.source.name, 0)
        return self._run_stage(doc, "critique", lambda: found)

    def _step_resolve(self, doc, batch):
        return batch


def a_folder(tmp_path, **files) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = folder / name.replace("_", ".")
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return folder


def by_invocation(run: RunDir) -> dict[str, list[tuple[str, str]]]:
    alerts: dict[str, list[tuple[str, str]]] = {}
    for alert in run.alerts():
        alerts.setdefault(alert["invocation"], []).append(
            (alert["check"], alert["status"])
        )
    return alerts


class TestTheOrchestrator:
    def test_a_transcription_is_checked_as_it_is_stored(self, tmp_path):
        orchestrator = Run(
            a_folder(tmp_path, photo_txt=STUDIO), tmp_path / "state"
        )
        orchestrator.run()
        alert = only(orchestrator.rundir, "language-short-text")
        (doc,) = orchestrator.documents()
        assert (alert["stage"], alert["subject"], alert["status"]) == (
            "transcription",
            doc.sha1[:12],
            "new",
        )

    def test_a_critique_is_checked_as_it_is_stored(
        self, tmp_path, monkeypatch
    ):
        thresholds = tmp_path / "thresholds.yaml"
        thresholds.write_text("checks:\n  critic-rate:\n    min-samples: 3\n")
        monkeypatch.setenv(anomalies.CONFIG_VARIABLE, str(thresholds))
        texts = {
            "a_txt": NATURALISATION,
            "b_txt": MARRIAGE,
            "c_txt": SOURCES["family-tree-1984"].text,
            "d_txt": SOURCES["interview-2002"].text,
        }
        orchestrator = Run(
            a_folder(tmp_path, **texts),
            tmp_path / "state",
            batch=a_batch("en"),
            findings={"d.txt": 4},
        )
        orchestrator.run()
        alert = only(orchestrator.rundir, "critic-rate")
        (last,) = [
            doc
            for doc in orchestrator.documents()
            if doc.source.name == "d.txt"
        ]
        assert (alert["stage"], alert["subject"], alert["value"]) == (
            "critique",
            last.sha1[:12],
            2.0,
        )

    def test_a_reused_output_carries_its_alert(self, tmp_path):
        folder = a_folder(tmp_path, brief_txt=MARRIAGE)
        first = Run(folder, tmp_path / "state")
        first.run()
        second = Run(folder, tmp_path / "state")
        second.run()
        alerts = by_invocation(second.rundir)
        assert alerts[first.rundir.invocation] == [("zero-claims", "new")]
        assert alerts[second.rundir.invocation] == [("zero-claims", "carried")]
        reused = [
            event["stage"]
            for event in second.rundir.events()
            if event["event"] == "reused"
            and event["invocation"] == second.rundir.invocation
        ]
        assert reused == ["transcription", "extraction", "critique"]
        assert not any(
            event["event"] == "anomaly-error"
            for event in second.rundir.events()
        )

    def test_a_paused_kind_holds_its_documents_and_others_go_on(
        self, tmp_path, monkeypatch
    ):
        timers = Timers()
        monkeypatch.setattr(anomalies, "_start_timer", timers)
        # six scans, each of other bytes: identical bytes are one artifact
        images = {f"page{n}_png": b"\x89PNG %d" % n for n in range(1, 7)}
        folder = a_folder(tmp_path, zbrief_txt=NATURALISATION, **images)
        orchestrator = Run(folder, tmp_path / "state", vision=Refusing())
        orchestrator.run()
        run = orchestrator.rundir
        failed = [
            event["subject"]
            for event in run.events()
            if event["event"] == "failed"
        ]
        media = sorted(
            (doc for doc in orchestrator.documents() if doc.kind == "media"),
            key=lambda doc: doc.source.name,
        )
        # six media documents are planned: most of them is four
        assert failed == [doc.sha1 for doc in media[:4]]
        alert = only(run, "kind-failures")
        assert (alert["value"], alert["limit"]) == (4, 3.0)
        assert [r["scope"] for r in requests(run)] == ["kind:media"]
        # the text document went through its stages
        (text,) = [d for d in orchestrator.documents() if d.kind == "text"]
        done = [
            event["stage"]
            for event in run.events()
            if event["event"] == "done" and event["subject"] == text.sha1
        ]
        assert done == ["transcription", "extraction", "critique"]
        assert len(timers.started) == 1

    def test_no_alert_changes_what_the_stages_make(
        self, tmp_path, monkeypatch
    ):
        everything = tmp_path / "everything.yaml"
        everything.write_text(
            yaml.safe_dump(
                {"checks": {"language-unexpected": {"expected": ["en"]}}}
            )
        )
        nothing = tmp_path / "nothing.yaml"
        shipped = yaml.safe_load(anomalies.DEFAULTS.read_text())
        nothing.write_text(
            yaml.safe_dump(
                {
                    "checks": {
                        name: {"enabled": False} for name in shipped["checks"]
                    }
                }
            )
        )
        views = {}
        for label, thresholds in (("all", everything), ("none", nothing)):
            monkeypatch.setenv(anomalies.CONFIG_VARIABLE, str(thresholds))
            folder = a_folder(tmp_path / label, brief_txt=MARRIAGE)
            # claims whose excerpts are tagged with another language than
            # the German transcription's, so that the batch has an alert
            run = Run(
                folder,
                tmp_path / label / "state",
                batch=a_batch("en", "en", "en", "en"),
            )
            run.run()
            (doc,) = run.documents()
            # every view and derived file, byte for byte; the state names
            # its own source's path, so its status and detail stand for it
            views[label] = {
                path.relative_to(doc.dir): path.read_bytes()
                for path in sorted(doc.dir.rglob("*"))
                if path.is_file() and path.name != "state.json"
            } | {"state": (doc.state.status, doc.state.detail)}
            views[label + " alerts"] = sorted(checks_raised(run.rundir))
        assert views["all alerts"] == [
            "language-mismatch",
            "language-unexpected",
        ]
        assert views["none alerts"] == []
        assert views["all"] == views["none"]

    def test_a_check_that_fails_never_fails_a_document(
        self, tmp_path, monkeypatch
    ):
        def broken(self, doc, stage, output):
            raise KeyError("Sebastian Löbig")

        monkeypatch.setattr(AnomalyChecks, "stored", broken)
        orchestrator = Run(
            a_folder(tmp_path, brief_txt=MARRIAGE), tmp_path / "state"
        )
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert doc.state.status == "awaiting claim review"
        errors = [
            event
            for event in orchestrator.rundir.events()
            if event["event"] == "anomaly-error"
        ]
        assert {(e["error"], e["hook"]) for e in errors} == {
            ("KeyError", "stored")
        }
        # the exception's name, never its message
        assert "Löbig" not in (
            orchestrator.state_dir / "events.jsonl"
        ).read_text(encoding="utf-8")


# -- recheck ------------------------------------------------------------------


def snapshot(directory: Path) -> dict:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def a_recorded_document(
    state_dir,
    name,
    *,
    text,
    engine,
    claims=0,
    record=True,
    document_class="letter",
    excerpt=False,
):
    """A document directory as a run leaves it: its state, and its
    transcription and batch views. A vision transcription is made from
    the record of one page holding `text`, unless `record` is False. An
    `excerpt`'s directory is named by its SHA-1, in the excerpts'
    directory."""
    directory = (
        state_dir / "excerpts" / sha1_of(name)
        if excerpt
        else state_dir / name.split(".")[0]
    )
    directory.mkdir(parents=True)
    resolution = resolve_language(text, "auto")
    (directory / "state.json").write_text(
        json.dumps(
            {
                "file": str(state_dir.parent / "docs" / name),
                "title": name,
                "status": "awaiting claim review",
                "artifact_qid": "Q23",
                "detail": {
                    "language": {
                        "code": resolution.code,
                        "source": resolution.source,
                        "confidence": resolution.confidence,
                    }
                },
                "content_sha1": sha1_of(name),
            }
        )
    )
    if record and engine.startswith("llm-vision:"):
        transcription = Transcription.from_record(
            parse_record(record_answer(text, document_class=document_class)),
            artifact=ArtifactRef(sha1=sha1_of(name)),
            language=resolution.code,
            engine=engine,
        )
    else:
        transcription = Transcription(
            artifact=ArtifactRef(sha1=sha1_of(name)),
            text=text,
            language=resolution.code,
            engine=engine,
        )
    (directory / "transcription.json").write_text(
        transcription.model_dump_json()
    )
    (directory / "batch.json").write_text(
        a_batch(*["en"] * claims).model_dump_json()
    )
    return directory


class TestRecheck:
    def test_new_thresholds_reach_stored_outputs_without_a_stage(
        self, tmp_path
    ):
        folder = a_folder(tmp_path, brief_txt=MARRIAGE)
        orchestrator = Run(folder, tmp_path / "state")
        orchestrator.run()
        state = orchestrator.state_dir
        (doc,) = orchestrator.documents()
        views = snapshot(doc.dir)
        events = len(orchestrator.rundir.events())
        alerts = recheck(
            state,
            load_config(
                overrides={
                    "checks": {
                        "zero-claims": {"min-chars": 1000},
                        "language-unexpected": {"expected": ["en"]},
                    }
                }
            ),
        )
        assert [(a["check"], a["status"]) for a in alerts] == [
            ("language-unexpected", "carried")
        ]
        (invocation,) = {alert["invocation"] for alert in alerts}
        assert invocation.startswith("recheck-")
        added = orchestrator.rundir.events()[events:]
        assert [event["event"] for event in added] == [
            "anomaly-recheck",
            "alert",
        ]
        assert added[0]["documents"] == 1
        assert snapshot(doc.dir) == views

    def test_the_transcription_check_meets_every_stored_reading(
        self, tmp_path
    ):
        state = tmp_path / "state"
        # a text file's tool failure fails
        a_recorded_document(
            state,
            "page.png",
            text="pdftoppm: command not found",
            engine="text-file:x",
        )
        # a record's text holding the same words only flags it (ADR-0037)
        a_recorded_document(
            state,
            "caption.png",
            text="pdftoppm: command not found",
            engine="llm-vision:fake-model",
        )
        # a vision transcription with no record fails
        a_recorded_document(
            state,
            "scan.png",
            text=MARRIAGE,
            engine="llm-vision:fake-model",
            record=False,
        )
        # a text file's, which has none, does not
        a_recorded_document(
            state, "brief.txt", text=MARRIAGE, engine="text-file:x"
        )
        # a curator's correction is never judged
        a_recorded_document(
            state,
            "note.png",
            text="pdftoppm: command not found",
            engine="correction",
        )
        alerts = recheck(state, load_config())
        checks = [a for a in alerts if a["check"] == "transcription-check"]
        assert sorted((a["subject"], a["failure"]) for a in checks) == sorted(
            [
                (sha1_of("page.png")[:12], "tool-failure"),
                (sha1_of("scan.png")[:12], "not-a-record"),
            ]
        )
        assert {a["status"] for a in checks} == {"carried"}

    def test_every_document_is_judged_against_the_whole_run(self, tmp_path):
        state = tmp_path / "state"
        # the outlier is recorded first, where a run judges nothing yet
        for name, claims in [("a.txt", 60)] + [
            (f"card{n}.txt", 5) for n in range(3)
        ]:
            a_recorded_document(
                state,
                name,
                text=NATURALISATION,
                engine="text-file:x",
                claims=claims,
            )
        alerts = recheck(
            state,
            load_config(
                overrides={"checks": {"claim-yield": {"min-samples": 3}}}
            ),
        )
        (alert,) = [a for a in alerts if a["check"] == "claim-yield"]
        assert alert["subject"] == sha1_of("a.txt")[:12]
        assert alert["status"] == "carried"
        assert (alert["median"], alert["samples"]) == (5.0, 3)

    def test_a_recheck_compares_within_a_document_class(self, tmp_path):
        """Three photographs and a letter, all scans: the letter is alone
        in its class, and is measured against no photograph."""
        state = tmp_path / "state"
        for name, claims, document_class in [("a.png", 60, "letter")] + [
            (f"photo{n}.png", 5, "photograph") for n in range(3)
        ]:
            a_recorded_document(
                state,
                name,
                text=NATURALISATION,
                engine="llm-vision:fake-model",
                claims=claims,
                document_class=document_class,
            )
        alerts = recheck(
            state,
            load_config(
                overrides={"checks": {"claim-yield": {"min-samples": 3}}}
            ),
        )
        assert [a for a in alerts if a["check"] == "claim-yield"] == []

    def test_a_recheck_carries_the_flag_the_check_raises_now(
        self, tmp_path, monkeypatch
    ):
        """A recheck reads a stored transcription's flag as a run's reuse
        of it would: a flag the check raises now is raised, though the
        view was made without it, and one it no longer raises is not,
        though the view stores it (ADR-0037 §2)."""
        state = tmp_path / "state"
        a_recorded_document(
            state, "gained.png", text=MARRIAGE, engine="llm-vision:fake-model"
        )
        dropped = a_recorded_document(
            state,
            "dropped.png",
            text=NATURALISATION,
            engine="llm-vision:fake-model",
        )
        view = json.loads((dropped / "transcription.json").read_text())
        (dropped / "transcription.json").write_text(
            json.dumps({**view, "flag": "media-missing"})
        )
        flagging = transcription_check._signature(
            "flagged-now",
            transcription_check.TOOL_FAILURE,
            r"Standesamt Flörburg",
        )
        monkeypatch.setattr(
            transcription_check,
            "SIGNATURES",
            (*transcription_check.SIGNATURES, flagging),
        )
        alerts = recheck(state, load_config())
        assert [
            (alert["subject"], alert["signature"])
            for alert in alerts
            if alert["check"] == "transcription-flag"
        ] == [(sha1_of("gained.png")[:12], "flagged-now")]

    def test_a_whole_pdf_s_record_gives_exactly_its_pages(self, tmp_path):
        """A record of two pages is too many for a PDF of one, read whole,
        and not for an image, which may show an open book."""
        state = tmp_path / "state"
        for name in ("extract.pdf", "book.png"):
            directory = a_recorded_document(
                state, name, text=MARRIAGE, engine="llm-vision:fake-model"
            )
            two = Transcription.from_record(
                parse_record(record_answer(MARRIAGE, MARRIAGE)),
                artifact=ArtifactRef(sha1=sha1_of(name)),
                language="de",
                engine="llm-vision:fake-model",
            )
            (directory / "transcription.json").write_text(
                two.model_dump_json()
            )
        alerts = recheck(state, load_config(), page_count=lambda path: 1)
        assert [
            (alert["subject"], alert["failure"])
            for alert in alerts
            if alert["check"] == "transcription-check"
        ] == [(sha1_of("extract.pdf")[:12], "pages-extra")]

    def test_a_document_the_checks_cannot_read_stops_no_other(self, tmp_path):
        state = tmp_path / "state"
        broken = a_recorded_document(
            state, "a.png", text=MARRIAGE, engine="llm-vision:fake-model"
        )
        (broken / "batch.json").write_text('{"claims": 5, "entities": []}')
        a_recorded_document(
            state,
            "b.png",
            text="pdftoppm: command not found",
            engine="text-file:x",
        )
        alerts = recheck(state, load_config())
        assert [
            (alert["subject"], alert["check"])
            for alert in alerts
            if alert["check"] == "transcription-check"
        ] == [(sha1_of("b.png")[:12], "transcription-check")]
        errors = [
            (event["error"], event["hook"])
            for event in RunDir(state).events()
            if event["event"] == "anomaly-error"
        ]
        assert errors == [("TypeError", "recheck")]

    def test_an_excerpt_is_rechecked_as_a_document_of_its_own(self, tmp_path):
        """The image of Sebastian's 1939 letter that the 2011 booklet
        places is an excerpt (ADR-0008): its transcription, batch and
        critique are checked again as the booklet's are, under its own
        kind."""
        state = tmp_path / "state"
        a_recorded_document(
            state,
            "family-booklet-2011.pdf",
            text=NATURALISATION,
            engine="llm-vision:fake-model",
            claims=1,
        )
        excerpt = a_recorded_document(
            state,
            "letter-excerpt-1939.png",
            text=MARRIAGE,
            engine="llm-vision:fake-model",
            excerpt=True,
        )
        (excerpt / "batch.json").write_text(
            a_dated_batch(
                URSULA_BORN,
                a_dated_claim("ursula", "date_of_death", a_time(1801)),
            ).model_dump_json()
        )
        (excerpt / "critique.json").write_text("[]")
        events = len(RunDir(state).events())
        alerts = recheck(
            state,
            load_config(
                overrides={
                    "checks": {
                        "language-unexpected": {"expected": ["en"]},
                    }
                }
            ),
        )
        subject = sha1_of("letter-excerpt-1939.png")[:12]
        assert sorted(
            (a["check"], a["stage"], a["kind"])
            for a in alerts
            if a["subject"] == subject
        ) == [
            ("implausible-date", "extraction", "excerpt"),
            ("language-unexpected", "transcription", "excerpt"),
        ]
        (opened,) = [
            event
            for event in RunDir(state).events()[events:]
            if event["event"] == "anomaly-recheck"
        ]
        assert opened["documents"] == 2

    def test_a_directory_set_aside_is_not_read(self, tmp_path):
        """A dot-prefixed directory, at the top or among the excerpts, is
        set aside, and is neither checked nor counted, though it holds a
        state.json as valid as any."""
        state = tmp_path / "state"
        a_recorded_document(
            state, "brief.txt", text=MARRIAGE, engine="text-file:x"
        )
        aside = a_recorded_document(
            state, "card.txt", text=MARRIAGE, engine="text-file:x"
        )
        aside.rename(aside.with_name(".card"))
        excerpt = a_recorded_document(
            state,
            "letter-excerpt-1939.png",
            text=MARRIAGE,
            engine="text-file:x",
            excerpt=True,
        )
        excerpt.rename(excerpt.with_name(f".{excerpt.name}"))
        events = len(RunDir(state).events())
        alerts = recheck(
            state,
            load_config(
                overrides={
                    "checks": {"language-unexpected": {"expected": ["en"]}}
                }
            ),
        )
        assert {a["subject"] for a in alerts} == {sha1_of("brief.txt")[:12]}
        (opened,) = [
            event
            for event in RunDir(state).events()[events:]
            if event["event"] == "anomaly-recheck"
        ]
        assert opened["documents"] == 1

    def test_excerpts_meet_the_distributions(self, tmp_path):
        """Four excerpts of one class: the one with twelve times the
        claims, and the one whose critique finds twenty times as much,
        are each outliers among the four."""
        state = tmp_path / "state"
        for n, claims in enumerate([60, 5, 5, 5]):
            directory = a_recorded_document(
                state,
                f"excerpt{n}.png",
                text=NATURALISATION,
                engine="llm-vision:fake-model",
                claims=claims,
                document_class="photograph",
                excerpt=True,
            )
            findings = 20 if n == 1 else 1
            (directory / "critique.json").write_text(
                json.dumps([{"note": "x"}] * findings)
            )
        alerts = recheck(
            state,
            load_config(
                overrides={
                    "checks": {
                        "claim-yield": {"min-samples": 3},
                        "critic-rate": {"min-samples": 3},
                    }
                }
            ),
        )
        assert sorted(
            (a["check"], a["subject"], a["kind"])
            for a in alerts
            if a["check"] in ("claim-yield", "critic-rate")
        ) == sorted(
            [
                ("claim-yield", sha1_of("excerpt0.png")[:12], "excerpt"),
                ("critic-rate", sha1_of("excerpt1.png")[:12], "excerpt"),
            ]
        )

    def test_the_module_s_command(self, tmp_path, capsys):
        folder = a_folder(tmp_path, brief_txt=MARRIAGE)
        orchestrator = Run(folder, tmp_path / "state")
        orchestrator.run()
        thresholds = tmp_path / "thresholds.yaml"
        thresholds.write_text(
            "checks:\n  language-unexpected:\n    expected: [en]\n"
        )
        state = str(orchestrator.state_dir)
        command = ["recheck", state, "--config", str(thresholds)]
        assert anomalies.main(command) == 0
        out = capsys.readouterr().out
        assert out.startswith("2 alerts appended to ")
        assert re.search(r"language-unexpected\s+1", out)
        assert re.search(r"zero-claims\s+1", out)
        # the transcription's alert and the batch's, both carried
        assert {
            alert["status"]
            for alert in orchestrator.rundir.alerts()
            if alert["invocation"].startswith("recheck-")
        } == {"carried"}
        assert anomalies.main(["recheck", str(tmp_path)]) == 2
        thresholds.write_text("checks:\n  no-such-check: {}\n")
        assert anomalies.main(command) == 2


# -- the unit tests' own default ----------------------------------------------


class TestTheUnitTestDefaults:
    """tests/unit/conftest.py records the checks' pause timers in every
    unit test, and leaves an operator's thresholds out."""

    def test_a_pause_timer_is_recorded_not_started(
        self, tmp_path, _no_anomaly_timers
    ):
        run = RunDir(tmp_path / "run")
        checks = AnomalyChecks(run, load_config())
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        (timer,) = _no_anomaly_timers
        assert timer.seconds > 1800
        assert not any(
            isinstance(thread, threading.Timer)
            for thread in threading.enumerate()
        )

    def test_an_operator_s_thresholds_are_left_out(self):
        """Run with `$YIZKR_ANOMALIES` set, this shows the default at
        work; the mutation evidence runs it so."""
        assert anomalies.CONFIG_VARIABLE not in os.environ


# -- helpers ------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        "values",
        [[1.0, 2.0], [3.0, 1.0, 2.0], [5.0, 5.0, 1.0, 9.0], [2.0] * 4],
    )
    def test_the_median_without_one_value(self, values):
        ordered = sorted(values)
        for index in range(len(ordered)):
            rest = ordered[:index] + ordered[index + 1 :]
            assert anomalies._median_without(ordered, index) == (
                statistics.median(rest)
            )

    def test_the_pages_a_yield_is_measured_per(self):
        slides = (
            "[Slide 1]\nFlörburg\n[Slide 2]\n[Slide 2 notes]\nx\n[Slide 3]"
        )
        assert document_pages("presentation", slides, None, 2000) == 3
        assert document_pages("media", "EUGEN LÖBIG", 4, 2000) == 4
        assert document_pages("text", "x" * 5000, None, 2000) == 2.5
        assert document_pages("text", "x", None, 2000) == 1

    def test_the_event_tail_waits_for_a_line_to_end(self, tmp_path):
        log = tmp_path / "events.jsonl"
        tail = anomalies._Tail(log, b'"i1"')
        log.write_bytes(b'{"invocation": "i1", "n": 1}\n{"invocation": "i1"')
        assert tail.read() == [{"invocation": "i1", "n": 1}]
        with log.open("ab") as handle:
            handle.write(b', "n": 2}\n{"invocation": "i2", "n": 3}\n')
        assert tail.read() == [{"invocation": "i1", "n": 2}]
        log.write_bytes(b'{"invocation": "i1", "n": 4}\n')
        assert tail.read() == [{"invocation": "i1", "n": 4}]

    def test_the_event_tail_skips_a_deeply_nested_line(self, tmp_path):
        """A line nested past Python's recursion limit makes `json.loads`
        raise RecursionError, not ValueError: the tail skips it like any
        other line that holds the marker but cannot be decoded, and the
        entries around it are read (#285). No writer here produces such
        a line."""
        log = tmp_path / "events.jsonl"
        tail = anomalies._Tail(log, b'"i1"')
        log.write_bytes(
            b'{"invocation": "i1", "n": 1}\n'
            + b"[" * 100_000
            + b'"i1"\n'
            + b'{"invocation": "i1", "n": 2}\n'
        )
        assert tail.read() == [
            {"invocation": "i1", "n": 1},
            {"invocation": "i1", "n": 2},
        ]

    def test_requests_skips_a_deeply_nested_line(self):
        """A line nested past Python's recursion limit makes `json.loads`
        raise RecursionError, not ValueError: `_requests` skips it like
        any other line it cannot decode, and the requests around it are
        read (#285). No writer here produces such a line."""
        data = (
            b'{"action": "pause", "scope": "run"}\n'
            + b"[" * 100_000
            + b'\n{"action": "resume", "scope": "run"}\n'
        )
        assert anomalies._requests(data) == [
            {"action": "pause", "scope": "run"},
            {"action": "resume", "scope": "run"},
        ]


def test_no_test_here_waits_on_or_reads_the_real_clock():
    """No test in this file sleeps, reads a real clock, waits a real time
    or starts a real timer with a wait (#432): the checks' clocks are the
    fake Clock, their timers the fake Timers or the real _start_timer
    given no wait, and a wait for a thread or an event is bounded only by
    HANG_GUARD."""
    source = Path(__file__).read_text(encoding="utf-8")
    uses = real_time_uses(
        source,
        # its timer is made, never started: threading.Timer is patched
        exempt={"test_a_timer_waits_at_most_the_longest_wait_a_thread_can"},
        hang_guards={"HANG_GUARD"},
        real_timers={"START_TIMER", "_start_timer"},
    )
    assert uses == []
