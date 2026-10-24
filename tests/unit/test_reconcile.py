"""Offline tests for entity reconciliation (ADR-0007). No instance, no LLM."""

import json
import re
import urllib.parse

import pytest

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    EntityRecord,
    LocalRef,
    PlaceStructure,
    QidRef,
)
from yizkr.pipeline.reconcile import (
    GRAPH_ADJUDICATION_PROMPT,
    PairFacts,
    PlaceContainment,
    Proposal,
    RosterEntity,
    _batch_pair_facts,
    _roster_pair_facts,
    adjudicate,
    apply_equivalences,
    block_candidates,
    entity_facts,
    export_equivalences_csv,
    fetch_place_containment,
    impossible_pair,
    propose_graph_pairs,
    ratify_equivalence_verdicts,
    read_approved_equivalences,
    read_place_containment,
)

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-13T09:00:00Z",
}

# the bytes of a fictional scan, which the claims below cite by
# their SHA-1 (#102)
SCAN = ArtifactRef.from_bytes(b"fictional scan: a Loebig family document")


def entities() -> list[EntityRecord]:
    return [
        EntityRecord(
            id="e1",
            label="Hildegard Löbig",
            aliases=["Hilde"],
            kind="human",
            summary="married Sebastian Löbig in 1905",
        ),
        EntityRecord(id="e2", label="Flörburg", kind="place"),
    ]


def roster() -> list[RosterEntity]:
    return [
        RosterEntity(
            qid="Q40",
            label="Hildegard Laudler",
            # an earlier document's form of her name, kept as an alias
            aliases=["Hildegard Löbig"],
            kind="human",
            birth="1876-03-19",
            spouses=["Sebastian Löbig"],
        ),
        # a different person whose nickname the batch also gives
        RosterEntity(
            qid="Q77", label="Ursula Löbig", nicknames=["Hilde"], kind="human"
        ),
        RosterEntity(qid="Q90", label="FLÖRBURG", kind="place"),
        RosterEntity(qid="Q99", label="Unrelated Person", kind="human"),
    ]


def batch(claims=(), entity_list=None) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": list(claims),
            "entities": [e.model_dump() for e in (entity_list or entities())],
        }
    )


def spouse_claim() -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Hildegard Löbig",
        },
        "property": "residence",
        "value": {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": "e2",
                "label": "Flörburg",
            },
        },
        "source": {"artifact": SCAN.model_dump()},
    }


class ScriptedLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


def name_claim(prop: str, text: str, subject: str = "e1") -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": subject,
            "label": "Hildegard Löbig",
        },
        "property": prop,
        "value": {
            "type": "monolingual",
            "value": {"text": text, "language": "en"},
        },
        "source": {"artifact": SCAN.model_dump()},
    }


class TestTypedNameEvidence:
    """ADR-0011: typed names surface as adjudication evidence, both sides."""

    def test_roster_facts_include_typed_names(self):
        entity = RosterEntity(
            qid="Q40",
            label="Hildegard Löbig",
            kind="human",
            birth_names=["Hildegard Laudler"],
            married_names=["Hildegard Löbig"],
            nicknames=["Hilde"],
        )
        facts = entity.facts()
        assert "birth name: Hildegard Laudler" in facts
        assert "married name: Hildegard Löbig" in facts
        assert "nickname: Hilde" in facts

    def test_entity_facts_renders_monolingual_names_plainly(self):
        facts = entity_facts(
            batch(claims=[name_claim("birth_name", "Hildegard Laudler")]),
            "e1",
        )
        assert "birth_name: Hildegard Laudler" in facts
        assert "language=" not in facts  # not a pydantic repr

    def test_batch_name_claim_reaches_the_adjudication_prompt(self):
        llm = ScriptedLlm(json.dumps({"done": True, "proposals_emitted": 0}))
        source = batch(claims=[name_claim("birth_name", "Hildegard Laudler")])
        adjudicate(
            llm, entities(), block_candidates(entities(), roster()), source
        )
        # The bare name is not enough: Q40's roster label already puts it
        # in the candidates block (#223). Only the batch facts line renders
        # the typed claim as "birth_name: ..." (roster facts say
        # "birth name: ...").
        assert "birth_name: Hildegard Laudler" in llm.prompts[0]


