"""Offline tests for what the extraction prompts hand over (#407; ADR-0035,
ADR-0031, ADR-0036): the entity manifest's mention records and judgement
notes, the claims' roles, kinship classes, markers, civil status and
unions, and `issued_by`, whose subject is the document itself.

What the model gives that fails validation is counted, never repaired: a
mention record or an entity's note that cannot stand is set aside and
counted in coverage.json, and a claim that cannot stand is a counted
reject. Every example comes from the fictional universe (docs/EXAMPLES.md),
its Russian and Frisian sources included.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.unit.test_chunking import (
    DOC,
    DOC_REF,
    LINE_LENGTH,
    ScriptedLlm,
    claim,
    ndjson,
    side_output,
    transcription,
)
from tests.unit.test_pipeline import ITEMS, PROPERTIES, RUN, FakeWiki
from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    validate_against_manifest,
)
from yizkr.pipeline import critique
from yizkr.pipeline.chunking import (
    CHUNK_PROMPT,
    ENTITY_ASSIGNMENT_BLOCK,
    ENTITY_MANIFEST_PROMPT,
    LINE_ASSIGNMENT_BLOCK,
    ChunkedClaimExtractor,
)
from yizkr.pipeline.extract import (
    EXTRACTION_PROMPT,
    run_metadata,
    salvage_valid_claims,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.writer import BatchWriter

PROMPTS = {
    "manifest": ENTITY_MANIFEST_PROMPT,
    "chunk": CHUNK_PROMPT,
    "lines": LINE_ASSIGNMENT_BLOCK,
    "entities": ENTITY_ASSIGNMENT_BLOCK,
    "single": EXTRACTION_PROMPT,
}
OTHER_REF = ArtifactRef.from_bytes(b"another document")


def flat(text: str) -> str:
    return " ".join(text.split())


def run() -> dict:
    return run_metadata("recorded").model_dump(mode="json")


# -- the prompts --------------------------------------------------------------


class TestTheManifestPrompt:
    """The entity-manifest prompt asks for a record per entity, with its
    label status (ADR-0035 §2, §5)."""

    prompt = flat(
        ENTITY_MANIFEST_PROMPT.format(
            kinds="",
            kind_descriptions="",
            artifact_sha1=DOC_REF.sha1,
            language="de",
            numbered_text="",
        )
    )

    @pytest.mark.parametrize(
        "shape",
        [
            '{"kind": "human", "status": "<status>",',
            '"surnames": [{"written": "...", "type": "<birth, married or '
            'other>"}]',
            '"patronymics": [...], "nicknames": [...], "titles": [...], '
            '"suffixes": [...], "unsplit": [...]',
            '{"kind": "place", "status": "<toponym, deictic or generic>",',
            '"names": [{"written": "...", "toponym": "...", "head": "..."}]',
            '"address": {"written": "...", "street": "...", "number": "..."}',
            '{"kind": "organization", "names": [...], "kinds": [...], '
            '"places": [...]}',
            '{"kind": "family", "surnames": [...]}',
            '"structure": {<the mention record, for the four kinds above>}',
            '"note": {<only for a judgement call>}',
        ],
    )
    def test_it_asks_for_each_record_field(self, shape):
        assert shape in self.prompt

    def test_it_names_every_status_the_record_allows(self):
        for status in (
            "`name`",
            "`role`",
            "`kinship`",
            "`courtesy`",
            "`placeholder`",
            "`description`",
            "`toponym`",
            "`deictic`",
            "`generic`",
        ):
            assert status in self.prompt, status

    def test_the_record_divides_and_never_adds(self):
        assert (
            "Divide and label the words; never add one that is in neither "
            "the label nor an alias (a given name's standard form aside)"
        ) in self.prompt

    def test_the_document_decides_which_part_is_which(self):
        # read by the document's conventions, never by a rule about a
        # language; examples from the Soviet certificate and the card
        assert "never by a rule about a language" in self.prompt
        assert "Лёбиг Рудольф Феликсович" in self.prompt
        assert "the patronymic Феликсович" in self.prompt
        assert '"LÖBIG, Ida, née SÖRNE"' in self.prompt
        assert "put the form in `unsplit`" in self.prompt

    def test_its_examples_include_the_russian_and_frisian_sources(self):
        for example in (
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
            "It tichelwurk fan Snylsjum",
            "Калдовское районное бюро ЗАГС",
            "по мужу Лёбиг",
            "S. van 't Nijks",
        ):
            assert example in self.prompt, example

    def test_the_relation_decides_the_kind(self):
        # ADR-0031 replaces the rule that an institution named only as
        # where something happened is an organization
        assert "The relation decides the kind (ADR-0031)." in self.prompt
        assert "named only as WHERE something happened is a place" in (
            self.prompt
        )
        assert (
            "give TWO entities, a place for the site and an organization "
            "for the body"
        ) in self.prompt
        assert "an organization HAS a location" not in self.prompt
        assert "is an `organization` even when" not in self.prompt

    def test_office_holders_and_religion(self):
        assert (
            "a person who holds an office (a consul, a director, a rabbi, a "
            "mayor, a registrar) is a `human`"
        ) in self.prompt
        assert (
            "a religion, a denomination or an ethnicity is never an "
            "`occupation_class`"
        ) in self.prompt

    def test_deictic_and_generic_references_are_no_alias(self):
        assert (
            'for a place, a deictic or locative reference back to it ("dort", '
            '"hier", "daselbst", "ebenda", "there")'
        ) in self.prompt
        assert "a place's alias is a toponym" in self.prompt
        assert "an organization's alias is a name" in self.prompt

    def test_an_unnamed_person_takes_the_documents_words(self):
        assert (
            'the postcard\'s writer signs "Dein Vater", so that is his label'
        ) in self.prompt

    def test_type_hints_in_english(self):
        assert "in English whatever the document's language" in self.prompt

    def test_the_issuing_authority_and_an_officiant_are_extracted(self):
        # ADR-0035 §3.2, §3.4: the apparatus rule gives way where the
        # source shows them acting
        assert "the authority that issued the document" in self.prompt
        assert "Whom the source shows acting is the exception" in self.prompt

    def test_marriages_are_entities(self):
        assert "a marriage is an entity, not an event (ADR-0036)" in (
            self.prompt
        )


class TestTheChunkPrompt:
    prompt = flat(CHUNK_PROMPT)

    def test_no_prompt_names_the_retired_spouse_property(self):
        """`spouse` is retired (ADR-0036): the prompts name it only as a
        participant's role, never as a property to claim."""
        for name, text in PROMPTS.items():
            said = flat(text)
            for found in re.finditer(r"`spouse`", said):
                before = said[max(0, found.start() - 40) : found.start()]
                assert "role" in before, (name, before)
            assert "spouse claim" not in said, name
            assert "on the `spouse`" not in said, name
        properties, _items = vocabulary_prompt_lines()
        assert not re.search(r"^\s*spouse:", properties, re.M)

    def test_participant_roles(self):
        for role in (
            "`principal`",
            "`parent`",
            "`informant`",
            "`witness`",
            "`officiant`",
        ):
            assert role in self.prompt, role
        assert "A death's principal is never its informant." in self.prompt
        assert "is `parent` and `informant`" in self.prompt

    def test_kinship_classes_beside_the_term(self):
        assert "`kinship_class`: its class, one of the kin_ items" in (
            self.prompt
        )
        assert (
            '"дядя по отцу" (uncle on the father\'s side) is '
            "kin_aunt_or_uncle on the paternal side"
        ) in self.prompt
        assert "kin_cousin of degree 1" in self.prompt
        assert '"oerbeppe" (great-grandmother) is kin_grandparent' in (
            self.prompt
        )
        assert "ONLY where the term itself states them, never guessed" in (
            self.prompt
        )

    def test_markers_are_claims_never_name_parts(self):
        assert (
            "A word that states a fact rather than naming someone is a "
            "claim, never part of a name"
        ) in self.prompt
        for marker in ('"вдова"', '"widdo"', '"покойный"', '"sillige"'):
            assert marker in self.prompt, marker
        assert "`civil_status` `widowed`" in self.prompt
        assert "It names no partner and makes no marriage." in self.prompt

    def test_marriages_are_unions_bounded_by_events(self):
        assert (
            "Marriages and partnerships are entities bounded by events "
            "(ADR-0036)"
        ) in self.prompt
        assert "the union is `started_by` it" in self.prompt
        assert "the union is `ended_by` a `divorce` or an `annulment`" in (
            self.prompt
        )
        assert "The couple take part in the union, not in its wedding" in (
            self.prompt
        )

    def test_issued_by_names_the_document_as_its_subject(self):
        rendered = flat(
            CHUNK_PROMPT.format(
                artifact_sha1=DOC_REF.sha1,
                property_keys="",
                item_keys="",
                assignment_block="",
                schema="",
                document_block="",
            )
        )
        assert (
            "Its subject is the document, given exactly as the source's "
            f'artifact: {{"kind": "artifact", "sha1": "{DOC_REF.sha1}"}}'
        ) in rendered
        properties, _items = vocabulary_prompt_lines()
        assert re.search(r"^\s*issued_by:", properties, re.M)

    def test_judgement_notes_only_where_a_reader_could_differ(self):
        assert (
            "ONLY where you made a judgement call that a careful reader "
            "could make differently"
        ) in self.prompt
        assert "Leave it out for a plain reading." in self.prompt

    def test_the_relation_decides_the_kind(self):
        assert (
            "a relation of agency (issued_by, carried_out_by, employer, "
            "educated_at, member_of) takes an organization"
        ) in self.prompt
        assert "an agent, never a place" not in flat(LINE_ASSIGNMENT_BLOCK)

    def test_the_issuer_chunk_emits_issued_by(self):
        assert (
            "belongs to the chunk assigned the organization that issued it"
        ) in flat(ENTITY_ASSIGNMENT_BLOCK)
        assert "the `issued_by` claim of an issuer the unit names" in flat(
            LINE_ASSIGNMENT_BLOCK
        )


