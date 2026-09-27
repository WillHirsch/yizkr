"""Seeded random draws behind the fictional example universe (#40).

The example universe in ``example_universe.py`` was written blind: its
author never saw the project's real records.  Instead of recalling
names, dates and places (recall is how real data leaks into examples),
every specific was drawn here from broad tables with a fixed seed and
then read over for plausibility.  Run it to see the raw draws::

    uv run python tests/fixtures/universe_draws.py

The tables are deliberately generic: common given names of the period,
ordinary trades, large regions and countries.  Surnames, towns,
streets, firms and institutions are composed from syllables so that
they are invented rather than remembered.  ``example_universe.py``
notes where a draw was re-rolled or adjusted by hand, and why.

This script is documentation, not a test: nothing imports it.
"""

from __future__ import annotations

import random

SEED = 58213

# --- broad frames ---------------------------------------------------

# (region, country then, country later or None)
ORIGINS = (
    ("Saxony", "Germany", None),
    ("Bavaria", "Germany", None),
    ("Hesse", "Germany", None),
    ("Westphalia", "Germany", None),
    ("Baden", "Germany", None),
    ("Württemberg", "Germany", None),
    ("Thuringia", "Germany", None),
    ("Brandenburg", "Germany", None),
    ("Silesia", "Germany", "Poland"),
    ("Pomerania", "Germany", "Poland"),
    ("Lower Austria", "Austria", None),
    ("Styria", "Austria", None),
)
DESTINATIONS = (
    ("United Kingdom", ("Lancashire", "Yorkshire", "Kent", "Devon")),
    ("United States", ("Ohio", "Pennsylvania", "Illinois", "New York")),
    ("Canada", ("Ontario", "Manitoba", "Quebec")),
    ("Australia", ("Victoria", "New South Wales", "Queensland")),
    ("New Zealand", ("Canterbury", "Otago", "Auckland")),
    ("South Africa", ("Cape Province", "Transvaal", "Natal")),
)
TRADES = (
    "baker",
    "bookbinder",
    "brewer",
    "butcher",
    "cabinetmaker",
    "coal merchant",
    "confectioner",
    "cooper",
    "dyer",
    "furrier",
    "glazier",
    "grain merchant",
    "haberdasher",
    "hatter",
    "ironmonger",
    "locksmith",
    "printer",
    "rope maker",
    "saddler",
    "stationer",
    "tailor",
    "tanner",
    "timber merchant",
    "upholsterer",
    "watchmaker",
    "wine merchant",
    "bicycle dealer",
    "chandler",
)
FIRM_PATTERNS = (
    "{S} & Söhne",
    "Gebrüder {S}",
    "{G} {S} & Co.",
    "{S} & Sohn",
    "{G} {S} Nachf.",
)
RENAMINGS = (
    "merged with a neighbour and renamed",
    "granted spa status and prefixed 'Bad'",
    "renamed by the district authority",
)
HOMONYMS = (
    "grandson named after his grandfather",
    "unrelated namesake in the home town",
    "two cousins both named after their grandmother",
)
CONTRADICTION_KINDS = (
    "year off by one",
    "day and month transposed",
    "different day, same month",
)
AFTERMATHS = (
    "returned to the home town",
    "emigrated to join the family",
    "died within a year of liberation",
)
SOURCE_KINDS = (
    ("civil birth register extract", "de", "certificate"),
    ("civil marriage certificate", "de", "certificate"),
    ("trade directory entry", "de", "directory"),
    ("firm letterhead letter", "de", "letter"),
    ("family letter", "de", "letter"),
    ("studio photograph with caption", None, "photograph"),
    ("passenger list", "en", "list"),
    ("naturalisation certificate", "en", "certificate"),
    ("police prison admission card", "de", "card"),
    ("transport list", "de", "list"),
    ("post-war registration card", "en", "card"),
    ("newspaper death notice", "de", "newspaper"),
    ("hand-drawn family tree", "en", "chart"),
    ("oral-history interview", "en", "interview"),
    ("school report", "de", "certificate"),
    ("gravestone photograph", "de", "photograph"),
)

# --- names ------------------------------------------------------------

