"""Mixed slide words come only from complete, current native text blocks."""

import io
import json
import zipfile

import pytest

from tests.fixtures import slides
from yizkr.claims import ArtifactRef
from yizkr.pipeline.slide_layout import (
    NativeSlideLayout,
    assemble_native_layout,
    native_slide_blocks,
    parse_native_layout,
)


def xml(original=None):
    original = slides.layout_presentation() if original is None else original
    with zipfile.ZipFile(io.BytesIO(original)) as package:
        return package.read("ppt/slides/slide3.xml")


def blocks(original=None):
    return native_slide_blocks(xml(original), "slide-body-1")


def test_arrangement_adds_no_words_from_original_image():
    basis = blocks(slides.mixed_layout_presentation())
    assert [block.text for block in basis.blocks] == [
        "Stupfield arrivals",
        "1939",
        "Klara",
        "Gustav",
        "1938",
    ]
    layout = parse_native_layout(
        '{"rows":[["b1"],["b3","b5"],["b4","b2"]]}', basis
    )
    assert assemble_native_layout(basis, layout) == (
        "Stupfield arrivals\nKlara | 1938\nGustav | 1939"
    )
    assert "Angerring" not in assemble_native_layout(basis, layout)
    assert (
        NativeSlideLayout.model_validate_json(layout.model_dump_json())
        == layout
    )


@pytest.mark.parametrize(
    "answer",
    [
        '{"rows":[["b1"],["b3","b5"]]}',  # lost Gustav's row
        '{"rows":[["b1"],["b3","b5"],["b4","b2"],["b2"]]}',
        '{"rows":[["b1"],["b3","b5"],["b4","image1"]]}',
        '{"rows":[["b1"],["b3","b5"],'
        '["b4","Rahel wohnt jetzt Angerring 11"]]}',
        '{"rows":[["b1"],["b3","b5"],["b4","b2"],[]]}',
        '{"rows":[["b1"],["b3","b5"],["b4","b2"]],"text":"Angerring 11"}',
        '{"rows":[],"rows":[["b1"],["b3","b5"],["b4","b2"]]}',
        '{"rows":["b1","b2","b3","b4","b5"]}',
        '{"rows":[[1],[2],[3],[4],[5]]}',
    ],
)
def test_invalid_model_arrangements_cannot_become_native_evidence(answer):
    with pytest.raises(ValueError):
        parse_native_layout(answer, blocks())


@pytest.mark.parametrize(
    "update",
    [
        {"part_id": "another-slide"},
        {"byte_object": ArtifactRef.from_bytes(b"another slide")},
        {"basis_sha256": "0" * 64},
    ],
)
def test_saved_layout_must_identify_current_original_xml_and_block_basis(
    update,
):
    basis = blocks()
    layout = parse_native_layout(
        '{"rows":[["b1"],["b3","b5"],["b4","b2"]]}', basis
    )
    with pytest.raises(ValueError, match="another input"):
        assemble_native_layout(basis, layout.model_copy(update=update))


def test_changed_native_text_invalidates_saved_layout():
    original = xml()
    basis = native_slide_blocks(original, "slide-body-1")
    layout = parse_native_layout(
        '{"rows":[["b1"],["b3","b5"],["b4","b2"]]}', basis
    )
    changed = native_slide_blocks(
        original.replace(b"1938", b"1939"), "slide-body-1"
    )
    with pytest.raises(ValueError, match="another input"):
        assemble_native_layout(changed, layout)


def test_native_paragraphs_stay_exact_and_need_their_own_row():
    original = xml(slides.layout_presentation(arrangement="columns"))
    original = original.replace(
        b"Gustav: Stupfield, 1939</a:t></a:r></a:p>",
        b"Gustav: Stupfield, 1939</a:t></a:r></a:p>"
        b"<a:p><a:r><a:t>Klara came first.</a:t></a:r></a:p>",
    )
    basis = native_slide_blocks(original, "slide-body-1")
    layout = parse_native_layout('{"rows":[["b2"],["b1"]]}', basis)
    assert assemble_native_layout(basis, layout) == (
        "Klara: Stupfield, 1938\nGustav: Stupfield, 1939\nKlara came first."
    )
    with pytest.raises(ValueError, match="singleton"):
        parse_native_layout('{"rows":[["b2","b1"]]}', basis)


def test_image_only_body_needs_no_native_words():
    original = xml(slides.mixed_layout_presentation())
    import xml.etree.ElementTree as ET

    root = ET.fromstring(original)
    tree = root.find(f"{{{slides.P}}}cSld/{{{slides.P}}}spTree")
    for child in list(tree):
        if child.tag == f"{{{slides.P}}}sp":
            tree.remove(child)
    basis = native_slide_blocks(ET.tostring(root), "slide-body-1")
    assert basis.blocks == ()
    assert (
        assemble_native_layout(
            basis, parse_native_layout('{"rows":[]}', basis)
        )
        == ""
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            lambda value: value.replace(b'prst="rect"', b'prst="rightArrow"'),
            "drawings",
        ),
        (
            lambda value: value.replace(
                b'<a:off x="4500000"', b'<a:off x="500000"'
            ),
            "overlapping-text",
        ),
        (
            lambda value: value.replace(b"<a:xfrm>", b'<a:xfrm rot="1">'),
            "unsupported-geometry",
        ),
        (
            lambda value: value.replace(
                b"</p:spTree>", b"<p:grpSp/></p:spTree>"
            ),
            "grouped-shapes",
        ),
    ],
)
def test_explicit_mixed_selection_cannot_bypass_layout_limits(change, reason):
    with pytest.raises(ValueError, match=reason):
        native_slide_blocks(change(xml()), "slide-body-1")


def test_simple_text_remains_eligible_for_explicit_selection():
    basis = blocks(slides.layout_presentation(arrangement="simple"))
    layout = parse_native_layout(json.dumps({"rows": [["b1"]]}), basis)
    assert (
        assemble_native_layout(basis, layout)
        == "Klara came first, Gustl in 1939."
    )
