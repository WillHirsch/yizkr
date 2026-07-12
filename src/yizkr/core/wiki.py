"""Authenticated Wikibase client shared by every writing tool.

One client for the model bootstrap, the pipeline registrar/writer, and
the query publisher. Entity lookup is done by enumerating pages through
the MediaWiki API (lag-free: no dependency on the search index or the
query service), and manifest keys are resolved to this instance's P/Q
ids by exact English label — the project's idempotency convention.

English labels are unique per entity kind by convention; the label
index fails loudly on a duplicate rather than silently picking one.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import requests
import yaml

from yizkr.core.config import USER_AGENT, Config
from yizkr.model.schema import DEFAULT_MANIFEST, read_manifest_data


class WikiClient:
    def __init__(self, config: Config, manifest_source=DEFAULT_MANIFEST) -> None:
        self.config = config
        self.manifest_source = manifest_source
        self._wbi = None
        self._login = None
        self._vocabulary: tuple[dict[str, str], dict[str, str]] | None = None

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
        _ = self.wbi  # ensure initialized
        return self._login

    # -- entity lookup (lag-free page enumeration) --------------------------

    def list_entity_ids(self) -> list[str]:
        """All P/Q entity ids on the instance."""
        ids: set[str] = set()
        # Properties live in namespace 122; items on Wikibase Suite live in
        # main (0); 120 covers installs with a dedicated Item namespace.
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

    def fetch_entities(self, ids: list[str]) -> dict[str, dict]:
        """id -> {'label': en label or None, 'datatype': for properties}."""
        entities: dict[str, dict] = {}
        for start in range(0, len(ids), 50):
            batch = ids[start : start + 50]
            data = self.api_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": "labels|datatype",
                    "languages": "en",
                }
            )
            for entity_id, entity in data["entities"].items():
                entities[entity_id] = {
                    "label": entity.get("labels", {}).get("en", {}).get("value"),
                    "datatype": entity.get("datatype"),
                }
        return entities

    def snapshot(self) -> dict[str, dict]:
        """Everything on the instance: id -> {'label', 'datatype'}."""
        return self.fetch_entities(self.list_entity_ids())

    def label_index(
        self, snapshot: dict[str, dict] | None = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        """(property label -> P id, item label -> Q id).

        Labels are the idempotency key across the whole system; a
        duplicate label is a modeling defect and fails loudly here.
        """
        entities = snapshot if snapshot is not None else self.snapshot()
        properties: dict[str, str] = {}
        items: dict[str, str] = {}
        for entity_id, info in entities.items():
            label = info["label"]
            if label is None:
                continue
            table = properties if entity_id.startswith("P") else items
            if label in table:
                raise RuntimeError(
                    f"Two entities share the English label {label!r}: "
                    f"{table[label]} and {entity_id}. Labels are the "
                    f"idempotency key; resolve the duplicate on the wiki."
                )
            table[label] = entity_id
        return properties, items

    def vocabulary(self) -> tuple[dict[str, str], dict[str, str]]:
        """(property key -> P id, item key -> Q id) via manifest labels."""
        if self._vocabulary is not None:
            return self._vocabulary
        raw = yaml.safe_load(read_manifest_data(self.manifest_source))
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
                f"Run `yizkr bootstrap` against it first."
            )
        self._vocabulary = (properties, items)
        return self._vocabulary

    def find_item_by_label(self, label: str) -> str | None:
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

    def upload_file(
        self,
        path: Path,
        filename: str | None = None,
        comment: str = "ingest: artifact media upload",
    ) -> str:
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
            existing = self.api_get(
                {
                    "action": "query",
                    "titles": f"File:{filename}",
                    "prop": "imageinfo",
                    "iiprop": "sha1",
                }
            )
            local_sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
            for page in existing["query"]["pages"].values():
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("sha1") == local_sha1:
                    return f"File:{filename.replace('_', ' ')}"
            raise RuntimeError(
                f"File:{filename} already exists with different content; "
                f"choose another filename."
            )
        raise RuntimeError(f"Upload failed: {data}")
