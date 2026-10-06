"""Two items under one label never break a command (#94).

Two people of one name are two items under one English label. The wiki
client's label index gives such a label its numerically earliest item
and keeps every holder (`ItemLabels`); only a lookup whose label must
name one item alone fails on it: a manifest vocabulary item (the
vocabulary, the bootstrap) and a process item such as a run item (the
writer's, the place-hierarchy pass's). Bootstrap, write, resolve, backup
and the site keep working beside two same-labelled people.

The homonyms are fictional (docs/EXAMPLES.md): Eugen Löbig the master
hatter (1836-1909), and a son of the same name, a pair this test makes
up for the homonym case.
"""

from __future__ import annotations

import pytest
import yaml

from tests.unit.test_mention_records import FakeBootstrapWiki
from tests.unit.test_pipeline import MEDIA_SHA1, RUN
from tests.unit.test_pipeline import FakeWiki as WriterWiki
from tests.unit.test_places import RunItemWiki
from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.wiki import ItemLabels, WikiClient
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import (
    DEFAULT_MANIFEST,
    load_manifest,
    read_manifest_data,
)
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.writer import BatchWriter, _run_terms

EUGEN = "Eugen Löbig"
ARTIFACT = {"kind": "artifact", "sha1": MEDIA_SHA1}


def config() -> Config:
    return Config(
        wikibase_url="http://wiki.invalid",
        concept_uri="http://wiki.invalid/entity/",
        admin_name="",
        admin_pass="",
        wdqs_url="http://query.invalid",
        sparql_url="http://query.invalid/sparql",
        env_file=None,
    )


def manifest_snapshot() -> dict[str, dict]:
    """The manifest's vocabulary as a bootstrapped instance holds it,
    numbered in manifest order, as the site's and backup's tests number
    it."""
    raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
    snapshot = {
        f"P{n}": {"label": spec["label"], "datatype": spec.get("datatype")}
        for n, spec in enumerate(raw["properties"].values(), 1)
    }
    snapshot.update(
        {
            f"Q{n}": {"label": spec["label"], "datatype": None}
            for n, spec in enumerate(raw["items"].values(), 1)
        }
    )
    return snapshot


def with_homonyms(snapshot: dict[str, dict]) -> dict[str, dict]:
    """`snapshot` with two people labelled Eugen Löbig, the later item
    (the son) listed first, as the instance's page order may give them."""
    return {
        "Q902": {"label": EUGEN, "datatype": None},
        **snapshot,
        "Q901": {"label": EUGEN, "datatype": None},
    }


def a_client(snapshot: dict[str, dict]) -> WikiClient:
    client = WikiClient(config())
    client.snapshot = lambda: dict(snapshot)
    return client


# -- the label index ----------------------------------------------------------


class TestTheLabelIndex:
    def test_two_items_under_one_label_name_the_earliest(self):
        _, items = a_client(with_homonyms({})).label_index()

        assert items[EUGEN] == "Q901"
        assert items.holders(EUGEN) == ("Q901", "Q902")
        assert sorted(items.pairs()) == [(EUGEN, "Q901"), (EUGEN, "Q902")]

    def test_the_earliest_is_by_number_not_by_text(self):
        """Q1000 is later than Q999, though it sorts first as text."""
        snapshot = {
            "Q1000": {"label": EUGEN, "datatype": None},
            "Q999": {"label": EUGEN, "datatype": None},
        }
        _, items = a_client(snapshot).label_index()

        assert items[EUGEN] == "Q999"
        assert items.holders(EUGEN) == ("Q999", "Q1000")

    def test_a_label_one_item_holds_has_no_holders_beside_it(self):
        _, items = a_client(manifest_snapshot()).label_index()

        assert items.shared == {}
        assert items.holders("no such label") == ()
        assert items.sole("no such label") is None

    def test_sole_fails_on_a_shared_label_and_names_both(self):
        _, items = a_client(with_homonyms({})).label_index()

        with pytest.raises(RuntimeError, match="Q901, Q902"):
            items.sole(EUGEN)

    def test_two_properties_under_one_label_still_fail(self):
        snapshot = {
            "P1": {"label": "date of birth", "datatype": "time"},
            "P2": {"label": "date of birth", "datatype": "time"},
        }
        with pytest.raises(RuntimeError, match="Two properties"):
            a_client(snapshot).label_index()

    def test_a_given_snapshot_is_indexed_alike(self):
        client = a_client({})
        _, items = client.label_index(with_homonyms({}))

        assert isinstance(items, ItemLabels)
        assert items.holders(EUGEN) == ("Q901", "Q902")

    def test_a_plain_mapping_is_taken_as_one_item_a_label(self):
        labels = ItemLabels.of({EUGEN: "Q901"})

        assert labels.sole(EUGEN) == "Q901"
        assert ItemLabels.of(labels) is labels

    def test_find_item_by_label_wants_one_item(self):
        client = a_client(
            with_homonyms({"Q5": {"label": "x", "datatype": None}})
        )

        assert client.find_item_by_label("x") == "Q5"
        assert client.find_item_by_label("no such label") is None
        with pytest.raises(RuntimeError, match="share the English label"):
            client.find_item_by_label(EUGEN)


