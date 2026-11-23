"""Crash and competing-client boundaries for server-owned creation intents."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from wikibaseintegrator.datatypes import Item

from tests.unit.test_pipeline import FakeWiki
from yizkr.core.config import Config
from yizkr.core.smoke_marker import SMOKE_DESCRIPTION, SMOKE_LABEL
from yizkr.core.wiki import (
    EntityCreation,
    RecoveryEvidenceError,
    WikiClient,
    WikiPage,
)
from yizkr.core.write_recovery import (
    GENERATION_PAGE,
    AmbiguousCreation,
    CreationRefused,
    CreationRegistry,
    write_existing,
)

HANG_GUARD = 30
OPERATION = {"kind": "entity", "source": "family-tree-1984", "local_id": "e1"}


def person(wiki, label="Leopold Löbig"):
    item = wiki.wbi.item.new()
    item.labels.set("en", label)
    return item


def create(wiki, **kwargs):
    return CreationRegistry(wiki).create(
        OPERATION, person(wiki), summary="ingest: fictional person", **kwargs
    )


def test_repeated_creation_uses_binding_despite_changed_generated_terms():
    wiki = FakeWiki()
    first = create(wiki)
    second = CreationRegistry(copy.copy(wiki)).create(
        OPERATION, person(wiki, "Leopold"), summary="ingest: another proposal"
    )
    assert first.created and not second.created
    assert first.qid == second.qid
    assert first.entity_id == second.entity_id
    assert wiki.wbi.item.writes == 1
    assert CreationRegistry(wiki).lookup(OPERATION) == second
    assert all(
        "created" not in page.text for page in wiki._recovery_pages.values()
    )


def test_response_lost_after_server_commit_recovers_without_second_post():
    wiki = FakeWiki()
    item = person(wiki)
    send = item.write
    requests = []

    def lost_response(**kwargs):
        requests.append(kwargs)
        send(**kwargs)
        raise ConnectionError("fictional response loss")

    item.write = lost_response
    binding = CreationRegistry(wiki).create(OPERATION, item, summary="ingest")
    assert binding.qid == item.id
    assert not binding.created
    assert len(requests) == 1
    assert requests[0]["max_retries"] == 1
    assert requests[0]["retry_after"] == 0
    assert create(wiki).qid == binding.qid
    assert wiki.wbi.item.writes == 1


def test_process_interrupted_after_creation_is_recovered_read_only():
    wiki = FakeWiki()
    item = person(wiki)
    send = item.write

    def interrupted(**kwargs):
        send(**kwargs)
        raise KeyboardInterrupt()

    item.write = interrupted
    with pytest.raises(KeyboardInterrupt):
        CreationRegistry(wiki).create(OPERATION, item, summary="ingest")
    pages = dict(wiki._recovery_pages)
    recovered = CreationRegistry(wiki).lookup(OPERATION)
    assert recovered.qid == item.id
    assert not recovered.created
    assert wiki._recovery_pages == pages
    assert CreationRegistry(wiki).reconcile(OPERATION).qid == item.id
    assert wiki.wbi.item.writes == 1
    assert any(title.endswith("/receipt") for title in wiki._recovery_pages)


def test_dispatched_but_no_visible_creation_never_retries():
    wiki = FakeWiki()
    item = person(wiki)
    attempts = []

    def uncertain(**kwargs):
        attempts.append(kwargs)
        raise ConnectionError("request may still be in flight")

    item.write = uncertain
    with pytest.raises(AmbiguousCreation, match="0 visible"):
        CreationRegistry(wiki).create(OPERATION, item, summary="ingest")
    with pytest.raises(AmbiguousCreation, match="0 visible"):
        create(wiki)
    assert len(attempts) == 1
    assert wiki.wbi.item.writes == 0


def test_two_clients_compete_while_first_request_is_in_flight():
    wiki = FakeWiki()
    entered, release = Event(), Event()
    item = person(wiki)
    send = item.write

    def held(**kwargs):
        entered.set()
        assert release.wait(HANG_GUARD)
        return send(**kwargs)

    item.write = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            CreationRegistry(wiki).create, OPERATION, item, summary="ingest"
        )
        assert entered.wait(HANG_GUARD)
        try:
            with pytest.raises(AmbiguousCreation):
                create(copy.copy(wiki))
        finally:
            release.set()
        first = future.result(HANG_GUARD)
    assert create(copy.copy(wiki)).qid == first.qid
    assert wiki.wbi.item.writes == 1


def test_duplicate_creation_evidence_never_selects_earliest_qid():
    wiki = FakeWiki()
    first = create(wiki)
    title = next(
        title for title in wiki._recovery_pages if title.endswith("/receipt")
    )
    del wiki._recovery_pages[title]
    history = wiki.wbi.item.creation_history[0]
    other = person(wiki, "Leopold")
    other.write(summary=history["summary"])
    with pytest.raises(AmbiguousCreation, match="2 visible"):
        CreationRegistry(wiki).lookup(OPERATION)
    assert other.id != first.qid
    assert wiki.wbi.item.writes == 2


@pytest.mark.parametrize("damage", ["item", "marker", "receipt", "generation"])
def test_damaged_binding_refuses_without_new_item(damage):
    wiki = FakeWiki()
    binding = create(wiki)
    if damage == "item":
        del wiki.wbi.item.store[binding.qid]
    elif damage == "marker":
        wiki.wbi.item.creation_history[0]["hidden"] = True
    elif damage == "receipt":
        title = next(t for t in wiki._recovery_pages if t.endswith("/receipt"))
        wiki._recovery_edited.add(title)
    else:
        del wiki._recovery_pages[GENERATION_PAGE]
    with pytest.raises((CreationRefused, RecoveryEvidenceError)):
        create(wiki)
    assert wiki.wbi.item.writes == 1


def test_identity_map_is_one_immutable_generation_wide_claim():
    wiki = FakeWiki()
    registry = CreationRegistry(wiki)
    before = dict(wiki._recovery_pages)
    registry.check_identity_map("a" * 64)
    assert wiki._recovery_pages == before
    registry.claim_identity_map("a" * 64)
    CreationRegistry(copy.copy(wiki)).claim_identity_map("a" * 64)
    with pytest.raises(CreationRefused, match="another identity map"):
        CreationRegistry(copy.copy(wiki)).claim_identity_map("b" * 64)
    before = dict(wiki._recovery_pages)
    with pytest.raises(CreationRefused, match="another identity map"):
        CreationRegistry(copy.copy(wiki)).check_identity_map("b" * 64)
    assert wiki._recovery_pages == before
    assert wiki.wbi.item.writes == 0


def test_lookup_without_generation_is_read_only_and_legacy_creation_refuses():
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    person(wiki).write(summary="fictional legacy ingestion")
    before = wiki.wbi.item.writes
    assert CreationRegistry(wiki).lookup(OPERATION) is None
    assert not wiki._recovery_pages
    with pytest.raises(CreationRefused, match="verified migration"):
        create(wiki)
    assert not wiki._recovery_pages
    assert wiki.wbi.item.writes == before


def test_vocabulary_only_graph_can_initialize_generation_once():
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    first = CreationRegistry(wiki)
    second = CreationRegistry(copy.copy(wiki))
    assert first.generation is None
    first.claim_identity_map("a" * 64)
    second.claim_identity_map("a" * 64)
    assert first.generation == second.generation
    assert wiki.wbi.item.writes == 0


@pytest.mark.parametrize("vocabulary", [False, True])
def test_generation_preflight_is_read_only_for_an_admissible_graph(vocabulary):
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    if not vocabulary:
        wiki.vocabulary = lambda: ({}, {})
    registry = CreationRegistry(wiki)

    assert registry.check_generation() is None
    assert not wiki._recovery_pages
    assert wiki.wbi.item.writes == 0
    generation = registry.ensure_generation()
    before = dict(wiki._recovery_pages)
    assert registry.check_generation() == generation
    assert registry.ensure_generation() == generation
    assert wiki._recovery_pages == before


def test_generation_preflight_refuses_legacy_without_mutating_it():
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    person(wiki).write(summary="fictional legacy ingestion")
    before = wiki.wbi.item.writes

    with pytest.raises(CreationRefused, match="verified migration"):
        CreationRegistry(wiki).check_generation()

    assert not wiki._recovery_pages
    assert wiki.wbi.item.writes == before


def smoke_marker(wiki):
    """The item `yizkr smoke-test` writes: its label and description, and
    nothing else."""
    item = wiki.wbi.item.new()
    item.labels.set("en", SMOKE_LABEL)
    item.descriptions.set("en", SMOKE_DESCRIPTION)
    return item


def without_generation():
    """The in-memory wiki holding the vocabulary alone, with no generation
    or recovery page, as a scratch reset leaves an instance before its
    smoke test."""
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    return wiki


def test_a_smoke_test_marker_item_admits_the_generation():
    wiki = without_generation()
    smoke_marker(wiki).write()
    writes = wiki.wbi.item.writes
    registry = CreationRegistry(wiki)

    assert registry.check_generation() is None
    assert not wiki._recovery_pages
    generation = registry.ensure_generation()
    assert list(wiki._recovery_pages) == [GENERATION_PAGE]
    assert CreationRegistry(copy.copy(wiki)).ensure_generation() == generation
    assert list(wiki._recovery_pages) == [GENERATION_PAGE]
    assert wiki.wbi.item.writes == writes


@pytest.mark.parametrize(
    "change", ["statement", "description", "alias", "German label"]
)
def test_an_item_that_is_more_than_the_marker_refuses_the_generation(change):
    wiki = without_generation()
    properties, items = wiki.vocabulary()
    item = smoke_marker(wiki)
    if change == "statement":
        item.claims.add(
            Item(prop_nr=properties["instance_of"], value=items["human"])
        )
    elif change == "description":
        item.descriptions.set("en", "A hatter of Flörburg")
    elif change == "alias":
        item.aliases.set("en", "Smoke-test item")
    else:
        item.labels.set("de", "Rauchtest-Objekt")
    item.write()
    registry = CreationRegistry(wiki)

    with pytest.raises(CreationRefused):
        registry.ensure_generation()
    assert not wiki._recovery_pages


def test_a_marker_beside_an_ingested_person_refuses_the_generation():
    wiki = without_generation()
    properties, items = wiki.vocabulary()
    smoke_marker(wiki).write()
    leopold = person(wiki)
    leopold.claims.add(
        Item(prop_nr=properties["instance_of"], value=items["human"])
    )
    leopold.write(summary="fictional legacy ingestion")
    registry = CreationRegistry(wiki)

    with pytest.raises(CreationRefused):
        registry.ensure_generation()
    assert not wiki._recovery_pages


def test_observed_generation_change_stops_existing_registry():
    wiki = FakeWiki()
    registry = CreationRegistry(wiki)
    original = registry.generation
    changed = original.model_copy(update={"generation": "3" * 32})
    wiki._recovery_pages[GENERATION_PAGE] = WikiPage(
        GENERATION_PAGE, changed.model_dump_json(), 2
    )
    with pytest.raises(CreationRefused, match="changed"):
        registry.lookup(OPERATION)


def test_existing_write_uses_revision_guard_and_single_attempt():
    wiki = FakeWiki(detached=True)
    binding = create(wiki)
    first = wiki.wbi.item.get(binding.qid)
    stale = wiki.wbi.item.get(binding.qid)
    first.descriptions.set("en", "fictional person")
    write_existing(first, summary="ingest: description")
    stale.descriptions.set("en", "another description")
    with pytest.raises(Exception, match="stale item"):
        write_existing(stale, summary="ingest: stale description")
    assert wiki.wbi.item.writes == 2
    assert (
        wiki.wbi.item.get(binding.qid).descriptions.get("en").value
        == "fictional person"
    )


class ApiClient(WikiClient):
    def __init__(self, answer):
        super().__init__(
            Config(
                wikibase_url="http://wiki.invalid",
                concept_uri="http://wiki.invalid/entity/",
                admin_name="",
                admin_pass="",
                wdqs_url=None,
                sparql_url=None,
                env_file=None,
            )
        )
        self.answer = answer
        self.calls = []

    def api_get(self, params):
        self.calls.append(params)
        return self.answer(params)


def immutable_response(params, page, *, events=()):
    """A query can independently request log entries and page revisions."""
    query = {}
    if params.get("list") == "logevents":
        query["logevents"] = [
            event for event in events if event["type"] == params["letype"]
        ]
    if params.get("prop") == "revisions":
        query["pages"] = [page]
    return {"query": query}


def immutable_record(*, text="{}", revision=4, parent=0):
    return {
        "title": GENERATION_PAGE,
        "revisions": [
            {
                "revid": revision,
                "parentid": parent,
                "slots": {"main": {"content": text}},
            }
        ],
    }


def test_immutable_api_record_reads_content_with_two_query_budget():
    client = ApiClient(
        lambda params: immutable_response(params, immutable_record())
    )

    assert client.immutable_page(GENERATION_PAGE) == WikiPage(
        GENERATION_PAGE, "{}", 4
    )
    # Immutable evidence reads dominate the real integration suite. Keep
    # this request budget while the refusal cases protect both log checks.
    assert len(client.calls) <= 2
    assert all(call["letitle"] == GENERATION_PAGE for call in client.calls)
    content_reads = [c for c in client.calls if c.get("prop") == "revisions"]
    assert len(content_reads) == 1
    assert content_reads[0]["titles"] == GENERATION_PAGE


@pytest.mark.parametrize("parent,hidden", [(1, False), (0, True)])
def test_immutable_api_record_rejects_edits_or_hidden_content(parent, hidden):
    page = immutable_record(parent=parent)
    if hidden:
        page["revisions"][0]["slots"]["main"].pop("content")
    client = ApiClient(lambda params: immutable_response(params, page))

    message = (
        "content or revision is unavailable"
        if hidden
        else "not an immutable initial revision"
    )
    with pytest.raises(RecoveryEvidenceError, match=message):
        client.immutable_page(GENERATION_PAGE)


@pytest.mark.parametrize(
    "log_type,action",
    [("delete", "delete"), ("delete", "restore"), ("move", "move")],
)
def test_immutable_api_record_rejects_logged_changes_despite_initial_content(
    log_type, action
):
    event = {"logid": 1, "type": log_type, "action": action}
    client = ApiClient(
        lambda params: immutable_response(
            params, immutable_record(), events=[event]
        )
    )

    with pytest.raises(RecoveryEvidenceError, match="deleted or moved"):
        client.immutable_page(GENERATION_PAGE)


def test_immutable_api_record_absent_without_logs_is_unused():
    client = ApiClient(
        lambda params: immutable_response(
            params, {"title": GENERATION_PAGE, "missing": True}
        )
    )

    assert client.immutable_page(GENERATION_PAGE) is None
    assert len(client.calls) <= 2


def test_deleted_dispatch_cannot_be_treated_as_an_unused_title():
    client = ApiClient(lambda params: {"query": {"logevents": [{"logid": 1}]}})
    with pytest.raises(RecoveryEvidenceError, match="deleted or moved"):
        client.immutable_page("Project:Yizkr write recovery/dispatch")
    assert len(client.calls) == 1


def test_creation_scan_follows_continuation_and_requires_initial_revision():
    marker = "[yizkr-create:fictional]"

    def answer(params):
        if "arvcontinue" not in params:
            return {
                "query": {
                    "allrevisions": [
                        {
                            "title": "Q10",
                            "revisions": [
                                {"revid": 4, "parentid": 3, "comment": marker},
                                {
                                    "revid": 3,
                                    "parentid": 0,
                                    "comment": "unrelated",
                                },
                            ],
                        }
                    ]
                },
                "continue": {"arvcontinue": "next", "continue": "-||"},
            }
        return {
            "query": {
                "allrevisions": [
                    {
                        "title": "Item:Q11",
                        "revisions": [
                            {
                                "revid": 5,
                                "parentid": 0,
                                "comment": "prefix " + marker,
                            },
                        ],
                    }
                ]
            }
        }

    client = ApiClient(answer)
    assert client.creation_revisions(marker) == [EntityCreation("Q11", 5)]
    assert len(client.calls) == 2


@pytest.mark.parametrize("wrong", ["title", "parent", "comment", "live"])
def test_receipt_api_verification_checks_initial_revision_and_live_target(
    wrong,
):
    marker = "[yizkr-create:fictional]"

    def answer(params):
        if params["action"] == "wbgetentities":
            return {
                "entities": {
                    "Q10": {
                        "id": "Q10",
                        "type": "item",
                        **({"missing": ""} if wrong == "live" else {}),
                    }
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "Q11" if wrong == "title" else "Q10",
                        "revisions": [
                            {
                                "revid": 3,
                                "parentid": 1 if wrong == "parent" else 0,
                                "comment": "another marker"
                                if wrong == "comment"
                                else marker,
                            }
                        ],
                    }
                ]
            }
        }

    with pytest.raises(RecoveryEvidenceError):
        ApiClient(answer).verify_creation("Q10", 3, marker)


@pytest.mark.parametrize("outcome", ["accepted", "lost-response", "competing"])
def test_createonly_api_never_retries_or_overwrites(monkeypatch, outcome):
    from types import SimpleNamespace

    from wikibaseintegrator import wbi_helpers

    sent = []
    existing = "other invocation" if outcome == "competing" else None

    def send(**kwargs):
        nonlocal existing
        sent.append(kwargs)
        if existing is not None:
            raise RuntimeError("articleexists")
        existing = kwargs["data"]["text"]
        if outcome == "lost-response":
            raise ConnectionError("accepted request lost its response")
        return {"edit": {"result": "Success"}}

    def answer(params):
        return immutable_response(
            params, immutable_record(text=existing, revision=8)
        )

    monkeypatch.setattr(wbi_helpers, "mediawiki_api_call_helper", send)
    client = ApiClient(answer)
    client._wbi = object()
    client._login = SimpleNamespace(get_edit_token=lambda: "fictional-token")
    page = client.create_page_once(GENERATION_PAGE, "this invocation", "test")
    assert page.text == (
        "other invocation" if outcome == "competing" else "this invocation"
    )
    assert len(sent) == 1
    assert sent[0]["data"]["createonly"] == "1"
    assert sent[0]["max_retries"] == 1
    assert sent[0]["retry_after"] == 0


ALL_PROPS = "info|sitelinks|aliases|labels|descriptions|claims|datatype"
TERMS = ("labels", "descriptions", "aliases")


class EntityClient(ApiClient):
    """A client of an instance with no generation or recovery page and no
    vocabulary, which holds `entities`, id -> entity data, None for an
    entity that is listed but served missing. It answers `wbgetentities`
    as Wikibase does: only the props asked for, terms only in the
    languages asked for, and each id under its own key."""

    def __init__(self, entities):
        super().__init__(self.wbgetentities)
        self.entities = entities

    def immutable_page(self, title):
        return None

    def page_titles(self, prefix):
        return []

    def list_entity_ids(self):
        return sorted(self.entities)

    def vocabulary(self):
        return {}, {}

    def wbgetentities(self, params):
        assert params["action"] == "wbgetentities"
        props = params.get("props", ALL_PROPS).split("|")
        languages = params.get("languages")
        served = {}
        for entity_id in params["ids"].split("|"):
            entity = self.entities[entity_id]
            if entity is None:
                served[entity_id] = {"id": entity_id, "missing": ""}
                continue
            data = {"type": entity["type"], "id": entity_id, "lastrevid": 9}
            for key in props:
                if key not in entity:
                    continue
                value = copy.deepcopy(entity[key])
                if languages is not None and key in TERMS:
                    value = {
                        language: terms
                        for language, terms in value.items()
                        if language in languages.split("|")
                    }
                data[key] = value
            served[entity_id] = data
        return {"entities": served}


def served_marker(**changes):
    """The smoke-test marker item as an instance serves it."""
    return {
        "type": "item",
        "labels": {"en": {"language": "en", "value": SMOKE_LABEL}},
        "descriptions": {"en": {"language": "en", "value": SMOKE_DESCRIPTION}},
        "aliases": {},
        "claims": {},
        "sitelinks": {},
        **changes,
    }


def served_person():
    """Leopold Löbig as an instance serves an ingested person."""
    return {
        "type": "item",
        "labels": {"en": {"language": "en", "value": "Leopold Löbig"}},
        "descriptions": {},
        "aliases": {},
        "claims": {
            "P1": [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "property": "P1",
                        "datavalue": {
                            "value": {"entity-type": "item", "id": "Q1"},
                            "type": "wikibase-entityid",
                        },
                    },
                    "type": "statement",
                    "rank": "normal",
                }
            ]
        },
        "sitelinks": {},
    }


def requested(client):
    """The ids of each `wbgetentities` request the client made."""
    return [call["ids"].split("|") for call in client.calls]


def test_generation_reads_many_marker_items_fifty_a_request():
    markers = {f"Q{1000 + n}": served_marker() for n in range(120)}
    client = EntityClient(markers)

    assert CreationRegistry(client).check_generation() is None
    batches = requested(client)
    assert [len(batch) for batch in batches] == [50, 50, 20]
    assert sorted(qid for batch in batches for qid in batch) == sorted(markers)


def test_generation_refuses_at_the_first_item_that_is_not_a_marker():
    entities = {f"Q{1000 + n}": served_marker() for n in range(120)}
    entities["Q1000"] = served_person()
    client = EntityClient(entities)

    with pytest.raises(CreationRefused):
        CreationRegistry(client).check_generation()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "entity",
    [
        served_marker(
            labels={
                "en": {"language": "en", "value": SMOKE_LABEL},
                "de": {"language": "de", "value": "Rauchtest-Objekt"},
            }
        ),
        served_marker(
            aliases={"de": [{"language": "de", "value": "Rauchtest-Objekt"}]}
        ),
        None,
    ],
    ids=["German label", "German alias", "missing"],
)
def test_generation_refuses_an_item_read_as_more_than_the_marker(entity):
    client = EntityClient({"Q1000": served_marker(), "Q1001": entity})

    with pytest.raises(CreationRefused):
        CreationRegistry(client).check_generation()


def test_generation_refuses_a_property_outside_the_vocabulary_unread():
    client = EntityClient(
        {
            "Q1000": served_marker(),
            "P900": served_marker(type="property", datatype="string"),
        }
    )

    with pytest.raises(CreationRefused):
        CreationRegistry(client).check_generation()
    assert client.calls == []
