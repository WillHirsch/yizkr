"""Live: the write path writes a depicts claim only when a curator approved
it on its own review-sheet row (ADR-0005, #226).

The example universe's 1890s studio portrait of the Löbig family is
registered under a test title, and two faces on it are matched to two of
its sitters, enrolled in a gallery by label only. The raw detect-faces
batch is refused whole, through the writer and through `yizkr ingest
write`, before anything is written. The same batch decided row by row on
a review sheet is written as depicts statements carrying the face's
region and both references, the photograph cited by the SHA-1 of its
bytes (#102). The photograph's bytes, its title and the sitters' labels
carry the session's marker (conftest.fixture_mark), so each session
meets the fixture as a first write: depicts statements, sitters or a run
item that an earlier session wrote, a live mutant's run included, never
meet it. Within the session the labels are fixed, so a re-run reuses the
items, and the reviewed write then adds nothing.
"""

from __future__ import annotations

import csv

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import run_label, write_mapped
from tests.integration.conftest import fictional_png, fixture_mark
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.cli import build_parser
from yizkr.pipeline import faces, writer
from yizkr.pipeline.faces import (
    DetectedFace,
    DetectionResult,
    Gallery,
    PixelBox,
    proposals_to_batch,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review import apply_review, claim_id, export_review_csv
from yizkr.pipeline.writer import (
    BatchWriter,
    UnapprovedDepictsError,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0005: the Löbig family's 1890s studio portrait, two faces on it "
    "matched to Eugen and Sebastian, refused raw and written reviewed"
)
MARK = fixture_mark(FIXTURE, faces, writer)
PORTRAIT = u.by_key()["studio-photo-1890s"]
PORTRAIT_TITLE = f"Integration Test {PORTRAIT.title} {MARK}"
PORTRAIT_BYTES = fictional_png(f"yizkr live test #226: the portrait {MARK}")
SITTERS = [
    f"Integration Test {u.by_key()[key].label} {MARK}"
    for key in ("eugen-loebig", "sebastian-loebig")
]
# each face on the 1000x500 scan, as a region string
REGIONS = ["10%,10%,20%,50%", "40%,10%,20%,50%"]

# unit-normed toy embeddings, one per sitter
DIM = 8
EMBEDDINGS = [
    [1.0] + [0.0] * (DIM - 1),
    [0.0, 1.0] + [0.0] * (DIM - 2),
]


@pytest.fixture(scope="module")
def portrait(client, tmp_path_factory):
    media = tmp_path_factory.mktemp("media") / f"portrait-{MARK}.png"
    media.write_bytes(PORTRAIT_BYTES)
    return register_artifact(
        client,
        media_path=media,
        artifact_type="photograph",
        title=PORTRAIT_TITLE,
        description="written by tests/integration; safe to delete",
    )


@pytest.fixture(scope="module")
def raw_faces(portrait) -> ClaimBatch:
    """The detect-faces batch: one depicts proposal per matched face."""
    gallery = Gallery()
    for label, embedding in zip(SITTERS, EMBEDDINGS, strict=True):
        gallery.enroll(label=label, embedding=embedding)
    detection = DetectionResult(
        photo_file=f"portrait-{MARK}.png",
        photo_sha1=ArtifactRef.from_bytes(PORTRAIT_BYTES).sha1,
        artifact_qid=portrait.artifact_qid,
        image_width=1000,
        image_height=500,
        model_identifier="integration-test-detector",
        faces=[
            DetectedFace(
                index=index,
                bbox=PixelBox(
                    x=100 + 300 * index, y=50, width=200, height=250
                ),
                detection_score=0.99,
                embedding=embedding,
            )
            for index, embedding in enumerate(EMBEDDINGS)
        ],
    )
    batch, unmatched = proposals_to_batch(detection, gallery)
    assert (len(batch.claims), unmatched) == (2, [])
    return batch


def last_revision(client, qid: str) -> int:
    info = client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": "info"}
    )
    return info["entities"][qid]["lastrevid"]


def run_item_label(batch: ClaimBatch) -> str:
    """The label the writer gives the batch's extraction-run item, with the
    map of the batch alone (write_mapped)."""
    return run_label(batch)


