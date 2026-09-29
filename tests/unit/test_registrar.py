"""Offline tests for registration by the SHA-1 of an artifact's media
(ADR-0010; #103).

Covered here: the lag-free enumeration the registrar reads the instance
with, the additive statement helper derived-media registration uses (#50),
and the registrar itself over an in-memory wiki whose uploads run the
real `WikiClient.upload_file` against a fake MediaWiki (#186): artifacts
found or made by SHA-1 alone, never by title; page fragments keyed by
their artifact (#187); a container's derived media registered from their
record (#128); and the never-ingest list (#29). The orchestrator's
write-time registration of a document runs here too, through its real
`_step_register`. The flow is also verified live
(tests/integration/test_live_write_time_registration.py,
test_live_registrar.py, test_live_pipeline.py and
test_live_presentation.py).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_no_wiki_before_write import (
    DECK,
    SHOP_FRONT,
    build_deck,
)
from tests.unit.test_no_wiki_before_write import PORTRAIT as STUDIO
from tests.unit.test_upload_warnings import FakeMediaWiki, FakeUploadClient
from yizkr.core.corpus import ExclusionList
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.presentation import (
    EmbeddedMedium,
    PresentationContent,
    Slide,
    record_derived_media,
)
from yizkr.pipeline.registrar import (
    ExcludedMedia,
    Registrar,
    _cites,
    _ensure_referenced,
    _item_claim,
    _string_claim,
    register_artifact,
    register_derived_media,
)


class FakeIndexClient(WikiClient):
    """A WikiClient whose entity enumeration is scripted, so
    string_statement_index can be exercised without an instance."""

    def __init__(self, entities: dict[str, dict]) -> None:
        self._entities = entities
        self.requests: list[dict] = []

    def list_entity_ids(self):
        return list(self._entities)

    def api_get(self, params):
        self.requests.append(params)
        ids = params["ids"].split("|")
        return {"entities": {qid: self._entities[qid] for qid in ids}}


def _with_hash(pid: str, value: str) -> dict:
    return {"claims": {pid: [{"mainsnak": {"datavalue": {"value": value}}}]}}


class TestStringStatementIndex:
    def test_maps_value_to_entity(self):
        client = FakeIndexClient(
            {
                "Q5": _with_hash("P42", "aaa"),
                "Q6": _with_hash("P42", "bbb"),
                "Q7": {"claims": {}},  # no hash: ignored
            }
        )
        index = client.string_statement_index("P42")
        assert index == {"aaa": "Q5", "bbb": "Q6"}

    def test_duplicate_value_resolves_to_numerically_earliest(self):
        # the very leak the hash key prevents may exist in legacy data;
        # it must resolve deterministically, not raise (unlike labels)
        client = FakeIndexClient(
            {
                "Q30": _with_hash("P42", "dup"),
                "Q4": _with_hash("P42", "dup"),
                "Q100": _with_hash("P42", "dup"),
            }
        )
        assert client.string_statement_index("P42") == {"dup": "Q4"}

    def test_non_string_and_missing_property_ignored(self):
        client = FakeIndexClient(
            {
                "Q5": {
                    "claims": {
                        "P42": [{"mainsnak": {"datavalue": {"value": 3}}}]
                    }
                },
                "Q6": {
                    "claims": {
                        "P99": [{"mainsnak": {"datavalue": {"value": "x"}}}]
                    }
                },
            }
        )
        assert client.string_statement_index("P42") == {}

    def test_empty_instance(self):
        assert FakeIndexClient({}).string_statement_index("P42") == {}


class TestIterEntities:
    def test_every_entity_fifty_a_request(self):
        entities = {f"Q{n}": {"claims": {}} for n in range(1, 121)}
        client = FakeIndexClient(entities)
        assert dict(client.iter_entities("labels|claims")) == entities
        assert [len(r["ids"].split("|")) for r in client.requests] == [
            50,
            50,
            20,
        ]
        assert {(r["props"], r["languages"]) for r in client.requests} == {
            ("labels|claims", "en")
        }


# -- additive statements (ADR-0002, ADR-0008; #50) ---------------------------

PART_OF, STATED_IN, PAGE, MEDIA_FILE = "P3", "P2", "P6", "P57"


def _entity():
    from wikibaseintegrator import WikibaseIntegrator

    return WikibaseIntegrator().item.new()


def _part_of(deck: str, slide: int):
    from wikibaseintegrator.datatypes import String

    claim = _item_claim(PART_OF, deck, STATED_IN, deck)
    claim.qualifiers.add(String(prop_nr=PAGE, value=str(slide)))
    return claim


def _media_file(value: str, source: str):
    return _string_claim(MEDIA_FILE, value, STATED_IN, source)


def _ensure(entity, claim, source: str) -> str:
    return _ensure_referenced(entity, claim, STATED_IN, source)


def _statements(entity, pid: str) -> list[dict]:
    return [claim.get_json() for claim in entity.claims.get(pid)]


class TestEnsureReferenced:
    def test_new_deck_appends_its_own_part_of_statement(self):
        entity = _entity()
        assert _ensure(entity, _part_of("Q10", 1), "Q10") == "added"
        before = _statements(entity, PART_OF)

        assert _ensure(entity, _part_of("Q20", 3), "Q20") == "added"

        first, second = entity.claims.get(PART_OF)
        # the first deck's statement is untouched: same value, qualifier
        # and references (never overwritten or re-sourced)
        assert first.get_json() == before[0]
        assert _cites(first, STATED_IN, "Q10")
        assert not _cites(first, STATED_IN, "Q20")
        assert second.mainsnak.datavalue["value"]["id"] == "Q20"
        assert second.qualifiers.get(PAGE)[0].datavalue["value"] == "3"
        assert _cites(second, STATED_IN, "Q20")

    def test_same_deck_other_slide_is_a_distinct_statement(self):
        entity = _entity()
        _ensure(entity, _part_of("Q10", 1), "Q10")
        assert _ensure(entity, _part_of("Q10", 2), "Q10") == "added"
        assert len(entity.claims.get(PART_OF)) == 2

    def test_rerun_is_a_no_op(self):
        entity = _entity()
        _ensure(entity, _part_of("Q10", 1), "Q10")
        before = _statements(entity, PART_OF)
        assert _ensure(entity, _part_of("Q10", 1), "Q10") == "present"
        assert _statements(entity, PART_OF) == before

    def test_agreeing_source_gains_a_reference_not_a_duplicate(self):
        entity = _entity()
        _ensure(entity, _media_file("File:Img.png", "Q10"), "Q10")

        outcome = _ensure(entity, _media_file("File:Img.png", "Q20"), "Q20")

        assert outcome == "referenced"
        (statement,) = entity.claims.get(MEDIA_FILE)
        # both sources cite the one statement, the first one still first
        first_ref = statement.references.references[0]
        assert first_ref.snaks.get(STATED_IN)[0].datavalue["value"]["id"] == (
            "Q10"
        )
        assert _cites(statement, STATED_IN, "Q20")
        # re-asserting from either source changes nothing
        for source in ("Q10", "Q20"):
            claim = _media_file("File:Img.png", source)
            assert _ensure(entity, claim, source) == "present"
        assert len(statement.references.references) == 2

    def test_different_value_coexists(self):
        entity = _entity()
        _ensure(entity, _media_file("File:A.png", "Q10"), "Q10")
        assert _ensure(entity, _media_file("File:B.png", "Q10"), "Q10") == (
            "added"
        )
        assert {
            claim.mainsnak.datavalue["value"]
            for claim in entity.claims.get(MEDIA_FILE)
        } == {"File:A.png", "File:B.png"}


# -- the in-memory wiki ------------------------------------------------------


class WriteRefused(RuntimeError):
    """The fake wiki refused a write, as a lost connection would."""


class FakeWiki:
    """In-memory stand-in for WikiClient: real WBI item objects over a
    JSON store that round-trips them as the API does, the lag-free
    enumeration registration reads the instance with, and the real
    `WikiClient.upload_file` over a fake MediaWiki upload API
    (tests/unit/test_upload_warnings.py).

    `refuse` decides, for each write's entity data, whether the write
    fails before anything is stored; `reads` counts the enumerations."""

    PROPERTIES = {
        "instance_of": "P1",
        "stated_in": "P2",
        "part_of": "P3",
        "page_number": "P6",
        "media_file": "P57",
        "media_sha1": "P58",
    }
    ITEMS = {
        "photograph": "Q2",
        "artifact": "Q3",
        "artifact_fragment": "Q4",
        "presentation": "Q5",
        "certificate": "Q6",
        "genealogical_report": "Q7",
    }

    def __init__(self) -> None:
        from wikibaseintegrator import WikibaseIntegrator
        from wikibaseintegrator.entities.item import ItemEntity

        wiki = self
        api = WikibaseIntegrator()

        class StoredItem(ItemEntity):
            def write(self, **_):
                return wiki.store(self)

        class Items:
            def new(self):
                return StoredItem(api=api)

            def get(self, qid):
                entity = copy.deepcopy(wiki.entities[qid])
                return StoredItem(api=api).from_json(entity)

        self.wbi = SimpleNamespace(item=Items())
        self.entities: dict[str, dict] = {}
        self.media = FakeMediaWiki()
        self.writes = 0
        self.reads = 0
        self.refuse = lambda data: False
        self._next_qid = 100
        self._next_claim = 0

    def store(self, entity):
        data = entity.get_json()
        if self.refuse(data):
            raise WriteRefused("the connection to the wiki was lost")
        qid = entity.id
        if qid is None:
            qid, self._next_qid = f"Q{self._next_qid}", self._next_qid + 1
        claims = {}
        for pid, statements in data.get("claims", {}).items():
            assert not any("remove" in s for s in statements), "a removal"
            for statement in statements:
                if "id" not in statement:
                    self._next_claim += 1
                    statement["id"] = f"{qid}$claim-{self._next_claim}"
                for reference in statement.get("references", []):
                    # the wiki hashes each reference block by content
                    snaks = json.dumps(reference["snaks"], sort_keys=True)
                    reference.setdefault(
                        "hash", hashlib.sha1(snaks.encode()).hexdigest()
                    )
            claims[pid] = statements
        self.entities[qid] = {
            **data,
            "id": qid,
            "type": "item",
            "lastrevid": self.writes + 1,
            "claims": claims,
        }
        self.writes += 1
        return self.wbi.item.get(qid)

    # -- the WikiClient surface registration uses --------------------------

    def vocabulary(self):
        return self.PROPERTIES, self.ITEMS

    def iter_entities(self, props):
        assert props == "labels|claims"
        self.reads += 1
        for qid in sorted(self.entities, key=lambda q: int(q[1:])):
            yield qid, copy.deepcopy(self.entities[qid])

    @property
    def files(self) -> dict[str, str]:
        """File page -> SHA-1 of the file's current bytes."""
        return {
            f"File:{name.replace('_', ' ')}": sha1
            for name, sha1 in self.media.current().items()
        }

    def upload_file(self, path, filename=None):
        # the real upload logic; whatever it does, no stored file may gain
        # a version and no stored bytes may gain a second file
        before = copy.deepcopy(self.media.versions)
        page = FakeUploadClient(self.media).upload_file(path, filename)
        held = {versions[-1] for versions in before.values()}
        for name, versions in self.media.versions.items():
            if name in before:
                assert versions == before[name], f"File:{name} overwritten"
            else:
                assert versions[-1] not in held, f"File:{name} duplicates"
        return page

    def api_get(self, params):
        assert params["prop"] == "imageinfo"
        current = self.media.current()
        pages = {}
        for n, title in enumerate(params["titles"].split("|")):
            name = FakeMediaWiki.normalise(title.removeprefix("File:"))
            if name in current:
                pages[str(n)] = {
                    "title": title,
                    "imageinfo": [{"sha1": current[name]}],
                }
            else:
                pages[str(-n - 1)] = {"title": title, "missing": ""}
        return {"query": {"pages": pages}}

    def drift(self) -> list[tuple[str, str]]:
        """(item, file page) for every `media file` whose current bytes
        are not the item's `media SHA-1`."""
        current = self.files
        drifted = []
        for qid, entity in self.entities.items():
            claims = entity["claims"]
            hashes = {
                s["mainsnak"]["datavalue"]["value"]
                for s in claims.get(self.PROPERTIES["media_sha1"], [])
            }
            for statement in claims.get(self.PROPERTIES["media_file"], []):
                page = statement["mainsnak"]["datavalue"]["value"]
                if hashes and current.get(page) not in hashes:
                    drifted.append((qid, page))
        return drifted

    # -- fixture setup -------------------------------------------------------

    def item(
        self, label: str, *statements: tuple, stated_in: str | None = None
    ) -> str:
        """Put an item on the wiki directly: `label`, and each statement
        a (property key, value, page qualifier or None) triple, where an
        item value is a QID and any other value a string, each stated in
        `stated_in`, by default the item itself. Returns its QID."""
        from wikibaseintegrator.datatypes import String

        entity = self.wbi.item.new()
        entity.labels.set("en", label)
        entity = entity.write()
        source = stated_in or entity.id
        cite = self.PROPERTIES["stated_in"]
        for key, value, page in statements:
            pid = self.PROPERTIES[key]
            make = _item_claim if value.startswith("Q") else _string_claim
            claim = make(pid, value, cite, source)
            if page is not None:
                claim.qualifiers.add(
                    String(prop_nr=self.PROPERTIES["page_number"], value=page)
                )
            _ensure_referenced(entity, claim, cite, source)
        return entity.write().id


