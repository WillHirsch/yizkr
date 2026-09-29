"""Offline tests for the unit tests' own defaults (tests/unit/conftest.py,
#254): one BLAS thread unless the environment gives a thread count, sync
calls that flush nothing unless a test is marked `real_fsync`, and a
record of the instance of each test's own (#199).
"""

from __future__ import annotations

import errno
import hashlib
import os
import posix
import warnings
from pathlib import Path

import pytest

from tests.unit import conftest


class TestBlasThreads:
    @pytest.mark.parametrize(
        "environ",
        [
            {},
            {"PATH": "/usr/bin"},
            {"OPENBLAS_NUM_THREADS": ""},
            {"OMP_NUM_THREADS": "", "MKL_NUM_THREADS": ""},
        ],
        ids=["nothing", "no-count", "empty", "all-empty"],
    )
    def test_one_thread_unless_the_environment_gives_a_count(self, environ):
        """An empty variable gives no count: OpenBLAS reads it as unset."""
        given = dict(environ)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # numpy is not loaded: no warning
            conftest.default_blas_threads(environ, modules={"yaml"})
        assert environ == given | {"OPENBLAS_NUM_THREADS": "1"}

    @pytest.mark.parametrize(
        "name", ["OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"]
    )
    def test_a_count_in_the_environment_is_kept(self, name):
        """Kept whether or not numpy has loaded, and with no warning: the
        count was in the environment when numpy loaded."""
        for modules in ({"yaml"}, {"numpy", "yaml"}):
            environ = {name: "4"}
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                conftest.default_blas_threads(environ, modules)
            assert environ == {name: "4"}

    def test_a_default_set_after_numpy_has_loaded_warns(self):
        """OpenBLAS read the variable as numpy loaded, so the default is
        too late for this process, and the warning says what to do."""
        environ: dict[str, str] = {}
        with pytest.warns(UserWarning, match="numpy was loaded") as caught:
            conftest.default_blas_threads(environ, modules={"numpy"})
        assert environ == {"OPENBLAS_NUM_THREADS": "1"}
        assert "in the environment" in str(caught[0].message)

    def test_this_process_runs_with_a_thread_count(self):
        """The conftest applies the default to this process's environment
        as it is imported."""
        assert any(
            os.environ.get(name)
            for name in (
                "OPENBLAS_NUM_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
            )
        )


def sync_calls() -> list:
    """The calls that take a descriptor: os.fsync, and os.fdatasync where
    the platform has it."""
    return [os.fsync] + ([os.fdatasync] if hasattr(os, "fdatasync") else [])


@pytest.fixture
def pipe():
    """The write end of a pipe: open, but no call can sync it."""
    read, write = os.pipe()
    yield write
    os.close(read)
    os.close(write)


class TestSyncCalls:
    """The stand-ins for the sync calls, and the marker that keeps the
    real ones."""

    def test_unit_tests_skip_the_sync_calls(self, pipe):
        # the real calls refuse a pipe (see the marker's test); a stand-in
        # flushes nothing, so it takes any open descriptor
        for call in sync_calls():
            assert call(pipe) is None
        assert os.sync is not posix.sync

    def test_the_stand_in_for_os_sync_takes_no_argument(self, pipe):
        """As the real call takes none."""
        assert os.sync() is None
        with pytest.raises(TypeError):
            os.sync(pipe)

    def test_a_descriptor_that_is_not_open_still_fails(self):
        read, write = os.pipe()
        os.close(read)
        os.close(write)
        for call in sync_calls():
            with pytest.raises(OSError) as refused:
                call(write)
            assert refused.value.errno == errno.EBADF

    def test_a_file_object_is_taken_as_the_real_calls_take_it(self, tmp_path):
        """By its fileno() method, which fails once the file is
        closed."""
        with open(tmp_path / "entry.json", "w") as handle:
            for call in sync_calls():
                assert call(handle) is None
        for call in sync_calls():
            with pytest.raises(ValueError, match="closed file"):
                call(handle)

    @pytest.mark.real_fsync
    def test_the_marker_keeps_the_real_calls(self, pipe):
        for call in sync_calls():
            with pytest.raises(OSError) as refused:
                call(pipe)
            assert refused.value.errno == errno.EINVAL
        assert os.sync is posix.sync


class TestInstanceRecord:
    def test_each_test_keeps_its_own_record_of_the_instance(
        self, request, tmp_path_factory
    ):
        """In the session's temporary directory, apart from the user's
        own record, named for the test, and not made until written."""
        from yizkr.core.instance import state_root

        root = state_root()
        assert root.is_relative_to(tmp_path_factory.getbasetemp())
        assert not root.is_relative_to(Path.home() / ".local")
        assert (
            root.name
            == (hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:16])
        )
        assert not root.exists()
