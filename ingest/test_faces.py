"""Offline tests for face proposals (pytest; no detector dependency)."""

import pytest

from ingest.claims import QidRef, validate_against_manifest
from ingest.faces import (
    DEFAULT_THRESHOLD,
    DetectedFace,
    DetectionResult,
    Gallery,
    PixelBox,
    best_match,
    cosine_similarity,
    enroll_from_detections,
    proposals_to_batch,
    to_region,
)
from ingest.review import export_review_csv

# Unit-normed toy embeddings: person A along one axis, person B along
# another, a third vector between them.
DIM = 8
A = [1.0] + [0.0] * (DIM - 1)
B = [0.0, 1.0] + [0.0] * (DIM - 2)
NEAR_A = [0.9, 0.1] + [0.0] * (DIM - 2)


def face(index: int, embedding, x=100, y=50) -> DetectedFace:
    return DetectedFace(
        index=index,
        bbox=PixelBox(x=x, y=y, width=200, height=250),
        detection_score=0.99,
        embedding=embedding,
    )


def detection(*faces) -> DetectionResult:
    return DetectionResult(
        photo_file="photo.jpg",
        artifact_qid="Q30",
        image_width=1000,
        image_height=500,
        model_identifier="fake-detector",
        faces=list(faces),
    )


def gallery_with_a(qid="Q26") -> Gallery:
    gallery = Gallery()
    gallery.enroll(label="Person One", embedding=A, qid=qid)
    return gallery


class TestGeometry:
    def test_pixel_to_percent(self):
        region = to_region(PixelBox(x=100, y=50, width=200, height=250), 1000, 500)
        assert region.as_wikibase_string() == "10%,10%,20%,50%"

    def test_overflowing_box_is_clipped_not_widened(self):
        region = to_region(PixelBox(x=950, y=480, width=100, height=100), 1000, 500)
        assert region.x + region.width <= 100
        assert region.y + region.height <= 100


class TestMatching:
    def test_cosine(self):
        assert cosine_similarity(A, A) == pytest.approx(1.0)
        assert cosine_similarity(A, B) == pytest.approx(0.0)

    def test_best_match_picks_highest(self):
        gallery = gallery_with_a()
        gallery.enroll(label="Person Two", embedding=B, qid="Q27")
        person, score = best_match(NEAR_A, gallery, DEFAULT_THRESHOLD)
        assert person.label == "Person One"
        assert score == pytest.approx(cosine_similarity(NEAR_A, A))

    def test_threshold_cuts_off(self):
        assert best_match(B, gallery_with_a(), DEFAULT_THRESHOLD) is None

    def test_empty_gallery_matches_nothing(self):
        assert best_match(A, Gallery(), DEFAULT_THRESHOLD) is None


class TestGallery:
    def test_enroll_appends_to_existing_person(self):
        gallery = gallery_with_a()
        gallery.enroll(label="Person One", embedding=NEAR_A, qid="Q26")
        assert len(gallery.persons) == 1
        assert len(gallery.persons[0].embeddings) == 2

    def test_enroll_upgrades_missing_qid(self):
        gallery = Gallery()
        gallery.enroll(label="Person One", embedding=A)
        gallery.enroll(label="Person One", embedding=NEAR_A, qid="Q26")
        assert gallery.persons[0].qid == "Q26"

    def test_save_load_round_trip(self, tmp_path):
        path = tmp_path / "gallery.json"
        gallery_with_a().save(path)
        assert Gallery.load(path) == gallery_with_a()

    def test_load_missing_file_is_empty(self, tmp_path):
        assert Gallery.load(tmp_path / "none.json") == Gallery()

    def test_enroll_from_detections_unknown_index(self):
        with pytest.raises(ValueError, match="no face with index 7"):
            enroll_from_detections(
                detection(face(0, A)), 7, Gallery(), label="X"
            )


class TestProposals:
    def test_matched_face_becomes_valid_depicts_claim(self):
        batch, unmatched = proposals_to_batch(
            detection(face(0, NEAR_A)), gallery_with_a()
        )
        assert unmatched == []
        assert len(batch.claims) == 1
        claim = batch.claims[0]
        assert claim.subject == QidRef(qid="Q30")
        assert claim.property == "depicts"
        assert claim.value.value == QidRef(qid="Q26")
        assert claim.qualifiers[0].property == "relative_position_within_image"
        assert claim.qualifiers[0].value.value == "10%,10%,20%,50%"
        assert claim.source.artifact == QidRef(qid="Q30")
        assert claim.source.region.as_wikibase_string() == "10%,10%,20%,50%"
        assert 0 < claim.confidence <= 1
        assert batch.extraction_run.model_identifier == (
            "face-detection:fake-detector"
        )
        # rides the same contract: manifest validation passes
        assert validate_against_manifest(batch) == []

    def test_unmatched_face_produces_no_claim(self):
        batch, unmatched = proposals_to_batch(
            detection(face(0, NEAR_A), face(1, B)), gallery_with_a()
        )
        assert len(batch.claims) == 1
        assert unmatched == [1]

    def test_label_only_person_becomes_local_ref(self):
        gallery = Gallery()
        gallery.enroll(label="Person One", embedding=A)  # no QID yet
        batch, _ = proposals_to_batch(detection(face(0, A)), gallery)
        ref = batch.claims[0].value.value
        assert ref.kind == "local"
        assert ref.label == "Person One"
        assert ref.instance_of == "human"

    def test_proposals_flow_into_review_csv(self, tmp_path):
        batch, _ = proposals_to_batch(detection(face(0, A)), gallery_with_a())
        csv_path = tmp_path / "review.csv"
        assert export_review_csv(batch, csv_path) == 1
        content = csv_path.read_text()
        assert "depicts" in content
        assert "relative_position_within_image=10%,10%,20%,50%" in content
