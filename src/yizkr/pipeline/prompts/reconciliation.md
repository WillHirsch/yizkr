You adjudicate entity identity for a family archive. For each entity extracted from a new document, decide whether it is the same real-world entity as each candidate already in the graph. Weigh RELATIONAL evidence over spelling: the same spouse, the same parents, compatible birth/death dates, or an alias relationship between names is strong; a shared surname alone is weak. People recur across family documents under fuller or shorter names. Typed names are strong evidence when they line up: one entity's married name equalling another's birth/maiden name (a name change on marriage), or a shared nickname, points to identity — a differing birth name to distinctness.

Verdicts: "same" (confident identity), "uncertain" (plausible, needs the reviewer's judgement), "distinct" (different entities).

For every (entity, candidate) pair below, output one NDJSON line:

```text
{{"entity_id": "<id>", "qid": "<Q..>", "verdict": "same|uncertain|distinct",
"confidence": <0-1>, "evidence": "<one sentence naming the deciding facts>"}}
```

No wrapper object, no code fences, no prose. After the last line, end with exactly one final line:

```text
{{"done": true}}
```

Entities from the new document:

{entities_block}

Candidates already in the graph:

{candidates_block}
