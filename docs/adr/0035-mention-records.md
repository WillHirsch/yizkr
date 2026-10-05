# ADR-0035: Extraction hands over structure — mention records, participant roles and kinship classes

Status: **accepted** (owner decision 2026-09-29, #385). Like ADR-0031
and ADR-0036, the owner's merge of the PR that adds it records its
acceptance; #385's implementation carries it out.

Amended by #386 (2026-09-29): the critic's findings are validated records
that name their entity and claim by id and give their axis and a
category (ADR-0023 §3), so the sweep's item on the critic's target
grammar is resolved; and the review sheet's `critique_notes` puts each
finding on exactly one row (its claim's, else the first row naming its
entity), not on every row of its subject.

Amended by #407 (2026-09-29): the extraction prompts ask for records,
roles, kinship classes, markers as claims, civil status, unions and
judgement notes (§5). A claim names its own document as subject by an
`artifact` reference, the source's own content key; validation takes one
only on a property the manifest marks `document_subject` (`issued_by`,
§3.4), and only naming the claim's own source, and refuses a claim of
such a property about anything else, as a counted reject. The writer
checks the same rule itself, since `yizkr write` takes a batch without
validating it, refusing and counting a claim that breaks it, and
resolves a document subject to the artifact item as it resolves the
source. A kinship term other than a cousin's with no "great-" has degree
0 (§3.3). A note's categories are written `kind`, `relation`,
`date_reading`, `identity` and `other`, each its manifest item's key
less `judgement_`. An entity's record or note that fails validation is
set aside and counted in `coverage.json` (`records`, `notes`), and a
claim whose note fails is a counted reject.

Amended by #412 (2026-10-02): the non-privileged export is written as
`nonprivileged.nt.gz` (ADR-0020); §4's `public.nt.gz` names it as it
was then.

Amended by #420 (2026-09-29): the run report sums each extraction's
`coverage.json` records (given, by status, the `unsplit` forms they
hold, recordless, the records set aside, and the reasons records were
set aside by, by category: a record that parses, of its entity's kind,
gives one for each problem found in it, so these count reasons, not
records) and notes (by category, and those set aside, each counted there
as one `note` reason), and counts apart, never as zero, an extraction
whose coverage holds none: one that fell back to line ranges, and one
whose coverage predates these counts (or holds only one half of them).
`coverage.json`'s `records` now also carries `set_aside_records`, the
records set aside, once each, and `unsplit_forms`, the `unsplit` forms
in the records given (§5); a coverage written before them still counts
its other figures, and the report counts apart, never as zero, the
extractions that lack each. The notes written are the writer's count,
and wait for #410, which writes judgement notes to the wiki.

Amended by #389 (2026-09-29): the transcription check, the stream
assembly and language detection no longer read wording (§10): the check
reads the transcription record (ADR-0037), and no wording fails a
record, so its English signatures only flag one; the answer is the text
after the last tool result; and detection reads a text outside its
editorial marks.

Amended by #370 (2026-10-04): alias hygiene (§8) compares an alias with
the label and with the aliases before it in the form the instance keeps
a term in (the writer's `stored_term`: each run of control characters
and line or paragraph separators made one space, trimmed, cleaned up as
Wikibase cleans a term up, then NFC), so an alias that differs from the
label only in that form, such as NFD against NFC or a line break
against a space, is dropped as the label is,
and two forms of one alias are kept once, the first as given. An alias
the instance would keep nothing of, such as control characters alone,
is blank and dropped.

Amended by #477 (2026-10-05): the other comparisons of names before the
write take both sides in that same form (`stored_term`). Every key §7
makes, of a label, an alias or a record's part, reads the part's words
in that form before folding (`part_keys`), and reconcile's blocking
keys through it, a manifest's names and a graph item's alike. Folding
already took NFD and NFC alike; what the stored form changes is a
control character, made one space, the edge characters it trims (such
as U+180E), and U+FFFE and U+FFFF, made U+FFFD: "Cäcilie\x08Löbig"
now meets "Cäcilie Löbig". Clustering leaves out of a mention's
aliases each one the instance would keep as its label, and, for a
batch without a manifest, takes each label its refs give once by that
form, the first as given. The identity map's code key (`CODE`) holds
writer.py, which gives the form.

Amended by #434 (2026-10-02): a record nickname is a name the source
gives as what the person is called, by anyone, and so also an alias; a
name the source gives as what one person calls another, or that names
the person through a relation but is no literal kinship term, is
ADR-0018's `referred_to_as`, never an alias and never a record nickname
(§2.1). A nickname that is a form of a given name is also that given
name. The `courtesy` status is a courtesy title or other relational
appellation that is no literal relation.

Amended by #408 (2026-09-29): the GEDCOM adapter fills §6 as follows.
The label of a NAME leaves out its bracketed parts, each of which is an
alias and an `unsplit` form; a NAME whose value gives a name only in
brackets that hold a word, with no part that gives a name, is labelled
by the brackets as written ("(Peppi)"), so that its person keeps an
entity and its claims. A NAME
without slashes, whose parts no GIVN or SURN gives, gives no typed-name
claim, whatever its TYPE; one with more than one pair of slashes is
read so too, counted apart (`NAME slashes not one pair`). Given names
are the words of the part before the slashes and of each GIVN piece; a
NICK, less one pair of quotes that encloses it, and a `_MARNM` are
aliases, so the record adds no name (§5); the alias hygiene keeps a
form that alone carries a part of the record. An NPFX or NSFX that the
value does not restate reaches its field beside the value's name, as
the table says, and the full form is an alias, counted as `NAME parts
not restated`; only in a NAME with no value, whose parts give no given
name or surname, is a prefix or a suffix counted instead (`NAME part
without a name`). A particle that the value's surname holds is no
particle without a surname. A `_MARNM` gives a `married` surname and a
`married_name` claim only where its slashes hold a surname; otherwise it
is `unsplit`, counted, with no typed-name claim, as a NAME without
slashes gives none. HEAD.LANG maps GEDCOM 5.5.1's language names to BCP 47 where the
instance is known to take the code; an absent or unmapped one gives
`und`. The live
GEDCOM tests check that the instance takes `und` and every mapped code
for a monolingual text. What §6 counts is counted in the adapter's
outcomes, beside its census of unmapped tags, and reaches the run's
document state and log.

Amended by #408 (2026-09-29; decided by the orchestrator, the owner may
override): a NAME without slashes that has only a GIVN or only a SURN
keeps its whole value as an `unsplit` form beside that part, counted
(`NAME without slashes, one part given`), so that no word the part does
not place is dropped silently. The table's `unsplit` row is the NAME
with neither. A HEAD.LANG that is already one of the map's codes ("de")
is taken as it is. The export's `NICK Gustl` is a nickname and
an alias in Gustav's record, which clustering (#409) meets the postcard's
"Gustl" on.

Amended by #408 (2026-10-02; decided by the orchestrator, the owner may
override). The rule behind each: never drop a person who has a word, and
never narrow or guess.

