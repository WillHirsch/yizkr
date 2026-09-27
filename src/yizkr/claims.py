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
  `sourcing_circumstances` qualifiers. Nothing invites the extractor to
  round or guess.

Vocabulary: `property` fields and vocabulary references hold *manifest
keys* (e.g. ``spouse``, ``circa``) from model/manifest.yaml — stable,
instance-independent names. `validate_against_manifest()` checks that
keys exist and that each value's type matches the property's datatype.
Local P/Q ids never appear in pipeline JSON (they differ per instance);
only the writer resolves keys to ids, at write time.

Source artifacts are content-addressed (ADR-0010): a claim's
`source.artifact` is an ``artifact`` reference holding the SHA-1 of the
artifact's media bytes, which is the same before registration, after an
instance reset and on any instance. The writer resolves it to the
artifact item carrying that `media SHA-1`, at write time. A ``qid``
reference to an artifact item is accepted there too, and ties the claim
to that instance.

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
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from yizkr.model.schema import DEFAULT_MANIFEST, load_manifest

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


class TimeValue(StrictModel):
    """A date as the source states it — never rounder, never narrower.

    `precision` defaults to the finest field given. Fields finer than
    the declared precision are rejected: stating "month precision" while
    supplying a day means the claim narrows or pads what the source
    says; both are forbidden (docs/MODELING.md, "Uncertainty and
    time"). For decades use the first year (1920s -> year=1920,
    precision=decade); same for centuries (1800).
    Ranges ("between 1930 and 1935") are NOT a precision problem: use
    earliest_date/latest_date qualifiers on the claim.
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
            # (1920s -> year=1920), so a bare year is expected; month/day
            # would claim more than the source says.
            if finest != "year":
                raise ValueError(
                    f"precision {self.precision!r} cannot carry a {finest} "
                    f"field (never narrow or pad what a source says)"
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
        Wikidata)."""
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


# The artifact a claim cites. An ``artifact`` reference is the
# content-addressed form. A ``qid`` reference names an artifact item on
# the target instance directly, so a claim that carries one is valid on
# that instance only.
SourceArtifactRef = Annotated[
    ArtifactRef | QidRef, Field(discriminator="kind")
]


class SourceReference(StrictModel):
    """Where the artifact asserts the claim (ADR-0003).

    `artifact` names the source artifact by the SHA-1 of its media, and
    the writer resolves it to the artifact item at write time. The
    locators narrow the pointer inside the artifact: a page number, an
    image region. The excerpt supports one-click human verification.
    """

    artifact: SourceArtifactRef
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
    # False = recorded for audit but NOT extracted: entities appearing
    # only in non-assertive context (logistics, boilerplate) or whose
    # claims the archive does not want (contemporary living people in
    # incidental roles). Mirrors SegmentationUnit.extract.
    extract: bool = True
    # open-schema escape hatch (ADR-0013): free-text note of what an
    # entity actually is when kind is `unclassified_entity` (no modelled
    # kind fits). Written as a `type_hint` string statement; drives the
    # next vocabulary round instead of the entity being mistyped.
    type_hint: str | None = None
    qid: str | None = Field(default=None, pattern=r"^Q[1-9]\d*$")


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
) -> list[str]:
    """Check every key and datatype in the batch against the manifest.

    Returns a list of human-readable problems (empty means valid), so a
    pipeline step can report them all at once instead of failing one at
    a time.
    """
    properties, items = (
        vocabulary
        if vocabulary is not None
        else load_vocabulary(manifest_path)
    )
    errors: list[str] = []

    entity_ids: set[str] = {entity.id for entity in batch.entities}
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

    def check_property_value(prop: str, value, where: str) -> None:
        datatype = properties.get(prop)
        if datatype is None:
            errors.append(f"{where}: unknown property key {prop!r}")
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

    for index, claim in enumerate(batch.claims):
        where = f"claims[{index}]"
        check_ref(claim.subject, f"{where}.subject")
        check_property_value(claim.property, claim.value, where)
        for q_index, qualifier in enumerate(claim.qualifiers):
            check_property_value(
                qualifier.property,
                qualifier.value,
                f"{where}.qualifiers[{q_index}]",
            )

    return errors


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
