# ADR-0037: The transcription record — a record per page, read by code instead of the wording

Status: **accepted** (owner decision 2026-09-29, #389). Like ADR-0031
and ADR-0036, the owner's merge of the PR that adds it is its
acceptance. It takes in #214 (its split of the check into fail and flag
becomes the fallback for wording), amends ADR-0008 and ADR-0015 in place,
and resolves the transcription items of ADR-0035 §10. It lands before
scratch run 1.

Amended by #424 (2026-10-02): extraction locates claims by the printed
page numbers the record gives. Every extraction prompt shows a page's
printed number beside its `[Page N]` marker, on the marker's own line;
a claim keeps the physical page as its `page number` and gains the
printed one, from the record and never guessed, as a `printed page
number` in its source block (§2, "Page numbers for extraction").

Amended by #449 (2026-10-05): the claims review sheet shows a claim's
printed page number in a `printed_page` column beside `page`, empty
where its source has none. The sheet's locators are never read back, so
a curator's edit to the column is ignored and the value stays the
record's.

Amended by #502 (2026-10-16): a deck's transcription has pages too, one
per slide, read from its XML (the page method `slide-xml`): each slide
`transcribed`, or `blank` when it holds no text, with no printed number,
languages or notes, and the SHA-1s of the images it reads on its own; a
deck has no `document_class` (§2, "Pages of a deck").

