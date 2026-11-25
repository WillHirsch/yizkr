"""Offline tests for face proposals (pytest; no detector dependency)."""

import json
import sys
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki, _cited
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
    approval_digest,
    claim_id,
    entity_records,
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
    """`claim` of `batch` with the row approval the writer requires on a
    depicts claim (review.approval_digest)."""
    return claim.model_copy(
        update={"row_approval": approval_digest(claim, batch)}
    )


def approved(batch: ClaimBatch) -> ClaimBatch:
    """`batch` with each of its depicts claims approved on its own row.
    Nothing in the package records such an approval, so the writer refuses
    every depicts claim it is given; these tests build it to keep the gate
    covered while the map-bound writer applies it (#893)."""
    return batch.model_copy(
        update={
            "claims": [
                approved_on_its_row(claim, batch)
                if claim.property == "depicts"
                else claim
                for claim in batch.claims
            ]
        }
    )


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
    photograph.claims.add(
        String(prop_nr=PROPERTIES["media_sha1"], value=PORTRAIT_SCAN.sha1)
    )
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
    """What a depicts claim's per-row approval binds, and why the record
    changes nothing for a claim without one."""

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
    def test_record_survives_the_batch_file(self, batch):
        recorded = approved(batch())
        again = ClaimBatch.model_validate_json(recorded.model_dump_json())
        assert again == recorded
        assert unapproved_depicts(again) == []

    def test_unmarked_claims_serialise_as_without_the_field(self):
        # batch.json, content hashes and run-item labels of every batch
        # without an approved depicts claim stay as they were
        text = portrait_text()
        dumped = text.model_dump(mode="json")
        assert all("row_approval" not in claim for claim in dumped["claims"])
        assert "row_approval" not in text.model_dump_json()

    def test_extraction_prompts_never_see_it(self):
        # the schema the extraction prompts carry, so their cache keys hold
        assert "row_approval" not in json.dumps(ClaimBatch.model_json_schema())