class TestBlocking:
    def test_kind_and_a_shared_whole_name(self):
        candidates = block_candidates(entities(), roster())
        # Q40 by an alias, Q77 by a nickname statement, Q90 by its label
        # in capitals; Q99 shares nothing
        assert [c.qid for c in candidates["e1"]] == ["Q40", "Q77"]
        assert [c.qid for c in candidates["e2"]] == ["Q90"]

    def test_no_candidates_no_entry(self):
        lone = [EntityRecord(id="e9", label="Zzyzx Qwx", kind="human")]
        assert block_candidates(lone, roster()) == {}

    def test_a_shared_word_alone_never_blocks(self):
        """A surname, or a clipped form, that two names share is no shared
        name: "Gus" never reaches "Gustave Lobig", and "Ursula Löbig"
        never "Gustav Löbig"."""
        found = [
            EntityRecord(id="e5", label="Gus", kind="human"),
            EntityRecord(id="e6", label="Gustav Löbig", kind="human"),
        ]
        extended = [
            RosterEntity(qid="Q124", label="Gustave Lobig", kind="human"),
            RosterEntity(qid="Q77", label="Ursula Löbig", kind="human"),
        ]
        assert block_candidates(found, extended) == {}

    def test_case_and_umlauts_fold(self):
        """A whole name keys folded, and with its umlauts written out
        (ADR-0035 §7): "LÖBIG, Ida" never meets "Ida Löbig" (another
        order), but "IDA LOEBIG" does."""
        ida = [EntityRecord(id="e1", label="IDA LOEBIG", kind="human")]
        graph = [
            RosterEntity(qid="Q41", label="Ida Löbig", kind="human"),
            RosterEntity(qid="Q42", label="LÖBIG, Ida", kind="human"),
        ]
        assert [c.qid for c in block_candidates(ida, graph)["e1"]] == ["Q41"]

    def test_typed_name_statements_key_whole(self):
        """The graph's birth, nickname and official name statements key
        as whole names; a married name, a family name alone, never does."""
        graph = [
            RosterEntity(
                qid="Q41",
                label="Ida Löbig",
                birth_names=["Ida Sörne"],
                kind="human",
            ),
            RosterEntity(
                qid="Q124",
                label="Gustav Löbig",
                official_names=["Gustave Lobig"],
                kind="human",
            ),
            RosterEntity(
                qid="Q43",
                label="Cäcilie Löbig",
                married_names=["Tarle"],
                kind="human",
            ),
        ]
        found = [
            EntityRecord(id="e1", label="Ida Sörne", kind="human"),
            EntityRecord(id="e2", label="Gustave Lobig", kind="human"),
            EntityRecord(id="e3", label="Tarle", kind="human"),
        ]
        candidates = block_candidates(found, graph)
        assert {
            key: [c.qid for c in found] for key, found in candidates.items()
        } == {
            "e1": ["Q41"],
            "e2": ["Q124"],
        }

    def test_a_record_that_names_no_one_keys_nothing(self):
        """A person whose label is a kinship term (ADR-0035 §2.1) or a
        place whose label is deictic names no one, whatever the graph
        holds under those words."""
        found = [
            EntityRecord(
                id="e1",
                label="Dein Vater",
                kind="human",
                structure={"kind": "human", "status": "kinship"},
            ),
            EntityRecord(
                id="e2",
                label="dort",
                kind="place",
                structure={"kind": "place", "status": "deictic"},
            ),
        ]
        graph = [
            RosterEntity(qid="Q50", label="Dein Vater", kind="human"),
            RosterEntity(qid="Q51", label="dort", kind="place"),
        ]
        assert block_candidates(found, graph) == {}

    def test_a_country_given_by_an_adjective_keys_nothing(self):
        """#573: "an Australian citizen" gives a place labelled with the
        adjective, which is no name of the country (ADR-0035 §2.2)."""
        found = [
            EntityRecord(
                id="e1",
                label="Australian",
                kind="place",
                structure={"kind": "place", "status": "adjective"},
            )
        ]
        graph = [RosterEntity(qid="Q52", label="Australian", kind="place")]
        assert block_candidates(found, graph) == {}
        toponym = found[0].model_copy(
            update={"structure": PlaceStructure(status="toponym")}
        )
        assert [c.qid for c in block_candidates([toponym], graph)["e1"]] == [
            "Q52"
        ]

    def test_a_person_record_adds_its_unsplit_forms_and_nicknames(self):
        found = [
            EntityRecord(
                id="e1",
                label="Sebastian Löbig",
                kind="human",
                structure={
                    "kind": "human",
                    "given": [{"written": "Sebastian"}],
                    "surnames": [{"written": "Löbig"}],
                    "nicknames": ["Wastl"],
                    "unsplit": ["Löbig Sebastian"],
                },
            )
        ]
        graph = [
            RosterEntity(qid="Q60", label="Wastl", kind="human"),
            RosterEntity(qid="Q61", label="Löbig Sebastian", kind="human"),
            # a given name alone is no whole name of his
            RosterEntity(qid="Q62", label="Sebastian", kind="human"),
        ]
        assert [c.qid for c in block_candidates(found, graph)["e1"]] == [
            "Q60",
            "Q61",
        ]

    def test_a_given_name_s_standard_form_keys_with_the_surname(self):
        """The postcard's "Gustl", read as Gustav, keys "Gustav Löbig"
        with his surname, and meets the graph's Gustav."""
        gustl = EntityRecord(
            id="e1",
            label="Gustl",
            kind="human",
            structure={
                "kind": "human",
                "given": [{"written": "Gustl", "standard": "Gustav"}],
                "surnames": [{"written": "Löbig"}],
            },
        )
        graph = [
            RosterEntity(qid="Q31", label="Gustav Löbig", kind="human"),
            RosterEntity(qid="Q32", label="Gustl Löbig", kind="human"),
            RosterEntity(qid="Q33", label="Gustav", kind="human"),
        ]
        assert [c.qid for c in block_candidates([gustl], graph)["e1"]] == [
            "Q31",
            "Q32",
        ]

    def test_each_typed_surname_keys_with_the_given_name(self):
        """ "Agnes Löbig geb. Nertler" keys "Agnes Löbig" and "Agnes
        Nertler" from her record, whatever marker her label holds."""
        agnes = EntityRecord(
            id="e1",
            label="Agnes Löbig geb. Nertler",
            kind="human",
            structure={
                "kind": "human",
                "given": [{"written": "Agnes"}],
                "surnames": [
                    {"written": "Löbig"},
                    {"written": "Nertler", "type": "birth"},
                ],
            },
        )
        graph = [
            RosterEntity(qid="Q40", label="Agnes Nertler", kind="human"),
            RosterEntity(
                qid="Q41",
                label="Agnes Löbig",
                birth_names=["Agnes Nertler"],
                kind="human",
            ),
            RosterEntity(qid="Q42", label="Löbig Agnes", kind="human"),
        ]
        assert [c.qid for c in block_candidates([agnes], graph)["e1"]] == [
            "Q40",
            "Q41",
        ]

    def test_nicknames_and_patronymics_compose(self):
        found = [
            EntityRecord(
                id="e1",
                label="Лёбиг Рудольф Феликсович",
                kind="human",
                structure={
                    "kind": "human",
                    "given": [{"written": "Рудольф"}],
                    "surnames": [{"written": "Лёбиг"}],
                    "patronymics": ["Феликсович"],
                    "nicknames": ["Rudi"],
                },
            )
        ]
        graph = [
            RosterEntity(qid="Q50", label="Рудольф Феликсович", kind="human"),
            RosterEntity(qid="Q51", label="Rudi Лёбиг", kind="human"),
            RosterEntity(qid="Q52", label="Рудольф Лёбиг", kind="human"),
        ]
        assert [c.qid for c in block_candidates(found, graph)["e1"]] == [
            "Q50",
            "Q51",
            "Q52",
        ]

    @pytest.mark.parametrize("misread", ["LÖBIG", "Lobig", "Loebig"])
    def test_a_given_name_that_folds_like_the_surname_composes_nothing(
        self, misread
    ):
        """A register's "Löbig" read as a given name beside the surname
        Löbig makes no "Löbig Löbig" (the guard of #361)."""
        from yizkr.pipeline.reconcile import entity_name_keys

        misread = EntityRecord(
            id="e1",
            label="Ursula",
            kind="human",
            structure={
                "kind": "human",
                "given": [
                    {"written": misread},
                    {"written": "Ursula", "standard": "Ursula"},
                ],
                "surnames": [{"written": "Löbig"}],
            },
        )
        assert entity_name_keys(misread) == {
            "ursula",
            "ursula lobig",
            "ursula loebig",
        }

    def test_a_person_keys_by_its_label_and_its_aliases(self):
        found = [
            EntityRecord(
                id="e1",
                label="Klara Löbig",
                aliases=["Pünktchen"],
                kind="human",
                structure={"kind": "human"},
            )
        ]
        assert [
            c.qid
            for c in block_candidates(
                found,
                [RosterEntity(qid="Q60", label="KLARA LÖBIG", kind="human")],
            )["e1"]
        ] == ["Q60"]
        assert [
            c.qid
            for c in block_candidates(
                found,
                [RosterEntity(qid="Q61", label="Pünktchen", kind="human")],
            )["e1"]
        ] == ["Q61"]

    def test_a_place_keys_by_its_record_not_its_short_form(self):
        """The camp's alias "Klurnau", its name's toponym, keys nothing
        (ADR-0035 §2.2): the camp never meets the town."""
        camp = EntityRecord(
            id="e1",
            label="Lager Klurnau",
            aliases=["Klurnau"],
            kind="place",
            structure={
                "kind": "place",
                "names": [
                    {
                        "written": "Lager Klurnau",
                        "head": "Lager",
                        "toponym": "Klurnau",
                    },
                    {"written": "Klurnau"},
                ],
                "address": {"written": "8 Hawthorn Road"},
            },
        )
        graph = [
            RosterEntity(qid="Q70", label="Klurnau", kind="place"),
            RosterEntity(qid="Q71", label="LAGER KLURNAU", kind="place"),
            RosterEntity(qid="Q72", label="8 hawthorn road", kind="place"),
        ]
        assert [c.qid for c in block_candidates([camp], graph)["e1"]] == [
            "Q71",
            "Q72",
        ]

    def test_a_toponym_written_as_its_name_keys(self):
        """A name whose toponym is the name itself is no short form of
        another: "Flörburg" keys."""
        town = EntityRecord(
            id="e1",
            label="Flörburg, Hesse",
            kind="place",
            structure={
                "kind": "place",
                "names": [{"written": "Flörburg", "toponym": "Flörburg"}],
                "within": ["Hesse"],
            },
        )
        graph = [RosterEntity(qid="Q90", label="Flörburg", kind="place")]
        assert [c.qid for c in block_candidates([town], graph)["e1"]] == [
            "Q90"
        ]

    def test_names_key_in_the_instances_form(self):
        """A name blocks in the form the instance keeps a term in
        (writer.stored_term): a control character inside a name keys as
        the space the instance keeps it as, so "Cäcilie\\x08Löbig" meets
        the graph's "Cäcilie Löbig" (#477). The decomposed rows are
        regression guards: folding already took NFD and NFC alike."""
        from unicodedata import normalize

        found = [
            EntityRecord(
                id="e1", label=normalize("NFD", "Flörburg"), kind="place"
            ),
            EntityRecord(
                id="e2", label=normalize("NFD", "Ida Sörne"), kind="human"
            ),
            EntityRecord(id="e3", label="Gustav Löbig", kind="human"),
            EntityRecord(id="e4", label="Cäcilie\x08Löbig", kind="human"),
        ]
        graph = [
            RosterEntity(qid="Q90", label="Flörburg", kind="place"),
            RosterEntity(
                qid="Q41",
                label="Ida Löbig",
                birth_names=["Ida Sörne"],
                kind="human",
            ),
            RosterEntity(
                qid="Q124",
                label="Gustave Lobig",
                aliases=[normalize("NFD", "Gustav Löbig")],
                kind="human",
            ),
            RosterEntity(qid="Q43", label="Cäcilie Löbig", kind="human"),
        ]
        candidates = block_candidates(found, graph)
        assert {
            key: [c.qid for c in found] for key, found in candidates.items()
        } == {"e1": ["Q90"], "e2": ["Q41"], "e3": ["Q124"], "e4": ["Q43"]}

    def test_records_key_in_the_instances_form(self):
        """A person's record nickname, the whole form its typed parts
        make and a place's record name each key in the stored form: a
        control character for a space meets the graph's name written
        with one (#477)."""
        found = [
            EntityRecord(
                id="e1",
                label="Gustl",
                kind="human",
                structure={
                    "kind": "human",
                    "nicknames": ["Gustl\x08Löbig"],
                },
            ),
            EntityRecord(
                id="e2",
                label="Gustav",
                kind="human",
                structure={
                    "kind": "human",
                    "given": [{"written": "Gustav\x08"}],
                    "surnames": [{"written": "Löbig"}],
                },
            ),
            EntityRecord(
                id="e3",
                label="Mockburg",
                kind="place",
                structure={
                    "kind": "place",
                    "names": [{"written": "Kreis\x08Mockburg"}],
                },
            ),
        ]
        graph = [
            RosterEntity(qid="Q170", label="Gustl Löbig", kind="human"),
            RosterEntity(qid="Q124", label="Gustav Löbig", kind="human"),
            RosterEntity(qid="Q95", label="Kreis Mockburg", kind="place"),
        ]
        candidates = block_candidates(found, graph)
        assert {
            key: [c.qid for c in found] for key, found in candidates.items()
        } == {"e1": ["Q170"], "e2": ["Q124"], "e3": ["Q95"]}

    def test_graph_pairs_key_in_the_instances_form(self):
        """Two items pair on a name the one gives with a control
        character for a space; the decomposed birth name is a regression
        guard."""
        from unicodedata import normalize

        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(qid="Q41", label="Ida Sörne", kind="human"),
            RosterEntity(
                qid="Q42",
                label="Ida Löbig",
                birth_names=[normalize("NFD", "Ida Sörne")],
                kind="human",
            ),
            RosterEntity(qid="Q43", label="Cäcilie Löbig", kind="human"),
            RosterEntity(
                qid="Q44",
                label="Cäcilie Tarle",
                official_names=["Cäcilie\x08Löbig"],
                kind="human",
            ),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, pair_roster, batch_pairs=1)
        assert len(llm.prompts) == 2
        assert "Q41" in llm.prompts[0] and "Q42" in llm.prompts[0]
        assert "Q43" in llm.prompts[1] and "Q44" in llm.prompts[1]

    def test_graph_pairs_need_one_kind(self):
        """A vessel and a place that share a name never pair."""
        from yizkr.pipeline.reconcile import propose_graph_pairs

        unlike = [
            RosterEntity(qid="Q80", label="RMS Flund", kind="vessel"),
            RosterEntity(qid="Q81", label="RMS Flund", kind="place"),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        assert propose_graph_pairs(llm, unlike) == []
        assert llm.prompts == []

    def test_a_place_record_without_names_keys_by_its_label(self):
        town = EntityRecord(
            id="e1",
            label="Flörburg",
            kind="place",
            structure={"kind": "place", "within": ["Hesse"]},
        )
        graph = [
            RosterEntity(qid="Q90", label="Flörburg", kind="place"),
            RosterEntity(qid="Q91", label="Hesse", kind="place"),
        ]
        assert [c.qid for c in block_candidates([town], graph)["e1"]] == [
            "Q90"
        ]

    def test_graph_pairs_block_on_a_shared_whole_name(self):
        """Same rule for roster x roster pairs: two items pair on a shared
        name form, here an alias against a nickname statement, and never
        on a clipped form."""
        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(
                qid="Q170", label="Gustl", aliases=["Gus"], kind="human"
            ),
            RosterEntity(
                qid="Q124",
                label="Gustave Lobig",
                nicknames=["Gus"],
                kind="human",
            ),
            RosterEntity(qid="Q125", label="Gustav", kind="human"),
        ]
        response = "\n".join(
            [
                json.dumps(
                    {
                        "entity_id": "Q170",
                        "qid": "Q124",
                        "verdict": "uncertain",
                        "confidence": 0.5,
                        "evidence": "one nickname",
                    }
                ),
                json.dumps({"done": True, "proposals_emitted": 1}),
            ]
        )
        llm = ScriptedLlm(response)
        proposals = propose_graph_pairs(llm, pair_roster)
        assert [(p.entity_id, p.qid) for p in proposals] == [("Q170", "Q124")]
        assert len(llm.prompts) == 1
        assert "Gustave Lobig" in llm.prompts[0]
        assert "Q125" not in llm.prompts[0]


