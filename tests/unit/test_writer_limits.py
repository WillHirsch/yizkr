"""Offline tests of the text limit the writer keeps to (#366; `TEXT_LIMIT`
in yizkr.pipeline.writer).

Wikibase holds at most 400 characters in the text of a string,
external-id or monolingual text value, whether the value is a
statement's, a qualifier's or a reference's, and refuses the whole edit
that sends a longer one. The unit fake refuses such an edit as the
instance does (`_refuse_over_limit` in test_pipeline.py), so a writer
that sent one would fail the batch here as it does on the instance. The
writer counts each text in NFC, the form the instance keeps it in, and
sends none over the limit:

* a reference's page number or region over the limit is cut at a break
  between words and ends with " … sha1 " and 12 hex digits of the SHA-1
  of the whole text, so a re-run finds the block, and two places whose
  locators agree up to the cut stay two blocks (#381); an excerpt over the
  limit is cut at a space and ends with "…";
* a claim whose own text, its value or a qualifier's, is over the limit
  is refused, never cut (CLAUDE.md rule 5): nothing of it is written,
  the result names it by its review-sheet id, and a warning says why,
  quoting none of its text. Every other claim is written, with its
  references (rules 1 and 2), and a kind only a refused claim gives is
  neither recorded nor warned of;
* an entity's type hint over the limit is left out, with a warning.

Every text comes from the fictional universe (docs/EXAMPLES.md), said
over and over where a test needs it long.
"""

from __future__ import annotations

import copy
import hashlib
import unicodedata

import pytest
from wikibaseintegrator.wbi_exceptions import ModificationFailed

from tests.unit.test_pipeline import (
    DIRECTORY_SHA1,
    INTERVIEW_SHA1,
    ITEMS,
    MEDIA_SHA1,
    PROPERTIES,
    REGISTER_SHA1,
    TEXT_CLAIMS,
    TEXT_MEDIA,
    FakeWiki,
    _batch_of,
    _by_value,
    _certificate_batch,
    _cites,
    _directory_batch,
    _firm_batch,
    _local,
    _not_in_nfc,
    _places,
    _snapshot,
    _sources,
    _statements,
    _values,
)
from yizkr.claims import RegionPercent
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import CUT_MARK, TEXT_LIMIT, BatchWriter

MEDIA = {MEDIA_SHA1: "Q23", **TEXT_MEDIA}


