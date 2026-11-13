"""A neighbouring source never supplies evidence for a corrected claim."""

import copy
import json
from dataclasses import replace

import pytest

from tests.unit.test_interpretation_consumers import (
    FOREIGN,
    NEIGHBOUR,
    PRIMARY,
    ForkingClient,
    answer,
    claim,
    manifest,
    situated_reading,
)
from tests.unit.test_text_stages import ScriptedClient
from yizkr.claims import ArtifactRef
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
)
from yizkr.pipeline.extract import ExtractionFailed, correction_dump_path
from yizkr.pipeline.llm import (
    REFUSAL_STOP,
    ClassifierRefused,
    cached_client,
    open_cache,
    reject_response,
)
from yizkr.pipeline.source_context import PRIMARY_QUOTE_MISSING
from yizkr.pipeline.stages import sha256_text
from yizkr.pipeline.text_stages import ExtractionRequest, extract


def request(tmp_path, strategy, reading=None, context=None):
    if reading is None:
        reading, context = situated_reading()
    return ExtractionRequest(
        reading,
        reading.artifact,
        strategy=strategy,
        context=context,
        state_dir=tmp_path,
        raw_dump=tmp_path / "raw.txt",
        rejects_dump=tmp_path / "rejects.json",
        diagnostic_dump=tmp_path / "incomplete.json",
        concurrency=1,
    )


def history(req):
    path = (
        req.state_dir / "corrections.json"
        if req.strategy == "chunked"
        else correction_dump_path(req.raw_dump)
    )
    return path, json.loads(path.read_text())


