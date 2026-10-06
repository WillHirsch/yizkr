"""Offline tests for the flags registration raises on an item that may
be an older artifact (#371; ADR-0010, "Registration at write time").

Registration finds an artifact by the SHA-1 of its media alone, and a
title never finds an item. An item is flagged, never adopted, on every
registration, made or found, while either condition holds: another item
names a wiki file that holds its bytes (`shared-file`; every such file,
as MediaWiki lists them by their SHA-1), or its title is the label of an
item with no hash (`unhashed-title`). An item with no hash has no `media
SHA-1` and names a `media file`, or is an `instance of` an artifact
kind, as a GEDCOM media object's item is. A flag changes nothing
registration finds or writes, and `ingest run` raises it as an alert of
the write. On an instance that only registration and the GEDCOM import
have filled, as every scratch instance is, shared-file is never raised,
and unhashed-title only for a scan or derived medium registered under
exactly a GEDCOM media object's label.

The in-memory wiki, its fictional media and the helpers come from
tests/unit/test_registrar.py. The flow is verified live too
(tests/integration/test_live_registration_flags.py).
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from argparse import Namespace

import pytest

# the module, not its TestStepRegister, so pytest collects that class once
from tests.unit import test_registrar
from tests.unit.test_registrar import (
    IMAGE,
    ITEMS,
    LONG,
    OTHER_IMAGE,
    PORTRAIT,
    RESCAN,
    SHOP,
    TITLE,
    FakeWiki,
    P,
    StoringWiki,
    WriteRefused,
    assert_labels_unique,
    deck_file,
    label_of,
    recorded,
    register_deck,
    scan,
    sha1,
    strings,
)
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import registrar as registrar_module
from yizkr.pipeline.registrar import (
    REGISTRATION_CHECKS,
    SHARED_FILE,
    UNHASHED_TITLE,
    Registrar,
    RegistrationFlag,
    _ensure_referenced,
    _file_key,
    _Instance,
    _string_claim,
)

# fictional sources (docs/EXAMPLES.md): Rudolf's Soviet death certificate,
# Sybren van 't Nijks's West Frisian letter to his sister Sipkje, and the
# civil marriage certificate of Sebastian and Hildegard
CERTIFICATE = b"\x89PNG synthetic scan of Rudolf's 1941 death certificate"
CERTIFICATE_TITLE = "Свидетельство о смерти, Лёбиг Рудольф Феликсович"
LETTER = b"\x89PNG synthetic scan of the 1952 letter from Snylsjum"
LETTER_RESCAN = b"\x89PNG synthetic re-scan of the 1952 letter from Snylsjum"
LETTER_TITLE = "letter-to-sipkje-1952"
MARRIAGE = b"%PDF synthetic scan of the 1905 civil marriage certificate"
MARRIAGE_TITLE = "marriage-cert-1905"
# a GEDCOM import's item, the one every media object's item is stated in
GEDCOM_TITLE = "example-family.ged"
GEDCOM = b"0 HEAD synthetic GEDCOM export of the Loebig family tree"
# a media object's title in that GEDCOM, which no scan below takes
MEDIA_OBJECT_TITLE = "Löbig family, Flörburg, about 1895"


def unhashed(wiki, label, page, data, *, kind="photograph"):
    """An item as registration left one before content hashing: `label`,
    its kind and a `media file` naming `page`, which holds `data`, and no
    `media SHA-1`, each statement stated in the item itself. `kind` None
    leaves its kind out."""
    wiki.media.store(page.removeprefix("File:").replace(" ", "_"), data)
    statements = [("media_file", page, None)]
    if kind is not None:
        statements.insert(0, ("instance_of", ITEMS[kind], None))
    return wiki.item(label, *statements)


def gedcom_media_object(wiki, label: str) -> str:
    """An item as the GEDCOM import makes one for a media object: `label`,
    `instance of artifact`, no `media file` and no `media SHA-1`, stated in
    the GEDCOM's own item, which registration made with its hash."""
    gedcom = wiki.item(
        GEDCOM_TITLE,
        ("instance_of", ITEMS["genealogical_report"], None),
        ("media_sha1", sha1(GEDCOM), None),
    )
    return wiki.item(
        label, ("instance_of", ITEMS["artifact"], None), stated_in=gedcom
    )


def hash_item(wiki, qid: str, data: bytes) -> None:
    """Give the item `qid` a `media SHA-1`, stated in itself, as a curator
    would hash an older artifact."""
    item = wiki.wbi.item.get(qid)
    _ensure_referenced(
        item,
        _string_claim(P["media_sha1"], sha1(data), P["stated_in"], qid),
        P["stated_in"],
        qid,
    )
    item.write()


def flag_tuples(flags) -> list[tuple]:
    return [
        (flag.kind, flag.artifact, flag.sha1, flag.others) for flag in flags
    ]


# -- what is unhashed, and which file an item names ---------------------------


class TestTheInstanceAsRead:
    def test_an_item_naming_a_file_without_a_hash_is_unhashed(self):
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        instance = _Instance.read(wiki, *wiki.vocabulary())
        assert instance.unhashed == {old}
        assert instance.files == {"Portrait-1890s.png": {old}}

    def test_a_hashed_item_is_not_unhashed(self, tmp_path):
        wiki = FakeWiki()
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        instance = _Instance.read(wiki, *wiki.vocabulary())
        assert instance.unhashed == set()
        assert instance.files == {"Portrait-1890s.png": {result.artifact_qid}}

    @pytest.mark.parametrize(
        "statements",
        [
            # a hash and no file
            [("media_sha1", "0" * 40, None)],
            # an artifact kind and a hash, no file
            [
                ("instance_of", ITEMS["artifact"], None),
                ("media_sha1", "0" * 40, None),
            ],
            # an item the writer minted: a label alone
            [],
            # a page fragment, a run item: kinds no artifact has
            [("instance_of", ITEMS["artifact_fragment"], None)],
            [("instance_of", ITEMS["extraction_run"], None)],
            # a blank `media file` names no file
            [("media_file", "File:", None)],
            [("media_file", "  ", None)],
        ],
        ids=[
            "hash without a file",
            "artifact kind with a hash",
            "label alone",
            "artifact fragment",
            "run item",
            "blank file",
            "blank value",
        ],
    )
    def test_an_item_naming_no_file_of_no_artifact_kind_is_not_unhashed(
        self, statements
    ):
        wiki = FakeWiki()
        wiki.item(TITLE, *statements)
        instance = _Instance.read(wiki, *wiki.vocabulary())
        assert instance.unhashed == set()
        assert instance.files == {}

    @pytest.mark.parametrize(
        "kind",
        ["artifact", "photograph", "certificate", "genealogical_report"],
    )
    def test_an_artifact_kind_without_a_hash_is_unhashed(self, kind):
        """A GEDCOM media object's item names no file: an `instance of`
        an artifact kind with no `media SHA-1` is unhashed all the same."""
        wiki = FakeWiki()
        gedcom = wiki.item(GEDCOM_TITLE)
        qid = wiki.item(
            TITLE, ("instance_of", ITEMS[kind], None), stated_in=gedcom
        )
        instance = _Instance.read(wiki, *wiki.vocabulary())
        assert instance.unhashed == {qid}
        assert instance.files == {}

    @pytest.mark.parametrize(
        "page",
        [
            "File:Portrait-1890s.png",
            "Portrait-1890s.png",
            "file:portrait-1890s.png",
            "File:Portrait-1890s.png ",
        ],
    )
    def test_a_file_is_keyed_as_mediawiki_takes_its_title(self, page):
        assert _file_key(page) == "Portrait-1890s.png"

    def test_underscores_and_runs_of_spaces_are_one_space(self):
        assert _file_key("File:Letter_to__sipkje  1952.png") == (
            "Letter to sipkje 1952.png"
        )
        assert _file_key("File:") == ""

    def test_a_file_is_keyed_in_nfc(self):
        decomposed = "File:Lo\u0308big portrait.png"
        assert _file_key(decomposed) == "Löbig portrait.png"
        assert _file_key(decomposed) == _file_key("File:Löbig_portrait.png")

    @pytest.mark.parametrize(
        "mark", ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d",
                 "\u202e"],
    )  # fmt: skip
    def test_the_bidi_marks_mediawiki_strips_are_left_out(self, mark):
        assert _file_key(f"File:{mark}Löbig{mark} portrait.png") == (
            "Löbig portrait.png"
        )

    @pytest.mark.parametrize("mark", ["\u2066", "\u2067", "\u2068", "\u2069"])
    def test_an_isolate_is_kept_as_mediawiki_keeps_it(self, mark):
        """MediaWiki 1.45's title parser strips U+200E, U+200F and
        U+202A-U+202E alone (TitleParser::splitTitleString): a name with
        an isolate is another file."""
        assert _file_key(f"File:Löbig{mark} portrait.png") == (
            f"Löbig{mark} portrait.png"
        )

    def test_letters_after_the_first_keep_their_case(self):
        """MediaWiki titles differ by case after the first letter, so two
        files whose names differ only there are two files."""
        assert _file_key("File:Brief an Sipkje.png") != _file_key(
            "File:Brief an SIPKJE.png"
        )


# -- register_artifact --------------------------------------------------------


