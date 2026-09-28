# Examples: one fictional universe

**The rule.** Every example in code, comments, prompts, tests, ADRs,
issues and PRs comes only from the fictional universe defined in
[`tests/fixtures/example_universe.py`](../tests/fixtures/example_universe.py).
Never use real family data, not even partially anonymised: no real name
with a changed surname, no real date shifted by a year, no real town,
address or family firm. If an example needs a pattern the universe
lacks, add it to the universe first (see *Extending* below).
Placeholders such as `A /B/` are fine where no example is needed.

**Why.** The archive holds records about real, partly living people, and
examples travel: prompts go to models, issues and PRs are read by agents,
and the repository may be shared. One consistent cast also lets tests,
prompts and ADRs tell the same story.

## How it was made

The universe was invented blind: its authors worked without access to
the archive's records, and every specific was drawn at random, with
fixed seeds, from broad tables (period given names, ordinary trades,
large regions, syllables for surnames and towns), then read over for
plausibility. Real geography appears only at the broadest level:
Germany, Hesse, Australia and Victoria. Every town, street, firm,
institution, ship and document is invented.

## The world

- **Hesse, Germany.** Kreis Peiltal holds the town of Peiltal (with the
  court prison, *Gerichtsgefängnis Peiltal*, and the photo studio
  *Photograph Brüsrich*), the town of Kredfeld and the town of
  **Flörburg**: Brunnenring 35 (the firm and the family home),
  Wasserallee 59, Angerring 57 and the cemetery, *Friedhof Flörburg*.
  Flörburg became **Bad Flörburg** in 1954. Fläckfeld lies in Kreis
  Mockburg or Kreis Peiltal, depending on the source. Moshausen is
  placed only in Germany. **Rekau**, seat of the regional court
  (*Landgericht Rekau*), is a town that forms an urban district of its
  own name (*Stadtkreis Rekau*): the town Rekau lies in the district
  Rekau.
- **Klurnau**, with the camp *Lager Klurnau*: no source says where it
  lay, so these are the only places that reach no country.
- **Victoria, Australia.** The town of Stupfield, with 8 Hawthorn Road
  and 31 Juniper Street.

## The family

The Löbigs are hatters. Two grandchildren emigrated to Australia and
became the Lobigs.

```
1  Eugen "Muckel" (1836-1909) = 1859 Cäcilie Tarle "Cilli" (c. 1833-1902)
   |  founded the Hutmacherei zum Blauen Hut, hatters, in 1863;
   |  Sebastian owns it after Eugen's death
2  Sebastian "Wastl" (1862-1945) = 1905 Hildegard Laudler "Hilde" (1876-1968)
   Felix (1867-1931) = Ida Sörne "Peppi" (1881-1946)
   Rahel (1871-after 1945)
   |
3  of Sebastian and Hildegard:
     Ursula (1911-1941)
     Klara "Pünktchen" (1917-2001), later Clara Lobig
     Gustav "Gustl" (1919-1996), later Gustave "Gus" Lobig,
       = 1942 Florence Faxridge (1914-1963)
   of Felix and Ida:
     Leopold (1914-1960) = 1947 Agnes Nertler (1912-1974), divorced 1957
     Rudolf "Rudi" (1916-1941), an unnamed infant (1922)
   |
4  Nicholas Lobig (b. 1950), son of Gustav and Florence
```

Not kin: a widow listed in the 1912 directory as *Löbig, Ursula,
Wwe.*, who shares a name with Sebastian's daughter; and Maisie Cutmore,
a neighbour in Stupfield whom Nicholas calls *Nana Maisie*.

## Sources

Fourteen invented documents carry transcribed text (`Source.text`), and
every assertion quotes the passage that makes it (`Assertion.excerpt`).

