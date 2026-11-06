"""Reviewed historical support crosses the real assignment/write boundary.

The readings and claims are authored from repository fiction. Identity and
historical approval are recorded separately; the actual server supplies every
statement GUID, reference hash and durable application receipt.
"""

from __future__ import annotations

import copy
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock

import pytest
from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.models import Claim as WbiClaim
from wikibaseintegrator.models import Reference, References
from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

from tests.fixtures import example_universe as u
from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    ExtractionRunMetadata,
    ItemValue,
    LocalRef,
    MonolingualText,
    MonolingualValue,
    Qualifier,
    SourceReference,
)
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import (
    CreationRegistry,
    EntityBinding,
    write_existing,
)
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    ClaimSelection,
    prepare_claim_review,
    record_claim_decisions,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.transcribe import TranscribedPage, Transcription

pytestmark = pytest.mark.live
WHEN = "2026-01-01T12:00:00Z"
HANG_GUARD = 120
NAMES = {
    "nicholas": "Nicholas Lobig",
    "gustav": "Gustav Löbig",
    "maisie": "Maisie Cutmore",
}


@dataclass(frozen=True)
class Fixture:
    database: Path
    request: ReviewRequest
    bindings: dict[str, EntityBinding]
    selections: tuple[ClaimSelection, ...]
    directory: Path


def _local(key):
    return LocalRef(local_id=key)


def _item(key):
    return ItemValue(value=_local(key))


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "info|labels|descriptions|aliases|claims",
        }
    )["entities"][qid]


def _snapshot(client):
    from yizkr.core.write_recovery import PREFIX

    ids = client.list_entity_ids()
    entities = {}
    for start in range(0, len(ids), 50):
        entities.update(
            client.api_get(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(ids[start : start + 50]),
                    "props": "info|labels|descriptions|aliases|claims",
                }
            )["entities"]
        )
    titles = client.page_titles(PREFIX)
    records = {}
    # Raw revision/content equality proves no effects without revalidating
    # each immutable record separately. Long registry titles keep GET batches
    # smaller than entity batches to fit the server's request-line limit.
    for start in range(0, len(titles), 20):
        selected = titles[start : start + 20]
        pages = client.api_get(
            {
                "action": "query",
                "prop": "revisions",
                "titles": "|".join(selected),
                "rvprop": "ids|content",
                "rvslots": "main",
                "formatversion": "2",
            }
        )["query"]["pages"]
        assert len(pages) == len(selected)
        for page in pages:
            assert not any(
                k in page for k in ("missing", "invalid", "redirect")
            )
            (revision,) = page["revisions"]
            assert revision["parentid"] == 0
            assert page["title"] not in records
            records[page["title"]] = (
                revision["revid"],
                revision["slots"]["main"]["content"],
            )
    assert records.keys() == set(titles)
    return entities, records


