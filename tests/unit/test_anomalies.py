"""Offline tests for the anomaly checks (#196, ADR-0025 §3).

Every check meets fictional outputs from the example universe
(docs/EXAMPLES.md): what makes it fire, and what keeps it quiet. The
checks read a run's events as the orchestrator, the LLM cache and the
engine's call reports write them, so the tests write those events through
the same RunDir. Then the thresholds, new and carried alerts, the
fail-fast pauses and their timeout into an abort, the orchestrator's
hooks (a carried alert on a reused output, a paused kind while another
goes on, outputs no alert alters), and a recheck of stored outputs under
new thresholds, which recomputes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.fixtures import example_universe as u
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import anomalies, rundir
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
    check_transcription,
)

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


@pytest.fixture(autouse=True)
def no_operator_thresholds(monkeypatch):
    """The tests' thresholds are the defaults and what each test sets,
    never an operator's file."""
    monkeypatch.delenv(anomalies.CONFIG_VARIABLE, raising=False)


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
    """The checks' clock, moved on by hand; the `clock` fixture makes it
    the run directory's too, which stamps the control file's requests."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(
        rundir, "_now", lambda: clock.at.isoformat(timespec="seconds")
    )
    return clock


class Timers:
    """The timers the checks start, recorded instead of started: `fire`
    runs each timer not run yet, as a real one runs once when its time
    has come."""

    def __init__(self) -> None:
        self.started: list[tuple[float, object]] = []
        self._pending: list = []

    def __call__(self, seconds, callback):
        self.started.append((seconds, callback))
        self._pending.append(callback)

    def fire(self) -> None:
        due, self._pending = self._pending, []
        for callback in due:
            callback()


def make_checks(tmp_path, overrides=None, pages=None, clock=None, timers=None):
    run = RunDir(tmp_path / "run")
    checks = AnomalyChecks(
        run,
        load_config(overrides=overrides or {}),
        now=clock or Clock(),
        timer=timers if timers is not None else Timers(),
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
) -> None:
    """The events of one stage attempt, as the stage runner, the LLM cache
    and the engine's call reports write them: its start, the cache's hits
    and misses, a call event per outcome in `calls`, the cache's
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
    for reason in rejects:
        run.emit("cache-reject", key="k", reason=reason, **about)
    if failed is None:
        run.emit("done", **where)
    else:
        run.emit("failed", reason=failed[:500], **where)


