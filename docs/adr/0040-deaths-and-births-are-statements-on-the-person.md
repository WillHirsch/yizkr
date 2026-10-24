# ADR-0040: Deaths and births are sourced statements on the person, and a parent's kind is stated only where the source states it

Status: **accepted on the owner's merge** (owner decisions 2026-10-24,
#658). Like ADR-0024, ADR-0025 and ADR-0031, it records decisions the
owner has made, so the owner's merge of the PR that adds it, with its
owner questions (§9) answered, is its acceptance (AGENT_PROTOCOL §8,
§9). It reverses ADR-0020 §2's death event, ADR-0021's #323 death
event, ADR-0035 §3.2's birth and death events and ADR-0036 §1's
`ended_by` → death, and amends ADR-0023, ADR-0033, ADR-0039 §10.1(c)
and MODELING.md (§1). Each passage it reverses or amends keeps its
text, followed by a line dated 2026-10-24 that names this ADR. ADR-0007
needs no line: its roster reads birth and death years, which come from
statements on the person, and its #610 amendment (`principals_apart`)
reads event principals in general, which still serves a deportation or
an imprisonment.

Its build is follow-up B (§8), which lands with follow-up A's fixture
cases. Until B lands, the code does what the passages it reverses say;
every dated line it adds, and every MODELING.md line dated ADR-0040,
describes the model from that build on.

## In brief

- **A death is a set of sourced statements on the person.** The burial
  register gives Eugen a `date_of_death` of 16 March 1909, a
  `place_of_death` of Flörburg, a `cause_of_death` "Wassersucht", an
  `age_at_death` "73 Jahre" and a `burial_place` of Friedhof Flörburg
  dated 20 March 1909, each cited to its own line. No item stands for
  the death.
- **"Died, nothing else known" is a `date_of_death` of unknown value**
  (Wikibase "some value"), used only where a source says the person
  died and gives no limit: a GEDCOM `1 DEAT Y`, or "the late Felix" in
  an undated note (both hypothetical). A blank field, "d. ?" or "date
  unknown" gives no claim.
- **A death that a document's date bounds keeps today's form.** The
  1958 letter's "вдова Вашего покойного брата Рудольфа" gives Rudolf a
  `date_of_death` of 25 December 1958 with `latest_date` 25 December
  1958, beside the certificate's 6 March 1941: a refinement, not a
  contradiction.
- **A union ended by a death points to the partner who died**
  (`ended_by_death_of`, Q2), and readers take that partner's own death
  statements, so nothing is copied.
- **A birth is the same.** `date_of_birth` and `place_of_birth` go on
  the child, and its parents are `father` and `mother`, each cited. No
  item stands for the birth, and the `parent` role is retired.
- **A parent's kind is stated only where the source states it.** A new
  qualifier, `parentage`, says birth, adoptive or foster, set only where
  the source's words or tags say so; the word itself stays in
  `kinship_type`. A birth father and an adoptive father never disagree.
- **Privacy fails closed.** The new person-valued property joins the
  person-value set; validation refuses a death property on anything but
  a person; an unknown-value `date_of_death` is death evidence because
  a source stated the death.

## Context

Today a death can be recorded in two places, and so can a birth:

- **statements on the person:** `date_of_death`, `place_of_death`,
  `cause_of_death`, `burial_place`; `date_of_birth`, `place_of_birth`,
  with `father` and `mother`;
- **an event item:** a `death` or a `birth` with the person as
  `principal` (ADR-0035 §3.2), parents as `parent` and an informant as
  `informant`. ADR-0020 §2 makes "died, nothing else known" a death
  event; ADR-0021's #323 amendment makes `1 DEAT Y` one; ADR-0035 §3.1
  makes "the late" and a widow's or widower's marker one; ADR-0036 §1
  ends a union by `ended_by` → the partner's death event.

