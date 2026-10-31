"""The fictional measurement exercises real reuse, not model quality."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts/experiments/conversation_reuse.py"
)
SPEC = importlib.util.spec_from_file_location("conversation_reuse", SCRIPT)
measurement = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = measurement
SPEC.loader.exec_module(measurement)


def test_scenarios_distinguish_restore_replay_and_inline_costs(tmp_path):
    report = measurement.measure(tmp_path)
    assert report["authored_results_equal"]
    assert len(report["scenarios"]) == 18
    for document in measurement.DOCUMENTS:
        rows = {
            (row["mode"], row["cache_state"]): row
            for row in report["scenarios"]
            if row["document"] == document
        }
        restore = rows["restore", "partial"]
        replay = rows["replay", "partial"]
        inline = rows["inline", "partial"]
        assert restore["session_restores"] == 1
        assert restore["calls"] == {"fork": restore["total_calls"]}
        assert replay["calls"]["open"] == 1
        assert replay["total_calls"] == restore["total_calls"] + 1
        assert inline["total_calls"] == restore["total_calls"]
        assert restore["synthetic_restore_copy_bytes"] > 0
        assert (
            restore["synthetic_restore_copy_bytes"]
            == restore["session_restore_payload_bytes"]
            == restore["persisted_session_bytes_before"]
        )
        assert (
            restore["logical_context_utf8_bytes"]
            > (restore["submitted_prompt_utf8_bytes"])
        )
        assert (
            inline["logical_context_utf8_bytes"]
            == (inline["submitted_prompt_utf8_bytes"])
        )
        assert rows["restore", "cold"]["session_restores"] == 0
        for mode in measurement.MODES:
            full = rows[mode, "full"]
            assert full["total_calls"] == 0
            assert full["session_restores"] == 0
            assert full["synthetic_restore_copy_bytes"] == 0
            assert full["claims"] > 0


def test_output_inside_repository_is_refused_before_measurement(monkeypatch):
    def forbidden(_root):
        pytest.fail("a refused output must not start the measurement")

    monkeypatch.setattr(measurement, "measure", forbidden)
    with pytest.raises(SystemExit) as caught:
        measurement.main(["--out", str(measurement.ROOT / "measurement")])
    assert caught.value.code == 2