def _said_over(phrase: str, length: int) -> str:
    """`phrase` said over and over, `length` characters long in NFC, the
    last of them a full stop."""
    text = (phrase * (length // len(phrase) + 1))[: length - 1] + "."
    assert len(unicodedata.normalize("NFC", text)) == length
    return text


def _quotes(warnings: list[str], *texts: str) -> list[str]:
    """The warnings that quote any of `texts`."""
    return [
        warning
        for warning in warnings
        if any(text.strip(" ,.!-") in warning for text in texts)
    ]


# -- the fake -----------------------------------------------------------------


def _item_holding(where: str, kind: str, text: str):
    """A new item of the fake's with one statement that holds `text` as a
    `kind` ("string", "monolingual") value `where` it says: as the
    statement's value, as a qualifier's, or in its reference."""
    from wikibaseintegrator.datatypes import Item, MonolingualText, String
    from wikibaseintegrator.models import Reference, References

    wiki = FakeWiki(detached=True)

    def holding(key: str):
        if kind == "string":
            return String(prop_nr=PROPERTIES[key], value=text)
        return MonolingualText(
            prop_nr=PROPERTIES[key], text=text, language="de"
        )

    item = wiki.wbi.item.new()
    if where == "value":
        statement = holding("kinship_type" if kind == "string" else "nickname")
    else:
        statement = Item(prop_nr=PROPERTIES["relative"], value="Q1")
    if where == "qualifier":
        statement.qualifiers.add(
            holding("kinship_type" if kind == "string" else "nickname")
        )
    block = Reference()
    block.add(Item(prop_nr=PROPERTIES["stated_in"], value="Q23"))
    if where == "reference":
        block.add(
            holding(
                "page_number" if kind == "string" else "transcription_excerpt"
            )
        )
    references = References()
    references.add(block)
    statement.references = references
    item.claims.add(statement)
    return wiki, item


class TestTheFakeHoldsWhatWikibaseHolds:
    """The fake refuses an edit as the instance does: whole, when a snak
    of it holds a text over 400 characters, a string value counted in NFC
    and a monolingual text as it is sent (_refuse_over_limit)."""

    @pytest.mark.parametrize("length", [400, 401])
    @pytest.mark.parametrize("kind", ["string", "monolingual"])
    @pytest.mark.parametrize("where", ["value", "qualifier", "reference"])
    def test_an_edit_over_the_limit_is_refused_whole(
        self, where, kind, length
    ):
        text = _said_over("Liebes Pünktchen! ", length)
        wiki, item = _item_holding(where, kind, text)

        if length > 400:
            with pytest.raises(ModificationFailed, match="400 characters"):
                item.write()
            assert (wiki.wbi.item.writes, wiki.wbi.item.store) == (0, {})
        else:
            item.write()
            assert wiki.wbi.item.writes == 1

    def test_a_string_is_counted_in_nfc_and_a_monolingual_text_as_sent(
        self,
    ):
        # 400 characters in NFC, more as the batch gives it (NFD)
        text = unicodedata.normalize(
            "NFD", _said_over("Liebes Pünktchen! ", 400)
        )
        assert len(text) > 400
        wiki, item = _item_holding("value", "string", text)
        item.write()
        assert wiki.wbi.item.writes == 1
        wiki, item = _item_holding("value", "monolingual", text)
        with pytest.raises(ModificationFailed):
            item.write()


# -- page numbers and regions: cut --------------------------------------------

# (here) the burial register's entry for Eugen, which an extraction places
# on each leaf of the register's index that points to it, and the address
# as the register writes it, a bar over its n doubling it
STREET = "Brun\u0304enring"


def _leaves(first: int, count: int, street: str = "") -> str:
    return " ; ".join(
        f"Blatt {leaf}{street}" for leaf in range(first, first + count)
    )


def _cause(
    page: str | None = None, cause: str = "Wassersucht", kind: str = "human"
) -> dict:
    """Eugen's cause of death as the 1909 burial register gives it, on
    `page`, his ref giving him the kind `kind`."""
    source = _cites(REGISTER_SHA1, "Todesursache: Wassersucht.")
    if page:
        source["page_number"] = page
    return {
        "subject": _local("e07", "Eugen Löbig", kind),
        "property": "cause_of_death",
        "value": {
            "type": "monolingual",
            "value": {"text": cause, "language": "de"},
        },
        "source": source,
    }


def _stored_page(wiki: FakeWiki, qid: str) -> str:
    """The page number of the one source block of the item's cause of
    death, as the instance keeps it."""
    (died,) = _statements(wiki, qid, "cause_of_death")
    ((_, page, _, _),) = _places(died)
    return page


def _mark(page: str) -> str:
    """What a page over the limit is cut with: CUT_MARK, then "sha1" and
    the first 12 hex digits of the SHA-1 of the whole page in NFC."""
    whole = unicodedata.normalize("NFC", page).encode()
    return f"{CUT_MARK} sha1 {hashlib.sha1(whole).hexdigest()[:12]}"


# the room a cut leaves before its mark
ROOM = TEXT_LIMIT - len(_mark(""))
assert ROOM == 380


def _to_the_last_space(page: str) -> str:
    """`page`, over the limit, cut as the writer cuts a page whose words,
    as far as the room, each begin after a space: at the last space the
    room holds or ends at, its mark after it."""
    return page[: page.rindex(" ", 0, ROOM + 1)].rstrip() + _mark(page)


# a page number by what sets it apart -> (the page as a claim gives it,
# the page the instance keeps)
PAGES = {}
# at the limit, it is kept whole; one character more and it is cut
AT = _leaves(6, 60)[:400]
assert len(AT) == 400 and AT[-1].isdigit()
PAGES["400 characters"] = (AT, AT)
PAGES["401 characters"] = (AT + "0", _to_the_last_space(AT + "0"))
# the room ends at a space: the page keeps all of the room, its mark after
# it
BREAK = _leaves(8, 60)
assert BREAK[ROOM] == " " and BREAK[ROOM - 1].isdigit()
PAGES["the room ends at a space"] = (BREAK, BREAK[:ROOM] + _mark(BREAK))
# the room ends inside leaf 109: a cut there would keep "Blatt 10", another
# leaf, so the cut leaves it out whole
NUMBER = _leaves(76, 60)
assert NUMBER[ROOM - 3 : ROOM + 1] == " 109"
PAGES["the room ends inside a number"] = (
    NUMBER,
    NUMBER[: ROOM - 3] + _mark(NUMBER),
)
# the room ends just past the point of leaf 1.000, as German writes a
# thousand: a cut there would keep "Blatt 1.", leaf 1, so the cut leaves
# the number out whole
POINT = " ; ".join(
    f"Blatt {leaf:,}".replace(",", ".") for leaf in range(969, 1014)
)
assert POINT[ROOM - 3 : ROOM + 3] == " 1.000"
PAGES["the room ends at a point inside a number"] = (
    POINT,
    POINT[: ROOM - 3] + _mark(POINT),
)
# (here) an index in English, which writes a thousand with a comma: the
# room ends just past the comma of page 1,003
COMMA = " ; ".join(f"p. {page:,}" for page in range(962, 1022))
assert COMMA[ROOM - 3 : ROOM + 3] == " 1,003"
PAGES["the room ends at a comma inside a number"] = (
    COMMA,
    COMMA[: ROOM - 3] + _mark(COMMA),
)
# (here) the sheets of the tree Clara drew, each marked as her copy: the
# room ends at the apostrophe of "Clara's", written either way, so the cut
# leaves the word out whole
for apostrophe in ("'", "\u2019"):
    CLARA = " ; ".join(
        f"sheet {sheet}, Clara{apostrophe}s copy" for sheet in range(100, 130)
    )
    assert CLARA[ROOM - 6 : ROOM + 1] == f" Clara{apostrophe}"
    PAGES[f"the room ends at an apostrophe ({apostrophe})"] = (
        CLARA,
        CLARA[: ROOM - 6] + _mark(CLARA),
    )
# the room ends between the register's n and its bar: the cut leaves the
# street's name out whole, the letter with its mark
MARK = _leaves(90, 40, f", {STREET}")
assert MARK[ROOM - 5 : ROOM + 1] == f" {STREET[:5]}"
PAGES["the room ends at a combining mark"] = (
    MARK,
    MARK[: ROOM - 5] + _mark(MARK),
)
# (here) a transcription that gives a stray diaeresis on a semicolon: the
# room ends between the two, which the cut keeps together, so both go
STRAY = _leaves(6, 60)
assert STRAY[ROOM - 2 : ROOM] == " ;"
STRAY = STRAY[:ROOM] + "\u0308" + STRAY[ROOM:]
PAGES["the room ends at a stray mark on a semicolon"] = (
    STRAY,
    STRAY[: ROOM - 2] + _mark(STRAY),
)
# (here) a register that circles its leaf numbers, which the transcription
# gives with a combining enclosing circle: the room ends between leaf 41
# and its circle, which the cut keeps together, so both go
CIRCLE = " ; ".join(f"Blatt {leaf}\u20dd" for leaf in range(10, 70))
assert CIRCLE[ROOM - 3 : ROOM + 1] == " 41\u20dd"
PAGES["the room ends at an enclosing mark"] = (
    CIRCLE,
    CIRCLE[: ROOM - 3] + _mark(CIRCLE),
)
# (here) a typeset index, which binds "S." to its page with a no-break
# space: the room ends inside page 50, which the cut leaves out whole, and
# the no-break space before it goes too
BOUND = " ; ".join(f"S.\u00a0{page}" for page in range(2, 72))
assert BOUND[ROOM - 4 : ROOM + 1] == "S.\u00a050"
PAGES["a no-break space before the cut"] = (
    BOUND,
    BOUND[: ROOM - 2] + _mark(BOUND),
)
# one word fills the room: it is cut, and the letter whose bar the room
# leaves out goes with its bar
WORD = "Hauptregister" + STREET * 40
assert WORD[ROOM - 1 : ROOM + 1] == "n\u0304"
PAGES["one word fills the room"] = (WORD, WORD[: ROOM - 1] + _mark(WORD))
# given decomposed (NFD), the page is longer than the limit; in NFC, the
# form the instance keeps and counts, it is at the limit
UMLAUTS = _said_over("Blatt 5, Flörburg ; ", 400)
assert len(unicodedata.normalize("NFD", UMLAUTS)) > 400
PAGES["in NFD, NFC at the limit"] = (
    unicodedata.normalize("NFD", UMLAUTS),
    UMLAUTS,
)
# given in NFD and over the limit even in NFC: it is cut in NFC, the form
# the instance keeps, and its digest is of the whole page in NFC. The room
# ends inside leaf 23, which the cut leaves out whole
TOWN = _leaves(5, 40, ", Flörburg")
assert TOWN[ROOM - 2 : ROOM + 2] == " 23,"
PAGES["in NFD, over the limit in NFC"] = (
    unicodedata.normalize("NFD", TOWN),
    TOWN[: ROOM - 2] + _mark(TOWN),
)


class TestLocatorsOverTheLimit:
    """A page number or region over the limit is cut, visibly, at a break
    between words, never failing the batch. The cut depends on the page
    alone, so a re-run finds the block and writes nothing, and its digest
    keeps two places whose pages agree up to the cut two blocks."""

    @pytest.mark.parametrize(("page", "stored"), PAGES.values(), ids=PAGES)
    def test_a_page_over_the_limit_is_cut(self, page, stored):
        assert len(stored) <= TEXT_LIMIT
        wiki = FakeWiki(MEDIA, detached=True)
        batch = _batch_of(_cause(page))

        written = BatchWriter(wiki).write_batch(batch)

        eugen = written.created_items["e07"]
        assert _stored_page(wiki, eugen) == stored
        assert (written.written, written.refused) == (1, [])
        assert _not_in_nfc(wiki) == []
        # a re-run builds the block the instance keeps, and adds nothing
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert wiki.wbi.item.writes == writes

    def test_places_that_agree_up_to_the_cut_stay_apart(self):
        # (here) the register's index gives Eugen's entry on two lists of
        # leaves that agree well past the room, one ending with leaf 500
        # and one with leaf 777; and a third claim gives as its page what
        # the first one's cut keeps, with the mark but without the digest.
        # Each is a place of its own, and so a reference block of its own
        common = _leaves(1, 60)
        assert len(common) > TEXT_LIMIT
        first, second = common + " ; Blatt 500", common + " ; Blatt 777"
        start = _to_the_last_space(first)[: -len(_mark(first))] + CUT_MARK
        wiki = FakeWiki(MEDIA, detached=True)
        batch = _batch_of(_cause(first), _cause(second), _cause(start))

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        assert [page for _, page, _, _ in _places(died)] == [
            _to_the_last_space(first),
            _to_the_last_space(second),
            start,
        ]
        assert (written.written, written.referenced, written.skipped) == (
            1,
            2,
            0,
        )
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 3)
        assert wiki.wbi.item.writes == writes

    def test_a_region_over_the_limit_is_cut(self, monkeypatch):
        # a region's form, as a percentage box gives it, is far shorter;
        # the cut applies to whatever form the region takes. The room ends
        # just past the comma after a box's third percentage
        form = " ".join(["10%,20%,60%,10%"] * 30)
        assert form[ROOM - 2 : ROOM + 1] == "%,1"
        monkeypatch.setattr(
            RegionPercent, "as_wikibase_string", lambda region: form
        )
        claim = _cause()
        claim["source"]["region"] = {
            "x": 10,
            "y": 20,
            "width": 60,
            "height": 10,
        }
        wiki = FakeWiki(MEDIA, detached=True)
        batch = _batch_of(claim)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, _, region, _),) = _places(died)
        assert region == form[:ROOM] + _mark(form)
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.skipped) == (0, 1)
        assert wiki.wbi.item.writes == writes

    def test_an_excerpt_over_the_limit_is_cut_at_a_space(self):
        # (here) the register's entry quoted whole, over and over, as the
        # excerpt: it is cut at the last space in its first 397 characters,
        # which falls inside a word, and ends with "…"
        excerpt = _said_over("Gestorben: 16. März 1909 in Flörburg. ", 450)
        assert excerpt[396] != " " and excerpt[397] != " "
        kept = excerpt[: excerpt.rindex(" ", 0, 397)]
        claim = _cause()
        claim["source"]["transcription_excerpt"] = {
            "text": excerpt,
            "language": "de",
        }
        wiki = FakeWiki(MEDIA, detached=True)
        batch = _batch_of(claim)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, _, _, stored),) = _places(died)
        assert stored == kept + "…"
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.skipped) == (0, 1)
        assert wiki.wbi.item.writes == writes


