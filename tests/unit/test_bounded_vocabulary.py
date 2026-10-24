"""The vocabulary read bounded, from the bootstrap's mapping page (#507).

`WikiClient.vocabulary` takes the ids the `Project:Model mapping` page
gives the manifest's labels and checks each id's English label with one
`wbgetentities` request per 50 ids, so its cost grows with the
vocabulary, not with the instance. When the page is missing, lacks a
manifest label, names one twice, or any id is missing, a redirect or
carries another label, and when the read fails in any way, it reads every
label whole, as it does without a page.

The fake wiki numbers the manifest's vocabulary in manifest order, as the
homonym and backup tests do, and the instance's other items are fictional
(docs/EXAMPLES.md).
"""

from __future__ import annotations

import pytest
import requests
import yaml

from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient
from yizkr.model.bootstrap import build_mapping_wikitext, mapping_rows
from yizkr.model.schema import (
    DEFAULT_MANIFEST,
    load_manifest,
    read_manifest_data,
)

RAW = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
MANIFEST = load_manifest()
PROPERTY_IDS = {key: f"P{n}" for n, key in enumerate(RAW["properties"], 1)}
ITEM_IDS = {key: f"Q{n}" for n, key in enumerate(RAW["items"], 1)}
# what the vocabulary is on the fake instance, however it is read
VOCABULARY = (PROPERTY_IDS, ITEM_IDS)
PAGE = build_mapping_wikitext(MANIFEST, PROPERTY_IDS, ITEM_IDS)
# items of the instance beside the vocabulary
PEOPLE = {"Q901": "Eugen Löbig", "Q902": "Rahel Löbig"}


def config() -> Config:
    return Config(
        wikibase_url="http://wiki.invalid",
        concept_uri="http://wiki.invalid/entity/",
        admin_name="",
        admin_pass="",
        wdqs_url=None,
        sparql_url=None,
        env_file=None,
    )


def instance_labels() -> dict[str, str]:
    """id -> English label of every entity of the fake instance."""
    labels = {
        PROPERTY_IDS[key]: spec["label"]
        for key, spec in RAW["properties"].items()
    }
    labels.update(
        {ITEM_IDS[key]: spec["label"] for key, spec in RAW["items"].items()}
    )
    labels.update(PEOPLE)
    return labels


class FakeWiki(WikiClient):
    """The wiki client over an in-memory instance: its pages' latest text,
    `wbgetentities` for labels, and its whole snapshot, which the full
    read takes. It records every request and every whole read, and fails
    on a request it does not know."""

    def __init__(self, page: str | None = PAGE) -> None:
        super().__init__(config())
        self.labels = instance_labels()
        self.pages = {} if page is None else {RAW["mapping_page"]: page}
        # id -> the item a merge made it a redirect to
        self.redirects: dict[str, str] = {}
        self.requests: list[dict] = []
        self.whole_reads = 0

    def api_get(self, params: dict) -> dict:
        self.requests.append(dict(params))
        if params.get("prop") == "revisions":
            title = params["titles"]
            if title not in self.pages:
                return {
                    "query": {"pages": [{"title": title, "missing": True}]}
                }
            content = {"slots": {"main": {"content": self.pages[title]}}}
            return {
                "query": {"pages": [{"title": title, "revisions": [content]}]}
            }
        if params.get("action") == "wbgetentities":
            return {
                "entities": {
                    entity_id: self.entity(entity_id)
                    for entity_id in params["ids"].split("|")
                }
            }
        raise AssertionError(f"a request the fake does not know: {params}")

    def entity(self, entity_id: str) -> dict:
        if entity_id in self.redirects:
            target = self.redirects[entity_id]
            return {
                "id": target,
                "redirects": {"from": entity_id, "to": target},
                "labels": {"en": {"value": self.labels[target]}},
            }
        if entity_id not in self.labels:
            return {"id": entity_id, "missing": ""}
        return {
            "id": entity_id,
            "labels": {
                "en": {"language": "en", "value": self.labels[entity_id]}
            },
        }

    def snapshot(self) -> dict[str, dict]:
        self.whole_reads += 1
        return {
            entity_id: {"label": label, "datatype": None}
            for entity_id, label in self.labels.items()
            if entity_id not in self.redirects
        }

    def label_requests(self) -> list[dict]:
        return [
            params
            for params in self.requests
            if params.get("action") == "wbgetentities"
        ]


def page_without(label: str) -> str:
    """The mapping page with the row of `label` left out."""
    return "\n".join(
        line for line in PAGE.splitlines() if f"|| {label} ||" not in line
    )


# -- the bounded read ---------------------------------------------------------


