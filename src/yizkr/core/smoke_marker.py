"""The smoke-test marker item: its terms, and how its entity data is known.

`yizkr smoke-test` (`yizkr.smoke`) creates one item with these terms and
nothing else, to check item creation and the query-service round trip.
The deployment generation's admission (`core.write_recovery`) does not count
such an item as ingested, so an instance that holds only the vocabulary and
marker items, as a scratch reset leaves it, can establish a generation.

The marker is known by its whole content, never by its label alone: an item
with any other term, an alias, a sitelink or a statement is not one, so an
item that ingestion wrote cannot pass for it.
"""

from __future__ import annotations

from collections.abc import Mapping

SMOKE_LABEL = "Wikibase Suite smoke-test item"
SMOKE_DESCRIPTION = (
    "Created by `yizkr smoke-test` to verify item creation and the "
    "SPARQL round-trip. Safe to delete."
)

# Each is present in entity data read with it among the props, and empty
# on a marker item, served as an empty map or an empty list.
_EMPTY_ON_A_MARKER = ("aliases", "claims", "sitelinks")


def _only_english(terms: object, value: str) -> bool:
    """Whether `terms`, a language -> term map as Wikibase serves labels
    or descriptions, holds the English term `value` and no other."""
    return (
        isinstance(terms, Mapping)
        and set(terms) == {"en"}
        and isinstance(terms["en"], Mapping)
        and terms["en"].get("value") == value
    )


def is_smoke_marker(entity: Mapping) -> bool:
    """Whether `entity`, an entity's data as `wbgetentities` serves it in
    every language, is a smoke-test marker item: an item whose only label
    is the English `SMOKE_LABEL`, whose only description is the English
    `SMOKE_DESCRIPTION`, and which has no alias, sitelink or statement.

    Data read without its aliases, statements or sitelinks is not a
    marker, since it cannot show that they are empty. The entity's other
    keys, such as its id, page and revision, do not matter."""
    return (
        entity.get("type") == "item"
        and _only_english(entity.get("labels"), SMOKE_LABEL)
        and _only_english(entity.get("descriptions"), SMOKE_DESCRIPTION)
        and all(
            key in entity and not entity[key] for key in _EMPTY_ON_A_MARKER
        )
    )
