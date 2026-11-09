"""Recovery and transport boundaries for enforced server mutation exclusion."""

from __future__ import annotations

import copy
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from yizkr.core.mutation_guard import (
    CAPABILITY_HEADER,
    PROTOCOL,
    MutationGuardError,
    protected_mutation,
)
from yizkr.core.wiki import WikiClient

HANG_GUARD = 30


class Server:
    def __init__(self):
        self.state = {
            "protocol": PROTOCOL,
            "instance": "a" * 32,
            "epoch": 0,
            "active": False,
            "operation_id": None,
        }
        self.capability = None
        self.calls = []
        self.lose = None

    def request(self, client, mode, operation):
        capability = client.login.get_session().headers.get(CAPABILITY_HEADER)
        self.calls.append(mode)
        if mode == "inspect":
            return copy.deepcopy(self.state)
        if mode == "begin" and not self.state["active"]:
            prefix = f"{self.state['instance']}.{self.state['epoch'] + 1}."
            if not capability or not capability.startswith(prefix):
                raise RuntimeError("stale capability")
            self.state.update(
                active=True,
                epoch=self.state["epoch"] + 1,
                operation_id=operation,
            )
            self.capability = capability
        elif capability != self.capability or not self.state["active"]:
            raise RuntimeError("not the owner")
        if mode == "end":
            if self.lose == "before-end":
                self.lose = None
                raise ConnectionError("fictional connection loss")
            self.state.update(active=False, operation_id=None)
        if self.lose == mode:
            self.lose = None
            raise ConnectionError("fictional response loss")
        return copy.deepcopy(self.state)


def client(server):
    wiki = WikiClient(SimpleNamespace(wikibase_url="http://fictional.invalid"))
    session = requests.Session()
    wiki._login = SimpleNamespace(
        get_session=lambda: session, get_edit_token=lambda: "fictional-csrf"
    )
    wiki._wbi = object()
    wiki.mutation_guard_request = lambda mode, operation: server.request(
        wiki, mode, operation
    )
    return wiki


def test_guard_carries_private_capability_on_read_and_wbi_write_session(
    tmp_path, monkeypatch
):
    server = Server()
    wiki = client(server)
    path = tmp_path / "guard.json"
    observed = []

    def get(url, **kwargs):
        if CAPABILITY_HEADER in kwargs["headers"]:
            assert kwargs["allow_redirects"] is False
        observed.append(kwargs["headers"])
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"query": {}}
        )

    monkeypatch.setattr(requests, "get", get)
    with protected_mutation(
        wiki, state_path=path, operation_id="fictional"
    ) as guard:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        saved = json.loads(path.read_text())
        guard.assert_active()
        wiki.api_get({"action": "query"})
        assert observed[0][CAPABILITY_HEADER] == saved["capability"]
        assert (
            wiki.login.get_session().headers[CAPABILITY_HEADER]
            == saved["capability"]
        )
        assert guard.evidence == {
            "protocol": PROTOCOL,
            "epoch": 1,
            "operation_id": "fictional",
        }
        assert saved["capability"] not in repr(guard.evidence)
    assert not path.exists()
    assert not server.state["active"]
    assert CAPABILITY_HEADER not in wiki.login.get_session().headers
    assert wiki.login.get_session().max_redirects == 30
    wiki.api_get({"action": "query"})
    assert CAPABILITY_HEADER not in observed[-1]


def test_handled_refusal_releases_after_server_drain(tmp_path):
    server = Server()
    wiki = client(server)
    path = tmp_path / "guard.json"
    with (
        pytest.raises(ValueError, match="fictional refusal"),
        protected_mutation(wiki, state_path=path, operation_id="fictional"),
    ):
        raise ValueError("fictional refusal")
    assert server.calls == ["inspect", "begin", "end"]
    assert not server.state["active"] and not path.exists()


