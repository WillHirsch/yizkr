"""Deaths and births on the person: what validation, salvage, the
manifest parse and the writer refuse (ADR-0040 §2, §3, §5, §7, §8).

Every example comes from the fictional universe (docs/EXAMPLES.md): the
1909 burial register ("Witwer der Cäcilie Löbig geb. Tarle"), Gustav's
birth register extract, Rudolf's 1941 death certificate and the 1958
letter, and a hypothetical "der selige Felix".
"""

from __future__ import annotations

import json

import pytest

from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki
from tests.unit.test_writer_notes_unions import batch_of
from yizkr.claims import (
    END_NOT_PARTNER,
    LEFT_OUT_PERSON,
    PARENTAGE_PROPERTY,
    REJECT_REASONS,
    UNKNOWN_EXCERPT,
    UNKNOWN_VALUE,
    VITAL_KIND,
    VITAL_REASONS,
    VITAL_SUBJECT,
    WIDOWED_END,
    ClaimBatch,
    UnknownValue,
    reject_reasons,
    validate_against_manifest,
    vital_problems,
)
from yizkr.model.schema import load_acceptance
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.extract import (
    entity_kind_prompt_lines,
    salvage_valid_claims,
)
from yizkr.pipeline.writer import BatchWriter, pre_plan_refusals

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1",
    "started_at": "2026-10-24T10:00:00+00:00",
}
BURIAL = "b" * 40
WITWER = "Witwer der Cäcilie Löbig geb. Tarle"

ENTITIES = [
    {"id": "eugen", "label": "Eugen Löbig", "kind": "human"},
    {"id": "caecilie", "label": "Cäcilie Löbig", "kind": "human"},
    {"id": "felix", "label": "Felix Löbig", "kind": "human"},
    {
        "id": "marriage",
        "label": "marriage of Eugen and Cäcilie Löbig",
        "kind": "marriage",
    },
    {"id": "friedhof", "label": "Friedhof Flörburg", "kind": "place"},
    {"id": "wedding", "label": "the 1905 wedding", "kind": "wedding"},
    # the register's pastor, who only certifies the entry (§2.8)
    {
        "id": "pastor",
        "label": "Pfarrer Gerlach",
        "kind": "human",
        "extract": False,
    },
]


def local(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def item(local_id: str) -> dict:
    return {"type": "item", "value": local(local_id)}


def vocab(key: str) -> dict:
    return {"type": "item", "value": {"kind": "vocab", "key": key}}


UNKNOWN = {"type": "unknown"}


def claim(
    subject: str,
    prop: str,
    value: dict,
    *qualifiers: tuple[str, dict],
    excerpt: str | None = WITWER,
) -> dict:
    source: dict = {"artifact": {"kind": "artifact", "sha1": BURIAL}}
    if excerpt is not None:
        source["transcription_excerpt"] = {"text": excerpt, "language": "de"}
    return {
        "subject": local(subject),
        "property": prop,
        "value": value,
        "qualifiers": [
            {"property": key, "value": given} for key, given in qualifiers
        ],
        "source": source,
    }


def batch(*claims: dict, entities=ENTITIES) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "claims": list(claims), "entities": entities}
    )


def codes(*claims: dict, entities=ENTITIES) -> list[str]:
    return reject_reasons(
        validate_against_manifest(batch(*claims, entities=entities))
    )


def spouse(person: str) -> dict:
    return claim(
        "marriage",
        "participant",
        item(person),
        ("object_has_role", vocab("spouse")),
    )


WIDOWED = claim("eugen", "civil_status", vocab("widowed"))
ENDED = claim("marriage", "ended_by_death_of", item("caecilie"))


