"""The assignment-path benchmark's cohorts and its regression baseline.

The benchmark (scripts/assignment_cost.py, docs/TESTING.md "Assignment path
cost") measures generated fictional cohorts against an in-memory Wikibase.
These checks keep its cohorts reviewable, as the path requires, and keep
the request counts of its smallest size, the fixed regression scenario,
as recorded: a change that sends more or fewer requests, or writes other
records, shows here, and records its new counts with the procedure in
docs/TESTING.md. No wall-clock time is read or asserted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from scripts import assignment_cost as benchmark

from tests.fixtures import example_universe as universe
from tests.fixtures.assignment_cohort import (
    REGRESSION,
    Scale,
    generate,
    saved_products,
)
from tests.fixtures.fake_wikibase import (
    FakeWikibase,
    UnsupportedRequest,
    config,
    serving,
)
from yizkr.core.corpus import ExclusionList
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.claim_review import locate_support
from yizkr.pipeline.document_products import load_document_products
from yizkr.pipeline.review_mentions import observe_mentions

BASELINE = (
    Path(__file__).parents[1] / "fixtures" / "assignment_cost_baseline.json"
)
SCALE = Scale(sources=5, mentions=3, shared=1 / 3, claims=4)


def test_a_scale_draws_one_cohort_and_a_larger_one_extends_it():
    cohort = generate(SCALE)
    assert generate(SCALE) == cohort
    smaller = generate(Scale(sources=3, mentions=3, shared=1 / 3, claims=4))
    assert smaller.sources == cohort.sources[:3]
    # the people already in the graph are drawn after every register's
    assert generate(replace(SCALE, existing=7)).sources == cohort.sources
    seen = set()
    for source in cohort.sources:
        shared = {m.person for m in source.mentions if m.shared}
        assert shared <= seen
        assert len(shared) == (1 if source.number > 1 else 0)
        assert len({m.person for m in source.mentions}) == 3
        seen |= {m.person for m in source.mentions}
        assert len(source.claims) == 4
    assert set(cohort.people) == seen
    assert len(cohort.people) == 3 + 4 * 2
    assert (cohort.identities, cohort.claims) == (15, 20)


def test_a_cohort_draws_its_names_from_the_universe_tables():
    cohort = generate(
        Scale(sources=8, mentions=3, shared=0.5, claims=0, existing=40)
    )
    starts, ends = universe.COHORT_SURNAME_SYLLABLES
    surnames = {start + end for start in starts for end in ends}
    universe_words = {
        word
        for person in universe.PEOPLE
        for name in person.names
        for word in name.text.split()
    }
    # no surname the tables can give is a word of a universe person's name
    assert surnames.isdisjoint(universe_words)
    people = (*cohort.people, *cohort.existing)
    assert len(cohort.existing) == 40
    assert len({person.name for person in people}) == len(people)
    for person in people:
        assert person.given in universe.COHORT_GIVEN_NAMES
        assert person.surname in surnames
        first, last = universe.COHORT_BIRTH_YEARS
        assert first <= person.born <= last
        assert person.born + 20 <= person.died <= 1945
    towns = {
        place.label
        for place in universe.PLACES
        if place.key in universe.COHORT_TOWNS
    }
    assert {source.town for source in cohort.sources} <= towns


def test_no_full_name_is_drawn_twice_until_the_tables_run_out():
    starts, ends = universe.COHORT_SURNAME_SYLLABLES
    names = len(universe.COHORT_GIVEN_NAMES) * len(starts) * len(ends)
    every = Scale(
        sources=1, mentions=1, shared=0, claims=0, existing=names - 1
    )
    cohort = generate(every)
    people = (*cohort.people, *cohort.existing)
    assert len({person.name for person in people}) == len(people) == names
    with pytest.raises(ValueError):
        generate(replace(every, existing=names))


def test_every_mention_is_anchored_and_every_claim_located(tmp_path):
    """The path refuses an unanchored mention or an unlocated claim, so
    each register's saved views must give neither."""
    cohort = generate(SCALE)
    for saved in saved_products(cohort, tmp_path):
        products = load_document_products(
            saved.path, saved.document_dir, exclusions=ExclusionList()
        )
        observations = observe_mentions(
            saved.sha1, products.batch, products.reading
        )
        assert [o.local_id for o in observations] == [
            mention.local_id for mention in saved.source.mentions
        ]
        for observation in observations:
            assert observation.anchor is not None
            assert observation.limitations == []
        assert len(products.batch.claims) == len(saved.source.claims)
        for claim, authored in zip(
            products.batch.claims, saved.source.claims, strict=True
        ):
            assert claim.subject.local_id == authored.mention.local_id
            assert claim.value.year == authored.year
            locate_support(claim.source, products.reading)


def test_the_fake_refuses_a_request_it_does_not_model():
    wiki = FakeWikibase()
    with serving(wiki), pytest.raises(UnsupportedRequest):
        WikiClient(config()).api_get({"action": "parse", "page": "Main"})


