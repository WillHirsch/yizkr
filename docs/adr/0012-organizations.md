# ADR-0012: Organizations as agents

> **Temporary deprecated material — TODO [#733](https://github.com/WillHirsch/yizkr/issues/733):** this document still mixes current design details with development history and superseded prescriptions. Its historical material is not authoritative for new policy; use [REQUIREMENTS](../REQUIREMENTS.md), [CONTRACT](../CONTRACT.md) and the implemented code. Remove this note only after rewriting the document as current guidance with necessary rationale, or consolidating it elsewhere and deleting this file. The linked task tracks removal of all remaining historical and obsolete passages.

Status: accepted  
Amended by [ADR-0031](0031-the-relation-decides-the-kind.md) (#202, 2026-09-29): the relation a source states decides an entity's kind (Fork C), and a source that uses one institution both ways gives two items, a place and an organisation located at it, never one entity of its dominant use (Forks C and F).

## Context

The model had `human`, `place`, `event`, `artifact`, `occupation` — but no way to represent an **organization acting as an agent**. In the third trial this surfaced concretely: the Hutmacherei zum Blauen Hut that _employed_ Sebastian Löbig was classified as a **place**, because `place` was the nearest available kind. Companies, banks, committees, government departments, schools, and congregations recur throughout this corpus (the Wiedergutmachungsamt Peiltal that handled Leopold's claim, the Landgericht Rekau that divorced him, the Städtische Realschule Flörburg that taught his brother Rudolf), and none could be typed correctly.

An organization is not a place (it has a location; it is not one) and not a person (it acts, but is not human). It is a distinct kind of agent.

## Decision

Add an `organization` class and the one relation that makes it useful. Fork decisions (from the reviewed spec) are recorded inline.

- **`organization`** — a new top-level item class (mirror Wikidata `Q43229`), a sibling of `place` and `human`, subclass of nothing. _(Fork A: flat class, no subtype taxonomy yet — a `company` / `government body` / `institution` subclass is one manifest entry away when a query demands it. Deferred-on-trigger register: #171.)_

- **`employer`** (Wikidata `P108`, wikibase-item) — person → organization. This is the load-bearing relation for the corpus: Sebastian Löbig at the family firm, and any staff, post or appointment a document records. _(Fork B: class + `employer`; `member of`/`owner`/`founder` deferred. See ADR-0014 §4 and #171.)_

- **`occupation` stays orthogonal to `employer`** _(Fork D)_. A hatter's record asserts two independent, separately-referenced things: what they do (`occupation → "Hatter"`, an occupation_class item) and who they work for (`employer → Hutmacherei zum Blauen Hut`, an organization item). Neither subsumes the other; either can be queried alone.

- **Single kind, by primary role** _(Fork C)_. `EntityRecord.kind` stays a single manifest key. Extraction picks `organization` when the source treats the entity as an **agent** (employs, is deregistered, pays tax, helps, hires) and `place` when it treats it as a **location** (born at, died at, lives in, located at). So the Hutmacherei zum Blauen Hut (employs) is an organization; "Friedhof Flörburg" (a burial place) stays a place. Dual-nature entities pick their dominant use in the document. True dual `instance of` (org _and_ place) would need a multi-kind pipeline and is out of scope (see #47).

- **Location, not containment** _(Fork F)_. An organization may carry `location` (P276) → a place, keeping it mappable without dragging it into the place-hierarchy pass (ADR-0009). Its physical premises are a separate place item only when the source clearly distinguishes them.

- **Verbatim labels** _(Fork G)_. Organization labels keep the source's wording (`Hut=macherei zum Blauen Hut`, OCR artifact and all); normalisation is a curator edit in the review CSV, never an extraction guess — the same rule as occupation/cause-of-death labels.

- **No backwards migration** _(Fork E)_. The instance is reinitialised from the GEDCOM + source documents against the finished model rather than reclassified in place, so there is no add-and-deprecate path and no legacy contamination to carry. (Only two existing place items were ever org-adjacent, both hospitals that correctly remain places.)

## Consequences

- Companies, agencies, committees, and institutions become first-class agents; "who employed whom", "which businesses the Löbigs ran", and "which agency deported Ida Löbig" become answerable.
- Address-book and biographical documents can record employment (`occupation` + `employer`) instead of collapsing it into a mistyped place.
- Registrar, writer, and reconciler need no structural change: `organization` is another item kind and `employer` is a wikibase-item property, both already handled. The extractor's kind list and prompt guidance are the only pipeline edits.
- Pairs with ADR-0013: the open-schema harness ensures that the _next_ missing agent-type (a ship, a regiment, a congregation the flat class can't distinguish) is surfaced as a reviewable gap rather than mistyped, instead of waiting for a human to spot it in a trial.
