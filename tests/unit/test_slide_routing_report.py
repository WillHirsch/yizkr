"""Saved slide decisions remain local detail and safe aggregate counts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit import test_run_report as fixture
from yizkr.pipeline import report
from yizkr.pipeline.document_state import slugify


def saved(tmp_path):
    run = fixture.record(tmp_path / "run")
    name = fixture.FILES[fixture.DECK][0]
    path = run / slugify(Path(name).stem) / "transcription.json"
    reading = json.loads(path.read_text())
    reading["pages"] = [
        {"page": 1, "images": [], "provenance": {"method": "vision"}},
        {"page": 2, "images": [], "provenance": {"method": "slide-xml"}},
    ]
    plan = {
        "policy": "layout-v1",
        "positions": [1],
        "slides": [
            {
                "position": 1,
                "part_id": "slide:one",
                "route": "vision",
                "reason": "spatial-text-layout",
                "limitations": [],
            },
            {
                "position": 2,
                "part_id": "slide:two",
                "route": "slide-xml",
                "reason": "mixed-source",
                "limitations": ["mixed-source-attribution"],
            },
        ],
    }
    reading["provenance"] = {
        "metadata": {"dependencies": {"slide_routing": plan}}
    }

    def build():
        path.write_text(json.dumps(reading))
        return report.build(run, now=fixture.NOW)

    return reading, plan, build


@pytest.mark.parametrize("policy", ["layout-v1", "layout-v2"])
def test_saved_routing_has_local_detail_and_aggregate_counts(tmp_path, policy):
    _, plan, build = saved(tmp_path)
    plan["policy"] = policy
    made = build()
    local = fixture.document(made, fixture.DECK)["transcription"][
        "slide_routing"
    ]
    assert local == {"status": "recorded", **plan}
    assert made["aggregate"]["documents"]["slide_routing"] == {
        "recorded_documents": 1,
        "unavailable_documents": 0,
        "invalid_documents": 0,
        "by_route": {"slide-xml": 1, "vision": 1},
        "by_reason": {"mixed-source": 1, "spatial-text-layout": 1},
        "limitations": {"mixed-source-attribution": 1},
    }
    local_html = fixture.drilled(made, fixture.DECK)
    assert "Slide 1: vision (spatial-text-layout)" in local_html
    assert "Slide 2: slide-xml (mixed-source)" in local_html
    aggregate = report.render_markdown(made["aggregate"])
    assert "PPTX layout routing: 1 documents recorded" in aggregate
    assert "mixed-source-attribution 1" in aggregate
    assert "slide:one" not in aggregate and "slide:two" not in aggregate
    assert fixture.leaks(aggregate) == []


def test_recorded_native_plan_is_distinct_from_unavailable_metadata(tmp_path):
    reading, plan, build = saved(tmp_path)
    plan["positions"] = []
    plan["slides"][0].update(route="slide-xml", reason="no-layout-trigger")
    reading["pages"][0]["provenance"]["method"] = "slide-xml"
    made = build()
    counts = made["aggregate"]["documents"]["slide_routing"]
    assert counts["recorded_documents"] == 1
    assert counts["by_route"] == {"slide-xml": 2}
    reading.pop("provenance")
    made = build()
    counts = made["aggregate"]["documents"]["slide_routing"]
    assert counts["recorded_documents"] == 0
    assert counts["unavailable_documents"] == 1
    assert counts["by_route"] == {}
    assert "Slide layout routing: unavailable" in fixture.drilled(
        made, fixture.DECK
    )


@pytest.mark.parametrize(
    "damage",
    [
        "shape",
        "policy",
        "duplicates",
        "missing",
        "route",
        "selection",
        "labels",
    ],
)
def test_malformed_or_inconsistent_plan_does_not_invent_counts(
    tmp_path, damage
):
    reading, plan, build = saved(tmp_path)
    if damage == "shape":
        reading["provenance"]["metadata"]["dependencies"]["slide_routing"] = []
    elif damage == "policy":
        plan["policy"] = "unrecognized"
    elif damage == "duplicates":
        plan["slides"][1]["position"] = 1
    elif damage == "missing":
        plan["slides"].pop()
    elif damage == "route":
        reading["pages"][0]["provenance"]["method"] = "slide-xml"
    elif damage == "selection":
        plan["positions"] = [True]
    else:
        plan["slides"][0]["limitations"] = {"drawings": 1}
    made = build()
    local = fixture.document(made, fixture.DECK)["transcription"][
        "slide_routing"
    ]
    assert local == {"status": "invalid"}
    counts = made["aggregate"]["documents"]["slide_routing"]
    assert counts["invalid_documents"] == 1
    assert (
        counts["by_route"]
        == counts["by_reason"]
        == counts["limitations"]
        == {}
    )
    assert "Slide layout routing: invalid" in fixture.drilled(
        made, fixture.DECK
    )


def test_unknown_labels_and_source_part_names_stay_out_of_aggregates(tmp_path):
    _, plan, build = saved(tmp_path)
    for slide in plan["slides"]:
        slide["reason"] = "Hutmacherei zum Blauen Hut"
        slide["limitations"] = ["Hildegard Laudler", "Hildegard Laudler"]
        slide["part_id"] = "Hildegard Laudler"
    made = build()
    counts = made["aggregate"]["documents"]["slide_routing"]
    assert counts["by_reason"] == {report.OTHER: 2}
    assert counts["limitations"] == {report.OTHER: 2}
    assert fixture.leaks(report.render_markdown(made["aggregate"])) == []
    assert fixture.leaks(json.dumps(made["aggregate"])) == []
    assert "Hildegard Laudler" in json.dumps(
        fixture.document(made, fixture.DECK)
    )


@pytest.mark.parametrize("positions", [[2, 3], [2, 1]])
def test_matching_pages_and_plan_still_need_complete_physical_order(
    tmp_path, positions
):
    reading, plan, build = saved(tmp_path)
    for position, page, decision in zip(
        positions, reading["pages"], plan["slides"], strict=True
    ):
        page["page"] = decision["position"] = position
    plan["positions"] = [positions[0]]
    made = build()
    counts = made["aggregate"]["documents"]["slide_routing"]
    assert counts["invalid_documents"] == 1
    assert counts["recorded_documents"] == 0
    assert counts["by_route"] == {}


def test_visual_attention_uses_only_its_fixed_aggregate_label():
    assert (
        report._attention_label(
            "slide visual reading unsupported: slide 2 (Hildegard Laudler)"
        )
        == "slide visual reading unsupported"
    )
