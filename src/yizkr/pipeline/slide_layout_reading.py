"""Read mixed-slide layout while retaining native text as its only wording."""

from __future__ import annotations

import json
from pathlib import Path

from yizkr.claims import ArtifactRef
from yizkr.pipeline.language import resolve_language
from yizkr.pipeline.llm import at_output_limit
from yizkr.pipeline.prompts import load_prompt
from yizkr.pipeline.slide_layout import (
    NativeSlideBlocks,
    NativeTextBlock,
    assemble_native_layout,
    parse_native_layout,
)
from yizkr.pipeline.transcribe import (
    LlmVisionTranscriber,
    Transcription,
    UnreadableAnswer,
)
from yizkr.pipeline.transcription_check import check_transcription
from yizkr.pipeline.transcription_record import (
    BLANK,
    TRANSCRIBED,
    UNCLASSIFIED,
    RecordPage,
    TranscriptionRecord,
)

NATIVE_LAYOUT_PROMPT = load_prompt("slide_layout.md")


def native_layout_stand_in(prompt: str) -> str | None:
    """A valid unknown-response placeholder for read-only cost estimation.

    It preserves XML order and claims no observed visual interpretation.
    Only this exact prompt schema is recognized; callers never cache it.
    """
    before, after = NATIVE_LAYOUT_PROMPT.split("{blocks}")
    before, after = before.format(), after.format()
    if not prompt.startswith(before) or not prompt.endswith(after):
        return None
    raw = prompt[len(before) : len(prompt) - len(after) if after else None]
    try:
        value = json.loads(raw)
        if not isinstance(value, list):
            return None
        blocks = [NativeTextBlock.model_validate(block) for block in value]
    except ValueError:
        return None
    if [block.id for block in blocks] != [
        f"b{index}" for index in range(1, len(blocks) + 1)
    ]:
        return None
    return json.dumps({"rows": [[block.id] for block in blocks]})


class NativeLayoutTranscriber(LlmVisionTranscriber):
    """Use the checked model/retry boundary for a constrained layout answer.

    The model supplies a permutation and grouping, never source wording.
    Original image evidence stays in its independently owned reading.
    """

    def __init__(self, client, blocks: NativeSlideBlocks, **kwargs):
        super().__init__(client, **kwargs)
        self.blocks = blocks

    def _prompt_templates(self):
        return {
            "pipeline.slide_layout_reading.NATIVE_LAYOUT_PROMPT": (
                NATIVE_LAYOUT_PROMPT
            )
        }

    def transcribe_unchecked(self, media_path: Path) -> Transcription:
        self._response = None
        if self.before_read is not None:
            self.before_read(media_path)
        artifact = ArtifactRef.from_path(media_path)
        prompt = NATIVE_LAYOUT_PROMPT.format(
            blocks=json.dumps(
                [
                    block.model_dump(mode="json")
                    for block in self.blocks.blocks
                ],
                ensure_ascii=False,
            )
        )
        response = self.client.complete(prompt, [media_path])
        self._response = response
        if at_output_limit(self.client, response):
            raise UnreadableAnswer(
                check_transcription("", cut_off=True), response
            )
        try:
            layout = parse_native_layout(response, self.blocks)
            text = assemble_native_layout(self.blocks, layout)
        except ValueError as error:
            check = check_transcription(response, expect_record=True)
            check = check.model_copy(
                update={
                    "reason": "mixed-slide layout must arrange every verified "
                    "native text block exactly once without adding source text"
                }
            )
            raise UnreadableAnswer(check, response) from error
        self.last_resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        record = TranscriptionRecord(
            document_class=UNCLASSIFIED,
            pages=[
                RecordPage(
                    page=1,
                    status=TRANSCRIBED if text else BLANK,
                    text=text,
                )
            ],
        )
        made_by = getattr(response, "made_by", None)
        producer = made_by or self.client.model_identifier
        reading = Transcription.from_record(
            record,
            artifact=artifact,
            language=self.last_resolution.code,
            engine=f"llm-slide-layout:{producer}",
        )
        reading.pages[0].provenance.native_layout = layout
        return reading
