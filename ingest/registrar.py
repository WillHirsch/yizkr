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
registration"). Rule 1 holds: no statement is written unreferenced.

Idempotent: re-registering the same title reuses the existing item
(matched by exact English label) and skips statements that already
exist; re-uploading identical media is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ingest.wiki import WikiClient


@dataclass
class RegistrationResult:
    artifact_qid: str
    file_page: Optional[str]
    fragment_qids: dict[int, str] = field(default_factory=dict)
    created: bool = True


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
    media_path: Optional[Path],
    artifact_type: str,
    title: str,
    description: Optional[str] = None,
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
    page_number = properties["page_number"]

    file_page: Optional[str] = None
    if media_path is not None:
        file_page = client.upload_file(media_path)

    # -- artifact item (reuse by exact label) -----------------------------
    existing_qid = client.find_item_by_label(title)
    if existing_qid:
        artifact = client.wbi.item.get(existing_qid)
        created = False
    else:
        artifact = client.wbi.item.new()
        artifact.labels.set("en", title)
        if description:
            artifact.descriptions.set("en", description)
        artifact = artifact.write(summary="ingest: register artifact")
        created = True
    artifact_qid = artifact.id

    changed = False
    if items[artifact_type] not in _existing_claim_targets(artifact, instance_of):
        artifact.claims.add(
            _item_claim(instance_of, items[artifact_type], stated_in, artifact_qid)
        )
        changed = True
    if file_page and file_page not in _existing_claim_strings(artifact, media_file):
        artifact.claims.add(
            _string_claim(media_file, file_page, stated_in, artifact_qid)
        )
        changed = True
    if changed:
        artifact.write(summary="ingest: artifact statements")

    # -- page fragments ----------------------------------------------------
    fragment_qids: dict[int, str] = {}
    for page in range(1, pages + 1):
        fragment_qids[page] = _ensure_fragment(
            client, artifact_qid, title, page,
            instance_of=instance_of, part_of=part_of, stated_in=stated_in,
            page_number=page_number, fragment_class=items["artifact_fragment"],
        )

    return RegistrationResult(
        artifact_qid=artifact_qid,
        file_page=file_page,
        fragment_qids=fragment_qids,
        created=created,
    )


def _ensure_fragment(client: WikiClient, artifact_qid: str, title: str,
                     page: int, *, instance_of: str, part_of: str,
                     stated_in: str, page_number: str,
                     fragment_class: str) -> str:
    from wikibaseintegrator.datatypes import String

    label = f"{title}, page {page}"
    existing = client.find_item_by_label(label)
    if existing:
        return existing
    fragment = client.wbi.item.new()
    fragment.labels.set("en", label)
    fragment.descriptions.set("en", f"page {page} of {title}")
    fragment = fragment.write(summary="ingest: register fragment")

    fragment.claims.add(
        _item_claim(instance_of, fragment_class, stated_in, artifact_qid)
    )
    part_claim = _item_claim(part_of, artifact_qid, stated_in, artifact_qid)
    part_claim.qualifiers.add(String(prop_nr=page_number, value=str(page)))
    fragment.claims.add(part_claim)
    fragment.write(summary="ingest: fragment statements")
    return fragment.id
