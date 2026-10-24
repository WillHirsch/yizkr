"""The writer's judgement notes, unions and refusals (#410).

A judgement note goes in the run block of the statement it concerns, as
`judgement` (its category) and `judgement_note` (its sentence), compared
whole with the statement's blocks, so a re-run adds nothing and a second
claim of the run adds its own run block only where its note differs
(ADR-0035 §4). An entity's note goes in the run block of its `instance of`.
A union and its events are written as any entity's claims are, with their
roles and kinship qualifiers (ADR-0035 §3, ADR-0036). A claim of a retired
or reference property is refused by the writer itself, as `yizkr write`
takes a batch without validating it. Every example comes from the
fictional universe (docs/EXAMPLES.md): the 1957 divorce judgment, the
burial register, the interview.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from tests.fixtures.identity import joined, mapped, write_mapped
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import TEXT_LIMIT, BatchWriter

# the vocabulary the notes, roles, kinship classes and unions add, beside
# the unit tests' own
NOTE_PROPERTIES = {
    **PROPERTIES,
    "judgement": "P40",
    "judgement_note": "P41",
    "participant": "P42",
    "object_has_role": "P43",
    "started_by": "P44",
    "ended_by": "P45",
    "carried_out_by": "P46",
    "kinship_class": "P47",
    "side": "P48",
    "degree": "P49",
    "removes": "P50",
    "civil_status": "P51",
    "ended_by_death_of": "P52",
    "parentage": "P53",
}
NOTE_ITEMS = {
    **ITEMS,
    "judgement_kind": "Q40",
    "judgement_relation": "Q41",
    "judgement_date_reading": "Q42",
    "judgement_identity": "Q43",
    "judgement_other": "Q44",
    "marriage": "Q45",
    "partnership": "Q46",
    "civil_wedding": "Q47",
    "divorce": "Q48",
    "death": "Q49",
    "spouse": "Q50",
    "principal": "Q51",
    "kin_cousin": "Q52",
    "kin_aunt_or_uncle": "Q53",
    "widowed": "Q54",
    "wedding": "Q55",
    "parentage_adoptive": "Q56",
}

JUDGMENT_SHA1 = hashlib.sha1(
    b"fictional scan: the 1957 divorce judgment of the Landgericht Rekau"
).hexdigest()
BURIAL_SHA1 = hashlib.sha1(
    b"fictional scan: the burial register's entry for Eugen Loebig"
).hexdigest()
INTERVIEW_SHA1 = hashlib.sha1(
    b"fictional recording: the interview with Nicholas"
).hexdigest()
MEDIA = {JUDGMENT_SHA1: "Q23", BURIAL_SHA1: "Q24", INTERVIEW_SHA1: "Q25"}

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-12T09:00:00Z",
}

DATE_NOTE = {
    "category": "date_reading",
    "text": "'8. Februar 1947' read as the day the marriage was concluded",
}
OTHER_DATE_NOTE = {
    "category": "date_reading",
    "text": "the judgment's date of the wedding read as given, not as the "
    "date of its registration",
}
KIND_NOTE = {
    "category": "kind",
    "text": "the Landgericht Rekau typed an organisation: the judgment "
    "names it as the court that pronounced the divorce",
}


class NotedWiki(FakeWiki):
    """The unit tests' instance, with the vocabulary of notes, roles,
    kinship classes and unions."""

    def __init__(self, detached: bool = True) -> None:
        super().__init__(dict(MEDIA), detached=detached)

    def vocabulary(self):
        return dict(NOTE_PROPERTIES), dict(NOTE_ITEMS)


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def source(sha1: str = JUDGMENT_SHA1, excerpt: str = "", page: str = "1"):
    block = {"artifact": {"kind": "artifact", "sha1": sha1}}
    if page:
        block["page_number"] = page
    if excerpt:
        block["transcription_excerpt"] = {"text": excerpt, "language": "de"}
    return block


LEOPOLD = local("e1", "Leopold Löbig", "human")
AGNES = local("e2", "Agnes Löbig", "human")
MARRIAGE = local(
    "e3", "marriage of Leopold Löbig and Agnes Nertler", "marriage"
)
WEDDING = local("e4", "civil wedding of Leopold and Agnes", "civil_wedding")
DIVORCE = local("e5", "divorce of Leopold and Agnes", "divorce")
COURT = local("e6", "Landgericht Rekau", "organization")
FLOERBURG = local("e7", "Flörburg", "place")

EXCERPT = (
    "Die am 8. Februar 1947 vor dem Standesbeamten in Flörburg "
    "geschlossene Ehe der Parteien wird geschieden"
)


def wedding_date(note: dict | None = None, **source_options) -> dict:
    claim = {
        "subject": WEDDING,
        "property": "point_in_time",
        "value": {"type": "time", "year": 1947, "month": 2, "day": 8},
        "source": source(excerpt=EXCERPT, **source_options),
    }
    if note:
        claim["note"] = note
    return claim


def batch_of(*claims: dict, entities=(), run=RUN) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": run, "claims": list(claims), "entities": entities}
    )


def statements(wiki: FakeWiki, qid: str, key: str) -> list:
    return wiki.wbi.item.get(qid).claims.get(NOTE_PROPERTIES[key]) or []


def blocks(statement) -> list[dict[str, list]]:
    """Each reference block of `statement`, by property key, each snak's
    value as an item id or a string."""
    keys = {pid: key for key, pid in NOTE_PROPERTIES.items()}
    shown = []
    for reference in statement.references.references:
        block: dict[str, list] = {}
        for pid, snaks in reference.snaks.snaks.items():
            for snak in snaks:
                value = snak.datavalue["value"]
                if isinstance(value, dict):
                    value = value.get("id") or value.get("text")
                block.setdefault(keys[pid], []).append(value)
        shown.append(block)
    return shown


def run_blocks(statement) -> list[tuple]:
    """Each run block of `statement` as (run item, category item, note),
    the last two None where the block carries no note, sorted."""
    return sorted(
        (
            (
                block["inferred_from_extraction_run"][0],
                (block.get("judgement") or [None])[0],
                (block.get("judgement_note") or [None])[0],
            )
            for block in blocks(statement)
            if "inferred_from_extraction_run" in block
        ),
        key=lambda shown: tuple(part or "" for part in shown),
    )


def the_wedding_date(wiki, result) -> object:
    (statement,) = statements(
        wiki, result.created_items["e4"], "point_in_time"
    )
    return statement


class TestAClaimsNote:
    def test_it_goes_in_the_run_block(self):
        wiki = NotedWiki()
        result = write_mapped(
            BatchWriter(wiki), batch_of(wedding_date(DATE_NOTE))
        )
        assert (result.written, result.notes_written) == (1, 1)
        statement = the_wedding_date(wiki, result)
        source_block, run_block = blocks(statement)
        # never in the `stated in` block, never as a qualifier
        assert source_block["stated_in"] == ["Q23"]
        assert "judgement" not in source_block
        assert "judgement_note" not in source_block
        assert not statement.qualifiers.qualifiers
        assert run_block == {
            "inferred_from_extraction_run": [result.run_qid],
            "judgement": [NOTE_ITEMS["judgement_date_reading"]],
            "judgement_note": [DATE_NOTE["text"]],
        }

    def test_each_category_names_its_item(self):
        for category in (
            "kind",
            "relation",
            "date_reading",
            "identity",
            "other",
        ):
            wiki = NotedWiki()
            note = {"category": category, "text": "a call one could differ on"}
            result = write_mapped(
                BatchWriter(wiki), batch_of(wedding_date(note))
            )
            ((_, item, _),) = run_blocks(the_wedding_date(wiki, result))
            assert item == NOTE_ITEMS[f"judgement_{category}"], category

    def test_a_claim_without_one_has_a_bare_run_block(self):
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch_of(wedding_date()))
        assert result.notes_written == 0
        assert run_blocks(the_wedding_date(wiki, result)) == [
            (result.run_qid, None, None)
        ]

    def test_a_rewrite_adds_nothing(self):
        wiki = NotedWiki()
        batch = batch_of(wedding_date(DATE_NOTE))
        first = write_mapped(BatchWriter(wiki), batch)
        before = blocks(the_wedding_date(wiki, first))
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert again.notes_written == 0
        assert wiki.wbi.item.writes == writes
        assert blocks(the_wedding_date(wiki, again)) == before

    def test_a_note_given_in_nfd_finds_its_block(self):
        """The run block is compared in NFC: a note given decomposed is the
        note the instance gives back composed."""
        wiki = NotedWiki()
        note = dict(DATE_NOTE, text="'8. Februar 1947' read as in Flörburg")
        first = write_mapped(BatchWriter(wiki), batch_of(wedding_date(note)))
        decomposed = dict(
            note, text=unicodedata.normalize("NFD", note["text"])
        )
        assert decomposed != note
        batch = batch_of(wedding_date(decomposed))
        # the same run item: a run's item is keyed on its batch, so the
        # decomposed batch is written under the first one's run item
        writer = BatchWriter(wiki)
        writer.ensure_run_item = lambda *_: first.run_qid
        again = write_mapped(writer, batch)
        assert (again.skipped, again.notes_written) == (1, 0)
        ((_, _, text),) = run_blocks(the_wedding_date(wiki, again))
        assert text == unicodedata.normalize("NFC", note["text"])

    def test_a_second_claim_of_the_run_with_another_note_adds_its_block(self):
        """Two claims of one run on one statement, from the same place in
        the judgment, with two notes: the statement gains the second run
        block, and its source block stays one."""
        wiki = NotedWiki()
        result = write_mapped(
            BatchWriter(wiki),
            batch_of(wedding_date(DATE_NOTE), wedding_date(OTHER_DATE_NOTE)),
        )
        assert (result.written, result.referenced) == (1, 1)
        assert result.notes_written == 2
        statement = the_wedding_date(wiki, result)
        assert [b for b in blocks(statement) if "stated_in" in b] == [
            {
                "stated_in": ["Q23"],
                "page_number": ["1"],
                "transcription_excerpt": [EXCERPT],
            }
        ]
        assert run_blocks(statement) == sorted(
            [
                (result.run_qid, "Q42", DATE_NOTE["text"]),
                (result.run_qid, "Q42", OTHER_DATE_NOTE["text"]),
            ]
        )

    def test_a_second_claim_with_the_same_note_adds_no_run_block(self):
        """The same note from another page of the judgment adds that page's
        source block, and no second copy of the run block."""
        wiki = NotedWiki()
        result = write_mapped(
            BatchWriter(wiki),
            batch_of(
                wedding_date(DATE_NOTE), wedding_date(DATE_NOTE, page="2")
            ),
        )
        assert (result.written, result.referenced) == (1, 1)
        assert result.notes_written == 1
        statement = the_wedding_date(wiki, result)
        assert len(blocks(statement)) == 3
        assert run_blocks(statement) == [
            (result.run_qid, "Q42", DATE_NOTE["text"])
        ]

    @pytest.mark.parametrize("noted_first", [True, False])
    def test_a_noted_and_a_bare_claim_meet_in_either_order(self, noted_first):
        """A claim without a note beside a noted one of the same run, from
        the same place, adds the bare run block, whichever comes first, so
        a batch's order changes nothing written."""
        wiki = NotedWiki()
        claims = [wedding_date(DATE_NOTE), wedding_date()]
        if not noted_first:
            claims.reverse()
        result = write_mapped(BatchWriter(wiki), batch_of(*claims))
        assert (result.written, result.referenced) == (1, 1)
        assert run_blocks(the_wedding_date(wiki, result)) == [
            (result.run_qid, None, None),
            (result.run_qid, "Q42", DATE_NOTE["text"]),
        ]
        assert result.notes_written == 1

    def test_a_later_run_without_a_note_adds_nothing(self):
        """A later run's claim from the same place, without a note, finds
        the claim recorded, as before notes; one with a note adds its run
        block, the note it carries, beside the earlier run's."""
        wiki = NotedWiki()
        first = write_mapped(BatchWriter(wiki), batch_of(wedding_date()))
        later = dict(RUN, model_identifier="fake-model-2")
        bare = write_mapped(
            BatchWriter(wiki), batch_of(wedding_date(), run=later)
        )
        assert bare.run_qid != first.run_qid
        assert (bare.skipped, bare.notes_written) == (1, 0)
        assert run_blocks(the_wedding_date(wiki, bare)) == [
            (first.run_qid, None, None)
        ]
        noted_run = dict(RUN, model_identifier="fake-model-3")
        noted = write_mapped(
            BatchWriter(wiki), batch_of(wedding_date(DATE_NOTE), run=noted_run)
        )
        assert (noted.referenced, noted.notes_written) == (1, 1)
        assert run_blocks(the_wedding_date(wiki, noted)) == sorted(
            [
                (first.run_qid, None, None),
                (noted.run_qid, "Q42", DATE_NOTE["text"]),
            ]
        )

    def test_a_curated_claim_has_no_note(self):
        wiki = NotedWiki()
        claim = dict(wedding_date(DATE_NOTE), curated=True)
        result = write_mapped(BatchWriter(wiki), batch_of(claim))
        assert (result.written, result.notes_written) == (1, 0)
        statement = the_wedding_date(wiki, result)
        assert run_blocks(statement) == []
        assert all("judgement" not in block for block in blocks(statement))

    def test_a_long_note_is_cut_as_a_locator_is(self):
        """A note over TEXT_LIMIT is cut at a break between words, with the
        mark and the digest of the whole, and never refuses its claim; a
        re-run finds its block."""
        wiki = NotedWiki()
        text = "the date read as the wedding's, " * 15
        assert len(text.strip()) > TEXT_LIMIT
        note = {"category": "date_reading", "text": text}
        batch = batch_of(wedding_date(note))
        result = write_mapped(BatchWriter(wiki), batch)
        assert (result.written, result.refused) == (1, [])
        ((_, _, kept),) = run_blocks(the_wedding_date(wiki, result))
        digest = hashlib.sha1(text.strip().encode()).hexdigest()[:12]
        assert len(kept) <= TEXT_LIMIT
        assert kept.endswith(f" … sha1 {digest}")
        assert text.startswith(kept.split(" …")[0])
        assert write_mapped(BatchWriter(wiki), batch).skipped == 1

    def test_a_note_the_instance_may_keep_otherwise_is_left_out(self):
        """A note holding a character Python's Unicode tables do not know
        is left out, with a warning that quotes none of it, and its claim
        is written."""
        wiki = NotedWiki()
        unknown = "͸"
        assert unicodedata.category(unknown) == "Cn"
        note = {"category": "other", "text": f"read as {unknown}"}
        claim = wedding_date(note)
        batch = batch_of(claim)
        result = write_mapped(BatchWriter(wiki), batch)
        assert (result.written, result.notes_written) == (1, 0)
        assert run_blocks(the_wedding_date(wiki, result)) == [
            (result.run_qid, None, None)
        ]
        (warning,) = [w for w in result.warnings if "judgement note" in w]
        assert claim_id(batch.claims[0]) in warning
        assert "U+0378" in warning
        assert "read as" not in warning


