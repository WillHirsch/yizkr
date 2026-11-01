"""Server-owned item creation intents and verified instance-local bindings.

Immutable MediaWiki pages reserve a generation, identity map, operation and
single dispatch. The item creation revision carries the intent marker in its
summary. A lost response can therefore be recovered without repeating a
``new=item`` request. A dispatched intent with no visible creation remains
ambiguous: absence never proves that an in-flight request cannot still commit.

These records travel with the database. Restoring the complete database also
restores its generation; detecting an out-of-band restore requires an explicit
operational generation change, not a host-local shadow registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.pipeline.cancellation import check_cancelled

PREFIX = "Project:Yizkr write recovery/"
GENERATION_PAGE = PREFIX + "generation"
_HEX = r"^[0-9a-f]{32}$"
_HASH = r"^[0-9a-f]{64}$"


class CreationRefused(RuntimeError):
    """Server authority does not permit this creation or binding."""


class AmbiguousCreation(CreationRefused):
    """A dispatched creation must be reconciled, never blindly retried."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


class Generation(_Record):
    kind: Literal["generation"] = "generation"
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    instance_uri: str


class _Identity(_Record):
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    entity_id: str = Field(pattern=_HEX)
    intent_key: str = Field(pattern=_HASH)


class _Intent(_Identity):
    kind: Literal["intent"] = "intent"
    payload_hash: str = Field(pattern=_HASH)


class _Dispatch(_Identity):
    kind: Literal["dispatch"] = "dispatch"
    attempt: str = Field(pattern=_HEX)


class EntityBinding(_Identity):
    kind: Literal["receipt"] = "receipt"
    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    revision: int = Field(gt=0)
    created: bool = Field(default=False, exclude=True)


class _Map(_Record):
    kind: Literal["identity-map"] = "identity-map"
    deployment_id: str = Field(pattern=_HEX)
    generation: str = Field(pattern=_HEX)
    map_hash: str = Field(pattern=_HASH)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _dump(record: _Record) -> str:
    return _json(record.model_dump(mode="json"))


def _parse(cls, page):
    try:
        return cls.model_validate_json(page.text)
    except (ValueError, ValidationError) as exc:
        raise CreationRefused("invalid server creation record") from exc


def _identity(intent: _Identity) -> dict:
    return {
        key: getattr(intent, key)
        for key in ("deployment_id", "generation", "entity_id", "intent_key")
    }


def _marker(intent: _Identity) -> str:
    return f"[yizkr-create:{intent.intent_key}:{intent.entity_id}]"


def write_existing(item, *, summary: str):
    """Submit one guarded update; a conflict or uncertain result stops.

    The library's default retry loop is unsafe for a response lost after a
    successful write. A caller must reread and reconcile before another update.
    """
    revision = item.lastrevid
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
    ):
        raise CreationRefused("existing item has no verified base revision")
    check_cancelled()
    return item.write(
        summary=summary, baserevid=revision, max_retries=1, retry_after=0
    )


