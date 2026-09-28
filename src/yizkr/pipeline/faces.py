"""Face proposals (W1 minimal, ADR-0005): detection -> review -> depicts.

A face proposal is an ordinary claim — ``depicts`` on the photograph
item, the person as value, a ``relative position within image``
qualifier, and the photograph as its own source, cited by the SHA-1 of
its bytes as every source is (an artifact asserts whom it depicts;
docs/MODELING.md) — so proposals ride the SAME review flow as
extracted claims: export-review -> human decision -> apply-review ->
write. Detection output never touches the graph
directly; only human-confirmed rows become statements, with dual
provenance (stated in -> photograph + region; inferred from extraction
run -> the detection run).

Workflow ("initial manual inputs", R5):

1. ``detect-faces`` runs a detector over a registered photograph. Faces
   are matched against a gallery of curator-enrolled embeddings;
   matches become claims in a batch, every face (matched or not) is
   recorded with its embedding in a detections file.
2. ``enroll-face`` states an identity for a detected face; its
   embedding joins the gallery. This is a curator act.
3. ``match-faces`` re-matches saved detections against the grown
   gallery without recomputing embeddings.
4. The batch joins the normal review flow.

Engines implement `FaceDetector`; `InsightFaceDetector` (InsightFace-
class model, ADR-0005) is provided and entirely optional — everything
runs offline from a saved detections file. Detection runs locally;
nothing leaves the machine (privacy first).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    ItemValue,
    LocalRef,
    QidRef,
    Qualifier,
    RegionPercent,
    SourceReference,
    StringValue,
)
from yizkr.pipeline.extract import run_metadata

DEFAULT_THRESHOLD = 0.4  # cosine similarity on unit-normed embeddings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PixelBox(StrictModel):
    """Face bounding box in pixels (top-left origin)."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DetectedFace(StrictModel):
    index: int = Field(ge=0)
    bbox: PixelBox
    detection_score: float = Field(ge=0, le=1)
    embedding: list[float] = Field(min_length=8)


class DetectionResult(StrictModel):
    """The faces a detector found on one photograph. `artifact_qid` is the
    photograph's item, the subject of its depicts claims; `photo_sha1` is
    the SHA-1 of the photograph's bytes, by which those claims cite it as
    their source."""

    photo_file: str
    photo_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    artifact_qid: str = Field(pattern=r"^Q[1-9]\d*$")
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    model_identifier: str = Field(min_length=1)
    faces: list[DetectedFace] = []


class GalleryPerson(StrictModel):
    label: str = Field(min_length=1)
    qid: str | None = Field(default=None, pattern=r"^Q[1-9]\d*$")
    embeddings: list[list[float]] = []


class Gallery(StrictModel):
    """Curator-enrolled face embeddings; the R5 'initial manual inputs'."""

    persons: list[GalleryPerson] = []

    @classmethod
    def load(cls, path: Path) -> Gallery:
        if not path.exists():
            return cls()
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    def enroll(
        self, label: str, embedding: list[float], qid: str | None = None
    ) -> GalleryPerson:
        for person in self.persons:
            if (qid and person.qid == qid) or person.label == label:
                person.embeddings.append(embedding)
                if qid and not person.qid:
                    person.qid = qid
                return person
        person = GalleryPerson(label=label, qid=qid, embeddings=[embedding])
        self.persons.append(person)
        return person


# ---------------------------------------------------------------------------
# Geometry and matching (pure python — gallery scale is a family archive)
# ---------------------------------------------------------------------------