# -- the manifest: records and notes set aside, and counted ------------------


def manifest(*entities: dict) -> str:
    return json.dumps({"entities": list(entities)})


HILDEGARD = {
    "id": "e1",
    "label": "Hildegard Löbig",
    "aliases": ["Hildegard"],
    "kind": "human",
    "summary": "b. 1876 Fläckfeld",
    "mention_lines": [2],
}
SEBASTIAN = {
    "id": "e2",
    "label": "Sebastian Löbig",
    "aliases": [],
    "kind": "human",
    "summary": "b. 1862 Flörburg",
    "mention_lines": [3],
}
FLOERBURG = {
    "id": "e3",
    "label": "Flörburg",
    "kind": "place",
    "mention_lines": [3],
    "extract": False,
}


def name_record(given: str, surname: str) -> dict:
    return {
        "kind": "human",
        "status": "name",
        "given": [{"written": given, "standard": given}],
        "surnames": [{"written": surname}],
    }


def extract(tmp_path, *entities: dict):
    """A chunked extraction of the two-person document from a manifest of
    `entities`, one chunk a person; returns the batch and coverage.json."""
    llm = ScriptedLlm(
        [
            manifest(*entities),
            ndjson([claim("e1", "Hildegard Löbig", 1876)]),
            ndjson([claim("e2", "Sebastian Löbig", 1862)]),
        ]
    )
    batch = ChunkedClaimExtractor(
        llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
    ).extract(transcription(), DOC_REF)
    return batch, side_output(tmp_path, "coverage.json")


