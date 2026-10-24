# ADR-0039: Claims a container makes about the artifacts it places

Status: **accepted** (accepted by the owner, 2026-10-23; drafted for
#456, 2026-10-16). It answers ADR-0008's open questions 2 (context
across witnesses) and 6 (claims about an excerpt itself), and amends
ADR-0008, ADR-0035 §3.4 to §3.6 and MODELING.md, as §9 gives. The owner
accepted it as written, each owner question settled by its
recommendation (below). It covers both kinds of container: a PDF whose
pages place images (ADR-0008, as amended by #176 and #397) and a deck
whose slides embed them, which #502 makes excerpts too.
Reversed by #626, 2026-10-24: the recommendations of owner
questions 2 and 4 are reversed, and the 2026-10-16 subject ruling is
widened (§10).
Amended by #554 (2026-10-23): the critic's `relation_kind_mismatch`
category names `author` and `addressee`, as relations of authorship or
address, with the kinds they take (a person, an organization or a
family), as ADR-0031 records for the relations of agency.
Amended by #583 (2026-10-23; owner decision 2026-10-24, interim): on a
structured adapter's batch alone, §2.2's properties also take, as their
subject, a batch entity of an artifact kind, an un-held media item
(ADR-0021): the GEDCOM adapter gives an OBJE's own DATE as the
`inception` of the object's media item.
Replaced by #626, 2026-10-24: §10's general rule replaces this
interim exception: §2.2's properties take a batch entity of an artifact
kind as their subject on any batch, and an extraction may not name an
artifact kind (§10.1(b), §10.6).
Amended by #626 (2026-10-24; the owner's decision of 2026-10-24,
accepted by the owner's merge with the owner questions it raises
answered): a source may state a document-subject property about an
artifact it places by bytes, or about the artifact its own structured
record makes, cited to itself with its page and the passage as its
transcription excerpt, where the passage, read where it stands,
conveys the fact (ADR-0003's support test, as #607 writes it). It
reverses owner questions 2 and 4 and widens the 2026-10-16 subject
ruling: see §10. Each passage it reverses or widens keeps its text,
marked with a dated line.
Amended by #645 (2026-10-24; the owner's answers): questions 5 to 10
settled by their leans; §10.1(c) adopts (c4): a source may also state a
document-subject property about an artifact it only writes about, on its
own mention item of it (an `unclassified_entity`), linked to the held
scan later.
Amended by #670 (2026-10-24; the owner's decision on #670): a curator's
confirmation that such a mention, or the artifact a structured record
makes, is an existing item takes its document-subject claims onto that
item (§10.1(c), "A confirmed mention or record's artifact").
Amended by ADR-0040 (2026-10-24): §10.1(c)'s `main_subject` → "Rudolf's
death" reads `main_subject` → Rudolf, since no death item exists.

Revised 2026-10-16 on the owner's direction on #517, quoted: "Wait, are
these fields about a source's relevance to its container, or independent
details of a source's contents? Because if the former, I think it should
be very simple and minimal, probably just a single free text field, and
the other details should be assigned to the independent attributes of
the (inner) source itself." The first draft let a container state an
artifact's author, addressee and date; it is now an alternative
considered. A container gives only its own words for what it places, and
every structured attribute is the inner source's own.

Reversed by #626, 2026-10-24: the first draft's structured container
claims are adopted in part. The caption stays one verbatim text, and
every attribute an artifact states of itself stays its own; beside
them, a container states the seven document-subject properties of an
artifact it places where its passage, read where it stands, conveys
them, each claim labelled as the container's (§10).

## In brief

- **A container's claim about an artifact it places is its own words for
  it, and nothing else.** The caption under the letter's image in *Our
  Family from Germany*, "Wastl to Klara, July 1939.", is written
  verbatim as one `caption` statement on the image's item, `stated in`
  the booklet with its page.

  Widened by #626, 2026-10-24: the caption stays its own words, one
  verbatim statement. Beside it, where a passage of the container, read
  where it stands, conveys a fact of the artifact (who made it, for
  whom, when, where, what it is about, whom it shows, who issued it),
  the container
  states that too, cited to itself with the passage as its excerpt
  (§10). The caption gives `author` → Wastl and `addressee` → Klara;
  the booklet's translation of the letter's dateline gives `inception`
  → 28 July 1939.
- **Every structured attribute is the inner source's own.** The letter's
  date, place and addressee are the letter's claims about itself, cited
  to the letter's image, as `issued_by` is a certificate's claim about
  itself. A container never states them about what it places.

  Reversed by #626, 2026-10-24: every attribute an artifact states of
  itself stays its own, cited to it; a source that places the artifact,
  or whose structured record makes it, may state the same properties of
  it where its passage conveys them, each statement with its own
  reference, side by side (ADR-0002; §10.2).
- **The graph joins the two on the image's item.** The booklet's caption
  and the letter's own statements stand side by side there, each
  attributed to its own source (ADR-0002).
- **`main_subject` is what a work is about; `shows` is who or what an
  image shows, as its own content says.** The studio portrait's reverse
  names five people: five `shows` statements, cited to the portrait.
  `depicts` stays the faces flow's (ADR-0005).

  Widened by #626, 2026-10-24: `shows` may also come from a source that
  places the image, where its passage conveys it: slide 2's title gives
  `shows` → Wastl and `shows` → Hilde (§10.4, on its lean).
- **The caption's people stay the container's entities**, with their
  appellations (#499), as today. The caption links them to the image only
  as text.

  Reversed by #626, 2026-10-24: where the caption conveys it, its
  people are also the image's `author` or `addressee`, item values:
  "Wastl to Klara" gives `author` → Wastl and `addressee` → Klara
  (§10.4, on its lean).
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

Widened by #626, 2026-10-24: what an artifact is stays its own claim
about itself, and a container's passage that, read where it stands,
conveys what the artifact is is also the container's claim about it,
cited to the container with its page and the passage as its excerpt
(§10).

- Each source keeps its own reference (CLAUDE.md rule 1, ADR-0003): the
  caption cites the booklet, page 6; the letter's statements about
  itself cite the letter's image.
- Nothing crosses: the caption's "July 1939" is never the letter's date,
  and the letter's "28. Juli 1939" is never the booklet's (rule 5).
  Reversed by #626, 2026-10-24: the caption's "July 1939" is still
  never the letter's date, now because the caption does not say what
  its July 1939 dates (§10.4), not because a container may give no
  date: the booklet's translation of the dateline gives the image
  `inception` → 28 July 1939, cited to the booklet, page 6. The
  letter's "28. Juli 1939" is still never the booklet's date.
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
  Widened by #626, 2026-10-24: `caption` is no longer the only
  property whose subject may be an artifact its source places: the
  seven document-subject properties take one too (§2.2, §10.1(a)).
  `caption` still takes no other subject, never the source itself, and
  the placement check above (the record, `--placed`, the writer's
  registered media) is the one those properties' form (a) uses.
- The manifest marks it `placed_subject: true`, a field of its own, so
  that ADR-0035 §3.4's `document_subject` rule stays "the source itself".
  Widened by #626, 2026-10-24: the flag stays `caption`'s alone, but
  `document_subject` no longer means "the source itself" alone: it takes
  the source, an artifact the source places, or a batch entity of an
  artifact kind (§10.5).

#### 2.2 A source's own attributes

The manifest gains six properties marked `document_subject: true`,
beside `issued_by`. A source gives them only about itself, under ADR-0035
§3.4's rule as it stands, widened to these properties: the subject is the
claim's own source, and these properties take no other subject.

Widened by #626, 2026-10-24: a source gives these about itself, about
an artifact it places by bytes, and about the artifact its own
structured record makes (a batch entity of an artifact kind), each only
where its passage, read where it stands, conveys the fact (§10.1).

| Key | What the source states of itself | Accepts | Wikidata |
|---|---|---|---|
| `author` | who wrote, drew or made it | human, organization, family | P50 |
| `addressee` | whom it was addressed to | human, organization, family | P1817 |
| `inception` | when it was made | a time | P571 |
| `location_of_creation` | where it was made | place | P1071 |
| `main_subject` | what it is *about*: a letter's topic, the district a map covers, the family a booklet is about | any kind | P921 |
| `shows` | who or what the image shows, as its own content states it, such as an inscription on a print | any kind | none (below) |
| `issued_by` (already) | the organisation that issued it | organization | P2378 |

Widened by #626, 2026-10-24: each property in this table, `shows`
included, may also be stated by a source that places the artifact or
whose structured record makes it, where its passage conveys the fact
(§10); `shows` is then who or what the image shows as that source's
passage conveys it, not only as its own content states it.

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
- **An export's own record of an object, an interim exception**
  (amended by #583; owner decision 2026-10-24). An export's structured
  OBJE record is the export's own record of the object it links (ADR-0021),
  not words a container places beside an image, so the GEDCOM adapter
  gives an OBJE's own DATE as the `inception` of the object's un-held
  media item, with the range and circa the date states, citing the
  export. Validation and the writer take such a subject, a batch entity
  of an artifact kind (`artifact` or a `subclass_of` it), on these
  properties only on a structured adapter's batch: one whose extraction
  run's model identifier is in `STRUCTURED_ADAPTERS` (`claims.py`), which
  code sets and no model does. A model's batch keeps the rule above
  exactly, whatever kind its entity manifest gives an entity. The owner
  has asked for an amendment that would make this the general rule: a
  source may state these properties about an artifact it places or
  describes, cited to itself, where its passage conveys the fact.
  Replaced by #626, 2026-10-24: §10's general rule replaces this
  exception. A batch entity of an artifact kind may be these
  properties' subject on any batch, and an extraction may not name an
  artifact kind, so such an entity exists only where a structured
  record or a hand-written batch makes one (§10.1(b), §10.6).
- **A container never gives these about what it places.** The caption's
  facts stay its text (§2.1). From the keeper run on, a curator may
  promote one (§4).
  Reversed by #626, 2026-10-24: a container gives these about what
  it places where its passage conveys them, beside the caption (§10).

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

Reversed by #626, 2026-10-24: "give nothing else about the artifact"
becomes: give a document-subject claim about a listed artifact where
the container's passage, read where it stands, conveys it, with the
passage as its excerpt and its page; a translation of the artifact's
own text gives these claims and no caption (§10.5).

**Every document's prompt** widens its paragraph on the document itself
from `issued_by` to the seven document-subject properties, each given
only where the document states it of itself.

Widened by #626, 2026-10-24: the paragraph's subject sentence widens to
the listed artifacts; "only where the document states it of itself"
stays for the document's claims about itself (§10.5).

**The caption's people** are entities of the container's batch, as now,
with their appellations and aliases (ADR-0018, #499): the caption's
"Wastl" is an alias and a `referred_to_as` of the local entity it names,
cited to the booklet, page 6. What the caption says about them as people
is extracted as today. The caption links them to the image only as
text.

Reversed by #626, 2026-10-24: where the caption, read where it stands,
conveys it, its people are also the image's `author` or `addressee`, as
item values, beside the caption (§10.4, on its lean).

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

Widened by #626, 2026-10-24: the booklet's batch gives five
structured statements beside its caption, on the leans of §10.4:

| Subject | Property | Value | Source |
|---|---|---|---|
| the letter's image | `caption` | "Wastl to Klara, July 1939." (en) | booklet, page 6 (printed 4) |
| the letter's image | `author` | Wastl (the booklet's local entity) | booklet, page 6 (printed 4), excerpt "Wastl to Klara, July 1939." |
| the letter's image | `addressee` | Klara (the booklet's local entity) | booklet, page 6 (printed 4), excerpt "Wastl to Klara, July 1939." |
| the letter's image | `inception` | 28 July 1939 (day) | booklet, page 6 (printed 4), the translation, `machine_assisted` |
| the letter's image | `location_of_creation` | Brunnenring 35 (the booklet's local entity) | booklet, page 6 (printed 4), the translation, `machine_assisted` |
| the letter's image | `addressee` | Pünktchen (the booklet's local entity) | booklet, page 6 (printed 4), the translation, `machine_assisted` |

