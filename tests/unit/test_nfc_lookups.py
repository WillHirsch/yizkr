"""Offline tests: the label lookups outside the writer compare text in the
form the instance keeps a term in, as the writer does (stored_term,
_term_text; #370); the resolve step looks up no label (#109). Each is
checked over a label given in NFD, as a source transcribed on another
system may give it.

The universe's names with a precomposed letter serve: Gustav Löbig, Ida
Sörne, Flörburg."""

from __future__ import annotations

import unicodedata

import pytest

from tests.unit.test_places import FINGERPRINT, RunItemWiki
from yizkr.pipeline.names import normalize_aliases
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.writer import TERM_LIMIT, _term_text


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


GUSTAV = nfc("Gustav Löbig")
# a combining mark Python's Unicode tables do not know (Unicode 15.0)
MARK = "\U0001e08f"


def test_the_forms_differ():
    """The NFD label is another string than the one the instance keeps."""
    assert nfd(GUSTAV) != GUSTAV
    assert _term_text(nfd(GUSTAV)) == GUSTAV


class TestPlacesRunItem:
    def test_a_model_identifier_with_a_line_break_is_kept_on_one_line(self):
        """The instance makes a line break in a label a space, so the
        label is sent and looked for so, and a second pass finds it."""
        wiki = RunItemWiki()

        qid = ensure_places_run_item(wiki, FINGERPRINT, "Löbig-model\nv2")

        assert wiki.entities[qid]["labels"]["en"]["value"] == (
            f"place-hierarchy run Löbig-model v2 {FINGERPRINT}"
        )
        assert (
            ensure_places_run_item(wiki, FINGERPRINT, "Löbig-model\nv2") == qid
        )

    def test_a_long_model_identifier_is_cut_as_the_instance_holds_it(self):
        model = "Flörburg-" * 40
        wiki = RunItemWiki()

        qid = ensure_places_run_item(wiki, FINGERPRINT, model)

        stored = wiki.entities[qid]["labels"]["en"]["value"]
        assert stored == _term_text(
            f"place-hierarchy run {model} {FINGERPRINT}"
        )
        assert len(stored) <= TERM_LIMIT
        assert ensure_places_run_item(wiki, FINGERPRINT, model) == qid
        assert list(wiki.entities) == [qid]

    def test_an_unknown_character_past_the_cut_is_refused_unread(self):
        """The whole label is checked, not its cut: the instance may keep
        the mark in another form, so nothing is read or written."""
        wiki = RunItemWiki()
        looked = []
        wiki.find_item_by_label = looked.append

        with pytest.raises(ValueError):
            ensure_places_run_item(wiki, FINGERPRINT, "Flörburg-" * 40 + MARK)

        assert looked == []
        assert wiki.entities == {}

    def test_an_nfd_model_identifier_finds_the_run_item(self):
        """The run item made under a model identifier in NFC is found when
        the identifier comes in NFD, and its label is kept in NFC."""
        model = nfc("Löbig-model")
        wiki = RunItemWiki()

        first = ensure_places_run_item(wiki, FINGERPRINT, model)
        again = ensure_places_run_item(wiki, FINGERPRINT, nfd(model))

        assert again == first
        assert list(wiki.entities) == [first]
        assert wiki.entities[first]["labels"]["en"]["value"] == (
            f"place-hierarchy run {model} {FINGERPRINT}"
        )

    def test_a_run_item_made_from_nfd_is_labelled_in_nfc(self):
        model = nfc("Löbig-model")
        wiki = RunItemWiki()

        qid = ensure_places_run_item(wiki, FINGERPRINT, nfd(model))

        assert wiki.entities[qid]["labels"]["en"]["value"] == (
            f"place-hierarchy run {model} {FINGERPRINT}"
        )
        assert ensure_places_run_item(wiki, FINGERPRINT, model) == qid


class TestAliases:
    def test_an_nfd_alias_equal_to_the_label_is_dropped(self):
        assert normalize_aliases(GUSTAV, [nfd(GUSTAV)]) == []

    def test_an_nfc_alias_equal_to_an_nfd_label_is_dropped(self):
        assert normalize_aliases(nfd(GUSTAV), [GUSTAV, "Gustl"]) == ["Gustl"]

    def test_an_alias_given_in_both_forms_is_kept_once(self):
        """The first form given is kept, verbatim."""
        sorne = nfc("Ida Sörne")
        assert normalize_aliases(
            "Ida Löbig", [nfd(sorne), sorne, "Peppi"]
        ) == [nfd(sorne), "Peppi"]

    def test_an_alias_of_control_characters_alone_is_dropped(self):
        """The instance keeps nothing of it, so it is blank."""
        assert normalize_aliases(GUSTAV, ["\x07", "Gustl"]) == ["Gustl"]

    def test_an_alias_equal_to_a_label_on_two_lines_is_dropped(self):
        """The instance keeps the label on one line, as the alias gives
        it."""
        assert normalize_aliases(
            "Gustav\nLöbig", ["Gustav Löbig", "Gustl"]
        ) == ["Gustl"]
