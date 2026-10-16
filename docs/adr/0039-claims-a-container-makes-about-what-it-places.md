# ADR-0039: Claims a container makes about the artifacts it places

Status: **proposed** (drafted for #456, 2026-10-16). It answers ADR-0008's
open questions 2 (context across witnesses) and 6 (claims about an
excerpt itself), and amends ADR-0035 §3.4 and MODELING.md once the owner
accepts it. Nothing else changes until then: the amendments below are
proposed text, and ADR-0008 and ADR-0035 carry only a dated pointer here.
It covers both kinds of container: a PDF whose pages place images
(ADR-0008, as amended by #176 and #397) and a deck whose slides embed
them, which #502 makes excerpts too.

Revised 2026-10-16 with the owner's answers on #517: owner questions 2
to 6 are accepted as recommended; on question 1, who an image shows is
split from what an artifact is about, so `shows` joins the
artifact-subject properties beside `main_subject` (§2), and question 1
now asks the owner to confirm that split.

## In brief

- **A container speaks about the artifacts it places.** A caption under
  the letter's image in *Our Family from Germany* says who wrote it, to
  whom and when. That is the booklet's claim about the image, and it is
  written as statements on the image's own item, each `stated in` the
  booklet with its page.
- **The excerpt speaks for itself.** What the letter says stays cited to
  the letter's image. Nothing the caption says is moved into the letter's
  claims, and nothing the letter says is moved into the caption's.
- **The graph joins the two on the image's item.** Both sources' values
  stand side by side there, attributed, and may disagree (ADR-0002).
- **A document may say the same of itself.** The letter's own greeting
  names its addressee, so the same properties take a document's own
  claims about itself, as `issued_by` already does.
- **Who an image shows is `shows`; what a work is about is
  `main_subject`.** A group photograph gets one `shows` statement per
  person its source names; `depicts` stays the faces flow's confirmed,
  region-bound statement (ADR-0005).
- **No value is built from both** by the pipeline. Who a caption's
  "Wastl" is stays clustering's and curation's question (ADR-0007).
- **A slide's caption is the same question** as a compilation's, and has
  the same answer.

## Context

**What already holds.** An image a PDF places within a text-layer page
is a derived artifact, keyed by the SHA-1 of its bytes, `part of` the
PDF once per page it is on (ADR-0008, as amended by #289). Where the
image has a reading, it is a document of its own, an excerpt, whose
extraction reads the image's text alone and cites the image
(`ExcerptRun`, ADR-0008 as amended by #397). The PDF's own
transcription holds its own text, page by page, and marks where each
image sits by its SHA-1: `[Page 6, image 1: sha1:…]`. The write
registers the PDF, then each image its record (`derived-media.json`)
names, then writes the claims that cite them, so an image has an item
before any claim about it is written. An image that shows no page of a
document (a photograph with no writing) is no excerpt but is still
registered as the PDF's image.

A deck's images are recorded in the same record, with the slides they
are on, and registered at write the same way (ADR-0008 as amended by
#128 and #103). They are never read: the deck's transcription is its
slide text, one `[Slide N]` block per slide, and does not mark where an
image sits. #502 gives PDFs and decks one paged-container path, in which
every placed image goes through the excerpt path and every page or
slide marks its images by SHA-1.

**What is missing.** A claim's subject is a QID, a vocabulary item, a
batch-local entity, or, on a property the manifest marks
`document_subject` (`issued_by` only), the claim's own source document
(ADR-0035 §3.4). A caption's writer, recipient and date are statements
about the image, which is none of these to the compilation's
extraction. So today:

- the caption's people are extracted as people, with what the caption
  says about them (ADR-0008 as amended by #289), but its claim that one
  of them wrote the letter in the image is lost;
- the letter's own date stays whatever the letter gives, and a letter
  that gives no year gives no date, though the caption gives one;
- ADR-0008's question 2 asked whether the excerpt's extraction should
  read the caption to fill that gap. A "yes" would put the caption's
  year into a claim cited to the excerpt, which never states it
  (CLAUDE.md rules 1 and 5).

**What the corpus shows.** Scratch run 2, in aggregate: six PDFs placed
101 images that went through the excerpt path, and one deck embedded 17
images that were never read. Neither figure says how many of the images
a caption describes; the run did not count that. The owner's ruling of
2026-10-16 brings this in before #146, with every other open issue that
affects write structure.

**The owner's other rulings of 2026-10-16** apply the same principle,
that the graph records what each source asserts (ADR-0002), to other
claims about identity and structure:

- #499: every form a person was known by is an alias and a sourced
  `referred_to_as` statement;
- #515 (ADR-0038): a place's containment is a source's claim, scoped to
  the time the source gives.

A container's caption is the same kind of thing: a claim of the
container's, about another artifact, kept as the container's.

**The fictional example.** *Our Family from Germany*
(`family-booklet-2011`, docs/EXAMPLES.md) sets the top of Sebastian's
letter to Klara (`letter-excerpt-1939`) on its physical page 6, printed
as page 4, under the caption "Wastl to Klara, July 1939." The letter's
image reads "Flörburg, Brunnenring 35, den 28. Juli 1939. Liebes
Pünktchen! …". *Where the Lobigs came from* (`library-talk-2018`) sets a
photograph from the family album on its slide 2, titled "Wastl and
Hilde".

## Decision

### 1. The principle

**A container's text about an artifact it places is the container's
claim about that artifact, written on the artifact's own item and cited
to the container with its page. It is never transferred into the
artifact's own claims, and the artifact's are never transferred into
the container's.**

- Each source keeps its own reference (CLAUDE.md rule 1, ADR-0003): the
  caption's claims cite the booklet, page 6, with the caption as their
  transcription excerpt; the letter's cite the letter's image.
- Each keeps its own value and precision (rule 5): the caption's "July
  1939" is a month, the letter's "28. Juli 1939" a day, and neither
  narrows or widens the other.
- They coexist on the image's item (ADR-0002). Neither is preferred, and
  no frontend picks one (rules 4 and 6).

### 2. Which properties take an artifact as subject

**The artifact-subject properties.** The manifest gains six properties,
each marked `document_subject: true`, beside `issued_by`:

| Key | What it states | Accepts | Wikidata |
|---|---|---|---|
| `author` | the artifact → who wrote, drew or made it | human, organization, family | P50 |
| `addressee` | the artifact → whom it was addressed to | human, organization, family | P1817 |
| `inception` | the artifact → when it was made | a time | P571 |
| `location_of_creation` | the artifact → where it was made | place | P1071 |
| `main_subject` | the artifact → what it is about: a letter's topic, the district a map covers, the family a booklet is about | any kind | P921 |
| `shows` | the image → who or what it shows, as a source states it (a caption, an inscription on a print) | any kind | none (below) |
| `issued_by` (already) | the document → the organisation that issued it | organization | P2378 |

Each accepts `unclassified_entity` too, wherever it accepts a real kind
(ADR-0013). A photograph's author is its photographer: Photograph
Brüsrich, stamped on the studio portrait's mount, is the portrait's
`author` per the portrait itself. Dates take the qualifiers any date
takes (`earliest_date`, `latest_date`, `sourcing_circumstances`),
precision as the source gives it (MODELING.md, "Uncertainty and time").

**What a work is about, and whom an image shows, are two properties.**
`main_subject` says what a document or an image is *about*: the topic of
a letter, the district a map covers, the family a booklet is about.
Naming every person in a group photograph as its "main subject" would
stretch that meaning and mix the two questions, so who or what an image
*shows*, as a source states it, is `shows`:

- one `shows` statement per person or thing the source names, each
  stated in its source with its locator. The studio portrait's reverse
  reads "Muckel · Cilli · Wastl · Rahel · Felix": five `shows`
  statements on the portrait's item, each cited to the portrait itself,
  whose own text it is. A list gives no positions, and none is read into
  its order;
- where a caption words a position ("left to right: …", "seated, centre"),
  the statement may keep it in the qualifier `stated_position` (a string,
  verbatim as the caption words it, in the caption's language). It is
  never a computed region, and never `relative position within image`,
  which is the faces flow's measured box;
- no Wikidata mapping. Wikidata's nearest property is P180 *depicts*,
  which the manifest already maps to `depicts`; mapping `shows` to P180
  as well would make an export or a federated query read a caption's
  claim as a confirmed depiction, the very difference this split keeps.
  `stated_position` is unmapped too: P2677 *relative position within
  image* is a measured region.

**Not `depicts`.** `depicts` stays ADR-0005's: a depiction confirmed by a
person, tied to a region, written only through the faces flow, and never
in a scratch run. CLAUDE.md's rule that nothing writes `depicts` without
a person confirming it is untouched, and the writer refuses an
unapproved `depicts` anyway (ADR-0005, as amended by #226). The faces
flow may later read `shows` as a hint for whom to propose; that stays
outside the graph until a person confirms it (owner question 1).

**Not `instance_of`.** A caption that calls the image "a letter" is a
claim about its class, which ADR-0008's open question 4 still holds
(the registrar's `photograph` for a raster image). It is not among these
properties until that question is settled (owner question 4).

**The rule** that validation and the writer both apply, as ADR-0035
§3.4 applies it today, now reads:

1. A claim of an artifact-subject property takes as its subject an
   `artifact` reference, `{"kind": "artifact", "sha1": …}`, and nothing
   else: a local entity, a QID or a vocabulary item is refused.
2. That artifact is either the claim's own source, or an artifact the
   claim's source places: an image its record of derived media names, a
   deck's embedded image or a PDF's placed image.
3. No other property takes an artifact reference as its subject.

Each breach is a counted reject in validation, with its reason, and a
refusal in the writer, named in its `refused` list with its warning,
before the first write (`pre_plan_refusals`). The claim is the unit of
rejection.

What a container places is what its record says it places, never what
the model says: validation is handed the set of SHA-1s the container's
record names, beside its own source key; the writer checks the subject
against the derived media it registers for that source in the same
write. No page constraint applies. A caption on page 6 about the image
on page 6 is the usual case, but a commentary on page 8 may speak of the
letter on page 6; the claim's locator is where the container says it,
and the image's `part of` statements say where it sits.

A document that is not a container gives claims of these properties only
about itself. A narrative that mentions "the letter Dad wrote in July
1939" names no artifact by bytes, and the pipeline does not guess which
held artifact it means: such a claim is not given (owner question 6).

### 3. How a container's extraction is asked, and how the writer resolves the subject

**The prompt.** A container's extraction prompt (both the whole-document
prompt and the chunked one, ADR-0006) gains, beside the `issued_by`
paragraph:

- the list of the artifacts the text the call reads places, each as its
  artifact reference and its place: `{"kind": "artifact", "sha1": "…"}`,
  page 6, image 1. The list is taken from the record, matching the
  markers in the transcription; no prompt names an artifact by its QID
  (CLAUDE.md, Engineering conventions);
- one instruction: where the container's own text states something
  about one of those artifacts (a caption, a heading over an image that
  names it or what it shows, a sentence of commentary that speaks of
  it), give it as a claim whose subject is that artifact's reference,
  with the page the text stands on. Where the text states it of the
  document itself, give the document's own reference. Never give such a
  claim from what an image's own text says: that is the image's witness,
  read on its own;
- a judgement note (ADR-0035 §4, `judgement_relation`) where the reading
  that a heading or a sentence speaks of a given image is the
  extraction's call rather than the text's plain statement.

The people, places and organisations a caption names are entities of the
container's batch, as now, with their appellations and aliases (ADR-0018,
#499): the caption's "Wastl" is an alias and a `referred_to_as` of the
local entity it names, cited to the booklet, page 6.

**The excerpt's own prompt** gains the same paragraph for its own
reference only. It places nothing.

**The subject's resolution.** The writer resolves an artifact subject as
it resolves a source (writer step 0): to the item that carries that
`media SHA-1`. The container's write registers the container, then every
image its record names, before it writes any claim (ADR-0008 as amended
by #103 and #397), so the subject has an item by then, whether or not
the image is an excerpt and whatever becomes of the excerpt's own
extraction. An image the never-ingest list names is in no record, so it
is in no prompt, and a claim that names it anyway is refused. An image
whose bytes a document of the corpus has resolves to that document's
item, which is the same item (ADR-0010).

**Decks.** The same holds for a deck once #502 marks each slide's images
by SHA-1 in the deck's transcription (`[Slide 2, image 1: sha1:…]`), as
a PDF's pages mark theirs. A slide's text and its notes are the deck's,
cited to the deck with the slide as `page number`. Until then the deck's
prompt lists no images, so it gives no claim about one. Slide 2's notes,
"Photograph from the family album", say where the image came from,
which no listed property expresses, so they give no claim; its title,
"Wastl and Hilde", over the slide's one image, may give `shows` →
Sebastian and `shows` → Hildegard on the photograph's item,
stated in the deck, page 2, with a judgement note.

**Where nothing changes.** An image on a page read by vision is read as
that page, has no SHA-1 of its own and no item, and a caption beside it
stays a claim about the people it names, as ADR-0008's question 3 leaves
it. The images that a PDF read whole holds are the same.

**The example.** The booklet's batch gives, on page 6 (printed page 4),
with the caption as excerpt:

| Subject | Property | Value | Source |
|---|---|---|---|
| the letter's image | `author` | Wastl (the booklet's local entity) | booklet, page 6 |
| the letter's image | `addressee` | Klara | booklet, page 6 |
| the letter's image | `inception` | July 1939 (month) | booklet, page 6 |

The letter's image's batch gives, with no page and its own words as
excerpt:

| Subject | Property | Value | Source |
|---|---|---|---|
| the letter's image (itself) | `inception` | 28 July 1939 (day) | the letter's image |
| the letter's image (itself) | `location_of_creation` | Brunnenring 35, Flörburg | the letter's image |
| the letter's image (itself) | `addressee` | Pünktchen (its own local entity) | the letter's image |

On the image's item: two `inception` statements and two `addressee`
statements, each cited to its own source. The disagreement report lists
the dates as differing in resolution (`spans_nest`, ADR-0033), and the
addressees as one person only where clustering has made them one item.
The letter's own claims of Rahel's address and of Gustav's visa are
untouched, as are the booklet translation's.

A second, hypothetical case: a compilation reproducing a map under a
caption naming Kreis Peiltal would give `main_subject` → Kreis Peiltal
on the map's item, cited to the compilation. A curator's `coordinate
location` for Flörburg read off that map cites the map image, not the
compilation. Which district Flörburg lay in, and when, is each source's
claim (ADR-0038), never read off the map's subject.

### 4. Derived assertions

A value built from two sources (a caption's year with a letter's day and
month, had the letter given only "28. Juli") is not made by the pipeline.
Neither source states the combined value, so no extraction can cite it,
and a combining pass after write would be a machine inference that
narrows what each source says. Before the keeper run (#159) no one makes
one: scratch graphs are never curated (ADR-0025).

From the keeper run on, a curator may make one, as a new statement beside
both sources' own (CLAUDE.md rule 3), at normal rank (rule 4):

- two references, each `stated in` its source with its own locator;
- the qualifier `determination_method` (P459) → `combined_from_sources`,
  a manifest item ("combined from sources"), so that no reader takes the
  two references to mean that each source states the value;
- the curator's own provenance, as any curator statement carries it.

Frontends show it as "28 July 1939, combined from the letter and the
booklet's caption", beside the two values it was built from. A
deterministic pass that proposes such values for a curator to accept is
left for later, with process provenance (ADR-0023) and a proposals queue
outside the graph; nothing writes one unattended (owner question 2).

### 5. The container's claims never reach the excerpt's extraction

ADR-0008's question 2 is answered **no**: the excerpt's extraction reads
the excerpt's text alone, as now, and is handed nothing a container says
about it, not even as identity hints.

- The caption's claims are now expressible as the container's (§2), so
  nothing is lost by keeping them out.
- Identity is decided where both mentions already meet: clustering reads
  the booklet's batch and the excerpt's together, and reconciliation
  (ADR-0007) and curation decide who the caption's "Wastl" is.
- One image is one excerpt and one extraction, whichever containers
  place it (ADR-0008 as amended by #397). A prompt that held a
  container's caption would hold every container's, and its cache key
  would change as containers are added to the corpus, though the image
  did not change (ADR-0032).
- A hint is never only a hint to a model. A caption's names in the
  excerpt's prompt would steer which person the excerpt's "Pünktchen" is
  taken for, and from there its claims, which the excerpt alone must
  earn (CLAUDE.md rules 1 and 5).

### 6. Clustering

Two mentions that two sources give as the value of the same property of
the same artifact (the caption's Klara and the letter's Pünktchen, both
the image's `addressee`) **meet**: clustering pairs them, so that the
pair is judged, keyed by the artifact's SHA-1 and the property. The
shared role alone never makes the link confident, and never makes them
agree on anything else. Two sources may name different addressees of
one letter, and both claims are healthy (ADR-0002); a missed merge is
safe, and a false confident link is not (ADR-0035 as amended by #409).
Meeting is not merging (owner question 3).

The artifact's statements are never read as person evidence by
clustering beyond that pairing: being a letter's `addressee` says nothing
of a person's dates or kin.

### 7. Privacy and display

**The non-privileged export** needs no new rule. A statement on an
artifact item whose value is an item left out goes whole, with its
qualifiers and references, and its transcription excerpt leaves only
with a statement it attests (ADR-0020, as amended by #364 and #412). An
artifact-subject property marks no one as a person: its values are like
`depicts`'s, which mark no one (ADR-0020 as amended by #344), since an
author or an addressee may be an organisation and an image may show a
building. `shows` is the same: a person a caption names as shown in a
photograph is not thereby released. So a possibly living
person named only as a caption's addressee is not released by it, and
an item the check never assessed is dropped, failing closed.

**The static site** (private; plural by default, CLAUDE.md rule 6):

- an artifact's page gains a table, "What sources say about this
  artifact": each statement on its item by property, every value with
  its source label and locator, "per *Our Family from Germany*, page 6
  (printed 4)", or "per this artifact" for the artifact's own; values
  that disagree are listed together, none first;
- the container's page keeps its evidence table, in which these
  statements already appear, the image as their subject;
- a person's page gains "Named in artifacts": each statement whose value
  is that person, as "author of *Our Family from Germany* — page 6,
  image 1, per *Our Family from Germany*, page 6"; it carries the
  possibly-living badge as the rest of the page does;
- the timeline places an artifact's `inception` only on the artifact's
  own page, labelled per source; whether a person's timeline shows the
  letters they wrote or received is left for later.

### 8. What this costs and checks

- The manifest's six properties, the `stated_position` qualifier, the
  prompts' paragraph and the list
  of placed artifacts change the extraction stage: `stage:extraction`
  and `needs:scratch-diff` (ADR-0025 §4). Validation and the writer each
  check the rule (§2), and a live test writes a container's claim about
  its image through the running stack (CLAUDE.md, Engineering
  conventions).
- The run report counts, as aggregates, the artifact-subject claims given
  by property, how many name the source itself and how many a placed
  artifact, and the rejects by reason.
- The deck half waits on #502's markers; the PDF half does not.

### 9. Proposed amendments (applied on acceptance)

**ADR-0008.**

- Question 2: answered by ADR-0039 §5. The excerpt's extraction reads
  the excerpt alone; what a container says about it is the container's
  claim, now written on the excerpt's item (§2), and identity across the
  two is clustering's and curation's.
- Question 6: answered by ADR-0039 §2 and §3. A claim's subject may name
  an artifact by its SHA-1 where the claim's source is that artifact or
  places it, on the artifact-subject properties only; the writer
  resolves it as it resolves a source. The #289 amendment's sentence "A
  claim whose subject is the excerpt itself cannot be expressed yet"
  points here.
- The same holds for a deck's embedded images, once #502 marks them.

**ADR-0035 §3.4.** "`issued_by`" becomes "the artifact-subject
properties (`issued_by`, `author`, `addressee`, `inception`,
`location_of_creation`, `main_subject`, `shows`; ADR-0039)". The
subject is the source artifact itself, or an artifact the source places by its record
of derived media, named by its SHA-1, and resolved to its item at write.
§3.5's table gains the six rows of §2. §3.6's vocabulary gains them,
`stated_position` (§2), and `determination_method` and
`combined_from_sources` (§4).

**MODELING.md.**

- The paragraph on `document_subject` (core classes): it names the
  seven properties (`issued_by` and the six of §2), and the subject may be the source or an artifact it
  places.
- "Compound artifacts": a bullet that a container's claims about an
  artifact it places are written on that artifact's item, `stated in`
  the container with its page, beside the artifact's own claims, which
  cite the artifact; neither is moved into the other.
- "Artifact coverage": "subject coverage reuses `depicts`" becomes
  "subject coverage uses `main subject`; who or what an image shows, as
  a source states it, is `shows`; `depicts` is the faces flow's
  confirmed depiction (ADR-0005)".
- "Faces and depiction": a bullet that `shows` is a source's statement
  of whom an image shows, never a depiction, and may seed a face
  proposal outside the graph.
- "Contradictions": the example of a caption's and a letter's dates on
  one artifact item.

## Owner questions

1. **`shows` for whom an image shows, `main_subject` for what a work is
   about** (reopened by the owner, 2026-10-16: "Does main_subject not
   create an awkward issue of contrivance for, say a group photo?").
   Recommended: confirm the split of §2. `shows` takes one statement per
   person or thing a source names as shown, with a caption's worded
   position kept verbatim in `stated_position`, never a computed region,
   and maps to no Wikidata property. `main_subject` keeps only "what it
   is about". `depicts` stays the faces flow's confirmed, region-bound
   statement (ADR-0005), and `shows` may seed a face proposal outside
   the graph. The alternatives: `main_subject` for both, contrived for a
   group photograph; or `depicts` without a region, cited to the
   container, which would make ADR-0005's writer rule tell two kinds of
   `depicts` apart.
2. **Derived assertions.** Accepted by the owner, 2026-10-16.
   Recommended: none by the pipeline; from the
   keeper run on, a curator may write one with two references and
   `determination_method` → `combined_from_sources` (§4). A deterministic
   pass that proposes them for a curator waits until someone asks for it.
3. **Clustering on a shared artifact role.** Accepted by the owner,
   2026-10-16. Recommended: two mentions
   that are the same property's value on the same artifact meet, so the
   pair is judged, but the role alone never makes a confident link (§6).
   The alternative, counting it as agreement, would merge two sources
   that name different addressees of one letter.
4. **A caption's class for an image.** Accepted by the owner,
   2026-10-16. Recommended: `instance_of` is not
   an artifact-subject property until ADR-0008's question 4 (the class of
   a compilation's images) is settled; then a caption's "letter" could be
   the container's `instance_of` claim, beside the registrar's.
5. **The caption never reaches the excerpt's extraction** (ADR-0008
   question 2). Accepted by the owner, 2026-10-16. Recommended: not at all, not even as identity hints (§5).
   The alternative, identity hints only, would change the excerpt's
   prompt and its cache key with every container that places it, and
   steer the excerpt's own readings.
6. **Only the source itself, or what it places.** Accepted by the
   owner, 2026-10-16. Recommended: a claim
   may name as its subject only its own source or an artifact its source
   places by bytes. A document that writes about a held artifact without
   placing it (a narrative recalling a letter) gives no claim about it; a
   curator may link them. The alternative would ask the model to pick a
   held artifact from a description, which is a guess.

## Consequences

- A caption's writer, recipient and date are kept, attributed to the
  container, on the item a reader opens for the image. Queries join
  them through it: "letters whose author, per the compilation, is
  Sebastian" reads the image's `author` statements and keeps those
  whose reference is `stated in` an artifact the image is `part of`.
- A document's own claims about itself (a letter's date and place, its
  greeting's addressee, a photograph's studio) become statements on its
  own item, beside the people and events it speaks of.
- A group photograph's named people are each a `shows` statement on its
  item, attributed to the source that names them, and none of them is a
  depiction until a person confirms a face through the faces flow.
- Two witnesses of one artifact stay separable: no reference cites a
  source for what it never said, and no excerpt's claim depends on any
  container's text.
- The manifest gains six properties, two qualifiers (`stated_position`
  and `determination_method`) and one value, and the
  rule of ADR-0035 §3.4 widens from "the source itself" to "the source,
  or what it places by bytes", still checked twice, before and at write.
- A deck's images gain container claims only with #502; until then its
  17 images have none.
- Captions beside images on scanned pages stay claims about people only
  (ADR-0008 question 3).
- Whether the disagreement report and the under-witnessed queue cover
  artifact items as they cover people is left for later; nothing here
  depends on it.

## Alternatives considered

- **The excerpt's extraction reads the caption** (ADR-0008 question 2,
  "yes"). It puts the caption's year into claims cited to the excerpt,
  which never states it (CLAUDE.md rules 1 and 5), and ties one image's
  extraction to every container's text.
- **Container claims only about the people the caption names**, as
  today. It keeps the caption's names but loses what it says of the
  artifact: that this letter is Sebastian's, to Klara, of July 1939.
- **A local artifact entity in the container's batch**, minted as an
  item of its own and later linked to the image. It makes a second item
  for an artifact that already has one, keyed by its bytes (ADR-0010),
  and leaves the link to curation.
- **`depicts` from captions.** See owner question 1.
- **`main_subject` for whom an image shows.** Contrived for a group
  photograph, whose five named people are not five "main subjects", and
  it mixes what a work is about with whom an image shows.
- **Combining values by a pass after write.** See §4: it would narrow
  what each source says, and write a value no source states.
