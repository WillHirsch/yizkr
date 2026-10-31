"""The response lock stays exclusive across aliases, replacement and fork."""

import os
import select
import signal
import threading

from tests.unit.test_llmcache import lock_record, opened_elsewhere
from yizkr.pipeline import llmcache
from yizkr.pipeline.llmcache import LOCK_FILE, CacheInUse, EngineCache

HANG_GUARD = 60


def test_aliases_of_one_lock_inode_do_not_refuse_the_same_process(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    EngineCache(first)
    before = lock_record(first)
    second.mkdir(mode=0o700)
    os.link(first / LOCK_FILE, second / LOCK_FILE)
    EngineCache(second)
    assert lock_record(second) == before
    for root in (first, second):
        assert "CacheInUse" in opened_elsewhere(root).stderr


def test_rename_replacement_locks_the_new_file_and_keeps_old_alias(tmp_path):
    root, alias = tmp_path / "cache", tmp_path / "alias"
    EngineCache(root)
    alias.mkdir(mode=0o700)
    os.link(root / LOCK_FILE, alias / LOCK_FILE)
    replacement = tmp_path / "replacement"
    replacement.write_text("")
    replacement.replace(root / LOCK_FILE)
    EngineCache(root)
    assert lock_record(root)["pid"] == os.getpid()
    assert "CacheInUse" in opened_elsewhere(root).stderr
    assert "CacheInUse" in opened_elsewhere(alias).stderr


def test_forked_child_cannot_inherit_ownership_or_a_locked_mutex(tmp_path):
    root = tmp_path / "cache"
    EngineCache(root)
    before = lock_record(root)
    held, release = threading.Event(), threading.Event()

    def holding_mutex():
        with llmcache._held_lock:
            held.set()
            assert release.wait(HANG_GUARD)

    holder = threading.Thread(target=holding_mutex)
    holder.start()
    assert held.wait(HANG_GUARD)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            try:
                EngineCache(root)
            except CacheInUse:
                os.write(write_fd, b"refused")
            else:
                os.write(write_fd, b"incorrectly acquired")
        finally:
            os._exit(0)
    os.close(write_fd)
    reaped = False
    try:
        ready, _, _ = select.select([read_fd], [], [], HANG_GUARD)
        assert ready, "child inherited a mutex held by a vanished thread"
        assert os.read(read_fd, 100) == b"refused"
        assert os.waitpid(child, 0) == (child, 0)
        reaped = True
    finally:
        os.close(read_fd)
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        release.set()
        holder.join(HANG_GUARD)
    assert not holder.is_alive()
    assert lock_record(root) == before
    assert "CacheInUse" in opened_elsewhere(root).stderr
