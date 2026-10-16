"""The write side of a scratch run (ADR-0025 §1 and §2): the corpus
stages after clustering, in order, `reset`, `register`, `write` and
`site`, each a stage of the scratch driver (yizkr.pipeline.scratch).
They make no model call, and each touches the wiki, so the driver runs
them only while the stack answers and the instance carries no kept-state
marker, which it checks before each.

**The write key.** What the write side would put on the instance is named
by one key (write_key): the identity map's hash; each document's SHA-1
with the canonical content hash of its batch, whole, run stamps included,
since the run item's label holds them, and what registration reads of it
(its kind, its title, its file name, which its artifact's description
gives, the record of its derived media and, for an excerpt, the PDFs
that place it); the never-ingest list's hash; the writer's version
(WRITER_VERSION and the package's source, code_digest); and the model
manifest's hash. Never a QID, an mtime or a timestamp. A batch's run
stamps differ from run to run, so a new run resets and writes again,
and only a later invocation of the run that wrote finds the instance
holding what its key makes; writing makes no model call, so this costs
time alone.

**reset.** When the instance-contents record (yizkr.core.instance) still
stands and records this key, the instance holds what the key makes: the
stage resets nothing, and register and write skip, so a rerun on
unchanged batches and code reuses the instance as a view is reused. When
this run reset the instance in an earlier invocation, with this key, and
nothing has written to it since but this run's own stages (the
write-phase state below), the stage resets nothing and the run goes on
where it stopped. Otherwise it resets the instance as `yizkr scratch
reset --yes` does (yizkr.reset), which refuses a kept instance itself,
and records the write-phase state, both under one hold of the live-stack
lock, so no session that takes the lock writes between the reset and the
state, which would take its write for this run's. The records register,
write and site kept of an earlier reset go with it.

**The write-phase state** (STATE_FILE, in the instance's write-phase
directory, which the reset clears) names the run that reset the
instance, the write key, an epoch the reset drew, and the instance's
newest revision and log entry as this run's last write left them.
Register and write each refuse, before anything is written, unless it
names this run and this key, and the instance's newest revision and log
entry are still the ones it records: so they write only to an instance
this run reset, and that has taken no write since but this run's own. A
write cut short in the middle of a document leaves a revision the state
does not record, so the next invocation resets again, and writes every
document afresh.

**register** registers each document whose batch the write will take,
as `ingest run` registers a document at its write (the orchestrator's
_step_register): its artifact item, a deck's slides and the images it
embeds, and the images a PDF places, each by the SHA-1 of its bytes,
with registration's flags raised as alerts of the write. A batch the
writer would refuse whole is refused here first, before anything of its
document is registered (check_batch): a mention the identity map does
not hold, an unsupported date, a run item's term the instance may keep
in another form. Nothing of a refused batch is written; a refused PDF's
artifact may still be registered as the container of an excerpt whose
batch was not refused.

**write** writes every claim of each registered document's batch, the
extraction's batch, which holds only claims the claim schema and the
manifest take, through the run's one identity map, at normal rank, with
both references (stated in the artifact or fragment with its locator,
inferred from the run item), and no approval file: the development stage
has no human gate (ADR-0025 §1). Depicts claims are left out of what the
writer is given, and counted: no scratch run writes a depicts statement
(ADR-0005). Each run item is marked an unratified scratch run's (the
`scratch_run` statement, naming the run) and records the hashes of the
run manifest's settings and of the identity map, each stated in itself.
Once every document is written, the instance-contents record takes the
write key.

**site** builds the LAN site (yizkr.staticsite) from the instance into
the stage's directory of the run, once the query service shows the run
items of the run whose writes the instance holds. Every page's header
says the instance holds a development-stage scratch run, and names it.

Each stage keeps what it did in its directory of the run (RECORD); the
run report reads the write's.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from yizkr.claims import ClaimBatch
from yizkr.core.instance import (
    private_dir,
    record_contents,
    recorded_contents,
    watermark,
    write_state_dir,
)
from yizkr.pipeline.stages import (
    atomic_write_text,
    canonical_json,
    sha256_text,
)

# the version of what the write side writes for the same inputs: a change
# that writes something else for them bumps it, as does a change to the
# package's source, which the key hashes too (code_digest)
WRITER_VERSION = 2
# the package whose source the key hashes, and the modules it leaves out:
# those that only read the run directory or the instance, and decide
# nothing a write puts on it
PACKAGE = Path(__file__).resolve().parents[1]
READ_ONLY = frozenset(
    {
        "cli.py",
        "disagreement.py",
        "staticsite.py",
        "pipeline/estimate.py",
        "pipeline/monitor.py",
        "pipeline/report.py",
        "pipeline/rundiff.py",
    }
)
MANIFEST = files("yizkr.model") / "manifest.yaml"
KEY_FORMAT = 1
# the stages, in the order they run after clustering
RESET, REGISTER, WRITE, SITE = "reset", "register", "write", "site"
# each stage's record of what it did, in its directory of the run
RECORD = "record.json"
# the write-phase state, in the instance's write-phase directory
STATE_FILE = "scratch-run.json"
STATE_FORMAT = 1
# what the reset stage did
SKIPPED, RESUMED, RESET_DONE = "skipped", "resumed", "reset"
# the property a depicts claim has: never written by a scratch run
DEPICTS = "depicts"
# the edit summary of a claim's write
CLAIM_SUMMARY = "scratch run: write unratified claim"
# how long the site stage waits for the query service to show the run
SITE_WAIT = 300.0
SITE_POLL = 5.0
# what the site stage waits with, and the clock its deadline is on
sleep = time.sleep
clock = time.monotonic
# the extraction-run item's marks (manifest properties)
MARK_RUN = "scratch_run"
MARK_MANIFEST = "run_manifest_hash"
MARK_MAP = "identity_map_hash"


class ScratchWriteRefused(RuntimeError):
    """The write side will not write: what the message names has to change
    first. Nothing was written for it."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- what the write side reads of the run --------------------------------


