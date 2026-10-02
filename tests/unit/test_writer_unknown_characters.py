"""Offline tests of the characters the writer's Unicode tables do not know
(#431; `unknown_characters` and INSTANCE_UNICODE in
yizkr.pipeline.writer).

The instance puts text in NFC by the tables of Unicode 15.0 (ICU 72.1 in
the wikibase/wikibase:7 image, through UtfNormal 4.0.0 and PHP's intl),
the writer by Python's: 14.0.0 on Python 3.11, the interpreter CI runs.
By Unicode's normalization stability policy the two agree on every text
whose characters both assign, so they differ only on a text holding a
character Unicode assigned after 14.0.0. Ten of those have a combining
class of their own, U+1E08F, the combining Cyrillic letter
Byelorussian-Ukrainian i, among them: on a Cyrillic letter with a dot
below after it, the instance's NFC puts the dot first, and Python 3.11's
leaves them as they are. So the writer refuses what holds such a
character rather than send a text the instance may keep in another form:

* a claim whose value, qualifier, page number, region or excerpt holds
  one is refused whole, named in `refused`, with a warning naming the
  code point and quoting no text;
* a description, an alias or a type hint that holds one is left out,
  never changed, named in `refused_terms`, with a warning;
* an entity whose label holds one is neither found nor minted, and every
  claim that mentions it is refused;
* the run item's label or description holding one raises ValueError
  before anything is read or written, as the place-hierarchy pass's run
  item's label does;
* the registrar never makes an alias of a title that holds one, and
  always content-tags a new item's label made from one.

Here the fake keeps text as an instance with Unicode 15.0's tables would
(`newer_tables`), so a writer that sent such a text would be caught by a
second write that sends an edit. A noncharacter, which Unicode never
assigns, and a private-use character, which Python assigns, are written
as they are. A test that needs the interpreter not to know U+1E08F is
skipped on one that knows it. Every text comes from the fictional
universe (docs/EXAMPLES.md): (here) a copy of Nina Sartina's 1958 letter
whose transcriber wrote her surname with the combining Cyrillic i over
its a and a dot below it.
"""

from __future__ import annotations

import copy
import hashlib
import unicodedata

import pytest

from tests.unit import test_pipeline, test_registrar
from tests.unit.test_pipeline import (
    DIRECTORY_SHA1,
    INTERVIEW_SHA1,
    FakeWiki,
    _batch_of,
    _by_value,
    _cites,
    _directory_batch,
    _firm_batch,
    _labelled,
    _local,
    _places,
    _statements,
    _values,
)
from tests.unit.test_writer_limits import MEDIA, _quotes, _with_properties
from yizkr.claims import ClaimBatch, RegionPercent
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import (
    INSTANCE_UNICODE,
    TERM_LIMIT,
    TEXT_LIMIT,
    BatchWriter,
    _term_text,
    _unknown_fault,
    unknown_characters,
)

# The ten code points Unicode 15.0 assigns with a canonical combining
# class other than 0, which Python 3.11's 14.0.0 tables leave unassigned,
# with their classes: three Arabic small low words, the Kawi killer and
# conjoiner, the combining Cyrillic Byelorussian-Ukrainian i, and four
# Nag Mundari signs
NEWER_CLASSES = {
    0x10EFD: 220,
    0x10EFE: 220,
    0x10EFF: 220,
    0x11F41: 9,
    0x11F42: 9,
    0x1E08F: 230,
    0x1E4EC: 232,
    0x1E4ED: 232,
    0x1E4EE: 220,
    0x1E4EF: 230,
}
MARK = "\U0001e08f"
DOT_BELOW = "\u0323"
# Nina's surname as the copy writes it: the combining i over its a, then
# a dot below, which the instance puts before the i in NFC
MARKED = f"Са{MARK}{DOT_BELOW}ртина"
MARKED_KEPT = f"Са{DOT_BELOW}{MARK}ртина"
# a term that holds the mark past TERM_LIMIT, and a text that holds it
# past TEXT_LIMIT: a cut of either holds no character Python's tables do
# not know, and the writer still sends no cut of it
LONG_TERM = "Нина " * 52 + MARKED
LONG_TEXT = "Нина Сартина " * 40 + MARKED
VERSION = unicodedata.unidata_version
FAULT = (
    f"holds U+1E08F, a character Python's Unicode {VERSION} tables do not "
    f"know, which the instance may put in another order in NFC"
)

knows_the_mark = pytest.mark.skipif(
    unicodedata.category(MARK) != "Cn",
    reason=(
        f"this interpreter's Unicode {VERSION} tables assign U+1E08F, so "
        f"it is no character they do not know"
    ),
)