| Key | What | Language |
|---|---|---|
| `studio-photo-1890s` | studio portrait, names pencilled on the back | none |
| `marriage-cert-1905` | civil marriage certificate, Sebastian and Hildegard | German |
| `burial-register-1909` | the cemetery's burial register: Eugen's death, cause and burial | German |
| `directory-1912` | Flörburg address book: the firm (with its founding year) and the widow | German |
| `school-report-1930` | Rudolf's school report | German |
| `birth-extract-1938` | Gustav's birth register extract, issued for emigration | German |
| `postcard-1938` | Sebastian to Klara in Australia, dated only by its postmark | German |
| `registration-card-1945` | Ida's post-war card, with her son Leopold's arrest | English |
| `naturalisation-1952` | Gustav's naturalisation as Gustave Lobig | English |
| `compensation-file-1953` | Leopold's claim for his late mother Ida: her deportation | German |
| `gravestone-photo-1956` | photograph of Eugen and Cäcilie's gravestone | German |
| `divorce-judgment-1957` | the Landgericht Rekau's judgment divorcing Leopold and Agnes | German |
| `family-tree-1984` | the tree Klara drew in Australia | English |
| `interview-2002` | oral history with Nicholas (possibly living), who says where Gustav is buried | English |

## Pattern index

| Pattern | In the universe |
|---|---|
| Birth, married and nick names (ADR-0011) | Ida: born *Ida Sörne*, married *Ida Löbig*, nickname *Peppi*; likewise Cäcilie (*Cilli*) and Hildegard (*Hilde*) |
| German and English forms | Löbig/Lobig, Gustav/Gustave, Klara/Clara, Flörburg/Floerburg, Gerichtsgefängnis Peiltal/Peiltal court prison, Hutmacherei zum Blauen Hut/the Blue Hat, Hutmacher/hatter |
| Diminutives | German *Wastl, Cilli, Hilde, Gustl, Rudi*; English *Gus*; pet names *Muckel, Peppi, Pünktchen* |
| Names as sources write them (ADR-0022) | *Cäcilie Löbig geb. Tarle*; *Hildegard Löbig geborene Laudler*; *Agnes Löbig geb. Nertler*; *LÖBIG, Ida, née SÖRNE*; *Gustave LOBIG (formerly Gustav LÖBIG)*; *Löbig, Ursula, Wwe.*; *Mrs Cutmore*; *Sebastian 'Wastl'*; the placeholder *N.N. Löbig* |
| Homonyms (ADR-0017) | two Ursula Löbigs: Sebastian's daughter, born 1911, and a widow in the 1912 directory; a one-year-old cannot be that widow |
| Spouses and parents, 3+ generations | four generations, Eugen to Nicholas; marriages in 1859, 1905, an undated one, 1942 and 1947 |
| Divorce | Leopold and Agnes, married on 8 Feb 1947 and divorced on 2 Jan 1957 by the Landgericht Rekau (the judgment); the 1984 tree says *div. 1957*. The spouse statements carry it as `end_time`, the GEDCOM export as `DIV` |
| Kinship terms and courtesy titles (ADR-0018) | literal: *Dein Vater* (the postcard, from Klara's view); *Dad*, *Aunt Pünktchen*, *Dad's cousin Leopold* (the interview, from Nicholas's view). Each is recorded as the kinship, never as `referred_to_as`: *Dein Vater* and *Dad* as `father`; the other two as `relative` + `kinship_type`, with the referent as subject and the ego as object: `Klara —relative→ Nicholas ; kinship_type = "aunt"` (and *Pünktchen* is her `nickname`) and `Leopold —relative→ Gustav ; kinship_type = "cousin"` (the ego is *Dad*). Courtesy: *Nana Maisie* (the interview), a neighbour who is no relation: `referred_to_as`, never `relative` |
| Family firm (ADR-0012) | Hutmacherei zum Blauen Hut, hat makers, founded 1863 (*gegr. 1863*), at Brunnenring 35, owned by Sebastian in 1912, after his father's death in 1909 (*Inh. Sebastian Löbig*), gone *before 1926* |
| Other agents and kinds | Standesamt Flörburg, Städtische Realschule Flörburg, Wiedergutmachungsamt Peiltal, Landgericht Rekau, Photograph Brüsrich (organisations); RMS Flund (vessel); occupations *Hatter* and *Master hatter* |
| Street address and containment (ADR-0009) | Brunnenring 35 → Brunnenring → Flörburg → Kreis Peiltal → Hesse → Germany; 8 Hawthorn Road → Hawthorn Road → Stupfield → Victoria → Australia |
| A place inside a same-named parent | the town Rekau in the urban district Rekau (*Stadtkreis Rekau*) |
| A place named two ways over time | Flörburg, *Bad Flörburg* from 1954; the 1984 tree uses the new name for an 1836 birth, the naturalisation spells it *Floerburg* |
| A place under two parents | Fläckfeld: Kreis Mockburg (1905) or Kreis Peiltal (1984) |
| A place with no known location | Klurnau |
| Persecution events (ADR-0014) | Leopold's arrest (3 Sep 1939), imprisonment in the Peiltal court prison and release (1 Oct 1939); Ida's deportation to Lager Klurnau (21 Aug 1942) and liberation at Klurnau (28 Jan 1945). Emigrations are `migration` events too |
| Contradictory dates (ADR-0002) | Gustav born 7 Jan 1919 (civil register) and 1 July 1919 (naturalisation): day and month transposed. Also Fläckfeld's district |
| Refinement, not contradiction | Eugen died 1909 (gravestone) and 16 Mar 1909 (burial register); Klara left in '38 or '39 (interview) and on 22 June 1938 (postcard); Leopold born 1914 (tree) and 29 May 1914 (judgment), Agnes likewise; Leopold held "in Peiltal" and in its court prison; Ida deported to Klurnau (card) and to Lager Klurnau (compensation file) |
| Uncertain dates | circa: Cäcilie *c. 1833* (`ABT 1833`); before: the firm gone *before 1926* (`BEF 1926`); after: Rahel died *after 1945* (`AFT 1945`); between: Klara left *between 1938 and 1939* (`BET 1938 AND 1939`); decade: the studio portrait, *1890s* (`BET 1890 AND 1899`) |
| A person without name or sex | the infant of 1922, *N.N. Löbig* (`SEX U`) |
| Privacy (ADR-0020) | Nicholas Lobig (b. 1950, no death asserted) is possibly living; he is dated to the year only |
| GEDCOM rendering (ADR-0021) | `tests/fixtures/example-family.ged`: typed names, `NICK`, an inline `(Peppi)`, `_MARNM`, `TYPE immigrant`, a dated burial and an undated one, cause of death, child count, residences at street addresses, a `DIV`, an `EDUC` and a `NOTE` the adapter reports as unsupported, and the studio portrait as a shared `OBJE` |

## Using it

```python
from tests.fixtures import example_universe as u

u.about("gustav-loebig", "date_of_birth")    # three sources, two days
u.containment_paths("brunnenring-35")        # address -> ... -> country
str(u.Date(1938, qualifier="between", until=u.Date(1939)))
# 'between 1938 and 1939'
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
the rest were drawn: at random with a fixed seed from broad tables
(period given names, ordinary trades, syllables for surnames and
towns), never recalled, and then read over for plausibility. Prefer
such invented names to real small towns or real surnames, and write
document wording around the drawn facts. A session that has handled
real data also confirms, as part of its self-review
(docs/AGENT_PROTOCOL.md §7), that no new name, place, organisation,
date or source document matches or mirrors a real one, including a
document that resembles a real one by kind and date.

## Status

- Earlier examples are gone from the ADRs, MODELING.md and the READMEs
  (#31), the prompts and source comments (#32) and the tests (#33),
  except two in `claims.TimeValue`'s docstring. The extraction prompt
  embeds that docstring, so they change with the next extraction-prompt
  change (#33).
- CLAUDE.md and docs/AGENT_PROTOCOL.md state the rule (ADR-0024). The
  token denylist once used to check additions is retired (ADR-0024 §5).
