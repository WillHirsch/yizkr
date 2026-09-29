#!/usr/bin/env python3
"""Claim JSON schema: the contract between extraction and writing.

Every step of the ingestion pipeline (ADR-0003) exchanges claims in
this format: the LLM extractor emits it, entity resolution rewrites
references in it, the reviewer export renders it to CSV, and the writer
turns it into Wikibase statements.

The schema makes the archive's core conventions structural:

* No statement without a reference (ADR-0003): `Claim.source` is a
  required field. A claim without a source artifact cannot be
  represented, parsed, or written.
* Dual provenance (ADR-0003): every batch carries
  `extraction_run` metadata; the writer creates an extraction-run item
  and adds the second reference to every statement it writes.
* Ranks default to normal (ADR-0002): there is deliberately NO rank
  field. Machine writes are always normal rank; preferring/deprecating
  is a curator action on the wiki, never part of the pipeline contract.
* Uncertainty is structural (docs/MODELING.md): time values carry explicit
  precision (day/month/year/decade/century) and a calendar; ranges and
  approximation are expressed with `earliest_date`/`latest_date`/
  `sourcing_circumstances` qualifiers, and a range for the start or end
  time qualifier of a period (a marriage, a residence) with that end's own
  bounds, `latest_start_date` and `earliest_end_date` among them. Nothing
  invites the extractor to round or guess.

Vocabulary: `property` fields and vocabulary references hold *manifest
keys* (e.g. ``spouse``, ``circa``) from model/manifest.yaml — stable,
instance-independent names. `validate_against_manifest()` checks that
keys exist and that each value's type matches the property's datatype.
Local P/Q ids never appear in pipeline JSON (they differ per instance);
only the writer resolves keys to ids, at write time.

Source artifacts are content-addressed (ADR-0010): a claim's
`source.artifact` is an ``artifact`` reference holding the SHA-1 of the
artifact's media bytes, which is the same before registration, after an
instance reset and on any instance. It is the only form a source takes:
no claim names its artifact by QID. The writer resolves it to the
artifact item carrying that `media SHA-1`, at write time.

Entity references (subjects and item values) come in three kinds:

* ``qid``   — an entity that already exists in the instance (e.g. an
  item a reviewer matched a document's entity to).
* ``vocab`` — a manifest item by key (e.g. ``circa``, ``yes``).
* ``local`` — a batch-scoped placeholder (``person-1``) for an entity
  the extractor proposes; entity resolution replaces it with a ``qid``
  reference or leaves it for item creation during the write step.

CLI:
    python3 claims.py --json-schema  # print JSON Schema (for LLM prompts)
    python3 claims.py --validate batch.json  # parse + manifest-check a file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from yizkr.model.schema import (
    DEFAULT_MANIFEST,
    Acceptance,
    load_acceptance,
    load_manifest,
)

SCHEMA_VERSION = 1

# Manifest keys (properties and items) use snake_case identifiers.
ManifestKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class StrictModel(BaseModel):
    """All schema models reject unknown fields: typos must fail loudly."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Entity references
# ---------------------------------------------------------------------------


class QidRef(StrictModel):
    """An entity that already exists in the target instance."""

    kind: Literal["qid"] = "qid"
    qid: str = Field(pattern=r"^Q[1-9]\d*$")


class VocabRef(StrictModel):
    """A vocabulary item from model/manifest.yaml, by manifest key."""

    kind: Literal["vocab"] = "vocab"
    key: ManifestKey


class LocalRef(StrictModel):
    """A batch-scoped placeholder for an entity proposed by extraction.

    `local_id` must be stable within one batch so several claims can
    talk about the same not-yet-resolved entity. `label`, `description`
    and `instance_of` (a manifest item key) carry what the source says,
    to power entity resolution and item creation.
    """

    kind: Literal["local"] = "local"
    local_id: str = Field(min_length=1)
    label: str | None = None
    description: str | None = None
    instance_of: ManifestKey | None = None


EntityRef = Annotated[
    QidRef | VocabRef | LocalRef, Field(discriminator="kind")
]


# ---------------------------------------------------------------------------
# Values (discriminated on `type`; one variant per Wikibase datatype in use)
# ---------------------------------------------------------------------------


