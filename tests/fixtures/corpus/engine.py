"""The recorded engine: a fake of the claude-code engine that answers the
fixture corpus's calls from its recorded answers (answers.py).

It answers every call the stages before write make of a document of the
corpus, and tells the document by its bytes: a vision call by the SHA-1 of
the image it is given, an extraction call by the artifact SHA-1 its prompt
names, and a critique call by the entity manifest its prompt lists. It
holds conversations as the claude-code engine does: each is a session,
whose transcript it gives to be kept (one .jsonl file holding the opening
turn) and resumes only from such a transcript of the same turn, and it
forks only a session it holds, for the document the fork's prompt names.
So a fork behind the LLM cache resumes a conversation the cache answered
without a call, from the transcript the cache kept (ADR-0032 §6). The text
a call sends is the claude-code engine's own (`call_text`): an image's
path goes in the prompt, as the cache's key leaves it out. Every call it
answers is logged, as (what, document), with what one of vision,
manifest, chunk or critique. A call it has no answer for fails the test
that made it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tests.fixtures.corpus import FOLDER
from tests.fixtures.corpus.answers import (
    RECORDED,
    SOURCES,
    Recorded,
    claims,
    manifest,
    mistakes_in,
)
from tests.fixtures.records import record_answer

from yizkr.pipeline.llm import ClaudeCodeClient
from yizkr.pipeline.transcription_record import document_classes

CLI = "9.9.9"
# what each kind of call's prompt says, as the pipeline words it
MANIFEST_PROMPT = "manifest of EVERY distinct entity"
CRITIQUE_PROMPT = "You review a genealogical extraction"
# the category the recorded critic files a misread year under
MISREAD = "implausible_value"
SEGMENTATION_PROMPT = "You segment a document transcription"
ASSIGNED = "Assigned entities:"
_SHA1 = re.compile(r"[0-9a-f]{40}")
_ASSIGNED_ID = re.compile(r"^- (\w+): ", re.MULTILINE)
_ENTITY_ROW = re.compile(r"^(\w+) \| [^|\n]+ \| ([^|\n]+?) \| ", re.MULTILINE)


class NoAnswer(AssertionError):
    """The recorded engine was asked something it has no answer for."""


@cache
def recorded_by_sha1() -> dict[str, Recorded]:
    """Each recorded document, by the SHA-1 of its committed bytes."""
    return {
        hashlib.sha1((FOLDER / name).read_bytes()).hexdigest(): recorded
        for name, recorded in RECORDED.items()
    }


class Calls:
    """What the recorded engine was asked, in order, as (what, document)."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def log(self, what: str, document: str) -> None:
        with self._lock:
            self.asked.append((what, document))

    def of(self, what: str) -> list[str]:
        return [document for kind, document in self.asked if kind == what]


@dataclass(frozen=True)
class Session:
    """A conversation the recorded engine holds: the document it is about
    (by SHA-1) and its opening turn."""

    sha1: str
    prompt: str
    answer: str

    def transcript(self) -> bytes:
        """The session's transcript, as a CLI keeps one: a JSON line a
        turn."""
        turns = ({"prompt": self.prompt}, {"answer": self.answer})
        return "".join(json.dumps(turn) + "\n" for turn in turns).encode()


