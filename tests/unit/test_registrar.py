"""Offline tests for registration by the SHA-1 of an artifact's media
(ADR-0010; #103).

Covered here: the lag-free enumeration the registrar reads the instance
with, the additive statement helper derived-media registration uses (#50),
and the registrar itself over an in-memory wiki whose uploads run the
real `WikiClient.upload_file` against a fake MediaWiki (#186): artifacts
found or made by SHA-1 alone, never by title; page fragments keyed by
their artifact (#187); a container's derived media registered from their
record (#128); upload names in the form MediaWiki takes, and
descriptions, within MediaWiki's and Wikibase's limits (#373); the
never-ingest list (#29); and `ensure_self_typed`, which
gives an item found without its kind that kind, stated in the item itself
(#368; its use for the place-hierarchy pass's run item is tested in
tests/unit/test_places.py). The orchestrator's
write-time registration of a document runs here too, through its real
`_step_register`. The flow is also verified live
(tests/integration/test_live_write_time_registration.py,
test_live_registrar.py, test_live_pipeline.py,
test_live_presentation.py and test_live_upload_names.py).
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import unicodedata
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
    _cut_to_fit,
    _ensure_referenced,
    _item_claim,
    _string_claim,
    _upload_name,
    _whole_letters,
    ensure_self_typed,
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
        "extraction_run": "Q8",
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


def stored(term: str) -> str:
    """A term as Wikibase stores it, emulated from the Unicode categories
    its trimming names: separators (Z) and controls (Cc) trimmed at either
    end, each run of controls inside made one space, and the whole in
    NFC."""

    def control(char: str) -> bool:
        return unicodedata.category(char) == "Cc"

    def edge(char: str) -> bool:
        return control(char) or unicodedata.category(char).startswith("Z")

    start, end = 0, len(term)
    while start < end and edge(term[start]):
        start += 1
    while end > start and edge(term[end - 1]):
        end -= 1
    inner = "".join(
        " " if is_control else "".join(run)
        for is_control, run in itertools.groupby(term[start:end], control)
    )
    return unicodedata.normalize("NFC", inner)


class StoringWiki(FakeWiki):
    """The in-memory wiki keeping terms as Wikibase does (`stored`). Like
    Wikibase, it refuses a label, description or alias longer than 250
    characters, and an item whose English label and description are
    another item's."""

    def store(self, entity):
        data = entity.get_json()
        label = stored(data.get("labels", {}).get("en", {}).get("value", ""))
        description = stored(
            data.get("descriptions", {}).get("en", {}).get("value", "")
        )
        aliases = [
            stored(alias["value"])
            for alias in data.get("aliases", {}).get("en", [])
        ]
        if any(len(term) > 250 for term in (label, description, *aliases)):
            raise WriteRefused("a term is longer than 250 characters")
        if label and description:
            for qid, other in self.entities.items():
                if qid != entity.id and (
                    other.get("labels", {}).get("en", {}).get("value"),
                    other.get("descriptions", {}).get("en", {}).get("value"),
                ) == (label, description):
                    raise WriteRefused(
                        f"{qid} already has this label and description"
                    )
        written = super().store(entity)
        record = self.entities[written.id]
        for kind in ("labels", "descriptions"):
            for term in record.get(kind, {}).values():
                term["value"] = stored(term["value"])
        for terms in record.get("aliases", {}).values():
            for term in terms:
                term["value"] = stored(term["value"])
        return self.wbi.item.get(written.id)


# -- fictional media (docs/EXAMPLES.md) ---------------------------------------

# two different scans of the Löbig family's studio portrait of the 1890s
PORTRAIT = b"\x89PNG synthetic scan of the 1890s studio portrait"
RESCAN = b"\x89PNG synthetic re-scan of the 1890s studio portrait"
TITLE = "Studio portrait of the Löbig family"
# a title of 256 characters, which Flörburg repeats, for the limit on a
# term's length
LONG = "Flörburg" * 32
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

    def test_a_title_whose_tagged_form_is_taken_too_is_refused(self, tmp_path):
        """Labels stay unique: when the title and its content-tagged form
        both name other items, nothing is made, written or uploaded."""
        wiki = FakeWiki()
        wiki.item(TITLE)
        wiki.item(f"{TITLE} [{sha1(PORTRAIT)[:8]}]")
        writes = wiki.writes
        with pytest.raises(RuntimeError, match="labels stay unique"):
            register(wiki, scan(tmp_path, PORTRAIT))
        assert wiki.writes == writes
        assert wiki.media.requests == []
        assert_labels_unique(wiki)

    def test_of_two_items_with_the_bytes_the_earliest_is_the_artifact(
        self, tmp_path
    ):
        """Two items that carry one media SHA-1 are the duplicate the key
        exists to prevent: registration takes the numerically earliest,
        as the writer's index does, and makes no third."""
        wiki = FakeWiki()
        hashed = ("media_sha1", sha1(PORTRAIT), None)
        first = wiki.item("Studio portrait, first copy", hashed)
        second = wiki.item("Studio portrait, second copy", hashed)
        result = register(wiki, scan(tmp_path, PORTRAIT))
        assert result.artifact_qid == first != second
        assert not result.created
        assert len(wiki.entities) == 2

    @pytest.mark.parametrize("length", [250, 251])
    def test_a_label_of_250_characters_is_taken_and_251_refused(
        self, tmp_path, length
    ):
        """Wikibase takes a label of 250 characters and refuses one of
        251: so does registration, which refuses it before anything is
        uploaded or written."""
        wiki = StoringWiki()
        title = LONG[:length]
        if length == 250:
            result = register(wiki, scan(tmp_path, PORTRAIT), title)
            assert label_of(wiki, result.artifact_qid) == title
            return
        with pytest.raises(ValueError, match="250 characters"):
            register(wiki, scan(tmp_path, PORTRAIT), title)
        assert (wiki.writes, wiki.media.requests) == (0, [])

    @pytest.mark.parametrize("length", [239, 240])
    def test_a_tagged_label_of_250_characters_is_taken_and_251_refused(
        self, tmp_path, length
    ):
        """A title another item holds is tagged, 11 characters more: 250
        in all is taken, and 251 refused before anything is uploaded or
        written, retry after retry, so that no half-made item is left."""
        wiki = StoringWiki()
        title = LONG[:length]
        wiki.item(title)
        writes = wiki.writes
        tagged = f"{title} [{sha1(PORTRAIT)[:8]}]"
        if length == 239:
            result = register(wiki, scan(tmp_path, PORTRAIT), title)
            assert label_of(wiki, result.artifact_qid) == tagged
            assert len(tagged) == 250
            return
        for _ in range(2):
            with pytest.raises(ValueError, match="250 characters"):
                register(wiki, scan(tmp_path, PORTRAIT), title)
        assert (wiki.writes, wiki.media.requests) == (writes, [])

    @pytest.mark.parametrize("length", [250, 251])
    def test_a_name_of_250_characters_is_an_alias_and_251_not_one(
        self, tmp_path, length
    ):
        wiki = StoringWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        name = LONG[:length]
        again = register(
            wiki, scan(tmp_path, PORTRAIT, name="x.png", folder="x"), name
        )
        assert again.artifact_qid == first.artifact_qid
        aliases = wiki.entities[first.artifact_qid].get("aliases", {})
        assert [alias["value"] for alias in aliases.get("en", [])] == (
            [name] if length == 250 else []
        )

    def test_a_blank_title_is_refused(self, tmp_path):
        wiki = Untouchable()
        with pytest.raises(ValueError, match="blank title"):
            register(wiki, scan(tmp_path, PORTRAIT), " \t ")
        assert wiki.asked == []

    def test_a_property_label_leaves_the_title_free(self, tmp_path):
        """Labels are unique per kind: a property with the title as its
        label leaves the title free for an item."""
        wiki = FakeWiki()
        wiki.entities["P900"] = {
            "id": "P900",
            "type": "property",
            "labels": {"en": {"language": "en", "value": TITLE}},
            "claims": {},
        }
        result = register(wiki, scan(tmp_path, PORTRAIT))
        assert label_of(wiki, result.artifact_qid) == TITLE

    def test_its_own_tag_keeps_the_plain_title_from_its_aliases(
        self, tmp_path
    ):
        """An item tagged because its title named another item never takes
        that title as an alias, even once the other item is renamed: the
        title belongs to the other artifact."""
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        changed = register(wiki, scan(tmp_path, RESCAN, folder="rescans"))
        # a curator renames the first artifact
        label = wiki.entities[first.artifact_qid]["labels"]["en"]
        label["value"] = "Studio portrait, 1890s"
        writes = wiki.writes
        register(wiki, scan(tmp_path, RESCAN, folder="rescans"))
        assert wiki.writes == writes
        assert not wiki.entities[changed.artifact_qid].get("aliases")

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


