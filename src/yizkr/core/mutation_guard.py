"""Capability-scoped exclusion enforced by the configured Wikibase server.

The local file only retains the capability for recovery. The server gate drains
PHP and database requests and excludes every competing mutation. It never
expires an owner. A completed release removes the private local capability;
interruption or an uncertain release preserves it for the same operation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from yizkr.core.wiki import WikiClient

PROTOCOL = "yizkr-mutation-guard-v1"
CAPABILITY_HEADER = "X-Yizkr-Mutation-Capability"


class MutationGuardError(RuntimeError):
    """The server cannot prove exclusive mutation authority."""


def _status(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or set(value)
        != {"protocol", "instance", "epoch", "active", "operation_id"}
        or value["protocol"] != PROTOCOL
        or not isinstance(value["instance"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", value["instance"])
        or type(value["epoch"]) is not int
        or value["epoch"] < 0
        or type(value["active"]) is not bool
        or (
            value["active"]
            and (
                not isinstance(value["operation_id"], str)
                or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,200}", value["operation_id"]
                )
            )
        )
        or (not value["active"] and value["operation_id"] is not None)
    ):
        raise MutationGuardError(
            "server mutation exclusion response is invalid"
        )
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_private(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise MutationGuardError(
                "mutation capability must be a regular 0600 file"
            )
        try:
            value = json.load(handle)
        except (ValueError, TypeError) as exc:
            raise MutationGuardError(
                "mutation capability file is invalid"
            ) from exc
    if not isinstance(value, dict):
        raise MutationGuardError("mutation capability file is invalid")
    return value


def _write_private(path: Path, value: dict) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


@dataclass(frozen=True)
class MutationGuard:
    client: WikiClient
    operation_id: str
    instance: str
    epoch: int

    @property
    def evidence(self) -> dict:
        """Nonsecret evidence suitable for an immutable operation record."""
        return {
            "protocol": PROTOCOL,
            "epoch": self.epoch,
            "operation_id": self.operation_id,
        }

    def _request(self, mode: str) -> dict:
        try:
            state = _status(
                self.client.mutation_guard_request(mode, self.operation_id)
            )
        except MutationGuardError:
            raise
        except Exception as exc:
            raise MutationGuardError(
                "server mutation exclusion could not be verified"
            ) from exc
        if state["instance"] != self.instance or state["epoch"] != self.epoch:
            raise MutationGuardError(
                "server mutation exclusion changed instance or epoch"
            )
        return state

    def assert_active(self) -> None:
        state = self._request("status")
        if not state["active"] or state["operation_id"] != self.operation_id:
            raise MutationGuardError(
                "server mutation exclusion is no longer owned"
            )


@contextmanager
def protected_mutation(
    client: WikiClient, *, state_path: Path, operation_id: str
) -> Iterator[MutationGuard]:
    """Acquire or recover one server fence, and release after handled exits.

    An unsuccessful release keeps the private capability file. A retry must use
    the same instance, operation and path. No timeout authorizes a successor.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation_id):
        raise ValueError("mutation operation ID is invalid")
    if not client._mutation_lock.acquire(blocking=False):
        raise MutationGuardError(
            "the WikiClient already owns a mutation context"
        )
    descriptor = None
    session = None
    installed = False
    redirects = None
    try:
        client._mutation_thread = threading.get_ident()
        # Authentication remains available during an existing owner's fence.
        session = client.login.get_session()
        if CAPABILITY_HEADER in session.headers:
            raise MutationGuardError(
                "the WikiClient already carries a capability"
            )
        state_path = Path(state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            state_path.with_name(state_path.name + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MutationGuardError(
                "mutation capability is in use by another process"
            ) from exc
        try:
            observed = _status(
                client.mutation_guard_request("inspect", operation_id)
            )
        except Exception as exc:
            raise MutationGuardError(
                "server mutation exclusion is unavailable"
            ) from exc
        if state_path.exists() or state_path.is_symlink():
            saved = _read_private(state_path)
            if (
                set(saved)
                != {
                    "protocol",
                    "api_url",
                    "operation_id",
                    "instance",
                    "epoch",
                    "capability",
                }
                or saved["protocol"] != PROTOCOL
                or saved["api_url"] != client.api_url
                or saved["operation_id"] != operation_id
                or saved["instance"] != observed["instance"]
                or type(saved["epoch"]) is not int
                or saved["epoch"] < 1
                or not isinstance(saved["capability"], str)
                or not re.fullmatch(
                    re.escape(f"{saved['instance']}.{saved['epoch']}.")
                    + r"[a-f0-9]{64}",
                    saved["capability"],
                )
            ):
                raise MutationGuardError(
                    "mutation capability has another operation or instance"
                )
            # The server may have completed a release whose response was lost.
            if not observed["active"] and observed["epoch"] >= saved["epoch"]:
                state_path.unlink()
                _sync_directory(state_path.parent)
                saved = None
        else:
            saved = None
        if saved is None:
            if observed["active"]:
                raise MutationGuardError(
                    "server mutation exclusion belongs to another operation"
                )
            epoch = observed["epoch"] + 1
            saved = {
                "protocol": PROTOCOL,
                "api_url": client.api_url,
                "operation_id": operation_id,
                "instance": observed["instance"],
                "epoch": epoch,
                "capability": (
                    f"{observed['instance']}.{epoch}.{secrets.token_hex(32)}"
                ),
            }
            _write_private(state_path, saved)
        client._mutation_capability = saved["capability"]
        redirects = session.max_redirects
        session.max_redirects = 0
        session.headers[CAPABILITY_HEADER] = saved["capability"]
        installed = True
        guard = MutationGuard(
            client, operation_id, saved["instance"], saved["epoch"]
        )
        acquired = guard._request("begin")
        if not acquired["active"] or acquired["operation_id"] != operation_id:
            raise MutationGuardError("server did not grant mutation exclusion")
        try:
            yield guard
        finally:
            released = guard._request("end")
            if released["active"]:
                raise MutationGuardError(
                    "server did not release mutation exclusion"
                )
            state_path.unlink()
            _sync_directory(state_path.parent)
    finally:
        client._mutation_capability = None
        client._mutation_thread = None
        if session is not None and installed:
            session.headers.pop(CAPABILITY_HEADER, None)
            session.max_redirects = redirects
        if descriptor is not None:
            os.close(descriptor)
        client._mutation_lock.release()
