"""Offline tests for the containment a GEDCOM PLAC states (ADR-0035 §6,
ADR-0038 §1, §2 and §8.5; #571): each jurisdiction of a PLAC is a place
of its own, named by its own name, and each link of the PLAC's chain is
an undated `part_of` claim the export states, which the place stage
matches by name.

The fixture is the example universe's GEDCOM rendering, and every inline
snippet uses the same universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

from pathlib import Path

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.places import PlaceRecord, build_worklist, stated_links

FIXTURE = Path(__file__).parent.parent / "fixtures" / "example-family.ged"
EXPORT = ArtifactRef.from_path(FIXTURE)


def adapted(text: str | None = None):
    if text is None:
        return adapt_gedcom(FIXTURE.read_text(), artifact=EXPORT)
    whole = "0 HEAD\n1 CHAR UTF-8\n" + text + "0 TRLR\n"
    return adapt_gedcom(whole, artifact=EXPORT)


def born(*placs: str) -> str:
    """Eugen's record, born in each of `placs`."""
    return "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n1 BIRT\n" + "".join(
        f"2 PLAC {plac}\n" for plac in placs
    )


def places(batch: ClaimBatch) -> dict[str, object]:
    """The batch's places, by label."""
    return {e.label: e for e in batch.entities if e.kind == "place"}


def containment(batch: ClaimBatch) -> set[tuple[str, str]]:
    """Each `part_of` claim, as (place, container) by their labels."""
    labels = {e.id: e.label for e in batch.entities}
    return {
        (labels[c.subject.local_id], labels[c.value.value.local_id])
        for c in batch.claims
        if c.property == "part_of"
    }


def placs() -> list[str]:
    return [
        line.split(" PLAC ", 1)[1]
        for line in FIXTURE.read_text().splitlines()
        if " PLAC " in line
    ]


def tails(plac: str) -> list[str]:
    parts = [part.strip() for part in plac.split(",")]
    return [", ".join(parts[i:]) for i in range(1, len(parts))]


class TestJurisdictions:
    def test_each_distinct_plac_tail_is_one_place(self):
        batch = adapted().batch
        expected = {*placs(), *(tail for p in placs() for tail in tails(p))}
        found = [e.label for e in batch.entities if e.kind == "place"]
        assert sorted(found) == sorted(expected)
        # one entity of each, however many PLACs end in it
        assert len(found) == len(set(found))

    def test_a_plac_equal_to_a_tail_is_that_tail_s_place(self):
        # (here) Flörburg is a PLAC of its own and the tail of the
        # addresses in it: one place, which the addresses lie within
        batch = adapted().batch
        floerburg = places(batch)["Flörburg, Hesse, Germany"]
        assert (
            "Brunnenring 35, Flörburg, Hesse, Germany",
            "Flörburg, Hesse, Germany",
        ) in containment(batch)
        located = {
            c.value.value.local_id
            for c in batch.claims
            if c.property == "place_of_birth" and c.subject.local_id == "i8"
        }
        assert located == {floerburg.id}

    def test_a_jurisdiction_s_record_names_it_within_the_rest(self):
        batch = adapted().batch
        hesse = places(batch)["Hesse, Germany"]
        assert [n.written for n in hesse.structure.names] == ["Hesse"]
        assert hesse.structure.within == ["Germany"]
        germany = places(batch)["Germany"]
        assert [n.written for n in germany.structure.names] == ["Germany"]
        assert germany.structure.within == []

    def test_each_place_carries_its_own_name(self):
        batch = adapted().batch
        for entity in places(batch).values():
            own = entity.structure.names[0].written
            assert own in (entity.label, *entity.aliases), entity.label
        assert places(batch)["Flörburg, Hesse, Germany"].aliases == [
            "Flörburg"
        ]
        # a name that is the label is no alias of it
        assert places(batch)["Germany"].aliases == []

    def test_plac_strings_that_differ_in_spacing_are_one_place(self):
        batch = adapted(
            born("Flörburg, Hesse, Germany", "Flörburg,Hesse ,  Germany")
        ).batch
        assert sorted(places(batch)) == [
            "Flörburg, Hesse, Germany",
            "Germany",
            "Hesse, Germany",
        ]
        # each spelling's line states each link: a claim of its own, citing
        # it as written
        labels = {e.id: e.label for e in batch.entities}
        cited = {
            (
                labels[c.subject.local_id],
                c.source.transcription_excerpt.text,
            )
            for c in batch.claims
            if c.property == "part_of"
        }
        assert cited == {
            (place, f"2 PLAC {line}")
            for place in ("Flörburg, Hesse, Germany", "Hesse, Germany")
            for line in (
                "Flörburg, Hesse, Germany",
                "Flörburg,Hesse ,  Germany",
            )
        }


