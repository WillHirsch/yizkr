"""PPTX source occurrences survive reading filters and legacy projection."""

from __future__ import annotations

import hashlib
import struct
import zipfile
from xml.sax.saxutils import escape

import pytest

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import read_inventory
from yizkr.pipeline.presentation import (
    inventory_current,
    read_derived_media,
    read_on_their_own,
    record_derived_media,
    slide_pages,
    slide_transcript,
    unpack_pptx,
)

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
PHOTO = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 300, 200)
)
RULE = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 200, 2)
)
UNSUPPORTED = b"fictional library-talk-2018 embedded object"


def relationships(*entries):
    return (
        f'<Relationships xmlns="{RELS}">'
        + "".join(
            f'<Relationship Id="{rid}" Type="{R}/{kind}" '
            f'Target="{escape(target)}"'
            + (' TargetMode="External"' if external else "")
            + "/>"
            for rid, kind, target, external in entries
        )
        + "</Relationships>"
    )


def picture(rid, number, *, hidden=False):
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{number}" name="album photograph"'
        + (' hidden="1"' if hidden else "")
        + "/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="{rid}"/>'
        '<a:srcRect l="1000" t="2000" r="3000" b="4000"/>'
        "<a:stretch><a:fillRect/></a:stretch></p:blipFill>"
        '<p:spPr><a:xfrm rot="60000" flipH="1"><a:off x="100" y="200"/>'
        '<a:ext cx="300" cy="400"/></a:xfrm></p:spPr></p:pic>'
    )


def body(text):
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="caption"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>"
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>"
        + escape(text)
        + "</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def source_deck(path, change=None):
    """The fictional library talk, including repeated album-photo placements.

    Unused, hidden, missing and external references exercise inventory limits;
    none adds a historical assertion to the fictional universe.
    """
    grouped = (
        '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="photo group"/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        '<p:grpSpPr><a:xfrm><a:off x="10" y="20"/>'
        '<a:ext cx="1000" cy="2000"/><a:chOff x="0" y="0"/>'
        '<a:chExt cx="500" cy="1000"/></a:xfrm></p:grpSpPr>'
        + picture("photo", 5, hidden=True)
        + "</p:grpSp>"
    )
    members = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>'
        ),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            '<p:sldId id="256" r:id="first"/>'
            '<p:sldId id="257" r:id="second"/>'
            "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": relationships(
            ("first", "slide", "slides/slide9.xml", False),
            ("second", "slide", "slides/slide2.xml", False),
        ),
        "ppt/slides/slide9.xml": (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            "<p:cSld><p:spTree>"
        )
        + body("Wastl and Hilde")
        + picture("rule", 3)
        + picture("photo", 6)
        + grouped
        + "<p:graphicFrame><p:nvGraphicFramePr>"
        '<p:cNvPr id="7" name="embedded object"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        '<a:graphic><a:graphicData><p:oleObj r:id="unsupported"/>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
        + picture("missing", 8)
        + picture("external", 9)
        + "</p:spTree></p:cSld></p:sld>",
        "ppt/slides/slide2.xml": (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" show="0">'
            "<p:cSld><p:spTree>"
        )
        + picture("copy", 3)
        + "</p:spTree></p:cSld></p:sld>",
        "ppt/slides/_rels/slide9.xml.rels": relationships(
            ("photo", "image", "../media/album.png", False),
            ("rule", "image", "../media/rule.png", False),
            (
                "unsupported",
                "oleObject",
                "../embeddings/oleObject1.bin",
                False,
            ),
            ("missing", "image", "../media/missing.png", False),
            ("external", "image", "https://example.invalid/album.png", True),
            ("unused", "image", "../media/relationship.png", False),
            ("notes", "notesSlide", "../notesSlides/notesSlide9.xml", False),
        ),
        "ppt/slides/_rels/slide2.xml.rels": relationships(
            ("copy", "image", "../media/copy.png", False)
        ),
        "ppt/notesSlides/notesSlide9.xml": (
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
        )
        + body("Photograph from the family album")
        + "</p:spTree></p:cSld></p:notes>",
        "ppt/media/album.png": PHOTO,
        "ppt/media/copy.png": PHOTO,
        "ppt/media/rule.png": RULE,
        "ppt/media/relationship.png": PHOTO + b"relationship-only",
        "ppt/media/orphan.png": PHOTO + b"unreferenced",
        "ppt/embeddings/oleObject1.bin": UNSUPPORTED,
    }
    if change is not None:
        change(members)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data
            )
    return members


