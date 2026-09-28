# ADR-0002: Neutral coexistence of contradictory assertions
# (source-first / factoid model)

Status: accepted

## Context

The archive's purpose is to let a reader see what the evidence says –
e.g. "Gustav Löbig was born on 7 January 1919 per his birth register
extract; on 1 July 1919 per his naturalisation certificate; in 1919
per a family tree" – even when no curator or algorithm has expressed
a view. Any design that depends on resolving disagreements is
over-dependent on those resolutions.

## Decision

Adopt the source-first ("factoid") model: the database records what
sources assert, never bare truth. Contradictory statements coexist at
normal rank as the default, healthy state. Resolution (rank changes,
curator conclusions) is an optional overlay, and a resolution – when
one happens – is itself just another assertion whose source is a
curator or an algorithm.

## Consequences

- Acceptance test for every feature: a property with zero canonical
  values and N attributed values must work everywhere.
- The hard remaining problem moves to presentation: frontends must be
  plural-by-default (both dates on the timeline, labeled by source).
- CRMinf-style belief/argumentation machinery is explicitly not
  needed: its distinctive value is the resolution apparatus we treat
  as optional.
- Nothing is lost by deferring resolution: preferring is a special
  case of asserting and can be added at any time without redesign.
