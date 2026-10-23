"""Offline tests pinning the extraction prompts' rules on names, kinds,
dates and containment, and the critic's containment check (#459, #460,
#463, #464, #465; ADR-0018, ADR-0013, ADR-0009).

Scratch run 1 (#145) found kinship words kept as labels and aliases,
`unclassified_entity` used where a real kind fits, groups typed as one
person, bare years typed as events, dates read with false precision or
in the wrong century, and places put in a container their source never
names. The fix is the prompts' wording, never a wording-based filter in
code (ADR-0035): these tests read the prompt text, and the critic keeps
flagging what slips through. Every example comes from the fictional
universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.corpus.engine import _ENTITY_ROW
from yizkr.claims import ArtifactRef, ClaimBatch, load_vocabulary
from yizkr.pipeline import critique
from yizkr.pipeline.chunking import (
    CHUNK_PROMPT,
    ENTITY_MANIFEST_PROMPT,
    LINE_ASSIGNMENT_BLOCK,
)
from yizkr.pipeline.extract import EXTRACTION_PROMPT

SCAN = ArtifactRef.from_bytes(b"fictional scan: the 1905 marriage record")
RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-16T09:00:00Z",
}


def flat(text: str) -> str:
    return " ".join(text.split())


MANIFEST = flat(ENTITY_MANIFEST_PROMPT)
CHUNK = flat(CHUNK_PROMPT)
LINES = flat(LINE_ASSIGNMENT_BLOCK)
SINGLE = flat(EXTRACTION_PROMPT)
ESCAPE_HATCH = flat(critique.CATEGORIES["escape_hatch_misused"])
CONTAINMENT_CATEGORY = critique.CATEGORIES.get("unsupported_containment", "")

# -- kinship words are never labels of the named (#459, #499, ADR-0018) -----


class TestKinshipWordsAreNoNames:
    def test_the_manifest_takes_the_name_never_the_kinship_word(self):
        assert (
            "A kinship word is never the label of a person the document "
            "names: the label takes the name, never the kinship word, and "
            "the form stays an alias (ADR-0018)."
        ) in MANIFEST

    def test_the_manifest_gives_a_kinship_term_alone(self):
        assert (
            'a kinship term alone ("Oma", "Omama", "Mum", "Grandma", "Mem", '
            '"дядя"): an alias, and the label only of a person the document '
            "never names"
        ) in MANIFEST

    def test_the_manifest_gives_a_kinship_term_with_a_name(self):
        """The whole phrase is an alias, and the name beside the kinship
        word follows the label and alias rules: "Aunt Pünktchen" gives
        Klara's nickname as an alias too, and the 1958 letter's "дядя
        Себастьян", his only name there, his label."""
        assert (
            'a kinship term with a name ("Aunt Pünktchen", "Tante Rahel", '
            '"дядя Себастьян"): the whole phrase is an alias, never the '
            "label, and the name follows the label and alias rules below: a "
            'nickname is an alias too ("Pünktchen"), and a given name that is '
            "the person's only name in the document is the label "
            '("Себастьян");'
        ) in MANIFEST

    def test_the_manifest_lists_no_sole_given_name_as_an_alias(self):
        """A bare given name is never an alias (the name-composition
        rule), so no example may make one."""
        assert '"Rahel", "Себастьян"), never the whole phrase' not in MANIFEST
        assert 'the name alone is the alias ("Pünktchen", "Rahel"' not in (
            MANIFEST
        )

    def test_the_manifest_gives_a_writers_your_father(self):
        assert (
            'a writer\'s "your father" ("Dein Vater", "Ваш отец", "your '
            'aunt") for a person the document names elsewhere: the label is '
            "that name, and the phrase, a description, is neither label nor "
            "alias."
        ) in MANIFEST

    def test_the_manifest_still_labels_an_unnamed_writer_by_his_words(self):
        """The postcard's writer signs only "Dein Vater": with no name to
        take, his own words stay his label."""
        assert (
            'the postcard\'s writer signs "Dein Vater", so that is his label'
        ) in MANIFEST

    def test_the_chunk_takes_the_name_never_the_kinship_word(self):
        assert (
            "A kinship word is never the label of a person the document "
            "names: take the name, never the kinship word."
        ) in CHUNK

    def test_the_chunk_gives_a_kinship_term_alone_and_with_a_name(self):
        assert (
            'A kinship term alone ("Oma", "Omama", "Mum", "Grandma", "Mem") '
            'or with a name ("Aunt Pünktchen", "Tante Rahel", "дядя '
            'Себастьян") is an alias and an appellation, and the name beside '
            "it follows the label and alias rules: a nickname is an alias too "
            '("Pünktchen"), and a given name that is the person\'s only name '
            'in the document is the label ("Себастьян");'
        ) in CHUNK

    def test_the_chunk_gives_a_writers_your_father(self):
        assert (
            'a writer\'s "your father" ("Dein Vater", "Ваш отец") for a '
            "person named elsewhere leaves that name the label"
        ) in CHUNK

    def test_the_chunk_labels_only_an_unnamed_person_by_kinship_words(self):
        assert (
            "Only a person the document never names is labelled with its "
            "kinship words."
        ) in CHUNK

    def test_the_line_assignment_label_is_no_kinship_word(self):
        assert (
            "never a kinship word where the document names the person"
        ) in LINES

    def test_the_single_call_takes_the_name_never_the_kinship_word(self):
        assert (
            "A kinship word is never the label of a person the document "
            "names: take the name, never the kinship word."
        ) in SINGLE

    def test_the_single_call_gives_its_examples(self):
        assert (
            '"Aunt Pünktchen" is labelled "Pünktchen", or a fuller name the '
            'document gives her, and a writer\'s "Dein Vater" ("your '
            "father\") takes the father's name where the document gives it."
        ) in SINGLE

    def test_the_single_call_labels_only_an_unnamed_person_by_kinship(self):
        assert (
            "Only a person the document never names is labelled with its "
            'kinship words ("Mem").'
        ) in SINGLE

    def test_the_critic_flags_a_kinship_label_only(self):
        assert flat(critique.CATEGORIES["kinship_term_alias"]).startswith(
            "a kinship term or other appellation recorded as the label of a "
            "person the document names"
        )


