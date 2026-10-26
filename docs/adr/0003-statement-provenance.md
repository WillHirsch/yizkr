# ADR-0003: Evidence and appropriate provenance

Status: accepted. The [current contract](../CONTRACT.md) defines the provenance policy; the encodings below describe the current implementation.

## Policy

Historical assertions identify their supporting sources and the most specific reliable locations available. Machine interpretations also identify their producing process. Derived results identify their inputs and method; curator attestations identify the person making them and their context. Schema and operational records need accountable origin and versioning, not necessarily a source-artifact citation.

Source uncertainty and reading uncertainty remain distinct. Missing or uncertain locations stay explicit. A reference must not invent a passage or claim more precision than its evidence supports. Reading route does not determine which witness owns an assertion, and known dependencies between sources must not be presented as independent corroboration.

## Supporting passages

A citation is a located passage, read in context. Its words and position must convey the recorded fact and any time it carries. A place name in the district field of a form can support containment; the same name in a list of towns does not. An issuing authority's heading does not by itself locate every place named in the document. A transcription excerpt absent from the cited transcription supports nothing.

The current extraction and critique paths use `core/verbatim.py` to locate excerpts by lines and, where supplied, page. A physical page number and a printed page number have different meanings: retain both when known and do not guess either. Image regions use percentage bounding boxes. IIIF canvas/region URIs remain an optional future representation, not a required citation tier.

## Current encoding and checks

- `ClaimBatch` requires a source artifact identified by the SHA-1 of its media. The writer resolves that hash to an artifact item before writing; this path does not yet accept every provenance category the policy allows.
- Historical claims use a `stated in` reference to the artifact or fragment, with page/region locators and a supporting excerpt where available. Some claim forms require an excerpt; [MODELING.md](../MODELING.md#reference-pattern-see-adr-0003) documents the details.
- Machine claims also use an `inferred from extraction run` reference. Its run item identifies the producing process; optional judgement notes belong in that process block, not in the source's words.
- Review corrections marked `curated` keep their source reference and use the reviewed artifact and MediaWiki edit history for the human action; the writer omits the machine run block on those claims.
- The registrar and writer currently reference their structural statements too: schema statements cite the model manifest item, and run items cite themselves for their type. These are current encodings, not a requirement to treat bookkeeping as historical testimony.
- The unreferenced-statements report checks for a missing reference block, not whether a reference adequately supports a claim. Presence and evidentiary adequacy are different checks.

The reference requirements in current schemas and writers remain enforced until supported replacements are implemented and verified. [#708](https://github.com/WillHirsch/yizkr/issues/708) defines the evidence/history contract; retained identity and curation work under [#130](https://github.com/WillHirsch/yizkr/issues/130) and [#142](https://github.com/WillHirsch/yizkr/issues/142) must preserve provenance through change. This ADR does not implement a new persistence format or authorize unreferenced historical claims.
