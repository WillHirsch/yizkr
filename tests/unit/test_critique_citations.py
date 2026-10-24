"""The critic reads each citation where it stands (#607, ADR-0003 as
amended, ADR-0023).

The critique prompt carries the document's transcription, each line
numbered, and every claim's row gives its times and its citation: the
excerpt with the lines (and the page) where it occurs. Each container's
evidence is shown so too. One support test replaces the special-case
rules, and three keys name what fails it.

Every text is the fictional universe's (docs/EXAMPLES.md): the 1909 burial
register, the 1938 birth register extract, the 1941 death certificate's
district slot and Fläckfeld's two districts.
"""

from __future__ import annotations

import json

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch, load_vocabulary
from yizkr.pipeline.critique import (
    CATEGORIES,
    CRITIQUE_PROMPT,
    Finding,
    Invalid,
    check,
    listings,
    run_critique,
)
from yizkr.pipeline.transcribe import (
    PageProvenance,
    TranscribedPage,
    Transcription,
)

SCAN = ArtifactRef.from_bytes(b"fictional scan: the 1909 burial register")
RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-16T09:00:00Z",
}

# the 1909 burial register: Flörburg in its heading and in Eugen's death
REGISTER = "\n".join(
    [
        "Friedhof Flörburg, Beerdigungsbuch 1909.",  # 1
        "Nr. 14. Eugen Löbig, Hutmacher, 73 Jahre.",  # 2
        "Gestorben: 16. März 1909 in Flörburg.",  # 3
        "Beerdigt: 19. März 1909.",  # 4
    ]
)
# the 1938 extract's one line, which names Flörburg twice
EXTRACT = (
    "Standesamt Flörburg, Geburtsregister Nr. 12/1919: Gustav Löbig, "
    "geboren am 7. Januar 1919 in Flörburg."
)
# the 1941 certificate's place of death, its fields on one line
DEATH_PLACE = (
    "Место смерти: город, селение д. Кошновка; район Калдовский; область, "
    "край Чадская; республика РСФСР."
)


def flat(text: str) -> str:
    return " ".join(text.split())


class Recording:
    """A critic that answers nothing, keeping each prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return '{"done": true}'


def transcription(
    text: str, pages: list[tuple[int, str]] = ()
) -> Transcription:
    """`text`, or, with `pages`, each page's text after its marker line."""
    if not pages:
        return Transcription(artifact=SCAN, text=text, engine="test")
    parts, spans = [], []
    offset = 0
    for number, body in pages:
        marker = f"[Page {number}]\n"
        offset += len(marker)
        spans.append((number, offset, offset + len(body)))
        parts.append(marker + body)
        offset += len(body) + 1
    return Transcription(
        artifact=SCAN,
        text="\n".join(parts),
        engine="test",
        pages=[
            TranscribedPage(
                page=number,
                start=start,
                end=end,
                status="transcribed",
                provenance=PageProvenance(method="vision", engine="test"),
            )
            for number, start, end in spans
        ],
    )


def claim(
    property_: str,
    value: dict,
    excerpt: str | None,
    *,
    subject: str = "e01",
    page: str | None = None,
    qualifiers: list[dict] = (),
) -> dict:
    source = {"artifact": SCAN.model_dump(mode="json")}
    if excerpt is not None:
        source["transcription_excerpt"] = {"text": excerpt, "language": "de"}
    if page is not None:
        source["page_number"] = page
    return {
        "subject": {"kind": "local", "local_id": subject},
        "property": property_,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": source,
    }


def place_of_death(excerpt: str | None, **kwargs) -> dict:
    return claim(
        "place_of_death",
        {"type": "item", "value": {"kind": "local", "local_id": "e02"}},
        excerpt,
        **kwargs,
    )


def eugen_batch(
    *claims: dict, mention_lines=(), within=(), place_lines=(1,)
) -> ClaimBatch:
    """Eugen (e01) and Flörburg (e02), with `claims`; Flörburg's record
    puts it in each of `within`, its mentions on `place_lines`."""
    flörburg = {"id": "e02", "label": "Flörburg", "kind": "place"}
    if within:
        flörburg["mention_lines"] = list(place_lines)
        flörburg["structure"] = {
            "kind": "place",
            "names": [{"written": "Flörburg"}],
            "within": list(within),
        }
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {
                    "id": "e01",
                    "label": "Eugen Löbig",
                    "kind": "human",
                    "mention_lines": list(mention_lines),
                },
                flörburg,
            ],
            "claims": list(claims),
        }
    )


