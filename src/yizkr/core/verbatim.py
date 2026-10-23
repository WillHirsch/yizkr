"""How an excerpt a model quotes is compared with the transcription it
quotes from (ADR-0035 §5).

An excerpt is verbatim when it occurs in the transcription with both
read the same way: folded (Unicode NFC, case folded, accents dropped),
typographic quotes, apostrophes and dashes read as their plain forms,
every run of whitespace, line breaks among it, made one space, and a
hyphen at a line's end joined to the next line's word, so "Drake-" on one
line and "radiel" on the next read "Drakeradiel". A hyphenated name the
line breaks at its own hyphen ("Peiltal-" then "Süd") is read a second
way, the hyphen kept ("Peiltal-Süd"), which is tried when the joined
reading holds no match. An excerpt matches whole words only: it may not
start or end inside a word of the transcription. Nothing else is
forgiven: a word the transcription does not hold, or holds in another
order, is no match. A mention record's container evidence is checked
this way, and a claim's excerpt can be.
"""

from __future__ import annotations

import re
import unicodedata

# a word that ends a line on a hyphen, and the word the next line opens
# with; the hyphen and the line break are dropped
_LINE_END_HYPHEN = re.compile(r"(?<=\w)-[^\S\r\n]*(?:\r\n|[\n\r])\s*(?=\w)")
_WORDS = re.compile(r"\w+")
# typographic punctuation, read as its plain form
_PLAIN = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2013": "-",
        "\u2014": "-",
    }
)


def fold(text: str) -> str:
    """Case and accents folded after Unicode normalisation, typographic
    quotes, apostrophes and dashes read as their plain forms."""
    decomposed = unicodedata.normalize(
        "NFD", unicodedata.normalize("NFC", text).casefold()
    )
    return "".join(
        c for c in decomposed if not unicodedata.combining(c)
    ).translate(_PLAIN)


def _flat(text: str, keep_hyphen: bool = False) -> tuple[str, list[int]]:
    """The text as it is compared (the module's reading), and for each of
    its characters the number of the line (from 1) it comes from. With
    `keep_hyphen`, a hyphen at a line's end stays, with no space after
    it, instead of being joined away."""
    chars: list[str] = []
    numbers: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = unicodedata.normalize("NFC", line).strip()
        if not line:
            continue
        if chars:
            hyphen = (
                chars[-1] == "-" and len(chars) > 1 and chars[-2].isalnum()
            )
            if hyphen and line[0].isalnum():
                # a hyphen at the line's end joins the next line's word
                if not keep_hyphen:
                    chars.pop()
                    numbers.pop()
            else:
                chars.append(" ")
                numbers.append(number)
        for char in line:
            if char.isspace():
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    numbers.append(number)
                continue
            for folded in fold(char):
                chars.append(folded)
                numbers.append(number)
    return "".join(chars), numbers


def _whole(flat: str, wanted: str) -> int | None:
    """Where `wanted` first occurs in `flat` neither starting nor ending
    inside a word, or None."""
    start = flat.find(wanted)
    while start >= 0:
        end = start + len(wanted)
        opens = (
            not wanted[0].isalnum() or not flat[start - 1 : start].isalnum()
        )
        closes = not wanted[-1].isalnum() or not flat[end : end + 1].isalnum()
        if opens and closes:
            return start
        start = flat.find(wanted, start + 1)
    return None


def joined(text: str) -> str:
    """`text` with each hyphen at a line's end joined to the next line's
    word, as the comparison reads it."""
    return _LINE_END_HYPHEN.sub("", text)


def words(text: str) -> list[str]:
    """The folded words of `text`, a hyphen at a line's end joined."""
    return _WORDS.findall(fold(joined(text)))


class Transcript:
    """A transcription as excerpts are compared with it."""

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines()
        # the joined reading first, then the one that keeps a hyphen at a
        # line's end
        self._readings = [_flat(text), _flat(text, keep_hyphen=True)]

    def line(self, number: int) -> str:
        """The transcription's line `number` (from 1) as written; "" for
        one it does not have."""
        if 1 <= number <= len(self._lines):
            return self._lines[number - 1]
        return ""

    def span(self, excerpt: str) -> tuple[int, int] | None:
        """The numbers of the first and the last line (from 1) of the
        first place `excerpt` occurs in the transcription, compared as
        the module reads both, whole words only; None when it does not
        occur, or holds nothing to compare. Each reading of the excerpt
        is looked for in each of the transcription's, joined first."""
        readings = (_flat(excerpt)[0], _flat(excerpt, keep_hyphen=True)[0])
        for wanted in dict.fromkeys(readings):
            if not wanted:
                continue
            for flat, numbers in self._readings:
                start = _whole(flat, wanted)
                if start is not None:
                    return numbers[start], numbers[start + len(wanted) - 1]
        return None

    def locate(self, excerpt: str) -> int | None:
        """The number of the line (from 1) on which `excerpt` first
        occurs in the transcription (`span`); None when it does not."""
        found = self.span(excerpt)
        return None if found is None else found[0]

    def nearest(self, excerpt: str) -> int | None:
        """The number of the line that shares the most of the excerpt's
        folded words, the first of those that share as many; None when no
        line shares one. It names the line an excerpt that does not occur
        was most likely meant to quote, for a reader to compare."""
        wanted = set(words(excerpt))
        best, best_shared = None, 0
        for number, line in enumerate(self._lines, start=1):
            shared = len(wanted & set(words(line)))
            if shared > best_shared:
                best, best_shared = number, shared
        return best