Amended by #570 (2026-10-23): the prompt sets a table's layout. Every
table, list in columns or form grid, ruled or not, is written one line
per row, its cells separated by ` | `, each empty cell kept as an empty
field so every row has as many cells as the table has columns; a header
follows the same rule, and a cell whose text wraps stays on its row's
line, joined by a space. Running prose is never laid out so, even when
printed in columns (§1, "Tables"). The prompt is in every vision
transcription's cache key, so the next scratch run asks every vision
transcription again (§7).

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
| `pages[]` | one object per page, in the media's order. An image is one page, unless it shows several pages of a document (an open book). A PDF gives one per page of the PDF, never more: a page of the PDF that shows a spread (two pages of a document) holds the text of both, in reading order |
| `pages[].page` | the page's place in the media, from 1: the **physical index** |
| `pages[].printed_page` | the page number printed or written on the page, exactly as written (`"iv"`, `"12"`, `"3a"`), or null: a field of its own, never the index |
| `pages[].status` | `transcribed` (its text was read, in whole or in part), `blank` (no text), `illegible` (text of which nothing can be read) or `not-a-document-page` (a colour chart, a ruler, a scanner's cover) |
| `pages[].text` | the page's text exactly as written, with the editorial marks `[illegible]`, `[?]`, and `[redacted]` where text is blacked out or covered; no commentary |
| `pages[].languages` | the language codes the page's text is written in, the one that holds most of it first |
| `pages[].notes` | the engine's own words about the page (damage, a stamp, another hand), never part of its text |

The record is strict: every page has a status, the pages are numbered
1, 2, 3… in order (each `page` a JSON integer, so `"1"` or `true`
fails), a page called `transcribed` holds text, the class and the status
come from the vocabulary above, the language codes are codes, and no
other field is allowed. A missing or unknown status, an unknown class
and bad page numbers fail the answer.

**Tables.** A page's text keeps its line breaks, with one exception
the prompt states: every table, list in columns or form grid, ruled or
not, is written one line per row, with the row's cells separated by
` | `. The header follows the same rule: a header of several lines is
written one line per header line, and a heading that spans several
columns stands in the first of its cells. Each empty cell stays an
empty field, so every row has as many cells as the table has columns
and each value keeps its column; a cell whose text runs over several
lines stays on its row's line, its lines joined by a space. Running
prose is never laid out this way, even where it is printed in several
columns (a newspaper's columns, a page set in two columns): each column
is written in turn, in reading order. A title, or a note above or under
a table, is written as it stands. The carve-out is by what the text is,
not by its layout, since a rule by layout would put a newspaper's
columns side by side as rows and interleave their lines. Without the
rule each call chose its own layout (` | `, columns aligned with
spaces, or cells run together), and a row run together loses its empty
cells, and with them which column a value, such as a mark in one of two
adjacent columns, stood in (#570). The prompt's example is a register
row from the fictional universe:

```
Name | Alter | ledig | verh. | verw.
Löbig, Eugen | 73 |  |  | x
```

A few harmless near-misses of the record's shape say what the record
says, and are **normalised, not refused** (a Pydantic before-validator
on each field): a `printed_page` given as a JSON number becomes its
string (`12` and `12.0` are `"12"`), though one that is not finite
(`NaN` or `Infinity`, which Python's JSON reader takes as numbers)
fails, since no page prints one; a `text`, `notes` or `languages` given
as null is empty; and a language code is lower-cased (`"DE"` is `"de"`).
A record's language code, as a claim's, may carry a region subtag, so
`"de-DE"` is kept as `"de-de"`; were the record's language type ISO 639
only, it would be reduced to its primary subtag instead.

An answer that is one fenced code block holding the object (tagged
`json` or not) is read as the object; anything before or after it is
not. **Prose around the JSON** ("Here is the record:" before it, "I hope
this helps" after it) makes the answer not the record, and it is asked
again on the next run. That stays so for now: scratch run 1's count of
`not-a-record` failures decides whether the parser is loosened to take
the single JSON object an answer holds.

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
not (`TRANSCRIPTION_VOLATILE`). A stored flag is recomputed everywhere
it is read: a run's reuse of a view, an excerpt made from a kept image
reading (ADR-0008, as amended by #397) and `yizkr scratch recheck`
each carry the flag of the check made now, not the one stored with the
view or the reading, so a change to the flag tier reaches every stored
transcription without making any of them again.

**Vision pages of a PDF read page by page** (ADR-0008, as amended) fill
the same fields: a page read by vision takes them from its reading's
record, merged into one page where its image shows several (the texts in
order, the printed numbers joined, the languages in order of mention,
the notes joined, and `transcribed` if any of them is); a page read from
its text layer is `transcribed`, with no printed number, languages or
notes, since no engine read it; and a page with nothing to read is
`blank`. Such a PDF has no `document_class`: no call saw it whole. On
reuse, each page read by vision is checked as its reading was when the
page was made: with the one-page record its stored fields make
(`Transcription.page_record`), so that a page the record calls blank,
illegible or not a document page, which needed no text then, needs none
on reuse either. The PDF's flag is its first vision page's.

**Pages of a deck** (ADR-0008, as amended by #502): a deck is read slide
by slide as a PDF is read page by page, so its transcription's `pages`
hold one page per slide behind its `[Slide N]` marker: read from the
slide's XML (method `slide-xml`, engine `pptx-adapter`), `transcribed`,
or `blank` when the slide holds no text, with no printed number,
languages or notes, since no engine read it, and the SHA-1s of the
images on it that are read on their own. A deck has no
`document_class`, and no flag: no engine read its text.

**Page numbers for extraction** are read from the record through
`Transcription.printed_pages()`: the printed page numbers by physical
index. Amended by #424 (2026-10-02): extraction reads them in two
places.

- **The prompts.** The text every extraction prompt carries (the entity
  manifest's, the segmentation's, each chunk's document, units and
  mention lines, and the single call's) is `Transcription.prompt_text()`:
  the transcription's text with each page's printed number beside its
  own `[Page N]` marker, on that line, as `[Page 6] (printed page number
  "4")`, the number exactly as the record gives it (a page that shows
  two printed pages, `"12, 13"`, as joined). The line count is the
  text's, so every line number into the text (mention lines, segment
  ranges, the coverage audits) holds. The marker is found where the
  page's opening lies, between the page before's text and its own; an
  image's marker (`[Page 6, image 1: sha1:…]`) keeps its physical page
  and shows no number. `text` itself, the transcription's content, is
  unchanged, and a transcription whose record gives no printed number
  is carried as it is: its prompt text is the text itself. The chunk
  prompt and the single call's prompt ask for `page_number` as the
  physical N of the marker, never the printed number beside it, a rule
  both state for every document, whether its record gives printed
  numbers or not.
- **The locator.** After the model answers, the extractor sets each
  claim's printed page number itself, beside its source artifact
  (`cite_artifact`): the record's printed number for the page the claim
  cites, only when its `page_number` is one physical page, given as its
  digits alone, and the record gives that page a printed number. A
  range, a page given in words, a page with no printed number in the
  record and a transcription with no record get none, and whatever the
  model wrote there is dropped. The claim schema the prompts carry does
  not show the field, and a source without one serialises as before.
  The number describes the physical page the `page number` names: a
  page that shows a spread with only one half numbered keeps that one
  number for every claim read from it, the unnumbered half's included,
  since a PDF read page by page and one read whole give that page the
  same number. A printed number of whitespace alone is none, in the
  prompt as in the locator. Validation refuses a source that gives a
  printed page number without a page number, so a prepared claims file
  cannot write one alone. A `printed_page` holding a lone surrogate
  fails the record, as page text holding one fails the transcription.
  The writer puts it in the claim's `stated in` block beside the page
  number, as the reference property `printed page number` (MODELING.md,
  "Reference pattern"; ADR-0003). An excerpt is extracted from its own
  transcription, the reading of its image (ADR-0008, as amended by
  #397), so its claims never take the printed number of the container's
  page, and its `part of` the container keeps the container's physical
  page.

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
  more is allowed for an image, which may show an open book, but the
  record of a PDF read whole gives exactly the PDF's pages, and more
  fail (`pages-extra`), since its page markers would point at pages the
  PDF lacks. The prompt says so: a PDF gives one record page for each
  page of the PDF, and a page that shows a spread (two pages of a
  document) holds the text of both, in reading order. Scratch run 1's
  `pages-extra` count confirms that the engine keeps to it;
- a blank output, or, for a document of two or more pages, fewer than
  three letters or digits per page the record calls `transcribed`
  (`too-short`); blank, illegible and not-a-document pages are the
  engine's plain statement and need no text;
- in a text with **no record** only (a text file's), a token no source
  document writes (``pdftoppm``, a traceback, an API error) or an output
  that is wholly the engine's announcement: the signatures of the
  **fail** tier.

**With a record parsed, no wording fails a page's text**: every
signature, `narration-only` and `shell-error` among them, only flags it.
Inside a parsed record such a match can only misfire: narration and tool
output around the record already fail it as not the record, and the
answer is the text after the last tool result (§5), so a postcard's one
line "I will bring the documents to Stupfield." is a source's sentence,
not an announcement.

The other signatures, phrases in the engine's voice that a letter may
also write, are the **flag** tier (#214's fallback): a match passes the
transcription, which goes downstream carrying the signature's name in its
`flag`, and raises a `transcription-flag` alert for review
(docs/ANOMALIES.md). The flag tier reads the record's text and then each
page's `notes`, the engine's own words, each note on its own: a note
saying the engine could not see the image ("No image was provided.")
flags the transcription, and never fails it.

The signatures are English only, an accepted known limit: a refusal in
German, Russian or Frisian written as a transcribed page's text passes
unflagged, as does such a note. Outside a record, a refusal in any
language fails as not the record.

### 4. The language hint comes from the record

A vision transcription's language is the one that holds most of the
record's text: each page's letters and digits, outside the marks, count
toward the first language it lists, weighed by its primary subtag (so
`de-de` and `de` are one language, given by its code as first listed),
and a tie goes to the language listed first; a record that names none
takes the fallback code. The resolution is recorded as `record`. An
explicit `--language` still forces its code. py3langid runs only on a
text that has no record (a text file, a correction, a deck, a PDF
read page by page), and reads it outside its editorial marks (ADR-0015,
as amended). An excerpt (ADR-0008, as amended by #397) is the reading of
its image, so its language, like its class and its pages, comes from
that reading's record, and its flag is the one the check made now raises
on that reading (§2).

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
gives, whatever its kind. A text with no record takes its class from the
entity manifest once the manifest states one; until then it is
`unclassified`. Every `unclassified` document, whether its record says so
or it has none, is compared within its kind among the unclassified
documents, so that the unclassified scans and the PDFs read page by page
are one sample. Each distribution alert says which class it was measured
in (`document_class`, and `class_source`: `record` or `none`).

### 7. Cache keys and the seeded archive

The changed prompt changes every transcription call's cache key, which is
intended: scratch run 1 transcribes the corpus afresh, about 21 more
calls by #144's estimate. The transcriptions an earlier `ingest run`
left were made with the earlier prompt, so `scratch
import-transcriptions` adopts none of them; and one that were offered
under the current prompt would still need to be the record, since the
import judges an archived reading as the run judges an answer.

## Decided by the orchestrator (2026-09-29; the owner may override)

On the review's owner questions, following #389 and this ADR:

1. **Wording inside a record.** With a record parsed, no wording
   signature fails a page's text: `narration-only` and `shell-error`
   included, they only flag (§3). Without a record, the fail tier is
   unchanged.
2. **Near-miss shapes** are normalised, not refused: a numeric
   `printed_page` becomes a string; null text, notes or languages become
   empty; language codes are lower-cased, and a region-tagged code is
   kept, since the record's language type is not ISO 639 only (§1). A
   missing or unknown status, an unknown class and bad page numbers
   still fail.
3. **Prose around the JSON** stays not-a-record for now; scratch run 1's
   `not-a-record` count decides whether to loosen it (§1).
4. **Notes.** The flag tier also reads each page's `notes`, with the
   same signatures: a flag, never a failure (§3).
5. **Whole-PDF page counts.** The record of a PDF read whole gives
   exactly the PDF's pages: more fail, as fewer already do (§3).
6. **Page numbers.** `printed_pages()` without a caller is the intended
   scope; extraction reading them is a follow-up, #424 (§2). Amended by
   #424 (2026-10-02): extraction reads them, in its prompts and for each
   claim's printed page number (§2).
7. **Refusals in other languages** written as a page's text pass
   unflagged: English-only flag signatures are an accepted known limit
   (§3).

On the re-review's owner questions (2026-09-29; the owner may
override):

- **A. Stored flags** are recomputed everywhere they are read: a run's
  reuse, an excerpt made from a stored image reading, and `yizkr
  scratch recheck` (§2).
- **B. Illegible embedded images** stay excerpts, so that a reader sees
  they were there. Only an image whose record calls every page `blank`
  or `not-a-document-page` is no excerpt; a record of a transcribed page
  and a blank one (an open book's written page and its blank verso) is
  one (ADR-0008, as amended by #397).
- **C. Two-page spreads in a PDF.** The prompt asks for one record page
  per page of the PDF, with a spread's two halves in reading order
  within that page. `pages-extra` stays a failure, and scratch run 1's
  `pages-extra` count confirms it (§3).

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
- The physical-page rule is in the chunk prompt and the single call's
  prompt of every document, so every chunk and single-call key changes
  once, printed numbers or not, as do the manifest and segmentation keys
  of a document whose record gives printed numbers; the scratch diff
  against main shows it (ADR-0025, ADR-0032).
- **Follow-ups.** A text with no record takes its class from the entity
  manifest, once the manifest states one; the flag tier's signatures are revisited with the
  first scratch runs' `transcription-flag` alerts (#214).
