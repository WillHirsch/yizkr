"""Offline tests: the place stage cites each container's evidence on the
`part_of` link it writes (#593; ADR-0038 §8.5). No instance, no LLM.

* A stated link carries its container's evidence, in the document's
  language, with the page of the transcription its first line lies on
  and the number printed on that page, where the transcription has
  pages.
* `write_link` writes the evidence as the excerpt of the link's
  `stated in` block, with its page. A link a claim also gives keeps the
  claim's own reference; a link a record alone states is recorded only
  by its source block whole, and an equal statement citing the artifact
  without the excerpt gains the excerpt's block beside it (rule 3), never
  a second statement.
* A run directory's and a scratch run's documents give each link its
  page from their transcription view.

Fictional (docs/EXAMPLES.md): Rudolf Löbig's 1941 death certificate,
whose second page's place-of-death field puts Koshnovka in Kaldovsky
District, Chadsk Oblast; and the universe's worked example of a dated
containment, "in 1941 Koshnovka belonged to Kaldovsky District".
"""

from __future__ import annotations

import json

import pytest

from tests.unit.test_places import (
    CREATED,
    DEATH,
    IN_1941,
    Runs,
    _batch,
    _entity,
    _item,
    _part_of,
    _written,
    _year,
    cited,
    statements,
)
from yizkr.claims import ArtifactRef
from yizkr.pipeline.places import (
    PRESENT,
    REFERENCED,
    UNDETERMINED,
    WRITTEN,
    StatedLink,
    evidence_page,
    excerpt_language,
    read_transcription,
    record_links,
    stated_links,
    write_link,
)
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.transcription_record import (
    UNCLASSIFIED,
    RecordPage,
    TranscriptionRecord,
)

FIELD = "Koshnovka, Kaldovsky District, Chadsk Oblast"
OTHER = "Koshnovka in the Kaldovsky District"
CLAIMED = "in 1941 Koshnovka belonged to Kaldovsky District"


def certificate(printed: str | None = "12", language="en") -> Transcription:
    """The certificate's two pages: the second's place-of-death field
    breaks its line after the village."""
    record = TranscriptionRecord(
        document_class=UNCLASSIFIED,
        pages=[
            RecordPage(
                page=1,
                status="transcribed",
                text="Certificate of death\nName: Rudolf Löbig",
            ),
            RecordPage(
                page=2,
                printed_page=printed,
                status="transcribed",
                text="Place of death: Koshnovka,\n"
                "Kaldovsky District, Chadsk Oblast\nYear: 1941",
            ),
        ],
    )
    return Transcription.from_record(
        record,
        artifact=ArtifactRef(sha1=DEATH),
        language=language,
        engine="fake-vision",
    )


def death_batch(*claims, evidence=FIELD):
    """Koshnovka, its record placing it in Kaldovsky District, Chadsk
    Oblast, each container with `evidence`."""
    return _batch(
        [
            _entity(
                "kosh",
                "Koshnovka",
                [
                    {"name": name, "evidence": evidence}
                    for name in ("Kaldovsky District", "Chadsk Oblast")
                ],
            ),
            _entity("kald", "Kaldovsky District"),
            _entity("chadsk", "Chadsk Oblast"),
        ],
        claims,
    )


def record_only(evidence=FIELD, page="2", printed="12") -> StatedLink:
    """Koshnovka within Kaldovsky District, as the record alone states it,
    with its evidence on the certificate's second page."""
    (found, _) = stated_links(
        death_batch(evidence=evidence),
        CREATED,
        DEATH,
        certificate(printed=printed) if page else None,
    )
    return found


def dated() -> StatedLink:
    """The same link, which a claim gives in 1941."""
    (found, _) = stated_links(
        death_batch(_part_of(*IN_1941, DEATH, [_year("point_in_time", 1941)])),
        CREATED,
        DEATH,
        certificate(),
    )
    return found


