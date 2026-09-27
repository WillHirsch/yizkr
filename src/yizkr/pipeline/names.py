"""Deterministic name-alias hygiene (ADR-0022 §4).

A thin VALIDATOR, not a parser. The semantic work — composition,
annotation-stripping, and name TYPING — is the LLM's (through the extraction
prompt's articulable standards) and the GEDCOM adapter's structured tags. This
module only enforces mechanical, language-agnostic invariants on the
UNREFERENCED alias bag (ADR-0011), as the downstream safety net and parity
mechanism shared by both ingest paths:

* drop an alias a reader can RE-DERIVE from the label — a bare given name, a
  surname the label already carries, or an initialism (its significant
  >=3-char tokens are a strict subset of the label's) — so the bag holds only
  forms that add a search token;
* never SPLIT a name, so hyphenated and space-separated compound / nobiliary
  particle surnames (``A-B``, ``de la Cruz``, ``van der Berg``,
  ``ben Yosef``) stay whole — a naive whitespace atomizer would shatter them
  into particle non-name tokens (rule 6);
* assert NO name kind (given vs family is undecidable and never stored,
  ADR-0022) and compose NOTHING (composition is the LLM's job, and the
  frontend's to render).

Recall-safe: a dropped form's tokens are a strict subset of tokens already on
the label, and blocking (``reconcile._name_tokens``) tokenises the same way,
so candidate generation loses nothing. Idempotent: re-running on a cleaned bag
is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterable

# mirror reconcile._name_tokens so "derivable" means "adds no blocking token"
_STRIP = ".,;:()\"'"


def _tokens(name: str) -> frozenset[str]:
    """Significant match tokens of a name — the shape blocking uses:
    punctuation-stripped, lower-cased, >=3 chars (so initials and short
    nobiliary particles fall out)."""
    return frozenset(
        token
        for raw in name.split()
        if len(token := raw.strip(_STRIP).lower()) >= 3
    )


def _is_derivable(label_tokens: frozenset[str], alias: str) -> bool:
    """True when `alias` adds no token the label lacks — a bare given name, an
    initialism, or a surname the label already carries (a strict subset). An
    alternative surname the label does not carry keeps its own token and is
    NOT derivable."""
    alias_tokens = _tokens(alias)
    return bool(alias_tokens) and alias_tokens < label_tokens


def normalize_aliases(label: str, aliases: Iterable[str]) -> list[str]:
    """Hygiene one entity's alias bag (ADR-0022 §4): drop blanks, the label
    itself, case-insensitive duplicates, and re-derivable short-forms, keeping
    the order and verbatim spelling of what survives. Never splits, so
    compound/particle surnames stay whole; asserts no kind, composes nothing.
    """
    label_tokens = _tokens(label)
    label_key = label.strip().lower()
    seen: set[str] = set()
    kept: list[str] = []
    for raw in aliases:
        alias = raw.strip()
        if not alias:
            continue
        key = alias.lower()
        if key == label_key or key in seen:
            continue
        if _is_derivable(label_tokens, alias):
            continue
        seen.add(key)
        kept.append(alias)
    return kept


def normalize_entity_dict(entity: dict) -> dict:
    """Return `entity` with a HUMAN entity's aliases hygiened; every other kind
    (place, organization, ...) passes through UNCHANGED — the derivability
    rules are about personal names, and would wrongly drop a genuine short
    place name ("Flörburg" beside "Flörburg, Hesse, Germany")."""
    if entity.get("kind") != "human":
        return entity
    return {
        **entity,
        "aliases": normalize_aliases(
            entity.get("label", "") or "", entity.get("aliases") or []
        ),
    }
