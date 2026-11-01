"""Interrupted creation preserves QIDs and curator work."""

from __future__ import annotations

import copy

import pytest

from tests.fixtures.identity import mapped, write_mapped
from tests.unit.test_pipeline import MEDIA_SHA1, PROPERTIES, FakeWiki, batch
from yizkr.core.write_recovery import CreationRefused
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.writer import BatchWriter, _run_terms


class LostReply(BaseException):
    pass


def wiki() -> FakeWiki:
    return FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)


def interrupt_creation(
    monkeypatch, target: FakeWiki, summary: str
) -> list[str]:
    """Stop the process after the fake server has committed a creation."""
    bound = target.wbi.item._bound
    created: list[str] = []

    def wrap(item):
        item = bound(item)
        write = item.write

        def lose_reply(summary=None, **options):
            new = item.id is None
            result = write(summary=summary, **options)
            if new and summary.endswith(prefix):
                created.append(result.id)
                raise LostReply("creation response lost")
            return result

        item.write = lose_reply
        return item

    prefix = summary
    monkeypatch.setattr(target.wbi.item, "_bound", wrap)
    return created


@pytest.mark.parametrize(
    "summary",
    ["ingest: extraction run", "ingest: entity proposed by extraction"],
)
def test_lost_creation_reply_recovers_after_curator_changes_terms(
    monkeypatch, summary
):
    target = wiki()
    with monkeypatch.context() as patch:
        created = interrupt_creation(patch, target, summary)
        with pytest.raises(LostReply):
            write_mapped(BatchWriter(target), batch())
    (qid,) = created
    item = target.wbi.item.get(qid)
    item.labels.set("en", "Eugen Löbig — curator's retained label")
    item.descriptions.set("en", "curator's retained description")
    item.write(summary="curator: correct display terms")
    creations = len(target.wbi.item.creation_history)

    result = write_mapped(BatchWriter(target), batch())

    recovered = (
        result.run_qid
        if summary == "ingest: extraction run"
        else result.created_items["person-1"]
    )
    assert recovered == qid
    assert target.wbi.item.get(qid).labels.get("en").value == (
        "Eugen Löbig — curator's retained label"
    )
    assert target.wbi.item.get(qid).descriptions.get("en").value == (
        "curator's retained description"
    )
    expected_remaining = 2 if summary == "ingest: extraction run" else 1
    assert (
        len(target.wbi.item.creation_history) == creations + expected_remaining
    )
    assert result.written == len(batch().claims)


def test_bound_cluster_bypasses_a_now_incompatible_display_label():
    target = wiki()
    original = write_mapped(BatchWriter(target), batch())
    qid = original.created_items["person-1"]
    person = target.wbi.item.get(qid)
    previous = str(person.labels.get("en"))
    person.labels.set("en", "Eugen Löbig — corrected")
    person.descriptions.set("en", "retained curator description")
    person.write(summary="curator: correct display terms")
    # A registered source now holds the old label. Collision planning for
    # a new entity must not displace the established person binding.
    source = target.wbi.item.new()
    source.labels.set("en", previous)
    from wikibaseintegrator.datatypes import String

    source.claims.add(String(prop_nr=PROPERTIES["media_sha1"], value="a" * 40))
    source.write(summary="curator: register fictional source")
    before = len(target.wbi.item.creation_history)

    repeated = write_mapped(BatchWriter(target), batch())

    assert repeated.created_items == original.created_items
    assert repeated.refused == []
    assert len(target.wbi.item.creation_history) == before
    assert str(target.wbi.item.get(qid).labels.get("en")) == (
        "Eugen Löbig — corrected"
    )


def test_matching_legacy_cluster_terms_require_migration_before_writes():
    target = wiki()
    planner = BatchWriter(target, dry_run=True)
    write_mapped(planner, batch())
    placement = planner._placements["person-1"]
    item = target.wbi.item.new()
    item.labels.set("en", placement.label)
    item.descriptions.set("en", placement.description)
    item.write(summary="legacy: fictional item")
    before = target.wbi.item.writes
    pages = copy.deepcopy(target._recovery_pages)

    with pytest.raises(CreationRefused, match="migration"):
        write_mapped(BatchWriter(target), batch())

    assert target.wbi.item.writes == before
    assert target._recovery_pages == pages


def test_matching_legacy_run_label_requires_migration_before_writes():
    target = wiki()
    label, description = _run_terms(batch(), mapped(batch()).hash)
    item = target.wbi.item.new()
    item.labels.set("en", label)
    item.descriptions.set("en", description)
    item.write(summary="legacy: fictional process item")
    before = target.wbi.item.writes
    pages = copy.deepcopy(target._recovery_pages)

    with pytest.raises(CreationRefused, match="migration"):
        write_mapped(BatchWriter(target), batch())

    assert target.wbi.item.writes == before
    assert target._recovery_pages == pages