def blocks(statement, key: str) -> list[list]:
    """Each reference block's snaks of the property `key`, by value."""
    from tests.unit.test_containment_claims import VOCABULARY

    found = []
    for reference in statement.references.references:
        found.append(
            [
                snak.datavalue["value"]
                for snak in reference.snaks.get(VOCABULARY[key]) or []
            ]
        )
    return found


def excerpts(statement) -> list:
    return [
        [(value["text"], value["language"]) for value in values]
        for values in blocks(statement, "transcription_excerpt")
    ]


@pytest.fixture
def written():
    """The in-memory instance, its writer, Koshnovka, Kaldovsky District
    and the certificate's artifact item."""
    from tests.unit.test_containment_claims import PlaceWiki
    from yizkr.pipeline.writer import BatchWriter

    wiki = PlaceWiki()
    child, parent = _item(wiki, "Koshnovka"), _item(wiki, "Kaldovsky District")
    artifact = _item(wiki, "1941 death certificate")
    return wiki, BatchWriter(wiki), child, parent, artifact


# -- the links a record states ------------------------------------------------


class TestStatedEvidence:
    def test_each_link_carries_its_container_s_evidence_and_page(self):
        first, second = stated_links(
            death_batch(), CREATED, DEATH, certificate()
        )
        for found in (first, second):
            assert (found.evidence, found.language) == (FIELD, "en")
            assert (found.page, found.printed_page) == ("2", "12")
        assert (first.container, second.container) == (
            "Kaldovsky District",
            "Chadsk Oblast",
        )

    def test_a_page_with_no_printed_number_gives_its_page_alone(self):
        found = record_only(printed=None)
        assert (found.page, found.printed_page) == ("2", None)

    def test_with_no_transcription_the_link_has_no_page(self):
        found = record_only(page=None)
        assert found.evidence == FIELD
        assert (found.page, found.printed_page) == (None, None)

    def test_a_container_with_no_evidence_gives_none(self):
        (found, _) = stated_links(
            _batch(
                [
                    _entity(
                        "kosh",
                        "Koshnovka",
                        ["Kaldovsky District", "Chadsk Oblast"],
                    )
                ]
            ),
            CREATED,
            DEATH,
            certificate(),
        )
        assert (found.evidence, found.language, found.page) == (
            None,
            None,
            None,
        )
        assert found.source() is None

    def test_a_link_round_trips_through_json_with_its_evidence(self):
        found = record_only()
        assert StatedLink.from_json(found.as_json()) == found
        assert StatedLink.from_json(
            json.loads(json.dumps(found.as_json()))
        ) == (found)

    def test_a_stored_link_without_evidence_reads_as_none(self):
        stored = record_only().as_json()
        for key in ("evidence", "language", "page", "printed_page"):
            del stored[key]
        found = StatedLink.from_json(stored)
        assert (found.evidence, found.page) == (None, None)

    def test_its_source_is_a_claim_s_with_the_evidence_as_excerpt(self):
        source = record_only().source()
        assert source.artifact.sha1 == DEATH
        assert (source.page_number, source.printed_page_number) == ("2", "12")
        assert source.transcription_excerpt.text == FIELD
        assert source.transcription_excerpt.language == "en"


class TestEvidencePage:
    def test_the_page_its_first_line_lies_on(self):
        assert evidence_page(certificate(), FIELD) == ("2", "12")
        assert evidence_page(certificate(), "Name: Rudolf Löbig") == (
            "1",
            None,
        )

    def test_evidence_the_transcription_does_not_hold_has_no_page(self):
        assert evidence_page(certificate(), "Kreis Mockburg") == (None, None)

    def test_a_transcription_with_no_pages_gives_none(self):
        text = Transcription(
            artifact=ArtifactRef(sha1=DEATH),
            text=f"Place of death: {FIELD}",
            engine="correction",
        )
        assert evidence_page(text, FIELD) == (None, None)
        assert evidence_page(None, FIELD) == (None, None)

    def test_a_blank_printed_number_is_none(self):
        assert evidence_page(certificate(printed=" "), FIELD) == ("2", None)

    def test_a_document_s_transcription_is_read_once(self, monkeypatch):
        """Each container of a document whose evidence is the same is
        looked for once, in one reading of the transcription."""
        from yizkr.core import verbatim

        made, looked = [], []

        class Counted(verbatim.Transcript):
            def __init__(self, text):
                made.append(text)
                super().__init__(text)

            def locate(self, excerpt):
                looked.append(excerpt)
                return super().locate(excerpt)

        monkeypatch.setattr(verbatim, "Transcript", Counted)
        first, second = stated_links(
            death_batch(), CREATED, DEATH, certificate()
        )
        assert (first.page, second.page) == ("2", "2")
        assert (len(made), looked) == (1, [FIELD])


