# ADR-0012: Organizations as agents

Status: accepted

## Context

The model had `human`, `place`, `event`, `artifact`, `occupation` — but
no way to represent an **organization acting as an agent**. In the third
trial (real German business/emigration documents) this surfaced
concretely: the Hermannmühlen-Aktiengesellschaft that *employed* Hermann
Rothholz and 29 mill workers was classified as a **place**, because
`place` was the nearest available kind. Companies, banks, committees,
government departments, schools, and congregations recur throughout this
corpus (the Jewish Support Committee that relocated Charlotte, the
Persian Department of Industry that hired Erwin, the European bookshop he
ran), and none could be typed correctly.

An organization is not a place (it has a location; it is not one) and not
a person (it acts, but is not human). It is a distinct kind of agent.

## Decision

Add an `organization` class and the one relation that makes it useful.
Fork decisions (from the reviewed spec) are recorded inline.

- **`organization`** — a new top-level item class (mirror Wikidata
  `Q43229`), a sibling of `place` and `human`, subclass of nothing.
  *(Fork A: flat class, no subtype taxonomy yet — a `company` /
  `government body` / `institution` subclass is one manifest entry away
  when a query demands it.)*

- **`employer`** (Wikidata `P108`, wikibase-item) — person → organization.
  This is the load-bearing relation for the corpus: the mill staff,
  Hermann's post, Erwin's government appointment and bookshop.
  *(Fork B: class + `employer`; `member of`/`owner`/`founder` deferred.)*

- **`occupation` stays orthogonal to `employer`** *(Fork D)*. A mill
  worker asserts two independent, separately-referenced things: what they
  do (`occupation → "miller"`, an occupation_class item) and who they
  work for (`employer → Hermannmühlen AG`, an organization item). Neither
  subsumes the other; either can be queried alone.

- **Single kind, by primary role** *(Fork C)*. `EntityRecord.kind` stays
  a single manifest key. Extraction picks `organization` when the source
  treats the entity as an **agent** (employs, is deregistered, pays tax,
  helps, hires) and `place` when it treats it as a **location** (born at,
  died at, lives in, located at). So Hermannmühlen AG (deregistered,
  employs) is an organization; "Jewish Hospital, Auguststrasse" (a
  birthplace) stays a place. Dual-nature entities pick their dominant use
  in the document. True dual `instance of` (org *and* place) would need a
  multi-kind pipeline and is out of scope.

- **Location, not containment** *(Fork F)*. An organization may carry
  `location` (P276) → a place, keeping it mappable without dragging it
  into the place-hierarchy pass (ADR-0009). Its physical premises are a
  separate place item only when the source clearly distinguishes them.

- **Verbatim labels** *(Fork G)*. Organization labels keep the source's
  wording (`Hermannmühlen=Aktiengesellschaft`, OCR artifact and all);
  normalisation is a curator edit in the review CSV, never an extraction
  guess — the same rule as occupation/cause-of-death labels.

- **No backwards migration** *(Fork E)*. The instance is reinitialised
  from the GEDCOM + source documents against the finished model rather
  than reclassified in place, so there is no add-and-deprecate path and
  no legacy contamination to carry. (Only two existing place items were
  ever org-adjacent, both hospitals that correctly remain places.)

## Consequences

- Companies, agencies, committees, and institutions become first-class
  agents; "who employed whom", "which businesses the family ran", and
  "which agency deported them" become answerable.
- The staff-list and biographical documents can record employment
  (`occupation` + `employer`) instead of collapsing it into a mistyped
  place.
- Registrar, writer, and reconciler need no structural change:
  `organization` is another item kind and `employer` is a wikibase-item
  property, both already handled. The extractor's kind list and prompt
  guidance are the only pipeline edits.
- Pairs with ADR-0013: the open-schema harness ensures that the *next*
  missing agent-type (a ship, a regiment, a congregation the flat class
  can't distinguish) is surfaced as a reviewable gap rather than
  mistyped, instead of waiting for a human to spot it in a trial.
