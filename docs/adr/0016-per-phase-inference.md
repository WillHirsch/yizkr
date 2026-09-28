# ADR-0016: Phase-tiered inference (effort and model per pipeline phase)

Status: accepted — implemented. Reasoning effort and model are chosen
**per pipeline phase**, not per run. Mechanical bulk extraction defaults
to low effort on a discipline-first model (`claude-sonnet-5`); the
judgment steps keep the run's main model at high effort.
Amended by #128 (2026-09-28): `ingest run` no longer adjudicates, since
its reconcile step reads no roster (ADR-0007, as amended), so there
`--review-effort` and the main model govern the critique alone.

## Context

The pipeline runs one document through distinct phases with different
demands: vision transcription and chunked claim extraction are
high-volume and mechanical; adjudication, equivalence, and critique are
low-volume, high-stakes judgment. A single run-wide model and effort
level served all of them.

Two observations broke that assumption.

**Effort.** Adaptive-reasoning models (Sonnet 5, Opus 4.8) ignore
`MAX_THINKING_TOKENS`; the only lever over how much they deliberate is
the `--effort` level. Captured extraction reasoning on a birth
certificate showed the mechanical passes spending 10–15K output tokens
re-deriving already-settled decisions — a ~6× thinking:input ratio to
emit ~18 claims. Deep thinking on a high-volume mechanical step is
mostly waste; on a judgment step it is the point.

**Model.** A controlled experiment ran the *same* frozen extraction
chunk (a birth certificate: three people and one address) across five
model/effort configurations. The finding that mattered was not raw
capability but **source discipline** — extracting only what the source
asserts (rule 5), not what it plausibly implies:

| Model (low effort) | Tok/claim | Discipline |
|---|---|---|
| Fable 5 | 253 | over-infers parent sex; propagates one address to a spouse |
| Opus 4.8 | 286 | clean sex; mild spouse-address over-reach |
| **Sonnet 5** | 382 | **cleanest — no inferred sex, no propagated residence** |
| Sonnet 4.6 | 595 | over-infers sex; mishandles the address's field-owner |

Every model got the load-bearing facts right (birth date, places of
birth, maiden name). They differed in whether they invented claims the
source only implied — a father's sex from the "FATHER" role label, a
spouse's residence from the informant's address. For a source-first
archive those inferences are defects, and the newer, more advanced
models were not uniformly better at avoiding them: Sonnet 5 was the most
restrained while also thinking roughly half as much as Sonnet 4.6.

## Decision

Inference configuration is **phase-tiered**.

1. **Effort per phase.** `--extract-effort` (default `low`) governs the
   mechanical bulk steps (transcription, extraction); `--review-effort`
   (default `high`) governs adjudication/equivalence and critique. Low
   where thinking is wasted, high where the decision is worth it.
2. **Model per phase.** `--extract-model` (default `claude-sonnet-5`)
   overrides the model for the **claim-extraction step only**. The run's
   main model (`$YIZKR_LLM_MODEL`) still drives transcription and every
   judgment step. Extraction rewards discipline over capability, so it
   defaults to the most source-faithful model measured; the judgment
   steps, where capability pays off, stay on the operator's chosen model.
   `--extract-model inherit` opts out — extraction uses the main model
   too.
3. **Scope is deliberately narrow.** The override covers claim
   extraction, not transcription: transcription is a vision task the
   experiment did not measure, and the strongest available model reading
   the image is a coherent default. Only the step whose discipline the
   experiment measured is retargeted.
4. **Engine-agnostic.** The model threads to both the `claude-code` and
   `anthropic` clients via `make_client(..., model=...)`; effort applies
   to `claude-code` only (the API path has no equivalent lever yet).

## Consequences

- The default ingest runs disciplined extraction (Sonnet 5, low effort)
  while leaving adjudication and critique on the run's main model — the
  operator picks capability where it matters and inherits discipline
  where it matters, with no flags.
- A cheaper/faster or maximally-capable extraction model is one flag
  away (`--extract-model`), and reverting to a single-model run is
  `--extract-model inherit`.
- The choice is empirical and will drift as models change. The frozen
  chunk and the runner are reproducible, so re-measuring when a new model
  ships is cheap; this ADR records the method, not a permanent ranking.
- Sonnet 4.6 remains useful for *diagnosis* — it is the newest model that
  still exposes readable (summarized) thinking, so it is the model to run
  when inspecting *why* extraction behaves as it does — but it is not the
  production default: it is the slowest, most token-hungry, and least
  disciplined of the models measured.