# -- the vocabulary -----------------------------------------------------------


class TestTheVocabulary:
    def test_two_same_labelled_people_leave_it_whole(self):
        client = a_client(with_homonyms(manifest_snapshot()))

        properties, items = client.vocabulary()

        raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
        assert len(properties) == len(raw["properties"])
        assert len(items) == len(raw["items"])
        assert items["human"] == f"Q{list(raw['items']).index('human') + 1}"

    def test_answered_from_labels_read_with_homonyms(self):
        client = a_client(with_homonyms(manifest_snapshot()))
        reads: list[str] = []
        snapshot = client.snapshot

        def counted():
            reads.append("snapshot")
            return snapshot()

        client.snapshot = counted

        client.label_index()
        client.vocabulary()

        assert reads == ["snapshot"]

    @pytest.mark.parametrize("labels_read_first", [False, True])
    def test_a_vocabulary_label_two_items_hold_fails(self, labels_read_first):
        snapshot = manifest_snapshot()
        human = snapshot["Q1"]["label"]
        snapshot["Q950"] = {"label": human, "datatype": None}
        client = a_client(snapshot)
        if labels_read_first:
            client.label_index()

        with pytest.raises(RuntimeError, match=f"{human!r}"):
            client.vocabulary()


# -- the bootstrap ------------------------------------------------------------


class TestTheBootstrap:
    def test_it_runs_again_beside_two_same_labelled_people(self):
        manifest = load_manifest()
        wiki = FakeBootstrapWiki()
        first = bootstrap_instance(wiki, manifest, log=lambda *_: None)
        for _ in range(2):
            person = wiki.wbi.item.new()
            person.labels.set("en", EUGEN)
            person.write()

        again = bootstrap_instance(wiki, manifest, log=lambda *_: None)

        assert again["created"] == 0 and again["updated"] == 0
        assert again["unchanged"] == first["created"]

    def test_a_manifest_label_two_items_hold_fails(self):
        manifest = load_manifest()
        wiki = FakeBootstrapWiki()
        bootstrap_instance(wiki, manifest, log=lambda *_: None)
        label = manifest.items["human"].label
        twin = wiki.wbi.item.new()
        twin.labels.set("en", label)
        twin.write()
        writes = wiki.writes

        with pytest.raises(RuntimeError, match=f"{label!r}"):
            bootstrap_instance(wiki, manifest, log=lambda *_: None)
        assert wiki.writes == writes


# -- the writer and resolve ---------------------------------------------------


class HomonymWiki(WriterWiki):
    """The writer tests' in-memory wiki, its label index the wiki client's
    own over the items it holds, with the statements of the items asked
    for, as resolve reads them."""

    def snapshot(self) -> dict[str, dict]:
        return {
            qid: {"label": str(item.labels.get("en") or "") or None}
            for qid, item in self.wbi.item.store.items()
        }

    def label_index(self, snapshot=None):
        return WikiClient.label_index(self, snapshot or self.snapshot())

    def find_item_by_label(self, label):
        return WikiClient.find_item_by_label(self, label)

    def fetch_claims(self, ids):
        store = self.wbi.item.store
        return {
            qid: store[qid].get_json().get("claims") or {}
            for qid in ids
            if qid in store
        }

    def labelled(self, label: str) -> str:
        """Make an item under `label`; returns its QID."""
        item = self.wbi.item.new()
        item.labels.set("en", label)
        return item.write().id

    def homonyms(self) -> tuple[str, str]:
        """Two people labelled Eugen Löbig, father then son, the son listed
        first, as the instance's page order may give them."""
        father, son = self.labelled(EUGEN), self.labelled(EUGEN)
        store = self.wbi.item.store
        store[son] = store.pop(son)
        store[father] = store.pop(father)
        return father, son


