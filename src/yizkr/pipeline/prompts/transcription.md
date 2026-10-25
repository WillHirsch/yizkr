Transcribe this historical document (an image, or a PDF of one or more pages) exactly as written, and answer with the transcription record: one JSON object and nothing else, in this form:

```text
{{"document_class": "...", "pages": [{{"page": 1, "printed_page": null,
"status": "transcribed", "text": "...", "languages": ["de"],
"notes": ""}}]}}
```

- "document_class": what the document is, one of {listed}, or "{unclassified}" when none of them fits.
- "pages": one object for each page, in order. An image is one page, unless it shows several pages of a document (an open book): then one object for each page it shows. A PDF gives one object for each page of the PDF, never more: where a page of the PDF shows two pages of a document (a spread), its object holds the text of both, in reading order.
- "page": the page's place, 1 for the first, 2 for the next, and so on.
- "printed_page": the page number printed or written on the page, exactly as written ("iv", "12", "3a"), or null when it shows none.
- "status": "transcribed" for a page whose text you read, in whole or in part; "blank" for a page with no text (a photograph without any writing, an empty reverse); "illegible" for a page with text of which nothing can be read; "not-a-document-page" for anything that is not a page of the document (a colour chart, a ruler, a scanner's cover).
- "text": every piece of text visible on the page, exactly as written. Preserve line breaks, spelling mistakes, abbreviations and uncertain readings; mark illegible passages with [illegible], uncertain readings with [?], and text that is blacked out or covered with [redacted]. Do not translate, do not normalise, and put no commentary in the text. An empty string for a page with no text.
- Tables: write every table, list in columns or form grid, ruled or not, one line per row, with the row's cells separated by " | ". The header follows the same rule: a header of several lines is written one line per header line, and a heading that spans several columns stands in the first of its cells. Keep each empty cell as an empty field, so that every row has as many cells as the table has columns and each value stays in its column. A cell whose text runs over several lines stays on its row's line, its lines joined by a space; this is the one exception to preserving line breaks. Running prose is never laid out this way, even where it is printed in several columns (a newspaper's columns, a page set in two columns): write each column in turn, in reading order. A title, or a note above or under a table, is written as it stands. For example, a register with the columns Name, Alter, ledig, verh. and verw., whose one entry is marked in the last column:

  ```text
  Name | Alter | ledig | verh. | verw.
  Löbig, Eugen | 73 |  |  | x
  ```

- "languages": the language codes (ISO 639-1, such as "en", "de", "ru", "fy") of the languages the page's text is written in, the one that holds most of it first; an empty list for a page with no text.
- "notes": anything you have to say about the page (damage, a stamp, a hand other than the main one), or an empty string.
