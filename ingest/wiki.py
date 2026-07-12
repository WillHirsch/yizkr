"""Shared Wikibase connection and vocabulary resolution for pipeline steps.

Configuration mirrors deploy/smoke_test.py: values come from the
environment, falling back to an env file (default deploy/.env):
WIKIBASE_PUBLIC_HOST, PUBLIC_SCHEME, HTTP_PORT, MW_ADMIN_NAME,
MW_ADMIN_PASS; WIKIBASE_URL overrides the base URL directly.

Vocabulary: pipeline JSON refers to properties/items by manifest key
(see ingest/claims.py). `WikiClient.vocabulary()` resolves keys to this
instance's P/Q ids by matching English labels, using plain page
enumeration (lag-free, same approach as model/bootstrap.py). A missing
key means model/bootstrap.py has not been run against the instance.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
import yaml

USER_AGENT = "yizkr-ingest/0.1 (ingest pipeline)"
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / "deploy" / ".env"
DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "model" / "manifest.yaml"


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


@dataclass
class WikiConfig:
    wikibase_url: str
    admin_name: str
    admin_pass: str

    @classmethod
    def load(cls, env_file: Path = DEFAULT_ENV_FILE) -> "WikiConfig":
        file_values = read_env_file(env_file)

        def get(key: str, default: str = "") -> str:
            return os.environ.get(key) or file_values.get(key) or default

        scheme = get("PUBLIC_SCHEME", "http")
        host = get("WIKIBASE_PUBLIC_HOST")
        port = get("HTTP_PORT", "80")
        port_suffix = "" if port in ("80", "") or scheme == "https" else f":{port}"
        url = get("WIKIBASE_URL") or (f"{scheme}://{host}{port_suffix}" if host else "")
        config = cls(
            wikibase_url=url,
            admin_name=get("MW_ADMIN_NAME"),
            admin_pass=get("MW_ADMIN_PASS"),
        )
        missing = [
            name
            for name, value in (
                ("WIKIBASE_URL or WIKIBASE_PUBLIC_HOST", config.wikibase_url),
                ("MW_ADMIN_NAME", config.admin_name),
                ("MW_ADMIN_PASS", config.admin_pass),
            )
            if not value
        ]
        if missing:
            sys.exit(
                f"Missing configuration: {missing}. Set the variables in the "
                f"environment or in {env_file}."
            )
        return config


class WikiClient:
    """Thin authenticated client shared by the registrar and the writer."""

    def __init__(self, config: WikiConfig,
                 manifest_path: Path = DEFAULT_MANIFEST) -> None:
        self.config = config
        self.manifest_path = manifest_path
        self._wbi = None
        self._login = None
        self._vocabulary: Optional[tuple[dict[str, str], dict[str, str]]] = None

    # -- plumbing ---------------------------------------------------------

    @property
    def api_url(self) -> str:
        return f"{self.config.wikibase_url}/w/api.php"

    def api_get(self, params: dict) -> dict:
        response = requests.get(
            self.api_url,
            params={**params, "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"MediaWiki API error: {data['error']}")
        return data

    @property
    def wbi(self):
        if self._wbi is None:
            from wikibaseintegrator import WikibaseIntegrator, wbi_login
            from wikibaseintegrator.wbi_config import config as wbi_config

            wbi_config["MEDIAWIKI_API_URL"] = self.api_url
            wbi_config["USER_AGENT"] = USER_AGENT
            self._login = wbi_login.Clientlogin(
                user=self.config.admin_name, password=self.config.admin_pass
            )
            self._wbi = WikibaseIntegrator(login=self._login)
        return self._wbi

    @property
    def login(self):
        self.wbi  # ensure initialized
        return self._login

    # -- entity lookup (lag-free, mirrors model/bootstrap.py) --------------

    def list_entity_ids(self) -> list[str]:
        ids: set[str] = set()
        for namespace in (0, 120, 122):
            continue_token: Optional[str] = None
            while True:
                params = {
                    "action": "query",
                    "list": "allpages",
                    "apnamespace": namespace,
                    "aplimit": "500",
                }
                if continue_token:
                    params["apcontinue"] = continue_token
                data = self.api_get(params)
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

    def fetch_labels(self, ids: list[str]) -> dict[str, Optional[str]]:
        labels: dict[str, Optional[str]] = {}
        for start in range(0, len(ids), 50):
            batch = ids[start : start + 50]
            data = self.api_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": "labels",
                    "languages": "en",
                }
            )
            for entity_id, entity in data["entities"].items():
                labels[entity_id] = (
                    entity.get("labels", {}).get("en", {}).get("value")
                )
        return labels

    def label_index(self) -> tuple[dict[str, str], dict[str, str]]:
        """(property label -> P id, item label -> Q id) for the instance."""
        labels = self.fetch_labels(self.list_entity_ids())
        properties: dict[str, str] = {}
        items: dict[str, str] = {}
        for entity_id, label in labels.items():
            if label is None:
                continue
            (properties if entity_id.startswith("P") else items)[label] = entity_id
        return properties, items

    def vocabulary(self) -> tuple[dict[str, str], dict[str, str]]:
        """(property key -> P id, item key -> Q id) via manifest labels.

        Fails with a pointer to model/bootstrap.py when the instance is
        missing manifest entries.
        """
        if self._vocabulary is not None:
            return self._vocabulary
        with open(self.manifest_path) as f:
            raw = yaml.safe_load(f)
        property_labels, item_labels = self.label_index()
        properties: dict[str, str] = {}
        items: dict[str, str] = {}
        missing: list[str] = []
        for key, spec in raw["properties"].items():
            pid = property_labels.get(spec["label"])
            if pid is None:
                missing.append(f"property {key!r}")
            else:
                properties[key] = pid
        for key, spec in raw["items"].items():
            qid = item_labels.get(spec["label"])
            if qid is None:
                missing.append(f"item {key!r}")
            else:
                items[key] = qid
        if missing:
            raise RuntimeError(
                f"Instance is missing manifest vocabulary: {missing}. "
                f"Run model/bootstrap.py against it first."
            )
        self._vocabulary = (properties, items)
        return self._vocabulary

    def find_item_by_label(self, label: str) -> Optional[str]:
        _, item_labels = self.label_index()
        return item_labels.get(label)

    # -- wiki pages ---------------------------------------------------------

    def edit_page(self, title: str, text: str, summary: str) -> None:
        """Create or fully replace a wiki page."""
        from wikibaseintegrator import wbi_helpers

        wbi_helpers.mediawiki_api_call_helper(
            data={
                "action": "edit",
                "title": title,
                "text": text,
                "summary": summary,
                "token": self.login.get_edit_token(),
                "bot": "1",
            },
            login=self.login,
        )

    # -- media upload -------------------------------------------------------

    def upload_file(self, path: Path, filename: Optional[str] = None,
                    comment: str = "ingest: artifact media upload") -> str:
        """Upload a media file; returns the file page title (File:...).

        Re-uploading identical content is treated as success (idempotent);
        a name collision with different content is an error.
        """
        filename = filename or path.name
        # MediaWiki normalizes titles to an uppercase first letter.
        filename = filename[:1].upper() + filename[1:]
        session = self.login.get_session()
        response = session.post(
            self.api_url,
            data={
                "action": "upload",
                "filename": filename,
                "comment": comment,
                "ignorewarnings": "0",
                "token": self.login.get_edit_token(),
                "format": "json",
            },
            files={"file": (filename, path.open("rb"))},
            headers={"User-Agent": USER_AGENT},
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        error_code = data.get("error", {}).get("code")
        if error_code == "fileexists-no-change":
            # identical content already uploaded under this name: no-op
            return f"File:{filename.replace('_', ' ')}"
        upload = data.get("upload", {})
        if upload.get("result") == "Success":
            return f"File:{upload['filename'].replace('_', ' ')}"
        warnings = upload.get("warnings", {})
        if "exists" in warnings or "duplicate" in warnings:
            duplicates = warnings.get("duplicate")
            if duplicates:
                # identical content already uploaded under another name
                return f"File:{duplicates[0].replace('_', ' ')}"
            # same name: check whether content is identical via SHA1
            existing = self.api_get(
                {
                    "action": "query",
                    "titles": f"File:{filename}",
                    "prop": "imageinfo",
                    "iiprop": "sha1",
                }
            )
            pages = existing["query"]["pages"]
            import hashlib

            local_sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
            for page in pages.values():
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("sha1") == local_sha1:
                    return f"File:{filename.replace('_', ' ')}"
            raise RuntimeError(
                f"File:{filename} already exists with different content; "
                f"choose another filename."
            )
        raise RuntimeError(f"Upload failed: {data}")
