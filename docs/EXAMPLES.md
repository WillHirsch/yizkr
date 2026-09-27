# Examples: one fictional universe

**The rule.** Every example in code, comments, prompts, tests, ADRs,
issues and PRs comes only from the fictional universe defined in
[`tests/fixtures/example_universe.py`](../tests/fixtures/example_universe.py).
Never use real family data, not even partly anonymised: no real name
with a changed surname, no real date shifted by a year, no real town,
street, address, firm or institution. If an example needs a pattern the
universe lacks, add it to the universe first (see *Extending* below).
Placeholders such as `A /B/` are fine where no example is needed.

**Why.** The archive holds records about real, partly living people, and
examples travel: prompts go to models, issues and PRs are read by agents,
and the repository may be shared. One consistent cast also lets tests,
prompts and ADRs tell the same story.

## How it was made

The universe was written in a clean room: its author never saw the
archive's records, nor the values of the earlier example universe it
replaces. Instead of recalling names and dates, every specific was drawn
by [`tests/fixtures/universe_draws.py`](../tests/fixtures/universe_draws.py)
with fixed seeds from broad tables (period given names, ordinary
trades, large regions, syllables for surnames and towns), then read over
for plausibility. The module's docstring lists every hand change. A
separate checker, who can see the real corpus, compares the result
against it before it merges; the fifth round of draws re-drew what the
first check sent back. Real geography appears only at the broadest
level: Germany, Hesse, Australia and Victoria. Every town, street,
firm, institution, ship and document is invented.

## The world

- **Hesse, Germany.** Kreis Peiltal holds the town of Peiltal (with the
  court prison, *Gerichtsgefängnis Peiltal*, and the photo studio
  Atelier Brüsrich), the town of Kredfeld and the town of **Flörburg**:
  Brunnenring 35 (the firm and the family home), Wasserallee 59,
  Angerring 57 and the cemetery, *Friedhof Flörburg*. Flörburg became
  **Bad Flörburg** in 1954. Fläckfeld lies in Kreis Mockburg or Kreis
  Peiltal, depending on the source. Moshausen is placed only in Germany.
- **Klurnau**, with the camp *Lager Klurnau*: no source says where it
  lay, so these are the only places that reach no country.
- **Victoria, Australia.** The town of Stupfield, with 8 Hawthorn Road
  and 31 Juniper Street.

## The family

The Löbigs are hatters. Two grandchildren emigrated to Australia and
became the Lobigs.

```
1  Moritz "Muckel" (1836-1919) = 1859 Charlotte Tarle "Lotte" (c. 1840-1902)
   |  founded the Hutmacherei zum Blauen Hut, hatters, in 1863
2  Georg "Schorsch" (1860-1953) = 1905 Hedwig Laudler "Hedi" (1884-1968)
   Luise (1864-after 1945)
   Ludwig (1866-1942) = Katharina Sörne "Peppi" (1887-1946)
   |
3  of Georg and Hedwig:
     Gustav "Gustl" (1907-1996), later Gustave "Gus" Lobig,
       = 1945 Susan Faxridge (1914-1963)
     Rosa "Pünktchen" (1910-2001), later Rose Lobig
     Frieda (1911-1941)
   of Ludwig and Katharina:
     Leopold (1914-1960), Rudolf "Rudi" (1916-1941), an unnamed infant (1925)
   |
4  Nicholas Lobig (b. 1947), son of Gustav and Susan
```

Not kin: a widow listed in the 1912 directory as *Löbig, Frieda,
Wwe.*, who shares a name with Georg's daughter.

## Sources

Thirteen invented documents carry transcribed text (`Source.text`), and
every assertion quotes the passage that makes it (`Assertion.excerpt`).

| Key | What | Language |
|---|---|---|
| `studio-photo-1890s` | studio portrait, names pencilled on the back | none |
| `marriage-cert-1905` | civil marriage certificate, Georg and Hedwig | German |
| `directory-1912` | Flörburg address book: the firm (with its founding year) and the widow | German |
| `burial-register-1919` | the cemetery's burial register: Moritz's death, cause and burial | German |
| `school-report-1930` | Rudolf's school report | German |
| `birth-extract-1938` | Gustav's birth register extract, issued for emigration | German |
| `postcard-1938` | Georg to Rosa in Australia, dated only by its postmark | German |
| `registration-card-1945` | Katharina's post-war card, with her son Leopold's arrest | English |
| `naturalisation-1952` | Gustav's naturalisation as Gustave Lobig | English |
| `compensation-file-1953` | Leopold's claim for his late mother Katharina: her deportation | German |
| `gravestone-photo-1956` | photograph of Moritz and Charlotte's gravestone | German |
| `family-tree-1984` | the tree Rosa drew in Australia | English |
| `interview-2002` | oral history with Nicholas (possibly living) | English |

## Pattern index

