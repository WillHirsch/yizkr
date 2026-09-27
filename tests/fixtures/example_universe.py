"""The one fictional example universe (#40; rule and index: docs/EXAMPLES.md).

Every example in code, comments, prompts, tests, ADRs, issues and PRs comes
from this module: never from real family data, not even partially
anonymised. Everything here is invented, down to the countries.

The universe has a ground truth (who exists, which place contains which) so
tests can check identity and containment decisions against it. What is said
*about* its people is recorded source-first, like the archive itself: every
fact is an :class:`Assertion` by a fictional :class:`Source`, and some facts
are deliberately asserted twice with incompatible values (ADR-0002).

tests/fixtures/example-family.ged is an excerpt of the family tree's GEDCOM
export (source ``family_tree_1990``); a person's ``gedcom_xref`` is its
record there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GEDCOM_FIXTURE = Path(__file__).parent / "example-family.ged"

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class Date:
    """A date exactly as precise as its source says; never narrowed.

    ``qualifier`` carries the source's own uncertainty: ``circa``,
    ``before``, ``after`` or ``between`` (``until`` is then the upper
    bound). ``decade`` means the source gives only the decade of ``year``.
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
        """The GEDCOM 5.5.1 date phrase, as example-family.ged writes it."""
        if self.decade:
            return f"BET {self.year} AND {self.year + 9}"
        plain = self._plain(gedcom=True)
        if self.qualifier == "between" and self.until:
            return f"BET {plain} AND {self.until.gedcom}"
        prefix = {"circa": "ABT ", "before": "BEF ", "after": "AFT "}
        return prefix.get(self.qualifier or "", "") + plain

    def __str__(self) -> str:
        if self.decade:
            return f"the {self.year}s"
        plain = self._plain(gedcom=False)
        if self.qualifier == "between" and self.until:
            return f"between {plain} and {self.until}"
        prefix = {"circa": "c. ", "before": "before ", "after": "after "}
        return prefix.get(self.qualifier or "", "") + plain


@dataclass(frozen=True)
class Name:
    """One form of a name. ``form`` is birth, married, nickname or
    official (the typed kinds, ADR-0011), anglicised (the English form used
    without a legal change), exonym (a place's other-language name) or
    as_written (how one source writes it: composites, suffixes, initials).
    ``lang`` is the language of the form."""

    text: str
    form: str
    lang: str | None = None


