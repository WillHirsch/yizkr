"""Append an accepted mention's reviewed names to its entity's aliases.

An accepted identity decision is the authority for these display terms: no
alias takes a decision of its own. The names are the mention's label and its
entity record's aliases as the decision's review row showed them: the label
in the proposal's ``review_labels``, the aliases in its material basis. They
depend on the decision's proposal alone, never on who made the decision. A
mention without an entity record names nothing: its review label is its
local ID.

Each name is compared and sent in the form the instance keeps a term in, as
the map-bound writer's ``BatchWriter._ensure_aliases`` compares and sends a
document's name variants, so for one entity record the two add the same
aliases. A name holding a character Python's Unicode tables do not know,
which the instance may keep in another form, is left out and reported in the
contribution, never altered.

The update is append-only and completes once for each accepted assignment
revision. It reads the item afresh and appends the names the item holds
neither as its label nor as an alias, leaving its label, descriptions,
statements and other aliases, a curator's included, as they are. Immutable
registry pages keyed to the assignment revision record it: when the item
lacks any of the names, an intent names the aliases the update appends
before it is sent, and a receipt, once the item holds them, names the
aliases it appended, or none. One revision-checked, single-attempt edit sends
them; a conflict or an uncertain result stops before the receipt. A retry
without a receipt reads the item again and appends whichever of the intent's
names it lacks, instead of resending an earlier edit; its receipt names
every intended alias the item then holds, as the lost edit may have appended
them. A receipt completes the update: a later repeat reads no item and
appends nothing, so a curator's later rename, or removal of an appended
alias, stands.

Each publisher of an accepted assignment's direct origin, a cohort choice's
(``cohort_provenance``) and an owner override's
(``owner_override_provenance``), appends them after the origin and returns an
``AssignmentPublication``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.core.cancellation import check_cancelled
from yizkr.core.identity_assignments import AssignmentRevision, record_json
from yizkr.core.instance import require_retained_code
from yizkr.core.write_recovery import (
    PREFIX,
    CreationRefused,
    CreationRegistry,
    write_existing,
)
from yizkr.pipeline.identity_provenance import IdentityPublication
from yizkr.pipeline.identity_review import Decision
from yizkr.pipeline.writer import _terms, unknown_characters


class AliasContribution(BaseModel):
    """The names one accepted identity decision gives its mention's entity.

    ``names`` are in the form the instance keeps a term in, the label
    first; ``left_out`` are the shown names the instance may keep in
    another form, as the review row showed them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mention_id: str
    decision_id: str
    decision_revision: int
    names: tuple[str, ...]
    left_out: tuple[str, ...] = ()


class _AliasRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1] = 1
    deployment_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    assignment: str = Field(pattern=r"^[0-9a-f]{64}$")
    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    contribution: AliasContribution


class AliasIntent(_AliasRecord):
    """The aliases an assignment's update appends to the item ``qid``,
    recorded before the update is sent; it may or may not have landed
    until a receipt records it."""

    page: ClassVar[str] = "intent"
    kind: Literal["assignment-alias-intent"] = "assignment-alias-intent"
    names: tuple[str, ...] = Field(min_length=1)


class AliasReceipt(_AliasRecord):
    """A completed alias update of an assignment: ``added`` are the
    aliases it appended to the item ``qid``, in the order sent, which the
    item held once it completed; empty when the item already held every
    name."""

    page: ClassVar[str] = "receipt"
    kind: Literal["assignment-alias-receipt"] = "assignment-alias-receipt"
    added: tuple[str, ...]


@dataclass(frozen=True)
class AliasAddition:
    """One call's alias update: ``added`` are the aliases this call
    appended, and ``receipt`` records the assignment's completed update,
    which an earlier call may have made."""

    receipt: AliasReceipt
    added: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentPublication(IdentityPublication):
    """A published assignment origin and the aliases appended after it.

    ``changed`` describes the origin publication alone; ``aliases.added``
    names what this call's alias update appended.
    """

    aliases: AliasAddition


