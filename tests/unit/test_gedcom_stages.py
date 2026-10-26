"""Deterministic stage proposals retain the export and its located evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.fixtures.semantic_acceptance import source_case
from yizkr import __version__
from yizkr.claims import ArtifactRef
from yizkr.core.dates import UnsupportedDate
from yizkr.pipeline import gedcom_stages, llm
from yizkr.pipeline.gedcom import ADAPTER_IDENTIFIER, adapt_gedcom
from yizkr.pipeline.gedcom_stages import GedcomRequest, derive_gedcom
from yizkr.pipeline.stages import sha256_text

FIXTURE = Path(__file__).parents[1] / "fixtures" / "example-family.ged"


def contradictory_births() -> str:
    return (
        "0 HEAD\n1 LANG English\n"
        "0 @S1@ SOUR\n"
        f"1 TITL {source_case('birth-extract-1938').title}\n"
        "0 @S2@ SOUR\n"
        f"1 TITL {source_case('naturalisation-1952').title}\n"
        "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n"
        "1 BIRT\n2 DATE 7 JAN 1919\n2 SOUR @S1@\n3 PAGE Nr. 12\n"
        "1 BIRT\n2 DATE 1 JUL 1919\n2 SOUR @S2@\n3 PAGE 1\n"
        "1 EDUC\n0 TRLR\n"
    )


def test_stage_preserves_contradictory_values_and_located_citations(
    tmp_path, monkeypatch
):
    def no_model(*args, **kwargs):
        pytest.fail("deterministic GEDCOM derivation requested a model")

    monkeypatch.setattr(llm, "make_client", no_model)
    source = tmp_path / "gustav.ged"
    contents = contradictory_births().replace("\n", "\r\n").encode()
    source.write_bytes(contents)
    artifact = ArtifactRef.from_bytes(contents)

    result = derive_gedcom(GedcomRequest(source, artifact))

    assert result.status == "complete"
    batch = result.require_complete()
    births = [
        claim for claim in batch.claims if claim.property == "date_of_birth"
    ]
    assert [
        (claim.value.year, claim.value.month, claim.value.day)
        for claim in births
    ] == [(1919, 1, 7), (1919, 7, 1)]
    assert [claim.source.page_number for claim in births] == ["Nr. 12", "1"]
    for claim, source_key, date, pointer in zip(
        births,
        ("birth-extract-1938", "naturalisation-1952"),
        ("7 JAN 1919", "1 JUL 1919"),
        ("@S1@", "@S2@"),
        strict=True,
    ):
        excerpt = claim.source.transcription_excerpt
        assert excerpt is not None
        assert f"2 DATE {date}" in excerpt.text
        assert f"2 SOUR {pointer}" in excerpt.text
        assert source_case(source_key).title in excerpt.text
    assert all(claim.source.artifact == artifact for claim in batch.claims)
    assert batch.extraction_run.model_identifier == ADAPTER_IDENTIFIER
    assert batch.extraction_run.pipeline_version == __version__
    assert batch.extraction_run.started_at.tzinfo is not None
    assert result.unsupported_tags == {"EDUC": 1}
    assert result.warnings == []
    assert result.artifacts == ()
    assert (
        result.dependencies["source_sha1"]
        == hashlib.sha1(contents).hexdigest()
    )
    assert result.dependencies["text_sha256"] == sha256_text(contents.decode())
    assert result.dependencies["configuration"] == {
        "adapter": ADAPTER_IDENTIFIER,
        "encoding": "utf-8",
    }
    assert list(tmp_path.iterdir()) == [source]


def test_stage_preserves_adapter_semantics_and_is_independent_of_filename(
    tmp_path,
):
    source = tmp_path / "family.ged"
    contents = FIXTURE.read_bytes()
    source.write_bytes(contents)
    first = derive_gedcom(GedcomRequest(source))
    adapter = adapt_gedcom(contents.decode(), ArtifactRef.from_bytes(contents))
    copy = tmp_path / "renamed-export.ged"
    copy.write_bytes(contents)
    second = derive_gedcom(GedcomRequest(copy))

    for result in (first, second):
        assert result.batch.claims == adapter.batch.claims
        assert result.batch.entities == adapter.batch.entities
        assert result.warnings == adapter.warnings
        assert result.unsupported_tags == adapter.unsupported_tags
        assert result.outcomes == adapter.outcomes
        assert (
            result.batch.extraction_run.model_identifier == ADAPTER_IDENTIFIER
        )
    assert first.dependencies == second.dependencies


def test_hash_and_proposals_use_the_same_source_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "gustav.ged"
    before = contradictory_births().encode()
    source.write_bytes(before)
    read_bytes = Path.read_bytes

    def replaced_after_read(path):
        contents = read_bytes(path)
        if path == source:
            source.write_bytes(before.replace(b"7 JAN 1919", b"1 JUL 1919"))
        return contents

    monkeypatch.setattr(Path, "read_bytes", replaced_after_read)
    result = derive_gedcom(GedcomRequest(source))

    births = [
        claim
        for claim in result.batch.claims
        if claim.property == "date_of_birth"
    ]
    assert {(claim.value.month, claim.value.day) for claim in births} == {
        (1, 7),
        (7, 1),
    }
    assert all(
        claim.source.artifact == ArtifactRef.from_bytes(before)
        for claim in result.batch.claims
    )
    assert result.dependencies["text_sha256"] == sha256_text(before.decode())
    assert (
        result.dependencies["source_sha1"] == hashlib.sha1(before).hexdigest()
    )


def test_changed_source_is_refused_before_adapter_execution(
    tmp_path, monkeypatch
):
    source = tmp_path / "family.ged"
    source.write_bytes(FIXTURE.read_bytes())
    expected = ArtifactRef.from_path(source)
    source.write_bytes(contradictory_births().encode())

    def no_adaptation(*args, **kwargs):
        pytest.fail("changed source reached the GEDCOM adapter")

    monkeypatch.setattr(gedcom_stages, "adapt_gedcom", no_adaptation)
    with pytest.raises(ValueError, match="no longer matches"):
        derive_gedcom(GedcomRequest(source, expected))


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        (b"0 HEAD\nnot a GEDCOM line\n", ValueError),
        (
            "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n1 BIRT\n"
            "2 DATE @#DJULIAN@ 7 JAN 1919\n0 TRLR\n".encode(),
            UnsupportedDate,
        ),
        (b"0 HEAD\n\xff\n", UnicodeDecodeError),
    ],
)
def test_invalid_sources_produce_no_accepted_result(tmp_path, contents, error):
    source = tmp_path / "invalid.ged"
    source.write_bytes(contents)
    with pytest.raises(error):
        derive_gedcom(GedcomRequest(source))
    assert source.read_bytes() == contents
    assert list(tmp_path.iterdir()) == [source]