class TestAdjudication:
    def response(self, extra_lines=()):
        lines = [
            json.dumps(
                {
                    "entity_id": "e1",
                    "qid": "Q40",
                    "verdict": "same",
                    "confidence": 0.95,
                    "evidence": "same spouse Sebastian Löbig; married name",
                }
            ),
            json.dumps(
                {
                    "entity_id": "e1",
                    "qid": "Q77",
                    "verdict": "distinct",
                    "confidence": 0.9,
                    "evidence": "no relational overlap",
                }
            ),
            *extra_lines,
        ]
        lines.append(json.dumps({"done": True}))
        return "\n".join(lines)

    def test_valid_proposals_survive(self):
        llm = ScriptedLlm(self.response())
        proposals = adjudicate(
            llm, entities(), block_candidates(entities(), roster()), batch()
        )
        assert [(p.entity_id, p.qid, p.verdict) for p in proposals] == [
            ("e1", "Q40", "same"),
            ("e1", "Q77", "distinct"),
        ]
        # the prompt carries relational facts from the batch (e1's summary)
        # and from the roster (Q40's spouse), each matched by text that only
        # its own side renders
        prompt = llm.prompts[0]
        assert "married Sebastian Löbig in 1905" in prompt
        assert "spouse of: Sebastian Löbig" in prompt

    def test_rulings_on_unblocked_pairs_are_dropped(self):
        invented = json.dumps(
            {
                "entity_id": "e1",
                "qid": "Q99",
                "verdict": "same",
                "confidence": 1,
            }
        )
        llm = ScriptedLlm(self.response(extra_lines=[invented]))
        proposals = adjudicate(
            llm, entities(), block_candidates(entities(), roster()), batch()
        )
        assert all(p.qid != "Q99" for p in proposals)

    def test_missing_sentinel_raises(self):
        llm = ScriptedLlm(
            json.dumps({"entity_id": "e1", "qid": "Q40", "verdict": "same"})
        )
        with pytest.raises(ValueError, match="sentinel"):
            adjudicate(
                llm,
                entities(),
                block_candidates(entities(), roster()),
                batch(),
            )

    def test_no_candidates_no_call(self):
        llm = ScriptedLlm("must not be called")
        assert adjudicate(llm, entities(), {}, batch()) == []
        assert llm.prompts == []

    def test_batched_adjudication_bounds_each_call(self):
        """The response size is bounded like chunk output (ADR-0006, one
        level up): pairs split across calls, each with its own sentinel,
        and each prompt carries only its batch's entities."""

        class QueueLlm(ScriptedLlm):
            def __init__(self, responses):
                self.responses = list(responses)
                self.prompts = []

            def complete(self, prompt, image_paths=()):
                self.prompts.append(prompt)
                return self.responses.pop(0)

        first = "\n".join(
            [
                json.dumps(
                    {"entity_id": "e1", "qid": "Q40", "verdict": "same"}
                ),
                json.dumps(
                    {"entity_id": "e1", "qid": "Q77", "verdict": "distinct"}
                ),
                json.dumps({"done": True, "proposals_emitted": 2}),
            ]
        )
        second = "\n".join(
            [
                json.dumps(
                    {"entity_id": "e2", "qid": "Q90", "verdict": "same"}
                ),
                json.dumps({"done": True, "proposals_emitted": 1}),
            ]
        )
        llm = QueueLlm([first, second])
        proposals = adjudicate(
            llm,
            entities(),
            block_candidates(entities(), roster()),
            batch(),
            batch_pairs=2,  # e1 has 2 candidates, e2 has 1 -> two calls
        )
        assert len(llm.prompts) == 2
        assert "Flörburg" not in llm.prompts[0]  # e2 not in batch 1
        assert "Hildegard" not in llm.prompts[1]  # e1 not in batch 2
        assert [(p.entity_id, p.qid) for p in proposals] == [
            ("e1", "Q40"),
            ("e1", "Q77"),
            ("e2", "Q90"),
        ]


