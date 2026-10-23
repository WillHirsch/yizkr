"""Each container of a place's mention record carries its evidence, and is
checked on its own (#569, ADR-0035 §2.2 and §5 as amended).

A `within` entry is a name and a verbatim excerpt of the transcription
that states the containment. Each fails for one reason, counted apart:
`no-evidence`, `evidence-not-in-source` and `evidence-does-not-name`. By
the owner's option B a failing entry is dropped alone and the record
stands. An older answer's bare-string entry is read with the check that
answer was made for. The GEDCOM adapter gives each entry its PLAC's value.
The critic may flag `evidence_does_not_state`, advisory.

Every text is the fictional universe's (docs/EXAMPLES.md): the 1952
letter's sender, "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân", and
the 1941 certificate's form about Koshnovka, whose fields run oblast,
district, village.
"""

from __future__ import annotations

import json

import pytest

from tests.unit.test_chunking import DOC_REF, ScriptedLlm
from yizkr.claims import (
    EVIDENCE_DOES_NOT_NAME,
    EVIDENCE_NOT_IN_SOURCE,
    NO_EVIDENCE,
    ClaimBatch,
    Container,
    EntityRecord,
    PlaceStructure,
    container_problem,
    container_problems,
    drop_containers,
    set_aside_structure,
)
from yizkr.core.verbatim import Transcript, words
from yizkr.pipeline.chunking import (
    ENTITY_MANIFEST_PROMPT,
    ChunkedClaimExtractor,
)
from yizkr.pipeline.transcribe import Transcription

# the 1952 letter's sender, the mention broken over two lines at a hyphen
LETTER = "\n".join(
    [
        "Bêste Sipkje,",  # 1
        "Sybren van 't Nijks",  # 2
        "Snylsjum, gemeente Drake-",  # 3
        "radiel,  Fryslân, Nederlân",  # 4
    ]
)
# the 1941 certificate's form, a field a line, oblast first
FORM = "\n".join(
    [
        "Oblast: Chadsk Oblast",  # 1
        "District: Kaldovsky District",  # 2
        "Village: Koshnovka",  # 3
    ]
)
SENDER = "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân"


def snylsjum(*within) -> dict:
    return {
        "id": "e1",
        "label": "Snylsjum",
        "kind": "place",
        "mention_lines": [3],
        "structure": {
            "kind": "place",
            "names": [{"written": "Snylsjum"}],
            "within": list(within),
        },
    }


def koshnovka(*within) -> dict:
    return {
        "id": "e2",
        "label": "Koshnovka",
        "kind": "place",
        "mention_lines": [3],
        "structure": {
            "kind": "place",
            "names": [{"written": "Koshnovka"}],
            "within": list(within),
        },
    }


def entry(name: str, evidence: str | None = None) -> dict:
    found = {"name": name}
    if evidence is not None:
        found["evidence"] = evidence
    return found


class TestTheComparison:
    """The one comparison a container's evidence, and a claim's excerpt,
    is read by (core/verbatim.py)."""

    def test_an_excerpt_is_found_folded_with_its_line(self):
        found = Transcript(LETTER)
        assert found.locate("SNYLSJUM, Gemeente") == 3
        assert found.locate("Fryslan, Nederlan") == 4
        assert found.locate("Sybren van 't Nijks") == 2

    def test_whitespace_and_line_breaks_are_one_space(self):
        found = Transcript(LETTER)
        assert found.locate("Nijks Snylsjum") == 2
        assert found.locate("radiel, Fryslân") == 4
        assert found.locate("radiel,\n\tFryslân") == 4

    def test_a_hyphen_at_a_line_s_end_is_joined(self):
        found = Transcript(LETTER)
        assert found.locate(SENDER) == 3
        # as the evidence may write it too
        assert found.locate("gemeente Drake-\nradiel") == 3
        assert words("Drake-\nradiel") == ["drakeradiel"]
        # a hyphen inside a line is no line end
        assert Transcript("Drake- radiel").locate("Drakeradiel") is None

    def test_anything_else_is_no_match(self):
        found = Transcript(LETTER)
        assert found.locate("Snylsjum, Drakeradiel") is None
        assert found.locate("Nederlân, Fryslân") is None
        assert found.locate("  ") is None

    def test_the_nearest_line_shares_the_most_words(self):
        found = Transcript(FORM)
        assert found.nearest("Kaldovsky District, Chadsk") == 2
        assert found.nearest("Peiltal") is None
        assert found.line(2) == "District: Kaldovsky District"
        assert found.line(9) == ""


