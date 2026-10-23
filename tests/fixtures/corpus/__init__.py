"""The fixture corpus (#76): six documents of the fictional universe, for
offline end-to-end tests, and the traps they carry (docs/EXAMPLES.md, "The
fixture corpus").

The documents are in `documents/` (FOLDER), made by `build.py`:

* NARRATIVE, a two-page family history (a text file, family-history-2008);
* DECK, a three-slide talk with a photograph (a .pptx, library-talk-2018);
* EXPORT, a small GEDCOM export of the Lobig line (a slice of
  tests/fixtures/example-family.ged, with two traps of its own);
* CERTIFICATE, the naturalisation certificate (an image,
  naturalisation-1952);
* LETTER, the 1938 postcard, a short German letter (an image,
  postcard-1938);
* DEATH_CERTIFICATE, Florence's death certificate (an image,
  death-certificate-1963), whose informant gave her own birth name as
  her mother's (#425).

Each is one kind of document the scratch driver plans: a text file, a
deck, a GEDCOM file and three images. `engine.RecordedEngine` answers their
calls from their recorded answers (`answers.py`), which carry one model
mistake, and `tests/unit/test_fixture_corpus.py` runs a whole scratch run
over them.

TRAPS names the patterns the corpus sets for later stages to meet, and
PRIVACY_TRAPS the evidence the privacy traps turn on, in the universe's
terms, for the possibly-living predicate's tests (#120) to build on:

    from tests.fixtures.corpus import PRIVACY_TRAPS
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tests.fixtures import example_universe as u

FOLDER = Path(__file__).parent / "documents"

NARRATIVE = "family-history-2008.txt"
DECK = "library-talk-2018.pptx"
EXPORT = "lobig-family.ged"
CERTIFICATE = "naturalisation-1952.png"
LETTER = "postcard-1938.png"
DEATH_CERTIFICATE = "death-certificate-1963.png"

__all__ = [
    "CERTIFICATE",
    "DEATH_CERTIFICATE",
    "DECK",
    "DOCUMENTS",
    "EXPORT",
    "FOLDER",
    "LETTER",
    "NARRATIVE",
    "PRIVACY_TRAPS",
    "TRAPS",
    "Document",
    "Evidence",
    "PrivacyTrap",
    "Trap",
    "copy_to",
]


@dataclass(frozen=True)
class Document:
    """A document of the corpus: its file name, the kind the scratch driver
    plans it as, and the universe source it is (None for the export, which
    renders the universe's family rather than being one of its
    sources)."""

    name: str
    kind: str
    source: str | None


DOCUMENTS = {
    document.name: document
    for document in (
        Document(NARRATIVE, "text", "family-history-2008"),
        Document(DECK, "presentation", "library-talk-2018"),
        Document(EXPORT, "gedcom", None),
        Document(CERTIFICATE, "media", "naturalisation-1952"),
        Document(LETTER, "media", "postcard-1938"),
        Document(DEATH_CERTIFICATE, "media", "death-certificate-1963"),
    )
}


def copy_to(folder: Path) -> Path:
    """Copy every document of the corpus into `folder`, made if need be;
    returns it."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in DOCUMENTS:
        shutil.copyfile(FOLDER / name, folder / name)
    return folder


@dataclass(frozen=True)
class Trap:
    """A pattern the corpus sets: its name, the words #76 gives it, the
    documents and people (universe keys) it involves, how the corpus sets
    it, and what must come of it."""

    name: str
    title: str
    documents: tuple[str, ...]
    people: tuple[str, ...]
    how: str
    expected: str