class ItemValue(StrictModel):
    type: Literal["item"] = "item"
    value: EntityRef


class StringValue(StrictModel):
    type: Literal["string"] = "string"
    value: str = Field(min_length=1)


class ExternalIdValue(StrictModel):
    type: Literal["external-id"] = "external-id"
    value: str = Field(min_length=1)


class MonolingualText(StrictModel):
    text: str = Field(min_length=1)
    language: str = Field(pattern=r"^[a-z]{2,3}(-[a-z0-9-]+)?$")


class MonolingualValue(StrictModel):
    type: Literal["monolingual"] = "monolingual"
    value: MonolingualText


PRECISIONS = ("century", "decade", "year", "month", "day")
WIKIBASE_PRECISION = {
    "century": 7,
    "decade": 8,
    "year": 9,
    "month": 10,
    "day": 11,
}
CALENDAR_URI = {
    "gregorian": "http://www.wikidata.org/entity/Q1985727",
    "julian": "http://www.wikidata.org/entity/Q1985786",
}


def century_of(year: int) -> int:
    """The century a year falls in, counted as Wikibase and Wikidata count
    centuries (docs/MODELING.md, "Uncertainty and time"): the nth century
    runs from year 100(n - 1) + 1 to year 100n, so 1801 to 1900 is the 19th
    century and 1800 is the last year of the 18th. There is no year zero: a
    negative year counts back from 1 BCE, and its century is negative, so
    -1800 to -1701 is the 18th century BCE, -18."""
    count = (abs(year) + 99) // 100
    return count if year > 0 else -count


def century_years(century: int) -> tuple[int, int]:
    """The first and last year of a century as `century_of` counts it: 1801
    and 1900 for the 19th, and -1800 and -1701 for the 18th BCE (-18)."""
    if century > 0:
        return 100 * century - 99, 100 * century
    return 100 * century, 100 * century + 99


class TimeValue(StrictModel):
    """A date as the source states it — never rounder, never narrower.

    `precision` defaults to the finest field given. Fields finer than
    the declared precision are rejected: stating "month precision" while
    supplying a day means the claim narrows or pads what the source
    says; both are forbidden (docs/MODELING.md, "Uncertainty and
    time"). For decades use the first year (1890s -> year=1890,
    precision=decade). Centuries are counted as Wikibase counts them:
    the 19th century is 1801-1900, so use its first year (19th century
    -> year=1801, precision=century); 1800 is in the 18th. The 1800s,
    meaning 1800-1899, are no such century: give them as a range.
    Ranges ("between 1938 and 1939") are NOT a precision problem: use
    earliest_date/latest_date qualifiers on the claim. A start_time or
    end_time qualifier dating a period (a marriage, a residence) takes
    that end's own bounds instead: earliest_date/latest_start_date for a
    start, earliest_end_date/latest_date for an end. An entity's own
    start_time or end_time claim (a firm's founding) is a date value that
    earliest_date/latest_date bound.
    """

    type: Literal["time"] = "time"
    year: int
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    precision: Literal["century", "decade", "year", "month", "day"] | None = (
        None
    )
    calendar: Literal["gregorian", "julian"] = "gregorian"

    @field_validator("year")
    @classmethod
    def check_year(cls, value: int) -> int:
        if value == 0:
            raise ValueError("there is no year zero")
        return value

    @model_validator(mode="after")
    def check_component_consistency(self) -> TimeValue:
        if self.day is not None and self.month is None:
            raise ValueError("a day requires a month")
        finest = "day" if self.day else "month" if self.month else "year"
        if self.precision is None:
            self.precision = finest
        elif self.precision in ("century", "decade"):
            # Coarse precisions are expressed through their first year
            # (1890s -> year=1890), so a bare year is expected; month/day
            # would claim more than the source says.
            if finest != "year":
                raise ValueError(
                    f"precision {self.precision!r} cannot carry a {finest} "
                    f"field (never narrow or pad what a source says)"
                )
            # A century is given by its first year alone, as Wikibase
            # counts centuries: every source that names it states one
            # value, and a round hundred, which starts "the 1800s" but ends
            # the 18th century in that count, never stands for the 19th.
            if (
                self.precision == "century"
                and century_years(century_of(self.year))[0] != self.year
            ):
                raise ValueError(
                    f"precision 'century' takes the first year of the "
                    f"century, as Wikibase counts centuries: the 19th "
                    f"century is 1801-1900, so year=1801 (the 18th century "
                    f"BCE is -1800 to -1701, so year=-1800); year "
                    f"{self.year} is not a century's first year"
                )
        elif PRECISIONS.index(finest) > PRECISIONS.index(self.precision):
            raise ValueError(
                f"fields are {finest}-precise but precision says "
                f"{self.precision!r}: drop the finer fields or fix the "
                f"precision (never narrow or pad what a source says)"
            )
        elif PRECISIONS.index(finest) < PRECISIONS.index(self.precision):
            raise ValueError(
                f"precision {self.precision!r} requires the "
                f"{self.precision} field"
            )
        return self

    def to_wikibase(self) -> dict:
        """Wikibase time datavalue (unspecified parts zeroed, as on
        Wikidata). The year is written as given, so a century's first
        year is stored, which Wikibase shows as that century: 1801 at
        century precision is "19. century"."""
        sign = "-" if self.year < 0 else "+"
        return {
            "time": f"{sign}{abs(self.year):04d}-{self.month or 0:02d}-"
            f"{self.day or 0:02d}T00:00:00Z",
            "precision": WIKIBASE_PRECISION[self.precision],
            "calendarmodel": CALENDAR_URI[self.calendar],
        }