class TestArtifactFlags:
    def test_a_title_another_items_label_with_its_file_raises_both(
        self, tmp_path
    ):
        """The item left from before content hashing has this title and a
        file that holds these bytes: the new item shares its file and
        takes its title, so both flags are raised, and nothing is adopted:
        the new item is made with its own hash and a tagged label, and the
        older item is left as it is."""
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        before = copy.deepcopy(wiki.entities[old])
        registrar = Registrar(wiki)

        result = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )

        new = result.artifact_qid
        assert result.created and new != old
        assert result.file_page == "File:Portrait-1890s.png"
        assert flag_tuples(result.flags) == [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
        ]
        assert registrar.flags == result.flags
        assert wiki.entities[old] == before
        assert strings(wiki, new, "media_sha1") == [sha1(PORTRAIT)]
        assert label_of(wiki, new) == f"{TITLE} [{sha1(PORTRAIT)[:8]}]"
        assert_labels_unique(wiki)
        assert wiki.drift() == []

    def test_a_file_another_item_names_raises_shared_file(self, tmp_path):
        """The bytes are uploaded under another name: the wiki holds them
        already, as the file an item with another label names, so the
        upload is that file, and the new item shares it."""
        wiki = FakeWiki()
        old = unhashed(
            wiki,
            CERTIFICATE_TITLE,
            "File:Death-certificate-1941.png",
            CERTIFICATE,
            kind="certificate",
        )
        before = copy.deepcopy(wiki.entities[old])

        result = Registrar(wiki).register_artifact(
            scan(tmp_path, CERTIFICATE, "Свидетельство-1941.png"),
            "certificate",
            "death-certificate-1941",
        )

        assert result.created and result.artifact_qid != old
        assert result.file_page == "File:Death-certificate-1941.png"
        assert flag_tuples(result.flags) == [
            (SHARED_FILE, result.artifact_qid, sha1(CERTIFICATE), (old,))
        ]
        assert wiki.entities[old] == before
        assert label_of(wiki, result.artifact_qid) == "death-certificate-1941"

    def test_an_unhashed_items_label_raises_unhashed_title(self, tmp_path):
        """The item with no hash has this title and a file of other bytes:
        the upload takes a content-tagged name of its own, so only the
        title is flagged."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, LETTER_TITLE, "File:Letter-to-sipkje-1952.png", LETTER_RESCAN
        )
        before = copy.deepcopy(wiki.entities[old])

        result = Registrar(wiki).register_artifact(
            scan(tmp_path, LETTER, "letter-to-sipkje-1952.png"),
            "artifact",
            LETTER_TITLE,
        )

        assert result.created and result.artifact_qid != old
        assert result.file_page != "File:Letter-to-sipkje-1952.png"
        assert flag_tuples(result.flags) == [
            (UNHASHED_TITLE, result.artifact_qid, sha1(LETTER), (old,))
        ]
        assert wiki.entities[old] == before
        assert label_of(wiki, result.artifact_qid) == (
            f"{LETTER_TITLE} [{sha1(LETTER)[:8]}]"
        )

    def test_a_file_named_with_underscores_is_the_same_file(self, tmp_path):
        """An older `media file` value may spell the page with underscores
        or a lower-case first letter: it names the same wiki file."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, "a Löbig portrait", "File:Portrait-1890s.png", PORTRAIT
        )
        statement = wiki.entities[old]["claims"][
            FakeWiki.PROPERTIES["media_file"]
        ]
        statement[0]["mainsnak"]["datavalue"]["value"] = "portrait-1890s.png"

        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT, "portrait_1890s.png"), "photograph", TITLE
        )

        assert [flag.kind for flag in result.flags] == [SHARED_FILE]

    def test_every_item_naming_the_file_is_named_in_order(self, tmp_path):
        wiki = FakeWiki()
        first = unhashed(
            wiki, "a Löbig portrait", "File:Portrait-1890s.png", PORTRAIT
        )
        second = wiki.item(
            "the same portrait, catalogued twice",
            ("media_file", "File:Portrait-1890s.png", None),
        )
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert flag_tuples(result.flags) == [
            (SHARED_FILE, result.artifact_qid, sha1(PORTRAIT), (first, second))
        ]

    def test_an_unhashed_item_of_no_artifact_kind_still_counts(self, tmp_path):
        """What makes an item an artifact registered before hashing is the
        `media file` it names, whatever kind it states, if any."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, TITLE, "File:Portrait-1890s.png", RESCAN, kind=None
        )
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert flag_tuples(result.flags) == [
            (UNHASHED_TITLE, result.artifact_qid, sha1(PORTRAIT), (old,))
        ]

    def test_a_content_tagged_unhashed_label_is_not_the_title(self, tmp_path):
        """A title is compared with an unhashed item's label as it is: a
        label that ends in a content tag is another label, so only a
        shared file would flag it."""
        wiki = FakeWiki()
        unhashed(
            wiki, f"{TITLE} [0123abcd]", "File:Portrait-1890s.png", RESCAN
        )
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert result.created and result.flags == []
        assert label_of(wiki, result.artifact_qid) == TITLE

    def test_a_title_is_compared_in_its_stored_form(self, tmp_path):
        wiki = StoringWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", RESCAN)
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", f"  {TITLE} "
        )
        assert [(flag.kind, flag.others) for flag in result.flags] == [
            (UNHASHED_TITLE, (old,))
        ]

    def test_a_gedcom_media_objects_title_raises_unhashed_title(
        self, tmp_path
    ):
        """The GEDCOM import's item for a media object has the title, an
        artifact kind and no hash: a scan registered under exactly that
        title is flagged against it, never adopted. The new item is made
        with its own hash and a tagged label, and the media object's item
        is left as it is."""
        wiki = FakeWiki()
        media_object = gedcom_media_object(wiki, TITLE)
        before = copy.deepcopy(wiki.entities[media_object])
        registrar = Registrar(wiki)
        result = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        new = result.artifact_qid
        assert result.created and new != media_object
        assert flag_tuples(result.flags) == [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (media_object,))
        ]
        assert registrar.flags == result.flags
        assert label_of(wiki, new) == f"{TITLE} [{sha1(PORTRAIT)[:8]}]"
        assert wiki.entities[media_object] == before

    def test_a_writer_minted_label_raises_nothing(self, tmp_path):
        """An item the writer minted has the title, no kind of artifact
        and no file: the new item's label is tagged, as for any taken
        label, and nothing is flagged."""
        wiki = FakeWiki()
        gedcom = wiki.item(GEDCOM_TITLE)
        wiki.item(TITLE, stated_in=gedcom)
        registrar = Registrar(wiki)
        result = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert result.created
        assert label_of(wiki, result.artifact_qid) == (
            f"{TITLE} [{sha1(PORTRAIT)[:8]}]"
        )
        assert result.flags == [] and registrar.flags == []

    def test_a_re_run_flags_the_found_item_again(self, tmp_path):
        """Registered again, the bytes find the item they made by their
        SHA-1: no item is made, and while the conditions hold it is
        flagged again, each run as the first was. One registrar gathers a
        kind of flag on an item once; each run's registrar gathers it
        anew. The older item is still left as it is."""
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        before = copy.deepcopy(wiki.entities[old])
        path = scan(tmp_path, PORTRAIT)
        registrar = Registrar(wiki)
        first = registrar.register_artifact(path, "photograph", TITLE)
        new = first.artifact_qid
        expected = [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
        ]
        assert flag_tuples(first.flags) == expected
        entities = copy.deepcopy(wiki.entities)

        again = registrar.register_artifact(path, "photograph", TITLE)
        fresh = Registrar(wiki)
        anew = fresh.register_artifact(path, "photograph", TITLE)

        for result in (again, anew):
            assert not result.created
            assert result.artifact_qid == new
            assert flag_tuples(result.flags) == expected
        assert flag_tuples(registrar.flags) == expected
        assert flag_tuples(fresh.flags) == expected
        assert wiki.entities == entities
        assert wiki.entities[old] == before

    def test_a_registrar_gathers_each_kind_of_flag_on_an_item(self, tmp_path):
        """One registrar registers the same bytes twice: under its first
        title the item is flagged shared-file alone, and under a GEDCOM
        media object's label, found by its SHA-1, unhashed-title too. The
        registrar gathers each kind once, the second kind included."""
        wiki = FakeWiki()
        other = wiki.item(
            "a Löbig portrait, catalogued",
            ("instance_of", ITEMS["photograph"], None),
            ("media_file", "File:Portrait-1890s.png", None),
            ("media_sha1", "0" * 40, None),
        )
        wiki.media.store("Portrait-1890s.png", PORTRAIT)
        media_object = gedcom_media_object(wiki, MEDIA_OBJECT_TITLE)
        registrar = Registrar(wiki)
        first = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        new = first.artifact_qid
        assert flag_tuples(first.flags) == [
            (SHARED_FILE, new, sha1(PORTRAIT), (other,))
        ]

        second = registrar.register_artifact(
            scan(tmp_path, PORTRAIT, "copy.png", "copy"),
            "photograph",
            MEDIA_OBJECT_TITLE,
        )

        assert second.artifact_qid == new
        assert flag_tuples(registrar.flags) == [
            (SHARED_FILE, new, sha1(PORTRAIT), (other,)),
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (media_object,)),
        ]

    @pytest.mark.parametrize(
        "cleared", ["relabelled", "hashed", "file unnamed", "merged"]
    )
    def test_no_flag_once_its_condition_clears(self, tmp_path, cleared):
        """A curator acts on the older item: relabelled, it no longer has
        the title; hashed, it is no longer unhashed; once it names the
        file no more, or is merged away, nothing shares the file. The flag
        that condition raised is raised no more."""
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        path = scan(tmp_path, PORTRAIT)
        first = Registrar(wiki).register_artifact(path, "photograph", TITLE)
        assert len(first.flags) == 2
        if cleared == "relabelled":
            wiki.entities[old]["labels"]["en"]["value"] = "the Löbig portrait"
        elif cleared == "hashed":
            hash_item(wiki, old, b"\x89PNG synthetic: another scan")
        elif cleared == "file unnamed":
            del wiki.entities[old]["claims"][P["media_file"]]
        else:
            del wiki.entities[old]

        registrar = Registrar(wiki)
        again = registrar.register_artifact(path, "photograph", TITLE)

        assert again.artifact_qid == first.artifact_qid
        left = {
            "relabelled": [SHARED_FILE],
            "hashed": [SHARED_FILE],
            "file unnamed": [UNHASHED_TITLE],
            "merged": [],
        }[cleared]
        assert [flag.kind for flag in again.flags] == left
        assert registrar.flags == again.flags

    def test_the_alias_path_flags_once_a_registration(self, tmp_path):
        """The same bytes registered under another title and file name
        find the item, which takes the title as an alias: while its
        conditions hold it is flagged, once for each kind in each
        registration, under its own label as well as the title given."""
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        registrar = Registrar(wiki)
        first = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        other = "the Löbig family, 1890s"
        copy_path = scan(tmp_path, PORTRAIT, "loebig-family.png", "copy")

        again = registrar.register_artifact(copy_path, "photograph", other)
        fresh = Registrar(wiki)
        anew = fresh.register_artifact(copy_path, "photograph", other)

        new = first.artifact_qid
        expected = [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
        ]
        for result in (again, anew):
            assert result.artifact_qid == new and not result.created
            assert flag_tuples(result.flags) == expected
        assert flag_tuples(registrar.flags) == expected
        assert flag_tuples(fresh.flags) == expected
        aliases = wiki.entities[new]["aliases"]["en"]
        assert [alias["value"] for alias in aliases] == [other]

    def test_the_alias_path_flags_nothing_without_a_condition(self, tmp_path):
        wiki = FakeWiki()
        gedcom_media_object(wiki, MEDIA_OBJECT_TITLE)
        unhashed(wiki, "the Snylsjum letter", "File:Snylsjum.png", LETTER)
        first = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        registrar = Registrar(wiki)
        again = registrar.register_artifact(
            scan(tmp_path, PORTRAIT, "loebig-family.png", "copy"),
            "photograph",
            "the Löbig family, 1890s",
        )
        assert first.flags == []
        assert again.artifact_qid == first.artifact_qid
        assert again.flags == [] and registrar.flags == []

    def test_a_hashed_item_naming_the_file_raises_shared_file(self, tmp_path):
        """An item that names the file and carries another hash (its file
        overwritten before uploads stopped overwriting) is not unhashed,
        but it names a file that holds these bytes: shared-file."""
        wiki = FakeWiki()
        other = wiki.item(
            "a Löbig portrait, catalogued",
            ("instance_of", ITEMS["photograph"], None),
            ("media_file", "File:Portrait-1890s.png", None),
            ("media_sha1", "0" * 40, None),
        )
        wiki.media.store("Portrait-1890s.png", PORTRAIT)
        assert _Instance.read(wiki, *wiki.vocabulary()).unhashed == set()

        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT, "loebig.png"), "photograph", TITLE
        )

        assert result.created and result.artifact_qid != other
        assert flag_tuples(result.flags) == [
            (SHARED_FILE, result.artifact_qid, sha1(PORTRAIT), (other,))
        ]

    def test_every_file_holding_the_bytes_is_shared(self, tmp_path):
        """The bytes are in two wiki files, each named by an item with no
        hash, and the upload is one of them: both items are named, as
        MediaWiki lists every file of these bytes. An item naming a file
        of other bytes is not."""
        wiki = FakeWiki()
        first = unhashed(
            wiki, "a Löbig portrait", "File:Portrait-1890s-a.png", PORTRAIT
        )
        second = unhashed(
            wiki, "the Löbig portrait, again", "File:Portrait-1890s-b.png",
            PORTRAIT,
        )  # fmt: skip
        unhashed(wiki, "the re-scan", "File:Portrait-rescan.png", RESCAN)

        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT, "loebig.png"), "photograph", TITLE
        )

        assert result.file_page == "File:Portrait-1890s-a.png"
        assert flag_tuples(result.flags) == [
            (SHARED_FILE, result.artifact_qid, sha1(PORTRAIT), (first, second))
        ]

    def test_a_file_holding_the_bytes_counts_without_the_upload(
        self, tmp_path
    ):
        """The found item's own file holds its bytes, and so does another
        file, which an unhashed item names: MediaWiki's listing finds it,
        though no upload answered with it."""
        wiki = FakeWiki()
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        wiki.media.store("Portrait-catalogued.png", PORTRAIT)
        old = unhashed(
            wiki, "a Löbig portrait", "File:Portrait-catalogued.png", PORTRAIT
        )
        again = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert again.artifact_qid == result.artifact_qid
        assert flag_tuples(again.flags) == [
            (SHARED_FILE, result.artifact_qid, sha1(PORTRAIT), (old,))
        ]

    @pytest.mark.parametrize("first", [9, 99])
    def test_others_are_in_the_order_of_their_numbers(self, tmp_path, first):
        """Q9 before Q10, and Q99 before Q100: by number, not as text."""
        wiki = FakeWiki()
        wiki._next_qid = first
        a = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        b = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        assert (a, b) == (f"Q{first}", f"Q{first + 1}")
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert [flag.others for flag in result.flags] == [(a, b), (a, b)]

    def test_a_250_character_title_an_unhashed_item_holds(self, tmp_path):
        """The title, of the 250 characters Wikibase takes, is an unhashed
        item's label: the new item's tagged label is cut to fit, and the
        item is made and flagged, never refused."""
        wiki = StoringWiki()
        title = LONG[:250]
        old = unhashed(wiki, title, "File:Portrait-1890s.png", RESCAN)
        result = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", title
        )
        tag = f" [{sha1(PORTRAIT)[:8]}]"
        assert result.created
        assert label_of(wiki, result.artifact_qid) == title[:238] + "…" + tag
        assert flag_tuples(result.flags) == [
            (UNHASHED_TITLE, result.artifact_qid, sha1(PORTRAIT), (old,))
        ]

    def test_flags_gather_over_a_registrars_artifacts(self, tmp_path):
        wiki = FakeWiki()
        portrait = unhashed(wiki, TITLE, "File:Portrait-1890s.png", RESCAN)
        letter = unhashed(
            wiki, "a letter from Snylsjum", "File:Snylsjum-1952.png", LETTER
        )
        registrar = Registrar(wiki)
        a = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        b = registrar.register_artifact(
            scan(tmp_path, MARRIAGE, "marriage-cert-1905.pdf"),
            "certificate",
            MARRIAGE_TITLE,
        )
        c = registrar.register_artifact(
            scan(tmp_path, LETTER, "letter.png"), "photograph", LETTER_TITLE
        )
        assert b.flags == []
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, a.artifact_qid, sha1(PORTRAIT), (portrait,)),
            (SHARED_FILE, c.artifact_qid, sha1(LETTER), (letter,)),
        ]

    def test_the_module_wrapper_returns_the_flags(self, tmp_path):
        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", RESCAN)
        result = registrar_module.register_artifact(
            wiki, scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert [(flag.kind, flag.others) for flag in result.flags] == [
            (UNHASHED_TITLE, (old,))
        ]

    def test_cmd_register_prints_the_flags(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr import cli

        wiki = FakeWiki()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        monkeypatch.setattr(cli, "_refuse_if_excluded", lambda *a, **k: None)
        args = Namespace(
            file=scan(tmp_path, PORTRAIT),
            type="photograph",
            title=TITLE,
            description=None,
            pages=0,
            env_file=None,
        )

        assert cli.cmd_register(args) == 0

        printed = json.loads(capsys.readouterr().out)
        new = printed["artifact_qid"]
        assert printed["created"] is True
        assert printed["flags"] == [
            {
                "kind": kind,
                "artifact": new,
                "sha1": sha1(PORTRAIT),
                "others": [old],
            }
            for kind in (UNHASHED_TITLE, SHARED_FILE)
        ]


# -- register_derived_media ---------------------------------------------------


class TestDerivedMediaFlags:
    def test_a_medium_is_flagged_as_an_artifact_is(self, tmp_path):
        """A deck's image whose label, before any tag, is an unhashed
        item's, and whose bytes that item's file holds: both flags, and a
        new medium with its own hash."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, "Deck A — image1.png", "File:Shop-front.png", IMAGE
        )
        before = copy.deepcopy(wiki.entities[old])
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        container = registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )

        derived = registrar.register_derived_media(record, directory)

        medium = derived[sha1(IMAGE)]
        assert medium != old
        assert container.flags == []
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, medium, sha1(IMAGE), (old,)),
            (SHARED_FILE, medium, sha1(IMAGE), (old,)),
        ]
        assert wiki.entities[old] == before
        assert strings(wiki, medium, "media_sha1") == [sha1(IMAGE)]
        assert strings(wiki, medium, "media_file") == ["File:Shop-front.png"]
        assert label_of(wiki, medium) == (
            f"Deck A — image1.png [{sha1(IMAGE)[:8]}]"
        )
        assert_labels_unique(wiki)

    def test_a_second_container_finds_the_flagged_medium(self, tmp_path):
        """A second deck embedding the image finds the medium by its SHA-1
        and, while the older item still names its file, flags it again."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, "the shop front at Brunnenring 35", "File:Shop.png", IMAGE
        )
        _, derived = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "b")
        registrar.register_artifact(deck, "presentation", "Deck B")
        record, directory = recorded(
            tmp_path, deck, "Deck B", [("image1.png", IMAGE, [2])]
        )
        assert registrar.register_derived_media(record, directory) == derived
        assert flag_tuples(registrar.flags) == [
            (SHARED_FILE, derived[sha1(IMAGE)], sha1(IMAGE), (old,))
        ]
        assert old not in derived.values()

    def test_a_found_medium_is_flagged_under_its_own_label(self, tmp_path):
        """Deck A's image took a tagged label, its title an unhashed item's;
        found through deck B, under another first label, it is flagged
        again under its own label before the tag."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, "Deck A — image1.png", "File:Shop-retouched.png", OTHER_IMAGE
        )
        _, derived = register_deck(
            wiki, tmp_path, "a", "Deck A", [("image1.png", IMAGE, [1])]
        )
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "b")
        registrar.register_artifact(deck, "presentation", "Deck B")
        record, directory = recorded(
            tmp_path, deck, "Deck B", [("image1.png", IMAGE, [2])]
        )
        assert registrar.register_derived_media(record, directory) == derived
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, derived[sha1(IMAGE)], sha1(IMAGE), (old,))
        ]

    def test_a_medium_is_flagged_under_its_first_name_alone(self, tmp_path):
        """A medium of two names is labelled by its first: an unhashed
        item labelled with the deck's title and its second name is not its
        title, and raises nothing."""
        wiki = FakeWiki()
        unhashed(
            wiki, "Deck A — shop.png", "File:Shop-retouched.png", OTHER_IMAGE
        )
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path,
            deck,
            "Deck A",
            [("image1.png", IMAGE, [1]), ("shop.png", IMAGE, [2])],
        )
        derived = registrar.register_derived_media(record, directory)
        assert label_of(wiki, derived[sha1(IMAGE)]) == "Deck A — image1.png"
        assert registrar.flags == []

    def test_a_shared_file_medium_is_flagged_once(self, tmp_path):
        wiki = FakeWiki()
        old = unhashed(
            wiki, "the shop front at Brunnenring 35", "File:Shop.png", IMAGE
        )
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        derived = registrar.register_derived_media(record, directory)
        assert flag_tuples(registrar.flags) == [
            (SHARED_FILE, derived[sha1(IMAGE)], sha1(IMAGE), (old,))
        ]
        # the same record again: found, and one registrar gathers the
        # flag once; a fresh one, as the next run's, gathers it again
        registrar.register_derived_media(record, directory)
        assert len(registrar.flags) == 1
        fresh = Registrar(wiki)
        fresh.register_derived_media(record, directory)
        assert fresh.flags == registrar.flags

    def test_a_mediums_title_alone(self, tmp_path):
        wiki = FakeWiki()
        old = unhashed(
            wiki, "Deck A — image1.png", "File:Shop-retouched.png", OTHER_IMAGE
        )
        deck = deck_file(tmp_path, "a")
        registrar = Registrar(wiki)
        registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        derived = registrar.register_derived_media(record, directory)
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, derived[sha1(IMAGE)], sha1(IMAGE), (old,))
        ]

    def test_the_module_wrapper_registers_as_before(self, tmp_path):
        wiki = FakeWiki()
        deck = deck_file(tmp_path, "a")
        registrar_module.register_artifact(
            wiki, deck, "presentation", "Deck A"
        )
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        derived = registrar_module.register_derived_media(
            wiki, record, directory
        )
        assert list(derived) == [sha1(IMAGE)]

    def test_the_module_wrapper_adds_the_flags_it_is_given(self, tmp_path):
        wiki = FakeWiki()
        old = unhashed(
            wiki, "Deck A — image1.png", "File:Shop-retouched.png", OTHER_IMAGE
        )
        deck = deck_file(tmp_path, "a")
        registrar_module.register_artifact(
            wiki, deck, "presentation", "Deck A"
        )
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )
        flags: list[RegistrationFlag] = []
        derived = registrar_module.register_derived_media(
            wiki, record, directory, flags=flags
        )
        assert flag_tuples(flags) == [
            (UNHASHED_TITLE, derived[sha1(IMAGE)], sha1(IMAGE), (old,))
        ]


# -- a scratch instance: registration and the GEDCOM import -----------------


class TestScratchInstance:
    def test_registrations_alone_never_raise_a_flag(self, tmp_path):
        """Every item registration makes names its file and its hash
        together, so on an instance only registration, the writer and the
        GEDCOM import have written to, the only items with no hash are
        the GEDCOM's media objects, and with no scan under exactly one's
        title no flag is raised, whatever is registered, each twice:
        homonyms of other bytes, the same bytes under other names, a
        changed source, decks that share an image, an image also ingested
        alone, and a title a minted item already has."""
        wiki = FakeWiki()
        media_object = gedcom_media_object(wiki, MEDIA_OBJECT_TITLE)
        wiki.item(LETTER_TITLE)  # a label the writer gave an item it minted
        results = []
        registrars = []

        def registered(data, title, name, kind="photograph"):
            registrar = Registrar(wiki)
            registrars.append(registrar)
            path = scan(tmp_path, data, name, folder=f"r{len(registrars)}")
            results.append(registrar.register_artifact(path, kind, title))
            return results[-1]

        registered(PORTRAIT, TITLE, "portrait-1890s.png")
        # the same title, other bytes, the same file name: a homonym
        registered(RESCAN, TITLE, "portrait-1890s.png")
        # the same bytes under another name and title: found, an alias
        registered(PORTRAIT, "Löbig family, studio portrait", "copy.png")
        # the letter, then its changed source under its own name
        registered(LETTER, LETTER_TITLE, "letter-to-sipkje-1952.png")
        registered(LETTER_RESCAN, LETTER_TITLE, "letter-to-sipkje-1952.png")
        registered(
            CERTIFICATE, CERTIFICATE_TITLE, "death-certificate-1941.png",
            "certificate",
        )  # fmt: skip
        registered(MARRIAGE, MARRIAGE_TITLE, "marriage-cert-1905.pdf")
        # two decks embedding one image under one name, and the image
        # ingested alone under that name, then under the deck's label
        for name in ("a", "b"):
            registrar = Registrar(wiki)
            registrars.append(registrar)
            deck = deck_file(tmp_path, name)
            results.append(
                registrar.register_artifact(deck, "presentation", "Deck")
            )
            record, directory = recorded(
                tmp_path,
                deck,
                "Deck",
                [
                    ("image1.png", IMAGE, [1]),
                    (f"{name}.png", OTHER_IMAGE, [2]),
                ],
            )
            registrar.register_derived_media(record, directory)
        registered(IMAGE, "image1", "image1.png")
        registered(IMAGE, "Deck — image1.png", "shop.png")
        registered(OTHER_IMAGE, "Deck — image1.png", "retouched.png")
        # a write that runs again registers each again
        for result in list(results):
            if result.file_page is not None:
                registered(
                    wiki_bytes(wiki, result.file_page), "again", "again.png"
                )

        assert all(result.flags == [] for result in results)
        assert all(registrar.flags == [] for registrar in registrars)
        instance = _Instance.read(wiki, *wiki.vocabulary())
        assert instance.unhashed == {media_object}
        # every file is named by the items of its bytes alone
        for qids in instance.files.values():
            assert (
                len({tuple(strings(wiki, q, "media_sha1")) for q in qids}) == 1
            )
        assert wiki.drift() == []
        assert_labels_unique(wiki)

    def test_a_scan_under_a_media_objects_title_raises_that_alone(
        self, tmp_path
    ):
        """A scan registered under exactly a GEDCOM media object's title is
        the one flag a scratch instance can raise: unhashed-title, against
        the media object's item. Shared-file is never raised there."""
        wiki = FakeWiki()
        media_object = gedcom_media_object(wiki, MEDIA_OBJECT_TITLE)
        registrar = Registrar(wiki)
        Registrar(wiki).register_artifact(
            scan(tmp_path, RESCAN, folder="first"), "photograph", TITLE
        )
        result = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", MEDIA_OBJECT_TITLE
        )
        again = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT, "copy.png", "copy"), "photograph", TITLE
        )
        expected = [
            (UNHASHED_TITLE, result.artifact_qid, sha1(PORTRAIT),
             (media_object,))
        ]  # fmt: skip
        assert flag_tuples(registrar.flags) == expected
        # found under another title, its label still the media object's
        assert flag_tuples(again.flags) == expected

    def test_a_medium_under_a_media_objects_label_raises_that_alone(
        self, tmp_path
    ):
        """A derived medium whose first label is exactly a GEDCOM media
        object's label raises unhashed-title too, against that item, as a
        scan under it does: an artifact or a derived medium can raise it on
        a scratch instance."""
        wiki = FakeWiki()
        media_object = gedcom_media_object(wiki, "Deck A — image1.png")
        registrar = Registrar(wiki)
        deck = deck_file(tmp_path, "a")
        container = registrar.register_artifact(deck, "presentation", "Deck A")
        record, directory = recorded(
            tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
        )

        derived = registrar.register_derived_media(record, directory)

        assert container.flags == []
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, derived[sha1(IMAGE)], sha1(IMAGE),
             (media_object,))
        ]  # fmt: skip