class Untouchable:
    """A wiki client of which nothing may be asked: every attribute is
    refused, and recorded before it is."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __getattr__(self, name: str):
        self.asked.append(name)
        raise AssertionError(f"the wiki was asked for {name}")


# -- fictional media (docs/EXAMPLES.md) ---------------------------------------

# two different scans of the Löbig family's studio portrait of the 1890s
PORTRAIT = b"\x89PNG synthetic scan of the 1890s studio portrait"
RESCAN = b"\x89PNG synthetic re-scan of the 1890s studio portrait"
TITLE = "Studio portrait of the Löbig family"
# two copies of Gustav's birth register extract, each scanned into a
# folder of its own under the same file name: two artifacts, one title
EXTRACT = b"%PDF synthetic scan of Gustav's birth register extract"
SECOND_COPY = b"%PDF synthetic scan of the extract's second copy, 1938"
EXTRACT_TITLE = "birth-extract-1938"
# the family firm's deck, and the shop front it shows, as scanned and
# as retouched
SHOP = "Hutmacherei zum Blauen Hut"
IMAGE = b"\x89PNG synthetic: the shop front at Brunnenring 35"
OTHER_IMAGE = b"\x89PNG synthetic: the shop front, retouched"
P, ITEMS = FakeWiki.PROPERTIES, FakeWiki.ITEMS


def scan(tmp_path, data: bytes, name="portrait-1890s.png", folder="scans"):
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def register(wiki, path, title=TITLE, artifact_type="photograph", pages=0):
    return register_artifact(
        wiki,
        media_path=path,
        artifact_type=artifact_type,
        title=title,
        pages=pages,
    )


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def value(statement):
    datavalue = statement["mainsnak"]["datavalue"]["value"]
    return datavalue["id"] if isinstance(datavalue, dict) else datavalue


def cited(statement) -> list[str]:
    return [
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(P["stated_in"], [])
    ]


def strings(wiki, qid: str, key: str) -> list[str]:
    return [
        value(statement)
        for statement in wiki.entities[qid]["claims"].get(P[key], [])
    ]


def label_of(wiki, qid: str) -> str | None:
    return wiki.entities[qid]["labels"].get("en", {}).get("value")


def part_of(entity) -> dict[tuple[str, str], list[str]]:
    """(whole, page) -> the items each part-of statement is stated in."""
    return {
        (
            value(statement),
            statement["qualifiers"][P["page_number"]][0]["datavalue"]["value"],
        ): cited(statement)
        for statement in entity["claims"].get(P["part_of"], [])
    }


def assert_labels_unique(wiki) -> None:
    """The wiki client's label index fails on a duplicate label."""
    labels = [label_of(wiki, qid) for qid in wiki.entities]
    labels = [label for label in labels if label is not None]
    assert len(labels) == len(set(labels)), labels