# -- unclassified_entity only where no kind fits (#460, ADR-0013) -------------


class TestTheEscapeHatch:
    def test_the_manifest_keeps_it_for_what_nothing_in_the_list_fits(self):
        assert (
            "This is a LAST resort, right only where nothing in the list fits"
        ) in MANIFEST

    def test_the_manifest_leads_into_the_cases_with_a_kind(self):
        assert (
            "These always have a real kind, the one the relation calls for "
            "(above):"
        ) in MANIFEST

    @pytest.mark.parametrize(
        "case",
        [
            "an agency, a ministry, an office, a chancellery, a registry or "
            "a firm's department is an `organization` (the "
            "Wiedergutmachungsamt Peiltal, the Standesamt Flörburg), or a "
            "`place` where the source names it only as where something "
            "happened;",
            "a cemetery, a building, an address or another site is a "
            "`place` (the Friedhof Flörburg, Brunnenring 35), unless the "
            "source shows it acting (above);",
            "a named ship is a `vessel` (the RMS Flund);",
            "a law, a decree, a gazette or a register the source cites is a "
            "`legal_instrument`",
        ],
    )
    def test_the_manifest_names_the_cases_that_have_a_kind(self, case):
        assert case in MANIFEST

    def test_a_mentioned_document_keeps_the_escape_hatch(self):
        """An artifact kind is no entity kind (the manifest offers none),
        so a letter or certificate a source mentions takes the hatch."""
        assert (
            "A letter or a certificate the source mentions, other than the "
            "document you are reading, has no kind in the list: it is "
            '`unclassified_entity` with its type_hint ("letter", "death '
            "certificate\"), as the 1958 letter's enclosed certificate of "
            "Rudolf's death is."
        ) in MANIFEST

    @pytest.mark.parametrize(
        "prompt",
        [MANIFEST, LINES, SINGLE],
        ids=["manifest", "lines", "single"],
    )
    def test_an_aircraft_is_no_vessel(self, prompt):
        """The manifest's vessel is a ship or boat (Q11446): a named
        aircraft keeps the escape hatch."""
        assert "aircraft is a `vessel`" not in prompt
        assert "ship or aircraft" not in prompt
        assert (
            "aircraft is no vessel: it is `unclassified_entity` with the "
            'type_hint "aircraft".'
        ) in prompt.replace("no `vessel` either", "no vessel")

    def test_no_example_of_the_hatch_has_a_kind(self):
        """A regiment is a military_unit and a building a place: neither
        may stand as an example of what no kind fits."""
        assert '"military regiment"' not in MANIFEST
        assert "a building that is not a settlement" not in MANIFEST

    def test_the_line_assignment_names_the_cases_too(self):
        assert (
            "`unclassified_entity` is right only where no kind fits: an "
            "agency, an office or a firm's department is an organization; a "
            "cemetery or a building a place; a named ship a vessel."
        ) in LINES

    def test_the_single_call_names_the_cases_with_the_place_exception(self):
        """The single call has no relation-decides-the-kind rule of its
        own, so its agency case carries the exception itself."""
        assert (
            "`unclassified_entity` is right only where no kind fits: an "
            "agency, an office or a firm's department is an organization, "
            "unless the source names it only as where something happened, "
            "which makes it a place; a cemetery or a building is a place; a "
            "named ship a vessel."
        ) in SINGLE

    def test_the_critic_names_the_cases_and_spares_a_mentioned_document(self):
        assert (
            "an agency, office or firm's department is an `organization`; a "
            "cemetery or a building a `place`; a named ship a `vessel`."
        ) in ESCAPE_HATCH
        assert (
            "A named aircraft rightly takes it, as does a letter or a "
            "certificate a source mentions, since an artifact kind is no kind "
            "of entity"
        ) in ESCAPE_HATCH


