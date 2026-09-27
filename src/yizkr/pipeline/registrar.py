"""Artifact registrar: media upload + artifact/fragment items (ADR-0003 §4).

Runs BEFORE extraction so that claims can reference artifact items by
QID. Writes, per artifact:

* the media file, uploaded to the wiki (W1: artifacts as wiki media,
  ADR-0004);
* an artifact item: label, description, ``instance of -> <type>`` and
  ``media file -> File:...``;
* optional page-fragment items: ``instance of -> artifact fragment``,
  ``part of -> <artifact>`` with a ``page number`` qualifier.

Provenance of registrar statements: an artifact asserts its own kind
and its own composition by existing, so `instance of`, `media file`,
and fragment `part of` statements are referenced ``stated in -> the
artifact item itself`` (see docs/MODELING.md, "Artifacts and
registration"). No statement is written unreferenced (ADR-0003).

Idempotent, keyed on content (ADR-0010): when media is present the
artifact item is matched by its SHA-1 content hash first, so the same
bytes under a different filename reuse the one item (the alternate
filename is kept as an alias) instead of minting a duplicate that
shares a blob. Falls back to the exact English label when there is no
media to hash (re-registration). Statements that already exist are
skipped; re-uploading identical media is a no-op (MediaWiki dedups the
blob by SHA-1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.core.wiki import WikiClient


@dataclass
class RegistrationResult:
    artifact_qid: str
    file_page: str | None
    fragment_qids: dict[int, str] = field(default_factory=dict)
    created: bool = True


def _append(entity, claim) -> None:
    """Add a claim WITHOUT touching existing ones (attributed statements
    coexist and are never overwritten — ADR-0002): WBI's
    claims.add() defaults to REPLACE_ALL for same-property claims."""
    from wikibaseintegrator.wbi_enums import ActionIfExists

    entity.claims.add(claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)


def _existing_claim_targets(entity, pid: str) -> set[str]:
    targets = set()
    for claim in entity.claims.get(pid) or []:
        value = (claim.mainsnak.datavalue or {}).get("value", {})
        if isinstance(value, dict) and "id" in value:
            targets.add(value["id"])
    return targets


def _existing_claim_strings(entity, pid: str) -> set[str]:
    values = set()
    for claim in entity.claims.get(pid) or []:
        value = (claim.mainsnak.datavalue or {}).get("value")
        if isinstance(value, str):
            values.add(value)
    return values


def _statement_key(claim) -> tuple:
    """Property, value and qualifiers of a statement, ignoring its
    references and snak hashes. Same value with different qualifiers is
    a different assertion (ADR-0002), as in the writer's matching."""

    def value_key(datavalue) -> str:
        value = (datavalue or {}).get("value")
        if isinstance(value, dict) and "id" in value:
            return value["id"]
        return json.dumps(value, sort_keys=True)

    qualifiers = sorted(
        (pid, value_key(snak.datavalue))
        for pid, snaks in claim.qualifiers.qualifiers.items()
        for snak in snaks
    )
    return (
        claim.mainsnak.property_number,
        value_key(claim.mainsnak.datavalue),
        tuple(qualifiers),
    )


def _cites(claim, stated_in_pid: str, source_qid: str) -> bool:
    """Does any reference block on `claim` cite `stated in -> source`?"""
    for reference in claim.references.references:
        for snak in reference.snaks.get(stated_in_pid) or []:
            value = (snak.datavalue or {}).get("value") or {}
            if isinstance(value, dict) and value.get("id") == source_qid:
                return True
    return False


def _ensure_referenced(
    entity, claim, stated_in_pid: str, source_qid: str
) -> str:
    """Make `entity` carry `claim` as asserted by `source_qid`, additively.

    * no equal statement (same property, value AND qualifiers): `claim`
      is appended as a new statement -> "added";
    * an equal statement exists but cites no `stated in -> source`: the
      claim's reference blocks are appended to it, an agreeing second
      source (the writer's rule; ADR-0002, ADR-0003) -> "referenced";
    * already cited -> "present" (safe re-run).

    Existing statements and their references are never removed or
    rewritten (CLAUDE.md rule 3). The caller writes the entity.
    """
    key = _statement_key(claim)
    for existing in entity.claims.get(claim.mainsnak.property_number):
        if _statement_key(existing) != key:
            continue
        if _cites(existing, stated_in_pid, source_qid):
            return "present"
        for reference in claim.references.references:
            existing.references.add(reference)
        return "referenced"
    _append(entity, claim)
    return "added"


def _item_claim(pid: str, qid: str, stated_in_pid: str, source_qid: str):
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References

    reference = Reference()
    reference.add(Item(prop_nr=stated_in_pid, value=source_qid))
    references = References()
    references.add(reference)
    return Item(prop_nr=pid, value=qid, references=references)


def _string_claim(pid: str, value: str, stated_in_pid: str, source_qid: str):
    from wikibaseintegrator.datatypes import Item, String
    from wikibaseintegrator.models import Reference, References

    reference = Reference()
    reference.add(Item(prop_nr=stated_in_pid, value=source_qid))
    references = References()
    references.add(reference)
    return String(prop_nr=pid, value=value, references=references)