def prompt_of(batch: ClaimBatch, given: Transcription | None) -> str:
    llm = Recording()
    run_critique(llm, batch, given)
    (prompt,) = llm.prompts
    return prompt


def rows(prompt: str) -> list[str]:
    """The claims block's rows."""
    block = prompt.split("\nClaims (subject | property | value | times | ")[1]
    return block.split(":\n", 1)[1].split("\n\n")[0].splitlines()


def within_rows(prompt: str) -> list[str]:
    block = prompt.split(
        "where it occurs, nearest the place's mentions\nfirst:\n"
    )
    return block[1].split("\n\n")[0].splitlines()


class TestThePrompt:
    def test_it_carries_the_numbered_transcription(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg")), transcription(REGISTER)
        )
        assert (
            "The source's transcription, each line numbered:\n"
            "1: Friedhof Flörburg, Beerdigungsbuch 1909.\n"
            "2: Nr. 14. Eugen Löbig, Hutmacher, 73 Jahre.\n"
            "3: Gestorben: 16. März 1909 in Flörburg.\n"
            "4: Beerdigt: 19. März 1909.\n"
        ) in prompt

    def test_with_no_transcription_it_carries_none(self):
        prompt = prompt_of(eugen_batch(place_of_death("Flörburg")), None)
        assert "each line numbered:\n(none)\n" in prompt
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | not in the "
            'transcription: "Flörburg"'
        ]

    def test_a_claim_row_gives_its_times_and_citation(self):
        dated = claim(
            "date_of_death",
            {"type": "time", "year": 1909, "month": 3, "day": 16},
            "Gestorben: 16. März 1909",
        )
        prompt = prompt_of(
            eugen_batch(
                dated, place_of_death("Gestorben: 16. März 1909 in Flörburg.")
            ),
            transcription(REGISTER),
        )
        batch = eugen_batch(dated)
        (listing,) = listings(batch)
        assert rows(prompt)[0] == (
            f"e01 | date_of_death | {listing[2]} | times: - | line 3: "
            '"Gestorben: 16. März 1909"'
        )
        assert rows(prompt)[1] == (
            "e01 | place_of_death | e02 | times: - | line 3: "
            '"Gestorben: 16. März 1909 in Flörburg."'
        )

    def test_each_row_s_value_column_is_the_listing(self):
        batch = eugen_batch(
            place_of_death(
                "Flörburg",
                qualifiers=[
                    {
                        "property": "point_in_time",
                        "value": {"type": "time", "year": 1909},
                    }
                ],
            ),
            place_of_death(None),
        )
        prompt = prompt_of(batch, transcription(REGISTER))
        for row, listing in zip(rows(prompt), listings(batch), strict=True):
            assert row.split(" | ")[2] == listing[2]
        assert rows(prompt)[0].split(" | ")[3] == "times: point_in_time 1909"

    def test_a_within_row_gives_the_evidence_s_lines(self):
        batch = eugen_batch(
            within=[
                {"name": "Kreis Peiltal", "evidence": "Flörburg"},
                {"name": "Hesse"},
            ]
        )
        assert within_rows(prompt_of(batch, transcription(REGISTER))) == [
            '- e02 within Kreis Peiltal | lines 1; 3: "Flörburg"',
            "- e02 within Hesse | -",
        ]

    def test_a_within_row_lists_the_place_s_mentions_first(self):
        batch = eugen_batch(
            within=[{"name": "Kreis Peiltal", "evidence": "Flörburg"}],
            place_lines=[3],
        )
        assert within_rows(prompt_of(batch, transcription(REGISTER))) == [
            '- e02 within Kreis Peiltal | lines 3; 1: "Flörburg"'
        ]

    def test_the_marker_line_shows_the_printed_page(self):
        given = transcription(
            "", [(1, "Friedhof Flörburg, Beerdigungsbuch 1909.")]
        )
        given.pages[0].printed_page = "17"
        prompt = prompt_of(eugen_batch(place_of_death("Flörburg")), given)
        assert (
            'each line numbered:\n1: [Page 1] (printed page number "17")\n'
            "2: Friedhof Flörburg, Beerdigungsbuch 1909.\n"
        ) in prompt

    def test_a_line_break_in_a_time_stays_on_its_row(self):
        batch = eugen_batch(
            place_of_death(
                "Flörburg",
                qualifiers=[
                    {
                        "property": "sourcing_circumstances",
                        "value": {
                            "type": "string",
                            "value": "um\n1909 \\ etwa",
                        },
                    }
                ],
            )
        )
        (row,) = rows(prompt_of(batch, transcription(REGISTER)))
        assert row.split(" | ")[3] == (
            "times: sourcing_circumstances um\\n1909 \\\\ etwa"
        )

    def test_the_separate_containment_block_is_gone(self):
        assert "Each `part_of` claim above" not in CRITIQUE_PROMPT
        assert "{containment_block}" not in CRITIQUE_PROMPT


