# ADR-0008: Compound artifacts — presentations and derived media

Status: accepted
Amended by #50 (2026-09-27) and #128 (2026-09-28): see Amendments.
Amended by #289 (2026-09-28): an original excerpt that a compilation
embeds is an artifact of its own, `part of` the compilation, as the owner
decided on #178: see Amendments.
Amended by #103 (2026-09-29): the write registers the deck, its slides
and its derived media, each by its SHA-1: see Amendments.
Amended by #176 (2026-09-29): a PDF is read page by page, and each image
placed within its text-layer pages is read on its own: see Amendments.
Amended by #397 (2026-09-29): each image placed within a PDF's text-layer
pages that has a reading of its own is extracted as a document of its
own, an excerpt, and the write registers the images: see Amendments.
Amended by #389 (2026-09-29): every page of a transcription carries the
transcription record's fields (ADR-0037): a page read by vision takes
them from its reading's record, merged into one page where the image
shows several; a page read from its text layer is `transcribed`, with no
printed page number, languages or notes; a page with nothing to read is
`blank`. A PDF read whole has the pages its record gives, each read by
vision, and its `document_class`; a PDF read page by page gives none,
since no call saw it whole.
Amended by #369 (2026-09-29): labels are compared and set by the one rule
the writer keeps labels by: see Amendments (#103's).
Amended by #403 (2026-09-29): that rule makes U+FFFE and U+FFFF U+FFFD
once it has trimmed a label, as Wikibase does: see Amendments (#103's).
Amended by #373 (2026-10-02): a derived medium's file name keeps its
container's title, in the form MediaWiki takes and cut to fit its limit:
see Amendments.
Amended by #371 (2026-10-02): a derived medium is flagged as an
artifact is, on each registration, made or found, while a file another
item names holds its bytes or its label, before any content tag, is the
label of an item with no hash: see Amendments (#103's).
Amended by #457 (2026-10-05): the anomaly checks' recheck reads the
excerpts' directories too, and checks each excerpt's stored outputs as
a document of its own, of the kind `excerpt` (docs/ANOMALIES.md,
Recheck); #397's "Left for later" no longer lists it.

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
The fictional universe's compilation, *Our Family from Germany*
(docs/EXAMPLES.md), is an example: where the letter excerpt it embeds
gives Rahel's address as "Angerring 11", its machine-assisted translation
reads 71.
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

- **Transcription (#176; done, see the #176 amendment below).** Before
  #176 the vision engine read a PDF whole,
  so an excerpt's text was read into the compilation's transcription and
  every claim read from it cited the PDF: the two witnesses were merged.
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
  and ADR-0010's "Media files", except one: the #50 amendment's adoption
  of an item without a hash by its label no longer applies. An image is
  found by its SHA-1 alone, and a new image whose label another item
  already has takes a content-tagged label. An image is flagged, never
  adopted, as ADR-0010 flags an artifact (amended by #371, 2026-10-02):
  on each registration, made or found, while a file another item names
  holds its bytes, or its first label before the tag, or its own label
  before its tag, is the label of an item with no hash. Its bytes are
  uploaded from the file the record keeps, under the name `"<deck
  title> <name in the archive>"` (in the form MediaWiki takes, as
  amended by #373). Bytes the
  archive holds under several names are one medium: the first name
  labels it (`"<deck title> — <name>"`), and the
  others are its aliases. Labels are compared and set in the form
  Wikibase stores them in, by the rule the writer keeps labels by (each
  run of control characters and line or paragraph separators made one
  space, the separators and the Mongolian vowel separator trimmed at
  either end, then U+FFFE and U+FFFF made U+FFFD, then NFC; ADR-0010,
  #369, #403). A deck title that would push its images' or its slides'
  labels, or its slides' descriptions, past the 250 characters Wikibase
  takes is cut there, ending in "…", since those only display it; so is
  the list of slides in an image's description;
- an image the never-ingest list names, by any of its names or by its
  SHA-1, is left out: neither registered nor uploaded. The deck's own
  file is uploaded as it is, with whatever it embeds (the owner's
  decision, 2026-09-29: refusing the deck would add complexity for a case
  with no meaningful reason to arise).

A compilation's excerpts are registered the same way once follow-up A
records them.

Amended by #176 (2026-09-29): **a PDF is read page by page, and each
image placed within its text-layer pages is read on its own.** This does
the #289 amendment's transcription item, and the part of its follow-up A
that comes before the write. Where it says the owner decided, the owner
did so on 2026-09-29, in the review of its PR.

- **Page by page.** The vision route reads a PDF page by page when any
  of its pages has a usable text layer: such a page from its text layer,
  deterministically and without a model call; a page with nothing to read
  by nothing; and any other page by a vision call of its own. The PDF's
  transcription holds its own text only, page by page behind `[Page N]`
  markers, and marks where each image sits by the image's SHA-1, as the
  #289 amendment allows; its `pages` hold each page's method apart from
  its content. A PDF none of whose pages has a usable text layer is read
  whole, by one vision call that gives the engine the PDF itself, so a
  scanned compilation stays as that amendment's third question leaves it.
- **The definitions** the #289 amendment's seventh question asks for, as
  the transcription uses them (`yizkr.pipeline.textlayer`):
  - a page is a scan when images together cover at least 60% of it, as
    the strips or tiles of one scan do, and the layers of a mixed raster
    content page, and more of the letters and digits it draws visibly lie
    over the images than beside them, as the owner decided. Text beside a
    large image is a
    typed page, whose image is a witness of its own; text over one, a
    stamp or a header over a scan, or text laid over a picture that fills
    the page, is a scan, read by vision. An OCR layer is drawn invisibly,
    and is never the text layer;
  - a page's one image is the page itself, given to the engine as held,
    when it alone covers at least 90% of the page and the page draws
    nothing else;
  - an image under 24 pixels on a side, or under 10,000 pixels in all, is
    decoration, and is not read.

  Registration can take them as they stand; a change to them is a change
  to the transcription stage, checked by a scratch diff (ADR-0025 §4).
- **What else sends a page to vision.** A text layer whose replacement
  character stands within a word is garbled, and its page is read by
  vision. So is a page that shows text in an annotation, a free-text one
  or a form field holding text; a text-layer page's
  other annotations are left unread, and the run report counts the pages
  that show them. Text a page hides, under an image, a filled path or an
  annotation painted after it (a box or ink a viewer draws over a line,
  a stamp, a redaction marked and not yet applied, but not a highlight,
  an underline, a squiggle or a strike-out, which leave the text in
  view), is never read from the text layer: its page is read by vision,
  which reads what the page shows. Were a text layer ever given without
  such text, `[redacted]` would stand where it lies, never nothing, as
  the owner decided; #389's transcription record carries the mark
  through what the vision engine writes too. Each of these is the
  owner's decision.
- **Known limits of hidden text** (from the review of #176): text hidden
  in ways the page does not show as covering it is read from the text
  layer as though it showed. That is text under a thick stroked line (a
  marker drawn over it and flattened into the page), text a clip path
  cuts out of view, text drawn in the colour behind it (white on a white
  page), and text drawn at a fill opacity of 0. None is handled yet.
- **Each image placed within a text-layer page** is recorded before the
  write as a deck's derived media are: in the PDF's `derived-media.json`,
  keyed by the SHA-1 of its bytes, named for its places (`page 6, image
  1`), with the pages it is on. Its bytes are the JPEG file the PDF holds,
  or else a PNG of its pixels, written by a deterministic encoder that a
  test holds byte for byte. The never-ingest list is asked of its SHA-1
  before it is read. It is read by a vision call of its own, once however
  many times the PDF places it, and its transcription, which names it by
  its SHA-1, is kept beside its bytes. An image on a page read by vision
  is read with that page, as the compilation's page. An image the
  never-ingest list names on such a page, or in a PDF read whole, reaches
  the engine with the page: the owner decided that the case will never
  arise and is not worth handling.
- **Determinism.** PDFium is started afresh for each PDF and draws a font
  the PDF does not embed with a built-in one, never the host's, so
  a page's text layer, its rendering and an image's bytes, and so the keys
  of the calls that read them, are the same on every host and in every
  run; pyproject.toml pins the PDFium build, since another could decode or
  render to other bytes. The owner accepts what reading each image on its
  own costs in calls.
- **Still to do** (follow-up A; done by #397, see its amendment below):
  extracting each image's transcription into a batch of its own;
  registering the images at write time; and content dedup across
  documents, since another document or excerpt that brings the same
  bytes keeps a view of its own, though the LLM cache answers its call.
  Until then no claim is read from an image, and every claim read from a
  PDF cites the PDF and comes from its own text.

Amended by #397 (2026-09-29): **each image a PDF places within a
text-layer page is extracted as a document of its own, an excerpt, and
the write registers it, `part of` the PDF.** This does the rest of
follow-up A, and settles the first half of the #289 amendment's seventh
question: where an excerpt's views live, and how its status relates to
the compilation's.

- **An excerpt is a document.** Each image that a PDF's transcription
  reads on its own and keeps a reading of, which passes the transcription
  check, is a document of the kind `excerpt` (`ExcerptRun` in
  `yizkr.pipeline.orchestrate`), found from what the PDF's directory
  keeps: the record of its images (`derived-media.json`) and each image's
  reading beside its bytes. Its identity is the SHA-1 of its bytes, and
  its directory is `excerpts/<sha1>/` in the state directory, apart from
  the documents' directories, which no document's can take. It takes the
  stages any extracted document takes, transcription, extraction and the
  critique, each with its own view and its own status, and in `ingest
  run` its own gates. Its transcription is the reading the PDF's
  transcription made of it, taken without a model call, so the image is
  read once. Its extraction cites the image (`cite_artifact`), and reads
  the image's text alone: nothing the compilation says reaches it, as the
  #289 amendment's second question leaves it.
- **Its status is its own.** An excerpt joins the run once its PDF's
  transcription has read it, and goes on whatever becomes of the PDF's
  extraction, critique or review; the PDF goes on whatever becomes of
  its excerpts. An image whose reading failed is no excerpt until a later
  run reads it (below).
- **One witness per SHA-1.** The same image in two PDFs is one excerpt,
  with both among the PDFs that place it, and one extraction. An image
  whose bytes a document of the corpus has is that document, and no
  excerpt: its bytes are not extracted again (ADR-0010). A written
  excerpt seeds content dedup across runs as a written document does, so
  a file added later with its bytes is its duplicate. An image the
  never-ingest list names is asked about again as excerpts are found, and
  is never one.
- **The scratch driver** takes an excerpt's stages as the PDF's
  transcription ends: the first excerpt of an invocation is the
  excerpts' canary, which goes through its stages next and joins the
  run's `canaries`; the others take their stages rank by rank with the
  rest of the documents. Each is added to the run plan (`documents`, with
  the kind `excerpt`) as it is found, and a later invocation plans those
  its directories hold from the start. The barrier takes the excerpts
  whose stages all completed to the corpus stages with the documents.
  `ingest run` takes an excerpt after the folder's documents, and in
  serial mode (ADR-0019) extracts the excerpts before it orders them.
- **Registration at write time.** The write of a PDF registers the images
  its record names, after the PDF, as a deck's derived media are
  registered (the #50 and #103 amendments): an item per image, found or
  made by its SHA-1, labelled `"<title> — page <n>, image <k>"` for its
  first place, `part of` the PDF once per page it is on with the page's
  number, a `photograph` (the Decision's class for a raster image, which
  the #289 amendment's fourth question leaves open), its description
  naming the pages. Its file is uploaded under `"<title> page <n>, image
  <k>.<suffix>"` (in the form MediaWiki takes, as amended by #373). The
  write of an excerpt registers each PDF that places it, with its
  images, and cites the image's item. The public export
  needs no change: an image states its kind, stated in the PDF, as any
  artifact does.
- **Reuse.** A PDF's transcription that passes the check is still set
  aside and made again when what it marks is not kept whole beside it: an
  image the never-ingest list names now; a record that is missing,
  damaged, made for other bytes, or names other images, or an image's
  bytes that are missing or changed; or an image with no reading of its
  own, whose reading failed or was lost, which is so read again. Its
  pages and images are read again through the LLM cache, which answers
  the calls it answered before. The record then names none of the images
  the list names, so nothing registers or extracts what an earlier
  reading kept of them.
- **The run report** counts, as aggregates, the images the PDFs read page
  by page place: the PDFs that place any, the images placed (each once
  per PDF), the distinct images, how many PDFs place how many, and how
  many images are documents of their own.
- A curator's correction of a PDF's transcription
  (`transcription.corrected.txt`) replaces the PDF's own text only: its
  excerpts come from the record its reading kept.
- **The transcription record (#389, ADR-0037).** An excerpt's
  transcription is the kept reading, so its class and its pages are the
  reading's record's, never its kind's; its language is the one that
  holds most of the record's text (resolution `record`), and py3langid
  guesses it only for a reading with no record; and its flag is the one
  the check made now raises on the reading, not the one stored with it.
  The kept reading is judged by the record check, as any vision reading
  is.
- **An image that shows no page of a document is no excerpt.** An image
  whose kept reading's record calls every page `blank` (a photograph with
  no writing) or `not-a-document-page` (a scanner's colour chart) passes
  the check and is kept, but holds nothing to extract, so it is no
  excerpt; the write still registers it as the PDF's image. An image the
  record calls `illegible` is an excerpt, so that a reader sees it was
  there, and so is one with a transcribed page beside a blank one (an
  open book's written page and its blank verso).
- **Decided by default** (the owner may override): an excerpt's status
  is independent of its PDF's; an excerpt's write may register its PDF's
  item (its identity, no claims) before the PDF's own review is
  accepted; an image whose reading always fails is read again on each
  run, through the LLM cache, with no cap; and what earlier runs kept of
  an image the never-ingest list names since stays on disk, as an
  excluded document's directory does, runtime state that is never
  extracted or written again.
- **Left for later:** the call estimate (`yizkr scratch estimate`) does
  not yet count an excerpt's extraction and critique; an excerpt's
  transcription view does not name the PDF's kept reading among its
  inputs, so a reading made again with other text that still passes the
  check leaves the excerpt's view as it was; an excerpt's write fails
  when one of the PDFs that place it no longer has the bytes it was read
  with, even when another places the image.

Amended by #373 (2026-10-02): **a derived medium's file name keeps its
container's title.** The names of the #103 amendment, `"<deck title>
<name in the archive>"`, and of the #397 amendment, `"<title> page <n>,
image <k>.<suffix>"`, kept only the title's ASCII letters, digits and
` _.-`, and nothing bounded their length: a deck titled "Familie Löbig"
uploaded `Familie Lbig image1.png`, a title in Russian left only
`presentation` or `pdf`, and a long title passed MediaWiki's limit, so
the upload was refused once the container's item was made. Each part of
the name is now in the form MediaWiki takes as it is (ADR-0010, "File
names MediaWiki takes"): every letter kept, `-` for each character or
sequence it refuses, and the whole within 231 bytes, the title cut first,
ending in "…", or left out, so the medium's own name stays whole
whenever it fits on its own; a cut never falls between a letter and its
combining marks. The container's kind stands for a title of which that
form leaves nothing. A medium is labelled by the first of its names that
shows something once stored; a name that shows nothing (control
characters, a zero-width space or bidirectional marks alone) is no
alias. A medium with no such name is labelled
and uploaded as `image N`, N its place among the record's media in the
order of their SHA-1s, excluded media counted, so its label ends in a
name the instance keeps as sent, and a re-run sends no edit.