def nothing_written(client, portrait, batch: ClaimBatch):
    """A check that nothing was written since it was made: the photograph
    unedited, no sitter item minted, no run item for `batch`."""
    revision = last_revision(client, portrait.artifact_qid)
    sitters = {label: client.find_item_by_label(label) for label in SITTERS}

    def check() -> None:
        assert last_revision(client, portrait.artifact_qid) == revision
        assert {
            label: client.find_item_by_label(label) for label in SITTERS
        } == sitters
        assert client.find_item_by_label(run_item_label(batch)) is None

    return check


def test_raw_detect_faces_batch_is_refused(client, portrait, raw_faces):
    check = nothing_written(client, portrait, raw_faces)

    with pytest.raises(UnapprovedDepictsError) as refusal:
        write_mapped(BatchWriter(client), raw_faces)

    assert refusal.value.claim_ids == [
        claim_id(claim) for claim in raw_faces.claims
    ]
    check()


def test_ingest_write_refuses_the_raw_batch_file(
    client, portrait, raw_faces, tmp_path
):
    faces_json = tmp_path / "faces-batch.json"
    faces_json.write_text(raw_faces.model_dump_json(indent=2))
    check = nothing_written(client, portrait, raw_faces)
    args = build_parser().parse_args(
        ["ingest", "write", "--batch", str(faces_json)]
    )

    with pytest.raises(UnapprovedDepictsError):
        args.func(args)

    check()


def depicts_statements(client, qid: str) -> dict[str, dict]:
    """The item's depicts statements, by the item each depicts."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["depicts"],
        }
    )
    return {
        claim["mainsnak"]["datavalue"]["value"]["id"]: claim
        for claims in data["claims"].values()
        for claim in claims
    }


def depicts_ids(client, qid: str) -> list[str]:
    """The ids of the item's depicts statements, one for each."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["depicts"],
        }
    )
    return [
        claim["id"] for claims in data["claims"].values() for claim in claims
    ]


def test_batch_decided_row_by_row_is_written(
    client, portrait, raw_faces, tmp_path
):
    review_csv = tmp_path / "review.csv"
    export_review_csv(raw_faces, review_csv)
    with open(review_csv, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = [{**row, "decision": "approve"} for row in reader]
    with open(review_csv, "w", newline="") as f:
        sheet = csv.DictWriter(f, fieldnames=fieldnames)
        sheet.writeheader()
        sheet.writerows(rows)
    approved, warnings = apply_review(raw_faces, review_csv)
    assert warnings == []

    result = write_mapped(BatchWriter(client), approved)

    # the session's first reviewed write writes both
    assert (result.written, result.skipped) == (2, 0)
    properties, _ = client.vocabulary()
    statements = depicts_statements(client, portrait.artifact_qid)
    for index, region in enumerate(REGIONS):
        sitter = result.created_items[f"face-person-{index}"]
        assert client.find_item_by_label(SITTERS[index]) == sitter
        statement = statements[sitter]
        assert statement["rank"] == "normal"
        assert [
            snak["datavalue"]["value"]
            for snak in statement["qualifiers"][
                properties["relative_position_within_image"]
            ]
        ] == [region]
        cited = {
            (pid, snak["datavalue"]["value"].get("id"))
            for reference in statement["references"]
            for pid, snaks in reference["snaks"].items()
            for snak in snaks
            if isinstance(snak["datavalue"]["value"], dict)
        }
        assert (properties["stated_in"], portrait.artifact_qid) in cited
        assert properties["inferred_from_extraction_run"] in {
            pid for pid, _ in cited
        }

    # written again, the reviewed batch finds both sitters and both
    # statements, and adds nothing: no sitter minted again, no statement
    # duplicated (the statements are counted, not keyed by sitter)
    before = depicts_statements(client, portrait.artifact_qid)
    count = len(depicts_ids(client, portrait.artifact_qid))
    again = write_mapped(BatchWriter(client), approved)
    assert (again.written, again.skipped) == (0, 2)
    assert {
        f"face-person-{index}": again.created_items[f"face-person-{index}"]
        for index in range(len(REGIONS))
    } == {
        f"face-person-{index}": result.created_items[f"face-person-{index}"]
        for index in range(len(REGIONS))
    }
    assert depicts_statements(client, portrait.artifact_qid) == before
    assert len(depicts_ids(client, portrait.artifact_qid)) == count == 2
