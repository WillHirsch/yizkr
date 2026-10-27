"""Native source locations shared by inventory records and citations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StructuralLocator(BaseModel):
    """A native part or occurrence, before eligibility and reading filters.

    ``position`` is the physical page/slide order, not a printed label or
    an ordinal among the images selected for reading. ``path`` preserves a
    native object/shape occurrence path when no native identifier suffices.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    kind: str = Field(min_length=1)
    package_part: str | None = None
    position: int | None = Field(default=None, ge=1)
    path: str | None = None
    native_id: str | None = None
    relationship_id: str | None = None
