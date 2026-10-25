"""Backup and export.

One `yizkr backup` run produces a timestamped directory containing
everything needed to rebuild the instance, plus a non-privileged export:

* ``database.sql.gz`` — mariadb dump of the MediaWiki/Wikibase database
  (THE authoritative backup: items, statements, history, users).
* ``media.tar.gz`` — the wiki's uploaded files (scanned artifacts).
* ``full.nt.gz`` — complete RDF dump (portable export; also the input
  for the non-privileged variant). WDQS state is derived and never
  backed up: it reloads from this dump.
* ``nonprivileged.nt.gz`` — the non-privileged export, which leaves out
  every possibly-living person (R7, docs/MODELING.md "Living persons and
  privacy"). Nothing about a possibly-living person leaves the instance
  in it: every triple that mentions an excluded person — as subject,
  statement node, or object of someone else's claim — is dropped, and a
  statement whose value, or a qualifier's value, is an item left out
  goes whole, with its rank, dates, qualifiers and references. All else
  leaves as it is, text included: whatever is recorded about a person
  the check releases, and about the other items it keeps.

Exclusion is CONSERVATIVE by design and computed at RUNTIME — there is
no stored "living" flag (ADR-0020). A person is excluded unless the graph
gives positive evidence they are not living: any death evidence (a date,
place, or cause of death, a place of burial, or a death event whose
single principal they are: an informant or a witness at someone else's
death, or anyone in a death with no role or several principals, is not
released by it), or births that all lie more than ``cutoff_years`` ago
(``DEFAULT_CUTOFF_YEARS`` by default), every birth statement a curator has
not deprecated and every dated birth event they are the single principal
of (one that is nobody's own adds its date to the births its participants
have already), read at the latest year it allows. Unknown means excluded.
The determination, and the queries that select people and read their births
and events for it, live in ``yizkr.core.privacy``.

The export keeps only the people that check releases, and so fails
closed:

* The check assesses every item with person evidence, read through
  statement nodes at every rank (``privacy.PEOPLE_SELECTION``), so a
  deprecated or outranked `human` type hides no one.
* The export reads the same evidence from its own dump (``read_dump``)
  and drops every item that has any of it and was not released: an item
  the check never assessed is dropped, not kept. A redirect goes with the
  item it redirects to.
* It also drops every item that states no type, as what such an item is
  cannot be told, so no type at all hides no one either. The instance's
  vocabulary and the classes its items are typed with are kept.
* It drops every family too, released or not: a family stands for its
  people, any of whom may be living (``privacy.FAMILY_CLASS``).
* It drops every event that an item it drops took part in, at any rank,
  as a `participant` statement's value or as an item a qualifier of
  that statement names, whatever the statement's value, an unknown one
  or none included: what the event records, its date and place, is that
  participant's too. It drops every event that any of its statements
  links to a person it drops, or to an item it drops as one that may be
  a person, as the value or a qualifier's value, whatever the property;
  a union is no event (ADR-0036), nor is an item typed human at normal
  or preferred rank, unless typed a family too. An item it drops only as
  untyped, or as a released group, takes no event of no person kind
  that merely names it. A released group, a family (or any item named a
  `member of`) that names members, read through the groups among them,
  each of which names a member, has no participant of unknown value and,
  if it has person evidence, was released, and whose members it all
  released, takes no event it took part in either, unless of a person
  kind (a class the dump makes a subclass of one included), which is its
  members' own, however it names the group. A person named a `member
  of` is a member whose standing counts, and whose own members are read
  too; a person is never a released group. It drops every event
  of a person kind that names no participant, and every event that has
  participant statements all of unknown value, as whom it happened to is
  unknown; and the events that start and end a union it drops, a
  partner's own death aside.
* It drops the extractor's judgement notes, its remarks on its own
  reading, every caption statement whole, a container's raw words about
  an image it places, which may name possibly living people as text
  (ADR-0039 §7), the container's references, excerpts included, on its
  other claims about that image, each such claim whole that no other
  source states (ADR-0039 §10.6), and any line it cannot read
  (``filter_nonprivileged_export``), and refuses a dump that holds what
  it is not made to filter: a sitelink, or a property of a type the
  model does not use (``UnfilterableDump``).
* The check runs on the query service, the dump comes from the database:
  the export waits for the query service to hold exactly the dump's
  revision of every item and property, and refuses to run when it does
  not, or when the graph changes while the check runs
  (``QueryServiceLag``).
* It writes each file under a ``.partial`` name and moves it onto its
  own once whole, the exclusions before the dump, so a dump under its
  shareable name is always complete and has its exclusions beside it.

Scheduling is the host's job (this is a home-server deployment): see
deploy/README.md for when the project's stage calls for backups
(ADR-0024) and for the cron line. Retention: ``--keep N`` prunes the
oldest runs. A run's directory is named by the UTC second it starts, and
always sorts after every run already there (``next_backup_name``), so the
names order the runs as they were made, whatever the clock does.
"""

from __future__ import annotations

import datetime
import gzip
import json
import os
import re
import shutil
import subprocess
import time
import warnings
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.core import sparql
from yizkr.core.config import Config, read_env_file
from yizkr.core.dates import UnsupportedDate, unsupported_in_graph
from yizkr.core.privacy import (
    DEFAULT_CUTOFF_YEARS,
    EVENT_DEATH_PROPERTIES,
    FAMILY_CLASS,
    NON_PERSON_CLASSES,
    PERSON_CLASS,
    PERSON_EVENTS,
    PERSON_SUBJECT_PROPERTIES,
    PERSON_VALUE_PROPERTIES,
    SAME_AS,
    event_kinds,
    is_possibly_living,
    people_selection,
    read_evidence,
)
from yizkr.queries.publish import prefix_block

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# a backup run's directory name, as TIMESTAMP_FORMAT writes it
TIMESTAMP_NAME = re.compile(r"\d{8}T\d{6}Z")
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
# possibly-living exclusion (non-privileged export) — runtime, no stored flag
# ---------------------------------------------------------------------------


# One row per item with person evidence (``{people}``: the privacy
# module's selection, at every rank), with a value per death signal of
# the person's own statements, bound when the person has that signal:
# only whether it is bound counts, so any value sampled does, an unknown
# value (a blank node) included, since a source stated the death (ADR-0040
# §2.2). A union's end by the person's death, curator-made death events
# and births are read apart (``yizkr.core.privacy.read_evidence``): a death
# event counts only for its principal, and needs no date, so an undated
# death marks its principal deceased just like a burial place or a cause
# of death does. Each of DEATH_EVIDENCE_PROPERTIES has its arm here,
# bound by the name _DEATH_BINDINGS gives it.
PERSON_FACTS_QUERY = """\
SELECT ?person
       (SAMPLE(?deathDate) AS ?death) (SAMPLE(?deathPlace) AS ?deathPlace)
       (SAMPLE(?deathCause) AS ?deathCause) (SAMPLE(?burial) AS ?burial)
       (SAMPLE(?deathAge) AS ?deathAge)
WHERE {{
{people}
  OPTIONAL {{ ?person wdt:{date_of_death} ?deathDate . }}
  OPTIONAL {{ ?person wdt:{place_of_death} ?deathPlace . }}
  OPTIONAL {{ ?person wdt:{cause_of_death} ?deathCause . }}
  OPTIONAL {{ ?person wdt:{burial_place} ?burial . }}
  OPTIONAL {{ ?person wdt:{age_at_death} ?deathAge . }}
}}
GROUP BY ?person
"""

# each of DEATH_EVIDENCE_PROPERTIES, by the binding PERSON_FACTS_QUERY
# gives it
_DEATH_BINDINGS = {
    "date_of_death": "death",
    "place_of_death": "deathPlace",
    "cause_of_death": "deathCause",
    "burial_place": "burial",
    "age_at_death": "deathAge",
}


@dataclass
class ExclusionReport:
    """The check's call on every item with person evidence: `excluded`
    holds those it takes for possibly living, `included` those it
    releases, and `reasons` says why, for each of them."""

    excluded: set[str] = field(default_factory=set)
    included: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)


def utc_now(
    now: Callable[[], datetime.datetime] | None = None,
) -> datetime.datetime:
    """The time `now` reads (the system's UTC clock by default), in UTC.
    A clock that gives a datetime with no time zone is refused
    (ValueError): its zone would be guessed."""
    reading = (now or (lambda: datetime.datetime.now(datetime.UTC)))()
    if reading.utcoffset() is None:
        raise ValueError(
            f"the clock read {reading.isoformat()}, a time with no time "
            "zone; give it one"
        )
    return reading.astimezone(datetime.UTC)