def wiki_bytes(wiki, page: str) -> bytes:
    """The fictional bytes the in-memory wiki holds under `page`."""
    current = wiki.files[page]
    for data in (
        PORTRAIT,
        RESCAN,
        LETTER,
        LETTER_RESCAN,
        CERTIFICATE,
        MARRIAGE,
        IMAGE,
        OTHER_IMAGE,
        *(f"PK synthetic deck {n}".encode() for n in "ab"),
    ):
        if sha1(data) == current:
            return data
    raise AssertionError(page)  # fmt: skip


# -- the run's alerts ---------------------------------------------------------

_WORDLESS = re.compile(r"Q[0-9]+|[0-9a-f]{12}|[a-z][a-z0-9-]*|[0-9TZ:.+-]+")


class TestFlagsReachTheRunsAlerts:
    orchestrator = staticmethod(test_registrar.TestStepRegister.orchestrator)

    def test_a_flag_is_an_alert_of_the_write(self, tmp_path):
        wiki = FakeWiki()
        old = unhashed(
            wiki, "portrait-1890s", "File:Portrait-1890s.png", PORTRAIT
        )
        orchestrator, lines = self.orchestrator(
            tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
        )
        (doc,) = orchestrator.documents()

        qid = orchestrator._step_register(doc)

        alerts = orchestrator.rundir.alerts()
        assert [alert["check"] for alert in alerts] == [
            UNHASHED_TITLE,
            SHARED_FILE,
        ]
        for alert in alerts:
            assert alert["check"] in REGISTRATION_CHECKS
            assert alert["stage"] == "write"
            assert alert["subject"] == doc.sha1[:12]
            assert alert["value"] is True
            assert alert["status"] == "new"
            assert alert["kind"] == doc.kind
            assert alert["artifact"] == qid != old
            assert alert["others"] == [old]
            assert alert["medium"] == doc.sha1[:12]
        events = [
            e for e in orchestrator.rundir.events() if e["event"] == "alert"
        ]
        assert [event["check"] for event in events] == [
            UNHASHED_TITLE,
            SHARED_FILE,
        ]
        assert any(
            line.strip() == f"flagged {SHARED_FILE}: item {qid}, against {old}"
            for line in lines
        )

    def test_no_alert_holds_a_documents_words(self, tmp_path):
        """No title, no file name: ids, hashes, names from the pipeline's
        vocabulary, and `true` (docs/ANOMALIES.md)."""
        wiki = FakeWiki()
        unhashed(
            wiki,
            "letter-to-sipkje-1952",
            "File:Letter-to-sipkje-1952.png",
            LETTER,
        )
        orchestrator, lines = self.orchestrator(
            tmp_path, wiki, [("letter-to-sipkje-1952.png", LETTER)]
        )
        (doc,) = orchestrator.documents()
        orchestrator._step_register(doc)
        alerts = orchestrator.rundir.alerts()
        assert len(alerts) == 2

        def leaves(value):
            if isinstance(value, dict):
                for inner in value.values():
                    yield from leaves(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from leaves(inner)
            else:
                yield value

        for alert in alerts:
            for leaf in leaves(alert):
                if isinstance(leaf, bool):
                    continue
                assert isinstance(leaf, str) and _WORDLESS.fullmatch(leaf), (
                    leaf
                )
            assert "sipkje" not in json.dumps(alert).lower()
        assert not any("sipkje" in line.lower() for line in lines)

    def test_a_re_run_raises_the_alerts_again_until_cleared(self, tmp_path):
        """Each write raises the flags while their conditions hold; once a
        curator relabels and hashes the older item, the next raises none."""
        wiki = FakeWiki()
        old = unhashed(
            wiki, "portrait-1890s", "File:Portrait-1890s.png", PORTRAIT
        )
        orchestrator, _ = self.orchestrator(
            tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
        )
        (doc,) = orchestrator.documents()
        qid = orchestrator._step_register(doc)
        assert orchestrator._step_register(doc) == qid
        alerts = orchestrator.rundir.alerts()
        assert [(a["check"], a["artifact"], a["others"]) for a in alerts] == [
            (UNHASHED_TITLE, qid, [old]),
            (SHARED_FILE, qid, [old]),
        ] * 2
        wiki.entities[old]["labels"]["en"]["value"] = "the Löbig portrait"
        del wiki.entities[old]["claims"][P["media_file"]]
        assert orchestrator._step_register(doc) == qid
        assert len(orchestrator.rundir.alerts()) == 4

    def test_registration_alone_raises_no_alert(self, tmp_path):
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(
            tmp_path,
            wiki,
            [
                ("portrait-1890s.png", PORTRAIT),
                ("portrait-1890s-2.png", RESCAN),
            ],
        )
        for doc in orchestrator.documents():
            orchestrator._step_register(doc)
        assert orchestrator.rundir.alerts() == []

    def test_a_flag_of_a_registration_cut_short_is_raised(self, tmp_path):
        """A deck's registration fails at its media, after the deck's own
        item was made and flagged: the flag is still raised, as the next
        write would raise it again."""
        wiki = FakeWiki()
        orchestrator, _ = self.orchestrator(tmp_path, wiki)
        old = unhashed(wiki, "Deck", "File:A.pptx", b"PK synthetic deck a")
        deck = deck_file(tmp_path, "a")
        doc = SimpleDoc(deck, "presentation")
        record, _ = recorded(
            tmp_path, deck, "Deck", [("image1.png", IMAGE, [1])]
        )

        def register(registrar, document):
            registrar.register_artifact(deck, "presentation", "Deck")
            wiki.refuse = lambda data: True
            registrar.register_derived_media(record, tmp_path / "state" / "a")

        orchestrator._register_document = register
        with pytest.raises(WriteRefused):
            orchestrator._step_register(doc)
        alerts = orchestrator.rundir.alerts()
        assert [alert["check"] for alert in alerts] == [
            UNHASHED_TITLE,
            SHARED_FILE,
        ]
        assert all(alert["others"] == [old] for alert in alerts)


# -- a file listing that fails ------------------------------------------------


class ListingFails(FakeWiki):
    """The in-memory wiki, its file listing (list=allimages) failing, as a
    5xx or an API error does through `WikiClient.api_get`, while `fails`."""

    fails = True

    def api_get(self, params):
        if self.fails and params.get("list") == "allimages":
            raise RuntimeError("list=allimages: 503 Service Unavailable")
        return super().api_get(params)


class TestAFailedListing:
    """A file listing that fails fails the document's registration for the
    run, as any other wiki call does, and the next write raises the flag
    (docs/ANOMALIES.md). The flags by its names, which need no listing,
    are gathered before it is asked, and the item and its fragments are
    complete before it is."""

    def test_the_registration_fails_and_the_next_one_flags(self, tmp_path):
        wiki = ListingFails()
        old = unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
        registrar = Registrar(wiki)
        path = scan(tmp_path, PORTRAIT)

        with pytest.raises(RuntimeError, match="allimages"):
            registrar.register_artifact(path, "photograph", TITLE, pages=2)

        (new,) = (
            qid
            for qid in wiki.entities
            if strings(wiki, qid, "media_sha1") == [sha1(PORTRAIT)]
        )
        # the title flag needs no listing: it is gathered before it
        assert flag_tuples(registrar.flags) == [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,))
        ]
        entities = copy.deepcopy(wiki.entities)

        wiki.fails = False
        again = Registrar(wiki).register_artifact(
            path, "photograph", TITLE, pages=2
        )

        assert again.artifact_qid == new and not again.created
        assert flag_tuples(again.flags) == [
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
        ]
        # nothing was left half-registered: the item and both fragments
        # were made before the listing failed, and are found now
        assert set(again.fragment_qids.values()) <= set(entities)
        assert wiki.entities == entities

    def test_a_failed_listing_still_raises_the_title_alert(self, tmp_path):
        wiki = ListingFails()
        old = unhashed(
            wiki, "portrait-1890s", "File:Portrait-1890s.png", PORTRAIT
        )
        orchestrator, _ = test_registrar.TestStepRegister.orchestrator(
            tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
        )
        (doc,) = orchestrator.documents()

        with pytest.raises(RuntimeError, match="allimages"):
            orchestrator._step_register(doc)

        alerts = orchestrator.rundir.alerts()
        assert [(a["check"], a["others"]) for a in alerts] == [
            (UNHASHED_TITLE, [old])
        ]
        wiki.fails = False
        qid = orchestrator._step_register(doc)
        assert [
            (a["check"], a["artifact"])
            for a in orchestrator.rundir.alerts()[1:]
        ] == [(UNHASHED_TITLE, qid), (SHARED_FILE, qid)]


class SimpleDoc:
    """The fields of a document `_step_register` reads beyond those its
    registration does."""

    def __init__(self, path, kind):
        self.source = path
        self.kind = kind
        self.sha1 = sha1(path.read_bytes())


class PagedListing:
    """A wiki client whose file listing comes a page at a time."""

    def __init__(self, pages):
        self.pages = pages
        self.asked: list[dict] = []

    def api_get(self, params):
        self.asked.append(dict(params))
        return self.pages[len(self.asked) - 1]


def test_every_page_of_the_file_listing_is_read():
    client = PagedListing(
        [
            {
                "continue": {
                    "aicontinue": "Portrait-b.png",
                    "continue": "-||",
                },
                "query": {"allimages": [{"title": "File:Portrait-a.png"}]},
            },
            {"query": {"allimages": [{"title": "File:Portrait-b.png"}]}},
        ]
    )
    assert registrar_module._files_with_bytes(client, sha1(PORTRAIT)) == [
        "File:Portrait-a.png",
        "File:Portrait-b.png",
    ]
    assert client.asked[0] == {
        "action": "query",
        "list": "allimages",
        "aisha1": sha1(PORTRAIT),
        "ailimit": "max",
    }
    # a continued listing asks the same SHA-1, from the cursor on
    assert client.asked[1] == {
        **client.asked[0],
        "aicontinue": "Portrait-b.png",
    }


def test_a_listing_without_files_is_none():
    assert (
        registrar_module._files_with_bytes(PagedListing([{}]), "0" * 40) == []
    )


def test_a_continuation_given_twice_fails_the_listing():
    """A listing that would ask the same page again and again fails, as a
    wiki call that fails does, rather than loop."""
    page = {
        "continue": {"aicontinue": "Portrait-b.png", "continue": "-||"},
        "query": {"allimages": [{"title": "File:Portrait-a.png"}]},
    }
    client = PagedListing([page, page, {}])
    with pytest.raises(RuntimeError, match="continuation twice"):
        registrar_module._files_with_bytes(client, sha1(PORTRAIT))
    assert len(client.asked) == 2


def test_a_flag_is_frozen():
    flag = RegistrationFlag(SHARED_FILE, "Q101", "0" * 40, ("Q100",))
    with pytest.raises(AttributeError):
        flag.kind = UNHASHED_TITLE  # type: ignore[misc]
    assert {SHARED_FILE, UNHASHED_TITLE} == REGISTRATION_CHECKS


def test_media_on_the_never_ingest_list_raise_no_flag(tmp_path):
    """Media the never-ingest list names are refused before anything is
    read: no flag is raised for them."""
    wiki = FakeWiki()
    unhashed(wiki, TITLE, "File:Portrait-1890s.png", PORTRAIT)
    exclusions = ExclusionList(sha1s=frozenset({sha1(PORTRAIT)}))
    registrar = Registrar(wiki, exclusions=exclusions)
    with pytest.raises(registrar_module.ExcludedMedia):
        registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
    assert registrar.flags == []


def test_the_instance_read_is_kept_current_with_the_files(tmp_path):
    """What a registrar read is kept current with the files it makes its
    items name, an artifact's and a derived medium's: it is what a fresh
    reading of the instance gives."""
    wiki = FakeWiki()
    registrar = Registrar(wiki)
    registrar.register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
    deck = deck_file(tmp_path, "a")
    registrar.register_artifact(deck, "presentation", "Deck A")
    record, directory = recorded(
        tmp_path, deck, "Deck A", [("image1.png", IMAGE, [1])]
    )
    registrar.register_derived_media(record, directory)

    fresh = _Instance.read(wiki, *wiki.vocabulary())
    assert registrar._read().files == fresh.files
    assert len(fresh.files) == 3
    assert registrar._read().unhashed == fresh.unhashed == set()


def test_an_excerpts_write_raises_its_pdfs_flags_on_the_excerpt(tmp_path):
    """An excerpt's write registers each PDF that places it, with the
    images it places: a flag of any of these new items is an alert on the
    excerpt. Its `medium` is the new item's own: the excerpt's for its
    own derived medium, the PDF's for the PDF (docs/ANOMALIES.md)."""
    from yizkr.pipeline.orchestrate import ExcerptRun
    from yizkr.pipeline.presentation import (
        EmbeddedMedium,
        PresentationContent,
        Slide,
        record_derived_media,
    )

    pdf_bytes = b"%PDF synthetic family history of 2008"
    wiki = FakeWiki()
    orchestrator, _ = test_registrar.TestStepRegister.orchestrator(
        tmp_path, wiki, [("family-history-2008.pdf", pdf_bytes)]
    )
    (pdf,) = orchestrator.documents()
    record = record_derived_media(
        PresentationContent(
            slides=[Slide(number=1, text="")],
            media=[EmbeddedMedium(name="image1.png", data=IMAGE, slides=[1])],
        ),
        source_sha1=pdf.sha1,
        title=pdf.state.title,
        directory=pdf.dir,
    )
    orchestrator._placed_images = lambda doc: record
    (medium,) = record.media.values()
    excerpt = ExcerptRun(sha1(IMAGE), pdf, medium, tmp_path / "state")
    # the PDF's file and the image's label, each on an item with no hash
    old_pdf = unhashed(
        wiki, pdf.state.title, "File:Family-history-2008.pdf", pdf_bytes
    )
    old_image = unhashed(
        wiki, f"{pdf.state.title} — image1.png", "File:Shop.png", OTHER_IMAGE
    )

    qid = orchestrator._step_register(excerpt)

    alerts = orchestrator.rundir.alerts()
    assert {alert["subject"] for alert in alerts} == {excerpt.sha1[:12]}
    by_medium = {}
    for alert in alerts:
        by_medium.setdefault(alert["medium"], []).append(
            (alert["check"], alert["others"])
        )
    assert by_medium == {
        pdf.sha1[:12]: [(UNHASHED_TITLE, [old_pdf]), (SHARED_FILE, [old_pdf])],
        excerpt.sha1[:12]: [(UNHASHED_TITLE, [old_image])],
    }
    assert pdf.sha1 != excerpt.sha1
    assert {
        alert["artifact"]
        for alert in alerts
        if alert["medium"] == excerpt.sha1[:12]
    } == {qid}


# -- a GEDCOM's media object and a registered artifact, in either order -------

# the family's GEDCOM export (tests/fixtures/example-family.ged): Sebastian
# Löbig, whose media object is the studio portrait, under the title the
# scan of that portrait is registered under
FAMILY_GEDCOM = (
    "0 HEAD\n1 CHAR UTF-8\n"
    "0 @I1@ INDI\n1 NAME Sebastian /Löbig/\n1 OBJE @O1@\n"
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
    f"2 TITL {TITLE}\n"
    "0 TRLR\n"
)
# the export with Hildegard too, whose media object is the 1905 wedding
# portrait, under a title no item holds
TWO_PEOPLE_GEDCOM = FAMILY_GEDCOM.replace(
    "0 TRLR\n",
    "0 @I2@ INDI\n1 NAME Hildegard /Löbig/\n1 OBJE @O2@\n"
    "0 @O2@ OBJE\n1 FILE wedding-1905.jpg\n"
    "2 TITL Wedding portrait, 1905\n"
    "0 TRLR\n",
)


