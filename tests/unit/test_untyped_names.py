"""Name forms of no stated type (ADR-0011 as amended by #435): the
manifest's `name` property, the extraction prompt's rule, validation, the
privacy check's person evidence, and the run report's count by source
kind. The GEDCOM adapter's side is tests/unit/test_gedcom_untyped_names.py.

Every name is from the example universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import pytest

import tests.unit.test_run_report as run_report
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.core.privacy import PERSON_SUBJECT_PROPERTIES
from yizkr.model.schema import load_manifest
from yizkr.pipeline.chunking import CHUNK_PROMPT
from yizkr.pipeline.extract import (
    salvage_valid_claims,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.report import OTHER, build, render_markdown

RUN = {
    "model_identifier": "claude-code/2.1.237:claude-sonnet-5",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}
SHA1 = "0" * 40


def test_the_manifest_has_a_generic_name_property():
    spec = load_manifest().properties["name"]
    assert spec.label == "name"
    assert spec.datatype == "monolingualtext"
    assert spec.wikidata == "P2561"
    assert not spec.reference
    # what the model and a curator read: of no stated type, verbatim,
    # never the label and never a typed form
    description = " ".join(spec.description.split())
    for words in (
        "without stating its type",
        "as the source writes it",
        "never the person's label",
        "never a form the source types",
    ):
        assert words in description


def test_the_extraction_prompt_offers_it():
    properties, _items = vocabulary_prompt_lines()
    assert any(
        line.lstrip("- ").startswith("name:")
        for line in properties.splitlines()
    )
    # the rule: a distinct name of open kind beside the label's; never a
    # short form or an unmarked nickname use, the label's own form or a
    # form the model types
    prompt = " ".join(CHUNK_PROMPT.split())
    for words in (
        "A distinct name the source gives a person beside the label's, "
        "whose kind the source leaves open",
        "is given as `name`, verbatim (ADR-0011), and is an alias too",
        "A shortening, an initial, a spelling variant or a part of the "
        "label is an alias alone, never `name`",
        "a nickname the source uses without marking it follows the "
        "nickname and referred_to_as rules below, never `name`",
        "Never give the label's own form as `name`",
        "and never a form you type",
    ):
        assert words in prompt, words


def ida_named(text: str, kind: str = "human") -> ClaimBatch:
    """Ida's batch, her bracketed name given as `name`."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                {
                    "id": "e1",
                    "label": "Ida Sörne",
                    "aliases": ["Peppi"],
                    "kind": kind,
                }
            ],
            "claims": [
                {
                    "subject": {"kind": "local", "local_id": "e1"},
                    "property": "name",
                    "value": {
                        "type": "monolingual",
                        "value": {"text": text, "language": "de"},
                    },
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": SHA1},
                        "transcription_excerpt": {
                            "text": "Ida (Peppi) Sörne",
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )


def test_validation_accepts_it_for_a_person():
    assert validate_against_manifest(ida_named("Peppi")) == []


@pytest.mark.parametrize("kind", ["place", "organization"])
def test_validation_refuses_it_for_anything_but_a_person(kind):
    (problem,) = validate_against_manifest(ida_named("Peppi", kind))
    assert "property 'name' is a person's" in problem


@pytest.mark.parametrize(
    "text", ["Ida Sörne", "IDA SÖRNE", "Ida  Sörne", "Ida So\u0308rne"]
)
def test_validation_refuses_the_subjects_label(text):
    (problem,) = validate_against_manifest(ida_named(text))
    assert "never restates its subject's label" in problem


def test_validation_keeps_a_spelling_variant_of_the_label():
    assert validate_against_manifest(ida_named("Ida Sorne")) == []


def test_salvage_counts_a_refused_name_as_a_reject():
    batch = ida_named("Ida Sörne").model_dump(mode="json")
    kept, rejects = salvage_valid_claims(
        batch["claims"], RUN, entities=batch["entities"]
    )
    assert kept.claims == []
    assert len(rejects) == 1


def test_it_is_person_evidence_as_a_nickname_is():
    assert "name" in PERSON_SUBJECT_PROPERTIES
    assert "nickname" in PERSON_SUBJECT_PROPERTIES


@pytest.fixture
def named_run(tmp_path, monkeypatch):
    """The report's fictional run, its marriage certificate giving one
    `name` claim and its GEDCOM export two."""
    original = run_report.views

    def views():
        found = original()
        for sha1, count in ((run_report.CERT, 1), (run_report.TREE, 2)):
            batch = found[sha1]["batch.json"]
            batch["claims"] = [
                *batch["claims"],
                *(
                    run_report.claim("e1", "name", "Hilde", sha1)
                    for _ in range(count)
                ),
            ]
        return found

    monkeypatch.setattr(run_report, "views", views)
    return run_report.record(tmp_path / run_report.RUN_ID)


def test_the_report_counts_them_by_source_kind(named_run):
    made = build(named_run, now=run_report.NOW)
    claims = made["aggregate"]["content"]["claims"]
    kinds = {
        run_report.FILES[sha1][1]
        for sha1 in (run_report.CERT, run_report.TREE)
    }
    assert set(claims["untyped_names_by_kind"]) == kinds
    assert claims["untyped_names_by_kind"] == {"gedcom": 2, "media": 1}
    assert claims["by_property"]["name"] == 3
    assert (
        "Name forms of no stated type (`name`) by source kind: gedcom 2, "
        "media 1." in render_markdown(made["aggregate"])
    )


def test_the_report_folds_an_unknown_kind(named_run, tmp_path):
    plan = run_report.manifest()
    for document in plan["documents"]:
        if document["sha1"] == run_report.TREE:
            document["kind"] = "hologram"
    path = run_report.record(tmp_path / "unknown-kind", plan=plan)
    made = build(path, now=run_report.NOW)
    claims = made["aggregate"]["content"]["claims"]
    assert claims["untyped_names_by_kind"] == {OTHER: 2, "media": 1}


def test_the_report_counts_none_where_no_batch_gives_one(tmp_path):
    made = build(
        run_report.record(tmp_path / run_report.RUN_ID), now=run_report.NOW
    )
    claims = made["aggregate"]["content"]["claims"]
    assert claims["untyped_names_by_kind"] == {}
    assert (
        "Name forms of no stated type (`name`) by source kind: none."
        in render_markdown(made["aggregate"])
    )