TRAPS = (
    Trap(
        "homonyms",
        "two homonyms with identical source descriptions",
        (EXPORT, NARRATIVE),
        ("ursula-loebig", "ursula-loebig-angerring"),
        "Sebastian's daughter and the widow of the 1912 address book are "
        "both Ursula Löbig. The export gives each a record alike (one name, "
        "one sex, no date), so the adapter describes them alike, by label, "
        "kind and summary, and only their links tell them apart: the "
        "daughter's family, the widow's residence at Angerring 57. The "
        "family history names both, one line apart.",
        "Two entities in each document, each with claims of its own. "
        "Clustering never merges them (#105), and the writer mints two "
        "items (#86, #109).",
    ),
    Trap(
        "contradiction",
        "a cross-source contradiction",
        (EXPORT, CERTIFICATE),
        ("gustav-loebig",),
        "Gustav's birth: 7 January 1919 in the export, as the civil "
        "register has it, and 1 July 1919 on the naturalisation "
        "certificate, day and month transposed.",
        "Both values stand, each cited to its own document (ADR-0002).",
    ),
    Trap(
        "mirrored-kinship",
        "mirrored kinship",
        (EXPORT, LETTER, NARRATIVE, DECK),
        (
            "sebastian-loebig",
            "hildegard-loebig",
            "ursula-loebig",
            "klara-loebig",
            "gustav-loebig",
        ),
        "Sebastian and Hildegard's children, stated from both ends: the "
        "export gives each child a father and a mother, and the postcard's "
        "writer signs as Klara's father ('Dein Vater'), while the family "
        "history and the talk give the parents their children (`child`).",
        "Each document's claims state the kinship the way it does, from the "
        "child's end or the parents'; clustering reads the two ends as one "
        "(#105). The postcard's writer is labelled by its own words, 'Dein "
        "Vater', a kinship term and no name (ADR-0035), so he never blocks "
        "with her. Klara's candidate cluster is ambiguous, since the "
        "postcard's own mention of her links to the export on the name "
        "alone, and its confident component merges (#568): the talk's, "
        "the family history's and the export's mentions of her, whose kin "
        "agree. The postcard's own mention stays apart. The corpus's hard "
        "(kin-edge) conflict is Florence's death certificate's instead "
        "(the kin-edge trap).",
    ),
    Trap(
        "death-date",
        "a death-date trap",
        (EXPORT,),
        ("nicholas-lobig",),
        "Nicholas, the one possibly living person, has a DEAT in the export "
        "that states no death: no DATE, no PLAC and no Y, as some programs "
        "write for everyone.",
        "No death evidence for him comes of it (ADR-0020): no date, place "
        "or cause of death, no burial and no death event, and the adapter "
        "counts the DEAT among the tags it does not map.",
    ),
    Trap(
        "early-birth",
        "a hallucination-style early birth and a later conflicting birth",
        (NARRATIVE, EXPORT, DECK),
        ("nicholas-lobig",),
        "The family history says 'I was born in '50', and the recorded model "
        "reads it as 1850, a century the document does not give; the export "
        "and the talk give 1950.",
        "Both births stand, each cited to its document, and the critic flags "
        "the early one. Under a sharper extraction prompt the model reads "
        "1950. The early birth never releases him from privacy: every birth "
        "is read, the latest counts (#334), and an unconfirmed one counts "
        "for nothing (#120).",
    ),
    Trap(
        "merge-carrying-death",
        "a cross-source merge carrying death evidence",
        (CERTIFICATE, NARRATIVE, EXPORT),
        ("gustav-loebig",),
        "The naturalisation certificate's Gustave Lobig, born 1919, carries "
        "no death evidence; the family history's Gus carries his death in "
        "'96 and his burial in Stupfield, and so does the export's Gustav.",
        "The certificate's batch holds no death evidence for him: it reaches "
        "his mention there only through a merge of mentions (#105), which "
        "is no confirmed evidence (#120). The postcard's lone 'Gustl', "
        "which meets the export's Gustav only as a lone name (ADR-0035 §7) "
        "and which nothing corroborates, holds Gustav's candidate cluster "
        "ambiguous, and its confident component merges (#568): the "
        "certificate's mention with those that carry his death. The "
        "postcard's 'Gustl' stays apart.",
    ),
    Trap(
        "kin-edge",
        "a hard (kin-edge) conflict",
        (DEATH_CERTIFICATE, EXPORT, NARRATIVE, DECK),
        ("florence-lobig", "sipkje-faxridge"),
        "Florence's death certificate names her mother by Florence's own "
        "birth name, Florence Faxridge, as its informant gave it; the 1952 "
        "letter, outside the corpus, names her Sipkje. So two sources give "
        "Florence different mothers by name. Each of the certificate's two "
        "mentions, Florence and the mother it names, meets the export's "
        "Florence by name, since she is Florence Faxridge by birth; and "
        "the certificate makes one the other's mother.",
        "The closure joins the certificate's two mentions through the "
        "export's Florence, though no pair of them was ever proposed, and "
        "the impossibility filter refuses the candidate cluster: a kin "
        "edge joins two of its members, so it conflicts hard (#425). The "
        "mother never shares a cluster with any mention of Florence. A "
        "conflicting cluster never merges, its confident components "
        "included (#361, #568), so Florence's own mentions, whose links to "
        "the export are confident, stay apart with the rest of the "
        "conflicting cluster. That is expected under the rule, not a split "
        "to mend here (#380).",
    ),
)


