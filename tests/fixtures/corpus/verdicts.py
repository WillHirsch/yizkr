"""The verdicts the recorded engine gives when clustering's adjudication
asks of the fixture corpus's pairs (#587).

Clustering asks the model of each ambiguous pair it cannot decide
(yizkr.pipeline.adjudication): people first, then unions, then events,
each group once the one before has merged, so a pair a merge made
confident is never asked (the weddings of the marriages adjudication
merges, here). A faithful model answers each from the evidence the
prompt gives, and the corpus's evidence decides every pair it is asked,
so each has a `same` or `different` verdict recorded here, with its
one-sentence reason. A pair is keyed by its two mentions, each as the
prompt names it: the document it is in (by file name; the prompt gives
the SHA-1 of its bytes) and its label. A pair the engine is asked that has no
verdict here fails the test that asked it, and
tests/unit/test_fixture_corpus.py holds every verdict to the universe
(`same` exactly when its two mentions are one entity of the universe)
and asks that every one of them is asked.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.fixtures.corpus import DECK, EXPORT, LETTER, NARRATIVE

SAME = "same"
DIFFERENT = "different"

# a mention as the adjudication prompt names it: (document, label)
Mention = tuple[str, str]


@dataclass(frozen=True)
class Verdict:
    """A recorded verdict on a pair of mentions, either way round, and
    its reason."""

    first: Mention
    second: Mention
    verdict: str
    reason: str

    @property
    def pair(self) -> frozenset[Mention]:
        return frozenset((self.first, self.second))


GUSTAV_AND_FLORENCE = "Gustave Lobig and Florence Faxridge"
GUSTAV_AND_FLORENCE_EXPORT = "Gustav Löbig and Florence Faxridge"

VERDICTS = (
    # people
    Verdict(
        (LETTER, "Klara Löbig"),
        (EXPORT, "Klara Löbig"),
        SAME,
        "The postcard writes to Klara Löbig, whose father writes it, and "
        "the export's Klara Löbig is Sebastian's daughter.",
    ),
    Verdict(
        (NARRATIVE, "Nicholas Lobig"),
        (EXPORT, "Nicholas Lobig"),
        SAME,
        "Both are Gustave/Gustav Lobig's son Nicholas; a birth in 1850 to "
        "a father who died in 1996 cannot be right.",
    ),
    Verdict(
        (LETTER, "Gustl"),
        (EXPORT, "Gustav Löbig"),
        SAME,
        "The export gives Gustav Löbig the nickname Gustl, and nothing the "
        "postcard says of its Gustl tells against him.",
    ),
    # unions
    Verdict(
        (DECK, f"Marriage of {GUSTAV_AND_FLORENCE}"),
        (NARRATIVE, f"Marriage of {GUSTAV_AND_FLORENCE}"),
        SAME,
        "One couple, married in 1942 by both accounts.",
    ),
    Verdict(
        (DECK, f"Marriage of {GUSTAV_AND_FLORENCE}"),
        (EXPORT, f"Marriage of {GUSTAV_AND_FLORENCE_EXPORT}"),
        SAME,
        "One couple, married in 1942 by both accounts.",
    ),
    Verdict(
        (NARRATIVE, f"Marriage of {GUSTAV_AND_FLORENCE}"),
        (EXPORT, f"Marriage of {GUSTAV_AND_FLORENCE_EXPORT}"),
        SAME,
        "One couple, married in 1942 by both accounts.",
    ),
    # events
    Verdict(
        (DECK, "Emigration of Klara Löbig"),
        (LETTER, "Emigration of Klara Löbig"),
        SAME,
        "Both are the emigration of Klara Löbig as its principal, and "
        "nothing either source gives tells against one event.",
    ),
    Verdict(
        (DECK, "Emigration of Klara Löbig"),
        (NARRATIVE, "Emigration of Klara Löbig"),
        SAME,
        "Both are the emigration of Klara Löbig as its principal, and "
        "nothing either source gives tells against one event.",
    ),
    Verdict(
        (LETTER, "Emigration of Klara Löbig"),
        (NARRATIVE, "Emigration of Klara Löbig"),
        SAME,
        "Both are the emigration of Klara Löbig as its principal, and "
        "nothing either source gives tells against one event.",
    ),
)

# each recorded verdict by its pair
BY_PAIR = {entry.pair: entry for entry in VERDICTS}
