# Wikibase modeling conventions

This guide is the entry point for the implemented graph model. The [current contract](CONTRACT.md) governs policy; [requirements](REQUIREMENTS.md) describe intended outcomes, including capabilities that are not yet implemented. Detailed domain rules live in the guides linked below. A valid claim, successful write or displayed assertion establishes neither historical truth nor permission to disclose it.

## Model and identity

People, places, artifacts, relationships, events and named periods can have independent identity where useful. Dates and uncertain intervals normally use values and qualifiers; not every fact needs another item. Historical assertions, machine interpretations, identity decisions and operational metadata have different origins and authority.

The [model manifest](../src/yizkr/model/manifest.yaml) declares vocabulary keys, descriptions, datatypes and acceptance rules. Bootstrap creates properties and vocabulary items in the instance and maintains `Project:Model mapping`. Local P/Q identifiers vary by instance; repository vocabulary uses manifest keys and mirrors Wikidata semantics where appropriate. Ordinary vocabulary additions can preserve evidence and identity, while new semantics may need code or migration.

Labels, descriptions and aliases are display terms, not evidence or identity. Historical entities can share a name; vocabulary labels must resolve unambiguously for bootstrap. Creation bindings and accepted assignments identify persistent entities even after display edits. A matching label or description does not authorize adopting an existing item. [Names and mention records](MENTIONS_AND_MATCHING.md) distinguishes display terms from source-attributed names, structured interpretations and matching keys.

## Entities, statements and vocabulary

| Model element | Meaning and current boundary |
| --- | --- |
| Historical entity | A person, place, organization, family, vessel, occupation or other supported kind. The relation and source context determine the kind; matching text alone does not. |
| Relationship or event | A partnership/marriage is an entity, with participants and optional boundary events. Extracted birth/death facts are statements on the person; curator-made vital events remain readable separately. |
| Source artifact | A held source identified by its bytes, or a source-mentioned unheld artifact. Fragments locate parts of an artifact; generated reading representations do not become additional witnesses. |
| Class or controlled value | A kind such as `human` differs from a value such as `circa` or a participant role. Both are items, but their permitted uses differ. |
| Statement | A subject/property/value assertion, qualified where necessary by time, role, parentage or other context, with references identifying support and process. Distinct qualifier sets can represent distinct assertions of the same value. |
| Operational record | A run, identity origin or application receipt accounts for processing or placement. It is not another historical witness or implicit human approval. |

Manifest descriptions are also model inputs: changing them can change extraction and cache dependencies. `entity_kind` marks document-entity kinds, while `entity_class` identifies other class keys. Extraction availability also respects `extracted` and `offered` controls; a declared kind is not necessarily offered to a model. For example, birth/death kinds exist for curator events but are excluded from automated extraction. The [extraction guide](EXTRACTION.md) owns entity preparation, schema-gap handling and prompt boundaries.

Item-valued properties declare `accepts`: explicit kinds, `any`, the subject's kind, or a class. Lists are exact, not inherited through arbitrary subclass chains. Validation checks known batch kinds; an existing item referenced only by QID supplies no batch kind to check. Broad acceptance does not prove historical meaning: citizenship accepts a place, but that alone does not distinguish a country from a town. [Kind validation](PLACES.md#what-kind-validation-establishes) describes these limits. Named-period vocabulary likewise does not imply automated period extraction: the current extractor cannot supply the required `time_period_class` entity kind.