def identity_map(context: Any):
    """The identity map the clustering stage left in its directory of the
    run (yizkr.pipeline.clustering)."""
    from yizkr.pipeline.clustering import IDENTITY_MAP, IdentityMap

    return IdentityMap.read(
        context.directory.parent / "clustering" / IDENTITY_MAP
    )


def extracted_batch(doc: Any) -> ClaimBatch | None:
    """The document's batch, as its extraction or the GEDCOM adapter made
    it, or None when it has none."""
    if not doc.batch.exists():
        return None
    return ClaimBatch.model_validate_json(doc.batch.read_text())


def batch_to_write(extracted: ClaimBatch) -> tuple[ClaimBatch, int]:
    """The batch the writer is given, every local ref as it is
    (yizkr.pipeline.resolve), with its depicts claims left out; and how
    many it left out."""
    from yizkr.pipeline.resolve import resolve_batch

    resolved = resolve_batch(extracted)
    kept = [claim for claim in resolved.claims if claim.property != DEPICTS]
    left_out = len(resolved.claims) - len(kept)
    return resolved.model_copy(update={"claims": kept}), left_out


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def package_digest(package: Path) -> str:
    """The SHA-256 of every file of the package, by its path within it,
    the query pages the reset publishes among them, but the READ_ONLY
    modules and compiled bytecode. What registers and writes reaches far
    into the package (the claim contract's values, dates, the manifest,
    the review's entity records, the identity map's terms), so the whole
    of it is hashed, and a change anywhere in it resets and writes again,
    which costs no model call."""
    found = {}
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package).as_posix()
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or relative in READ_ONLY
        ):
            continue
        found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return sha256_text(canonical_json(found))


@functools.lru_cache(maxsize=1)
def code_digest() -> str:
    """package_digest of the package this module is in, read once a
    process: the code a process runs does not change under it."""
    return package_digest(PACKAGE)


def _registration(doc: Any) -> dict[str, Any]:
    """What registration reads of a document besides its bytes' SHA-1."""
    from yizkr.pipeline.presentation import DERIVED_MEDIA

    containers = getattr(doc, "containers", None)
    source = getattr(doc, "source", None)
    return {
        "kind": doc.kind,
        "title": getattr(doc.state, "title", None),
        "name": source.name if source is not None else None,
        "derived_media": _file_hash(doc.dir / DERIVED_MEDIA),
        "containers": (
            sorted(container.sha1 for container in containers)
            if containers
            else None
        ),
    }