def scripted(strategy, *responses, fork=False):
    client = ForkingClient if fork else ScriptedClient
    engine = client(
        *([manifest()] if strategy == "chunked" else []), *responses
    )
    engine.config_hash = sha256_text("fictional quotation-recovery client")
    return engine


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("omit", [False, True])
def test_one_correction_uses_only_primary_evidence_and_keeps_history(
    tmp_path, strategy, fork, omit
):
    req = request(tmp_path, strategy)
    bad = answer(claim(req.transcription, FOREIGN))
    good = (
        '{"claims": []}\n{"done": true}'
        if omit
        else answer(claim(req.transcription))
    )
    client = scripted(strategy, bad, good, fork=fork)
    result = extract(req, client)
    batch = result.require_complete()
    assert len(batch.claims) == (0 if omit else 1)
    if not omit:
        assert batch.claims[0].source.transcription_excerpt.text == PRIMARY
        assert batch.claims[0].source.artifact == req.artifact
        assert batch.claims[0].source.support is not None
    assert len(client.prompts) == (3 if strategy == "chunked" else 2)
    assert client.prompts[-1].startswith(client.prompts[-2])
    assert bad in client.prompts[-1]
    assert (
        "Omit claims that the primary reading does not support"
        in client.prompts[-1]
    )
    path, (record,) = history(req)
    assert path in result.artifacts
    assert record["original"] == bad
    assert record["responses"] == [good]
    assert record["rejects"][0]["problems"] == [PRIMARY_QUOTE_MISSING]
    assert record["rejects_after"] == []
    assert not req.diagnostic_dump.exists()


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize(
    "failure",
    [
        "foreign",
        "malformed",
        "schema",
        "unread",
        "unfinished",
        "outage",
        "refused",
    ],
)
def test_failed_correction_stays_incomplete_without_a_third_attempt(
    tmp_path, strategy, failure
):
    req = request(tmp_path, strategy)
    bad = answer(claim(req.transcription), claim(req.transcription, FOREIGN))
    invalid = claim(req.transcription, FOREIGN)
    invalid.pop("value")
    unread = claim(req.transcription, FOREIGN)
    unread.pop("property")
    second = {
        "foreign": bad,
        "malformed": "not claim JSON",
        "schema": answer(claim(req.transcription), invalid),
        "unread": answer(claim(req.transcription), unread),
        "unfinished": json.dumps(claim(req.transcription)),
        "outage": OSError("fictional provider unavailable"),
        "refused": ClassifierRefused(REFUSAL_STOP, "fictional"),
    }[failure]
    client = scripted(strategy, bad, second)
    result = extract(req, client)
    assert result.status == "incomplete"
    assert result.batch is None
    assert len(result.diagnostic_batch.claims) == 1
    with pytest.raises(ExtractionFailed, match="primary reading"):
        result.require_complete()
    assert len(client.prompts) == (3 if strategy == "chunked" else 2)
    _, (record,) = history(req)
    assert record["original"] == bad
    if isinstance(second, str):
        assert record["responses"] == [second]
    else:
        assert record["responses"] == []
        assert str(second) in record["error"]


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize("empty", [None, False, 0, ""])
def test_malformed_empty_correction_cannot_withdraw_unsupported_claims(
    tmp_path, strategy, empty
):
    req = request(tmp_path, strategy)
    bad = answer(claim(req.transcription, FOREIGN))
    malformed = json.dumps({"claims": empty}) + '\n{"done":true}'
    client = scripted(strategy, bad, malformed)
    result = extract(req, client)
    assert result.status == "incomplete"
    assert len(client.prompts) == (3 if strategy == "chunked" else 2)
    _, (record,) = history(req)
    assert record["responses"] == [malformed]
    assert record["unread_after"]


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_cancellation_during_correction_propagates_with_original_evidence(
    tmp_path, strategy
):
    req = request(tmp_path, strategy)
    bad = answer(claim(req.transcription, FOREIGN))
    client = scripted(strategy, bad, RunCancelled("fictional cancellation"))
    with pytest.raises(RunCancelled):
        extract(req, client)
    _, (record,) = history(req)
    assert record["original"] == bad
    assert record["responses"] == []


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_returned_correction_is_kept_before_cancellation_stops_publication(
    tmp_path, strategy
):
    req = request(tmp_path, strategy)
    bad = answer(claim(req.transcription, FOREIGN))
    good = answer(claim(req.transcription))
    cancellation = Cancellation()

    class CancelAfterAnswer(ScriptedClient):
        def complete(self, prompt, image_paths=()):
            response = super().complete(prompt, image_paths)
            if "Previous answer to correct" in prompt:
                cancellation.cancel("fictional completed-call interruption")
            return response

    client = CancelAfterAnswer(
        *([manifest()] if strategy == "chunked" else []), bad, good
    )
    with cancellation_scope(cancellation), pytest.raises(RunCancelled):
        extract(req, client)
    _, (record,) = history(req)
    assert record["original"] == bad
    assert record["responses"] == [good]


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize(
    "defect", ["unsupported", "stale", "wrong-owner", "ambiguous"]
)
def test_structural_attribution_failure_never_requests_quote_correction(
    tmp_path, strategy, defect
):
    req = request(tmp_path, strategy)
    reading = req.transcription
    span = reading.source_context.readings[0]
    if defect == "unsupported":
        span.state = "unsupported"
        span.reason = "fictional composite remains unresolved"
    elif defect == "stale":
        span.support.reading_sha256 = sha256_text("another reading")
    elif defect == "wrong-owner":
        span.owner = ArtifactRef.from_bytes(b"fictional other original")
    else:
        other = span.model_copy(deep=True)
        other.owner = ArtifactRef.from_bytes(b"fictional other original")
        reading.source_context.readings.append(other)
    client = scripted(strategy, answer(claim(reading, FOREIGN)))
    result = extract(req, client)
    assert result.status == "incomplete"
    assert len(client.prompts) == (2 if strategy == "chunked" else 1)
    path = (
        req.state_dir / "corrections.json"
        if strategy == "chunked"
        else correction_dump_path(req.raw_dump)
    )
    assert not path.exists()


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_correction_replays_both_cached_answers_without_a_new_provider_call(
    tmp_path, strategy
):
    req = request(tmp_path / "first", strategy)
    bad = answer(claim(req.transcription, FOREIGN))
    good = answer(claim(req.transcription))
    engine = scripted(strategy, bad, good)
    cache = open_cache(tmp_path / "cache")
    try:
        first = extract(req, cached_client(engine, cache, role="extract"))
        assert first.status == "complete"
        prompts = list(engine.prompts)
        assert len(prompts) == (3 if strategy == "chunked" else 2)
        second_req = request(tmp_path / "second", strategy)
        forbidden = scripted("single")
        second = extract(
            second_req, cached_client(forbidden, cache, role="extract")
        )
        assert (
            second.require_complete().claims == first.require_complete().claims
        )
        assert forbidden.prompts == []
        assert history(second_req)[1] == history(req)[1]
        assert len(list((tmp_path / "cache").glob("*/*.json"))) == len(prompts)
    finally:
        cache.close()