Reference-only and retired properties are refused as claim or qualifier properties. `offered: false` alone instead controls prompt availability; operational properties can remain usable by code. Existing spouse statements remain readable, but new `spouse` claims are refused in favor of [unions](LIFE_EVENTS.md#adapter-and-reader-compatibility). A claim or item-valued qualifier cannot name its own subject; the writer rechecks after identity resolution, because distinct mentions can resolve to one item. A failing qualifier refuses the whole claim.

The [claim schema](../src/yizkr/claims.py) uses manifest keys, local entity references, supported artifact references and explicit existing-item references. Ranks are not represented in `ClaimBatch`; machine additions use normal rank and preserve existing curator ranks. Schema and writer checks enforce supported shapes, not the truth of an interpretation.

## Reference pattern (see ADR-0003)

The [provenance policy](adr/0003-statement-provenance.md) requires origin appropriate to each kind of information. The current historical-claim path has a narrower encoding: each claim requires an `ArtifactRef` identifying source media by SHA-1, resolved to a registered artifact before writing. A source QID is not a substitute in this field. Unheld media mentioned by GEDCOM cannot supply that source; the held GEDCOM export supplies its assertions. Broader policy does not make every provenance category representable by this schema.

The [provenance diagram](ARCHITECTURE.md#source-and-process-provenance) separates historical and process references:

| Reference block | Contents and authority |
| --- | --- |
| Historical source | `stated_in` the artifact, with reliable physical/printed page, image region, source-part locators and a language-tagged supporting excerpt where available. It locates the assertion, not the model that read it. |
| Machine process | `inferred_from_extraction_run` the bound process item recording producing stages and application context. Optional `judgement` category and `judgement_note` text belong in this block, never in the source's words or a claim qualifier. |
| Source's preparation statement | Where the source explicitly states machine-assisted preparation, separate located evidence supports that sourcing-circumstance marker. It does not replace the ordinary supporting passage or identify the current extraction process. |
| Identity origin/confirmation | Separate operational references explain entity placement and accepted decisions. They do not turn a kind assertion into identity consent or provide another historical source. |

### Source locations and supporting text

Physical page numbers are one-based positions in the media. Printed page numbers reproduce the page's own numbering and require a physical page locator. Extraction copies printed numbering from a structured reading for a single physical page; it does not ask the claim model to invent it. An excerpt image uses its own reading, not the printed number of a page that places it. [Source reading](SOURCE_READING.md#structured-vision-record-adr-0037) defines the available records and language selection.

Image regions use percentage boxes (`x%,y%,w%,h%`). Container source-part locators encode the container digest, original part and placement identity, kind, location and available reading binding. They come from verified reader/inventory context. Repeated placements locate one source, not additional witnesses; unplaced resources gain no invented page. Page fragments are cited on the claim path through their owning artifact and locators. [Container sources](ARTIFACTS.md#container-sources-adr-0008) owns these bindings.

A supporting excerpt preserves the source's wording and language within storage limits. Some forms require one, including the non-source subjects of [document-property claims](ARTIFACTS.md#claims-about-artifacts-adr-0039) and applicable [unknown death/burial assertions](LIFE_EVENTS.md#unknown-values-missing-evidence-and-burial); a container caption is itself the verbatim passage. Locating text, validating its shape or attaching a reference does not prove that it supports the asserted meaning. IIIF region identifiers are an optional capability, not a required tier of the current claim schema.

### Process notes, repeated evidence and curation

A judgement note records a machine interpretation or ambiguity. An entity's note accompanies its sourced kind statement. Notes do not determine rank, confidence, truth or consent. Note text is normalized and shortened for storage; an unusable note or missing note vocabulary can be omitted with a warning while the claim remains writable. [Extraction and critique](EXTRACTION.md#machine-uncertainty-and-judgment-adr-0023) distinguishes these notes from separate advisory critic findings.

The claims writer can combine equal property/value/qualifier assertions on one statement while retaining distinct source/location reference blocks. Repeating a write does not manufacture another witness. Different notes from one run can produce multiple process blocks, so readers count run items rather than blocks. Curator ranks and protected references remain authoritative; nuanced reference-replay and kind/type-hint behavior is specified in the [writer](../src/yizkr/pipeline/writer.py), not by an assumption that every repeated claim adds a new run reference.

A CSV value correction marked `curated` keeps its historical source reference but omits the machine run block. The reviewed batch and edit history record that action; extraction strips model-supplied curation/approval flags. This is distinct from [durable claim review](IDENTITY_REVIEW.md#apply-reviewed-historical-claims), which binds approval to located content, assignments and supporting material and records actual graph support. Neither source/run references nor a CSV flag alone supply the full durable approval contract.

Schema statements currently cite the model-manifest item, and run items cite themselves for their type. Creation can therefore precede completion of self-references; verified creation bindings support recovery on the same item. [Run provenance](RUN_PROVENANCE.md) owns producer facts and process records, and [creation recovery](ARCHITECTURE.md#item-creation-and-interrupted-write-recovery) owns interrupted operations. These encodings do not require inventing historical artifact citations for all bookkeeping.

### Text storage and refusal boundaries

The writer uses NFC and the instance's storage constraints. Display terms have a 250-character limit; normalized or shortened labels/aliases remain display, never identity. Ordinary string, external-ID and monolingual claim values and qualifiers have a 400-character limit and must survive storage unchanged. Unsupported whitespace, control characters or Unicode can refuse a claim rather than silently changing what it says.

Ordinary page/region locators and excerpts can instead be normalized to one line and shortened with a digest suffix, preserving distinction between different original texts. Unsupported Unicode in those fields can still refuse the claim. Exact structural source-part records are never truncated: an unstoreable record refuses its claim. Judgement notes have their own omission behavior described above. [Writer storage rules](../src/yizkr/pipeline/writer.py) and [artifact registration](ARTIFACTS.md#registration-and-held-media-adr-0010) define the detailed term and filename handling.

Some refusals are per claim; others, including missing source bindings, identity admission, unsupported dates or missing face approval, can stop a batch before mutation. Inspect the returned refusals and recovery state rather than treating a completed invocation as proof that every proposed claim was written. No multi-item batch is atomic.

## Sources and registration

Held media use a wiki file plus an artifact item carrying `media_file` and `media_sha1`. Registration verifies bytes, exclusions and unique content/creation bindings, preserves curator work and never overwrites a file with changed bytes. Source registration approves no historical assertion; the retained text workflow requires its own explicit registration confirmation.

PDFs and PPTX presentations share the [container-source model](ARTIFACTS.md): native text, shapes and notes belong to the container, while original embedded images are independent source owners, including backgrounds and unplaced resources. Generated page/slide renders are representations. Captions and facts supplied by a container cite the container; independently read image assertions cite the image. Reading size and layout do not redefine ownership.

Verified owner/part bindings can survive re-encoding of a reading representation. Similar titles, prepared pixels or newly supplied lookalike files do not establish historical equivalence. The artifact guide owns composition, subject forms, exclusions and current reading limits. General near-duplicate/version review remains [#162](https://github.com/WillHirsch/yizkr/issues/162); filtering an embedded source does not redact an otherwise eligible container upload.

## Faces and depiction (see ADR-0005)

`depicts` associates a photograph region with a person and requires affirmative human confirmation of that association. Detection creates regions and matching proposes identities; enrolling a gallery example does not approve later matches. General document/run acceptance cannot confirm faces, and scratch rebuilds omit `depicts`.

The [face workflow](adr/0005-faces-strategy.md) describes detection, enrollment, matching and CSV review. Its per-row `row_approval` digest binds the claim and involved manifest records; it is batch review state, not an authenticated signature or durable graph-revision binding. The separate durable claim API requires explicit `depicts_confirmed` for any supported `depicts` decision and rechecks it during application. This does not make face-detector batches automatically admissible through the retained text workflow; source preparation and identity boundaries still apply.

A source's `shows` assertion, a container's verbatim `caption` and an associated-media `image` link do not assert confirmed face identity. [Artifact claims](ARTIFACTS.md#claims-about-artifacts-adr-0039) and [GEDCOM media](GEDCOM.md#unheld-media) define their attribution. Using those words to suggest a face match still requires confirmation.

## Domain guides

| Subject | Current guide and principal distinction |
| --- | --- |
| Names and mentions | [Names, mention records and matching](MENTIONS_AND_MATCHING.md): source-attributed names versus display terms and structured matching inputs. |
| Life facts, relationships and dates | [Life events](LIFE_EVENTS.md): birth/death and burial assertions, unions, kinship, roles, uncertain dates and unknown values. Source uncertainty differs from machine doubt; missing data does not itself assert an event. |
| Places and organizations | [Places](PLACES.md): sites versus acting bodies, source-stated containment, identity review and reader limits. Geographic knowledge or an identity verdict does not supply a new historical assertion. |
| Reading and extraction | [Source reading](SOURCE_READING.md) and [extraction](EXTRACTION.md): source-grounded readings, claims, schema gaps and advisory critique. |
| Deterministic adaptation | [GEDCOM](GEDCOM.md): tag-specific conversion, attribution and unsupported material; determinism does not establish truth or consent. |

## Identity, review and supported operation

[Durable identity review](IDENTITY_REVIEW.md) separates source occurrences, interpretations, local mentions, assignments and persistent entities. Recorded graph context supports later-cohort matching; equal names or machine clustering are proposals, not accepted identity. Existing-target assignments and reviewed new-entity creation preserve their original authority, with identity origins separate from confirmation and from historical evidence. A sourced `said_to_be_the_same_as` assertion records evidence of equivalence but does not execute a merge.

The [one-source text workflow](TEXT_INGESTION.md) implements durable preparation, explicit source registration, identity review and separately approved historical additions under server protection. Creation initially establishes reviewed display terms; historical kinds and other assertions require their own approval. Support receipts bind applied claims to statement/reference support and assignment revisions, including already-present support. Unchanged approvals can survive compatible correspondence and unrelated additions; material changes need scoped review.

Legacy map writers retain their one-map-per-deployment-generation guard. The scratch workflow can rebuild an explicitly disposable instance and write unratified proposals without human approval, but cannot reset a retained instance, write face associations or authorize retention. Its outputs and QIDs are not durable acceptance merely because a run completes. [Workflow diagrams](ARCHITECTURE.md) and [stage products](SCRATCH_WORKFLOW.md) describe execution and recovery.

Reviewed additions do not supply general retained correction, retraction, reassignment, merge or split; [#142](https://github.com/WillHirsch/yizkr/issues/142) tracks correction/split work. Multi-format retained orchestration and an accessible general curation interface remain outside the one-text-source API. Current preservation guards stay in force, and the owner authorizes the actual keeper transition under [#159](https://github.com/WillHirsch/yizkr/issues/159).

## Contradictions

Genuine source disagreement can remain without a canonical answer, including within one source. Preferred rank does not make normal-rank evidence cease to exist. Reviewed machine alternatives and corrections are not independent historical witnesses; the [correction contract](CONTRACT.md#evidence-interpretations-and-correction) governs their treatment.

The [disagreement report](../src/yizkr/queries/README.md#disagreement-comparison-adr-0033) compares selected attributed readings across artifacts. It is a selective view, not a complete consistency check: dates, parent sets, containment and source grouping affect its results. Derived death bounds and union endings are labeled view results, not additional stored assertions or independent witnesses. The domain/query guides describe the different scopes of timelines, kinship and other consumers.

## Living persons and privacy (see ADR-0020)

Archive data and exploration are private by default. The non-privileged export is a filtered view, not a public-safe archive; retained text can name living people. Passing a runtime check grants no publication permission.

[Runtime privacy](PRIVACY.md) owns person selection, the 123-year default cutoff, birth/death checks, badges and additional export exclusions. There is no stored living flag or universal human-confirmation gate. Private-site badges share the person-evidence predicate but do not apply the complete export filter. Final privacy/cutoff decisions remain under [#70](https://github.com/WillHirsch/yizkr/issues/70) and [#72](https://github.com/WillHirsch/yizkr/issues/72), with public-site authorization separate under [#163](https://github.com/WillHirsch/yizkr/issues/163).