class RecordedEngine:
    """What the orchestrator reads off an engine client (its engine, model,
    effort, configuration hash and CLI version), and the recorded answers.
    `calls` logs what it answers."""

    engine = "claude-code"
    system_prompt = "You answer for the fixture corpus's recorded model."
    call_text = ClaudeCodeClient.call_text

    def __init__(self, calls: Calls, *, effort=None, model=None) -> None:
        self.calls = calls
        self.effort = effort
        self.model = model or os.environ.get("YIZKR_LLM_MODEL")
        self.cli_version = CLI
        self.config_hash = f"recorded-{self.model}-{effort}"
        self.model_identifier = f"claude-code/{CLI}:{self.model}"
        # session id -> the conversation it holds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # -- telling the document --------------------------------------------

    @staticmethod
    def _named(prompt: str) -> tuple[Recorded, str]:
        """(the document an extraction prompt is about, its SHA-1): the one
        whose SHA-1 the prompt names."""
        known = recorded_by_sha1()
        named = {sha1 for sha1 in _SHA1.findall(prompt) if sha1 in known}
        if len(named) != 1:
            raise NoAnswer("the prompt names no one recorded document")
        (sha1,) = named
        return known[sha1], sha1

    @staticmethod
    def _critiqued(prompt: str) -> Recorded:
        """The document whose batch a critique prompt lists: the one whose
        manifest has exactly the listed entities."""
        listed = set(_ENTITY_ROW.findall(prompt))
        for recorded in RECORDED.values():
            if listed == {(e.id, e.label) for e in recorded.entities}:
                return recorded
        raise NoAnswer("the critique lists no recorded document's entities")

    # -- answers -----------------------------------------------------------

    def _manifest(self, prompt: str) -> str:
        recorded, _sha1 = self._named(prompt)
        self.calls.log("manifest", recorded.document)
        return json.dumps(manifest(recorded), ensure_ascii=False)

    def _chunk(self, prompt: str) -> str:
        """The claims of the entities the chunk is assigned, and its
        sentinel."""
        recorded, sha1 = self._named(prompt)
        self.calls.log("chunk", recorded.document)
        assigned = set(_ASSIGNED_ID.findall(prompt.partition(ASSIGNED)[2]))
        sharper = all(m.rule in prompt for m in mistakes_in(recorded))
        answered = [
            claim
            for claim in claims(recorded, sha1, sharper=sharper)
            if claim["subject"]["local_id"] in assigned
        ]
        lines = [json.dumps(claim, ensure_ascii=False) for claim in answered]
        sentinel = {"done": True, "claims_emitted": len(answered)}
        return "\n".join([*lines, json.dumps(sentinel | {"gaps_emitted": 0})])

    def _critique(self, prompt: str) -> str:
        """The critic's findings: one for each recorded mistake the batch
        holds, on its claim as the prompt lists it, and its sentinel."""
        recorded = self._critiqued(prompt)
        self.calls.log("critique", recorded.document)
        ids = recorded.ids()
        findings = []
        for mistake in mistakes_in(recorded):
            subject = ids[mistake.subject]
            listed = [
                line.split(" | ", 2)[2]
                for line in prompt.splitlines()
                if line.startswith(f"{subject} | {mistake.property} | ")
            ]
            year = str(mistake.read_as.year)
            for value in (v for v in listed if v.startswith(year)):
                findings.append(
                    {
                        "entity": subject,
                        "claim": {
                            "subject": subject,
                            "property": mistake.property,
                            "value": value,
                        },
                        "axis": "value",
                        "category": MISREAD,
                        "issue": mistake.flag,
                        "suggestion": {"note": "read the year again"},
                    }
                )
        lines = [json.dumps(finding) for finding in findings]
        return "\n".join([*lines, json.dumps({"done": True})])

    # -- the engine's calls --------------------------------------------------

    def complete(
        self, prompt: str, image_paths: Sequence[str | os.PathLike] = ()
    ) -> str:
        if image_paths:
            (path,) = image_paths
            sha1 = hashlib.sha1(Path(path).read_bytes()).hexdigest()
            recorded = recorded_by_sha1().get(sha1)
            if recorded is None or not recorded.read:
                raise NoAnswer("no recorded reading of this image")
            self.calls.log("vision", recorded.document)
            # the transcription record (ADR-0037): the source's class
            # where it is one of the record's, else unclassified
            source = SOURCES[recorded.source]
            document_class = source.artifact_class
            if document_class not in document_classes():
                document_class = "unclassified"
            return record_answer(
                source.text,
                document_class=document_class,
                languages=[source.lang] if source.lang else [],
            )
        if prompt.startswith(CRITIQUE_PROMPT):
            return self._critique(prompt)
        if MANIFEST_PROMPT in prompt:
            return self._manifest(prompt)
        if prompt.startswith(SEGMENTATION_PROMPT):
            raise NoAnswer("the recorded model gives no segmentation")
        return self._chunk(prompt)

    def _hold(self, session: str, prompt: str, answer: str) -> str:
        _recorded, sha1 = self._named(prompt)
        with self._lock:
            if session in self._sessions:
                raise ValueError(f"the session {session} is open already")
            self._sessions[session] = Session(sha1, prompt, answer)
        return session

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        """The entity manifest, as the opening turn of a new session; the
        handle is the session's id."""
        if MANIFEST_PROMPT not in prompt:
            raise NoAnswer("a conversation opens on the entity manifest")
        answer = self._manifest(prompt)
        return answer, self._hold(str(uuid.uuid4()), prompt, answer)

    def fork(self, handle: Any, prompt: str) -> str:
        """A chunk, forked from a session held for its document."""
        _recorded, sha1 = self._named(prompt)
        with self._lock:
            session = self._sessions.get(handle)
        if session is None or session.sha1 != sha1:
            raise NoAnswer("a fork of no session held for its document")
        return self._chunk(prompt)

    def keep_conversation(self, handle: Any) -> dict[str, bytes]:
        """The session's transcript, one file; none for a session not
        held."""
        with self._lock:
            session = self._sessions.get(handle)
        if session is None:
            return {}
        return {f"recorded/{handle}.jsonl": session.transcript()}

    def resume_conversation(
        self, prompt: str, response: str, files: Mapping[str, bytes]
    ) -> Any:
        """The session a kept transcript holds, held again, when it holds
        this opening turn; ValueError otherwise."""
        names = [name for name in files if name.endswith(".jsonl")]
        if len(names) != 1:
            raise ValueError("the kept files hold no single transcript")
        (name,) = names
        if files[name] != Session("", prompt, response).transcript():
            raise ValueError("the transcript holds another opening turn")
        return self._hold(Path(name).stem, prompt, response)

    def close_conversation(self, handle: Any) -> None:
        with self._lock:
            self._sessions.pop(handle, None)
