"""The fictional example universe (#40): the Löbig family and its world.

Every example in this project's code, comments, prompts, tests, ADRs,
issues and PRs comes from this module and nowhere else (see
``docs/EXAMPLES.md``).  It is plain importable data: people, places,
other entities (a firm, organisations, occupations, events), sources
with transcribed text, and the assertions each source makes, in the
archive's own vocabulary (manifest property ids, entity kinds and
artifact classes).

The family, in four generations:

* Moritz Löbig (1836-1919), master hatter, founds the Hutmacherei zum
  Blauen Hut in 1863 at Brunnenring 35 in Flörburg (Hesse, Germany);
  he marries Charlotte Tarle ("Lotte") of Kredfeld.
* Their children Georg ("Schorsch"), Luise and Ludwig; Georg marries
  Hedwig Laudler, Ludwig marries Katharina Sörne ("Peppi").
* Ludwig and Katharina's son Leopold is arrested in 1939, held in the
  court prison at Peiltal and released; Katharina is deported to Lager
  Klurnau in 1942 and liberated at Klurnau in 1945.
* Georg's children Rosa ("Pünktchen") and Gustav ("Gustl") emigrate to
  Stupfield (Victoria, Australia) in 1938 and 1939 and take the surname
  Lobig; Gustav's son Nicholas, born 1947, is possibly living.
* An unrelated widow in Flörburg shares the name Frieda Löbig with
  Georg's daughter: the homonym.

Flörburg became Bad Flörburg in 1954, so it is named two ways over
time.  Gustav's birth date is deliberately contradictory (6 September
per the civil register, 9 June per his naturalisation: day and month
transposed), and the sources carry every uncertain date form: circa,
before, after, between and decade.

``GEDCOM_FIXTURE`` renders the family as a genealogy program would
export it from these sources; ``tests/unit/test_example_universe.py``
checks that every name, date, place and family link in it is one the
data here asserts.

How it was made (clean room).  The author never saw the project's real
records.  Every specific was drawn by ``tests/fixtures/universe_draws.py``
from broad tables of period given names, ordinary trades, large regions
and syllables, then read over for plausibility: four rounds from
``random.Random(58213)``, each appended after the last so earlier draws
never changed, and a fifth from its own ``random.Random(136490)``.

The fifth round followed the first clean-room review.  It re-drew the
surname, a town, a street and the firm's name; most given names; the
dates of the first two generations, of the firm and of the gravestone
photograph; Susan's and Nicholas's birth years; who was arrested; and
the kinds of three documents.  Its docstring says what it kept.  Its
generation-two births (1860-1866) sit well before the kept third
generation (1907-1925) and the kept wives (born 1884 and 1887), so
Georg and Ludwig are some twenty years older than their wives; the
read-over kept that as drawn.

Hand changes made on the read-overs:

* the third drawn emigration date (October 1939, after the outbreak of
  war) was dropped; the first ship name was skipped as a real surname;
* "Löbig" was anglicised to "Lobig" by dropping the umlaut (the draw
  only said to anglicise);
* the first round's marriage age for Gustav (1938, before he emigrated)
  and his wife's age gap were superseded by third-round draws;
* Katharina's drawn death year was superseded by the "died within a
  year of liberation" draw; Nicholas's birth is dated to the year only
  because he is possibly living;
* document wording, the civil register's filing date (the day after
  the birth) and the postcard's date (a spare day draw) were written
  around the drawn facts; the prison and camp take drawn town names;
  the burial register's age at death follows from the drawn years, and
  the firm's English name translates its drawn house sign.
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
        """The GEDCOM 5.5.1 date phrase, e.g. ``ABT 1840``."""
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
    anglicised, as_written (whole, as one source writes it) or
    placeholder; for places: official, historical, transliterated or
    translated.
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
        "moritz-loebig",
        "Moritz Löbig",
        "male",
        1,
        _names(
            ("Moritz Löbig", "birth", "de"),
            ("Muckel", "nickname", "de"),
        ),
        gedcom_xref="@I1@",
        note="Master hatter; founded the Hutmacherei zum Blauen Hut in 1863.",
    ),
    Person(
        "charlotte-loebig",
        "Charlotte Löbig",
        "female",
        1,
        _names(
            ("Charlotte Tarle", "birth", "de"),
            ("Charlotte Löbig", "married", "de"),
            ("Lotte", "diminutive", "de"),
            ("Charlotte Löbig geb. Tarle", "as_written", "de"),
        ),
        gedcom_xref="@I2@",
        note="Born in Kredfeld; the tree gives her birth year as circa.",
    ),
    Person(
        "georg-loebig",
        "Georg Löbig",
        "male",
        2,
        _names(
            ("Georg Löbig", "birth", "de"),
            ("Schorsch", "diminutive", "de"),
        ),
        gedcom_xref="@I3@",
        note="Owned the firm by 1912, in his father's lifetime; never left.",
    ),
    Person(
        "hedwig-loebig",
        "Hedwig Löbig",
        "female",
        2,
        _names(
            ("Hedwig Laudler", "birth", "de"),
            ("Hedwig Löbig", "married", "de"),
            ("Hedi", "diminutive", "de"),
            ("Hedwig Löbig geborene Laudler", "as_written", "de"),
        ),
        gedcom_xref="@I4@",
    ),
    Person(
        "luise-loebig",
        "Luise Löbig",
        "female",
        2,
        _names(("Luise Löbig", "birth", "de")),
        gedcom_xref="@I5@",
        note="Never married; the tree says only that she died after 1945.",
    ),
    Person(
        "ludwig-loebig",
        "Ludwig Löbig",
        "male",
        2,
        _names(("Ludwig Löbig", "birth", "de")),
        gedcom_xref="@I6@",
        note="Stayed in Flörburg; died in 1942.",
    ),
    Person(
        "katharina-loebig",
        "Katharina Löbig",
        "female",
        2,
        _names(
            ("Katharina Sörne", "birth", "de"),
            ("Katharina Löbig", "married", "de"),
            ("Peppi", "nickname", "de"),
            ("LÖBIG, Katharina, née SÖRNE", "as_written", "en"),
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
        "rosa-loebig",
        "Rosa Löbig",
        "female",
        3,
        _names(
            ("Rosa Löbig", "birth", "de"),
            ("Pünktchen", "nickname", "de"),
            ("Rose Lobig", "anglicised", "en"),
        ),
        gedcom_xref="@I9@",
        note="Emigrated first, in 1938; drew the family tree in 1984.",
    ),
    Person(
        "frieda-loebig",
        "Frieda Löbig",
        "female",
        3,
        _names(("Frieda Löbig", "birth", "de")),
        gedcom_xref="@I10@",
        note="Homonym of the unrelated widow frieda-loebig-angerring.",
    ),
    Person(
        "leopold-loebig",
        "Leopold Löbig",
        "male",
        3,
        _names(("Leopold Löbig", "birth", "de")),
        gedcom_xref="@I11@",
        note="Arrested and imprisoned in 1939, then released.",
    ),
    Person(
        "rudolf-loebig",
        "Rudolf Löbig",
        "male",
        3,
        _names(
            ("Rudolf Löbig", "birth", "de"),
            ("Rudi", "diminutive", "de"),
        ),
        gedcom_xref="@I12@",
    ),
    Person(
        "infant-loebig",
        "Infant Löbig",
        None,
        3,
        _names(("N.N. Löbig", "placeholder", None)),
        gedcom_xref="@I13@",
        note="The tree shows a child who died in 1925: no name, no sex.",
    ),
    Person(
        "susan-lobig",
        "Susan Lobig",
        "female",
        3,
        _names(
            ("Susan Faxridge", "birth", "en"),
            ("Susan Lobig", "married", "en"),
        ),
        gedcom_xref="@I14@",
    ),
    Person(
        "nicholas-lobig",
        "Nicholas Lobig",
        "male",
        4,
        _names(("Nicholas Lobig", "birth", "en")),
        gedcom_xref="@I15@",
        note=(
            "Born 1947 and no source asserts a death: possibly living, "
            "so private by default and dated to the year only."
        ),
    ),
    Person(
        "frieda-loebig-angerring",
        "Frieda Löbig",
        "female",
        None,
        _names(("Löbig, Frieda, Wwe.", "as_written", "de")),
        gedcom_xref="@I16@",
        note=(
            "A widow (Wwe.) listed in the 1912 directory; no relation "
            "to the family is known.  Homonym of frieda-loebig."
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
    Place("angerring-57", "Angerring 57", "address", ("angerring",)),
    Place(
        "floerburg-cemetery",
        "Friedhof Flörburg",
        "cemetery",
        ("floerburg",),
        note="Where Moritz and Charlotte are buried.",
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
        _names(("Australia", "official", "en")),
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
        "Deportation of Katharina Löbig",
        "deportation",
        note="Persecution event: from Flörburg to Lager Klurnau.",
    ),
    Entity(
        "liberation-1945",
        "Liberation of Katharina Löbig",
        "release",
        note="Persecution event: liberation at Klurnau.",
    ),
    Entity("emigration-1938", "Emigration of Rosa Löbig", "migration"),
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
        "atelier-bruesrich",
        "Atelier Brüsrich",
        "organization",
        note="Photographic studio in Peiltal.",
    ),
    Entity(
        "rms-flund",
        "RMS Flund",
        "vessel",
        note="Named in the naturalisation certificate.",
    ),
)

SOURCES = (
    Source(
        "studio-photo-1890s",
        "Studio portrait of the Löbig family",
        None,
        1890,
        "photograph",
        text=(
            "[mount] Atelier Brüsrich · Peiltal "
            "[reverse] Muckel · Lotte · Schorsch · Luise · Ludwig"
        ),
        note="Undated; the studio mount dates it only to the 1890s.",
    ),
    Source(
        "marriage-cert-1905",
        "Marriage certificate of Georg Löbig and Hedwig Laudler",
        "de",
        1905,
        "certificate",
        text=(
            "Standesamt Flörburg. Flörburg, am 1. Februar 1905. Vor dem "
            "Standesbeamten erschienen zum Zwecke der Eheschließung: der "
            "Hutmacher Georg Löbig, geboren am 13. März 1860 zu "
            "Flörburg, Sohn des Hutmachermeisters Moritz Löbig und "
            "dessen verstorbener Ehefrau Charlotte geborene Tarle; und "
            "die Hedwig Laudler, geboren am 10. Juni 1884 zu Fläckfeld, "
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
            "Hutmacherei zum Blauen Hut, gegr. 1863, Inh. Georg Löbig, "
            "Brunnenring 35.\n"
            "Löbig, Frieda, Wwe., Angerring 57."
        ),
        note="Two Löbig entries on one page: the homonym.",
    ),
    Source(
        "burial-register-1919",
        "Burial register of Friedhof Flörburg: Moritz Löbig",
        "de",
        1919,
        "artifact",
        text=(
            "Friedhof Flörburg, Beerdigungsbuch 1919.\n"
            "Name: Löbig, Moritz, Hutmachermeister. Wohnung: Brunnenring "
            "35. Witwer der Charlotte Löbig geb. Tarle. Alter: 83 Jahre.\n"
            "Gestorben: 6. Dezember 1919 in Flörburg. Todesursache: "
            "Wassersucht.\n"
            "Beerdigt: 9. Dezember 1919."
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
            "Oktober 1916, Sohn des Hutmachers Ludwig Löbig, "
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
            "Standesamt Flörburg. Flörburg, am 7. September 1907. Der "
            "Hutmacher Georg Löbig, wohnhaft in Flörburg, Brunnenring "
            "35, zeigte an, daß von seiner Ehefrau Hedwig Löbig "
            "geborene Laudler zu Flörburg in seiner Wohnung am sechsten "
            "September des Jahres tausend neunhundert und sieben ein "
            "Knabe geboren worden sei, welcher den Vornamen Gustav "
            "erhalten habe."
        ),
        note="Extract issued in 1938 for emigration papers.",
    ),
    Source(
        "postcard-1938",
        "Postcard from Georg Löbig to his daughter Rosa",
        "de",
        1938,
        "letter",
        text=(
            "[postmark] Flörburg 2.6.38\n"
            "Fräulein Rosa Löbig, 31 Juniper Street, Stupfield, "
            "Victoria, Australien\n"
            "Liebes Pünktchen! Seit Du am 12. März abgereist bist, ist "
            "es still im Haus. Gustl wartet noch auf sein Visum. Dein "
            "Vater"
        ),
        note="A postcard; only its postmark dates it.",
    ),
    Source(
        "registration-card-1945",
        "Post-war registration card for Katharina Löbig",
        "en",
        1945,
        "artifact",
        text=(
            "LÖBIG, Katharina, née SÖRNE. Born 27.9.1887 at Moshausen. "
            "Last address: Flörburg, Wasserallee 59. Deported 21.8.1942 "
            "to Klurnau. Liberated at Klurnau 28.1.1945. Husband: "
            "Ludwig LÖBIG, died 1942. Son: Leopold LÖBIG, arrested "
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
            "Victoria. Born 9th June 1907 at Floerburg, Germany. "
            "Arrived in Australia 1939 per RMS Flund. Granted 1952."
        ),
        note="Its birth date transposes the register's day and month.",
    ),
    Source(
        "compensation-file-1953",
        "Compensation file for Katharina Löbig",
        "de",
        1953,
        "artifact",
        text=(
            "Wiedergutmachungsamt Peiltal, 1953. Antrag des Leopold "
            "Löbig, Flörburg, für seine verstorbene Mutter Katharina "
            "Löbig geb. Sörne, geb. 27.9.1887, zuletzt wohnhaft in "
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
            "MORITZ LÖBIG\n1836 – 1919\n"
            "CHARLOTTE LÖBIG\nGEB. TARLE\n1840 – 1902"
        ),
        note="The grave is in the Flörburg cemetery.",
    ),
    Source(
        "family-tree-1984",
        "Family tree drawn by Rose Lobig",
        "en",
        1984,
        "genealogical_report",
        text=(
            "Moritz Löbig, b. Bad Flörburg 1836, d. 1919 = (1859) "
            "Charlotte 'Lotte' Tarle of Kredfeld, c. 1840–1902.\n"
            "Their children: Georg 'Schorsch' 1860–1953 = Hedwig 'Hedi' "
            "Laudler of Fläckfeld (Kr. Peiltal) 1884–1968; "
            "Luise 1864 – after 1945; "
            "Ludwig 1866–1942 = Katharina 'Peppi' Sörne 1887–1946.\n"
            "Children of Schorsch and Hedi: Gustav 'Gustl' 1907 = (1945) "
            "Susan Faxridge 1914–1963; Rosa 'Pünktchen' 1910; "
            "Frieda 1911–1941.\n"
            "Children of Ludwig and Peppi: Leopold 1914–1960; "
            "Rudolf 'Rudi' 1916–1941; infant 1925.\n"
            "Children of Gustl and Susan: Nicholas 1947."
        ),
        note="Hand-drawn in Australia by the emigrant Rosa.",
    ),
    Source(
        "interview-2002",
        "Oral-history interview with Nicholas Lobig",
        "en",
        2002,
        None,
        text=(
            "I was born in '47, in Stupfield. Dad — Gus, everyone "
            "called him — left Germany in the August, just before the "
            "war. Aunt Pünktchen had gone ahead in '37 or '38. Dad's "
            "cousin Leopold was taken the first week of the war and "
            "held about a month in Peiltal. The hat shop, the Blue Hat, "
            "was gone before 1926. Dad died in '96, and Aunt Pünktchen "
            "died last year."
        ),
        note="A possibly living narrator; family hearsay throughout.",
    ),
)

ASSERTIONS = tuple(
    _said(
        "studio-photo-1890s",
        ("studio-photo-1890s", "point_in_time", Date(1890, decade=True)),
        ("atelier-bruesrich", "location", "peiltal", "Brüsrich · Peiltal"),
        (
            "studio-photo-1890s",
            "depicts",
            "moritz-loebig",
            {"referred_to_as": "Muckel"},
            "Muckel",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "charlotte-loebig",
            {"referred_to_as": "Lotte"},
            "Lotte",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "georg-loebig",
            {"referred_to_as": "Schorsch"},
            "Schorsch",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "luise-loebig",
            {"referred_to_as": "Luise"},
            "Luise",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "ludwig-loebig",
            {"referred_to_as": "Ludwig"},
            "Ludwig",
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
            "georg-loebig",
            "spouse",
            "hedwig-loebig",
            {"start_time": Date(1905, 2, 1), "location": "floerburg"},
            "Flörburg, am 1. Februar 1905",
        ),
        (
            "georg-loebig",
            "date_of_birth",
            Date(1860, 3, 13),
            "geboren am 13. März 1860 zu Flörburg",
        ),
        (
            "georg-loebig",
            "place_of_birth",
            "floerburg",
            "geboren am 13. März 1860 zu Flörburg",
        ),
        (
            "georg-loebig",
            "occupation",
            "hatter",
            "der Hutmacher Georg Löbig",
        ),
        (
            "georg-loebig",
            "father",
            "moritz-loebig",
            "Sohn des Hutmachermeisters Moritz Löbig",
        ),
        (
            "georg-loebig",
            "mother",
            "charlotte-loebig",
            "Charlotte geborene Tarle",
        ),
        (
            "moritz-loebig",
            "occupation",
            "master-hatter",
            "Hutmachermeisters Moritz Löbig",
        ),
        (
            "charlotte-loebig",
            "birth_name",
            "Charlotte Tarle",
            "Charlotte geborene Tarle",
        ),
        (
            "hedwig-loebig",
            "birth_name",
            "Hedwig Laudler",
            "die Hedwig Laudler",
        ),
        (
            "hedwig-loebig",
            "date_of_birth",
            Date(1884, 6, 10),
            "geboren am 10. Juni 1884 zu Fläckfeld",
        ),
        (
            "hedwig-loebig",
            "place_of_birth",
            "flaeckfeld",
            "geboren am 10. Juni 1884 zu Fläckfeld",
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
            "georg-loebig",
            "employer",
            "zum-blauen-hut",
            {"point_in_time": Date(1912)},
            "Inh. Georg Löbig",
        ),
        ("zum-blauen-hut", "start_time", Date(1863), "gegr. 1863"),
        (
            "frieda-loebig-angerring",
            "residence",
            "angerring-57",
            {"point_in_time": Date(1912)},
            "Löbig, Frieda, Wwe., Angerring 57",
        ),
    )
    + _said(
        "burial-register-1919",
        (
            "moritz-loebig",
            "date_of_death",
            Date(1919, 12, 6),
            "Gestorben: 6. Dezember 1919",
        ),
        (
            "moritz-loebig",
            "place_of_death",
            "floerburg",
            "Gestorben: 6. Dezember 1919 in Flörburg",
        ),
        (
            "moritz-loebig",
            "cause_of_death",
            "Wassersucht",
            "Todesursache: Wassersucht",
        ),
        ("moritz-loebig", "age_at_death", "83 Jahre", "Alter: 83 Jahre"),
        (
            "moritz-loebig",
            "burial_place",
            "floerburg-cemetery",
            {"point_in_time": Date(1919, 12, 9)},
            "Beerdigt: 9. Dezember 1919",
        ),
        (
            "moritz-loebig",
            "occupation",
            "master-hatter",
            "Löbig, Moritz, Hutmachermeister",
        ),
        (
            "moritz-loebig",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1919, 12, 6)},
            "Wohnung: Brunnenring 35",
        ),
        (
            "moritz-loebig",
            "spouse",
            "charlotte-loebig",
            "Witwer der Charlotte Löbig geb. Tarle",
        ),
        (
            "charlotte-loebig",
            "birth_name",
            "Charlotte Tarle",
            "Charlotte Löbig geb. Tarle",
        ),
        (
            "charlotte-loebig",
            "married_name",
            "Charlotte Löbig",
            "Charlotte Löbig geb. Tarle",
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
            "ludwig-loebig",
            "Sohn des Hutmachers Ludwig Löbig",
        ),
        (
            "rudolf-loebig",
            "educated_at",
            "realschule-floerburg",
            {"point_in_time": Date(1930)},
            "Städtische Realschule zu Flörburg",
        ),
        (
            "ludwig-loebig",
            "occupation",
            "hatter",
            "Sohn des Hutmachers Ludwig Löbig",
        ),
        (
            "ludwig-loebig",
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
            Date(1907, 9, 6),
            "am sechsten September des Jahres tausend neunhundert und sieben",
        ),
        (
            "gustav-loebig",
            "place_of_birth",
            "floerburg",
            "zu Flörburg in seiner Wohnung",
        ),
        ("gustav-loebig", "father", "georg-loebig", "Georg Löbig"),
        (
            "gustav-loebig",
            "mother",
            "hedwig-loebig",
            "Hedwig Löbig geborene Laudler",
        ),
        (
            "hedwig-loebig",
            "birth_name",
            "Hedwig Laudler",
            "Hedwig Löbig geborene Laudler",
        ),
        (
            "hedwig-loebig",
            "married_name",
            "Hedwig Löbig",
            "Hedwig Löbig geborene Laudler",
        ),
        (
            "georg-loebig",
            "occupation",
            "hatter",
            "Der Hutmacher Georg Löbig",
        ),
        (
            "georg-loebig",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1907, 9, 7)},
            "wohnhaft in Flörburg, Brunnenring 35",
        ),
    )
    + _said(
        "postcard-1938",
        (
            "postcard-1938",
            "point_in_time",
            Date(1938, 6, 2),
            "[postmark] Flörburg 2.6.38",
        ),
        (
            "georg-loebig",
            "referred_to_as",
            "Dein Vater",
            {"perspective": "rosa-loebig"},
            "Dein Vater",
        ),
        ("rosa-loebig", "father", "georg-loebig", "Dein Vater"),
        ("rosa-loebig", "nickname", "Pünktchen", "Liebes Pünktchen!"),
        (
            "emigration-1938",
            "participant",
            "rosa-loebig",
            "Seit Du am 12. März abgereist bist",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1938, 3, 12),
            "Seit Du am 12. März abgereist bist",
        ),
        (
            "rosa-loebig",
            "residence",
            "juniper-street-31",
            {"point_in_time": Date(1938, 6, 2)},
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
        ("katharina-loebig", "birth_name", "Katharina Sörne", "née SÖRNE"),
        (
            "katharina-loebig",
            "married_name",
            "Katharina Löbig",
            "LÖBIG, Katharina, née SÖRNE",
        ),
        (
            "katharina-loebig",
            "date_of_birth",
            Date(1887, 9, 27),
            "Born 27.9.1887 at Moshausen",
        ),
        (
            "katharina-loebig",
            "place_of_birth",
            "moshausen",
            "Born 27.9.1887 at Moshausen",
        ),
        (
            "katharina-loebig",
            "residence",
            "wasserallee-59",
            "Last address: Flörburg, Wasserallee 59",
        ),
        (
            "deportation-1942",
            "participant",
            "katharina-loebig",
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
            "katharina-loebig",
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
            "katharina-loebig",
            "spouse",
            "ludwig-loebig",
            "Husband: Ludwig LÖBIG",
        ),
        (
            "leopold-loebig",
            "mother",
            "katharina-loebig",
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
            "ludwig-loebig",
            "date_of_death",
            Date(1942),
            "Ludwig LÖBIG, died 1942",
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
            Date(1907, 6, 9),
            "Born 9th June 1907",
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
            "katharina-loebig",
            "für seine verstorbene Mutter",
        ),
        (
            "katharina-loebig",
            "birth_name",
            "Katharina Sörne",
            "Katharina Löbig geb. Sörne",
        ),
        (
            "katharina-loebig",
            "married_name",
            "Katharina Löbig",
            "Katharina Löbig geb. Sörne",
        ),
        (
            "katharina-loebig",
            "date_of_birth",
            Date(1887, 9, 27),
            "geb. 27.9.1887",
        ),
        (
            "katharina-loebig",
            "residence",
            "wasserallee-59",
            "zuletzt wohnhaft in Flörburg, Wasserallee 59",
        ),
        (
            "deportation-1942",
            "participant",
            "katharina-loebig",
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
            "katharina-loebig",
            "date_of_death",
            Date(1946),
            "1946 verstorben",
        ),
    )
    + _said(
        "gravestone-photo-1956",
        ("moritz-loebig", "date_of_birth", Date(1836), "1836 – 1919"),
        ("moritz-loebig", "date_of_death", Date(1919), "1836 – 1919"),
        ("charlotte-loebig", "date_of_birth", Date(1840), "1840 – 1902"),
        ("charlotte-loebig", "date_of_death", Date(1902), "1840 – 1902"),
        (
            "charlotte-loebig",
            "birth_name",
            "Charlotte Tarle",
            "GEB. TARLE",
        ),
        (
            "moritz-loebig",
            "burial_place",
            "floerburg-cemetery",
            "MORITZ LÖBIG",
        ),
        (
            "charlotte-loebig",
            "burial_place",
            "floerburg-cemetery",
            "CHARLOTTE LÖBIG",
        ),
    )
    + _said(
        "family-tree-1984",
        ("moritz-loebig", "number_of_children", 3, "Their children"),
        (
            "moritz-loebig",
            "date_of_birth",
            Date(1836),
            "b. Bad Flörburg 1836",
        ),
        (
            "moritz-loebig",
            "place_of_birth",
            "floerburg",
            {"referred_to_as": "Bad Flörburg"},
            "b. Bad Flörburg 1836",
        ),
        ("moritz-loebig", "date_of_death", Date(1919), "d. 1919"),
        (
            "moritz-loebig",
            "spouse",
            "charlotte-loebig",
            {"start_time": Date(1859)},
            "= (1859) Charlotte 'Lotte' Tarle",
        ),
        (
            "charlotte-loebig",
            "nickname",
            "Lotte",
            "Charlotte 'Lotte' Tarle",
        ),
        (
            "charlotte-loebig",
            "birth_name",
            "Charlotte Tarle",
            "Charlotte 'Lotte' Tarle",
        ),
        (
            "charlotte-loebig",
            "place_of_birth",
            "kredfeld",
            "Tarle of Kredfeld",
        ),
        (
            "charlotte-loebig",
            "date_of_birth",
            Date(1840, qualifier="circa"),
            "c. 1840–1902",
        ),
        (
            "charlotte-loebig",
            "date_of_death",
            Date(1902),
            "c. 1840–1902",
        ),
        ("georg-loebig", "father", "moritz-loebig", "Their children"),
        (
            "georg-loebig",
            "mother",
            "charlotte-loebig",
            "Their children",
        ),
        (
            "georg-loebig",
            "nickname",
            "Schorsch",
            "Georg 'Schorsch' 1860–1953",
        ),
        (
            "georg-loebig",
            "date_of_birth",
            Date(1860),
            "Georg 'Schorsch' 1860–1953",
        ),
        (
            "georg-loebig",
            "date_of_death",
            Date(1953),
            "Georg 'Schorsch' 1860–1953",
        ),
        (
            "georg-loebig",
            "spouse",
            "hedwig-loebig",
            "1860–1953 = Hedwig 'Hedi' Laudler",
        ),
        ("hedwig-loebig", "nickname", "Hedi", "Hedwig 'Hedi' Laudler"),
        (
            "hedwig-loebig",
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
            "hedwig-loebig",
            "date_of_birth",
            Date(1884),
            "(Kr. Peiltal) 1884–1968",
        ),
        (
            "hedwig-loebig",
            "date_of_death",
            Date(1968),
            "(Kr. Peiltal) 1884–1968",
        ),
        ("luise-loebig", "father", "moritz-loebig", "Their children"),
        (
            "luise-loebig",
            "mother",
            "charlotte-loebig",
            "Their children",
        ),
        (
            "luise-loebig",
            "date_of_birth",
            Date(1864),
            "Luise 1864 – after 1945",
        ),
        (
            "luise-loebig",
            "date_of_death",
            Date(1945, qualifier="after"),
            "Luise 1864 – after 1945",
        ),
        ("ludwig-loebig", "father", "moritz-loebig", "Their children"),
        (
            "ludwig-loebig",
            "mother",
            "charlotte-loebig",
            "Their children",
        ),
        ("ludwig-loebig", "date_of_birth", Date(1866), "Ludwig 1866–1942"),
        ("ludwig-loebig", "date_of_death", Date(1942), "Ludwig 1866–1942"),
        (
            "ludwig-loebig",
            "spouse",
            "katharina-loebig",
            "1866–1942 = Katharina 'Peppi' Sörne",
        ),
        (
            "katharina-loebig",
            "nickname",
            "Peppi",
            "Katharina 'Peppi' Sörne",
        ),
        (
            "katharina-loebig",
            "date_of_birth",
            Date(1887),
            "Katharina 'Peppi' Sörne 1887–1946",
        ),
        (
            "katharina-loebig",
            "date_of_death",
            Date(1946),
            "Katharina 'Peppi' Sörne 1887–1946",
        ),
        ("gustav-loebig", "father", "georg-loebig", "Schorsch and Hedi"),
        ("gustav-loebig", "mother", "hedwig-loebig", "Schorsch and Hedi"),
        ("gustav-loebig", "nickname", "Gustl", "Gustav 'Gustl' 1907"),
        ("gustav-loebig", "date_of_birth", Date(1907), "Gustav 'Gustl' 1907"),
        (
            "gustav-loebig",
            "spouse",
            "susan-lobig",
            {"start_time": Date(1945)},
            "= (1945) Susan Faxridge",
        ),
        (
            "susan-lobig",
            "birth_name",
            "Susan Faxridge",
            "Susan Faxridge 1914–1963",
        ),
        (
            "susan-lobig",
            "date_of_birth",
            Date(1914),
            "Susan Faxridge 1914–1963",
        ),
        (
            "susan-lobig",
            "date_of_death",
            Date(1963),
            "Susan Faxridge 1914–1963",
        ),
        ("rosa-loebig", "father", "georg-loebig", "Schorsch and Hedi"),
        ("rosa-loebig", "mother", "hedwig-loebig", "Schorsch and Hedi"),
        ("rosa-loebig", "nickname", "Pünktchen", "Rosa 'Pünktchen' 1910"),
        (
            "rosa-loebig",
            "date_of_birth",
            Date(1910),
            "Rosa 'Pünktchen' 1910",
        ),
        ("frieda-loebig", "father", "georg-loebig", "Schorsch and Hedi"),
        ("frieda-loebig", "mother", "hedwig-loebig", "Schorsch and Hedi"),
        ("frieda-loebig", "date_of_birth", Date(1911), "Frieda 1911–1941"),
        ("frieda-loebig", "date_of_death", Date(1941), "Frieda 1911–1941"),
        ("leopold-loebig", "father", "ludwig-loebig", "Ludwig and Peppi"),
        ("leopold-loebig", "mother", "katharina-loebig", "Ludwig and Peppi"),
        ("leopold-loebig", "date_of_birth", Date(1914), "Leopold 1914–1960"),
        ("leopold-loebig", "date_of_death", Date(1960), "Leopold 1914–1960"),
        (
            "rudolf-loebig",
            "father",
            "ludwig-loebig",
            "Ludwig and Peppi",
        ),
        (
            "rudolf-loebig",
            "mother",
            "katharina-loebig",
            "Ludwig and Peppi",
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
            "ludwig-loebig",
            "Ludwig and Peppi",
        ),
        (
            "infant-loebig",
            "mother",
            "katharina-loebig",
            "Ludwig and Peppi",
        ),
        ("infant-loebig", "date_of_birth", Date(1925), "infant 1925"),
        ("infant-loebig", "date_of_death", Date(1925), "infant 1925"),
        (
            "nicholas-lobig",
            "father",
            "gustav-loebig",
            "Children of Gustl and Susan",
        ),
        (
            "nicholas-lobig",
            "mother",
            "susan-lobig",
            "Children of Gustl and Susan",
        ),
        ("nicholas-lobig", "date_of_birth", Date(1947), "Nicholas 1947"),
    )
    + _said(
        "interview-2002",
        (
            "nicholas-lobig",
            "date_of_birth",
            Date(1947),
            "I was born in '47, in Stupfield",
        ),
        (
            "nicholas-lobig",
            "place_of_birth",
            "stupfield",
            "I was born in '47, in Stupfield",
        ),
        (
            "nicholas-lobig",
            "father",
            "gustav-loebig",
            "Dad — Gus, everyone called him",
        ),
        (
            "gustav-loebig",
            "referred_to_as",
            "Dad",
            {"perspective": "nicholas-lobig"},
            "Dad — Gus",
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
        ("rosa-loebig", "nickname", "Pünktchen", "Aunt Pünktchen"),
        (
            "rosa-loebig",
            "referred_to_as",
            "Aunt Pünktchen",
            {"perspective": "nicholas-lobig"},
            "Aunt Pünktchen",
        ),
        (
            "nicholas-lobig",
            "relative",
            "rosa-loebig",
            {"kinship_type": "aunt"},
            "Aunt Pünktchen",
        ),
        (
            "emigration-1938",
            "participant",
            "rosa-loebig",
            "Aunt Pünktchen had gone ahead in '37 or '38",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1937, qualifier="between", until=Date(1938)),
            "Aunt Pünktchen had gone ahead in '37 or '38",
        ),
        (
            "gustav-loebig",
            "relative",
            "leopold-loebig",
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
            "rosa-loebig",
            "date_of_death",
            Date(2001),
            "Aunt Pünktchen died last year",
        ),
    )
)

# (subject, property) pairs where two sources disagree and nothing in
# the data says which is right: the resting state, not an error.
CONTRADICTIONS = (
    # 6 September 1907 (civil register) vs 9 June 1907 (naturalisation).
    ("gustav-loebig", "date_of_birth"),
    # Kreis Mockburg (1905 certificate) vs Kreis Peiltal (1984 tree).
    ("flaeckfeld", "part_of"),
)

# (subject, property) pairs where one source is vaguer than another but
# they agree: the precise value falls inside the vague one.
REFINEMENTS = (
    # 1919 (gravestone, tree) narrowed to 6 December 1919 (register).
    ("moritz-loebig", "date_of_death"),
    # c. 1840 (tree) and 1840 (gravestone).
    ("charlotte-loebig", "date_of_birth"),
    # between 1937 and 1938 (interview) narrowed to 12 March 1938.
    ("emigration-1938", "point_in_time"),
    # c. September 1939 (interview) narrowed to 3 September 1939.
    ("arrest-1939", "point_in_time"),
    # Peiltal (interview) narrowed to its court prison (card).
    ("imprisonment-1939", "location"),
    # Klurnau (card) narrowed to Lager Klurnau (compensation file).
    ("deportation-1942", "location"),
)


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
