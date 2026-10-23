"""Offline tests for face proposals (pytest; no detector dependency)."""

import codecs
import csv
import json
import sys
from types import SimpleNamespace

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki, _cited
from yizkr import cli
from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    MonolingualText,
    MonolingualValue,
    QidRef,
    Qualifier,
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
    InsightFaceDetector,
    PixelBox,
    UnkeyedDetections,
    best_match,
    cosine_similarity,
    enroll_from_detections,
    load_detections,
    proposals_to_batch,
    to_region,
)
from yizkr.pipeline.review import (
    accept_all,
    apply_review,
    approval_digest,
    claim_id,
    entity_records,
    export_review_csv,
    is_bulk_accepted,
    unapproved_depicts,
)
from yizkr.pipeline.writer import BatchWriter, UnapprovedDepictsError

# The bytes of the universe's 1890s studio portrait, fictional: its depicts
# claims cite it by their SHA-1 (#102), and its item is Q30.
PORTRAIT_SCAN = ArtifactRef.from_bytes(b"fictional scan: a studio portrait")

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
        photo_sha1=PORTRAIT_SCAN.sha1,
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
        # the photograph is its own source, cited by its bytes
        assert claim.source.artifact == PORTRAIT_SCAN
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
        assert f"sha1:{PORTRAIT_SCAN.sha1}" in content


class TestPhotoKey:
    """A detection names its photograph by the SHA-1 of its bytes, which
    the depicts claims cite (#102)."""

    def test_the_detector_keys_the_photo_by_its_bytes(
        self, tmp_path, monkeypatch
    ):
        photo = tmp_path / "portrait.png"
        photo.write_bytes(b"fictional scan: a studio portrait")
        # no model or image library: the detector reads a blank image
        # and finds no face in it
        monkeypatch.setitem(
            sys.modules,
            "cv2",
            SimpleNamespace(imread=lambda path: SimpleNamespace(shape=(5, 9))),
        )
        detector = object.__new__(InsightFaceDetector)
        detector._app = SimpleNamespace(get=lambda image: [])
        detector.model_identifier = "insightface-test"
        result = detector.detect(photo, "Q30")
        assert (result.photo_sha1, result.artifact_qid) == (
            PORTRAIT_SCAN.sha1,
            "Q30",
        )

    @staticmethod
    def unkeyed(tmp_path):
        """A detections file saved without the photo's key."""
        saved = json.loads(detection(face(0, A)).model_dump_json())
        del saved["photo_sha1"]
        path = tmp_path / "detections.json"
        path.write_text(json.dumps(saved))
        return path

    def test_a_detections_file_without_the_photos_key_is_refused(
        self, tmp_path
    ):
        """No dual format: a detections file saved without the photo's
        key cannot give its depicts claims their source. It is refused,
        saying what to do, not with the schema's error."""
        with pytest.raises(UnkeyedDetections, match="run detect-faces"):
            load_detections(self.unkeyed(tmp_path))

    @pytest.mark.parametrize(
        "command",
        [
            ["match-faces"],
            ["enroll-face", "--face", "0", "--label", "Person One"],
        ],
        ids=["match-faces", "enroll-face"],
    )
    def test_the_face_commands_refuse_it_with_the_fix(self, tmp_path, command):
        detections = self.unkeyed(tmp_path)
        gallery = tmp_path / "gallery.json"
        args = build_parser().parse_args(
            [
                "ingest",
                *command,
                *("--detections", str(detections)),
                *("--gallery", str(gallery)),
            ]
        )
        with pytest.raises(SystemExit, match="run detect-faces"):
            args.func(args)
        assert not gallery.exists()


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
        source=SourceReference(artifact=PORTRAIT_SCAN),
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
    gives, as the scan's own `inception` (ADR-0039 §2.2, the universe's
    "studio-photo-1890s"), then two of the sitters named on its reverse
    (Eugen, as "Muckel", and Rahel)."""
    return ClaimBatch(
        extraction_run=run_metadata("fake-model"),
        claims=[
            Claim(
                subject=PORTRAIT_SCAN,
                property="inception",
                value=TimeValue(year=1890, precision="decade"),
                source=SourceReference(artifact=PORTRAIT_SCAN),
            ),
            on_portrait("depicts", ItemValue(value=QidRef(qid="Q26"))),
            on_portrait("depicts", ItemValue(value=QidRef(qid="Q28"))),
        ],
    )


def test_portrait_text_passes_manifest_validation():
    # the date is the document's own, its subject the source itself, as
    # validation takes it (ADR-0035 §3.4, ADR-0039 §2.2; #585)
    text = portrait_text()
    date, *_ = text.claims
    assert (date.property, date.subject) == ("inception", PORTRAIT_SCAN)
    # at the precision the universe gives it ("studio-photo-1890s")
    assert date.value == TimeValue(year=1890, precision="decade")
    assert validate_against_manifest(text) == []


# Two of the portrait's sitters by label, as a gallery holds people not
# yet in the graph, and a third the portrait's text names under Q28.
EUGEN, SEBASTIAN, RAHEL = (
    u.by_key()[key].label
    for key in ("eugen-loebig", "sebastian-loebig", "rahel-loebig")
)


def portrait_faces_by_label(manifest: bool = False) -> ClaimBatch:
    """The portrait's two faces matched to its sitters enrolled by label
    only, so each depicts claim names its sitter by a local ref
    (`face-person-0`, `face-person-1`). With `manifest`, the batch also
    carries an entity-manifest record for each, which the writer resolves
    that local ref through."""
    gallery = Gallery()
    gallery.enroll(label=EUGEN, embedding=A)
    gallery.enroll(label=SEBASTIAN, embedding=B)
    batch, unmatched = proposals_to_batch(
        detection(face(0, A), face(1, B, x=400)), gallery
    )
    assert (len(batch.claims), unmatched) == (2, [])
    if manifest:
        records = [
            EntityRecord(id=ref.local_id, label=ref.label, kind="human")
            for ref in (claim.value.value for claim in batch.claims)
        ]
        batch = batch.model_copy(update={"entities": records})
    return batch


def muckel(label: str, **fields) -> Claim:
    """The name the portrait gives its first sitter, "Muckel", on a claim
    naming that sitter by the first face's local id, with `label` and the
    face's kind, or other `fields`."""
    return Claim(
        subject=LocalRef(
            **{
                "local_id": "face-person-0",
                "label": label,
                "instance_of": "human",
                **fields,
            }
        ),
        property="referred_to_as",
        value=MonolingualValue(
            value=MonolingualText(text="Muckel", language="de")
        ),
        source=SourceReference(artifact=PORTRAIT_SCAN),
    )


