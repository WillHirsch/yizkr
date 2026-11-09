"""Immutable human creation scope and its operational matching context.

These records connect an explicit creation decision to the existing creation
registry. The context describes the interpretation reviewed by the curator;
it is neither a historical graph assertion nor a new source product.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import EntityRecord, container_problems, structure_problems
from yizkr.core.identity_assignments import record_digest
from yizkr.core.write_recovery import Generation
from yizkr.pipeline.identity_review import Decision, MentionRevision
from yizkr.pipeline.review_mentions import source_scope_data

_HASH = r"^[0-9a-f]{64}$"


def creation_payload_sha256(payload: dict[str, Any]) -> str:
    """Use the creation registry's exact initial-payload encoding."""
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _display_payload(payload: dict[str, Any]) -> None:
    if (
        set(payload)
        - {"type", "labels", "descriptions", "aliases", "sitelinks", "claims"}
        or payload.get("type") != "item"
        or not payload.get("labels")
        or payload.get("claims")
        or payload.get("aliases")
        or payload.get("sitelinks")
    ):
        raise ValueError(
            "reviewed creation permits only display labels and descriptions"
        )
    for field in ("labels", "descriptions"):
        terms = payload.get(field, {})
        if not isinstance(terms, dict):
            raise ValueError(
                "creation display terms must be language mappings"
            )
        for language, term in terms.items():
            if (
                not isinstance(term, dict)
                or set(term) != {"language", "value"}
                or term["language"] != language
                or not isinstance(term["value"], str)
                or not term["value"].strip()
            ):
                raise ValueError("creation display term is invalid")


def creation_review_evidence(
    initial_payload: dict[str, Any],
) -> dict[str, Any]:
    """The value for proposal evidence's ``reviewed_creation`` field."""
    _display_payload(initial_payload)
    return {
        "format": 1,
        "initial_payload": json.loads(json.dumps(initial_payload)),
        "initial_payload_sha256": creation_payload_sha256(initial_payload),
    }


def reviewed_creation_operation(decision: Decision) -> dict[str, str]:
    """A durable scope survives refreshed observations and graph snapshots."""
    proposal = decision.proposal
    if (
        proposal.operation != "new_entity"
        or decision.outcome != "approve"
        or len(proposal.mentions) != 1
        or proposal.target is not None
    ):
        raise ValueError(
            "creation needs one explicit approved new-entity decision"
        )
    return {
        "kind": "reviewed-entity-creation",
        "proposal_id": proposal.id,
        "mention_id": proposal.mentions[0],
    }


def reviewed_creation_context(
    decision: Decision, mention: MentionRevision
) -> EntityRecord:
    """Recover only the entity interpretation actually shown for this scope."""
    reviewed_creation_operation(decision)
    proposal = decision.proposal
    observation = mention.observation
    if (
        proposal.mentions != (mention.id,)
        or len(proposal.mention_revisions) != 1
        or proposal.mention_revisions[0].get("id") != mention.id
        or type(proposal.mention_revisions[0].get("revision")) is not int
        or not 0
        < proposal.mention_revisions[0]["revision"]
        <= mention.revision
        or observation.anchor is None
        or observation.source_scope is None
        or observation.limitations
    ):
        raise ValueError(
            "creation lacks unambiguous current source correspondence"
        )
    try:
        material = proposal.basis["mentions"][mention.id]
        interpretation = material["interpretation"]
        expected_occurrence = {
            "scope": source_scope_data(observation.source_scope),
            "quote": unicodedata.normalize(
                "NFC", observation.context_quote or ""
            ),
            "correspondence": observation.anchor.correspondence_key,
        }
        label = proposal.origin["review_labels"][mention.id]
        if (
            material["occurrence"] != expected_occurrence
            or interpretation["mention"] != mention.id
        ):
            raise ValueError(
                "creation source differs from reviewed occurrence"
            )
        entity = EntityRecord.model_validate(
            interpretation["interpretation"]
            | {"id": observation.local_id, "label": label}
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "creation lacks reviewed entity interpretation"
        ) from exc
    if entity.qid is not None:
        raise ValueError("new-entity context cannot name an existing QID")
    if structure_problems(entity) or container_problems(entity):
        raise ValueError(
            "reviewed creation context fails current entity validation"
        )
    return entity


class ReviewedCreationAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1] = 1
    kind: Literal["reviewed-creation-authorization"] = (
        "reviewed-creation-authorization"
    )
    generation: Generation
    decision: Decision
    mention: MentionRevision
    context: EntityRecord
    operation: dict[str, str]
    initial_payload: dict[str, Any]
    initial_payload_sha256: str = Field(pattern=_HASH)
    origin: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exact_scope(self):
        if self.operation != reviewed_creation_operation(self.decision):
            raise ValueError(
                "creation operation differs from its reviewed scope"
            )
        if self.context != reviewed_creation_context(
            self.decision, self.mention
        ):
            raise ValueError(
                "creation context differs from its reviewed meaning"
            )
        expected = creation_review_evidence(self.initial_payload)
        if (
            self.initial_payload_sha256 != expected["initial_payload_sha256"]
            or self.decision.proposal.basis.get("evidence", {}).get(
                "reviewed_creation"
            )
            != expected
        ):
            raise ValueError(
                "initial payload differs from explicit creation review"
            )
        proposal = self.decision.proposal
        if (
            record_digest(
                {
                    "operation": proposal.operation,
                    "mentions": proposal.mentions,
                    "target": None,
                    "before": proposal.before,
                    "basis_version": proposal.basis_version,
                    "basis": proposal.basis,
                }
            )
            != proposal.digest
        ):
            raise ValueError("creation decision has an altered material basis")
        record_digest(self.origin)
        return self

    @property
    def digest(self) -> str:
        return record_digest(self)