# German given name -> (English form, diminutive or None)
MALE = {
    "Wilhelm": ("William", "Willi"),
    "Friedrich": ("Frederick", "Fritz"),
    "Heinrich": ("Henry", "Heinz"),
    "Karl": ("Charles", "Karli"),
    "Johann": ("John", "Hans"),
    "Ludwig": ("Lewis", "Lutz"),
    "Hermann": ("Herman", "Manni"),
    "Ernst": ("Ernest", None),
    "Georg": ("George", "Schorsch"),
    "Paul": ("Paul", None),
    "Emil": ("Emile", None),
    "Gustav": ("Gustave", "Gustl"),
    "Rudolf": ("Rudolph", "Rudi"),
    "Walter": ("Walter", None),
    "Julius": ("Julius", None),
    "Albert": ("Albert", "Berti"),
    "Theodor": ("Theodore", "Theo"),
    "Alfred": ("Alfred", "Fredi"),
    "Leopold": ("Leopold", "Poldi"),
    "Bernhard": ("Bernard", "Bernd"),
    "Eduard": ("Edward", "Edi"),
    "Josef": ("Joseph", "Sepp"),
    "Moritz": ("Maurice", None),
    "Werner": ("Werner", None),
}
FEMALE = {
    "Anna": ("Anne", "Anni"),
    "Margarete": ("Margaret", "Gretel"),
    "Elisabeth": ("Elizabeth", "Liesl"),
    "Katharina": ("Catherine", "Käthe"),
    "Gertrud": ("Gertrude", "Trude"),
    "Hedwig": ("Hedwig", "Hedi"),
    "Johanna": ("Joan", "Hanni"),
    "Wilhelmine": ("Wilhelmina", "Minna"),
    "Frieda": ("Frieda", "Friedl"),
    "Klara": ("Clara", None),
    "Martha": ("Martha", None),
    "Emma": ("Emma", "Emmi"),
    "Luise": ("Louise", None),
    "Charlotte": ("Charlotte", "Lotte"),
    "Helene": ("Helen", "Lene"),
    "Rosa": ("Rose", "Rosl"),
    "Paula": ("Paula", None),
    "Sophie": ("Sophia", None),
    "Irene": ("Irene", "Reni"),
    "Hildegard": ("Hildegard", "Hilde"),
    "Susanne": ("Susan", "Susi"),
    "Lieselotte": ("Liselotte", "Lilo"),
}
ENGLISH_GIVEN = (
    "Susan",
    "Linda",
    "Christine",
    "Janet",
    "Carol",
    "Gillian",
    "Judith",
    "Hilary",
    "Stephen",
    "David",
    "Michael",
    "Peter",
    "Andrew",
    "Philip",
    "Anthony",
    "Nicholas",
    "Jonathan",
    "Martin",
)
PET_NAMES = ("Spatz", "Bubi", "Mausi", "Muckel", "Peppi", "Nuschi")

# --- syllables ----------------------------------------------------------

ONSETS = (
    "B",
    "Br",
    "D",
    "F",
    "Fl",
    "G",
    "Gr",
    "H",
    "K",
    "Kl",
    "Kr",
    "L",
    "M",
    "N",
    "P",
    "Pf",
    "R",
    "S",
    "Sch",
    "Schw",
    "St",
    "T",
    "W",
    "Z",
)
VOWELS = ("a", "e", "i", "o", "u", "ä", "ö", "ü", "ei", "au")
CODAS = (
    "b",
    "ck",
    "d",
    "ff",
    "g",
    "l",
    "ll",
    "m",
    "n",
    "nd",
    "ng",
    "nk",
    "r",
    "rd",
    "rn",
    "rt",
    "s",
    "st",
    "tt",
    "tz",
)
SURNAME_ENDINGS = (
    "",
    "e",
    "el",
    "er",
    "ert",
    "ig",
    "ing",
    "inger",
    "ke",
    "le",
    "ler",
    "mann",
    "ner",
    "old",
    "rich",
    "sch",
)
TOWN_ENDINGS = (
    "au",
    "bach",
    "berg",
    "brück",
    "burg",
    "dorf",
    "feld",
    "hausen",
    "heim",
    "ingen",
    "leben",
    "rode",
    "stedt",
    "tal",
    "witz",
    "wald",
)
STREET_WORDS = (
    "Kessel",
    "Mühlen",
    "Brunnen",
    "Linden",
    "Garten",
    "Kirch",
    "Markt",
    "Schul",
    "Anger",
    "Tor",
    "Wasser",
    "Ziegel",
)
STREET_ENDINGS = ("gasse", "straße", "weg", "ring", "allee", "platz")
POLISH = (
    (
        "B",
        "Ch",
        "D",
        "G",
        "J",
        "K",
        "Kr",
        "M",
        "N",
        "P",
        "Pr",
        "R",
        "S",
        "Sz",
        "W",
        "Z",
    ),
    ("a", "e", "i", "o", "u", "y"),
    ("ł", "l", "r", "n", "sz", "cz", "w", "dz"),
    ("ów", "owo", "ice", "in", "no", "sko", "yn", "ewo"),
)
EN_ONSETS = (
    "B",
    "Bl",
    "Br",
    "C",
    "Ch",
    "D",
    "F",
    "G",
    "Gr",
    "H",
    "K",
    "L",
    "M",
    "N",
    "P",
    "R",
    "S",
    "Sh",
    "St",
    "T",
    "Th",
    "W",
    "Wh",
)
EN_VOWELS = ("a", "e", "i", "o", "u", "ea", "oo", "ar", "or")
EN_CODAS = ("b", "ck", "d", "l", "ll", "m", "n", "nd", "p", "r", "s", "t", "x")
EN_TOWN_ENDINGS = (
    "borough",
    "bury",
    "by",
    "combe",
    "field",
    "ford",
    "ham",
    "hurst",
    "ley",
    "mere",
    "stead",
    "stow",
    "thorpe",
    "ton",
    "well",
    "wick",
)
EN_SURNAME_ENDINGS = (
    "ley",
    "ton",
    "well",
    "wood",
    "by",
    "ham",
    "ridge",
    "field",
    "man",
    "ett",
    "ock",
    "wick",
)
EN_STREET_WORDS = (
    "Alder",
    "Beech",
    "Copper",
    "Elm",
    "Fern",
    "Hawthorn",
    "Juniper",
    "Laurel",
    "Orchard",
    "Quarry",
    "Rowan",
    "Tannery",
    "Willow",
)
EN_STREET_ENDINGS = ("Road", "Street", "Lane", "Avenue", "Terrace")
SHIP_PREFIXES = ("SS", "MV", "RMS")