def write_key(
    identity: Any, documents: Sequence[Any], exclusions: Any = None
) -> str:
    """The write key (the module docstring)."""
    from yizkr.pipeline.rundir import exclusions_record

    batches = {}
    for doc in documents:
        extracted = extracted_batch(doc)
        batches[doc.sha1] = {
            "batch": (
                None
                if extracted is None
                else sha256_text(
                    canonical_json(extracted.model_dump(mode="json"))
                )
            ),
            "registration": _registration(doc),
        }
    record = exclusions_record(exclusions) if exclusions is not None else None
    return sha256_text(
        canonical_json(
            {
                "format": KEY_FORMAT,
                "identity_map": identity.hash,
                "documents": batches,
                "exclusions": (record or {}).get("sha256"),
                "writer": {"version": WRITER_VERSION, "code": code_digest()},
                "manifest": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            }
        )
    )


def manifest_hash(run: Any) -> str:
    """The SHA-256 of the settings the run manifest records, those a run
    that keeps its settings keeps (yizkr.pipeline.rundir)."""
    from yizkr.pipeline.rundir import _fixed_settings

    return sha256_text(canonical_json(_fixed_settings(run.manifest())))


# -- the stage's own record, in its directory of the run ----------------------


def read_record(directory: Path) -> dict:
    try:
        record = json.loads((directory / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def save_record(directory: Path, record: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / RECORD, json.dumps(record, indent=2) + "\n")


def _reset_record(context: Any) -> dict:
    return read_record(context.directory.parent / RESET)


# -- the write-phase state, on the host ---------------------------------------


def state_path(config: Any) -> Path:
    return write_state_dir(config) / STATE_FILE


def read_state(config: Any) -> dict | None:
    try:
        state = json.loads(state_path(config).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
        return None
    if state.get("instance") != config.concept_uri:
        return None
    return state


def save_state(client: Any, run_id: str, key: str, epoch: str) -> dict:
    """Record that the instance holds this run's writes alone, as they
    stand now."""
    config = client.config
    state = {
        "format": STATE_FORMAT,
        "instance": config.concept_uri,
        "run_id": run_id,
        "key": key,
        "epoch": epoch,
        "watermark": watermark(client),
        "at": _now(),
    }
    path = state_path(config)
    private_dir(path.parent)
    atomic_write_text(path, json.dumps(state, indent=2) + "\n")
    return state


def _state_problem(client: Any, run_id: str, key: str) -> str | None:
    """Why the instance does not hold this run's writes alone, under this
    key, or None when it does."""
    state = read_state(client.config)
    if state is None:
        return "no scratch run has reset the instance since it was last reset"
    if state.get("run_id") != run_id:
        return f"the scratch run {state.get('run_id')} reset it, not this one"
    if state.get("key") != key:
        return "this run reset it for another write key"
    now = watermark(client)
    if state.get("watermark") != now:
        return (
            "it has been written to since this run's last write: its newest "
            f"revision and log entry were {state.get('watermark')}, and are "
            f"{now}"
        )
    return None


def check_state(client: Any, run_id: str, key: str) -> dict:
    """The write-phase state, while it says the instance holds this run's
    writes alone, under this key; ScratchWriteRefused otherwise."""
    problem = _state_problem(client, run_id, key)
    if problem is not None:
        raise ScratchWriteRefused(
            f"this run did not reset the instance, or it has taken other "
            f"writes since ({problem}): nothing was written. Invoke the run "
            "again: its reset stage resets the instance first"
        )
    return read_state(client.config)


# -- what the tests replace ----------------------------------------------


def reset_instance(env_file: Path | None, log: Callable[..., None]) -> Any:
    """`yizkr scratch reset --yes` on the stack beside the env file
    (yizkr.reset). Its caller holds the live-stack lock (run_reset), so
    the reset takes no hold of its own."""
    from yizkr.reset import reset_instance as reset

    return reset(
        env_file=env_file, yes=True, log=log, lock=contextlib.nullcontext
    )


@contextlib.contextmanager
def live_stack() -> Iterator[None]:
    """The live-stack lock, held while a stage writes to the instance, so
    no live test session writes to it meanwhile (yizkr.reset)."""
    from yizkr.reset import live_stack_lock

    with live_stack_lock():
        yield


def client_of(context: Any) -> Any:
    """The wiki client the stages write with: the driver's."""
    return context.driver.client


def _forget_client(context: Any) -> None:
    """Drop the driver's client, whose login a reset has voided."""
    context.driver._client = None


def build_site(config: Any, out: Path) -> dict:
    from yizkr.staticsite import publish_site

    return publish_site(config, out, clean=True)


def query_service_probe(config: Any) -> Callable[[], tuple[list[str], int]]:
    """What the query service shows: (scratch runs, newest revision)
    (yizkr.staticsite.query_service_probe)."""
    from yizkr.staticsite import query_service_probe as probe

    return probe(config)


# -- reset ----------------------------------------------------------------


def _key_of(context: Any) -> tuple[Any, str]:
    identity = identity_map(context)
    key = write_key(
        identity,
        context.documents,
        getattr(context.driver, "exclusions", None),
    )
    return identity, key


def run_reset(context: Any) -> list[str]:
    """The reset stage (the module docstring)."""
    _, key = _key_of(context)
    run_id = context.driver.run_id
    client = client_of(context)
    recorded = recorded_contents(client)
    if recorded.key == key:
        context.log(
            f"{RESET}: the instance holds what this write key makes, written "
            f"by scratch run {recorded.run_id}: no reset, and no write"
        )
        save_record(
            context.directory,
            {"key": key, "action": SKIPPED, "run_id": recorded.run_id},
        )
        return []
    held = read_state(client.config)
    if (
        held is not None
        and _state_problem(client, run_id, key) is None
        and _reset_record(context).get("epoch") == held.get("epoch")
    ):
        context.log(
            f"{RESET}: this run reset the instance, which holds its writes "
            "alone: the run goes on where it stopped"
        )
        save_record(
            context.directory,
            {
                "key": key,
                "action": RESUMED,
                "run_id": run_id,
                "epoch": held["epoch"],
            },
        )
        return []
    context.log(f"{RESET}: resetting the instance ({recorded.why})")
    # what the stages after it recorded of an earlier reset no longer
    # holds, and is not shown as this one's
    for stage in (REGISTER, WRITE, SITE):
        with contextlib.suppress(FileNotFoundError):
            (context.directory.parent / stage / RECORD).unlink()
    # the reset and the state it records under one hold of the lock, so
    # no session that takes the lock writes between them, where the state
    # would take its write for this run's
    with live_stack():
        reset_instance(context.env_file, context.log)
        _forget_client(context)
        epoch = uuid.uuid4().hex
        save_state(client_of(context), run_id, key, epoch)
    save_record(
        context.directory,
        {
            "key": key,
            "action": RESET_DONE,
            "run_id": run_id,
            "epoch": epoch,
            "at": _now(),
        },
    )
    return []


# -- register ---------------------------------------------------------------


def refusal(doc: Any, identity: Any) -> str | None:
    """Why the writer would refuse the document's batch whole, before it
    reads or writes anything (yizkr.pipeline.writer.check_batch): the
    exception's type, which never quotes the document; None when it would
    not."""
    from yizkr.pipeline.writer import check_batch

    extracted = extracted_batch(doc)
    if extracted is None:
        return "no batch"
    batch, _ = batch_to_write(extracted)
    try:
        check_batch(batch, identity, mapped_from=extracted)
    except (LookupError, ValueError) as exc:
        return type(exc).__name__
    return None


def _skipped(context: Any, stage: str) -> dict | None:
    """The reset stage's record, when it found the instance holding what
    the key makes, so that this stage does nothing."""
    record = _reset_record(context)
    if record.get("action") == SKIPPED:
        context.log(
            f"{stage}: skipped, the instance holds what this write key makes"
        )
        return record
    return None


def _ongoing(context: Any, key: str, epoch: str) -> dict:
    """The stage's record of this reset of the instance: the one in its
    directory, when it was made for this key and epoch, else a new one."""
    record = read_record(context.directory)
    if record.get("key") == key and record.get("epoch") == epoch:
        return record
    return {
        "key": key,
        "epoch": epoch,
        "documents": {},
        "refused": {},
        "failed": {},
    }


def _held(context: Any, key: str) -> tuple[str, str, dict]:
    """(run id, epoch, the write-phase state), refusing unless the reset
    stage ran in this invocation's order for this key and the instance
    holds this run's writes alone."""
    record = _reset_record(context)
    if record.get("key") != key or record.get("action") not in (
        RESUMED,
        RESET_DONE,
    ):
        raise ScratchWriteRefused(
            "the reset stage has not readied the instance for this write "
            "key: nothing was written"
        )
    run_id = record["run_id"]
    state = check_state(client_of(context), run_id, key)
    if state.get("epoch") != record.get("epoch"):
        raise ScratchWriteRefused(
            "the instance was reset since this run's reset stage ran: "
            "nothing was written"
        )
    return run_id, record["epoch"], state


def run_register(context: Any) -> list[str]:
    """The register stage (the module docstring)."""
    identity, key = _key_of(context)
    if _skipped(context, REGISTER) is not None:
        return []
    with live_stack():
        run_id, epoch, _ = _held(context, key)
        record = _ongoing(context, key, epoch)
        # every batch the writer would refuse is refused before anything
        # of any document is registered
        refused = {}
        for doc in context.documents:
            why = refusal(doc, identity)
            if why is not None:
                refused[doc.sha1] = why
        record["refused"] = refused
        save_record(context.directory, record)
        driver = context.driver
        for doc in context.documents:
            if doc.sha1 in refused or doc.sha1 in record["documents"]:
                continue
            try:
                qid = driver._step_register(doc)
            except Exception as exc:
                record["failed"][doc.sha1] = type(exc).__name__
                context.log(
                    f"{REGISTER}: {doc.sha1[:12]} not registered "
                    f"({type(exc).__name__})"
                )
            else:
                record["documents"][doc.sha1] = qid
                record["failed"].pop(doc.sha1, None)
            save_state(client_of(context), run_id, key, epoch)
            save_record(context.directory, record)
    context.log(
        f"{REGISTER}: {len(record['documents'])} documents registered, "
        f"{len(refused)} refused, {len(record['failed'])} failed"
    )
    return _attention(refused, record["failed"])


def _attention(refused: dict, failed: dict) -> list[str]:
    attention = []
    if refused:
        attention.append(f"{len(refused)} batches refused whole")
    if failed:
        attention.append(f"{len(failed)} documents failed")
    return attention


# -- write ------------------------------------------------------------------


def _depicts(documents: Sequence[Any]) -> int:
    count = 0
    for doc in documents:
        extracted = extracted_batch(doc)
        if extracted is not None:
            count += batch_to_write(extracted)[1]
    return count


def run_write(context: Any) -> list[str]:
    """The write stage (the module docstring)."""
    from yizkr.pipeline.writer import BatchWriter

    identity, key = _key_of(context)
    depicts = _depicts(context.documents)
    skipped = _skipped(context, WRITE)
    if skipped is not None:
        if read_record(context.directory).get("key") == key and (
            read_record(context.directory).get("recorded")
        ):
            # this run made what the instance holds: its counts stand
            return []
        save_record(
            context.directory,
            {
                "key": key,
                "skipped": True,
                "run_id": skipped.get("run_id"),
                "depicts_left_out": depicts,
            },
        )
        return []
    registered = read_record(context.directory.parent / REGISTER)
    with live_stack():
        run_id, epoch, _ = _held(context, key)
        if (registered.get("key"), registered.get("epoch")) != (key, epoch):
            raise ScratchWriteRefused(
                "the register stage has not registered the documents for "
                "this reset of the instance: nothing was written"
            )
        record = _ongoing(context, key, epoch)
        record.update(
            run_id=run_id,
            refused=dict(registered.get("refused") or {}),
            depicts_left_out=depicts,
        )
        marks = {
            MARK_RUN: run_id,
            MARK_MANIFEST: manifest_hash(context.run),
            MARK_MAP: identity.hash,
        }
        qids = registered.get("documents") or {}
        for doc in context.documents:
            if doc.sha1 not in qids or doc.sha1 in record["documents"]:
                continue
            extracted = extracted_batch(doc)
            batch, left_out = batch_to_write(extracted)
            try:
                result = BatchWriter(
                    client_of(context),
                    artifact_qids={doc.sha1: qids[doc.sha1]},
                    run_marks=marks,
                ).write_batch(
                    batch,
                    identity=identity,
                    mapped_from=extracted,
                    claim_summary=CLAIM_SUMMARY,
                )
            except Exception as exc:
                record["failed"][doc.sha1] = type(exc).__name__
                context.log(
                    f"{WRITE}: {doc.sha1[:12]} not written "
                    f"({type(exc).__name__})"
                )
            else:
                record["documents"][doc.sha1] = {
                    "run_qid": result.run_qid,
                    "claims": len(extracted.claims),
                    "written": result.written,
                    "referenced": result.referenced,
                    "skipped": result.skipped,
                    "refused": len(result.refused),
                    "depicts_left_out": left_out,
                    "items_minted": len(result.minted),
                    "notes_written": result.notes_written,
                }
                record["failed"].pop(doc.sha1, None)
                context.driver._raise_flags(doc, result.flags)
            state = save_state(client_of(context), run_id, key, epoch)
            record["revision"] = state["watermark"]["revision"]
            save_record(context.directory, record)
        complete = (
            not record["failed"]
            and all(
                doc.sha1 in record["documents"]
                for doc in context.documents
                if doc.sha1 in qids
            )
            and not (registered.get("failed") or {})
        )
        if complete:
            record_contents(client_of(context), key, run_id)
            record["recorded"] = True
        save_record(context.directory, record)
    written = record["documents"].values()
    context.log(
        f"{WRITE}: {len(record['documents'])} documents written: "
        f"{sum(d['written'] for d in written)} statements new, "
        f"{sum(d['referenced'] for d in written)} referenced, "
        f"{sum(d['refused'] for d in written)} claims refused, "
        f"{sum(d.get('notes_written', 0) for d in written)} judgement notes "
        f"written; "
        f"{depicts} depicts claims left out"
        + ("" if complete else "; the instance-contents key is not recorded")
    )
    return _attention(record["refused"], record["failed"])


# -- site -------------------------------------------------------------------


def run_site(context: Any) -> list[str]:
    """The site stage (the module docstring). It waits for the query
    service only when the instance holds a run item of the run, one this
    run wrote or the one whose writes it kept: until it shows that run,
    and, after this run's write, an entity of the newest revision the
    write left or a later one, for at most SITE_WAIT seconds."""
    expected = _reset_record(context).get("run_id")
    written = read_record(context.directory.parent / WRITE)
    revision = written.get("revision") or 0
    config = client_of(context).config
    if written.get("skipped") or written.get("documents"):
        probe = query_service_probe(config)
        deadline = clock() + SITE_WAIT
        while True:
            shown, newest = probe()
            if expected in shown and newest >= revision:
                break
            if clock() >= deadline:
                raise ScratchWriteRefused(
                    f"the query service has not caught up with the write "
                    f"after {SITE_WAIT:.0f} seconds: it shows the run items "
                    f"of {', '.join(shown) or 'no scratch run'} and "
                    f"revision {newest}, waiting for {expected}'s and "
                    f"revision {revision}: the site was not built"
                )
            sleep(SITE_POLL)
    out = context.directory / "site"
    summary = build_site(config, out)
    save_record(
        context.directory,
        {"run_id": expected, "out": str(out), "built_at": _now()},
    )
    context.log(
        f"{SITE}: built in {out} for scratch run {expected} "
        f"({summary.get('people', 0)} people, "
        f"{summary.get('artifacts', 0)} artifacts); serve it on the LAN "
        f"only: python -m http.server -d {out}"
    )
    return []