# (here) the copy of Nina's letter, registered as an artifact
LETTER_SHA1 = hashlib.sha1(
    b"fictional scan: a copy of Nina Sartina's 1958 letter to Leopold"
).hexdigest()
LETTER = {**MEDIA, LETTER_SHA1: "Q40"}
NINA = "Нина Лёбиг"
RUDOLF = "Лёбиг Рудольф Феликсович"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _newer_class(char: str) -> int:
    """`char`'s canonical combining class in Unicode 15.0's tables."""
    return NEWER_CLASSES.get(ord(char)) or unicodedata.combining(char)


def _newer_nfc(text: str) -> str:
    """`text` in NFC as Unicode 15.0's tables give it, emulated for the
    ten marks of NEWER_CLASSES: decomposed, each run of marks put in the
    order of their classes by those tables, then composed by Python's,
    which take each of the ten for a starter and so keep that order."""
    ordered: list[str] = []
    run: list[str] = []
    for char in unicodedata.normalize("NFD", text):
        if _newer_class(char):
            run.append(char)
            continue
        ordered += sorted(run, key=_newer_class)
        run = []
        ordered.append(char)
    ordered += sorted(run, key=_newer_class)
    return unicodedata.normalize("NFC", "".join(ordered))


@pytest.fixture
def newer_tables(monkeypatch):
    """The fakes keep and serve text as an instance whose NFC follows
    Unicode 15.0's tables: the writer's (test_pipeline._cleaned_up, which
    every term, value and served text goes through) and the registrar's
    (test_registrar.stored). A sufficient fake, not a complete one: it
    puts each run of marks in 15.0's order, which is all a test here needs
    to see a text kept in another form than it was sent, but it composes
    by Python's tables, so it composes nothing across one of the ten
    marks as 15.0's NFC does (`a`, U+1E4EE and U+0301 stay as they are,
    where 15.0 makes them `á` and U+1E4EE)."""
    cleaned_up = test_pipeline._cleaned_up
    stored = test_registrar.stored
    monkeypatch.setattr(
        test_pipeline, "_cleaned_up", lambda text: _newer_nfc(cleaned_up(text))
    )
    monkeypatch.setattr(
        test_registrar, "stored", lambda term: _newer_nfc(stored(term))
    )


def _written_again(wiki: FakeWiki, batch: ClaimBatch):
    """`batch` written a second time to `wiki`: the result, and whether
    the write sent no edit and made no item."""
    writes, items = wiki.wbi.item.writes, len(wiki.wbi.item.store)
    again = BatchWriter(wiki).write_batch(batch)
    return again, (wiki.wbi.item.writes, len(wiki.wbi.item.store)) == (
        writes,
        items,
    )


def _same_counts(first, again) -> bool:
    """Whether a second write refused and left out what the first did."""
    return (again.refused, again.refused_terms, again.warnings) == (
        first.refused,
        first.refused_terms,
        first.warnings,
    )


def _nina(label: str = NINA, **given) -> dict:
    return dict(_local("nina", label), **given)


def _named(text: str, key: str = "birth_name", **ref) -> dict:
    """Nina's `key` name as the copy gives it, `text`, in Russian."""
    return {
        "subject": _nina(**ref),
        "property": key,
        "value": {
            "type": "monolingual",
            "value": {"text": text, "language": "ru"},
        },
        "source": _cites(LETTER_SHA1, "Нина Васильевна Сартина", "ru"),
    }


def _widow(kinship: str = "вдова", **ref) -> dict:
    """Nina as Rudolf's widow, the kinship as the copy words it."""
    return {
        "subject": _nina(**ref),
        "property": "relative",
        "value": {"type": "item", "value": _local("rudolf", RUDOLF)},
        "qualifiers": [
            {
                "property": "kinship_type",
                "value": {"type": "string", "value": kinship},
            }
        ],
        "source": _cites(LETTER_SHA1, "вдова Рудольфа", "ru"),
    }


# the copy's clean claim, which every batch here writes beside the others
MARRIED = _named("Лёбиг", "married_name")


# -- which characters the writer's tables do not know -------------------------


