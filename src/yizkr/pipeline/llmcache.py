"""The LLM cache: model responses reused at the engine boundary (ADR-0032).

A run recomputes a document's views (its transcription, batch and
critique) when their inputs change, when a scratch run starts from fresh
state directories, or on `--refresh`. The costly, non-deterministic part
of a recompute is the model call; everything after it (parsing, merging,
validation, the transcription check) is code that runs in moments. So
each response is kept under a key built from what its call sends, and a
call made again with the same key is answered from the cache: an
accelerator, which the pipeline runs the same without. A change to what a
call sends changes its key by itself, so no change needs a version bump.

A key is the SHA-256 of the canonical JSON of:

* the engine, the model configuration names (None when the engine runs a
  default of its own) and the effort;
* the engine client's ``config_hash`` (yizkr.pipeline.llm);
* the kind of call: one-shot, the opening turn of a conversation, or a
  fork of one;
* the SHA-256 of the system prompt the engine adds, and of the prompt text
  the engine sends with every path of a file the call is given replaced
  by that file's content SHA-256, so that no key depends on where the
  corpus, a state directory or a temporary file lives;
* the content SHA-256 of every file the call may read, which are the
  files its caller passes;
* for a fork, the keys of the conversation it forks and the SHA-256 of
  the conversation's opening answer, which the fork's context holds;
* the attempt: a caller that asks again after an answer it cannot use,
  and then falls back, keys each attempt apart (llm.for_attempt);
* the role's epoch: 0 until someone re-samples the role on purpose.

Nothing on the engine's server is keyed. The account data the claude CLI
fetches can change its system prompt, its default model and its
compaction, and the CLI fills in details of its own when it runs, such as
the date (yizkr.pipeline.llm). Each entry records the model the engine
reported, so a reuse across such a change can be seen.

Layout under the cache root (runtime state, kept under var/, mode 700):

    <root>/.lock
    <root>/<first two hex digits of the key>/<key>.json
                                            /<key>.rejected-<stamp>.json
                                            /<key>.replaced-<stamp>.json
                                            /<key>.session/<opening>/...

One process at a time uses a cache (ADR-0032 §3). Opening it locks
``.lock`` exclusively (flock) for the rest of the process and records the
process there: its id, host, command and start. A second process that
opens the cache is refused at once (CacheInUse, a pause of its pass) with
that record. The kernel releases the lock when its holder ends, however
it ends, so a holder that crashed blocks nothing: the next process to open
the cache finds the record it left, says so, and records itself in its
place; a clean exit empties the record. Sessions are why: a resumed
claude-code session's transcript sits under its original id in the CLI's
projects folder, which every process shares, so one process's release
would pull it from under another's forks.

``<key>.json`` is the one entry a call is served. It holds the key's
components, the response, its origin (``engine``, or the reason it was
adopted) and its provenance: the role, the engine, the model the engine
reported, the CLI version, the client's config hash and the time; a fork
also records how its session was obtained and the opening answer it was
made on. Only a response the engine returned with some text in it is
stored: an error, a timeout, a pause (PassPaused) or an empty answer
stores nothing. An entry is published whole, by a link that fails where
the key already has one, so a reader sees an entry whole or not at all,
and the cache deletes nothing: an entry a caller rejects, and any entry
a newer one displaces (a refresh's, a racing call's, or one damaged), is
moved aside under a name no lookup reads, and kept for inspection.
``<key>.session/<opening>/`` keeps the session files of a conversation's
opening turn, for the engines that need them to resume it exactly, under
the SHA-256 of the opening answer the session holds: a session, whether
kept there or held in memory after a replay, serves a conversation only
for the answer it was made on, whichever answer a racing call or a
replay has since stored under the key.

docs/LLM_CACHE.md describes the cache, its events and the run directory.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import fcntl
import json
import logging
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yizkr.pipeline.llm import (
    COMPLETE,
    FORK,
    OPEN,
    ConversationalLlmClient,
    ConversationChanged,
    PassPaused,
    engine_client,
)
from yizkr.pipeline.stages import (
    atomic_write_text,
    canonical_json,
    sha256_file,
    sha256_text,
    staging_path,
    write_synced,
)

logger = logging.getLogger(__name__)

FORMAT = 1

# where an entry's response came from: the engine, or an adoption, which
# carries its own reason
ENGINE = "engine"
ADOPTED = "adopted"

# the events a caching client emits
HIT = "cache-hit"
MISS = "cache-miss"
RESUME = "cache-resume"
REPLAY = "cache-replay"
REJECT = "cache-reject"
ADOPT = "cache-adopt"
EVENTS = (HIT, MISS, RESUME, REPLAY, REJECT, ADOPT)

# why a call was answered from the cache, besides an adoption's reason
KEY_MATCH = "key match"
# why a call went to the engine
ABSENT = "absent"
REJECTED = "rejected"
REFRESH = "refresh"

# how a fork's session was obtained: from the opening turn asked in this
# run; resumed exactly from what the cache kept; by a replay of the
# opening turn that gave the cached answer again; or from an opening turn
# recomputed in this run because its replay answered differently
LIVE = "live"
RESUMED = "resumed"
REPLAYED = "replayed"
RECOMPUTED = "recomputed"

# a key, or the SHA-256 of the opening answer a kept session holds
_KEY = re.compile(r"^[0-9a-f]{64}$")
# how many times a publish moves a newer entry aside before it gives up
_PUBLISH_TRIES = 20

# the file under a cache root that the process using the cache locks, and
# records itself in
LOCK_FILE = ".lock"
# how many times, a moment apart, a refused process reads the holder's
# record while it names no running process: a process that has just taken
# the lock records itself right after
_HOLDER_READS = 10
_HOLDER_WAIT = 0.02


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stamp() -> str:
    return (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )


def _dump(entry: Mapping[str, Any]) -> str:
    return json.dumps(entry, indent=2, ensure_ascii=False) + "\n"


def _read(path: Path, key: str) -> dict | None:
    """The entry at `path` if it is a whole entry for `key`, else None."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("key") != key
        or not isinstance(entry.get("response"), str)
    ):
        return None
    return entry