def main() -> None:
    rng = random.Random(SEED)

    def syllable() -> str:
        return rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(CODAS)

    def surname() -> str:
        return syllable() + rng.choice(SURNAME_ENDINGS)

    def town() -> str:
        return syllable() + rng.choice(TOWN_ENDINGS)

    def en_syllable() -> str:
        return (
            rng.choice(EN_ONSETS)
            + rng.choice(EN_VOWELS)
            + rng.choice(EN_CODAS)
        )

    def date(first: int, last: int, months: int = 12) -> str:
        year = rng.randint(first, last)
        month = rng.randint(1, months)
        day = rng.randint(1, 28)
        return f"{year:04d}-{month:02d}-{day:02d}"

    def show(label: str, value: object) -> None:
        print(f"{label:28} {value}")

    region, country, later = rng.choice(ORIGINS)
    show("origin", (region, country, later))
    dest_country, dest_regions = rng.choice(DESTINATIONS)
    show("destination", (dest_country, rng.choice(dest_regions)))
    show("trade", rng.choice(TRADES))
    show("firm pattern", rng.choice(FIRM_PATTERNS))
    show("renaming", "border change" if later else rng.choice(RENAMINGS))
    show("homonym", rng.choice(HOMONYMS))
    show("contradiction", rng.choice(CONTRADICTION_KINDS))
    show("aftermath", rng.choice(AFTERMATHS))

    show("surnames", [surname() for _ in range(10)])
    show("german towns", [town() for _ in range(8)])
    p_on, p_vo, p_mid, p_end = POLISH
    show(
        "later (slavic) names",
        [
            rng.choice(p_on)
            + rng.choice(p_vo)
            + rng.choice(p_mid)
            + rng.choice(p_end)
            for _ in range(4)
        ],
    )
    show(
        "english towns",
        [en_syllable() + rng.choice(EN_TOWN_ENDINGS) for _ in range(6)],
    )
    show(
        "english surnames",
        [en_syllable() + rng.choice(EN_SURNAME_ENDINGS) for _ in range(6)],
    )
    show(
        "german streets",
        [
            (
                rng.choice(STREET_WORDS) + rng.choice(STREET_ENDINGS),
                rng.randint(1, 60),
            )
            for _ in range(4)
        ],
    )
    show(
        "english streets",
        [
            (
                rng.randint(1, 120),
                rng.choice(EN_STREET_WORDS)
                + " "
                + rng.choice(EN_STREET_ENDINGS),
            )
            for _ in range(3)
        ],
    )
    show(
        "ships",
        [rng.choice(SHIP_PREFIXES) + " " + syllable() for _ in range(3)],
    )
    show("camp/prison stems", [syllable() for _ in range(4)])

    # --- family structure --------------------------------------------
    male = list(MALE)
    female = list(FEMALE)
    rng.shuffle(male)
    rng.shuffle(female)
    show("male given (in order)", male)
    show("female given (in order)", female)
    show("english given", rng.sample(ENGLISH_GIVEN, 6))
    show("pet names", rng.sample(PET_NAMES, 3))

    g1_husband = rng.randint(1848, 1866)
    g1_wife = g1_husband + rng.randint(1, 9)
    marriage = g1_wife + rng.randint(19, 26)
    show("G1 husband born", g1_husband)
    show("G1 wife born", g1_wife)
    show("G1 marriage year", marriage)
    show("G1 husband died", rng.randint(1905, 1932))
    show("G1 wife died", rng.randint(1925, 1942))
    born = marriage + rng.randint(1, 2)
    children = []
    for _ in range(rng.randint(3, 5)):
        if born - g1_wife > 44:
            break
        children.append((born, rng.choice("MF")))
        born += rng.randint(1, 4)
    show("G2 children (year, sex)", children)
    show("G2 child dies young?", rng.random() < 0.4)
    show("G2 second married idx", rng.randrange(len(children)))
    show("G2 spouse age gaps", [rng.randint(-3, 8) for _ in range(3)])
    show("G2 marriage ages", [rng.randint(22, 31) for _ in range(3)])
    show("G3 counts per couple", [rng.randint(1, 3) for _ in range(3)])
    show("G3 birth gaps", [rng.randint(1, 4) for _ in range(6)])
    show("G4 child born", rng.randint(1946, 1958))
    show("G4 child sex", rng.choice("MF"))
    show("anglicise surname?", rng.random() < 0.5)

    # --- events ----------------------------------------------------------
    show("arrest date", date(1936, 1939))
    show("weeks imprisoned", rng.randint(2, 10))
    show("emigration dates", [date(1937, 1939) for _ in range(3)])
    show("deportation date", date(1941, 1943))
    show("liberation date", date(1945, 1945, months=5))
    show("firm founded", rng.randint(1878, 1895))
    show("firm closed", rng.randint(1936, 1939))

    # --- sources ---------------------------------------------------------
    kinds = rng.sample(SOURCE_KINDS, 13)
    show("sources", [kind[0] for kind in kinds])

    second_round(rng, show, date)


