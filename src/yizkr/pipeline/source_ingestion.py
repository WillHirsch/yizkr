"""A resumable, explicitly reviewed one-source addition to an existing graph.

Document products live in the workspace's text checkpoint or in an explicit
saved document directory, consent in ReviewStore, and accepted identities and
graph effects in CreationRegistry. Each mutating phase can be retried
independently; local products never substitute for those shared authorities.
Callers own clients, cancellation scopes and reviewer interaction.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList, read_exclusions
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
from yizkr.pipeline.critique import Finding, Invalid, sheet_notes
from yizkr.pipeline.document_products import (
    UNSUPPORTED_KINDS,
    ProductsRefused,
    UnsupportedKind,
    load_document_products,
)
from yizkr.pipeline.document_state import document_kind
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
    TextSettings,
    load_text_products,
    prepare_text_products,
)
from yizkr.pipeline.text_stages import validate_paths
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import artifact_kinds


class CritiqueWarnings(Protocol):
    """The critic's advisory output, shown beside the claims it concerns."""

    @property
    def findings(self) -> Sequence[Finding | Invalid]: ...

    @property
    def attention(self) -> Sequence[str]: ...


class ReviewedProducts(Protocol):
    """What registration, identity and claim review use of a source's
    complete products.

    `digest` binds the other members: registration pins it, and a later
    phase refuses products whose digest differs. Extraction attention,
    critique attention and critique findings are the warnings shown with
    each claim for approval.
    """

    @property
    def artifact(self) -> ArtifactRef: ...

    @property
    def source_sha256(self) -> str: ...

    @property
    def digest(self) -> str: ...

    @property
    def reading(self) -> Transcription: ...

    @property
    def batch(self) -> ClaimBatch: ...

    @property
    def extraction_attention(self) -> Sequence[str]: ...

    @property
    def critique(self) -> CritiqueWarnings: ...


class ProductsLoader(Protocol):
    """Admit complete products from `directory` after rechecking the source's
    current bytes and exclusions, refusing with a ValueError otherwise.
    `load_text_products` and `load_document_products` implement this."""

    def __call__(
        self, source: Path, directory: Path, *, exclusions: ExclusionList
    ) -> ReviewedProducts: ...


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


def _checkpoint_source(source: Path) -> None:
    """Refuse a source that the text checkpoint would not read as its own
    text. The kind is the one a run gives the file type: a container or
    GEDCOM file refuses with its reason (UnsupportedKind), and any other
    non-text source, such as an image, enters only as a run's saved views."""
    kind = document_kind(source)
    if kind in UNSUPPORTED_KINDS:
        raise UnsupportedKind(kind)
    if kind != "text":
        raise ProductsRefused(
            "the text checkpoint reads only plain-text sources; give an "
            "image's saved views as document_dir"
        )


class SourceIngestion:
    """Add one immutable source, stopping at each review boundary.

    Products come from one of two places. Without `document_dir`, the
    workspace keeps a text source's checkpoint, which `prepare` makes with
    the shared text stages; any other kind of source refuses. With
    `document_dir`, products are one document's complete saved views, such
    as those a run made for a plain-text source or a standalone image; this
    workflow runs no stage for them.

    Reopen with the same workspace, products, source bytes, review store and
    deployment. Completed products do not require model clients or a
    response cache. Different source bytes/settings need a new workspace.
    This interface never marks an instance retained, resets it, or changes
    established assignments.
    """

    def __init__(
        self,
        source: Path,
        directory: Path,
        *,
        client: WikiClient,
        review_store: ReviewStore,
        exclusions_file: Path,
        document_dir: Path | None = None,
    ) -> None:
        self.source = Path(source)
        self.directory = Path(directory)
        self.client = client
        self.store = review_store
        self.exclusions_file = Path(exclusions_file)
        self.document_dir = (
            None if document_dir is None else Path(document_dir)
        )
        validate_paths(
            inputs=(
                self.source,
                self.exclusions_file,
                review_store.path,
                self.document_dir,
            ),
            workspaces=(self.directory,),
        )
        if self.document_dir is None:
            _checkpoint_source(self.source)
        self._load: ProductsLoader = (
            load_text_products
            if self.document_dir is None
            else load_document_products
        )
        self._products_dir = self.document_dir or self.directory / "products"
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
                raise ReviewRefused("ingestion workspace is in use") from exc
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
    ) -> ReviewedProducts:
        """Make or admit complete products without accessing the graph.

        The text checkpoint reads, extracts and critiques what is not yet
        complete. Saved document products are admitted as saved: a setting
        or client for a stage refuses, since this workflow runs none.
        """
        with self._workspace():
            if self.document_dir is None:
                return prepare_text_products(
                    self.source,
                    self._products_dir,
                    exclusions=read_exclusions(self.exclusions_file),
                    settings=settings,
                    extractor_client=extractor_client,
                    critic_client=critic_client,
                )
            if any(
                value is not None
                for value in (settings, extractor_client, critic_client)
            ):
                raise ReviewRefused(
                    "saved document products run no stage here; "
                    "a run makes them"
                )
            return self._products()

    def _products(self) -> ReviewedProducts:
        return self._load(
            self.source,
            self._products_dir,
            exclusions=read_exclusions(self.exclusions_file),
        )

    def _target(self) -> IngestionTarget:
        generation = self.registry.generation
        if generation is None:
            raise ReviewRefused(
                "source additions require an established deployment generation"
            )
        return IngestionTarget(
            wikibase_url=self.client.config.wikibase_url,
            generation=generation,
        )

    def _pin_target(self, target: IngestionTarget) -> None:
        if self._target() != target:
            raise ReviewRefused("ingestion destination changed")
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
        # A stable guard operation ID, persisted while protection is held;
        # its prefix names the operation, not the source's kind.
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

    def _admit_registration(self, products: ReviewedProducts) -> None:
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

    def _verify_source(self, products: ReviewedProducts) -> None:
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

    def _registered(self) -> ReviewedProducts:
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

    def _admit_products(self, expected: ReviewedProducts) -> None:
        if self._registered().digest != expected.digest:
            raise ReviewRefused("source products changed during application")

    def _request(
        self, products: ReviewedProducts, choices=()
    ) -> ReviewRequest:
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
            request = self._request(products)
            # Resolve only the selected durable mention IDs here. The shared
            # application captures current graph context inside its guard.
            prepared = prepare_review(self.store, request)
            try:
                mentions = tuple(
                    prepared.mentions[products.artifact.sha1, key].id
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
                request,
                self.registry,
                scope=mentions,
                state_path=self.directory / "mutation.capability",
                operation_id=self._operation("identity", list(mentions)),
                record_dir=self.directory / "records",
                admit=admit,
            )

    def _claims(
        self, products: ReviewedProducts, indices
    ) -> PreparedClaimReview:
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