def call_key(components: Mapping[str, Any]) -> str:
    """A call's key: the SHA-256 of its components in canonical JSON."""
    return sha256_text(canonical_json(dict(components)))


# -- one process per cache --------------------------------------------------


class CacheInUse(PassPaused):
    """Another process holds the cache, and one process at a time uses a
    cache. Like any pause, it ends the pass before its next call, rather
    than failing each document in turn: the pass can go on once that
    process has ended, or with another cache."""

    def __init__(self, root: Path, holder: Mapping[str, Any] | None) -> None:
        self.root = root
        self.holder = dict(holder or {})
        super().__init__(
            f"the LLM cache {root} is in use by "
            f"{_describe(holder, 'another process, not yet recorded')}: one "
            "process at a time uses a cache (ADR-0032). Wait for that "
            "process to end, or give this one another cache folder, or none"
        )


# the caches this process holds: the real path of each root -> (its lock
# file, open and locked; the process that locked it, which a forked
# process does not share)
_held: dict[str, tuple[int, int]] = {}
_held_lock = threading.Lock()


def _hold(root: Path) -> None:
    """Hold the cache under `root`, a folder that exists, for the rest of
    this process: lock its lock file exclusively, without waiting, and
    record this process in it. A cache this process holds already is held
    once, however many times it is opened. Raises CacheInUse, naming the
    holder, when another process holds it.

    The lock is flock's, so the kernel releases it when the process ends,
    however it ends. A process that crashed leaves only its record, which
    the next holder reports, in a warning, and replaces; a clean exit
    empties the record (_clear_records)."""
    real = os.path.realpath(root)
    path = os.path.join(real, LOCK_FILE)
    with _held_lock:
        held = _held.pop(real, None)
        if held is not None:
            fd, pid = held
            if pid == os.getpid() and _same_file(fd, path):
                _held[real] = held
                return
            # a lock a forked process inherited, which is its parent's; or
            # one on a lock file removed or replaced since, which excludes
            # no one
            os.close(fd)
            if pid == os.getpid():
                logger.warning(
                    "the lock file of the LLM cache %s was removed or "
                    "replaced while this process held it; locking it again",
                    real,
                )
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CacheInUse(Path(real), _holder(fd)) from None
            stale = _record_text(fd)
            _write_record(fd)
        except BaseException:
            os.close(fd)
            raise
        _held[real] = (fd, os.getpid())
    if stale.strip():
        logger.warning(
            "the LLM cache %s was held by %s, which ended without clearing "
            "its record (a crash or a kill): the lock went with it, and this "
            "process holds the cache now",
            real,
            _describe(_parse_record(stale), "a process it cannot name"),
        )


@atexit.register
def _clear_records() -> None:
    """Empty the records of the caches this process holds, as it exits
    cleanly: a record the next holder finds then marks a process that did
    not."""
    with _held_lock:
        for fd, pid in _held.values():
            if pid == os.getpid():
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)


def _same_file(fd: int, path: str) -> bool:
    """Is the file open as `fd` the one at `path`?"""
    try:
        on_disk = os.stat(path)
    except OSError:
        return False
    held = os.fstat(fd)
    return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)


def _record_text(fd: int) -> str:
    return os.pread(fd, 1 << 16, 0).decode("utf-8", errors="replace")


def _parse_record(text: str) -> dict | None:
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _write_record(fd: int) -> None:
    """Record this process in the lock file it holds."""
    record = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": " ".join(sys.argv)[:500],
        "since": _now(),
    }
    os.ftruncate(fd, 0)
    os.pwrite(fd, (json.dumps(record) + "\n").encode(), 0)


def _holder(fd: int) -> dict | None:
    """The record of the process that holds the lock file open as `fd`,
    or None while it has not recorded itself: the file is read again, a
    moment apart, while it names no running process, since a process that
    has just taken the lock records itself right after, over the record of
    the process before it."""
    for attempt in range(_HOLDER_READS):
        if attempt:
            time.sleep(_HOLDER_WAIT)
        record = _parse_record(_record_text(fd))
        if record is not None and _running(record):
            return record
    return None


