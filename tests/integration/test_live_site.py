"""Live static-site build (frontend ladder rung 2): the generator runs
real queries against the running stack and the flagship plural-display
behavior holds on real data."""

from __future__ import annotations

import pytest

from yizkr.staticsite import publish_site

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def site(config, tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    summary = publish_site(config, out)
    return out, summary


def test_summary_counts_are_plausible(site):
    _, summary = site
    assert summary["people"] >= 100
    assert summary["statements"] >= 500
    assert summary["disagreements"] >= 1


def test_person_pages_show_coexisting_values_with_sources(site):
    out, _ = site
    # Christine Horton (Q77): sources disagree on her birthplace; the
    # page must show BOTH values, each labeled — never silently pick one
    text = (out / "people/Q77.html").read_text()
    assert "Sheffield" in text
    assert "Guildford" in text
    assert "GEDCOM" in text  # the source labels are visible

    index = (out / "index.html").read_text()
    assert 'href="people/Q77.html"' in index


def test_gallery_and_timeline_render(site):
    out, _ = site
    gallery = (out / "gallery.html").read_text()
    assert "<img src=" in gallery and "Special:FilePath" in gallery
    timeline = (out / "timeline.html").read_text()
    assert 'class="entry"' in timeline
    assert (out / "timeline.json").exists()