class TestContainmentClaims:
    def test_each_link_of_a_chain_is_a_part_of_claim(self):
        batch = adapted(
            born("Fläckfeld, Kreis Mockburg, Hesse, Germany")
        ).batch
        assert containment(batch) == {
            (
                "Fläckfeld, Kreis Mockburg, Hesse, Germany",
                "Kreis Mockburg, Hesse, Germany",
            ),
            ("Kreis Mockburg, Hesse, Germany", "Hesse, Germany"),
            ("Hesse, Germany", "Germany"),
        }

    def test_a_containment_carries_no_time_and_cites_its_plac(self):
        batch = adapted(
            "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n"
            "1 BIRT\n2 DATE 1836\n2 PLAC Fläckfeld, Hesse, Germany\n"
        ).batch
        claims = [c for c in batch.claims if c.property == "part_of"]
        assert len(claims) == 2
        for claim in claims:
            # a PLAC states no time (ADR-0038 §2): the birth's date is
            # the event's, never the containment's
            assert claim.qualifiers == []
            assert claim.source.artifact.sha1 == EXPORT.sha1
            assert (
                claim.source.transcription_excerpt.text
                == "2 PLAC Fläckfeld, Hesse, Germany"
            )

    def test_a_plac_s_citation_is_folded_into_no_containment(self):
        batch = adapted(
            "0 @S1@ SOUR\n1 TITL Kirchenbuch Flörburg\n"
            + born("Flörburg, Hesse, Germany")
            + "3 SOUR @S1@\n"
        ).batch
        for claim in batch.claims:
            if claim.property == "part_of":
                assert claim.source.page_number is None
                assert "cited:" not in claim.source.transcription_excerpt.text

    def test_every_event_s_plac_states_its_chain(self):
        # (here) Eugen's occupation in Fläckfeld, his wedding in Kredfeld
        # and his divorce in Moshausen, each a chain of its own
        batch = adapted(
            "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n"
            "1 OCCU Master hatter\n"
            "2 PLAC Fläckfeld, Kreis Mockburg, Hesse, Germany\n"
            "1 FAMS @F1@\n"
            "0 @I2@ INDI\n1 NAME Cäcilie /Tarle/\n1 FAMS @F1@\n"
            "0 @F1@ FAM\n1 HUSB @I1@\n1 WIFE @I2@\n"
            "1 MARR\n2 PLAC Kredfeld, Hesse, Germany\n"
            "1 DIV\n2 PLAC Moshausen, Germany\n"
        ).batch
        assert containment(batch) == {
            (
                "Fläckfeld, Kreis Mockburg, Hesse, Germany",
                "Kreis Mockburg, Hesse, Germany",
            ),
            ("Kreis Mockburg, Hesse, Germany", "Hesse, Germany"),
            ("Hesse, Germany", "Germany"),
            ("Kredfeld, Hesse, Germany", "Hesse, Germany"),
            ("Moshausen, Germany", "Germany"),
        }

    def test_a_plac_of_one_component_states_no_containment(self):
        batch = adapted(born("Flörburg")).batch
        assert containment(batch) == set()
        assert sorted(places(batch)) == ["Flörburg"]

    def test_the_fixture_s_chains_hold_every_link_once(self):
        batch = adapted().batch
        expected = {
            (whole, first)
            for plac in placs()
            for whole, first in zip(
                [plac, *tails(plac)], tails(plac), strict=False
            )
        }
        assert containment(batch) == expected
        claims = [c for c in batch.claims if c.property == "part_of"]
        # one claim of each link a PLAC line states: the same line text,
        # repeated across events, is the one claim
        texts = {
            (
                c.subject.local_id,
                c.value.value.local_id,
                c.source.model_dump_json(),
            )
            for c in claims
        }
        assert len(texts) == len(claims)


COUNTED = "PLAC component without a word"


