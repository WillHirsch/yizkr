# Examples: one fictional universe

**The rule.** Every example in code, comments, prompts, tests, ADRs,
issues and PRs comes only from the fictional universe defined in
[`tests/fixtures/example_universe.py`](../tests/fixtures/example_universe.py).
Never use real family data, not even partially anonymised: no real name
with a changed surname, no real date shifted by a year, no real town,
address or family firm. If an example needs a pattern the universe
lacks, add it to the universe first (see *Extending* below).

**Why.** The archive holds records about real, partly living people, and
examples travel: prompts go to models, issues and PRs are read by agents,
and the repository may be shared. One consistent cast also lets tests,
prompts and ADRs tell the same story.

## The world

Everything is invented, down to the countries.

- **Exampleland** (German *Beispielland*), German-speaking: Example County
  holds the towns of Springfield (with Rosengasse 12, the Lower Town and
  Springfield Hospital) and Lakeside (with Lakeside Cemetery). Capital
  City is a city inside a city-state of the same name, with the Capital
  City police prison. Musterfeld camp is in the Eastern Province.
- **Havenland** (German *Hafenland*), English-speaking: Sampleton, with
  4 Station Road and Laurel Bank Maternity Home. Older sources place it in
  Westshire, newer ones in South Westshire (created 1974).

## The family

The Beispiels became the Examples after emigrating to Havenland.

```
0  Gustav Beispiel (1851-1909) = Ottilie Faber          founded the firm 1887
   |
1  Bern Beispiel (c. 1880-1950) = 1902 Alma Hedwig Muster "Hedi"
   |  Hugo Beispiel sen. (1882-after Oct 1942) = 1900 Leonore Grau "Lore"
   |
2  Carla Beispiel (1910/12-1994) = 1935 Dov Muster (div. 1939), = 1946 Roland Thorne
   Hugo Beispiel jun. (1901-1987) = 1924 Margarete Sommer "Gretl"
   |
3  Gustav "Gus" (1925-2004), Irmgard "Irmi" Example-Hollis (1927-2011)
   = Neville Hollis, Ignaz "Iggy" (1929-2015, later Ian Example)
   |
4  Nora Example (b. 1958), daughter of Gus
```

Bern and Hugo sen. are brothers, and Carla is Bern's daughter. Hugo jun.
is Hugo sen.'s son. Not kin: Elfriede "Friedl" Kranich (later Elfreda
Crane), a family friend, and the tree's unfilled *Nameless Placeholder*.

## Pattern index