class TestAnEntitysNote:
    def court_batch(self, note=KIND_NOTE, *, also=None) -> ClaimBatch:
        entities = [
            {"id": "e6", "label": COURT["label"], "kind": "organization"}
        ]
        if note:
            entities[0]["note"] = note
        claims = [
            {
                "subject": DIVORCE,
                "property": "carried_out_by",
                "value": {"type": "item", "value": COURT},
                "source": source(excerpt=EXCERPT),
            }
        ]
        entities.append(
            {"id": "e5", "label": DIVORCE["label"], "kind": "divorce"}
        )
        if also:
            entities.append(also)
            claims.append(
                {
                    "subject": DIVORCE,
                    "property": "carried_out_by",
                    "value": {
                        "type": "item",
                        "value": local(
                            also["id"], also["label"], "organization"
                        ),
                    },
                    "source": source(excerpt=EXCERPT, page="2"),
                }
            )
        return batch_of(*claims, entities=entities)

    def test_it_goes_on_the_instance_of_of_its_item(self):
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), self.court_batch())
        court = result.created_items["e6"]
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == [
            (result.run_qid, NOTE_ITEMS["judgement_kind"], KIND_NOTE["text"])
        ]
        assert result.notes_written == 1
        # never on the claims that mention it, nor on another entity's kind
        (claim,) = statements(
            wiki, result.created_items["e5"], "carried_out_by"
        )
        assert run_blocks(claim) == [(result.run_qid, None, None)]
        (divorce_kind,) = statements(
            wiki, result.created_items["e5"], "instance_of"
        )
        assert run_blocks(divorce_kind) == [(result.run_qid, None, None)]

    def test_a_rewrite_adds_nothing(self):
        wiki = NotedWiki()
        batch = self.court_batch()
        first = write_mapped(BatchWriter(wiki), batch)
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), batch)
        assert again.created_items["e6"] == first.created_items["e6"]
        assert (again.notes_written, again.skipped) == (0, 1)
        assert again.type_statements_written == 0
        assert again.type_statements_referenced == 0
        assert wiki.wbi.item.writes == writes

    def test_a_second_mention_with_another_note_adds_its_run_block(self):
        """Two mentions of the court in one batch, one cluster of the map,
        each with a note of its own: the item's `instance of`, cited to the
        one artifact, gains the second note's run block, and a re-run adds
        nothing."""
        wiki = NotedWiki()
        other = {
            "category": "identity",
            "text": "the court of the second page read as the same court",
        }
        batch = self.court_batch(
            also={
                "id": "e8",
                "label": COURT["label"],
                "kind": "organization",
                "note": other,
            }
        )
        identity = joined(mapped(batch), COURT["label"])
        result = write_mapped(BatchWriter(wiki), batch, identity=identity)
        court = result.created_items["e6"]
        assert result.created_items["e8"] == court
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == sorted(
            [
                (result.run_qid, "Q40", KIND_NOTE["text"]),
                (result.run_qid, "Q43", other["text"]),
            ]
        )
        assert [b for b in blocks(kind) if "stated_in" in b] == [
            {"stated_in": ["Q23"]}
        ]
        assert result.notes_written == 2
        again = write_mapped(BatchWriter(wiki), batch, identity=identity)
        assert again.notes_written == 0
        (kind,) = statements(wiki, court, "instance_of")
        assert len(blocks(kind)) == 3

    def test_a_later_document_adds_its_note_with_its_source(self):
        """The court found again from another document gains that
        document's two blocks on its `instance of`, the run block with the
        record's note."""
        wiki = NotedWiki()
        earlier = self.court_batch(note=None)
        later = self.court_batch()
        # the same claims, cited to another document
        later = ClaimBatch.model_validate_json(
            later.model_dump_json().replace(JUDGMENT_SHA1, BURIAL_SHA1)
        )
        # one map of the two documents, in which each entity is one
        identity = joined(
            mapped(earlier, later), COURT["label"], DIVORCE["label"]
        )
        first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
        court = first.created_items["e6"]
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        assert result.created_items["e6"] == court
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == sorted(
            [
                (first.run_qid, None, None),
                (result.run_qid, "Q40", KIND_NOTE["text"]),
            ]
        )
        assert result.notes_written == 1

    def test_a_note_left_out_is_warned_of(self):
        wiki = NotedWiki()
        note = {"category": "kind", "text": "typed as ͸"}
        result = write_mapped(BatchWriter(wiki), self.court_batch(note))
        (kind,) = statements(wiki, result.created_items["e6"], "instance_of")
        assert run_blocks(kind) == [(result.run_qid, None, None)]
        assert any(
            w.startswith("entity 'e6' has a judgement note that holds U+0378")
            for w in result.warnings
        )