# -- register_artifact: identity by SHA-1 alone -------------------------------


class TestRegisterArtifact:
    def test_first_registration_uploads_and_hashes(self, tmp_path):
        wiki = FakeWiki()
        result = register(wiki, scan(tmp_path, PORTRAIT))
        assert result.created
        assert result.content_sha1 == sha1(PORTRAIT)
        assert result.file_page == "File:Portrait-1890s.png"
        assert label_of(wiki, result.artifact_qid) == TITLE
        assert strings(wiki, result.artifact_qid, "media_file") == [
            "File:Portrait-1890s.png"
        ]
        assert strings(wiki, result.artifact_qid, "media_sha1") == [
            sha1(PORTRAIT)
        ]
        # an artifact asserts its own kind and media (ADR-0003)
        for statements in wiki.entities[result.artifact_qid][
            "claims"
        ].values():
            for statement in statements:
                assert cited(statement) == [result.artifact_qid]
        assert wiki.drift() == []

    def test_changed_source_under_the_same_name_is_its_own_artifact(
        self, tmp_path
    ):
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        before = copy.deepcopy(wiki.entities[first.artifact_qid])

        changed = register(wiki, scan(tmp_path, RESCAN, folder="rescans"))

        assert changed.artifact_qid != first.artifact_qid
        tag = sha1(RESCAN)[:8]
        assert label_of(wiki, changed.artifact_qid) == f"{TITLE} [{tag}]"
        # the stored file is not overwritten: the new bytes get their own
        assert changed.file_page == f"File:Portrait-1890s {tag}.png"
        assert wiki.files == {
            "File:Portrait-1890s.png": sha1(PORTRAIT),
            f"File:Portrait-1890s {tag}.png": sha1(RESCAN),
        }
        assert wiki.entities[first.artifact_qid] == before
        assert wiki.drift() == []

    def test_same_bytes_under_a_new_name_upload_nothing(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        requests = len(wiki.media.requests)

        copied = register(
            wiki, scan(tmp_path, PORTRAIT, name="muckel and cilli.png"), "Copy"
        )

        assert copied.artifact_qid == first.artifact_qid
        assert not copied.created
        assert copied.file_page == "File:Portrait-1890s.png"
        assert len(wiki.media.requests) == requests  # held already
        assert len(wiki.files) == 1
        aliases = wiki.entities[first.artifact_qid]["aliases"]["en"]
        assert [alias["value"] for alias in aliases] == ["Copy"]

    def test_rerun_uploads_and_writes_nothing(self, tmp_path):
        wiki = FakeWiki()
        register(wiki, scan(tmp_path, PORTRAIT))
        register(wiki, scan(tmp_path, RESCAN, folder="rescans"))
        snapshot, writes = copy.deepcopy(wiki.entities), wiki.writes
        requests = len(wiki.media.requests)

        register(wiki, scan(tmp_path, PORTRAIT))
        changed = register(wiki, scan(tmp_path, RESCAN, folder="rescans"))

        assert wiki.entities == snapshot
        assert wiki.writes == writes
        assert len(wiki.media.requests) == requests
        # the tagged item never takes the other artifact's title as alias
        entity = wiki.entities[changed.artifact_qid]
        assert not entity.get("aliases", {}).get("en")

    def test_a_title_naming_another_item_is_not_an_alias(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        other = register(
            wiki, scan(tmp_path, RESCAN, name="rescan.png"), "Rescan"
        )

        # the portrait bytes again, under the title of the other artifact
        again = register(
            wiki, scan(tmp_path, PORTRAIT, name="x.png", folder="x"), "Rescan"
        )

        assert again.artifact_qid == first.artifact_qid != other.artifact_qid
        entity = wiki.entities[first.artifact_qid]
        assert not entity.get("aliases", {}).get("en")

    @pytest.mark.parametrize(
        "holding", ["the same bytes", "no file", "other bytes"]
    )
    def test_an_item_is_never_found_by_its_title(self, tmp_path, holding):
        """An item that carries the title but not the media's SHA-1 is
        never taken for the artifact, whatever its file holds: a title is
        no identity (#103). The new artifact's label is tagged, since the
        title is taken."""
        wiki = FakeWiki()
        files = {
            "the same bytes": [
                ("media_file", "File:Portrait-1890s.png", None)
            ],
            "no file": [],
            "other bytes": [("media_file", "File:Portrait-1890s.png", None)],
        }[holding]
        stored = RESCAN if holding == "other bytes" else PORTRAIT
        if files:
            wiki.media.store("Portrait-1890s.png", stored)
        titled = wiki.item(
            TITLE, ("instance_of", ITEMS["photograph"], None), *files
        )
        before = copy.deepcopy(wiki.entities[titled])

        result = register(wiki, scan(tmp_path, PORTRAIT))

        assert result.created and result.artifact_qid != titled
        assert wiki.entities[titled] == before
        tag = sha1(PORTRAIT)[:8]
        assert label_of(wiki, result.artifact_qid) == f"{TITLE} [{tag}]"
        assert strings(wiki, result.artifact_qid, "media_sha1") == [
            sha1(PORTRAIT)
        ]
        assert_labels_unique(wiki)
        assert wiki.drift() == []

    def test_an_overwritten_file_is_not_trusted(self, tmp_path):
        # a file overwritten outside this code (an older upload that
        # ignored warnings) no longer holds the item's bytes: the item
        # gains a file that does, and its earlier statement stays
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        qid = first.artifact_qid
        wiki.media.versions["Portrait-1890s.png"].append(sha1(RESCAN))

        again = register(wiki, scan(tmp_path, PORTRAIT))

        tag = sha1(PORTRAIT)[:8]
        assert again.artifact_qid == qid
        assert again.file_page == f"File:Portrait-1890s {tag}.png"
        assert strings(wiki, qid, "media_file") == [
            "File:Portrait-1890s.png",
            f"File:Portrait-1890s {tag}.png",
        ]
        assert wiki.files[f"File:Portrait-1890s {tag}.png"] == sha1(PORTRAIT)
        assert wiki.drift() == [(qid, "File:Portrait-1890s.png")]

    def test_an_item_the_same_bytes_have_gains_its_own_reference(
        self, tmp_path
    ):
        """An item a container registered for these bytes (a deck's
        derived medium) is the artifact: its own registration, which finds
        every statement it would make there already, adds its own reference
        to each, in one write, and replaces none."""
        wiki = FakeWiki()
        deck = wiki.item(SHOP)
        label = f"{SHOP} — image2.png"
        wiki.media.store("Shop image2.png", PORTRAIT)
        media = wiki.item(
            label,
            ("media_sha1", sha1(PORTRAIT), None),
            ("media_file", "File:Shop image2.png", None),
            ("instance_of", ITEMS["photograph"], None),
            stated_in=deck,
        )
        writes = wiki.writes

        result = register(wiki, scan(tmp_path, PORTRAIT), title=label)

        assert result.artifact_qid == media and not result.created
        assert wiki.writes == writes + 1
        assert wiki.media.requests == []  # its file holds the bytes
        claims = wiki.entities[media]["claims"]
        (kind,) = claims[P["instance_of"]]
        (hashed,) = claims[P["media_sha1"]]
        (stored,) = claims[P["media_file"]]
        assert cited(kind) == cited(hashed) == cited(stored) == [deck, media]

    def test_registration_needs_media(self):
        wiki = Untouchable()
        with pytest.raises(ValueError, match="SHA-1 of its media"):
            register_artifact(
                wiki, media_path=None, artifact_type="photograph", title=TITLE
            )
        assert wiki.asked == []

    def test_a_source_whose_bytes_changed_is_refused(self, tmp_path):
        wiki = Untouchable()
        path = scan(tmp_path, RESCAN)
        with pytest.raises(RuntimeError, match="no longer has the bytes"):
            Registrar(wiki).register_artifact(
                path, "photograph", TITLE, sha1=sha1(PORTRAIT)
            )
        assert wiki.asked == []

    def test_a_registration_cut_short_takes_no_label(self, tmp_path):
        """The artifact's statements cite the item itself, so it is made in
        two writes; a registration cut short between them leaves an item
        with no label and no statement, and the next registration of the
        bytes takes the plain title."""
        wiki = FakeWiki()
        wiki.refuse = lambda data: bool(data.get("labels"))
        with pytest.raises(RuntimeError, match="connection"):
            register(wiki, scan(tmp_path, PORTRAIT))
        (stray,) = wiki.entities.values()
        assert "en" not in stray["labels"] and not stray["claims"]

        wiki.refuse = lambda data: False
        result = register(wiki, scan(tmp_path, PORTRAIT))

        assert result.artifact_qid != stray["id"]
        assert label_of(wiki, result.artifact_qid) == TITLE
        assert wiki.drift() == []

    def test_one_registrar_reads_the_instance_once(self, tmp_path):
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        first = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE, pages=2
        )
        # what it wrote is found again without another reading
        again = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE, pages=2
        )
        changed = registrar.register_artifact(
            scan(tmp_path, RESCAN, folder="rescans"), "photograph", TITLE
        )
        assert wiki.reads == 1
        assert again.artifact_qid == first.artifact_qid
        assert again.fragment_qids == first.fragment_qids
        assert changed.artifact_qid != first.artifact_qid
        assert_labels_unique(wiki)


