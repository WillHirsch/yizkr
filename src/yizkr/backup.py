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
no stored "living" flag (ADR-0020). A human is excluded unless the graph
gives positive evidence they are not living: any death evidence (a date,
place, or cause of death, a place of burial, or a death event), or birth
more than ``cutoff_years`` (default 100) ago. Unknown means excluded.
The determination itself lives in ``yizkr.core.privacy``.

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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.core.config import Config, read_env_file
from yizkr.core.privacy import DEFAULT_CUTOFF_YEARS
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


# One row per human with a boolean per death signal. A death event needs
# only the person as participant — no date — so an undated death marks a
# person deceased just like a burial place or a cause of death does.
PERSON_FACTS_QUERY = """\
SELECT ?person (SAMPLE(?birthDate) AS ?birth)
       (SAMPLE(?deathDate) AS ?death) (SAMPLE(?deathPlace) AS ?deathPlace)
       (SAMPLE(?deathCause) AS ?deathCause) (SAMPLE(?burial) AS ?burial)
       (SAMPLE(?deathEventItem) AS ?deathEvent)
WHERE {{
  ?person wdt:{instance_of} wd:{human} .
  OPTIONAL {{ ?person wdt:{date_of_birth} ?birthDate . }}
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
    excluded: set[str] = field(default_factory=set)
    included: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)


def exclusion_report(
    config: Config, cutoff_years: int = DEFAULT_CUTOFF_YEARS
) -> ExclusionReport:
    """Which humans the public export must not mention, and why."""
    from yizkr.core.privacy import is_possibly_living, year_of
    from yizkr.core.wiki import WikiClient

    properties, items = WikiClient(config).vocabulary()
    query = prefix_block(config.concept_uri) + PERSON_FACTS_QUERY.format(
        instance_of=properties["instance_of"],
        date_of_birth=properties["date_of_birth"],
        date_of_death=properties["date_of_death"],
        place_of_death=properties["place_of_death"],
        cause_of_death=properties["cause_of_death"],
        burial_place=properties["burial_place"],
        participant=properties["participant"],
        human=items["human"],
        death=items["death"],
    )
    url = (
        config.sparql_url
        + "?"
        + urllib.parse.urlencode({"query": query, "format": "json"})
    )
    with urllib.request.urlopen(url, timeout=120) as response:
        bindings = json.load(response)["results"]["bindings"]

    cutoff_year = datetime.datetime.now(datetime.UTC).year - cutoff_years
    report = ExclusionReport()
    for binding in bindings:
        qid = binding["person"]["value"].rsplit("/", 1)[-1]
        birth_year = year_of(binding.get("birth", {}).get("value", ""))
        has_death = any(
            binding.get(name, {}).get("value") for name in _DEATH_BINDINGS
        )
        possibly_living, reason = is_possibly_living(
            birth_year, has_death, cutoff_year
        )
        report.reasons[qid] = reason
        (report.excluded if possibly_living else report.included).add(qid)
    return report


_PROVENANCE_NODE = re.compile(r"<[^>]*/(?:reference|value)/([0-9a-f]{16,})>")


def filter_public_export(
    full_dump: Path,
    public_dump: Path,
    excluded: set[str],
    excerpt_pids: frozenset[str] | set[str] = frozenset(),
) -> tuple[int, int]:
    """Produce the public dump: three privacy filters over N-triples.

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
    """Full backup run; returns the manifest that is also written to disk."""
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

    if public_export:
        if not config.sparql_url:
            raise RuntimeError(
                "public export needs the query service (WDQS_PUBLIC_HOST) to "
                "compute possibly-living exclusions"
            )
        from yizkr.core.wiki import WikiClient

        report = exclusion_report(config, cutoff_years=cutoff_years)
        properties, _ = WikiClient(config).vocabulary()
        kept, dropped_lines = filter_public_export(
            out_dir / "full.nt.gz",
            out_dir / "public.nt.gz",
            report.excluded,
            excerpt_pids={properties["transcription_excerpt"]},
        )
        (out_dir / "public-exclusions.json").write_text(
            json.dumps(
                {
                    "cutoff_years": cutoff_years,
                    "excluded": {
                        qid: report.reasons[qid]
                        for qid in sorted(report.excluded)
                    },
                    "included_persons": sorted(report.included),
                },
                indent=2,
            )
        )
        record(
            "public_export",
            out_dir / "public.nt.gz",
            excluded_persons=len(report.excluded),
            included_persons=len(report.included),
            triples_kept=kept,
            triples_dropped=dropped_lines,
        )

    manifest["pruned"] = prune_old_backups(backup_root, keep) if keep else []
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
