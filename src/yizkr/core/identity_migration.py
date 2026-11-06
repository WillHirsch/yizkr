"""Verified map-origin evidence and an assignment-acceptance receipt.

The existing creation registry owns receipt storage. A receipt establishes no
historical statement write, graph projection completion or legacy approval.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.identity_assignments import AssignmentRevision, RegistryBinding

_HASH = r"^[0-9a-f]{64}$"
_ID = r"^[0-9a-f]{32}$"


class MigrationRefused(ValueError):
    """The supplied evidence cannot support this bounded migration."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


class MigrationCorrespondence(_Record):
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    local_id: str = Field(min_length=1)
    mention_id: str = Field(pattern=_ID)
    mention_revision: int = Field(
        gt=0,
        description="Current reviewed observation; an unchanged assignment "
        "can retain its original earlier observation revision.",
    )
    legacy_fingerprint: str = Field(pattern=_HASH)
    cluster_id: str = Field(pattern=_HASH)
    kind: str = Field(min_length=1)
    method: str = Field(min_length=1)
    binding: RegistryBinding


class MigrationReceipt(_Record):
    kind: Literal["identity-migration-accepted"] = (
        "identity-migration-accepted"
    )
    migration_id: str = Field(pattern=_HASH)
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    map_hash: str = Field(pattern=_HASH)
    map_sha256: str = Field(pattern=_HASH)
    record_sha256: str = Field(pattern=_HASH)
    assignment_head: str = Field(pattern=_HASH)
    correspondences: tuple[MigrationCorrespondence, ...]
    assignments: tuple[AssignmentRevision, ...]

    @model_validator(mode="after")
    def exact_scope(self):
        mentions = {item.mention_id for item in self.correspondences}
        assigned = {item.mention_id for item in self.assignments}
        if (
            not mentions
            or mentions != assigned
            or len(mentions) != len(self.correspondences)
            or len(assigned) != len(self.assignments)
        ):
            raise ValueError(
                "migration receipt has an inconsistent mention scope"
            )
        return self
