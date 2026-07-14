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
    assert summary["inconsistencies"] >= 1


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


def test_sources_hyperlink_to_artifact_pages_with_downloads(site):
    out, _ = site
    person = (out / "people/Q77.html").read_text()
    assert '"../artifacts/Q' in person  # source labels are links
    # the GEDCOM artifact page exists, offers the raw file, lists claims
    artifact = (out / "artifacts/Q229.html").read_text()
    assert "download the original" in artifact
    assert "What this artifact asserts" in artifact


def test_inconsistencies_suppress_recorded_containment(site):
    out, _ = site
    text = (out / "inconsistencies.html").read_text()
    # Shrubbery Maternity Home is recorded as located in High Wycombe:
    # a difference of resolution, not an inconsistency
    assert "Shrubbery" not in text
    # genuine disagreements remain, with linked sources
    assert "Guildford" in text
    assert 'href="artifacts/Q' in text
