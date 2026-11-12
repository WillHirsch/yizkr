"""Performance report decisions use authored timings, never real clocks."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from scripts import transcription_performance as performance


def report():
    return {
        "format": 1,
        "environment": {
            "python": "3.11.15",
            "system": "Linux",
            "machine": "x86_64",
            "cpu": "fictional benchmark processor",
        },
        "workload_sha256": "a" * 64,
        "settings": {"samples": 5, "iterations": 3},
        "revision": "1" * 40,
        "dirty": False,
        "results": {
            name: {
                "wall_seconds": [1.0] * 5,
                "cpu_seconds": [0.9] * 5,
            }
            for name in ("repetitive-text", "structured-record")
        },
    }


def test_comparison_uses_median_and_allows_a_new_revision():
    baseline = report()
    candidate = deepcopy(baseline)
    candidate["revision"] = "2" * 40
    candidate["dirty"] = True
    # A single scheduling outlier does not turn stable validation into a
    # regression. CPU measurements are diagnostic, not the wall-time budget.
    candidate["results"]["repetitive-text"]["wall_seconds"] = [
        1.0,
        1.2,
        9.0,
        1.1,
        1.0,
    ]
    candidate["results"]["structured-record"]["cpu_seconds"] = [2.0] * 5
    assert performance.compare(baseline, candidate) == []


@pytest.mark.parametrize(
    "seconds, regressions",
    [(1.5, []), (1.51, ["repetitive-text"])],
)
def test_comparison_enforces_the_material_slowdown_budget(
    seconds, regressions
):
    baseline = report()
    candidate = deepcopy(baseline)
    candidate["results"]["repetitive-text"]["wall_seconds"] = [seconds] * 5
    # A report's optional summary cannot conceal the raw observations.
    candidate["results"]["repetitive-text"]["median_wall_seconds"] = 0.01
    assert performance.compare(baseline, candidate) == regressions


@pytest.mark.parametrize(
    "field, changed",
    [
        ("format", 2),
        ("environment", {"cpu": "another fictional processor"}),
        ("workload_sha256", "b" * 64),
        ("settings", {"samples": 5, "iterations": 4}),
    ],
)
def test_incomparable_reports_refuse_a_performance_verdict(field, changed):
    baseline = report()
    candidate = deepcopy(baseline)
    candidate[field] = changed
    with pytest.raises(ValueError):
        performance.compare(baseline, candidate)


def test_changed_workload_names_refuse_a_comparison():
    baseline = report()
    candidate = deepcopy(baseline)
    candidate["results"]["another-record"] = candidate["results"].pop(
        "structured-record"
    )
    with pytest.raises(ValueError):
        performance.compare(baseline, candidate)


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize(
    "samples",
    [
        [],
        [1.0] * 4,
        [float("nan")] * 5,
        [float("inf")] * 5,
        [0.0] * 5,
        [-1.0] * 5,
    ],
    ids=["empty", "incomplete", "nan", "infinite", "zero", "negative"],
)
def test_invalid_samples_cannot_pass_or_inflate_the_baseline(side, samples):
    reports = {"baseline": report(), "candidate": report()}
    reports[side]["results"]["repetitive-text"]["wall_seconds"] = samples
    with pytest.raises(ValueError):
        performance.compare(reports["baseline"], reports["candidate"])


def test_invalid_cpu_observations_refuse_a_report():
    baseline = report()
    candidate = deepcopy(baseline)
    candidate["results"]["structured-record"]["cpu_seconds"] = [
        float("nan")
    ] * 5
    with pytest.raises(ValueError):
        performance.compare(baseline, candidate)


def return_worker_report(monkeypatch, value):
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(value)),
    )


def refuse_worker(monkeypatch):
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("no measurement should run"),
    )


def test_measurement_only_saves_evidence_without_claiming_a_budget(
    tmp_path, monkeypatch, capsys
):
    value = report()
    return_worker_report(monkeypatch, value)
    output = tmp_path / "measurement.json"
    assert performance.main(["--output", str(output)]) == 0
    assert json.loads(output.read_text()) == value
    assert "Measurement only; no baseline compared" in capsys.readouterr().out


def test_cli_keeps_the_report_when_a_comparison_exceeds_budget(
    tmp_path, monkeypatch, capsys
):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(report()))
    candidate = report()
    candidate["results"]["repetitive-text"]["wall_seconds"] = [2.0] * 5
    return_worker_report(monkeypatch, candidate)
    output = tmp_path / "candidate.json"
    assert (
        performance.main(
            ["--baseline", str(baseline), "--output", str(output)]
        )
        == 1
    )
    assert json.loads(output.read_text()) == candidate
    assert "Performance budget exceeded" in capsys.readouterr().err


def test_cli_refuses_an_incompatible_comparison(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(report()))
    candidate = report()
    candidate["environment"]["cpu"] = "another fictional processor"
    return_worker_report(monkeypatch, candidate)
    output = tmp_path / "candidate.json"
    assert (
        performance.main(
            ["--baseline", str(baseline), "--output", str(output)]
        )
        == 2
    )
    assert json.loads(output.read_text()) == candidate


def test_null_baseline_cannot_become_measurement_only(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text("null")
    refuse_worker(monkeypatch)
    output = tmp_path / "candidate.json"
    assert (
        performance.main(
            ["--baseline", str(baseline), "--output", str(output)]
        )
        == 2
    )
    assert not output.exists()


def test_existing_output_is_not_overwritten_or_measured(tmp_path, monkeypatch):
    output = tmp_path / "baseline.json"
    saved = json.dumps(report())
    output.write_text(saved)
    refuse_worker(monkeypatch)
    assert performance.main(["--output", str(output)]) == 2
    assert output.read_text() == saved


def test_worker_timeout_is_an_incomplete_check(tmp_path, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise performance.subprocess.TimeoutExpired("fictional worker", 60)

    monkeypatch.setattr(performance.subprocess, "run", timeout)
    output = tmp_path / "candidate.json"
    assert performance.main(["--output", str(output)]) == 2
    assert not output.exists()
