"""Interpretation context follows verified fictional source placements."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from tests.fixtures import pdfs
from tests.fixtures.example_universe import SOURCES
from tests.fixtures.records import record_answer
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import Eligibility, keep_inventory
from yizkr.pipeline.container_sources import (
    image_page_source,
    image_source_context,
    project_sources,
)
from yizkr.pipeline.interpretation_context import (
    ContainerReadings,
    build_interpretation_context,
)
from yizkr.pipeline.pdf_inventory import inventory_pdf
from yizkr.pipeline.textlayer import read_pages
from yizkr.pipeline.transcribe import (
    PageProvenance,
    PdfPageTranscriber,
    Transcription,
    reading_digest,
)
from yizkr.pipeline.transcription_record import parse_record

SOURCES_BY_KEY = {source.key: source for source in SOURCES}
TREE = SOURCES_BY_KEY["family-tree-1984"].text
TITLE = SOURCES_BY_KEY["family-booklet-2011"].title
FIRST, CONTINUED = TREE.split("d. 1909", 1)
CONTINUED = "d. 1909" + CONTINUED


def scan(image):
    return pdfs.Page().image(image, 40, 60, 400, 600)


def reading(owner, text):
    return Transcription.from_record(
        parse_record(record_answer(text)),
        artifact=owner,
        language="en",
        engine="fictional-reader",
    )


def supplied(tmp_path, pages, texts):
    """Real parser/projection; only model readings are fictional answers."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = pdfs.pdf(pages)
    path = tmp_path / "family-tree-1984.pdf"
    path.write_bytes(data)
    plan = inventory_pdf(data, lambda _sha: False)
    content = read_pages(
        path, tmp_path, excluded=lambda _sha: False, inventory=plan
    )
    assert content is not None
    parent = PdfPageTranscriber(lambda: None)._transcription(
        ArtifactRef.from_bytes(data), content
    )
    sources = content.sources
    keep_inventory(sources.inventory, plan.data, tmp_path)
    images = {}
    for body in sources.bodies:
        found = image_page_source(sources, body)
        if found is None:
            continue
        source, _part, _placement = found
        text = texts[body.locator.position]
        image = reading(source.owner, text)
        image.reading_input = source.current_input.representation
        image.source_context = image_source_context(
            image, [sources.support(part_id) for part_id in source.part_ids]
        )
        images[source.owner.sha1] = image
    return ContainerReadings(sources, parent, images)


def image_at(container, position):
    body = next(
        b for b in container.sources.bodies if b.locator.position == position
    )
    source, part, placement = image_page_source(container.sources, body)
    return container.images[source.owner.sha1], part, placement


def test_continuation_keeps_primary_owner_and_exposes_verified_neighbour(
    tmp_path,
):
    container = supplied(
        tmp_path,
        [scan(pdfs.gray(24, 24)), scan(pdfs.jpeg())],
        {1: FIRST, 2: CONTINUED},
    )
    target, part, placement = image_at(container, 2)
    before = target.model_dump(mode="json")
    context = build_interpretation_context(target, [container])
    assert context.has_context
    assert target.model_dump(mode="json") == before
    assert context.target == target.artifact
    assert context.reading_sha256 == reading_digest(target)
    (group,) = context.groups
    assert [p.position for p in group.passages] == [1, 2]
    assert group.targets[0].id == placement.id
    assert group.passages[1].support.part_id == part.id
    assert group.passages[1].support.reading_sha256 == reading_digest(target)
    assert group.passages[1].text == ""
    assert FIRST in group.passages[0].text
    block = context.prompt_block()
    assert block.startswith("\n\n")
    assert FIRST in block
    assert CONTINUED not in block
    assert "primary reading" in block
    assert not context.gaps


