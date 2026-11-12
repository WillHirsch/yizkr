# Places, organizations and source-stated containment

A place identity, a source's assertion about its surroundings and an organization's identity are different things. This guide describes their current representation and the post-write containment workflow. [Mention records](MENTIONS_AND_MATCHING.md#structured-mention-records-adr-0035) own source-name parsing; [identity review](IDENTITY_REVIEW.md) owns durable assignments and approval; [life-event dates](LIFE_EVENTS.md#dates-and-bounds-adr-0034) define temporal qualifiers.

## Place or acting organization (ADR-0012, ADR-0031)

The relation being asserted determines the needed entity. Born, buried, resident, located or detained at a named institution requires a place. Issuing a certificate, carrying out an act, employing or educating requires an organization. Membership can identify an organization or a named family. A camp, school, prison or hospital is not automatically an organization merely because it can act in another context.

If the source uses an institution both as a site and as a body, represent the two entities separately and link the organization to its source-stated `location`. Preserve the source's names; do not invent a site/body pair, a location or a normalized institutional name without support. A human officeholder remains human; a generic reference such as “the company” does not become an organization alias. Historical places that no longer exist remain ordinary places, with any bounds the source gives.

| Relation | Current meaning |
| --- | --- |
| `location` | An organization's or event's source-stated place; place-to-place location also participates in containment readers. |
| `issued_by` | A permitted document/artifact subject's issuing organization, with the support required by [artifact-subject rules](ARTIFACTS.md#claims-about-artifacts-adr-0039). |
| `carried_out_by` | An event's acting organization. |
| `employer` | The person's employer organization, independent of their occupation; source-stated employment dates can qualify the assertion. |
| `educated_at` | Source-stated education at an organization. |
| `member_of` | Source-stated membership in an organization or family. |

### What kind validation establishes

Manifest `accepts` declarations specify exact permitted value kinds, without automatic subclass closure. They can also use `any`, `subject` (the subject's kind) or `class` for the applicable properties. A vocabulary class reference is different from an instance of that class. Unknown QID kinds cannot receive checks requiring a known kind. Unclassified entities are accepted where the declaration explicitly includes them.

This is a structural check, not proof of historical meaning. For example, `country_of_citizenship` accepts a place, but validation does not distinguish a country from a town. `part_of` accepts a flat set of place/artifact values rather than enforcing every subject-dependent combination. Claim validation rejects self-references, and the writer checks them again after identity resolution, when previously distinct mentions may resolve to one item. The writer repeats selected subject, vital, retired/reference-property and self-reference guards; it does not independently repeat every general value-kind check.

## Containment as an assertion (ADR-0038)

A `part_of` or `location` assertion needs the source that conveys it and the available locator, not merely a place-name match, punctuation or a model's geographic knowledge. Normal-rank assertions from different sources can coexist. Process provenance identifies a machine interpretation but cannot replace historical support. A container identity decision answers which place the source names; it is not a new source for geography.

For example, the fictional marriage certificate puts Fläckfeld in Kreis Mockburg while the family tree puts it in Kreis Peiltal. Both claims can remain with their respective sources. A place need not be split merely because sources name different administrative containers, and one container is not automatically the preferred answer.

Containment carries only the time its source states for that relation. Use start/end qualifiers for a dated period and `point_in_time` for a dated observation, with their respective [bounds](LIFE_EVENTS.md#dates-and-bounds-adr-0034). A document date or the date of a birth at that place does not automatically date the containment. A record-only `within` chain has no temporal fields, so lifting it produces undated links. Source attribution remains available even when the relation has no date.

Source names and evidence can describe a nearest-first chain: place → district → region. Each adjacent relation needs support. Extraction validates container entries individually and the critic checks whether their cited passage actually states containment. A familiar geographic name or an occurrence elsewhere in the transcription is insufficient. [GEDCOM PLAC adaptation](GEDCOM.md#export-ownership-and-citations) uses its explicit component chain as evidence, with its own placeholder and missing-component rules.

## Resolving source-stated containers (ADR-0009)

The place pass operates after the relevant entities and sources exist. It receives written batches, local-mention-to-QID mappings, transcriptions, registered source artifacts and an existing place roster. It creates containment assertions between existing items; it does not mint missing containers, split comma-delimited labels or ask a model to invent a hierarchy.

| Stage / process | Inputs → intermediate product |
| --- | --- |
| Read context / instance I/O | Existing places, labels/aliases and nondeprecated containment → place roster and cycle-check graph. |
| Lift source links / deterministic | Mention records, supporting claims, written item mappings and transcription locations → adjacent source-attributed links with evidence and any claim qualifiers. |
| Build worklist / deterministic | Source links and existing namesakes → numbered container-identity questions, already settled links and unresolved counts. |
| Adjudicate / model + validation | Named chains and numbered candidate summaries → candidate selections or uncertainty, with rejected answers counted. |
| Resolve and plan / deterministic | Answers, predecessor resolutions and existing/planned containment → writeable links, unresolved links and cycle refusals. |
| Apply / reviewed command or disposable-run I/O | Authorized selections and source/run provenance → new statements, added references or already-present outcomes. |

Candidate matching uses whole labels/aliases with the pass's case/accent folding and umlaut transliteration. Matching existing namesakes other than the starting place itself are candidates. The prompt identifies candidates and questions by number rather than asking the model for QIDs. This is a different operation from deciding whether two place items should merge.

A container with several candidates needs adjudication. So does a link present only in a mention record, even with one candidate. A single-candidate question is settled without a call only when every grouped link already has a corresponding batch claim, supplied by the ordinary write. Missing candidates or a starting item absent from the place roster are counted and left unresolved. A missing or uncertain middle container does not authorize bridging across it: later links need the preceding container's resolved identity.

The model sees named chains and candidate context, not the raw source quotation. Its confidence is descriptive; there is no write-confidence threshold. An uncertain, missing or invalid answer writes nothing for that choice. Conflicting answers are refused. Selected candidates must belong to the actual question, and resolving a later chain link requires its predecessor. The cycle guard rejects self-containment or a path back to the child through existing containment and links accepted earlier in the plan. It is conservative across time: different historical periods do not permit a cycle.

The cycle graph includes nondeprecated `part_of`/`location` edges whether or not they have source references. This is intentionally a different read scope from disagreement and identity-containment comparison, which require sourced edges. A mistaken or unsourced graph edge can therefore block a proposed link; the guard is not a proof that the recorded geography is true.

### Writing and repeating a link

A new accepted link becomes a normal-rank `part_of` statement with a historical source reference and a separate process-run reference. A supporting claim supplies its qualifiers. A record-only link supplies no time and uses the container evidence as excerpt, with a page/printed-page locator from the transcription's first matching occurrence of the evidence. That location is an approximation, not a uniquely bound occurrence. The write path retains compatibility with links lacking evidence text by citing the artifact alone; this does not prove the missing support or waive extraction's evidence checks.

The statement match includes value and qualifiers. An already represented assertion is not rewritten, and a reused statement keeps its rank. For a link supplied by a claim, an equal statement already citing the artifact is sufficient. For a record-only link with evidence, the complete source-reference block must already be present; otherwise the statement gains the new block alongside its existing references. An equal claim from another source can likewise gain references. The pass does not delete a direct assertion just because a transitive route exists, remove contrary statements or change their rank. Repeated passes can ask the same identity question again: repeatable writes do not imply fixed-round convergence.

The standalone `yizkr reconcile-places` command exports a CSV with an empty `decision` column. Its `--apply` path accepts `approve` rows with verdict `same` only when they still match a current question and candidate. This is bounded link review, not durable person/place identity consent or a complete retained-ingestion workflow. Apply refetches the worklist/containment, checks source registration and producer metadata, and uses revision-guarded item edits, but does not acquire the scratch lifecycle lock or its reset/write-state guard. [The ingestion contract](INGESTION_CONTRACT.md) still governs retained operation.

The disposable corpus run applies validated selections in its `places` stage after a completed write and WDQS synchronization. It records planned links, answers, refusals and per-link outcomes in `corpus/places/record.json`, and model answers in `responses.json`. Completed link writes can resume from the current stage/reset records; interrupted model calls can reuse compatible cache responses. The live-stack lock, expected run and write-phase watermarks guard that route. See the [places workflow diagram and product contract](SCRATCH_WORKFLOW.md#places-resolve-the-containers-that-sources-name) for orchestration and exact paths.

## Consumers and current limits

- The disagreement report treats connected places as differences of resolution by traversing sourced, nondeprecated `part_of`/`location` paths. It uses a path at any time; it does not align each edge's temporal scope with the compared claim. Identity-containment filtering similarly needs actual QIDs, not equal labels.
- The static site's place tree reads truthy containment. It is not a complete plural, source-attributed, time-scoped container view; separate assertions remain in the graph without every distinction appearing in that tree.
- GeoNames, World Historical Gazetteer identifiers and coordinates are vocabulary/reader capabilities, not an implemented gazetteer importer or automatic place resolver. Importing outside geography would need an explicit source and its actual temporal scope; this pass supplies neither.
- Different source container claims are evidence for review, not automatic place identity decisions. Matching, historical acceptance and disclosure remain separate authorities.

Implementation: [place worklist, model answers and writes](../src/yizkr/pipeline/places.py), [corpus write/places orchestration](../src/yizkr/pipeline/scratchwrite.py), [claim validation](../src/yizkr/claims.py), [manifest acceptance](../src/yizkr/model/schema.py), [disagreement comparison](../src/yizkr/disagreement.py) and [site rendering](../src/yizkr/staticsite.py).
