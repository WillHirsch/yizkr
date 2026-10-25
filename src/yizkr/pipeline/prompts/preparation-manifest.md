One more thing to read, from the same transcription: what the document says about how its own text was prepared. Look for a statement in which the document says that its own text, or part of it, was prepared with machine assistance: that its translations were made with translation software or a machine translator, that its text was recognised by a machine (OCR) and not checked, or that a computer program or an AI tool wrote or translated it.

- Only the document's own words count. Never judge from how its text reads: a text that reads like a machine translation, or holds the errors one makes, but says nothing of how it was made, has no such statement.
- A statement about some other text is not about the document's own text: a letter it quotes that says it was typed, a program it writes about, a note on how a photograph was scanned or restored.
- The statement must say that a machine took part. A translation the document says its compiler made, or a text it says was retyped, says nothing of a machine.
- When there is such a statement, quote it verbatim, as a short passage the transcription holds word for word, and say what it covers: "all" when it names no part of the text, or names all of it; otherwise each passage it names (each translation, say), by the first and the last of the numbered lines that hold that passage's text. A line that holds only other text (a caption, the compiler's own commentary, the statement itself) is not part of a passage the statement names.

{pointer}

Give it as the key "preparation" of the same JSON object as "entities", one of:

```text
"preparation": {{"statement": null}}
"preparation": {{"statement": "<the statement, verbatim>", "covers": "all"}}
"preparation": {{"statement": "<the statement, verbatim>",
                "covers": [{{"first_line": 4, "last_line": 6}}]}}
```
