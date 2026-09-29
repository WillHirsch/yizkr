# ADR-0008: Compound artifacts — presentations and derived media

Status: accepted
Amended by #50 (2026-09-27) and #128 (2026-09-28): see Amendments.
Amended by #289 (2026-09-28): an original excerpt that a compilation
embeds is an artifact of its own, `part of` the compilation, as the owner
decided on #178: see Amendments.
Amended by #103 (2026-09-29): the write registers the deck, its slides
and its derived media, each by its SHA-1: see Amendments.

## Context

Family knowledge increasingly arrives in *compound* containers:
slide decks assembled for family gatherings, and eventually scrapbook
PDFs or photo albums. A deck is one file, but it carries many pieces
of embedded media (scans of photographs, documents, maps) plus
per-slide narrative text. Treating the container as an opaque single
artifact loses exactly what makes it valuable:

- each embedded photograph should be an artifact in its own right —
  viewable in a gallery, usable by the faces flow (ADR-0005),
  linkable by `depicts` — while its provenance (which deck, which
  slide) stays traceable;
- the narrative text is slide-scoped, and claims extracted from it
  should carry slide-level fragment locators (R2: traceable to the
  *specific part* of the artifact that asserts it).

PPTX is the first concrete case. It is a documented ZIP+XML format,
so unpacking is fully deterministic — like GEDCOM (ADR-0006 context),
structure never rides the generative path. Only the *interpretation*
of slide text (claim extraction) uses an LLM.

## Decision

### Modeling (extends docs/MODELING.md conventions)

- `presentation` becomes an artifact subclass in the manifest.
- Slides ARE page fragments: the registrar's existing fragment
  machinery mints one `artifact fragment` per slide, `part_of` the
  deck with a `page number` qualifier holding the slide number.
  Claim locators reuse `page_number` — no new property.
- Each embedded medium becomes its OWN artifact item: media uploaded
  to the wiki under a deck-prefixed name, item labeled
  "<deck title> — <media name>", `instance of` `photograph` for
  raster images (curators refine misclassified diagrams later; the
  faces flow needs photographs to be first-class), `artifact`
  otherwise. One `part_of` statement per slide the medium appears
  on, with the slide-number qualifier.
- Every statement on a derived-media item is referenced
  `stated in → the presentation` — the deck asserts its own
  composition, exactly the registrar convention for fragments
  (MODELING.md "Artifacts and registration"). Nothing is
  unreferenced (ADR-0003).

### Pipeline

A deterministic unpacker (`pipeline/presentation.py`) reads the ZIP
directly (slide order from `presentation.xml`, text runs and speaker
notes in document order, slide→media from the relationship parts).
In `yizkr ingest run`:

