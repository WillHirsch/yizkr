"""Exact graph support primitives for an already authorized application.

These internal operations do not establish identity or claim approval. Their
caller must publish the durable application intent and protect its authority
before applying a payload. Inspection uses authoritative item reads; no cache,
label lookup or first-equal-statement choice supplies a support identity.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from wikibaseintegrator.models import Claim as WbiClaim
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import ActionIfExists

from yizkr.claims import ArtifactRef, Claim, LocalRef, QidRef, VocabRef
from yizkr.core.write_recovery import write_existing
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.writer import BatchWriter, WriteResult

_QID = r"^Q[1-9][0-9]*$"
_HASH = re.compile(r"^[0-9a-f]{40}$")
_REFERENCE_PROPERTIES = (
    "stated_in",
    "inferred_from_extraction_run",
    "judgement",
)


class SupportRefused(ValueError):
    """Exact support cannot be established under current graph conditions."""


class StaleSupport(SupportRefused):
    """The target item changed after its application preflight."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupportPayload(_Record):
    qid: str = Field(pattern=_QID)
    statement: dict[str, Any]
    primary: dict[str, Any]
    circumstance: dict[str, Any] | None = None
    process: dict[str, Any] | None = None
    properties: dict[str, str | None]


class SupportState(_Record):
    qid: str = Field(pattern=_QID)
    revision: int = Field(gt=0)
    guid: str | None = None
    primary_hash: str | None = None
    circumstance_hash: str | None = None
    # Observed statement-level context; these do not associate an old source
    # reference with one particular process when several share the statement.
    process_hashes: tuple[str, ...] = ()
    primary: dict[str, Any] | None = None
    circumstance: dict[str, Any] | None = None
    observed_process: tuple[dict[str, Any], ...] = ()
    complete: bool = False
    missing_references: tuple[dict[str, Any], ...] = ()


def resolve_artifacts(client, sha1s: Iterable[str]) -> dict[str, str]:
    """Resolve unique current source hashes, refusing retired-only matches."""
    from yizkr.pipeline.source_bindings import unique_source_qids

    wanted = {ArtifactRef(sha1=sha1).sha1 for sha1 in sha1s}
    properties, _ = client.vocabulary()
    try:
        return unique_source_qids(client, properties["media_sha1"], wanted)
    except ValueError as exc:
        raise SupportRefused(str(exc)) from exc


