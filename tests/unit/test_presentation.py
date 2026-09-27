"""Offline tests for the PPTX adapter (ADR-0008). Fixture deck is
assembled in-test with zipfile — the repo carries no personal data."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest

from tests.unit.test_upload_warnings import FakeMediaWiki, FakeUploadClient
from yizkr.pipeline.presentation import (
    EmbeddedMedium,
    PresentationContent,
    Slide,
    register_derived_media,
    slide_transcript,
    unpack_pptx,
)

CT = "[Content_Types].xml"


def slide_xml(*paragraphs: str) -> str:
    body = "".join(
        "<a:p>"
        + "".join(f"<a:r><a:t>{run}</a:t></a:r>" for run in runs)
        + "</a:p>"
        for runs in paragraphs
    )
    return (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
    )


def rels_xml(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>'
        for rid, kind, target in entries
    )
    return (
        "<Relationships xmlns="
        '"http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{body}</Relationships>"
    )


IMAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
SLIDE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
NOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def build_deck(path, reorder=False):
    """Two slides; slide FILE 1 shows an image and has notes; the deck's
    sldIdLst can present the files in reversed order."""
    order = (
        [("rId2", "slides/slide2.xml"), ("rId1", "slides/slide1.xml")]
        if reorder
        else [("rId1", "slides/slide1.xml"), ("rId2", "slides/slide2.xml")]
    )
    presentation = (
        "<p:presentation xmlns:p="
        '"http://schemas.openxmlformats.org/presentationml/2006/main"'
        " xmlns:r="
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(
            f'<p:sldId id="25{i}" r:id="{rid}"/>'
            for i, (rid, _) in enumerate(order)
        )
        + "</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(CT, "<Types/>")
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            rels_xml([(rid, SLIDE_REL, target) for rid, target in order]),
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            slide_xml(
                ["The Beispiel family", " in Springfield"], ["Married 1901"]
            ),
        )
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            rels_xml(
                [
                    ("rId7", IMAGE_REL, "../media/image1.jpeg"),
                    ("rId8", NOTES_REL, "../notesSlides/notesSlide1.xml"),
                ]
            ),
        )
        archive.writestr(
            "ppt/slides/slide2.xml", slide_xml(["Anna's certificate"])
        )
        archive.writestr(
            "ppt/slides/_rels/slide2.xml.rels",
            rels_xml(
                [
                    ("rId7", IMAGE_REL, "../media/image1.jpeg"),
                    ("rId9", IMAGE_REL, "../media/clip1.mp4"),
                ]
            ),
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            slide_xml(["Scanned from the family album"]),
        )
        archive.writestr("ppt/media/image1.jpeg", b"\xff\xd8fakejpeg")
        archive.writestr("ppt/media/clip1.mp4", b"fakemp4")
    return path


class TestUnpack:
    def test_slides_text_and_notes(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        assert [slide.number for slide in content.slides] == [1, 2]
        assert (
            content.slides[0].text
            == "The Beispiel family in Springfield\nMarried 1901"
        )
        assert content.slides[0].notes == "Scanned from the family album"
        assert content.slides[1].text == "Anna's certificate"
        assert content.slides[1].notes == ""

    def test_slide_order_follows_presentation_not_filenames(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx", reorder=True))
        # sldIdLst puts file 2 first: it becomes slide NUMBER 1
        assert content.slides[0].text == "Anna's certificate"
        assert content.slides[0].number == 1
        assert "Married 1901" in content.slides[1].text

    def test_media_mapped_to_slides_and_deduplicated(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        (image,) = content.media
        assert image.name == "image1.jpeg"
        assert image.slides == [1, 2]  # same image used on both slides
        assert image.data.startswith(b"\xff\xd8")

    def test_non_image_media_counted_not_extracted(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        assert content.skipped_media == {".mp4": 1}

    def test_non_pptx_raises(self, tmp_path):
        bogus = tmp_path / "bogus.pptx"
        with zipfile.ZipFile(bogus, "w") as archive:
            archive.writestr("hello.txt", "not a deck")
        with pytest.raises(ValueError, match="not a PPTX"):
            unpack_pptx(bogus)


class TestTranscript:
    def test_slide_markers_scope_the_text(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        transcript = slide_transcript(content)
        assert transcript.index("[Slide 1]") < transcript.index("Married 1901")
        assert (
            transcript.index("Married 1901")
            < transcript.index("[Slide 1 notes]")
            < transcript.index("Scanned from the family album")
            < transcript.index("[Slide 2]")
            < transcript.index("Anna's certificate")
        )


class TestOrchestratorBranch:
    def test_pptx_routes_through_adapter(self, tmp_path):
        from tests.unit.test_orchestrate import ScriptedOrchestrator, quiet

        folder = tmp_path / "docs"
        folder.mkdir()
        build_deck(folder / "Family Deck.pptx")

        from yizkr.pipeline.orchestrate import Orchestrator

        class ScriptedDeck(ScriptedOrchestrator):
            def _step_register(self, doc):
                self.calls.append(f"register:{doc.source.name}")
                # the register step asks the adapter for the slide count
                assert len(self._presentation(doc).slides) == 2
                return "Q500"

            def _step_batch(self, doc):
                # use the REAL branch: deterministic pptx transcription,
                # then the (scripted) extract below
                self.calls.append(f"batch:{doc.source.name}")
                return Orchestrator._step_batch(self, doc)

            def _step_derive_media(self, doc):
                self.calls.append(f"derive:{doc.source.name}")
                return {"image1.jpeg": "Q501"}

            def _step_extract(self, doc, transcription):
                self.calls.append("extract")
                assert transcription.engine == "pptx-adapter"
                assert "[Slide 2]" in transcription.text
                from tests.unit.test_orchestrate import make_batch

                return make_batch()

        orchestrator = ScriptedDeck(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert "derive:Family Deck.pptx" in orchestrator.calls
        assert doc.state.detail["derived_media"] == {"image1.jpeg": "Q501"}
        # derived media are registered exactly once across re-runs
        orchestrator.run()
        assert orchestrator.calls.count("derive:Family Deck.pptx") == 1


# -- derived-media registration (ADR-0008, ADR-0010; #50) --------------------


class FakeWiki:
    """In-memory stand-in for WikiClient: real WBI item objects over a
    JSON store that round-trips them as the API does, lag-free label and
    hash lookups, and the real `WikiClient.upload_file` over a fake
    MediaWiki upload API (tests/unit/test_upload_warnings.py)."""

    PROPERTIES = {
        "instance_of": "P1",
        "stated_in": "P2",
        "part_of": "P3",
        "page_number": "P6",
        "media_file": "P57",
        "media_sha1": "P58",
    }
    ITEMS = {"photograph": "Q2", "artifact": "Q3"}

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
        self._next_qid = 100
        self._next_claim = 0

    def store(self, entity):
        data = entity.get_json()
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

    # -- the WikiClient surface register_derived_media uses ---------------

    def vocabulary(self):
        return self.PROPERTIES, self.ITEMS

    def find_item_by_label(self, label):
        for qid, entity in self.entities.items():
            if entity["labels"].get("en", {}).get("value") == label:
                return qid
        return None

    def string_statement_index(self, pid):
        index: dict[str, str] = {}
        for qid in sorted(self.entities, key=lambda q: int(q[1:])):
            for statement in self.entities[qid]["claims"].get(pid, []):
                index.setdefault(
                    statement["mainsnak"]["datavalue"]["value"], qid
                )
        return index

    @property
    def files(self) -> dict[str, str]:
        """File page -> SHA-1 of the file's current bytes."""
        return {
            f"File:{name.replace('_', ' ')}": sha1
            for name, sha1 in self.media.current().items()
        }

    def upload_file(self, path):
        # the real upload logic; whatever it does, no stored file may gain
        # a version and no stored bytes may gain a second file
        before = copy.deepcopy(self.media.versions)
        page = FakeUploadClient(self.media).upload_file(path)
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


