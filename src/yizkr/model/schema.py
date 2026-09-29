"""Manifest schema: the declared vocabulary (docs/MODELING.md).

The manifest ships as package data (yizkr/model/manifest.yaml) so the
vocabulary versions with the code; pass an explicit path to work
against an edited copy.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Traversable resource; read with read_manifest_data(), not open().
DEFAULT_MANIFEST = files("yizkr.model") / "manifest.yaml"

# Wikibase datatypes the manifest may use (extend when needed).
Datatype = Literal[
    "wikibase-item",
    "string",
    "time",
    "globe-coordinate",
    "external-id",
    "monolingualtext",
    "url",
    "quantity",
]


def read_manifest_data(source=DEFAULT_MANIFEST) -> str:
    """Text of a manifest, whether a filesystem path or package resource."""
    if isinstance(source, str):
        source = Path(source)
    return source.read_text()


# The forms `accepts` takes besides a list of item keys (ADR-0035 §3.5):
# any kind at all (`depicts`, ADR-0005), the claim subject's own kind
# (`said_to_be_the_same_as`), or a class of the manifest named by its key
# (`instance_of`, `subclass_of`).
AcceptsAnyKind = Literal["any", "subject", "class"]

# The holding class (ADR-0013) that every list naming an entity kind
# accepts too, so an entity no modelled kind fits can still take part.
UNCLASSIFIED = "unclassified_entity"


class ManifestProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    # Wikibase caps entity descriptions at 250 characters; fail at
    # manifest-load time rather than mid-write against the instance.
    description: str = Field(min_length=1, max_length=250)
    datatype: Datatype
    aliases: list[str] = []
    wikidata: str | None = Field(default=None, pattern=r"^P\d+$")
    # The kinds an item value of this property may be (ADR-0031,
    # ADR-0035 §3.5): item keys, each accepted exactly, so a subclass is
    # accepted only where it is listed; or one of AcceptsAnyKind. Every
    # item-valued property declares it, and only those do.
    accepts: list[str] | AcceptsAnyKind | None = None
    # A retired property stays in the instance but takes no new claim:
    # validation refuses every claim of it, and no prompt offers it
    # (ADR-0036's `spouse`). It accepts nothing.
    retired: bool = False
    # A reference property is written only in a statement's references
    # (ADR-0003; ADR-0035 §4's judgement notes): validation refuses it as
    # a claim's or a qualifier's property, and no prompt offers it.
    reference: bool = False
    # Whether the prompts rendered from the manifest offer the property.
    # A retired or reference property is never offered, whatever this
    # says.
    offered: bool = True
    # Whether a claim of the property has its own source document as its
    # subject, named by the document's content key, and no other subject
    # (`issued_by`, ADR-0035 §3.4).
    document_subject: bool = False

    @property
    def in_prompts(self) -> bool:
        """Whether a prompt rendered from the manifest lists it."""
        return self.offered and not self.retired and not self.reference


class ManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    # Wikibase caps entity descriptions at 250 characters; fail at
    # manifest-load time rather than mid-write against the instance.
    description: str = Field(min_length=1, max_length=250)
    aliases: list[str] = []
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    subclass_of: str | None = None
    # Offerable to the extractor as an entity kind (the ADR-0007
    # manifest pass). False for artifact types, controlled-vocabulary
    # values, and infrastructure classes: real classes the extractor
    # never types a *document-mentioned* entity as. The manifest is the
    # one place this set is declared — the prompt renders it from here.
    entity_kind: bool = False


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    mapping_page: str = Field(min_length=1)
    schema_source_item: str
    properties: dict[str, ManifestProperty]
    items: dict[str, ManifestItem]

    @model_validator(mode="after")
    def check_cross_references(self) -> Manifest:
        if self.schema_source_item not in self.items:
            raise ValueError(
                f"schema_source_item {self.schema_source_item!r} "
                f"is not an item key"
            )
        for key, item in self.items.items():
            if (
                item.subclass_of is not None
                and item.subclass_of not in self.items
            ):
                raise ValueError(
                    f"items.{key}.subclass_of {item.subclass_of!r} "
                    f"is not an item key"
                )
        if self.items and "subclass_of" not in self.properties:
            for item in self.items.values():
                if item.subclass_of is not None:
                    raise ValueError(
                        "manifest declares subclass_of relations but no "
                        "'subclass_of' property"
                    )
        for kind, entries in (
            ("properties", self.properties),
            ("items", self.items),
        ):
            seen: dict[str, str] = {}
            for key, entry in entries.items():
                if entry.label in seen:
                    raise ValueError(
                        f"duplicate label {entry.label!r} in {kind}: "
                        f"{seen[entry.label]!r} and {key!r} (labels are the "
                        f"idempotency key and must be unique)"
                    )
                seen[entry.label] = key
        self._check_accepts()
        return self

    def _check_accepts(self) -> None:
        """Fail closed on the kinds each property accepts (ADR-0035
        §3.5): an item-valued property that declares none would escape
        the check, so the manifest does not load."""
        kinds = {key for key, item in self.items.items() if item.entity_kind}
        for key, spec in self.properties.items():
            where = f"properties.{key}"
            if spec.datatype != "wikibase-item":
                if spec.accepts is not None:
                    raise ValueError(
                        f"{where} declares accepts, but only an item-valued "
                        f"property takes item values"
                    )
                if spec.retired:
                    raise ValueError(
                        f"{where} is retired, but only an item-valued "
                        f"property is retired"
                    )
                continue
            if spec.retired:
                if spec.accepts:
                    raise ValueError(
                        f"{where} is retired and accepts nothing, but "
                        f"declares accepts {spec.accepts!r}"
                    )
                continue
            if spec.accepts is None:
                raise ValueError(
                    f"{where} is item-valued and declares no accepts: every "
                    f"item-valued property names the kinds it accepts "
                    f"(ADR-0035 §3.5)"
                )
            if isinstance(spec.accepts, str):
                continue
            if not spec.accepts:
                raise ValueError(
                    f"{where} accepts nothing, which only a retired "
                    f"property does"
                )
            for kind in spec.accepts:
                if kind not in self.items:
                    raise ValueError(
                        f"{where}.accepts names {kind!r}, which is not an "
                        f"item key"
                    )
            if len(set(spec.accepts)) != len(spec.accepts):
                raise ValueError(f"{where}.accepts repeats a kind")
            if set(spec.accepts) & (kinds - {UNCLASSIFIED}) and (
                UNCLASSIFIED not in spec.accepts
            ):
                raise ValueError(
                    f"{where} accepts an entity kind but not "
                    f"{UNCLASSIFIED!r}, which is accepted wherever a real "
                    f"kind is (ADR-0013)"
                )


@dataclass(frozen=True)
class Acceptance:
    """What validation reads of the manifest to check a value's kind
    (ADR-0035 §3.5): each item-valued property's accepted kinds, the
    retired and reference properties, and the entity kinds."""

    # property key -> a frozenset of item keys, or an AcceptsAnyKind form
    accepts: MappingProxyType
    retired: frozenset[str]
    # the items an entity may be typed as: named by its key, such an item
    # is a class, never an entity of that kind
    entity_kinds: frozenset[str] = frozenset()
    # the properties written only in references
    references: frozenset[str] = frozenset()
    # the properties whose claims take their source document, and only it,
    # as subject
    document_subjects: frozenset[str] = frozenset()

    def refusal(
        self,
        prop: str,
        kind: str | None,
        subject_kind: str | None = None,
        vocabulary: bool = False,
    ) -> str | None:
        """Why `prop` refuses a value of `kind`, or None when it takes it.

        `kind` is the value's entity kind, or, for a manifest item named
        by its key (`vocabulary`), that key. A value whose kind is unknown
        (an existing item named by its QID) is refused only by a retired
        property, since nothing else can be checked of it."""
        if prop in self.retired:
            return (
                f"property {prop!r} is retired and accepts no claim (ADR-0036)"
            )
        accepts = self.accepts.get(prop)
        if accepts is None or kind is None or accepts == "any":
            return None
        if accepts == "subject":
            if subject_kind is None or kind == subject_kind:
                return None
            return (
                f"property {prop!r} accepts only the subject's own kind "
                f"{subject_kind!r}, not a {kind!r}"
            )
        if accepts == "class":
            if vocabulary:
                return None
            return f"property {prop!r} accepts a class, not a {kind!r} entity"
        if vocabulary and kind in self.entity_kinds:
            return (
                f"property {prop!r} accepts an entity of kind {kind!r}, "
                f"not the class itself"
            )
        if kind in accepts:
            return None
        return (
            f"property {prop!r} does not accept a {kind!r}: it accepts "
            f"{', '.join(sorted(accepts))} (ADR-0031)"
        )


def load_acceptance(source=DEFAULT_MANIFEST) -> Acceptance:
    """The manifest's accepted kinds, read as `load_manifest` reads it.
    Shared and immutable, so it is cached with the parse."""
    return _acceptance(read_manifest_data(source))


@functools.lru_cache(maxsize=8)
def _acceptance(text: str) -> Acceptance:
    manifest = _parse_manifest(text)
    return Acceptance(
        accepts=MappingProxyType(
            {
                key: (
                    spec.accepts
                    if isinstance(spec.accepts, str)
                    else frozenset(spec.accepts)
                )
                for key, spec in manifest.properties.items()
                if spec.accepts is not None
            }
        ),
        retired=frozenset(
            key for key, spec in manifest.properties.items() if spec.retired
        ),
        entity_kinds=frozenset(
            key for key, item in manifest.items.items() if item.entity_kind
        ),
        references=frozenset(
            key for key, spec in manifest.properties.items() if spec.reference
        ),
        document_subjects=frozenset(
            key
            for key, spec in manifest.properties.items()
            if spec.document_subject
        ),
    )


def load_manifest(source=DEFAULT_MANIFEST) -> Manifest:
    """The manifest at `source`, parsed and validated.

    The text is read on every call, and parsed only when it is not among
    the eight distinct texts loaded most recently; threads that load a
    new text at once may each parse it. Every call returns its own deep
    copy, so a caller that changes its manifest changes no other
    caller's.
    """
    return _parse_manifest(read_manifest_data(source)).model_copy(deep=True)


# Keyed on the text itself, never on the path or its modification time,
# so an edited file is parsed afresh. The pure-Python YAML parse costs
# tens of milliseconds, and the pipeline loads the manifest for every
# extraction, from several threads when documents are extracted
# concurrently. lru_cache is thread-safe: threads that meet a new text
# together may each parse it, to equal results. A process meets one
# manifest or a few edited copies of it; the bound stops a long-lived
# one from keeping every text it has read.
@functools.lru_cache(maxsize=8)
def _parse_manifest(text: str) -> Manifest:
    """Shared by every caller, so never handed out: `load_manifest`
    returns copies."""
    return Manifest.model_validate(yaml.safe_load(text))
