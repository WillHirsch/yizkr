# ADR-0040: Deaths and births are sourced statements on the person, and a parent's kind is stated only where the source states it

Status: **accepted on the owner's merge** (owner decisions of
2026-10-24 on #658, and the owner's answers to its questions of the
same day, recorded as #658's two newest comments). Like ADR-0024,
ADR-0025 and ADR-0031, it records decisions the owner has made, so the
owner's merge of the PR that adds it is its acceptance (AGENT_PROTOCOL
§8, §9). Every owner question is answered (§9). The owner accepted Q1,
Q3, Q4, Q7, Q8, Q10, Q11, Q12 and the lean-only list on their leans;
amended Q2; reversed Q9; and decided Q5 and Q6.

It reverses ADR-0020 §2's death event, ADR-0021's #323 death event,
ADR-0035 §3.2's birth and death events and ADR-0036 §1's `ended_by` →
death. It amends ADR-0023, ADR-0033, ADR-0039 §10.1(c) and MODELING.md
(§1). Each passage it reverses or amends keeps its text, followed by a
line dated 2026-10-24 that names this ADR.

ADR-0007 needs no line. Its roster reads birth and death years, which
come from statements on the person (and, from build B on, through the
shared derivation of §2.4). Its #610 amendment (`principals_apart`)
reads event principals in general, which still serves a deportation or
an imprisonment.

Its build is follow-up B (§8), which lands with follow-up A's fixture
cases. Until B lands, the code does what the passages it reverses say.
Every dated line this ADR adds, and every MODELING.md line dated
ADR-0040, describes the model from that build on.

## In brief

- **A death is a set of sourced statements on the person.** No item
  stands for the death. The burial register gives Eugen, each statement
  cited to its own line:
  - a `date_of_death` of 16 March 1909;
  - a `place_of_death` of Flörburg;
  - a `cause_of_death` "Wassersucht";
  - an `age_at_death` "73 Jahre";
  - a `burial_place` of Friedhof Flörburg, dated 20 March 1909.
- **A source's statement of a death with no date is a `date_of_death`
  of unknown value** (Wikibase "some value"), beside any place, cause or
  age the source gives. The one exception: a widow marker that names
  the late spouse gives no death statement for them (§2.2, §2.5).
  - Examples: a GEDCOM `1 DEAT Y` (hypothetical), or "the late Felix",
    in an undated note or a dated letter alike.
  - A blank field, "d. ?" or "date unknown" gives no claim.
- **"Died by" dates are derived, never stored** (Q5). A view works out
  that "the late X" died no later than the document's own date, and
  that a widow's late husband died no later than the date the source
  presents her as widowed. One shared derivation does this for the
  person page, the timeline, the disagreement report, clustering and
  reconcile. The 1958 letter's widow phrase so gives Rudolf "died by 25
  December 1958 (derived)", at Nina's widowhood, beside the
  certificate's 6 March 1941.
- **A union ended by a death points to the partner who died**
  (`ended_by_death_of`). It is written only where a source specifically
  states that the couple were married when that partner died: a
  survivor's widow or widower word naming the late spouse ("Witwer der
  Cäcilie Löbig geb. Tarle"), or a death record's "Ehefrau des Eugen".
  Where the end is only implicit, views derive it and nothing is written
  (Q2). The link is evidence that the partner died (Q9).
- **A birth is the same.** `date_of_birth` and `place_of_birth` go on
  the child, and its parents are `father` and `mother`, each cited. No
  item stands for the birth, and the `parent` role is retired.
- **Officials and witnesses with no other role are left out** (Q6). A
  person who only certifies, registers, officiates, examines, reports
  or witnesses a record is `extract` false, unless the same record
  makes them kin of someone it is about, or a party to it. Sebastian,
  who reports Gustav's birth, stays as his father.
- **A parent's kind is stated only where the source states it.** A new
  qualifier, `parentage`, says birth, adoptive or foster, set only where
  the source's words or tags say so; the word itself stays in
  `kinship_type`. A birth father and an adoptive father never disagree.
- **Privacy fails closed.**
  - The new person-valued property joins the person-value set.
  - Validation refuses a death or birth property on anything but a
    person.
  - An unknown-value `date_of_death` is death evidence, because a
    source stated the death.
  - `age_at_death` is death evidence (Q1).
  - A union `ended_by_death_of` a person releases that person (Q9).
  - A death event a curator makes still releases its principal (Q5).

## Context

Today a death can be recorded in two places, and so can a birth:

- **Statements on the person:** `date_of_death`, `place_of_death`,
  `cause_of_death` and `burial_place`; `date_of_birth` and
  `place_of_birth`, with `father` and `mother`.
- **An event item:** a `death` or a `birth` with the person as
  `principal` (ADR-0035 §3.2), parents as `parent` and an informant as
  `informant`. Four rulings make one:
  - ADR-0020 §2 makes "died, nothing else known" a death event;
  - ADR-0021's #323 amendment makes `1 DEAT Y` one;
  - ADR-0035 §3.1 makes "the late" and a widow's or widower's marker
    one;
  - ADR-0036 §1 ends a union by `ended_by` → the partner's death event.

Scratch run 3 shows that the event adds nothing. These aggregates come
from #146 and were already shared with the owner:

- 61 death events, in which every participant is the principal: no
  informants, no witnesses;
- 128 death dates on persons and 50 on events, with 17 people dead only
  through a death event;
- 19 birth events: the 13 that carry a date or a place all repeat the
  person's own, and 6 are empty;
- two births on one form page lost;
- no adoption or foster wording.

With two places to look, every reader reads both:

- privacy reads a death event's principal (`core/privacy.py`,
  `event_principal` and `read_evidence`; ADR-0020 as amended by #345),
  and has to stop an event being taken for a person on its own death
  statements (#577, `EVENT_DEATH_PROPERTIES`);
- clustering moves an event's date onto its principal (`ONE_PER_PERSON`
  and the principal pass in `_nodes`);