def _qid(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(_QID, value) is None:
        raise SupportRefused("support resolution requires a concrete item")
    return value


def prepare_support(
    writer: BatchWriter,
    claim: Claim,
    *,
    local_qids: Mapping[str, str],
    artifact_qids: Mapping[str, str],
    run_qid: str | None = None,
) -> SupportPayload:
    """Serialize one claim without graph access or entity side effects.

    Local references remain in the caller's approved application with their
    role-specific assignment evidence. This representation is only the exact
    graph value and source blocks to inspect/apply under that authority.
    """
    claim = Claim.model_validate(claim.model_dump(mode="json"))

    def resolve(ref):
        try:
            if isinstance(ref, LocalRef):
                return _qid(local_qids[ref.local_id])
            if isinstance(ref, ArtifactRef):
                return _qid(artifact_qids[ref.sha1])
            if isinstance(ref, QidRef):
                return _qid(ref.qid)
            if isinstance(ref, VocabRef):
                return _qid(writer.items[ref.key])
        except KeyError as exc:
            raise SupportRefused(
                "support contains an unresolved reference"
            ) from exc
        raise SupportRefused("support contains an unsupported reference")

    subject = resolve(claim.subject)
    artifact = resolve(claim.source.artifact)
    if run_qid is not None:
        _qid(run_qid)
    result = WriteResult(run_qid=run_qid or "")
    statement = writer._to_wbi_claim(
        claim.property, claim.value, result, None, resolve=resolve
    )
    for qualifier in claim.qualifiers:
        statement.qualifiers.add(
            writer._to_wbi_claim(
                qualifier.property,
                qualifier.value,
                result,
                None,
                resolve=resolve,
            )
        )
    references = writer._references(
        artifact,
        None if claim.curated else run_qid,
        claim=claim,
        note=claim.note,
    )
    blocks = list(references.references)
    primary = blocks.pop(0).get_json()
    process = (
        blocks.pop(0).get_json()
        if run_qid is not None and not claim.curated
        else None
    )
    circumstance = (
        blocks.pop(0).get_json()
        if claim.circumstance_source is not None
        else None
    )
    raw = statement.get_json()
    serialized = {
        key: raw[key]
        for key in ("mainsnak", "type", "qualifiers")
        if key in raw
    }
    keys = {
        *_REFERENCE_PROPERTIES,
        claim.property,
        *(q.property for q in claim.qualifiers),
    }
    # Locator properties can appear in source blocks without being claim keys.
    used = {
        pid
        for block in (primary, circumstance, process)
        if block
        for pid in block["snaks"]
    }
    keys.update(key for key, pid in writer.properties.items() if pid in used)
    return SupportPayload(
        qid=subject,
        statement=serialized,
        primary=primary,
        circumstance=circumstance,
        process=process,
        properties={key: writer.properties.get(key) for key in sorted(keys)},
    )


def _json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _snak(snak: dict) -> str:
    """Keep complete values/types; only the server's snak hash is omitted."""
    return _json({key: value for key, value in snak.items() if key != "hash"})


def _snaks(data: dict) -> tuple[str, ...]:
    return tuple(
        sorted(_snak(snak) for snaks in data.values() for snak in snaks)
    )


def _statement_key(data):
    return _snak(data["mainsnak"]), _snaks(data.get("qualifiers", {}))


def same_statement(left: dict, right: dict) -> bool:
    """Compare full values and qualifiers, independent of transport order."""
    return _statement_key(left) == _statement_key(right)


def _reference_key(data):
    return _snaks(data["snaks"])


def _reference_json(reference):
    digest = reference.hash
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise SupportRefused(
            "authoritative source reference has no usable hash"
        )
    return {**reference.get_json(), "hash": digest}


def _payload(client, payload):
    payload = SupportPayload.model_validate(payload.model_dump(mode="json"))
    properties, _ = client.vocabulary()
    if not set(_REFERENCE_PROPERTIES) <= payload.properties.keys() or any(
        properties.get(key) != value
        for key, value in payload.properties.items()
    ):
        raise SupportRefused("support vocabulary changed since preparation")
    # The expected semantic property keys are retained, not merely arbitrary
    # property numbers copied out of an old graph or process record.
    known = set(payload.properties.values()) - {None}
    snaks = [payload.statement["mainsnak"]]
    snaks.extend(
        s
        for group in payload.statement.get("qualifiers", {}).values()
        for s in group
    )
    for block in (payload.primary, payload.circumstance, payload.process):
        if block:
            snaks.extend(s for group in block["snaks"].values() for s in group)
    if any(s.get("property") not in known for s in snaks):
        raise SupportRefused("support contains an unbound property")
    return payload


def _inspect(item, payload):
    revision = item.lastrevid
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
    ):
        raise SupportRefused("support target has no authoritative revision")
    wanted = _statement_key(payload.statement)
    matches = [
        s
        for s in item.claims.get(payload.statement["mainsnak"]["property"])
        or ()
        if _statement_key(s.get_json()) == wanted
    ]
    if len(matches) > 1:
        raise SupportRefused("equal graph statements have ambiguous support")
    if not matches:
        blocks = [payload.primary, payload.circumstance, payload.process]
        return SupportState(
            qid=payload.qid,
            revision=revision,
            missing_references=tuple(b for b in blocks if b),
        ), None
    statement = matches[0]
    if not statement.id or not statement.id.startswith(payload.qid + "$"):
        raise SupportRefused("matched statement has no authoritative GUID")
    references = list(statement.references)

    def exact(block):
        if block is None:
            return None
        found = [
            _reference_json(r)
            for r in references
            if _reference_key(r.get_json()) == _reference_key(block)
        ]
        hashes = {r["hash"] for r in found}
        if len(hashes) > 1:
            raise SupportRefused(
                "equal source blocks have ambiguous reference identities"
            )
        return found[0] if found else None

    primary = exact(payload.primary)
    circumstance = exact(payload.circumstance)
    process = exact(payload.process)
    run = payload.properties["inferred_from_extraction_run"]
    stated_in = payload.properties["stated_in"]
    observed = tuple(
        _reference_json(r) for r in references if r.snaks.get(run)
    )
    curated = not observed or statement.rank.value != "normal"
    missing = []
    for wanted_block, found in (
        (payload.primary, primary),
        (payload.circumstance, circumstance),
    ):
        if wanted_block is None or found is not None:
            continue
        sources = {_snak(s) for s in wanted_block["snaks"].get(stated_in, [])}
        cited = any(
            sources
            & {_snak(s.get_json()) for s in r.snaks.get(stated_in) or ()}
            for r in references
        )
        if curated and cited:
            raise SupportRefused(
                "curator source citation lacks the exact requested support"
            )
        missing.append(wanted_block)
    # New evidence carries its proposing process. Exact existing evidence does
    # not acquire a bare current run merely because it was applied again.
    noted = (
        payload.process
        and payload.properties.get("judgement") in payload.process["snaks"]
    )
    if payload.process and process is None and (missing or noted):
        missing.append(payload.process)
    return SupportState(
        qid=payload.qid,
        revision=revision,
        guid=statement.id,
        primary_hash=primary["hash"] if primary else None,
        circumstance_hash=circumstance["hash"] if circumstance else None,
        process_hashes=tuple(sorted({r["hash"] for r in observed})),
        primary=primary,
        circumstance=circumstance,
        observed_process=observed,
        complete=not missing,
        missing_references=tuple(missing),
    ), statement


def inspect_support(client, payload: SupportPayload) -> SupportState:
    """Inspect exact support using a fresh item and current vocabulary."""
    payload = _payload(client, payload)
    state, _ = _inspect(client.wbi.item.get(payload.qid), payload)
    return state


def _reference(data):
    return Reference().from_json(
        {"hash": None, "snaks-order": [], **copy.deepcopy(data)}
    )


def apply_support(
    client, payload: SupportPayload, expected_revision: int, summary: str
) -> SupportState:
    """Apply once under the caller's durable intent, then verify exact effects.

    No uncertain write is retried here. A caller must reread and reconcile its
    intent before deciding whether any further mutation is justified.
    """
    check_cancelled()
    payload = _payload(client, payload)
    item = client.wbi.item.get(payload.qid)
    if (
        type(expected_revision) is not int
        or expected_revision <= 0
        or item.lastrevid != expected_revision
    ):
        raise StaleSupport("support target revision changed")
    before, statement = _inspect(item, payload)
    if before.complete:
        return before
    if statement is None:
        statement = WbiClaim().from_json(
            {"id": "", "rank": "normal", **copy.deepcopy(payload.statement)}
        )
        statement.id = None
        for block in before.missing_references:
            statement.references.add(_reference(block))
        item.claims.add(
            statement, action_if_exists=ActionIfExists.FORCE_APPEND
        )
    else:
        for block in before.missing_references:
            statement.references.add(_reference(block))
    write_existing(item, summary=summary)
    after = inspect_support(client, payload)
    if not after.complete or (
        before.guid is not None and after.guid != before.guid
    ):
        raise SupportRefused(
            "graph write did not establish exact requested support"
        )
    return after
