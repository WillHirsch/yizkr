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
Germany, Hesse, Australia, Victoria, Russia, the Netherlands and
Friesland. Every town, street, firm, institution, ship and document is
invented.

Its sources are in German and English, and in two languages drawn for
their naming patterns (#387): Russian and West Frisian. They were drawn
at random with a fixed seed from a table of European languages; the
module records the table and the seed (`LANGUAGE_TABLE`,
`LANGUAGE_SEED`), and `tests/unit/test_example_universe.py` draws them
again. #400's description, which added these sources, gives every other
specific drawn for them, each with its table, its seed and its value,
and the script that draws them again.

## The world

- **Hesse, Germany.** Kreis Peiltal holds the town of Peiltal (with the
  court prison, *Gerichtsgefängnis Peiltal*, and the photo studio
  *Photograph Brüsrich*), the town of Kredfeld and the town of
  **Flörburg**: Brunnenring 35 (the firm and the family home),
  Wasserallee 59, Angerring 11 and 57, and the cemetery, *Friedhof
  Flörburg*; Angerring 71 is named only by a misreading of Angerring 11.
  Flörburg became **Bad Flörburg** in 1954. Fläckfeld lies in Kreis
  Mockburg or Kreis Peiltal, depending on the source. Moshausen is
  placed only in Germany. **Rekau**, seat of the regional court
  (*Landgericht Rekau*), is a town that forms an urban district of its
  own name (*Stadtkreis Rekau*): the town Rekau lies in the district
  Rekau.
- **Klurnau**, with the camp *Lager Klurnau*: no source says where it
  lay, so these are the only places that reach no country.
- **Victoria, Australia.** The town of Stupfield, with 8 Hawthorn Road,
  31 Juniper Street and 1 Linden Grove.
- **Russia.** Chadsk Oblast holds Kaldovsky District, with its civil
  registry (*Калдовское районное бюро ЗАГС*), and the district holds the
  village of **Koshnovka**. The 1941 death certificate names the republic
  as the RSFSR.
- **Friesland, the Netherlands.** The municipality of Drakeradiel holds
  the village of **Snylsjum**, with its brickworks (*it tichelwurk fan
  Snylsjum*).

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
       = 1942 Florence Faxridge (1914-1963), daughter of Sipkje Faxridge
   of Felix and Ida:
     Leopold (1914-1960) = 1947 Agnes Nertler (1912-1974), divorced 1957
     Rudolf "Rudi" (1916-1941) = 1938 Nina Sartina, in the Soviet Union
     an unnamed infant (1922)
   |
4  Nicholas Lobig (b. 1950), son of Gustav and Florence
```

Rudolf went to the Soviet Union in 1936 and worked as a fitter in
Koshnovka. He married Nina Sartina there in 1938 and died there on
6 March 1941. In 1958 Nina, daughter of Vasily Sartin and sister of
Pavel Sartin, wrote to Leopold and sent him Rudolf's death certificate.

Florence's mother, Sipkje Faxridge, born van 't Nijks, came from Snylsjum
in Friesland. In 1952 her brother Sybren van 't Nijks wrote to her in
Stupfield: their mother, a widow, had become Nicholas's
great-grandmother. His letter names their parents only as *Mem*
(mother) and *ús sillige heit* (our late father), who had worked at the
village brickworks.

Not kin: a widow listed in the 1912 directory as *Löbig, Ursula,
Wwe.*, who shares a name with Sebastian's daughter; and Maisie Cutmore,
a neighbour in Stupfield whom Nicholas calls *Nana Maisie*.

## Sources

Twenty-one invented documents carry transcribed text (`Source.text`), and
every assertion quotes the passage that makes it (`Assertion.excerpt`).
The module glosses each Russian and West Frisian text in English, in a
comment above it.

| Key | What | Language |
|---|---|---|
| `studio-photo-1890s` | studio portrait, names pencilled on the back | none |
| `marriage-cert-1905` | civil marriage certificate, Sebastian and Hildegard | German |
| `burial-register-1909` | the cemetery's burial register: Eugen's death, cause and burial | German |
| `directory-1912` | Flörburg address book: the firm (with its founding year) and the widow | German |
| `school-report-1930` | Rudolf's school report | German |
| `birth-extract-1938` | Gustav's birth register extract, issued for emigration | German |
| `postcard-1938` | Sebastian to Klara in Australia, dated only by its postmark | German |
| `letter-excerpt-1939` | the top of Sebastian's July 1939 letter to Klara, as an image on page 6 of the 2011 booklet: an artifact of its own, `part of` the booklet | German |
| `death-certificate-1941` | Rudolf's Soviet death certificate: his name surname first, with a patronymic, and his village with its district, oblast and republic, a field each | Russian |
| `registration-card-1945` | Ida's post-war card, with her son Leopold's arrest | English |
| `naturalisation-1952` | Gustav's naturalisation as Gustave Lobig | English |
| `letter-to-sipkje-1952` | Sybren van 't Nijks in Snylsjum to his sister Sipkje, Florence's mother, in Stupfield: their widowed mother is now Nicholas's great-grandmother | West Frisian |
| `compensation-file-1953` | Leopold's claim for his late mother Ida: her deportation | German |
| `gravestone-photo-1956` | photograph of Eugen and Cäcilie's gravestone | German |
| `divorce-judgment-1957` | the Landgericht Rekau's judgment divorcing Leopold and Agnes | German |
| `letter-to-leopold-1958` | Nina Sartina, Rudolf's widow, to Leopold, with the certificate: her married name, what her village calls her, and Rudolf's uncle and cousin | Russian |
| `family-tree-1984` | the tree Klara drew in Australia | English |
| `interview-2002` | oral history with Nicholas (possibly living), who says where Gustav is buried | English |
| `family-history-2008` | *A few pages of family history*, Nicholas's two typed pages: the hatters of Flörburg, Sebastian and Hildegard's children, the other Ursula Löbig, the emigrations, "I was born in '50" and his father's grave | English |
| `family-booklet-2011` | *Our Family from Germany*, Nicholas's compilation: the letter's excerpt beside his translation, made with translation software, and his commentary | English |
| `library-talk-2018` | *Where the Lobigs came from*, Nicholas's three slides for a library talk, with a photograph from the family album: its text is the deck's slide text | English |

## Pattern index

| Pattern | In the universe |
|---|---|
| Birth, married and nick names (ADR-0011) | Ida: born *Ida Sörne*, married *Ida Löbig*, nickname *Peppi*; likewise Cäcilie (*Cilli*) and Hildegard (*Hilde*) |
| German and English forms | Löbig/Lobig, Gustav/Gustave, Klara/Clara, Flörburg/Floerburg, Gerichtsgefängnis Peiltal/Peiltal court prison, Hutmacherei zum Blauen Hut/the Blue Hat, Hutmacher/hatter |
| Diminutives | German *Wastl, Cilli, Hilde, Gustl, Rudi*; English *Gus*; pet names *Muckel, Peppi, Pünktchen* |
| Names as sources write them (ADR-0022) | *Cäcilie Löbig geb. Tarle*; *Hildegard Löbig geborene Laudler*; *Agnes Löbig geb. Nertler*; *LÖBIG, Ida, née SÖRNE*; *Gustave LOBIG (formerly Gustav LÖBIG)*; *Löbig, Ursula, Wwe.*; *Mrs Cutmore*; *Sebastian 'Wastl'*; *Лёбиг Рудольф Феликсович*; *Нина Васильевна Сартина*; *S. Faxridge-van 't Nijks*; *S. van 't Nijks*; the placeholder *N.N. Löbig* |
| A surname written first, with no comma | *Лёбиг Рудольф Феликсович*: surname, given name and patronymic, in the 1941 death certificate's form |
| A patronymic | *Феликсович*, "son of Felix" (the certificate): a `father` claim, which the school report and the tree corroborate; *Васильевна*, "daughter of Vasily" (the 1958 letter): a `father` claim naming Vasily Sartin, whom no other source names |
| A married-name marker | *по мужу Лёбиг*, "Löbig by her husband" (the 1958 letter); *S. Faxridge-van 't Nijks*, her husband's surname, a hyphen, then her own (the 1952 letter). Each passage gives a `married_name` and a `birth_name` |
| A married-name marker written as a suffix | *Рудольфиха*, Rudolf's given name with the colloquial suffix *-иха*, "Rudolf's wife": what Nina's village calls her (the 1958 letter). Russian uses such forms in rural speech, never in an official name, so it is a `referred_to_as` appellation and a `spouse` claim, never one of her names |
| A surname with a masculine and a feminine form | *Сартин* and *Сартина*: Pavel Sartin and his sister Nina (the 1958 letter) |
| A name particle | *van 't Nijks*, kept whole in every name that carries it: *S. van 't Nijks* and *S. Faxridge-van 't Nijks* (the 1952 letter's envelope) |
| Widow and "the late" markers | *вдова Вашего покойного брата Рудольфа*, "the widow of your late brother Rudolf" (the 1958 letter); *Se is widdo sûnt ús sillige heit stoarn is*, "she is a widow since our late father died" (the 1952 letter). A widow marker is a `spouse` claim, and "the late" a death *before* the letter's date: Rudolf's, which the certificate gives as 6 March 1941 and the tree as 1941 |
| Kinship terms with a side and a degree | *дядя по отцу*, "uncle on the father's side" (Sebastian), and *двоюродная сестра*, "first cousin" (Klara), with Rudolf as the ego (the 1958 letter); *oerbeppe*, "great-grandmother" (Sipkje's mother), with Nicholas as the ego (the 1952 letter). Each is `relative` + `kinship_type`, worded as the source words it, and each follows from the parent edges |
| An institution named by its kind and its place | *Калдовское районное бюро ЗАГС*, the civil registry of Kaldovsky District, whose name it takes as an adjective (the certificate); *it tichelwurk fan Snylsjum*, "the Snylsjum brickworks" (the 1952 letter) |
| A place named with its containers | *д. Кошновка Калдовского района Чадской области*, "Koshnovka village, Kaldovsky district, Chadsk oblast", its district and oblast in the genitive (the 1958 letter), and the certificate's form, a field each; *Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân*, the 1952 letter's sender. Each container is a `part_of` claim |
| Homonyms (ADR-0017) | two Ursula Löbigs: Sebastian's daughter, born 1911, and a widow in the 1912 directory; a one-year-old cannot be that widow. The 2008 family history names both, one line apart |
| Mirrored kinship | Sebastian and Hildegard's children, stated from both ends: the parents' `child` claims in the 2008 family history and the 2018 talk; the children's `father` and `mother` elsewhere (the tree, the GEDCOM rendering), and the postcard's *Dein Vater* |
| Two-digit years | *I was born in '50* (the interview, the 2008 family history) and *Dad died in '96*: the century is the reader's to supply, which the fixture corpus's recorded model gets wrong once (below) |
| Spouses and parents, 3+ generations | four generations, Eugen to Nicholas; marriages in 1859, 1905, 1938, 1942 and 1947, and two undated ones |
| Divorce | Leopold and Agnes, married on 8 Feb 1947 and divorced on 2 Jan 1957 by the Landgericht Rekau (the judgment); the 1984 tree says *div. 1957*. The spouse statements carry it as `end_time`, the GEDCOM export as `DIV` |
| Kinship terms and courtesy titles (ADR-0018) | literal: *Dein Vater* (the postcard, from Klara's view); *Dad*, *Aunt Pünktchen*, *Dad's cousin Leopold* (the interview, from Nicholas's view). Each is recorded as the kinship, never as `referred_to_as`: *Dein Vater* and *Dad* as `father`; the other two as `relative` + `kinship_type`, with the referent as subject and the ego as object: `Klara —relative→ Nicholas ; kinship_type = "aunt"` (and *Pünktchen* is her `nickname`) and `Leopold —relative→ Gustav ; kinship_type = "cousin"` (the ego is *Dad*). Courtesy: *Nana Maisie* (the interview), a neighbour who is no relation: `referred_to_as`, never `relative` |
| Absolute and relational names (ADR-0018) | absolute, what the person is called by anyone: *Peppi*, *Gustl*, nicknames (an alias and a record nickname); relational, what one person calls another or a name through a relation: *Nana Maisie*, *Рудольфиха* (above), and *Bubele*, ADR-0018's pet name a grandmother gives a grandchild, which no source in the universe uses: `referred_to_as`, never an alias or a record nickname |
| Family firm (ADR-0012) | Hutmacherei zum Blauen Hut, hat makers, founded 1863 (*gegr. 1863*), at Brunnenring 35, owned by Sebastian in 1912, after his father's death in 1909 (*Inh. Sebastian Löbig*), gone *before 1926* |
| Other agents and kinds | Standesamt Flörburg, Städtische Realschule Flörburg, Wiedergutmachungsamt Peiltal, Landgericht Rekau, Photograph Brüsrich, the Kaldovsky district civil registry, the Snylsjum brickworks (organisations); RMS Flund (vessel); occupations *Hatter*, *Master hatter* and *Fitter* (*слесарь*) |
| Street address and containment (ADR-0009) | Brunnenring 35 → Brunnenring → Flörburg → Kreis Peiltal → Hesse → Germany; 8 Hawthorn Road → Hawthorn Road → Stupfield → Victoria → Australia; Koshnovka → Kaldovsky District → Chadsk Oblast → Russia; Snylsjum → Drakeradiel → Friesland → Netherlands |
| A place inside a same-named parent | the town Rekau in the urban district Rekau (*Stadtkreis Rekau*) |
| A place named two ways over time | Flörburg, *Bad Flörburg* from 1954; the 1984 tree uses the new name for an 1836 birth, the naturalisation spells it *Floerburg* |
| A place under two parents | Fläckfeld: Kreis Mockburg (1905) or Kreis Peiltal (1984) |
| A place with no known location | Klurnau |
| Persecution events (ADR-0014) | Leopold's arrest (3 Sep 1939), imprisonment in the Peiltal court prison and release (1 Oct 1939); Ida's deportation to Lager Klurnau (21 Aug 1942) and liberation at Klurnau (28 Jan 1945). Emigrations are `migration` events too |
| Contradictory dates (ADR-0002) | Gustav born 7 Jan 1919 (civil register) and 1 July 1919 (naturalisation): day and month transposed. Also Fläckfeld's district, and Rahel's address in July 1939 (the booklet, below) |
| Refinement, not contradiction | Eugen died 1909 (gravestone) and 16 Mar 1909 (burial register); Klara left in '38 or '39 (interview) and on 22 June 1938 (postcard); Leopold born 1914 (tree) and 29 May 1914 (judgment), Agnes likewise; Leopold held "in Peiltal" and in its court prison; Ida deported to Klurnau (card) and to Lager Klurnau (compensation file); Rudolf died 1941 (tree), 6 March 1941 (certificate) and before 25 December 1958 (the 1958 letter) |
| Uncertain dates | circa: Cäcilie *c. 1833* (`ABT 1833`); before: the firm gone *before 1926* (`BEF 1926`); after: Rahel died *after 1945* (`AFT 1945`); between: Klara left *between 1938 and 1939* (`BET 1938 AND 1939`); decade: the studio portrait, *1890s* (`BET 1890 AND 1899`) |
| An embedded original and its translation, as two witnesses (ADR-0008, amended) | *Our Family from Germany* (2011), Nicholas's compilation, sets an image of the top of Sebastian's 1939 letter to Klara (`letter-excerpt-1939`, an artifact of its own, `part of` the booklet on page 6) beside his translation. Where the letter says Rahel now lives at *Angerring 11*, the translation reads *Angerring 71*: each witness gives Rahel's address its own value. The booklet says its translations were done with translation software, so each claim read from the translation carries `sourcing_circumstances` → `machine_assisted` and no claim of the excerpt does. The caption and the commentary are not translations and carry no marker; the commentary says what the letter does not, that Gustav was Ursula's younger brother (`sibling`) |
| A person without name or sex | the infant of 1922, *N.N. Löbig* (`SEX U`) |
| People named only by a kinship term | Sipkje and Sybren's parents, *Mem* (mother) and *heit* (father) in the 1952 letter: each has a sex, a placeholder name (*N.N. van 't Nijks*) and the letter's `mother` or `father` claims |
| Privacy (ADR-0020) | Nicholas Lobig (b. 1950, no death asserted) is possibly living; he is dated to the year only, and the 1952 letter, which names him as a baby, gives no date |
| GEDCOM rendering (ADR-0021) | `tests/fixtures/example-family.ged`: typed names, `NICK`, an inline `(Peppi)`, `_MARNM`, `TYPE immigrant`, a dated burial and an undated one, cause of death, child count, residences at street addresses, a `DIV`, an `EDUC` and a `NOTE` the adapter reports as unsupported, and the studio portrait as a shared `OBJE` |

## Using it

```python
from tests.fixtures import example_universe as u

u.about("gustav-loebig", "date_of_birth")    # three sources, two days
u.containment_paths("brunnenring-35")        # address -> ... -> country
str(u.Date(1938, qualifier="between", until=u.Date(1939)))
# 'between 1938 and 1939'
u.NAMING_PATTERNS["patronymic"]              # (source, passage) pairs
```

People, places, entities (organisations, occupations, a vessel,
events) and sources are ground truth, so tests can check identity and
containment decisions against it. Everything said *about* them is a
source-attributed `Assertion` in the archive's own vocabulary: manifest
property ids, entity kinds and artifact classes. One value is planned
rather than in the manifest yet: `machine_assisted`, which ADR-0008's
follow-up B adds and `PLANNED_VOCABULARY` names. `CONTRADICTIONS` and
`REFINEMENTS` name the deliberate cases, and `NAMING_PATTERNS` names the
passages that show each naming pattern mention records must handle
(#385). Each person's `gedcom_xref` points into
`tests/fixtures/example-family.ged`, and
`tests/unit/test_example_universe.py` checks that the GEDCOM file says
nothing the sources do not.

## The fixture corpus

`tests/fixtures/corpus/` (#76) holds five documents of the universe for
offline end-to-end tests, one of each kind the scratch driver plans, in
`documents/`:

| Document | What | Kind |
|---|---|---|
| `family-history-2008.txt` | the two-page family history | text file |
| `library-talk-2018.pptx` | the three-slide talk, with its photograph | deck |
| `lobig-family.ged` | a small GEDCOM export of the Lobig line | GEDCOM |
| `naturalisation-1952.png` | the naturalisation certificate, a page | image |
| `postcard-1938.png` | the postcard, a short German letter | image |

`build.py` makes them (`uv run python -m tests.fixtures.corpus.build`),
and a test fails when the committed files differ from what it makes. The
two images are small stand-ins that show no text; the export is
`example-family.ged`'s Lobig line, record for record, with two traps of
its own. `engine.RecordedEngine` answers every call the stages before
write make of them, as a faithful model would, from the universe's own
assertions for each source (`answers.py`), with one deliberate mistake: it
reads the family history's *'50* as 1850, and reads it right once the
chunk prompt carries a rule about two-digit years. Its answers take the
form the extraction prompts ask for (ADR-0035, ADR-0036; #407): each
person, place and organisation of a manifest has its mention record (the
postcard's writer, whom it names only as *Dein Vater*, is labelled so,
with the status `kinship`); each event's participant has a role; and each
of the universe's spouse assertions is answered as a marriage whose
couple take part in it as spouses, started by a wedding where the source
dates the start.
`tests/unit/test_fixture_corpus.py` takes the corpus through whole scratch
runs under a refusing wiki, and asserts what must come of each stage and
each trap, with no gold set. Past the driver it holds the run report
(`yizkr scratch report`) and the clustering stage's identity map to the
corpus too (#380): the report's aggregates for the corpus, with no
universe name, file name or SHA-1 in report.md, and each trap's
clustering outcome, the two the whole-cluster merge rule cannot meet
(#361) included, each recorded as expected with its reason.

`TRAPS`, in `tests/fixtures/corpus/__init__.py`, names the six traps the
corpus sets, in #76's words: the documents and people each involves, how
the corpus sets it, and what must come of it, clustering's outcomes
included. `PRIVACY_TRAPS` gives the evidence of the four that bear on
privacy, in the universe's terms, for the possibly-living predicate's
tests (#120): `from tests.fixtures.corpus import PRIVACY_TRAPS`. To add a
document, give it a source in `tests/fixtures/example_universe.py` first,
then its bytes in `build.py`, its entity manifest in `answers.py` and an
entry in `DOCUMENTS`, and build again.

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
  (#31), the prompts and source comments (#32) and the tests (#33). The
  last two, in `claims.TimeValue`'s docstring, are replaced with #231's
  extraction-prompt change, since the extraction prompt embeds that
  docstring.
- CLAUDE.md and docs/AGENT_PROTOCOL.md state the rule (ADR-0024). The
  token denylist once used to check additions is retired (ADR-0024 §5).
