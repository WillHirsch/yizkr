"""Read-only evidence that an invocation's original process still exists.

Linux process start ticks distinguish reused PIDs; boot and PID-namespace
identity limit the observation to the context in which those ticks mean
anything. Unavailable or foreign context is unknown, never proof of life or
permission to resume. Existing execution locks remain the write authority.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path

ACTIVE = "active"
ABANDONED = "abandoned"
UNKNOWN = "unknown"


def _context() -> dict:
    if int(os.readlink("/proc/self")) != os.getpid():
        raise OSError("procfs exposes a different PID namespace")
    return {
        "host": socket.gethostname(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid_namespace": Path("/proc/self/ns/pid").stat().st_ino,
    }


def _process_identity(pid: int) -> tuple[str, int]:
    # comm can contain parentheses, spaces and non-UTF-8 bytes. Fields after
    # its final ')' begin at field 3 (state); starttime is field 22.
    fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
    state = fields[0].decode("ascii")
    if state not in {
        "R",
        "S",
        "D",
        "T",
        "t",
        "Z",
        "X",
        "x",
        "K",
        "W",
        "P",
        "I",
    }:
        raise ValueError("unrecognized process state")
    return state, int(fields[19])


def _valid(owner: object) -> bool:
    if (
        not isinstance(owner, dict)
        or type(owner.get("format")) is not int
        or owner["format"] != 1
    ):
        return False
    if not isinstance(owner.get("host"), str) or not owner["host"]:
        return False
    for key, minimum in (("pid", 1), ("pid_namespace", 1), ("start_ticks", 0)):
        if type(owner.get(key)) is not int or owner[key] < minimum:
            return False
    try:
        return str(uuid.UUID(owner["boot_id"])) == owner["boot_id"]
    except (KeyError, ValueError, AttributeError, TypeError):
        return False


def capture_owner() -> dict | None:
    """Identity of this process, or None where it cannot be established."""
    try:
        pid = os.getpid()
        _state, ticks = _process_identity(pid)
        owner = {"format": 1, **_context(), "pid": pid, "start_ticks": ticks}
        return owner if _valid(owner) else None
    except (OSError, ValueError, IndexError):
        return None


def owner_liveness(owner: object) -> tuple[str, str | None]:
    """Active, abandoned or unknown, without probing another host or writing.

    Missing identity includes older manifests. A context mismatch or failed
    read is not evidence of termination; a missing local process, changed
    start identity or an exited-but-unreaped process is.
    """
    if not _valid(owner):
        return UNKNOWN, "invocation has no verifiable process identity"
    try:
        context = _context()
    except (OSError, ValueError):
        return UNKNOWN, "local process context is unavailable"
    if any(owner[key] != context[key] for key in context):
        return (
            UNKNOWN,
            "invocation belongs to another host, boot or PID namespace",
        )
    try:
        state, ticks = _process_identity(owner["pid"])
    except (FileNotFoundError, ProcessLookupError):
        # procfs can hide a live PID. Signal zero corroborates absence only;
        # success or denied access cannot establish the original identity.
        try:
            os.kill(owner["pid"], 0)
        except ProcessLookupError:
            return ABANDONED, "invocation process no longer exists"
        except (OSError, OverflowError):
            pass
        return UNKNOWN, "invocation process identity cannot be read"
    except (OSError, ValueError, IndexError):
        return UNKNOWN, "invocation process identity cannot be read"
    if ticks != owner["start_ticks"]:
        return ABANDONED, "invocation PID now belongs to another process"
    if state in "ZXx":
        return ABANDONED, "invocation process has exited"
    return ACTIVE, None