class TestTheBoundedRead:
    def test_the_page_answers_with_no_whole_read(self):
        wiki = FakeWiki()

        assert wiki.vocabulary() == VOCABULARY
        assert wiki.whole_reads == 0
        assert not any("list" in params for params in wiki.requests)

    def test_it_asks_the_labels_of_the_vocabulary_alone_fifty_a_request(self):
        wiki = FakeWiki()

        wiki.vocabulary()

        asked = wiki.label_requests()
        ids = [i for params in asked for i in params["ids"].split("|")]
        vocabulary_ids = {*PROPERTY_IDS.values(), *ITEM_IDS.values()}
        assert sorted(ids) == sorted(vocabulary_ids)
        assert len(asked) == -(-len(vocabulary_ids) // 50)
        assert all(len(params["ids"].split("|")) <= 50 for params in asked)
        assert {params["props"] for params in asked} == {"labels"}
        assert {params["languages"] for params in asked} == {"en"}

    def test_it_reads_the_manifests_mapping_page(self):
        wiki = FakeWiki()

        wiki.vocabulary()

        (page_read,) = [p for p in wiki.requests if p.get("prop")]
        assert page_read["titles"] == RAW["mapping_page"]
        assert page_read["rvprop"] == "content"
        assert page_read["rvslots"] == "main"
        assert page_read["formatversion"] == "2"

    def test_the_vocabulary_read_bounded_is_kept(self):
        wiki = FakeWiki()
        first = wiki.vocabulary()
        asked = len(wiki.requests)

        assert wiki.vocabulary() is first
        assert len(wiki.requests) == asked

    def test_a_forgotten_vocabulary_reads_the_page_again(self):
        wiki = FakeWiki()
        wiki.vocabulary()
        wiki.forget_vocabulary()
        wiki.requests.clear()

        assert wiki.vocabulary() == VOCABULARY
        assert any(params.get("prop") for params in wiki.requests)
        assert wiki.whole_reads == 0

    def test_labels_read_whole_answer_before_the_page(self):
        wiki = FakeWiki()
        wiki.label_index()
        wiki.requests.clear()

        assert wiki.vocabulary() == VOCABULARY
        assert wiki.requests == []
        assert wiki.whole_reads == 1

    def test_the_bounded_read_leaves_the_label_lookups_whole(self):
        """find_item_by_label reads the label index itself: the bounded
        read gives it no labels, so a run item's label is still checked
        against every item."""
        wiki = FakeWiki()
        wiki.vocabulary()

        assert wiki.find_item_by_label(PEOPLE["Q901"]) == "Q901"
        assert wiki.whole_reads == 1


# -- the fallback to the whole read -------------------------------------------


class TestTheFallback:
    def assert_read_whole(self, wiki: FakeWiki) -> None:
        assert wiki.vocabulary() == VOCABULARY
        assert wiki.whole_reads == 1

    def test_no_page(self):
        wiki = FakeWiki(page=None)

        self.assert_read_whole(wiki)
        assert wiki.label_requests() == []

    def test_a_page_without_a_manifest_property(self):
        label = RAW["properties"]["date_of_birth"]["label"]

        self.assert_read_whole(FakeWiki(page=page_without(label)))

    def test_a_page_without_a_manifest_item(self):
        label = RAW["items"]["human"]["label"]

        self.assert_read_whole(FakeWiki(page=page_without(label)))

    def test_a_page_that_names_a_label_twice(self):
        """Two rows give the human label two ids, and both items carry it:
        the page cannot say which is the vocabulary's, and the whole read
        refuses the label two items hold."""
        label = RAW["items"]["human"]["label"]
        twice = PAGE.replace(
            "|}\n",
            f"|-\n| [[Item:Q901|Q901]] || {label} || — || —\n|}}\n",
            1,
        )
        assert list(mapping_rows(twice)).count(("Q901", label)) == 1
        wiki = FakeWiki(page=twice)
        wiki.labels["Q901"] = label

        with pytest.raises(RuntimeError, match=f"{label!r}"):
            wiki.vocabulary()
        assert wiki.whole_reads == 1

    def test_a_page_that_names_an_items_id_under_another_label(self):
        """The human row names the place item's id: both labels name one
        id, and the page cannot say which item is the vocabulary's."""
        human, place = ITEM_IDS["human"], ITEM_IDS["place"]
        page = PAGE.replace(
            f"| [[Item:{human}|{human}]] ||", f"| [[Item:{place}|{place}]] ||"
        )
        assert page != PAGE

        self.assert_read_whole(FakeWiki(page=page))

    def test_a_page_that_names_a_propertys_id_under_another_label(self):
        """The date of birth row names the date of death property's id, so
        a birth must never be written as a death."""
        born, died = (
            PROPERTY_IDS["date_of_birth"],
            PROPERTY_IDS["date_of_death"],
        )
        page = PAGE.replace(
            f"| [[Property:{born}|{born}]] ||",
            f"| [[Property:{died}|{died}]] ||",
        )
        assert page != PAGE

        self.assert_read_whole(FakeWiki(page=page))

    def test_an_answer_that_leaves_out_an_id(self):
        class Forgetful(FakeWiki):
            def api_get(self, params):
                data = super().api_get(params)
                data.get("entities", {}).pop(ITEM_IDS["human"], None)
                return data

        self.assert_read_whole(Forgetful())

    def test_an_id_that_carries_another_label(self):
        """A vocabulary item relabelled on the wiki and in the manifest
        together, the page not yet refreshed."""
        wiki = FakeWiki()
        human = ITEM_IDS["human"]
        wiki.labels[human] = "Löbig family member"

        with pytest.raises(RuntimeError, match="missing manifest vocabulary"):
            wiki.vocabulary()
        assert wiki.whole_reads == 1

    def test_a_property_that_carries_another_label(self):
        wiki = FakeWiki()
        born = PROPERTY_IDS["date_of_birth"]
        moved = "P900"
        wiki.labels[moved] = wiki.labels.pop(born)
        wiki.labels[born] = "date of birth (retired)"

        assert wiki.vocabulary() == (
            {**PROPERTY_IDS, "date_of_birth": moved},
            ITEM_IDS,
        )
        assert wiki.whole_reads == 1

    def test_a_missing_id(self):
        wiki = FakeWiki()
        human = ITEM_IDS["human"]
        label = wiki.labels.pop(human)
        wiki.labels["Q903"] = label

        assert wiki.vocabulary() == (
            PROPERTY_IDS,
            {**ITEM_IDS, "human": "Q903"},
        )
        assert wiki.whole_reads == 1

    def test_a_redirect(self):
        """A vocabulary item merged into another item that took its
        label: the id resolves to the target, which is not on the page."""
        wiki = FakeWiki()
        human = ITEM_IDS["human"]
        wiki.labels["Q903"] = wiki.labels[human]
        wiki.redirects[human] = "Q903"

        assert wiki.vocabulary() == (
            PROPERTY_IDS,
            {**ITEM_IDS, "human": "Q903"},
        )
        assert wiki.whole_reads == 1

    def test_a_label_check_stops_at_the_first_disagreement(self):
        wiki = FakeWiki()
        wiki.labels[PROPERTY_IDS["instance_of"]] = "kind of thing"
        wiki.labels["P900"] = RAW["properties"]["instance_of"]["label"]

        wiki.vocabulary()

        assert len(wiki.label_requests()) == 1

    def test_a_page_read_that_fails(self):
        class Unreachable(FakeWiki):
            def api_get(self, params):
                raise requests.ConnectionError("connection refused")

        self.assert_read_whole(Unreachable())

    def test_an_api_error(self):
        class Refusing(FakeWiki):
            def api_get(self, params):
                if params.get("action") == "wbgetentities":
                    raise RuntimeError("MediaWiki API error: maxlag")
                return super().api_get(params)

        self.assert_read_whole(Refusing())

    def test_a_fake_that_passes_every_request_on(self):
        """A fake whose api_get knows only other requests and fails on
        these, as the writer's and registrar's fakes may: the whole read
        answers."""

        class Elsewhere(FakeWiki):
            def api_get(self, params):
                raise AssertionError(f"unexpected request {params}")

        self.assert_read_whole(Elsewhere())

    def test_an_answer_of_another_shape(self):
        class Odd(FakeWiki):
            def api_get(self, params):
                return {"batchcomplete": True}

        self.assert_read_whole(Odd())

    def test_a_vocabulary_the_instance_lacks_still_fails(self):
        wiki = FakeWiki(page=None)
        del wiki.labels[ITEM_IDS["human"]]

        with pytest.raises(RuntimeError, match="item 'human'"):
            wiki.vocabulary()


# -- the page -----------------------------------------------------------------


class TestThePage:
    def test_every_row_the_bootstrap_writes_is_read(self):
        rows = list(mapping_rows(PAGE))

        assert rows == sorted(
            rows, key=lambda row: (row[0][0] == "Q", int(row[0][1:]))
        )
        assert dict(rows) == {
            **{
                PROPERTY_IDS[key]: spec.label
                for key, spec in MANIFEST.properties.items()
            },
            **{
                ITEM_IDS[key]: spec.label
                for key, spec in MANIFEST.items.items()
            },
        }

    def test_a_line_in_no_rows_form_gives_nothing(self):
        assert list(mapping_rows("== Properties ==\n|-\n| P1 || x || y")) == []
        assert list(mapping_rows("| [[Item:P1|P1]] || instance of || x")) == []
        assert list(mapping_rows("| [[Property:Q1|Q1]] || human || x")) == []
        assert list(mapping_rows("| [[Item:Q1|Q2]] || human || x")) == []
        assert list(mapping_rows("| [[Property:P1|P2]] || x || y")) == []

    def test_page_text(self):
        wiki = FakeWiki()

        assert wiki.page_text(RAW["mapping_page"]) == PAGE
        assert wiki.page_text("Project:No such page") is None