def by_package(inventory):
    return {part.locator.package_part: part for part in inventory.parts}


def persisted(tmp_path, excluded=lambda sha1, names: False):
    source = tmp_path / "library-talk-2018.pptx"
    members = source_deck(source)
    content = unpack_pptx(source)
    directory = tmp_path / "document"
    record_derived_media(
        content,
        source_sha1=content.inventory.source.sha1,
        title="Library talk",
        directory=directory,
        excluded=excluded,
    )
    return content, read_inventory(directory), directory, members


def test_native_body_and_notes_have_distinct_located_readings(tmp_path):
    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source)
    content = unpack_pptx(source)
    parts = by_package(content.inventory)
    slide = parts["ppt/slides/slide9.xml"]
    notes = parts["ppt/notesSlides/notesSlide9.xml"]
    assert content.inventory.source == ArtifactRef.from_path(source)
    assert slide.text == "Wastl and Hilde"
    assert notes.text == "Photograph from the family album"
    assert (slide.kind, notes.kind) == ("slide_body", "speaker_notes")
    assert slide.id != notes.id
    assert slide.locator.position == notes.locator.position == 1
    assert slide.reading.state == notes.reading.state == "read"
    assert not slide.reading.empty and not notes.reading.empty
    empty = parts["ppt/slides/slide2.xml"]
    assert empty.locator.position == 2
    assert empty.text == "" and empty.reading.state == "read"
    assert empty.reading.empty is True
    assert "hidden slide content" in empty.limitations


def test_repeated_bytes_keep_each_structural_occurrence_and_raw_geometry(
    tmp_path,
):
    content, inventory, _directory, _members = persisted(tmp_path)
    photo = hashlib.sha1(PHOTO).hexdigest()
    parts = {part.id: part for part in inventory.parts}
    placed = [
        p
        for p in inventory.placements
        if parts[p.child].byte_object
        and parts[p.child].byte_object.sha1 == photo
    ]
    assert len(placed) == len({p.id for p in placed}) == 3
    assert [p.locator.position for p in placed] == [1, 1, 2]
    assert placed[0].locator.path != placed[1].locator.path
    assert placed[0].order < placed[1].order
    assert (
        placed[0].locator.relationship_id
        == placed[1].locator.relationship_id
        == "photo"
    )
    group, image = placed[1].layout
    assert group["element"] == "p:grpSp"
    assert image["element"] == "p:pic"
    assert group["coordinate_frame"] == image["coordinate_frame"] == "parent"
    assert group["transforms"][0]["children"][-1]["attributes"] == {
        "cx": "500",
        "cy": "1000",
    }
    assert image["transforms"][0]["attributes"] == {
        "l": "1000",
        "t": "2000",
        "r": "3000",
        "b": "4000",
    }
    assert image["transforms"][1]["attributes"] == {
        "rot": "60000",
        "flipH": "1",
    }
    assert image["nonvisual"]["hidden"] == "1"


def test_original_bytes_and_explicit_limits_round_trip(tmp_path):
    _content, inventory, directory, members = persisted(tmp_path)
    parts = by_package(inventory)
    for part in inventory.parts:
        if part.file is not None:
            original = members[part.locator.package_part]
            if isinstance(original, str):
                original = original.encode()
            assert (directory / part.file).read_bytes() == original
    unsupported = parts["ppt/embeddings/oleObject1.bin"]
    assert unsupported.reading.state == "unsupported"
    assert (directory / unsupported.file).read_bytes() == UNSUPPORTED
    missing = parts["ppt/media/missing.png"]
    assert missing.file is missing.byte_object is None
    assert missing.eligibility.state == "undetermined"
    assert missing.reading.state == "unsupported"
    assert not inventory.complete
    for target, reason in (
        ("relationship.png", "relationship-only"),
        ("orphan.png", "unreferenced"),
    ):
        part = parts[f"ppt/media/{target}"]
        assert any(reason in limit for limit in part.limitations)
        assert all(p.child != part.id for p in inventory.placements)
    assert any(
        "external relationship" in limit
        for part in inventory.parts
        for limit in part.limitations
    )
    assert all(
        "https:" not in part.locator.package_part for part in inventory.parts
    )


