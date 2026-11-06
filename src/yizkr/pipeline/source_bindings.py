"""Authoritative, unique current media bindings for attributed writes."""

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yizkr.core.wiki import WikiClient


def unique_source_qids(
    client: "WikiClient", property_id: str, wanted: Iterable[str]
) -> dict[str, str]:
    # Unlike the registrar's compatibility index, provenance cannot choose the
    # earliest QID when duplicate source hashes make support ambiguous.
    wanted = set(wanted)
    candidates = {sha1: set() for sha1 in wanted}
    active = {sha1: set() for sha1 in wanted}
    for qid, entity in client.iter_entities("claims"):
        for statement in entity.get("claims", {}).get(property_id, []):
            value = (statement.get("mainsnak", {}).get("datavalue") or {}).get(
                "value"
            )
            if isinstance(value, str) and value.lower() in candidates:
                candidates[value.lower()].add(qid)
                if statement.get("rank") != "deprecated":
                    active[value.lower()].add(qid)
    if any(
        len(qids) != 1 or active[sha1] != qids
        for sha1, qids in candidates.items()
    ):
        raise ValueError("source binding is missing or ambiguous")
    return {sha1: next(iter(qids)) for sha1, qids in candidates.items()}