# a combining mark Unicode 15.0 assigns and the Python 3.11 the repository
# pins leaves unassigned: the writer refuses text that holds it
UNKNOWN_MARK = "\U0001e08f"
needs_unknown_mark = pytest.mark.skipif(
    unicodedata.category(UNKNOWN_MARK) != "Cn",
    reason="this interpreter's Unicode tables assign U+1E08F",
)


class WritingWiki(FakeWiki):
    """The in-memory wiki with the surface the writer and resolve use
    beside the registrar's: its vocabulary, the label lookup and the label
    index, the statements of the entities asked for, and the media SHA-1
    index. The label lookup and index fail on a duplicate label, which
    the writer and registrar never make, though the wiki client's label
    index takes item homonyms (#94; test_homonym_labels.py)."""

    PROPERTIES = {
        **FakeWiki.PROPERTIES,
        "image": "P60",
        "inferred_from_extraction_run": "P61",
        "transcription_excerpt": "P62",
        "type_hint": "P63",
        "relative_position_within_image": "P64",
    }
    ITEMS = {**FakeWiki.ITEMS, "human": "Q9"}

    def find_item_by_label(self, label: str) -> str | None:
        assert_labels_unique(self)
        for qid in self.entities:
            if label_of(self, qid) == label:
                return qid
        return None

    def label_index(self) -> tuple[dict[str, str], dict[str, str]]:
        assert_labels_unique(self)
        return {}, {label_of(self, qid): qid for qid in self.entities}

    def fetch_claims(self, ids: list[str]) -> dict[str, dict]:
        return {
            qid: copy.deepcopy(self.entities[qid]["claims"])
            for qid in ids
            if qid in self.entities
        }

    def string_statement_index(self, pid: str) -> dict[str, str]:
        index: dict[str, str] = {}
        for qid in sorted(self.entities, key=lambda q: int(q[1:])):
            for statement in self.entities[qid]["claims"].get(pid, []):
                index.setdefault(
                    statement["mainsnak"]["datavalue"]["value"], qid
                )
        return index


def family_gedcom(wiki, text: str = FAMILY_GEDCOM):
    """The GEDCOM export's batch, its own item registered by its hash
    under a file name of its own, beside the export gedcom_media_object
    registers."""
    from yizkr.claims import ArtifactRef
    from yizkr.pipeline.gedcom import adapt_gedcom

    exported = ArtifactRef.from_bytes(text.encode())
    if not wiki.string_statement_index(P["media_sha1"]).get(exported.sha1):
        wiki.item(
            "family-tree-export.ged",
            ("instance_of", ITEMS["genealogical_report"], None),
            ("media_sha1", exported.sha1, None),
        )
    return adapt_gedcom(text, artifact=exported).batch


def naming(*labels: str):
    """A batch of the GEDCOM export's claim once for each of `labels`,
    its subject a ref with that label, no kind and no record."""
    from yizkr.claims import ArtifactRef, ClaimBatch
    from yizkr.pipeline.gedcom import adapt_gedcom

    exported = ArtifactRef.from_bytes(FAMILY_GEDCOM.encode())
    given = adapt_gedcom(FAMILY_GEDCOM, artifact=exported).batch.model_dump(
        mode="json"
    )
    (claim,) = given["claims"]
    given["claims"] = [
        dict(
            claim,
            subject={"kind": "local", "local_id": f"e{n}", "label": label},
        )
        for n, label in enumerate(labels)
    ]
    return ClaimBatch.model_validate(given)


def write_gedcom(wiki, text: str = FAMILY_GEDCOM):
    from yizkr.pipeline.writer import BatchWriter

    return BatchWriter(wiki).write_batch(family_gedcom(wiki, text))


def images(wiki, qid: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]["id"]
        for statement in wiki.entities[qid]["claims"].get(
            WritingWiki.PROPERTIES["image"], []
        )
    ]


