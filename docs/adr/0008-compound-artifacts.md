# ADR-0008: Compound artifacts — presentations and derived media

Status: accepted

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
   is image-first;
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
  locators) is ready for scrapbook PDFs and photo albums later;
  IIIF (W2) refines locators without changing the items.
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
(ADR-0010), with its names in the archive, the slides it is on and the
SHA-1 of the deck itself. `derived-media/` beside it keeps the bytes.
Write-time registration (#103) registers and uploads them, by the rules
of the #50 amendment above; until it lands, `ingest run` registers no
derived media.