# -- terms in the form Wikibase stores them (#367 review) ---------------------

# the portrait's title as a file name made on some systems keeps it:
# decomposed (NFD), which the instance stores composed (NFC)
NFD_TITLE = unicodedata.normalize("NFD", TITLE)
FROM_FILE = "ingested by `yizkr ingest run` from portrait-1890s.png"


class TestTermsInTheStoredForm:
    """Wikibase stores every label trimmed of separators and controls, with
    each run of controls inside made one space, and in NFC, and the reading
    of the instance holds them so: a title is compared, and set, in that
    form (StoringWiki keeps terms as the instance does)."""

    @pytest.mark.parametrize(
        "title",
        [
            NFD_TITLE,
            f"{TITLE} ",
            f"\t{TITLE}",
            TITLE.replace(" of", "\tof"),
            f"{TITLE}\x7f",
        ],
        ids=[
            "decomposed",
            "trailing space",
            "leading tab",
            "inner tab",
            "trailing DEL",
        ],
    )
    def test_a_title_another_item_holds_is_tagged(self, tmp_path, title):
        wiki = StoringWiki()
        held = wiki.item(TITLE)  # a person or place the writer minted, say
        result = register(wiki, scan(tmp_path, PORTRAIT), title)
        assert result.artifact_qid != held
        assert label_of(wiki, result.artifact_qid) == (
            f"{TITLE} [{sha1(PORTRAIT)[:8]}]"
        )
        assert_labels_unique(wiki)

    def test_changed_bytes_under_such_a_title_are_an_artifact(self, tmp_path):
        """The file's bytes change under its name: the same title and the
        same description. The new artifact's tagged label keeps the pair
        unique, so the instance takes its second write, and no item is
        left without a label."""
        wiki = StoringWiki()
        first = register_artifact(
            wiki, scan(tmp_path, PORTRAIT), "photograph", NFD_TITLE, FROM_FILE
        )
        changed = register_artifact(
            wiki,
            scan(tmp_path, RESCAN, folder="rescans"),
            "photograph",
            NFD_TITLE,
            FROM_FILE,
        )
        assert changed.artifact_qid != first.artifact_qid
        assert label_of(wiki, first.artifact_qid) == TITLE
        assert label_of(wiki, changed.artifact_qid) == (
            f"{TITLE} [{sha1(RESCAN)[:8]}]"
        )
        assert all(label_of(wiki, qid) for qid in wiki.entities)

    def test_a_rerun_under_such_a_title_writes_nothing(self, tmp_path):
        wiki = StoringWiki()
        path = scan(tmp_path, PORTRAIT)
        first = register(wiki, path, NFD_TITLE)
        writes = wiki.writes
        assert (
            register(wiki, path, NFD_TITLE).artifact_qid == first.artifact_qid
        )
        assert wiki.writes == writes
        assert not wiki.entities[first.artifact_qid].get("aliases")

    def test_a_medium_label_is_compared_as_stored(self, tmp_path):
        """A deck titled in NFD: its image's label is its title's stored
        form, tagged when another item holds that, and a second write of
        the deck writes nothing."""
        wiki = StoringWiki()
        title = f"{SHOP}, Flörburg"
        decomposed = unicodedata.normalize("NFD", title)
        held = wiki.item(f"{title} — image1.png")
        media = [("image1.png", IMAGE, [1])]
        _, derived = register_deck(wiki, tmp_path, "a", decomposed, media)
        qid = derived[sha1(IMAGE)]
        assert qid != held
        assert label_of(wiki, qid) == (
            f"{title} — image1.png [{sha1(IMAGE)[:8]}]"
        )
        assert_labels_unique(wiki)
        writes = wiki.writes
        register_deck(wiki, tmp_path, "a", decomposed, media)
        assert wiki.writes == writes

    def test_a_name_that_leaves_nothing_once_stored_is_image_n(self, tmp_path):
        """A medium named only by control characters keeps no name once
        stored: it is "image N", N its place among the record's media, so
        its label ends in a name the instance keeps as sent, and a second
        write of the deck sends no edit."""
        wiki = StoringWiki()
        media = [("\x01\x02", IMAGE, [1])]
        _, derived = register_deck(wiki, tmp_path, "a", SHOP, media)
        assert label_of(wiki, derived[sha1(IMAGE)]) == f"{SHOP} — image 1"
        writes = wiki.writes
        register_deck(wiki, tmp_path, "a", SHOP, media)
        assert wiki.writes == writes

    def test_nameless_media_are_numbered_in_the_order_of_their_sha1s(
        self, tmp_path
    ):
        """Two media with names of control characters alone, one of them
        on the never-ingest list, and one whose second name is such a
        name: each nameless one is "image N", N its place among the
        record's media in the order of their SHA-1s, excluded media
        counted; a name the stored form leaves nothing of is no alias;
        and a second write of the deck sends no edit."""
        wiki = StoringWiki()
        nameless = (IMAGE, OTHER_IMAGE, RESCAN)
        # the deck holds them in the reverse of their SHA-1s' order
        media = [
            (control, data, [slide])
            for slide, (control, data) in enumerate(
                zip(
                    ("\x01\x02", "\x03", "\x04"),
                    sorted(nameless, key=sha1, reverse=True),
                    strict=True,
                ),
                start=1,
            )
        ] + [("image1.png", PORTRAIT, [4]), ("\x01", PORTRAIT, [4])]
        excluded_one = sorted(nameless, key=sha1)[1]
        excluded = ExclusionList(sha1s=frozenset({sha1(excluded_one)}))
        _, derived = register_deck(
            wiki, tmp_path, "a", SHOP, media, exclusions=excluded
        )
        order = sorted(map(sha1, (*nameless, PORTRAIT)))
        assert sha1(excluded_one) not in derived
        for data in nameless:
            if data != excluded_one:
                number = order.index(sha1(data)) + 1
                assert label_of(wiki, derived[sha1(data)]) == (
                    f"{SHOP} — image {number}"
                )
        portrait = wiki.entities[derived[sha1(PORTRAIT)]]
        assert label_of(wiki, derived[sha1(PORTRAIT)]) == (
            f"{SHOP} — image1.png"
        )
        assert not portrait.get("aliases")
        writes = wiki.writes
        register_deck(wiki, tmp_path, "a", SHOP, media, exclusions=excluded)
        assert wiki.writes == writes

    def test_nameless_media_are_numbered_whatever_the_records_order(
        self, tmp_path
    ):
        """A record that holds its media out of their SHA-1s' order numbers
        them in that order all the same."""
        wiki = StoringWiki()
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", SHOP)
        media = [("\x01", IMAGE, [1]), ("\x02", OTHER_IMAGE, [2])]
        record, directory = recorded(tmp_path, deck, SHOP, media)
        backwards = dict(reversed(list(record.media.items())))
        record = record.model_copy(update={"media": backwards})
        derived = registrar.register_derived_media(record, directory)
        for number, key in enumerate(sorted(derived), start=1):
            assert label_of(wiki, derived[key]) == f"{SHOP} — image {number}"

    def test_a_nameless_medium_is_uploaded_as_image_n(self, tmp_path):
        """Its file takes the name its label ends in, with no extension,
        as its record's file has none: MediaWiki adds its type's."""
        wiki = FakeWiki()
        media = [("\x01\x02", IMAGE, [1])]
        register_deck(wiki, tmp_path, "a", SHOP, media)
        assert uploaded(wiki, sha1(IMAGE)) == f"File:{SHOP} image 1"

    def test_a_surviving_name_comes_before_an_emptied_one(self, tmp_path):
        """A medium whose first name the stored form empties and whose
        second survives is labelled by the second."""
        wiki = StoringWiki()
        media = [("\x01", IMAGE, [1]), ("image3.png", IMAGE, [1])]
        _, derived = register_deck(wiki, tmp_path, "a", SHOP, media)
        assert label_of(wiki, derived[sha1(IMAGE)]) == f"{SHOP} — image3.png"


