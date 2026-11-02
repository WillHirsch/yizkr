"""Portable process provenance, independent of graph and execution clients."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordRef(_Record):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodeProvenance(_Record):
    version: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    diff_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _Timed(_Record):
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("process timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered_times(self):
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("process end precedes its start")
        return self


class StageProvenance(_Timed):
    stage: str = Field(min_length=1)
    process: Literal["deterministic", "model", "mixed", "manual", "unknown"]
    model: str | None = None
    model_identifier: str | None = None
    effort: str | None = None
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    config_hash: str | None = None
    dependency_hash: str | None = None
    resource_hashes: dict[str, str] = Field(default_factory=dict)
    semantic_version: str | int | None = None
    code: CodeProvenance | None = None
    cli_version: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()


class ProcessRecord(_Timed):
    format: Literal[1] = 1
    kind: str = Field(min_length=1)
    stages: tuple[StageProvenance, ...] = ()
    producer_refs: tuple[RecordRef, ...] = ()
    code: CodeProvenance | None = None
    unratified: bool | None = None
    scratch_id: str | None = None
    manifest_hash: str | None = None
    run_manifest_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