def divorce_judgment() -> ClaimBatch:
    """The 1957 judgment: Leopold and Agnes's marriage, its civil wedding on
    8 February 1947 at Flörburg, and its divorce on 2 January 1957, carried
    out by the Landgericht Rekau (ADR-0036 §2)."""

    def claim(subject, prop, value, qualifiers=()):
        return {
            "subject": subject,
            "property": prop,
            "value": value,
            "qualifiers": list(qualifiers),
            "source": source(excerpt=EXCERPT),
        }

    def item(ref):
        return {"type": "item", "value": ref}

    def role(key):
        return {
            "property": "object_has_role",
            "value": item({"kind": "vocab", "key": key}),
        }

    return batch_of(
        claim(MARRIAGE, "participant", item(LEOPOLD), [role("spouse")]),
        claim(MARRIAGE, "participant", item(AGNES), [role("spouse")]),
        claim(MARRIAGE, "started_by", item(WEDDING)),
        claim(MARRIAGE, "ended_by", item(DIVORCE)),
        wedding_date(),
        claim(WEDDING, "location", item(FLOERBURG)),
        claim(
            DIVORCE,
            "point_in_time",
            {"type": "time", "year": 1957, "month": 1, "day": 2},
        ),
        claim(DIVORCE, "carried_out_by", item(COURT)),
    )


