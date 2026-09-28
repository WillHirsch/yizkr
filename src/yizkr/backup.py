"""Backup and export.

One `yizkr backup` run produces a timestamped directory containing
everything needed to rebuild the instance, plus a public export:

* ``database.sql.gz`` — mariadb dump of the MediaWiki/Wikibase database
  (THE authoritative backup: items, statements, history, users).
* ``media.tar.gz`` — the wiki's uploaded files (scanned artifacts).
* ``full.nt.gz`` — complete RDF dump (portable export; also the input
  for the public variant). WDQS state is derived and never backed up:
  it reloads from this dump.
* ``public.nt.gz`` — the possibly-living-excluded export (R7,
  docs/MODELING.md "Living persons and privacy"). Nothing about a
  possibly-living person leaves the instance: every triple that
  mentions an excluded person — as subject, statement node, or object
  of someone else's claim — is dropped.

Exclusion is CONSERVATIVE by design and computed at RUNTIME — there is
no stored "living" flag (ADR-0020). A person is excluded unless the graph
gives positive evidence they are not living: any death evidence (a date,
place, or cause of death, a place of burial, or a death event), or births
that all lie more than ``cutoff_years`` ago (``DEFAULT_CUTOFF_YEARS`` by
default), every birth statement a curator has not deprecated read at the
latest year it allows. Unknown means excluded. The determination, and the
queries that select people and read their births for it, live in
``yizkr.core.privacy``.

The export keeps only the people that check releases, and so fails
closed:

* The check assesses every item with person evidence, read through
  statement nodes at every rank (``privacy.PEOPLE_SELECTION``), so a
  deprecated, outranked or missing `human` type hides no one.
* The export reads the same evidence from its own dump (``read_dump``)
  and drops every item that has any of it and was not released: an item
  the check never assessed is dropped, not kept. A redirect goes with the
  item it redirects to.
* The check runs on the query service, the dump comes from the database:
  the export waits for the query service to hold exactly the dump's
  revision of every item, and refuses to run when it does not, or when
  the graph changes while the check runs (``QueryServiceLag``).

Scheduling is the host's job (this is a home-server deployment): see
deploy/README.md for the cron line. Retention: ``--keep N`` prunes the
oldest runs.
"""

from __future__ import annotations

import datetime
import gzip
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.core import sparql
from yizkr.core.config import Config, read_env_file
from yizkr.core.privacy import (
    BIRTHS_QUERY,
    DEFAULT_CUTOFF_YEARS,
    PERSON_CLASS,
    PERSON_EVENTS,
    PERSON_SUBJECT_PROPERTIES,
    PERSON_VALUE_PROPERTIES,
    is_possibly_living,
    latest_births,
    people_selection,
)
from yizkr.queries.publish import prefix_block

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_QID_TOKEN = re.compile(r"Q[1-9]\d*\b")


# ---------------------------------------------------------------------------
# container plumbing
# ---------------------------------------------------------------------------


def compose_base(
    deploy_dir: Path, compose_files: list[str] | None = None
) -> list[str]:
    files = compose_files or ["docker-compose.yml"]
    command = ["docker", "compose", "--env-file", str(deploy_dir / ".env")]
    for name in files:
        path = Path(name)
        command += [
            "-f",
            str(path if path.is_absolute() else deploy_dir / path),
        ]
    return command


