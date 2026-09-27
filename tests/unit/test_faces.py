"""Offline tests for face proposals (pytest; no detector dependency)."""

import csv

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import (
    Claim,
    ClaimBatch,
    ItemValue,
    QidRef,
    SourceReference,
    TimeValue,
    validate_against_manifest,
)
from yizkr.cli import build_parser
from yizkr.pipeline.extract import run_metadata
from yizkr.pipeline.faces import (
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
from yizkr.pipeline.review import (
    accept_all,
    apply_review,
    claim_id,
    export_review_csv,
)

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
        region = to_region(
            PixelBox(x=100, y=50, width=200, height=250), 1000, 500
        )
        assert region.as_wikibase_string() == "10%,10%,20%,50%"

    def test_overflowing_box_is_clipped_not_widened(self):
        region = to_region(
            PixelBox(x=950, y=480, width=100, height=100), 1000, 500
        )
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


# The universe's 1890s studio portrait, under the QID `detection` gives it.
PORTRAIT = QidRef(qid="Q30")


def portrait_sitters() -> Gallery:
    """Two of the portrait's sitters, enrolled under placeholder QIDs."""
    people = u.by_key()
    gallery = Gallery()
    gallery.enroll(label=people["eugen-loebig"].label, embedding=A, qid="Q26")
    gallery.enroll(
        label=people["sebastian-loebig"].label, embedding=B, qid="Q27"
    )
    return gallery


def on_portrait(prop: str, value) -> Claim:
    """A claim the portrait makes about itself."""
    return Claim(
        subject=PORTRAIT,
        property=prop,
        value=value,
        source=SourceReference(artifact=PORTRAIT),
    )


def portrait_faces() -> ClaimBatch:
    """Two faces on the portrait, matched to the enrolled sitters."""
    batch, unmatched = proposals_to_batch(
        detection(face(0, A), face(1, B, x=400)), portrait_sitters()
    )
    assert (len(batch.claims), unmatched) == (2, [])
    return batch


def portrait_text() -> ClaimBatch:
    """What extraction reads off the portrait: the 1890s its studio mount
    gives, then two of the sitters named on its reverse (Eugen, as
    "Muckel", and Rahel)."""
    return ClaimBatch(
        extraction_run=run_metadata("fake-model"),
        claims=[
            on_portrait(
                "point_in_time", TimeValue(year=1890, precision="decade")
            ),
            on_portrait("depicts", ItemValue(value=QidRef(qid="Q26"))),
            on_portrait("depicts", ItemValue(value=QidRef(qid="Q28"))),
        ],
    )


# Hand edits to a review sheet, as a curator's spreadsheet saves them.


def edit_rows(path, edit) -> None:
    """Re-save a sheet with `edit` applied to each row, a dict by column."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = [edit(dict(row)) for row in reader]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def edit_columns(path, edit) -> None:
    """Re-save a sheet with `edit(header, body)` applied to its cells as
    lists, for edits to the columns themselves."""
    with open(path, newline="") as f:
        header, *body = csv.reader(f)
    header, body = edit(header, body)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([header, *body])


def set_depicts_cells(value: str):
    """A row edit that writes `value` over each `depicts` property cell."""

    def edit(row: dict) -> dict:
        if row["property"] == "depicts":
            row["property"] = value
        return row

    return edit


def drop_property(header, body):
    at = header.index("property")
    return (
        header[:at] + header[at + 1 :],
        [row[:at] + row[at + 1 :] for row in body],
    )


def rename_property(name: str):
    def edit(header, body):
        return [name if col == "property" else col for col in header], body

    return edit


def add_blank_property(header, body):
    return [*header, "property"], [[*row, ""] for row in body]


def pose_as_equivalences(header, body):
    """Add an equivalences sheet's columns, judging every row `same`."""
    return (
        [*header, "entity_id", "qid", "verdict"],
        [[*row, f"e{n}", "Q26", "same"] for n, row in enumerate(body, 1)],
    )


class TestAcceptAllRefusesDepicts:
    """ADR-0005: only a per-row human decision confirms a depicts claim,
    whether face detection or a source's text proposed it, so `accept_all`
    refuses a claims sheet with a depicts row, or without the single
    `property` column that shows them, and writes nothing (#24)."""

    def test_depicts_only_sheet_is_refused(self, tmp_path):
        faces = portrait_faces()
        review_csv = tmp_path / "review.csv"
        export_review_csv(faces, review_csv)
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="depicts") as refusal:
            accept_all(review_csv, out)

        for claim in faces.claims:
            assert f"claim {claim_id(claim)}" in str(refusal.value)
        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    @pytest.mark.parametrize(
        "encoding", ["utf-8", "utf-8-sig"], ids=["plain", "bom"]
    )
    def test_mixed_sheet_is_refused_naming_only_its_depicts_rows(
        self, tmp_path, encoding
    ):
        # "utf-8-sig" saves the sheet with a byte-order mark, as Excel's
        # "CSV UTF-8" does: it must hide neither the sheet's kind nor a row
        text = portrait_text()
        date, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        review_csv.write_text(review_csv.read_text(), encoding=encoding)
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="depicts") as refusal:
            accept_all(review_csv, out)

        named = str(refusal.value)
        for claim in depicts:
            assert f"claim {claim_id(claim)}" in named
        assert claim_id(date) not in named
        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    def test_hand_edited_depicts_cells_are_refused(self, tmp_path):
        text = portrait_text()
        _, eugen, rahel = text.claims
        cells = {claim_id(eugen): " Depicts ", claim_id(rahel): "DEPICTS"}
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(
            review_csv,
            lambda row: {
                **row,
                "property": cells.get(row["claim_id"], row["property"]),
            },
        )

        with pytest.raises(ValueError, match="depicts") as refusal:
            accept_all(review_csv, tmp_path / "review.reviewed.csv")

        for claim in (eugen, rahel):
            assert f"claim {claim_id(claim)}" in str(refusal.value)

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(drop_property, id="deleted"),
            pytest.param(rename_property("Property"), id="capitalised"),
            pytest.param(rename_property(" property"), id="leading-space"),
            pytest.param(rename_property("property "), id="trailing-space"),
            pytest.param(rename_property("\ufeffproperty"), id="bom-inside"),
            pytest.param(add_blank_property, id="blank-duplicate"),
            pytest.param(
                lambda header, body: pose_as_equivalences(
                    *drop_property(header, body)
                ),
                id="posing-as-equivalences",
            ),
        ],
    )
    def test_claims_sheet_without_one_property_column_is_refused(
        self, tmp_path, edit
    ):
        review_csv = tmp_path / "review.csv"
        export_review_csv(portrait_faces(), review_csv)
        edit_columns(review_csv, edit)
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="'property' column"):
            accept_all(review_csv, out)

        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    def test_the_portraits_other_claims_are_still_accepted(self, tmp_path):
        # the refusal keys on the property, not on the photograph
        text = portrait_text()
        dated = text.model_copy(update={"claims": text.claims[:1]})
        review_csv = tmp_path / "review.csv"
        export_review_csv(dated, review_csv)

        result = accept_all(review_csv, tmp_path / "review.reviewed.csv")
        assert (result.mode, result.total, result.approved) == ("claims", 1, 1)

    def test_depicts_row_without_claim_id_is_named_by_position(self, tmp_path):
        # a damaged sheet is still refused
        sheet = tmp_path / "review.csv"
        sheet.write_text(
            "claim_id,property,decision\n"  # row 1, as a spreadsheet counts
            "0a1b2c3d4e5f,point_in_time,\n"  # row 2
            ",depicts,\n"  # row 3, its claim_id lost
        )
        with pytest.raises(ValueError, match="row 3"):
            accept_all(sheet, tmp_path / "review.reviewed.csv")

    def test_cli_exits_non_zero_and_keeps_the_reviewed_sheet(self, tmp_path):
        text = portrait_text()
        _, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        reviewed = tmp_path / "review.reviewed.csv"
        reviewed.write_text("decided by hand\n")
        args = build_parser().parse_args(
            ["ingest", "accept-all", "--csv", str(review_csv), "--force"]
        )

        with pytest.raises(SystemExit) as exit_:
            args.func(args)

        # sys.exit(message): the message goes to stderr, the status is 1
        message = exit_.value.code
        assert isinstance(message, str)
        for claim in depicts:
            assert f"claim {claim_id(claim)}" in message
        assert reviewed.read_text() == "decided by hand\n"


