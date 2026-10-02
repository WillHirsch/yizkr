"""Offline tests of the shape of the text the writer sends (#384;
LINE_BREAKS, WHITESPACE, TERM_LIMIT and `_term_text` in
yizkr.pipeline.writer).

Wikibase refuses a text value, a statement's, a qualifier's or a
reference's, that holds a line break or a tab, or has whitespace at either
end: its `illegal-string-chars` check, /^\\s|[\\v\\t]|\\s$/u, which PHP
matches by Unicode property. It keeps a label, a description or an alias
trimmed and cleaned up (StringNormalizer::trimToNFC), and refuses one of
more than 250 characters once trimmed. It cleans up a string value before
it stores it, and every text its API gives back (#403): each C0 control
character but the tab, the line feed and the carriage return, and U+FFFE
and U+FFFF, made U+FFFD. It refuses the whole edit that sends a text it
does not hold. The unit fake refuses, keeps and serves text as the
instance does (`_refuse_as_the_instance`, `_kept_term`, `_as_stored` and
`_as_served` in test_pipeline.py), its character classes written out
apart from the writer's. The writer sends no text the instance would
refuse, or give back changed:

* a claim whose own text, its value or a qualifier's, holds a line break
  or a tab, or has whitespace at either end, or holds a character the
  instance cleans up, is refused, never changed (CLAUDE.md rule 5), as one
  over the limit is (test_writer_limits.py): nothing of it is written, the
  result names it, and a warning says why, quoting none of it. Every other
  claim is written;
* a reference's page number, region and excerpt go on one line:
  whitespace at either end dropped, a line break inside made " | ", a tab
  a space, and each other character the instance cleans up U+FFFD. One of
  whitespace alone is left out, and one still over the limit is cut, with
  the digest of the one-line text;
* an entity's type hint, free text the writer compares ignoring its
  spacing, goes with its whitespace as single spaces, cleaned up;
* a label, a description or an alias goes as the instance keeps it, and
  one over 250 characters is cut as a page number is, with the digest of
  the whole: a label given in another form or at length finds its item
  again, two labels or aliases that agree up to the cut stay two, and the
  run item's label, which holds the model's identifier, is found again.

The rule the writer and the registrar share for a term, and the probes of
#403, are tested in test_writer_stored_terms.py.

Every text comes from the fictional universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import unicodedata

import pytest
from wikibaseintegrator.wbi_exceptions import ModificationFailed

from tests.unit.test_pipeline import (
    DIRECTORY_SHA1,
    INTERVIEW_SHA1,
    ITEMS,
    MEDIA_SHA1,
    RUN,
    SEBASTIAN,
    TEXT_CLAIMS,
    TEXT_OF,
    TREE_SHA1,
    WIKIBASE_BREAK_OR_TAB,
    WIKIBASE_CLEANED,
    WIKIBASE_SPACE,
    FakeWiki,
    _aliases,
    _batch_of,
    _by_value,
    _certificate_batch,
    _directory_batch,
    _firm_batch,
    _labelled,
    _not_in_nfc,
    _places,
    _snaks,
    _sources,
    _statements,
    _tree_batch,
    _values,
)
from tests.unit.test_writer_limits import (
    BIRTH,
    MEDIA,
    MENTIONS,
    WHERE,
    _cause,
    _item_holding,
    _mark,
    _quotes,
    _quoting,
    _said_over,
    _saying,
    _to_the_last_space,
    _with_properties,
)
from yizkr.claims import ClaimBatch, RegionPercent
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import (
    TERM_LIMIT,
    TEXT_LIMIT,
    BatchWriter,
    batch_fingerprint,
)

# the characters these tests give texts, by name
LINE_SEPARATOR = "\N{LINE SEPARATOR}"
NEXT_LINE = "\x85"
NO_BREAK_SPACE = "\xa0"
IDEOGRAPHIC_SPACE = "\N{IDEOGRAPHIC SPACE}"
# whitespace to Wikibase, and not to Python's str.isspace()
VOWEL_SEPARATOR = "\N{MONGOLIAN VOWEL SEPARATOR}"
# whitespace to neither
ZERO_WIDTH_SPACE = "\N{ZERO WIDTH SPACE}"


# -- the fake -----------------------------------------------------------------

# a text value by what gives it a shape Wikibase refuses. WikibaseIntegrator
# itself refuses a line feed or a carriage return before anything is sent,
# so the fake is shown the other line breaks
REFUSED_TEXTS = {
    "a line separator inside": f"Blatt{LINE_SEPARATOR}12",
    "a next line (NEL) inside": f"Blatt{NEXT_LINE}12",
    "a tab inside": "Blatt\t12",
    "a space at its start": " Blatt 12",
    "a no-break space at its end": f"Blatt 12{NO_BREAK_SPACE}",
    "an ideographic space at its end": f"Blatt 12{IDEOGRAPHIC_SPACE}",
    "a Mongolian vowel separator at its start": f"{VOWEL_SEPARATOR}Blatt 12",
}
# and the line breaks Wikibase refuses in a monolingual text alone: it
# makes each U+FFFD in a string value before it checks it (#401's review)
MONOLINGUAL_REFUSED = {
    "a vertical tab inside": "Blatt\x0b12",
    "a form feed at its end": "Blatt 12\x0c",
}
# and by what gives it a shape Wikibase holds
HELD_TEXTS = {
    "a no-break space inside": f"Blatt{NO_BREAK_SPACE}12",
    "two spaces inside": "Blatt  12",
    "a zero-width space at its end": f"Blatt 12{ZERO_WIDTH_SPACE}",
    "a Mongolian vowel separator inside": f"Blatt{VOWEL_SEPARATOR}12",
}


class TestTheFakeRefusesWhatWikibaseRefuses:
    """The fake refuses an edit whole, as the instance does, when a text
    value in it holds a line break or a tab, or has whitespace at either
    end; and keeps a label, a description or an alias trimmed, refusing
    one of more than 250 characters once trimmed, or an alias with nothing
    left (_refuse_as_the_instance, _kept_term)."""

    @pytest.mark.parametrize("kind", ["string", "monolingual"])
    @pytest.mark.parametrize("where", ["value", "qualifier", "reference"])
    @pytest.mark.parametrize("text", REFUSED_TEXTS.values(), ids=REFUSED_TEXTS)
    def test_a_text_value_of_a_shape_wikibase_refuses(self, text, where, kind):
        wiki, item = _item_holding(where, kind, text)

        with pytest.raises(ModificationFailed, match="Illegal characters"):
            item.write()

        assert (wiki.wbi.item.writes, wiki.wbi.item.store) == (0, {})

    @pytest.mark.parametrize("kind", ["string", "monolingual"])
    @pytest.mark.parametrize("where", ["value", "qualifier", "reference"])
    @pytest.mark.parametrize(
        "text", MONOLINGUAL_REFUSED.values(), ids=MONOLINGUAL_REFUSED
    )
    def test_a_vertical_tab_or_form_feed_in_a_string_value_is_changed(
        self, text, where, kind
    ):
        wiki, item = _item_holding(where, kind, text)

        if kind == "monolingual":
            with pytest.raises(ModificationFailed, match="Illegal characters"):
                item.write()
            assert wiki.wbi.item.store == {}
        else:
            item.write()
            assert wiki.wbi.item.writes == 1

    @pytest.mark.parametrize("kind", ["string", "monolingual"])
    @pytest.mark.parametrize("text", HELD_TEXTS.values(), ids=HELD_TEXTS)
    def test_a_text_value_of_a_shape_wikibase_holds(self, text, kind):
        wiki, item = _item_holding("value", kind, text)

        item.write()

        assert wiki.wbi.item.writes == 1

    def test_a_term_is_kept_trimmed(self):
        wiki = FakeWiki(detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", f"\tEugen\r\nLöbig{IDEOGRAPHIC_SPACE}")
        item.descriptions.set("en", "  ")
        item.aliases.set("en", ["Muckel ", " Muckel", "Muckel\n"])

        item.write()

        (kept,) = wiki.wbi.item.store.values()
        assert str(kept.labels.get("en")) == "Eugen Löbig"
        assert kept.descriptions.get("en") is None
        assert [str(alias) for alias in kept.aliases.get("en")] == ["Muckel"]

    @pytest.mark.parametrize(
        ("label", "alias"),
        [
            ("Eugen Löbig" + "s" * 240, None),
            (f"Eugen{LINE_SEPARATOR}Löbig", None),
            (f"{VOWEL_SEPARATOR}Eugen Löbig", None),
            ("Eugen Löbig", " \t "),
        ],
        ids=[
            "a label of 251 characters",
            "a line separator inside a label",
            "a Mongolian vowel separator at a label's start",
            "an alias of whitespace alone",
        ],
    )
    def test_a_term_wikibase_refuses(self, label, alias):
        wiki = FakeWiki(detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", label)
        if alias:
            item.aliases.set("en", [alias])

        with pytest.raises(ModificationFailed):
            item.write()

        assert wiki.wbi.item.store == {}

    def test_an_item_whose_label_is_its_description_is_refused(self):
        # the two compared as Wikibase keeps them
        wiki = FakeWiki(detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", "Eugen Löbig")
        item.descriptions.set("en", "Eugen Löbig ")

        with pytest.raises(ModificationFailed, match="must not be the same"):
            item.write()

        assert wiki.wbi.item.store == {}

    def test_a_term_is_counted_once_trimmed(self):
        wiki = FakeWiki(detached=True)
        item = wiki.wbi.item.new()
        label = f"  {'s' * 250}\n"
        item.labels.set("en", label)

        item.write()

        assert wiki.wbi.item.writes == 1

    def test_a_term_is_kept_trimmed_then_cleaned_up(self):
        # trimmed first, so U+FFFE after a space at its start is not
        # trimmed, and then made U+FFFD, as trimToNFC does (#403)
        wiki = FakeWiki(detached=True)
        item = wiki.wbi.item.new()
        item.labels.set("en", " \ufffeEugen\x01Löbig\uffff")
        item.aliases.set("en", ["Muckel\uffff", "Muckel\ufffd"])

        item.write()

        (kept,) = wiki.wbi.item.store.values()
        assert str(kept.labels.get("en")) == "\ufffdEugen Löbig\ufffd"
        assert [str(alias) for alias in kept.aliases.get("en")] == [
            "Muckel\ufffd"
        ]

    @pytest.mark.parametrize("where", ["value", "qualifier", "reference"])
    def test_a_string_value_is_stored_cleaned_up(self, where):
        # (#403) each character of WIKIBASE_CLEANED made U+FFFD, and the
        # value held
        text = f"Blatt{WIKIBASE_CLEANED.replace(chr(0x0B), '')}12"
        wiki, item = _item_holding(where, "string", text)

        item.write()

        (stored,) = wiki.wbi.item.store.values()
        cleaned = "Blatt" + "\ufffd" * (len(WIKIBASE_CLEANED) - 1) + "12"
        assert cleaned in _held_texts(stored)
        assert cleaned in _held_texts(wiki.wbi.item.get(stored.id))

    @pytest.mark.parametrize("where", ["value", "qualifier", "reference"])
    def test_a_monolingual_text_is_stored_as_sent_and_served_cleaned_up(
        self, where
    ):
        # Wikibase checks and stores a monolingual text as it is sent, and
        # its API cleans up every text it gives back (#403)
        text = "Blatt\x01 12\uffff"
        wiki, item = _item_holding(where, "monolingual", text)

        item.write()

        (stored,) = wiki.wbi.item.store.values()
        assert text in _held_texts(stored)
        served = _held_texts(wiki.wbi.item.get(stored.id))
        assert "Blatt\ufffd 12\ufffd" in served and text not in served


def _held_texts(item) -> list[str]:
    """The text of every string or monolingual text snak of `item`'s
    statements, as the item holds it."""
    texts = []
    for statements in item.get_json().get("claims", {}).values():
        for statement in statements:
            for snak in _snaks(statement):
                value = (snak.get("datavalue") or {}).get("value")
                if isinstance(value, dict) and "text" in value:
                    texts.append(value["text"])
                elif isinstance(value, str):
                    texts.append(value)
    return texts


# -- a claim's own text: refused, never changed -------------------------------

# a claim's own text by what gives it a shape the instance refuses -> how
# the text is made from the claim's words, and the fault its warning names
SHAPES = {
    "a line break inside": (
        lambda words: f"{words}\n{words}",
        "holds a line break",
    ),
    "a CR LF pair inside": (
        lambda words: f"{words}\r\n{words}",
        "holds a line break",
    ),
    "a line separator inside": (
        lambda words: f"{words}{LINE_SEPARATOR}{words}",
        "holds a line break",
    ),
    "a tab inside": (lambda words: f"{words}\t{words}", "holds a tab"),
    "a space at its start": (
        lambda words: f" {words}",
        "holds whitespace at its start",
    ),
    "a no-break space at its end": (
        lambda words: f"{words}{NO_BREAK_SPACE}",
        "holds whitespace at its end",
    ),
    "a Mongolian vowel separator at its start": (
        lambda words: f"{VOWEL_SEPARATOR}{words}",
        "holds whitespace at its start",
    ),
    "a line break and a tab, and a line break at its end": (
        lambda words: f"{words}\t{words}\n",
        "holds a line break, a tab and whitespace at its end",
    ),
}
# and a shape the instance holds, which is written as it is
HELD_SHAPES = {
    "a no-break space inside": lambda words: f"{words}{NO_BREAK_SPACE}{words}",
    "two spaces inside": lambda words: f"{words}  {words}",
    "a zero-width space at its end": lambda words: (
        f"{words}{ZERO_WIDTH_SPACE}"
    ),
}


class TestClaimsOfAShapeTheInstanceRefuses:
    """A claim whose own text holds a line break or a tab, or has
    whitespace at either end, is refused, and nothing of it is written; it
    is never changed (rule 5). The result names it by its claim id, a
    warning says why without quoting it, and every other claim of the batch
    is written, with its references (rules 1 and 2)."""

    @pytest.mark.parametrize("shape", SHAPES, ids=SHAPES)
    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    def test_the_claim_is_refused_and_the_rest_written(self, case, shape):
        shaped, fault = SHAPES[shape]
        refused = _saying(case, shaped(TEXT_OF[case]))
        wiki = FakeWiki(MEDIA, detached=True)
        # the refused claim comes first: the claim after it is written
        batch = _batch_of(refused, BIRTH)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.refused == [cid]
        assert written.warnings == [
            f"claim {cid} ({refused['property']}) is refused, and nothing "
            f"of it is written: {WHERE[case]} {fault}, which Wikibase "
            f"refuses or changes in a text value. A cut or any other change "
            f"would alter what the source says"
        ]
        assert _quotes(written.warnings, TEXT_OF[case]) == []
        # the certificate's birth is written, with both its references
        assert written.written == 1
        (born,) = _statements(
            wiki, written.created_items["e01"], "date_of_birth"
        )
        assert _sources(born) == [(["Q23"], []), ([], [written.run_qid])]
        # nothing of the refused claim, and no item only it mentions
        assert _with_properties(wiki, refused["property"]) == []
        for local_id in MENTIONS[case]:
            assert local_id not in written.created_items

    @pytest.mark.parametrize("shape", HELD_SHAPES, ids=HELD_SHAPES)
    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    def test_a_shape_the_instance_holds_is_written_as_it_is(self, case, shape):
        text = HELD_SHAPES[shape](TEXT_OF[case])
        claim = _saying(case, text)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(claim))

        assert (written.written, written.refused, written.warnings) == (
            1,
            [],
            [],
        )
        subject = written.created_items[claim["subject"]["local_id"]]
        (statement,) = _statements(wiki, subject, claim["property"])
        assert _values(statement) == [text]

    def test_a_text_over_the_limit_and_of_a_refused_shape(self):
        # the postcard's greeting as Klara's nickname, at length and over
        # two lines: one warning names both faults
        text = _said_over("Liebes Pünktchen! ", TEXT_LIMIT) + "\nDein Vater"
        batch = _batch_of(_saying("monolingual value", text))
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (warning,) = written.warnings
        assert warning == (
            f"claim {claim_id(batch.claims[0])} (nickname) is refused, and "
            f"nothing of it is written: its value has 411 characters in "
            f"Unicode NFC, over the 400 Wikibase holds in a text value; its "
            f"value holds a line break, which Wikibase refuses or changes in "
            f"a text value. A cut or any other change would alter what the "
            f"source says"
        )


# -- page numbers, regions and excerpts: on one line --------------------------

# a page number or excerpt as a claim gives it -> as the instance keeps it
ONE_LINE = {
    "a line break inside": ("Blatt 12,\nFlörburg", "Blatt 12, | Flörburg"),
    "a CR LF pair inside": ("Blatt 12,\r\nFlörburg", "Blatt 12, | Flörburg"),
    "a carriage return inside": (
        "Blatt 12,\rFlörburg",
        "Blatt 12, | Flörburg",
    ),
    "a line separator inside": (
        f"Blatt 12,{LINE_SEPARATOR}Flörburg",
        "Blatt 12, | Flörburg",
    ),
    "two line breaks inside": (
        "Blatt 12\n\nFlörburg",
        "Blatt 12 |  | Flörburg",
    ),
    "a tab inside": ("Blatt\t12", "Blatt 12"),
    "whitespace at either end": (
        f"{IDEOGRAPHIC_SPACE} Blatt 12{NO_BREAK_SPACE}\t",
        "Blatt 12",
    ),
    "a line break at either end": ("\nBlatt 12\r\n", "Blatt 12"),
    "a Mongolian vowel separator at its end": (
        f"Blatt 12{VOWEL_SEPARATOR}",
        "Blatt 12",
    ),
}


class TestLocatorsGoOnOneLine:
    """A page number, region or excerpt only places the claim in its
    source, or helps a reader check it there, so it goes on one line
    rather than failing the batch: whitespace at either end dropped, a
    line break inside made " | ", visibly, and a tab a space. A re-run
    finds the block."""

    @pytest.mark.parametrize("where", ["page number", "excerpt"])
    @pytest.mark.parametrize(
        ("given", "kept"), ONE_LINE.values(), ids=ONE_LINE
    )
    def test_a_locator_goes_on_one_line(self, given, kept, where):
        claim = _cause(given) if where == "page number" else _quoting(given)
        batch = _batch_of(claim)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, page, _, excerpt),) = _places(died)
        assert (page if where == "page number" else excerpt) == kept
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert wiki.wbi.item.writes == writes

    def test_a_region_goes_on_one_line(self, monkeypatch):
        # a region's form, as a percentage box gives it, holds no
        # whitespace; the rule applies to whatever form the region takes
        monkeypatch.setattr(
            RegionPercent,
            "as_wikibase_string",
            lambda region: "\t10%,20%,\n60%,10%\n",
        )
        claim = _cause()
        claim["source"]["region"] = {
            "x": 10,
            "y": 20,
            "width": 60,
            "height": 10,
        }
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(claim))

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, _, region, _),) = _places(died)
        assert region == "10%,20%, | 60%,10%"

    @pytest.mark.parametrize("where", ["page number", "excerpt"])
    def test_a_locator_of_whitespace_alone_is_left_out(self, where):
        blank = f" \t\n{NO_BREAK_SPACE}"
        claim = _cause(blank) if where == "page number" else _quoting(blank)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(claim))

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        excerpt = (
            "Todesursache: Wassersucht." if where == "page number" else None
        )
        # the block cites the register, and gives no page, or no excerpt
        assert _places(died) == [("Q34", None, None, excerpt)]

    @pytest.mark.parametrize("where", ["page number", "excerpt"])
    def test_places_apart_in_whitespace_alone_are_one(self, where):
        # the register's page given twice, the second time with a tab
        # before it and a line break after it: as the instance keeps them,
        # one place, and so one block (#401's review)
        first, second = "Blatt 12, Flörburg", "\tBlatt 12, Flörburg\n"
        claim = _cause if where == "page number" else _quoting
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(
            _batch_of(claim(first), claim(second))
        )

        assert (written.written, written.referenced, written.skipped) == (
            1,
            0,
            1,
        )
        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        assert len(_places(died)) == 1

    def test_a_long_locator_goes_on_one_line_before_it_is_cut(self):
        # (here) the leaves of the register's index that point to Eugen's
        # entry, one to a line: the page goes on one line, and is then cut,
        # with the digest of the page on one line
        lines = "\n".join(f"Blatt {leaf}" for leaf in range(6, 66))
        one_line = lines.replace("\n", " | ")
        assert len(one_line) > TEXT_LIMIT
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(_cause(lines)))

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, page, _, _),) = _places(died)
        assert page == _to_the_last_space(one_line)
        assert page.endswith(_mark(one_line))


# -- an entity's type hint: its whitespace as single spaces -------------------


class TestTypeHintSpacing:
    """A type hint is free text, which the writer compares ignoring its
    spacing: it goes with each run of whitespace in it made one space, and
    none at either end. One of whitespace alone is not recorded."""

    @pytest.mark.parametrize(
        ("given", "kept"),
        [
            (
                "Hutmacher und\nHutgeschäfte,\tMützenmacher ",
                "Hutmacher und Hutgeschäfte, Mützenmacher",
            ),
            (f"{NO_BREAK_SPACE}Hutgeschäft{LINE_SEPARATOR}", "Hutgeschäft"),
            (
                f"Hut{VOWEL_SEPARATOR}geschäft{VOWEL_SEPARATOR}",
                "Hut geschäft",
            ),
            (" \n", None),
        ],
        ids=[
            "over two lines",
            "whitespace at either end",
            "a Mongolian vowel separator inside and at its end",
            "whitespace alone",
        ],
    )
    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_a_type_hint_goes_with_single_spaces(self, found, given, kept):
        # (here) the directory's extraction gives the firm no kind, and
        # as its type hint the heading of the trade section it is listed
        # under, as the directory sets it
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        if found:
            BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_batch("unclassified_entity", type_hint=given)

        written = BatchWriter(wiki).write_batch(directory)

        firm = written.created_items["firm"]
        hints = set(_by_value(_statements(wiki, firm, "type_hint")))
        earlier = {"hat shop"} if found else set()
        assert hints == earlier | ({kept} if kept else set())
        assert written.warnings == []
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(directory)
        assert wiki.wbi.item.writes == writes


# -- labels, descriptions and aliases: as the instance keeps them -------------


def _named(batch: ClaimBatch, label: str, record: str | None = None):
    """`batch`, whose claims are about its one entity, with that entity's
    refs labelled `label` and its manifest record `record` (`label` when
    none)."""
    data = batch.model_dump(mode="json")
    for claim in data["claims"]:
        claim["subject"]["label"] = label
    (entity,) = data["entities"]
    entity["label"] = record or label
    return ClaimBatch.model_validate(data)


def _with_aliases(batch: ClaimBatch, aliases: list[str]) -> ClaimBatch:
    """`batch` with its one entity's manifest record giving `aliases`."""
    (entity,) = batch.entities
    return batch.model_copy(
        update={"entities": [entity.model_copy(update={"aliases": aliases})]}
    )


