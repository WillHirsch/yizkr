"""Each container of a place's mention record carries its evidence, and is
checked on its own (#569, ADR-0035 §2.2 and §5 as amended).

A `within` entry is a name and a verbatim excerpt of the transcription
that states the containment. Each fails for one reason, counted apart:
`no-evidence`, `evidence-not-in-source` and `evidence-does-not-name`. By
the owner's option B a failing entry is dropped alone and the record
stands. A bare-string entry gives no evidence and is dropped as
`no-evidence`; a stored batch's bare strings still parse. The GEDCOM
adapter gives each entry its PLAC's value. The critic may flag
`evidence_does_not_state`, advisory, reading the evidence where it stands
(#607). An entry of the wrong shape is dropped alone as `malformed` (#607).

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
    MALFORMED,
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
        assert found.locate("Drakeradiel,  Fryslân") == 3
        assert found.locate("Drakeradiel,\n\tFryslân") == 3

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


class TestWholeWords:
    """An excerpt may neither start nor end inside a word of the
    transcription, so a container the model added by quoting part of a
    word does not stand."""

    def test_an_excerpt_ending_inside_a_word_is_no_match(self):
        found = Transcript("Snylsjum, gemeente Drakeradiel")
        assert found.locate("gemeente Drake") is None
        assert found.locate("Snylsjum, gemeente Drakeradiel") == 1

    def test_an_excerpt_starting_inside_a_word_is_no_match(self):
        assert Transcript("Fläckfeld, Kreis Mockburg").locate("burg") is None
        assert Transcript(SENDER).locate("ryslân, Nederlân") is None

    def test_a_later_whole_word_match_is_found(self):
        found = Transcript("Mockburger Weg\nKreis Mockburg")
        assert found.locate("Mockburg") == 2

    def test_punctuation_at_an_excerpt_s_edge_needs_no_boundary(self):
        assert Transcript(SENDER).locate(", gemeente") == 1

    def test_a_partial_word_container_is_not_in_the_source(self):
        reason, _ = container_problem(
            Container(name="Drake", evidence="Snylsjum, gemeente Drake"),
            Transcript("Snylsjum, gemeente Drakeradiel"),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE
        reason, _ = container_problem(
            Container(name="Burg", evidence="burg"),
            Transcript("Fläckfeld, Kreis Mockburg"),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE


class TestAWordBrokenForLayout:
    """A word a line breaks at a hyphen for layout alone is one word: the
    reading that keeps the hyphen takes only an excerpt that crosses it,
    so a part of the word on either side is no match (#569, review)."""

    def test_a_part_before_the_hyphen_is_no_match(self):
        assert Transcript(LETTER).locate("Snylsjum, gemeente Drake") is None
        assert (
            Transcript("Geboren in Mockburg-\nSüd").locate(
                "Geboren in Mockburg"
            )
            is None
        )

    def test_a_part_after_the_hyphen_is_no_match(self):
        assert Transcript(LETTER).locate("radiel, Fryslân") is None

    def test_a_match_may_not_start_or_end_at_another_kept_hyphen(self):
        # two breaks in one word: an excerpt crossing one hyphen but
        # stopping at the other quotes part of the word
        found = Transcript("Mock-\nburg-\nSüd")
        assert found.locate("burg-Süd") is None
        assert found.locate("Mock-burg") is None
        assert found.locate("Mock-burg-Süd") == 1

    def test_an_excerpt_crossing_the_hyphen_matches_both_ways(self):
        found = Transcript(LETTER)
        assert found.locate("gemeente Drakeradiel") == 3
        assert found.locate("gemeente Drake-radiel") == 3

    def test_a_container_quoting_part_of_the_word_is_not_in_the_source(self):
        reason, _ = container_problem(
            Container(name="Drake", evidence="Snylsjum, gemeente Drake"),
            Transcript(LETTER),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE
        reason, _ = container_problem(
            Container(name="Mockburg", evidence="Geboren in Mockburg"),
            Transcript("Geboren in Mockburg-\nSüd"),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE

    def test_an_excerpt_s_own_kept_hyphen(self):
        assert (
            Transcript("Mockburg-Süd, Kreis Mockburg").locate(
                "Mockburg-\nSüd, Kreis Mockburg"
            )
            == 1
        )
        # and its name is read in the kept reading too
        assert container_problem(
            Container(name="Mockburg-Süd", evidence="Mockburg-\nSüd"), None
        ) == (None, None)


class TestReadings:
    """The edge cases of the reading both sides share."""

    def test_a_hyphenated_name_broken_at_its_hyphen(self):
        # Mockburg-Süd, the universe's worked example (docs/EXAMPLES.md)
        found = Transcript("Mockburg-\nSüd, Kreis Mockburg")
        assert found.locate("Mockburg-Süd, Kreis Mockburg") == 1
        # the joined reading is tried first, and still found
        assert found.locate("MockburgSüd") == 1
        assert found.span("Mockburg-Süd, Kreis Mockburg") == (1, 2)

    def test_a_spaced_dash_at_a_line_s_end_is_no_hyphen(self):
        assert (
            Transcript("Snylsjum -\nFryslân").locate("Snylsjum - Fryslân") == 1
        )

    def test_a_blank_line_is_one_space(self):
        assert Transcript("a,\n\nb").locate("a, b") == 1

    def test_whitespace_at_a_line_s_edges(self):
        found = Transcript("Snylsjum, gemeente Drake-  \n  radiel")
        assert found.locate("Drakeradiel") == 1
        assert found.locate("Snylsjum, gemeente") == 1

    def test_typographic_punctuation_reads_plain(self):
        found = Transcript("Sybren van \u2019t Nijks \u2013 Snylsjum")
        assert found.locate("Sybren van 't Nijks - Snylsjum") == 1

    def test_a_span_gives_its_first_and_last_line(self):
        assert Transcript(LETTER).span("Nijks Snylsjum, gemeente") == (2, 3)
        assert Transcript(LETTER).span("Peiltal") is None


class TestEachReason:
    def check(self, container, text=LETTER):
        return container_problem(
            container, None if text is None else Transcript(text)
        )

    def test_evidence_the_source_holds_naming_the_container_stands(self):
        assert self.check(Container(name="Fryslân", evidence=SENDER)) == (
            None,
            (3, 4),
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
        reason, lines = self.check(
            Container(name="Fryslân", evidence="Snylsjum in Fryslân")
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE
        # the line it was most likely meant to quote
        assert lines == (3, 3)

    def test_evidence_that_does_not_name_the_container(self):
        reason, lines = self.check(
            Container(name="Friesland", evidence=SENDER)
        )
        assert (reason, lines) == (EVIDENCE_DOES_NOT_NAME, (3, 4))

    def test_a_name_the_label_holds_but_the_evidence_lacks(self):
        given = snylsjum(entry("Fryslân", "Sybren van 't Nijks"))
        given["label"] = "Snylsjum, Fryslân"
        _, audit = drop_containers(given, Transcript(LETTER))
        assert [found["reason"] for found in audit] == [EVIDENCE_DOES_NOT_NAME]

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
            Transcript(FORM),
        ) == (None, (2, 2))

    def test_with_no_transcript_the_source_is_not_looked_in(self):
        assert self.check(
            Container(name="Fryslân", evidence="Snylsjum in Fryslân"), None
        ) == (None, None)


class TestEveryOccurrence:
    """`Transcript.spans` lists every place an excerpt occurs, `span` the
    first, and `page` the page a line opens on (#607)."""

    def test_every_occurrence_is_listed_in_order(self):
        found = Transcript(LETTER + "\nFryslân")
        assert found.spans("Fryslân") == [(4, 4), (5, 5)]
        assert found.span("Fryslân") == (4, 4)
        assert found.spans(SENDER) == [(3, 4)]
        assert found.spans("Peiltal") == []
        assert found.span("Peiltal") is None

    def test_occurrences_on_one_line_are_one(self):
        found = Transcript("Fryslân, Fryslân")
        assert found.spans("Fryslân") == [(1, 1)]

    def test_a_line_s_page(self):
        from yizkr.pipeline.places import EvidencePages
        from yizkr.pipeline.transcribe import (
            PageProvenance,
            TranscribedPage,
        )

        text = "[Page 1]\nBêste Sipkje,\n[Page 2]\nSnylsjum, Fryslân"
        pages = [
            TranscribedPage(
                page=number,
                start=start,
                end=end,
                status="transcribed",
                provenance=PageProvenance(method="vision", engine="test"),
            )
            for number, start, end in ((1, 9, 22), (2, 32, 49))
        ]
        found = Transcript(text)
        assert [
            getattr(found.page(n, pages), "page", None) for n in range(0, 6)
        ] == [None, None, 1, None, 2, None]
        # the place stage's page of an evidence is the same helper's
        given = Transcription(
            artifact=DOC_REF, text=text, engine="test", pages=pages
        )
        assert EvidencePages(given)("Snylsjum, Fryslân") == ("2", None)


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
            "last_line": 4,
            "line_text": "Snylsjum, gemeente Drake-",
        }

    def test_name_only_evidence_is_kept_and_not_told_apart(self):
        # #607: a container's name alone is a citation like any other,
        # which the critic reads where it stands
        given = snylsjum(
            entry("Fryslân", "Fryslân"),
            entry("Nederlân", "Fryslân, Nederlân"),
        )
        _, audit = drop_containers(given, Transcript(LETTER))
        assert [found["reason"] for found in audit] == [None, None]
        for found in audit:
            assert "name_only" not in found and "names_place" not in found

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


class TestABareString:
    """An entry written as a bare string gives a name alone, no evidence:
    at extraction it is dropped as `no-evidence` like any other, whatever
    the label or an alias holds. A stored batch's bare strings still read
    as containers with no evidence."""

    def test_a_bare_string_the_label_holds_is_dropped(self):
        given = snylsjum("Fryslân", "Nederlân")
        given["label"] = SENDER
        kept, audit = drop_containers(given, Transcript(LETTER))
        assert kept["structure"]["within"] == []
        assert [found["reason"] for found in audit] == [NO_EVIDENCE] * 2
        assert [found["evidence"] for found in audit] == [None, None]

    def test_a_bare_string_an_alias_holds_is_dropped(self):
        given = snylsjum("Fryslân")
        given["aliases"] = ["Snylsjum, Fryslân"]
        _, audit = drop_containers(given, Transcript(LETTER))
        assert [found["reason"] for found in audit] == [NO_EVIDENCE]

    def test_a_bare_string_no_form_holds_is_dropped(self):
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
                | {"extract": False},
                # a second place, its first container's evidence the
                # name alone
                {
                    "id": "e3",
                    "label": "gemeente Drakeradiel",
                    "kind": "place",
                    "extract": False,
                    "structure": {
                        "kind": "place",
                        "names": [{"written": "gemeente Drakeradiel"}],
                        "within": [
                            entry("Fryslân", "Fryslân"),
                            entry("Nederlân", SENDER),
                            # name-only and not in the letter: dropped,
                            # so never counted as name-only kept
                            entry("Friesland", "Friesland"),
                        ],
                    },
                },
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
        place, other = batch.entities
        assert place.structure.containers() == ["gemeente Drakeradiel"]
        assert place.structure.within[0].evidence == SENDER
        assert other.structure.containers() == ["Fryslân", "Nederlân"]

    def test_each_reason_is_counted_apart_from_records_set_aside(
        self, extracted
    ):
        _, coverage, _ = extracted
        records = coverage["records"]
        assert records["containers_checked"] == 7
        assert "containers_name_only" not in records
        assert records["containers_dropped"] == {
            EVIDENCE_DOES_NOT_NAME: 1,
            EVIDENCE_NOT_IN_SOURCE: 2,
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
            ("structure.place.within.0", None),
            ("structure.place.within.1", None),
            ("structure.place.within.2", EVIDENCE_NOT_IN_SOURCE),
        ]
        assert [found["entity"] for found in audit] == ["e1"] * 4 + ["e3"] * 3
        assert audit[2]["line_text"] == "Snylsjum, gemeente Drake-"


class TestAMalformedEntry:
    """An entry of `within` that cannot become a container is dropped
    alone, as `malformed`, and the record stands (#607, ADR-0035 §5)."""

    @pytest.mark.parametrize(
        "malformed",
        [
            # its evidence a list: the 1952 letter's Fryslân
            {"name": "Fryslân", "evidence": [SENDER]},
            {"name": "Fryslân", "evidence": 1952},
            # a key a container has not, the name part's shape among them
            {"name": "Fryslân", "evidence": SENDER, "lines": [3]},
            {"written": "Fryslân"},
            # no name, or one that is no string
            {"evidence": SENDER},
            {"name": None, "evidence": SENDER},
            {"name": ["Fryslân"], "evidence": SENDER},
            # neither an object nor a string
            None,
            1952,
            ["Fryslân", SENDER],
        ],
    )
    def test_each_malformed_shape_is_dropped_alone(self, malformed):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            malformed,
            entry("Nederlân", SENDER),
        )
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert reasons == []
        kept, audit = drop_containers(kept, Transcript(LETTER), taken)
        record = EntityRecord.model_validate(kept)
        assert record.structure.containers() == [
            "gemeente Drakeradiel",
            "Nederlân",
        ]
        assert [n.written for n in record.structure.names] == ["Snylsjum"]
        assert [(found["field"], found["reason"]) for found in audit] == [
            ("structure.place.within.0", None),
            ("structure.place.within.1", MALFORMED),
            ("structure.place.within.2", None),
        ]

    def test_its_audit_keeps_the_entry_and_its_error(self):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            {"name": "Fryslân", "evidence": [SENDER]},
        )
        taken: list[dict] = []
        kept, _ = set_aside_structure(given, taken)
        _, audit = drop_containers(kept, Transcript(LETTER), taken)
        assert audit[1] == {
            "entity": "e1",
            "field": "structure.place.within.1",
            "name": None,
            "evidence": None,
            "reason": MALFORMED,
            "entry": json.dumps(
                {"name": "Fryslân", "evidence": [SENDER]}, ensure_ascii=False
            ),
            "error": "evidence: Input should be a valid string",
            "line": None,
            "last_line": None,
            "line_text": None,
        }

    def test_the_entry_kept_is_clipped(self):
        long = {"name": "Fryslân", "evidence": [SENDER] * 20}
        taken: list[dict] = []
        set_aside_structure(snylsjum(long), taken)
        assert len(taken[0]["entry"]) == 200
        assert json.dumps(long, ensure_ascii=False).startswith(
            taken[0]["entry"]
        )

    def test_null_evidence_stays_no_evidence(self):
        given = snylsjum({"name": "Fryslân", "evidence": None})
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert (reasons, taken) == ([], [])
        _, audit = drop_containers(kept, Transcript(LETTER), taken)
        assert [found["reason"] for found in audit] == [NO_EVIDENCE]

    def test_the_paths_keep_their_place_in_the_list(self):
        # an empty-string entry first is dropped before the indexes count
        given = snylsjum(
            "",
            entry("gemeente Drakeradiel", SENDER),
            {"name": "Fryslân", "evidence": [SENDER]},
            entry("Friesland", SENDER),
        )
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert reasons == [
            "dropped: structure.place.within.0: an empty string"
        ]
        _, audit = drop_containers(kept, Transcript(LETTER), taken)
        assert [(found["field"], found["reason"]) for found in audit] == [
            ("structure.place.within.0", None),
            ("structure.place.within.1", MALFORMED),
            ("structure.place.within.2", EVIDENCE_DOES_NOT_NAME),
        ]
        assert audit[2]["name"] == "Friesland"

    def test_a_record_set_aside_otherwise_counts_none_of_its_containers(
        self,
    ):
        given = snylsjum(
            entry("Fryslân", SENDER), {"name": "Nederlân", "evidence": [1]}
        )
        given["structure"]["names"] = [{"written": "Drakeradiel"}]
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert [reason.split(":")[0] for reason in reasons] == ["addition"]
        assert drop_containers(kept, Transcript(LETTER), taken) == (kept, [])

    def test_a_malformed_entry_alone_leaves_the_record_standing(self):
        given = snylsjum({"name": "Fryslân", "evidence": [SENDER]})
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert reasons == []
        kept, audit = drop_containers(kept, Transcript(LETTER), taken)
        assert EntityRecord.model_validate(kept).structure.within == []
        assert [found["reason"] for found in audit] == [MALFORMED]

    @pytest.mark.parametrize("within", [SENDER, {"name": "Fryslân"}, None])
    def test_a_within_that_is_no_list_stays_schema(self, within):
        given = snylsjum()
        given["structure"]["within"] = within
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert "structure" not in kept and taken == []
        assert [reason.split(":")[0] for reason in reasons] == ["schema"]

    def test_empty_strings_stay_dropped(self):
        given = snylsjum("", entry("", SENDER))
        taken: list[dict] = []
        kept, reasons = set_aside_structure(given, taken)
        assert taken == []
        assert [reason.split(":")[0] for reason in reasons] == ["dropped"] * 2
        given = snylsjum()
        given["structure"]["within"] = ""
        kept, reasons = set_aside_structure(given, taken)
        assert taken == [] and "structure" in kept
        assert [reason.split(":")[0] for reason in reasons] == ["dropped"]

    def test_a_bare_string_stays_no_evidence(self):
        taken: list[dict] = []
        kept, _ = set_aside_structure(snylsjum("Fryslân"), taken)
        assert taken == []
        _, audit = drop_containers(kept, Transcript(LETTER), taken)
        assert [found["reason"] for found in audit] == [NO_EVIDENCE]

    def test_an_entry_given_to_drop_containers_never_raises(self):
        given = snylsjum(
            entry("gemeente Drakeradiel", SENDER),
            {"name": "Fryslân", "evidence": SENDER, "lines": [3]},
            None,
        )
        kept, audit = drop_containers(given, Transcript(LETTER))
        assert [(found["field"], found["reason"]) for found in audit] == [
            ("structure.place.within.0", None),
            ("structure.place.within.1", MALFORMED),
            ("structure.place.within.2", MALFORMED),
        ]
        assert EntityRecord.model_validate(kept).structure.containers() == [
            "gemeente Drakeradiel"
        ]

    def test_the_manifest_is_never_refused_for_one(self):
        answer = json.dumps(
            {
                "entities": [
                    snylsjum(
                        entry("gemeente Drakeradiel", SENDER),
                        {"name": "Fryslân", "evidence": [SENDER]},
                        entry("Nederlân", SENDER),
                    )
                ]
            },
            ensure_ascii=False,
        )
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4, LETTER
        )
        assert problems == []
        (place,) = parsed.entities
        assert place.structure.containers() == [
            "gemeente Drakeradiel",
            "Nederlân",
        ]
        assert [found["reason"] for found in parsed.containers] == [
            None,
            MALFORMED,
            None,
        ]
        assert parsed.set_aside == {} and parsed.set_aside_records == 0

    def test_it_is_counted_in_the_coverage_and_the_report(self, tmp_path):
        from yizkr.pipeline.report import _records

        manifest = {
            "entities": [
                snylsjum(
                    entry("gemeente Drakeradiel", SENDER),
                    {"name": "Fryslân", "evidence": [SENDER]},
                )
                | {"extract": False}
            ]
        }
        state = tmp_path / "state"
        ChunkedClaimExtractor(
            ScriptedLlm([json.dumps(manifest)]), state_dir=state
        ).extract(
            Transcription(artifact=DOC_REF, text=LETTER, engine="test"),
            DOC_REF,
        )
        coverage = json.loads((state / "coverage.json").read_text())
        records = coverage["records"]
        assert records["containers_checked"] == 2
        assert records["containers_dropped"] == {MALFORMED: 1}
        assert records["set_aside"] == {} and records["recordless"] == 0
        assert _records(coverage)["records"][
            "containers_dropped_by_reason"
        ] == {MALFORMED: 1}

    def test_the_reason_is_one_the_report_knows(self):
        from yizkr.claims import CONTAINER_REASONS

        assert MALFORMED == "malformed"
        assert MALFORMED in CONTAINER_REASONS