class TestOccurrences:
    def test_a_citation_on_two_lines_lists_both(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg")), transcription(REGISTER)
        )
        assert rows(prompt) == [
            'e01 | place_of_death | e02 | times: - | lines 1; 3: "Flörburg"'
        ]

    def test_occurrences_on_one_line_show_once(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg")), transcription(EXTRACT)
        )
        assert rows(prompt) == [
            'e01 | place_of_death | e02 | times: - | line 1: "Flörburg"'
        ]

    def test_at_most_three_are_shown_and_the_rest_counted(self):
        text = "\n".join(f"Flörburg, Blatt {n}" for n in range(1, 6))
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg")), transcription(text)
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | lines 1; 2; 3 and 2 "
            'more: "Flörburg"'
        ]

    def test_those_nearest_the_subject_s_mentions_come_first(self):
        text = "\n".join(f"Flörburg, Blatt {n}" for n in range(1, 6))
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg"), mention_lines=[5]),
            transcription(text),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | lines 5; 4; 3 and 2 "
            'more: "Flörburg"'
        ]

    def test_an_occurrence_holding_a_mention_line_comes_first(self):
        # the occurrence over lines 2-4 holds the mention line, 4, though
        # it opens further from it than the one on line 5
        text = "\n".join(
            [
                "Nr. 14.",  # 1
                "Hutmacher",  # 2
                "",  # 3
                "Löbig.",  # 4
                "Hutmacher Löbig",  # 5
            ]
        )
        prompt = prompt_of(
            eugen_batch(place_of_death("Hutmacher Löbig"), mention_lines=[4]),
            transcription(text),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | lines 2-4; 5: "
            '"Hutmacher Löbig"'
        ]

    def test_an_excerpt_over_lines_gives_its_range(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("73 Jahre. Gestorben")),
            transcription(REGISTER),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | lines 2-3: "
            '"73 Jahre. Gestorben"'
        ]


class TestPages:
    def paged(self) -> Transcription:
        return transcription(
            "",
            [
                (1, "Friedhof Flörburg, Beerdigungsbuch 1909."),
                (2, "Gestorben: 16. März 1909 in Flörburg.\nFlörburg"),
            ],
        )

    def test_a_cited_page_shows_its_occurrences_alone(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg", page="2")), self.paged()
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | page 2, lines 4; 5: "
            '"Flörburg"'
        ]

    def test_an_excerpt_only_elsewhere_is_not_on_its_page(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Beerdigungsbuch 1909", page="2")),
            self.paged(),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | not on page 2 (line 2): "
            '"Beerdigungsbuch 1909"'
        ]

    def test_a_page_given_in_words_is_read_everywhere(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Beerdigungsbuch 1909", page="p. 2")),
            self.paged(),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | line 2: "
            '"Beerdigungsbuch 1909"'
        ]

    def test_a_page_the_transcription_has_not_is_read_everywhere(self):
        # an image excerpt's claims cite the container's page, which the
        # excerpt's own transcription does not number so
        prompt = prompt_of(
            eugen_batch(place_of_death("Beerdigungsbuch 1909", page="5")),
            self.paged(),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | line 2: "
            '"Beerdigungsbuch 1909"'
        ]

    def test_a_transcription_without_pages_reads_a_page_everywhere(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Flörburg", page="2")),
            transcription(REGISTER),
        )
        assert rows(prompt) == [
            'e01 | place_of_death | e02 | times: - | lines 1; 3: "Flörburg"'
        ]


