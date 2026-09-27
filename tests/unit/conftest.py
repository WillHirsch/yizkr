"""Fixtures shared by the offline unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_claude_config(monkeypatch, tmp_path_factory):
    """Point the claude CLI's config directory at one that does not exist,
    so no unit test reads the developer's own Claude Code login or global
    config (a claude-code client checks the global config when it starts
    and before every call). Tests that need one set their own."""
    missing = tmp_path_factory.getbasetemp() / "no-claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(missing))
