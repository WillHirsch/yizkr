"""Source observations and the review records the creation registry embeds.

The pipeline's review store persists these records, and the creation
registry stores and checks them in its own records: reviewed creations,
assignment evidence and planned claim applications. They are plain frozen
models with no ``format`` field, unlike the registry's own records.

Their canonical form folds Unicode to NFC, sorts mapping keys and writes
integral floats as integers, without folding meaningful spelling or spaces.
Correspondence and scope keys, and the product digest of an observation,
are digests of that form, and the material identity basis is written in
it. Stored keys and proposal digests stay valid only while the form is
unchanged.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import ArtifactRef, SourceSupport
from yizkr.core.write_recovery import EntityBinding

Operation = Literal["same", "different", "assign", "new_entity"]
Outcome = Literal["approve", "reject"]


def canonical_form(value: Any) -> Any:
    """Canonical JSON scalars without folding meaningful spelling or spaces."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            key: canonical_form(item) for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [canonical_form(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_form_json(value: Any) -> str:
    return json.dumps(
        canonical_form(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_form_digest(value: Any) -> str:
    return hashlib.sha256(canonical_form_json(value).encode()).hexdigest()


def sorted_by_canonical_form(values: list) -> list:
    return sorted(values, key=canonical_form_json)


def support_scope(support: SourceSupport) -> dict:
    data = support.model_dump(mode="json", exclude={"reading_sha256"})
    data["placements"] = sorted_by_canonical_form(data["placements"])
    return canonical_form(data)


class SourceScope(BaseModel):
    """Verified ownership and native scope, without an occurrence identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: ArtifactRef
    support: SourceSupport | None = None
    physical_page: int | None = Field(default=None, ge=1)


def source_scope_data(scope: SourceScope) -> dict:
    """Material native scope, excluding reading digest and text offsets."""
    return {
        "owner": scope.owner.model_dump(mode="json"),
        "support": support_scope(scope.support) if scope.support else None,
        "physical_page": scope.physical_page,
    }


class OccurrenceAnchor(SourceScope):
    """One literal occurrence with its exact reading ranges and source scope.

    Positions are zero-based character offsets; the quote range addresses the
    reading and the mention range addresses that quote. A physical page is
    supplied only for a standalone paged reading without native source context.
    All native placements remain in support, without selecting one witness.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote: str = Field(min_length=1)
    quote_start: int = Field(ge=0)
    quote_end: int = Field(ge=0)
    mention_start: int = Field(ge=0)
    mention_end: int = Field(ge=0)
    literal: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ranges_match_text(self) -> OccurrenceAnchor:
        if self.quote_end - self.quote_start != len(self.quote):
            raise ValueError("quote range does not match the recorded text")
        if not 0 <= self.mention_start < self.mention_end <= len(self.quote):
            raise ValueError("mention range does not lie in its quote")
        if self.quote[self.mention_start : self.mention_end] != self.literal:
            raise ValueError("mention range does not match the literal")
        return self

    @property
    def scope_key(self) -> str:
        return canonical_form_digest(anchor_scope(self))

    @property
    def correspondence_key(self) -> str:
        # Canonically equivalent Unicode has the same text and relative
        # location even when its encoded character count differs.
        prefix = unicodedata.normalize("NFC", self.quote[: self.mention_start])
        literal = unicodedata.normalize("NFC", self.literal)
        return canonical_form_digest(
            {
                "version": 1,
                "scope": anchor_scope(self),
                "quote": self.quote,
                "mention_start": len(prefix),
                "mention_end": len(prefix) + len(literal),
            }
        )


def anchor_scope(anchor: OccurrenceAnchor) -> dict:
    """Native source identity, excluding reading digest and text offsets."""
    return source_scope_data(anchor)


class MentionObservation(BaseModel):
    """An auditable input to correspondence, with no allocated durable ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: str = Field(pattern=r"^[0-9a-f]{40}$")
    local_id: str = Field(min_length=1)
    product_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reading_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_scope: SourceScope | None = None
    context_quote: str | None = Field(default=None, min_length=1)
    context_start: int | None = Field(default=None, ge=0)
    context_end: int | None = Field(default=None, ge=0)
    anchor: OccurrenceAnchor | None = None
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _located_context(self) -> MentionObservation:
        context = (
            self.source_scope,
            self.context_quote,
            self.context_start,
            self.context_end,
        )
        if any(value is not None for value in context):
            if any(value is None for value in context):
                raise ValueError("source context needs its quote and ranges")
            if self.context_end - self.context_start != len(
                self.context_quote
            ):
                raise ValueError("context range does not match the quote")
            if self.source_scope.owner.sha1 != self.artifact:
                raise ValueError("source scope belongs to another owner")
        if self.anchor is not None and (
            self.source_scope is None
            or (
                anchor_scope(self.anchor)
                != source_scope_data(self.source_scope)
                or self.anchor.quote != self.context_quote
                or self.anchor.quote_start != self.context_start
                or self.anchor.quote_end != self.context_end
            )
        ):
            raise ValueError("anchor does not match its located context")
        return self

    @property
    def correspondence_key(self) -> str | None:
        return self.anchor.correspondence_key if self.anchor else None


class ReviewRefused(ValueError):
    """The requested review cannot be attached to its claimed evidence."""


def check_actor_time(actor: str, time: str) -> None:
    """Refuse a review without an actor and a timezone-aware ISO time."""
    if not actor.strip():
        raise ReviewRefused("a review actor is required")
    try:
        parsed = datetime.fromisoformat(time)
    except (TypeError, ValueError) as exc:
        raise ReviewRefused("review time must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRefused("review time must include its timezone")


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MentionRevision(_Record):
    id: str
    revision: int = Field(gt=0)
    observation: MentionObservation
    correspondence: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.observation.artifact, self.observation.local_id

    @property
    def reference(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


class Proposal(_Record):
    id: str
    revision: int = Field(gt=0)
    operation: Operation
    mentions: tuple[str, ...]
    mention_revisions: tuple[dict[str, Any], ...]
    target: EntityBinding | None = None
    before: dict[str, Any] = Field(default_factory=dict)
    basis_version: Literal[1] = 1
    basis: dict[str, Any]
    origin: dict[str, Any]
    digest: str


class Decision(_Record):
    id: str
    revision: int = Field(gt=0)
    proposal: Proposal
    outcome: Outcome
    actor: str
    time: str
    predecessor: dict[str, Any] | None = None
    review_sheet_id: str | None = None

    @property
    def reference(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


class ClaimProposal(_Record):
    """An immutable historical meaning and support, separate from identity."""

    id: str
    revision: Literal[1] = 1
    basis_version: Literal[1] = 1
    basis: dict[str, Any]
    digest: str
    origin: dict[str, Any]


class ClaimDecision(_Record):
    id: str
    revision: int = Field(gt=0)
    proposal: ClaimProposal
    outcome: Outcome
    actor: str
    time: str
    depicts_confirmed: bool = False
    predecessor: dict[str, Any] | None = None

    @property
    def reference(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}
