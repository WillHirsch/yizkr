"""Claim extraction step: transcription -> ClaimBatch JSON.

`LlmClaimExtractor` prompts an LLM with the claim JSON Schema, the
manifest vocabulary, and the transcription; the response is parsed and
validated against the schema AND the model manifest before it goes
anywhere. Validation is per claim: claims that pass proceed (to human
review — nothing machine-written reaches the graph unreviewed), claims
that fail are reported as rejects with their reasons. Malformed machine
output must never limp toward the graph, but one bad claim must not
discard an expensive extraction either.

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
from functools import lru_cache
from pathlib import Path
from typing import Any

import yizkr
from yizkr.claims import (
    ClaimBatch,
    ExtractionRunMetadata,
    load_vocabulary,
    validate_against_manifest,
)
from yizkr.model.schema import load_manifest
from yizkr.pipeline.llm import LlmClient, reject_response
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
  written, instance_of from: {kinds}).
- Use ONLY these property keys (property key: datatype — what it means),
  for claims and qualifiers alike — a claim naming any other key is
  discarded. The description is authoritative: it says what the VALUE
  must be, and so which way a relation points. If the document asserts
  something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- Dating a marriage, a residence or any other period: a marriage's date
  is `start_time` on the `spouse` claim, and the date the source gives
  for its end (a divorce, an annulment, widowhood) is its `end_time`; a
  move in and a move out date a `residence` the same way. `point_in_time`
  on a spouse or residence claim only records a date at which the source
  shows the marriage or the residence holding (a census, a directory
  entry), never a wedding date. A range bounds one end: a start takes
  `earliest_date` and `latest_start_date`, an end takes
  `earliest_end_date` and `latest_date`. Married "between X and Y" is
  start_time X with earliest_date X and latest_start_date Y; divorced
  "before Z" is end_time Z with latest_date Z.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of the JSON Schema below. After the last claim, end with
exactly one final line:
{{"done": true, "claims_emitted": N}}
where N is the number of claim lines you emitted.

{schema}

Transcription of artifact {artifact_qid} (language: {language}):

{text}
"""


def run_metadata(model_identifier: str) -> ExtractionRunMetadata:
    return ExtractionRunMetadata(
        model_identifier=model_identifier,
        pipeline_version=yizkr.__version__,
        software_commit=_software_commit(),
        started_at=datetime.now(UTC),
    )


# The commit git gave for this code's checkout, once it has given one.
_commit: str | None = None


def _software_commit() -> str | None:
    """The short commit of the git checkout this code runs from, or None
    when git gives none (see _ask_git_for_commit).

    Every batch asks git until it answers, so a failed ask costs only its
    own batch the commit. The first answer is kept for the process, and
    later batches name the same commit without starting git; batches that
    ask at the same moment as the first may each start git."""
    global _commit
    if _commit is None:
        commit = _ask_git_for_commit()
        if commit is None:
            return None
        _commit = commit
    return _commit