# -- a claim's own text: refused, never cut -----------------------------------

# a claim whose own text an extraction gave at length, by where that text
# is -> the words it is made of: (here) the postcard's greeting taken whole
# as Klara's nickname, the compensation file's words for Hildegard taken
# whole as her kinship, and a gazetteer's key taken with the town's name
# over and over
LONG_TEXTS = {
    "monolingual value": "Liebes Pünktchen! ",
    "string qualifier": "Schwägerin der Verstorbenen, ",
    "external id": "flörburg-",
}


def _saying(case: str, text: str) -> dict:
    """TEXT_CLAIMS' claim of `case` with `text` as its own text."""
    claim = copy.deepcopy(TEXT_CLAIMS[case])
    if case == "monolingual value":
        claim["value"]["value"]["text"] = text
    elif case == "string qualifier":
        (qualifier,) = claim["qualifiers"]
        qualifier["value"]["value"] = text
    else:
        claim["value"]["value"] = text
    return claim


# the local ids of the entities each claim mentions, which no other claim
# of the tests' batches mentions
MENTIONS = {
    "monolingual value": ["e02"],
    "string qualifier": ["e06", "e05"],
    "external id": ["pl1"],
}
WHERE = {
    "monolingual value": "its value",
    "string qualifier": "its kinship_type qualifier",
    "external id": "its value",
}
BIRTH = _certificate_batch().model_dump(mode="json")["claims"][0]


