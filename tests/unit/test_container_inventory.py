"""Inventory identity and persistence over authored fictional byte objects."""

import hashlib
import json
import os

import pytest
from pydantic import ValidationError

from tests.fixtures.semantic_acceptance import source_case
from yizkr.claims import ArtifactRef
from yizkr.pipeline import container_inventory as inventory_module
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    ContainerInventory,
    Eligibility,
    Placement,
    ReadingOutcome,
    SourcePart,
    StructuralLocator,
    keep_inventory,
    read_inventory,
    structural_id,
)


@pytest.fixture
def inventory():
    body = source_case("naturalisation-1952").source_bytes
    notes = source_case("birth-extract-1938").source_bytes
    image = b"fictional original image bytes: album-photo-1906"
    source = ArtifactRef.from_bytes(body + notes + image)
    parts = []
    data = {}
    for kind, member, text, payload in (
        ("slide_body", "ppt/slides/slide1.xml", body.decode(), body),
        (
            "speaker_notes",
            "ppt/notesSlides/notesSlide1.xml",
            notes.decode(),
            notes,
        ),
        ("embedded_resource", "ppt/media/image1.png", None, image),
    ):
        locator = StructuralLocator(
            kind=kind, package_part=member, position=1 if text else None
        )
        original = ArtifactRef.from_bytes(payload)
        data[original.sha1] = payload
        parts.append(
            SourcePart(
                id=structural_id(source, locator),
                kind=kind,
                locator=locator,
                byte_object=original,
                file=f"container-parts/{original.sha1}.bin",
                text=text,
                eligibility=Eligibility(state="allowed"),
                reading=ReadingOutcome(
                    state="read", empty=False, method="native"
                )
                if text
                else ReadingOutcome(),
            )
        )
    placements = []
    for order in (1, 2):
        locator = StructuralLocator(
            kind="image_placement",
            package_part="ppt/slides/slide1.xml",
            position=1,
            path=f"spTree/pic[{order}]",
            relationship_id="rId7",
        )
        placements.append(
            Placement(
                id=structural_id(source, locator),
                parent=parts[0].id,
                child=parts[2].id,
                locator=locator,
                order=order,
                layout=[
                    {
                        "kind": "transform",
                        "x": str(100 * order),
                        "unit": "EMU",
                    },
                    {"kind": "crop", "left": "1200", "right": "0"},
                ],
            )
        )
    return (
        ContainerInventory(
            source=source,
            container="presentation",
            parts=parts,
            placements=placements,
            complete=False,
            limitations=["visibility of hidden shapes is not interpreted"],
        ),
        data,
    )


def test_round_trip_keeps_parts_repeated_placements_and_original_bytes(
    tmp_path, inventory
):
    record, data = inventory
    keep_inventory(record, data, tmp_path)
    loaded = read_inventory(tmp_path)
    assert loaded == record
    assert loaded.parts[0].text != loaded.parts[1].text
    assert loaded.parts[0].locator.kind == "slide_body"
    assert loaded.parts[1].locator.kind == "speaker_notes"
    assert len({place.id for place in loaded.placements}) == 2
    assert len({place.child for place in loaded.placements}) == 1
    assert loaded.placements[0].layout == record.placements[0].layout
    assert not loaded.complete and loaded.limitations == record.limitations
    for part in loaded.parts:
        assert (tmp_path / part.file).read_bytes() == data[
            part.byte_object.sha1
        ]


def test_eligibility_and_reading_do_not_change_source_or_placement_identity(
    inventory,
):
    record, _data = inventory
    part = record.parts[-1]
    identity = structural_id(record.source, part.locator)
    part.eligibility = Eligibility(
        state="excluded", reason="listed byte object"
    )
    part.reading = ReadingOutcome(state="unsupported", reason="format")
    assert part.id == identity == structural_id(record.source, part.locator)
    another = ArtifactRef.from_bytes(b"another fictional source revision")
    assert structural_id(another, part.locator) != identity
    assert record.placements[0].id != record.placements[1].id


def test_empty_read_and_unattempted_are_distinct():
    assert ReadingOutcome().empty is None
    assert ReadingOutcome(state="read", empty=True).empty is True
    with pytest.raises(ValidationError, match="declares whether empty"):
        ReadingOutcome(state="read")
    with pytest.raises(ValidationError, match="successful reading"):
        ReadingOutcome(state="unattempted", empty=True)
    with pytest.raises(ValidationError, match="both path and digest"):
        ReadingOutcome(state="read", empty=False, product="reading.json")