class TestExcerptLanguage:
    def test_the_transcription_s_language(self):
        assert excerpt_language(death_batch(), certificate(language="ru")) == (
            "ru"
        )

    def test_else_the_language_most_claims_excerpts_give(self):
        def claim(language):
            found = _part_of(*IN_1941, DEATH)
            found["source"]["transcription_excerpt"] = {
                "text": FIELD,
                "language": language,
            }
            return found

        batch = death_batch(claim("de"), claim("ru"), claim("ru"))
        assert excerpt_language(batch) == "ru"
        assert excerpt_language(death_batch(claim("de"))) == "de"

    def test_else_undetermined(self):
        assert excerpt_language(death_batch()) == UNDETERMINED == "und"
        assert record_only(page=None).language == "und"


class TestRecordLinksPages:
    def test_a_run_directory_s_transcription_gives_the_page(self, tmp_path):
        directory = tmp_path / "death"
        _written(
            directory,
            death_batch().model_dump(mode="json")["entities"],
            CREATED,
            DEATH,
        )
        (directory / "transcription.json").write_text(
            certificate().model_dump_json()
        )
        first, _ = record_links(tmp_path)
        assert (first.evidence, first.page, first.printed_page) == (
            FIELD,
            "2",
            "12",
        )

    def test_an_unreadable_transcription_gives_no_page(self, tmp_path):
        directory = tmp_path / "death"
        _written(
            directory,
            death_batch().model_dump(mode="json")["entities"],
            CREATED,
            DEATH,
        )
        (directory / "transcription.json").write_text("{}")
        first, _ = record_links(tmp_path)
        assert (first.evidence, first.page) == (FIELD, None)
        assert read_transcription(directory / "transcription.json") is None
        assert read_transcription(directory / "absent.json") is None


# -- the write ----------------------------------------------------------------


