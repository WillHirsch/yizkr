"""Each claim's excerpt is looked for in its transcription, a counted flag
first (#580, ADR-0023 as amended).

A claim's `transcription_excerpt` is compared with the transcription it
quotes by the shared comparison of core/verbatim.py (#569): folded, with
whitespace and line breaks collapsed, a hyphen at a line's end joined,
whole words only. A miss is never a reject: the claim stays in its
batch, the miss is counted in the extraction's coverage (`excerpts`), the
`excerpt-not-in-source` anomaly check raises it, the run report counts
misses by property and the run diff compares them. The local
`excerpts.json` keeps each miss's claim id and the transcription line
nearest to its excerpt.

Every text is the fictional universe's (docs/EXAMPLES.md): Gustav
Löbig's birth register extract, Flörburg 1919, his father the hatter
Sebastian, his mother Hildegard née Laudler, the family home at
Brunnenring 35.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.unit.test_chunking import ScriptedLlm, ndjson
from tests.unit.test_run_report import (  # noqa: F401
    CERT,
    FILES,
    NOW,
    POSTCARD,
    document,
    drilled,
    run,
)
from yizkr.claims import ArtifactRef, Claim
from yizkr.core.verbatim import Transcript
from yizkr.pipeline import anomalies, report, rundiff
from yizkr.pipeline.anomalies import batch_findings, default_config
from yizkr.pipeline.chunking import ChunkedClaimExtractor, excerpt_audit
from yizkr.pipeline.orchestrate import slugify
from yizkr.pipeline.report import OTHER, build, render_markdown
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.transcribe import Transcription

EXTRACT = "\n".join(
    [
        "Auszug aus dem Geburtsregister",  # 1
        "Standesamt Flörburg, Nr. 12/1919",  # 2
        "Gustav Löbig wurde am 7. Januar 1919",  # 3
        "in Flörburg, Brunnen-",  # 4
        "ring 35, geboren, Sohn des Hutmachers",  # 5
        "Sebastian Löbig und der Hildegard",  # 6
        "geb. Laudler.",  # 7
    ]
)
EXTRACT_REF = ArtifactRef.from_bytes(EXTRACT.encode())

VERBATIM = "Gustav Löbig wurde am 7. Januar 1919"
# lines 6 and 7
ACROSS_A_LINE_BREAK = "Sebastian Löbig und der Hildegard geb. Laudler"
# lines 4 and 5, the street broken at a hyphen for layout alone
HYPHENATED_LINE_END = "in Flörburg, Brunnenring 35"
# the date as another source gives it, not as this one does
MISQUOTE = "am 1. Juli 1919"
# words the extract does not hold, no word of them on any line
INVENTED = "Taufpate Eugen Tarle"


def claim(excerpt: str | None, prop: str = "date_of_birth") -> dict:
    value = (
        {
            "type": "time",
            "year": 1919,
            "month": 1,
            "day": 7,
            "precision": "day",
        }
        if prop == "date_of_birth"
        else {
            "type": "monolingual",
            "value": {"text": "Gustl", "language": "de"},
        }
    )
    source: dict = {"artifact": EXTRACT_REF.model_dump()}
    if excerpt is not None:
        source["transcription_excerpt"] = {"text": excerpt, "language": "de"}
    return {
        "subject": {
            "kind": "local",
            "local_id": "e1",
            "label": "Gustav Löbig",
            "instance_of": "human",
        },
        "property": prop,
        "value": value,
        "source": source,
    }


def claims(*excerpts: str | None) -> list[Claim]:
    return [Claim.model_validate(claim(text)) for text in excerpts]


def audit(*excerpts: str | None) -> dict:
    return excerpt_audit(claims(*excerpts), Transcript(EXTRACT))


class TestTheComparison:
    """The five cases the issue names, each through excerpt_audit."""

    @pytest.mark.parametrize(
        "excerpt", [VERBATIM, ACROSS_A_LINE_BREAK, HYPHENATED_LINE_END]
    )
    def test_an_excerpt_the_transcription_holds_is_no_miss(self, excerpt):
        assert audit(excerpt) == {
            "counts": {
                "checked": 1,
                "without_excerpt": 0,
                "not_in_source": 0,
                "not_in_source_by_property": {},
            },
            "missing": [],
        }

    def test_a_misquote_is_a_miss_kept_with_its_nearest_line(self):
        (given,) = claims(MISQUOTE)
        found = excerpt_audit([given], Transcript(EXTRACT))
        assert found["counts"]["not_in_source"] == 1
        assert found["counts"]["not_in_source_by_property"] == {
            "date_of_birth": 1
        }
        assert found["missing"] == [
            {
                "claim_id": claim_id(given),
                "property": "date_of_birth",
                "excerpt": MISQUOTE,
                # "am" and "1919" are on line 3
                "nearest_line": 3,
                "nearest_text": "Gustav Löbig wurde am 7. Januar 1919",
            }
        ]

    def test_an_invented_excerpt_is_a_miss_with_no_nearest_line(self):
        (missed,) = audit(INVENTED)["missing"]
        assert (missed["nearest_line"], missed["nearest_text"]) == (None, None)

    def test_a_claim_with_no_excerpt_is_counted_apart(self):
        assert audit(None, " ", VERBATIM)["counts"] == {
            "checked": 1,
            "without_excerpt": 2,
            "not_in_source": 0,
            "not_in_source_by_property": {},
        }

    def test_misses_are_counted_by_property(self):
        given = [
            Claim.model_validate(claim(MISQUOTE)),
            Claim.model_validate(claim(INVENTED)),
            Claim.model_validate(claim("Gustl", "nickname")),
            Claim.model_validate(claim(VERBATIM, "nickname")),
        ]
        counts = excerpt_audit(given, Transcript(EXTRACT))["counts"]
        assert counts["checked"] == 4
        assert counts["not_in_source"] == 3
        assert counts["not_in_source_by_property"] == {
            "date_of_birth": 2,
            "nickname": 1,
        }


# Forms a careful reader would accept as quoting the extract, each with
# whether the comparison counts it a miss. The owner's question, whether a
# miss should become a reject, reads these: 5 of the 8 are counted.
NEAR_MISSES = [
    # folded away: no miss
    ("case", "gustav löbig WURDE am 7. januar 1919", False),
    ("spacing", "Gustav  Löbig wurde\tam 7. Januar 1919", False),
    ("accents dropped", "in Florburg, Brunnenring 35", False),
    # not forgiven: a miss
    ("ellipsis", "Gustav Löbig … 1919", True),
    ("comma dropped", "in Flörburg Brunnenring 35", True),
    ("layout hyphen copied", "Brunnen- ring 35", True),
    ("umlaut transliterated", "in Floerburg, Brunnenring 35", True),
    ("abbreviation expanded", "Hildegard geboren Laudler", True),
]


class TestLegitimateNearMisses:
    @pytest.mark.parametrize(
        ("form", "excerpt", "missed"),
        NEAR_MISSES,
        ids=[form for form, _, _ in NEAR_MISSES],
    )
    def test_each_form(self, form, excerpt, missed):
        assert bool(audit(excerpt)["missing"]) is missed

    def test_the_expected_rate(self):
        counts = audit(*(excerpt for _, excerpt, _ in NEAR_MISSES))["counts"]
        assert (counts["not_in_source"], counts["checked"]) == (5, 8)


class TestTheExtraction:
    """The chunked extractor checks the claims it keeps against the
    transcription it reads: an excerpt document's extraction reads its
    own."""

    @pytest.fixture
    def extracted(self, tmp_path):
        manifest = {
            "entities": [
                {
                    "id": "e1",
                    "label": "Gustav Löbig",
                    "kind": "human",
                    "mention_lines": [3],
                }
            ]
        }
        llm = ScriptedLlm(
            [
                json.dumps(manifest),
                ndjson(
                    [
                        claim(VERBATIM),
                        claim(HYPHENATED_LINE_END),
                        claim(MISQUOTE),
                        claim(INVENTED, "nickname"),
                        claim(None),
                    ]
                ),
            ]
        )
        state = tmp_path / "state"
        batch = ChunkedClaimExtractor(llm, state_dir=state).extract(
            Transcription(artifact=EXTRACT_REF, text=EXTRACT, engine="test"),
            EXTRACT_REF,
        )
        return batch, state

    def test_a_miss_is_kept_in_the_batch(self, extracted):
        batch, state = extracted
        excerpts = [
            None
            if found.source.transcription_excerpt is None
            else found.source.transcription_excerpt.text
            for found in batch.claims
        ]
        assert excerpts == [
            VERBATIM,
            HYPHENATED_LINE_END,
            MISQUOTE,
            INVENTED,
            None,
        ]
        assert not (state / "rejects.json").exists()

    def test_the_coverage_counts_and_quotes_nothing(self, extracted):
        _, state = extracted
        coverage = json.loads((state / "coverage.json").read_text())
        assert coverage["excerpts"] == {
            "checked": 4,
            "without_excerpt": 1,
            "not_in_source": 2,
            "not_in_source_by_property": {"date_of_birth": 1, "nickname": 1},
        }
        assert coverage["rejected_claims"] == 0
        assert "Juli" not in json.dumps(coverage)

    def test_the_local_audit_keeps_each_miss(self, extracted):
        batch, state = extracted
        kept = json.loads((state / "excerpts.json").read_text())
        assert [found["claim_id"] for found in kept] == [
            claim_id(batch.claims[2]),
            claim_id(batch.claims[3]),
        ]
        assert [found["nearest_line"] for found in kept] == [3, None]

    def test_no_miss_leaves_no_audit(self, tmp_path):
        state = tmp_path / "state"
        (state).mkdir()
        (state / "excerpts.json").write_text("[]")
        manifest = {
            "entities": [
                {"id": "e1", "label": "Gustav Löbig", "kind": "human"}
            ]
        }
        llm = ScriptedLlm([json.dumps(manifest), ndjson([claim(VERBATIM)])])
        ChunkedClaimExtractor(llm, state_dir=state).extract(
            Transcription(artifact=EXTRACT_REF, text=EXTRACT, engine="test"),
            EXTRACT_REF,
        )
        assert not (state / "excerpts.json").exists()
        coverage = json.loads((state / "coverage.json").read_text())
        assert coverage["excerpts"]["not_in_source"] == 0


def coverage(missing: int, checked: int) -> dict:
    return {
        "claims": checked,
        "rejected_claims": 0,
        "unread_objects": 0,
        "fallback": {},
        "excerpts": {
            "checked": checked,
            "without_excerpt": 0,
            "not_in_source": missing,
            "not_in_source_by_property": {"date_of_birth": missing},
        },
    }


def findings(config, found: dict | None) -> list:
    return [
        finding
        for finding in batch_findings(
            config,
            entities=1,
            claims=4,
            excerpts=[],
            language=None,
            letters=None,
            coverage=found,
        )
        if finding.check == "excerpt-not-in-source"
    ]


class TestTheAnomalyCheck:
    def test_its_threshold_is_in_anomalies_yaml(self):
        shipped = yaml.safe_load(anomalies.DEFAULTS.read_text())
        assert shipped["checks"]["excerpt-not-in-source"] == {
            "enabled": True,
            "max-share": 0.1,
            "min-count": 4,
        }

    def test_a_share_above_the_threshold_alerts(self):
        (finding,) = findings(default_config(), coverage(1, 4))
        assert (finding.value, finding.limit) == (0.25, 0.1)
        assert finding.fields == {"count": 1, "of": 4}

    @pytest.mark.parametrize(
        ("missing", "checked"),
        [(1, 10), (0, 4), (3, 3)],
        ids=["at the share", "none", "too few checked"],
    )
    def test_quiet(self, missing, checked):
        assert findings(default_config(), coverage(missing, checked)) == []

    def test_a_coverage_that_predates_the_count_is_quiet(self):
        older = coverage(4, 4)
        del older["excerpts"]
        assert findings(default_config(), older) == []

    def test_an_operator_can_turn_it_off(self, tmp_path):
        path = tmp_path / "anomalies.yaml"
        path.write_text(
            "checks:\n  excerpt-not-in-source:\n    enabled: false\n"
        )
        config = anomalies.load_config(path)
        assert findings(config, coverage(4, 4)) == []


def with_excerpts(directory: Path, sha1: str, counts: dict) -> None:
    """Give the fictional run's document an excerpt count in its
    coverage."""
    name, _ = FILES[sha1]
    path = (
        directory / slugify(Path(name).stem) / "extraction" / "coverage.json"
    )
    found = json.loads(path.read_text())
    found["excerpts"] = counts
    path.write_text(json.dumps(found))


class TestTheRunReport:
    @pytest.fixture
    def made(self, run):  # noqa: F811
        with_excerpts(
            run,
            CERT,
            {
                "checked": 3,
                "without_excerpt": 1,
                "not_in_source": 2,
                "not_in_source_by_property": {
                    "date_of_birth": 1,
                    "shoe_size": 1,
                },
            },
        )
        with_excerpts(
            run,
            POSTCARD,
            {
                "checked": 2,
                "without_excerpt": 0,
                "not_in_source": 0,
                "not_in_source_by_property": {},
            },
        )
        return build(run, now=NOW)

    def test_the_aggregate_counts_misses_by_property(self, made):
        assert made["aggregate"]["content"]["excerpts"] == {
            "extractions": 2,
            "checked": 5,
            "without_excerpt": 1,
            "not_in_source": 2,
            "documents": 1,
            "rate": 0.4,
            # a property the vocabulary does not know is folded
            "not_in_source_by_property": {"date_of_birth": 1, OTHER: 1},
            # the other extractions' coverage predates the count
            "absent": 3,
        }

    def test_the_markdown_gives_counts_alone(self, made):
        assert (
            "\n### Excerpts not in their source\n\n"
            "- Claims whose excerpt was looked for in their transcription: "
            "5, in 2 extractions; claims with no excerpt: 1\n"
            "- Excerpts the transcription does not hold: 2 (40.0% of those "
            "checked), in 1 documents; the claims are kept\n"
            "- Extractions whose coverage predates the count: 3\n\n"
            "| Property | Excerpts not in source |\n"
            "|---|---:|\n"
            "| date_of_birth | 1 |\n"
            "| other | 1 |\n"
        ) in render_markdown(made["aggregate"])

    def test_each_document_s_own_counts(self, made):
        assert document(made, CERT)["coverage"]["excerpts"] == {
            "checked": 3,
            "without_excerpt": 1,
            "not_in_source": 2,
            "by_property": {"date_of_birth": 1, "shoe_size": 1},
        }
        assert document(made, POSTCARD)["coverage"]["excerpts"] == {
            "checked": 2,
            "without_excerpt": 0,
            "not_in_source": 0,
            "by_property": {},
        }
        assert (
            "Excerpts not in the transcription: 2 of 3 checked, by "
            "property: date_of_birth 1, shoe_size 1; claims with no "
            "excerpt: 1"
        ) in drilled(made, CERT)

    def test_a_coverage_that_predates_the_count_is_absent(self, made):
        from tests.unit.test_run_report import DECK

        assert document(made, DECK)["coverage"]["excerpts"] is None
        assert (
            "Excerpts not in the transcription: not counted (its coverage "
            "gives no count of them)"
        ) in drilled(made, DECK)

    def test_the_run_diff_compares_them(self, made):
        before = json.loads(json.dumps(made["aggregate"]))
        before["content"]["excerpts"]["not_in_source"] = 5
        before["content"]["excerpts"]["not_in_source_by_property"] = {
            "date_of_birth": 5
        }
        rows = {
            row["figure"]: row
            for row in rundiff.figures(before, made["aggregate"])
        }
        assert rows["content.excerpts.not_in_source"]["delta"] == -3
        assert rows["content.excerpts.not_in_source_by_property.other"] == {
            "figure": "content.excerpts.not_in_source_by_property.other",
            "baseline": 0,
            "run": 1,
            "delta": 1,
        }
        older = json.loads(json.dumps(made["aggregate"]))
        del older["content"]["excerpts"]
        rows = {
            row["figure"]: row
            for row in rundiff.figures(older, made["aggregate"])
        }
        assert rows["content.excerpts.checked"]["delta"] is None


def test_a_count_that_is_no_count_reads_as_absent():
    """A coverage whose excerpt counts are no whole numbers of at least 0
    is not counted, never read as zero."""
    assert report._excerpts({"excerpts": {"checked": -1}}) is None
    assert (
        report._excerpts({"excerpts": {"checked": 2, "not_in_source": "1"}})
        is None
    )
    assert report._excerpts({"excerpts": []}) is None
    assert report._excerpts({}) is None
