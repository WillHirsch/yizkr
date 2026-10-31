"""Native container provenance reaches registration and claim references."""

from __future__ import annotations

import copy
import json

import pytest

from tests.fixtures import pdfs
from tests.fixtures.identity import write_mapped
from tests.unit.test_pipeline import PROPERTIES, FakeWiki
from tests.unit.test_printed_pages import BOOKLET_REF, booklet_batch
from tests.unit.test_registrar import (
    IMAGE,
    Registrar,
    StoringWiki,
    assert_additive,
    deck_file,
    recorded,
)
from yizkr.claims import (
    ArtifactRef,
    PlacementSupport,
    SourceReference,
    SourceSupport,
    read_placed,
    source_support_locators,
)
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    ContainerInventory,
    Eligibility,
    MediaOwnerBinding,
    Placement,
    SourcePart,
    keep_inventory,
    structural_id,
)
from yizkr.pipeline.container_sources import prepare_sources, project_sources
from yizkr.pipeline.pdf_inventory import inventory_pdf
from yizkr.pipeline.writer import BatchWriter
from yizkr.source_structure import StructuralLocator


def support(kind="slide_body", *, repeats=0):
    locator = StructuralLocator(
        kind=kind,
        package_part=(
            "ppt/notesSlides/notesSlide1.xml"
            if kind == "speaker_notes"
            else "ppt/slides/slide1.xml"
        ),
        position=1,
    )
    places = [
        StructuralLocator(kind="placement", position=1, path=f"/pic[{n}]")
        for n in range(1, repeats + 1)
    ]
    return SourceSupport(
        container=BOOKLET_REF,
        part_id=structural_id(BOOKLET_REF, locator),
        locator=locator,
        placements=[
            PlacementSupport(id=structural_id(BOOKLET_REF, p), locator=p)
            for p in places
        ],
        reading_sha256="a" * 64,
    )


def common_record(directory, source, *, repeats=2, excluded=False):
    image = ArtifactRef.from_bytes(IMAGE)
    body_location = StructuralLocator(kind="slide_body", position=1)
    image_location = StructuralLocator(
        kind="embedded_resource", package_part="ppt/media/image1.png"
    )
    body = SourcePart(
        id=structural_id(source, body_location),
        kind="slide_body",
        locator=body_location,
    )
    file = f"container-parts/{image.sha1}.png"
    part = SourcePart(
        id=structural_id(source, image_location),
        kind="embedded_resource",
        locator=image_location,
        byte_object=image,
        file=file,
        media_type="image/png",
        eligibility=Eligibility(state="excluded" if excluded else "allowed"),
        source_role="independent",
        owner_binding=MediaOwnerBinding(
            artifact=image,
            file=file,
            media_type="image/png",
            basis="original-media",
        ),
    )
    placements = []
    for number in range(1, repeats + 1):
        locator = StructuralLocator(
            kind="placement", position=1, path=f"/pic[{number}]"
        )
        placements.append(
            Placement(
                id=structural_id(source, locator),
                parent=body.id,
                child=part.id,
                locator=locator,
                order=number,
                occurrence="paint",
            )
        )
    inventory = ContainerInventory(
        source=source,
        container="presentation",
        parts=[body, part],
        placements=placements,
    )
    keep_inventory(inventory, {image.sha1: IMAGE}, directory)
    return inventory


def test_support_preserves_old_serialization_and_is_deterministic():
    source = SourceReference(artifact=BOOKLET_REF, page_number="1")
    assert "support" not in source.model_dump()
    assert "support" not in SourceReference.model_json_schema()["properties"]
    located = source.model_copy(update={"support": support(repeats=2)})
    restored = SourceReference.model_validate_json(located.model_dump_json())
    assert restored.support == located.support
    values = [
        json.loads(value)
        for value in source_support_locators(restored.support)
    ]
    assert len(values) == 2
    assert {value["placement"] for value in values} == {
        place.id for place in restored.support.placements
    }
    assert all(value["container"] == BOOKLET_REF.sha1 for value in values)


