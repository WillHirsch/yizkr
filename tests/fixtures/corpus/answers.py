"""What the recorded engine answers for each document of the fixture corpus.

A faithful model's answers, recorded from the example universe itself: a
document's claims are the assertions the universe records for its source
(tests/fixtures/example_universe.py), each citing its excerpt and the page
or slide it is on, in the form the extraction prompts ask for (#407): an
event's participant with its role, and a spouse assertion as a marriage
bounded by its wedding (`answered_as`, ADR-0036). The only thing written
here by hand is each document's entity manifest: the entities as the
document names them, each with the universe entity it is (`truth`) and
its mention record (ADR-0035 §2); a marriage and its wedding have keys of
their own. One answer is deliberately wrong,
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
from tests.fixtures.corpus import (
    CERTIFICATE,
    DEATH_CERTIFICATE,
    DECK,
    LETTER,
    NARRATIVE,
)

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
    # the mention record the recorded model gives (ADR-0035 §2), for a
    # person, a place, an organisation or a family
    structure: dict | None = None
    # for a marriage or its wedding, the universe keys of the couple
    couple: tuple[str, str] | None = None


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
        "born in 1850, 92 years before his father married in 1942",
    ),
)


def given(written: str, standard: str | None = None) -> dict:
    """A given name as written, with the standard form the document
    supports; a full name is its own."""
    name = {"written": written}
    return name | {"standard": standard} if standard else name


def person(*names: dict, surnames=(), nicknames=(), status="name") -> dict:
    """A person's mention record: given names (`given`), surnames, each
    untyped unless the document marks it, and nicknames."""
    record: dict = {"kind": "human", "status": status}
    if names:
        record["given"] = list(names)
    if surnames:
        record["surnames"] = [{"written": name} for name in surnames]
    if nicknames:
        record["nicknames"] = list(nicknames)
    return record


def place(*names: str) -> dict:
    """A place's mention record: its names, each its own toponym."""
    return {
        "kind": "place",
        "status": "toponym",
        "names": [{"written": n, "toponym": n} for n in names],
    }