# -- the never-ingest list (#29) ----------------------------------------------


class TestNeverIngest:
    @pytest.mark.parametrize(
        "entry",
        [sha1(PORTRAIT), "portrait-*.PNG"],
        ids=["by SHA-1", "by name"],
    )
    def test_a_listed_file_is_never_registered_or_uploaded(
        self, tmp_path, entry
    ):
        exclusions = ExclusionList.parse(entry + "\n")
        wiki = Untouchable()
        with pytest.raises(ExcludedMedia, match="never-ingest"):
            register_artifact(
                wiki,
                media_path=scan(tmp_path, PORTRAIT),
                artifact_type="photograph",
                title=TITLE,
                exclusions=exclusions,
            )
        # refused before the wiki was asked anything, uploads included
        assert wiki.asked == []

    def test_a_file_the_list_does_not_name_is_registered(self, tmp_path):
        wiki = FakeWiki()
        exclusions = ExclusionList.parse(f"{sha1(RESCAN)}\nletter-*.pdf\n")
        result = register_artifact(
            wiki,
            media_path=scan(tmp_path, PORTRAIT),
            artifact_type="photograph",
            title=TITLE,
            exclusions=exclusions,
        )
        assert strings(wiki, result.artifact_qid, "media_sha1") == [
            sha1(PORTRAIT)
        ]


