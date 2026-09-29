"""What the recorded engine answers for each document of the fixture corpus.

A faithful model's answers, recorded from the example universe itself: a
document's claims are the assertions the universe records for its source
(tests/fixtures/example_universe.py), each citing its excerpt and the page
or slide it is on, and the only thing written here by hand is each
document's entity manifest: the entities as the document names them, each
with the universe entity it is (`truth`). One answer is deliberately wrong,
as models are: the family history's "I was born in '50" is read as 1850
(MISTAKES). Under an extraction prompt that carries SHARPER_RULE the
recorded model reads it right, and the critic flags the mistake wherever a
batch holds it.

The export (a GEDCOM file) is adapted, not extracted, so it has no answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from tests.fixtures import example_universe as u
from tests.fixtures.corpus import CERTIFICATE, DECK, LETTER, NARRATIVE

from yizkr.model.schema import load_manifest

SOURCES = {source.key: source for source in u.SOURCES}

# The extraction-prompt rule a sharper prompt adds: under it the recorded
# model reads a two-digit year as the document means it. The rule is a line
# of the chunk prompt, with no brace in it.
SHARPER_RULE = (
    "- A two-digit year (as in '50) names no century: take the century "
    "from the document itself, never from the oldest date it gives."
)
CONFIDENCE = 0.9


@dataclass(frozen=True)
class Entity:
    """One entry of a document's entity manifest, as the recorded model
    gives it, and the universe entity it is (`truth`). It is mentioned on
    every line of the transcription that holds one of `mentions`, by
    default its label and aliases."""

    id: str
    label: str
    kind: str
    truth: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    mentions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recorded:
    """A document's recorded answers: the universe source it is, the
    language its transcription is in, and its entity manifest. `read` marks
    an image, whose transcription the recorded engine reads off it: its
    source's text."""

    document: str
    source: str
    language: str
    entities: tuple[Entity, ...]
    read: bool = False

    @property
    def text(self) -> str:
        """The document's transcription, as the pipeline makes it: its
        source's text (a text file's has a newline at its end, which gives
        it no line of its own)."""
        return SOURCES[self.source].text

    def ids(self) -> dict[str, str]:
        """universe key -> manifest id"""
        return {entity.truth: entity.id for entity in self.entities}


@dataclass(frozen=True)
class Mistake:
    """A claim the recorded model gets wrong: of `subject`'s `property`,
    the source says `says` and the model answers `read_as`, except under a
    chunk prompt that carries `rule`. The critic's `flag` names it wherever
    a batch holds it."""

    document: str
    subject: str
    property: str
    says: u.Date
    read_as: u.Date
    rule: str
    flag: str


MISTAKES = (
    Mistake(
        NARRATIVE,
        "nicholas-lobig",
        "date_of_birth",
        u.Date(1950),
        u.Date(1850),
        SHARPER_RULE,
        "born in 1850, ninety years before his father married in 1942",
    ),
)