1. register the deck (`presentation`, one fragment per slide);
2. register derived media (idempotent and keyed on content, see the
   amendment below; re-runs reuse the items). Non-image media (video,
   audio) are counted and reported, not uploaded — W1 media handling
   is image-first (#171);
3. the transcription is built deterministically, one `[Slide N]`
   block per slide (speaker notes as `[Slide N notes]`) — no
   LLM-vision call; extraction is instructed by the markers to carry
   the slide number as the claim's `page_number` locator;
4. everything downstream (entity-first extraction, reconciliation,
   review, write) is the ordinary shared path.

### Testing

Offline: a synthetic fixture deck is assembled with `zipfile` in the
tests (the repo carries no personal data); unpacking, slide order,
notes, media→slide mapping, transcript markers, and the orchestrator
branch are unit-tested. Live: a synthetic deck through the running
stack validates upload config and provenance shape.

## Consequences

- Embedded photographs become first-class gallery/faces material with
  slide-accurate provenance, without the curator manually splitting
  decks.
- The same shape (container artifact + derived artifacts + fragment
  locators) is ready for scrapbook PDFs and photo albums later (the
  deferred-on-trigger register, #171); IIIF (W2, #166) refines locators
  without changing the items.
- One more deterministic adapter to maintain; PPTX only in v1 (PPT
  binary format and Google Slides exports arrive as PPTX in
  practice).
- Derived items multiply the item count per deck; labels are
  deck-prefixed so idempotency and disambiguation hold.

## Amendments

Amended by #50 (2026-09-27): **one image shared by several decks is one
derived item, `part of` each deck.** Derived media had been keyed only by
their deck-prefixed label, so an image that a second deck (or a
regenerated copy of the same deck) also embeds kept only the first
deck's `part of`. Derived items now follow artifact identity (ADR-0010):

- each carries `media SHA-1` and is matched by it first, so the same
  bytes are one item whichever deck or standalone file brings them. A
  pre-hash item is matched by its label and adopted (hash backfilled)
  only when its wiki file holds the same bytes (or it has no file yet).
  A same-label item with different bytes stays a separate artifact, and
  the new one's label and file name carry a short content tag;
- every deck that embeds the image contributes its own assertions,
  each referenced `stated in → that deck`: one `part of → the deck` per
  slide (slide-number qualified); an `instance of`, `media file` or
  `media SHA-1` statement that another deck (or the artifact itself)
  already made gains that deck's reference rather than a duplicate
  statement (the writer's agreeing-source rule). Nothing is overwritten
  or removed (CLAUDE.md rule 3), and every deck's assertions about the
  item are each referenced `stated in` that deck;
- a reused image is not uploaded again when one of the item's files
  already holds its bytes; a later deck's label for it becomes an alias
  unless that label is another item's (the case that content-tagged
  the item), since that alias would cross-link two artifacts.

Amended by #128 (2026-09-28): **derived media are recorded before the
write and registered at write time.** Step 2 of `ingest run` above now
records the deck's images instead of registering them, since a stage
before the write reads nothing from the wiki and writes nothing to it
(ADR-0025 §2). The deck's state directory holds the record,
`derived-media.json`: each distinct image keyed by the SHA-1 of its bytes
(ADR-0010), with its names in the archive and the slides it is on, and
the SHA-1 and title of the deck itself, which the item's label begins
with. `derived-media/` beside it keeps the bytes, each image in a file
named for its SHA-1. The record stands while it is there whole, made for
the deck's bytes as they are now, and each image it names has its file:
a regular file, not a symlink, named for the image's SHA-1 and holding
the bytes that SHA-1 names. Otherwise it is made again, and only a file
that is not what it should be is written. Write-time registration (#103)
registers and uploads them, by the rules of the #50 amendment above;
until it lands, `ingest run` registers no derived media.

Amended by #289 (2026-09-28): **an original excerpt that a compilation
embeds is an artifact of its own, `part of` the compilation.** This
records the owner's decision on #178, quoted: "My decision for #178 is
option 2".

A compilation sets text of its own, such as a typed translation, a
retyped transcription or commentary, beside images of excerpts from the
original documents it draws on. It therefore holds two witnesses of one
text: the original, whose writer asserts it, and the compilation, whose
compiler translated or retyped it, sometimes with machine assistance. The
two differ in authorship, date and authority, and they can disagree.
Under ADR-0002 both readings are kept, each attributed to the witness
that makes it. Were the excerpts fragments of the compilation (#178's
option 1), the two witnesses would be one source, and the disagreement
report, which pairs only values from different artifacts (ADR-0023 §4),
could never list a translation that contradicts its original. The
compound-artifact pattern keeps them apart.

**The excerpt is a derived artifact.**

- An image that a compilation places within one of its pages is a
  derived artifact, as an image a deck embeds is (the Decision above,
  and the #50 and #128 amendments). Its identity is the SHA-1 of its
  bytes (ADR-0010). It carries `media SHA-1` and `media file`, and it is
  `part of` the compilation once per page it is on, with that page's
  number as the `page number` qualifier. Every statement the
  compilation contributes to it is referenced `stated in → the
  compilation`. One image that two compilations embed, or a compilation
  and a deck, or that is also ingested on its own, is one item, `part
  of` each container.
- Its bytes are the image as the PDF holds it, where that is a whole
  file (a JPEG stream); any other image is written out losslessly by a
  deterministic encoder, and its SHA-1 names those bytes, so every run
  finds the same item. A change to how images are written out changes
  their SHA-1s, and with them the identity of every excerpt: it is a
  stage change, checked by a scratch diff (ADR-0025 §4). A library
  upgrade that changes the encoder's output re-keys the excerpts just
  the same, so before the keeper run (#159) the encoding is frozen, or a
  test holds its output byte for byte; otherwise an upgrade would mint a
  second item for every excerpt already kept.
- An image that is a whole page, as each page of a scanned PDF is (with
  or without a text layer over it), is that page of the compilation, not
  an embedded medium, and nothing is derived from it. How to tell such
  an image deterministically is left to follow-up A (question 7 below).
- Its label is `"<compilation title> — page <n>, image <k>"`: the first
  page it is on, and its place among that page's images, since a PDF's
  own names for its images mean nothing to a reader. The label is the
  source label that frontends show beside its values (CLAUDE.md rule 6).
  Its class follows the Decision: `photograph` for a raster image, which
  curators refine.
- Nothing here depends on a class for the compilation. The pipeline goes
  by what the file holds, and cannot tell a compilation from any other
  PDF that places images in its pages (a report with a photograph in
  it), so every such PDF takes this shape. That generalisation is this
  amendment's inference, not #178's ruling, which is about compilations.
  It is the scrapbook-PDF case that the Consequences above foresaw, so it
  fires the "More compound artifact types" entry of the
  deferred-on-trigger register (#171); follow-up A takes it up.

**How claims cite each witness.** A claim cites the witness that
asserts it (CLAUDE.md rule 1, ADR-0003), by the SHA-1 of that witness's
media (MODELING.md, "Reference pattern"):

- a claim read from the compilation's own text cites the compilation,
  with the `page number` of the page that says it and a transcription
  excerpt in the language it is written in: a retyped transcription is
  in the original's language, though it is the compilation's text;
- a claim read from an excerpt cites the excerpt, with no page number,
  since it is one image, optionally a `region` within it, and a
  transcription excerpt in the original's language;
- what the compilation says about an excerpt, such as a caption naming
  who wrote it, to whom and when, is the compilation's assertion, and a
  claim it gives cites the compilation. A claim whose subject is the
  excerpt itself cannot be expressed yet (question 6 below).

Each witness has its own transcription and its own batch: the
compilation's transcription holds its own text only, and an excerpt's
transcription the excerpt's only. An extractor sets every claim's source
to the key of the document it read (`cite_artifact`), so nothing read
from one witness can cite the other.

**Machine-assisted preparation is a sourcing circumstance.**

- When a compilation states that its text was prepared with machine
  assistance (that its translations are machine translations, say),
  each claim read from that text carries the `sourcing circumstances`
  qualifier with the value `machine-assisted`: the claims read from the
  passages the statement names, or from all of the compilation's own
  text when it names no part. The qualifier records what the compilation
  says about its own making. It is never inferred from how a text reads
  (CLAUDE.md rule 5), and never set on an excerpt's claims, which are
  another witness's.
- It is the source's circumstance, not the pipeline's. The pipeline's
  own machine work is process provenance, the `inferred from extraction
  run` reference (ADR-0003), and a machine's doubt stays out of the
  graph (ADR-0023).
- It informs the reader and is never a rank: no frontend prefers an
  excerpt's value because the compilation's is machine-assisted
  (CLAUDE.md rules 4 and 6). The static site's person page shows it among
  the statement's qualifiers.
- Like circa, it makes the compilation's claim a statement of its own
  beside the same value from a source without it, since the writer
  matches a statement by its value and its qualifiers.
- `circa`, the one sourcing-circumstances value in the manifest, says
  that a value is approximate, which does not fit. So the manifest gains
  `machine_assisted` (label "machine-assisted"). The extraction prompts
  list every vocabulary item, so adding it changes the extraction stage,
  and it lands with follow-up B below.

**What each stage does.** In place already:

- claims name their artifact by the SHA-1 of its media, and so do
  transcriptions and prompts (#102, #127), so an excerpt can be cited
  before anything is registered;
- an extractor cites the document it read (`cite_artifact`), so one
  transcription per witness gives each witness's claims their own
  source;
- a deck's derived media are recorded before the write, each by its
  SHA-1, with the deck's SHA-1 and title and the slides it is on (the
  #128 amendment);
- the disagreement report counts a page fragment as the artifact it is
  part of, and every other source as itself (ADR-0033), so a derived
  artifact is a source of its own there;
- the static site's person page lists every qualifier of a statement
  beside its value and its sources.

Still to do, each keeping the two witnesses separable:

- **Transcription (#176).** Today the vision engine reads a PDF whole,
  so an excerpt's text is read into the compilation's transcription and
  every claim read from it cites the PDF: the two witnesses are merged.
  #176 routes a PDF by page. The compilation's transcription holds the
  text of its own pages only, from their text layer or, for a scanned
  page, by vision, with page markers so that each claim carries its page.
  It may mark where an excerpt sits, by the excerpt's SHA-1, but never
  holds what is read from the excerpt's image; a retyped transcription in
  the compilation's own text is the compilation's. Each image placed
  within a page gets a vision transcription of its own: a view made from
  the image's bytes, which names the excerpt by its SHA-1. An image
  without text gives no claims.
- **The transcription seed (#144).** An archived transcription read its
  PDF whole, excerpts included, into one text. #144 already leaves out
  the two that the spot-check flagged (#92); one of them waits on #176
  and this ruling to be transcribed afresh. A scratch run after #176
  starts from fresh state directories (ADR-0032 §1), so a PDF whose pages
  are read from their text layer has its views made afresh and uses no
  seed entry, and no archived reading stands as a born-digital
  compilation's view; a scanned PDF, read by vision, keeps its entry (a
  scanned compilation is left open below). Outside a scratch run, a view
  made before #176 stands until `--refresh transcribe` makes it again,
  since a change of code does not by itself make a view stale (ADR-0032
  §1).
- **Excerpts as documents (follow-up A, or part of #176).** Each image
  that a PDF places within a page is recorded before the write as a
  deck's derived media are, the record generalised to any container.
  Before any step reads it, the never-ingest list is checked against its
  SHA-1, and content dedup applies to it: bytes that another document or
  excerpt already brought are not transcribed or extracted again
  (ADR-0010). Question 7 below lists what else it must settle.
- **Extraction.** Each witness's transcription is extracted into a batch
  of its own, as any document's is: the prompts name the witness by its
  SHA-1 (#127), and `cite_artifact` sets every claim's source to it. The
  marker is follow-up B: the manifest gains `machine_assisted`, and the
  extraction sets it on the compilation's claims as above, once the
  first question left open below is settled.
- **Registration (#103).** Write-time registration registers and uploads
  a compilation's excerpts from their record, as it does a deck's derived
  media, by the rules of the #50 amendment and of ADR-0010's "Media
  files". It does so before any batch that cites them is written, since
  the writer refuses a batch that cites media no item carries. Where it
  looks a page fragment up by its `part of` and page, it also matches
  `instance of artifact fragment`: an excerpt, like a deck's derived
  media, is `part of` its container with a page qualifier too, and must
  never be taken for that page's fragment.
- **Writing.** The writer needs no change. It resolves each claim's
  SHA-1 to the compilation's item or to the excerpt's, and writes the
  marker as it writes circa.

**Coexistence and the disagreement report.**

- A compilation and an excerpt it embeds are two artifacts, so the
  report pairs the values they give as it pairs any two sources' values
  (`from_different_artifacts`, ADR-0033). The report needs no change.
- For example, were Gustav's birth register extract (docs/EXAMPLES.md)
  reproduced in a compilation beside a machine-assisted translation
  that reads its "am siebenten Januar" as the seventeenth, the report
  would list his date of birth as 7 January 1919 per the birth register
  extract and per the excerpt of it that the compilation embeds, 17
  January 1919 per the compilation, and 1 July 1919 per the
  naturalisation certificate, and the person page would show the
  compilation's value with its marker. No value is preferred, and no
  witness's reading replaces another's (CLAUDE.md rules 3 and 4).
- Agreement is not independence. Where a translation agrees with its
  excerpt, the value has two sources, though one reading derives from
  the other. With the marker they are two statements; without it, the
  compilation's reference joins the excerpt's statement, and the
  under-witnessed queue (ADR-0023 §5) counts two witnesses. The `part
  of` link lets a query tell them apart; whether the queue should is
  left open below.
- An excerpt of a document that the archive also holds on its own, such
  as the birth register extract itself, is a crop of it: different
  bytes, so a distinct artifact and a second source until the version
  relation of ADR-0010 §2 and 3 (#162) links them, as ADR-0010 says of
  any near-duplicate.

**Left open for the owner** (raised with #289):

1. Who sets the marker: the extractor, claim by claim, from the
   compilation's statement about its preparation; or one step per
   document, whose marker the pipeline puts on every claim read from the
   compilation's own text, with that statement read once or pointed to
   by a curator. A curator's part is only to point to where the
   compilation says so: a marker set on a curator's own knowledge would
   be the curator's assertion, cited to the compilation, which never
   made it (CLAUDE.md rule 1).
2. Context across witnesses. As this amendment has it, an excerpt's
   extraction reads the excerpt only, so what only the compilation says
   (a caption's writer or year) stays the compilation's claim, and a date
   in the excerpt that gives no year gives no claim. Should the
   excerpt's extraction also read the compilation's caption, to resolve
   who and when, its claims still citing the excerpt? A "yes" would put
   the caption's year into a claim cited to the excerpt, which never
   states it (CLAUDE.md rules 1 and 5). Only who a person is could come
   from the caption safely, since that is identity, which reconciliation
   decides (ADR-0007), not a value the excerpt is cited for.
3. An original that a compilation reproduces only as a whole page, or as
   part of a scanned page. An image that fills a page has bytes of its
   own, but so does each page of a scanned PDF, so telling an original
   reproduced whole from the compilation's own page needs a rule; a
   region of a scanned page has no bytes of its own, and would have to
   be cropped deterministically. Until this is settled, such a page is
   read as the compilation's page: the claims read from it cite the
   compilation and carry the marker as its other claims do, whenever the
   compilation's statement covers that page. So a claim that comes from
   an original's text, whether the original is reproduced as a whole
   page or read with its translation in a scanned compilation's one
   transcription, carries a marker that the original's text never
   earned.
4. A compilation's images: whether they take the Decision's `photograph`
   for a raster image, which puts them in the photo gallery and within
   reach of the faces flow, or another class; and whether an image that
   is the compiler's own, such as a chart or a picture of its own text,
   can be marked so that its claims cite the compilation rather than the
   image.
5. Whether the under-witnessed queue counts a compilation and an excerpt
   it embeds as one witness where they agree.
6. Claims about an excerpt itself. A caption's writer, recipient and
   date are statements whose subject is the excerpt, an artifact named
   by its SHA-1, but a claim's subject (`EntityRef` in
   `src/yizkr/claims.py`) is a QID, a vocabulary item or a batch-local
   entity only. Should the claim schema gain a subject that names an
   artifact by its SHA-1, resolved at write time as a source is, or do
   such statements wait? The people and places a caption names are
   claims about them, and can be made meanwhile.
7. For follow-up A to settle: where an excerpt's views live, and how its
   status relates to the compilation's, given that content dedup seeds
   only from `written` documents (ADR-0010), that each document passes
   the per-document review gates, and that the scratch driver slices
   documents by kind and sends the first of each kind through first as
   its canary; and a deterministic definition of an image that is a
   whole page, which covers a page stored as strips or tiles, the layers
   of a mixed raster content (MRC) page, and tiny decorative images.

Amended by #103 (2026-09-29): **the write registers the deck, its
slides and its derived media.** Steps 1 and 2 of `ingest run` above
move to the write, which registers a deck just before it writes the
claims that cite it (ADR-0025 §2; ADR-0010, "Registration at write
time"):

- the deck, found or made by the SHA-1 of its bytes, with one page
  fragment per slide. A fragment is the item that is an `instance of
  artifact fragment` and `part of` the deck with the slide's `page
  number`, so a derived medium, `part of` the deck with a slide
  qualifier too, is never taken for one: the lookup the #289
  amendment's registration item asks for;
- then each image of the record kept in the deck's state directory,
  found or made by its own SHA-1 under the rules of the #50 amendment
  and ADR-0010's "Media files". Its bytes are uploaded from the file the
  record keeps, under the name `"<deck title> <name in the archive>"`.
  Bytes the archive holds under several names are one medium: the first
  name labels it (`"<deck title> — <name>"`), and the others are its
  aliases;
- an image the never-ingest list names, by a name or by its SHA-1, is
  left out: neither registered nor uploaded.

A compilation's excerpts are registered the same way once follow-up A
records them.