# -- a group is no human, a date no event (#464) ------------------------------

CHILD_COUNT = (
    "A count of children the source states is the parent's "
    "`number_of_children`, whether or not it names them."
)


class TestGroupsAndDates:
    def test_the_manifest_never_types_a_group_as_one_human(self):
        assert (
            'a group of people is never one `human`. "The Löbig children" '
            'or "Sebastian\'s children" is a `family` where the source '
            "presents them as a family or household; else each child the "
            "source names is a `human` of their own (Ursula, Klara, Gustav)"
        ) in MANIFEST

    def test_unnamed_members_are_no_entity_and_a_count_is_kept(self):
        """A stated count is kept beside named children too: the universe
        records Eugen's three children both ways."""
        assert (
            'where it names none ("drei Kinder"), the group is no entity at '
            "all. A count of children the source states is the parent's "
            "`number_of_children` claim at extraction, whether or not it "
            "names them;"
        ) in MANIFEST

    def test_the_manifest_never_makes_a_date_an_entity(self):
        assert (
            'a year or a date alone ("1938", "im Jahre 1905") is never an '
            "entity, and never an `event`: it is a time value "
            "(`point_in_time`, `start_time`, `end_time`) on the event it "
            "dates"
        ) in MANIFEST

    @pytest.mark.parametrize(
        "prompt", [CHUNK, SINGLE], ids=["chunk", "single"]
    )
    def test_a_stated_child_count_is_kept(self, prompt):
        """The entity path's chunk prompt claims the count the manifest
        leaves to extraction."""
        assert CHILD_COUNT in prompt

    def test_the_line_assignment_family_clause(self):
        assert (
            "A group of people is never one human: it is a family where the "
            "source presents a family or household, else each member it "
            "names."
        ) in LINES

    def test_the_line_assignment_counts_only_children(self):
        """ "Sebastian hatte drei Brüder" gives no number_of_children."""
        assert (
            "Where it names none of a person's children, their count is that "
            "person's `number_of_children`."
        ) in LINES

    def test_the_single_call_family_clause(self):
        assert (
            '"The Löbig children" is a family where the source presents a '
            "family or household, else each child it names."
        ) in SINGLE

    @pytest.mark.parametrize(
        "prompt", [LINES, SINGLE], ids=["lines", "single"]
    )
    def test_the_other_prompts_say_it_too(self, prompt):
        assert "group of people is never one human" in prompt
        assert (
            "A year or a date alone is no entity and no event: it dates the "
            "event it belongs to."
        ) in prompt