# The town the portrait's mount names, "Photograph Brüsrich · Peiltal":
# by a local ref, as a batch without an entity manifest names a place.
PEILTAL = u.by_key()["peiltal"].label
KREDFELD = u.by_key()["kredfeld"].label


def town(label: str = PEILTAL) -> LocalRef:
    """The studio's town, by the local id `studio-town`, with `label`."""
    return LocalRef(local_id="studio-town", label=label, instance_of="place")


def portrait_faces_in(place: LocalRef) -> ClaimBatch:
    """The portrait's two faces, each depicted at `place`: a `location`
    qualifier whose item is a local ref, beside the image region."""
    faces = portrait_faces()
    located = [
        claim.model_copy(
            update={
                "qualifiers": [
                    *claim.qualifiers,
                    Qualifier(
                        property="location", value=ItemValue(value=place)
                    ),
                ]
            }
        )
        for claim in faces.claims
    ]
    return faces.model_copy(update={"claims": located})


def studio_in(place: LocalRef) -> Claim:
    """The mount's own statement: the studio is located at `place`."""
    return Claim(
        subject=LocalRef(
            local_id="studio",
            label=u.by_key()["studio-bruesrich"].label,
            instance_of="organization",
        ),
        property="location",
        value=ItemValue(value=place),
        source=SourceReference(artifact=PORTRAIT_SCAN),
    )