Scratch run 3 shows that the event adds nothing (aggregates from #146,
already shared with the owner): 61 death events, every participant the
principal (no informants, no witnesses); 128 death dates on persons and
50 on events, with 17 people dead only through a death event; 19 birth
events, the 13 that carry a date or a place all repeating the person's
own, and 6 empty; two births on one form page lost; no adoption or
foster wording.

With two places to look, every reader reads both: privacy reads a death
event's principal (`core/privacy.py`, `event_principal` and
`read_evidence`; ADR-0020 as amended by #345) and has to stop an event
being taken for a person on its own death statements (#577,
`EVENT_DEATH_PROPERTIES`); clustering moves an event's date onto its
principal (`ONE_PER_PERSON` and the principal pass in `_nodes`);
reconcile's roster reads event dates (`ROSTER_QUERY`); the site has its
own query for undated death events (`UNDATED_DEATHS_QUERY`, #354) and
still shows nowhere the undated sources of a dated death (#624); and
extraction has put `cause_of_death` and an age on the event rather than
the person (#592's follow-up).

Parenthood has one form. `father`, `mother` and `child` cannot say that
a parent is adoptive or foster, so a source naming an adoptive father
beside a register naming the birth father would be listed as a
disagreement (`disagreement.stated_together`). A step or a half term has
no kinship class (ADR-0035 §3.3).

## Decision

The owner's decisions of 2026-10-24 are recorded as decided. What #658
left to this ADR is settled here on a lean where it is lean-only, and
listed with the owner questions in §9.

### 1. What it replaces or amends

| Passage | Line |
|---|---|
| ADR-0020: the #345 and #577 header lines; §2's "died, nothing else known … is simply a death event" and its #345 paragraph; §4's #577 bullet and its union-bounds bullet's "A partner's own `death` event"; Ruling 3; Consequences' "and undated death events" | superseded or amended (§2, §5); §2's list of death evidence stays, apart from Q1 |
| ADR-0021: the #323 header line, and its amendment's "a DEAT that states the death … is a `death` event" | superseded (§2, Q4) |
| ADR-0023: the critic's fixed category list | amended: gains `vital_not_on_person` (§3) |
| ADR-0033: the #547 parents row | amended: parents compared within one kind, and an unknown value set aside (§4) |
| ADR-0035: In brief (the "Witwer" bullet and "Everyone in an event gets a role"); the #409 amendment's "A birth or death with no principal …"; §3.1's three death rows; §3.2's births and deaths, its role list; §3.3; §3.5's and §3.6's tables; §5's "a death's principal is not its informant"; §6's DEAT row; §7's rule 9 and the "one participant" row | superseded or amended (§2 to §4, §7) |
| ADR-0036: the #410 header line; §1's `ended_by` row and its death bullet; §2's "Witwer der Cäcilie" row; §3's "A death stays its principal's own"; §5's `DIV Y` row (the undated divorce stays an event); §6's partner's death | superseded or amended (§2, §5) |
| ADR-0039 §10.1(c): `main_subject` → "Rudolf's death", in the (c) paragraph and in option (c4) | amended: `main_subject` → Rudolf (§2.7) |
| MODELING.md: "Core classes", "Uncertainty and time", "Occupations, causes, counts", "Kinship", "Marriages, residences and other periods", "Identity and reconciliation", "Contradictions", "Living persons and privacy" | amended by dated paragraphs |
| docs/EXAMPLES.md: "Widow and 'the late' markers" | amended; follow-up A adds the new cases' rows |

**The extraction prompts' death and birth rules**, which the build
changes and this draft does not: `CHUNK_PROMPT` ("the late", the widow
markers, the events paragraph's births, deaths, `parent` and
`informant`, the union-end bullet, kinship), `ENTITY_MANIFEST_PROMPT`
(births and deaths in its entity list, its marker rule) and, in
`EXTRACTION_PROMPT`, only its union-end sentence and the roles and kinds
it renders from the manifest.

**Related issues.** #610's `principals_apart` no longer meets births or
deaths, and still applies to other events. #592's follow-up (a cause of
death and an age put on the event) is settled by this ADR. #624 closes
as superseded when the build lands, its criteria becoming this ADR's for
the site (§6).

### 2. Deaths

#### 2.1 The properties

| Property | Subject | Value | Notes |
|---|---|---|---|
| `date_of_death` (P570) | a person | a time, or unknown value (§2.2) | with `earliest_date`, `latest_date`, circa, as any date |
| `place_of_death` (P20) | a person | a place | |
| `cause_of_death` (P509) | a person | monolingual text, verbatim | |
| `burial_place` (P119) | a person | a place (unknown value: Q3) | `point_in_time` the burial's own date (Q3) |
| `age_at_death` | a person | a string, verbatim | a statement (Q1's lean); nearest Wikidata form: the qualifier *age at event* (P3629) |

A person is an entity of kind `human` or `unclassified_entity`. Each
statement has its own reference, to the artifact and its fragment, and
a pipeline write also cites its extraction run (rules 1 and 2). A
person may have many of each, side by side, none preferred (rule 6).

#### 2.2 The unknown value

- Only `date_of_death` takes it (Q3 asks whether `burial_place` does),
  and the manifest marks it as taking one.
- It is used only where a source says the person died and gives no
  limit, and only where that source's statement of the death gives no
  other death claim (no date, place, cause or burial), as `gedcom.died`
  does for a death event today. A hypothetical undated "verstorben in
  Flörburg" gives `place_of_death` alone.
- Examples, both hypothetical: a GEDCOM `1 DEAT Y` (the GEDCOM export,
  `tests/fixtures/example-family.ged`, dates every DEAT), and "the late
  Felix" in an undated note.
- It is never used for a blank field, "d. ?" or "date unknown": these
  give no claim.
- It carries no date qualifiers: a source that gives a limit gives the
  bounded form (§2.3) instead.
- It carries an excerpt, the words that state the death, and is refused
  without one (Q10, lean). The critic's `excerpt_does_not_state` (#607)
  judges that excerpt as any other.

GEDCOM DEATs that state the death without a claim of their own are Q4.

#### 2.3 The bounded form

A marker that states a death before the document ("the late", "der
selige", "покойного") gives a `date_of_death` bounded by the document's
own stated date (its `inception`, ADR-0039): the bound is the latest
date that `inception` allows, at its precision and never narrower, and
it is both the value and the `latest_date`.

- The 1958 letter: 25 December 1958, as decided; the universe already
  records it.
- The 1905 certificate's "dessen verstorbener Ehefrau Cäcilie": 15
  December 1905 with `latest_date` 15 December 1905, beside the
  gravestone's and the GEDCOM export's 1902.
- Hypothetical: "der selige Felix" on the postcard, whose `inception` is
  "before 19 August 1938", gives 19 August 1938 with `latest_date` 19
  August 1938, beside the GEDCOM export's 1931. A document dated only
  "1890s" gives that decade, at decade precision, as both.
- A document whose own date is open at the late end ("after 1945"), or
  that states none, gives the unknown value.
- A marker adds nothing where the same source dates or places that
  death itself: the compensation file's "seine verstorbene Mutter Ida …
  1946 verstorben" gives only the 1946 date (and `mother`).

#### 2.4 A widow's or widower's marker that names the late spouse

It gives three statements, each cited to the marker:

- the marked person's `civil_status` `widowed`, with `point_in_time` the
  date at which the source presents it as holding;
- the late spouse's own `date_of_death`, bounded by that same date
  (Q7, lean), or of unknown value in an undated document;
- the union, `ended_by_death_of` that spouse (Q2).

A marker that names no spouse ("Löbig, Ursula, Wwe.") gives the civil
status alone. "His late wife" with no widowhood marked (the 1905
certificate's "dessen verstorbener Ehefrau Cäcilie") gives her bounded
death and the marriage, and no union end (Q12, lean).

#### 2.5 What the 1909 burial register gives

This is the ADR's form; the universe still records the "Witwer" line as
a retired `spouse` claim, which follow-up A migrates. Each statement
cites its own line:

- Eugen: `date_of_death` 16 March 1909 and `place_of_death` Flörburg
  ("Gestorben: 16. März 1909 in Flörburg."); `cause_of_death`
  "Wassersucht", verbatim; `age_at_death` "73 Jahre" (Q1); `burial_place`
  Friedhof Flörburg with `point_in_time` 20 March 1909 ("Beerdigt: 20.
  März 1909.").
- From "Witwer der Cäcilie Löbig geb. Tarle": Eugen's `civil_status`
  `widowed` at 16 March 1909; Cäcilie's `date_of_death` 16 March 1909
  with `latest_date` 16 March 1909 (on Q7's lean), beside her 1902; and
  their marriage, `ended_by_death_of` Cäcilie.

#### 2.6 Informants and witnesses

The informants and witnesses of a death are not extracted. Whether a
record states its own informant as a claim about itself is Q6.

#### 2.7 ADR-0039 §10.1(c)

Its `main_subject` → "Rudolf's death" names a death item, which this ADR
removes, so the dated line there reads `main_subject` → Rudolf. #646
(the ADR-0039 §10 build) has a unit test with the old value: the
orchestrator changes it there, or, if #646 lands first, build B
migrates it.

### 3. Births

- `date_of_birth`, `place_of_birth`, `father` and `mother`, each cited.
  Gustav's birth register extract (`birth-extract-1938`) gives Gustav a
  `date_of_birth` of 7 January 1919 and a `place_of_birth` of Flörburg,
  with `father` → Sebastian and `mother` → Hildegard. Its informant
  (Sebastian, who "zeigte an") is not extracted.
- "Born, nothing else known" needs no statement.
- **The `parent` role is retired** as the `spouse` property was:
  `object_has_role` no longer accepts it, and no prompt offers it. The
  item stays in the vocabulary. `informant`: Q6.
- **Validation refuses an entity of kind `birth` or `death`**, and a
  reference typed as one (a `LocalRef`'s `instance_of`). §7 says where,
  and how the refusals are counted. The classes stay, for events a
  curator makes (Q5), and are marked as kinds no extraction makes.
- **The critic checks that a stated birth or death reaches its person**:
  a new category, `vital_not_on_person` (lean name), for a birth or a
  death the page states whose date and place no claim gives to that
  person. It reads the numbered transcription the critic reads (#607).
  Its findings stay advisory (ADR-0013, ADR-0023), and are counted with
  the critic's categories; the two births lost on one form page are the
  case it is for.

### 4. Kinds of parenthood

- **The qualifier.** `parentage` (an item) on `father`, `mother` and
  `child`, with the values `parentage_birth`, `parentage_adoptive` and
  `parentage_foster`, labelled "birth (parentage)", "adoptive
  (parentage)" and "foster (parentage)" (lean), since the `birth` class
  owns the label "birth" and no two items share a label (MODELING.md,
  "Controlled vocabulary values"). Nearest Wikidata form: *type of
  kinship* (P1039) on a father or mother statement, with an adoptive or
  foster value; the build records the ids.
- **When it is set:** only where the source's words or tags state the
  kind ("Adoptivvater", "leiblicher Vater", "birth mother", "foster
  mother", a PEDI tag), never inferred. A register's parents carry no
  kind, since being a register words none (lean). A parent with no kind
  stated is compared as a birth parent is today.
- **The word** stays verbatim in `kinship_type`, which `father`,
  `mother`, `child` and `sibling` may now carry as well as `relative`.
- **Dates.** An adoption's date is a `start_time` on each adoptive
  parent statement. A fostering takes a `start_time` and an `end_time`.
  There is no adoption event, unless a source states something that has
  no other place to go. The court that granted an adoption is the
  order's `issued_by` (lean), so it has a place.
- **Example** (hypothetical, in the universe's style; follow-up A adds
  the case): a 1950 adoption order of the Amtsgericht Flörburg, by which
  Leopold and Agnes Löbig adopt a boy whose own birth register names his
  birth parents. The order gives the boy `father` → Leopold and `mother`
  → Agnes, each with `parentage` adoptive, a `start_time` of the order's
  date, and the order's own word ("an Kindes Statt angenommen", say) in
  `kinship_type`. The register gives his birth parents, with no kind
  stated. The report lists none of them as disagreeing.
- **Disagreement.** Parents are compared within one kind, so a birth
  father and an adoptive father never disagree. A pair is set aside
  when any non-deprecated statement gives either value a kind other
  than birth.
- **Site.** The kind shows beside the value. The person page shows
  qualifiers already (`qualifier_span`); the build adds a test.
- **Family queries** (`queries/kinship.rq`), which read `kinship_type`
  words through an inverse table, no `kinship_class`, and derive
  siblings from any shared parent:
  - its term table gains the step and half words ("stiefvater",
    "stiefmutter", "stiefsohn", "stieftochter", "stiefbruder",
    "stiefschwester", "halbbruder", "halbschwester", and the English step
    and half terms) with their inverses; reading `kinship_class` instead
    stays ADR-0035 §3.3's pending reader;
  - its parent step reads `parentage`: adoptive paths are labelled
    ("adoptive father", "grandparent through adoption"; lean), and no
    sibling or longer path uses a foster link.
- **Step and half** (hypothetical): a letter's "mein Stiefvater" gives
  `relative` with `kinship_class` `kin_step_parent` and `kinship_type`
  "Stiefvater". New classes: `kin_step_parent`, `kin_step_child`,
  `kin_step_sibling`; nearest Wikidata form for the first, *stepparent*
  (P3448). A "Halbbruder" gives `sibling` with `kinship_type`
  "Halbbruder".
- **Clustering** reads kin of every kind, since a parent is the same
  person either way (lean).
- **Deferred**, each until a scratch run meets a source that carries it
  (run 3 has none): the GEDCOM `PEDI`, `_FREL`, `_MREL` and `ADOP`
  mapping (counted meanwhile); guardians; godparents. A GEDCOM family
  with no `MARR` stays a partnership for now.

### 5. Privacy

- `PERSON_VALUE_PROPERTIES` gains `ended_by_death_of`, and `informant`
  if it is ever a property (Q6).
- `ended_by_death_of` stays out of `backup.UNION_BOUNDS`. Were it there,
  `read_dump` would record the partner as a bound of the union (its
  `event not in deaths` test lets a person through), and
  `nonprivileged_exclusions` would drop a released partner with
  `BOUND_LEFT_OUT` whenever the union is dropped. The partner follows
  the person rule (ADR-0036 §6) through `PERSON_VALUE_PROPERTIES` alone.
- `PERSON_EVENTS` keeps `birth` and `death`, so the participants of an
  event a curator makes are still assessed.
- `DEATH_EVIDENCE_PROPERTIES` keeps its four properties; Q1 decides
  whether `age_at_death` joins them. An unknown value is a truthy value,
  so the check releases its person, as the site's `?deceased`
  (`staticsite.PEOPLE_QUERY`, `EXISTS { ?person wdt:<date of death> []
  }`) and the export's `backup.PERSON_FACTS_QUERY` with
  `_DEATH_BINDINGS` already read it. It counts only because a source
  stated the death (§2.2), and the build tests it live. A deprecated
  unknown value, or a "no value", releases no one.
- `ended_by_death_of` is not death evidence on its own (Q9, lean): the
  partner's own death statement, which the marker now always gives,
  is.
- If `age_at_death` becomes a statement (Q1), `PERSON_SUBJECT_PROPERTIES`
  gains it, which `read_dump`'s `subject_links` reuse.
- Validation refuses `date_of_death`, `place_of_death`,
  `cause_of_death`, `burial_place` and `age_at_death` on a subject that
  is neither `human` nor `unclassified_entity`, and `date_of_birth` and
  `place_of_birth` too (Q11, lean).
- Events a curator makes: Q5.
- ADR-0028 (reserved; #67, #120) will read the same death evidence.

### 6. Display

- **Person page.** Each `date_of_death` statement with its own sources;
  an unknown value shows as "died (date not stated)". The privacy
  filter decides who is shown, as today.
- **#624's criteria.** Each source shows what it says beside the dated
  entry. In the universe, Rudolf's certificate and the 1958 letter give
  two dated statements (6 March 1941, and the bound 25 December 1958),
  shown side by side, each with its source. The unknown-value display is
  tested on a row of its own (follow-up A's case).
- **Timeline.** `person-timeline.rq` lists only dated rows, so an
  unknown value needs its own reader, under "No date stated". A burial
  is placed by its `point_in_time`.
- `UNDATED_DEATHS_QUERY` and its entries go, and with them the
  undated-death display #623 built.
- **Places map.** It gains `place_of_birth`, `place_of_death` and
  `burial_place` (lean), since a death's place no longer reaches it
  through an event's `location`.

### 7. Where each check sits

- **A `birth` or `death` entity in a manifest is dropped alone** where
  model output is first read (`ChunkedClaimExtractor._parse_entity_manifest`,
  `pipeline/chunking.py`), and its claims rejected under a reason of
  their own and counted; it never fails the manifest turn (Q8, lean).
  Today an unknown kind is a manifest problem, and `_first_turn` accepts
  a parse only with none, so one stray `death` entity would fail both
  attempts and send the document to line-range segmentation with no
  manifest, which a death certificate makes likely.
- `extract.salvage_valid_claims` and `claims.validate_against_manifest`
  refuse a claim whose reference names one of these kinds, counted
  under the same reason.
- ADR-0039 §10.1(b)'s artifact-kind check (#646) treats an artifact
  kind as an unknown kind, which fails the turn. The two stay separate
  marks (lean); Q8 asks whether they should behave alike.
- The subject rule for death and birth properties is a manifest field
  the loader reads (`Acceptance`, `model/schema.py`).
- The writer refuses the same things for a batch it is given without
  validation (`writer.pre_plan_refusals`).

### 8. The readers that change

Build B (the PR's follow-up) carries them; in brief:

- **model** (`manifest.yaml`, `schema.py`, `bootstrap.py`): the
  unknown-value mark, the subject kinds, `parentage` and its values,
  `ended_by_death_of`, the step classes; `object_has_role` without
  `parent` (and `informant`, Q6); `ended_by` without `death`; the
  descriptions of `kinship_type`, `age_at_death`, `principal` and
  `marriage`; `birth` and `death` marked as kinds no extraction makes;
- **claims and validation** (`claims.py`): an unknown-value type, the
  refusals of §2, §3, §5 and §7, the principal-and-informant check gone,
  new reject reasons;
- **prompts** (§1's constants);
- **GEDCOM** (`pipeline/gedcom.py`): `died` and `death_event` give way to
  an unknown-value `date_of_death` citing the DEAT (Q4), a `BURI`
  without a place as Q3 settles;
- **critic** (`pipeline/critique.py`): `vital_not_on_person`, and rows
  that show an unknown value;
- **clustering** (`pipeline/clustering.py`): `ONE_PER_PERSON`, the birth
  and death principal pass and link go (a `VERSION` bump);
  `principals_apart` stays for other events; dates skip an unknown
  value; `ended_by_death_of` is not lifted (lean), since the couple
  already key the union;
- **adjudication, anomalies, review CSV, report**: an unknown value
  shown, skipped or counted; `parentage` shown;
- **writer** (`pipeline/writer.py`): an unknown value as a `somevalue`
  snak; `_find_matching_statement` compares snak types, since today it
  compares `mainsnak.datavalue.get("value")` alone, which neither an
  unknown value nor a "no value" has; `unsupported_dates` skips it;
  `pre_plan_refusals` refuses what validation refuses;
- **privacy and the export** (`core/privacy.py`, `backup.py`): §5;
- **site** (`staticsite.py`) and **queries** (`person-timeline.rq`,
  `places-map.rq`, `kinship.rq`): §4, §6;
- **disagreement** (`disagreement.py`, `disagreement-report.rq`): an
  unknown value set aside and never listed; parents within one kind;
- **reconcile** (`ROSTER_QUERY`): an unknown value; Q5's event arms.

### 9. Owner questions, with leans

Until the owner answers, the leans stand; the owner's merge with them
answered accepts this ADR.

1. **`age_at_death`.** Lean: a string statement on the person, verbatim,
   with its own reference, as the universe records "Alter: 73 Jahre"; it
   needs no date of death to hang on, and it is death evidence (ADR-0020
   §2's "any death-presupposing assertion"), joining
   `DEATH_EVIDENCE_PROPERTIES` (and so `EVENT_DEATH_PROPERTIES`, its
   alias), `PERSON_SUBJECT_PROPERTIES` and the written-out arms of
   `PEOPLE_QUERY` and `PERSON_FACTS_QUERY`. Alternatives: (a) a statement
   that is person evidence only, which withholds someone known only by
   "Alter: 73 Jahre" and departs from ADR-0020 §2; (b) a qualifier on
   `date_of_death`, as the manifest and MODELING.md say today, which then
   needs a `date_of_death`, dated or unknown, to sit on. Either statement
   form rewrites `privacy.py`'s comment.
2. **How a union ends by a death.** Lean: a new property
   `ended_by_death_of` (union → `human` or `unclassified_entity`), with
   `ended_by` no longer accepting `death`; where the batch names the
   couple, validation checks that the value is one of them.
   Alternative: `ended_by` with a person value, one property with two
   kinds of value, on which every reader of `ended_by` branches.
3. **Dating `burial_place`, and a burial with no place.** Lean: its
   `point_in_time` is the burial's own date as the source gives it, with
   its range and circa (the GEDCOM export's `2 DATE 20 MAR 1909` beneath
   `BURI`, the register's "Beerdigt: 20. März 1909"), never the
   document's date, and no `date_of_death` is derived from it. A burial
   stated without its place (today the adapter warns and writes
   nothing): lean, a `burial_place` of unknown value, dated where dated
   (`1 BURI Y` undated); this widens the unknown value to a second
   property and releases people withheld today, so the owner confirms
   it, and the scratch diff counts them by kind of source. Alternatives:
   a `date_of_burial` (P4602) for a dated burial and nothing for `1 BURI
   Y`; or no claim, as today.
4. **GEDCOM DEATs that state the death but give no claim** (`states_event`
   today: a `Y`, an unparsed DATE on a DEAT line with no value, or a PLAC
   with any value). Leans: `1 DEAT Y`, and `1 DEAT Y` with `2 DATE ?`,
   give the unknown value; a DATE that says the date is unknown ('?',
   'UNKNOWN', '(date unknown)') gives no claim, counted, as the owner
   ruled; any other DATE the grammar cannot read (hypothetical "Spring
   1900") gives no claim, counted, since its limit may exist
   (alternative: the unknown value with the DATE in the excerpt, which
   widens decision 1); a PLAC with no word (`2 PLAC ?, -`) gives no
   claim, counted, as a blank place field (alternative: the unknown
   value, since GEDCOM 5.5.1 has a PLAC with a value assert the event).
   People released today only by such DEATs become possibly living; the
   scratch diff counts them.
5. **Birth and death events a curator makes** (the classes stay). Lean:
   they release no one and date no one. `event_principal`'s release in
   `read_evidence` goes, and so does a dated birth event's release of a
   principal born only by it; a birth event's date still holds its
   participants back; the event arms of `ROSTER_QUERY` go; #577's
   set-aside stays; nothing needs the undated-death display.
   Alternative: keep both arms of `read_evidence` and the roster's event
   arms, and then, for rule 6, a narrowed `UNDATED_DEATHS_QUERY` that
   shows a curator-made undated death as "died (date not stated)".
6. **Informants.** Lean: they stay unextracted; follow-up C, an ADR-0039
   amendment issue, decides whether a record states its own informant,
   parked until a source needs it, and any property it adopts joins
   `PERSON_VALUE_PROPERTIES`. Lean: `informant` leaves `object_has_role`
   and the prompts, as `parent` does; the item stays. Alternative: keep
   accepting it, for events a curator makes and for C. `witness` and
   `officiant` stay.
7. **What bounds a widowhood marker's death.** Lean: the date at which
   the source presents the widowhood as holding, the `civil_status`
   statement's `point_in_time` (the burial register: 16 March 1909; a
   letter: its own date, as the 1958 letter's 25 December 1958).
   Alternative: always the document's own date, as "the late" takes it
   (the register's 1909, at year precision).
8. **A `birth` or `death` entity in a manifest.** Lean: dropped alone and
   counted, never failing the turn (§7). Alternative: fail the turn, as
   ADR-0039 §10.1(b) does for an artifact kind; the scratch diff then
   counts manifest fallbacks.
9. **`ended_by_death_of` as death evidence.** Lean: no; the spouse's own
   `date_of_death` from the same marker releases them. Alternative: yes,
   for its value.
10. **An unknown value with no excerpt.** Lean: refused and counted,
    since nothing else shows what the source said. Alternative: accepted
    like any claim.
11. **Birth properties on a non-person.** Lean: refused, as the death
    properties are. Alternative: the owner's death-only rule.
12. **"His late wife" with no widowhood marked** (raised by this draft).
    The 1905 certificate's "dessen verstorbener Ehefrau Cäcilie" names
    Cäcilie as Eugen's late wife. Lean: her bounded death and the
    marriage, and no `ended_by_death_of`, since only a survivor's
    widowhood is the marker that ends a union (§2.4), as a partner's
    DEAT ends none in GEDCOM (ADR-0036 §5). Alternative: the union
    `ended_by_death_of` Cäcilie, as for a widower's marker.

Lean-only choices beyond the 2026-10-24 decisions, listed with these in
the PR: the `parentage` keys and labels; a register's parents with no
kind; the court as the order's `issued_by`; the critic's category name;
the places map; clustering reading kin of every kind; `ended_by_death_of`
not lifted; adoptive path labels; the bounded form's precision rule
(§2.3); the two manifest checks kept separate (§7).

## Consequences

- One place to look. Privacy, clustering, reconcile, the site and the
  report read a death or a birth from the person's statements, and
  #577's event set-aside becomes a guard for curator-made events only.
- An undated death has one form, the unknown value, used only where a
  source states the death; the writer, the readers and the report learn
  the `somevalue` snak.
- People released in run 3 only through a death event are released
  through statements; those released only by a DEAT that Q4 leaves
  without a claim become possibly living, failing closed. The scratch
  diff counts releases both ways.
- A union ended by a death copies no date and needs no event, so the
  drift ADR-0036's alternatives warned of cannot arise.
- Adoption and fostering have a form before any source needs one, and a
  birth father and an adoptive father never disagree.
- Every extraction call and every critique misses the cache once, a
  cost shared with #646's build.

## Alternatives considered

- **Keep death and birth events (option A).** Run 3's events add no
  participant, date or place a statement lacks, and every reader pays
  for reading both.
- **An event only where a source names an informant or a witness.** It
  keeps two forms, and run 3 has no such participant.
- **"Died, nothing else known" as no claim.** A source's statement of
  the death would then be lost, against rule 5, and the person withheld
  though a source says they died.
- **A stored "deceased" flag.** ADR-0020 rejects one.
- **`parentage` inferred from context** (a later surname, an order's
  existence). Rule 5 forbids narrowing; only a source's words or tags
  set a kind.
- **An adoption event.** Its date and court fit the parent statements
  (`start_time`) and the order (`issued_by`); an event stays available
  for what has no other place.