# -- dates: a year is year precision, a century is read in context (#463) ----


@pytest.mark.parametrize("prompt", [CHUNK, SINGLE], ids=["chunk", "single"])
class TestDatePrecision:
    def test_a_year_alone_is_year_precision(self, prompt):
        assert (
            'A year alone is year precision, never a full date: "1905" is '
            "never 1 January 1905 at day precision"
        ) in prompt

    def test_an_implied_century_is_read_in_context(self, prompt):
        assert (
            'A century or a decade the source implies ("in the thirties", '
            '"в тридцатые годы") is read in the document\'s own context (the '
            '1930s, in a letter of 1958), never as a literal "1st century" or '
            "the year 30"
        ) in prompt


def test_the_chunk_gives_a_month_its_precision():
    assert '"im Mai 1938" is month precision.' in CHUNK


def test_the_chunk_notes_an_implied_century():
    assert (
        'never as a literal "1st century" or the year 30, with a '
        "date_reading note."
    ) in CHUNK


# -- containment only where the source states it (#465, ADR-0009) ------------


class TestContainmentInThePrompts:
    def test_the_manifest_lists_containers_nearest_first(self):
        # #555: the place stage links each container to the next, so the
        # record gives them nearest first, whatever a form's field order.
        assert (
            "within: the places the source puts it in, as written, nearest "
            "first, whatever order the source writes them in"
        ) in MANIFEST
        assert "source's order" not in MANIFEST

    def test_the_manifest_gives_a_running_text_and_a_form_example(self):
        assert (
            '"Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân" is '
            'Snylsjum within "gemeente Drakeradiel", "Fryslân" and '
            '"Nederlân"'
        ) in MANIFEST
        assert (
            "a form whose fields run oblast, district, village, giving "
            "Chadsk Oblast, Kaldovsky District and Koshnovka, is Koshnovka "
            'within "Kaldovsky District" and "Chadsk Oblast".'
        ) in MANIFEST

    def test_the_chunk_gives_containment_only_where_stated(self):
        assert (
            "Give a place's containment (`part_of`, its country among them) "
            "only where the source states it: in the place's mention "
            '("Fläckfeld, Kreis Mockburg"), in the fields a form gives that '
            "place (a certificate's village, district and oblast fields), or "
            "in a chain of containers, which states each link"
        ) in CHUNK

    @pytest.mark.parametrize(
        "prompt", [CHUNK, SINGLE], ids=["chunk", "single"]
    )
    def test_a_chain_states_each_link(self, prompt):
        assert (
            '("Snylsjum, gemeente Drakeradiel, Fryslân" also puts gemeente '
            "Drakeradiel in Fryslân)"
        ) in prompt

    def test_the_chunk_says_why(self):
        assert (
            "Containment the source does not state is world knowledge, "
            "proposed later by the place pass (ADR-0009)"
        ) in CHUNK

    def test_the_chunk_never_infers_or_carries_it_over(self):
        assert (
            "never infer it from what you know of a place (that Flörburg "
            "lies in Kreis Peiltal, in Hesse), and never carry it over from "
            "another place the document names."
        ) in CHUNK
        assert (
            'Where a source writes "Flörburg, Kreis Peiltal" and, elsewhere, '
            'a bare "Fläckfeld", Fläckfeld takes no `part_of` at all.'
        ) in CHUNK

    def test_the_single_call_says_it_too(self):
        assert (
            "Give a place's containment (`part_of`, its country among them) "
            "only where the source states it: in the place's mention "
            '("Fläckfeld, Kreis Mockburg"), in the fields a form gives that '
            "place, or in a chain of containers, which states each link"
        ) in SINGLE
        assert (
            "Never infer it from what you know of a place, and never carry it "
            "over from another place the document names."
        ) in SINGLE


