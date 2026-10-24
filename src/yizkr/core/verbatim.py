"""How an excerpt a model quotes is compared with the transcription it
quotes from (ADR-0035 §5).

An excerpt is verbatim when it occurs in the transcription with both
read the same way: folded (Unicode NFC, case folded, accents dropped),
typographic quotes, apostrophes and dashes read as their plain forms,
every run of whitespace, line breaks among it, made one space, and a
hyphen at a line's end joined to the next line's word, so "Drake-" on one
line and "radiel" on the next read "Drakeradiel". A hyphenated name the
line breaks at its own hyphen ("Mockburg-" then "Süd") is read a second
way, the hyphen kept ("Mockburg-Süd"), which is tried when the joined
reading holds no match, and only for an excerpt that crosses the hyphen:
an excerpt that stops at the hyphen ("gemeente Drake", where a line
breaks "Drake-" from "radiel") or starts after it ("radiel, Fryslân")
quotes part of a word, and is no match. An excerpt matches whole words
only: it may not start or end inside a word of the transcription.
Nothing else is
forgiven: a word the transcription does not hold, or holds in another
order, is no match. A mention record's container evidence is checked
this way, and so is each claim's excerpt (pipeline.chunking.excerpt_audit);
the critic is shown each citation at every place it occurs
(Transcript.spans, pipeline.critique), and the place stage cites a
container's evidence by the page its first line lies on
(Transcript.page, pipeline.places.EvidencePages).
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


def _flat(
    text: str, keep_hyphen: bool = False
) -> tuple[str, list[int], frozenset[int]]:
    """The text as it is compared (the module's reading); for each of its
    characters the number of the line (from 1) it comes from; and the
    indexes of the hyphens at a line's end it keeps. With `keep_hyphen`,
    a hyphen at a line's end stays, with no space after it, instead of
    being joined away."""
    chars: list[str] = []
    numbers: list[int] = []
    kept: set[int] = set()
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
                if keep_hyphen:
                    kept.add(len(chars) - 1)
                else:
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
    return "".join(chars), numbers, frozenset(kept)


def _wholes(flat: str, wanted: str, kept: frozenset[int] = frozenset()):
    """Each index, in order, at which `wanted` occurs in `flat` neither
    starting nor ending inside a word. Where `flat` keeps hyphens at a
    line's end (`kept`, their indexes), a match must cross one, and may
    neither start just after nor end just before one: elsewhere the
    reading that joins them decides."""
    start = flat.find(wanted)
    while start >= 0:
        end = start + len(wanted)
        if kept and (
            not any(start < j < end - 1 for j in kept)
            or start - 1 in kept
            or end in kept
        ):
            start = flat.find(wanted, start + 1)
            continue
        opens = (
            not wanted[0].isalnum() or not flat[start - 1 : start].isalnum()
        )
        closes = not wanted[-1].isalnum() or not flat[end : end + 1].isalnum()
        if opens and closes:
            yield start
        start = flat.find(wanted, start + 1)


def joined(text: str) -> str:
    """`text` with each hyphen at a line's end joined to the next line's
    word, as the comparison reads it."""
    return _LINE_END_HYPHEN.sub("", text)


def words(text: str) -> list[str]:
    """The folded words of `text`, a hyphen at a line's end joined."""
    return _WORDS.findall(fold(joined(text)))


def word_readings(text: str) -> list[list[str]]:
    """The folded words of `text` read both ways: a hyphen at a line's
    end joined, and kept, as for a hyphenated name a line breaks at its
    own hyphen."""
    return [words(text), _WORDS.findall(fold(text))]


class Transcript:
    """A transcription as excerpts are compared with it."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._lines = text.splitlines()
        # where each line opens in `text`, worked out on the first `page`
        self._offsets: list[int] | None = None
        # the joined reading first, then the one that keeps a hyphen at a
        # line's end
        self._readings = [_flat(text), _flat(text, keep_hyphen=True)]
        # each line's folded words, read once, on the first `nearest`
        self._line_words: list[frozenset[str]] | None = None

    def line(self, number: int) -> str:
        """The transcription's line `number` (from 1) as written; "" for
        one it does not have."""
        if 1 <= number <= len(self._lines):
            return self._lines[number - 1]
        return ""

    def spans(self, excerpt: str) -> list[tuple[int, int]]:
        """The numbers of the first and the last line (from 1) of each
        place `excerpt` occurs in the transcription, in order, compared
        as the module reads both, whole words only, each pair once;
        empty when it does not occur, or holds nothing to compare. Each
        reading of the excerpt is looked for in each of the
        transcription's, joined first, and the first that finds it gives
        every place."""
        readings = (_flat(excerpt)[0], _flat(excerpt, keep_hyphen=True)[0])
        for wanted in dict.fromkeys(readings):
            if not wanted:
                continue
            for flat, numbers, kept in self._readings:
                found = dict.fromkeys(
                    (numbers[start], numbers[start + len(wanted) - 1])
                    for start in _wholes(flat, wanted, kept)
                )
                if found:
                    return list(found)
        return []

    def span(self, excerpt: str) -> tuple[int, int] | None:
        """The first place `excerpt` occurs in the transcription
        (`spans`); None when it does not."""
        found = self.spans(excerpt)
        return found[0] if found else None

    def page(self, number: int, pages) -> object | None:
        """The page of `pages` (each with the `start` and `end` of its
        text in the transcription, as a transcription's pages give them)
        that line `number` (from 1) opens on; None for a line before,
        between or after them, or one the transcription does not have."""
        if not 1 <= number <= len(self._lines):
            return None
        if self._offsets is None:
            self._offsets = [0]
            for line in self._text.splitlines(keepends=True):
                self._offsets.append(self._offsets[-1] + len(line))
        start = self._offsets[number - 1]
        for page in pages:
            if page.start <= start <= page.end:
                return page
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
        if self._line_words is None:
            self._line_words = [frozenset(words(line)) for line in self._lines]
        best, best_shared = None, 0
        for number, held in enumerate(self._line_words, start=1):
            shared = len(wanted & held)
            if shared > best_shared:
                best, best_shared = number, shared
        return best
