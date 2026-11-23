"""Explicit cohort origins stay distinct from inferred identity methods, and
an accepted mention's reviewed names become its entity's aliases."""

import csv
import io
import json
import unicodedata
from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String
from wikibaseintegrator.wbi_enums import ActionIfExists
from wikibaseintegrator.wbi_exceptions import MWApiError

from tests.unit.test_identity_assignments import delta
from tests.unit.test_identity_provenance import Wiki, values
from tests.unit.test_review_stages import request
from tests.unit.test_run_statements import ITEMS, PROPERTIES
from yizkr.core.provenance import ProcessRecord, RecordRef, StageProvenance
from yizkr.core.reviewed_creation import creation_review_evidence
from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.core.write_recovery import CreationRefused, CreationRegistry
from yizkr.pipeline import identity_provenance
from yizkr.pipeline.assignment_aliases import alias_records
from yizkr.pipeline.cohort_provenance import publish_cohort_assignment
from yizkr.pipeline.correction_planning import (
    CorrectionRequest,
    plan_correction,
)
from yizkr.pipeline.identity_provenance import IdentityProvenanceRefused
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import (
    ReviewProposal,
    export_review,
    prepare_review,
)
from yizkr.pipeline.run_provenance import read_record, record_digest
from yizkr.pipeline.writer import BatchWriter


def fixture(
    tmp_path,
    operation="assign",
    *,
    producer=False,
    origin_update=None,
    registered=True,
    inherited=False,
    aliases=(),
):
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    registry.ensure_generation()
    item = wiki.wbi.item.new()
    item.labels.set("en", "Leopold Löbig")
    item.descriptions.set("en", "fictional reviewed identity")
    binding = None
    if operation == "assign":
        binding = registry.create(
            {"kind": "fictional existing person"}, item, summary="fixture"
        )
        registry.index_binding(binding)
    initial = request()
    record = initial.sources[0].batch.entities[0]
    record.aliases = list(aliases)
    key = initial.proposals[0].mentions[0]
    origin = {
        "method": (
            "reviewed_assignment"
            if operation == "assign"
            else "reviewed_creation"
        ),
        "cohort_context_sha256": "c" * 64,
        "process": "deterministic",
    }
    original = None
    if producer:
        original = ProcessRecord(
            kind="recorded-cohort-matching",
            metadata={"producer_run_id": "fictional-recorded-matching"},
            stages=(
                StageProvenance(
                    stage="candidate-generation", process="deterministic"
                ),
            ),
        )
        origin["provenance"] = original.model_dump(mode="json")
    origin.update(origin_update or {})
    evidence = {"cohort_candidates": {"fictional_graph_sha256": "d" * 64}}
    if operation == "new_entity":
        evidence["reviewed_creation"] = creation_review_evidence(
            item.get_json()
        )
    proposals = (
        ReviewProposal(
            operation=operation,
            mentions=(key,),
            target=binding,
            origin=origin,
            evidence=evidence,
        ),
    )
    if inherited:
        other = initial.proposals[1].mentions[0]
        proposals += (ReviewProposal(operation="same", mentions=(key, other)),)
    initial = replace(
        initial,
        proposals=proposals,
        current_targets=(binding,) if binding else (),
    )
    authorization = None
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, initial)
        sheet = export_review(store, prepared)
        decisions = [
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="fictional curator",
                time="2026-01-01T12:00:00Z",
            )
            for proposal in prepared.proposals
        ]
        mention = prepared.mentions[key.key]
        if operation == "new_entity":
            authorization = registry.authorize_reviewed_creation(
                decisions[0], mention, initial_payload=item.get_json()
            )
            binding = registry.create(
                authorization.operation,
                item,
                summary="fictional reviewed creation",
            )
            registry.index_binding(binding)
        if inherited:
            mention = prepared.mentions[other.key]
        snapshot = registry.append_assignments(
            delta(
                list(prepared.mentions.values()),
                decisions,
                [(mention, binding)],
            ),
            expected_head=None,
        )
    if registered:
        artifact = wiki.wbi.item.new()
        artifact.labels.set("en", "Fictional family names")
        artifact.claims.add(
            String(prop_nr=PROPERTIES["media_sha1"], value=key.artifact_sha1)
        )
        artifact.write()
    return SimpleNamespace(
        wiki=wiki,
        registry=registry,
        binding=binding,
        mention=mention,
        decision=decisions[0],
        assignment=snapshot.assignments[mention.id],
        authorization=authorization,
        original=original,
        directory=tmp_path / "records",
        record=record,
        sheet=sheet,
    )