class TestTheManifest:
    def test_the_reasons_are_reject_reasons(self):
        assert set(VITAL_REASONS) <= set(REJECT_REASONS)

    def test_the_unknown_value_properties(self):
        assert load_acceptance().unknown_values == {
            "date_of_death",
            "burial_place",
        }

    def test_the_death_and_birth_properties_are_a_persons(self):
        subjects = load_acceptance().subjects
        for prop in (
            "date_of_death",
            "place_of_death",
            "cause_of_death",
            "burial_place",
            "age_at_death",
            "date_of_birth",
            "place_of_birth",
        ):
            assert subjects[prop] == {"human", "unclassified_entity"}, prop

    def test_no_extraction_makes_a_birth_or_a_death(self):
        assert load_acceptance().unextracted_kinds == {"birth", "death"}
        kinds = entity_kind_prompt_lines()[0].split(", ")
        assert "birth" not in kinds and "death" not in kinds
        assert "deportation" in kinds


class TestTheUnknownValue:
    def test_a_death_stated_without_its_date(self):
        # "der selige Felix" (hypothetical), with the words as excerpt
        late = claim(
            "felix", "date_of_death", UNKNOWN, excerpt="der selige Felix"
        )
        assert codes(late) == []
        assert isinstance(batch(late).claims[0].value, UnknownValue)

    def test_a_burial_stated_without_its_place(self):
        buried = claim(
            "eugen",
            "burial_place",
            UNKNOWN,
            (
                "point_in_time",
                {"type": "time", "year": 1909, "month": 3, "day": 20},
            ),
            excerpt="Beerdigt: 20. März 1909.",
        )
        assert codes(buried) == []

    @pytest.mark.parametrize("prop", ["date_of_birth", "place_of_death"])
    def test_no_other_property_takes_one(self, prop):
        assert codes(claim("felix", prop, UNKNOWN)) == [UNKNOWN_VALUE]

    @pytest.mark.parametrize(
        "bound", ["latest_date", "earliest_date", "sourcing_circumstances"]
    )
    def test_it_carries_no_bound(self, bound):
        value = (
            vocab("circa")
            if bound == "sourcing_circumstances"
            else {"type": "time", "year": 1938}
        )
        late = claim("felix", "date_of_death", UNKNOWN, (bound, value))
        assert codes(late) == [UNKNOWN_VALUE]

    @pytest.mark.parametrize(
        "dated", ["point_in_time", "start_time", "end_time"]
    )
    def test_a_date_of_death_carries_no_date(self, dated):
        # an unknown date of death states none (ADR-0040 §2.2); an unknown
        # place of burial keeps its burial's own date (Q3, above)
        late = claim(
            "felix",
            "date_of_death",
            UNKNOWN,
            (dated, {"type": "time", "year": 1909}),
        )
        assert codes(late) == [UNKNOWN_VALUE]

    def test_no_qualifier_takes_one(self):
        dated = claim(
            "wedding", "location", item("friedhof"), ("point_in_time", UNKNOWN)
        )
        assert codes(dated) == [UNKNOWN_VALUE]

    def test_it_needs_its_excerpt(self):
        # Q10: nothing else shows what the source said
        late = claim("felix", "date_of_death", UNKNOWN, excerpt=None)
        assert codes(late) == [UNKNOWN_EXCERPT]
        blank = claim("felix", "date_of_death", UNKNOWN, excerpt=" ")
        assert codes(blank) == [UNKNOWN_EXCERPT]


class TestSubjects:
    @pytest.mark.parametrize(
        ("prop", "value"),
        [
            # Q11: the 1905 certificate's "geboren am 15. Februar 1862",
            # put on the wedding rather than on Sebastian
            (
                "date_of_birth",
                {"type": "time", "year": 1862, "month": 2, "day": 15},
            ),
            ("date_of_death", {"type": "time", "year": 1909}),
            ("age_at_death", {"type": "string", "value": "73 Jahre"}),
            ("burial_place", item("friedhof")),
        ],
    )
    def test_a_death_or_birth_property_is_a_persons(self, prop, value):
        assert codes(claim("wedding", prop, value)) == [VITAL_SUBJECT]
        assert codes(claim("eugen", prop, value)) == []

    def test_an_unclassified_entity_takes_one(self):
        entities = [
            *ENTITIES,
            {"id": "u1", "label": "Löbig", "kind": "unclassified_entity"},
        ]
        found = codes(
            claim("u1", "date_of_death", {"type": "time", "year": 1909}),
            entities=entities,
        )
        assert found == []


