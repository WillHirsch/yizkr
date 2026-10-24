"""What a scratch run needs to know about the instance, and where it is
kept (ADR-0025 §1, §2 and §6).

**The kept-state marker** is kept twice: as the page KEPT_STATE_PAGE on
the instance itself, and as the file KEPT_FILE in the instance's record
on the host. Keeper mode writes both (mark_kept) once the keeper run's
write completes; from then on `yizkr scratch reset` refuses on the
instance if either says it is kept, and so does a scratch run's stage
that touches the wiki (ADR-0025 §6). The page goes wherever the
instance's database goes, into its backups and through a restore. The
host-side record holds when the instance cannot be read, or when the
Docker engine that answers is not the one that runs it. A reader that
cannot read the page cannot tell whether the instance is kept, and
refuses. Removing either marker is a deliberate owner step.

**The instance-contents record** says what the scratch write stage wrote:
the write stage's key (ADR-0025 §2) and the run that wrote it, with the
instance's newest revision and newest log entry as the write finished. A
later run skips its reset and write only when its key equals the
recorded one and the record still stands, and it stands only while
nothing else has written to the instance: every write adds a revision or
a log entry, so a write the scratch write stage did not make, by any
tool, test or hand, voids the record, and the next scratch run resets
first. The live-test harness, `yizkr bootstrap` and `yizkr scratch
reset` also clear it outright, and the cleared record says who cleared
it, when, and from which process.

**The write-phase state** is what the scratch write stage keeps between
its invocations, in write_state_dir. The reset clears it along with the
record.

The record and the write-phase state are kept per user, apart from any
checkout: every checkout and worktree on the machine uses the one
instance, so a live test run from a worktree must void what a scratch
run in another checkout recorded. They are kept in
$YIZKR_INSTANCE_STATE, by default ~/.local/state/yizkr/instances (under
$XDG_STATE_HOME when that is set), in a directory for each instance,
named after the host of its concept URI and private to the user.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yizkr.core.config import Config
from yizkr.pipeline.stages import atomic_write_text

# the page on the instance whose presence marks the instance kept
KEPT_STATE_PAGE = "Project:Kept state"
# the directory that holds the record of each instance
STATE_ENV = "YIZKR_INSTANCE_STATE"
# in an instance's directory: the instance-contents record, the
# write-phase state, and the host-side kept-state marker
CONTENTS_FILE = "contents.json"
WRITE_DIR = "write"
KEPT_FILE = "kept.json"
# the version of the record's layout
RECORD_FORMAT = 1


def state_root() -> Path:
    """$YIZKR_INSTANCE_STATE, else yizkr/instances in the user's state
    directory ($XDG_STATE_HOME, else ~/.local/state). ValueError for a
    setting that names no usable directory: a relative path, or a home
    folder that cannot be found."""
    configured = os.environ.get(STATE_ENV, "")
    try:
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                raise ValueError(
                    f"{STATE_ENV}={configured!r} is a relative path: "
                    "commands run in other working directories would keep "
                    "other records of the one instance"
                )
            return path
        # a relative XDG_STATE_HOME is invalid, and ignored (XDG base
        # directory specification)
        xdg = os.environ.get("XDG_STATE_HOME", "")
        base = (
            Path(xdg)
            if xdg and Path(xdg).is_absolute()
            else Path.home() / ".local" / "state"
        )
    except RuntimeError:  # pathlib's error for a home it cannot find
        raise ValueError(
            f"the home folder that holds the record of the instance cannot "
            f"be found: set {STATE_ENV} to an absolute path"
        ) from None
    return base / "yizkr" / "instances"


def instance_name(concept_uri: str) -> str:
    """The name of an instance's directory: the host (and port, if any)
    of its concept URI, in lower case, with every character a file name
    should not hold replaced by '_'."""
    host = urlsplit(concept_uri).netloc.lower()
    name = re.sub(r"[^a-z0-9.-]+", "_", host).strip("._-")
    if not name:
        raise ValueError(
            f"the concept URI {concept_uri!r} names no host to keep the "
            "instance's record under"
        )
    return name


def instance_dir(config: Config, root: Path | None = None) -> Path:
    """The directory that holds the record of the instance `config`
    names, in `root` (by default state_root())."""
    return (state_root() if root is None else root) / instance_name(
        config.concept_uri
    )


def contents_path(config: Config, root: Path | None = None) -> Path:
    return instance_dir(config, root) / CONTENTS_FILE


def write_state_dir(config: Config, root: Path | None = None) -> Path:
    """Where the scratch write stage keeps its write-phase state, which
    `yizkr scratch reset` clears."""
    return instance_dir(config, root) / WRITE_DIR


def private_dir(path: Path) -> Path:
    """Make `path` a directory, private to the user when this makes it."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_record(config: Config, record: dict, root: Path | None) -> Path:
    path = contents_path(config, root)
    private_dir(path.parent)
    atomic_write_text(path, json.dumps(record, indent=2) + "\n")
    return path


