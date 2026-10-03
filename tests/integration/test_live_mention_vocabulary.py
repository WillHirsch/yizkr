"""Live: the bootstrap creates ADR-0035's and ADR-0036's vocabulary, and a
second bootstrap changes nothing (#406).

On a fresh instance the first bootstrap creates it all. On one bootstrapped
before, it creates what is new and appends each new subclass statement,
leaving any earlier one (statements coexist, ADR-0002): a `marriage` that
was a subclass of `event` gains `partnership` beside it until the scratch
reset gives an empty graph (ADR-0025).
"""

from __future__ import annotations

import pytest

from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest

pytestmark = pytest.mark.live

NEW_PROPERTIES = {
    "object_has_role": "wikibase-item",
    "kinship_class": "wikibase-item",
    "side": "wikibase-item",
    "degree": "quantity",
    "removes": "quantity",
    "issued_by": "wikibase-item",
    "carried_out_by": "wikibase-item",
    "civil_status": "wikibase-item",
    "judgement": "wikibase-item",
    "judgement_note": "string",
    "started_by": "wikibase-item",
    "ended_by": "wikibase-item",
}
NEW_ITEMS = (
    "partnership",
    "wedding",
    "civil_wedding",
    "religious_wedding",
    "divorce",
    "annulment",
    "principal",
    "parent",
    "partner",
    "spouse",
    "informant",
    "witness",
    "officiant",
    "kin_parent",
    "kin_cousin",
    "kin_relative",
    "paternal",
    "maternal",
    "unmarried",
    "married",
    "widowed",
    "divorced",
    "judgement_kind",
    "judgement_relation",
    "judgement_date_reading",
    "judgement_identity",
    "judgement_other",
)
SUBCLASSES = {
    "marriage": "partnership",
    "wedding": "event",
    "civil_wedding": "wedding",
    "religious_wedding": "wedding",
    "divorce": "event",
    "annulment": "event",
}


def superclasses(client, qid: str, subclass_pid: str) -> set[str]:
    entity = client.wbi.item.get(qid)
    return {
        (claim.mainsnak.datavalue or {}).get("value", {}).get("id")
        for claim in entity.claims.get(subclass_pid) or []
    }


def mapping_page(client, title: str) -> tuple[int, str]:
    """The mapping page's latest revision id and its text."""
    data = client.api_get(
        {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "ids|content",
            "rvslots": "main",
            "formatversion": "2",
        }
    )
    (revision,) = data["query"]["pages"][0]["revisions"]
    return revision["revid"], revision["slots"]["main"]["content"]


def test_bootstrap_creates_the_new_vocabulary_once(client):
    manifest = load_manifest()
    bootstrap_instance(client, manifest, log=lambda *_: None)
    client.forget_vocabulary()  # read the instance afresh
    properties, items = client.vocabulary()
    snapshot = client.fetch_entities(
        [properties[key] for key in NEW_PROPERTIES]
    )
    for key, datatype in NEW_PROPERTIES.items():
        assert snapshot[properties[key]]["datatype"] == datatype, key
    for key in NEW_ITEMS:
        assert items[key].startswith("Q"), key
    # the retired property stays in the instance
    assert properties["spouse"].startswith("P")
    for key, parent in SUBCLASSES.items():
        assert items[parent] in superclasses(
            client, items[key], properties["subclass_of"]
        ), key

    page = mapping_page(client, manifest.mapping_page)
    for key in NEW_ITEMS:
        assert f"[[Item:{items[key]}|" in page[1], key

    second = bootstrap_instance(client, manifest, log=lambda *_: None)
    # the same text again makes no new revision
    assert mapping_page(client, manifest.mapping_page) == page
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["statements_added"] == 0