- reconcile's roster reads event dates (`ROSTER_QUERY`);
- the site has its own query for undated death events
  (`UNDATED_DEATHS_QUERY`, #354), and still shows nowhere the undated
  sources of a dated death (#624);
- extraction has put `cause_of_death` and an age on the event rather
  than on the person (#592's follow-up).

Parenthood has one form. `father`, `mother` and `child` cannot say that
a parent is adoptive or foster, so a source naming an adoptive father
beside a register naming the birth father would be listed as a
disagreement (`disagreement.stated_together`). A step or a half term has
no kinship class (ADR-0035 §3.3).

## Decision

The owner's decisions of 2026-10-24 are recorded as decided. What #658
left to this ADR is settled here. Its lean-only choices are accepted
(owner, 2026-10-24), and §9 records each question with its answer.

### 1. What it replaces or amends

| Passage | Line |
|---|---|
| ADR-0020: the #345 and #577 header lines; §2's "died, nothing else known … is simply a death event" and its #345 paragraph; §4's #577 bullet and its union-bounds bullet's "A partner's own `death` event"; Ruling 3; Consequences' "and undated death events" | superseded or amended (§2, §5). §2's list of death evidence stays, gaining `age_at_death` (Q1) and the value of `ended_by_death_of` (Q9) |
| ADR-0021: the #323 header line, and its amendment's "a DEAT that states the death … is a `death` event" | superseded (§2, Q4) |
| ADR-0023: the critic's fixed category list | amended: gains `vital_not_on_person` (§3) |
| ADR-0033: the #547 parents row | amended: parents are compared within one kind, an unknown value is set aside, and derived bounds are compared through the shared derivation (§2.4, §4) |
| ADR-0035: In brief (the "Witwer" bullet and "Everyone in an event gets a role"); the #409 amendment's "A birth or death with no principal …"; §3.1's three death rows; §3.2's births and deaths, its Standesbeamter bullet (Q6) and its role list; §3.3; §3.5's and §3.6's tables; §5's "a death's principal is not its informant"; §6's DEAT row; §7's rule 9 and the "one participant" row | superseded or amended (§2 to §4, §7) |
| ADR-0036: the #410 header line; §1's `ended_by` row, its death bullet and its event bullet (Q6); §2's "Witwer der Cäcilie" row; §3's "A death stays its principal's own"; §5's `DIV Y` row (the undated divorce stays an event) and its partner's-DEAT row; §6's partner's death | superseded or amended (§2, §5) |
| ADR-0039 §10.1(c): `main_subject` → "Rudolf's death", in the (c) paragraph and in option (c4) | amended: `main_subject` → Rudolf (§2.9) |
| MODELING.md: "Core classes", "Uncertainty and time", "Occupations, causes, counts", "Kinship", "Marriages, residences and other periods", "Identity and reconciliation", "Contradictions", "Living persons and privacy" | amended by dated paragraphs |
| docs/EXAMPLES.md: "Widow and 'the late' markers" | amended. Follow-up A adds the new cases' rows, removes the universe's two stored bounds (Rudolf's "before 25 December 1958" and `mr-van-t-nijks`'s "before 24 January 1952"), and updates the REFINEMENTS comment and the "Refinement, not contradiction" row |

**The extraction prompts** are changed by the build, not by this
draft:

- `CHUNK_PROMPT`:
  - "the late";
  - the widow markers (`pipeline/chunking.py`, around :260-263);
  - the events paragraph's births, deaths, `parent` and `informant`
    (around :367-377);
  - the wedding bullet (around :384-387);
  - the union-end bullet (around :388-392);
  - the residence rule's "the informant/applicant on a certificate"
    (around :400-402), for Q6 (a);
  - kinship.
- `ENTITY_MANIFEST_PROMPT`:
  - the kinship-word rule that makes a person from a kinship word alone
    (around :719-724), for §2.8's unnamed reporter;
  - births and deaths in its entity list;
  - its marker rule (around :617-621);
  - its boilerplate rule (around :865-873, with `claims.py` around
    :750-759).
- `EXTRACTION_PROMPT` (`pipeline/extract.py`):
  - its union-end sentence (around :133-138);
  - its "Dating a period" sentence, whose "its wedding and its end are
    events" (around :144-145) makes a death end an event;
  - the roles and kinds it renders from the manifest.

**Related issues.**

- #610's `principals_apart` no longer meets births or deaths, and still
  applies to other events.
- #592's follow-up (a cause of death and an age put on the event) is
  settled by this ADR.
- #624 closes as superseded when the build lands, and its criteria
  become this ADR's for the site (§6).

### 2. Deaths

#### 2.1 The properties

| Property | Subject | Value | Notes |
|---|---|---|---|
| `date_of_death` (P570) | a person | a time, or unknown value (§2.2) | with `earliest_date`, `latest_date` and circa, as any date |
| `place_of_death` (P20) | a person | a place | |
| `cause_of_death` (P509) | a person | monolingual text, verbatim | |
| `burial_place` (P119) | a person | a place, or unknown value (Q3) | its `point_in_time` is the burial's own date (Q3) |
| `age_at_death` | a person | a string, verbatim | a statement (Q1); the nearest Wikidata form is the qualifier *age at event* (P3629) |

A person is an entity of kind `human` or `unclassified_entity`. Each
statement has its own reference, to the artifact and its fragment, and
a pipeline write also cites its extraction run (rules 1 and 2). A
person may have many statements of each property, side by side, none
of them preferred (rule 6).

#### 2.2 The unknown value

- **Which properties take it.** `date_of_death` takes it, and
  `burial_place` does for a burial stated without its place (Q3). The
  manifest marks both.
- **When it is given** (one rule, lean-only): a source that states a
  death but not its date gives a `date_of_death` of unknown value,
  beside any place, cause or age it gives. This holds whatever the
  document's own date (Q5: "the late X" gives X's `date_of_death` of
  unknown value, even in a dated document).
  - Examples, all hypothetical:
    - a GEDCOM `1 DEAT Y` (the GEDCOM export,
      `tests/fixtures/example-family.ged`, dates every DEAT);
    - "the late Felix" in an undated note;
    - "der selige Felix" on a dated letter;
    - "verstorben in Flörburg", which gives `place_of_death` and the
      unknown value.
  - **The one exception:** a widow's or widower's marker that names the
    late spouse gives no death statement for the spouse (§2.5). That
    includes one phrase that is both markers.
  - GEDCOM's DEAT rules (Q4) are unchanged.
- **When it is never used.** A blank field, "d. ?" or "date unknown"
  give no claim (decided).
- **It carries no date qualifiers.** The source states no limit. A
  "died by" date that a document's own date implies is derived when a
  view is built (§2.4), never stored on it.
- **It carries an excerpt**, the words that state the death, and is
  refused without one (Q10). The critic's `excerpt_does_not_state`
  (#607) judges that excerpt as any other.

**What counts as one source.** "That source's statement of the death"
is, in a GEDCOM export, the DEAT, since each DEAT carries its own
citation: `1 DEAT Y` beside a dated DEAT gives both. In a document, it
is the document. GEDCOM DEATs that state the death without a claim of
their own are covered by Q4.

#### 2.3 "The late", and a source's own date for the death

- **"The late X"** ("der selige", "покойного", "ús sillige") states a
  death and no date, so it gives the unknown value (§2.2), cited to its
  passage.
- **A marker adds nothing where the same source dates the death
  itself.** The compensation file's "seine verstorbene Mutter Ida … 1946
  verstorben" gives only the 1946 date (and `mother`).
- **A marker where the same source places the death without dating
  it** gives the place and the unknown value (§2.2). A hypothetical
  dated letter's "der selige Felix … in Peiltal verstorben" gives a
  `place_of_death` of Peiltal and a `date_of_death` of unknown value,
  which the derivation (§2.4) bounds by the letter's own date.
- **Reversed by the owner** (2026-10-24, "Agree all 5 leans", on #658's
  comment "Q5, death dates: store only what a source says"): the
  earlier decided form, in which a document's date bounds "the late" as
  a stored value with `latest_date` ("keeps today's form"; the 1958
  letter's stored 25 December 1958), no longer holds. With it goes the
  rule that the unknown value is given only where a source gives no
  limit: a dated document's limit is now derived, not stored.

#### 2.4 "Died by" dates are derived when a view is built

**One shared derivation** works out every "died by" date at view time.
The person page, the timeline, the disagreement report, clustering's
death comparison (`pipeline/clustering.py`, around :1086-1087 and
:1673-1677) and reconcile's roster (`pipeline/reconcile.py`, around
:208-215, which today reads a death date's value and precision but not
its `latest_date`) all use it.

**The rule** (worded as one rule, lean-only; deriving at view time is
Q5's, and the widowhood bound is Q7's). A death statement with no date
is bounded by its document's own date. A stated widowhood date replaces
it for the late spouse. Neither is ever narrower than the date it comes
from. An undated or open-ended document gives no bound.

- **A death statement with no date** is a `date_of_death` of unknown
  value. It died no later than its document's own date (`inception`,
  ADR-0039).
  - A hypothetical dated postcard's "der selige Felix", whose
    `inception` is "before 19 August 1938", gives Felix "by 19 August
    1938", beside the GEDCOM export's 1931.
  - A document dated only "1890s" gives "by the 1890s", at decade
    precision.
- **The late spouse of a stated union end** (§2.5) has no death
  statement. They died no later than the date at which the same batch
  presents the survivor as widowed (the survivor's `civil_status`
  `widowed`, Q7), or, where it gives none, the document's own date.
  - The burial register gives Cäcilie "by 16 March 1909", at Eugen's
    widowhood.
  - The 1958 letter's "вдова Вашего покойного брата Рудольфа" gives
    Rudolf "by 25 December 1958", at Nina's widowhood on the letter's
    date.
- **Never narrower** (rule 5). The bound is the latest date that any
  non-deprecated statement of that date allows: a `point_in_time` with
  its `latest_date`, or every `inception` the document has. It is taken
  at its precision.
- **No bound.** A document with no stated date, or one whose date is
  open at the late end ("after 1945"), gives none.
- **What the derivation reads.** Only `date_of_death` statements
  (dated, bounded or unknown) and union ends. A burial gives no "died
  by": it gives its `burial_place` with its `point_in_time` (or unknown
  value), and no death bound (Q3).

**Its inputs.** The derivation is a pure function over claims, or query
rows, with their references. It reads no wiki of its own.

- Clustering runs before the write and reads no wiki (ADR-0025 §2). It
  feeds the derivation from its batches: each document's `inception`,
  the batch's `civil_status` `widowed`, and the union end.
- The site, the disagreement report and reconcile's `fetch_roster` feed
  it from the graph.

**What a derived date is not:**

- It is labelled as derived wherever it shows ("died by 25 December
  1958 (derived), per the 1958 letter").
- It is never written, stores nothing, and is never death evidence of
  its own. The statement it derives from is the evidence.
- It is compared, as a range open at its early end, in the
  disagreement report and clustering, beside the statements it comes
  from.

#### 2.5 A source that states a union ended by a partner's death

**Q2's test.** `ended_by_death_of` is written only where a source
specifically states that the couple were married when that partner
died. Where the end is only implicit (no other ending of the marriage,
plus a partner's death), views derive it and nothing is written
(§2.6).

**Forms that meet the test:**

- a survivor's widow or widower word that names the late spouse:
  - "Witwer der Cäcilie Löbig geb. Tarle" (the burial register);
  - "вдова Вашего покойного брата Рудольфа" (the 1958 letter);
  - "Se is widdo sûnt ús sillige heit stoarn is" (the 1952 letter);
- a death record that calls the deceased the survivor's spouse, as a
  hypothetical death register of Cäcilie's that calls her "Ehefrau des
  Eugen": it states that they were married when she died (owner,
  2026-10-24);
- hypothetically, a remarriage entry's "Witwer der …", and a margin
  note "Ehe durch den Tod des Ehemannes aufgelöst".

**Forms that do not meet it,** whose end views derive:

- "his late wife", the 1905 certificate's "dessen verstorbener Ehefrau
  Cäcilie" (Q12);
- Ida's card's "Husband: Felix LÖBIG, died 1931";
- a GEDCOM family plus a partner's DEAT;
- a marriage and a death stated in two documents.

**What a widow's or widower's marker that names the late spouse gives**
(Q5), each statement cited to the marker:

- the survivor's `civil_status` `widowed`, with `point_in_time` the
  date at which the source presents it as holding;
- the union, `ended_by_death_of` that spouse.

It gives no death statement for the spouse: the end is the evidence
(Q9), and the spouse's "died by" date is derived (§2.4).

- **Where one phrase is both markers** (the 1958 letter's "вдова Вашего
  покойного брата Рудольфа"; the 1952 letter's "widdo … ús sillige
  heit"), the union end alone stands for the death: no unknown-value
  `date_of_death` is added beside it. The survivor's civil status is
  given, as for any widow marker.
- **A marker that names no spouse** ("Löbig, Ursula, Wwe.") gives the
  civil status alone.
- **"His late wife" with no widowhood marked** (the 1905 certificate)
  gives her unknown-value `date_of_death` ("the late", §2.3) and the
  marriage, and no union end (Q12). Views derive the end (Q2).
- **The guard** (lean-only, raised with Q9's answer). Validation
  refuses an `ended_by_death_of` whose value the same batch calls
  `widowed`.
  - It needs no excerpt match, so it catches a slip even when the model
    quotes the two claims a little differently.
  - It loses a correct link only in a rare record of a widower who later
    died in a second marriage (hypothetically, Eugen as "Witwer der
    Cäcilie", and a second marriage ended by his own death). That fails
    closed.
  - The advisory critic checks it too.
  - **What a refusal costs.** Under Q5 a widow marker gives no death
    statement. So when the guard refuses a link, the late spouse keeps
    no death evidence from that marker, and stays possibly living
    (fails closed).
  - **What it cannot check.** A death record's "Ehefrau des Eugen"
    need come with no `civil_status` `widowed`, so the guard may have
    nothing to compare. The critic checks that the value is the person whose death
    the record states.

#### 2.6 The derived end of a union

**Stated ends.** A union with no non-deprecated `ended_by` or
`ended_by_death_of` shows "no source states how it ended", with each
partner's death statements and their sources.

**Each partner's death span** is the hull of all their non-deprecated
dated death statements (derived "died by" bounds included), each at its
stated bounds. It is never narrower than the statements it comes from,
and no one statement is picked (rule 6). From the spans:

- Where one partner's span lies wholly before the other's, the union
  shows "ended by X's death (derived)".
- Where the spans overlap, or only one partner has a dated death, it
  shows only "ended no later than X's death" for each partner with death
  evidence, never "ended by X's death" (rule 5).

**How it is computed.** In Python, as `disagreement.py` compares spans.
It stores nothing, and is never death evidence. The site's pages show
it; the raw `person-timeline.rq` view does not.

#### 2.7 What the 1909 burial register gives

This is the ADR's form. The universe still records the "Witwer" line as
a retired `spouse` claim, which follow-up A migrates. Follow-up A also
removes the universe's two stored bounds from phrases that are both
markers: Rudolf's "before 25 December 1958" and `mr-van-t-nijks`'s
"before 24 January 1952". It updates the REFINEMENTS comment and
EXAMPLES.md's "Refinement, not contradiction" row to match. Each statement
cites its own line:

- **Eugen:**
  - `date_of_death` 16 March 1909 and `place_of_death` Flörburg
    ("Gestorben: 16. März 1909 in Flörburg.");
  - `cause_of_death` "Wassersucht", verbatim;
  - `age_at_death` "73 Jahre" (Q1);
  - `burial_place` Friedhof Flörburg with `point_in_time` 20 March 1909
    ("Beerdigt: 20. März 1909.").
- **From "Witwer der Cäcilie Löbig geb. Tarle":**
  - Eugen's `civil_status` `widowed` at 16 March 1909;
  - their marriage, `ended_by_death_of` Cäcilie. The end is written
    because "Witwer der" states that the marriage lasted until her
    death (Q2's test).
  - Cäcilie has no death statement from this line. The derivation
    gives her "died by 16 March 1909" (Q7, Q5), beside her 1902 from
    the gravestone and the GEDCOM export.

#### 2.8 Informants, witnesses and officials (Q6, rule (a))

**Who is left out.** A person who only certifies, registers, officiates,
examines, reports or witnesses is `extract` false (left out), unless the
same record gives them another role: kin of someone the record is
about, or a party to it. A rank, a job, an address or a signature is
not another role.

**What it covers** (lean-only). The rule covers a record's apparatus
(certificates, registers, forms), and persons only.

- Organizations are untouched. The issuing office stays `issued_by`,
  and a court stays `carried_out_by`, as today's boilerplate rule's
  issuing-authority exception keeps them (`pipeline/chunking.py`,
  around :872-873; `claims.py`, around :756-757).
- A neighbour, a teacher or a doctor whom a letter names is no
  apparatus, and stays.

**Who stays.** A person with another role is extracted, with what the
record states of them: name, address, kinship. Sebastian, who "zeigte
an" Gustav's birth, stays as Gustav's `father`, with his address. His
role as informant is not recorded.

**Weddings.** A wedding's registrar and its non-kin witnesses are left
out. A kin witness or officiant stays, with that role.

**A reporter given only by role** (lean-only). A person the record gives only by
the role in which they report or certify, with no name, is not made,
even where the role word is a kinship word. So "Informant: husband"
(Florence's 1963 certificate) records nothing (owner, 2026-10-24): no
unnamed husband and no union is made.

**Refusals.** Validation and the writer refuse any claim in the batch
whose value is an `extract` false person.

**What is not added.** No cross-document matching, and no count of name
matches.

**A record's own informant.** Whether a record states its own informant
as a claim about itself is follow-up C, parked (Q6).

#### 2.9 ADR-0039 §10.1(c)

Its `main_subject` → "Rudolf's death" names a death item, which this ADR
removes, so the dated line there reads `main_subject` → Rudolf. #646
(the ADR-0039 §10 build) has a unit test with the old value. The
orchestrator changes it there, or, if #646 lands first, build B
migrates it.

### 3. Births

- `date_of_birth`, `place_of_birth`, `father` and `mother`, each cited.
  Gustav's birth register extract (`birth-extract-1938`) gives Gustav a
  `date_of_birth` of 7 January 1919 and a `place_of_birth` of Flörburg,
  with `father` → Sebastian and `mother` → Hildegard. Sebastian's role
  as its informant ("zeigte an") is not recorded (§2.8).
- "Born, nothing else known" needs no statement.
- **The `parent` role is retired** as the `spouse` property was:
  `object_has_role` no longer accepts it, and no prompt offers it. The
  item stays in the vocabulary. So does `informant` (Q6).
- **Validation refuses an entity of kind `birth` or `death`**, and a
  reference typed as one (a `LocalRef`'s `instance_of`). §7 says where,
  and how the refusals are counted. The classes stay, for events a
  curator makes (Q5), and are marked as kinds no extraction makes.
- **The critic checks that a stated birth or death reaches its person**:
  a new category, `vital_not_on_person`, for a birth or a
  death the page states with a date or a place, where no claim gives
  that person either. It reads the numbered transcription the critic reads (#607).
  Its findings stay advisory (ADR-0013, ADR-0023), and are counted with
  the critic's categories; the two births lost on one form page are the
  case it is for.

### 4. Kinds of parenthood

- **The qualifier.** `parentage` (an item) on `father`, `mother` and
  `child`, with the values `parentage_birth`, `parentage_adoptive` and
  `parentage_foster`, labelled "birth (parentage)", "adoptive
  (parentage)" and "foster (parentage)", since the `birth` class
  owns the label "birth" and no two items share a label (MODELING.md,
  "Controlled vocabulary values"). Nearest Wikidata form: *type of
  kinship* (P1039) on a father or mother statement, with an adoptive or
  foster value; the build records the ids.
- **When it is set:** only where the source's words or tags state the
  kind ("Adoptivvater", "leiblicher Vater", "birth mother", "foster
  mother", a PEDI tag), never inferred. A register's parents carry no
  kind, since being a register words none. A parent with no kind
  stated is compared as a birth parent is today.
- **The word** stays verbatim in `kinship_type`, which `father`,
  `mother`, `child` and `sibling` may now carry as well as `relative`.
- **Dates.** An adoption's date is a `start_time` on each adoptive
  parent statement. A fostering takes a `start_time` and an `end_time`.
  There is no adoption event, unless a source states something that has
  no other place to go. The court that granted an adoption is the
  order's `issued_by`, so it has a place.
- **Example** (hypothetical, in the universe's style; follow-up A adds
  the case): a 1950 adoption order of the Amtsgericht Flörburg, by which
  Leopold and Agnes Löbig adopt a boy whose own birth register names his
  birth parents. The order gives the boy `father` → Leopold and `mother`
  → Agnes, each with `parentage` adoptive, a `start_time` of the order's
  date, and the order's own word for the relation ("Adoptivvater",
  "Adoptivmutter", say) in `kinship_type`. The register gives his birth parents, with no kind
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
    ("adoptive father", "grandparent through adoption"), and no
    sibling or longer path uses a foster link.
- **Step and half** (hypothetical): a letter's "mein Stiefvater" gives
  `relative` with `kinship_class` `kin_step_parent` and `kinship_type`
  "Stiefvater". New classes: `kin_step_parent`, `kin_step_child`,
  `kin_step_sibling`; nearest Wikidata form for the first, *stepparent*
  (P3448). A "Halbbruder" gives `sibling` with `kinship_type`
  "Halbbruder".
- **Clustering** reads kin of every kind, since a parent is the same
  person either way.
- **Deferred**, each until a scratch run meets a source that carries it
  (run 3 has none): the GEDCOM `PEDI`, `_FREL`, `_MREL` and `ADOP`
  mapping (counted meanwhile); guardians; godparents. A GEDCOM family
  with no `MARR` stays a partnership for now.

### 5. Privacy

**Person values and union bounds.**

- `PERSON_VALUE_PROPERTIES` gains `ended_by_death_of`, and `informant`
  if it is ever a property (follow-up C).
- `ended_by_death_of` stays out of `backup.UNION_BOUNDS`. Were it
  there, `read_dump` would record the partner as a bound of the union
  (its `event not in deaths` test lets a person through), and
  `nonprivileged_exclusions` would drop a released partner with
  `BOUND_LEFT_OUT` whenever the union is dropped. The partner follows
  the person rule (ADR-0036 §6) through `PERSON_VALUE_PROPERTIES` alone.
- A partner released by the end alone is still kept when the export
  leaves the union out (`PARTICIPANT_LEFT_OUT`). The export then lacks
  the statement that released them.
- `PERSON_EVENTS` keeps `birth` and `death`, so the participants of an
  event a curator makes are still assessed.

**Death evidence on the person.** `DEATH_EVIDENCE_PROPERTIES` keeps its
four properties, and `age_at_death` joins them (Q1). It also joins the
readers that read death evidence:

- `EVENT_DEATH_PROPERTIES` (`core/privacy.py`, around :184);
- `PERSON_SUBJECT_PROPERTIES`, which `read_dump`'s `subject_links`
  reuse;
- the written-out arms of `staticsite.PEOPLE_QUERY` and
  `backup.PERSON_FACTS_QUERY`;
- `_DEATH_BINDINGS` (`backup.py`, around :273);
- `exclusion_report`'s keyword arguments to `PERSON_FACTS_QUERY.format`
  (`backup.py`, around :329-337), since the export reads death signals
  by binding name.

**An unknown value is a truthy value**, so the check releases its
person. The site's `?deceased` (`EXISTS { ?person wdt:<date of death>
[] }`) and the export's `PERSON_FACTS_QUERY` with `_DEATH_BINDINGS`
already read it that way. It counts only because a source stated the
death (§2.2), and the build tests it live. A deprecated unknown value,
or a "no value", releases no one.

**A union `ended_by_death_of` a person releases that person** (Q9,
reversed by the owner: "Sounds like proof that she died to me").

- **How it is read.** In `core/privacy.read_evidence` (around :793-825
  at 5198df2), into `LifeEvidence.died`, which the badge and the export
  combine. It is not added to `DEATH_EVIDENCE_PROPERTIES`, which reads
  the subject and would make the union a person. It is read at its
  truthy value, item values only (lean-only).
- **Its risk.** A value misplaced on the survivor releases a widow or
  widower who may be living. §2.5's guard refuses a value the same
  batch calls `widowed`, and the critic checks it.
  - A refused link costs the late spouse that marker's death evidence,
    which fails closed.
  - The guard cannot check a death record's "Ehefrau des Eugen". There
    the critic checks that the value is the person whose death the
    record states.

**Subjects.** Validation refuses `date_of_death`, `place_of_death`,
`cause_of_death`, `burial_place`, `age_at_death`, `date_of_birth` and
`place_of_birth` on a subject that is neither `human` nor
`unclassified_entity` (Q11).

**Events a curator makes** (Q5, decided). A death event releases its
principal, and a dated birth event dates its principal's birth, as
today:

- `read_evidence` (around :811-823) and `ROSTER_QUERY`'s event arms
  read them;
- the site's badge and the export read them alike;
- this follows ADR-0020 §2's "any death-presupposing assertion".

**ADR-0028** (reserved; #67, #120) will read the same death evidence.

### 6. Display

- **Person page.** Each `date_of_death` statement shows with its own
  sources, and an unknown value shows as "died (date not stated)".
  Derived "died by" dates show beside the death statements they come
  from, labelled as derived. The privacy filter decides who is shown,
  as today.
- **#624's criteria.** Each source shows what it says beside the dated
  entry. In the universe, once follow-up A removes its stored bound,
  Rudolf's certificate gives 6 March 1941, and the 1958 letter's union
  end gives "died by 25 December 1958 (derived)". The two show side by
  side, each with its source. The
  unknown-value display is tested on a row of its own (follow-up A's
  case).
- **Timeline.** `person-timeline.rq` lists only dated rows, so an
  unknown value needs its own reader.
  - The unknown value shows beside the person's dated death entry when
    a statement dates that death (#624's criterion). Hypothetical until
    follow-up A: a certificate's dated death and a GEDCOM `1 DEAT Y`,
    side by side, each with its source.
  - It goes under "No date stated" only when no statement dates that
    death.
  - A burial is placed by its `point_in_time`.
- **Unions.** The person page and the timeline show each union the
  person takes part in, with its stated end and its sources, or the
  derived end (§2.6) where none is stated.
  - Today `STATEMENTS_QUERY` shows no union.
  - `person-timeline.rq` dates a marriage only through a `spouse`
    statement's qualifiers (its qualifier arm, around :57-88), and
    reaches a union's own observation through the event arm (around
    :89-129), not its wedding, divorce or end.
  - So this lands with #520's readers or with build B.
- **Events a curator makes** (Q5). The timeline's event arm reads
  `participant` at any role, so a dated birth or death event would show
  on every participant's timeline, an informant's included. For a
  `birth` or `death` event it reads the `principal` alone, so the event
  shows on its principal's timeline only. Other events keep every
  participant.
- **The #623 display.** The undated-death display that #623 built is
  replaced (owner, 2026-10-24): an undated death is shown through the
  person's statements. The one exception (Q5, decided):
  - `UNDATED_DEATHS_QUERY` stays as it is, reading the `principal` only;
  - after build B it meets only events a curator makes, since no
    extraction or GEDCOM batch makes a death event;
  - such a death still shows as "died (date not stated)" with its
    sources on its principal's page and timeline (rule 6).
- **Places map.** It gains `place_of_birth`, `place_of_death` and
  `burial_place`, since a death's place no longer reaches it through an
  event's `location`.

### 7. Where each check sits

- **A `birth` or `death` entity in a manifest is dropped alone** where
  model output is first read (`ChunkedClaimExtractor._parse_entity_manifest`,
  `pipeline/chunking.py`). Its claims are rejected under a reason of
  their own and counted, and it never fails the manifest turn (Q8).
  Today an unknown kind is a manifest problem, and `_first_turn` accepts
  a parse only when there is none. So one stray `death` entity would
  fail both attempts and send the document to line-range segmentation
  with no manifest, and a death certificate makes such an entity likely.
- `extract.salvage_valid_claims` and `claims.validate_against_manifest`
  refuse a claim whose reference names one of these kinds, counted
  under the same reason.
- ADR-0039 §10.1(b)'s artifact-kind check (#646) treats an artifact
  kind as an unknown kind, which fails the turn. The two stay separate
  marks (Q8).
- The subject rule for death and birth properties is a manifest field
  that the loader reads (`Acceptance`, `model/schema.py`).
- The writer refuses the same things for a batch it is given without
  validation (`writer.pre_plan_refusals`).

### 8. The readers that change

Build B (the PR's follow-up) carries these changes. In brief:

- **Model** (`manifest.yaml`, `schema.py`, `bootstrap.py`):
  - the unknown-value marks on `date_of_death` and `burial_place`;
  - the subject kinds;
  - `parentage` and its values;
  - `ended_by_death_of`, whose description states Q2's test;
  - the step classes;
  - `object_has_role` without `parent` and `informant`;
  - `ended_by` without `death`;
  - the descriptions of `kinship_type`, `age_at_death`, `principal`,
    `marriage` and `author`. Today `author`'s description counts a
    signature ("who wrote, drew or made it, as the document itself
    states (a signature, a studio's stamp on a mount)"). Build B
    narrows it: a signature that only certifies or registers is no
    authorship (Q6);
  - `birth` and `death` marked as kinds no extraction makes.
- **Claims and validation** (`claims.py`), and the writer's
  `pre_plan_refusals` alike:
  - an unknown-value type;
  - the refusals of §2, §3, §5 and §7;
  - `parentage` on anything but `father`, `mother` and `child`;
  - an `ended_by_death_of` that names neither of the couple, where the
    batch names them;
  - §2.5's guard (a value the same batch calls `widowed`);
  - any claim in the batch whose value is an `extract` false person
    (§2.8);
  - the principal-and-informant check goes;
  - new reject reasons.
- **Fingerprints** (`pipeline/fingerprints.py`): `canonical_value` gains
  a print for an unknown value (a tag, no payload), with its docstring
  and a unit test, since the run diff matches claims through
  `claim_prints`.
- **Prompts** (§1's constants):
  - `CHUNK_PROMPT`'s union-end bullet drops "or by a partner's own
    `death` event";
  - `EXTRACTION_PROMPT`'s union-end sentence and its "Dating a period"
    sentence stop making every end an event;
  - the widow-marker rules end the union by `ended_by_death_of`, and
    give the late spouse no death statement (Q5); where one phrase is
    both markers, it gives the union end alone (§2.5);
  - "the late" gives the unknown value (Q5);
  - each says that a marriage plus a partner's death gives no end of any
    kind, and offers `ended_by_death_of` only for §2.5's stated forms,
    listing their words;
  - Q6 (a): the boilerplate rule widens to a person who only certifies,
    registers, officiates, examines, reports or witnesses, with "unless
    the same record gives them another role", and the events paragraph,
    the wedding bullet and the residence rule's informant clause
    follow;
  - a person the record gives only by the role in which they report or
    certify, with no name, is not made, even where the role word is a
    kinship word (§2.8).
- **GEDCOM** (`pipeline/gedcom.py`):
  - `died` and `death_event` give way to an unknown-value
    `date_of_death` citing the DEAT (Q4);
  - a `BURI` without a place gives a `burial_place` of unknown value,
    with its `point_in_time` where dated (Q3);
  - `states_event` and its census take BURI, so that BURI follows DEAT's
    rules (Q3, Q4).
- **Critic** (`pipeline/critique.py`): `vital_not_on_person`, and rows
  that show an unknown value.
- **The shared derivation** (§2.4, §2.6), a module of its own, read by
  the site, the timeline, the disagreement report, clustering and
  reconcile. Its tests:
  - Cäcilie by 16 March 1909;
  - Rudolf by 25 December 1958;
  - an undated "the late" with no bound;
  - two widowhoods in one source;
  - a union's derived end, both when the spans lie apart and when they
    overlap.
- **Clustering** (`pipeline/clustering.py`):
  - `ONE_PER_PERSON`, the birth and death principal pass and the link go
    (a `VERSION` bump);
  - `principals_apart` stays for other events;
  - dates skip an unknown value;
  - the death comparison reads derived bounds through the shared
    derivation, the late spouse's from the union end and the widowhood's
    date included. Reading the end for a death span is not "lifting" it
    as a key: `ended_by_death_of` is not lifted, since the couple already
    key the union, so the two do not conflict;
  - the derived bounds feed each node's `deaths`, and so join its
    evidence signature (`_signature`, which puts `dates(of.deaths)` in
    each node's signature; `clustering.py`, around :2017), with a test.
- **Adjudication, anomalies, review CSV, report:** an unknown value
  shown, skipped or counted; `parentage` shown.
- **Writer** (`pipeline/writer.py`):
  - an unknown value as a `somevalue` snak;
  - `_find_matching_statement` compares snak types, since today it
    compares `mainsnak.datavalue.get("value")` alone, which neither an
    unknown value nor a "no value" has;
  - `unsupported_dates` skips it;
  - `pre_plan_refusals` refuses what validation refuses.
- **Privacy and the export** (`core/privacy.py`, `backup.py`): §5. The
  `read_evidence` arm for `ended_by_death_of` is tested live.
- **Site** (`staticsite.py`) and **queries** (`person-timeline.rq`,
  `places-map.rq`, `kinship.rq`):
  - §4 and §6: the union display, the derived end and the derived
    bounds;
  - the artifact page's `ASSERTIONS_QUERY` rows, which would print an
    unknown value as its blank-node id, with a test.
- **Run evidence** (`scratchwrite.run_site`, `report.py`,
  docs/RUN_REPORT.md, docs/RUN_DIFF.md). The site stage's record, from
  `publish_site`'s summary, gains the possibly-living count and the
  releases by cause:
  - a dated `date_of_death`;
  - another death property;
  - an unknown-value `date_of_death`, by kind of source;
  - `age_at_death`;
  - an unknown `burial_place`;
  - a union `ended_by_death_of`, with a count of the people it alone
    releases;
  - the birth cutoff.

  It also counts derived bounds by kind of source. All of these are
  counted on the written graph, as disagreements are, and the report
  and the diff carry them. The links the guard refuses never reach the
  graph, so they are counted with the reject reasons. Today none of
  these tools counts possibly-living people or releases.
- **Disagreement** (`disagreement.py`, `disagreement-report.rq`):
  - an unknown value is set aside and never listed;
  - parents are compared within one kind;
  - derived bounds are compared through the shared derivation, fed
    from the graph with each death statement and its references, each
    cited artifact's `inception`, the `civil_status` `widowed`
    statements, and the union ends.
- **Reconcile** (`ROSTER_QUERY`):
  - an unknown value;
  - death dates read through the shared derivation, fed with the same
    rows as the disagreement report (each death statement and its
    references, each cited artifact's `inception`, the `civil_status`
    `widowed` statements, the union ends);
  - the event arms stay, for events a curator makes (Q5), but for a
    birth or a death event they read the `principal` alone, as the
    timeline's arm does, so a witness of a curator-made death event
    gets no date from it.

**Order and data.** B lands before the keeper run (#159), so no kept
graph holds death or birth events from extraction. No graph migration is
needed, since every scratch run resets the instance (ADR-0025). If the
keeper run came first, B would need a reader for kept events, which
this ADR does not give.

### 9. Owner questions and the owner's answers

**Every question is answered** (owner, 2026-10-24, #658's two newest
comments). Each question keeps its alternatives as the record.

**Decided before the questions:**

- `1 DEAT Y` gives a `date_of_death` of unknown value;
- a DATE that says the date is unknown ("d. ?", "date unknown") gives
  no claim;
- informants are not extracted by default.

**Decided with the answers:**

- Q2, amended: "'ended by death of' links should only be added when a
  source specifically states that the couple were married at that point
  of death; where it's implicit from absence of other ending of the
  marriage plus details of deaths this should be represented
  transitively in views";
- Q9, reversed: "Sounds like proof that she died to me";
- Q5, decided as B+C, with two earlier points reversed ("Agree all 5
  leans");
- Q6, decided as rule (a), persons only;
- two new points: a death record's "Ehefrau des Eugen" meets Q2's test;
  "Informant: husband" with no name records nothing.

1. **`age_at_death`.** *Decided (owner, 2026-10-24): the lean.*
   - **The lean.** A string statement on the person, verbatim, with its
     own reference.
     - The burial register's "Alter: 73 Jahre" gives Eugen
       `age_at_death` "73 Jahre". It needs no date of death to hang on.
     - It is death evidence, because ADR-0020 §2 counts "any
       death-presupposing assertion", and an age at death presupposes
       the death.
     - So it joins `DEATH_EVIDENCE_PROPERTIES` (and
       `EVENT_DEATH_PROPERTIES`, its alias), `PERSON_SUBJECT_PROPERTIES`,
       and the written-out arms of `PEOPLE_QUERY` and
       `PERSON_FACTS_QUERY` (§5).
   - **The risk.** The model can misplace an age. An age that a census,
     a passport or a letter gives for a living person, put in
     `age_at_death`, would release that person on that statement alone.
     The critic's `excerpt_does_not_state` is the only check on it, and
     it is advisory.
   - **Alternatives:**
     - (a) a statement that is person evidence only. It withholds
       someone known only by "Alter: 73 Jahre", and departs from
       ADR-0020 §2.
     - (b) a qualifier on `date_of_death`, as the manifest and
       MODELING.md say today. It then needs a `date_of_death`, dated
       or unknown, to sit on.
   - The statement form rewrites `privacy.py`'s comment.
   - **Lean-only (accepted):** a GEDCOM `DEAT.AGE` stays counted and
     unmapped. The alternative maps it to `age_at_death`, but only
     beneath a DEAT that states the death.
2. **How a union ends by a death.** *Decided, amended (owner,
   2026-10-24).* The owner's words: "'ended by death of' links should
   only be added when a source specifically states that the couple were
   married at that point of death; where it's implicit from absence of
   other ending of the marriage plus details of deaths this should be
   represented transitively in views."
   - **As decided.** A new property `ended_by_death_of` (union → `human`
     or `unclassified_entity`), and `ended_by` no longer accepts
     `death`.
     - It is written only where a source states the couple were married
       when that partner died (§2.5).
     - Otherwise the end is derived in views (§2.6).
     - Eugen and Cäcilie's marriage is `ended_by_death_of` Cäcilie,
       from "Witwer der".
     - Where the batch names the couple, validation checks that the
       value is one of them.
     - Reason: each property then has one kind of value.
   - **The drafted lean** wrote the link for every widow marker; the
     owner narrowed it to Q2's test.
   - **Alternative:** `ended_by` with a person value. That is one
     property with two kinds of value, and every reader of `ended_by`
     has to branch on the kind: clustering's union lifting, the
     export's `UNION_BOUNDS`, the timeline.
3. **Dating `burial_place`, and a burial with no place.** *Decided
   (owner, 2026-10-24): the lean.*
   - **Dating.** Its `point_in_time` is the burial's own date as the
     source gives it, with its range and circa: the GEDCOM export's `2
     DATE 20 MAR 1909` beneath `BURI`, the register's "Beerdigt: 20.
     März 1909".
     - It is never the document's date, because a document's date
       dates its making, not the burial.
     - No `date_of_death` and no "died by" date is derived from a
       burial (§2.4).
   - **A burial stated without its place.** A `burial_place` of unknown
     value, with its `point_in_time` where dated.
     - A hypothetical `1 BURI` with `2 DATE 20 MAR 1909` and no PLAC
       gives that; `1 BURI Y` gives it undated.
     - Reason: a stated burial presupposes the death, as ADR-0020 §2
       counts a burial place. Today the adapter warns and writes
       nothing.
     - BURI follows DEAT's rules, Q4 included.
     - It releases people who are withheld today; the scratch diff
       counts them by kind of source.
   - **Alternatives:**
     - a burial stated with no place gives an unknown-value
       `date_of_death` instead;
     - a `date_of_burial` (P4602) for a dated burial, and nothing for
       `1 BURI Y`;
     - no claim, as today.
4. **GEDCOM DEATs that state the death but give no claim.** *Decided
   (owner, 2026-10-24): the leans.* Today `states_event`
   (`pipeline/gedcom.py`) takes a DEAT as stating the death by a `Y`, by
   a DATE the grammar cannot parse (when the DEAT's own line has no
   value), or by a PLAC with any value.
   - **`1 DEAT Y` with `2 DATE ?`** gives the unknown value. Reason: a
     `Y` states the death whatever its DATE holds, unlike "d. ?", which
     the owner ruled gives no claim.
   - **Any other DATE the grammar cannot read** (hypothetical: "Spring
     1900") gives no claim, counted, since its limit may exist.
     Alternative: the unknown value, with the DATE kept as written in
     the excerpt.
   - **A PLAC with no word** (`2 PLAC ?, -`) gives no claim, counted, as
     a blank place field does.
     - So does a PLAC of placeholder words only (`2 PLAC unbekannt`):
       `_names_a_place` takes it as stating the death, while
       `_place_parts` drops it as a placeholder
       (`gedcom_placeholders.yaml`).
     - Alternative: the unknown value, since GEDCOM 5.5.1 has a PLAC
       with a value assert the event.
   - **`states_event` is unchanged** for MARR, DIV and ANUL.
   - People who are released today only by such DEATs become possibly
     living. The scratch diff counts them.
5. **Death dates, and birth and death events a curator makes.**
   *Decided (owner, 2026-10-24).*
   - **Bounds: B+C, chosen** ("store only what a source says"). The four
     options:
     - (A), as drafted: a dated document's "the late" stores its bound
       as value and `latest_date`, and a widow marker writes the late
       spouse's bounded `date_of_death`.
     - (B): unknown values everywhere, with every bound derived.
     - (C): "the late" keeps its stored bound, but the widow marker
       writes no death.
     - (B+C), chosen: "the late X" gives X's `date_of_death` of unknown
       value even in a dated document; a widow's or widower's marker
       gives the survivor's civil status and the union end, and no
       death statement for the spouse; one phrase that is both markers
       gives the union end alone; and every "died by" date is derived
       at view time by one shared derivation (§2.4).
   - **Why derived, not stored.** A stored bound needs a rank change to
     correct, while a derived bound follows the corrected date.
   - **Reversals.** This reverses two points decided earlier, the 1958
     letter's stored bound ("keeps today's form") and "the unknown value
     only where a source gives no limit" (§2.3).
   - **Release: yes, as today.**
     - A death event a curator makes releases its principal, and a
       curator-made dated birth event dates its principal's birth.
     - `read_evidence`'s arms and `ROSTER_QUERY`'s event arms read them,
       for the badge and the export alike.
     - A hypothetical undated "Death of Rahel" a curator makes, with
       Rahel as its principal, releases her.
     - Rejected alternative: such an event releases no one, which would
       depart from ADR-0020 §2.
   - **Display.**
     - The undated-death display that #623 built is replaced (owner,
       2026-10-24). The one exception: `UNDATED_DEATHS_QUERY` stays as
       it is (reading the `principal` only), and after build B it meets
       only events a curator makes. It shows "Death of Rahel" as "died
       (date not stated)" with its sources on her page and timeline,
       since rule 6 needs every source's assertion shown (PR #623:
       "only shown, never weighed").
     - The timeline's event arm reads a birth or a death event's
       `principal` alone (§6).
     - Rejected alternative: no exception, so a curator-made undated
       death would show on no page.
6. **Informants, registrars and officials.** *Decided (owner,
   2026-10-24): rule (a), persons only* (§2.8).
   - **The options:**
     - (a), chosen: a person who only certifies, registers, officiates,
       examines, reports or witnesses is `extract` false, unless the
       same record gives them another role (kin of someone it is about,
       or a party to it). A rank, a job, an address or a signature is
       not another role. Today's `author` description counts a
       signature (manifest, around :734-740); build B narrows it, so
       that a signature that only certifies or registers is no
       authorship.
     - (b): keep such a person where their name matches a kept
       person's.
     - (c): keep everyone the record names, as today.
   - **Run 3's aggregates** (#146; counts only):
     - 34 such mentions in 13 of 79 documents. 22 of them, in 10
       documents, were already `extract` false, and none had a kin
       claim.
     - 5 reached the graph, among them an `extract` false notary as a
       document's `author`.
     - One non-kin informant of a death was kept without the role, and
       their occupation and address reached the graph.
     - No informant was also kin.
     - In (b)'s terms, 14 of the 22 matched by name, every one the same
       official in a second reading of the same record.
   - **What it keeps:**
     - `informant` leaves `object_has_role` and the prompts, and the
       item stays;
     - follow-up C (a record's own informant) stays parked;
     - `witness` and `officiant` stay accepted roles, for a kin witness
       or officiant and for events a curator makes.
   - **What it does not add:** no cross-document matching and no count
     of name matches.
7. **What bounds the death that a widow's or widower's marker states.**
   *Decided (owner, 2026-10-24): the lean.* It now decides what bounds
   the derived "died by" date (Q5, §2.4).
   - **The rule.** The date at which the source presents the widowhood
     as holding, which is also the `civil_status` statement's
     `point_in_time`.
     - The burial register presents Eugen as a widower at his death, so
       Cäcilie's bound is 16 March 1909.
     - A letter presents its writer's widowhood at its own date: the
       1958 letter gives 25 December 1958.
     - Reason: it is what the source says, and it is tighter than the
       document's date.
   - **Alternative:** always the document's own date, as "the late"
     takes it (the register's 1909, at year precision).
8. **A `birth` or `death` entity in a manifest.** For example, a 1941
   certificate's manifest that lists "Death of Rudolf". *Decided (owner,
   2026-10-24): the lean.*
   - **As decided.** It is dropped alone and counted, and never fails the
     manifest turn (§7). Reason: failing the turn would send the
     document to line-range segmentation with no manifest, and a death
     certificate makes such an entity likely.
   - **Alternative:** fail the turn, as ADR-0039 §10.1(b) does for an
     artifact kind.
9. **`ended_by_death_of` as death evidence.** *Decided, reversed
   (owner, 2026-10-24): yes, for its value* ("Sounds like proof that she
   died to me").
   - **How it is read.** In `read_evidence`, not through
     `DEATH_EVIDENCE_PROPERTIES` (§5).
   - **The guard.** §2.5's guard (a value the same batch calls
     `widowed` is refused) keeps a misplaced value off the
     survivor, and the critic checks it too.
   - **The drafted lean was no**, since the marker would also have given
     the spouse's own `date_of_death`. Under Q5 it gives none, so the
     end is the evidence.
10. **An unknown value with no excerpt.** *Decided (owner, 2026-10-24):
    the lean.*
    - **As decided.** Refused and counted, since nothing else shows what
      the source said. For example, "the late Felix" given as an unknown
      value with no excerpt is refused.
    - **Alternative:** accepted like any claim.
11. **Birth properties on a non-person.** *Decided (owner, 2026-10-24):
    the lean.*
    - **As decided.** Refused, as the death properties are, so it fails
      closed: `date_of_birth` is person evidence
      (`PERSON_SUBJECT_PROPERTIES`), so an event or a family that
      carries one is taken for a person. For example, the 1905 marriage
      certificate's "geboren am 15. Februar 1862", put on the marriage
      rather than on Sebastian, is refused.
    - **Alternative:** the owner's death-only rule.
12. **"His late wife" with no widowhood marked.** The 1905
    certificate's "dessen verstorbener Ehefrau Cäcilie". *Decided
    (owner, 2026-10-24): the lean.*
    - **As decided.** Her death ("the late", now an unknown value under
      Q5) and the marriage, with no `ended_by_death_of`. Reasons:
      - the words state a relation and a death, not that she was still
        his wife when she died, which is Q2's test;
      - ADR-0035 §3.1's row for these words already gives her marriage
        with no end;
      - in GEDCOM a partner's DEAT ends no union (ADR-0036 §5).

      Views derive the end (Q2).
    - **Alternative:** the union `ended_by_death_of` Cäcilie, as for a
      widower's marker.

**The lean-only choices are accepted** (owner, 2026-10-24):

- the `parentage` keys and labels;
- a register's parents with no kind;
- the court as the order's `issued_by`;
- the critic category's name;
- the places map;
- clustering reading kin of every kind;
- `ended_by_death_of` not lifted as a key;
- the adoptive path labels;
- the bounded form's precision rule, now the derivation's (§2.4);
- the two manifest checks kept separate (§7);
- `DEAT.AGE` left unmapped (Q1);
- the timeline's event arm reading a birth's or a death's principal
  alone (§6, Q5).

**Lean-only items raised with the owner's answers,** which the owner's
merge accepts:

- **one rule for an undated death** (§2.2): a source that states a death
  but not its date gives a `date_of_death` of unknown value, beside any
  place, cause or age it gives. The one exception is a widow marker
  that names the late spouse;
- **one phrase that is both markers** (§2.5) gives the union end and
  still the survivor's `civil_status` `widowed`;
- **the derivation as one rule** (§2.4): a death statement with no date
  is bounded by its document's own date; a stated widowhood date
  replaces it for the late spouse; neither is ever narrower than the
  date it comes from; an undated or open-ended document gives no
  bound;
- **a burial gives no "died by"** (§2.4, Q3);
- **a simpler guard** (§2.5, Q9): validation refuses an
  `ended_by_death_of` whose value the same batch calls `widowed`;
- **`ended_by_death_of` read at its truthy value,** item values only
  (§5, Q9);
- **Q6's scope** (§2.8): the rule covers a record's apparatus
  (certificates, registers, forms), so a doctor whom a letter names
  stays; and every unnamed reporter or certifier is not made.

## Consequences

- **One place to look.**
  - Privacy, clustering, reconcile, the site and the report read a death
    or a birth from the person's statements.
  - A union `ended_by_death_of` the person is the one other place where
    death status is read (Q9).
  - "Died by" dates are derived, not stored (Q5).
  - #577's event set-aside becomes a guard for curator-made events only.
- **An undated death has one form,** the unknown value, used only where
  a source states the death. The writer, the readers and the report
  learn the `somevalue` snak.
- **People whose release changes.**
  - People released in run 3 only through a death event are released
    through statements.
  - Those released only by a DEAT that Q4 leaves without a claim become
    possibly living, failing closed.
  - The people released only by a union end are counted.
  - The scratch diff counts releases both ways.
- **A union ended by a death copies no date and needs no event,** and is
  written only where a source states it; an implicit end is derived in
  views (Q2). So the drift that ADR-0036's alternatives warned of cannot
  arise.
- **Fewer officials.** Officials who only certify or witness no longer
  reach the graph (Q6).
- **Adoption and fostering** have a form before any source needs one,
  and a birth father and an adoptive father never disagree.
- **Cache.** Every extraction call and every critique misses the cache
  once, a cost shared with #646's build.

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