class Stopping(ScriptedLlm):
    """A scripted client whose engine reports why its model stopped
    (`last_report`, as the engines' clients do)."""

    def __init__(self, response: str, stop_reason: str) -> None:
        super().__init__(response)
        self.stop_reason = stop_reason

    def last_report(self) -> dict:
        return {"stop_reason": self.stop_reason}


# two roster items whose names block them into a pair for adjudication
NAMESAKES = [
    RosterEntity(qid="Q170", label="Gus", kind="human"),
    RosterEntity(
        qid="Q124", label="Gustave Lobig", nicknames=["Gus"], kind="human"
    ),
]


class TestTheSentinel:
    """An adjudication answer ends with `{"done": true}`, whose presence
    is the gate, and is cut off when its engine stopped it at max_tokens,
    sentinel or no (#7)."""

    def adjudicate(self, llm):
        return adjudicate(
            llm, entities(), block_candidates(entities(), roster()), batch()
        )

    def test_a_done_line_alone_is_the_sentinel(self):
        """No count of the proposals comes before it: the prompts ask for
        none."""
        llm = ScriptedLlm(TestAdjudication().response())
        assert len(self.adjudicate(llm)) == 2
        assert 'final line:\n{"done": true}\n' in llm.prompts[0]
        assert "emitted" not in llm.prompts[0]

    def test_a_count_on_the_done_line_is_not_read(self):
        answer = (
            TestAdjudication()
            .response()
            .replace(
                '{"done": true}', '{"done": true, "proposals_emitted": 9}'
            )
        )
        assert len(self.adjudicate(ScriptedLlm(answer))) == 2

    def test_an_answer_stopped_at_the_output_limit_raises(self):
        llm = Stopping(TestAdjudication().response(), "max_tokens")
        with pytest.raises(ValueError, match="cut off at max_tokens"):
            self.adjudicate(llm)
        llm = Stopping(TestAdjudication().response(), "end_turn")
        assert len(self.adjudicate(llm)) == 2

    def test_a_graph_answer_stopped_at_the_output_limit_raises(self):
        from yizkr.pipeline.reconcile import propose_graph_pairs

        llm = Stopping(json.dumps({"done": True}), "max_tokens")
        with pytest.raises(ValueError, match="cut off at max_tokens"):
            propose_graph_pairs(llm, NAMESAKES)
        llm = ScriptedLlm(json.dumps({"done": True}))
        assert propose_graph_pairs(llm, NAMESAKES) == []
        assert 'final line:\n{"done": true}\n' in llm.prompts[0]
        assert "emitted" not in llm.prompts[0]


class TestEquivalencesCsv:
    def proposals(self):
        return [
            Proposal(
                entity_id="e1",
                qid="Q40",
                verdict="same",
                confidence=0.95,
                evidence="same spouse",
            ),
            Proposal(
                entity_id="e1", qid="Q77", verdict="distinct", confidence=0.9
            ),
        ]

    def test_round_trip_only_approved_rows_take_effect(self, tmp_path):
        path = tmp_path / "equivalences.csv"
        export_equivalences_csv(self.proposals(), entities(), roster(), path)
        content = path.read_text()
        assert "Hildegard Laudler" in content and "same spouse" in content
        # machine verdict alone grants nothing
        assert read_approved_equivalences(path) == {}
        text = content.replace("same spouse,", "same spouse,approve")
        path.write_text(text)
        assert read_approved_equivalences(path) == {"e1": "Q40"}

    def test_aliases_visible_and_prunable_at_review(self, tmp_path):
        """An approval is also an alias contribution (the writer appends
        the document's aliases to the matched item), so the CSV must show
        them and honor the reviewer's pruning — a form's MOTHER label
        must be deletable before it pollutes the graph."""
        from yizkr.pipeline.reconcile import read_reviewed_aliases

        path = tmp_path / "equivalences.csv"
        # a multi-word alias, which review must keep whole, and a role label
        as_written = "Hildegard Löbig geborene Laudler"
        noisy = [
            EntityRecord(
                id="e1",
                label="Hildegard Löbig",
                aliases=[as_written, "MOTHER"],
                kind="human",
            )
        ]
        export_equivalences_csv(self.proposals(), noisy, roster(), path)
        content = path.read_text()
        assert f"{as_written}|MOTHER" in content  # visible to reviewer
        # the reviewer prunes the role label and approves
        content = content.replace(f"{as_written}|MOTHER", as_written)
        content = content.replace("same spouse,", "same spouse,approve")
        path.write_text(content)
        approved = read_approved_equivalences(path)
        reviewed = read_reviewed_aliases(path)
        assert reviewed == {"e1": [as_written]}
        applied = apply_equivalences(
            batch(entity_list=noisy), approved, reviewed
        )
        (entity,) = applied.entities
        assert entity.qid == "Q40"
        assert entity.aliases == [as_written]  # MOTHER pruned

    def test_old_csvs_without_aliases_column_still_apply(self, tmp_path):
        from yizkr.pipeline.reconcile import read_reviewed_aliases

        path = tmp_path / "old.csv"
        path.write_text(
            "entity_id,qid,verdict,confidence,evidence,decision\n"
            "e1,Q40,same,0.9,same spouse,approve\n"
        )
        assert read_approved_equivalences(path) == {"e1": "Q40"}
        assert read_reviewed_aliases(path) == {}

    def test_conflicting_approvals_raise(self, tmp_path):
        path = tmp_path / "equivalences.csv"
        export_equivalences_csv(self.proposals(), entities(), roster(), path)
        lines = path.read_text().splitlines()
        rows = [line + "approve" for line in lines[1:]]
        path.write_text("\n".join([lines[0], *rows]))
        with pytest.raises(ValueError, match="both"):
            read_approved_equivalences(path)


class TestApplyEquivalences:
    def test_refs_rewritten_and_qid_recorded(self):
        original = batch(claims=[spouse_claim()])
        applied = apply_equivalences(original, {"e1": "Q40"})
        claim = applied.claims[0]
        assert claim.subject == QidRef(qid="Q40")
        # e2 unapproved: stays local
        assert isinstance(claim.value.value, LocalRef)
        hildegard = next(e for e in applied.entities if e.id == "e1")
        assert hildegard.qid == "Q40"
        # original untouched
        assert isinstance(original.claims[0].subject, LocalRef)
        assert original.entities[0].qid is None

    def test_identical_claims_collapse_after_equivalence(self):
        # two document entities (duplicated records in the source) both
        # approved to one item leave byte-identical claims: collapse
        twin = dict(
            spouse_claim(), subject={"kind": "local", "local_id": "e3"}
        )
        entity_list = [
            *entities(),
            EntityRecord(id="e3", label="Hildegard Löbig", kind="human"),
        ]
        original = batch(
            claims=[spouse_claim(), twin], entity_list=entity_list
        )
        applied = apply_equivalences(original, {"e1": "Q40", "e3": "Q40"})
        assert len(applied.claims) == 1
        assert applied.claims[0].subject == QidRef(qid="Q40")

    def test_distinct_claims_survive_equivalence(self):
        # same subject item but different values must both remain
        other = dict(spouse_claim(), value={"type": "string", "value": "x"})
        applied = apply_equivalences(
            batch(claims=[spouse_claim(), other]), {"e1": "Q40"}
        )
        assert len(applied.claims) == 2


# -- impossibility filter (ADR-0017) -----------------------------------------


def rf(**kw) -> RosterEntity:
    """A roster entity with defaults; kw overrides."""
    kw.setdefault("kind", "human")
    return RosterEntity(
        qid=kw.pop("qid", "Q1"), label=kw.pop("label", "X"), **kw
    )