def test_filtering_changes_eligibility_without_renumbering_occurrences(
    tmp_path,
):
    content, original, directory, _members = persisted(tmp_path)

    def denied(sha1, names):
        return "copy.png" in names

    assert inventory_current(directory, original.source.sha1)
    assert not inventory_current(directory, original.source.sha1, denied)
    record_derived_media(
        content,
        source_sha1=original.source.sha1,
        title="Library talk",
        directory=directory,
        excluded=denied,
    )
    changed = read_inventory(directory)
    assert [part.id for part in changed.parts] == [
        part.id for part in original.parts
    ]
    assert changed.placements == original.placements
    photo = hashlib.sha1(PHOTO).hexdigest()
    photo_parts = [
        part
        for part in changed.parts
        if part.byte_object and part.byte_object.sha1 == photo
    ]
    assert len(photo_parts) == 2
    assert {part.eligibility.state for part in photo_parts} == {"excluded"}
    assert {part.reading.state for part in photo_parts} == {"unattempted"}
    assert inventory_current(directory, original.source.sha1, denied)
    assert not inventory_current(directory, "0" * 40, denied)


def test_legacy_projection_stays_available_without_defining_placements(
    tmp_path,
):
    content, inventory, directory, _members = persisted(tmp_path)
    legacy = read_derived_media(directory)
    relationship = by_package(inventory)["ppt/media/relationship.png"]
    assert relationship.byte_object.sha1 in legacy.media
    assert "ppt/media/relationship.png" in content.slides[0].media
    assert all(p.child != relationship.id for p in inventory.placements)
    assert legacy.source_sha1 == inventory.source.sha1
    assert hashlib.sha1(PHOTO).hexdigest() in legacy.media


def test_a_missing_slide_is_an_unsupported_part_not_a_successful_empty_read(
    tmp_path,
):
    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, lambda members: members.pop("ppt/slides/slide9.xml"))
    content = unpack_pptx(source)
    parts = by_package(content.inventory)
    missing = parts["ppt/slides/slide9.xml"]
    assert missing.locator.position == 1
    assert missing.reading.state == "unsupported"
    assert missing.reading.empty is None
    assert parts["ppt/slides/slide2.xml"].locator.position == 2
    assert not content.inventory.complete
    with pytest.raises(ValueError, match="cannot read slide 1.*missing"):
        slide_transcript(content)
    output = tmp_path / "reading"
    output.mkdir()
    with pytest.raises(ValueError, match="cannot read slide 1.*missing"):
        slide_pages(content, output)
    assert list(output.iterdir()) == []

    from yizkr.pipeline.transcribe import DeckTranscriber

    requested = []

    def client():
        requested.append("engine")
        raise AssertionError("missing native slide must fail before dispatch")

    with pytest.raises(ValueError, match="cannot read slide 1.*missing"):
        DeckTranscriber(client).transcribe(source)
    assert requested == []


def test_duplicate_notes_relationships_do_not_duplicate_source_parts(tmp_path):
    def duplicate(members):
        key = "ppt/slides/_rels/slide9.xml.rels"
        members[key] = members[key].replace(
            "</Relationships>",
            f'<Relationship Id="notesAgain" Type="{R}/notesSlide" '
            'Target="../notesSlides/notesSlide9.xml"/></Relationships>',
        )

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, duplicate)
    inventory = unpack_pptx(source).inventory
    assert len([p for p in inventory.parts if p.kind == "speaker_notes"]) == 1
    assert len({p.id for p in inventory.parts}) == len(inventory.parts)


def test_a_structural_reference_without_a_shape_discloses_unknown_visibility(
    tmp_path,
):
    def timing(members):
        key = "ppt/slides/slide9.xml"
        members[key] = members[key].replace(
            "</p:sld>",
            '<p:timing><p:audio r:embed="photo"/></p:timing></p:sld>',
        )

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, timing)
    inventory = unpack_pptx(source).inventory
    (occurrence,) = [
        p for p in inventory.placements if "p:audio" in p.locator.path
    ]
    assert occurrence.layout == []
    assert any(
        "visible placement unknown" in note for note in occurrence.limitations
    )


