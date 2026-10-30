"""Context restores readable continuity without becoming another witness.

Authored fictional answers exercise the model boundaries, parsers, source
binding and diagnostics. They do not claim to measure live-model accuracy.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.unit.test_container_claims import paged
from tests.unit.test_text_stages import ScriptedClient
from yizkr.claims import (
    SOURCE_SUPPORT,
    ArtifactRef,
    PlacementSupport,
    SourceSupport,
)
from yizkr.core.verbatim import Transcript
from yizkr.pipeline.chunking import excerpt_audit
from yizkr.pipeline.container_sources import image_source_context
from yizkr.pipeline.extract import ExtractionFailed
from yizkr.pipeline.interpretation_context import (
    ContextGap,
    ContextGroup,
    ContextPassage,
    InterpretationContext,
)
from yizkr.pipeline.source_context import bind_source_support
from yizkr.pipeline.stages import content_hash, sha256_text
from yizkr.pipeline.text_stages import (
    CritiqueRequest,
    ExtractionRequest,
    critique,
    extract,
)
from yizkr.pipeline.transcribe import reading_digest
from yizkr.source_structure import StructuralLocator

PRIMARY = "was born on 1 July 1919."
NEIGHBOUR = "Gustave Lobig, whose father was Sebastian Löbig,"
FOREIGN = "Sebastian Löbig was born in 1862."


def situated_reading(text=PRIMARY):
    container = ArtifactRef.from_bytes(b"fictional family-booklet-2011")
    previous = ArtifactRef.from_bytes(b"fictional first original scan")
    owner = ArtifactRef.from_bytes(b"fictional continuation original scan")
    reading = paged(owner, [(1, [], text)], engine="vision:fictional")

    def support(position, label):
        return SourceSupport(
            container=container,
            part_id=sha256_text(label),
            locator=StructuralLocator(kind="image", position=position),
            placements=[
                PlacementSupport(
                    id=sha256_text(label + " paint"),
                    locator=StructuralLocator(
                        kind="image", position=position, path="paint/1"
                    ),
                )
            ],
        )

    own = support(2, "continuation image")
    prior = support(1, "preceding image")
    reading.source_context = image_source_context(reading, [own])
    context = InterpretationContext(
        target=owner,
        reading_sha256=reading_digest(reading),
        groups=(
            ContextGroup(
                container=container,
                targets=tuple(own.placements),
                passages=(
                    ContextPassage(
                        owner=previous,
                        support=prior,
                        position=1,
                        kind="image",
                        text=NEIGHBOUR + "\n" + FOREIGN,
                        language="en",
                    ),
                ),
            ),
        ),
    )
    return reading, context


def manifest():
    return json.dumps(
        {
            "entities": [
                {
                    "id": "gustav",
                    "label": "Gustave Lobig",
                    "kind": "human",
                    "mention_lines": [2],
                }
            ],
            "preparation": {"statement": None},
        }
    )


def claim(reading, quote=PRIMARY):
    return {
        "subject": {
            "kind": "local",
            "local_id": "gustav",
            "label": "Gustave Lobig",
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": {
            "type": "time",
            "year": 1919,
            "month": 7,
            "day": 1,
            "precision": "day",
            "calendar": "gregorian",
        },
        "source": {
            "artifact": reading.artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"language": "en", "text": quote},
        },
    }


def answer(*claims):
    return "\n".join(json.dumps(c) for c in [*claims, {"done": True}])


class ForkingClient(ScriptedClient):
    def open_conversation(self, prompt):
        return self.complete(prompt), object()

    def fork(self, handle, prompt):
        return self.complete(prompt)

    def close_conversation(self, handle):
        pass


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize("fork", [False, True])
def test_continuation_uses_same_context_for_extract_and_critique(
    tmp_path, strategy, fork
):
    reading, context = situated_reading()
    original = reading.model_dump_json()
    replies = [answer(claim(reading))]
    if strategy == "chunked":
        replies.insert(0, manifest())
    client = (ForkingClient if fork else ScriptedClient)(*replies)
    result = extract(
        ExtractionRequest(
            reading,
            reading.artifact,
            strategy=strategy,
            context=context,
            state_dir=tmp_path,
        ),
        client,
    )
    batch = result.require_complete()
    (accepted,) = batch.claims
    assert accepted.source.artifact == reading.artifact
    assert (
        accepted.source.support == reading.source_context.readings[0].support
    )
    assert accepted.source.transcription_excerpt.text == PRIMARY
    assert reading.model_dump_json() == original
    assert all(context.prompt_block() in prompt for prompt in client.prompts)
    assert result.dependencies[
        "interpretation_context_sha256"
    ] == content_hash(context.model_dump(mode="json"))
    critic = ScriptedClient('{"done":true}')
    reviewed = critique(CritiqueRequest(batch, reading, context), critic)
    assert reviewed.status == "complete"
    assert context.prompt_block() in critic.prompts[0]
    assert (
        reviewed.dependencies["interpretation_context_sha256"]
        == result.dependencies["interpretation_context_sha256"]
    )


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize("quote", [FOREIGN, "words in neither reading", None])
@pytest.mark.parametrize("keep_primary", [False, True])
def test_context_cannot_launder_a_foreign_or_unlocated_claim(
    tmp_path, strategy, quote, keep_primary
):
    reading, context = situated_reading()
    unsupported = claim(reading, quote or PRIMARY)
    if quote is None:
        unsupported["source"].pop("transcription_excerpt")
    claims = ([claim(reading)] if keep_primary else []) + [unsupported]
    replies = [answer(*claims)]
    if strategy == "chunked":
        replies.insert(0, manifest())
    diagnostic = tmp_path / "incomplete.json"
    result = extract(
        ExtractionRequest(
            reading,
            reading.artifact,
            strategy=strategy,
            context=context,
            state_dir=tmp_path,
            diagnostic_dump=diagnostic,
            rejects_dump=tmp_path / "rejects.json",
        ),
        ScriptedClient(*replies),
    )
    assert result.status == "incomplete"
    assert result.batch is None
    assert len(result.diagnostic_batch.claims) == int(keep_primary)
    assert any("primary reading" in why for why in result.incomplete_reasons)
    assert json.loads(diagnostic.read_text())["status"] == "incomplete"
    rejected = json.loads((tmp_path / "rejects.json").read_text())
    assert rejected[0]["reasons"] == [SOURCE_SUPPORT]
    with pytest.raises(ExtractionFailed, match="source attribution"):
        result.require_complete()


def test_file_adapter_refuses_context_only_evidence(tmp_path):
    reading, context = situated_reading()
    good = extract(
        ExtractionRequest(reading, reading.artifact, context=context),
        ScriptedClient(manifest(), answer(claim(reading))),
    ).require_complete()
    path = tmp_path / "manual.json"
    path.write_text(good.model_dump_json())
    request = ExtractionRequest(
        reading,
        reading.artifact,
        strategy="file",
        claims_file=path,
        context=context,
    )
    assert extract(request).require_complete().claims == good.claims
    broken = good.model_dump(mode="json")
    broken["claims"][0]["source"]["transcription_excerpt"]["text"] = FOREIGN
    path.write_text(json.dumps(broken))
    with pytest.raises(ExtractionFailed, match="primary reading"):
        extract(request)


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize("invalid", ["schema", "vocabulary", "blank_quote"])
def test_invalid_proposal_without_quote_does_not_block_valid_context_claims(
    tmp_path, strategy, invalid
):
    reading, context = situated_reading()
    malformed = claim(reading)
    malformed["source"].pop("transcription_excerpt")
    if invalid == "schema":
        malformed.pop("value")
    elif invalid == "vocabulary":
        malformed["property"] = "unsupported_relation"
    else:
        malformed["source"]["transcription_excerpt"] = {
            "language": "en",
            "text": "",
        }
    replies = [answer(claim(reading), malformed)]
    if strategy == "chunked":
        replies.insert(0, manifest())
    result = extract(
        ExtractionRequest(
            reading,
            reading.artifact,
            strategy=strategy,
            context=context,
            state_dir=tmp_path,
            rejects_dump=tmp_path / "rejects.json",
        ),
        ScriptedClient(*replies),
    )
    assert len(result.require_complete().claims) == 1
    assert result.incomplete_reasons == []
    rejected = json.loads((tmp_path / "rejects.json").read_text())
    assert len(rejected) == 1
    assert SOURCE_SUPPORT not in rejected[0]["reasons"]
    if invalid == "vocabulary":
        assert any("unknown property" in p for p in rejected[0]["problems"])


@pytest.mark.parametrize("strategy", ["single", "chunked", "file"])
@pytest.mark.parametrize("quote", [PRIMARY[:-1] + "-", PRIMARY])
def test_literal_primary_quote_keeps_strict_quote_quality_diagnostic(
    tmp_path, strategy, quote
):
    text = PRIMARY[:-1] + "-\n" + PRIMARY
    reading, context = situated_reading(text)
    assert quote in reading.text
    assert not Transcript(reading.text).spans(quote)
    request = ExtractionRequest(
        reading,
        reading.artifact,
        strategy=strategy,
        context=context,
        state_dir=tmp_path,
    )
    if strategy == "file":
        path = tmp_path / "manual.json"
        path.write_text(json.dumps({"claims": [claim(reading, quote)]}))
        request = replace(request, claims_file=path)
        result = extract(request)
    else:
        replies = [answer(claim(reading, quote))]
        if strategy == "chunked":
            replies.insert(0, manifest())
        result = extract(request, ScriptedClient(*replies))
    batch = result.require_complete()
    assert len(batch.claims) == 1
    assert batch.claims[0].source.artifact == reading.artifact
    assert batch.claims[0].source.transcription_excerpt.text == quote
    assert (
        len(excerpt_audit(batch.claims, Transcript(reading.text))["missing"])
        == 1
    )


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_context_evidence_uses_original_quote_before_length_clipping(
    tmp_path, strategy
):
    text = (PRIMARY + " ") * 20
    reading, context = situated_reading(text)
    replies = [answer(claim(reading, text))]
    if strategy == "chunked":
        replies.insert(0, manifest())
    batch = extract(
        ExtractionRequest(
            reading,
            reading.artifact,
            strategy=strategy,
            context=context,
            state_dir=tmp_path,
        ),
        ScriptedClient(*replies),
    ).require_complete()
    assert len(batch.claims) == 1
    assert batch.claims[0].source.transcription_excerpt.text.endswith("…")
    assert (
        len(excerpt_audit(batch.claims, Transcript(reading.text))["missing"])
        == 1
    )


def test_literal_primary_quote_locates_repeated_parts_but_not_mixed_owners():
    text = PRIMARY[:-1] + "-\n" + PRIMARY
    reading, _ = situated_reading(text)
    support = reading.source_context.readings[0].support
    other_part = support.model_copy(update={"part_id": sha256_text("repeat")})
    reading.source_context = image_source_context(
        reading, [support, other_part]
    )
    accepted, rejected = bind_source_support(
        [claim(reading, PRIMARY)], reading, allow_literal_quote=True
    )
    assert not rejected
    assert accepted[0]["source"]["artifact"] == reading.artifact.model_dump(
        mode="json"
    )
    spans = reading.source_context.readings
    reading.source_context.readings = [
        spans[0],
        spans[1].model_copy(
            update={"owner": ArtifactRef.from_bytes(b"other original")}
        ),
    ]
    accepted, rejected = bind_source_support(
        [claim(reading, PRIMARY)], reading, allow_literal_quote=True
    )
    assert not accepted
    assert "ambiguous source ownership" in rejected[0]["problems"][0]


def test_file_adapter_checks_vocabulary_before_context_evidence(tmp_path):
    reading, context = situated_reading()
    malformed = claim(reading)
    malformed["property"] = "unsupported_relation"
    malformed["source"].pop("transcription_excerpt")
    path = tmp_path / "manual.json"
    path.write_text(json.dumps({"claims": [claim(reading), malformed]}))
    with pytest.raises(ExtractionFailed, match="manifest validation"):
        extract(
            ExtractionRequest(
                reading,
                reading.artifact,
                strategy="file",
                claims_file=path,
                context=context,
            )
        )


def test_foreign_appellation_cannot_change_manifest_aliases(tmp_path):
    reading, context = situated_reading()
    appellation = claim(reading, FOREIGN)
    appellation["property"] = "referred_to_as"
    appellation["value"] = {
        "type": "monolingual",
        "value": {"text": "Gus", "language": "en"},
    }
    result = extract(
        ExtractionRequest(
            reading, reading.artifact, context=context, state_dir=tmp_path
        ),
        ScriptedClient(manifest(), answer(claim(reading), appellation)),
    )
    assert result.status == "incomplete"
    assert len(result.diagnostic_batch.claims) == 1
    assert result.diagnostic_batch.entities[0].label == "Gustave Lobig"
    assert "Gus" not in result.diagnostic_batch.entities[0].aliases


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_absent_or_empty_context_preserves_prompts_and_bad_quote_policy(
    strategy,
):
    reading, context = situated_reading()
    empty = context.model_copy(update={"groups": ()})
    prompts = []
    batches = []
    for supplied in (None, empty):
        replies = [answer(claim(reading, FOREIGN))]
        if strategy == "chunked":
            replies.insert(0, manifest())
        client = ScriptedClient(*replies)
        result = extract(
            ExtractionRequest(
                reading, reading.artifact, strategy=strategy, context=supplied
            ),
            client,
        )
        batches.append(result.require_complete())
        prompts.append(client.prompts)
    assert prompts[0] == prompts[1]
    assert len(batches[0].claims) == len(batches[1].claims) == 1


def test_context_reaches_segmentation_and_inline_fallbacks(tmp_path):
    reading, context = situated_reading()
    segmentation = json.dumps(
        {
            "units": [
                {
                    "id": "u1",
                    "label": "continuing entry",
                    "kind": "paragraph",
                    "first_line": 1,
                    "last_line": 2,
                    "context": "",
                }
            ]
        }
    )
    client = ScriptedClient("{}", "{}", segmentation, answer(claim(reading)))
    result = extract(
        ExtractionRequest(
            reading, reading.artifact, context=context, state_dir=tmp_path
        ),
        client,
    )
    assert result.status == "complete"
    assert len(result.require_complete().claims) == 1
    assert len(client.prompts) == 4
    assert all(context.prompt_block() in prompt for prompt in client.prompts)


def test_stale_context_refuses_before_any_model_call():
    reading, context = situated_reading()
    changed = reading.model_copy(update={"text": reading.text + " amended"})
    client = ScriptedClient()
    with pytest.raises(ValueError, match="another reading"):
        extract(
            ExtractionRequest(changed, changed.artifact, context=context),
            client,
        )
    assert client.prompts == []


def test_context_change_alters_prompt_and_dependency_not_primary_source():
    reading, context = situated_reading()
    group = context.groups[0]
    passage = group.passages[0].model_copy(update={"text": NEIGHBOUR})
    changed = context.model_copy(
        update={"groups": (group.model_copy(update={"passages": (passage,)}),)}
    )
    results, prompts = [], []
    for supplied in (context, changed):
        client = ScriptedClient(answer(claim(reading)))
        results.append(
            extract(
                ExtractionRequest(
                    reading,
                    reading.artifact,
                    strategy="single",
                    context=supplied,
                ),
                client,
            )
        )
        prompts.append(client.prompts)
    assert prompts[0] != prompts[1]
    assert (
        results[0].dependencies["transcription_sha256"]
        == results[1].dependencies["transcription_sha256"]
    )
    assert (
        results[0].dependencies["interpretation_context_sha256"]
        != results[1].dependencies["interpretation_context_sha256"]
    )
    assert results[0].batch.claims == results[1].batch.claims


def test_context_cannot_change_primary_owner_without_structural_support(
    tmp_path,
):
    reading, context = situated_reading()
    reading = reading.model_copy(update={"source_context": None})
    foreign = claim(reading)
    foreign["source"]["artifact"] = ArtifactRef.from_bytes(
        b"other"
    ).model_dump(mode="json")
    path = tmp_path / "manual.json"
    path.write_text(json.dumps({"claims": [foreign]}))
    with pytest.raises(ExtractionFailed, match="primary source owner"):
        extract(
            ExtractionRequest(
                reading,
                reading.artifact,
                strategy="file",
                claims_file=path,
                context=context,
            )
        )


@pytest.mark.parametrize("available", [False, True])
def test_missing_context_is_visible_on_extraction_and_critique(available):
    reading, context = situated_reading()
    context = context.model_copy(
        update={
            "groups": context.groups if available else (),
            "gaps": (
                ContextGap(
                    container=context.groups[0].container,
                    position=3,
                    reason="neighbour reading unavailable",
                ),
            ),
        }
    )
    result = extract(
        ExtractionRequest(reading, reading.artifact, context=context),
        ScriptedClient(manifest(), answer(claim(reading))),
    )
    reviewed = critique(
        CritiqueRequest(result.require_complete(), reading, context),
        ScriptedClient('{"done":true}'),
    )
    assert result.status == reviewed.status == "complete"
    assert (
        result.attention
        == reviewed.attention
        == [
            "interpretation context is incomplete: "
            "neighbour reading unavailable"
        ]
    )
