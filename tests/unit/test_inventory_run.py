"""PPTX inventory publication, recovery and actual reading outcomes."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from tests.fixtures.records import record_answer
from tests.unit.test_deck_excerpts import talk_folder
from tests.unit.test_pdf_pages import (
    EXCERPT,
    LETTER_SHA1,
    REFUSAL,
    Raw,
    Reader,
    RefusingReader,
    document,
    run,
)
from tests.unit.test_presentation import DECK_TIME
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.container_inventory import read_inventory
from yizkr.pipeline.orchestrate import AWAITING_CLAIMS


def inventory_of(doc):
    inventory = read_inventory(doc.dir)
    assert inventory is not None
    assert inventory.source.sha1 == doc.sha1
    return inventory


def letter_part(doc):
    return next(
        part
        for part in inventory_of(doc).parts
        if part.kind == "embedded_resource"
        and part.byte_object is not None
        and part.byte_object.sha1 == LETTER_SHA1
    )


def test_reading_outcome_names_the_published_image_product(tmp_path):
    doc = document(
        run(tmp_path, talk_folder(tmp_path), Reader({LETTER_SHA1: EXCERPT})),
        "library-talk-2018.pptx",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    part = letter_part(doc)
    assert part.eligibility.state == "allowed"
    assert part.reading.state == "read"
    assert part.reading.empty is False
    product = (doc.dir / part.reading.product).read_bytes()
    assert part.reading.product_sha256 == hashlib.sha256(product).hexdigest()
    assert EXCERPT in json.loads(product)["text"]


@pytest.mark.parametrize("damage", ["missing", "version", "original-bytes"])
def test_regeneration_preserves_existing_readings_and_proposals(
    tmp_path, damage
):
    corpus = talk_folder(tmp_path)
    doc = document(
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT})),
        "library-talk-2018.pptx",
    )
    before = {
        path: path.read_bytes()
        for path in (doc.transcription, doc.batch, doc.review)
    }
    part_id = letter_part(doc).id
    manifest = doc.dir / "container-inventory.json"
    if damage == "missing":
        manifest.unlink()
    elif damage == "version":
        value = json.loads(manifest.read_text())
        value["format"] = 0
        manifest.write_text(json.dumps(value))
    else:
        original = next(part for part in inventory_of(doc).parts if part.file)
        (doc.dir / original.file).write_bytes(b"damaged fictional member")

    restored = document(
        run(tmp_path, corpus, RefusingReader({})),
        "library-talk-2018.pptx",
    )
    assert restored.state.status == AWAITING_CLAIMS, restored.state.error
    assert {path: path.read_bytes() for path in before} == before
    part = letter_part(restored)
    assert part.id == part_id
    assert part.reading.state == "read"


def test_changed_exclusion_updates_inventory_without_an_image_call(tmp_path):
    corpus = talk_folder(tmp_path)
    first = document(
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT})),
        "library-talk-2018.pptx",
    )
    original_id = letter_part(first).id
    reader = Reader({})
    second = document(
        run(
            tmp_path,
            corpus,
            reader,
            exclusions=ExclusionList.parse("image2.png\n"),
        ),
        "library-talk-2018.pptx",
    )
    assert second.state.status == AWAITING_CLAIMS, second.state.error
    part = letter_part(second)
    assert part.id == original_id
    assert part.eligibility.state == "excluded"
    # The earlier successful reading remains an observation, not permission
    # to use its owner or send the now-excluded image again.
    assert part.reading.state == "read"
    assert second.dir.joinpath(part.reading.product).is_file()
    assert reader.read == []


def test_failed_image_reading_is_not_reported_as_unattempted(tmp_path):
    doc = document(
        run(
            tmp_path,
            talk_folder(tmp_path),
            Reader({LETTER_SHA1: Raw(REFUSAL)}),
        ),
        "library-talk-2018.pptx",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    part = letter_part(doc)
    assert part.eligibility.state == "allowed"
    assert part.reading.state == "failed"
    assert part.reading.reason
    assert part.reading.product is None


def test_unreferenced_excluded_alias_cannot_send_the_shared_image(tmp_path):
    corpus = talk_folder(tmp_path)
    with zipfile.ZipFile(corpus / "library-talk-2018.pptx", "a") as archive:
        original = archive.read("ppt/media/image2.png")
        archive.writestr(
            zipfile.ZipInfo(
                "ppt/media/excluded-copy.png", date_time=DECK_TIME
            ),
            original,
        )
    reader = Reader({})
    doc = document(
        run(
            tmp_path,
            corpus,
            reader,
            exclusions=ExclusionList.parse("excluded-copy.png\n"),
        ),
        "library-talk-2018.pptx",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    copies = [
        part
        for part in inventory_of(doc).parts
        if part.byte_object and part.byte_object.sha1 == LETTER_SHA1
    ]
    assert len(copies) == 2
    assert all(part.eligibility.state == "excluded" for part in copies)
    assert all(part.reading.state == "unattempted" for part in copies)
    assert reader.read == []


def test_successful_blank_image_is_an_explicit_empty_reading(tmp_path):
    doc = document(
        run(
            tmp_path,
            talk_folder(tmp_path),
            Reader({LETTER_SHA1: record_answer("")}),
        ),
        "library-talk-2018.pptx",
    )
    assert doc.state.status == AWAITING_CLAIMS, doc.state.error
    part = letter_part(doc)
    assert part.reading.state == "read"
    assert part.reading.empty is True
