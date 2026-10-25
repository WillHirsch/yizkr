"""What the extraction prompts say of deaths, births, union ends and
officials (ADR-0040 §1, §2.2, §2.5, §2.8, §8).

A prompt change re-asks every extraction call, so these pins name each
rule's words. Every example comes from the fictional universe
(docs/EXAMPLES.md).
"""

from __future__ import annotations

import pytest

from yizkr.pipeline.chunking import CHUNK_PROMPT, ENTITY_MANIFEST_PROMPT
from yizkr.pipeline.extract import DOCUMENT_ITSELF, EXTRACTION_PROMPT


def flat(text: str) -> str:
    return " ".join(text.split())


CHUNK = flat(CHUNK_PROMPT)
SINGLE = flat(EXTRACTION_PROMPT)
MANIFEST = flat(ENTITY_MANIFEST_PROMPT)
DOCUMENT = flat(DOCUMENT_ITSELF)
CLAIM_PROMPTS = pytest.mark.parametrize(
    "prompt", [CHUNK, SINGLE], ids=["chunked", "single"]
)


class TestDeathsAndBirthsAreOnThePerson:
    @CLAIM_PROMPTS
    def test_no_birth_or_death_is_an_event(self, prompt):
        assert "A birth and a death are statements on the person" in prompt
        assert "`death` event" not in prompt
        assert "partner's own `death`" not in prompt

    def test_the_manifest_makes_no_birth_or_death_entity(self):
        assert "a birth or a death is never an entity" in MANIFEST
        assert "(births, deaths," not in MANIFEST

    @CLAIM_PROMPTS
    def test_a_death_without_its_date_is_the_unknown_value(self, prompt):
        assert '"value": {{"type": "unknown"}}' in prompt
        assert "no date qualifiers" in prompt or (
            "no earliest_date, latest_date or circa" in prompt
        )
        assert 'A blank field, "d. ?" or "date unknown" gives no claim' in (
            prompt
        )

    def test_the_chunk_prompt_gives_it_beside_a_place_cause_or_age(self):
        assert "beside any place, cause or age the source gives" in CHUNK
        assert "Where the same source dates the death itself, give that " in (
            CHUNK
        )


class TestUnionEnds:
    @CLAIM_PROMPTS
    def test_only_a_stated_form_ends_a_union_by_a_death(self, prompt):
        assert "`ended_by_death_of` that partner" in prompt
        assert (
            "ONLY where the source states the couple were married when that "
            "partner died" in prompt
        )
        for form in ('"Witwer der Cäcilie Löbig"', '"Ehefrau des Eugen"'):
            assert form in prompt, form

    @CLAIM_PROMPTS
    def test_a_marriage_and_a_death_stated_apart_give_no_end(self, prompt):
        assert "A marriage and a partner's death stated apart" in prompt
        assert "give no end of any kind" in prompt

    def test_a_widow_marker_gives_no_death_statement(self):
        assert "NO death statement for the spouse" in CHUNK
        assert (
            'One phrase that is both markers ("вдова Вашего покойного '
            'брата Рудольфа"' in CHUNK
        )
        assert "and no `date_of_death`" in CHUNK

    def test_the_phrase_that_is_both_markers_gives_both_claims(self):
        # the widowhood and the union end, and no death for the spouse
        assert (
            "your late brother Rudolf) gives the same: the survivor's "
            "`civil_status` `widowed` and the union's `ended_by_death_of`, "
            "and no `date_of_death`;" in CHUNK
        )
        assert (
            "A widow's or widower's marker gives the survivor's "
            "`civil_status` `widowed`; one that names the late spouse also "
            "gives their marriage, `ended_by_death_of` that spouse, and no "
            "death statement for the spouse, and so does one phrase that is "
            "both markers" in SINGLE
        )

    def test_the_single_call_prompt_says_so_too(self):
        assert "and no death statement for the spouse" in SINGLE
        assert "one phrase that is both markers" in SINGLE


class TestOfficials:
    def test_the_boilerplate_rule_covers_every_such_role(self):
        assert (
            "A person who only certifies, registers, officiates, examines, "
            "reports or witnesses the record" in MANIFEST
        )
        assert "unless the same record gives them another role" in MANIFEST
        assert "A rank, a job, an address or a signature is no other role" in (
            MANIFEST
        )
        # Sebastian stays as Gustav's father
        assert 'Sebastian, who "zeigte an" Gustav\'s birth' in MANIFEST

    def test_an_unnamed_reporter_is_no_entity(self):
        assert '("Informant: husband", "the registrar")' in MANIFEST
        assert "even where the role word is a kinship word" in MANIFEST

    def test_the_wedding_bullet_leaves_out_its_registrar(self):
        assert "a witness or officiant who is kin of the couple" in CHUNK
        assert "a registrar, and a witness who is no kin" in CHUNK

    def test_the_residence_rule_names_no_informant(self):
        assert "the informant/applicant" not in CHUNK
        assert "(e.g. the applicant on a certificate)" in CHUNK

    def test_a_certifying_signature_is_no_authorship(self):
        assert (
            "a signature that only certifies or registers (a registrar's, a "
            "notary's) is no authorship" in DOCUMENT
        )

    @CLAIM_PROMPTS
    def test_a_left_out_person_is_no_claims_value(self, prompt):
        assert "is no claim's subject or value" in prompt or (
            "is never a claim's value" in prompt
        )


class TestParentage:
    @CLAIM_PROMPTS
    def test_a_parents_kind_only_where_the_source_states_it(self, prompt):
        assert "`parentage`" in prompt
        assert '"Adoptivvater"' in prompt


class TestPinnedWords:
    """The words a reworded prompt could lose without another test
    noticing (ADR-0040 §4, §8)."""

    def test_an_unnamed_reporter_makes_no_person_and_no_union(self):
        assert (
            '("Informant: husband", "the registrar"), is no entity at all, '
            "even where the role word is a kinship word: it makes no person "
            "and no union." in MANIFEST
        )

    def test_a_step_term_takes_a_step_class(self):
        assert (
            'A step term ("mein Stiefvater") is `relative` with '
            "kinship_class kin_step_parent (kin_step_child, "
            "kin_step_sibling) and the word in kinship_type" in CHUNK
        )
        assert (
            'a half term ("Halbbruder") is `sibling` with the word in '
            "kinship_type" in CHUNK
        )

    def test_the_critics_vital_category_flags_a_missing_claim(self):
        from yizkr.pipeline.critique import CATEGORIES

        assert CATEGORIES["vital_not_on_person"].startswith(
            "a birth or a death the transcription states with a date or a "
            "place, where no claim gives that person the date or the place"
        )