class CoordinateValue(StrictModel):
    type: Literal["coordinate"] = "coordinate"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    precision: float | None = Field(default=None, gt=0)


class QuantityValue(StrictModel):
    """A count or measure exactly as the source states it.

    W1 quantities are unitless (ages, child counts); a source that
    gives a unit or a range keeps it verbatim in string/qualifier form
    instead of a lossy conversion.
    """

    type: Literal["quantity"] = "quantity"
    amount: int | float

    def to_wikibase(self) -> dict:
        """Wikibase quantity datavalue ('1' is the unitless unit)."""
        return {"amount": f"{self.amount:+g}", "unit": "1"}


Value = Annotated[
    ItemValue
    | StringValue
    | ExternalIdValue
    | MonolingualValue
    | TimeValue
    | CoordinateValue
    | QuantityValue,
    Field(discriminator="type"),
]

# Which value type satisfies each manifest datatype.
DATATYPE_TO_VALUE_TYPE = {
    "wikibase-item": "item",
    "string": "string",
    "external-id": "external-id",
    "monolingualtext": "monolingual",
    "time": "time",
    "globe-coordinate": "coordinate",
    "quantity": "quantity",
}


# ---------------------------------------------------------------------------
# Source reference and locators (ADR-0003)
# ---------------------------------------------------------------------------


class RegionPercent(StrictModel):
    """Percentage bounding box within an image, from the top-left corner.

    Serialized for Wikibase as the P2677-style string "x%,y%,w%,h%".
    """

    x: float = Field(ge=0, lt=100)
    y: float = Field(ge=0, lt=100)
    width: float = Field(gt=0, le=100)
    height: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def check_within_bounds(self) -> RegionPercent:
        if self.x + self.width > 100:
            raise ValueError(
                "region extends past the right edge (x + width > 100)"
            )
        if self.y + self.height > 100:
            raise ValueError(
                "region extends past the bottom edge (y + height > 100)"
            )
        return self

    def as_wikibase_string(self) -> str:
        return f"{self.x:g}%,{self.y:g}%,{self.width:g}%,{self.height:g}%"


class ArtifactRef(StrictModel):
    """An artifact named by the SHA-1 of its media bytes (ADR-0010).

    The key is the content itself, so it does not depend on registration
    or on any instance. The writer resolves it to the artifact item that
    carries this `media SHA-1`, at write time.
    """

    kind: Literal["artifact"] = "artifact"
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("sha1", mode="before")
    @classmethod
    def lowercase_hex(cls, value):
        # the registrar stores lowercase hex; the same digest in capitals
        # names the same bytes
        return value.lower() if isinstance(value, str) else value

    @classmethod
    def from_bytes(cls, data: bytes) -> ArtifactRef:
        return cls(sha1=hashlib.sha1(data).hexdigest())

    @classmethod
    def from_path(cls, path: Path) -> ArtifactRef:
        """The reference for a media file, hashed as the registrar
        hashes it: the file's bytes, independent of its name."""
        return cls.from_bytes(Path(path).read_bytes())

    def __str__(self) -> str:
        return f"sha1:{self.sha1}"


