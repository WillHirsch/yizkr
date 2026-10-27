"""Source parts and placements recorded before choosing a reading route.

The inventory names original bytes and structural occurrences, independently
of eligibility and reading results. It is not a source-owner registry or an
authorization to send a part to a model. Legacy readers may project a smaller
view while their callers migrate to these distinctions.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from yizkr.claims import ArtifactRef
from yizkr.pipeline.stages import canonical_json, staging_path

CONTAINER_INVENTORY = "container-inventory.json"
Identifier = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or value == "."
        or value == CONTAINER_INVENTORY
    ):
        raise ValueError("a product must name a safe relative file")
    return value


class StructuralLocator(_Record):
    """A native part or occurrence, before eligibility and reading filters.

    ``position`` is the physical page/slide order, not a printed label or
    an ordinal among the images selected for reading. ``path`` preserves a
    native object/shape occurrence path when no native identifier suffices.
    """

    kind: Identifier
    package_part: str | None = None
    position: int | None = Field(default=None, ge=1)
    path: str | None = None
    native_id: str | None = None
    relationship_id: str | None = None


def structural_id(source: ArtifactRef, locator: StructuralLocator) -> str:
    """Identity of an occurrence, independent of its current reading state."""
    value = {
        "source": source.model_dump(mode="json"),
        "locator": locator.model_dump(mode="json", exclude_none=True),
    }
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Eligibility(_Record):
    state: Literal["allowed", "excluded", "undetermined"] = "undetermined"
    reason: str | None = None


class ReadingOutcome(_Record):
    """Current reading outcome, separate from permission to read the part.

    A successful empty reading is explicit. Native text may be kept inline
    in its source part; a separate reading product names both path and digest.
    The product may later be absent: consumers validate it before use rather
    than treating this observation as proof that its bytes are still present.
    """

    state: Literal["unattempted", "unsupported", "failed", "read"] = (
        "unattempted"
    )
    method: str | None = None
    reason: str | None = None
    empty: bool | None = None
    product: str | None = None
    product_sha256: Sha256 | None = None

    @field_validator("product")
    @classmethod
    def product_path(cls, value: str | None) -> str | None:
        return _relative_file(value) if value is not None else None

    @model_validator(mode="after")
    def complete_outcome(self) -> ReadingOutcome:
        if self.state == "read":
            if self.empty is None:
                raise ValueError("a successful reading declares whether empty")
        elif self.empty is not None or self.product is not None:
            raise ValueError("only a successful reading has output content")
        if (self.product is None) != (self.product_sha256 is None):
            raise ValueError("a reading product needs both path and digest")
        return self


class SourceRepresentation(_Record):
    """Bytes prepared from the containing source part for a named process.

    The representation's digest does not replace that part's original-byte
    identity. An excluded or unavailable representation may name its digest
    without publishing a file.
    """

    byte_object: ArtifactRef
    file: str | None = None
    media_type: str
    method: Identifier
    eligibility: Eligibility = Field(default_factory=Eligibility)

    @field_validator("file")
    @classmethod
    def representation_path(cls, value: str | None) -> str | None:
        return _relative_file(value) if value is not None else None


class SourcePart(_Record):
    id: Identifier
    kind: Identifier
    locator: StructuralLocator
    byte_object: ArtifactRef | None = None
    file: str | None = None
    media_type: str | None = None
    text: str | None = None
    # Ordered native layout records, with units/attributes preserved by the
    # format adapter rather than guessed pixel geometry.
    layout: list[dict[str, JsonValue]] = Field(default_factory=list)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    reading: ReadingOutcome = Field(default_factory=ReadingOutcome)
    representations: list[SourceRepresentation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def original_path(cls, value: str | None) -> str | None:
        return _relative_file(value) if value is not None else None

    @model_validator(mode="after")
    def named_bytes(self) -> SourcePart:
        if self.file is not None and self.byte_object is None:
            raise ValueError("an original-byte file needs a byte identity")
        return self


def _byte_files(parts: list[SourcePart]) -> Iterator[tuple[str, ArtifactRef]]:
    for part in parts:
        for product in (part, *part.representations):
            if product.file is not None:
                yield product.file, product.byte_object


class Placement(_Record):
    id: Identifier
    parent: Identifier
    child: Identifier
    locator: StructuralLocator
    order: int = Field(ge=1)
    layout: list[dict[str, JsonValue]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ContainerInventory(_Record):
    format: Literal[1] = 1
    source: ArtifactRef
    container: Identifier
    parts: list[SourcePart] = Field(default_factory=list)
    placements: list[Placement] = Field(default_factory=list)
    complete: bool = True
    limitations: list[str] = Field(default_factory=list)

    @field_validator("format", mode="before")
    @classmethod
    def supported_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported container inventory version")
        return value

    @model_validator(mode="after")
    def intact_references(self) -> ContainerInventory:
        parts = {part.id for part in self.parts}
        if len(parts) != len(self.parts):
            raise ValueError("source part identities must be unique")
        placements = {placement.id for placement in self.placements}
        if len(placements) != len(self.placements):
            raise ValueError("placement identities must be unique")
        if parts & placements:
            raise ValueError("a placement and a source part need distinct ids")
        for placement in self.placements:
            if placement.parent not in parts or placement.child not in parts:
                raise ValueError("a placement must reference recorded parts")
            if placement.parent == placement.child:
                raise ValueError("a part cannot be placed inside itself")
        bindings: dict[str, str] = {}
        for file, byte_object in _byte_files(self.parts):
            digest = byte_object.sha1
            previous = bindings.setdefault(file, digest)
            if previous != digest:
                raise ValueError("one file cannot name different bytes")
        return self


def _path(directory: Path, relative: str) -> Path:
    """Confine a product to the directory without following linked parents."""
    path = directory / relative
    if directory.is_symlink():
        raise ValueError("an inventory directory must not be a symlink")
    current = directory
    for component in PurePosixPath(relative).parts[:-1]:
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError("an inventory file has an unsafe parent")
    return path


def _read_regular(directory: Path, relative: str) -> bytes | None:
    try:
        path = _path(directory, relative)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            return handle.read()
    except (OSError, ValueError):
        return None


def _keep(directory: Path, relative: str, data: bytes) -> None:
    if _read_regular(directory, relative) == data:
        return
    path = _path(directory, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(path)
    try:
        with open(staging, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def keep_inventory(
    inventory: ContainerInventory,
    data: Mapping[str, bytes | Path],
    directory: Path,
) -> None:
    """Verify named bytes, publish them, then atomically publish the record.

    ``data`` is keyed by byte-object SHA-1. Missing entries reuse verified
    files already present, so an outcome update can supply an empty mapping.
    Identical files are untouched; a linked final filename is replaced rather
    than written through. Publication is per-file atomic, with metadata last.
    """
    inventory = ContainerInventory.model_validate(inventory.model_dump())
    payloads: dict[str, bytes] = {}
    for file, byte_object in _byte_files(inventory.parts):
        _path(directory, file)
        digest = byte_object.sha1
        supplied = data.get(digest)
        payload = (
            supplied.read_bytes()
            if isinstance(supplied, Path)
            else supplied
            if supplied is not None
            else _read_regular(directory, file)
        )
        if payload is None or hashlib.sha1(payload).hexdigest() != digest:
            raise ValueError(f"inventory bytes are missing or damaged: {file}")
        payloads[file] = payload
    _path(directory, CONTAINER_INVENTORY)
    for relative, payload in payloads.items():
        _keep(directory, relative, payload)
    _keep(
        directory,
        CONTAINER_INVENTORY,
        inventory.model_dump_json(indent=2).encode("utf-8"),
    )


def read_inventory(directory: Path) -> ContainerInventory | None:
    """Load a supported inventory with intact references and named bytes."""
    saved = _read_regular(directory, CONTAINER_INVENTORY)
    if saved is None:
        return None
    try:
        contents = json.loads(saved)
        if not isinstance(contents, dict) or "format" not in contents:
            return None
        inventory = ContainerInventory.model_validate(contents)
    except (ValueError, RecursionError):
        return None
    for file, byte_object in _byte_files(inventory.parts):
        payload = _read_regular(directory, file)
        if (
            payload is None
            or hashlib.sha1(payload).hexdigest() != byte_object.sha1
        ):
            return None
    return inventory