def _fixture(client, directory, suffix):
    directory.mkdir()
    registry = CreationRegistry(client)
    bindings = {}
    for key, name in NAMES.items():
        item = client.wbi.item.new()
        item.labels.set("en", f"Assignment writes {suffix}: {name}")
        bindings[key] = registry.create(
            {"kind": "fictional-assignment-write", "case": suffix, "id": key},
            item,
            summary="fictional assignment-write target",
        )
    sources, proposals, selections = [], [], []
    for source_key in ("interview-2002", "family-tree-1984"):
        original = u.by_key()[source_key]
        text = (
            original.title
            + "\n"
            + original.text
            + f"\nFictional assignment-write fixture: {suffix}.\n"
        )
        path = directory / f"{source_key}.txt"
        path.write_text(text)
        artifact = ArtifactRef.from_path(path)
        registered = register_artifact(
            client,
            path,
            "artifact",
            f"Assignment writes {suffix}: {original.title}",
            pages=1,
        )
        assert registered.created
        interview = source_key == "interview-2002"
        labels = (
            {"nicholas": "Nicholas Lobig", "gustav": "Gus", "maisie": "Maisie"}
            if interview
            else {"nicholas": "Nicholas", "gustav": "Gustav"}
        )
        entities = []
        for key, label in labels.items():
            lines = [
                n
                for n, line in enumerate(text.splitlines(), 1)
                if label in line
            ]
            assert len(lines) == 1
            entities.append(
                EntityRecord(
                    id=key, label=label, kind="human", mention_lines=lines
                )
            )
            proposals.append(
                ReviewProposal(
                    operation="assign",
                    mentions=(
                        MentionKey(artifact_sha1=artifact.sha1, local_id=key),
                    ),
                    target=bindings[key],
                    origin={"method": "human-proposal"},
                )
            )

        def source(quote, *, text=text, artifact=artifact):
            assert text.count(quote) == 1
            return SourceReference(
                artifact=artifact,
                page_number="1",
                transcription_excerpt=MonolingualText(
                    text=quote, language="en"
                ),
            )

        claims = [
            Claim(
                subject=_local("nicholas"),
                property="father",
                value=_item("gustav"),
                source=source(
                    "Dad — Gus, everyone called him"
                    if interview
                    else "Children of Gustl and Florence: Nicholas 1950."
                ),
            )
        ]
        if interview:
            claims.append(
                Claim(
                    subject=_local("maisie"),
                    property="referred_to_as",
                    value=MonolingualValue(
                        value=MonolingualText(
                            text="Nana Maisie", language="en"
                        )
                    ),
                    qualifiers=[
                        Qualifier(
                            property="perspective", value=_item("nicholas")
                        )
                    ],
                    source=source("Nana Maisie next door"),
                )
            )
        batch = ClaimBatch(
            extraction_run=ExtractionRunMetadata(
                model_identifier="authored-fictional-claims",
                pipeline_version="fixture",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            entities=entities,
            claims=claims,
        )
        reading = Transcription(
            artifact=artifact,
            text=text,
            language="en",
            engine="authored-fictional-reading",
            pages=[
                TranscribedPage(
                    page=1,
                    start=0,
                    end=len(text),
                    status="transcribed",
                    provenance={
                        "method": "text-layer",
                        "engine": "authored-fictional-text",
                    },
                )
            ],
        )
        sources.append(ReviewSource(artifact.sha1, batch, reading))
        selections.extend(
            ClaimSelection(artifact_sha1=artifact.sha1, index=n)
            for n in range(len(claims))
        )
    request = ReviewRequest(
        sources=tuple(sources),
        proposals=tuple(proposals),
        current_targets=tuple(bindings.values()),
    )
    database = directory / "review.sqlite"
    with ReviewStore(database) as store:
        prepared = prepare_review(store, request)
        assert all(
            app.status == "needs_review"
            for app in prepared.projection.applications
        ), [app.reasons for app in prepared.projection.applications]
        assert all(
            mention.observation.anchor is not None
            and not mention.observation.limitations
            for mention in prepared.mentions.values()
        )
        for proposal in prepared.proposals:
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="fictional identity curator",
                time=WHEN,
            )
        accepted = accept_reviewed_assignments(store, request, registry)
        assert len(accepted.snapshot.assignments) >= len(proposals)
        prepared = prepare_claim_review(
            store, request, registry, selections=tuple(selections)
        )
        assert all(claim.decision is None for claim in prepared.claims)
        record_claim_decisions(
            store,
            prepared,
            [
                ClaimDecisionInput(
                    proposal_id=claim.proposal.id,
                    expected_digest=claim.proposal.digest,
                    outcome="approve",
                    actor="fictional historical-claim curator",
                    time=WHEN,
                )
                for claim in prepared.claims
            ],
        )
    return Fixture(database, request, bindings, tuple(selections), directory)


def _prepare(client, fixture, store, selections=None):
    prepared = prepare_claim_review(
        store,
        fixture.request,
        CreationRegistry(client),
        selections=selections or fixture.selections,
    )
    assert all(
        claim.decision is not None and claim.decision.outcome == "approve"
        for claim in prepared.claims
    )
    return prepared


def _without_snak_hashes(reference):
    """Keep reference identity/content; WBI omits inner snak hashes."""
    result = copy.deepcopy(reference)
    for snaks in result["snaks"].values():
        for snak in snaks:
            snak.pop("hash", None)
    return result