class TestEachReason:
    forms = ["Snylsjum"]

    def check(self, container, text=LETTER):
        return container_problem(
            container,
            self.forms,
            None if text is None else Transcript(text),
        )

    def test_evidence_the_source_holds_naming_the_container_stands(self):
        assert self.check(Container(name="Fryslân", evidence=SENDER)) == (
            None,
            3,
        )

    def test_no_evidence(self):
        assert self.check(Container(name="Fryslân")) == (NO_EVIDENCE, None)
        # evidence of no word is none
        assert self.check(Container(name="Fryslân", evidence=" - ")) == (
            NO_EVIDENCE,
            None,
        )

    def test_evidence_not_in_source(self):
        # the model's own words, which the letter does not hold
        reason, line = self.check(
            Container(name="Fryslân", evidence="Snylsjum in Fryslân")
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE
        # the line it was most likely meant to quote
        assert line == 3

    def test_evidence_that_does_not_name_the_container(self):
        reason, line = self.check(Container(name="Friesland", evidence=SENDER))
        assert (reason, line) == (EVIDENCE_DOES_NOT_NAME, 3)

    def test_a_name_is_compared_as_whole_words(self):
        reason, _ = self.check(
            Container(name="Drake", evidence="gemeente Drakeradiel")
        )
        assert reason == EVIDENCE_DOES_NOT_NAME

    def test_a_form_s_field_line_is_evidence(self):
        assert container_problem(
            Container(
                name="Kaldovsky District",
                evidence="District: Kaldovsky District",
            ),
            ["Koshnovka"],
            Transcript(FORM),
        ) == (None, 2)

    def test_with_no_transcript_the_source_is_not_looked_in(self):
        assert self.check(
            Container(name="Fryslân", evidence="Snylsjum in Fryslân"), None
        ) == (None, None)


class TestOptionB:
    """A container that fails is dropped alone: the record stands with
    its names, keys and other containers (the owner's option B)."""

    def test_a_failing_container_is_dropped_alone(self):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            entry("Fryslân", "Snylsjum in Fryslân"),
            entry("Nederlân"),
        )
        kept, reasons = set_aside_structure(given)
        assert reasons == []
        kept, audit = drop_containers(kept, Transcript(LETTER))
        record = EntityRecord.model_validate(kept)
        assert record.structure.containers() == ["gemeente Drakeradiel"]
        assert [n.written for n in record.structure.names] == ["Snylsjum"]
        assert [found["reason"] for found in audit] == [
            None,
            EVIDENCE_NOT_IN_SOURCE,
            NO_EVIDENCE,
        ]

    def test_the_audit_keeps_the_path_evidence_and_line(self):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            entry("Friesland", SENDER),
        )
        _, audit = drop_containers(given, Transcript(LETTER))
        assert audit[1] == {
            "entity": "e1",
            "field": "structure.place.within.1",
            "name": "Friesland",
            "evidence": SENDER,
            "reason": EVIDENCE_DOES_NOT_NAME,
            "line": 3,
            "line_text": "Snylsjum, gemeente Drake-",
        }

    def test_a_record_whose_containers_all_stand_is_unchanged(self):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            entry("Fryslân", SENDER),
            entry("Nederlân", SENDER),
        )
        kept, audit = drop_containers(given, Transcript(LETTER))
        assert kept == given
        assert [found["reason"] for found in audit] == [None] * 3

    def test_a_container_is_no_addition(self):
        # a form's container is never in the place's label: the
        # no-addition check reads names alone
        given = koshnovka(
            entry("Kaldovsky District", "District: Kaldovsky District"),
            entry("Chadsk Oblast", "Oblast: Chadsk Oblast"),
        )
        kept, reasons = set_aside_structure(given)
        assert reasons == [] and "structure" in kept
        kept, audit = drop_containers(kept, Transcript(FORM))
        assert EntityRecord.model_validate(kept).structure.containers() == [
            "Kaldovsky District",
            "Chadsk Oblast",
        ]
        assert [found["line"] for found in audit] == [2, 1]

    def test_a_set_aside_record_has_no_container_to_check(self):
        given = snylsjum(entry("Fryslân", SENDER))
        given["structure"]["names"] = [{"written": "Drakeradiel"}]
        kept, reasons = set_aside_structure(given)
        assert [reason.split(":")[0] for reason in reasons] == ["addition"]
        assert drop_containers(kept, Transcript(LETTER)) == (kept, [])

    def test_an_empty_evidence_is_absent_and_gives_no_evidence(self):
        given = snylsjum(entry("Fryslân", ""), "")
        kept, reasons = set_aside_structure(given)
        assert reasons == [
            "dropped: structure.place.within.1: an empty string"
        ]
        kept, audit = drop_containers(kept, Transcript(LETTER))
        assert [found["reason"] for found in audit] == [NO_EVIDENCE]
        assert EntityRecord.model_validate(kept).structure.within == []


