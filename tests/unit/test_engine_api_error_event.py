"""Offline tests for llm.is_api_error_event (#651): the one predicate by
which the claude-code engine tells an assistant event that stands for an
API error (one with an `error` string or marked is_api_error_message)
from the model's own words. unsupported_model reads an unsupported-model
account only from such an event, classifier_refusal a refusal frame only
from one, and _assemble_stream keeps such an event's text out of the
answer; each agrees with the predicate on every shape of the marks.
Documents come from the fictional universe (docs/EXAMPLES.md)."""

from __future__ import annotations

import pytest

from tests.unit.test_engine_limits import INIT, stream
from yizkr.pipeline.llm import (
    BAD_REQUEST,
    REFUSAL_STOP,
    ClaudeCodeClient,
    classifier_refusal,
    is_api_error_event,
    unsupported_model,
)

UNSUPPORTED = "Claude Code requires a newer version to use this model."
ANSWER = "The letter from Flörburg is dated 1921."

# (marks, whether they make an assistant event an API-error one)
MARKS = [
    ({"error": "invalid_request", "is_api_error_message": True}, True),
    ({"error": "invalid_request"}, True),
    ({"error": ""}, True),
    ({"is_api_error_message": True}, True),
    ({"error": None, "is_api_error_message": True}, True),
    ({}, False),
    ({"error": None}, False),
    ({"error": True}, False),
    ({"error": {"type": "invalid_request"}}, False),
    ({"is_api_error_message": 1}, False),
    ({"is_api_error_message": "true"}, False),
    ({"is_api_error_message": False}, False),
    ({"error": None, "is_api_error_message": False}, False),
]
IDS = [repr(marks) for marks, _ in MARKS]


def assistant(text: str, marks: dict, **message) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-opus-5-5",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            **message,
        },
        "session_id": "s1",
        **marks,
    }


class TestPredicate:
    @pytest.mark.parametrize(("marks", "expected"), MARKS, ids=IDS)
    def test_an_assistant_event_by_its_marks(self, marks, expected):
        assert is_api_error_event(assistant("notice", marks)) is expected

    @pytest.mark.parametrize("kind", ["user", "result", "system", None])
    def test_only_an_assistant_event_is_one(self, kind):
        marks = {"error": "invalid_request", "is_api_error_message": True}
        event = assistant("notice", marks)
        event["type"] = kind
        assert is_api_error_event(event) is False

    def test_an_empty_event_is_none(self):
        assert is_api_error_event({}) is False


class TestCallersAgree:
    """Each of the three readers treats an event as an API-error one
    exactly when the predicate does."""

    @pytest.mark.parametrize(("marks", "expected"), MARKS, ids=IDS)
    def test_unsupported_model(self, marks, expected):
        events = [
            INIT,
            assistant(UNSUPPORTED, marks),
            {
                "type": "result",
                "is_error": True,
                "api_error_status": BAD_REQUEST,
                "result": "API Error",
                "session_id": "s1",
            },
        ]
        assert (unsupported_model(events, 1) is not None) is expected

    @pytest.mark.parametrize(("marks", "expected"), MARKS, ids=IDS)
    def test_classifier_refusal(self, marks, expected):
        events = [
            INIT,
            assistant("notice", marks, stop_reason=REFUSAL_STOP),
            {"type": "result", "is_error": True, "session_id": "s1"},
        ]
        assert (classifier_refusal(events, 1) is not None) is expected

    @pytest.mark.parametrize(("marks", "expected"), MARKS, ids=IDS)
    def test_assemble_stream(self, marks, expected):
        events = [INIT, assistant("notice ", marks), assistant(ANSWER, {})]
        assembled = ClaudeCodeClient._assemble_stream(stream(events))
        assert assembled == (ANSWER if expected else "notice " + ANSWER)

    @pytest.mark.parametrize("message", [None, "API Error"])
    def test_an_api_error_event_with_no_message_dict_is_skipped(self, message):
        """An API-error event whose message is not an object carries no
        text or stop reason to read: both readers pass over it."""
        event = {
            "type": "assistant",
            "message": message,
            "session_id": "s1",
            "error": "invalid_request",
            "is_api_error_message": True,
        }
        events = [
            INIT,
            event,
            {
                "type": "result",
                "is_error": True,
                "api_error_status": BAD_REQUEST,
                "result": "API Error",
                "session_id": "s1",
            },
        ]
        assert unsupported_model(events, 1) is None
        assert classifier_refusal(events, 1) is None