class SourceReference(StrictModel):
    """Where the artifact asserts the claim (ADR-0003).

    `artifact` names the source artifact by the SHA-1 of its media, and
    the writer resolves it to the artifact item at write time. The
    locators narrow the pointer inside the artifact: a page number, an
    image region. The excerpt supports one-click human verification.
    """

    artifact: ArtifactRef
    page_number: str | None = None
    region: RegionPercent | None = None
    transcription_excerpt: MonolingualText | None = None


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


class Qualifier(StrictModel):
    property: ManifestKey
    value: Value


class Claim(StrictModel):
    """One source-attributed assertion.

    There is intentionally no `rank` field: pipeline writes are always
    normal rank (ADR-0002). `confidence` is extractor metadata for review
    ordering; it is never written to the graph.

    `curated` marks a claim (re)stated by the human reviewer — a value
    correction or a curation flag from the review CSV. Curated claims
    keep their `stated in -> artifact` reference but carry no
    `inferred from extraction run` reference: process provenance is for
    machine writes (ADR-0003), and this statement's process was a human.
    """

    subject: EntityRef
    property: ManifestKey
    value: Value
    qualifiers: list[Qualifier] = []
    source: SourceReference
    confidence: float | None = Field(default=None, ge=0, le=1)
    curated: bool = False
    # That a curator approved the claim on its own row of a review sheet.
    # `apply_review` sets it on a `depicts` claim, to a digest of the
    # claim's review-sheet id and of the entity-manifest records its local
    # refs resolve through (yizkr.pipeline.review.approval_digest). The
    # writer writes a depicts claim only when it holds that digest for the
    # claim and those records as they now read (ADR-0005). It is review
    # state, never extraction output: the JSON Schema that extraction
    # prompts carry leaves it out, and it is documented in this comment
    # because the class docstring is that schema's description. A claim
    # without one serialises exactly as it would if the field did not
    # exist, so its content hashes are those of any other claim.
    row_approval: SkipJsonSchema[str | None] = Field(
        default=None, pattern=r"^[0-9a-f]{12}$"
    )

    @model_serializer(mode="wrap")
    def _omit_absent_row_approval(
        self, handler: SerializerFunctionWrapHandler
    ):
        data = handler(self)
        if self.row_approval is None and isinstance(data, dict):
            data.pop("row_approval", None)
        return data


class ExtractionRunMetadata(StrictModel):
    """Process provenance for the batch (ADR-0003).

    The writer mints one extraction-run item per batch from this and
    references every written statement to it.
    """

    model_identifier: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    software_commit: str | None = Field(default=None, min_length=4)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def check_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return value


# ---------------------------------------------------------------------------
# Mention records (ADR-0035 §2)
# ---------------------------------------------------------------------------
#
# What an entity's names are made of, read from its label and aliases as
# the document writes them. A record is extraction's reading, not
# something the source asserts, so no field of it is ever written to the
# wiki (ADR-0035 §2.6); it is input to identity. Every field is optional,
# and each list holds forms as written.


class GivenName(StrictModel):
    """A given name as written, with the full form a diminutive or an
    abbreviation stands for where the document supports one. An initial
    has no standard form."""

    written: str = Field(min_length=1)
    standard: str | None = Field(default=None, min_length=1)
    initial: bool = False


class Surname(StrictModel):
    """A surname as written, its particles kept in it. It is `birth` or
    `married` only where the source marks it so (ADR-0011)."""

    written: str = Field(min_length=1)
    type: Literal["birth", "married", "other"] = "other"


