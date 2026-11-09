"""Fictional document processing through the production scratch scheduler.

Only model responses and environment preflight are replaced. Source discovery,
container handling, stage planning, publication and cancellation run normally.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.scratch import ScratchDriver

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-14T09:00:00Z",
}
LETTER_REF = ArtifactRef.from_bytes(b"Rahel was born in 1871.")


def make_batch(entities=True) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e1",
                        "label": "Rahel",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1871},
                    "source": {"artifact": LETTER_REF.model_dump()},
                }
            ],
            "entities": [{"id": "e1", "label": "Rahel", "kind": "human"}]
            if entities
            else [],
        }
    )


def _person(prompt: str) -> tuple[str, int]:
    """The first word and year of a prompt's first numbered line."""
    match = re.search(r"^1: (.*)$", prompt, re.M)
    line = match.group(1) if match else ""
    words = re.findall(r"[A-Za-z]+", line)
    year = re.search(r"\b(1[5-9]\d\d)\b", line)
    return (words[0] if words else "Someone"), (
        int(year.group(1)) if year else 1900
    )


class ScriptedModel:
    """A scripted engine: answers the entity-manifest, chunk and critique
    prompts from the document text it is shown, one person per document,
    and logs each call as '<role>:<file name>'. It has no conversation
    capability, so chunks see the document inline."""

    model_identifier = "fake-model"

    def __init__(self, calls: list[str], name: str, fail: str | None = None):
        self.calls, self.name, self.fail = calls, name, fail

    def complete(self, prompt, image_paths=()):
        if "manifest of EVERY distinct entity" in prompt:
            role = "manifest"
        elif prompt.startswith("You review a genealogical extraction"):
            role = "critique"
        else:
            role = "chunk"
        self.calls.append(f"{role}:{self.name}")
        if role == self.fail:
            raise RuntimeError(f"{role} engine down")
        person, year = _person(prompt)
        if role == "manifest":
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": person,
                            "kind": "human",
                            "mention_lines": [1],
                        }
                    ]
                }
            )
        if role == "critique":
            return '{"done": true, "findings_emitted": 0}'
        # the artifact as the prompt names it, by the SHA-1 of its media
        sha1 = re.search(r'"sha1": "([0-9a-f]{40})"', prompt).group(1)
        claim = {
            "subject": {"kind": "local", "local_id": "e1", "label": person},
            "property": "date_of_birth",
            "value": {"type": "time", "year": year},
            "source": {"artifact": {"kind": "artifact", "sha1": sha1}},
        }
        return "\n".join(
            [json.dumps(claim), '{"done": true, "claims_emitted": 1}']
        )


def steps(calls: list[str]) -> list[str]:
    return [call.split(":")[0] for call in calls]


def quiet(*args, **kwargs):
    pass


def make_run(tmp_path, name="letter.txt", **kwargs):
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text("Rahel was born in 1871.")
    kwargs.setdefault("log", quiet)
    orchestrator = ScriptedDriver(folder, tmp_path / "state", **kwargs)
    return orchestrator


def collect(lines: list[str]):
    """A log that keeps each message."""

    def log(*args, **kwargs):
        lines.append(" ".join(str(arg) for arg in args))

    return log


def saved_last(path) -> None:
    """Give `path` a modification time after every other file beside it,
    as a file saved after them has. Files written moments apart can share
    a timestamp on a coarse filesystem clock, so the tests set it."""
    newest = max(
        other.stat().st_mtime_ns
        for other in path.parent.iterdir()
        if other.is_file() and other != path
    )
    stamp = newest + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def snapshot(directory, *, skip=("state.json",)) -> dict:
    """Every file under `directory` but those named in `skip` (its
    state.json, by default), with its bytes and modification time: a run
    that touches none of them leaves this unchanged."""
    return {
        path.relative_to(directory): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name not in skip
    }


def contents(directory) -> dict:
    """Every file under `directory` with its bytes: what a run that
    rewrites nothing, or rewrites it the same, leaves unchanged."""
    return {
        path.relative_to(directory): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def this_run(orchestrator, *names: str) -> list[dict]:
    """The events of these names the orchestrator's invocation wrote."""
    return [
        event
        for event in orchestrator.rundir.events()
        if event["event"] in names
        and event["invocation"] == orchestrator.rundir.invocation
    ]


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@contextlib.contextmanager
def unreadable(path: Path):
    """`path` without permissions while the block runs, restored after it.
    Where permissions do not stop this user's reads (as root), the test is
    skipped."""
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("permissions do not stop this user's reads")
        yield path
    finally:
        path.chmod(mode)


@contextlib.contextmanager
def read_only(directory: Path):
    """`directory` without write permission while the block runs, restored
    after it. Where permissions do not stop this user's writes (as root),
    the test is skipped."""
    mode = directory.stat().st_mode
    directory.chmod(0o500)
    try:
        if os.access(directory, os.W_OK):
            pytest.skip("permissions do not stop this user's writes")
        yield directory
    finally:
        directory.chmod(mode)


def engine_steps(calls: list[str]) -> list[str]:
    return [
        step
        for step in steps(calls)
        if step in {"manifest", "chunk", "critique"}
    ]


class IsolatedDriver(ScratchDriver):
    """Real scratch scheduling with authored responses and local fixtures."""

    def __init__(self, folder, state_dir, *, fail_step=None, **kwargs):
        kwargs.setdefault("exclusions", ExclusionList())
        kwargs.setdefault("cache_dir", Path(state_dir).parent / "cache")
        kwargs.setdefault("until", "critique")
        kwargs.setdefault("corpus_stages", ())
        kwargs.setdefault("min_free", 0)
        super().__init__(folder, state_dir, **kwargs)
        self.fail_step = fail_step
        self.calls = []

    def _preflight_host(self):
        pass

    def _preflight_models(self, past):
        pass


class ScriptedDriver(IsolatedDriver):
    def _llm(self, role, doc):
        return ScriptedModel(self.calls, doc.source.name, fail=self.fail_step)


def completed_run(tmp_path, **kwargs):
    driver = make_run(tmp_path, **kwargs)
    assert driver.run() == 0
    return driver


def prepare_document(driver, doc, *, until="extraction"):
    """Process one document through the driver's stage boundary."""
    for stage in ("transcription", "extraction", "critique"):
        result = driver._dispatch_one(doc, stage)
        if result != "ended" or stage == until:
            break
    return doc.state.status