def _running(record: Mapping[str, Any]) -> bool:
    """Is the process a record names running? A process on another host
    is taken to be, since this one cannot see it."""
    if record.get("host") != socket.gethostname():
        return True
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _describe(
    record: Mapping[str, Any] | None, unknown: str = "a process"
) -> str:
    """A lock record, for a message; `unknown` without one."""
    if not record:
        return unknown
    text = f"process {record.get('pid')}"
    if record.get("host") != socket.gethostname():
        text += f" on {record.get('host')}"
    if record.get("command"):
        text += f" ({record['command']})"
    if record.get("since"):
        text += f", since {record['since']}"
    return text


class EngineCache:
    """The entries under one cache root, as one run uses them.

    ``refresh`` names roles whose calls go to the engine whatever the
    cache holds, once per key in the run, the new answer displacing the
    entry; ``epochs`` gives roles an epoch other than 0. Opening a cache
    makes its root and holds it for the rest of the process (see the
    module docstring), and raises CacheInUse when another process holds
    it; no entry is written until a call is answered. The root and its
    subdirectories are private to their owner (mode 700, applied to
    folders that exist already): the entries hold corpus content."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        refresh: Iterable[str] = (),
        epochs: Mapping[str, int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.refresh = frozenset(refresh)
        self.epochs = {
            role: int(epoch) for role, epoch in (epochs or {}).items()
        }
        self._lock = threading.Lock()
        # keys a refreshed role has had answered afresh in this run
        self._refreshed: set[str] = set()
        # roles already warned about an unpinned model
        self._warned: set[str] = set()
        # key -> (engine client, session, SHA-256 of the opening answer it
        # was made on) for conversations whose opening turn was recomputed
        # in this run, until one is opened again
        self._sessions: dict[str, tuple[Any, Any, str]] = {}
        self._directory(self.root)
        _hold(self.root)

    def epoch(self, role: str) -> int:
        return self.epochs.get(role, 0)

    def refreshing(self, role: str, key: str) -> bool:
        """Does a call of `role` under `key` go to the engine whatever the
        cache holds? For a refreshed role, until the key has been answered
        afresh in this run."""
        if role not in self.refresh:
            return False
        with self._lock:
            return key not in self._refreshed

    def refreshed(self, key: str) -> None:
        with self._lock:
            self._refreshed.add(key)

    def warn_unpinned(self, role: str) -> None:
        """Say once per role that its keys name no model."""
        with self._lock:
            if role in self._warned:
                return
            self._warned.add(role)
        logger.warning(
            "no model is pinned for the %s role, so its cache keys name "
            "none: if the engine's own default model changes, the answers "
            "of the previous default are still reused (each entry records "
            "the model that answered). Set $YIZKR_LLM_MODEL to key the "
            "role on a model id.",
            role,
        )

    def remember_session(
        self, key: str, client: Any, session: Any, opening: str
    ) -> None:
        """Hold the live session of a conversation whose opening turn was
        recomputed in this run, with the SHA-256 of the opening answer it
        was made on, for the next time it is opened. A session held for
        `key` before, which this one displaces and so nothing will take,
        is released through its engine client's close_conversation."""
        with self._lock:
            displaced = self._sessions.get(key)
            self._sessions[key] = (client, session, opening)
        if displaced is None or (
            displaced[0] is client and displaced[1] == session
        ):
            return
        try:
            _release(displaced[0], displaced[1])
        except Exception as exc:  # a leftover, never the caller's failure
            logger.warning(
                "could not release the session displaced under %s: %s",
                key,
                exc,
            )

    def take_session(self, key: str, client: Any) -> tuple[Any, str] | None:
        """The live session remembered for `key` with this engine client,
        and the SHA-256 of the opening answer it was made on, once; None
        when there is none."""
        with self._lock:
            held = self._sessions.get(key)
            if held is None or held[0] is not client:
                return None
            del self._sessions[key]
            return held[1], held[2]

    # -- entries -----------------------------------------------------------

    def path(self, key: str) -> Path:
        """Where the entry a call with this key is served lives."""
        if not _KEY.match(key or ""):
            raise ValueError(f"not a cache key: {key!r}")
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        """The entry a call with this key is served, or None."""
        return _read(self.path(key), key)

    def rejected(self, key: str) -> list[Path]:
        """The entries under this key that callers rejected."""
        path = self.path(key)
        return sorted(path.parent.glob(f"{key}.rejected-*.json"))

    def put(self, key: str, entry: Mapping[str, Any]) -> None:
        """Store `entry` under `key`. An entry the key already has, live or
        damaged, is moved aside as replaced, never overwritten."""
        path = self.path(key)
        self._directory(path.parent)
        staging = staging_path(path)
        try:
            write_synced(staging, _dump(entry))
            for _ in range(_PUBLISH_TRIES):
                if _link(staging, path):
                    return
                self._move_aside(
                    path, "replaced", {"replaced": {"at": _now()}}
                )
            raise RuntimeError(f"could not store the cache entry {path}")
        finally:
            staging.unlink(missing_ok=True)

    def reject(
        self, key: str, response: str, reason: str, **fields: Any
    ) -> bool:
        """Move the entry under `key` aside, with the reason, if it holds
        `response`. The entry is first renamed to a private name and only
        then read, so an answer stored under the key meanwhile is never
        the one moved aside: one that is not `response` goes back, unless
        the key has since been given another, in which case it is kept
        aside as replaced. The next call under the key goes to the
        engine. Returns whether `response` was moved aside."""
        path = self.path(key)
        held = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rejecting")
        try:
            os.rename(path, held)
        except FileNotFoundError:
            return False
        entry = _read(held, key)
        if entry is not None and entry["response"] == response:
            rejection = {"at": _now(), "reason": reason}
            rejection.update(
                {k: v for k, v in fields.items() if v is not None}
            )
            aside = self._aside_name(path, "rejected")
            os.rename(held, aside)
            atomic_write_text(aside, _dump(entry | {"rejected": rejection}))
            return True
        try:
            if not _link(held, path):
                self._move_aside(
                    held, "replaced", {"replaced": {"at": _now()}}, key=key
                )
        finally:
            held.unlink(missing_ok=True)
        return False

    def seed(
        self,
        key: str,
        response: str,
        provenance: Mapping[str, Any],
        reason: str = ADOPTED,
    ) -> bool:
        """Adopt `response` for the call with this key: an answer the
        engine did not give it (an archived transcription, say), stored as
        a visible equivalence. A hit on it reports `reason` rather than a
        key match, and its provenance is the one given. Nothing is stored
        when the key already has an entry, or when `response` is one a
        caller rejected under it. Returns whether it was adopted."""
        if reason == ENGINE:
            raise ValueError(
                f"an adoption's reason cannot be {ENGINE!r}: that origin "
                "marks the engine's own answers"
            )
        path = self.path(key)
        for aside in self.rejected(key):
            if (_read(aside, key) or {}).get("response") == response:
                return False
        self._directory(path.parent)
        entry = {
            "format": FORMAT,
            "key": key,
            "response": response,
            "origin": reason,
            "provenance": {**dict(provenance), "adopted_at": _now()},
        }
        staging = staging_path(path)
        try:
            write_synced(staging, _dump(entry))
            return _link(staging, path)
        finally:
            staging.unlink(missing_ok=True)

    # -- kept sessions -------------------------------------------------------

    def _session_dir(self, key: str, opening: str) -> Path:
        if not _KEY.match(opening or ""):
            raise ValueError(f"not an opening answer's SHA-256: {opening!r}")
        return self.path(key).with_suffix(".session") / opening

    def keep_session(
        self, key: str, opening: str, files: Mapping[str, bytes]
    ) -> None:
        """Keep the session files of the conversation opened under `key`
        whose opening answer has the SHA-256 `opening`, replacing (and
        keeping aside) any kept before for that answer. The files are
        published whole, by renaming their folder into place, so the
        answer they are kept under names the session they hold."""
        if not files:
            return
        target = self._session_dir(key, opening)
        self._directory(target.parent)
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            staging.mkdir(mode=0o700)
            for name, content in files.items():
                path = (staging / name).resolve()
                if not path.is_relative_to(staging.resolve()):
                    raise ValueError(
                        f"a session file outside its folder: {name}"
                    )
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(content)
            if target.exists():
                os.rename(target, self._aside_name(target, "replaced"))
            os.rename(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def kept_session(self, key: str, opening: str) -> dict[str, bytes]:
        """The session files kept for the conversation opened under `key`
        with the opening answer whose SHA-256 is `opening`, by their
        relative paths; empty when none are kept for that answer."""
        folder = self._session_dir(key, opening)
        if not folder.is_dir():
            return {}
        return {
            path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }

    # -- helpers -------------------------------------------------------------

    def _directory(self, directory: Path) -> None:
        """Make `directory`, and each folder from the root down to it,
        private to its owner."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        folders = [self.root]
        for part in directory.relative_to(self.root).parts:
            folders.append(folders[-1] / part)
        for folder in folders:
            folder.mkdir(mode=0o700, exist_ok=True)
            if stat.S_IMODE(folder.stat().st_mode) != 0o700:
                os.chmod(folder, 0o700)

    @staticmethod
    def _aside_name(path: Path, what: str) -> Path:
        base = path.name.removesuffix(".json")
        suffix = ".json" if path.name.endswith(".json") else ""
        return path.with_name(f"{base}.{what}-{_stamp()}{suffix}")

    def _move_aside(
        self,
        path: Path,
        what: str,
        note: Mapping[str, Any],
        key: str | None = None,
    ) -> bool:
        """Rename the entry at `path` to a name no lookup reads, then add
        `note` to it. Returns whether there was an entry to move."""
        name = self.path(key) if key is not None else path
        aside = self._aside_name(name, what)
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return False
        try:
            entry = json.loads(aside.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if isinstance(entry, dict):
            atomic_write_text(aside, _dump(entry | dict(note)))
        return True


class CacheAsIs(EngineCache):
    """The entries under one cache root as a run would be served them,
    read as they are: without the cache's lock, which a running pass may
    hold, and with nothing written, the folders included. What calls made
    through it change (an answer rejected, one stored, a key refreshed) it
    keeps in memory, for as long as it lives. It takes the layout, and the
    reading of an entry, from EngineCache, but none of its opening, which
    makes the root and holds its lock. A kept session is given by its
    files' names, without their bytes: whether they can resume a
    conversation is all a reader of it asks. A kept session is read
    through its folder, opened once, so a process that replaces it
    meanwhile (keep_session: the old folder renamed aside, the new one
    renamed in) never tears the listing, and a folder missing where
    another is kept under the key is read again a moment later, as it is
    while one replaces it."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        refresh: Iterable[str] = (),
        epochs: Mapping[str, int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.refresh = frozenset(refresh)
        self.epochs = {
            role: int(epoch) for role, epoch in (epochs or {}).items()
        }
        self._lock = threading.Lock()
        self._refreshed: set[str] = set()
        self._warned: set[str] = set()
        self._sessions: dict[str, tuple[Any, Any, str]] = {}
        # answers stored, and keys rejected, through this view
        self._stored: dict[str, dict] = {}
        self._rejected: set[str] = set()

    def warn_unpinned(self, role: str) -> None:
        """Nothing is written, and nothing is said."""

    def remember_session(
        self, key: str, client: Any, session: Any, opening: str
    ) -> None:
        """No session is made through this view, so none is held."""

    def take_session(self, key: str, client: Any) -> None:
        return None

    def get(self, key: str) -> dict | None:
        if key in self._stored:
            return self._stored[key]
        if key in self._rejected:
            return None
        return super().get(key)

    def rejected(self, key: str) -> list[Path]:
        if key in self._rejected:
            return [self.path(key)]
        return super().rejected(key)

    def put(self, key: str, entry: Mapping[str, Any]) -> None:
        self._stored[key] = dict(entry)

    def reject(
        self, key: str, response: str, reason: str, **fields: Any
    ) -> bool:
        entry = self.get(key)
        if entry is None or entry["response"] != response:
            return False
        self._stored.pop(key, None)
        self._rejected.add(key)
        return True

    def seed(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def keep_session(
        self, key: str, opening: str, files: Mapping[str, bytes]
    ) -> None:
        """No session is made through this view, so none is kept."""

    def kept_session(self, key: str, opening: str) -> dict[str, bytes]:
        folder = self._session_dir(key, opening)
        if not folder.parent.is_dir():
            # nothing kept for any answer under the key
            return {}
        for attempt in range(_HOLDER_READS):
            if attempt:
                time.sleep(_HOLDER_WAIT)
            try:
                return _file_names(folder)
            except OSError:
                continue
        return {}


def _file_names(folder: Path) -> dict[str, bytes]:
    """The files under `folder`, by their paths relative to it, with no
    bytes, listed through the folder opened once. OSError when it cannot
    be opened (missing, say) or read."""

    def fail(error: OSError) -> None:
        raise error

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return {
            Path(os.path.normpath(os.path.join(top, name))).as_posix(): b""
            for top, _dirs, files, _fd in os.fwalk(
                ".", dir_fd=fd, onerror=fail
            )
            for name in sorted(files)
        }
    finally:
        os.close(fd)


def held_by(root: str | os.PathLike[str]) -> str | None:
    """The process that holds the cache under `root` now, described, as
    its lock file records it, read without taking the lock; None when the
    record names no running process, or there is none."""
    try:
        text = (Path(root) / LOCK_FILE).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    record = _parse_record(text.strip())
    if record is None or not _running(record):
        return None
    return _describe(record)


def _release(client: Any, session: Any) -> None:
    """Release `session` through the engine client that holds it, when the
    engine holds sessions."""
    close = getattr(client, "close_conversation", None)
    if callable(close):
        close(session)


def _link(source: Path, target: Path) -> bool:
    """Publish `source` as `target` unless `target` exists: True when it
    was published."""
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    return True


class CachedResponse(str):
    """A response, the key it is cached under, and whether the cache
    answered the call with it (`hit`) rather than the engine in this call;
    in every other respect it is the text. It copies and pickles with
    both."""

    key: str
    hit: bool

    def __new__(cls, text: str, key: str, hit: bool = False) -> CachedResponse:
        response = super().__new__(cls, text)
        response.key = key
        response.hit = hit
        return response

    def __getnewargs__(self) -> tuple[str, str]:
        return str(self), self.key


class Conversation:
    """A conversation opened through the cache.

    It carries the keys of its turns (its forks' parent chain), the
    opening turn's key components, prompt and answer with that answer's
    SHA-256, how its session was, or will be, obtained, and the engine's
    own handle once the engine holds the conversation. `changed` says a
    replay answered differently, and `failed` why a replay failed: either
    holds for the rest of the pass, whose forks then fail at once."""

    def __init__(
        self,
        components: dict[str, Any],
        prompt: str,
        response: str,
        session: Any = None,
        origin: str | None = None,
    ) -> None:
        self.components = components
        self.chain = (call_key(components),)
        self.prompt = prompt
        self.response = response
        self.opening_sha256 = sha256_text(response)
        self.session = session
        self.origin = origin
        self.changed = False
        self.failed: str | None = None
        self.lock = threading.Lock()


def _file_forms(path: str | os.PathLike[str]) -> set[str]:
    """The ways a call's text may spell a file's path."""
    return {
        os.fspath(path),
        os.path.abspath(path),
        str(Path(path).resolve()),
    }


class CachingClient:
    """An engine's client whose calls the cache answers when the same call
    was answered before, keyed as the module docstring describes.

    ``client`` is the engine's own client, or one that reports the calls
    it makes (llm.observed_client). The calls go through ``client``;
    everything that describes the engine (the key's components, an
    entry's provenance, the sessions it holds) is read from the engine's
    own client, so that a key is the same whoever reports the calls.
    ``role`` names the pipeline step the calls are made for (transcribe,
    extract, critique): the role's epoch is keyed, and its refresh
    setting applies. ``events``, when given, is called with each event's
    name and fields: a hit (with its reason: a key match, or the
    adoption's reason), a miss (absent, rejected or refresh) as the
    engine is asked, a resume, a replay, a rejection or an adoption."""

    def __init__(
        self,
        client: Any,
        cache: EngineCache,
        *,
        role: str,
        events: Callable[..., Any] | None = None,
    ) -> None:
        engine = engine_client(client)
        if not hasattr(engine, "config_hash"):
            raise TypeError(
                f"{type(engine).__name__} has no config_hash, so its calls "
                "cannot be keyed"
            )
        self.client = client
        # the engine's own client: what it applies, and the sessions it
        # holds, whoever reports its calls
        self._engine = engine
        self.cache = cache
        self.role = role
        self._events = events
        self._attempt = 0
        # None or "": the engine runs a default model of its own
        if getattr(engine, "model", "unnamed") in (None, ""):
            cache.warn_unpinned(role)

    @property
    def model_identifier(self) -> str:
        return self._engine.model_identifier

    def attempt(self, number: int) -> CachingClient:
        """This client for attempt `number` of the same call: each attempt
        is keyed, and kept, apart (llm.for_attempt)."""
        sibling = copy.copy(self)
        sibling._attempt = number
        return sibling

    # -- keys ---------------------------------------------------------------

    def components(
        self,
        call: str,
        prompt: str,
        image_paths: Sequence[str | os.PathLike[str]] = (),
        parent: Sequence[str] = (),
        opening: str | None = None,
    ) -> dict[str, Any]:
        """The components of a call's key (see the module docstring)."""
        client = self._engine
        render = getattr(client, "call_text", None)
        text = render(prompt, image_paths) if callable(render) else prompt
        files: list[str] = []
        forms: list[tuple[str, str]] = []
        for path in image_paths:
            digest = sha256_file(Path(path))
            if digest is None:
                raise FileNotFoundError(
                    f"{path}: a file a call reads must exist to be keyed"
                )
            files.append(digest)
            forms += [(form, digest) for form in _file_forms(path)]
        for form, digest in sorted(forms, key=lambda f: -len(f[0])):
            text = text.replace(form, f"sha256:{digest}")
        system = getattr(client, "system_prompt", None)
        return {
            "engine": getattr(client, "engine", type(client).__name__),
            "model": getattr(client, "model", None) or None,
            "effort": getattr(client, "effort", None),
            "config": client.config_hash,
            "call": call,
            "system": None if system is None else sha256_text(system),
            "prompt": sha256_text(text),
            "files": files,
            "parent": list(parent),
            "opening": opening,
            "attempt": self._attempt,
            "epoch": self.cache.epoch(self.role),
        }

    def complete_key(
        self,
        prompt: str,
        image_paths: Sequence[str | os.PathLike[str]] = (),
    ) -> str:
        """The key `complete(prompt, image_paths)` is served under: what
        an adoption of an answer for that call is seeded with."""
        return call_key(self.components(COMPLETE, prompt, image_paths))

    # -- calls ----------------------------------------------------------------

    def complete(
        self,
        prompt: str,
        image_paths: Sequence[str | os.PathLike[str]] = (),
    ) -> str:
        def ask(miss: Callable[[], None]) -> tuple[str, dict[str, Any]]:
            miss()
            return self.client.complete(prompt, image_paths), {}

        return self._serve(
            COMPLETE, self.components(COMPLETE, prompt, image_paths), ask
        )[0]

    def _serve(
        self,
        call: str,
        components: dict[str, Any],
        ask: Callable[[Callable[[], None]], tuple[str, dict[str, Any]]],
    ) -> tuple[CachedResponse, bool]:
        """(the answer, whether the engine gave it now): the cached answer
        to a call, or the engine's, which is then stored when it holds
        some text. `ask` makes the call and returns its response with any
        provenance of its own; it is given the function that emits the
        miss, and calls it just before it asks the engine, so that a call
        that fails before then (a fork without a session) is no miss.
        Whatever `ask` raises propagates, and nothing is stored."""
        key = call_key(components)
        refresh = self.cache.refreshing(self.role, key)
        if not refresh:
            entry = self.cache.get(key)
            if entry is not None:
                origin = entry.get("origin", ENGINE)
                self._emit(
                    HIT,
                    call=call,
                    key=key,
                    reason=KEY_MATCH if origin == ENGINE else origin,
                )
                return CachedResponse(entry["response"], key, hit=True), False
        if refresh:
            reason = REFRESH
        elif self.cache.rejected(key):
            reason = REJECTED
        else:
            reason = ABSENT
        response, provenance = ask(
            lambda: self._emit(MISS, call=call, key=key, reason=reason)
        )
        if str(response).strip():
            self.cache.put(
                key, self._entry(key, components, response, provenance)
            )
            if refresh:
                self.cache.refreshed(key)
        return CachedResponse(response, key), True

    def _entry(
        self,
        key: str,
        components: dict[str, Any],
        response: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        """A new entry for a response the engine has just returned."""
        last_report = getattr(self._engine, "last_report", None)
        report = last_report() if callable(last_report) else {}
        return {
            "format": FORMAT,
            "key": key,
            "components": components,
            "response": str(response),
            "origin": ENGINE,
            "provenance": {
                "role": self.role,
                "call": components["call"],
                "engine": components["engine"],
                "model": report.get("model"),
                "cli_version": report.get("cli_version")
                or getattr(self._engine, "cli_version", None),
                "config_hash": components["config"],
                "model_identifier": self._engine.model_identifier,
                "at": _now(),
                **provenance,
            },
        }

    # -- rejection and adoption ---------------------------------------------

    def reject(self, response: str, reason: str) -> bool:
        """Reject `response`, which this client returned: the entry under
        its key is moved aside with `reason` and never served again, so
        the next identical call asks the engine. The rejection is an
        event when an entry was moved. Returns whether the cache answered
        the call with `response` (a hit), rather than the engine in this
        call: a caller that asks once more then has the engine's answer,
        as it would without the cache (llm.RejectingLlmClient)."""
        key = getattr(response, "key", None)
        if not isinstance(key, str):
            raise ValueError(
                "only a response this client returned can be rejected"
            )
        if self.cache.reject(key, str(response), reason, role=self.role):
            self._emit(REJECT, key=key, reason=reason[:500])
        return bool(getattr(response, "hit", False))

    def seed(
        self,
        key: str,
        response: str,
        provenance: Mapping[str, Any],
        reason: str = ADOPTED,
    ) -> bool:
        """`EngineCache.seed`, with an adoption event when it adopts."""
        adopted = self.cache.seed(key, response, provenance, reason=reason)
        if adopted:
            self._emit(ADOPT, key=key, reason=reason)
        return adopted

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def _emit(self, event: str, **fields: Any) -> None:
        if self._events is not None:
            self._events(event, role=self.role, **fields)


class ConversationalCachingClient(CachingClient):
    """A caching client for an engine that holds conversations (ADR-0006).

    Opening a conversation is a call like any other. A fork is keyed on the
    keys of the conversation it forks and on its opening answer, and a
    fork the cache answers needs no session. A fork the cache cannot
    answer, of a conversation whose opening turn it did answer, needs the
    engine to hold that conversation exactly as the fork's context
    assumes:

    * the conversation is resumed exactly, without a call, from what the
      cache kept when it was opened (llm.ResumableLlmClient): the session
      files of an engine that stores sessions, kept under the opening
      answer they hold and resumed only for that answer, or the turn
      itself for an engine that needs nothing more;
    * failing that, the opening turn is replayed. A replay that gives the
      cached answer again serves the conversation's forks. One that
      answers differently makes the whole conversation a miss: the
      replay's answer becomes the opening turn's entry, the replay's
      session is kept under that answer and held for the run with it,
      and the fork raises ConversationChanged, so that its caller starts
      over from the new answer, whose forks are keyed apart from the old
      ones. The conversation opened again takes the held session only
      when the cache serves it the answer the session was made on; when
      a racing call or replay has stored another answer since, the held
      session is released, and a fork resumes the session kept for the
      served answer, or replays. A replay that fails (the engine raises, or
      answers with no text) fails the fork, and every later fork of the
      conversation in the pass, without asking the engine again.

    A session, however obtained, serves every later fork of the
    conversation in the run, and never a conversation opened on another
    answer than the one it holds. A fork the cache cannot answer is a miss
    once the engine holds its session, just before the fork is asked: a
    fork that fails for want of a session is none."""

    def open_conversation(self, prompt: str) -> tuple[str, Conversation]:
        components = self.components(OPEN, prompt)
        sessions: list[Any] = []

        def ask(miss: Callable[[], None]) -> tuple[str, dict[str, Any]]:
            miss()
            response, session = self.client.open_conversation(prompt)
            sessions.append(session)
            return response, {}

        response, asked = self._serve(OPEN, components, ask)
        if asked:
            conversation = Conversation(
                components, prompt, str(response), sessions[0], LIVE
            )
            self._keep(response.key, conversation.opening_sha256, sessions[0])
            return response, conversation
        conversation = Conversation(components, prompt, str(response))
        held = self.cache.take_session(response.key, self._engine)
        if held is not None:
            session, opening = held
            if opening == conversation.opening_sha256:
                conversation.session = session
                conversation.origin = RECOMPUTED
            else:
                # made on another answer than the one served, which a
                # racing call or replay has since stored under the key
                self._release(session)
        return response, conversation

    def fork(self, handle: Any, prompt: str) -> str:
        if not isinstance(handle, Conversation):
            raise TypeError("fork takes a conversation this client opened")

        def ask(miss: Callable[[], None]) -> tuple[str, dict[str, Any]]:
            session = self._session(handle)
            miss()
            response = self.client.fork(session, prompt)
            return response, {
                "session": handle.origin,
                "opening_sha256": handle.opening_sha256,
            }

        components = self.components(
            FORK, prompt, parent=handle.chain, opening=handle.opening_sha256
        )
        return self._serve(FORK, components, ask)[0]

    def _keep(self, key: str, opening: str, session: Any) -> None:
        """Keep what the engine needs to resume the conversation opened
        under `key` exactly, under the SHA-256 of the opening answer the
        session holds (nothing, for an engine that cannot). A session that
        cannot be kept is only a warning: no fork resumes it, and one that
        misses replays the opening turn instead."""
        keep = getattr(self.client, "keep_conversation", None)
        if not callable(keep):
            return
        try:
            self.cache.keep_session(key, opening, keep(session))
        except (OSError, ValueError) as exc:
            logger.warning("could not keep the session of %s: %s", key, exc)

    def _session(self, conversation: Conversation) -> Any:
        """The engine's handle for `conversation` (see the class
        docstring): resumed exactly, from the session kept for its opening
        answer, or replayed and found the same; a replay that differs
        raises ConversationChanged. A replay that fails raises, and the
        failure is remembered: the conversation's later forks raise it
        again at once, rather than asking the engine once per fork."""
        with conversation.lock:
            key = conversation.chain[-1]
            if conversation.changed:
                raise _changed(key)
            if conversation.failed is not None:
                raise RuntimeError(
                    f"the replay of the conversation opened under {key} "
                    f"failed earlier in this pass: {conversation.failed}"
                )
            if conversation.session is not None:
                return conversation.session
            resume = getattr(self.client, "resume_conversation", None)
            if callable(resume):
                try:
                    session = resume(
                        conversation.prompt,
                        conversation.response,
                        self.cache.kept_session(
                            key, conversation.opening_sha256
                        ),
                    )
                except Exception as exc:  # nothing kept, or unusable
                    logger.info("could not resume %s: %s", key, exc)
                else:
                    conversation.session = session
                    conversation.origin = RESUMED
                    self._emit(RESUME, call=OPEN, key=key)
                    return session
            try:
                response, session = self.client.open_conversation(
                    conversation.prompt
                )
            except Exception as exc:
                conversation.failed = f"{type(exc).__name__}: {exc}"
                raise
            if not str(response).strip():
                conversation.failed = "it returned no text"
                self._release(session)
                raise RuntimeError(
                    f"the replay of the conversation opened under {key} "
                    "returned no text"
                )
            matches = response == conversation.response
            self._emit(REPLAY, call=OPEN, key=key, matches=matches)
            if matches:
                conversation.session = session
                conversation.origin = REPLAYED
                self._keep(key, conversation.opening_sha256, session)
                return session
            self.cache.put(
                key,
                self._entry(
                    key,
                    conversation.components,
                    response,
                    {"recomputed": True},
                ),
            )
            opening = sha256_text(response)
            self._keep(key, opening, session)
            self.cache.remember_session(key, self._engine, session, opening)
            conversation.changed = True
            raise _changed(key)

    def close_conversation(self, handle: Any) -> None:
        """Release the engine's session of a conversation, if it holds
        one."""
        if isinstance(handle, Conversation) and handle.session is not None:
            self._release(handle.session)

    def _release(self, session: Any) -> None:
        _release(self.client, session)


def _changed(key: str) -> ConversationChanged:
    return ConversationChanged(
        f"the conversation opened under {key} answered differently when "
        "it was replayed"
    )


def wrap(
    client: Any,
    cache: EngineCache,
    *,
    role: str,
    events: Callable[..., Any] | None = None,
) -> CachingClient:
    """`client` behind `cache`, conversational exactly when it is."""
    kind = (
        ConversationalCachingClient
        if isinstance(client, ConversationalLlmClient)
        else CachingClient
    )
    return kind(client, cache, role=role, events=events)
