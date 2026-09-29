"""Backup and export.

One `yizkr backup` run produces a timestamped directory containing
everything needed to rebuild the instance, plus a non-privileged export:

* ``database.sql.gz`` — mariadb dump of the MediaWiki/Wikibase database
  (THE authoritative backup: items, statements, history, users).
* ``media.tar.gz`` — the wiki's uploaded files (scanned artifacts).
* ``full.nt.gz`` — complete RDF dump (portable export; also the input
  for the non-privileged variant). WDQS state is derived and never
  backed up: it reloads from this dump.
* ``public.nt.gz`` — the non-privileged export, which leaves out every
  possibly-living person (R7, docs/MODELING.md "Living persons and
  privacy"). Nothing about a possibly-living person leaves the instance
  in it: every triple that mentions an excluded person — as subject,
  statement node, or object of someone else's claim — is dropped. All
  else leaves as it is, text included: whatever is recorded about a
  person the check releases, and about the other items it keeps.

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
* It drops every event that an item it drops took part in, at any rank:
  what the event records, its date and place, is that participant's too.
  So it drops every event of a person kind that names no participant,
  as whom it happened to is unknown, and the events that start and end
  a union it drops (ADR-0036), a partner's own death aside.
* It drops the extractor's judgement notes, its remarks on its own
  reading, and any line it cannot read (``filter_public_export``), and
  refuses a dump that holds what it is not made to filter: a sitelink,
  or a property of a type the model does not use (``UnfilterableDump``).
* The check runs on the query service, the dump comes from the database:
  the export waits for the query service to hold exactly the dump's
  revision of every item and property, and refuses to run when it does
  not, or when the graph changes while the check runs
  (``QueryServiceLag``).

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
    FAMILY_CLASS,
    NON_PERSON_CLASSES,
    PERSON_CLASS,
    PERSON_EVENTS,
    PERSON_SUBJECT_PROPERTIES,
    PERSON_VALUE_PROPERTIES,
    SAME_AS,
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
# the value (``<statement> ps:P <value>``), each qualifier's value
# (``<statement> pq:P <value>``) and the rank (``wikibase:rank``); a data
# node (``Special:EntityData/Q1``) gives each entity's revision
# (``schema:version``), a redirect's included; and a redirect is
# ``owl:sameAs`` the item it redirects to.
_ENTITY = re.compile(r"<[^<>]*/entity/(Q[1-9]\d*)>")
_CLAIM = re.compile(r"<[^<>]*/prop/(P[1-9]\d*)>")
_CLAIM_VALUE = re.compile(r"<[^<>]*/prop/statement/(P[1-9]\d*)>")
_QUALIFIER_VALUE = re.compile(r"<[^<>]*/prop/qualifier/(P[1-9]\d*)>")
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
# vocabulary does not hold reads nothing.
UNION_BOUNDS = ("started_by", "ended_by")


@dataclass
class DumpReading:
    """What the public export reads from its own dump: `people`, every
    item with person evidence (the privacy module's, at every rank);
    `untyped`, every item that states no type, the vocabulary and classes
    aside, and `families`, every item typed as a family (``read_dump``);
    `participants`, the items that take part in each event, at every
    rank; `unattended`, every event of a person kind that names no item
    taking part; `bounds`, the events that start and end each union, a
    partner's own death aside; `redirects`, each redirect's target;
    `revisions`, the revision of every item and property that is not a
    redirect; `property_types`, each property's type as the dump names it;
    and `sitelinks`, every entity that a sitelink is about."""

    people: set[str] = field(default_factory=set)
    untyped: set[str] = field(default_factory=set)
    families: set[str] = field(default_factory=set)
    participants: dict[str, set[str]] = field(default_factory=dict)
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
    normal or preferred rank, is a person only on other evidence. A value
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
    value names. An event of a person kind that names none (no
    `participant` statement, or only one whose value is unknown) is
    unattended. So are read, at every rank, the events that start and end
    each union (``UNION_BOUNDS``), but a `death` event, which is a
    partner's own; and each property's type, and every sitelink, whose
    page is anything but an entity's data node.

    Lines are read as they end, at a newline alone."""
    instance_of = properties["instance_of"]
    subclass_of = properties["subclass_of"]
    participant = properties["participant"]
    bound_links = {
        properties[key] for key in UNION_BOUNDS if key in properties
    }
    subject_links = {properties[key] for key in PERSON_SUBJECT_PROPERTIES}
    value_links = {properties[key] for key in PERSON_VALUE_PROPERTIES}
    human = items[PERSON_CLASS]
    event_kinds = {items[key] for key in PERSON_EVENTS}
    non_person_kinds = {items[key] for key in NON_PERSON_CLASSES}

    reading = DumpReading()
    # typing and participation statements, joined once the whole dump is
    # read, whatever order its lines come in
    typed: dict[str, str] = {}  # statement -> the item it types
    kinds: dict[str, str] = {}  # statement -> the type it gives
    events: dict[str, str] = {}  # statement -> the event it is part of
    participants: dict[str, str] = {}  # statement -> the participant
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
                        )
                        if (match := pattern.fullmatch(predicate))
                    ),
                    None,
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
                if pid in bound_links:
                    unions[term] = item
                if pid in subject_links:
                    reading.people.add(item)
                continue
            item = _item(term)
            if item is None:
                continue
            if pid in value_links:
                reading.people.add(item)
            if role == "qualifier":
                continue
            if pid == instance_of:
                kinds[subject] = item
                classes.add(item)
            if pid == subclass_of:
                classes.add(item)
            if pid == participant:
                participants[subject] = item
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
    person_events = typed_as(event_kinds)
    set_aside = typed_as(non_person_kinds, ranked=True)
    reading.people.update(
        person
        for statement, person in participants.items()
        if events.get(statement) in person_events and person not in set_aside
    )
    for statement, part in participants.items():
        if statement in events:
            reading.participants.setdefault(events[statement], set()).add(part)
    reading.unattended = person_events - set(reading.participants)
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
    reading.families = typed_as({items[FAMILY_CLASS]}, ranked=True)
    return reading