class TestTheManifestsRecords:
    def test_a_record_that_stands_is_kept_and_counted(self, tmp_path):
        batch, coverage = extract(
            tmp_path,
            HILDEGARD | {"structure": name_record("Hildegard", "Löbig")},
            SEBASTIAN | {"structure": name_record("Sebastian", "Löbig")},
            FLOERBURG
            | {
                "structure": {
                    "kind": "place",
                    "names": [{"written": "Flörburg", "toponym": "Flörburg"}],
                }
            },
        )
        records = {e.id: e.structure for e in batch.entities}
        assert records["e1"].surnames[0].written == "Löbig"
        assert records["e3"].names[0].toponym == "Flörburg"
        assert coverage["records"] == {
            "given": 3,
            "recordless": 0,
            "by_status": {"name": 2, "toponym": 1},
            "set_aside": {},
        }

    def test_a_record_that_adds_a_word_is_set_aside_not_the_entity(
        self, tmp_path
    ):
        """Gustave never had the surname Sörne: a record that gives a part
        no form of the entity holds is set aside, and the entity keeps its
        label, aliases and kind (ADR-0035 §5)."""
        batch, coverage = extract(
            tmp_path,
            HILDEGARD | {"structure": name_record("Hildegard", "Sörne")},
            SEBASTIAN | {"structure": {"kind": "human", "given": "Sebastian"}},
            FLOERBURG | {"structure": {"kind": "human", "status": "name"}},
        )
        by_id = {e.id: e for e in batch.entities}
        assert {e.structure for e in batch.entities} == {None}
        assert (by_id["e1"].label, by_id["e1"].kind) == (
            "Hildegard Löbig",
            "human",
        )
        assert by_id["e3"].kind == "place"
        assert coverage["records"] == {
            "given": 0,
            "recordless": 3,
            "by_status": {},
            "set_aside": {"addition": 1, "kind": 1, "schema": 1},
        }
        # the claims are extracted as ever
        assert len(batch.claims) == 2
        # and the counts quote no name
        assert "Sörne" not in json.dumps(coverage)

    def test_an_entity_without_a_record_is_recordless(self, tmp_path):
        _batch, coverage = extract(tmp_path, HILDEGARD, SEBASTIAN)
        assert coverage["records"]["recordless"] == 2
        assert coverage["records"]["set_aside"] == {}

    def test_an_entitys_note_that_cannot_stand_is_set_aside(self, tmp_path):
        batch, coverage = extract(
            tmp_path,
            HILDEGARD
            | {"note": {"category": "guess", "text": "read as Hildegard"}},
            SEBASTIAN
            | {
                "note": {
                    "category": "identity",
                    "text": "the register's Sebastian read as the hatter",
                }
            },
        )
        notes = {e.id: e.note for e in batch.entities}
        assert notes["e1"] is None
        assert notes["e2"].category == "identity"
        assert coverage["records"]["set_aside"] == {"note": 1}
        assert coverage["notes"] == {"identity": 1}

    def test_a_manifest_with_bad_records_is_never_refused(self):
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            manifest(
                HILDEGARD | {"structure": "a person"},
                SEBASTIAN | {"note": "no call"},
            ),
            4,
        )
        assert problems == []
        assert [e.id for e in parsed.entities] == ["e1", "e2"]
        assert parsed.set_aside == {"note": 1, "schema": 1}

    def test_no_capitalised_token_check(self, tmp_path):
        """Capitals are no sign of a name in every language, so coverage
        no longer lists lines whose capitalised words match no entity."""
        assert not hasattr(ChunkedClaimExtractor, "_entity_coverage")
        _batch, coverage = extract(tmp_path, HILDEGARD, SEBASTIAN)
        # the logistics line's "Dinner" names no entity, and nothing says so
        assert "Dinner" in DOC
        assert set(coverage) == {
            "strategy",
            "entities_total",
            "entities_not_extracted",
            "entities_without_claims",
            "distinct_subject_ids",
            "failed_chunks",
            "records",
            "notes",
            "chunks",
            "claims",
            "rejected_claims",
            "unread_objects",
            "fallback",
        }