class TestUnknownCharacters:
    """unknown_characters names the characters Python's tables leave
    unassigned, noncharacters aside: each once, in their order."""

    @knows_the_mark
    @pytest.mark.parametrize(
        "code", NEWER_CLASSES, ids=[f"U+{code:04X}" for code in NEWER_CLASSES]
    )
    def test_each_combining_mark_unicode_15_assigns_is_unknown(self, code):
        assert unknown_characters(f"Нина{chr(code)}") == [chr(code)]

    @pytest.mark.skipif(
        unicodedata.category("\u0897") != "Cn",
        reason=(
            f"this interpreter's Unicode {VERSION} tables assign U+0897, so "
            f"it is no character they do not know"
        ),
    )
    def test_a_mark_unicode_16_assigns_is_unknown(self):
        # U+0897, the Arabic pepet, a combining mark Unicode 16.0 assigns:
        # the rule is every character the tables leave unassigned, not
        # the ten marks of 15.0, so an instance whose ICU moves on to 16.0
        # is kept from text Python 3.11 to 3.13 does not know too
        assert unknown_characters("Нина\u0897") == ["\u0897"]

    def test_a_code_point_is_named_in_four_digits_at_least(self):
        # U+0378, which no version of Unicode assigns yet
        assert unknown_characters("Нина\u0378") == ["\u0378"]
        assert _unknown_fault(["\u0378"]).startswith(
            "holds U+0378, a character "
        )

    @knows_the_mark
    def test_each_is_named_once_in_its_order(self):
        text = f"{MARKED} {MARKED}\U00011f41"
        assert unknown_characters(text) == [MARK, "\U00011f41"]

    @pytest.mark.parametrize(
        "char",
        ["\ufdd0", "\ufdef", "\ufffe", "\uffff", "\U0001fffe", "\U0010ffff"],
        ids=["U+FDD0", "U+FDEF", "U+FFFE", "U+FFFF", "U+1FFFE", "U+10FFFF"],
    )
    def test_a_noncharacter_is_known(self, char):
        # Unicode never assigns a noncharacter, so every version's NFC
        # leaves it as it is
        assert unicodedata.category(char) == "Cn"
        assert unknown_characters(f"Pünktchen{char}") == []

    def test_a_private_use_character_is_known(self):
        assert unknown_characters("Pünktchen\ue000") == []

    @pytest.mark.parametrize(
        "text",
        [
            "Sebastian 'Wastl' Löbig",
            "Studio portrait of the Löbig family",
            RUDOLF,
            "Нина Васильевна Сартина",
            "Рудольфиха",
            "S. Faxridge-van 't Nijks",
            "Brun\u0304enring",
        ],
        ids=[
            "de",
            "en",
            "ru",
            "ru, a patronymic",
            "ru, a suffix",
            "fy",
            "a bar",
        ],
    )
    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_fictional_text_is_known(self, text, form):
        assert unknown_characters(unicodedata.normalize(form, text)) == []


class TestTheInterpreter:
    def test_its_tables_are_no_newer_than_the_instance(self):
        # the stability policy makes Python's NFC the instance's on every
        # text whose characters Python's tables assign only while those
        # tables are no newer than the instance's: an interpreter with
        # newer ones (Python 3.12's 15.0.0 is the newest that is not)
        # would send text the instance may keep in another form
        assert _version(VERSION) <= _version(INSTANCE_UNICODE)

    @knows_the_mark
    def test_the_fake_keeps_the_mark_as_the_instance_does(self, newer_tables):
        assert unicodedata.normalize("NFC", MARKED) == MARKED
        assert test_pipeline._kept_term(f" {MARKED}") == MARKED_KEPT
        assert test_pipeline._cleaned_up("a" + MARK + DOT_BELOW) == (
            "ạ" + MARK
        )


# -- claims: refused whole ----------------------------------------------------


