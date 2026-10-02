"""A label the writer cuts to Wikibase's limit keeps whole letters, by
the rule the registrar cuts file names and descriptions by
(`writer.extends`, ADR-0010 "File names MediaWiki takes"; #373): no cut
falls inside an emoji joined by zero-width joiners, nor between a
stressed Cyrillic vowel and its accent. Fictional names only
(docs/EXAMPLES.md)."""

from __future__ import annotations

import pytest

from yizkr.pipeline.writer import CUT_MARK, TERM_LIMIT, _term_text

JOINER = "\N{ZERO WIDTH JOINER}"
FAMILY = f"\N{MAN}{JOINER}\N{WOMAN}{JOINER}\N{GIRL}"
# a stressed vowel: a letter and a combining acute accent, which NFC
# leaves apart
STRESSED = "а\N{COMBINING ACUTE ACCENT}"


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
def test_a_label_cut_keeps_a_stressed_letter_whole(pad):
    """One run of letters fills the room, so the cut falls inside it: it
    keeps each vowel with its accent, whichever of the two the room ends
    at."""
    label = "x" * pad + STRESSED * 200
    rest = kept(label)[pad:]
    assert rest and rest == STRESSED * (len(rest) // len(STRESSED))
