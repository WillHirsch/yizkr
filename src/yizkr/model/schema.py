"""Manifest schema: the declared vocabulary (docs/MODELING.md).

The manifest ships as package data (yizkr/model/manifest.yaml) so the
vocabulary versions with the code; pass an explicit path to work
against an edited copy.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
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
    # Whether a claim of the property states what an artifact is
    # (`issued_by`, `author` and the other document-subject properties,
    # ADR-0035 §3.4, ADR-0039 §2.2, §10): its subject is its own source
    # document, named by the document's content key, an artifact its
    # source places, named by the SHA-1 of its bytes, a batch entity of an
    # artifact kind, or an unclassified entity of the batch, and no other.
    document_subject: bool = False
    # Whether a claim of the property has as its subject an artifact its
    # source places, named by the SHA-1 of the artifact's bytes, and no
    # other subject (`caption`, ADR-0039 §2.1).
    placed_subject: bool = False
    # Whether a claim of the property may give the unknown value (Wikibase
    # "some value"): a source that states a death or a burial but not its
    # date or place (ADR-0040 §2.2, Q3). No other property takes one.
    unknown_value: bool = False
    # The kinds a claim's subject may be, where the property takes only
    # some (ADR-0040 §5, Q11: a death or birth property is a person's).
    # None takes a subject of any kind.
    subjects: list[str] | None = None

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
    # A class of entities that is no entity kind: an artifact type, a
    # time period, an infrastructure class. With the entity kinds, these
    # are the manifest's kind keys: named by its key, such an item is the
    # class itself, so only a property that accepts a class takes it as a
    # value (ADR-0035 §3.5). An item in the subclass hierarchy is one or
    # the other. Outside the hierarchy nothing in the manifest tells a
    # class (`time_period_class`) from a controlled-vocabulary value
    # (`circa`): each may be the one item a property accepts. The mark is
    # what tells them apart, so the loader cannot check it there.
    entity_class: bool = False
    # False for an entity kind that only a curator makes and no extraction
    # types an entity as (`birth`, `death`, ADR-0040 §3, §7): validation
    # refuses an entity of it in a batch, and no prompt offers it.
    extracted: bool = True
    # Whether the prompts rendered from the manifest offer the item. An
    # item the pipeline sets from what a source says of itself, and no
    # extractor sets claim by claim (`machine_assisted`, ADR-0008), is
    # held back.
    offered: bool = True


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
        self._check_classes()
        self._check_accepts()
        for key, spec in self.properties.items():
            for kind in spec.subjects or ():
                if kind not in self.items:
                    raise ValueError(
                        f"properties.{key}.subjects names {kind!r}, which "
                        f"is not an item key"
                    )
            if spec.unknown_value and (spec.reference or spec.retired):
                raise ValueError(
                    f"properties.{key} takes the unknown value, but it is "
                    f"never a claim's property"
                )
        for key, item in self.items.items():
            if not item.extracted and not item.entity_kind:
                raise ValueError(
                    f"items.{key} is marked extracted false, which only an "
                    f"entity kind is"
                )
        for key, spec in self.properties.items():
            if spec.document_subject and spec.placed_subject:
                raise ValueError(
                    f"properties.{key} is marked both document_subject and "
                    f"placed_subject: a claim's subject is its own source "
                    f"or an artifact its source places, never either"
                )
        return self

    def _check_classes(self) -> None:
        """Fail closed on the kind keys: an item in the subclass
        hierarchy, a subclass or a superclass, is a class, so it is an
        entity kind or marked `entity_class`; and an entity kind is never
        also marked one."""
        superclasses = {
            item.subclass_of
            for item in self.items.values()
            if item.subclass_of is not None
        }
        for key, item in self.items.items():
            if item.entity_kind and item.entity_class:
                raise ValueError(
                    f"items.{key} is an entity kind, which is a class "
                    f"already: it is not also marked entity_class"
                )
            in_hierarchy = item.subclass_of is not None or key in superclasses
            if in_hierarchy and not (item.entity_kind or item.entity_class):
                raise ValueError(
                    f"items.{key} is in the subclass hierarchy, so it is a "
                    f"class: mark it entity_kind or entity_class"
                )

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
    # the other classes of entities (`entity_class`): with the entity
    # kinds, the kind keys
    entity_classes: frozenset[str] = frozenset()
    # the properties written only in references
    references: frozenset[str] = frozenset()
    # the properties whose claims take their source document as subject,
    # an artifact it places, a batch entity of an artifact kind (an un-held
    # media item) or an unclassified entity of the batch (ADR-0039 §10.1)
    document_subjects: frozenset[str] = frozenset()
    # the properties whose claims take an artifact their source places, and
    # only such an artifact, as subject
    placed_subjects: frozenset[str] = frozenset()
    # the artifact kinds: `artifact`, and each item a `subclass_of` it
    artifact_kinds: frozenset[str] = frozenset()
    # the properties whose claims may give the unknown value
    unknown_values: frozenset[str] = frozenset()
    # property key -> the kinds its claim's subject may be, for the
    # properties that take only some
    subjects: MappingProxyType = field(
        default_factory=lambda: MappingProxyType({})
    )
    # the entity kinds no extraction makes (`birth`, `death`)
    unextracted_kinds: frozenset[str] = frozenset()

    @property
    def kind_keys(self) -> frozenset[str]:
        """The items that name a class of entities: the entity kinds and
        the other entity classes."""
        return self.entity_kinds | self.entity_classes

    def refusal(
        self,
        prop: str,
        kind: str | None,
        subject_kind: str | None = None,
        vocabulary: bool = False,
    ) -> str | None:
        """Why `prop` refuses a value of `kind`, or None when it takes it.

        `kind` is the value's entity kind, or, for a manifest item named
        by its key (`vocabulary`), that key. A kind key named so is a
        class: only a property that accepts a class takes it, and such a
        property takes no other vocabulary item. A property that accepts
        any kind, or the subject's own, takes an entity and so no
        vocabulary item at all. A value whose kind is
        unknown (an existing item named by its QID) is refused only by a
        retired property, since nothing else can be checked of it."""
        if prop in self.retired:
            return (
                f"property {prop!r} is retired and accepts no claim (ADR-0036)"
            )
        accepts = self.accepts.get(prop)
        if accepts is None:
            return None
        if accepts == "class":
            if vocabulary:
                if kind in self.kind_keys:
                    return None
                return (
                    f"property {prop!r} accepts a class, and {kind!r} is a "
                    f"vocabulary value, not a class"
                )
            if kind is None:
                return None
            return f"property {prop!r} accepts a class, not a {kind!r} entity"
        if vocabulary and kind in self.kind_keys:
            return (
                f"property {prop!r} accepts an entity of kind {kind!r}, not "
                f"the class itself: {kind!r} is a kind key"
            )
        if vocabulary and accepts in ("any", "subject"):
            return (
                f"property {prop!r} takes an entity, not the vocabulary "
                f"value {kind!r}"
            )
        if kind is None or accepts == "any":
            return None
        if accepts == "subject":
            if subject_kind is None or kind == subject_kind:
                return None
            return (
                f"property {prop!r} accepts only the subject's own kind "
                f"{subject_kind!r}, not a {kind!r}"
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
        entity_classes=frozenset(
            key for key, item in manifest.items.items() if item.entity_class
        ),
        references=frozenset(
            key for key, spec in manifest.properties.items() if spec.reference
        ),
        document_subjects=frozenset(
            key
            for key, spec in manifest.properties.items()
            if spec.document_subject
        ),
        placed_subjects=frozenset(
            key
            for key, spec in manifest.properties.items()
            if spec.placed_subject
        ),
        artifact_kinds=frozenset(
            key
            for key, item in manifest.items.items()
            if key == "artifact" or item.subclass_of == "artifact"
        ),
        unknown_values=frozenset(
            key
            for key, spec in manifest.properties.items()
            if spec.unknown_value
        ),
        subjects=MappingProxyType(
            {
                key: frozenset(spec.subjects)
                for key, spec in manifest.properties.items()
                if spec.subjects is not None
            }
        ),
        unextracted_kinds=frozenset(
            key
            for key, item in manifest.items.items()
            if item.entity_kind and not item.extracted
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
