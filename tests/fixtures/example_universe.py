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
  Lager Klurnau in 1942 and liberated at Klurnau in 1945.
* Sebastian's children Klara ("Pünktchen") and Gustav ("Gustl")
  emigrate to Stupfield (Victoria, Australia) in 1938 and 1939 and take
  the surname Lobig; Gustav marries Iris Faxridge, and their son
  Nicholas, born 1950, is possibly living.
* An unrelated widow in Flörburg shares the name Ursula Löbig with
  Sebastian's daughter: the homonym.

Flörburg became Bad Flörburg in 1954, so it is named two ways over
time.  Gustav's birth date is deliberately contradictory (7 January
per the civil register, 1 July per his naturalisation: day and month
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
never changed, then a fifth, sixth and seventh from their own
``random.Random(136490)``, ``random.Random(238286)`` and
``random.Random(559179)``.

The fifth round followed the first clean-room review.  It re-drew the
surname, a town, a street and the firm's name; most given names; the
dates of the first two generations, of the firm and of the gravestone
photograph; the birth years of Gustav's wife and son; who was arrested;
and the kinds of three documents.

The sixth round followed the second review.  From a wider table of
names it re-drew the given names of the first two generations, of the
emigrant daughter and of Iris; the second generation's birth years; a
wife's birth date; Eugen's death date; the days of the 1905 marriage
and of two births; and the form of the studio's name.

The seventh round followed the third review.  It re-drew the given
names of both second-generation wives, of the emigrant daughter and of
the two Ursulas; the birth years of Cäcilie, of Sebastian, Felix and
their wives, of Gustav, Klara and the infant; Sebastian's and Felix's
death years; Eugen's death date, which falls between the 1905 marriage
certificate (it names him living) and the 1912 directory (his son owns
the firm); Klara's departure and the postmark after it; and the years
of Gustav's marriage and of his son's birth.  Each round's docstring
says what it kept.  Sebastian and Felix (born 1862 and 1867) are each
fourteen years older than their wives (born 1876 and 1881) and have
children into their fifties; the read-over kept that as drawn.

Hand changes made on the read-overs:

* the third drawn emigration date (October 1939, after the outbreak of
  war) was dropped; the first ship name was skipped as a real surname;
* "Löbig" was anglicised to "Lobig" by dropping the umlaut (the draw
  only said to anglicise);
* the first round's marriage age for Gustav and his wife's age gap were
  superseded by later draws;
* Ida's drawn death year was superseded by the "died within a year of
  liberation" draw; Nicholas's birth is dated to the year only because
  he is possibly living;
* document wording and the civil register's filing date (the day after
  the birth) were written around the drawn facts; the prison and camp
  take drawn town names; the burial register's age at death follows
  from the drawn years, and the firm's English name translates its
  drawn house sign;
* the sixth round's name check gained its compound rule before its
  draws were used: a first run gave the first-generation wife a
  compound name that holds a diminutive an earlier round had used;
* the seventh round's checks gained their read-over rules before its
  draws were used.  A first run named the emigrant daughter Auguste,
  which shares "gust" with her brother Gustav's name; put Gustav in
  the emigrant daughter's old year; put the infant fifteen years before
  his brothers; and had Felix die in a year two of the family died.  A
  second run gave Gustav's mother Irene beside his wife Iris.  So a new
  name shares no four letters in a row with a used one and no first two
  letters with one in the cast, neither emigrant grandchild comes near
  either emigrant's old year, siblings are one to six years apart, and
  neither brother dies in a year another of the family dies;
* the interview's vague years became '38 or '39, the only other pair
  of consecutive years that holds the 1938 departure;
