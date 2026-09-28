"""Anomaly checks: cheap signals over a run's stage outputs (ADR-0025 §3).

Checks that call no model watch a run as it unfolds, for the operator and
the run report. They run on every stage output a run stores, on every
stored output a run reuses when it dispatches a document, and on the
run's own events. Each finding is an alert in the run directory
(rundir.RunDir.alert: a line of `alerts.jsonl` and an `alert` event), so
the live monitor and the run report show it. An alert names the stage,
the check, the document by the first SUBJECT_PREFIX hex digits of its
SHA-1, the measured value and the threshold it crossed, and whether it is
new or carried:

* an output the run stores is `new` when the engine answered at least one
  call of the stage that made it, or the stage makes none (a text file's
  transcription, the GEDCOM adapter), and `carried` when the LLM cache
  answered every call the stage made, by key match or adoption;
* a stored output the run reuses, its view current, is `carried`.

So an alert is new exactly where the live monitor counts its stage done,
and carried where it counts it reused. An alert on the run's dispatches
and calls (a rate check) is new unless every failure it counts came from
cached answers.

The checks, which docs/ANOMALIES.md describes one by one:

* transcription: the transcription check's verdicts, and a vision
  transcription sparse for its pages;
* language: a detected language outside the expected ones, detected with
  low confidence, or detected confidently on very short text; and a
  batch whose excerpts are mostly tagged with another language than its
  transcription's;
* extraction: no entities or no claims from a non-trivial transcription,
  a fallback manifest or segmentation, and per document the shares of
  its chunk calls that failed, of its chunk answers cut off and of the
  claims it was given that were rejected or unread;
* distributions: entities and claims per page, and critic findings per
  item reviewed, far from the median of the run's documents of the kind;
* rates: a stage's failures over a rolling window of its dispatches, its
  failures among the first documents of a kind, and the failures among
  its recent engine calls.

They reuse the pipeline's own signals rather than judge again: the
transcription check (yizkr.pipeline.transcription_check) and the reasons
its failures give in the run's events, the extraction's health in its
coverage side output and the reasons a failed extraction gives, the
language resolution the run records in a document's state, and the
stage, cache and call events of the run directory.

The thresholds live in one file, `anomalies.yaml` beside this module,
with an operator's overrides in the file `$YIZKR_ANOMALIES` names
(load_config). They decide only which alerts are raised and which slices
are paused, never what a stage makes or what a cache key holds. Every
dispatch checks the outputs it reuses with the thresholds as they are
then, and `recheck` checks a run directory's stored outputs at once
without dispatching anything, so a change of thresholds reaches every
stored output without recomputing a stage.

A rate check marked fail-fast asks for a pause of its slice when it
fires: the stage, or the document kind (RunDir.request, with `by` BY),
and the other slices go on. When a pause of the checks alone stands on
the slice and the configured timeout has passed since they asked for
it, the checks append an abort of it, so an unattended run never waits
on it indefinitely. A slice they paused stays watched for the rest of the
process, by a timer that looks even while no stage runs and by every
later check: a driver that invokes the run again may ask again for a
pause of theirs as it stood, and the timeout of such a pause still runs
from the checks' own request (_SliceWatch). A pause of the same slice
that someone else asked for is theirs, and is never aborted here. An
abort, like a pause, only stops dispatch: nothing is deleted, and every
completed output stays.

Alerts are signals. The checks never change, move or drop an output or a
claim (ADR-0002), and a check that fails is an `anomaly-error` event and
a logged warning, never a document's failure.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import fcntl
import json
import logging
import os
import re
import statistics
import sys
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from yizkr.pipeline.rundir import (
    ABORT,
    ALERTS,
    CONTROL,
    EVENTS,
    MANIFEST,
    PAUSE,
    RESUME,
    SET_ASIDE,
    RunDir,
    fold_control,
)
from yizkr.pipeline.stages import canonical_json, read_jsonl, sha256_text
from yizkr.pipeline.transcription_check import (
    check_transcription,
    media_page_count,
    text_char_count,
)

logger = logging.getLogger(__name__)

# the thresholds shipped with the package, and the variable that names an
# operator's file of overrides
DEFAULTS = files("yizkr.pipeline") / "anomalies.yaml"
CONFIG_VARIABLE = "YIZKR_ANOMALIES"

# how many hex digits of a document's SHA-1 an alert names it by
SUBJECT_PREFIX = 12
# who a control request of the checks says it came from
BY = "anomaly"
# the event that opens a recheck's alerts, and the one a failing check
# leaves
RECHECK_EVENT = "anomaly-recheck"
ERROR_EVENT = "anomaly-error"

# the stages the checks look at (yizkr.pipeline.orchestrate)
TRANSCRIPTION = "transcription"
EXTRACTION = "extraction"
CRITIQUE = "critique"
GEDCOM = "gedcom"

# the routes whose transcriptions the transcription check judges, by the
# first part of a transcription's `engine`: an engine's reading and a text
# file. A curator's correction and a deck's slide text are never judged.
CHECKED_ROUTES = ("llm-vision", "text-file")
VISION = "llm-vision"

# the measures each stage's output gives the distribution checks
_MEASURES = {
    EXTRACTION: ("entity-yield", "claim-yield"),
    CRITIQUE: ("critic-rate",),
}


# -- the thresholds -----------------------------------------------------------


class AnomalyConfigError(ValueError):
    """A file of thresholds that cannot be read, or that sets a key the
    defaults do not have, or a value out of range."""


def _dashed(name: str) -> str:
    return name.replace("_", "-")


class _Settings(BaseModel):
    """Keys are written with dashes, as in anomalies.yaml; any other key
    is refused."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=_dashed
    )


class Check(_Settings):
    enabled: bool


class Sparse(Check):
    min_pages: int = Field(ge=1)
    min_chars_per_page: float = Field(ge=0)


class Expected(Check):
    expected: list[str]


class Confidence(Check):
    min_confidence: float = Field(ge=0, le=1)


class ShortText(Check):
    max_chars: int = Field(ge=0)
    min_confidence: float = Field(ge=0, le=1)


class Share(Check):
    max_share: float = Field(ge=0, le=1)
    min_count: int = Field(ge=1)


class NonTrivial(Check):
    min_chars: int = Field(ge=0)


class Outlier(Check):
    factor: float = Field(gt=1)
    smoothing: float = Field(gt=0)
    min_samples: int = Field(ge=1)


class Rolling(Check):
    window: int = Field(ge=1)
    min_count: int = Field(ge=1)
    max_share: float = Field(ge=0, lt=1)
    fail_fast: bool

    @model_validator(mode="after")
    def _counted_in_the_window(self) -> Rolling:
        if self.min_count > self.window:
            raise ValueError(
                f"min-count {self.min_count} is more than the window of "
                f"{self.window} it is counted in"
            )
        return self


class FirstOfKind(Check):
    first: int = Field(ge=1)
    max_share: float = Field(ge=0, lt=1)
    fail_fast: bool