# -- page fragments, keyed by their artifact (#187) ---------------------------


def fragment_statements(wiki, qid: str) -> tuple[list, dict]:
    """(its kinds, its part-of statements) of a fragment item."""
    entity = wiki.entities[qid]
    kinds = [value(s) for s in entity["claims"].get(P["instance_of"], [])]
    return kinds, part_of(entity)


class TestFragments:
    def test_pages_become_fragments_of_the_artifact(self, tmp_path):
        wiki = FakeWiki()
        result = register(
            wiki,
            scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf"),
            EXTRACT_TITLE,
            "certificate",
            pages=2,
        )
        artifact = result.artifact_qid
        assert sorted(result.fragment_qids) == [1, 2]
        for page, qid in result.fragment_qids.items():
            kinds, parts = fragment_statements(wiki, qid)
            assert kinds == [ITEMS["artifact_fragment"]]
            # part of the artifact on its page, stated in the artifact
            assert parts == {(artifact, str(page)): [artifact]}
            for statement in wiki.entities[qid]["claims"][P["instance_of"]]:
                assert cited(statement) == [artifact]
            assert label_of(wiki, qid) == f"{EXTRACT_TITLE}, page {page}"
        # each fragment is made in one write: no fragment without its
        # statements, ever
        assert wiki.writes == 2 + 2

    def test_same_titled_artifacts_get_distinct_fragments(self, tmp_path):
        wiki = FakeWiki()
        first = register(
            wiki,
            scan(tmp_path, EXTRACT, "birth-extract-1938.pdf", "emigration"),
            EXTRACT_TITLE,
            "certificate",
            pages=2,
        )
        second = register(
            wiki,
            scan(tmp_path, SECOND_COPY, "birth-extract-1938.pdf", "copies"),
            EXTRACT_TITLE,
            "certificate",
            pages=2,
        )

        assert second.artifact_qid != first.artifact_qid
        assert set(first.fragment_qids.values()).isdisjoint(
            second.fragment_qids.values()
        )
        for result in (first, second):
            for page, qid in result.fragment_qids.items():
                _, parts = fragment_statements(wiki, qid)
                assert parts == {
                    (result.artifact_qid, str(page)): [result.artifact_qid]
                }
        tag = sha1(SECOND_COPY)[:8]
        assert label_of(wiki, second.fragment_qids[1]) == (
            f"{EXTRACT_TITLE} [{tag}], page 1"
        )
        assert_labels_unique(wiki)

    def test_rerun_finds_the_artifacts_own_fragments(self, tmp_path):
        wiki = FakeWiki()
        paths = [
            scan(tmp_path, EXTRACT, "birth-extract-1938.pdf", "emigration"),
            scan(tmp_path, SECOND_COPY, "birth-extract-1938.pdf", "copies"),
        ]
        first = [
            register(wiki, path, EXTRACT_TITLE, "certificate", pages=2)
            for path in paths
        ]
        snapshot, writes = copy.deepcopy(wiki.entities), wiki.writes

        # a new reading of the instance, as a later write's registrar has
        again = [
            register(wiki, path, EXTRACT_TITLE, "certificate", pages=2)
            for path in paths
        ]

        assert [r.fragment_qids for r in again] == [
            r.fragment_qids for r in first
        ]
        assert wiki.writes == writes
        assert wiki.entities == snapshot

    def test_more_pages_add_only_the_missing_fragments(self, tmp_path):
        wiki = FakeWiki()
        path = scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf")
        one = register(wiki, path, EXTRACT_TITLE, "certificate", pages=1)
        three = register(wiki, path, EXTRACT_TITLE, "certificate", pages=3)
        assert three.fragment_qids[1] == one.fragment_qids[1]
        assert len(set(three.fragment_qids.values())) == 3

    def test_a_same_labelled_fragment_of_another_artifact_is_not_taken(
        self, tmp_path
    ):
        """An item labelled as the page's fragment would be, but part of
        another artifact, is that artifact's: the new fragment is made,
        and its label tagged."""
        wiki = FakeWiki()
        elsewhere = wiki.item("Another copy")
        theirs = wiki.item(
            f"{EXTRACT_TITLE}, page 1",
            ("instance_of", ITEMS["artifact_fragment"], None),
            ("part_of", elsewhere, "1"),
        )
        before = copy.deepcopy(wiki.entities[theirs])

        result = register(
            wiki,
            scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf"),
            EXTRACT_TITLE,
            "certificate",
            pages=1,
        )

        ours = result.fragment_qids[1]
        assert ours != theirs
        assert wiki.entities[theirs] == before
        _, parts = fragment_statements(wiki, ours)
        assert parts == {(result.artifact_qid, "1"): [result.artifact_qid]}
        tag = sha1(EXTRACT)[:8]
        assert label_of(wiki, ours) == f"{EXTRACT_TITLE}, page 1 [{tag}]"
        assert_labels_unique(wiki)

    def test_a_derived_medium_is_never_taken_for_a_fragment(self, tmp_path):
        """A deck's image is `part of` the deck with a slide qualifier too,
        but it is no `artifact fragment` (ADR-0008, as amended by #289):
        the slide's fragment is an item of its own."""
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        deck = registrar.register_artifact(
            scan(tmp_path, b"PK synthetic deck", name="deck.pptx"),
            "presentation",
            "Hutmacherei zum Blauen Hut",
        ).artifact_qid
        image = wiki.item(
            "Hutmacherei zum Blauen Hut — image1.png",
            ("instance_of", ITEMS["photograph"], None),
            ("part_of", deck, "1"),
        )

        result = register(
            wiki,
            scan(tmp_path, b"PK synthetic deck", name="deck.pptx"),
            "Hutmacherei zum Blauen Hut",
            "presentation",
            pages=1,
        )

        assert result.artifact_qid == deck
        assert result.fragment_qids[1] != image
        kinds, parts = fragment_statements(wiki, result.fragment_qids[1])
        assert kinds == [ITEMS["artifact_fragment"]]
        assert parts == {(deck, "1"): [deck]}

    def test_a_fragment_is_found_by_its_page(self, tmp_path):
        wiki = FakeWiki()
        path = scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf")
        artifact = register(wiki, path, EXTRACT_TITLE, "certificate")
        qid = artifact.artifact_qid
        # a fragment a curator made for page 2, under a label of their own
        curated = wiki.item(
            "second page of the extract",
            ("instance_of", ITEMS["artifact_fragment"], None),
            ("part_of", qid, "2"),
        )

        result = register(wiki, path, EXTRACT_TITLE, "certificate", pages=2)

        assert result.fragment_qids[2] == curated
        assert result.fragment_qids[1] != curated


