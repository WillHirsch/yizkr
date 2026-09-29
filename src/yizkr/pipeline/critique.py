"""Extraction critic (ADR-0013 mechanism C).

One adversarial LLM turn that reviews an extracted batch for the
confident-but-wrong assignments the first pass will not self-report — an
agent typed as a place, a role/field label captured as a name, an entity
dumped into the escape hatch that plainly fits a real kind. It FLAGS,
never rewrites: findings are surfaced to the human curator at the
existing review gate. It is the reconciliation adjudicator's pattern
applied to extraction, and it is what catches the "MOTHER as a name" and
"hat shop as a place" errors without a human eyeballing raw output.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, computed_field

from yizkr.claims import ClaimBatch, ItemValue
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)

logger = logging.getLogger(__name__)


def axis_of(target: str) -> str | None:
    """Which universal statement-part a Finding concerns (ADR-0023),
    derived from the `target` grammar: ``claim: e01 prop`` -> predicate,
    ``e01 alias: X`` -> value, bare ``e01`` -> subject. Unrecognised ->
    None. This is the general form of ADR-0022 §6's per-name axes,
    addressable across every property; existence-axis concerns have no
    target form and stay None.
    """
    text = target.strip()
    if text.startswith("claim:"):
        return "predicate"
    if "alias:" in text:
        return "value"
    if text and " " not in text:
        return "subject"
    return None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str  # "e01", "e01 alias: MOTHER", or "claim: e01 occupation"
    issue: str
    suggestion: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def axis(self) -> str | None:
        """subject | predicate | value (ADR-0023), derived from `target`."""
        return axis_of(self.target)


CRITIQUE_PROMPT = """\
You review a genealogical extraction for likely mistakes before a human
curator sees it. You FLAG concerns only; you never rewrite.

Flag, as a genuine concern only:
- an entity whose kind is wrong — an AGENT (company, bank, committee,
  agency, school) typed as `place` or `human`; a place typed as
  `human`; a person typed as a place;
- an alias that is a ROLE or FORM-FIELD label, not a name — MOTHER,
  FATHER, CHILD, INFORMANT, BRIDE, GROOM, WITNESS — or a relational /
  perspectival reference ("my husband", "our great grandmother", "Oma",
  "Nana Maisie", "the deceased") recorded as if it were a name. These are
  not names: suggest recording the term as `relative` + `kinship_type`
  (a literal kinship term) or `referred_to_as` (a courtesy/pet name),
  NOT as an alias (ADR-0018);
- an entity marked `unclassified_entity` that clearly fits a real kind
  (the escape hatch was used when it should not have been);
- a claim whose property is plainly misapplied.

A relational appellation CORRECTLY recorded as a `referred_to_as` claim,
or a kinship term as `relative` + `kinship_type`, is right — do not flag
it.

Entities (id | kind | label | aliases):
{entities_block}

Claims (subject | property | value):
{claims_block}

Output NDJSON, one finding per line, no prose, no code fences:
{{"target": "<entity id, or 'e01 alias: MOTHER', or 'claim: e01 occupation'>",
"issue": "<one line>",
"suggestion": "<one line: likely-correct kind/name/property, or 'drop'>"}}
Emit a line ONLY for a genuine concern; emit NONE if the extraction
looks sound. After the last finding, end with exactly one final line:
{{"done": true}}
"""


def _entities_block(batch: ClaimBatch) -> str:
    lines = []
    for entity in batch.entities:
        aliases = ", ".join(entity.aliases) if entity.aliases else "-"
        hint = f" (hint: {entity.type_hint})" if entity.type_hint else ""
        lines.append(
            f"{entity.id} | {entity.kind}{hint} | {entity.label} | {aliases}"
        )
    return "\n".join(lines)


def _claims_block(batch: ClaimBatch) -> str:
    lines = []
    for claim in batch.claims:
        subject = getattr(claim.subject, "local_id", None) or getattr(
            claim.subject, "qid", "?"
        )
        value = claim.value
        if isinstance(value, ItemValue):
            rendered = (
                getattr(value.value, "label", None)
                or getattr(value.value, "local_id", None)
                or getattr(value.value, "qid", "?")
            )
        elif value.type == "monolingual":
            rendered = value.value.text
        else:
            rendered = str(getattr(value, "value", value))
        lines.append(f"{subject} | {claim.property} | {rendered}")
    return "\n".join(lines)


def run_critique(client: LlmClient, batch: ClaimBatch) -> list[Finding]:
    """Review the batch; return flagged concerns (possibly empty).

    Advisory only — an unfinished answer never raises (unlike chunk
    extraction): the critic must never block the pipeline, only inform
    the reviewer. An answer the engine stopped at its output limit
    (llm.at_output_limit), or one without its terminal sentinel (a
    refusal, an answer cut off, a usage-limit notice), still gives the
    findings it holds, and is rejected through the client
    (llm.reject_response), so that a client that keeps answers asks the
    critic again next time rather than serving it. One the client kept
    from an earlier call is asked for once more, and the engine's answer
    is used in its place, as it would be without the cache. A call the
    engine gave up on at its output limit (llm.OutputLimitReached) gave no
    answer: there are no findings, and nothing to reject, since the cache
    keeps nothing for it.
    """
    if not batch.entities and not batch.claims:
        return []
    prompt = CRITIQUE_PROMPT.format(
        entities_block=_entities_block(batch) or "(none)",
        claims_block=_claims_block(batch) or "(none)",
    )
    try:
        response = client.complete(prompt)
        findings, unfinished = _read(client, response)
        if unfinished and _reject(client, response, unfinished):
            response = client.complete(prompt)
            findings, unfinished = _read(client, response)
            if unfinished:
                _reject(client, response, unfinished)
    except OutputLimitReached:
        logger.warning("critique %s: no findings", CUT_OFF_AT_LIMIT)
        return []
    return findings


def _read(client: LlmClient, response: str) -> tuple[list[Finding], str]:
    """(the findings a critique answer holds, why the answer is unfinished,
    or "" when it is not): the engine stopped it at its output limit
    (llm.at_output_limit), or it has no terminal sentinel. `response` is
    the answer `client` has just given."""
    findings, finished = _parse_critique(response)
    if at_output_limit(client, response):
        return findings, f"critique: {CUT_OFF_AT_LIMIT}"
    if not finished:
        return findings, "critique: no terminal sentinel"
    return findings, ""


def _parse_critique(response: str) -> tuple[list[Finding], bool]:
    """(the findings a critique answer holds, whether it has its terminal
    sentinel, a line whose `done` is true)."""
    findings: list[Finding] = []
    finished = False
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        if "target" in obj and "issue" in obj:
            try:
                findings.append(Finding.model_validate(obj))
            except Exception:
                continue
    return findings, finished


def _reject(client: LlmClient, response: str, reason: str) -> bool:
    """Reject an unfinished critique answer for `reason`; True when it was
    one the client kept from an earlier call (llm.reject_response). A
    rejection that fails is only a warning: the critic never blocks the
    pipeline."""
    try:
        return reject_response(client, response, reason)
    except Exception as exc:
        logger.warning("could not reject a critique answer: %s", exc)
        return False
