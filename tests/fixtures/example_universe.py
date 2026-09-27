"""The fictional example universe (#40): the Schöser family and its world.

Every example in this project's code, comments, prompts, tests, ADRs,
issues and PRs comes from this module and nowhere else (see
``docs/EXAMPLES.md``).  It is plain importable data: people, places,
other entities (a firm, organisations, occupations, events), sources
with transcribed text, and the assertions each source makes, in the
archive's own vocabulary (manifest property ids, entity kinds and
artifact classes).

The family, in four generations:

* Alfred Schöser (1850-1927), master hatter, founds the firm Schöser &
  Sohn in 1885 at Brunnenring 35 in Flörburg (Hesse, Germany); he
  marries Wilhelmine Tarle ("Minna") of Dönwald.
* Their children Johann ("Hans"), Paula and Julius; Johann marries
  Hedwig Laudler, Julius marries Gertrud Sörne ("Peppi").
* Julius is arrested in 1939, held in the court prison at Peiltal and
  released; Gertrud is deported to Lager Klurnau in 1942 and liberated
  at Klurnau in 1945.
* Grandchildren Martha ("Bubi") and Josef ("Sepp") emigrate to
  Stupfield (Victoria, Australia) in 1938 and 1939 and take the surname
  Shoser; Josef's son Jonathan, born 1953, is possibly living.
* An unrelated widow in Flörburg shares the name Frieda Schöser with
  Johann's daughter: the homonym.

Flörburg became Bad Flörburg in 1954, so it is named two ways over
time.  Josef's birth date is deliberately contradictory (6 September
per the civil register, 9 June per his naturalisation: day and month
transposed), and the sources carry every uncertain date form: circa,
before, after, between and decade.

``GEDCOM_FIXTURE`` renders the family as a genealogy program would
export it from these sources; ``tests/unit/test_example_universe.py``
checks that every name, date, place and family link in it is one the
data here asserts.

How it was made (clean room).  The author never saw the project's real
records.  Every specific was drawn by ``tests/fixtures/universe_draws.py``
(``random.Random(58213)``, four rounds, each appended after the last
so earlier draws never changed) from broad tables of period given
names, ordinary trades, large regions and syllables, then read over for
plausibility.  Hand changes made on that read-over:

* the third drawn emigration date (October 1939, after the outbreak of
  war) was dropped; the first ship name was skipped as a real surname;
* "Schöser" was anglicised to "Shoser" by hand (the draw only said to
  anglicise), and "Joe" was added as the English diminutive of Joseph;
* the first round's marriage age for Josef (1938, before he emigrated)
  and his wife's age gap were superseded by third-round draws;
* Gertrud's drawn death year was superseded by the "died within a year
  of liberation" draw; Jonathan's drawn birth day is withheld (year
  only) because he is possibly living;
* document wording, the civil register's filing date (the day after
  the birth) and the letter's date (a spare day draw) were written
  around the drawn facts; the prison and camp take drawn town names.
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
        """The GEDCOM 5.5.1 date phrase, e.g. ``ABT 1855``."""
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
        "alfred-schoeser",
        "Alfred Schöser",
        "male",
        1,
        _names(
            ("Alfred Schöser", "birth", "de"),
            ("Muckel", "nickname", "de"),
        ),
        gedcom_xref="@I1@",
        note="Master hatter; founded Schöser & Sohn in 1885.",
    ),
    Person(
        "wilhelmine-schoeser",
        "Wilhelmine Schöser",
        "female",
        1,
        _names(
            ("Wilhelmine Tarle", "birth", "de"),
            ("Wilhelmine Schöser", "married", "de"),
            ("Minna", "diminutive", "de"),
            ("Wilhelmine Schöser geb. Tarle", "as_written", "de"),
        ),
        gedcom_xref="@I2@",
        note="Born in Dönwald; the tree gives her birth year as circa.",
    ),
    Person(
        "johann-schoeser",
        "Johann Schöser",
        "male",
        2,
        _names(
            ("Johann Schöser", "birth", "de"),
            ("Hans", "diminutive", "de"),
        ),
        gedcom_xref="@I3@",
        note="Ran the firm after his father; stayed in Flörburg.",
    ),
    Person(
        "hedwig-schoeser",
        "Hedwig Schöser",
        "female",
        2,
        _names(
            ("Hedwig Laudler", "birth", "de"),
            ("Hedwig Schöser", "married", "de"),
            ("Hedi", "diminutive", "de"),
            ("Hedwig Schöser geborene Laudler", "as_written", "de"),
        ),
        gedcom_xref="@I4@",
    ),
    Person(
        "paula-schoeser",
        "Paula Schöser",
        "female",
        2,
        _names(("Paula Schöser", "birth", "de")),
        gedcom_xref="@I5@",
        note="Never married; the tree says only that she died after 1945.",
    ),
    Person(
        "julius-schoeser",
        "Julius Schöser",
        "male",
        2,
        _names(("Julius Schöser", "birth", "de")),
        gedcom_xref="@I6@",
        note="Arrested and imprisoned in 1939, then released.",
    ),
    Person(
        "gertrud-schoeser",
        "Gertrud Schöser",
        "female",
        2,
        _names(
            ("Gertrud Sörne", "birth", "de"),
            ("Gertrud Schöser", "married", "de"),
            ("Peppi", "nickname", "de"),
            ("SCHÖSER, Gertrud, née SÖRNE", "as_written", "en"),
        ),
        gedcom_xref="@I7@",
        note="Deported to Lager Klurnau in 1942; liberated in 1945.",
    ),
    Person(
        "josef-schoeser",
        "Josef Schöser",
        "male",
        3,
        _names(
            ("Josef Schöser", "birth", "de"),
            ("Sepp", "diminutive", "de"),
            ("Joseph Shoser", "anglicised", "en"),
            ("Joe", "diminutive", "en"),
            ("Joseph SHOSER (formerly Josef SCHÖSER)", "as_written", "en"),
        ),
        gedcom_xref="@I8@",
        note=(
            "Emigrated to Australia in 1939.  His birth date is the "
            "deliberate contradiction."
        ),
    ),
    Person(
        "martha-schoeser",
        "Martha Schöser",
        "female",
        3,
        _names(
            ("Martha Schöser", "birth", "de"),
            ("Bubi", "nickname", "de"),
            ("Martha Shoser", "anglicised", "en"),
        ),
        gedcom_xref="@I9@",
        note="Emigrated first, in 1938; drew the family tree in 1984.",
    ),
    Person(
        "frieda-schoeser",
        "Frieda Schöser",
        "female",
        3,
        _names(("Frieda Schöser", "birth", "de")),
        gedcom_xref="@I10@",
        note="Homonym of the unrelated widow frieda-schoeser-angerring.",
    ),
    Person(
        "karl-schoeser",
        "Karl Schöser",
        "male",
        3,
        _names(("Karl Schöser", "birth", "de")),
        gedcom_xref="@I11@",
    ),
    Person(
        "rudolf-schoeser",
        "Rudolf Schöser",
        "male",
        3,
        _names(
            ("Rudolf Schöser", "birth", "de"),
            ("Rudi", "diminutive", "de"),
        ),
        gedcom_xref="@I12@",
    ),
    Person(
        "infant-schoeser",
        "Infant Schöser",
        None,
        3,
        _names(("N.N. Schöser", "placeholder", None)),
        gedcom_xref="@I13@",
        note="The tree shows a child who died in 1925: no name, no sex.",
    ),
    Person(
        "christine-shoser",
        "Christine Shoser",
        "female",
        3,
        _names(
            ("Christine Faxridge", "birth", "en"),
            ("Christine Shoser", "married", "en"),
        ),
        gedcom_xref="@I14@",
    ),
    Person(
        "jonathan-shoser",
        "Jonathan Shoser",
        "male",
        4,
        _names(("Jonathan Shoser", "birth", "en")),
        gedcom_xref="@I15@",
        note=(
            "Born 1953 and no source asserts a death: possibly living, "
            "so private by default and dated to the year only."
        ),
    ),
    Person(
        "frieda-schoeser-angerring",
        "Frieda Schöser",
        "female",
        None,
        _names(("Schöser, Frieda, Wwe.", "as_written", "de")),
        gedcom_xref="@I16@",
        note=(
            "A widow (Wwe.) listed in the 1912 directory; no relation "
            "to the family is known.  Homonym of frieda-schoeser."
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
            "tree uses the new name even for an 1850 birth."
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
    Place("muehlenweg", "Mühlenweg", "street", ("floerburg",)),
    Place("muehlenweg-16", "Mühlenweg 16", "address", ("muehlenweg",)),
    Place("angerring", "Angerring", "street", ("floerburg",)),
    Place("angerring-57", "Angerring 57", "address", ("angerring",)),
    Place(
        "floerburg-cemetery",
        "Friedhof Flörburg",
        "cemetery",
        ("floerburg",),
        note="Where Alfred and Wilhelmine are buried.",
    ),
    Place("doenwald", "Dönwald", "town", ("kreis-peiltal",)),
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
        "schoeser-und-sohn",
        "Schöser & Sohn",
        "organization",
        ("Schöser and Son",),
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
        "Arrest of Julius Schöser",
        "event",
        note="Persecution event: the arrest.",
    ),
    Entity(
        "imprisonment-1939",
        "Imprisonment of Julius Schöser",
        "imprisonment",
        note="Persecution event: four weeks in the Peiltal court prison.",
    ),
    Entity(
        "release-1939",
        "Release of Julius Schöser",
        "release",
        note="Persecution event: release from prison.",
    ),
    Entity(
        "deportation-1942",
        "Deportation of Gertrud Schöser",
        "deportation",
        note="Persecution event: from Flörburg to Lager Klurnau.",
    ),
    Entity(
        "liberation-1945",
        "Liberation of Gertrud Schöser",
        "release",
        note="Persecution event: liberation at Klurnau.",
    ),
    Entity("emigration-1938", "Emigration of Martha Schöser", "migration"),
    Entity("emigration-1939", "Emigration of Josef Schöser", "migration"),
    Entity("floerburg-registry", "Standesamt Flörburg", "organization"),
    Entity(
        "realschule-floerburg",
        "Städtische Realschule Flörburg",
        "organization",
    ),
    Entity(
        "floerburger-anzeiger",
        "Flörburger Anzeiger",
        "organization",
        note="The local newspaper that printed the 1927 death notice.",
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
        "Studio portrait of the Schöser family",
        None,
        1890,
        "photograph",
        text=(
            "[mount] Atelier Brüsrich · Peiltal "
            "[reverse] Muckel · Minna · Hans · Paula · Julius"
        ),
        note="Undated; the studio mount dates it only to the 1890s.",
    ),
    Source(
        "marriage-cert-1905",
        "Marriage certificate of Johann Schöser and Hedwig Laudler",
        "de",
        1905,
        "certificate",
        text=(
            "Standesamt Flörburg. Flörburg, am 1. Februar 1905. Vor dem "
            "Standesbeamten erschienen zum Zwecke der Eheschließung: der "
            "Hutmacher Johann Schöser, geboren am 13. März 1881 zu "
            "Flörburg, Sohn des Hutmachermeisters Alfred Schöser und "
            "dessen Ehefrau Wilhelmine geborene Tarle; und die Hedwig "
            "Laudler, geboren am 10. Juni 1884 zu Fläckfeld, Kreis "
            "Mockburg."
        ),
    ),
    Source(
        "directory-1912",
        "Adressbuch der Stadt Flörburg 1912",
        "de",
        1912,
        "address_book",
        text=(
            "Schöser & Sohn, Hutmacherei, Inh. Johann Schöser, "
            "Brunnenring 35.\n"
            "Schöser, Frieda, Wwe., Angerring 57."
        ),
        note="Two Schöser entries on one page: the homonym.",
    ),
    Source(
        "death-notice-1927",
        "Death notice for Alfred Schöser, Flörburger Anzeiger",
        "de",
        1927,
        "newspaper_clipping",
        text=(
            "Am 20. März 1927 verschied nach kurzer Krankheit unser "
            "lieber Vater, der Hutmachermeister Alfred Schöser, im 77. "
            "Lebensjahre.\n"
            "Flörburg, Brunnenring 35.\n"
            "In tiefer Trauer: Wilhelmine Schöser geb. Tarle, Johann "
            "Schöser, Paula Schöser, Julius Schöser.\n"
            "Die Beerdigung findet Dienstag, den 22. März, auf dem "
            "Friedhof Flörburg statt."
        ),
    ),
    Source(
        "school-report-1930",
        "School report for Rudolf Schöser",
        "de",
        1930,
        "certificate",
        text=(
            "Städtische Realschule zu Flörburg. Schuljahr 1929/30. "
            "Zeugnis für den Schüler Rudolf Schöser, geb. am 24. "
            "Oktober 1916, Sohn des Hutmachers Julius Schöser, "
            "Flörburg, Mühlenweg 16."
        ),
    ),
    Source(
        "birth-extract-1938",
        "Birth register extract for Josef Schöser",
        "de",
        1938,
        "certificate",
        text=(
            "Standesamt Flörburg. Flörburg, am 7. September 1907. Der "
            "Hutmacher Johann Schöser, wohnhaft in Flörburg, Brunnenring "
            "35, zeigte an, daß von seiner Ehefrau Hedwig Schöser "
            "geborene Laudler zu Flörburg in seiner Wohnung am sechsten "
            "September des Jahres tausend neunhundert und sieben ein "
            "Knabe geboren worden sei, welcher den Vornamen Josef "
            "erhalten habe."
        ),
        note="Extract issued in 1938 for emigration papers.",
    ),
    Source(
        "letter-1938",
        "Letter from Johann Schöser to his daughter Martha",
        "de",
        1938,
        "letter",
        text=(
            "SCHÖSER & SOHN · Hutmacherei · gegr. 1885 · Flörburg, "
            "Brunnenring 35\n"
            "Flörburg, den 2. Juni 1938\n"
            "An Fräulein Martha Schöser, 31 Juniper Street, Stupfield, "
            "Victoria, Australien\n"
            "Liebe Bubi! Seit Du am 12. März abgereist bist, ist es "
            "still im Haus. Sepp wartet noch auf sein Visum. Dein Vater"
        ),
        note="Written on the firm's letterhead.",
    ),
    Source(
        "transport-list-1942",
        "Transport list, Flörburg to Lager Klurnau",
        "de",
        1942,
        "artifact",
        text=(
            "Transport Flörburg – Lager Klurnau, 21. August 1942.\n"
            "Schöser, Gertrud geb. Sörne, geb. 27.9.1887, Flörburg, "
            "Mühlenweg 16."
        ),
    ),
    Source(
        "registration-card-1945",
        "Post-war registration card for Gertrud Schöser",
        "en",
        1945,
        "artifact",
        text=(
            "SCHÖSER, Gertrud, née SÖRNE. Born 27.9.1887 at Moshausen. "
            "Last address: Flörburg, Mühlenweg 16. Deported 21.8.1942 "
            "to Klurnau. Liberated at Klurnau 28.1.1945. Husband: "
            "Julius SCHÖSER, arrested 3.9.1939, held in Peiltal court "
            "prison until 1.10.1939; died 1942."
        ),
    ),
    Source(
        "naturalisation-1952",
        "Certificate of naturalisation for Joseph Shoser",
        "en",
        1952,
        "certificate",
        text=(
            "Certificate of naturalisation. Joseph SHOSER (formerly "
            "Josef SCHÖSER), hatter, of 8 Hawthorn Road, Stupfield, "
            "Victoria. Born 9th June 1907 at Floerburg, Germany. "
            "Arrived in Australia 1939 per RMS Flund. Granted 1952."
        ),
        note="Its birth date transposes the register's day and month.",
    ),
    Source(
        "gravestone-photo-1978",
        "Photograph of the Schöser gravestone, Bad Flörburg",
        "de",
        1978,
        "photograph",
        text=(
            "ALFRED SCHÖSER\n1850 – 1927\n"
            "WILHELMINE SCHÖSER\nGEB. TARLE\n1855 – 1928"
        ),
        note="The grave is in the Flörburg cemetery.",
    ),
    Source(
        "family-tree-1984",
        "Family tree drawn by Martha Shoser",
        "en",
        1984,
        "genealogical_report",
        text=(
            "Alfred Schöser, b. Bad Flörburg 1850, d. 1927 = (1879) "
            "Wilhelmine 'Minna' Tarle of Dönwald, c. 1855–1928.\n"
            "Their children: Johann 'Hans' 1881–1953 = Hedwig 'Hedi' "
            "Laudler of Fläckfeld (Kr. Peiltal) 1884–1968; "
            "Paula 1882 – after 1945; "
            "Julius 1884–1942 = Gertrud 'Peppi' Sörne 1887–1946.\n"
            "Children of Hans and Hedi: Josef 'Sepp' 1907 = (1945) "
            "Christine Faxridge 1923–1963; Martha 'Bubi' 1910; "
            "Frieda 1911–1941.\n"
            "Children of Julius and Peppi: Karl 1914–1960; "
            "Rudolf 'Rudi' 1916–1941; infant 1925.\n"
            "Children of Sepp and Christine: Jonathan 1953."
        ),
        note="Hand-drawn in Australia by the emigrant Martha.",
    ),
    Source(
        "interview-2002",
        "Oral-history interview with Jonathan Shoser",
        "en",
        2002,
        None,
        text=(
            "I was born in '53, in Stupfield. Dad — Joe, everyone "
            "called him — left Germany in the August, just before the "
            "war. Aunt Bubi had gone ahead in '37 or '38. Great-uncle "
            "Julius was taken the first week of the war and held about "
            "a month in Peiltal. The hat shop, Schöser and Son, was "
            "gone before 1940. Dad died in '96, and Aunt Bubi died "
            "last year."
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
            "alfred-schoeser",
            {"referred_to_as": "Muckel"},
            "Muckel",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "wilhelmine-schoeser",
            {"referred_to_as": "Minna"},
            "Minna",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "johann-schoeser",
            {"referred_to_as": "Hans"},
            "Hans",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "paula-schoeser",
            {"referred_to_as": "Paula"},
            "Paula",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "julius-schoeser",
            {"referred_to_as": "Julius"},
            "Julius",
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
            "johann-schoeser",
            "spouse",
            "hedwig-schoeser",
            {"start_time": Date(1905, 2, 1), "location": "floerburg"},
            "Flörburg, am 1. Februar 1905",
        ),
        (
            "johann-schoeser",
            "date_of_birth",
            Date(1881, 3, 13),
            "geboren am 13. März 1881 zu Flörburg",
        ),
        (
            "johann-schoeser",
            "place_of_birth",
            "floerburg",
            "geboren am 13. März 1881 zu Flörburg",
        ),
        (
            "johann-schoeser",
            "occupation",
            "hatter",
            "der Hutmacher Johann Schöser",
        ),
        (
            "johann-schoeser",
            "father",
            "alfred-schoeser",
            "Sohn des Hutmachermeisters Alfred Schöser",
        ),
        (
            "johann-schoeser",
            "mother",
            "wilhelmine-schoeser",
            "Wilhelmine geborene Tarle",
        ),
        (
            "alfred-schoeser",
            "occupation",
            "master-hatter",
            "Hutmachermeisters Alfred Schöser",
        ),
        (
            "wilhelmine-schoeser",
            "birth_name",
            "Wilhelmine Tarle",
            "Wilhelmine geborene Tarle",
        ),
        (
            "hedwig-schoeser",
            "birth_name",
            "Hedwig Laudler",
            "die Hedwig Laudler",
        ),
        (
            "hedwig-schoeser",
            "date_of_birth",
            Date(1884, 6, 10),
            "geboren am 10. Juni 1884 zu Fläckfeld",
        ),
        (
            "hedwig-schoeser",
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
            "schoeser-und-sohn",
            "location",
            "brunnenring-35",
            {"point_in_time": Date(1912)},
            "Brunnenring 35",
        ),
        (
            "johann-schoeser",
            "employer",
            "schoeser-und-sohn",
            {"point_in_time": Date(1912)},
            "Inh. Johann Schöser",
        ),
        (
            "frieda-schoeser-angerring",
            "residence",
            "angerring-57",
            {"point_in_time": Date(1912)},
            "Schöser, Frieda, Wwe., Angerring 57",
        ),
    )
    + _said(
        "death-notice-1927",
        (
            "alfred-schoeser",
            "date_of_death",
            Date(1927, 3, 20),
            "Am 20. März 1927 verschied",
        ),
        (
            "alfred-schoeser",
            "place_of_death",
            "floerburg",
            "Flörburg, Brunnenring 35",
        ),
        (
            "alfred-schoeser",
            "cause_of_death",
            "short illness",
            "nach kurzer Krankheit",
        ),
        (
            "alfred-schoeser",
            "burial_place",
            "floerburg-cemetery",
            {"point_in_time": Date(1927, 3, 22)},
            "Die Beerdigung findet Dienstag, den 22. März, auf dem "
            "Friedhof Flörburg statt",
        ),
        (
            "alfred-schoeser",
            "occupation",
            "master-hatter",
            "der Hutmachermeister Alfred Schöser",
        ),
        (
            "alfred-schoeser",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1927, 3, 20)},
            "Flörburg, Brunnenring 35",
        ),
        (
            "alfred-schoeser",
            "spouse",
            "wilhelmine-schoeser",
            "Wilhelmine Schöser geb. Tarle",
        ),
        (
            "wilhelmine-schoeser",
            "birth_name",
            "Wilhelmine Tarle",
            "Wilhelmine Schöser geb. Tarle",
        ),
        (
            "wilhelmine-schoeser",
            "married_name",
            "Wilhelmine Schöser",
            "Wilhelmine Schöser geb. Tarle",
        ),
        ("johann-schoeser", "father", "alfred-schoeser", "Johann Schöser"),
        ("paula-schoeser", "father", "alfred-schoeser", "Paula Schöser"),
        ("julius-schoeser", "father", "alfred-schoeser", "Julius Schöser"),
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
            "rudolf-schoeser",
            "date_of_birth",
            Date(1916, 10, 24),
            "geb. am 24. Oktober 1916",
        ),
        (
            "rudolf-schoeser",
            "father",
            "julius-schoeser",
            "Sohn des Hutmachers Julius Schöser",
        ),
        (
            "rudolf-schoeser",
            "educated_at",
            "realschule-floerburg",
            {"point_in_time": Date(1930)},
            "Städtische Realschule zu Flörburg",
        ),
        (
            "julius-schoeser",
            "occupation",
            "hatter",
            "Sohn des Hutmachers Julius Schöser",
        ),
        (
            "julius-schoeser",
            "residence",
            "muehlenweg-16",
            {"point_in_time": Date(1930)},
            "Flörburg, Mühlenweg 16",
        ),
    )
    + _said(
        "birth-extract-1938",
        (
            "josef-schoeser",
            "date_of_birth",
            Date(1907, 9, 6),
            "am sechsten September des Jahres tausend neunhundert und sieben",
        ),
        (
            "josef-schoeser",
            "place_of_birth",
            "floerburg",
            "zu Flörburg in seiner Wohnung",
        ),
        ("josef-schoeser", "father", "johann-schoeser", "Johann Schöser"),
        (
            "josef-schoeser",
            "mother",
            "hedwig-schoeser",
            "Hedwig Schöser geborene Laudler",
        ),
        (
            "hedwig-schoeser",
            "birth_name",
            "Hedwig Laudler",
            "Hedwig Schöser geborene Laudler",
        ),
        (
            "hedwig-schoeser",
            "married_name",
            "Hedwig Schöser",
            "Hedwig Schöser geborene Laudler",
        ),
        (
            "johann-schoeser",
            "occupation",
            "hatter",
            "Der Hutmacher Johann Schöser",
        ),
        (
            "johann-schoeser",
            "residence",
            "brunnenring-35",
            {"point_in_time": Date(1907, 9, 7)},
            "wohnhaft in Flörburg, Brunnenring 35",
        ),
    )
    + _said(
        "letter-1938",
        ("letter-1938", "point_in_time", Date(1938, 6, 2), "2. Juni 1938"),
        (
            "johann-schoeser",
            "referred_to_as",
            "Dein Vater",
            {"perspective": "martha-schoeser"},
            "Dein Vater",
        ),
        ("martha-schoeser", "father", "johann-schoeser", "Dein Vater"),
        ("martha-schoeser", "nickname", "Bubi", "Liebe Bubi!"),
        (
            "emigration-1938",
            "participant",
            "martha-schoeser",
            "Seit Du am 12. März abgereist bist",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1938, 3, 12),
            "Seit Du am 12. März abgereist bist",
        ),
        (
            "martha-schoeser",
            "residence",
            "juniper-street-31",
            {"point_in_time": Date(1938, 6, 2)},
            "31 Juniper Street, Stupfield",
        ),
        (
            "josef-schoeser",
            "nickname",
            "Sepp",
            "Sepp wartet noch auf sein Visum",
        ),
        ("schoeser-und-sohn", "start_time", Date(1885), "gegr. 1885"),
        (
            "schoeser-und-sohn",
            "location",
            "brunnenring-35",
            {"point_in_time": Date(1938, 6, 2)},
            "Flörburg, Brunnenring 35",
        ),
    )
    + _said(
        "transport-list-1942",
        (
            "deportation-1942",
            "point_in_time",
            Date(1942, 8, 21),
            "21. August 1942",
        ),
        (
            "deportation-1942",
            "location",
            "klurnau-camp",
            "Transport Flörburg – Lager Klurnau",
        ),
        (
            "deportation-1942",
            "participant",
            "gertrud-schoeser",
            "Schöser, Gertrud geb. Sörne",
        ),
        (
            "gertrud-schoeser",
            "birth_name",
            "Gertrud Sörne",
            "Schöser, Gertrud geb. Sörne",
        ),
        (
            "gertrud-schoeser",
            "date_of_birth",
            Date(1887, 9, 27),
            "geb. 27.9.1887",
        ),
        (
            "gertrud-schoeser",
            "residence",
            "muehlenweg-16",
            {"point_in_time": Date(1942, 8, 21)},
            "Flörburg, Mühlenweg 16",
        ),
    )
    + _said(
        "registration-card-1945",
        ("gertrud-schoeser", "birth_name", "Gertrud Sörne", "née SÖRNE"),
        (
            "gertrud-schoeser",
            "married_name",
            "Gertrud Schöser",
            "SCHÖSER, Gertrud, née SÖRNE",
        ),
        (
            "gertrud-schoeser",
            "date_of_birth",
            Date(1887, 9, 27),
            "Born 27.9.1887 at Moshausen",
        ),
        (
            "gertrud-schoeser",
            "place_of_birth",
            "moshausen",
            "Born 27.9.1887 at Moshausen",
        ),
        (
            "gertrud-schoeser",
            "residence",
            "muehlenweg-16",
            "Last address: Flörburg, Mühlenweg 16",
        ),
        (
            "deportation-1942",
            "participant",
            "gertrud-schoeser",
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
            "gertrud-schoeser",
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
            "gertrud-schoeser",
            "spouse",
            "julius-schoeser",
            "Husband: Julius SCHÖSER",
        ),
        (
            "arrest-1939",
            "participant",
            "julius-schoeser",
            "Julius SCHÖSER, arrested 3.9.1939",
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
            "julius-schoeser",
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
            "julius-schoeser",
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
        ("julius-schoeser", "date_of_death", Date(1942), "died 1942"),
    )
    + _said(
        "naturalisation-1952",
        (
            "josef-schoeser",
            "official_name",
            "Joseph Shoser",
            "Joseph SHOSER",
        ),
        (
            "josef-schoeser",
            "birth_name",
            "Josef Schöser",
            "formerly Josef SCHÖSER",
        ),
        (
            "josef-schoeser",
            "date_of_birth",
            Date(1907, 6, 9),
            "Born 9th June 1907",
        ),
        (
            "josef-schoeser",
            "place_of_birth",
            "floerburg",
            {"referred_to_as": "Floerburg"},
            "at Floerburg, Germany",
        ),
        ("josef-schoeser", "occupation", "hatter", "hatter"),
        (
            "josef-schoeser",
            "residence",
            "hawthorn-road-8",
            {"point_in_time": Date(1952)},
            "8 Hawthorn Road, Stupfield, Victoria",
        ),
        (
            "josef-schoeser",
            "residence",
            "australia",
            {"start_time": Date(1939)},
            "Arrived in Australia 1939",
        ),
        (
            "josef-schoeser",
            "country_of_citizenship",
            "australia",
            {"start_time": Date(1952)},
            "Granted 1952",
        ),
    )
    + _said(
        "gravestone-photo-1978",
        ("alfred-schoeser", "date_of_birth", Date(1850), "1850 – 1927"),
        ("alfred-schoeser", "date_of_death", Date(1927), "1850 – 1927"),
        ("wilhelmine-schoeser", "date_of_birth", Date(1855), "1855 – 1928"),
        ("wilhelmine-schoeser", "date_of_death", Date(1928), "1855 – 1928"),
        (
            "wilhelmine-schoeser",
            "birth_name",
            "Wilhelmine Tarle",
            "GEB. TARLE",
        ),
        (
            "alfred-schoeser",
            "burial_place",
            "floerburg-cemetery",
            "ALFRED SCHÖSER",
        ),
        (
            "wilhelmine-schoeser",
            "burial_place",
            "floerburg-cemetery",
            "WILHELMINE SCHÖSER",
        ),
    )
    + _said(
        "family-tree-1984",
        ("alfred-schoeser", "number_of_children", 3, "Their children"),
        (
            "alfred-schoeser",
            "date_of_birth",
            Date(1850),
            "b. Bad Flörburg 1850",
        ),
        (
            "alfred-schoeser",
            "place_of_birth",
            "floerburg",
            {"referred_to_as": "Bad Flörburg"},
            "b. Bad Flörburg 1850",
        ),
        ("alfred-schoeser", "date_of_death", Date(1927), "d. 1927"),
        (
            "alfred-schoeser",
            "spouse",
            "wilhelmine-schoeser",
            {"start_time": Date(1879)},
            "= (1879) Wilhelmine 'Minna' Tarle",
        ),
        (
            "wilhelmine-schoeser",
            "nickname",
            "Minna",
            "Wilhelmine 'Minna' Tarle",
        ),
        (
            "wilhelmine-schoeser",
            "birth_name",
            "Wilhelmine Tarle",
            "Wilhelmine 'Minna' Tarle",
        ),
        (
            "wilhelmine-schoeser",
            "place_of_birth",
            "doenwald",
            "Tarle of Dönwald",
        ),
        (
            "wilhelmine-schoeser",
            "date_of_birth",
            Date(1855, qualifier="circa"),
            "c. 1855–1928",
        ),
        (
            "wilhelmine-schoeser",
            "date_of_death",
            Date(1928),
            "c. 1855–1928",
        ),
        ("johann-schoeser", "father", "alfred-schoeser", "Their children"),
        (
            "johann-schoeser",
            "mother",
            "wilhelmine-schoeser",
            "Their children",
        ),
        ("johann-schoeser", "nickname", "Hans", "Johann 'Hans' 1881–1953"),
        (
            "johann-schoeser",
            "date_of_birth",
            Date(1881),
            "Johann 'Hans' 1881–1953",
        ),
        (
            "johann-schoeser",
            "date_of_death",
            Date(1953),
            "Johann 'Hans' 1881–1953",
        ),
        (
            "johann-schoeser",
            "spouse",
            "hedwig-schoeser",
            "1881–1953 = Hedwig 'Hedi' Laudler",
        ),
        ("hedwig-schoeser", "nickname", "Hedi", "Hedwig 'Hedi' Laudler"),
        (
            "hedwig-schoeser",
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
            "hedwig-schoeser",
            "date_of_birth",
            Date(1884),
            "(Kr. Peiltal) 1884–1968",
        ),
        (
            "hedwig-schoeser",
            "date_of_death",
            Date(1968),
            "(Kr. Peiltal) 1884–1968",
        ),
        ("paula-schoeser", "father", "alfred-schoeser", "Their children"),
        (
            "paula-schoeser",
            "mother",
            "wilhelmine-schoeser",
            "Their children",
        ),
        (
            "paula-schoeser",
            "date_of_birth",
            Date(1882),
            "Paula 1882 – after 1945",
        ),
        (
            "paula-schoeser",
            "date_of_death",
            Date(1945, qualifier="after"),
            "Paula 1882 – after 1945",
        ),
        ("julius-schoeser", "father", "alfred-schoeser", "Their children"),
        (
            "julius-schoeser",
            "mother",
            "wilhelmine-schoeser",
            "Their children",
        ),
        ("julius-schoeser", "date_of_birth", Date(1884), "Julius 1884–1942"),
        ("julius-schoeser", "date_of_death", Date(1942), "Julius 1884–1942"),
        (
            "julius-schoeser",
            "spouse",
            "gertrud-schoeser",
            "1884–1942 = Gertrud 'Peppi' Sörne",
        ),
        (
            "gertrud-schoeser",
            "nickname",
            "Peppi",
            "Gertrud 'Peppi' Sörne",
        ),
        (
            "gertrud-schoeser",
            "date_of_birth",
            Date(1887),
            "Gertrud 'Peppi' Sörne 1887–1946",
        ),
        (
            "gertrud-schoeser",
            "date_of_death",
            Date(1946),
            "Gertrud 'Peppi' Sörne 1887–1946",
        ),
        ("josef-schoeser", "father", "johann-schoeser", "Hans and Hedi"),
        ("josef-schoeser", "mother", "hedwig-schoeser", "Hans and Hedi"),
        ("josef-schoeser", "nickname", "Sepp", "Josef 'Sepp' 1907"),
        ("josef-schoeser", "date_of_birth", Date(1907), "Josef 'Sepp' 1907"),
        (
            "josef-schoeser",
            "spouse",
            "christine-shoser",
            {"start_time": Date(1945)},
            "= (1945) Christine Faxridge",
        ),
        (
            "christine-shoser",
            "birth_name",
            "Christine Faxridge",
            "Christine Faxridge 1923–1963",
        ),
        (
            "christine-shoser",
            "date_of_birth",
            Date(1923),
            "Christine Faxridge 1923–1963",
        ),
        (
            "christine-shoser",
            "date_of_death",
            Date(1963),
            "Christine Faxridge 1923–1963",
        ),
        ("martha-schoeser", "father", "johann-schoeser", "Hans and Hedi"),
        ("martha-schoeser", "mother", "hedwig-schoeser", "Hans and Hedi"),
        ("martha-schoeser", "nickname", "Bubi", "Martha 'Bubi' 1910"),
        (
            "martha-schoeser",
            "date_of_birth",
            Date(1910),
            "Martha 'Bubi' 1910",
        ),
        ("frieda-schoeser", "father", "johann-schoeser", "Hans and Hedi"),
        ("frieda-schoeser", "mother", "hedwig-schoeser", "Hans and Hedi"),
        ("frieda-schoeser", "date_of_birth", Date(1911), "Frieda 1911–1941"),
        ("frieda-schoeser", "date_of_death", Date(1941), "Frieda 1911–1941"),
        ("karl-schoeser", "father", "julius-schoeser", "Julius and Peppi"),
        ("karl-schoeser", "mother", "gertrud-schoeser", "Julius and Peppi"),
        ("karl-schoeser", "date_of_birth", Date(1914), "Karl 1914–1960"),
        ("karl-schoeser", "date_of_death", Date(1960), "Karl 1914–1960"),
        (
            "rudolf-schoeser",
            "father",
            "julius-schoeser",
            "Julius and Peppi",
        ),
        (
            "rudolf-schoeser",
            "mother",
            "gertrud-schoeser",
            "Julius and Peppi",
        ),
        ("rudolf-schoeser", "nickname", "Rudi", "Rudolf 'Rudi' 1916–1941"),
        (
            "rudolf-schoeser",
            "date_of_birth",
            Date(1916),
            "Rudolf 'Rudi' 1916–1941",
        ),
        (
            "rudolf-schoeser",
            "date_of_death",
            Date(1941),
            "Rudolf 'Rudi' 1916–1941",
        ),
        (
            "infant-schoeser",
            "father",
            "julius-schoeser",
            "Julius and Peppi",
        ),
        (
            "infant-schoeser",
            "mother",
            "gertrud-schoeser",
            "Julius and Peppi",
        ),
        ("infant-schoeser", "date_of_birth", Date(1925), "infant 1925"),
        ("infant-schoeser", "date_of_death", Date(1925), "infant 1925"),
        (
            "jonathan-shoser",
            "father",
            "josef-schoeser",
            "Children of Sepp and Christine",
        ),
        (
            "jonathan-shoser",
            "mother",
            "christine-shoser",
            "Children of Sepp and Christine",
        ),
        ("jonathan-shoser", "date_of_birth", Date(1953), "Jonathan 1953"),
    )
    + _said(
        "interview-2002",
        (
            "jonathan-shoser",
            "date_of_birth",
            Date(1953),
            "I was born in '53, in Stupfield",
        ),
        (
            "jonathan-shoser",
            "place_of_birth",
            "stupfield",
            "I was born in '53, in Stupfield",
        ),
        (
            "jonathan-shoser",
            "father",
            "josef-schoeser",
            "Dad — Joe, everyone called him",
        ),
        (
            "josef-schoeser",
            "referred_to_as",
            "Dad",
            {"perspective": "jonathan-shoser"},
            "Dad — Joe",
        ),
        ("josef-schoeser", "nickname", "Joe", "Joe, everyone called him"),
        (
            "emigration-1939",
            "participant",
            "josef-schoeser",
            "left Germany in the August, just before the war",
        ),
        (
            "emigration-1939",
            "point_in_time",
            Date(1939, 8),
            "left Germany in the August, just before the war",
        ),
        ("martha-schoeser", "nickname", "Bubi", "Aunt Bubi"),
        (
            "martha-schoeser",
            "referred_to_as",
            "Aunt Bubi",
            {"perspective": "jonathan-shoser"},
            "Aunt Bubi",
        ),
        (
            "jonathan-shoser",
            "relative",
            "martha-schoeser",
            {"kinship_type": "aunt"},
            "Aunt Bubi",
        ),
        (
            "emigration-1938",
            "participant",
            "martha-schoeser",
            "Aunt Bubi had gone ahead in '37 or '38",
        ),
        (
            "emigration-1938",
            "point_in_time",
            Date(1937, qualifier="between", until=Date(1938)),
            "Aunt Bubi had gone ahead in '37 or '38",
        ),
        (
            "julius-schoeser",
            "referred_to_as",
            "Great-uncle Julius",
            {"perspective": "jonathan-shoser"},
            "Great-uncle Julius",
        ),
        (
            "jonathan-shoser",
            "relative",
            "julius-schoeser",
            {"kinship_type": "great-uncle"},
            "Great-uncle Julius",
        ),
        (
            "arrest-1939",
            "participant",
            "julius-schoeser",
            "Great-uncle Julius was taken",
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
            "schoeser-und-sohn",
            "referred_to_as",
            "Schöser and Son",
            "The hat shop, Schöser and Son",
        ),
        (
            "schoeser-und-sohn",
            "end_time",
            Date(1940, qualifier="before"),
            "was gone before 1940",
        ),
        ("josef-schoeser", "date_of_death", Date(1996), "Dad died in '96"),
        (
            "martha-schoeser",
            "date_of_death",
            Date(2001),
            "Aunt Bubi died last year",
        ),
    )
)

# (subject, property) pairs where two sources disagree and nothing in
# the data says which is right: the resting state, not an error.
CONTRADICTIONS = (
    # 6 September 1907 (civil register) vs 9 June 1907 (naturalisation).
    ("josef-schoeser", "date_of_birth"),
    # Kreis Mockburg (1905 certificate) vs Kreis Peiltal (1984 tree).
    ("flaeckfeld", "part_of"),
)

# (subject, property) pairs where one source is vaguer than another but
# they agree: the precise value falls inside the vague one.
REFINEMENTS = (
    # 1927 (gravestone, tree) narrowed to 20 March 1927 (death notice).
    ("alfred-schoeser", "date_of_death"),
    # c. 1855 (tree) and 1855 (gravestone).
    ("wilhelmine-schoeser", "date_of_birth"),
    # between 1937 and 1938 (interview) narrowed to 12 March 1938.
    ("emigration-1938", "point_in_time"),
    # c. September 1939 (interview) narrowed to 3 September 1939.
    ("arrest-1939", "point_in_time"),
    # Peiltal (interview) narrowed to its court prison (card).
    ("imprisonment-1939", "location"),
    # Klurnau (card) narrowed to Lager Klurnau (transport list).
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