- **A placeholder names nothing.** A word that holds no letter or digit
  ("?", "-", "…", "_", "()", "(?)") is a placeholder for an unknown name
  or part. Wherever it stands, in a NAME's value, a bracket, a quote, a
  GIVN, SURN, NPFX, NSFX or SPFX piece, a NICK or a `_MARNM`, it is left
  out and counted (`NAME part without a word`), never placed: "Eugen
  /?/" is labelled "Eugen" with no surname, `GIVN Eugen ?` gives the
  given name Eugen, and a record left with no given name, surname,
  nickname or unsplit form is none. A bracket or a quote keeps its marks
  around the words left in it ("(Peppi ?)" is "(Peppi)", "'? Muckel'" is
  "'Muckel'"), and one with none left goes whole. Leaving a placeholder
  out can give two people one label ("Eugen ? /Löbig/" and "Eugen
  /Löbig/" are both "Eugen Löbig"), as exact namesakes already share one.
- **A typed name with a placeholder gives no typed claim** (decision K).
  A NAME with a TYPE that held a placeholder anywhere, in its value
  (inside its brackets too: "Eugen (?) /Löbig/", "Eugen /(?)/"), its
  quotes, or its GIVN, SURN, NPFX, NSFX or SPFX ("GIVN Agnes (?)"), a
  stray bracket ("Eugen) /Löbig/") or an empty bracket nested in another
  ("(Peppi ())") included, gives no typed-name claim, counted (`typed NAME with a placeholder`):
  a tree writes "(?)" to say it is unsure of the name, and the claim
  would narrow it (rule 5). A NICK's placeholder does not block it, as a
  NICK is no part of the typed name. A `_MARNM` whose slashes still hold
  a surname but that held a placeholder gives none either. Their forms
  stay aliases.
- **The label falls back, tier by tier, to any word.** A person's label
  is the first name form a NAME gives. Where none gives one, it is the
  first of: a NAME value's brackets that hold a word ("(Peppi)", so that
  `1 NAME (Peppi)` then `1 NAME Ida /Sörne/` is labelled "Ida Sörne",
  with "Peppi" an alias); a GIVN's quoted or bracketed part; a NICK; a
  `_MARNM`. Each such label keeps the source's marks as written (`GIVN
  (Peppi)` gives "(Peppi)", `GIVN 'Peppi'` "'Peppi'", `NICK 'Muckel'`
  "'Muckel'"), so that, where the source wrote any marks, a person known
  only so meets no one whose name is the bare word (decision L); an
  unmarked `NICK Muckel` still gives the label "Muckel". A person with any word in
  any name keeps an entity and its claims. A person none of these gives
  a word is skipped with the adapter's warning, as one whose NAME is
  empty is, and counted (`person without a name`): the writer finds an
  item by its label and would make every person labelled "?" one item.
  A prefix, a suffix or a particle alone gives no label, since "Wwe."
  would make everyone it stood for one item too.
- **An NPFX is a title, as the export tags it.** An NPFX the value
  restates is a title, whatever the word: `Dr. /Löbig/` + NPFX Dr. gives
  the title Dr. and no given name, and so does `Eugen /Löbig/` + NPFX
  Eugen, counted (`NPFX restated as the whole given part`); telling a
  mis-tagged given name from a title would need a word list (§10). An
  NPFX the value states in part ("Dr. Eugen" + NPFX "Dr. med.", or
  "Eugen" + NPFX "Dr. Eugen") gives the form "Dr. med. Eugen Löbig" or
  "Dr. Eugen Löbig", writing no word twice, and a prefix is matched by
  its words folded ("Dr med" is "Dr. med.").
- **Quotes and brackets.** A quoted word after the surname ("Eugen
  /Löbig/ 'Muckel'") is a nickname, and each run of words between such
  quotes is a suffix of its own. A slashless NAME with a quoted word
  between its words ("Eugen 'Muckel' Löbig") keeps the form without it
  ("Eugen Löbig") as an alias beside its unsplit form. A `_MARNM`'s
  unsplit form leaves its brackets out ("(Peppi)" gives "Peppi"). A
  GIVN's quotes and brackets are read before the GIVN is split at its
  commas, and as the value's are: a quoted part ("Ida 'Peppi'", "'Ida,
  Peppi'") is one nickname beside the given name, and a bracketed part
  ("Ida (Peppi)") an alias and an unsplit form, counted (`NAME bracketed
  part`). This corrects re-review 1's decision 5, which made a GIVN's
  bracket a nickname, to follow the owner's bracket rule (§6, 2026-09-29:
  a bracket may hold a maiden name, a later name or a note as well as a
  nickname). Either still gives the label of a person no other name
  gives one, as the tier above says; a GIVN's quoted part also gives a
  nickname claim cited to its NAME, as a NICK does, and its bracketed
  part none. A bracket within a bracket is read by the outermost pair
  ("(Pep(pi))" gives "Peppi"); a '(' never closed or a ')' never opened
  is left out, counted. A quote whose only content is a bracket ("Ida
  '(Peppi)'") is the bracket, its marks no placeholder; such a quote
  opens and closes a word as any quote does, or stands against a slash
  or a GIVN's comma ("Ida '(Peppi)', Agnes"), so a bracket between two
  quotes ("Eugen 'Muckel' (Peppi) 'Cilli' /Löbig/") leaves both
  nicknames, Muckel and Cilli. Any quote may stand against a slash
  ("Eugen 'Muckel'/Löbig/"). German low quotes ("„Peppi“", "‚Peppi‘")
  and guillemets ("»Peppi«", "«Peppi»") mark a nickname as the others
  do, and a NICK loses any opening and closing mark around it, matched
  or not ("„Peppi”"), as a quote in a value does. A placeholder in a
  nested bracket ("(Peppi (?))") is counted once. A malformed nest ("Ida (Pep(pi) /Sörne/") is read by the pair that
  closes ("(pi)" an alias, "Pep" a given name) and its stray '(' counted:
  the adapter does not guess which bracket the tree meant. A bracket name
  keeps an NPFX or NSFX beside it as a title or suffix, its full form an
  alias ("Frau Peppi").
- **Connectors** (decisions M, W, Y). A single dash (of any width) or
  "&" between two words of one field is a connector, kept as written
  ("Ida /Löbig - Sörne/" keeps the surname "Löbig - Sörne"). Its
  neighbours are read within its own field, before the slashes become
  spaces: at a field's edge, against a slash, a bracket or a quote
  ("Agnes - /Löbig/", "Agnes /- Löbig/", "Ida - (Peppi) /Sörne/",
  `_MARNM Agnes - /Löbig/`), or standing as a whole part or value
  ("/-/"), it is a placeholder, dropped, counted and blocking a typed
  claim (K) (decision Y). "+" and runs such as "--" are never
  connectors (decision W): a word made only of them is a placeholder,
  since in German trees "+" often marks a death, which is no part of a
  name.
- **Particles** (decision X). As many SPFX slots (comma-separated, an
  empty one or one holding only a placeholder included) as SURN slots
  pair by position ("von, de" + "Löbig, Sörne" gives "von Löbig" and
  "de Sörne"; "von," gives "von Löbig" and "Sörne"; "?, de" gives
  "Löbig" and "de Sörne"), an empty slot giving no particle. Otherwise the SPFX pieces together go on each
  SURN piece ("von" + "Löbig, Sörne" gives "von Löbig" and "von
  Sörne"), and the mismatch is counted (`SPFX particles not matching
  SURN pieces`).
- **A prefix that holds the whole name.** An NPFX that writes the name
  the value gives anywhere within it ("Dr. Eugen Löbig Jr." beside
  "Eugen /Löbig/") is that form whole, the name not written twice.
- **Language.** A HEAD.LANG of `und` is taken as it is, uncounted.

Amended by #388 (2026-10-04; the typed parts, whole birth names, chain
links and known limits below decided by the orchestrator 2026-10-05,
the owner may override): this settles where the first four
readers after write that §10 lists find structure.

- **Reconcile's blocking and the ADR-0019 sequencer** key on whole name
  forms, folded as §7 folds a part (`part_keys`), never on name tokens or
  prefixes. A manifest entity keys through its record:
  - a person only with the status `name`, by its label, aliases,
    unsplit forms and nicknames, and by the whole forms its typed parts
    make: each given name, as written and as its standard form, and
    each nickname, before each surname and patronymic, a pair whose two
    parts fold alike making none. The postcard's "Gustl", read as
    Gustav, keys "Gustav Löbig"; "Agnes Löbig geb. Nertler" keys "Agnes
    Löbig" and "Agnes Nertler", whatever marker her label holds;
  - a place only as a `toponym`, by its record's own names and address,
    less a short form that is another name's toponym, so the camp never
    meets its town;
  - any other entity, or one without a record, by its label and aliases.

  A graph item has no record, so it keys by its label, aliases and the
  values of its typed name statements (`birth_name`, `nickname`,
  `official_name`), each whole; a `married_name` is a family name alone
  and keys nothing. The roster maps an item's kind to its manifest key
  by the kind item's QID.
- **The place hierarchy's parents** come from the chains of containers
  a place's records state (`within`, §2.2), read from the batch views of
  the run that wrote the graph and joined to the written item by the QID
  reconcile resolved, else the item the writer used for its local id,
  found under its label or minted (`write.json`'s `created_items`). A
  chain is asked link by link: the place within the nearest
  container that names an existing place, and each such container
  within the next, every place of that name each a question for a human
  (ADR-0009, as amended).
- **The impossibility filter** reads containment by QID only, and in
  clustering by the mention id that stands in for one; a name shared
  with a container never prunes a pair (ADR-0017, as amended).
- **The disagreement report** keeps comparing whole names, as §10
  allows: a birth name is the graph's own typed name statement, kept in
  the source's wording, and "Sörne" beside "Ida Sörne" are two values,
  listed with their sources (ADR-0002).
- The static site skips `instance of` by its property id, not its
  English label, and no longer skips a "living person" label, a
  property the manifest does not hold (§10's operator-side list).

Known limits, to revisit with the readers' next round: a place
whose record gives only an address ("8 Hawthorn Road, Stupfield,
Victoria") keys only on the address, so its town never links it; and an
event, which has no record (§2.5), keys on its label as a whole string,
not on its participants; and a derivable alias the cleanup drops (§8,
ADR-0022 §4) is composed again from the record's typed parts only for
its own batch's blocking, so a later document giving only that form
("Ida Löbig" against an item labelled "Ida Sörne Löbig") does not block
against the written item.

`kinship.rq`'s term table and the possibly-living readers stay with
their own issues.

Amended by #409 (2026-09-29): clustering keys on the records as §7 says,
and settles what §7 leaves open. A person's kin agree only on a key a
record gives (never on a recordless kin's name), and the couple of a
union count as each other's spouses. A lone given name, and a key only a
GEDCOM `unsplit` form gives, pair but never make a link confident; a
document's own `unsplit` form keys like its typed parts. Two places that
share a name with containers neither equal nor nested stay ambiguous;
their containers are a record's `within` and the places the manifest's
`part_of` and `location` claims put them in. Rule 6's places stand as
written: a place an organisation's `location` claims name agrees with the
other's when the two would link under rule 5 (`exact` or `nested`), or
when one would link so with a place the other lies in by the `part_of`
and `location` claims of its manifest. A place without a record, which
rule 5 cannot link, is compared by its exact names, which leave the
places unknown when shared and differ otherwise; it never makes them
agree. Containment that only a record's `within` states does not count
here: a civil registry at "Fläckfeld, Kreis Mockburg" and one located at
Kreis Mockburg stay apart, the safe direction. Places a record names
carry no containers, and agree with the other's places only as rule 5
would let them (below), counting only places with records. Places differ
when both sides state places and none of this holds. An organisation
without a proper name keys and links on kind and place only against
another without one. §5's verdict on a record is extraction's, taken on
the alias bag before the cleanup of §8 and never again after it;
clustering reads any record of the entity's kind. A birth or death with
no principal, or several, or one whose own date of that kind conflicts
with it, is nobody's own and meets nothing through its other
participants (§3.2; rule 9). The alias cleanup (§8) leaves an entity
without a standing record its aliases, blanks and duplicates aside, and
keeps a person's first alias that alone holds a title or suffix the
record gives, preferring one that also gives a name part, so that the
record names no part its forms do not hold ("Frau Löbig" beside "Ida
Löbig" stays where her record titles her Frau, decision 11 below). A
spelling variant is never derivable: an alias that spells a part
otherwise than the label does stays ("Gustav Lobig" beside "Gustav
Löbig", "Hans Xxx Löbig" beside "Hans-Xxx Löbig"), while a reorder or a
case-only variant goes. The GEDCOM adapter cleans by the record it fills, as extraction
does, so an alias that restates its bracketed label ("Peppi" beside
"(Peppi)") is derivable and goes. A mention without a record meets only
mentions without one, so a document's mention whose record was set aside
never meets the export, whose people and places all have records. The
run report counts recordless mentions (`summary.recordless_mentions`),
and #420 counts the records and the judgement notes of §4 in it.

Amended by #409 (2026-09-29), decided by the orchestrator (the owner may
override; conservative, since a missed merge is safe and a false
confident link is not):

- Rule 6 on places stays as written above for places that location
  claims name: a place name the two share is not enough for their places
  to agree.
- Two organisations named only by a kind, or by a kind and a proper name
  of one word (such as "Firma Löbig"), never link confidently where
  either states no place: a proper name of one word links only on places
  that agree, a shared kind notwithstanding, since no rule can tell a
  surname from another one-word name. Containment of stated places still
  agrees: a nameless Landgericht at Rekau, which its manifest puts in
  Hesse, links with a nameless Landgericht at Hesse.
- Rule 1: a given part that folds like any of the person's own surnames
  or a kin's pairs with no kin's surname; a kin without a record lends
  the words of its label to that test, and to nothing else. Two
  registers that misread the sisters "Löbig Ursula" and "Löbig Klara",
  daughters of Hildegard Laudler, with "Löbig" as a given name, never key
  (Löbig, Laudler), whether their father is named with a record, without
  one, or not at all. A child named after a parent without a record (a
  "Sebastian" whose father is a "Sebastian Löbig" without one) pairs
  with no kin's surname either, so two such registers may never pair:
  the safe direction.
- Rule 3: a given name also keys by its written form, not only its
  standard form, so a "Gustl" whose record gives the standard form
  Gustav meets a "Gustl" whose record gives none. Meeting is not
  merging: the pair is still judged.
- A known limit, to revisit after scratch run 1 (#145): a record that
  gives both "Bad Flörburg" (with the head "Bad") and "Flörburg" drops
  "Flörburg" as a short form, so it never meets a plain Flörburg. That
  keeps two mentions apart, which is safe.

Amended by #409 (2026-10-02), decided by the orchestrator (the owner may
override; conservative as above):

- Rules 3 and 8: a shared key neither makes a link confident nor makes
  kin agree (it is `weak`) when it joins two given parts that cannot be
  one name. A given part is a given name or a word of a document's
  `unsplit` form, which states no standard form.
  - Two given parts whose records give standard forms that share no
    key: a "Gustl" read as Gustav and a "Gustl" read as another name
    meet, and stay ambiguous.
  - Two nicknames, where both records give given names or unsplit
    words that share no key: two brothers with two given names and one
    family nickname never merge, nor do they where one brother's name is
    an unsplit form ("Xxx Löbig", or an export's NAME without slashes).
  - A given part against a nickname, only where the given part states a
    standard form that shares no key with the nickname holder's given
    names. So "Gustav 'Gustl' Löbig" against a "Gustl Löbig" whose
    record gives no standard form, or Gustav, merges on corroboration
    (a shared father), and against a "Gustl" read as Xxx stays
    ambiguous. A nickname holder with no given names bars nothing: a
    "Wastl" read as Sebastian against a nickname-only "Wastl Löbig"
    keeps the key. A word of an `unsplit` form states no standard form,
    so "Wastl Löbig" written unsplit against "Sebastian 'Wastl' Löbig"
    keeps it too. (A one-word `unsplit` form makes no key with a
    surname; it meets only as a lone name, rule 1.)

  A "Gustl" read as Gustav and a "Gustl" with no standard form still
  meet, and merge only on corroboration. An export's given name is its
  own standard form (§6), so an export's "Gustl /Löbig/" never merges
  confidently with a document's "Gustl" read as Gustav: the safe
  direction.
- Rule 6, places a record names: they agree with the other's only as
  rule 5 would let them. Each side is one list of parts: a record's
  places, or the places the other's location claims reach and every
  place those lie in, a place without a record counting by its exact
  names. Where one list lies among the other, the places agree if a part
  with a record is shared, and read `unknown` if only a part without one
  is. Otherwise they differ: a shared name whose other stated places
  differ ("Fläckfeld, Kreis Mockburg" against "Fläckfeld, Kreis
  Peiltal"), whether or not the located side's district has a record,
  and lists naming places at different levels (`["Flörburg", "Hesse"]`
  against `["Kreis Peiltal", "Hesse"]`).
- Rule 6, this ADR's own clause "on a proper name of one word only with
  a shared kind or place": the decision of 2026-09-29 above narrows it to
  "only with a place that agrees". It is recorded here as an in-place
  amendment for the owner to confirm at merge, or to move to a new ADR
  (AGENT_PROTOCOL §8).
- Rule 5 compares the containers that `part_of` and `location` claims
  give a place, through the places its manifest puts it in, as well as
  its record's `within`. Two Fläckfelds that such claims put in Kreis
  Mockburg and in Kreis Peiltal stay ambiguous, and so do nameless
  organisations at each, whether or not the districts have records; a
  claimed container is one part, its own containers apart. Containers
  stated at different levels keep a pair apart too: a Flörburg that
  part_of puts in Kreis Peiltal and one located in Hesse stay
  ambiguous, the safe direction. A place whose record and claims give
  contradictory containers (Kreis Peiltal and Kreis Mockburg) nests in
  both namesakes; its component stays ambiguous, which is safe.

Amended by #409 (2026-10-02), decided by the orchestrator (the owner may
override): **decision 11, §8's "Frau Löbig" example narrowed.** §8 drops
"Frau Löbig" beside "Ida Löbig" as derivable. Where her record titles her
Frau and no other form holds the title, it now stays, so that the record
names no part its forms do not hold; where the record gives no title, or
the label or another kept alias holds it, it still goes. Of several
holders, one that also gives a name part is kept before a role phrase
("Frau Mutter") or a bare title. This is an in-place narrowing of §8's
example, for the owner to confirm at merge, as decision 9 is.

Amended by #435 (2026-10-02, the owner's decision on the bracket
question): §6's bracketed part, a NAME with no TYPE and one with a TYPE
the adapter does not recognise each also give a `name` claim of no type
(ADR-0011, "Name forms of no stated type"), cited to its NAME, unless it
is the label's own form, a form the person's record types, or from a
NAME that held a placeholder (counted, `untyped name form with a
placeholder`). The extraction prompt offers `name` for a name form whose
type the source does not mark, other than the label's.
Amended by #435, orchestrator decisions BN to BQ (2026-10-04; the owner
may override at merge): a form a recognised TYPE or a `_MARNM` gives is
typed whether or not a typed claim is made of it; the label and the
typed forms are compared in NFC, whitespace collapsed, lower-cased (a
sharp s kept apart from "ss"; decision CD); a placeholder anywhere in a
NAME (its value, a bracket, a quote or a part) blocks its display form,
one in a bracket blocks that bracket too, and a complete bracket of
such a NAME still gives its name, so "Ida (Peppi) /?/" gives `name`
"Peppi" (decision CE keeps this, as decided on 2026-10-02); a bracket
holding a cue or a note ("(geb. Laudler)") gives its text verbatim,
never read as a type; and in a document only a distinct name of open
kind is a `name`. Decision BS, that a lettered stand-in ("N.N.",
"[unknown]") in an export gives no `name`, is withdrawn: such a stand-in
holds letters, and no word list may decide it (§6 reads no word by its
meaning), so it stays a `name` statement. The extraction prompt, whose
model reads meaning, tells it a stand-in is never `name`.

**Decided by the owner (2026-09-29):**

- Before scratch run 1 (#145), extraction hands over the structure of
  what it reads, and code keys and judges on that structure instead of on
  fixed word lists (#385).
- The other readings of meaning from wording that a sweep of the code
  found are resolved before run 1 too (#385).
- An extracted fact may carry a judgement note, written to the wiki
  inside the extraction-run reference of the statement it concerns
  (#385).
- The relation a source states decides the kind of what it points to
  (ADR-0031, accepted; #202). This ADR carries its structural half (§3.4,
  §3.5).
- Marriages and partnerships are entities (ADR-0036, the owner's
  decision on #311; accepted when PR #398 merges). Their participants
  take the roles `spouse` and `partner` defined here.

**Amends:** ADR-0003 (an extraction-run reference may carry a note),
ADR-0007 (the manifest record gains a structure and a note), ADR-0011
(the GEDCOM adapter's brackets), ADR-0013 §A (type hints in English),
ADR-0014 and ADR-0018 (a kinship term gains a class), ADR-0020 (a civil
status is person evidence), and ADR-0022 (Rulings 1 and 2, §3 and §4:
the manifest may say which part of a name is which).

**Supersedes in part:** ADR-0023 §2 and Ruling 1 (§4 below gives the
dated line ADR-0023 gains).

**Replaces:** the name reading of the corpus clustering stage (#105,
merged with PR #361). Its closure, cannot-link and classification logic
stay.

Line numbers are those of main at 52e2a08, for paths under `src/yizkr/`.

## In brief

- **Extraction says what each part of a name is, and code stops
  guessing.** The registration card's "LÖBIG, Ida, née SÖRNE" arrives as
  the given name Ida, the surname Löbig and the birth surname Sörne.
  Clustering's lists of titles, kin words and generic place words go.
- **These readings are never written to the wiki.** They help decide who
  is who, but no source ever says "Ida is a given name".
- **What a source does say becomes a claim.** The burial register's
  "Witwer der Cäcilie Löbig" gives Eugen's civil status (widowed),
  Cäcilie's death, and their marriage.
- **Everyone in an event gets a role.** In Gustav's birth register
  extract, Gustav is the one born (the event's *principal*), and his
  father Sebastian is both a parent and the informant who reported the
  birth. A birth's date belongs to the person born alone, so a death that
  names only the person who reported it no longer dates that person's
  death.
- **A kinship term keeps its words and gains a class.** "Dad's cousin
  Leopold" gets the class cousin beside the word "cousin".
- **The relation decides the kind.** The Landgericht Rekau, which divorces
  Leopold and Agnes, is an organisation; the Peiltal court prison, where
  Leopold was held, is a place. A claim that points a relation at the
  wrong kind is refused and counted.
- **Given names match by their full form, never by their first letters.**
  "Gustl" meets "Gustav"; a man's name no longer meets the woman's name
  that adds a letter to it.
- **Some pairs are no longer proposed.** Names that share only a part
  ("Bad Flörburg" and "Flörburg") or are one name in two languages
  ("Gustav" and "Gustave") no longer pair on their own; a source that
  gives both still joins them. A bracket in a family-tree export no longer
  marks a nickname; a quote still does.
- **An umlaut also meets its two-letter spelling.** "Floerburg" meets
  "Flörburg" (ö written as oe), as "Lobig" already meets "Löbig".
- **The extractor may leave a note where it made a judgement call**, such
  as "the postmark '19.8.38' read as 1938". The note is written to the
  wiki as the extraction's own remark, beside the record of the run that
  made the claim, never as something the source says. It never ranks or
  hides a value, a claim without one is not thereby certain, and it never
  reaches the non-privileged export.
- **What cannot be structured is counted, never guessed.** Its mentions
  stay apart, and the run report says how many.

## Context

**Clustering reads structure back out of free text.** The clustering
stage (#105, merged with PR #361) needs to know which word of a label is a
surname, which is a title, which part of a place name is the place
itself, and whether an organisation's name is its own or only its kind.
Extraction never says, so the stage rebuilds it from each label with
fixed word lists (`pipeline/clustering.py:295-398`):

| List | Words | What it stands for |
|---|---|---|
| `TITLES` | 28 | honorifics |
| `KIN_WORDS` | 49 | kin and courtesy words |
| `PARTICLES` | 20 | name particles and generational words |
| `FORM_MARKERS` | 11 | "formerly", "later", "called" |
| `MAIDEN_MARKERS` | 4 | maiden-name markers |
| `OCCUPATIONS`, `OCCUPATION_ENDINGS` | 63 + 8 | occupations beside a name |
| `ORG_GENERIC` | 25 | words no organisation is known by |
| `ORG_INSTITUTIONAL` | 70 | words naming an institution's kind |
| `PLACE_GENERIC` | 25 | generic heads of place names |

The lists are almost entirely German and English, so they cover only the
languages someone wrote them for. A list is never complete. And they
partly redo what ADR-0011 (name typing) and ADR-0018 (relational
appellations) already ask extraction to do.

**The sweep.** The owner asked for a read-only sweep of other code that
reads meaning from wording. Probed with fictional names, it found the
same kind of reading well beyond the lists:

- given names and surnames read from word order, commas and brackets:
  "LÖBIG Ida" keys Ida as the surname (`pipeline/clustering.py:581-603`,
  618-655);
- given names matched by prefix, so a man's given name meets the woman's
  form that adds a letter to it, while "Gustl" never meets "Gustav"
  (658-680, 1213-1227);
- the GEDCOM adapter types any short bracketed part of a name as a
  nickname, "(geb. Laudler)" included (`pipeline/gedcom.py` before
  #408);
- place structure read from commas and token length, so a toponym of
  three letters or fewer never keys (`pipeline/clustering.py:1002-1022`,
  `pipeline/clustering.yaml:21`);
- accent folding that keeps "Flörburg" and "Floerburg" apart
  (`pipeline/clustering.py:522-527`);
- a label the model composes for someone unnamed ("Klara's father")
  keying like a name;
- alias cleanup by token shape (`pipeline/names.py:36-77`);
- markers that state a relation or a death (a widow's marker, "the late",
  patronymic and married-name markers) read as parts of names;
- a birth or death event with one participant taken as that person's own
  (`pipeline/clustering.py:956-970`), so a death that names only its
  informant dates the informant's death: a participant claim cannot say
  how someone took part. The possibly-living check shares the blind spot
  (#345).

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
not whether to read it, but who reads it, from what, and where it is
kept. The model can read the document's own conventions; a list reads
none.

## Decision

### 1. The principle

**Extraction hands over structure. Code keys and judges on that
structure, never on wording, word order or punctuation.**

- The model says what each part of a name is. For a GEDCOM export, its
  tags say it, with the one inline convention ADR-0011 adopted for names
  (§6). Code never decides it from a word list, a position, a comma, a
  bracket, capitals or a token's length.
- A word that states a fact rather than naming someone (a widow's marker,
  "the late", a kinship term, a role in an event) becomes a claim, with
  its source, never a part of a name (§3).
- A reading that no source asserts (which part is the surname, which word
  is a place's generic head, whether an organisation's name is its own)
  stays on the extraction side, as input to identity (§2.6).
- Code compares whole typed parts, folding only case and accents, plus one
  stated rule for umlaut spellings (§7).

### 2. Mention records

Each entity of the entity manifest (a *mention*, as clustering calls it)
of four kinds gains a `structure`: what its names are made of, read from
its label and aliases as the document writes them. A record holds only
what identity reads. A person's `status` defaults to `name`, a place's
to `toponym`, and a surname's type to `other`.

#### 2.1 A person (`human`)

| Field | What it holds | Keys? |
|---|---|---|
| `status` | what the label is: `name`, `role`, `kinship`, `courtesy`, `placeholder` or `description` | only a `name` keys |
| `given` | each given name as written, with its standard form, marked when it is an initial | yes, by standard form |
| `surnames` | each surname as written, particles kept in it, typed `birth`, `married` or `other` | yes |
| `patronymics` | each name formed from the father's given name, as written | yes, as a surname |
| `nicknames` | the informal names the person is called by, as written | yes, as given names |
| `titles` | honorifics and ranks | never |
| `suffixes` | generational and status words | never |
| `unsplit` | a name form whose given name and surname the source does not tell apart | yes, each ordered pair of its distinct words |

- **The source's conventions decide which part is which, never a rule
  about a language.** The card's "LÖBIG, Ida, née SÖRNE" gives the given
  name Ida and the surnames LÖBIG (`other`) and SÖRNE (`birth`): the card
  writes surnames first and in capitals, and the model reads that. A
  register, a directory, a form's labelled fields, a signature and
  running prose each have conventions of their own. Where the document
  does not settle the order, the form is `unsplit`.
- **A surname is `birth` or `married` only where the source marks it**
  (ADR-0011; ADR-0022 Ruling 2). "Cäcilie Löbig geb. Tarle" gives Tarle
  as `birth` and Löbig as `other`. A title never types a surname: "Mrs
  Cutmore" gives the title Mrs and the surname Cutmore, `other`.
- **Particles stay in their surname** (ADR-0022 §4), so a surname keys
  whole.
- **A standard form** is the full form of a given name in its usual
  spelling, which a diminutive or an abbreviation stands for, and it is
  given only as the document supports it. The 2008 family history's
  "became Gustave Lobig here, Gus to everyone" makes its "Gus" Gustave.
  The postcard's lone "Gustl" never spells the full form, so it gets
  none, and its written form stands in. A full name is its own standard
  form. A standard form is never a translation, and an initial has none.
  A name the source gives as what the person is called, by anyone, is a
  nickname. One that is a form of a given name ("Gustl", "Gus") is also
  that given name, with its standard form where the document supports
  it; one that stands for no given name ("Peppi", "Muckel") has no
  standard form. A name the source gives as what one person calls
  another, or that names the person through a relation but is no
  literal kinship term ("Bubele", "Nana Maisie", "Рудольфиха"), is no
  nickname: it is `referred_to_as` (ADR-0018), never an alias.
- **A patronymic** is a name part of its own, never a surname. The
  relation it states is a `father` claim where the source names the
  father (§3.1). (An example follows when #400 adds a patronymic to the
  universe.)
- **A nickname is a name; a title or a status word is not.** "Sebastian
  'Wastl'" gives the nickname Wastl. The directory's "Löbig, Ursula,
  Wwe." gives the suffix Wwe., which never keys; her widowhood is a claim
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
  - `kinship`: a literal kinship term (ADR-0018 (A));
  - `courtesy`: a courtesy title or other relational appellation that
    is no literal relation, such as "Nana Maisie" where a document names
    her no other way (ADR-0018 (B));
  - `placeholder`: the source's stand-in for a name it does not know,
    such as "N.N. Löbig";
  - `description`: a label the model composes for someone the document
    has no words for at all.
- **One rule for the label of someone unnamed.** The label is the
  document's own words: a name where it gives one, else its own reference.
  The postcard's writer signs "Dein Vater", so that is his label, with
  the status `kinship`. Only where a document has no words for a person
  at all does the model compose one, such as "Klara's father", with the
  status `description`. The label feeds the mention's fingerprint (§8), so
  one rule keeps it stable.
- **Sex is never read from a given name.** A source states it in words or
  in its grammar ("ein Knabe" in the birth register extract, "den Schüler
  Rudolf Löbig" in the school report), as a `sex_or_gender` claim.

#### 2.2 A place (`place`)

| Field | What it holds | Keys? |
|---|---|---|
| `status` | what the label is: `toponym`, `deictic` or `generic` | only a `toponym` keys |
| `names` | the place's own names as written, each with its toponym and generic head where they can be told apart | yes, each as written, with one exception below |
| `address` | a street and a house number, as written, in the source's order; the address is also one of the names | yes, as written, among the names |
| `within` | the places the source puts it in, as written, in the source's order | never; they decide nesting |

- "Fläckfeld, Kreis Mockburg" (the marriage certificate) gives the name
  Fläckfeld, within Kreis Mockburg.
- "Lager Klurnau" gives one name, with the head Lager and the toponym
  Klurnau. It keys as written, so the camp never meets the town Klurnau.
  "Stadtkreis Rekau" likewise never meets the town Rekau inside it.
- **A short form that is another name's toponym keys nothing.** Where the
  camp's aliases include a bare "Klurnau", the toponym of its name "Lager
  Klurnau", that alias stays for lookup but keys nothing, so the camp
  never meets the town through it. This is what the head and toponym
  split is read for, with ADR-0031's split of a site from its town.
- "8 Hawthorn Road, Stupfield, Victoria" (the naturalisation) gives the
  address 8 Hawthorn Road, street Hawthorn Road and number 8, within
  Stupfield and Victoria. The address keys as written, so it meets the
  GEDCOM export's "8 Hawthorn Road, Stupfield, Victoria, Australia".
- The compensation file's "in das Lager Klurnau verschleppt, dort befreit"
  refers back to the camp: "dort" is `deictic`, never an alias and never a
  place of its own (#42). A place named by a head alone ("Lager", with no
  toponym) is `generic`.
- A head never keys on its own. A container never keys at all: it only
  decides whether one place nests in another (§7).

#### 2.3 An organisation (`organization`)

| Field | What it holds | Keys? |
|---|---|---|
| `names` | its proper names, as written | yes |
| `kinds` | the words naming what kind of institution it is, as written | only with a place |
| `places` | the places its names carry, as written; or else those its `location` claims name, with every place its manifest puts them in (#361's `_places`, `pipeline/clustering.py:1609-1620`) | only with a kind |

- "Hutmacherei zum Blauen Hut" gives the name "zum Blauen Hut" and the
  kind Hutmacherei. "Photograph Brüsrich" gives the name Brüsrich and the
  kind Photograph.
- "Städtische Realschule zu Flörburg" and "Standesamt Flörburg" have no
  proper name: each gives a kind and the place Flörburg, and is known by
  the two together.
- Generic words that any organisation carries (a word for a firm or a
  company, a legal form) are neither names nor kinds: "Firma Löbig" gives
  the name Löbig alone.

**ADR-0031's rule, which this record follows.** The relation a source
states decides the kind. The Landgericht Rekau divorces Leopold and Agnes:
the court acts (`carried_out_by`, §3.4), so it is an organisation, with
the kind Landgericht and the place Rekau. Rudolf's school report makes
him a pupil of the Städtische Realschule (`educated_at`), so the school
is an organisation too. The registration card says Leopold was "held in
Peiltal court prison": a place, the location of his imprisonment. Where
one source uses the same institution both ways, the manifest holds two
entities, a site and an institution, each with its own record, linked by
the organisation's `location` claim. Clustering never merges a place with
an organisation, and neither record meets the town.

#### 2.4 A family (`family`)

| Field | What it holds | Keys? |
|---|---|---|
| `surnames` | the surnames the family is named by, as written | yes, for pairs only: clustering never merges two families |

"Familie Löbig", "the Löbigs" and "the Lobig family" each give the
surname Löbig or Lobig. Two documents' "Familie Löbig" pair, and the pair
goes to adjudication (#106): a surname names many families.

#### 2.5 Other kinds

Events have no name structure: an event is identified by who took part,
in which role (§3.2), and when. Marriages, partnerships and their events
follow ADR-0036. Occupations, vessels, military units, legal instruments,
artifacts and unclassified entities have no record either: each keys on
its exact normalised name, as in #361, so the naturalisation's "RMS
Flund" meets another "RMS Flund", never a bare "Flund". Vessels and
military units get a record only when a scratch run shows the pairs this
misses.

#### 2.6 Where records live

The record sits in `EntityRecord.structure`, beside the label: in the
batch view, and in the LLM cache's answer to the manifest call. The
batch's content hash covers it, so the identity map is made again when a
record changes (#361's reuse key).

**No field of the structure is written; an entity's note is (§4).** A
reading of a label is the pipeline's interpretation, not something the
source asserts: the card writes "LÖBIG, Ida", it does not say that Ida
is a given name. Written with `stated in` the card, the reading would be
attributed to a source that never made it (CLAUDE.md rule 1; ADR-0002,
ADR-0003); ADR-0022 rejected run-referenced name parts for the same
reason. A kinship class, a role or a civil status is written because the
source does say it: "Aunt Pünktchen" states a relation, a register
states who reported a birth, "Wwe." states a widowhood. Which part of a
name is the surname is only how a reader parses the name. What sources
assert about names keeps its home: typed name claims (ADR-0011),
`referred_to_as` and kinship claims (ADR-0018), and the label and
aliases. Records hold names, so they are local-only data, like the labels
in the batch views and the identity map.

**ADR-0022, as amended.** Ruling 1 still holds for the graph: there is no
name-part property. The manifest may now say which part is the given
name and which the surname, read from the document's own conventions; a
form it cannot settle is `unsplit`. Ruling 2's "never infer kind … from
position" gives way for the manifest in the same way: the model reads a
register's surname-first order as that register's convention, never as
a rule. §4's validator gives way to the parts (§8).

### 3. What becomes a claim

Structure that a source asserts is written, as claims and qualifiers that
carry the source's references.

#### 3.1 Relation and death markers

| The source writes | Extraction states |
|---|---|
| "dessen verstorbener Ehefrau Cäcilie" (the 1905 certificate) | Cäcilie's death: a `death` event with her as principal (§3.2); and her marriage to Eugen (ADR-0036) |
| "seine verstorbene Mutter Ida" (the compensation file) | Ida's death, and `mother` |
| "Witwer der Cäcilie Löbig geb. Tarle" (the burial register) | Eugen's civil status `widowed`; Cäcilie's death; and their marriage, ended by her death (ADR-0036) |
| "Löbig, Ursula, Wwe." (the 1912 directory) | Ursula's civil status `widowed` at the directory's date; no marriage, since no husband is named (ADR-0036) |
| "Sohn des Hutmachermeisters Eugen Löbig" | `father` |
| a patronymic (example pending #400) | the patronymic part (§2.1), and `father` where the source names the father |
| a married-name marker (example pending #400) | the surname typed `married`, and a `married_name` claim (ADR-0011) |

Marriages and partnerships: ADR-0036.

A new property, `civil_status`, holds a civil status the source states:
`unmarried` (never married), `married`, `widowed` or `divorced`, dated as
the source dates it. A civil status is a person's own state at a date. It
names no partner and makes no marriage: a marriage comes only where a
source names the partner (ADR-0036). A status changes over a life, so the
disagreement report never compares civil statuses as single-valued
(`disagreement.py:71-87` must not list it). A civil status statement is
person evidence for ADR-0020's check. Where the vocabulary has no claim
for what a marker asserts, the chunk writes a gap line (ADR-0013), never
a name part.

#### 3.2 Participant roles

A `participant` claim gains a role, as Wikidata qualifies one with
*object has role* (P3831): `principal`, `parent`, `partner`, `spouse`,
`informant`, `witness` or `officiant`.

- **Who is the principal.** In a birth or a death, the one born or who
  died. In any other event, the person it happens to: Ida in her
  deportation, Leopold in his imprisonment. A union's own events are the
  exception: the couple take part in the partnership or marriage, as
  `partner`s or `spouse`s, and its wedding, divorce or annulment carries
  only witnesses and an officiant (ADR-0036).
- **Gustav's birth register extract** gives the boy as principal and
  Sebastian and Hildegard as parents. Sebastian, who reported the birth,
  is the informant too: one participant claim with two role qualifiers.
- **The 1905 marriage certificate's Standesbeamter** is the officiant of
  Sebastian and Hildegard's wedding (ADR-0036); a witness the source
  names takes part with the role `witness`. The manifest's rule that the
  apparatus certifying a record is boilerplate (`extract` false;
  `pipeline/chunking.py:403-409`, `claims.py:511-523`) keeps its point
  for those who only certify, and gives way where the source shows them
  acting: an officiant or a witness of the event, and the authority that
  issued the document (§3.4).
- **A birth or a death is its principal's own.** With no principal, or
  with several, it is nobody's own. Its date moves only onto its
  principal, and only where it does not conflict with the principal's own
  date of that kind: #361's guard (`pipeline/clustering.py:956-966`) is
  kept. This replaces #361's one-participant rule: a death that names
  only its informant dates no one's death.
- **A death's principal is never also its informant**: validation refuses
  such a claim.
- **A participant claim points from the event, or the union (ADR-0036),
  to the person.** The accepted kinds (§3.5) refuse one from a person to
  an event, which #361 read either way (`pipeline/clustering.py:918-922`).
- **The GEDCOM adapter** makes each person's death event with that person
  as principal.
- **Privacy.** #345 reads the role: a death event is death evidence for
  its principal alone, and a dated birth event is a birth of its
  principal. Until #345 lands, the possibly-living readers
  (`backup.py:205-220`, `staticsite.py:83-102`) count every participant
  of a death event as dead, informants, witnesses and officiants
  included. So #345 lands before any export or publication.

#### 3.3 Kinship classes

A `relative` claim keeps its `kinship_type`, the term as written
(ADR-0018), and gains a controlled class beside it:

- `kinship_class` (an item): parent, child, sibling, grandparent,
  grandchild, aunt or uncle, niece or nephew, cousin, parent-in-law,
  child-in-law or sibling-in-law, or `relative` where the term's class is
  unclear. A spousal term gives a marriage, not a class (ADR-0036).
- `side` (an item): paternal or maternal, only where the term states it:
  a source's "paternal aunt" is an aunt or uncle on the paternal side.
- `degree` (a quantity): how many "great-"s the term has ("aunt" 0,
  "great-aunt" 1); for a cousin, its number ("second cousin" 2).
- `removes` (a quantity, cousins only): its removes. A "second cousin
  once removed" has the degree 2 and the removes 1.

| Term | `kinship_type` | class | side | degree | removes |
|---|---|---|---|---|---|
| "Aunt Pünktchen" (the interview) | aunt | aunt or uncle | — | 0 | — |
| "Dad's cousin Leopold" (the interview) | cousin | cousin | — | — | — |
| "our great grandmother" (ADR-0018's Cäcilie) | great grandmother | grandparent | — | 1 | — |

A side, a degree or removes the term does not state is left out, never
guessed: "cousin" alone states no number. A literal term that reduces to
a primitive stays one: "Dein Vater" is `father`. `queries/kinship.rq`
then reads classes instead of its term table
(`queries/kinship.rq:224-285`), a reader after write (§10).

#### 3.4 `issued_by` and `carried_out_by`

ADR-0031 §1 leaves both to this revision.

- **`issued_by`** (P2378): a document → the organisation that issued it.
  The claim's subject is the source artifact itself, named by the SHA-1
  of its media as a claim's source is (ADR-0010), and resolved to the
  artifact item at write, as the writer resolves sources (writer step 0).
  Gustav's birth register extract was issued by the Standesamt Flörburg.
- **`carried_out_by`**: an event → the organisation that carried it out;
  a person who performs an event takes part in it as its `officiant`
  (§3.2). The Landgericht Rekau carries out Leopold and Agnes's divorce
  (an event of ADR-0036). Wikidata has no exact equivalent; the nearest
  are *court* (P4884) and *organizer* (P664).
- The rule that a document's issuing authority is boilerplate gives way
  where the source shows the authority issuing: that authority is
  extracted, with `extract` true.

#### 3.5 The kinds each property accepts

ADR-0031 rules that the relation decides the kind of what it points to;
its §5 leaves the mechanism to this revision. Every item-valued property
and qualifier declares `accepts`, the kinds its value may be, and the
manifest loader refuses one that declares none, so no property escapes
the check. `unclassified_entity` is accepted wherever a real kind is
(ADR-0013). Validation refuses a value of another kind, as it refuses an
unknown property, and each refusal is a counted reject with its reason.
The critic no longer has to spot a mismatch by reading wording.

| Property or qualifier | Accepts |
|---|---|
| `place_of_birth`, `place_of_death`, `burial_place`, `residence`, `location`, `country_of_citizenship`, `part_of` between places | place |
| `part_of` from an artifact fragment or a deck's derived media (the registrar's own statements, ADR-0008) | artifact |
| `father`, `mother`, `child`, `sibling`, `relative`, `gave_refuge_to` | human |
| `participant`, `perspective` | human, family |
| `member_of` | organization, family |
| `employer`, `educated_at`, `issued_by`, `carried_out_by` | organization |
| `occupation` | occupation_class |
| `time_period` | time_period_class |
| `image` | artifact |
| `depicts` | any kind (ADR-0005) |
| `said_to_be_the_same_as` | the subject's own kind |
| `sex_or_gender` | male, female |
| `sourcing_circumstances` | its vocabulary items (`circa`, …) |
| `object_has_role` | the roles of §3.2 |
| `kinship_class`, `side` | the classes and sides of §3.3 |
| `civil_status` | `unmarried`, `married`, `widowed`, `divorced` |
| `judgement` | the note categories of §4 |
| ADR-0036's properties | as ADR-0036 declares them |

A retired property (ADR-0036's `spouse`) accepts nothing: validation
refuses any claim of it, and the loader's rule exempts it. The reference
and schema properties (`stated_in`,
`inferred_from_extraction_run`, `instance_of`, `subclass_of`) declare
theirs too: an artifact, an extraction run, and a class.

#### 3.6 The vocabulary this adds

| Key | What it is | Datatype | Wikidata |
|---|---|---|---|
| `object_has_role` | qualifier on `participant` | item | P3831 |
| `principal`, `parent`, `partner`, `spouse`, `informant`, `witness`, `officiant` | its values | items | — |
| `kinship_class` | qualifier on `relative` | item | — (P1039 states the inverse) |
| `side` | qualifier on `relative` | item | — |
| `degree`, `removes` | qualifiers on `relative` | quantity | — |
| the kinship classes, `paternal`, `maternal` | their values | items | — |
| `issued_by` | document → organisation | item | P2378 |
| `carried_out_by` | event → organisation | item | — (nearest P4884, P664) |
| `civil_status` | a person's stated civil status | item | — |
| `unmarried`, `married`, `widowed`, `divorced` | its values | items | — |
| `judgement` | reference property: a note's category | item | — |
| `judgement_kind`, `judgement_relation`, `judgement_date_reading`, `judgement_identity`, `judgement_other` | its values | items | — |
| `judgement_note` | reference property: a note's sentence | string | — (P2315 is deprecated; P887 names a heuristic) |
| `accepts` | a manifest field of every item-valued property | — | — |

### 4. Judgement notes

**What a note is.** Any extracted fact, a claim or a manifest entity, may
carry an optional `note` where the extractor made a judgement call that a
careful reader could make differently: a category (`kind`, `relation`,
`date reading`, `identity` or `other`) and one sentence, in English, that
names what was read.

| Category | Example | ADR-0023's axis |
|---|---|---|
| `date reading` | "the postmark '19.8.38' read as 1938" | value |
| `relation` | "'Dein Vater' recorded as Klara's father, not as a relative" | predicate |
| `identity` | "the card's 'Klurnau' read as the town, since it names no camp" | subject |
| `kind` | "the court prison typed a place: the card names it only as where Leopold was held" | predicate |
| `other` | any other call | existence, or any |

The prompt asks for a note only where a reasonable reader could decide
otherwise. A claim without a note is not thereby certain.

**Where a note is written.** In the extraction-run reference block of the
statement it concerns, beside `inferred from extraction run`, as
`judgement` (the category, an item) and `judgement_note` (the sentence,
a string). Never in the `stated in` block, never as a qualifier, never on
the run item.

- A claim's note goes on that claim's statement.
- An entity's note (a kind or an identity call) goes on the `instance of`
  statement the writer writes, or adds a reference to, for that entity
  (writer steps 2 and 5).
- A note on a fact that is never written (a refused or rejected claim, an
  entity no claim mentions) stays in the run's outputs.
- A curated claim has no run reference, so it has no note.
- A note over 400 characters is cut as the writer cuts a locator: at a
  break between words, ending with " …" and a digest of the whole text
  (#381; #384 for excerpts). It never refuses its claim.

**How the writer merges.** The run block is compared whole, in NFC, with
its note (writer step 4), so writing a batch again adds nothing (#335
holds). A second claim of the same run on the same statement adds a run
block only if its note differs. No block is ever edited to add or remove a
note, so one statement can carry several blocks from one run; a reader
counts runs by run item, not by blocks. A later run's block carries that
run's note, or none. Earlier notes stay, never updated or removed.

**Who reads notes.**

- The review sheet shows two machine columns beside `confidence`:
  `judgement` and `judgement_note`, never the curator's `notes` column
  (`pipeline/review.py:113-128`). An entity's note appears on its
  subject's rows, as `critique_notes` does. An edited note changes the
  row's `claim_id` (`pipeline/review.py:133-147`), so the sheet fails
  loudly.
- The run report counts notes by category, and notes written (the
  writer's count, with #410, which writes the notes to the wiki), as
  aggregates (docs/RUN_REPORT.md).
- The non-privileged export (`public.nt.gz`) drops both note snaks, in
  any form: `backup.DROPPED_PROPERTIES` names both properties (#363;
  ADR-0020; MODELING.md, "Living persons and privacy"). An excerpt, by
  contrast, leaves there with the statement it attests (the owner,
  2026-09-29). The private site may show a note beside its source,
  labelled as the extraction's remark, never as a mark on the value.
- **A note never ranks or filters.** No query, view, report, frontend,
  export or stage uses a note's presence, category or text to rank,
  prefer, order, hide or pick a value. Curation may query notes to choose
  what to check again. A unit test guards this, as
  `tests/unit/test_no_wiki_before_write.py` guards the wiki: only the
  writer, the review sheet, the run report, the export filter and one
  curation query may name the note properties.

**ADR-0023's arguments, answered.**

- *An uncalibrated self-report*: a note has no number or scale, and a
  reader checks it against the excerpt beside it.
- *Not re-derivable*: a note is a dated record of one run, bound to the
  run item, not a claim about the world.
- *Blind to confident-but-wrong*: conceded. So a missing note means
  nothing, and the critic and the under-witnessed queue stay the checks.
- *Rank governance*: rule 4 now holds by rule and by the unit test above,
  no longer "by construction".
- *ADR-0020's stale inference*: a note records what one run did at its
  date; it is not a present-tense fact that can go stale.
- *The rejected trust tier*: a note is never shown as a status of the
  value.
- *§1's "never a per-claim identity flag"*: an identity note is text in
  the reference of the `instance of` statement; identity is still decided
  by clustering and by #106.
- *The rejected reference snak*: that concerned a quantity snak's value
  nodes, which an item and a string do not have.
- *Ruling 6 stands*: notes record the extraction's process, not an
  acceptance.

**ADR-0023's dated line**, added with this ADR: "Superseded in part by
ADR-0035 (owner decision 2026-09-29, #385): §2 and Ruling 1 no
longer keep every machine judgement out of the graph. An extraction's
judgement note, a category and one sentence, is written inside the
extraction-run reference of the statement it concerns, as process
provenance. It is never used to rank, order or filter a value; a
statement without one is not thereby certain; the public export leaves
notes out as free text. The graph still holds no confidence number,
epistemic qualifier, or per-statement trust or acceptance marker. §1,
§3–§6 and Rulings 2–6 stand. The consequence "zero graph / export /
privacy delta" no longer holds." ADR-0003's dated line says that an
extraction-run reference may carry a judgement note.

### 5. How extraction fills it

**The prompts.** The entity-manifest prompt asks for each field of §2, the
notes of §4, and each type hint in English, which #91's
unclassified-entities query groups. It describes each pattern by its
structure (a surname-first register, capitals, a maiden-name or
married-name marker, a courtesy title, a place's generic head, an
institution's kind), gives examples from the fictional universe, and
lists no marker words, so it applies to sources in any language. The
chunk prompt asks for roles, kinship classes, civil status, sex as the
source or its grammar states it, `issued_by` and `carried_out_by`, the
claims of §3.1, and notes. Claims still never say which part of a name is
which; the manifest's structure does. A name's type is still recorded
only where the source marks it (`pipeline/chunking.py:199-202`, 375).
Each answer still ends with `{"done": true}` (#7), and none carries a
count of records or notes.

**Validation (Pydantic).** The structure is a strict model that refuses
unknown fields, with one variant per kind and closed values for statuses,
surname types, roles, classes and note categories. Beyond the schema:

- the structure's variant matches the entity's kind;
- **no addition**: every part the model writes occurs in the label or an
  alias, folded, as whole words (a family's surname may end inside a
  word, as in "the Löbigs"). The model may divide and label a name, never
  add to it: ADR-0022 §4's hallucination check, made structural. Standard
  forms are exempt. A record that gives the naturalisation's Gustave the
  surname Sörne, which no form of his holds, is set aside;
- a `name` status needs a name part, and a `toponym` status a name or an
  address;
- a claim's value is of a kind its property accepts (§3.5), and a death's
  principal is not its informant.

**Omissions and inconsistencies are flagged, never repaired by parsing.**
An omitted optional field is empty. A structure that fails validation is
set aside, never the whole manifest: the entity keeps its label, aliases
and kind, and the reason is counted in the extraction's `coverage.json`
(`records.set_aside`, by category), as is the record
(`records.set_aside_records`, once however many reasons it gives); the
same `records` counts the records given, by status
(`records.by_status`), the `unsplit` forms they hold
(`records.unsplit_forms`) and the recordless mentions.
Such a mention, like one of a batch without a manifest (the line-range
fallback of ADR-0007), is *recordless* (§7, rule 1), and is counted in
the identity map's summary and the run report. The capitalised-token
coverage check goes (`pipeline/chunking.py:1504-1553`), since it takes
capitals for names.

### 6. How the GEDCOM adapter fills it

An export's tags carry the structure, so the adapter reads them and
guesses nothing. The one exception is the inline convention of quotes
and brackets in a NAME value, which §10 names.

| Tag or form | Field or claim |
|---|---|
| NAME: the part before the slashes | given names, less an NPFX it starts with, and less its quoted and bracketed parts |
| NAME: the part between the slashes | a surname, typed by the NAME's TYPE |
| NAME: the part after them | suffixes, unless NSFX gives them |
| NAME with no slashes, and no GIVN or SURN | `unsplit` |
| GIVN; SURN with SPFX | given names; surnames, each whole with its particle |
| NPFX; NSFX | titles; suffixes |
| NICK, or a quoted part ("Cäcilie 'Cilli' Tarle") | nicknames |
| a bracketed part ("Ida (Peppi) Sörne", "(geb. Laudler)") | an alias and an `unsplit` form, with no typed-name claim, counted, and a `name` claim of no type with its text verbatim, cue or note included (ADR-0011) |
| TYPE birth or maiden | the NAME's surname `birth`, and its `birth_name` claim (ADR-0011) |
| TYPE married or marriage | `married`, and its `married_name` claim |
| TYPE aka, nick or nickname | the NAME's given names are nicknames, and its `nickname` claim |
| TYPE official, legal, change, immigrant or adopted | `other`, and its `official_name` claim |
| any other TYPE, or none | `other`, with no typed-name claim; the NAME's form, unless it is the label's, a `name` claim of no type (ADR-0011) |
| `_MARNM` | a `married` surname: the part between its slashes; a value without them is `unsplit` |
| PLAC | its first non-empty component is the place's name and the rest are `within`, each as written, with no head or toponym split: GEDCOM lists jurisdictions from the lowest |
| HEAD.LANG | GEDCOM 5.5.1's language name, mapped to a BCP 47 code, sets the language of the adapter's texts instead of a fixed `en` (`GEDCOM_LANGUAGES`, `UNDETERMINED` in `pipeline/gedcom.py`); an absent or unknown one gives undetermined (`und`), counted |
| a DEAT that states the death and gives no date, place or cause | a `death` event with the person as principal |

- **No NAME part is dropped without a count** (#352). A part the NAME's
  value does not restate still reaches its field, and the name the parts
  give becomes an alias. A particle with no surname is counted.
- **An export's given name is its own standard form**: the adapter has no
  model to expand a diminutive, so its NICKs and the documents' standard
  forms do that work.
- **A one-word `unsplit` form counts as a given name for a lone name's
  match**, so a document's lone "Peppi" still meets the export's Ida.
- **The bracket rule amends ADR-0011's inline convention.** A bracket may
  hold a maiden name, a later name or a note as well as a nickname; a
  quote marks a nickname. Quotes and brackets inside a NAME value are a
  convention of the people who keep trees, not GEDCOM's grammar, so §10
  names them as a heuristic that remains.
- **An implementation check:** the instance must accept `und` as a
  monolingual language. If it does not, the adapter keeps `en` for those
  texts and counts them, so the gap shows in the run report.

### 7. How clustering uses it

**Matching rules.**

1. **Keys come only from typed parts.**
   - A person whose status is not `name`, and a place whose status is not
     `toponym`, keys on nothing.
   - A person: each given name (by its standard form) and each nickname,
     with each of its own surnames and patronymics and each surname of
     the relatives the same document names. The divorce judgment's
     "Agnes Löbig geb. Nertler" keys as Agnes Löbig and Agnes Nertler.
     With no surname of either kind, its given names alone, which pair
     only with the GEDCOM export, as in #361.
   - No key pairs two parts that fold alike: a given name or nickname
     makes no key with a surname or patronymic that folds to the same
     key, the person's own or a relative's (#361's guard,
     `pipeline/clustering.py:1070-1073`). An `unsplit` form keys each
     ordered pair of its distinct words, never with a relative's surname:
     "Löbig Ursula" keys (Löbig, Ursula) and (Ursula, Löbig), never
     (Löbig, Löbig).
   - A key that only a GEDCOM `unsplit` form gives (a bracketed part, a
     NAME without slashes) proposes a pair but never makes a link
     confident: the export does not say which word is which, and some of
     its words may be markers ("geb.").
   - A place: each of its names as written, its address among them,
     except a short form that is another name's toponym (§2.2).
   - An organisation: each proper name; without one, each kind with each
     place.
   - A family: each surname.
   - A recordless mention, and a mention of a kind that takes no record
     (events apart, rule 9): its exact normalised name, from its label
     and from each alias. A recordless mention meets only mentions
     without a record, never one with a record, and for the four kinds
     that take records such a link is never confident. Two line-range
     batches that each name "Hildegard Löbig" pair, but never merge.
2. **Sex and civil status only block.** Stated sexes that differ, or civil
   statuses that cannot both hold, make a pair ambiguous, and a closure
   that joins them a contradiction. They never corroborate. Statuses
   cannot both hold where `unmarried` (never married) is stated for a date
   whose range lies wholly after a `married`, `widowed` or `divorced` date
   another source states: the directory's widow Ursula Löbig (1912) never
   merges with an Ursula Löbig whom a later source calls never married.
3. **Given names match on their standard forms, exactly, never by
   prefix.** "Gustl" meets "Gustav" through its standard form where its
   document supports one, as the 2008 family history does ("Gustav, the
   youngest, was Gustl at home"); a man's given name no longer meets a
   woman's that adds a letter to it.
4. **Parts compare by #361's `normal`** (`pipeline/clustering.py:541-543`):
   case and accents folded after Unicode normalisation (`fold`, 522-527),
   each word's edge punctuation stripped, and single spaces, plus the
   umlaut rule below.
5. **Places.** Fläckfeld in Kreis Mockburg (the 1905 certificate) and in
   Kreis Peiltal (the 1984 tree) stays ambiguous: from the names alone,
   one village two sources place differently cannot be told from two
   villages. A place nests in another of the same name when its `within`
   are among the other's, compared as written with the umlaut rule. Two
   places are at the exact level, which `max-anchors` counts, when they
   share a name and their containers are equal; a place counts the
   anchors it nests in as one, as in #361. The record compares a place's
   containers as one set, so a short form of its name no longer lets it
   nest in a namesake elsewhere.
6. **Organisations** link on a shared proper name of two words or more; on
   a proper name of one word only with a shared kind or place, since a
   firm named after a surname, such as "Firma Löbig", may be any Löbig's;
   and without a proper name, only where kind and place both agree. Two
   places agree when they would link under rule 5, or when one lies
   inside the other by the containers the manifests state; they differ
   only when neither holds. Places that differ keep a pair ambiguous even
   on a long shared name: two shops called "zum Blauen Hut" in two towns
   stay apart.
7. **Families** pair on a shared surname and never merge in clustering:
   every pair goes to #106, as two documents' "Familie Löbig" do.
8. **Kin and contradictions in record terms.** Two kin agree when they
   share a key (a given name's standard form or a nickname, with a
   surname), never a surname alone, and their dates do not conflict: a
   mother named only "Frau Löbig" agrees with no one. Two members of a
   closure contradict on names when they share no key (for people), no
   name and no nesting (for places), or neither a proper name nor a kind
   and a place (for organisations).
9. **Events.** A birth or death is its principal's own and meets another
   through its principal: Gustav's birth in his register extract and in
   the naturalisation meet through him. Marriages, partnerships and their
   events follow ADR-0036. Other events meet through their participants,
   as in #361.

Everything else of #361 stays: dates as ranges at their precision, kin
lifted from both ends, soft and hard cannot-link, the closure and its
contradictions, the confident, ambiguous and conflicting classification,
GEDCOM anchoring, hubs, the identity map and its reuse. Its `VERSION`
bumps.

**The umlaut rule: a second key under DIN 5007-2.** A part that holds an
umlaut (ä, ö, ü, or Ä, Ö, Ü) keys twice: once with the accent folded
(Flörburg as `florburg`), and once with the umlaut written out
(`floerburg`: ae, oe, ue). ß already folds to "ss". A two-letter spelling
is never turned back into an umlaut. The rule applies to every part that
keys or is compared: names, containers (rule 5), organisations' kinds
and places, and recordless names. So the naturalisation's "Floerburg"
meets the register's "Flörburg". The alternative was a standard spelling
the model gives for each part. It would reach spelling variants of any
language, but the GEDCOM adapter has no model, so the export's parts
would key one way and the documents' another; a model's spelling varies
from call to call; and it invites "correcting" a spelling a person really
used, such as the emigrants' Lobig. **The pick is the DIN 5007-2 second
key**: closed, and the same on every run. Given names keep their
model-given standard form, since no rule can expand a diminutive, and it
is folded the same way.

**The word lists go, and so do the token-length thresholds.**

| #361 reads | Where (`pipeline/clustering.py`) | Replaced by |
|---|---|---|
| `TITLES` | not a name token (608); a lone token after one names a surname alone (612-615) | `titles`, which never key; a record with surnames and no given name or nickname names a surname alone |
| `KIN_WORDS` | not a name token (608) | the statuses `kinship` and `courtesy`, which key nothing; the term becomes a claim (§3.3) |
| `PARTICLES` | not a name token (608) | kept whole in `surnames`; generational words in `suffixes`; a patronymic in `patronymics` |
| `FORM_MARKERS` | not a name token (608) | one record gathers every form's parts; the marker is none of them |
| `MAIDEN_MARKERS` | a surname-first name split at one (561-569); "X Y née Z" read as two forms (629-645) | a `birth` surname; every surname keys with every given name |
| `OCCUPATIONS`, `OCCUPATION_ENDINGS` | an occupation where given names stand (572-578, 597) | no name part; an `occupation` claim |
| the "N.N." stand-in | a surname-first name without given names (337, 603) | the status `placeholder` |
| `ORG_GENERIC` | the tokens an organisation is known by (683-690, 1024-1035) | `names` and `kinds`; generic words are neither |
| `ORG_INSTITUTIONAL` | a name of institution words needs a place (1601-1607) | an organisation without `names` keys and links on kind and place |
| `PLACE_GENERIC` | heads never key (1003-1011) | `head`; a place keys on its name as written |
| brackets, commas, order | forms, surname-first order, a place's first comma part (552-558, 581-603, 1012-1022) | the record |
| `min-name-token`, `min-place-token`, the organisation token floor | token length (`pipeline/clustering.yaml:17`, 21; `pipeline/clustering.py:400`) | nothing: keys are whole parts |
| prefix matching | Hilde meets Hildegard (658-680, 1213-1227) | standard forms |
| one participant | a birth's or a death's date (956-970) | the principal |

### 8. Fingerprints and aliases

**A mention fingerprint hashes what it hashes now**
(`pipeline/fingerprints.py`, #197): the artifact's SHA-1, the verbatim
label in NFC, the kind, the excerpt span and the occurrence. The label
stays verbatim, so run diffs stay stable. No record, note or role enters
a fingerprint: a new reading of a label moves a mention between
clusters, which the diff (#198) shows as a merge or a split, and never
renames it. The canonical label does not change either: it stays the
fullest clean form the document gives (ADR-0007, ADR-0022), or, for
someone unnamed, the label rule of §2.1.

**The alias list and its cleanup derive from the parts**, in place of the
token shapes of `pipeline/names.py:36-77`. An alias's parts are the
record's parts that occur in it by §5's no-addition test. For a person,
an alias that gives no part is no name and is dropped (a kinship term or
a role that slipped through); an alias whose parts the label already has
is derivable and dropped; and an alias that adds a part is kept
("Wastl"). So "Frau Löbig" and "Löbig, Ida" beside "Ida Löbig" are
dropped, where `pipeline/names.py:47-53` keeps "Löbig, Ida": this is the
change to ADR-0022 §4. For a place or an organisation, an alias that
gives no part (a deictic or generic reference, #42) is dropped, while a
short form is kept for lookup, since a place is looked up by its
toponym.

### 9. What the same prompt revision takes in

- **#42**: a place alias is a toponym and an organisation alias a name; a
  deictic or generic reference is neither (§2.2, §8).
- **#4**: an office-holder is a `human`, whose label is a `role` where the
  source names only the office; a religion is a `religion` claim, never an
  `occupation`.
- **#44, first part**: the critic's stable rules
  (`pipeline/critique.py:58-94`: a kind that does not fit, a role or
  relational reference taken for an alias, the escape hatch misused) are
  stated where names and kinds are made, and most become structural
  checks (the statuses of §2, the accepted kinds of §3.5, the aliases of
  §8). The critic does not see records in this revision; it narrows once
  scratch run 1 shows what still recurs. The second part stays deferred
  (#171).
- **#91**: type hints in English, for its unclassified-entities query.
- **#89**: ADR-0031's rule, in the prompt and the critic, with
  `issued_by` and `carried_out_by`.
- **Judgement notes** (§4).

Taking #4 and #42 in before run 1 turns their "measure first" acceptance
into "fix first, and measure in run 1": under the pre-baseline exception
(docs/AGENT_PROTOCOL.md §2), run 1's report stands in for their diffs.

### 10. What remains

**Still in place before write, named.**

- The organisation rule's word count (§7, rule 6): a count, not a list,
  guarding firms named after a surname.
- `normal`'s edge punctuation (§7, rule 4) and the no-addition test's
  word boundaries (§5, §8), save a family's surname, which may end inside
  a word ("the Löbigs").
- Containers compared as written: an abbreviation ("Kr. Peiltal" beside
  "Kreis Peiltal") misses, and the pair stays ambiguous.
- GEDCOM's grammar and vocabulary: slashes, comma-separated pieces, NAME
  TYPE values (`_NAME_TYPE_PROPERTY` in `pipeline/gedcom.py`), PLAC's order of
  jurisdictions and HEAD.LANG's language names. With them, the tree
  keepers' inline convention: a quote marks a nickname, a bracket an
  untyped form (§6). An initial or a lettered stand-in ("N.N.") in an
  export keys as written, and meets only the same form; a placeholder
  with no letter or digit ("?") is no key at all, since the adapter
  leaves it out (the 2026-10-02 amendment above). A title an export writes in a
  NAME's given part with no NPFX ("Dr.") keys as a given name: an
  accepted limitation.
- GEDCOM dates: month names beyond the standard's abbreviations come from
  an English and German table (`MONTHS` in `pipeline/gedcom.py`); a date in
  any other language gives no claim and a warning, as now.
- #361's date thresholds (`circa-years`, `max-corroborating-years`):
  numbers, not wording.
- The transcription check's English phrases only flag a transcription
  (ADR-0037, #389): it fails on the record's structure, so a refusal in
  any language, which is not the record, fails. The stream assembly takes
  the answer after the last tool result.
- Language detection reads a text that has no transcription record
  outside its editorial marks, and a vision transcription's language
  comes from its record (ADR-0037, #389); the language anomaly checks
  read the result.
- The critic's axis is read from its target's grammar
  (`pipeline/critique.py:26-41`, `pipeline/orchestrate.py:1555-1579`,
  `pipeline/report.py:942-950`): a grammar the prompt sets, not a
  language.
- The writer labels items in `en` (`pipeline/writer.py:553-559`) and finds
  them by exact label (`pipeline/writer.py:530-548`,
  `pipeline/resolve.py:18-21`) until #109.
- The single-call extraction path (`cli.py:375-394`,
  `pipeline/extract.py:49-102`) makes no manifest, so its batches carry no
  records. Scratch runs never take it.
- Operator-side words that read no source: the review sheet's decision
  words (`pipeline/review.py:101-102`), the monitor's command grammar
  (`pipeline/monitor.py:183-218`), the model-alias check
  (`pipeline/rundir.py:369-385`) and the site's skip of a "living person"
  label (`staticsite.py:541`; gone since #388, amended above).

**Readers after write, moved in a follow-up issue.** Scratch run 1 writes
nothing, so these wait (the first four settled by #388, amended above):

- reconcile's name tokens and prefix overlap
  (`pipeline/reconcile.py:256-273`), in the per-document reconcile
  (495-503) and the ADR-0019 sequencer (961-966);
- the place hierarchy's comma parents (`pipeline/places.py:184-235`, and
  `comma_depth` at 58-60);
- the impossibility filter's container-label check
  (`pipeline/reconcile.py:456-474`), which clustering calls with whole
  labels meanwhile;
- the disagreement report's name comparison (`disagreement.py:469-480`);
- `kinship.rq`'s term table (§3.3);
- the possibly-living readers' death events (`backup.py:205-220`,
  `staticsite.py:83-102`), in #345 (§3.2).

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
  stay as they are. The design list PR #361 gave #66 (its items 4 and 5,
  names and blocking keys) is restated from records, and ADR-0027 cites
  this ADR for how keys are read.
- **#106 (adjudication)** sees records, roles and classes rather than raw
  labels, in any language, and can weigh a married surname against a
  birth surname as ADR-0011 advises. Fewer spurious candidates reach it,
  since keys no longer meet on a shared token. For the same reason, it no
  longer sees pairs that share only part of a name ("Bad Flörburg" and
  "Flörburg", "Blauen Hut" and "zum Blauen Hut"), which #361 proposed as
  ambiguous; a candidate rule for them is #106's to add, if the runs show
  such pairs missed.
- **Two spellings of one given name across languages**, such as Gustav
  and Gustave, which #361's prefix matching paired, no longer meet on
  their names alone. They meet where a source names both, as the
  naturalisation does, and a diminutive meets its full name through the
  standard form its document supports (the 2008 family history's "Gus to
  everyone" is Gustave).
- **Patronymics key like surnames**, so a person known by a given name and
  a patronymic meets another known the same way, and not only the GEDCOM
  export.
- **#107 (overrides)** keeps its key. Overrides key on fingerprints, which
  a new reading does not change, and a changed record re-queues only the
  clusters whose members move.
- **#345** gets the principal role it asks for, and lands before any
  export or publication (§3.2).
- **Risks.**
  - *The model reads one name differently in two documents.* A misread
    part mostly loses a key, which splits rather than merges. Standard
    forms and the umlaut key absorb common variants. The no-addition
    check bounds what a misreading of a part can do, though not a
    standard form, which it exempts: a wrong standard form can join two
    people, which their dates and kin still have to corroborate. The run
    report counts records by status, `unsplit` forms, set-aside
    structures and notes, and the diff shows the clusters that move, so
    the prompt is fixed where the counts point.
  - *Over-structuring.* Every field is optional and its values closed, and
    the escape hatches (`unsplit`, the statuses, the `relative` class) say
    "not settled" instead of forcing a reading. Every field but two has a
    reader: the head and toponym split, for one, keeps a camp's short form
    from meeting its town (§2.2). An address's street and number have none
    yet, since an address keys as written; they are kept for the place
    hierarchy's follow-up (§10).
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
- **Dated lines.** This ADR adds them to ADR-0003, ADR-0007, ADR-0011,
  ADR-0013 (§A, type hints in English), ADR-0018, ADR-0020, ADR-0022 and
  ADR-0023 (§4's text). ADR-0014's (the kinship class) waits for #398,
  which edits that header, and lands with #385's implementation.
- **Docs that move with the implementation:** docs/MODELING.md ("Names",
  "Places", "Organizations", "Kinship", "Identity and reconciliation",
  "Reference pattern", "Living persons and privacy", and the "Machine
  uncertainty is process" entry under "Contradictions"); the pipeline
  README's clustering section; docs/RUN_REPORT.md (the counts of records and
  notes); docs/EXAMPLES.md; and the docstrings of the writer, the review
  sheet, `Claim` and `EntityRecord`.
- **Tests** (#385): fictional fixtures in languages beyond English and
  German, once a separate issue has extended the universe with them; the
  probes; the note guard of §4; mutation evidence; `stage:extraction` and
  `stage:clustering` under the pre-baseline exception.

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
- **Keep judgement notes in the run's outputs only.** Rejected by the
  owner (2026-09-29): a note written beside the run's reference can be
  queried long after the write, and reads as the extraction's remark on
  its own reading, never as the source's.