# the room a term's cut leaves before its mark
TERM_ROOM = TERM_LIMIT - len(_mark(""))
assert TERM_ROOM == 230


def _term_cut(term: str) -> str:
    """`term`, over the limit and its words each after a space, cut as the
    writer cuts it: at the last space the room holds or ends at, its mark
    after it, with the digest of the whole term."""
    return term[: term.rindex(" ", 0, TERM_ROOM + 1)].rstrip() + _mark(term)


# (here) the heading of the register's index, which an extraction takes into
# the label of each person the index names: two such labels agree well past
# the room, which ends inside the heading's "Beerdigte"
INDEX = _said_over(
    "Beerdigte der Gemeinde Flörburg im Jahre 1909, Blatt 12, ", 240
)
LONG_SEBASTIAN = f"{INDEX} {SEBASTIAN}"
LONG_EUGEN = f"{INDEX} Eugen Löbig"
assert LONG_SEBASTIAN[TERM_ROOM - 2 : TERM_ROOM + 3] == "Beerd"
assert len(LONG_EUGEN) > TERM_LIMIT
# a label as a batch gives it -> as the instance keeps it
LABELS = {
    "whitespace at either end": (
        f" {SEBASTIAN}{NO_BREAK_SPACE}",
        SEBASTIAN,
    ),
    "a line break inside": ("Sebastian\nLöbig", SEBASTIAN),
    "a CR LF pair inside": ("Sebastian\r\nLöbig", SEBASTIAN),
    "a tab inside": ("Sebastian\tLöbig", SEBASTIAN),
    "control characters inside": ("Sebastian\x0c\x1fLöbig", SEBASTIAN),
    "a line separator inside": (
        f"Sebastian{LINE_SEPARATOR}Löbig",
        SEBASTIAN,
    ),
    "a Mongolian vowel separator at its end": (
        f"{SEBASTIAN}{VOWEL_SEPARATOR}",
        SEBASTIAN,
    ),
    "over the limit": (LONG_SEBASTIAN, _term_cut(LONG_SEBASTIAN)),
    "in NFD, over the limit": (
        unicodedata.normalize("NFD", LONG_SEBASTIAN),
        _term_cut(LONG_SEBASTIAN),
    ),
    # trimmed before it is cut, and its digest of the label trimmed
    "over the limit, with whitespace around it": (
        f"\n{LONG_SEBASTIAN} ",
        _term_cut(LONG_SEBASTIAN),
    ),
}