class TestLongDeckTitles:
    """A deck's title is part of the labels of its slides and images, and
    of its slides' descriptions, which only display it. A title near the
    250 characters Wikibase takes would push those past it, and the deck
    would fail after its own item was made, so the title is cut there,
    ending in "…" (StoringWiki refuses a longer term, as the instance
    does)."""

    # 245 characters: the deck's own label takes it whole
    TITLE = ("Hutmacherei zum Blauen Hut, Flörburg " * 7)[:245].strip()

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_its_slides_and_images_labels_are_cut_to_fit(self, tmp_path, form):
        """The cut is made on the title's stored form, so a title given
        decomposed (NFD) is cut where the composed one is."""
        wiki = StoringWiki()
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "long")
        given = unicodedata.normalize(form, self.TITLE)
        result = registrar.register_artifact(
            deck, "presentation", given, pages=2
        )
        # the shop front under two names in the archive: one medium
        shop = unicodedata.normalize(form, "Flörburg-shop.png")
        record, directory = recorded(
            tmp_path,
            deck,
            given,
            [(shop, IMAGE, [1]), ("image3.png", IMAGE, [2])],
        )
        derived = registrar.register_derived_media(record, directory)

        assert label_of(wiki, result.artifact_qid) == self.TITLE
        for page, qid in result.fragment_qids.items():
            label = label_of(wiki, qid)
            assert label == self.TITLE[:241].rstrip() + f"…, page {page}"
            assert len(label) <= 250
            description = wiki.entities[qid]["descriptions"]["en"]["value"]
            assert description.startswith(f"page {page} of Hutmacherei")
            assert description.endswith("…") and len(description) <= 250
        medium = wiki.entities[derived[sha1(IMAGE)]]
        # " — Flörburg-shop.png" takes 20 characters, and "…" one
        label = self.TITLE[:229].rstrip() + "… — Flörburg-shop.png"
        assert medium["labels"]["en"]["value"] == label
        assert len(label) <= 250
        # its other name is an alias, cut the same way
        assert [alias["value"] for alias in medium["aliases"]["en"]] == [
            self.TITLE[:236].rstrip() + "… — image3.png"
        ]
        # the same cut every time: a second write of the deck writes nothing
        writes = wiki.writes
        again = Registrar(wiki)
        again.register_artifact(deck, "presentation", given, pages=2)
        again.register_derived_media(record, directory)
        assert wiki.writes == writes

    def test_a_cut_label_another_item_holds_is_tagged_and_fits(self, tmp_path):
        wiki = StoringWiki()
        held = wiki.item(self.TITLE[:241].rstrip() + "…, page 1")
        result = register(
            wiki, deck_file(tmp_path, "long"), self.TITLE, "presentation", 1
        )
        tag = sha1(b"PK synthetic deck long")[:8]
        label = label_of(wiki, result.fragment_qids[1])
        assert result.fragment_qids[1] != held
        assert label == self.TITLE[:230].rstrip() + f"…, page 1 [{tag}]"
        assert len(label) <= 250

    def test_an_image_on_many_slides_has_a_description_that_fits(
        self, tmp_path
    ):
        """The slides an image is on are listed in its description, cut at
        a slide when they would pass the limit."""
        wiki = StoringWiki()
        slides = list(range(1, 121))
        _, derived = register_deck(
            wiki, tmp_path, "a", SHOP, [("image1.png", IMAGE, slides)]
        )
        entity = wiki.entities[derived[sha1(IMAGE)]]
        description = entity["descriptions"]["en"]["value"]
        assert len(description) <= 250
        prefix = "media embedded in the presentation (slide "
        assert description.startswith(prefix) and description.endswith("…)")
        listed = description[len(prefix) : -2].split(", ")
        assert listed == [str(n) for n in slides[: len(listed)]]
        assert len(listed) < len(slides)


# -- upload names (#373) ------------------------------------------------------


def uploaded(wiki, content_sha1: str) -> str:
    """The page of the one file holding `content_sha1`'s bytes."""
    (page,) = [p for p, held in wiki.files.items() if held == content_sha1]
    return page


def name_bytes(page: str) -> int:
    """The bytes a file page's name takes, in UTF-8."""
    return len(page.removeprefix("File:").encode())


def overridden(wiki) -> bool:
    """Did any upload override MediaWiki's warnings, about its name or
    anything else?"""
    return any("ignorewarnings" in request for request in wiki.media.requests)


# the firm's address, in ASCII, to fill a name to a given number of bytes
ADDRESS = "Hutmacherei zum Blauen Hut, Brunnenring 35, Floerburg; " * 6