@dataclass(frozen=True)
class Evidence:
    """One claim a privacy trap turns on, as a document of the corpus gives
    it: whose, which property, its value (a date, or the universe key of a
    place), and whether it is the recorded model's mistake rather than what
    the document says."""

    person: str
    property: str
    value: u.Date | str
    document: str
    mistaken: bool = False


@dataclass(frozen=True)
class PrivacyTrap:
    """A trap (TRAPS, by `trap`) as the possibly-living predicate meets it:
    the person, the evidence about them, and whether they are possibly
    living while no claim and no merge of mentions is confirmed, with why.
    Every claim of a scratch run is a machine's, so none is (ADR-0025)."""

    trap: str
    person: str
    evidence: tuple[Evidence, ...]
    possibly_living: bool
    why: str


PRIVACY_TRAPS = (
    PrivacyTrap(
        "death-date",
        "nicholas-lobig",
        (Evidence("nicholas-lobig", "date_of_birth", u.Date(1950), EXPORT),),
        True,
        "Born 1950, and the DEAT the export gives him states no death, so "
        "nothing about him is death evidence.",
    ),
    PrivacyTrap(
        "early-birth",
        "nicholas-lobig",
        (
            Evidence(
                "nicholas-lobig",
                "date_of_birth",
                u.Date(1850),
                NARRATIVE,
                mistaken=True,
            ),
            Evidence("nicholas-lobig", "date_of_birth", u.Date(1950), EXPORT),
            Evidence("nicholas-lobig", "date_of_birth", u.Date(1950), DECK),
        ),
        True,
        "The latest birth, 1950, is too recent to release him; the early "
        "one is a misreading, and would not release him even confirmed "
        "while the later births stand.",
    ),
    PrivacyTrap(
        "merge-carrying-death",
        "gustav-loebig",
        (
            Evidence(
                "gustav-loebig",
                "date_of_birth",
                u.Date(1919, 7, 1),
                CERTIFICATE,
            ),
            Evidence(
                "gustav-loebig", "date_of_death", u.Date(1996), NARRATIVE
            ),
            Evidence("gustav-loebig", "burial_place", "stupfield", NARRATIVE),
            Evidence("gustav-loebig", "date_of_death", u.Date(1996), EXPORT),
            Evidence("gustav-loebig", "burial_place", "stupfield", EXPORT),
        ),
        True,
        "Born 1919; his death is stated only by other documents than the "
        "certificate, and reaches its mention of him only by a merge that "
        "nobody has confirmed.",
    ),
    PrivacyTrap(
        "homonyms",
        "ursula-loebig-angerring",
        (
            Evidence(
                "ursula-loebig-angerring", "residence", "angerring-57", EXPORT
            ),
            Evidence(
                "ursula-loebig-angerring",
                "residence",
                "angerring-57",
                NARRATIVE,
            ),
        ),
        True,
        "Nothing dates the widow's birth or death, so she is possibly "
        "living; the other Ursula's death in 1941 would reach her only by a "
        "false merge.",
    ),
)
