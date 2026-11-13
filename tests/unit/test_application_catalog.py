"""Read-only application discovery exposes incomplete and orphan lineage."""

import json

import pytest

from tests.unit.test_assignment_writes import apply, approved, snapshot
from tests.unit.test_claim_review import case as case
from tests.unit.test_pipeline import FakeWiki
from yizkr.core.wiki import WikiPage
from yizkr.core.write_recovery import (
    GENERATION_PAGE,
    PREFIX,
    CreationRefused,
    CreationRegistry,
)


def application(case, tmp_path):
    result = apply(case, approved(case), tmp_path)
    plan = case.registry.application_plan(result.plan_id)
    titles = {
        title.rsplit("/", 1)[1]: title
        for title in case.wiki._recovery_pages
        if f"/applications/{result.plan_id}/" in title
    }
    return plan, titles


@pytest.mark.parametrize("state", ["plan", "intent", "receipt"])
def test_catalog_keeps_unapplied_and_completed_plans_without_writes(
    case, tmp_path, monkeypatch, state
):
    plan, titles = application(case, tmp_path)
    if state != "receipt":
        del case.wiki._recovery_pages[titles["receipt"]]
    if state == "plan":
        del case.wiki._recovery_pages[titles["intent"]]
    before = snapshot(case)

    def forbidden(*args, **kwargs):
        pytest.fail("application catalog attempted a mutation or graph read")

    monkeypatch.setattr(case.wiki, "create_page_once", forbidden)
    monkeypatch.setattr(case.wiki.wbi.item, "get", forbidden)
    assert case.registry.application_catalog() == (plan,)
    assert snapshot(case) == before


def test_catalog_accepts_an_empty_established_generation(case):
    before = snapshot(case)
    assert case.registry.application_catalog() == ()
    assert snapshot(case) == before


def test_catalog_does_not_establish_a_missing_generation():
    wiki = FakeWiki()
    wiki._recovery_pages.clear()
    with pytest.raises(CreationRefused, match="requires a generation"):
        CreationRegistry(wiki).application_catalog()
    assert not wiki._recovery_pages


@pytest.mark.parametrize("missing", ["plan", "intent"])
def test_catalog_refuses_orphan_lineage(case, tmp_path, missing):
    _, titles = application(case, tmp_path)
    del case.wiki._recovery_pages[titles[missing]]
    before = snapshot(case)
    with pytest.raises(CreationRefused, match=f"no cataloged {missing}"):
        case.registry.application_catalog()
    assert snapshot(case) == before


@pytest.mark.parametrize(
    "suffix",
    [
        "not-a-plan/plan",
        "a" * 64 + "/unknown",
        "a" * 64 + "/" + "b" * 64 + "/unknown",
        "A" * 64 + "/plan",
        "a" * 64 + "/plan/extra",
    ],
)
def test_catalog_refuses_unrecognized_record_paths(case, suffix):
    generation = case.registry.generation
    title = f"{PREFIX}{generation.generation}/applications/{suffix}"
    case.wiki._recovery_pages[title] = WikiPage(title, "{}", 1)
    before = snapshot(case)
    with pytest.raises(CreationRefused, match="unrecognized"):
        case.registry.application_catalog()
    assert snapshot(case) == before


def test_catalog_checks_claim_scope_of_every_lineage_record(case, tmp_path):
    plan, titles = application(case, tmp_path)
    original = case.wiki._recovery_pages[titles["intent"]]
    title = titles["intent"].replace(plan.claims[0].claim_id, "b" * 64)
    case.wiki._recovery_pages[title] = WikiPage(title, original.text, 1)
    with pytest.raises(CreationRefused, match="unknown claim"):
        case.registry.application_catalog()


@pytest.mark.parametrize("kind", ["plan", "intent", "receipt"])
def test_catalog_validates_present_record_contents(case, tmp_path, kind):
    _, titles = application(case, tmp_path)
    title = titles[kind]
    page = case.wiki._recovery_pages[title]
    data = json.loads(page.text)
    if kind == "plan":
        data["plan_id"] = "a" * 64
    elif kind == "intent":
        data["support"]["qid"] = "Q99999"
    else:
        data["intent_sha256"] = "a" * 64
    case.wiki._recovery_pages[title] = WikiPage(title, json.dumps(data), 1)
    before = snapshot(case)
    with pytest.raises(CreationRefused):
        case.registry.application_catalog()
    assert snapshot(case) == before


@pytest.mark.parametrize("kind", ["plan", "intent", "receipt"])
def test_catalog_refuses_an_enumerated_record_that_disappears(
    case, tmp_path, monkeypatch, kind
):
    _, titles = application(case, tmp_path)
    original = case.wiki.immutable_page

    def missing(title):
        return None if title == titles[kind] else original(title)

    monkeypatch.setattr(case.wiki, "immutable_page", missing)
    with pytest.raises(CreationRefused, match="missing"):
        case.registry.application_catalog()


def test_catalog_refuses_duplicate_enumeration(case, tmp_path, monkeypatch):
    application(case, tmp_path)
    original = case.wiki.page_titles

    def repeated(prefix):
        titles = original(prefix)
        return [*titles, titles[0]]

    monkeypatch.setattr(case.wiki, "page_titles", repeated)
    with pytest.raises(CreationRefused, match="duplicate"):
        case.registry.application_catalog()


def test_catalog_accepts_canonical_project_namespace_names(
    case, tmp_path, monkeypatch
):
    plan, _ = application(case, tmp_path)
    original = case.wiki.page_titles
    monkeypatch.setattr(
        case.wiki,
        "page_titles",
        lambda prefix: [
            title.replace("Project:", "Yizkr:", 1)
            for title in original(prefix)
        ],
    )
    assert case.registry.application_catalog() == (plan,)


def test_catalog_refuses_changed_record_inventory(case, tmp_path, monkeypatch):
    application(case, tmp_path)
    original = case.wiki.page_titles
    calls = 0

    def changed(prefix):
        nonlocal calls
        calls += 1
        titles = original(prefix)
        return titles if calls == 1 else titles[:-1]

    monkeypatch.setattr(case.wiki, "page_titles", changed)
    with pytest.raises(CreationRefused, match="catalog changed"):
        case.registry.application_catalog()


def test_catalog_rechecks_generation_after_reading_lineage(
    case, tmp_path, monkeypatch
):
    application(case, tmp_path)
    original = case.wiki.page_titles
    calls = 0

    def changed(prefix):
        nonlocal calls
        calls += 1
        titles = original(prefix)
        if calls == 2:
            page = case.wiki._recovery_pages[GENERATION_PAGE]
            data = json.loads(page.text)
            data["generation"] = "3" * 32
            case.wiki._recovery_pages[GENERATION_PAGE] = WikiPage(
                GENERATION_PAGE, json.dumps(data), 1
            )
        return titles

    monkeypatch.setattr(case.wiki, "page_titles", changed)
    with pytest.raises(CreationRefused, match="generation changed"):
        case.registry.application_catalog()
