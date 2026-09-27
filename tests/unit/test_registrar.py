"""Offline tests for the content-identity dedup key (ADR-0010).

Covered here: the lag-free lookup primitive the registrar relies on, the
additive statement helper derived-media registration uses (#50), and
`register_artifact` itself over the in-memory wiki of test_presentation,
whose uploads run the real `WikiClient.upload_file` against a fake
MediaWiki (#186). The flow is also verified live
(tests/integration/test_live_pipeline.py, test_live_registrar.py).
"""

from __future__ import annotations

import copy
import hashlib

from tests.unit.test_presentation import FakeWiki
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.registrar import (
    _cites,
    _ensure_referenced,
    _item_claim,
    _string_claim,
    register_artifact,
)


class FakeIndexClient(WikiClient):
    """A WikiClient whose entity enumeration is scripted, so
    string_statement_index can be exercised without an instance."""

    def __init__(self, entities: dict[str, dict]) -> None:
        self._entities = entities

    def list_entity_ids(self):
        return list(self._entities)

    def api_get(self, params):
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


# -- register_artifact: media file never drifts from media SHA-1 (#186) ------

# Fictional media (docs/EXAMPLES.md): two different scans of the Löbig
# family's studio portrait of the 1890s.
PORTRAIT = b"\x89PNG synthetic scan of the 1890s studio portrait"
RESCAN = b"\x89PNG synthetic re-scan of the 1890s studio portrait"
TITLE = "Studio portrait of the Löbig family"
P = FakeWiki.PROPERTIES


def scan(tmp_path, data: bytes, name="portrait-1890s.png", folder="scans"):
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def register(wiki, path, title=TITLE):
    return register_artifact(
        wiki, media_path=path, artifact_type="photograph", title=title
    )


def strings(wiki, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in wiki.entities[qid]["claims"].get(P[key], [])
    ]


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class TestRegisterArtifact:
    def test_first_registration_uploads_and_hashes(self, tmp_path):
        wiki = FakeWiki()
        result = register(wiki, scan(tmp_path, PORTRAIT))
        assert result.created
        assert result.file_page == "File:Portrait-1890s.png"
        assert strings(wiki, result.artifact_qid, "media_file") == [
            "File:Portrait-1890s.png"
        ]
        assert strings(wiki, result.artifact_qid, "media_sha1") == [
            sha1(PORTRAIT)
        ]
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
        entity = wiki.entities[changed.artifact_qid]
        assert entity["labels"]["en"]["value"] == f"{TITLE} [{tag}]"
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

    def test_pre_hash_item_holding_the_bytes_is_adopted(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        del wiki.entities[first.artifact_qid]["claims"][P["media_sha1"]]

        again = register(wiki, scan(tmp_path, PORTRAIT))

        assert again.artifact_qid == first.artifact_qid
        assert strings(wiki, first.artifact_qid, "media_sha1") == [
            sha1(PORTRAIT)
        ]
        assert len(wiki.files) == 1

    def test_pre_hash_item_holding_other_bytes_is_not_adopted(self, tmp_path):
        # a same-title item without a hash whose file holds different
        # bytes is different content: backfilling this hash onto it would
        # make its media file show other bytes than its media SHA-1
        wiki = FakeWiki()
        first = register(wiki, scan(tmp_path, PORTRAIT))
        del wiki.entities[first.artifact_qid]["claims"][P["media_sha1"]]
        before = copy.deepcopy(wiki.entities[first.artifact_qid])

        changed = register(wiki, scan(tmp_path, RESCAN, folder="rescans"))

        assert changed.artifact_qid != first.artifact_qid
        assert wiki.entities[first.artifact_qid] == before
        assert strings(wiki, changed.artifact_qid, "media_sha1") == [
            sha1(RESCAN)
        ]
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