class TestTheParser:
    def test_an_empty_evidence_written_by_the_model_is_no_evidence(self):
        answer = json.dumps(
            {"entities": [snylsjum(entry("Fryslân", ""), entry("Nederlân"))]}
        )
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4, LETTER
        )
        assert problems == []
        (place,) = parsed.entities
        assert place.structure.within == []
        assert [found["reason"] for found in parsed.containers] == [
            NO_EVIDENCE,
            NO_EVIDENCE,
        ]
        # an empty evidence is no record's reason
        assert parsed.set_aside == {} and parsed.set_aside_records == 0


def with_container(*within) -> dict:
    """The sender's village, its name written with its province."""
    place = snylsjum(*within)
    place["structure"]["names"] = [
        {"written": "Snylsjum, Fryslân", "toponym": "Snylsjum"}
    ]
    return place


class TestALabelAndContainerName:
    """A written name that is the label and one of the record's
    containers stands only while that container stands on its evidence
    (ADR-0035 §5, #655): one dropped leaves the name adding to every
    form, and the record is set aside."""

    def test_a_container_that_stands_keeps_the_record(self):
        answer = json.dumps(
            {
                "entities": [
                    with_container(entry("Fryslân", "Fryslân, Nederlân"))
                ]
            }
        )
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4, LETTER
        )
        assert problems == []
        (place,) = parsed.entities
        assert place.structure.containers() == ["Fryslân"]
        assert parsed.set_aside == {"label and container": 1}
        assert parsed.set_aside_records == 0
        assert len(parsed.containers) == 1

    @pytest.mark.parametrize(
        "container",
        [entry("Fryslân", "Snylsjum, Fryslân"), "Fryslân"],
        ids=["evidence-not-in-source", "a bare string"],
    )
    def test_a_container_dropped_sets_the_record_aside(self, container):
        answer = json.dumps({"entities": [with_container(container)]})
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4, LETTER
        )
        assert problems == []
        (place,) = parsed.entities
        assert place.structure is None
        assert place.label == "Snylsjum" and place.kind == "place"
        assert parsed.set_aside == {"label and container": 1, "addition": 1}
        assert parsed.set_aside_records == 1
        # the record set aside leaves no container to audit
        assert parsed.containers == []


