# ADR-0037: The transcription record — a record per page, read by code instead of the wording

Status: **accepted** (owner decision 2026-09-29, #389). Like ADR-0031
and ADR-0036, the owner's merge of the PR that adds it is its
acceptance. It takes in #214 (its split of the check into fail and flag
becomes the fallback for wording), amends ADR-0008 and ADR-0015 in place,
and resolves the transcription items of ADR-0035 §10. It lands before
scratch run 1.

## Context

The vision transcriber answered one block of text per document, and code
read meaning from its wording (#385's sweep, #389):

- **The check** failed a transcription whose wording matched a failure
  or refusal signature. The signatures are English, so a refusal in
  German, Russian or Frisian passed; and a letter about family research
  can say what an engine says ("I couldn't open the attachment"), so
  every tightening failed some correspondence and every loosening let an
  engine failure through (#214). A false failure has no remedy: the
  document is transcribed again on every run and never ingested.
- **Page numbers** were found by a regular expression, and extraction
  guessed them from free text.
- **Language** was detected over the whole text, the editorial marks
  included: the universe postcard's faded greeting "Liebe Klara,
  [illegible] [illegible] [?] Dein Vater" read as German at 0.42 and fell
  back to `en`; without the marks it reads as German with confidence.
- **Narration.** The stream assembly joined every assistant text block of
  a call, so the model's narration before a tool call ("Let me read the
  image.") stayed in the answer.
- **Document class.** Nothing stated a document's class, so the anomaly
  checks compared yields within the file kind, and captions, certificates
  and registers shared one median.
- **Truncation.** An answer the engine stopped at its output limit
  (#7 records the stop reason) was not acted on.

## Decision

### 1. The record

The engine answers the transcription prompt with one JSON object, the
transcription record, and nothing else (`yizkr.pipeline.
transcription_record`):

| Field | What it holds |
|---|---|
| `document_class` | the document's class: one of the manifest's artifact classes (the items that are a `subclass_of` artifact: `photograph`, `certificate`, `letter`, `census_page`, …), or `unclassified` when none fits |
| `pages[]` | one object per page, in the media's order. An image is one page, unless it shows several pages of a document (an open book) |
| `pages[].page` | the page's place in the media, from 1: the **physical index** |
| `pages[].printed_page` | the page number printed or written on the page, exactly as written (`"iv"`, `"12"`, `"3a"`), or null: a field of its own, never the index |
| `pages[].status` | `transcribed` (its text was read, in whole or in part), `blank` (no text), `illegible` (text of which nothing can be read) or `not-a-document-page` (a colour chart, a ruler, a scanner's cover) |
| `pages[].text` | the page's text exactly as written, with the editorial marks `[illegible]`, `[?]`, and `[redacted]` where text is blacked out or covered; no commentary |
| `pages[].languages` | the language codes the page's text is written in, the one that holds most of it first |
| `pages[].notes` | the engine's own words about the page (damage, a stamp, another hand), never part of its text |

The record is strict: every page has a status, the pages are numbered
1, 2, 3… in order, a page called `transcribed` holds text, the class and
the status come from the vocabulary above, the language codes are
codes, and no other field is allowed. An answer that is one fenced code
block holding the object is read as the object; anything before or after
it is not.

### 2. The transcription view carries the record

A vision transcription's `pages` hold the record's fields beside where
each page's text lies in `text` (`page` stays the physical index, and
the printed number is `printed_page`), and the view holds the record's
`document_class`. The text opens every page with its `[Page N]` marker,
N the physical index, so that each claim can carry its page; a page with
nothing to read is its marker alone.

**Content and provenance.** The record's fields are content: a change to
a page's printed number, status, languages or notes, or to the class,
changes the transcription's content hash, as a change to its text does,
and extraction is made again. Each page's provenance (the method that
read it and the engine) and the check's flag are not, as the engine is
not (`TRANSCRIPTION_VOLATILE`).

**Vision pages of a PDF read page by page** (ADR-0008, as amended) fill
the same fields: a page read by vision takes them from its reading's
record, merged into one page where its image shows several (the texts in
order, the printed numbers joined, the languages in order of mention,
the notes joined, and `transcribed` if any of them is); a page read from
its text layer is `transcribed`, with no printed number, languages or
notes, since no engine read it; and a page with nothing to read is
`blank`. Such a PDF has no `document_class`: no call saw it whole.

**Page numbers for extraction** are read from the record through
`Transcription.printed_pages()`: the printed page numbers by physical
index. Extraction's chunking does not read them yet; that is a
follow-up, since the chunker is being rewritten in parallel (#407).

### 3. The check reads the record

The transcription check (`yizkr.pipeline.transcription_check`, version
3) fails a transcription only on an unambiguous signal, and the document
is transcribed again on the next run:

- a tool error the engine's stream reports on the input (#213);
- an answer the engine stopped at its output limit: the claude CLI gave
  up on it (`OutputLimitReached`), or the engine reports the stop reason
  `max_tokens` of it (`cut-off`);
- an answer that does not parse as the record, a page without a status
  among the ways, or a stored vision transcription that holds none
  (`not-a-record`). A refusal in any language is not the record, so it
  fails without any signature;
- a record that gives fewer pages than the media has (`pages-missing`);
  more is allowed, for an image of an open book;
- a blank output, or, for a document of two or more pages, fewer than
  three letters or digits per page the record calls `transcribed`
  (`too-short`); blank, illegible and not-a-document pages are the
  engine's plain statement and need no text;
- a token no source document writes (``pdftoppm``, a traceback, an API
  error) or an output that is wholly the engine's announcement: the
  signatures of the **fail** tier.

The other signatures, phrases in the engine's voice that a letter may
also write, are the **flag** tier (#214's fallback): a match passes the
transcription, which goes downstream carrying the signature's name in its
`flag`, and raises a `transcription-flag` alert for review
(docs/ANOMALIES.md). They read the record's text only, never its notes.

### 4. The language hint comes from the record

A vision transcription's language is the one that holds most of the
record's text: each page's letters and digits, outside the marks, count
toward the first language it lists, and a tie goes to the language
listed first; a record that names none takes the fallback code. The
resolution is recorded as `record`. An explicit `--language` still
forces its code. py3langid runs only on a text that has no record (a text
file, a correction, slide text, a PDF read page by page), and reads it
outside its editorial marks (ADR-0015, as amended).

### 5. The answer is taken from the stream's structure

An answer's text is what the model says after the call's last tool
result (a user event carrying a `tool_result` block); a continuation
across assistant turns with no tool call between them still joins,
without a separator. A model that wrote text only before its last tool
result gave no answer. The result event answers only a stream whose
assistant events hold no text.

### 6. Yields compare within a document class

The anomaly checks' distributions (entity and claim yield, critic rate)
compare a document with the documents of its class: the class its record
gives. A text with no record takes its class from the entity manifest
once the manifest states one; until then it is `unclassified`, compared
within its kind among the unclassified documents, and each distribution
alert says so (`document_class`, and `class_source`: `record` or
`none`).

### 7. Cache keys and the seeded archive

The changed prompt changes every transcription call's cache key, which is
intended: scratch run 1 transcribes the corpus afresh, about 21 more
calls by #144's estimate. The transcriptions an earlier `ingest run`
left were made with the earlier prompt, so `scratch
import-transcriptions` adopts none of them; and one that were offered
under the current prompt would still need to be the record, since the
import judges an archived reading as the run judges an answer.

## Consequences

- A refusal or an engine failure note in any language fails, because it
  is not the record; correspondence that shares an engine's words is
  never failed for them, only flagged.
- The run report counts the resolution `record` beside `detected`,
  `fallback` and `override`.
- A view made before the record fails the check on reuse (`not-a-record`)
  and is made again; with the prompt changed, its call misses the cache.
- The prompt names the manifest's artifact classes, so a change to them
  changes the transcription calls' keys.
- **Follow-ups.** Extraction reads page numbers from the record
  (`Transcription.printed_pages()`), once #407's chunker lands; a text
  with no record takes its class from the entity manifest, once the
  manifest states one; the flag tier's signatures are revisited with the
  first scratch runs' `transcription-flag` alerts (#214).
