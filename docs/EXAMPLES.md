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
with a fixed seed from broad tables (period given names, ordinary
trades, large regions, syllables for surnames and towns), then read over
for plausibility. The module's docstring lists every hand change. A
separate checker, who can see the real corpus, compares the result
against it before it merges. Real geography appears only at the
broadest level: Germany, Hesse, Australia and Victoria. Every town,
street, firm, institution, ship and document is invented.

## The world

- **Hesse, Germany.** Kreis Peiltal holds the town of Peiltal (with the
  court prison, *Gerichtsgefängnis Peiltal*, and the photo studio
  Atelier Brüsrich), the town of Dönwald and the town of **Flörburg**:
  Brunnenring 35 (the firm and the family home), Mühlenweg 16,
  Angerring 57 and the cemetery, *Friedhof Flörburg*. Flörburg became
  **Bad Flörburg** in 1954. Fläckfeld lies in Kreis Mockburg or Kreis
  Peiltal, depending on the source. Moshausen is placed only in Germany.
- **Klurnau**, with the camp *Lager Klurnau*: no source says where it
  lay, so these are the only places that reach no country.
- **Victoria, Australia.** The town of Stupfield, with 8 Hawthorn Road
  and 31 Juniper Street.

## The family

The Schösers are hatters. Two grandchildren emigrated to Australia and
became the Shosers.

```
1  Alfred "Muckel" (1850-1927) = 1879 Wilhelmine Tarle "Minna" (c. 1855-1928)
   |  founded Schöser & Sohn, hatters, in 1885
2  Johann "Hans" (1881-1953) = 1905 Hedwig Laudler "Hedi" (1884-1968)
   Paula (1882-after 1945)
   Julius (1884-1942) = Gertrud Sörne "Peppi" (1887-1946)
   |
3  of Johann and Hedwig:
     Josef "Sepp" (1907-1996), later Joseph "Joe" Shoser,
       = 1945 Christine Faxridge (1923-1963)
     Martha "Bubi" (1910-2001), later Martha Shoser
     Frieda (1911-1941)
   of Julius and Gertrud:
     Karl (1914-1960), Rudolf "Rudi" (1916-1941), an unnamed infant (1925)
   |
4  Jonathan Shoser (b. 1953), son of Josef and Christine
```

Not kin: a widow listed in the 1912 directory as *Schöser, Frieda,
Wwe.*, who shares a name with Johann's daughter.

## Sources

Thirteen invented documents carry transcribed text (`Source.text`), and
every assertion quotes the passage that makes it (`Assertion.excerpt`).

| Key | What | Language |
|---|---|---|
| `studio-photo-1890s` | studio portrait, names pencilled on the back | none |
| `marriage-cert-1905` | civil marriage certificate, Johann and Hedwig | German |
| `directory-1912` | Flörburg address book: the firm and the widow | German |
| `death-notice-1927` | Alfred's death notice, with the funeral date | German |
| `school-report-1930` | Rudolf's school report | German |
| `birth-extract-1938` | Josef's birth register extract, issued for emigration | German |
| `letter-1938` | Johann to Martha in Australia, on the firm's letterhead | German |
| `transport-list-1942` | Gertrud's deportation | German |
| `registration-card-1945` | Gertrud's post-war card, with Julius's arrest | English |
| `naturalisation-1952` | Josef's naturalisation as Joseph Shoser | English |
| `gravestone-photo-1978` | photograph of Alfred and Wilhelmine's gravestone | German |
| `family-tree-1984` | the tree Martha drew in Australia | English |
| `interview-2002` | oral history with Jonathan (possibly living) | English |

## Pattern index

