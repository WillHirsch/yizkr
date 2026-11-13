"""Only bounded, admitted text arrangements become automatic vision."""

import io
import zipfile

import pytest

from tests.fixtures import slides
from tests.unit.test_slide_admission import projected
from yizkr.claims import ArtifactRef
from yizkr.pipeline import slide_routing
from yizkr.pipeline.slide_admission import (
    UnsupportedSlideRendering,
    admit_slides,
)


def allowed(_sha, _names=()):
    return False


def edited(original, change):
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        parts = {
            name: archive.read(name).decode()
            if name.endswith((".xml", ".rels"))
            else archive.read(name)
            for name in archive.namelist()
        }
    change(parts)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in parts.items():
            archive.writestr(
                zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data
            )
    return output.getvalue()


def metadata_extension(parts):
    """An opaque fictional extension with a resolved internal metadata edge."""
    key = "ppt/presentation.xml"
    parts[key] = parts[key].replace(
        "</p:presentation>",
        '<p:extLst><p:ext uri="urn:example:review-metadata">'
        '<example:metadata xmlns:example="urn:example:slide-extension" '
        'r:id="metadata"/></p:ext></p:extLst></p:presentation>',
    )
    key = "ppt/_rels/presentation.xml.rels"
    parts[key] = parts[key].replace(
        "</Relationships>",
        '<Relationship Id="metadata" '
        'Type="https://example.invalid/relationships/metadata" '
        'Target="metadata.xml"/></Relationships>',
    )
    parts["ppt/metadata.xml"] = '<metadata xmlns="urn:example:metadata"/>'


def inputs(tmp_path, original=None):
    original = slides.layout_presentation() if original is None else original
    path = tmp_path / "library-talk-2018.pptx"
    path.write_bytes(original)
    sources = projected(path, tmp_path)
    return original, sources


def plan(tmp_path, original=None):
    original, sources = inputs(tmp_path, original)
    return slide_routing.plan_slide_layout(original, sources, excluded=allowed)


@pytest.mark.parametrize(
    "arrangement,reason",
    [
        ("columns", "spatial-text-layout"),
        ("grid", "spatial-text-layout"),
        ("reversed", "xml-order"),
    ],
)
def test_clear_text_arrangements_use_vision_in_physical_slide_order(
    tmp_path, arrangement, reason
):
    reading = plan(
        tmp_path, slides.layout_presentation(arrangement=arrangement)
    )
    assert reading.positions == (1,)
    assert reading.slides[0].reason == reason
    assert reading.slides[0].limitations == ()
    assert [s.route for s in reading.slides] == [
        "vision",
        "slide-xml",
        "slide-xml",
    ]
    assert [s.position for s in reading.slides] == [1, 2, 3]
    assert reading.configuration()["positions"] == [1]
    assert "Klara" not in str(reading.configuration())


@pytest.mark.parametrize(
    "arrangement,order",
    [
        ("simple", None),
        ("reversed", (0, 1)),
    ],
)
def test_ordinary_native_text_needs_no_vision(tmp_path, arrangement, order):
    reading = plan(
        tmp_path,
        slides.layout_presentation(arrangement=arrangement, order=order),
    )
    assert reading.positions == ()
    assert all(
        s.reason == "no-layout-trigger" and not s.limitations
        for s in reading.slides
    )


def test_spatial_relationship_is_not_lost_when_xml_order_already_matches(
    tmp_path,
):
    reading = plan(tmp_path, slides.layout_presentation(order=(0, 1, 2, 3)))
    assert reading.positions == (1,)