class TestPlaceholderComponents:
    """A component without a word ('?') is left out and counted wherever
    it stands (ADR-0035 §6): a place named by it would be named by
    nothing."""

    def test_a_jurisdiction_without_a_word_is_left_out_and_counted(self):
        # (here) the export leaves Fläckfeld's district unknown ('?')
        result = adapted(born("Fläckfeld, ?, Hesse, Germany"))
        batch = result.batch
        assert sorted(places(batch)) == [
            "Fläckfeld, Hesse, Germany",
            "Germany",
            "Hesse, Germany",
        ]
        fläckfeld = places(batch)["Fläckfeld, Hesse, Germany"]
        assert fläckfeld.structure.within == ["Hesse", "Germany"]
        assert containment(batch) == {
            ("Fläckfeld, Hesse, Germany", "Hesse, Germany"),
            ("Hesse, Germany", "Germany"),
        }
        assert result.outcomes[COUNTED] == 1

    def test_an_unknown_place_is_placed_in_its_jurisdictions(self):
        # (here) the export knows only that the birth was in Hesse
        result = adapted(born("?, Hesse, Germany"))
        (birth,) = [
            c for c in result.batch.claims if c.property == "place_of_birth"
        ]
        labels = {e.id: e.label for e in result.batch.entities}
        assert labels[birth.value.value.local_id] == "Hesse, Germany"
        assert result.outcomes[COUNTED] == 1

    def test_a_plac_with_no_word_places_nothing_but_states_its_event(self):
        # a PLAC with a value asserts its event (GEDCOM 5.5.1): Eugen's
        # death is stated, with no place
        result = adapted(
            "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n1 DEAT\n2 PLAC ?, -\n"
        )
        batch = result.batch
        assert places(batch) == {}
        assert [e.kind for e in batch.entities if e.kind == "death"] == [
            "death"
        ]
        assert result.outcomes[COUNTED] == 2

    def test_each_plac_is_counted_once(self):
        result = adapted(
            born("Fläckfeld, ?, Hesse, Germany")
            + "1 RESI\n2 PLAC Fläckfeld, ?, Hesse, Germany\n"
        )
        assert result.outcomes[COUNTED] == 2

    def test_named_components_count_nothing(self):
        assert COUNTED not in adapted().outcomes

    def test_a_placeholder_with_a_word_is_kept_as_written(self):
        """A component with a word is kept as written, as the adapter
        cannot tell a name from a word that is none (ADR-0035 §6): the
        export's 'unbekannt' district is a jurisdiction of its own, which
        each PLAC through it lies within, and nothing is counted."""
        result = adapted(
            born("Fläckfeld, unbekannt, Hesse, Germany")
            + "1 RESI\n2 PLAC Kredfeld, unbekannt, Hesse, Germany\n"
        )
        batch = result.batch
        fläckfeld = places(batch)["Fläckfeld, unbekannt, Hesse, Germany"]
        assert fläckfeld.structure.within == ["unbekannt", "Hesse", "Germany"]
        district = places(batch)["unbekannt, Hesse, Germany"]
        assert district.aliases == ["unbekannt"]
        assert containment(batch) == {
            (
                "Fläckfeld, unbekannt, Hesse, Germany",
                "unbekannt, Hesse, Germany",
            ),
            (
                "Kredfeld, unbekannt, Hesse, Germany",
                "unbekannt, Hesse, Germany",
            ),
            ("unbekannt, Hesse, Germany", "Hesse, Germany"),
            ("Hesse, Germany", "Germany"),
        }
        assert COUNTED not in result.outcomes

    def test_empty_components_are_no_placeholders(self):
        # (here) a tool left Flörburg's district and its country empty
        result = adapted(born(", Flörburg, , Hesse"))
        assert sorted(places(result.batch)) == ["Flörburg, Hesse", "Hesse"]
        assert COUNTED not in result.outcomes
        assert COUNTED not in adapted(born(", , ,")).outcomes


def graph_of(batch: ClaimBatch) -> tuple[dict[str, str], list[PlaceRecord]]:
    """Each place of `batch` written as an item of its own, with its label
    and aliases, as the writer leaves it: (local id -> QID, the places)."""
    created: dict[str, str] = {}
    records: list[PlaceRecord] = []
    for number, entity in enumerate(
        (e for e in batch.entities if e.kind == "place"), start=100
    ):
        created[entity.id] = f"Q{number}"
        records.append(
            PlaceRecord(
                qid=f"Q{number}",
                label=entity.label,
                aliases=list(entity.aliases),
                description=f"place {number}",
            )
        )
    return created, records


class TestThePlaceStage:
    def test_every_link_the_export_states_is_claimed_and_settled(self):
        """The place stage finds each container of a PLAC's chain by its
        name among the export's own places, and each link is one a claim
        of the export gives: none is left out unnamed, none is
        record-only, and each is settled without a question."""
        batch = adapted().batch
        created, graph = graph_of(batch)
        links = stated_links(batch, created, EXPORT.sha1)
        assert links
        assert all(link.claimed for link in links)
        assert all(link.qualifiers == () for link in links)
        work = build_worklist(graph, links)
        assert (work.unnamed, work.unresolved) == (0, 0)
        assert work.questions == []
        assert len(work.settled) == len({link.key for link in links})

    def test_a_settled_container_is_the_jurisdiction_s_place(self):
        batch = adapted(born("Brunnenring 35, Flörburg, Hesse, Germany")).batch
        created, graph = graph_of(batch)
        qid = {
            e.label: created[e.id] for e in batch.entities if e.id in created
        }
        work = build_worklist(graph, stated_links(batch, created, EXPORT.sha1))
        address = qid["Brunnenring 35, Flörburg, Hesse, Germany"]
        chain = ("Flörburg", "Hesse", "Germany")
        assert (
            work.settled[(address, chain, 0)]
            == qid["Flörburg, Hesse, Germany"]
        )
        assert work.settled[(address, chain, 1)] == qid["Hesse, Germany"]
        assert work.settled[(address, chain, 2)] == qid["Germany"]

    def test_namesakes_elsewhere_are_asked_of(self):
        """A container whose name another place of the graph shares is a
        question, as for any document: the export's own place is one of
        its candidates, never assumed."""
        batch = adapted(born("Flörburg, Hesse, Germany")).batch
        created, graph = graph_of(batch)
        graph.append(PlaceRecord(qid="Q900", label="Hesse", description="x"))
        work = build_worklist(graph, stated_links(batch, created, EXPORT.sha1))
        (question,) = work.questions
        assert question.container == "Hesse"
        assert sorted(question.candidates) == sorted(
            ["Q900", created[places(batch)["Hesse, Germany"].id]]
        )