def exclusion_report(
    config: Config,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    run: sparql.Runner | None = None,
    vocabulary: tuple[dict[str, str], dict[str, str]] | None = None,
    *,
    now: Callable[[], datetime.datetime] | None = None,
) -> ExclusionReport:
    """Which people the non-privileged export must not mention, and why: every
    item the privacy module's selection finds. The queries go to the
    instance's query service, with its vocabulary, unless `run` and
    `vocabulary` are given. The cutoff year is `cutoff_years` before the
    year `now` reads (the system's UTC clock by default). A graph holding a
    date before the common era or in a calendar other than the proleptic
    Gregorian anywhere, at any rank, raises UnsupportedDate naming its items
    and statements, before anyone is assessed: the export stops
    (ADR-0034)."""
    if vocabulary is None:
        from yizkr.core.wiki import WikiClient

        vocabulary = WikiClient(config).vocabulary()
    properties, items = vocabulary
    run = run or sparql.runner(config)
    prefixes = prefix_block(config.concept_uri)
    unsupported_in_graph(run, prefixes)
    bindings = run(
        prefixes
        + PERSON_FACTS_QUERY.format(
            people=people_selection(properties, items),
            instance_of=properties["instance_of"],
            date_of_death=properties["date_of_death"],
            place_of_death=properties["place_of_death"],
            cause_of_death=properties["cause_of_death"],
            burial_place=properties["burial_place"],
            age_at_death=properties["age_at_death"],
        )
    )
    evidence = read_evidence(run, prefixes, properties, items)

    cutoff_year = utc_now(now).year - cutoff_years
    report = ExclusionReport()
    for binding in bindings:
        qid = binding["person"]["value"].rsplit("/", 1)[-1]
        has_death = qid in evidence.died or any(
            binding.get(name, {}).get("value")
            for name in _DEATH_BINDINGS.values()
        )
        possibly_living, reason = is_possibly_living(
            evidence.births.get(qid, []), has_death, cutoff_year
        )
        report.reasons[qid] = reason
        (report.excluded if possibly_living else report.included).add(qid)
    return report


# The dump in N-Triples, as dumpRdf.php writes it: an item's statement
# links it to a statement node (``<item> p:P <statement>``), which holds
# the value (``<statement> ps:P <value>``), each qualifier's value
# (``<statement> pq:P <value>``) and the rank (``wikibase:rank``); a data
# node (``Special:EntityData/Q1``) gives each entity's revision
# (``schema:version``), a redirect's included; and a redirect is
# ``owl:sameAs`` the item it redirects to.
_ENTITY = re.compile(r"<[^<>]*/entity/(Q[1-9]\d*)>")
_CLAIM = re.compile(r"<[^<>]*/prop/(P[1-9]\d*)>")
_CLAIM_VALUE = re.compile(r"<[^<>]*/prop/statement/(P[1-9]\d*)>")
_QUALIFIER_VALUE = re.compile(r"<[^<>]*/prop/qualifier/(P[1-9]\d*)>")
_DIRECT = re.compile(r"<[^<>]*/prop/direct/(P[1-9]\d*)>")
_DATA_NODE = re.compile(r"<[^<>]*[/:]([QP][1-9]\d*)>")
_VERSION = "<http://schema.org/version>"
_RANK = "<http://wikiba.se/ontology#rank>"
_DEPRECATED = "<http://wikiba.se/ontology#DeprecatedRank>"
# A property's type (``wikibase:propertyType``), and what a page is about
# (``schema:about``): an entity's data node, or a sitelink's page.
_PROPERTY_TYPE = "<http://wikiba.se/ontology#propertyType>"
_PROPERTY = re.compile(r"<[^<>]*/entity/(P[1-9]\d*)>")
_ABOUT = "<http://schema.org/about>"
_ENTITY_DATA = re.compile(r"<[^<>]*/Special:EntityData/[QP][1-9]\d*>")

# The properties by which a union, a marriage or a partnership, points to
# the events that start and end it (ADR-0036 §6): they are the couple's, and
# go with the union. Keys of the model manifest; one the instance's
# vocabulary does not hold reads nothing. `ended_by_death_of` is not among
# them: its value is a partner, who follows the person rule through
# PERSON_VALUE_PROPERTIES alone, so a released partner stays in the export
# when the union is left out (ADR-0040 §5).
UNION_BOUNDS = ("started_by", "ended_by")

# The class of every union, a partnership or a marriage (ADR-0036): an
# entity, not an event. Its `started_by` and `ended_by` statements point to
# its events, which go with it (``BOUND_LEFT_OUT``), never the other way.
UNION_CLASS = "partnership"


@dataclass
class DumpReading:
    """What the non-privileged export reads from its own dump: `people`, every
    item with person evidence (the privacy module's, at every rank);
    `untyped`, every item that states no type, the vocabulary and classes
    aside, and `families`, every item typed as a family (``read_dump``);
    `participants`, the items that take part in each event, at every
    rank, those a qualifier of a `participant` statement names included;
    `links`, the items that each event's statements name, as their value
    or a qualifier's, at every rank, whatever the property; `members`, the
    items each item is named a `member of` by, at every rank, through
    redirects; `takers`, the values of each item's own `participant`
    statements, at every rank, its qualifiers aside; `unknown_takers`, the
    items with a `participant` statement of unknown value; `person_events`,
    the items typed, at any rank, as a person kind of event; `names`, the items
    that each item's statements name, as their value or a qualifier's, at
    every rank, whatever the item;
    `unattended`, every event of a person kind whose `participant`
    statements name no item as their value, and every item whose
    `participant` statements all hold an unknown value or none; `bounds`,
    the events that
    start and end each union, a partner's own death aside; `redirects`,
    each redirect's target;
    `revisions`, the revision of every item and property that is not a
    redirect; `property_types`, each property's type as the dump names it;
    and `sitelinks`, every entity that a sitelink is about."""

    people: set[str] = field(default_factory=set)
    untyped: set[str] = field(default_factory=set)
    families: set[str] = field(default_factory=set)
    participants: dict[str, set[str]] = field(default_factory=dict)
    links: dict[str, set[str]] = field(default_factory=dict)
    members: dict[str, set[str]] = field(default_factory=dict)
    takers: dict[str, set[str]] = field(default_factory=dict)
    unknown_takers: set[str] = field(default_factory=set)
    person_events: set[str] = field(default_factory=set)
    names: dict[str, set[str]] = field(default_factory=dict)
    unattended: set[str] = field(default_factory=set)
    bounds: dict[str, set[str]] = field(default_factory=dict)
    redirects: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
    property_types: dict[str, str] = field(default_factory=dict)
    sitelinks: set[str] = field(default_factory=set)


def _item(term: str) -> str | None:
    match = _ENTITY.fullmatch(term)
    return match.group(1) if match else None


def _redirected(redirects: Mapping[str, str], qid: str) -> str:
    """The item `qid` stands for: the one it redirects to, through a chain
    of redirects (a cycle ends where it closes), or itself."""
    seen = {qid}
    while qid in redirects:
        qid = redirects[qid]
        if qid in seen:
            break
        seen.add(qid)
    return qid


