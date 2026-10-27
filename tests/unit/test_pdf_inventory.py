"""PDF source inventory survives reading routes and eligibility filters."""

from __future__ import annotations

import hashlib
import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject

from tests.fixtures import pdfs
from tests.unit.test_pdf_exclusions import (
    BROKEN,
    JPEG_SHA1,
    PERMITTED,
    PERMITTED_PNG,
    scan,
    typed,
)
from tests.unit.test_pdf_opaque_resources import (
    dictionary,
    numbers,
    patterned,
    stream,
)
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import keep_inventory, read_inventory
from yizkr.pipeline.pdf_inventory import inventory_pdf
from yizkr.pipeline.textlayer import PdfExclusionError, png, read_pages


def original_parts(plan, data):
    reference = ArtifactRef.from_bytes(data)
    return [
        part for part in plan.inventory.parts if part.byte_object == reference
    ]


def occurrences(plan, data):
    sources = {part.id for part in original_parts(plan, data)}
    return [p for p in plan.inventory.placements if p.child in sources]


def kept(tmp_path, data, excluded=lambda sha1: False):
    plan = inventory_pdf(data, excluded)
    keep_inventory(plan.inventory, plan.data, tmp_path)
    saved = read_inventory(tmp_path)
    assert saved is not None
    assert saved.source == ArtifactRef.from_bytes(data)
    assert saved.container == "pdf"
    return plan, saved


def repeated_page(*, form=False):
    page = typed()
    place = page.form_image if form else page.image
    place(pdfs.jpeg(), 72, 400, 128, 96)
    name = b"/Fm1" if form else b"/Im1"
    page.ops.append(b"q 128 0 0 96 300 400 cm " + name + b" Do Q")
    return page


