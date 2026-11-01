"""Ingestion registration admits retained code before any graph effect."""

import copy

import pytest

from tests.unit.test_registrar import (
    IMAGE,
    PORTRAIT,
    SHOP,
    FakeWiki,
    deck_file,
    recorded,
    register,
    scan,
)
from yizkr.core.instance import KEPT_STATE_PAGE
from yizkr.core.write_recovery import GENERATION_PAGE
from yizkr.pipeline import rundir
from yizkr.pipeline.registrar import (
    Registrar,
    ensure_self_typed,
    register_artifact,
    register_derived_media,
)


def retained_dirty(wiki, monkeypatch):
    wiki.create_page_once(
        KEPT_STATE_PAGE, "Fictional retained target", "fixture: retain target"
    )
    observed = []

    def code_version(*, fresh=False):
        observed.append(fresh)
        return {"commit": "a" * 40, "dirty": True}

    monkeypatch.setattr(rundir, "code_version", code_version)
    return observed


def unchanged(wiki):
    return copy.deepcopy(
        (wiki.entities, wiki._recovery_pages, wiki.media.requests, wiki.writes)
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_artifact_refuses_before_generation_upload_alias_or_fragment(
    tmp_path, monkeypatch, existing, direct
):
    wiki = FakeWiki()
    path = scan(tmp_path, PORTRAIT)
    if existing:
        register(wiki, path)
    else:
        del wiki._recovery_pages[GENERATION_PAGE]
    observed = retained_dirty(wiki, monkeypatch)
    before = unchanged(wiki)
    registration = (
        Registrar(wiki).register_artifact
        if direct
        else lambda *args, **kwargs: register_artifact(wiki, *args, **kwargs)
    )
    with pytest.raises(ValueError, match="verified clean software"):
        registration(path, "photograph", "Studio portrait, renamed", pages=2)
    assert unchanged(wiki) == before
    assert observed == [True]


@pytest.mark.parametrize("direct", [False, True])
def test_embedded_registration_refuses_before_upload_or_recovery_mutation(
    tmp_path, monkeypatch, direct
):
    wiki = FakeWiki()
    deck = deck_file(tmp_path, "family-firm")
    register(wiki, deck, SHOP, artifact_type="presentation")
    record, directory = recorded(
        tmp_path, deck, SHOP, [("shop-front.png", IMAGE, [1])]
    )
    observed = retained_dirty(wiki, monkeypatch)
    before = unchanged(wiki)
    with pytest.raises(ValueError, match="verified clean software"):
        if direct:
            Registrar(wiki).register_derived_media(record, directory)
        else:
            register_derived_media(wiki, record, directory)
    assert unchanged(wiki) == before
    assert observed == [True]


def test_self_typing_allows_dry_run_but_refuses_retained_mutation(monkeypatch):
    wiki = FakeWiki()
    qid = wiki.item("Fictional extraction run")
    observed = retained_dirty(wiki, monkeypatch)
    before = unchanged(wiki)
    assert ensure_self_typed(
        wiki, qid, "extraction_run", summary="fictional test", dry_run=True
    )
    assert observed == []
    with pytest.raises(ValueError, match="verified clean software"):
        ensure_self_typed(
            wiki, qid, "extraction_run", summary="fictional test"
        )
    assert unchanged(wiki) == before
    assert observed == [True]
