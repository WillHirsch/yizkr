"""Freshness decisions over fictional inputs and isolated package resources."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.fixtures.semantic_acceptance import source_case
from yizkr.pipeline import stage_planning as planning
from yizkr.pipeline.stage_planning import (
    DOCUMENT_STAGES,
    MODEL_ROLES,
    PHASES,
    PROCESSING_RESOURCES,
    STAGE_ROLES,
    STAGES,
    VIEWS,
    batch_stage,
    plan_view,
    processing_dependencies,
    stages_for,
)


def test_gedcom_keeps_its_deterministic_process_and_extract_refresh_role():
    assert DOCUMENT_STAGES["gedcom"] == ("gedcom",)
    assert stages_for("gedcom") == (batch_stage("gedcom"),)
    assert STAGES["gedcom"].process == "deterministic"
    assert STAGE_ROLES["gedcom"] == "extract"
    assert PHASES["gedcom"] == PHASES["extraction"]
    assert VIEWS["gedcom"] == VIEWS["extraction"] == "batch.json"
    assert MODEL_ROLES == ("transcribe", "extract", "critique")
    assert DOCUMENT_STAGES["text"] == (
        "transcription",
        "extraction",
        "critique",
    )
    assert STAGES["extraction"].input_artifacts == (
        "transcription.json",
        "preparation.pointer.txt",
        "interpretation-context.json",
    )


@pytest.mark.parametrize("kind", DOCUMENT_STAGES)
def test_each_document_plan_declares_one_batch_producer(kind):
    producer = batch_stage(kind)
    assert producer in stages_for(kind)
    assert producer.artifact == "batch.json"
    assert producer.role == "extract"


@pytest.mark.parametrize(
    "resource",
    [
        "pipeline/gedcom.py",
        "pipeline/gedcom_stages.py",
        "pipeline/gedcom_placeholders.yaml",
    ],
)
def test_gedcom_adapter_and_policy_changes_invalidate_its_processing(
    package, resource
):
    def observed():
        return processing_dependencies(
            "gedcom",
            package=package,
            source_sha1="1" * 40,
            configuration={},
        )

    before = observed()
    path = package / resource
    path.write_text(path.read_text() + "changed fictional adapter policy\n")
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=observed(),
    )
    assert plan.action == "recompute"
    assert plan.changed_dependencies == (f"processing.resources.{resource}",)


def test_text_operations_and_prompts_do_not_invalidate_gedcom(package):
    def observed():
        return processing_dependencies(
            "gedcom",
            package=package,
            source_sha1="1" * 40,
            configuration={},
        )

    before = observed()
    for resource in (
        "pipeline/text_stages.py",
        "pipeline/prompts/extraction.md",
        "pipeline/prompts/critique.md",
    ):
        path = package / resource
        path.write_text(path.read_text() + "changed fictional model policy\n")
    assert observed() == before


def test_current_inputs_and_processing_reuse_without_mutating_metadata():
    inputs = {"transcription.json": source_case("naturalisation-1952").sha1}
    processing = {"configuration": {"model": "authored-fictional-answers"}}
    before = deepcopy((inputs, processing))

    plan = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs,
        recorded_processing=processing,
        processing=processing,
    )

    assert plan.action == "reuse"
    assert plan.reasons == plan.changed_dependencies == ()
    assert (inputs, processing) == before


@pytest.mark.parametrize(
    ("exists", "refresh", "reason"),
    [(False, False, "missing-output"), (True, True, "refresh")],
)
def test_missing_output_and_explicit_refresh_recompute(
    exists, refresh, reason
):
    processing = {"format": 1, "stage": "extraction"}
    plan = plan_view(
        exists=exists,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=processing,
        processing=processing,
        refresh=refresh,
    )
    assert plan.action == "recompute"
    assert plan.reasons == (reason,)


def test_a_view_without_recorded_processing_recomputes():
    inputs = {"transcription.json": source_case("naturalisation-1952").sha1}
    legacy = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs,
        processing={"format": 1, "stage": "extraction"},
    )
    assert legacy.action == "recompute"
    assert legacy.reasons == ("missing-processing",)
    assert legacy.changed_dependencies == ("processing",)


def test_no_current_processing_descriptor_never_reuses_a_view():
    inputs = {"transcription.json": source_case("naturalisation-1952").sha1}
    plan = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs,
        recorded_processing={"format": 1, "stage": "extraction"},
    )
    assert plan.action == "recompute"


@pytest.mark.parametrize("broken", ["corrupt", [1], 1])
def test_malformed_persisted_dependencies_cannot_establish_reuse(broken):
    plan = plan_view(
        exists=True,
        recorded_inputs=broken,
        current_inputs={"transcription.json": "reading"},
        recorded_processing=broken,
        processing={"format": 1},
    )
    assert plan.action == "recompute"
    assert "inputs-changed" in plan.reasons
    assert "missing-processing" in plan.reasons


def test_changed_pointer_names_the_material_input():
    inputs = {"transcription.json": "reading", "preparation.pointer.txt": None}
    processing = {"format": 1, "stage": "extraction"}
    plan = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs | {"preparation.pointer.txt": "new-pointer"},
        recorded_processing=processing,
        processing=processing,
    )
    assert plan.action == "recompute"
    assert plan.reasons == ("inputs-changed",)
    assert plan.changed_dependencies == ("preparation.pointer.txt",)


def test_unavailable_dependency_blocks_even_a_missing_output_or_refresh():
    plan = plan_view(
        exists=False,
        recorded_inputs=None,
        current_inputs={},
        processing={"format": 1},
        refresh=True,
        blocked_reason="the source dependency could not be inspected",
    )
    assert plan.action == "blocked"
    assert plan.reasons == ("dependency-unavailable",)
    assert plan.changed_dependencies == ()


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "fictional-package"
    for name in (
        {path for paths in PROCESSING_RESOURCES.values() for path in paths}
        | set(planning.MEDIA_READING_RESOURCES)
        | set(planning.PDF_READING_RESOURCES)
        | set(planning.PRESENTATION_READING_RESOURCES)
        | set(planning.SLIDE_VISION_RESOURCES)
        | set(planning.VISION_READING_RESOURCES)
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fictional semantic resource: {name}\n")
    monkeypatch.setattr(planning, "version", lambda name: "fictional-version")
    return root


@pytest.mark.parametrize(
    ("kind", "route", "resource"),
    [
        ("media", "vision", "pipeline/prompts/transcription.md"),
        ("pdf", "paged", "pipeline/pdf_inventory.py"),
        ("pdf", "correction", "pipeline/textlayer.py"),
        ("presentation", "paged", "pipeline/pptx_inventory.py"),
        ("excerpt", "excerpt", "pipeline/container_sources.py"),
        ("excerpt", "excerpt", "pipeline/media_stages.py"),
    ],
)
def test_media_generation_observes_its_reader_resources(
    package, kind, route, resource
):
    def observe():
        return processing_dependencies(
            "transcription",
            package=package,
            source_sha1="1" * 40,
            document_kind=kind,
            configuration={"route": route},
        )

    before = observe()
    path = package / resource
    path.write_text(path.read_text() + "changed fictional reader policy\n")
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=observe(),
    )
    assert plan.changed_dependencies == (f"processing.resources.{resource}",)


@pytest.mark.parametrize(
    "resource",
    [
        "pipeline/slide_reading.py",
        "pipeline/slide_layout.py",
        "pipeline/slide_layout_reading.py",
        "pipeline/prompts/slide_layout.md",
    ],
)
def test_selected_slide_reading_observes_visual_policy_only_when_requested(
    package,
    resource,
):
    def observe(selected):
        configuration = {"route": "paged"}
        if selected:
            configuration["slide_vision"] = {"positions": [2]}
        return processing_dependencies(
            "transcription",
            package=package,
            source_sha1="1" * 40,
            document_kind="presentation",
            configuration=configuration,
        )

    native, visual = observe(False), observe(True)
    path = package / resource
    path.write_text(path.read_text() + "changed fictional slide policy\n")
    assert observe(False) == native
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=visual,
        processing=observe(True),
    )
    assert plan.changed_dependencies == (f"processing.resources.{resource}",)


@pytest.mark.parametrize("kind", ["pdf", "presentation", "media", "excerpt"])
def test_deterministic_reading_does_not_depend_on_the_vision_prompt(
    package, kind
):
    def observe():
        return processing_dependencies(
            "transcription",
            package=package,
            source_sha1="1" * 40,
            document_kind=kind,
            configuration={"route": "correction"},
        )

    before = observe()
    (package / "pipeline/prompts/transcription.md").write_text(
        "different fictional model instructions"
    )
    assert observe() == before


def test_pdf_parser_versions_affect_only_pdf_reading(package, monkeypatch):
    def observe(kind, stage="transcription"):
        return processing_dependencies(
            stage,
            package=package,
            source_sha1="1" * 40,
            document_kind=kind,
            configuration={"route": "paged"},
        )

    before = {kind: observe(kind) for kind in ("pdf", "presentation", "text")}
    extraction = observe("pdf", "extraction")
    monkeypatch.setattr(
        planning,
        "version",
        lambda name: (
            "changed-version"
            if name in {"pypdf", "pypdfium2"}
            else "fictional-version"
        ),
    )
    assert observe("pdf") != before["pdf"]
    assert observe("presentation") == before["presentation"]
    assert observe("text") == before["text"]
    assert observe("pdf", "extraction") == extraction


def test_excerpt_reading_changes_are_generation_inputs(package):
    def observe(reading):
        return processing_dependencies(
            "transcription",
            package=package,
            source_sha1="1" * 40,
            document_kind="excerpt",
            configuration={"route": "excerpt", "reading_sha256": reading},
        )

    before = observe("original-reading")
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=observe("corrected-reading"),
    )
    assert plan.changed_dependencies == (
        "processing.configuration.reading_sha256",
    )


def fingerprint(package, **configuration):
    return processing_dependencies(
        "extraction",
        package=package,
        source_sha1=source_case("naturalisation-1952").sha1,
        configuration={"strategy": "chunked", **configuration},
    )


@pytest.mark.parametrize(
    "resource",
    [
        "pipeline/prompts/entity-manifest.md",
        "model/manifest.yaml",
        "claims.py",
        "source_structure.py",
        "pipeline/source_context.py",
        "pipeline/text_storage.py",
        "pipeline/claim_naming.py",
        "pipeline/evidence_pages.py",
    ],
)
def test_changed_prompt_schema_and_transitive_code_require_reprocessing(
    package, resource
):
    before = fingerprint(package)
    path = package / resource
    path.write_text(path.read_text() + "changed fictional interpretation\n")
    after = fingerprint(package)
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=after,
    )
    assert plan.action == "recompute"
    assert plan.reasons == ("processing-changed",)
    assert plan.changed_dependencies == (f"processing.resources.{resource}",)


def test_documentation_and_another_stages_prompt_do_not_refresh_extraction(
    package,
):
    before = fingerprint(package)
    (package / "README.md").write_text("fictional documentation update")
    (package / "pipeline/prompts/critique.md").write_text(
        "changed critic prompt"
    )
    assert fingerprint(package) == before


# the claim writer, its depicts gate and the place writer: the write path,
# which changes without recomputing the views of the stages before it
WRITE_PATH = ("pipeline/writer.py", "pipeline/review.py", "pipeline/places.py")

# each fingerprint a stage takes: a text document's for every stage, and
# the transcriptions that add the media, PDF, presentation, slide vision
# and vision reader lists
FINGERPRINTS = [
    *(pytest.param(stage, "text", {}, id=stage) for stage in STAGES),
    pytest.param(
        "transcription", "media", {"route": "vision"}, id="media-vision"
    ),
    pytest.param("transcription", "pdf", {"route": "paged"}, id="pdf-paged"),
    pytest.param(
        "transcription",
        "presentation",
        {"route": "paged", "slide_vision": {"positions": [2]}},
        id="presentation-slide-vision",
    ),
]


def stage_fingerprint(
    package, stage, document_kind="text", configuration=None
):
    return processing_dependencies(
        stage,
        package=package,
        source_sha1="1" * 40,
        document_kind=document_kind,
        configuration=configuration or {},
    )


@pytest.mark.parametrize(
    ("stage", "document_kind", "configuration"), FINGERPRINTS
)
def test_write_path_changes_do_not_refresh_a_stage_before_write(
    package, stage, document_kind, configuration
):
    def observed():
        return stage_fingerprint(package, stage, document_kind, configuration)

    for name in WRITE_PATH:
        (package / name).write_text(f"fictional write path: {name}\n")
    before = observed()
    for name in WRITE_PATH:
        path = package / name
        path.write_text(path.read_text() + "changed fictional write path\n")
    assert observed() == before


@pytest.mark.parametrize(
    ("stage", "resource"),
    [
        ("critique", "pipeline/claim_naming.py"),
        ("gedcom", "pipeline/text_storage.py"),
    ],
)
def test_shared_helpers_of_critique_and_gedcom_require_reprocessing(
    package, stage, resource
):
    before = stage_fingerprint(package, stage)
    path = package / resource
    path.write_text(path.read_text() + "changed fictional helper\n")
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=stage_fingerprint(package, stage),
    )
    assert plan.action == "recompute"
    assert plan.changed_dependencies == (f"processing.resources.{resource}",)


def test_source_and_declared_configuration_are_independent_dependencies(
    package,
):
    before = fingerprint(package, effort="low", epoch=0)
    changed_configuration = fingerprint(package, effort="high", epoch=0)
    changed_source = processing_dependencies(
        "extraction",
        package=package,
        source_sha1=source_case("birth-extract-1938").sha1,
        configuration=before["configuration"],
    )
    assert before["resources"] == changed_configuration["resources"]
    assert before["resources"] == changed_source["resources"]
    assert before["source_sha1"] != changed_source["source_sha1"]
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=changed_configuration,
    )
    assert plan.changed_dependencies == ("processing.configuration.effort",)


def test_configuration_order_is_irrelevant_and_later_mutation_is_not_retained(
    package,
):
    configuration = {
        "strategy": "chunked",
        "model": {"name": "scripted", "effort": "low"},
    }
    source = source_case("naturalisation-1952").sha1
    before = processing_dependencies(
        "extraction",
        package=package,
        source_sha1=source,
        configuration=configuration,
    )
    assert before == processing_dependencies(
        "extraction",
        package=package,
        source_sha1=source,
        configuration={
            "model": {"effort": "low", "name": "scripted"},
            "strategy": "chunked",
        },
    )
    configuration["model"]["effort"] = "high"
    assert before["configuration"]["model"]["effort"] == "low"


def test_missing_semantic_resource_is_an_error_not_an_equivalent_fingerprint(
    package,
):
    (package / "claims.py").unlink()
    with pytest.raises(FileNotFoundError):
        fingerprint(package)


def test_installed_semantic_library_changes_are_recorded(package, monkeypatch):
    before = fingerprint(package)

    def upgraded(name):
        if name == "py3langid":
            return "changed-fictional-version"
        if name == "PyYAML":
            raise planning.PackageNotFoundError(name)
        return "fictional-version"

    monkeypatch.setattr(planning, "version", upgraded)
    after = fingerprint(package)
    assert after["libraries"]["PyYAML"] is None
    plan = plan_view(
        exists=True,
        recorded_inputs={},
        current_inputs={},
        recorded_processing=before,
        processing=after,
    )
    assert plan.changed_dependencies == (
        "processing.libraries.PyYAML",
        "processing.libraries.py3langid",
    )
