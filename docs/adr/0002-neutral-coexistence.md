# ADR-0002: Neutral coexistence of contradictory assertions
# (source-first / factoid model)

Status: accepted

## Context

The archive's purpose is to let a reader see what the evidence says –
e.g. "moved to France in 1929 per source A; in 1930–1935 per source B;
married there in 1933 per the marriage record" – even when no curator
or algorithm has expressed a view. Any design that depends on
resolving disagreements is over-dependent on those resolutions.

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