def approved_on_its_row(claim: Claim, batch: ClaimBatch) -> Claim:
    """`claim` of `batch` as `apply_review` returns it after a per-row
    approve."""
    return claim.model_copy(
        update={"row_approval": approval_digest(claim, batch)}
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


def edit_columns(path, *edits) -> None:
    """Re-save a sheet with each `edit(header, body)` applied in turn to its
    cells as lists, for edits to the columns themselves."""
    with open(path, newline="") as f:
        header, *body = csv.reader(f)
    for edit in edits:
        header, body = edit(header, body)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([header, *body])


def add_boms(path, count: int) -> None:
    """Prefix `count` byte-order marks: one as Excel's "CSV UTF-8" writes,
    two when such a file is read as plain UTF-8 and saved that way again."""
    path.write_bytes(codecs.BOM_UTF8 * count + path.read_bytes())


# Invisible characters, named so that the tests show where they are.
BOM = "\N{BYTE ORDER MARK}"
ZWSP = "\N{ZERO WIDTH SPACE}"


def set_depicts_cells(value: str):
    """A row edit that writes `value` over each `depicts` property cell."""

    def edit(row: dict) -> dict:
        if row["property"] == "depicts":
            row["property"] = value
        return row

    return edit


def drop_column(name: str):
    def edit(header, body):
        at = header.index(name)
        return (
            header[:at] + header[at + 1 :],
            [row[:at] + row[at + 1 :] for row in body],
        )

    return edit


def rename_column(old: str, new: str):
    def edit(header, body):
        return [new if col == old else col for col in header], body

    return edit


def add_columns(**cells: str):
    """A column edit appending `cells`, each holding its value on every row."""

    def edit(header, body):
        return [*header, *cells], [[*row, *cells.values()] for row in body]

    return edit


def pose_as_equivalences(header, body):
    """Add an equivalences sheet's columns, judging every row `same`."""
    return (
        [*header, "entity_id", "qid", "verdict"],
        [[*row, f"e{n}", "Q26", "same"] for n, row in enumerate(body, 1)],
    )


class TestAcceptAllRefusesDepicts:
    """ADR-0005: only a per-row human decision confirms a depicts claim,
    whether face detection or a source's text proposed it, so `accept_all`
    refuses a claims sheet with a depicts row, or without the exactly named
    columns that show them, takes a sheet with any column like `claim_id`
    for a claims sheet, and writes nothing when it refuses (#24)."""

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

    @pytest.mark.parametrize("boms", [0, 1], ids=["plain", "bom"])
    def test_mixed_sheet_is_refused_naming_only_its_depicts_rows(
        self, tmp_path, boms
    ):
        # a byte-order mark, as Excel's "CSV UTF-8" writes one, must hide
        # neither the sheet's kind nor a row
        text = portrait_text()
        date, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        add_boms(review_csv, boms)
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
        "edits",
        [
            pytest.param([drop_column("property")], id="deleted"),
            pytest.param(
                [rename_column("property", "Property")], id="capitalised"
            ),
            pytest.param(
                [rename_column("property", " property")], id="leading-space"
            ),
            pytest.param(
                [rename_column("property", "property ")], id="trailing-space"
            ),
            pytest.param([add_columns(property="")], id="blank-duplicate"),
            pytest.param(
                [drop_column("property"), pose_as_equivalences],
                id="posing-as-equivalences",
            ),
        ],
    )
    def test_claims_sheet_without_one_property_column_is_refused(
        self, tmp_path, edits
    ):
        review_csv = tmp_path / "review.csv"
        export_review_csv(portrait_faces(), review_csv)
        edit_columns(review_csv, *edits)
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="'property' column"):
            accept_all(review_csv, out)

        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    @pytest.mark.parametrize(
        "edits",
        [
            pytest.param([pose_as_equivalences], id="with-property"),
            pytest.param(
                [drop_column("property"), pose_as_equivalences],
                id="property-deleted",
            ),
        ],
    )
    def test_double_bom_disguise_is_refused(self, tmp_path, edits):
        # a claims sheet dressed as an equivalences sheet and saved with two
        # byte-order marks: decoding strips one, so its first column reads
        # BOM + "claim_id", which is not claim_id
        review_csv = tmp_path / "review.csv"
        export_review_csv(portrait_text(), review_csv)
        edit_columns(review_csv, *edits)
        add_boms(review_csv, 2)
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="format character"):
            accept_all(review_csv, out)

        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    @pytest.mark.parametrize(
        "name",
        ["Claim_ID", "claim_id ", "claim" + ZWSP + "_id"],
        ids=["capitalised", "trailing-space", "zero-width-inside"],
    )
    def test_renamed_claim_id_cannot_pass_for_equivalences(
        self, tmp_path, name
    ):
        review_csv = tmp_path / "review.csv"
        export_review_csv(portrait_text(), review_csv)
        edit_columns(
            review_csv, rename_column("claim_id", name), pose_as_equivalences
        )
        sheet = review_csv.read_bytes()
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="named exactly|format character"):
            accept_all(review_csv, out)

        assert not out.exists()
        assert review_csv.read_bytes() == sheet

    @pytest.mark.parametrize(
        "old, new",
        [("property", BOM + "property"), ("notes", "notes" + ZWSP)],
        ids=["bom-inside", "zero-width-in-notes"],
    )
    def test_invisible_character_in_any_column_name_is_refused(
        self, tmp_path, old, new
    ):
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(
            text.model_copy(update={"claims": text.claims[:1]}), review_csv
        )
        edit_columns(review_csv, rename_column(old, new))
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="format character"):
            accept_all(review_csv, out)

        assert not out.exists()

    def test_sheet_of_neither_gate_is_refused(self, tmp_path):
        # a claims sheet that lost its claim_id column: no gate can apply it
        review_csv = tmp_path / "review.csv"
        export_review_csv(portrait_text(), review_csv)
        edit_columns(review_csv, drop_column("claim_id"))
        out = tmp_path / "review.reviewed.csv"

        with pytest.raises(ValueError, match="neither"):
            accept_all(review_csv, out)

        assert not out.exists()

    def test_row_with_an_extra_cell_is_refused_before_writing(self, tmp_path):
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(
            text.model_copy(update={"claims": text.claims[:1]}), review_csv
        )
        edit_columns(
            review_csv, lambda header, body: (header, [[*body[0], "stray"]])
        )
        reviewed = tmp_path / "review.reviewed.csv"
        reviewed.write_text("decided by hand\n")

        with pytest.raises(ValueError, match="more cells than columns"):
            accept_all(review_csv, reviewed)

        assert reviewed.read_text() == "decided by hand\n"

    def test_the_portraits_other_claims_are_still_accepted(self, tmp_path):
        # the refusal keys on the property, not on the photograph
        text = portrait_text()
        dated = text.model_copy(update={"claims": text.claims[:1]})
        review_csv = tmp_path / "review.csv"
        export_review_csv(dated, review_csv)

        result = accept_all(review_csv, tmp_path / "review.reviewed.csv")
        assert (result.mode, result.total, result.approved) == ("claims", 1, 1)

    def test_depicts_row_without_claim_id_is_named_by_position(self, tmp_path):
        # a damaged sheet is still refused; row 2 stands for the portrait's
        # date, whose subject is the scan itself
        sheet = tmp_path / "review.csv"
        sheet.write_text(
            "claim_id,property,decision\n"  # row 1, as a spreadsheet counts
            "0a1b2c3d4e5f,inception,\n"  # row 2
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
    cells say, and any claims sheet shaped like an equivalences sheet, and
    still takes a per-row human approve (#24)."""

    @pytest.mark.parametrize(
        "cell",
        ["inception", "", "P180", ZWSP + "depicts"],
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

    @pytest.mark.parametrize(
        "prior", [None, "decided earlier\n"], ids=["absent", "pre-existing"]
    )
    def test_cli_exits_non_zero_and_writes_no_approved_batch(
        self, tmp_path, prior
    ):
        # the refusal is one line on stderr, not a traceback, and leaves
        # --out as it was (#220)
        text = portrait_text()
        _, *depicts = text.claims
        batch = tmp_path / "batch.json"
        batch.write_text(text.model_dump_json(indent=2))
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(review_csv, set_depicts_cells("inception"))
        reviewed = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed)
        approved = tmp_path / "approved.json"
        if prior is not None:
            approved.write_text(prior)
        args = build_parser().parse_args(
            [
                "ingest",
                "apply-review",
                "--batch",
                str(batch),
                "--csv",
                str(reviewed),
                "--out",
                str(approved),
            ]
        )

        with pytest.raises(SystemExit) as exit_:
            args.func(args)

        message = exit_.value.code
        assert isinstance(message, str)
        assert "\n" not in message
        for claim in depicts:
            assert f"claim {claim_id(claim)}" in message
        if prior is None:
            assert not approved.exists()
        else:
            assert approved.read_text() == prior

    def test_sheet_posing_as_equivalences_gets_bulk_accept_then_refused(
        self, tmp_path
    ):
        # blanked depicts cells under an equivalences sheet's columns: its
        # claim_id column keeps it a claims sheet, so accept-all writes
        # bulk-accept, never the approve an equivalences verdict would get,
        # and apply_review refuses a claims sheet of that shape outright
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(review_csv, set_depicts_cells(""))
        edit_columns(review_csv, pose_as_equivalences)
        reviewed = tmp_path / "review.reviewed.csv"

        assert accept_all(review_csv, reviewed).mode == "claims"
        with open(reviewed, newline="") as f:
            assert {row["decision"] for row in csv.DictReader(f)} == {
                "bulk-accept"
            }
        with pytest.raises(ValueError, match="equivalences sheet"):
            apply_review(text, reviewed)

    @pytest.mark.parametrize(
        "columns",
        [
            {"entity_id": "e1"},
            {"verdict": "same"},
            {"entity_id": "e1", "qid": "Q26", "verdict": "same"},
            {" Verdict": "same"},
            {"verdict" + ZWSP: "same"},
        ],
        ids=["entity_id", "verdict", "all-three", "padded", "zero-width"],
    )
    def test_claims_sheet_with_equivalences_columns_is_refused(
        self, tmp_path, columns
    ):
        # what accept-all's equivalences branch would leave on a disguised
        # claims sheet: approve on every row, the depicts rows included
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_columns(review_csv, add_columns(**columns))
        edit_rows(review_csv, lambda row: {**row, "decision": "approve"})

        with pytest.raises(ValueError, match="equivalences sheet"):
            apply_review(text, review_csv)

    @pytest.mark.parametrize(
        "token",
        ["Bulk-Accept", " bulk-accept ", "BULK-ACCEPT"],
        ids=["title-case", "padded", "upper-case"],
    )
    def test_bulk_accept_is_refused_however_it_is_written(
        self, tmp_path, token
    ):
        text = portrait_text()
        _, *depicts = text.claims
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        edit_rows(review_csv, lambda row: {**row, "decision": token})

        with pytest.raises(ValueError, match="bulk-accept") as refusal:
            apply_review(text, review_csv)

        for claim in depicts:
            assert f"claim {claim_id(claim)}" in str(refusal.value)

    @pytest.mark.parametrize("boms", [0, 1], ids=["plain", "bom"])
    def test_per_row_approve_on_a_depicts_claim_passes(self, tmp_path, boms):
        # the human gate: approve typed on each depicts row, beside a
        # bulk-accept on the rest; a byte-order mark, as Excel's
        # "CSV UTF-8" writes one, must not get in its way
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
        add_boms(review_csv, boms)

        approved, warnings = apply_review(text, review_csv)

        # each depicts claim carries the record of its per-row approval
        # (#226); the bulk-accepted date does not
        date, *depicts = text.claims
        assert approved.claims == [
            date,
            *(approved_on_its_row(claim, text) for claim in depicts),
        ]
        assert approved.claims[0].row_approval is None
        assert warnings == []


# The write path (#226): what apply_review records, the writer requires.


def decide_by_hand(review_csv, decide=lambda row: "approve") -> None:
    """A curator's decision typed on each row of a sheet: `decide(row)`."""
    edit_rows(review_csv, lambda row: {**row, "decision": decide(row)})


def reviewed(batch: ClaimBatch, tmp_path, decide=lambda row: "approve"):
    """`batch` exported for review, decided row by row, and applied."""
    review_csv = tmp_path / "review.csv"
    export_review_csv(batch, review_csv)
    decide_by_hand(review_csv, decide)
    approved, warnings = apply_review(batch, review_csv)
    assert warnings == []
    return approved


class PortraitWiki(FakeWiki):
    """The unit fake, with a document's own date (`inception`, the
    portrait's text's date) in its vocabulary."""

    def vocabulary(self):
        return dict(PROPERTIES) | {"inception": "P92"}, dict(ITEMS)


def wiki_with_portrait() -> FakeWiki:
    """A fake instance holding the portrait's photograph item, and Rahel's
    item under Q28, the QID the portrait's text names her by: so a batch
    retargeted to her would write, were the writer to let it."""
    wiki = PortraitWiki({PORTRAIT_SCAN.sha1: PORTRAIT.qid})
    photograph = wiki.wbi.item.new()
    photograph.id = PORTRAIT.qid
    photograph.write()
    rahel = wiki.wbi.item.new()
    rahel.id = "Q28"
    rahel.labels.set("en", RAHEL)
    rahel.write()
    return wiki


def depicted(wiki: FakeWiki) -> dict[str, object]:
    """The photograph's depicts statements, by the item each depicts."""
    photograph = wiki.wbi.item.get(PORTRAIT.qid)
    return {
        statement.mainsnak.datavalue["value"]["id"]: statement
        for statement in photograph.claims.get(PROPERTIES["depicts"])
    }


class TestRowApprovalRecord:
    """How apply_review records a per-row approval on a claim, and why the
    record changes nothing for a claim without one."""

    def test_apply_review_records_it_on_each_approved_depicts_claim(
        self, tmp_path
    ):
        faces = portrait_faces()
        approved = reviewed(faces, tmp_path)
        assert approved.claims == [
            approved_on_its_row(claim, faces) for claim in faces.claims
        ]
        assert unapproved_depicts(faces) == [
            claim_id(claim) for claim in faces.claims
        ]
        assert unapproved_depicts(approved) == []

    def test_edited_locator_cells_leave_approved_claims_as_batched(
        self, tmp_path
    ):
        faces = portrait_faces()
        review_csv = tmp_path / "review.csv"
        export_review_csv(faces, review_csv)
        edit_rows(
            review_csv,
            lambda row: {
                **row,
                "page": "5",
                "printed_page": "17",
                "region": "1%,2%,3%,4%",
                "decision": "approve",
            },
        )
        approved, warnings = apply_review(faces, review_csv)
        assert warnings == []
        assert approved.claims == [
            approved_on_its_row(claim, faces) for claim in faces.claims
        ]

    def test_record_is_not_content(self):
        faces = portrait_faces()
        claim = faces.claims[0]
        assert claim_id(approved_on_its_row(claim, faces)) == claim_id(claim)

    def test_record_covers_the_manifest_records_its_refs_resolve_through(
        self,
    ):
        faces = portrait_faces_by_label(manifest=True)
        eugen, sebastian = faces.claims
        first, second = faces.entities
        renamed = faces.model_copy(
            update={
                "entities": [first.model_copy(update={"label": RAHEL}), second]
            }
        )
        assert approval_digest(eugen, renamed) != approval_digest(eugen, faces)
        # a record none of the claim's local refs resolves through is not
        assert approval_digest(sebastian, renamed) == approval_digest(
            sebastian, faces
        )
        # and a local ref without a record is bound to having none
        unrecorded = faces.model_copy(update={"entities": []})
        assert approval_digest(eugen, unrecorded) != approval_digest(
            eugen, faces
        )

    @pytest.mark.parametrize(
        "batch",
        [portrait_faces, lambda: portrait_faces_by_label(manifest=True)],
        ids=["qids", "local-refs-with-manifest"],
    )
    def test_record_survives_the_approved_batch_file(self, tmp_path, batch):
        approved = reviewed(batch(), tmp_path)
        again = ClaimBatch.model_validate_json(approved.model_dump_json())
        assert again == approved
        assert unapproved_depicts(again) == []

    def test_unmarked_claims_serialise_as_without_the_field(self):
        # batch.json, approved.json, content hashes and run-item labels of
        # every batch without an approved depicts claim stay as they were
        text = portrait_text()
        dumped = text.model_dump(mode="json")
        assert all("row_approval" not in claim for claim in dumped["claims"])
        assert "row_approval" not in text.model_dump_json()

    def test_extraction_prompts_never_see_it(self):
        # the schema the extraction prompts carry, so their cache keys hold
        assert "row_approval" not in json.dumps(ClaimBatch.model_json_schema())


class TestWriteRefusesUnapprovedDepicts:
    """ADR-0005 at the write path (#226): the writer writes a depicts claim
    only with the per-row approval apply_review records on it, and refuses
    a batch holding any other, before it reads or writes anything, naming
    the claims. A batch without depicts claims is not affected."""

    def test_raw_detect_faces_batch_is_refused(self):
        faces = portrait_faces()
        wiki = wiki_with_portrait()
        writes = wiki.wbi.item.writes

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), faces)

        ids = [claim_id(claim) for claim in faces.claims]
        assert refusal.value.claim_ids == ids
        for cid in ids:
            assert f"claim {cid}" in str(refusal.value)
        # no run item, no minted person, no statement
        assert wiki.wbi.item.writes == writes
        assert depicted(wiki) == {}

    def test_dry_run_refuses_it_too(self):
        wiki = wiki_with_portrait()
        with pytest.raises(UnapprovedDepictsError):
            write_mapped(BatchWriter(wiki, dry_run=True), portrait_faces())

    def test_depicts_a_source_text_proposes_is_refused_alike(self):
        text = portrait_text()
        date, *depicts = text.claims

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki_with_portrait()), text)

        assert refusal.value.claim_ids == [claim_id(c) for c in depicts]
        assert claim_id(date) not in str(refusal.value)

    def test_batch_from_an_approved_sheet_is_written(self, tmp_path):
        approved = reviewed(portrait_faces(), tmp_path)
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved)

        assert result.written == 2
        statements = depicted(wiki)
        assert set(statements) == {"Q26", "Q27"}
        regions = {
            snak.datavalue["value"]
            for statement in statements.values()
            for snak in statement.qualifiers.get(
                PROPERTIES["relative_position_within_image"]
            )
        }
        assert regions == {"10%,10%,20%,50%", "40%,10%,20%,50%"}
        for statement in statements.values():
            assert _cited(statement, "stated_in") == [PORTRAIT.qid]
            assert _cited(statement, "inferred_from_extraction_run") == [
                result.run_qid
            ]
        # and re-writing it is the no-op it is for any reviewed batch
        again = write_mapped(BatchWriter(wiki), approved)
        assert (again.written, again.skipped) == (0, 2)

    def test_mixed_sheet_writes_every_approved_claim(self, tmp_path):
        # depicts approved row by row, the date bulk-accepted beside them
        text = portrait_text()
        review_csv = tmp_path / "review.csv"
        export_review_csv(text, review_csv)
        decide_by_hand(
            review_csv,
            lambda row: (
                "approve" if row["property"] == "depicts" else "bulk-accept"
            ),
        )
        approved, _ = apply_review(text, review_csv)
        wiki = wiki_with_portrait()

        result = write_mapped(
            BatchWriter(wiki),
            approved,
            bulk_accepted=is_bulk_accepted(review_csv),
        )

        assert result.written == 3
        assert set(depicted(wiki)) == {"Q26", "Q28"}

    def test_approval_does_not_survive_an_edit_to_the_claim(self, tmp_path):
        # the second sitter re-identified in the approved batch after review
        approved = reviewed(portrait_faces(), tmp_path)
        eugen, sebastian = approved.claims
        edited = sebastian.model_copy(
            update={"value": ItemValue(value=QidRef(qid="Q28"))}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(
                BatchWriter(wiki),
                approved.model_copy(update={"claims": [eugen, edited]}),
            )

        assert refusal.value.claim_ids == [claim_id(edited)]
        assert depicted(wiki) == {}

    @pytest.mark.parametrize(
        "manifest", [False, True], ids=["refs", "records"]
    )
    def test_sitters_named_by_local_ref_are_written(self, tmp_path, manifest):
        approved = reviewed(portrait_faces_by_label(manifest), tmp_path)
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved)

        assert result.written == 2
        sitters = {
            str(wiki.wbi.item.get(qid).labels.get("en"))
            for qid in depicted(wiki)
        }
        assert sitters == {EUGEN, SEBASTIAN}

    # Edits to an approved batch's entity manifest, as a hand edit to
    # approved.json's `entities` makes them: each retargets the first
    # face's local ref without touching its claim (#226 review).
    MANIFEST_EDITS = {
        # a record where there was none, resolving the ref to an item
        "record-added-with-qid": (
            False,
            lambda records: [
                *records,
                EntityRecord(
                    id="face-person-0", label=RAHEL, kind="human", qid="Q28"
                ),
            ],
        ),
        "record-added-with-label": (
            False,
            lambda records: [
                *records,
                EntityRecord(id="face-person-0", label=RAHEL, kind="human"),
            ],
        ),
        "qid-set": (
            True,
            lambda records: [
                records[0].model_copy(update={"qid": "Q28"}),
                *records[1:],
            ],
        ),
        "label-changed": (
            True,
            lambda records: [
                records[0].model_copy(update={"label": RAHEL}),
                *records[1:],
            ],
        ),
        # the ref then resolves by its own label: it is bound to the
        # record it had, too
        "record-removed": (True, lambda records: records[1:]),
    }

    @pytest.mark.parametrize(
        "manifest, edit", MANIFEST_EDITS.values(), ids=MANIFEST_EDITS.keys()
    )
    def test_approval_does_not_survive_an_edit_to_the_entity_manifest(
        self, tmp_path, manifest, edit
    ):
        approved = reviewed(portrait_faces_by_label(manifest), tmp_path)
        edited = approved.model_copy(
            update={"entities": edit(approved.entities)}
        )
        wiki = wiki_with_portrait()
        writes = wiki.wbi.item.writes

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), edited)

        # the second sitter's claim, whose record is untouched, keeps its
        # approval; the batch is refused whole all the same
        assert refusal.value.claim_ids == [claim_id(approved.claims[0])]
        assert wiki.wbi.item.writes == writes
        assert depicted(wiki) == {}

    # A ref to the first face's local id, which has no record, that differs
    # from the face's own ref in one field (#258). The writer makes the id
    # into an item from the first ref it meets, so a claim naming it ahead
    # of the approved ones decides what the first depicts claim depicts.
    OTHER_REFS = {
        # another person's name: the writer would make the id her item
        "label": {"label": RAHEL},
        # the item minted for him would take this description instead
        "description": {"label": EUGEN, "description": "master hatter"},
        # and this kind, or the writer would record it on the item it
        # finds by his label
        "kind": {"label": EUGEN, "instance_of": "unclassified_entity"},
    }

    @pytest.mark.parametrize(
        "differs", OTHER_REFS.values(), ids=OTHER_REFS.keys()
    )
    def test_approval_does_not_survive_another_ref_to_its_local_id(
        self, tmp_path, differs
    ):
        approved = reviewed(portrait_faces_by_label(), tmp_path)
        eugen, sebastian = approved.claims
        retargeted = approved.model_copy(
            update={"claims": [muckel(**differs), eugen, sebastian]}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), retargeted)

        assert refusal.value.claim_ids == [claim_id(eugen)]
        assert depicted(wiki) == {}

    def test_a_matching_ref_to_its_local_id_leaves_it_approved(self, tmp_path):
        # the sheet names the first sitter twice by one local ref: once as
        # depicted, once by the name the portrait gives him
        faces = portrait_faces_by_label()
        both = faces.model_copy(
            update={"claims": [muckel(EUGEN), *faces.claims]}
        )
        approved = reviewed(both, tmp_path)
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved)

        assert result.written == 3
        eugen = result.created_items["face-person-0"]
        assert eugen in depicted(wiki)
        (name,) = wiki.wbi.item.get(eugen).claims.get(
            PROPERTIES["referred_to_as"]
        )
        assert name.mainsnak.datavalue["value"]["text"] == "Muckel"

    # A depicts claim's qualifier can name an item by a local ref too: the
    # portrait's sitters depicted at the studio's town, which the mount's
    # own claim about the studio names by the same local id (#258).

    def test_a_qualifiers_local_ref_is_written_as_approved(self, tmp_path):
        faces = portrait_faces_in(town())
        approved = reviewed(
            faces.model_copy(
                update={"claims": [studio_in(town()), *faces.claims]}
            ),
            tmp_path,
        )
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved)

        assert result.written == 3
        peiltal = result.created_items["studio-town"]
        assert str(wiki.wbi.item.get(peiltal).labels.get("en")) == PEILTAL
        for statement in depicted(wiki).values():
            (place,) = statement.qualifiers.get(PROPERTIES["location"])
            assert place.datavalue["value"]["id"] == peiltal

    def test_approval_does_not_survive_another_ref_to_its_qualifiers_local_id(
        self, tmp_path
    ):
        # the mount's claim, added ahead of the approved depicts claims,
        # names the studio's town by the same local id as another town:
        # the writer would place both sitters there
        approved = reviewed(portrait_faces_in(town()), tmp_path)
        retargeted = approved.model_copy(
            update={"claims": [studio_in(town(KREDFELD)), *approved.claims]}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), retargeted)

        assert refusal.value.claim_ids == [
            claim_id(claim) for claim in approved.claims
        ]
        assert depicted(wiki) == {}

    def test_approval_covers_the_record_a_qualifiers_ref_resolves_through(
        self, tmp_path
    ):
        # with a manifest record for the town, the approval binds it: a
        # record renamed to another town after review voids it
        faces = portrait_faces_in(town())
        record = EntityRecord(id="studio-town", label=PEILTAL, kind="place")
        approved = reviewed(
            faces.model_copy(update={"entities": [record]}), tmp_path
        )
        assert unapproved_depicts(approved) == []
        moved = approved.model_copy(
            update={
                "entities": [record.model_copy(update={"label": KREDFELD})]
            }
        )

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki_with_portrait()), moved)

        assert refusal.value.claim_ids == [
            claim_id(claim) for claim in approved.claims
        ]

    def test_batch_without_depicts_is_written_unreviewed(self):
        # as the scratch write stage writes a batch, without its depicts
        # claims (ADR-0025): the gate leaves such a batch alone
        text = portrait_text()
        dated = text.model_copy(update={"claims": text.claims[:1]})
        result = write_mapped(BatchWriter(wiki_with_portrait()), dated)
        assert result.written == 1