def containment_batch(*part_ofs: tuple[str, str, str | None]) -> ClaimBatch:
    """Fläckfeld (e01), Kreis Mockburg (e02) and Kreis Peiltal (e03), a
    `part_of` claim for each (subject, value, excerpt or None), and
    Fläckfeld's own instance claim, which is no containment."""
    claims = [
        {
            "subject": {"kind": "local", "local_id": subject},
            "property": "part_of",
            "value": {
                "type": "item",
                "value": {"kind": "local", "local_id": value},
            },
            "source": {
                "artifact": SCAN.model_dump(mode="json"),
                **(
                    {
                        "transcription_excerpt": {
                            "text": excerpt,
                            "language": "de",
                        }
                    }
                    if excerpt
                    else {}
                ),
            },
        }
        for subject, value, excerpt in part_ofs
    ]
    claims.append(
        {
            "subject": {"kind": "local", "local_id": "e01"},
            "property": "instance_of",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "place"},
            },
            "source": {
                "artifact": SCAN.model_dump(mode="json"),
                "transcription_excerpt": {
                    "text": "Fläckfeld",
                    "language": "de",
                },
            },
        }
    )
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {"id": "e01", "label": "Fläckfeld", "kind": "place"},
                {"id": "e02", "label": "Kreis Mockburg", "kind": "place"},
                {"id": "e03", "label": "Kreis Peiltal", "kind": "place"},
            ],
            "claims": claims,
        }
    )


class Recording:
    model_identifier = "fake-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return json.dumps({"done": True})


def critic_prompt(batch_: ClaimBatch) -> str:
    llm = Recording()
    critique.run_critique(llm, batch_)
    (prompt,) = llm.prompts
    return prompt