class TestWriteRefusesUnapprovedDepicts:
    """ADR-0005 at the write path (#226): the writer writes a depicts claim
    only with a per-row approval bound to it (review.approval_digest), and
    refuses a batch holding any other, before it reads or writes anything,
    naming the claims. A batch without depicts claims is not affected."""

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

    def test_a_batch_approved_row_by_row_is_written(self):
        faces = approved(portrait_faces())
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), faces)

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
        again = write_mapped(BatchWriter(wiki), faces)
        assert (again.written, again.skipped) == (0, 2)

    def test_a_mixed_batch_writes_every_claim(self):
        # the portrait's text: its date, and its depicts claims each with a
        # bound record of approval
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved(portrait_text()))

        assert result.written == 3
        assert set(depicted(wiki)) == {"Q26", "Q28"}

    def test_approval_does_not_survive_an_edit_to_the_claim(self):
        # the second sitter re-identified in the batch after its approval
        faces = approved(portrait_faces())
        eugen, sebastian = faces.claims
        edited = sebastian.model_copy(
            update={"value": ItemValue(value=QidRef(qid="Q28"))}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(
                BatchWriter(wiki),
                faces.model_copy(update={"claims": [eugen, edited]}),
            )

        assert refusal.value.claim_ids == [claim_id(edited)]
        assert depicted(wiki) == {}

    @pytest.mark.parametrize(
        "manifest", [False, True], ids=["refs", "records"]
    )
    def test_sitters_named_by_local_ref_are_written(self, manifest):
        wiki = wiki_with_portrait()

        result = write_mapped(
            BatchWriter(wiki), approved(portrait_faces_by_label(manifest))
        )

        assert result.written == 2
        sitters = {
            str(wiki.wbi.item.get(qid).labels.get("en"))
            for qid in depicted(wiki)
        }
        assert sitters == {EUGEN, SEBASTIAN}

    # Edits to an approved batch's entity manifest, as a hand edit to the
    # batch file's `entities` makes them: each retargets the first face's
    # local ref without touching its claim (#226 review).
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
        self, manifest, edit
    ):
        faces = approved(portrait_faces_by_label(manifest))
        edited = faces.model_copy(update={"entities": edit(faces.entities)})
        wiki = wiki_with_portrait()
        writes = wiki.wbi.item.writes

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), edited)

        # the second sitter's claim, whose record is untouched, keeps its
        # approval; the batch is refused whole all the same
        assert refusal.value.claim_ids == [claim_id(faces.claims[0])]
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
        self, differs
    ):
        faces = approved(portrait_faces_by_label())
        eugen, sebastian = faces.claims
        retargeted = faces.model_copy(
            update={"claims": [muckel(**differs), eugen, sebastian]}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), retargeted)

        assert refusal.value.claim_ids == [claim_id(eugen)]
        assert depicted(wiki) == {}

    def test_a_matching_ref_to_its_local_id_leaves_it_approved(self):
        # the batch names the first sitter twice by one local ref: once as
        # depicted, once by the name the portrait gives him
        faces = portrait_faces_by_label()
        both = faces.model_copy(
            update={"claims": [muckel(EUGEN), *faces.claims]}
        )
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), approved(both))

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

    def test_a_qualifiers_local_ref_is_written_as_approved(self):
        faces = portrait_faces_in(town())
        with_studio = approved(
            faces.model_copy(
                update={"claims": [studio_in(town()), *faces.claims]}
            )
        )
        wiki = wiki_with_portrait()

        result = write_mapped(BatchWriter(wiki), with_studio)

        assert result.written == 3
        peiltal = result.created_items["studio-town"]
        assert str(wiki.wbi.item.get(peiltal).labels.get("en")) == PEILTAL
        for statement in depicted(wiki).values():
            (place,) = statement.qualifiers.get(PROPERTIES["location"])
            assert place.datavalue["value"]["id"] == peiltal

    def test_approval_does_not_survive_another_ref_to_its_qualifiers_local_id(
        self,
    ):
        # the mount's claim, added ahead of the approved depicts claims,
        # names the studio's town by the same local id as another town:
        # the writer would place both sitters there
        faces = approved(portrait_faces_in(town()))
        retargeted = faces.model_copy(
            update={"claims": [studio_in(town(KREDFELD)), *faces.claims]}
        )
        wiki = wiki_with_portrait()

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki), retargeted)

        assert refusal.value.claim_ids == [
            claim_id(claim) for claim in faces.claims
        ]
        assert depicted(wiki) == {}

    def test_approval_covers_the_record_a_qualifiers_ref_resolves_through(
        self,
    ):
        # with a manifest record for the town, the approval binds it: a
        # record renamed to another town after the approval voids it
        record = EntityRecord(id="studio-town", label=PEILTAL, kind="place")
        faces = approved(
            portrait_faces_in(town()).model_copy(update={"entities": [record]})
        )
        assert unapproved_depicts(faces) == []
        moved = faces.model_copy(
            update={
                "entities": [record.model_copy(update={"label": KREDFELD})]
            }
        )

        with pytest.raises(UnapprovedDepictsError) as refusal:
            write_mapped(BatchWriter(wiki_with_portrait()), moved)

        assert refusal.value.claim_ids == [
            claim_id(claim) for claim in faces.claims
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
    def two_records_for_the_first_face() -> ClaimBatch:
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
        return approved(doubled)

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
        self,
    ):
        faces = self.two_records_for_the_first_face()
        picked = entity_records(faces)["face-person-0"]
        wiki = self.wiki()

        write_mapped(BatchWriter(wiki), faces)

        assert picked.qid in depicted(wiki)
        # an edit to the picked record voids the approval
        with pytest.raises(UnapprovedDepictsError):
            write_mapped(
                BatchWriter(self.wiki()),
                self.edited(faces, picked, qid="Q27"),
            )

    def test_passed_over_record_resolves_no_local_ref_adds_no_alias_or_kind(
        self,
    ):
        faces = self.two_records_for_the_first_face()
        picked = entity_records(faces)["face-person-0"]
        (passed_over,) = [
            record
            for record in faces.entities
            if record.id == "face-person-0" and record is not picked
        ]
        # were it read, it would name Rahel, and give her item its aliases
        # and a kind of its own
        edited = self.edited(
            faces,
            passed_over,
            qid="Q28",
            aliases=["Muckel"],
            kind="unclassified_entity",
        )
        assert unapproved_depicts(edited) == []
        written, run_labels = {}, {}
        for name, batch in (("approved", faces), ("edited", edited)):
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
