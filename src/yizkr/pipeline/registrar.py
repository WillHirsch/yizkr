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
skipped. The bytes are uploaded only when none of the item's wiki files
holds them, and an upload never overwrites a wiki file or stores bytes
the wiki already holds (`WikiClient.upload_file`), so every `media file`
holds the bytes its item's `media SHA-1` names.
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


def _files_holding(
    client: WikiClient, file_pages: set[str], content_sha1: str
) -> list[str]:
    """The wiki files among `file_pages` whose current bytes have this
    SHA-1, by MediaWiki's own hash of each file (imageinfo)."""
    if not file_pages:
        return []
    data = client.api_get(
        {
            "action": "query",
            "titles": "|".join(sorted(file_pages)),
            "prop": "imageinfo",
            "iiprop": "sha1",
        }
    )
    query = data.get("query", {})
    # map API-normalised titles back to the statement values asked for
    asked = {
        entry["to"]: entry["from"] for entry in query.get("normalized", [])
    }
    return sorted(
        asked.get(page["title"], page["title"])
        for page in query.get("pages", {}).values()
        if (page.get("imageinfo") or [{}])[0].get("sha1") == content_sha1
    )


def _locate_by_content(
    client: WikiClient,
    hashed_qid: str | None,
    label: str,
    content_sha1: str,
    *,
    media_file: str,
    media_sha1: str,
):
    """Find the artifact item that a medium's bytes are (ADR-0010).

    `hashed_qid` is the item whose `media SHA-1` is `content_sha1`, if
    any: the bytes are that item. Otherwise an item labelled `label`
    that carries no hash is these bytes only if one of its wiki files
    holds them, or it has no file yet (a registration interrupted before
    its statements); it is adopted and its hash is backfilled by the
    caller. Any other item with that label is different content.

    Returns ``(item, held, label_taken)``: the item or None, its wiki
    files that hold these bytes, and whether `label` names a different
    artifact, so that a new item needs a content-tagged label. The
    caller uploads the bytes only when `held` is empty; `upload_file`
    never overwrites a file, so every `media file` it records holds the
    bytes its `media SHA-1` names.
    """
    if hashed_qid is not None:
        item = client.wbi.item.get(hashed_qid)
        files = _existing_claim_strings(item, media_file)
        return item, _files_holding(client, files, content_sha1), False
    labelled = client.find_item_by_label(label)
    if labelled is None:
        return None, [], False
    candidate = client.wbi.item.get(labelled)
    files = _existing_claim_strings(candidate, media_file)
    held = _files_holding(client, files, content_sha1)
    if not _existing_claim_strings(candidate, media_sha1) and (
        held or not files
    ):
        return candidate, held, False
    return None, [], True


def _takes_alias(
    client: WikiClient, item, name: str, content_sha1: str
) -> bool:
    """Should `name`, another name for an item's bytes, become its alias?

    Name variants live in aliases (MODELING.md), but not a name that
    belongs to other content: another item's label, or the plain form of
    this item's content-tagged label (tagged because that name was
    taken). Aliasing it would cross-link two artifacts.
    """
    current = str(item.labels.get("en") or "")
    aliases = {str(alias) for alias in item.aliases.get("en") or []}
    return (
        name != current
        and name not in aliases
        and current != f"{name} [{content_sha1[:8]}]"
        and client.find_item_by_label(name) in (None, item.id)
    )


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

    # -- locate the artifact item: content hash is the identity key -------
    # Identical bytes are one artifact however they are named: with media
    # present the hash decides (see _locate_by_content), and the label is
    # consulted only to adopt an unhashed item whose file holds these
    # bytes. With no media to hash, the label is the key.
    file_page: str | None = None
    content_sha1: str | None = None
    artifact = None
    label_taken = reused_by_hash = False
    if media_path is not None:
        # the content identity is the file's bytes, independent of the
        # name MediaWiki files them under (ADR-0010)
        content_sha1 = hashlib.sha1(media_path.read_bytes()).hexdigest()
        hashed_qid = client.string_statement_index(media_sha1).get(
            content_sha1
        )
        artifact, held, label_taken = _locate_by_content(
            client,
            hashed_qid,
            title,
            content_sha1,
            media_file=media_file,
            media_sha1=media_sha1,
        )
        reused_by_hash = hashed_qid is not None
        # upload before any item is minted, and only when none of the
        # item's files holds the bytes already
        file_page = held[0] if held else client.upload_file(media_path)
    else:
        existing_qid = client.find_item_by_label(title)
        if existing_qid:
            artifact = client.wbi.item.get(existing_qid)

    if artifact is not None:
        created = False
    else:
        # new content. Labels are unique per kind (the wiki's idempotency
        # convention), so if this title already names DIFFERENT content,
        # disambiguate with a short content tag rather than collide.
        label = f"{title} [{content_sha1[:8]}]" if label_taken else title
        artifact = client.wbi.item.new()
        artifact.labels.set("en", label)
        if description:
            artifact.descriptions.set("en", description)
        artifact = artifact.write(summary="ingest: register artifact")
        created = True
    artifact_qid = artifact.id

    # same bytes under a new filename: keep the new name as an alias
    if reused_by_hash and _takes_alias(client, artifact, title, content_sha1):
        aliases = {str(alias) for alias in (artifact.aliases.get("en") or [])}
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