class TestAnOlderAnswer:
    """An answer made before entries carried evidence wrote bare strings.
    Such an entry is read with the check that answer was made for: it
    stands where its name occurs in the label or an alias, and is dropped
    as `no-evidence` otherwise. A stored batch's bare strings read as
    containers with no evidence."""

    def test_a_bare_string_the_label_holds_stands(self):
        given = snylsjum("Fryslân", "Nederlân")
        given["label"] = SENDER
        kept, audit = drop_containers(given, Transcript(LETTER))
        assert kept == given
        assert [found["reason"] for found in audit] == [None, None]
        assert [found["evidence"] for found in audit] == [None, None]

    def test_a_bare_string_no_form_holds_is_dropped_as_no_evidence(self):
        kept, audit = drop_containers(snylsjum("Fryslân"), Transcript(LETTER))
        assert [found["reason"] for found in audit] == [NO_EVIDENCE]
        assert kept["structure"]["within"] == []

    def test_a_stored_batch_s_bare_strings_read_as_containers(self):
        record = PlaceStructure.model_validate(
            {"kind": "place", "within": ["Kreis Mockburg", "Hesse"]}
        )
        assert record.within == [
            Container(name="Kreis Mockburg"),
            Container(name="Hesse"),
        ]
        assert record.containers() == ["Kreis Mockburg", "Hesse"]


