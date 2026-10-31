"""Execution owners stop all clients before releasing session resources."""

import pytest

from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.cancellation import RunCancelled
from yizkr.pipeline.estimate import Estimator
from yizkr.pipeline.orchestrate import Orchestrator


class Resource:
    def __init__(self, name, closed, failure=None):
        self.name, self.closed, self.failure = name, closed, failure

    def close(self):
        self.closed.append(self.name)
        if self.failure is not None:
            raise self.failure


@pytest.fixture(params=[Orchestrator, Estimator])
def execution_owner(request, tmp_path):
    owner = request.param(
        tmp_path / "sources", tmp_path / "run", exclusions=ExclusionList()
    )

    def install(clients, sessions):
        if isinstance(owner, Estimator):
            owner._clients, owner._as_is = clients, sessions
            return owner._close_clients
        owner._engines, owner._cache = clients, sessions
        return owner._close_engines

    return install


def test_failed_client_does_not_skip_others_or_release_sessions(
    execution_owner,
):
    closed = []
    failure = OSError("fictional client still active")
    first = Resource("first", closed, failure)
    second = Resource("second", closed)
    close = execution_owner(
        {"transcribe": first, "extract": second}, Resource("sessions", closed)
    )
    with pytest.raises(OSError) as caught:
        close()
    assert caught.value is failure
    assert closed == ["first", "second"]
    # The owner retains the failed client and service, so cleanup can finish
    # without closing the already-stopped second client again.
    first.failure = None
    close()
    assert closed == ["first", "second", "first", "sessions"]


@pytest.mark.parametrize("failing", ["client", "sessions"])
def test_cleanup_preserves_the_primary_terminal_cause(
    execution_owner, failing
):
    closed = []
    failure = OSError("fictional cleanup detail")
    close = execution_owner(
        {
            "extract": Resource(
                "first", closed, failure if failing == "client" else None
            ),
            "critique": Resource("second", closed),
        },
        Resource(
            "sessions", closed, failure if failing == "sessions" else None
        ),
    )
    primary = RunCancelled("operator cancelled")
    with pytest.raises(RunCancelled) as caught:
        try:
            raise primary
        finally:
            close()
    assert caught.value is primary
    assert "OSError" in primary.__notes__[0]
    assert "fictional cleanup detail" not in primary.__notes__[0]
    assert closed == ["first", "second"] + (
        ["sessions"] if failing == "sessions" else []
    )


def test_session_cleanup_failure_is_reported_after_clients_stop(
    execution_owner,
):
    closed = []
    failure = OSError("fictional session cleanup failure")
    close = execution_owner(
        {"extract": Resource("engine", closed)},
        Resource("sessions", closed, failure),
    )
    with pytest.raises(OSError) as caught:
        close()
    assert caught.value is failure
    assert closed == ["engine", "sessions"]