class TestAGedcomsMediaObjectAndARegisteredArtifact:
    """A GEDCOM media object (a label, no file, no hash) and an artifact
    registered under its title are never one item, whichever is written
    first: the pair is flagged unhashed-title, with the artifact as the
    item flagged and the media object's item against it (ADR-0010)."""

    def write(self, tmp_path, wiki, order: str):
        """The scan registered and the GEDCOM written, in `order`: the
        registration's flags, the write's result, and the scan's item."""
        registrar = Registrar(wiki)
        if order == "gedcom-first":
            written = write_gedcom(wiki)
        registered = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        if order == "artifact-first":
            written = write_gedcom(wiki)
        return registrar.flags, written, registered.artifact_qid

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_the_two_are_never_one_item(self, tmp_path, order):
        wiki = WritingWiki()
        _, written, artifact = self.write(tmp_path, wiki, order)

        media_object = written.created_items["md1"]
        assert media_object != artifact
        assert strings(wiki, media_object, "media_sha1") == []
        assert strings(wiki, artifact, "instance_of") == [ITEMS["photograph"]]
        assert not wiki.entities[artifact].get("aliases")
        assert images(wiki, written.created_items["i1"]) == [media_object]
        assert_labels_unique(wiki)

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_the_pair_is_flagged_in_either_order(self, tmp_path, order):
        wiki = WritingWiki()
        registered, written, artifact = self.write(tmp_path, wiki, order)

        media_object = written.created_items["md1"]
        flag = (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        assert flag_tuples([*registered, *written.flags]) == [flag]

    def test_a_media_object_after_the_artifact_takes_a_tagged_label(
        self, tmp_path
    ):
        """The artifact keeps the title; the media object's item takes it
        with the writer's tag, so labels stay unique."""
        wiki = WritingWiki()
        _, written, artifact = self.write(tmp_path, wiki, "artifact-first")

        assert label_of(wiki, artifact) == TITLE
        assert (
            label_of(wiki, written.created_items["md1"])
            == f"{TITLE} [media object]"
        )

    def test_each_later_write_of_either_flags_the_pair_again(self, tmp_path):
        wiki = WritingWiki()
        _, first, artifact = self.write(tmp_path, wiki, "artifact-first")
        media_object = first.created_items["md1"]
        flag = (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))

        again = write_gedcom(wiki)
        assert again.created_items["md1"] == media_object
        assert flag_tuples(again.flags) == [flag]

        registrar = Registrar(wiki)
        registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        assert flag_tuples(registrar.flags) == [flag]
        assert_labels_unique(wiki)

    def test_a_derived_medium_under_the_title_is_never_joined(self, tmp_path):
        """A derived medium carries a hash of its own: a media object
        under its label is flagged against it as against an artifact."""
        wiki = WritingWiki()
        medium = wiki.item(
            TITLE,
            ("instance_of", ITEMS["photograph"], None),
            ("media_sha1", sha1(IMAGE), None),
        )
        written = write_gedcom(wiki)

        media_object = written.created_items["md1"]
        assert media_object != medium
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, medium, sha1(IMAGE), (media_object,))
        ]

    def test_a_first_dry_run_makes_nothing_and_flags_the_placeholder(
        self, tmp_path
    ):
        """A first dry run reports the pair the write would flag, against
        the placeholder of the media object's item, and writes nothing."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        batch = family_gedcom(wiki)
        before = copy.deepcopy(wiki.entities)

        written = BatchWriter(wiki, dry_run=True).write_batch(batch)

        assert written.created_items["md1"] == "(new:md1)"
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, artifact, sha1(PORTRAIT), ("(new:md1)",))
        ]
        assert wiki.entities == before

    def test_a_dry_run_after_a_write_flags_the_pair_it_finds(self, tmp_path):
        """Once the media object's tagged item is there, a dry run finds it
        and reports the flag a write raises, writing nothing."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        _, first, artifact = self.write(tmp_path, wiki, "artifact-first")
        media_object = first.created_items["md1"]
        batch = family_gedcom(wiki)
        before = copy.deepcopy(wiki.entities)

        written = BatchWriter(wiki, dry_run=True).write_batch(batch)

        assert written.created_items["md1"] == media_object
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        ]
        assert wiki.entities == before

    def test_an_entity_of_an_artifact_class_is_never_joined(self, tmp_path):
        """Any artifact kind, not only `artifact`: an entity of a class
        the manifest makes a subclass of it, here a photograph, is kept
        from a registered artifact under its label too."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        given = family_gedcom(wiki).model_dump(mode="json")
        (record,) = [e for e in given["entities"] if e["id"] == "md1"]
        record["kind"] = "photograph"
        batch = type(family_gedcom(wiki)).model_validate(given)

        written = BatchWriter(wiki).write_batch(batch)

        media_object = written.created_items["md1"]
        assert media_object != artifact
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        ]

    def test_a_title_at_the_term_limit_keeps_its_tag_within_it(self, tmp_path):
        """A title as long as Wikibase takes a label: the tagged label is
        cut to leave room for the tag, so it fits, and a later
        registration still finds the pair under it."""
        from yizkr.pipeline.writer import TERM_LIMIT, media_object_label

        long_title = ("Studio portrait of the Löbig family, Hamburg " * 6)[
            :TERM_LIMIT
        ].strip()
        assert len(long_title) > TERM_LIMIT - len(" [media object]")
        tagged = media_object_label(long_title)
        assert len(tagged) <= TERM_LIMIT
        assert tagged.endswith(" [media object]")

        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(
                scan(tmp_path, PORTRAIT), "photograph", long_title
            )
            .artifact_qid
        )
        written = write_gedcom(wiki, FAMILY_GEDCOM.replace(TITLE, long_title))
        media_object = written.created_items["md1"]
        assert label_of(wiki, media_object) == tagged
        flag = (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        assert flag_tuples(written.flags) == [flag]

        again = Registrar(wiki)
        again.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", long_title
        )
        assert flag_tuples(again.flags) == [flag]

    def both_labels_registered(self, wiki) -> tuple[str, str]:
        """Two registered items, the scan under the media object's title
        and another scan under its tagged label."""
        return tuple(
            wiki.item(
                label,
                ("instance_of", ITEMS["photograph"], None),
                ("media_sha1", sha1(data), None),
            )
            for label, data in (
                (TITLE, PORTRAIT),
                (f"{TITLE} [media object]", RESCAN),
            )
        )

    def test_two_registered_items_under_both_labels_refuse_the_entity(
        self, tmp_path
    ):
        """The media object may join neither: it is refused, with the
        claims that name it, before the first write, and the rest of the
        export is written. The warning names the two items by QID alone,
        and the scan is flagged against the item under the tagged
        label."""
        from yizkr.pipeline.review import claim_id

        wiki = WritingWiki()
        plain, tagged = self.both_labels_registered(wiki)
        batch = family_gedcom(wiki, TWO_PEOPLE_GEDCOM)
        named = [
            claim_id(claim)
            for claim in batch.claims
            if claim.value.value.local_id == "md1"
        ]
        assert len(named) == 1

        for _ in range(2):
            written = write_gedcom(wiki, TWO_PEOPLE_GEDCOM)

            assert written.refused == named
            assert "md1" not in written.created_items
            assert "i1" not in written.created_items
            assert images(wiki, written.created_items["i2"]) == [
                written.created_items["md2"]
            ]
            assert written.written + written.skipped == 1
            assert flag_tuples(written.flags) == [
                (UNHASHED_TITLE, plain, sha1(PORTRAIT), (tagged,))
            ]
            (warning,) = [w for w in written.warnings if "'md1'" in w]
            assert plain in warning and tagged in warning
            assert named[0] in warning
            assert TITLE not in warning
            assert "Löbig" not in warning
        labels = [label_of(wiki, qid) for qid in wiki.entities]
        assert "Sebastian Löbig" not in labels
        assert [label for label in labels if TITLE in label] == [
            TITLE,
            f"{TITLE} [media object]",
        ]
        assert not wiki.entities[plain].get("aliases")
        assert not wiki.entities[tagged].get("aliases")
        assert_labels_unique(wiki)

    def test_the_plan_is_made_again_without_the_refused_claims(self, tmp_path):
        """Sebastian is named only by the refused media object's claim:
        his name, which Hildegard's media object is titled with, is free
        once that claim is refused, so the media object takes it, as it
        would in a batch without the claim."""
        wiki = WritingWiki()
        self.both_labels_registered(wiki)
        text = TWO_PEOPLE_GEDCOM.replace(
            "Wedding portrait, 1905", "Sebastian Löbig"
        )

        written = write_gedcom(wiki, text)

        assert "i1" not in written.created_items
        assert label_of(wiki, written.created_items["md2"]) == (
            "Sebastian Löbig"
        )

    def test_the_refusal_comes_before_the_first_write(self, tmp_path):
        """Nothing is written when every claim names a refused entity, but
        the run item; a dry run reports the same refusal."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        self.both_labels_registered(wiki)
        batch = family_gedcom(wiki)
        before = copy.deepcopy(wiki.entities)

        planned = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert wiki.entities == before
        written = BatchWriter(wiki).write_batch(batch)

        assert planned.refused == written.refused != []
        assert flag_tuples(planned.flags) == flag_tuples(written.flags)
        made = set(wiki.entities) - set(before)
        assert made == {written.run_qid}

    @staticmethod
    def with_media_object(batch, **terms):
        """`batch` with the terms `terms` of the media object md1's
        manifest record replaced."""
        from yizkr.claims import ClaimBatch

        given = batch.model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "md1":
                record.update(terms)
        return ClaimBatch.model_validate(given)

    @needs_unknown_mark
    @pytest.mark.parametrize("blocked", [True, False])
    def test_a_blocked_entitys_marked_description_is_not_counted(
        self, blocked
    ):
        """The media object's description holds a character Python's
        Unicode tables do not know (#444, #431). Blocked, it is refused
        with its claim, once, and its description, which only an item a
        written claim mints would take, is neither named in
        refused_terms nor warned of. Not blocked, its item is made and the
        description is named, as the writer leaves it out."""
        from yizkr.pipeline.review import claim_id
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        if blocked:
            self.both_labels_registered(wiki)
        batch = self.with_media_object(
            family_gedcom(wiki, TWO_PEOPLE_GEDCOM),
            summary=f"the studio portrait, Flörburg {UNKNOWN_MARK}",
        )
        named = [
            claim_id(claim)
            for claim in batch.claims
            if claim.value.value.local_id == "md1"
        ]

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == (named if blocked else [])
        assert written.refused_terms == (
            [] if blocked else ["md1: description"]
        )
        described = [
            w for w in written.warnings if "'md1' has a description" in w
        ]
        assert len(described) == (0 if blocked else 1)
        assert ("md1" in written.created_items) is not blocked

    @needs_unknown_mark
    def test_a_marked_label_is_refused_once_and_never_planned(
        self, monkeypatch
    ):
        """A media object whose label holds a character Python's Unicode
        tables do not know is refused by that rule, with its claim, once:
        the plan is made without that claim, so it never places the
        entity nor looks its label up, and no warning calls it blocked."""
        from yizkr.pipeline.review import claim_id
        from yizkr.pipeline.writer import BatchWriter

        placed: list[str] = []
        place = BatchWriter._place

        def spied(self, label, *args):
            placed.append(label)
            return place(self, label, *args)

        monkeypatch.setattr(BatchWriter, "_place", spied)
        wiki = WritingWiki()
        self.both_labels_registered(wiki)
        batch = self.with_media_object(
            family_gedcom(wiki, TWO_PEOPLE_GEDCOM),
            label=f"{TITLE}{UNKNOWN_MARK}",
        )
        named = [
            claim_id(claim)
            for claim in batch.claims
            if claim.value.value.local_id == "md1"
        ]

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == named
        assert written.refused_terms == ["md1: label"]
        assert placed and not [p for p in placed if UNKNOWN_MARK in p]
        assert not [w for w in written.warnings if "is refused, with" in w]
        assert "md1" not in written.created_items

    @staticmethod
    def with_marked_excerpt(batch, local_id: str):
        """`batch` with a character Python's Unicode tables do not know at
        the end of the excerpt of each claim whose value is `local_id`, so
        the writer refuses each such claim for its text."""
        from yizkr.claims import ClaimBatch

        given = batch.model_dump(mode="json")
        for claim in given["claims"]:
            if claim["value"]["value"].get("local_id") == local_id:
                claim["source"]["transcription_excerpt"]["text"] += (
                    UNKNOWN_MARK
                )
        return ClaimBatch.model_validate(given)

    @needs_unknown_mark
    def test_an_unsent_records_long_type_hint_is_not_warned_of(self):
        """The media object's one claim is refused for its text, so nothing
        of its record is sent: its type hint, over the limit, is not
        warned of, as one holding an unknown character is not named."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import TEXT_LIMIT, BatchWriter

        wiki = WritingWiki()
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "md1":
                record["type_hint"] = "Kabinettkarte " * (TEXT_LIMIT // 14 + 1)
        batch = self.with_marked_excerpt(
            ClaimBatch.model_validate(given), "md1"
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert len(written.refused) == 1
        assert not [w for w in written.warnings if "type hint of" in w]
        assert "md1" not in written.created_items

    @needs_unknown_mark
    def test_a_media_object_only_a_refused_claim_names_is_never_placed(
        self, monkeypatch
    ):
        """Registered artifacts hold both labels of the media object, and
        the excerpt of its one claim holds a character Python's Unicode
        tables do not know: the claim is refused for its text, so the plan,
        made without it, never places the media object, which is neither
        blocked nor flagged."""
        from yizkr.pipeline.review import claim_id
        from yizkr.pipeline.writer import BatchWriter

        placed: list[str] = []
        place = BatchWriter._place

        def spied(self, label, *args):
            placed.append(label)
            return place(self, label, *args)

        monkeypatch.setattr(BatchWriter, "_place", spied)
        wiki = WritingWiki()
        self.both_labels_registered(wiki)
        batch = self.with_marked_excerpt(
            family_gedcom(wiki, TWO_PEOPLE_GEDCOM), "md1"
        )
        named = [
            claim_id(claim)
            for claim in batch.claims
            if claim.value.value.local_id == "md1"
        ]

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == named
        assert written.flags == []
        assert not [w for w in written.warnings if "is refused, with" in w]
        assert placed and TITLE not in placed
        assert "md1" not in written.created_items
        assert "md2" in written.created_items

    @needs_unknown_mark
    @pytest.mark.parametrize("refusal", ["blocked", "marked"])
    def test_a_records_kind_is_warned_of_whatever_its_claims(self, refusal):
        """Sebastian's record gives the kind 'pet', which the vocabulary
        lacks, and his one claim is refused, with the media object the plan
        blocks or for its marked excerpt: the record's kind is warned of
        all the same. A second claim about him, refused for its marked
        excerpt, has a place without a record of the kind 'lighthouse',
        which the vocabulary lacks too: a ref's kind is warned of only when
        a written claim mentions it, so it is not."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        if refusal == "blocked":
            self.both_labels_registered(wiki)
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "i1":
                record["kind"] = "pet"
                record.pop("structure", None)
        (claim,) = given["claims"]
        place = {
            "kind": "local",
            "local_id": "pl1",
            "label": "Flörburg",
            "instance_of": "lighthouse",
        }
        given["claims"].append(
            dict(claim, value={"type": "item", "value": place})
        )
        batch = self.with_marked_excerpt(
            ClaimBatch.model_validate(given), "pl1"
        )
        if refusal == "marked":
            batch = self.with_marked_excerpt(batch, "md1")

        written = BatchWriter(wiki).write_batch(batch)

        assert len(written.refused) == 2
        assert written.created_items == {}
        kinds = [w for w in written.warnings if "vocabulary lacks" in w]
        assert kinds == [
            "entity 'i1' has the kind 'pet', which the vocabulary lacks: "
            "no kind is recorded for it"
        ]

    @needs_unknown_mark
    @pytest.mark.parametrize("refusal", ["written", "blocked", "marked"])
    def test_an_unsent_records_alias_and_type_hint_are_not_counted(
        self, refusal
    ):
        """The media object's record has an alias and a type hint that
        hold a character Python's Unicode tables do not know. Written, its
        item is made and both are named in refused_terms, as the writer
        leaves them out. Blocked, or with its one claim refused for its
        marked excerpt, nothing of the record is sent, so neither is
        named nor warned of."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        if refusal == "blocked":
            self.both_labels_registered(wiki)
        batch = self.with_media_object(
            family_gedcom(wiki, TWO_PEOPLE_GEDCOM),
            aliases=[f"the studio portrait {UNKNOWN_MARK}"],
            type_hint=f"cabinet card {UNKNOWN_MARK}",
        )
        if refusal == "marked":
            batch = self.with_marked_excerpt(batch, "md1")

        written = BatchWriter(wiki).write_batch(batch)

        sent = refusal == "written"
        assert ("md1" in written.created_items) is sent
        assert written.refused_terms == (
            ["md1: alias", "md1: type hint"] if sent else []
        )
        left_out = [w for w in written.warnings if "'md1' has a" in w]
        assert len(left_out) == (2 if sent else 0)

    @needs_unknown_mark
    def test_an_equivalences_aliases_are_sent_whatever_its_claims(self):
        """Sebastian's record names his item by an approved equivalence,
        and his one claim is refused for its marked excerpt: the record's
        aliases go on his item before any claim is written all the same,
        so the alias the writer leaves out is named in refused_terms."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        sebastian = wiki.item(
            "Sebastian Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "i1":
                record["qid"] = sebastian
                record["aliases"] = ["Wastl", f"Wastl {UNKNOWN_MARK}"]
        batch = self.with_marked_excerpt(
            ClaimBatch.model_validate(given), "md1"
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert len(written.refused) == 1
        assert written.refused_terms == ["i1: alias"]
        assert wiki.entities[sebastian]["aliases"]["en"] == [
            {"language": "en", "value": "Wastl"}
        ]

    def test_a_blocked_records_terms_never_reach_the_item_under_its_label(
        self,
    ):
        """Registered artifacts hold both labels of the media object, which
        is blocked, and a written claim names the scan under its label by
        QID: the media object's record names no item by its label, so the
        scan gains neither its alias nor its kind."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        plain, _ = self.both_labels_registered(wiki)
        given = self.with_media_object(
            family_gedcom(wiki, TWO_PEOPLE_GEDCOM),
            aliases=["the studio portrait"],
        ).model_dump(mode="json")
        for claim in given["claims"]:
            if claim["value"]["value"]["local_id"] == "md2":
                claim["value"]["value"] = {"kind": "qid", "qid": plain}
        batch = ClaimBatch.model_validate(given)
        before = copy.deepcopy(wiki.entities[plain])

        written = BatchWriter(wiki).write_batch(batch)

        assert len(written.refused) == 1
        assert "i2" in written.created_items
        assert images(wiki, written.created_items["i2"]) == [plain]
        assert wiki.entities[plain] == before

    def test_a_reused_writer_reads_its_items_afresh_each_write(self):
        """The media object joins the portrait's item, registered before
        content hashing, and a curator then hashes that item: the same
        writer's next write judges it registered, as a new writer's
        would, so the media object takes its tagged label and the
        portrait is flagged against it."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        old = unhashed(wiki, TITLE, "File:Studio portrait.png", PORTRAIT)
        batch = family_gedcom(wiki)
        writer = BatchWriter(wiki)
        first = writer.write_batch(batch)
        assert first.created_items["md1"] == old
        assert first.flags == []
        hash_item(wiki, old, PORTRAIT)

        again = writer.write_batch(batch)

        media_object = again.created_items["md1"]
        assert media_object != old
        assert label_of(wiki, media_object) == f"{TITLE} [media object]"
        assert flag_tuples(again.flags) == [
            (UNHASHED_TITLE, old, sha1(PORTRAIT), (media_object,))
        ]
        assert flag_tuples(BatchWriter(wiki).write_batch(batch).flags) == (
            flag_tuples(again.flags)
        )

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_a_scan_and_a_rescan_under_the_title_are_both_flagged(
        self, tmp_path, order
    ):
        """Two scans of other bytes registered under the media object's
        title, one labelled with it and one with it content-tagged: each
        is flagged against the media object's item, whichever is written
        first."""
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        if order == "gedcom-first":
            written = write_gedcom(wiki)
        first = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        ).artifact_qid
        second = registrar.register_artifact(
            scan(tmp_path, RESCAN, name="portrait-rescan.png", folder="s2"),
            "photograph",
            TITLE,
        ).artifact_qid
        if order == "artifact-first":
            written = write_gedcom(wiki)

        media_object = written.created_items["md1"]
        assert media_object not in {first, second}
        assert sorted(flag_tuples([*registrar.flags, *written.flags])) == [
            (UNHASHED_TITLE, first, sha1(PORTRAIT), (media_object,)),
            (UNHASHED_TITLE, second, sha1(RESCAN), (media_object,)),
        ]
        assert_labels_unique(wiki)

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_a_media_object_under_a_fragments_label_is_never_joined(
        self, tmp_path, order
    ):
        """A media object titled as a page fragment of a registered
        artifact is never that fragment: the artifact is flagged against
        the media object's item, whichever is written first."""
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        text = FAMILY_GEDCOM.replace(TITLE, f"{LETTER_TITLE}, page 1")
        if order == "gedcom-first":
            written = write_gedcom(wiki, text)
        registered = registrar.register_artifact(
            scan(tmp_path, LETTER, name="letter.png"),
            "photograph",
            LETTER_TITLE,
            pages=2,
        )
        if order == "artifact-first":
            written = write_gedcom(wiki, text)

        media_object = written.created_items["md1"]
        assert media_object != registered.artifact_qid
        assert media_object not in registered.fragment_qids.values()
        assert strings(wiki, media_object, "part_of") == []
        assert images(wiki, written.created_items["i1"]) == [media_object]
        flag = (
            UNHASHED_TITLE,
            registered.artifact_qid,
            sha1(LETTER),
            (media_object,),
        )
        assert flag_tuples([*registrar.flags, *written.flags]) == [flag]
        assert_labels_unique(wiki)

        # and on every later registration, and, the media object's item
        # tagged, every later write of the GEDCOM
        if order == "artifact-first":
            assert flag_tuples(write_gedcom(wiki, text).flags) == [flag]
        again = Registrar(wiki)
        again.register_artifact(
            scan(tmp_path, LETTER, name="letter.png"),
            "photograph",
            LETTER_TITLE,
        )
        assert flag_tuples(again.flags) == [flag]

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_a_rescans_page_fragment_is_flagged_in_either_order(
        self, tmp_path, order
    ):
        """A scan and a re-scan of the 1952 letter, each with two pages,
        registered under one title: the re-scan's fragment is labelled
        with its content tag before ", page 1", and is flagged against a
        media object titled as the first page, as the scan's is,
        whichever is written first; the write flags them in the order of
        their items."""
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        text = FAMILY_GEDCOM.replace(TITLE, f"{LETTER_TITLE}, page 1")
        if order == "gedcom-first":
            written = write_gedcom(wiki, text)
        first = registrar.register_artifact(
            scan(tmp_path, LETTER, name="letter.png"),
            "photograph",
            LETTER_TITLE,
            pages=2,
        )
        second = registrar.register_artifact(
            scan(tmp_path, LETTER_RESCAN, name="letter.png", folder="s2"),
            "photograph",
            LETTER_TITLE,
            pages=2,
        )
        if order == "artifact-first":
            tag = sha1(LETTER_RESCAN)[:8]
            assert label_of(wiki, second.fragment_qids[1]) == (
                f"{LETTER_TITLE} [{tag}], page 1"
            )
            written = write_gedcom(wiki, text)

        media_object = written.created_items["md1"]
        flags = [
            (
                UNHASHED_TITLE,
                first.artifact_qid,
                sha1(LETTER),
                (media_object,),
            ),
            (
                UNHASHED_TITLE,
                second.artifact_qid,
                sha1(LETTER_RESCAN),
                (media_object,),
            ),
        ]
        assert flag_tuples([*registrar.flags, *written.flags]) == flags
        assert_labels_unique(wiki)

    def test_two_media_objects_under_one_title_raise_one_flag(self, tmp_path):
        """Two objects of one export titled as the scan: both are the one
        tagged item, and the scan is flagged against it once."""
        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        text = TWO_PEOPLE_GEDCOM.replace("Wedding portrait, 1905", TITLE)

        written = write_gedcom(wiki, text)

        media_object = written.created_items["md1"]
        assert written.created_items["md2"] == media_object
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        ]

    def test_a_media_objects_other_titles_go_on_its_own_item(self, tmp_path):
        """The object's second title, and its own title, are aliases of its
        tagged item, on each write, never of the scan's."""
        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        text = FAMILY_GEDCOM.replace(
            f"2 TITL {TITLE}\n", f"2 TITL {TITLE}\n2 TITL Atelierbild Löbig\n"
        )
        media_object = write_gedcom(wiki, text).created_items["md1"]
        wiki.entities[media_object]["aliases"] = {}

        assert write_gedcom(wiki, text).created_items["md1"] == media_object

        assert sorted(
            alias["value"]
            for alias in wiki.entities[media_object]["aliases"]["en"]
        ) == ["Atelierbild Löbig", TITLE]
        assert not wiki.entities[artifact].get("aliases")

    def test_a_bracketed_suffix_that_is_no_content_tag_is_not_flagged(
        self, tmp_path
    ):
        """Only " [" and eight hex digits and "]" is a content tag: a
        registered copy titled with another bracketed suffix is another
        title, and is not flagged."""
        wiki = WritingWiki()
        plain, copy_ = (
            wiki.item(
                label,
                ("instance_of", ITEMS["photograph"], None),
                ("media_sha1", sha1(data), None),
            )
            for label, data in (
                (TITLE, PORTRAIT),
                (f"{TITLE} [Kopie 01]", RESCAN),
            )
        )
        written = write_gedcom(wiki)
        assert flag_tuples(written.flags) == [
            (
                UNHASHED_TITLE,
                plain,
                sha1(PORTRAIT),
                (written.created_items["md1"],),
            )
        ]
        assert copy_ not in {flag.artifact for flag in written.flags}

    def test_a_page_the_artifact_lacks_is_not_flagged(self, tmp_path):
        """Registration compares only the labels of the artifact's own
        fragments: a media object titled as a third page of a scan with
        one page is not flagged, though another artifact has a third
        page."""
        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, LETTER, name="letter.png"),
            "photograph",
            LETTER_TITLE,
            pages=3,
        )
        gedcom_media_object(wiki, f"{TITLE}, page 3")
        registrar = Registrar(wiki)

        registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE, pages=1
        )

        assert registrar.flags == []

    @pytest.mark.parametrize("order", ["artifact-first", "gedcom-first"])
    def test_a_title_with_a_bracketed_date_finds_its_rescan(
        self, tmp_path, order
    ):
        """A title that holds eight digits in brackets, as a date can: the
        re-scan's label is the title with its content tag after the date,
        and taking out the date is not taking out the tag, so both scans
        are flagged against the media object, whichever is written
        first."""
        title = f"{LETTER_TITLE} [19520312]"
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        text = FAMILY_GEDCOM.replace(TITLE, title)
        if order == "gedcom-first":
            written = write_gedcom(wiki, text)
        first, second = (
            registrar.register_artifact(
                scan(tmp_path, data, name="letter.png", folder=folder),
                "photograph",
                title,
            ).artifact_qid
            for data, folder in ((LETTER, "s1"), (LETTER_RESCAN, "s2"))
        )
        if order == "artifact-first":
            assert label_of(wiki, second) == (
                f"{title} [{sha1(LETTER_RESCAN)[:8]}]"
            )
            written = write_gedcom(wiki, text)

        media_object = written.created_items["md1"]
        assert flag_tuples([*registrar.flags, *written.flags]) == [
            (UNHASHED_TITLE, first, sha1(LETTER), (media_object,)),
            (UNHASHED_TITLE, second, sha1(LETTER_RESCAN), (media_object,)),
        ]

    @pytest.mark.parametrize("order", ["scan-first", "gedcom-first"])
    def test_a_scan_under_a_persons_name_is_flagged_in_either_order(
        self, tmp_path, order
    ):
        """A person holds the name the portrait's scan and its media object
        are both titled with: the scan is content-tagged under it, the
        media object takes its tagged label, and the pair is flagged once,
        by the registration or by the write, whichever comes second."""
        wiki = WritingWiki()
        person = wiki.item(
            "Sebastian Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        text = FAMILY_GEDCOM.replace(TITLE, "Sebastian Löbig")
        registrar = Registrar(wiki)
        if order == "gedcom-first":
            written = write_gedcom(wiki, text)
        artifact = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        ).artifact_qid
        if order == "scan-first":
            written = write_gedcom(wiki, text)

        media_object = written.created_items["md1"]
        assert written.created_items["i1"] == person
        assert label_of(wiki, person) == "Sebastian Löbig"
        assert label_of(wiki, artifact) == (
            f"Sebastian Löbig [{sha1(PORTRAIT)[:8]}]"
        )
        assert label_of(wiki, media_object) == "Sebastian Löbig [media object]"
        assert flag_tuples([*registrar.flags, *written.flags]) == [
            (UNHASHED_TITLE, artifact, sha1(PORTRAIT), (media_object,))
        ]
        assert_labels_unique(wiki)

    def test_a_refusal_with_no_item_under_the_tagged_label_flags_nothing(
        self, tmp_path
    ):
        """A person of the batch is named as the media object's tagged
        label, and a registered scan holds its title: the media object is
        refused, and as no item holds its tagged label, nothing is flagged
        against one; the warning says the batch makes that item."""
        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        text = FAMILY_GEDCOM.replace(
            "1 NAME Sebastian /Löbig/", f"1 NAME {TITLE} [media object]"
        )

        written = write_gedcom(wiki, text)

        assert "md1" not in written.created_items
        assert len(written.refused) == 1
        assert written.flags == []
        (warning,) = [w for w in written.warnings if "'md1'" in w]
        assert artifact in warning
        assert "an item this batch makes" in warning


