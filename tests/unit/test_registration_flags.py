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
and unhashed-title only for a scan registered under exactly a GEDCOM
media object's title.

The in-memory wiki, its fictional media and the helpers come from
tests/unit/test_registrar.py. The flow is verified live too
(tests/integration/test_live_registration_flags.py).
"""

from __future__ import annotations

import copy
import json
import re
from argparse import Namespace

import pytest

from tests.unit.test_registrar import (
    IMAGE,
    ITEMS,
    LONG,
    OTHER_IMAGE,
    PORTRAIT,
    RESCAN,
    TITLE,
    FakeWiki,
    P,
    StoringWiki,
    TestStepRegister,
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
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
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
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
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
            (SHARED_FILE, new, sha1(PORTRAIT), (old,)),
            (UNHASHED_TITLE, new, sha1(PORTRAIT), (old,)),
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
            for kind in (SHARED_FILE, UNHASHED_TITLE)
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
            (SHARED_FILE, medium, sha1(IMAGE), (old,)),
            (UNHASHED_TITLE, medium, sha1(IMAGE), (old,)),
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
    orchestrator = staticmethod(TestStepRegister.orchestrator)

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
            SHARED_FILE,
            UNHASHED_TITLE,
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
            SHARED_FILE,
            UNHASHED_TITLE,
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
            (SHARED_FILE, qid, [old]),
            (UNHASHED_TITLE, qid, [old]),
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
            SHARED_FILE,
            UNHASHED_TITLE,
        ]
        assert all(alert["others"] == [old] for alert in alerts)


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
    assert client.asked[1]["aicontinue"] == "Portrait-b.png"


def test_a_listing_without_files_is_none():
    assert (
        registrar_module._files_with_bytes(PagedListing([{}]), "0" * 40) == []
    )


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
    orchestrator, _ = TestStepRegister.orchestrator(
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
        pdf.sha1[:12]: [(SHARED_FILE, [old_pdf]), (UNHASHED_TITLE, [old_pdf])],
        excerpt.sha1[:12]: [(UNHASHED_TITLE, [old_image])],
    }
    assert pdf.sha1 != excerpt.sha1
    assert {
        alert["artifact"]
        for alert in alerts
        if alert["medium"] == excerpt.sha1[:12]
    } == {qid}