class TestWriteEvidence:
    def test_a_record_s_link_cites_its_evidence_and_page(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (WRITTEN)
        (statement,) = statements(wiki, child)
        assert cited(statement, "stated_in") == [[artifact], []]
        assert excerpts(statement) == [[(FIELD, "en")], []]
        assert blocks(statement, "page_number") == [["2"], []]
        assert blocks(statement, "printed_page_number") == [["12"], []]
        assert cited(statement, "inferred_from_extraction_run") == [
            [],
            [runs.qid],
        ]

    def test_a_link_with_no_page_cites_its_evidence_alone(self, written):
        wiki, writer, child, parent, artifact = written
        write_link(
            writer, child, parent, record_only(page=None), artifact, Runs(wiki)
        )
        (statement,) = statements(wiki, child)
        assert excerpts(statement) == [[(FIELD, "und")], []]
        assert blocks(statement, "page_number") == [[], []]

    def test_a_claim_s_link_cites_its_evidence_and_its_time(self, written):
        wiki, writer, child, parent, artifact = written
        write_link(writer, child, parent, dated(), artifact, Runs(wiki))
        (statement,) = statements(wiki, child)
        assert excerpts(statement) == [[(FIELD, "en")], []]
        assert blocks(statement, "page_number") == [["2"], []]

    def test_a_link_written_already_is_present(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        write_link(writer, child, parent, record_only(), artifact, runs)
        writes = wiki.wbi.item.writes
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (PRESENT)
        assert wiki.wbi.item.writes == writes and runs.calls == 1

    def test_an_earlier_block_without_the_excerpt_gains_one_beside_it(
        self, written
    ):
        """An earlier pass wrote the link citing the certificate with no
        excerpt: the statement stays one, its block stays as it was, and
        the excerpt's block is added beside it (rule 3)."""
        wiki, writer, child, parent, artifact = written
        earlier = Runs(wiki, "place-hierarchy run fake-model earlier")
        runs = Runs(wiki)
        bare = StatedLink(
            sha1=DEATH, place=child, chain=("Kaldovsky District",), index=0
        )
        write_link(writer, child, parent, bare, artifact, earlier)
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (REFERENCED)
        (statement,) = statements(wiki, child)
        assert [found for found in cited(statement, "stated_in") if found] == [
            [artifact],
            [artifact],
        ]
        assert [found for found in excerpts(statement)] == [
            [],
            [],
            [(FIELD, "en")],
            [],
        ]
        assert [
            found
            for found in cited(statement, "inferred_from_extraction_run")
            if found
        ] == [[earlier.qid], [runs.qid]]
        # and again: nothing more
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (PRESENT)
        assert len(statements(wiki, child)) == 1

    def test_the_same_run_s_block_is_not_added_twice(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        bare = StatedLink(
            sha1=DEATH, place=child, chain=("Kaldovsky District",), index=0
        )
        write_link(writer, child, parent, bare, artifact, runs)
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (REFERENCED)
        (statement,) = statements(wiki, child)
        assert len(statement.references.references) == 3

    def test_another_excerpt_of_the_same_artifact_adds_its_block(
        self, written
    ):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        write_link(writer, child, parent, record_only(), artifact, runs)
        other = record_only(evidence=OTHER, page=None)
        assert write_link(writer, child, parent, other, artifact, runs) == (
            REFERENCED
        )
        (statement,) = statements(wiki, child)
        assert [found for found in excerpts(statement) if found] == [
            [(FIELD, "en")],
            [(OTHER, "und")],
        ]

    def test_a_claim_s_link_keeps_the_claim_s_own_reference(self, written):
        """The claim's statement cites the certificate under the claim's
        own excerpt, the worked example's words (a block of the shape the
        writer gives a claim, `stated in` with its page and excerpt): the
        claimed link, whose container's evidence is other words, is
        recorded by it, and nothing is added."""
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        claim_s = StatedLink(
            sha1=DEATH,
            place=child,
            chain=("Kaldovsky District",),
            index=0,
            qualifiers=dated().qualifiers,
            claimed=True,
            evidence=CLAIMED,
            language="en",
            page="2",
        )
        write_link(writer, child, parent, claim_s, artifact, runs)
        writes = wiki.wbi.item.writes
        assert dated().evidence == FIELD != CLAIMED
        assert write_link(writer, child, parent, dated(), artifact, runs) == (
            PRESENT
        )
        assert wiki.wbi.item.writes == writes
        (statement,) = statements(wiki, child)
        assert excerpts(statement) == [[(CLAIMED, "en")], []]

    def test_a_link_with_no_evidence_is_present_by_its_artifact(self, written):
        wiki, writer, child, parent, artifact = written
        runs = Runs(wiki)
        write_link(writer, child, parent, record_only(), artifact, runs)
        bare = StatedLink(
            sha1=DEATH, place=child, chain=("Kaldovsky District",), index=0
        )
        assert write_link(writer, child, parent, bare, artifact, runs) == (
            PRESENT
        )

    def test_another_artifact_s_statement_gains_the_excerpt_block(
        self, written
    ):
        wiki, writer, child, parent, artifact = written
        other = _item(wiki, "1984 family tree")
        runs = Runs(wiki)
        bare = StatedLink(
            sha1=DEATH, place=child, chain=("Kaldovsky District",), index=0
        )
        write_link(writer, child, parent, bare, other, runs)
        assert write_link(
            writer, child, parent, record_only(), artifact, runs
        ) == (REFERENCED)
        (statement,) = statements(wiki, child)
        assert [found for found in cited(statement, "stated_in") if found] == [
            [other],
            [artifact],
        ]
        assert [found for found in excerpts(statement) if found] == [
            [(FIELD, "en")]
        ]


@pytest.mark.parametrize("printed", ["12", None])
def test_the_source_block_is_the_writer_s(written, printed):
    """The block is the writer's own source block for the link's source,
    so it is compared as a claim's is."""
    wiki, writer, child, parent, artifact = written
    found = record_only(printed=printed)
    write_link(writer, child, parent, found, artifact, Runs(wiki))
    (statement,) = statements(wiki, child)
    expected = writer._source_block(artifact, found.source())
    assert writer._reference_key(
        statement.references.references[0]
    ) == writer._reference_key(expected)


# -- the scratch run's place stage --------------------------------------------


@pytest.mark.write_side
def test_the_scratch_stage_cites_the_evidence_on_its_page(
    tmp_path, monkeypatch
):
    """Gustav's letter puts Fläckfeld in Kreis Peiltal on its second
    page; its record alone states the link, so the stage writes it,
    citing the letter's words, on that page."""
    from tests.unit.test_scratch_places import (
        EXTRACT,
        LEOPOLD,
        LETTER,
        RUN,
        PlaceRun,
        PlaceWiki,
        born_at,
        extract_batch,
        place,
    )
    from yizkr.claims import ClaimBatch
    from yizkr.core import verbatim
    from yizkr.pipeline.scratchwrite import read_record as stage_record

    words = "born in Fläckfeld, Kreis Peiltal"
    letter = ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                LEOPOLD,
                {
                    "id": "flackfeld",
                    "label": "Fläckfeld",
                    "kind": "place",
                    "structure": {
                        "kind": "place",
                        "within": [
                            {"name": "Kreis Peiltal", "evidence": words}
                        ],
                    },
                },
            ],
            "claims": [born_at(LETTER, place("flackfeld", "Fläckfeld"))],
        }
    )
    run = PlaceRun(
        tmp_path,
        monkeypatch,
        documents=(("leopold-birth-extract.png", EXTRACT, extract_batch()),),
    )
    doc = run.add("gustav-letter-1952.png", LETTER, letter)
    record = TranscriptionRecord(
        document_class=UNCLASSIFIED,
        pages=[
            RecordPage(page=1, status="transcribed", text="Dear Leopold,"),
            RecordPage(
                page=2,
                printed_page="3",
                status="transcribed",
                text=f"you were {words}.",
            ),
        ],
    )
    transcription = Transcription.from_record(
        record,
        artifact=ArtifactRef(sha1=doc.sha1),
        language="de",
        engine="fake-vision",
    )
    assert verbatim.Transcript(transcription.text).locate(words)
    doc.transcription.write_text(transcription.model_dump_json())
    run.past_the_barrier()

    flackfeld = run.qid("Fläckfeld")
    (statement,) = run.wiki.entities[flackfeld]["claims"][
        PlaceWiki.PROPERTIES["part_of"]
    ]
    stated, _ = statement["references"]
    snaks = stated["snaks"]
    assert [
        snak["datavalue"]["value"]
        for snak in snaks[PlaceWiki.PROPERTIES["transcription_excerpt"]]
    ] == [{"text": words, "language": "de"}]
    assert [
        snak["datavalue"]["value"]
        for snak in snaks[PlaceWiki.PROPERTIES["page_number"]]
    ] == ["2"]
    assert [
        snak["datavalue"]["value"]
        for snak in snaks[PlaceWiki.PROPERTIES["printed_page_number"]]
    ] == ["3"]
    # the stage's record keeps the link's evidence and page
    planned = stage_record(run.context("places").directory)["planned"]
    assert [
        (entry["link"]["evidence"], entry["link"]["page"])
        for entry in planned
        if entry["child"] == flackfeld
    ] == [(words, "2")]