class TestTheReport:
    def test_a_reason_the_report_does_not_know_is_other(self):
        from yizkr.pipeline.report import OTHER, _records

        counted = _records(
            {
                "records": {
                    "given": 1,
                    "recordless": 0,
                    "by_status": {},
                    "set_aside": {},
                    "containers_checked": 3,
                    "containers_dropped": {
                        NO_EVIDENCE: 1,
                        "evidence-too-long": 2,
                    },
                    "containers_name_only": 1,
                },
                "notes": {},
            }
        )["records"]
        assert counted["containers_dropped_by_reason"] == {
            OTHER: 2,
            NO_EVIDENCE: 1,
        }
        # an older coverage's name-only count is not read (#607)
        assert "containers_name_only" not in counted


class TestThePrompt:
    def test_the_evidence_leaves_out_the_line_numbers(self):
        prompt = " ".join(ENTITY_MANIFEST_PROMPT.split())
        assert (
            "Copy the words alone, never the line numbers the document is "
            "shown with."
        ) in prompt

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
                        entry("gemeente Drakeradiel", SENDER),
                        entry("Fryslân"),
                        entry("Nederlân", "Nederlân"),
                    )
                ],
                "claims": [],
            }
        )

    def test_the_prompt_lists_each_container_with_its_evidence(self):
        from tests.unit.test_critique import ScriptedLlm as CriticLlm
        from yizkr.pipeline.critique import run_critique

        llm = CriticLlm('{"done": true}')
        letter = Transcription(artifact=DOC_REF, text=LETTER, engine="test")
        run_critique(llm, self.batch(), letter)
        (prompt,) = llm.prompts
        # each evidence where it stands (#607)
        assert (
            f'- e1 within gemeente Drakeradiel | lines 3-4: "{SENDER}"\n'
            "- e1 within Fryslân | -\n"
            '- e1 within Nederlân | line 4: "Nederlân"\n'
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

    def test_the_prompt_has_no_name_only_rule(self):
        # #607: one support test replaces the special cases
        from yizkr.pipeline.critique import CATEGORIES, CRITIQUE_PROMPT

        prompt = " ".join(f"{CRITIQUE_PROMPT} {CATEGORIES}".split())
        assert "outside a field of a form" not in prompt
        assert "the container's name alone included" not in prompt

    def test_a_container_on_an_entity_with_no_place_record_is_invalid(self):
        from yizkr.claims import load_vocabulary
        from yizkr.pipeline.critique import Invalid, check

        batch = self.batch().model_copy(deep=True)
        batch.entities.append(
            EntityRecord.model_validate(
                {"id": "e9", "label": "Sybren van 't Nijks", "kind": "human"}
            )
        )
        found = check(self.finding(entity="e9"), batch, load_vocabulary())
        assert isinstance(found, Invalid)
        assert found.why == [
            "container 'gemeente Drakeradiel' is not one the entity's "
            "record lists"
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
