"""Source-occurrence correspondence and material identity evidence."""

from __future__ import annotations

import copy
import unicodedata

import pytest
from pydantic import ValidationError

from tests.fixtures import example_universe as u
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    PersonStructure,
    PlaceStructure,
    SourceSupport,
)
from yizkr.pipeline.review_mentions import (
    OccurrenceAnchor,
    anchor_scope,
    canonical_basis,
    observe_mentions,
)
from yizkr.pipeline.source_context import LocatedReading, SourceContext
from yizkr.pipeline.transcribe import (
    TranscribedPage,
    Transcription,
    reading_digest,
)

SOURCES = {source.key: source for source in u.SOURCES}
TEXT = SOURCES["birth-extract-1938"].text
ARTIFACT = ArtifactRef.from_bytes(TEXT.encode())
CONTAINER = ArtifactRef.from_bytes(b"fictional birth extract container")
RUN = {
    "model_identifier": "scripted-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}


def birth_batch(child="e1", father="e2", line=1):
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {
                    "id": child,
                    "label": "Gustav Löbig",
                    "aliases": ["Gustav"],
                    "kind": "human",
                    "mention_lines": [line],
                },
                {
                    "id": father,
                    "label": "Sebastian Löbig",
                    "kind": "human",
                    "mention_lines": [line],
                },
            ],
            "claims": [
                {
                    "subject": {"kind": "local", "local_id": child},
                    "property": "father",
                    "value": {
                        "type": "item",
                        "value": {"kind": "local", "local_id": father},
                    },
                    "source": {
                        "artifact": ARTIFACT.model_dump(),
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": TEXT,
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )


def reading(text=TEXT):
    return Transcription(
        artifact=ARTIFACT, text=text, language="de", engine="scripted"
    )


def support(transcription, *, part="1", placements=(1, 2)):
    return SourceSupport.model_validate(
        {
            "container": CONTAINER.model_dump(),
            "part_id": part * 64,
            "locator": {"kind": "image", "native_id": "5 0 R"},
            "placements": [
                {
                    "id": str(position) * 64,
                    "locator": {
                        "kind": "image-paint",
                        "position": position,
                        "path": "objects/1",
                    },
                }
                for position in placements
            ],
            "reading_sha256": reading_digest(transcription),
        }
    )


def located(transcription=None):
    transcription = transcription or reading()
    native = support(transcription)
    transcription.source_context = SourceContext(
        container=CONTAINER,
        readings=[
            LocatedReading(
                start=0,
                end=len(transcription.text),
                owner=ARTIFACT,
                support=native,
            )
        ],
    )
    return transcription


def rebuilt(batch, edit):
    data = batch.model_dump(mode="json")
    edit(data)
    return ClaimBatch.model_validate(data)


def keys(batch, transcription):
    return {
        item.local_id: item.correspondence_key
        for item in observe_mentions(ARTIFACT.sha1, batch, transcription)
    }


def test_local_id_swaps_and_manifest_order_do_not_swap_source_occurrences():
    original = birth_batch()
    swapped = birth_batch("e2", "e1")
    swapped.entities.reverse()
    first = keys(original, reading())
    second = keys(swapped, reading())
    assert first["e1"] == second["e2"] is not None
    assert first["e2"] == second["e1"] is not None
    assert first["e1"] != first["e2"]
    assert canonical_basis(
        original, "e1", {"e1": "child", "e2": "father"}
    ) == (canonical_basis(swapped, "e2", {"e2": "child", "e1": "father"}))


def test_verified_line_shift_records_changed_ranges_and_reading_provenance():
    first = observe_mentions(ARTIFACT.sha1, birth_batch(), located())[0]
    prefix = SOURCES["directory-1912"].text + "\n"
    later = observe_mentions(
        ARTIFACT.sha1,
        birth_batch(line=3),
        located(reading(prefix + TEXT)),
    )[0]
    assert first.correspondence_key == later.correspondence_key is not None
    assert first.reading_sha256 != later.reading_sha256
    assert first.product_sha256 != later.product_sha256
    assert later.anchor.quote_start - first.anchor.quote_start == len(prefix)
    assert first.anchor.quote == later.anchor.quote == TEXT
    assert first.anchor.literal == later.anchor.literal == "Gustav"


def test_display_label_and_process_metadata_are_not_identity_evidence():
    first = birth_batch()

    def edit(data):
        data["entities"][0]["label"] = "Gustave Lobig"
        data["entities"][0]["summary"] = "The child in the register."
        data["entities"][0]["qid"] = "Q1"
        data["extraction_run"]["model_identifier"] = "another-scripted-model"
        data["extraction_run"]["started_at"] = "2026-10-01T09:00:00Z"
        data["claims"][0]["confidence"] = 0.8
        data["claims"][0]["subject"]["label"] = "Gustave Lobig"
        data["claims"][0]["value"]["value"]["label"] = "Sebastian"

    later = rebuilt(first, edit)
    stable = {"e1": "child", "e2": "father"}
    assert canonical_basis(first, "e1", stable) == canonical_basis(
        later, "e1", stable
    )
    assert keys(first, reading())["e1"] == keys(later, reading())["e1"]


def test_duplicate_claimed_occurrences_abstain_without_order_tiebreakers():
    batch = birth_batch()
    batch.claims = []
    batch.entities[1] = batch.entities[0].model_copy(update={"id": "e2"})
    observations = observe_mentions(ARTIFACT.sha1, batch, reading())
    assert all(item.anchor is None for item in observations)
    assert {tuple(item.limitations) for item in observations} == {
        ("shared_source_occurrence",)
    }
    # A reviewer can still inspect the exact source context, but its presence
    # alone does not tell the two proposed mentions apart.
    assert all(item.source_scope.owner == ARTIFACT for item in observations)
    assert all(item.context_quote == TEXT for item in observations)
    assert all(item.context_start == 0 for item in observations)
    assert all(item.context_end == len(TEXT) for item in observations)
    batch.entities.reverse()
    assert all(
        item.anchor is None
        for item in observe_mentions(ARTIFACT.sha1, batch, reading())
    )


def test_same_name_twice_in_one_declared_context_has_no_automatic_anchor():
    batch = birth_batch()
    batch.claims = []
    batch.entities = [
        batch.entities[0].model_copy(
            update={"label": "Ursula", "aliases": [], "mention_lines": [1]}
        ),
        batch.entities[1].model_copy(
            update={"label": "Ursula", "aliases": [], "mention_lines": [1]}
        ),
    ]
    result = observe_mentions(ARTIFACT.sha1, batch, reading("Ursula; Ursula"))
    assert all(item.anchor is None for item in result)
    assert all(item.source_scope is not None for item in result)
    assert all(item.correspondence_key is None for item in result)
    assert all(
        "missing_or_ambiguous_literal_mention" in item.limitations
        for item in result
    )


def test_native_page_scope_can_distinguish_a_repeated_quote():
    duplicate = reading(TEXT + "\n" + TEXT)
    assert keys(birth_batch(), duplicate) == {"e1": None, "e2": None}
    duplicate.pages = [
        TranscribedPage.model_validate(
            {
                "page": page,
                "start": start,
                "end": start + len(TEXT),
                "status": "transcribed",
                "provenance": {"method": "vision", "engine": "scripted"},
            }
        )
        for page, start in [(1, 0), (2, len(TEXT) + 1)]
    ]
    duplicate = Transcription.model_validate(duplicate.model_dump())
    first = observe_mentions(ARTIFACT.sha1, birth_batch(), duplicate)[0]
    second = observe_mentions(ARTIFACT.sha1, birth_batch(line=2), duplicate)[0]
    assert first.anchor.physical_page == 1
    assert second.anchor.physical_page == 2
    assert first.correspondence_key != second.correspondence_key
    # Persisted readings are schema-validated. A caller editing a model in
    # memory must not turn an invalid page range into verified source scope.
    duplicate.pages[0].end = len(duplicate.text) + 1
    result = observe_mentions(ARTIFACT.sha1, birth_batch(), duplicate)
    assert all(item.source_scope is None for item in result)
    assert all(item.limitations == ["invalid_page_scope"] for item in result)


@pytest.mark.parametrize(
    "case",
    [
        "unsupported",
        "other-owner",
        "other-part",
        "partial-other-part",
        "digest",
    ],
)
def test_unverified_or_ambiguous_native_scope_is_not_selected(case):
    transcription = located()
    span = transcription.source_context.readings[0]
    if case == "unsupported":
        span.state, span.reason = "unsupported", "unresolved source owner"
    elif case == "other-owner":
        transcription.source_context.readings.append(
            span.model_copy(update={"owner": CONTAINER})
        )
    elif case in ("other-part", "partial-other-part"):
        transcription.source_context.readings.append(
            span.model_copy(
                update={
                    "support": support(transcription, part="2"),
                    "end": 20 if case == "partial-other-part" else span.end,
                }
            )
        )
    else:
        span.support.reading_sha256 = "0" * 64
    assert keys(birth_batch(), transcription) == {"e1": None, "e2": None}
    assert all(
        item.source_scope is None
        for item in observe_mentions(
            ARTIFACT.sha1, birth_batch(), transcription
        )
    )


def test_all_native_placements_survive_and_order_is_not_identity():
    transcription = located()
    first = observe_mentions(ARTIFACT.sha1, birth_batch(), transcription)[0]
    assert len(first.anchor.support.placements) == 2
    transcription.source_context.readings[0].support.placements.reverse()
    reversed_order = observe_mentions(
        ARTIFACT.sha1, birth_batch(), transcription
    )[0]
    assert first.correspondence_key == reversed_order.correspondence_key
    assert anchor_scope(first.anchor) == anchor_scope(reversed_order.anchor)
    transcription.source_context.readings[0].support.placements.pop()
    changed = observe_mentions(ARTIFACT.sha1, birth_batch(), transcription)[0]
    assert first.correspondence_key != changed.correspondence_key


def test_a_conflicting_reading_digest_is_not_hidden_by_duplicate_span_order():
    transcription = located()
    spans = transcription.source_context.readings
    spans.append(spans[0].model_copy(deep=True))
    spans[1].support.reading_sha256 = "0" * 64
    for _ in range(2):
        result = observe_mentions(ARTIFACT.sha1, birth_batch(), transcription)
        assert all(item.anchor is None for item in result)
        assert all(
            item.limitations == ["reading_support_mismatch"] for item in result
        )
        spans.reverse()


@pytest.mark.parametrize("lines", [[], [2], [1, 3]])
def test_missing_or_unlocated_mentions_are_explicitly_unsupported(lines):
    batch = birth_batch()
    batch.entities[0].mention_lines = lines
    observation = observe_mentions(ARTIFACT.sha1, batch, reading())[0]
    assert observation.anchor is None
    assert observation.limitations == [
        "missing_or_noncontiguous_mention_lines"
    ]
    absent = observe_mentions(ARTIFACT.sha1, batch)[0]
    assert absent.anchor is None and absent.limitations == [
        "reading_unavailable"
    ]


def test_reading_source_identity_and_anchor_ranges_are_checked():
    wrong = reading().model_copy(update={"artifact": CONTAINER})
    with pytest.raises(ValueError, match="another source"):
        observe_mentions(ARTIFACT.sha1, birth_batch(), wrong)
    anchor = observe_mentions(ARTIFACT.sha1, birth_batch(), reading())[
        0
    ].anchor
    for updates in (
        {"quote_end": anchor.quote_end + 1},
        {"mention_end": len(TEXT) + 1},
        {"literal": "Sebastian"},
    ):
        with pytest.raises(ValidationError):
            OccurrenceAnchor.model_validate(anchor.model_dump() | updates)


def test_native_location_changes_are_not_automatic_correspondence():
    first = located()
    initial = keys(birth_batch(), first)["e1"]
    changed = first.model_copy(deep=True)
    changed.source_context.readings[0].support.locator.native_id = "6 0 R"
    assert keys(birth_batch(), changed)["e1"] != initial
    changed = first.model_copy(deep=True)
    changed.source_context.readings[0].support.part_id = "2" * 64
    assert keys(birth_batch(), changed)["e1"] != initial


def test_semantic_basis_canonicalizes_sets_and_keeps_place_hierarchy():
    batch = birth_batch()
    batch.entities[0].aliases = ["Gustav", "Gustl"]
    batch.entities[0].structure = PersonStructure.model_validate(
        {
            "kind": "human",
            "given": [{"written": "Gustav"}],
            "nicknames": ["Gustl", "Gus"],
        }
    )
    batch = ClaimBatch.model_validate(batch.model_dump())
    stable = {"e1": "child", "e2": "father"}
    reordered = batch.model_copy(deep=True)
    reordered.entities.reverse()
    reordered.entities[1].aliases.reverse()
    reordered.entities[1].structure.nicknames.reverse()
    assert canonical_basis(batch, "e1", stable) == canonical_basis(
        reordered, "e1", stable
    )
    place = birth_batch()
    place.claims = []
    place.entities[0] = place.entities[0].model_copy(
        update={
            "label": "Brunnenring 35",
            "aliases": [],
            "kind": "place",
            "structure": PlaceStructure(within=["Flörburg", "Hesse"]),
        }
    )
    place = ClaimBatch.model_validate(place.model_dump())
    changed = place.model_copy(deep=True)
    changed.entities[0].structure.within.reverse()
    assert canonical_basis(place, "e1", stable) != canonical_basis(
        changed, "e1", stable
    )


@pytest.mark.parametrize(
    "change",
    [
        "property",
        "qualifier",
        "excerpt",
        "page",
        "owner",
        "support",
        "judgement",
        "alias",
        "eligibility",
        "reference-kind",
        "unknown-value",
    ],
)
def test_material_identity_evidence_changes_require_a_new_basis(change):
    batch = birth_batch()
    data = copy.deepcopy(batch.model_dump(mode="json"))
    claim = data["claims"][0]
    if change == "property":
        claim["property"] = "mother"
    elif change == "qualifier":
        claim["qualifiers"] = [
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1919},
            }
        ]
    elif change == "excerpt":
        claim["source"]["transcription_excerpt"]["text"] = "Gustav"
    elif change == "page":
        claim["source"]["page_number"] = "2"
    elif change == "owner":
        claim["source"]["artifact"] = CONTAINER.model_dump()
    elif change == "support":
        claim["source"]["support"] = support(reading()).model_dump()
    elif change == "judgement":
        claim["note"] = {
            "category": "relation",
            "text": "The father is inferred from the register wording.",
        }
    elif change == "alias":
        data["entities"][0]["aliases"].append("Gustl")
    elif change == "eligibility":
        data["entities"][0]["extract"] = False
    elif change == "reference-kind":
        claim["value"]["value"]["instance_of"] = "human"
    else:
        claim["value"] = {"type": "unknown"}
    changed = ClaimBatch.model_validate(data)
    stable = {"e1": "child", "e2": "father"}
    assert canonical_basis(batch, "e1", stable) != canonical_basis(
        changed, "e1", stable
    )


