"""Live check of the vocabulary read bounded (#507).

`WikiClient.vocabulary`, which the writer, resolve, the backup and the
site read through a fresh client, takes the ids the bootstrap's
`Project:Model mapping` page gives the manifest's labels and checks their
labels, 50 ids a request, with no enumeration of the instance. It reads
every label whole when the page is missing or disagrees. Each read gives
the vocabulary the whole read gives.

The bootstrap refreshes the real mapping page; it changes nothing on an
instance that holds the manifest. The pages for the fallback are the
session's own: their titles carry a marker, eight hex digits of a hash
over the fixture, over the wiki client's and bootstrap's source and over
eight random bytes drawn once a session (conftest.fixture_mark). One is
never written; the other is a mapping page whose `human` row names
another vocabulary item's id.
"""

from __future__ import annotations

import pytest
import yaml

from yizkr.core import wiki
from yizkr.core.wiki import WikiClient, _vocabulary_of
from yizkr.model import bootstrap
from yizkr.model.bootstrap import bootstrap_instance, build_mapping_wikitext
from yizkr.model.schema import (
    DEFAULT_MANIFEST,
    load_manifest,
    read_manifest_data,
)

from .conftest import fixture_mark

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new pages
FIXTURE = (
    "#507: a mapping page the instance lacks, and one whose human row "
    "names another vocabulary item"
)
MARK = f"(vocabulary test {fixture_mark(FIXTURE, wiki, bootstrap)})"


class Recording(WikiClient):
    """A fresh wiki client that records its API requests and its whole
    reads of the instance."""

    def __init__(self, config, manifest_source=DEFAULT_MANIFEST) -> None:
        super().__init__(config, manifest_source)
        self.asked: list[dict] = []
        self.whole_reads = 0

    def api_get(self, params: dict) -> dict:
        self.asked.append(dict(params))
        return super().api_get(params)

    def snapshot(self) -> dict[str, dict]:
        self.whole_reads += 1
        return super().snapshot()


def whole_vocabulary(config) -> tuple[dict[str, str], dict[str, str]]:
    """The vocabulary as every label of the instance gives it."""
    raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
    properties, items, missing = _vocabulary_of(
        raw, *WikiClient(config).label_index()
    )
    assert not missing
    return properties, items


def manifest_naming(tmp_path, title: str):
    """The manifest with `title` as its mapping page, as a file."""
    raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
    raw["mapping_page"] = title
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    return path


@pytest.fixture(scope="module")
def bootstrapped(client: WikiClient, config):
    bootstrap_instance(client, load_manifest(), log=lambda *_: None)
    return whole_vocabulary(config)


def test_a_fresh_client_reads_the_vocabulary_bounded(config, bootstrapped):
    reader = Recording(config)

    assert reader.vocabulary() == bootstrapped
    assert reader.whole_reads == 0
    assert not any("list" in params for params in reader.asked)
    asked = [
        params
        for params in reader.asked
        if params.get("action") == "wbgetentities"
    ]
    size = len(bootstrapped[0]) + len(bootstrapped[1])
    assert len(asked) == -(-size // 50)


def test_without_its_page_the_vocabulary_reads_whole(
    client, config, bootstrapped, tmp_path
):
    title = f"Project:Model mapping {MARK} never written"
    assert client.page_text(title) is None
    reader = Recording(config, manifest_naming(tmp_path, title))

    assert reader.vocabulary() == bootstrapped
    assert reader.whole_reads == 1


def test_a_page_that_disagrees_reads_whole(
    client, config, bootstrapped, tmp_path
):
    properties, items = bootstrapped
    # the human row names the id of another vocabulary item
    swapped = {**items, "human": items["place"], "place": items["human"]}
    title = f"Project:Model mapping {MARK} disagreeing"
    client.edit_page(
        title,
        build_mapping_wikitext(load_manifest(), properties, swapped),
        summary="live test: a mapping page that disagrees",
    )
    reader = Recording(config, manifest_naming(tmp_path, title))

    assert reader.vocabulary() == bootstrapped
    assert reader.whole_reads == 1
