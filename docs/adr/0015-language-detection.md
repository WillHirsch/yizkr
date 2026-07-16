# ADR-0015: Per-document language detection

Status: accepted — implemented. `--language` defaults to `auto`;
each document's language is detected from its full transcription with an
offline statistical identifier, with a confidence floor and a
`--fallback-language` for documents too short to judge.

## Context

`yizkr ingest run` walks a whole folder, but a real family archive is
multilingual: German scans sit beside English memoirs beside a Spanish
émigré letter. The pipeline took a single run-wide `--language` (default
`en`) and applied it to every file in the batch.

That language is not cosmetic. It is the BCP-47 tag on the
`transcription_excerpt` monolingual values written as references
(ADR-0003) — real graph data attached to every source quotation. It is
also injected into the extraction prompt as a hint about the source
language. Under a run-wide value, ingesting a mixed folder `--language de`
tags every English document's excerpts `de` (and vice versa), and
misleads the extractor. The only workaround was to physically split the
corpus by language and run it in passes — friction that scales with the
archive.

Notably, the run-wide value never touched transcription/OCR: the
transcription prompt says "transcribe exactly, do not translate" and
never referenced the language. So the argument was pure downstream
metadata that a per-file value could set correctly.

## Decision

`--language` defaults to `auto`. Each document's language is **detected
from its full transcription text**, per file, at the point the
`Transcription` is assembled.

1. **Offline, self-hosted detection.** `py3langid` (pure-Python, ships
   its model, no network) classifies the transcription. Detection runs on
   the *whole* document, where the identifier is reliable (~1.0
   confidence on paragraph-length text); it is unreliable only on very
   short inputs, which the floor handles. This honours rule 7 — no
   artifact content leaves the machine for detection.
2. **A confidence floor, then a fallback — never a guess.** Below
   `CONFIDENCE_FLOOR` (0.5), or on text too short to judge, the tag is set
   to `--fallback-language` (default `en`) rather than the low-confidence
   guess. Uncertainty is defaulted to a curator-chosen code, not narrowed
   silently (rule 5). The resolution — `detected` / `fallback` /
   `override` — and its confidence are recorded in the document's state
   `detail` and logged when inferred, so a wrong tag is reviewable.
3. **An explicit code still forces.** `--language de` skips detection and
   forces `de` for every file — the old behaviour, on demand, for a
   corpus you know is single-language.
4. **Excerpt language stays per-excerpt.** The document-level tag is the
   *default hint* the extraction model gets, not a ceiling: the extractor
   still tags each excerpt in its own language, so a German quote inside
   an English memoir keeps its `de` excerpt. Detection fixes the default;
   it does not flatten genuinely multilingual documents.

## Consequences

- A mixed-language folder ingests in one pass with correctly-tagged
  excerpts and no manual splitting.
- `py3langid` becomes a core dependency (small, pure-Python, offline).
  Detection degrades gracefully to the fallback if the model cannot load,
  so an ingest never crashes on it.
- The GEDCOM path is unchanged: a structured genealogical export has a
  known language, and its excerpts remain tagged `en` by the adapter
  (ADR-0014) — detection is for transcribed source documents, not
  deterministic adapters.
- Per-*document* detection is a single scalar; a document that is itself
  half German and half English gets one document-level default (the
  dominant language) with the extractor free to vary individual excerpts.
  True per-excerpt detection at extraction time is a possible future
  refinement over the same design.