def _assert_receipts(client, result):
    registry = CreationRegistry(client)
    snapshot = registry.assignment_snapshot()
    properties, _ = client.vocabulary()
    plan = registry.application_plan(result.plan_id)
    assert plan is not None
    roles = set()
    for receipt in result.receipts:
        assert (
            registry.application_receipt(result.plan_id, receipt.claim_id)
            == receipt
        )
        intent = registry.application_intent(result.plan_id, receipt.claim_id)
        assert intent is not None
        planned = next(
            c for c in plan.claims if c.claim_id == receipt.claim_id
        )
        roles.update(r["role"].split(":", 1)[0] for r in planned.roles)
        entity = _entity(client, receipt.qid)
        assert entity["lastrevid"] >= receipt.revision
        statements = [
            s
            for group in entity["claims"].values()
            for s in group
            if s["id"] == receipt.statement_guid
        ]
        (statement,) = statements
        references = {
            r["hash"]: _without_snak_hashes(r) for r in statement["references"]
        }
        assert references[receipt.primary_hash] == _without_snak_hashes(
            receipt.primary
        )
        if receipt.circumstance_hash is not None:
            assert references[
                receipt.circumstance_hash
            ] == _without_snak_hashes(receipt.circumstance)
        assert all(
            references[reference["hash"]] == _without_snak_hashes(reference)
            for reference in receipt.observed_process
        )
        for role in planned.roles:
            assignment = snapshot.assignments[role["mention_id"]]
            assert role["assignment"] == assignment.model_dump(mode="json")
            binding = registry.binding(assignment.entity_id)
            assert role["binding"] == binding.model_dump(mode="json")
            qid = binding.binding.qid
            if role["role"] == "subject":
                assert receipt.qid == qid
            elif role["role"] == "object":
                assert statement["mainsnak"]["datavalue"]["value"]["id"] == qid
            else:
                _, property_key, _ = role["role"].split(":")
                assert any(
                    snak["datavalue"]["value"]["id"] == qid
                    for snak in statement["qualifiers"][
                        properties[property_key]
                    ]
                )
    return roles


def test_reviewed_writes_keep_exact_shared_support_and_assignment_roles(
    fictional_client, tmp_path
):
    from yizkr.pipeline.assignment_writes import apply_reviewed_claims

    client = fictional_client
    fixture = _fixture(client, tmp_path / "shared", "shared-support")
    properties, _ = client.vocabulary()
    nicholas = fixture.bindings["nicholas"].qid
    with ReviewStore(fixture.database) as store:
        first = _prepare(client, fixture, store, fixture.selections[:2])
        original_local_refs = copy.deepcopy(fixture.request)
        first_result = apply_reviewed_claims(
            store, first, client, record_dir=fixture.directory / "records"
        )
        assert len(first_result.receipts) == 2
        assert _assert_receipts(client, first_result) == {
            "subject",
            "object",
            "qualifier",
        }
        item = client.wbi.item.get(nicholas)
        (father,) = item.claims.get(properties["father"])
        father.rank = WikibaseRank.PREFERRED
        item.labels.set("en", "Curator's assignment-write Nicholas Lobig")
        item.descriptions.set("en", "fictional preserved curator description")
        write_existing(item, summary="fictional curator label and rank")
        before = _entity(client, nicholas)
        (old_father,) = before["claims"][properties["father"]]
        later = _prepare(client, fixture, store, fixture.selections[2:])
        second_result = apply_reviewed_claims(
            store, later, client, record_dir=fixture.directory / "records"
        )
        assert len(second_result.receipts) == 1
        assert _assert_receipts(client, second_result) == {"subject", "object"}
        after = _entity(client, nicholas)
        (father,) = after["claims"][properties["father"]]
        assert father["id"] == old_father["id"]
        assert father["rank"] == "preferred"
        assert after["labels"] == before["labels"]
        assert after["descriptions"] == before["descriptions"]
        references = {r["hash"]: r for r in father["references"]}
        assert all(
            references[r["hash"]] == r for r in old_father["references"]
        )
        father_receipts = [
            r
            for r in (*first_result.receipts, *second_result.receipts)
            if r.qid == nicholas
        ]
        assert len({r.statement_guid for r in father_receipts}) == 1
        assert len({r.primary_hash for r in father_receipts}) == 2
        frozen = _snapshot(client)
        for selection, expected in (
            (fixture.selections[:2], first_result),
            (fixture.selections[2:], second_result),
        ):
            again = apply_reviewed_claims(
                store,
                _prepare(client, fixture, store, selection),
                client,
                record_dir=fixture.directory / "records",
            )
            assert again == expected
        assert _snapshot(client) == frozen
        assert fixture.request == original_local_refs

        # A genuinely new confirmation has no old application receipt. It
        # records the support already present without adding a current run's
        # bare reference or rewriting the curator's ranked statement.
        confirmation = _prepare(client, fixture, store, fixture.selections[:1])
        (claim,) = confirmation.claims
        record_claim_decisions(
            store,
            confirmation,
            [
                ClaimDecisionInput(
                    proposal_id=claim.proposal.id,
                    expected_digest=claim.proposal.digest,
                    expected_decision_revision=claim.decision.revision,
                    outcome="approve",
                    actor="second fictional historical-claim curator",
                    time="2026-01-02T12:00:00Z",
                )
            ],
        )
        target_before = _entity(client, nicholas)
        confirmed = apply_reviewed_claims(
            store,
            _prepare(client, fixture, store, fixture.selections[:1]),
            client,
            record_dir=fixture.directory / "records",
        )
        assert confirmed.plan_id != first_result.plan_id
        (confirmed_support,) = confirmed.receipts
        original_support = next(
            r for r in first_result.receipts if r.qid == nicholas
        )
        assert (
            confirmed_support.statement_guid == original_support.statement_guid
        )
        assert confirmed_support.primary_hash == original_support.primary_hash
        assert _entity(client, nicholas) == target_before
        assert _assert_receipts(client, confirmed) == {"subject", "object"}