class TestOtherKindsStillMatchByLabel:
    """An artifact-kind entity joins by its label only an item with no hash
    that is an artifact or a part of one, such as another export's media
    object; any other entity only an item that is neither, as before
    (ADR-0010, "What is still matched by label"). Where it may not join
    the item under its label, it is an item of its own under its tagged
    label, its own label an alias, and no flag is raised unless an
    artifact is registered under a media object's label."""

    def test_a_record_no_claim_names_never_names_a_media_objects_item(
        self,
    ):
        """Sebastian Löbig, whom the export gives a name alone and no
        claim names, and Hildegard's media object titled with his name:
        a rewrite of the export finds the media object's item again and
        names it with nothing of his record."""
        from yizkr.pipeline.writer import BatchWriter

        text = (
            "0 HEAD\n1 CHAR UTF-8\n"
            "0 @I1@ INDI\n1 NAME Sebastian /Löbig/\n"
            "0 @I2@ INDI\n1 NAME Hildegard /Löbig/\n1 OBJE @O1@\n"
            "0 @O1@ OBJE\n1 FILE sebastian.jpg\n2 TITL Sebastian Löbig\n"
            "0 TRLR\n"
        )
        wiki = WritingWiki()
        first = BatchWriter(wiki).write_batch(family_gedcom(wiki, text))
        media = first.created_items["md1"]
        kept = copy.deepcopy(wiki.entities[media])

        again = BatchWriter(wiki).write_batch(family_gedcom(wiki, text))

        assert again.created_items["md1"] == media
        assert again.type_statements_written == 0
        assert wiki.entities[media] == kept
        assert WritingWiki.ITEMS["human"] not in strings(
            wiki, media, "instance_of"
        )

    def test_a_media_object_record_never_names_a_registered_scan(
        self, tmp_path
    ):
        """The studio portrait's scan is registered under its title; the
        export's media object of that title, with an alias and a type
        hint, is named by no claim, while a claim names the scan by its
        QID: the scan gains nothing of the media object's record."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        artifact = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "md1":
                record["aliases"] = ["the studio portrait"]
                record["type_hint"] = "cabinet card"
        (claim,) = given["claims"]
        claim["value"] = {
            "type": "item",
            "value": {"kind": "qid", "qid": artifact.artifact_qid},
        }
        kept = copy.deepcopy(wiki.entities[artifact.artifact_qid])

        written = BatchWriter(wiki).write_batch(
            ClaimBatch.model_validate(given)
        )

        assert written.type_statements_written == 0
        assert wiki.entities[artifact.artifact_qid] == kept

    def test_a_media_object_record_never_names_a_persons_item(self):
        """Gustav Löbig's item exists, and a claim names it by its QID as
        the value of Sebastian's image; the export's media object titled
        with Gustav's name, with an alias and a type hint, is named by no
        claim: Gustav's item gains nothing of the media object's record."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        gustav = wiki.item(
            "Gustav Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "md1":
                record["label"] = "Gustav Löbig"
                record["aliases"] = ["the studio portrait"]
                record["type_hint"] = "cabinet card"
        (claim,) = given["claims"]
        claim["value"] = {
            "type": "item",
            "value": {"kind": "qid", "qid": gustav},
        }
        kept = copy.deepcopy(wiki.entities[gustav])

        BatchWriter(wiki).write_batch(ClaimBatch.model_validate(given))

        assert wiki.entities[gustav] == kept

    def test_a_person_joins_the_item_under_its_label(self, tmp_path):
        wiki = WritingWiki()
        person = wiki.item(
            "Sebastian Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        written = write_gedcom(wiki)
        assert written.created_items["i1"] == person
        assert written.flags == []

    def test_a_person_under_a_registered_artifacts_label_is_its_own_item(
        self, tmp_path
    ):
        """The artifact keeps its label and gains nothing; the person is an
        item of its own, under its tagged label, its name an alias, found
        again by it on a re-run."""
        wiki = WritingWiki()
        artifact = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )
        before = copy.deepcopy(wiki.entities[artifact.artifact_qid])

        written = write_gedcom(wiki)

        person = written.created_items["i1"]
        assert person != artifact.artifact_qid
        assert label_of(wiki, person) == "Sebastian Löbig [not an artifact]"
        assert [
            alias["value"] for alias in wiki.entities[person]["aliases"]["en"]
        ] == ["Sebastian Löbig"]
        assert strings(wiki, person, "instance_of") == ["Q9"]
        assert wiki.entities[artifact.artifact_qid] == before
        assert written.flags == []
        assert write_gedcom(wiki).created_items["i1"] == person
        assert_labels_unique(wiki)

    def test_a_person_written_before_the_artifact_keeps_its_item(
        self, tmp_path
    ):
        """In the other order the person holds its name: registration makes
        the artifact under it content-tagged, never the person's item, and
        flags nothing, as a person is no item with no hash; a re-run finds
        the person's item."""
        wiki = WritingWiki()
        person = write_gedcom(wiki).created_items["i1"]
        registrar = Registrar(wiki)

        artifact = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        ).artifact_qid

        assert artifact != person
        assert label_of(wiki, person) == "Sebastian Löbig"
        assert label_of(wiki, artifact) == (
            f"Sebastian Löbig [{sha1(PORTRAIT)[:8]}]"
        )
        assert strings(wiki, person, "media_sha1") == []
        assert registrar.flags == []
        assert write_gedcom(wiki).created_items["i1"] == person

    def test_a_person_never_joins_an_unhashed_media_object(self, tmp_path):
        """An item with no hash of an artifact kind, another export's media
        object under the person's name, is never the person's item."""
        wiki = WritingWiki()
        older = gedcom_media_object(wiki, "Sebastian Löbig")
        written = write_gedcom(wiki)
        person = written.created_items["i1"]
        assert person != older
        assert label_of(wiki, person) == "Sebastian Löbig [not an artifact]"
        assert written.flags == []

    def test_a_person_whose_two_labels_are_artifacts_is_refused(
        self, tmp_path
    ):
        """Artifacts under the person's name and under its tagged label: the
        person is refused, with its claims, and nothing is flagged, as no
        media object is involved."""
        from yizkr.pipeline.review import claim_id

        wiki = WritingWiki()
        plain, tagged = (
            wiki.item(
                label,
                ("instance_of", ITEMS["photograph"], None),
                ("media_sha1", sha1(data), None),
            )
            for label, data in (
                ("Sebastian Löbig", PORTRAIT),
                ("Sebastian Löbig [not an artifact]", RESCAN),
            )
        )
        batch = family_gedcom(wiki, TWO_PEOPLE_GEDCOM)

        written = write_gedcom(wiki, TWO_PEOPLE_GEDCOM)

        assert written.refused == [
            claim_id(claim)
            for claim in batch.claims
            if claim.subject.local_id == "i1"
        ]
        assert {"i1", "md1"}.isdisjoint(written.created_items)
        assert "i2" in written.created_items
        assert written.flags == []
        (warning,) = [w for w in written.warnings if "'i1'" in w]
        assert plain in warning and tagged in warning
        assert "Löbig" not in warning

    def test_a_portrait_titled_as_its_sitter_is_an_item_of_its_own(
        self, tmp_path
    ):
        """A media object titled with the name of the person it shows, in
        one export: the person is the item under the name, the media
        object an item of its own under its tagged label, unflagged, and
        a re-run finds each again."""
        wiki = WritingWiki()
        text = FAMILY_GEDCOM.replace(TITLE, "Sebastian Löbig")

        written = write_gedcom(wiki, text)

        person, media_object = (
            written.created_items[i] for i in ("i1", "md1")
        )
        assert person != media_object
        assert label_of(wiki, person) == "Sebastian Löbig"
        assert label_of(wiki, media_object) == "Sebastian Löbig [media object]"
        assert images(wiki, person) == [media_object]
        assert images(wiki, media_object) == []
        assert written.flags == []
        again = write_gedcom(wiki, text)
        assert again.created_items == written.created_items
        assert_labels_unique(wiki)

    def test_a_media_object_never_joins_a_person_under_its_title(
        self, tmp_path
    ):
        wiki = WritingWiki()
        person = wiki.item(
            "Sebastian Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        text = FAMILY_GEDCOM.replace(TITLE, "Sebastian Löbig")

        written = write_gedcom(wiki, text)

        assert written.created_items["i1"] == person
        media_object = written.created_items["md1"]
        assert label_of(wiki, media_object) == "Sebastian Löbig [media object]"
        assert ITEMS["artifact"] not in strings(wiki, person, "instance_of")
        assert images(wiki, person) == [media_object]
        assert written.flags == []

    def test_two_media_objects_of_one_export_join_one_item(self, tmp_path):
        wiki = WritingWiki()
        text = TWO_PEOPLE_GEDCOM.replace("Wedding portrait, 1905", TITLE)
        written = write_gedcom(wiki, text)
        assert written.created_items["md1"] == written.created_items["md2"]
        assert label_of(wiki, written.created_items["md1"]) == TITLE

    def test_a_media_object_joins_a_fragment_of_an_unhashed_artifact(
        self, tmp_path
    ):
        """On an instance from before content hashing, a fragment of an
        artifact with no hash is a part of an artifact with no hash: a
        media object titled as it joins it, unflagged, as it joins the
        artifact; a person never does."""
        page = f"{LETTER_TITLE}, page 1"

        def legacy():
            wiki = WritingWiki()
            whole = unhashed(
                wiki, LETTER_TITLE, "File:Letter-to-sipkje-1952.png", LETTER
            )
            fragment = wiki.item(
                page,
                ("instance_of", ITEMS["artifact_fragment"], None),
                ("part_of", whole, "1"),
                stated_in=whole,
            )
            return wiki, fragment

        wiki, fragment = legacy()
        written = write_gedcom(wiki, FAMILY_GEDCOM.replace(TITLE, page))
        assert written.created_items["md1"] == fragment
        assert written.flags == []

        wiki, fragment = legacy()
        text = FAMILY_GEDCOM.replace("Sebastian /Löbig/", f"/{page}/")
        person = write_gedcom(wiki, text).created_items["i1"]
        assert label_of(wiki, fragment) == page
        assert person != fragment

    def test_a_media_object_joins_an_unhashed_media_object(self, tmp_path):
        wiki = WritingWiki()
        older = gedcom_media_object(wiki, TITLE)
        written = write_gedcom(wiki)
        assert written.created_items["md1"] == older
        assert written.flags == []

    def test_resolve_never_resolves_an_artifact_kind_by_label(self):
        from yizkr.claims import LocalRef, QidRef
        from yizkr.pipeline.resolve import resolve_batch

        wiki = WritingWiki()
        batch = family_gedcom(wiki)
        given = batch.model_dump(mode="json")
        (claim,) = given["claims"]
        claim["subject"].update(label="Sebastian Löbig")
        claim["value"]["value"].update(label=TITLE)
        # a ref of an artifact class with no record, by its own kind
        photo = dict(
            claim,
            value={
                "type": "item",
                "value": {
                    "kind": "local",
                    "local_id": "ph1",
                    "label": TITLE,
                    "instance_of": "photograph",
                },
            },
        )
        given["claims"].append(photo)
        # the media object as a claim's subject, and as an item-valued
        # qualifier's value, by its record's kind
        media_object = dict(claim["value"]["value"])
        given["claims"].append(
            dict(
                claim,
                subject=media_object,
                value={"type": "item", "value": claim["subject"]},
                qualifiers=[
                    {
                        "property": "image",
                        "value": {"type": "item", "value": media_object},
                    }
                ],
            )
        )
        labels = {"Sebastian Löbig": "Q500", TITLE: "Q501"}

        resolved = resolve_batch(type(batch).model_validate(given), labels)

        first, second, third = resolved.claims
        assert first.subject == QidRef(qid="Q500")
        assert isinstance(first.value.value, LocalRef)
        assert isinstance(second.value.value, LocalRef)
        assert isinstance(third.subject, LocalRef)
        assert third.value.value == QidRef(qid="Q500")
        (qualifier,) = third.qualifiers
        assert isinstance(qualifier.value.value, LocalRef)

    def test_resolve_never_resolves_an_artifact_fragment_by_label(self):
        """A ref of the kind artifact fragment is on the artifact side, as
        in the writer (is_artifact_kind): an item holding its label, here
        a person's, is never its item."""
        from yizkr.claims import LocalRef, QidRef
        from yizkr.pipeline.resolve import resolve_batch

        wiki = WritingWiki()
        batch = family_gedcom(wiki)
        given = batch.model_dump(mode="json")
        (claim,) = given["claims"]
        claim["subject"].update(label="Sebastian Löbig")
        page = {
            "kind": "local",
            "local_id": "pg1",
            "label": "Sebastian Löbig",
            "instance_of": "artifact_fragment",
        }
        given["claims"] = [dict(claim, value={"type": "item", "value": page})]
        labels = {"Sebastian Löbig": "Q500"}

        resolved = resolve_batch(type(batch).model_validate(given), labels)

        (only,) = resolved.claims
        assert only.subject == QidRef(qid="Q500")
        assert isinstance(only.value.value, LocalRef)

    def test_resolve_never_resolves_a_ref_to_an_artifact(self, tmp_path):
        """The labels a batch is resolved against leave out every artifact
        and every part of one: a registered artifact, its fragment, an
        artifact with no hash and a media object's item. A person's
        remains."""
        from yizkr.pipeline.resolve import resolvable_labels

        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig", pages=1
        )
        unhashed(wiki, LETTER_TITLE, "File:Letter-to-sipkje-1952.png", LETTER)
        unhashed(
            wiki,
            "letter-to-sipkje-1952, copy",
            "File:Letter-to-sipkje-1952-copy.png",
            LETTER_RESCAN,
            kind=None,
        )
        wiki.item(SHOP, ("media_sha1", sha1(IMAGE), None))
        gedcom_media_object(wiki, TITLE)
        person = wiki.item(
            "Hildegard Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )

        held = [label_of(wiki, qid) for qid in wiki.entities]

        labels = resolvable_labels(wiki, naming(*held, "Gustav Löbig"))

        assert labels == {"Hildegard Löbig": person}

    def test_the_resolve_step_leaves_a_person_under_an_artifacts_label_local(
        self, tmp_path
    ):
        """`ingest run`'s resolve step resolves against those labels: a
        person whose name a registered artifact holds stays local, and the
        writer gives it an item of its own; another person resolves."""
        from types import SimpleNamespace

        from yizkr.claims import ClaimBatch, LocalRef, QidRef
        from yizkr.pipeline.orchestrate import Orchestrator

        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )
        given = family_gedcom(wiki, TWO_PEOPLE_GEDCOM).model_dump(mode="json")
        for claim, name in zip(
            given["claims"],
            ("Sebastian Löbig", "Hildegard Löbig"),
            strict=True,
        ):
            claim["subject"]["label"] = name
        batch = ClaimBatch.model_validate(given)
        hildegard = wiki.item(
            "Hildegard Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        doc = SimpleNamespace(equivalences_reviewed=tmp_path / "none.csv")
        step = SimpleNamespace(client=wiki, log=lambda *a, **k: None)

        resolved = Orchestrator._step_resolve(step, doc, batch)

        subjects = [claim.subject for claim in resolved.claims]
        assert isinstance(subjects[0], LocalRef)
        assert subjects[1] == QidRef(qid=hildegard)

    def test_cmd_resolve_leaves_a_person_under_an_artifacts_label_local(
        self, tmp_path, monkeypatch
    ):
        """`yizkr resolve` resolves against the same labels: a person whose
        name a registered artifact holds stays local in the batch it
        writes, and another person resolves."""
        from yizkr import cli
        from yizkr.claims import ClaimBatch, LocalRef, QidRef

        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )
        given = family_gedcom(wiki, TWO_PEOPLE_GEDCOM).model_dump(mode="json")
        for claim, name in zip(
            given["claims"],
            ("Sebastian Löbig", "Hildegard Löbig"),
            strict=True,
        ):
            claim["subject"]["label"] = name
        hildegard = wiki.item(
            "Hildegard Löbig",
            ("instance_of", WritingWiki.ITEMS["human"], None),
        )
        path = tmp_path / "batch.json"
        path.write_text(json.dumps(given))
        out = tmp_path / "out" / "resolved.json"
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        args = Namespace(batch=path, equivalences=None, out=out, env_file=None)

        assert cli.cmd_resolve(args) == 0

        resolved = ClaimBatch.model_validate_json(out.read_text())
        subjects = [claim.subject for claim in resolved.claims]
        assert isinstance(subjects[0], LocalRef)
        assert subjects[1] == QidRef(qid=hildegard)

    def test_a_sitter_named_by_another_persons_portrait_keeps_her_name(
        self, tmp_path
    ):
        """Sebastian's portrait is titled with Hildegard's name, and his
        claim names it before hers names her: the people are placed
        first, so she takes her name and the portrait its tagged label,
        unflagged, and a re-run finds each again."""
        wiki = WritingWiki()
        text = TWO_PEOPLE_GEDCOM.replace(TITLE, "Hildegard Löbig")

        written = write_gedcom(wiki, text)

        hildegard, portrait = (written.created_items[i] for i in ("i2", "md1"))
        assert label_of(wiki, hildegard) == "Hildegard Löbig"
        assert label_of(wiki, portrait) == "Hildegard Löbig [media object]"
        assert images(wiki, written.created_items["i1"]) == [portrait]
        assert written.flags == []
        assert write_gedcom(wiki, text).created_items == written.created_items
        assert_labels_unique(wiki)

    @pytest.mark.parametrize("kind", ["photograph", "artifact_fragment"])
    def test_an_artifact_kind_ref_named_first_leaves_the_person_its_name(
        self, tmp_path, kind
    ):
        """A hand-made batch whose first claim is about a photograph, or a
        part of an artifact, under Sebastian's name, its value Sebastian:
        he takes his name and the photograph its tagged label, two items,
        though the batch names the photograph first."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        given = family_gedcom(wiki).model_dump(mode="json")
        (claim,) = given["claims"]
        photograph = {
            "kind": "local",
            "local_id": "ph1",
            "label": "Sebastian Löbig",
            "instance_of": kind,
        }
        given["claims"] = [
            dict(
                claim,
                subject=photograph,
                value={"type": "item", "value": claim["subject"]},
            ),
            claim,
        ]

        written = BatchWriter(wiki).write_batch(
            ClaimBatch.model_validate(given)
        )

        person, photo = (written.created_items[i] for i in ("i1", "ph1"))
        assert person != photo
        assert label_of(wiki, person) == "Sebastian Löbig"
        assert label_of(wiki, photo) == "Sebastian Löbig [media object]"
        assert [
            alias["value"] for alias in wiki.entities[photo]["aliases"]["en"]
        ] == ["Sebastian Löbig"]
        assert_labels_unique(wiki)

    @pytest.mark.parametrize("word", ["Sebastian Felix ", "S "])
    def test_a_long_name_keeps_its_tag_within_the_term_limit(
        self, tmp_path, word
    ):
        """A person's name as long as Wikibase takes a label, which a
        registered artifact holds: the tagged label is cut to leave room
        for ENTITY_TAG, so it fits, and a re-run finds it. Given names of
        one-letter words leave the cut, which stops at a word break, no
        slack, so a cut sized for a longer tag would not fit."""
        from yizkr.pipeline.writer import ENTITY_TAG, TERM_LIMIT

        given = (word * TERM_LIMIT)[: TERM_LIMIT - len(" Löbig")]
        name = f"{given.strip()} Löbig"
        given = given.strip()
        assert len(name) > TERM_LIMIT - len(ENTITY_TAG)
        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", name
        )
        text = FAMILY_GEDCOM.replace("Sebastian /Löbig/", f"{given} /Löbig/")

        written = write_gedcom(wiki, text)

        person = written.created_items["i1"]
        label = label_of(wiki, person)
        assert len(label) <= TERM_LIMIT
        assert label.endswith(ENTITY_TAG)
        assert write_gedcom(wiki, text).created_items["i1"] == person
        assert_labels_unique(wiki)

    @pytest.mark.parametrize(
        ("kind", "tag"),
        [
            (None, " [not an artifact]"),
            ("military_unit", " [not an artifact]"),
            ("human", " [not an artifact]"),
            ("place", " [not an artifact]"),
            ("artifact", " [media object]"),
            ("photograph", " [media object]"),
            ("artifact_fragment", " [media object]"),
        ],
    )
    def test_one_tag_for_each_side(self, kind, tag):
        """Every entity not on the artifact side takes one tag, whatever
        its kind or none; an artifact kind, or artifact fragment, takes
        the media object's."""
        from yizkr.pipeline.writer import tagged_label

        assert tagged_label("Sebastian Löbig", kind) == f"Sebastian Löbig{tag}"

    @staticmethod
    def kindless(wiki):
        """The family GEDCOM's claim with its subject a ref with no kind
        and no record, named "Sebastian Löbig", as an extraction may give
        one."""
        from yizkr.claims import ClaimBatch

        given = family_gedcom(wiki).model_dump(mode="json")
        (claim,) = given["claims"]
        claim["subject"] = {
            "kind": "local",
            "local_id": "e1",
            "label": "Sebastian Löbig",
        }
        given["entities"] = [
            record for record in given["entities"] if record["id"] != "i1"
        ]
        return ClaimBatch.model_validate(given)

    @pytest.mark.parametrize("order", ["human-first", "kindless-first"])
    def test_a_kindless_ref_and_a_human_ref_meet_on_one_item(
        self, tmp_path, order
    ):
        """A registered artifact holds Sebastian's name: the GEDCOM's
        `human` ref and an extraction's ref with no kind take one item
        under the one tagged label, in either order, as they would meet
        under his name with no artifact holding it."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )
        writes = [
            lambda: write_gedcom(wiki).created_items["i1"],
            lambda: (
                BatchWriter(wiki)
                .write_batch(self.kindless(wiki))
                .created_items["e1"]
            ),
        ]
        if order == "kindless-first":
            writes.reverse()

        first, second = (write() for write in writes)

        assert first == second
        assert label_of(wiki, first) == "Sebastian Löbig [not an artifact]"
        assert_labels_unique(wiki)

    def test_a_kindless_ref_joins_a_person_written_before_the_artifact(
        self, tmp_path
    ):
        """Written before the artifact, the person keeps his name, and a
        ref with no kind written after it joins his item there."""
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        person = write_gedcom(wiki).created_items["i1"]
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )

        written = BatchWriter(wiki).write_batch(self.kindless(wiki))

        assert written.created_items["e1"] == person
        assert label_of(wiki, person) == "Sebastian Löbig"


class TestTheReviewsProbes:
    """Each side of the label rules a probe of the PR review pinned
    (#444): an entity of the kind artifact fragment, the content tag that
    is an artifact's own, an artifact of a subclass with no hash, the
    labels the batch itself plans, the flags against two media objects,
    the first ref's label, and two refused entities in one claim."""

    def test_an_artifact_fragment_entity_finds_its_item_on_every_write(
        self, tmp_path
    ):
        """An entity of the kind artifact fragment is on the artifact side:
        its item, an artifact's part, is found again under its label on a
        second and a third write, never a second item, never refused."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "md1":
                record.update(
                    label=f"{LETTER_TITLE}, page 1", kind="artifact_fragment"
                )
        batch = ClaimBatch.model_validate(given)

        runs = [BatchWriter(wiki).write_batch(batch) for _ in range(3)]

        fragment = runs[0].created_items["md1"]
        assert [run.created_items["md1"] for run in runs] == [fragment] * 3
        assert [run.refused for run in runs] == [[], [], []]
        assert label_of(wiki, fragment) == f"{LETTER_TITLE}, page 1"
        assert strings(wiki, fragment, "instance_of") == [
            ITEMS["artifact_fragment"]
        ]
        assert images(wiki, runs[0].created_items["i1"]) == [fragment]
        assert_labels_unique(wiki)

    def test_a_dated_title_is_not_another_artifacts_content_tag(
        self, tmp_path
    ):
        """The portrait is registered as "Atelierbild Löbig", and another
        photograph under "Atelierbild Löbig [18950312]", a date and no
        content tag of its own: a media object titled "Atelierbild Löbig"
        flags the portrait alone, as the registrar flags nothing when the
        dated photograph is registered again."""
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        portrait = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Atelierbild Löbig"
        ).artifact_qid
        dated = scan(tmp_path, RESCAN, name="atelier-1895.png", folder="s2")
        registrar.register_artifact(
            dated, "photograph", "Atelierbild Löbig [18950312]"
        )

        written = write_gedcom(
            wiki, FAMILY_GEDCOM.replace(TITLE, "Atelierbild Löbig")
        )

        media_object = written.created_items["md1"]
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, portrait, sha1(PORTRAIT), (media_object,))
        ]
        again = Registrar(wiki)
        again.register_artifact(
            dated, "photograph", "Atelierbild Löbig [18950312]"
        )
        assert again.flags == []

    def test_a_person_never_joins_an_unhashed_item_of_an_artifact_class(
        self, tmp_path
    ):
        """An item with no hash and no file that is an `instance of
        photograph` alone, a subclass of artifact, is an artifact item: the
        person under its label takes the tagged label."""
        wiki = WritingWiki()
        photo = wiki.item(
            "Sebastian Löbig", ("instance_of", ITEMS["photograph"], None)
        )

        person = write_gedcom(wiki).created_items["i1"]

        assert person != photo
        assert label_of(wiki, person) == "Sebastian Löbig [not an artifact]"

    def test_a_media_object_titled_as_the_persons_tagged_label(self, tmp_path):
        """A scan holds "Sebastian Löbig" and the media object is titled
        literally "Sebastian Löbig [not an artifact]": the person plans
        that label first, so the media object takes its own tagged label,
        two items."""
        wiki = WritingWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", "Sebastian Löbig"
        )
        text = FAMILY_GEDCOM.replace(
            TITLE, "Sebastian Löbig [not an artifact]"
        )

        written = write_gedcom(wiki, text)

        person, media_object = (
            written.created_items[i] for i in ("i1", "md1")
        )
        assert person != media_object
        assert label_of(wiki, person) == "Sebastian Löbig [not an artifact]"
        assert label_of(wiki, media_object) == (
            "Sebastian Löbig [not an artifact] [media object]"
        )
        assert_labels_unique(wiki)

    def test_one_rescan_is_flagged_against_two_media_objects(self, tmp_path):
        """A scan under the title and its re-scan under its content-tagged
        form; one media object titled with the title and another with the
        re-scan's label: the re-scan is flagged against each."""
        wiki = WritingWiki()
        registrar = Registrar(wiki)
        first = registrar.register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        ).artifact_qid
        second = registrar.register_artifact(
            scan(tmp_path, RESCAN, name="portrait-rescan.png", folder="s2"),
            "photograph",
            TITLE,
        ).artifact_qid
        rescan_label = label_of(wiki, second)
        assert rescan_label == f"{TITLE} [{sha1(RESCAN)[:8]}]"
        text = TWO_PEOPLE_GEDCOM.replace(
            "Wedding portrait, 1905", rescan_label
        )

        written = write_gedcom(wiki, text)

        md1, md2 = (written.created_items[i] for i in ("md1", "md2"))
        assert md1 != md2
        assert flag_tuples(written.flags) == [
            (UNHASHED_TITLE, first, sha1(PORTRAIT), (md1,)),
            (UNHASHED_TITLE, second, sha1(RESCAN), (md1,)),
            (UNHASHED_TITLE, second, sha1(RESCAN), (md2,)),
        ]

    def test_an_entity_with_no_record_takes_its_first_refs_label(
        self, tmp_path
    ):
        """An entity with no record named "Sebastian Löbig" by its first
        ref and "Sebastian Loebig" by its second: its item takes the
        first."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        given = family_gedcom(wiki).model_dump(mode="json")
        (claim,) = given["claims"]
        given["entities"] = [
            record for record in given["entities"] if record["id"] != "i1"
        ]
        given["claims"] = [
            dict(
                claim,
                subject=dict(
                    claim["subject"], label=name, instance_of="human"
                ),
            )
            for name in ("Sebastian Löbig", "Sebastian Loebig")
        ]

        written = BatchWriter(wiki).write_batch(
            ClaimBatch.model_validate(given)
        )

        assert label_of(wiki, written.created_items["i1"]) == "Sebastian Löbig"

    def test_two_refused_entities_in_one_claim(self, tmp_path):
        """Registered artifacts hold both labels of Sebastian and both of
        his media object, and a second claim names Sebastian and a place
        of a kind the vocabulary lacks: each refused entity is warned of
        once, naming the claims that name it, each claim is refused once,
        and the place's kind is not warned of, as no claim naming it is
        written."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.review import claim_id
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        for label, data in (
            ("Sebastian Löbig", PORTRAIT),
            ("Sebastian Löbig [not an artifact]", RESCAN),
            (TITLE, LETTER),
            (f"{TITLE} [media object]", LETTER_RESCAN),
        ):
            wiki.item(
                label,
                ("instance_of", ITEMS["photograph"], None),
                ("media_sha1", sha1(data), None),
            )
        given = family_gedcom(wiki).model_dump(mode="json")
        (claim,) = given["claims"]
        place = {
            "kind": "local",
            "local_id": "pl1",
            "label": "Flörburg",
            "instance_of": "lighthouse",
        }
        given["claims"].append(
            dict(claim, value={"type": "item", "value": place})
        )
        batch = ClaimBatch.model_validate(given)
        first, second = (claim_id(c) for c in batch.claims)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [first, second]
        refusals = [w for w in written.warnings if "is refused" in w]
        assert len(refusals) == len(set(refusals)) == 2
        (sebastian,) = [w for w in refusals if "'i1'" in w]
        (media_object,) = [w for w in refusals if "'md1'" in w]
        assert f"({first}, {second})" in sebastian
        assert f"({first})" in media_object
        assert not [w for w in written.warnings if "lighthouse" in w]
        assert written.created_items == {}

    def test_a_blocked_entitys_warning_names_only_claims_it_would_write(
        self, tmp_path
    ):
        """Registered artifacts hold both labels of Sebastian, and a
        second claim about him has a value with a line break, which the
        writer refuses for its own text: his warning names only the claim
        the plan writes, and each claim is refused once."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.review import claim_id
        from yizkr.pipeline.writer import BatchWriter

        wiki = WritingWiki()
        for label, data in (
            ("Sebastian Löbig", PORTRAIT),
            ("Sebastian Löbig [not an artifact]", RESCAN),
        ):
            wiki.item(
                label,
                ("instance_of", ITEMS["photograph"], None),
                ("media_sha1", sha1(data), None),
            )
        given = family_gedcom(wiki).model_dump(mode="json")
        (claim,) = given["claims"]
        given["claims"].insert(
            0,
            dict(
                claim,
                value={"type": "string", "value": "Sebastian\nLöbig"},
            ),
        )
        batch = ClaimBatch.model_validate(given)
        broken, named = (claim_id(c) for c in batch.claims)
        assert broken != named

        written = BatchWriter(wiki).write_batch(batch)

        assert sorted(written.refused) == sorted([broken, named])
        (sebastian,) = [
            w for w in written.warnings if "'i1'" in w and "is refused" in w
        ]
        assert f"({named})" in sebastian
        assert broken not in sebastian


class TestTheWriteStepRaisesTheFlag:
    def test_the_flag_is_an_alert_of_the_gedcoms_write(
        self, tmp_path, monkeypatch
    ):
        """Raised as registration raises it: on the GEDCOM's write, the
        artifact as its item, the media object's item against it, and the
        artifact's own SHA-1 as its medium (docs/ANOMALIES.md)."""
        from tests.unit.test_write_step import WriteStep
        from yizkr.pipeline import rundir

        monkeypatch.setattr(
            rundir, "_now", lambda: "2026-10-02T09:00:00+00:00"
        )
        wiki = WritingWiki()
        artifact = Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", TITLE
        )
        batch = family_gedcom(wiki)
        step = WriteStep(tmp_path, wiki, "example-family.ged")
        step.doc.source.parent.mkdir(parents=True, exist_ok=True)
        step.doc.source.write_text(FAMILY_GEDCOM)
        step.review(batch, decide=lambda row: "approve")

        summary = step.write(batch)

        media_object = summary["created_items"]["md1"]
        (alert,) = step.orchestrator.rundir.alerts()
        assert alert["check"] == UNHASHED_TITLE
        assert alert["stage"] == "write"
        assert alert["subject"] == step.doc.sha1[:12]
        assert alert["kind"] == step.doc.kind
        assert alert["artifact"] == artifact.artifact_qid
        assert alert["others"] == [media_object]
        assert alert["medium"] == sha1(PORTRAIT)[:12]
        assert any(
            line.strip()
            == f"flagged {UNHASHED_TITLE}: item {artifact.artifact_qid}, "
            f"against {media_object}"
            for line in step.lines
        )

    def test_a_refused_media_objects_flag_is_an_alert_of_the_write(
        self, tmp_path, monkeypatch
    ):
        """A media object that may join neither the item under its label
        nor the one under its tagged label is refused before the first
        write, the rest of the export written, and the scan under its
        label raised against the item under its tagged label."""
        from tests.unit.test_write_step import WriteStep
        from yizkr.pipeline import rundir

        monkeypatch.setattr(
            rundir, "_now", lambda: "2026-10-02T09:00:00+00:00"
        )
        wiki = WritingWiki()
        plain, tagged = (
            TestAGedcomsMediaObjectAndARegisteredArtifact().both_labels_registered(
                wiki
            )
        )
        batch = family_gedcom(wiki, TWO_PEOPLE_GEDCOM)
        step = WriteStep(tmp_path, wiki, "example-family.ged")
        step.doc.source.parent.mkdir(parents=True, exist_ok=True)
        step.doc.source.write_text(TWO_PEOPLE_GEDCOM)
        step.review(batch, decide=lambda row: "approve")

        summary = step.write(batch)

        assert len(summary["refused"]) == 1
        assert summary["written"] == 1
        assert "md1" not in summary["created_items"]
        (alert,) = step.orchestrator.rundir.alerts()
        assert alert["check"] == UNHASHED_TITLE
        assert alert["artifact"] == plain
        assert alert["others"] == [tagged]
        assert alert["medium"] == sha1(PORTRAIT)[:12]

    def test_cmd_write_prints_the_flags(self, tmp_path, monkeypatch, capsys):
        from yizkr import cli

        wiki = WritingWiki()
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        path = tmp_path / "batch.json"
        path.write_text(family_gedcom(wiki).model_dump_json())
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        args = Namespace(batch=path, dry_run=False, env_file=None)

        assert cli.cmd_write(args) == 0

        printed = json.loads(capsys.readouterr().out)
        (flag,) = printed["flags"]
        assert flag["kind"] == UNHASHED_TITLE
        assert flag["artifact"] == artifact
        assert flag["others"] == [printed["created_items"]["md1"]]