def test_notes_never_select_a_slide_for_vision(tmp_path):
    original = slides.layout_presentation(
        arrangement="simple", notes_text="Klara\n1938\nGustav\n1939"
    )
    assert plan(tmp_path, original).positions == ()


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda v: v.replace(
                '<a:xfrm><a:off x="500000"',
                '<a:xfrm rot="5400000"><a:off x="500000"',
            ),
            "unsupported-geometry",
        ),
        (
            lambda v: v.replace('<a:off x="4500000"', '<a:off x="500000"'),
            "overlapping-text",
        ),
        (
            lambda v: v.replace(
                '<a:bodyPr anchor="ctr"', '<a:bodyPr vert="vert" anchor="ctr"'
            ),
            "unsupported-text-flow",
        ),
        (
            lambda v: v.replace(
                '<a:bodyPr anchor="ctr"', '<a:bodyPr numCol="2" anchor="ctr"'
            ),
            "unsupported-text-flow",
        ),
        (
            lambda v: v.replace(
                '<p:cNvPr id="2"', '<p:cNvPr hidden="1" id="2"'
            ),
            "hidden-shapes",
        ),
        (lambda v: v.replace('prst="rect"', 'prst="rightArrow"'), "drawings"),
        (
            lambda v: v.replace("</p:spTree>", "<p:cxnSp/></p:spTree>"),
            "drawings",
        ),
        (
            lambda v: v.replace("</p:spTree>", "<p:grpSp/></p:spTree>"),
            "grouped-shapes",
        ),
        (
            lambda v: v.replace('<a:off x="500000" y="1000000"/>', ""),
            "unsupported-geometry",
        ),
    ],
)
def test_unhandled_local_layout_stays_native_with_gap(tmp_path, change, code):
    def mutate(parts):
        key = "ppt/slides/slide3.xml"
        parts[key] = change(parts[key])

    reading = plan(tmp_path, edited(slides.layout_presentation(), mutate))
    assert reading.positions == ()
    assert reading.slides[0].reason == "unsupported-layout"
    assert code in reading.slides[0].limitations
    assert reading.slides[1].reason == "no-layout-trigger"


def test_simple_mixed_native_body_has_no_layout_trigger_or_gap(tmp_path):
    reading = plan(tmp_path, slides.presentation())
    assert reading.positions == ()
    assert reading.policy == "layout-v2"
    assert reading.slides[0].reason == "no-layout-trigger"
    assert reading.slides[0].limitations == ()


@pytest.mark.parametrize("placement", ["direct", "repeated", "background"])
def test_mixed_native_grid_is_selected_with_original_image_owners_preserved(
    tmp_path, placement
):
    original, sources = inputs(
        tmp_path, slides.mixed_layout_presentation(placement=placement)
    )
    admission = admit_slides(original, sources, excluded=allowed)
    image = next(
        p for p in sources.inventory.parts if p.kind == "embedded_resource"
    )
    assert admission[0].image_part_ids == [image.id]
    assert len(admission[0].placement_ids) == (
        2 if placement == "repeated" else 1
    )
    reading = slide_routing.plan_slide_layout(
        original, sources, excluded=allowed
    )
    assert reading.positions == (1,)
    assert reading.slides[0].reason == "spatial-text-layout"
    assert reading.slides[0].limitations == ()


@pytest.mark.parametrize(
    "kind,code",
    [
        ("drawing", "render-drawing"),
        ("inherited", "render-inheritance"),
    ],
)
def test_known_rendering_limits_are_typed_and_diagnostic(tmp_path, kind, code):
    def change(parts):
        key = (
            "ppt/slides/slide3.xml"
            if kind == "drawing"
            else "ppt/slideMasters/slideMaster1.xml"
        )
        shape = "<p:graphicFrame/>" if kind == "drawing" else "<p:sp/>"
        if "</p:spTree>" in parts[key]:
            parts[key] = parts[key].replace(
                "</p:spTree>", shape + "</p:spTree>"
            )
        else:
            parts[key] = parts[key].replace(
                "<p:spTree/>", "<p:spTree>" + shape + "</p:spTree>"
            )

    original, sources = inputs(
        tmp_path, edited(slides.layout_presentation(), change)
    )
    with pytest.raises(UnsupportedSlideRendering) as caught:
        admit_slides(original, sources, excluded=allowed)
    assert caught.value.limitation == code
    reading = slide_routing.plan_slide_layout(
        original, sources, excluded=allowed
    )
    assert reading.positions == ()
    assert all(
        s.reason == "unsupported-rendering" and code in s.limitations
        for s in reading.slides
    )