def read_dump(
    full_dump: Path, properties: Mapping[str, str], items: Mapping[str, str]
) -> DumpReading:
    """Read person evidence, untyped items, redirects and revisions from
    a dump, by the instance's ids.

    Person evidence is read as ``PEOPLE_SELECTION`` reads it from the query
    service: an item typed `human`, the subject of a person-only property,
    the value of a person-valued one in a statement or a qualifier, or a
    participant in a person event, each through its statement node at
    every rank. A participant typed as one of the non-person classes, at
    normal or preferred rank, is a person only on other evidence, and so is
    an item typed so as an event, and not as a family, on the death
    evidence it states, of each
    property it has a truthy value of (``privacy.EVENT_DEATH_PROPERTIES``),
    read from the dump's truthy triples. A value
    that is not an item (an unknown value is a blank node) is no evidence.

    An item states no type when none of its `instance of` statements at
    normal or preferred rank names an item: an unknown or no value, or a
    type a curator deprecated, is none. The instance's vocabulary (the
    manifest's items, which bootstrap mints untyped) and every item that
    is the value of an `instance of` or `subclass of` statement, at any
    rank, are classes, and not counted; nor is a redirect, which has no
    statements of its own. A type that names a redirect is read as the
    item the redirect leads to.

    An item is a family when an `instance of` statement of it at normal or
    preferred rank names the family class.

    The items that take part in each event are read at every rank,
    whatever the event's type: every item a `participant` statement's
    value names, and every item a qualifier of such a statement names,
    whatever the statement's value, an unknown one or none included.
    Taking part so is no person evidence of its own (a role such a
    qualifier gives is vocabulary), and attends no event: an event of a
    person kind whose `participant` statements name no item as their
    value (none, or only ones whose value is unknown, however qualified)
    is unattended, and so is any item that has `participant` statements,
    all of an unknown value or none, whatever its kind; an item of no
    person kind with no `participant` statement names no one, and is not.

    An event is an item typed, at any rank, with `event`
    (``privacy.EVENT_CLASS``) or a class the manifest makes a subclass of
    it (``event_kinds``), a
    person kind of event, or a class the dump makes a subclass of one, at
    any rank and through redirects; or an item with a `participant`
    statement; but never a union, an item typed, at normal or preferred
    rank, as a partnership, a marriage (``UNION_CLASS``) or a class the
    dump makes a subclass of one at any rank, whose events go with it and
    not it with them; and never an item typed human at normal or
    preferred rank, unless typed a family too, for the participant, link
    or unattended rule. Who is a `member of` each item is read too, at
    every rank (``members``).
    Each is linked to every item that any of its statements names, as its
    value or a qualifier's, at every rank (``links``).

    So are read, at every rank, the events that start and
    end each union (``UNION_BOUNDS``), but a `death` event, which is a
    partner's own: no batch makes one, and `ended_by` takes none, so this
    guards a curator's; and each property's type, and every sitelink, whose
    page is anything but an entity's data node.

    Lines are read as they end, at a newline alone."""
    instance_of = properties["instance_of"]
    subclass_of = properties["subclass_of"]
    participant = properties["participant"]
    member_of = properties.get("member_of")
    bound_links = {
        properties[key] for key in UNION_BOUNDS if key in properties
    }
    subject_links = {properties[key] for key in PERSON_SUBJECT_PROPERTIES}
    death_links = {properties[key] for key in EVENT_DEATH_PROPERTIES}
    value_links = {properties[key] for key in PERSON_VALUE_PROPERTIES}
    human = items[PERSON_CLASS]
    person_kinds = {items[key] for key in PERSON_EVENTS}
    non_person_kinds = {items[key] for key in NON_PERSON_CLASSES}

    reading = DumpReading()
    # typing and participation statements, joined once the whole dump is
    # read, whatever order its lines come in
    typed: dict[str, str] = {}  # statement -> the item it types
    kinds: dict[str, str] = {}  # statement -> the type it gives
    events: dict[str, str] = {}  # statement -> the event it is part of
    participants: dict[str, str] = {}  # statement -> the participant
    qualified: dict[str, set[str]] = {}  # statement -> items qualifiers name
    owners: dict[str, str] = {}  # statement -> the item it is of
    named: dict[
        str, set[str]
    ] = {}  # statement -> items it or a qualifier names
    narrows: dict[str, str] = {}  # statement -> the class it makes a subclass
    broader: dict[str, str] = {}  # statement -> the class it is a subclass of
    # statement -> the group it makes its subject a member of
    memberships: dict[str, str] = {}
    unknown_parts: set[str] = set()  # participant statements of unknown value
    # statement -> the item it states death evidence of, and its property
    death_stated: dict[str, tuple[str, str]] = {}
    # (item, property): the death evidence an item has a truthy value of
    truthy_deaths: set[tuple[str, str]] = set()
    unions: dict[str, str] = {}  # statement -> the union it bounds
    bounds: dict[str, str] = {}  # statement -> the event it points to
    deprecated: set[str] = set()  # statements a curator deprecated
    classes = set(items.values())  # the vocabulary, and every type named
    links: dict[str, tuple[str, str] | None] = {}
    with gzip.open(full_dump, "rt", encoding="utf-8", newline="\n") as source:
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
            if predicate == SAME_AS:
                redirect, target = _item(subject), _item(term)
                if redirect and target:
                    reading.redirects[redirect] = target
                continue
            if predicate == _RANK:
                if term == _DEPRECATED:
                    deprecated.add(subject)
                continue
            if predicate == _PROPERTY_TYPE:
                if prop := _PROPERTY.fullmatch(subject):
                    reading.property_types[prop.group(1)] = term
                continue
            if predicate == _ABOUT:
                if not _ENTITY_DATA.fullmatch(subject):
                    reading.sitelinks.add(_item(term) or term)
                continue
            if predicate not in links:
                links[predicate] = next(
                    (
                        (role, match.group(1))
                        for role, pattern in (
                            ("claim", _CLAIM),
                            ("value", _CLAIM_VALUE),
                            ("qualifier", _QUALIFIER_VALUE),
                            ("direct", _DIRECT),
                        )
                        if (match := pattern.fullmatch(predicate))
                    ),
                    None,
                )
            link = links[predicate]
            if link is None:
                continue
            role, pid = link
            if role == "direct":
                if pid in death_links and (item := _item(subject)):
                    truthy_deaths.add((item, pid))
                continue
            if role == "claim":
                item = _item(subject)
                if item is None:
                    continue
                owners[term] = item
                if pid == instance_of:
                    typed[term] = item
                if pid == subclass_of:
                    narrows[term] = item
                if pid == participant:
                    events[term] = item
                if pid in bound_links:
                    unions[term] = item
                if pid in death_links:
                    death_stated[term] = (item, pid)
                elif pid in subject_links:
                    reading.people.add(item)
                continue
            item = _item(term)
            if item is None:
                if (
                    pid == participant
                    and role == "value"
                    and _unknown(rest.split(" ", 1)[0])
                ):
                    unknown_parts.add(subject)
                continue
            if pid in value_links:
                reading.people.add(item)
            named.setdefault(subject, set()).add(item)
            if role == "qualifier":
                qualified.setdefault(subject, set()).add(item)
                continue
            if pid == instance_of:
                kinds[subject] = item
                classes.add(item)
            if pid == subclass_of:
                classes.add(item)
                broader[subject] = item
            if pid == participant:
                participants[subject] = item
            if pid == member_of:
                memberships[subject] = item
            if pid in bound_links:
                bounds[subject] = item

    # a type is read through the redirects it names: an item typed with a
    # class a curator merged into `human` is typed human, and the class it
    # was merged into is a class
    kinds = {
        statement: _redirected(reading.redirects, kind)
        for statement, kind in kinds.items()
    }
    classes |= {_redirected(reading.redirects, kind) for kind in classes}

    def typed_as(kinds_of: set[str] | None, ranked: bool = False) -> set:
        """The items a statement types as one of `kinds_of` (any type,
        when None): at normal or preferred rank when `ranked`, at any rank
        otherwise."""
        return {
            typed[statement]
            for statement, kind in kinds.items()
            if (kinds_of is None or kind in kinds_of)
            and statement in typed
            and not (ranked and statement in deprecated)
        }

    reading.people |= typed_as({human})
    # an item typed as an event, at normal or preferred rank, is taken at
    # its type on the death evidence it states, of each property it has a
    # truthy value of (``privacy.EVENT_DEATH_PROPERTIES``)
    # but never a family, which stays a person (``privacy.FAMILY_CLASS``)
    events_typed = typed_as(
        {items[key] for key in event_kinds() if key in items}, ranked=True
    ) - typed_as({items[FAMILY_CLASS]}, ranked=True)
    reading.people.update(
        item
        for item, pid in death_stated.values()
        if item not in events_typed or (item, pid) not in truthy_deaths
    )
    # the subclasses the dump declares, read through redirects
    subclasses = {
        (
            _redirected(reading.redirects, narrows[statement]),
            _redirected(reading.redirects, kind),
        )
        for statement, kind in broader.items()
        if statement in narrows
    }

    def closed(kinds: set[str]) -> set[str]:
        """`kinds` and every class the dump makes a subclass of one."""
        kinds = set(kinds)
        while wider := {
            narrow
            for narrow, kind in subclasses
            if kind in kinds and narrow not in kinds
        }:
            kinds |= wider
        return kinds

    # a class the dump makes a subclass of a person kind is one too
    person_events = typed_as(closed(person_kinds))
    reading.person_events = person_events
    set_aside = typed_as(non_person_kinds, ranked=True)
    reading.people.update(
        person
        for statement, person in participants.items()
        if events.get(statement) in person_events and person not in set_aside
    )
    for statement, part in participants.items():
        if statement in events:
            reading.participants.setdefault(events[statement], set()).add(part)
    # an event is attended by its participant statements' values alone; an
    # item a qualifier names takes part all the same, but a role names no
    # one who was there. An event of a person kind that names no item so is
    # unattended, and so is any item that has participant statements, each
    # of an unknown value or none; an item of no person kind with no
    # participant statement names no one, and is not. An item typed human,
    # at normal or preferred rank, unless typed a family too, is never an
    # event for the participant, link or unattended rule
    humans = typed_as({human}, ranked=True)
    reading.families = typed_as({items[FAMILY_CLASS]}, ranked=True)
    # a family stays a family, a human type beside it notwithstanding
    persons = humans - reading.families
    reading.unattended = (person_events | set(events.values())) - (
        set(reading.participants) | persons
    )
    reading.takers = {
        item: set(parts) for item, parts in reading.participants.items()
    }
    reading.unknown_takers = {
        events[statement] for statement in unknown_parts if statement in events
    }
    for person in persons:
        reading.participants.pop(person, None)
    for statement, names in qualified.items():
        if statement in events and events[statement] not in persons:
            reading.participants.setdefault(events[statement], set()).update(
                names
            )
    for statement, group in memberships.items():
        if statement in owners:
            reading.members.setdefault(
                _redirected(reading.redirects, group), set()
            ).add(owners[statement])
    # the event classes: `event` and the manifest's subclasses of it, the
    # person kinds, and every class the dump makes a subclass of one of
    # them, at any rank, read through redirects
    event_classes = person_kinds | {
        items[key] for key in event_kinds() if key in items
    }
    event_classes = closed(event_classes)
    # a union is no event, whatever its kind: a marriage is a person kind
    # for the unattended rule, and its partners are its participants. Its
    # classes are the manifest's and those the dump makes subclasses of one
    unions_typed = typed_as(
        closed(
            {items[key] for key in event_kinds(UNION_CLASS) if key in items}
        ),
        ranked=True,
    )
    # an event is linked to every item that a statement of it names, as its
    # value or a qualifier's, at any rank, whatever the property
    linked = (typed_as(event_classes) | set(events.values())) - (
        unions_typed | persons
    )
    for statement, event in owners.items():
        if event in linked and statement in named:
            reading.links.setdefault(event, set()).update(named[statement])
    for statement, owner in owners.items():
        if statement in named:
            reading.names.setdefault(owner, set()).update(named[statement])
    # a curator's death event that a union is ended by stays its
    # principal's own, never the union's bound
    deaths = typed_as({items["death"]})
    for statement, event in bounds.items():
        if statement in unions and event not in deaths:
            reading.bounds.setdefault(unions[statement], set()).add(event)
    for redirect in reading.redirects:
        reading.revisions.pop(redirect, None)
    reading.untyped = (
        {entity for entity in reading.revisions if entity.startswith("Q")}
        - typed_as(None, ranked=True)
        - classes
    )
    return reading


