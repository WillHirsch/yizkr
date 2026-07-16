"""Extraction critic (ADR-0013 mechanism C).

One adversarial LLM turn that reviews an extracted batch for the
confident-but-wrong assignments the first pass will not self-report — an
agent typed as a place, a role/field label captured as a name, an entity
dumped into the escape hatch that plainly fits a real kind. It FLAGS,
never rewrites: findings are surfaced to the human curator at the
existing review gate. It is the reconciliation adjudicator's pattern
applied to extraction, and it is what catches the "MOTHER as a name" and
"mill as a place" errors without a human eyeballing raw output.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from yizkr.claims import ClaimBatch, ItemValue
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import LlmClient


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str  # "e01", "e01 alias: MOTHER", or "claim: e01 occupation"
    issue: str
    suggestion: str = ""


CRITIQUE_PROMPT = """\
You review a genealogical extraction for likely mistakes before a human
curator sees it. You FLAG concerns only; you never rewrite.

Flag, as a genuine concern only:
- an entity whose kind is wrong — an AGENT (company, bank, committee,
  agency, school) typed as `place` or `human`; a place typed as
  `human`; a person typed as a place;
- an alias that is a ROLE or FORM-FIELD label, not a name — MOTHER,
  FATHER, CHILD, INFORMANT, BRIDE, GROOM, WITNESS — or a descriptive
  phrase ("my husband", "the deceased", "our friends") recorded as if it
  were a name;
- an entity marked `unclassified_entity` that clearly fits a real kind
  (the escape hatch was used when it should not have been);
- a claim whose property is plainly misapplied.

Entities (id | kind | label | aliases):
{entities_block}

Claims (subject | property | value):
{claims_block}

Output NDJSON, one finding per line, no prose, no code fences:
{{"target": "<entity id, or 'e01 alias: MOTHER', or 'claim: e01 occupation'>",
"issue": "<one line>", "suggestion": "<one line: likely-correct kind/name/property, or 'drop'>"}}
Emit a line ONLY for a genuine concern; emit NONE if the extraction
looks sound. After the last finding, end with exactly one final line:
{{"done": true, "findings_emitted": K}}
"""


def _entities_block(batch: ClaimBatch) -> str:
    lines = []
    for entity in batch.entities:
        aliases = ", ".join(entity.aliases) if entity.aliases else "-"
        hint = f" (hint: {entity.type_hint})" if entity.type_hint else ""
        lines.append(f"{entity.id} | {entity.kind}{hint} | {entity.label} | {aliases}")
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

    Advisory only — a malformed or missing sentinel never raises (unlike
    chunk extraction): the critic must never block the pipeline, only
    inform the reviewer.
    """
    if not batch.entities and not batch.claims:
        return []
    prompt = CRITIQUE_PROMPT.format(
        entities_block=_entities_block(batch) or "(none)",
        claims_block=_claims_block(batch) or "(none)",
    )
    response = client.complete(prompt)
    findings: list[Finding] = []
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            continue
        if "target" in obj and "issue" in obj:
            try:
                findings.append(Finding.model_validate(obj))
            except Exception:
                continue
    return findings