class TestMissesAndClips:
    def test_a_true_miss_is_not_in_the_transcription(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Gestorben in Peiltal")),
            transcription(REGISTER),
        )
        assert rows(prompt) == [
            "e01 | place_of_death | e02 | times: - | not in the "
            'transcription: "Gestorben in Peiltal"'
        ]

    def test_a_clipped_excerpt_is_located_and_marked(self):
        # a long passage of the 2008 family history, over three lines
        passage = [
            "Die Hutmacherei zum Blauen Hut wurde 1863 am Brunnenring 35 "
            "in Flörburg gegründet und blieb über drei Generationen im "
            "Besitz der Familie Löbig, die dort Hüte fertigte.",
            "Nach dem Tod Eugens im Jahr 1909 führte sein Sohn Sebastian "
            "das Geschäft weiter, das im Adressbuch von 1912 als Inhaber "
            "genannt wird, bis es vor 1926 aufgegeben wurde.",
            "Von den Räumen am Brunnenring ist heute nichts mehr erhalten, "
            "doch erinnern sich die Enkel an die Hutformen im Keller und "
            "an den Geruch des Filzes in der Werkstatt.",
        ]
        text = "\n".join(["Familiengeschichte, 2008.", "", *passage])
        whole = " ".join(passage)
        assert len(whole) > 400
        # as the extraction clips an excerpt over 400 characters
        clipped = whole[:397].rsplit(" ", 1)[0] + "…"
        prompt = prompt_of(
            eugen_batch(place_of_death(clipped)), transcription(text)
        )
        (row,) = rows(prompt)
        assert row.startswith(
            "e01 | place_of_death | e02 | times: - | lines 3-5, clipped: "
            '"Die Hutmacherei'
        )
        assert row.endswith('…"')

    def test_a_clipped_excerpt_found_nowhere_is_a_miss(self):
        prompt = prompt_of(
            eugen_batch(place_of_death("Gestorben in Peiltal …")),
            transcription(REGISTER),
        )
        assert rows(prompt)[0].endswith(
            '| not in the transcription: "Gestorben in Peiltal …"'
        )

    def test_no_excerpt_is_a_dash(self):
        prompt = prompt_of(
            eugen_batch(place_of_death(None)), transcription(REGISTER)
        )
        assert rows(prompt) == ["e01 | place_of_death | e02 | times: - | -"]


