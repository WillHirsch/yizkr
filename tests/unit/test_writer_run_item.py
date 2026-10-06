"""Offline tests of the writer's extraction-run item (#368).

The run item's kind, `instance of -> extraction run`, cites the run item
itself, so the writer writes it after the write that makes the item under
its label. A write cut short between the two leaves the item labelled and
untyped, and the non-privileged export leaves out an untyped item, so the
statements it backs would lose their run reference there. A re-run finds
the item by its label and gives it its kind (`ensure_self_typed` in
yizkr.pipeline.registrar); a run item that states its kind already, in
any way, is left as it is, and a dry run writes nothing.

The fake serves items as an instance does (DetachedItems in
test_pipeline.py). Every fixture is fictional (docs/EXAMPLES.md).
"""

from __future__ import annotations

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.identity import write_mapped
from tests.unit.test_pipeline import (
    ITEMS,
    MEDIA_SHA1,
    PROPERTIES,
    FakeWiki,
    _cited,
    batch,
)
from yizkr.pipeline.writer import BatchWriter

# the write that gives a new run item its kind
TYPING = "ingest: run item statements"


class CutShort(RuntimeError):
    """The connection to the wiki was lost during a write."""


def cut_short_at(patch, wiki, refused: str) -> None:
    """Make every write with the edit summary `refused` fail before
    anything of it is stored, as a lost connection would, while `patch`
    holds."""
    items = wiki.wbi.item
    bound = items._bound

    def refusing(item):
        item = bound(item)
        write = item.write

        def cut(summary=None, **options):
            if summary == refused:
                raise CutShort("the connection to the wiki was lost")
            return write(summary=summary, **options)

        item.write = cut
        return item

    patch.setattr(items, "_bound", refusing)


def instance_wiki() -> FakeWiki:
    """A wiki holding the artifact the fixture batch cites."""
    return FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)


def kinds(wiki, qid: str) -> list[tuple[str, str, list[str]]]:
    """(kind, rank, the items it is stated in) of each `instance of`
    statement of an item, as the instance serves it."""
    return [
        (
            statement.mainsnak.datavalue["value"]["id"],
            statement.rank.value,
            _cited(statement, "stated_in"),
        )
        for statement in wiki.wbi.item.get(qid).claims.get(
            PROPERTIES["instance_of"]
        )
        or []
    ]


def left_untyped(monkeypatch) -> tuple[FakeWiki, str]:
    """A wiki where a write of the fixture batch was cut short between the
    run item's two writes: the run item, labelled and untyped, is all it
    wrote. Returns the wiki and the run item."""
    wiki = instance_wiki()
    with monkeypatch.context() as patch:
        cut_short_at(patch, wiki, TYPING)
        with pytest.raises(CutShort):
            write_mapped(BatchWriter(wiki), batch())
    (run,) = wiki.wbi.item.store
    assert str(wiki.wbi.item.get(run).labels.get("en")).startswith(
        "extraction run 2026-07-12 fake-model "
    )
    assert kinds(wiki, run) == []
    return wiki, run


def writes_to(wiki, qid: str, since: int) -> list[dict]:
    """The writes sent for the item `qid` after the first `since`."""
    return [data for data in wiki.wbi.item.sent[since:] if data["id"] == qid]


class TestWriterRunItem:
    def test_a_new_run_item_states_its_kind_in_itself(self):
        wiki = instance_wiki()

        result = write_mapped(BatchWriter(wiki), batch())

        run = result.run_qid
        assert kinds(wiki, run) == [(ITEMS["extraction_run"], "normal", [run])]
        person = wiki.wbi.item.get(result.created_items["person-1"])
        (born,) = person.claims.get(PROPERTIES["date_of_birth"])
        assert _cited(born, "inferred_from_extraction_run") == [run]

    def test_a_write_cut_short_is_typed_by_the_rerun(self, monkeypatch):
        """The re-run finds the run item by its label, gives it its kind,
        stated in itself, and writes the batch's claims citing it: one run
        item, typed."""
        wiki, left = left_untyped(monkeypatch)

        result = write_mapped(BatchWriter(wiki), batch())

        assert result.run_qid == left
        assert kinds(wiki, left) == [
            (ITEMS["extraction_run"], "normal", [left])
        ]
        runs = [
            qid
            for qid in wiki.wbi.item.store
            if str(wiki.wbi.item.get(qid).labels.get("en")).startswith(
                "extraction run "
            )
        ]
        assert runs == [left]
        person = wiki.wbi.item.get(result.created_items["person-1"])
        (born,) = person.claims.get(PROPERTIES["date_of_birth"])
        assert _cited(born, "inferred_from_extraction_run") == [left]

    def test_a_run_item_with_its_kind_gets_no_write(self):
        wiki = instance_wiki()
        first = write_mapped(BatchWriter(wiki), batch())
        run = first.run_qid
        before = wiki.wbi.item.get(run).get_json()
        sent = len(wiki.wbi.item.sent)

        again = write_mapped(BatchWriter(wiki), batch())

        assert again.run_qid == run
        assert writes_to(wiki, run, sent) == []
        assert wiki.wbi.item.get(run).get_json() == before

    def test_a_dry_run_writes_nothing(self, monkeypatch):
        """A dry run finds the untyped run item and writes nothing to it,
        as it writes nothing at all; with no run item, it makes none."""
        fresh = instance_wiki()
        planned = write_mapped(BatchWriter(fresh, dry_run=True), batch())
        assert planned.run_qid == "(new:run)"
        assert fresh.wbi.item.writes == 0

        wiki, left = left_untyped(monkeypatch)
        writes = wiki.wbi.item.writes

        planned = write_mapped(BatchWriter(wiki, dry_run=True), batch())

        assert planned.run_qid == left
        assert wiki.wbi.item.writes == writes
        assert kinds(wiki, left) == []

    def test_a_kind_a_curator_deprecated_is_left_alone(self):
        """A curator deprecated the run item's kind: the rank stands
        (CLAUDE.md rules 3 and 4), and a re-run puts no second statement
        beside it."""
        wiki = instance_wiki()
        run = write_mapped(BatchWriter(wiki), batch()).run_qid
        item = wiki.wbi.item.get(run)
        (statement,) = item.claims.get(PROPERTIES["instance_of"])
        statement.rank = WikibaseRank.DEPRECATED
        item.write(summary="curator: deprecate the run item's kind")
        before = wiki.wbi.item.get(run).get_json()
        sent = len(wiki.wbi.item.sent)

        again = write_mapped(BatchWriter(wiki), batch())

        assert again.run_qid == run
        assert writes_to(wiki, run, sent) == []
        assert wiki.wbi.item.get(run).get_json() == before
        assert kinds(wiki, run) == [
            (ITEMS["extraction_run"], "deprecated", [run])
        ]