# Why the export drops an item with person evidence that the check did
# not assess, one that states no type, a family, an event that names no
# participant, an event that items it drops took part in or that one of
# its statements names, and one that starts or ends a union it drops (the
# braces take QIDs).
NOT_ASSESSED = (
    "person evidence in the dump, but the check did not assess this item: "
    "unknown means excluded"
)
FAMILY = (
    "family: its membership is not known to be complete, so any of its "
    "people may be living"
)
NO_TYPE = (
    "no type (no instance of, at normal or preferred rank, that names an "
    "item): unknown means excluded"
)
UNATTENDED = (
    "an event that names no participant (of a person kind), or whose "
    "participants are all of unknown value: unknown means excluded"
)
PARTICIPANT_LEFT_OUT = (
    "a participant the export leaves out ({}): what this event records "
    "is theirs too"
)
LINKED_LEFT_OUT = (
    "a statement of it names an item the export leaves out ({}): what this "
    "event records is theirs too"
)
BOUND_LEFT_OUT = (
    "it starts or ends a union the export leaves out ({}): it is the couple's"
)


def nonprivileged_exclusions(
    reading: DumpReading, report: ExclusionReport
) -> dict[str, str]:
    """Every item the non-privileged export drops, with why:

    * each item with person evidence, in the dump or at the query service,
      that the check did not release: an item it did not assess is
      dropped, not kept (``NOT_ASSESSED``);
    * each item that states no type (``NO_TYPE``), even one the check
      released;
    * each family (``FAMILY``), whatever it holds, released or not;
    * each event of a person kind that names no participant, and each
      item whose `participant` statements all hold an unknown value or
      none (``UNATTENDED``): whom it happened to is unknown;
    * each event that an item it drops takes part in, at any rank, as a
      `participant` statement's value or named in its qualifiers, unless
      that item is a released group (a family, or any item named a
      `member of`, that names at least one member, read through the groups
      among its members, each of which names a member, has no participant
      of unknown value and, if it has person evidence, was released, all
      of them released, and no participant of unknown value; a person
      among its members is a member whose standing counts, and whose own
      members are read too), which takes part harmlessly,
      except in a person kind of event, which is its members' own
      (``PARTICIPANT_LEFT_OUT``, naming them); each event that a statement
      of it names an item it drops, as its value or a qualifier's,
      whatever the property, unless it drops that item only as untyped, or
      as a released group (as defined above) in an event of no person
      kind, which protects no one: an item that may be a person, through
      redirects, that the participant or unattended rule drops too, a
      statement of which names an item that protects people, or that is
      named a `member of` that is not a released group, is not dropped
      only so
      (``LINKED_LEFT_OUT``, naming them); and each event that
      starts or ends a union it drops (``BOUND_LEFT_OUT``, naming the
      union), read through redirects; and so on until no more go, as an
      event dropped so may itself be a participant, or a union.

    A redirect stands for the item it redirects to: it is dropped when
    that item is, and when it has person evidence of its own and that item
    was not released."""

    def target(qid: str) -> str:
        return _redirected(reading.redirects, qid)

    people = reading.people | report.excluded | report.included

    def why(qid: str) -> str | None:
        if qid in reading.families:
            return FAMILY
        if qid in report.excluded:
            return report.reasons[qid]
        if qid in people and qid not in report.included:
            return NOT_ASSESSED
        if qid in reading.untyped:
            return NO_TYPE
        if qid in reading.unattended:
            return UNATTENDED
        return None

    # what is left out only as untyped or as a family protects no one, so an
    # event that merely names it stays (the statement naming it goes): not
    # an item that may be a person, read through redirects, nor one the
    # unattended rule leaves out, nor a group (a family, or any item named
    # a member of) that names no member, its participant statements' values
    # and those named a member of it, or one with a member the check did
    # not release; and what names an item left out, in the loop below
    unreleased = {target(qid) for qid in people} - report.included
    groups = reading.families | set(reading.members)

    # a person named a group, a household recorded under its head, say, is
    # a member whose standing counts, and whose own members are read too;
    # a person is never a released group
    walked = groups - (people - reading.families)

    def released(group: str) -> bool:
        """Whether `group` names at least one member, read through the
        groups among its members, and the check released every one: each
        group so read names a member, has no participant of unknown value
        and, if it has person evidence, was released. A person among them
        counts as a member, and their own members are read too."""
        seen, stack, found = set(), [group], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in reading.unknown_takers or current in unreleased:
                return False
            members = reading.members.get(current, set()) | (
                reading.takers.get(current, set())
            )
            if not members:
                return False
            for member in members:
                member = target(member)
                if member in groups:
                    stack.append(member)
                if member not in walked:
                    found.add(member)
        return bool(found) and found <= report.included

    released_groups = {group for group in walked if released(group)}
    impersonal = (reading.untyped | reading.families) - (
        (groups - released_groups) | unreleased | reading.unattended
    )

    excluded = {}
    candidates = (
        people | reading.untyped | reading.families | reading.unattended
    )
    for qid in candidates | set(reading.redirects):
        found = target(qid)
        reason = why(found)
        if reason is None and qid in people and found not in report.included:
            reason = NOT_ASSESSED
        if reason is not None:
            excluded[qid] = (
                reason if found == qid else f"a redirect to {found}: {reason}"
            )
    # an event goes with the items it drops that take part in it, a
    # participant named through a redirect included, and with a union it
    # drops that it starts or ends; a redirect goes with the event
    while True:
        left_out = set(excluded)
        shielding = {
            qid
            for qid in left_out
            if target(qid) not in impersonal
            or reading.participants.get(target(qid), set())
            & {
                part
                for part in left_out
                if target(part) not in released_groups
            }
        }
        # and what names an item that protects people protects them too
        while grow := {
            qid
            for qid in left_out - shielding
            if reading.names.get(target(qid), set()) & shielding
        }:
            shielding |= grow
        # a group whose members are all released takes part harmlessly,
        # but never in a person kind of event, which is its members' own,
        # however that event names it
        harmless = {
            qid
            for qid in left_out - shielding
            if target(qid) in released_groups
        }
        guarded = shielding | harmless
        gone = {
            event: LINKED_LEFT_OUT.format(
                ", ".join(sorted(naming, key=_order))
            )
            for event, named in reading.links.items()
            if event not in left_out
            and (
                naming := named
                & (guarded if event in reading.person_events else shielding)
            )
        }
        harmed = left_out - harmless
        gone.update(
            (
                event,
                PARTICIPANT_LEFT_OUT.format(
                    ", ".join(sorted(parts & taking, key=_order))
                ),
            )
            for event, parts in reading.participants.items()
            if event not in left_out
            and (
                taking := parts
                & (left_out if event in reading.person_events else harmed)
            )
        )
        for union, bounds in reading.bounds.items():
            if union in left_out:
                for event in {target(bound) for bound in bounds}:
                    if event not in left_out and event not in gone:
                        gone[event] = BOUND_LEFT_OUT.format(union)
        if not gone:
            return excluded
        excluded.update(gone)
        for redirect in reading.redirects:
            found = target(redirect)
            if found in gone and redirect not in excluded:
                excluded[redirect] = (
                    f"a redirect to {found}: {excluded[found]}"
                )


# The revision of every entity the query service holds, redirects left
# out: the dump gives a redirect a revision, which the query service need
# not keep beside its owl:sameAs link, so the two are compared on the
# items and properties that are not redirects. Properties count as much
# as items: the check reads a qualifier's precision through its property,
# and an earliest date whose precision is unread can release a birth that
# contradicts itself.
REVISIONS_QUERY = """\
SELECT ?entity ?version WHERE {
  ?entity <http://schema.org/version> ?version .
  FILTER NOT EXISTS {
    ?entity <http://www.w3.org/2002/07/owl#sameAs> ?target .
  }
}
"""

_HELD_ENTITY = re.compile(r".*/entity/([QP][1-9]\d*)")

# The files the non-privileged export writes into a backup's directory.
NONPRIVILEGED_DUMP = "nonprivileged.nt.gz"
NONPRIVILEGED_EXCLUSIONS = "nonprivileged-exclusions.json"
# The export writes each file under its name plus this suffix and moves it
# onto that name once it is whole: the exclusions first, then the dump, so
# a dump under NONPRIVILEGED_DUMP is always complete and always has its
# exclusions beside it. A failure removes what it left half-written.
PARTIAL_SUFFIX = ".partial"

# How long the export waits for the query service to hold the dump's
# revisions, and how often it asks.
LAG_TIMEOUT = 180.0
LAG_POLL = 5.0


class NonprivilegedExportRefused(RuntimeError):
    """The non-privileged export refuses to run, and writes nothing.
    `manifest` is the backup's, once ``run_backup`` has recorded the
    refusal in it."""

    manifest: dict | None = None


class QueryServiceLag(NonprivilegedExportRefused):
    """The query service does not hold the dump's revisions. The check
    assesses people there, so its release of anyone would not be about
    what the dump holds: the export refuses to run."""


class UnfilterableDump(NonprivilegedExportRefused):
    """The dump holds what the export is not made to filter. A sitelink:
    its page's own triples (its name, its language, its site) never name
    the entity it is about, so leaving that entity out would leave its
    page's name behind. Or a property of a type the model does not use (a
    URL, a media file, a shape), whose values the filter has not been
    reviewed for. The export refuses to run."""


