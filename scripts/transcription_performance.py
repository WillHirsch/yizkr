"""Explicit local measurement of transcription validation; no model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

from tests.fixtures.transcription_performance import workloads

from yizkr.pipeline.transcription_check import check_transcription

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = 5
ITERATIONS = 3
MAX_SLOWDOWN = 1.5
HANG_GUARD = 60


def environment() -> dict:
    """Comparisons use the same quiet Linux host and locked interpreter."""
    if (
        platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or platform.python_implementation() != "CPython"
    ):
        raise ValueError("measurement requires CPython on Linux x86_64")
    cpu = next(
        line.split(":", 1)[1].strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines()
        if line.startswith("model name")
    )
    return {
        "platform": platform.platform(),
        "cpu": cpu,
        "affinity": sorted(os.sched_getaffinity(0)),
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "optimized": sys.flags.optimize,
        "lock_sha256": hashlib.sha256(
            (ROOT / "uv.lock").read_bytes()
        ).hexdigest(),
    }


def measure() -> dict:
    """Time warm validation calls, checking every verdict outside timing."""
    cases = workloads()
    identity = [
        {
            "name": case.name,
            "arguments": case.arguments,
            "expected": case.expected,
        }
        for case in cases
    ]
    encoded = json.dumps(
        identity,
        sort_keys=True,
        ensure_ascii=False,
        default=lambda value: value.model_dump(mode="json"),
    ).encode()
    report = {
        "format": 1,
        "environment": environment(),
        "workload_sha256": hashlib.sha256(encoded).hexdigest(),
        "settings": {"samples": SAMPLES, "iterations": ITERATIONS},
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "results": {},
    }
    for case in cases:

        def verify(result, case=case):
            observed = result.model_dump()
            if any(
                observed[key] != value for key, value in case.expected.items()
            ):
                raise ValueError(f"{case.name}: unexpected validation result")

        verify(check_transcription(**case.arguments))
        samples = {"wall_seconds": [], "cpu_seconds": []}
        for _ in range(SAMPLES):
            cpu_start = time.process_time()
            wall_start = time.perf_counter()
            results = [
                check_transcription(**case.arguments)
                for _ in range(ITERATIONS)
            ]
            wall = time.perf_counter() - wall_start
            cpu = time.process_time() - cpu_start
            for result in results:
                verify(result)
            samples["wall_seconds"].append(wall / ITERATIONS)
            samples["cpu_seconds"].append(cpu / ITERATIONS)
        report["results"][case.name] = samples
    return report


def medians(report: dict) -> dict[str, float]:
    """Validate timing evidence and derive medians from its raw samples."""
    try:
        settings = report["settings"]
        if (
            report["format"] != 1
            or not isinstance(report["environment"], dict)
            or not report["environment"]
            or not isinstance(report["workload_sha256"], str)
            or not report["workload_sha256"]
            or type(settings["samples"]) is not int
            or settings["samples"] < 3
            or type(settings["iterations"]) is not int
            or settings["iterations"] < 1
            or not report["results"]
        ):
            raise ValueError("invalid timing report metadata")
        found = {}
        for name, result in report["results"].items():
            for metric in ("wall_seconds", "cpu_seconds"):
                values = result[metric]
                if (
                    not isinstance(values, list)
                    or len(values) != settings["samples"]
                ):
                    raise ValueError(f"{name}: missing timing samples")
                if any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value <= 0
                    for value in values
                ):
                    raise ValueError(f"{name}: invalid timing samples")
            found[name] = statistics.median(result["wall_seconds"])
        return found
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid timing report") from exc


def compare(baseline: dict, candidate: dict) -> list[str]:
    """Names exceeding the fixed median wall-time budget; refuse drift."""
    before, after = medians(baseline), medians(candidate)
    for key in ("format", "environment", "workload_sha256", "settings"):
        if baseline[key] != candidate[key]:
            raise ValueError(
                f"incompatible {key}; measure a comparable baseline"
            )
    if before.keys() != after.keys():
        raise ValueError("incompatible workload names")
    return [
        name for name in before if after[name] > MAX_SLOWDOWN * before[name]
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new JSON report path")
    parser.add_argument("--baseline", type=Path, help="accepted baseline JSON")
    parser.add_argument(
        "--worker", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(measure(), allow_nan=False))
        return 0
    if args.output is None:
        parser.error("--output is required")
    try:
        if args.output.exists():
            raise ValueError(
                "--output must be a new file; reports are not overwritten"
            )
        baseline = None
        if args.baseline is not None:
            baseline = json.loads(args.baseline.read_text())
            medians(baseline)
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.transcription_performance",
                "--worker",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=HANG_GUARD,
            check=True,
        )
        report = json.loads(child.stdout)
        current = medians(report)
        with args.output.open("x") as output:
            output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
        failures = compare(baseline, report) if baseline is not None else []
        for name, value in current.items():
            cpu = statistics.median(report["results"][name]["cpu_seconds"])
            detail = ""
            if baseline is not None:
                before = statistics.median(
                    baseline["results"][name]["wall_seconds"]
                )
                detail = (
                    f"; {value / before:.2f}x baseline "
                    f"(budget {MAX_SLOWDOWN:.2f}x)"
                )
            print(f"{name}: median {value:.6f}s wall, {cpu:.6f}s CPU{detail}")
        if failures:
            print(
                "Performance budget exceeded: " + ", ".join(failures),
                file=sys.stderr,
            )
            return 1
        print(
            "Budget passed"
            if baseline is not None
            else "Measurement only; no baseline compared"
        )
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Validation worker failed:\n{exc.stderr}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Performance check could not complete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