| Pattern | In the universe |
|---|---|
| Birth, married and nick names (ADR-0011) | Gertrud: born *Gertrud Sörne*, married *Gertrud Schöser*, nickname *Peppi*; likewise Wilhelmine (*Minna*) and Hedwig (*Hedi*) |
| German and English forms | Schöser/Shoser, Josef/Joseph, Flörburg/Floerburg, Gerichtsgefängnis Peiltal/Peiltal court prison, Schöser & Sohn/Schöser and Son, Hutmacher/hatter |
| Diminutives | German *Hans, Minna, Hedi, Sepp, Rudi*; English *Joe*; pet names *Muckel, Peppi, Bubi* |
| Names as sources write them (ADR-0022) | *Wilhelmine Schöser geb. Tarle*; *Hedwig Schöser geborene Laudler*; *SCHÖSER, Gertrud, née SÖRNE*; *Joseph SHOSER (formerly Josef SCHÖSER)*; *Schöser, Frieda, Wwe.*; *Johann 'Hans'*; the placeholder *N.N. Schöser* |
| Homonyms (ADR-0017) | two Frieda Schösers: Johann's daughter, born 1911, and a widow in the 1912 directory; a one-year-old cannot be that widow |
| Spouses and parents, 3+ generations | four generations, Alfred to Jonathan; marriages in 1879, 1905, an undated one and 1945 |
| Kinship terms (ADR-0018) | *Dein Vater* (the letter, from Martha's view); *Dad*, *Aunt Bubi*, *Great-uncle Julius* (the interview, from Jonathan's view) |
| Family firm (ADR-0012) | Schöser & Sohn, hat makers, founded 1885 (*gegr. 1885*), at Brunnenring 35, owned by Johann in 1912 (*Inh. Johann Schöser*), gone *before 1940* |
| Other agents and kinds | Standesamt Flörburg, Städtische Realschule Flörburg, Flörburger Anzeiger, Atelier Brüsrich (organisations); RMS Flund (vessel); occupations *Hatter* and *Master hatter* |
| Street address and containment (ADR-0009) | Brunnenring 35 → Brunnenring → Flörburg → Kreis Peiltal → Hesse → Germany; 8 Hawthorn Road → Hawthorn Road → Stupfield → Victoria → Australia |
| A place named two ways over time | Flörburg, *Bad Flörburg* from 1954; the 1984 tree uses the new name for an 1850 birth, the naturalisation spells it *Floerburg* |
| A place under two parents | Fläckfeld: Kreis Mockburg (1905) or Kreis Peiltal (1984) |
| A place with no known location | Klurnau |
| Persecution events (ADR-0014) | Julius's arrest (3 Sep 1939), imprisonment in the Peiltal court prison and release (1 Oct 1939); Gertrud's deportation to Lager Klurnau (21 Aug 1942) and liberation at Klurnau (28 Jan 1945). Emigrations are `migration` events too |
| Contradictory dates (ADR-0002) | Josef born 6 Sep 1907 (civil register) and 9 June 1907 (naturalisation): day and month transposed. Also Fläckfeld's district |
| Refinement, not contradiction | Alfred died 1927 (gravestone) and 20 Mar 1927 (death notice); Martha left in '37 or '38 (interview) and on 12 Mar 1938 (letter); Julius held "in Peiltal" and in its court prison |
| Uncertain dates | circa: Wilhelmine *c. 1855* (`ABT 1855`); before: the firm gone *before 1940* (`BEF 1940`); after: Paula died *after 1945* (`AFT 1945`); between: Martha left *between 1937 and 1938* (`BET 1937 AND 1938`); decade: the studio portrait, *1890s* (`BET 1890 AND 1899`) |
| A person without name or sex | the infant of 1925, *N.N. Schöser* (`SEX U`) |
| Privacy (ADR-0020) | Jonathan Shoser (b. 1953, no death asserted) is possibly living; he is dated to the year only |
| GEDCOM rendering (ADR-0021) | `tests/fixtures/example-family.ged`: typed names, `NICK`, an inline `(Peppi)`, `_MARNM`, `TYPE immigrant`, a dated burial, cause of death, child count, residences at street addresses, an `EDUC` and a `NOTE` the adapter reports as unsupported, and the studio portrait as a shared `OBJE` |

## Using it

```python
from tests.fixtures import example_universe as u

u.about("josef-schoeser", "date_of_birth")   # three sources, two days
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
an earlier one, so earlier draws stay put), run it, and note any hand
change in the module docstring. Before committing, check every new name,
place, organisation and exact date against the out-of-repo denylist
(#35), both its enforced and its review lists.

## Status

- Other tests, prompts, source comments and ADRs still use earlier
  examples; replacing them is #31, #32 and #33.
- CLAUDE.md and docs/AGENT_PROTOCOL.md state the rule once #20 lands.