class TestAUnion:
    def test_the_union_and_its_events_are_written(self):
        batch = divorce_judgment()
        assert validate_against_manifest(batch) == []
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch)
        assert (result.written, result.refused) == (8, [])
        made = result.created_items
        kinds = {
            local_id: [
                s.mainsnak.datavalue["value"]["id"]
                for s in statements(wiki, qid, "instance_of")
            ]
            for local_id, qid in made.items()
        }
        assert kinds == {
            "e1": ["Q1"],
            "e2": ["Q1"],
            "e3": [NOTE_ITEMS["marriage"]],
            "e4": [NOTE_ITEMS["civil_wedding"]],
            "e5": [NOTE_ITEMS["divorce"]],
            "e6": ["Q4"],
            "e7": ["Q5"],
        }
        marriage = made["e3"]
        couple = statements(wiki, marriage, "participant")
        assert sorted(s.mainsnak.datavalue["value"]["id"] for s in couple) == [
            made["e1"],
            made["e2"],
        ]
        for statement in couple:
            (snak,) = statement.qualifiers.get(
                NOTE_PROPERTIES["object_has_role"]
            )
            assert snak.datavalue["value"]["id"] == NOTE_ITEMS["spouse"]
        # the union points to its events, and no event links back
        assert [
            s.mainsnak.datavalue["value"]["id"]
            for s in statements(wiki, marriage, "started_by")
        ] == [made["e4"]]
        assert [
            s.mainsnak.datavalue["value"]["id"]
            for s in statements(wiki, marriage, "ended_by")
        ] == [made["e5"]]
        assert statements(wiki, made["e4"], "participant") == []
        (court,) = statements(wiki, made["e5"], "carried_out_by")
        assert court.mainsnak.datavalue["value"]["id"] == made["e6"]
        # every statement, the kinds included, with both references
        for qid in made.values():
            for statement in wiki.wbi.item.get(qid).claims:
                cited = blocks(statement)
                assert [b["stated_in"] for b in cited if "stated_in" in b] == [
                    ["Q23"]
                ]
                assert run_blocks(statement) == [(result.run_qid, None, None)]
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.written, again.skipped, again.referenced) == (0, 8, 0)

    def test_a_union_ended_by_a_partners_death(self):
        """The burial register's "Witwer der Cäcilie Löbig geb. Tarle":
        Eugen and Cäcilie's marriage, ended by the death of Cäcilie, and
        Eugen's civil status; no event stands for her death, and the
        line gives her no death statement (ADR-0040 §2.5, §2.7)."""
        eugen = local("b1", "Eugen Löbig", "human")
        caecilie = local("b2", "Cäcilie Löbig", "human")
        union = local("b3", "marriage of Eugen and Cäcilie Löbig", "marriage")
        excerpt = "Witwer der Cäcilie Löbig geb. Tarle"

        def claim(subject, prop, value, qualifiers=()):
            return {
                "subject": subject,
                "property": prop,
                "value": value,
                "qualifiers": list(qualifiers),
                "source": source(BURIAL_SHA1, excerpt=excerpt),
            }

        def item(ref):
            return {"type": "item", "value": ref}

        def role(key):
            return [
                {
                    "property": "object_has_role",
                    "value": item({"kind": "vocab", "key": key}),
                }
            ]

        batch = batch_of(
            claim(union, "participant", item(eugen), role("spouse")),
            claim(union, "participant", item(caecilie), role("spouse")),
            claim(union, "ended_by_death_of", item(caecilie)),
            claim(
                eugen,
                "civil_status",
                item({"kind": "vocab", "key": "widowed"}),
            ),
        )
        assert validate_against_manifest(batch) == []
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch)
        assert (result.written, result.refused) == (4, [])
        made = result.created_items
        (ended,) = statements(wiki, made["b3"], "ended_by_death_of")
        assert ended.mainsnak.datavalue["value"]["id"] == made["b2"]
        assert statements(wiki, made["b3"], "ended_by") == []
        (status,) = statements(wiki, made["b1"], "civil_status")
        assert (
            status.mainsnak.datavalue["value"]["id"] == NOTE_ITEMS["widowed"]
        )

    def test_the_guard_refuses_the_survivor_as_the_partner_who_died(self):
        """§2.5's guard: the end names Eugen, whom the same batch calls a
        widower, so the writer refuses it, as validation does."""
        eugen = local("b1", "Eugen Löbig", "human")
        union = local("b3", "marriage of Eugen and Cäcilie Löbig", "marriage")
        excerpt = "Witwer der Cäcilie Löbig geb. Tarle"
        widowed = {
            "subject": eugen,
            "property": "civil_status",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "widowed"},
            },
            "source": source(BURIAL_SHA1, excerpt=excerpt),
        }
        wrong = {
            "subject": union,
            "property": "ended_by_death_of",
            "value": {"type": "item", "value": eugen},
            "source": source(BURIAL_SHA1, excerpt=excerpt),
        }
        batch = batch_of(widowed, wrong)
        (problem,) = validate_against_manifest(batch)
        assert problem.endswith("[widowed_end]")
        result = write_mapped(BatchWriter(NotedWiki()), batch)
        assert result.refusal_reasons == ["vital_rule"]
        assert result.written == 1


class TestKinshipQualifiers:
    def test_a_class_and_a_degree_are_written_and_found_again(self):
        """The interview's "Aunt Pünktchen": Klara, Nicholas's relative,
        `kinship_type` aunt, of class aunt or uncle, degree 0 (ADR-0035
        §3.3); a quantity qualifier of 0 is found again on a re-run."""
        klara = local("i1", "Klara Löbig", "human")
        nicholas = local("i2", "Nicholas Lobig", "human")
        batch = batch_of(
            {
                "subject": klara,
                "property": "relative",
                "value": {"type": "item", "value": nicholas},
                "qualifiers": [
                    {
                        "property": "kinship_type",
                        "value": {"type": "string", "value": "aunt"},
                    },
                    {
                        "property": "kinship_class",
                        "value": {
                            "type": "item",
                            "value": {
                                "kind": "vocab",
                                "key": "kin_aunt_or_uncle",
                            },
                        },
                    },
                    {
                        "property": "degree",
                        "value": {"type": "quantity", "amount": 0},
                    },
                ],
                "source": source(INTERVIEW_SHA1, excerpt="Aunt Pünktchen"),
            }
        )
        assert validate_against_manifest(batch) == []
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch)
        (statement,) = statements(wiki, result.created_items["i1"], "relative")
        qualifiers = statement.qualifiers
        (kin,) = qualifiers.get(NOTE_PROPERTIES["kinship_class"])
        assert kin.datavalue["value"]["id"] == NOTE_ITEMS["kin_aunt_or_uncle"]
        (degree,) = qualifiers.get(NOTE_PROPERTIES["degree"])
        assert degree.datavalue["value"]["amount"] == "+0"
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.written, again.skipped) == (0, 1)


class TestARetiredPropertyIsRefused:
    def spouse_batch(self) -> ClaimBatch:
        return batch_of(
            {
                "subject": LEOPOLD,
                "property": "spouse",
                "value": {"type": "item", "value": AGNES},
                "source": source(excerpt=EXCERPT),
            },
            wedding_date(),
        )

    def test_the_claim_is_refused_and_the_rest_written(self):
        """`yizkr write` takes a batch without validating it, so the
        writer refuses a `spouse` claim itself, before any of its refs
        resolves: Agnes, whom only it names, gets no item."""
        batch = self.spouse_batch()
        assert validate_against_manifest(batch) != []
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch)
        spouse_claim = batch.claims[0]
        assert result.refused == [claim_id(spouse_claim)]
        assert result.written == 1
        assert "e2" not in result.created_items
        assert "e1" not in result.created_items
        (warning,) = [w for w in result.warnings if "retired" in w]
        assert warning == (
            f"claim {claim_id(spouse_claim)} (spouse) is refused, and "
            f"nothing of it is written: its property is retired and takes "
            f"no claim (ADR-0036)"
        )
        assert not any(
            item.claims.get(NOTE_PROPERTIES["spouse"])
            for item in wiki.wbi.item.store.values()
        )

    def test_a_dry_run_refuses_it_too(self):
        wiki = NotedWiki()
        result = write_mapped(
            BatchWriter(wiki, dry_run=True), self.spouse_batch()
        )
        assert result.refused == [claim_id(self.spouse_batch().claims[0])]
        assert result.written == 1
        assert wiki.wbi.item.writes == 0

    @pytest.mark.parametrize("as_qualifier", [False, True])
    def test_a_reference_property_is_refused(self, as_qualifier):
        """A note's property, as a claim's or a qualifier's, is written only
        in a statement's references (ADR-0035 §4)."""
        note = {"type": "string", "value": "read as 1947"}
        if as_qualifier:
            claim = dict(
                wedding_date(),
                qualifiers=[{"property": "judgement_note", "value": note}],
            )
            where = "its judgement_note qualifier"
        else:
            claim = {
                "subject": WEDDING,
                "property": "judgement_note",
                "value": note,
                "source": source(),
            }
            where = "its property"
        batch = batch_of(claim)
        wiki = NotedWiki()
        result = write_mapped(BatchWriter(wiki), batch)
        assert result.refused == [claim_id(batch.claims[0])]
        assert result.written == 0
        assert any(
            f"{where} is a reference property, written only in a "
            f"statement's references (ADR-0035 §4)" in warning
            for warning in result.warnings
        )
        # no item but the run item
        assert (result.created_items, result.minted) == ({}, [])


LATER_RUN = dict(RUN, model_identifier="fake-model-later")