def _with_properties(wiki: FakeWiki, key: str) -> list[str]:
    """The items of the fake's that hold a statement on `key`."""
    return [
        qid
        for qid, item in wiki.wbi.item.store.items()
        if item.claims.get(PROPERTIES[key])
    ]


class TestClaimsOverTheLimit:
    """A claim whose own text is over the limit is refused, and nothing of
    it is written, not even an item only it mentions; it is never cut
    (rule 5). The result names it by its review-sheet id, a warning says
    why without quoting it, and every other claim of the batch is written,
    with its references (rules 1 and 2)."""

    @pytest.mark.parametrize("case", LONG_TEXTS, ids=LONG_TEXTS)
    def test_a_claim_over_the_limit_is_refused_and_the_rest_written(
        self, case
    ):
        text = _said_over(LONG_TEXTS[case], TEXT_LIMIT + 1)
        refused = _saying(case, text)
        wiki = FakeWiki(MEDIA, detached=True)
        # the refused claim comes first: the claim after it is written
        batch = _batch_of(refused, BIRTH)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.refused == [cid]
        (warning,) = written.warnings
        assert warning.startswith(
            f"claim {cid} ({refused['property']}) is refused, and nothing "
            f"of it is written: {WHERE[case]} has 401 characters"
        )
        assert "400" in warning
        # a warning says what, never quotes it
        assert _quotes(written.warnings, LONG_TEXTS[case]) == []
        # the certificate's birth is written, with both its references
        assert written.written == 1
        (born,) = _statements(
            wiki, written.created_items["e01"], "date_of_birth"
        )
        assert _sources(born) == [(["Q23"], []), ([], [written.run_qid])]
        # nothing of the refused claim: no statement, cut or whole, and no
        # item only it mentions
        assert _with_properties(wiki, refused["property"]) == []
        for local_id in MENTIONS[case]:
            assert local_id not in written.created_items

    @pytest.mark.parametrize("case", LONG_TEXTS, ids=LONG_TEXTS)
    @pytest.mark.parametrize(
        "given",
        ["400 characters", "401 characters", "in NFD, NFC at the limit"],
    )
    def test_the_limit_is_counted_in_nfc(self, case, given):
        length = TEXT_LIMIT + (given == "401 characters")
        text = _said_over(LONG_TEXTS[case], length)
        if given.startswith("in NFD"):
            decomposed = unicodedata.normalize("NFD", text)
            assert len(decomposed) > TEXT_LIMIT
            claim = _saying(case, decomposed)
        else:
            claim = _saying(case, text)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(claim))

        if length > TEXT_LIMIT:
            assert (written.written, len(written.refused)) == (0, 1)
            return
        # at the limit in NFC, the claim is written whole, in NFC
        assert (written.written, written.refused) == (1, [])
        subject = written.created_items[claim["subject"]["local_id"]]
        (statement,) = _statements(wiki, subject, claim["property"])
        assert _values(statement) == [text]

    def test_a_later_qualifier_over_the_limit_refuses_the_claim(self):
        # (here) the compensation file dates Hildegard's kinship to its own
        # year, then words the kinship at length: the second qualifier is
        # over the limit
        claim = copy.deepcopy(TEXT_CLAIMS["string qualifier"])
        kinship = _said_over(LONG_TEXTS["string qualifier"], 401)
        claim["qualifiers"] = [
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1953},
            },
            {
                "property": "kinship_type",
                "value": {"type": "string", "value": kinship},
            },
        ]
        batch = _batch_of(claim, BIRTH)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.refused == [claim_id(batch.claims[0])]
        (warning,) = written.warnings
        assert "its kinship_type qualifier has 401 characters" in warning
        assert written.written == 1
        assert _with_properties(wiki, "relative") == []

    def test_several_claims_over_the_limit_in_one_batch(self):
        # the postcard's greeting as Klara's nickname and the compensation
        # file's sentence as Hildegard's kinship are refused; Sebastian's
        # birth, on a page cut to the limit, and the town's key, at the
        # limit and given in NFD, are written
        nickname = _saying(
            "monolingual value", _said_over("Liebes Pünktchen! ", 401)
        )
        kinship = _saying(
            "string qualifier",
            unicodedata.normalize(
                "NFD", _said_over("Schwägerin der Verstorbenen, ", 450)
            ),
        )
        birth = dict(BIRTH, source=dict(BIRTH["source"], page_number=NUMBER))
        key = _said_over("flörburg-", 400)
        town = _saying("external id", unicodedata.normalize("NFD", key))
        batch = _batch_of(nickname, birth, kinship, town)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        ids = [claim_id(batch.claims[0]), claim_id(batch.claims[2])]
        assert written.refused == ids
        nickname_warning, kinship_warning = written.warnings
        assert nickname_warning.startswith(f"claim {ids[0]} (nickname)")
        assert kinship_warning.startswith(f"claim {ids[1]} (relative)")
        # counted in NFC, the form the instance keeps it in
        assert "its kinship_type qualifier has 450 characters" in (
            kinship_warning
        )
        assert (written.written, written.referenced, written.skipped) == (
            2,
            0,
            0,
        )
        sebastian = written.created_items["e01"]
        (born,) = _statements(wiki, sebastian, "date_of_birth")
        assert _places(born) == [
            ("Q23", NUMBER[: ROOM - 3] + _mark(NUMBER), None, None)
        ]
        (keyed,) = _statements(
            wiki, written.created_items["pl1"], "whg_place_id"
        )
        assert _values(keyed) == [key]
        assert _with_properties(wiki, "nickname") == []
        assert _with_properties(wiki, "relative") == []
        # a re-run refuses the same claims again, and writes nothing
        before = {qid: _snapshot(wiki, qid) for qid in wiki.wbi.item.store}
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert again.refused == ids
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
        assert wiki.wbi.item.writes == writes
        assert {qid: _snapshot(wiki, qid) for qid in before} == before

    def test_a_dry_run_names_the_same_claims(self):
        text = _said_over(LONG_TEXTS["external id"], 401)
        batch = _batch_of(_saying("external id", text), BIRTH)
        wiki = FakeWiki(MEDIA, detached=True)

        planned = BatchWriter(wiki, dry_run=True).write_batch(batch)

        assert planned.refused == [claim_id(batch.claims[0])]
        assert planned.written == 1
        assert len(planned.warnings) == 1
        # the plan holds the birth's subject, and not the town, which only
        # the refused claim mentions
        assert set(planned.created_items) == {"e01"}
        assert wiki.wbi.item.writes == 0


