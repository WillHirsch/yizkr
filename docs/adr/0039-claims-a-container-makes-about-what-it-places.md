# ADR-0039: Claims a container makes about the artifacts it places

Status: **accepted** (accepted by the owner, 2026-10-23; drafted for
#456, 2026-10-16). It answers ADR-0008's open questions 2 (context
across witnesses) and 6 (claims about an excerpt itself), and amends
ADR-0008, ADR-0035 §3.4 to §3.6 and MODELING.md, as §9 gives. The owner
accepted it as written, each owner question settled by its
recommendation (below). It covers both kinds of container: a PDF whose
pages place images (ADR-0008, as amended by #176 and #397) and a deck
whose slides embed them, which #502 makes excerpts too.

Revised 2026-10-16 on the owner's direction on #517, quoted: "Wait, are
these fields about a source's relevance to its container, or independent
details of a source's contents? Because if the former, I think it should
be very simple and minimal, probably just a single free text field, and
the other details should be assigned to the independent attributes of
the (inner) source itself." The first draft let a container state an
artifact's author, addressee and date; it is now an alternative
considered. A container gives only its own words for what it places, and
every structured attribute is the inner source's own.

## In brief

- **A container's claim about an artifact it places is its own words for
  it, and nothing else.** The caption under the letter's image in *Our
  Family from Germany*, "Wastl to Klara, July 1939.", is written
  verbatim as one `caption` statement on the image's item, `stated in`
  the booklet with its page.
- **Every structured attribute is the inner source's own.** The letter's
  date, place and addressee are the letter's claims about itself, cited
  to the letter's image, as `issued_by` is a certificate's claim about
  itself. A container never states them about what it places.
- **The graph joins the two on the image's item.** The booklet's caption
  and the letter's own statements stand side by side there, each
  attributed to its own source (ADR-0002).
- **`main_subject` is what a work is about; `shows` is who or what an
  image shows, as its own content says.** The studio portrait's reverse
  names five people: five `shows` statements, cited to the portrait.
  `depicts` stays the faces flow's (ADR-0005).
- **The caption's people stay the container's entities**, with their
  appellations (#499), as today. The caption links them to the image only
  as text.
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
(ADR-0035 §3.4). So today:

- a caption's people are extracted as people, with what the caption says
  about them (ADR-0008 as amended by #289), but what the caption says of
  the image is lost;
- a document cannot state its own author, addressee, date or place of
  making, though a letter's heading and greeting say them;
- ADR-0008's question 2 asked whether the excerpt's extraction should
  read the caption to fill its gaps. A "yes" would put the caption's
  year into a claim cited to the excerpt, which never states it
  (CLAUDE.md rules 1 and 5).

**What the corpus shows.** Scratch run 2, in aggregate: six PDFs placed
101 images that went through the excerpt path, and one deck embedded 17
images that were never read. Neither figure says how many of the images
a caption describes; the run did not count that. The owner's ruling of
2026-10-16 brings this in before #146, with every other open issue that
affects write structure.

**The owner's other rulings of 2026-10-16** apply the same principle,
that the graph records what each source asserts (ADR-0002):

- #499: every form a person was known by is an alias and a sourced
  `referred_to_as` statement;
- #515 (ADR-0038): a place's containment is a source's claim, scoped to
  the time the source gives.

**The fictional example.** *Our Family from Germany*
(`family-booklet-2011`, docs/EXAMPLES.md) sets the top of Sebastian's
letter to Klara (`letter-excerpt-1939`) on its physical page 6, printed
as page 4, under the caption "Wastl to Klara, July 1939." The letter's
image reads "Flörburg, Brunnenring 35, den 28. Juli 1939. Liebes
Pünktchen! …". *Where the Lobigs came from* (`library-talk-2018`) sets a
photograph from the family album on its slide 2, titled "Wastl and
Hilde", with the speaker note "Photograph from the family album". The
studio portrait (`studio-photo-1890s`) carries "Photograph Brüsrich ·
Peiltal" on its mount and "Muckel · Cilli · Wastl · Rahel · Felix" on
its reverse.

## Decision

### 1. The principle

**What a container says about an artifact it places is the container's
text about it, kept verbatim on the artifact's item and cited to the
container with its page. What an artifact is (who made it, for whom,
when, where, what it is about, whom it shows) is the artifact's own
claim about itself, cited to the artifact.**

- Each source keeps its own reference (CLAUDE.md rule 1, ADR-0003): the
  caption cites the booklet, page 6; the letter's statements about
  itself cite the letter's image.
- Nothing crosses: the caption's "July 1939" is never the letter's date,
  and the letter's "28. Juli 1939" is never the booklet's (rule 5).
- They coexist on the image's item (ADR-0002), and no frontend picks one
  (rules 4 and 6).

### 2. The properties

#### 2.1 `caption`: a container's own words for what it places

| Key | What it states | Datatype | Wikidata |
|---|---|---|---|
| `caption` | the placed artifact → the container's own words about it, verbatim | monolingual text, in the container's language | P2096 (*media legend*) |

- **What it holds.** A caption, a heading over the image, or a sentence
  of commentary that speaks of it: each is the container's text about
  the artifact, as written, one statement per passage. Nothing in it is
  normalised, corrected or translated, and a passage the instance cannot
  hold as it is (too long for a monolingual text, say) is refused and
  counted by the writer, never cut (rule 5).
- **How it is cited.** `stated in` the container with the page or slide
  the passage stands on (and its printed page number, ADR-0037), beside
  `inferred from extraction run` (rule 2).
- **Several containers.** A second container that places the same image
  adds its own `caption` statement, cited to it. A container that speaks
  of one image in two places gives two.
- **The rule.** `caption` is the only property whose subject may be an
  artifact the claim's source places, and it takes no other subject: its
  subject is an `artifact` reference, `{"kind": "artifact", "sha1": …}`,
  that the container's record of derived media names. Validation is
  handed the SHA-1s of that record beside the source's own key; the
  writer checks the subject against the derived media it registers for
  that source in the same write. From the command line (`yizkr ingest
  write`, `yizkr claims --validate`, `claims.py --validate`), the
  curator gives them with `--placed`: the container's record of derived
  media, or the source's SHA-1 and theirs (`<source>=<sha1>`), where the
  placement is the curator's word; the write still needs an item that
  carries each SHA-1. Without it every caption is refused, with a
  message that says how to give them (#551). A breach is a counted
  reject in validation and a refusal in the writer
  (`pre_plan_refusals`), named with its reason; the claim is the unit of
  rejection. No page
  constraint applies: a commentary on page 8 may speak of the image on
  page 6.
- The manifest marks it `placed_subject: true`, a field of its own, so
  that ADR-0035 §3.4's `document_subject` rule stays "the source itself".

#### 2.2 A source's own attributes

The manifest gains six properties marked `document_subject: true`,
beside `issued_by`. A source gives them only about itself, under ADR-0035
§3.4's rule as it stands, widened to these properties: the subject is the
claim's own source, and these properties take no other subject.

| Key | What the source states of itself | Accepts | Wikidata |
|---|---|---|---|
| `author` | who wrote, drew or made it | human, organization, family | P50 |
| `addressee` | whom it was addressed to | human, organization, family | P1817 |
| `inception` | when it was made | a time | P571 |
| `location_of_creation` | where it was made | place | P1071 |
| `main_subject` | what it is *about*: a letter's topic, the district a map covers, the family a booklet is about | any kind | P921 |
| `shows` | who or what the image shows, as its own content states it, such as an inscription on a print | any kind | none (below) |
| `issued_by` (already) | the organisation that issued it | organization | P2378 |

- Each accepts `unclassified_entity` too, wherever it accepts a real
  kind (ADR-0013). Dates take the qualifiers any date takes, at the
  precision the source gives (MODELING.md, "Uncertainty and time").
- **`shows`, not `main_subject`, for whom an image shows.** Naming every
  person in a group photograph as its "main subject" would stretch that
  property and mix what a work is about with whom an image shows. The
  portrait's reverse gives one `shows` statement per name, five in all,
  each cited to the portrait. A list gives no positions, and none is read
  into its order; a position an inscription words stays in the
  statement's transcription excerpt.
- **`shows` maps to no Wikidata property.** The nearest is P180
  *depicts*, which the manifest already maps to `depicts`; mapping both
  to P180 would let an export or a federated query read a source's
  inscription as a confirmed depiction.
- **Not `depicts`.** `depicts` stays ADR-0005's: confirmed by a person,
  tied to a region, written only through the faces flow, never in a
  scratch run. CLAUDE.md's rule that nothing writes `depicts` without a
  person confirming it is untouched, and the writer refuses an
  unapproved one anyway (ADR-0005, as amended by #226). The faces flow
  may later read a `shows` statement or a caption as a hint for whom to
  propose, outside the graph until a person confirms it.
- **A container never gives these about what it places.** The caption's
  facts stay its text (§2.1). From the keeper run on, a curator may
  promote one (§4).

### 3. How extraction is asked, and how the writer resolves the subject

**A container's prompt** (both the whole-document prompt and the chunked
one, ADR-0006) gains, beside the `issued_by` paragraph:

- the list of the artifacts the text the call reads places, each as its
  artifact reference and its place: `{"kind": "artifact", "sha1": "…"}`,
  page 6, image 1. The list is taken from the record, matching the
  markers in the transcription; no prompt names an artifact by its QID
  (CLAUDE.md, Engineering conventions);
- one instruction: give each passage of the container's own text that
  speaks of one of those artifacts (a caption, a heading over it, a
  sentence of commentary) as one `caption` claim, its subject that
  artifact's reference, its value the passage exactly as written, with
  the page it stands on; give nothing else about the artifact. A
  judgement note (ADR-0035 §4, `judgement_relation`) marks a passage the
  extraction read as speaking of an image where the text does not say
  so plainly, such as a slide's title over its one image.

**Every document's prompt** widens its paragraph on the document itself
from `issued_by` to the seven document-subject properties, each given
only where the document states it of itself.

**The caption's people** are entities of the container's batch, as now,
with their appellations and aliases (ADR-0018, #499): the caption's
"Wastl" is an alias and a `referred_to_as` of the local entity it names,
cited to the booklet, page 6. What the caption says about them as people
is extracted as today. The caption links them to the image only as
text.

**The subject's resolution.** The writer resolves an artifact subject as
it resolves a source (writer step 0): to the item that carries that
`media SHA-1`. The container's write registers the container, then every
image its record names, before it writes any claim (ADR-0008 as amended
by #103 and #397), so the subject has an item by then, whether or not
the image is an excerpt. An image the never-ingest list names is in no
record, so it is in no prompt, and a claim that names it anyway is
refused. An image whose bytes a document of the corpus has resolves to
that document's item (ADR-0010).

**Decks.** The same holds for a deck once #502 marks each slide's images
by SHA-1 in the deck's transcription (`[Slide 2, image 1: sha1:…]`), as
a PDF's pages mark theirs. A slide's text and its notes are the deck's,
cited to the deck with the slide as `page number`. Until then the deck's
prompt lists no images, so it gives no caption.

**Where nothing changes.** An image on a page read by vision is read as
that page, has no SHA-1 of its own and no item, and a caption beside it
gives no `caption` statement, as ADR-0008's question 3 leaves it. The
images that a PDF read whole holds are the same.

**The examples.** The booklet's batch gives one statement about the
letter's image:

| Subject | Property | Value | Source |
|---|---|---|---|
| the letter's image | `caption` | "Wastl to Klara, July 1939." (en) | booklet, page 6 (printed 4) |

The letter's image's batch gives its own attributes, with no page:

| Subject | Property | Value | Source |
|---|---|---|---|
| the letter's image (itself) | `inception` | 28 July 1939 (day) | the letter's image |
| the letter's image (itself) | `location_of_creation` | Brunnenring 35, Flörburg | the letter's image |
| the letter's image (itself) | `addressee` | Pünktchen (its own local entity) | the letter's image |

The letter's own claims of Rahel's address and of Gustav's visa are
untouched, as are the booklet translation's. The caption's year and its
"Wastl" stay its text: the letter's image carries no author statement,
since the top of the letter does not name its writer.

The deck's slide 2, once #502 marks its image, gives two `caption`
statements on the photograph's item, each stated in the deck, page 2:
"Wastl and Hilde" (with a judgement note, since a title over an image
is read as speaking of it) and "Photograph from the family album". The
photograph has no writing, so it is no excerpt and states nothing of
itself.

The studio portrait gives of itself, cited to the portrait: `author` →
Photograph Brüsrich, and `shows` → Muckel, Cilli, Wastl, Rahel and
Felix, one statement each.

### 4. Promotion and combination by a curator

The pipeline never turns a caption into structured statements, and never
builds a value from two sources. Before the keeper run (#159) no one
does: scratch graphs are never curated (ADR-0025). From the keeper run
on, a curator may:

- **promote a caption's fact** to a structured statement on the artifact
  item, such as `inception` → July 1939 from "Wastl to Klara, July
  1939.": `stated in` the container with the caption's page and the
  caption as its transcription excerpt, since the container does state
  it, with the curator's own provenance, at normal rank, beside the
  caption and the artifact's own statements (owner question 2);
- **combine** two sources' values (a caption's year with a letter's day
  and month, had the letter given only "28. Juli") as a new statement
  beside both (rule 3): two references, each with its own locator, and
  the qualifier `determination_method` (P459) → `combined_from_sources`,
  a manifest item, so that no reader takes the two references to mean
  that each source states the value. Frontends show it as "28 July
  1939, combined from the letter and the booklet's caption". This was
  accepted by the owner, 2026-10-16.

### 5. The container's text never reaches the excerpt's extraction

ADR-0008's question 2 is answered **no** (accepted by the owner,
2026-10-16): the excerpt's extraction reads the excerpt's text alone, and
is handed nothing a container says about it, not even as identity hints.

- The caption is kept, on the excerpt's item, as the container's (§2.1).
- Identity is decided where both mentions already meet: clustering reads
  the booklet's batch and the excerpt's together, and reconciliation
  (ADR-0007) and curation decide who the caption's "Wastl" is.
- One image is one excerpt and one extraction, whichever containers
  place it (ADR-0008 as amended by #397). A prompt that held a
  container's caption would hold every container's, and its cache key
  would change as containers are added (ADR-0032).
- A hint is never only a hint to a model: a caption's names in the
  excerpt's prompt would steer its readings, which the excerpt alone
  must earn (CLAUDE.md rules 1 and 5).

### 6. Clustering

Nothing changes. A `caption` is text and keys nothing; clustering reads
the caption's people as the container's mentions, as today. A source's
statements about itself (an `addressee`, a `shows`) are claims of its
batch like any other, whose values are its mentions; being a letter's
addressee says nothing of a person's dates or kin.

### 7. Privacy and display

**The non-privileged export.** A `caption` is a raw source quotation
that may name people, possibly living ones, only as text, which the
export cannot read. So caption statements stay out of the non-privileged
export, failing closed, as a transcription excerpt leaves only with a
statement it attests (ADR-0020, as amended by #364 and #412; owner
question 3). The document-subject properties need no new rule: a
statement whose value is an item left out goes whole. They mark no one
as a person: their values are like `depicts`'s, which mark no one
(ADR-0020 as amended by #344), since an author may be an organisation
and an image may show a building.

**The static site** (private; plural by default, CLAUDE.md rule 6):

- an artifact's page gains "What sources say about this artifact": its
  captions, each "per *Our Family from Germany*, page 6 (printed 4)",
  and its own statements, each "per this artifact", values that disagree
  listed together, none first;
- the container's page keeps its evidence table, in which its captions
  already appear, the image as their subject;
- a person's page gains "Named in artifacts": each statement whose value
  is that person, such as "shown in *Studio portrait of the Löbig
  family*, per the portrait", with the possibly-living badge as the rest
  of the page carries it. A caption links no one, so it is not listed
  there;
- an artifact's `inception` is shown on its own page, labelled per
  source; whether a person's timeline shows the letters they wrote or
  received is left for later.

### 8. What this costs and checks

- The manifest gains `caption` (with its `placed_subject` mark), the six
  document-subject properties, `determination_method` and
  `combined_from_sources`. The manifest, the prompts' paragraphs and the
  list of placed artifacts change the extraction stage:
  `stage:extraction` and `needs:scratch-diff` (ADR-0025 §4). Validation
  and the writer each check both rules (§2), and a live test writes a
  container's caption about its image and a document's statement about
  itself through the running stack (CLAUDE.md, Engineering conventions).
- The run report counts, as aggregates, the captions given, the
  document-subject claims by property, and the rejects by reason.
- The deck half waits on #502's markers; the PDF half does not.

### 9. Amendments (applied on acceptance, 2026-10-23)

**ADR-0008.**

- Question 2: answered by ADR-0039 §5. The excerpt's extraction reads
  the excerpt alone; what a container says about it is kept, verbatim, as
  the container's `caption` on the excerpt's item (§2.1).
- Question 6: answered by ADR-0039 §2. A container's claim about an
  excerpt is its `caption` only, its subject named by the excerpt's
  SHA-1 and resolved as a source is; what an excerpt is (its author,
  date, place, subject) is the excerpt's own claim about itself. The
  #289 amendment's sentence "A claim whose subject is the excerpt itself
  cannot be expressed yet" points here.
- The same holds for a deck's embedded images, once #502 marks them.

**ADR-0035 §3.4.** "`issued_by`" becomes "the document-subject properties
(`issued_by`, `author`, `addressee`, `inception`, `location_of_creation`,
`main_subject`, `shows`; ADR-0039)"; the subject is still the source
artifact itself. A paragraph adds `caption`, the one `placed_subject`
property, whose subject is an artifact the source's record of derived
media names. §3.5's table gains the rows of §2, and §3.6's vocabulary
gains them, `determination_method` and `combined_from_sources`.

**MODELING.md.**

- The paragraph on `document_subject` (core classes): it names the seven
  properties, and adds `caption` and its `placed_subject` rule.
- "Compound artifacts": a bullet that a container's words about an
  artifact it places are one verbatim `caption` statement on that
  artifact's item, `stated in` the container with its page; what the
  artifact is stays its own claims, which cite it.
- "Artifact coverage": "subject coverage reuses `depicts`" becomes
  "subject coverage uses `main subject`; who or what an image shows, as
  its own content states it, is `shows`; `depicts` is the faces flow's
  confirmed depiction (ADR-0005)".
- "Faces and depiction": a bullet that `shows` and `caption` are a
  source's words, never a depiction, and may seed a face proposal
  outside the graph.
- "Living persons and privacy": captions stay out of the non-privileged
  export.

## Owner questions

Each was settled by its recommendation when the owner accepted this ADR,
2026-10-23 ("I accept both ADRs"); the questions' text is kept as it was
asked.

1. **`main_subject` is what a work is about; `shows` is who or what an
   image shows, as its own content states it.** Recommended: both are
   document-subject properties (§2.2). A group photograph's inscription
   gives one `shows` per name, `shows` maps to no Wikidata property
   since P180 is `depicts`, and `depicts` stays the faces flow's
   confirmed, region-bound statement (ADR-0005).
   **Settled by its recommendation** (owner, 2026-10-23).
2. **Promoting a caption's fact.** Recommended: never by the pipeline;
   from the keeper run on, a curator may write a caption's fact as a
   structured statement on the artifact item, `stated in` the container
   with the caption's page and the caption as its excerpt, with the
   curator's provenance, beside the caption (§4).
   **Settled by its recommendation** (owner, 2026-10-23).
3. **Captions and the non-privileged export.** Recommended: caption
   statements stay out of it, since they may name possibly living people
   as text the export cannot check (§7).
   **Settled by its recommendation** (owner, 2026-10-23).
4. **This minimal design over the structured one drafted first.**
   Recommended: confirm it. A container gives only its verbatim caption,
   and every structured attribute is the inner source's own. Its cost:
   a fact only the caption gives (the year a letter lacks, the writer of
   a letter whose top names none) stays text on the image's item, not
   queryable data, until a curator promotes it.
   **Settled by its recommendation** (owner, 2026-10-23).

**Accepted by the owner, 2026-10-16:**

- the caption never reaches the excerpt's extraction, not even as
  identity hints (§5; ADR-0008 question 2);
- a claim's subject is its own source, or an artifact its source places
  by bytes, never one it only writes about: a narrative recalling a
  letter gives no claim about the held letter, and a curator may link
  them;
- combined values only by a curator, with two references and
  `determination_method` → `combined_from_sources` (§4).

The first draft's questions on clustering on a shared artifact role and
on a caption's class for an image no longer arise: a container makes no
structured claim about what it places.

## Consequences

- A caption is kept, verbatim and attributed, on the item a reader opens
  for the image, and a second container's caption beside it.
- A document's own claims about itself (a letter's date and place, its
  greeting's addressee, a portrait's studio and the names on its back)
  become statements on its own item.
- Two witnesses of one artifact stay separable: no reference cites a
  source for what it never said, no structured value about an artifact
  comes from any source but the artifact, and no excerpt's claim depends
  on a container's text.
- A fact only a caption gives is not queryable until a curator promotes
  it: "letters whose writer, per the compilation, is Sebastian" is a
  text search over captions, not a join.
- The manifest gains `caption`, six document-subject properties, a
  qualifier and its value; ADR-0035 §3.4's rule stays "the source
  itself", and one property, `caption`, takes what the source places.
- A deck's images gain captions only with #502; until then its 17 images
  have none.
- Captions beside images on scanned pages stay part of the page's text
  (ADR-0008 question 3).

## Alternatives considered

- **Structured container claims** (this ADR's first draft): a container
  states `author`, `addressee`, `inception`, `location_of_creation` and
  `main_subject` about an artifact it places, cited to the container.
  It makes a caption's facts queryable data at once, but it gives a
  container's view of its contents the same standing as the contents'
  own attributes, needs rules on which properties a container may state
  and on clustering over shared roles, and asks the model to turn a
  caption into structure. The owner preferred the caption as text, with
  structure kept for the inner source itself.
- **`main_subject` for whom an image shows.** Contrived for a group
  photograph, whose five named people are not five "main subjects".
- **The excerpt's extraction reads the caption** (ADR-0008 question 2,
  "yes"). It puts the caption's year into claims cited to the excerpt,
  which never states it (CLAUDE.md rules 1 and 5).
- **A local artifact entity in the container's batch**, minted as an
  item and later linked to the image. It makes a second item for an
  artifact that already has one, keyed by its bytes (ADR-0010).
- **`depicts` from captions or inscriptions.** It would make ADR-0005's
  writer rule tell a person's confirmed depiction from a source's words.