# -- derived media, from their record (ADR-0008; #50, #128) -------------------


def deck_file(tmp_path, name: str) -> Path:
    """A deck's bytes: only their SHA-1 matters to registration."""
    return scan(tmp_path, f"PK synthetic deck {name}".encode(), f"{name}.pptx")


def recorded(tmp_path, deck: Path, title: str, media) -> tuple:
    """The deck's derived media, recorded as the stages before the write
    record them, in a directory of its own: (record, directory). `media`
    is a list of (name, bytes, slides)."""
    directory = tmp_path / "state" / deck.stem
    content = PresentationContent(
        slides=[Slide(number=n, text="") for n in range(1, 9)],
        media=[
            EmbeddedMedium(name=name, data=data, slides=slides)
            for name, data, slides in media
        ],
    )
    record = record_derived_media(
        content, source_sha1=sha1(deck.read_bytes()), title=title,
        directory=directory,
    )  # fmt: skip
    return record, directory


def register_deck(wiki, tmp_path, name, title, media, exclusions=None):
    """Register a deck and then its derived media, as the write does:
    (the deck's QID, {media SHA-1 -> QID})."""
    deck = deck_file(tmp_path, name)
    registrar = Registrar(wiki, exclusions=exclusions)
    qid = registrar.register_artifact(deck, "presentation", title).artifact_qid
    record, directory = recorded(tmp_path, deck, title, media)
    return qid, registrar.register_derived_media(record, directory)


def assert_additive(before: dict, after: dict) -> None:
    """Every earlier statement survives with its value, qualifiers and
    references; references may only have been appended (rule 3)."""
    for pid, statements in before["claims"].items():
        now = {s["id"]: s for s in after["claims"][pid]}
        for statement in statements:
            kept = now[statement["id"]]
            assert kept["mainsnak"] == statement["mainsnak"]
            assert kept.get("qualifiers") == statement.get("qualifiers")
            earlier = statement.get("references", [])
            assert kept.get("references", [])[: len(earlier)] == earlier