# -- what resolve and a write read of the instance ----------------------------


class CountingWiki(WritingWiki):
    """The in-memory wiki counting what is read of it: each read of every
    label (`label_index`, `find_item_by_label`, `snapshot` and
    `list_entity_ids` each read them all, and `vocabulary` does, as
    WikiClient.vocabulary does, when it is not yet known and no
    `label_index` has been read since `uncache`), each read of every
    entity (`iter_entities`, `string_statement_index`), the entities
    whose statements are fetched and each fetch (`fetch_claims`), and the
    items read one by one (`wbi.item.get`)."""

    def __init__(self) -> None:
        super().__init__()
        self.vocabulary_known = False
        self.label_reads: list[str] = []
        self.whole_reads: list[str] = []
        self.claims_of: list[str] = []
        self.fetches = 0
        self.items_read: list[str] = []
        get = self.wbi.item.get

        def counted(qid):
            self.items_read.append(qid)
            return get(qid)

        self.wbi.item.get = counted

    def forget(self) -> None:
        """Count from here on."""
        for reads in (
            self.label_reads,
            self.whole_reads,
            self.claims_of,
            self.items_read,
        ):
            reads.clear()
        self.fetches = 0

    def uncache(self) -> None:
        """As a fresh WikiClient: its vocabulary not yet read."""
        self.vocabulary_known = False

    def vocabulary(self):
        if not self.vocabulary_known:
            self.label_reads.append("vocabulary")
            self.vocabulary_known = True
        return super().vocabulary()

    def label_index(self):
        self.label_reads.append("label_index")
        self.vocabulary_known = True
        return super().label_index()

    def find_item_by_label(self, label):
        self.label_reads.append("find_item_by_label")
        return super().find_item_by_label(label)

    def snapshot(self):
        self.label_reads.append("snapshot")
        return {
            qid: {"label": label_of(self, qid), "datatype": None}
            for qid in self.entities
        }

    def list_entity_ids(self):
        self.label_reads.append("list_entity_ids")
        return sorted(self.entities)

    def iter_entities(self, props):
        self.whole_reads.append("iter_entities")
        return super().iter_entities(props)

    def string_statement_index(self, pid):
        self.whole_reads.append("string_statement_index")
        return super().string_statement_index(pid)

    def fetch_claims(self, ids):
        self.fetches += 1
        self.claims_of.extend(ids)
        return super().fetch_claims(ids)


