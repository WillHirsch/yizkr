"""The LLM cache: model responses reused at the engine boundary (ADR-0032).

Development runs the whole corpus through the pipeline again and again
(ADR-0025). The costly, non-deterministic part of a run is the model
call; everything after it (parsing, merging, validation, the
transcription check) is code that runs in moments. So each response is
kept under a key built from what its call sends, and a call made again
with the same key is answered from the cache. Everything after the call
runs on every run, whether its response came from the engine or from the
cache, so a fix to it costs no model call, and no change needs a version
bump: a change to what a call sends changes its key by itself.

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
* for a fork, the keys of the conversation it forks;
* the role's epoch: 0 until someone re-samples the role on purpose.

Nothing on the engine's server is keyed. The account data the claude CLI
fetches can change its system prompt, its default model and its
compaction, and the CLI fills in details of its own when it runs, such as
the date (yizkr.pipeline.llm). Each entry records the model the engine
reported, so a reuse across such a change can be seen.

Layout under the cache root (runtime state, kept under var/):

    <root>/<first two hex digits of the key>/<key>.json
                                            /<key>.rejected-<stamp>.json
                                            /<key>.replaced-<stamp>.json

``<key>.json`` is the one entry a call is served. It holds the key's
components, the response, its origin (``engine``, or the reason it was
adopted) and its provenance: the role, the engine, the model the engine
reported, the CLI version, the client's config hash and the time. Only a
response the engine returned is stored: an error, a timeout or a pause
(PassPaused) stores nothing. Entries are written atomically, so a reader
sees one whole or not at all, and calls answered in parallel under one
key leave one whole entry. The cache deletes nothing: an entry a caller
rejects, or that a refresh replaces, is moved aside under a name no
lookup reads, and kept for inspection.

docs/LLM_CACHE.md describes the cache, its events and the run directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yizkr.pipeline.llm import ConversationalLlmClient
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

# the kinds of call a key tells apart
COMPLETE = "complete"
OPEN = "open"
FORK = "fork"

# the events a caching client emits
HIT = "cache-hit"
MISS = "cache-miss"
REPLAY = "cache-replay"
REJECT = "cache-reject"
ADOPT = "cache-adopt"
EVENTS = (HIT, MISS, REPLAY, REJECT, ADOPT)

# why a call was answered from the cache, besides an adoption's reason
KEY_MATCH = "key match"
# why a call went to the engine
ABSENT = "absent"
REJECTED = "rejected"
REFRESH = "refresh"

_KEY = re.compile(r"^[0-9a-f]{64}$")


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


class EngineCache:
    """The entries under one cache root, as one run uses them.

    ``refresh`` names roles whose calls go to the engine whatever the
    cache holds, once per key in the run, the new answer replacing the
    entry; ``epochs`` gives roles an epoch other than 0. Nothing is
    written until a call is answered. The root and its subdirectories are
    private to their owner: the entries hold corpus content."""

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

    def put(
        self, key: str, entry: Mapping[str, Any], *, replace: bool = False
    ) -> None:
        """Store `entry` under `key`, atomically. With `replace`, an entry
        the key already has is moved aside first, as replaced."""
        path = self.path(key)
        self._directory(path.parent)
        if replace:
            self._move_aside(path, "replaced", {"replaced": {"at": _now()}})
        atomic_write_text(path, _dump(entry))

    def reject(
        self, key: str, response: str, reason: str, **fields: Any
    ) -> bool:
        """Move the entry under `key` aside, with the reason, if it holds
        `response`; an entry stored since with another answer stays. The
        next call under the key goes to the engine. Returns whether an
        entry was moved."""
        path = self.path(key)
        entry = _read(path, key)
        if entry is None or entry["response"] != response:
            return False
        rejection = {"at": _now(), "reason": reason}
        rejection.update({k: v for k, v in fields.items() if v is not None})
        return self._move_aside(path, "rejected", {"rejected": rejection})

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
        key match, and its provenance is the one given. An entry the key
        already has is kept and nothing is stored. Returns whether the
        response was adopted."""
        path = self.path(key)
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
            try:
                # a link fails where the key already has an entry, so an
                # entry stored meanwhile is never overwritten
                os.link(staging, path)
            except FileExistsError:
                return False
            except OSError:  # a filesystem without hard links
                if path.exists():
                    return False
                os.replace(staging, path)
            return True
        finally:
            staging.unlink(missing_ok=True)

    def _directory(self, directory: Path) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)

    def _move_aside(
        self, path: Path, what: str, note: Mapping[str, Any]
    ) -> bool:
        """Rename the entry at `path` to a name no lookup reads, then add
        `note` to it. Returns whether there was an entry to move."""
        aside = path.with_name(f"{path.stem}.{what}-{_stamp()}.json")
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return False
        try:
            entry = json.loads(aside.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if isinstance(entry, dict):
            entry.update(note)
            atomic_write_text(aside, _dump(entry))
        return True


class CachedResponse(str):
    """A response, and the key it is cached under; in every other respect
    it is the text."""

    key: str

    def __new__(cls, text: str, key: str) -> CachedResponse:
        response = super().__new__(cls, text)
        response.key = key
        return response


class Conversation:
    """A conversation opened through the cache.

    It carries the keys of its turns (its forks' parent chain), the prompt
    and the answer of its opening turn, and the engine's own handle once
    the engine holds the conversation: from the opening call when that
    went to the engine, or from one replay of it when the cache answered
    the opening turn and a fork has to be asked of the engine."""

    def __init__(
        self, key: str, prompt: str, response: str, session: Any = None
    ) -> None:
        self.chain = (key,)
        self.prompt = prompt
        self.response = response
        self.session = session
        self.replayed = False
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

    ``role`` names the pipeline step the calls are made for (transcribe,
    extract, critique): the role's epoch is keyed, and its refresh setting
    applies. ``events``, when given, is called with each event's name and
    fields: a hit (with its reason: a key match, or the adoption's
    reason), a miss (absent, rejected or refresh), a replay, a rejection
    or an adoption."""

    def __init__(
        self,
        client: Any,
        cache: EngineCache,
        *,
        role: str,
        events: Callable[..., Any] | None = None,
    ) -> None:
        if not hasattr(client, "config_hash"):
            raise TypeError(
                f"{type(client).__name__} has no config_hash, so its calls "
                "cannot be keyed"
            )
        self.client = client
        self.cache = cache
        self.role = role
        self._events = events
        if getattr(client, "model", "") is None:
            cache.warn_unpinned(role)

    @property
    def model_identifier(self) -> str:
        return self.client.model_identifier

    # -- keys ---------------------------------------------------------------

    def components(
        self,
        call: str,
        prompt: str,
        image_paths: Sequence[str | os.PathLike[str]] = (),
        parent: Sequence[str] = (),
    ) -> dict[str, Any]:
        """The components of a call's key (see the module docstring)."""
        client = self.client
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
            "model": getattr(client, "model", None),
            "effort": getattr(client, "effort", None),
            "config": client.config_hash,
            "call": call,
            "system": None if system is None else sha256_text(system),
            "prompt": sha256_text(text),
            "files": files,
            "parent": list(parent),
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
        return self._serve(
            COMPLETE,
            self.components(COMPLETE, prompt, image_paths),
            lambda: (self.client.complete(prompt, image_paths), {}),
        )

    def _serve(
        self,
        call: str,
        components: dict[str, Any],
        ask: Callable[[], tuple[str, dict[str, Any]]],
    ) -> CachedResponse:
        """The cached answer to a call, or the engine's, which is then
        stored. `ask` makes the call and returns its response with any
        provenance of its own; whatever it raises propagates, and nothing
        is stored."""
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
                return CachedResponse(entry["response"], key)
        if refresh:
            reason = REFRESH
        elif self.cache.rejected(key):
            reason = REJECTED
        else:
            reason = ABSENT
        self._emit(MISS, call=call, key=key, reason=reason)
        response, provenance = ask()
        self.cache.put(
            key,
            self._entry(key, components, response, provenance),
            replace=refresh,
        )
        if refresh:
            self.cache.refreshed(key)
        return CachedResponse(response, key)

    def _entry(
        self,
        key: str,
        components: dict[str, Any],
        response: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        """A new entry for a response the engine has just returned."""
        last_report = getattr(self.client, "last_report", None)
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
                or getattr(self.client, "cli_version", None),
                "config_hash": components["config"],
                "model_identifier": self.client.model_identifier,
                "at": _now(),
                **provenance,
            },
        }

    # -- rejection and adoption ---------------------------------------------

    def reject(self, response: str, reason: str) -> None:
        """Reject `response`, which this client returned: the entry under
        its key is moved aside with `reason` and never served again, so
        the next identical call asks the engine."""
        key = getattr(response, "key", None)
        if not isinstance(key, str):
            raise ValueError(
                "only a response this client returned can be rejected"
            )
        self.cache.reject(key, str(response), reason, role=self.role)
        self._emit(REJECT, key=key, reason=reason[:500])

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
    keys of the conversation it forks, and a fork the cache answers needs
    no session. A fork the cache cannot answer, of a conversation whose
    opening turn it did answer, has no session to fork: the opening turn
    is replayed once, for real, and the session it gives serves that fork
    and every later one of the conversation. The replay's own answer is
    not stored, since the cached one stands, and it may differ from it: the
    replay event says whether it matched."""

    def open_conversation(self, prompt: str) -> tuple[str, Conversation]:
        sessions: list[Any] = []

        def ask() -> tuple[str, dict[str, Any]]:
            response, session = self.client.open_conversation(prompt)
            sessions.append(session)
            return response, {}

        response = self._serve(OPEN, self.components(OPEN, prompt), ask)
        conversation = Conversation(
            response.key,
            prompt,
            str(response),
            sessions[0] if sessions else None,
        )
        return response, conversation

    def fork(self, handle: Any, prompt: str) -> str:
        if not isinstance(handle, Conversation):
            raise TypeError("fork takes a conversation this client opened")

        def ask() -> tuple[str, dict[str, Any]]:
            session = self._session(handle)
            response = self.client.fork(session, prompt)
            return response, {
                "session": "replayed" if handle.replayed else "original"
            }

        return self._serve(
            FORK, self.components(FORK, prompt, parent=handle.chain), ask
        )

    def _session(self, conversation: Conversation) -> Any:
        """The engine's handle for `conversation`, replaying its opening
        turn once when the cache answered it."""
        with conversation.lock:
            if conversation.session is None:
                response, session = self.client.open_conversation(
                    conversation.prompt
                )
                conversation.session = session
                conversation.replayed = True
                self._emit(
                    REPLAY,
                    call=OPEN,
                    key=conversation.chain[-1],
                    matches=response == conversation.response,
                )
            return conversation.session

    def close_conversation(self, handle: Any) -> None:
        """Release the engine's session of a conversation, if it holds
        one."""
        if not isinstance(handle, Conversation) or handle.session is None:
            return
        close = getattr(self.client, "close_conversation", None)
        if callable(close):
            close(handle.session)


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
