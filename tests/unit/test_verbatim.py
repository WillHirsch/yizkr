"""The verbatim comparison's third reading of a hyphen at a line's end,
spaced, and its fold of typographic quotes, dashes and the soft hyphen
(#612, ADR-0035 §5), and a dash at a line's end, a break between words
(#664).

Every text is the fictional universe's (docs/EXAMPLES.md): the hat firm
at Brunnenring 35, Flörburg, the 1952 letter's sender in Drakeradiel, and
Mockburg-Süd, the worked example of a name a line breaks at its own
hyphen.
"""

from __future__ import annotations

import pytest

from yizkr.claims import EVIDENCE_NOT_IN_SOURCE, Container, container_problem
from yizkr.core.verbatim import Transcript, fold, word_readings, words

# the firm's street broken at a hyphen for layout alone
STREET = "in Flörburg, Brunnen-\nring 35"
# a suspended hyphen the line breaks at its end
TRADES = "Hut-\nund Mützenmacherei zum Blauen Hut"
SENDER = "Snylsjum, gemeente Drake-\nradiel, Fryslân"


class TestTheSpacedReading:
    """A hyphen at a line's end is read a third way, kept with a space
    after it, as a quote copies the line's end."""

    def test_a_layout_hyphen_copied_with_its_space(self):
        assert Transcript(STREET).span("Brunnen- ring 35") == (1, 2)
        assert Transcript(SENDER).locate("gemeente Drake- radiel") == 1

    def test_a_suspended_hyphen_broken_at_a_line_s_end(self):
        found = Transcript(TRADES)
        # its one correct quote
        assert found.span("Hut- und Mützenmacherei") == (1, 2)
        # the two other readings still match, as before
        assert found.locate("Hutund Mützenmacherei") == 1
        assert found.locate("Hut-und Mützenmacherei") == 1

    def test_the_joined_and_kept_readings_still_match(self):
        found = Transcript(STREET)
        assert found.locate("Brunnenring 35") == 1
        assert found.locate("Brunnen-ring 35") == 1

    def test_an_excerpt_s_own_line_end_hyphen_against_a_spaced_one(self):
        # the transcription holds the hyphen and space within a line
        assert (
            Transcript("Hut- und Mützenmacherei").locate(
                "Hut-\nund Mützenmacherei"
            )
            == 1
        )

    def test_a_spaced_hyphen_within_a_line_is_no_line_end(self):
        assert Transcript("Brunnen- ring 35").locate("Brunnenring 35") is None

    def test_a_spaced_dash_is_unchanged(self):
        assert (
            Transcript("Snylsjum -\nFryslân").locate("Snylsjum - Fryslân") == 1
        )
        assert (
            Transcript("Snylsjum -\nFryslân").locate("Snylsjum- Fryslân")
            is None
        )


