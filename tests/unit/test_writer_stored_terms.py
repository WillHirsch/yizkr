"""Offline tests of the one rule by which the writer and the registrar keep
a term, and of the text Wikibase cleans up (#369, #403; `_stored_term`,
`_cleaned_up` and CLEANED_UP in yizkr.pipeline.writer).

Wikibase keeps a label, a description or an alias as its trimToNFC gives
it: trimmed first, then cleaned up (UtfNormal's cleanUp, which makes each
C0 control character but the tab, the line feed and the carriage return,
and the noncharacters U+FFFE and U+FFFF, U+FFFD), then in NFC. It cleans
up a string or external-id value before it stores it, stores a
monolingual text as it is sent, and its API cleans up every text it gives
back. The unit fake keeps and serves text so (`_as_stored`, `_as_served`
and `_kept_term` in test_pipeline.py), its character sets written out apart
from the writer's. So:

* a label given with whitespace around it, or with U+FFFF in it, finds
  the item written for it: a second write mints nothing and sends no edit;
* a description is cut once trimmed, so whitespace around it never counts
  towards the limit;
* a page number, region, excerpt or type hint with a character Wikibase
  cleans up is sent cleaned up, so a second write finds the reference
  block or type statement written for it and sends no edit;
* a claim whose own text, its value or a qualifier's, holds such a
  character is refused whole, never cleaned up (CLAUDE.md rule 5), as one
  with whitespace around it is, never trimmed: a warning names the fault,
  quoting no text, and the batch's other claims are written.

The registrar keeps a title by the same rule (test_registrar.py). Every
text comes from the fictional universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import pytest

from tests.unit.test_pipeline import (
    DIRECTORY_SHA1,
    INTERVIEW_SHA1,
    MEDIA_SHA1,
    TEXT_CLAIMS,
    TEXT_OF,
    FakeWiki,
    _batch_of,
    _by_value,
    _certificate_batch,
    _directory_batch,
    _firm_batch,
    _labelled,
    _places,
    _sources,
    _statements,
    _values,
)
from tests.unit.test_writer_limits import (
    BIRTH,
    MEDIA,
    MENTIONS,
    WHERE,
    _cause,
    _quotes,
    _quoting,
    _said_over,
    _saying,
    _with_properties,
)
from tests.unit.test_writer_text_shapes import TERM_ROOM, _term_cut
from yizkr.claims import ClaimBatch, RegionPercent
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import TERM_LIMIT, BatchWriter

REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def _summarised(summary: str) -> ClaimBatch:
    """The certificate's batch, its manifest record of Sebastian giving
    `summary`."""
    batch = _certificate_batch()
    (record,) = batch.entities
    return batch.model_copy(
        update={"entities": [record.model_copy(update={"summary": summary})]}
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


# -- labels and descriptions: trimmed, then cleaned up, then cut --------------


class TestTermsAsTheInstanceKeepsThem:
    """A label is sent and looked for, and a description sent, in the form
    Wikibase keeps a term in (_stored_term): whitespace around it trimmed,
    U+FFFE and U+FFFF made U+FFFD, and cut once in that form."""

    @pytest.mark.parametrize(
        ("given", "kept"),
        [
            ("  Eugen Löbig\N{IDEOGRAPHIC SPACE}", "Eugen Löbig"),
            ("\tEugen Löbig\n", "Eugen Löbig"),
            ("Eugen Löbig￿", f"Eugen Löbig{REPLACEMENT}"),
            (" ￾Eugen Löbig", f"{REPLACEMENT}Eugen Löbig"),
        ],
        ids=[
            "spaces before, an ideographic space after",
            "a tab before, a line feed after",
            "U+FFFF at its end",
            "a space, then U+FFFE, at its start",
        ],
    )
    def test_a_label_written_twice_finds_its_item(self, given, kept):
        # (here) the burial register gives Eugen's name as a transcription
        # can: the second write finds his item by it, and mints nothing
        batch = _batch_of(_cause(label=given))
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        eugen = written.created_items["e07"]
        assert _labelled(wiki, kept) == [eugen]
        again, unchanged = _written_again(wiki, batch)
        assert again.created_items["e07"] == eugen
        assert unchanged
        assert _labelled(wiki, kept) == [eugen]

    def test_a_description_is_cut_once_trimmed(self):
        # (here) Sebastian's summary, said over to one character past the
        # limit, a line break and spaces around it: cut as the summary
        # alone is, with the digest of the summary alone
        summary = _said_over(
            "Master hatter of Flörburg, of the Hutmacherei zum Blauen Hut, ",
            TERM_LIMIT + 1,
        )
        assert summary[TERM_ROOM - 1 : TERM_ROOM + 1] == "ei"
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        written = BatchWriter(wiki).write_batch(
            _summarised(f"\n  {summary} \N{IDEOGRAPHIC SPACE}")
        )

        item = wiki.wbi.item.get(written.created_items["e01"])
        assert str(item.descriptions.get("en")) == _term_cut(summary)

    def test_whitespace_around_a_description_never_counts(self):
        # the summary at the limit, taken past it by whitespace alone: kept
        # whole, as the instance keeps it
        summary = _said_over(
            "Master hatter of Flörburg, of the Hutmacherei zum Blauen Hut, ",
            TERM_LIMIT,
        )
        padded = f"\t {summary}\n\N{IDEOGRAPHIC SPACE}"
        assert len(padded) > TERM_LIMIT
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        written = BatchWriter(wiki).write_batch(_summarised(padded))

        item = wiki.wbi.item.get(written.created_items["e01"])
        assert str(item.descriptions.get("en")) == summary


# -- locators and type hints: cleaned up as the instance gives them back ------


class TestLocatorsAndHintsCleanedUp:
    """A page number, region, excerpt or type hint with a character
    Wikibase cleans up goes with it made U+FFFD, as the instance gives it
    back, so a second write of the batch finds what the first wrote, sends
    no edit and adds no reference block or statement."""

    @pytest.mark.parametrize(
        ("where", "char"),
        [("page number", "\x08"), ("excerpt", "\x1f")],
        ids=["a backspace in a page number", "U+001F in an excerpt"],
    )
    def test_a_locator_written_twice_is_one_block(self, where, char):
        given = f"Blatt 12,{char}Flörburg"
        claim = _cause(given) if where == "page number" else _quoting(given)
        batch = _batch_of(claim)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, page, _, excerpt),) = _places(died)
        kept = page if where == "page number" else excerpt
        assert kept == f"Blatt 12,{REPLACEMENT}Flörburg"
        again, unchanged = _written_again(wiki, batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert unchanged
        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        assert len(_sources(died)) == 2

    def test_a_region_written_twice_is_one_block(self, monkeypatch):
        # a region's form, as a percentage box gives it, holds no control
        # character; the rule applies to whatever form the region takes
        monkeypatch.setattr(
            RegionPercent,
            "as_wikibase_string",
            lambda region: "10%,20%,\x0160%,10%",
        )
        claim = _cause()
        claim["source"]["region"] = {
            "x": 10,
            "y": 20,
            "width": 60,
            "height": 10,
        }
        batch = _batch_of(claim)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        (died,) = _statements(
            wiki, written.created_items["e07"], "cause_of_death"
        )
        ((_, _, region, _),) = _places(died)
        assert region == f"10%,20%,{REPLACEMENT}60%,10%"
        again, unchanged = _written_again(wiki, batch)
        assert again.skipped == 1
        assert unchanged

    @pytest.mark.parametrize(
        "char", ["\x01", "￾", "￿"], ids=["U+0001", "U+FFFE", "U+FFFF"]
    )
    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_a_type_hint_written_twice_is_one_statement(self, found, char):
        # (here) the directory's heading for the firm's trade, as a scan's
        # text layer gives it
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        if found:
            BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_batch(
            "unclassified_entity", type_hint=f"Hut{char}geschäft"
        )

        written = BatchWriter(wiki).write_batch(directory)

        firm = written.created_items["firm"]
        hints = set(_by_value(_statements(wiki, firm, "type_hint")))
        earlier = {"hat shop"} if found else set()
        assert hints == earlier | {f"Hut{REPLACEMENT}geschäft"}
        again, unchanged = _written_again(wiki, directory)
        assert unchanged
        assert again.type_statements_written == 0
        assert len(_statements(wiki, firm, "type_hint")) == len(hints)


# -- a claim's own text: refused, never trimmed or cleaned up -----------------


class TestClaimsTheInstanceWouldChange:
    """A claim whose own text has whitespace around it, or holds a
    character Wikibase cleans up, is refused whole: its text is never
    trimmed or cleaned up, as that would change what the source says (rule
    5). The warning names the fault and quotes no text, and the batch's
    other claims are written."""

    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    def test_a_value_is_never_trimmed(self, case):
        words = TEXT_OF[case]
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(
            _batch_of(_saying(case, f"  {words}\t"), BIRTH)
        )

        assert (written.written, len(written.refused)) == (1, 1)
        (warning,) = written.warnings
        assert "a tab, whitespace at its start and whitespace at" in warning
        assert _with_properties(wiki, TEXT_CLAIMS[case]["property"]) == []

    @pytest.mark.parametrize(
        ("case", "char"),
        [
            ("external id", "\x1f"),
            ("monolingual value", "\x01"),
            ("string qualifier", "￿"),
        ],
        ids=[
            "U+001F in a value",
            "U+0001 in a monolingual value",
            "U+FFFF in a qualifier",
        ],
    )
    def test_a_cleaned_up_character_refuses_the_claim(self, case, char):
        words = TEXT_OF[case]
        refused = _saying(case, f"{words}{char}{words}")
        wiki = FakeWiki(MEDIA, detached=True)
        batch = _batch_of(refused, BIRTH)

        written = BatchWriter(wiki).write_batch(batch)

        cid = claim_id(batch.claims[0])
        assert written.refused == [cid]
        assert written.warnings == [
            f"claim {cid} ({refused['property']}) is refused, and nothing "
            f"of it is written: {WHERE[case]} holds a control character or "
            f"a noncharacter, which Wikibase changes into U+FFFD. A cut or "
            f"any other change would alter what the source says"
        ]
        assert _quotes(written.warnings, words) == []
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

    def test_a_character_the_instance_keeps_is_written(self):
        # DEL and the C1 control characters Wikibase neither refuses nor
        # cleans up in a value: the claim is written as it is
        text = "Pünktchen\x7f\x9f"
        claim = _saying("monolingual value", text)
        wiki = FakeWiki(MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(_batch_of(claim))

        assert (written.written, written.refused) == (1, [])
        (named,) = _statements(wiki, written.created_items["e02"], "nickname")
        assert _values(named) == [text]