class TestWhatResolveAndAWriteRead:
    """Resolve reads the instance's labels once a batch, and the
    statements of only the items its refs' labels name; a write reads the
    labels once, and the statements of only the items it finds. Neither
    reads every entity's statements, however many the instance holds
    (ADR-0010, "What is still matched by label")."""

    UNRELATED = 200

    def instance(self, tmp_path):
        """The wiki with UNRELATED people the batch never names, the scan
        registered under the media object's title, and the GEDCOM export
        registered: the wiki, the people, the scan's item and the
        export's."""
        wiki = CountingWiki()
        people = [
            wiki.item(
                f"Unrelated person {n:04d}",
                ("instance_of", WritingWiki.ITEMS["human"], None),
            )
            for n in range(self.UNRELATED)
        ]
        artifact = (
            Registrar(wiki)
            .register_artifact(scan(tmp_path, PORTRAIT), "photograph", TITLE)
            .artifact_qid
        )
        batch = family_gedcom(wiki)
        exported = wiki.string_statement_index(P["media_sha1"])[
            batch.claims[0].source.artifact.sha1
        ]
        wiki.forget()
        return wiki, people, artifact, exported

    def test_resolve_reads_the_statements_of_the_items_named(self, tmp_path):
        from yizkr.pipeline.resolve import resolvable_labels

        wiki, people, artifact, _ = self.instance(tmp_path)

        labels = resolvable_labels(
            wiki, naming("Unrelated person 0007", TITLE, "Gustav Löbig")
        )

        assert labels == {"Unrelated person 0007": people[7]}
        assert wiki.label_reads == ["label_index"]
        assert sorted(wiki.claims_of) == sorted([people[7], artifact])
        assert wiki.fetches == 1
        assert wiki.whole_reads == []
        assert wiki.items_read == []

    def test_a_fresh_client_resolves_from_one_read_of_the_labels(
        self, tmp_path
    ):
        """A client that has not read the vocabulary yet, as the CLI's
        is, reads the labels once for the match and the vocabulary."""
        from yizkr.pipeline.resolve import resolvable_labels

        wiki, people, _, _ = self.instance(tmp_path)
        wiki.uncache()

        labels = resolvable_labels(wiki, naming("Unrelated person 0007"))
        assert labels == {"Unrelated person 0007": people[7]}
        assert wiki.label_reads == ["label_index"]

        wiki.uncache()
        wiki.forget()
        assert resolvable_labels(wiki, naming("Gustav Löbig")) == {}
        assert wiki.label_reads == ["label_index"]

    def test_resolve_reads_no_statements_for_a_media_object(self, tmp_path):
        """A ref of an artifact kind under the scan's title, as a GEDCOM
        media object is, is never resolved by its label, so the scan's
        statements are not read."""
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline.resolve import resolvable_labels

        wiki, *_ = self.instance(tmp_path)
        given = naming(TITLE).model_dump(mode="json")
        given["claims"][0]["subject"]["instance_of"] = "photograph"
        batch = ClaimBatch.model_validate(given)
        wiki.forget()

        assert resolvable_labels(wiki, batch) == {}
        assert wiki.label_reads == ["label_index"]
        assert wiki.fetches == 0
        assert wiki.whole_reads == []

    def test_resolve_reads_no_statements_when_no_label_is_held(self, tmp_path):
        from yizkr.pipeline.resolve import resolvable_labels

        wiki, *_ = self.instance(tmp_path)

        assert resolvable_labels(wiki, naming("Gustav Löbig")) == {}
        assert wiki.label_reads == ["label_index"]
        assert wiki.fetches == 0
        assert wiki.whole_reads == []

    def test_the_resolve_step_reads_as_resolve_does(self, tmp_path):
        from types import SimpleNamespace

        from yizkr.pipeline.orchestrate import Orchestrator

        wiki, people, artifact, _ = self.instance(tmp_path)
        doc = SimpleNamespace(equivalences_reviewed=tmp_path / "none.csv")
        step = SimpleNamespace(client=wiki, log=lambda *a, **k: None)

        Orchestrator._step_resolve(
            step, doc, naming("Unrelated person 0007", TITLE)
        )

        assert wiki.label_reads == ["label_index"]
        assert sorted(wiki.claims_of) == sorted([people[7], artifact])
        assert wiki.whole_reads == []

    def test_a_write_reads_the_labels_once(self, tmp_path):
        """The GEDCOM written after the scan: its media object is placed
        under its tagged label and flagged against the scan, its person
        made, and its run item made, all from one read of the labels."""
        from yizkr.pipeline.writer import BatchWriter

        wiki, people, artifact, exported = self.instance(tmp_path)
        batch = family_gedcom(wiki)
        sha = batch.claims[0].source.artifact.sha1
        wiki.forget()

        written = BatchWriter(wiki, artifact_qids={sha: exported}).write_batch(
            batch
        )

        assert [flag.artifact for flag in written.flags] == [artifact]
        assert wiki.label_reads == ["label_index"]
        assert wiki.whole_reads == []
        assert wiki.claims_of == []
        assert not set(wiki.items_read) & set(people)

    def test_a_re_write_reads_the_labels_once_and_finds_its_items(
        self, tmp_path
    ):
        from yizkr.pipeline.writer import BatchWriter

        wiki, people, _, exported = self.instance(tmp_path)
        batch = family_gedcom(wiki)
        sha = batch.claims[0].source.artifact.sha1
        first = BatchWriter(wiki, artifact_qids={sha: exported}).write_batch(
            batch
        )
        wiki.forget()

        again = BatchWriter(wiki, artifact_qids={sha: exported}).write_batch(
            batch
        )

        assert again.created_items == first.created_items
        assert again.run_qid == first.run_qid
        assert wiki.label_reads == ["label_index"]
        assert wiki.whole_reads == []
        assert not set(wiki.items_read) & set(people)

    def test_two_refs_under_one_label_meet_on_the_item_the_write_makes(
        self, tmp_path
    ):
        """The labels are read once, before the write makes anything: a
        second ref under a label the write has made an item under finds
        that item, as it would under the label read afresh."""
        from yizkr.pipeline.writer import BatchWriter

        wiki, _, _, exported = self.instance(tmp_path)
        batch = naming("Gustav Löbig", "Gustav Löbig")
        sha = batch.claims[0].source.artifact.sha1

        written = BatchWriter(wiki, artifact_qids={sha: exported}).write_batch(
            batch
        )

        assert written.created_items["e0"] == written.created_items["e1"]
        assert_labels_unique(wiki)
        assert wiki.label_reads == ["label_index"]

    def test_each_write_reads_the_labels_afresh(self, tmp_path):
        """A writer that writes two batches reads the labels once for
        each: an item made between them under a label the second names is
        that entity's item."""
        from yizkr.pipeline.writer import BatchWriter

        wiki, _, _, exported = self.instance(tmp_path)
        batch = family_gedcom(wiki)
        sha = batch.claims[0].source.artifact.sha1
        writer = BatchWriter(wiki, artifact_qids={sha: exported})
        writer.write_batch(batch)
        gustav = wiki.item(
            "Gustav Löbig", ("instance_of", WritingWiki.ITEMS["human"], None)
        )
        wiki.forget()

        written = writer.write_batch(naming("Gustav Löbig"))

        assert written.created_items["e0"] == gustav
        assert wiki.label_reads == ["label_index"]

    def test_resolve_reads_the_labels_of_values_and_qualifiers(self, tmp_path):
        """Sebastian's claim names Gustav as its value and Hildegard in a
        qualifier, each under the label of a person the instance holds:
        both labels are wanted, the statements of only their items are
        read, and both refs resolve to those items."""
        from yizkr.claims import ClaimBatch, LocalRef, QidRef
        from yizkr.pipeline.resolve import resolvable_labels, resolve_batch

        wiki, *_ = self.instance(tmp_path)
        gustav, hildegard = (
            wiki.item(name, ("instance_of", WritingWiki.ITEMS["human"], None))
            for name in ("Gustav Löbig", "Hildegard Löbig")
        )
        given = naming("Sebastian Löbig").model_dump(mode="json")
        (claim,) = given["claims"]
        claim["property"] = "relative"
        claim["value"] = {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": "g",
                "label": "Gustav Löbig",
            },
        }
        claim["qualifiers"] = [
            {
                "property": "participant",
                "value": {
                    "type": "item",
                    "value": {
                        "kind": "local",
                        "local_id": "h",
                        "label": "Hildegard Löbig",
                    },
                },
            }
        ]
        batch = ClaimBatch.model_validate(given)
        wiki.forget()

        labels = resolvable_labels(wiki, batch)

        assert labels == {"Gustav Löbig": gustav, "Hildegard Löbig": hildegard}
        assert sorted(wiki.claims_of) == sorted([gustav, hildegard])
        assert wiki.fetches == 1
        assert wiki.label_reads == ["label_index"]
        (resolved,) = resolve_batch(batch, labels).claims
        assert isinstance(resolved.subject, LocalRef)
        assert resolved.value.value == QidRef(qid=gustav)
        (qualifier,) = resolved.qualifiers
        assert qualifier.value.value == QidRef(qid=hildegard)

    def test_a_write_scans_each_label_once_for_content_tags(
        self, tmp_path, monkeypatch
    ):
        """Two media objects, each under the title of a registered scan,
        are flagged from one index of the labels the write read: each
        label is scanned for content tags once, and each lookup scans
        only the labels it finds, however many the instance holds."""
        from yizkr.pipeline import writer as writer_module
        from yizkr.pipeline.writer import BatchWriter

        class Counting:
            def __init__(self, pattern):
                self.pattern = pattern
                self.scans = 0

            def finditer(self, text):
                self.scans += 1
                return self.pattern.finditer(text)

            def __getattr__(self, name):
                return getattr(self.pattern, name)

        wiki, _, artifact, _ = self.instance(tmp_path)
        letter = (
            Registrar(wiki)
            .register_artifact(
                scan(tmp_path, LETTER, name="letter.png"),
                "photograph",
                "Wedding portrait, 1905",
            )
            .artifact_qid
        )
        wiki.item(
            "two-people-export.ged",
            ("instance_of", ITEMS["genealogical_report"], None),
            ("media_sha1", sha1(TWO_PEOPLE_GEDCOM.encode()), None),
        )
        batch = family_gedcom(wiki, TWO_PEOPLE_GEDCOM)
        read = len(wiki.entities)
        counting = Counting(writer_module._CONTENT_TAG)
        monkeypatch.setattr(writer_module, "_CONTENT_TAG", counting)

        written = BatchWriter(wiki).write_batch(batch)

        assert sorted(flag.artifact for flag in written.flags) == sorted(
            [artifact, letter]
        )
        assert counting.scans == read + 2

    def test_the_label_index_finds_a_title_as_it_is_or_content_tagged(self):
        """_FoundLabels.titled: each label that is the title as it is, or
        with one content tag taken out of it wherever it is, in the order
        of their items by number."""
        from yizkr.pipeline.writer import _FoundLabels

        held = {
            "Studio portrait": "Q10",
            "Studio portrait [0123abcd]": "Q9",
            "Studio portrait [0123abcd], page 1": "Q11",
            "Studio portrait [0123abcd] [89abcdef]": "Q2",
            "Studio portrait, 1895": "Q3",
            "Studio portrait [0123abcz]": "Q4",
        }

        class Client:
            reads = 0

            def label_index(self):
                Client.reads += 1
                return {}, dict(held)

        labels = _FoundLabels(Client())

        assert labels.titled("Studio portrait") == [
            ("Studio portrait [0123abcd]", "Q9"),
            ("Studio portrait", "Q10"),
        ]
        assert labels.titled("Studio portrait, page 1") == [
            ("Studio portrait [0123abcd], page 1", "Q11")
        ]
        assert labels.titled("Studio portrait [0123abcd]") == [
            ("Studio portrait [0123abcd] [89abcdef]", "Q2"),
            ("Studio portrait [0123abcd]", "Q9"),
        ]
        assert labels.titled("Studio portrait [89abcdef]") == [
            ("Studio portrait [0123abcd] [89abcdef]", "Q2")
        ]
        assert labels.titled("Studio portraits") == []
        labels.made("Studio portrait [fedcba98]", "Q12")
        assert labels.titled("Studio portrait") == [
            ("Studio portrait [0123abcd]", "Q9"),
            ("Studio portrait", "Q10"),
        ]
        assert Client.reads == 1


class TestTheClientsBoundedReads:
    """WikiClient.fetch_claims asks for the entities named alone, 50 a
    request, and WikiClient.vocabulary answers from the labels the client
    has just read whole rather than reading them again."""

    @staticmethod
    def client():
        from yizkr.core.config import Config
        from yizkr.core.wiki import WikiClient

        return WikiClient(
            Config(
                wikibase_url="http://wiki.invalid",
                concept_uri="http://wiki.invalid/entity/",
                admin_name="",
                admin_pass="",
                wdqs_url=None,
                sparql_url=None,
                env_file=None,
            )
        )

    def test_fetch_claims_asks_for_the_ids_named_fifty_a_request(self):
        client = self.client()
        asked: list[dict] = []

        def api_get(params):
            asked.append(params)
            return {
                "entities": {
                    qid: {"id": qid, "claims": {"P1": [qid]}}
                    for qid in params["ids"].split("|")
                }
            }

        client.api_get = api_get
        ids = [f"Q{n}" for n in range(1, 121)]

        claims = client.fetch_claims(ids)

        assert [len(params["ids"].split("|")) for params in asked] == [
            50,
            50,
            20,
        ]
        assert {params["props"] for params in asked} == {"claims"}
        assert {params["action"] for params in asked} == {"wbgetentities"}
        assert claims == {qid: {"P1": [qid]} for qid in ids}
        asked.clear()
        assert client.fetch_claims([]) == {}
        assert asked == []

    @staticmethod
    def manifest_snapshot() -> dict[str, dict]:
        import yaml

        from yizkr.model.schema import DEFAULT_MANIFEST, read_manifest_data

        raw = yaml.safe_load(read_manifest_data(DEFAULT_MANIFEST))
        snapshot = {
            f"P{n}": {"label": spec["label"], "datatype": None}
            for n, spec in enumerate(raw["properties"].values(), 1)
        }
        snapshot.update(
            {
                f"Q{n}": {"label": spec["label"], "datatype": None}
                for n, spec in enumerate(raw["items"].values(), 1)
            }
        )
        return snapshot

    def test_the_vocabulary_reuses_the_labels_just_read(self):
        client = self.client()
        whole = self.manifest_snapshot()
        reads: list[str] = []

        def snapshot():
            reads.append("snapshot")
            return dict(whole)

        client.snapshot = snapshot

        _, items = client.label_index()
        properties, vocabulary_items = client.vocabulary()

        assert reads == ["snapshot"]
        assert set(vocabulary_items.values()) <= set(items.values())
        assert properties

    def test_the_vocabulary_reads_afresh_when_the_labels_lack_it(self):
        client = self.client()
        whole = self.manifest_snapshot()
        first = {"Q1": whole["Q1"]}
        given = [first, whole]
        reads: list[str] = []

        def snapshot():
            reads.append("snapshot")
            return dict(given.pop(0))

        client.snapshot = snapshot

        client.label_index()
        client.vocabulary()

        assert reads == ["snapshot", "snapshot"]

    def test_fetch_claims_gives_an_id_the_instance_lacks_none(self):
        client = self.client()

        def api_get(params):
            return {
                "entities": {
                    "Q1": {"id": "Q1", "claims": {"P1": ["Q1"]}},
                    "Q99": {"id": "Q99", "missing": ""},
                }
            }

        client.api_get = api_get

        assert client.fetch_claims(["Q1", "Q99"]) == {
            "Q1": {"P1": ["Q1"]},
            "Q99": {},
        }

    def test_a_vocabulary_answered_from_the_labels_read_is_kept(self):
        """Once answered from the labels just read, the vocabulary is
        kept: labels read later that lack it do not make it read again."""
        client = self.client()
        whole = self.manifest_snapshot()
        given = [whole, {"Q1": whole["Q1"]}]
        reads: list[str] = []

        def snapshot():
            reads.append("snapshot")
            return dict(given.pop(0))

        client.snapshot = snapshot

        client.label_index()
        first = client.vocabulary()
        client.label_index()

        assert client.vocabulary() is first
        assert reads == ["snapshot", "snapshot"]

    def test_a_forgotten_vocabulary_is_read_afresh(self):
        """forget_vocabulary forgets the vocabulary and the labels read,
        so the next vocabulary reads the instance again."""
        client = self.client()
        whole = self.manifest_snapshot()
        reads: list[str] = []

        def snapshot():
            reads.append("snapshot")
            return dict(whole)

        client.snapshot = snapshot

        client.label_index()
        client.vocabulary()
        client.forget_vocabulary()
        client.vocabulary()

        assert reads == ["snapshot", "snapshot"]

    def test_the_vocabulary_reads_the_labels_when_none_were_read(self):
        client = self.client()
        reads: list[str] = []
        whole = self.manifest_snapshot()

        def snapshot():
            reads.append("snapshot")
            return dict(whole)

        client.snapshot = snapshot

        client.vocabulary()
        client.vocabulary()

        assert reads == ["snapshot"]