def deeply_nested_image_before_shallow_image():
    page = typed().image(pdfs.jpeg(), 72, 400, 128, 96)
    page.image(PERMITTED, 300, 400, 160, 100)
    writer = PdfWriter(io.BytesIO(pdfs.pdf([page])))
    target = writer.pages[0]
    images = target["/Resources"]["/XObject"]
    child = images.raw_get("/Im1")
    name = "Im"
    for _ in range(18):
        child = writer._add_object(
            stream(
                f"/{name} Do".encode(),
                Type=NameObject("/XObject"),
                Subtype=NameObject("/Form"),
                BBox=numbers(0, 0, 1, 1),
                Resources=dictionary(XObject=dictionary(**{name: child})),
            )
        )
        name = "Fm"
    images[NameObject("/Deep")] = child
    target[NameObject("/Contents")] = writer._add_object(
        stream(
            target.get_contents().get_data().replace(b"/Im1 Do", b"/Deep Do")
        )
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_native_annotation_and_unusable_text_routes_keep_placed_sources(
    tmp_path,
):
    plain = typed().image(pdfs.jpeg(), 72, 400, 128, 96)
    annotated = typed().image(pdfs.jpeg(), 72, 400, 128, 96)
    annotated.annotate(
        "/Subtype /FreeText /Rect [72 500 300 540] "
        "/Contents (Wastl and Hilde) /DA (/Helv 12 Tf 0 g)"
    )
    garbled = pdfs.Page().text(["Wastl and Hilde"], font=pdfs.GARBLED)
    garbled.image(pdfs.jpeg(), 72, 400, 128, 96)
    plans = []
    for index, (page, route) in enumerate(
        ((plain, "text-layer"), (annotated, "vision"), (garbled, "vision"))
    ):
        directory = tmp_path / str(index)
        directory.mkdir()
        data = pdfs.pdf([page])
        path = directory / "library-talk-2018.pdf"
        path.write_bytes(data)
        reading = read_pages(path, directory)
        assert [p.method for p in reading.pages] == [route]
        plan, saved = kept(directory / "inventory", data)
        plans.append(plan)
        assert plan.page_count == 1
        (part,) = original_parts(plan, pdfs.JPEG)
        (placement,) = occurrences(plan, pdfs.JPEG)
        assert part.kind == "embedded_resource"
        assert placement.locator.position == 1
        assert placement.locator.path
        assert part.id in {item.id for item in saved.parts}
        assert placement.child == part.id
    assert len({plan.inventory.source.sha1 for plan in plans}) == 3
    assert (
        len(
            {
                original_parts(plan, pdfs.JPEG)[0].byte_object.sha1
                for plan in plans
            }
        )
        == 1
    )
    # Route differences do not remove or combine the image's occurrence.
    assert [len(occurrences(plan, pdfs.JPEG)) for plan in plans] == [1, 1, 1]


@pytest.mark.parametrize("form", [False, True])
def test_repeated_image_or_form_paints_have_distinct_occurrences(form):
    plan = inventory_pdf(pdfs.pdf([repeated_page(form=form)]), lambda _: False)
    (image,) = original_parts(plan, pdfs.JPEG)
    placed = occurrences(plan, pdfs.JPEG)
    assert len(placed) == len({item.id for item in placed}) == 2
    assert {item.child for item in placed} == {image.id}
    assert len({item.locator.path for item in placed}) == 2
    assert [item.locator.position for item in placed] == [1, 1]
    assert placed[0].order < placed[1].order
    assert all(item.layout for item in placed)
    assert image.locator.native_id


def test_filtering_does_not_renumber_or_remove_structural_occurrences():
    decoration = pdfs.gray(8, 8, shade=7)
    page = typed().image(decoration, 20, 20, 8, 8)
    page.image(pdfs.jpeg(), 72, 400, 128, 96)
    page.ops.append(b"q 128 0 0 96 300 400 cm /Im2 Do Q")
    data = pdfs.pdf([page])
    allowed = inventory_pdf(data, lambda _: False)
    excluded = inventory_pdf(data, lambda sha1: sha1 == JPEG_SHA1)
    assert {p.id for p in allowed.inventory.parts} == {
        p.id for p in excluded.inventory.parts
    }
    assert allowed.inventory.placements == excluded.inventory.placements
    assert len(occurrences(allowed, decoration.data)) == 1
    assert len(occurrences(allowed, pdfs.JPEG)) == 2
    (permitted,) = original_parts(allowed, pdfs.JPEG)
    (denied,) = original_parts(excluded, pdfs.JPEG)
    assert permitted.eligibility.state == "allowed"
    assert denied.eligibility.state == "excluded"
    assert denied.eligibility.reason
    assert denied.reading.state != "read"
    excluded.guard(set())
    with pytest.raises(PdfExclusionError):
        excluded.guard({1})


@pytest.mark.parametrize(
    "image, decoded, media_type, size",
    [
        (PERMITTED, PERMITTED_PNG, "image/png", (160, 100)),
        (pdfs.jpeg(flated=True), pdfs.JPEG, "image/jpeg", pdfs.JPEG_SIZE),
    ],
)
def test_original_stream_identity_is_separate_from_decoded_representation(
    tmp_path, image, decoded, media_type, size
):
    data = pdfs.pdf([typed().image(image, 72, 400, *size)])
    plan, saved = kept(tmp_path, data)
    (original,) = original_parts(plan, image.data)
    assert plan.data[original.byte_object.sha1] == image.data
    assert (tmp_path / original.file).read_bytes() == image.data
    assert original.byte_object != ArtifactRef.from_bytes(decoded)
    (representation,) = [
        rep
        for rep in original.representations
        if rep.byte_object == ArtifactRef.from_bytes(decoded)
    ]
    assert representation.method == "pdfium-image"
    assert representation.media_type == media_type
    assert representation.eligibility.state == "allowed"
    assert (tmp_path / representation.file).read_bytes() == decoded
    assert original.locator.native_id
    dictionary = original.layout[0]["pdf_dictionary"]
    assert dictionary["/Width"] == size[0]
    assert dictionary["/Height"] == size[1]
    assert dictionary["/ColorSpace"] == "/DeviceGray"
    assert saved.parts == plan.inventory.parts


def test_existing_decoded_image_exclusions_apply_to_the_original_part():
    data = pdfs.pdf([typed().image(PERMITTED, 72, 400, 160, 100)])
    sha1 = hashlib.sha1(PERMITTED_PNG).hexdigest()
    plan = inventory_pdf(data, lambda candidate: candidate == sha1)
    (part,) = original_parts(plan, PERMITTED.data)
    assert part.byte_object.sha1 != sha1
    assert part.eligibility.state == "excluded"
    assert part.representations
    assert all(
        rep.eligibility.state == "excluded" for rep in part.representations
    )
    assert all(rep.file is None for rep in part.representations)
    with pytest.raises(PdfExclusionError):
        plan.guard({1})


def test_decoration_is_still_subject_to_page_vision_exclusions():
    decoration = pdfs.gray(8, 8, shade=7)
    sha1 = hashlib.sha1(png(8, 8, 1, decoration.data)).hexdigest()
    page = scan().image(decoration, 20, 20, 8, 8)
    plan = inventory_pdf(pdfs.pdf([page]), lambda candidate: candidate == sha1)
    (part,) = original_parts(plan, decoration.data)
    assert part.eligibility.state == "excluded"
    assert len(occurrences(plan, decoration.data)) == 1
    with pytest.raises(PdfExclusionError):
        plan.guard()


def test_undecodable_image_is_retained_and_blocks_only_affected_vision_pages():
    data = pdfs.pdf([scan(), scan(BROKEN)])
    plan = inventory_pdf(data, lambda _: False)
    (broken,) = original_parts(plan, BROKEN.data)
    assert broken.reading.state in {"failed", "unsupported"}
    assert broken.reading.reason
    assert plan.data[broken.byte_object.sha1] == BROKEN.data
    assert len(occurrences(plan, BROKEN.data)) == 1
    plan.guard({1})
    with pytest.raises(PdfExclusionError):
        plan.guard({2})
    with pytest.raises(PdfExclusionError):
        plan.guard()


def test_opaque_pattern_is_explicitly_unsupported_and_cannot_reach_vision():
    writer = PdfWriter(io.BytesIO(pdfs.pdf([scan(pdfs.jpeg())])))
    patterned(writer)
    output = io.BytesIO()
    writer.write(output)
    plan = inventory_pdf(output.getvalue(), lambda _: False)
    assert not plan.inventory.complete
    opaque = [p for p in plan.inventory.parts if p.kind == "opaque_resource"]
    assert opaque
    assert any(part.reading.state == "unsupported" for part in opaque)
    assert any(part.limitations for part in opaque)
    with pytest.raises(PdfExclusionError):
        plan.guard()


def test_missing_native_resource_remains_explicitly_unresolved():
    page = typed()
    page.ops.append(b"q 128 0 0 96 72 400 cm /Missing Do Q")
    plan = inventory_pdf(pdfs.pdf([page]), lambda _: False)
    assert not plan.inventory.complete
    assert plan.inventory.limitations or any(
        part.limitations for part in plan.inventory.parts
    )
    parts = {part.id: part for part in plan.inventory.parts}
    assert all(
        parts[placement.child].reading.state in {"unsupported", "failed"}
        for placement in plan.inventory.placements
    )
    with pytest.raises(PdfExclusionError):
        plan.guard({1})


def test_inline_images_keep_occurrence_locators_without_native_object_ids():
    writer = PdfWriter(io.BytesIO(pdfs.pdf([scan(pdfs.jpeg())])))
    inline = b"BI /W 128 /H 96 /CS /G /BPC 8 /F /DCT ID " + pdfs.JPEG + b" EI"
    content = (
        b"q 128 0 0 96 72 400 cm "
        + inline
        + b" Q q 128 0 0 96 300 400 cm "
        + inline
        + b" Q"
    )
    writer.pages[0][NameObject("/Contents")] = writer._add_object(
        stream(content)
    )
    output = io.BytesIO()
    writer.write(output)
    plan = inventory_pdf(output.getvalue(), lambda _: False)
    placed = occurrences(plan, pdfs.JPEG)
    assert len(placed) == len({item.id for item in placed}) == 2
    assert len({item.locator.path for item in placed}) == 2
    assert all(item.locator.position == 1 for item in placed)


def test_unreadable_pdf_has_a_failed_inventory_and_no_admitted_route(tmp_path):
    data = b"%PDF-1.7\nnot a readable fictional PDF\n%%EOF"
    plan, saved = kept(tmp_path, data)
    assert plan.page_count == 0
    assert not saved.complete
    assert any(part.reading.state == "failed" for part in saved.parts)
    with pytest.raises(PdfExclusionError):
        plan.guard()


def test_unpainted_resource_is_retained_without_an_invented_placement():
    page = typed()
    page.images["Unused"] = pdfs.jpeg()
    plan = inventory_pdf(pdfs.pdf([page]), lambda sha1: sha1 == JPEG_SHA1)
    (part,) = original_parts(plan, pdfs.JPEG)
    assert part.limitations
    assert part.reading.state != "read"
    assert not occurrences(plan, pdfs.JPEG)
    # Checked renderings do not include unused original resources.
    plan.guard()


def invoke_with_plan(operation, path, directory, excluded, plan):
    from yizkr.pipeline import textlayer

    if operation == "check":
        return textlayer.check_pdf_vision(path, excluded, inventory=plan)
    if operation == "read":
        return textlayer.read_pages(
            path, directory, excluded=excluded, inventory=plan
        )
    return textlayer.prepare_pdf_vision(
        path, directory, excluded, inventory=plan
    )


@pytest.mark.parametrize("operation", ["check", "read", "prepare"])
def test_existing_plan_cannot_bypass_the_current_exclusion_policy(
    tmp_path, operation
):
    data = pdfs.pdf([typed(), scan(pdfs.jpeg())])
    plan = inventory_pdf(data, lambda _: False)
    plan.guard()
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(data)
    directory = tmp_path / "out"
    directory.mkdir()
    with pytest.raises(PdfExclusionError):
        invoke_with_plan(
            operation, path, directory, lambda sha1: sha1 == JPEG_SHA1, plan
        )
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("operation", ["check", "read", "prepare"])
def test_plan_for_other_source_bytes_is_refused_before_outputs(
    tmp_path, operation
):
    original = pdfs.pdf([typed(), scan(pdfs.jpeg())])
    replacement = pdfs.pdf([typed(), scan()])
    plan = inventory_pdf(original, lambda _: False)
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(replacement)
    directory = tmp_path / "out"
    directory.mkdir()
    with pytest.raises(PdfExclusionError):
        invoke_with_plan(operation, path, directory, lambda _: False, plan)
    assert list(directory.iterdir()) == []


def test_nested_paints_keep_representations_with_their_native_sources():
    decoration = pdfs.gray(8, 8, shade=7)
    page = typed().image(decoration, 20, 20, 8, 8)
    page.form_image(pdfs.jpeg(), 72, 400, 128, 96)
    page.image(PERMITTED, 300, 400, 160, 100)
    plan = inventory_pdf(pdfs.pdf([page]), lambda _: False)
    for original, decoded in (
        (decoration.data, png(8, 8, 1, decoration.data)),
        (pdfs.JPEG, pdfs.JPEG),
        (PERMITTED.data, PERMITTED_PNG),
    ):
        (part,) = original_parts(plan, original)
        assert {rep.byte_object.sha1 for rep in part.representations} == {
            hashlib.sha1(decoded).hexdigest()
        }
        (placement,) = occurrences(plan, original)
        assert placement.child == part.id


def test_deep_inventory_image_does_not_shift_legacy_shallow_reading(tmp_path):
    data = deeply_nested_image_before_shallow_image()
    plan = inventory_pdf(data, lambda _: False)
    assert len(occurrences(plan, pdfs.JPEG)) == 1
    assert len(occurrences(plan, PERMITTED.data)) == 1
    for original, decoded in (
        (pdfs.JPEG, pdfs.JPEG),
        (PERMITTED.data, PERMITTED_PNG),
    ):
        (part,) = original_parts(plan, original)
        assert {rep.byte_object.sha1 for rep in part.representations} == {
            hashlib.sha1(decoded).hexdigest()
        }

    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(data)
    expected = [JPEG_SHA1, hashlib.sha1(PERMITTED_PNG).hexdigest()]
    # Independent sources come from the full inventory; the shallow native
    # text classifier no longer controls discovery of embedded source parts.
    for mode in ("legacy", "inventory"):
        directory = tmp_path / mode
        directory.mkdir()
        reading = read_pages(
            path,
            directory,
            **(
                {"excluded": lambda _: False, "inventory": plan}
                if mode == "inventory"
                else {}
            ),
        )
        assert reading is not None
        assert [page.method for page in reading.pages] == ["text-layer"]
        assert reading.pages[0].images == list(enumerate(expected, 1))
        assert set(reading.images) == set(expected)
        assert reading.images[expected[1]].path.read_bytes() == PERMITTED_PNG


def test_repeated_source_keeps_each_occurrence_prepared_image(
    tmp_path, monkeypatch
):
    from yizkr.pipeline import textlayer

    # Substitute two prepared views of one real shared native resource to
    # test occurrence mapping independently of PDFium color-space support.
    prepared = (
        PERMITTED_PNG,
        png(160, 100, 1, pdfs.gray(160, 100, shade=93).data),
    )
    results = iter(prepared)
    monkeypatch.setattr(
        textlayer, "image_bytes", lambda _obj: (next(results), ".png")
    )
    data = pdfs.pdf([repeated_page()])
    plan = inventory_pdf(data, lambda _: False)
    (part,) = original_parts(plan, pdfs.JPEG)
    assert len(occurrences(plan, pdfs.JPEG)) == 2
    expected = [hashlib.sha1(data).hexdigest() for data in prepared]
    assert {rep.byte_object.sha1 for rep in part.representations} == set(
        expected
    )
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(data)
    reading = read_pages(
        path, tmp_path, excluded=lambda _: False, inventory=plan
    )
    assert reading is not None
    assert reading.pages[0].images == []
    assert plan.inventory.parts
    assert any(
        "unique" in issue.reason for issue in reading.sources.unresolved
    )
    assert reading.images == {}
    current = next(p for p in plan.inventory.parts if p.id == part.id)
    assert current.owner_binding is None
    assert current.ownership_issue


def test_tightened_policy_withdraws_prepared_whole_pdf_representation(
    tmp_path,
):
    from yizkr.pipeline import textlayer

    data = pdfs.pdf([scan(pdfs.jpeg())])
    plan = inventory_pdf(data, lambda _: False)
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(data)
    output = tmp_path / "prepared"
    output.mkdir()
    textlayer.prepare_pdf_vision(path, output, lambda _: False, inventory=plan)
    (container,) = [
        p for p in plan.inventory.parts if p.kind == "container_source"
    ]
    (composite,) = container.representations
    assert composite.file is not None
    assert composite.eligibility.state == "allowed"
    with pytest.raises(PdfExclusionError):
        textlayer.check_pdf_vision(
            path, lambda sha1: sha1 == JPEG_SHA1, inventory=plan
        )
    assert composite.file is None
    assert composite.eligibility.state == "excluded"
    with pytest.raises(PdfExclusionError):
        plan.guard()
