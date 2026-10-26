# ADR-0002: Attributed evidence and neutral coexistence

Status: accepted. The [current contract](../CONTRACT.md) governs the policy summarized here.

## Decision

Retain what sources assert, with their attribution and expressed uncertainty. Genuine incompatible assertions remain available without requiring a canonical answer. Gustav's birth dates in the fictional register and naturalisation certificate can coexist, each with its source, whether or not a curator prefers either one.

Distinguish genuine conflicting passages, alternative interpretations of one passage, and a corrected extraction mistake. Contradictory passages may occur in one artifact; two model guesses do not become independent witnesses. A compilation and its embedded original can make different assertions, but their known dependence must not masquerade as independent corroboration.

Machine writes use normal rank. Curator preferences are optional: a feature must work without a preferred value. Views must reveal material disagreement and make the attributed evidence accessible; grouping, summaries, pagination and clearly indicated filters are permitted when they do not silently imply consensus.

## Correction and consequential decisions

Do not erase genuine source disagreement to manufacture consensus. Erroneous interpretations, duplicate representations and attribution mistakes may be corrected, superseded or retracted through an auditable process that preserves evidence and intelligible history, subject to separately authorized retention/privacy decisions. The active view need not present every superseded machine mistake as a historical assertion.

Recording an assertion is distinct from accepting it as true, confirming an identity or authorizing disclosure. Those decisions have their own evidence and approval requirements.

## Implementation limits

The current writer adds statements and references and respects curator ranks; it does not provide a general retained-state retraction or recovery workflow. That contract and implementation belong to [#708](https://github.com/WillHirsch/yizkr/issues/708), [#130](https://github.com/WillHirsch/yizkr/issues/130) and [#142](https://github.com/WillHirsch/yizkr/issues/142). This policy is not permission to bypass existing write guards or delete data.

The current disagreement report compares non-deprecated values attributed to different artifacts, counting a fragment as its parent artifact. It does not detect every genuine within-source contradiction. The report's scope is an implementation limit, not the definition of permissible historical evidence. [MODELING.md](../MODELING.md#contradictions) describes its comparison rules.