class UnsupportedDateRefused(NonprivilegedExportRefused, UnsupportedDate):
    """The graph holds a date before the common era or in a calendar
    other than the proleptic Gregorian (ADR-0034), named by its item and
    statement. The check reads no such date, so it releases no one: the
    export refuses to run."""


# The types of property the export is made to filter, as the dump names
# them: those the model manifest uses, an item, a string, a text in a
# language, an external identifier, a time, a quantity and a globe
# coordinate. The unit, calendar or globe of a value is an IRI, which
# Wikibase takes only as an entity's.
_MODEL_TYPES = frozenset(
    f"<http://wikiba.se/ontology#{name}>"
    for name in (
        "WikibaseItem",
        "String",
        "Monolingualtext",
        "ExternalId",
        "Time",
        "Quantity",
        "GlobeCoordinate",
    )
)


def unfilterable(reading: DumpReading) -> list[str]:
    """What in the dump the export is not made to filter: each property of
    a type outside ``_MODEL_TYPES``, and each entity a sitelink is
    about."""
    return [
        f"{pid} ({kind.rsplit('#', 1)[-1].rstrip('>')})"
        for pid, kind in sorted(
            reading.property_types.items(), key=lambda pair: _order(pair[0])
        )
        if kind not in _MODEL_TYPES
    ] + [f"{entity} (a sitelink)" for entity in sorted(reading.sitelinks)]


def _order(entity: str) -> tuple[str, int]:
    return entity[0], int(entity[1:])


def stale_entities(
    run: sparql.Runner, revisions: Mapping[str, int]
) -> list[str]:
    """The items and properties whose revision at the query service
    differs from the dump's `revisions`: one it holds at an older or a
    newer revision, at one that is no integer, or not at all, and one it
    holds that the dump lacks (one deleted or created since). Empty when
    the two hold the same entities."""
    held: dict[str, set[int]] = {}
    for row in run(REVISIONS_QUERY):
        entity = _HELD_ENTITY.fullmatch(row.get("entity", {}).get("value", ""))
        if entity is None:
            continue
        version = row.get("version", {}).get("value", "")
        held.setdefault(entity.group(1), set()).add(
            int(version) if version.isdigit() else -1
        )
    stale = {
        entity
        for entity, revision in revisions.items()
        if held.get(entity) != {revision}
    }
    stale.update(set(held) - set(revisions))
    return sorted(stale, key=_order)


def _named(stale: list[str]) -> str:
    """'3 entities (P7, Q1, Q2)', naming ten at most."""
    shown = ", ".join(stale[:10])
    if len(stale) > 10:
        shown += f" and {len(stale) - 10} more"
    return f"{len(stale)} entit{'y' if len(stale) == 1 else 'ies'} ({shown})"