class Checks(_Settings):
    transcription_check: Check
    transcription_sparse: Sparse
    language_unexpected: Expected
    language_low_confidence: Confidence
    language_short_text: ShortText
    language_mismatch: Share
    zero_entities: NonTrivial
    zero_claims: NonTrivial
    fallback_manifest: Check
    fallback_segmentation: Check
    chunk_failures: Share
    truncations: Share
    schema_rejects: Share
    entity_yield: Outlier
    claim_yield: Outlier
    critic_rate: Outlier
    stage_failures: Rolling
    kind_failures: FirstOfKind
    call_failures: Rolling

    def outlier(self, name: str) -> Outlier:
        """The settings of a distribution check, by its name."""
        return getattr(self, name.replace("-", "_"))


class AnomalyConfig(_Settings):
    """The thresholds of every check (anomalies.yaml)."""

    version: Literal[1]
    # a timer waits at most threading.TIMEOUT_MAX; infinity would stop
    # the timer's thread, and null holds a pause instead
    pause_timeout_s: (
        Annotated[
            float,
            Field(ge=0, le=threading.TIMEOUT_MAX, allow_inf_nan=False),
        ]
        | None
    )
    chars_per_page: int = Field(ge=1)
    checks: Checks

    def digest(self) -> str:
        """The first 12 hex digits of the SHA-256 of the thresholds: every
        alert carries it, so that alerts raised under other thresholds
        tell apart."""
        return sha256_text(
            canonical_json(self.model_dump(mode="json", by_alias=True))
        )[:12]


def _merged(base: Any, override: Any) -> Any:
    """`override` over `base`: mappings merged key by key, anything else
    (a list included) replaced whole."""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merged(base[key], value) if key in base else value
        return merged
    return override


def _validated(data: Any, source: Any = None) -> AnomalyConfig:
    try:
        return AnomalyConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors()
        )
        raise AnomalyConfigError(
            f"anomaly thresholds{f' in {source}' if source else ''}: "
            f"{problems}"
        ) from exc


def default_config() -> AnomalyConfig:
    """The thresholds of anomalies.yaml alone."""
    return _validated(yaml.safe_load(DEFAULTS.read_text(encoding="utf-8")))


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> AnomalyConfig:
    """The thresholds: anomalies.yaml, with the file at `path` over it
    (by default the file `$YIZKR_ANOMALIES` names, when it names one), and
    `overrides` over both, keys written as in anomalies.yaml. Raises
    AnomalyConfigError for a file that cannot be read, a key the defaults
    do not have, or a value out of range."""
    data = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))
    source = path if path is not None else os.environ.get(CONFIG_VARIABLE)
    if source:
        try:
            loaded = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AnomalyConfigError(
                f"the anomaly thresholds in {source} cannot be read "
                f"({type(exc).__name__})"
            ) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            raise AnomalyConfigError(
                f"{source} holds no mapping of anomaly thresholds"
            )
        data = _merged(data, loaded)
    if overrides:
        data = _merged(data, overrides)
    return _validated(data, source)


# -- findings on one output ---------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One check's finding, before it is an alert: the check's name, the
    measured value, the threshold it crossed, and the alert's other
    fields."""

    check: str
    value: Any
    limit: Any = None
    fields: Mapping[str, Any] = field(default_factory=dict)


def _get(value: Any, name: str) -> Any:
    """A field of an output, whether it is a model or its JSON."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _count(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _primary(code: Any) -> str | None:
    """A language code's primary subtag, lowercased: `de` for `de-AT`."""
    if not isinstance(code, str) or not code.strip():
        return None
    return code.strip().split("-", 1)[0].lower()


def route(engine: Any) -> str | None:
    """What made a transcription, from its `engine`: the part before a
    colon (`llm-vision`, `text-file`, `pptx-adapter`, `correction`)."""
    return engine.split(":", 1)[0] if isinstance(engine, str) else None


def share(name: str, settings: Share, part: int, whole: int) -> Finding | None:
    """A finding when more than `max-share` of `whole` is `part`, over at
    least `min-count`."""
    if not settings.enabled or whole < max(1, settings.min_count):
        return None
    measured = part / whole
    if measured <= settings.max_share:
        return None
    return Finding(
        name,
        round(measured, 3),
        settings.max_share,
        {"count": part, "of": whole},
    )


def language_findings(
    config: AnomalyConfig, resolution: Any, letters: int
) -> list[Finding]:
    """Findings on how a document's language was resolved, as the run
    records it (yizkr.pipeline.language.LanguageResolution: its `code`,
    its `source`, detected, fallback or override, and its `confidence`),
    for a transcription of `letters` letters and digits. A language the
    run was told (override) or fell back to for want of a guess is the
    operator's, and is never judged."""
    if not isinstance(resolution, Mapping):
        return []
    checks = config.checks
    code = resolution.get("code")
    source = resolution.get("source")
    confidence = _number(resolution.get("confidence"))
    found = []
    unexpected = checks.language_unexpected
    expected = {_primary(listed) for listed in unexpected.expected} - {None}
    if (
        unexpected.enabled
        and expected
        and source == "detected"
        and _primary(code) not in expected
    ):
        found.append(
            Finding(
                "language-unexpected",
                code,
                sorted(unexpected.expected),
                {"confidence": confidence},
            )
        )
    low = checks.language_low_confidence
    if (
        low.enabled
        and confidence is not None
        and (
            source == "fallback"
            or (source == "detected" and confidence < low.min_confidence)
        )
    ):
        found.append(
            Finding(
                "language-low-confidence",
                round(confidence, 3),
                low.min_confidence,
                {"resolved": source},
            )
        )
    short = checks.language_short_text
    if (
        short.enabled
        and source == "detected"
        and confidence is not None
        and confidence >= short.min_confidence
        and letters < short.max_chars
    ):
        found.append(
            Finding(
                "language-short-text",
                letters,
                short.max_chars,
                {"confidence": round(confidence, 3)},
            )
        )
    return found


def transcription_findings(
    config: AnomalyConfig,
    *,
    text: str,
    engine: Any,
    page_count: int | None,
    resolution: Any,
) -> list[Finding]:
    """Findings on one transcription: a vision transcription sparse for
    its `page_count` pages, and how its language was resolved."""
    letters = text_char_count(text)
    found = []
    sparse = config.checks.transcription_sparse
    if (
        sparse.enabled
        and route(engine) == VISION
        and page_count is not None
        and page_count >= sparse.min_pages
    ):
        per_page = letters / page_count
        if per_page < sparse.min_chars_per_page:
            found.append(
                Finding(
                    "transcription-sparse",
                    round(per_page, 1),
                    sparse.min_chars_per_page,
                    {"pages": page_count},
                )
            )
    return found + language_findings(config, resolution, letters)


def verdict(failure: Any, signature: Any = None) -> Finding:
    """The transcription check's failing verdict as a finding: the kind of
    failure, and the signature that matched, if one did."""
    fields: dict[str, Any] = {"failure": failure}
    if signature:
        fields["signature"] = signature
    return Finding("transcription-check", True, None, fields)


def check_verdict(
    config: AnomalyConfig, *, text: str, engine: Any, page_count: int | None
) -> Finding | None:
    """The transcription check's verdict on a stored transcription, when
    it fails: only for the routes it judges (CHECKED_ROUTES)."""
    if (
        not config.checks.transcription_check.enabled
        or route(engine) not in CHECKED_ROUTES
    ):
        return None
    check = check_transcription(text, pages=page_count)
    if check.passed:
        return None
    return verdict(check.failure, check.signature)