# -- the kind of an entity only a refused claim gives -------------------------


class TestKindsBesideARefusal:
    """A local id without a manifest record takes its kind from the first
    ref to it in a claim the writer writes: a refused claim's refs are
    never resolved, so the kind it gives is neither recorded nor warned
    of, and the kind a later claim gives is."""

    UNKNOWN = (
        "entity 'e07' has the kind 'no_such_kind', which the vocabulary "
        "lacks: no kind is recorded for it"
    )

    @pytest.mark.parametrize(
        ("refused", "kept"),
        [("no_such_kind", "human"), ("human", "no_such_kind")],
        ids=["the written ref's kind is known", "it is not"],
    )
    def test_the_kind_is_the_first_written_refs(self, refused, kept):
        # (here) the register's cause of death, taken at length and so
        # refused, names Eugen first, by a kind of its own
        cause = _said_over("Wassersucht nach langer Krankheit; ", 401)
        batch = _batch_of(_cause(cause=cause, kind=refused), _cause(kind=kept))
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        eugen = written.created_items["e07"]
        kinds = [
            statement.mainsnak.datavalue["value"]["id"]
            for statement in _statements(wiki, eugen, "instance_of")
        ]
        unknown = [w for w in written.warnings if "vocabulary lacks" in w]
        if kept == "human":
            assert (kinds, unknown) == ([ITEMS["human"]], [])
        else:
            assert (kinds, unknown) == ([], [self.UNKNOWN])