def _facts(entity: RosterEntity):
    return _roster_pair_facts(entity)


class TestImpossiblePair:
    def test_kinship_edge_rules_out_a_same_name_pair(self):
        # a "Jr." case: father and son share a name but a father edge on the
        # son (naming the father's QID) proves they are two people.
        son = rf(qid="Q1", label="A B", father_qids=["Q2"])
        father = rf(qid="Q2", label="A B")
        assert impossible_pair(_facts(son), _facts(father)) == "kinship-edge"
        # symmetric — direction of the edge does not matter
        assert impossible_pair(_facts(father), _facts(son)) == "kinship-edge"

    def test_spouse_edge_rules_out(self):
        # the universe's Sebastian and Hildegard, married in 1905: their
        # shared surname blocks them as candidates, but the spouse edge
        # proves they are two people
        a = rf(qid="Q1", label="Sebastian Löbig", spouse_qids=["Q2"])
        b = rf(qid="Q2", label="Hildegard Löbig")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"

    @pytest.mark.parametrize(
        "field_name",
        [
            "father_qids",
            "mother_qids",
            "spouse_qids",
            "child_qids",
            "sibling_qids",
        ],
    )
    def test_every_kinship_relation_feeds_the_filter(self, field_name):
        a = rf(qid="Q1", label="A B", **{field_name: ["Q2"]})
        b = rf(qid="Q2", label="A B")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"

    def test_unrelated_same_name_is_not_impossible(self):
        a = rf(qid="Q1", label="Ursula Löbig")
        b = rf(qid="Q2", label="Ursula Löbig")
        assert impossible_pair(_facts(a), _facts(b)) is None

    def test_place_containment_by_qid(self):
        # Q66 (a prison) is transitively inside Q10 (Germany)
        containment = PlaceContainment({"Q66": {"Q10"}})
        prison = rf(qid="Q66", label="Peiltal court prison", kind="place")
        germany = rf(qid="Q10", label="Germany", kind="place")
        assert (
            impossible_pair(
                _facts(prison), _facts(germany), containment=containment
            )
            == "place-containment"
        )
        # a place NOT in the closure with the other is a live candidate
        other = rf(qid="Q11", label="Germany (other)", kind="place")
        assert (
            impossible_pair(
                _facts(prison), _facts(other), containment=containment
            )
            is None
        )

    def test_a_place_without_a_qid_is_never_pruned_by_its_name(self):
        # a new "Germany" (no QID) vs the graph's Peiltal court prison,
        # which the graph places inside a "Germany": places share names,
        # so the name never stands in for the container (ADR-0017)
        containment = PlaceContainment({"Q66": {"Q10"}})
        batch_germany = _batch_pair_facts(
            EntityRecord(id="e1", label="Germany", kind="place"), None
        )
        prison = _facts(
            rf(qid="Q66", label="Peiltal court prison", kind="place")
        )
        assert (
            impossible_pair(batch_germany, prison, containment=containment)
            is None
        )
        assert containment.containers("Q66") == {"Q10"}
        assert containment.containers("Q10") == set()

    def test_containment_only_applies_between_places(self):
        containment = PlaceContainment({"Q66": {"Q10"}})
        a = rf(qid="Q66", label="X", kind="human")
        b = rf(qid="Q10", label="X", kind="human")
        # same closure, but humans — never a containment prune
        assert (
            impossible_pair(_facts(a), _facts(b), containment=containment)
            is None
        )

    def test_eponymous_nesting_prunes_by_qid(self):
        # the town Rekau sits inside the urban district Rekau: a batch
        # entity already resolved to the district is never the town
        containment = PlaceContainment({"Q70": {"Q71"}})
        district = _batch_pair_facts(
            EntityRecord(id="e1", label="Rekau", kind="place", qid="Q71"),
            None,
        )
        town = _facts(rf(qid="Q70", label="Rekau", kind="place"))
        assert (
            impossible_pair(district, town, containment=containment)
            == "place-containment"
        )
        assert (
            impossible_pair(town, district, containment=containment)
            == "place-containment"
        )

    def test_date_disagreement_is_never_impossible(self):
        # contradictory dates coexist (ADR-0002); they are surfaced to the
        # human, never pruned — the filter has no temporal rule at all.
        a = rf(qid="Q1", label="A B", birth="1900", death="1910")
        b = rf(qid="Q2", label="A B", birth="1936", death="1990")
        assert impossible_pair(_facts(a), _facts(b)) is None

    def test_symmetry_of_truthiness(self):
        son = rf(qid="Q1", label="Gustav Löbig", father_qids=["Q2"])
        father = rf(qid="Q2", label="Sebastian Löbig")
        assert bool(impossible_pair(_facts(son), _facts(father))) == bool(
            impossible_pair(_facts(father), _facts(son))
        )

    def test_predicate_does_no_io(self):
        # pure function — no client, no network; a plain call returns
        a = rf(qid="Q1", label="Gustav Löbig", father_qids=["Q2"])
        b = rf(qid="Q2", label="Sebastian Löbig")
        assert impossible_pair(_facts(a), _facts(b)) == "kinship-edge"


# A son and his father whose names block each other: one name for both (a
# "Jr." case), written alike or in capitals.
FATHER_AND_SON = pytest.mark.parametrize(
    ("son", "father"),
    [("Xxx Yyy", "Xxx Yyy"), ("Sebastian Löbig", "SEBASTIAN LÖBIG")],
    ids=["same-name", "case-folded"],
)


class TestBlockingWithImpossibility:
    def test_containment_candidate_is_pruned_from_blocking(self):
        # a batch place resolved to the urban district Rekau shares its
        # name with the town inside it
        places = [
            EntityRecord(id="e1", label="Rekau", kind="place", qid="Q71")
        ]
        roster_places = [RosterEntity(qid="Q70", label="Rekau", kind="place")]
        containment = PlaceContainment({"Q70": {"Q71"}})
        # without the closure the shared name blocks it
        assert block_candidates(places, roster_places)["e1"][0].qid == "Q70"
        # with it, the impossible pair is pruned -> e1 has no candidates
        assert (
            block_candidates(places, roster_places, containment=containment)
            == {}
        )

    @FATHER_AND_SON
    def test_batch_kinship_qidref_prunes_candidate(self, son, father):
        # a batch entity whose father is ALREADY the graph candidate (a
        # QidRef) cannot be that same candidate — pruned before adjudication.
        entity = EntityRecord(id="e1", label=son, kind="human")
        b = batch(
            claims=[
                {
                    "subject": {"kind": "local", "local_id": "e1"},
                    "property": "father",
                    "value": {
                        "type": "item",
                        "value": {"kind": "qid", "qid": "Q84"},
                    },
                    "source": {"artifact": SCAN.model_dump()},
                }
            ],
            entity_list=[entity],
        )
        roster_people = [RosterEntity(qid="Q84", label=father, kind="human")]
        assert block_candidates([entity], roster_people, batch=b) == {}
        # without the batch (no resolved father) the pair survives to review
        assert block_candidates([entity], roster_people)["e1"][0].qid == "Q84"

    @FATHER_AND_SON
    def test_graph_pair_pruning_drops_impossible_pairs(self, son, father):
        # roster x roster: a father edge between two same-name items means
        # the graph-dedup pass never even
        # asks the model about them.
        from yizkr.pipeline.reconcile import propose_graph_pairs

        pair_roster = [
            RosterEntity(
                qid="Q1", label=son, kind="human", father_qids=["Q2"]
            ),
            RosterEntity(qid="Q2", label=father, kind="human"),
        ]

        class _Boom:
            model_identifier = "fake"

            def complete(self, *a, **k):  # must never be called
                raise AssertionError(
                    "adjudicator called on an impossible pair"
                )

        assert propose_graph_pairs(_Boom(), pair_roster) == []
        # without the father edge the names still block: the pair reaches
        # the adjudicator
        unlinked = [
            RosterEntity(qid="Q1", label=son, kind="human"),
            RosterEntity(qid="Q2", label=father, kind="human"),
        ]
        llm = ScriptedLlm(json.dumps({"done": True, "proposals_emitted": 0}))
        propose_graph_pairs(llm, unlinked)
        assert len(llm.prompts) == 1


