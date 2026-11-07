"""Accepted support uses authoritative identities, never first matches."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String
from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank
from wikibaseintegrator.wbi_exceptions import MWApiError

from tests.unit.test_pipeline import CLAIMS, MEDIA_SHA1
from tests.unit.test_run_statements import PROPERTIES, ProvenanceWiki
from yizkr.claims import Claim
from yizkr.pipeline import assigned_support
from yizkr.pipeline.assigned_support import (
    StaleSupport,
    SupportPayload,
    SupportRefused,
    apply_support,
    inspect_support,
    prepare_support,
    resolve_artifacts,
)
from yizkr.pipeline.writer import BatchWriter


class Wiki(ProvenanceWiki):
    def iter_entities(self, props):
        assert props == "claims"
        for qid in self.wbi.item.store:
            yield qid, self.wbi.item.get(qid).get_json()


def item(wiki, label):
    entity = wiki.wbi.item.new()
    entity.labels.set("en", label)
    return entity.write().id


@pytest.fixture
def case():
    wiki = Wiki(detached=True)
    subject = item(wiki, "Leopold Löbig")
    sibling = item(wiki, "Rudolf Löbig")
    source = item(wiki, "Fictional marriage certificate")
    process = item(wiki, "Fictional accepted extraction")
    alternate_process = item(wiki, "Fictional repeated extraction")
    return SimpleNamespace(
        wiki=wiki,
        writer=BatchWriter(wiki),
        subject=subject,
        source=source,
        process=process,
        alternate_process=alternate_process,
        local_qids={"person-1": subject, "person-2": sibling},
        artifact_qids={MEDIA_SHA1: source},
    )


def claim(**updates):
    raw = copy.deepcopy(CLAIMS[0])
    raw.update(updates)
    return Claim.model_validate(raw)


def payload(case, assertion=None, *, run=None):
    return prepare_support(
        case.writer,
        assertion or claim(),
        local_qids=case.local_qids,
        artifact_qids=case.artifact_qids,
        run_qid=run or case.process,
    )


def apply(case, requested):
    before = inspect_support(case.wiki, requested)
    return apply_support(
        case.wiki,
        requested,
        before.revision,
        "Apply fictional accepted support",
    )


def graph_statement(case):
    (statement,) = case.wiki.wbi.item.get(case.subject).claims.get(
        PROPERTIES["sibling"]
    )
    return statement


def cited(ref, key):
    return [
        snak["datavalue"]["value"]["id"]
        for snak in ref["snaks"].get(PROPERTIES[key], [])
    ]


def test_preparation_resolves_roles_without_materialization(case, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preparation performed graph lookup or entity mutation")

    monkeypatch.setattr(case.wiki.wbi.item, "get", forbidden)
    monkeypatch.setattr(case.writer, "_materialize_local", forbidden)
    before = case.wiki.wbi.item.writes
    requested = payload(case)
    assert requested.qid == case.subject
    assert (
        requested.statement["mainsnak"]["datavalue"]["value"]["id"]
        == (case.local_qids["person-2"])
    )
    assert requested.statement["qualifiers"][PROPERTIES["point_in_time"]]
    assert cited(requested.primary, "stated_in") == [case.source]
    assert case.wiki.wbi.item.writes == before
    # Item-valued qualifier references use the same explicit mapping.
    named = claim(
        property="referred_to_as",
        value={"type": "string", "value": "Leopold"},
        qualifiers=[{"property": "perspective", "value": CLAIMS[0]["value"]}],
    )
    ref = payload(case, named).statement["qualifiers"][
        PROPERTIES["perspective"]
    ]
    assert ref[0]["datavalue"]["value"]["id"] == case.local_qids["person-2"]


def test_unresolved_local_reference_refuses_without_graph_effects(case):
    before = case.wiki.wbi.item.writes
    with pytest.raises(SupportRefused, match="unresolved"):
        prepare_support(
            case.writer,
            claim(),
            local_qids={"person-1": case.subject},
            artifact_qids=case.artifact_qids,
        )
    assert case.wiki.wbi.item.writes == before


def test_roundtrip_payload_adds_exact_guid_and_actual_reference_hashes(case):
    requested = SupportPayload.model_validate_json(
        payload(case).model_dump_json()
    )
    before = case.wiki.wbi.item.get(case.subject).get_json()
    state = apply(case, requested)
    stored = graph_statement(case)
    assert state.complete and state.guid == stored.id
    assert state.primary_hash == state.primary["hash"]
    assert state.process_hashes == (state.observed_process[0]["hash"],)
    assert cited(state.primary, "stated_in") == [case.source]
    assert cited(
        state.observed_process[0], "inferred_from_extraction_run"
    ) == [case.process]
    assert state.primary_hash in {ref.hash for ref in stored.references}
    after = case.wiki.wbi.item.get(case.subject).get_json()
    assert after["labels"] == before["labels"]
    assert after["aliases"] == before["aliases"]
    assert list(after["claims"]) == [PROPERTIES["sibling"]]


def test_exact_source_noop_records_old_process_without_adding_current_run(
    case,
):
    old = apply(case, payload(case))
    requested = payload(case, run=case.alternate_process)
    before = case.wiki.wbi.item.writes
    current = apply(case, requested)
    assert case.wiki.wbi.item.writes == before
    assert current.guid == old.guid
    assert current.primary == old.primary
    assert current.observed_process == old.observed_process
    assert cited(
        current.observed_process[0], "inferred_from_extraction_run"
    ) == [case.process]


def test_second_locator_keeps_statement_and_old_refs_then_adds_its_process(
    case,
):
    old = apply(case, payload(case))
    source = {**CLAIMS[0]["source"], "page_number": "2"}
    current = apply(
        case, payload(case, claim(source=source), run=case.alternate_process)
    )
    assert current.guid == old.guid
    assert current.primary_hash != old.primary_hash
    assert len(current.process_hashes) == 2
    assert {old.primary_hash, current.primary_hash} <= {
        ref.hash for ref in graph_statement(case).references
    }


def test_new_circumstance_gets_exact_separate_reference_and_process(case):
    old = apply(case, payload(case))
    circumstance = {**CLAIMS[0]["source"], "page_number": "2"}
    current = apply(
        case,
        payload(
            case,
            claim(circumstance_source=circumstance),
            run=case.alternate_process,
        ),
    )
    assert current.guid == old.guid and current.primary == old.primary
    assert current.circumstance_hash is not None
    assert current.circumstance_hash != current.primary_hash
    assert len(current.process_hashes) == 2
    assert (
        current.circumstance["snaks"][PROPERTIES["page_number"]][0][
            "datavalue"
        ]["value"]
        == "2"
    )


@pytest.mark.parametrize(
    "rank", [WikibaseRank.PREFERRED, WikibaseRank.DEPRECATED]
)
def test_curator_rank_and_exact_support_survive_noop(case, rank):
    requested = payload(case)
    old = apply(case, requested)
    target = case.wiki.wbi.item.get(case.subject)
    target.claims.get(PROPERTIES["sibling"])[0].rank = rank
    target.write()
    before = case.wiki.wbi.item.writes
    current = apply(case, payload(case, run=case.alternate_process))
    assert current.guid == old.guid and current.primary == old.primary
    assert graph_statement(case).rank == rank
    assert case.wiki.wbi.item.writes == before


@pytest.mark.parametrize("curator", ["rank", "no_process"])
def test_curator_artifact_only_citation_is_not_exact_support(case, curator):
    apply(case, payload(case))
    target = case.wiki.wbi.item.get(case.subject)
    statement = target.claims.get(PROPERTIES["sibling"])[0]
    if curator == "rank":
        statement.rank = WikibaseRank.PREFERRED
    else:
        statement.references.references = [
            ref
            for ref in statement.references
            if ref.snaks.get(PROPERTIES["stated_in"])
        ]
    target.write()
    before = case.wiki.wbi.item.writes
    requested = payload(
        case, claim(source={**CLAIMS[0]["source"], "page_number": "2"})
    )
    with pytest.raises(SupportRefused, match="curator"):
        apply(case, requested)
    assert case.wiki.wbi.item.writes == before


def test_two_equal_statements_are_ambiguous_not_first_equal(case):
    apply(case, payload(case))
    target = case.wiki.wbi.item.get(case.subject)
    old = target.claims.get(PROPERTIES["sibling"])[0]
    other = copy.deepcopy(old)
    other.id = None
    target.claims.add(other, action_if_exists=ActionIfExists.FORCE_APPEND)
    target.write()
    before = case.wiki.wbi.item.writes
    with pytest.raises(SupportRefused, match="ambiguous support"):
        inspect_support(case.wiki, payload(case))
    assert case.wiki.wbi.item.writes == before


def test_full_qualifier_value_is_not_reduced_to_date_string(case):
    requested = payload(case)
    first = apply(case, requested)
    # Calendar model is an independent part of the actual qualifier value.
    target = case.wiki.wbi.item.get(case.subject)
    old = target.claims.get(PROPERTIES["sibling"])[0]
    snak = old.qualifiers.get(PROPERTIES["point_in_time"])[0]
    snak.datavalue["value"]["calendarmodel"] = (
        "http://www.wikidata.org/entity/Q1985786"
    )
    target.write()
    before = inspect_support(case.wiki, requested)
    assert before.guid is None and not before.complete
    second = apply(case, requested)
    assert second.guid != first.guid
    assert (
        len(
            case.wiki.wbi.item.get(case.subject).claims.get(
                PROPERTIES["sibling"]
            )
        )
        == 2
    )


def test_native_source_part_and_placement_remain_in_exact_support(case):
    support = {
        "container": CLAIMS[0]["source"]["artifact"],
        "part_id": "a" * 64,
        "locator": {"kind": "embedded_resource", "native_id": "12 0 R"},
        "placements": [
            {"id": "b" * 64, "locator": {"kind": "image", "position": 1}}
        ],
        "reading_sha256": "c" * 64,
    }
    source = {**CLAIMS[0]["source"], "support": support}
    first = apply(case, payload(case, claim(source=source)))
    support["placements"][0]["id"] = "d" * 64
    second = apply(case, payload(case, claim(source=source)))
    assert first.guid == second.guid
    assert first.primary_hash != second.primary_hash
    for state, placement in ((first, "b" * 64), (second, "d" * 64)):
        values = state.primary["snaks"][PROPERTIES["source_part_locator"]]
        assert placement in values[0]["datavalue"]["value"]
        assert "c" * 64 in values[0]["datavalue"]["value"]


def test_server_value_normalization_must_not_fabricate_exact_readback(case):
    # A malformed saved payload bypasses normal NFC preparation. The graph
    # serves normalized text: it cannot receipt the unequal old source block.
    requested = payload(case).model_copy(deep=True)
    requested.primary["snaks"][PROPERTIES["transcription_excerpt"]][0][
        "datavalue"
    ]["value"]["text"] = "Rudolf Lo\u0308big"
    with pytest.raises(SupportRefused, match="did not establish exact"):
        apply(case, requested)
    assert not inspect_support(case.wiki, requested).complete


def test_stale_revision_refuses_even_when_support_is_already_complete(case):
    requested = payload(case)
    state = apply(case, requested)
    target = case.wiki.wbi.item.get(case.subject)
    target.descriptions.set("en", "Curator's fictional revision")
    target.write()
    before = case.wiki.wbi.item.writes
    with pytest.raises(StaleSupport):
        apply_support(
            case.wiki, requested, state.revision, "fictional stale write"
        )
    assert case.wiki.wbi.item.writes == before


def test_server_revision_conflict_is_not_retried(case, monkeypatch):
    requested = payload(case)
    state = inspect_support(case.wiki, requested)
    original = assigned_support.write_existing
    attempts = []

    def competing_write(target, *, summary):
        curator = case.wiki.wbi.item.get(target.id)
        curator.descriptions.set(
            "en", "Curator's concurrent fictional revision"
        )
        curator.write()
        attempts.append(target.lastrevid)
        return original(target, summary=summary)

    monkeypatch.setattr(assigned_support, "write_existing", competing_write)
    with pytest.raises(MWApiError, match="stale item"):
        apply_support(
            case.wiki, requested, state.revision, "fictional conflict"
        )
    assert attempts == [state.revision]
    assert not inspect_support(case.wiki, requested).complete
    assert case.wiki.wbi.item.get(case.subject).descriptions.get(
        "en"
    ).value == ("Curator's concurrent fictional revision")


def test_lost_success_response_can_be_read_back_without_duplicate_write(
    case, monkeypatch
):
    requested = payload(case)
    original = assigned_support.write_existing

    def lost_response(target, *, summary):
        original(target, summary=summary)
        raise OSError("fictional response lost after commit")

    monkeypatch.setattr(assigned_support, "write_existing", lost_response)
    with pytest.raises(OSError, match="response lost"):
        apply(case, requested)
    observed = inspect_support(case.wiki, requested)
    assert observed.complete and observed.guid and observed.primary_hash
    before = case.wiki.wbi.item.writes
    assert apply(case, requested) == observed
    assert case.wiki.wbi.item.writes == before


def test_changed_vocabulary_refuses_before_target_mutation(case, monkeypatch):
    requested = payload(case)
    original = case.wiki.vocabulary

    def changed():
        properties, items = original()
        properties["stated_in"] = "P999999"
        return properties, items

    monkeypatch.setattr(case.wiki, "vocabulary", changed)
    before = case.wiki.wbi.item.writes
    with pytest.raises(SupportRefused, match="vocabulary changed"):
        apply(case, requested)
    assert case.wiki.wbi.item.writes == before


def test_authoritative_reference_hash_is_required_for_receipt(
    case, monkeypatch
):
    requested = payload(case)
    apply(case, requested)
    original = case.wiki.wbi.item.get

    def without_hash(qid):
        target = original(qid)
        for statement in target.claims:
            for ref in statement.references:
                ref.hash = None
        return target

    monkeypatch.setattr(case.wiki.wbi.item, "get", without_hash)
    with pytest.raises(SupportRefused, match="reference has no usable hash"):
        inspect_support(case.wiki, requested)


@pytest.mark.parametrize("state", ["active", "retired", "duplicate"])
def test_source_resolution_requires_one_current_holder(case, state):
    source = case.wiki.wbi.item.get(case.source)
    statement = String(prop_nr=PROPERTIES["media_sha1"], value=MEDIA_SHA1)
    if state == "retired":
        statement.rank = WikibaseRank.DEPRECATED
    source.claims.add(statement)
    source.write()
    if state == "duplicate":
        duplicate = case.wiki.wbi.item.new()
        duplicate.claims.add(
            String(prop_nr=PROPERTIES["media_sha1"], value=MEDIA_SHA1)
        )
        duplicate.write()
    before = case.wiki.wbi.item.writes
    if state == "active":
        assert (
            resolve_artifacts(case.wiki, [MEDIA_SHA1.upper()])
            == case.artifact_qids
        )
    else:
        with pytest.raises(SupportRefused, match="missing or ambiguous"):
            resolve_artifacts(case.wiki, [MEDIA_SHA1])
    assert case.wiki.wbi.item.writes == before
