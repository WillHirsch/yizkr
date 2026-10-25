You identify PLACES for a family archive. Each question below gives the chain of containers a source states for a place, nearest first, and names one container of that chain. More than one existing place may bear that name, or the source's name may fit none of them: decide which of the numbered candidates the source means by it, from the chain and from what is known of each candidate. Answer only which place the name means, never whether one place lies within another.

For every question output one NDJSON line, naming the question and the place by their numbers:

```text
{{"question": <its number>, "place": <a candidate's number, or
"uncertain" when you cannot tell or no candidate fits>,
"confidence": <0-1>, "evidence": "<one sentence>"}}
```

No wrapper object, no code fences, no prose. End with exactly:

```text
{{"done": true}}
```

Questions:

{questions_block}
