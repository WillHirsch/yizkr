"""The fictional example universe (#40): the Löbig family and its world.

Every example in this project's code, comments, prompts, tests, ADRs,
issues and PRs comes from this module and nowhere else (see
``docs/EXAMPLES.md``).  It is plain importable data: people, places,
other entities (a firm, organisations, occupations, events), sources
with transcribed text, and the assertions each source makes, in the
archive's own vocabulary (manifest property ids, entity kinds and
artifact classes).

The family, in four generations:

* Eugen Löbig (1836-1909), master hatter, founds the Hutmacherei zum
  Blauen Hut in 1863 at Brunnenring 35 in Flörburg (Hesse, Germany);
  he marries Cäcilie Tarle ("Cilli") of Kredfeld.
* Their children Sebastian ("Wastl"), Felix and Rahel; Sebastian, who
  owns the firm after his father's death, marries Hildegard Laudler
  ("Hilde"), and Felix marries Ida Sörne ("Peppi").
* Felix and Ida's son Leopold is arrested in 1939, held in the court
  prison at Peiltal and released; Ida, widowed in 1931, is deported to
  Lager Klurnau in 1942 and liberated at Klurnau in 1945.  Leopold
  marries Agnes Nertler in 1947, and the Landgericht Rekau divorces
  them in 1957.
* Sebastian's children Klara ("Pünktchen") and Gustav ("Gustl")
  emigrate to Stupfield (Victoria, Australia) in 1938 and 1939 and take
  the surname Lobig; Gustav marries Florence Faxridge, and their son
  Nicholas, born 1950, is possibly living.  Nicholas calls a neighbour,
  Maisie Cutmore, "Nana Maisie", though she is no relation.
* An unrelated widow in Flörburg shares the name Ursula Löbig with
  Sebastian's daughter: the homonym.
* Felix's son Rudolf goes to the Soviet Union in 1936, marries Nina
  Sartina there in 1938 and dies there, at Koshnovka, in 1941.  In 1958
  Nina writes to Leopold, in Russian, and sends him Rudolf's death
  certificate.
* Florence's mother, Sipkje Faxridge, born van 't Nijks, came from
  Snylsjum in Friesland.  Her brother Sybren writes to her in Stupfield in
  1952, in West Frisian: their widowed mother has become Nicholas's
  great-grandmother.

Flörburg became Bad Flörburg in 1954, so it is named two ways over
time, and the town of Rekau lies in the urban district of the same
name.  Gustav's birth date is deliberately contradictory (7 January
per the civil register, 1 July per his naturalisation: day and month
transposed), and the sources carry every uncertain date form: circa,
before, after, between and decade.

Nicholas's 2011 booklet, "Our Family from Germany", is a compilation:
it sets an image of the top of a 1939 letter from Sebastian to Klara,
an artifact of its own, beside a translation made with translation
software, which misreads the address the letter gives for Rahel,
Angerring 11, as Angerring 71.  The two are witnesses of one text that
disagree (ADR-0008, as amended by #289).  The booklet numbers its pages
from its third, after the title page and the note on its translations,
so its sixth page, which sets the image, is printed as page 4: a
printed page number apart from the physical page (ADR-0037, #424).

Nicholas also typed two pages of family history in 2008, which name both
Ursula Löbigs and give his birth and his father's death in two-digit years
('50, '96), and gave a library talk from three slides in 2018. With the
naturalisation certificate, the 1938 postcard, Florence's 1963 death
certificate and a GEDCOM export of the Lobig line they make the fixture
corpus (tests/fixtures/corpus, #76). The death certificate's informant
gave Florence's own birth name as her mother's, so it names her mother
"Florence Faxridge" where the 1952 letter names Sipkje: a mother whose
name is her daughter's, which only kinship tells apart (#425).

The Russian and West Frisian sources (#387) carry the naming patterns that
extraction's mention records must handle (#385), and ``NAMING_PATTERNS``
says where each one is: a surname written first, a patronymic, a
married-name marker (one of them a suffix), a surname's masculine and
feminine forms, a name particle, widow and "the late" markers, kinship
terms with a side and a degree, an institution named by its kind and its
place, and a place named with its containers.

``GEDCOM_FIXTURE`` renders the family as a genealogy program would
export it from these sources; ``tests/unit/test_example_universe.py``
checks that every name, date, place and family link in it is one the
data here asserts.

Every specific was invented blind, by seeded random draws from broad
tables of period names, ordinary trades, large regions and syllables,
not recalled from any real record.  The languages beyond German and
English were drawn the same way, for their naming patterns:
``LANGUAGE_TABLE`` and ``LANGUAGE_SEED`` record the table and the seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GEDCOM_FIXTURE = Path(__file__).parent / "example-family.ged"

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class Date:
    """A date as a source states it: precision and qualifier included.

    ``qualifier`` is one of ``circa``, ``before``, ``after`` or
    ``between`` (which needs ``until``); ``decade`` marks "the 1890s".
    """

    year: int
    month: int | None = None
    day: int | None = None
    qualifier: str | None = None
    until: Date | None = None
    decade: bool = False

    @property
    def precision(self) -> str:
        if self.decade:
            return "decade"
        return "day" if self.day else "month" if self.month else "year"

    def _plain(self, gedcom: bool) -> str:
        month = _MONTHS[self.month - 1] if self.month else ""
        if gedcom:
            month = month[:3].upper()
        return " ".join(
            str(part) for part in (self.day, month, self.year) if part
        )

    @property
    def gedcom(self) -> str:
        """The GEDCOM 5.5.1 date phrase, e.g. ``ABT 1833``."""
        if self.decade:
            return f"BET {self.year} AND {self.year + 9}"
        plain = self._plain(gedcom=True)
        if self.qualifier == "between" and self.until:
            return f"BET {plain} AND {self.until.gedcom}"
        prefix = {"circa": "ABT ", "before": "BEF ", "after": "AFT "}
        return prefix.get(self.qualifier or "", "") + plain

    def __str__(self) -> str:
        if self.decade:
            return f"{self.year}s"
        plain = self._plain(gedcom=False)
        if self.qualifier == "between" and self.until:
            return f"between {plain} and {self.until}"
        prefix = {"circa": "c. ", "before": "before ", "after": "after "}
        return prefix.get(self.qualifier or "", "") + plain


@dataclass(frozen=True)
class Name:
    """One form of a name.

    ``form`` for people: birth, married, nickname, diminutive,
    anglicised, as_written (whole, as one source writes it), given (a
    given name alone, as a child's patronymic gives it) or placeholder;
    for places: official, historical, transliterated or translated.
    """

    text: str
    form: str
    lang: str | None = None


@dataclass(frozen=True)
class Person:
    key: str
    label: str
    sex: str | None  # "male", "female" or None (unknown)
    generation: int | None  # None: not family
    names: tuple[Name, ...]
    gedcom_xref: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Place:
    key: str
    label: str
    kind: str
    part_of: tuple[str, ...]
    names: tuple[Name, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Entity:
    """Anything that is neither a person nor a place: an organisation,
    an occupation, a vessel, or an event such as an arrest.  ``kind``
    is a manifest entity kind."""

    key: str
    label: str
    kind: str
    names: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    lang: str | None
    year: int
    artifact_class: str | None = None
    text: str = ""
    note: str = ""


@dataclass(frozen=True)
class Assertion:
    """One thing one source says, as a manifest property and value.
    ``excerpt``, when given, is the passage of the source's ``text``
    that says it."""

    subject: str
    property: str
    value: str | int | Date
    source: str
    qualifiers: tuple[tuple[str, str | Date], ...] = ()
    excerpt: str = ""


def _names(*forms: tuple[str, str, str | None]) -> tuple[Name, ...]:
    return tuple(Name(*form) for form in forms)


def _said(source: str, *rows: tuple) -> list[Assertion]:
    """Assertions made by one source.

    Each row is ``(subject, property, value)`` followed optionally by a
    qualifier dict and/or an excerpt string, in either order.
    """
    said = []
    for subject, prop, value, *rest in rows:
        qualifiers = next((r for r in rest if isinstance(r, dict)), {})
        excerpt = next((r for r in rest if isinstance(r, str)), "")
        said.append(
            Assertion(
                subject,
                prop,
                value,
                source,
                tuple(qualifiers.items()),
                excerpt,
            )
        )
    return said


PEOPLE = (
    Person(
        "eugen-loebig",
        "Eugen Löbig",
        "male",
        1,
        _names(
            ("Eugen Löbig", "birth", "de"),
            ("Muckel", "nickname", "de"),
        ),
        gedcom_xref="@I1@",
        note="Master hatter; founded the Hutmacherei zum Blauen Hut in 1863.",
    ),
    Person(
        "caecilie-loebig",
        "Cäcilie Löbig",
        "female",
        1,
        _names(
            ("Cäcilie Tarle", "birth", "de"),
            ("Cäcilie Löbig", "married", "de"),
            ("Cilli", "diminutive", "de"),
            ("Cäcilie Löbig geb. Tarle", "as_written", "de"),
        ),
        gedcom_xref="@I2@",
        note="Born in Kredfeld; the tree gives her birth year as circa.",
    ),
    Person(
        "sebastian-loebig",
        "Sebastian Löbig",
        "male",
        2,
        _names(
            ("Sebastian Löbig", "birth", "de"),
            ("Wastl", "diminutive", "de"),
        ),
        gedcom_xref="@I3@",
        note="Owned the firm by 1912, after his father's death; never left.",
    ),
    Person(
        "hildegard-loebig",
        "Hildegard Löbig",
        "female",
        2,
        _names(
            ("Hildegard Laudler", "birth", "de"),
            ("Hildegard Löbig", "married", "de"),
            ("Hilde", "diminutive", "de"),
            ("Hildegard Löbig geborene Laudler", "as_written", "de"),
        ),
        gedcom_xref="@I4@",
    ),
    Person(
        "rahel-loebig",
        "Rahel Löbig",
        "female",
        2,
        _names(("Rahel Löbig", "birth", "de")),
        gedcom_xref="@I5@",
        note="Never married; the tree says only that she died after 1945.",
    ),
    Person(
        "felix-loebig",
        "Felix Löbig",
        "male",
        2,
        _names(("Felix Löbig", "birth", "de")),
        gedcom_xref="@I6@",
        note="Stayed in Flörburg; died in 1931.",
    ),
    Person(
        "ida-loebig",
        "Ida Löbig",
        "female",
        2,
        _names(
            ("Ida Sörne", "birth", "de"),
            ("Ida Löbig", "married", "de"),
            ("Peppi", "nickname", "de"),
            ("LÖBIG, Ida, née SÖRNE", "as_written", "en"),
        ),
        gedcom_xref="@I7@",
        note="Deported to Lager Klurnau in 1942; liberated in 1945.",
    ),
    Person(
        "gustav-loebig",
        "Gustav Löbig",
        "male",
        3,
        _names(
            ("Gustav Löbig", "birth", "de"),
            ("Gustl", "diminutive", "de"),
            ("Gustave Lobig", "anglicised", "en"),
            ("Gus", "diminutive", "en"),
            ("Gustave LOBIG (formerly Gustav LÖBIG)", "as_written", "en"),
        ),
        gedcom_xref="@I8@",
        note=(
            "Emigrated to Australia in 1939.  His birth date is the "
            "deliberate contradiction."
        ),
    ),
    Person(
        "klara-loebig",
        "Klara Löbig",
        "female",
        3,
        _names(
            ("Klara Löbig", "birth", "de"),
            ("Pünktchen", "nickname", "de"),
            ("Clara Lobig", "anglicised", "en"),
        ),
        gedcom_xref="@I9@",
        note="Emigrated first, in 1938; drew the family tree in 1984.",
    ),
    Person(
        "ursula-loebig",
        "Ursula Löbig",
        "female",
        3,
        _names(("Ursula Löbig", "birth", "de")),
        gedcom_xref="@I10@",
        note="Homonym of the unrelated widow ursula-loebig-angerring.",
    ),
    Person(
        "leopold-loebig",
        "Leopold Löbig",
        "male",
        3,
        _names(("Leopold Löbig", "birth", "de")),
        gedcom_xref="@I11@",
        note=(
            "Arrested and imprisoned in 1939, then released; married "
            "Agnes Nertler in 1947 and was divorced from her in 1957."
        ),
    ),
    Person(
        "rudolf-loebig",
        "Rudolf Löbig",
        "male",
        3,
        _names(
            ("Rudolf Löbig", "birth", "de"),
            ("Rudi", "diminutive", "de"),
            ("Лёбиг Рудольф Феликсович", "as_written", "ru"),
        ),
        gedcom_xref="@I12@",
        note=(
            "Went to the Soviet Union in 1936, married Nina Sartina there "
            "in 1938 and died there in 1941."
        ),
    ),
    Person(
        "infant-loebig",
        "Infant Löbig",
        None,
        3,
        _names(("N.N. Löbig", "placeholder", None)),
        gedcom_xref="@I13@",
        note="The tree shows a child who died in 1922: no name, no sex.",
    ),
    Person(
        "florence-lobig",
        "Florence Lobig",
        "female",
        3,
        _names(
            ("Florence Faxridge", "birth", "en"),
            ("Florence Lobig", "married", "en"),
        ),
        gedcom_xref="@I14@",
        note="Her mother, Sipkje Faxridge, came from Friesland.",
    ),
    Person(
        "nicholas-lobig",
        "Nicholas Lobig",
        "male",
        4,
        _names(("Nicholas Lobig", "birth", "en")),
        gedcom_xref="@I15@",
        note=(
            "Born 1950 and no source asserts a death: possibly living, "
            "so private by default and dated to the year only."
        ),
    ),
    Person(
        "ursula-loebig-angerring",
        "Ursula Löbig",
        "female",
        None,
        _names(("Löbig, Ursula, Wwe.", "as_written", "de")),
        gedcom_xref="@I16@",
        note=(
            "A widow (Wwe.) listed in the 1912 directory; no relation "
            "to the family is known.  Homonym of ursula-loebig."
        ),
    ),
    Person(
        "agnes-loebig",
        "Agnes Löbig",
        "female",
        3,
        _names(
            ("Agnes Nertler", "birth", "de"),
            ("Agnes Löbig", "married", "de"),
            ("Agnes Löbig geb. Nertler", "as_written", "de"),
        ),
        gedcom_xref="@I17@",
        note="Leopold's wife from 1947 until their divorce in 1957.",
    ),
    Person(
        "maisie-cutmore",
        "Maisie Cutmore",
        "female",
        None,
        _names(("Mrs Cutmore", "as_written", "en")),
        note=(
            "A neighbour in Stupfield whom Nicholas called Nana Maisie: "
            "a courtesy title, not kinship."
        ),
    ),
    Person(
        "nina-sartina",
        "Nina Sartina",
        "female",
        3,
        _names(
            ("Нина Сартина", "birth", "ru"),
            ("Нина Лёбиг", "married", "ru"),
            ("Нина Васильевна Сартина", "as_written", "ru"),
        ),
        note=(
            "Rudolf's wife from 1938, in the Soviet Union, and his widow; "
            "her village calls her Рудольфиха, colloquial for Rudolf's wife."
        ),
    ),
    Person(
        "pavel-sartin",
        "Pavel Sartin",
        "male",
        3,
        _names(("Павел Сартин", "birth", "ru")),
        note="Nina's brother: his surname is the masculine form of hers.",
    ),
    Person(
        "vasily-sartin",
        "Vasily Sartin",
        "male",
        2,
        _names(("Василий", "given", "ru")),
        note=(
            "Nina's father, named only in her patronymic, Васильевна "
            "(daughter of Vasily); no source gives his surname or dates."
        ),
    ),
    Person(
        "sipkje-faxridge",
        "Sipkje Faxridge",
        "female",
        2,
        _names(
            ("Sipkje van 't Nijks", "birth", "fy"),
            ("Sipkje Faxridge", "married", "en"),
            ("S. Faxridge-van 't Nijks", "as_written", "fy"),
        ),
        note=(
            "Florence's mother, from Snylsjum in Friesland; she lived in "
            "Stupfield."
        ),
    ),
    Person(
        "sybren-van-t-nijks",
        "Sybren van 't Nijks",
        "male",
        2,
        _names(
            ("Sybren van 't Nijks", "birth", "fy"),
            ("S. van 't Nijks", "as_written", "fy"),
        ),
        note="Sipkje's brother in Snylsjum, who wrote to her in 1952.",
    ),
    Person(
        "mr-van-t-nijks",
        "Mr Van 't Nijks",
        "male",
        1,
        _names(("N.N. van 't Nijks", "placeholder", None)),
        note=(
            "Sipkje and Sybren's father, whom Sybren's letter names only "
            "as heit (father), and late; he worked at the Snylsjum "
            "brickworks."
        ),
    ),
    Person(
        "mrs-van-t-nijks",
        "Mrs Van 't Nijks",
        "female",
        1,
        _names(("N.N. van 't Nijks", "placeholder", None)),
        note=(
            "Sipkje and Sybren's mother, whom Sybren's letter names only as "
            "Mem (mother): a widow, and Nicholas's great-grandmother."
        ),
    ),
)

PLACES = (
    Place(
        "germany",
        "Germany",
        "country",
        (),
        _names(("Deutschland", "official", "de")),
    ),
    Place(
        "hesse",
        "Hesse",
        "region",
        ("germany",),
        _names(("Hessen", "official", "de")),
    ),
    Place("kreis-peiltal", "Kreis Peiltal", "district", ("hesse",)),
    Place("kreis-mockburg", "Kreis Mockburg", "district", ("hesse",)),
    Place("peiltal", "Peiltal", "town", ("kreis-peiltal",)),
    Place(
        "floerburg",
        "Flörburg",
        "town",
        ("kreis-peiltal",),
        _names(
            ("Flörburg", "historical", "de"),
            ("Bad Flörburg", "official", "de"),
            ("Floerburg", "transliterated", "en"),
        ),
        note=(
            "Renamed Bad Flörburg in 1954 on becoming a spa; the 1984 "
            "tree uses the new name even for an 1836 birth."
        ),
    ),
    Place("brunnenring", "Brunnenring", "street", ("floerburg",)),
    Place(
        "brunnenring-35",
        "Brunnenring 35",
        "address",
        ("brunnenring",),
        note="The firm's premises and the family home.",
    ),
    Place("wasserallee", "Wasserallee", "street", ("floerburg",)),
    Place("wasserallee-59", "Wasserallee 59", "address", ("wasserallee",)),
    Place("angerring", "Angerring", "street", ("floerburg",)),
    Place(
        "angerring-11",
        "Angerring 11",
        "address",
        ("angerring",),
        note="Rahel's address in July 1939, per Sebastian's letter.",
    ),
    Place("angerring-57", "Angerring 57", "address", ("angerring",)),
    Place(
        "angerring-71",
        "Angerring 71",
        "address",
        ("angerring",),
        note=(
            "Named only by the 2011 booklet's translation, which misreads "
            "the letter's Angerring 11."
        ),
    ),
    Place(
        "floerburg-cemetery",
        "Friedhof Flörburg",
        "cemetery",
        ("floerburg",),
        note="Where Eugen and Cäcilie are buried.",
    ),
    Place("kredfeld", "Kredfeld", "town", ("kreis-peiltal",)),
    Place(
        "flaeckfeld",
        "Fläckfeld",
        "town",
        ("kreis-mockburg", "kreis-peiltal"),
        note=(
            "The 1905 marriage certificate puts it in Kreis Mockburg, "
            "the 1984 tree in Kreis Peiltal."
        ),
    ),
    Place(
        "moshausen",
        "Moshausen",
        "town",
        ("germany",),
        note="No source gives its district or region.",
    ),
    Place(
        "stadtkreis-rekau",
        "Rekau",
        "district",
        ("hesse",),
        _names(("Stadtkreis Rekau", "official", "de")),
        note="An urban district of one town, which bears the same name.",
    ),
    Place(
        "rekau",
        "Rekau",
        "town",
        ("stadtkreis-rekau",),
        note="Seat of the Landgericht Rekau; nested in its namesake.",
    ),
    Place(
        "peiltal-court-prison",
        "Gerichtsgefängnis Peiltal",
        "prison",
        ("peiltal",),
        _names(
            ("Gerichtsgefängnis Peiltal", "official", "de"),
            ("Peiltal court prison", "translated", "en"),
        ),
    ),
    Place(
        "klurnau",
        "Klurnau",
        "locality",
        (),
        note="No source says where Klurnau lay: it reaches no country.",
    ),
    Place(
        "klurnau-camp",
        "Lager Klurnau",
        "camp",
        ("klurnau",),
        _names(
            ("Lager Klurnau", "official", "de"),
            ("Klurnau camp", "translated", "en"),
        ),
    ),
    Place(
        "australia",
        "Australia",
        "country",
        (),
        _names(
            ("Australia", "official", "en"),
            ("Austraalje", "translated", "fy"),
        ),
    ),
    Place("victoria", "Victoria", "region", ("australia",)),
    Place("stupfield", "Stupfield", "town", ("victoria",)),
    Place("hawthorn-road", "Hawthorn Road", "street", ("stupfield",)),
    Place("hawthorn-road-8", "8 Hawthorn Road", "address", ("hawthorn-road",)),
    Place("juniper-street", "Juniper Street", "street", ("stupfield",)),
    Place(
        "juniper-street-31",
        "31 Juniper Street",
        "address",
        ("juniper-street",),
    ),
    Place("linden-grove", "Linden Grove", "street", ("stupfield",)),
    Place(
        "linden-grove-1",
        "1 Linden Grove",
        "address",
        ("linden-grove",),
        note="Sipkje Faxridge's address in 1952.",
    ),
    Place(
        "russia",
        "Russia",
        "country",
        (),
        _names(("РСФСР", "historical", "ru")),
        note="The death certificate names it as the RSFSR, a republic.",
    ),
    Place(
        "chadsk-oblast",
        "Chadsk Oblast",
        "region",
        ("russia",),
        _names(("Чадская область", "official", "ru")),
    ),
    Place(
        "kaldovsky-district",
        "Kaldovsky District",
        "district",
        ("chadsk-oblast",),
        _names(("Калдовский район", "official", "ru")),
    ),
    Place(
        "koshnovka",
        "Koshnovka",
        "village",
        ("kaldovsky-district",),
        _names(("Кошновка", "official", "ru")),
        note="Where Rudolf lived from 1936 and died in 1941.",
    ),
    Place(
        "netherlands",
        "Netherlands",
        "country",
        (),
        _names(("Nederlân", "translated", "fy")),
    ),
    Place(
        "friesland",
        "Friesland",
        "region",
        ("netherlands",),
        _names(("Fryslân", "official", "fy")),
    ),
    Place("drakeradiel", "Drakeradiel", "municipality", ("friesland",)),
    Place(
        "snylsjum",
        "Snylsjum",
        "village",
        ("drakeradiel",),
        note="Sipkje's family's village, with its brickworks.",
    ),
)

ENTITIES = (
    Entity(
        "zum-blauen-hut",
        "Hutmacherei zum Blauen Hut",
        "organization",
        ("the Blue Hat",),
        note="The family's hat-making firm at Brunnenring 35, Flörburg.",
    ),
    Entity("hatter", "Hatter", "occupation_class", ("Hutmacher",)),
    Entity(
        "master-hatter",
        "Master hatter",
        "occupation_class",
        ("Hutmachermeister",),
    ),
    Entity(
        "arrest-1939",
        "Arrest of Leopold Löbig",
        "event",
        note="Persecution event: the arrest.",
    ),
    Entity(
        "imprisonment-1939",
        "Imprisonment of Leopold Löbig",
        "imprisonment",
        note="Persecution event: four weeks in the Peiltal court prison.",
    ),
    Entity(
        "release-1939",
        "Release of Leopold Löbig",
        "release",
        note="Persecution event: release from prison.",
    ),
    Entity(
        "deportation-1942",
        "Deportation of Ida Löbig",
        "deportation",
        note="Persecution event: from Flörburg to Lager Klurnau.",
    ),
    Entity(
        "liberation-1945",
        "Liberation of Ida Löbig",
        "release",
        note="Persecution event: liberation at Klurnau.",
    ),
    Entity("emigration-1938", "Emigration of Klara Löbig", "migration"),
    Entity("emigration-1939", "Emigration of Gustav Löbig", "migration"),
    Entity("floerburg-registry", "Standesamt Flörburg", "organization"),
    Entity(
        "realschule-floerburg",
        "Städtische Realschule Flörburg",
        "organization",
    ),
    Entity(
        "wiedergutmachungsamt-peiltal",
        "Wiedergutmachungsamt Peiltal",
        "organization",
        note="The compensation office that kept the 1953 file.",
    ),
    Entity(
        "landgericht-rekau",
        "Landgericht Rekau",
        "organization",
        ("Rekau regional court",),
        note="The regional court that divorced Leopold and Agnes in 1957.",
    ),
    Entity(
        "studio-bruesrich",
        "Photograph Brüsrich",
        "organization",
        note="Photographic studio in Peiltal.",
    ),
    Entity(
        "rms-flund",
        "RMS Flund",
        "vessel",
        note="Named in the naturalisation certificate.",
    ),
    Entity(
        "emigration-1936",
        "Emigration of Rudolf Löbig",
        "migration",
        note="To Koshnovka, in the Soviet Union.",
    ),
    Entity("fitter", "Fitter", "occupation_class", ("слесарь",)),
    Entity(
        "kaldovsky-registry",
        "Kaldovsky district civil registry",
        "organization",
        ("Калдовское районное бюро ЗАГС",),
        note=(
            "Registered Rudolf's death: named by its kind and by its "
            "district, whose name it takes as an adjective."
        ),
    ),
    Entity(
        "snylsjum-brickworks",
        "Snylsjum brickworks",
        "organization",
        ("tichelwurk fan Snylsjum",),
        note="Where Sipkje's father worked; closed by 1952.",
    ),
)

# The sources beyond German and English are in languages drawn for their
# naming patterns (#387), at random with a fixed seed from this table of
# European languages (ISO 639-1 code, name):
# random.Random(LANGUAGE_SEED).sample(LANGUAGE_TABLE, 2) draws LANGUAGES.
LANGUAGE_SEED = 387
LANGUAGE_TABLE = (
    ("sq", "Albanian"),
    ("eu", "Basque"),
    ("be", "Belarusian"),
    ("br", "Breton"),
    ("bg", "Bulgarian"),
    ("ca", "Catalan"),
    ("hr", "Croatian"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("nl", "Dutch"),
    ("et", "Estonian"),
    ("fo", "Faroese"),
    ("fi", "Finnish"),
    ("fy", "Frisian"),
    ("gl", "Galician"),
    ("el", "Greek"),
    ("hu", "Hungarian"),
    ("is", "Icelandic"),
    ("ga", "Irish"),
    ("la", "Latin"),
    ("lv", "Latvian"),
    ("lt", "Lithuanian"),
    ("mk", "Macedonian"),
    ("mt", "Maltese"),
    ("no", "Norwegian"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("gd", "Scottish Gaelic"),
    ("sr", "Serbian"),
    ("sk", "Slovak"),
    ("sl", "Slovene"),
    ("es", "Spanish"),
    ("sv", "Swedish"),
    ("uk", "Ukrainian"),
    ("cy", "Welsh"),
    ("yi", "Yiddish"),
)
LANGUAGES = ("ru", "fy")

SOURCES = (
    Source(
        "studio-photo-1890s",
        "Studio portrait of the Löbig family",
        None,
        1890,
        "photograph",
        text=(
            "[mount] Photograph Brüsrich · Peiltal "
            "[reverse] Muckel · Cilli · Wastl · Rahel · Felix"
        ),
        note="Undated; the studio mount dates it only to the 1890s.",
    ),
    Source(
        "marriage-cert-1905",
        "Marriage certificate of Sebastian Löbig and Hildegard Laudler",
        "de",
        1905,
        "certificate",
        text=(
            "Standesamt Flörburg. Flörburg, am 15. Dezember 1905. Vor dem "
            "Standesbeamten erschienen zum Zwecke der Eheschließung: der "
            "Hutmacher Sebastian Löbig, geboren am 15. Februar 1862 zu "
            "Flörburg, Sohn des Hutmachermeisters Eugen Löbig und "
            "dessen verstorbener Ehefrau Cäcilie geborene Tarle; und "
            "die Hildegard Laudler, geboren am 19. März 1876 zu Fläckfeld, "
            "Kreis Mockburg."
        ),
    ),
    Source(
        "directory-1912",
        "Adressbuch der Stadt Flörburg 1912",
        "de",
        1912,
        "address_book",
        text=(
            "Hutmacherei zum Blauen Hut, gegr. 1863, Inh. Sebastian Löbig, "
            "Brunnenring 35.\n"
            "Löbig, Ursula, Wwe., Angerring 57."
        ),
        note="Two Löbig entries on one page: the homonym.",
    ),
    Source(
        "burial-register-1909",
        "Burial register of Friedhof Flörburg: Eugen Löbig",
        "de",
        1909,
        "artifact",
        text=(
            "Friedhof Flörburg, Beerdigungsbuch 1909.\n"
            "Name: Löbig, Eugen, Hutmachermeister. Wohnung: Brunnenring "
            "35. Witwer der Cäcilie Löbig geb. Tarle. Alter: 73 Jahre.\n"
            "Gestorben: 16. März 1909 in Flörburg. Todesursache: "
            "Wassersucht.\n"
            "Beerdigt: 20. März 1909."
        ),
        note="One entry in the cemetery's register of burials.",
    ),
    Source(
        "school-report-1930",
        "School report for Rudolf Löbig",
        "de",
        1930,
        "certificate",
        text=(
            "Städtische Realschule zu Flörburg. Schuljahr 1929/30. "
            "Zeugnis für den Schüler Rudolf Löbig, geb. am 24. "
            "Oktober 1916, Sohn des Hutmachers Felix Löbig, "
            "Flörburg, Wasserallee 59."
        ),
    ),
    Source(
        "birth-extract-1938",
        "Birth register extract for Gustav Löbig",
        "de",
        1938,
        "certificate",
        text=(
            "Standesamt Flörburg. Flörburg, am 8. Januar 1919. Der "
            "Hutmacher Sebastian Löbig, wohnhaft in Flörburg, Brunnenring "
            "35, zeigte an, daß von seiner Ehefrau Hildegard Löbig "
            "geborene Laudler zu Flörburg in seiner Wohnung am siebenten "
            "Januar des Jahres tausend neunhundert und neunzehn ein "
            "Knabe geboren worden sei, welcher den Vornamen Gustav "
            "erhalten habe."
        ),
        note="Extract issued in 1938 for emigration papers.",
    ),
    Source(
        "postcard-1938",
        "Postcard from Sebastian Löbig to his daughter Klara",
        "de",
        1938,
        "letter",
        text=(
            "[postmark] Flörburg 19.8.38\n"
            "Fräulein Klara Löbig, 31 Juniper Street, Stupfield, "
            "Victoria, Australien\n"
            "Liebes Pünktchen! Seit Du am 22. Juni abgereist bist, ist "
            "es still im Haus. Gustl wartet noch auf sein Visum. Dein "
            "Vater"
        ),
        note="A postcard; only its postmark dates it.",
    ),
    Source(
        "registration-card-1945",
        "Post-war registration card for Ida Löbig",
        "en",
        1945,
        "artifact",
        text=(
            "LÖBIG, Ida, née SÖRNE. Born 17.4.1881 at Moshausen. "
            "Last address: Flörburg, Wasserallee 59. Deported 21.8.1942 "
            "to Klurnau. Liberated at Klurnau 28.1.1945. Husband: "
            "Felix LÖBIG, died 1931. Son: Leopold LÖBIG, arrested "
            "3.9.1939, held in Peiltal court prison until 1.10.1939."
        ),
    ),
    Source(
        "naturalisation-1952",
        "Certificate of naturalisation for Gustave Lobig",
        "en",
        1952,
        "certificate",
        text=(
            "Certificate of naturalisation. Gustave LOBIG (formerly "
            "Gustav LÖBIG), hatter, of 8 Hawthorn Road, Stupfield, "
            "Victoria. Born 1st July 1919 at Floerburg, Germany. "
            "Arrived in Australia 1939 per RMS Flund. Granted 1952."
        ),
        note="Its birth date transposes the register's day and month.",
    ),
    Source(
        "compensation-file-1953",
        "Compensation file for Ida Löbig",
        "de",
        1953,
        "artifact",
        text=(
            "Wiedergutmachungsamt Peiltal, 1953. Antrag des Leopold "
            "Löbig, Flörburg, für seine verstorbene Mutter Ida "
            "Löbig geb. Sörne, geb. 17.4.1881, zuletzt wohnhaft in "
            "Flörburg, Wasserallee 59. Am 21. August 1942 von Flörburg "
            "in das Lager Klurnau verschleppt, dort befreit, 1946 "
            "verstorben."
        ),
        note="A son's claim for his late mother.",
    ),
    Source(
        "gravestone-photo-1956",
        "Photograph of the Löbig gravestone, Bad Flörburg",
        "de",
        1956,
        "photograph",
        text=(
            "EUGEN LÖBIG\n1836 – 1909\nCÄCILIE LÖBIG\nGEB. TARLE\n1833 – 1902"
        ),
        note="The grave is in the Flörburg cemetery.",
    ),
    Source(
        "divorce-judgment-1957",
        "Divorce judgment for Leopold and Agnes Löbig",
        "de",
        1957,
        "artifact",
        text=(
            "Landgericht Rekau. Im Namen des Volkes! Urteil. In der "
            "Ehesache des Leopold Löbig, geboren am 29. Mai 1914, "
            "wohnhaft in Flörburg, und der Agnes Löbig geb. Nertler, "
            "geboren am 20. April 1912 zu Kredfeld, hat das Landgericht "
            "Rekau am 2. Januar 1957 für Recht erkannt: Die am 8. "
            "Februar 1947 vor dem Standesbeamten in Flörburg "
            "geschlossene Ehe der Parteien wird geschieden."
        ),
        note="The court's judgment; the day it gives is the divorce date.",
    ),
    Source(
        "family-tree-1984",
        "Family tree drawn by Clara Lobig",
        "en",
        1984,
        "genealogical_report",
        text=(
            "Eugen Löbig, b. Bad Flörburg 1836, d. 1909 = (1859) "
            "Cäcilie 'Cilli' Tarle of Kredfeld, c. 1833–1902.\n"
            "Their children: Sebastian 'Wastl' 1862–1945 = Hildegard "
            "'Hilde' Laudler of Fläckfeld (Kr. Peiltal) 1876–1968; "
            "Felix 1867–1931 = Ida 'Peppi' Sörne 1881–1946; "
            "Rahel 1871 – after 1945.\n"
            "Children of Wastl and Hilde: Ursula 1911–1941; "
            "Klara 'Pünktchen' 1917; Gustav 'Gustl' 1919 = (1942) "
            "Florence Faxridge 1914–1963.\n"
            "Children of Felix and Peppi: Leopold 1914–1960 = (1947) "
            "Agnes Nertler 1912–1974, div. 1957; "
            "Rudolf 'Rudi' 1916–1941; infant 1922.\n"
            "Children of Gustl and Florence: Nicholas 1950."
        ),
        note="Hand-drawn in Australia by the emigrant Klara.",
    ),
    Source(
        "interview-2002",
        "Oral-history interview with Nicholas Lobig",
        "en",
        2002,
        None,
        text=(
            "I was born in '50, in Stupfield. Nana Maisie next door "
            "minded me after school: Mrs Cutmore, no relation, but I "
            "always called her Nana. Dad — Gus, everyone "
            "called him — left Germany in the August, just before the "
            "war. Aunt Pünktchen had gone ahead in '38 or '39. Dad's "
            "cousin Leopold was taken the first week of the war and "
            "held about a month in Peiltal. The hat shop, the Blue Hat, "
            "was gone before 1926. Dad died in '96 and he's buried here "
            "in Stupfield. Aunt Pünktchen died last year."
        ),
        note="A possibly living narrator; family hearsay throughout.",
    ),
    Source(
        "family-booklet-2011",
        "Our Family from Germany",
        "en",
        2011,
        "artifact",
        text=(
            "[page 1] Our Family from Germany. Compiled by Nicholas Lobig. "
            "Stupfield, 2011.\n"
            "[page 2] All translations in this booklet were done with "
            "translation software; any mistakes are mine.\n"
            "[page 6] [image 1] Wastl to Klara, July 1939.\n"
            "Translation: Flörburg, Brunnenring 35, 28 July 1939. Dear "
            "Pünktchen! Gustl finally has his visa. Hilde is sewing him "
            "shirts for the journey. Rahel now lives at Angerring 71.\n"
            "Gustl was Ursula's younger brother."
        ),
        note=(
            "A compilation, holding its own text only: the image beside "
            "the translation is letter-excerpt-1939, an artifact of its own "
            "(ADR-0008). The translation misreads Rahel's Angerring 11 as "
            "71. Its pages are numbered from its third, after the title "
            "page and the note on its translations, so its physical page 6 "
            "carries the printed page number 4."
        ),
    ),
    Source(
        "letter-excerpt-1939",
        "Our Family from Germany — page 6, image 1",
        "de",
        1939,
        "photograph",
        text=(
            "Flörburg, Brunnenring 35, den 28. Juli 1939.\n"
            "Liebes Pünktchen! Gustl hat endlich sein Visum. Hilde näht "
            "ihm Hemden für die Reise. Rahel wohnt jetzt Angerring 11."
        ),
        note=(
            "The top of the first page of Sebastian's letter to Klara, as "
            "the booklet reproduces it; the archive holds the letter only "
            "as this image."
        ),
    ),
    Source(
        "family-history-2008",
        "A few pages of family history",
        "en",
        2008,
        "artifact",
        text=(
            "[page 1] A few pages of family history\n"
            "Nicholas Lobig, Stupfield, 2008\n"
            "The Löbigs were hatters in Flörburg, in Hesse. Eugen Löbig was "
            "a master hatter, and his son Sebastian, Wastl to everyone, born "
            "in 1862, was a hatter after him.\n"
            "Wastl married Hildegard Laudler, Hilde, in 1905. Their children "
            "were Ursula, Klara and Gustav.\n"
            "Ursula Löbig, the eldest, was born in 1911 and died in 1941. "
            "Klara, called Pünktchen, was born in 1917. Gustav, the "
            "youngest, was Gustl at home.\n"
            "The Flörburg address book of 1912 also lists an Ursula Löbig, "
            "a widow at Angerring 57. Nobody in the family ever knew who "
            "she was.\n"
            "[page 2] Klara left Flörburg first and settled in Stupfield. "
            "Gustl, my father, followed her in 1939 and became Gustave "
            "Lobig here, Gus to everyone.\n"
            "Dad married Florence Faxridge in 1942. I was born in '50.\n"
            "Dad died in '96 and he is buried here in Stupfield."
        ),
        note=(
            "Two typed pages by Nicholas, a possibly living writer: the "
            "fixture corpus's narrative (tests/fixtures/corpus)."
        ),
    ),
    Source(
        "library-talk-2018",
        "Where the Lobigs came from",
        "en",
        2018,
        "presentation",
        text=(
            "[Slide 1]\n"
            "Where the Lobigs came from\n"
            "Nicholas Lobig · library talk, 2018\n"
            "[Slide 2]\n"
            "Wastl and Hilde\n"
            "Sebastian 'Wastl' Löbig 1862–1945 = Hildegard 'Hilde' "
            "Laudler 1876–1968\n"
            "Their children: Ursula, Klara 'Pünktchen' and Gustav 'Gustl'\n"
            "[Slide 2 notes]\n"
            "Photograph from the family album\n"
            "[Slide 3]\n"
            "Stupfield\n"
            "Klara came first, Gustl in 1939.\n"
            "Gustave 'Gus' Lobig = Florence Faxridge, 1942\n"
            "Their son: Nicholas 1950"
        ),
        note=(
            "Nicholas's three slides for a talk at the library, with a "
            "photograph from the family album on the second; the text is "
            "the deck's slide text, as the pipeline reads it."
        ),
    ),
    # English gloss: "Death certificate. Citizen Löbig Rudolf Feliksovich
    # died on 6 March 1941, aged 24, and the entry was made in the civil
    # register of deaths on 7 March 1941 as no. 113. Cause of death: heart
    # defect. Place of death: town, village: the village of Koshnovka;
    # district: Kaldovsky; oblast, krai: Chadsk; republic: RSFSR. Place of
    # registration: the Kaldovsky district civil registry bureau. Issued on
    # 7 March 1941."
    Source(
        "death-certificate-1941",
        "Death certificate of Rudolf Löbig",
        "ru",
        1941,
        "certificate",
        text=(
            "Свидетельство о смерти.\n"
            "Гр. Лёбиг Рудольф Феликсович умер 6 марта 1941 г. в возрасте "
            "24 лет, о чём в книге записей актов гражданского состояния о "
            "смерти 1941 г. марта месяца 7 числа произведена "
            "соответствующая запись за № 113.\n"
            "Причина смерти: порок сердца.\n"
            "Место смерти: город, селение д. Кошновка; район Калдовский; "
            "область, край Чадская; республика РСФСР.\n"
            "Место регистрации: Калдовское районное бюро ЗАГС.\n"
            "Дата выдачи: 7 марта 1941 г."
        ),
        note=(
            "A Soviet form: the name surname first, with a patronymic, and "
            "the place of death in fields, with its district, oblast and "
            "republic. Nina sent it to Leopold with her 1958 letter."
        ),
    ),
    # English gloss: "Koshnovka village, Kaldovsky district, Chadsk
    # oblast, 25 December 1958. Dear Leopold! Nina Vasilyevna Sartina,
    # Löbig by her husband, the widow of your late brother Rudolf, writes to
    # you. You asked for his death certificate: I enclose it. Rudolf came
    # to Koshnovka in 1936 and worked as a fitter on the collective farm.
    # We married in 1938. In the village they call me just that: Rudolfikha
    # (Rudolf's wife). Is Sebastian, his uncle on his father's side, alive?
    # And where is Klara now, his first cousin, who went to Australia? My
    # brother Pavel Sartin sends you his regards. Nina Sartina"
    Source(
        "letter-to-leopold-1958",
        "Letter from Nina Sartina, Rudolf's widow, to Leopold Löbig",
        "ru",
        1958,
        "letter",
        text=(
            "д. Кошновка Калдовского района Чадской области, 25 декабря "
            "1958 г.\n"
            "Дорогой Леопольд!\n"
            "Пишет Вам Нина Васильевна Сартина, по мужу Лёбиг, вдова Вашего "
            "покойного брата Рудольфа. Вы просили прислать свидетельство о "
            "его смерти, посылаю.\n"
            "Рудольф приехал в Кошновку в 1936 году и работал слесарем в "
            "колхозе. В 1938 году мы поженились. В деревне меня так и "
            "зовут: Рудольфиха.\n"
            "Жив ли Себастьян, его дядя по отцу? И где теперь Клара, его "
            "двоюродная сестра, что уехала в Австралию?\n"
            "Мой брат Павел Сартин кланяется Вам.\n"
            "Нина Сартина"
        ),
        note=(
            "Kept with Leopold's papers. Rudolf is its ego: the kinship "
            "terms say what Sebastian and Klara were to him."
        ),
    ),
    # English gloss: "Snylsjum, 24 January 1952. Dear Sipkje, Many thanks
    # for your letter and for the photograph of your daughter Florence's
    # little Nicholas. Mem (mother) is so proud: now she is a
    # great-grandmother! She has been a widow since our late father died,
    # but she is still in good health and lives with us. The Snylsjum
    # brickworks, where Father worked so long, is closed now. Warm
    # greetings to you, and to Florence and Gus too, your brother Sybren.
    # [envelope] Mrs S. Faxridge-van 't Nijks, 1 Linden Grove, Stupfield,
    # Victoria, Australia. [envelope, back] Sender: S. van 't Nijks,
    # Snylsjum, municipality of Drakeradiel, Friesland, the Netherlands."
    Source(
        "letter-to-sipkje-1952",
        "Letter from Sybren van 't Nijks to his sister Sipkje Faxridge",
        "fy",
        1952,
        "letter",
        text=(
            "Snylsjum, 24 jannewaris 1952\n"
            "Leave Sipkje,\n"
            "Tige tank foar dyn brief en foar it portret fan dyn dochter "
            "Florence har lytse Nicholas. Mem is sa grutsk: no is se "
            "oerbeppe! Se is widdo sûnt ús sillige heit stoarn is, mar se "
            "is noch goed sûn en wennet by ús.\n"
            "It tichelwurk fan Snylsjum, dêr't heit sa lang wurke hat, is "
            "no ticht.\n"
            "Hertlike groetnis oan dy, en ek oan Florence en Gus,\n"
            "dyn broer Sybren\n"
            "[envelope] Mefrou S. Faxridge-van 't Nijks, 1 Linden Grove, "
            "Stupfield, Victoria, Austraalje\n"
            "[envelope, back] Ôfstjoerder: S. van 't Nijks, Snylsjum, "
            "gemeente Drakeradiel, Fryslân, Nederlân"
        ),
        note=(
            "Kept with Florence's mother's papers. It names her parents only "
            "as Mem and heit, and gives no date for Nicholas."
        ),
    ),
    Source(
        "death-certificate-1963",
        "Certificate of death for Florence Lobig",
        "en",
        1963,
        "certificate",
        text=(
            "State of Victoria. Certificate of death.\n"
            "Name: Florence LOBIG, née FAXRIDGE.\n"
            "Date of death: 3rd July 1963.\n"
            "Place of death: Stupfield.\n"
            "Father: not stated.\n"
            "Mother: Florence FAXRIDGE.\n"
            "Informant: husband."
        ),
        note=(
            "Its informant gave Florence's own birth name as her mother's: "
            "the mother it names is Sipkje, named wrongly. The fixture "
            "corpus's hard (kin-edge) conflict (tests/fixtures/corpus)."
        ),
    ),
)

# Values the assertions use that the manifest does not hold yet.
# ADR-0008 (as amended by #289) adds `machine_assisted`, the sourcing
# circumstance of a text that its source says was prepared with machine
# assistance, with the extraction change that sets it (its follow-up B).
PLANNED_VOCABULARY = ("machine_assisted",)

ASSERTIONS = tuple(
    _said(
        "studio-photo-1890s",
        ("studio-photo-1890s", "point_in_time", Date(1890, decade=True)),
        ("studio-bruesrich", "location", "peiltal", "Brüsrich · Peiltal"),
        # what the portrait states of itself (ADR-0039 §2.2): its mount
        # names the studio, and its reverse whom it shows, one statement a
        # name; a source's words, never a confirmed depiction (ADR-0005)
        (
            "studio-photo-1890s",
            "author",
            "studio-bruesrich",
            "Photograph Brüsrich",
        ),
        (
            "studio-photo-1890s",
            "shows",
            "eugen-loebig",
            {"referred_to_as": "Muckel"},
            "Muckel",
        ),
        (
            "studio-photo-1890s",
            "shows",
            "caecilie-loebig",
            {"referred_to_as": "Cilli"},
            "Cilli",
        ),
        (
            "studio-photo-1890s",
            "shows",
            "sebastian-loebig",
            {"referred_to_as": "Wastl"},
            "Wastl",
        ),
        (
            "studio-photo-1890s",
            "shows",
            "rahel-loebig",
            {"referred_to_as": "Rahel"},
            "Rahel",
        ),
        (
            "studio-photo-1890s",
            "shows",
            "felix-loebig",
            {"referred_to_as": "Felix"},
            "Felix",
        ),
    )
    + _said(
        "marriage-cert-1905",
        (
            "floerburg-registry",
            "location",
            "floerburg",
            "Standesamt Flörburg",
        ),
        (
            "sebastian-loebig",
            "spouse",
            "hildegard-loebig",
            {"start_time": Date(1905, 12, 15), "location": "floerburg"},
            "Flörburg, am 15. Dezember 1905",
        ),
        (
            "sebastian-loebig",
            "date_of_birth",
            Date(1862, 2, 15),
            "geboren am 15. Februar 1862 zu Flörburg",
        ),
        (
            "sebastian-loebig",
            "place_of_birth",
            "floerburg",
            "geboren am 15. Februar 1862 zu Flörburg",
        ),
        (
            "sebastian-loebig",
            "occupation",
            "hatter",
            "der Hutmacher Sebastian Löbig",
        ),
        (
            "sebastian-loebig",
            "father",
            "eugen-loebig",
            "Sohn des Hutmachermeisters Eugen Löbig",
        ),
        (
            "sebastian-loebig",
            "mother",
            "caecilie-loebig",
            "Cäcilie geborene Tarle",
        ),
        (
            "eugen-loebig",
            "occupation",
            "master-hatter",
            "Hutmachermeisters Eugen Löbig",
        ),
        (
            "caecilie-loebig",
            "birth_name",
            "Cäcilie Tarle",
            "Cäcilie geborene Tarle",
        ),
        (
            "hildegard-loebig",
            "birth_name",
            "Hildegard Laudler",
            "die Hildegard Laudler",
        ),
        (
            "hildegard-loebig",
            "date_of_birth",
            Date(1876, 3, 19),
            "geboren am 19. März 1876 zu Fläckfeld",
        ),
        (
            "hildegard-loebig",
            "place_of_birth",
            "flaeckfeld",
            "geboren am 19. März 1876 zu Fläckfeld",
        ),
        (
            "flaeckfeld",
            "part_of",
            "kreis-mockburg",
            "Fläckfeld, Kreis Mockburg",
        ),
    )
    + _said(
        "directory-1912",
        (
            "zum-blauen-hut",
            "location",
            "brunnenring-35",
            {"point_in_time": Date(1912)},
            "Brunnenring 35",
        ),
        (
            "sebastian-loebig",
            "employer",
            "zum-blauen-hut",
            {"point_in_time": Date(1912)},
            "Inh. Sebastian Löbig",
        ),
        ("zum-blauen-hut", "start_time", Date(1863), "gegr. 1863"),
        (
            "ursula-loebig-angerring",
            "residence",
            "angerring-57",
            {"point_in_time": Date(1912)},
            "Löbig, Ursula, Wwe., Angerring 57",
        ),
    )
    + _said(
        "burial-register-1909",
        (
            "eugen-loebig",
            "date_of_death",
            Date(1909, 3, 16),
            "Gestorben: 16. März 1909",
        ),
        (
            "eugen-loebig",
            "place_of_death",
            "floerburg",
            "Gestorben: 16. März 1909 in Flörburg",
        ),
        (
            "eugen-loebig",
            "cause_of_death",
            "Wassersucht",
            "Todesursache: Wassersucht",
        ),
        ("eugen-loebig", "age_at_death", "73 Jahre", "Alter: 73 Jahre"),
        (
            "eugen-loebig",
            "burial_place",
            "floerburg-cemetery",
            {"point_in_time": Date(1909, 3, 20)},
            "Beerdigt: 20. März 1909",
        ),
        (
            "eugen-loebig",
            "occupation",
            "master-hatter",
            "Löbig, Eugen, Hutmachermeister",
        ),
        (
            "eugen-loebig",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1909, 3, 16)},
            "Wohnung: Brunnenring 35",
        ),
        (
            "eugen-loebig",
            "spouse",
            "caecilie-loebig",
            "Witwer der Cäcilie Löbig geb. Tarle",
        ),
        (
            "caecilie-loebig",
            "birth_name",
            "Cäcilie Tarle",
            "Cäcilie Löbig geb. Tarle",
        ),
        (
            "caecilie-loebig",
            "married_name",
            "Cäcilie Löbig",
            "Cäcilie Löbig geb. Tarle",
        ),
    )
    + _said(
        "school-report-1930",
        (
            "realschule-floerburg",
            "location",
            "floerburg",
            "Städtische Realschule zu Flörburg",
        ),
        (
            "rudolf-loebig",
            "date_of_birth",
            Date(1916, 10, 24),
            "geb. am 24. Oktober 1916",
        ),
        (
            "rudolf-loebig",
            "father",
            "felix-loebig",
            "Sohn des Hutmachers Felix Löbig",
        ),
        (
            "rudolf-loebig",
            "educated_at",
            "realschule-floerburg",
            {"point_in_time": Date(1930)},
            "Städtische Realschule zu Flörburg",
        ),
        (
            "felix-loebig",
            "occupation",
            "hatter",
            "Sohn des Hutmachers Felix Löbig",
        ),
        (
            "felix-loebig",
            "residence",
            "wasserallee-59",
            {"point_in_time": Date(1930)},
            "Flörburg, Wasserallee 59",
        ),
    )
    + _said(
        "birth-extract-1938",
        (
            "gustav-loebig",
            "date_of_birth",
            Date(1919, 1, 7),
            "am siebenten Januar des Jahres tausend neunhundert und neunzehn",
        ),
        (
            "gustav-loebig",
            "place_of_birth",
            "floerburg",
            "zu Flörburg in seiner Wohnung",
        ),
        ("gustav-loebig", "father", "sebastian-loebig", "Sebastian Löbig"),
        (
            "gustav-loebig",
            "mother",
            "hildegard-loebig",
            "Hildegard Löbig geborene Laudler",
        ),
        (
            "hildegard-loebig",
            "birth_name",
            "Hildegard Laudler",
            "Hildegard Löbig geborene Laudler",
        ),
        (
            "hildegard-loebig",
            "married_name",
            "Hildegard Löbig",
            "Hildegard Löbig geborene Laudler",
        ),
        (
            "sebastian-loebig",
            "occupation",
            "hatter",
            "Der Hutmacher Sebastian Löbig",
        ),
        (
            "sebastian-loebig",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1919, 1, 8)},
            "wohnhaft in Flörburg, Brunnenring 35",
        ),
    )
    + _said(
        "postcard-1938",
        (
            "postcard-1938",
            "point_in_time",
            Date(1938, 8, 19),
            "[postmark] Flörburg 19.8.38",
        ),
        ("klara-loebig", "father", "sebastian-loebig", "Dein Vater"),
        # a pet name the writer greets her by: an appellation, from his
        # view (ADR-0018 as amended by #499)
        (
            "klara-loebig",
            "referred_to_as",
            "Pünktchen",
            {"perspective": "sebastian-loebig"},
            "Liebes Pünktchen!",
        ),
        (
            "emigration-1938",
            "participant",
            "klara-loebig",
            "Seit Du am 22. Juni abgereist bist",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1938, 6, 22),
            "Seit Du am 22. Juni abgereist bist",
        ),
        (
            "klara-loebig",
            "residence",
            "juniper-street-31",
            {"point_in_time": Date(1938, 8, 19)},
            "31 Juniper Street, Stupfield",
        ),
        (
            "gustav-loebig",
            "nickname",
            "Gustl",
            "Gustl wartet noch auf sein Visum",
        ),
    )
    + _said(
        "registration-card-1945",
        ("ida-loebig", "birth_name", "Ida Sörne", "née SÖRNE"),
        (
            "ida-loebig",
            "married_name",
            "Ida Löbig",
            "LÖBIG, Ida, née SÖRNE",
        ),
        (
            "ida-loebig",
            "date_of_birth",
            Date(1881, 4, 17),
            "Born 17.4.1881 at Moshausen",
        ),
        (
            "ida-loebig",
            "place_of_birth",
            "moshausen",
            "Born 17.4.1881 at Moshausen",
        ),
        (
            "ida-loebig",
            "residence",
            "wasserallee-59",
            "Last address: Flörburg, Wasserallee 59",
        ),
        (
            "deportation-1942",
            "participant",
            "ida-loebig",
            "Deported 21.8.1942 to Klurnau",
        ),
        (
            "deportation-1942",
            "point_in_time",
            Date(1942, 8, 21),
            "Deported 21.8.1942 to Klurnau",
        ),
        (
            "deportation-1942",
            "location",
            "klurnau",
            "Deported 21.8.1942 to Klurnau",
        ),
        (
            "liberation-1945",
            "participant",
            "ida-loebig",
            "Liberated at Klurnau 28.1.1945",
        ),
        (
            "liberation-1945",
            "point_in_time",
            Date(1945, 1, 28),
            "Liberated at Klurnau 28.1.1945",
        ),
        (
            "liberation-1945",
            "location",
            "klurnau",
            "Liberated at Klurnau 28.1.1945",
        ),
        (
            "ida-loebig",
            "spouse",
            "felix-loebig",
            "Husband: Felix LÖBIG",
        ),
        (
            "leopold-loebig",
            "mother",
            "ida-loebig",
            "Son: Leopold LÖBIG",
        ),
        (
            "arrest-1939",
            "participant",
            "leopold-loebig",
            "Leopold LÖBIG, arrested 3.9.1939",
        ),
        (
            "arrest-1939",
            "point_in_time",
            Date(1939, 9, 3),
            "arrested 3.9.1939",
        ),
        (
            "imprisonment-1939",
            "participant",
            "leopold-loebig",
            "held in Peiltal court prison",
        ),
        (
            "imprisonment-1939",
            "location",
            "peiltal-court-prison",
            "held in Peiltal court prison",
        ),
        (
            "imprisonment-1939",
            "start_time",
            Date(1939, 9, 3),
            "arrested 3.9.1939, held",
        ),
        (
            "imprisonment-1939",
            "end_time",
            Date(1939, 10, 1),
            "until 1.10.1939",
        ),
        (
            "release-1939",
            "participant",
            "leopold-loebig",
            "held in Peiltal court prison until 1.10.1939",
        ),
        (
            "release-1939",
            "point_in_time",
            Date(1939, 10, 1),
            "until 1.10.1939",
        ),
        (
            "release-1939",
            "location",
            "peiltal-court-prison",
            "held in Peiltal court prison until 1.10.1939",
        ),
        (
            "felix-loebig",
            "date_of_death",
            Date(1931),
            "Felix LÖBIG, died 1931",
        ),
    )
    + _said(
        "naturalisation-1952",
        (
            "gustav-loebig",
            "official_name",
            "Gustave Lobig",
            "Gustave LOBIG",
        ),
        (
            "gustav-loebig",
            "birth_name",
            "Gustav Löbig",
            "formerly Gustav LÖBIG",
        ),
        (
            "gustav-loebig",
            "date_of_birth",
            Date(1919, 7, 1),
            "Born 1st July 1919",
        ),
        (
            "gustav-loebig",
            "place_of_birth",
            "floerburg",
            {"referred_to_as": "Floerburg"},
            "at Floerburg, Germany",
        ),
        ("gustav-loebig", "occupation", "hatter", "hatter"),
        (
            "gustav-loebig",
            "residence",
            "hawthorn-road-8",
            {"point_in_time": Date(1952)},
            "8 Hawthorn Road, Stupfield, Victoria",
        ),
        (
            "gustav-loebig",
            "residence",
            "australia",
            {"start_time": Date(1939)},
            "Arrived in Australia 1939",
        ),
        (
            "gustav-loebig",
            "country_of_citizenship",
            "australia",
            {"start_time": Date(1952)},
            "Granted 1952",
        ),
    )
    + _said(
        "compensation-file-1953",
        (
            "wiedergutmachungsamt-peiltal",
            "location",
            "peiltal",
            "Wiedergutmachungsamt Peiltal",
        ),
        (
            "leopold-loebig",
            "mother",
            "ida-loebig",
            "für seine verstorbene Mutter",
        ),
        (
            "ida-loebig",
            "birth_name",
            "Ida Sörne",
            "Ida Löbig geb. Sörne",
        ),
        (
            "ida-loebig",
            "married_name",
            "Ida Löbig",
            "Ida Löbig geb. Sörne",
        ),
        (
            "ida-loebig",
            "date_of_birth",
            Date(1881, 4, 17),
            "geb. 17.4.1881",
        ),
        (
            "ida-loebig",
            "residence",
            "wasserallee-59",
            "zuletzt wohnhaft in Flörburg, Wasserallee 59",
        ),
        (
            "deportation-1942",
            "participant",
            "ida-loebig",
            "Am 21. August 1942 von Flörburg in das Lager Klurnau verschleppt",
        ),
        (
            "deportation-1942",
            "point_in_time",
            Date(1942, 8, 21),
            "Am 21. August 1942",
        ),
        (
            "deportation-1942",
            "location",
            "klurnau-camp",
            "in das Lager Klurnau verschleppt",
        ),
        (
            "ida-loebig",
            "date_of_death",
            Date(1946),
            "1946 verstorben",
        ),
    )
    + _said(
        "gravestone-photo-1956",
        ("eugen-loebig", "date_of_birth", Date(1836), "1836 – 1909"),
        ("eugen-loebig", "date_of_death", Date(1909), "1836 – 1909"),
        ("caecilie-loebig", "date_of_birth", Date(1833), "1833 – 1902"),
        ("caecilie-loebig", "date_of_death", Date(1902), "1833 – 1902"),
        (
            "caecilie-loebig",
            "birth_name",
            "Cäcilie Tarle",
            "GEB. TARLE",
        ),
        (
            "eugen-loebig",
            "burial_place",
            "floerburg-cemetery",
            "EUGEN LÖBIG",
        ),
        (
            "caecilie-loebig",
            "burial_place",
            "floerburg-cemetery",
            "CÄCILIE LÖBIG",
        ),
    )
    + _said(
        "divorce-judgment-1957",
        (
            "landgericht-rekau",
            "location",
            "rekau",
            "hat das Landgericht Rekau",
        ),
        (
            "leopold-loebig",
            "spouse",
            "agnes-loebig",
            {
                "start_time": Date(1947, 2, 8),
                "location": "floerburg",
                "end_time": Date(1957, 1, 2),
            },
            "am 2. Januar 1957 für Recht erkannt: Die am 8. Februar 1947 "
            "vor dem Standesbeamten in Flörburg geschlossene Ehe der "
            "Parteien wird geschieden",
        ),
        (
            "leopold-loebig",
            "date_of_birth",
            Date(1914, 5, 29),
            "geboren am 29. Mai 1914",
        ),
        (
            "leopold-loebig",
            "residence",
            "floerburg",
            {"point_in_time": Date(1957, 1, 2)},
            "wohnhaft in Flörburg",
        ),
        (
            "agnes-loebig",
            "birth_name",
            "Agnes Nertler",
            "Agnes Löbig geb. Nertler",
        ),
        (
            "agnes-loebig",
            "married_name",
            "Agnes Löbig",
            "Agnes Löbig geb. Nertler",
        ),
        (
            "agnes-loebig",
            "date_of_birth",
            Date(1912, 4, 20),
            "geboren am 20. April 1912 zu Kredfeld",
        ),
        (
            "agnes-loebig",
            "place_of_birth",
            "kredfeld",
            "geboren am 20. April 1912 zu Kredfeld",
        ),
    )
    + _said(
        "family-tree-1984",
        ("eugen-loebig", "number_of_children", 3, "Their children"),
        (
            "eugen-loebig",
            "date_of_birth",
            Date(1836),
            "b. Bad Flörburg 1836",
        ),
        (
            "eugen-loebig",
            "place_of_birth",
            "floerburg",
            {"referred_to_as": "Bad Flörburg"},
            "b. Bad Flörburg 1836",
        ),
        ("eugen-loebig", "date_of_death", Date(1909), "d. 1909"),
        (
            "eugen-loebig",
            "spouse",
            "caecilie-loebig",
            {"start_time": Date(1859)},
            "= (1859) Cäcilie 'Cilli' Tarle",
        ),
        (
            "caecilie-loebig",
            "nickname",
            "Cilli",
            "Cäcilie 'Cilli' Tarle",
        ),
        (
            "caecilie-loebig",
            "birth_name",
            "Cäcilie Tarle",
            "Cäcilie 'Cilli' Tarle",
        ),
        (
            "caecilie-loebig",
            "place_of_birth",
            "kredfeld",
            "Tarle of Kredfeld",
        ),
        (
            "caecilie-loebig",
            "date_of_birth",
            Date(1833, qualifier="circa"),
            "c. 1833–1902",
        ),
        (
            "caecilie-loebig",
            "date_of_death",
            Date(1902),
            "c. 1833–1902",
        ),
        ("sebastian-loebig", "father", "eugen-loebig", "Their children"),
        (
            "sebastian-loebig",
            "mother",
            "caecilie-loebig",
            "Their children",
        ),
        (
            "sebastian-loebig",
            "nickname",
            "Wastl",
            "Sebastian 'Wastl' 1862–1945",
        ),
        (
            "sebastian-loebig",
            "date_of_birth",
            Date(1862),
            "Sebastian 'Wastl' 1862–1945",
        ),
        (
            "sebastian-loebig",
            "date_of_death",
            Date(1945),
            "Sebastian 'Wastl' 1862–1945",
        ),
        (
            "sebastian-loebig",
            "spouse",
            "hildegard-loebig",
            "1862–1945 = Hildegard 'Hilde' Laudler",
        ),
        (
            "hildegard-loebig",
            "nickname",
            "Hilde",
            "Hildegard 'Hilde' Laudler",
        ),
        (
            "hildegard-loebig",
            "place_of_birth",
            "flaeckfeld",
            "Laudler of Fläckfeld",
        ),
        (
            "flaeckfeld",
            "part_of",
            "kreis-peiltal",
            "Fläckfeld (Kr. Peiltal)",
        ),
        (
            "hildegard-loebig",
            "date_of_birth",
            Date(1876),
            "(Kr. Peiltal) 1876–1968",
        ),
        (
            "hildegard-loebig",
            "date_of_death",
            Date(1968),
            "(Kr. Peiltal) 1876–1968",
        ),
        ("felix-loebig", "father", "eugen-loebig", "Their children"),
        (
            "felix-loebig",
            "mother",
            "caecilie-loebig",
            "Their children",
        ),
        ("felix-loebig", "date_of_birth", Date(1867), "Felix 1867–1931"),
        ("felix-loebig", "date_of_death", Date(1931), "Felix 1867–1931"),
        (
            "felix-loebig",
            "spouse",
            "ida-loebig",
            "1867–1931 = Ida 'Peppi' Sörne",
        ),
        (
            "ida-loebig",
            "nickname",
            "Peppi",
            "Ida 'Peppi' Sörne",
        ),
        (
            "ida-loebig",
            "date_of_birth",
            Date(1881),
            "Ida 'Peppi' Sörne 1881–1946",
        ),
        (
            "ida-loebig",
            "date_of_death",
            Date(1946),
            "Ida 'Peppi' Sörne 1881–1946",
        ),
        ("rahel-loebig", "father", "eugen-loebig", "Their children"),
        (
            "rahel-loebig",
            "mother",
            "caecilie-loebig",
            "Their children",
        ),
        (
            "rahel-loebig",
            "date_of_birth",
            Date(1871),
            "Rahel 1871 – after 1945",
        ),
        (
            "rahel-loebig",
            "date_of_death",
            Date(1945, qualifier="after"),
            "Rahel 1871 – after 1945",
        ),
        ("ursula-loebig", "father", "sebastian-loebig", "Wastl and Hilde"),
        ("ursula-loebig", "mother", "hildegard-loebig", "Wastl and Hilde"),
        ("ursula-loebig", "date_of_birth", Date(1911), "Ursula 1911–1941"),
        ("ursula-loebig", "date_of_death", Date(1941), "Ursula 1911–1941"),
        ("klara-loebig", "father", "sebastian-loebig", "Wastl and Hilde"),
        ("klara-loebig", "mother", "hildegard-loebig", "Wastl and Hilde"),
        ("klara-loebig", "nickname", "Pünktchen", "Klara 'Pünktchen' 1917"),
        (
            "klara-loebig",
            "date_of_birth",
            Date(1917),
            "Klara 'Pünktchen' 1917",
        ),
        ("gustav-loebig", "father", "sebastian-loebig", "Wastl and Hilde"),
        ("gustav-loebig", "mother", "hildegard-loebig", "Wastl and Hilde"),
        ("gustav-loebig", "nickname", "Gustl", "Gustav 'Gustl' 1919"),
        ("gustav-loebig", "date_of_birth", Date(1919), "Gustav 'Gustl' 1919"),
        (
            "gustav-loebig",
            "spouse",
            "florence-lobig",
            {"start_time": Date(1942)},
            "= (1942) Florence Faxridge",
        ),
        (
            "florence-lobig",
            "birth_name",
            "Florence Faxridge",
            "Florence Faxridge 1914–1963",
        ),
        (
            "florence-lobig",
            "date_of_birth",
            Date(1914),
            "Florence Faxridge 1914–1963",
        ),
        (
            "florence-lobig",
            "date_of_death",
            Date(1963),
            "Florence Faxridge 1914–1963",
        ),
        ("leopold-loebig", "father", "felix-loebig", "Felix and Peppi"),
        ("leopold-loebig", "mother", "ida-loebig", "Felix and Peppi"),
        ("leopold-loebig", "date_of_birth", Date(1914), "Leopold 1914–1960"),
        ("leopold-loebig", "date_of_death", Date(1960), "Leopold 1914–1960"),
        (
            "leopold-loebig",
            "spouse",
            "agnes-loebig",
            {"start_time": Date(1947), "end_time": Date(1957)},
            "= (1947) Agnes Nertler 1912–1974, div. 1957",
        ),
        (
            "agnes-loebig",
            "birth_name",
            "Agnes Nertler",
            "Agnes Nertler 1912–1974",
        ),
        (
            "agnes-loebig",
            "date_of_birth",
            Date(1912),
            "Agnes Nertler 1912–1974",
        ),
        (
            "agnes-loebig",
            "date_of_death",
            Date(1974),
            "Agnes Nertler 1912–1974",
        ),
        (
            "rudolf-loebig",
            "father",
            "felix-loebig",
            "Felix and Peppi",
        ),
        (
            "rudolf-loebig",
            "mother",
            "ida-loebig",
            "Felix and Peppi",
        ),
        ("rudolf-loebig", "nickname", "Rudi", "Rudolf 'Rudi' 1916–1941"),
        (
            "rudolf-loebig",
            "date_of_birth",
            Date(1916),
            "Rudolf 'Rudi' 1916–1941",
        ),
        (
            "rudolf-loebig",
            "date_of_death",
            Date(1941),
            "Rudolf 'Rudi' 1916–1941",
        ),
        (
            "infant-loebig",
            "father",
            "felix-loebig",
            "Felix and Peppi",
        ),
        (
            "infant-loebig",
            "mother",
            "ida-loebig",
            "Felix and Peppi",
        ),
        ("infant-loebig", "date_of_birth", Date(1922), "infant 1922"),
        ("infant-loebig", "date_of_death", Date(1922), "infant 1922"),
        (
            "nicholas-lobig",
            "father",
            "gustav-loebig",
            "Children of Gustl and Florence",
        ),
        (
            "nicholas-lobig",
            "mother",
            "florence-lobig",
            "Children of Gustl and Florence",
        ),
        ("nicholas-lobig", "date_of_birth", Date(1950), "Nicholas 1950"),
    )
    + _said(
        "interview-2002",
        (
            "nicholas-lobig",
            "date_of_birth",
            Date(1950),
            "I was born in '50, in Stupfield",
        ),
        (
            "maisie-cutmore",
            "referred_to_as",
            "Nana Maisie",
            {"perspective": "nicholas-lobig"},
            "Nana Maisie next door",
        ),
        (
            "nicholas-lobig",
            "place_of_birth",
            "stupfield",
            "I was born in '50, in Stupfield",
        ),
        (
            "nicholas-lobig",
            "father",
            "gustav-loebig",
            "Dad — Gus, everyone called him",
        ),
        ("gustav-loebig", "nickname", "Gus", "Gus, everyone called him"),
        (
            "emigration-1939",
            "participant",
            "gustav-loebig",
            "left Germany in the August, just before the war",
        ),
        (
            "emigration-1939",
            "point_in_time",
            Date(1939, 8),
            "left Germany in the August, just before the war",
        ),
        ("klara-loebig", "nickname", "Pünktchen", "Aunt Pünktchen"),
        (
            "klara-loebig",
            "relative",
            "nicholas-lobig",
            {"kinship_type": "aunt"},
            "Aunt Pünktchen",
        ),
        (
            "emigration-1938",
            "participant",
            "klara-loebig",
            "Aunt Pünktchen had gone ahead in '38 or '39",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1938, qualifier="between", until=Date(1939)),
            "Aunt Pünktchen had gone ahead in '38 or '39",
        ),
        (
            "leopold-loebig",
            "relative",
            "gustav-loebig",
            {"kinship_type": "cousin"},
            "Dad's cousin Leopold",
        ),
        (
            "arrest-1939",
            "participant",
            "leopold-loebig",
            "Dad's cousin Leopold was taken",
        ),
        (
            "arrest-1939",
            "point_in_time",
            Date(1939, 9, qualifier="circa"),
            "taken the first week of the war",
        ),
        (
            "imprisonment-1939",
            "location",
            "peiltal",
            "held about a month in Peiltal",
        ),
        (
            "zum-blauen-hut",
            "referred_to_as",
            "the Blue Hat",
            "The hat shop, the Blue Hat",
        ),
        (
            "zum-blauen-hut",
            "end_time",
            Date(1926, qualifier="before"),
            "was gone before 1926",
        ),
        ("gustav-loebig", "date_of_death", Date(1996), "Dad died in '96"),
        (
            "gustav-loebig",
            "burial_place",
            "stupfield",
            "he's buried here in Stupfield",
        ),
        (
            "klara-loebig",
            "date_of_death",
            Date(2001),
            "Aunt Pünktchen died last year",
        ),
    )
    + _said(
        "family-booklet-2011",
        (
            "family-booklet-2011",
            "point_in_time",
            Date(2011),
            "Stupfield, 2011",
        ),
        # the booklet asserts its own composition (ADR-0008)
        (
            "letter-excerpt-1939",
            "part_of",
            "family-booklet-2011",
            {"page_number": "6"},
        ),
        # read from the translation, which page 2 says was done with
        # translation software: each claim carries the marker
        (
            "sebastian-loebig",
            "residence",
            "brunnenring-35",
            {
                "point_in_time": Date(1939, 7, 28),
                "sourcing_circumstances": "machine_assisted",
            },
            "Flörburg, Brunnenring 35, 28 July 1939",
        ),
        (
            "klara-loebig",
            "referred_to_as",
            "Pünktchen",
            {
                "perspective": "sebastian-loebig",
                "sourcing_circumstances": "machine_assisted",
            },
            "Dear Pünktchen!",
        ),
        (
            "gustav-loebig",
            "nickname",
            "Gustl",
            {"sourcing_circumstances": "machine_assisted"},
            "Gustl finally has his visa",
        ),
        (
            "hildegard-loebig",
            "nickname",
            "Hilde",
            {"sourcing_circumstances": "machine_assisted"},
            "Hilde is sewing him shirts",
        ),
        (
            "rahel-loebig",
            "residence",
            "angerring-71",
            {
                "point_in_time": Date(1939, 7, 28),
                "sourcing_circumstances": "machine_assisted",
            },
            "Rahel now lives at Angerring 71",
        ),
        # the caption and the commentary are no translation: no marker
        ("sebastian-loebig", "nickname", "Wastl", "Wastl to Klara"),
        # the caption is the booklet's words about the image it places on
        # page 6, kept verbatim on the image's item (ADR-0039 §2.1); its
        # "July 1939" is never the letter's date
        (
            "letter-excerpt-1939",
            "caption",
            "Wastl to Klara, July 1939.",
            {"page_number": "6"},
            "Wastl to Klara, July 1939.",
        ),
        (
            "gustav-loebig",
            "sibling",
            "ursula-loebig",
            "Gustl was Ursula's younger brother",
        ),
    )
    + _said(
        "letter-excerpt-1939",
        # what the letter states of itself (ADR-0039 §2.2): its dateline,
        # its heading and its greeting; the top of the letter names no
        # writer, so it gives no author
        (
            "letter-excerpt-1939",
            "inception",
            Date(1939, 7, 28),
            "den 28. Juli 1939",
        ),
        (
            "letter-excerpt-1939",
            "location_of_creation",
            "brunnenring-35",
            "Flörburg, Brunnenring 35, den",
        ),
        (
            "letter-excerpt-1939",
            "addressee",
            "klara-loebig",
            "Liebes Pünktchen",
        ),
        (
            "sebastian-loebig",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1939, 7, 28)},
            "Flörburg, Brunnenring 35, den 28. Juli 1939",
        ),
        (
            "klara-loebig",
            "referred_to_as",
            "Pünktchen",
            {"perspective": "sebastian-loebig"},
            "Liebes Pünktchen!",
        ),
        (
            "gustav-loebig",
            "nickname",
            "Gustl",
            "Gustl hat endlich sein Visum",
        ),
        ("hildegard-loebig", "nickname", "Hilde", "Hilde näht ihm Hemden"),
        (
            "rahel-loebig",
            "residence",
            "angerring-11",
            {"point_in_time": Date(1939, 7, 28)},
            "Rahel wohnt jetzt Angerring 11",
        ),
    )
    + _said(
        "family-history-2008",
        (
            "family-history-2008",
            "point_in_time",
            Date(2008),
            "Stupfield, 2008",
        ),
        (
            "eugen-loebig",
            "occupation",
            "master-hatter",
            "Eugen Löbig was a master hatter",
        ),
        ("sebastian-loebig", "father", "eugen-loebig", "his son Sebastian"),
        ("sebastian-loebig", "nickname", "Wastl", "Wastl to everyone"),
        ("sebastian-loebig", "date_of_birth", Date(1862), "born in 1862"),
        (
            "sebastian-loebig",
            "occupation",
            "hatter",
            "was a hatter after him",
        ),
        ("floerburg", "part_of", "hesse", "Flörburg, in Hesse"),
        (
            "sebastian-loebig",
            "spouse",
            "hildegard-loebig",
            {"start_time": Date(1905)},
            "Wastl married Hildegard Laudler, Hilde, in 1905",
        ),
        ("hildegard-loebig", "nickname", "Hilde", "Hildegard Laudler, Hilde"),
        # the children, from the parents' side: the mirror of the export's
        # father and mother (the fixture corpus's mirrored kinship)
        *(
            (
                parent,
                "child",
                child,
                "Their children were Ursula, Klara and Gustav",
            )
            for parent in ("sebastian-loebig", "hildegard-loebig")
            for child in ("ursula-loebig", "klara-loebig", "gustav-loebig")
        ),
        (
            "ursula-loebig",
            "date_of_birth",
            Date(1911),
            "Ursula Löbig, the eldest, was born in 1911",
        ),
        ("ursula-loebig", "date_of_death", Date(1941), "died in 1941"),
        ("klara-loebig", "nickname", "Pünktchen", "Klara, called Pünktchen"),
        (
            "klara-loebig",
            "date_of_birth",
            Date(1917),
            "Klara, called Pünktchen, was born in 1917",
        ),
        (
            "gustav-loebig",
            "nickname",
            "Gustl",
            "Gustav, the youngest, was Gustl at home",
        ),
        # the homonym, named in the same source as Sebastian's daughter
        (
            "ursula-loebig-angerring",
            "residence",
            "angerring-57",
            {"point_in_time": Date(1912)},
            "an Ursula Löbig, a widow at Angerring 57",
        ),
        (
            "emigration-1938",
            "participant",
            "klara-loebig",
            "Klara left Flörburg first",
        ),
        ("klara-loebig", "residence", "stupfield", "settled in Stupfield"),
        (
            "emigration-1939",
            "participant",
            "gustav-loebig",
            "followed her in 1939",
        ),
        (
            "emigration-1939",
            "point_in_time",
            Date(1939),
            "followed her in 1939",
        ),
        ("nicholas-lobig", "father", "gustav-loebig", "Gustl, my father"),
        (
            "gustav-loebig",
            "official_name",
            "Gustave Lobig",
            "became Gustave Lobig here",
        ),
        ("gustav-loebig", "nickname", "Gus", "Gus to everyone"),
        (
            "gustav-loebig",
            "spouse",
            "florence-lobig",
            {"start_time": Date(1942)},
            "Dad married Florence Faxridge in 1942",
        ),
        # a two-digit year: the century is the reader's (the fixture
        # corpus's recorded model reads it as 1850)
        ("nicholas-lobig", "date_of_birth", Date(1950), "I was born in '50"),
        ("gustav-loebig", "date_of_death", Date(1996), "Dad died in '96"),
        (
            "gustav-loebig",
            "burial_place",
            "stupfield",
            "he is buried here in Stupfield",
        ),
    )
    + _said(
        "library-talk-2018",
        (
            "library-talk-2018",
            "point_in_time",
            Date(2018),
            "library talk, 2018",
        ),
        (
            "sebastian-loebig",
            "nickname",
            "Wastl",
            "Sebastian 'Wastl' Löbig",
        ),
        (
            "sebastian-loebig",
            "date_of_birth",
            Date(1862),
            "Sebastian 'Wastl' Löbig 1862–1945",
        ),
        (
            "sebastian-loebig",
            "date_of_death",
            Date(1945),
            "Sebastian 'Wastl' Löbig 1862–1945",
        ),
        (
            "sebastian-loebig",
            "spouse",
            "hildegard-loebig",
            "1862–1945 = Hildegard 'Hilde' Laudler",
        ),
        (
            "hildegard-loebig",
            "nickname",
            "Hilde",
            "Hildegard 'Hilde' Laudler",
        ),
        (
            "hildegard-loebig",
            "date_of_birth",
            Date(1876),
            "Laudler 1876–1968",
        ),
        (
            "hildegard-loebig",
            "date_of_death",
            Date(1968),
            "Laudler 1876–1968",
        ),
        *(
            (parent, "child", child, excerpt)
            for parent in ("sebastian-loebig", "hildegard-loebig")
            for child, excerpt in (
                ("ursula-loebig", "Their children: Ursula"),
                ("klara-loebig", "Klara 'Pünktchen'"),
                ("gustav-loebig", "Gustav 'Gustl'"),
            )
        ),
        ("klara-loebig", "nickname", "Pünktchen", "Klara 'Pünktchen'"),
        ("gustav-loebig", "nickname", "Gustl", "Gustav 'Gustl'"),
        (
            "emigration-1938",
            "participant",
            "klara-loebig",
            "Klara came first",
        ),
        (
            "emigration-1939",
            "participant",
            "gustav-loebig",
            "Gustl in 1939",
        ),
        (
            "emigration-1939",
            "point_in_time",
            Date(1939),
            "Gustl in 1939",
        ),
        ("gustav-loebig", "nickname", "Gus", "Gustave 'Gus' Lobig"),
        (
            "gustav-loebig",
            "spouse",
            "florence-lobig",
            {"start_time": Date(1942)},
            "Gustave 'Gus' Lobig = Florence Faxridge, 1942",
        ),
        *(
            (parent, "child", "nicholas-lobig", "Their son: Nicholas 1950")
            for parent in ("gustav-loebig", "florence-lobig")
        ),
        ("nicholas-lobig", "date_of_birth", Date(1950), "Nicholas 1950"),
    )
    + _said(
        "death-certificate-1941",
        (
            "death-certificate-1941",
            "point_in_time",
            Date(1941, 3, 7),
            "Дата выдачи: 7 марта 1941 г.",
        ),
        # surname, given name, patronymic: Feliksovich, son of Felix
        (
            "rudolf-loebig",
            "father",
            "felix-loebig",
            "Лёбиг Рудольф Феликсович",
        ),
        (
            "rudolf-loebig",
            "date_of_death",
            Date(1941, 3, 6),
            "умер 6 марта 1941 г.",
        ),
        ("rudolf-loebig", "age_at_death", "24 лет", "в возрасте 24 лет"),
        (
            "rudolf-loebig",
            "cause_of_death",
            "порок сердца",
            "Причина смерти: порок сердца",
        ),
        (
            "rudolf-loebig",
            "place_of_death",
            "koshnovka",
            "Место смерти: город, селение д. Кошновка",
        ),
        (
            "kaldovsky-registry",
            "location",
            "kaldovsky-district",
            "Калдовское районное бюро ЗАГС",
        ),
        # the place of death with its containers, in the form's fields
        (
            "koshnovka",
            "part_of",
            "kaldovsky-district",
            "д. Кошновка; район Калдовский; область, край Чадская; "
            "республика РСФСР",
        ),
        (
            "kaldovsky-district",
            "part_of",
            "chadsk-oblast",
            "д. Кошновка; район Калдовский; область, край Чадская; "
            "республика РСФСР",
        ),
        (
            "chadsk-oblast",
            "part_of",
            "russia",
            {"referred_to_as": "РСФСР"},
            "д. Кошновка; район Калдовский; область, край Чадская; "
            "республика РСФСР",
        ),
    )
    + _said(
        "letter-to-leopold-1958",
        (
            "letter-to-leopold-1958",
            "point_in_time",
            Date(1958, 12, 25),
            "25 декабря 1958 г.",
        ),
        # "по мужу" (by her husband) marks the married name, and so the
        # other as her own
        (
            "nina-sartina",
            "birth_name",
            "Нина Сартина",
            "Нина Васильевна Сартина, по мужу Лёбиг",
        ),
        ("nina-sartina", "married_name", "Нина Лёбиг", "по мужу Лёбиг"),
        # her patronymic: Vasilyevna, daughter of Vasily
        (
            "nina-sartina",
            "father",
            "vasily-sartin",
            "Нина Васильевна Сартина",
        ),
        # "вдова" (widow) states the marriage, and "покойного" (the late)
        # a death before the letter: claims, not parts of a name
        (
            "nina-sartina",
            "spouse",
            "rudolf-loebig",
            "вдова Вашего покойного брата Рудольфа",
        ),
        (
            "rudolf-loebig",
            "date_of_death",
            Date(1958, 12, 25, qualifier="before"),
            "покойного брата Рудольфа",
        ),
        (
            "rudolf-loebig",
            "sibling",
            "leopold-loebig",
            "Вашего покойного брата Рудольфа",
        ),
        (
            "emigration-1936",
            "participant",
            "rudolf-loebig",
            "Рудольф приехал в Кошновку в 1936 году",
        ),
        (
            "emigration-1936",
            "point_in_time",
            Date(1936),
            "Рудольф приехал в Кошновку в 1936 году",
        ),
        ("emigration-1936", "location", "koshnovka", "приехал в Кошновку"),
        (
            "rudolf-loebig",
            "residence",
            "koshnovka",
            {"start_time": Date(1936)},
            "Рудольф приехал в Кошновку в 1936 году",
        ),
        (
            "rudolf-loebig",
            "occupation",
            "fitter",
            "работал слесарем в колхозе",
        ),
        (
            "nina-sartina",
            "spouse",
            "rudolf-loebig",
            {"start_time": Date(1938)},
            "В 1938 году мы поженились",
        ),
        # a married-name marker written as a suffix: Rudolf + -иха, his
        # wife, as her village calls her (colloquial; never an official name)
        # ADR-0018 routes a relational reference one way, as kinship or as
        # an appellation; this one gives two claims: what she is called, an
        # appellation and no kinship term (referred_to_as), and, since its
        # suffix is a married-name marker, whose wife she is (spouse)
        (
            "nina-sartina",
            "referred_to_as",
            "Рудольфиха",
            "меня так и зовут: Рудольфиха",
        ),
        (
            "nina-sartina",
            "spouse",
            "rudolf-loebig",
            "меня так и зовут: Рудольфиха",
        ),
        # kinship terms with a side ("uncle on the father's side") and a
        # degree ("first cousin"), verbatim; Rudolf is the ego
        (
            "sebastian-loebig",
            "relative",
            "rudolf-loebig",
            {"kinship_type": "дядя по отцу"},
            "Себастьян, его дядя по отцу",
        ),
        (
            "klara-loebig",
            "relative",
            "rudolf-loebig",
            {"kinship_type": "двоюродная сестра"},
            "Клара, его двоюродная сестра",
        ),
        (
            "emigration-1938",
            "participant",
            "klara-loebig",
            "Клара, его двоюродная сестра, что уехала в Австралию",
        ),
        # one surname, masculine (Сартин) and feminine (Сартина)
        ("pavel-sartin", "sibling", "nina-sartina", "Мой брат Павел Сартин"),
        (
            "nina-sartina",
            "residence",
            "koshnovka",
            {"point_in_time": Date(1958, 12, 25)},
            "д. Кошновка Калдовского района Чадской области",
        ),
        # the village, with its district and oblast in the genitive
        (
            "koshnovka",
            "part_of",
            "kaldovsky-district",
            "д. Кошновка Калдовского района Чадской области",
        ),
        (
            "kaldovsky-district",
            "part_of",
            "chadsk-oblast",
            "д. Кошновка Калдовского района Чадской области",
        ),
    )
    + _said(
        "letter-to-sipkje-1952",
        (
            "letter-to-sipkje-1952",
            "point_in_time",
            Date(1952, 1, 24),
            "Snylsjum, 24 jannewaris 1952",
        ),
        (
            "florence-lobig",
            "mother",
            "sipkje-faxridge",
            "dyn dochter Florence",
        ),
        (
            "nicholas-lobig",
            "mother",
            "florence-lobig",
            "Florence har lytse Nicholas",
        ),
        # a kinship term with a degree: Mem is Nicholas's great-grandmother
        (
            "mrs-van-t-nijks",
            "relative",
            "nicholas-lobig",
            {"kinship_type": "oerbeppe"},
            "no is se oerbeppe",
        ),
        # people named only by a kinship term: Mem (mother), heit (father)
        ("sipkje-faxridge", "mother", "mrs-van-t-nijks", "Mem is sa grutsk"),
        (
            "sybren-van-t-nijks",
            "mother",
            "mrs-van-t-nijks",
            "Mem is sa grutsk",
        ),
        ("sipkje-faxridge", "father", "mr-van-t-nijks", "ús sillige heit"),
        ("sybren-van-t-nijks", "father", "mr-van-t-nijks", "ús sillige heit"),
        # "widdo" (widow) and "sillige" (the late)
        (
            "mrs-van-t-nijks",
            "spouse",
            "mr-van-t-nijks",
            "Se is widdo sûnt ús sillige heit stoarn is",
        ),
        (
            "mr-van-t-nijks",
            "date_of_death",
            Date(1952, 1, 24, qualifier="before"),
            "ús sillige heit",
        ),
        # an employer named by its kind and its village
        (
            "snylsjum-brickworks",
            "location",
            "snylsjum",
            "It tichelwurk fan Snylsjum",
        ),
        (
            "mr-van-t-nijks",
            "employer",
            "snylsjum-brickworks",
            "It tichelwurk fan Snylsjum, dêr't heit sa lang wurke hat",
        ),
        (
            "snylsjum-brickworks",
            "end_time",
            Date(1952, 1, 24, qualifier="before"),
            "is no ticht",
        ),
        ("gustav-loebig", "nickname", "Gus", "Florence en Gus"),
        (
            "sybren-van-t-nijks",
            "sibling",
            "sipkje-faxridge",
            "dyn broer Sybren",
        ),
        # the husband's surname, a hyphen, then her own, particle and all
        (
            "sipkje-faxridge",
            "married_name",
            "Sipkje Faxridge",
            "Mefrou S. Faxridge-van 't Nijks",
        ),
        (
            "sipkje-faxridge",
            "birth_name",
            "Sipkje van 't Nijks",
            "Mefrou S. Faxridge-van 't Nijks",
        ),
        (
            "sipkje-faxridge",
            "residence",
            "linden-grove-1",
            {"point_in_time": Date(1952, 1, 24)},
            "1 Linden Grove, Stupfield, Victoria, Austraalje",
        ),
        (
            "sybren-van-t-nijks",
            "residence",
            "snylsjum",
            {"point_in_time": Date(1952, 1, 24)},
            "S. van 't Nijks, Snylsjum",
        ),
        # the sender's village with its municipality, province and country
        (
            "snylsjum",
            "part_of",
            "drakeradiel",
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
        ),
        (
            "drakeradiel",
            "part_of",
            "friesland",
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
        ),
        (
            "friesland",
            "part_of",
            "netherlands",
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
        ),
    )
    + _said(
        "death-certificate-1963",
        (
            "florence-lobig",
            "birth_name",
            "Florence Faxridge",
            "Florence LOBIG, née FAXRIDGE",
        ),
        (
            "florence-lobig",
            "date_of_death",
            Date(1963, 7, 3),
            "Date of death: 3rd July 1963",
        ),
        (
            "florence-lobig",
            "place_of_death",
            "stupfield",
            "Place of death: Stupfield",
        ),
        # the informant's slip: her mother, Sipkje, under Florence's own
        # birth name
        (
            "florence-lobig",
            "mother",
            "sipkje-faxridge",
            "Mother: Florence FAXRIDGE",
        ),
    )
)

# (subject, property) pairs where two sources disagree and nothing in
# the data says which is right: the resting state, not an error.
CONTRADICTIONS = (
    # 7 January 1919 (civil register) vs 1 July 1919 (naturalisation).
    ("gustav-loebig", "date_of_birth"),
    # Kreis Mockburg (1905 certificate) vs Kreis Peiltal (1984 tree).
    ("flaeckfeld", "part_of"),
    # Rahel at Angerring 11 (the letter's excerpt) vs Angerring 71 (the
    # 2011 booklet's machine-assisted translation of it) on 28 July 1939:
    # two witnesses of one text (ADR-0008).  The disagreement report
    # compares a residence only by its dates, so it does not list this one.
    ("rahel-loebig", "residence"),
)

# (subject, property) pairs where one source is vaguer than another but
# they agree: the precise value falls inside the vague one.
REFINEMENTS = (
    # 1909 (gravestone, tree) narrowed to 16 March 1909 (register).
    ("eugen-loebig", "date_of_death"),
    # c. 1833 (tree) and 1833 (gravestone).
    ("caecilie-loebig", "date_of_birth"),
    # between 1938 and 1939 (interview) narrowed to 22 June 1938.
    ("emigration-1938", "point_in_time"),
    # c. September 1939 (interview) narrowed to 3 September 1939.
    ("arrest-1939", "point_in_time"),
    # Peiltal (interview) narrowed to its court prison (card).
    ("imprisonment-1939", "location"),
    # Klurnau (card) narrowed to Lager Klurnau (compensation file).
    ("deportation-1942", "location"),
    # 1914 (tree) narrowed to 29 May 1914 (divorce judgment).
    ("leopold-loebig", "date_of_birth"),
    # 1912 (tree) narrowed to 20 April 1912 (divorce judgment).
    ("agnes-loebig", "date_of_birth"),
    # 1941 (tree) narrowed to 6 March 1941 (certificate), and bounded by
    # the 1958 letter's "the late".
    ("rudolf-loebig", "date_of_death"),
    # 1963 (tree) narrowed to 3 July 1963 (death certificate).
    ("florence-lobig", "date_of_death"),
)

# The naming patterns that extraction's mention records must handle
# (#385), each with the sources and the passages that show it (#387).
# A marker of a marriage, a widow or a death is a claim, never part of a
# name: each passage lies in the excerpt of an assertion its source makes.
NAMING_PATTERNS = {
    "surname first, no comma": (
        ("death-certificate-1941", "Лёбиг Рудольф Феликсович"),
    ),
    "married-name marker": (
        ("letter-to-leopold-1958", "по мужу Лёбиг"),
        ("letter-to-sipkje-1952", "S. Faxridge-van 't Nijks"),
    ),
    "married-name suffix": (("letter-to-leopold-1958", "Рудольфиха"),),
    "masculine and feminine surname": (
        ("letter-to-leopold-1958", "Нина Васильевна Сартина"),
        ("letter-to-leopold-1958", "Павел Сартин"),
    ),
    "name particle": (("letter-to-sipkje-1952", "van 't Nijks"),),
    "widow": (
        ("letter-to-leopold-1958", "вдова"),
        ("letter-to-sipkje-1952", "widdo"),
    ),
    "the late": (
        ("letter-to-leopold-1958", "покойного"),
        ("letter-to-sipkje-1952", "sillige"),
    ),
    "patronymic": (
        ("death-certificate-1941", "Феликсович"),
        ("letter-to-leopold-1958", "Васильевна"),
    ),
    "kinship side": (("letter-to-leopold-1958", "дядя по отцу"),),
    "kinship degree": (
        ("letter-to-leopold-1958", "двоюродная сестра"),
        ("letter-to-sipkje-1952", "oerbeppe"),
    ),
    "institution by kind and place": (
        ("death-certificate-1941", "Калдовское районное бюро ЗАГС"),
        ("letter-to-sipkje-1952", "tichelwurk fan Snylsjum"),
    ),
    "place with containers": (
        (
            "death-certificate-1941",
            "д. Кошновка; район Калдовский; область, край Чадская; "
            "республика РСФСР",
        ),
        (
            "letter-to-leopold-1958",
            "д. Кошновка Калдовского района Чадской области",
        ),
        (
            "letter-to-sipkje-1952",
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
        ),
    ),
}


def by_key() -> dict[str, Person | Place | Entity | Source]:
    """Every person, place, entity and source, keyed by its key."""
    return {obj.key: obj for obj in (*PEOPLE, *PLACES, *ENTITIES, *SOURCES)}


def about(subject: str, prop: str | None = None) -> list[Assertion]:
    """Every assertion about ``subject`` (optionally one property)."""
    return [
        a
        for a in ASSERTIONS
        if a.subject == subject and (prop is None or a.property == prop)
    ]


def containment_paths(place_key: str) -> list[list[str]]:
    """Every chain from ``place_key`` up to a root; several when a place
    has more than one parent."""
    places = {place.key: place for place in PLACES}
    parents = places[place_key].part_of
    if not parents:
        return [[place_key]]
    return [
        [place_key, *path]
        for parent in parents
        for path in containment_paths(parent)
    ]
