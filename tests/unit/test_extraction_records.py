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

from tests.fixtures.identity import write_mapped
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
    document_block,
    run_metadata,
    salvage_valid_claims,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.review import claim_id
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
            '"patronymics": ["..."], "nicknames": ["..."], "suffixes": '
            '["..."], "unsplit": ["..."], "titles": ["..."]}.',
            '{"kind": "place", "status": "<toponym, deictic, generic or '
            'adjective>",',
            '"names": [{"written": "...", "toponym": "...", "head": "..."}]',
            '"address": {"written": "...", "street": "...", "number": "..."}',
            '{"kind": "organization", "names": ["..."], "kinds": ["..."], '
            '"places": ["..."]}',
            '{"kind": "family", "surnames": ["..."]}',
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
            "for the body, linked by the organization's `location` (the "
            "site)"
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

    def test_an_entity_note_is_a_kind_or_identity_call(self):
        # ADR-0035 §4: an entity's note is a kind or an identity call; the
        # other categories are a claim's
        assert (
            'an entity may carry "note": {"category": "<kind or identity>", '
            '"text": "<one sentence in English>"}'
        ) in self.prompt

    def test_an_organizations_record_examples(self):
        assert (
            '"Standesamt Flörburg" has no proper name, the kind Standesamt '
            "and the place Flörburg"
        ) in self.prompt
        assert (
            "A generic word any organization carries (a word for a firm, a "
            "legal form) is neither a name nor a kind."
        ) in self.prompt
        assert 'a named family or household ("the Löbigs")' in self.prompt


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
            "kin_aunt_or_uncle of degree 0 on the paternal side"
        ) in self.prompt
        assert (
            'Apart from a cousin, a kinship term with no "great-" has degree 0'
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
        assert (
            "or by an `event` where the source does not say how the union "
            "ended"
        ) in self.prompt

    def test_sex_only_where_the_source_states_it(self):
        assert (
            "Record `sex_or_gender` (male/female vocab) only where the "
            'source states it, in words or in its grammar ("den Schüler"), '
            "never from a given name."
        ) in self.prompt

    def test_issued_by_names_the_document_as_its_subject(self):
        rendered = flat(
            CHUNK_PROMPT.format(
                artifact_sha1=DOC_REF.sha1,
                property_keys="",
                item_keys="",
                assignment_block="",
                schema="",
                document_block="",
                document_paragraphs=document_block(transcription(), DOC_REF),
            )
        )
        assert (
            "Their subject is THIS document, given exactly as the source's "
            f'artifact: {{"kind": "artifact", "sha1": "{DOC_REF.sha1}"}}; '
        ) in rendered
        assert "and never anything else" in rendered
        assert "`issued_by`: the organization that issued it" in rendered
        properties, _items = vocabulary_prompt_lines()
        assert re.search(r"^\s*issued_by:", properties, re.M)

    def test_the_single_call_prompt_gives_issued_by_its_subject(self):
        rendered = flat(
            EXTRACTION_PROMPT.format(
                artifact_sha1=DOC_REF.sha1,
                kinds="",
                property_keys="",
                item_keys="",
                schema="",
                language="de",
                text="",
                document_block=document_block(transcription(), DOC_REF),
            )
        )
        assert (
            "What a document is: these properties state who made a document"
        ) in rendered
        assert (
            "Their subject is THIS document, given exactly as the source's "
            f'artifact: {{"kind": "artifact", "sha1": "{DOC_REF.sha1}"}}; '
        ) in rendered

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

    def test_one_chunk_emits_issued_by(self):
        # every claim about the document itself has one owner chunk (#456)
        assert (
            "Every claim whose subject is the document itself or an image it "
            "places (`issued_by`,"
        ) in flat(ENTITY_ASSIGNMENT_BLOCK)
        assert "the `issued_by` claim of an issuer the unit names" in flat(
            LINE_ASSIGNMENT_BLOCK
        )


# -- appellations, nicknames and the record (ADR-0018) -----------------------

# One rule, in the same words in every prompt that routes a name (#434,
# #499): every form a source uses to name or address a person is an
# alias, and one that is no plain name form is also `referred_to_as`.
ALIAS_RULE = (
    "Every form a source uses to name or address a person is an alias, "
    "with no distinction between them: a name form, a kinship term with a "
    'name ("Tante Rahel"), a kinship term alone ("Omama"), a pet name '
    '("Pünktchen", "Bubele") and a courtesy name ("Nana Maisie").'
)
APPELLATION_RULE = (
    "A form that is no plain name form (a name, or a spelling, a "
    'shortening, a diminutive or an initial of one, such as "Gustl" for '
    "Gustav) is an appellation: it is also one `referred_to_as` claim, the "
    "form verbatim with its excerpt, with a `perspective` qualifier where "
    "the source gives the ego as an entity."
)
DESCRIPTION_RULE = (
    'A description that is no form of address is never an alias: "the '
    'deceased", "the informant", a role or form-field label, "my husband", '
    '"our great grandmother".'
)
NICKNAME_RULE = (
    "A name the source gives as what the person is called, by anyone "
    '("Peppi", "Gustl", "Pünktchen"), is also a record nickname, and a '
    "`nickname` claim only where the source marks the type. A form one "
    "person calls another, or that names the person through a relation "
    '("Bubele", "Nana Maisie", "Рудольфиха", "Tante Rahel", "Omama"), is '
    "never a record nickname."
)
# "is their name form (label or alias)" keeps the rule apart from the
# `name` property (#435)
GREETING_RULE = (
    "A name a writer simply uses for someone, in a greeting "
    '("Liebes Pünktchen!") or a mention, is their name form (label or '
    "alias), and an appellation too where it is no plain name form."
)
_MANIFEST_ALIASES, _RECORD = flat(ENTITY_MANIFEST_PROMPT).split(
    "The mention record (ADR-0035)"
)
NAME_ROUTING_SITES = pytest.mark.parametrize(
    "site",
    [flat(CHUNK_PROMPT), _MANIFEST_ALIASES],
    ids=["chunk", "manifest-aliases"],
)
RECORD_SITES = pytest.mark.parametrize(
    "site",
    [flat(CHUNK_PROMPT), _MANIFEST_ALIASES, _RECORD],
    ids=["chunk", "manifest-aliases", "record"],
)


class TestAppellations:
    @NAME_ROUTING_SITES
    def test_every_form_that_names_a_person_is_an_alias(self, site):
        assert ALIAS_RULE in site

    @NAME_ROUTING_SITES
    def test_a_form_no_plain_name_is_also_referred_to_as(self, site):
        assert APPELLATION_RULE in site

    @NAME_ROUTING_SITES
    def test_a_description_is_no_alias(self, site):
        assert DESCRIPTION_RULE in site

    @RECORD_SITES
    def test_a_nickname_is_a_record_nickname_and_a_relation_is_not(self, site):
        assert NICKNAME_RULE in site

    @RECORD_SITES
    def test_a_name_a_writer_uses_is_their_name_form(self, site):
        """The postcard's "Liebes Pünktchen!" uses Klara's pet name: a
        name form of hers, and an appellation too."""
        assert GREETING_RULE in site

    def test_the_chunk_types_a_nickname_by_what_the_person_is_called(self):
        assert (
            "nickname (a name marked as what the person is called, such as "
            'an "aka")'
        ) in flat(CHUNK_PROMPT)

    def test_the_chunk_gives_one_claim_per_appellation(self):
        assert (
            "Give each appellation of the person as ONE `referred_to_as` "
            "claim (the form verbatim, never cut to its name or its kinship "
            "word; a form the document repeats is still one claim)"
        ) in flat(CHUNK_PROMPT)

    def test_a_literal_kinship_term_also_gives_the_kinship(self):
        assert (
            "If it holds a LITERAL kinship term, record the kinship too, as "
            'above: "Tante Rahel", where Rahel is the aunt, also gives '
            '`relative` + `kinship_type` "Tante" + `kinship_class` '
            "kin_aunt_or_uncle, with the ego as the object."
        ) in flat(CHUNK_PROMPT)
        assert (
            "If it is NOT a literal relation (a courtesy title such as a "
            '"Tante" who is no aunt, "Nana Maisie", a pet name, or a name '
            'one person calls another, "Bubele"), it gives no kinship: '
            "`referred_to_as` alone, beside a `nickname` claim where the "
            "source marks a pet name as what the person is called."
        ) in flat(CHUNK_PROMPT)

    def test_an_unsure_term_is_referred_to_as_alone(self):
        assert (
            "When unsure whether a term is literal kinship, give "
            "`referred_to_as` alone"
        ) in flat(CHUNK_PROMPT)

    def test_the_chunk_works_the_example_through(self):
        """ "Tante Rahel", "Omama" named elsewhere and never named, and a
        pet name, each with its claims."""
        chunk = flat(CHUNK_PROMPT)
        assert (
            'a letter to Klara that writes "Tante Rahel" and elsewhere '
            '"Rahel Löbig" gives Rahel the claims `referred_to_as` "Tante '
            'Rahel", with Klara as its `perspective`, and `relative` to '
            'Klara with the kinship_type "Tante" and the kinship_class '
            "kin_aunt_or_uncle."
        ) in chunk
        assert (
            '`relative` to Nicholas with the kinship_type "Omama" and the '
            "kinship_class kin_grandparent"
        ) in chunk
        assert (
            'whether it names her elsewhere ("Hildegard Löbig", her label) '
            'or never (when "Omama" is her label).'
        ) in chunk
        assert (
            'The postcard\'s "Liebes Pünktchen!" gives Klara `referred_to_as` '
            '"Pünktchen", with its writer as its `perspective`: a pet name, '
            "and no relation."
        ) in chunk

    def test_the_manifest_works_the_example_through(self):
        assert (
            'a letter to Klara that writes "Tante Rahel" and elsewhere '
            '"Rahel Löbig" gives one entity labelled "Rahel Löbig" with the '
            'alias "Tante Rahel". A letter of Nicholas\'s that calls his '
            'grandmother Hildegard "Omama" and names her "Hildegard Löbig" '
            'gives the label "Hildegard Löbig" and the alias "Omama"; one '
            'that never names her gives the label "Omama" (status '
            '`kinship`). The postcard\'s "Liebes Pünktchen!" gives Klara the '
            'alias "Pünktchen", a pet name. Each of these forms is also a '
            "`referred_to_as` claim at extraction."
        ) in _MANIFEST_ALIASES

    def test_a_description_is_captured_as_a_claim(self):
        assert (
            "They are captured as claims at extraction (a literal kinship "
            "term becomes `relative` + `kinship_type`; a spousal term gives a "
            "marriage). Never record any of these as an alias (ADR-0018);"
        ) in _MANIFEST_ALIASES

    def test_a_courtesy_label_is_a_relational_appellation(self):
        assert (
            "`courtesy`, a courtesy title or other relational appellation "
            'that is no literal relation ("Nana Maisie")'
        ) in _RECORD

    def test_a_diminutive_nickname_is_also_a_given_name(self):
        assert (
            'A nickname that is a form of a given name ("Gustl", "Gus") is '
            "also that given name, with its standard form where the document "
            'supports it; one that stands for no given name ("Peppi") has no '
            "standard form."
        ) in _RECORD

    def test_a_kinship_word_is_no_record_part_and_no_title(self):
        """Scratch run 2 set aside two records that filed "Tante" or
        "Onkel" as a title (#514, superseded by #499): the record
        prompt says a kinship word is part of the appellation only."""
        assert (
            'A kinship or relational word before a name ("Tante", "Onkel", '
            '"Oma", "Aunt"), literal or a courtesy one, is part of the '
            "appellation (an alias and a `referred_to_as` claim), never part "
            "of the record: neither a name part nor a `titles` entry. "
            '"Tante Rahel" gives the given name Rahel and nothing for '
            '"Tante".'
        ) in _RECORD
        assert (
            "Titles are honorifics and ranks (Mrs, Dr., Frau, Fräulein, "
            "Mefrou, Rabbi), never a kinship word"
        ) in _RECORD

    @RECORD_SITES
    def test_the_size_of_the_calling_group_routes_no_name(self, site):
        """The family's "Gustl at home" is a nickname: what makes a name
        relational is that it names the person through someone, never how
        many call them so."""
        assert "or group" not in site

    def test_the_single_call_asks_for_referred_to_as(self):
        assert (
            "Every form the document uses to name or address a person that "
            "is no plain name form (a name, or a spelling, a shortening, a "
            "diminutive or an initial of one) is an appellation"
        ) in flat(EXTRACTION_PROMPT)
        assert (
            "Give each as ONE `referred_to_as` claim, the form verbatim, with "
            "a `perspective` qualifier naming the ego only where the source "
            "gives it as an entity; a literal kinship term also gives the "
            "kinship"
        ) in flat(EXTRACTION_PROMPT)
        assert (
            '`relative` with the kinship_type "Tante" and the kinship_class '
            "kin_aunt_or_uncle)."
        ) in flat(EXTRACTION_PROMPT)
        assert (
            'A description that is no form of address ("the deceased", "my '
            'husband") gives none.'
        ) in flat(EXTRACTION_PROMPT)

    def test_the_critic_is_told_an_appellation_is_right(self):
        assert (
            "An appellation (a kinship term, alone or with a name, a pet "
            "name or a courtesy name) among a person's aliases, or recorded "
            "as a `referred_to_as` claim, and a kinship term as `relative` + "
            "`kinship_type`, are right — do not flag them."
        ) in flat(critique.CRITIQUE_PROMPT)

    def test_the_vocabulary_draws_the_same_line(self):
        """The single-call prompt names the two properties through the
        manifest's descriptions too."""
        assert "{property_keys}" in EXTRACTION_PROMPT
        properties, _items = vocabulary_prompt_lines()
        assert (
            "nickname: monolingualtext — a name the source marks as what the "
            'person is called, by anyone ("Peppi", "Gustl"); never their '
            "formal name, and never what one person calls another or a name "
            "through a relation (`referred_to_as` alone)"
        ) in properties
        assert (
            "the person or family whose term it is (a grandchild who calls a "
            'grandmother "Omama"; a child who calls a non-relative "Nana '
            'Maisie").'
        ) in properties
        assert (
            "referred_to_as: monolingualtext — a form naming or addressing "
            "the person that is no plain name form: a kinship term with a "
            'name ("Tante Rahel") or alone ("Omama"), a pet name, a courtesy '
            'name ("Nana Maisie"); verbatim, with `perspective` when the ego '
            "is known"
        ) in properties

    def test_the_critic_spares_an_appellation_among_the_aliases(self):
        category = critique.CATEGORIES["kinship_term_alias"]
        assert category.startswith(
            "a kinship term or other appellation recorded as the label of a "
            "person the document names"
        )
        assert (
            "A kinship term, a pet name or a courtesy name among the aliases "
            '("Tante Rahel", "Omama", "Pünktchen", "Nana Maisie") is right '
            "and no finding."
        ) in category
        assert "a marriage" in category
        assert "`referred_to_as`" in category

    @pytest.mark.parametrize(
        "ambiguous",
        [
            "pet appellation",
            "family/pet",
            "courtesy/pet",
            "informal/pet",
            "endearment",
            "familiar name",
        ],
    )
    def test_no_prompt_gives_a_name_an_ambiguous_kind(self, ambiguous):
        properties, _items = vocabulary_prompt_lines()
        texts = {
            **PROMPTS,
            "vocabulary": properties,
            "critic": str(critique.CATEGORIES),
        }
        for name, text in texts.items():
            assert ambiguous not in flat(text), name


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


def appellation(local_id: str, label: str, text: str, language="de"):
    value = {
        "type": "monolingual",
        "value": {"text": text, "language": language},
    }
    return {
        "subject": {
            "kind": "local",
            "local_id": local_id,
            "label": label,
            "instance_of": "human",
        },
        "property": "referred_to_as",
        "value": value,
        "source": {
            "artifact": DOC_REF.model_dump(),
            "transcription_excerpt": {"text": text, "language": "de"},
        },
    }


class TestTheAppellationLabel:
    """Where the batch is assembled, once the chunks' claims exist: a
    label that is one of the document's appellations of a person whose
    `name` record stands becomes the name (ADR-0018 as amended by #499),
    in the entity and in every local ref, and coverage.json counts it."""

    def extract(self, tmp_path):
        llm = ScriptedLlm(
            [
                manifest(
                    HILDEGARD
                    | {
                        "label": "Omama",
                        "aliases": ["Hildegard Löbig"],
                        "structure": name_record("Hildegard", "Löbig"),
                    },
                    SEBASTIAN
                    | {
                        "label": "Opa",
                        "aliases": ["Sebastian Löbig"],
                        "structure": name_record("Sebastian", "Löbig"),
                    },
                ),
                ndjson(
                    [
                        claim("e1", "Omama", 1876),
                        appellation("e1", "Omama", "Omama"),
                    ]
                ),
                # a referred_to_as whose language is no tag is a reject,
                # and marks nothing
                ndjson(
                    [
                        claim("e2", "Opa", 1862),
                        appellation("e2", "Opa", "Opa", language="Deutsch"),
                    ]
                ),
            ]
        )
        batch = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        ).extract(transcription(), DOC_REF)
        return batch, side_output(tmp_path, "coverage.json")

    def test_the_label_becomes_the_name_and_the_form_an_alias(self, tmp_path):
        batch, _coverage = self.extract(tmp_path)
        by_id = {e.id: e for e in batch.entities}
        assert by_id["e1"].label == "Hildegard Löbig"
        assert by_id["e1"].aliases == ["Omama"]
        subjects = {
            c.subject.label for c in batch.claims if c.subject.local_id == "e1"
        }
        assert subjects == {"Hildegard Löbig"}
        (said,) = [c for c in batch.claims if c.property == "referred_to_as"]
        assert said.value.value.text == "Omama"

    def test_a_rejected_claim_marks_no_appellation(self, tmp_path):
        batch, coverage = self.extract(tmp_path)
        by_id = {e.id: e for e in batch.entities}
        assert by_id["e2"].label == "Opa"
        assert coverage["rejected_claims"] == 1

    def name_claim(self, local_id, label, text):
        found = appellation(local_id, label, text)
        found["property"] = "name"
        return found

    def extract_names(self, tmp_path):
        """Hildegard labelled "Omama", her record holding it as a
        nickname, with three `name` claims: one restating the model's
        label, one the name her label becomes, and one another form.
        Sebastian's appellation names an ego outside the manifest."""
        omama = appellation("e2", "Opa", "Opa")
        omama["qualifiers"] = [
            {
                "property": "perspective",
                "value": {
                    "type": "item",
                    "value": {"kind": "local", "local_id": "e9"},
                },
            }
        ]
        record = name_record("Hildegard", "Löbig") | {"nicknames": ["Omama"]}
        llm = ScriptedLlm(
            [
                manifest(
                    HILDEGARD
                    | {
                        "label": "Omama",
                        "aliases": ["Hildegard Löbig"],
                        "structure": record,
                    },
                    SEBASTIAN
                    | {
                        "label": "Opa",
                        "aliases": ["Sebastian Löbig"],
                        "structure": name_record("Sebastian", "Löbig"),
                    },
                ),
                ndjson(
                    [
                        claim("e1", "Omama", 1876),
                        appellation("e1", "Omama", "Omama"),
                        self.name_claim("e1", "Omama", "Omama"),
                        # the new label, cased otherwise
                        self.name_claim("e1", "Omama", "HILDEGARD Löbig"),
                        self.name_claim("e1", "Omama", "Hilde"),
                    ]
                ),
                ndjson([claim("e2", "Opa", 1862), omama]),
            ]
        )
        extractor = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        )
        batch = extractor.extract(transcription(), DOC_REF)
        return batch, side_output(tmp_path, "coverage.json"), extractor

    def test_a_name_claim_restating_the_models_label_stays_rejected(
        self, tmp_path
    ):
        batch, coverage, extractor = self.extract_names(tmp_path)
        names = [
            c.value.value.text for c in batch.claims if c.property == "name"
        ]
        assert names == ["Hilde"]
        rejected = [r["claim"] for r in extractor._rejects]
        assert [c["property"] for c in rejected] == ["name", "referred_to_as"]
        assert rejected[0]["value"]["value"]["text"] == "Omama"
        assert coverage["rejected_claims"] == 2

    def test_a_name_claim_of_the_new_label_is_dropped_and_counted(
        self, tmp_path
    ):
        batch, coverage, _extractor = self.extract_names(tmp_path)
        by_id = {e.id: e for e in batch.entities}
        assert by_id["e1"].label == "Hildegard Löbig"
        assert coverage["records"]["appellation_label_names"] == 1

    def test_an_appellation_naming_an_ego_outside_the_manifest_marks_nothing(
        self, tmp_path
    ):
        batch, _coverage, _extractor = self.extract_names(tmp_path)
        assert {e.id: e for e in batch.entities}["e2"].label == "Opa"

    def test_a_record_part_that_is_an_appellation_is_counted(self, tmp_path):
        _batch, coverage, _extractor = self.extract_names(tmp_path)
        assert coverage["records"]["appellation_parts"] == 1

    def test_coverage_counts_the_labels_it_replaced(self, tmp_path):
        _batch, coverage = self.extract(tmp_path)
        assert coverage["records"]["appellation_labels"] == 1
        assert "Omama" not in json.dumps(coverage)


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
            "unsplit_forms": 0,
            "set_aside_records": 0,
            "set_aside": {},
            "containers_checked": 0,
            "containers_dropped": {},
            "appellation_labels": 0,
            "appellation_label_names": 0,
            "appellation_parts": 0,
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
            "unsplit_forms": 0,
            "set_aside_records": 3,
            "set_aside": {"addition": 1, "kind": 1, "schema": 1},
            "containers_checked": 0,
            "containers_dropped": {},
            "appellation_labels": 0,
            "appellation_label_names": 0,
            "appellation_parts": 0,
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
        # a note set aside sets no record aside
        assert coverage["records"]["set_aside_records"] == 0
        assert coverage["notes"] == {"identity": 1}

    def test_the_records_set_aside_are_counted_once_each(self, tmp_path):
        """A record that gives several reasons is one record set aside:
        Hildegard's gives three given names no form of hers holds
        (Klara, Ida and the initial H.) and the initial's standard
        form, four reasons in all. Flörburg's toponym with no name is
        the second record set aside. A note set aside on the same
        entity sets no further record aside. The unsplit forms are
        counted in the records given, and only there."""
        _batch, coverage = extract(
            tmp_path,
            HILDEGARD
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [
                        {"written": "Klara"},
                        {"written": "Ida"},
                        {
                            "written": "H.",
                            "initial": True,
                            "standard": "Hilde",
                        },
                    ],
                    "unsplit": ["Hildegard Löbig"],
                },
                "note": {"category": "guess", "text": "read as Hildegard"},
            },
            SEBASTIAN
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "unsplit": ["Sebastian Löbig", "Sebastian"],
                }
            },
            FLOERBURG | {"structure": {"kind": "place", "status": "toponym"}},
        )
        assert coverage["records"] == {
            "given": 1,
            "recordless": 2,
            "by_status": {"name": 1},
            "unsplit_forms": 2,
            "set_aside_records": 2,
            "appellation_labels": 0,
            "appellation_label_names": 0,
            "appellation_parts": 0,
            "set_aside": {
                "addition": 3,
                "initial": 1,
                "no toponym": 1,
                "note": 1,
            },
            "containers_checked": 0,
            "containers_dropped": {},
        }
        assert "Klara" not in json.dumps(coverage)

    def test_a_record_whose_empty_parts_are_dropped_stands(self, tmp_path):
        """An empty standard form is absent and gives no reason; an empty
        name part is dropped and counted as `dropped`, but the record
        it leaves stands, so no record is set aside. Flörburg's only
        name, dropped, leaves a toponym with no name: that record is
        set aside, once, with both its reasons (#497)."""
        batch, coverage = extract(
            tmp_path,
            HILDEGARD
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [{"written": "Hildegard", "standard": ""}],
                    "surnames": [{"written": "Löbig"}, {"written": ""}],
                }
            },
            SEBASTIAN
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [{"written": "Sebastian", "standard": ""}],
                    "titles": [""],
                }
            },
            FLOERBURG
            | {
                "structure": {
                    "kind": "place",
                    "status": "toponym",
                    "names": [{"written": "", "head": ""}],
                }
            },
        )
        structures = {e.id: e.structure for e in batch.entities}
        assert structures["e1"].given[0].standard is None
        assert [s.written for s in structures["e1"].surnames] == ["Löbig"]
        assert structures["e2"].titles == []
        assert structures["e3"] is None
        assert coverage["records"] == {
            "given": 2,
            "recordless": 1,
            "by_status": {"name": 2},
            "unsplit_forms": 0,
            "set_aside_records": 1,
            "set_aside": {"dropped": 3, "no toponym": 1},
            "containers_checked": 0,
            "containers_dropped": {},
            "appellation_labels": 0,
            "appellation_label_names": 0,
            "appellation_parts": 0,
        }

    def test_a_list_entry_written_as_an_object_reads_as_its_text(
        self, tmp_path
    ):
        """An entry of a list of strings written as an object holding
        only `written` is that text, counted as `read as text`: the
        record stands when what is left can, and is no record set aside;
        one whose text is no name part of the entity is then set aside
        for `addition`, once (#514)."""
        batch, coverage = extract(
            tmp_path,
            HILDEGARD
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [{"written": "Hildegard"}],
                    "surnames": [{"written": "Löbig"}],
                    "unsplit": [{"written": "Hildegard Löbig"}],
                }
            },
            SEBASTIAN
            | {
                "structure": {
                    "kind": "human",
                    "status": "name",
                    "given": [{"written": "Sebastian"}],
                    "titles": [{"written": "Inh."}],
                }
            },
        )
        structures = {e.id: e.structure for e in batch.entities}
        assert structures["e1"].unsplit == ["Hildegard Löbig"]
        assert structures["e2"] is None
        assert coverage["records"]["set_aside_records"] == 1
        assert coverage["records"]["set_aside"] == {
            "addition": 1,
            "read as text": 2,
        }
        assert coverage["records"]["unsplit_forms"] == 1

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
        assert parsed.set_aside_records == 1

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
            "rejected_by_reason",
            "unread_objects",
            "fallback",
            # each claim's excerpt looked for in the transcription (#580)
            "quotes",
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
        assert "does not take an artifact as its subject" in problem

    def test_issued_by_takes_no_other_subject(self):
        """An `issued_by` claim is about the document itself (ADR-0035
        §3.4): one whose subject is a person is refused, and the
        extraction counts it as a reject."""
        person_issued = cited(
            subject=item("e1", "Hildegard Löbig", "human")["value"],
            property="issued_by",
            value=item("e9", "Standesamt Flörburg", "organization"),
        )
        batch = ClaimBatch.model_validate(
            {"extraction_run": run(), "claims": [person_issued]}
        )
        (problem,) = validate_against_manifest(batch)
        assert "takes as its subject the claim's own source" in problem
        kept, rejects = salvage_valid_claims([person_issued], run())
        assert kept.claims == []
        (reject,) = rejects
        assert reject["claim"]["property"] == "issued_by"

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
        # counted by its reason, for the run report (#456)
        assert coverage["rejected_by_reason"] == {"document_subject": 1}


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
        result = write_mapped(BatchWriter(wiki), batch)
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

    @pytest.mark.parametrize(
        ("subject", "prop", "value", "why"),
        [
            (
                item("e1", "Hildegard Löbig", "human")["value"],
                "issued_by",
                item("e9", "Standesamt Flörburg", "organization"),
                "its property takes only its source document",
            ),
            (
                OTHER_REF,
                "point_in_time",
                {"type": "time", "year": 1941},
                "its property does not take an artifact as its subject",
            ),
            (
                OTHER_REF,
                "issued_by",
                item("e9", "Standesamt Flörburg", "organization"),
                "its subject is neither its own source nor an artifact given "
                "to this write as placed by its source",
            ),
            (
                DOC_REF,
                "point_in_time",
                {"type": "time", "year": 1941},
                "its property does not take an artifact as its subject",
            ),
        ],
    )
    def test_it_refuses_a_document_subject_the_rule_forbids(
        self, subject, prop, value, why
    ):
        """The writer holds the document-subject rule itself, as a batch
        handed to `yizkr write` is never validated against the manifest:
        a claim sourced from Gustav's birth register extract (A) whose
        subject names the 1941 death certificate (B), or names A on a
        property that does not take it, or an `issued_by` claim about a
        person, is refused and counted, and no statement lands on either
        artifact item."""
        wiki = IssuingWiki()
        items = {}
        for ref in (DOC_REF, OTHER_REF):
            artifact = wiki.wbi.item.new()
            artifact.write()
            wiki.media[ref.sha1] = artifact.id
            items[ref.sha1] = artifact.id
        if isinstance(subject, ArtifactRef):
            subject = subject.model_dump()
        forbidden = cited(subject=subject, property=prop, value=value)
        # every local entity has its record, as the identity map reads a
        # batch with a manifest by its records (#109)
        hildegard = {"id": "e1", "label": "Hildegard Löbig", "kind": "human"}
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [forbidden],
                "entities": [STANDESAMT, hildegard],
            }
        )
        result = write_mapped(BatchWriter(wiki), batch)
        (only,) = batch.claims
        assert result.refused == [claim_id(only)]
        assert result.written == 0
        # a refused claim's refs never resolve, so it mints nothing
        assert result.created_items == {}
        assert any(why in warning for warning in result.warnings)
        for qid in items.values():
            assert not wiki.wbi.item.get(qid).claims.get(
                PROPERTIES["point_in_time"]
            )
            assert not wiki.wbi.item.get(qid).claims.get("P90")

    def test_a_subject_naming_an_unregistered_document_is_refused(self):
        """Only a claim's own source is looked up before the write: a
        claim sourced from Gustav's birth register extract (A) whose
        subject names the 1941 death certificate (B), which has no item,
        is refused and counted, and the rest of the batch is written."""
        wiki = IssuingWiki()
        artifact = wiki.wbi.item.new()
        artifact.write()
        wiki.media[DOC_REF.sha1] = artifact.id
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    issued_by(DOC_REF.model_dump()),
                    cited(
                        subject=OTHER_REF.model_dump(),
                        property="point_in_time",
                        value={"type": "time", "year": 1941},
                    ),
                ],
                "entities": [STANDESAMT],
            }
        )
        result = write_mapped(BatchWriter(wiki), batch)
        _, foreign = batch.claims
        assert result.refused == [claim_id(foreign)]
        assert result.written == 1
        assert wiki.wbi.item.get(artifact.id).claims.get("P90")


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