@pytest.mark.parametrize("location", ["notes", "extension"])
def test_a_background_outside_the_direct_slide_fill_is_not_promoted(
    tmp_path, location
):
    def background(members):
        fill = (
            '<p:bg><p:bgPr><a:blipFill><a:blip r:embed="unused"/>'
            "</a:blipFill></p:bgPr></p:bg>"
        )
        if location == "notes":
            key = "ppt/notesSlides/notesSlide9.xml"
            members[key] = (
                members[key]
                .replace(f'xmlns:a="{A}"', f'xmlns:a="{A}" xmlns:r="{R}"')
                .replace("<p:cSld>", "<p:cSld>" + fill)
            )
            members["ppt/notesSlides/_rels/notesSlide9.xml.rels"] = (
                relationships(
                    ("unused", "image", "../media/relationship.png", False)
                )
            )
        else:
            key = "ppt/slides/slide9.xml"
            members[key] = members[key].replace(
                "</p:sld>", "<p:extLst>" + fill + "</p:extLst></p:sld>"
            )

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, background)
    inventory = unpack_pptx(source).inventory
    (occurrence,) = [
        p for p in inventory.placements if "/p:bg[1]/" in p.locator.path
    ]
    assert occurrence.occurrence == "reference"
    assert any(
        "visible placement unknown" in note for note in occurrence.limitations
    )


def test_chart_bytes_are_retained_and_inherited_layout_is_an_explicit_limit(
    tmp_path,
):
    def chart(members):
        key = "ppt/slides/_rels/slide2.xml.rels"
        members[key] = members[key].replace(
            "</Relationships>",
            f'<Relationship Id="chart" Type="{R}/chart" '
            'Target="../charts/chart1.xml"/>'
            f'<Relationship Id="layout" Type="{R}/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/></Relationships>',
        )
        members["ppt/charts/chart1.xml"] = "<chart/>"
        members["ppt/slideLayouts/slideLayout1.xml"] = "<layout/>"

    source = tmp_path / "library-talk-2018.pptx"
    members = source_deck(source, chart)
    content = unpack_pptx(source)
    parts = by_package(content.inventory)
    chart_part = parts["ppt/charts/chart1.xml"]
    assert chart_part.reading.state == "unsupported"
    assert (
        content.inventory_bytes[chart_part.byte_object.sha1]
        == members["ppt/charts/chart1.xml"].encode()
    )
    assert any(
        "related package part is not expanded" in note
        for note in parts["ppt/slides/slide2.xml"].limitations
    )
    assert not content.inventory.complete


def test_a_hyperlink_inside_a_shape_does_not_establish_image_paint(tmp_path):
    def hyperlink(members):
        key = "ppt/slides/slide9.xml"
        members[key] = members[key].replace(
            '<p:cNvPr id="2" name="caption"/>',
            '<p:cNvPr id="2" name="caption">'
            '<a:hlinkClick r:id="unused"/></p:cNvPr>',
        )

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, hyperlink)
    inventory = unpack_pptx(source).inventory
    (link,) = [
        p for p in inventory.placements if "a:hlinkClick" in p.locator.path
    ]
    assert link.layout
    assert any(
        "visible placement unknown" in note for note in link.limitations
    )


def test_an_unresolved_slide_relationship_does_not_renumber_later_slides(
    tmp_path,
):
    def unresolved(members):
        key = "ppt/presentation.xml"
        members[key] = members[key].replace('r:id="first"', 'r:id="absent"')

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, unresolved)
    content = unpack_pptx(source)
    slide = by_package(content.inventory)["ppt/slides/slide2.xml"]
    assert slide.locator.position == 2
    assert content.slides[0].number == 2
    assert any("position 1" in note for note in content.inventory.limitations)
    assert not content.inventory.complete


def test_an_unreferenced_alias_excludes_the_same_bytes_from_both_reader_views(
    tmp_path,
):
    def same_photo(members):
        members["ppt/media/orphan.png"] = PHOTO

    def denied(sha1, names):
        return "orphan.png" in names

    source = tmp_path / "library-talk-2018.pptx"
    source_deck(source, same_photo)
    content = unpack_pptx(source)
    directory = tmp_path / "document"
    record = record_derived_media(
        content,
        source_sha1=content.inventory.source.sha1,
        title="Library talk",
        directory=directory,
        excluded=denied,
    )
    photo = hashlib.sha1(PHOTO).hexdigest()
    assert "orphan.png" not in record.media[photo].names
    assert photo not in slide_pages(content, directory, excluded=denied).images
    assert photo not in read_on_their_own(record, directory, excluded=denied)
    inventory = read_inventory(directory)
    assert {
        part.eligibility.state
        for part in inventory.parts
        if part.byte_object and part.byte_object.sha1 == photo
    } == {"excluded"}