class TestAKindOnlyACuratorMakes:
    @pytest.mark.parametrize("kind", ["birth", "death"])
    def test_an_entity_of_it_is_refused(self, kind):
        entities = [
            *ENTITIES,
            {"id": "d1", "label": "Death of Rudolf", "kind": kind},
        ]
        principal = claim(
            "d1",
            "participant",
            item("eugen"),
            ("object_has_role", vocab("principal")),
        )
        problems = validate_against_manifest(
            batch(principal, entities=entities)
        )
        assert any(p.startswith("entities[") for p in problems)
        assert reject_reasons(problems) == [VITAL_KIND]

    def test_a_ref_typed_as_one_is_refused(self):
        typed = claim("eugen", "residence", item("friedhof"))
        typed["value"]["value"] = {
            "kind": "local",
            "local_id": "d9",
            "instance_of": "death",
        }
        problems = vital_problems(batch(typed, entities=[]))
        assert [p[-12:] for p in problems[0]] == [f"[{VITAL_KIND}]"]


class TestAQidSubject:
    """A subject a curator confirmed as an existing item keeps the kind
    of the mention it was confirmed from (Q11)."""

    def test_a_confirmed_wedding_takes_no_date_of_death(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        dated = claim(
            "wedding", "date_of_death", {"type": "time", "year": 1909}
        )
        confirmed = apply_equivalences(batch(dated), {"wedding": "Q500"})
        assert confirmed.claims[0].subject.qid == "Q500"
        assert reject_reasons(validate_against_manifest(confirmed)) == [
            VITAL_SUBJECT
        ]
        assert pre_plan_refusals(confirmed, {})[0][0] == "vital_rule"

    def test_its_confirmed_kind_stands_without_its_record(self):
        # a batch whose entity record is gone keeps the kind the claim
        # records it was confirmed from
        dated = {
            **claim(
                "wedding", "date_of_death", {"type": "time", "year": 1909}
            ),
            "subject": {"kind": "qid", "qid": "Q503"},
            "confirmed_from": {
                "kind": "local",
                "local_id": "wedding",
                "instance_of": "wedding",
            },
        }
        assert codes(dated, entities=[]) == [VITAL_SUBJECT]

    def test_a_confirmed_person_takes_one(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        dated = claim("eugen", "date_of_death", {"type": "time", "year": 1909})
        confirmed = apply_equivalences(batch(dated), {"eugen": "Q501"})
        assert reject_reasons(validate_against_manifest(confirmed)) == []

    def test_a_qid_subject_of_an_entity_record_has_its_kind(self):
        dated = {
            **claim(
                "wedding", "date_of_death", {"type": "time", "year": 1909}
            ),
            "subject": {"kind": "qid", "qid": "Q502"},
        }
        entities = [
            *ENTITIES,
            {
                "id": "w2",
                "label": "a wedding",
                "kind": "wedding",
                "qid": "Q502",
            },
        ]
        assert codes(dated, entities=entities) == [VITAL_SUBJECT]


class TestAClassingClaim:
    @pytest.mark.parametrize("prop", ["instance_of", "subclass_of"])
    @pytest.mark.parametrize("kind", ["birth", "death"])
    def test_it_names_no_kind_only_a_curator_makes(self, prop, kind):
        # an event typed only `event` in its record, made a death by a
        # claim (Q8)
        entities = [
            *ENTITIES,
            {"id": "e9", "label": "Death of Rudolf", "kind": "event"},
        ]
        typed = claim("e9", prop, vocab(kind))
        assert codes(typed, entities=entities) == [VITAL_KIND]
        assert (
            pre_plan_refusals(batch(typed, entities=entities), {})[0][0]
            == "vital_rule"
        )


class TestUnionEnds:
    def test_a_witness_is_none_of_the_couple(self):
        # a wedding's witness takes part in the union too, but only the
        # spouses are its couple (ADR-0036)
        witness = claim(
            "marriage",
            "participant",
            item("felix"),
            ("object_has_role", vocab("witness")),
        )
        ended = claim("marriage", "ended_by_death_of", item("felix"))
        assert codes(spouse("eugen"), spouse("caecilie"), witness, ended) == [
            END_NOT_PARTNER
        ]

    def test_a_partner_is_one_by_their_qid_or_their_local_id(self):
        # Eugen confirmed as Q7: the union end and the widowhood name him
        # by QID, his participation by local id (Q9, §2.5)
        entities = [
            {**entity, "qid": "Q7"} if entity["id"] == "eugen" else entity
            for entity in ENTITIES
        ]
        by_qid = {"type": "item", "value": {"kind": "qid", "qid": "Q7"}}
        ended = claim("marriage", "ended_by_death_of", by_qid)
        assert (
            codes(
                spouse("eugen"), spouse("caecilie"), ended, entities=entities
            )
            == []
        )
        widowed = {
            **WIDOWED,
            "subject": {"kind": "qid", "qid": "Q7"},
        }
        wrong = claim("marriage", "ended_by_death_of", item("eugen"))
        assert codes(
            spouse("eugen"),
            spouse("caecilie"),
            wrong,
            widowed,
            entities=entities,
        ) == [WIDOWED_END]

    def test_with_no_roles_every_participant_is_of_the_couple(self):
        given = [
            claim("marriage", "participant", item(person))
            for person in ("eugen", "caecilie")
        ]
        assert codes(*given, ENDED) == []
        stranger = claim("marriage", "ended_by_death_of", item("felix"))
        assert codes(*given, stranger) == [END_NOT_PARTNER]

    def test_the_burial_registers_line(self):
        # "Witwer der Cäcilie": the union ended by her death, his status
        assert codes(spouse("eugen"), spouse("caecilie"), ENDED, WIDOWED) == []

    def test_the_guard_refuses_the_survivor(self):
        # the value the same batch calls widowed (§2.5)
        wrong = claim("marriage", "ended_by_death_of", item("eugen"))
        assert codes(spouse("eugen"), spouse("caecilie"), wrong, WIDOWED) == [
            WIDOWED_END
        ]

    def test_the_guard_needs_no_couple(self):
        wrong = claim("marriage", "ended_by_death_of", item("eugen"))
        assert codes(wrong, WIDOWED) == [WIDOWED_END]

    def test_one_of_the_couple(self):
        stranger = claim("marriage", "ended_by_death_of", item("felix"))
        assert codes(spouse("eugen"), spouse("caecilie"), stranger) == [
            END_NOT_PARTNER
        ]

    def test_a_batch_that_names_no_couple_is_not_checked(self):
        assert (
            codes(claim("marriage", "ended_by_death_of", item("felix"))) == []
        )

    def test_salvage_applies_them_to_the_batch(self):
        # one by one each claim passes; together the guard refuses one
        wrong = claim("marriage", "ended_by_death_of", item("eugen"))
        valid, rejects = salvage_valid_claims(
            [spouse("eugen"), spouse("caecilie"), wrong, WIDOWED],
            RUN,
            entities=ENTITIES,
        )
        assert len(valid.claims) == 3
        (reject,) = rejects
        assert reject["claim"] == wrong
        assert reject["reasons"] == [WIDOWED_END]


class TestParentage:
    def test_it_qualifies_a_parent(self):
        adopted = claim(
            "felix",
            "father",
            item("eugen"),
            ("parentage", vocab("parentage_adoptive")),
            ("kinship_type", {"type": "string", "value": "Adoptivvater"}),
        )
        assert codes(adopted) == []

    def test_and_nothing_else(self):
        wrong = claim(
            "felix",
            "sibling",
            item("eugen"),
            ("parentage", vocab("parentage_foster")),
        )
        assert codes(wrong) == [PARENTAGE_PROPERTY]


class TestALeftOutPerson:
    def test_no_claim_takes_one_as_its_value(self):
        # the pastor who only certifies the entry is no author (Q6)
        signed = {
            **claim("eugen", "author", item("pastor")),
            "subject": {"kind": "artifact", "sha1": BURIAL},
        }
        assert codes(signed) == [LEFT_OUT_PERSON]

    @pytest.mark.parametrize("prop", ["author", "residence", "relative"])
    def test_nor_once_a_curator_confirms_them_as_an_item(self, prop):
        from yizkr.pipeline.reconcile import apply_equivalences

        named = claim("eugen", prop, item("pastor"))
        if prop == "author":
            named["subject"] = {"kind": "artifact", "sha1": BURIAL}
        confirmed = apply_equivalences(batch(named), {"pastor": "Q600"})
        assert confirmed.claims[0].value.value.qid == "Q600"
        assert LEFT_OUT_PERSON in reject_reasons(
            validate_against_manifest(confirmed)
        )
        assert pre_plan_refusals(confirmed, {})[0][0] == "vital_rule"

    def test_nor_as_a_qualifiers_value(self):
        named = claim(
            "eugen",
            "residence",
            item("friedhof"),
            ("said_to_be_the_same_as", item("pastor")),
        )
        assert LEFT_OUT_PERSON in codes(named)


class TestTheManifestParse:
    def test_a_death_entity_is_dropped_alone(self):
        # Q8: the 1941 certificate's manifest lists "Death of Rudolf"
        answer = json.dumps(
            {
                "entities": [
                    {"id": "e1", "label": "Rudolf Löbig", "kind": "human"},
                    {"id": "e2", "label": "Death of Rudolf", "kind": "death"},
                ]
            }
        )
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4
        )
        assert problems == []
        assert [entity.id for entity in parsed.entities] == ["e1"]
        assert parsed.dropped_kinds == {"e2": "death"}

    def test_an_id_given_twice_is_a_problem_whatever_its_kinds(self):
        # Rudolf and "Death of Rudolf" both given as e1: the manifest is
        # refused, never emptied
        answer = json.dumps(
            {
                "entities": [
                    {"id": "e1", "label": "Rudolf Löbig", "kind": "human"},
                    {"id": "e1", "label": "Death of Rudolf", "kind": "death"},
                ]
            }
        )
        _, problems = ChunkedClaimExtractor._parse_entity_manifest(answer, 4)
        assert problems == ["duplicate entity id 'e1'"]

    def test_its_claims_are_rejects_of_their_own(self):
        principal = claim(
            "d1",
            "participant",
            item("eugen"),
            ("object_has_role", vocab("principal")),
        )
        valid, rejects = salvage_valid_claims(
            [principal, WIDOWED],
            RUN,
            entities=ENTITIES,
            dropped={"d1": "death"},
        )
        assert len(valid.claims) == 1
        (reject,) = rejects
        assert reject["reasons"] == [VITAL_KIND]


class UnknownWiki(FakeWiki):
    """The unit fake, with a date of death and a place of burial."""

    def vocabulary(self):
        return (
            dict(PROPERTIES) | {"date_of_death": "P60", "burial_place": "P61"},
            dict(ITEMS),
        )


class TestTheWriter:
    def test_it_refuses_what_validation_refuses(self):
        wrong = claim("marriage", "ended_by_death_of", item("eugen"))
        refusals = pre_plan_refusals(batch(wrong, WIDOWED), {})
        assert list(refusals) == [0]
        assert refusals[0][0] == "vital_rule"
        assert refusals[0][1].endswith(f"[{WIDOWED_END}]")

    @staticmethod
    def late_felix() -> dict:
        source = {
            "artifact": {"kind": "artifact", "sha1": "1" * 40},
            "transcription_excerpt": {
                "text": "der selige Felix",
                "language": "de",
            },
        }
        return {
            "subject": {
                "kind": "local",
                "local_id": "e1",
                "label": "Felix Löbig",
                "instance_of": "human",
            },
            "property": "date_of_death",
            "value": UNKNOWN,
            "source": source,
        }

    def test_an_unknown_value_is_a_somevalue_snak(self):
        from tests.fixtures.identity import write_mapped

        wiki = UnknownWiki({"1" * 40: "Q900"}, detached=True)
        given = batch_of(self.late_felix())
        result = write_mapped(BatchWriter(wiki), given)
        assert (result.written, result.refused) == (1, [])
        felix = result.created_items["e1"]
        (statement,) = wiki.wbi.item.get(felix).claims.get("P60")
        assert statement.mainsnak.snaktype.value == "somevalue"
        # a second write writes no copy
        again = write_mapped(BatchWriter(wiki), batch_of(self.late_felix()))
        assert (
            len(wiki.wbi.item.get(again.created_items["e1"]).claims.get("P60"))
            == 1
        )

    def test_an_unknown_value_carries_both_references_and_its_excerpt(self):
        from tests.fixtures.identity import write_mapped
        from tests.unit.test_pipeline import _cited

        wiki = UnknownWiki({"1" * 40: "Q900"}, detached=True)
        result = write_mapped(BatchWriter(wiki), batch_of(self.late_felix()))
        (statement,) = wiki.wbi.item.get(
            result.created_items["e1"]
        ).claims.get("P60")
        assert _cited(statement, "stated_in") == ["Q900"]
        assert _cited(statement, "inferred_from_extraction_run") == [
            result.run_qid
        ]
        (excerpt,) = [
            snak
            for block in statement.references.references
            for snak in block.snaks.get(PROPERTIES["transcription_excerpt"])
            or []
        ]
        assert excerpt.datavalue["value"]["text"] == "der selige Felix"

    def test_an_unknown_place_of_burial_is_a_somevalue_snak(self):
        from tests.fixtures.identity import write_mapped

        buried = {
            **self.late_felix(),
            "property": "burial_place",
            "qualifiers": [
                {
                    "property": "point_in_time",
                    "value": {"type": "time", "year": 1931},
                }
            ],
        }
        buried["source"]["transcription_excerpt"]["text"] = "beerdigt 1931"
        wiki = UnknownWiki({"1" * 40: "Q900"}, detached=True)
        result = write_mapped(BatchWriter(wiki), batch_of(buried))
        assert (result.written, result.refused) == (1, [])
        (statement,) = wiki.wbi.item.get(
            result.created_items["e1"]
        ).claims.get("P61")
        assert statement.mainsnak.snaktype.value == "somevalue"

    def test_an_unknown_value_never_matches_a_no_value(self):
        """The writer compares snak types: a curator's "no value", which
        has no value as an unknown value has none, is never the unknown
        value's statement, so the unknown value is written beside it."""
        from wikibaseintegrator.wbi_enums import WikibaseSnakType

        from tests.fixtures.identity import write_mapped

        wiki = UnknownWiki({"1" * 40: "Q900"}, detached=True)
        first = write_mapped(BatchWriter(wiki), batch_of(self.late_felix()))
        felix = first.created_items["e1"]
        # a curator makes the statement a "no value"
        entity = wiki.wbi.item.get(felix)
        (statement,) = entity.claims.get("P60")
        statement.mainsnak.snaktype = WikibaseSnakType.NO_VALUE
        entity.write()
        again = write_mapped(BatchWriter(wiki), batch_of(self.late_felix()))
        assert again.written == 1
        assert sorted(
            each.mainsnak.snaktype.value
            for each in wiki.wbi.item.get(felix).claims.get("P60")
        ) == ["novalue", "somevalue"]