def test_contiguous_context_includes_native_pages_beyond_nearest_neighbour(
    tmp_path,
):
    pages = [pdfs.Page().text([TITLE]) for _ in range(5)]
    pages[2] = scan(pdfs.jpeg())
    container = supplied(tmp_path, pages, {3: CONTINUED})
    target, _, _ = image_at(container, 3)
    context = build_interpretation_context(target, [container])
    (group,) = context.groups
    assert [p.position for p in group.passages] == [1, 2, 3, 4, 5]
    assert [p.kind for p in group.passages] == [
        "native",
        "native",
        "image",
        "native",
        "native",
    ]
    assert all(
        p.owner == container.parent.artifact
        for p in group.passages
        if p.kind == "native"
    )
    assert all(p.text == TITLE for p in group.passages if p.kind == "native")


def test_verified_empty_page_preserves_continuity(tmp_path):
    container = supplied(
        tmp_path,
        [pdfs.Page().text([TITLE]), pdfs.Page(), scan(pdfs.jpeg())],
        {3: CONTINUED},
    )
    assert container.parent.pages[1].provenance.method == "none"
    target, _, _ = image_at(container, 3)
    context = build_interpretation_context(target, [container])
    assert [p.position for p in context.groups[0].passages] == [1, 2, 3]
    assert context.groups[0].passages[1].text == ""
    assert not context.gaps


@pytest.mark.parametrize(
    "boundary",
    [
        "missing",
        "missing-page",
        "excluded",
        "unsupported",
        "unlocated",
        "wrong-input",
    ],
)
def test_unavailable_page_stops_context_without_bridging(tmp_path, boundary):
    container = supplied(
        tmp_path,
        [
            scan(pdfs.gray(24, 24)),
            scan(pdfs.gray(24, 24, shade=7)),
            scan(pdfs.jpeg()),
        ],
        {1: FIRST, 2: TITLE, 3: CONTINUED},
    )
    target, _, _ = image_at(container, 3)
    middle, middle_part, _ = image_at(container, 2)
    if boundary == "missing":
        container = replace(
            container,
            images={
                k: v
                for k, v in container.images.items()
                if k != middle.artifact.sha1
            },
        )
    elif boundary == "missing-page":
        container.parent.pages = [
            page for page in container.parent.pages if page.page != 2
        ]
    elif boundary == "excluded":
        middle_part.eligibility = Eligibility(state="excluded")
        sources = project_sources(container.sources.inventory, tmp_path)
        container = replace(container, sources=sources)
    elif boundary == "unsupported":
        container.parent.pages[1].provenance = PageProvenance(
            method="vision", engine="fictional-reader"
        )
    elif boundary == "wrong-input":
        middle.reading_input = target.reading_input
    else:
        container.parent.pages[1].provenance = None
    context = build_interpretation_context(target, [container])
    assert not context.has_context
    assert [[p.position for p in g.passages] for g in context.groups] == [[3]]
    assert any(gap.position == 2 for gap in context.gaps)
    assert FIRST not in context.prompt_block()


def test_repeated_placements_and_containers_have_deterministic_context(
    tmp_path,
):
    first = supplied(
        tmp_path / "first",
        [scan(pdfs.jpeg()), scan(pdfs.gray(24, 24)), scan(pdfs.jpeg())],
        {1: CONTINUED, 2: FIRST, 3: CONTINUED},
    )
    second = supplied(
        tmp_path / "second",
        [pdfs.Page().text([TITLE]), scan(pdfs.jpeg())],
        {2: CONTINUED},
    )
    target, _, _ = image_at(first, 1)
    a = build_interpretation_context(target, [first, second, first])
    b = build_interpretation_context(target, [second, first])
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    assert a.prompt_block() == b.prompt_block()
    assert len(a.groups) == 2
    assert sorted(len(group.targets) for group in a.groups) == [1, 2]
    assert (
        sum(p.owner == target.artifact for g in a.groups for p in g.passages)
        == 3
    )
    assert all(
        not p.text
        for g in a.groups
        for p in g.passages
        if p.owner == target.artifact
    )


def test_missing_parent_and_conflicting_observations_remain_explicit(tmp_path):
    container = supplied(
        tmp_path,
        [pdfs.Page().text([TITLE]), scan(pdfs.jpeg())],
        {2: CONTINUED},
    )
    target, _, _ = image_at(container, 2)
    absent = replace(container, parent=None, images={})
    context = build_interpretation_context(target, [absent])
    assert not context.has_context
    assert context.gaps and not context.groups
    conflict = build_interpretation_context(target, [container, absent])
    reversed_conflict = build_interpretation_context(
        target, [absent, container]
    )
    assert conflict == reversed_conflict
    assert not conflict.groups
    assert "conflicting" in conflict.gaps[0].reason