def await_query_service(
    run: sparql.Runner,
    revisions: Mapping[str, int],
    timeout: float = LAG_TIMEOUT,
    poll: float = LAG_POLL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until the query service holds exactly the dump's `revisions`
    (``stale_entities``), asking every `poll` seconds; raise
    QueryServiceLag when it still does not after `timeout` seconds. The
    match is exact, so an entity whose revision the query service never
    receives stops every export until that entity is edited again or the
    query service is reloaded; the refusal names it."""
    deadline = clock() + timeout
    while True:
        stale = stale_entities(run, revisions)
        if not stale:
            return
        if clock() >= deadline:
            raise QueryServiceLag(
                f"after {timeout:.0f} s the query service still does not "
                f"hold the dump's revision of {_named(stale)}. The "
                "non-privileged export assesses people there, so it refuses "
                "to run until the two agree. Run the backup again once the "
                "graph is at rest and the updater has caught up (docker "
                "compose logs wdqs-updater). An entity whose revision never "
                "reaches the query service keeps the export refusing, with "
                "the entity named here, until it is edited again or the "
                "query service is reloaded from a full dump "
                "(deploy/README.md)."
            )
        sleep(poll)


_PROVENANCE_NODE = re.compile(r"<[^>]*/(?:reference|value)/([0-9a-f]{16,})>")

# A triple as dumpRdf.php writes one, on a line of its own: a subject, a
# predicate and an object, one space apart, and a closing dot. The object
# is an IRI, a blank node, or a literal with its escapes, and a language or
# a type.
_IRI = r'<[^\x00-\x20<>"{}|^`\\]*>'
_TRIPLE = re.compile(
    rf"({_IRI}|_:\S+) ({_IRI}) "
    rf'({_IRI}|_:\S+|"(?:[^"\\\n\r]|\\.)*"'
    rf"(?:@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|\^\^{_IRI})?) \."
)

# The statement links the export reads to tell which statements go whole,
# each with its property: an entity's link to its statement (``p:``), the
# statement's value (``ps:``) and its normalized value (``psn:``), and a
# qualifier's value (``pq:``). A best-ranked statement also gives its
# value to its entity as a truthy triple (``wdt:``, and ``wdtn:`` for the
# normalized value). A statement of no value types its node with its
# property's "no value" class (``wdno:``), and so the entity it is
# best-ranked on.
_CLAIM_NORMALIZED = re.compile(
    r"<[^<>]*/prop/statement/value-normalized/(P[1-9]\d*)>"
)
_TRUTHY = re.compile(r"<[^<>]*/prop/direct(-normalized)?/(P[1-9]\d*)>")
_NO_VALUE = re.compile(r"<[^<>]*/prop/novalue/(P[1-9]\d*)>")
_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
# a statement's link to each of its references, and a reference's value
# of a property (``pr:``), as the placed-subject rule reads them
_DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
_REFERENCE_VALUE = re.compile(r"<[^<>]*/prop/reference/(P[1-9]\d*)>")


def _text(term: str) -> bool:
    """Whether `term` is a literal of text: a plain or language-tagged
    one, as a string or a monolingual text value is written, not one of
    a datatype (a time, a quantity, a coordinate)."""
    return term.startswith('"') and not term.endswith(">")


def _unknown(term: str) -> bool:
    """Whether `term` is an unknown value: a blank node, as dumpRdf.php
    writes one, or a skolem IRI, as the query service writes one."""
    return term.startswith("_:") or "/.well-known/genid/" in term


def _statement_link(predicate: str) -> tuple[str, str] | None:
    """What a predicate links a statement to, with its property: "claim"
    (an entity to its statement), "value", "normalized" or "qualifier";
    "rank" or "type", with none; None for anything else."""
    if predicate == _RANK:
        return "rank", ""
    if predicate == _TYPE:
        return "type", ""
    for role, pattern in (
        ("claim", _CLAIM),
        ("value", _CLAIM_VALUE),
        ("normalized", _CLAIM_NORMALIZED),
        ("qualifier", _QUALIFIER_VALUE),
    ):
        if match := pattern.fullmatch(predicate):
            return role, match.group(1)
    return None


def statements_left_out(
    full_dump: Path,
    excluded: set[str],
    left_out_pids: frozenset[str] | set[str] = frozenset(),
    placed: PlacedSubjectRule | None = None,
) -> tuple[set[str], set[tuple[str, str, str, str]], set[tuple[str, str]]]:
    """The statements of the dump that go whole, the truthy triples that
    go with them, and the links from statements to references that go.

    A statement goes whole when its value, or the value of any of its
    qualifiers, is an item of `excluded`, at any rank, or when it is a
    statement of a property of `left_out_pids` (a caption's, ADR-0039
    §7), whatever its value, or, under `placed`, when it is a statement
    of a document-subject property that keeps no reference `stated in`
    an item once its container's references go
    (_placed_subject_statements): a source's claim about an image it
    places, whose excerpt may be a caption's own text (ADR-0039 §10.5),
    so it fails closed. A container's reference is one of a
    document-subject statement whose entity carries a `media SHA-1` and
    that is `stated in` another item, or whose entity is `part of` (at
    any rank) an item it is `stated in`. When the container's claim
    agrees with the image's own and the writer made them one statement,
    the statement stays with the image's own reference, and only the
    link to the container's goes (the returned (statement node,
    reference node) pairs), unless a qualifier of the statement holds
    text, which may be the container's words: then it goes whole. Each
    statement that goes, goes with its node and every triple that holds
    it (the returned node IRIs). A truthy triple goes with it when it
    gave it, and no statement that stays gives it too: when the entity,
    the property and the value (the normalized value, for a normalized
    truthy triple; the "no value" class, for a statement of no value,
    one with no value triple) are those of a statement that goes at
    normal or preferred rank, and of no statement of that entity and
    property that stays at normal or preferred rank. A deprecated
    statement gives no truthy triple, so one that goes takes none.
    An unknown value is a blank node (or a skolem IRI) of its statement's
    own, and so is the one its truthy triple holds, under another label.
    The export does not reproduce Wikibase's labels, so it does not read
    which truthy triple of unknown value a statement gave: when a
    statement of unknown value goes, every truthy triple of unknown value
    of its entity and property goes with it, a kept statement's included,
    whose own node stays ("_:" stands for any such value). Each is
    returned as (entity IRI, property, "" or "-normalized", value).

    The lines may come in any order: a statement is joined to its entity
    once the whole dump is read."""
    owners: dict[str, tuple[str, str]] = {}  # statement -> entity, property
    # (statement, form, value): its value (any unknown one as "_:") and
    # its normalized value
    values: set[tuple[str, str, str]] = set()
    # each "no value" class a statement's node is typed with, its own or a
    # qualifier's: its own only when it has no value triple
    no_value_types: dict[str, set[str]] = {}
    valued: set[str] = set()  # statements with a value triple
    deprecated: set[str] = set()  # statements a curator deprecated
    left_out: set[str] = set()
    links: dict[str, tuple[str, str] | None] = {}
    no_values: dict[str, bool] = {}  # a type -> whether it is "no value"
    # for the placed-subject rule: each statement's references, each
    # reference's `stated in` items, and each `part of` statement's items
    references: dict[str, set[str]] = {}
    stated_in: dict[str, set[str]] = {}
    part_of: dict[str, set[str]] = {}
    worded: set[str] = set()  # statements with a qualifier of text
    with gzip.open(full_dump, "rt", encoding="utf-8", newline="\n") as source:
        for line in source:
            triple = _TRIPLE.fullmatch(line.removesuffix("\n"))
            if triple is None:
                continue
            subject, predicate, value = triple.groups()
            if placed is not None:
                if predicate == _DERIVED_FROM:
                    references.setdefault(subject, set()).add(value)
                    continue
                cited = _REFERENCE_VALUE.fullmatch(predicate)
                if cited is not None:
                    if cited.group(1) == placed.stated_in and _item(value):
                        stated_in.setdefault(subject, set()).add(_item(value))
                    continue
            if predicate not in links:
                links[predicate] = _statement_link(predicate)
            link = links[predicate]
            if link is None:
                continue
            role, pid = link
            if role == "claim":
                owners[value] = (subject, pid)
                if pid in left_out_pids:
                    left_out.add(value)
            elif role == "rank":
                if value == _DEPRECATED:
                    deprecated.add(subject)
            elif role == "type":
                if value not in no_values:
                    no_values[value] = bool(_NO_VALUE.fullmatch(value))
                if no_values[value]:
                    no_value_types.setdefault(subject, set()).add(value)
            else:
                if role in ("value", "qualifier") and _item(value) in excluded:
                    left_out.add(subject)
                if role == "qualifier":
                    if _text(value):
                        worded.add(subject)
                    continue
                if role == "value":
                    valued.add(subject)
                    if (
                        placed is not None
                        and placed.part_of is not None
                        and pid == placed.part_of
                        and _item(value)
                    ):
                        part_of.setdefault(subject, set()).add(_item(value))
                if _unknown(value):
                    value = "_:"
                form = "-normalized" if role == "normalized" else ""
                values.add((subject, form, value))

    unlinked: set[tuple[str, str]] = set()
    if placed is not None:
        whole, unlinked = _placed_subject_statements(
            placed, owners, references, stated_in, part_of, worded
        )
        left_out |= whole
    # what the statements that go give their entities, and what those that
    # stay at normal or preferred rank do; an unknown value is no other
    # statement's, so a kept one keeps no truthy triple of one that goes
    values.update(
        (statement, "", kind)
        for statement, kinds in no_value_types.items()
        if statement not in valued
        for kind in kinds
    )
    gone: set[tuple[str, str, str, str]] = set()
    kept: set[tuple[str, str, str, str]] = set()
    for statement, form, value in values:
        if statement not in owners or statement in deprecated:
            continue
        given = (*owners[statement], form, value)
        if statement in left_out:
            gone.add(given)
        elif value != "_:":
            kept.add(given)
    return left_out, gone - kept, unlinked


@dataclass(frozen=True)
class PlacedSubjectRule:
    """What statements_left_out reads to leave out a container's claims
    about an image it places (ADR-0039 §10.5): the instance's ids of the
    document-subject properties, of `part of`, of `stated in` and of
    `media SHA-1` (None for a property the instance does not hold)."""

    document_subjects: frozenset[str]
    part_of: str | None
    stated_in: str
    media_sha1: str | None = None


def _placed_subject_statements(
    placed: PlacedSubjectRule,
    owners: Mapping[str, tuple[str, str]],
    references: Mapping[str, set[str]],
    stated_in: Mapping[str, set[str]],
    part_of: Mapping[str, set[str]],
    worded: Collection[str] = frozenset(),
) -> tuple[set[str], set[tuple[str, str]]]:
    """What the export leaves out of the document-subject statements that
    one source makes about another artifact it places: (the statements
    that go whole, the (statement, reference) links that go).

    A container's reference is one of a document-subject statement whose
    entity carries a `media SHA-1` (an artifact registered by its bytes,
    as every artifact a source places is, whoever gave the placement) and
    that is `stated in` an item other than that entity, or one of a
    statement whose entity is `part of` an item it is `stated in`. Each
    such link goes, and the reference with it unless a statement that
    stays names it too, its excerpt included. A statement that keeps no
    reference `stated in` an item once its container's references go,
    the container's own claim, goes whole: what stays of its references
    names no source of its value. So does one of `worded`, a statement
    with a qualifier of text (a string or a monolingual text, an excerpt
    or a caption say): the writer merges only claims whose qualifiers
    match, so a merged statement's qualifiers are the container's as much
    as the image's, and their text may be the container's own words,
    which the export cannot tell apart, so it fails closed. `owners`
    gives each statement's entity and property, `references` each
    statement's reference nodes, `stated_in` each reference's items,
    `part_of` each `part of` statement's items, and `worded` the
    statements with a qualifier of text. Every statement counts, whatever
    its rank. An un-held media item and a source's mention of an
    enclosure carry no SHA-1, so their statements keep every reference."""
    containers: dict[str, set[str]] = {}
    for statement, items in part_of.items():
        if statement in owners:
            entity = _item(owners[statement][0])
            containers.setdefault(entity, set()).update(items)
    hashed = {
        _item(entity)
        for entity, pid in owners.values()
        if placed.media_sha1 is not None and pid == placed.media_sha1
    }

    def a_container_s(reference: str, entity: str | None) -> bool:
        cited = stated_in.get(reference, set())
        return bool(
            (entity in hashed and cited - {entity})
            or containers.get(entity, set()) & cited
        )

    whole: set[str] = set()
    links: set[tuple[str, str]] = set()
    for statement, (entity, pid) in owners.items():
        if pid not in placed.document_subjects:
            continue
        item = _item(entity)
        refs = references.get(statement, set())
        theirs = {ref for ref in refs if a_container_s(ref, item)}
        if not theirs:
            continue
        if statement not in worded and any(
            stated_in.get(ref) for ref in refs - theirs
        ):
            links.update((statement, ref) for ref in theirs)
        else:
            whole.add(statement)
    return whole, links


# How the query service's munger reads a dump: entity by entity, in
# blocks. A line of an entity's data node (``Special:EntityData/Q1``)
# starts a block when any line of another subject came since the last one
# began, and so does a redirect's ``owl:sameAs``; a line of the dump's
# header (``wikibase:Dump``) is of no block; every other line is of the
# block it falls in. A redirect's line is matched for an item only, as
# the model has no property redirects and no lexemes. Within a block, the
# munger keeps a statement, value or reference node's lines only when a
# line of that block names it; it reads the block once, in order, and a
# node's lines read before the line that names it register nothing, so
# the export writes each node at the end of its block, after every line
# that names it.
_DUMP_HEADER = "<http://wikiba.se/ontology#Dump>"
_SAME_AS = "<http://www.w3.org/2002/07/owl#sameAs>"


class _Blocks:
    """Count a dump's lines into the munger's blocks, as they are read."""

    def __init__(self) -> None:
        self.index = 0  # the block the lines read fall in
        self.open = False  # whether a line of no data node came since

    def read(self, line: str) -> bool:
        """Count `line` in; True when it starts a new block."""
        subject, predicate = line.split(" ", 2)[:2]
        if subject == _DUMP_HEADER:
            return False
        data = "/Special:EntityData/" in subject
        if not data and not (
            predicate == _SAME_AS and _ENTITY.fullmatch(subject)
        ):
            self.open = True
            return False
        starts = self.open
        if starts:
            self.index += 1
        self.open = not data
        return starts


# The properties whose snaks the export drops, wherever one is written:
# the extractor's judgement notes (ADR-0035), its remarks on its own
# reading, which are no information on anyone. Keys of the model manifest;
# one the instance's vocabulary does not hold drops nothing.
DROPPED_PROPERTIES = ("judgement", "judgement_note")

# The properties whose statements the export leaves out whole, with their
# references and the excerpts only they hold: a container's captions
# (ADR-0039 §7), raw source quotations that may name possibly living
# people as text the export cannot read, so they fail closed. Keys of the
# model manifest; one the instance's vocabulary does not hold leaves out
# nothing.
LEFT_OUT_PROPERTIES = ("caption",)


def placed_subject_rule(
    properties: Mapping[str, str],
) -> PlacedSubjectRule | None:
    """The export's rule for a container's claims about an image it places
    (PlacedSubjectRule), from the instance's property ids by manifest key,
    or None when the instance holds no `stated in` property, when no
    statement names its source."""
    from yizkr.model.schema import load_acceptance

    if "stated_in" not in properties:
        return None
    return PlacedSubjectRule(
        document_subjects=frozenset(
            properties[key]
            for key in load_acceptance().document_subjects
            if key in properties
        ),
        part_of=properties.get("part_of"),
        stated_in=properties["stated_in"],
        media_sha1=properties.get("media_sha1"),
    )


def filter_nonprivileged_export(
    full_dump: Path,
    nonprivileged_dump: Path,
    excluded: set[str],
    dropped_pids: frozenset[str] | set[str] = frozenset(),
    left_out_pids: frozenset[str] | set[str] = frozenset(),
    placed: PlacedSubjectRule | None = None,
) -> tuple[int, int]:
    """Produce the export's dump: five filters over N-triples. `excluded`
    holds every item to drop: for the export, every item with person
    evidence that the check did not release, and the rest
    ``nonprivileged_exclusions`` names. `dropped_pids` holds the properties
    whose snaks are dropped: for the export, those of ``DROPPED_PROPERTIES``.
    `left_out_pids` holds the properties whose statements go whole: for
    the export, those of ``LEFT_OUT_PROPERTIES``. `placed`, when given,
    leaves out a container's references from the document-subject
    statements about an image it places, and a statement whole that keeps
    no reference stated in an item without them (``PlacedSubjectRule``,
    ``statements_left_out``).

    1. Drop every line that is not a triple as dumpRdf.php writes one
       (``_TRIPLE``): what the filter cannot read, it does not pass on.
       Lines end at a newline alone.
    2. Drop every triple that mentions an excluded entity: entity URIs,
       statement node URIs (entity/statement/Q76-...) of its own
       statements, and object references all carry the QID as a
       delimited token, so token matching catches a person wherever they
       appear — including in other people's claims.
    3. Drop whole every statement whose value, or a qualifier's value,
       is an excluded item (``statements_left_out``): a statement node
       carries its subject's QID, not its value's, so a kept item's
       statement about an excluded one would otherwise keep its rank,
       its qualifiers (a wedding's date, say) and its references. Every
       triple whose subject or object is such a node goes, and so does a
       truthy triple that only such a statement gave. So does every
       statement of a property of `left_out_pids`, whatever its value.
       Under `placed`, a document-subject statement about an artifact a
       source places loses each link to a reference `stated in` that
       source, and goes whole when no reference `stated in` an item is
       left to it.
    4. Drop every triple of a property of `dropped_pids`, in any form: a
       statement's value, a qualifier's, a reference's, or a truthy one.
    5. Sweep orphaned provenance nodes. Reference/value nodes are
       shared, content-addressed resources; once the statements using
       them are dropped, their triples would still leak what those
       statements recorded about an excluded person. Only nodes still
       reachable from a kept statement survive.

    Everything else leaves as it is, its text included: the labels,
    descriptions and aliases of the items kept, their statements' values,
    and the excerpts of the references their statements keep.

    The lines kept leave in the dump's order, but for the provenance
    nodes. The query service's munger reads a dump entity by entity, in
    blocks (``_Blocks``), and keeps a value or reference node's lines only
    in the block of an entity whose statements name it. dumpRdf.php
    writes each such shared node once, in the block of the first entity
    that uses it; when that entity, or its statement, goes, the node's
    lines would sit in a block that does not name it, and the munger
    would drop them from the statements that keep it. So each kept node
    leaves once, all its lines together, at the end of the first block
    whose kept lines name it: a line of no provenance node, or a kept
    node placed in that block (a value node that only a reference names
    goes with the reference). A node's line repeated in the dump leaves
    once.

    Returns (kept, dropped) line counts: a node's line written counts as
    kept, a repeat of one as dropped. Streams the gzipped dump five times
    (four when `excluded` and `left_out_pids` are empty and `placed` is
    None). It holds in memory the nodes of the statements that go, the
    truthy triples that go with them, the reference links that go, the
    hashes of the provenance nodes kept, the kept nodes'
    lines and the block each is placed in; and, while it reads which
    statements go, each statement's entity, property and value, and the
    statements deprecated.
    """
    dropped_predicates = tuple(f"/{pid}>" for pid in dropped_pids)
    left_out, truthy_left_out, unlinked = (
        statements_left_out(full_dump, excluded, left_out_pids, placed)
        if excluded or left_out_pids or placed is not None
        else (set(), set(), set())
    )
    # what each predicate of a truthy triple gives, and whether each type
    # is a property's "no value" class, read once
    forms: dict[str, tuple[str, str] | None] = {}
    no_values: dict[str, re.Match | None] = {}

    def truthy(subject: str, predicate: str, value: str) -> tuple | None:
        """The triple as ``statements_left_out`` returns a truthy one, an
        unknown value as "_:", or None when it is none."""
        if predicate == _TYPE:
            if value not in no_values:
                no_values[value] = _NO_VALUE.fullmatch(value)
            no_value = no_values[value]
            if no_value is None:
                return None
            return subject, no_value.group(1), "", value
        if predicate not in forms:
            match = _TRUTHY.fullmatch(predicate)
            forms[predicate] = (
                (match.group(2), match.group(1) or "") if match else None
            )
        form = forms[predicate]
        if form is None:
            return None
        return subject, *form, "_:" if _unknown(value) else value

    def suppressed(line: str) -> bool:
        triple = _TRIPLE.fullmatch(line.removesuffix("\n"))
        if triple is None:
            return True
        if excluded and any(
            token in excluded for token in _QID_TOKEN.findall(line)
        ):
            return True
        subject, predicate, value = triple.groups()
        if subject in left_out or value in left_out:
            return True
        if (
            unlinked
            and predicate == _DERIVED_FROM
            and ((subject, value) in unlinked)
        ):
            return True
        if truthy_left_out and truthy(subject, predicate, value) in (
            truthy_left_out
        ):
            return True
        return "/prop/" in predicate and predicate.endswith(dropped_predicates)

    def subject_hash(line: str) -> str | None:
        match = _PROVENANCE_NODE.fullmatch(line.split(" ", 1)[0])
        return match.group(1) if match else None

    def dump_lines():
        # each pass reads the same lines, ended at a newline alone
        return gzip.open(full_dump, "rt", encoding="utf-8", newline="\n")

    # pass 1: which provenance nodes do the surviving statements need?
    needed: set[str] = set()
    with dump_lines() as source:
        for line in source:
            if suppressed(line) or subject_hash(line) is not None:
                continue
            needed.update(_PROVENANCE_NODE.findall(line))
    # ...and which value nodes do the surviving reference nodes need?
    with dump_lines() as source:
        for line in source:
            if suppressed(line):
                continue
            subject = subject_hash(line)
            if subject is not None and subject in needed:
                needed.update(_PROVENANCE_NODE.findall(line.split(" ", 1)[1]))

    # pass 2: each kept node's lines, once each, and the block it sits in;
    # and the first block that each node is named in by a kept line of
    # no provenance node
    nodes: dict[str, dict[str, None]] = {}
    sits: dict[str, int] = {}
    placed: dict[str, int] = {}
    blocks = _Blocks()
    with dump_lines() as source:
        for line in source:
            if suppressed(line):
                continue
            subject = subject_hash(line)
            if subject is not None:
                if subject in needed:
                    nodes.setdefault(subject, {})[line] = None
                    sits.setdefault(subject, blocks.index)
                continue
            blocks.read(line)
            for node in _PROVENANCE_NODE.findall(line):
                placed.setdefault(node, blocks.index)
    # a node a kept node names goes in that node's block when it is the
    # earlier one; a node named by no line placed sits where it is
    names = {
        node: {
            named
            for line in lines
            for named in _PROVENANCE_NODE.findall(line.split(" ", 1)[1])
            if named in nodes and named != node
        }
        for node, lines in nodes.items()
    }
    moved = True
    while moved:
        moved = False
        for node, named in names.items():
            if node not in placed:
                continue
            for other in named:
                if placed[node] < placed.get(other, placed[node] + 1):
                    placed[other] = placed[node]
                    moved = True
    ends: dict[int, list[str]] = {}
    for node, lines in nodes.items():
        ends.setdefault(placed.get(node, sits[node]), []).extend(lines)

    # pass 3: write everything that survives, each node at the end of its
    # block
    kept = dropped = 0
    node_lines = 0

    def flush(block: int) -> None:
        nonlocal kept
        for line in ends.pop(block, ()):
            sink.write(line)
            kept += 1

    blocks = _Blocks()
    with (
        dump_lines() as source,
        gzip.open(
            nonprivileged_dump,
            "wt",
            encoding="utf-8",
            newline="\n",
            compresslevel=6,
        ) as sink,
    ):
        for line in source:
            subject = subject_hash(line)
            if suppressed(line) or (
                subject is not None and subject not in needed
            ):
                dropped += 1
                continue
            if subject is not None:
                node_lines += 1
                continue
            if blocks.read(line):
                flush(blocks.index - 1)
            sink.write(line)
            kept += 1
        for block in sorted(ends):
            flush(block)
    written = sum(len(lines) for lines in nodes.values())
    return kept, dropped + node_lines - written


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _stamp(moment: datetime.datetime) -> str:
    """`moment` as TIMESTAMP_FORMAT writes it, its year in four digits
    whatever the platform's strftime does with a year before 1000."""
    return f"{moment.year:04d}" + moment.strftime("%m%dT%H%M%SZ")


def next_backup_name(
    backup_root: Path, *, now: Callable[[], datetime.datetime] | None = None
) -> str:
    """The directory name of a new backup run in `backup_root`: the UTC
    second `now` reads (``utc_now``), or, when that does not sort after
    every entry already named like a run (a second run in one second, or a
    clock set back), the second after the latest of them. A new run's name
    so always sorts last, and pruning, which keeps the names that sort
    last, never removes it. An entry named like a run that no second
    sorts after, or that names no time, and sorts last, refuses the run
    (ValueError), naming it."""
    stamp = _stamp(utc_now(now))
    names = (
        sorted(
            entry.name
            for entry in backup_root.iterdir()
            if TIMESTAMP_NAME.fullmatch(entry.name)
        )
        if backup_root.is_dir()
        else []
    )
    if not names or stamp > names[-1]:
        return stamp
    latest = names[-1]
    try:
        after = datetime.datetime.strptime(latest, TIMESTAMP_FORMAT)
        return _stamp(after + datetime.timedelta(seconds=1))
    except (ValueError, OverflowError):
        raise ValueError(
            f"{backup_root / latest} is named like a backup run that no "
            "new run's name can sort after, so pruning could remove the "
            "new run; move or rename it"
        ) from None


def prune_old_backups(backup_root: Path, keep: int) -> list[str]:
    """Delete the oldest timestamped backup dirs beyond `keep`: those
    whose names sort first."""
    runs = sorted(
        entry.name
        for entry in backup_root.iterdir()
        if entry.is_dir() and TIMESTAMP_NAME.fullmatch(entry.name)
    )
    doomed = runs[:-keep] if keep > 0 else []
    for name in doomed:
        shutil.rmtree(backup_root / name)
    return doomed


def export_nonprivileged(
    config: Config,
    out_dir: Path,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    run: sparql.Runner | None = None,
    vocabulary: tuple[dict[str, str], dict[str, str]] | None = None,
    lag_timeout: float = LAG_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    *,
    now: Callable[[], datetime.datetime] | None = None,
) -> dict:
    """Write the non-privileged export of the dump ``out_dir/full.nt.gz``:
    ``nonprivileged.nt.gz`` (NONPRIVILEGED_DUMP), and
    ``nonprivileged-exclusions.json`` (NONPRIVILEGED_EXCLUSIONS), which lists
    every item dropped for privacy with why (``nonprivileged_exclusions``:
    people, untyped items, families and the events such items took part in,
    redirects to them included), and the people released and kept.
    Returns the counts the backup's manifest records: its
    `excluded_persons` counts every item dropped.

    It reads the dump first, and refuses a dump that holds what the
    export is not made to filter (``unfilterable``: UnfilterableDump). It
    then waits for the query service to hold exactly the dump's revisions
    (``await_query_service``), runs the check there, and asks again once
    it has run: a graph that changed meanwhile raises QueryServiceLag.
    Either refusal writes nothing. Every item with person evidence that
    the check did not release is then dropped (``nonprivileged_exclusions``),
    and so are the judgement notes, every caption statement, whole
    (``LEFT_OUT_PROPERTIES``), every reference that another item gives a
    document-subject statement about an artifact registered by its bytes
    (a source's claim about an image it places, by a record of derived
    media or a curator's `--placed`), or that the item an artifact is
    `part of` gives one about it, with its excerpt, and such a statement
    whole when no reference stated in an item is left to it
    (``placed_subject_rule``), and every statement whose value or a
    qualifier's value is an item dropped, whole
    (``filter_nonprivileged_export``, ``statements_left_out``). The queries
    go to the instance's query service, with its vocabulary,
    unless `run` and `vocabulary` are given. `now` is the check's clock
    (``exclusion_report``).

    Both files are written under their names plus PARTIAL_SUFFIX and moved
    onto their names once whole, the exclusions first. Partial files an
    earlier run left are removed before anything else, refusals included,
    so none can reach the dump. Any failure removes the partial files; a
    failure after the exclusions moved removes them and any dump under its
    name, so a failure leaves no dump without its exclusions, and neither
    file once the exclusions moved."""
    dump = out_dir / NONPRIVILEGED_DUMP
    exclusions = out_dir / NONPRIVILEGED_EXCLUSIONS
    dump_partial = out_dir / (NONPRIVILEGED_DUMP + PARTIAL_SUFFIX)
    exclusions_partial = out_dir / (NONPRIVILEGED_EXCLUSIONS + PARTIAL_SUFFIX)
    for path in (dump_partial, exclusions_partial):
        path.unlink(missing_ok=True)
    if vocabulary is None:
        from yizkr.core.wiki import WikiClient

        vocabulary = WikiClient(config).vocabulary()
    properties, items = vocabulary
    run = run or sparql.runner(config)
    full_dump = out_dir / "full.nt.gz"
    reading = read_dump(full_dump, properties, items)
    unreadable = unfilterable(reading)
    if unreadable:
        raise UnfilterableDump(
            "the dump holds what the export is not made to filter: "
            f"{', '.join(unreadable)}. A sitelink's page is named apart "
            "from its entity, and a property of another type than the "
            "model's has not been reviewed, so the export refuses to run "
            "until it is removed."
        )
    await_query_service(
        run, reading.revisions, timeout=lag_timeout, sleep=sleep, clock=clock
    )
    try:
        report = exclusion_report(
            config,
            cutoff_years=cutoff_years,
            run=run,
            vocabulary=vocabulary,
            now=now,
        )
    except UnsupportedDate as found:
        raise UnsupportedDateRefused(str(found)) from found
    changed = stale_entities(run, reading.revisions)
    if changed:
        raise QueryServiceLag(
            "the graph changed while the non-privileged export assessed it: "
            "the query service now holds another revision than the dump of "
            f"{_named(changed)}. The export refuses to run; run the backup "
            "again."
        )
    excluded = nonprivileged_exclusions(reading, report)
    included = report.included - set(excluded)
    moved = False  # whether this export's exclusions are under their name
    try:
        kept, dropped = filter_nonprivileged_export(
            full_dump,
            dump_partial,
            set(excluded),
            dropped_pids={
                properties[key]
                for key in DROPPED_PROPERTIES
                if key in properties
            },
            left_out_pids={
                properties[key]
                for key in LEFT_OUT_PROPERTIES
                if key in properties
            },
            placed=placed_subject_rule(properties),
        )
        exclusions_partial.write_text(
            json.dumps(
                {
                    "cutoff_years": cutoff_years,
                    "excluded": {
                        qid: excluded[qid] for qid in sorted(excluded)
                    },
                    "included_persons": sorted(included),
                },
                indent=2,
            )
        )
        os.replace(exclusions_partial, exclusions)
        moved = True
        os.replace(dump_partial, dump)
    except BaseException:
        for path in (dump_partial, exclusions_partial):
            path.unlink(missing_ok=True)
        if moved:
            # The exclusions under the name are this export's, so any dump
            # under its name has lost its own exclusions and goes too.
            for path in (exclusions, dump):
                path.unlink(missing_ok=True)
        raise
    return {
        "excluded_persons": len(excluded),
        "included_persons": len(included),
        "triples_kept": kept,
        "triples_dropped": dropped,
    }


def run_backup(
    config: Config,
    backup_root: Path,
    deploy_dir: Path,
    compose_files: list[str] | None = None,
    skip_media: bool = False,
    nonprivileged_export: bool = True,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    keep: int = 0,
    *,
    now: Callable[[], datetime.datetime] | None = None,
) -> dict:
    """Full backup run; returns the manifest that is also written to disk.

    The run's directory is named by `now` (the system's UTC clock by
    default; ``next_backup_name``), which also sets the export's cutoff
    year (``exclusion_report``). A directory of that name made meanwhile
    by another run stops this one (FileExistsError) rather than being
    shared.

    A non-privileged export that refuses to run
    (NonprivilegedExportRefused: the query service lags, the dump holds
    what the export is not made to filter, or the graph holds an
    unsupported date) leaves the other dumps in place:
    the manifest records them and the refusal, old runs are pruned, and the
    refusal is raised."""
    stamp = next_backup_name(backup_root, now=now)
    out_dir = backup_root / stamp
    out_dir.mkdir(parents=True)
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
    if nonprivileged_export:
        if not config.sparql_url:
            raise RuntimeError(
                "the non-privileged export needs the query service "
                "(WDQS_PUBLIC_HOST) to compute possibly-living exclusions"
            )
        try:
            record(
                "nonprivileged_export",
                out_dir / NONPRIVILEGED_DUMP,
                **export_nonprivileged(
                    config, out_dir, cutoff_years=cutoff_years, now=now
                ),
            )
        except NonprivilegedExportRefused as refusal:
            manifest["nonprivileged_export_refused"] = str(refusal)
            refused = refusal

    manifest["pruned"] = prune_old_backups(backup_root, keep) if keep else []
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if refused is not None:
        refused.manifest = manifest
        raise refused
    return manifest


# The export's former Python names resolve, with a DeprecationWarning that
# names the new one, until the keeper run, when they are removed
# (ADR-0020). The file names have no alias: a backup's directory holds
# NONPRIVILEGED_DUMP and NONPRIVILEGED_EXCLUSIONS only.
_RENAMED = {
    "PublicExportRefused": "NonprivilegedExportRefused",
    "export_public": "export_nonprivileged",
    "filter_public_export": "filter_nonprivileged_export",
    "public_exclusions": "nonprivileged_exclusions",
}


def __getattr__(name: str):
    new = _RENAMED.get(name)
    if new is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"yizkr.backup.{name} is renamed yizkr.backup.{new}; the old name "
        "is removed at the keeper run",
        DeprecationWarning,
        stacklevel=2,
    )
    return globals()[new]