def test_capability_bearing_session_does_not_follow_redirects(tmp_path):
    wiki = client(Server())
    urls = []

    class Redirect(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            urls.append(request.url)
            response = requests.Response()
            response.status_code = 302
            response.url = request.url
            response.headers["Location"] = "http://other-fictional.invalid/"
            response._content = b""
            return response

        def close(self):
            pass

    wiki.login.get_session().mount("http://", Redirect())
    with (
        protected_mutation(
            wiki, state_path=tmp_path / "guard", operation_id="fictional"
        ),
        pytest.raises(requests.TooManyRedirects),
    ):
        wiki.login.get_session().post(wiki.api_url)
    assert urls == [wiki.api_url]


@pytest.mark.parametrize("lost", ["begin", "before-end", "end"])
def test_lost_control_response_retains_capability_and_recovers(tmp_path, lost):
    server = Server()
    path = tmp_path / "guard.json"
    server.lose = lost
    with (
        pytest.raises(MutationGuardError),
        protected_mutation(
            client(server), state_path=path, operation_id="fictional"
        ),
    ):
        pass
    assert path.exists()
    old_epoch = json.loads(path.read_text())["epoch"]
    with protected_mutation(
        client(server), state_path=path, operation_id="fictional"
    ) as guard:
        guard.assert_active()
        assert guard.epoch == old_epoch + (lost == "end")
    assert not path.exists() and not server.state["active"]


def test_an_independent_client_with_same_account_cannot_inherit_owner(
    tmp_path,
):
    server = Server()
    with protected_mutation(
        client(server), state_path=tmp_path / "first", operation_id="first"
    ):
        with (
            pytest.raises(MutationGuardError, match="another operation"),
            protected_mutation(
                client(server),
                state_path=tmp_path / "second",
                operation_id="second",
            ),
        ):
            pytest.fail("outsider acquired")
        assert server.state["active"]
    assert not (tmp_path / "second").exists()


def test_same_client_and_same_capability_file_refuse_concurrent_use(tmp_path):
    server = Server()
    wiki = client(server)
    path = tmp_path / "guard"
    with protected_mutation(wiki, state_path=path, operation_id="fictional"):
        with (
            pytest.raises(MutationGuardError, match="already owns"),
            protected_mutation(
                wiki, state_path=path, operation_id="fictional"
            ),
        ):
            pytest.fail("nested owner")
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(RuntimeError, match="across threads"),
        ):
            executor.submit(lambda: wiki.wbi).result(timeout=HANG_GUARD)
        with (
            pytest.raises(MutationGuardError, match="another process"),
            protected_mutation(
                client(server), state_path=path, operation_id="fictional"
            ),
        ):
            pytest.fail("shared recovery file")


@pytest.mark.parametrize(
    "mutation",
    ["permissions", "operation", "instance", "symlink", "invalid-json"],
)
def test_capability_file_is_private_and_bound_to_operation_and_instance(
    tmp_path, mutation
):
    server = Server()
    path = tmp_path / "guard"
    server.lose = "begin"
    with (
        pytest.raises(MutationGuardError),
        protected_mutation(
            client(server), state_path=path, operation_id="fictional"
        ),
    ):
        pass
    if mutation == "permissions":
        path.chmod(0o644)
    elif mutation == "symlink":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "invalid-json":
        path.write_text("{")
    else:
        saved = json.loads(path.read_text())
        saved["operation_id" if mutation == "operation" else "instance"] = (
            "different"
        )
        path.write_text(json.dumps(saved))
    with (
        pytest.raises((MutationGuardError, OSError)),
        protected_mutation(
            client(server), state_path=path, operation_id="fictional"
        ),
    ):
        pytest.fail("invalid recovery capability admitted")
    assert server.state["active"]


@pytest.mark.parametrize(
    "changed",
    [
        {"protocol": "unknown"},
        {"epoch": True},
        {"active": "yes"},
        {"instance": None},
    ],
)
def test_missing_or_malformed_server_protocol_refuses_before_authority(
    tmp_path, changed
):
    server = Server()
    server.state.update(changed)
    with (
        pytest.raises(MutationGuardError),
        protected_mutation(
            client(server),
            state_path=tmp_path / "guard",
            operation_id="fictional",
        ),
    ):
        pytest.fail("unsupported gate admitted")
    assert server.calls == ["inspect"]
    assert not (tmp_path / "guard").exists()


def test_preexisting_client_header_is_not_removed_when_context_refuses(
    tmp_path,
):
    wiki = client(Server())
    wiki.login.get_session().headers[CAPABILITY_HEADER] = "fictional-existing"
    with (
        pytest.raises(MutationGuardError, match="already carries"),
        protected_mutation(
            wiki, state_path=tmp_path / "guard", operation_id="fictional"
        ),
    ):
        pass
    assert (
        wiki.login.get_session().headers[CAPABILITY_HEADER]
        == "fictional-existing"
    )


def test_control_uses_authenticated_post_and_csrf_without_retry():
    wiki = client(Server())
    calls = []
    wiki.login.get_session().post = lambda url, **kwargs: (
        calls.append((url, kwargs))
        or SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"yizkrmutationguard": {"active": False}},
        )
    )
    assert WikiClient.mutation_guard_request(wiki, "inspect", "fictional") == {
        "active": False
    }
    assert len(calls) == 1
    assert calls[0][1]["data"] == {
        "action": "yizkrmutationguard",
        "mode": "inspect",
        "operation": "fictional",
        "token": "fictional-csrf",
        "format": "json",
        "formatversion": "2",
    }
