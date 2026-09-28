"""Writer step: approved ClaimBatch -> Wikibase statements (ADR-0003).

Before anything else, the writer refuses a batch that holds a `depicts`
claim no curator approved on its own review-sheet row (ADR-0005): each
depicts claim must carry the row approval `apply_review` records on it,
for the claim and the entity-manifest records its local refs resolve
through as they now read, and no other ref in the batch may change the
item one of its unrecorded local refs becomes (`unapproved_depicts` in
yizkr.pipeline.review). The refusal (UnapprovedDepictsError) names
every such claim by its review-sheet id, and comes before `write_batch`
reads from or writes to the instance, in a dry run too. A batch without
depicts claims is not affected.

Per batch, the writer:

0. resolves every artifact the batch cites, by the SHA-1 of its media,
   to the artifact item carrying that `media SHA-1` (ADR-0010). This
   happens before anything is written: a batch that cites media no item
   carries is refused whole, since its statements would have no source
   to reference;
1. mints an extraction-run item from the batch metadata (idempotent:
   matched by a deterministic label containing the batch content hash),
   with its run details in the label/description — items describe
   themselves; statements stay reserved for source-attributed claims;
2. creates items for unresolved `local` subjects/values (reusing an
   existing item on exact label match, so re-runs never duplicate
   people), writing each minted item's `instance of`, and an
   unclassified entity's `type hint`, with two references: stated in ->
   the artifact that mentions the entity, and inferred from extraction
   run -> the run item. An entity's kind is the extraction's proposal,
   a machine write like any claim (ADR-0003);
3. writes each claim as a statement at NORMAL rank (the contract has
   no rank field — ADR-0002) with TWO references (ADR-0003):
   * stated in -> artifact, with page number / region / excerpt
     qualifiers inside the reference;
   * inferred from extraction run -> the run item;
4. when a statement with the same property, value, AND qualifiers
   already exists: if it is not yet referenced to this claim's
   artifact, the claim's references are APPENDED to it (an agreeing
   second source is knowledge — R2 records who asserts a fact, not
   just the fact); if the artifact reference is already present the
   claim is skipped (safe re-run). Same value but different
   qualifiers is a different assertion and coexists as its own
   statement (ADR-0002);
5. after the claims, records the manifest's kind and type hint for
   every entity whose item existed before this batch — found by exact
   label here or by the resolve step, or named by an approved
   equivalence — and that one of the batch's claims mentions. The
   source is the artifact of the first claim that mentions the entity,
   as for a minted item. A kind or type hint is compared with the
   item's unqualified statements on the same property (a type hint as
   free text, ignoring case, Unicode form and spacing):
   * a matching statement already referenced to that artifact: nothing
     to add (safe re-run);
   * a matching machine statement at normal rank: it gains this
     source's two reference blocks, each only if not already there;
   * otherwise (a different value, or a same value held only by curated
     statements): a new statement at normal rank with both references,
     coexisting with the others (ADR-0002).
   A curated statement (one with no extraction-run reference, or whose
   rank a curator changed) is never altered. The claims come first so
   that an explicit `instance of` claim keeps its own locator and
   excerpt.

Nothing here auto-approves: the writer expects the output of
`apply_review` and trusts the human decisions that produced it, except
for depicts, whose per-row approval it checks, as above.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    CoordinateValue,
    EntityRecord,
    EntityRef,
    ExternalIdValue,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    SourceArtifactRef,
    StringValue,
    TimeValue,
    VocabRef,
)
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.review import unapproved_depicts


class UnapprovedDepictsError(ValueError):
    """A batch holds depicts claims that no curator approved on their own
    review-sheet row (ADR-0005)."""

    def __init__(self, claim_ids: list[str]) -> None:
        self.claim_ids = claim_ids
        named = ", ".join(f"claim {cid}" for cid in claim_ids)
        super().__init__(
            f"{len(claim_ids)} depicts claim(s) carry no per-row human "
            f"approval of the item they would depict: {named}. A depicts "
            f"claim is written only after a curator approves it on its own "
            f"row of a review sheet and apply-review records that approval "
            f"(ADR-0005). The record holds while the claim and the entity-"
            f"manifest records its local refs resolve through read as they "
            f"did then, and while no other ref in the batch to one of its "
            f"local ids without a record differs from its own. Export-review "
            f"this batch, decide these rows by hand, apply-review, and write "
            f"the batch apply-review writes. Nothing was written."
        )


class UnregisteredArtifactError(LookupError):
    """A batch cites media that no artifact item on the instance carries."""

    def __init__(self, sha1s: list[str]) -> None:
        self.sha1s = sha1s
        shown = ", ".join(sha1[:12] for sha1 in sha1s)
        super().__init__(
            f"{len(sha1s)} cited artifact(s) not registered on this "
            f"instance: no item carries media SHA-1 {shown}. Register "
            f"the media before writing claims that cite it."
        )


@dataclass
class WriteResult:
    run_qid: str
    written: int = 0
    skipped: int = 0
    # existing same-value statements that gained this batch's source
    # reference (an agreeing second source)
    referenced: int = 0
    created_items: dict[str, str] = field(
        default_factory=dict
    )  # local_id -> QID
    # `instance of` and `type hint` statements of items this batch found
    # rather than minted: new statements written, and existing ones that
    # gained this batch's source (step 5 of the module docstring)
    type_statements_written: int = 0
    type_statements_referenced: int = 0


def batch_fingerprint(batch: ClaimBatch) -> str:
    canonical = json.dumps(batch.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:10]


def normalized_type_hint(text: str) -> str:
    """A type hint's comparison form: free text, so Unicode
    compatibility forms, letter case and runs of whitespace do not
    make two hints different."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class BatchWriter:
    def __init__(
        self,
        client: WikiClient,
        dry_run: bool = False,
        artifact_qids: Mapping[str, str] | None = None,
    ) -> None:
        """`artifact_qids` maps media SHA-1 to artifact QID for artifacts
        the caller has just registered. A cited SHA-1 it does not hold is
        looked up in the instance's `media SHA-1` index."""
        self.client = client
        self.dry_run = dry_run
        self.properties, self.items = client.vocabulary()
        self._entity_cache: dict[str, object] = {}
        # entity manifest of the batch being written (ADR-0007):
        # local_id -> EntityRecord; set per write_batch call
        self._entity_records: dict[str, EntityRecord] = {}
        # media SHA-1 -> artifact QID, for every artifact resolved so far
        self._artifact_qids: dict[str, str] = {
            sha1.lower(): qid for sha1, qid in (artifact_qids or {}).items()
        }
        # the instance's lag-free media SHA-1 index, read on first need
        self._media_sha1_index: dict[str, str] | None = None
        # per write_batch call: item QID -> artifact QID of the first
        # claim that mentions the item, and the items the batch minted
        self._mentions: dict[str, str] = {}
        self._minted: set[str] = set()

    # -- artifact resolution (content-addressed sources) -------------------

    def resolve_artifacts(self, batch: ClaimBatch) -> dict[str, str]:
        """Media SHA-1 -> artifact QID for every artifact `batch` cites.

        Reads only. Raises UnregisteredArtifactError, naming every
        unresolved SHA-1, when any cited media has no artifact item.
        The index is read at most once per call: when a SHA-1 is neither
        supplied by the caller nor in the index read earlier, the index
        is read afresh, so media registered since then still resolves.
        """
        wanted = sorted(
            {
                claim.source.artifact.sha1
                for claim in batch.claims
                if isinstance(claim.source.artifact, ArtifactRef)
            }
        )
        cached = self._media_sha1_index or {}
        if any(
            sha1 not in self._artifact_qids and sha1 not in cached
            for sha1 in wanted
        ):
            self._media_sha1_index = self.client.string_statement_index(
                self.properties["media_sha1"]
            )
        index = self._media_sha1_index or {}
        for sha1 in wanted:
            if sha1 not in self._artifact_qids and sha1 in index:
                self._artifact_qids[sha1] = index[sha1]
        missing = [sha1 for sha1 in wanted if sha1 not in self._artifact_qids]
        if missing:
            raise UnregisteredArtifactError(missing)
        return {sha1: self._artifact_qids[sha1] for sha1 in wanted}

    def _source_artifact_qid(self, ref: SourceArtifactRef) -> str:
        if isinstance(ref, QidRef):
            return ref.qid
        return self._artifact_qids[ref.sha1]

    # -- reference resolution ---------------------------------------------

    def _resolve_ref(
        self,
        ref: EntityRef,
        result: WriteResult,
        source_artifact_qid: str | None,
    ) -> str:
        if isinstance(ref, VocabRef):
            return self.items[ref.key]
        if isinstance(ref, QidRef):
            qid = ref.qid
        elif isinstance(ref, LocalRef):
            qid = self._materialize_local(ref, result, source_artifact_qid)
        else:
            raise TypeError(f"unknown ref {ref!r}")
        if source_artifact_qid:
            # the source an existing item's manifest kind is attributed
            # to, as a minted item's is (step 5 of the module docstring)
            self._mentions.setdefault(qid, source_artifact_qid)
        return qid

    def _materialize_local(
        self,
        ref: LocalRef,
        result: WriteResult,
        source_artifact_qid: str | None,
    ) -> str:
        if ref.local_id in result.created_items:
            return result.created_items[ref.local_id]
        record = self._entity_records.get(ref.local_id)
        # the entity manifest (ADR-0007) is authoritative when present:
        # canonical label, kind, summary, and the document's aliases
        label = record.label if record else ref.label
        description = (record.summary or None) if record else ref.description
        if description and len(description) > 250:
            # MediaWiki hard-caps descriptions at 250 chars; the summary
            # is a convenience, not an assertion — clip at a word break
            description = description[:247].rsplit(" ", 1)[0] + "…"
        instance_of = record.kind if record else ref.instance_of
        aliases = record.aliases if record else []
        if record and record.qid:
            # reviewed equivalence: attach to the existing item and
            # contribute this document's aliases to it
            self._ensure_aliases(record.qid, [record.label, *aliases])
            result.created_items[ref.local_id] = record.qid
            return record.qid
        if not label:
            raise ValueError(
                f"local ref {ref.local_id!r} has no label; cannot create an "
                f"item for it (resolve it to a QID or add a label)"
            )
        existing = self.client.find_item_by_label(label)
        if existing:
            if aliases:
                self._ensure_aliases(existing, aliases)
            result.created_items[ref.local_id] = existing
            return existing
        if self.dry_run:
            result.created_items[ref.local_id] = f"(new:{ref.local_id})"
            return result.created_items[ref.local_id]
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        if description:
            item.descriptions.set("en", description)
        if aliases:
            item.aliases.set("en", aliases)
        item = item.write(summary="ingest: entity proposed by extraction")
        qid = item.id
        self._minted.add(qid)
        # The kind and type hint come from the extraction, never from
        # review, so they carry the run reference even when the claim
        # that mentions the entity is a curated one (ADR-0003).
        if instance_of and source_artifact_qid:
            claim = self._item_claim(
                self.properties["instance_of"], self.items[instance_of]
            )
            claim.references = self._references(
                artifact_qid=source_artifact_qid, run_qid=result.run_qid
            )
            self._append(item, claim)
            item.write(summary="ingest: instance of")
        # open-schema escape hatch (ADR-0013): record what an unclassified
        # entity actually is, so the gap is queryable, not lost
        type_hint = record.type_hint if record else None
        if type_hint and source_artifact_qid:
            from wikibaseintegrator.datatypes import String

            hint = String(
                prop_nr=self.properties["type_hint"], value=type_hint
            )
            hint.references = self._references(
                artifact_qid=source_artifact_qid, run_qid=result.run_qid
            )
            self._append(item, hint)
            item.write(summary="ingest: type hint (unclassified entity)")
        result.created_items[ref.local_id] = qid
        return qid

    def _ensure_aliases(self, qid: str, aliases: list[str]) -> None:
        """Append this document's name variants to an existing item.

        Idempotent and append-only (MODELING.md: name variants live in
        aliases; matching quality compounds per ingested document). The
        item's own label never counts as a missing alias.
        """
        if self.dry_run or not aliases:
            return
        item = self.client.wbi.item.get(qid)
        label = str(item.labels.get("en") or "")
        existing = {str(alias) for alias in (item.aliases.get("en") or [])}
        missing = [
            alias
            for alias in aliases
            if alias and alias != label and alias not in existing
        ]
        if not missing:
            return
        item.aliases.set("en", sorted(existing | set(missing)))
        item.write(summary="ingest: aliases from document entity manifest")

    # -- WBI datavalue construction -----------------------------------------

    @staticmethod
    def _append(entity, claim, beside_equal: bool = False) -> None:
        """Add a claim WITHOUT touching existing ones.

        WikibaseIntegrator's claims.add() defaults to REPLACE_ALL, which
        silently deletes existing statements on the same property — a
        coexistence violation (ADR-0002): a source-attributed statement
        must never be overwritten.
        Every add in the pipeline must go through here.

        `beside_equal` adds the claim as a statement of its own even when
        a statement with the same value and qualifiers exists (a curated
        one the caller must leave as it is): APPEND_OR_REPLACE would
        instead replace that statement's references and rank with the
        new claim's.
        """
        from wikibaseintegrator.wbi_enums import ActionIfExists

        entity.claims.add(
            claim,
            action_if_exists=ActionIfExists.FORCE_APPEND
            if beside_equal
            else ActionIfExists.APPEND_OR_REPLACE,
        )

    @staticmethod
    def _item_claim(pid: str, qid: str):
        from wikibaseintegrator.datatypes import Item

        return Item(prop_nr=pid, value=qid)

    def _to_wbi_claim(
        self,
        prop_key: str,
        value,
        result: WriteResult,
        source_artifact_qid: str | None,
    ):
        from wikibaseintegrator.datatypes import (
            ExternalID,
            GlobeCoordinate,
            Item,
            MonolingualText,
            Quantity,
            String,
            Time,
        )

        pid = self.properties[prop_key]
        if isinstance(value, ItemValue):
            return Item(
                prop_nr=pid,
                value=self._resolve_ref(
                    value.value, result, source_artifact_qid
                ),
            )
        if isinstance(value, StringValue):
            return String(prop_nr=pid, value=value.value)
        if isinstance(value, ExternalIdValue):
            return ExternalID(prop_nr=pid, value=value.value)
        if isinstance(value, MonolingualValue):
            return MonolingualText(
                prop_nr=pid,
                text=value.value.text,
                language=value.value.language,
            )
        if isinstance(value, TimeValue):
            wikibase = value.to_wikibase()
            return Time(
                prop_nr=pid,
                time=wikibase["time"],
                precision=wikibase["precision"],
                calendarmodel=wikibase["calendarmodel"],
            )
        if isinstance(value, CoordinateValue):
            return GlobeCoordinate(
                prop_nr=pid,
                latitude=value.latitude,
                longitude=value.longitude,
                precision=value.precision or 0.0001,
            )
        if isinstance(value, QuantityValue):
            return Quantity(prop_nr=pid, amount=value.to_wikibase()["amount"])
        raise TypeError(f"unhandled value {value!r}")

    def _references(
        self,
        artifact_qid: str,
        run_qid: str | None = None,
        claim: Claim | None = None,
    ):
        from wikibaseintegrator.datatypes import Item, MonolingualText, String
        from wikibaseintegrator.models import Reference, References

        references = References()
        source_ref = Reference()
        source_ref.add(
            Item(prop_nr=self.properties["stated_in"], value=artifact_qid)
        )
        if claim is not None and claim.source.page_number:
            source_ref.add(
                String(
                    prop_nr=self.properties["page_number"],
                    value=claim.source.page_number,
                )
            )
        if claim is not None and claim.source.region:
            source_ref.add(
                String(
                    prop_nr=self.properties["relative_position_within_image"],
                    value=claim.source.region.as_wikibase_string(),
                )
            )
        if claim is not None and claim.source.transcription_excerpt:
            excerpt = claim.source.transcription_excerpt
            # excerpts are verification aids: Wikibase caps text values
            # at 400 chars and forbids newlines in monolingual text —
            # normalize visibly, never fail the write
            text = excerpt.text.replace("\n", " | ")
            if len(text) > 400:
                text = text[:397].rsplit(" ", 1)[0] + "…"
            source_ref.add(
                MonolingualText(
                    prop_nr=self.properties["transcription_excerpt"],
                    text=text,
                    language=excerpt.language,
                )
            )
        references.add(source_ref)
        # dry-run mints no run item; its placeholder cannot be referenced
        if run_qid is not None and re.fullmatch(r"Q[1-9]\d*", run_qid):
            run_ref = Reference()
            run_ref.add(
                Item(
                    prop_nr=self.properties["inferred_from_extraction_run"],
                    value=run_qid,
                )
            )
            references.add(run_ref)
        return references

    # -- extraction-run item -------------------------------------------------

    def ensure_run_item(self, batch: ClaimBatch) -> str:
        run = batch.extraction_run
        fingerprint = batch_fingerprint(batch)
        label = (
            f"extraction run {run.started_at.date().isoformat()} "
            f"{run.model_identifier} {fingerprint}"
        )
        existing = self.client.find_item_by_label(label)
        if existing:
            return existing
        if self.dry_run:
            return "(new:run)"
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set(
            "en",
            f"pipeline {run.pipeline_version}"
            + (
                f", commit {run.software_commit}"
                if run.software_commit
                else ""
            )
            + f", started {run.started_at.isoformat()}",
        )
        item = item.write(summary="ingest: extraction run")
        claim = self._item_claim(
            self.properties["instance_of"], self.items["extraction_run"]
        )
        # The run item is the document of its own execution (same
        # convention as the model manifest, docs/MODELING.md).
        claim.references = self._references(artifact_qid=item.id)
        self._append(item, claim)
        item.write(summary="ingest: run item statements")
        return item.id

    # -- statement existence check -------------------------------------------

    def _entity(self, qid: str):
        """The item as last read or written by this writer."""
        if qid not in self._entity_cache:
            self._entity_cache[qid] = self.client.wbi.item.get(qid)
        return self._entity_cache[qid]

    def _find_matching_statement(self, subject_qid: str, wbi_claim):
        """The existing statement equal to this claim, or None.

        Equal means same property, same mainsnak value, AND the same
        qualifiers: a same-value claim with different qualifiers (a
        marriage date on a spouse statement, a residence date) is a
        DIFFERENT assertion and must coexist as its own statement
        (ADR-0002), never be swallowed by a bare-value match.
        """
        if subject_qid.startswith("(new:"):
            return None
        entity = self._entity(subject_qid)
        prop = wbi_claim.mainsnak.property_number
        new_value = wbi_claim.mainsnak.datavalue.get("value")
        new_qualifiers = self._qualifier_snapshot(wbi_claim)
        for existing in entity.claims.get(prop) or []:
            existing_value = (existing.mainsnak.datavalue or {}).get("value")
            if self._values_equal(existing_value, new_value) and (
                self._qualifier_snapshot(existing) == new_qualifiers
            ):
                return existing
        return None

    @classmethod
    def _qualifier_snapshot(cls, wbi_claim) -> list:
        """Order-independent, hash-independent view of the qualifiers."""
        snapshot = []
        for prop, snaks in wbi_claim.qualifiers.qualifiers.items():
            for snak in snaks:
                value = (snak.datavalue or {}).get("value")
                snapshot.append(
                    (prop, json.dumps(cls._value_key(value), sort_keys=True))
                )
        return sorted(snapshot)

    @staticmethod
    def _value_key(value):
        """The identity-relevant part of a datavalue (drops hashes,
        display-only fields, and unset optionals)."""
        if isinstance(value, dict):
            keys = (
                {"id"}
                if "id" in value
                else {"time", "precision", "calendarmodel"}
                if "time" in value
                else {"latitude", "longitude"}
                if "latitude" in value
                else {"text", "language"}
                if "text" in value
                else {"amount", "unit"}
                if "amount" in value
                else set(value)
            )
            return {key: value.get(key) for key in keys}
        return value

    @classmethod
    def _values_equal(cls, a, b) -> bool:
        if isinstance(a, dict) and isinstance(b, dict):
            return all(a.get(k) == b.get(k) for k in cls._value_key(a))
        return a == b

    def _has_artifact_reference(self, wbi_claim, artifact_qid: str) -> bool:
        """Does any reference block cite `stated in -> artifact_qid`?"""
        stated_in = self.properties["stated_in"]
        for reference in wbi_claim.references.references:
            for snak in reference.snaks.get(stated_in) or []:
                value = (snak.datavalue or {}).get("value") or {}
                if isinstance(value, dict) and value.get("id") == artifact_qid:
                    return True
        return False

    # -- type statements of items the batch did not mint ---------------------

    def _found_entities(
        self, batch: ClaimBatch, result: WriteResult
    ) -> list[tuple[str, EntityRecord]]:
        """(item QID, manifest record) for every record whose item existed
        before this batch and one of its claims mentions.

        A record names its item by an approved equivalence (`qid`), by
        the exact-label match that materialized its local refs here, or,
        once the resolve step has replaced those refs with the item's
        QID, by the item's English label being the record's label: the
        match the resolve step made.
        """
        found: list[tuple[str, EntityRecord]] = []
        by_label: dict[str, list[EntityRecord]] = {}
        for record in batch.entities:
            qid = record.qid or result.created_items.get(record.id)
            if qid is None:
                by_label.setdefault(record.label, []).append(record)
            elif qid not in self._minted:
                found.append((qid, record))
        if by_label:
            for qid in self._mentions:
                if qid in self._minted:
                    continue
                label = str(self._entity(qid).labels.get("en") or "")
                for record in by_label.get(label, []):
                    found.append((qid, record))
        return [(qid, rec) for qid, rec in found if qid in self._mentions]

    def _is_curated(self, statement) -> bool:
        """A human write (no extraction-run reference, ADR-0003) or a
        statement whose rank a curator changed."""
        from wikibaseintegrator.wbi_enums import WikibaseRank

        run = self.properties["inferred_from_extraction_run"]
        machine = any(
            reference.snaks.get(run)
            for reference in statement.references.references
        )
        return not machine or statement.rank != WikibaseRank.NORMAL

    @classmethod
    def _reference_key(cls, reference) -> frozenset:
        """A reference block's snaks, independent of hashes and order."""
        return frozenset(
            (
                prop,
                json.dumps(
                    cls._value_key((snak.datavalue or {}).get("value")),
                    sort_keys=True,
                ),
            )
            for prop, snaks in reference.snaks.snaks.items()
            for snak in snaks
        )

    @staticmethod
    def _type_value(statement):
        """What a type statement says: the item of an `instance of`, the
        normalized text of a `type hint`."""
        value = (statement.mainsnak.datavalue or {}).get("value")
        if isinstance(value, dict):
            return value.get("id")
        if isinstance(value, str):
            return normalized_type_hint(value)
        return None

    def _record_type_statement(
        self, qid: str, claim, artifact_qid: str, result: WriteResult
    ) -> None:
        """Record that `artifact_qid` gives the item `claim`'s kind or
        type hint (step 5 of the module docstring)."""
        entity = self._entity(qid)
        value = self._type_value(claim)
        same = [
            statement
            for statement in entity.claims.get(claim.mainsnak.property_number)
            or []
            if self._type_value(statement) == value
            and not self._qualifier_snapshot(statement)
        ]
        if any(self._has_artifact_reference(s, artifact_qid) for s in same):
            return
        references = self._references(
            artifact_qid=artifact_qid, run_qid=result.run_qid
        )
        target = next((s for s in same if not self._is_curated(s)), None)
        if target is not None:
            present = {
                self._reference_key(reference)
                for reference in target.references.references
            }
            for reference in references.references:
                if self._reference_key(reference) not in present:
                    target.references.add(reference)
            entity.write(
                summary="ingest: additional source for existing type statement"
            )
            result.type_statements_referenced += 1
        else:
            claim.references = references
            self._append(entity, claim, beside_equal=True)
            entity.write(summary="ingest: type statement from another source")
            result.type_statements_written += 1
        self._entity_cache[qid] = self.client.wbi.item.get(qid)

    def _record_found_types(
        self, batch: ClaimBatch, result: WriteResult
    ) -> None:
        from wikibaseintegrator.datatypes import String

        for qid, record in self._found_entities(batch, result):
            artifact_qid = self._mentions[qid]
            self._record_type_statement(
                qid,
                self._item_claim(
                    self.properties["instance_of"], self.items[record.kind]
                ),
                artifact_qid,
                result,
            )
            if record.type_hint:
                self._record_type_statement(
                    qid,
                    String(
                        prop_nr=self.properties["type_hint"],
                        value=record.type_hint,
                    ),
                    artifact_qid,
                    result,
                )

    # -- main entry ----------------------------------------------------------

    def write_batch(
        self, batch: ClaimBatch, bulk_accepted: bool = False
    ) -> WriteResult:
        """Write `batch`. Raises UnapprovedDepictsError, before reading
        from or writing to the instance, when a depicts claim in it lacks
        its row approval (the module docstring), in a dry run too."""
        unapproved = unapproved_depicts(batch)
        if unapproved:
            raise UnapprovedDepictsError(unapproved)
        # a bulk-accepted batch (whole-sheet accept-all, ADR-0023) gets a
        # distinct edit summary so MediaWiki history is the low-scrutiny
        # reappraisal queue — no graph flag, no staleness.
        claim_summary = (
            "ingest: write bulk-accepted claim"
            if bulk_accepted
            else "ingest: write reviewed claim"
        )
        # every cited artifact must resolve before the first write
        self.resolve_artifacts(batch)
        self._entity_records = {record.id: record for record in batch.entities}
        self._mentions = {}
        self._minted = set()
        # reviewed equivalences (ADR-0007): resolve replaced these
        # entities' refs with QIDs, so materialization never sees them —
        # contribute the document's aliases to the existing items here
        if not self.dry_run:
            for record in batch.entities:
                if record.qid:
                    self._ensure_aliases(
                        record.qid, [record.label, *record.aliases]
                    )
        result = WriteResult(run_qid=self.ensure_run_item(batch))
        for claim in batch.claims:
            artifact_qid = self._source_artifact_qid(claim.source.artifact)
            subject_qid = self._resolve_ref(
                claim.subject, result, artifact_qid
            )
            if self.dry_run:
                # planning pass: resolve every ref so created_items is
                # complete, then count — WBI datavalues cannot hold the
                # (new:...) placeholders dry-run materialization returns
                if isinstance(claim.value, ItemValue):
                    self._resolve_ref(claim.value.value, result, artifact_qid)
                for qualifier in claim.qualifiers:
                    if isinstance(qualifier.value, ItemValue):
                        self._resolve_ref(
                            qualifier.value.value, result, artifact_qid
                        )
                result.written += 1
                continue
            wbi_claim = self._to_wbi_claim(
                claim.property, claim.value, result, artifact_qid
            )
            for qualifier in claim.qualifiers:
                qualifier_claim = self._to_wbi_claim(
                    qualifier.property, qualifier.value, result, artifact_qid
                )
                wbi_claim.qualifiers.add(qualifier_claim)
            # curated claims are human writes: artifact reference only,
            # no extraction-run reference (ADR-0003 — process provenance
            # is for machine writes)
            run_qid = None if claim.curated else result.run_qid
            existing = self._find_matching_statement(subject_qid, wbi_claim)
            if existing is not None:
                if self._has_artifact_reference(existing, artifact_qid):
                    result.skipped += 1
                    continue
                # an agreeing second source: the statement stays one
                # statement, and gains this claim's references (R2 —
                # who asserts a fact is knowledge, not just the fact)
                for reference in self._references(
                    artifact_qid=artifact_qid, run_qid=run_qid, claim=claim
                ).references:
                    existing.references.add(reference)
                entity = self._entity_cache[subject_qid]
                entity.write(
                    summary="ingest: additional source for existing statement"
                )
                self._entity_cache[subject_qid] = self.client.wbi.item.get(
                    subject_qid
                )
                result.referenced += 1
                continue
            wbi_claim.references = self._references(
                artifact_qid=artifact_qid, run_qid=run_qid, claim=claim
            )
            if not self.dry_run:
                entity = self._entity_cache.get(
                    subject_qid
                ) or self.client.wbi.item.get(subject_qid)
                self._append(entity, wbi_claim)
                entity.write(summary=claim_summary)
                self._entity_cache[subject_qid] = self.client.wbi.item.get(
                    subject_qid
                )
            result.written += 1
        if not self.dry_run:
            self._record_found_types(batch, result)
        return result