class CreationRegistry:
    """One server authority shared by independent clients and hosts.

    Construction and lookup are read-only, including recovery from a visible
    creation revision. Explicit ensure, claim, create and reconcile operations
    publish records.
    An absent generation may initialize only on an empty or vocabulary-only
    graph; existing ingestion requires a separate verified migration.
    """

    def __init__(self, client):
        self.client = client
        self._observed: Generation | None = None

    @property
    def generation(self) -> Generation | None:
        page = self.client.immutable_page(GENERATION_PAGE)
        if page is None:
            if self._observed is not None or self.client.page_titles(PREFIX):
                raise CreationRefused(
                    "deployment generation is missing "
                    "but recovery state exists"
                )
            return None
        generation = _parse(Generation, page)
        if generation.instance_uri != self.client.config.concept_uri:
            raise CreationRefused(
                "deployment generation belongs to another instance"
            )
        if self._observed is not None and generation != self._observed:
            raise CreationRefused(
                "deployment generation changed during this operation"
            )
        self._observed = generation
        return generation

    def check_generation(self) -> Generation | None:
        """Check admission without publishing a generation or other effects."""
        generation = self.generation
        if generation is not None:
            return generation
        ids = set(self.client.list_entity_ids())
        if ids:
            properties, items = self.client.vocabulary()
            if ids - set(properties.values()) - set(items.values()):
                raise CreationRefused(
                    "existing ingested entities have no deployment "
                    "generation; "
                    "verified migration required"
                )
        return None

    def ensure_generation(self) -> Generation:
        """Establish admitted deployment authority before any mutation."""
        generation = self.check_generation()
        if generation is not None:
            return generation
        proposed = Generation(
            deployment_id=uuid.uuid4().hex,
            generation=uuid.uuid4().hex,
            instance_uri=self.client.config.concept_uri,
        )
        check_cancelled()
        self.client.create_page_once(
            GENERATION_PAGE,
            _dump(proposed),
            "ingest: establish deployment generation",
        )
        generation = self.generation
        if generation is None:
            raise CreationRefused("deployment generation was not established")
        return generation

    def claim_identity_map(self, map_hash: str) -> None:
        if not re.fullmatch(_HASH, map_hash):
            raise ValueError("identity-map hash must be SHA-256")
        generation = self.ensure_generation()
        record = _Map(
            deployment_id=generation.deployment_id,
            generation=generation.generation,
            map_hash=map_hash,
        )
        title = PREFIX + generation.generation + "/identity-map"
        page = self.client.immutable_page(title)
        if page is None:
            check_cancelled()
            page = self.client.create_page_once(
                title, _dump(record), "ingest: bind deployment identity map"
            )
        if _parse(_Map, page) != record:
            raise CreationRefused(
                "deployment is bound to another identity map"
            )
        self._assert_generation(generation)

    def check_identity_map(self, map_hash: str) -> None:
        """Check the map reservation without creating a generation or claim."""
        if not re.fullmatch(_HASH, map_hash):
            raise ValueError("identity-map hash must be SHA-256")
        generation = self.generation
        if generation is None:
            return
        title = PREFIX + generation.generation + "/identity-map"
        page = self.client.immutable_page(title)
        if page is not None:
            expected = _Map(
                deployment_id=generation.deployment_id,
                generation=generation.generation,
                map_hash=map_hash,
            )
            if _parse(_Map, page) != expected:
                raise CreationRefused(
                    "deployment is bound to another identity map"
                )
        self._assert_generation(generation)

    def _assert_generation(self, generation: Generation) -> None:
        if self.generation != generation:
            raise CreationRefused("deployment generation changed")

    def _key(self, generation: Generation, operation: Mapping | str) -> str:
        if not isinstance(operation, (Mapping, str)) or not operation:
            raise ValueError(
                "creation operation must be a nonempty stable locator"
            )
        value = (
            dict(operation) if isinstance(operation, Mapping) else operation
        )
        return hashlib.sha256(
            _json(
                {
                    "generation": generation.model_dump(mode="json"),
                    "operation": value,
                }
            ).encode()
        ).hexdigest()

    def _title(self, generation: Generation, key: str, suffix: str) -> str:
        return PREFIX + generation.generation + "/" + key + "/" + suffix

    def _intent(self, generation: Generation, key: str) -> _Intent | None:
        page = self.client.immutable_page(
            self._title(generation, key, "intent")
        )
        if page is None:
            return None
        intent = _parse(_Intent, page)
        if (
            intent.intent_key != key
            or intent.generation != generation.generation
            or intent.deployment_id != generation.deployment_id
        ):
            raise CreationRefused(
                "creation intent belongs to another operation or deployment"
            )
        return intent

    def _binding(
        self, generation: Generation, intent: _Intent
    ) -> EntityBinding | None:
        dispatch_page = self.client.immutable_page(
            self._title(generation, intent.intent_key, "dispatch")
        )
        receipt_page = self.client.immutable_page(
            self._title(generation, intent.intent_key, "receipt")
        )
        if dispatch_page is None:
            if receipt_page is not None:
                raise CreationRefused(
                    "creation receipt has no dispatch authority"
                )
            return None
        if _identity(_parse(_Dispatch, dispatch_page)) != _identity(intent):
            raise CreationRefused("dispatch does not match its intent")
        if receipt_page is not None:
            binding = _parse(EntityBinding, receipt_page)
            if _identity(binding) != _identity(intent):
                raise CreationRefused("receipt does not match its intent")
        else:
            matches = self.client.creation_revisions(_marker(intent))
            if len(matches) != 1:
                raise AmbiguousCreation(
                    f"dispatched creation has {len(matches)} visible initial "
                    "revisions; reconciliation required"
                )
            binding = EntityBinding(
                **_identity(intent),
                qid=matches[0].qid,
                revision=matches[0].revision,
            )
        self.client.verify_creation(
            binding.qid, binding.revision, _marker(intent)
        )
        self._assert_generation(generation)
        return binding

    def lookup(self, operation: Mapping | str) -> EntityBinding | None:
        generation = self.generation
        if generation is None:
            return None
        intent = self._intent(generation, self._key(generation, operation))
        return self._binding(generation, intent) if intent else None

    def reconcile(self, operation: Mapping | str) -> EntityBinding | None:
        """Persist a recovered receipt without authorizing a new item."""
        generation = self.generation
        if generation is None:
            return None
        intent = self._intent(generation, self._key(generation, operation))
        if intent is None:
            return None
        binding = self._binding(generation, intent)
        if binding is None:
            return None
        return self._receipt(generation, intent, binding)

    def _receipt(
        self, generation: Generation, intent: _Intent, binding: EntityBinding
    ) -> EntityBinding:
        self.client.verify_creation(
            binding.qid, binding.revision, _marker(intent)
        )
        self._assert_generation(generation)
        title = self._title(generation, intent.intent_key, "receipt")
        page = self.client.immutable_page(title)
        if page is None:
            check_cancelled()
            page = self.client.create_page_once(
                title, _dump(binding), "ingest: record verified item binding"
            )
        if _parse(EntityBinding, page) != binding:
            raise CreationRefused("competing creation receipt disagrees")
        self._assert_generation(generation)
        return binding

    def create(
        self, operation: Mapping | str, item, *, summary: str
    ) -> EntityBinding:
        generation = self.ensure_generation()
        key = self._key(generation, operation)
        intent = self._intent(generation, key)
        if intent is not None:
            binding = self._binding(generation, intent)
            if binding is not None:
                return self._receipt(generation, intent, binding)
        if item.id:
            raise CreationRefused("creation requires a new unsaved item")
        payload_hash = hashlib.sha256(
            _json(item.get_json()).encode()
        ).hexdigest()
        if intent is None:
            proposed = _Intent(
                deployment_id=generation.deployment_id,
                generation=generation.generation,
                entity_id=uuid.uuid4().hex,
                intent_key=key,
                payload_hash=payload_hash,
            )
            check_cancelled()
            self.client.create_page_once(
                self._title(generation, key, "intent"),
                _dump(proposed),
                "ingest: reserve stable entity identity",
            )
            intent = self._intent(generation, key)
            if intent is None:
                raise CreationRefused("creation intent was not established")
            binding = self._binding(generation, intent)
            if binding is not None:
                return self._receipt(generation, intent, binding)
        if intent.payload_hash != payload_hash:
            raise CreationRefused(
                "uncompleted intent has different initial item data"
            )
        dispatch = _Dispatch(**_identity(intent), attempt=uuid.uuid4().hex)
        self._assert_generation(generation)
        check_cancelled()
        page = self.client.create_page_once(
            self._title(generation, key, "dispatch"),
            _dump(dispatch),
            "ingest: authorize one item creation attempt",
        )
        stored = _parse(_Dispatch, page)
        if stored != dispatch:
            binding = self._binding(generation, intent)
            if binding is None:
                raise AmbiguousCreation("another dispatch owns this creation")
            return self._receipt(generation, intent, binding)
        self._assert_generation(generation)
        check_cancelled()
        try:
            item.write(
                summary=f"{_marker(intent)} {summary}",
                max_retries=1,
                retry_after=0,
            )
        except Exception as exc:
            try:
                binding = self._binding(generation, intent)
            except (CreationRefused, RecoveryEvidenceError) as recovery:
                raise recovery from exc
            if binding is None:
                raise AmbiguousCreation(
                    "item creation result is unknown"
                ) from exc
            return self._receipt(generation, intent, binding)
        binding = EntityBinding(
            **_identity(intent), qid=item.id, revision=item.lastrevid
        )
        return self._receipt(generation, intent, binding).model_copy(
            update={"created": True}
        )
