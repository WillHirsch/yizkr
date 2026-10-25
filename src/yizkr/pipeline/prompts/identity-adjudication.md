You adjudicate IDENTITY for a family archive. Each numbered pair below names two mentions, A and B, each from a source document, with what that source says about it. Deterministic clustering found the two alike but could not decide whether they are one entity: decide, from the evidence the sources give, whether they are the SAME entity, DIFFERENT entities, or whether the evidence cannot tell.

Weigh relational evidence first: kin and spouses, dates, places, roles in events. A shared name alone is weak evidence, a common surname weaker still: two people of one name are often two people. Sources disagree about dates and spellings and can still describe one person, so a difference is evidence, not proof. A date is given at the precision its source states: "the 1830s" is a decade and "the 19th century" a century, never a year within them. Answer `different` only when the evidence shows two entities, and `uncertain` when it does not decide; never guess.

Each pair also gives clustering's comparison as name/dates/kin (how the names, the dates and the kin compare) and, after a further slash, what kept it from linking, when something did.

For every pair output one NDJSON line:

```text
{{"pair": <its number>, "verdict": "same" | "different" | "uncertain",
"reason": "<one sentence>"}}
```

No wrapper object, no code fences, no prose. End with exactly:

```text
{{"done": true}}
```

Pairs:

{pairs_block}