class PersonStructure(StrictModel):
    """A person's mention record (ADR-0035 §2.1). Only a `name` status
    keys: the other statuses say the label names no one."""

    kind: Literal["human"] = "human"
    status: Literal[
        "name", "role", "kinship", "courtesy", "placeholder", "description"
    ] = "name"
    given: list[GivenName] = []
    surnames: list[Surname] = []
    # names formed from the father's given name: a name part of their
    # own, never a surname
    patronymics: list[str] = []
    nicknames: list[str] = []
    # honorifics, ranks, and generational and status words: never keys
    titles: list[str] = []
    suffixes: list[str] = []
    # name forms whose given name and surname the source does not tell
    # apart
    unsplit: list[str] = []


class PlaceName(StrictModel):
    """One of a place's own names as written, with its toponym and its
    generic head where they can be told apart ("Lager Klurnau": head
    Lager, toponym Klurnau)."""

    written: str = Field(min_length=1)
    toponym: str | None = Field(default=None, min_length=1)
    head: str | None = Field(default=None, min_length=1)


class Address(StrictModel):
    """A street and a house number, as written, in the source's order."""

    written: str = Field(min_length=1)
    street: str | None = Field(default=None, min_length=1)
    number: str | None = Field(default=None, min_length=1)


class PlaceStructure(StrictModel):
    """A place's mention record (ADR-0035 §2.2). Only a `toponym` status
    keys; the containers only decide nesting."""

    kind: Literal["place"] = "place"
    status: Literal["toponym", "deictic", "generic"] = "toponym"
    names: list[PlaceName] = []
    address: Address | None = None
    # the places the source puts it in, in the source's order
    within: list[str] = []


class OrganizationStructure(StrictModel):
    """An organisation's mention record (ADR-0035 §2.3): its proper
    names, the words naming its kind of institution, and its places,
    which it carries in its names or its `location` claims name."""

    kind: Literal["organization"] = "organization"
    names: list[str] = []
    kinds: list[str] = []
    places: list[str] = []


class FamilyStructure(StrictModel):
    """A family's mention record (ADR-0035 §2.4)."""

    kind: Literal["family"] = "family"
    surnames: list[str] = []


MentionStructure = Annotated[
    PersonStructure | PlaceStructure | OrganizationStructure | FamilyStructure,
    Field(discriminator="kind"),
]

# The entity kinds that take a mention record; every other kind has none
# (ADR-0035 §2.5).
STRUCTURED_KINDS = ("human", "place", "organization", "family")