class TestUploadNames:
    """A medium's bytes are uploaded under ``"<container title> <name>"``
    and an artifact's under its own file name, each in the form MediaWiki
    takes as it is: every letter kept, each character or sequence it
    refuses made "-", as is the dot before a part it refuses as an
    extension, no part starting with a dot, never empty, and within its
    240 bytes with room for the content tag of `WikiClient.upload_file`'s
    second attempt, 231 bytes. The fake MediaWiki refuses, changes and
    warns about names as MediaWiki does, so a name it would refuse fails
    here, and one it would change overrides a warning."""

    # 244 characters, of 250 bytes
    TITLE = TestLongDeckTitles.TITLE
    # a deck title of Russian sources, two bytes a letter
    CYRILLIC = ("Калдовское районное бюро ЗАГС, " * 8).strip(" ,")

    @pytest.mark.parametrize(
        "title",
        [
            "Familie Löbig",
            "Калдовское районное бюро ЗАГС",
            "Sipkje van 't Nijks (it tichelwurk fan Snylsjum)",
            # the 1912 directory's form, its dot at the end kept
            "Löbig, Ursula, Wwe.",
            # a compatibility character, which only NFKC would change
            "Brunnenring 35½",
        ],
    )
    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_a_deck_title_keeps_its_letters(self, tmp_path, title, form):
        wiki = FakeWiki()
        given = unicodedata.normalize(form, title)
        register_deck(wiki, tmp_path, "a", given, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == f"File:{title} image1.png"
        # MediaWiki takes the name as it is: no warning overridden
        assert not overridden(wiki)

    @pytest.mark.parametrize(
        "title",
        [
            # the bidirectional isolates, which a title keeps
            "Familie\N{LEFT-TO-RIGHT ISOLATE}Löbig\N{POP DIRECTIONAL ISOLATE}",
            # "&;" names no character, so it is no reference
            "Brief &; Markt",
        ],
    )
    def test_a_title_mediawiki_takes_as_it_is_is_kept(self, tmp_path, title):
        wiki = FakeWiki()
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == f"File:{title} image1.png"
        assert not overridden(wiki)

    def test_two_titles_cut_to_one_name_are_told_apart_by_the_tag(
        self, tmp_path
    ):
        """Two long titles that differ only past the cut ask for one name.
        The first deck's image takes it; the second's bytes are other
        bytes under that name, so they go under its content-tagged form.
        Nothing is overwritten, and a re-run finds both by SHA-1."""
        wiki = FakeWiki()
        common = "Hutmacherei zum Blauen Hut, Brunnenring 35, Flörburg; " * 4
        first, second = f"{common}Teil 1", f"{common}Teil 2"
        register_deck(wiki, tmp_path, "a", first, [("image1.png", IMAGE, [1])])
        register_deck(
            wiki, tmp_path, "b", second, [("image1.png", OTHER_IMAGE, [1])]
        )
        taken = uploaded(wiki, sha1(IMAGE))
        assert taken.endswith("… image1.png")
        stem = taken.removesuffix(".png")
        tagged = f"{stem} {sha1(OTHER_IMAGE)[:8]}.png"
        assert uploaded(wiki, sha1(OTHER_IMAGE)) == tagged
        assert name_bytes(tagged) <= 240
        files, requests = dict(wiki.files), len(wiki.media.requests)
        register_deck(wiki, tmp_path, "a", first, [("image1.png", IMAGE, [1])])
        register_deck(
            wiki, tmp_path, "b", second, [("image1.png", OTHER_IMAGE, [1])]
        )
        assert wiki.files == files
        assert len(wiki.media.requests) == requests

    def test_characters_mediawiki_refuses_become_dashes(self, tmp_path):
        wiki = FakeWiki()
        title = "Löbig: Briefe [1919] #2 {a|b} <c> d/e\\f\x07g"
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == (
            "File:Löbig- Briefe -1919- -2 -a-b- -c- d-e-f-g image1.png"
        )
        assert not overridden(wiki)

    @pytest.mark.parametrize(
        "char",
        [
            *"#<>[]{}|:/\\",
            "\x00",
            "\x1b",
            "\x7f",
            "\x80",  # the controls beyond ASCII, which show as nothing
            "\x9f",
            "\ufffd",
            "\ufffe",
            "\uffff",
            "\udc80",  # a byte of a file name that is no UTF-8
            "\ud83d",  # half a pair, as a JSON string may hold one
        ],
    )
    def test_each_character_a_name_cannot_hold_becomes_a_dash(self, char):
        assert _upload_name("image1.png", f"Löbig{char}Flörburg", "x") == (
            "Löbig-Flörburg image1.png"
        )

    def test_sequences_mediawiki_refuses_are_broken(self, tmp_path):
        wiki = FakeWiki()
        title = "Hutmacherei 100%2F ~~~~ &amp; ~~ &x 50% 9%a"
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == (
            "File:Hutmacherei 100-2F --~~ -amp; ~~ &x 50% 9%a image1.png"
        )
        assert not overridden(wiki)

    @pytest.mark.parametrize(
        "title, upload",
        [
            # a lower-case escape, as a downloaded file's name holds one
            ("L%c3%b6big", "L-c3-b6big"),
            # a reference by number: digits alone
            ("Brief &1919;", "Brief -1919;"),
        ],
    )
    def test_lower_case_escapes_and_numbered_references_are_broken(
        self, tmp_path, title, upload
    ):
        wiki = FakeWiki()
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == f"File:{upload} image1.png"
        assert not overridden(wiki)

    def test_a_character_reference_of_any_letters_is_broken(self, tmp_path):
        """A title refuses "&<letters>;" whatever its letters: MediaWiki's
        pattern takes every byte beyond ASCII among them."""
        wiki = FakeWiki()
        title = "Nina &Сартина; aus &Flörburg;"
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == (
            "File:Nina -Сартина; aus -Flörburg; image1.png"
        )
        assert not overridden(wiki)

    def test_a_character_reference_made_by_normalising_is_broken(self):
        """U+037E, the Greek question mark, is ";" in NFC: the name is
        made legal in the form MediaWiki reads it in."""
        assert _upload_name("image1.png", "Löbig &amp\u037e", "x") == (
            "Löbig -amp; image1.png"
        )

    def test_spaces_underscores_and_bidi_marks(self):
        title = (
            "\u200e Familie\t Löbig_ _Flörburg\n\x85\u3000\u180e1919 \u202e"
        )
        assert _upload_name("image 1.png", title, "presentation") == (
            "Familie Löbig Flörburg 1919 image 1.png"
        )

    @pytest.mark.parametrize(
        "mark", ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d"]
    )
    def test_each_bidi_mark_mediawiki_strips_is_left_out(self, mark):
        assert _upload_name("image1.png", f"Löbig{mark} Flörburg", "x") == (
            "Löbig Flörburg image1.png"
        )

    def test_bidi_marks_go_before_the_name_is_composed(self):
        """A mark between a letter and its combining diaeresis keeps them
        apart until it goes: the name is composed (NFC) once it has."""
        upload = _upload_name("image1.png", "Lo\u200e\u0308big", "x")
        assert upload == "L\u00f6big image1.png"

    @pytest.mark.parametrize("title", ["", " ", "\u200f_\u200e", "_"])
    def test_a_title_that_leaves_nothing_names_the_container(self, title):
        assert _upload_name("page 6, image 1.png", title, "pdf") == (
            "pdf page 6, image 1.png"
        )

    def test_a_deck_title_that_leaves_nothing_names_the_container(
        self, tmp_path
    ):
        wiki = FakeWiki()
        register_deck(wiki, tmp_path, "a", "_", [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == "File:Presentation image1.png"
        assert not overridden(wiki)

    def test_a_pdf_title_that_leaves_nothing_names_the_pdf(self, tmp_path):
        """The container's kind stands for the title: a PDF's images are
        "pdf <name>"."""
        wiki = FakeWiki()
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", "_")
        record, directory = recorded(
            tmp_path, deck, "_", [("page 6, image 1.png", IMAGE, [6])]
        )
        pdf = record.model_copy(update={"container": "pdf"})
        registrar.register_derived_media(pdf, directory)
        assert uploaded(wiki, sha1(IMAGE)) == "File:Pdf page 6, image 1.png"
        assert not overridden(wiki)

    def test_a_medium_name_is_made_legal_too(self):
        assert _upload_name(".Flörburg [1].png", "Familie Löbig") == (
            "Familie Löbig -.Flörburg -1-.png"
        )

    @pytest.mark.parametrize(
        "name, title, upload",
        [
            ("..png", "Familie Löbig", "Familie Löbig -..png"),
            (".jpg", "", "-.jpg"),
            ("_.png", "", "-.png"),
            ("\u200e .png", "", "-.png"),
            ("page 6, image 1.png", " . ", "-. page 6, image 1.png"),
        ],
    )
    def test_a_part_that_would_start_with_a_dot_starts_with_a_dash(
        self, name, title, upload
    ):
        """The dot stays, a "-" before it: no part starts with a dot, which
        would make the file a hidden one on the wiki's disk, and a name
        keeps its extension."""
        assert _upload_name(name, title) == upload

    def test_a_corpus_file_name_with_nothing_before_its_extension(
        self, tmp_path
    ):
        """A corpus file "_.png" is ".png" in a title's form, which
        MediaWiki refuses (``filename-tooshort``): it is uploaded as
        "-.png"."""
        wiki = FakeWiki()
        result = register(wiki, scan(tmp_path, PORTRAIT, "_.png"))
        assert result.file_page == "File:-.png"
        assert not overridden(wiki)

    @pytest.mark.parametrize("name", ["", " ", "_", "\u200e_\u200f", "\t\n"])
    def test_a_name_that_leaves_nothing_is_never_empty(self, name):
        """The title stands as the name; with none, the name is "-": never
        empty, for which `upload_file` would take the file's own name."""
        assert _upload_name(name, "Familie Löbig") == "Familie Löbig"
        assert _upload_name(name, "", "presentation") == "presentation"
        assert _upload_name(name) == "-"

    def test_a_corpus_file_name_that_leaves_nothing(self, tmp_path):
        """A corpus file named "_" is uploaded as "-", not under its own
        name, an empty title, which MediaWiki refuses."""
        wiki = FakeWiki()
        register(wiki, scan(tmp_path, PORTRAIT, "_"))
        assert [r["filename"] for r in wiki.media.requests] == ["-"]

    def test_a_title_standing_as_the_name_is_cut_at_its_end(self):
        """With no name to keep whole, the title is cut as a name with no
        extension is: at its end, with no space before the "…"."""
        upload = _upload_name("_", self.TITLE, "presentation")
        assert upload.endswith("…") and not upload.endswith(" …")
        assert self.TITLE.startswith(upload.removesuffix("…"))
        assert 228 <= len(upload.encode()) <= 231

    def test_a_legal_short_name_is_unchanged(self):
        for name in ("portrait-1890s.png", "page 6, image 1.png"):
            assert _upload_name(name) == name
        assert _upload_name("image1.png", "Deck A", "x") == "Deck A image1.png"

    def test_a_name_of_231_bytes_is_whole_and_one_more_is_cut(self):
        name = "Brunnenring 35.png"
        title = ADDRESS[:212]
        assert _upload_name(name, title) == f"{title} {name}"
        assert len(f"{title} {name}".encode()) == 231
        # one byte more: "… Brunnenring 35.png" leaves the title 209
        # bytes, "...Brunnenring 35, ", and the space at their end goes
        longer = ADDRESS[:213]
        assert ADDRESS[:209].endswith("35, ")
        assert _upload_name(name, longer) == f"{ADDRESS[:208]}… {name}"

    @pytest.mark.parametrize("size", [228, 229, 230, 231])
    def test_a_name_that_fits_alone_is_uploaded_alone_and_whole(
        self, tmp_path, size
    ):
        """Under a title, a name of 228 to 231 bytes leaves no room for
        "… <name>": it is uploaded alone, the title left out, with no
        "…" and no byte of it lost."""
        wiki = FakeWiki()
        name = ADDRESS[: size - 4] + ".png"
        assert len(name.encode()) == size
        register_deck(wiki, tmp_path, "a", SHOP, [(name, IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == f"File:{name}"
        assert not overridden(wiki)

    def test_a_name_of_many_bytes_is_measured_in_bytes(self, tmp_path):
        """The name "Лёбиг" * 23 + ".png" takes 119 characters and 234
        bytes: too long alone, and under a title, so it is cut before its
        extension to 231 bytes."""
        wiki = FakeWiki()
        name = "Лёбиг" * 23 + ".png"
        register_deck(
            wiki, tmp_path, "a", "Familie Löbig", [(name, IMAGE, [1])]
        )
        page = uploaded(wiki, sha1(IMAGE))
        # 224 bytes, 112 letters of two, then "….png"
        assert page == f"File:{name[:112]}….png"
        assert name_bytes(page) == 231

    def test_an_extension_of_many_bytes_is_measured_in_bytes(self):
        """What follows the last dot of a name with no extension, 117
        characters of 232 bytes, leaves no room: the name is cut at its
        end."""
        name = "Brief v. " + "Лёбиг" * 23
        # "Brief v. " and 109 letters of two, 227 bytes, then "…"
        assert _upload_name(name) == f"{name[:118]}…"

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_a_long_title_is_cut_keeping_the_name(self, tmp_path, form):
        wiki = FakeWiki()
        given = unicodedata.normalize(form, self.TITLE)
        register_deck(
            wiki, tmp_path, "a", given, [("Flörburg-shop.png", IMAGE, [1])]
        )
        page = uploaded(wiki, sha1(IMAGE))
        # 231 bytes: " Flörburg-shop.png" takes 19 and "…" 3, so the title
        # keeps its longest start of at most 209 bytes
        kept = next(
            self.TITLE[:n].rstrip()
            for n in range(len(self.TITLE), 0, -1)
            if len(self.TITLE[:n].encode()) <= 209
        )
        assert page == f"File:{kept}… Flörburg-shop.png"
        assert name_bytes(page) <= 231
        assert not overridden(wiki)

    def test_a_cut_is_made_between_letters_of_many_bytes(self):
        """The title's 216 bytes end within a letter of two: the cut is
        made before it."""
        name = _upload_name("image10.png", self.CYRILLIC, "presentation")
        assert name.endswith("… image10.png")
        kept = name.removesuffix("… image10.png")
        assert self.CYRILLIC.startswith(kept)
        # 231 bytes at most, and one more letter would not have fitted
        assert len(name.encode()) == 230
        longer = self.CYRILLIC[: len(kept) + 1]
        assert len(f"{longer}… image10.png".encode()) > 231
        assert name == unicodedata.normalize("NFC", name)

    @pytest.mark.parametrize("size, kept", [(225, "Л"), (226, ""), (227, "")])
    def test_the_title_is_cut_to_nothing_before_the_name(self, size, kept):
        """A name of 227 bytes leaves room for "… " alone; of 226, for
        one byte of a title whose first letter takes two."""
        name = ADDRESS[: size - 4] + ".png"
        assert _upload_name(name, "Лёбиг", "x") == f"{kept}… {name}"

    @pytest.mark.parametrize(
        "name, upload",
        [
            (
                "Hutmacherei zum Blauen Hut.com.pdf",
                "Hutmacherei zum Blauen Hut-com.pdf",
            ),
            # in any case, each such part
            ("Brunnenring 35.Exe.JS.png", "Brunnenring 35-Exe-JS.png"),
            # a whole part, never a part's start
            ("Brief.combined.pdf", "Brief.combined.pdf"),
            # "com " is "com_" as a title holds it, which MediaWiki takes
            ("Hutmacherei.com .pdf", "Hutmacherei.com .pdf"),
            # the last part is the file's type, left as it is
            (
                "Hutmacherei zum Blauen Hut.com",
                "Hutmacherei zum Blauen Hut.com",
            ),
        ],
    )
    def test_the_dot_before_an_extension_mediawiki_refuses_is_a_dash(
        self, name, upload
    ):
        assert _upload_name(name) == upload

    # $wgProhibitedFileExtensions by default, as MediaWiki 1.45.4's
    # includes/MainConfigSchema.php lists them
    MEDIAWIKI_PROHIBITED = (
        "html", "htm", "js", "jsb", "mhtml", "mht", "xhtml", "xht",
        "php", "phtml", "php3", "php4", "php5", "phps", "phar",
        "shtml", "jhtml", "pl", "py", "cgi",
        "exe", "scr", "dll", "msi", "vbs", "bat", "com", "pif", "cmd", "vxd",
        "cpl",
        "xml",
    )  # fmt: skip

    @pytest.mark.parametrize("extension", MEDIAWIKI_PROHIBITED)
    def test_each_extension_mediawiki_refuses_loses_its_dot(self, extension):
        assert _upload_name(f"Brief.{extension}.pdf") == (
            f"Brief-{extension}.pdf"
        )

    def test_the_fake_refuses_the_extensions_mediawiki_refuses(self):
        assert set(self.MEDIAWIKI_PROHIBITED) == FakeMediaWiki.PROHIBITED

    def test_a_title_holding_an_extension_mediawiki_refuses(self, tmp_path):
        """MediaWiki refuses "Flörburg St.Pl.Dok image1.png"
        (``filetype-banned``): its part "Pl" is the extension "pl"."""
        wiki = FakeWiki()
        title = "Flörburg St.Pl.Dok"
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == (
            "File:Flörburg St-Pl.Dok image1.png"
        )
        assert not overridden(wiki)

    def test_parts_are_judged_in_the_name_as_cut(self, tmp_path):
        """The parts are those of the name as it is uploaded, cut: each
        "Pl" the cut keeps has a "-" before it, the last one too, before
        "D…"."""
        wiki = FakeWiki()
        title = ("Flörburg St.Pl.Dok " * 12).strip()
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        page = uploaded(wiki, sha1(IMAGE))
        assert page == (
            "File:"
            + "Flörburg St-Pl.Dok " * 10
            + "Flörburg St-Pl.D… image1.png"
        )
        assert name_bytes(page) == 231
        assert not overridden(wiki)

    def test_a_part_the_cut_ends_with_is_no_extension(self, tmp_path):
        """The title cut after "St.Pl" leaves "Pl… image1", which MediaWiki
        takes: its dot stays."""
        wiki = FakeWiki()
        title = "Hutmacherei zum Blauen Hut, " * 7 + "Flörburg Markt St.Pl.Dok"
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        assert uploaded(wiki, sha1(IMAGE)) == (
            "File:"
            + "Hutmacherei zum Blauen Hut, " * 7
            + "Flörburg Markt St.Pl… image1.png"
        )
        assert not overridden(wiki)

    def test_a_cut_whose_end_takes_all_the_room_keeps_none_of_the_text(self):
        # 238 bytes, more than the 231 a name takes
        end = "… " + "Лёбиг" * 23 + ".png"
        assert _cut_to_fit("Familie Löbig", end) == end

    def test_the_content_tagged_name_fits_too(self, tmp_path):
        """A cut name another file holds with other bytes is uploaded under
        its tagged form, 9 bytes longer, within MediaWiki's 240."""
        wiki = FakeWiki()
        name = _upload_name("image1.png", self.TITLE, "presentation")
        wiki.media.store(name, OTHER_IMAGE)
        register_deck(
            wiki, tmp_path, "a", self.TITLE, [("image1.png", IMAGE, [1])]
        )
        page = uploaded(wiki, sha1(IMAGE))
        stem = name.removesuffix(".png")
        assert page == f"File:{stem} {sha1(IMAGE)[:8]}.png"
        assert name_bytes(page) <= 240
        # a second write finds the file by its item: nothing is uploaded
        requests = len(wiki.media.requests)
        register_deck(
            wiki, tmp_path, "a", self.TITLE, [("image1.png", IMAGE, [1])]
        )
        assert len(wiki.media.requests) == requests

    def test_a_name_too_long_alone_keeps_its_extension(self, tmp_path):
        """With no room even for "… <name>", the title is left out and the
        medium's own name cut before its extension."""
        wiki = FakeWiki()
        long_name = "Flörburg" * 30 + ".png"
        register_deck(wiki, tmp_path, "a", SHOP, [(long_name, IMAGE, [1])])
        page = uploaded(wiki, sha1(IMAGE))
        assert page.startswith("File:Flörburg")
        assert page.endswith("….png")
        assert name_bytes(page) <= 231
        stem = page.removeprefix("File:").removesuffix("….png")
        assert long_name.startswith(stem)
        assert len(f"{long_name[: len(stem) + 1]}….png".encode()) > 231

    def test_an_extension_too_long_to_keep_is_cut_with_the_name(self):
        """A dot early in a name with no extension: what follows it is no
        extension to keep, and the name is cut at its end."""
        name = "Brief v. " + "Gustav Löbig an Nina, Flörburg; " * 9
        cut = _upload_name(name)
        assert cut.startswith("Brief v. Gustav Löbig an Nina")
        assert cut.endswith("…") and name.startswith(cut.removesuffix("…"))
        assert len(cut.encode()) <= 231
        assert len(cut.encode()) >= 228

    def test_a_long_corpus_file_name_is_cut_keeping_its_extension(
        self, tmp_path
    ):
        wiki = FakeWiki()
        long_name = ("Hutmacherei zum Blauen Hut, Flörburg, " * 7)[:240]
        long_name = long_name.strip(" ,") + ".png"
        assert len(long_name.encode()) > 240
        result = register(wiki, scan(tmp_path, PORTRAIT, long_name))
        assert result.file_page.startswith("File:Hutmacherei zum Blauen")
        assert result.file_page.endswith("….png")
        assert name_bytes(result.file_page) <= 231
        assert wiki.files == {result.file_page: sha1(PORTRAIT)}
        # a re-scan under the same name takes the tagged name, which fits
        rescan = register(
            wiki, scan(tmp_path, RESCAN, long_name, folder="rescans")
        )
        stem = result.file_page.removesuffix(".png")
        assert rescan.file_page == f"{stem} {sha1(RESCAN)[:8]}.png"
        assert name_bytes(rescan.file_page) <= 240

    def test_a_corpus_file_name_mediawiki_refuses_is_made_legal(
        self, tmp_path
    ):
        wiki = FakeWiki()
        path = scan(tmp_path, PORTRAIT, "brief #2 [1919] ~~~ {Löbig}.png")
        result = register(wiki, path)
        assert result.file_page == "File:Brief -2 -1919- -~~ -Löbig-.png"
        assert not overridden(wiki)


# "Калдовское районное бюро ЗАГС" with its stress marks: each stressed
# vowel is a letter and a combining acute accent, which NFC leaves apart
STRESS = "\N{COMBINING ACUTE ACCENT}"
STRESSED = f"Калдо{STRESS}вское ра{STRESS}йонное бю{STRESS}ро ЗАГС"


class TestCutsKeepWholeLetters:
    """A cut never falls between a letter and the combining marks after
    it: the letter goes with them. Each text is padded so that a cut by
    bytes or characters alone would keep the letter and drop its
    accent."""

    def test_a_title_cut_keeps_whole_letters(self, tmp_path):
        title = "Hutmacherei" + "x" * 5 + " " + ", ".join([STRESSED] * 4)
        wiki = FakeWiki()
        register_deck(wiki, tmp_path, "a", title, [("image1.png", IMAGE, [1])])
        page = uploaded(wiki, sha1(IMAGE))
        kept = page.removeprefix("File:").removesuffix("… image1.png")
        assert title.startswith(kept) and kept.endswith(", Калд")
        # the whole "о" with its accent went, not the accent alone
        assert title[len(kept) :].startswith(f"о{STRESS}")
        assert not overridden(wiki)

    def test_a_name_cut_before_its_extension_keeps_whole_letters(self):
        stem = "Brief" + "x" * 1 + " " + ", ".join([STRESSED] * 8)
        name = _upload_name(f"{stem}.png")
        assert name.endswith("….png")
        kept = name.removesuffix("….png")
        assert stem.startswith(kept) and kept.endswith("кое р")
        assert stem[len(kept) :].startswith(f"а{STRESS}")

    def test_a_description_cut_keeps_whole_letters(self, tmp_path):
        wiki = StoringWiki()
        description = (
            "ingested by `yizkr ingest run` from Brief"
            + "x" * 13
            + " "
            + ", ".join([STRESSED] * 12)
            + ".pdf"
        )
        result = register_artifact(
            wiki,
            media_path=scan(tmp_path, PORTRAIT),
            artifact_type="photograph",
            title=TITLE,
            description=description,
        )
        stored = wiki.entities[result.artifact_qid]["descriptions"]["en"]
        # 249 characters would end in "бю", the accent of its "ю" cut off
        assert description[248:250] == f"ю{STRESS}"
        assert stored["value"] == description[:248] + "…"

    DE = (
        "\N{REGIONAL INDICATOR SYMBOL LETTER D}"
        "\N{REGIONAL INDICATOR SYMBOL LETTER E}"
    )
    ZW = (
        "\N{REGIONAL INDICATOR SYMBOL LETTER Z}"
        "\N{REGIONAL INDICATOR SYMBOL LETTER W}"
    )
    JOINER = "\N{ZERO WIDTH JOINER}"
    FAMILY = f"\N{MAN}{JOINER}\N{WOMAN}{JOINER}\N{GIRL}"
    THUMBS = "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"
    # a subdivision's flag: a black flag, the tags "gbeng", a cancel tag
    ENGLAND = (
        "\N{WAVING BLACK FLAG}"
        + "".join(chr(0xE0000 + ord(letter)) for letter in "gbeng")
        + "\N{CANCEL TAG}"
    )
    KANA = (
        "\N{HALFWIDTH KATAKANA LETTER KA}"
        "\N{HALFWIDTH KATAKANA VOICED SOUND MARK}"
    )
    # an old Hangul syllable, whose jamo NFC leaves apart
    JAMO = (
        "\N{HANGUL CHOSEONG KIYEOK}"
        "\N{HANGUL JUNGSEONG ARAEA}"
        "\N{HANGUL JONGSEONG KIYEOK}"
    )
    JAMO_EXTENDED = "\N{HANGUL CHOSEONG KIYEOK}\N{HANGUL JUNGSEONG O-YEO}"
    CONJUNCT = (
        "\N{DEVANAGARI LETTER KA}"
        "\N{DEVANAGARI SIGN VIRAMA}"
        "\N{DEVANAGARI LETTER SSA}"
    )

    @pytest.mark.parametrize(
        "text, cut, kept",
        [
            # a letter with two marks: all three go
            (f"Ра{STRESS}{STRESS}йон", f"Ра{STRESS}", "Р"),
            # a letter whose marks leave no room keeps nothing
            (f"а{STRESS}{STRESS}б", "а", ""),
            (STRESS * 5, STRESS * 3, ""),
            # a zero-width joiner joins the letters either side: both go
            (f"Löbig a{JOINER}b", f"Löbig a{JOINER}", "Löbig "),
            (f"Flörburg {FAMILY}", f"Flörburg {FAMILY[:3]}", "Flörburg "),
            (f"Flörburg {FAMILY}", "Flörburg \N{MAN}", "Flörburg "),
            # a zero-width non-joiner belongs to the letter before it
            (
                "Löbig a\N{ZERO WIDTH NON-JOINER}b",
                "Löbig a",
                "Löbig ",
            ),
            # a skin tone belongs to its emoji
            (f"Flörburg {THUMBS}", f"Flörburg {THUMBS[0]}", "Flörburg "),
            # the tags belong to their flag
            (f"Flörburg {ENGLAND}", f"Flörburg {ENGLAND[:3]}", "Flörburg "),
            # a halfwidth sound mark belongs to its kana
            (f"Flörburg {KANA}", f"Flörburg {KANA[0]}", "Flörburg "),
            # a vowel or final jamo belongs to its syllable
            (f"Flörburg {JAMO}", f"Flörburg {JAMO[:2]}", "Flörburg "),
            (
                f"Flörburg {JAMO_EXTENDED}",
                f"Flörburg {JAMO_EXTENDED[0]}",
                "Flörburg ",
            ),
            # a letter after a virama belongs to the conjunct
            (f"Flörburg {CONJUNCT}", f"Flörburg {CONJUNCT[:2]}", "Flörburg "),
            # half a flag goes, a whole one stays
            (f"Flörburg {DE}{DE}", f"Flörburg {DE}{DE[0]}", f"Flörburg {DE}"),
            (f"Flörburg {DE}{DE}", f"Flörburg {DE}", f"Flörburg {DE}"),
            (f"Flörburg {ZW}{ZW}", f"Flörburg {ZW}{ZW[0]}", f"Flörburg {ZW}"),
            (f"Flörburg {ZW}", f"Flörburg {ZW}", f"Flörburg {ZW}"),
            # a lone indicator, the first of their range, goes
            (
                "Flörburg \N{REGIONAL INDICATOR SYMBOL LETTER A}",
                "Flörburg \N{REGIONAL INDICATOR SYMBOL LETTER A}",
                "Flörburg ",
            ),
            # a cut that ends after a whole letter is kept as it is
            (f"ра{STRESS}йон", f"ра{STRESS}", f"ра{STRESS}"),
        ],
    )
    def test_a_cut_ends_after_a_whole_letter(self, text, cut, kept):
        assert _whole_letters(text, cut) == kept

    def test_a_cut_backed_off_to_a_space_ends_before_it(self):
        """The cut's 228 bytes end in a flag's first tag character: the
        flag goes, and so does the space before it."""
        text = "x" * 222 + " " + self.ENGLAND
        assert _cut_to_fit(text, "…") == "x" * 222 + "…"

    def test_a_cut_ending_in_a_space_backs_off_from_before_it(self):
        """The cut's 228 bytes end in the space after a joiner: the space
        is trimmed first, so the joiner, and the letter it joins, go
        too, and no joiner is left before the "…"."""
        text = "x" * 223 + f"a{self.JOINER} " + "b" * 20
        assert _cut_to_fit(text, "…") == "x" * 223 + "…"

    @pytest.mark.parametrize(
        "unit",
        [
            f"а{STRESS}",
            FAMILY,
            THUMBS,
            ENGLAND,
            KANA,
            JAMO,
            CONJUNCT,
            f"{DE}",
        ],
    )
    @pytest.mark.parametrize("pad", range(8))
    def test_an_upload_name_ends_after_a_whole_letter(self, unit, pad):
        """However the bytes fall, the title is cut after a whole
        letter: what it keeps of the repeated letter is whole letters."""
        before = "Hutmacherei" + "Ф" * pad + " "
        name = _upload_name("image1.png", before + unit * 80, "presentation")
        kept = name.removesuffix("… image1.png")
        assert name.endswith("… image1.png") and kept.startswith(
            before.rstrip()
        )
        rest = kept[len(before) :]
        assert rest == unit * (len(rest) // len(unit))


class TestLongDescriptions:
    """A description is cut to the 250 characters Wikibase takes, ending
    in "…": a longer one would be refused before the artifact had an item
    (StoringWiki refuses it, as the instance does)."""

    def test_a_long_description_is_cut_with_a_mark(self, tmp_path):
        wiki = StoringWiki()
        description = "Brief von Gustav Löbig aus Flörburg, " * 10
        result = register_artifact(
            wiki,
            media_path=scan(tmp_path, PORTRAIT),
            artifact_type="photograph",
            title=TITLE,
            description=description,
        )
        stored_description = wiki.entities[result.artifact_qid][
            "descriptions"
        ]["en"]["value"]
        assert len(stored_description) <= 250
        assert stored_description == description[:249].rstrip() + "…"

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    @pytest.mark.parametrize("length", [249, 250])
    def test_a_description_that_fits_is_unchanged(
        self, tmp_path, length, form
    ):
        """Its length is counted in the form Wikibase stores it in: one
        given decomposed (NFD) has more characters, and is not cut."""
        wiki = StoringWiki()
        description = ("Brief von Gustav Löbig aus Flörburg " * 10)[:length]
        description = description[:-1] + "x"
        result = register_artifact(
            wiki,
            media_path=scan(tmp_path, PORTRAIT),
            artifact_type="photograph",
            title=TITLE,
            description=unicodedata.normalize(form, description),
        )
        entity = wiki.entities[result.artifact_qid]
        assert entity["descriptions"]["en"]["value"] == description


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

    def test_of_two_fragments_for_a_page_the_earliest_is_taken(self, tmp_path):
        """Two fragments for one page of an artifact are a duplicate the
        key exists to prevent: the numerically earliest is taken, and no
        third is made."""
        wiki = FakeWiki()
        path = scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf")
        qid = register(wiki, path, EXTRACT_TITLE, "certificate").artifact_qid
        first, second = (
            wiki.item(
                f"first page, copy {n}",
                ("instance_of", ITEMS["artifact_fragment"], None),
                ("part_of", qid, "1"),
            )
            for n in (1, 2)
        )
        result = register(wiki, path, EXTRACT_TITLE, "certificate", pages=1)
        assert result.fragment_qids == {1: first} and first != second

    def test_a_leftover_with_a_fragments_label_is_not_taken(self, tmp_path):
        """An item with a fragment's label and no statements, as a
        registration cut short between the two writes that made a fragment
        before #103 could leave, is not taken for the fragment (#368): the
        fragment is made beside it, its label tagged, and the leftover is
        left as it is."""
        wiki = FakeWiki()
        leftover = wiki.item(f"{EXTRACT_TITLE}, page 1")
        before = copy.deepcopy(wiki.entities[leftover])

        result = register(
            wiki,
            scan(tmp_path, EXTRACT, name="birth-extract-1938.pdf"),
            EXTRACT_TITLE,
            "certificate",
            pages=1,
        )

        fragment = result.fragment_qids[1]
        assert fragment != leftover
        assert wiki.entities[leftover] == before
        kinds, parts = fragment_statements(wiki, fragment)
        assert kinds == [ITEMS["artifact_fragment"]]
        assert parts == {(result.artifact_qid, "1"): [result.artifact_qid]}
        assert label_of(wiki, fragment) == (
            f"{EXTRACT_TITLE}, page 1 [{sha1(EXTRACT)[:8]}]"
        )
        assert_labels_unique(wiki)

    def test_a_fragment_label_a_registrar_gave_is_taken(self, tmp_path):
        """A registrar keeps the labels it gives fragments in its reading:
        an artifact it registers later under one of them is tagged."""
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        registrar.register_artifact(
            scan(tmp_path, EXTRACT, name="e.pdf"),
            "certificate",
            EXTRACT_TITLE,
            pages=1,
        )
        later = registrar.register_artifact(
            scan(tmp_path, SECOND_COPY, name="p.pdf"),
            "certificate",
            f"{EXTRACT_TITLE}, page 1",
        )
        assert label_of(wiki, later.artifact_qid) == (
            f"{EXTRACT_TITLE}, page 1 [{sha1(SECOND_COPY)[:8]}]"
        )
        assert_labels_unique(wiki)


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

    def test_one_registrar_makes_one_item_for_an_image_two_decks_embed(
        self, tmp_path
    ):
        """A registrar keeps the media it registers in its reading: a
        second deck embedding the image finds its item (as one registrar
        for a whole corpus does)."""
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        found = []
        for name, title in (("a", "Deck A"), ("b", "Deck B")):
            deck = deck_file(tmp_path, name)
            registrar.register_artifact(deck, "presentation", title)
            record, directory = recorded(
                tmp_path, deck, title, [("image1.png", IMAGE, [1])]
            )
            found.append(registrar.register_derived_media(record, directory))
        assert found[0] == found[1]
        assert [
            qid
            for qid in wiki.entities
            if sha1(IMAGE) in strings(wiki, qid, "media_sha1")
        ] == list(found[0].values())

    def test_a_medium_label_a_registrar_gave_is_taken(self, tmp_path):
        """A registrar keeps the labels it gives media in its reading: an
        artifact it registers later under one of them is tagged."""
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "a")
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        registrar.register_derived_media(record, directory)
        later = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Deck A — image1.png"
        )
        assert label_of(wiki, later.artifact_qid) == (
            f"Deck A — image1.png [{sha1(PORTRAIT)[:8]}]"
        )
        assert_labels_unique(wiki)

    def test_a_medium_listed_by_another_of_its_names_is_left_out(
        self, tmp_path
    ):
        """Bytes the archive holds under two names are one medium, and the
        list naming either name leaves it out: never registered or
        uploaded."""
        wiki = FakeWiki()
        registrar = Registrar(
            wiki, exclusions=ExclusionList.parse("image4.png\n")
        )
        deck = deck_file(tmp_path, "a")
        registrar.register_artifact(deck, "presentation", SHOP)
        record, directory = recorded(
            tmp_path,
            deck,
            SHOP,
            [
                ("image2.png", IMAGE, [2]),
                ("image4.png", IMAGE, [3]),
                ("image5.png", OTHER_IMAGE, [4]),
            ],
        )
        derived = registrar.register_derived_media(record, directory)
        assert list(derived) == [sha1(OTHER_IMAGE)]
        assert sha1(IMAGE) not in wiki.files.values()
        assert not any(
            sha1(IMAGE) in strings(wiki, qid, "media_sha1")
            for qid in wiki.entities
        )

    def test_a_kept_medium_reached_through_a_link_is_refused(self, tmp_path):
        """The file a record keeps must be a regular file: one replaced by a
        link is never uploaded through, even to the medium's own bytes."""
        wiki = FakeWiki()
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "a")
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        (medium,) = record.media.values()
        elsewhere = tmp_path / "elsewhere.png"
        elsewhere.write_bytes(IMAGE)
        kept = directory / medium.file
        kept.unlink()
        kept.symlink_to(elsewhere)
        requests, writes = len(wiki.media.requests), wiki.writes
        with pytest.raises(RuntimeError, match="does not hold the bytes"):
            registrar.register_derived_media(record, directory)
        assert (len(wiki.media.requests), wiki.writes) == (requests, writes)

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

    def test_a_long_file_name_is_described_within_the_limit(self, tmp_path):
        """The description "ingested by `yizkr ingest run` from <file
        name>" passes 250 characters for a name of more than 214: it is
        cut, ending in "…", and the artifact is made (StoringWiki refuses
        a longer term)."""
        wiki = StoringWiki()
        name = ("Hutmacherei zum Blauen Hut, Brunnenring 35, " * 6)[:230]
        name = name.strip(" ,") + ".png"
        orchestrator, _ = self.orchestrator(tmp_path, wiki, [(name, PORTRAIT)])
        (doc,) = orchestrator.documents()
        qid = orchestrator._step_register(doc)
        entity = wiki.entities[qid]
        whole = f"ingested by `yizkr ingest run` from {name}"
        assert len(whole) > 250
        assert entity["descriptions"]["en"]["value"] == (
            whole[:249].rstrip() + "…"
        )
        assert label_of(wiki, qid) == name.removesuffix(".png")
        # its file name, of 234 bytes, is cut to fit too
        (page,) = strings(wiki, qid, "media_file")
        assert page.endswith("….png") and name_bytes(page) <= 231
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

    def test_each_write_reads_the_instance_afresh(self, tmp_path):
        """Each write's registrar reads the instance anew, so a label an
        earlier write gave an item (a person the writer minted, say) is
        taken: no duplicate label."""
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(
            tmp_path,
            wiki,
            [("a-portrait.png", PORTRAIT), ("studio.png", RESCAN)],
        )
        first, second = orchestrator.documents()
        orchestrator._step_register(first)
        wiki.item("studio")  # what the first write minted, between the two
        qid = orchestrator._step_register(second)
        assert label_of(wiki, qid) == f"studio [{sha1(RESCAN)[:8]}]"
        assert_labels_unique(wiki)

    def test_a_decks_write_reads_the_instance_once(self, tmp_path):
        """One registrar per write: the deck, its slides and its media in
        one reading of the instance."""
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(tmp_path, wiki)
        build_deck(orchestrator.folder / DECK)
        (doc,) = orchestrator.documents()
        orchestrator._step_register(doc)
        assert wiki.reads == 1

    def test_a_record_made_for_other_bytes_is_made_again(self, tmp_path):
        """A derived-media record in the deck's directory made for other
        deck bytes (as in a directory two sources shared) is made again at
        the write: the deck's own images are registered, as part of it,
        and nothing is made part of the other deck."""
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(tmp_path, wiki)
        build_deck(orchestrator.folder / DECK)
        (doc,) = orchestrator.documents()
        other = deck_file(tmp_path, "other")
        other_qid = (
            Registrar(wiki)
            .register_artifact(other, "presentation", "Deck other")
            .artifact_qid
        )
        record_derived_media(
            PresentationContent(
                slides=[Slide(number=1, text="")],
                media=[
                    EmbeddedMedium(name="image1.png", data=IMAGE, slides=[1])
                ],
            ),
            source_sha1=sha1(other.read_bytes()),
            title="Deck other",
            directory=doc.dir,
        )
        deck = orchestrator._step_register(doc)
        assert deck != other_qid
        assert parts_of(wiki, other_qid)["medium"] == {}
        assert sorted(parts_of(wiki, deck)["medium"]) == ["1", "2", "3"]


class TestRegisterCommand:
    def test_it_needs_the_media_file(self, capsys):
        """`yizkr ingest register` finds or makes the artifact by its media's
        SHA-1, so it refuses to run without the media file."""
        from yizkr.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(
                [
                    "ingest",
                    "register",
                    "--type",
                    "photograph",
                    "--title",
                    TITLE,
                ]
            )
        assert "--file" in capsys.readouterr().err


# -- an item's kind, stated in the item itself (#368) -------------------------

# a run item, labelled as the writer labels one: a date, a model and the
# batch's fingerprint
RUN_LABEL = "extraction run 2026-07-12 fake-model 0a9afd5746"


def kinds_of(wiki, qid: str) -> list[tuple[str, str, list[str]]]:
    """(kind, rank, the items it is stated in) of each `instance of`
    statement of an item."""
    return [
        (value(statement), statement["rank"], cited(statement))
        for statement in wiki.entities[qid]["claims"].get(P["instance_of"], [])
    ]


def typed(wiki, qid: str, **options) -> bool:
    return ensure_self_typed(
        wiki, qid, "extraction_run", summary="run item statements", **options
    )


class TestEnsureSelfTyped:
    """A run item's kind cites the item itself, so it is written after the
    write that makes the item. Whatever finds an item that a write cut short
    left without it gives it that kind; an item that states it already, in
    any way, is left as it is."""

    def test_an_item_without_its_kind_gets_it_stated_in_itself(self):
        wiki = FakeWiki()
        qid = wiki.item(RUN_LABEL)
        writes = wiki.writes

        assert typed(wiki, qid) is True

        assert wiki.writes == writes + 1
        assert kinds_of(wiki, qid) == [
            (ITEMS["extraction_run"], "normal", [qid])
        ]
        assert label_of(wiki, qid) == RUN_LABEL

    def test_an_item_that_states_its_kind_gets_no_write(self):
        wiki = FakeWiki()
        qid = wiki.item(
            RUN_LABEL, ("instance_of", ITEMS["extraction_run"], None)
        )
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        assert typed(wiki, qid) is False

        assert wiki.writes == writes
        assert wiki.entities == before

    def test_its_kind_under_another_reference_is_left_as_it_is(self):
        """A curator's statement of the kind, cited to another item, types
        the item: it gains no statement and no reference."""
        wiki = FakeWiki()
        elsewhere = wiki.item("model manifest")
        qid = wiki.item(
            RUN_LABEL,
            ("instance_of", ITEMS["extraction_run"], None),
            stated_in=elsewhere,
        )
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        assert typed(wiki, qid) is False

        assert wiki.writes == writes
        assert wiki.entities == before
        assert kinds_of(wiki, qid) == [
            (ITEMS["extraction_run"], "normal", [elsewhere])
        ]

    def test_a_kind_a_curator_deprecated_is_left_alone(self):
        """The kind at deprecated rank is still stated: the curator's rank
        stands (CLAUDE.md rules 3 and 4), and no second statement is put
        beside it."""
        wiki = FakeWiki()
        qid = wiki.item(
            RUN_LABEL, ("instance_of", ITEMS["extraction_run"], None)
        )
        (statement,) = wiki.entities[qid]["claims"][P["instance_of"]]
        statement["rank"] = "deprecated"
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        assert typed(wiki, qid) is False

        assert wiki.writes == writes
        assert wiki.entities == before
        assert kinds_of(wiki, qid) == [
            (ITEMS["extraction_run"], "deprecated", [qid])
        ]

    def test_another_kind_is_kept_beside_it(self):
        """An item that states another kind only gains this one beside it:
        nothing it states is replaced or removed."""
        wiki = FakeWiki()
        qid = wiki.item(RUN_LABEL, ("instance_of", ITEMS["artifact"], None))

        assert typed(wiki, qid) is True

        assert kinds_of(wiki, qid) == [
            (ITEMS["artifact"], "normal", [qid]),
            (ITEMS["extraction_run"], "normal", [qid]),
        ]

    def test_a_dry_run_writes_nothing(self):
        wiki = FakeWiki()
        qid = wiki.item(RUN_LABEL)
        before, writes = copy.deepcopy(wiki.entities), wiki.writes

        # the item lacks its kind, and the dry run says so
        assert typed(wiki, qid, dry_run=True) is True

        assert wiki.writes == writes
        assert wiki.entities == before
