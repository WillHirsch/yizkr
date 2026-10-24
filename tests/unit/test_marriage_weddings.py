"""Offline tests for when a marriage gets its wedding (#656, ADR-0036).

The extraction prompts ask for a marriage's wedding where the source
dates or places the marriage's start, as a stated divorce gets its
divorce, and for none where the source only names the marriage; the run
report counts the extractions' marriages by their weddings, from the
batches' structure alone. Every example comes from the fictional
universe (docs/EXAMPLES.md): the 1984 tree's "= (1942) Florence
Faxridge" and its undated "= Hildegard".
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.test_run_report import (
    CERT,
    FILES,
    NOW,
    RUN_ID,
    TREE,
    claim,
    document,
    leaks,
    record,
)
from yizkr.claims import ArtifactRef, ClaimBatch, validate_against_manifest
from yizkr.pipeline import report
from yizkr.pipeline.chunking import CHUNK_PROMPT, ENTITY_MANIFEST_PROMPT
from yizkr.pipeline.extract import EXTRACTION_PROMPT
from yizkr.pipeline.orchestrate import slugify


def flat(text: str) -> str:
    return " ".join(text.split())


MANIFEST = flat(ENTITY_MANIFEST_PROMPT)
CHUNK = flat(CHUNK_PROMPT)
SINGLE = flat(EXTRACTION_PROMPT)


class TestTheManifestRule:
    """The manifest's marriage rule says when a marriage gets its
    wedding entry, with the 1984 tree's two lines as its example."""

    RULE = (
        "A marriage whose start the source dates or places, at any "
        "precision, gets its wedding entry, as a stated divorce gets its "
        "divorce. A marriage the source only names, a partnership, or an "
        "observation (a directory or census that lists the couple as "
        "married, with no start) gets no wedding entry: in the 1984 tree, "
        '"= (1942) Florence Faxridge" gives a marriage and its 1942 '
        'wedding, and the undated "= Hildegard" gives the marriage alone;'
    )

    def test_the_rule_closes_the_marriage_rule(self):
        assert (
            "`divorce` or `annulment` that ended it are events of their "
            "own. " + self.RULE + " - a birth or a death is never an entity"
        ) in MANIFEST

    def test_it_is_said_once(self):
        assert MANIFEST.count("gets its wedding entry") == 1

    def test_its_example_is_the_universe_s_tree(self):
        tree = flat(
            next(s for s in u.SOURCES if s.key == "family-tree-1984").text
        )
        assert "= (1942) Florence Faxridge" in tree
        assert "= Hildegard" in tree
        assert "= (1942) Hildegard" not in tree


class TestTheChunkRule:
    """The chunk call gives the listed wedding its date and place, and
    the marriage its `started_by`."""

    def test_the_wedding_bullet_says_it(self):
        assert (
            "The couple take part in the union, not in its wedding. Where "
            "the manifest lists a marriage's wedding, give the marriage its "
            "`started_by` and the wedding the date and place the source "
            "gives the marriage's start; * a divorce or an annulment that "
            "ends it is an event too"
        ) in CHUNK


class TestTheSingleCallRule:
    def test_the_marriage_rule_says_it(self):
        assert (
            "of any kind. A marriage whose "
            "start the source dates or places, at any precision, gets its "
            "wedding, as a stated divorce gets its divorce. A marriage the "
            "source only names, a partnership, or an observation (a "
            "directory or census that lists the couple as married, with no "
            "start) gets no wedding."
        ) in SINGLE


# -- the count --------------------------------------------------------------

SCAN = ArtifactRef.from_bytes(b"fictional scan: the 1984 family tree")


def _entity(local, label, kind, **more):
    return {"id": local, "label": label, "kind": kind, **more}


def _source(excerpt):
    return {
        "artifact": {"kind": "artifact", "sha1": SCAN.sha1},
        "transcription_excerpt": {"text": excerpt, "language": "en"},
    }


def _link(subject, prop, target, excerpt):
    return {
        "subject": {"kind": "local", "local_id": subject},
        "property": prop,
        "value": {
            "type": "item",
            "value": {"kind": "local", "local_id": target},
        },
        "source": _source(excerpt),
    }