def alias_contribution(
    decision: Decision, mention_id: str
) -> AliasContribution:
    """The names ``decision`` gives the entity of ``mention_id``."""
    proposal = decision.proposal
    material = proposal.basis["mentions"][mention_id]["interpretation"]
    record = material["interpretation"]
    shown = (
        []
        if record is None
        else [proposal.origin["review_labels"][mention_id], *record["aliases"]]
    )
    return AliasContribution(
        mention_id=mention_id,
        decision_id=decision.id,
        decision_revision=decision.revision,
        names=tuple(_terms(shown)),
        left_out=tuple(
            dict.fromkeys(name for name in shown if unknown_characters(name))
        ),
    )


def _record(registry, generation, assignment, cls, proposed=None):
    """The assignment's alias record of type ``cls``, publishing
    ``proposed`` first when there is none; None when there is none."""
    title = (
        f"{PREFIX}{generation.generation}/assignment-aliases/"
        f"{assignment.revision_id}/{cls.page}"
    )
    page = registry.client.immutable_page(title)
    if page is None and proposed is not None:
        require_retained_code(registry.client)
        check_cancelled()
        try:
            page = registry.client.create_page_once(
                title,
                record_json(proposed),
                f"identity: record an assignment's alias {cls.page}",
            )
        except Exception:
            # A lost response may follow a durable create: read, not resend.
            page = registry.client.immutable_page(title)
            if page is None:
                raise
        if registry.generation != generation:
            raise CreationRefused("deployment generation changed")
    if page is None:
        return None
    try:
        record = cls.model_validate_json(page.text)
    except (ValueError, ValidationError) as exc:
        raise CreationRefused("invalid assignment alias record") from exc
    if (
        record.deployment_id != generation.deployment_id
        or record.generation != generation.generation
        or record.assignment != assignment.revision_id
        or record.contribution.mention_id != assignment.mention_id
    ):
        raise CreationRefused(
            "alias record belongs to another assignment or generation"
        )
    return record


def alias_records(
    registry: CreationRegistry, assignment: AssignmentRevision
) -> tuple[AliasIntent | None, AliasReceipt | None]:
    """The recorded intent and receipt of the assignment's alias update;
    each None when there is none. Read only."""
    generation = registry.generation
    if generation is None:
        return None, None
    return (
        _record(registry, generation, assignment, AliasIntent),
        _record(registry, generation, assignment, AliasReceipt),
    )


def append_aliases(
    registry: CreationRegistry,
    assignment: AssignmentRevision,
    qid: str,
    contribution: AliasContribution,
) -> AliasAddition:
    """Append the contribution's names that the item ``qid`` lacks, once
    for the assignment revision."""
    generation = registry.generation
    if generation is None:
        raise CreationRefused("alias records need a deployment generation")
    intent, receipt = alias_records(registry, assignment)
    if any(r is not None and r.qid != qid for r in (intent, receipt)):
        raise CreationRefused("alias record names another item")
    if receipt is not None:
        return AliasAddition(receipt, ())
    item = registry.client.wbi.item.get(qid)
    label = str(item.labels.get("en") or "")
    held = {str(alias) for alias in item.aliases.get("en") or []}
    record = {
        "deployment_id": generation.deployment_id,
        "generation": generation.generation,
        "assignment": assignment.revision_id,
        "qid": qid,
    }
    if intent is None:
        missing = sorted(
            {name for name in contribution.names if name != label} - held
        )
        if missing:
            intent = _record(
                registry,
                generation,
                assignment,
                AliasIntent,
                AliasIntent(
                    **record, contribution=contribution, names=missing
                ),
            )
    names = () if intent is None else intent.names
    sent = tuple(name for name in names if name != label and name not in held)
    if sent:
        require_retained_code(registry.client)
        item.aliases.set("en", list(sent))
        write_existing(
            item, summary="identity: aliases of an accepted assignment"
        )
    proposed = AliasReceipt(
        **record,
        contribution=contribution if intent is None else intent.contribution,
        added=tuple(name for name in names if name in held or name in sent),
    )
    receipt = _record(registry, generation, assignment, AliasReceipt, proposed)
    if receipt != proposed:
        raise CreationRefused("another alias receipt records different names")
    return AliasAddition(receipt, sent)