@dataclass(frozen=True)
class Person:
    key: str
    label: str
    sex: str | None
    generation: int | None  # 0 = the firm's founder; None = not kin
    names: tuple[Name, ...]
    gedcom_xref: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Place:
    key: str
    label: str
    kind: str  # country, region, town, address, building, camp, ...
    part_of: tuple[str, ...]  # containing places (a DAG, not a tree)
    names: tuple[Name, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Entity:
    """Any other entity; ``kind`` is a manifest item key with
    entity_kind (organization, family, vessel, occupation_class, or an
    event kind such as deportation)."""

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
    artifact_class: str | None = None  # manifest artifact item, if one fits
    text: str = ""  # a short passage, usable as a transcription
    note: str = ""


@dataclass(frozen=True)
class Assertion:
    """What one source says: ``subject`` ``property`` ``value``.

    ``property`` and qualifier keys are manifest property keys
    (src/yizkr/model/manifest.yaml); item-valued ones hold universe keys,
    time-valued ones a :class:`Date`.
    """

    subject: str
    property: str
    value: str | int | Date
    source: str
    qualifiers: tuple[tuple[str, str | Date], ...] = ()
    excerpt: str = ""


def _names(*forms: tuple[str, str, str | None]) -> tuple[Name, ...]:
    return tuple(Name(*form) for form in forms)


def _said(source: str, *rows: tuple) -> list[Assertion]:
    """Rows of (subject, property, value[, {qualifiers}][, excerpt])."""
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


# -- people -------------------------------------------------------------------
# Five generations of the Beispiel family, who became the Examples after
# emigrating from Exampleland (German-speaking) to Havenland (English-
# speaking). Two pairs of homonyms: the two Gustavs (disjoint lifespans)
# and the two Hugos (father and son).

PEOPLE = (
    Person(
        "gustav_1851", "Gustav Beispiel", "male", 0,
        _names(
            ("Gustav Beispiel", "birth", "de"),
            ("G. Beispiel", "as_written", "de"),
        ),
        note="founded the family firm in 1887; homonym of gustav_1925",
    ),
    Person(
        "ottilie", "Ottilie Beispiel", "female", 0,
        _names(
            ("Ottilie Faber", "birth", "de"),
            ("Ottilie Beispiel", "married", "de"),
        ),
    ),
    Person(
        "bern", "Bern Beispiel", "male", 1,
        _names(
            ("Bern Beispiel", "birth", "de"),
            ("Bern Example", "anglicised", "en"),
        ),
        gedcom_xref="@I2@",
        note="cigar maker, not in the family firm; emigrated 1939",
    ),
    Person(
        "alma", "Alma Beispiel", "female", 1,
        _names(
            ("Alma Hedwig Muster", "birth", "de"),
            ("Alma Beispiel", "married", "de"),
            ("Hedi", "nickname", "de"),
            ("Alma Hedwig Beispiel", "as_written", "de"),
        ),
        gedcom_xref="@I1@",
        note="her Muster family is unrelated to Dov Muster's",
    ),
    Person(
        "hugo_sen", "Hugo Beispiel", "male", 1,
        _names(
            ("Hugo Beispiel", "birth", "de"),
            ("Hugo Beispiel sen.", "as_written", "de"),
        ),
        note="the family firm's Prokurist; deported 1942; father of "
        "hugo_jun",
    ),
    Person(
        "leonore", "Leonore Beispiel", "female", 1,
        _names(
            ("Leonore Grau", "birth", "de"),
            ("Leonore Beispiel", "married", "de"),
            ("Lore", "nickname", "de"),
        ),
        note="deported 1942, released 1945; birth date contradicted",
    ),
    Person(
        "hugo_jun", "Hugo Beispiel", "male", 2,
        _names(
            ("Hugo Beispiel", "birth", "de"),
            ("Hugo Beispiel jun.", "as_written", "de"),
            ("Hugo Example", "anglicised", "en"),
        ),
        note="worked in the family firm; emigrated 1938 with wife and "
        "children",
    ),
    Person(
        "gretl", "Margarete Beispiel", "female", 2,
        _names(
            ("Margarete Sommer", "birth", "de"),
            ("Margarete Beispiel", "married", "de"),
            ("Gretl", "nickname", "de"),
            ("Peggy Example", "anglicised", "en"),
        ),
    ),
    Person(
        "carla", "Carla Beispiel", "female", 2,
        _names(
            ("Carla Beispiel", "birth", "de"),
            ("Carla Muster", "married", "de"),
            ("Carla Thorne", "married", "en"),
            ("Carla Beispiel/Muster/Thorne", "as_written", "en"),
        ),
        gedcom_xref="@I3@",
        note="married Dov Muster 1935, divorced 1939; Roland Thorne 1946",
    ),
    Person(
        "dov", "Dov Muster", "male", 2,
        _names(
            ("Dov Muster", "birth", "de"),
            ("Don Masters", "official", "en"),
            ("Dov Muster (later Don Masters)", "as_written", "en"),
        ),
        gedcom_xref="@I4@",
        note="imprisoned November 1938, released January 1939; emigrated "
        "1939",
    ),
    Person(
        "roland", "Roland Thorne", "male", 2,
        _names(("Roland Thorne", "birth", "en")),
    ),
    Person(
        "gustav_1925", "Gustav Beispiel", "male", 3,
        _names(
            ("Gustav Beispiel", "birth", "de"),
            ("Gustl", "nickname", "de"),
            ("Gus", "nickname", "en"),
            ("Gus Example", "anglicised", "en"),
            ("G. Beispiel", "as_written", "de"),
        ),
        note="named after his great-grandfather gustav_1851",
    ),
    Person(
        "irmgard", "Irmgard Example-Hollis", "female", 3,
        _names(
            ("Irmgard Beispiel", "birth", "de"),
            ("Irmi", "nickname", "de"),
            ("Irmgard Example-Hollis", "married", "en"),
            ("Irmgard Example-Hollis (b. Beispiel)", "as_written", "en"),
        ),
    ),
    Person(
        "neville", "Neville Hollis", "male", 3,
        _names(("Neville Hollis", "birth", "en")),
    ),
    Person(
        "ignaz", "Ignaz Beispiel", "male", 3,
        _names(
            ("Ignaz Beispiel", "birth", "de"),
            ("Iggy", "nickname", "en"),
            ("Ian Example", "official", "en"),
            ('Ian "Iggy" Example', "as_written", "en"),
        ),
        note="youngest of three; wrote the 1995 memoir",
    ),
    Person(
        "nora", "Nora Example", "female", 4,
        _names(("Nora Example", "birth", "en")),
        note="possibly living (no death asserted); compiled the tree",
    ),
    Person(
        "friedl", "Elfriede Kranich", "female", None,
        _names(
            ("Elfriede Kranich", "birth", "de"),
            ("Friedl", "nickname", "de"),
            ("Elfreda Crane", "official", "en"),
            ("Elfriede Braun", "married", "de"),
            ("Elfriede (Friedl) Kranich", "as_written", "de"),
        ),
        gedcom_xref="@I6@",
        note="family friend, no relation, yet 'Oma Friedl' to Nora",
    ),
    Person(
        "placeholder", "Nameless Placeholder", None, None,
        _names(("Nameless Placeholder", "as_written", None)),
        gedcom_xref="@I5@",
        note="a record the compiler never filled in; its birth date "
        "('SOMEDAY MAYBE') is unparseable and must never be guessed",
    ),
)  # fmt: skip

# -- places -------------------------------------------------------------------

PLACES = (
    Place(
        "exampleland", "Exampleland", "country", (),
        _names(("Beispielland", "exonym", "de")),
        note="German-speaking; the family's home country",
    ),
    Place(
        "example_county", "Example County", "region", ("exampleland",),
        _names(("Kreis Beispiel", "exonym", "de")),
    ),
    Place(
        "springfield", "Springfield", "town", ("example_county",),
        _names(("Espringfeld", "as_written", "en")),
        note="the family's home town; a 1938 clerk wrote 'Espringfeld'",
    ),
    Place(
        "rosengasse_12", "Rosengasse 12", "address", ("springfield",),
        _names(("Rosengasse 12, Springfield", "as_written", "de")),
        note="Bern and Alma's home (street -> town -> region -> country)",
    ),
    Place(
        "lower_town", "Lower Town", "locality", ("springfield",),
        _names(("Unterstadt", "exonym", "de")),
        note="site of the family firm",
    ),
    Place(
        "springfield_hospital", "Springfield Hospital", "building",
        ("springfield",),
        _names(("Springfield Hospital, Lindenallee", "as_written", "de")),
        note="a birthplace here; an organization when a source treats "
        "it as an agent (ADR-0012)",
    ),
    Place("lakeside", "Lakeside", "town", ("example_county",)),
    Place(
        "lakeside_cemetery", "Lakeside Cemetery", "cemetery",
        ("lakeside",),
    ),
    Place(
        "capital_city_state", "Capital City", "region", ("exampleland",),
        note="a city-state named after its city (eponymous nesting)",
    ),
    Place(
        "capital_city", "Capital City", "town", ("capital_city_state",),
    ),
    Place(
        "capital_city_police_prison", "Capital City police prison",
        "prison", ("capital_city",),
        _names(("Polizeigefängnis Capital City", "exonym", "de")),
    ),
    Place(
        "eastern_province", "Eastern Province", "region", ("exampleland",),
        _names(("Ostprovinz", "exonym", "de")),
    ),
    Place(
        "musterfeld_camp", "Musterfeld camp", "camp",
        ("eastern_province",),
        _names(
            ("Lager Musterfeld", "exonym", "de"),
            ("Musterfeld camp, Exampleland", "as_written", "en"),
        ),
    ),
    Place(
        "havenland", "Havenland", "country", (),
        _names(("Hafenland", "exonym", "de")),
        note="English-speaking; where the family found refuge",
    ),
    Place(
        "westshire", "Westshire", "region", ("havenland",),
        note="historic county",
    ),
    Place(
        "south_westshire", "South Westshire", "region", ("havenland",),
        note="created 1974 from part of Westshire",
    ),
    Place(
        "sampleton", "Sampleton", "town",
        ("westshire", "south_westshire"),
        _names(
            ("Sampleton, Westshire, Havenland", "as_written", "en"),
            ("Sampleton, South Westshire", "as_written", "en"),
        ),
        note="the same town under the old and the 1974 county names",
    ),
    Place(
        "station_road_4", "4 Station Road", "address", ("sampleton",),
        _names(("4 Station Road, Sampleton", "as_written", "en")),
    ),
    Place(
        "laurel_bank", "Laurel Bank Maternity Home", "building",
        ("sampleton",),
    ),
)  # fmt: skip

# -- organisations, family, vessel, occupations, events -----------------------

ENTITIES = (
    Entity(
        "beispiel_soehne", "Beispiel & Söhne", "organization",
        ("Seifenfabrik Beispiel & Söhne", "Seifen=Fabrik Beispiel & Söhne"),
        note="the family firm: a soap works in the Lower Town, founded "
        "1887 by gustav_1851. The '=' form is how a Fraktur double hyphen "
        "OCRs from a scan. Named after people, it is still an agent, never "
        "a place or a human",
    ),
    Entity("example_academy", "Example Academy", "organization"),
    Entity(
        "fire_office", "Example County Fire Office", "organization",
        ("Feuerkasse Kreis Beispiel",),
        note="the county's fire insurer; issuing authority of the 1908 "
        "policy (boilerplate, not a genealogical subject)",
    ),
    Entity(
        "tracing_service", "Exampleland Tracing Service", "organization",
        ("Suchdienst Beispielland",),
    ),
    Entity(
        "refugee_committee", "Sampleton Refugee Committee", "organization",
    ),
    Entity("ss_havenstar", "SS Havenstar", "vessel"),
    Entity("beispiel_family", "the Beispiels", "family", ("the Examples",)),
    Entity(
        "cigar_maker", "Cigar maker", "occupation_class",
        note="the tree's wording",
    ),
    Entity(
        "zigarrenmacher", "Zigarrenmacher", "occupation_class",
        note="the address book's wording for the same trade",
    ),
    Entity(
        "prokurist", "Prokurist", "occupation_class",
        note="authorised signatory: a role, recorded beside the employer",
    ),
    Entity(
        "deportation_1942", "Deportation of Hugo and Leonore Beispiel",
        "deportation",
    ),
    Entity("imprisonment_1938", "Imprisonment of Dov Muster", "imprisonment"),
    Entity("release_1939", "Release of Dov Muster", "release"),
    Entity("release_1945", "Release of Leonore Beispiel", "release"),
    Entity(
        "emigration_1938", "Emigration of the Hugo Beispiel jun. family",
        "migration",
        note="on the SS Havenstar; its date is contradicted",
    ),
)  # fmt: skip

# -- sources ------------------------------------------------------------------

SOURCES = (
    Source(
        "family_tree_1990", "The Example family tree", "en", 1990,
        "genealogical_report",
        note="compiled by Nora Example; example-family.ged is an excerpt "
        "of its GEDCOM export",
    ),
    Source(
        "gravestone_1927", "Gravestone of Gustav and Ottilie Beispiel",
        "de", 1927, "photograph",
        text="Gustav Beispiel 1851 - 27. Januar 1909. "
        "Ottilie Beispiel geb. Faber 185_ - 1927.",
        note="Lakeside Cemetery; the stone is weathered",
    ),
    Source(
        "marriage_register_1900",
        "Springfield civil register: marriage of Hugo Beispiel and "
        "Leonore Grau",
        "de", 1900, "certificate",
    ),
    Source(
        "insurance_1908", "Fire insurance policy of Beispiel & Söhne",
        "de", 1908, "certificate",
        text="Feuerkasse Kreis Beispiel. Versicherungsschein Nr. 4471. "
        "Versichert: Siedehaus und Lager der Seifen=Fabrik Beispiel & Söhne, "
        "Unterstadt, Springfield. Springfield, den 14. April 1908. "
        "ppa. Hugo Beispiel, Prokurist.",
        note="the '=' is a Fraktur double hyphen as OCR reads it",
    ),
    Source(
        "birth_certificate_1929",
        "Springfield civil register: birth of Ignaz Beispiel",
        "de", 1929, "certificate",
        text="Geboren am dritten März neunzehnhundertneunundzwanzig im "
        "Springfield Hospital, Lindenallee: Ignaz Beispiel, Sohn des Hugo "
        "Beispiel und der Margarete Beispiel geb. Sommer.",
    ),
    Source(
        "photo_1929",
        "Photograph: Hugo and Margarete Beispiel with two children",
        None, 1929, "photograph",
        note="caption on the back: 'Hugo, Gretl, Gustl und Irmi, Sommer 1929'",
    ),
    Source(
        "address_book_1932", "Springfield address book", "de", 1932,
        "address_book",
        text="Beispiel, Bern, Zigarrenmacher, Rosengasse 12",
    ),
    Source(
        "passenger_list_1938", "Passenger list of the SS Havenstar", "en",
        1938,
        text="BEISPIEL, Hugo, soap maker, last residence Espringfeld; "
        "destination 4 Station Road, Sampleton, Westshire, Havenland. "
        "With wife Margarete and children Gustav, Irmgard, Ignaz.",
    ),
    Source(
        "deportation_list_1942", "Deportation list, Springfield", "de",
        1942,
        text="Beispiel, Hugo, geb. 1882. "
        "Beispiel, Leonore geb. Grau, geb. 3.11.1881.",
    ),
    Source(
        "naturalisation_1947", "Havenland naturalisation certificates",
        "en", 1947, "certificate",
    ),
    Source(
        "tracing_letter_1950", "Letter from the Exampleland Tracing Service",
        "de", 1950, "letter",
    ),
    Source(
        "gazette_1978", "Sampleton Gazette: interview with Don Masters",
        "en", 1978, "newspaper_clipping",
        text="Don Masters, born Dov Muster in Capital City, was arrested "
        "in November 1938 and held in the city's police prison until "
        "January 1939.",
        note="a recollection forty years on: month precision only",
    ),
    Source(
        "memoir_1995", "Memoir of Ian Example (born Ignaz Beispiel)", "en",
        1995,
        text="I was born in Springfield in 1929, the youngest of three "
        "children. My father worked in the family soap works, and my "
        "earliest memories are of the smell of soap on his coat. My mother "
        "called me Mäuschen. We left for Havenland in 1939. Oma Lore came "
        "back from Musterfeld in 1945; Opa Hugo did not. Großtante Alma "
        "never left Exampleland. Dov Muster (later Don Masters) lodged with "
        "Friedl, who was no relation.",
    ),
    Source(
        "death_notice_2015", "Death notice of Ian Example, Sampleton Gazette",
        "en", 2015, "newspaper_clipping",
        text='EXAMPLE, Ian "Iggy" (born Ignaz Beispiel), of Sampleton, '
        "South Westshire, died peacefully in 2015.",
    ),
)  # fmt: skip

# -- assertions, grouped by the source that makes them ------------------------

ASSERTIONS = tuple(
    _said(
        "family_tree_1990",
        ("hugo_sen", "father", "gustav_1851"),
        ("hugo_sen", "mother", "ottilie"),
        ("bern", "father", "gustav_1851"),
        ("bern", "mother", "ottilie"),
        ("bern", "date_of_birth", Date(1880, qualifier="circa")),
        ("bern", "date_of_death", Date(1950, 3)),
        ("bern", "cause_of_death", "Heart failure"),
        ("bern", "occupation", "cigar_maker"),
        ("bern", "educated_at", "example_academy"),
        ("bern", "spouse", "alma", {"start_time": Date(1902, 4, 2)}),
        ("alma", "birth_name", "Alma Hedwig Muster"),
        ("alma", "married_name", "Alma Beispiel"),
        ("alma", "nickname", "Hedi"),
        ("alma", "date_of_birth", Date(1885, 10, 17)),
        ("alma", "place_of_birth", "springfield"),
        ("alma", "date_of_death", Date(1945, 5, 8, qualifier="before")),
        ("alma", "place_of_death", "lakeside"),
        ("alma", "burial_place", "lakeside_cemetery"),
        ("alma", "number_of_children", 1),
        (
            "carla", "date_of_birth",
            Date(1910, qualifier="between", until=Date(1912)),
        ),
        ("carla", "place_of_birth", "springfield"),
        ("carla", "father", "bern"),
        ("carla", "mother", "alma"),
        (
            "carla", "spouse", "dov",
            {"start_time": Date(1935, 7, 30), "end_time": Date(1939, 10, 10)},
        ),
        ("carla", "married_name", "Carla Muster"),
        (
            "carla", "married_name", "Carla Thorne",
            "Carla Beispiel/Muster/Thorne",
        ),
        ("carla", "spouse", "roland", {"start_time": Date(1946)}),
        ("carla", "date_of_death", Date(1994)),
        ("dov", "date_of_birth", Date(1908)),
        ("dov", "place_of_birth", "capital_city"),
        ("dov", "date_of_death", Date(1979)),
        ("roland", "date_of_birth", Date(1906)),
        ("roland", "place_of_birth", "sampleton"),
        ("roland", "date_of_death", Date(1971)),
        ("leonore", "date_of_death", Date(1961)),
        ("leonore", "place_of_death", "sampleton"),
        ("hugo_jun", "father", "hugo_sen"),
        ("hugo_jun", "mother", "leonore"),
        ("hugo_jun", "date_of_birth", Date(1901, 2, 14)),
        ("hugo_jun", "spouse", "gretl", {"start_time": Date(1924)}),
        ("hugo_jun", "date_of_death", Date(1987)),
        ("hugo_jun", "place_of_death", "sampleton"),
        ("gretl", "date_of_birth", Date(1903)),
        ("gretl", "date_of_death", Date(1990)),
        ("gustav_1925", "father", "hugo_jun"),
        ("gustav_1925", "mother", "gretl"),
        ("gustav_1925", "date_of_birth", Date(1925)),
        ("gustav_1925", "place_of_birth", "springfield"),
        ("gustav_1925", "date_of_death", Date(2004)),
        ("gustav_1925", "place_of_death", "sampleton"),
        ("irmgard", "father", "hugo_jun"),
        ("irmgard", "mother", "gretl"),
        ("irmgard", "date_of_birth", Date(1927)),
        ("irmgard", "spouse", "neville", {"start_time": Date(1950)}),
        ("irmgard", "date_of_death", Date(2011)),
        ("neville", "date_of_birth", Date(1924)),
        ("neville", "date_of_death", Date(1999)),
        ("nora", "father", "gustav_1925"),
        ("nora", "date_of_birth", Date(1958)),
        ("nora", "place_of_birth", "laurel_bank"),
        (
            "leonore", "relative", "nora", {"kinship_type": "Uroma"},
            "our Uroma Lore",
        ),
        ("friedl", "date_of_birth", Date(1912)),
        ("friedl", "date_of_death", Date(1998)),
        ("friedl", "nickname", "Friedl", "Elfriede (Friedl) Kranich"),
        ("friedl", "married_name", "Elfriede Braun"),
        (
            "friedl", "referred_to_as", "Oma Friedl",
            {"perspective": "nora"}, "Oma Friedl (no relation)",
        ),
    )
    + _said(
        "gravestone_1927",
        ("gustav_1851", "date_of_birth", Date(1851)),
        ("gustav_1851", "date_of_death", Date(1909, 1, 27)),
        ("gustav_1851", "burial_place", "lakeside_cemetery"),
        ("ottilie", "birth_name", "Ottilie Faber"),
        ("ottilie", "spouse", "gustav_1851"),
        ("ottilie", "date_of_birth", Date(1850, decade=True), "185_"),
        ("ottilie", "date_of_death", Date(1927)),
        ("ottilie", "burial_place", "lakeside_cemetery"),
    )
    + _said(
        "marriage_register_1900",
        ("hugo_sen", "spouse", "leonore", {"start_time": Date(1900)}),
        ("hugo_sen", "date_of_birth", Date(1882)),
        ("hugo_sen", "father", "gustav_1851"),
        ("hugo_sen", "mother", "ottilie"),
        ("leonore", "birth_name", "Leonore Grau"),
        (
            "leonore", "date_of_birth", Date(1880, 11, 3),
            "geboren am 3. November 1880",
        ),
    )
    + _said(
        "insurance_1908",
        (
            "hugo_sen", "employer", "beispiel_soehne",
            "ppa. Hugo Beispiel, Prokurist",
        ),
        ("hugo_sen", "occupation", "prokurist"),
        ("beispiel_soehne", "location", "lower_town"),
    )
    + _said(
        "birth_certificate_1929",
        ("ignaz", "birth_name", "Ignaz Beispiel"),
        ("ignaz", "date_of_birth", Date(1929, 3, 3)),
        ("ignaz", "place_of_birth", "springfield_hospital"),
        ("ignaz", "father", "hugo_jun", "Sohn des Hugo Beispiel"),
        ("ignaz", "mother", "gretl"),
        (
            "gretl", "birth_name", "Margarete Sommer",
            "Margarete Beispiel geb. Sommer",
        ),
    )
    + _said(
        "photo_1929",
        ("photo_1929", "depicts", "hugo_jun"),
        ("photo_1929", "depicts", "gretl"),
        ("photo_1929", "depicts", "gustav_1925"),
        ("photo_1929", "depicts", "irmgard"),
    )
    + _said(
        "address_book_1932",
        ("bern", "residence", "rosengasse_12", {"point_in_time": Date(1932)}),
        ("bern", "occupation", "zigarrenmacher"),
    )
    + _said(
        "passenger_list_1938",
        ("emigration_1938", "participant", "hugo_jun"),
        ("emigration_1938", "participant", "gretl"),
        ("emigration_1938", "participant", "gustav_1925"),
        ("emigration_1938", "participant", "irmgard"),
        ("emigration_1938", "participant", "ignaz"),
        ("emigration_1938", "location", "sampleton"),
        ("emigration_1938", "point_in_time", Date(1938, 8)),
        (
            "hugo_jun", "residence", "station_road_4",
            {"point_in_time": Date(1938, 8)},
        ),
    )
    + _said(
        "deportation_list_1942",
        ("deportation_1942", "participant", "hugo_sen"),
        ("deportation_1942", "participant", "leonore"),
        ("deportation_1942", "location", "musterfeld_camp"),
        ("deportation_1942", "point_in_time", Date(1942, 10)),
        ("leonore", "date_of_birth", Date(1881, 11, 3), "geb. 3.11.1881"),
    )
    + _said(
        "naturalisation_1947",
        ("ignaz", "official_name", "Ian Example"),
        ("dov", "official_name", "Don Masters"),
        ("friedl", "official_name", "Elfreda Crane"),
    )
    + _said(
        "tracing_letter_1950",
        (
            "hugo_sen", "date_of_death",
            Date(1942, 10, qualifier="after"),
        ),
        ("hugo_sen", "place_of_death", "musterfeld_camp"),
        ("release_1945", "participant", "leonore"),
        ("release_1945", "location", "musterfeld_camp"),
        ("release_1945", "point_in_time", Date(1945, 5)),
    )
    + _said(
        "gazette_1978",
        ("imprisonment_1938", "participant", "dov"),
        ("imprisonment_1938", "location", "capital_city_police_prison"),
        ("imprisonment_1938", "start_time", Date(1938, 11)),
        ("imprisonment_1938", "end_time", Date(1939, 1)),
        ("release_1939", "participant", "dov"),
        ("release_1939", "location", "capital_city_police_prison"),
        ("release_1939", "point_in_time", Date(1939, 1)),
    )
    + _said(
        "memoir_1995",
        ("ignaz", "date_of_birth", Date(1929)),
        ("ignaz", "place_of_birth", "springfield"),
        (
            "hugo_jun", "employer", "beispiel_soehne",
            "My father worked in the family soap works",
        ),
        (
            "emigration_1938", "point_in_time", Date(1939),
            "We left for Havenland in 1939.",
        ),
        (
            "leonore", "relative", "ignaz", {"kinship_type": "Oma"},
            "Oma Lore",
        ),
        (
            "hugo_sen", "relative", "ignaz", {"kinship_type": "Opa"},
            "Opa Hugo",
        ),
        (
            "alma", "relative", "ignaz", {"kinship_type": "Großtante"},
            "Großtante Alma",
        ),
        (
            "ignaz", "referred_to_as", "Mäuschen", {"perspective": "gretl"},
            "My mother called me Mäuschen.",
        ),
        (
            "friedl", "gave_refuge_to", "dov",
            "Dov Muster (later Don Masters) lodged with Friedl",
        ),
        ("friedl", "member_of", "refugee_committee"),
        ("gustav_1925", "nickname", "Gus"),
        ("irmgard", "birth_name", "Irmgard Beispiel"),
        (
            "irmgard", "married_name", "Irmgard Example-Hollis",
            "Irmgard Example-Hollis (b. Beispiel)",
        ),
    )
    + _said(
        "death_notice_2015",
        ("ignaz", "date_of_death", Date(2015)),
        ("ignaz", "place_of_death", "sampleton"),
        ("ignaz", "nickname", "Iggy", 'Ian "Iggy" Example'),
    )
)  # fmt: skip

# One fact, two sources, incompatible values: the healthy resting state the
# archive must display side by side (CLAUDE.md rule 6), never resolve.
CONTRADICTIONS = (
    ("leonore", "date_of_birth"),  # 1880 register vs 1881 deportation list
    ("emigration_1938", "point_in_time"),  # Aug 1938 list vs 1939 memoir
)

# Same fact at different resolutions: NOT a contradiction.
REFINEMENTS = (
    ("ignaz", "date_of_birth"),  # 3 Mar 1929 certificate vs 1929 memoir
    ("ignaz", "place_of_birth"),  # Springfield Hospital vs Springfield
)


def by_key() -> dict[str, Person | Place | Entity | Source]:
    """Every keyed object in the universe."""
    return {obj.key: obj for obj in (*PEOPLE, *PLACES, *ENTITIES, *SOURCES)}


def about(subject: str, prop: str | None = None) -> list[Assertion]:
    """Every assertion about ``subject`` (optionally one property)."""
    return [
        a
        for a in ASSERTIONS
        if a.subject == subject and (prop is None or a.property == prop)
    ]


def containment_paths(place_key: str) -> list[list[str]]:
    """Every part_of path from a place up to a place with no parent."""
    places = {place.key: place for place in PLACES}
    parents = places[place_key].part_of
    if not parents:
        return [[place_key]]
    return [
        [place_key, *path]
        for parent in parents
        for path in containment_paths(parent)
    ]
