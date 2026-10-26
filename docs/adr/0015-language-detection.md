# ADR-0015: Per-document language detection

> **Temporary deprecated material — TODO [#730](https://github.com/WillHirsch/yizkr/issues/730):** this document still mixes current design details with development history and superseded prescriptions. Its historical material is not authoritative for new policy; use [REQUIREMENTS](../REQUIREMENTS.md), [CONTRACT](../CONTRACT.md) and the implemented code. Remove this note only after rewriting the document as current guidance with necessary rationale, or consolidating it elsewhere and deleting this file. The linked task tracks removal of all remaining historical and obsolete passages.

Status: accepted — implemented. `--language` defaults to `auto`; each document's language is detected from its full transcription with an offline statistical identifier, with a confidence floor and a `--fallback-language` for documents too short to judge.  
Amended by #389 (2026-09-29): a vision transcription's language comes from its transcription record (ADR-0037), whose pages name the languages they are written in: the document's is the one that holds most of its text (resolution `record`), and a record that names none falls back. Detection runs only on a text that has no record (a text file, a correction, slide text, a PDF read page by page), and reads it outside its editorial marks (`[illegible]`, `[?]`, `[redacted]`, page markers), which a short text would otherwise be read by.

## Context

`yizkr ingest run` walks a whole folder, but a real family archive is multilingual: German certificates sit beside an English oral history and naturalisation papers. The pipeline took a single run-wide `--language` (default `en`) and applied it to every file in the batch.

That language is not cosmetic. It is the BCP-47 tag on the `transcription_excerpt` monolingual values written as references (ADR-0003) — real graph data attached to every source quotation. It is also injected into the extraction prompt as a hint about the source language. Under a run-wide value, ingesting a mixed folder `--language de` tags every English document's excerpts `de` (and vice versa), and misleads the extractor. The only workaround was to physically split the corpus by language and run it in passes — friction that scales with the archive.

Notably, the run-wide value never touched transcription/OCR: the transcription prompt says "transcribe exactly, do not translate" and never referenced the language. So the argument was pure downstream metadata that a per-file value could set correctly.

## Decision

`--language` defaults to `auto`. Each document's language is **detected from its full transcription text**, per file, at the point the `Transcription` is assembled.

1. **Offline, self-hosted detection.** `py3langid` (pure-Python, ships its model, no network) classifies the transcription. Detection runs on the _whole_ document, where the identifier is reliable (~1.0 confidence on paragraph-length text); it is unreliable only on very short inputs, which the floor handles. This honours rule 7 — no artifact content leaves the machine for detection.
2. **A confidence floor, then a fallback — never a guess.** Below `CONFIDENCE_FLOOR` (0.5), or on text too short to judge, the tag is set to `--fallback-language` (default `en`) rather than the low-confidence guess. Uncertainty is defaulted to a curator-chosen code, not narrowed silently (rule 5). The resolution — `detected` / `fallback` / `override` — and its confidence are recorded in the document's state `detail` and logged when inferred, so a wrong tag is reviewable.
3. **An explicit code still forces.** `--language de` skips detection and forces `de` for every file — the old behaviour, on demand, for a corpus you know is single-language.
4. **Excerpt language stays per-excerpt.** The document-level tag is the _default hint_ the extraction model gets, not a ceiling: the extractor still tags each excerpt in its own language, so a German quote inside an English interview keeps its `de` excerpt. Detection fixes the default; it does not flatten genuinely multilingual documents.

## Consequences

- A mixed-language folder ingests in one pass with correctly-tagged excerpts and no manual splitting.
- `py3langid` becomes a core dependency (small, pure-Python, offline). Detection degrades gracefully to the fallback if the model cannot load, so an ingest never crashes on it.
- The GEDCOM path is unchanged: a structured genealogical export has a known language, and its excerpts remain tagged `en` by the adapter (ADR-0014) — detection is for transcribed source documents, not deterministic adapters.
- Per-_document_ detection is a single scalar; a document that is itself half German and half English gets one document-level default (the dominant language) with the extractor free to vary individual excerpts. True per-excerpt detection at extraction time is a possible future refinement over the same design (the deferred-on-trigger register, #171).
