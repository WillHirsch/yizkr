"""How an excerpt a model quotes is compared with the transcription it
quotes from (ADR-0035 §5).

An excerpt is verbatim when it occurs in the transcription with both
read the same way: folded (Unicode NFC, case folded, accents dropped),
every run of whitespace, line breaks among it, made one space, and a
hyphen at a line's end joined to the next line's word, so "Drake-" on one
line and "radiel" on the next read "Drakeradiel". Nothing else is
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


def fold(text: str) -> str:
    """Case and accents folded after Unicode normalisation."""
    decomposed = unicodedata.normalize(
        "NFD", unicodedata.normalize("NFC", text).casefold()
    )
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _flat(text: str) -> tuple[str, list[int]]:
    """The text as it is compared (the module's reading), and for each of
    its characters the number of the line (from 1) it comes from."""
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
        self._flat, self._numbers = _flat(text)

    def line(self, number: int) -> str:
        """The transcription's line `number` (from 1) as written; "" for
        one it does not have."""
        if 1 <= number <= len(self._lines):
            return self._lines[number - 1]
        return ""

    def locate(self, excerpt: str) -> int | None:
        """The number of the line (from 1) on which `excerpt` first
        occurs in the transcription, compared as the module reads both;
        None when it does not occur, or holds nothing to compare."""
        wanted, _ = _flat(excerpt)
        if not wanted:
            return None
        found = self._flat.find(wanted)
        return self._numbers[found] if found >= 0 else None

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
