"""Shared operations preserve source identity and distinguish partial work.

Authored answers exercise real readers, parsers and validation. They establish
operation behavior, not model quality; the client never branches on prompts.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixtures.semantic_acceptance import (
    birth_batch,
    homonym_batches,
    source_case,
)
from yizkr.claims import ArtifactRef, LocalRef
from yizkr.pipeline.extract import ExtractionFailed, LlmClaimExtractor
from yizkr.pipeline.llm import (
    OutputLimitReached,
    PassPaused,
    cached_client,
    observed_client,
)
from yizkr.pipeline.llmcache import EngineCache
from yizkr.pipeline.stages import sha256_file, sha256_text
from yizkr.pipeline.text_stages import (
    CritiqueRequest,
    ExtractionRequest,
    TextReadRequest,
    critique,
    extract,
    read_text,
    validate_paths,
)


class ScriptedClient:
    """A fixed sequence of authored answers, also usable by caller tests."""

    model_identifier = "authored-fictional-answers"

    def __init__(self, *answers, cached_first=False):
        self.answers = iter(answers)
        self.prompts = []
        self.closed = False
        self.cached_first = cached_first

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def reject(self, response, reason):
        cached, self.cached_first = self.cached_first, False
        return cached

    def close(self):
        self.closed = True


def source_transcription():
    return source_case("naturalisation-1952").transcription()


def birth_answer(*, finished=True):
    batch = birth_batch(
        "naturalisation-1952", subject=LocalRef(local_id="gustav")
    )
    answer = batch.claims[0].model_dump_json()
    return answer + ('\n{"done":true}' if finished else "")


def scripted_text_client(*, final=None):
    """One-entity, one-chunk extraction of the fictional certificate.

    A caller may supply an unfinished final answer or an exception. There
    is no prompt-aware interpretation or model-quality claim.
    """
    manifest = json.dumps(
        {
            "entities": [
                {
                    "id": "gustav",
                    "label": "Gustave Lobig",
                    "kind": "human",
                    "mention_lines": [2],
                }
            ],
            "preparation": None,
        }
    )
    return ScriptedClient(manifest, birth_answer() if final is None else final)


def test_reading_keeps_media_identity_separate_from_text_and_correction(
    tmp_path,
):
    case = source_case("naturalisation-1952")
    media = tmp_path / "naturalisation-1952.bin"
    media.write_bytes(b"fictional certificate scan: naturalisation-1952")
    reading = tmp_path / "naturalisation-1952.txt"
    reading.write_text(case.text)
    request = TextReadRequest(media, reading, language="en")

    result = read_text(request)
    assert result.transcription.artifact == ArtifactRef.from_path(media)
    assert result.transcription.artifact != ArtifactRef.from_path(reading)
    assert result.transcription.text == case.text
    assert result.resolution.code == "en"
    assert result.dependencies["text_sha256"] == sha256_text(case.text)
    assert (
        result.dependencies["source_sha1"]
        == result.transcription.artifact.sha1
    )

    corrected = read_text(replace(request, correction=True))
    assert corrected.transcription.engine == "correction"
    assert corrected.transcription.artifact == result.transcription.artifact
    assert corrected.dependencies["configuration"]["correction"] is True


def test_complete_chunked_operation_returns_proposals_and_emitted_products(
    tmp_path,
):
    transcription = source_transcription()
    client = scripted_text_client()
    output = tmp_path / "extraction"
    result = extract(
        ExtractionRequest(
            transcription, transcription.artifact, state_dir=output
        ),
        client,
    )

    assert result.status == "complete"
    batch = result.require_complete()
    (claim,) = batch.claims
    assert (claim.value.year, claim.value.month, claim.value.day) == (
        1919,
        7,
        1,
    )
    assert claim.source.artifact == transcription.artifact
    assert result.diagnostic_batch is None
    assert output / "coverage.json" in result.artifacts
    assert result.dependencies["configuration"]["strategy"] == "chunked"
    assert (
        result.dependencies["model"]["model_identifier"]
        == client.model_identifier
    )
    assert not client.closed


def test_partial_chunk_result_cannot_be_consumed_as_an_accepted_batch(
    tmp_path,
):
    transcription = source_case("family-tree-1984").transcription()
    daughter, _ = homonym_batches(local_ids=("ursula", "widow"))
    manifest = json.dumps(
        {
            "entities": [
                {
                    "id": key,
                    "label": label,
                    "kind": "human",
                    "mention_lines": [4],
                }
                for key, label in (
                    ("ursula", "Ursula Löbig"),
                    ("gustav", "Gustav Löbig"),
                )
            ],
            "preparation": None,
        }
    )
    client = ScriptedClient(
        manifest,
        daughter.claims[0].model_dump_json() + '\n{"done":true}',
        RuntimeError("fictional second chunk unavailable"),
    )
    output = tmp_path / "extraction"
    diagnostic = output / "incomplete.json"
    result = extract(
        ExtractionRequest(
            transcription,
            transcription.artifact,
            state_dir=output,
            diagnostic_dump=diagnostic,
            budget=1,
            retries=0,
        ),
        client,
    )

    assert result.status == "incomplete"
    assert result.batch is None
    assert len(result.diagnostic_batch.claims) == 1
    assert result.failed_chunks == ["c002"]
    with pytest.raises(ExtractionFailed, match="chunks failed"):
        result.require_complete()
    stored = json.loads(diagnostic.read_text())
    assert stored["status"] == "incomplete"
    assert "claims" not in stored
    assert len(stored["diagnostic_batch"]["claims"]) == 1
    assert stored["dependencies"] == result.dependencies
    assert result.artifacts == (diagnostic,)
    assert not (output / "coverage.json").exists()
    assert not client.closed


def test_single_unfinished_answer_is_diagnostic_and_retry_clears_it(tmp_path):
    transcription = source_transcription()
    diagnostic = tmp_path / "incomplete.json"
    request = ExtractionRequest(
        transcription,
        transcription.artifact,
        strategy="single",
        diagnostic_dump=diagnostic,
        raw_dump=tmp_path / "answer.txt",
    )
    incomplete = extract(request, ScriptedClient(birth_answer(finished=False)))
    assert incomplete.batch is None
    assert len(incomplete.diagnostic_batch.claims) == 1
    assert incomplete.incomplete_reasons == [
        "extraction: no terminal sentinel"
    ]
    assert diagnostic in incomplete.artifacts
    with pytest.raises(ExtractionFailed, match="terminal sentinel"):
        incomplete.require_complete()

    complete = extract(request, ScriptedClient(birth_answer()))
    assert len(complete.require_complete().claims) == 1
    assert not diagnostic.exists()
    assert diagnostic not in complete.artifacts


def test_single_retry_reports_only_the_final_answers_completeness():
    transcription = source_transcription()
    client = ScriptedClient(
        birth_answer(finished=False), birth_answer(), cached_first=True
    )
    extractor = LlmClaimExtractor(client)
    assert (
        len(extractor.extract(transcription, transcription.artifact).claims)
        == 1
    )
    assert extractor.incomplete_reasons == []
    assert len(client.prompts) == 2


def test_empty_claim_free_answer_is_complete_with_a_reason(tmp_path):
    transcription = source_transcription()
    result = extract(
        ExtractionRequest(
            transcription, transcription.artifact, state_dir=tmp_path
        ),
        ScriptedClient('{"entities":[],"preparation":null}'),
    )
    assert result.require_complete().claims == []
    assert any("no entity" in reason for reason in result.attention)
    assert result.failed_chunks == []


def test_prepared_claims_need_no_client_and_record_the_supplied_file(tmp_path):
    transcription = source_transcription()
    path = tmp_path / "claims.json"
    path.write_text('{"claims":[]}')
    result = extract(
        ExtractionRequest(
            transcription,
            transcription.artifact,
            strategy="file",
            claims_file=path,
        )
    )
    assert result.require_complete().claims == []
    assert result.attention
    assert result.dependencies["claims_file_sha256"] == sha256_file(path)
    assert "model" not in result.dependencies


def test_unreadable_prepared_claims_keep_the_adapters_failure_classification(
    tmp_path, monkeypatch
):
    from yizkr.pipeline import text_stages

    transcription = source_transcription()
    claims = tmp_path / "claims.json"
    claims.write_text('{"claims":[]}')
    unreadable = PermissionError("prepared claims are unreadable")

    def denied(path):
        assert path == claims
        raise unreadable

    monkeypatch.setattr(text_stages, "sha256_file", denied)
    with pytest.raises(
        ExtractionFailed, match="cannot read the claims file"
    ) as error:
        extract(
            ExtractionRequest(
                transcription,
                transcription.artifact,
                strategy="file",
                claims_file=claims,
            )
        )
    assert error.value.__cause__ is unreadable
    assert claims.read_text() == '{"claims":[]}'


@pytest.mark.parametrize(
    "error", [RuntimeError("failed"), PassPaused(), KeyboardInterrupt()]
)
def test_failure_pause_and_interrupt_propagate_without_taking_client_ownership(
    tmp_path, error
):
    transcription = source_transcription()
    client = ScriptedClient(error)
    diagnostic = tmp_path / "incomplete.json"
    with pytest.raises(type(error)) as caught:
        extract(
            ExtractionRequest(
                transcription,
                transcription.artifact,
                strategy="single",
                diagnostic_dump=diagnostic,
            ),
            client,
        )
    assert caught.value is error
    assert not diagnostic.exists()
    assert not client.closed


def test_source_mismatch_is_refused_before_any_model_call():
    transcription = source_transcription()
    client = scripted_text_client()
    with pytest.raises(ValueError, match="different source artifact"):
        extract(
            ExtractionRequest(
                transcription,
                ArtifactRef.from_bytes(b"another fictional source"),
            ),
            client,
        )
    assert client.prompts == []


@pytest.mark.parametrize("answer", ["", OutputLimitReached()])
def test_an_empty_unfinished_critic_is_advisory_but_never_clean(answer):
    batch = birth_batch("naturalisation-1952")
    client = ScriptedClient(answer)
    result = critique(CritiqueRequest(batch, source_transcription()), client)
    assert result.findings == []
    assert result.status == "incomplete"
    assert result.incomplete_reasons
    assert result.attention == result.incomplete_reasons
    assert not client.closed


def test_critic_retries_cached_answer_and_receives_complete_source():
    client = ScriptedClient("", '{"done":true}', cached_first=True)
    transcription = source_transcription()
    result = critique(
        CritiqueRequest(birth_batch("naturalisation-1952"), transcription),
        client,
    )
    assert result.status == "complete"
    assert result.findings == []
    assert result.incomplete_reasons == []
    assert result.attention == []
    assert source_case("naturalisation-1952").text in client.prompts[-1]


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_diagnostic_path_cannot_delete_a_supplied_claims_file(tmp_path, alias):
    transcription = source_transcription()
    claims = tmp_path / "claims.json"
    claims.write_text('{"claims":[]}')
    diagnostic = claims if alias == "same" else tmp_path / "diagnostic.json"
    if alias == "symlink":
        diagnostic.symlink_to(claims)
    elif alias == "hardlink":
        diagnostic.hardlink_to(claims)
    before = claims.read_bytes()
    with pytest.raises(ValueError, match="overlaps input"):
        extract(
            ExtractionRequest(
                transcription,
                transcription.artifact,
                strategy="file",
                claims_file=claims,
                diagnostic_dump=diagnostic,
            )
        )
    assert claims.read_bytes() == before
    assert diagnostic.exists()


@pytest.mark.parametrize("destination", ["raw", "rejects", "coverage"])
def test_diagnostic_output_collisions_are_refused_before_model_call(
    tmp_path, destination
):
    transcription = source_transcription()
    client = scripted_text_client()
    diagnostic = tmp_path / "diagnostic.json"
    options = {"strategy": "single"}
    if destination == "coverage":
        options = {"strategy": "chunked", "state_dir": tmp_path}
        diagnostic = tmp_path / "coverage.json"
    else:
        options[destination + "_dump"] = diagnostic
    diagnostic.write_text("existing fictional diagnostic")
    with pytest.raises(ValueError, match="outputs .* overlap"):
        extract(
            ExtractionRequest(
                transcription,
                transcription.artifact,
                diagnostic_dump=diagnostic,
                **options,
            ),
            client,
        )
    assert diagnostic.read_text() == "existing fictional diagnostic"
    assert client.prompts == []


def test_input_cannot_be_hidden_in_an_output_workspace(tmp_path):
    workspace = tmp_path / "extraction"
    workspace.mkdir()
    source = workspace / "naturalisation-1952.txt"
    source.write_text(source_case("naturalisation-1952").text)
    alias = tmp_path / "linked-extraction"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="contains input"):
        validate_paths(inputs=(source,), workspaces=(alias,))
    assert source.read_text() == source_case("naturalisation-1952").text


def test_model_fingerprint_survives_observation_and_response_caching(tmp_path):
    transcription = source_transcription()
    engine = ScriptedClient(birth_answer())
    engine.config_hash = sha256_text("fictional scripted configuration")
    client = cached_client(
        observed_client(engine, role="extract", events=lambda *_a, **_k: None),
        EngineCache(tmp_path / "cache"),
        role="extract",
    )
    request = ExtractionRequest(
        transcription, transcription.artifact, strategy="single"
    )
    result = extract(request, client)
    replay = extract(request, client)
    assert result.dependencies["model"]["config_hash"] == engine.config_hash
    assert replay.dependencies["model"] == result.dependencies["model"]
    assert len(engine.prompts) == 1
    assert not engine.closed


def test_uninspectable_output_path_is_refused_before_model_execution(
    tmp_path, monkeypatch
):
    transcription = source_transcription()
    client = ScriptedClient(birth_answer())
    output = tmp_path / "incomplete.json"
    exists = Path.exists

    def uninspectable(path):
        if path == output:
            raise OSError("cannot inspect the diagnostic destination")
        return exists(path)

    monkeypatch.setattr(Path, "exists", uninspectable)
    with pytest.raises(ValueError, match="cannot inspect stage paths"):
        extract(
            ExtractionRequest(
                transcription,
                transcription.artifact,
                strategy="single",
                diagnostic_dump=output,
                raw_dump=tmp_path / "answer.txt",
            ),
            client,
        )
    assert client.prompts == []
    assert not (tmp_path / "answer.txt").exists()