def eugen_batch() -> ClaimBatch:
    """Eugen Löbig, the master hatter, born in 1836 (the 2008 family
    history)."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "eugen",
                        "label": EUGEN,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": {"artifact": ARTIFACT},
                }
            ],
        }
    )


class TestTheWriterAndResolve:
    def test_a_write_finds_the_earliest_and_mints_no_third(self):
        wiki = HomonymWiki({MEDIA_SHA1: "Q23"})
        father, son = wiki.homonyms()

        result = BatchWriter(wiki).write_batch(eugen_batch())

        assert result.written == 1
        assert result.created_items["eugen"] == father
        people = [
            qid
            for qid, item in wiki.wbi.item.store.items()
            if str(item.labels.get("en") or "") == EUGEN
        ]
        assert sorted(people) == sorted([father, son])
        # a second write finds the same item and adds nothing
        again = BatchWriter(wiki).write_batch(eugen_batch())
        assert again.created_items["eugen"] == father
        assert again.written == 0

    def test_resolve_names_the_item_the_writer_finds(self):
        wiki = HomonymWiki({MEDIA_SHA1: "Q23"})
        father, _ = wiki.homonyms()
        batch = eugen_batch()

        found = resolvable_labels(wiki, batch)

        assert found == {EUGEN: father}
        resolved = resolve_batch(batch, found)
        assert resolved.claims[0].subject.qid == father

    def test_a_run_label_two_items_hold_fails_before_any_claim(self):
        wiki = HomonymWiki({MEDIA_SHA1: "Q23"})
        label, _ = _run_terms(eugen_batch())
        wiki.labelled(label)
        wiki.labelled(label)
        writes = wiki.wbi.item.writes

        with pytest.raises(RuntimeError, match="share the English label"):
            BatchWriter(wiki).write_batch(eugen_batch())
        assert wiki.wbi.item.writes == writes


# -- the place-hierarchy pass's run item --------------------------------------


class PlacesWiki(RunItemWiki):
    """The places tests' in-memory wiki, finding an item by its label as
    the wiki client does."""

    def label_index(self, snapshot=None):
        return WikiClient.label_index(
            self,
            snapshot
            or {
                qid: {"label": entity["labels"].get("en", {}).get("value")}
                for qid, entity in self.entities.items()
            },
        )

    def find_item_by_label(self, label):
        return WikiClient.find_item_by_label(self, label)

    def labelled(self, label: str) -> str:
        item = self.wbi.item.new()
        item.labels.set("en", label)
        return item.write().id


class TestThePlacesRunItem:
    def test_two_same_labelled_people_leave_it_found(self):
        wiki = PlacesWiki()
        first = ensure_places_run_item(wiki, "0a9afd5746", "fake-model")
        wiki.labelled(EUGEN)
        wiki.labelled(EUGEN)

        assert ensure_places_run_item(wiki, "0a9afd5746", "fake-model") == (
            first
        )

    def test_a_run_label_two_items_hold_fails(self):
        wiki = PlacesWiki()
        label = "place-hierarchy run fake-model 0a9afd5746"
        wiki.labelled(label)
        wiki.labelled(label)
        writes = wiki.writes

        with pytest.raises(RuntimeError, match="share the English label"):
            ensure_places_run_item(wiki, "0a9afd5746", "fake-model")
        assert wiki.writes == writes


# -- backup and the site ------------------------------------------------------


class TestBackupAndTheSite:
    """Backup and the site read the vocabulary of a fresh wiki client
    (WikiClient.vocabulary), which two same-labelled people leave whole."""

    @pytest.fixture
    def homonyms(self, monkeypatch):
        snapshot = with_homonyms(manifest_snapshot())
        monkeypatch.setattr(WikiClient, "snapshot", lambda self: snapshot)

    def test_the_backups_exclusion_report_reads_the_vocabulary(self, homonyms):
        from yizkr.backup import exclusion_report

        queries: list[str] = []

        def run(query: str):
            queries.append(query)
            return []

        report = exclusion_report(config(), run=run)

        assert queries
        assert report is not None

    def test_the_site_builds_both_people(self, homonyms, tmp_path):
        from yizkr.staticsite import SiteBuilder

        def run(query: str):
            if "GROUP_CONCAT(DISTINCT ?alias" in query:
                return [
                    {
                        "person": {
                            "type": "uri",
                            "value": f"http://wiki.invalid/entity/{qid}",
                        },
                        "personLabel": {"type": "literal", "value": EUGEN},
                        "deceased": {"type": "literal", "value": "true"},
                        "aliases": {"type": "literal", "value": ""},
                    }
                    for qid in ("Q901", "Q902")
                ]
            return []

        builder = SiteBuilder(config(), tmp_path / "site", run=run)
        builder.build()

        for qid in ("Q901", "Q902"):
            page = tmp_path / "site" / "people" / f"{qid}.html"
            assert EUGEN in page.read_text()


class TestTheWritersTitleIndex:
    """The writer flags every artifact registered under a media object's
    label (_registered_under), so its index of titles holds every item
    under a shared label, not only the earliest."""

    def test_every_holder_of_a_shared_label_is_indexed(self):
        from yizkr.pipeline.writer import _FoundLabels

        title = "Gravestone of Eugen and Cäcilie Löbig"

        class Labels:
            def label_index(self):
                return {}, ItemLabels(
                    {title: "Q4", f"{title} [0123abcd]": "Q7"},
                    {title: ("Q4", "Q9")},
                )

        found = _FoundLabels(Labels())

        assert found.titled(title) == [
            (title, "Q4"),
            (f"{title} [0123abcd]", "Q7"),
            (title, "Q9"),
        ]
        assert found.get(title) == "Q4"
        with pytest.raises(RuntimeError, match="Q4, Q9"):
            found.sole(title)
        found.made("a new label", "Q12")
        assert found.sole("a new label") == "Q12"