The letter's image's own statements are unchanged. The caption's "July
1939" gives no `inception`, and the caption stays. The letter's image
now carries an `author` statement, the booklet's; it still states none
of itself. Which of the booklet's mentions are one person, and who
they are, is clustering's and reconciliation's, as before.

The deck's slide 2, once #502 marks its image, gives two `caption`
statements on the photograph's item, each stated in the deck, page 2:
"Wastl and Hilde" (with a judgement note, since a title over an image
is read as speaking of it) and "Photograph from the family album". The
photograph has no writing, so it is no excerpt and states nothing of
itself.

Widened by #626, 2026-10-24: slide 2's title also gives `shows` →
Wastl and `shows` → Hilde on the photograph's item, each stated in the
deck, page 2, with the title as its excerpt and a judgement note of
category `relation` (§10.4). "Photograph from the family album" says
where the photograph was kept, not what it is, and gives no structured
claim.

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

Reversed by #626, 2026-10-24: the pipeline gives what a passage conveys
(§10), so "never turns a caption into structured statements" no longer
holds: a caption that conveys a fact gives the caption and the
structured claim. From the keeper run on, a curator may still write a
fact the extraction missed, under the same support test, with the
curator's provenance. Combination is unchanged: the pipeline still
never builds a value from two sources.

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

Widened by #626, 2026-10-24: the deterministic rules read none of
these properties and stay unchanged; adjudication sees them, so a
booklet mention named as the `author` or `addressee` of the letter's
image gains a line and its pairs are asked again (§10.5).

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