def test_partial_writes_recover_and_ambiguous_support_refuses(
    fictional_client, tmp_path, monkeypatch
):
    from yizkr.pipeline import assigned_support
    from yizkr.pipeline.assignment_writes import (
        ApplicationInterrupted,
        apply_reviewed_claims,
    )

    client = fictional_client
    fixture = _fixture(client, tmp_path / "partial", "partial-support")
    registry = CreationRegistry(client)
    original_write = assigned_support.write_existing
    original_receipt = CreationRegistry.record_application_receipt
    written = []
    attempted_receipts = []

    def counted_write(item, **kwargs):
        result = original_write(item, **kwargs)
        written.append(item.id)
        return result

    def interrupted_receipt(registry, receipt):
        attempted_receipts.append(receipt)
        if len(attempted_receipts) == 2:
            raise OSError("fictional interrupted support receipt publication")
        return original_receipt(registry, receipt)

    with ReviewStore(fixture.database) as store:
        prepared = _prepare(client, fixture, store)
        with monkeypatch.context() as patch:
            patch.setattr(assigned_support, "write_existing", counted_write)
            patch.setattr(
                CreationRegistry,
                "record_application_receipt",
                interrupted_receipt,
            )
            with pytest.raises(ApplicationInterrupted) as stopped:
                apply_reviewed_claims(
                    store,
                    prepared,
                    client,
                    record_dir=fixture.directory / "records",
                )
        partial = stopped.value
        assert len(partial.receipts) == 1
        plan = registry.application_plan(partial.plan_id)
        assert plan is not None and len(plan.claims) == 3
        assert (
            sum(
                registry.application_intent(partial.plan_id, c.claim_id)
                is not None
                for c in plan.claims
            )
            == 2
        )
        observed = {
            key: _entity(client, binding.qid)["claims"]
            for key, binding in fixture.bindings.items()
        }
        with monkeypatch.context() as patch:
            patch.setattr(assigned_support, "write_existing", counted_write)
            resumed = apply_reviewed_claims(
                store,
                _prepare(client, fixture, store),
                client,
                record_dir=fixture.directory / "records",
            )
        assert len(written) == 3, "completed or uncertain support was resent"
        assert resumed.plan_id == partial.plan_id
        assert len(resumed.receipts) == 3
        assert all(r in resumed.receipts for r in partial.receipts)
        assert _assert_receipts(client, resumed) == {
            "subject",
            "object",
            "qualifier",
        }
        for key, old_claims in observed.items():
            current = _entity(client, fixture.bindings[key].qid)["claims"]
            current_by_id = {s["id"]: s for ss in current.values() for s in ss}
            for statement in (s for ss in old_claims.values() for s in ss):
                preserved = current_by_id[statement["id"]]
                assert preserved["rank"] == statement["rank"]
                refs = {r["hash"]: r for r in preserved.get("references", [])}
                assert all(
                    refs[r["hash"]] == r
                    for r in statement.get("references", [])
                )

    # Both unsupported cases start from current approval without a historical
    # application. The fixture creates problematic graph evidence explicitly.
    for variant in ("ambiguous", "curator"):
        fixture = _fixture(client, tmp_path / variant, variant)
        properties, _ = client.vocabulary()
        target = client.wbi.item.get(fixture.bindings["nicholas"].qid)
        father = Item(
            prop_nr=properties["father"], value=fixture.bindings["gustav"].qid
        )
        if variant == "curator":
            source_qid = (
                CreationRegistry(client)
                .lookup(
                    {
                        "kind": "source",
                        "sha1": fixture.selections[0].artifact_sha1,
                    }
                )
                .qid
            )
            reference = Reference()
            reference.add(
                Item(prop_nr=properties["stated_in"], value=source_qid)
            )
            father.references = References().add(reference)
            father.rank = WikibaseRank.DEPRECATED
        target.claims.add(father, action_if_exists=ActionIfExists.FORCE_APPEND)
        if variant == "ambiguous":
            duplicate = WbiClaim().from_json({"id": "", **father.get_json()})
            duplicate.id = None
            target.claims.add(
                duplicate, action_if_exists=ActionIfExists.FORCE_APPEND
            )
        write_existing(target, summary="fictional unsupported graph support")
        before = _snapshot(client)
        with ReviewStore(fixture.database) as store:
            prepared = _prepare(client, fixture, store, fixture.selections[:1])
            with pytest.raises(
                ValueError, match="ambiguous|exact requested support"
            ):
                apply_reviewed_claims(
                    store,
                    prepared,
                    client,
                    record_dir=fixture.directory / "records",
                )
        assert _snapshot(client) == before