def test_writer_preserves_body_and_notes_as_distinct_support_on_one_owner():
    wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
    batch = booklet_batch(
        {"page_number": "1", "support": support().model_dump()},
        {"page_number": "1", "support": support("speaker_notes").model_dump()},
    )
    result = write_mapped(BatchWriter(wiki), batch)
    (statement,) = wiki.wbi.item.get(result.created_items["e1"]).claims.get(
        PROPERTIES["sibling"]
    )
    values = [
        json.loads(snak.datavalue["value"])
        for reference in statement.references.references
        for snak in reference.snaks.get(PROPERTIES["source_part_locator"])
        or ()
    ]
    assert {value["kind"] for value in values} == {
        "slide_body",
        "speaker_notes",
    }
    assert {value["part"] for value in values} == {
        source["support"]["part_id"]
        for source in [claim.source.model_dump() for claim in batch.claims]
    }
    before = statement.get_json()["references"]
    again = write_mapped(BatchWriter(wiki), batch)
    assert again.written == again.referenced == 0
    assert (
        wiki.wbi.item.get(result.created_items["e1"])
        .claims.get(PROPERTIES["sibling"])[0]
        .get_json()["references"]
        == before
    )


def test_writer_refuses_overlong_native_locator_without_truncating_it():
    located = support()
    located.locator.kind = "x" * 500
    wiki = FakeWiki({BOOKLET_REF.sha1: "Q23"}, detached=True)
    result = write_mapped(
        BatchWriter(wiki),
        booklet_batch({"page_number": "1", "support": located.model_dump()}),
    )
    assert result.written == 0
    assert len(result.refused) == 1
    assert any("source-part locator" in warning for warning in result.warnings)


def test_common_registration_enriches_existing_owner_without_duplicates(
    tmp_path,
):
    wiki = StoringWiki()
    path = deck_file(tmp_path, "library-talk-2018")
    registrar = Registrar(wiki)
    registrar.register_artifact(path, "presentation", "Library Talk 2018")
    legacy, directory = recorded(
        tmp_path, path, "Library Talk 2018", [("image1.png", IMAGE, [1])]
    )
    before_mapping = registrar.register_derived_media(legacy, directory)
    image = ArtifactRef.from_bytes(IMAGE)
    qid = before_mapping[image.sha1]
    before = copy.deepcopy(wiki.entities[qid])
    inventory = common_record(directory, ArtifactRef.from_path(path))
    sources = project_sources(inventory, directory)
    assert (
        registrar.register_container_sources(
            sources, directory, title="Library Talk 2018"
        )
        == before_mapping
    )
    after = wiki.entities[qid]
    assert_additive(before, after)
    (part_of,) = after["claims"][wiki.PROPERTIES["part_of"]]
    locators = [
        json.loads(snak["datavalue"]["value"])
        for reference in part_of["references"]
        for snak in reference["snaks"].get(
            wiki.PROPERTIES["source_part_locator"], []
        )
    ]
    assert {value["placement"] for value in locators} == {
        placement.id for placement in inventory.placements
    }
    assert len(locators) == 2
    writes = wiki.writes
    assert (
        registrar.register_container_sources(
            sources, directory, title="Library Talk 2018"
        )
        == before_mapping
    )
    assert wiki.writes == writes


def prepared_pdf(tmp_path):
    path = tmp_path / "library-talk-2018.pdf"
    path.write_bytes(
        pdfs.pdf(
            [
                pdfs.Page()
                .text(["Wastl to Klara, July 1939."])
                .image(pdfs.gray(160, 100), 72, 400, 160, 100)
            ]
        )
    )
    plan = inventory_pdf(path.read_bytes(), lambda _: False)
    directory = tmp_path / "state"
    inventory = prepare_sources(plan.inventory, plan.data, directory)
    keep_inventory(inventory, plan.data, directory)
    sources = project_sources(inventory, directory)
    (owner,) = sources.sources.values()
    assert not sources.unresolved
    assert {
        part.owner_binding.basis
        for part in inventory.parts
        if part.id in owner.part_ids
    } == {"prepared-part"}
    return path, directory, sources, owner


def test_prepared_image_can_repeat_its_own_recorded_graph_binding(tmp_path):
    wiki = StoringWiki()
    path, directory, sources, owner = prepared_pdf(tmp_path)
    registrar = Registrar(wiki)
    registrar.register_artifact(path, "artifact", "Library Talk 2018")
    registered = registrar.register_container_sources(
        sources, directory, title="Library Talk 2018"
    )
    assert set(registered) == {owner.owner.sha1}
    before = copy.deepcopy(wiki.entities)
    uploads = copy.deepcopy(wiki.media.versions)
    # A fresh registrar must establish reuse from persisted graph evidence.
    assert (
        Registrar(wiki).register_container_sources(
            sources, directory, title="Library Talk 2018"
        )
        == registered
    )
    assert wiki.entities == before
    assert wiki.media.versions == uploads


