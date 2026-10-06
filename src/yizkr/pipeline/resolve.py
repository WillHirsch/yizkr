"""Entity resolution step: every local ref stays local, for the writer to
resolve through the identity map.

An entity's item is found or minted through the identity map alone
(yizkr.pipeline.clustering, ADR-0035): the writer maps each mention of a
batch to its cluster, and finds or makes the one item of that cluster
(yizkr.pipeline.writer, step 2). Nothing is matched by its label, so this
step reads nothing of the wiki and replaces no local ref: an approved
equivalence, the one decision that names an existing item, has replaced
its entity's refs with the item's QID before it (reconcile.
apply_equivalences), and a curator's decision that two entities are
distinct is never undone by a label they share. The review sheet so
shows every local entity as NEW (yizkr.pipeline.review).

Given the identity map the batch is to be written with, the step checks
that the map holds every mention of the batch, so a batch the writer
would refuse whole for an unmapped mention is refused here, before its
review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yizkr.claims import ClaimBatch

if TYPE_CHECKING:
    from yizkr.pipeline.clustering import IdentityMap


def resolve_batch(
    batch: ClaimBatch, identity: IdentityMap | None = None
) -> ClaimBatch:
    """A copy of `batch` with every ref as it is: the writer finds each
    local entity's item through the identity map (the module docstring).
    With `identity`, raises UnmappedMention when the batch names a local
    entity and the map does not hold every mention of the batch under an
    artifact its claims cite (IdentityMap.clusters_of)."""
    if identity is not None:
        from yizkr.pipeline.writer import mapped_clusters

        mapped_clusters(batch, identity)
    return batch.model_copy(deep=True)