def clear_contents(
    config: Config, by: str, *, root: Path | None = None
) -> Path:
    """Clear the instance-contents record of the instance `config` names:
    in its place, a record of who cleared it (`by`), when, and from which
    process. Returns the record's path. Clearing a cleared record, or one
    that was never made, is safe."""
    return _write_record(
        config,
        {
            "format": RECORD_FORMAT,
            "instance": config.concept_uri,
            "cleared": {"by": by, "at": _now(), "pid": os.getpid()},
        },
        root,
    )


def read_record(config: Config, root: Path | None = None) -> Any:
    """The instance-contents record as it stands in its file, or None when
    there is none. Raises OSError or ValueError when the file cannot be
    read as JSON, and RecursionError when it is nested too deep to
    decode."""
    try:
        return json.loads(contents_path(config, root).read_text())
    except FileNotFoundError:
        return None


def record_contents(
    client, key: str, run_id: str, *, root: Path | None = None
) -> dict:
    """Record that the instance holds what the scratch write stage wrote
    under `key`, in the run `run_id`, as it stands now: with its newest
    revision and newest log entry, which any later write moves on (the
    module docstring). Returns the record."""
    if not key:
        raise ValueError("a recorded instance-contents key is not empty")
    config = client.config
    record = {
        "format": RECORD_FORMAT,
        "instance": config.concept_uri,
        "key": key,
        "run_id": run_id,
        "watermark": watermark(client),
        "recorded_at": _now(),
    }
    _write_record(config, record, root)
    return record


@dataclass(frozen=True)
class RecordedContents:
    """What the instance-contents record says now: the key and the run
    that wrote it, while the record stands; otherwise no key, and why."""

    key: str | None
    run_id: str | None = None
    why: str = ""


def recorded_contents(client, *, root: Path | None = None) -> RecordedContents:
    """The recorded instance-contents key of the instance `client` reads,
    while the record stands: it was made for this instance, nothing has
    cleared it, and the instance's newest revision and log entry are
    still the ones it recorded. Otherwise no key, and why. Raises when
    the instance cannot be read, since then no one can tell."""
    config = client.config
    path = contents_path(config, root)
    try:
        record = read_record(config, root)
    except (OSError, ValueError, RecursionError) as exc:
        return RecordedContents(None, why=f"{path} cannot be read ({exc})")
    if record is None:
        return RecordedContents(None, why="no key is recorded")
    if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
        return RecordedContents(
            None, why=f"{path} is not a record this version reads"
        )
    if record.get("instance") != config.concept_uri:
        return RecordedContents(
            None,
            why=f"{path} records another instance, {record.get('instance')}",
        )
    cleared = record.get("cleared")
    if cleared is not None:
        cleared = cleared if isinstance(cleared, dict) else {}
        return RecordedContents(
            None,
            why=f"cleared by {cleared.get('by')} at {cleared.get('at')}",
        )
    key, run_id = record.get("key"), record.get("run_id")
    if not isinstance(key, str) or not key:
        return RecordedContents(None, why=f"{path} records no key")
    then, now = record.get("watermark"), watermark(client)
    if then != now:
        return RecordedContents(
            None,
            run_id=run_id,
            why=(
                "the instance has been written to since the key was "
                f"recorded: its newest revision and log entry were {then}, "
                f"and are {now}"
            ),
        )
    return RecordedContents(key, run_id=run_id)