def test_competing_reviewed_plans_dispatch_one_historical_write(
    fictional_client, tmp_path, monkeypatch
):
    from yizkr.pipeline import assigned_support
    from yizkr.pipeline.assignment_writes import (
        ApplicationInterrupted,
        apply_reviewed_claims,
    )

    client = fictional_client
    fixture = _fixture(client, tmp_path / "concurrent", "concurrent-support")
    copied_database = fixture.directory / "second-review.sqlite"
    shutil.copyfile(fixture.database, copied_database)
    competitor = replace(fixture, database=copied_database)
    selection = fixture.selections[:1]
    with ReviewStore(competitor.database) as store:
        prepared = _prepare(client, competitor, store, selection)
        (claim,) = prepared.claims
        record_claim_decisions(
            store,
            prepared,
            [
                ClaimDecisionInput(
                    proposal_id=claim.proposal.id,
                    expected_digest=claim.proposal.digest,
                    expected_decision_revision=claim.decision.revision,
                    outcome="approve",
                    actor="second fictional historical-claim curator",
                    time="2026-01-02T12:00:00Z",
                )
            ],
        )

    barrier, lock = Barrier(2), Lock()
    attempts, writes = [], []
    original_reserve = CreationRegistry.reserve_support_dispatch
    original_write = assigned_support.write_existing

    def race(registry, intent):
        with lock:
            attempts.append(
                (intent.plan_id, intent.support["qid"], intent.base_revision)
            )
        barrier.wait(HANG_GUARD)
        return original_reserve(registry, intent)

    def counted_write(item, **kwargs):
        with lock:
            writes.append(item.id)
        return original_write(item, **kwargs)

    def apply(current):
        independent = WikiClient(client.config)
        with ReviewStore(current.database) as store:
            try:
                return apply_reviewed_claims(
                    store,
                    _prepare(independent, current, store, selection),
                    independent,
                    record_dir=current.directory / "records",
                )
            except ApplicationInterrupted as exc:
                return exc

    with monkeypatch.context() as patch:
        patch.setattr(CreationRegistry, "reserve_support_dispatch", race)
        patch.setattr(assigned_support, "write_existing", counted_write)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(apply, f) for f in (fixture, competitor)]
            try:
                outcomes = [f.result(HANG_GUARD) for f in futures]
            finally:
                barrier.abort()
        assert len(attempts) == 2
        assert len({plan for plan, _, _ in attempts}) == 2
        assert len({(qid, revision) for _, qid, revision in attempts}) == 1
        assert len(writes) == 1
        assert any(not isinstance(r, ApplicationInterrupted) for r in outcomes)
        # A losing request may already have seen the winner's exact effect.
        # Otherwise its retry must recover that effect without another send.
        patch.setattr(
            CreationRegistry, "reserve_support_dispatch", original_reserve
        )
        complete = [apply(f) for f in (fixture, competitor)]
        assert all(not isinstance(r, ApplicationInterrupted) for r in complete)
        assert len(writes) == 1
    for result in complete:
        assert _assert_receipts(client, result) == {"subject", "object"}
    receipts = [result.receipts[0] for result in complete]
    assert len({r.statement_guid for r in receipts}) == 1
    assert len({r.primary_hash for r in receipts}) == 1
    assert all(len(result.receipts) == 1 for result in complete)
    qid = fixture.bindings["nicholas"].qid
    properties, _ = client.vocabulary()
    (father,) = _entity(client, qid)["claims"][properties["father"]]
    assert father["id"] == receipts[0].statement_guid
    assert (
        sum(
            ref["hash"] == receipts[0].primary_hash
            for ref in father["references"]
        )
        == 1
    )
