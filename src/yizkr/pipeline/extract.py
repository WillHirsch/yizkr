"""Claim extraction step: transcription -> ClaimBatch JSON.

`LlmClaimExtractor` prompts an LLM with the claim JSON Schema and the
transcription; the response is parsed and validated against the schema
AND the model manifest before it goes anywhere. Anything that fails
validation fails the step — malformed machine output must never limp
toward the graph.

`ClaimFileExtractor` passes a prepared claim file through the same
validation, so hand-written batches and future extraction tools get
identical treatment.

Every batch records extraction-run metadata (ADR-0003). Subjects the
model proposes are `local` refs; resolution to existing items happens
in the resolve step, never inside extraction.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yizkr
from yizkr.claims import (
    ClaimBatch,
    ExtractionRunMetadata,
    validate_against_manifest,
)
from yizkr.pipeline.llm import LlmClient
from yizkr.pipeline.transcribe import Transcription

EXTRACTION_PROMPT = """\
You extract source-attributed genealogical claims from transcriptions of
historical documents.

Rules — these mirror the archive's non-negotiable conventions:
- Record exactly what the source asserts. Never round dates, never guess,
  never narrow a vague statement. Use precision (year/month/day/decade/
  century), earliest_date/latest_date qualifiers for ranges, and the
  sourcing_circumstances qualifier with the "circa" vocabulary item for
  approximate values.
- Every claim MUST include the source reference: artifact QID {artifact_qid},
  plus page number and a short verbatim transcription excerpt supporting it.
- Refer to people, places, and events the document mentions with "local"
  references (stable local_id per entity within this batch, label as
  written, instance_of from: human, place, event, birth, death, marriage,
  migration, military_service).
- Use only property keys and vocabulary item keys that appear in the JSON
  Schema description below; values must match the property's datatype.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

Output ONLY a JSON object with a "claims" array (no prose, no code fences),
where each claim matches the "Claim" definition of this JSON Schema:

{schema}

Transcription of artifact {artifact_qid} (language: {language}):

{text}
"""


def run_metadata(model_identifier: str) -> ExtractionRunMetadata:
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=Path(__file__).parent,
            ).stdout.strip()
            or None
        )
    except OSError:
        commit = None
    return ExtractionRunMetadata(
        model_identifier=model_identifier,
        pipeline_version=yizkr.__version__,
        software_commit=commit,
        started_at=datetime.now(UTC),
    )


def _parse_response(response: str) -> dict:
    """Tolerate code fences and stray prose around the JSON object."""
    text = response.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"no JSON object in extractor response: {text[:200]!r}")
        text = text[start : end + 1]
    return json.loads(text)


class LlmClaimExtractor:
    def __init__(self, client: LlmClient) -> None:
        self.client = client

    def extract(self, transcription: Transcription, artifact_qid: str) -> ClaimBatch:
        schema = json.dumps(ClaimBatch.model_json_schema(), indent=2)
        prompt = EXTRACTION_PROMPT.format(
            schema=schema,
            artifact_qid=artifact_qid,
            language=transcription.language,
            text=transcription.text,
        )
        response = self.client.complete(prompt)
        payload = _parse_response(response)
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": run_metadata(self.client.model_identifier).model_dump(
                    mode="json"
                ),
                "claims": payload.get("claims", []),
            }
        )
        problems = validate_against_manifest(batch)
        if problems:
            raise ValueError(
                "extractor output failed manifest validation:\n  "
                + "\n  ".join(problems)
            )
        return batch


class ClaimFileExtractor:
    """Validate a prepared claims file into a batch (manual/offline flows).

    The file holds either a full ClaimBatch or a bare {"claims": [...]}
    object; in the latter case run metadata is generated here.
    """

    def __init__(self, claims_file: Path, model_identifier: str = "manual") -> None:
        self.claims_file = claims_file
        self.model_identifier = model_identifier

    def extract(self, transcription: Transcription, artifact_qid: str) -> ClaimBatch:
        payload = json.loads(self.claims_file.read_text())
        if "extraction_run" not in payload:
            payload = {
                "extraction_run": run_metadata(self.model_identifier).model_dump(
                    mode="json"
                ),
                "claims": payload.get("claims", payload),
            }
        batch = ClaimBatch.model_validate(payload)
        problems = validate_against_manifest(batch)
        if problems:
            raise ValueError(
                "claims file failed manifest validation:\n  " + "\n  ".join(problems)
            )
        return batch