def watermark(client) -> dict[str, int]:
    """The instance's newest revision and newest log entry, by id (0 for
    none): every write to the instance adds one or the other, a deletion
    included. Read through the API, from the database, so a write is seen
    at once."""
    revisions = client.api_get(
        {
            "action": "query",
            "list": "allrevisions",
            "arvlimit": 1,
            "arvprop": "ids",
        }
    )
    logs = client.api_get(
        {"action": "query", "list": "logevents", "lelimit": 1, "leprop": "ids"}
    )
    try:
        pages = revisions["query"]["allrevisions"]
        entries = logs["query"]["logevents"]
        revision = max(
            (
                int(found["revid"])
                for page in pages
                for found in page.get("revisions", [])
            ),
            default=0,
        )
        log = max((int(entry["logid"]) for entry in entries), default=0)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"the wiki's answer names no newest revision or log entry "
            f"({type(exc).__name__}: {exc})"
        ) from None
    return {"revision": revision, "log": log}


def carries_kept_marker(client) -> bool:
    """Whether the instance `client` reads carries the kept-state marker,
    the page KEPT_STATE_PAGE. Raises when the wiki's answer does not say,
    so the caller cannot tell and refuses."""
    data = client.api_get(
        {"action": "query", "titles": KEPT_STATE_PAGE, "prop": "info"}
    )
    pages = (data.get("query") or {}).get("pages")
    if not isinstance(pages, dict) or not pages:
        raise ValueError(
            f"the wiki's answer about {KEPT_STATE_PAGE} names no page: {data}"
        )
    for page in pages.values():
        if not isinstance(page, dict) or "invalid" in page:
            raise ValueError(
                f"the wiki takes {KEPT_STATE_PAGE} for no page: {page}"
            )
        if "missing" not in page:
            return True
    return False


def kept_path(config: Config, root: Path | None = None) -> Path:
    """The host-side kept-state marker of the instance `config` names."""
    return instance_dir(config, root) / KEPT_FILE


def host_kept_marker(config: Config, root: Path | None = None) -> str | None:
    """What the host-side kept-state marker of the instance `config` names
    says, when there is one; None when there is none. Whatever the file
    holds, its being there marks the instance kept, so one that cannot be
    read, nested too deep to decode included, marks it kept too."""
    path = kept_path(config, root)
    if not os.path.lexists(path):
        return None
    try:
        kept = json.loads(path.read_text())["kept"]
        said = f"marked kept by {kept['by']} at {kept['at']}"
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        said = "a marker that cannot be read, which marks it kept all the same"
    return f"{path}: {said}"


def mark_kept(client, by: str, *, root: Path | None = None) -> None:
    """Mark the instance `client` writes to as kept (the module
    docstring), the host-side record first, so that a failed write to the
    instance still leaves it marked. Keeper mode calls this once the
    keeper run's write completes (ADR-0025 §6)."""
    config = client.config
    path = kept_path(config, root)
    private_dir(path.parent)
    at = _now()
    atomic_write_text(
        path,
        json.dumps(
            {
                "format": RECORD_FORMAT,
                "instance": config.concept_uri,
                "kept": {"by": by, "at": at},
            },
            indent=2,
        )
        + "\n",
    )
    client.edit_page(
        KEPT_STATE_PAGE,
        f"This instance is kept: marked by {by} at {at}. `yizkr scratch "
        "reset` refuses on it while this page, or the host-side record, is "
        "there.",
        summary="kept-state marker",
    )


def instance_is_kept(
    env_file: Path | None,
    *,
    client_factory: Callable[[Config], Any] | None = None,
    root: Path | None = None,
) -> bool:
    """Whether the instance the connection settings in `env_file` name is
    kept: its host-side record says so, or it carries the page, read
    through its API. Raises when the settings, or the instance with no
    host-side record, cannot be read, so the caller cannot tell and
    refuses."""
    try:
        config = Config.load(env_file, require_admin=False)
    except SystemExit as exc:  # ConfigError: a setting is missing
        raise ValueError(str(exc)) from None
    if host_kept_marker(config, root) is not None:
        return True
    if client_factory is None:
        from yizkr.core.wiki import WikiClient

        client_factory = WikiClient
    return carries_kept_marker(client_factory(config))
