"""Offline tests for the order of scratch runs (#437): a new run's id
always sorts after every run already in its runs directory, whatever the
clock reads, so `runs_in`, `--resume latest` and the command's choice of
run never depend on two runs starting in different clock ticks.

Every clock here is a fake that reads what the test sets; nothing reads
the real clock or waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.real_time import real_time_uses
from yizkr.pipeline import scratch
from yizkr.pipeline.rundir import RunDir

START = datetime(2026, 9, 28, 9, 0, 0, 170, tzinfo=UTC)


class Clock:
    """A clock that reads `now`, which only the test moves."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now


def start(runs: Path, clock: Clock, mode: str = scratch.MODE) -> Path:
    """A new run in `runs`, with its run.json, as the driver starts one."""
    run = runs / scratch.new_run_id(runs, clock=clock)
    RunDir(run).update_manifest(run_id=run.name, mode=mode)
    return run


@pytest.fixture
def runs(tmp_path) -> Path:
    path = tmp_path / "runs"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


class TestNewRunIds:
    def test_the_id_is_the_time_the_clock_reads(self, runs, tmp_path):
        clock = Clock()
        assert scratch.new_run_id(runs, clock=clock) == (
            "20260928T090000Z-0000aa"
        )
        assert clock.reads == 1
        # a runs directory not made yet holds no run
        assert scratch.new_run_id(tmp_path / "none", clock=clock) == (
            "20260928T090000Z-0000aa"
        )

    def test_a_clock_in_another_zone_gives_the_utc_time(self, runs):
        zone = timezone(timedelta(hours=2))
        clock = Clock(datetime(2026, 9, 28, 11, 0, 0, 170, tzinfo=zone))
        assert scratch.new_run_id(runs, clock=clock) == (
            "20260928T090000Z-0000aa"
        )

    def test_a_second_run_in_the_same_tick_sorts_after_the_first(self, runs):
        clock = Clock()
        first = start(runs, clock)
        second = start(runs, clock)
        assert first.name == "20260928T090000Z-0000aa"
        assert second.name == "20260928T090000Z-0000ab"
        assert scratch.runs_in(runs) == [first, second]

    def test_a_clock_set_back_still_gives_a_later_id(self, runs):
        clock = Clock()
        first = start(runs, clock)
        clock.now = START - timedelta(hours=1)
        second = start(runs, clock)
        assert second.name == "20260928T090000Z-0000ab"
        assert scratch.runs_in(runs) == [first, second]
        assert scratch.resolve_run(runs, scratch.LATEST) == second
        # the clock moving on past the latest id takes its own time again
        clock.now = START + timedelta(seconds=5)
        assert start(runs, clock).name == "20260928T090005Z-0000aa"

    def test_ids_sort_as_the_runs_were_started(self, runs):
        clock = Clock()
        started = []
        for step in (0, 0, -3600, 1, -1, 0, 86400, -86400 * 30):
            clock.now += timedelta(seconds=step)
            started.append(start(runs, clock))
        assert scratch.runs_in(runs) == started
        assert len({run.name for run in started}) == len(started)
        assert scratch.resolve_run(runs) == started[-1]

    def test_the_last_microsecond_of_a_second_moves_to_the_next(self, runs):
        (runs / "20260928T090059Z-0f423f").mkdir()
        clock = Clock()
        assert scratch.new_run_id(runs, clock=clock) == (
            "20260928T090100Z-000000"
        )
        # an id whose hex digits are past any microsecond, as the ids of
        # runs RunDir names itself are
        (runs / "20260928T090100Z-ffffff").mkdir()
        assert scratch.new_run_id(runs, clock=clock) == (
            "20260928T090101Z-000000"
        )

    def test_a_reserved_directory_without_its_manifest_counts(self, runs):
        """A run's directory made before its run.json, or any entry named
        like a run id, is not taken again or sorted before."""
        (runs / "20260928T090000Z-0000aa").mkdir()
        (runs / "20260928T090000Z-0000ab").write_text("")
        assert scratch.runs_in(runs) == []
        assert scratch.new_run_id(runs, clock=Clock()) == (
            "20260928T090000Z-0000ac"
        )

    def test_other_names_in_the_runs_directory_are_not_run_ids(self, runs):
        for name in (
            "notes",
            ".hidden-29991231T235959Z-0000aa",
            "29991231T235959Z-0000aa-copy",
            "29991231T235959Z-0000AA",
            "29991399T235959Z-0000aa",  # no such date
            scratch.LOCK_FILE,
        ):
            (runs / name).mkdir()
        (runs / "foreign").mkdir()
        RunDir(runs / "foreign").update_manifest(run_id="x", mode="ingest")
        assert scratch.new_run_id(runs, clock=Clock()) == (
            "20260928T090000Z-0000aa"
        )


class TestTheCommand:
    def test_a_new_run_is_chosen_after_the_latest_one(self, runs):
        clock = Clock()
        first = start(runs, clock)
        RunDir(first).update_manifest(scratch={"complete": True})
        clock.now = START - timedelta(days=1)
        chosen = scratch.choose_run(runs, clock=clock)
        assert chosen == runs / "20260928T090000Z-0000ab"
        assert scratch.choose_run(runs, new=True, clock=clock) == chosen
        assert not chosen.exists()  # chosen, not made: the driver makes it
        clock.now = START + timedelta(days=1)
        assert scratch.choose_run(runs, clock=clock).name == (
            "20260929T090000Z-0000aa"
        )

    def test_an_unfinished_latest_run_is_resumed_whatever_the_clock(
        self, runs
    ):
        clock = Clock()
        first = start(runs, clock)
        clock.now = START - timedelta(days=1)
        second = start(runs, clock)
        assert scratch.choose_run(runs, clock=clock) == second
        assert scratch.choose_run(runs, resume=scratch.LATEST) == second
        assert scratch.choose_run(runs, resume=first.name) == first

    def test_the_command_reads_the_drivers_clock(self, runs, tmp_path):
        clock = Clock()
        taken = []

        class Driver:
            def __init__(self, folder, run_dir, **options) -> None:
                taken.append((run_dir, options))

            def run(self) -> int:
                RunDir(taken[-1][0]).update_manifest(
                    run_id=taken[-1][0].name,
                    mode=scratch.MODE,
                    scratch={"complete": True},
                )
                return 0

        for _ in range(2):
            assert (
                scratch.scratch_run(
                    tmp_path / "corpus", runs=runs, driver=Driver, clock=clock
                )
                == 0
            )
            clock.now -= timedelta(hours=1)
        names = [run_dir.name for run_dir, _options in taken]
        assert names == [
            "20260928T090000Z-0000aa",
            "20260928T090000Z-0000ab",
        ]
        assert all(options["clock"] is clock for _run, options in taken)
        assert scratch.runs_in(runs) == [run_dir for run_dir, _ in taken]


def test_no_test_here_waits_on_or_reads_the_real_clock():
    """No test in this file sleeps, reads a real clock or waits (#437):
    every run id is read from a fake Clock."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert real_time_uses(source, hang_guards={"HANG_GUARD"}) == []
