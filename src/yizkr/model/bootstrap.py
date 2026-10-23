"""Idempotent model bootstrap: manifest -> Wikibase entities + mapping page.

Ensures every property and item declared in the manifest exists in the
target instance, matched by exact English label (lag-free page
enumeration via WikiClient), then records the local id <-> Wikidata id
mapping on the wiki page named in the manifest. A manifest entity's
label must name one entity alone, and the bootstrap fails on one that two
items hold (`ItemLabels.sole`); other items may share labels, as two
people of one name do.

Idempotent: re-running against an up-to-date instance is a no-op.
The only statements written are the manifest's `subclass of` claims,
each referenced `stated in -> the model manifest item` (the reference
requirement, ADR-0003, applies to schema statements too), APPENDED so
no existing claim is ever replaced (statements coexist — ADR-0002).

CLI: `yizkr bootstrap [--dry-run] [--manifest PATH]`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator

from yizkr.core.wiki import ItemLabels, WikiClient
from yizkr.model.schema import Manifest


def ensure_entity(
    client: WikiClient,
    kind: str,
    spec,
    existing_id: str | None,
    existing_info: dict | None,
    dry_run: bool,
) -> tuple[str, str]:
    """Create or update one property/item. Returns (entity id, action)."""
    if existing_id is None:
        if dry_run:
            return f"({kind} to create)", "create"
        entity = (
            client.wbi.property.new(datatype=spec.datatype)
            if kind == "property"
            else client.wbi.item.new()
        )
        entity.labels.set("en", spec.label)
        entity.descriptions.set("en", spec.description)
        if spec.aliases:
            entity.aliases.set("en", spec.aliases)
        entity = entity.write(summary="model bootstrap: create")
        return entity.id, "create"

    if kind == "property" and existing_info["datatype"] != spec.datatype:
        sys.exit(
            f"Property {existing_id} ({spec.label!r}) has datatype "
            f"{existing_info['datatype']!r} but the manifest declares "
            f"{spec.datatype!r}. Wikibase cannot change a property's "
            f"datatype; mint a new property instead."
        )

    entity = (
        client.wbi.property.get(existing_id)
        if kind == "property"
        else client.wbi.item.get(existing_id)
    )
    changed = False
    current_description = entity.descriptions.get("en")
    current_description = (
        current_description.value if current_description else None
    )
    if current_description != spec.description:
        entity.descriptions.set("en", spec.description)
        changed = True
    current_aliases = {a.value for a in (entity.aliases.get("en") or [])}
    new_aliases = [a for a in spec.aliases if a not in current_aliases]
    if new_aliases:
        entity.aliases.set("en", new_aliases)  # appends, keeps curator aliases
        changed = True
    if changed and not dry_run:
        entity.write(summary="model bootstrap: update description/aliases")
    return existing_id, ("update" if changed else "ok")


def ensure_subclass_statement(
    client: WikiClient,
    item_qid: str,
    target_qid: str,
    subclass_pid: str,
    stated_in_pid: str,
    source_qid: str,
    dry_run: bool,
) -> str:
    """Add `subclass of -> target` with a manifest reference, if absent."""
    from wikibaseintegrator.datatypes import Item as ItemClaim
    from wikibaseintegrator.models import Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists

    entity = client.wbi.item.get(item_qid)
    for claim in entity.claims.get(subclass_pid) or []:
        value = (claim.mainsnak.datavalue or {}).get("value", {})
        if value.get("id") == target_qid:
            return "ok"
    if dry_run:
        return "create"
    reference = Reference()
    reference.add(ItemClaim(prop_nr=stated_in_pid, value=source_qid))
    references = References()
    references.add(reference)
    claim = ItemClaim(
        prop_nr=subclass_pid, value=target_qid, references=references
    )
    # APPEND, never the REPLACE_ALL default: adding a second superclass
    # must not delete the first (coexistence applies to schema too).
    entity.claims.add(claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
    entity.write(summary="model bootstrap: subclass hierarchy")
    return "create"


def build_mapping_wikitext(
    manifest: Manifest, property_ids: dict[str, str], item_ids: dict[str, str]
) -> str:
    """Wikitext for the local id <-> Wikidata id mapping page."""

    def numeric(entity_id: str) -> int:
        return int(entity_id[1:])

    def wikidata_cell(external_id: str | None) -> str:
        if not external_id:
            return "—"
        prefix = "Property:" if external_id.startswith("P") else ""
        return (
            f"[https://www.wikidata.org/wiki/{prefix}{external_id}"
            f" {external_id}]"
        )

    lines = [
        "''Generated by <code>yizkr bootstrap</code> from "
        "<code>model/manifest.yaml</code> — do not edit by hand; re-run the "
        "bootstrap to refresh. Wikidata equivalents mirror semantics only "
        "(docs/MODELING.md).''",
        "",
        "== Properties ==",
        '{| class="wikitable sortable"',
        "! Local ID !! Label !! Datatype !! Wikidata equivalent",
    ]
    for key, pid in sorted(
        property_ids.items(), key=lambda kv: numeric(kv[1])
    ):
        spec = manifest.properties[key]
        lines += [
            "|-",
            f"| [[Property:{pid}|{pid}]] || {spec.label} || {spec.datatype} "
            f"|| {wikidata_cell(spec.wikidata)}",
        ]
    lines += [
        "|}",
        "",
        "== Classes and vocabulary items ==",
        '{| class="wikitable sortable"',
        "! Local ID !! Label !! Subclass of !! Wikidata equivalent",
    ]
    for key, qid in sorted(item_ids.items(), key=lambda kv: numeric(kv[1])):
        spec = manifest.items[key]
        parent = (
            manifest.items[spec.subclass_of].label if spec.subclass_of else "—"
        )
        lines += [
            "|-",
            f"| [[Item:{qid}|{qid}]] || {spec.label} || {parent} "
            f"|| {wikidata_cell(spec.wikidata)}",
        ]
    lines += ["|}", ""]
    return "\n".join(lines)


# a row of either table of the mapping page: its Local ID cell, the id's
# namespace matching its kind, then its Label cell
_MAPPING_ROW = re.compile(
    r"\| \[\[(?:Property:(?P<pid>P\d+)\|(?P=pid)|Item:(?P<qid>Q\d+)\|(?P=qid))"
    r"\]\] \|\| (?P<label>.*?) \|\| "
)


def mapping_rows(wikitext: str) -> Iterator[tuple[str, str]]:
    """(Local ID, Label) of each row of the mapping page's two tables, as
    `build_mapping_wikitext` writes them, properties and items alike. A
    line in no row's form gives nothing."""
    for line in wikitext.splitlines():
        match = _MAPPING_ROW.match(line)
        if match:
            yield match["pid"] or match["qid"], match["label"]