def court_pair(
    first: dict | None, second: dict | None, *, reverse=False, run=RUN
) -> ClaimBatch:
    """Two mentions of the court in one document, e6 with the note
    `first` and e8 with `second`, each in a claim of its own, those claims
    in the batch's order or `reverse`d."""
    records = [
        {"id": "e5", "label": DIVORCE["label"], "kind": "divorce"},
        {"id": "e6", "label": COURT["label"], "kind": "organization"},
        {"id": "e8", "label": COURT["label"], "kind": "organization"},
    ]
    for record, note in ((records[1], first), (records[2], second)):
        if note:
            record["note"] = note
    claims = [
        {
            "subject": DIVORCE,
            "property": "carried_out_by",
            "value": {
                "type": "item",
                "value": local(local_id, COURT["label"], "organization"),
            },
            "source": source(excerpt=EXCERPT, page=page),
        }
        for local_id, page in (("e6", "1"), ("e8", "2"))
    ]
    if reverse:
        claims.reverse()
    return batch_of(*claims, entities=records, run=run)


class TestTheBatchsOrderChangesNothing:
    """A noted and a note-less claim (or mention) of one run on one
    statement give the same blocks in either order, for the run that wrote
    the statement and for a later one, and writing the batch again adds
    nothing (step 4 of the writer's module docstring)."""

    @pytest.mark.parametrize("noted_first", [True, False])
    def test_a_later_runs_noted_and_bare_claims(self, noted_first):
        wiki = NotedWiki()
        first = write_mapped(BatchWriter(wiki), batch_of(wedding_date()))
        claims = [wedding_date(DATE_NOTE), wedding_date()]
        if not noted_first:
            claims.reverse()
        later = batch_of(*claims, run=LATER_RUN)
        result = write_mapped(BatchWriter(wiki), later)
        assert (result.referenced, result.skipped) == (2, 0)
        assert result.notes_written == 1
        statement = the_wedding_date(wiki, result)
        assert run_blocks(statement) == sorted(
            [
                (first.run_qid, None, None),
                (result.run_qid, None, None),
                (result.run_qid, "Q42", DATE_NOTE["text"]),
            ],
            key=lambda shown: tuple(part or "" for part in shown),
        )
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), later)
        assert (again.referenced, again.skipped) == (0, 2)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("noted_first", [True, False])
    def test_the_same_runs_claims_written_again_add_nothing(self, noted_first):
        wiki = NotedWiki()
        claims = [wedding_date(DATE_NOTE), wedding_date()]
        if not noted_first:
            claims.reverse()
        batch = batch_of(*claims)
        write_mapped(BatchWriter(wiki), batch)
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), batch)
        assert (again.referenced, again.skipped) == (0, 2)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("reverse", [False, True])
    def test_an_entitys_noted_and_bare_mentions(self, reverse):
        wiki = NotedWiki()
        batch = court_pair(KIND_NOTE, None, reverse=reverse)
        identity = joined(mapped(batch), COURT["label"])
        result = write_mapped(BatchWriter(wiki), batch, identity=identity)
        (kind,) = statements(wiki, result.created_items["e6"], "instance_of")
        assert run_blocks(kind) == [
            (result.run_qid, None, None),
            (result.run_qid, "Q40", KIND_NOTE["text"]),
        ]
        assert result.notes_written == 1
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), batch, identity=identity)
        assert again.notes_written == 0
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("reverse", [False, True])
    def test_a_later_runs_noted_and_bare_mentions(self, reverse):
        wiki = NotedWiki()
        earlier = court_pair(None, None)
        identity = joined(mapped(earlier), COURT["label"])
        first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
        later = court_pair(KIND_NOTE, None, reverse=reverse, run=LATER_RUN)
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        court = result.created_items["e6"]
        assert court == first.created_items["e6"]
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == [
            (first.run_qid, None, None),
            (result.run_qid, None, None),
            (result.run_qid, "Q40", KIND_NOTE["text"]),
        ]
        assert result.notes_written == 1
        writes = wiki.wbi.item.writes
        write_mapped(BatchWriter(wiki), later, identity=identity)
        assert wiki.wbi.item.writes == writes


UNKNOWN_NOTE = {"category": "other", "text": "read as ͸"}


def cited_to(batch: ClaimBatch, sha1: str) -> ClaimBatch:
    """`batch`'s claims cited to the document `sha1` instead."""
    return ClaimBatch.model_validate_json(
        batch.model_dump_json().replace(JUDGMENT_SHA1, sha1)
    )


def note_warnings(result) -> list[str]:
    return [w for w in result.warnings if "judgement note" in w]