def _ask_git_for_commit() -> str | None:
    """git's short commit for the checkout this code runs from, or None
    when git cannot be started (it is missing, say, or the process is out
    of processes or descriptors), exits with an error (outside a checkout)
    or prints no commit. A git that runs past the timeout raises
    subprocess.TimeoutExpired."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent,
        )
    except OSError:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def scan_json_objects(response: str) -> list[dict]:
    """Every JSON object decodable from model output, in order.

    Tolerates code fences, surrounding prose, NDJSON, and corrupt
    regions (scanning resumes after any undecodable span). Objects
    nested inside a decoded object are not yielded separately."""
    text = response.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        index = end
    return objects


def _parse_response(response: str) -> dict:
    """Extract claim JSON from model output.

    Accepts NDJSON claim lines (the format the prompt asks for), a
    wrapped {"claims": [...]} object or several concatenated ones
    (continuation chunks), or any mix. Salvage property: a
    response that spans several turns can carry an inexact seam that
    corrupts an enclosing object — every individual claim object that
    still decodes is collected regardless, so one bad seam cannot
    discard an entire expensive extraction. Duplicates (models overlap
    at resume points) are dropped."""
    merged: list = []
    salvaged: list = []
    found = False
    for obj in scan_json_objects(response):
        if "claims" in obj:
            found = True
            merged.extend(obj.get("claims") or [])
        elif "subject" in obj and "property" in obj:
            salvaged.append(obj)
    if not found and not salvaged:
        raise ValueError(
            f"no claim JSON in extractor response (neither a 'claims' "
            f"object nor loose claim objects): {response.strip()[:200]!r}"
        )
    claims = merged + salvaged
    deduped: list = []
    for claim in claims:
        if claim not in deduped:
            deduped.append(claim)
    return {"claims": deduped}


def _normalize_claim_shape(claim: dict) -> dict:
    """Repair a mechanical output-shape slip models actually make.

    Item values nest under "value" ({"type": "item", "value": {...}}),
    and models sometimes wrap time values the same way:
    {"type": "time", "value": {"year": ...}} instead of the flat
    {"type": "time", "year": ...} the schema requires. The content is
    unambiguous, so flatten rather than reject 10 minutes of output.
    """

    def flatten(value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("type") == "time"
            and isinstance(value.get("value"), dict)
            and "year" in value["value"]
        ):
            return {"type": "time", **value["value"]}
        return value

    fixed = dict(claim)
    fixed["value"] = flatten(claim.get("value"))
    if isinstance(claim.get("qualifiers"), list):
        fixed["qualifiers"] = [
            {**qualifier, "value": flatten(qualifier.get("value"))}
            if isinstance(qualifier, dict)
            else qualifier
            for qualifier in claim["qualifiers"]
        ]
    # excerpts are verification aids ("short" per ADR-0003) and Wikibase
    # caps text values at 400 chars; clip visibly rather than lose the claim
    source = fixed.get("source")
    if isinstance(source, dict):
        excerpt = source.get("transcription_excerpt")
        if isinstance(excerpt, dict) and len(excerpt.get("text") or "") > 400:
            fixed["source"] = {
                **source,
                "transcription_excerpt": {
                    **excerpt,
                    "text": excerpt["text"][:397].rsplit(" ", 1)[0] + "…",
                },
            }
    return fixed


def salvage_valid_claims(
    claims: list, extraction_run: dict, entities: list[dict] | None = None
) -> tuple[ClaimBatch, list[dict]]:
    """Partition raw claim dicts into a valid ClaimBatch and rejects.

    Machine output is expensive and reviewed by a human either way, so a
    claim that fails schema or manifest validation must not sink the
    claims that pass: the valid subset proceeds to review, the rejects
    are reported (each with its reasons) for diagnosis.

    With `entities` (an entity manifest, ADR-0007), local refs naming
    ids outside the manifest are rejected, and the manifest travels in
    the returned batch.
    """
    vocabulary = load_vocabulary()
    entities = entities or []
    if entities:
        # manifest-level defects poison every claim; check them once,
        # loudly, before per-claim triage
        manifest_only = ClaimBatch.model_validate(
            {"extraction_run": extraction_run, "entities": entities}
        )
        manifest_problems = validate_against_manifest(
            manifest_only, vocabulary=vocabulary
        )
        if manifest_problems:
            raise ValueError(
                "entity manifest failed validation:\n  "
                + "\n  ".join(manifest_problems)
            )
    valid: list = []
    rejects: list[dict] = []
    for claim in claims:
        if isinstance(claim, dict):
            claim = _normalize_claim_shape(claim)
        try:
            batch = ClaimBatch.model_validate(
                {
                    "extraction_run": extraction_run,
                    "claims": [claim],
                    "entities": entities,
                }
            )
        except Exception as exc:  # pydantic ValidationError, in short form
            rejects.append({"claim": claim, "problems": [str(exc)]})
            continue
        problems = validate_against_manifest(batch, vocabulary=vocabulary)
        if problems:
            rejects.append({"claim": claim, "problems": problems})
        else:
            valid.append(claim)
    return (
        ClaimBatch.model_validate(
            {
                "extraction_run": extraction_run,
                "claims": valid,
                "entities": entities,
            }
        ),
        rejects,
    )


def _one_line(text: str) -> str:
    """Collapse a folded YAML description to a single prompt line."""
    return " ".join(text.split())


# The renderers below run for every extraction prompt, chunk prompts
# included, and what they render cannot change mid-run: the manifest
# ships as package data, and the claim schema is built from the claim
# models. Rendering parses and validates the whole manifest, or builds
# the whole schema, so each is cached and a hundred-chunk document pays
# for it once.


@lru_cache(maxsize=1)
def vocabulary_prompt_lines() -> tuple[str, str]:
    """Render the manifest vocabulary for the extraction prompt.

    Each property carries its manifest description, because that
    description is where the semantics a model needs to place a claim
    correctly already live: which way a relation points (`employer` is
    the organization, not the employee) and what kind of entity a value
    must be (`location` takes a place). Keys and datatypes alone would
    leave the model to guess both, and the manifest already holds the
    words.
    """
    manifest = load_manifest()
    property_lines = "\n".join(
        f"  {key}: {spec.datatype} — {_one_line(spec.description)}"
        for key, spec in sorted(manifest.properties.items())
    )
    item_lines = "  " + ", ".join(sorted(manifest.items))
    return property_lines, item_lines


@lru_cache(maxsize=1)
def entity_kind_prompt_lines() -> tuple[str, str]:
    """Render the entity kinds for the manifest pass (ADR-0007).

    Returns (bare comma-joined keys, described block). The kinds are the
    manifest items flagged `entity_kind`, so the offerable set is
    declared in one place rather than restated in the prompt.
    """
    kinds = sorted(
        (
            (key, spec)
            for key, spec in load_manifest().items.items()
            if spec.entity_kind
        ),
        key=lambda entry: entry[0],
    )
    if not kinds:
        raise ValueError("manifest declares no entity_kind items")
    return (
        ", ".join(key for key, _ in kinds),
        "\n".join(
            f"  {key}: {_one_line(spec.description)}" for key, spec in kinds
        ),
    )


@lru_cache(maxsize=1)
def claim_schema_prompt_text() -> str:
    """The claim JSON Schema, as the single-call and chunk prompts carry
    it."""
    return json.dumps(ClaimBatch.model_json_schema(), indent=2)


class LlmClaimExtractor:
    """Single-call extraction: one prompt, one NDJSON answer.

    An answer the extractor cannot fully use is rejected through the
    client (llm.reject_response), so that a client that keeps answers
    asks the engine again next time rather than serving it: one with no
    claim JSON or no valid claim (the extraction then fails), and one
    without its terminal sentinel, which may be cut off (the claims it
    holds are still returned). An answer the client kept from an earlier
    call is asked for once more when it is rejected, and the engine's
    answer is used in its place, as it would be without the cache."""

    def __init__(
        self,
        client: LlmClient,
        raw_dump: Path | None = None,
        rejects_dump: Path | None = None,
    ) -> None:
        self.client = client
        self.raw_dump = raw_dump
        self.rejects_dump = rejects_dump

    def extract(
        self, transcription: Transcription, artifact_qid: str
    ) -> ClaimBatch:
        property_keys, item_keys = vocabulary_prompt_lines()
        prompt = EXTRACTION_PROMPT.format(
            schema=claim_schema_prompt_text(),
            property_keys=property_keys,
            item_keys=item_keys,
            kinds=entity_kind_prompt_lines()[0],
            artifact_qid=artifact_qid,
            language=transcription.language,
            text=transcription.text,
        )
        try:
            return self._extract(prompt)
        except _KeptAnswerRejected:
            return self._extract(prompt, again=True)

    def _extract(self, prompt: str, again: bool = False) -> ClaimBatch:
        """The batch one answer gives. A rejected answer the client kept
        from an earlier call raises _KeptAnswerRejected, unless this is
        the call made `again` after one."""

        def reject_answer(response: str, reason: str) -> None:
            if reject_response(self.client, response, reason) and not again:
                raise _KeptAnswerRejected(reason)

        response = self.client.complete(prompt)
        if self.raw_dump is not None:
            # Engine output is expensive; persist it before anything that
            # can fail, so a parse/validation error is debuggable and the
            # response recoverable.
            self.raw_dump.parent.mkdir(parents=True, exist_ok=True)
            self.raw_dump.write_text(response)
        try:
            payload = _parse_response(response)
        except ValueError:
            reject_answer(response, "extraction: no claim JSON")
            raise
        finished = any(
            obj.get("done") is True for obj in scan_json_objects(response)
        )
        batch, rejects = salvage_valid_claims(
            payload.get("claims", []),
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
        )
        if self.rejects_dump is not None:
            if rejects:
                self.rejects_dump.parent.mkdir(parents=True, exist_ok=True)
                self.rejects_dump.write_text(json.dumps(rejects, indent=2))
            else:
                self.rejects_dump.unlink(missing_ok=True)  # no stale rejects
        if not batch.claims:
            reject_answer(response, "extraction: no valid claim")
            detail = "\n  ".join(
                problem
                for reject in rejects[:20]
                for problem in reject["problems"]
            )
            raise ValueError(
                f"every extracted claim failed validation "
                f"({len(rejects)} rejects):\n  {detail}"
            )
        if not finished:
            reject_answer(response, "extraction: no terminal sentinel")
        return batch


class _KeptAnswerRejected(Exception):
    """An answer the client kept from an earlier call was rejected: the
    engine has not answered the call yet, and is asked once more."""


class ClaimFileExtractor:
    """Validate a prepared claims file into a batch (manual/offline flows).

    The file holds either a full ClaimBatch or a bare {"claims": [...]}
    object; in the latter case run metadata is generated here.
    """

    def __init__(
        self, claims_file: Path, model_identifier: str = "manual"
    ) -> None:
        self.claims_file = claims_file
        self.model_identifier = model_identifier

    def extract(
        self, transcription: Transcription, artifact_qid: str
    ) -> ClaimBatch:
        payload = json.loads(self.claims_file.read_text())
        if "extraction_run" not in payload:
            payload = {
                "extraction_run": run_metadata(
                    self.model_identifier
                ).model_dump(mode="json"),
                "claims": payload.get("claims", payload),
            }
        batch = ClaimBatch.model_validate(payload)
        problems = validate_against_manifest(batch)
        if problems:
            raise ValueError(
                "claims file failed manifest validation:\n  "
                + "\n  ".join(problems)
            )
        return batch