class TestTheCriticChecksContainment:
    def test_the_category_names_the_check(self):
        assert CONTAINMENT_CATEGORY.startswith(
            "a place's `part_of` claim (its container, or its country) that "
            "its excerpt does not state, neither in the place's mention, nor "
            "in the fields a form gives that place, nor as a link of a chain "
            "of containers"
        )
        assert "inferred from what is known of the place" in (
            CONTAINMENT_CATEGORY
        )
        assert "carried over from another place's container" in (
            CONTAINMENT_CATEGORY
        )
        assert (
            '(Fläckfeld put in Kreis Peiltal on an excerpt that reads only "'
            'Fläckfeld")'
        ) in CONTAINMENT_CATEGORY
        assert "(ADR-0009): suggest drop" in CONTAINMENT_CATEGORY

    def test_the_prompt_lists_the_category_and_asks_for_the_check(self):
        prompt = critic_prompt(containment_batch(("e01", "e02", "Fläckfeld")))
        assert "- unsupported_containment: a place's `part_of` claim" in prompt
        assert (
            "Check each against its excerpt: a containment the excerpt does "
            "not state (in the place's mention, in the fields a form gives "
            "that place, or as a link of a chain of containers) is a concern "
            "(unsupported_containment)."
        ) in flat(prompt)

    def test_each_part_of_claim_is_shown_with_its_excerpt(self):
        prompt = critic_prompt(
            containment_batch(
                ("e01", "e02", "Fläckfeld, Kreis Mockburg"),
                ("e01", "e03", "Fläckfeld"),
            )
        )
        block = prompt.split(
            "Each `part_of` claim above, with the excerpt of the source it "
            "cites:\n"
        )[1].split("\nCheck each")[0]
        assert block.splitlines() == [
            '- e01 part_of e02: "Fläckfeld, Kreis Mockburg"',
            '- e01 part_of e03: "Fläckfeld"',
        ]

    def test_a_claim_without_an_excerpt_says_so(self):
        prompt = critic_prompt(containment_batch(("e01", "e03", None)))
        assert "\n- e01 part_of e03: -\n" in prompt

    def test_a_batch_without_containment_shows_none(self):
        prompt = critic_prompt(containment_batch())
        assert (
            "with the excerpt of the source it cites:\n(none)\nCheck each"
        ) in prompt

    def test_an_excerpt_stays_on_its_line(self):
        prompt = critic_prompt(
            containment_batch(("e01", "e02", "Fläckfeld,\nKreis Mockburg"))
        )
        assert '- e01 part_of e02: "Fläckfeld,\\nKreis Mockburg"' in prompt

    def test_a_containment_line_is_no_entity_row(self):
        """The recorded engine tells a critique's document by its entity
        rows; a containment line, even one whose excerpt holds the claims
        block's separator, is never taken for one."""
        prompt = critic_prompt(
            containment_batch(("e01", "e02", "Fläckfeld | Kreis | Mockburg"))
        )
        assert set(_ENTITY_ROW.findall(prompt)) == {
            ("e01", "Fläckfeld"),
            ("e02", "Kreis Mockburg"),
            ("e03", "Kreis Peiltal"),
        }

    def test_a_finding_on_a_containment_is_valid(self):
        batch_ = containment_batch(("e01", "e03", "Fläckfeld"))
        finding = {
            "entity": "e01",
            "claim": {"subject": "e01", "property": "part_of", "value": "e03"},
            "axis": "existence",
            "category": "unsupported_containment",
            "issue": "the excerpt names no district",
            "suggestion": {"note": "drop"},
        }
        checked = critique.check(finding, batch_, load_vocabulary())
        assert isinstance(checked, critique.Finding)

    @pytest.mark.parametrize(
        "brk", ["\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"]
    )
    def test_no_line_break_splits_a_containment_line(self, brk):
        """JSON leaves U+2028, U+2029 and NEL raw, and splitlines() breaks
        at each: every break is escaped, so the line stays whole."""
        prompt = critic_prompt(
            containment_batch(("e01", "e02", f"Fläckfeld,{brk}Kreis Mockburg"))
        )
        (line,) = [
            line
            for line in prompt.splitlines()
            if line.startswith("- e01 part_of e02: ")
        ]
        assert line.endswith('Kreis Mockburg"')


@pytest.mark.parametrize(
    "brk", ["\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"]
)
def test_no_line_break_splits_a_listed_claim(brk):
    """A text value listed in the claims block stays on its line too."""
    batch_ = ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {"id": "e01", "label": "Sebastian Löbig", "kind": "human"}
            ],
            "claims": [
                {
                    "subject": {"kind": "local", "local_id": "e01"},
                    "property": "occupation",
                    "value": {
                        "type": "string",
                        "value": f"Hutmacher{brk}Meister",
                    },
                    "source": {"artifact": SCAN.model_dump(mode="json")},
                }
            ],
        }
    )
    (listing,) = critique.listings(batch_)
    assert (
        listing[2]
        == "Hutmacher"
        + brk.encode("unicode_escape").decode("ascii")
        + "Meister"
    )
    assert any(
        line.startswith("e01 | occupation | Hutmacher")
        and line.endswith("Meister")
        for line in critic_prompt(batch_).splitlines()
    )
