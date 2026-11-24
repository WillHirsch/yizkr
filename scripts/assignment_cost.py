"""Explicit offline measurement of the reviewed assignment path's cost.

Generated fictional cohorts (tests/fixtures/assignment_cohort.py) go
through registration, identity application and claim application against
an in-memory Wikibase behind the production clients
(tests/fixtures/fake_wikibase.py). The report gives each operation's
server requests by kind, the records written, the review-store calls and
the wall time, and their totals per phase, per identity and per claim, at
each size. No model, wiki or query service is called, and no archive
setting or instance record is read. docs/TESTING.md, "Assignment path
cost", describes the sizes, the report and the regression baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/assignment_cost_baseline.json"
# the categories a size's summary shows, per identity and per claim
SHOWN = (
    "server requests",
    "entity scans",
    "immutable page reads",
    "item reads",
    "writes",
    "query-service queries",
    "review-store calls",
    "proposals read",
)


def isolate(directory: Path) -> None:
    """Keep the measurement from every archive setting and record: an
    empty settings file and instance records in `directory`."""
    settings = directory / "settings.env"
    settings.write_text("")
    os.environ["YIZKR_ENV_FILE"] = str(settings)
    os.environ["YIZKR_INSTANCE_STATE"] = str(directory / "instances")


def environment() -> dict:
    return {
        "platform": platform.platform(),
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "cpus": os.cpu_count(),
        "lock_sha256": hashlib.sha256(
            (ROOT / "uv.lock").read_bytes()
        ).hexdigest(),
    }


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def measurement(names: list[str]) -> dict:
    from tests.fixtures.assignment_cohort import SIZES, measure

    report = {
        "format": 1,
        "revision": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "environment": environment(),
        "sizes": {},
    }
    for name in names:
        with tempfile.TemporaryDirectory(prefix="assignment-cost-") as tmp:
            isolate(Path(tmp))
            started = time.perf_counter()
            found = measure(
                SIZES[name], Path(tmp) / "run", clock=time.perf_counter
            )
            found["seconds"] = time.perf_counter() - started
        report["sizes"][name] = {"scale": asdict(SIZES[name]), **found}
        print(f"measured {name} in {found['seconds']:.0f} s", flush=True)
    return report


def _row(name: str, size: dict, key: str) -> str:
    per = size[key]
    if not per:
        return f"| {name} | - |" + " - |" * len(SHOWN)
    shown = " | ".join(f"{per['categories'][k]:.1f}" for k in SHOWN)
    seconds = per.get("seconds")
    timed = "-" if seconds is None else f"{seconds:.2f}"
    return f"| {name} | {size['setup']['entities']} | {shown} | {timed} |"


def summary(report: dict) -> str:
    """Markdown tables of the mean cost of one identity and one claim at
    each size of `report`."""
    lines = []
    for key, title in (("per_identity", "identity"), ("per_claim", "claim")):
        lines += [
            f"Mean cost of one {title}:",
            "",
            "| Size | Entities before | "
            + " | ".join(name.capitalize() for name in SHOWN)
            + " | Seconds |",
            "| --- | ---: |" + " ---: |" * (len(SHOWN) + 1),
        ]
        lines += [
            _row(name, size, key) for name, size in report["sizes"].items()
        ]
        lines.append("")
    return "\n".join(lines)


def compare(baseline: dict, candidate: dict) -> str:
    """The change of each size's per-identity and per-claim categories from
    `baseline` to `candidate`, for the sizes both measured with the same
    scale; refuses reports of another format."""
    if baseline.get("format") != 1 or candidate.get("format") != 1:
        raise ValueError("compare reports of format 1")
    lines = ["Change from the baseline (candidate / baseline):", ""]
    for name, size in candidate["sizes"].items():
        before = baseline["sizes"].get(name)
        if before is None or before["scale"] != size["scale"]:
            lines.append(f"- {name}: no comparable baseline size")
            continue
        for key in ("per_identity", "per_claim"):
            parts = []
            for category in SHOWN:
                old = before[key]["categories"][category]
                new = size[key]["categories"][category]
                ratio = "-" if not old else f"{new / old:.2f}"
                parts.append(f"{category} {ratio}")
            lines.append(f"- {name}, {key}: " + "; ".join(parts))
    return "\n".join(lines) + "\n"


def write_baseline(path: Path) -> None:
    """Measure the fixed regression scenario and record its operations'
    request and write counts at `path`."""
    from tests.fixtures.assignment_cohort import (
        REGRESSION,
        measure,
        operation_counts,
    )

    with tempfile.TemporaryDirectory(prefix="assignment-cost-") as tmp:
        isolate(Path(tmp))
        found = measure(REGRESSION, Path(tmp) / "run")
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "scale": asdict(REGRESSION),
                "operations": operation_counts(found),
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )


def main(argv=None) -> int:
    from tests.fixtures.assignment_cohort import SIZES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size",
        action="append",
        choices=sorted(SIZES),
        help="a size to measure; repeat for more (default: every size)",
    )
    parser.add_argument("--output", type=Path, help="new JSON report path")
    parser.add_argument(
        "--baseline", type=Path, help="an earlier report to compare with"
    )
    parser.add_argument(
        "--write-baseline",
        nargs="?",
        type=Path,
        const=BASELINE,
        metavar="PATH",
        help="record the regression scenario's counts (default: the "
        "checked-in baseline) and measure nothing else",
    )
    args = parser.parse_args(argv)
    try:
        if args.write_baseline is not None:
            write_baseline(args.write_baseline)
            print(f"Regression baseline written: {args.write_baseline}")
            return 0
        if args.output is None:
            parser.error("--output is required")
        if args.output.exists():
            raise ValueError(
                "--output must be a new file; reports are not overwritten"
            )
        baseline = (
            json.loads(args.baseline.read_text()) if args.baseline else None
        )
        names = args.size or list(SIZES)
        report = measurement(names)
        with args.output.open("x") as output:
            output.write(json.dumps(report, indent=1) + "\n")
        print(summary(report))
        if baseline is not None:
            print(compare(baseline, report))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Measurement could not complete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