Widened by #626, 2026-10-24: a document-subject statement about an
artifact its source places may carry a caption's text as its excerpt,
so it is left out of the export whole, failing closed, as owner
question 9 settled it (§10.5); a statement about an artifact a structured record makes is
exported or left out as any of that record's statements is.

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

Widened by #626, 2026-10-24: "What sources say about this artifact"
also lists what other sources state of it, each value labelled by its
source and page, every value of one property together, none first; a
person's "Named in artifacts" labels values the same way; an un-held
media item's page shows its `inception` (§10.2).

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

Widened by #626, 2026-10-24: §10.5 gives what the amendment costs and
checks: new prompt paragraphs (every chunk call and every critique
asked again), the extraction-stage kind check, the widened subject
rule, the report's split by subject form, the export rule and the site
wording. Its build is a follow-up issue of its own, `stage:extraction`
and `needs:scratch-diff`, with a live test that writes a container's
`inception` about its placed image and an export's `inception` about an
un-held media item.

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

### 10. What a source's passage conveys about another artifact (amended by #626, 2026-10-24)

The owner asked, on 2026-10-24: "what happens to the date in the letter
example? It seems right that the date be associated with the photo in a
suitably expressive way. Isn't it an Inception claim made by the letter
about the photo? Shouldn't GEDCOM be the same?" The owner then decided
(#626):

- A source may state a document-subject property (`inception`,
  `author`, `addressee`, `location_of_creation`, `main_subject`, `shows`,
  `issued_by`) about an artifact it places or describes.
- The claim is cited to that source, with its page and the quoted
  passage as its transcription excerpt, beside the extraction run
  (CLAUDE.md rules 1 and 2).
- It is allowed only where the passage, read where it stands in the
  source, conveys the fact. This is the one support test the owner
  decided on #607 (2026-10-23, Q2), which #607 writes into ADR-0003.
  There is no test of its own (§10.4).
- GEDCOM's OBJE DATE follows the same rule, with no adapter exception
  (§10.6).
- The cost is accepted. The extraction prompts change, so every chunk
  call is asked again in the next run, which also takes #625 and any
  manifest fix from #574 (§10.5).

So three subject forms are decided: (a), (b) and (c4) below. Where this
section followed a lean, the owner question that names it is listed
under Owner questions ("Raised by #626"); each was settled by its lean
(owner, 2026-10-24, #645). The build is a follow-up issue of its own (`stage:extraction`,
`needs:scratch-diff`), after #607's located citations; until it lands,
the prompts, validation and the writer keep §2.2's rule and #583's
interim exception.

#### 10.1 Which subject a claim about another artifact may name

**(a) An artifact its source places by bytes (decided).** This is an
image the container's record of derived media names, the subject
`caption` takes (§2.1).

- Identity is already settled. The SHA-1 is in the record and in the
  transcription's markers. The prompts list it (`extract.placed_lines`),
  validation is handed it (`placed`), and the writer checks that an item
  carries it (`resolved`).
- Which artifact the properties describe. A placed image's
  document-subject properties describe what the image's own claims make
  it: the original it reproduces. The letter image's own `inception` is
  the letter's dateline (§3), not the day the booklet scanned it, and a
  container's `inception` of it means the same.
- A container's words about the reproduction (who scanned or
  photographed it, and when) give no document-subject claim. They fail
  the support test for that artifact (§10.4), and stay a caption where
  they speak of the image. A line the booklet does not have, "Scanned by
  Nicholas for this booklet, 2010.", would give no `inception` → 2010
  and no `author` → Nicholas. Otherwise `inception`, which is
  single-valued, would list 2010 against 1939 as a false conflict.

**(b) The artifact a source's own structured record makes (decided).**
This is a GEDCOM OBJE, which the adapter makes a batch entity of an
artifact kind: an un-held media item (ADR-0021 §2).

- Identity within the export is the object's xref, so several people's
  links share one item.
