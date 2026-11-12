"""Only accounted-for, eligible slide paint reaches composed rendering."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from PIL import Image

from tests.unit.test_pptx_inventory import (
    A,
    P,
    R,
    body,
    inherited_background_deck,
    picture,
    relationships,
)
from yizkr.pipeline.container_sources import prepare_sources, project_sources
from yizkr.pipeline.presentation import unpack_pptx
from yizkr.pipeline.slide_admission import admit_slides


def raster():
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(image, "PNG")
    return image.getvalue()


def deck(path, change=None):
    """Two fictional slides, real raster bytes, and separate speaker notes."""

    def members(values):
        key = "ppt/slides/_rels/slide1.xml.rels"
        values[key] = values[key].replace(
            "</Relationships>",
            f'<Relationship Id="notes" Type="{R}/notesSlide" '
            'Target="../notesSlides/notesSlide1.xml"/></Relationships>',
        )
        values["ppt/notesSlides/notesSlide1.xml"] = (
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}">'
            "<p:cSld><p:spTree>"
            + body("Photograph from the family album")
            + "</p:spTree></p:cSld></p:notes>"
        )
        if change:
            change(values)

    return inherited_background_deck(path, raster(), change=members)


def projected(path, directory, excluded=lambda _sha1, _names: False):
    content = unpack_pptx(path)
    inventory = prepare_sources(
        content.inventory,
        content.inventory_bytes,
        directory,
        excluded=excluded,
    )
    return project_sources(
        inventory, directory, data=content.inventory_bytes, excluded=excluded
    )


def admission(path, directory, excluded=lambda _sha1, _names: False):
    return admit_slides(
        path.read_bytes(),
        projected(path, directory),
        excluded=excluded,
        source_names=[path.name],
    )


def test_inherited_backgrounds_bind_actual_sources_and_consulted_xml(tmp_path):
    path = deck(tmp_path / "library-talk-2018.pptx")
    sources = projected(path, tmp_path)
    proofs = admit_slides(
        path.read_bytes(), sources, excluded=lambda *_: False
    )
    assert [proof.position for proof in proofs] == [1, 2]
    image = next(
        p for p in sources.inventory.parts if p.kind == "embedded_resource"
    )
    assert all(proof.image_part_ids == [image.id] for proof in proofs)
    assert proofs[0].placement_ids != proofs[1].placement_ids
    assert "ppt/notesSlides/notesSlide1.xml" not in proofs[0].package_parts
    assert "ppt/slides/slide2.xml" not in proofs[0].package_parts
    with zipfile.ZipFile(path) as archive:
        for name, digest in proofs[0].package_parts.items():
            assert digest == hashlib.sha256(archive.read(name)).hexdigest()
    assert {
        "ppt/slides/slide1.xml",
        "ppt/slideLayouts/slideLayout1.xml",
        "ppt/slideMasters/slideMaster1.xml",
        "ppt/theme/theme1.xml",
    } <= proofs[0].package_parts.keys()


def test_direct_repeated_images_keep_each_placement_and_single_owner(tmp_path):
    def change(values):
        key = "ppt/slides/slide1.xml"
        values[key] = values[key].replace(
            "</p:spTree>",
            picture("photo", 3) + picture("photo", 4) + "</p:spTree>",
        )
        key = "ppt/slides/_rels/slide1.xml.rels"
        values[key] = values[key].replace(
            "</Relationships>",
            f'<Relationship Id="photo" Type="{R}/image" '
            'Target="../media/letter.png"/></Relationships>',
        )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    first, second = admission(path, tmp_path)
    assert first.image_part_ids == second.image_part_ids
    assert len(first.placement_ids) == 3
    assert len(second.placement_ids) == 1


@pytest.mark.parametrize(
    "excluded_by", ["container", "image", "alias", "outside-alias"]
)
def test_current_exclusions_are_checked_after_projection(
    tmp_path, excluded_by
):
    def alias(values):
        values["ppt/media/excluded-copy.png"] = values["ppt/media/letter.png"]
        values["ppt/artwork/outside-copy.png"] = values["ppt/media/letter.png"]

    path = deck(tmp_path / "library-talk-2018.pptx", alias)
    sources = projected(path, tmp_path)
    image = next(iter(sources.sources))

    def excluded(sha1, names):
        return {
            "container": sha1 == sources.inventory.source.sha1,
            "image": sha1 == image,
            "alias": "excluded-copy.png" in names,
            "outside-alias": "outside-copy.png" in names,
        }[excluded_by]

    with pytest.raises(ValueError, match="excluded"):
        admit_slides(path.read_bytes(), sources, excluded=excluded)


def test_solid_override_does_not_admit_unused_ancestor_image(tmp_path):
    def change(values):
        for number in (1, 2):
            key = f"ppt/slides/slide{number}.xml"
            values[key] = values[key].replace(
                "<p:cSld>",
                "<p:cSld><p:bg><p:bgPr><a:solidFill>"
                '<a:srgbClr val="FFFFFF"/></a:solidFill></p:bgPr></p:bg>',
            )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    proofs = admission(path, tmp_path, lambda _, names: "letter.png" in names)
    assert all(proof.image_part_ids == [] for proof in proofs)


def test_notes_images_are_not_visual_contributors(tmp_path):
    def change(values):
        key = "ppt/notesSlides/notesSlide1.xml"
        values[key] = (
            values[key]
            .replace(f'xmlns:a="{A}"', f'xmlns:a="{A}" xmlns:r="{R}"')
            .replace("</p:spTree>", picture("notes", 3) + "</p:spTree>")
        )
        values["ppt/notesSlides/_rels/notesSlide1.xml.rels"] = relationships(
            ("notes", "image", "../media/notes.png", False)
        )
        values["ppt/media/notes.png"] = (
            values["ppt/media/letter.png"] + b"notes"
        )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    proofs = admission(path, tmp_path, lambda _, names: "notes.png" in names)
    assert all(len(proof.image_part_ids) == 1 for proof in proofs)


@pytest.mark.parametrize(
    "fill",
    [
        "<a:noFill/>",
        "",
        "<a:effectLst/>",
        '<a:solidFill><a:srgbClr val="FFFFFF"><a:alpha val="0"/>'
        "</a:srgbClr></a:solidFill>",
        "<a:gradFill/>",
        "<a:pattFill/>",
        '<a:solidFill><a:schemeClr val="bg1"/></a:solidFill>',
        '<a:blipFill><a:blip r:embed="override"/></a:blipFill>',
    ],
)
def test_unproved_override_cannot_hide_inherited_image(tmp_path, fill):
    def change(values):
        key = "ppt/slides/slide1.xml"
        values[key] = values[key].replace(
            "<p:cSld>", f"<p:cSld><p:bg><p:bgPr>{fill}</p:bgPr></p:bg>"
        )
        if "override" in fill:
            rels = "ppt/slides/_rels/slide1.xml.rels"
            values[rels] = values[rels].replace(
                "</Relationships>",
                f'<Relationship Id="override" Type="{R}/image" '
                'Target="../media/override.png"/></Relationships>',
            )
            values["ppt/media/override.png"] = raster() + b"override"

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    with pytest.raises(ValueError, match="background"):
        admission(path, tmp_path, lambda _, names: "letter.png" in names)


def test_native_shape_no_fill_is_not_a_background_override(tmp_path):
    def change(values):
        key = "ppt/slides/slide1.xml"
        values[key] = values[key].replace(
            "<p:spPr/>", "<p:spPr><a:noFill/></p:spPr>"
        )

    assert (
        len(
            admission(
                deck(tmp_path / "library-talk-2018.pptx", change), tmp_path
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "element",
    [
        body("Photograph from the family album"),
        picture("letter", 3),
        "<p:cxnSp><p:nvCxnSpPr/><p:spPr/></p:cxnSp>",
    ],
)
def test_unrepresented_inherited_content_refuses(tmp_path, element):
    def change(values):
        key = "ppt/slideLayouts/slideLayout1.xml"
        values[key] = values[key].replace(
            "<p:spTree/>", f"<p:spTree>{element}</p:spTree>"
        )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    with pytest.raises(ValueError, match="inherited shape"):
        admission(path, tmp_path)


def test_empty_inherited_placeholder_formatting_is_supported(tmp_path):
    def change(values):
        key = "ppt/slideLayouts/slideLayout1.xml"
        placeholder = body("").replace(
            "<p:nvPr/>", '<p:nvPr><p:ph type="body"/></p:nvPr>'
        )
        values[key] = values[key].replace(
            "<p:spTree/>", f"<p:spTree>{placeholder}</p:spTree>"
        )

    assert (
        len(
            admission(
                deck(tmp_path / "library-talk-2018.pptx", change), tmp_path
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "failure",
    [
        "external",
        "duplicate-id",
        "unknown-kind",
        "unknown-element",
        "image-link",
        "theme-shape-image",
        "invalid-visibility",
        "missing-layout",
    ],
)
def test_ambiguous_or_unsupported_render_inputs_refuse(tmp_path, failure):
    def change(values):
        rels = "ppt/slides/_rels/slide1.xml.rels"
        slide = "ppt/slides/slide1.xml"
        if failure == "external":
            values[rels] = values[rels].replace(
                'Id="layout"', 'TargetMode="External" Id="layout"'
            )
        elif failure == "duplicate-id":
            values[rels] = values[rels].replace('Id="notes"', 'Id="layout"')
        elif failure == "unknown-kind":
            values[rels] = values[rels].replace(
                f"{R}/slideLayout", "https://example.invalid/slideLayout"
            )
        elif failure == "unknown-element":
            values[slide] = values[slide].replace(
                "</p:cSld>", "<p:extLst/></p:cSld>"
            )
        elif failure == "image-link":
            key = "ppt/slideLayouts/slideLayout1.xml"
            values[key] = values[key].replace(
                'r:embed="letter"', 'r:link="letter"'
            )
        elif failure == "theme-shape-image":
            values[slide] = values[slide].replace(
                "<p:spPr/>", '<p:spPr/><p:style><a:fillRef idx="1"/></p:style>'
            )
        elif failure == "invalid-visibility":
            values[slide] = values[slide].replace(
                "<p:sld ", '<p:sld show="unknown" '
            )
        elif failure == "missing-layout":
            del values["ppt/slideLayouts/slideLayout1.xml"]

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    with pytest.raises(ValueError):
        admission(path, tmp_path)


def test_duplicate_package_members_are_rejected_even_with_matching_projection(
    tmp_path,
):
    path = deck(tmp_path / "library-talk-2018.pptx")
    with (
        zipfile.ZipFile(path, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr(
            "ppt/slides/slide1.xml", archive.read("ppt/slides/slide1.xml")
        )
    with pytest.raises(ValueError, match="duplicate package member"):
        admission(path, tmp_path)


def test_missing_projected_image_binding_cannot_be_replaced_by_rendering(
    tmp_path,
):
    path = deck(tmp_path / "library-talk-2018.pptx")
    sources = projected(path, tmp_path)
    sources.sources.clear()
    with pytest.raises(ValueError, match="bytes are unavailable"):
        admit_slides(path.read_bytes(), sources, excluded=lambda *_: False)


@pytest.mark.parametrize("origin", ["master", "theme", "theme-override"])
def test_effective_background_supports_each_declared_inheritance_level(
    tmp_path, origin
):
    path = inherited_background_deck(
        tmp_path / "library-talk-2018.pptx", raster(), origin=origin
    )
    proofs = admission(path, tmp_path)
    assert len(proofs) == 2
    assert all(len(proof.image_part_ids) == 1 for proof in proofs)


@pytest.mark.parametrize("visibility", ["0", "false"])
def test_hidden_slides_keep_their_declared_position_and_sources(
    tmp_path, visibility
):
    def change(values):
        key = "ppt/slides/slide1.xml"
        values[key] = values[key].replace(
            "<p:sld ", f'<p:sld show="{visibility}" '
        )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    proofs = admission(path, tmp_path)
    assert [proof.position for proof in proofs] == [1, 2]
    assert proofs[0].image_part_ids == proofs[1].image_part_ids
    assert proofs[0].placement_ids != proofs[1].placement_ids


def test_standard_package_metadata_and_nonvisual_settings_are_supported(
    tmp_path,
):
    def change(values):
        values["_rels/.rels"] = relationships(
            ("document", "officeDocument", "ppt/presentation.xml", False),
            ("properties", "extended-properties", "docProps/app.xml", False),
        )
        values["_rels/.rels"] = values["_rels/.rels"].replace(
            "</Relationships>",
            '<Relationship Id="thumbnail" Type="http://schemas.openxmlformats.org/'
            'package/2006/relationships/metadata/thumbnail" '
            'Target="docProps/thumbnail.png"/></Relationships>',
        )
        values["docProps/app.xml"] = "<Properties/>"
        values["docProps/thumbnail.png"] = raster()
        for kind, filename, root in (
            (
                "presProps",
                "presProps.xml",
                f'<p:presentationPr xmlns:p="{P}"/>',
            ),
            ("viewProps", "viewProps.xml", f'<p:viewPr xmlns:p="{P}"/>'),
            (
                "tableStyles",
                "tableStyles.xml",
                f'<a:tblStyleLst xmlns:a="{A}"/>',
            ),
        ):
            values[f"ppt/{filename}"] = root
            key = "ppt/_rels/presentation.xml.rels"
            values[key] = values[key].replace(
                "</Relationships>",
                f'<Relationship Id="{kind}" Type="{R}/{kind}" '
                f'Target="{filename}"/></Relationships>',
            )

    proofs = admission(
        deck(tmp_path / "library-talk-2018.pptx", change), tmp_path
    )
    assert "ppt/presProps.xml" in proofs[0].package_parts


def test_direct_image_outside_inventory_media_paths_refuses(tmp_path):
    def change(values):
        values["ppt/artwork/letter.png"] = values["ppt/media/letter.png"]
        slide = "ppt/slides/slide1.xml"
        values[slide] = values[slide].replace(
            "</p:spTree>", picture("outside", 3) + "</p:spTree>"
        )
        rels = "ppt/slides/_rels/slide1.xml.rels"
        values[rels] = values[rels].replace(
            "</Relationships>",
            f'<Relationship Id="outside" Type="{R}/image" '
            'Target="../artwork/letter.png"/></Relationships>',
        )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    with pytest.raises(ValueError, match="no verified paint occurrence"):
        admission(path, tmp_path)


@pytest.mark.parametrize("content", ["supported", "external", "opaque"])
def test_presentation_theme_is_checked_even_outside_the_slide_chain(
    tmp_path, content
):
    def change(values):
        key = "ppt/_rels/presentation.xml.rels"
        values[key] = values[key].replace(
            "</Relationships>",
            f'<Relationship Id="globalTheme" Type="{R}/theme" '
            'Target="theme/global.xml"/></Relationships>',
        )
        values["ppt/theme/global.xml"] = (
            f'<a:theme xmlns:a="{A}"><a:themeElements/></a:theme>'
            if content != "opaque"
            else f'<a:theme xmlns:a="{A}"><a:objectDefaults>'
            "<a:spDef/></a:objectDefaults></a:theme>"
        )
        if content == "external":
            values["ppt/theme/_rels/global.xml.rels"] = relationships(
                ("outside", "image", "https://example.invalid/photo.png", True)
            )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    if content == "supported":
        proofs = admission(path, tmp_path)
        assert all("ppt/theme/global.xml" in p.package_parts for p in proofs)
    else:
        with pytest.raises(ValueError, match="external|unsupported drawing"):
            admission(path, tmp_path)


@pytest.mark.parametrize(
    "failure",
    [
        "alternate-document",
        "theme-effect-image",
        "inherited-group-image",
        "escaping-target",
        "alternate-content",
        "unsupported-image",
    ],
)
def test_opaque_or_ambiguous_package_content_refuses(tmp_path, failure):
    def change(values):
        if failure == "alternate-document":
            values["ppt/other.xml"] = values["ppt/presentation.xml"]
            values["_rels/.rels"] = relationships(
                ("document", "officeDocument", "ppt/other.xml", False)
            )
        elif failure == "theme-effect-image":
            key = "ppt/theme/theme1.xml"
            values[key] = values[key].replace(
                "</a:fmtScheme>",
                '<a:lnStyleLst><a:ln><a:blipFill><a:blip r:embed="letter"/>'
                "</a:blipFill></a:ln></a:lnStyleLst></a:fmtScheme>",
            )
        elif failure == "inherited-group-image":
            key = "ppt/slideLayouts/slideLayout1.xml"
            values[key] = values[key].replace(
                "<p:spTree/>",
                '<p:spTree><p:grpSpPr><a:blipFill><a:blip r:embed="letter"/>'
                "</a:blipFill></p:grpSpPr></p:spTree>",
            )
        elif failure == "escaping-target":
            key = "ppt/slides/_rels/slide1.xml.rels"
            values[key] = values[key].replace(
                "../slideLayouts/slideLayout1.xml",
                "../../../ppt/slideLayouts/slideLayout1.xml",
            )
        elif failure == "alternate-content":
            key = "ppt/slides/slide1.xml"
            values[key] = values[key].replace(
                "</p:cSld>",
                '<AlternateContent xmlns="urn:unsupported"/></p:cSld>',
            )
        elif failure == "unsupported-image":
            values["ppt/media/letter.png"] = (
                b'<svg xmlns="http://www.w3.org/2000/svg"/>'
            )

    path = deck(tmp_path / "library-talk-2018.pptx", change)
    with pytest.raises(ValueError, match="slide render admission refused"):
        admission(path, tmp_path)