class TestTheOneTest:
    PROMPT = flat(CRITIQUE_PROMPT)

    def test_the_test_is_stated_once(self):
        assert self.PROMPT.count("Read where it stands in the source") == 1
        assert (
            "Read where it stands in the source, does the passage convey "
            "the fact the claim or container states, its value and any time "
            "it carries?"
        ) in self.PROMPT
        assert (
            "The passage itself must carry what the statement adds, such as "
            "the container's name or the value; its context says how to "
            "read it: which document or form, which field, whose entry."
        ) in self.PROMPT

    def test_it_gives_the_passing_examples(self):
        assert (
            flat(
                f'on the 1941 death certificate\'s line "{DEATH_PLACE}", the '
                'evidence "район Калдовский" stands in the district slot of a '
                "place of death whose village is Koshnovka, and conveys that "
                "Koshnovka is in Kaldovsky District"
            )
            in self.PROMPT
        )
        assert (
            'so does "District: Kaldovsky District" in the district field '
            "of a form, one field a line, whose village field is Koshnovka"
        ) in self.PROMPT

    def test_it_gives_the_failing_examples(self):
        assert (
            "It fails: a passage that only names both places (a list of "
            'towns); a bare "Fläckfeld" beside "Flörburg, Kreis Peiltal", '
            'where "Kreis Peiltal" is about Flörburg, so Fläckfeld put in '
            'Kreis Peiltal on an excerpt that reads only "Fläckfeld" fails.'
        ) in self.PROMPT

    def test_any_occurrence_passes_on_the_cited_page(self):
        assert (
            "A citation passes when any one of its occurrences conveys the "
            "fact; for a row that names a page, the occurrence must be on "
            "that page. You may read any occurrence in the numbered "
            "transcription, not only those listed. A citation not in the "
            "transcription is judged on its words alone."
        ) in self.PROMPT

    def test_the_legend_says_a_page_lists_its_own_occurrences(self):
        assert (
            "a row that names a page lists only the occurrences on that "
            "page, and otherwise every occurrence is listed, nearest the "
            "subject's mentions first; at most three are shown"
        ) in self.PROMPT
        assert '"clipped" for one cut short (ending in "…")' in self.PROMPT

    def test_the_suggestion_re_cites_or_drops(self):
        assert (
            "When a citation fails, the suggestion's note names, by its "
            "lines, another passage of the source that conveys the fact "
            '("re-cite line 3"), and the claim or container stays; it says '
            '"drop" only when the source conveys the fact nowhere, and "drop '
            'the qualifier" when only a time fails.'
        ) in self.PROMPT
        assert (
            "In the 1909 burial register, Eugen's place of death Flörburg "
            'cited by the heading "Friedhof Flörburg, Beerdigungsbuch 1909." '
            '(line 1) is conveyed by line 3, "Gestorben: 16. März 1909 in '
            'Flörburg.": re-cite line 3. Fläckfeld put in Kreis Peiltal on '
            'an excerpt that reads only "Fläckfeld": drop.'
        ) in self.PROMPT

    def test_a_finding_names_a_claim_by_its_first_three_columns(self):
        assert (
            "Name a claim when the finding concerns one, by its first three "
            "columns (subject, property, value) exactly as listed"
        ) in self.PROMPT

    def test_the_three_keys_say_only_what_is_cited(self):
        assert flat(CATEGORIES["evidence_does_not_state"]) == (
            "a place's `within` entry whose evidence fails the support "
            "test: name the entry by its container as listed (`container`)"
        )
        assert flat(CATEGORIES["excerpt_does_not_state"]) == (
            "any other claim whose excerpt fails the support test, on its "
            "value or on its times, or, for a claim about an artifact, on "
            "which artifact it concerns: name the claim. Such a claim that "
            "cites no excerpt is not judged"
        )
        # a part_of with no excerpt stays a concern
        assert (
            "or that cites no excerpt, since then no passage conveys it"
            in (flat(CATEGORIES["unsupported_containment"]))
        )


class TestCheck:
    def batch(self) -> ClaimBatch:
        return eugen_batch(place_of_death("Friedhof Flörburg"))

    def finding(self, **changes) -> dict:
        return {
            "entity": "e01",
            "claim": {
                "subject": "e01",
                "property": "place_of_death",
                "value": "e02",
            },
            "axis": "value",
            "category": "excerpt_does_not_state",
            "issue": "the heading names the cemetery's town",
            "suggestion": {"note": "re-cite line 3"},
        } | changes

    def test_a_finding_on_the_new_key_is_valid(self):
        found = check(self.finding(), self.batch(), load_vocabulary())
        assert isinstance(found, Finding)

    @pytest.mark.parametrize(
        "category", ["unsupported_containment", "excerpt_does_not_state"]
    )
    def test_a_claim_key_finding_naming_no_claim_is_invalid(self, category):
        found = check(
            self.finding(category=category, claim=None),
            self.batch(),
            load_vocabulary(),
        )
        assert isinstance(found, Invalid)
        assert found.why == [f"an {category} finding names no claim"]

    def test_another_key_needs_no_claim(self):
        found = check(
            self.finding(category="implausible_value", claim=None),
            self.batch(),
            load_vocabulary(),
        )
        assert isinstance(found, Finding)


def test_the_report_counts_the_new_key():
    from yizkr.pipeline.report import _known

    assert _known("excerpt_does_not_state", CATEGORIES) == (
        "excerpt_does_not_state"
    )


def test_a_row_is_quoted_as_json():
    prompt = prompt_of(
        eugen_batch(place_of_death('Gestorben: 16. März 1909 in "Flörburg"')),
        transcription('Gestorben: 16. März 1909 in "Flörburg"'),
    )
    quoted = json.dumps(
        'Gestorben: 16. März 1909 in "Flörburg"', ensure_ascii=False
    )
    assert rows(prompt) == [
        f"e01 | place_of_death | e02 | times: - | line 1: {quoted}"
    ]