def _stream_to_gzip(
    command: list[str], out_path: Path, already_compressed=False
):
    """Run a container command, streaming stdout to (gzipped) out_path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        if already_compressed:
            with out_path.open("wb") as sink:
                shutil.copyfileobj(process.stdout, sink, length=1024 * 1024)
        else:
            with gzip.open(out_path, "wb", compresslevel=6) as sink:
                shutil.copyfileobj(process.stdout, sink, length=1024 * 1024)
    if process.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"backup command failed with exit {process.returncode}: "
            f"{' '.join(command)}"
        )
    if out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"backup command produced no output: {' '.join(command)}"
        )


def dump_database(
    deploy_dir: Path, out_path: Path, compose_files=None
) -> None:
    env = read_env_file(deploy_dir / ".env")
    missing = [
        key for key in ("DB_USER", "DB_PASS", "DB_NAME") if not env.get(key)
    ]
    if missing:
        raise RuntimeError(f"{deploy_dir / '.env'} is missing {missing}")
    _stream_to_gzip(
        compose_base(deploy_dir, compose_files)
        + [
            "exec",
            "-T",
            "mysql",
            "mariadb-dump",
            "--single-transaction",
            "--quick",
            f"--user={env['DB_USER']}",
            f"--password={env['DB_PASS']}",
            env["DB_NAME"],
        ],
        out_path,
    )


def dump_media(deploy_dir: Path, out_path: Path, compose_files=None) -> None:
    _stream_to_gzip(
        compose_base(deploy_dir, compose_files)
        + [
            "exec",
            "-T",
            "wikibase",
            "tar",
            "-C",
            "/var/www/html",
            "-czf",
            "-",
            "images",
        ],
        out_path,
        already_compressed=True,
    )


def dump_rdf(deploy_dir: Path, out_path: Path, compose_files=None) -> None:
    _stream_to_gzip(
        compose_base(deploy_dir, compose_files)
        + [
            "exec",
            "-T",
            "wikibase",
            "php",
            "maintenance/run.php",
            "./extensions/Wikibase/repo/maintenance/dumpRdf.php",
            "--format",
            "nt",
        ],
        out_path,
    )


# ---------------------------------------------------------------------------
# possibly-living exclusion (public export) — runtime, no stored flag
# ---------------------------------------------------------------------------


# One row per item with person evidence (``{people}``: the privacy
# module's selection, at every rank), with a value per death signal,
# bound when the person has that signal: only whether it is bound counts,
# so any value sampled does. A death event needs only the person as
# participant — no date — so an undated death marks a person deceased
# just like a burial place or a cause of death does. Births are read
# apart, statement by statement (``yizkr.core.privacy.BIRTHS_QUERY``).
PERSON_FACTS_QUERY = """\
SELECT ?person
       (SAMPLE(?deathDate) AS ?death) (SAMPLE(?deathPlace) AS ?deathPlace)
       (SAMPLE(?deathCause) AS ?deathCause) (SAMPLE(?burial) AS ?burial)
       (SAMPLE(?deathEventItem) AS ?deathEvent)
