"""Greedy-overlap artifact sequencing for serial ingestion (ADR-0019).

Serial ingestion reconciles each artifact against the graph its
predecessors have already populated. Processing the most *connective*
artifacts first — the ones whose entities recur across the rest of the
corpus — seeds that graph with rich, reusable context early, so later
artifacts' matches are high-confidence rather than name-only guesses.

This is a sequencing hint only: it changes the ORDER artifacts are
processed, never what is written. A poor ranking costs a little review
ease, never correctness. The overlap signal is a shared whole name form
read through each entity's mention record — the same signal candidate
blocking uses (`reconcile.entity_name_keys`) — so "share an entity" here
means "share a name key", a cheap proxy computed offline from the
extraction manifests, no model calls.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable


def sequence_documents(
    items: Iterable[tuple[Hashable, set[str]]],
) -> list[Hashable]:
    """Order document ids by greedy overlap with the remaining corpus.

    ``items`` pairs each document id with its set of entity name keys.
    Repeatedly place the still-unplaced document whose key set overlaps
    the union of the *other* unplaced documents' keys the most (the most
    connective hub), breaking ties by the larger key set, then by the
    original order for full determinism. Returns the ids in that order.
    """
    unplaced: list[tuple[Hashable, set[str]]] = list(items)
    order: list[Hashable] = []
    while unplaced:
        best_index = 0
        best_score = (-1, -1)
        for index, (_, keys) in enumerate(unplaced):
            others: set[str] = set()
            for other_index, (_, other_keys) in enumerate(unplaced):
                if other_index != index:
                    others |= other_keys
            score = (len(keys & others), len(keys))
            if score > best_score:  # strict: first max wins -> stable ties
                best_score = score
                best_index = index
        order.append(unplaced.pop(best_index)[0])
    return order