def test_no_op_writes_and_metadata_only_reading_update(tmp_path, inventory):
    record, data = inventory
    keep_inventory(record, data, tmp_path)
    files = [tmp_path / CONTAINER_INVENTORY] + [
        tmp_path / part.file for part in record.parts
    ]
    for path in files:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in files
    }
    keep_inventory(record, {}, tmp_path)
    assert {
        path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in files
    } == before

    record.parts[-1].reading = ReadingOutcome(
        state="read",
        method="vision",
        empty=True,
        product="derived-media/reading.json",
        product_sha256=hashlib.sha256(b"fictional empty reading").hexdigest(),
    )
    keep_inventory(record, {}, tmp_path)
    assert read_inventory(tmp_path) == record
    for path in files[1:]:
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == before[path]


@pytest.mark.parametrize("damage", ["missing", "changed", "symlink", "fifo"])
def test_unverified_original_bytes_refuse_loading_and_metadata_update(
    tmp_path, inventory, damage
):
    record, data = inventory
    keep_inventory(record, data, tmp_path)
    original = tmp_path / record.parts[-1].file
    saved = (tmp_path / CONTAINER_INVENTORY).read_bytes()
    original.unlink()
    if damage == "changed":
        original.write_bytes(b"different bytes")
    elif damage == "symlink":
        target = tmp_path / "other.png"
        target.write_bytes(data[record.parts[-1].byte_object.sha1])
        original.symlink_to(target)
    elif damage == "fifo":
        os.mkfifo(original)
    assert read_inventory(tmp_path) is None
    with pytest.raises(ValueError, match="missing or damaged"):
        keep_inventory(record, {}, tmp_path)
    assert (tmp_path / CONTAINER_INVENTORY).read_bytes() == saved


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "missing-version",
        "duplicate-part",
        "duplicate-placement",
        "reference",
    ],
)
def test_invalid_version_and_references_are_not_admitted(
    tmp_path, inventory, damage
):
    record, data = inventory
    keep_inventory(record, data, tmp_path)
    malformed = record.model_dump(mode="json")
    if damage == "version":
        malformed["format"] = 2
    elif damage == "missing-version":
        malformed.pop("format")
    elif damage == "duplicate-part":
        malformed["parts"].append(malformed["parts"][0])
    elif damage == "duplicate-placement":
        malformed["placements"].append(malformed["placements"][0])
    else:
        malformed["placements"][0]["child"] = "absent-part"
    (tmp_path / CONTAINER_INVENTORY).write_text(json.dumps(malformed))
    assert read_inventory(tmp_path) is None


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "a/../../outside", "a\\b"]
)
def test_original_byte_paths_cannot_escape_the_record(name, inventory):
    record, _data = inventory
    with pytest.raises(ValidationError, match="safe relative file"):
        record.parts[0].file = name


def test_mismatched_supplied_bytes_are_refused_before_any_publication(
    tmp_path, inventory
):
    record, data = inventory
    wrong = data | {record.parts[-1].byte_object.sha1: b"wrong original bytes"}
    with pytest.raises(ValueError, match="missing or damaged"):
        keep_inventory(record, wrong, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_linked_parent_cannot_write_outside_the_inventory(tmp_path, inventory):
    record, data = inventory
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "inventory"
    directory.mkdir()
    (directory / "container-parts").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(ValueError, match="unsafe parent"):
        keep_inventory(record, data, directory)
    assert list(outside.iterdir()) == []
    assert not (directory / CONTAINER_INVENTORY).exists()


def test_linked_record_is_replaced_without_changing_its_target(
    tmp_path, inventory
):
    record, data = inventory
    outside = tmp_path / "outside.json"
    outside.write_text("unrelated bytes")
    (tmp_path / CONTAINER_INVENTORY).symlink_to(outside)
    assert read_inventory(tmp_path) is None
    keep_inventory(record, data, tmp_path)
    assert outside.read_text() == "unrelated bytes"
    assert not (tmp_path / CONTAINER_INVENTORY).is_symlink()
    assert read_inventory(tmp_path) == record


def test_failed_metadata_publication_preserves_the_previous_record(
    tmp_path, inventory, monkeypatch
):
    record, data = inventory
    keep_inventory(record, data, tmp_path)
    before = (tmp_path / CONTAINER_INVENTORY).read_bytes()
    record.parts[-1].eligibility = Eligibility(
        state="excluded", reason="listed"
    )
    replace = inventory_module.os.replace

    def interrupted(source, destination):
        if destination.name == CONTAINER_INVENTORY:
            raise OSError("fictional interrupted publication")
        replace(source, destination)

    monkeypatch.setattr(inventory_module.os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted publication"):
        keep_inventory(record, {}, tmp_path)
    assert (tmp_path / CONTAINER_INVENTORY).read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))
    assert read_inventory(tmp_path).parts[-1].eligibility.state == "allowed"