IMAGE = b"\x89PNG synthetic image bytes"
OTHER_IMAGE = b"\x89PNG different synthetic bytes"
P, ITEMS = FakeWiki.PROPERTIES, FakeWiki.ITEMS


def deck_content(slides: list[int], data: bytes = IMAGE):
    return PresentationContent(
        slides=[Slide(number=n, text="") for n in range(1, max(slides) + 1)],
        media=[EmbeddedMedium(name="image1.png", data=data, slides=slides)],
    )


def register(wiki, tmp_path, deck_qid, deck_title, content):
    return register_derived_media(
        wiki,
        deck_qid=deck_qid,
        deck_title=deck_title,
        content=content,
        workdir=tmp_path / deck_qid,
    )


def value(statement):
    datavalue = statement["mainsnak"]["datavalue"]["value"]
    return datavalue["id"] if isinstance(datavalue, dict) else datavalue


def cited(statement) -> list[str]:
    return [
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(P["stated_in"], [])
    ]


def part_of(entity) -> dict[tuple[str, str], list[str]]:
    """(deck, slide) -> the decks each part-of statement is stated in."""
    return {
        (
            value(statement),
            statement["qualifiers"][P["page_number"]][0]["datavalue"]["value"],
        ): cited(statement)
        for statement in entity["claims"][P["part_of"]]
    }


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
        derived = register(
            wiki, tmp_path, "Q10", "Deck A", deck_content([1, 2])
        )
        (qid,) = derived.values()
        entity = wiki.entities[qid]
        assert entity["labels"]["en"]["value"] == "Deck A — image1.png"
        assert part_of(entity) == {
            ("Q10", "1"): ["Q10"],
            ("Q10", "2"): ["Q10"],
        }
        (sha1,) = entity["claims"][P["media_sha1"]]
        assert value(sha1) == hashlib.sha1(IMAGE).hexdigest()
        # every statement is referenced stated in -> the deck (ADR-0008)
        for statements in entity["claims"].values():
            for statement in statements:
                assert cited(statement) == ["Q10"]
        assert wiki.writes == 1

    def test_second_deck_is_appended_never_replacing_the_first(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        before = copy.deepcopy(wiki.entities[first["image1.png"]])

        second = register(
            wiki, tmp_path, "Q20", "Deck B", deck_content([2, 3])
        )

        assert second == first  # same bytes: one artifact (ADR-0010)
        after = wiki.entities[first["image1.png"]]
        assert_additive(before, after)
        assert part_of(after) == {
            ("Q10", "1"): ["Q10"],
            ("Q20", "2"): ["Q20"],
            ("Q20", "3"): ["Q20"],
        }
        # the deck-independent statements stay single, cited by both decks
        for pid in ("instance_of", "media_file", "media_sha1"):
            (statement,) = after["claims"][P[pid]]
            assert cited(statement) == ["Q10", "Q20"]
        assert (
            value(after["claims"][P["instance_of"]][0]) == ITEMS["photograph"]
        )
        # the later deck's name is an alias; the blob is stored once
        assert [a["value"] for a in after["aliases"]["en"]] == [
            "Deck B — image1.png"
        ]
        assert len(wiki.files) == 1
        assert wiki.drift() == []

    def test_regenerated_deck_with_the_same_title(self, tmp_path):
        # the #50 case: new bytes make a new deck item, the image is reused
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        again = register(wiki, tmp_path, "Q11", "Deck A", deck_content([1]))
        assert again == first
        after = wiki.entities[first["image1.png"]]
        assert part_of(after) == {("Q10", "1"): ["Q10"], ("Q11", "1"): ["Q11"]}
        assert "aliases" not in after or not after["aliases"].get("en")
        assert len(wiki.files) == 1  # nothing uploaded a second time

    def test_rerun_writes_nothing(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        register(wiki, tmp_path, "Q20", "Deck B", deck_content([2]))
        snapshot, writes = copy.deepcopy(wiki.entities), wiki.writes
        assert (
            register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
            == first
        )
        assert (
            register(wiki, tmp_path, "Q20", "Deck B", deck_content([2]))
            == first
        )
        assert wiki.writes == writes
        assert wiki.entities == snapshot

    def test_pre_hash_item_is_adopted_and_backfilled(self, tmp_path):
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        qid = first["image1.png"]
        del wiki.entities[qid]["claims"][P["media_sha1"]]  # a pre-hash item
        before = copy.deepcopy(wiki.entities[qid])

        again = register(wiki, tmp_path, "Q11", "Deck A", deck_content([1]))

        assert again == first
        after = wiki.entities[qid]
        assert_additive(before, after)
        (sha1,) = after["claims"][P["media_sha1"]]
        assert value(sha1) == hashlib.sha1(IMAGE).hexdigest()
        assert cited(sha1) == ["Q11"]
        assert part_of(after) == {("Q10", "1"): ["Q10"], ("Q11", "1"): ["Q11"]}
        # adopted because its wiki file holds these bytes; not re-uploaded
        assert len(wiki.files) == 1

    @pytest.mark.parametrize("pre_hash", [False, True])
    def test_changed_image_under_the_same_name_is_distinct(
        self, tmp_path, pre_hash
    ):
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        if pre_hash:
            del wiki.entities[first["image1.png"]]["claims"][P["media_sha1"]]
        before = copy.deepcopy(wiki.entities)

        changed = register(
            wiki, tmp_path, "Q11", "Deck A", deck_content([1], OTHER_IMAGE)
        )

        assert changed != first
        tag = hashlib.sha1(OTHER_IMAGE).hexdigest()[:8]
        entity = wiki.entities[changed["image1.png"]]
        assert (
            entity["labels"]["en"]["value"] == f"Deck A — image1.png [{tag}]"
        )
        assert part_of(entity) == {("Q11", "1"): ["Q11"]}
        assert wiki.files[f"File:Deck A image1 {tag}.png"] == (
            hashlib.sha1(OTHER_IMAGE).hexdigest()
        )
        assert wiki.drift() == []
        # the earlier artifact is not touched at all
        assert (
            wiki.entities[first["image1.png"]] == before[first["image1.png"]]
        )

        # re-running the changed deck writes nothing, and the tagged item
        # never takes the earlier item's label as an alias
        snapshot, writes = copy.deepcopy(wiki.entities), wiki.writes
        again = register(
            wiki, tmp_path, "Q11", "Deck A", deck_content([1], OTHER_IMAGE)
        )
        assert again == changed
        assert wiki.writes == writes
        assert wiki.entities == snapshot
        tagged = wiki.entities[changed["image1.png"]]
        aliases = tagged.get("aliases", {}).get("en", [])
        assert "Deck A — image1.png" not in [a["value"] for a in aliases]

    def test_reused_image_never_aliases_another_items_label(self, tmp_path):
        # a deck whose image is already another deck's medium: its own
        # deck-prefixed label belongs to a different artifact, so it is
        # not added as an alias (that would cross-link the two)
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        other = register(
            wiki, tmp_path, "Q20", "Deck B", deck_content([1], OTHER_IMAGE)
        )
        before = copy.deepcopy(wiki.entities[first["image1.png"]])

        reused = register(
            wiki, tmp_path, "Q11", "Deck A", deck_content([2], OTHER_IMAGE)
        )

        assert reused == other  # same bytes: one artifact (ADR-0010)
        entity = wiki.entities[other["image1.png"]]
        assert part_of(entity) == {
            ("Q20", "1"): ["Q20"],
            ("Q11", "2"): ["Q11"],
        }
        assert "aliases" not in entity or not entity["aliases"].get("en")
        assert wiki.entities[first["image1.png"]] == before
        writes = wiki.writes
        register(
            wiki, tmp_path, "Q11", "Deck A", deck_content([2], OTHER_IMAGE)
        )
        assert wiki.writes == writes

    def test_an_overwritten_file_is_not_trusted(self, tmp_path):
        # the item's file was overwritten outside this code (an older
        # upload that ignored warnings): the next deck finds no file of
        # the item holding its bytes, so it stores them again under its
        # own name, and the earlier statement stays as it is (#186)
        wiki = FakeWiki()
        first = register(wiki, tmp_path, "Q10", "Deck A", deck_content([1]))
        qid = first["image1.png"]
        other = hashlib.sha1(OTHER_IMAGE).hexdigest()
        wiki.media.versions["Deck_A_image1.png"].append(other)
        before = copy.deepcopy(wiki.entities[qid])

        again = register(wiki, tmp_path, "Q20", "Deck B", deck_content([1]))

        assert again == first
        after = wiki.entities[qid]
        assert_additive(before, after)
        files = {
            value(statement): cited(statement)
            for statement in after["claims"][P["media_file"]]
        }
        assert files == {
            "File:Deck A image1.png": ["Q10"],
            "File:Deck B image1.png": ["Q20"],
        }
        assert wiki.files["File:Deck B image1.png"] == (
            hashlib.sha1(IMAGE).hexdigest()
        )
        assert wiki.drift() == [(qid, "File:Deck A image1.png")]

    def test_no_media_touches_nothing(self, tmp_path):
        wiki = FakeWiki()
        content = PresentationContent(
            slides=[Slide(number=1, text="")], media=[]
        )
        assert register(wiki, tmp_path, "Q10", "Deck A", content) == {}
        assert wiki.writes == 0
