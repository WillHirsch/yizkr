# ADR-0018: Relational and perspectival appellations

Status: **accepted** — implemented (manifest `referred_to_as` + `perspective`, extraction-prompt routing, critic update). No retro-fit: the model applies to a clean re-ingest, not a migration of existing rows (see Rulings).  
Amended by #222 (2026-09-27): in "Reciprocal / dyadic naming", the example records the literal "Oma" as (A), `relative` + `kinship_type`, as Ruling 2 routes it, and the section tells the two statements apart by subject and ego (the `relative` object or the `perspective` qualifier).  
Amended by #459 (2026-10-05): the extraction prompts restate the rule where labels and aliases are asked for: a kinship word is never the label or an alias of a person the document names, alone ("Oma"), with a name, or as a writer's "your father". The name beside a kinship word follows the label and alias rules: "Aunt Pünktchen" gives the alias "Pünktchen", and a sole given name ("дядя Себастьян") is the label. Only a person the document never names is labelled with its kinship words ("Dein Vater" on the postcard); the critic's `kinship_term_alias` covers a kinship label too.  
Amended by [ADR-0035](0035-mention-records.md) (owner decision 2026-09-29, #385): a `relative` claim gains a controlled `kinship_class`, with the side, degree and removes its term states, beside the verbatim `kinship_type` (ADR-0035 §3.3).  
Amended by #434 (2026-10-02): a name is placed by whether it is absolute or relational, never by whether it is a pet name. A name the source gives as what the person is called, by anyone ("Peppi", "Gustl"), is a nickname: an alias, a record nickname (ADR-0035 §2.1), and a `nickname` claim only where the source marks the type; so is a name a family or other group calls the person ("Gustl" at home), and a name a writer simply uses for someone, in a greeting ("Liebes Pünktchen!") or a mention. A name the source gives as what one person calls another, with or without a stated perspective, or that names the person through a relation but is no literal kinship term ("Bubele", "Nana Maisie", and "Рудольфиха", "Rudolf's wife"), is `referred_to_as` (B): never an alias and never a record nickname. A literal kinship term stays (A), and a spousal term ("my husband") gives a marriage (ADR-0036). (B)'s "family-specific term of address" below means only a relational one, such as what a family calls a non-relative; `perspective` names that family only then. The chunk, manifest and record prompts say this in the same words, and the two properties' manifest descriptions state the same rule.  
Amended by #498 (2026-10-06): the critic's `kinship_term_alias` exempts a person the document never names, as the extraction prompts do: its label is rightly the document's own words for them, so the postcard's "Dein Vater" is no finding, while "Tante Rahel" as Rahel's label is one. The critic's entity rows show each person's mention-record status (ADR-0035 §2.1), which says whether the document names them.  
Amended by #499 (2026-10-16, the owner's decision): a relational or perspectival form is an alias too. A person's aliases hold every form a source uses to name or address them, without structural distinction: a name form, a kinship term with a name ("Tante Rahel"), a kinship term alone ("Omama"), a pet name ("Pünktchen", "Bubele") and a courtesy name ("Nana Maisie"); that the person was known by it, to at least someone, is what needs keeping. (B) is widened to literal kinship terms and pet names: each form that is no plain name form (a name, or a spelling, a shortening, a diminutive or an initial of one) is also one `referred_to_as` statement, verbatim, in its document with its locator, with `perspective` where the source gives the ego. The alias is the lookup; the statement is the evidence. A literal kinship term still also gives (A), `relative` + `kinship_type`, where the ego is an entity. A description that is no form of address ("the deceased", "the informant", "my husband", "our great grandmother", a role or form-field label) stays out of the aliases, and its kinship goes to (A) as before. The label stays the display name: a name form wherever the document names the person, the document's own words only for a person it never names. Where extraction labels a person by one of the document's appellations and the person's record names them, the label becomes the name (ADR-0035 §8, as amended). Identity is unchanged: a recordless mention never keys on a form its document gives as an appellation, so however many people are called "Oma" they never meet by it; a person with a record keys on its name parts; and a pet name the person is called by anyone stays a record nickname and keys as one. Places, organisations and families, never persons, key on marked forms as on any other name; persons and the kinds that may be persons (an unclassified entity, a ref with no kind) never do. A form one person calls another, or one through a relation, is never a record name part, and a kinship word before a name is never a record title. The critic's `kinship_term_alias` flags a kinship form as the label of a person the document names, and a description as an alias; a kinship form among the aliases is no finding. The "never an alias" rule below, and the #434 and #459 amendments' rulings that a relational appellation or a kinship word is never an alias, are superseded so far; appellations of possibly-living people ride the possibly-living exclusion, as before (ADR-0020).  
Amended by #523 (2026-10-24): reconcile's blocking against graph items (ADR-0007) keys the same way. The roster reads each item's `referred_to_as` values at any rank but deprecated, and a person's label or alias equal to one of the item's own values (compared as alias hygiene compares names) keys nothing, so two graph items each labelled or aliased "Oma" are never a candidate pair by it, while a name form they share, or a longer name holding the appellation as a word, still is. A place, an organisation or a family has no appellations, so every name it has keys. The item's typed name statements (birth name, nickname, official name) key whatever their value: a nickname that is also an appellation keys as a nickname.  
Amended by #638 (2026-10-24): the document side keys the same way. A person's (or a kind that may be a person's) label or alias equal to a form its batch gives as an appellation of it (`names.appellations_of`, compared as alias hygiene compares names) keys nothing in reconcile's blocking, so a document's "Hildegard Löbig", aliased "Oma", never meets another grandmother by "Oma"; a record's nicknames still key, and a place, an organisation or a family keys on every name. Blocking without the batch keys every alias. The adjudication prompts (reconcile's and reconcile-graph's) show a graph item's appellation as "called: Oma", never among its aliases; a document entity's appellation is left out of its aliases, and its facts show it as its `referred_to_as` claim.  
Amended by #681 (2026-10-24): the serial sequencer's overlap signal (ADR-0019) keys the same way, given each document's batch, so two documents that share only an appellation no longer count as sharing a name.

## Context

Ingestion routes relational references to a person — "our great grandmother", "meine Oma", "Nana Maisie", "my husband", "the deceased" — into that person's **alias bag**. The entity-manifest extraction prompt instructs it directly; its worked example unifies `"Cäcilie Löbig"`, `"Cäcilie"`, and `"our great grandmother"` into one entry whose aliases list "every other form, verbatim".

That is the wrong home, on three axes at once. ADR-0011 defines aliases as "a flat, untyped, append-only bag of name strings" that carry "no reference, qualifier, or rank". A relational reference is:

- **not a name** — "great grandmother" is a _description_, and the alias bag asserts it as an appellation _of_ Cäcilie without saying so;
- **perspectival** — "our" / "meine" is relative to a speaker; the term is meaningless without the viewpoint the alias bag discards;
- **unreferenced** — no record of _who_ used it or _which source_.

The cost is visible as **critic traffic**: `critique.py` is built to flag "a descriptive phrase ('my husband', 'the deceased', 'our friends') recorded as if it were a name". So every such alias draws an adversarial finding to the review gate — the critic correctly objecting to a modelling error the extractor was _told_ to make. Fixing the model removes the traffic at its source rather than triaging it downstream.

There is also an opportunity. A perspectival reference carries the very context — the ego, the "our" — that lets a _later_ bare reference ("Grandpa took me…", "Tante said…") be resolved to a specific person. Captured as an absolute alias, that context is thrown away; captured perspectivally, it becomes a resolution key.

## Decision

A relational reference to a person is **never an alias**. It is one of two things, and the extractor routes it accordingly (amended by #499: a form that names or addresses the person is an alias as well, and (B) is given for every such form that is no plain name form, a literal kinship term included; see the amendment above):

### (A) A derivable kinship description → `relative` + `kinship_type`

"our great grandmother", "meine Oma", "mein Onkel", "Dad's cousin Leopold" assert a **kinship relation** to an ego. These reuse the existing ADR-0014 machinery with no new vocabulary: `Cäcilie —relative→ <ego> ; kinship_type = "great grandmother"`, referenced to the artifact. The ego (the "our") is the _object_ of the statement, so the viewpoint is captured structurally, and the frontend re-renders the term from the derivation calculus (gendered, degreed) rather than storing a string on the person.

### (B) A non-derivable appellation → a new perspectival property

"Nana Maisie" where Maisie is no relation (a courtesy title), "Bubele" (a pet name), a family-specific term of address — these are genuine appellations but not literal kinship. They get a **new, first-class, referenced statement on the referent**:

- **`referred_to_as`** (monolingualtext) — the appellation, verbatim, in the source's wording. Mirrors the spirit of Wikidata **P1810 "subject named as"** (the term a specific context uses for a subject); it is deliberately _not_ `nickname` (P1449), which is an _absolute_ informal personal name ("Peppi", "Cilli") and would re-import the perspective problem.
- **`perspective`** (wikibase-item, qualifier, **optional**) — the person or `family` (ADR-0014) from whose viewpoint the appellation applies (the ego). Omitted when the only viewpoint is the source's own, which the statement's artifact reference already carries.

So "X is known to Y as Z" is `X —referred_to_as→ "Z" ; perspective = Y`, referenced to the artifact that records it.

#### The `family` perspective-holder is opportunistic, never fabricated

A `family` item enters the graph exactly like any other entity — the entity-manifest pass mints one **only when a source names a collective** ("the Löbigs", "the Lobig family", "the Lobig couple"), after which it flows through reconcile → review → write and its members attach via kinship or `member of`. A bare possessive — "_our_ great grandmother", "_meine_ Oma" — is a pronoun, not a named collective, and must **never** mint a family (that would be inventing an entity from a pronoun, exactly the guessing rules 5–6 forbid). Consequently `perspective` points at a family only when ADR-0014's extractor has _independently_ named that family; the appellation layer is never a back-door reason to create one.

Where a family item _does_ legitimately exist, it is the **preferred** perspective value over any single ego: a later "our Oma" from that family then resolves transitively to any member's grandmother, not just one person's. Where no named collective exists, `perspective` falls back to a specific person (the author/informant, when identifiable) or is left empty — leaning on the source reference for viewpoint. (Family membership rides `member of`, whose scope is widened here to include a `family`, not only an organization, so "who is _in_ the Lobig family" is recordable.)

### The routing rule (conservative by default)

Promote to (A) only when the source supports a **literal** kinship reading; otherwise use (B). `relative` + `kinship_type` _asserts a kinship edge_, so sending a courtesy "Tante" there would fabricate a relation. `referred_to_as` asserts only that the term was used — it never narrows or invents (rules 5 & 6). When in doubt, (B) is safe. Aliases keep only genuine **name** forms (spellings, maiden/married names); the label stays the fullest real name. (Amended by #499: aliases keep every form that names or addresses the person; the label stays the fullest real name.)

### Reciprocal / dyadic naming (each party names the other)

The model must hold the case where _both_ members of a pair have a relational name for the other worth recording — a grandmother known to her grandchild as "Oma" while the grandchild is known to her as "Bubele"; two sisters who each call the other "Schwesterchen". This falls out of the design because **every appellation is a statement whose SUBJECT is the referent and whose ego is the other party**:

- The two directions are **two independent statements on two different subjects** — `grandmother —relative→ grandchild ; kinship_type = "Oma"` and `grandchild —referred_to_as→ "Bubele" ; perspective = grandmother`. There is no dyad object to build and nothing to keep in sync; the subject and the ego (the `relative` object on one side, the `perspective` qualifier on the other) distinguish them.
- A **symmetric term** ("Schwesterchen" both ways) is still two statements, disambiguated by subject — never one conflated value. Neither shadows the other.
- A reciprocal pair may be **mixed type**: "Oma" is derivable kinship → (A) on one side, "Bubele" is a pet name → (B) on the other. The two mechanisms compose without special-casing.
- For the (A)/kinship direction, the **inverse is derived, not stored**: if `grandmother —relative→ grandchild` is recorded, the calculus yields the grandchild→grandmother direction, so a reciprocal kinship pair need be asserted only once (recording both is allowed but redundant — a match, per ADR-0014, corroborates).
- One source frequently records **both directions at once** ("your loving Oma" over a signature, a letter that names writer and recipient); both statements cite the same artifact, kept apart by subject and ego (the `relative` object or the `perspective` qualifier).

### Decoding otherwise-context-free references

The two layers give two resolution paths, and the **ego is the key** each one needs:

- **Compositional terms** ("Grandpa said…") decode by **kinship derivation given the ego**: query the person who is grandfather-of-ego over the primitive graph. This resolves references the source never spelled out, as long as the viewpoint is anchored — the anchor the alias bag discarded and this model preserves.
- **Idiosyncratic terms** decode via the `referred_to_as` + `perspective` statements as an explicit table: the same "Tante" that denotes different women across families' letters resolves correctly because the perspective scopes it.

### Neutral coexistence and privacy

Both properties are multi-valued and source-attributed: many people may call one person many things, each appellation its own referenced statement at normal rank, none canonical (ADR-0002). Appellations of possibly-living people are personal data and ride the existing possibly-living exclusion (ADR-0020, MODELING.md) — dropped with every other mention of an excluded person; no special handling, but noted.

## Rulings

1. **`perspective` value type.** Item-valued, optional, taking a **person or a `family`** — the family value used only when the collective is independently named (see above), never fabricated from a pronoun.
2. **(A) vs (B) boundary.** Route **literal** kinship terms to `relative` + `kinship_type` (ego as object); reserve `referred_to_as` for non-kinship appellations. Courtesy and idiosyncratic terms default to (B), which asserts only that the term was used and so never invents a relation.
3. **Ego-identification policy.** Fill `perspective` when an entity (person or named family) is available; otherwise **omit it** and lean on the source reference. Never guess an ego, and never mint an entity to have one.
4. **No retro-fit.** The model applies to future ingests only; existing role/relational aliases already in the graph are **not** migrated. The combined noise from recently-fixed extraction/review pathologies (this one included) makes a **single clean re-ingest from scratch** more efficient than a migration pass, so none is built.

## Consequences

- The critic stops flagging relational references (they are no longer mis-typed as names), removing a standing source of review-gate traffic.
- Aliases regain their meaning as a name-match index; reconciliation blocking no longer carries perspective-free descriptive strings that never match anything.
- The archive gains a queryable appellation layer and, with the ego anchored, the ability to resolve bare relational references — including ones no single source disambiguates.
- One new property (`referred_to_as`) and one qualifier (`perspective`), plus a widened `member of` (now person → organization _or_ family); the derivable-kinship path reuses ADR-0014 unchanged.
- No migration code: the next run is a clean re-ingest, so the model takes effect corpus-wide without a retro-fit pass.
