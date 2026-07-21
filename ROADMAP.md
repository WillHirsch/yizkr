# Roadmap

Tasks are sized for a single focused working session. Definition
of done for every task: runnable, tested where applicable, docs
updated, and no path writes an unreferenced statement.

## Phase W1

1. **Infrastructure** – Docker Compose for Wikibase Suite (MediaWiki +
   Wikibase, WDQS, Elasticsearch) with an `.env.example`, a README
   runbook for home-server deployment, and a smoke-test script that
   verifies item creation and SPARQL round-trip.
   *Done — see `deploy/`.*
2. **Model bootstrap** – `model/manifest.yaml` declaring the core
   properties/classes from docs/MODELING.md, plus an idempotent Python
   script (WikibaseIntegrator) that creates/updates them and records
   the local–Wikidata property mapping on a wiki page.
   *Done — see `model/`.*
3. **Claim schema** – Pydantic models for the pipeline's claim JSON
   (entity refs, property, value, uncertainty fields, fragment
   locator, extraction-run metadata), with validation tests. This is
   the contract between extraction and writing.
   *Done — see `ingest/claims.py`.*
4. **Ingestion skeleton** – `ingest/` package with pluggable steps:
   artifact registrar (media upload + artifact/fragment items),
   transcription (LLM-vision first; HTR platform adapter stub),
   claim extractor (LLM → claim JSON), reviewer export (CSV for
   OpenRefine), and writer (claims → statements per ADR-0003).
   *Done — see `ingest/` (plus a minimal exact-label resolve step).*
5. **Face proposals (W1 minimal)** – detection script emitting
   candidate depicts rows into the same review CSV flow (ADR-0005).
   *Done — see `ingest/faces.py` (detect/enroll/match commands).*
6. **Query library** – SPARQL queries checked in and embedded on wiki
   pages: disagreement report (properties whose values differ across
   sources – the flagship R2 view), per-person timeline, places map,
   unreferenced-statement QC report.
   *Done — see `queries/` (plus an artifact-assertions inventory).*
7. **Backup/export** – scheduled RDF dump + media backup job, with a
   possibly-living-excluded public export variant (empty for now).
   *Done — see `backup.py`; `yizkr backup` (database + media + RDF +
   conservative public export), cron runbook in deploy/README.md.*

### Hardening items from the first trial ingestion

8. **Chunked extraction** – dense documents (a 60-person family tree)
   overflow a single model response; continuation turns are reassembled
   and salvaged today, but per-unit extraction with batch merge is the
   structural fix. Specified in ADR-0006 (semantic segmentation →
   packed chunks → NDJSON emission with sentinel → deterministic merge;
   per-chunk state with resume, subsuming raw-dump re-parse).
   *Done — see `pipeline/chunking.py`; `ingest extract --strategy`.*
