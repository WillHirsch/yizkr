"""Deployment smoke: API up, item creation, WDQS round-trip."""

import pytest

from yizkr.smoke import run_smoke

pytestmark = pytest.mark.live


def test_smoke_roundtrip(config):
    qid = run_smoke(config, timeout=300, log=lambda *_: None)
    assert qid.startswith("Q")


def test_bootstrap_is_idempotent(client):
    """Second bootstrap run must be a pure no-op (labels resolve, nothing
    created or updated)."""
    from yizkr.model.bootstrap import bootstrap_instance
    from yizkr.model.schema import load_manifest

    manifest = load_manifest()
    first = bootstrap_instance(client, manifest, log=lambda *_: None)
    client._vocabulary = None  # drop cache; second run sees fresh state
    second = bootstrap_instance(client, manifest, log=lambda *_: None)
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["statements_added"] == 0
    assert (
        second["unchanged"] == first["created"] + first["unchanged"] + first["updated"]
    )