def _year(subject, year, excerpt):
    return {
        "subject": {"kind": "local", "local_id": subject},
        "property": "point_in_time",
        "value": {"type": "time", "year": year, "precision": "year"},
        "source": _source(excerpt),
    }


def tree_batch() -> dict:
    """A batch built from the universe's couples, varied to cover each
    case: three marriages started by a dated wedding (Gustav and
    Florence, 1942; Leopold and Agnes, 1947, later divorced; Eugen and
    Cäcilie, 1859), one by a wedding the source places and does not date
    (Rudolf and Nina, in the Soviet Union), and two with no wedding
    (Sebastian and Hildegard; Felix and Ida, a spouse the source only
    names); Felix and Ida's partnership too, and a marriage not
    extracted. The three counts differ, so no two can be swapped
    unseen."""
    entities = [
        _entity("e1", "Marriage of Gustav and Florence", "marriage"),
        _entity("e2", "Wedding of Gustav and Florence", "wedding"),
        _entity("e3", "Marriage of Leopold and Agnes", "marriage"),
        _entity("e4", "Wedding of Leopold and Agnes", "civil_wedding"),
        _entity("e5", "Divorce of Leopold and Agnes", "divorce"),
        _entity("e6", "Marriage of Sebastian and Hildegard", "marriage"),
        _entity("e7", "Marriage of Rudolf and Nina", "marriage"),
        _entity("e8", "Wedding of Rudolf and Nina", "wedding"),
        _entity("e9", "Soviet Union", "place"),
        _entity("e10", "Partnership of Felix and Ida", "partnership"),
        _entity("e11", "Wedding of Felix and Ida", "wedding"),
        _entity("e12", "Marriage of Eugen and Cäcilie", "marriage"),
        _entity("e20", "Wedding of Eugen and Cäcilie", "wedding"),
        _entity("e21", "Marriage of Felix and Ida", "marriage"),
        _entity(
            "e22", "Marriage of Leopold and Agnes", "marriage", extract=False
        ),
    ]
    claims = [
        _link("e1", "started_by", "e2", "= (1942) Florence Faxridge"),
        _year("e2", 1942, "= (1942) Florence Faxridge"),
        _link("e3", "started_by", "e4", "= (1947) Agnes Nertler"),
        _year("e4", 1947, "= (1947) Agnes Nertler"),
        _link("e3", "ended_by", "e5", "div. 1957"),
        _year("e5", 1957, "div. 1957"),
        _link("e7", "started_by", "e8", "= Nina Sartina, in the Soviet Union"),
        _link("e8", "location", "e9", "in the Soviet Union"),
        # a partnership is no marriage, whatever it is started by
        _link("e10", "started_by", "e11", "Felix = Ida"),
        _year("e11", 1913, "Felix = Ida"),
        # a dated event that no marriage is started by dates none
        _link("e6", "ended_by", "e5", "= Hildegard"),
        _link("e12", "started_by", "e20", "= (1859) Cäcilie 'Cilli' Tarle"),
        _year("e20", 1859, "= (1859) Cäcilie 'Cilli' Tarle"),
    ]
    return {
        "schema_version": 1,
        "extraction_run": {
            "model_identifier": "fake-model",
            "pipeline_version": "0.1.0",
            "started_at": "2026-10-24T09:00:00Z",
        },
        "entities": entities,
        "claims": claims,
    }


