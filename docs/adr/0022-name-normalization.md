# ADR-0022: Name normalization — LLM-primary, no new property, composition without kind

Status: **accepted** — design settled through three adversarial design passes;
implemented: the extraction-prompt standards (chunking.py's entity-manifest and
chunk prompts) and the thin deterministic `pipeline/names.py` validator, wired
into both the LLM manifest path and the GEDCOM adapter. Still pending: the
**frontend factoring** layer (§5 — composing display forms and deriving
short-forms from the atoms; #140), and the §6 confidence surfacing, which was
generalised and implemented by ADR-0023. **Amends ADR-0011** (re-affirms and
re-grounds the given/family component deferral) and applies ADR-0018's
derive-don't-store discipline to name surface forms. Validation is the live
review flow, not a synthetic eval (§Decision 7), **extended by ADR-0025** to
whole-corpus scratch runs in the development stage. Renumbered from local
ADR-0021 when reconciling with origin (#18); the GEDCOM ADR kept 0021.
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): the entity manifest may say which part of a name is
the given name and which the surname, read from the document's own
conventions, and never writes that reading; the graph still has no
name-part property (Rulings 1 and 2, §3). §4's alias cleanup works on
those parts rather than on token shapes (ADR-0035 §2.6, §8).

## Context

Names live in two layers (ADR-0011): (1) the **alias bag** — the entity's
MediaWiki label + altLabels: flat, untyped, append-only, **unreferenced**; it is
the reconciliation blocking substrate (token overlap via `_name_tokens`,
`block_candidates`) and the label-lookup key; and (2) **typed name statements** —
`birth_name`, `married_name`, `nickname`, `official_name`: ordinary referenced
monolingual-text claims, used only where a source marks a name's *type*. ADR-0011
deliberately **deferred** structured given/family components (P734/P735 name
items) and, separately, chose not to promote an ordinary principal name to a
referenced statement, "to avoid doubling every person's claim count for no new
information."

The **GEDCOM adapter** already normalizes names deterministically (NAME TYPE /
NICK / `_MARNM` / inline parentheticals → typed names + a clean label). The **LLM
entity-manifest path has no equivalent**, so it emits three pathologies, all seen
in real extracted data:

1. **Composed permutations** — "Hildegard Laudler/Löbig" with aliases
   `Hildegard Laudler | Hildegard Löbig`: one given name and two alternative
   surnames held non-concurrently, flattened into opaque full-name alias
   strings.
2. **Derivable short-forms** — `Gustav | Gustl | G. Löbig`: a bare given
   name and an initialism stored as if they were distinct names.
3. **Label-embedded metadata** — `Gustav Löbig (later Gustave Lobig)`, `Agnes
   Löbig (geb. Nertler)`: typed-name assertions left inside the label
   parenthetical instead of parsed out to a clean label + typed statements.

Three adversarial design passes (recorded in the run's workflow transcripts)
established the shape of the fix and, crucially, separated concerns the first
analyses had conflated:

- **A new given/family (name-part) property is the wrong answer.** Two
  independent rulers with opposite priors (YAGNI-minimalist and structure-honest)
  both ruled against it, at high confidence. The given/family *kind* is
  undecidable without name-order convention (a source-first-forbidden guess that
  inverts on surname-first cultures — "Sörne/Löbig Ida" — and shatters
  particle surnames — "van der Berg"); where it *is* cleanly decidable (GEDCOM
  `SURN`/`GIVN`) it is the component ADR-0011 already deferred and yields a
  type-unknown surname the adjudication prompt itself classes as *weak* evidence;
  it adds zero blocking recall (blocking already tokenises); the "all the
  Löbigs" query needs either the fuzzy matching the alias bag already
  supports or a shared item node that would create an ADR-0020 re-identification
  surface; and ADR-0009's world-knowledge-via-run-provenance precedent does not
  transfer, because place containment is checkable against external geography
  whereas a given/family parse has no external ground truth — run-referencing it
  would launder a guess rather than record verifiable world-knowledge.
- **But composition ≠ kind.** There are *three* axes, not two, and they differ in
  decidability: **composition role** (which token is the fixed part vs. the
  alternating part) is prescribed by the notation — the `/` fixes "Hildegard"
  and alternates the surname, so `Hildegard Löbig` is a *licensed* composition,
  not a fabrication; **kind** (given vs. family) needs name-order convention and is
  never asserted; **type** (birth vs. married vs. official) needs a marker and is
  recorded only where marked. The earlier "never compose permutations" ban folded
  composition into kind and over-reached.
- **The LLM is the right primary normalizer.** The semantic work — stripping
  "born / née / geb. / z domu / urodzona / …" across languages and scripts,
  reading licensed compositions, inferring `Gustl`~`Gustav` from context, and
  emitting a typed name *with a located source excerpt* (which a label-parser
  cannot produce) — is the LLM's comparative advantage; it already reads the whole
  document. A hand-coded multilingual marker lexicon in Python is exactly the
  brittle parsing to avoid. A deterministic layer is still wanted, but as a thin
  validator, not the semantic engine.

Binding principles: source-first (ADR-0002/0003 — never assert structure a source
did not, never conflate source and process provenance), ADR-0011's anti-doubling
ruling, ADR-0020 privacy, and ADR-0019's file-based, reconstructable,
**auditable** pipeline.

## Decision

### 1. No new schema

Re-affirm ADR-0011: **no** `family_name` / `given_name` / name-part property, no
name items, no `claims.py` change. The four typed-name properties are reused
verbatim. Names stay two-layer. This is not merely neutral — it is what makes
leaning on the LLM *safe*: the one undecidable axis (given/family kind) has **no
home in the schema**, so the model cannot be tempted to emit a name-order guess.
Its only outputs are the label, alias forms (composition), and the four typed
properties (type, only where marked).

### 2. Three axes, distinguished

- **Composition role** — decidable from the source's own notation; honored.
- **Kind** (given/family) — never asserted (no property; §1).
- **Type** (birth/married/nickname/official) — recorded only where a source marks
  it; the marker may be contextual, not just a fixed token.

### 3. LLM-primary normalization (articulable standards in the extraction prompt)

The primary normalization moves into the entity-manifest / chunk extraction
prompt as explicit standards:

- Compose only the forms the source's notation and context **license** — never a
  blind cross-product (a multi-axis name like "Klara/Clara Löbig/Lobig"
  is capped, not exploded), and never a contiguous surface form the source never
  used.
- Produce one **clean canonical label** (the fullest attested form, annotations
  stripped) — never the slash-mash and never an annotation-bearing label.
- Strip annotations in any language/script; where an annotation marks a *type*
  (a maiden née/geb./z domu, a marriage, a legal/official change), emit the
  existing typed-name statement **with a located source excerpt**.
- **Never** infer given vs. family from word order or position.

### 4. Thin deterministic validator (not a parser)

`names.py` is a small, language-agnostic, idempotent **validator** — it enforces
mechanical invariants, it does no multilingual semantics:

- Drop an alias whose stripped ≥3-char token set is a strict subset of the
  label's tokens (bare givens, initialisms) — recall-safe by the semantics of
  `_name_tokens` / `_tokens_overlap`; keep genuinely non-derivable forms
  (`Gustl`).
- Keep hyphenated **and** space-separated compound / nobiliary-particle surnames
  whole as single alias units (`A-B`, `de la Cruz`, `van der Berg`,
  `ben Yosef`) — never shatter them into particle tokens.
- Guard a composed alias against introducing a token absent from the attested
  forms (a hallucination check).

The **GEDCOM** path stays deterministic, but that is reading structured *tags*
(`SURN`/`GIVN`/`NAME TYPE`/`NICK`/`_MARNM`), not parsing free text — not the
brittle layer §3 avoids.

### 5. Frontend factors, never fabricates

Composed forms stored as aliases are **factored** for compact display
(longest-common-prefix → "Hildegard (Laudler / Löbig)"), and
short-forms and succession are derived at render — the same derive-at-frontend
posture as kinship, co-residence, and possibly-living status. Factoring stored
compositions is pure string work; it never invents a permutation the source did
not license. This is the composition role of §2, reconstructed for display
without any stored kind.

### 6. Confidence and axis-resolution are first-class and surfaced for scrutiny

> **Generalised by ADR-0023.** The per-name, per-axis framing below is a special
> case of the universal statement axes (subject/predicate/value/existence).
> ADR-0023 supersedes it with the general mechanism — a machine-readable
> `Finding.axis` on the extraction critic, inherited by every property — and rules
> that machine confidence stays out of the graph, with reappraisal delivered as a
> structural query, not a stored scalar. Read this section as the name instance of
> that general rule; where they differ, ADR-0023 governs.

The "live flow is the eval" strategy (§7) only has teeth if a reviewer can *see
and attribute* the contentious calls. So the LLM must express, per decision,
**which axes it resolved and its confidence/basis**, and the harness routes the
risky ones to the reviewer through machinery that already exists:

- **Typed-name claims** carry the existing `Claim.confidence` field (surfaced in
  the review CSV `confidence` column) and a basis distinction — a type marked
  explicitly in the source vs. one inferred from context — so a shaky or
  context-only typing is low-confidence and scrutinised.
- **Alias-layer decisions** (a chosen composition, an annotation strip, a
  short-form drop, an overloaded-`/` read that could be an *aka* or a
  transliteration variant) are surfaced through the extraction critic's existing
  `target` grammar, which already addresses aliases (`"e01 alias: X"`), into the
  `critique_notes` column (ADR-0013 / ADR-0020). Low-confidence, context-inferred,
  or overloaded-glyph decisions are flagged there, so the most-likely-contentious
  items are the easiest to find at the claim gate.

No new graph property is needed for this; it is per-decision metadata the LLM
emits, carried by the existing `confidence` field and the critic/critique_notes
channel.

### 7. Validation is the live review flow, not a synthetic eval

> **Extended by ADR-0025.** ADR-0025 reaffirms this ruling and extends it to
> the whole corpus. In the development stage the live flow is the
> whole-corpus scratch run: its run report, its diff against the latest run
> on main, and the views of its graph are where errors show, each
> attributable to a stage and to the key component that changed, and fixes
> go into the pipeline, never into the graph. No gold set or synthetic eval
> is built. From the keeper run on, cluster review and ratification are the
> human review this section relies on.

No toy eval set is built. The pipeline is file-based and auditable (ADR-0019):
every machine-written claim references its extraction-run item, every
intermediate (transcription, entity manifest, batch, critique) is a durable,
inspectable file, and normalization decisions surface — with their confidence
(§6) — in the review CSV. So a normalization error is **visible**, **attributable
to the pass that made it**, and becomes the basis for refining that pass's prompt
or the validator. This spends the human review that is happening anyway on real,
representative cases rather than on toy examples that may not generalise.

## Rulings

1. **No name-part property; kind is never asserted.** The undecidable given/family
   axis has no schema home (source-first by construction).
2. **Composition is honored, type only where marked.** The LLM composes the forms
   the source licenses (never a blind cross-product) and emits a typed name only
   where a source marks the type — the marker may be contextual — always with a
   located excerpt. Never infer kind or type from position.
3. **LLM-primary; the deterministic layer is a thin validator + GEDCOM tags**,
   never a multilingual free-text parser.
4. **Per-axis confidence/basis is emitted and surfaced**: the `confidence` column
   for typed-name claims (with a marker-explicit vs. context-inferred basis), and
   the critic's `alias:` / `claim:` targets → `critique_notes` for alias-layer
   decisions.
5. **Live-flow validation; no synthetic eval** — errors are surfaced, attributed,
   and fed back into the harness.

## Consequences

- The three pathologies are fixed: the alias bag holds honest, *licensed*
  composed appellations plus a clean canonical label; typed names are enriched
  wherever a source marks a type, each with a source excerpt.
- The undecidable given/family axis is **structurally impossible to assert**, so
  the LLM's well-known eagerness to apply Western name order cannot corrupt the
  graph — the schema, not a guard, prevents it.
- **New frontend obligation**: factoring/composing display forms and deriving
  short-forms/succession from atoms + the shared reference (a dependency to
  build; until then, atoms render as a flat alias list — the same posture as
  other frontend-derived data).
- Review scrutiny is *steered* to contentious items by per-axis confidence +
  `critique_notes`; the errors that surface are the harness-refinement signal
  (the live eval).
- Non-determinism of LLM normalization is **not a regression** — names in the LLM
  path are already LLM-produced; the human review gate, the deterministic
  validator, and the durable run artifacts are the stability and audit anchors.
- Docs move with the code: this ADR + a short ADR-0011 amendment (pointer +
  re-grounded deferral) + a MODELING.md "Names" update (the composition/kind/type
  axes, the alias-hygiene rules, the derive-at-frontend rule); unit tests for the
  deterministic validator's invariants and the GEDCOM path; the extraction-prompt
  standards; and the live flow as the semantic eval.

## Open questions (deferred, not blocking)

- The multi-axis composition **cap** value (how many alternating axes/forms before
  the LLM stores atoms and defers composition entirely to the frontend). Tracked
  in #140.
- Whether alias-layer decisions on brand-new (unmatched) entities — which today
  pass through a thinner review surface than claims (`export_equivalences_csv`
  reviews matched proposals only) — eventually warrant their own lightweight
  review view, beyond the `critique_notes` flag. See #112 and #137.
- The frontend factoring/derivation layer is a separate build with its own
  design (search must resolve "Hildegard Löbig" when the label is
  "Hildegard Laudler" and the atoms/compositions are aliases). Tracked in #140.
- If a concrete position-typed-surname query need or a space-compound blocking
  defect ever materialises, reopen a *targeted* feature — not a per-person tier.
  In the deferred-on-trigger register, #171.
