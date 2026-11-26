"""A lane fixture written straight to the wiki, in the shapes an instance
holds.

`Fixture` writes items through WikibaseIntegrator, never through the
pipeline's writer: a source's reading carries a `stated in` block with its
page and excerpt, and a machine reading also the block of its source's
extraction run; a curator's statement carries the `stated in` block
alone; an artifact's own kind cites the artifact, and a page fragment's or
an embedded photograph's kind and `part of` cite its whole; a curator's
rank is the rank the statement holds. Legacy `spouse` statements and the
curator-made birth and death events the readers still read are written in
the same way. `synchronize` waits until the query service holds what was
written.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable

import pytest
from wikibaseintegrator.datatypes import (
    GlobeCoordinate,
    Item,
    MonolingualText,
    String,
    Time,
)
from wikibaseintegrator.models import Reference, References
from wikibaseintegrator.wbi_enums import (
    ActionIfExists,
    WikibaseRank,
    WikibaseSnakType,
)

from yizkr.claims import TimeValue
from yizkr.core import sparql
from yizkr.core.write_recovery import write_existing

# How long `synchronize` waits for the query service before it fails.
HANG_GUARD = 240

Value = Callable[[str], object]


def time_value(
    year: int,
    month: int | None = None,
    day: int | None = None,
    precision: str | None = None,
) -> Value:
    """A time as the writer stores it (`TimeValue.to_wikibase`)."""
    stored = TimeValue(
        year=year, month=month, day=day, precision=precision
    ).to_wikibase()
    return lambda pid: Time(prop_nr=pid, **stored)


def text(value: str, language: str = "en") -> Value:
    return lambda pid: MonolingualText(
        prop_nr=pid, text=value, language=language
    )


def string(value: str) -> Value:
    return lambda pid: String(prop_nr=pid, value=value)


def unknown_time(pid: str):
    """A time its source states without giving it: an unknown value."""
    return Time(prop_nr=pid, snaktype=WikibaseSnakType.UNKNOWN_VALUE)


def coordinate(latitude: float, longitude: float) -> Value:
    return lambda pid: GlobeCoordinate(
        prop_nr=pid, latitude=latitude, longitude=longitude, precision=0.0001
    )


def between(start: Value, end: Value) -> tuple:
    """The earliest and latest dates a source gives a value."""
    return (("earliest_date", start), ("latest_date", end))


class Fixture:
    """The fixture's items: each made under its label, then given all its
    statements in one further write, so no statement is ever held without
    its references. `name` says in each edit summary which fixture wrote
    it."""

    def __init__(self, client, name: str) -> None:
        self.client = client
        self.summary = f"fictional {name} fixture"
        self.properties, self.items = client.vocabulary()
        self.qids: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self._entities: dict[str, object] = {}
        self._statements: defaultdict[str, list] = defaultdict(list)
        self.sources: list[str] = []
        self.run_keys: list[str] = []
        self._runs: dict[str, str] = {}
        self._whole: dict[str, str] = {}

    def make(
        self,
        key: str,
        label: str,
        scene: str,
        aliases=(),
        description: str | None = None,
    ) -> None:
        """An item under `label`, described by its scene unless
        `description` is given: Wikibase holds a label and a description
        on one item alone, so two items of one label in one scene need
        descriptions of their own."""
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set(
            "en", description or f"fictional {scene} fixture"
        )
        if aliases:
            item.aliases.set("en", list(aliases))
        self._entities[key] = item.write(summary=self.summary)
        self.qids[key] = self._entities[key].id
        self.labels[key] = label

    def source(self, key: str, title: str, kind: str, whole=None) -> None:
        """An artifact whose own kind cites itself, or a part of `whole`
        (a page fragment, an embedded photograph) whose kind and `part of`
        cite its whole, as the registrar writes them."""
        self.make(key, title, "source")
        self.sources.append(key)
        self.state(key, "instance_of", self.vocab(kind), whole or key)
        if whole:
            self._whole[key] = whole
            self.state(key, "part_of", self.item(whole), whole)

    def entity(
        self,
        key,
        label,
        scene,
        kind,
        *sources,
        aliases=(),
        kind_rank=None,
        description=None,
    ) -> None:
        """An item and the kind a machine reading of `sources` gives it."""
        self.make(key, label, scene, aliases, description)
        self.state(
            key,
            "instance_of",
            self.vocab(kind),
            *sources,
            run=True,
            rank=kind_rank,
        )

    def item(self, key: str) -> Value:
        qid = self.qids[key]
        return lambda pid: Item(prop_nr=pid, value=qid)

    def vocab(self, key: str) -> Value:
        qid = self.items[key]
        return lambda pid: Item(prop_nr=pid, value=qid)

    def block(
        self,
        source: str,
        page: str | None = None,
        excerpt: str | None = None,
        language: str = "en",
        printed: str | None = None,
    ) -> Reference:
        """A `stated in` block citing `source`, with the page, printed
        page and excerpt given."""
        properties = self.properties
        block = Reference()
        block.add(
            Item(prop_nr=properties["stated_in"], value=self.qids[source])
        )
        for key, locator in (
            ("page_number", page),
            ("printed_page_number", printed),
        ):
            if locator:
                block.add(String(prop_nr=properties[key], value=locator))
        if excerpt:
            block.add(
                MonolingualText(
                    prop_nr=properties["transcription_excerpt"],
                    text=excerpt,
                    language=language,
                )
            )
        return block

    def state(
        self,
        subject: str,
        prop: str,
        value: Value,
        *sources: str,
        run: bool = False,
        run_only: str | None = None,
        page: str | None = None,
        printed: str | None = None,
        excerpt: str | None = None,
        language: str = "en",
        qualifiers: tuple = (),
        rank: WikibaseRank | None = None,
    ):
        """One statement of `subject`, stated in each of `sources`, each
        block with `page`, `printed` and `excerpt`, and, with `run`, each
        followed by the block of its source's extraction run. `run_only`
        names the source whose run alone the statement cites. Returns the
        statement, which `write` sends."""
        references = References()
        for source in sources:
            references.add(
                self.block(source, page, excerpt, language, printed)
            )
            if run:
                references.add(self._run_block(source))
        if run_only:
            references.add(self._run_block(run_only))
        claim = value(self.properties[prop])
        claim.references = references
        for key, qualifier in qualifiers:
            claim.qualifiers.add(qualifier(self.properties[key]))
        if rank is not None:
            claim.rank = rank
        self._statements[subject].append(claim)
        return claim

    def reading(self, subject, prop, value, *sources, **options):
        """A machine reading of page 1 of each of `sources`."""
        return self.state(
            subject, prop, value, *sources, run=True, page="1", **options
        )

    def curated(self, subject, prop, value, *sources, **options):
        """A curator's statement citing page 1 of each of `sources`."""
        return self.state(subject, prop, value, *sources, page="1", **options)

    def cite(self, claim, source: str, **locators) -> None:
        """A further `stated in` block on `claim`, with the locators
        `block` takes."""
        claim.references.add(self.block(source, **locators))

    def _run_block(self, source: str) -> Reference:
        whole = self._whole.get(source, source)
        if whole not in self._runs:
            key = f"run of {whole}"
            self.make(key, f"Extraction run over {self.labels[whole]}", "run")
            self.run_keys.append(key)
            self.state(key, "instance_of", self.vocab("extraction_run"), key)
            self._runs[whole] = self.qids[key]
        block = Reference()
        block.add(
            Item(
                prop_nr=self.properties["inferred_from_extraction_run"],
                value=self._runs[whole],
            )
        )
        return block

    def write(self) -> None:
        for key, statements in self._statements.items():
            entity = self._entities[key]
            for statement in statements:
                entity.claims.add(
                    statement, action_if_exists=ActionIfExists.FORCE_APPEND
                )
            self._entities[key] = write_existing(entity, summary=self.summary)
        self._statements.clear()

    def key_of(self, iri_or_qid: str) -> str:
        """The fixture key of an item, given by its IRI or QID."""
        qid = iri_or_qid.rsplit("/", 1)[-1]
        found = [key for key, value in self.qids.items() if value == qid]
        return found[0] if found else qid

    def iri(self, key: str) -> str:
        return f"{self.client.config.concept_uri}/entity/{self.qids[key]}"


def synchronize(client) -> None:
    """Wait until WDQS holds every entity's current API revision, the
    query vocabulary's included."""
    wanted = client.entity_revisions(client.list_entity_ids())
    entity = client.config.concept_uri + "/entity/"
    query = (
        "SELECT ?entity ?version WHERE "
        "{ ?entity <http://schema.org/version> ?version . }"
    )
    deadline = time.monotonic() + HANG_GUARD
    while True:
        seen = {
            row["entity"]["value"].removeprefix(entity): int(
                row["version"]["value"]
            )
            for row in sparql.select(client.config.sparql_url, query)
        }
        stale = [
            qid
            for qid, revision in wanted.items()
            if seen.get(qid, -1) < revision
        ]
        if not stale:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{len(stale)} entities have not reached their API revision "
                f"in WDQS after {HANG_GUARD}s; check the fictional "
                "wdqs-updater logs"
            )
        time.sleep(2)