class TestGraphDedupAssertsNoGeography:
    """ADR-0038 §3 and §8.2 (#515): graph dedup's adjudicator answers
    identity only. On a `distinct` verdict it names no container, so no
    containment proposal comes from it; the verdict itself stands."""

    def test_the_prompt_asks_for_no_container(self):
        prompt = GRAPH_ADJUDICATION_PROMPT.lower()
        assert "contained_in" not in prompt
        assert "containment" not in prompt
        assert "inside" not in prompt

    def test_a_proposal_carries_no_container(self):
        assert set(Proposal.model_fields) == {
            "entity_id",
            "qid",
            "verdict",
            "confidence",
            "evidence",
        }

    def test_a_distinct_verdict_on_two_places_stands(self):
        # the town Rekau and the urban district Rekau share a name
        roster = [
            RosterEntity(qid="Q66", label="Rekau", kind="place"),
            RosterEntity(qid="Q10", label="Rekau", kind="place"),
        ]
        line = {
            "entity_id": "Q66",
            "qid": "Q10",
            "verdict": "distinct",
            "evidence": "a town and its urban district",
        }
        llm = ScriptedLlm(json.dumps(line) + "\n" + json.dumps({"done": True}))
        (proposal,) = propose_graph_pairs(llm, roster)
        assert proposal.verdict == "distinct"
        assert proposal.model_dump().keys() == set(Proposal.model_fields)


def _edge(item, target, *, rank="NormalRank", stated=True, link=None):
    concept = "http://wikibase.example"
    return {
        "item": {"type": "uri", "value": f"{concept}/entity/{item}"},
        "link": {"type": "uri", "value": link or f"{concept}/entity/P2"},
        "target": {"type": "uri", "value": f"{concept}/entity/{target}"},
        "rank": {"type": "uri", "value": f"http://wikiba.se/ontology#{rank}"},
        "stated": {
            "type": "literal",
            "datatype": "http://www.w3.org/2001/XMLSchema#boolean",
            "value": "true" if stated else "false",
        },
    }


class TestRuleTwoReadsTheReportsEdges:
    """ADR-0017 rule 2, as ADR-0038 §8.2 keeps it: the closure follows the
    edges the inconsistency report follows (§5), a part-of or location a
    source states, not deprecated, at any time it carries."""

    # Brunnenring 35 in Brunnenring, in Flörburg, in Kreis Peiltal
    CHAIN = [("Q35", "Q30"), ("Q30", "Q20"), ("Q20", "Q12")]

    def test_the_closure_is_transitive_over_stated_claims(self):
        containment = read_place_containment(
            [_edge(*edge) for edge in self.CHAIN]
        )
        assert containment.containers("Q35") == {"Q30", "Q20", "Q12"}
        assert containment.contains("Q12", "Q35")
        assert not containment.contains("Q35", "Q12")

    def test_a_cycle_ends(self):
        # two sources nest two places each in the other: the closure still
        # ends, each place among the other's containers
        containment = read_place_containment(
            [_edge("Q211", "Q212"), _edge("Q212", "Q211")]
        )
        assert containment.containers("Q211") == {"Q211", "Q212"}
        assert containment.contains("Q211", "Q212")

    def test_a_preferred_claim_is_followed(self):
        containment = read_place_containment(
            [_edge("Q35", "Q30", rank="PreferredRank")]
        )
        assert containment.contains("Q30", "Q35")

    def test_a_deprecated_claim_is_not_followed(self):
        rows = [
            _edge(*self.CHAIN[0]),
            _edge(*self.CHAIN[1], rank="DeprecatedRank"),
        ]
        containment = read_place_containment(rows)
        assert containment.containers("Q35") == {"Q30"}

    def test_a_claim_no_source_states_is_not_followed(self):
        rows = [_edge(*self.CHAIN[0]), _edge(*self.CHAIN[1], stated=False)]
        containment = read_place_containment(rows)
        assert containment.containers("Q35") == {"Q30"}
        # so a pair the unstated edge alone would nest stays a candidate
        town = PairFacts(kind="place", qid="Q20")
        street = PairFacts(kind="place", qid="Q30")
        assert impossible_pair(town, street, containment=containment) is None

    def test_a_merge_redirect_is_no_containment(self):
        same_as = "http://www.w3.org/2002/07/owl#sameAs"
        row = _edge("Q9001", "Q20", link=same_as)
        del row["rank"], row["stated"]
        assert read_place_containment([row]).containers("Q9001") == set()

    def test_it_runs_the_reports_containment_query(self, monkeypatch):
        from yizkr.core import sparql
        from yizkr.queries.publish import load_templates

        sent = []

        def runner(config):
            def run(query):
                sent.append(query)
                return [_edge(*edge) for edge in self.CHAIN]

            return run

        class Client:
            def vocabulary(self):
                return {
                    "location": "P1",
                    "part_of": "P2",
                    "stated_in": "P3",
                }, {}

        class Config:
            concept_uri = "http://wikibase.example"

        monkeypatch.setattr(sparql, "runner", runner)
        containment = fetch_place_containment(Client(), Config())
        assert containment.contains("Q12", "Q35")
        (query,) = sent
        template = load_templates()["place-containment"]
        body = template.split("SELECT", 1)[1].split("WHERE", 1)[0]
        assert body in query
        assert "wikibase.example/prop/reference/>" in query


class TestRatifyEquivalenceVerdicts:
    """`accept-all` on an equivalences CSV ratifies the machine per row
    (same->approve, distinct->reject, uncertain->blank) — never a blanket
    merge — and leaves a self-contradicting entity blank."""

    def test_maps_each_verdict_to_its_decision(self):
        rows = [
            {
                "entity_id": "e1",
                "qid": "Q10",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e2",
                "qid": "Q20",
                "verdict": "distinct",
                "decision": "",
            },
            {
                "entity_id": "e3",
                "qid": "Q30",
                "verdict": "uncertain",
                "decision": "",
            },
        ]
        counts = ratify_equivalence_verdicts(rows)
        assert [r["decision"] for r in rows] == ["approve", "reject", ""]
        assert counts == {
            "approved": 1,
            "rejected": 1,
            "uncertain": 1,
            "conflicted": 0,
        }

    def test_same_against_two_items_is_left_blank(self):
        # a contradiction read_approved_equivalences would reject: don't merge
        rows = [
            {
                "entity_id": "e4",
                "qid": "Q40",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e4",
                "qid": "Q41",
                "verdict": "same",
                "decision": "",
            },
            {
                "entity_id": "e5",
                "qid": "Q50",
                "verdict": "same",
                "decision": "",
            },
        ]
        counts = ratify_equivalence_verdicts(rows)
        assert [r["decision"] for r in rows] == ["", "", "approve"]
        assert counts["conflicted"] == 2
        assert counts["approved"] == 1


