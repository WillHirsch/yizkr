"""Copied provider checkpoints own separate runtime files and exact history."""

from __future__ import annotations

import copy
import gc
import json
import shutil
import stat
import uuid
from pathlib import Path

import pytest

from tests.unit.test_claude_code_pinning import install_fake_cli
from yizkr.pipeline import llm
from yizkr.pipeline.cancellation import Cancellation, RunCancelled


@pytest.fixture
def cli(monkeypatch, tmp_path):
    return install_fake_cli(monkeypatch, tmp_path)


@pytest.fixture
def checkpoint():
    session, parent, message = (str(uuid.uuid4()) for _ in range(3))
    stem = f"fictional-project/{session}"
    rows = [
        {
            "type": "user",
            "sessionId": session,
            "uuid": parent,
            "parentUuid": None,
            "message": {"role": "user", "content": "fictional context"},
        },
        {
            "type": "assistant",
            "sessionId": session,
            "uuid": message,
            "parentUuid": parent,
            "message": {"role": "assistant", "content": "recorded answer"},
        },
    ]
    return {
        f"{stem}.jsonl": ("\n".join(map(json.dumps, rows)) + "\n").encode(),
        f"{stem}/tool-results/result.txt": b"opaque saved tool result\x00\xff",
        f"{stem}/snapshot/state.jsonl": b'{"parentUuid":"unchanged"}\n',
    }


def restore(engine, files):
    return engine.resume_conversation(
        "fictional context", "recorded answer", files
    )


def fork_path(engine, handle, cli):
    assert engine.fork(handle, "fictional follow-up") == "answer"
    command, _kwargs = cli.calls[-1]
    assert "--fork-session" in command
    return Path(command[command.index("--resume") + 1])


def test_copied_checkpoints_keep_ancestry_and_have_independent_lifetimes(
    cli, checkpoint
):
    original = copy.deepcopy(checkpoint)
    first, second = llm.ClaudeCodeClient(), llm.ClaudeCodeClient()
    a, b = (
        restore(first, checkpoint),
        restore(second, copy.deepcopy(checkpoint)),
    )
    assert a != b
    assert cli.calls == []  # Restoration itself makes no provider call.
    path_a, path_b = fork_path(first, a, cli), fork_path(second, b, cli)
    assert path_a != path_b
    for path in (path_a, path_b):
        assert path.is_absolute() and not path.is_relative_to(cli.projects)
        assert path.read_bytes() == next(iter(checkpoint.values()))
        assert stat.S_IMODE(path.parents[1].stat().st_mode) == 0o700
    assert (
        first.keep_conversation(a) == second.keep_conversation(b) == original
    )
    first.close_conversation(a)
    assert not path_a.exists()
    assert path_b.exists() and second.keep_conversation(b) == original
    assert fork_path(second, b, cli) == path_b
    second.close()
    assert not path_b.exists()
    assert checkpoint == original


def test_restore_never_overwrites_existing_shared_session(cli, checkpoint):
    name = next(iter(checkpoint))
    existing = cli.projects / name
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"a separately owned live session")
    client = llm.ClaudeCodeClient()
    handle = restore(client, checkpoint)
    private = fork_path(client, handle, cli)
    client.close_conversation(handle)
    assert existing.read_bytes() == b"a separately owned live session"
    assert not private.exists()


def test_archived_restores_are_kept_under_their_owned_handles(
    cli, checkpoint, tmp_path
):
    record = tmp_path / "transcripts"
    client = llm.ClaudeCodeClient(transcripts=record)
    a, b = restore(client, checkpoint), restore(client, checkpoint)
    client.close()
    for handle in (a, b):
        assert {
            path.relative_to(record / handle).as_posix(): path.read_bytes()
            for path in (record / handle).rglob("*")
            if path.is_file()
        } == checkpoint


def test_garbage_collection_releases_only_the_private_restore(cli, checkpoint):
    client = llm.ClaudeCodeClient()
    handle = restore(client, checkpoint)
    private = fork_path(client, handle, cli)
    del client
    gc.collect()
    assert not private.exists()


@pytest.mark.parametrize("failure", ["write", "cancel"])
def test_interrupted_restore_cleans_partial_materialization(
    cli, checkpoint, monkeypatch, tmp_path, failure
):
    token = Cancellation()
    client = llm.ClaudeCodeClient(cancellation=token)
    original_open = Path.open
    writes = []

    def interrupted(path, mode="r", *args, **kwargs):
        if mode == "xb":
            writes.append(path)
            if len(writes) == 2:
                if failure == "write":
                    raise OSError("fictional full disk")
                token.cancel()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupted)
    with pytest.raises(OSError if failure == "write" else RunCancelled):
        restore(client, checkpoint)
    assert writes
    assert not list((tmp_path / "tmp").glob("yizkr-claude-session-*"))
    assert cli.calls == []
    assert client._restored == {}


@pytest.mark.parametrize(
    "bad_name",
    ["../outside", "/absolute", "fictional-project/other/file", "a/../b"],
)
def test_unowned_sidefiles_are_refused_before_materialization(
    cli, checkpoint, tmp_path, bad_name
):
    client = llm.ClaudeCodeClient()
    with pytest.raises(ValueError):
        restore(client, {**checkpoint, bad_name: b"unowned"})
    assert not list((tmp_path / "tmp").glob("yizkr-claude-session-*"))
    assert cli.calls == []