class TestRegisterDerivedMedia:
    def test_first_deck_mints_a_hash_keyed_item(self, tmp_path):
        wiki = FakeWiki()
        deck, derived = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1, 2])]
        )
        assert list(derived) == [sha1(IMAGE)]
        entity = wiki.entities[derived[sha1(IMAGE)]]
        assert entity["labels"]["en"]["value"] == "Deck A — image1.png"
        assert part_of(entity) == {(deck, "1"): [deck], (deck, "2"): [deck]}
        (hashed,) = entity["claims"][P["media_sha1"]]
        assert value(hashed) == sha1(IMAGE)
        (kind,) = entity["claims"][P["instance_of"]]
        assert value(kind) == ITEMS["photograph"]
        # every statement is referenced stated in -> the deck (ADR-0008)
        for statements in entity["claims"].values():
            for statement in statements:
                assert cited(statement) == [deck]
        # uploaded under the deck-prefixed name, and made in one write
        assert wiki.files == {
            "File:A.pptx": sha1(b"PK synthetic deck a"),
            "File:Deck A image1.png": sha1(IMAGE),
        }
        assert wiki.drift() == []

    def test_second_deck_is_appended_never_replacing_the_first(self, tmp_path):
        wiki = FakeWiki()
        deck_a, first = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        qid = first[sha1(IMAGE)]
        before = copy.deepcopy(wiki.entities[qid])

        deck_b, second = register_deck(
            wiki, tmp_path, "b", "Deck B", [("image1.png", IMAGE, [2, 3])]
        )

        assert second == first  # same bytes: one artifact (ADR-0010)
        after = wiki.entities[qid]
        assert_additive(before, after)
        assert part_of(after) == {
            (deck_a, "1"): [deck_a],
            (deck_b, "2"): [deck_b],
            (deck_b, "3"): [deck_b],
        }
        # the deck-independent statements stay single, cited by both decks
        for pid in ("instance_of", "media_file", "media_sha1"):
            (statement,) = after["claims"][P[pid]]
            assert cited(statement) == [deck_a, deck_b]
        # the later deck's name is an alias; the blob is stored once
        assert [a["value"] for a in after["aliases"]["en"]] == [
            "Deck B — image1.png"
        ]
        assert sum(sha == sha1(IMAGE) for sha in wiki.files.values()) == 1
        assert wiki.drift() == []

    def test_regenerated_deck_with_the_same_title(self, tmp_path):
        # new deck bytes make a new deck item; the image is reused
        wiki = FakeWiki()
        deck, first = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        regenerated, again = register_deck(
            wiki, tmp_path, "a2", "Deck A", [("image1.png", IMAGE, [1])]
        )
        assert regenerated != deck and again == first
        after = wiki.entities[first[sha1(IMAGE)]]
        assert part_of(after) == {
            (deck, "1"): [deck],
            (regenerated, "1"): [regenerated],
        }
        assert "aliases" not in after or not after["aliases"].get("en")
        # nothing uploaded a second time
        assert sum(sha == sha1(IMAGE) for sha in wiki.files.values()) == 1

    def test_rerun_writes_nothing(self, tmp_path):
        wiki = FakeWiki()
        media = [("image1.png", IMAGE, [1])]
        register_deck(wiki, tmp_path, "a", "Deck A", media)
        register_deck(wiki, tmp_path, "b", "Deck B", media)
        snapshot, writes = copy.deepcopy(wiki.entities), wiki.writes
        requests = len(wiki.media.requests)

        register_deck(wiki, tmp_path, "a", "Deck A", media)
        register_deck(wiki, tmp_path, "b", "Deck B", media)

        assert wiki.writes == writes
        assert wiki.entities == snapshot
        assert len(wiki.media.requests) == requests

    def test_an_item_with_the_label_but_not_the_bytes_is_not_taken(
        self, tmp_path
    ):
        """A medium is found by its SHA-1 alone: an item with its label
        and no hash, even one whose file holds its bytes, is not it."""
        wiki = FakeWiki()
        wiki.media.store("Deck A image1.png", IMAGE)
        unhashed = wiki.item(
            "Deck A — image1.png",
            ("media_file", "File:Deck A image1.png", None),
        )
        before = copy.deepcopy(wiki.entities[unhashed])

        _, derived = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )

        qid = derived[sha1(IMAGE)]
        assert qid != unhashed
        assert wiki.entities[unhashed] == before
        tag = sha1(IMAGE)[:8]
        assert label_of(wiki, qid) == f"Deck A — image1.png [{tag}]"
        assert_labels_unique(wiki)

    def test_changed_image_under_the_same_name_is_distinct(self, tmp_path):
        wiki = FakeWiki()
        _, first = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        before = copy.deepcopy(wiki.entities)

        deck, changed = register_deck(
            wiki, tmp_path, "a2", "Deck A", [("image1.png", OTHER_IMAGE, [1])]
        )

        qid = changed[sha1(OTHER_IMAGE)]
        assert qid != first[sha1(IMAGE)]
        tag = sha1(OTHER_IMAGE)[:8]
        assert label_of(wiki, qid) == f"Deck A — image1.png [{tag}]"
        assert part_of(wiki.entities[qid]) == {(deck, "1"): [deck]}
        assert wiki.files[f"File:Deck A image1 {tag}.png"] == sha1(OTHER_IMAGE)
        assert wiki.drift() == []
        # the earlier artifact is not touched at all
        assert wiki.entities[first[sha1(IMAGE)]] == before[first[sha1(IMAGE)]]
        # the tagged item never takes the earlier item's label as an alias
        aliases = wiki.entities[qid].get("aliases", {}).get("en", [])
        assert "Deck A — image1.png" not in [a["value"] for a in aliases]

    def test_reused_image_never_aliases_another_items_label(self, tmp_path):
        # a deck whose image is already another deck's medium: its own
        # deck-prefixed label belongs to a different artifact, so it is
        # not added as an alias (that would cross-link the two)
        wiki = FakeWiki()
        _, first = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        deck_b, other = register_deck(
            wiki, tmp_path, "b", "Deck B", [("image1.png", OTHER_IMAGE, [1])]
        )
        before = copy.deepcopy(wiki.entities[first[sha1(IMAGE)]])

        deck, reused = register_deck(
            wiki, tmp_path, "a2", "Deck A", [("image1.png", OTHER_IMAGE, [2])]
        )

        assert reused == other  # same bytes: one artifact (ADR-0010)
        entity = wiki.entities[other[sha1(OTHER_IMAGE)]]
        assert part_of(entity) == {
            (deck_b, "1"): [deck_b],
            (deck, "2"): [deck],
        }
        assert "aliases" not in entity or not entity["aliases"].get("en")
        assert wiki.entities[first[sha1(IMAGE)]] == before

    def test_an_overwritten_file_is_not_trusted(self, tmp_path):
        # the item's file was overwritten outside this code (an older
        # upload that ignored warnings): the next deck finds no file of
        # the item holding its bytes, so it stores them again under its
        # own name, and the earlier statement stays as it is (#186)
        wiki = FakeWiki()
        deck_a, first = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        qid = first[sha1(IMAGE)]
        wiki.media.versions["Deck_A_image1.png"].append(sha1(OTHER_IMAGE))
        before = copy.deepcopy(wiki.entities[qid])

        deck_b, again = register_deck(
            wiki, tmp_path, "b", "Deck B", [("image1.png", IMAGE, [1])]
        )

        assert again == first
        after = wiki.entities[qid]
        assert_additive(before, after)
        files = {
            value(statement): cited(statement)
            for statement in after["claims"][P["media_file"]]
        }
        assert files == {
            "File:Deck A image1.png": [deck_a],
            "File:Deck B image1.png": [deck_b],
        }
        assert wiki.files["File:Deck B image1.png"] == sha1(IMAGE)
        assert wiki.drift() == [(qid, "File:Deck A image1.png")]

    def test_one_medium_under_several_names_is_one_item(self, tmp_path):
        wiki = FakeWiki()
        _, derived = register_deck(
            wiki,
            tmp_path,
            "a",
            SHOP,
            [("image2.png", IMAGE, [2]), ("image4.png", IMAGE, [3])],
        )
        (qid,) = derived.values()
        entity = wiki.entities[qid]
        assert label_of(wiki, qid) == f"{SHOP} — image2.png"
        assert [a["value"] for a in entity["aliases"]["en"]] == [
            f"{SHOP} — image4.png"
        ]
        assert sorted(page for _, page in part_of(entity)) == ["2", "3"]

    def test_an_image_ingested_on_its_own_too_is_one_item(self, tmp_path):
        """The shop front is a document of its own and a deck's image: one
        artifact (ADR-0010), whose own statements gain the deck's
        reference."""
        wiki = FakeWiki()
        own = register(wiki, scan(tmp_path, IMAGE, "shop-front.png"), "Shop")

        deck, derived = register_deck(
            wiki, tmp_path, "a", SHOP, [("image1.png", IMAGE, [1])]
        )

        assert derived == {sha1(IMAGE): own.artifact_qid}
        claims = wiki.entities[own.artifact_qid]["claims"]
        (hashed,) = claims[P["media_sha1"]]
        assert cited(hashed) == [own.artifact_qid, deck]
        assert sum(sha == sha1(IMAGE) for sha in wiki.files.values()) == 1

    def test_the_container_is_registered_first(self, tmp_path):
        wiki = FakeWiki()
        deck = deck_file(tmp_path, "a")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        with pytest.raises(LookupError, match="register the container"):
            register_derived_media(wiki, record, directory)
        assert wiki.writes == 0 and wiki.media.requests == []

    def test_a_medium_whose_kept_bytes_changed_is_refused(self, tmp_path):
        wiki = FakeWiki()
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        (medium,) = record.media.values()
        (directory / medium.file).write_bytes(OTHER_IMAGE)
        requests, writes = len(wiki.media.requests), wiki.writes

        with pytest.raises(RuntimeError, match="does not hold the bytes"):
            registrar.register_derived_media(record, directory)
        assert len(wiki.media.requests) == requests
        assert wiki.writes == writes

    @pytest.mark.parametrize(
        "entry", [sha1(IMAGE), "IMAGE1.*"], ids=["by SHA-1", "by name"]
    )
    def test_a_listed_medium_is_neither_registered_nor_uploaded(
        self, tmp_path, entry
    ):
        wiki = FakeWiki()
        _, derived = register_deck(
            wiki,
            tmp_path,
            "a",
            "Deck A",
            [("image1.png", IMAGE, [1]), ("image2.png", OTHER_IMAGE, [2])],
            exclusions=ExclusionList.parse(entry + "\n"),
        )
        assert list(derived) == [sha1(OTHER_IMAGE)]
        assert sha1(IMAGE) not in wiki.files.values()
        assert not any(
            sha1(IMAGE) in strings(wiki, qid, "media_sha1")
            for qid in wiki.entities
        )

    def test_no_media_asks_nothing(self, tmp_path):
        wiki = Untouchable()
        record = record_derived_media(
            PresentationContent(slides=[Slide(number=1, text="")], media=[]),
            source_sha1=sha1(b"PK a deck"),
            title="Deck A",
            directory=tmp_path,
        )
        assert register_derived_media(wiki, record, tmp_path) == {}
        assert wiki.asked == []