class TestTheCount:
    def test_the_batch_is_one_extraction_can_give(self):
        problems = validate_against_manifest(
            ClaimBatch.model_validate(tree_batch())
        )
        assert problems == []

    def test_marriages_by_their_weddings(self):
        view = tree_batch()
        assert report._marriages(view["entities"], view["claims"]) == Counter(
            {"dated_wedding": 3, "undated_wedding": 1, "no_wedding": 2}
        )

    def test_a_started_by_to_no_wedding_gives_none(self):
        """`started_by` to an entity of another kind, to an entity the
        batch does not list, or to an item outside it gives no wedding,
        and nor does a wedding linked by another property."""
        view = tree_batch()
        entities = view["entities"] + [
            _entity("e13", "Gustav's emigration", "migration")
        ]
        claims = [
            c for c in view["claims"] if c["subject"]["local_id"] != "e1"
        ]
        claims += [
            _link("e1", "started_by", "e13", "Gustl 1919"),
            _link("e1", "started_by", "e99", "Gustl 1919"),
            # a wedding linked by any other property starts nothing
            _link("e1", "ended_by", "e2", "Gustl 1919"),
            {
                **_link("e1", "started_by", "e2", "Gustl 1919"),
                "value": {
                    "type": "item",
                    "value": {"kind": "qid", "qid": "Q7"},
                },
            },
        ]
        assert report._marriages(entities, claims)["no_wedding"] == 3

    def test_one_dated_wedding_of_two_dates_the_marriage(self):
        """A marriage started by two ceremonies, one of them dated."""
        view = tree_batch()
        view["entities"].append(
            _entity("e14", "Church wedding of Leopold and Agnes", "wedding")
        )
        view["claims"].append(
            _link("e3", "started_by", "e14", "= (1947) Agnes Nertler")
        )
        counted = report._marriages(view["entities"], view["claims"])
        assert counted["dated_wedding"] == 3

    def test_a_malformed_batch_counts_what_it_can(self):
        entities = [
            _entity("e1", "Marriage of Gustav and Florence", "marriage"),
            {"id": 7, "kind": "marriage"},
            {"kind": "marriage"},
        ]
        claims = [
            {"subject": "e1", "property": "started_by", "value": "e2"},
            {"subject": {"kind": "local", "local_id": "e1"}, "value": None},
        ]
        assert report._marriages(entities, claims) == Counter(
            {"no_wedding": 1}
        )


@pytest.fixture
def counted(tmp_path) -> dict:
    """The fictional run with the tree's unions in the certificate's
    extraction batch, and a dated marriage in the GEDCOM batch."""
    path = record(tmp_path / RUN_ID)
    directory = path / slugify(Path(FILES[CERT][0]).stem)
    view = json.loads((directory / "batch.json").read_text())
    unions = tree_batch()
    view["entities"] += unions["entities"]
    view["claims"] += unions["claims"]
    (directory / "batch.json").write_text(json.dumps(view))
    gedcom = path / slugify(Path(FILES[TREE][0]).stem)
    tree = json.loads((gedcom / "batch.json").read_text())
    tree["entities"] += [
        {
            "id": "F1",
            "label": "Marriage of Eugen and Cäcilie",
            "kind": "marriage",
        }
    ]
    tree["claims"].append(claim("F1", "started_by", "1859", TREE))
    (gedcom / "batch.json").write_text(json.dumps(tree))
    return report.build(path, now=NOW)


class TestTheReportCounts:
    def test_the_aggregate_counts_extractions_alone(self, counted):
        marriages = counted["aggregate"]["content"]["marriages"]
        # each completed extraction counts, with marriages or none; the
        # GEDCOM batch is no extraction
        assert marriages["extractions"] == sum(
            d["batch"] is not None and d["batch"]["stage"] == "extraction"
            for d in counted["documents"]
        )
        assert {k: v for k, v in marriages.items() if k != "extractions"} == {
            "total": 6,
            "dated_wedding": 3,
            "undated_wedding": 1,
            "no_wedding": 2,
        }

    def test_each_document_gives_its_own(self, counted):
        assert document(counted, CERT)["batch"]["marriages"] == {
            "dated_wedding": 3,
            "undated_wedding": 1,
            "no_wedding": 2,
        }
        assert document(counted, TREE)["batch"]["marriages"] is None

    def test_report_md_states_it(self, counted):
        text = report.render_markdown(counted["aggregate"])
        assert "### Marriages and their weddings" in text
        assert (
            "6 marriages in "
            f"{counted['aggregate']['content']['marriages']['extractions']} "
            "extractions: 3 started by a dated wedding, 1 by an undated "
            "one, 2 with no wedding."
        ) in text
        assert leaks(text) == []
        assert "Florence" not in text

    def test_the_drill_down_gives_the_document_s_own(self, counted):
        page = report.render_html(counted)
        assert (
            "Marriages by their weddings: dated_wedding 3, no_wedding 2, "
            "undated_wedding 1"
        ) in page