WHERE {{
{people}
  OPTIONAL {{ ?person wdt:{date_of_death} ?deathDate . }}
  OPTIONAL {{ ?person wdt:{place_of_death} ?deathPlace . }}
  OPTIONAL {{ ?person wdt:{cause_of_death} ?deathCause . }}
  OPTIONAL {{ ?person wdt:{burial_place} ?burial . }}
  OPTIONAL {{ ?deathEventItem wdt:{participant} ?person ;
                              wdt:{instance_of} wd:{death} . }}
}}
GROUP BY ?person
"""

_DEATH_BINDINGS = ("death", "deathPlace", "deathCause", "burial", "deathEvent")


@dataclass
class ExclusionReport:
    """The check's call on every item with person evidence: `excluded`
    holds those it takes for possibly living, `included` those it
    releases, and `reasons` says why, for each of them."""

    excluded: set[str] = field(default_factory=set)
    included: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)


def exclusion_report(
    config: Config,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    run: sparql.Runner | None = None,
    vocabulary: tuple[dict[str, str], dict[str, str]] | None = None,
) -> ExclusionReport:
    """Which people the public export must not mention, and why: every
    item the privacy module's selection finds. The queries go to the
    instance's query service, with its vocabulary, unless `run` and
    `vocabulary` are given."""
    if vocabulary is None:
        from yizkr.core.wiki import WikiClient

        vocabulary = WikiClient(config).vocabulary()
    properties, items = vocabulary
    run = run or sparql.runner(config)
    prefixes = prefix_block(config.concept_uri)
    bindings = run(
        prefixes
        + PERSON_FACTS_QUERY.format(
            people=people_selection(properties, items),
            instance_of=properties["instance_of"],
            date_of_death=properties["date_of_death"],
            place_of_death=properties["place_of_death"],
            cause_of_death=properties["cause_of_death"],
            burial_place=properties["burial_place"],
            participant=properties["participant"],
            death=items["death"],
        )
    )
    births = latest_births(run(prefixes + BIRTHS_QUERY.format(**properties)))

    cutoff_year = datetime.datetime.now(datetime.UTC).year - cutoff_years
    report = ExclusionReport()
    for binding in bindings:
        qid = binding["person"]["value"].rsplit("/", 1)[-1]
        has_death = any(
            binding.get(name, {}).get("value") for name in _DEATH_BINDINGS
        )
        possibly_living, reason = is_possibly_living(
            births.get(qid, []), has_death, cutoff_year
        )
        report.reasons[qid] = reason
        (report.excluded if possibly_living else report.included).add(qid)
    return report


# The dump in N-Triples, as dumpRdf.php writes it: an item's statement
# links it to a statement node (``<item> p:P <statement>``), which holds
# the value (``<statement> ps:P <value>``) whatever its rank; a data node
# (``Special:EntityData/Q1``) gives each entity's revision
# (``schema:version``), a redirect's included; and a redirect is
# ``owl:sameAs`` the item it redirects to.
_ENTITY = re.compile(r"<[^<>]*/entity/(Q[1-9]\d*)>")
_CLAIM = re.compile(r"<[^<>]*/prop/(P[1-9]\d*)>")
_CLAIM_VALUE = re.compile(r"<[^<>]*/prop/statement/(P[1-9]\d*)>")
_DATA_NODE = re.compile(r"<[^<>]*[/:](Q[1-9]\d*)>")
_VERSION = "<http://schema.org/version>"
_SAME_AS = "<http://www.w3.org/2002/07/owl#sameAs>"


@dataclass
class DumpReading:
    """What the public export reads from its own dump: `people`, every
    item with person evidence (the privacy module's, at every rank);
    `redirects`, each redirect's target; and `revisions`, the revision of
    every item that is not a redirect."""

    people: set[str] = field(default_factory=set)
    redirects: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)


def _item(term: str) -> str | None:
    match = _ENTITY.fullmatch(term)
    return match.group(1) if match else None


def read_dump(
    full_dump: Path, properties: Mapping[str, str], items: Mapping[str, str]
) -> DumpReading:
    """Read person evidence, redirects and revisions from a dump, by the
    instance's ids. Person evidence is read as ``PEOPLE_SELECTION`` reads
    it from the query service: an item typed `human`, the subject of a
    person-only property, the value of a person-valued one, or a
    participant in a person event, each through its statement node at
    every rank. A value that is not an item (an unknown value is a blank
    node) is no evidence."""
    instance_of = properties["instance_of"]
    participant = properties["participant"]
    subject_links = {properties[key] for key in PERSON_SUBJECT_PROPERTIES}
    value_links = {properties[key] for key in PERSON_VALUE_PROPERTIES}
    human = items[PERSON_CLASS]
    event_kinds = {items[key] for key in PERSON_EVENTS}

    reading = DumpReading()
    # typing and participation statements, joined once the whole dump is
    # read, whatever order its lines come in
    typed: dict[str, str] = {}  # statement -> the item it types
    kinds: dict[str, str] = {}  # statement -> the type it gives
    events: dict[str, str] = {}  # statement -> the event it is part of
    participants: dict[str, str] = {}  # statement -> the participant
    links: dict[str, tuple[str, str] | None] = {}
    with gzip.open(full_dump, "rt", encoding="utf-8") as source:
        for line in source:
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            subject, predicate, rest = parts
            term = rest[: rest.find(">") + 1] if rest.startswith("<") else ""
            if predicate == _VERSION:
                node = _DATA_NODE.fullmatch(subject)
                version = re.match(r'"(\d+)"', rest)
                if node and version:
                    reading.revisions[node.group(1)] = int(version.group(1))
                continue
            if predicate == _SAME_AS:
                redirect, target = _item(subject), _item(term)
                if redirect and target:
                    reading.redirects[redirect] = target
                continue
            if predicate not in links:
                claim = _CLAIM.fullmatch(predicate)
                value = _CLAIM_VALUE.fullmatch(predicate)
                links[predicate] = (
                    ("claim", claim.group(1))
                    if claim
                    else ("value", value.group(1))
                    if value
                    else None
                )
            link = links[predicate]
            if link is None:
                continue
            role, pid = link
            if role == "claim":
                item = _item(subject)
                if item is None:
                    continue
                if pid == instance_of:
                    typed[term] = item
                if pid == participant:
                    events[term] = item
                if pid in subject_links:
                    reading.people.add(item)
            else:
                item = _item(term)
                if item is None:
                    continue
                if pid == instance_of:
                    kinds[subject] = item
                if pid == participant:
                    participants[subject] = item
                if pid in value_links:
                    reading.people.add(item)
    reading.people.update(
        typed[statement]
        for statement, kind in kinds.items()
        if kind == human and statement in typed
    )
    person_events = {
        typed[statement]
        for statement, kind in kinds.items()
        if kind in event_kinds and statement in typed
    }
    reading.people.update(
        person
        for statement, person in participants.items()
        if events.get(statement) in person_events
    )
    for redirect in reading.redirects:
        reading.revisions.pop(redirect, None)
    return reading


# Why the export drops an item with person evidence that the check did
# not assess.
NOT_ASSESSED = (
    "person evidence in the dump, but the check did not assess this item: "
    "unknown means excluded"
)


def public_exclusions(
    reading: DumpReading, report: ExclusionReport
) -> dict[str, str]:
    """Every item the public export drops, with why: each item with person
    evidence, in the dump or at the query service, that the check did not
    release. An item it did not assess is dropped, not kept. A redirect
    stands for the item it redirects to: it is dropped when that item has
    person evidence and was not released, even with none of its own."""

    def target(qid: str) -> str:
        seen = {qid}
        while qid in reading.redirects:
            qid = reading.redirects[qid]
            if qid in seen:
                break
            seen.add(qid)
        return qid

    people = reading.people | report.excluded | report.included
    people |= {
        redirect
        for redirect in reading.redirects
        if target(redirect) in people
    }
    excluded = {}
    for qid in people:
        found = target(qid)
        if found in report.included:
            continue
        reason = report.reasons.get(found, NOT_ASSESSED)
        excluded[qid] = (
            reason if found == qid else f"a redirect to {found}: {reason}"
        )
    return excluded


# The revision of every entity the query service holds, redirects left
# out: the dump gives a redirect a revision, which the query service need
# not keep beside its owl:sameAs link, so the two are compared on the
# items that are not redirects. Items alone are compared: the check reads
# people's statements from items, and a property the query service lags
# on can only leave a birth's value unread, which keeps its person
# possibly living.
REVISIONS_QUERY = """\
SELECT ?entity ?version WHERE {
  ?entity <http://schema.org/version> ?version .
  FILTER NOT EXISTS {
    ?entity <http://www.w3.org/2002/07/owl#sameAs> ?target .
  }
}
"""

_HELD_ITEM = re.compile(r".*/entity/(Q[1-9]\d*)")

# How long the export waits for the query service to hold the dump's
# revisions, and how often it asks.
LAG_TIMEOUT = 180.0
LAG_POLL = 5.0


class QueryServiceLag(RuntimeError):
    """The query service does not hold the dump's revisions. The check
    assesses people there, so its release of anyone would not be about
    what the dump holds: the public export refuses to run."""


def stale_items(run: sparql.Runner, revisions: Mapping[str, int]) -> list[str]:
    """The items whose revision at the query service differs from the
    dump's `revisions`: one it holds at an older or a newer revision, or
    not at all, and one it holds that the dump lacks (one deleted or
    created since). Empty when the two hold the same items."""
    held: dict[str, set[int]] = {}
    for row in run(REVISIONS_QUERY):
        item = _HELD_ITEM.fullmatch(row.get("entity", {}).get("value", ""))
        if item is None:
            continue
        version = row.get("version", {}).get("value", "")
        held.setdefault(item.group(1), set()).add(
            int(version) if version.isdigit() else -1
        )
    stale = {
        item
        for item, revision in revisions.items()
        if held.get(item) != {revision}
    }
    stale.update(set(held) - set(revisions))
    return sorted(stale, key=lambda qid: int(qid[1:]))


def _named(stale: list[str]) -> str:
    """'3 items (Q1, Q2, Q7)', naming ten at most."""
    shown = ", ".join(stale[:10])
    if len(stale) > 10:
        shown += f" and {len(stale) - 10} more"
    return f"{len(stale)} item{'' if len(stale) == 1 else 's'} ({shown})"


def await_query_service(
    run: sparql.Runner,
    revisions: Mapping[str, int],
    timeout: float = LAG_TIMEOUT,
    poll: float = LAG_POLL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until the query service holds exactly the dump's `revisions`
    (``stale_items``), asking every `poll` seconds; raise
    QueryServiceLag when it still does not after `timeout` seconds."""
    deadline = clock() + timeout
    while True:
        stale = stale_items(run, revisions)
        if not stale:
            return
        if clock() >= deadline:
            raise QueryServiceLag(
                f"after {timeout:.0f} s the query service still does not "
                f"hold the dump's revision of {_named(stale)}. The public "
                "export assesses people there, so it refuses to run until "
                "the two agree. Run the backup again once the graph is at "
                "rest and the updater has caught up (docker compose logs "
                "wdqs-updater); a query service that lacks items for good "
                "needs reloading from a full dump (deploy/README.md)."
            )
        sleep(poll)


_PROVENANCE_NODE = re.compile(r"<[^>]*/(?:reference|value)/([0-9a-f]{16,})>")


def filter_public_export(
    full_dump: Path,
    public_dump: Path,
    excluded: set[str],
    excerpt_pids: frozenset[str] | set[str] = frozenset(),
) -> tuple[int, int]:
    """Produce the public dump: three privacy filters over N-triples.
    `excluded` holds every item to drop: for the public export, every
    item with person evidence that the check did not release
    (``public_exclusions``).

    1. Drop every triple that mentions an excluded entity: entity URIs,
       statement node URIs (entity/statement/Q76-...), and object
       references all carry the QID as a delimited token, so token
       matching catches a person wherever they appear — including in
       other people's claims.
    2. Drop every transcription-excerpt triple. Excerpts quote raw
       source text for curator verification and can name ANYONE the
       source mentions, living people included; they are not public
       data.
    3. Sweep orphaned provenance nodes. Reference/value nodes are
       shared, content-addressed resources; once the statements using
       them are dropped, their triples would still leak what those
       statements recorded about an excluded person. Only nodes still
       reachable from a kept statement survive.

    Returns (kept, dropped) line counts. Streams the gzipped dump three
    times, holding only node-hash sets in memory.
    """
    excerpt_predicates = tuple(
        f"/prop/reference/{pid}>" for pid in excerpt_pids
    )

    def suppressed(line: str) -> bool:
        if excluded and any(
            token in excluded for token in _QID_TOKEN.findall(line)
        ):
            return True
        parts = line.split(" ", 2)
        predicate = parts[1] if len(parts) >= 3 else ""
        return any(predicate.endswith(marker) for marker in excerpt_predicates)

    def subject_hash(line: str) -> str | None:
        match = _PROVENANCE_NODE.fullmatch(line.split(" ", 1)[0])
        return match.group(1) if match else None

    # pass 1: which provenance nodes do the surviving statements need?
    needed: set[str] = set()
    with gzip.open(full_dump, "rt", encoding="utf-8") as source:
        for line in source:
            if suppressed(line) or subject_hash(line) is not None:
                continue
            needed.update(_PROVENANCE_NODE.findall(line))
    # ...and which value nodes do the surviving reference nodes need?
    with gzip.open(full_dump, "rt", encoding="utf-8") as source:
        for line in source:
            if suppressed(line):
                continue
            subject = subject_hash(line)
            if subject is not None and subject in needed:
                needed.update(_PROVENANCE_NODE.findall(line.split(" ", 1)[1]))

    # pass 2: write everything that survives
    kept = dropped = 0
    with (
        gzip.open(full_dump, "rt", encoding="utf-8") as source,
        gzip.open(
            public_dump, "wt", encoding="utf-8", compresslevel=6
        ) as sink,
    ):
        for line in source:
            subject = subject_hash(line)
            if suppressed(line) or (
                subject is not None and subject not in needed
            ):
                dropped += 1
                continue
            sink.write(line)
            kept += 1
    return kept, dropped


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def prune_old_backups(backup_root: Path, keep: int) -> list[str]:
    """Delete the oldest timestamped backup dirs beyond `keep`."""
    runs = sorted(
        entry.name
        for entry in backup_root.iterdir()
        if entry.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", entry.name)
    )
    doomed = runs[:-keep] if keep > 0 else []
    for name in doomed:
        shutil.rmtree(backup_root / name)
    return doomed


def export_public(
    config: Config,
    out_dir: Path,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    run: sparql.Runner | None = None,
    vocabulary: tuple[dict[str, str], dict[str, str]] | None = None,
    lag_timeout: float = LAG_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Write the public export of the dump ``out_dir/full.nt.gz``:
    ``public.nt.gz``, and ``public-exclusions.json``, which lists every
    item dropped for privacy with why, and the people released. Returns
    the counts the backup's manifest records.

    It reads the dump first, then waits for the query service to hold
    exactly the dump's revisions (``await_query_service``), runs the check
    there, and asks again once it has run: a graph that changed meanwhile
    raises QueryServiceLag, and nothing is written. Every item with person
    evidence that the check did not release is then dropped
    (``public_exclusions``). The queries go to the instance's query
    service, with its vocabulary, unless `run` and `vocabulary` are
    given."""
    if vocabulary is None:
        from yizkr.core.wiki import WikiClient

        vocabulary = WikiClient(config).vocabulary()
    properties, items = vocabulary
    run = run or sparql.runner(config)
    full_dump = out_dir / "full.nt.gz"
    reading = read_dump(full_dump, properties, items)
    await_query_service(
        run, reading.revisions, timeout=lag_timeout, sleep=sleep, clock=clock
    )
    report = exclusion_report(
        config, cutoff_years=cutoff_years, run=run, vocabulary=vocabulary
    )
    changed = stale_items(run, reading.revisions)
    if changed:
        raise QueryServiceLag(
            "the graph changed while the public export assessed it: the "
            "query service now holds another revision than the dump of "
            f"{_named(changed)}. The export refuses to run; run the backup "
            "again."
        )
    excluded = public_exclusions(reading, report)
    kept, dropped = filter_public_export(
        full_dump,
        out_dir / "public.nt.gz",
        set(excluded),
        excerpt_pids={properties["transcription_excerpt"]},
    )
    (out_dir / "public-exclusions.json").write_text(
        json.dumps(
            {
                "cutoff_years": cutoff_years,
                "excluded": {qid: excluded[qid] for qid in sorted(excluded)},
                "included_persons": sorted(report.included),
            },
            indent=2,
        )
    )
    return {
        "excluded_persons": len(excluded),
        "included_persons": len(report.included),
        "triples_kept": kept,
        "triples_dropped": dropped,
    }


def run_backup(
    config: Config,
    backup_root: Path,
    deploy_dir: Path,
    compose_files: list[str] | None = None,
    skip_media: bool = False,
    public_export: bool = True,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    keep: int = 0,
) -> dict:
    """Full backup run; returns the manifest that is also written to disk.

    A public export that refuses to run (QueryServiceLag) leaves the other
    dumps in place: the manifest records them and the refusal, old runs
    are pruned, and the refusal is raised."""
    stamp = datetime.datetime.now(datetime.UTC).strftime(TIMESTAMP_FORMAT)
    out_dir = backup_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"timestamp": stamp, "components": {}}

    def record(name: str, path: Path, **extra) -> None:
        manifest["components"][name] = {
            "file": path.name,
            "bytes": path.stat().st_size,
            **extra,
        }

    dump_database(deploy_dir, out_dir / "database.sql.gz", compose_files)
    record("database", out_dir / "database.sql.gz")

    if not skip_media:
        dump_media(deploy_dir, out_dir / "media.tar.gz", compose_files)
        record("media", out_dir / "media.tar.gz")

    dump_rdf(deploy_dir, out_dir / "full.nt.gz", compose_files)
    record("rdf", out_dir / "full.nt.gz")

    refused = None
    if public_export:
        if not config.sparql_url:
            raise RuntimeError(
                "public export needs the query service (WDQS_PUBLIC_HOST) to "
                "compute possibly-living exclusions"
            )
        try:
            record(
                "public_export",
                out_dir / "public.nt.gz",
                **export_public(config, out_dir, cutoff_years=cutoff_years),
            )
        except QueryServiceLag as refusal:
            manifest["public_export_refused"] = str(refusal)
            refused = refusal

    manifest["pruned"] = prune_old_backups(backup_root, keep) if keep else []
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if refused is not None:
        raise refused
    return manifest