def register_artifact(
    client: WikiClient,
    media_path: Path | None,
    artifact_type: str,
    title: str,
    description: str | None = None,
    pages: int = 0,
) -> RegistrationResult:
    """Ensure media, artifact item, and page fragments exist. Returns ids.

    `artifact_type` is a manifest item key (photograph, certificate,
    letter, census_page, newspaper_clipping, or artifact itself).
    """
    properties, items = client.vocabulary()
    if artifact_type not in items:
        raise ValueError(
            f"unknown artifact type {artifact_type!r}; expected a manifest "
            f"item key such as photograph or certificate"
        )
    instance_of = properties["instance_of"]
    part_of = properties["part_of"]
    stated_in = properties["stated_in"]
    media_file = properties["media_file"]
    media_sha1 = properties["media_sha1"]
    page_number = properties["page_number"]

    file_page: str | None = None
    content_sha1: str | None = None
    if media_path is not None:
        # hash before upload: the content identity is the file's bytes,
        # independent of the name MediaWiki files it under (ADR-0010)
        content_sha1 = hashlib.sha1(media_path.read_bytes()).hexdigest()
        file_page = client.upload_file(media_path)

    # -- locate the artifact item: content hash is the identity key -------
    # Identical bytes are one artifact however they are named. With media
    # present the hash decides; the label is consulted only to adopt an
    # artifact item that carries no hash (same title; the hash is
    # backfilled below). A same-title item that already carries a
    # DIFFERENT hash is genuinely different content and must not be
    # collapsed onto. With no media to hash, the label is the key.
    reused_by_hash = False
    existing_qid = None
    if content_sha1 is not None:
        existing_qid = client.string_statement_index(media_sha1).get(
            content_sha1
        )
        reused_by_hash = existing_qid is not None
        if existing_qid is None:
            labelled = client.find_item_by_label(title)
            if labelled is not None and not _existing_claim_strings(
                client.wbi.item.get(labelled), media_sha1
            ):
                existing_qid = labelled  # unhashed item: adopt and backfill
    else:
        existing_qid = client.find_item_by_label(title)

    if existing_qid:
        artifact = client.wbi.item.get(existing_qid)
        created = False
    else:
        # new content. Labels are unique per kind (the wiki's idempotency
        # convention), so if this title already names DIFFERENT content,
        # disambiguate with a short content tag rather than collide.
        label = title
        if content_sha1 is not None and client.find_item_by_label(title):
            label = f"{title} [{content_sha1[:8]}]"
        artifact = client.wbi.item.new()
        artifact.labels.set("en", label)
        if description:
            artifact.descriptions.set("en", description)
        artifact = artifact.write(summary="ingest: register artifact")
        created = True
    artifact_qid = artifact.id

    # same bytes under a new filename: keep the new name as an alias
    # (name variants live in aliases — docs/MODELING.md)
    if reused_by_hash:
        current_label = str(artifact.labels.get("en") or "")
        aliases = {str(alias) for alias in (artifact.aliases.get("en") or [])}
        if title != current_label and title not in aliases:
            artifact.aliases.set("en", sorted(aliases | {title}))
            artifact.write(summary="ingest: alternate filename as alias")

    changed = False
    if items[artifact_type] not in _existing_claim_targets(
        artifact, instance_of
    ):
        _append(
            artifact,
            _item_claim(
                instance_of, items[artifact_type], stated_in, artifact_qid
            ),
        )
        changed = True
    if file_page and file_page not in _existing_claim_strings(
        artifact, media_file
    ):
        _append(
            artifact,
            _string_claim(media_file, file_page, stated_in, artifact_qid),
        )
        changed = True
    # backfill the content hash (also covers an unhashed item matched
    # above by label)
    if content_sha1 and content_sha1 not in _existing_claim_strings(
        artifact, media_sha1
    ):
        _append(
            artifact,
            _string_claim(media_sha1, content_sha1, stated_in, artifact_qid),
        )
        changed = True
    if changed:
        artifact.write(summary="ingest: artifact statements")

    # -- page fragments ----------------------------------------------------
    fragment_qids: dict[int, str] = {}
    for page in range(1, pages + 1):
        fragment_qids[page] = _ensure_fragment(
            client,
            artifact_qid,
            title,
            page,
            instance_of=instance_of,
            part_of=part_of,
            stated_in=stated_in,
            page_number=page_number,
            fragment_class=items["artifact_fragment"],
        )

    return RegistrationResult(
        artifact_qid=artifact_qid,
        file_page=file_page,
        fragment_qids=fragment_qids,
        created=created,
    )


def _ensure_fragment(
    client: WikiClient,
    artifact_qid: str,
    title: str,
    page: int,
    *,
    instance_of: str,
    part_of: str,
    stated_in: str,
    page_number: str,
    fragment_class: str,
) -> str:
    from wikibaseintegrator.datatypes import String

    label = f"{title}, page {page}"
    existing = client.find_item_by_label(label)
    if existing:
        return existing
    fragment = client.wbi.item.new()
    fragment.labels.set("en", label)
    fragment.descriptions.set("en", f"page {page} of {title}")
    fragment = fragment.write(summary="ingest: register fragment")

    _append(
        fragment,
        _item_claim(instance_of, fragment_class, stated_in, artifact_qid),
    )
    part_claim = _item_claim(part_of, artifact_qid, stated_in, artifact_qid)
    part_claim.qualifiers.add(String(prop_nr=page_number, value=str(page)))
    _append(fragment, part_claim)
    fragment.write(summary="ingest: fragment statements")
    return fragment.id