def publish(case):
    return publish_cohort_assignment(
        case.wiki, case.mention.id, record_dir=case.directory
    )


def anchor(case):
    (statement,) = case.wiki.wbi.item.get(case.binding.qid).claims.get(
        PROPERTIES["identity_record"]
    )
    return statement


@pytest.mark.parametrize("operation", ["assign", "new_entity"])
@pytest.mark.parametrize("producer", [False, True])
def test_direct_human_origin_is_repeatable_without_historical_kind(
    tmp_path, operation, producer
):
    case = fixture(tmp_path, operation, producer=producer)
    before = case.wiki.wbi.item.get(case.binding.qid).get_json()
    result = publish(case)
    assert result.changed and result.record_qid != case.binding.qid
    after = case.wiki.wbi.item.get(case.binding.qid).get_json()
    assert after["labels"] == before["labels"]
    assert after["descriptions"] == before["descriptions"]
    assert set(after["claims"]) == {PROPERTIES["identity_record"]}
    origin = next(
        r for r in anchor(case).references if values(r, "identity_method")
    )
    confirmation = next(
        r
        for r in anchor(case).references
        if values(r, "identity_confirmation_actor")
    )
    method = (
        "identity_reviewed_assignment"
        if operation == "assign"
        else "identity_reviewed_creation"
    )
    assert values(origin, "identity_method")[0]["id"] == ITEMS[method]
    assert not values(origin, "identity_confirmation_actor")
    assert not values(confirmation, "identity_method")
    assert values(confirmation, "identity_confirmation_actor") == [
        "fictional curator"
    ]
    assert values(confirmation, "identity_decision_revision") == [
        f"{case.decision.id}:{case.decision.revision}"
    ]
    (digest,) = values(origin, "run_record_sha256")
    record = read_record(case.directory, RecordRef(sha256=digest))
    retained = record.metadata["identity_cohort"]
    assert retained["assignment"] == case.assignment.model_dump(mode="json")
    assert retained["mention"] == case.mention.model_dump(mode="json")
    assert retained["confirmation"] == case.decision.model_dump(mode="json")
    assert retained["original_origin"] == case.decision.proposal.origin
    assert retained["creation_authorization"] == (
        case.authorization.model_dump(mode="json")
        if case.authorization
        else None
    )
    if producer:
        assert values(origin, "identity_origin_record_sha256") == [
            record_digest(case.original)
        ]
        assert record.stages[:-1] == case.original.stages
        assert (
            read_record(case.directory, record.producer_refs[0])
            == case.original
        )
    else:
        assert not values(origin, "identity_origin_record_sha256")
        assert not values(origin, "identity_origin_run")
        assert record.producer_refs == ()
        assert any("unavailable" in limit for limit in record.limitations)
        assert all(
            s.model_identifier is None and not s.prompt_hashes
            for s in record.stages
        )
    assert record.stages[-1].process == "manual"
    writes = case.wiki.wbi.item.writes
    assert not publish(case).changed
    assert case.wiki.wbi.item.writes == writes
    assert (
        case.registry.assignment_snapshot().assignments[case.mention.id]
        == case.assignment
    )


