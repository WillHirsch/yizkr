"""Offline tests: the label lookups outside the writer compare text in the
form the instance keeps a term in, as the writer does (stored_term,
_term_text; #370). Each is checked over a label given in NFD, as a source
transcribed on another system may give it.

The universe's names with a precomposed letter serve: Gustav Löbig, Ida
Sörne, Flörburg."""

from __future__ import annotations

import unicodedata

import pytest

from tests.unit.test_pipeline import ARTIFACT, RUN
from tests.unit.test_places import FINGERPRINT, RunItemWiki
from yizkr.claims import ClaimBatch, LocalRef, QidRef
from yizkr.pipeline.names import normalize_aliases
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
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


def naming(*labels: str) -> ClaimBatch:
    """A batch of one claim for each of `labels`, its subject a person's
    local ref with that label."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": f"e{n}",
                        "label": label,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1919},
                    "source": {"artifact": ARTIFACT},
                }
                for n, label in enumerate(labels)
            ],
        }
    )


class Instance:
    """The reads resolve makes of the instance: its item labels, as the
    instance serves them, and the statements of the items they name (none
    here, so none is an artifact)."""

    def __init__(self, labels: dict[str, str]) -> None:
        self.labels = labels
        self.fetched: list[str] = []

    def label_index(self):
        return {}, dict(self.labels)

    def vocabulary(self):
        return {}, {}

    def fetch_claims(self, qids):
        self.fetched.extend(qids)
        return {}


def subjects(batch: ClaimBatch) -> list:
    return [claim.subject for claim in batch.claims]


class TestResolve:
    def test_an_nfd_label_resolves_to_the_item_under_its_nfc_form(self):
        instance = Instance({GUSTAV: "Q7"})
        batch = naming(nfd(GUSTAV))

        labels = resolvable_labels(instance, batch)

        assert labels == {GUSTAV: "Q7"}
        assert instance.fetched == ["Q7"]
        assert subjects(resolve_batch(batch, labels)) == [QidRef(qid="Q7")]

    def test_resolve_batch_looks_an_nfd_label_up_in_nfc(self):
        """The instance's whole label index, as the live tests pass it,
        resolves an NFD ref."""
        (subject,) = subjects(
            resolve_batch(naming(nfd(GUSTAV)), {GUSTAV: "Q7"})
        )
        assert subject == QidRef(qid="Q7")

    def test_a_label_the_instance_trims_resolves(self):
        """Whitespace and a line break the instance trims or makes one
        space of do not keep a ref from the item the writer reuses."""
        given = "  Gustav\nLöbig\t"
        assert _term_text(given) == GUSTAV
        instance = Instance({GUSTAV: "Q7"})
        batch = naming(given)

        labels = resolvable_labels(instance, batch)

        assert subjects(resolve_batch(batch, labels)) == [QidRef(qid="Q7")]

    def test_a_label_longer_than_the_instance_holds_resolves(self):
        """The writer looks such a label up cut, with its digest; resolve
        matches the same item."""
        given = nfd("Hutmacherei zum Blauen Hut, Flörburg " * 10)
        assert len(given) > TERM_LIMIT
        held = _term_text(given)
        instance = Instance({held: "Q9"})
        batch = naming(given)

        labels = resolvable_labels(instance, batch)

        assert labels == {held: "Q9"}
        assert subjects(resolve_batch(batch, labels)) == [QidRef(qid="Q9")]

    def test_a_label_with_an_unknown_character_stays_local(self):
        """The instance may keep it in another form, and the writer matches
        no item by it, so neither does resolve."""
        given = f"{GUSTAV}{MARK}"
        instance = Instance({given: "Q7", GUSTAV: "Q8"})
        batch = naming(given)

        assert resolvable_labels(instance, batch) == {}
        (subject,) = subjects(resolve_batch(batch, {given: "Q7"}))
        assert isinstance(subject, LocalRef)

    def test_an_unknown_character_past_the_cut_stays_local(self):
        """The mark falls after the 250 characters the instance holds, but
        the whole label is checked, as the writer checks it: the item under
        the cut label is not matched."""
        given = nfd("Hutmacherei zum Blauen Hut, Flörburg " * 8) + MARK
        held = _term_text(given)
        assert MARK not in held
        instance = Instance({held: "Q9"})
        batch = naming(given)

        assert resolvable_labels(instance, batch) == {}
        (subject,) = subjects(resolve_batch(batch, {held: "Q9"}))
        assert isinstance(subject, LocalRef)

    def test_another_label_stays_local(self):
        batch = naming(nfd("Ida Sörne"))
        instance = Instance({GUSTAV: "Q7"})

        assert resolvable_labels(instance, batch) == {}
        assert instance.fetched == []
        (subject,) = subjects(resolve_batch(batch, {GUSTAV: "Q7"}))
        assert isinstance(subject, LocalRef)


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