class TestTerms:
    """A label, a description or an alias goes as the instance keeps it:
    trimmed, each run of control characters and each line or paragraph
    separator in it made one space, in NFC, and cut to 250 characters as a
    page number is, with the digest of the whole. So a label given in
    another form or at length finds its item again, and two labels, or two
    aliases, that agree up to the cut stay two."""

    @pytest.mark.parametrize(("given", "kept"), LABELS.values(), ids=LABELS)
    def test_a_label_goes_as_the_instance_keeps_it(self, given, kept):
        # the certificate's batch names Sebastian by `given`
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        batch = _named(_certificate_batch(), given)

        written = BatchWriter(wiki).write_batch(batch)

        sebastian = written.created_items["e01"]
        assert _labelled(wiki, kept) == [sebastian]
        assert _not_in_nfc(wiki) == []
        # a re-run finds him by that label, and writes nothing
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert again.created_items["e01"] == sebastian
        assert wiki.wbi.item.writes == writes
        # and so does the tree's batch, which names him as the instance
        # keeps his label
        tree = BatchWriter(wiki).write_batch(_named(_tree_batch(), kept))
        assert tree.created_items["e03"] == sebastian

    def test_a_label_of_whitespace_alone_is_no_label(self):
        # an item with no label could never be found again: as for a ref
        # that gives no label, the writer mints none
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        batch = _named(_certificate_batch(), f" \n{NO_BREAK_SPACE}")

        with pytest.raises(ValueError, match="no label, or none but"):
            BatchWriter(wiki).write_batch(batch)

        assert _labelled(wiki, "") == []

    def test_labels_that_agree_up_to_the_cut_name_two_items(self):
        # (here) Eugen's cause of death and his daughter-in-law's kinship,
        # each subject labelled with the index's heading
        assert _term_cut(LONG_EUGEN)[:-12] == _term_cut(LONG_SEBASTIAN)[:-12]
        kinship = dict(
            TEXT_CLAIMS["string qualifier"],
            subject={
                "kind": "local",
                "local_id": "e06",
                "label": LONG_SEBASTIAN,
                "instance_of": "human",
            },
        )
        batch = _batch_of(_cause(label=LONG_EUGEN), kinship)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        eugen, other = (
            written.created_items["e07"],
            written.created_items["e06"],
        )
        assert eugen != other
        assert _labelled(wiki, _term_cut(LONG_EUGEN)) == [eugen]
        assert _labelled(wiki, _term_cut(LONG_SEBASTIAN)) == [other]
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(batch)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("length", [TERM_LIMIT, TERM_LIMIT + 1])
    def test_a_description_over_the_limit_is_cut(self, length):
        # (here) Eugen's summary, said over and over: at the limit it is
        # his description whole; one character more, and it is cut inside
        # "Hutmacherei", which the cut leaves out whole
        summary = _said_over(
            "Master hatter of Flörburg, of the Hutmacherei zum Blauen Hut, ",
            length,
        )
        batch = _certificate_batch()
        (record,) = batch.entities
        batch = batch.model_copy(
            update={
                "entities": [record.model_copy(update={"summary": summary})]
            }
        )
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        item = wiki.wbi.item.get(written.created_items["e01"])
        description = str(item.descriptions.get("en"))
        if length > TERM_LIMIT:
            assert summary[TERM_ROOM - 1 : TERM_ROOM + 1] == "ei"
            assert description == _term_cut(summary)
        else:
            assert description == summary

    @pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
    def test_a_description_that_is_the_label_is_left_out(self, dry_run):
        # (here) the certificate's extraction gives Sebastian a summary of
        # his name alone, a space after it: as the instance keeps them, his
        # label and description would be the same, which it refuses. His
        # item is made without a description, and a warning says so, in a
        # dry run too (#401's review)
        batch = _certificate_batch()
        (record,) = batch.entities
        batch = batch.model_copy(
            update={
                "entities": [
                    record.model_copy(update={"summary": f"{SEBASTIAN} "})
                ]
            }
        )
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        written = BatchWriter(wiki, dry_run=dry_run).write_batch(batch)

        assert written.warnings == [
            "entity 'e01' has a description that is its label: its item is "
            "made without one, as Wikibase refuses an item whose label and "
            "description are the same"
        ]
        if dry_run:
            assert wiki.wbi.item.writes == 0
            return
        sebastian = written.created_items["e01"]
        item = wiki.wbi.item.get(sebastian)
        assert (str(item.labels.get("en")), item.descriptions.get("en")) == (
            SEBASTIAN,
            None,
        )
        # a re-run finds him, warns of nothing and writes nothing
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.created_items["e01"], again.warnings) == (sebastian, [])
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_aliases_go_as_the_instance_keeps_them(self, found):
        # (here) the tree gives Sebastian's pet name as a transcription
        # can: with a line break after it, with spaces around it, as a
        # blank, and twice at length, under two headings that agree past
        # the room
        aliases = [
            "Wastl\n",
            " Wastl ",
            " \t",
            f"{INDEX} Wastl, the hatter",
            f"{INDEX} Sebastian 'Wastl'",
        ]
        assert min(map(len, aliases[3:])) > TERM_LIMIT
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        if found:
            BatchWriter(wiki).write_batch(_certificate_batch())
        tree = _with_aliases(_tree_batch(), aliases)

        written = BatchWriter(wiki).write_batch(tree)

        sebastian = written.created_items["e03"]
        assert sorted(_aliases(wiki, sebastian)) == sorted(
            [
                "Wastl",
                _term_cut(f"{INDEX} Wastl, the hatter"),
                _term_cut(f"{INDEX} Sebastian 'Wastl'"),
            ]
        )
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(tree)
        assert wiki.wbi.item.writes == writes

    def test_a_run_label_over_the_limit_is_cut_and_found_again(self):
        # (here) a local model whose identifier names its whole setup: two
        # batches it wrote each have a run item of their own, and a re-run
        # finds its batch's
        model = _said_over(
            "fake-model für Kurrent, Flörburger Register, ", 260
        )
        run = dict(RUN, model_identifier=model)
        first = _batch_of(_cause(), run=run)
        second = _batch_of(_cause(cause="Herzschwäche"), run=run)
        label = f"extraction run 2026-07-12 {model} {batch_fingerprint(first)}"
        assert label[TERM_ROOM - 1 : TERM_ROOM + 1] == " F"
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(first)
        other = BatchWriter(wiki).write_batch(second)

        assert _labelled(wiki, _term_cut(label)) == [written.run_qid]
        assert other.run_qid != written.run_qid
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(first)
        assert again.run_qid == written.run_qid
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize(
        "given",
        [f"  {SEBASTIAN}\n", LONG_SEBASTIAN],
        ids=["with whitespace around it", "over the limit"],
    )
    def test_a_record_reaches_the_item_the_resolve_step_found(self, given):
        # the certificate's batch mints Sebastian under `given`; the tree's
        # refs to him give his label as the instance keeps it, and the
        # resolve step matched them to his item, while its manifest record
        # gives `given`. The record's kind and alias reach the item
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        certificate = BatchWriter(wiki).write_batch(
            _named(_certificate_batch(), given)
        )
        sebastian = certificate.created_items["e01"]
        (kept,) = [
            label
            for label in (SEBASTIAN, _term_cut(LONG_SEBASTIAN))
            if _labelled(wiki, label)
        ]
        tree = _named(_tree_batch(), kept, record=given)

        written = BatchWriter(wiki).write_batch(
            resolve_batch(tree, {kept: sebastian})
        )

        assert _aliases(wiki, sebastian) == ["Wastl"]
        (kind,) = _statements(wiki, sebastian, "instance_of")
        assert kind.mainsnak.datavalue["value"]["id"] == ITEMS["human"]
        assert _sources(kind)[2:] == [(["Q29"], []), ([], [written.run_qid])]

    def test_a_record_with_no_label_the_instance_keeps_reaches_no_item(self):
        # (here) the register's extraction lists in its manifest an entity
        # whose label is whitespace alone, of the kind human and with the
        # alias "Muckel", and makes no claim of it; the batch's one claim is
        # about Flörburg, an item with a German label and no English one.
        # A label with nothing the instance keeps names no item: the kind
        # and the alias reach none, least of all Flörburg (#401's review)
        wiki = FakeWiki(MEDIA, detached=True)
        town = wiki.wbi.item.new()
        town.labels.set("de", "Flörburg")
        town = town.write()
        keyed = dict(
            TEXT_CLAIMS["external id"],
            subject={"kind": "qid", "qid": town.id},
        )
        blank = {
            "id": "blank",
            "label": " \n",
            "kind": "human",
            "aliases": ["Muckel"],
            "extract": False,
        }

        written = BatchWriter(wiki).write_batch(
            _batch_of(keyed, entities=(blank,))
        )

        assert written.written == 1
        assert _aliases(wiki, town.id) == []
        assert _statements(wiki, town.id, "instance_of") == []
        assert (
            written.type_statements_written,
            written.type_statements_referenced,
        ) == (0, 0)