| Pattern | In the universe |
|---|---|
| Birth, married and nick names (ADR-0011) | Alma: born *Alma Hedwig Muster*, married *Alma Beispiel*, nickname *Hedi* |
| German and English forms | Beispiel/Example, Kranich/Crane, Muster/Masters; Ignaz/Ian, Margarete/Peggy; Exampleland/Beispielland, Lower Town/Unterstadt |
| Diminutives | German *Gretl, Irmi, Friedl, Lore, Gustl, Hedi*; English *Gus, Iggy, Peggy* |
| Names as sources write them (ADR-0022) | *Carla Beispiel/Muster/Thorne*; *Irmgard Example-Hollis (b. Beispiel)*; *Dov Muster (later Don Masters)*; *Elfriede (Friedl) Kranich*; *Ian "Iggy" Example*; *Hugo Beispiel sen.* / *jun.*; the ambiguous initial *G. Beispiel* |
| Homonyms (ADR-0017) | two Gustav Beispiels with disjoint lifespans; two Hugo Beispiels who are father and son. Alma's Musters and Dov's Musters share a surname but are unrelated |
| Spouses and parents, 3+ generations | Gustav, Hugo sen., Hugo jun., Gus, Nora; seven marriages, one ended by divorce |
| Kinship terms and courtesy titles (ADR-0018) | literal: *Oma Lore*, *Opa Hugo*, *Großtante Alma* (Ignaz's memoir), *our Uroma Lore* (Nora's tree); courtesy: *Oma Friedl*, who is no relation; pet name: *Mäuschen* |
| Family firm (ADR-0012) | Beispiel & Söhne (in full *Seifenfabrik Beispiel & Söhne*), a soap works in the Lower Town, employing Hugo sen. as *Prokurist* and Hugo jun.; OCR form *Seifen=Fabrik Beispiel & Söhne*, a Fraktur double hyphen. It is named after people but is an organization |
| Other agents and kinds | Example Academy; Example County Fire Office (*Feuerkasse Kreis Beispiel*; issuing authority, so boilerplate); Exampleland Tracing Service; Sampleton Refugee Committee; vessel SS Havenstar; family *the Beispiels* |
| Occupation wording | *Cigar maker* (tree) and *Zigarrenmacher* (address book) |
| Street address and containment (ADR-0009) | Rosengasse 12, Springfield, Example County, Exampleland; 4 Station Road, Sampleton, Westshire or South Westshire, Havenland |
| Resolution and naming conventions | *Springfield Hospital, Lindenallee* and *Springfield* for one birth; *Sampleton, Westshire, Havenland* and *Sampleton, South Westshire*; *Capital City* and *Capital City, Exampleland*; the misspelling *Espringfeld* |
| Eponymous nesting | the city of Capital City inside the city-state of Capital City |
| Persecution events (ADR-0014) | deportation of Hugo sen. and Leonore to Musterfeld camp (Oct 1942); imprisonment of Dov in the Capital City police prison (November 1938 to January 1939, as he recalled in a 1978 interview) and his release; Leonore's release from Musterfeld (May 1945) |
| Contradictory dates (ADR-0002) | Leonore born 3 Nov 1880 (marriage register) and 3 Nov 1881 (deportation list); the family's emigration in Aug 1938 (passenger list) and 1939 (memoir) |
| Refinement, not contradiction | Ignaz born 3 Mar 1929 (certificate) and 1929 (memoir); at Springfield Hospital and in Springfield |
| Uncertain dates | circa: Bern *c. 1880* (`ABT 1880`); before: Alma *before 8 May 1945* (`BEF`); after: Hugo sen. *after October 1942* (`AFT`); between: Carla *1910 to 1912* (`BET 1910 AND 1912`); decade: Ottilie's weathered *185_* |
| A date never guessed | the placeholder record's `SOMEDAY MAYBE` |
| Privacy (ADR-0020) | Nora Example (b. 1958, no death asserted) is possibly living |
| Sample texts | `Source.text`: German (birth certificate, insurance policy, address book, deportation list, gravestone) and English (memoir, passenger list, newspaper interview, death notice) |

## Using it

```python
from tests.fixtures import example_universe as u

u.about("leonore", "date_of_birth")    # both assertions, one per source
u.containment_paths("rosengasse_12")   # street -> town -> region -> country
str(u.Date(1910, qualifier="between", until=u.Date(1912)))  # between 1910 and 1912
```

The module keeps a ground truth (who exists, which place contains which)
so tests can check identity and containment decisions against it.
Everything said *about* people is a source-attributed `Assertion`, like
the archive itself. `CONTRADICTIONS` and `REFINEMENTS` name the deliberate
cases. `tests/fixtures/example-family.ged` is an excerpt of the family
tree's GEDCOM export, and each person's `gedcom_xref` points into it.

## Extending

Add to the module rather than inventing a one-off name inline, and keep
`tests/unit/test_example_universe.py` green. Before committing, check
every new name, place, organisation and exact date against the
out-of-repo denylist (#35), both its enforced and its review lists.
Prefer names built on the universe's own roots (Beispiel, Muster,
Example, Sample-) to real small towns or real surnames.

## Status

- `tests/fixtures/example-family.ged` predates the universe. The records
  for Alma Beispiel and Elfriede Kranich (see their `gedcom_xref`) still
  carry earlier names, and Alma's an earlier birth date. Other tests,
  prompts and ADRs still use earlier examples too. Replacing them is #31,
  #32 and #33.
- CLAUDE.md and docs/AGENT_PROTOCOL.md state the rule once #20 lands.
