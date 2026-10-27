"""PDF inventory survives preparation, model refusal and persisted reuse."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures import pdfs
from tests.fixtures.records import record_answer
from tests.unit.test_pdf_exclusions import JPEG_SHA1, scan
from tests.unit.test_pdf_inventory import repeated_page
from tests.unit.test_pdf_pages import (
    EXCERPT,
    EXTRACT,
    LETTER_SHA1,
    REFUSAL,
    Raw,
    Reader,
    RefusingReader,
    booklet,
    document,
    folder,
    renderer,  # noqa: F401
    run,
)
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    read_inventory,
)
from yizkr.pipeline.estimate import Estimator, _Made
from yizkr.pipeline.orchestrate import AWAITING_CLAIMS, FAILED
from yizkr.pipeline.presentation import (
    derived_transcription,
    read_derived_media,
)
from yizkr.pipeline.textlayer import png


def saved(doc):
    inventory = read_inventory(doc.dir)
    assert inventory is not None
    assert inventory.source.sha1 == doc.sha1
    return inventory


def embedded(inventory):
    return [p for p in inventory.parts if p.kind == "embedded_resource"]


def test_native_pdf_records_original_parts_and_actual_image_reading(tmp_path):
    doc = document(
        run(
            tmp_path,
            folder(tmp_path, family_booklet_2011=booklet()),
            Reader({LETTER_SHA1: EXCERPT}),
        ),
        "family-booklet-2011.pdf",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    inventory = saved(doc)
    (part,) = embedded(inventory)
    assert part.byte_object.sha1 != LETTER_SHA1
    assert part.reading.state == "read"
    assert part.reading.product is not None
    assert any(
        representation.byte_object.sha1 == LETTER_SHA1
        for representation in part.representations
    )
    assert len(inventory.placements) >= 1
    assert all(
        page.reading.state == "read"
        for page in inventory.parts
        if page.kind == "page_body"
    )


def test_contextual_representations_do_not_invent_distinct_source_owners(
    tmp_path, monkeypatch
):
    # Substitute decoder results, not parsing or reading: this does not claim
    # PDFium supports a particular context-dependent PDF color space.
    variants = [
        png(128, 96, 1, bytes([shade]) * (128 * 96)) for shade in (7, 31)
    ]
    digests = {hashlib.sha1(data).hexdigest() for data in variants}
    decoded = iter(variants)
    monkeypatch.setattr(
        "yizkr.pipeline.textlayer.image_bytes",
        lambda _obj: (next(decoded), ".png"),
    )
    reader = Reader(dict.fromkeys(digests, EXCERPT))
    doc = document(
        run(
            tmp_path,
            folder(tmp_path, family_booklet_2011=pdfs.pdf([repeated_page()])),
            reader,
        ),
        "family-booklet-2011.pdf",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    (part,) = embedded(saved(doc))
    assert {rep.byte_object.sha1 for rep in part.representations} == digests
    assert reader.read == []
    assert read_derived_media(doc.dir) is None
    assert not any(
        derived_transcription(doc.dir, digest).is_file() for digest in digests
    )
    assert part.owner_binding is None
    assert "unique prepared" in part.ownership_issue
    assert part.reading.state == "unattempted"
    assert part.reading.product is None


def test_whole_pdf_publishes_inventory_before_model_and_keeps_render_lineage(
    tmp_path,
):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan()]))

    class InspectingReader(Reader):
        def complete(self, prompt, image_paths=()):
            if image_paths:
                records = list((tmp_path / "state").rglob(CONTAINER_INVENTORY))
                assert len(records) == 1
                inventory = read_inventory(records[0].parent)
                assert inventory is not None
                assert embedded(inventory)
            return super().complete(prompt, image_paths)

    reader = InspectingReader({"birth-extract-1938.pdf": EXTRACT})
    doc = document(run(tmp_path, corpus, reader), "birth-extract-1938.pdf")
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    inventory = saved(doc)
    assert embedded(inventory)
    representations = [r for p in inventory.parts for r in p.representations]
    whole = [r for r in representations if r.method == "rendered-pdf-v1"]
    assert len(whole) == 1
    assert whole[0].byte_object.sha1 == reader.read[0][1]
    assert whole[0].byte_object.sha1 != doc.sha1
    assert (doc.dir / whole[0].file).read_bytes().startswith(b"%PDF-")


@pytest.mark.parametrize("damage", [None, "missing-record", "missing-render"])
def test_reused_whole_pdf_keeps_or_restores_prepared_lineage(tmp_path, damage):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan()]))
    first = document(
        run(tmp_path, corpus, Reader({"birth-extract-1938.pdf": EXTRACT})),
        "birth-extract-1938.pdf",
    )
    representation = next(
        rep
        for part in saved(first).parts
        for rep in part.representations
        if rep.method == "rendered-pdf-v1"
    )
    before = {
        path: path.read_bytes()
        for path in (first.transcription, first.batch, first.review)
    }
    if damage == "missing-record":
        (first.dir / CONTAINER_INVENTORY).unlink()
    elif damage == "missing-render":
        (first.dir / representation.file).unlink()
    again = document(
        run(tmp_path, corpus, RefusingReader({})), "birth-extract-1938.pdf"
    )
    assert again.state.status == AWAITING_CLAIMS, again.state.error
    assert {path: path.read_bytes() for path in before} == before
    assert representation in [
        rep for part in saved(again).parts for rep in part.representations
    ]
    assert (again.dir / representation.file).is_file()


def test_whole_pdf_blank_outcome_does_not_count_page_markers_as_content(
    tmp_path,
):
    corpus = folder(tmp_path, family_booklet_2011=pdfs.pdf([scan(), scan()]))
    doc = document(
        run(
            tmp_path,
            corpus,
            Reader({"family-booklet-2011.pdf": record_answer("", "")}),
        ),
        "family-booklet-2011.pdf",
    )
    inventory = saved(doc)
    (source,) = [p for p in inventory.parts if p.kind == "container_source"]
    assert source.reading.state == "read"
    assert source.reading.empty is True


@pytest.mark.parametrize(
    "damage", ["missing", "version", "original-bytes", "representation"]
)
def test_recovery_keeps_unchanged_readings_proposals_and_reviews(
    tmp_path, damage
):
    corpus = folder(tmp_path, family_booklet_2011=booklet())
    first = document(
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT})),
        "family-booklet-2011.pdf",
    )
    inventory = saved(first)
    before = {
        path: path.read_bytes()
        for path in (first.transcription, first.batch, first.review)
    }
    manifest = first.dir / CONTAINER_INVENTORY
    if damage == "missing":
        manifest.unlink()
    elif damage == "version":
        data = json.loads(manifest.read_text())
        data["format"] = 0
        manifest.write_text(json.dumps(data))
    else:
        part = embedded(inventory)[0]
        name = (
            part.file
            if damage == "original-bytes"
            else next(r.file for r in part.representations if r.file)
        )
        (first.dir / name).write_bytes(b"damaged fictional PDF product")
    restored = document(
        run(tmp_path, corpus, RefusingReader({})),
        "family-booklet-2011.pdf",
    )
    assert restored.state.status == AWAITING_CLAIMS, restored.state.error
    assert {path: path.read_bytes() for path in before} == before
    recovered = saved(restored)
    assert [p.id for p in recovered.parts] == [p.id for p in inventory.parts]
    assert [p.id for p in recovered.placements] == [
        p.id for p in inventory.placements
    ]
    assert embedded(recovered)[0].reading.state == "read"


def test_excluded_pdf_keeps_metadata_but_sends_and_publishes_no_image(
    tmp_path,
):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan(pdfs.jpeg())]))
    reader = RefusingReader({})
    doc = document(
        run(
            tmp_path,
            corpus,
            reader,
            exclusions=ExclusionList(sha1s=frozenset({JPEG_SHA1})),
        ),
        "birth-extract-1938.pdf",
    )
    assert doc.state.status == FAILED
    assert "excluded embedded image" in str(doc.state.error)
    parts = embedded(saved(doc))
    assert parts and all(p.eligibility.state == "excluded" for p in parts)
    assert all(p.file is None for p in parts)
    assert all(r.file is None for p in parts for r in p.representations)
    assert reader.read == []
    assert not doc.transcription.exists()


def test_malformed_pdf_keeps_an_explicit_failed_inventory(tmp_path):
    corpus = folder(tmp_path, birth_extract_1938=b"%PDF-1.4\ninvalid fixture")
    doc = document(
        run(tmp_path, corpus, RefusingReader({})), "birth-extract-1938.pdf"
    )
    assert doc.state.status == FAILED
    inventory = saved(doc)
    assert not inventory.complete
    assert inventory.limitations
    assert any(p.reading.state == "failed" for p in inventory.parts)
    assert not doc.transcription.exists()


def test_failed_image_reading_remains_distinct_from_unattempted(tmp_path):
    doc = document(
        run(
            tmp_path,
            folder(tmp_path, family_booklet_2011=booklet()),
            Reader({LETTER_SHA1: Raw(REFUSAL)}),
        ),
        "family-booklet-2011.pdf",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    (part,) = embedded(saved(doc))
    assert part.eligibility.state == "allowed"
    assert part.reading.state == "failed"
    assert part.reading.reason
    assert part.reading.product is None


def test_estimate_rechecks_without_publishing_missing_inventory(tmp_path):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan()]))
    doc = document(
        run(tmp_path, corpus, Reader({"birth-extract-1938.pdf": EXTRACT})),
        "birth-extract-1938.pdf",
    )
    (doc.dir / CONTAINER_INVENTORY).unlink()
    before = {p: p.read_bytes() for p in doc.dir.rglob("*") if p.is_file()}
    estimator = Estimator(
        corpus, tmp_path / "state", exclusions=ExclusionList()
    )
    result = estimator._transcription(doc, False, _Made())
    assert result.reused and not result.fails
    assert estimator._clients == {}
    after = {p: p.read_bytes() for p in doc.dir.rglob("*") if p.is_file()}
    assert after == before