@pytest.mark.parametrize(
    "origin_update",
    [{"method": "singleton"}, {"cohort_context_sha256": None}],
)
def test_unknown_or_noncohort_origin_refuses_before_effects(
    tmp_path, origin_update
):
    case = fixture(tmp_path, origin_update=origin_update)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(
        IdentityProvenanceRefused, match="cohort decision origin"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages
    assert not case.directory.exists()


def test_same_inherited_assignment_does_not_become_direct_human_origin(
    tmp_path,
):
    case = fixture(tmp_path, inherited=True)
    writes = case.wiki.wbi.item.writes
    with pytest.raises(
        IdentityProvenanceRefused, match="direct cohort decision"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes


def test_unregistered_source_refuses_before_graph_effects(tmp_path):
    case = fixture(tmp_path, registered=False)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(IdentityProvenanceRefused, match="source"):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages


def test_creation_receipt_damage_refuses_publication(tmp_path):
    case = fixture(tmp_path, "new_entity")
    receipt = case.registry.binding(case.binding.entity_id).receipt_locator
    case.wiki._recovery_edited.add(receipt)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises((CreationRefused, RecoveryEvidenceError)):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages
    assert not case.directory.exists()


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_projection_reuses_record_and_exact_confirmation(
    tmp_path, monkeypatch, committed
):
    case = fixture(tmp_path, "new_entity")
    original = identity_provenance.write_existing

    def interrupt(entity, **kwargs):
        if committed:
            original(entity, **kwargs)
        raise OSError("fictional lost projection response")

    with monkeypatch.context() as patch:
        patch.setattr(identity_provenance, "write_existing", interrupt)
        with pytest.raises(OSError, match="lost projection"):
            publish(case)
    items = set(case.wiki.wbi.item.store)
    target = case.wiki.wbi.item.get(case.binding.qid)
    target.labels.set("en", "Leopold")
    target.write()
    result = publish(case)
    assert result.record_qid in items
    assert result.changed is not committed
    assert set(case.wiki.wbi.item.store) == items
    assert (
        case.wiki.wbi.item.get(case.binding.qid).labels.get("en") == "Leopold"
    )
    assert len(list(anchor(case).references)) == 2


def aliases(wiki, qid):
    return [str(a) for a in wiki.wbi.item.get(qid).aliases.get("en") or []]


def recorded(case):
    """The assignment's alias intent and receipt as the server keeps them."""
    return alias_records(case.registry, case.assignment)


def shown(case):
    """The label and aliases the decision's review row showed."""
    (row,) = (
        row
        for row in csv.DictReader(io.StringIO(case.sheet))
        if row["proposal_id"] == case.decision.proposal.id
    )
    material = json.loads(row["basis"])["mentions"][case.mention.id]
    return [
        json.loads(row["labels"])[case.mention.id],
        *material["interpretation"]["interpretation"]["aliases"],
    ]


def curator_aliases(case, *aliases):
    """A curator's edit that leaves the item exactly these aliases."""
    item = case.wiki.wbi.item.get(case.binding.qid)
    item.aliases.set("en", list(aliases), ActionIfExists.REPLACE_ALL)
    item.write()


def on_alias_write(wiki, patch, effect):
    """Run ``effect(write, options)`` in place of the write that changes
    an item's aliases, as a server's reply or a curator's edit would
    interleave with it."""
    items = wiki.wbi.item
    get = items.get

    def read(qid):
        item = get(qid)
        write = item.write

        def guarded(**options):
            if (item.aliases.get("en") or []) != (
                get(qid).aliases.get("en") or []
            ):
                return effect(write, options)
            return write(**options)

        item.write = guarded
        return item

    patch.setattr(items, "get", read)


def lose_reply(committed):
    def effect(write, options):
        if committed:
            write(**options)
        raise OSError("fictional lost alias update reply")

    return effect


@pytest.mark.parametrize("operation", ["assign", "new_entity"])
def test_accepted_names_are_appended_as_shown_and_only_once(
    tmp_path, operation
):
    label = "Leopold Löbig"
    case = fixture(
        tmp_path,
        operation,
        aliases=(
            "Leopold LÖBIG",
            "Leopold",
            unicodedata.normalize("NFD", label),
        ),
    )
    curator_aliases(case, "Leopold")
    before = case.wiki.wbi.item.get(case.binding.qid).get_json()
    result = publish(case)
    after = case.wiki.wbi.item.get(case.binding.qid).get_json()
    names = list(
        dict.fromkeys(unicodedata.normalize("NFC", n) for n in shown(case))
    )
    assert names == [label, "Leopold", "Leopold LÖBIG"]
    assert result.aliases.added == ("Leopold LÖBIG",)
    assert aliases(case.wiki, case.binding.qid) == ["Leopold", "Leopold LÖBIG"]
    assert after["labels"] == before["labels"]
    assert after["descriptions"] == before["descriptions"]
    intent, receipt = recorded(case)
    assert receipt == result.aliases.receipt
    assert intent.names == receipt.added == ("Leopold LÖBIG",)
    assert (receipt.assignment, receipt.qid) == (
        case.assignment.revision_id,
        case.binding.qid,
    )
    contribution = receipt.contribution
    assert list(contribution.names) == names
    assert (
        contribution.mention_id,
        contribution.decision_id,
        contribution.decision_revision,
    ) == (case.mention.id, case.decision.id, case.decision.revision)
    writes = case.wiki.wbi.item.writes
    repeated = publish(case)
    assert repeated.aliases.added == ()
    assert repeated.aliases.receipt == receipt
    assert case.wiki.wbi.item.writes == writes


@pytest.mark.parametrize("edit", ["remove the alias", "rename the item"])
def test_a_curators_later_edit_stands_when_the_application_repeats(
    tmp_path, edit
):
    case = fixture(tmp_path, aliases=("Leopold LÖBIG",))
    assert publish(case).aliases.added == ("Leopold LÖBIG",)
    if edit == "remove the alias":
        curator_aliases(case, "Leopold")
    else:
        item = case.wiki.wbi.item.get(case.binding.qid)
        item.labels.set("en", "Curator's Leopold")
        item.write()
    curated = case.wiki.wbi.item.get(case.binding.qid).get_json()
    writes = case.wiki.wbi.item.writes
    assert publish(case).aliases.added == ()
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki.wbi.item.get(case.binding.qid).get_json() == curated


@pytest.mark.skipif(
    unicodedata.category("\U0001e08f") != "Cn",
    reason="the interpreter's Unicode tables know U+1E08F",
)
def test_a_name_the_instance_may_keep_otherwise_is_left_out_and_reported(
    tmp_path,
):
    # (here) a transcriber's stray combining Cyrillic i after the surname
    stray = "Leopold LÖBIG\U0001e08f"
    case = fixture(tmp_path, aliases=("Leopold", stray))
    result = publish(case)
    assert result.aliases.added == ("Leopold",)
    assert result.aliases.receipt.contribution.left_out == (stray,)
    assert aliases(case.wiki, case.binding.qid) == ["Leopold"]
    assert recorded(case)[1].contribution.left_out == (stray,)


def test_a_stale_revision_is_reread_not_overwritten(tmp_path, monkeypatch):
    case = fixture(tmp_path, aliases=("Leopold LÖBIG",))
    qid = case.binding.qid
    get = case.wiki.wbi.item.get

    def curator_first(write, options):
        curator = get(qid)
        curator.descriptions.set("en", "Curator's description of Leopold")
        curator.aliases.set("en", ["Leopold"])
        curator.write()
        return write(**options)

    with monkeypatch.context() as patch:
        on_alias_write(case.wiki, patch, curator_first)
        with pytest.raises(MWApiError):
            publish(case)
    assert aliases(case.wiki, qid) == ["Leopold"]
    assert recorded(case)[1] is None
    snapshot = case.registry.assignment_snapshot()
    result = publish(case)
    assert result.aliases.added == ("Leopold LÖBIG",)
    item = case.wiki.wbi.item.get(qid)
    assert aliases(case.wiki, qid) == ["Leopold", "Leopold LÖBIG"]
    assert str(item.descriptions.get("en")) == (
        "Curator's description of Leopold"
    )
    assert case.registry.assignment_snapshot() == snapshot


@pytest.mark.parametrize("committed", [False, True])
def test_an_interrupted_alias_update_completes_once_from_a_reread(
    tmp_path, monkeypatch, committed
):
    case = fixture(tmp_path, aliases=("Leopold LÖBIG",))
    with monkeypatch.context() as patch:
        on_alias_write(case.wiki, patch, lose_reply(committed))
        with pytest.raises(OSError):
            publish(case)
    intent, receipt = recorded(case)
    assert intent.names == ("Leopold LÖBIG",) and receipt is None
    writes = case.wiki.wbi.item.writes
    snapshot = case.registry.assignment_snapshot()
    result = publish(case)
    assert result.aliases.added == (() if committed else ("Leopold LÖBIG",))
    assert result.aliases.receipt.added == ("Leopold LÖBIG",)
    assert case.wiki.wbi.item.writes == writes + (0 if committed else 1)
    assert aliases(case.wiki, case.binding.qid) == ["Leopold LÖBIG"]
    assert case.registry.assignment_snapshot() == snapshot
    curator_aliases(case, "Leopold")
    writes = case.wiki.wbi.item.writes
    assert publish(case).aliases.added == ()
    assert case.wiki.wbi.item.writes == writes
    assert aliases(case.wiki, case.binding.qid) == ["Leopold"]


def test_one_record_gains_the_aliases_the_map_bound_writer_adds(tmp_path):
    case = fixture(
        tmp_path,
        aliases=(
            " Leopold LÖBIG\n",
            "Leopold LÖBIG",
            "Leopold",
            unicodedata.normalize("NFD", "Leopold Löbig"),
            " \t",
            "Son: Leopold LÖBIG, arrested " * 10,
        ),
    )
    curator_aliases(case, "Leopold")
    other = Wiki(detached=True)
    item = other.wbi.item.new()
    item.labels.set("en", "Leopold Löbig")
    item.aliases.set("en", ["Leopold"])
    item.write()
    BatchWriter(other)._ensure_aliases(
        item.id, [case.record.label, *case.record.aliases]
    )
    publish(case)
    gained = aliases(case.wiki, case.binding.qid)
    assert gained == aliases(other, item.id)
    assert len(gained) == 3


def test_correction_planning_lists_the_names_an_update_appended(
    tmp_path, monkeypatch
):
    case = fixture(tmp_path, aliases=("Leopold LÖBIG",))
    other = case.wiki.wbi.item.new()
    other.labels.set("en", "Eugen Löbig")
    target = case.registry.index_binding(
        case.registry.create(
            {"kind": "fictional correction target"},
            other,
            summary="fictional existing correction target",
        )
    )

    def plan():
        return plan_correction(
            case.registry,
            CorrectionRequest(
                source=case.registry.binding(case.binding.entity_id),
                target=target,
                assignments=(case.assignment,),
                reason="Review a possible mistaken mention assignment.",
            ),
        )

    unpublished = plan()
    assert unpublished.aliases == unpublished.pending_aliases == ()
    with monkeypatch.context() as patch:
        on_alias_write(case.wiki, patch, lose_reply(committed=False))
        with pytest.raises(OSError):
            publish(case)
    interrupted = plan()
    assert interrupted.aliases == ()
    (pending,) = interrupted.pending_aliases
    assert pending.names == ("Leopold LÖBIG",)
    result = publish(case)
    completed = plan()
    assert completed.aliases == (result.aliases.receipt,)
    assert completed.aliases[0].added == ("Leopold LÖBIG",)
    assert completed.pending_aliases == ()


def test_correction_planning_lists_nothing_where_nothing_was_appended(
    tmp_path,
):
    case = fixture(tmp_path)
    other = case.wiki.wbi.item.new()
    other.labels.set("en", "Eugen Löbig")
    target = case.registry.index_binding(
        case.registry.create(
            {"kind": "fictional correction target"},
            other,
            summary="fictional existing correction target",
        )
    )
    result = publish(case)
    assert result.aliases.receipt.contribution.names == ("Leopold Löbig",)
    assert result.aliases.receipt.added == ()
    plan = plan_correction(
        case.registry,
        CorrectionRequest(
            source=case.registry.binding(case.binding.entity_id),
            target=target,
            assignments=(case.assignment,),
            reason="Review a possible mistaken mention assignment.",
        ),
    )
    assert plan.aliases == plan.pending_aliases == ()