def bootstrap_instance(
    client: WikiClient, manifest: Manifest, dry_run: bool = False, log=print
) -> dict[str, int]:
    """Apply the manifest; returns counts for reporting and tests."""
    log(
        f"Manifest: {len(manifest.properties)} properties, "
        f"{len(manifest.items)} items"
    )
    log("Enumerating existing entities ...")
    snapshot = client.snapshot()
    labels_p, labels_q = client.label_index(snapshot)
    # a manifest item's label must name one item alone; any other label
    # may be shared, as two people of one name share theirs
    labels_q = ItemLabels.of(labels_q)

    counts = {"create": 0, "update": 0, "ok": 0}
    property_ids: dict[str, str] = {}
    item_ids: dict[str, str] = {}

    for key, spec in manifest.properties.items():
        existing_id = labels_p.get(spec.label)
        info = snapshot.get(existing_id) if existing_id else None
        pid, action = ensure_entity(
            client, "property", spec, existing_id, info, dry_run
        )
        property_ids[key] = pid
        counts[action] += 1
        if action != "ok":
            log(f"  [{action}] property {pid}  {spec.label}")

    for key, spec in manifest.items.items():
        existing_id = labels_q.sole(spec.label)
        info = snapshot.get(existing_id) if existing_id else None
        qid, action = ensure_entity(
            client, "item", spec, existing_id, info, dry_run
        )
        item_ids[key] = qid
        counts[action] += 1
        if action != "ok":
            log(f"  [{action}] item {qid}  {spec.label}")

    statements = {"create": 0, "ok": 0}
    if dry_run and counts["create"]:
        log("  (dry run: skipping statement checks, entities missing)")
    else:
        subclass_pid = property_ids["subclass_of"]
        stated_in_pid = property_ids["stated_in"]
        source_qid = item_ids[manifest.schema_source_item]
        for key, spec in manifest.items.items():
            if spec.subclass_of is None:
                continue
            action = ensure_subclass_statement(
                client,
                item_ids[key],
                item_ids[spec.subclass_of],
                subclass_pid,
                stated_in_pid,
                source_qid,
                dry_run,
            )
            statements[action] += 1
            if action != "ok":
                log(
                    f"  [claim]  {spec.label} subclass of "
                    f"{manifest.items[spec.subclass_of].label}"
                )

    if not dry_run:
        log(f"Writing mapping page {manifest.mapping_page!r} ...")
        client.edit_page(
            manifest.mapping_page,
            build_mapping_wikitext(manifest, property_ids, item_ids),
            summary="model bootstrap: refresh id mapping",
        )

    summary = {
        "created": counts["create"],
        "updated": counts["update"],
        "unchanged": counts["ok"],
        "statements_added": statements["create"],
        "statements_present": statements["ok"],
    }
    log(
        f"\nDone{' (dry run)' if dry_run else ''}: "
        f"{summary['created']} created, {summary['updated']} updated, "
        f"{summary['unchanged']} already correct; "
        f"{summary['statements_added']} subclass statements added, "
        f"{summary['statements_present']} already present."
    )
    return summary
