"""Authenticated Wikibase client shared by every writing tool.

One client for the model bootstrap, the pipeline registrar/writer, and
the query publisher. Entity lookup is done by enumerating pages through
the MediaWiki API (lag-free: no dependency on the search index or the
query service), and manifest keys are resolved to this instance's P/Q
ids by exact English label — the project's idempotency convention.

A property label names one property (Wikibase refuses two), and the label
index fails loudly on a duplicate. Item labels need not be unique: two
people of one name are two items under one label (homonyms). The index
gives such a label its numerically earliest item and keeps every holder
(`ItemLabels`), and only a lookup whose label must name one item alone
fails on it (`ItemLabels.sole`): a manifest vocabulary item, found by its
label in `vocabulary` and the bootstrap, and a process item, such as a
run item, whose label is its key (`find_item_by_label`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import requests
import yaml

from yizkr.core.config import USER_AGENT, Config
from yizkr.model.schema import DEFAULT_MANIFEST, read_manifest_data


class ItemLabels(dict[str, str]):
    """The instance's item labels, label -> QID, as `WikiClient.label_index`
    reads them. A label two or more items hold (homonyms, such as two
    people of one name) names the numerically earliest of them, so every
    reader of a label finds the same item; `holders` gives them all, in
    that order. A lookup whose label must name one item alone, a manifest
    vocabulary item's or a run item's, asks `sole`, which fails on a label
    two items hold rather than pick one."""

    def __init__(
        self,
        labels: Mapping[str, str] | Iterable[tuple[str, str]] = (),
        shared: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__(labels)
        # label -> every item that holds it, earliest first, for a label
        # two or more items hold
        self.shared: dict[str, tuple[str, ...]] = dict(shared or {})

    @classmethod
    def of(cls, labels: Mapping[str, str]) -> ItemLabels:
        """`labels` as ItemLabels: itself when it is one, and otherwise a
        mapping in which each label names one item."""
        return labels if isinstance(labels, ItemLabels) else cls(labels)

    def holders(self, label: str) -> tuple[str, ...]:
        """Every item that holds `label`, earliest first; none when no
        item does."""
        if label in self.shared:
            return self.shared[label]
        return (self[label],) if label in self else ()

    def pairs(self) -> Iterator[tuple[str, str]]:
        """(label, QID) for every item that holds a label, each holder of a
        shared label included."""
        for label in self:
            for qid in self.holders(label):
                yield label, qid

    def sole(self, label: str) -> str | None:
        """The one item that holds `label`, or None when no item does.
        Raises when two or more items hold it: the caller finds an item
        by a label that must name one item alone."""
        holders = self.holders(label)
        if len(holders) > 1:
            raise RuntimeError(
                f"{len(holders)} items share the English label {label!r}: "
                f"{', '.join(holders)}. This label must name one item "
                f"alone (a manifest vocabulary item, or a process item "
                f"such as a run item); resolve the duplicate on the wiki."
            )
        return holders[0] if holders else None


def _qid_order(qid: str) -> int:
    return int(qid[1:])


class WikiClient:
    def __init__(
        self, config: Config, manifest_source=DEFAULT_MANIFEST
    ) -> None:
        self.config = config
        self.manifest_source = manifest_source
        self._wbi = None
        self._login = None
        self._vocabulary: tuple[dict[str, str], dict[str, str]] | None = None
        self._labels_read: tuple[dict[str, str], dict[str, str]] | None = None

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
        """All live P/Q entity ids on the instance.

        Redirects (merged items, ADR-0007) are excluded here: a
        redirect's entity data resolves to its target, so counting it
        would produce phantom duplicate labels downstream.
        """
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
                    "apfilterredir": "nonredirects",
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
                    "label": entity.get("labels", {})
                    .get("en", {})
                    .get("value"),
                    "datatype": entity.get("datatype"),
                }
        return entities

    def fetch_claims(self, ids: list[str]) -> dict[str, dict]:
        """id -> its statements, as `wbgetentities` gives them, for the
        entities `ids` alone, 50 a request: what a reader that needs the
        statements of a few entities asks, never the whole instance
        (`iter_entities`). An id the instance does not hold has none."""
        claims: dict[str, dict] = {}
        for start in range(0, len(ids), 50):
            data = self.api_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(ids[start : start + 50]),
                    "props": "claims",
                }
            )
            for entity_id, entity in data.get("entities", {}).items():
                claims[entity_id] = entity.get("claims") or {}
        return claims

    def snapshot(self) -> dict[str, dict]:
        """Everything on the instance: id -> {'label', 'datatype'}."""
        return self.fetch_entities(self.list_entity_ids())

    def label_index(
        self, snapshot: dict[str, dict] | None = None
    ) -> tuple[dict[str, str], ItemLabels]:
        """(property label -> P id, item label -> Q id).

        A property label names one property, and two properties under one
        label fail loudly here. Two or more items may hold one label
        (homonyms): the label names the earliest of them, and the index
        keeps every holder (ItemLabels), so that only a lookup whose label
        must name one item fails on it (`ItemLabels.sole`).
        """
        entities = snapshot if snapshot is not None else self.snapshot()
        properties: dict[str, str] = {}
        holders: dict[str, list[str]] = {}
        for entity_id, info in entities.items():
            label = info["label"]
            if label is None:
                continue
            if not entity_id.startswith("P"):
                holders.setdefault(label, []).append(entity_id)
                continue
            if label in properties:
                raise RuntimeError(
                    f"Two properties share the English label {label!r}: "
                    f"{properties[label]} and {entity_id}. A property is "
                    f"found by its label; resolve the duplicate on the "
                    f"wiki."
                )
            properties[label] = entity_id
        for qids in holders.values():
            qids.sort(key=_qid_order)
        items = ItemLabels(
            {label: qids[0] for label, qids in holders.items()},
            {
                label: tuple(qids)
                for label, qids in holders.items()
                if len(qids) > 1
            },
        )
        if snapshot is None:
            self._labels_read = (properties, items)
        return properties, items

    def vocabulary(self) -> tuple[dict[str, str], dict[str, str]]:
        """(property key -> P id, item key -> Q id) via manifest labels.

        Each manifest item's label must name one item alone
        (`ItemLabels.sole`): a label it shares with another item fails
        here, whatever other labels items share.
        The labels this client last read whole (`label_index`) answer it
        when they hold the whole vocabulary, so a caller that has just
        read them, as resolve does, costs no second read; otherwise the
        labels are read afresh."""
        if self._vocabulary is not None:
            return self._vocabulary
        raw = yaml.safe_load(read_manifest_data(self.manifest_source))
        if self._labels_read is not None:
            found = _vocabulary_of(raw, *self._labels_read)
            if not found[2]:
                self._vocabulary = found[0], found[1]
                return self._vocabulary
        properties, items, missing = _vocabulary_of(raw, *self.label_index())
        if missing:
            raise RuntimeError(
                f"Instance is missing manifest vocabulary: {missing}. "
                f"Run `yizkr bootstrap` against it first."
            )
        self._vocabulary = (properties, items)
        return self._vocabulary

    def forget_vocabulary(self) -> None:
        """Forget the vocabulary and the labels this client has read, so
        that the next `vocabulary` reads the instance afresh."""
        self._vocabulary = None
        self._labels_read = None

    def find_item_by_label(self, label: str) -> str | None:
        """The one item that holds `label`, or None: for a label that must
        name one item alone, as a process item's does. Raises when two or
        more items hold it (`ItemLabels.sole`)."""
        _, item_labels = self.label_index()
        return ItemLabels.of(item_labels).sole(label)

    def iter_entities(self, props: str) -> Iterator[tuple[str, dict]]:
        """(id, data) for every live entity on the instance, as
        `wbgetentities` gives it with `props` and English terms only, 50
        entities a request. Lag-free like `label_index`: it enumerates the
        API, never the search index or query service, so an entity written
        moments earlier is there."""
        ids = self.list_entity_ids()
        for start in range(0, len(ids), 50):
            data = self.api_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(ids[start : start + 50]),
                    "props": props,
                    "languages": "en",
                }
            )
            yield from data.get("entities", {}).items()

    def string_statement_index(self, property_pid: str) -> dict[str, str]:
        """Map {statement string value -> entity id} for one property.

        Lag-free like `label_index` (enumerates the API, never the search
        index or query service), so it is a safe idempotency key even for
        an entity written moments earlier. Used as a content-identity key
        for artifacts: the media SHA-1 that dedups re-uploads of the same
        bytes under different filenames (ADR-0010).

        If two entities carry the same value the numerically-earliest id
        wins deterministically — that is the very duplicate the hash key
        exists to prevent, surfaced for the near-duplicate pass rather
        than fatal here, as an item label two items hold names the
        earliest of them in `label_index`.
        """
        candidates: dict[str, list[str]] = {}
        for entity_id, entity in self.iter_entities("claims"):
            for claim in entity.get("claims", {}).get(property_pid, []):
                value = (claim.get("mainsnak", {}).get("datavalue") or {}).get(
                    "value"
                )
                if isinstance(value, str):
                    candidates.setdefault(value, []).append(entity_id)
        return {
            value: min(matches, key=lambda qid: int(qid[1:]))
            for value, matches in candidates.items()
        }

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

    def merge_items(self, from_qid: str, to_qid: str, summary: str) -> None:
        """Merge one item into another; the source becomes a redirect.

        A curator action (ADR-0007): callers name one explicit pair per
        call — never bulk CSV automation. Wikibase refuses merges with
        conflicting same-language labels, so the source's label is
        first preserved as an alias on the target and then aligned;
        statements (with their references), aliases, and sitelinks move
        to the target. Other items' statements that point at the source
        keep pointing at the redirect, which WDQS follows.
        """
        from wikibaseintegrator import wbi_helpers

        def api(data: dict) -> None:
            wbi_helpers.mediawiki_api_call_helper(
                data={
                    **data,
                    "token": self.login.get_edit_token(),
                    "bot": "1",
                },
                login=self.login,
            )

        source = self.wbi.item.get(from_qid)
        target = self.wbi.item.get(to_qid)
        source_label = str(source.labels.get("en") or "")
        target_label = str(target.labels.get("en") or "")
        if source_label and source_label != target_label:
            # preserve the source's name as an alias on the target, then
            # strip the source's label/description (an empty value in
            # wbsetlabel/wbsetdescription REMOVES the term) so the merge
            # has no term conflicts and genuinely empties the source
            aliases = {
                str(alias) for alias in (target.aliases.get("en") or [])
            }
            if source_label not in aliases:
                target.aliases.set("en", sorted(aliases | {source_label}))
                target.write(
                    summary=f"merge: preserve label of {from_qid} as alias"
                )
            api(
                {
                    "action": "wbsetlabel",
                    "id": from_qid,
                    "language": "en",
                    "value": "",
                    "summary": (
                        f"merge: clear label before merging into {to_qid}"
                    ),
                }
            )
        if str(source.descriptions.get("en") or ""):
            api(
                {
                    "action": "wbsetdescription",
                    "id": from_qid,
                    "language": "en",
                    "value": "",
                    "summary": (
                        f"merge: clear description before merging "
                        f"into {to_qid}"
                    ),
                }
            )
        wbi_helpers.mediawiki_api_call_helper(
            data={
                "action": "wbmergeitems",
                "fromid": from_qid,
                "toid": to_qid,
                "ignoreconflicts": "description",
                "summary": summary,
                "token": self.login.get_edit_token(),
                "bot": "1",
            },
            login=self.login,
        )
        # wbmergeitems moves content but does not redirect the emptied
        # source (the wiki UI issues this as a second call, and so do we)
        wbi_helpers.mediawiki_api_call_helper(
            data={
                "action": "wbcreateredirect",
                "from": from_qid,
                "to": to_qid,
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
        """Store a media file's bytes on the wiki exactly once; returns
        the page title (File:...) of the file that holds them.

        A wiki file is never overwritten and the same bytes are never
        stored twice (ADR-0010), so a `media file` statement always shows
        the bytes its item's `media SHA-1` names. The upload is sent
        without `ignorewarnings` (MediaWiki reads a boolean parameter as
        true whenever it is present, whatever its value), so MediaWiki
        stops at every warning and each one is answered here:

        * the bytes are stored already (``nochange`` under this name,
          ``duplicate`` under others): that file is returned and nothing
          is uploaded;
        * this name holds different bytes (``exists``): the bytes go
          under a content-tagged name, ``"<stem> <sha1[:8]><suffix>"``,
          and the existing file is left as it is;
        * a warning about the name alone (`NAME_WARNINGS`): no file holds
          the name, so the upload is repeated with warnings overridden;
        * any other warning is refused, among them ``duplicate-archive``
          (these bytes were deleted from the wiki): re-uploading them
          would undo a curator's deletion, so it is left to a person.
        """
        name = filename or path.name
        content_sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
        stem, suffix = Path(name).stem, Path(name).suffix
        for attempt in (name, f"{stem} {content_sha1[:8]}{suffix}"):
            upload = self._upload(path, attempt, comment)
            if upload["result"] == "Success":
                return _file_page(upload["filename"])
            warnings = upload["warnings"]
            if "nochange" in warnings:
                return _file_page(warnings["exists"])
            if warnings.get("duplicate"):
                return _file_page(sorted(warnings["duplicate"])[0])
            if "exists" in warnings:
                continue  # different bytes hold this name: tag the name
            refused = set(warnings) - NAME_WARNINGS
            if refused:
                raise RuntimeError(
                    f"Upload of {path.name} as File:{attempt} stopped on "
                    f"MediaWiki warnings {sorted(refused)} ({warnings}); "
                    f"these need a person's decision. `duplicate-archive` "
                    f"means the bytes were deleted from the wiki: restore "
                    f"that file to reuse it."
                )
            upload = self._upload(path, attempt, comment, override=True)
            clash = {"exists", "duplicate"} & set(upload.get("warnings", {}))
            if upload["result"] != "Success" or clash:
                raise RuntimeError(
                    f"Upload of {path.name} as File:{attempt}, name "
                    f"warnings overridden, met existing bytes: {upload}"
                )
            return _file_page(upload["filename"])
        raise RuntimeError(
            f"File:{name} and File:{attempt} both hold other bytes than "
            f"{path.name}; neither is overwritten."
        )

    def _upload(
        self, path: Path, filename: str, comment: str, override: bool = False
    ) -> dict:
        """One action=upload request; returns its ``upload`` result, a
        success or a set of warnings, and raises on anything else.

        `override` sends ``ignorewarnings``, so MediaWiki stores the file
        whatever its warnings; `upload_file` sets it only after a request
        without it has shown that no warning concerns stored bytes.
        """
        fields = {"filename": filename, "comment": comment}
        if override:
            fields["ignorewarnings"] = "1"
        data = self._post_upload(path, fields)
        upload = data.get("upload") or {}
        if upload.get("result") == "Success" or upload.get("warnings"):
            return upload
        raise RuntimeError(f"Upload of {path.name} failed: {data}")

    def _post_upload(self, path: Path, fields: dict) -> dict:
        """POST `fields` and the file's bytes as action=upload; returns
        the decoded response."""
        with path.open("rb") as handle:
            response = self.login.get_session().post(
                self.api_url,
                data={
                    **fields,
                    "action": "upload",
                    "token": self.login.get_edit_token(),
                    "format": "json",
                },
                files={"file": (fields["filename"], handle)},
                headers={"User-Agent": USER_AGENT},
                timeout=300,
            )
        response.raise_for_status()
        return response.json()


# MediaWiki upload warnings about a file's name alone: the name is unusual
# (a camera-style prefix, a thumbnail-like form, characters MediaWiki
# replaces) or close to another file's (another extension, a deleted file,
# a description page with no file). With no `exists` warning no file holds
# the name, and with no `duplicate` no file holds the bytes, so uploading
# despite these overwrites nothing and stores nothing twice.
NAME_WARNINGS = frozenset(
    {
        "badfilename",
        "bad-prefix",
        "exists-normalized",
        "page-exists",
        "thumb",
        "thumb-name",
        "was-deleted",
    }
)


def _file_page(name: str) -> str:
    """A file name as MediaWiki reports it -> its page title."""
    return f"File:{name.replace('_', ' ')}"


def _vocabulary_of(
    raw: dict, property_labels: dict[str, str], item_labels: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(property key -> P id, item key -> Q id, the keys not found) of the
    manifest `raw` under the instance's labels. Raises when a manifest
    item's label is one that two or more items hold (ItemLabels.sole)."""
    properties: dict[str, str] = {}
    items: dict[str, str] = {}
    missing: list[str] = []
    for key, spec in raw["properties"].items():
        pid = property_labels.get(spec["label"])
        if pid is None:
            missing.append(f"property {key!r}")
        else:
            properties[key] = pid
    held = ItemLabels.of(item_labels)
    for key, spec in raw["items"].items():
        qid = held.sole(spec["label"])
        if qid is None:
            missing.append(f"item {key!r}")
        else:
            items[key] = qid
    return properties, items, missing