def to_region(
    bbox: PixelBox, image_width: int, image_height: int
) -> RegionPercent:
    """Pixel box -> percentage region, clamped inside the frame.

    Detectors may return boxes that poke past the frame edge; the region
    is clipped to the image, never widened.
    """
    x = min(max(bbox.x / image_width * 100, 0), 99.99)
    y = min(max(bbox.y / image_height * 100, 0), 99.99)
    width = min(bbox.width / image_width * 100, 100 - x)
    height = min(bbox.height / image_height * 100, 100 - y)
    return RegionPercent(
        x=round(x, 2),
        y=round(y, 2),
        width=round(max(width, 0.01), 2),
        height=round(max(height, 0.01), 2),
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def best_match(
    embedding: list[float], gallery: Gallery, threshold: float
) -> tuple[GalleryPerson, float] | None:
    """Best gallery person by max cosine over their enrolled embeddings."""
    best: tuple[GalleryPerson, float] | None = None
    for person in gallery.persons:
        for candidate in person.embeddings:
            score = cosine_similarity(embedding, candidate)
            if best is None or score > best[1]:
                best = (person, score)
    if best is None or best[1] < threshold:
        return None
    return best


# ---------------------------------------------------------------------------
# Proposals -> claim batch (the bridge into the shared review flow)
# ---------------------------------------------------------------------------


def proposals_to_batch(
    detection: DetectionResult,
    gallery: Gallery,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[ClaimBatch, list[int]]:
    """(claim batch of matched faces, indices of unmatched faces).

    Unmatched faces produce no claim — an unidentified face has no
    person to depict. They stay in the detections file for enrolment.
    """
    claims: list[Claim] = []
    unmatched: list[int] = []
    for face in detection.faces:
        match = best_match(face.embedding, gallery, threshold)
        if match is None:
            unmatched.append(face.index)
            continue
        person, score = match
        region = to_region(
            face.bbox, detection.image_width, detection.image_height
        )
        if person.qid:
            person_ref = QidRef(qid=person.qid)
        else:
            person_ref = LocalRef(
                local_id=f"face-person-{face.index}",
                label=person.label,
                instance_of="human",
            )
        claims.append(
            Claim(
                subject=QidRef(qid=detection.artifact_qid),
                property="depicts",
                value=ItemValue(value=person_ref),
                qualifiers=[
                    Qualifier(
                        property="relative_position_within_image",
                        value=StringValue(value=region.as_wikibase_string()),
                    )
                ],
                source=SourceReference(
                    artifact=ArtifactRef(sha1=detection.photo_sha1),
                    region=region,
                ),
                confidence=round(min(max(score, 0.0), 1.0), 3),
            )
        )
    batch = ClaimBatch(
        extraction_run=run_metadata(
            f"face-detection:{detection.model_identifier}"
        ),
        claims=claims,
    )
    return batch, unmatched


# ---------------------------------------------------------------------------
# Detection engines
# ---------------------------------------------------------------------------


class FaceDetector(Protocol):
    model_identifier: str

    def detect(self, photo: Path, artifact_qid: str) -> DetectionResult: ...


class InsightFaceDetector:
    """InsightFace-class detector+embedder (ADR-0005 W1). Local inference.

    Optional heavyweight dependency: pip install insightface
    onnxruntime opencv-python-headless. The model pack (default
    buffalo_l) is downloaded to ~/.insightface on first use.
    """

    def __init__(self, model_pack: str = "buffalo_l") -> None:
        try:
            import cv2  # noqa: F401
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise RuntimeError(
                "InsightFace engine needs: pip install insightface "
                "onnxruntime opencv-python-headless"
            ) from exc
        self._app = FaceAnalysis(
            name=model_pack, allowed_modules=["detection", "recognition"]
        )
        self._app.prepare(ctx_id=-1)  # CPU
        self.model_identifier = f"insightface-{model_pack}"

    def detect(self, photo: Path, artifact_qid: str) -> DetectionResult:
        import cv2

        image = cv2.imread(str(photo))
        if image is None:
            raise ValueError(f"could not read image {photo}")
        height, width = image.shape[:2]
        faces = []
        for index, face in enumerate(self._app.get(image)):
            x1, y1, x2, y2 = (int(v) for v in face.bbox)
            x1, y1 = max(x1, 0), max(y1, 0)
            faces.append(
                DetectedFace(
                    index=index,
                    bbox=PixelBox(
                        x=x1,
                        y=y1,
                        width=max(x2 - x1, 1),
                        height=max(y2 - y1, 1),
                    ),
                    detection_score=round(
                        min(max(float(face.det_score), 0), 1), 3
                    ),
                    embedding=[float(v) for v in face.normed_embedding],
                )
            )
        return DetectionResult(
            photo_file=photo.name,
            photo_sha1=ArtifactRef.from_path(photo).sha1,
            artifact_qid=artifact_qid,
            image_width=width,
            image_height=height,
            model_identifier=self.model_identifier,
            faces=faces,
        )


def make_detector(engine: str) -> FaceDetector:
    if engine == "insightface":
        return InsightFaceDetector()
    raise ValueError(f"unknown face engine {engine!r}; available: insightface")


# ---------------------------------------------------------------------------
# Saved-detections helpers (CLI plumbing)
# ---------------------------------------------------------------------------


class UnkeyedDetections(ValueError):
    """A saved detections file that names no `photo_sha1`: its faces'
    depicts claims would have no source to cite."""


def load_detections(path: Path) -> DetectionResult:
    """The detections saved at `path`. A file that names no `photo_sha1`,
    the SHA-1 of the photograph's bytes, raises UnkeyedDetections, which
    says to run detect-faces on the photograph again."""
    try:
        return DetectionResult.model_validate_json(path.read_text())
    except ValidationError as exc:
        if any(
            error["type"] == "missing" and error["loc"] == ("photo_sha1",)
            for error in exc.errors()
        ):
            raise UnkeyedDetections(
                f"{path} names no photo_sha1, the SHA-1 of the photograph's "
                f"bytes, by which its faces' depicts claims cite the "
                f"photograph: run detect-faces on the photograph again"
            ) from None
        raise


def enroll_from_detections(
    detections: DetectionResult,
    face_index: int,
    gallery: Gallery,
    label: str,
    qid: str | None = None,
) -> GalleryPerson:
    for face in detections.faces:
        if face.index == face_index:
            return gallery.enroll(
                label=label, embedding=face.embedding, qid=qid
            )
    raise ValueError(
        f"no face with index {face_index} in detections "
        f"(have {[f.index for f in detections.faces]})"
    )