def address(
    written: str, street: str, number: str, *within: str, evidence: str = ""
) -> dict:
    """An address's mention record: the address as written, its street
    and its number, and the places the label puts it in, in its order,
    each with `evidence`, the document's words that state it (ADR-0035
    §2.2, #569)."""
    record: dict = {
        "kind": "place",
        "status": "toponym",
        "address": {"written": written, "street": street, "number": number},
    }
    if within:
        record["within"] = [
            {"name": name, "evidence": evidence} for name in within
        ]
    return record


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
                structure=person(
                    given("Gustave", "Gustave"),
                    given("Gustav", "Gustav"),
                    surnames=("Lobig", "Löbig"),
                ),
            ),
            Entity(
                "e2",
                "8 Hawthorn Road, Stupfield, Victoria",
                "place",
                "hawthorn-road-8",
                structure=address(
                    "8 Hawthorn Road",
                    "Hawthorn Road",
                    "8",
                    "Stupfield",
                    "Victoria",
                    evidence="8 Hawthorn Road, Stupfield, Victoria",
                ),
            ),
            Entity(
                "e3",
                "Floerburg",
                "place",
                "floerburg",
                structure=place("Floerburg"),
            ),
            Entity(
                "e4",
                "Australia",
                "place",
                "australia",
                structure=place("Australia"),
            ),
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
                structure=person(
                    given("Klara", "Klara"),
                    surnames=("Löbig",),
                    nicknames=("Pünktchen",),
                ),
            ),
            # the writer, whom the postcard names only by its own words
            # for him: his label is those words, with the status kinship
            # (ADR-0035 §2.1)
            Entity(
                "e2",
                "Dein Vater",
                "human",
                "sebastian-loebig",
                summary="the sender, Klara's father",
                structure=person(status="kinship"),
            ),
            # the postcard never spells the full form, so it gives none
            Entity(
                "e3",
                "Gustl",
                "human",
                "gustav-loebig",
                summary="waiting for his visa",
                structure=person(given("Gustl")),
            ),
            Entity(
                "e4",
                "31 Juniper Street, Stupfield, Victoria, Australien",
                "place",
                "juniper-street-31",
                structure=address(
                    "31 Juniper Street",
                    "Juniper Street",
                    "31",
                    "Stupfield",
                    "Victoria",
                    "Australien",
                    evidence=(
                        "31 Juniper Street, Stupfield, Victoria, Australien"
                    ),
                ),
            ),
            Entity(
                "e5",
                "Flörburg",
                "place",
                "floerburg",
                structure=place("Flörburg"),
            ),
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
    DEATH_CERTIFICATE: Recorded(
        DEATH_CERTIFICATE,
        "death-certificate-1963",
        "en",
        (
            Entity(
                "e1",
                "Florence Lobig",
                "human",
                "florence-lobig",
                aliases=("Florence Faxridge",),
                summary="died 3 July 1963 at Stupfield",
                mentions=("Name:",),
                structure=person(
                    given("Florence", "Florence"),
                    surnames=("Lobig", "Faxridge"),
                ),
            ),
            # her mother, whom the certificate names by Florence's own
            # birth name (the informant's slip): a name like hers, and
            # her kin
            Entity(
                "e2",
                "Florence Faxridge",
                "human",
                "sipkje-faxridge",
                summary="the deceased's mother",
                mentions=("Mother:",),
                structure=person(
                    given("Florence", "Florence"), surnames=("Faxridge",)
                ),
            ),
            Entity(
                "e3",
                "Stupfield",
                "place",
                "stupfield",
                structure=place("Stupfield"),
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
                structure=person(given("Eugen", "Eugen"), surnames=("Löbig",)),
            ),
            Entity(
                "e2",
                "Sebastian Löbig",
                "human",
                "sebastian-loebig",
                aliases=("Wastl",),
                summary="Eugen's son; born 1862; hatter; married 1905",
                mentions=("Sebastian", "Wastl"),
                structure=person(
                    given("Sebastian", "Sebastian"),
                    surnames=("Löbig",),
                    nicknames=("Wastl",),
                ),
            ),
            Entity(
                "e3",
                "Hildegard Laudler",
                "human",
                "hildegard-loebig",
                aliases=("Hilde",),
                summary="married Wastl in 1905",
                structure=person(
                    given("Hildegard", "Hildegard"),
                    surnames=("Laudler",),
                    nicknames=("Hilde",),
                ),
            ),
            Entity(
                "e4",
                "Ursula Löbig",
                "human",
                "ursula-loebig",
                summary="Wastl and Hilde's eldest; born 1911, died 1941",
                mentions=("Ursula,", "the eldest"),
                structure=person(
                    given("Ursula", "Ursula"), surnames=("Löbig",)
                ),
            ),
            Entity(
                "e5",
                "Klara Löbig",
                "human",
                "klara-loebig",
                aliases=("Pünktchen",),
                summary="Wastl and Hilde's daughter; born 1917; went first",
                mentions=("Klara",),
                structure=person(
                    given("Klara", "Klara"),
                    surnames=("Löbig",),
                    nicknames=("Pünktchen",),
                ),
            ),
            # "Gustav, the youngest, was Gustl at home" and "became
            # Gustave Lobig here, Gus to everyone": the document supports
            # each diminutive's full form
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
                structure=person(
                    given("Gustave", "Gustave"),
                    given("Gustl", "Gustav"),
                    given("Gus", "Gustave"),
                    surnames=("Lobig",),
                ),
            ),
            Entity(
                "e7",
                "Ursula Löbig",
                "human",
                "ursula-loebig-angerring",
                summary="a widow at Angerring 57 in 1912; no known relation",
                mentions=("a widow",),
                structure=person(
                    given("Ursula", "Ursula"), surnames=("Löbig",)
                ),
            ),
            Entity(
                "e8",
                "Florence Faxridge",
                "human",
                "florence-lobig",
                summary="married Gus in 1942",
                structure=person(
                    given("Florence", "Florence"), surnames=("Faxridge",)
                ),
            ),
            Entity(
                "e9",
                "Nicholas Lobig",
                "human",
                "nicholas-lobig",
                summary="the writer; Gus's son; born in '50",
                mentions=("Nicholas", "my father", "I was born"),
                structure=person(
                    given("Nicholas", "Nicholas"), surnames=("Lobig",)
                ),
            ),
            Entity(
                "e10",
                "Flörburg",
                "place",
                "floerburg",
                mentions=("Flörburg",),
                structure=place("Flörburg"),
            ),
            Entity("e11", "Hesse", "place", "hesse", structure=place("Hesse")),
            Entity(
                "e12",
                "Angerring 57",
                "place",
                "angerring-57",
                structure=address("Angerring 57", "Angerring", "57"),
            ),
            Entity(
                "e13",
                "Stupfield",
                "place",
                "stupfield",
                structure=place("Stupfield"),
            ),
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
            Entity(
                "e18",
                "Marriage of Sebastian Löbig and Hildegard Laudler",
                "marriage",
                "marriage-sebastian-hildegard",
                mentions=("Wastl married",),
                couple=("sebastian-loebig", "hildegard-loebig"),
            ),
            Entity(
                "e19",
                "Wedding of Sebastian Löbig and Hildegard Laudler",
                "wedding",
                "wedding-sebastian-hildegard",
                mentions=("Wastl married",),
                couple=("sebastian-loebig", "hildegard-loebig"),
            ),
            Entity(
                "e20",
                "Marriage of Gustave Lobig and Florence Faxridge",
                "marriage",
                "marriage-gustav-florence",
                mentions=("Dad married",),
                couple=("gustav-loebig", "florence-lobig"),
            ),
            Entity(
                "e21",
                "Wedding of Gustave Lobig and Florence Faxridge",
                "wedding",
                "wedding-gustav-florence",
                mentions=("Dad married",),
                couple=("gustav-loebig", "florence-lobig"),
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
                structure=person(
                    given("Sebastian", "Sebastian"),
                    surnames=("Löbig",),
                    nicknames=("Wastl",),
                ),
            ),
            Entity(
                "e2",
                "Hildegard Laudler",
                "human",
                "hildegard-loebig",
                aliases=("Hilde",),
                summary="Hilde, 1876–1968; married Wastl",
                structure=person(
                    given("Hildegard", "Hildegard"),
                    surnames=("Laudler",),
                    nicknames=("Hilde",),
                ),
            ),
            Entity(
                "e3",
                "Ursula Löbig",
                "human",
                "ursula-loebig",
                summary="a child of Wastl and Hilde",
                mentions=("Ursula",),
                structure=person(
                    given("Ursula", "Ursula"), surnames=("Löbig",)
                ),
            ),
            Entity(
                "e4",
                "Klara Löbig",
                "human",
                "klara-loebig",
                aliases=("Pünktchen",),
                summary="a child of Wastl and Hilde; came first",
                mentions=("Klara",),
                structure=person(
                    given("Klara", "Klara"),
                    surnames=("Löbig",),
                    nicknames=("Pünktchen",),
                ),
            ),
            # "Gustav 'Gustl'" and "Gustave 'Gus' Lobig": the deck gives
            # each diminutive's full form
            Entity(
                "e5",
                "Gustave Lobig",
                "human",
                "gustav-loebig",
                aliases=("Gustl", "Gus"),
                summary="a child of Wastl and Hilde; came in 1939",
                mentions=("Gustl", "Gustave"),
                structure=person(
                    given("Gustave", "Gustave"),
                    given("Gustl", "Gustav"),
                    given("Gus", "Gustave"),
                    surnames=("Lobig",),
                ),
            ),
            Entity(
                "e6",
                "Florence Faxridge",
                "human",
                "florence-lobig",
                summary="married Gus in 1942",
                structure=person(
                    given("Florence", "Florence"), surnames=("Faxridge",)
                ),
            ),
            Entity(
                "e7",
                "Nicholas Lobig",
                "human",
                "nicholas-lobig",
                summary="the speaker; Gus and Florence's son, born 1950",
                mentions=("Nicholas",),
                structure=person(
                    given("Nicholas", "Nicholas"), surnames=("Lobig",)
                ),
            ),
            Entity(
                "e8",
                "Stupfield",
                "place",
                "stupfield",
                structure=place("Stupfield"),
            ),
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
            # "=" marks the couple married, and dates nothing: a marriage
            # with no wedding
            Entity(
                "e11",
                "Marriage of Sebastian Löbig and Hildegard Laudler",
                "marriage",
                "marriage-sebastian-hildegard",
                mentions=("= Hildegard",),
                couple=("sebastian-loebig", "hildegard-loebig"),
            ),
            Entity(
                "e12",
                "Marriage of Gustave Lobig and Florence Faxridge",
                "marriage",
                "marriage-gustav-florence",
                mentions=("= Florence",),
                couple=("gustav-loebig", "florence-lobig"),
            ),
            Entity(
                "e13",
                "Wedding of Gustave Lobig and Florence Faxridge",
                "wedding",
                "wedding-gustav-florence",
                mentions=("= Florence",),
                couple=("gustav-loebig", "florence-lobig"),
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
            | ({"structure": entity.structure} if entity.structure else {})
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
        entities = {entity.truth: entity for entity in recorded.entities}
        if value not in entities:
            # a vocabulary item, such as a participant's role
            return {"type": "item", "value": {"kind": "vocab", "key": value}}
        entity = entities[value]
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


# the universe's property for a couple, which the prompts no longer
# offer: the recorded model states a marriage as a union bounded by events
# instead (ADR-0036)
SPOUSE = "spouse"
# the kinds whose participants take part in them as the couple
UNION_KINDS = ("marriage", "partnership")


def union_of(recorded: Recorded, couple: set[str], kind: str) -> Entity:
    """The document's entity of `kind` (a marriage or its wedding) that
    joins the couple."""
    (found,) = [
        entity
        for entity in recorded.entities
        if entity.kind == kind and set(entity.couple or ()) == couple
    ]
    return found


def as_unions(recorded: Recorded, assertion: u.Assertion) -> list[tuple]:
    """A spouse assertion of the universe as the recorded model answers
    it, (subject key, property, value, qualifiers): each of the couple a
    participant of their marriage with the role spouse, and, where the
    source dates the marriage's start, the marriage started by its
    wedding, which the date dates (ADR-0036)."""
    couple = {assertion.subject, assertion.value}
    marriage = union_of(recorded, couple, "marriage")
    stated = [
        (marriage.truth, "participant", who, (("object_has_role", SPOUSE),))
        for who in (assertion.subject, assertion.value)
    ]
    for prop, when in assertion.qualifiers:
        if prop == "start_time":
            wedding = union_of(recorded, couple, "wedding")
            stated += [
                (marriage.truth, "started_by", wedding.truth, ()),
                (wedding.truth, "point_in_time", when, ()),
            ]
    return stated


def answered_as(recorded: Recorded, assertion: u.Assertion) -> list[tuple]:
    """What the recorded model answers for one assertion, as (subject key,
    property, value, qualifiers): a spouse assertion as its union
    (as_unions), and an event's participant with the role principal, the
    person the event happens to (ADR-0035 §3.2); any other as it is."""
    if assertion.property == SPOUSE:
        return as_unions(recorded, assertion)
    qualifiers = assertion.qualifiers
    if assertion.property == "participant":
        qualifiers = (*qualifiers, ("object_has_role", "principal"))
    return [
        (assertion.subject, assertion.property, assertion.value, qualifiers)
    ]


def claims(
    recorded: Recorded, sha1: str, *, sharper: bool = False
) -> list[dict]:
    """Every claim the recorded model answers for the document, whose
    SHA-1 is `sha1`: those of each assertion (answered_as), with its
    mistakes unless `sharper` (the chunk prompt carries each mistake's
    rule)."""
    ids = recorded.ids()
    by_id = {entity.id: entity for entity in recorded.entities}
    answered = []
    for assertion in said(recorded):
        for key, prop, value, qualifiers in answered_as(recorded, assertion):
            for mistake in MISTAKES:
                if (
                    not sharper
                    and (mistake.document, mistake.subject, mistake.property)
                    == (recorded.document, key, prop)
                    and value == mistake.says
                ):
                    value = mistake.read_as
            subject = by_id[ids[key]]
            answered.append(
                {
                    "subject": {
                        "kind": "local",
                        "local_id": subject.id,
                        "label": subject.label,
                    },
                    "property": prop,
                    "value": _value(recorded, prop, value, recorded.language),
                    "qualifiers": [
                        {
                            "property": qualifier,
                            "value": _value(
                                recorded, qualifier, held, recorded.language
                            ),
                        }
                        for qualifier, held in qualifiers
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