class TestOneEntityRecordLookup:
    """The writer and the depicts gate read a batch's entity manifest
    through one lookup (`entity_records`, #258): the record a depicts
    claim's approval binds is the record the writer resolves its local
    ref through, however the lookup picks among records that share an
    id. A record it does not pick leaves the approval as it is, resolves
    no local ref, and adds no alias or kind; it is still part of the
    batch file, which the extraction-run item's fingerprint covers
    whole."""

    @staticmethod
    def two_records_for_the_first_face(tmp_path) -> ClaimBatch:
        """The portrait's faces approved row by row, with two manifest
        records under the first face's local id (which manifest
        validation would refuse), each naming a different item. The
        second face's record names Sebastian's item too: a batch whose
        mentions share a local id cannot be mapped (#109), so every local
        entity of it is one an approved equivalence names."""
        faces = portrait_faces_by_label(manifest=True)
        first, second = faces.entities
        doubled = faces.model_copy(
            update={
                "entities": [
                    first.model_copy(update={"qid": "Q26"}),
                    first.model_copy(update={"label": RAHEL, "qid": "Q28"}),
                    second.model_copy(update={"qid": "Q29"}),
                ]
            }
        )
        return reviewed(doubled, tmp_path)

    @staticmethod
    def wiki() -> FakeWiki:
        """The portrait's instance, with Eugen's item under Q26 and
        Sebastian's under Q29 too."""
        wiki = wiki_with_portrait()
        for qid, label in (("Q26", EUGEN), ("Q29", SEBASTIAN)):
            item = wiki.wbi.item.new()
            item.id = qid
            item.labels.set("en", label)
            item.write()
        return wiki

    @staticmethod
    def edited(batch: ClaimBatch, record: EntityRecord, **update):
        """`batch` with `record` alone, of its entities, changed."""
        return batch.model_copy(
            update={
                "entities": [
                    entity.model_copy(update=update)
                    if entity is record
                    else entity
                    for entity in batch.entities
                ]
            }
        )

    def test_the_approval_binds_the_record_the_writer_resolves_through(
        self, tmp_path
    ):
        approved = self.two_records_for_the_first_face(tmp_path)
        picked = entity_records(approved)["face-person-0"]
        wiki = self.wiki()

        write_mapped(BatchWriter(wiki), approved)

        assert picked.qid in depicted(wiki)
        # an edit to the picked record voids the approval
        with pytest.raises(UnapprovedDepictsError):
            write_mapped(
                BatchWriter(self.wiki()),
                self.edited(approved, picked, qid="Q27"),
            )

    def test_passed_over_record_resolves_no_local_ref_adds_no_alias_or_kind(
        self, tmp_path
    ):
        approved = self.two_records_for_the_first_face(tmp_path)
        picked = entity_records(approved)["face-person-0"]
        (passed_over,) = [
            record
            for record in approved.entities
            if record.id == "face-person-0" and record is not picked
        ]
        # were it read, it would name Rahel, and give her item its aliases
        # and a kind of its own
        edited = self.edited(
            approved,
            passed_over,
            qid="Q28",
            aliases=["Muckel"],
            kind="unclassified_entity",
        )
        assert unapproved_depicts(edited) == []
        written, run_labels = {}, {}
        for name, batch in (("approved", approved), ("edited", edited)):
            wiki = self.wiki()
            result = write_mapped(BatchWriter(wiki), batch)
            rahel = wiki.wbi.item.get("Q28")
            written[name] = (
                set(depicted(wiki)),
                rahel.aliases.get("en"),
                sorted(
                    kind.mainsnak.datavalue["value"]["id"]
                    for kind in rahel.claims.get(PROPERTIES["instance_of"])
                ),
            )
            run_labels[name] = str(
                wiki.wbi.item.get(result.run_qid).labels.get("en")
            )
        assert written["edited"] == written["approved"]
        # the run item's label carries the fingerprint of the whole batch
        # file, the record passed over included
        assert run_labels["edited"] != run_labels["approved"]


