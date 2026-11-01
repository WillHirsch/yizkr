"""Retained admission checks code before an application or transition."""

from types import SimpleNamespace

import pytest

from yizkr.core import instance
from yizkr.pipeline import rundir


class Target:
    def __init__(self, *, kept=False, unavailable=False):
        self.config = SimpleNamespace(concept_uri="https://fictional.invalid")
        self.kept = kept
        self.unavailable = unavailable
        self.writes = []

    def api_get(self, params):
        if self.unavailable:
            raise ValueError("fictional target cannot be inspected")
        page = {"title": instance.KEPT_STATE_PAGE}
        if not self.kept:
            page["missing"] = ""
        return {"query": {"pages": {"1": page}}}

    def edit_page(self, title, text, summary):
        self.writes.append((title, text, summary))


def observe(monkeypatch, code):
    observations = []

    def version(*, fresh=False):
        observations.append(fresh)
        return dict(code)

    monkeypatch.setattr(rundir, "code_version", version)
    return observations


@pytest.mark.parametrize(
    "code",
    [
        {"commit": None, "dirty": False},
        {"commit": "abc1234", "dirty": False},
        {"commit": "g" * 40, "dirty": False},
        {"commit": "a" * 40, "dirty": True},
        {"commit": "a" * 40, "dirty": None},
    ],
)
def test_retained_or_transition_refuses_unknown_or_dirty_before_effects(
    tmp_path, monkeypatch, code
):
    calls = observe(monkeypatch, code)
    target = Target(kept=True)
    with pytest.raises(ValueError, match="retained operation requires"):
        instance.require_retained_code(target, root=tmp_path)
    target.kept = False
    with pytest.raises(ValueError, match="retained operation requires"):
        instance.mark_kept(target, "fictional curator", root=tmp_path)
    assert calls == [True, True]
    assert target.writes == []
    assert not instance.kept_path(target.config, tmp_path).exists()


def test_disposable_admission_does_not_restrict_development_code(
    tmp_path, monkeypatch
):
    calls = observe(monkeypatch, {"commit": None, "dirty": True})
    assert instance.require_retained_code(Target(), root=tmp_path) is None
    assert calls == []


@pytest.mark.parametrize("marker", ["host", "server"])
def test_either_retained_marker_requires_clean_code(
    tmp_path, monkeypatch, marker
):
    code = {"commit": "a" * 40, "dirty": False}
    calls = observe(monkeypatch, code)
    target = Target(kept=marker == "server", unavailable=marker == "host")
    if marker == "host":
        path = instance.kept_path(target.config, tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            "the host marker remains authoritative even if damaged"
        )
    assert instance.require_retained_code(target, root=tmp_path) == code
    assert calls == [True]
    assert target.writes == []


def test_unreadable_retention_state_cannot_be_assumed_disposable(tmp_path):
    with pytest.raises(ValueError, match="cannot be inspected"):
        instance.require_retained_code(Target(unavailable=True), root=tmp_path)


def test_later_admission_reobserves_code_after_the_run_started(
    tmp_path, monkeypatch
):
    code = {"commit": "a" * 40, "dirty": False}
    calls = observe(monkeypatch, code)
    target = Target(kept=True)
    assert instance.require_retained_code(target, root=tmp_path) == code
    code["dirty"] = True
    with pytest.raises(ValueError, match="verified clean"):
        instance.require_retained_code(target, root=tmp_path)
    assert calls == [True, True]
