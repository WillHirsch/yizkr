"""Semantic carry-forward through the production identity-review operations."""

import csv
import io
from dataclasses import replace

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewedRemap,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    export_review,
    prepare_review,
    record_review,
)
from yizkr.pipeline.source_context import SourceContext
from yizkr.pipeline.transcribe import Transcription


def request():
    artifact = ArtifactRef.from_bytes(b"fictional family names")
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "fictional",
                "pipeline_version": "test",
                "started_at": "2026-01-01T12:00:00Z",
            },
            "entities": [
                {
                    "id": "leopold",
                    "label": "Leopold Löbig",
                    "kind": "human",
                    "mention_lines": [1],
                },
                {
                    "id": "klara",
                    "label": "Klara Löbig",
                    "kind": "human",
                    "mention_lines": [2],
                },
            ],
        }
    )
    reading = Transcription(
        artifact=artifact,
        text="Leopold Löbig.\nKlara Löbig.",
        language="de",
        engine="fixture",
    )
    return ReviewRequest(
        sources=(ReviewSource(artifact.sha1, batch, reading),),
        proposals=tuple(
            ReviewProposal(
                operation="new_entity",
                mentions=(
                    MentionKey(artifact_sha1=artifact.sha1, local_id=key),
                ),
            )
            for key in ("leopold", "klara")
        ),
    )


def approve_sheet(store, prepared):
    reader = csv.DictReader(io.StringIO(export_review(store, prepared)))
    rows = list(reader)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, reader.fieldnames)
    writer.writeheader()
    writer.writerows(dict(row, decision="approve") for row in rows)
    record_review(store, output.getvalue(), actor="curator")


def test_locator_shift_preserves_then_corrected_reading_requeues_only_affected(
    tmp_path,
):
    original = request()
    with ReviewStore(tmp_path / "reviews.sqlite") as store:
        first = prepare_review(store, original)
        approve_sheet(store, first)
        source = original.sources[0]
        shifted = source.batch.model_copy(deep=True)
        for entity in shifted.entities:
            entity.mention_lines = [entity.mention_lines[0] + 1]
        shifted_reading = source.reading.model_copy(
            update={"text": "\n" + source.reading.text}
        )
        current = replace(
            original,
            sources=(
                ReviewSource(source.artifact_sha1, shifted, shifted_reading),
            ),
        )
        retained = prepare_review(store, current)
        assert set(retained.projection.new_entities) == {
            mention.id for mention in first.mentions.values()
        }
        leopold = retained.mentions[source.artifact_sha1, "leopold"]
        corrected = shifted_reading.model_copy(
            update={
                "text": "\nLeopold Löbig, son of Felix Löbig.\nKlara Löbig."
            }
        )
        changed = replace(
            current,
            sources=(ReviewSource(source.artifact_sha1, shifted, corrected),),
            remaps=(
                ReviewedRemap(
                    mention=MentionKey(
                        artifact_sha1=source.artifact_sha1, local_id="leopold"
                    ),
                    prior_id=leopold.id,
                    prior_revision=leopold.revision,
                    actor="curator",
                    reason="Corrected the same located occurrence",
                    time="2026-01-02T12:00:00Z",
                ),
            ),
        )
        projected = prepare_review(store, changed)
        assert (
            projected.mentions[source.artifact_sha1, "leopold"].id
            == leopold.id
        )
        assert projected.projection.new_entities == (
            first.mentions[source.artifact_sha1, "klara"].id,
        )
        assert sorted(
            app.status for app in projected.projection.applications
        ) == [
            "applied",
            "needs_review",
        ]


def test_source_keys_normalize_and_duplicate_remaps_refuse_before_changes(
    tmp_path,
):
    original = request()
    source = original.sources[0]
    uppercase = ReviewSource(
        source.artifact_sha1.upper(), source.batch, source.reading
    )
    assert uppercase.artifact_sha1 == source.artifact_sha1
    with ReviewStore(tmp_path / "reviews.sqlite") as store:
        prepared = prepare_review(
            store, replace(original, sources=(uppercase,))
        )
        mention = prepared.mentions[source.artifact_sha1, "leopold"]
        remap = ReviewedRemap(
            mention=MentionKey(
                artifact_sha1=source.artifact_sha1, local_id="leopold"
            ),
            prior_id=mention.id,
            prior_revision=mention.revision,
            actor="curator",
            reason="Reviewed occurrence",
            time="2026-01-02T12:00:00Z",
        )
        other = remap.model_copy(
            update={
                "mention": MentionKey(
                    artifact_sha1=source.artifact_sha1,
                    local_id="klara",
                )
            }
        )
        with pytest.raises(ReviewRefused, match="prior occurrence"):
            prepare_review(store, replace(original, remaps=(remap, other)))


def test_approval_cannot_bypass_missing_container_attribution(tmp_path):
    original = request()
    source = original.sources[0]
    unsupported = source.reading.model_copy(
        update={
            "source_context": SourceContext(container=source.reading.artifact)
        }
    )
    current = replace(
        original,
        sources=(
            ReviewSource(source.artifact_sha1, source.batch, unsupported),
        ),
    )
    with ReviewStore(tmp_path / "reviews.sqlite") as store:
        prepared = prepare_review(store, current)
        approve_sheet(store, prepared)
        projection = prepare_review(store, current).projection
        assert projection.new_entities == ()
        assert all(app.status == "blocked" for app in projection.applications)
        assert all(
            "source evidence: ambiguous or missing source scope" in app.reasons
            for app in projection.applications
        )