def _changes(recorded: list[dict], counted: list[dict]) -> str:
    lines = []
    for index, (before, after) in enumerate(
        zip(recorded, counted, strict=False)
    ):
        for part in ("requests", "writes"):
            kinds = sorted(set(before[part]) | set(after[part]))
            moved = [
                f"{kind} {before[part].get(kind, 0)} -> "
                f"{after[part].get(kind, 0)}"
                for kind in kinds
                if before[part].get(kind) != after[part].get(kind)
            ]
            if moved:
                lines.append(
                    f"operation {index} ({after['operation']}, source "
                    f"{after['source']}) {part}: " + ", ".join(moved)
                )
    if len(recorded) != len(counted):
        lines.append(f"{len(recorded)} operations -> {len(counted)}")
    return (
        "the regression scenario's counts changed:\n"
        + "\n".join(lines)
        + "\nIf the change is intended, record the new counts as "
        "docs/TESTING.md, Assignment path cost, describes."
    )


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Undo the settings file the benchmark points the environment at
    when it measures, once the test ends."""
    monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "unused.env"))


def test_the_regression_scenario_keeps_its_recorded_request_counts(
    tmp_path, isolated
):
    """A performance baseline, read through the benchmark's own report:
    `--write-baseline` records each operation's server requests by kind,
    review-store calls and records written, for the smallest size, as the
    checked-in baseline holds them."""
    path = tmp_path / "baseline.json"
    assert benchmark.main(["--write-baseline", str(path)]) == 0
    recorded = json.loads(BASELINE.read_text())
    counted = json.loads(path.read_text())
    assert recorded["scale"] == counted["scale"] == asdict(REGRESSION)
    assert counted["operations"] == recorded["operations"], _changes(
        recorded["operations"], counted["operations"]
    )


def _size(sources: int, **categories: float) -> dict:
    per = {"count": 1, "categories": categories}
    return {
        "scale": {"sources": sources},
        "per_identity": per,
        "per_claim": per,
    }


def test_a_comparison_gives_every_category_of_each_comparable_size():
    baseline = {
        "format": 1,
        "sizes": {
            "small": _size(2, **{"writes": 10, "revision reads": 4}),
            "medium": _size(10, writes=5),
        },
    }
    candidate = {
        "format": 1,
        "sizes": {
            "small": _size(
                2, **{"writes": 15, "revision reads": 2, "other requests": 3}
            ),
            "medium": _size(20, writes=5),
            "large": _size(50, writes=1),
        },
    }
    lines = benchmark.compare(baseline, candidate).splitlines()
    for key in ("per_identity", "per_claim"):
        (line,) = [
            line for line in lines if line.startswith(f"- small, {key}")
        ]
        ratios = dict(
            part.rsplit(" ", 1) for part in line.split(": ", 1)[1].split("; ")
        )
        assert ratios == {
            "writes": "1.50",
            "revision reads": "0.50",
            "other requests": "-",
        }
    # another scale, or a size the baseline lacks, gives no ratio
    for name in ("medium", "large"):
        assert (
            len([line for line in lines if line.startswith(f"- {name}")]) == 1
        )
        assert not any(line.startswith(f"- {name},") for line in lines)


@pytest.mark.parametrize(
    "report", [{"format": 2, "sizes": {}}, {"format": 1}, [], "baseline"]
)
def test_a_comparison_refuses_anything_but_a_benchmark_report(report):
    if report == "baseline":
        report = json.loads(BASELINE.read_text())
    with pytest.raises(ValueError):
        benchmark.compare(report, {"format": 1, "sizes": {}})


def test_an_existing_output_is_refused_before_anything_is_measured(
    tmp_path, isolated, capsys
):
    output = tmp_path / "report.json"
    output.write_text("earlier evidence\n")
    argv = ["--size", "small", "--output", str(output)]
    assert benchmark.main(argv) == 2
    assert output.read_text() == "earlier evidence\n"
    assert "measured" not in capsys.readouterr().out


def test_a_failure_once_measuring_has_begun_keeps_its_traceback(
    tmp_path, isolated, capsys
):
    """Only the checks before measuring end in exit 2. The report's
    folder is missing here, which shows only when the report is written."""
    output = tmp_path / "missing" / "report.json"
    with pytest.raises(FileNotFoundError):
        benchmark.main(["--size", "small", "--output", str(output)])
    assert "measured" in capsys.readouterr().out


@pytest.mark.parametrize("baseline", [None, "not json", "regression"])
def test_a_baseline_that_is_not_a_report_is_refused_before_measuring(
    tmp_path, isolated, capsys, baseline
):
    path = tmp_path / "baseline.json"
    if baseline == "regression":
        path.write_text(BASELINE.read_text())
    elif baseline is not None:
        path.write_text(baseline)
    output = tmp_path / "report.json"
    argv = [
        "--size",
        "small",
        "--baseline",
        str(path),
        "--output",
        str(output),
    ]
    assert benchmark.main(argv) == 2
    assert not output.exists()
    assert "measured" not in capsys.readouterr().out