def test_current_primary_is_not_replaced_by_another_parent_reading(tmp_path):
    container = supplied(
        tmp_path,
        [pdfs.Page().text([TITLE]), scan(pdfs.jpeg())],
        {2: FIRST},
    )
    old, _, _ = image_at(container, 2)
    target = reading(old.artifact, CONTINUED)
    context = build_interpretation_context(target, [container])
    context.validate_target(target)
    assert context.reading_sha256 == reading_digest(target)
    assert FIRST not in context.prompt_block()
    with pytest.raises(ValueError, match="another reading"):
        context.validate_target(old)


def test_standalone_primary_can_have_a_different_verified_representation(
    tmp_path,
):
    container = supplied(
        tmp_path,
        [pdfs.Page().text([TITLE]), scan(pdfs.jpeg())],
        {2: CONTINUED},
    )
    original, _, _ = image_at(container, 2)
    target = original.model_copy(deep=True)
    encoded = BytesIO()
    with Image.open(BytesIO(pdfs.JPEG)) as image:
        image.save(encoded, format="PNG")
    target.reading_input = ArtifactRef.from_bytes(encoded.getvalue())
    assert target.reading_input != original.reading_input
    context = build_interpretation_context(target, [container])
    assert context.has_context
    context.validate_target(target)
    assert context.target == original.artifact


def test_context_freshness_tracks_neighbour_readings_not_process_metadata(
    tmp_path,
):
    container = supplied(
        tmp_path,
        [scan(pdfs.gray(24, 24)), scan(pdfs.jpeg())],
        {1: FIRST, 2: CONTINUED},
    )
    target, _, _ = image_at(container, 2)
    neighbour, _, _ = image_at(container, 1)
    context = build_interpretation_context(target, [container])
    neighbour.engine = "another-fictional-reader"
    same = build_interpretation_context(target, [container])
    assert context.model_dump(mode="json") == same.model_dump(mode="json")
    changed = reading(neighbour.artifact, TREE)
    changed.reading_input = neighbour.reading_input
    images = dict(container.images)
    images[changed.artifact.sha1] = changed
    newer = build_interpretation_context(
        target, [replace(container, images=images)]
    )
    assert context.model_dump(mode="json") != newer.model_dump(mode="json")
    assert newer.target == context.target
    assert newer.reading_sha256 == context.reading_sha256
    serialized = context.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "fictional-reader" not in serialized


def test_ordinary_insert_does_not_invent_full_page_context(tmp_path):
    page = pdfs.Page().text([TITLE]).image(pdfs.jpeg(), 40, 60, 128, 96)
    container = supplied(tmp_path, [page], {})
    (source,) = container.sources.sources.values()
    target = reading(source.owner, CONTINUED)
    context = build_interpretation_context(target, [container])
    assert not context.groups
    assert not context.has_context


def test_unplaced_presentation_image_does_not_invent_context(tmp_path):
    from tests.unit.test_deck_excerpts import deck
    from yizkr.pipeline.presentation import read_slides
    from yizkr.pipeline.transcribe import DeckTranscriber

    path = deck(
        tmp_path / "library-talk-2018.pptx",
        [((TITLE,), ["image.jpeg"], "")],
        {"image.jpeg": pdfs.JPEG},
        painted={1: []},
    )
    pages = read_slides(path, tmp_path)
    parent = DeckTranscriber(lambda: None)._transcription(
        ArtifactRef.from_path(path), pages
    )
    (source,) = pages.sources.sources.values()
    assert source.readable
    assert not any(p.occurrence == "paint" for p in source.placements)
    target = reading(source.owner, TREE)
    context = build_interpretation_context(
        target,
        [
            ContainerReadings(
                pages.sources, parent, {source.owner.sha1: target}
            )
        ],
    )
    assert not context.groups
    assert not context.has_context
