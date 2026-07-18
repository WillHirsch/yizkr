"""Writer step: approved ClaimBatch -> Wikibase statements (ADR-0003).

Per batch, the writer:

1. mints an extraction-run item from the batch metadata (idempotent:
   matched by a deterministic label containing the batch content hash),
   with its run details in the label/description — items describe
   themselves; statements stay reserved for source-attributed claims;
2. creates items for unresolved `local` subjects/values (reusing an
   existing item on exact label match, so re-runs never duplicate
   people), writing their `instance of` with the artifact as source;
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
   statement (ADR-0002).

Nothing here auto-approves: the writer expects the output of
`apply_review` and trusts the human decision that produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from yizkr.claims import (
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
    StringValue,
    TimeValue,
    VocabRef,
)
from yizkr.core.wiki import WikiClient


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


def batch_fingerprint(batch: ClaimBatch) -> str:
    canonical = json.dumps(batch.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:10]


class BatchWriter:
    def __init__(self, client: WikiClient, dry_run: bool = False) -> None:
        self.client = client
        self.dry_run = dry_run
        self.properties, self.items = client.vocabulary()
        self._entity_cache: dict[str, object] = {}
        # entity manifest of the batch being written (ADR-0007):
        # local_id -> EntityRecord; set per write_batch call
        self._entity_records: dict[str, EntityRecord] = {}

    # -- reference resolution ---------------------------------------------

    def _resolve_ref(
        self,
        ref: EntityRef,
        result: WriteResult,
        source_artifact_qid: str | None,
    ) -> str:
        if isinstance(ref, QidRef):
            return ref.qid
        if isinstance(ref, VocabRef):
            return self.items[ref.key]
        if isinstance(ref, LocalRef):
            return self._materialize_local(ref, result, source_artifact_qid)
        raise TypeError(f"unknown ref {ref!r}")

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
        if instance_of and source_artifact_qid:
            claim = self._item_claim(
                self.properties["instance_of"], self.items[instance_of]
            )
            claim.references = self._references(
                artifact_qid=source_artifact_qid
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
                artifact_qid=source_artifact_qid
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
    def _append(entity, claim) -> None:
        """Add a claim WITHOUT touching existing ones.

        WikibaseIntegrator's claims.add() defaults to REPLACE_ALL, which
        silently deletes existing statements on the same property — a
        coexistence violation (ADR-0002): a source-attributed statement
        must never be overwritten.
        Every add in the pipeline must go through here.
        """
        from wikibaseintegrator.wbi_enums import ActionIfExists

        entity.claims.add(
            claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE
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
        if subject_qid not in self._entity_cache:
            self._entity_cache[subject_qid] = self.client.wbi.item.get(
                subject_qid
            )
        entity = self._entity_cache[subject_qid]
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

    # -- main entry ----------------------------------------------------------

    def write_batch(self, batch: ClaimBatch) -> WriteResult:
        self._entity_records = {record.id: record for record in batch.entities}
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
            artifact_qid = self._resolve_ref(
                claim.source.artifact, result, source_artifact_qid=None
            )
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
                entity.write(summary="ingest: write reviewed claim")
                self._entity_cache[subject_qid] = self.client.wbi.item.get(
                    subject_qid
                )
            result.written += 1
        return result