class TestTheExtraction:
    """The extractor drops each failing container alone, counts each
    reason in its coverage, apart from the records set aside, and keeps
    each container checked in its local containers.json."""

    @pytest.fixture
    def extracted(self, tmp_path):
        manifest = {
            "entities": [
                snylsjum(
                    entry("gemeente Drakeradiel", SENDER),
                    entry("Fryslân", "Snylsjum in Fryslân"),
                    entry("Friesland", SENDER),
                    entry("Nederlân"),
                )
                | {"extract": False}
            ]
        }
        llm = ScriptedLlm([json.dumps(manifest)])
        state = tmp_path / "state"
        batch = ChunkedClaimExtractor(llm, state_dir=state).extract(
            Transcription(artifact=DOC_REF, text=LETTER, engine="test"),
            DOC_REF,
        )
        coverage = json.loads((state / "coverage.json").read_text())
        audit = json.loads((state / "containers.json").read_text())
        return batch, coverage, audit

    def test_the_record_stands_with_the_container_that_stands(self, extracted):
        batch, _, _ = extracted
        (place,) = batch.entities
        assert place.structure.containers() == ["gemeente Drakeradiel"]
        assert place.structure.within[0].evidence == SENDER

    def test_each_reason_is_counted_apart_from_records_set_aside(
        self, extracted
    ):
        _, coverage, _ = extracted
        records = coverage["records"]
        assert records["containers_checked"] == 4
        assert records["containers_dropped"] == {
            EVIDENCE_DOES_NOT_NAME: 1,
            EVIDENCE_NOT_IN_SOURCE: 1,
            NO_EVIDENCE: 1,
        }
        assert records["set_aside_records"] == 0
        assert records["set_aside"] == {}
        assert records["recordless"] == 0
        # the coverage counts, and quotes nothing
        assert "Snylsjum" not in json.dumps(coverage)

    def test_the_local_audit_keeps_each_container(self, extracted):
        _, _, audit = extracted
        assert [(found["field"], found["reason"]) for found in audit] == [
            ("structure.place.within.0", None),
            ("structure.place.within.1", EVIDENCE_NOT_IN_SOURCE),
            ("structure.place.within.2", EVIDENCE_DOES_NOT_NAME),
            ("structure.place.within.3", NO_EVIDENCE),
        ]
        assert audit[2]["line_text"] == "Snylsjum, gemeente Drake-"


class TestThePrompt:
    def test_the_manifest_asks_for_each_container_s_evidence(self):
        prompt = " ".join(ENTITY_MANIFEST_PROMPT.split())
        assert '"within": [{{"name": "...", "evidence": "..."}}]' in prompt
        assert (
            "each of Snylsjum's three containers takes the evidence "
            '"Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân"'
        ) in prompt
        assert (
            'Koshnovka\'s "Kaldovsky District" takes its district line, '
            '"District: Kaldovsky District"'
        ) in prompt


class TestTheGedcomAdapter:
    """Each container takes its PLAC's value as its evidence, and a
    placeholder stays in `within`, record-only (#582)."""

    def adapted(self, *placs: str) -> ClaimBatch:
        from tests.unit.test_gedcom_containment import adapted, born

        return adapted(born(*placs)).batch

    def test_each_container_takes_the_plac_as_its_evidence(self):
        plac = "Fläckfeld, Kreis Mockburg, Hesse, Germany"
        batch = self.adapted(plac)
        places = {e.label: e for e in batch.entities if e.kind == "place"}
        fläckfeld = places[plac].structure
        assert fläckfeld.within == [
            Container(name=name, evidence=plac)
            for name in ("Kreis Mockburg", "Hesse", "Germany")
        ]
        hesse = places["Hesse, Germany"].structure
        assert hesse.within == [Container(name="Germany", evidence=plac)]
        for place in places.values():
            assert container_problems(place) == []

    def test_a_placeholder_stays_in_within_with_its_evidence(self):
        plac = "Fläckfeld, unbekannt, Hesse, Germany"
        batch = self.adapted(plac)
        places = {e.label: e for e in batch.entities if e.kind == "place"}
        assert places[plac].structure.within[0] == Container(
            name="unbekannt", evidence=plac
        )
        assert "unbekannt, Hesse, Germany" not in places
        part_of = [c for c in batch.claims if c.property == "part_of"]
        assert len(part_of) == 1


