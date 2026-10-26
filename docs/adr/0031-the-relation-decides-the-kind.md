# ADR-0031: The relation decides the kind — places, and organisations that act

> **Temporary deprecated material — TODO [#733](https://github.com/WillHirsch/yizkr/issues/733):** this document still mixes current design details with development history and superseded prescriptions. Its historical material is not authoritative for new policy; use [REQUIREMENTS](../REQUIREMENTS.md), [CONTRACT](../CONTRACT.md) and the implemented code. Remove this note only after rewriting the document as current guidance with necessary rationale, or consolidating it elsewhere and deleting this file. The linked task tracks removal of all remaining historical and obsolete passages.

Status: **accepted** (owner decision 2026-09-29, #202). Like ADR-0024 and ADR-0025, it records a decision the owner has made, so the owner's merge of the PR that adds it is its acceptance. It settles #47 and amends ADR-0012 (Forks C and F) and ADR-0009 in place. #89 implements it, within #385's extraction revision. Amended by #407 (2026-09-29): the extraction prompts carry the rule (#89). The entity-manifest prompt types an entity by the relations its source states, in place of the rule that a hospital, school or congregation is an organisation wherever it is named; the chunk prompt points each relation at an entity of a kind it accepts, and `issued_by` and `carried_out_by` are offered. The critic's relation/kind category names both agency relations too.

## Context

ADR-0012 types an entity by its primary role in a document (Fork C): an entity the source treats as an agent is an `organization`, one it treats as a location (born at, died at) is a `place`, and one used both ways takes its dominant use. The extraction prompt and the critic pull against each other on this.

- The entity-manifest prompt (`src/yizkr/pipeline/chunking.py:336-339`) makes a hospital, clinic, school or congregation an organisation even when a source names it only as where an event happened, such as a place of birth or death: "an organization HAS a location, it is not one".
- The critic (`src/yizkr/pipeline/critique.py:63-65`) flags an entity whose kind is wrong. In one run it flagged camps, ghettos and memorials typed as organisations but used only as a place of death, a typing that follows the prompt's rule (#47).

So the critic keeps flagging what the prompt asks for, and neither side can be checked except by a model reading wording.

## Decision

1. **The relation a source states decides the kind of what it points to.**
   - A relation of place (place of birth, death or burial, location, destination) takes a place.
   - A relation of agency (issued by, employer, educated at, carried out by, member of) takes an organisation (`member_of` also takes a family, ADR-0014).

   The two are incompatible: an act takes place _at_ a place, or is carried out _by_ an institution, never the reverse. The extractor picks the relation the source best fits, and the kind follows from it: in the manifest pass (ADR-0007), an entity's kind is the one its relations in that source call for. Of the relations of agency, the manifest has `employer`, `educated_at` and `member_of`; until it has one for "issued by" or "carried out by", such a relation is a gap (ADR-0013). #385's extraction revision adds both.

2. **A site named as where something happened is a place.** A camp, ghetto or memorial named as where someone died is a place: an item of a place kind, inside its town or region. An organisation, with that place as its `location`, is extracted only where the source shows the organisation acting: the camp's office issues a document, or a court divorces a couple.

   In the fictional universe (docs/EXAMPLES.md), the compensation file names Lager Klurnau as the destination of Ida's deportation ("in das Lager Klurnau verschleppt"), so the camp is a place, inside Klurnau, which no source locates. The registration card says Leopold was "held in Peiltal court prison": a place, inside Peiltal. The Landgericht Rekau's judgment divorces Leopold and Agnes, so the court is an organisation, located at Rekau.

3. **One rule for every institution tied to a site**: schools, hospitals, congregations, courts and registry offices alike. It replaces the extraction prompt's rule that a hospital, school or congregation is an organisation even when a source names it only as where something happened. Rudolf's school report makes him a pupil of the Städtische Realschule Flörburg (`educated_at`, a relation of agency), so the school is an organisation, located at Flörburg.

4. **One source, both uses.** Where one source uses the same named institution both ways, both are extracted: a place for the site and an organisation for the body, linked by the organisation's `location` → the place. Clustering never merges a place with an organisation.

5. **The manifest lists the kinds each item-valued property accepts**, so code catches a mismatch structurally, never by the critic reading wording. This ADR states the rule. The mechanism (the manifest's field, where the check runs, and what it does with a mismatch) is specified with #385's extraction revision, which ADR-0035 may carry.

## Consequences

- #89 implements the rule in the extraction and critic prompts, within #385's extraction revision, so that the two agree. Its fictional fixture first adds to the universe what it lacks (docs/EXAMPLES.md, "Extending"): no source there yet names a site as where someone died, or uses one institution both ways.
- #386's critic categories include a relation/kind mismatch, so the run report counts how often the pattern still occurs.
- Maps show sites, while organisations appear as actors. A site named as where something happened is a place, so it joins the place hierarchy (ADR-0009) and, with coordinates, the places map. An organisation appears in the facts it takes part in, and on the map through its `location`.
- A place in one source and a site inside it in another differ in resolution, not in fact, as in the universe's refinements: Klurnau and Lager Klurnau for Ida's deportation, Peiltal and its court prison for Leopold's imprisonment.
- The neutral-coexistence principle (ADR-0002) is unaffected. Two sources may type the same name differently through different relations: one names a camp as where someone died, a place; another shows the camp's office issuing a document, an organisation. Both stand, each referenced to its source, and neither corrects the other.
- ADR-0012 and ADR-0009 carry dated amendment lines. docs/MODELING.md ("Organizations") changes when #89's code lands.

## Alternatives considered

#47 set out three options.

- **A place subclass only.** Every camp, ghetto or memorial is a place, whatever a source says of it. Rejected: where a source shows the institution acting, an agent would be typed as a place, the error ADR-0012 was written to end, and the rule would not reach schools, courts or registry offices.
- **Always an organisation**, today's rule (`chunking.py:336-339`). Rejected: a place of death or a deportation's destination would point to something that is not a place, which the place-hierarchy pass (ADR-0009), listing places only, cannot put inside its town, and the archive would record an agent the source never shows. The rule was added to stop the extractor going back and forth between the two kinds; the relation now settles that question instead.
- **Context-dependent.** #47 asked how the extractor tells a camp's administration from its site. This decision is a principled form of that option: the context is the relation the source states. The extractor still judges which relation fits, but that is its only judgement: the kind follows, and code checks that the two agree. Under Fork C the kind was a judgement of the entity's primary role, which only another reading of the wording could check. No entity of two kinds is needed (ADR-0013 keeps those out of scope): a source that uses a name both ways gives two items, linked by `location`.
