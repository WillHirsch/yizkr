"""Execution owners stop all clients before releasing session resources."""

import pytest

from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.cancellation import RunCancelled
from yizkr.pipeline.estimate import Estimator
from yizkr.pipeline.scratch import ScratchDriver


class Resource:
    def __init__(self, name, closed, failure=None):
        self.name, self.closed, self.failure = name, closed, failure

    def close(self):
        self.closed.append(self.name)
        if self.failure is not None:
            raise self.failure


@pytest.fixture(params=[ScratchDriver, Estimator])
def execution_owner(request, tmp_path):
    owner = request.param(
        tmp_path / "sources", tmp_path / "run", exclusions=ExclusionList()
    )

    def install(clients, sessions):
        if isinstance(owner, Estimator):
            owner._clients, owner._as_is = clients, sessions
            return owner._close_clients
        owner.resources.engines, owner.resources.cache = clients, sessions
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


def test_rejected_engine_cleanup_preserves_settings_error_and_retries(
    tmp_path, monkeypatch
):
    from yizkr.pipeline import llm
    from yizkr.pipeline.run_resources import RunResources

    closed = []
    rejected = Resource("rejected", closed, OSError("private detail"))
    accepted = Resource("accepted", closed)
    clients = iter([rejected, accepted])
    monkeypatch.setattr(llm, "make_client", lambda *a, **k: next(clients))
    primary = ValueError("recorded settings differ")

    class Recording:
        calls = 0

        def record_engine(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise primary

    resources = RunResources(tmp_path / "cache")
    resources.cache = Resource("cache", closed)
    recording = Recording()
    with pytest.raises(ValueError) as caught:
        resources.engine(
            "extract", engine="fictional", effort="low", rundir=recording
        )
    assert caught.value is primary
    assert "OSError" in primary.__notes__[0]
    assert "private detail" not in primary.__notes__[0]
    assert (
        resources.engine(
            "extract", engine="fictional", effort="low", rundir=recording
        )
        is accepted
    )
    with pytest.raises(OSError):
        resources.close()
    assert closed == ["rejected", "accepted", "rejected"]
    rejected.failure = None
    resources.close()
    assert closed == ["rejected", "accepted", "rejected", "rejected", "cache"]
