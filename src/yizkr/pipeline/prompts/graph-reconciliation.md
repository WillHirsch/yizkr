You adjudicate whether pairs of items ALREADY in a family archive graph are the same real-world entity (duplicates from separate documents). Weigh RELATIONAL evidence over spelling: same spouse, same parents, compatible dates. Fuller and shorter forms of one name are common.

Verdicts: "same", "uncertain", "distinct".

For every pair below, output one NDJSON line:

```text
{{"entity_id": "<QID A>", "qid": "<QID B>",
"verdict": "same|uncertain|distinct",
"confidence": <0-1>, "evidence": "<one sentence naming the deciding facts>"}}
```

No wrapper object, no code fences, no prose.

After the last line, end with exactly one final line:

```text
{{"done": true}}
```

Pairs:

{pairs_block}
