"""The smoke-test marker predicate on the item `yizkr smoke-test` writes
and on items ingestion writes."""

from __future__ import annotations

import pytest
from wikibaseintegrator import WikibaseIntegrator, wbi_login
from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.entities.item import ItemEntity
from wikibaseintegrator.wbi_config import config as wbi_config

from yizkr.core.config import Config
from yizkr.core.smoke_marker import is_smoke_marker
from yizkr.smoke import create_item

CONFIG = Config(
    wikibase_url="http://wiki.invalid",
    concept_uri="http://wiki.invalid/entity/",
    admin_name="",
    admin_pass="",
    wdqs_url=None,
    sparql_url=None,
    env_file=None,
)


@pytest.fixture
def written(monkeypatch) -> dict:
    """The entity data `yizkr.smoke.create_item` writes, as an instance
    serves it back: with its id, page and revision."""
    sent = []

    def write(self, **kwargs):
        sent.append(self.get_json())
        self.id = "Q7"
        return self

    for key in ("MEDIAWIKI_API_URL", "USER_AGENT"):
        monkeypatch.setitem(wbi_config, key, wbi_config[key])
    monkeypatch.setattr(wbi_login, "Clientlogin", lambda **kwargs: None)
    monkeypatch.setattr(ItemEntity, "write", write)
    assert create_item(CONFIG) == "Q7"
    (data,) = sent
    return {
        **data,
        "id": "Q7",
        "pageid": 7,
        "ns": 120,
        "title": "Item:Q7",
        "lastrevid": 9,
        "modified": "2026-11-23T09:00:00Z",
    }


def test_the_item_the_smoke_test_writes_is_the_marker(written):
    assert is_smoke_marker(written)


def test_the_marker_served_with_empty_lists_is_the_marker(written):
    """Wikibase may serve an empty map as an empty list."""
    assert is_smoke_marker(
        {**written, "aliases": [], "claims": [], "sitelinks": []}
    )


@pytest.mark.parametrize("key", ["aliases", "claims", "sitelinks"])
def test_the_marker_read_without_a_part_it_must_lack_is_not_shown(
    written, key
):
    """Data that does not show the item's aliases, statements or
    sitelinks cannot show that they are empty."""
    assert not is_smoke_marker(
        {name: value for name, value in written.items() if name != key}
    )


def test_a_fictional_artifact_item_is_not_the_marker():
    item = WikibaseIntegrator().item.new()
    item.labels.set("en", "Birth register extract for Gustav Löbig")
    item.descriptions.set("en", "Extract issued in 1938 for emigration papers")
    item.claims.add(Item(prop_nr="P1", value="Q3"))
    item.id = "Q120"

    assert not is_smoke_marker(item.get_json())