* the GEDCOM export no longer renders Cäcilie's undated burial (the
  gravestone photograph still asserts it); a clause added to the
  interview gives Gustav's burial place instead, so the export's
  undated, place-only burial is his.
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
        note="The tree shows a child who died in 1922: no name, no sex.",
    ),
    Person(
        "iris-lobig",
        "Iris Lobig",
        "female",
        3,
        _names(
            ("Iris Faxridge", "birth", "en"),
            ("Iris Lobig", "married", "en"),
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
)

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
            "Iris Faxridge 1914–1963.\n"
            "Children of Felix and Peppi: Leopold 1914–1960; "
            "Rudolf 'Rudi' 1916–1941; infant 1922.\n"
            "Children of Gustl and Iris: Nicholas 1950."
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
            "I was born in '50, in Stupfield. Dad — Gus, everyone "
            "called him — left Germany in the August, just before the "
            "war. Aunt Pünktchen had gone ahead in '38 or '39. Dad's "
            "cousin Leopold was taken the first week of the war and "
            "held about a month in Peiltal. The hat shop, the Blue Hat, "
            "was gone before 1926. Dad died in '96 and he's buried here "
            "in Stupfield. Aunt Pünktchen died last year."
        ),
        note="A possibly living narrator; family hearsay throughout.",
    ),
)

ASSERTIONS = tuple(
    _said(
        "studio-photo-1890s",
        ("studio-photo-1890s", "point_in_time", Date(1890, decade=True)),
        ("studio-bruesrich", "location", "peiltal", "Brüsrich · Peiltal"),
        (
            "studio-photo-1890s",
            "depicts",
            "eugen-loebig",
            {"referred_to_as": "Muckel"},
            "Muckel",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "caecilie-loebig",
            {"referred_to_as": "Cilli"},
            "Cilli",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "sebastian-loebig",
            {"referred_to_as": "Wastl"},
            "Wastl",
        ),
        (
            "studio-photo-1890s",
            "depicts",
            "rahel-loebig",
            {"referred_to_as": "Rahel"},
            "Rahel",
        ),
        (
            "studio-photo-1890s",
            "depicts",
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
        (
            "sebastian-loebig",
            "referred_to_as",
            "Dein Vater",
            {"perspective": "klara-loebig"},
            "Dein Vater",
        ),
        ("klara-loebig", "father", "sebastian-loebig", "Dein Vater"),
        ("klara-loebig", "nickname", "Pünktchen", "Liebes Pünktchen!"),
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
            "iris-lobig",
            {"start_time": Date(1942)},
            "= (1942) Iris Faxridge",
        ),
        (
            "iris-lobig",
            "birth_name",
            "Iris Faxridge",
            "Iris Faxridge 1914–1963",
        ),
        (
            "iris-lobig",
            "date_of_birth",
            Date(1914),
            "Iris Faxridge 1914–1963",
        ),
        (
            "iris-lobig",
            "date_of_death",
            Date(1963),
            "Iris Faxridge 1914–1963",
        ),
        ("leopold-loebig", "father", "felix-loebig", "Felix and Peppi"),
        ("leopold-loebig", "mother", "ida-loebig", "Felix and Peppi"),
        ("leopold-loebig", "date_of_birth", Date(1914), "Leopold 1914–1960"),
        ("leopold-loebig", "date_of_death", Date(1960), "Leopold 1914–1960"),
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
            "Children of Gustl and Iris",
        ),
        (
            "nicholas-lobig",
            "mother",
            "iris-lobig",
            "Children of Gustl and Iris",
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
        ("klara-loebig", "nickname", "Pünktchen", "Aunt Pünktchen"),
        (
            "klara-loebig",
            "referred_to_as",
            "Aunt Pünktchen",
            {"perspective": "nicholas-lobig"},
            "Aunt Pünktchen",
        ),
        (
            "nicholas-lobig",
            "relative",
            "klara-loebig",
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
)

# (subject, property) pairs where two sources disagree and nothing in
# the data says which is right: the resting state, not an error.
CONTRADICTIONS = (
    # 7 January 1919 (civil register) vs 1 July 1919 (naturalisation).
    ("gustav-loebig", "date_of_birth"),
    # Kreis Mockburg (1905 certificate) vs Kreis Peiltal (1984 tree).
    ("flaeckfeld", "part_of"),
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
