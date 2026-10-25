You segment a document transcription into semantic units for genealogical claim extraction. A unit is a self-contained region a person could extract claims from in isolation: one person's entry in a family tree, one narrative section, one table. Prefer natural units over fixed sizes.

Output ONE JSON object, no prose, no code fences:

```text
{{"units": [{{"id": "u01", "label": "<short name>",
"kind": "<person entry|narrative section|...>",
"first_line": <int>, "last_line": <int>,
"context": "<what a reader needs from OUTSIDE the unit,
e.g. the ancestor path in a tree: 'child of X and Y (generation 2)'>",
"extract": <true, or false for regions asserting no facts about
people, places or events
(logistics, boilerplate, tables of contents)>}}, ...]}}
```

Rules:

- Units must not overlap. Cover every line that asserts extractable facts; lines you skip are audited as gaps.
- Mark non-assertive regions extract=false rather than skipping them.
- Line numbers refer to the numbering below (1-based, inclusive).

Transcription of artifact sha1:{artifact_sha1} (language: {language}):

{numbered_text}