- Across documents, the un-held item is not the held scan of the same
  photograph. The fixture export's `@O1@` ("Studio portrait of the Löbig
  family", `DATE BET 1890 AND 1899`) and the universe's
  `studio-photo-1890s` stay two items until a curator links them
  (ADR-0010 §2–3, deferred; #162). The registrar flags such a pair
  `unhashed-title` when the scan's title is the object's label. So the
  export's `inception` stands on the un-held item, and meets the scan's
  own only after that link.
- This widens the 2026-10-16 ruling ("never one it only writes about"),
  since the export holds no bytes of the object.
- **How (b) is keyed and enforced** (owner question 5, settled by its
  lean, owner, 2026-10-24). Keyed on the subject's kind alone, a model's batch could
  make an artifact-kind entity and state things about it: route (c2)
  under another name. Today only the structured-batch flag keeps
  document-subject claims off such an entity; nothing stops a model's
  batch from making the entity itself.
  `claims.validate_against_manifest` refuses an entity's kind only when
  it is no manifest item, and checks a `LocalRef`'s `instance_of` the
  same way; `ChunkedClaimExtractor._parse_entity_manifest` checks a
  record's kind against every manifest item, not only the kinds the
  prompt offers. A manifest record could type the 1952 letter's
  "portret fan dyn dochter Florence har lytse Nicholas" as `photograph`,
  with `shows` → Nicholas on it; that would pass, and the writer would
  mint a second artifact item. So:
  - an extraction may not name an artifact kind
    (`Acceptance.artifact_kinds`), whether in a manifest record or in a
    `LocalRef`'s `instance_of`. The check sits where model output is
    first read: `_parse_entity_manifest` treats such a kind as it treats
    an unknown kind, and `extract.salvage_valid_claims` rejects a claim
    whose reference names one, counted under a reason of its own;
    as built (#646, 2026-10-24), the check also covers a claim of
    `instance_of` or `subclass_of` whose value is an artifact kind,
    which would make its subject an artifact item, beside the manifest
    record and the `LocalRef`'s `instance_of`;
  - the subject rule (`claims.subject_problems`,
    `writer.subject_refusal`) then stays keyed on the subject's kind. An
    artifact-kind batch entity exists only where a structured record or
    a hand-written batch makes one. The manifest prompt already says
    that a letter or certificate a source mentions is an
    `unclassified_entity` with a type_hint, so the check enforces what
    the prompt asks.

  The alternative: refuse every kind the prompts do not offer, not only
  artifact kinds.
  The two differ on `artifact_fragment`. It is no artifact kind
  (`Acceptance.artifact_kinds`), so the lean lets a model's entity take
  it, as `_parse_entity_manifest` does today, though
  `writer.is_artifact_kind` treats it as artifact-side; the
  alternative refuses it unless a prompt offers it.

**(c) An artifact the source only writes about** (owner question 6,
settled by its lean: (c4), owner, 2026-10-24), such
as an enclosure held as a scan of its own. The 1958 letter says "Вы
просили прислать свидетельство о его смерти, посылаю." ("You asked for
his death certificate: I enclose it."). The certificate is a document of
its own (`death-certificate-1941`), and the letter conveys what it is
about (`main_subject` → Rudolf's death). Today the letter's manifest
names the certificate as an `unclassified_entity` with the type_hint
"death certificate" (`chunking.ENTITY_MANIFEST_PROMPT`). The letter's
extraction cannot name the held certificate: stages before write never
read the wiki, and a prompt names no artifact by QID, only its own
document and what that document places, by SHA-1. There are four
routes:

Amended by ADR-0040, 2026-10-24: "`main_subject` → Rudolf's death" reads
"`main_subject` → Rudolf", since no death item exists (ADR-0040 §2.9).

- **(c1) A curator's record of what a document encloses**, by the two
  SHA-1s (`<source>=<sha1>`, as `--placed` takes a curator's placement,
  §2.1). It needs:
  - a local-only record kept beside the corpus, never in the
    repository, since it identifies corpus pairs, with a CLI and an
    input to the scratch driver;
  - provenance for the identification. The letter never names the scan;
    that the enclosed certificate is this scan is the curator's
    judgement, an assertion of its own. Either each such statement
    carries a second reference naming the curator's record, with the
    curator's provenance (as §4's combined values carry two references),
    or a curator-cited statement links the scan to the letter, and the
    claims depend on it;
  - a write order or resolution rule. (a)'s writer check works because
    the container's write registers its placed media before its claims.
    An enclosure is registered by its own write, so the letter's claim
    is refused unless the enclosure was written first, or it waits and
    is counted;
  - a prompt paragraph of its own for the enclosures a record names: an
    enclosure has no `[Page N, image M: sha1:…]` marker, so it is not in
    the marker-based placed list (`extract.placed_lines`);
  - a widening of the 2026-10-16 ruling to "an artifact a curator's
    record says its source encloses".

  Only the prompts of documents with a record change.
- **(c2) An artifact-kind entity in the letter's batch**, linked to the
  held scan later. It makes a second item for an artifact that already
  has one: "A local artifact entity in the container's batch", which
  this ADR rejected and still rejects (Alternatives). Nothing matches
  artifacts across documents yet. (b)'s kind check keeps this route
  shut.
- **(c3) No claim on the enclosure's item.** The 2026-10-16 ruling stands
  for this case. From the keeper run on, a curator links the letter's
  local entity to the held scan. It needs no new provenance form.
- **(c4) The claim on the letter's own mention of the enclosure.** The
  letter's batch already names the enclosure as an `unclassified_entity`
  with a type_hint ("death certificate", or "photograph"), and the write
  makes it an item (ADR-0013). The letter's document-subject claim
  (`main_subject` → Rudolf's death; for an enclosed photograph,
  `inception` → the year the letter gives) goes on that mention item,
  cited to the letter with its passage. A curator, or later artifact
  matching (#162), links the mention item to the held scan; until then
  the claim sits on the letter's own record of what it encloses.
  (Amended by ADR-0040, 2026-10-24: `main_subject` → Rudolf, not
  "Rudolf's death", since no death item exists.)
  - It adds no item beyond the mention item today's prompt already
    makes, and needs no curator record before the write.
  - It widens the 2026-10-16 ruling to an artifact the source writes
    about, as the owner's question ("an Inception claim made by the
    letter about the photo") suggests.
  - Validation and the writer then accept an `unclassified_entity` (an
    artifact-kind type_hint on one included) as a document-subject
    subject, under the support test. (b)'s kind check is unaffected,
    since the entity is of no artifact kind.
  - Readers see the claim on the mention item, not on the scan, until
    the two are linked.
  - Its breadth: a `type_hint` is free text, so the subject rule would
    accept any `unclassified_entity` (an aircraft, a regiment), with
    only the advisory critic as a guard.
  - The mention item is no artifact item, so it gets no artifact page.
  - Like the rejected (c2), it puts artifact claims on a second item;
    it differs in making no item beyond today's mention item and in
    minting no artifact-kind entity.

Lean: (c4) if the owner wants the letter's claim recorded now, since it
is the closest to the owner's question and needs no curator record;
(c3) if not. Either way, (c1) and its needs above are the route a later
amendment may take, in an issue of its own. The owner adopted (c4)
(2026-10-24, #645, question 6): the enclosing source's claim goes on its
own mention item of the enclosure, cited to the source with its passage;
a curator, or later artifact matching (#162), links that mention item
to the held scan. The rule admits forms (a), (b) and (c4).

**A confirmed mention or record's artifact** (#670; owner decision,
2026-10-24: "yes, a confirmed mention takes its document-subject claims
onto the existing item"). A curator who confirms that a batch's entity
is an existing item (an approved equivalence:
`reconcile.apply_equivalences` sets the entity record's `qid` and turns
its local refs into that QID) links it as this section says. When the
claim's subject before confirmation was a (c4) mention or a (b)
record's artifact, the confirmed item is its subject, and the claim is
written there, cited to the source with its passage, as a confirmed
person match moves a document's claims onto the existing person. So
the 1958 letter's `main_subject`, on its mention of the enclosed death
certificate, goes onto the held scan
death-certificate-1941 once a curator confirms the mention is that
scan's item.

- Each claim keeps its own pre-confirmation subject. When
  `reconcile.apply_equivalences` replaces a claim's local subject with
  the confirmed QID, it records that local ref on the claim
  (`Claim.confirmed_from`), its `instance_of` set to the entity's kind:
  its manifest record's, else the ref's own, so a manifest-free mention
  keeps its kind. The record is kept through serialization and on later
  equivalence passes. It is resolution state, never extraction output:
  the prompts' JSON Schema leaves it out, `extract.cite_artifact` drops
  it, and a claim without one serialises as before. Where resolution leaves
  claims the same but for their records, they are kept once
  (`reconcile.collapse`, which `critique.counterparts` replays): the
  claim kept is the first whose record makes its subject a document
  subject, else the first, so a claim that named the QID outright, or
  whose subject was a person, never displaces a confirmed mention's
  claim, in either order.
- The subject form is `confirmed` (`claims.CONFIRMED_FORM`): a QID
  subject whose claim records a local subject of an artifact kind or an
  `unclassified_entity` (`claims.confirmed_kind`). Where the batch holds
  that entity's record, the kind is the record's, and the record must be
  confirmed as the same QID. Validation (`claims.subject_problems`) and
  the writer (`writer.subject_refusal`) take it, and no other QID
  subject: a claim that named a QID outright records no local subject
  and stays refused, however many entities are later confirmed as that
  item; so do a claim whose subject was a person, place or event, and
  one whose recorded entity is confirmed as another item. Each fails
  closed.
- It is held to (c4)'s and (b)'s other rules: a claim of this form
  carries the passage that conveys it as its excerpt (§10.4).
- The run report counts it (`container_claims.document_subject_by_form`:
  `confirmed`), reading a document's subject forms from its resolved
  batch (`batch.resolved.json`, where the confirmation is saved) when
  there is one, else from its stage's batch view.
- It arises only from the keeper run on, since scratch runs confirm no
  matches. Such a claim, on a held scan and stated in another document,
  then falls under the export's SHA-1 rule (`backup.placed_subject_rule`,
  §10.5) and is left out of the non-privileged export, failing closed.

#### 10.2 Coexistence, disagreement and display

- These claims stand beside the artifact's own, each with its own
  reference, at normal rank (ADR-0002; CLAUDE.md rules 4 and 6). None
  replaces another.
- **Disagreement.** `inception` is in `disagreement.SINGLE_VALUED`, and
  `disagreement.compared_values` already compares the inceptions
  different sources give one artifact item: it sets aside a difference
  of resolution and lists the rest together. No code changes; the build
  pins this with a test. The other six properties are not compared, and
  stay so, since a letter may have two authors.
- **The booklet's translation.** Page 6 reads "Translation: Flörburg,
  Brunnenring 35, 28 July 1939. Dear Pünktchen!". Read where it stands,
  beside the letter's image, it conveys the letter's date, place and
  addressee.
  - It gives `inception` → 28 July 1939 (day), `location_of_creation` →
    Brunnenring 35 and `addressee` → the booklet's Pünktchen. Each goes
    on the letter image's item, stated in the booklet, page 6 (printed
    4), with `sourcing_circumstances` → `machine_assisted` (ADR-0008, as
    amended by #289).
  - A translation of the image's own text is no caption. Page 6's only
    caption is "Wastl to Klara, July 1939.", so the translation gives
    the structured claims only, and §2.1's caption rule is unchanged.
  - These claims agree with the letter's own. Had the translation
    misread the year, as it misreads Angerring 11 as 71, the two
    inceptions would be listed together, none first.
  - Every translated excerpt gains such claims.
- **The site.** `staticsite.SiteBuilder.citations` already labels a
  statement by its source: another source is a link with its page and
  printed page ("per *Our Family from Germany*, page 6 (printed 4)"),
  and the artifact's own statement says "this artifact".
  - `sources_say_html` groups by property, so every value of one
    property is listed together. Only its sub-heading changes: it names
    captions and what the artifact states of itself, and gains "and what
    other sources state of it".
  - `named_in_html` labels values the same way.
  - An un-held media item gets an artifact page that shows its
    `inception` with its other statements; the build tests it.

#### 10.3 `caption` and §4

- `caption` is unchanged: one verbatim statement per passage, on the
  placed artifact (§2.1). A caption that conveys a structured fact gives
  both the caption and the structured claim, with the same passage as
  the claim's excerpt. A translation gives no caption (§10.2).
- `caption` stays the one `placed_subject` property, and it never takes
  the source itself.
- §4's promotion. The pipeline now gives what a passage conveys, so
  owner question 2's "never by the pipeline" is reversed. From the
  keeper run on, a curator may still write a fact the extraction
  missed, under the same support test, with the curator's provenance.
  Combination (§4's second bullet) is unchanged.
- §5 is unchanged. The excerpt's extraction reads the excerpt alone, and
  the container's claims never reach it.

#### 10.4 The support test, calibration and the critic

- **The test** is ADR-0003's, as #607 writes it: a citation supports a
  statement when the passage, read where it stands, conveys the fact the
  statement records. For a claim about another artifact, the fact
  includes the subject: the passage, read where it stands, must convey
  the fact of that artifact.
- **Calibration** (owner question 7, three readings, each settled by
  its lean, owner, 2026-10-24):
  - "Wastl to Klara" under the letter's image gives `author` → Wastl and
    `addressee` → Klara. Lean: yes. Under a letter, "X to Y" names its
    writer and its recipient.
  - "July 1939" in the same caption gives no `inception`. Lean: no. The
    names fill the two roles that "X to Y" names, whatever event the
    date marks; the date is tied to no event. It may be when the letter
    was written, sent or received, so reading it as the date of making
    narrows what the caption says (CLAUDE.md rule 5). The caption stays.
    The alternative: yes, `inception` → July 1939 (month), cited to page
    6 with the caption as its excerpt; it would agree with the letter's
    own 28 July 1939, since a day within the month differs only in
    resolution.
  - Slide 2's title "Wastl and Hilde", over its one image, gives `shows`
    → Wastl and `shows` → Hilde. Lean: yes, each with a judgement note
    of category `relation`, as its caption already carries (§3). #607's
    test asks whether the passage is reasonably understood to convey the
    fact, and a title over a slide's one photograph is. The note records
    that the reading is the extraction's.
- **The critic.** #607's `excerpt_does_not_state` covers these claims,
  so there is no new key, and findings stay advisory.
  `critique._ref_id` shows an artifact subject by its content key, so
  the critic cannot tell the image from the document. The row names the
  source itself as "this document" and a placed artifact by its place
  ("page 6, image 1"), as the prompt lists it; a finding that names a
  claim this way still resolves to it.
- **Excerpt required** (owner question 8, settled by its lean, owner,
  2026-10-24). A
  document-subject claim whose subject is not its own source must carry
  an excerpt, since without one no test can apply. Validation rejects
  it under a new `claims.SUBJECT_REASONS` code, counted in
  `rejects.by_reason`; the writer refuses it as `subject_rule`, with its
  own message.
- **GEDCOM.** No critic reads a GEDCOM batch (`orchestrate._derive`: "a
  GEDCOM file has only its batch"). The adapter meets the test through
  its mapping: an OBJE's DATE stands in the object's own record, so it
  conveys that object's date of making. ADR-0035 §6's table records this
  reading.

#### 10.5 Prompts, validation, clustering, report, privacy and cost

- **Prompts and manifest text.**
  - `extract.DOCUMENT_ITSELF`: the subject sentence ("and never anything
    else") widens to the listed images. "each only where the document
    states it of itself, never from what another document says of it"
    stays, for the document's claims about itself (§5).
  - `extract.PLACED_ARTIFACTS`: "Give nothing else about an image …"
    becomes: give a document-subject claim about a listed image where
    THIS document's passage, read where it stands, conveys it, with the
    passage as its excerpt and its page. A translation of the image's
    text gives these claims and no caption. The caption's date and the
    reproduction line are its failing examples.
  - `chunking.LINE_ASSIGNMENT_BLOCK` and `chunking.ENTITY_ASSIGNMENT_BLOCK`
    assign these claims as they assign captions.
  - `model/manifest.yaml`: the seven descriptions drop "the subject is
    the source document itself". The five "as the document itself
    states", and `shows`' "as its own content names them", are reworded
    to cover a source that conveys them about an artifact it places. The
    section comments follow ("The subject is the source document itself"
    above `issued_by`, and "a source's claims about itself").
  - `EXTRACTION_PROMPT` gets the same paragraphs through
    `extract.document_block`.
- **Validation and the writer.** On a document-subject property,
  `claims.subject_problems` and `writer.subject_refusal` accept three
  subjects: the claim's own source; an artifact its source places
  (`placed`, plus `resolved` in the writer); a batch entity of an
  artifact kind (`subject_kind` in `Acceptance.artifact_kinds`); and,
  under (c4), an `unclassified_entity` of the claim's own batch
  (§10.1(c)). Beside them:
  - the extraction-stage kind check of §10.1(b);
  - `claims.unplaced_captions` (renamed if need be) and
    `claims.unplaced_hint` count every claim whose subject is an
    artifact other than its source and whose source has no placed media
    given, not only captions;
  - the manifest flags keep their names: `document_subject` widens, and
    `placed_subject` stays `caption`'s alone;
  - the docstrings and comments follow: the module docstring of
    `claims.py`, step 0 in `writer.py`, and in `model/schema.py` the
    comments on `PropertySpec.document_subject` and
    `Acceptance.document_subjects`.
- **Clustering.** Its deterministic rules read none of these
  properties, so they are unchanged. Adjudication does see them:
  `adjudication._Document` puts every claim whose value is a local
  entity into `naming`, and `lines()` renders it as "named as the
  {property} of {subject}". So a booklet mention named as the `author`
  or `addressee` of the letter's image gains a line, and its pairs are
  asked again. `_Document.named` renders an `ArtifactRef` subject as "an
  entity", and keeps doing so in the build, so no clustering-stage
  template changes; better wording is a `stage:clustering` change of its
  own. The scratch diff states the adjudication misses.
  Amended by #667: `_Document.named` now names an artifact subject
  plainly, its own document as "this document" and an image the
  document places by its place ("page 6, image 1", as the critic's
  rows do, `critique.artifact_places`), read from the document's
  transcription view; an artifact without a place is named by the
  SHA-1 of its media. The stage key covers those places and the code
  digest the naming, so the pairs whose evidence names an artifact
  subject are asked again.
- **Report.** `report._container_claims` receives a per-property count.
  The claims fold that builds it also counts document-subject claims by
  subject form: the source itself, an artifact it places, an artifact
  its record makes, or, under (c4), the source's mention item of an
  artifact it only writes about. Aggregates only. docs/RUN_REPORT.md documents the
  split and any new reject reason.
- **Privacy** (owner question 9, settled by its lean, owner,
  2026-10-24).
  - Captions stay out of the non-privileged export (§7,
    `backup.LEFT_OUT_PROPERTIES`).
  - A form-(a) statement's excerpt may be a caption's own text ("Wastl
    to Klara", as an `author` excerpt). Under ADR-0020 an excerpt leaves
    with the statement it attests, so the caption's text would leave
    though the caption does not.
  - Against that: every statement's excerpt already leaves with it, and
    may name a possibly living relative. The owner was shown that
    consequence with the rule on 2026-09-29 and did not object
    (MODELING.md, "Living persons and privacy").
  - Lean: fail closed. `backup.filter_nonprivileged_export` leaves out
    whole a document-subject statement whose subject is `part of` the
    item its reference's `stated in` names, as a placed image is of its
    container: a per-statement test over the dump's triples, beside
    today's by-property `left_out_pids`. The alternative: no new rule.
  - A form-(b) statement leaves as any GEDCOM statement does, since its
    subject is not part of the export.
- **Cost**, from run 3's aggregates (#607):
  - A change to the chunk prompt, or to the manifest text it embeds,
    re-asks the 127 chunk calls and every critique (73).
  - Manifest turns stay cached under (a) and (b), since
    `ENTITY_MANIFEST_PROMPT` is unchanged. A cached manifest that names
    an artifact kind now fails §10.1(b)'s check and is asked again.
  - #625 changes `ENTITY_MANIFEST_PROMPT`, which alone re-asks every
    extraction call (205). So in that run this change adds no extraction
    call.
  - Adjudication pairs whose mentions gain lines are asked again.
  - (c1) would change only the prompts of documents that have a record.

#### 10.6 What replaces the interim exception of #583

- Rule (b) keys on the subject's kind, guarded by §10.1(b)'s
  extraction-stage check, not on the run's model identifier.
  - The build removes `claims.STRUCTURED_ADAPTERS`,
    `claims.GEDCOM_ADAPTER` and `claims.structured_batch`, and the
    `structured` argument of `subject_problems`, `subject_refusal` and
    their callers.
  - It keeps `subject_kind` and `Acceptance.artifact_kinds`.
  - `gedcom.ADAPTER_IDENTIFIER` stays the batch's model identifier.
- The GEDCOM's claims do not change, so this part adds nothing to the
  scratch diff.
- The test that a model's batch with the same entity is refused becomes
  a pair: a model's extraction cannot make an artifact-kind entity (it
  is rejected in `_parse_entity_manifest` and `salvage_valid_claims`,
  before the subject rule is reached); and a batch the adapter did not
  make passes the subject rule with an artifact-kind entity, one test on
  a hand-written batch (`yizkr ingest extract --engine file`, model
  identifier "manual") and one on the writer path through
  `pre_plan_refusals`.
- The interim text keeps its own text, with a dated line saying the
  general rule replaces it: this ADR's #583 status line and §2.2's
  bullet "An export's own record of an object, an interim exception";
  ADR-0021's #583 amendment; ADR-0035's #583 line; MODELING.md's
  `document_subject` paragraph.

**As built (#646, 2026-10-24),** with the owner's answers to questions
5 to 10 (each its lean; (c4) for an enclosure):

- **The subject forms.** `claims.document_subject_form` names four:
  the source itself (`source`), an artifact it places (`placed`), a
  batch entity of an artifact kind (`record`) and, under (c4), an
  `unclassified_entity` of the batch (`mention`). Validation
  (`claims.subject_problems`) and the writer (`writer.subject_refusal`)
  take those and no other subject on a document-subject property; a
  `placed` subject in the writer must also have an item. A claim of any
  form but `source` with no excerpt is a reject under
  `excerpt_required` and a `subject_rule` refusal whose message says it
  carries no excerpt (question 8); the writer reads the excerpt as the
  instance keeps text, so one the instance would keep blank is none.
  Validation reads it as Python strips it, so an excerpt only the
  instance keeps blank (U+180E alone) passes validation and is a
  `subject_rule` refusal at write time, not an `excerpt_required`
  reject.
- **An approved equivalence today.** `reconcile.apply_equivalences`
  turns an approved local ref into a QID reference, which no form takes,
  so a document-subject claim on a (c4) mention or a GEDCOM media item
  whose entity a curator approves as an existing item is refused by
  validation and the writer from then on. Whether an approval should be
  read as the link §10.1(c) names is an owner question (#661).
  Settled by #670, 2026-10-24: yes; the confirmed item is a subject of
  its own form, `confirmed` (§10.1(c), "A confirmed mention or record's
  artifact").
- **The interim exception is gone**: `claims.STRUCTURED_ADAPTERS`,
  `GEDCOM_ADAPTER`, `structured_batch` and the `structured` argument;
  `gedcom.ADAPTER_IDENTIFIER` keeps its value, "gedcom-adapter".
  `claims.unplaced_captions` is renamed `unplaced_claims`: it counts
  every caption or document-subject claim about an artifact other than
  its source whose source has no placed media given, and
  `unplaced_hint` says "claim(s) about an artifact other than their
  source".
- **The kind check.** `claims.artifact_kind_problems` names an entity
  record or a local ref of an artifact kind, and a claim of `instance_of`
  or `subclass_of` whose value is one, each problem ending in
  `[artifact_kind]`. `extract.salvage_valid_claims` rejects such a
  claim under that reason, counted in `rejected_by_reason`, and refuses
  an entity manifest that holds such a record, as it refuses one that
  fails validation. `ChunkedClaimExtractor._parse_entity_manifest`
  gives such a record as a problem, as it does an unknown kind, so the
  answer is unusable: each such answer is counted in the coverage's
  `manifest_artifact_kind`, whether a later attempt mends the turn or the
  manifest falls back, and the run report sums it.
  `artifact_fragment` is no artifact kind, and passes. Validation
  itself does not apply the check, so a hand-written batch or a
  structured record keeps an artifact-kind entity.
- **The critic** names the source as "this document" and an image the
  document places by its first place ("page 6, image 1",
  `critique.artifact_places`), as the extraction prompts list it; a
  finding that names a claim so resolves to it at the critique and on
  the review sheet, which reads the transcription view for the places,
  and a place is matched whatever its case.
  A (c4) mention is named by its manifest id, as every entity is, which
  the entities block labels with its type hint.
- **The report** counts the document-subject claims by subject form
  (`container_claims.document_subject_by_form`: `source`, `placed`,
  `record`, `mention`, and `other` for a subject validation refuses;
  `confirmed` too, from #670).
- **The export** (question 9) leaves out whole, at any rank, a
  document-subject statement whose subject carries a `media SHA-1` and
  one of whose references is `stated in` another item, or whose subject
  is `part of`, at any rank, an item one of its references is `stated
  in` (`backup.placed_subject_rule`). It so keys on what form (a) is in
  the graph: an artifact registered by its bytes that another source
  states, whether a record of derived media or a curator's `--placed`
  pair placed it. A container's claim that agrees with the image's own,
  which the writer makes one statement with two references, goes whole,
  the image's own reference with it, failing closed. An un-held media
  item and a (c4) mention item carry no SHA-1, so their statements are
  exported as any of their source's are, the (c4) excerpt included;
  once a curator or artifact matching merges a mention into the held
  scan's item, its statements stand on an item with a SHA-1, cited by
  the letter, and are left out whole, failing closed.
  - *Amended by #669 (owner, 2026-10-24, on #661's question 2):* the
    export removes the container's references, not the statements they
    are on. A reference is the container's when it is on a
    document-subject statement whose subject carries a `media SHA-1` and
    is `stated in` another item, or whose subject is `part of`, at any
    rank, an item it is `stated in`. The export drops that reference's
    link from the statement, and with it the reference and its excerpt,
    unless a statement that stays names the same reference node. A
    statement that keeps no reference `stated in` an item once those go,
    the container's own claim, goes whole as before, its truthy triple
    with it: an extraction run's reference names no source, so it does
    not keep the statement. So the container's agreeing value, which the
    writer folds into the image's own statement, leaves the statement
    with the image's own reference and excerpt (the letter's own *den 28.
    Juli 1939*), and the booklet's reference and translation stay out.
    The run references stay on a statement that stays, the container's
    run's included, as they hold no source text
    (`backup._placed_subject_statements`). A statement with a qualifier
    of text (a string or a monolingual text, such as a `caption` or a
    `transcription_excerpt` qualifier) still goes whole: the writer
    merges only claims whose qualifiers match, so the qualifier is the
    container's as much as the image's, and its text may be the
    container's own words, which the export cannot tell apart (review of
    PR #695). A qualifier of an item, a time or a quantity stays with the
    statement, as its value does.
- **The site.** `named_in_html` names a (c4) mention item, which has no
  artifact page, by its label, unlinked.
- **Chunk ownership.** Under entity assignment a (c4) claim belongs to
  the chunk its mention entity is assigned to, so a mention the manifest
  marks `extract: false` is in no chunk and gets no (c4) claim.
- **What is not built.** The manifest has no `machine_assisted` item yet
  (ADR-0008's follow-up B), so the booklet's translation claims carry
  no such qualifier in the pipeline; the fixture universe gives them
  with it, as planned vocabulary. The fixture corpus holds no booklet,
  and its deck's photograph is a decoration-sized stand-in that the
  deck's transcription does not mark, so no corpus document places an
  image and the corpus gives no claim about one.

  Amended by #665, 2026-10-24: the deck's photograph is now a stand-in
  large enough to be read on its own, with a recorded reading (a
  photograph with no writing, so no excerpt), so the deck's
  transcription marks it and a corpus run gives slide 2's two captions
  and its `shows` → Wastl and `shows` → Hilde, each with its judgement
  note of category `relation` (§10.4). The corpus still holds no
  booklet.

#### 10.7 Amendments to other records (#626, 2026-10-24)

Each gets a dated line; no earlier amendment line is rewritten.

- **ADR-0035**: its status header; §3.4's #456 amendment (the
  document-subject properties take the three subjects of §10.5); the
  #583 line (the general rule replaces the interim exception).
- **ADR-0008**: the status header's #456 line; the #289 bullet's inline
  #456 amendment; question 6's answer; the "Out of scope" bullet's #456
  amendment; the #456 Amendments block's question 6 and deck bullets.
- **ADR-0021**: the OBJE DATE's subject, after the #583 amendment.
- **MODELING.md**: the `document_subject` paragraph; the `issued_by`
  paragraph in "Organizations"; "Compound artifacts", the caption bullet
  and the static-site bullet; "Contradictions", a date of making; "Living
  persons and privacy", the export rule.
- **docs/EXAMPLES.md**: the ADR-0039 row.
- **This ADR, beyond #626's list**: "In brief" bullets 4 and 5, §2.2's
  table, §3's "The caption's people", and the Consequences bullet on
  ADR-0035 §3.4's rule.

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
   Reversed by #626, 2026-10-24: the pipeline gives a caption's fact
   where the caption, read where it stands, conveys it; a curator may
   still write one the extraction missed (§10.3).
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
   Reversed by #626, 2026-10-24: a container also states what its
   passage conveys about an artifact it places, beside the verbatim
   caption and the artifact's own attributes (§10).

**Accepted by the owner, 2026-10-16:**

- the caption never reaches the excerpt's extraction, not even as
  identity hints (§5; ADR-0008 question 2);
- a claim's subject is its own source, or an artifact its source places
  by bytes, never one it only writes about: a narrative recalling a
  letter gives no claim about the held letter, and a curator may link
  them;
  Widened by #626, 2026-10-24: also the artifact a source's own
  structured record makes, though the source holds no bytes of it
  (§10.1(b)); whether an artifact a source only writes about may be a
  subject is an owner question (§10.1(c));
  Settled by #645, 2026-10-24: (c4); an artifact a source only writes
  about is a subject as the source's own mention item of it (§10.1(c));
- combined values only by a curator, with two references and
  `determination_method` → `combined_from_sources` (§4).

The first draft's questions on clustering on a shared artifact role and
on a caption's class for an image no longer arise: a container makes no
structured claim about what it places.

Reversed by #626, 2026-10-24: a container now makes structured claims
about what it places (§10). Clustering over a shared artifact role is
answered in §10.5: the deterministic rules read no such property, and
adjudication's added lines are measured by the scratch diff. A
caption's class for an image still does not arise: `instance_of` is
none of the seven properties, so a container gives no class to what it
places.

**Raised by #626 (2026-10-24)**, each with its lean; §10's text follows
the leans. Each was settled by its lean when the owner answered them,
2026-10-24 ("yes accept all those leans", #645); the questions' text is
kept as it was asked.

5. **How form (b) is keyed and enforced** (§10.1(b)). Lean: an
   extraction may not name an artifact kind, in a manifest record or a
   `LocalRef`'s `instance_of`, checked where model output is first read;
   the subject rule then keys on the subject's kind. Alternative: refuse
   every kind the prompts do not offer. The two differ on
   `artifact_fragment`, which the lean admits and the alternative
   refuses unless a prompt offers it.
   **Settled by its lean** (owner, 2026-10-24): an extraction may not
   name an artifact kind, checked where model output is first read, and
   counted.
6. **An enclosure: (c1), (c3) or (c4)** (§10.1(c)). Lean: (c4), the
   claim on the letter's own mention of the enclosure, if the letter's
   claim should be recorded now (its subject rule accepts any
   `unclassified_entity`, the mention item gets no artifact page, and,
   like (c2), the claim sits on a second item until a link); (c3), no claim, if not. (c1) needs a
   curator's local record, a provenance form for the identification
   (a second reference naming the record, or a curator-cited link the
   claims depend on), a write order and a prompt paragraph of its own,
   and would be a later amendment in an issue of its own.
   **Settled by its lean** (owner, 2026-10-24): (c4), the enclosing
   source's claim on its own mention item of the enclosure; a curator,
   or later artifact matching, links that mention item to the held scan
   (§10.1(c)).
7. **Calibration** (§10.4). Lean: "Wastl to Klara" gives `author` and
   `addressee`, yes; the caption's "July 1939" gives `inception`, no
   (alternative: yes, at month precision); slide 2's title gives `shows`
   → Wastl and Hilde, yes, each with a `relation` judgement note.
   **Settled by its lean** (owner, 2026-10-24).
8. **An excerpt is required** on a document-subject claim whose subject
   is not its own source (§10.4). Lean: yes, a counted reject in
   validation and a `subject_rule` refusal in the writer.
   **Settled by its lean** (owner, 2026-10-24).
9. **The export rule** (§10.5). Lean: fail closed, leaving out whole a
   document-subject statement whose subject is `part of` the item its
   reference's `stated in` names. Alternative: no new rule.
   **Settled by its lean** (owner, 2026-10-24): fail closed.
10. **The form.** AGENT_PROTOCOL §8 says reversing a ruling takes a new
    ADR. The owner chose to amend this ADR in place, each reversed or
    widened passage keeping its text with a dated line. Lean: confirm
    the amendment in place for this case.
    **Settled by its lean** (owner, 2026-10-24): the amendment stays in
    place, an owner-chosen exception to AGENT_PROTOCOL §8.

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
  Reversed by #626, 2026-10-24: a structured value about an artifact
  may come from a source that places it or whose record makes it, cited
  to that source with its passage, beside the artifact's own; each
  reference still cites a source only for what it says, and no
  excerpt's claim depends on a container's text (§10).
- A fact only a caption gives is not queryable until a curator promotes
  it: "letters whose writer, per the compilation, is Sebastian" is a
  text search over captions, not a join.
  Reversed by #626, 2026-10-24: a fact a caption conveys is given as
  a structured statement by the pipeline, so "letters whose writer, per
  the compilation, is Sebastian" is a join; a fact the caption gives
  but does not convey of the artifact (its "July 1939", as owner
  question 7 settled it, §10.4) stays text.
- The manifest gains `caption`, six document-subject properties, a
  qualifier and its value; ADR-0035 §3.4's rule stays "the source
  itself", and one property, `caption`, takes what the source places.
  Widened by #626, 2026-10-24: the seven document-subject properties
  also take an artifact the source places, and the artifact a source's
  own structured record makes (§10.1); `caption` stays the one
  `placed_subject` property.
  Widened by #645, 2026-10-24: they also take, under (c4), the source's
  own mention item of an artifact it only writes about (§10.1(c)).
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
  Adopted in part by #626, 2026-10-24: a container states the seven
  document-subject properties of an artifact it places, where its
  passage conveys them (§10). Each cost is answered. Same standing: the
  container's view stands beside the artifact's own, each labelled by
  its source, values that disagree listed together (§10.2). Which
  properties: the seven, and no other. Clustering over shared roles:
  the deterministic rules read none of them, and adjudication's added
  lines are measured (§10.5). Turning a caption into structure: only
  where the passage, read where it stands, conveys the fact (§10.4),
  with the caption kept verbatim beside it.
- **`main_subject` for whom an image shows.** Contrived for a group
  photograph, whose five named people are not five "main subjects".
- **The excerpt's extraction reads the caption** (ADR-0008 question 2,
  "yes"). It puts the caption's year into claims cited to the excerpt,
  which never states it (CLAUDE.md rules 1 and 5).
- **A local artifact entity in the container's batch**, minted as an
  item and later linked to the image. It makes a second item for an
  artifact that already has one, keyed by its bytes (ADR-0010).
  Still rejected by #626 (2026-10-24): it is route (c2) of §10.1(c),
  which §10.1(b)'s kind check keeps shut.
- **`depicts` from captions or inscriptions.** It would make ADR-0005's
  writer rule tell a person's confirmed depiction from a source's words.