# -- an entity's type hint: left out, never cut -------------------------------

# (here) the directory's extraction gives the firm no kind, and as its type
# hint the whole heading of the trade section it is listed under
HEADING = "Hutmacher und Hutgeschäfte, Mützenmacher, "


class TestTypeHintsOverTheLimit:
    """An entity's type hint over the limit is left out wherever the
    writer would record it, minted or found, with a warning that does not
    quote it; never cut. The entity's kind and the batch's claims are
    written."""

    @pytest.mark.parametrize(
        "given",
        ["400 characters", "401 characters", "in NFD, NFC at the limit"],
    )
    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_a_type_hint_over_the_limit_is_left_out(self, found, given):
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        if found:
            BatchWriter(wiki).write_batch(_firm_batch())
        length = TEXT_LIMIT + (given == "401 characters")
        hint = _said_over(HEADING, length)
        sent = hint
        if given.startswith("in NFD"):
            sent = unicodedata.normalize("NFD", hint)
            assert len(sent) > TEXT_LIMIT
        directory = _directory_batch("unclassified_entity", type_hint=sent)

        written = BatchWriter(wiki).write_batch(directory)

        firm = written.created_items["firm"]
        hints = set(_by_value(_statements(wiki, firm, "type_hint")))
        earlier = {"hat shop"} if found else set()
        assert written.written == 1
        assert _statements(wiki, firm, "instance_of")
        if length > TEXT_LIMIT:
            assert hints == earlier
            (warning,) = written.warnings
            assert warning.startswith(
                "entity 'firm' has a type hint of 401 characters"
            )
            assert _quotes(written.warnings, HEADING) == []
        else:
            # at the limit in NFC, the hint is recorded whole, in NFC
            assert hints == earlier | {hint}
            assert written.warnings == []