class TestTheCritic:
    """The critic sees each container with its evidence, and may flag
    one whose evidence does not state the containment, naming it."""

    def batch(self) -> ClaimBatch:
        from tests.unit.test_critique import RUN

        return ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "entities": [
                    snylsjum(
                        entry("gemeente Drakeradiel", SENDER), entry("Fryslân")
                    )
                ],
                "claims": [],
            }
        )

    def test_the_prompt_lists_each_container_with_its_evidence(self):
        from tests.unit.test_critique import ScriptedLlm as CriticLlm
        from yizkr.pipeline.critique import run_critique

        llm = CriticLlm('{"done": true}')
        run_critique(llm, self.batch())
        (prompt,) = llm.prompts
        assert (
            f'- e1 within gemeente Drakeradiel: "{SENDER}"\n'
            "- e1 within Fryslân: -\n"
        ) in prompt
        assert "evidence_does_not_state" in prompt

    def finding(self, **changes) -> dict:
        return {
            "entity": "e1",
            "container": "gemeente Drakeradiel",
            "axis": "existence",
            "category": "evidence_does_not_state",
            "issue": "the excerpt lists the places",
        } | changes

    def check(self, **changes):
        from yizkr.claims import load_vocabulary
        from yizkr.pipeline.critique import check

        return check(self.finding(**changes), self.batch(), load_vocabulary())

    def test_a_finding_naming_a_container_of_the_entity_is_valid(self):
        from yizkr.pipeline.critique import Finding, note

        found = self.check()
        assert isinstance(found, Finding)
        assert "within 'gemeente Drakeradiel'" in note(found)

    def test_a_container_the_record_does_not_list_is_invalid(self):
        from yizkr.pipeline.critique import Invalid

        found = self.check(container="Friesland")
        assert isinstance(found, Invalid)
        assert found.why == [
            "container 'Friesland' is not one the entity's record lists"
        ]

    def test_the_category_names_a_container(self):
        from yizkr.pipeline.critique import Invalid

        found = self.check(container=None)
        assert isinstance(found, Invalid)
        assert found.why == [
            "an evidence_does_not_state finding names no container"
        ]


class TestTheReaders:
    """Clustering and the place stage read the containers that stand by
    name, nearest first, as they read bare names (ADR-0035 §7, ADR-0038
    §8.5)."""

    def test_clustering_nests_by_the_containers_names(self):
        from tests.unit.test_clustering import (
            document,
            identity_of,
            merged,
            place,
            register,
        )

        letter = document(
            "letter-to-sipkje-1952",
            [
                place(
                    "e1",
                    SENDER,
                    names=["Snylsjum"],
                    within=[
                        entry("gemeente Drakeradiel", SENDER),
                        entry("Fryslân", SENDER),
                        entry("Nederlân", SENDER),
                    ],
                )
            ],
            [],
            {"e1": "snylsjum"},
        )

        def snylsjum(name: str, within: list, truth: str):
            return register(
                name,
                [place("e1", "Snylsjum", names=["Snylsjum"], within=within)],
                [],
                {"e1": truth},
            )

        # an older batch's bare name nests in the letter's chain
        province = snylsjum("province", ["Fryslân"], "snylsjum")
        assert merged([letter, province], identity_of(letter, province)) == [
            {"snylsjum"}
        ]
        other = snylsjum(
            "other", [entry("Xxxprovinz", "Snylsjum, Xxxprovinz")], "other"
        )
        assert merged([letter, other], identity_of(letter, other)) == []

    def test_the_place_stage_reads_the_chain_nearest_first(self):
        from tests.unit.test_places import CREATED, DEATH, _batch, _entity
        from yizkr.pipeline.places import stated_links

        batch = _batch(
            [
                _entity(
                    "kosh",
                    "Koshnovka",
                    [
                        entry(
                            "Kaldovsky District",
                            "District: Kaldovsky District",
                        ),
                        entry("Chadsk Oblast", "Oblast: Chadsk Oblast"),
                    ],
                ),
                _entity("kald", "Kaldovsky District"),
                _entity("chadsk", "Chadsk Oblast"),
            ]
        )
        links = stated_links(batch, CREATED, DEATH)
        assert [(link.index, link.container) for link in links] == [
            (0, "Kaldovsky District"),
            (1, "Chadsk Oblast"),
        ]
        assert links[0].chain == ("Kaldovsky District", "Chadsk Oblast")

    def test_the_adjudicator_sees_the_containers_by_name(self):
        from yizkr.pipeline import adjudication

        record = EntityRecord.model_validate(
            snylsjum(entry("Fryslân", SENDER))
        )
        assert adjudication.record_line(record) == (
            'record: {"status": "toponym", "names": [{"written": '
            '"Snylsjum"}], "within": ["Fryslân"]}'
        )
