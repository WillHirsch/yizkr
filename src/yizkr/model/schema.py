"""Manifest schema: the declared vocabulary (docs/MODELING.md).

The manifest ships as package data (yizkr/model/manifest.yaml) so the
vocabulary versions with the code; pass an explicit path to work
against an edited copy.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
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


class ManifestProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    # Wikibase caps entity descriptions at 250 characters; fail at
    # manifest-load time rather than mid-write against the instance.
    description: str = Field(min_length=1, max_length=250)
    datatype: Datatype
    aliases: list[str] = []
    wikidata: str | None = Field(default=None, pattern=r"^P\d+$")


class ManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    # Wikibase caps entity descriptions at 250 characters; fail at
    # manifest-load time rather than mid-write against the instance.
    description: str = Field(min_length=1, max_length=250)
    aliases: list[str] = []
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    subclass_of: str | None = None


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
        return self


def load_manifest(source=DEFAULT_MANIFEST) -> Manifest:
    return Manifest.model_validate(yaml.safe_load(read_manifest_data(source)))
