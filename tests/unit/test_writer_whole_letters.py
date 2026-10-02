"""A label the writer cuts to Wikibase's limit keeps whole letters, by
the rule the registrar cuts file names and descriptions by
(`writer.extends`, ADR-0010 "File names MediaWiki takes"; #373): no cut
falls inside an emoji joined by zero-width joiners, halfway through a
flag, nor inside a stressed Cyrillic vowel, a Devanagari conjunct or an
old Hangul syllable. Fictional names only (docs/EXAMPLES.md)."""

from __future__ import annotations

import pytest

from yizkr.pipeline.writer import (
    CUT_MARK,
    TERM_LIMIT,
    _term_text,
    extends,
)

JOINER = "\N{ZERO WIDTH JOINER}"
FAMILY = f"\N{MAN}{JOINER}\N{WOMAN}{JOINER}\N{GIRL}"
# a stressed vowel: a letter and a combining acute accent, which NFC
# leaves apart
STRESSED = "а\N{COMBINING ACUTE ACCENT}"
# a Devanagari conjunct: two letters a virama joins
CONJUNCT = (
    "\N{DEVANAGARI LETTER KA}"
    "\N{DEVANAGARI SIGN VIRAMA}"
    "\N{DEVANAGARI LETTER SSA}"
)
# an old Hangul syllable, whose jamo NFC leaves apart
JAMO = (
    "\N{HANGUL CHOSEONG KIYEOK}"
    "\N{HANGUL JUNGSEONG ARAEA}"
    "\N{HANGUL JONGSEONG KIYEOK}"
)
GERMANY = (
    "\N{REGIONAL INDICATOR SYMBOL LETTER D}"
    "\N{REGIONAL INDICATOR SYMBOL LETTER E}"
)


def kept(label: str) -> str:
    """What the cut label keeps of `label`, before its mark."""
    term = _term_text(label)
    assert len(term) <= TERM_LIMIT and CUT_MARK in term
    return term.rsplit(f"{CUT_MARK} sha1 ", 1)[0]


@pytest.mark.parametrize("pad", range(5))
def test_a_label_cut_keeps_an_emoji_family_whole(pad):
    """However the room falls, at the joiners or between the people, the
    label keeps whole families: each pad puts the room at another place
    within one."""
    before = "Löbig " + "x" * pad
    label = before + FAMILY * 80
    rest = kept(label)[len(before) :]
    assert rest == FAMILY * (len(rest) // len(FAMILY))


@pytest.mark.parametrize("pad", range(2))
def test_a_label_cut_keeps_a_flag_whole(pad):
    """A cut between two regional indicators would split no word, as they
    are no letters, so only half_flag keeps the flag whole: each pad puts
    the room at another half of one."""
    before = "Löbig " + "x" * pad
    label = before + GERMANY * 150
    rest = kept(label)[len(before) :]
    assert rest == GERMANY * (len(rest) // len(GERMANY))


@pytest.mark.parametrize("unit", [STRESSED, CONJUNCT, JAMO])
@pytest.mark.parametrize("pad", range(3))
def test_a_label_cut_inside_one_run_keeps_whole_letters(unit, pad):
    """One run of letters fills the room, so the cut falls inside it: it
    keeps each letter whole, wherever in one the room ends."""
    label = "x" * pad + unit * 200
    rest = kept(label)[pad:]
    assert rest and rest == unit * (len(rest) // len(unit))


A = "a"
LEADING = "\N{HANGUL CHOSEONG KIYEOK}"
VIRAMA = "\N{DEVANAGARI SIGN VIRAMA}"


@pytest.mark.parametrize(
    "char, before, joins",
    [
        # each range of _EXTENDERS: its first and last assigned code
        # points extend a letter, and the code points either side do not
        (0x200B, A, False),
        (0x200C, A, True),
        (0x200D, A, True),
        (0x200E, A, False),
        (0x1F3FA, A, False),
        (0x1F3FB, A, True),
        (0x1F3FF, A, True),
        (0x1F400, A, False),
        (0xE001F, A, False),
        (0xE0020, A, True),
        (0xE007F, A, True),
        (0xE0080, A, False),
        (0xFF9D, A, False),
        (0xFF9E, A, True),
        (0xFF9F, A, True),
        (0xFFA0, A, False),
        (0x115F, A, False),
        (0x1160, A, True),
        (0x11FF, A, True),
        (0x1200, A, False),
        (0xD7AF, A, False),
        (0xD7B0, A, True),
        (0xD7FB, A, True),
        (0xD800, A, False),
        # a leading jamo joins a leading jamo or a precomposed syllable
        # after it: each range's ends, and the code points either side
        (0x10FF, LEADING, False),
        (0x1100, LEADING, True),
        (0x115F, LEADING, True),
        (0xA95F, LEADING, False),
        (0xA960, LEADING, True),
        (0xA97C, LEADING, True),
        (0xABFF, LEADING, False),
        (0xAC00, LEADING, True),
        (0xD7A3, LEADING, True),
        (0xD7A4, LEADING, False),
        # and only after a leading jamo, at either end of either range
        (0xAC00, "\N{GEORGIAN LETTER LABIAL SIGN}", False),
        (0xAC00, chr(0x1100), True),
        (0xAC00, chr(0x115F), True),
        (0xAC00, chr(0x1160), False),
        (0xAC00, "\N{REJANG SECTION MARK}", False),
        (0xAC00, chr(0xA960), True),
        (0xAC00, chr(0xA97C), True),
        (0xAC00, chr(0xA980), False),
        # a letter after a virama joins it; nothing else after it does
        (ord("\N{DEVANAGARI LETTER SSA}"), VIRAMA, True),
        (ord("\N{DEVANAGARI DIGIT ONE}"), VIRAMA, False),
        (ord(" "), VIRAMA, False),
        (ord("\N{DEVANAGARI LETTER SSA}"), "\N{DEVANAGARI SIGN NUKTA}", False),
    ],
)
def test_what_extends_a_letter(char, before, joins):
    assert extends(chr(char), before) is joins