def test_opaque_extension_relation_keeps_native_reading_without_rendering(
    tmp_path, monkeypatch
):
    from yizkr.pipeline import slide_renderer
    from yizkr.pipeline.media_stages import MediaReadRequest, read_media
    from yizkr.pipeline.slide_reading import SlideVisionRequest

    original, sources = inputs(
        tmp_path, edited(slides.layout_presentation(), metadata_extension)
    )
    with pytest.raises(UnsupportedSlideRendering) as caught:
        admit_slides(original, sources, excluded=allowed)
    assert caught.value.limitation == "render-relationship"

    def no_visual_work(*args, **kwargs):
        pytest.fail("unsupported extension must not render or start a model")

    monkeypatch.setattr(slide_renderer, "renderer_identity", no_visual_work)
    monkeypatch.setattr(slide_renderer, "render_slides", no_visual_work)
    with read_media(
        MediaReadRequest(
            tmp_path / "library-talk-2018.pptx",
            "paged",
            excluded=allowed,
            slide_vision=SlideVisionRequest(
                (), tmp_path / "renders", mode="layout"
            ),
        ),
        no_visual_work,
    ) as result:
        assert all(
            page.provenance.method == "slide-xml"
            for page in result.transcription.pages
        )
        decisions = result.dependencies["slide_routing"]
        assert decisions["positions"] == []
        assert all(
            slide["reason"] == "unsupported-rendering"
            and "render-relationship" in slide["limitations"]
            for slide in decisions["slides"]
        )
        assert result.attention


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("unresolved", "unresolved relationship"),
        ("external", "external relationship"),
        ("known-id", "unsupported relationship use"),
        ("image-binding", "binding|unavailable|unresolved"),
        ("image-excluded", "excluded"),
    ],
)
def test_opaque_extension_cannot_mask_unsafe_relationships_or_sources(
    tmp_path, fault, reason
):
    def change(parts):
        metadata_extension(parts)
        key = "ppt/presentation.xml"
        if fault == "unresolved":
            parts[key] = parts[key].replace(
                'r:id="metadata"', 'r:id="missing"'
            )
        elif fault == "known-id":
            parts[key] = parts[key].replace('r:id="rId1"', 'r:id="metadata"')
        elif fault == "external":
            key = "ppt/slides/_rels/slide3.xml.rels"
            parts[key] = parts[key].replace(
                "</Relationships>",
                f'<Relationship Id="external" Type="{slides.R}/image" '
                'Target="https://example.invalid/picture.png" '
                'TargetMode="External"/></Relationships>',
            )

    original, sources = inputs(tmp_path, edited(slides.presentation(), change))
    excluded = allowed
    if fault.startswith("image-"):
        part = next(p for p in sources.inventory.parts if p.owner_binding)
        if fault == "image-binding":
            part.owner_binding = None
        else:

            def excluded(sha, names):
                return sha == part.byte_object.sha1

    with pytest.raises(ValueError, match=reason) as caught:
        slide_routing.plan_slide_layout(original, sources, excluded=excluded)
    assert not isinstance(caught.value, UnsupportedSlideRendering)


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "body",
        "excluded",
        "image-binding",
        "image-excluded",
        "external",
    ],
)
def test_integrity_and_policy_failures_never_become_native_gap(
    tmp_path, fault
):
    original = (
        slides.presentation()
        if fault.startswith("image-")
        else slides.layout_presentation()
    )
    if fault == "external":

        def change(parts):
            key = "ppt/slides/_rels/slide3.xml.rels"
            parts[key] = parts[key].replace(
                "</Relationships>",
                f'<Relationship Id="external" Type="{slides.R}/image" '
                'Target="https://example.invalid/picture.png" '
                'TargetMode="External"/></Relationships>',
            )

        original = edited(original, change)
    original, sources = inputs(tmp_path, original)
    excluded = allowed
    if fault == "source":
        sources.inventory.source = ArtifactRef.from_bytes(
            b"another fictional presentation"
        )
    elif fault == "body":
        sources.bodies[0].byte_object = ArtifactRef.from_bytes(
            b"wrong slide body"
        )
    elif fault == "excluded":

        def excluded(sha, names):
            return sha == sources.inventory.source.sha1
    elif fault.startswith("image-"):
        part = next(p for p in sources.inventory.parts if p.owner_binding)
        if fault == "image-binding":
            part.owner_binding = None
        else:

            def excluded(sha, names):
                return sha == part.byte_object.sha1

    with pytest.raises(ValueError) as caught:
        slide_routing.plan_slide_layout(original, sources, excluded=excluded)
    assert not isinstance(caught.value, UnsupportedSlideRendering)