class TestApplyReviewRefusesBulkAcceptedDepicts:
    """ADR-0005 at the gate itself: `apply_review` refuses a bulk-accept on
    any claim whose property in the batch is depicts, whatever the sheet's
    cells say, and still takes a per-row human approve (#24)."""

    @pytest.mark.parametrize(
        "cell",
        ["point_in_time", "", "P180", "\u200bdepicts"],
        ids=["another-property", "blank", "wikidata-id", "zero-width-space"],
    )
    def test_bulk_accept_on_a_hidden_depicts_claim_is_refused(
        self, tmp_path, cell
    ):
        text = portrait_text()
        _, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(review_csv, set_depicts_cells(cell))
        reviewed = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed)  # the edited cells get past it

        with pytest.raises(ValueError, match="bulk-accept") as refusal:
            apply_review(text, reviewed)

        for claim in depicts:
            assert f"claim {claim_id(claim)}" in str(refusal.value)

    def test_sheet_posing_as_equivalences_still_gets_bulk_accept(
        self, tmp_path
    ):
        # blanked depicts cells under an equivalences sheet's columns: its
        # claim_id column keeps it a claims sheet, so accept-all writes
        # bulk-accept, never the approve an equivalences verdict would get
        text = portrait_text()
        _, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(review_csv, set_depicts_cells(""))
        edit_columns(review_csv, pose_as_equivalences)
        reviewed = tmp_path / "review.reviewed.csv"

        assert accept_all(review_csv, reviewed).mode == "claims"
        with pytest.raises(ValueError, match="bulk-accept") as refusal:
            apply_review(text, reviewed)

        for claim in depicts:
            assert f"claim {claim_id(claim)}" in str(refusal.value)

    @pytest.mark.parametrize(
        "encoding", ["utf-8", "utf-8-sig"], ids=["plain", "bom"]
    )
    def test_per_row_approve_on_a_depicts_claim_passes(
        self, tmp_path, encoding
    ):
        # the human gate: approve typed on each depicts row, beside a
        # bulk-accept on the rest; "utf-8-sig" saves the sheet with a
        # byte-order mark, as Excel's "CSV UTF-8" does
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(
            review_csv,
            lambda row: {
                **row,
                "decision": "approve"
                if row["property"] == "depicts"
                else "bulk-accept",
            },
        )
        review_csv.write_text(review_csv.read_text(), encoding=encoding)

        approved, warnings = apply_review(text, review_csv)

        assert approved.claims == text.claims
        assert warnings == []
