# ADR-0038: Place containment as time-scoped source claims

Status: **accepted** (accepted by the owner, 2026-10-23; drafted
2026-10-16 for #515, on the owner's direction of that day). Each owner
question below is settled by its recommendation. Supersedes in part
[ADR-0009](0009-place-hierarchy.md): its "Provenance" section and
decision FA of #201's amendment (PR #513, parked), with the parts of its
adjudication that follow from them (§9 below). The rest of ADR-0009
stands. ADR-0017 carries the amendment §8.2 gives it. The consequences in
§8 are applied with the code that implements them: items 1 to 4 with
#515's implementation, items 5 to 7 with the rebuild of #201's stage.
Its implementation comes before #146, the first scratch run that writes,
since the owner resolves every open issue that affects write structure
first (2026-10-16).

## Context

ADR-0009 treats containment between places as "world knowledge, not a
source assertion". Since #465, extraction writes a `part_of` only where
a source states it, referenced like any claim. The place-hierarchy pass
then asks a model whether places nest and writes timeless `part_of`
statements from its verdicts, referenced to the pass's run item. Some of
those statements no document states, such as the containment of a
competing pair of values (decision FA of #201's amendment, PR #513).

Nothing anywhere carries a time. Scratch run 2, in aggregate: 54
place-in-place `part_of` claims from 23 documents, none with a time
qualifier; 84 place mention records stating 131 container names.

Places change definition over time, and sources refer to them at
different points in relation to those changes. A town may lie in one
country in one year and in another later, whether or not either country
went on existing. Under ADR-0009 such a town holds timeless statements
that read as a contradiction, or one model's single answer, most likely
today's. The universe has the plain case already: the 1905 marriage
certificate puts Fläckfeld in Kreis Mockburg, the 1984 family tree in
Kreis Peiltal (docs/EXAMPLES.md). The archive can't tell whether the
district changed or one source is wrong, and it doesn't need to: it
records what each source says (ADR-0002).

The owner's direction (2026-10-16): containment is settled by the same
truth-agnostic approach as claims about people. A source that states
place A is within place B gives a record that A is claimed to be within
B, at the source's time, as claimed by that source. The structure should
also take historic boundaries for different points in time later.

## Decision

### 1. A containment is a source's claim

`A part of B` is a statement like any other. It is stated in the source
that says so, with its locator (ADR-0003 rule 1). It sits at normal
rank, beside any contrary source's claim (ADR-0002). A machine-written
one also cites its run (ADR-0003 rule 2): extraction's run for a claim
extraction gave, or the place stage's run item for a link the stage
writes (§8.5).

- **No source, no containment.** Nothing writes a `part_of` that no
  source states in the direction written. Process provenance alone (an
  `inferred from` with no `stated in`) never backs a containment. This
  replaces decision FA.
- `location`, which places an organisation or an event (ADR-0012,
  ADR-0031), follows the same rule: it is a source's claim, and the
  report reads it beside `part_of` (§5).
- A curator who records a containment cites the source it rests on,
  such as an artifact or a gazetteer (§6). Owner question 6 asks whether
  a curator may also assert one on their own authority.

### 2. Time: what the source gives, and nothing else

A containment holds for a period. It is dated the way MODELING.md dates
a residence or an occupation ("Marriages, residences and other
periods"), so the qualifiers, their ranges and their readers are
already there:

- **A source that dates the containment's start or end** ("A, in B
  from year Y"; "A, in B until year Z") gives `start_time` or `end_time`
  qualifiers. Each end has its own range: a start from its
  `earliest_date` to its `latest_start_date`, an end from its
  `earliest_end_date` to its `latest_date`.
- **A source that presents the containment as holding at a date**,
  without dating its start or end ("in year Y, A belonged to B"), gives a
  `point_in_time` qualifier. As on a residence, this dates an
  observation, bounded by `earliest_date` and `latest_date` where the
  source gives a range.
- **Circa** is `sourcing_circumstances`, as on any statement.
- **A source that states no time** gives a statement with no time
  qualifier. The document's own date is never written as the
  containment's point in time. It is no observation the source made of
  the containment: a later source often places a place in later
  containers, or names it by a later name (the 1984 tree's "Bad
  Flörburg" for an 1836 birth). Writing that date would narrow what the
  source says (rule 5).
- **Views read an undated claim "as of" the source's own date**,
  through its reference, and label it so: "Fläckfeld: in Kreis Mockburg
  per the 1905 marriage certificate; in Kreis Peiltal per the 1984
  family tree". That is a reading, never a stored value. Owner question
  1 asks where the source's date comes from, since an artifact item
  states no date today.
- **A containment belongs to the place, not to the event a source
  describes.** A 1984 source's container for an 1836 birthplace is read
  as of 1984, never as of 1836.

### 3. The model asserts no geography

No model, at any stage, answers whether one place lies within another.
A containment reaches the graph only from a source. The one question a
model may be asked is **which item a source's container name means**,
among existing places that share it: is "Rekau" in this document's chain
the town Rekau or Stadtkreis Rekau? That is an identity question, like
clustering's (ADR-0007, ADR-0019). Its answer picks the claim's value
and never adds a containment the source does not state. Where the model
can't tell, or names no candidate, nothing is written and the question
is counted.

### 4. Competing values stay plural

Two competing values of one fact (born in Brunnenring 35 per one
source, in Flörburg per another) are no longer a containment question.
Where some source nests them, its claim is in the graph and the report
reads it (§5). Where no source does, they stay on the inconsistency
report as two attributed values. A competing pair is never asked of a
model for containment, and neither is any other pair.

### 5. The report's containment suppression

The report leaves out a pair of place values that a containment path
connects, because they differ in resolution (MODELING.md,
"Contradictions"). Each edge on that path is now a source's claim, and
some carry a time. The report reads the path:

- **through claims a source states**: a `part_of` or `location` with a
  `stated in` reference, at normal or preferred rank. A deprecated
  claim is left out, as everywhere in the report. A statement with no
  stated-in source takes no part, as for any value the report compares.
- **at any time, for now** (recommended, owner question 2): one path
  in the graph is enough, whatever its claims' times. Reading the path
  "as of" the claims' times would need every undated claim's source date
  (owner question 1) and a rule for comparing periods. Until both exist,
  a time-aware reading would suppress less, and less predictably.

The static site's places page shows each place's containers plurally,
each with its sources and any time it carries (rule 6), never as one
tree.

### 6. Gazetteers are cited sources with their own time scope

GeoNames and the World Historical Gazetteer (WHG) are sources, not
ground truth. An edge one of them supplies is a claim `stated in` that
gazetteer's dataset item, with the record's id as its locator
(`geonames_id`, `whg_place_id`), beside `inferred from` the importing
run.

- **GeoNames** describes present-day administrative divisions. Its
  dataset item carries the date of the version imported, and an edge
  from it carries no time qualifier: views read it as of that date, like
  any undated source (§2).
- **WHG** records state when a relation held. Such a time is written as
  the claim's `start_time` and `end_time`, with ranges as §2 gives them.
  An edge WHG gives no time stays undated.
- A gazetteer's edge never displaces a document's claim. It sits beside
  it, and the report and the site name it as the gazetteer's.

ADR-0009's GeoNames section otherwise stands: activation needs an
account (#164), and coordinates and external ids come with it.

### 7. A place's identity is separate from its containers

- **One place, one item, over time.** A place keeps its item while its
  names and its containers change. Flörburg is one item named Flörburg
  and, from 1954, Bad Flörburg (its name statements carry the change),
  whatever any source says contains it in any year. A change of
  container is a new claim with its own time, never a new item.
- **An administrative unit that existed for a period is an item with
  its own time bounds.** A district, a province or a republic has its
  own `start_time` and `end_time` claims, each a source's claim
  (MODELING.md, "Places": historical places are ordinary items with time
  bounds). A container that ceased to exist stays the value of every
  claim that names it.
- **Whether two units are one item is an identity question**, for
  sources and curators through the ordinary identity flow (ADR-0007),
  never for a model's knowledge of history. The 1941 death certificate
  names its republic as the RSFSR. Whether that is a name of the item
  for Russia, as the universe has it, or a unit of its own is decided
  there. Either way, the certificate's claim is written against the item
  the identity map resolves.
- **Historic boundaries attach later to those units**, one geometry
  per period and per source: a statement on the unit's item, qualified
  by `start_time` and `end_time`, stated in the atlas or gazetteer that
  draws it. Its datatype and import get an ADR of their own. The
  containment model needs no change for them. Containment is never
  derived from geometry: a point inside a polygon is not a source saying
  so. A view may draw one over the other, but nothing is written from
  the overlap.

### 8. What follows from it

Applied with the implementation (accepted by the owner, 2026-10-23):

1. **The inconsistency report** reads the containment path as §5
   says. `place-containment.rq` fetches, for each edge, its rank and
   whether it has a stated-in source; `yizkr.disagreement` keeps only
   the edges §5 reads. The report's other rules are unchanged.
   *Applied (#515):* the query lists each `part_of` and `location`
   statement, not its truthy triple, with `?rank` and `?stated`;
   `yizkr.disagreement.followed` keeps an edge whose rank is not
   deprecated and that has a stated-in source.
2. **ADR-0017.**
   - **Rule 2 stands.** A source's claim that A lies within B is still
     a referenced fact that A and B are two places, at any time, since
     nothing contains itself. It reads the same edges as the report
     (§5).
   - **§2's hand-off is withdrawn** (owner question 5). Graph dedup's
     adjudicator returns a container QID from its own knowledge on a
     `distinct` verdict, which is the model asserting geography (§3).
     The `distinct` verdict stands; no `-containment.csv` row comes from
     it.
   - ADR-0017 gains an amendment line saying so.
   *Applied (#515):* the graph-dedup prompt asks for no container, a
   proposal carries none, and `reconcile-graph` writes no
   `-containment.csv`. Rule 2's closure (`fetch_place_containment`)
   runs `place-containment.rq` and keeps the edges `followed` keeps.
3. **Extraction's containment rule** keeps "only where the source
   states it", and asks for the time the source gives (§2), never the
   document's date. The extraction critic's `unsupported_containment`
   also flags a time qualifier its excerpt does not state. This changes
   a prompt, so its PR carries `needs:scratch-diff` and `stage:extraction`
   (ADR-0025 §4).
   *Applied (#515):* the single-call and chunk prompts ask for
   `start_time` or `end_time` where the source dates the containment's
   start or end, `point_in_time` where it presents it as holding at a
   date, with ranges and circa, and none otherwise. Neither the
   document's own date nor the date of an event the source places there
   is the containment's time: a containment belongs to the place (§2). The
   critic's containment block shows each `part_of` claim's dating
   qualifiers beside its excerpt, and `unsupported_containment` names
   both dates as times the excerpt does not state for the containment.
4. **The writer** writes a `part_of` claim's time qualifiers like any
   claim's. The claim schema and the manifest accept, where they don't yet, `point_in_time`,
   `start_time`, `end_time`, the four range qualifiers and
   `sourcing_circumstances` on `part_of` and `location`. The write test
   covers a dated and an undated containment.
   *Applied (#515):* the claim schema already takes any manifest
   property as a qualifier, and the writer writes a qualifier as it
   writes any claim's; the manifest's `part_of` description now names
   the time a source gives. tests/unit/test_containment_claims.py and
   tests/integration/test_live_containment.py cover a dated and an
   undated containment, side by side.
5. **The place stage and `reconcile-places`** become the identity
   question of §3, on the worklist ADR-0009 builds:
   - **Questions** come only from stated links: each chain of
     containers a mention record states (ADR-0035 §2.2), link by link,
     where the container name names more than one existing place, or
     where a record states a link that no claim of its document gives
     (owner question 3).
   - **Gone from the worklist:** competing pairs (§4), and the
     containment verdicts `a_within_b` and `b_within_a`. The answer
     names one of the candidates, or none (`uncertain`).
   - **What it writes:** a link the source states, `part of` the item
     the answer names, `stated in` the stating document's artifact and
     `inferred from` the stage's run item. It carries the time the
     document's claim gives, and none where the link comes from a
     record alone, since a record states no time.
   - **Stands from ADR-0009 and PR #513:** the stage after the write
     (reset → register → write → places → site), its key, guard and
     lock, decision FB (a complete write only) and decision FC.
     Reset-stable prompts stand too: no QID, numbered questions,
     namesakes numbered by description (decision FD). So do the cycle
     guard against the graph and the batch, idempotent skips, refusals
     of conflicting and out-of-schema answers, the stage store, and the
     report and diff counts, recounted for the new questions.
   - **The command `reconcile-places`** asks the same question for a
     curator, with the same CSV contract less the containment verdicts.
     Its `--apply` writes only links a source states, cited as above.

   *Applied (#201):* `yizkr.pipeline.places` takes each link a record
   states (`stated_links`), reading the record's chain nearest first,
   the order #555 has the entity manifest ask for (ADR-0035 §2.2; the
   stage relies on it, and #146 runs only after both merge), with the
   qualifiers of each claim of its document that gives it (a `part_of`
   from the link's inner end to an entity a name of which is the
   container's), and asks (`build_worklist`) of a link whose container
   names more than one place, or that no claim gives. A link a claim
   gives whose container names one place is settled without a question,
   as the write wrote it from the claim; a link whose container names no
   place, or whose place is none of the graph's, is left out and counted
   (`unnamed`, decision FC as applied). A question names the place, its
   chain, the container and the numbered candidates; the answer names a
   candidate's number or `uncertain`. The inner end of a later link is
   the place the link before it resolved to; a link whose inner end an
   uncertain answer left open is counted (`unresolved`) and not written.
   Each link is written by `write_link`: `part of` the named item with
   the claim's qualifiers, `stated in` the artifact with no page,
   `inferred from` the run item, each its own reference block; a
   statement of the same value and qualifiers that cites the artifact
   already is left as it is, and one citing other sources gains the
   link's blocks. The prompt names each place by its label and
   description too, which the instance keeps unique with the label and
   the writer makes from the identity map alone; places whose names
   still coincide are numbered by the documents whose records name them,
   else in the order given (decision FD as applied). Between rounds the
   graph's containment removes no question: a candidate the graph
   already puts the place in may be a namesake the source does not mean,
   so a later round asks again, and the idempotent skip keeps each link
   from being written twice (§9's "transitive elimination between
   rounds" as applied). The scratch run's `places` stage
   (yizkr.pipeline.scratchwrite) writes every answer unattended;
   `reconcile-places` writes the CSV's approved `same` rows, with the
   columns of ADR-0009's contract (A the place, B the candidate named)
   and the chain and the container's number in it (from 1, as the
   question gives it) beside them.
   *Applied (#569):* `stated_links` and `build_worklist` read the
   containers that stand, each a name with its evidence, by name and
   nearest first as before: a container whose evidence fails its checks
   is dropped from the record at extraction, its record standing
   (ADR-0035 §5), so it states no link here; a container written as a
   bare string gives no evidence and is dropped too. No model gives
   geography:
   the evidence is the source's words, checked against its transcription.
   *Applied (#571):* the GEDCOM export states the containment its PLACs
   give as the extracted documents do. Each jurisdiction of a PLAC is a
   place entity, one per distinct tail of components in the export, and
   each place, a PLAC's own and a jurisdiction, carries its own name as
   its label or an alias, so `stated_links` matches a claim to its
   container and `build_worklist` names the container by it. Each link
   of a PLAC's chain is a `part_of` claim of the export, cited by the
   PLAC's line, with no time qualifier, since a PLAC states none (§2).
   Its links are therefore claimed, not record-only, and a container the
   export's own place alone bears is settled without a question; one a
   place of another document also bears is asked of, as any is
   (ADR-0035 §6).
6. **The rebuild of #201's stage** reuses PR #513's plumbing as listed
   in point 5, and drops its competing-pair questions and its run-only
   provenance. Graphs written before it are scratch graphs, removed by
   the next reset (ADR-0025), so no migration is needed.
   *Applied (#201):* the stage runs after the write, under the write's
   guard and the live-stack lock, over a complete write alone, keyed by
   the place key, with its record and raw answers in `corpus/places/`.
7. **The cycle guard** still refuses a write that would close a cycle,
   at any time. A cycle across sources is almost always a namesake
   resolved to the wrong item, which is §3's question, so the refusal is
   counted and named in the run report (owner question 4).
   *Applied (#201):* `places.plan` walks the graph's non-deprecated
   `part_of` and `location` statements of any time and the links
   planned before; the run report counts the refusals in its aggregates
   (`refused_cycles`) and names each, by its places' labels and its
   document, in report.json and report.html, which stay on the machine.
   A record-only link's count is `record_only_links`, and of them those
   written `record_only_links_written` (owner question 3).

### 9. What it supersedes in ADR-0009, and what stands

**Superseded:**

- the "Provenance" paragraph of "Review and application": containment
  is a source's claim, not world knowledge carrying process provenance
  alone (§1);
- decision FA of #201's amendment (PR #513): a containment no document
  states is never written (§1);
- as a consequence:
  - the containment verdicts `a_within_b` and `b_within_a` as a model's
    answers ("Adjudication and verdicts");
  - competing pairs as a source of containment questions (worklist
    item 1);
  - the Context's premise that containment is world knowledge;
  - the GeoNames section's "machine claims referenced to a GeoNames
    dataset item plus the run", which §6 restates with the gazetteer's
    time scope.

**Stands:**

- the worklist mechanics PR #513 built: stated-container candidates
  link by link, every namesake asked, spine-first ordering, transitive
  elimination between rounds (as applied by #201, §8.5: idempotent
  skips, since the graph's containment answers no identity question);
- the cycle guard;
- reset-stable prompts (decision FD and the numbered questions);
- decisions FB and FC;
- the `same` verdict's routing to the curator-executed identity flow,
  for any pair the command still asks. As applied by #201 (§8.5) the
  command asks no pair: its `same` now says the container is the
  candidate B, and `--apply` writes the link it decides;
- the GeoNames activation and its external ids and coordinates.

## Consequences

- The acceptance test of ADR-0002 holds for places: a place can hold
  zero canonical containers and N attributed ones, dated or not, and
  the report, the site and reconciliation still work.
- Fewer containment edges reach the graph than under ADR-0009. Only
  what sources state does, so the report suppresses less and shows more
  differences of resolution as rows. Those rows are honest: no source
  in the archive nests the two values. A gazetteer, once activated,
  states most of the spine.
- The model's part shrinks to identity among namesakes. Its errors
  pick the wrong item for a stated container. Such a pick is a
  contestable identity, visible on the site, and never a containment no
  source makes.
- Time-scoped containment, administrative units with time bounds and
  later boundaries all use statements the graph already has: periods
  and ranges as for residences, and time bounds as for any entity.
- Views carry the work of reading containment as of a date. Until an
  artifact states its date (owner question 1), they label each
  container by its source alone.

## Owner questions

1. **A source's date for "as of".** An undated containment is read as
   of its source's date, but an artifact item states no date today.
   *Recommendation:* an artifact states its own date (of issue, or of
   writing), stated in itself like its registrar statements, with
   precision, ranges and circa as any date. It gets its own issue.
   Until then views label a container by its source alone.
   *Settled (owner, 2026-10-23):* as recommended. An artifact's own
   date gets an issue of its own; until it lands, views label a
   container by its source alone.
2. **The report's suppression in time.** *Recommendation:* a path at
   any time, through stated, non-deprecated claims (§5). Revisit "as of
   the claims' times" once question 1 is settled and dated containment
   exists in numbers.
   *Settled (owner, 2026-10-23):* as recommended, a path at any time
   through stated, non-deprecated claims (§5, §8.1).
3. **A link a record states that no claim gives.** *Recommendation:*
   the place stage writes it, stated in the document's artifact, with no
   page and no time (a record has no locator and states no time, as PR
   #513 found). The run report counts such links, a measure of
   extraction's recall on containment. The alternative is to count them
   only, and fix extraction until claims carry every link.
   *Settled (owner, 2026-10-23):* as recommended, the place stage
   writes such a link and the run report counts it (§8.5).
4. **A cycle across sources.** *Recommendation:* refuse and count, as
   now (§8.7). The alternative, letting it stand as two sources'
   coexisting claims, would need every reader of the closure to handle
   cycles.
   *Settled (owner, 2026-10-23):* as recommended, refuse and count
   (§8.7).
5. **ADR-0017 §2's hand-off.** *Recommendation:* withdraw it (§8.2),
   since its container comes from the model's knowledge. A reviewer who
   knows a containment records it by citing a source.
   *Settled (owner, 2026-10-23):* as recommended, the hand-off is
   withdrawn (§8.2).
6. **A curator's own containment.** *Recommendation:* a curator cites
   the source it rests on (§1), and never asserts geography on their own
   authority. The alternative is a curator-assertion reference, as
   ADR-0002 allows for resolutions.
   *Settled (owner, 2026-10-23):* as recommended, a curator cites the
   source a containment rests on (§1).