@knows_the_mark
@pytest.mark.usefixtures("newer_tables")
class TestClaimsRefused:
    """A claim whose own text, or whose source block, holds a character
    Python's tables do not know is refused whole, and named; the batch's
    other claims are written, and a second write sends no edit."""

    @pytest.mark.parametrize(
        ("claim", "where", "mentions"),
        [
            (_named(f"Нина {MARKED}"), "its value", []),
            (
                _widow(f"вдова Рудольфа {MARKED}"),
                "its kinship_type qualifier",
                ["rudolf"],
            ),
        ],
        ids=["a value", "a qualifier"],
    )
    def test_a_claims_text_refuses_it(self, claim, where, mentions):
        batch = _batch_of(claim, MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.refused == [cid]
        assert written.refused_terms == []
        assert written.warnings == [
            f"claim {cid} ({claim['property']}) is refused, and nothing of "
            f"it is written: {where} {FAULT}. A cut or any other change "
            f"would alter what the source says"
        ]
        assert _quotes(written.warnings, MARKED, "вдова") == []
        assert written.written == 1
        assert _with_properties(wiki, claim["property"]) == []
        for local_id in mentions:
            assert local_id not in written.created_items
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)
        assert (again.written, again.skipped) == (0, 1)

    @pytest.mark.parametrize("where", ["page number", "region", "excerpt"])
    def test_a_locator_refuses_its_claim(self, where, monkeypatch):
        # a page number or excerpt the copy gives with Nina's surname in
        # it, and a region in a form that holds the mark
        claim = copy.deepcopy(_named("Нина Сартина"))
        source = claim["source"]
        if where == "page number":
            source["page_number"] = f"лист 2, {MARKED}"
        elif where == "excerpt":
            source["transcription_excerpt"]["text"] = f"Нина {MARKED}"
        else:
            monkeypatch.setattr(
                RegionPercent,
                "as_wikibase_string",
                lambda region: f"10%,20%,60%,10%{MARK}",
            )
            source["region"] = {"x": 10, "y": 20, "width": 60, "height": 10}
        batch = _batch_of(claim, MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.refused == [cid]
        assert written.warnings == [
            f"claim {cid} (birth_name) is refused, and nothing of it is "
            f"written: its {where} {FAULT}. A cut or any other change would "
            f"alter what the source says"
        ]
        assert _with_properties(wiki, "birth_name") == []
        (married,) = _statements(
            wiki, written.created_items["nina"], "married_name"
        )
        assert [place[0] for place in _places(married)] == ["Q40"]
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_each_character_is_named(self):
        # the surname with the mark, then the Kawi killer, which Unicode
        # 15.0 assigns too
        batch = _batch_of(_named(f"Нина {MARKED}\U00011f41"), MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.warnings == [
            f"claim {cid} (birth_name) is refused, and nothing of it is "
            f"written: its value holds U+1E08F and U+11F41, characters "
            f"Python's Unicode {VERSION} tables do not know, which the "
            f"instance may put in another order in NFC. A cut or any other "
            f"change would alter what the source says"
        ]

    def test_a_text_gives_one_clause(self):
        # the mark twice in the value: one clause names it, once
        batch = _batch_of(_named(f"Нина {MARKED} {MARKED}"), MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.warnings == [
            f"claim {claim_id(batch.claims[0])} (birth_name) is refused, and "
            f"nothing of it is written: its value {FAULT}. A cut or any "
            f"other change would alter what the source says"
        ]

    def test_a_value_and_a_qualifier_give_a_clause_each(self):
        # the mark in the value and in a qualifier: one clause per text
        claim = dict(
            _named(f"Нина {MARKED}"),
            qualifiers=[
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": f"вдова {MARKED}"},
                }
            ],
        )
        batch = _batch_of(claim, MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.warnings == [
            f"claim {claim_id(batch.claims[0])} (birth_name) is refused, and "
            f"nothing of it is written: its value {FAULT}; its kinship_type "
            f"qualifier {FAULT}. A cut or any other change would alter what "
            f"the source says"
        ]

    def test_a_dry_run_refuses_the_same_and_writes_nothing(self):
        batch = _batch_of(
            _named(f"Нина {MARKED}"),
            MARRIED,
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "aliases": [f"Нина {MARKED}"],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        planned = BatchWriter(wiki, dry_run=True).write_batch(batch)

        assert wiki.wbi.item.writes == 0
        assert planned.refused == [claim_id(batch.claims[0])]
        assert planned.refused_terms == ["nina: alias"]
        written = BatchWriter(wiki).write_batch(batch)
        assert _same_counts(planned, written)


# -- labels: no item found or minted by one -----------------------------------


@knows_the_mark
@pytest.mark.usefixtures("newer_tables")
class TestLabels:
    """An entity whose label holds a character Python's tables do not know
    is neither found nor minted, and each claim that mentions it is
    refused before the first write."""

    @pytest.mark.parametrize("manifest", [True, False], ids=["record", "ref"])
    def test_every_claim_that_mentions_it_is_refused(self, manifest):
        label = f"Нина {MARKED}"
        if manifest:
            claims = (_named("Нина Сартина"), _widow())
            entities = ({"id": "nina", "label": label, "kind": "human"},)
        else:
            # the first ref gives her label clean, a later one marked: any
            # of them may be the one the writer resolves first
            claims = (_named("Нина Сартина"), _widow(label=label))
            entities = ()
        # Rudolf's own claim, which names her as his wife
        rudolf = {
            "subject": _local("rudolf", RUDOLF),
            "property": "relative",
            "value": {"type": "item", "value": _nina(label)},
            "qualifiers": [],
            "source": _cites(LETTER_SHA1, "жена Нина", "ru"),
        }
        batch = _batch_of(*claims, rudolf, entities=entities)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        cids = [claim_id(claim) for claim in batch.claims]
        assert written.refused == cids
        assert written.written == 0
        assert written.refused_terms == ["nina: label"]
        properties = ["birth_name", "relative", "relative"]
        assert written.warnings == [
            *(
                f"claim {cid} ({key}) is refused, and nothing of it is "
                f"written: it mentions entity 'nina', whose label {FAULT}, "
                f"so that no item is found or made by it. A cut or any "
                f"other change would alter what the source says"
                for cid, key in zip(cids, properties, strict=True)
            ),
            f"entity 'nina' has a label that {FAULT}: no item is found or "
            f"made by it, and any claim that mentions it is refused",
        ]
        assert "nina" not in written.created_items
        assert "rudolf" not in written.created_items
        assert _labelled(wiki, label) == []
        assert _labelled(wiki, f"Нина {MARKED_KEPT}") == []
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_the_characters_of_every_ref_are_named(self):
        # one ref gives her label with the mark, a later one with the Kawi
        # killer: the label is named by the characters of both
        batch = _batch_of(
            _named("Лёбиг", "married_name", label=f"Нина {MARKED}"),
            _widow(label="Нина Сартина\U00011f41"),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused_terms == ["nina: label"]
        assert written.warnings[-1] == (
            f"entity 'nina' has a label that holds U+1E08F and U+11F41, "
            f"characters Python's Unicode {VERSION} tables do not know, "
            f"which the instance may put in another order in NFC: no item "
            f"is found or made by it, and any claim that mentions it is "
            f"refused"
        )

    def test_an_entity_mentioned_twice_gives_one_clause(self):
        # a claim that names her as its subject and as its value
        label = f"Нина {MARKED}"
        claim = dict(
            _widow(label=label), value={"type": "item", "value": _nina(label)}
        )
        batch = _batch_of(claim, MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.warnings[0].count("it mentions entity 'nina'") == 1

    def test_a_clean_record_label_outranks_a_marked_ref(self):
        # the manifest record names Nina cleanly, and the writer names her
        # item by it: a ref's marked label is never sent
        claim = _named("Лёбиг", "married_name", label=f"Нина {MARKED}")
        batch = _batch_of(
            claim, entities=({"id": "nina", "label": NINA, "kind": "human"},)
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert (written.written, written.refused) == (1, [])
        assert (written.refused_terms, written.warnings) == ([], [])
        nina = wiki.wbi.item.get(written.created_items["nina"])
        assert str(nina.labels.get("en")) == NINA
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_an_unnamed_entity_names_its_label_alone(self):
        # nothing of an entity the writer cannot name is sent, so only its
        # label is named, not its marked description or alias
        batch = _batch_of(
            MARRIED,
            entities=(
                {
                    "id": "nina",
                    "label": f"Нина {MARKED}",
                    "kind": "human",
                    "summary": f"born {MARKED}",
                    "aliases": [f"Нина {MARKED}"],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[0])]
        assert written.refused_terms == ["nina: label"]

    def test_a_record_names_no_item_the_resolve_step_found(self):
        # an item labelled with the mark and no dot below, a form both
        # tables keep alike; the resolve step matched the record to it and
        # gave the claim its QID. The record's label holds the mark, so it
        # names no item, and neither its alias nor its kind is recorded
        wiki = FakeWiki(LETTER, detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", f"Нина Са{MARK}ртина")
        nina = item.write().id
        claim = dict(MARRIED, subject={"kind": "qid", "qid": nina})
        batch = _batch_of(
            claim,
            entities=(
                {
                    "id": "nina",
                    "label": f"Нина Са{MARK}ртина",
                    "kind": "human",
                    "aliases": ["Нина Сартина"],
                },
            ),
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert (written.written, written.refused) == (1, [])
        assert written.refused_terms == ["nina: label"]
        assert written.warnings == [
            f"entity 'nina' has a label that holds U+1E08F, a character "
            f"Python's Unicode {VERSION} tables do not know, which the "
            f"instance may put in another order in NFC: no item is found or "
            f"made by it, and any claim that mentions it is refused"
        ]
        found = wiki.wbi.item.get(nina)
        assert found.aliases.get("en") is None
        assert not _statements(wiki, nina, "instance_of")
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_a_label_an_equivalence_names_is_no_alias(self):
        # Nina's item, minted from the copy's clean label; a later batch's
        # record names it by an approved equivalence, its label marked:
        # that label would go as an alias of the item, and is left out
        wiki = FakeWiki(LETTER, detached=True)
        nina = (
            BatchWriter(wiki)
            .write_batch(_batch_of(MARRIED))
            .created_items["nina"]
        )
        claim = dict(
            _named("Нина Сартина"), subject={"kind": "qid", "qid": nina}
        )
        batch = _batch_of(
            claim,
            entities=(
                {
                    "id": "nina",
                    "label": f"Нина {MARKED}",
                    "kind": "human",
                    # a description the writer never sends to an item an
                    # equivalence names, so it is not named either
                    "summary": f"Rudolf's widow, born {MARKED}",
                    "aliases": ["Нина Сартина", f"Нина {MARKED}"],
                    "qid": nina,
                },
            ),
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == []
        assert written.refused_terms == ["nina: label", "nina: alias"]
        assert [
            str(alias) for alias in wiki.wbi.item.get(nina).aliases.get("en")
        ] == ["Нина Сартина"]
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)


# -- descriptions, aliases and type hints: left out ---------------------------


@knows_the_mark
@pytest.mark.usefixtures("newer_tables")
class TestTermsLeftOut:
    """A description, an alias or a type hint that holds a character
    Python's tables do not know is left out, never changed, and named; the
    item is written without it."""

    def test_a_description_and_an_alias_are_left_out(self):
        batch = _batch_of(
            MARRIED,
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "summary": f"Rudolf's widow, born {MARKED}",
                    "aliases": ["Нина Сартина", f"Нина {MARKED}"],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == []
        assert written.refused_terms == ["nina: description", "nina: alias"]
        assert written.warnings == [
            f"entity 'nina' has a description that {FAULT}: it is left out",
            f"entity 'nina' has an alias that {FAULT}: it is left out",
        ]
        nina = wiki.wbi.item.get(written.created_items["nina"])
        assert str(nina.labels.get("en")) == NINA
        assert nina.descriptions.get("en") is None
        assert [str(alias) for alias in nina.aliases.get("en")] == [
            "Нина Сартина"
        ]
        (married,) = _statements(wiki, nina.id, "married_name")
        assert _values(married) == ["Лёбиг"]
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_a_refs_description_is_left_out(self):
        # no manifest: the first ref's description is the one the writer
        # would send
        batch = _batch_of(
            _named("Лёбиг", "married_name", description=f"born {MARKED}")
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused_terms == ["nina: description"]
        nina = wiki.wbi.item.get(written.created_items["nina"])
        assert nina.descriptions.get("en") is None
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_a_refused_claims_ref_gives_no_description(self):
        # the first ref to Nina is in a claim refused for its value, and
        # the writer never resolves it: the description sent is the clean
        # one of the ref in the claim it writes, and none is named
        batch = _batch_of(
            _named(f"Нина {MARKED}", description=f"born {MARKED}"),
            _named("Лёбиг", "married_name", description="вдова Рудольфа"),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[0])]
        assert written.refused_terms == []
        nina = wiki.wbi.item.get(written.created_items["nina"])
        assert str(nina.descriptions.get("en")) == "вдова Рудольфа"
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    @pytest.mark.parametrize(
        "marked_first", [True, False], ids=["marked first", "clean first"]
    )
    def test_the_first_refs_description_is_the_one_counted(self, marked_first):
        # no manifest, two refs to Nina in claims the writer writes: the
        # first gives the description the writer sends, and only it is
        # named when it holds the mark
        marked = _named("Лёбиг", "married_name", description=f"born {MARKED}")
        clean = _named("Нина Сартина", description="вдова Рудольфа")
        batch = _batch_of(
            *((marked, clean) if marked_first else (clean, marked))
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert (written.written, written.refused) == (2, [])
        assert written.refused_terms == (
            ["nina: description"] if marked_first else []
        )
        nina = wiki.wbi.item.get(written.created_items["nina"])
        sent = nina.descriptions.get("en")
        assert (str(sent) if sent else None) == (
            None if marked_first else "вдова Рудольфа"
        )
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_a_record_only_a_refused_claim_mentions_names_no_description(
        self,
    ):
        # her one claim is refused for its value, so no item is minted for
        # her and her description would never be sent: it is not named.
        # Her alias still is, as the writer may send it to an item the
        # resolve step matched her record to
        batch = _batch_of(
            _named(f"Нина {MARKED}"),
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "summary": f"Rudolf's widow, born {MARKED}",
                    "aliases": [f"Нина {MARKED}"],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[0])]
        assert written.refused_terms == ["nina: alias"]
        assert "nina" not in written.created_items

    def test_each_term_left_out_is_named(self):
        # two aliases with the mark: each is named, and warned of, so the
        # count is of the terms left out
        batch = _batch_of(
            MARRIED,
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "aliases": [f"Нина {MARKED}", f"Нина Васильевна {MARKED}"],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused_terms == ["nina: alias", "nina: alias"]
        assert (
            written.warnings
            == [f"entity 'nina' has an alias that {FAULT}: it is left out"] * 2
        )

    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_a_type_hint_is_left_out(self, found):
        # (here) the directory's heading for the firm's trade, which a
        # transcriber gave with the mark
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        if found:
            BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_batch(
            "unclassified_entity", type_hint=f"шляпный {MARKED}"
        )

        written = BatchWriter(wiki).write_batch(directory)

        assert written.refused_terms == ["firm: type hint"]
        assert written.warnings == [
            f"entity 'firm' has a type hint that {FAULT}: it is left out"
        ]
        firm = written.created_items["firm"]
        hints = set(_by_value(_statements(wiki, firm, "type_hint")))
        assert hints == ({"hat shop"} if found else set())
        again, unchanged = _written_again(wiki, directory)
        assert unchanged
        assert _same_counts(written, again)


# -- past a cut: the whole text counts ----------------------------------------


@knows_the_mark
@pytest.mark.usefixtures("newer_tables")
class TestPastTheCut:
    """A text whose character Python's tables do not know lies past the
    cut (TERM_LIMIT, TEXT_LIMIT) is treated as one that holds it anywhere:
    a cut of it is never sent, and what the result names is what was left
    out."""

    def test_the_cuts_hold_no_unknown_character(self):
        assert len(LONG_TERM) > TERM_LIMIT
        assert unknown_characters(LONG_TERM) == [MARK]
        assert unknown_characters(_term_text(LONG_TERM)) == []
        assert len(LONG_TEXT) > TEXT_LIMIT

    def test_a_long_description_and_alias_are_left_out(self):
        batch = _batch_of(
            MARRIED,
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "summary": LONG_TERM,
                    "aliases": ["Нина Сартина", LONG_TERM],
                },
            ),
        )
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused_terms == ["nina: description", "nina: alias"]
        nina = wiki.wbi.item.get(written.created_items["nina"])
        assert nina.descriptions.get("en") is None
        assert [str(alias) for alias in nina.aliases.get("en")] == [
            "Нина Сартина"
        ]
        again, unchanged = _written_again(wiki, batch)
        assert unchanged
        assert _same_counts(written, again)

    def test_a_long_alias_goes_to_no_named_item(self):
        # an approved equivalence's aliases go to its item as other
        # aliases do
        wiki = FakeWiki(LETTER, detached=True)
        nina = (
            BatchWriter(wiki)
            .write_batch(_batch_of(MARRIED))
            .created_items["nina"]
        )
        claim = dict(
            _named("Нина Сартина"), subject={"kind": "qid", "qid": nina}
        )
        batch = _batch_of(
            claim,
            entities=(
                {
                    "id": "nina",
                    "label": NINA,
                    "kind": "human",
                    "aliases": ["Нина Сартина", LONG_TERM],
                    "qid": nina,
                },
            ),
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused_terms == ["nina: alias"]
        assert [
            str(alias) for alias in wiki.wbi.item.get(nina).aliases.get("en")
        ] == ["Нина Сартина"]

    def test_a_long_label_names_no_item(self):
        # an item labelled with the label's cut, which holds no mark: the
        # record still names no item, so neither its alias nor its kind
        # is recorded, as the claims that mention it by local id are
        # refused
        wiki = FakeWiki(LETTER, detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", _term_text(LONG_TERM))
        nina = item.write().id
        claim = dict(MARRIED, subject={"kind": "qid", "qid": nina})
        batch = _batch_of(
            claim,
            _named("Нина Сартина"),
            entities=(
                {
                    "id": "nina",
                    "label": LONG_TERM,
                    "kind": "human",
                    "aliases": ["Нина Сартина"],
                },
            ),
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[1])]
        assert written.refused_terms == ["nina: label"]
        assert wiki.wbi.item.get(nina).aliases.get("en") is None
        assert not _statements(wiki, nina, "instance_of")

    def test_a_long_excerpt_refuses_its_claim(self):
        claim = copy.deepcopy(_named("Нина Сартина"))
        claim["source"]["transcription_excerpt"]["text"] = LONG_TEXT
        batch = _batch_of(claim, MARRIED)
        wiki = FakeWiki(LETTER, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[0])]
        assert _with_properties(wiki, "birth_name") == []


# -- the run item -------------------------------------------------------------


@knows_the_mark
@pytest.mark.parametrize(
    ("given", "term"),
    [
        ({"model_identifier": f"fixture model {MARKED}"}, "label"),
        # past the cut of the label, which keeps a digest of the whole
        (
            {"model_identifier": "fixture-model-" + "x" * 300 + MARK},
            "label",
        ),
        ({"pipeline_version": f"0.0.0+{MARK}"}, "description"),
        ({"software_commit": f"abc{MARK}def"}, "description"),
    ],
    ids=["model", "model past the cut", "pipeline version", "commit"],
)
def test_a_run_item_term_refuses_the_batch_before_any_read(given, term):
    run = {
        "model_identifier": "fixture model",
        "pipeline_version": "0.0.0",
        "started_at": "2026-07-12T09:00:00Z",
        **given,
    }
    batch = _batch_of(MARRIED, run=run)
    wiki = FakeWiki(LETTER, detached=True)

    with pytest.raises(ValueError, match="U\\+1E08F") as refused:
        BatchWriter(wiki).write_batch(batch)

    assert f"extraction run's {term}" in str(refused.value)
    assert (wiki.index_reads, wiki.wbi.item.writes) == (0, 0)


@knows_the_mark
def test_a_place_hierarchy_run_label_refuses_before_any_read():
    # the place-hierarchy pass's run item is found by its label, which
    # holds the model's identifier
    wiki = test_registrar.Untouchable()

    with pytest.raises(ValueError, match="U\\+1E08F") as refused:
        ensure_places_run_item(wiki, "0a9afd5746", f"fixture model {MARKED}")

    assert "place-hierarchy run's label" in str(refused.value)
    assert wiki.asked == []


# -- the registrar ------------------------------------------------------------


@knows_the_mark
def test_a_title_registered_again_sends_no_edit(tmp_path, newer_tables):
    # the copy's scan registered under its title, which names Nina with the
    # mark: the instance keeps the label in another form than the title,
    # and a second registration makes no alias of the title
    wiki = test_registrar.StoringWiki()
    path = test_registrar.scan(tmp_path, b"\x89PNG synthetic: Nina's letter")
    title = f"Letter of Нина {MARKED} to Leopold"
    first = test_registrar.register(wiki, path, title)
    writes = wiki.writes

    again = test_registrar.register(wiki, path, title)

    assert again.artifact_qid == first.artifact_qid
    assert wiki.writes == writes
    entity = wiki.entities[first.artifact_qid]
    # a label made from a marked title is always tagged (the test below)
    tag = hashlib.sha1(b"\x89PNG synthetic: Nina's letter").hexdigest()[:8]
    assert entity["labels"]["en"]["value"] == (
        f"Letter of Нина {MARKED_KEPT} to Leopold [{tag}]"
    )
    assert entity.get("aliases", {}).get("en", []) == []


# two scans of the copy of Nina's letter, each in a folder of its own
# under the same file name: two artifacts, one title
LETTER_SCAN = b"\x89PNG synthetic scan of the copy of Nina's letter"
SECOND_SCAN = b"\x89PNG synthetic scan of the copy of Nina's letter, 1959"


@knows_the_mark
def test_same_named_copies_under_a_marked_title_both_register(
    tmp_path, newer_tables
):
    # the orchestrator gives both the same title and description, from
    # the file name: the instance keeps the first one's label in its own
    # order, which the second's would match unseen, and Wikibase would
    # refuse the second's label and description as the first's, after the
    # item is made. A marked title always takes the content tag, so each
    # copy registers, with its page, under a label of its own
    wiki = test_registrar.StoringWiki()
    title = f"Letter of Нина {MARKED} to Leopold"
    paths = [
        test_registrar.scan(tmp_path, data, "letter-1958.png", folder)
        for data, folder in ((LETTER_SCAN, "letters"), (SECOND_SCAN, "copies"))
    ]

    def register_all():
        return [
            test_registrar.register_artifact(
                wiki,
                media_path=path,
                artifact_type="photograph",
                title=title,
                description=(
                    "ingested by `yizkr ingest run` from letter-1958.png"
                ),
                pages=1,
            )
            for path in paths
        ]

    first = register_all()

    # two artifacts and their two pages, and no item without a label
    assert len(wiki.entities) == 4
    assert all(test_registrar.label_of(wiki, qid) for qid in wiki.entities)
    kept = f"Letter of Нина {MARKED_KEPT} to Leopold"
    for result, data in zip(first, (LETTER_SCAN, SECOND_SCAN), strict=True):
        tag = hashlib.sha1(data).hexdigest()[:8]
        assert test_registrar.label_of(wiki, result.artifact_qid) == (
            f"{kept} [{tag}]"
        )
        # a page's label shows its artifact's, tag and all, and is not
        # tagged again
        assert test_registrar.label_of(wiki, result.fragment_qids[1]) == (
            f"{kept} [{tag}], page 1"
        )
    writes, snapshot = wiki.writes, copy.deepcopy(wiki.entities)

    again = register_all()

    assert [r.artifact_qid for r in again] == [r.artifact_qid for r in first]
    assert (wiki.writes, wiki.entities) == (writes, snapshot)