def test_dry_run_creates_no_registry_or_graph_state():
    target = wiki()
    pages = copy.deepcopy(target._recovery_pages)

    result = write_mapped(BatchWriter(target, dry_run=True), batch())

    assert result.run_qid == "(new:run)"
    assert target.wbi.item.writes == 0
    assert target._recovery_pages == pages


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
def test_renaming_run_metadata_does_not_remove_the_one_map_guard(dry_run):
    target = wiki()
    original = batch()
    first = BatchWriter(target).write_batch(
        original, identity=mapped(original)
    )
    run = target.wbi.item.get(first.run_qid)
    run.labels.set("en", "Family-history review")
    run.write(summary="curator: clarify process label")
    changed = batch().model_dump(mode="json")
    for claim in changed["claims"]:
        claim["subject"]["label"] = "Eugen Löbig"
        if claim["value"]["type"] == "item":
            claim["value"]["value"]["label"] = "Hildegard Laudler"
    other = type(original).model_validate(changed)
    before = target.wbi.item.writes

    with pytest.raises(CreationRefused, match="map"):
        BatchWriter(target, dry_run=dry_run).write_batch(
            other, identity=mapped(other)
        )

    assert target.wbi.item.writes == before


@pytest.mark.parametrize("local_entity", [False, True], ids=["qids", "mixed"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
def test_legacy_generation_refusal_precedes_equivalence_aliases(
    local_entity, dry_run
):
    target = wiki()
    person = target.wbi.item.new()
    person.labels.set("en", "Eugen Löbig")
    person.write(summary="legacy: fictional entity")
    target._recovery_pages.clear()
    data = batch().model_dump(mode="json")
    data["entities"] = [
        {
            "id": "person-1",
            "label": "Eugen the hatter",
            "kind": "human",
            "qid": person.id,
        },
    ]
    if local_entity:
        data["entities"].append(
            {"id": "person-2", "label": "Person Two", "kind": "human"}
        )
    else:
        data["claims"] = [data["claims"][1]]
    for claim in data["claims"]:
        claim["subject"] = {"kind": "qid", "qid": person.id}
    given = type(batch()).model_validate(data)
    before = target.wbi.item.writes

    with pytest.raises(CreationRefused, match="migration"):
        BatchWriter(target, dry_run=dry_run).write_batch(
            given, identity=mapped(given), one_map=not local_entity
        )

    assert target.wbi.item.writes == before
    assert not target.wbi.item.get(person.id).aliases.get("en")
    assert target._recovery_pages == {}


def test_place_run_recovers_a_lost_creation_reply_after_rename(monkeypatch):
    target = wiki()
    with monkeypatch.context() as patch:
        created = interrupt_creation(
            patch, target, "reconcile-places: run item"
        )
        with pytest.raises(LostReply):
            ensure_places_run_item(target, "a" * 64, "fake-model")
    (qid,) = created
    item = target.wbi.item.get(qid)
    item.labels.set("en", "Fläckfeld place review")
    item.write(summary="curator: clarify process label")

    recovered = ensure_places_run_item(target, "a" * 64, "fake-model")

    assert recovered == qid
    assert len(target.wbi.item.creation_history) == 1
    assert (
        str(target.wbi.item.get(qid).labels.get("en"))
        == "Fläckfeld place review"
    )
    assert target.wbi.item.get(qid).claims.get(PROPERTIES["instance_of"])


def test_existing_updates_forward_revision_and_disable_automatic_retry(
    monkeypatch,
):
    """The stricter fake refuses stale snapshots; real Wikibase can merge
    nonconflicting changes. The real lane checks a same-field conflict.
    """
    from wikibaseintegrator.wbi_exceptions import MWApiError

    target = wiki()
    original = write_mapped(BatchWriter(target), batch())
    qid = original.created_items["person-1"]
    bound = target.wbi.item._bound
    raced = False
    attempts = []

    def wrap(item):
        item = bound(item)
        write = item.write

        def concurrent(summary=None, **options):
            nonlocal raced
            if item.id == qid and options.get("baserevid"):
                attempts.append(dict(options))
            if item.id == qid and options.get("baserevid") and not raced:
                raced = True
                curator = target.wbi.item.get(qid)
                curator.descriptions.set(
                    "en", "curator's concurrent correction"
                )
                curator.write(summary="curator: correct description")
            return write(summary=summary, **options)

        item.write = concurrent
        return item

    # The renamed item receives its source name as an alias; the simulated
    # curator then wins between that read and write.
    person = target.wbi.item.get(qid)
    person.labels.set("en", "Eugen Löbig — corrected label")
    person.write(summary="curator: correct label")
    expected_revision = person.lastrevid
    monkeypatch.setattr(target.wbi.item, "_bound", wrap)

    with pytest.raises(MWApiError, match="stale item"):
        write_mapped(BatchWriter(target), batch())

    assert raced
    assert attempts == [
        {"baserevid": expected_revision, "max_retries": 1, "retry_after": 0}
    ]
    assert str(target.wbi.item.get(qid).descriptions.get("en")) == (
        "curator's concurrent correction"
    )