class TestCuratedAndOtherStatements:
    def test_a_curated_statement_gains_no_note_from_its_artifact(self):
        wiki = NotedWiki()
        curated = write_mapped(
            BatchWriter(wiki), batch_of(dict(wedding_date(), curated=True))
        )
        before = blocks(the_wedding_date(wiki, curated))
        noted = write_mapped(
            BatchWriter(wiki), batch_of(wedding_date(DATE_NOTE))
        )
        assert (noted.skipped, noted.notes_written) == (1, 0)
        assert blocks(the_wedding_date(wiki, noted)) == before

    def test_a_curated_statement_gains_another_artifacts_noted_blocks(self):
        wiki = NotedWiki()
        curated = batch_of(dict(wedding_date(), curated=True))
        noted = cited_to(batch_of(wedding_date(DATE_NOTE)), BURIAL_SHA1)
        identity = joined(mapped(curated, noted), WEDDING["label"])
        write_mapped(BatchWriter(wiki), curated, identity=identity)
        result = write_mapped(BatchWriter(wiki), noted, identity=identity)
        assert (result.referenced, result.notes_written) == (1, 1)
        statement = the_wedding_date(wiki, result)
        assert [
            b["stated_in"] for b in blocks(statement) if "stated_in" in b
        ] == [
            ["Q23"],
            ["Q24"],
        ]
        assert run_blocks(statement) == [
            (result.run_qid, "Q42", DATE_NOTE["text"])
        ]

    def court(self, note=None, sha1=JUDGMENT_SHA1) -> ClaimBatch:
        return cited_to(TestAnEntitysNote().court_batch(note), sha1)

    def test_a_deprecated_kind_gains_no_note(self):
        from wikibaseintegrator.wbi_enums import WikibaseRank

        wiki = NotedWiki()
        identity = joined(mapped(self.court()), COURT["label"])
        first = write_mapped(
            BatchWriter(wiki), self.court(), identity=identity
        )
        court = first.created_items["e6"]
        item = wiki.wbi.item.get(court)
        (kind,) = item.claims.get(NOTE_PROPERTIES["instance_of"])
        kind.rank = WikibaseRank.DEPRECATED
        item.write()
        before = blocks(statements(wiki, court, "instance_of")[0])
        result = write_mapped(
            BatchWriter(wiki), self.court(KIND_NOTE), identity=identity
        )
        assert result.notes_written == 0
        (kind,) = statements(wiki, court, "instance_of")
        assert blocks(kind) == before

    def test_a_new_kind_beside_a_curated_one_carries_the_note(self):
        wiki = NotedWiki()
        earlier = self.court(sha1=BURIAL_SHA1)
        later = self.court(KIND_NOTE)
        identity = joined(
            mapped(earlier, later), COURT["label"], DIVORCE["label"]
        )
        first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
        court = first.created_items["e6"]
        # a curator's own kind statement: its run block taken off
        item = wiki.wbi.item.get(court)
        (kind,) = item.claims.get(NOTE_PROPERTIES["instance_of"])
        kind.references.references = kind.references.references[:1]
        item.write()
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        assert result.notes_written == 1
        kinds = statements(wiki, court, "instance_of")
        assert [run_blocks(kind) for kind in kinds] == [
            [],
            [(result.run_qid, "Q40", KIND_NOTE["text"])],
        ]

    def test_an_approved_equivalences_note_goes_on_its_kind(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        wiki = NotedWiki()
        first = write_mapped(BatchWriter(wiki), self.court())
        court = first.created_items["e6"]
        later = apply_equivalences(
            self.court(KIND_NOTE, sha1=BURIAL_SHA1), {"e6": court}
        )
        result = write_mapped(BatchWriter(wiki), later)
        assert result.notes_written == 1
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == sorted(
            [
                (first.run_qid, None, None),
                (result.run_qid, "Q40", KIND_NOTE["text"]),
            ],
            key=lambda shown: tuple(part or "" for part in shown),
        )

    def test_a_type_hint_carries_no_note(self):
        """The compensation file's Wiedergutmachungsamt Peiltal, an
        unclassified entity with a type hint: its kind takes the note, its
        type hint none."""
        wiki = NotedWiki()
        office = local(
            "o1", "Wiedergutmachungsamt Peiltal", "unclassified_entity"
        )
        batch = batch_of(
            {
                "subject": DIVORCE,
                "property": "carried_out_by",
                "value": {"type": "item", "value": office},
                "source": source(),
            },
            entities=[
                {"id": "e5", "label": DIVORCE["label"], "kind": "divorce"},
                {
                    "id": "o1",
                    "label": office["label"],
                    "kind": "unclassified_entity",
                    "type_hint": "compensation office",
                    "note": KIND_NOTE,
                },
            ],
        )
        result = write_mapped(BatchWriter(wiki), batch)
        qid = result.created_items["o1"]
        (kind,) = statements(wiki, qid, "instance_of")
        (hint,) = statements(wiki, qid, "type_hint")
        assert run_blocks(kind) == [(result.run_qid, "Q40", KIND_NOTE["text"])]
        assert run_blocks(hint) == [(result.run_qid, None, None)]


class TestNoteWarnings:
    def test_none_for_a_curated_claim(self):
        claim = dict(wedding_date(UNKNOWN_NOTE), curated=True)
        result = write_mapped(BatchWriter(NotedWiki()), batch_of(claim))
        assert note_warnings(result) == []

    def test_none_for_a_refused_claim_or_an_unsent_record(self):
        """A `spouse` claim with a note, whose value's record, which only
        it names, has one too: both are refused or unsent, so neither note
        is warned of."""
        claim = {
            "subject": LEOPOLD,
            "property": "spouse",
            "value": {"type": "item", "value": AGNES},
            "source": source(),
            "note": UNKNOWN_NOTE,
        }
        entities = [
            {"id": "e1", "label": LEOPOLD["label"], "kind": "human"},
            {
                "id": "e2",
                "label": AGNES["label"],
                "kind": "human",
                "note": UNKNOWN_NOTE,
            },
        ]
        result = write_mapped(
            BatchWriter(NotedWiki()), batch_of(claim, entities=entities)
        )
        assert len(result.refused) == 1
        assert note_warnings(result) == []

    def test_a_record_no_claim_mentions_is_not_recorded(self):
        entities = [
            {"id": "e4", "label": WEDDING["label"], "kind": "civil_wedding"},
            {
                "id": "e9",
                "label": "Standesamt Flörburg",
                "kind": "organization",
                "note": UNKNOWN_NOTE,
            },
        ]
        result = write_mapped(
            BatchWriter(NotedWiki()),
            batch_of(wedding_date(), entities=entities),
        )
        (warning,) = note_warnings(result)
        assert warning.startswith(
            "entity 'e9' has a judgement note that holds U+0378"
        )
        assert warning.endswith(": it is not recorded")

    def test_a_vocabulary_without_the_note_terms_leaves_notes_out(self):
        """An instance whose vocabulary has no `judgement` (the unit tests'
        plain one): the notes are left out with one warning, and the claims
        and kinds written."""
        wiki = FakeWiki({JUDGMENT_SHA1: "Q23"})
        person = local("p1", "Leopold Löbig", "human")
        born = {
            "subject": person,
            "property": "date_of_birth",
            "value": {"type": "time", "year": 1914, "month": 5},
            "source": source(),
        }
        batch = batch_of(
            dict(born, note=DATE_NOTE),
            dict(born, source=source(page="2"), note=OTHER_DATE_NOTE),
            entities=[
                {
                    "id": "p1",
                    "label": person["label"],
                    "kind": "human",
                    "note": KIND_NOTE,
                }
            ],
        )
        result = write_mapped(BatchWriter(wiki), batch)
        assert (result.written, result.notes_written) == (1, 0)
        assert note_warnings(result) == [
            "the vocabulary lacks judgement, judgement_note, "
            "judgement_date_reading and judgement_kind: 3 judgement notes "
            "are left out, and their claims and kinds are written without "
            "them"
        ]


class TestARetiredQualifier:
    def test_it_is_refused(self):
        claim = dict(
            wedding_date(),
            qualifiers=[
                {
                    "property": "spouse",
                    "value": {"type": "item", "value": AGNES},
                }
            ],
        )
        batch = batch_of(claim)
        result = write_mapped(BatchWriter(NotedWiki()), batch)
        assert result.refused == [claim_id(batch.claims[0])]
        assert (
            f"claim {claim_id(batch.claims[0])} (point_in_time) is refused, "
            f"and nothing of it is written: its spouse qualifier is retired "
            f"and takes no claim (ADR-0036)"
        ) in result.warnings


def explicit_kind(note: dict | None = None, *, reverse=False, run=RUN):
    """The court's batch with an explicit `instance of` organization claim
    for the court, from the judgment's page 1, and the court's record
    noted `note`; the two claims in the batch's order or `reverse`d."""
    batch = TestAnEntitysNote().court_batch(note)
    kind = {
        "subject": COURT,
        "property": "instance_of",
        "value": {
            "type": "item",
            "value": {"kind": "vocab", "key": "organization"},
        },
        "source": source(excerpt=EXCERPT),
    }
    claims = [*batch.model_dump(mode="json")["claims"], kind]
    if reverse:
        claims.reverse()
    return batch_of(
        *claims,
        entities=batch.model_dump(mode="json")["entities"],
        run=run,
    )


class TestKindsAndCuratedStatementsInOneWrite:
    """The kinds are recorded before the claims that added nothing are
    looked at again, and a statement is compared by the artifacts and
    blocks it had before the write, as a curator's only while it still is
    one, so neither an explicit `instance of` claim nor a curated
    statement makes the blocks depend on the batch's order (#410)."""

    @pytest.mark.parametrize("reverse", [False, True])
    def test_an_explicit_kind_claim_and_a_later_runs_noted_kind(self, reverse):
        wiki = NotedWiki()
        earlier = explicit_kind()
        identity = mapped(earlier)
        first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
        later = explicit_kind(KIND_NOTE, reverse=reverse, run=LATER_RUN)
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        court = result.created_items["e6"]
        (kind,) = statements(wiki, court, "instance_of")
        assert run_blocks(kind) == [
            (first.run_qid, None, None),
            (result.run_qid, None, None),
            (result.run_qid, "Q40", KIND_NOTE["text"]),
        ]
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), later, identity=identity)
        assert again.notes_written == 0
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize(
        "x, y",
        [
            # X and Y from the burial register, Y bare on page 1
            (
                wedding_date(DATE_NOTE, sha1=BURIAL_SHA1),
                wedding_date(sha1=BURIAL_SHA1),
            ),
            (
                wedding_date(DATE_NOTE, sha1=BURIAL_SHA1, page="2"),
                wedding_date(sha1=BURIAL_SHA1),
            ),
            (
                dict(wedding_date(sha1=BURIAL_SHA1, page="2"), curated=True),
                wedding_date(sha1=BURIAL_SHA1),
            ),
            # X from the judgment, Y bare from the burial register, page 1
            (wedding_date(DATE_NOTE), wedding_date(sha1=BURIAL_SHA1)),
            (
                wedding_date(DATE_NOTE, page="2"),
                wedding_date(sha1=BURIAL_SHA1),
            ),
            (wedding_date(page="2"), wedding_date(sha1=BURIAL_SHA1)),
            (
                dict(wedding_date(page="2"), curated=True),
                wedding_date(sha1=BURIAL_SHA1),
            ),
        ],
        ids=[
            "burial: noted, page 1",
            "burial: noted, page 2",
            "burial: curated, page 2",
            "judgment: noted, page 1",
            "judgment: noted, page 2",
            "judgment: bare, page 2",
            "judgment: curated, page 2",
        ],
    )
    def test_a_curated_statement_and_a_later_runs_claims(self, x, y):
        """The curator's wedding date from the judgment's page 1, then a
        later run's claims X and Y, Y bare from the burial register's page
        1: the same blocks in both orders, and a rewrite writes nothing."""
        shown = []
        for reverse in (False, True):
            wiki = NotedWiki()
            curated = batch_of(dict(wedding_date(), curated=True))
            claims = [x, y]
            if reverse:
                claims.reverse()
            later = batch_of(*claims, run=LATER_RUN)
            identity = joined(mapped(curated, later), WEDDING["label"])
            write_mapped(BatchWriter(wiki), curated, identity=identity)
            result = write_mapped(BatchWriter(wiki), later, identity=identity)
            statement = the_wedding_date(wiki, result)
            shown.append(
                sorted(
                    json.dumps(block, sort_keys=True)
                    for block in blocks(statement)
                )
            )
            writes = wiki.wbi.item.writes
            write_mapped(BatchWriter(wiki), later, identity=identity)
            assert wiki.wbi.item.writes == writes, reverse
        assert shown[0] == shown[1]


