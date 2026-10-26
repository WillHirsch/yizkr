"""Small authored scenarios from the fictional example universe.

Source text and expectations are domain data, independent of model prompts
and production schemas. The adapters below make current pipeline inputs;
they are not extraction results or an oracle for model quality. Text sources
use a one-page UTF-8 fixture representation. The photograph has identity-only
placeholder bytes, not pixels suitable for visual model evaluation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from tests.fixtures import example_universe as u

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    ExtractionRunMetadata,
    GivenName,
    ItemValue,
    LocalRef,
    MonolingualText,
    PersonStructure,
    QidRef,
    Qualifier,
    SourceReference,
    Surname,
    TimeValue,
    VocabRef,
)
from yizkr.pipeline.transcribe import Transcription

# Explicit inputs to simulated instance tests, never expected generated IDs.
GUSTAV_QID = "Q7001"
FACE_SOURCE = "album-photo-1906"
FACE_PEOPLE = ("sebastian-loebig", "hildegard-loebig")
FACE_PERSON_QIDS = ("Q7002", "Q7003")
FACE_ARTIFACT_QID = "Q7010"
ANGERRING_QID = "Q7004"


@dataclass(frozen=True)
class SourceCase:
    key: str
    title: str
    text: str
    language: str | None
    expected: tuple[u.Assertion, ...]

    @property
    def source_bytes(self) -> bytes:
        if self.text:
            return self.text.encode("utf-8")
        return f"fictional photograph placeholder: {self.key}".encode()

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.source_bytes).hexdigest()

    def source_ref(
        self, excerpt: str = "", *, page: str = "1"
    ) -> SourceReference:
        return SourceReference(
            artifact=ArtifactRef(sha1=self.sha1),
            page_number=page,
            transcription_excerpt=(
                MonolingualText(text=excerpt, language=self.language or "en")
                if excerpt
                else None
            ),
        )

    def transcription(self) -> Transcription:
        """Source-only input for optional text evaluation; no expectations."""
        if not self.text:
            raise ValueError("this photograph fixture has no source text")
        return Transcription(
            artifact=ArtifactRef(sha1=self.sha1),
            text=f"[Page 1]\n{self.text}",
            language=self.language or "en",
            engine="authored-fictional-text",
        )


def _source(key: str, subject: str, property: str) -> SourceCase:
    source = next(source for source in u.SOURCES if source.key == key)
    return SourceCase(
        key,
        source.title,
        source.text,
        source.lang,
        tuple(
            assertion
            for assertion in u.ASSERTIONS
            if (assertion.source, assertion.subject, assertion.property)
            == (key, subject, property)
        ),
    )


SOURCE_CASES = {
    key: _source(key, subject, property)
    for key, subject, property in (
        ("birth-extract-1938", "gustav-loebig", "date_of_birth"),
        ("naturalisation-1952", "gustav-loebig", "date_of_birth"),
        ("family-tree-1984", "ursula-loebig", "date_of_birth"),
        ("directory-1912", "ursula-loebig-angerring", "residence"),
        (FACE_SOURCE, FACE_SOURCE, "depicts"),
    )
}
# The universe already states "Wwe." in this source's text; retain that
# explicit status alongside its residence assertion in this narrow case.
SOURCE_CASES["directory-1912"] = replace(
    SOURCE_CASES["directory-1912"],
    expected=SOURCE_CASES["directory-1912"].expected
    + (
        u.Assertion(
            subject="ursula-loebig-angerring",
            property="civil_status",
            value="widowed",
            source="directory-1912",
            qualifiers=(("point_in_time", u.Date(1912)),),
            excerpt="Löbig, Ursula, Wwe., Angerring 57",
        ),
    ),
)


def source_case(key: str) -> SourceCase:
    return SOURCE_CASES[key]


@dataclass(frozen=True)
class Scenario:
    sources: tuple[str, ...]
    required: tuple[str, ...]
    acceptable_abstention: tuple[str, ...] = ()


SCENARIOS = {
    "contradictory_births": Scenario(
        ("birth-extract-1938", "naturalisation-1952"),
        (
            "Keep both stated dates at day precision with their own support.",
            "Write order does not make either source preferred.",
            "Preserve source location and process provenance in review/write.",
        ),
    ),
    "homonyms": Scenario(
        ("family-tree-1984", "directory-1912"),
        (
            "Do not silently merge the two Ursula Löbig mentions by name.",
            "Keep the daughter's birth separate from the widow's assertions.",
            "Retain the directory's explicit widow status and residence.",
        ),
        ("An unresolved identity proposal may leave the mentions apart.",),
    ),
    "reviewed_equivalence": Scenario(
        ("birth-extract-1938", "naturalisation-1952"),
        (
            "A machine same verdict alone changes no assignment.",
            "Approval may link both mentions to the chosen existing person.",
            "Reviewed equivalence preserves both contradictory sourced dates.",
        ),
        ("Matching may remain uncertain until the explicit human decision.",),
    ),
    "face_confirmation": Scenario(
        (FACE_SOURCE,),
        (
            "Wastl/Hilde suggestions do not automatically confirm identity.",
            "Confirm each displayed person/region before writing depicts.",
            "Bulk approval cannot confirm faces.",
            "Changed associations require new approval.",
        ),
        ("An unmatched or unconfirmed face has no depicts assertion.",),
    ),
}


def run_metadata(source_key: str) -> ExtractionRunMetadata:
    """Truthful process provenance for an authored proposed-claim fixture."""
    source_case(source_key)
    return ExtractionRunMetadata(
        model_identifier=f"authored-fixture:{source_key}",
        pipeline_version="semantic-acceptance-v1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _human(
    local_id: str,
    label: str,
    given: tuple[str, ...],
    surnames: tuple[str, ...],
) -> EntityRecord:
    return EntityRecord(
        id=local_id,
        label=label,
        aliases=[label],
        kind="human",
        structure=PersonStructure(
            given=[GivenName(written=name) for name in given],
            surnames=[Surname(written=name) for name in surnames],
        ),
    )


def _time(value: u.Date) -> TimeValue:
    return TimeValue(year=value.year, month=value.month, day=value.day)


def birth_batch(
    source_key: str, *, subject: QidRef | LocalRef | None = None
) -> ClaimBatch:
    if source_key not in SCENARIOS["contradictory_births"].sources:
        raise ValueError("birth_batch needs one of Gustav's two birth sources")
    source = source_case(source_key)
    (fact,) = source.expected
    assert isinstance(fact.value, u.Date)
    subject = subject or LocalRef(local_id="gustav")
    entities = []
    if isinstance(subject, LocalRef):
        naturalised = source_key == "naturalisation-1952"
        entities = [
            _human(
                subject.local_id,
                "Gustave Lobig" if naturalised else "Gustav Löbig",
                ("Gustave", "Gustav") if naturalised else ("Gustav",),
                ("LOBIG", "LÖBIG") if naturalised else ("Löbig",),
            )
        ]
    return ClaimBatch(
        extraction_run=run_metadata(source_key),
        entities=entities,
        claims=[
            Claim(
                subject=subject,
                property="date_of_birth",
                value=_time(fact.value),
                source=source.source_ref(fact.excerpt),
            )
        ],
    )


def homonym_batches(
    *, local_ids: tuple[str, str] = ("daughter", "widow")
) -> tuple[ClaimBatch, ClaimBatch]:
    daughter_source = source_case("family-tree-1984")
    widow_source = source_case("directory-1912")
    (born,) = daughter_source.expected
    resident, widowed = widow_source.expected
    assert isinstance(born.value, u.Date)
    daughter = ClaimBatch(
        extraction_run=run_metadata(daughter_source.key),
        entities=[
            _human(local_ids[0], "Ursula Löbig", ("Ursula",), ("Löbig",))
        ],
        claims=[
            Claim(
                subject=LocalRef(local_id=local_ids[0]),
                property="date_of_birth",
                value=_time(born.value),
                source=daughter_source.source_ref(born.excerpt),
            )
        ],
    )
    widow = ClaimBatch(
        extraction_run=run_metadata(widow_source.key),
        entities=[
            _human(local_ids[1], "Ursula Löbig", ("Ursula",), ("Löbig",))
        ],
        claims=[
            Claim(
                subject=LocalRef(local_id=local_ids[1]),
                property="residence",
                value=ItemValue(value=QidRef(qid=ANGERRING_QID)),
                qualifiers=[
                    Qualifier(property=prop, value=_time(value))
                    for prop, value in resident.qualifiers
                    if isinstance(value, u.Date)
                ],
                source=widow_source.source_ref(resident.excerpt),
            ),
            Claim(
                subject=LocalRef(local_id=local_ids[1]),
                property=widowed.property,
                value=ItemValue(value=VocabRef(key=str(widowed.value))),
                qualifiers=[
                    Qualifier(property=prop, value=_time(value))
                    for prop, value in widowed.qualifiers
                    if isinstance(value, u.Date)
                ],
                source=widow_source.source_ref(widowed.excerpt),
            ),
        ],
    )
    return daughter, widow