# How the transcription check's failure reads (transcription_check.
# TranscriptionCheckFailed): in a stage's `failed` event, after the
# exception's name, and in the `set-aside` event of a reused view it fails
# again. Only the kind of failure and the signature's name are read; the
# rest can quote the output.
_CHECK_FAILED = re.compile(
    r"(?:TranscriptionCheckFailed: )?transcription check v\d+ failed "
    r"\((?P<failure>[a-z-]+)\)"
)
_SIGNATURE = re.compile(r"\(signature (?P<signature>[a-z0-9-]+):")


def check_failure(reason: Any) -> Finding | None:
    """The transcription check's verdict in a failure's reason, or None
    when the reason is not the check's."""
    if not isinstance(reason, str):
        return None
    failed = _CHECK_FAILED.match(reason)
    if failed is None:
        return None
    signature = _SIGNATURE.search(reason, failed.end())
    return verdict(
        failed.group("failure"),
        signature.group("signature") if signature else None,
    )


# What a failed extraction's reason says of the claims it could not use
# (chunking.ExtractionFailed): validation rejected every claim, or the
# chunk answers held objects the parser could not read.
_FAILED_EXTRACTION = "ExtractionFailed: "
_EVERY_CLAIM_REJECTED = re.compile(
    r"validation rejected every claim \((\d+)\)"
)
_UNREAD_OBJECTS = re.compile(r"(\d+) unreadable objects? in the chunk answers")


def rejects_on_failure(config: AnomalyConfig, reason: Any) -> Finding | None:
    """The schema-rejects finding on an extraction that failed because
    none of its claims could be used: every one it was given was rejected
    or unread."""
    if not isinstance(reason, str) or not reason.startswith(
        _FAILED_EXTRACTION
    ):
        return None
    unusable = sum(map(int, _EVERY_CLAIM_REJECTED.findall(reason))) + sum(
        map(int, _UNREAD_OBJECTS.findall(reason))
    )
    return share(
        "schema-rejects", config.checks.schema_rejects, unusable, unusable
    )


def batch_findings(
    config: AnomalyConfig,
    *,
    entities: int,
    claims: int,
    excerpts: Sequence[Any],
    language: Any,
    letters: int | None,
    coverage: Any,
) -> list[Finding]:
    """Findings on one extraction's batch: no entities or no claims from a
    transcription of `letters` letters and digits, a fallback tier, the
    share of the claims it was given that were rejected or unread (from
    its coverage side output), and the share of its excerpts tagged with
    another language than its transcription's `language`."""
    checks = config.checks
    found: list[Finding] = []
    if letters is not None:
        for name, settings, count in (
            ("zero-entities", checks.zero_entities, entities),
            ("zero-claims", checks.zero_claims, claims),
        ):
            if (
                settings.enabled
                and not count
                and letters >= settings.min_chars
            ):
                found.append(Finding(name, letters, settings.min_chars))
    if isinstance(coverage, Mapping):
        fallback = coverage.get("fallback")
        if not isinstance(fallback, Mapping):
            fallback = {}
        if checks.fallback_manifest.enabled and "manifest" in fallback:
            found.append(Finding("fallback-manifest", True))
        if checks.fallback_segmentation.enabled and "segmentation" in fallback:
            found.append(Finding("fallback-segmentation", True))
        unusable = _count(coverage.get("rejected_claims")) + _count(
            coverage.get("unread_objects")
        )
        rejected = share(
            "schema-rejects",
            checks.schema_rejects,
            unusable,
            _count(coverage.get("claims")) + unusable,
        )
        if rejected is not None:
            found.append(rejected)
    target = _primary(language)
    tagged = [code for code in map(_primary, excerpts) if code]
    if target is not None:
        mismatch = share(
            "language-mismatch",
            checks.language_mismatch,
            sum(code != target for code in tagged),
            len(tagged),
        )
        if mismatch is not None:
            found.append(
                Finding(
                    mismatch.check,
                    mismatch.value,
                    mismatch.limit,
                    {**mismatch.fields, "language": language},
                )
            )
    return found


def outlier(
    name: str, settings: Outlier, value: float, others: Sequence[float]
) -> Finding | None:
    """A finding when `value` is more than `factor` times the median of
    `others`, or less than the median divided by it, with `smoothing`
    added to both; None with fewer than `min-samples` others."""
    if not settings.enabled or len(others) < settings.min_samples:
        return None
    return _against(
        name, settings, value, statistics.median(others), len(others)
    )


def _against(
    name: str, settings: Outlier, value: float, median: float, samples: int
) -> Finding | None:
    ratio = (value + settings.smoothing) / (median + settings.smoothing)
    if 1 / settings.factor <= ratio <= settings.factor:
        return None
    return Finding(
        name,
        round(value, 3),
        settings.factor,
        {"median": round(median, 3), "samples": samples},
    )


_SLIDE_MARK = re.compile(r"^\[Slide \d+\]$", re.MULTILINE)


def document_pages(
    kind: str | None, text: str, page_count: int | None, chars_per_page: int
) -> float:
    """The pages a document's yields are measured per: its medium's own
    pages (a PDF's, an image's one), a deck's slides (the `[Slide N]`
    marks of its slide text), and otherwise its transcription's length in
    pages of `chars_per_page` characters; at least one."""
    if kind == "presentation":
        slides = len(_SLIDE_MARK.findall(text))
        if slides:
            return float(slides)
    elif page_count:
        return float(page_count)
    return max(1.0, len(text) / chars_per_page)


# -- reading the run's events -------------------------------------------------