def office_batch(note=None, sha1=JUDGMENT_SHA1, run=RUN) -> ClaimBatch:
    """The Wiedergutmachungsamt Peiltal, an unclassified entity with the
    type hint 'compensation office', carrying out the divorce (a fixture
    pairing, not a source's), cited to `sha1`."""
    office = local("o1", "Wiedergutmachungsamt Peiltal", "unclassified_entity")
    record = {
        "id": "o1",
        "label": office["label"],
        "kind": "unclassified_entity",
        "type_hint": "compensation office",
    }
    if note:
        record["note"] = note
    batch = batch_of(
        {
            "subject": DIVORCE,
            "property": "carried_out_by",
            "value": {"type": "item", "value": office},
            "source": source(),
        },
        entities=[
            {"id": "e5", "label": DIVORCE["label"], "kind": "divorce"},
            record,
        ],
        run=run,
    )
    return cited_to(batch, sha1)


class TestMoreKinds:
    def test_a_later_documents_type_hint_takes_a_bare_run_block(self):
        """Written without a note from the judgment, then from the burial
        register with one: the type hint gains the later run's bare block,
        and the kind its noted one."""
        wiki = NotedWiki()
        earlier = office_batch()
        later = office_batch(KIND_NOTE, sha1=BURIAL_SHA1, run=LATER_RUN)
        identity = joined(
            mapped(earlier, later),
            "Wiedergutmachungsamt Peiltal",
            DIVORCE["label"],
        )
        first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        office = result.created_items["o1"]
        assert office == first.created_items["o1"]
        (hint,) = statements(wiki, office, "type_hint")
        assert run_blocks(hint) == [
            (first.run_qid, None, None),
            (result.run_qid, None, None),
        ]
        (kind,) = statements(wiki, office, "instance_of")
        assert (result.run_qid, "Q40", KIND_NOTE["text"]) in run_blocks(kind)

    def test_an_equivalences_note_left_out_says_its_kind_is_recorded(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        wiki = NotedWiki()
        court_of = TestCuratedAndOtherStatements().court
        first = write_mapped(BatchWriter(wiki), court_of())
        court = first.created_items["e6"]
        later = apply_equivalences(
            court_of(UNKNOWN_NOTE, sha1=BURIAL_SHA1), {"e6": court}
        )
        result = write_mapped(BatchWriter(wiki), later)
        (warning,) = note_warnings(result)
        assert warning.startswith("entity 'e6' has a judgement note")
        assert warning.endswith(": its kind is recorded without it")

    def test_a_curators_kind_listed_first_is_passed_over(self):
        """The court's kinds: a curator's, citing the judgment, listed
        before the machine one. A later run's note joins the machine one,
        once, and a rewrite writes nothing."""
        wiki = NotedWiki()
        court_of = TestCuratedAndOtherStatements().court
        identity = joined(mapped(court_of()), COURT["label"])
        first = write_mapped(BatchWriter(wiki), court_of(), identity=identity)
        court = first.created_items["e6"]
        item = wiki.wbi.item.get(court)
        curated = BatchWriter._item_claim(
            NOTE_PROPERTIES["instance_of"], NOTE_ITEMS["organization"]
        )
        curated.references = BatchWriter(wiki)._references("Q23")
        BatchWriter._append(item, curated, beside_equal=True)
        kinds = item.claims.claims[NOTE_PROPERTIES["instance_of"]]
        kinds.insert(0, kinds.pop())
        item.write()
        noted = court_of(KIND_NOTE).model_dump(mode="json")
        later = batch_of(
            *noted["claims"], entities=noted["entities"], run=LATER_RUN
        )
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        assert result.notes_written == 1
        curators, machine = statements(wiki, court, "instance_of")
        assert run_blocks(curators) == []
        assert run_blocks(machine) == [
            (first.run_qid, None, None),
            (result.run_qid, "Q40", KIND_NOTE["text"]),
        ]
        writes = wiki.wbi.item.writes
        again = write_mapped(BatchWriter(wiki), later, identity=identity)
        assert again.notes_written == 0
        assert wiki.wbi.item.writes == writes


class WikiWithoutDateReadings(NotedWiki):
    """An instance whose vocabulary lacks the `judgement_date_reading`
    item, so a date reading's note is left out and its claim's run block
    goes bare."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        del items["judgement_date_reading"]
        return properties, items


@pytest.mark.parametrize("reverse", [False, True])
def test_a_note_its_vocabulary_cannot_hold_meets_a_noted_claim(reverse):
    """A later run's date-reading note, left out on this instance, and its
    claim of another category, noted: the bare and the noted run block
    both land, in either order (#410)."""
    wiki = WikiWithoutDateReadings()
    first = write_mapped(BatchWriter(wiki), batch_of(wedding_date()))
    other = {"category": "other", "text": "the date read as the wedding's"}
    claims = [wedding_date(DATE_NOTE), wedding_date(other)]
    if reverse:
        claims.reverse()
    later = batch_of(*claims, run=LATER_RUN)
    result = write_mapped(BatchWriter(wiki), later)
    assert run_blocks(the_wedding_date(wiki, result)) == [
        (first.run_qid, None, None),
        (result.run_qid, None, None),
        (result.run_qid, NOTE_ITEMS["judgement_other"], other["text"]),
    ]


IDENTITY_NOTE = {
    "category": "identity",
    "text": "the court of the second page read as the judgment's own court",
}


class WikiWithoutKindNotes(NotedWiki):
    """An instance whose vocabulary lacks the `judgement_kind` item, so a
    kind note is left out and its witness's run block goes bare."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        del items["judgement_kind"]
        return properties, items


@pytest.mark.parametrize("reverse", [False, True])
def test_a_kind_note_its_vocabulary_cannot_hold_meets_a_noted_one(reverse):
    """The kinds' mirror of the claims' test above: a later run's kind
    note, left out on this instance, and an identity note, written, give
    the same blocks in either order, and a rewrite writes nothing."""
    wiki = WikiWithoutKindNotes()
    earlier = court_pair(None, None)
    identity = joined(mapped(earlier), COURT["label"])
    first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
    later = court_pair(
        KIND_NOTE, IDENTITY_NOTE, reverse=reverse, run=LATER_RUN
    )
    result = write_mapped(BatchWriter(wiki), later, identity=identity)
    (kind,) = statements(wiki, result.created_items["e6"], "instance_of")
    assert run_blocks(kind) == [
        (first.run_qid, None, None),
        (result.run_qid, None, None),
        (result.run_qid, "Q43", IDENTITY_NOTE["text"]),
    ]
    writes = wiki.wbi.item.writes
    write_mapped(BatchWriter(wiki), later, identity=identity)
    assert wiki.wbi.item.writes == writes


def curated_and_bare(sha1=JUDGMENT_SHA1, run=RUN, reverse=False):
    """A curated wedding date and a machine one, both bare, from one place
    of `sha1`'s page 1, in the batch's order or `reverse`d."""
    claims = [
        dict(wedding_date(sha1=sha1), curated=True),
        wedding_date(sha1=sha1),
    ]
    if reverse:
        claims.reverse()
    return batch_of(*claims, run=run)


class TestACuratedAndABareClaimFromOnePlace:
    """A curated claim beside a bare machine claim from the same place
    gives the same blocks in either order, fresh or on a statement an
    earlier run wrote, and a rewrite writes nothing: a claim's source
    block counts as carried only where the statement carried it before
    the write."""

    def shown(self, wiki, result) -> list[str]:
        return sorted(
            json.dumps(block, sort_keys=True)
            for block in blocks(the_wedding_date(wiki, result))
        )

    def test_fresh(self):
        shown = []
        for reverse in (False, True):
            wiki = NotedWiki()
            batch = curated_and_bare(reverse=reverse)
            identity = mapped(batch)
            result = write_mapped(BatchWriter(wiki), batch, identity=identity)
            shown.append(self.shown(wiki, result))
            # the bare claim's run block is added, in either order
            assert run_blocks(the_wedding_date(wiki, result)) == [
                (result.run_qid, None, None)
            ]
            writes = wiki.wbi.item.writes
            write_mapped(BatchWriter(wiki), batch, identity=identity)
            assert wiki.wbi.item.writes == writes, reverse
        assert shown[0] == shown[1]

    def test_after_an_earlier_machine_run(self):
        shown = []
        for reverse in (False, True):
            wiki = NotedWiki()
            earlier = batch_of(wedding_date(sha1=BURIAL_SHA1))
            later = curated_and_bare(run=LATER_RUN, reverse=reverse)
            identity = joined(mapped(earlier, later), WEDDING["label"])
            write_mapped(BatchWriter(wiki), earlier, identity=identity)
            result = write_mapped(BatchWriter(wiki), later, identity=identity)
            shown.append(self.shown(wiki, result))
            writes = wiki.wbi.item.writes
            write_mapped(BatchWriter(wiki), later, identity=identity)
            assert wiki.wbi.item.writes == writes, reverse
        assert shown[0] == shown[1]

    def test_an_explicit_kind_after_an_earlier_run(self):
        """An earlier run gives the court its kind from the mention alone,
        so the kind statement lacks the explicit claims' page-1 block
        before the later write: a curated and a bare explicit `instance
        of` claim from that place give the same blocks in either order,
        the bare one's run block among them, and a rewrite writes
        nothing."""
        shown = []
        for reverse in (False, True):
            wiki = NotedWiki()
            earlier = TestAnEntitysNote().court_batch(None)
            identity = mapped(earlier)
            first = write_mapped(BatchWriter(wiki), earlier, identity=identity)
            plain = explicit_kind().model_dump(mode="json")
            kind = plain["claims"][-1]
            claims = [
                *plain["claims"][:-1],
                dict(kind, curated=True),
                kind,
            ]
            if reverse:
                claims.reverse()
            later = batch_of(
                *claims, entities=plain["entities"], run=LATER_RUN
            )
            result = write_mapped(BatchWriter(wiki), later, identity=identity)
            (statement,) = statements(
                wiki, result.created_items["e6"], "instance_of"
            )
            assert run_blocks(statement) == [
                (first.run_qid, None, None),
                (result.run_qid, None, None),
            ]
            shown.append(
                sorted(
                    json.dumps(block, sort_keys=True)
                    for block in blocks(statement)
                )
            )
            writes = wiki.wbi.item.writes
            write_mapped(BatchWriter(wiki), later, identity=identity)
            assert wiki.wbi.item.writes == writes, reverse
        assert shown[0] == shown[1]


class TestTheStateBeforeTheWrite:
    """_before_write keeps each statement's state under its subject,
    property, value and qualifiers alike, and afresh for each write."""

    @pytest.mark.parametrize(
        "differs", ["subject", "property", "value", "qualifiers"]
    )
    def test_a_statement_differing_in_one_part_is_its_own(self, differs):
        wiki = NotedWiki()
        curated = batch_of(dict(wedding_date(), curated=True))
        other = wedding_date(DATE_NOTE, sha1=BURIAL_SHA1)
        if differs == "subject":
            other["subject"] = DIVORCE
        elif differs == "property":
            other["property"] = "start_time"
        elif differs == "value":
            other["value"] = {"type": "time", "year": 1948}
        else:
            other["qualifiers"] = [
                {
                    "property": "location",
                    "value": {"type": "item", "value": FLOERBURG},
                }
            ]
        second = dict(other, note=OTHER_DATE_NOTE)
        # the other statement first, then the curated one's own place
        later = batch_of(other, second, wedding_date(), run=LATER_RUN)
        identity = joined(mapped(curated, later), WEDDING["label"])
        first = write_mapped(BatchWriter(wiki), curated, identity=identity)
        before = blocks(the_wedding_date(wiki, first))
        result = write_mapped(BatchWriter(wiki), later, identity=identity)
        wedding = result.created_items["e4"]
        dates = statements(wiki, wedding, "point_in_time")
        curated_date = next(
            s
            for s in dates
            if s.mainsnak.datavalue["value"]["time"].startswith("+1947-02-08")
            and not s.qualifiers.qualifiers
        )
        assert blocks(curated_date) == before
        if differs == "value":
            (later_date,) = [s for s in dates if s is not curated_date]
            assert run_blocks(later_date) == sorted(
                [
                    (result.run_qid, "Q42", DATE_NOTE["text"]),
                    (result.run_qid, "Q42", OTHER_DATE_NOTE["text"]),
                ]
            )

    def test_one_writer_reads_each_write_afresh(self):
        """One BatchWriter writes the curated wedding date, then a later
        run's bare date from the judgment's page 2: the second write reads
        the statement as curated, and it gains nothing."""
        wiki = NotedWiki()
        writer = BatchWriter(wiki)
        curated = batch_of(dict(wedding_date(), curated=True))
        later = batch_of(wedding_date(page="2"), run=LATER_RUN)
        identity = joined(mapped(curated, later), WEDDING["label"])
        first = write_mapped(writer, curated, identity=identity)
        before = blocks(the_wedding_date(wiki, first))
        result = write_mapped(writer, later, identity=identity)
        assert (result.skipped, result.referenced) == (1, 0)
        assert blocks(the_wedding_date(wiki, result)) == before

    def test_a_dry_run_records_no_kinds(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        wiki = NotedWiki()
        court_of = TestCuratedAndOtherStatements().court
        first = write_mapped(BatchWriter(wiki), court_of())
        court = first.created_items["e6"]
        writes = wiki.wbi.item.writes
        later = apply_equivalences(
            court_of(KIND_NOTE, sha1=BURIAL_SHA1), {"e6": court}
        )
        write_mapped(BatchWriter(wiki, dry_run=True), later)
        assert wiki.wbi.item.writes == writes
