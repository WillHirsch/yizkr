"""The live monitor's stage counts beside rundir.run_state's, for the tests
of the monitor and of the scratch driver: both readers tell a stage cut
short by one rule (rundir.interrupted), so their counts agree on the same
files (#300). Not a test module: pytest collects nothing here."""

from __future__ import annotations

from pathlib import Path

from yizkr.pipeline import monitor
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    INTERRUPTED,
    NEEDS_ATTENTION,
    PENDING,
    RUNNING,
    RunDir,
    run_state,
    summarise,
)


def disagreement(run: Path, summary=None) -> str | None:
    """How the monitor's counts differ from rundir.run_state's for the
    same files (the monitor's split recombined: done and reused are
    done, and the interrupted, which the monitor counts among the
    pending, are interrupted; a stage named as the monitor shows it), or
    None. `summary` is the monitor's, read afresh when not given: the
    time it is read at changes no count."""
    summary = summary or monitor.summarise_run(run)
    expected = {
        monitor._token(stage): counts
        for stage, counts in summarise(run_state(RunDir(run))).items()
    }
    seen = {}
    for counts in summary.stages:
        row = {
            PENDING: counts.pending - counts.interrupted - counts.cancelled,
            RUNNING: counts.running,
            INTERRUPTED: counts.interrupted,
            "cancelled": counts.cancelled,
            DONE: counts.done + sum(counts.reused.values()),
            NEEDS_ATTENTION: counts.attention,
            FAILED: counts.failed,
        }
        row = {status: count for status, count in row.items() if count}
        if row:
            seen[counts.name] = row
    return None if seen == expected else f"{seen} != {expected}"