class _Tail:
    """The complete lines appended to a log since the last read, as
    objects: a line its writer has not ended yet waits for the next read,
    and a line that is not a JSON object is skipped. Only lines holding
    `marker` are parsed, so reading the log from its start costs little.
    A log that shrinks is read again from its start."""

    def __init__(self, path: Path, marker: bytes) -> None:
        self.path = path
        self.marker = marker
        self.offset = 0

    def read(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        entries = []
        with contextlib.suppress(OSError), open(self.path, "rb") as handle:
            handle.seek(self.offset)
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                self.offset += len(line)
                if self.marker not in line:
                    continue
                try:
                    entry = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
        return entries


@dataclass
class _Attempt:
    """One stage of one document in the invocation, from its `started`
    event to its end: what the engine and the LLM cache answered it (the
    cache's misses and replays, which reach the engine, and its hits),
    and for an extraction its chunk calls and answers (every call but a
    conversation's opening turn)."""

    engine: int = 0
    hits: int = 0
    chunk_calls: int = 0
    chunk_errors: int = 0
    chunk_answers: int = 0
    truncations: int = 0

    @property
    def carried(self) -> bool:
        """Whether the cache answered every call the stage made: at least
        one, and none reached the engine. Without the cache there are no
        hits, and every output is the engine's or the stage's own."""
        return self.hits > 0 and not self.engine


@dataclass
class _Facts:
    """What the checks know of one document, from its outputs as they
    are stored or reused, and the measures its outputs give the
    distribution checks."""

    kind: str | None
    letters: int | None = None
    language: Any = None
    pages: float | None = None
    entities: int | None = None
    claims: int | None = None
    measures: dict[str, float] = field(default_factory=dict)


@dataclass
class _Rolling:
    """A stage's latest outcomes (dispatches, or engine calls): each
    whether it failed and whether it came from cached answers. The check
    fires as its share of failures rises above the threshold, and again
    only once it has fallen back to it."""

    outcomes: deque
    armed: bool = True

    def reset(self) -> None:
        self.outcomes.clear()
        self.armed = True


@dataclass
class _FirstOf:
    """The first documents of a kind a stage took, each with its latest
    outcome."""

    outcomes: dict = field(default_factory=dict)
    fired: bool = False

    def reset(self) -> None:
        self.outcomes.clear()
        self.fired = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _start_timer(seconds: float, callback: Callable[[], None]) -> Any:
    """A daemon timer, started, that calls `callback` after `seconds`, or
    after the longest wait a thread can make (threading.TIMEOUT_MAX); it
    is returned, to be cancelled."""
    timer = threading.Timer(min(seconds, threading.TIMEOUT_MAX), callback)
    timer.daemon = True
    timer.start()
    return timer


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _foldable(request: dict) -> bool:
    """Whether a control request's scope is one the driver's fold can
    take: a name, or none (the whole run)."""
    scope = request.get("scope")
    return not scope or isinstance(scope, str)


def _fold_scopes(
    requests: Sequence[dict],
) -> tuple[dict[str, str], dict[str, tuple[datetime | None, bool]]]:
    """The control file as the driver folds it (rundir.fold_control):
    each stopped scope, paused or aborted, and each pause's stretch
    (_pause_stretches). A request the fold cannot take is left out, as
    the monitor leaves it out."""
    foldable = [request for request in requests if _foldable(request)]
    return fold_control(foldable).scopes, _pause_stretches(foldable)


def _pause_stretches(
    requests: Sequence[dict],
) -> dict[str, tuple[datetime | None, bool]]:
    """For each scope a pause holds, folded as rundir.fold_control folds
    the control file: when the pause began, and whether the anomaly checks
    asked for every pause of it since."""
    stretches: dict[str, tuple[datetime | None, bool]] = {}
    aborted: set[str] = set()
    for request in requests:
        action = request.get("action")
        if action == "start":
            stretches.clear()
            aborted.clear()
            continue
        scope = request.get("scope") or "run"
        if not isinstance(scope, str) or scope in aborted:
            continue
        if action == PAUSE:
            ours = request.get("by") == BY
            if scope in stretches:
                since, only_ours = stretches[scope]
                stretches[scope] = (since, only_ours and ours)
            else:
                stretches[scope] = (_time(request.get("at")), ours)
        elif action == RESUME:
            stretches.pop(scope, None)
        elif action == ABORT:
            stretches.pop(scope, None)
            aborted.add(scope)
    return stretches


def _requests(data: bytes) -> list[dict]:
    """The requests in a control file's bytes, read as stages.read_jsonl
    reads a log: split at newlines only, with a torn or blank line
    skipped."""
    requests = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            requests.append(entry)
    return requests


@contextlib.contextmanager
def _locked_control(path: Path) -> Iterator[list[dict]]:
    """The requests of the control file at `path`, read under the lock its
    appenders take (rundir's _append_line, fcntl.lockf), which is held to
    the end of the block: a request the block appends follows every
    request it read, and another process's request waits for the block.

    The lock is POSIX's, and so the process's: a request another thread
    of this process appends does not wait for it, and closing any
    descriptor on the file in this process (a reader's, or the appender's
    the block calls) releases it. A control file that cannot be opened for
    writing is read without the lock, and a missing one holds no
    request."""
    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError:
        yield read_jsonl(path)
        return
    try:
        with contextlib.suppress(OSError):
            fcntl.lockf(descriptor, fcntl.LOCK_EX)
        chunks = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
        yield _requests(b"".join(chunks))
    finally:
        os.close(descriptor)


# -- the pauses' timeouts -----------------------------------------------------


@dataclass
class _Watched:
    """A slice the checks paused: the check that paused it last, and that
    check's timeout, clock and timer; when the checks asked for the pause,
    as the control file stamps the request (`origin`, once a look has read
    it); and the pending timer that looks at the slice next, by the token
    it calls back with, and when it is due on that clock."""

    check: str
    timeout: float
    now: Callable[[], datetime]
    timer: Callable[[float, Callable[[], None]], Any]
    origin: datetime | None = None
    pending: Any = None
    token: object | None = None
    due: datetime | None = None


def _cancel(handle: Any) -> None:
    cancel = getattr(handle, "cancel", None)
    if callable(cancel):
        cancel()


class _SliceWatch:
    """The slices of one run directory that the anomaly checks paused,
    watched for the rest of the process, so that their pauses time out.

    A slice stays watched from the checks' pause on, whatever becomes of
    the pause. A driver that invokes the run again asks again, after the
    new start marker, for the pauses that stood before it (a carry), with
    the `by` they had and a new time: a pause of the checks stands again,
    and no invocation's checks asked for it. So each look reads the
    control file as the driver folds it, under the lock its appenders
    take, and aborts the slice when a pause of the checks alone stands on
    it and the timeout has passed since the checks asked for the pause
    (its origin, which a carry does not change). A look that finds such a
    pause short of its timeout looks again when it is due; any other look,
    the one that aborts included, looks again when the checks' request
    would reach its timeout, or once that has passed, a timeout later, for
    a carry still to come. One pending timer per slice makes the looks,
    and so does every check the orchestrator calls. The watch belongs to
    the process whose checks asked for the pause: a pause carried into
    another process is not watched there."""

    def __init__(self, path: Path) -> None:
        self.run = RunDir(path)
        self._lock = threading.Lock()
        self._slices: dict[str, _Watched] = {}

    def watch(
        self,
        scope: str,
        check: str,
        timeout: float | None,
        now: Callable[[], datetime],
        timer: Callable[[float, Callable[[], None]], Any],
    ) -> None:
        """Watch a slice the checks have just paused, with their timeout,
        clock and timer. With no timeout the slice is not watched: its
        pauses hold until someone resumes it."""
        with self._lock:
            watched = self._slices.pop(scope, None)
            if watched is not None:
                _cancel(watched.pending)
            if timeout is not None:
                watched = _Watched(check, timeout, now, timer)
                self._slices[scope] = watched
                self._look(scope, watched)

    def look(self) -> None:
        """Look at every watched slice now."""
        with self._lock:
            for scope, watched in list(self._slices.items()):
                self._look(scope, watched)

    def _fire(self, scope: str, token: object) -> None:
        """A pending timer's end: its slice is looked at, unless a later
        look has put another timer in its place."""
        try:
            with self._lock:
                watched = self._slices.get(scope)
                if watched is None or watched.token is not token:
                    return
                watched.pending = watched.token = watched.due = None
                self._look(scope, watched)
        except Exception as exc:
            _report(self.run, "timer", exc)

    def _look(self, scope: str, watched: _Watched) -> None:
        """Look at a slice, and see that its next look is pending: a timer
        is started when none is, or when this look wants one sooner than
        the pending one is due, which is then cancelled."""
        due = self._judge(scope, watched)
        if watched.pending is not None and (
            watched.due is not None and watched.due <= due
        ):
            return
        _cancel(watched.pending)
        token = object()
        watched.token, watched.due = token, due
        wait = max(0.0, (due - watched.now()).total_seconds())
        watched.pending = watched.timer(wait, lambda: self._fire(scope, token))

    def _judge(self, scope: str, watched: _Watched) -> datetime:
        """Abort the slice when a pause of the checks alone stands on it
        and the timeout has passed since its origin; when the slice is to
        be looked at again: when a pause standing short of the timeout
        reaches it, or else when the checks' request would reach it, or,
        that past, a timeout from now; and a second more, since the
        control file stamps its requests in whole seconds."""
        margin = timedelta(seconds=watched.timeout + 1)
        now = watched.now()
        with _locked_control(self.run.path / CONTROL) as requests:
            state, stretches = _fold_scopes(requests)
            since, ours = stretches.get(scope, (None, False))
            if state.get(scope) == "paused" and ours and since is not None:
                if watched.origin is None:
                    watched.origin = since
                start = min(since, watched.origin)
                if (now - start).total_seconds() < watched.timeout:
                    return start + margin
                what, name = scope.split(":", 1)
                self.run.request(
                    ABORT,
                    by=BY,
                    reason=f"{watched.check}: not resumed within "
                    f"{watched.timeout:g} s",
                    check=watched.check,
                    **{what: name},
                )
        origin = watched.origin
        if origin is not None and origin + margin > now:
            return origin + margin
        return now + margin


# each run directory's watch, made when the checks first pause one of its
# slices
_WATCHES: dict[str, _SliceWatch] = {}
_WATCHES_LOCK = threading.Lock()


def _watch_of(path: Path, *, make: bool = True) -> _SliceWatch | None:
    """The watch of the run directory at `path`: made when there is none
    and `make` asks for one; None otherwise."""
    where = os.path.abspath(path)
    with _WATCHES_LOCK:
        watch = _WATCHES.get(where)
        if watch is None and make:
            watch = _WATCHES[where] = _SliceWatch(Path(where))
        return watch


# -- the checks of one invocation ---------------------------------------------


class AnomalyChecks:
    """The anomaly checks of one invocation of a run: one RunDir.

    `stored`, `reused` and `failed` are called as a stage output is
    stored, as a stored output is reused when its document is dispatched,
    and as a stage fails; each first reads the events the invocation has
    appended since the last call. `now` gives the time (UTC), `timer`
    (seconds, callback) starts the timer that aborts a fail-fast pause
    nobody resumed, and `page_count` counts the pages of a document's
    medium (transcription_check.media_page_count)."""

    def __init__(
        self,
        run: RunDir,
        config: AnomalyConfig | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
        page_count: Callable[[Path], int | None] | None = None,
    ) -> None:
        self.run = run
        self.config = config if config is not None else load_config()
        self.digest = self.config.digest()
        self._now = now or _utc_now
        self._timer = timer or _start_timer
        self._page_count = page_count or media_page_count
        self._lock = threading.RLock()
        self._tail = _Tail(
            run.path / EVENTS, json.dumps(run.invocation).encode()
        )
        # stage attempts under way, by (document, stage); the latest that
        # ended; and the stage each document is in, which the cache's
        # events, naming the document alone, are counted for
        self._attempts: dict[tuple[str, str], _Attempt] = {}
        self._ended: dict[tuple[str, str], _Attempt] = {}
        self._current: dict[str, str] = {}
        self._facts: dict[str, _Facts] = {}
        self._pages: dict[str, int | None] = {}
        # each distribution's values so far, by (measure, kind)
        self._samples: dict[tuple[str, str | None], list[float]] = {}
        self._stage_windows: dict[str, _Rolling] = {}
        self._call_windows: dict[str, _Rolling] = {}
        self._first: dict[tuple[str, str], _FirstOf] = {}
        self._planned: Counter | None = None
        self._raised: set[tuple] = set()
        # the slices these checks paused in their invocation, each with
        # what starts afresh once it is resumed, and the slices they saw
        # aborted; the run directory's watch times the pauses out
        self._paused: dict[str, Callable[[], None]] = {}
        self._aborted: set[str] = set()

    # -- what the orchestrator calls ------------------------------------------

    def stored(self, doc: Any, stage: str, output: Any) -> None:
        """Check an output of `stage` the run has made for `doc` and
        stored."""
        with self._lock:
            self._poll()
            attempt = self._ended.get((doc.sha1, stage))
            self._output(
                doc,
                stage,
                output,
                carried=attempt is not None and attempt.carried,
            )

    def reused(self, doc: Any, stage: str, output: Any) -> None:
        """Check a stored output of `stage` the run reuses for `doc`."""
        with self._lock:
            self._poll()
            self._output(doc, stage, output, carried=True)

    def failed(self, doc: Any, stage: str) -> None:
        """Take in a failed stage, with everything else the run's events
        say since the last call."""
        with self._lock:
            self._poll()

    def poll(self) -> None:
        """Read the invocation's new events, and look at the slices: one
        these checks paused that has been resumed starts its check
        afresh, and one paused past its timeout is aborted."""
        with self._lock:
            self._poll()

    # -- outputs ------------------------------------------------------------

    def _facts_of(self, doc: Any) -> _Facts:
        facts = self._facts.get(doc.sha1)
        if facts is None:
            facts = self._facts[doc.sha1] = _Facts(kind=doc.kind)
        return facts

    def _output(
        self,
        doc: Any,
        stage: str,
        output: Any,
        *,
        carried: bool,
        online: bool = True,
    ) -> None:
        facts = self._facts_of(doc)
        if stage == TRANSCRIPTION:
            found = self._transcription(doc, facts, output)
        elif stage == EXTRACTION:
            found = self._batch(doc, facts, output)
        elif stage == CRITIQUE:
            found = self._critique(doc, facts, output)
        else:
            found = []
        if online:
            found += self._against_run(facts, _MEASURES.get(stage, ()))
        for finding in found:
            self._raise(
                finding,
                stage=stage,
                subject=doc.sha1,
                kind=doc.kind,
                carried=carried,
            )

    def _medium_pages(self, doc: Any) -> int | None:
        """A medium's pages, counted once per document: None for another
        kind of document, or a medium whose count is unknown."""
        if doc.kind != "media":
            return None
        if doc.sha1 not in self._pages:
            pages = None
            with contextlib.suppress(OSError, TypeError):
                pages = self._page_count(Path(doc.source))
            self._pages[doc.sha1] = pages
        return self._pages[doc.sha1]

    def _learn_transcription(
        self, doc: Any, facts: _Facts, transcription: Any
    ) -> tuple[str, int | None]:
        text = _get(transcription, "text")
        text = text if isinstance(text, str) else ""
        page_count = self._medium_pages(doc)
        facts.letters = text_char_count(text)
        facts.language = _get(transcription, "language")
        facts.pages = document_pages(
            doc.kind, text, page_count, self.config.chars_per_page
        )
        return text, page_count

    def _transcription(
        self, doc: Any, facts: _Facts, transcription: Any
    ) -> list[Finding]:
        text, page_count = self._learn_transcription(doc, facts, transcription)
        return transcription_findings(
            self.config,
            text=text,
            engine=_get(transcription, "engine"),
            page_count=page_count,
            resolution=(doc.state.detail or {}).get("language"),
        )

    def _batch(self, doc: Any, facts: _Facts, batch: Any) -> list[Finding]:
        if facts.letters is None:
            stored = _read_json(doc.transcription)
            if isinstance(stored, Mapping):
                self._learn_transcription(doc, facts, stored)
        entities = len(_get(batch, "entities") or [])
        claims = list(_get(batch, "claims") or [])
        facts.entities, facts.claims = entities, len(claims)
        if facts.pages:
            for name, count in (
                ("entity-yield", entities),
                ("claim-yield", len(claims)),
            ):
                # none at all is the zero checks' finding, not a yield's
                if count:
                    facts.measures[name] = count / facts.pages
        return batch_findings(
            self.config,
            entities=entities,
            claims=len(claims),
            excerpts=[_excerpt_language(claim) for claim in claims],
            language=facts.language,
            letters=facts.letters,
            coverage=_read_json(Path(doc.extraction) / "coverage.json"),
        )

    def _critique(
        self, doc: Any, facts: _Facts, findings: Any
    ) -> list[Finding]:
        if facts.entities is None:
            stored = _read_json(doc.batch)
            if isinstance(stored, Mapping):
                facts.entities = len(stored.get("entities") or [])
                facts.claims = len(stored.get("claims") or [])
        items = (facts.entities or 0) + (facts.claims or 0)
        if items:
            facts.measures["critic-rate"] = len(findings or []) / items
        return []

    def _against_run(
        self, facts: _Facts, names: Sequence[str]
    ) -> list[Finding]:
        """Each measure against the run's documents of the kind so far,
        which it then joins; each kind's values are kept in order."""
        found = []
        for name in names:
            value = facts.measures.get(name)
            if value is None:
                continue
            samples = self._samples.setdefault((name, facts.kind), [])
            finding = outlier(
                name, self.config.checks.outlier(name), value, samples
            )
            if finding is not None:
                found.append(finding)
            bisect.insort(samples, value)
        return found

    def _raise(
        self,
        finding: Finding,
        *,
        stage: str,
        subject: str | None,
        kind: Any,
        carried: bool,
    ) -> None:
        """Append the alert, once per document, stage, check and status in
        the invocation."""
        key = (subject, stage, finding.check, carried)
        if key in self._raised:
            return
        self._raised.add(key)
        self.run.alert(
            stage=stage,
            check=finding.check,
            subject=(subject or "")[:SUBJECT_PREFIX],
            value=finding.value,
            carried=carried,
            kind=kind if isinstance(kind, str) else None,
            limit=finding.limit,
            config=self.digest,
            **finding.fields,
        )

    # -- the run's events ----------------------------------------------------

    def _poll(self) -> None:
        """Read the invocation's new events once the slices these checks
        paused have been looked at, so that a slice resumed since is
        counted afresh from its new events on; then look at every watched
        slice of the run directory."""
        if self._paused:
            self._note_slices()
        for event in self._tail.read():
            if event.get("invocation") == self.run.invocation:
                self._fold(event)
        watch = _watch_of(self.run.path, make=False)
        if watch is not None:
            watch.look()

    def _fold(self, event: dict) -> None:
        name = event.get("event")
        subject = event.get("subject")
        if not isinstance(subject, str) or not subject:
            return
        if name == "started":
            stage = event.get("stage")
            if isinstance(stage, str) and stage:
                self._attempts[(subject, stage)] = _Attempt()
                self._current[subject] = stage
        elif name in ("done", "failed"):
            self._end(event, subject, failed=name == "failed")
        elif name == SET_ASIDE:
            if event.get("stage") == TRANSCRIPTION:
                self._check_failed(event, subject, carried=True)
        elif name == "call":
            self._call(event, subject)
        else:
            self._cache(event, name, subject)

    def _attempt_of(self, subject: str) -> _Attempt | None:
        stage = self._current.get(subject)
        return self._attempts.get((subject, stage)) if stage else None

    def _cache(self, event: dict, name: Any, subject: str) -> None:
        attempt = self._attempt_of(subject)
        if attempt is None:
            return
        chunk = event.get("role") == "extract" and event.get("call") != "open"
        if name in ("cache-miss", "cache-replay"):
            attempt.engine += 1
        elif name == "cache-hit":
            attempt.hits += 1
            attempt.chunk_answers += chunk
        elif name == "cache-reject":
            reason = event.get("reason")
            if (
                event.get("role") == "extract"
                and isinstance(reason, str)
                and reason.startswith("chunk:")
            ):
                attempt.truncations += 1

    def _call(self, event: dict, subject: str) -> None:
        outcome = event.get("outcome")
        if outcome not in ("ok", "error"):
            return
        failed = outcome == "error"
        attempt = self._attempt_of(subject)
        chunk = event.get("role") == "extract" and event.get("call") != "open"
        if attempt is not None and chunk:
            attempt.chunk_calls += 1
            attempt.chunk_errors += failed
            attempt.chunk_answers += not failed
        stage = event.get("stage")
        settings = self.config.checks.call_failures
        if not settings.enabled or not isinstance(stage, str) or not stage:
            return
        window = self._call_windows.get(stage)
        if window is None:
            window = self._call_windows[stage] = _Rolling(
                deque(maxlen=settings.window)
            )
        window.outcomes.append((failed, False))
        self._judge_rolling(
            "call-failures",
            settings,
            window,
            stage=stage,
            subject=subject,
            kind=event.get("kind"),
            what="engine calls",
        )

    def _end(self, event: dict, subject: str, *, failed: bool) -> None:
        stage = event.get("stage")
        if not isinstance(stage, str) or not stage:
            return
        attempt = self._attempts.pop((subject, stage), None) or _Attempt()
        if self._current.get(subject) == stage:
            del self._current[subject]
        self._ended[(subject, stage)] = attempt
        kind = event.get("kind")
        kind = kind if isinstance(kind, str) else None
        if stage == EXTRACTION:
            checks = self.config.checks
            for finding in (
                share(
                    "chunk-failures",
                    checks.chunk_failures,
                    attempt.chunk_errors,
                    attempt.chunk_calls,
                ),
                share(
                    "truncations",
                    checks.truncations,
                    attempt.truncations,
                    attempt.chunk_answers,
                ),
                rejects_on_failure(self.config, event.get("reason"))
                if failed
                else None,
            ):
                if finding is not None:
                    self._raise(
                        finding,
                        stage=stage,
                        subject=subject,
                        kind=kind,
                        carried=attempt.carried,
                    )
        elif stage == TRANSCRIPTION and failed:
            self._check_failed(event, subject, carried=attempt.carried)
        self._stage_outcome(stage, subject, kind, failed, attempt.carried)

    def _check_failed(
        self, event: dict, subject: str, *, carried: bool
    ) -> None:
        """The transcription check's verdict in a failed transcription's
        reason, or in that of a reused one it set aside."""
        if not self.config.checks.transcription_check.enabled:
            return
        finding = check_failure(event.get("reason"))
        if finding is not None:
            self._raise(
                finding,
                stage=TRANSCRIPTION,
                subject=subject,
                kind=event.get("kind"),
                carried=carried,
            )

    # -- rates --------------------------------------------------------------

    def _stage_outcome(
        self,
        stage: str,
        subject: str,
        kind: str | None,
        failed: bool,
        carried: bool,
    ) -> None:
        settings = self.config.checks.stage_failures
        if settings.enabled:
            window = self._stage_windows.get(stage)
            if window is None:
                window = self._stage_windows[stage] = _Rolling(
                    deque(maxlen=settings.window)
                )
            window.outcomes.append((failed, carried))
            self._judge_rolling(
                "stage-failures",
                settings,
                window,
                stage=stage,
                subject=subject,
                kind=kind,
                what="dispatches",
            )
        if kind is not None:
            self._first_of_kind(stage, kind, subject, failed, carried)

    def _judge_rolling(
        self,
        check: str,
        settings: Rolling,
        window: _Rolling,
        *,
        stage: str,
        subject: str,
        kind: Any,
        what: str,
    ) -> None:
        outcomes = window.outcomes
        if len(outcomes) < settings.min_count:
            return
        failures = [carried for failed, carried in outcomes if failed]
        measured = len(failures) / len(outcomes)
        if measured <= settings.max_share:
            window.armed = True
            return
        if not window.armed:
            return
        window.armed = False
        fields: dict[str, Any] = {"count": len(failures), "of": len(outcomes)}
        paused = self._fail_fast(
            settings,
            check,
            ("stage", stage),
            f"{check}: {len(failures)} of the last {len(outcomes)} "
            f"{stage} {what} failed",
            window.reset,
        )
        if paused:
            fields["pause"] = paused
        self._raise(
            Finding(check, round(measured, 3), settings.max_share, fields),
            stage=stage,
            subject=subject,
            kind=kind,
            carried=all(failures),
        )

    def _planned_of(self, kind: str) -> int:
        """How many documents of `kind` the run plans (run.json)."""
        if not self._planned:
            documents = self.run.manifest().get("documents")
            self._planned = Counter(
                document.get("kind")
                for document in documents or []
                if isinstance(document, dict)
            )
        return self._planned.get(kind, 0)

    def _first_of_kind(
        self,
        stage: str,
        kind: str,
        subject: str,
        failed: bool,
        carried: bool,
    ) -> None:
        settings = self.config.checks.kind_failures
        if not settings.enabled:
            return
        first = self._first.get((stage, kind))
        if first is None:
            first = self._first[(stage, kind)] = _FirstOf()
        if (
            subject not in first.outcomes
            and len(first.outcomes) >= settings.first
        ):
            return
        first.outcomes[subject] = (failed, carried)
        if first.fired:
            return
        failures = [
            from_cache
            for did_fail, from_cache in first.outcomes.values()
            if did_fail
        ]
        planned = self._planned_of(kind)
        limit = settings.max_share * (
            min(settings.first, planned) if planned else settings.first
        )
        if len(failures) <= limit:
            return
        first.fired = True
        fields: dict[str, Any] = {
            "of": len(first.outcomes),
            "first": settings.first,
        }
        paused = self._fail_fast(
            settings,
            "kind-failures",
            ("kind", kind),
            f"kind-failures: {stage} failed {len(failures)} of the first "
            f"{len(first.outcomes)} {kind} documents",
            first.reset,
        )
        if paused:
            fields["pause"] = paused
        self._raise(
            Finding("kind-failures", len(failures), round(limit, 3), fields),
            stage=stage,
            subject=subject,
            kind=kind,
            carried=all(failures),
        )

    # -- fail-fast pauses ----------------------------------------------------

    def _fail_fast(
        self,
        settings: Rolling | FirstOfKind,
        check: str,
        slice_: tuple[str, str],
        reason: str,
        reset: Callable[[], None],
    ) -> str | None:
        """Ask for a pause of the slice (a stage or a kind) for a fail-fast
        check, and have the run directory's watch time it out: the slice's
        scope, or None when the check is not fail-fast or the slice is
        stopped already."""
        if not settings.fail_fast:
            return None
        what, name = slice_
        scope = f"{what}:{name}"
        if scope in self._paused or scope in self._aborted:
            return None
        if _fold_scopes(self._control())[0].get(scope):
            return None
        self.run.request(
            PAUSE, by=BY, reason=reason, check=check, **{what: name}
        )
        self._paused[scope] = reset
        watch = _watch_of(self.run.path)
        watch.watch(
            scope, check, self.config.pause_timeout_s, self._now, self._timer
        )
        return scope

    def _control(self) -> list[dict]:
        return read_jsonl(self.run.path / CONTROL)

    def _note_slices(self) -> None:
        """Each slice these checks paused, as the control file leaves it:
        resumed, or cleared by a start marker, its check starts afresh on
        what comes after; aborted, it is never paused again in the
        invocation."""
        state, _stretches = _fold_scopes(self._control())
        for scope, reset in list(self._paused.items()):
            held = state.get(scope)
            if held == "paused":
                continue
            if held is None:
                reset()
            else:
                self._aborted.add(scope)
            del self._paused[scope]


def _excerpt_language(claim: Any) -> Any:
    excerpt = _get(_get(claim, "source"), "transcription_excerpt")
    return _get(excerpt, "language")


def _read_json(path: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


# -- one set of checks per invocation, and the orchestrator's hooks -----------

# each run directory's checks, those of the latest invocation seen: a new
# invocation's checks take the place of the last one's
_REGISTRY: dict[str, AnomalyChecks] = {}
_REGISTRY_LOCK = threading.Lock()


def _registered(run: RunDir) -> str:
    return os.path.abspath(run.path)


def attach(
    run: RunDir, config: AnomalyConfig | None = None, **options: Any
) -> AnomalyChecks:
    """Make the checks of `run`'s invocation with these thresholds
    (load_config's, by default) and these AnomalyChecks options: for a
    driver that sets them itself, before the invocation's first stage."""
    checks = AnomalyChecks(run, config, **options)
    with _REGISTRY_LOCK:
        _REGISTRY[_registered(run)] = checks
    return checks


def checks_for(run: RunDir) -> AnomalyChecks:
    """The checks of `run`'s invocation, shared by every RunDir of the
    same directory and invocation, and made on first use with
    load_config's thresholds. A file of thresholds that cannot be used
    leaves the defaults in force, and an `anomaly-config` alert and a
    warning say why."""
    where = _registered(run)
    with _REGISTRY_LOCK:
        checks = _REGISTRY.get(where)
    if checks is not None and checks.run.invocation == run.invocation:
        return checks
    problem = None
    try:
        config = load_config()
    except AnomalyConfigError as exc:
        config, problem = default_config(), str(exc)
    made = AnomalyChecks(run, config)
    with _REGISTRY_LOCK:
        checks = _REGISTRY.get(where)
        if checks is None or checks.run.invocation != run.invocation:
            checks = _REGISTRY[where] = made
    if checks is made and problem is not None:
        logger.warning("%s; the default thresholds are used", problem)
        run.alert(
            stage="run",
            check="anomaly-config",
            subject="",
            value=True,
            carried=False,
            config=made.digest,
            problem=problem[:500],
        )
    return checks


def _report(run: RunDir, where: str, exc: BaseException) -> None:
    """A check that failed: a warning and an event naming the exception,
    never its message, which could quote an output."""
    logger.warning("anomaly checks: %s in %s", type(exc).__name__, where)
    with contextlib.suppress(Exception):
        run.emit(ERROR_EVENT, error=type(exc).__name__, hook=where)


def _hook(run: RunDir, name: str, *args: Any) -> None:
    try:
        getattr(checks_for(run), name)(*args)
    except Exception as exc:
        _report(run, name, exc)


def stored(run: RunDir, doc: Any, stage: str, output: Any) -> None:
    """The orchestrator stored `output`, made by `stage` for `doc`
    (a DocumentRun). Never raises."""
    _hook(run, "stored", doc, stage, output)


def reused(run: RunDir, doc: Any, stage: str, output: Any) -> None:
    """The orchestrator reuses the stored `output` of `stage` for `doc`.
    Never raises."""
    _hook(run, "reused", doc, stage, output)


def failed(run: RunDir, doc: Any, stage: str) -> None:
    """The orchestrator's `stage` failed for `doc`. Never raises."""
    _hook(run, "failed", doc, stage)


# -- checking a run's stored outputs again ----------------------------------


@dataclass(frozen=True)
class _Recorded:
    """A document directory's record, as a recheck reads it."""

    sha1: str
    kind: str
    source: Path
    state: Any
    transcription: Path
    batch: Path
    critique: Path
    extraction: Path


def _recorded(state_dir: Path) -> Iterator[_Recorded]:
    """The document directories of a state directory whose state.json
    names a source and the SHA-1 it was last processed at; a directory
    set aside, and anything else, is skipped."""
    from yizkr.pipeline.orchestrate import DocumentRun

    for directory in sorted(state_dir.iterdir()):
        if directory.name.startswith(".") or not directory.is_dir():
            continue
        state = _read_json(directory / "state.json")
        if not isinstance(state, dict) or not isinstance(
            state.get("file"), str
        ):
            continue
        doc = DocumentRun(
            Path(state["file"]), state_dir, dirname=directory.name
        )
        sha1 = doc.state.content_sha1
        if doc.unreadable is not None or not sha1:
            continue
        yield _Recorded(
            sha1=sha1,
            kind=doc.kind,
            source=doc.source,
            state=doc.state,
            transcription=doc.transcription,
            batch=doc.batch,
            critique=doc.critique,
            extraction=doc.extraction,
        )


def _median_without(ordered: Sequence[float], index: int) -> float:
    """The median of the sorted `ordered` without its value at `index`."""
    rest = len(ordered) - 1

    def at(position: int) -> float:
        return ordered[position if position < index else position + 1]

    middle = rest // 2
    if rest % 2:
        return at(middle)
    return (at(middle - 1) + at(middle)) / 2


def _recheck_document(checks: AnomalyChecks, doc: _Recorded) -> None:
    """Check one document's stored outputs again (recheck)."""
    transcription = _read_json(doc.transcription)
    if isinstance(transcription, Mapping):
        text = transcription.get("text")
        finding = (
            check_verdict(
                checks.config,
                text=text,
                engine=transcription.get("engine"),
                page_count=checks._medium_pages(doc),
            )
            if isinstance(text, str)
            else None
        )
        if finding is not None:
            checks._raise(
                finding,
                stage=TRANSCRIPTION,
                subject=doc.sha1,
                kind=doc.kind,
                carried=True,
            )
        checks._output(
            doc, TRANSCRIPTION, transcription, carried=True, online=False
        )
    batch = _read_json(doc.batch)
    if isinstance(batch, Mapping):
        checks._output(
            doc,
            GEDCOM if doc.kind == "gedcom" else EXTRACTION,
            batch,
            carried=True,
            online=False,
        )
    critique = _read_json(doc.critique)
    if isinstance(critique, list):
        checks._output(doc, CRITIQUE, critique, carried=True, online=False)


def recheck(
    state_dir: str | os.PathLike[str],
    config: AnomalyConfig | None = None,
    *,
    page_count: Callable[[Path], int | None] | None = None,
) -> list[dict]:
    """Check the outputs stored in a run directory's document directories
    again, with `config` (load_config's thresholds by default), without
    dispatching anything or recomputing any stage, and append the alerts
    to the run's alerts, all `carried`, under an invocation of their own
    that an `anomaly-recheck` event opens. Returns them.

    Each stored transcription meets the transcription check as it is now,
    as a run's reuse of it would (only on the routes the check judges),
    and every output meets the per-output checks. The distribution checks
    compare each document with every other document of its kind in the
    run, not only the ones before it. The rate checks, which read a run's
    dispatches and calls rather than its outputs, are not checked again.
    A document whose outputs the checks cannot read is an `anomaly-error`
    event, and the others are checked. Nothing is written into a
    document's directory."""
    state_dir = Path(state_dir)
    run = RunDir(state_dir, invocation=f"recheck-{uuid.uuid4().hex[:8]}")
    checks = AnomalyChecks(run, config, page_count=page_count)
    documents = list(_recorded(state_dir))
    run.emit(RECHECK_EVENT, config=checks.digest, documents=len(documents))
    for doc in documents:
        try:
            _recheck_document(checks, doc)
        except Exception as exc:
            # one document's outputs that the checks cannot read stop
            # nothing: the others are checked
            _report(run, "recheck", exc)
    for stage, names in _MEASURES.items():
        for name in names:
            settings = checks.config.checks.outlier(name)
            by_kind: dict[Any, list[tuple[float, _Recorded]]] = {}
            for doc in documents:
                value = checks._facts_of(doc).measures.get(name)
                if value is not None:
                    by_kind.setdefault(doc.kind, []).append((value, doc))
            for measured in by_kind.values():
                others = len(measured) - 1
                if not settings.enabled or others < settings.min_samples:
                    continue
                ordered = sorted(value for value, _doc in measured)
                for value, doc in measured:
                    median = _median_without(
                        ordered, bisect.bisect_left(ordered, value)
                    )
                    finding = _against(name, settings, value, median, others)
                    if finding is not None:
                        checks._raise(
                            finding,
                            stage=stage,
                            subject=doc.sha1,
                            kind=doc.kind,
                            carried=True,
                        )
    return [
        alert
        for alert in run.alerts()
        if alert.get("invocation") == run.invocation
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m yizkr.pipeline.anomalies recheck <run dir>`."""
    parser = argparse.ArgumentParser(
        prog="python -m yizkr.pipeline.anomalies",
        description=(
            "Check a run directory's stored outputs again with the anomaly "
            "thresholds as they are now, without recomputing any stage "
            "(docs/ANOMALIES.md)."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    again = commands.add_parser(
        "recheck",
        help="append the alerts the stored outputs raise now",
    )
    again.add_argument("run_dir", type=Path, help="the run directory")
    again.add_argument(
        "--config",
        type=Path,
        help=(
            f"a file of thresholds over the defaults, in place of the one "
            f"${CONFIG_VARIABLE} names"
        ),
    )
    args = parser.parse_args(argv)
    if not (args.run_dir / MANIFEST).is_file():
        print(
            f"{args.run_dir} is no run directory: it has no {MANIFEST}",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_config(args.config)
    except AnomalyConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    alerts = recheck(args.run_dir, config)
    print(
        f"{len(alerts)} alert{'' if len(alerts) == 1 else 's'} appended to "
        f"{args.run_dir / ALERTS} (thresholds {config.digest()})"
    )
    for check, count in sorted(
        Counter(alert.get("check") for alert in alerts).items()
    ):
        print(f"  {check:24} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
