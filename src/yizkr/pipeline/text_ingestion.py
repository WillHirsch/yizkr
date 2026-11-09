"""A resumable, explicitly reviewed text-source addition to an existing graph.

Document products live in the workspace, consent in ReviewStore, and accepted
identities and graph effects in CreationRegistry. Each mutating phase can be
retried independently; a local checkpoint never substitutes for those shared
authorities. Callers own clients, cancellation scopes and reviewer interaction.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yizkr.core.corpus import read_exclusions
from yizkr.core.instance import require_retained_code
from yizkr.core.mutation_guard import protected_mutation
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import CreationRegistry, Generation
from yizkr.pipeline.assignment_writes import (
    ApplicationResult,
    apply_reviewed_claims,
)
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.claim_review import (
    ClaimSelection,
    PreparedClaimReview,
    prepare_claim_review,
)
from yizkr.pipeline.cohort_additions import (
    CohortApplication,
    PreparedCohort,
    apply_cohort_additions,
    prepare_cohort_additions,
)
from yizkr.pipeline.critique import sheet_notes
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.registrar import Registrar, RegistrationResult
from yizkr.pipeline.review_stages import (
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.source_bindings import unique_source_qids
from yizkr.pipeline.stages import atomic_write_text, content_hash
from yizkr.pipeline.text_products import (
    TextProducts,
    TextSettings,
    load_text_products,
    prepare_text_products,
)
from yizkr.pipeline.text_stages import validate_paths
from yizkr.pipeline.writer import artifact_kinds


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


class IngestionTarget(_Record):
    wikibase_url: str
    generation: Generation


class RegistrationProposal(_Record):
    """Inspectable source metadata and destination, before any graph effect."""

    target: IngestionTarget
    products_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None

    @field_validator("artifact_type")
    @classmethod
    def source_kind(cls, value):
        if value not in artifact_kinds():
            raise ValueError("source registration requires an artifact kind")
        return value

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class _Registration(_Record):
    proposal: RegistrationProposal
    actor: str = Field(min_length=1)
    time: str
    complete: bool = False


class TextIngestion:
    """Add one immutable plain-text source, stopping at each review boundary.

    Reopen with the same workspace, source bytes, review store and deployment.
    Completed products do not require model clients or a response cache.
    Different source bytes/settings need a new workspace. This interface never
    marks an instance retained, resets it, or changes established assignments.
    """

    def __init__(
        self,
        source: Path,
        directory: Path,
        *,
        client: WikiClient,
        review_store: ReviewStore,
        exclusions_file: Path,
    ) -> None:
        self.source = Path(source)
        self.directory = Path(directory)
        self.client = client
        self.store = review_store
        self.exclusions_file = Path(exclusions_file)
        validate_paths(
            inputs=(self.source, self.exclusions_file, review_store.path),
            workspaces=(self.directory,),
        )
        self.registry = CreationRegistry(client)

    @contextmanager
    def _workspace(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self.directory / "lifecycle.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReviewRefused(
                    "text ingestion workspace is in use"
                ) from exc
            check_cancelled()
            yield
        finally:
            os.close(fd)

    def prepare(
        self,
        *,
        settings: TextSettings | None = None,
        extractor_client=None,
        critic_client=None,
    ) -> TextProducts:
        """Read, extract and critique without accessing the graph."""
        with self._workspace():
            return prepare_text_products(
                self.source,
                self.directory / "products",
                exclusions=read_exclusions(self.exclusions_file),
                settings=settings,
                extractor_client=extractor_client,
                critic_client=critic_client,
            )

    def _products(self) -> TextProducts:
        return load_text_products(
            self.source,
            self.directory / "products",
            exclusions=read_exclusions(self.exclusions_file),
        )

    def _target(self) -> IngestionTarget:
        generation = self.registry.generation
        if generation is None:
            raise ReviewRefused(
                "text additions require an established deployment generation"
            )
        return IngestionTarget(
            wikibase_url=self.client.config.wikibase_url,
            generation=generation,
        )

    def _pin_target(self, target: IngestionTarget) -> None:
        if self._target() != target:
            raise ReviewRefused("text ingestion destination changed")
        path = self.directory / "target.json"
        if path.exists():
            if IngestionTarget.model_validate_json(path.read_text()) != target:
                raise ReviewRefused("workspace belongs to another deployment")
        else:
            atomic_write_text(path, target.model_dump_json(indent=2))

    def prepare_registration(
        self, artifact_type: str, title: str, description: str | None = None
    ) -> RegistrationProposal:
        """Describe the exact source registration for explicit confirmation."""
        with self._workspace():
            products = self._products()
            return RegistrationProposal(
                target=self._target(),
                products_sha256=products.digest,
                source_sha1=products.artifact.sha1,
                source_sha256=products.source_sha256,
                artifact_type=artifact_type,
                title=title,
                description=description,
            )

    def _operation(self, kind: str, material) -> str:
        return f"text-{kind}:{content_hash(material)}"

    def register_source(
        self,
        proposal: RegistrationProposal,
        *,
        expected_digest: str,
        actor: str,
    ) -> RegistrationResult:
        """Register confirmed bytes/metadata without approving claims."""
        with self._workspace():
            products = self._products()
            if (
                expected_digest != proposal.digest
                or products.digest != proposal.products_sha256
                or products.artifact.sha1 != proposal.source_sha1
                or products.source_sha256 != proposal.source_sha256
                or not actor.strip()
            ):
                raise ReviewRefused(
                    "registration confirmation is stale or incomplete"
                )
            self._pin_target(proposal.target)
            path = self.directory / "registration.json"
            if path.exists():
                registration = _Registration.model_validate_json(
                    path.read_text()
                )
                if registration.proposal != proposal:
                    raise ReviewRefused(
                        "workspace source registration changed"
                    )
            else:
                registration = _Registration(
                    proposal=proposal,
                    actor=actor,
                    time=datetime.now(UTC).isoformat(),
                )
                atomic_write_text(path, registration.model_dump_json(indent=2))
            with protected_mutation(
                self.client,
                state_path=self.directory / "mutation.capability",
                operation_id=self._operation("register", proposal.digest),
            ) as guard:
                self._pin_target(proposal.target)
                self._products()
                require_retained_code(self.client)
                self._admit_registration(products)
                registrar = Registrar(
                    self.client,
                    exclusions=read_exclusions(self.exclusions_file),
                )
                result = registrar.register_artifact(
                    self.source,
                    proposal.artifact_type,
                    proposal.title,
                    proposal.description,
                    sha1=proposal.source_sha1,
                )
                guard.assert_active()
                self._verify_source(products)
                atomic_write_text(
                    path,
                    registration.model_copy(
                        update={"complete": True}
                    ).model_dump_json(indent=2),
                )
                return result

    def _admit_registration(self, products: TextProducts) -> None:
        """Refuse legacy or conflicting ownership before registrar effects.

        An exact interrupted creation may have a verified binding before its
        hash statement is installed. That receipt permits finishing metadata;
        an existing hash-only item cannot acquire authority by being reused.
        """
        sha1 = products.artifact.sha1
        binding = self.registry.lookup({"kind": "source", "sha1": sha1})
        if binding is not None:
            self.registry.verify_binding(binding)
        properties, _ = self.client.vocabulary()
        owners, active = set(), set()
        for qid, entity in self.client.iter_entities("claims"):
            for statement in entity.get("claims", {}).get(
                properties["media_sha1"], ()
            ):
                value = (
                    statement.get("mainsnak", {}).get("datavalue") or {}
                ).get("value")
                if isinstance(value, str) and value.lower() == sha1:
                    owners.add(qid)
                    if statement.get("rank") != "deprecated":
                        active.add(qid)
        if owners and (
            binding is None or owners != {binding.qid} or active != owners
        ):
            raise ReviewRefused(
                "existing source ownership needs verified migration"
            )

    def _verify_source(self, products: TextProducts) -> None:
        binding = self.registry.lookup(
            {"kind": "source", "sha1": products.artifact.sha1}
        )
        if binding is None:
            raise ReviewRefused("source registration has no verified receipt")
        self.registry.verify_binding(binding)
        properties, _ = self.client.vocabulary()
        sources = unique_source_qids(
            self.client, properties["media_sha1"], (products.artifact.sha1,)
        )
        if sources[products.artifact.sha1] != binding.qid:
            raise ReviewRefused("source ownership differs from registration")

    def _registered(self) -> TextProducts:
        products = self._products()
        path = self.directory / "registration.json"
        if not path.is_file():
            raise ReviewRefused("confirm and register the source first")
        registration = _Registration.model_validate_json(path.read_text())
        if not registration.complete:
            raise ReviewRefused(
                "resume the interrupted source registration first"
            )
        if registration.proposal.products_sha256 != products.digest:
            raise ReviewRefused("registered products changed")
        self._pin_target(registration.proposal.target)
        self._verify_source(products)
        return products

    def _admit_products(self, expected: TextProducts) -> None:
        if self._registered().digest != expected.digest:
            raise ReviewRefused("source products changed during application")

    def _request(self, products, choices=()) -> ReviewRequest:
        request = ReviewRequest(
            sources=(
                ReviewSource(
                    products.artifact.sha1, products.batch, products.reading
                ),
            ),
            proposals=tuple(choices),
            current_targets=tuple(
                entry.binding
                for entry in self.registry.indexed_bindings().values()
            ),
        )
        observed = prepare_review(self.store, replace(request, proposals=()))
        mentions = {
            local: mention.id
            for (_, local), mention in observed.mentions.items()
        }
        path = self.directory / "mentions.json"
        if path.exists():
            if json.loads(path.read_text()) != mentions:
                raise ReviewRefused(
                    "workspace review correspondence changed; "
                    "restore its original review store"
                )
        else:
            atomic_write_text(path, json.dumps(mentions, sort_keys=True))
        return request

    def prepare_identities(
        self, choices: tuple[ReviewProposal, ...] = ()
    ) -> PreparedCohort:
        """Expose candidates/choices; record consent through ReviewStore."""
        with self._workspace():
            products = self._registered()
            return prepare_cohort_additions(
                self.store, self._request(products, choices), self.registry
            )

    def apply_identities(
        self, local_ids: Collection[str]
    ) -> CohortApplication:
        """Apply reviewed identities, at most one new entity per call."""
        with self._workspace():
            products = self._registered()
            keys = tuple(sorted(set(local_ids)))
            if not keys or len(keys) != len(local_ids):
                raise ReviewRefused("choose a nonempty unique identity scope")
            prepared = prepare_cohort_additions(
                self.store, self._request(products), self.registry
            )
            try:
                mentions = tuple(
                    prepared.review.mentions[products.artifact.sha1, key].id
                    for key in keys
                )
            except KeyError as exc:
                raise ReviewRefused(
                    "identity scope is absent from this source"
                ) from exc

            def admit() -> None:
                self._admit_products(products)

            return apply_cohort_additions(
                self.store,
                prepared.request,
                self.registry,
                scope=mentions,
                state_path=self.directory / "mutation.capability",
                operation_id=self._operation("identity", list(mentions)),
                record_dir=self.directory / "records",
                admit=admit,
            )

    def _claims(self, products, indices) -> PreparedClaimReview:
        chosen = tuple(indices)
        notes = sheet_notes(
            products.critique.findings, products.batch, products.batch
        )
        attention = tuple(products.extraction_attention) + tuple(
            products.critique.attention
        )
        selections = tuple(
            ClaimSelection(
                artifact_sha1=products.artifact.sha1,
                index=index,
                warnings=attention
                + ((notes[index],) if index in notes else ()),
            )
            for index in chosen
        )
        return prepare_claim_review(
            self.store,
            self._request(products),
            self.registry,
            selections=selections,
        )

    def prepare_claims(self, indices: Collection[int]) -> PreparedClaimReview:
        """Expose located claims and critique warnings for separate consent."""
        with self._workspace():
            return self._claims(self._registered(), indices)

    def apply_claims(self, indices: Collection[int]) -> ApplicationResult:
        """Resume reviewed source supports through shared intents/receipts."""
        with self._workspace():
            products = self._registered()
            selected = tuple(indices)
            if not selected or len(set(selected)) != len(selected):
                raise ReviewRefused("choose a nonempty unique claim scope")
            with protected_mutation(
                self.client,
                state_path=self.directory / "mutation.capability",
                operation_id=self._operation(
                    "claims", [products.digest, sorted(selected)]
                ),
            ) as guard:
                products = self._registered()
                prepared = self._claims(products, selected)
                result = apply_reviewed_claims(
                    self.store,
                    prepared,
                    self.client,
                    record_dir=self.directory / "records",
                    admit=lambda: self._admit_products(products),
                )
                guard.assert_active()
                return result