def vision(text: str, language: str = "de") -> Transcription:
    return Transcription(
        text=text, language=language, engine="llm-vision:fake-model"
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
        assert {"pause-timeout-s", "chars-per-page"} <= documented

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
            "pause-timeout-s: -1\n",
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
        reason = verdict_reason("I'm unable to assist with this request.")
        attempt(run, doc, "transcription", calls=["ok"], failed=reason)
        checks.failed(doc, "transcription")
        alert = only(run, "transcription-check")
        assert alert["stage"] == "transcription"
        assert alert["subject"] == doc.sha1[: anomalies.SUBJECT_PREFIX]
        assert (alert["value"], alert["status"]) == (True, "new")
        assert (alert["failure"], alert["signature"]) == (
            "refusal",
            "refusal-request",
        )
        assert alert["kind"] == "media"

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
            ("I'm unable to assist with this request.", 1, "refusal"),
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
        reason = verdict_reason("I'm unable to assist with this request.")
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
            Transcription(text=text, language="de", engine=engine),
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
            extractor.extract(transcription(), DOC_REF, artifact_qid="Q23")
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

    def test_chunk_answers_cut_off_above_the_share(self, tmp_path):
        with pytest.raises(ChunkIncomplete) as cut:
            parse_chunk_response(json.dumps(a_claim()))
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
            [{"target": "e1", "issue": "doubtful"}] * findings,
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
        assert checks._samples[("claim-yield", "text")] == [5.0] * 3

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
        assert alert["pause"] == "stage:transcription"
        (pause,) = requests(run)
        assert pause["scope"] == "stage:transcription"

    def test_half_the_recent_calls_failing_is_not_most(self, tmp_path):
        run, checks = make_checks(tmp_path)
        doc = Doc(tmp_path, "letter.png", "media")
        about = dict(subject=doc.sha1, kind="media", role="transcribe")
        for outcome in ["ok", "error"] * 10:
            run.emit("call", stage="transcription", outcome=outcome, **about)
        checks.poll()
        assert "call-failures" not in checks_raised(run)


# -- fail-fast pauses ---------------------------------------------------------


def checks_of(run: RunDir, clock, timers=None) -> AnomalyChecks:
    """The checks of `run`'s invocation, on the test's clock."""
    return AnomalyChecks(
        run,
        load_config(),
        now=clock,
        timer=timers if timers is not None else Timers(),
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


class TestFailFastPauses:
    def test_a_pause_nobody_resumes_times_out_into_an_abort(
        self, tmp_path, clock
    ):
        timers = Timers()
        run, _checks = paused_media(tmp_path, clock=clock, timers=timers)
        ((seconds, _callback),) = timers.started
        assert seconds > 1800
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

    def test_the_timer_runs_on_a_thread_of_its_own(self, tmp_path):
        """With the real timer and clock: an abort that no later check
        makes. The pause stands when the check that asked for it ends, and
        the timer's thread, of a daemon, aborts it in its own time."""
        started = []

        def start(seconds, callback):
            started.append(anomalies._start_timer(seconds, callback))

        run = RunDir(tmp_path / "run")
        checks = AnomalyChecks(
            run, load_config(overrides={"pause-timeout-s": 2}), timer=start
        )
        outcomes(run, "transcription", "media", "xxxxxx")
        checks.poll()
        assert run.control().stopped(kind="media") == "kind:media paused"
        (timer,) = started
        assert timer.daemon
        timer.join(timeout=30)
        assert not timer.is_alive()
        assert run.control().stopped(kind="media") == "kind:media aborted"

    def test_a_pause_a_driver_carries_on_times_out_from_the_carry(
        self, tmp_path, clock
    ):
        """A driver that invokes the run again after a subscription limit
        asks again for the pauses still standing, with who asked for them,
        as the scratch driver (#194) does. The checks that paused the
        slice keep a timer on it: the carried pause times out from its own
        request, with no check made in the new invocation."""
        timers = Timers()
        paused_media(tmp_path, clock=clock, timers=timers)
        clock.advance(600)
        run = RunDir(tmp_path / "run")
        run.start()
        run.request(
            "pause",
            kind="media",
            by="anomaly",
            reason="kind-failures: transcription failed 6 of the first 6 "
            "media documents",
            carried=True,
        )
        clock.advance(1201)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media paused"
        clock.advance(600)
        timers.fire()
        assert run.control().stopped(kind="media") == "kind:media aborted"
        abort = requests(run)[-1]
        assert (abort["action"], abort["by"], abort["check"]) == (
            "abort",
            "anomaly",
            "kind-failures",
        )


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
            Finding(target="e1", issue="an organisation typed as a place")
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


def a_recorded_document(state_dir, name, *, text, engine, claims=0):
    """A document directory as a run leaves it: its state, and its
    transcription and batch views."""
    directory = state_dir / name.split(".")[0]
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
    (directory / "transcription.json").write_text(
        Transcription(
            text=text, language=resolution.code, engine=engine
        ).model_dump_json()
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
        a_recorded_document(
            state,
            "page.png",
            text="I'm unable to assist with this request.",
            engine="llm-vision:fake-model",
        )
        # a curator's correction is never judged
        a_recorded_document(
            state,
            "note.png",
            text="I'm unable to assist with this request.",
            engine="correction",
        )
        alerts = recheck(state, load_config())
        checks = [a for a in alerts if a["check"] == "transcription-check"]
        assert [(a["subject"], a["failure"]) for a in checks] == [
            (sha1_of("page.png")[:12], "refusal")
        ]
        assert checks[0]["status"] == "carried"

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
        assert (alert["median"], alert["samples"]) == (5.0, 3)

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
        assert anomalies.main(["recheck", str(tmp_path)]) == 2
        thresholds.write_text("checks:\n  no-such-check: {}\n")
        assert anomalies.main(command) == 2


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
