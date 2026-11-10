"""Authored two-union history and an explicitly incorrect survivor reading.

The alternate history is separate from the example family's canonical
assertions. No model produced these proposed claims.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from tests.fixtures import example_universe as u
from tests.fixtures.semantic_acceptance import SourceCase

from yizkr.claims import (
    Claim,
    ClaimBatch,
    EntityRecord,
    ExtractionRunMetadata,
    ItemValue,
    LocalRef,
    QidRef,
    Qualifier,
    TimeValue,
    VocabRef,
)

HISTORY = u.WIDOWHOOD_SCENARIOS[0].key
INDEPENDENT = u.WIDOWHOOD_SCENARIOS[1].key
ENTITIES = (
    ("eugen", "Eugen Löbig", "human"),
    ("caecilie", "Cäcilie Löbig", "human"),
    ("ursula", "Ursula Löbig", "human"),
    ("first_union", "Hypothetical marriage of Eugen and Cäcilie", "marriage"),
    ("second_union", "Hypothetical marriage of Eugen and Ursula", "marriage"),
)


def source_case(key: str) -> SourceCase:
    source = next(
        source for source in u.WIDOWHOOD_SCENARIOS if source.key == key
    )
    return SourceCase(source.key, source.title, source.text, source.lang, ())


def history_batch(
    refs: Mapping[str, LocalRef | QidRef] | None = None,
    *,
    include_misreading: bool = False,
    independent: bool = False,
) -> ClaimBatch:
    """Two unions from one narrative, or its independent later end.

    With ``include_misreading``, append the false first-union end naming
    Eugen instead of Cäcilie. Claims use local IDs by default; supplying
    QID references exercises the same evidence against existing entities.
    """
    refs = refs or {key: LocalRef(local_id=key) for key, _, _ in ENTITIES}
    key = INDEPENDENT if independent else HISTORY
    source = source_case(key)

    def claim(subject, property, value, *qualifiers):
        return Claim(
            subject=refs[subject],
            property=property,
            value=ItemValue(value=value),
            qualifiers=list(qualifiers),
            source=source.source_ref(source.text),
        )

    claims = []
    if not independent:
        for union, couple in (
            ("first_union", ("eugen", "caecilie")),
            ("second_union", ("eugen", "ursula")),
        ):
            for partner in couple:
                claims.append(
                    claim(
                        union,
                        "participant",
                        refs[partner],
                        Qualifier(
                            property="object_has_role",
                            value=ItemValue(value=VocabRef(key="spouse")),
                        ),
                    )
                )
        claims.append(
            claim("first_union", "ended_by_death_of", refs["caecilie"])
        )
        for person, year in (("eugen", 1902), ("ursula", 1909)):
            claims.append(
                claim(
                    person,
                    "civil_status",
                    VocabRef(key="widowed"),
                    Qualifier(
                        property="point_in_time", value=TimeValue(year=year)
                    ),
                )
            )
    claims.append(claim("second_union", "ended_by_death_of", refs["eugen"]))
    if include_misreading:
        claims.append(claim("first_union", "ended_by_death_of", refs["eugen"]))

    return ClaimBatch(
        extraction_run=ExtractionRunMetadata(
            model_identifier=f"authored-fixture:{key}",
            pipeline_version="widowhood-acceptance-v1",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        entities=[
            EntityRecord(
                id=entity_key,
                label=label,
                kind=kind,
                qid=refs[entity_key].qid
                if isinstance(refs[entity_key], QidRef)
                else None,
            )
            for entity_key, label, kind in ENTITIES
        ],
        claims=claims,
    )