def test_other_clients_cannot_borrow_or_release_a_live_handle(cli):
    owner, other = llm.ClaudeCodeClient(), llm.ClaudeCodeClient()
    _, handle = owner.open_conversation("fictional context")
    kept = owner.keep_conversation(handle)
    assert kept
    assert other.keep_conversation(handle) == {}
    with pytest.raises(ValueError, match="not owned"):
        other.fork(handle, "fictional follow-up")
    other.close_conversation(handle)
    assert owner.keep_conversation(handle) == kept
    owner.close()


def test_fork_stream_cannot_release_restored_ancestors(
    cli, checkpoint, monkeypatch
):
    name = next(iter(checkpoint))
    original = cli.projects / name
    original.parent.mkdir(parents=True)
    original.write_bytes(b"another owner's original transcript")
    session = Path(name).stem
    client = llm.ClaudeCodeClient()
    handle = restore(client, checkpoint)
    original_cli = llm._run_process

    def with_ancestor(*args, **kwargs):
        result = original_cli(*args, **kwargs)
        result.stdout = (
            json.dumps({"session_id": session}) + "\n" + result.stdout
        )
        return result

    monkeypatch.setattr(llm, "_run_process", with_ancestor)
    fork_path(client, handle, cli)
    client.close()
    assert original.read_bytes() == b"another owner's original transcript"


def test_copied_caches_restore_separate_provider_prefixes(cli, tmp_path):
    seed_root = tmp_path / "seed-cache"
    seed = llm.open_cache(seed_root)
    first_engine = llm.ClaudeCodeClient()
    first = llm.cached_client(first_engine, seed, role="extract")
    response, _ = first.open_conversation("fictional context")
    first_engine.close()
    seed.close()
    a_root, b_root = tmp_path / "cache-copy", tmp_path / "restored-backup"
    shutil.copytree(seed_root, a_root)
    shutil.copytree(seed_root, b_root)
    a_resources, b_resources = llm.open_cache(a_root), llm.open_cache(b_root)
    a_engine, b_engine = llm.ClaudeCodeClient(), llm.ClaudeCodeClient()
    a = llm.cached_client(a_engine, a_resources, role="extract")
    b = llm.cached_client(b_engine, b_resources, role="extract")
    calls = len(cli.calls)
    try:
        a_response, a_handle = a.open_conversation("fictional context")
        b_response, b_handle = b.open_conversation("fictional context")
        assert a_response == b_response == response
        assert len(cli.calls) == calls
        assert a.fork(a_handle, "first follow-up") == "answer"
        a_command = cli.calls[-1][0]
        a_path = Path(a_command[a_command.index("--resume") + 1])
        assert b.fork(b_handle, "second follow-up") == "answer"
        b_command = cli.calls[-1][0]
        b_path = Path(b_command[b_command.index("--resume") + 1])
        assert a_path != b_path
        assert a_path.read_bytes() == b_path.read_bytes()
        a_engine.close()
        a_resources.close()
        assert not a_path.exists() and b_path.exists()
        assert b.fork(b_handle, "third follow-up") == "answer"
        assert len(cli.calls) == calls + 3
    finally:
        a_engine.close()
        b_engine.close()
        a_resources.close()
        b_resources.close()


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_absolute_original_sidefile_reference_requires_replay(
    cli, checkpoint, separator
):
    name = next(iter(checkpoint))
    folder = name.removesuffix(".jsonl")
    path = f"/another-environment/projects/{folder}/tool-results/result.txt"
    content = json.dumps({"content": path.replace("/", separator)}).encode()
    unsafe = {**checkpoint, name: content}
    client = llm.ClaudeCodeClient()
    with pytest.raises(ValueError, match="absolute original"):
        restore(client, unsafe)
    assert client._restored == {}
    assert cli.calls == []


@pytest.mark.parametrize("changed", [False, True])
def test_nonportable_checkpoint_replay_keeps_opening_equality_guard(
    cli, checkpoint, tmp_path, monkeypatch, changed
):
    name = next(iter(checkpoint))
    folder = name.removesuffix(".jsonl")
    unsafe = {
        **checkpoint,
        name: (
            f"/another-environment/projects/{folder}/tool-results/result.txt"
        ).encode(),
    }
    root = tmp_path / "cache"
    resources = llm.open_cache(root)
    first_engine = llm.ClaudeCodeClient()
    monkeypatch.setattr(
        first_engine, "keep_conversation", lambda _handle: unsafe
    )
    first = llm.cached_client(first_engine, resources, role="extract")
    first.open_conversation("fictional context")
    first_engine.close()
    resources.close()
    later_resources = llm.open_cache(root)
    later_engine = llm.ClaudeCodeClient()
    later = llm.cached_client(later_engine, later_resources, role="extract")
    calls = len(cli.calls)
    try:
        _, handle = later.open_conversation("fictional context")
        assert len(cli.calls) == calls
        if changed:
            cli.answer = "different opening"
            with pytest.raises(llm.ConversationChanged):
                later.fork(handle, "fictional follow-up")
            assert len(cli.calls) == calls + 1
        else:
            assert later.fork(handle, "fictional follow-up") == "answer"
            assert len(cli.calls) == calls + 2
            assert "--resume" not in cli.calls[-2][0]
            assert "--resume" in cli.calls[-1][0]
    finally:
        later_engine.close()
        later_resources.close()