9. **Cross-document entity reconciliation** – exact-label resolve
   duplicates people across documents ("Samuel Genserowski" vs "Samuel
   Moses Genserowski"). Specified in ADR-0007: entity-first extraction
   (per-document alias-unified entity manifests, which also make claim
   ownership structural within chunks), then deterministic candidate
   blocking + LLM relational adjudication against a prefetched graph
   roster, emitting equivalence proposals for human review — approved
   matches resolve to existing items before write; a separate
   occasional pass proposes merges/`said to be the same as` for
   duplicates already in the graph.
   *Done — see `pipeline/reconcile.py`; `ingest reconcile`,
   `resolve --equivalences`, `reconcile-graph`.*
10. **Living-person policy at ingestion** – done: the entity manifest's
    `extract: false` flag (ADR-0007) keeps logistics-context people out
    of extraction, and "possibly living" is a **runtime determination**
    from the absence of death evidence — no stored flag, no reviewer
    decision (ADR-0020). Vocabulary session for the observed gaps:
    artifact types (genealogical report, address book), occupation,
    sibling, cause of death (quantity datatype support for ages/counts).
    *Reworked (ADR-0020): the earlier `living` review-CSV column was
    removed and the `warning` column renamed `critique_notes` (its now
    single role). The possibly-living call lives in
    `yizkr.core.privacy`, shared by the public export and the
    static-site badge, keyed on death evidence — a date, place, or cause
    of death, a burial place, or a death event — so a person can be
    marked deceased with no date or place, and privacy needs no
    curation.*
    *Vocabulary session done: `place of burial` (P119), `occupation`
    (P106; item-valued, labels verbatim, occupations reconcile like
    places), `sibling` (P3373; asserted only, never derived from
    parentage), `cause of death` (P509 semantics; verbatim monolingual
    prose), `number of children` (P1971) riding the new quantity
    datatype support (schema, writer, review corrections); artifact
    classes `genealogical report` and `address book`. GEDCOM adapter
    now maps BURI/OCCU/NCHI/DEAT.CAUS, and `.ged` files register as
    genealogical reports. Live-validated end to end, and the one real
    BURI in the Ancestry export — Irma Charlotte Serelman's Golders
    Green burial — is in the graph through the standard review flow.*
11. **Review corrections** – the CSV review flow is approve/reject
    only; ambiguous machine readings (date-format ambiguity) currently
    force rejection. Add an edit/correction column that produces a
    curator-attributed replacement claim.
    *Done (rode along with item 15) — the review CSV's `correction`
    column replaces a misread value on approval: unambiguous ISO dates
    and plain text (item identities stay in the equivalence flow).
    Corrected claims are curator-attributed: artifact reference kept,
    no extraction-run reference (ADR-0003 — process provenance is for
    machine writes); live test asserts the reference shape.*
12. **Travel-notes re-ingestion + duplicate cleanup** – re-extract the
    travel notes entity-first (the trial batch predates the `extract`
    flag and wrote duplicate people), apply the already-reviewed
    equivalences, and write — the first live exercise of the writer's
    alias-append path. Execute the four approved duplicate merges from
    `reconcile-graph` (Samuel, Rosa, Alex, Salomon) and review the
    place candidates.
    *Done — merges executed via `yizkr merge-items`; re-ingestion
    resolved 41 reviewed equivalences and appended document aliases to
    the merged items; graph now has zero duplicate person labels.
    Remaining place/stub candidates (Portland pair, Charlotte stub
    Q183) left for a future curation pass.*
13. **GEDCOM adapter** – structured genealogy exports must never ride
    the generative path (ADR-0006 context): a deterministic
    GEDCOM → ClaimBatch adapter feeding the existing validation/review
    pipeline via `ClaimFileExtractor`. Exact, complete, free,
    reproducible; the Ancestry family tree becomes its first fixture.
    *Done — see `pipeline/gedcom.py`; `yizkr ingest gedcom`. Names,
    aliases, birth/death dates+places, parentage, marriages/divorces;
    GEDCOM uncertainty (ABT/BEF/AFT/BET) maps to structural qualifiers;
    unparseable dates warn, never guess; unsupported tags counted
    honestly — the census names the top-most unmapped tag in every branch
    of every record type, not just direct children of INDI/FAM (issue #8),
    so source citations (SOUR) and multimedia (OBJE) show at true
    prevalence; mapping those into the graph is deferred to ADR-0021.
    Fixture is fictional (the repo carries no personal data) — export
    the real Ancestry GEDCOM and run it through when ready.*
14. **Parallel chunk execution** – chunks are independent by design;
    v1 runs them sequentially. Add a concurrency flag (the 49-chunk
    travel-notes run made the wall-clock cost concrete). Same session:
    batch the reconciliation adjudication call — 369 pairs through one
    response is the same output-bound fragility ADR-0006 removed from
    extraction, one level up.
    *Done — `ingest extract --chunk-concurrency N`; adjudication batched at
    ≤40 pairs per call. Batched adjudication live-tested by the real
    Ancestry GEDCOM ingestion (item 13's first real file): 540 blocked
    pairs split into 15 batches, every per-batch sentinel validated.
    Parallel chunk extraction cannot be exercised by a GEDCOM (the
    deterministic adapter makes no LLM chunk calls); it stays covered
    by unit tests until the next narrative document is ingested.*
15. **Folder/batch orchestration** – `yizkr ingest run <folder>`:
    register → transcribe → extract → reconcile → resolve → pause for
    review → write, with per-document state (the chunk manifest
    already provides the progress unit) and safe re-runs.
    *Done — see `pipeline/orchestrate.py`. Each invocation advances
    every document as far as the two human gates allow (equivalences,
    claims); a gate opens only when the reviewer saves a
    `*.reviewed.csv` beside the exported CSV — presence is consent,
    half-filled files are not. GEDCOMs route through the deterministic
    adapter; per-document failures don't stop the folder; re-running a
    written document is a no-op. Validated live end to end (register →
    extract → review pause → write appended an agreeing-source
    reference; re-run no-op).*
16. **Second trial: the untested half** – a real photograph through
    the faces/depicts flow (never live-run), a handwritten certificate
    (HTR is a stub; triage the gap), and a German-language document.
    Gate further feature work on its findings, as the first trial
    gated this batch.
    *Certificate leg done — two real GRO birth certificates (one typed
    1958 register copy, one fully handwritten 1988 entry) through
    `ingest run` end to end. HTR triage: LLM-vision transcribed the
    cursive register entry essentially perfectly; the one substantive
    error was a surname misread in DEGRADED TYPESCRIPT ("Weisbrock"),
    caught at review and fixed by correcting the transcription and
    re-running — the HTR platform adapter stays an unneeded stub for
    this material class. Findings fixed in the same pass: (a) the
    orchestrator extracted small documents single-shot, which produces
    no entity manifest and therefore silently skipped reconciliation —
    it now always extracts entity-first; (b) upstream redos left stale
    downstream outputs and could mis-open review gates — dependencies
    are now make-style, and a reviewed file older than its CSV no
    longer counts as consent. (That run's first live use of the `living`
    column is now moot: the column was removed and possibly-living made a
    runtime determination — ADR-0020.) Photo and German legs still open:
    the candidate
    photographs and German documents on Drive are unreachable from
    this session (files exceed the connector's transfer cap or sit in
    shared folders it cannot index); the faces extra is verified
    installable, so the flow is runnable as soon as suitable files
    land in the Yizkr Drive folder.*

17. **Compound artifacts: PPTX decks** – slide decks carry embedded
    media (scans of photographs and documents) plus slide-scoped
    narrative. Deterministic unpacking (ADR-0008): the deck registers
    as a `presentation` with one fragment per slide; each embedded
    image becomes its own artifact item (`part_of` the deck, slide
    qualifier, every statement referenced `stated in → the deck`);
    the transcription is built slide-by-slide with `[Slide N]`
    markers so claim locators carry the slide number. Non-image
    media (video/audio) are counted and reported, image-first in W1.
    *Done — see `pipeline/presentation.py`; `.pptx` in
    `yizkr ingest run`. Validated live with a synthetic deck
    (fragments, derived-media provenance, slide-scoped locators,
    idempotent re-run); the real family decks await accessible
    copies in the Yizkr Drive folder, alongside the item-16 photo
    and German-document legs.*

18. **Place hierarchy & coordinates pass** – the inconsistency report
    now suppresses differences of RESOLUTION deterministically when
    the graph records containment (`location`/`part_of` paths: a
    maternity home located in the town another source names is not a
    contradiction). What does not exist yet is the pass that PROPOSES
    that containment: blocking + LLM adjudication over place × place
    (the ADR-0007 reconcile shape), emitting `part_of`/`location`
    proposals for human review — "Steinstrasse 16 is within Berlin",
    "Sheffield, South Yorkshire = Sheffield, Yorkshire". Same session:
    GeoNames/coordinate reconciliation (docs/MODELING.md "Places"),
    which lights up the site's map page. Every approval sharpens the
    inconsistency report and the map at once.
    *Hierarchy half done — `yizkr reconcile-places` (ADR-0009):
    demand-driven worklist (the report's own unresolved place pairs +
    deterministic comma-parent candidates), spine-first ordering,
    five-verdict adjudication, review CSV, cycle-guarded referenced
    `part_of` writes, transitive elimination between rounds. First
    live run: 26 questions round one, 25 edges approved and written,
    ONE question left in round two — the genuine Guildford/Sheffield
    contradiction; the report dropped from 12 rows to 10 with both
    Steinstrasse rows resolving themselves, and the site's places
    page now renders the containment tree. Still open: GeoNames
    reconciliation (IDs, coordinates, imported admin hierarchy) —
    designed in ADR-0009, activates when a `GEONAMES_USERNAME` is
    configured (needs a registered GeoNames account).*

19. **Artifact identity & near-duplicate handling** – ingestion in
    real batches re-encounters the same source: exact copies, renames,
    re-encodes, re-scans, crops. Three problems with different answers
    (ADR-0010): bit-identical (a hash decides), near-identical (a
    curator decides), and confirmed alternate versions (must stop
    counting as independent corroboration).
    *Content-hash key done: a `media SHA-1` property keys artifact
    identity — identical bytes are one item however named (alternate
    filename → alias), a colliding title over different content is
    disambiguated. The orchestrator skips transcription/extraction for
    duplicate files (a `duplicate` document state) both WITHIN a run and
    ACROSS runs: it persists each document's content hash and pre-seeds
    from prior completed docs, so a renamed copy of already-ingested
    content is recognised before any LLM pass; a changed source under an
    old name is detected and re-ingested. Lag-free lookup
    (`WikiClient.string_statement_index`), live-tested end to end. Still
    open (designed, deferred): perceptual-hash near-duplicate detection,
    an artifact-equivalence review queue mirroring ADR-0007, and a
    `version of` relation whose confirmation teaches the writer to treat
    two versions as one source.*

20. **Typed, referenced names** – names lived only as the entity label
    and a flat, untyped, unreferenced alias bag, so the model could not
    say a string was a birth name vs a married name vs a nickname — the
    very typing that resolves alias questions — nor attribute a name to
    a source (in tension with the reference rule).
    *Done (ADR-0011): four monolingual properties — `birth name`
    (P1477), `married name` (P2562), `nickname` (P1449), `official name`
    (P1448) — as a second layer over the alias index (aliases stay the
    match substrate). The GEDCOM adapter emits them deterministically
    from NAME TYPE, the NICK and `_MARNM` tags, and the universal inline
    convention (quoted/parenthetical token = nickname), each referenced
    to its record; a plainly-named person gets no name claim. The
    reconciler consumes typed names as evidence on both sides — batch
    entities via the existing fact rendering, graph items via an
    extended roster query — with prompt guidance that a married/birth
    name correspondence signals identity. Live-validated end to end.
    Deferred: given/family-name components (P735/P734) as name items,
    and promoting the primary untyped name to a referenced statement.*

### Agreed working order

7 (backup) → 12 (re-ingestion + merges) → 13 (GEDCOM) → 14 (parallel
chunks + batched adjudication) → 15 (folder orchestration) → 16
(photo/certificate/German trial). Items 10 and 11 ride along with
whichever of these first touches the review flow. Deferred by explicit
ADR decision, revisit on trigger: MCP/agentic graph access (roster
outgrows a prompt, or interactive curation), slimmed per-chunk prompt
schema, automated `said to be the same as` writing (curator-only by
design), modeling guidance for collective manifest entities.

## Phase W2 (IIIF tier) – later

Cantaloupe + OpenSeadragon/Mirador; W3C annotation store; migrate
fragment locators to canvas+xywh URIs; annotation-driven curation UI.

## Phase W3 (immich tier) – later

immich deployment; person–QID mapping table; sync bot writing depicts
statements; family onboarding notes.

## Model extensions – later

Cross-domain coverage relations on artifacts (docs/MODELING.md,
"Artifact coverage"). First up, **georeferenced maps**: a `map`
artifact subclass plus bounding-box corner properties (NW/SE,
globe-coordinate) so map artifacts carry their geographic extent and
frontends can draw their outlines on interactive maps; the places-map
query grows an artifact-outline variant. Manifest + bootstrap +
query in one session; W2 later upgrades the flat bounding box to
IIIF Georeference (Allmaps-style) annotations. The same pattern —
reuse existing structured properties, reference the artifact itself
or the producing run — stays open for temporal and subject coverage
and other cross-domain relations.

## Frontend ladder – in progress

Rung 1 — *done*: the published query library opens directly into the
query service's visual result views (`#defaultView:Timeline` on the
person timeline — validated rendering coexisting source-labeled dates
side by side; `Map` on the places map, empty until places gain
`coordinate location` statements; the photo-gallery query emits
instance-hosted image URLs, but the stock query GUI's ImageGrid
hardcodes Commons FilePath URLs, so it renders as a link table there
and feeds rung 2's gallery instead).

Rung 2 — *done*: `yizkr publish-site` renders the LIVE SPARQL endpoint
(decided; an internet-facing build must instead come from the
possibly-living-excluded public export — later variant) to a
self-contained static site under var/site: person pages showing every
attributed value side by side with its source label and stated
precision (never padded), runtime possibly-living badges (ADR-0020), the
timeline (page +
TimelineJS-format feed), the photo gallery (ADR-0008 derived media
with provenance), the disagreement report, and a places index that
becomes a map once places carry coordinates (GeoNames pass still
open; map tiles also need a privacy decision — no third-party
requests are made today). No CDN assets; serve on the LAN, e.g.
`python -m http.server -d var/site`. Georeferenced artifact outlines
arrive with the map-artifact model extension.

Rung 3 — later: custom plural-display SPA (timeline rendering
coexisting date claims side by side, labeled by source).