| Pattern | In the universe |
|---|---|
| Birth, married and nick names (ADR-0011) | Katharina: born *Katharina Sörne*, married *Katharina Löbig*, nickname *Peppi*; likewise Charlotte (*Lotte*) and Hedwig (*Hedi*) |
| German and English forms | Löbig/Lobig, Gustav/Gustave, Flörburg/Floerburg, Gerichtsgefängnis Peiltal/Peiltal court prison, Hutmacherei zum Blauen Hut/the Blue Hat, Hutmacher/hatter |
| Diminutives | German *Schorsch, Lotte, Hedi, Gustl, Rudi*; English *Gus*; pet names *Muckel, Peppi, Pünktchen* |
| Names as sources write them (ADR-0022) | *Charlotte Löbig geb. Tarle*; *Hedwig Löbig geborene Laudler*; *LÖBIG, Katharina, née SÖRNE*; *Gustave LOBIG (formerly Gustav LÖBIG)*; *Löbig, Frieda, Wwe.*; *Georg 'Schorsch'*; the placeholder *N.N. Löbig* |
| Homonyms (ADR-0017) | two Frieda Löbigs: Georg's daughter, born 1911, and a widow in the 1912 directory; a one-year-old cannot be that widow |
| Spouses and parents, 3+ generations | four generations, Moritz to Nicholas; marriages in 1859, 1905, an undated one and 1945 |
| Kinship terms (ADR-0018) | *Dein Vater* (the postcard, from Rosa's view); *Dad*, *Aunt Pünktchen*, *Dad's cousin Leopold* (the interview, from Nicholas's view) |
| Family firm (ADR-0012) | Hutmacherei zum Blauen Hut, hat makers, founded 1863 (*gegr. 1863*), at Brunnenring 35, owned by Georg in 1912 (*Inh. Georg Löbig*), gone *before 1926* |
| Other agents and kinds | Standesamt Flörburg, Städtische Realschule Flörburg, Wiedergutmachungsamt Peiltal, Atelier Brüsrich (organisations); RMS Flund (vessel); occupations *Hatter* and *Master hatter* |
| Street address and containment (ADR-0009) | Brunnenring 35 → Brunnenring → Flörburg → Kreis Peiltal → Hesse → Germany; 8 Hawthorn Road → Hawthorn Road → Stupfield → Victoria → Australia |
| A place named two ways over time | Flörburg, *Bad Flörburg* from 1954; the 1984 tree uses the new name for an 1836 birth, the naturalisation spells it *Floerburg* |
| A place under two parents | Fläckfeld: Kreis Mockburg (1905) or Kreis Peiltal (1984) |
| A place with no known location | Klurnau |
| Persecution events (ADR-0014) | Leopold's arrest (3 Sep 1939), imprisonment in the Peiltal court prison and release (1 Oct 1939); Katharina's deportation to Lager Klurnau (21 Aug 1942) and liberation at Klurnau (28 Jan 1945). Emigrations are `migration` events too |
| Contradictory dates (ADR-0002) | Gustav born 6 Sep 1907 (civil register) and 9 June 1907 (naturalisation): day and month transposed. Also Fläckfeld's district |
| Refinement, not contradiction | Moritz died 1919 (gravestone) and 6 Dec 1919 (burial register); Rosa left in '37 or '38 (interview) and on 12 Mar 1938 (postcard); Leopold held "in Peiltal" and in its court prison; Katharina deported to Klurnau (card) and to Lager Klurnau (compensation file) |
| Uncertain dates | circa: Charlotte *c. 1840* (`ABT 1840`); before: the firm gone *before 1926* (`BEF 1926`); after: Luise died *after 1945* (`AFT 1945`); between: Rosa left *between 1937 and 1938* (`BET 1937 AND 1938`); decade: the studio portrait, *1890s* (`BET 1890 AND 1899`) |
| A person without name or sex | the infant of 1925, *N.N. Löbig* (`SEX U`) |
| Privacy (ADR-0020) | Nicholas Lobig (b. 1947, no death asserted) is possibly living; he is dated to the year only |
| GEDCOM rendering (ADR-0021) | `tests/fixtures/example-family.ged`: typed names, `NICK`, an inline `(Peppi)`, `_MARNM`, `TYPE immigrant`, a dated burial, cause of death, child count, residences at street addresses, an `EDUC` and a `NOTE` the adapter reports as unsupported, and the studio portrait as a shared `OBJE` |

## Using it

```python
from tests.fixtures import example_universe as u

u.about("gustav-loebig", "date_of_birth")    # three sources, two days
u.containment_paths("brunnenring-35")        # address -> ... -> country
str(u.Date(1937, qualifier="between", until=u.Date(1938)))
# 'between 1937 and 1938'
```

People, places, entities (organisations, occupations, a vessel,
events) and sources are ground truth, so tests can check identity and
containment decisions against it. Everything said *about* them is a
source-attributed `Assertion` in the archive's own vocabulary: manifest
property ids, entity kinds and artifact classes. `CONTRADICTIONS` and
`REFINEMENTS` name the deliberate cases. Each person's `gedcom_xref`
points into `tests/fixtures/example-family.ged`, and
`tests/unit/test_example_universe.py` checks that the GEDCOM file says
nothing the sources do not.

## Extending

Add to the module rather than inventing a one-off name inline, and keep
`tests/unit/test_example_universe.py` green. Draw new specifics the way
the rest were drawn: append a round to `universe_draws.py` (never edit
an earlier one, so earlier draws stay put; a round may take its own
seed, as the fifth does), run it, and note any hand change in the
module docstring. Draw names only from what the tables hold beyond
those already used (`USED_GIVEN`, `USED_SHORT`, `USED_PET_NAMES`).
Before committing, check every new name, place, organisation and exact
date against the out-of-repo denylist (#35), both its enforced and its
review lists.

## Status

- Other tests, prompts, source comments and ADRs still use earlier
  examples; replacing them is #31, #32 and #33.
- CLAUDE.md and docs/AGENT_PROTOCOL.md state the rule once #20 lands.
