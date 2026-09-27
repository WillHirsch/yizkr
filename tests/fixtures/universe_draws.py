"""Seeded random draws behind the fictional example universe (#40).

The example universe in ``example_universe.py`` was written blind: its
author never saw the project's real records.  Instead of recalling
names, dates and places (recall is how real data leaks into examples),
every specific was drawn here from broad tables with fixed seeds and
then read over for plausibility: rounds one to four continue one
stream (``SEED``), the fifth starts its own (``SEED_5``), and where two
rounds drew the same slot the later one holds.  Run it to see the raw
draws::

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
    fifth_round()


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

    fourth_round(rng, show)


def fourth_round(rng: random.Random, show) -> None:
    """The GEDCOM rendering wants a dated burial: the death notice
    (20 March 1927) announces the funeral this many days later."""
    show("funeral days after death", rng.randint(2, 5))


# --- fifth round: its own seed ------------------------------------------

# Drawn with ``secrets.randbelow`` when the round was written.  A new
# stream rather than a continuation, so rounds one to four replay
# exactly as before.
SEED_5 = 136490

# Names in the universe after the fourth round, whether the fifth kept
# or replaced them.  No later round draws one of them, nor a name that
# shares its first four letters (a variant, such as Paul for Paula), so
# every name stays distinct from every earlier one.
USED_GIVEN = (
    "Alfred",
    "Wilhelmine",
    "Johann",
    "Hedwig",
    "Paula",
    "Julius",
    "Gertrud",
    "Josef",
    "Martha",
    "Frieda",
    "Karl",
    "Rudolf",
    "Christine",
    "Jonathan",
)
USED_SHORT = ("Hans", "Minna", "Hedi", "Sepp", "Joe", "Rudi")
USED_PET_NAMES = ("Muckel", "Peppi", "Bubi")
USED_STREET_WORDS = ("Brunnen", "Mühlen", "Anger")
# Street words that name a trade or a works (kettle, brick); with
# Mühlen above, none is drawn again.
TRADE_STREET_WORDS = ("Kessel", "Ziegel")

MORE_PET_NAMES = ("Hasi", "Krümel", "Mucki", "Pünktchen", "Schnecke", "Stups")
EN_FEMALE = ("Susan", "Linda", "Janet", "Carol", "Gillian", "Judith", "Hilary")
EN_MALE = (
    "Stephen",
    "David",
    "Michael",
    "Peter",
    "Andrew",
    "Philip",
    "Anthony",
    "Nicholas",
)
# English diminutives of the English forms in MALE, for an emigrant
# known by one in his new country.
EN_DIMINUTIVES = {
    "William": "Bill",
    "Frederick": "Fred",
    "Henry": "Harry",
    "Charles": "Charlie",
    "Lewis": "Lew",
    "Ernest": "Ernie",
    "George": "Georgie",
    "Gustave": "Gus",
    "Walter": "Wally",
    "Albert": "Bert",
    "Theodore": "Ted",
    "Leopold": "Leo",
    "Bernard": "Bernie",
    "Edward": "Eddie",
    "Maurice": "Morrie",
}
# Firm names that name no person: a house sign, the street or the town.
FIRM_PATTERNS_5 = (
    "Hutmacherei am {Street}",
    "{Town}er Hutmacherei",
    "Hutmacherei zum {Sign}",
    "{Town}er Hutfabrik",
)
HOUSE_SIGNS = ("Goldenen Stern", "Grünen Baum", "Blauen Hut", "Weißen Rad")
DEATH_RECORDS = (
    "civil death register extract",
    "probate court entry",
    "cemetery burial register entry",
)
DEPORTATION_RECORDS = ("compensation file entry", "memorial book entry")
LETTER_FORMS = ("postcard", "private letter")
CAUSES = (
    "Altersschwäche",
    "Lungenentzündung",
    "Herzschwäche",
    "Schlaganfall",
    "Wassersucht",
)


def _fold(text: str) -> str:
    for umlaut, plain in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        text = text.replace(umlaut, plain)
    return text.lower()


def _edits(first: str, second: str) -> int:
    """Levenshtein distance."""
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        previous, row[0] = row[0], i
        for j, b in enumerate(second, 1):
            previous, row[j] = (
                row[j],
                min(row[j] + 1, row[j - 1] + 1, previous + (a != b)),
            )
    return row[-1]


def fifth_round() -> None:
    """Re-draw the family after the first clean-room review (#40).

    A new surname, town, street and firm name; new given names for all
    of the first two generations but the elder son's wife, and for five
    of the later ones; new dates for the first two generations, the firm
    and the gravestone photograph; new birth years for the emigrant
    son's wife and child; a new person for the arrest; and new kinds for
    three documents.  The third generation's births (1907-1925) and the
    births of the two wives who married into generation two (1884 and
    1887) stay.  Draws repeat until they meet the constraint beside them.
    """
    rng = random.Random(SEED_5)

    def show(label: str, value: object) -> None:
        print(f"{label:28} {value}")

    def syllable(onsets=ONSETS) -> str:
        return rng.choice(onsets) + rng.choice(VOWELS) + rng.choice(CODAS)

    # sharing neither onset nor ending with Schöser, so the old and new
    # universes never blur, and with an umlaut for the anglicised form
    # to drop
    onsets = tuple(o for o in ONSETS if not o.startswith("Sch"))
    endings = tuple(e for e in SURNAME_ENDINGS if not e.endswith("er"))
    tries = 0
    while True:
        tries += 1
        surname = syllable(onsets) + rng.choice(endings)
        if any(u in surname for u in "äöü") and not surname.endswith("er"):
            break
    show("surname (tries)", (surname, tries))

    # a town that, umlauts folded, is no one-letter edit of a name in
    # the tables, so it cannot be misread as a person
    names = [*MALE, *FEMALE, *ENGLISH_GIVEN]
    names += [v[1] for v in (*MALE.values(), *FEMALE.values()) if v[1]]
    tries = 0
    while True:
        tries += 1
        town = syllable() + rng.choice(TOWN_ENDINGS)
        if min(_edits(_fold(town), _fold(n)) for n in names) > 1:
            break
    show("town (tries)", (town, tries))

    words = [
        w
        for w in STREET_WORDS
        if w not in USED_STREET_WORDS + TRADE_STREET_WORDS
    ]
    show(
        "street, number",
        (rng.choice(words) + rng.choice(STREET_ENDINGS), rng.randint(21, 60)),
    )
    show("firm name", (rng.choice(FIRM_PATTERNS_5), rng.choice(HOUSE_SIGNS)))

    # --- given names -----------------------------------------------------
    taken = [_fold(n)[:4] for n in USED_GIVEN]

    def pick(pool, need=lambda name: True):
        for name in pool:
            if _fold(name)[:4] not in taken and need(name):
                taken.append(_fold(name)[:4])
                return name
        raise LookupError("pool exhausted")

    def has_short(table):
        return lambda n: table[n][1] and table[n][1] not in USED_SHORT

    male = list(MALE)
    female = list(FEMALE)
    rng.shuffle(male)
    rng.shuffle(female)
    roles = {
        "G1 husband": pick(male),
        "G1 wife (+dim.)": pick(female, has_short(FEMALE)),
        "G2 elder son (+dim.)": pick(male, has_short(MALE)),
        "G2 daughter": pick(female),
        "G2 younger son": pick(male),
        "G2 younger son's wife": pick(female),
        "G3 emigrant son (+dims.)": pick(
            male,
            lambda n: has_short(MALE)(n) and MALE[n][0] in EN_DIMINUTIVES,
        ),
        "G3 emigrant daughter": pick(female),
        "G3 stayer son": pick(male),
    }
    for role, name in roles.items():
        english, short = MALE[name] if name in MALE else FEMALE[name]
        show(role, (name, english, short, EN_DIMINUTIVES.get(english)))
    en_female = list(EN_FEMALE)
    en_male = list(EN_MALE)
    rng.shuffle(en_female)
    rng.shuffle(en_male)
    show("G3 emigrant son's wife", pick(en_female))
    show("G4 child", pick(en_male))
    pets = [p for p in PET_NAMES + MORE_PET_NAMES if p not in USED_PET_NAMES]
    show("emigrant daughter's pet", rng.choice(pets))

    # --- years -----------------------------------------------------------
    # a generation earlier than before: G2 in order and born by 1874,
    # all three to a mother no older than 44
    tries = 0
    while True:
        tries += 1
        husband = rng.randint(1820, 1839)
        wife = husband + rng.randint(1, 9)
        marriage = wife + rng.randint(19, 26)
        born = [marriage + rng.randint(1, 2)]
        for _ in range(2):
            born.append(born[-1] + rng.randint(1, 4))
        if born[0] >= 1860 and born[-1] <= 1874 and born[-1] - wife <= 44:
            break
    show("G1 husband, wife, marriage", (husband, wife, marriage, tries))
    show("G2 born (son, dau., son)", born)
    # both alive for the 1890s portrait
    show(
        "G1 husband dies (y, m, d)",
        (rng.randint(1900, 1924), rng.randint(1, 12), rng.randint(1, 28)),
    )
    show("G1 husband buried +days", rng.randint(2, 5))
    show("G1 wife dies", rng.randint(1900, 1924))
    show("firm founded", rng.randint(husband + 24, husband + 40))
    show("firm gone before", rng.randint(1920, 1935))
    # after the 1954 renaming, which the photograph's title uses
    show("gravestone photographed", rng.randint(1955, 1974))
    # the emigrant son was born 1907 and married in 1945: a wife of
    # about his age, a child soon after the marriage
    while True:
        mother = rng.randint(1900, 1917)
        child = rng.randint(1946, 1949)
        if 20 <= child - mother <= 43:
            break
    show("G3 wife born, G4 born", (mother, child))

    # --- documents -------------------------------------------------------
    show("G1 husband's death record", rng.choice(DEATH_RECORDS))
    show("  cause, if it gives one", rng.choice(CAUSES))
    show(
        "deportation record, year",
        (rng.choice(DEPORTATION_RECORDS), rng.randint(1950, 1965)),
    )
    show("1938 letter as", rng.choice(LETTER_FORMS))
    # the arrested man: a son of the woman whose post-war card records
    # the arrest
    show("arrested 1939", rng.choice(("G3 stayer son", "Rudolf")))


if __name__ == "__main__":
    main()