# -- the write's registration of a document (Orchestrator._step_register) ----


def parts_of(wiki, whole: str) -> dict[str, dict[str, list[str]]]:
    """For each kind of item `part of` the whole ("fragment" or
    "medium"): page -> the items on it."""
    found: dict[str, dict[str, list[str]]] = {"fragment": {}, "medium": {}}
    for qid, entity in wiki.entities.items():
        kinds = [value(s) for s in entity["claims"].get(P["instance_of"], [])]
        kind = "fragment" if ITEMS["artifact_fragment"] in kinds else "medium"
        for (target, page), _ in part_of(entity).items():
            if target == whole:
                found[kind].setdefault(page, []).append(qid)
    return found


class TestStepRegister:
    """The orchestrator registers a document as its write begins
    (Orchestrator._step_register), through the registrar, on the in-memory
    wiki."""

    @staticmethod
    def orchestrator(tmp_path, wiki, files=(), exclusions=None):
        from yizkr.pipeline.orchestrate import Orchestrator

        folder = tmp_path / "corpus"
        folder.mkdir(parents=True)
        for name, data in files:
            (folder / name).write_bytes(data)
        lines: list[str] = []
        orchestrator = Orchestrator(
            folder,
            tmp_path / "state",
            exclusions=exclusions or ExclusionList(),
            log=lambda *a, **k: lines.append(" ".join(map(str, a))),
        )
        orchestrator._client = wiki
        return orchestrator, lines

    def test_a_scan_is_registered_as_the_run_types_it(self, tmp_path):
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(
            tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
        )
        (doc,) = orchestrator.documents()
        qid = orchestrator._step_register(doc)
        entity = wiki.entities[qid]
        assert label_of(wiki, qid) == "portrait-1890s"
        assert entity["descriptions"]["en"]["value"] == (
            "ingested by `yizkr ingest run` from portrait-1890s.png"
        )
        assert strings(wiki, qid, "media_sha1") == [doc.sha1]
        assert strings(wiki, qid, "instance_of") == [ITEMS["artifact"]]
        # a second write finds it by its bytes, and writes nothing
        writes = wiki.writes
        assert orchestrator._step_register(doc) == qid
        assert wiki.writes == writes

    def test_a_gedcom_export_is_a_genealogical_report(self, tmp_path):
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(
            tmp_path, wiki, [("example-family.ged", b"0 HEAD\n0 TRLR\n")]
        )
        (doc,) = orchestrator.documents()
        qid = orchestrator._step_register(doc)
        assert strings(wiki, qid, "instance_of") == [
            ITEMS["genealogical_report"]
        ]

    def test_a_deck_its_slides_and_its_images(self, tmp_path):
        wiki = FakeWiki()
        orchestrator, lines = self.orchestrator(tmp_path, wiki)
        build_deck(orchestrator.folder / DECK)
        (doc,) = orchestrator.documents()

        # the record of the deck's media is made when the write finds none
        deck = orchestrator._step_register(doc)

        assert strings(wiki, deck, "instance_of") == [ITEMS["presentation"]]
        parts = parts_of(wiki, deck)
        # three slides, each the deck's fragment for its page
        assert sorted(parts["fragment"]) == ["1", "2", "3"]
        assert all(len(qids) == 1 for qids in parts["fragment"].values())
        # the shop front on slides 1 and 2, the studio portrait (two names
        # in the archive, one item) on slides 2 and 3; the film is no image
        media = {
            sha1(data): qid
            for qid in wiki.entities
            for data in (SHOP_FRONT, STUDIO)
            if strings(wiki, qid, "media_sha1") == [sha1(data)]
        }
        assert parts["medium"] == {
            "1": [media[sha1(SHOP_FRONT)]],
            "2": sorted(media.values(), key=lambda q: int(q[1:])),
            "3": [media[sha1(STUDIO)]],
        }
        assert "  derived media: 2 registered" in lines
        assert wiki.drift() == []

        # a second write registers nothing again
        writes, requests = wiki.writes, len(wiki.media.requests)
        again, _ = self.orchestrator(tmp_path / "again", wiki)
        build_deck(again.folder / DECK)
        (doc,) = again.documents()
        assert again._step_register(doc) == deck
        assert (wiki.writes, len(wiki.media.requests)) == (writes, requests)

    def test_a_listed_image_is_left_out(self, tmp_path):
        wiki = FakeWiki()
        orchestrator, lines = self.orchestrator(
            tmp_path, wiki, exclusions=ExclusionList.parse(sha1(STUDIO))
        )
        build_deck(orchestrator.folder / DECK)
        (doc,) = orchestrator.documents()
        orchestrator._step_register(doc)
        assert (
            "  derived media: 1 registered, 1 left out (never-ingest list)"
            in (lines)
        )
        hashes = {
            hashed
            for qid in wiki.entities
            for hashed in strings(wiki, qid, "media_sha1")
        }
        assert sha1(SHOP_FRONT) in hashes and sha1(STUDIO) not in hashes
        assert sha1(STUDIO) not in wiki.files.values()

    def test_a_listed_document_is_refused_before_the_wiki(self, tmp_path):
        """The list is asked again at registration: a document it names,
        by a list the run was not started with, say, asks nothing of the
        wiki and uploads nothing."""
        from yizkr.pipeline.orchestrate import DocumentRun

        wiki = Untouchable()
        orchestrator, _ = self.orchestrator(tmp_path, wiki)
        source = orchestrator.folder / "portrait-1890s.png"
        source.write_bytes(PORTRAIT)
        doc = DocumentRun(source, orchestrator.state_dir)
        orchestrator.exclusions = ExclusionList.parse(sha1(PORTRAIT))
        with pytest.raises(ExcludedMedia):
            orchestrator._step_register(doc)
        assert wiki.asked == []

    def test_a_source_changed_since_it_was_read_is_refused(self, tmp_path):
        wiki = Untouchable()
        orchestrator, _ = self.orchestrator(
            tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
        )
        (doc,) = orchestrator.documents()
        doc.source.write_bytes(RESCAN)  # after the run read its SHA-1
        with pytest.raises(RuntimeError, match="no longer has the bytes"):
            orchestrator._step_register(doc)
        assert wiki.asked == []