class TestRosterKinds:
    """fetch_roster maps each item's kind back to its manifest key by the
    kind item's QID, never by its label (#388)."""

    class Vocabulary:
        def vocabulary(self):
            properties = {
                key: f"P{n}"
                for n, key in enumerate(
                    (
                        "instance_of",
                        "date_of_birth",
                        "date_of_death",
                        "spouse",
                        "father",
                        "mother",
                        "child",
                        "sibling",
                        "participant",
                        "point_in_time",
                        "birth_name",
                        "married_name",
                        "nickname",
                        "official_name",
                        "referred_to_as",
                    ),
                    1,
                )
            }
            items = {
                "human": "Q5",
                "birth": "Q20",
                "death": "Q21",
                # the label "occupation" and "time period" never spell
                # these keys
                "occupation_class": "Q30",
                "time_period_class": "Q31",
            }
            return properties, items

    def test_kinds_map_by_qid(self, monkeypatch):
        import io

        from yizkr.core.config import Config
        from yizkr.pipeline import reconcile

        def binding(qid, label, kind, kind_label="x"):
            return {
                "item": {"value": f"http://wikibase.example/entity/{qid}"},
                "itemLabel": {"value": label},
                "kind": {"value": f"http://wikibase.example/entity/{kind}"},
                "kindLabel": {"value": kind_label},
            }

        rows = [
            binding("Q100", "Gustav Löbig", "Q5"),
            binding("Q101", "Hutmachermeister", "Q30"),
            binding("Q102", "Xxx", "Q31"),
            binding("Q103", "Yyy", "Q999", "military unit"),
        ]
        queries = []

        def urlopen(url, timeout):
            queries.append(urllib.parse.unquote_plus(url))
            body = json.dumps({"results": {"bindings": rows}})
            return io.BytesIO(body.encode())

        monkeypatch.setattr(reconcile.urllib.request, "urlopen", urlopen)
        # a graph check run before the roster where one is, read here as
        # finding nothing
        monkeypatch.setattr(
            reconcile,
            "unsupported_in_graph",
            lambda *args, **kwargs: None,
            raising=False,
        )
        config = Config(
            wikibase_url="http://wikibase.example",
            concept_uri="http://wikibase.example",
            wdqs_url="http://query.example",
            sparql_url="http://query.example/sparql",
            admin_name="x",
            admin_pass="x",
            env_file=None,
        )
        roster = reconcile.fetch_roster(self.Vocabulary(), config)
        assert [(r.qid, r.kind) for r in roster] == [
            ("Q100", "human"),
            ("Q101", "occupation_class"),
            ("Q102", "time_period_class"),
            # a kind the manifest lacks keeps its QID
            ("Q103", "Q999"),
        ]
        # whose label is shown, and only its, beside it to the adjudicator
        assert [r.kind_label for r in roster] == ["", "", "", "military unit"]
        assert "kind: Q999 (military unit)" in roster[3].facts()
        assert roster[0].facts().endswith("kind: human")
        (query,) = queries
        # the kind's QID is selected; its label only rides along
        assert "SELECT ?item ?itemLabel ?kind ?kindLabel" in query
        assert "GROUP BY ?item ?itemLabel ?kind ?kindLabel\n" in query

    def test_an_occupation_blocks_against_an_occupation(self):
        found = [
            EntityRecord(
                id="e1", label="Hutmachermeister", kind="occupation_class"
            )
        ]
        graph = [
            RosterEntity(
                qid="Q101", label="Hutmachermeister", kind="occupation_class"
            ),
            RosterEntity(qid="Q102", label="Hutmachermeister", kind="human"),
        ]
        assert [c.qid for c in block_candidates(found, graph)["e1"]] == [
            "Q101"
        ]


def date_claim(prop: str, time: dict, subject: str = "e1") -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": subject,
            "label": "Hildegard Löbig",
        },
        "property": prop,
        "value": {"type": "time", **time},
        "source": {"artifact": SCAN.model_dump()},
    }


