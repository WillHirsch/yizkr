# ADR-0021: GEDCOM source citations (SOUR) and multimedia (OBJE)

Status: **proposed** — the honest unsupported-tag census that surfaced the
question is implemented (`pipeline/gedcom.py`, issue #8); how to *map*
SOUR and OBJE into the graph is the open decision this ADR records for
ratification. Nothing here is built yet.

## Context

The GEDCOM adapter (ROADMAP item 13) maps the genealogical content a
family-tree export carries — names, dates, places, parentage, marriages,
occupations, and so on — deterministically into referenced claims. Two
common tag families it does **not** map are `SOUR` (source citations) and
`OBJE` (multimedia objects). Until now they were "counted" only where they
sat one level under an `INDI`/`FAM` record, which under-reported them
badly; the census now names the top-most unmapped tag in every branch, so
their true prevalence is visible. In the real Ancestry export that
prompted this (`Hirsch Family Tree.ged`): **134 `SOUR`** and **8 `OBJE`**,
against 72 individuals.

That prevalence forces a principled question rather than a parser one,
because of what these tags *are*:

- A **`SOUR` citation** is the upstream source the export's own assertion
  rests on — a General Register Office birth index (`Volume 6a, Page 735`),
  a civil registration marriage index, a Find a Grave entry — carried as a
  pointer to a level-0 `SOUR` record plus a `PAGE` locator and an Ancestry
  `_APID`. This archive's founding principle (ADR-0002, CLAUDE.md) is that
  it *records what sources assert*. Ingesting the assertions while dropping
  the citations those assertions themselves cite is a standing tension with
  that principle: we keep "Donald was born Oct 1958" but discard "…per the
  GRO index, Vol 6a p 735, as cited by the tree".
- An **`OBJE` object** is a photograph, scan, or clipping — e.g. an
  *"Obituary notice in Chicago Tribune"*, dated and placed, with a
  `newspapers.com` URL. In an Ancestry export the **bytes are remote**: the
  file references media the archive does not hold. Registering media
  (ADR-0004/0010) keys on the SHA-1 of bytes we *have*, and the privacy
  rule (R7, CLAUDE.md rule 7) forbids fetching from third parties without
  explicit instruction. So `OBJE` cannot ride the normal media path, and
  must never trigger a background fetch of Ancestry/newspapers.com content.

Two guard-rails constrain any answer. First, **no auto-confirmation**
(ADR-0007/0010): the pipeline may not silently mint citation or media
*entities* as fact; identity is a reviewed judgement. Second, **every
statement carries a reference** (ADR-0003) — which the adapter already
satisfies by referencing each claim to the GEDCOM artifact itself with the
verbatim lines as excerpt. So the question is not "do these claims have a
reference" (they do) but "should the export's *own* upstream citation and
media pointers be preserved, and in what shape".

## Decision (proposed)

### 1. `SOUR` → preserved as reference detail, in two graded steps

**Step A (near-term, no new entities).** When a mapped claim's GEDCOM
record carries a `SOUR` citation, fold that citation into the claim's
existing reference: keep the citation's human text (`TITL`, `PAGE`, and the
verbatim `SOUR`/`PAGE` lines) in the transcription excerpt already attached
to the claim, and — where the model has a locator qualifier (ADR-0003) —
record `PAGE` there. This captures *what upstream source the tree cited*
for the reviewer, needs no new items, and cannot auto-confirm anything.
It is the conservative, source-first-faithful first move and is a
contained change to the adapter's `source()` helper.

**Step B (later, its own follow-up).** Promote each level-0 `SOUR` record
to an artifact / artifact-fragment item (a *cited source*, e.g. the GRO
index), referenced `stated in →` from every claim that cites it, and keyed
for de-duplication like places are — the same `_APID`/GRO reference
recurs across many people. This is real modeling work: citation identity,
dedup, and a review step (never auto-merge). It is **not** in scope for
issue #8 and should get its own ADR + review flow when prioritised.

Recommendation: adopt **Step A** now as the resolution of the source-first
tension; schedule **Step B** rather than deferring it indefinitely.

### 2. `OBJE` → an un-fetched media reference, never a fetch

Record that a subject *has* an associated medium as a structured,
referenced statement (or a lightweight media-reference item): the `FILE`
name/URL, `FORM`, `TITL`, and any `DATE`/`PLAC` the record carries — with
**zero bytes held and zero third-party fetch**. Referencing is not
fetching, so this does not touch rule 7; acquiring the bytes stays a
separate, explicitly-instructed step, and only then does the medium join
the normal SHA-1-keyed artifact path (ADR-0010). This preserves the
person↔media linkage the future faces/gallery flows (ADR-0005) will want,
without ever pulling personal media off Ancestry or newspapers.com on the
archive's own initiative. Detail (dedicated `media reference` property vs.
an un-held `photograph` item with a `media file` URL and no `media SHA-1`)
is left to implementation; the ruling is *reference, never fetch*.

### 3. Until ratified/implemented, the census keeps the drop honest

The unsupported-tag census (this change) now reports `SOUR`/`OBJE` at their
true counts across all records and depths, so the gap stays visible in
every ingest run instead of hiding behind a one-level scan.

## Open questions (decision needed)

1. Adopt **Step A** for `SOUR` now, or leave `SOUR` counted-only until
   Step B is scheduled?
2. `OBJE` as a **statement** on the subject vs. a lightweight **un-held
   media item**? (Both honour *reference-never-fetch*.)
3. Confirm the hard line: the pipeline **never** fetches `OBJE` bytes from a
   third party without an explicit, separate instruction.

## Consequences

- Source-first fidelity improves: the upstream citations a tree carries are
  preserved for the reviewer (Step A) and, later, become first-class cited
  sources (Step B).
- Privacy is preserved by construction: `OBJE` is referenced, never
  fetched; no personal media leaves or enters via a background download.
- No auto-confirmation: neither citations nor media become asserted
  *entities* without the ordinary human review (ADR-0007/0010).
- The heavier citation-as-entity modeling (Step B) is named and deferred to
  its own ADR, not smuggled into the adapter.
- Docs move with code: the adapter docstring already points here; if
  Step A / `OBJE` referencing lands, MODELING.md and ROADMAP item 13's
  note update in that same change.