class EntityRecord(StrictModel):
    """One document-scoped entity from the extraction's entity manifest
    (ADR-0007).

    The manifest is produced by extraction's first turn and unifies
    every alias a document uses for an entity under one `id`; claims
    refer to entities by these ids. `qid` is resolution state: the
    reconcile/resolve steps set it when a human approves an equivalence
    to an existing item, and the writer then attaches claims (and the
    document's aliases) to that item instead of minting a duplicate.
    """

    id: str = Field(min_length=1)
    label: str = Field(
        min_length=1
    )  # canonical: fullest form the document gives
    aliases: list[str] = []
    kind: ManifestKey
    summary: str = ""
    mention_lines: list[int] = []
    # extract=false for entities that appear ONLY in non-assertive context:
    # travel or event logistics (schedules, bookings, contact lists), people
    # taking part in present-day travel or events, boilerplate. They are
    # recorded for audit but no claims are extracted about them. Entities
    # the document asserts historical/genealogical facts about get
    # extract=true. On official certificates and registers the
    # administrative apparatus is boilerplate: the registrar, officiant, or
    # witness who certifies the record; the issuing authority (e.g. a civil
    # registry office such as a Standesamt); and the register or
    # certificate the document cites as its own source are all
    # extract=false. They attest the record — they are not its
    # genealogical subject. (A witness or officiant who is ALSO a relative
    # of the subject is the exception: extract=true for the relationship.)
    # The entity-manifest prompt (ENTITY_MANIFEST_PROMPT in
    # pipeline/chunking.py) gives the model this rule in the same words;
    # SegmentationUnit.extract is the line-range fallback's flag for a
    # region.
    extract: bool = True
    # open-schema escape hatch (ADR-0013): free-text note of what an
    # entity actually is when kind is `unclassified_entity` (no modelled
    # kind fits). Written as a `type_hint` string statement; drives the
    # next vocabulary round instead of the entity being mistyped.
    type_hint: str | None = None
    qid: str | None = Field(default=None, pattern=r"^Q[1-9]\d*$")
    # The mention record (ADR-0035 §2): what the entity's names are made
    # of, for identity only and never written. Its variant matches the
    # entity's kind; `structure_problems` checks it against the label and
    # aliases. The extraction prompts do not ask for it, so the JSON
    # Schema they carry leaves it out, and an entity without one
    # serialises as if the field did not exist, so its content hashes are
    # unchanged.
    structure: SkipJsonSchema[MentionStructure | None] = None

    @model_serializer(mode="wrap")
    def _omit_absent_structure(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        if self.structure is None and isinstance(data, dict):
            data.pop("structure", None)
        return data


class ClaimBatch(StrictModel):
    """Everything one extraction run asserts about one or more artifacts."""

    schema_version: Literal[1] = SCHEMA_VERSION
    extraction_run: ExtractionRunMetadata
    claims: list[Claim] = []
    # entity manifest (ADR-0007); empty for a batch built without one,
    # such as a face-proposal batch. When present, every local ref must
    # name one of these ids.
    entities: list[EntityRecord] = []


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def load_vocabulary(
    manifest_path=DEFAULT_MANIFEST,
) -> tuple[dict[str, str], set[str]]:
    """(property key -> datatype, item keys) from the model manifest.

    The narrow view: what validation needs. Callers that render the
    vocabulary into a prompt want the manifest's descriptions too and
    load it whole via `load_manifest`.
    """
    manifest = load_manifest(manifest_path)
    return (
        {key: spec.datatype for key, spec in manifest.properties.items()},
        set(manifest.items),
    )


def validate_against_manifest(
    batch: ClaimBatch,
    manifest_path=DEFAULT_MANIFEST,
    vocabulary: tuple[dict[str, str], set[str]] | None = None,
    acceptance: Acceptance | None = None,
    allow_retired: frozenset[str] = frozenset(),
) -> list[str]:
    """Check every key, datatype and value kind in the batch against the
    manifest.

    Returns a list of human-readable problems (empty means valid), so a
    pipeline step can report them all at once instead of failing one at
    a time. Beyond keys and datatypes:

    * an item value's kind must be one its property accepts (ADR-0031,
      ADR-0035 §3.5). A local ref's kind is its entity's, from the
      batch's entity manifest or else its own `instance_of`; a
      vocabulary ref's is its key. An existing item named by its QID has
      no kind here, so it is not checked;
    * a claim of a retired property is refused (ADR-0036), save one in
      `allow_retired`;
    * a death's principal is never also its informant (ADR-0035 §3.2).
    """
    properties, items = (
        vocabulary
        if vocabulary is not None
        else load_vocabulary(manifest_path)
    )
    acceptance = (
        acceptance
        if acceptance is not None
        else load_acceptance(manifest_path)
    )
    errors: list[str] = []

    entity_ids: set[str] = {entity.id for entity in batch.entities}
    entity_kinds: dict[str, str] = {
        entity.id: entity.kind for entity in batch.entities
    }
    seen_entity_ids: set[str] = set()
    for e_index, entity in enumerate(batch.entities):
        where = f"entities[{e_index}]"
        if entity.id in seen_entity_ids:
            errors.append(f"{where}: duplicate entity id {entity.id!r}")
        seen_entity_ids.add(entity.id)
        if entity.kind not in items:
            errors.append(
                f"{where}: kind names unknown item key {entity.kind!r}"
            )

    def check_ref(ref, where: str) -> None:
        if isinstance(ref, VocabRef) and ref.key not in items:
            errors.append(f"{where}: unknown vocabulary item key {ref.key!r}")
        if isinstance(ref, LocalRef):
            if ref.instance_of is not None and ref.instance_of not in items:
                errors.append(
                    f"{where}: instance_of names unknown item key "
                    f"{ref.instance_of!r}"
                )
            # ADR-0007: with an entity manifest present, an unknown local
            # id is an invented entity — a detectable defect, never a
            # silent duplicate
            if entity_ids and ref.local_id not in entity_ids:
                errors.append(
                    f"{where}: local id {ref.local_id!r} not in the "
                    f"batch's entity manifest"
                )

    def kind_of(ref) -> str | None:
        if isinstance(ref, VocabRef):
            return ref.key
        if isinstance(ref, LocalRef):
            return entity_kinds.get(ref.local_id, ref.instance_of)
        return None

    def check_property_value(
        prop: str, value, where: str, subject_kind: str | None
    ) -> None:
        datatype = properties.get(prop)
        if datatype is None:
            errors.append(f"{where}: unknown property key {prop!r}")
            return
        if prop in acceptance.retired and prop not in allow_retired:
            errors.append(f"{where}: {acceptance.refusal(prop, None)}")
            return
        expected = DATATYPE_TO_VALUE_TYPE.get(datatype)
        if expected is None:
            errors.append(
                f"{where}: property {prop!r} has datatype {datatype!r}, which "
                f"the claim schema does not support yet"
            )
            return
        if value.type != expected:
            errors.append(
                f"{where}: property {prop!r} ({datatype}) requires a "
                f"{expected!r} value, got {value.type!r}"
            )
        if isinstance(value, ItemValue):
            check_ref(value.value, f"{where}.value")
            if prop not in acceptance.retired:
                refusal = acceptance.refusal(
                    prop,
                    kind_of(value.value),
                    subject_kind,
                    vocabulary=isinstance(value.value, VocabRef),
                )
                if refusal is not None:
                    errors.append(f"{where}.value: {refusal}")

    def ref_key(ref) -> str:
        if isinstance(ref, LocalRef):
            return f"local:{ref.local_id}"
        if isinstance(ref, QidRef):
            return f"qid:{ref.qid}"
        return f"vocab:{ref.key}"

    # roles each person holds in each death, across the batch's claims
    death_roles: dict[tuple[str, str], set[str]] = {}
    for index, claim in enumerate(batch.claims):
        where = f"claims[{index}]"
        subject_kind = kind_of(claim.subject)
        check_ref(claim.subject, f"{where}.subject")
        check_property_value(claim.property, claim.value, where, subject_kind)
        for q_index, qualifier in enumerate(claim.qualifiers):
            check_property_value(
                qualifier.property,
                qualifier.value,
                f"{where}.qualifiers[{q_index}]",
                subject_kind,
            )
        if (
            claim.property == "participant"
            and subject_kind == "death"
            and isinstance(claim.value, ItemValue)
        ):
            roles = death_roles.setdefault(
                (ref_key(claim.subject), ref_key(claim.value.value)), set()
            )
            roles.update(
                qualifier.value.value.key
                for qualifier in claim.qualifiers
                if qualifier.property == "object_has_role"
                and isinstance(qualifier.value, ItemValue)
                and isinstance(qualifier.value.value, VocabRef)
            )
            if {"principal", "informant"} <= roles:
                errors.append(
                    f"{where}: a death's principal is never also its "
                    f"informant (ADR-0035 §3.2)"
                )

    return errors


# ---------------------------------------------------------------------------
# Mention record checks (ADR-0035 §5)
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Case and accents folded after Unicode normalisation."""
    decomposed = unicodedata.normalize(
        "NFD", unicodedata.normalize("NFC", text).casefold()
    )
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", _fold(text))


def _occurs(part: str, forms: list[list[str]], open_end: bool = False) -> bool:
    """Whether `part` occurs in one of the forms, folded, as whole words.
    With `open_end`, its last word may end inside a form's word, as a
    family's surname does in "the Löbigs"."""
    words = _words(part)
    if not words:
        return False
    size = len(words)
    for form in forms:
        for start in range(len(form) - size + 1):
            window = form[start : start + size]
            if window[:-1] != words[:-1]:
                continue
            last = window[-1]
            if last == words[-1] or (open_end and last.startswith(words[-1])):
                return True
    return False


def structure_problems(entity: EntityRecord) -> list[str]:
    """Why an entity's mention record cannot stand (ADR-0035 §5), each
    reason first: empty when it can, or when it has none.

    * `kind`: the record's variant is not the entity's kind;
    * `addition`: a part occurs in neither the label nor an alias, folded,
      as whole words. The model may divide and label a name, never add
      to it. A given name's standard form is exempt, as are an
      organisation's places, which its `location` claims may name;
    * `initial`: an initial gives a standard form;
    * `no name part`: a person whose status is `name` has no name part;
    * `no toponym`: a place whose status is `toponym` has no name and no
      address.
    """
    structure = entity.structure
    if structure is None:
        return []
    if structure.kind != entity.kind:
        return [
            f"kind: a {structure.kind!r} record on a {entity.kind!r} entity"
        ]
    forms = [_words(form) for form in (entity.label, *entity.aliases)]
    problems: list[str] = []
    parts: list[str] = []
    open_parts: list[str] = []
    if isinstance(structure, PersonStructure):
        parts += [name.written for name in structure.given]
        parts += [name.written for name in structure.surnames]
        parts += structure.patronymics + structure.nicknames
        parts += structure.titles + structure.suffixes + structure.unsplit
        for name in structure.given:
            if name.initial and name.standard is not None:
                problems.append(
                    f"initial: the initial {name.written!r} gives the "
                    f"standard form {name.standard!r}"
                )
        named = (
            structure.given
            or structure.surnames
            or structure.patronymics
            or structure.nicknames
            or structure.unsplit
        )
        if structure.status == "name" and not named:
            problems.append("no name part: a name status with no name part")
    elif isinstance(structure, PlaceStructure):
        for name in structure.names:
            parts += [name.written]
            parts += [part for part in (name.toponym, name.head) if part]
        if structure.address is not None:
            address = structure.address
            parts += [address.written]
            parts += [
                part for part in (address.street, address.number) if part
            ]
        parts += structure.within
        if structure.status == "toponym" and not (
            structure.names or structure.address
        ):
            problems.append(
                "no toponym: a toponym status with no name and no address"
            )
    elif isinstance(structure, OrganizationStructure):
        parts += structure.names + structure.kinds
    else:
        open_parts += structure.surnames
    for part, open_end in [(p, False) for p in parts] + [
        (p, True) for p in open_parts
    ]:
        if not _occurs(part, forms, open_end=open_end):
            problems.append(
                f"addition: {part!r} occurs in neither the label nor an alias"
            )
    return problems


def set_aside_structure(entity: dict) -> tuple[dict, list[str]]:
    """An entity of an entity manifest, as the model wrote it, with its
    mention record set aside when the record cannot stand (ADR-0035 §5),
    and the reasons.

    The entity keeps its label, aliases and kind: a record that fails is
    dropped, never the entity or the manifest, and the entity is then
    recordless. A record that does not parse gives the reason `schema`;
    one that parses gives those of `structure_problems`. An entity that
    is invalid without its record is returned as it is, for the
    manifest's own validation to report.
    """
    if entity.get("structure") is None:
        return entity, []
    rest = {key: value for key, value in entity.items() if key != "structure"}
    try:
        record = EntityRecord.model_validate(entity)
    except ValidationError as exc:
        try:
            EntityRecord.model_validate(rest)
        except ValidationError:
            return entity, []
        first = exc.errors()[0]
        location = ".".join(str(step) for step in first["loc"])
        return rest, [f"schema: {location}: {first['msg']}"]
    problems = structure_problems(record)
    return (rest, problems) if problems else (entity, [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--json-schema",
        action="store_true",
        help="print the ClaimBatch JSON Schema (for LLM extraction prompts)",
    )
    group.add_argument(
        "--validate",
        type=Path,
        metavar="FILE",
        help="parse FILE as a claim batch and check it against the manifest",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    if args.json_schema:
        print(json.dumps(ClaimBatch.model_json_schema(), indent=2))
        return 0

    try:
        batch = ClaimBatch.model_validate_json(args.validate.read_text())
    except ValidationError as exc:
        print(f"Schema errors in {args.validate}:\n{exc}", file=sys.stderr)
        return 1
    problems = validate_against_manifest(batch, manifest_path=args.manifest)
    for problem in problems:
        print(f"manifest check: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(
        f"{args.validate}: valid ({len(batch.claims)} claims, "
        f"run {batch.extraction_run.model_identifier})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
