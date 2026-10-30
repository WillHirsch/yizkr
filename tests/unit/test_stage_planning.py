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
    plan = plan_view(
        exists=exists,
        recorded_inputs={},
        current_inputs={},
        refresh=refresh,
    )
    assert plan.action == "recompute"
    assert plan.reasons == (reason,)


def test_legacy_processing_metadata_recomputes_only_migrated_stages():
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

    compatible = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs,
    )
    assert compatible.action == "reuse"


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
    plan = plan_view(
        exists=True,
        recorded_inputs=inputs,
        current_inputs=inputs | {"preparation.pointer.txt": "new-pointer"},
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
    for name in {
        path for paths in PROCESSING_RESOURCES.values() for path in paths
    }:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fictional semantic resource: {name}\n")
    monkeypatch.setattr(planning, "version", lambda name: "fictional-version")
    return root


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
        "pipeline/writer.py",
        "pipeline/places.py",
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
