"""Provider conversations and their execution-owned checkpoint resources.

Response storage remains independent of provider files and live handles. The
execution cache composes it with a session service for the same usage lifetime;
its caller closes engines before releasing this service. Existing checkpoint
paths and response ancestry remain compatible with persisted caches.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from yizkr.pipeline.cancellation import RunCancelled
from yizkr.pipeline.llm import (
    FORK,
    OPEN,
    ClassifierRefused,
    ConversationChanged,
)
from yizkr.pipeline.llmcache import (
    _KEY,
    _PUBLISH_TRIES,
    LIVE,
    RECOMPUTED,
    REPLAY,
    REPLAYED,
    RESUME,
    RESUMED,
    CacheAsIs,
    CachingClient,
    EngineCache,
    _make_private,
    call_key,
)
from yizkr.pipeline.stages import sha256_text

logger = logging.getLogger(__name__)


class SessionStore:
    """Compatible provider checkpoints, independent of response entries.

    The response owner opens and protects the root before creating this store.
    A names-only store supports estimation without reading transcript bytes or
    changing folders; it never retains provider handles.
    """

    def __init__(self, root: Path, *, names_only: bool = False) -> None:
        self.root = Path(root)
        self.names_only = names_only

    def path(self, key: str) -> Path:
        if not _KEY.match(key or ""):
            raise ValueError(f"not a cache key: {key!r}")
        return self.root / key[:2] / f"{key}.json"

    _aside_name = staticmethod(EngineCache._aside_name)

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
        answer they are kept under names the session they hold. A folder
        kept there before is first moved aside, so between the two renames
        the answer's own path holds no folder: a reader finds the session
        in the folder moved aside for as long as that lasts, whether it
        reads under the lock (kept_session) or without it
        (SessionStore.kept_session)."""
        if self.names_only or not files:
            return
        target = self._session_dir(key, opening)
        _make_private(target.parent)
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            _make_private(staging)
            for name, content in files.items():
                path = (staging / name).resolve()
                if not path.is_relative_to(staging.resolve()):
                    raise ValueError(
                        f"a session file outside its folder: {name}"
                    )
                _make_private(path.parent)
                path.write_bytes(content)
            if target.exists():
                self._move_session_aside(target)
            os.rename(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _move_session_aside(self, target: Path) -> None:
        """Move the session folder at `target` aside as replaced, under a
        name of its own. A folder a stamp of the same second already names
        is stamped again, since renaming a folder onto one that holds
        files fails rather than replacing it, and the keep with it: a
        folder moved aside is never displaced, whatever the stamp. A
        folder another keep has moved aside meanwhile is left where that
        one put it."""
        for _ in range(_PUBLISH_TRIES):
            try:
                os.rename(target, self._aside_name(target, "replaced"))
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
        raise FileExistsError(
            errno.EEXIST,
            "no name left to move the kept session aside under",
            str(target),
        )

    def _replaced_sessions(self, folder: Path) -> list[Path]:
        """The session folders kept aside as replaced where `folder` keeps
        the session of an opening answer, newest first as far as their
        stamps order them: those of one second, or from across a change of
        the clock, come in an order of their own. Each holds a whole
        session of the same opening answer."""
        try:
            return sorted(
                folder.parent.glob(f"{folder.name}.replaced-*"), reverse=True
            )
        except OSError:
            return []

    def kept_session(self, key: str, opening: str) -> dict[str, bytes]:
        """The session files kept for the conversation opened under `key`
        with the opening answer whose SHA-256 is `opening`, by their
        relative paths; empty when none are kept for that answer.

        A keep moves the folder it finds at the answer's path aside before
        it renames its own in, so one that ended between the two (killed,
        or its rename in refused) leaves that path with no folder and the
        session in the folder it moved aside. The newest of those holding
        a file is read instead, so this reader and the lock-free one
        (SessionStore.kept_session) answer alike, and a run resumes what the
        estimate counted on."""
        folder = self._session_dir(key, opening)
        if self.names_only:
            names = _listed(folder)
            if names:
                return names
            for aside in self._replaced_sessions(folder):
                names = _listed(aside)
                if names:
                    return names
            return {}
        if not folder.is_dir():
            kept = (
                aside
                for aside in self._replaced_sessions(folder)
                if any(path.is_file() for path in aside.rglob("*"))
            )
            folder = next(kept, None)
            if folder is None:
                return {}
        return {
            path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }


def _listed(folder: Path) -> dict[str, bytes]:
    """The files under `folder` as `_file_names` gives them; empty where
    it cannot be opened or read, as a folder a keep has just renamed
    cannot be."""
    try:
        return _file_names(folder)
    except OSError:
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


def _release(client: Any, session: Any) -> None:
    """Release `session` through the engine client that holds it, when the
    engine holds sessions."""
    close = getattr(client, "close_conversation", None)
    if callable(close):
        close(session)


class SessionLease:
    """An owned provider handle; release waits for its borrowed forks."""

    def __init__(self, pool, client: Any, handle: Any) -> None:
        self.pool = pool
        self.client = client
        self.handle = handle
        self.borrowers = 0
        self.retired = False
        self.released = False

    @contextlib.contextmanager
    def borrow(self):
        with self.pool._lock:
            self.pool.check()
            if self.retired:
                raise RunCancelled("the provider conversation was released")
            self.borrowers += 1
        failure = None
        try:
            yield self.handle
            self.pool.check()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            with self.pool._lock:
                self.borrowers -= 1
                release = self.pool._collect(self)
            if release:
                try:
                    _release(self.client, self.handle)
                except BaseException as exc:
                    if failure is None:
                        raise
                    failure.add_note(
                        "provider session cleanup also failed: "
                        f"{type(exc).__name__}"
                    )

    def close(self) -> None:
        self.pool.release(self)


class SessionPool:
    """Own conversation leases and replay handoffs for one execution cache.

    Closing prevents fresh provider work. Idle leases close immediately;
    borrowed leases close after their last active fork, including a custom
    worker that outlives the driver's bounded join.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._closed = False
        self._leases: list[SessionLease] = []
        self._remembered: dict[str, tuple[SessionLease, str]] = {}

    def check(self) -> None:
        with self._lock:
            if self._closed:
                raise RunCancelled("the provider session service was closed")

    def own(self, client: Any, handle: Any) -> SessionLease:
        with self._lock:
            if not self._closed:
                lease = SessionLease(self, client, handle)
                self._leases.append(lease)
                return lease
        failure = RunCancelled("the provider session service was closed")
        try:
            _release(client, handle)
        except BaseException as exc:
            failure.add_note(
                f"provider session cleanup also failed: {type(exc).__name__}"
            )
        raise failure

    def _collect(self, lease: SessionLease) -> bool:
        if lease.retired and not lease.borrowers and not lease.released:
            lease.released = True
            self._leases.remove(lease)
            return True
        return False

    def release(self, lease: SessionLease) -> None:
        with self._lock:
            lease.retired = True
            release = self._collect(lease)
        if release:
            _release(lease.client, lease.handle)

    def remember_session(
        self, key: str, lease: SessionLease, opening: str
    ) -> None:
        self.check()
        if self.store.names_only:
            lease.close()
            return
        with self._lock:
            self.check()
            displaced = self._remembered.get(key)
            self._remembered[key] = lease, opening
        if displaced is not None and displaced[0] is not lease:
            try:
                displaced[0].close()
            except Exception as exc:
                logger.warning(
                    "could not release the session displaced under %s: %s",
                    key,
                    exc,
                )

    def take_session(
        self, key: str, client: Any
    ) -> tuple[SessionLease, str] | None:
        self.check()
        with self._lock:
            held = self._remembered.get(key)
            if held is None or held[0].client is not client:
                return None
            del self._remembered[key]
            return held

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._remembered.clear()
            leases = list(self._leases)
        failure = None
        for lease in leases:
            try:
                lease.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(
                        "another provider session failed to close: "
                        f"{type(exc).__name__}"
                    )
        if failure is not None:
            raise failure


class ExecutionCache:
    """Compose response storage and execution-owned sessions explicitly.

    Unknown attributes forward only to the response store, preserving its
    public read/adoption API. Provider session operations are accessed through
    ``sessions``; the response store itself owns no files or handles for them.
    Its exclusive process lock remains unchanged. Callers stop their engines
    before closing these resources; close never closes an engine or its lock.
    """

    def __init__(self, responses: EngineCache) -> None:
        self.responses = responses
        self.sessions = SessionPool(
            SessionStore(
                responses.root, names_only=isinstance(responses, CacheAsIs)
            )
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.responses, name)

    def close(self) -> None:
        self.sessions.close()


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
        engine: Any,
        lease: SessionLease | None = None,
        origin: str | None = None,
    ) -> None:
        self.components = components
        self.chain = (call_key(components),)
        self.prompt = prompt
        self.response = response
        self.opening_sha256 = sha256_text(response)
        self.engine = engine
        self.lease = lease
        self.origin = origin
        self.changed = False
        self.failed: str | None = None
        self.lock = threading.Lock()

    @property
    def session(self) -> Any:
        """The provider's opaque handle, absent until acquired."""
        return None if self.lease is None else self.lease.handle


class ConversationalCachingClient(CachingClient):
    """A caching client for an engine that holds conversations (ADR-0006).

    Opening a conversation is a call like any other. A fork is keyed on the
    keys of the conversation it forks and on its opening answer, and a
    fork the cache answers needs no session. A fork the cache cannot
    answer, of a conversation whose opening turn it did answer, needs the
    engine to hold that conversation exactly as the fork's context
    assumes:

    * the conversation is resumed exactly, without a call, from what the
      session service kept when it was opened (llm.ResumableLlmClient):
      the files of an engine that stores sessions, kept under the opening
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

    def __init__(
        self, client, cache, *, role, events=None, sessions: SessionPool
    ):
        super().__init__(client, cache, role=role, events=events)
        self.sessions = sessions

    def _check_cancelled(self) -> None:
        super()._check_cancelled()
        self.sessions.check()

    def open_conversation(self, prompt: str) -> tuple[str, Conversation]:
        components = self.components(OPEN, prompt)
        sessions: list[SessionLease] = []

        def ask(miss: Callable[[], None]) -> tuple[str, dict[str, Any]]:
            miss()
            self.sessions.check()
            response, session = self.client.open_conversation(prompt)
            sessions.append(self.sessions.own(self._engine, session))
            return response, {}

        try:
            response, asked = self._serve(OPEN, components, ask)
        except BaseException as exc:
            for session in sessions:
                self._release(session, exc)
            raise
        if asked:
            conversation = Conversation(
                components,
                prompt,
                str(response),
                self._engine,
                sessions[0],
                LIVE,
            )
            try:
                self._keep(
                    response.key, conversation.opening_sha256, sessions[0]
                )
            except BaseException as exc:
                self._release(sessions[0], exc)
                raise
            return response, conversation
        conversation = Conversation(
            components, prompt, str(response), self._engine
        )
        held = self.sessions.take_session(response.key, self._engine)
        if held is not None:
            session, opening = held
            if opening == conversation.opening_sha256:
                conversation.lease = session
                conversation.origin = RECOMPUTED
            else:
                # made on another answer than the one served, which a
                # racing call or replay has since stored under the key
                self._release(session)
        return response, conversation

    def fork(self, handle: Any, prompt: str) -> str:
        if not isinstance(handle, Conversation):
            raise TypeError("fork takes a conversation this client opened")
        if handle.engine is not self._engine:
            raise ValueError("the conversation belongs to another engine")

        def ask(miss: Callable[[], None]) -> tuple[str, dict[str, Any]]:
            session = self._session(handle)
            with session.borrow() as provider_handle:
                miss()
                response = self.client.fork(provider_handle, prompt)
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
        self._check_cancelled()
        try:
            with session.borrow() as provider_handle:
                files = keep(provider_handle)
        except (OSError, ValueError) as exc:
            self._check_cancelled()
            logger.warning("could not keep the session of %s: %s", key, exc)
            return
        self._check_cancelled()
        try:
            self.sessions.check()
            self.sessions.store.keep_session(key, opening, files)
        except (OSError, ValueError) as exc:
            self._check_cancelled()
            logger.warning("could not keep the session of %s: %s", key, exc)

    def _session(self, conversation: Conversation) -> Any:
        """The engine's handle for `conversation` (see the class
        docstring): resumed exactly, from the session kept for its opening
        answer, or replayed and found the same; a replay that differs
        raises ConversationChanged. A replay that fails raises, and the
        failure is remembered: the conversation's later forks raise it
        again at once, rather than asking the engine once per fork. A
        replay the safety classifier refused (ClassifierRefused) raises a
        RuntimeError that names the refusal, as every later fork's does:
        it is a failure of the conversation's session, which a fork's
        caller falls back from, never the refusal of the fork itself.

        A replay is asked through the client's replay_conversation where
        it has one (a client that reports its calls: the replay's call
        event says it is one), else as an opening turn. One that matches
        leaves the entry as it was, with what the original call's stream
        reported; one that differs stores its own answer, with what its
        own stream reported."""
        with self._session_lock(conversation):
            self._check_cancelled()
            key = conversation.chain[-1]
            if conversation.changed:
                raise _changed(key)
            if conversation.failed is not None:
                raise RuntimeError(
                    f"the replay of the conversation opened under {key} "
                    f"failed earlier in this pass: {conversation.failed}"
                )
            if conversation.lease is not None:
                return conversation.lease
            self.sessions.check()
            resume = getattr(self.client, "resume_conversation", None)
            if callable(resume):
                try:
                    session = resume(
                        conversation.prompt,
                        conversation.response,
                        self.sessions.store.kept_session(
                            key, conversation.opening_sha256
                        ),
                    )
                except Exception as exc:  # nothing kept, or unusable
                    self._check_cancelled()
                    logger.info("could not resume %s: %s", key, exc)
                else:
                    session = self.sessions.own(self._engine, session)
                    try:
                        self._check_cancelled()
                    except BaseException as exc:
                        self._release(session, exc)
                        raise
                    conversation.lease = session
                    conversation.origin = RESUMED
                    self._emit(RESUME, call=OPEN, key=key)
                    return session
            replay = getattr(self.client, "replay_conversation", None)
            if not callable(replay):
                replay = self.client.open_conversation
            # a replay that answers differently is stored under the
            # opening turn's key, whose folder may be another than the
            # fork's
            self.cache.check_storable(key)
            try:
                self.sessions.check()
                response, session = replay(conversation.prompt)
                session = self.sessions.own(self._engine, session)
            except ClassifierRefused as exc:
                # the refusal is of the replay, not of the fork that needed
                # it: every fork of the conversation sees the same failure
                # of its session, which its caller can fall back from
                conversation.failed = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(
                    f"the replay of the conversation opened under {key} "
                    f"was refused: {exc}"
                ) from exc
            except Exception as exc:
                conversation.failed = f"{type(exc).__name__}: {exc}"
                raise
            try:
                self._check_cancelled()
            except BaseException as exc:
                self._release(session, exc)
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
                conversation.lease = session
                conversation.origin = REPLAYED
                self._keep(key, conversation.opening_sha256, session)
                return session
            try:
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
                self.sessions.remember_session(key, session, opening)
            except BaseException as exc:
                self._release(session, exc)
                raise
            conversation.changed = True
            raise _changed(key)

    def close_conversation(self, handle: Any) -> None:
        """Release the engine's session of a conversation, if it holds
        one."""
        if isinstance(handle, Conversation):
            if handle.engine is not self._engine:
                raise ValueError("the conversation belongs to another engine")
            if handle.lease is not None:
                handle.lease.close()

    @contextlib.contextmanager
    def _session_lock(self, conversation: Conversation):
        """Let a waiting fork stop while another fork restores its session."""
        self._check_cancelled()
        self.sessions.check()
        while not conversation.lock.acquire(timeout=0.05):
            self._check_cancelled()
            self.sessions.check()
        try:
            self._check_cancelled()
            self.sessions.check()
            yield
        finally:
            conversation.lock.release()

    def _release(
        self, session: SessionLease, failure: BaseException | None = None
    ) -> None:
        try:
            session.close()
        except BaseException as exc:
            if failure is None:
                raise
            failure.add_note(
                f"provider session cleanup also failed: {type(exc).__name__}"
            )


def _changed(key: str) -> ConversationChanged:
    return ConversationChanged(
        f"the conversation opened under {key} answered differently when "
        "it was replayed"
    )