# -- claims: notes and the document as subject --------------------------------


def cited(**fields) -> dict:
    return {
        "source": {
            "artifact": DOC_REF.model_dump(),
            "transcription_excerpt": {"text": "b. 1876", "language": "en"},
        },
        **fields,
    }


def item(local_id: str, label: str, kind: str) -> dict:
    return {
        "type": "item",
        "value": {
            "kind": "local",
            "local_id": local_id,
            "label": label,
            "instance_of": kind,
        },
    }


STANDESAMT = {
    "id": "e9",
    "label": "Standesamt Flörburg",
    "kind": "organization",
    "structure": {
        "kind": "organization",
        "kinds": ["Standesamt"],
        "places": ["Flörburg"],
    },
}


def issued_by(subject: dict) -> dict:
    return cited(
        subject=subject,
        property="issued_by",
        value=item("e9", "Standesamt Flörburg", "organization"),
    )


class TestClaimNotes:
    def test_a_claim_may_carry_a_note(self):
        noted = claim("e1", "Hildegard Löbig", 1876) | {
            "note": {
                "category": "date_reading",
                "text": "the postmark '19.8.38' read as 1938",
            }
        }
        batch, rejects = salvage_valid_claims([noted], run())
        assert rejects == []
        (kept,) = batch.claims
        assert kept.note.category == "date_reading"

    @pytest.mark.parametrize(
        "note",
        [
            {"category": "guess", "text": "read as 1938"},
            {"category": "date_reading", "text": ""},
            {"category": "date_reading"},
            "read as 1938",
        ],
    )
    def test_a_note_that_cannot_stand_is_a_counted_reject(self, note):
        noted = claim("e1", "Hildegard Löbig", 1876) | {"note": note}
        batch, rejects = salvage_valid_claims([noted], run())
        assert batch.claims == []
        assert [r["claim"]["note"] for r in rejects] == [note]

    def test_a_claim_without_a_note_serialises_as_before(self):
        plain = Claim.model_validate(claim("e1", "Hildegard Löbig", 1876))
        assert "note" not in plain.model_dump()
        record = EntityRecord.model_validate(HILDEGARD)
        assert "note" not in record.model_dump()

    def test_the_prompt_schema_carries_the_note(self):
        schema = ClaimBatch.model_json_schema()
        note = schema["$defs"]["JudgementNote"]
        assert note["properties"]["category"]["enum"] == [
            "kind",
            "relation",
            "date_reading",
            "identity",
            "other",
        ]


