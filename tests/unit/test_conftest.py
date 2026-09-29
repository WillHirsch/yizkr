"""Offline tests for the unit tests' own defaults (tests/unit/conftest.py,
#254): one BLAS thread unless the environment gives a thread count, sync
calls that flush nothing unless a test is marked `real_fsync`, a
record of the instance of each test's own (#199), and no settings from
the host (#422).
"""

from __future__ import annotations

import errno
import hashlib
import os
import posix
import subprocess
import sys
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


@pytest.fixture(scope="module")
def module_record_root() -> Path:
    """The record of the instance a module's fixture sees: it sets up
    before any test's own default."""
    from yizkr.core.instance import state_root

    return state_root()


class TestInstanceRecord:
    def test_a_modules_fixtures_keep_the_sessions_record(
        self, module_record_root, tmp_path_factory
    ):
        """As test_live_lock.py's child sessions do, which it starts once
        for its module."""
        basetemp = tmp_path_factory.getbasetemp()
        assert module_record_root == basetemp / "instance-records" / "session"

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


REPO = Path(__file__).resolve().parents[2]

# The two tests that read a never-ingest list from the host's deploy/.env
# before the unit tests were kept from the host's settings (#422).
LIST_READERS = [
    "tests/unit/test_llmcache.py::TestTheSingleCallExtractor::"
    "test_ingest_extract_calls_through_its_cache",
    "tests/unit/test_llmcache.py::TestTheSingleCallExtractor::"
    "test_ingest_extract_refuses_a_cache_git_would_track",
]
# and what a module's fixture sees, which sets up before any test's own
MODULE_CHECK = (
    "tests/unit/test_conftest.py::TestHostSettings::"
    "test_no_test_or_fixture_of_a_module_sees_a_host_setting"
)


def host_files(root: Path, never_ingest: Path) -> None:
    """A deploy/.env and a .env in `root`, as a developer's machine has,
    naming the never-ingest list, a corpus folder and a wiki."""
    (root / "deploy").mkdir()
    settings = (
        f"YIZKR_EXCLUDE_FILE={never_ingest}\n"
        f"YIZKR_CORPUS_DIR={root / 'corpus'}\n"
        "WIKIBASE_URL=http://wiki.example\n"
    )
    for env in (root / "deploy" / ".env", root / ".env"):
        env.write_text(settings)


@pytest.fixture
def never_ingest(tmp_path) -> Path:
    """A never-ingest list with a pattern entry."""
    path = tmp_path / "never-ingest.txt"
    path.write_text("postcard-*.png\n")
    return path


@pytest.fixture(scope="module")
def module_host_settings() -> list[str]:
    """The host's settings a module's fixture sees."""
    return conftest.host_settings(os.environ)


class TestHostSettings:
    def test_the_host_settings_are_the_yizkr_and_connection_variables(self):
        environ = {
            "YIZKR_EXCLUDE_FILE": "never.txt",
            "YIZKR_ENV_FILE": "deploy/.env",
            "YIZKR_INSTANCE_STATE": "records",
            "MW_ADMIN_PASS": "not-a-password",
            "WIKIBASE_URL": "http://wiki.example",
            "PATH": "/usr/bin",
            "OPENBLAS_NUM_THREADS": "1",
        }
        assert conftest.host_settings(environ) == [
            "MW_ADMIN_PASS",
            "WIKIBASE_URL",
            "YIZKR_ENV_FILE",
            "YIZKR_EXCLUDE_FILE",
        ]

    def test_no_test_or_fixture_of_a_module_sees_a_host_setting(
        self, module_host_settings
    ):
        assert module_host_settings == []
        assert conftest.host_settings(os.environ) == []

    def test_no_default_env_file_is_read(
        self, tmp_path, monkeypatch, never_ingest
    ):
        """A deploy/.env and a .env in the working directory, as the
        repository root holds on a developer's machine, are not found:
        what reads settings sees none, as on CI."""
        from yizkr.core.config import (
            Config,
            ConfigError,
            env_setting,
            resolve_env_file,
        )
        from yizkr.core.corpus import corpus_dir, load_exclusions

        host_files(tmp_path, never_ingest)
        monkeypatch.chdir(tmp_path)
        assert resolve_env_file() is None
        assert env_setting("YIZKR_EXCLUDE_FILE") == ""
        assert load_exclusions(required=False) is None
        assert corpus_dir() is None
        with pytest.raises(ConfigError, match="WIKIBASE_URL"):
            Config.load(require_admin=False)

    def test_a_setting_a_test_gives_is_read(
        self, tmp_path, monkeypatch, never_ingest
    ):
        """In an env file the test names, or in the environment."""
        from yizkr.core.config import env_setting
        from yizkr.core.corpus import corpus_dir, load_exclusions

        host_files(tmp_path, never_ingest)
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "deploy/.env"))
        assert load_exclusions(required=True).patterns == ("postcard-*.png",)
        assert corpus_dir() == tmp_path / "corpus"
        monkeypatch.setenv("YIZKR_CORPUS_DIR", str(tmp_path / "other"))
        assert corpus_dir() == tmp_path / "other"
        assert env_setting("YIZKR_CORPUS_DIR") == str(tmp_path / "other")

    def test_the_list_readers_pass_on_a_host_with_a_never_ingest_list(
        self, tmp_path, never_ingest
    ):
        """Run from a folder whose deploy/.env names a never-ingest list
        with a pattern entry, and with the list's variable exported, the
        tests that ingest without a media file still pass: they read no
        list, and no fixture of a module sees the host's settings."""
        host_files(tmp_path, never_ingest)
        environ = {
            name: value
            for name, value in os.environ.items()
            if name != "YIZKR_ENV_FILE"
        }
        environ["YIZKR_EXCLUDE_FILE"] = str(never_ingest)
        environ["MW_ADMIN_PASS"] = "not-a-password"
        environ["PYTHONPATH"] = str(REPO)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(tmp_path / "basetemp"),
                *(str(REPO / test) for test in [*LIST_READERS, MODULE_CHECK]),
            ],
            cwd=tmp_path,
            env=environ,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