@pytest.mark.parametrize("existing_composition", [False, True])
def test_prepared_alias_cannot_adopt_an_unrelated_graph_owner(
    tmp_path, existing_composition
):
    wiki = StoringWiki()
    path, directory, sources, owner = prepared_pdf(tmp_path)
    registrar = Registrar(wiki)
    registrar.register_artifact(path, "artifact", "Library Talk 2018")
    if existing_composition:
        # A matching image in another container is not proof that the
        # generated pixels stand for the same original source part.
        other = deck_file(tmp_path, "other-library-talk")
        registrar.register_artifact(other, "presentation", "Library Talk")
        inventory = common_record(
            tmp_path / "other", ArtifactRef.from_path(other)
        )
        other_part = inventory.parts[-1]
        other_part.byte_object = owner.owner
        other_part.file = f"container-parts/{owner.owner.sha1}.png"
        other_part.owner_binding.artifact = owner.owner
        other_part.owner_binding.file = other_part.file
        keep_inventory(
            inventory,
            {owner.owner.sha1: (directory / owner.file).read_bytes()},
            tmp_path / "other",
        )
        registrar.register_container_sources(
            project_sources(inventory, tmp_path / "other"),
            tmp_path / "other",
            title="Library Talk",
        )
    else:
        registrar.register_artifact(
            directory / owner.file, "photograph", "Library Talk image"
        )
    before = copy.deepcopy(wiki.entities)
    uploads = copy.deepcopy(wiki.media.versions)
    with pytest.raises(ValueError, match="original source-part binding"):
        Registrar(wiki).register_container_sources(
            sources, directory, title="Library Talk 2018"
        )
    assert wiki.entities == before
    assert wiki.media.versions == uploads


def test_verified_legacy_binding_can_enrich_an_existing_owner(tmp_path):
    wiki = StoringWiki()
    path, directory, sources, owner = prepared_pdf(tmp_path)
    registrar = Registrar(wiki)
    registrar.register_artifact(path, "artifact", "Library Talk 2018")
    prior = registrar.register_artifact(
        directory / owner.file, "photograph", "Library Talk image"
    )
    # This test supplies the result of the separately verified compatibility
    # adapter; the registrar must not reinterpret that established identity.
    for part in sources.inventory.parts:
        if part.id in owner.part_ids:
            part.owner_binding.basis = "verified-legacy-reading"
    before = copy.deepcopy(wiki.entities[prior.artifact_qid])
    assert registrar.register_container_sources(
        sources, directory, title="Library Talk 2018"
    ) == {owner.owner.sha1: prior.artifact_qid}
    assert_additive(before, wiki.entities[prior.artifact_qid])


def test_common_placed_input_is_primary_and_never_downgrades_invalid_data(
    tmp_path,
):
    source = ArtifactRef.from_bytes(b"fictional library-talk-2018 container")
    inventory = common_record(tmp_path, source)
    image = ArtifactRef.from_bytes(IMAGE)
    assert read_placed([str(tmp_path)]) == {
        source.sha1: frozenset({image.sha1})
    }
    assert read_placed([str(tmp_path / CONTAINER_INVENTORY)]) == read_placed(
        [str(tmp_path)]
    )
    # A valid metadata-only compatibility record must not mask a damaged
    # common record; otherwise stale ownership could regain admission.
    (tmp_path / "derived-media.json").write_text(
        json.dumps(
            {
                "source_sha1": source.sha1,
                "title": "Library Talk 2018",
                "media": {
                    image.sha1: {
                        "file": f"derived-media/{image.sha1}.png",
                        "names": ["album.png"],
                        "slides": [1],
                    }
                },
            }
        )
    )
    assert read_placed([str(tmp_path / "derived-media.json")]) == {
        source.sha1: frozenset({image.sha1})
    }
    (tmp_path / inventory.parts[-1].file).unlink()
    with pytest.raises(ValueError, match="reconstruct"):
        read_placed([str(tmp_path)])


def test_excluded_common_source_is_not_a_placed_owner(tmp_path):
    source = ArtifactRef.from_bytes(b"fictional library-talk-2018 container")
    common_record(tmp_path, source, excluded=True)
    assert read_placed([str(tmp_path)]) == {source.sha1: frozenset()}