def test_reading_digest_is_provenance_not_material_evidence_by_itself():
    batch = birth_batch()
    batch.claims[0].source.support = support(reading())
    changed = batch.model_copy(deep=True)
    changed.claims[0].source.support.reading_sha256 = "0" * 64
    stable = {"e1": "child", "e2": "father"}
    assert canonical_basis(batch, "e1", stable) == canonical_basis(
        changed, "e1", stable
    )


def test_equivalent_unicode_preserves_correspondence_and_basis():
    original = birth_batch()
    decomposed = ClaimBatch.model_validate_json(
        unicodedata.normalize("NFD", original.model_dump_json())
    )
    stable = {"e1": "child", "e2": "father"}
    assert canonical_basis(original, "e1", stable) == canonical_basis(
        decomposed, "e1", stable
    )
    assert keys(original, reading()) == keys(
        decomposed, reading(unicodedata.normalize("NFD", TEXT))
    )


def test_unknown_local_references_and_duplicate_locators_refuse():
    batch = birth_batch()
    with pytest.raises(ValueError, match="stable correspondence"):
        canonical_basis(batch, "e1", {"e1": "child"})
    with pytest.raises(ValueError, match="no current mention"):
        canonical_basis(batch, "absent", {"absent": "unknown"})
    duplicate = batch.model_copy(deep=True)
    duplicate.entities.append(duplicate.entities[0])
    with pytest.raises(ValueError, match="duplicate local IDs"):
        observe_mentions(ARTIFACT.sha1, duplicate, reading())
    missing = batch.model_copy(deep=True)
    missing.entities.pop()
    with pytest.raises(ValueError, match="no entity-manifest record"):
        observe_mentions(ARTIFACT.sha1, missing, reading())
