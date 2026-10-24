"""Offline tests for the unit tests' own defaults (tests/unit/conftest.py,
#254): one BLAS thread unless the environment gives a thread count, sync
calls that flush nothing unless a test is marked `real_fsync`, a
record of the instance of each test's own (#199), no settings from the
host (#422), and no network (#618).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import posix
import socket
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

    def test_the_connection_variables_are_what_config_load_reads(
        self, tmp_path, monkeypatch
    ):
        """CONNECTION_VARIABLES is every key Config.load looks up, and
        nothing else: the keys are taken from a load that records them."""
        from yizkr.core import config

        asked: set[str] = set()

        class Recording(dict):
            def get(self, key, default=None):
                asked.add(key)
                return super().get(key, default)

        monkeypatch.setattr(config, "read_env_file", lambda path: Recording())
        with pytest.raises(config.ConfigError):
            config.Config.load(tmp_path / "stack.env", require_admin=False)
        assert asked == set(conftest.CONNECTION_VARIABLES)

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

    @pytest.mark.parametrize("named", ["by default", "in $YIZKR_ENV_FILE"])
    def test_the_list_readers_pass_on_a_host_with_a_never_ingest_list(
        self, tmp_path, never_ingest, named
    ):
        """Run from a folder whose deploy/.env names a never-ingest list
        with a pattern entry, found by default or named by an exported
        $YIZKR_ENV_FILE, and with the list's variable exported, the
        tests that ingest without a media file still pass: they read no
        list, and no fixture of a module sees the host's settings."""
        host_files(tmp_path, never_ingest)
        environ = {
            name: value
            for name, value in os.environ.items()
            if name != "YIZKR_ENV_FILE"
        }
        if named == "in $YIZKR_ENV_FILE":
            environ["YIZKR_ENV_FILE"] = str(tmp_path / "deploy" / ".env")
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


class TestNoNetwork:
    @pytest.mark.parametrize(
        "host",
        [None, "", "localhost", "LOCALHOST.", "status.localhost", b"localhost"]
        + ["127.0.0.1", "127.8.0.1", "::1", "[::1]", "0.0.0.0", "::"],
    )
    def test_a_lookup_of_this_machine_is_local(self, host):
        assert conftest.is_local_host(host)

    @pytest.mark.parametrize(
        "host",
        ["wiki.invalid", "wiki.example", "localhost.example", "192.0.2.1"]
        + ["2001:db8::1", "[2001:db8::1]", b"query.invalid"],
    )
    def test_a_lookup_of_another_host_is_not(self, host):
        assert not conftest.is_local_host(host)

    @pytest.mark.parametrize(
        "family, address, local",
        [
            (socket.AF_UNIX, "/tmp/cue.sock", True),
            (socket.AF_UNIX, b"\0abstract", True),
            (socket.AF_INET, ("127.0.0.1", 8080), True),
            (socket.AF_INET, ("0.0.0.0", 8080), True),
            (socket.AF_INET6, ("::1", 8080, 0, 0), True),
            (socket.AF_INET6, ("fe80::1%lo", 8080, 0, 0), False),
            (socket.AF_INET, ("192.0.2.1", 80), False),
            (socket.AF_INET6, ("2001:db8::1", 80, 0, 0), False),
            # connect resolves a name itself, past the guarded lookups
            (socket.AF_INET, ("localhost", 80), False),
            (socket.AF_INET, ("wiki.invalid", 80), False),
        ],
    )
    def test_a_connect_is_local_on_loopback_alone(
        self, family, address, local
    ):
        assert conftest.is_local_address(family, address) is local

    def test_a_request_off_this_machine_is_refused_naming_the_test(
        self, request, _no_network
    ):
        """Refused at the lookup, as NetworkRefused, which is no OSError,
        so requests does not take it for a failed connection."""
        import requests

        with pytest.raises(conftest.NetworkRefused) as refused:
            requests.get("http://wiki.invalid/w/api.php", timeout=5)
        assert not isinstance(refused.value, OSError)
        assert request.node.nodeid in str(refused.value)
        assert "getaddrinfo('wiki.invalid'" in str(refused.value)
        assert "stand-in api_get" in str(refused.value)
        assert _no_network.refused == ["socket.getaddrinfo('wiki.invalid')"]
        _no_network.refused.clear()

    @pytest.mark.parametrize("call", ["connect", "connect_ex"])
    def test_a_connect_off_this_machine_is_refused(self, _no_network, call):
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
            pytest.raises(conftest.NetworkRefused, match="192.0.2.1"),
        ):
            getattr(sock, call)(("192.0.2.1", 80))
        with pytest.raises(conftest.NetworkRefused, match="gethostbyname"):
            socket.gethostbyname("wiki.invalid")
        with pytest.raises(conftest.NetworkRefused, match="hostbyname_ex"):
            socket.gethostbyname_ex("wiki.invalid")
        assert len(_no_network.refused) == 3
        _no_network.refused.clear()

    def test_a_wiki_client_s_read_is_refused(self, _no_network):
        """A WikiClient's api_get, as a unit fake that gives none makes
        it, reaches no wiki."""
        from yizkr.core.config import Config
        from yizkr.core.wiki import WikiClient

        client = WikiClient(
            Config(
                wikibase_url="https://wiki.invalid",
                concept_uri="https://wiki.invalid/entity/",
                admin_name="",
                admin_pass="",
                wdqs_url=None,
                sparql_url=None,
                env_file=None,
            )
        )
        with pytest.raises(conftest.NetworkRefused, match="wiki.invalid"):
            client.page_text("Yizkr:Vocabulary")
        assert _no_network.refused
        _no_network.refused.clear()

    def test_loopback_and_unix_sockets_go_through(self, tmp_path):
        with socket.create_server(("127.0.0.1", 0)) as server:
            host, port = server.getsockname()[:2]
            with socket.create_connection(("localhost", port), timeout=5):
                pass
            with socket.socket() as sock:
                assert sock.connect_ex((host, port)) == 0
        path = str(tmp_path / "cue.sock")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            listener.listen()
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(path)

    def test_a_caught_refusal_still_fails_the_test(self):
        """Code under test can catch the refusal (a fallback that catches
        every exception, say), so the guard fails the test at teardown
        for any refusal it recorded."""
        guard = conftest.NetworkGuard("tests/unit/test_a.py::test_b")
        guard.check()
        with contextlib.suppress(conftest.NetworkRefused):
            guard.refuse("socket.getaddrinfo('wiki.invalid')")
        with pytest.raises(
            pytest.fail.Exception,
            match=r"test_a.py::test_b asked for the network: "
            r"socket.getaddrinfo\('wiki.invalid'\)",
        ):
            guard.check()

    def test_no_proxy_is_left_to_send_a_request_through(self):
        """A proxy on loopback would pass the guard and reach the
        network, so the guard removes every proxy variable."""
        import urllib.request

        assert not set(conftest.PROXY_VARIABLES) & set(os.environ)
        with pytest.MonkeyPatch.context() as patch:
            for name in conftest.PROXY_VARIABLES:
                patch.setenv(name, "http://127.0.0.1:3128")
            conftest.NetworkGuard("tests/unit/test_a.py::test_b").install(
                patch
            )
            assert not set(conftest.PROXY_VARIABLES) & set(os.environ)
        # what requests and urllib look up for a request's scheme
        assert not {"http", "https", "all"} & set(urllib.request.getproxies())