class TestTheDocumentAsSubject:
    def test_issued_by_takes_its_own_document(self):
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": run(),
                "claims": [issued_by(DOC_REF.model_dump())],
                "entities": [STANDESAMT],
            }
        )
        assert validate_against_manifest(batch) == []

    def test_another_document_is_refused(self):
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": run(),
                "claims": [issued_by(OTHER_REF.model_dump())],
                "entities": [STANDESAMT],
            }
        )
        (problem,) = validate_against_manifest(batch)
        assert "a document subject names the claim's own source" in problem

    def test_no_other_property_takes_the_document(self):
        dated = cited(
            subject=DOC_REF.model_dump(),
            property="point_in_time",
            value={"type": "time", "year": 1938},
        )
        batch = ClaimBatch.model_validate(
            {"extraction_run": run(), "claims": [dated]}
        )
        (problem,) = validate_against_manifest(batch)
        assert "does not take the source document as its subject" in problem

    def test_the_extractor_never_mends_a_wrong_document_subject(
        self, tmp_path
    ):
        """The extractor sets every claim's source to the document it read
        (cite_artifact), and leaves the subject as the model wrote it: a
        subject that names another document is refused and counted."""
        llm = ScriptedLlm(
            [
                manifest(HILDEGARD, SEBASTIAN, STANDESAMT),
                ndjson(
                    [
                        claim("e1", "Hildegard Löbig", 1876),
                        issued_by(OTHER_REF.model_dump()),
                        issued_by(DOC_REF.model_dump()),
                    ]
                ),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
                ndjson([]),
            ]
        )
        batch = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        ).extract(transcription(), DOC_REF)
        issued = [c for c in batch.claims if c.property == "issued_by"]
        assert [c.subject for c in issued] == [DOC_REF]
        (reject,) = side_output(tmp_path, "rejects.json")
        assert reject["claim"]["subject"]["sha1"] == OTHER_REF.sha1
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["rejected_claims"] == 1


class IssuingWiki(FakeWiki):
    """The unit fake, with `issued_by` in its vocabulary."""

    def vocabulary(self):
        return dict(PROPERTIES) | {"issued_by": "P90"}, dict(ITEMS)


class TestTheWriter:
    def test_a_document_subject_is_written_on_its_artifact_item(self):
        """The writer resolves the document subject to the artifact item
        its media names, as it resolves the claim's source (writer step
        0), and writes the statement there, with both references."""
        wiki = IssuingWiki()
        artifact = wiki.wbi.item.new()
        artifact.write()
        wiki.media[DOC_REF.sha1] = artifact.id
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [issued_by(DOC_REF.model_dump())],
                "entities": [STANDESAMT],
            }
        )
        result = BatchWriter(wiki).write_batch(batch)
        assert result.written == 1
        (statement,) = wiki.wbi.item.get(artifact.id).claims.get("P90")
        office = statement.mainsnak.datavalue["value"]["id"]
        assert office == result.created_items["e9"]
        blocks = statement.references.references

        def referenced(key: str) -> list[str]:
            return [
                snak.datavalue["value"]["id"]
                for block in blocks
                for snak in block.snaks.get(PROPERTIES[key]) or []
            ]

        assert referenced("stated_in") == [artifact.id]
        assert referenced("inferred_from_extraction_run") == [result.run_qid]
        # the artifact is no entity of the batch: no kind is recorded on it
        assert not wiki.wbi.item.get(artifact.id).claims.get(
            PROPERTIES["instance_of"]
        )


class TestTheCritic:
    def test_it_lists_a_document_subject_by_its_content_key(self):
        """The critic lists each claim by its subject's id; a document
        subject has none, so it is listed by its content key, as the
        prompts name a document."""
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": run(),
                "claims": [issued_by(DOC_REF.model_dump())],
                "entities": [STANDESAMT],
            }
        )
        (listing,) = critique.listings(batch)
        assert listing == (f"sha1:{DOC_REF.sha1}", "issued_by", "e9")

    def test_its_agency_relations_include_issued_by(self):
        mismatch = critique.CATEGORIES["relation_kind_mismatch"]
        assert "issued by, carried out by" in mismatch