class TestDatesAtTheirPrecision:
    """The adjudication prompts show a century or a decade as such, as the
    site does, never as its first year (rule 5; #337): a batch's dates
    (`entity_facts`) and the roster's (`fetch_roster`, which reads each
    date's precision from its statement's value node)."""

    @pytest.mark.parametrize(
        "time, shown",
        [
            ({"year": 1801, "precision": "century"}, "the 19th century"),
            ({"year": 1830, "precision": "decade"}, "the 1830s"),
            ({"year": 1876}, "1876"),
            ({"year": 1876, "month": 3}, "1876-03"),
            ({"year": 1876, "month": 3, "day": 19}, "1876-03-19"),
        ],
        ids=["century", "decade", "year", "month", "day"],
    )
    def test_entity_facts_show_a_date_at_its_precision(self, time, shown):
        facts = entity_facts(
            batch(claims=[date_claim("date_of_birth", time)]), "e1"
        )
        assert facts == f"date_of_birth: {shown}"

    @staticmethod
    def fetch(monkeypatch, rows):
        import io

        from yizkr.core.config import Config
        from yizkr.pipeline import reconcile

        queries = []

        def urlopen(url, timeout):
            queries.append(urllib.parse.unquote_plus(url))
            body = json.dumps({"results": {"bindings": rows}})
            return io.BytesIO(body.encode())

        monkeypatch.setattr(reconcile.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(
            reconcile, "unsupported_in_graph", lambda *args, **kwargs: None
        )
        config = Config(
            wikibase_url="http://wikibase.example",
            concept_uri="http://wikibase.example",
            wdqs_url="http://query.example",
            sparql_url="http://query.example/sparql",
            admin_name="x",
            admin_pass="x",
            env_file=None,
        )
        roster = reconcile.fetch_roster(TestRosterKinds.Vocabulary(), config)
        (query,) = queries
        return roster, query

    @staticmethod
    def binding(qid, **dates):
        row = {
            "item": {"value": f"http://wikibase.example/entity/{qid}"},
            "itemLabel": {"value": "Cäcilie Löbig"},
            "kind": {"value": "http://wikibase.example/entity/Q5"},
            "kindLabel": {"value": "human"},
        }
        for name, value in dates.items():
            row[name] = {"value": value}
        return row

    def test_roster_dates_are_shown_at_their_precision(self, monkeypatch):
        rows = [
            self.binding(
                "Q100",
                birth="1801-01-01T00:00:00Z|7",
                death="1830-01-01T00:00:00Z|8",
            ),
            self.binding(
                "Q101",
                birth="1876-03-19T00:00:00Z|11",
                death="1941-03-01T00:00:00Z|10",
            ),
            self.binding("Q102", birth="1833-01-01T00:00:00Z|9"),
            # coarser than a claim states, as a curator may set in the wiki
            self.binding(
                "Q103",
                birth="1001-01-01T00:00:00Z|6",
                death="1900-01-01T00:00:00Z|5",
            ),
        ]
        roster, _ = self.fetch(monkeypatch, rows)
        assert [(r.birth, r.death) for r in roster] == [
            ("the 19th century", "the 1830s"),
            ("1876-03-19", "1941-03"),
            ("1833", ""),
            ("the 2nd millennium", "1900, to within 10,000 years"),
        ]
        assert "born: the 19th century; died: the 1830s" in roster[0].facts()

    def test_event_dates_fill_gaps_at_their_precision(self, monkeypatch):
        rows = [
            # a direct date wins over an event's
            self.binding(
                "Q100",
                birth="1876-03-19T00:00:00Z|11",
                eventBirth="1801-01-01T00:00:00Z|7",
                death="1941-03-01T00:00:00Z|10",
                eventDeath="1890-01-01T00:00:00Z|8",
            ),
            # an event's date stands in where there is no direct one
            self.binding(
                "Q101",
                eventBirth="1801-01-01T00:00:00Z|7",
                eventDeath="1890-01-01T00:00:00Z|8",
            ),
        ]
        roster, _ = self.fetch(monkeypatch, rows)
        assert [(r.birth, r.death) for r in roster] == [
            ("1876-03-19", "1941-03"),
            ("the 19th century", "the 1890s"),
        ]

    def test_roster_query_reads_each_dates_precision(self, monkeypatch):
        _, query = self.fetch(monkeypatch, [])
        flat = re.sub(r"\s+", " ", query)
        # each date's node reached through its own property's statement
        # value link, and sampled with its own precision
        for statement, prop, name in [
            ("birthStatement", "P2", "birth"),
            ("deathStatement", "P3", "death"),
            ("birthEventStatement", "P10", "birthEvent"),
            ("deathEventStatement", "P10", "deathEvent"),
        ]:
            assert f"wd:{prop} wikibase:statementValue ?{name}Link ." in flat
            assert f"?{statement} ?{name}Link ?{name}Node ." in flat
            assert (
                f'CONCAT(STR(?{name}Time), "|", STR(?{name}Precision))' in flat
            )
        # P2/P3 are date of birth/death and P10 point in time, as
        # TestRosterKinds.Vocabulary numbers them
        for statement, prop in [
            ("birthStatement", "P2"),
            ("deathStatement", "P3"),
            ("birthEventStatement", "P10"),
            ("deathEventStatement", "P10"),
        ]:
            assert f"p:{prop} ?{statement} ." in query
            # the best-rank statements, as the truthy value is
            assert f"?{statement} a wikibase:BestRank ." in query
        for name in ("birth", "death", "birthEvent", "deathEvent"):
            assert f"?{name}Node wikibase:timeValue ?{name}Time" in query
            assert f"wikibase:timePrecision ?{name}Precision" in query
            # time and precision sampled together, from one statement
            assert f'CONCAT(STR(?{name}Time), "|",' in query
        assert "(SAMPLE(?birthDate) AS ?birth)" in query
        assert "(SAMPLE(?birthEventDate) AS ?eventBirth)" in query


class TestAppellationAliases:
    """#523: a graph item's alias that one of its `referred_to_as`
    statements gives as an appellation (ADR-0018) keys nothing in
    reconcile's blocking; its name forms still do."""

    @staticmethod
    def grandmothers(**extra) -> list[RosterEntity]:
        return [
            RosterEntity(
                qid="Q40",
                label="Hildegard Löbig",
                aliases=["Oma", "Hilde"],
                appellations=["Oma"],
                kind="human",
                **extra,
            ),
            RosterEntity(
                qid="Q41",
                label="Sipkje Faxridge",
                aliases=["oma"],
                appellations=["Oma"],
                kind="human",
            ),
        ]

    def test_two_grandmothers_aliased_oma_are_no_candidate_pair(self):
        llm = ScriptedLlm(json.dumps({"done": True}))
        assert propose_graph_pairs(llm, self.grandmothers()) == []
        assert llm.prompts == []

    def test_the_alias_keys_without_its_referred_to_as_statement(self):
        """The rule reads the statements, never the alias's wording: an
        "Oma" alias no statement gives as an appellation still keys."""
        roster = [
            entity.model_copy(update={"appellations": []})
            for entity in self.grandmothers()
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 1

    def test_one_items_appellation_never_hides_anothers_name(self):
        """Each item's appellations leave out its own aliases only."""
        roster = self.grandmothers()
        roster[1] = roster[1].model_copy(update={"appellations": []})
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 0
        roster[0] = roster[0].model_copy(update={"appellations": []})
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 1

    def test_a_shared_real_name_still_pairs(self):
        roster = [
            *self.grandmothers(),
            RosterEntity(
                qid="Q42",
                label="Hildegard Laudler",
                aliases=["Oma", "Hildegard Löbig"],
                appellations=["Oma"],
                kind="human",
            ),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        (prompt,) = llm.prompts
        assert "Q40" in prompt and "Q42" in prompt
        assert "Q41" not in prompt

    @pytest.mark.parametrize(
        ("statement", "value"),
        [
            ("nicknames", "Hilde"),
            ("birth_names", "Hildegard Laudler"),
            ("official_names", "Hilde Löbig"),
        ],
    )
    def test_a_typed_name_statement_keys_though_it_is_an_appellation(
        self, statement, value
    ):
        """A typed name statement keys whatever its value: a nickname, a
        birth name or an official name that is also an appellation still
        meets an alias of the same form."""
        roster = [
            RosterEntity(
                qid="Q40",
                label="Hildegard Löbig",
                aliases=[value],
                appellations=[value],
                kind="human",
                **{statement: [value]},
            ),
            RosterEntity(
                qid="Q43", label="H. Löbig", aliases=[value], kind="human"
            ),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 1

    def test_a_label_that_is_an_appellation_keys_nothing(self):
        """A person the document never names is labelled by its own words
        for them ("Oma"), which the writer leaves out of the aliases: the
        label is filtered as an alias is."""
        never_named = [
            RosterEntity(
                qid=qid, label="Oma", appellations=["Oma"], kind="human"
            )
            for qid in ("Q44", "Q45")
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        assert propose_graph_pairs(llm, never_named) == []
        assert llm.prompts == []
        found = [
            EntityRecord(
                id="e1", label="Hildegard Löbig", aliases=["Oma"], kind="human"
            )
        ]
        assert block_candidates(found, never_named) == {}

    def test_a_name_holding_an_appellation_as_a_word_still_keys(self):
        """Only an alias equal to an appellation is left out: "Peppi Löbig"
        keys though "Peppi" is one."""
        roster = [
            RosterEntity(
                qid="Q46",
                label="Ida Löbig",
                aliases=["Peppi", "Peppi Löbig"],
                appellations=["Peppi"],
                kind="human",
            ),
            RosterEntity(
                qid="Q47",
                label="Ida Sörne",
                aliases=["Peppi Löbig"],
                kind="human",
            ),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 1

    def test_an_alias_matches_its_appellation_as_names_compare(self):
        """An alias and its appellation are compared by name_form_key:
        doubled inner whitespace is collapsed, as well as case folded."""
        roster = [
            RosterEntity(
                qid="Q48",
                label="Rahel Löbig",
                aliases=["Tante  Rahel"],
                appellations=["Tante Rahel"],
                kind="human",
            ),
            RosterEntity(
                qid="Q49",
                label="R. Löbig",
                aliases=["Tante Rahel"],
                kind="human",
            ),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        assert propose_graph_pairs(llm, roster) == []
        assert llm.prompts == []

    @pytest.mark.parametrize("kind", ["place", "organization", "family"])
    def test_a_kind_never_a_person_keys_every_name(self, kind):
        """Appellations are a person's only: a place's alias that its own
        `referred_to_as` statement also gives still keys."""
        roster = [
            RosterEntity(
                qid="Q90",
                label="Snylsjum",
                aliases=["Snylsjum village"],
                appellations=["Snylsjum village"],
                kind=kind,
            ),
            RosterEntity(qid="Q91", label="Snylsjum village", kind=kind),
        ]
        llm = ScriptedLlm(json.dumps({"done": True}))
        propose_graph_pairs(llm, roster)
        assert len(llm.prompts) == 1

    def test_a_document_entity_never_meets_an_appellation_alias(self):
        found = [EntityRecord(id="e1", label="Oma", kind="human")]
        assert block_candidates(found, self.grandmothers()) == {}
        graph = [
            entity.model_copy(update={"appellations": []})
            for entity in self.grandmothers()
        ]
        assert [c.qid for c in block_candidates(found, graph)["e1"]] == [
            "Q40",
            "Q41",
        ]

    def test_fetch_roster_reads_each_referred_to_as_value(self, monkeypatch):
        import io

        from yizkr.core.config import Config
        from yizkr.pipeline import reconcile

        rows = [
            {
                "item": {"value": "http://wikibase.example/entity/Q40"},
                "itemLabel": {"value": "Hildegard Löbig"},
                "kind": {"value": "http://wikibase.example/entity/Q5"},
                "aliases": {"value": "Oma|Omama|Hilde"},
                "appellations": {"value": "Oma|Omama"},
            }
        ]
        queries = []

        def urlopen(url, timeout):
            queries.append(urllib.parse.unquote_plus(url))
            body = json.dumps({"results": {"bindings": rows}})
            return io.BytesIO(body.encode())

        monkeypatch.setattr(reconcile.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(
            reconcile, "unsupported_in_graph", lambda *a, **k: None
        )
        config = Config(
            wikibase_url="http://wikibase.example",
            concept_uri="http://wikibase.example",
            wdqs_url="http://query.example",
            sparql_url="http://query.example/sparql",
            admin_name="x",
            admin_pass="x",
            env_file=None,
        )
        (item,) = reconcile.fetch_roster(TestRosterKinds.Vocabulary(), config)
        assert item.appellations == ["Oma", "Omama"]
        assert item.aliases == ["Oma", "Omama", "Hilde"]
        (query,) = queries
        flat = " ".join(query.split())
        # P15 is referred_to_as, as TestRosterKinds.Vocabulary numbers it:
        # each statement's value at any rank but deprecated, not the
        # truthy one alone
        assert "?item p:P15 ?appellationStatement ." in flat
        assert "?appellationStatement ps:P15 ?appellation ." in flat
        assert (
            "FILTER NOT EXISTS { ?appellationStatement wikibase:rank"
            " wikibase:DeprecatedRank . }"
        ) in flat
        # grouped per item in a sub-select of their own
        assert (
            'SELECT ?item (GROUP_CONCAT(DISTINCT ?appellation; separator="|")'
            " AS ?itemAppellations)"
        ) in flat
        assert "(SAMPLE(?itemAppellations) AS ?appellations)" in flat