def second_round(rng: random.Random, show, date) -> None:
    """Fill the slots the first round left open.

    Written after reading the first round's output and appended after
    it, so every first-round draw is unchanged.  The role lists below
    restate the first round's family (seed 58213): a couple born 1850
    and 1855; sons born 1881 and 1884 who married, a daughter born
    1882; three grandchildren of the elder son, two of the younger.
    """
    grandchildren = [
        "G3a 1907",
        "G3b 1910",
        "G3c 1911",
        "G3d 1914",
        "G3e 1916",
    ]
    show("G3 sexes", [rng.choice("MF") for _ in range(5)])
    first = rng.choice(grandchildren)
    second = rng.choice([g for g in grandchildren if g != first])
    show("emigrant 1938 / 1939", (first, second))
    show("G4 parent", rng.choice((first, second)))
    stayers = [
        "G2 son 1881",
        "G2 wife 1884",
        "G2 daughter 1882",
        "G2 son 1884",
        "G2 wife 1887",
    ] + [g for g in grandchildren if g not in (first, second)]
    show("arrested 1939", rng.choice(stayers))
    show("deported 1942", rng.choice(stayers))
    family = ["G1 husband", "G1 wife", *stayers, first, second]
    show("namesake shares name of", rng.choice(family))
    show("pet-name holders", rng.sample(family, 3))
    show("marriage certificate of", rng.choice(("G2 1881", "G2 1884")))
    show("school report of", rng.choice(grandchildren))
    show(
        "birth month/days",
        [(rng.randint(1, 12), rng.randint(1, 28)) for _ in range(16)],
    )
    show("death years, stayers", [rng.randint(1939, 1970) for _ in stayers])
    show("death years, emigrants", [rng.randint(1960, 2005) for _ in range(3)])
    show("post-liberation death", date(1945, 1946))
    show(
        "G1 death days",
        [(rng.randint(1, 12), rng.randint(1, 28)) for _ in range(2)],
    )
    show(
        "marriage days",
        [(rng.randint(1, 12), rng.randint(1, 28)) for _ in range(3)],
    )
    show("spa renaming year", rng.randint(1920, 1975))
    show("photo decade from", rng.randint(1885, 1899))
    show("directory year", rng.randint(1905, 1936))
    show("birth extract issued", rng.randint(1936, 1939))
    show("school report age", rng.randint(10, 14))
    show("letter year", rng.randint(1937, 1939))
    show("naturalisation year", rng.randint(1944, 1952))
    show("family tree drawn", rng.randint(1965, 1990))
    show("interview year", rng.randint(1995, 2015))
    show("gravestone photographed", rng.randint(1965, 2015))

    third_round(rng, show)


def third_round(rng: random.Random, show) -> None:
    """Fill the slots a read-over of the second round left open.

    The second round made G3a (born 1907) the parent of the child born
    1953, which needs a wife young enough to be that child's mother,
    and it left no person of unknown sex.  Appended as before.
    """
    show("G3a's wife born", rng.randint(1915, 1928))
    show("G3a marries in", rng.randint(1940, 1952))
    show("G3a marriage day", (rng.randint(1, 12), rng.randint(1, 28)))
    show("infant's parents", rng.choice(("G2 1881", "G2 1884")))
    show("infant born after marriage +", rng.randint(1, 12))


if __name__ == "__main__":
    main()