def test_changed_first_answer_cannot_reuse_another_answers_correction(
    tmp_path,
):
    req = request(tmp_path / "first", "single")
    first_bad = answer(claim(req.transcription, FOREIGN))
    second_bad = answer(claim(req.transcription, NEIGHBOUR))
    good = answer(claim(req.transcription))
    cache = open_cache(tmp_path / "cache")
    try:
        engine = scripted("single", first_bad, good)
        client = cached_client(engine, cache, role="extract")
        assert extract(req, client).status == "complete"
        initial_prompt = engine.prompts[0]
        kept = client.complete(initial_prompt)
        assert kept == first_bad
        assert reject_response(client, kept, "fictional changed answer")
        replacement = scripted("single", second_bad, good)
        result = extract(
            request(tmp_path / "second", "single"),
            cached_client(replacement, cache, role="extract"),
        )
        assert result.status == "complete"
        assert len(replacement.prompts) == 2
        assert replacement.prompts[0] == initial_prompt
        assert replacement.prompts[1] != engine.prompts[1]
    finally:
        cache.close()


def two_chunks(tmp_path):
    reading, context = situated_reading(PRIMARY + "\n" + FOREIGN)
    req = replace(request(tmp_path, "chunked", reading, context), budget=1)
    entities = json.loads(manifest())
    entities["entities"].append(
        {
            "id": "sebastian",
            "label": "Sebastian Löbig",
            "kind": "human",
            "mention_lines": [3],
        }
    )
    second = copy.deepcopy(claim(reading, FOREIGN))
    second["subject"].update(local_id="sebastian", label="Sebastian Löbig")
    second["value"] = {
        "type": "time",
        "year": 1862,
        "precision": "year",
        "calendar": "gregorian",
    }
    return req, json.dumps(entities), second


def test_successful_correction_and_unaffected_chunk_both_replay(tmp_path):
    req, opening, unaffected = two_chunks(tmp_path / "first")
    bad = answer(claim(req.transcription, NEIGHBOUR))
    engine = scripted(
        "single",
        opening,
        bad,
        answer(claim(req.transcription)),
        answer(unaffected),
    )
    cache = open_cache(tmp_path / "cache")
    try:
        first = extract(req, cached_client(engine, cache, role="extract"))
        assert len(engine.prompts) == 4
        assert first.status == "complete"
        forbidden = scripted("single")
        second = extract(
            replace(req, state_dir=tmp_path / "second"),
            cached_client(forbidden, cache, role="extract"),
        )
        assert (
            second.require_complete().claims == first.require_complete().claims
        )
        assert forbidden.prompts == []
    finally:
        cache.close()


@pytest.mark.parametrize("malformed", [None, "wrapped", "transform"])
def test_only_affected_chunk_is_corrected_and_unread_replacement_refused(
    tmp_path, malformed
):
    req, opening, unaffected = two_chunks(tmp_path)
    bad = answer(claim(req.transcription, NEIGHBOUR))
    corrected = answer(claim(req.transcription))
    if malformed == "wrapped":
        corrected = (
            json.dumps({"claims": [claim(req.transcription)]})
            + '\n{"done":true}'
        )
    elif malformed == "transform":
        invalid = claim(req.transcription)
        invalid["subject"]["local_id"] = {}
        corrected = answer(claim(req.transcription), invalid)
    engine = ForkingClient(opening, bad, corrected, answer(unaffected))
    result = extract(req, engine)
    assert len(engine.prompts) == 4
    assert "Previous answer to correct" not in engine.prompts[-1]
    _, (record,) = history(req)
    assert record["chunk"] == "c001"
    assert record["responses"] == [corrected]
    if malformed:
        assert result.status == "incomplete"
        assert len(result.diagnostic_batch.claims) == 1
        assert (
            result.diagnostic_batch.claims[0].subject.local_id == "sebastian"
        )
        assert record.get("unread_after") or record.get("rejects_after")
    else:
        assert {
            c.subject.local_id for c in result.require_complete().claims
        } == {"gustav", "sebastian"}


def test_corrective_attempt_stays_inline_after_initial_fork_failure(tmp_path):
    req = request(tmp_path, "chunked")

    class FailedFork(ForkingClient):
        forks = 0

        def fork(self, handle, prompt):
            self.forks += 1
            raise OSError("fictional fork unavailable")

    engine = FailedFork(
        manifest(),
        answer(claim(req.transcription, FOREIGN)),
        answer(claim(req.transcription)),
    )
    assert extract(req, engine).status == "complete"
    assert engine.forks == 1
    assert len(engine.prompts) == 3
