"""Deployment smoke: API up, item creation, WDQS round-trip."""

import pytest

from yizkr.smoke import run_smoke

pytestmark = pytest.mark.live


def test_smoke_roundtrip(config):
    qid = run_smoke(config, timeout=300, log=lambda *_: None)
    assert qid.startswith("Q")