def test_generic_admission_failure_is_not_downgraded(tmp_path, monkeypatch):
    original, sources = inputs(tmp_path)

    def refuse(*args, **kwargs):
        raise ValueError("source bindings changed")

    monkeypatch.setattr(slide_routing, "admit_slides", refuse)
    with pytest.raises(ValueError, match="bindings changed"):
        slide_routing.plan_slide_layout(original, sources, excluded=allowed)


@pytest.mark.parametrize(
    "fault",
    [
        "external",
        "missing-target",
        "unsafe-target",
        "declaration",
        "image-binding",
        "image-excluded",
        "note-binding",
        "note-excluded",
    ],
)
def test_earlier_known_limit_cannot_hide_later_input_failure(tmp_path, fault):
    def change(parts):
        # This valid but unsupported edge is encountered before slide inputs.
        key = "_rels/.rels"
        target = "ppt/presentation.xml"
        if fault == "missing-target":
            target = "ppt/missing.xml"
        elif fault == "unsafe-target":
            target = "../outside.xml"
        parts[key] = parts[key].replace(
            "</Relationships>",
            f'<Relationship Id="unsupported" Type="{slides.R}/unknown" '
            f'Target="{target}"/></Relationships>',
        )
        if fault == "external":
            key = "ppt/slides/_rels/slide3.xml.rels"
            parts[key] = parts[key].replace(
                "</Relationships>",
                f'<Relationship Id="external" Type="{slides.R}/image" '
                'Target="https://example.invalid/picture.png" '
                'TargetMode="External"/></Relationships>',
            )
        elif fault == "declaration":
            parts["ppt/unused.xml"] = "<!DOCTYPE unused><unused/>"
        # Make the early edge a supported root metadata relationship for the
        # later binding cases, with the known limit in inherited paint.
        if fault.startswith(("image-", "note-")):
            parts["_rels/.rels"] = parts["_rels/.rels"].replace(
                f'Type="{slides.R}/unknown"',
                f'Type="{slides.R}/extended-properties"',
            )
            key = "ppt/slideMasters/slideMaster1.xml"
            parts[key] = parts[key].replace(
                "<p:spTree/>", "<p:spTree><p:sp/></p:spTree>"
            )

    original, sources = inputs(tmp_path, edited(slides.presentation(), change))
    excluded = allowed
    if fault == "image-binding":
        next(
            p for p in sources.inventory.parts if p.owner_binding
        ).owner_binding = None
    elif fault == "note-binding":
        sources.notes[0].text = "changed native notes"
    elif fault.endswith("-excluded"):
        part = (
            sources.notes[0]
            if fault == "note-excluded"
            else next(p for p in sources.inventory.parts if p.owner_binding)
        )

        def excluded(sha, names):
            return sha == part.byte_object.sha1

    with pytest.raises(ValueError) as caught:
        slide_routing.plan_slide_layout(original, sources, excluded=excluded)
    assert not isinstance(caught.value, UnsupportedSlideRendering)


def test_persisted_plan_uses_only_known_codes(tmp_path):
    reading = plan(tmp_path)
    assert (
        slide_routing.SlideRoutingPlan.model_validate(reading.configuration())
        == reading
    )
    unknown = reading.configuration()
    unknown["slides"][0]["reason"] = "unrecognized"
    with pytest.raises(ValueError, match="unknown slide routing code"):
        slide_routing.SlideRoutingPlan.model_validate(unknown)