# Why the export drops an item with person evidence that the check did
# not assess, one that states no type, a family, an event of a person kind
# that names no participant, an event that items it drops took part in,
# and one that starts or ends a union it drops (the braces take QIDs).
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
    "an event of a person kind that names no participant: unknown means "
    "excluded"
)
PARTICIPANT_LEFT_OUT = (
    "a participant the export leaves out ({}): what this event records "
    "is theirs too"
)
BOUND_LEFT_OUT = (
    "it starts or ends a union the export leaves out ({}): it is the couple's"
)


def public_exclusions(
    reading: DumpReading, report: ExclusionReport
) -> dict[str, str]:
    """Every item the public export drops, with why:

    * each item with person evidence, in the dump or at the query service,
      that the check did not release: an item it did not assess is
      dropped, not kept (``NOT_ASSESSED``);
    * each item that states no type (``NO_TYPE``), even one the check
      released;
    * each family (``FAMILY``), whatever it holds, released or not;
    * each event of a person kind that names no participant
      (``UNATTENDED``): whom it happened to is unknown;
    * each event that an item it drops takes part in, at any rank
      (``PARTICIPANT_LEFT_OUT``, naming them), and each event that starts
      or ends a union it drops (``BOUND_LEFT_OUT``, naming the union),
      read through redirects; and so on until no more go, as an event
      dropped so may itself be a participant, or a union.

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
        gone = {
            event: PARTICIPANT_LEFT_OUT.format(
                ", ".join(sorted(parts & left_out, key=_order))
            )
            for event, parts in reading.participants.items()
            if event not in left_out and parts & left_out
        }
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

# How long the export waits for the query service to hold the dump's
# revisions, and how often it asks.
LAG_TIMEOUT = 180.0
LAG_POLL = 5.0


class PublicExportRefused(RuntimeError):
    """The non-privileged export refuses to run, and writes nothing.
    `manifest` is the backup's, once ``run_backup`` has recorded the
    refusal in it."""

    manifest: dict | None = None


class QueryServiceLag(PublicExportRefused):
    """The query service does not hold the dump's revisions. The check
    assesses people there, so its release of anyone would not be about
    what the dump holds: the export refuses to run."""


class UnfilterableDump(PublicExportRefused):
    """The dump holds what the export is not made to filter. A sitelink:
    its page's own triples (its name, its language, its site) never name
    the entity it is about, so leaving that entity out would leave its
    page's name behind. Or a property of a type the model does not use (a
    URL, a media file, a shape), whose values the filter has not been
    reviewed for. The export refuses to run."""


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
                f"hold the dump's revision of {_named(stale)}. The public "
                "export assesses people there, so it refuses to run until "
                "the two agree. Run the backup again once the graph is at "
                "rest and the updater has caught up (docker compose logs "
                "wdqs-updater). An entity whose revision never reaches the "
                "query service keeps the export refusing, with the entity "
                "named here, until it is edited again or the query service "
                "is reloaded from a full dump (deploy/README.md)."
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

# The properties whose snaks the export drops, wherever one is written:
# the extractor's judgement notes (ADR-0035), its remarks on its own
# reading, which are no information on anyone. Keys of the model manifest;
# one the instance's vocabulary does not hold drops nothing.
DROPPED_PROPERTIES = ("judgement", "judgement_note")


def filter_public_export(
    full_dump: Path,
    public_dump: Path,
    excluded: set[str],
    dropped_pids: frozenset[str] | set[str] = frozenset(),
) -> tuple[int, int]:
    """Produce the export's dump: four filters over N-triples. `excluded`
    holds every item to drop: for the export, every item with person
    evidence that the check did not release, and the rest
    ``public_exclusions`` names. `dropped_pids` holds the properties whose
    snaks are dropped: for the export, those of ``DROPPED_PROPERTIES``.

    1. Drop every line that is not a triple as dumpRdf.php writes one
       (``_TRIPLE``): what the filter cannot read, it does not pass on.
       Lines end at a newline alone.
    2. Drop every triple that mentions an excluded entity: entity URIs,
       statement node URIs (entity/statement/Q76-...), and object
       references all carry the QID as a delimited token, so token
       matching catches a person wherever they appear — including in
       other people's claims.
    3. Drop every triple of a property of `dropped_pids`, in any form: a
       statement's value, a qualifier's, a reference's, or a truthy one.
    4. Sweep orphaned provenance nodes. Reference/value nodes are
       shared, content-addressed resources; once the statements using
       them are dropped, their triples would still leak what those
       statements recorded about an excluded person. Only nodes still
       reachable from a kept statement survive.

    Everything else leaves as it is, its text included: the labels,
    descriptions and aliases of the items kept, their statements' values,
    and the excerpts of the references their statements keep.

    Returns (kept, dropped) line counts. Streams the gzipped dump three
    times, holding only node-hash sets in memory.
    """
    dropped_predicates = tuple(f"/{pid}>" for pid in dropped_pids)

    def suppressed(line: str) -> bool:
        triple = _TRIPLE.fullmatch(line.removesuffix("\n"))
        if triple is None:
            return True
        if excluded and any(
            token in excluded for token in _QID_TOKEN.findall(line)
        ):
            return True
        predicate = triple.group(2)
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

    # pass 2: write everything that survives
    kept = dropped = 0
    with (
        dump_lines() as source,
        gzip.open(
            public_dump, "wt", encoding="utf-8", newline="\n", compresslevel=6
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
    """Write the non-privileged export of the dump ``out_dir/full.nt.gz``:
    ``public.nt.gz``, and ``public-exclusions.json``, which lists every
    item dropped for privacy with why (``public_exclusions``: people,
    untyped items, families and the events such items took part in,
    redirects to them included), and the people released and kept.
    Returns the counts the backup's manifest records: its
    `excluded_persons` counts every item dropped.

    It reads the dump first, and refuses a dump that holds what the
    export is not made to filter (``unfilterable``: UnfilterableDump). It
    then waits for the query service to hold exactly the dump's revisions
    (``await_query_service``), runs the check there, and asks again once
    it has run: a graph that changed meanwhile raises QueryServiceLag.
    Either refusal writes nothing. Every item with person evidence that
    the check did not release is then dropped (``public_exclusions``),
    and so are the judgement notes (``filter_public_export``). The
    queries go to the instance's query service, with its vocabulary,
    unless `run` and `vocabulary` are given."""
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
    report = exclusion_report(
        config, cutoff_years=cutoff_years, run=run, vocabulary=vocabulary
    )
    changed = stale_entities(run, reading.revisions)
    if changed:
        raise QueryServiceLag(
            "the graph changed while the public export assessed it: the "
            "query service now holds another revision than the dump of "
            f"{_named(changed)}. The export refuses to run; run the backup "
            "again."
        )
    excluded = public_exclusions(reading, report)
    included = report.included - set(excluded)
    kept, dropped = filter_public_export(
        full_dump,
        out_dir / "public.nt.gz",
        set(excluded),
        dropped_pids={
            properties[key] for key in DROPPED_PROPERTIES if key in properties
        },
    )
    (out_dir / "public-exclusions.json").write_text(
        json.dumps(
            {
                "cutoff_years": cutoff_years,
                "excluded": {qid: excluded[qid] for qid in sorted(excluded)},
                "included_persons": sorted(included),
            },
            indent=2,
        )
    )
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
    public_export: bool = True,
    cutoff_years: int = DEFAULT_CUTOFF_YEARS,
    keep: int = 0,
) -> dict:
    """Full backup run; returns the manifest that is also written to disk.

    A non-privileged export that refuses to run (PublicExportRefused: the
    query service lags, or the dump holds what the export is not made to
    filter) leaves the other dumps in place: the manifest records them and
    the refusal, old runs are pruned, and the refusal is raised."""
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
        except PublicExportRefused as refusal:
            manifest["public_export_refused"] = str(refusal)
            refused = refusal

    manifest["pruned"] = prune_old_backups(backup_root, keep) if keep else []
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if refused is not None:
        refused.manifest = manifest
        raise refused
    return manifest