class TestIngestWriteRefusesUnapprovedDepicts:
    """The same gate through the CLI: `yizkr ingest write --batch`."""

    @staticmethod
    def run(monkeypatch, wiki: FakeWiki, *argv) -> None:
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        args = build_parser().parse_args([str(arg) for arg in argv])
        args.func(args)

    def test_raw_detect_faces_batch_is_refused(self, tmp_path, monkeypatch):
        faces_json = tmp_path / "faces-batch.json"
        faces_json.write_text(portrait_faces().model_dump_json(indent=2))
        wiki = wiki_with_portrait()

        with pytest.raises(
            SystemExit, match="carry no per-row human approval"
        ):
            self.run(
                monkeypatch, wiki, "ingest", "write", "--batch", faces_json
            )

        assert depicted(wiki) == {}

    def approve_by_hand(self, monkeypatch, wiki, tmp_path, batch):
        """`batch` as detect-faces writes it, exported for review, decided
        row by row and applied, each step reading the file the one before
        wrote: the path of the approved batch file."""
        faces_json = tmp_path / "faces-batch.json"
        faces_json.write_text(batch.model_dump_json(indent=2))
        review_csv = tmp_path / "review.csv"
        approved_json = tmp_path / "approved.json"
        self.run(
            monkeypatch,
            wiki,
            *("ingest", "export-review", "--batch", faces_json),
            *("--out", review_csv),
        )
        decide_by_hand(review_csv)
        self.run(
            monkeypatch,
            wiki,
            *("ingest", "apply-review", "--batch", faces_json),
            *("--csv", review_csv, "--out", approved_json),
        )
        return approved_json

    def test_batch_apply_review_writes_is_written(self, tmp_path, monkeypatch):
        wiki = wiki_with_portrait()
        approved_json = self.approve_by_hand(
            monkeypatch, wiki, tmp_path, portrait_faces()
        )

        self.run(
            monkeypatch, wiki, "ingest", "write", "--batch", approved_json
        )

        assert set(depicted(wiki)) == {"Q26", "Q27"}

    def test_approved_file_retargeted_by_its_entities_is_refused(
        self, tmp_path, monkeypatch
    ):
        # only the approved file's top-level `entities` edited after
        # review: a record, with the QID that records a resolution, for
        # the first sitter's local ref (#226 review)
        wiki = wiki_with_portrait()
        approved_json = self.approve_by_hand(
            monkeypatch, wiki, tmp_path, portrait_faces_by_label()
        )
        edited = json.loads(approved_json.read_text())
        edited["entities"].append(
            {
                "id": "face-person-0",
                "label": RAHEL,
                "kind": "human",
                "qid": "Q28",
            }
        )
        approved_json.write_text(json.dumps(edited, indent=2))

        with pytest.raises(
            SystemExit, match="carry no per-row human approval"
        ):
            self.run(
                monkeypatch, wiki, "ingest", "write", "--batch", approved_json
            )

        assert depicted(wiki) == {}