RECORDED = {
    CERTIFICATE: Recorded(
        CERTIFICATE,
        "naturalisation-1952",
        "en",
        (
            Entity(
                "e1",
                "Gustave Lobig",
                "human",
                "gustav-loebig",
                aliases=("Gustav Löbig",),
                summary=(
                    "hatter of 8 Hawthorn Road, Stupfield; born 1 July 1919 "
                    "at Floerburg, Germany; arrived 1939; naturalised 1952"
                ),
                mentions=("LOBIG",),
            ),
            Entity(
                "e2",
                "8 Hawthorn Road, Stupfield, Victoria",
                "place",
                "hawthorn-road-8",
            ),
            Entity("e3", "Floerburg", "place", "floerburg"),
            Entity("e4", "Australia", "place", "australia"),
            Entity("e5", "RMS Flund", "vessel", "rms-flund"),
            Entity("e6", "hatter", "occupation_class", "hatter"),
        ),
        read=True,
    ),
    LETTER: Recorded(
        LETTER,
        "postcard-1938",
        "de",
        (
            Entity(
                "e1",
                "Klara Löbig",
                "human",
                "klara-loebig",
                aliases=("Pünktchen",),
                summary="the addressee, in Stupfield; left on 22 June",
            ),
            Entity(
                "e2",
                "Klara's father",
                "human",
                "sebastian-loebig",
                summary="the sender, who signs 'Dein Vater'",
                mentions=("Dein Vater",),
            ),
            Entity(
                "e3",
                "Gustl",
                "human",
                "gustav-loebig",
                summary="waiting for his visa",
            ),
            Entity(
                "e4",
                "31 Juniper Street, Stupfield, Victoria, Australien",
                "place",
                "juniper-street-31",
            ),
            Entity("e5", "Flörburg", "place", "floerburg"),
            Entity(
                "e6",
                "Emigration of Klara Löbig",
                "migration",
                "emigration-1938",
                mentions=("abgereist",),
            ),
        ),
        read=True,
    ),
    NARRATIVE: Recorded(
        NARRATIVE,
        "family-history-2008",
        "en",
        (
            Entity(
                "e1",
                "Eugen Löbig",
                "human",
                "eugen-loebig",
                summary="master hatter in Flörburg; Sebastian's father",
            ),
            Entity(
                "e2",
                "Sebastian Löbig",
                "human",
                "sebastian-loebig",
                aliases=("Wastl",),
                summary="Eugen's son; born 1862; hatter; married 1905",
                mentions=("Sebastian", "Wastl"),
            ),
            Entity(
                "e3",
                "Hildegard Laudler",
                "human",
                "hildegard-loebig",
                aliases=("Hilde",),
                summary="married Wastl in 1905",
            ),
            Entity(
                "e4",
                "Ursula Löbig",
                "human",
                "ursula-loebig",
                summary="Wastl and Hilde's eldest; born 1911, died 1941",
                mentions=("Ursula,", "the eldest"),
            ),
            Entity(
                "e5",
                "Klara Löbig",
                "human",
                "klara-loebig",
                aliases=("Pünktchen",),
                summary="Wastl and Hilde's daughter; born 1917; went first",
                mentions=("Klara",),
            ),
            Entity(
                "e6",
                "Gustave Lobig",
                "human",
                "gustav-loebig",
                aliases=("Gustl", "Gus"),
                summary=(
                    "Wastl and Hilde's youngest; the writer's father; came in "
                    "1939; married Florence Faxridge 1942; died '96"
                ),
                mentions=("Gustav", "Gustl", "Dad"),
            ),
            Entity(
                "e7",
                "Ursula Löbig",
                "human",
                "ursula-loebig-angerring",
                summary="a widow at Angerring 57 in 1912; no known relation",
                mentions=("a widow",),
            ),
            Entity(
                "e8",
                "Florence Faxridge",
                "human",
                "florence-lobig",
                summary="married Gus in 1942",
            ),
            Entity(
                "e9",
                "Nicholas Lobig",
                "human",
                "nicholas-lobig",
                summary="the writer; Gus's son; born in '50",
                mentions=("Nicholas", "my father", "I was born"),
            ),
            Entity(
                "e10",
                "Flörburg",
                "place",
                "floerburg",
                mentions=("Flörburg",),
            ),
            Entity("e11", "Hesse", "place", "hesse"),
            Entity("e12", "Angerring 57", "place", "angerring-57"),
            Entity("e13", "Stupfield", "place", "stupfield"),
            Entity(
                "e14",
                "master hatter",
                "occupation_class",
                "master-hatter",
            ),
            Entity(
                "e15",
                "hatter",
                "occupation_class",
                "hatter",
                mentions=("a hatter after him",),
            ),
            Entity(
                "e16",
                "Emigration of Klara Löbig",
                "migration",
                "emigration-1938",
                mentions=("Klara left",),
            ),
            Entity(
                "e17",
                "Emigration of Gustave Lobig",
                "migration",
                "emigration-1939",
                mentions=("followed her in 1939",),
            ),
        ),
    ),
    DECK: Recorded(
        DECK,
        "library-talk-2018",
        "en",
        (
            Entity(
                "e1",
                "Sebastian Löbig",
                "human",
                "sebastian-loebig",
                aliases=("Wastl",),
                summary="Wastl, 1862–1945; married Hilde",
                mentions=("Wastl",),
            ),
            Entity(
                "e2",
                "Hildegard Laudler",
                "human",
                "hildegard-loebig",
                aliases=("Hilde",),
                summary="Hilde, 1876–1968; married Wastl",
            ),
            Entity(
                "e3",
                "Ursula Löbig",
                "human",
                "ursula-loebig",
                summary="a child of Wastl and Hilde",
                mentions=("Ursula",),
            ),
            Entity(
                "e4",
                "Klara Löbig",
                "human",
                "klara-loebig",
                aliases=("Pünktchen",),
                summary="a child of Wastl and Hilde; came first",
                mentions=("Klara",),
            ),
            Entity(
                "e5",
                "Gustave Lobig",
                "human",
                "gustav-loebig",
                aliases=("Gustl", "Gus"),
                summary="a child of Wastl and Hilde; came in 1939",
                mentions=("Gustl", "Gustave"),
            ),
            Entity(
                "e6",
                "Florence Faxridge",
                "human",
                "florence-lobig",
                summary="married Gus in 1942",
            ),
            Entity(
                "e7",
                "Nicholas Lobig",
                "human",
                "nicholas-lobig",
                summary="the speaker; Gus and Florence's son, born 1950",
                mentions=("Nicholas",),
            ),
            Entity("e8", "Stupfield", "place", "stupfield"),
            Entity(
                "e9",
                "Emigration of Klara Löbig",
                "migration",
                "emigration-1938",
                mentions=("Klara came first",),
            ),
            Entity(
                "e10",
                "Emigration of Gustave Lobig",
                "migration",
                "emigration-1939",
                mentions=("Gustl in 1939",),
            ),
        ),
    ),
}