class TestTheSpacedReadingMustCross:
    """The spaced reading takes only an excerpt that crosses the hyphen,
    as the kept one does: part of a word never matches (#589)."""

    @pytest.mark.parametrize(
        "excerpt",
        [
            # stops at the hyphen, or on it
            "in Flörburg, Brunnen",
            "in Flörburg, Brunnen-",
            "gemeente Drake",
            "gemeente Drake-",
            # starts after it
            "ring 35",
            "radiel, Fryslân",
            # starts on it
            "- ring 35",
        ],
    )
    def test_part_of_a_word_is_no_match(self, excerpt):
        text = SENDER if "Drake" in excerpt or "radiel" in excerpt else STREET
        assert Transcript(text).locate(excerpt) is None

    def test_another_kept_hyphen_bounds_a_match_too(self):
        found = Transcript("Mock-\nburg-\nSüd")
        assert found.locate("Mock- burg- Süd") == 1
        assert found.locate("burg- Süd") is None
        assert found.locate("Mock- burg") is None

    def test_a_part_of_the_word_is_not_in_the_source(self):
        reason, _ = container_problem(
            Container(name="Drake", evidence="gemeente Drake-"),
            Transcript(SENDER),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE


class TestTheContainerItNames:
    """The evidence's own words are read the same three ways, so evidence
    the spaced reading finds names the joined word."""

    def test_spaced_evidence_names_the_joined_word(self):
        assert word_readings("gemeente Drake- radiel")[-1] == [
            "gemeente",
            "drakeradiel",
        ]
        assert container_problem(
            Container(name="Drakeradiel", evidence="gemeente Drake- radiel"),
            Transcript(SENDER),
        ) == (None, (1, 2))

    @pytest.mark.parametrize("hyphen", ["\u2010", "\u2011"])
    @pytest.mark.parametrize("break_", [" ", "\n"])
    @pytest.mark.parametrize("written", ["-", "\u2010"])
    def test_typographic_hyphen_evidence_names_the_joined_word(
        self, hyphen, break_, written
    ):
        # the evidence's words read its typographic hyphens as the
        # transcript's reading does, so evidence found there names the word
        assert container_problem(
            Container(
                name="Drakeradiel",
                evidence=f"gemeente Drake{hyphen}{break_}radiel",
            ),
            Transcript(f"Snylsjum, gemeente Drake{written}\nradiel, Fryslân"),
        ) == (None, (1, 2))

    def test_an_unspaced_hyphen_within_a_line_is_not_joined(self):
        assert word_readings("Brunnen-ring")[-1] == ["brunnen", "ring"]

    def test_a_hyphenated_name_spaced(self):
        assert container_problem(
            Container(name="Mockburg-Süd", evidence="Mockburg- Süd"),
            Transcript("Mockburg-\nSüd, Kreis Mockburg"),
        ) == (None, (1, 2))


class TestTheFold:
    """Typographic quotes and dashes read as their plain forms."""

    @pytest.mark.parametrize(
        ("typographic", "plain"),
        [
            ("\u201ezum Blauen Hut\u201c", '"zum Blauen Hut"'),
            ("\u201ezum Blauen Hut\u201f", '"zum Blauen Hut"'),
            ("\u201azum Blauen Hut\u2018", "'zum Blauen Hut'"),
            ("\u201bzum Blauen Hut\u2019", "'zum Blauen Hut'"),
            ("\u00abzum Blauen Hut\u00bb", '"zum Blauen Hut"'),
            ("\u2039zum Blauen Hut\u203a", "'zum Blauen Hut'"),
            ("Mockburg\u2011Süd", "Mockburg-Süd"),
            ("1912\u20121926", "1912-1926"),
            ("1912\u20151926", "1912-1926"),
            ("\u22127", "-7"),
        ],
    )
    def test_each_reads_plain(self, typographic, plain):
        assert fold(typographic) == fold(plain)
        assert (
            Transcript(f"Hutmacherei {typographic}").locate(
                f"Hutmacherei {plain}"
            )
            == 1
        )

    def test_a_quote_of_the_firm_s_name(self):
        found = Transcript(
            "Hutmacherei \u201ezum Blauen Hut\u201c, gegr. 1863"
        )
        assert found.locate('Hutmacherei "zum Blauen Hut"') == 1
        assert found.locate("Hutmacherei \u00abzum Blauen Hut\u00bb") == 1


class TestTheSoftHyphen:
    """A soft hyphen is dropped, and at a line's end joins the next
    line's word."""

    def test_dropped_within_a_line(self):
        assert fold("Brunnen\u00adring") == "brunnenring"
        assert words("Brunnen\u00adring 35") == ["brunnenring", "35"]
        assert (
            Transcript("in Flörburg, Brunnen\u00adring 35").locate(
                "Brunnenring 35"
            )
            == 1
        )

    def test_joined_at_a_line_s_end(self):
        found = Transcript("in Flörburg, Brunnen\u00ad\nring 35")
        assert found.span("Flörburg, Brunnenring 35") == (1, 2)
        assert words("Brunnen\u00ad\nring 35") == ["brunnenring", "35"]
        # quoted with the soft hyphen too
        assert found.locate("Brunnen\u00ad\nring") == 1

    def test_only_the_line_it_ends_is_joined(self):
        found = Transcript(
            "in Flörburg, Brunnen\u00ad\nring 35\nInh. Sebastian Löbig"
        )
        assert found.locate("Brunnenring 35 Inh. Sebastian Löbig") == 1

    def test_before_a_line_that_opens_on_no_word_it_is_a_space(self):
        assert Transcript("Brunnen\u00ad\n(35)").locate("Brunnen (35)") == 1

    def test_after_a_space_it_adds_none(self):
        found = Transcript("Hutmacherei \u00ad\nzum Blauen Hut")
        assert found.locate("Hutmacherei zum Blauen Hut") == 1

    def test_part_of_the_word_is_no_match(self):
        found = Transcript("in Flörburg, Brunnen\u00ad\nring 35")
        assert found.locate("in Flörburg, Brunnen") is None
        assert found.locate("ring 35") is None
        # an invisible break is no hyphen: none of the hyphen's readings
        assert found.locate("Brunnen- ring 35") is None
        assert found.locate("Brunnen-ring 35") is None
        assert found.locate("Brunnen ring 35") is None


DASHES = ["‒", "–", "—", "―", "−"]


class TestADashAtALinesEnd:
    """Only the hyphen, U+2010, U+2011 and the soft hyphen break a word
    at a line's end; a dash there is a break between words (#664)."""

    @pytest.mark.parametrize("dash", DASHES)
    def test_the_years_either_side_are_words(self, dash):
        found = Transcript(f"Brunnenring 35, 1912{dash}\n1926")
        assert found.locate("1912") == 1
        assert found.span("1926") == (2, 2)
        assert found.span(f"1912{dash} 1926") == (1, 2)
        assert found.span("1912- 1926") == (1, 2)
        # never one word, nor the kept dash closed up
        assert found.locate("19121926") is None
        assert found.locate(f"1912{dash}1926") is None

    @pytest.mark.parametrize("dash", DASHES)
    def test_the_name_before_the_dash_is_whole(self, dash):
        found = Transcript(f"Gustav Löbig{dash}\nder Hutmacher")
        assert found.locate("Gustav Löbig") == 1
        assert found.span("der Hutmacher") == (2, 2)
        assert found.locate("Gustav Löbigder Hutmacher") is None

    @pytest.mark.parametrize("dash", DASHES)
    def test_its_words_are_read_apart(self, dash):
        for reading in word_readings(f"1912{dash}\n1926"):
            assert reading == ["1912", "1926"]
        # a dash spaced within a line is no copied line-end hyphen
        for reading in word_readings(f"Gustav Löbig{dash} der Hutmacher"):
            assert reading == ["gustav", "lobig", "der", "hutmacher"]

    @pytest.mark.parametrize("dash", DASHES)
    def test_an_excerpt_s_own_dash_is_no_hyphen(self, dash):
        # an excerpt that copies the line's end reads the dash spaced
        assert (
            Transcript(f"Gustav Löbig{dash} der Hutmacher").locate(
                f"Löbig{dash}\nder"
            )
            == 1
        )
        assert (
            Transcript("Gustav Löbigder Hutmacher").locate(f"Löbig{dash}\nder")
            is None
        )

    @pytest.mark.parametrize("dash", DASHES)
    def test_container_evidence_names_both_words(self, dash):
        assert container_problem(
            Container(name="Drake", evidence=f"gemeente Drake{dash}\nradiel"),
            Transcript(f"Snylsjum, gemeente Drake{dash}\nradiel, Fryslân"),
        ) == (None, (1, 2))
        reason, _ = container_problem(
            Container(name="Drakeradiel", evidence="gemeente Drakeradiel"),
            Transcript(f"Snylsjum, gemeente Drake{dash}\nradiel, Fryslân"),
        )
        assert reason == EVIDENCE_NOT_IN_SOURCE

    @pytest.mark.parametrize("hyphen", ["-", "‐", "‑", "­"])
    def test_each_hyphen_still_breaks_a_word(self, hyphen):
        found = Transcript(
            f"Snylsjum, gemeente Drake{hyphen}\nradiel, Fryslân"
        )
        assert found.span("gemeente Drakeradiel") == (1, 2)
        assert words(f"Drake{hyphen}\nradiel") == ["drakeradiel"]
        # #589's negatives: part of a word is no match
        assert found.locate("gemeente Drake") is None
        assert found.locate("radiel, Fryslân") is None

    def test_the_hyphens_are_those_the_fold_reads_as_one(self):
        from yizkr.core import verbatim

        assert set(verbatim._HYPHENS) == {"-", "‐", "‑"}
        assert {fold(c) for c in verbatim._HYPHENS} == {"-"}
