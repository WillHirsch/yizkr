"""Append-only logs keep records whose strings hold line separators other
than a newline: the run directory writes them raw. A line that is not
valid UTF-8 is skipped alone (#271)."""

from __future__ import annotations

import pytest

from yizkr.pipeline.rundir import RunDir
from yizkr.pipeline.stages import read_jsonl

SEPARATORS = ["\u2028", "\u2029", "\u0085"]


@pytest.mark.parametrize("separator", SEPARATORS)
def test_an_event_holding_a_line_separator_is_read_back_whole(
    tmp_path, separator
):
    run = RunDir(tmp_path / "run")
    run.emit("alert", note=f"Klara Löbig{separator}Flörburg")
    run.emit("document-done")
    events = run.events()
    assert [event["event"] for event in events] == ["alert", "document-done"]
    assert events[0]["note"] == f"Klara Löbig{separator}Flörburg"


def test_a_pause_whose_reason_holds_a_line_separator_still_pauses(tmp_path):
    run = RunDir(tmp_path / "run")
    run.request("pause", reason="checking the postcard\u2028of 1938")
    assert run.control().scopes == {"run": "paused"}


def test_a_torn_last_line_is_still_skipped(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text('{"event": "a"}\n{"event": "b"}\n{"event": ', "utf-8")
    assert read_jsonl(log) == [{"event": "a"}, {"event": "b"}]


def test_a_crlf_log_is_read(tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_bytes(b'{"event": "a"}\r\n{"event": "b"}\r\n')
    assert read_jsonl(log) == [{"event": "a"}, {"event": "b"}]


def test_a_whole_record_that_is_not_utf8_is_skipped_alone(tmp_path):
    """A complete line in the middle of a log that holds a byte UTF-8
    does not allow (Latin-1's "ö") is skipped, never read with the byte
    replaced, and the records around it are kept."""
    log = tmp_path / "events.jsonl"
    log.write_bytes(
        b'{"event": "a"}\n'
        b'{"event": "b", "note": "Klara L\xf6big"}\n'
        b'{"event": "c"}\n'
    )
    assert read_jsonl(log) == [{"event": "a"}, {"event": "c"}]