# -- the answers --------------------------------------------------------------

_PAGE = re.compile(r"^\[(?:page|Slide) (\d+)[\] ]", re.IGNORECASE)


def mention_lines(recorded: Recorded, entity: Entity) -> list[int]:
    """The lines of the document's transcription that mention `entity`."""
    names = entity.mentions or (entity.label, *entity.aliases)
    return [
        number
        for number, line in enumerate(recorded.text.splitlines(), start=1)
        if any(name in line for name in names)
    ]


def page_of(recorded: Recorded, excerpt: str) -> str:
    """The page (or slide) of the first line that holds `excerpt`: the
    last page marker at or before it, and "1" in a document with none."""
    page = "1"
    for line in recorded.text.splitlines():
        marker = _PAGE.match(line)
        if marker:
            page = marker.group(1)
        if excerpt in line:
            return page
    raise ValueError(f"{recorded.document}: no line holds {excerpt!r}")


def manifest(recorded: Recorded) -> dict:
    """The entity manifest the recorded model answers."""
    entities = []
    for entity in recorded.entities:
        lines = mention_lines(recorded, entity)
        if not lines:
            raise ValueError(f"{recorded.document}: {entity.id} is on no line")
        entities.append(
            {
                "id": entity.id,
                "label": entity.label,
                "aliases": list(entity.aliases),
                "kind": entity.kind,
                "summary": entity.summary,
                "mention_lines": lines,
                "extract": True,
            }
        )
    return {"entities": entities}


@cache
def _datatypes() -> dict[str, str]:
    vocabulary = load_manifest()
    return {key: spec.datatype for key, spec in vocabulary.properties.items()}


def _time(when: u.Date) -> dict:
    """A date a source states plainly, as a claim's time value. The corpus
    documents' dates are all plain: a qualified or a decade's date would
    need qualifiers of its own, which nothing here writes."""
    if when.qualifier or when.decade:
        raise NotImplementedError(f"a qualified date: {when}")
    value = {"type": "time", "year": when.year}
    if when.month:
        value["month"] = when.month
    if when.day:
        value["day"] = when.day
    return value


def _value(recorded: Recorded, prop: str, value, language: str) -> dict:
    """A universe value as the claim schema gives it for `prop`."""
    datatype = _datatypes()[prop]
    if datatype == "time":
        return _time(value)
    if datatype == "wikibase-item":
        entity = {entity.truth: entity for entity in recorded.entities}[value]
        return {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": entity.id,
                "label": entity.label,
            },
        }
    if datatype == "monolingualtext":
        return {
            "type": "monolingual",
            "value": {"text": value, "language": language},
        }
    if datatype == "quantity":
        return {"type": "quantity", "amount": value}
    return {"type": "string", "value": value}


def said(recorded: Recorded) -> list[u.Assertion]:
    """The universe's assertions of the document's source that a claim
    can make: those about an entity of the document, not about the
    document itself."""
    return [
        a
        for a in u.ASSERTIONS
        if a.source == recorded.source and a.subject in recorded.ids()
    ]


def claims(
    recorded: Recorded, sha1: str, *, sharper: bool = False
) -> list[dict]:
    """Every claim the recorded model answers for the document, whose
    SHA-1 is `sha1`: one per assertion, with its mistakes unless `sharper`
    (the chunk prompt carries each mistake's rule)."""
    ids = recorded.ids()
    by_id = {entity.id: entity for entity in recorded.entities}
    answered = []
    for assertion in said(recorded):
        value = assertion.value
        for mistake in MISTAKES:
            if (
                not sharper
                and (mistake.document, mistake.subject, mistake.property)
                == (recorded.document, assertion.subject, assertion.property)
                and value == mistake.says
            ):
                value = mistake.read_as
        subject = by_id[ids[assertion.subject]]
        answered.append(
            {
                "subject": {
                    "kind": "local",
                    "local_id": subject.id,
                    "label": subject.label,
                },
                "property": assertion.property,
                "value": _value(
                    recorded, assertion.property, value, recorded.language
                ),
                "qualifiers": [
                    {
                        "property": prop,
                        "value": _value(
                            recorded, prop, qualifier, recorded.language
                        ),
                    }
                    for prop, qualifier in assertion.qualifiers
                ],
                "source": {
                    "artifact": {"kind": "artifact", "sha1": sha1},
                    "page_number": page_of(recorded, assertion.excerpt),
                    "transcription_excerpt": {
                        "text": assertion.excerpt,
                        "language": recorded.language,
                    },
                },
                "confidence": CONFIDENCE,
            }
        )
    return answered


def mistakes_in(recorded: Recorded) -> list[Mistake]:
    return [m for m in MISTAKES if m.document == recorded.document]
