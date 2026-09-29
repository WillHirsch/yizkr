# ADR-0035: Extraction hands over structure — mention records, participant roles and kinship classes

Status: **proposed** — drafted for #385, which carries out the owner's
decisions of 2026-09-29: before scratch run 1 (#145), extraction hands
over the structure of what it reads, and code keys and judges on that
structure instead of on fixed word lists; the owner also asked for a sweep
of other code that reads meaning from wording, and for judgement notes.
Nothing implements it until the owner accepts it. It amends ADR-0007 (the
entity manifest's record gains a structure and a note), ADR-0011 (the
GEDCOM adapter's inline nicknames), ADR-0014 and ADR-0018 (a kinship term
gains a class beside its wording) and ADR-0022 (the manifest may hold a
reading of which name is a given name and which a surname; the graph still
holds none). It asks for ADR-0013's type hints in English. It carries the
structural half of ADR-0031's rule, the kinds each property accepts. It
replaces the name reading of the corpus clustering stage (#105, PR #361)
and keeps that stage's closure, cannot-link and classification logic.
Marriages and partnerships: ADR-0036 (proposed, #311).

Line numbers cited are those of 2026-09-29: main for paths under
`src/yizkr/`, and PR #361's branch for `clustering.py`, `clustering.yaml`
and `fingerprints.py`.

## Context

**Clustering reads structure back out of free text.** The clustering stage
of PR #361 needs to know which word of a label is a surname, which is a
title, which part of a place name is the place itself, and whether an
organization's name is its own or only its kind. Extraction never says,
so the stage rebuilds it from each label with fixed word lists
(`clustering.py`, 295-398):

| List | Words | What it stands for |
|---|---|---|
| `TITLES` | 28 | honorifics |
| `KIN_WORDS` | 49 | kin and courtesy words |
| `PARTICLES` | 20 | name particles and generational words |
| `FORM_MARKERS` | 11 | "formerly", "later", "called" |
| `MAIDEN_MARKERS` | 4 | maiden-name markers |
| `OCCUPATIONS`, `OCCUPATION_ENDINGS` | 63 + 8 | occupations beside a name |
| `ORG_GENERIC` | 25 | words no organization is known by |
| `ORG_INSTITUTIONAL` | 70 | words naming an institution's kind |
| `PLACE_GENERIC` | 25 | generic heads of place names |

The lists are almost entirely German and English, so they cover only the
languages someone wrote them for. A list is never complete. And they
partly redo what ADR-0011 (name typing) and ADR-0018 (relational
appellations) already ask extraction to do.

**The sweep.** The owner asked for a read-only sweep of other code that
reads meaning from wording. Probed with fictional names, it found the same
kind of reading well beyond the lists:

- given names and surnames read from word order, commas and brackets:
  "LÖBIG Ida" keys Ida as the surname (`clustering.py`, 581-603 and
  618-655);
- given names matched by prefix, so a masculine given name meets the
  feminine form that adds a letter to it, while "Gustl" never meets
  "Gustav" (658-680, 1213-1227);
- the GEDCOM adapter types any short bracketed part of a name as a
  nickname, "(geb. Laudler)" included (`pipeline/gedcom.py:646-665`);
- place structure read from commas and token length, so a toponym of
  three letters or fewer never keys (`clustering.py`, 1002-1022;
  `clustering.yaml`, 21);
- accent folding that keeps "Flörburg" and "Floerburg" apart (522-527);
- a label the model composes for someone unnamed ("Klara's father")
  keying like a name;
- alias cleanup by token shape (`pipeline/names.py:36-77`);
- markers that state a relation or a death (a widow's marker, "the late",
  patronymic and married-name markers) read as parts of names;
- a birth or death event with one participant taken as that person's own
  (`clustering.py`, 956-970), so a death that names only its informant
  dates the informant's death: a participant claim cannot say how someone
  took part. The possibly-living check has the same blind spot (#345).

**Extraction already knows.** The entity-manifest turn reads the whole
document. It unifies an entity's name forms (ADR-0007), types the names a
source marks (ADR-0011), routes kinship terms and courtesy appellations to
claims (ADR-0018) and strips annotations from labels (ADR-0022). Then it
hands clustering flat strings, and clustering guesses again, with lists,
what the model had already decided.

**ADR-0022's objection, and what changed.** ADR-0022 ruled that a name's
kind (given name or surname) is never asserted and has no home in the
schema, since it cannot be decided without a name-order convention. That
ruling guarded the graph, where a reading would be written as if a source
had asserted it. Unattended clustering (ADR-0025) cannot do without the
reading: #361 already makes it, from position and commas. The question is
not whether to read it, but who reads it, from what, and where it is kept.
The model can read the document's own conventions; a list reads none.

## Decision

### 1. The principle

**Extraction hands over structure. Code keys and judges on that
structure, never on wording, word order or punctuation.**

- The model, or for a GEDCOM export the export's own tags, says what each
  part of a name is. Code never decides it from a word list, a position, a
  comma, a bracket, capitals or a token's length.
- A word that states a fact rather than naming someone (a widow's marker,
  "the late", a kinship term, a role in an event) becomes a claim, with
  its source, never a part of a name (§3).
- A reading that no source asserts (which part is the surname, which word
  is a place's generic head, whether an organization's name is its own)
  stays on the extraction side, as input to identity. It is never written
  (§2.6).
- Code compares whole typed parts, folding only case and accents, plus one
  stated rule for umlaut spellings (§7).

### 2. Mention records

Each entity of the entity manifest (a *mention*, as clustering calls it)
of four kinds gains a `structure`: what its names are made of, read from
its label and aliases as the document writes them. A record holds only
what identity reads. A person's `status` defaults to `name`, a place's to
`toponym`, and a surname's type to `other`.

#### 2.1 A person (`human`)

| Field | What it holds | Keys? |
|---|---|---|
| `status` | what the label is: `name`, `role`, `kinship`, `courtesy`, `placeholder` or `description` | only a `name` keys |
| `given` | each given name as written, with its standard form; an initial is marked as one | yes, by standard form |
| `surnames` | each surname as written, particles kept in it, typed `birth`, `married` or `other` | yes |
| `nicknames` | the informal names the person is called by, as written | yes, as given names |
| `titles` | honorifics and ranks | never |
| `suffixes` | generational and status words | never |
| `unsplit` | a name form whose given name and surname the source does not tell apart | yes, read both ways |

- **The source's conventions decide which part is which, never a rule
  about a language.** The registration card's "LÖBIG, Ida, née SÖRNE"
  gives the given name Ida and the surnames LÖBIG (`other`) and SÖRNE
  (`birth`): the card writes surnames first and in capitals, and the model
  reads that. A register, a directory, a form's labelled fields, a
  signature and running prose each have conventions of their own. Where
  the document does not settle the order, the form is `unsplit`.
- **A surname is `birth` or `married` only where the source marks it**
  (ADR-0011; ADR-0022 Ruling 2). "Cäcilie Löbig geb. Tarle" gives Tarle
  as `birth` and Löbig as `other`. A title never types a surname: "Mrs
  Cutmore" gives the title Mrs and the surname Cutmore, `other`.
- **Particles stay in their surname** (ADR-0022 §4), so a surname keys
  whole.
- **A standard form** is the full form of a given name in its usual
  spelling, which a diminutive or an abbreviation stands for, as the
  document supports it: the postcard's "Gustl" is Gustav. A full name is
  its own standard form. A standard form is never a translation, and an
  initial has none.
- **A nickname is a name; a title or a status word is not.** "Sebastian
  'Wastl'" gives the nickname Wastl. The directory's "Löbig, Ursula, Wwe."
  gives the suffix Wwe., which never keys; her widowhood is a claim
  (§3.1).
- **What is not a name is no part of one.** The burial register's "Löbig,
  Eugen, Hutmachermeister" gives Eugen and Löbig, and the occupation is an
  `occupation` claim. The naturalisation's "Gustave LOBIG (formerly Gustav
  LÖBIG)" gives both given names and both surnames, and the change of
  name is the chunk extraction's `official_name` claim (ADR-0011).
- **The status says whether the label names anyone.** ADR-0013 and
  ADR-0018 already tell these apart in claims:
  - `name`: the label names the person;
  - `role`: a role or form-field label, such as INFORMANT or MOTHER
    (ADR-0013);
  - `kinship`: a literal kinship term, such as the postcard's signature
    "Dein Vater" (ADR-0018 (A));
  - `courtesy`: a courtesy title or pet appellation that is no literal
    relation, such as "Nana Maisie" where a document names her no other
    way (ADR-0018 (B));
  - `placeholder`: the source's stand-in for a name it does not know,
    such as "N.N. Löbig";
  - `description`: a label the model composed for someone the document
    does not name, such as "Klara's father" for "Dein Vater".

  Where a document names the person at all, the label is the name, and
  the relational reference is a claim (ADR-0018).
- **Sex is never read from a given name.** A source states it in words or
  in its grammar ("ein Knabe" in the birth register extract, "den Schüler
  Rudolf Löbig" in the school report), as a `sex_or_gender` claim.

#### 2.2 A place (`place`)

| Field | What it holds | Keys? |
|---|---|---|
| `status` | what the label is: `toponym`, `deictic` or `generic` | only a `toponym` keys |
| `names` | the place's own names as written, each with its toponym and generic head where they can be told apart | yes, each as written |
| `address` | a street and a house number, as written | yes, the two together |
| `within` | the places the source puts it in, as written, in the source's order | never; they decide nesting |

- "Fläckfeld, Kreis Mockburg" (the marriage certificate) gives the name
  Fläckfeld, within Kreis Mockburg.
- "Lager Klurnau" gives one name, with the head Lager and the toponym
  Klurnau. It keys as written, so the camp never meets the town Klurnau.
  "Stadtkreis Rekau" likewise never meets the town Rekau inside it.
- "8 Hawthorn Road, Stupfield, Victoria" gives the address Hawthorn Road
  8, within Stupfield and Victoria.
- The compensation file's "in das Lager Klurnau verschleppt, dort befreit"
  refers back to the camp: "dort" is `deictic`, never an alias and never a
  place of its own (#42). A place named by a head alone ("Lager", with no
  toponym) is `generic`.
- A head never keys on its own. A container never keys at all: it only
  decides whether one place nests in another (§7).

#### 2.3 An organization (`organization`)

| Field | What it holds | Keys? |
|---|---|---|
| `names` | its proper names, as written | yes |
| `kinds` | the words naming what kind of institution it is, as written | only with a place |
| `places` | the places its names carry, as written | only with a kind |

- "Hutmacherei zum Blauen Hut" gives the name "zum Blauen Hut" and the
  kind Hutmacherei. "Photograph Brüsrich" gives the name Brüsrich and the
  kind Photograph.
- "Städtische Realschule zu Flörburg" and "Standesamt Flörburg" have no
  proper name: each gives a kind and the place Flörburg, and is known by
  the two together.
- Generic words that any organization carries (a word for a firm or a
  company, a legal form) are neither names nor kinds: "Firma Löbig" gives
  the name Löbig alone.

**ADR-0031's rule** (the owner's decision on #202): the relation a source
states decides the kind of what it points to. Where the Landgericht Rekau
divorces Leopold and Agnes, the court acts, and it is an organization,
with the kind Landgericht and the place Rekau. Where a source names a
court, a school or a camp only as where something happened, it is a place:
Ida's deportation to Lager Klurnau has the camp as its location, and gives
the place record of §2.2. Where one source uses the same institution both
ways, the manifest holds two entities, a site and an institution, each
with its own record, linked by the organization's `location` claim.
Neither record keys like the other, and neither meets the town.

#### 2.4 A family (`family`)

| Field | What it holds | Keys? |
|---|---|---|
| `surnames` | the surnames the family is named by, as written | yes, but a surname alone never merges two families |

"Familie Löbig", "the Löbigs" and "the Lobig family" each give the one
surname.

#### 2.5 Other kinds

Events have no name structure: an event is identified by who took part,
in which role (§3.2), and when. Occupations, vessels, military units,
legal instruments, artifacts and unclassified entities have none either:
each keys on its exact normalised name, as in #361. A record for any of
them waits for evidence that one is needed, such as a scratch run's missed
pairs.

#### 2.6 Where records live

The record sits in `EntityRecord.structure`, beside the label: in the
batch view, and in the LLM cache's answer to the manifest call. The
batch's content hash covers it, so the identity map is made again when a
record changes (#361's reuse key).

**No record field is written to the graph.** A reading of a label is the
pipeline's interpretation, not something the source asserts: the card
writes "LÖBIG, Ida", it does not say that Ida is a given name. Written
with `stated in` the card, the reading would be attributed to a source
that never made it (CLAUDE.md rule 1; ADR-0002, ADR-0003); ADR-0022
rejected run-referenced name parts for the same reason. What sources do
assert about names keeps its home: typed name claims (ADR-0011),
`referred_to_as` and kinship claims (ADR-0018), and the label and aliases.
Records hold names, so they are local-only data, like the labels in the
batch views and the identity map.

ADR-0022's Ruling 1 stands for the graph, which still has no name-part
property. Its schema half, that the given-or-surname reading has no home
at all, gives way for the manifest. The prompt standard of ADR-0022 §3,
"never infer given vs family from word order or position", becomes: read
it from the document's own conventions, never from a rule about a
language, and leave a form `unsplit` when they do not settle it.

### 3. What becomes a claim

Structure that a source asserts is written, as claims and qualifiers that
carry the source's references.

#### 3.1 Relation and death markers

| The source writes | Extraction states |
|---|---|
| "dessen verstorbener Ehefrau Cäcilie" (the marriage certificate) | Cäcilie's death: a `death` event with her as principal (§3.2) |
| "seine verstorbene Mutter Ida" (the compensation file) | Ida's death, and `mother` |
| "Witwer der Cäcilie Löbig geb. Tarle" (the burial register) | Eugen's civil status `widowed`, and Cäcilie's death |
| "Löbig, Ursula, Wwe." (the 1912 directory) | Ursula's civil status `widowed`, at the directory's date |
| "Sohn des Hutmachermeisters Eugen Löbig" | `father` |
| a patronymic marker joining a given name to the father's | `father`; the father's name is no surname |
| a married-name marker | the surname typed `married`, and a `married_name` claim (ADR-0011) |

Marriages and partnerships: ADR-0036 (proposed, #311).

A new property, `civil_status`, holds a civil status the source states:
`unmarried`, `married`, `widowed` or `divorced`, dated as the source dates
it. Where the vocabulary has no claim for what a marker asserts, the chunk
writes a gap line (ADR-0013), never a name part.

#### 3.2 Participant roles

A `participant` claim gains a role, as Wikidata qualifies one with *object
has role* (P3831): `principal`, `parent`, `spouse`, `informant`, `witness`
or `officiant`. Gustav's birth register extract gives the boy as
principal, Sebastian and Hildegard as parents, and Sebastian, who reported
the birth, as informant too.

- A birth or a death is its principal's own, and its date moves only onto
  its principal. This replaces #361's one-participant rule: a death that
  names only its informant dates no one's death.
- #345 reads the role: a death event is death evidence for its principal
  alone.
- The GEDCOM adapter makes each person's death event with that person as
  principal.
- `spouse` stays a role, for the participants of a marriage under
  ADR-0036.

#### 3.3 Kinship classes

A `relative` claim keeps its `kinship_type`, the term as written
(ADR-0018), and gains a controlled class beside it: its `kinship_class`
(parent, child, sibling, spouse, grandparent, grandchild, aunt or uncle,
niece or nephew, cousin, parent-in-law, child-in-law, sibling-in-law, or
`relative` where the term's class is unclear), its `side` (paternal or
maternal, only where stated) and its `degree` (the steps a "great-" adds;
for a cousin, its degree and removes).

| Term | `kinship_type` | class | side | degree |
|---|---|---|---|---|
| "Aunt Pünktchen" (the interview) | aunt | aunt or uncle | — | 0 |
| "Dad's cousin Leopold" (the interview) | cousin | cousin | — | — (not stated) |
| "our great grandmother" (ADR-0018's Cäcilie) | great grandmother | grandparent | — | 1 |

A literal term that reduces to a primitive stays one: "Dein Vater" is
`father`. A side or a degree the term does not state is left out, never
guessed. `queries/kinship.rq` then reads classes instead of its term table
(`queries/kinship.rq:224-285`), a reader after write (§10).

#### 3.4 The kinds each property accepts

ADR-0031 rules that the relation decides the kind of what it points to.
Its structural half is decided here, with the rest of the manifest's
changes: the manifest lists, for each item-valued property and qualifier,
the kinds its value may be (`place_of_death` a place; `employer` and
`educated_at` an organization; `member_of` an organization or a family;
`perspective` a person or a family). Validation refuses a value of another
kind, as it refuses an unknown property, and the refusal is a counted
reject with its reason. The critic no longer has to spot a mismatch by
reading wording.

#### 3.5 The vocabulary this adds

| Key | What it is | Wikidata |
|---|---|---|
| `object_has_role` | qualifier on `participant` | P3831 |
| `principal`, `parent`, `spouse`, `informant`, `witness`, `officiant` | its values | — |
| `kinship_class` | qualifier on `relative` | P1039 |
| `side`, `degree` | qualifiers on `relative` | — |
| the kinship classes, `paternal`, `maternal` | their values | — |
| `civil_status` | a person's stated civil status | — |
| `unmarried`, `married`, `widowed`, `divorced` | its values | — |
| `accepts` | a field of each item-valued property: the kinds of its value | — |

### 4. Judgement notes

Any extracted fact, a claim or a manifest entity, may carry an optional
`note` where the extractor made a judgement call that a careful reader
could make differently. A note is a category (`kind`, `relation`,
`date reading`, `identity` or `other`) and one sentence.

- The postcard's postmark "19.8.38" read as 1938 is a `date reading`;
  "Dein Vater" recorded as Klara's `father`, not as a `relative`, a
  `relation`; the registration card's "Klurnau" read as the town, since
  the card names no camp, a `kind`.
- The prompt asks for a note only where a reasonable reader could decide
  otherwise. The run report counts notes by category, so over-use shows,
  and the review sheet shows each note beside its fact.
- Notes stay in the run's outputs and are never written: they are about
  the reading, not about what the source says (ADR-0023). Like
  `confidence`, a note is part of no claim fingerprint (#197).

### 5. How extraction fills it

**The prompts.** The entity-manifest prompt asks for each field of §2, the
notes of §4, and a type hint in English, so that #91 can group gaps. It
describes each pattern by its structure (a surname-first register,
capitals, a maiden-name or married-name marker, a courtesy title, a
place's generic head, an institution's kind), gives examples from the
fictional universe, and lists no marker words, so it applies to sources in
any language. The chunk prompt asks for roles, kinship classes, civil
status, sex as the source or its grammar states it, and the claims of
§3.1. Both prompts' rule against inferring given name and family name
from word order (`pipeline/chunking.py:199-202`, 375) keeps its point for
claims, which never state that reading, and gives way for the manifest's
structure, which holds it; a name's type is still recorded only where a
source marks it.

**Validation (Pydantic).** The structure is a strict model that refuses
unknown fields, with one variant per kind and closed values for statuses,
surname types, roles, classes and note categories. Beyond the schema:

- the structure's variant matches the entity's kind;
- every part the model writes occurs, once folded, in the label or an
  alias. The model may divide and label a name, never add to it: ADR-0022
  §4's hallucination check, made structural. Standard forms are exempt;
- a `name` status needs a name part, and a `toponym` status a name or an
  address;
- a claim's value is of a kind its property accepts (§3.4).

**Omissions and inconsistencies are flagged, never repaired by parsing.**
An omitted optional field is empty. A structure that fails validation is
set aside, never the whole manifest: the entity keeps its label, aliases
and kind, and the reason is counted in the extraction's `coverage.json`.
Such a mention, like one of a batch without a manifest (the line-range
fallback of ADR-0007), is *recordless*: it keys on its exact normalised
name, never links confidently on it (§7), and is counted in the identity
map's summary and the run report. The capitalised-token coverage check
goes (`pipeline/chunking.py:1504-1553`), since it takes capitals for
names.

### 6. How the GEDCOM adapter fills it

An export's tags carry the structure, so the adapter reads it and guesses
nothing.

| Tag or form | Field or claim |
|---|---|
| NAME: the part before the slashes | given names, less an NPFX it starts with and its quoted or bracketed parts |
| NAME: the part between the slashes | a surname, typed by the NAME's TYPE |
| NAME: the part after them | suffixes, unless NSFX gives them |
| NAME with no slashes, and no GIVN or SURN | `unsplit` |
| GIVN; SURN with SPFX | given names; surnames, each whole with its particle |
| NPFX; NSFX | titles; suffixes |
| NICK, or a quoted part ("Cäcilie 'Cilli' Tarle") | nicknames |
| a parenthetical part ("Ida (Peppi) Sörne", "(geb. Laudler)") | an alternative form with no type, counted |
| TYPE birth or maiden; married | the NAME's surname `birth`; `married` |
| TYPE aka | the NAME's given names are nicknames |
| any other TYPE, or none | `other` |
| `_MARNM` | a `married` surname: the part between its slashes; a value without them is `unsplit` |
| PLAC | its first component the place's name and the rest `within`, each as written and unsplit, since GEDCOM lists jurisdictions from the lowest |
| HEAD.LANG | the language of the adapter's texts, instead of a fixed `en` (`pipeline/gedcom.py:982`, 1230-1234, 1413-1421); without it, undetermined |
| a DEAT that states the death | a `death` event with the person as principal |

A NAME part that the NAME's value does not restate still reaches its
field, and the name the parts give becomes an alias, so no NAME part is
dropped without a count (#352). A particle with no surname is counted.
An export's given name is its own standard form: the adapter has no model
to expand a diminutive, so the export's NICKs and the documents' standard
forms carry that work.

The parenthetical rule amends ADR-0011's inline convention: a bracket
holds a maiden name, a later name or a note as often as a nickname, while
a quote marks a nickname.

### 7. How clustering uses it

**Matching rules.**

1. **Keys come only from typed parts.**
   - Only names and toponyms key: a person whose status is not `name`,
     or a place whose status is not `toponym`, keys on nothing.
   - A person: each given name's standard form, and each nickname, with
     each surname of its own or of its lifted kin. With no surname of
     either, its given names alone, which pair only with the GEDCOM
     export, as in #361.
   - A place: each of its names as written; an address, its street with
     its number.
   - An organization: each proper name; without one, each kind with each
     place.
   - A family: each surname.
   - Any other mention with no typed part (recordless, of a kind with no
     record, or with a record that gives none) keys on its exact
     normalised name. For the four kinds that take records, that link is
     never confident: two documents' "N.N. Löbig" may be two children.
2. **Sex and civil status only block.** Stated sexes that differ, or civil
   statuses that cannot both hold (`unmarried` stated at a date after
   another source's `married`, `widowed` or `divorced`), make a pair
   ambiguous, and a closure that joins them a contradiction. They never
   corroborate. The 1912 directory's widow, Ursula Löbig, never merges
   with an Ursula Löbig whom a later source states was never married.
3. **Given names match on their standard forms, exactly.** Never by
   prefix: a prefix lets a masculine name meet its feminine form, and
   never lets Gustl meet Gustav.
4. **Folding** is case and accents after Unicode normalisation (#361's
   `fold`, 522-527), plus the umlaut rule below.
5. **A place nests** in another of the same name when its `within` are
   among the other's. Containers that differ keep a pair ambiguous: from
   the names alone, one Fläckfeld in Kreis Mockburg and in Kreis Peiltal
   cannot be told from two. #361 compares each name form apart; the record
   compares a place's containers as one set, so a short form of its name
   no longer lets a place nest in a namesake elsewhere.
6. **An organization** links on a shared proper name of two words or more;
   on a proper name of one word only with a shared kind or place, since a
   firm named after a surname, such as "Firma Löbig", may be any Löbig's;
   and without a proper name, only where its kind and its place both
   agree.
7. **A family** never merges on a surname alone.
8. **Events.** A birth or death is its principal's own and meets another
   through its principal. Other events meet through their participants,
   as in #361.

Everything else of #361 stays: dates as ranges at their precision, kin
lifted from both ends, soft and hard cannot-link, the closure and its
contradictions, the confident, ambiguous and conflicting classification,
GEDCOM anchoring, hubs, the identity map and its reuse. Its `VERSION`
bumps.

**The umlaut rule: a second key under DIN 5007-2.** A part that holds an
umlaut or ß keys twice: once with the accent folded (Flörburg as
`florburg`), and once with the umlaut written out (`floerburg`: ä as ae, ö
as oe, ü as ue, ß as ss). A two-letter spelling is never turned back into
an umlaut. So the naturalisation's "Floerburg" meets the register's
"Flörburg". The alternative was a standard spelling the model gives for
each part. It would reach spelling variants of any language, but the
GEDCOM adapter has no model, so the export's parts would key one way and
the documents' another; a model's spelling varies from call to call; and
it invites "correcting" a spelling a person really used, such as the
emigrants' Lobig. **The pick is the DIN 5007-2 second key**: closed, and
the same on every run. Given names keep their model-given standard form,
since no rule can expand a diminutive, and it is folded the same way.

**The word lists go, and so do the token-length thresholds.**

| #361 reads | Where (`clustering.py`) | Replaced by |
|---|---|---|
| `TITLES` | not a name token (608); a lone token after one names a surname alone (612-615) | `titles`, which never key; a record with surnames and no given name or nickname names a surname alone |
| `KIN_WORDS` | not a name token (608) | `status` `kinship` or `courtesy`, which key nothing; the term becomes a claim (§3.3) |
| `PARTICLES` | not a name token (608) | kept whole in `surnames`; generational words in `suffixes`; a patronymic becomes a `father` claim |
| `FORM_MARKERS` | not a name token (608) | one record gathers every form's parts; the marker is none of them |
| `MAIDEN_MARKERS` | a surname-first name split at one (561-569); "X Y née Z" read as two forms (629-645) | a `birth` surname; every surname keys with every given name |
| `OCCUPATIONS`, `OCCUPATION_ENDINGS` | an occupation where given names stand (572-578, 597) | no name part; an `occupation` claim |
| the "N.N." stand-in | a surname-first name without given names (337, 603) | `status` `placeholder` |
| `ORG_GENERIC` | the tokens an organization is known by (683-690, 1024-1035) | `names` and `kinds`; generic words are neither |
| `ORG_INSTITUTIONAL` | a name of institution words needs a place (1601-1607) | an organization without `names` keys and links on kind and place |
| `PLACE_GENERIC` | heads never key (1003-1011) | `head`; a place keys on its name as written |
| brackets, commas, order | forms, surname-first order, a place's first comma part (552-558, 581-603, 1012-1022) | the record |
| `min-name-token`, `min-place-token`, the organization token floor | token length (`clustering.yaml`, 17 and 21; 400) | nothing: keys are whole parts |
| prefix matching | Hilde meets Hildegard (658-680, 1213-1227) | standard forms |
| one participant | a birth's or a death's date (956-970) | the principal |

### 8. Fingerprints and aliases

**A mention fingerprint hashes what it hashes now** (#197; PR #361's
`fingerprints.py`): the artifact's SHA-1, the verbatim label in NFC, the
kind, the excerpt span and the occurrence. The label stays verbatim, so
run diffs stay stable. No record, note or role enters a fingerprint: a new
reading of a label moves a mention between clusters, which the diff (#198)
shows as a merge or a split, and never renames it. The canonical label
does not change either: it stays the fullest clean form the document gives
(ADR-0007, ADR-0022).

**The alias list and its cleanup derive from the parts**, in place of the
token shapes of `pipeline/names.py:36-77`. For a person, an alias that
gives no part is no name and is dropped (a kinship term or a role that
slipped through); an alias whose parts the label already has is derivable
and dropped ("Frau Löbig" and "Löbig, Ida" beside "Ida Löbig"); and an
alias that adds a part is kept ("Wastl"). For a place or an organization,
an alias that gives no part (a deictic or generic reference, #42) is
dropped, while a short form is kept, since a place is looked up by its
toponym.

### 9. What the same prompt revision takes in

- **#42**: a place alias is a toponym and an organization alias a name; a
  deictic or generic reference is neither (§2.2, §8).
- **#4**: an office-holder is a `human`, whose label is a `role` where the
  source names only the office; a religion is a `religion` claim, never an
  `occupation`.
- **#44, first part**: the critic's stable rules
  (`pipeline/critique.py:58-94`: a kind that does not fit, a role or
  relational reference taken for an alias, the escape hatch misused) are
  stated where names and kinds are made, and most become structural
  checks (§2.1's status, §3.4, §8). The critic narrows once scratch run 1
  shows what still recurs; the second part stays deferred (#171).
- **#91**: type hints in English.
- **#89**: ADR-0031's rule, in the prompt and the critic.
- **Judgement notes** (§4).

Taking #4 and #42 in before run 1 turns their "measure first" acceptance
into "fix first, and measure in run 1": under the pre-baseline exception
(docs/AGENT_PROTOCOL.md §2), run 1's report stands in for their diffs.

### 10. What remains

**Still in place before write, named.**

- The organization rule's word count (§7, rule 6): a count, not a list,
  guarding firms named after a surname.
- GEDCOM's own grammar and vocabulary: slashes, comma-separated pieces,
  NAME TYPE values (`pipeline/gedcom.py:631-644`), PLAC's order of
  jurisdictions, and HEAD.LANG's language names. An initial or a
  placeholder in an export keys as written, and meets only the same form.
- GEDCOM dates: month names beyond the standard's abbreviations come from
  an English and German table (`pipeline/gedcom.py:168-179`); a date in
  any other language gives no claim and a warning, as now.
- #361's date thresholds (`circa-years`, `max-corroborating-years`):
  numbers, not wording.
- The transcription check knows failure text by English phrases
  (`pipeline/transcription_check.py:374-528`), so a refusal worded
  otherwise passes, and the stream assembly keeps an engine's narration
  ahead of its answer (`pipeline/llm.py:2475-2500`).
- Language detection reads a transcription with its editorial marks
  (`pipeline/transcribe.py:212-214`, `pipeline/language.py:109-128`),
  which can mislead it on a short text, and the language anomaly checks
  read its result (`pipeline/anomalies.py:424-489`, 649-666).
- The critic's axis is read from its target's grammar
  (`pipeline/critique.py:26-41`, `pipeline/orchestrate.py:1555-1579`,
  `pipeline/report.py:942-950`): a grammar the prompt sets, not a
  language.
- The writer labels items in `en` (`pipeline/writer.py:553-559`) and finds
  them by exact label (`pipeline/writer.py:530-548`,
  `pipeline/resolve.py:18-21`) until #109.
- The single-call extraction path (`cli.py:373-395`,
  `pipeline/extract.py:49-102`) makes no manifest, so its batches carry no
  records. Scratch runs never take it.
- Operator-side words that read no source: the review sheet's decision
  words (`pipeline/review.py:101-102`), the monitor's command grammar
  (`pipeline/monitor.py:183-218`), the model-alias check
  (`pipeline/rundir.py:369-385`) and the site's skip of a "living person"
  label (`staticsite.py:541`).

**Readers after write, moved in a follow-up issue.** Scratch run 1 writes
nothing, so these wait:

- reconcile's name tokens and prefix overlap
  (`pipeline/reconcile.py:256-273`), in the per-document reconcile
  (491-499) and the ADR-0019 sequencer (957-962);
- the place hierarchy's comma parents (`pipeline/places.py:184-235`, and
  `comma_depth` at 58-60);
- the impossibility filter's container-label check
  (`pipeline/reconcile.py:452-470`), which clustering calls with whole
  labels meanwhile;
- the disagreement report's name comparison (`disagreement.py:469-480`);
- `kinship.rq`'s term table (§3.3);
- the possibly-living readers' death events (`backup.py:205-220`,
  `staticsite.py:83-102`), in #345.

Records are not in the graph, so the follow-up also settles where each
reader finds structure: in the claims this ADR adds, in the batch views
and identity map of the run that wrote the graph, or nowhere, comparing
whole names as now.

## Consequences

- **Cost.** A prompt change recomputes every document's extraction: the
  LLM cache keys each call on what it sends (ADR-0032), and every chunk
  call forks the manifest turn. Scratch run 1 computes extraction afresh
  anyway (#145), so landing this revision before it starts costs nothing
  extra, and taking in #4, #42, #44, #89 and #91 with it makes one
  recompute where separate changes after run 1 would each make one.
  Landed after run 1 starts, it costs a second whole extraction. The
  GEDCOM adapter and clustering make no model call. The manifest turn's
  output grows by its records, by an estimated half or more per entity,
  and every chunk call reads it; run 1's call events measure it (#98).
- **ADR-0027 (#66) and #197.** The canonical label and the fingerprint
  stay as they are. The design list PR #361 gives #66 (its items 4 and 5,
  names and blocking keys) is restated from records, and ADR-0027 cites
  this ADR for how keys are read.
- **#106 (adjudication)** sees records, roles and classes rather than raw
  labels, in any language, and can weigh a married surname against a
  birth surname as ADR-0011 advises. Fewer spurious candidates reach it,
  since keys no longer meet on a shared token. For the same reason, it no
  longer sees pairs that share only part of a name ("Bad Flörburg" and
  "Flörburg", "Blauen Hut" and "zum Blauen Hut"), which #361 proposed
  as ambiguous; a candidate rule for them is #106's to add, if the runs
  show such pairs missed.
- **#107 (overrides)** keeps its key. Overrides key on fingerprints, which
  a new reading does not change, and a changed record re-queues only the
  clusters whose members move.
- **#345** gets the principal role it asks for.
- **Risks.**
  - *The model reads one name differently in two documents.* A misread
    part mostly loses a key, which splits rather than merges. Standard
    forms and the umlaut key absorb common variants, and the no-addition
    check bounds what a misreading can do. The run report counts records
    by status, `unsplit` forms, set-aside structures and notes, and the
    diff shows the clusters that move, so the prompt is fixed where the
    counts point.
  - *Over-structuring.* Every field is optional and its values closed; the
    escape hatches (`unsplit`, the statuses, the `relative` class) say
    "not settled" instead of forcing a reading; and no field is added that
    no stage reads.
  - *Output size.* A dense document's manifest turn grows. A manifest the
    turn cannot hold falls back as now (ADR-0007), and its mentions are
    recordless.
  - *Omission goes unseen.* Validation catches additions, not missing
    parts; the alias rule (§8) counts the forms that give nothing.
  - *Notes may be over-used*; their counts show it.
  - *#361's tests and the reviewers' probes* must be restated with
    records. #385 requires every probe from PR #361 and from the sweep to
    still give no false merge.
  - *Recordless mentions stay apart.* A document whose manifest falls back
    to line ranges merges nothing by name, and the report counts it.
- **Docs that move with the implementation:** docs/MODELING.md (Names,
  Places, Organizations, Kinship, Identity), dated pointer lines in
  ADR-0007, ADR-0011, ADR-0014, ADR-0018 and ADR-0022, a dated line in
  ADR-0013's §A for the type hint in English, the pipeline README's
  clustering section, docs/RUN_REPORT.md (the counts of records and
  notes) and docs/EXAMPLES.md.
- **Tests** (#385): fictional fixtures in languages beyond English and
  German, once a separate issue has extended the universe with them; the
  probes; mutation evidence; `stage:extraction` and `stage:clustering`
  under the pre-baseline exception.

## Alternatives considered

- **Extend the lists to more languages.** Rejected: open-ended, and still
  a reading of wording.
- **A deterministic name and address parser.** Rejected: the same
  heuristics, packaged. ADR-0022 §3 already rejected a hand-coded marker
  lexicon.
- **A record per name form**, each alias parsed apart. Rejected: several
  times the manifest's output for the same keys.
- **Write the structure as claims**, as Wikidata's given-name and
  family-name items. Rejected: a reading is not an assertion (§2.6), as
  ADR-0011 and ADR-0022 held.
- **A separate call for the structure.** Rejected: a call per document
  more, while the manifest turn already holds the forms it would read.
- **Fall back to parsing when a record is missing.** Rejected: it would
  keep the lists alive.
- **A model-given standard spelling for every part.** Weighed against the
  umlaut rule in §7, and rejected there.

## Open questions for the owner

Each has a default, which stands if the owner accepts the ADR without
answering it.

1. **Sequencing with #361.** Merge PR #361 as reviewed and let #385's
   implementation replace its name reading, or rework #361 first?
   Default: merge it, then replace its name reading before run 1. Its
   closure and classification stand, its lists never run in a scratch run,
   and reworking it first would reopen its four review rounds.
2. **`civil_status` as a new property.** The manifest has nothing that
   holds it, and the vocabulary round checks Wikidata for a property to
   mirror. Default: add it. The alternative, a gap line per marker, blocks
   nothing.
3. **Records for vessels and military units.** Default: none, until a
   scratch run shows missed pairs.
4. **The critic and records.** Should the critic see records and flag a
   misreading? Default: not in this revision; #44's measurement after run
   1 decides.
5. **HEAD.LANG absent.** Default: undetermined (`und`), if the instance
   accepts it.