# -- every character of the instance's classes --------------------------------

# each character of the fake's own classes (test_pipeline.py), written out
# apart from the writer's, and each control character (#401's review)
SPACES = {f"U+{ord(char):04X}": char for char in WIKIBASE_SPACE}
BREAKS = {f"U+{ord(char):04X}": char for char in WIKIBASE_BREAK_OR_TAB}
CONTROLS = {
    f"U+{code:04X}": chr(code) for code in (*range(0x20), *range(0x7F, 0xA0))
}
SHAPED = {**SPACES, **CONTROLS}
# each character Wikibase cleans up into U+FFFD, as the fake writes them out
# (#403)
CLEANED = {f"U+{ord(char):04X}": char for char in WIKIBASE_CLEANED}


class TestEveryCharacterOfTheInstancesClasses:
    """Each character Wikibase counts as whitespace, or as a line break or
    tab, as the fake writes its classes out, each control character, and
    each character Wikibase cleans up into U+FFFD, taken through a claim, a
    page number, an excerpt, a type hint and a label: none stops the batch,
    and each re-run finds what the first wrote."""

    @pytest.mark.parametrize("at", ["start", "end"])
    @pytest.mark.parametrize("char", SPACES.values(), ids=SPACES)
    def test_whitespace_at_either_end_of_a_claim_refuses_it(self, char, at):
        words = TEXT_OF["monolingual value"]
        text = char + words if at == "start" else words + char
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(
            _batch_of(_saying("monolingual value", text), BIRTH)
        )

        assert (written.written, len(written.refused)) == (1, 1)
        (warning,) = written.warnings
        assert f"whitespace at its {at}" in warning

    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    @pytest.mark.parametrize("char", BREAKS.values(), ids=BREAKS)
    def test_a_line_break_or_tab_inside_a_claim_refuses_it(self, char, case):
        # in a string value, the instance would change a vertical tab or a
        # form feed rather than refuse it: the writer refuses the claim all
        # the same, as a change would alter what the source says
        words = TEXT_OF[case]
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(
            _batch_of(_saying(case, words + char + words), BIRTH)
        )

        assert (written.written, len(written.refused)) == (1, 1)
        (warning,) = written.warnings
        assert ("a tab" if char == "\t" else "a line break") in warning

    @pytest.mark.parametrize("where", ["page number", "excerpt"])
    @pytest.mark.parametrize("char", SPACES.values(), ids=SPACES)
    def test_a_locator_of_any_whitespace_goes_on_one_line(self, char, where):
        given = f"{char}Blatt 12{char}Flörburg{char}"
        if char == "\t":
            inside = " "
        elif char in WIKIBASE_BREAK_OR_TAB:
            inside = " | "
        else:
            # in NFC, which makes U+2000 and U+2001 the spaces U+2002 and
            # U+2003
            inside = unicodedata.normalize("NFC", char)
        claim = _cause(given) if where == "page number" else _quoting(given)
        batch = _batch_of(claim)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, page, _, excerpt),) = _places(died)
        kept = page if where == "page number" else excerpt
        assert kept == f"Blatt 12{inside}Flörburg"
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("char", SHAPED.values(), ids=SHAPED)
    def test_a_label_of_any_shape_finds_its_item_again(self, char):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        batch = _named(
            _certificate_batch(), f"{char}Sebastian{char}Löbig{char}"
        )

        written = BatchWriter(wiki).write_batch(batch)

        sebastian = written.created_items["e01"]
        label = str(wiki.wbi.item.get(sebastian).labels.get("en"))
        assert label.startswith("Sebastian") and label.endswith("Löbig")
        assert _labelled(wiki, label) == [sebastian]
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert again.created_items["e01"] == sebastian
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    @pytest.mark.parametrize("char", CLEANED.values(), ids=CLEANED)
    def test_a_cleaned_up_character_in_a_claim_refuses_it(self, char, case):
        # (#403) a vertical tab or a form feed is a line break
        words = TEXT_OF[case]
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(
            _batch_of(_saying(case, words + char + words), BIRTH)
        )

        assert (written.written, len(written.refused)) == (1, 1)
        (warning,) = written.warnings
        if char in WIKIBASE_BREAK_OR_TAB:
            assert "holds a line break," in warning
            assert "U+FFFD" not in warning
        else:
            assert "holds a control character or a noncharacter" in warning

    @pytest.mark.parametrize("where", ["page number", "excerpt"])
    @pytest.mark.parametrize("char", CLEANED.values(), ids=CLEANED)
    def test_a_cleaned_up_character_in_a_locator_is_sent_so(self, char, where):
        # (#403) made U+FFFD, as the instance gives it back; a vertical tab
        # or a form feed is a line break
        given = f"{char}Blatt 12{char}Flörburg{char}"
        inside = " | " if char in WIKIBASE_BREAK_OR_TAB else "\ufffd"
        edge = "" if char in WIKIBASE_BREAK_OR_TAB else "\ufffd"
        claim = _cause(given) if where == "page number" else _quoting(given)
        batch = _batch_of(claim)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, page, _, excerpt),) = _places(died)
        kept = page if where == "page number" else excerpt
        assert kept == f"{edge}Blatt 12{inside}Flörburg{edge}"
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("char", CLEANED.values(), ids=CLEANED)
    def test_a_cleaned_up_character_in_a_type_hint_is_sent_so(self, char):
        # (#403) the firm found by the directory's batch: its hint made
        # U+FFFD, or a space for a vertical tab or a form feed
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_batch(
            "unclassified_entity", type_hint=f"Hut{char}geschäft"
        )

        written = BatchWriter(wiki).write_batch(directory)

        inside = " " if char in WIKIBASE_BREAK_OR_TAB else "\ufffd"
        hints = set(
            _by_value(
                _statements(wiki, written.created_items["firm"], "type_hint")
            )
        )
        assert hints == {"hat shop", f"Hut{inside}geschäft"}
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(directory)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("char", CLEANED.values(), ids=CLEANED)
    def test_a_label_with_a_cleaned_up_character_finds_its_item(self, char):
        # (#403) a control character is a space by the time Wikibase cleans
        # a term up; U+FFFE and U+FFFF are made U+FFFD
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        batch = _named(_certificate_batch(), f"Sebastian{char}Löbig{char}")

        written = BatchWriter(wiki).write_batch(batch)

        sebastian = written.created_items["e01"]
        if char in "\ufffe\uffff":
            kept = "Sebastian\ufffdLöbig\ufffd"
        else:
            kept = SEBASTIAN
        assert _labelled(wiki, kept) == [sebastian]
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert again.created_items["e01"] == sebastian
        assert wiki.wbi.item.writes == writes
