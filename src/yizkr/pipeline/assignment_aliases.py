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

The update is append-only. It reads the item afresh, appends the names the
item holds neither as its label nor as an alias, and leaves its label,
descriptions, statements and other aliases, a curator's included, as they
are. One revision-checked, single-attempt edit sends them; a conflict or an
uncertain result stops, and a retry reads the item again instead of resending
an earlier edit. A repeat against an unchanged item adds nothing; one after a
curator's edit appends whichever of the names the item no longer holds.

Each publisher of an accepted assignment's direct origin, a cohort choice's
(``cohort_provenance``) and an owner override's
(``owner_override_provenance``), appends them after the origin and returns an
``AssignmentPublication``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from yizkr.core.identity_assignments import AssignmentRevision
from yizkr.core.instance import require_retained_code
from yizkr.core.write_recovery import write_existing
from yizkr.pipeline.identity_provenance import IdentityPublication
from yizkr.pipeline.identity_review import Decision
from yizkr.pipeline.writer import _terms, unknown_characters

_DIRECT = ("assign", "new_entity")


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


@dataclass(frozen=True)
class AliasAddition:
    """The aliases one update appended to the item ``qid``, in the order
    sent; empty when the item already held every name."""

    contribution: AliasContribution
    qid: str
    added: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentPublication(IdentityPublication):
    """A published assignment origin and the aliases appended after it.

    ``changed`` describes the origin publication alone; ``aliases.added``
    names what the alias update appended.
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


def direct_decision(
    decisions: Iterable[Decision], assignment: AssignmentRevision
) -> Decision | None:
    """The one approved ``assign`` or ``new_entity`` decision of the
    assignment's own mention among the decisions it references; None when
    there is no such decision, as for an assignment that only a ``same``
    decision gave, or more than one."""
    references = {(r.id, r.revision) for r in assignment.decision_references}
    direct = {
        (d.id, d.revision): d
        for d in decisions
        if (d.id, d.revision) in references
        and d.outcome == "approve"
        and d.proposal.operation in _DIRECT
        and d.proposal.mentions == (assignment.mention_id,)
    }
    return next(iter(direct.values())) if len(direct) == 1 else None


def append_aliases(
    client, qid: str, contribution: AliasContribution
) -> AliasAddition:
    """Append the contribution's names that the item ``qid`` lacks."""
    item = client.wbi.item.get(qid)
    label = str(item.labels.get("en") or "")
    held = {str(alias) for alias in item.aliases.get("en") or []}
    missing = sorted(
        {name for name in contribution.names if name != label} - held
    )
    if missing:
        require_retained_code(client)
        item.aliases.set("en", missing)
        write_existing(
            item, summary="identity: aliases of an accepted assignment"
        )
    return AliasAddition(contribution, qid, tuple(missing))
