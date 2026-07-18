"""Runtime "possibly living" determination (privacy-first, R7 / ADR-0020).

There is **no stored "living" flag**. Whether a person is possibly living
is computed at the moment a privacy-sensitive action runs — the public
export (`yizkr backup`) and the private static-site badge — from the
*absence of death evidence* in the graph. The call is conservative by
design: unknown means possibly living, hence excluded from anything
public.

A person is *not* possibly living when the graph holds positive evidence
of death, or a birth beyond the cutoff. "Death evidence" is any
source-asserted signal that presupposes death:

* a `date of death`, `place of death`, or `cause of death`;
* a `place of burial` (burial presupposes death — the requirement's
  explicit example);
* participation in a `death` event (ADR-0014 models death as an event
  like any other).

None of these requires *both* a date and a place — a cause of death, a
burial place, or an undated death event each records "deceased" with no
particulars. So a person can be identified as deceased without a
time/place of death, and no curator "living/deceased" decision is ever
demanded: privacy falls out of what sources already assert.
"""

from __future__ import annotations

# Birth-recency cutoff: a birth at least this many years ago is taken as
# proof the person is not living, absent any death evidence. Shared by
# the public export and the private static-site badge so both agree.
DEFAULT_CUTOFF_YEARS = 123

# Statement properties (manifest keys) that presuppose the subject's
# death. `age_at_death` is intentionally absent: it is a qualifier on
# `date of death`, never a standalone statement, so it adds nothing.
# Death-event participation is checked separately (it is a relation
# through an event item, not a property on the person).
DEATH_EVIDENCE_PROPERTIES = (
    "date_of_death",
    "place_of_death",
    "cause_of_death",
    "burial_place",
)


def year_of(literal: str) -> int | None:
    """Leading 4-digit year of a Wikibase time literal, else None.

    Tolerates an optional leading ``+`` (CE); BCE or malformed values
    return None (irrelevant to family-record cutoffs).
    """
    text = literal[1:] if literal.startswith("+") else literal
    return int(text[:4]) if text[:4].isdigit() else None


def is_possibly_living(
    birth_year: int | None,
    has_death_evidence: bool,
    cutoff_year: int,
) -> tuple[bool, str]:
    """Conservative call: unknown means possibly living (excluded).

    Returns (possibly_living, reason). Only positive evidence of death,
    or a birth at/beyond the cutoff, marks a person not-living. There is
    no override in either direction — the determination is exactly what
    the graph's own referenced statements assert (ADR-0020).
    """
    if has_death_evidence:
        return False, "death recorded"
    if birth_year is not None and birth_year <= cutoff_year:
        return False, f"born {birth_year}, beyond the {cutoff_year} cutoff"
    if birth_year is None:
        return True, "no death evidence and no birth date: unknown means excluded"
    return True, f"born {birth_year} with no death evidence"
