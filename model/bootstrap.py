#!/usr/bin/env python3
"""Idempotent model bootstrap for the family-archive Wikibase.

Reads model/manifest.yaml and ensures every declared property and item
exists in the target Wikibase instance, then records the local id <->
Wikidata id mapping on a wiki page (docs/MODELING.md).

Idempotency: entities are matched to manifest entries by exact English
label (looked up by enumerating the instance's entities through the
MediaWiki API, so there is no dependency on search-index or query-service
lag). Existing entities are updated in place when the manifest's
description changes or gains aliases; matching entities are left alone.
Re-running against an already-bootstrapped instance is a no-op.

Statements: the only statements written are the schema-level `subclass of`
claims declared in the manifest. Each carries a `stated in` reference to
the "model manifest" item (the manifest is the document asserting them),
satisfying CLAUDE.md rule 1. A subclass claim that already exists is
never rewritten.

Configuration is read from the environment, falling back to an env file
(default: deploy/.env): WIKIBASE_PUBLIC_HOST, PUBLIC_SCHEME, HTTP_PORT,
MW_ADMIN_NAME, MW_ADMIN_PASS. Override the API base directly with
WIKIBASE_URL.

Usage:
    python3 bootstrap.py [--manifest manifest.yaml] [--env-file PATH]
                         [--dry-run]

Exit code 0 on success (including no-op runs), 1 on failure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Literal, Optional

import requests
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

USER_AGENT = "yizkr-model-bootstrap/1.0 (model/bootstrap.py)"

# Wikibase datatypes the manifest may use (extend when needed).
Datatype = Literal[
    "wikibase-item",
    "string",
    "time",
    "globe-coordinate",
    "external-id",
    "monolingualtext",
    "url",
    "quantity",
]


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class ManifestProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    datatype: Datatype
    aliases: list[str] = []
    wikidata: Optional[str] = Field(default=None, pattern=r"^P\d+$")


class ManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    aliases: list[str] = []
    wikidata: Optional[str] = Field(default=None, pattern=r"^Q\d+$")
    subclass_of: Optional[str] = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    mapping_page: str = Field(min_length=1)
    schema_source_item: str
    properties: dict[str, ManifestProperty]
    items: dict[str, ManifestItem]

    @model_validator(mode="after")
    def check_cross_references(self) -> "Manifest":
        if self.schema_source_item not in self.items:
            raise ValueError(
                f"schema_source_item {self.schema_source_item!r} is not an item key"
            )
        for key, item in self.items.items():
            if item.subclass_of is not None and item.subclass_of not in self.items:
                raise ValueError(
                    f"items.{key}.subclass_of {item.subclass_of!r} is not an item key"
                )
        if self.items and "subclass_of" not in self.properties:
            for key, item in self.items.items():
                if item.subclass_of is not None:
                    raise ValueError(
                        "manifest declares subclass_of relations but no "
                        "'subclass_of' property"
                    )
        for kind, entries in (("properties", self.properties), ("items", self.items)):
            seen: dict[str, str] = {}
            for key, entry in entries.items():
                if entry.label in seen:
                    raise ValueError(
                        f"duplicate label {entry.label!r} in {kind}: "
                        f"{seen[entry.label]!r} and {key!r} (labels are the "
                        f"idempotency key and must be unique)"
                    )
                seen[entry.label] = key
        return self


def load_manifest(path: Path) -> Manifest:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Manifest.model_validate(raw)


# ---------------------------------------------------------------------------
# Configuration (mirrors deploy/smoke_test.py)
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def build_config(env_file: Path) -> dict[str, str]:
    file_values = read_env_file(env_file)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default

    scheme = get("PUBLIC_SCHEME", "http")
    host = get("WIKIBASE_PUBLIC_HOST")
    port = get("HTTP_PORT", "80")
    port_suffix = "" if port in ("80", "") or scheme == "https" else f":{port}"
    config = {
        "wikibase_url": get("WIKIBASE_URL") or f"{scheme}://{host}{port_suffix}",
        "admin_name": get("MW_ADMIN_NAME"),
        "admin_pass": get("MW_ADMIN_PASS"),
    }
    missing = [k for k, v in config.items() if not v]
    if not host and not get("WIKIBASE_URL"):
        missing.append("WIKIBASE_PUBLIC_HOST")
    if missing:
        sys.exit(
            f"Missing configuration: {missing}. Set the variables in the "
            f"environment or in {env_file}."
        )
    return config


# ---------------------------------------------------------------------------
# Read side: enumerate what already exists (plain API, no search index)
# ---------------------------------------------------------------------------


def api_get(wikibase_url: str, params: dict) -> dict:
    response = requests.get(
        f"{wikibase_url}/w/api.php",
        params={**params, "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"MediaWiki API error: {data['error']}")
    return data


def list_entity_ids(wikibase_url: str) -> list[str]:
    """All P/Q entity ids on the instance, via page listing (lag-free)."""
    ids: set[str] = set()
    # Properties live in their own namespace (122); items on Wikibase Suite
    # live in main (0), but namespace 120 is checked too for installs that
    # use a dedicated Item namespace.
    for namespace in (0, 120, 122):
        continue_token: str | None = None
        while True:
            params = {
                "action": "query",
                "list": "allpages",
                "apnamespace": namespace,
                "aplimit": "500",
            }
            if continue_token:
                params["apcontinue"] = continue_token
            data = api_get(wikibase_url, params)
            for page in data["query"]["allpages"]:
                match = re.fullmatch(
                    r"(?:Property:|Item:)?([PQ]\d+)", page["title"]
                )
                if match:
                    ids.add(match.group(1))
            continue_token = data.get("continue", {}).get("apcontinue")
            if not continue_token:
                break
    return sorted(ids)


def fetch_entities(wikibase_url: str, ids: list[str]) -> dict[str, dict]:
    """id -> {'label': en label or None, 'datatype': for properties}."""
    result: dict[str, dict] = {}
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        data = api_get(
            wikibase_url,
            {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels|datatype",
                "languages": "en",
            },
        )
        for entity_id, entity in data["entities"].items():
            result[entity_id] = {
                "label": entity.get("labels", {}).get("en", {}).get("value"),
                "datatype": entity.get("datatype"),
            }
    return result


def index_by_label(existing: dict[str, dict]) -> tuple[dict, dict]:
    """Return (property label -> id, item label -> id); duplicates are fatal."""
    properties: dict[str, str] = {}
    items: dict[str, str] = {}
    for entity_id, info in existing.items():
        label = info["label"]
        if label is None:
            continue
        table = properties if entity_id.startswith("P") else items
        if label in table:
            sys.exit(
                f"Two entities share the English label {label!r}: "
                f"{table[label]} and {entity_id}. Labels are the idempotency "
                f"key; resolve the duplicate on the wiki, then re-run."
            )
        table[label] = entity_id
    return properties, items


# ---------------------------------------------------------------------------
# Write side (WikibaseIntegrator)
# ---------------------------------------------------------------------------


def make_wbi(config: dict):
    from wikibaseintegrator import WikibaseIntegrator, wbi_login
    from wikibaseintegrator.wbi_config import config as wbi_config

    wbi_config["MEDIAWIKI_API_URL"] = f"{config['wikibase_url']}/w/api.php"
    wbi_config["USER_AGENT"] = USER_AGENT
    login = wbi_login.Clientlogin(
        user=config["admin_name"], password=config["admin_pass"]
    )
    return WikibaseIntegrator(login=login), login


def ensure_entity(wbi, kind: str, spec, existing_id: str | None,
                  existing_info: dict | None, dry_run: bool) -> tuple[str, str]:
    """Create or update one property/item. Returns (entity id, action)."""
    if existing_id is None:
        if dry_run:
            return f"({kind} to create)", "create"
        entity = (
            wbi.property.new(datatype=spec.datatype)
            if kind == "property"
            else wbi.item.new()
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
        wbi.property.get(existing_id)
        if kind == "property"
        else wbi.item.get(existing_id)
    )
    changed = False
    current_description = entity.descriptions.get("en")
    current_description = current_description.value if current_description else None
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


def ensure_subclass_statement(wbi, item_qid: str, target_qid: str,
                              subclass_pid: str, stated_in_pid: str,
                              source_qid: str, dry_run: bool) -> str:
    """Add `subclass of -> target` with a manifest reference, if absent."""
    from wikibaseintegrator.datatypes import Item as ItemClaim
    from wikibaseintegrator.models import Reference, References

    entity = wbi.item.get(item_qid)
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
    entity.claims.add(claim)
    entity.write(summary="model bootstrap: subclass hierarchy")
    return "create"


# ---------------------------------------------------------------------------
# Mapping page
# ---------------------------------------------------------------------------


def build_mapping_wikitext(manifest: Manifest, property_ids: dict[str, str],
                           item_ids: dict[str, str]) -> str:
    """Wikitext for the local id <-> Wikidata id mapping page."""

    def numeric(entity_id: str) -> int:
        return int(entity_id[1:])

    def wikidata_cell(external_id: str | None) -> str:
        if not external_id:
            return "—"
        prefix = "Property:" if external_id.startswith("P") else ""
        return f"[https://www.wikidata.org/wiki/{prefix}{external_id} {external_id}]"

    lines = [
        "''Generated by <code>model/bootstrap.py</code> from "
        "<code>model/manifest.yaml</code> — do not edit by hand; re-run the "
        "bootstrap to refresh. Wikidata equivalents mirror semantics only "
        "(docs/MODELING.md).''",
        "",
        "== Properties ==",
        '{| class="wikitable sortable"',
        "! Local ID !! Label !! Datatype !! Wikidata equivalent",
    ]
    for key, pid in sorted(property_ids.items(), key=lambda kv: numeric(kv[1])):
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


def write_mapping_page(login, title: str, text: str) -> None:
    from wikibaseintegrator import wbi_helpers

    wbi_helpers.mediawiki_api_call_helper(
        data={
            "action": "edit",
            "title": title,
            "text": text,
            "summary": "model bootstrap: refresh id mapping",
            "token": login.get_edit_token(),
            "bot": "1",
        },
        login=login,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).parent / "manifest.yaml",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).parent.parent / "deploy" / ".env",
        help="env file for connection settings (default: deploy/.env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    config = build_config(args.env_file)
    print(f"Wikibase: {config['wikibase_url']}")
    print(f"Manifest: {len(manifest.properties)} properties, "
          f"{len(manifest.items)} items")

    print("Enumerating existing entities ...")
    entity_ids = list_entity_ids(config["wikibase_url"])
    existing = fetch_entities(config["wikibase_url"], entity_ids)
    labels_p, labels_q = index_by_label(existing)

    wbi, login = (None, None)
    if not args.dry_run:
        wbi, login = make_wbi(config)
    else:
        wbi = None

    counts = {"create": 0, "update": 0, "ok": 0}
    property_ids: dict[str, str] = {}
    item_ids: dict[str, str] = {}

    for key, spec in manifest.properties.items():
        existing_id = labels_p.get(spec.label)
        info = existing.get(existing_id) if existing_id else None
        pid, action = ensure_entity(
            wbi, "property", spec, existing_id, info, args.dry_run
        )
        property_ids[key] = pid
        counts[action] += 1
        if action != "ok":
            print(f"  [{action}] property {pid}  {spec.label}")

    for key, spec in manifest.items.items():
        existing_id = labels_q.get(spec.label)
        info = existing.get(existing_id) if existing_id else None
        qid, action = ensure_entity(
            wbi, "item", spec, existing_id, info, args.dry_run
        )
        item_ids[key] = qid
        counts[action] += 1
        if action != "ok":
            print(f"  [{action}] item {qid}  {spec.label}")

    statements = {"create": 0, "ok": 0}
    if args.dry_run and counts["create"]:
        print("  (dry run: skipping statement checks, entities missing)")
    else:
        subclass_pid = property_ids["subclass_of"]
        stated_in_pid = property_ids["stated_in"]
        source_qid = item_ids[manifest.schema_source_item]
        for key, spec in manifest.items.items():
            if spec.subclass_of is None:
                continue
            action = ensure_subclass_statement(
                wbi,
                item_ids[key],
                item_ids[spec.subclass_of],
                subclass_pid,
                stated_in_pid,
                source_qid,
                args.dry_run,
            )
            statements[action] += 1
            if action != "ok":
                print(f"  [claim]  {spec.label} subclass of "
                      f"{manifest.items[spec.subclass_of].label}")

    if not args.dry_run:
        print(f"Writing mapping page {manifest.mapping_page!r} ...")
        write_mapping_page(
            login,
            manifest.mapping_page,
            build_mapping_wikitext(manifest, property_ids, item_ids),
        )

    print(
        f"\nDone{' (dry run)' if args.dry_run else ''}: "
        f"{counts['create']} created, {counts['update']} updated, "
        f"{counts['ok']} already correct; "
        f"{statements['create']} subclass statements added, "
        f"{statements['ok']} already present."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
