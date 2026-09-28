"""Offline tests for the query library (pytest; no instance needed)."""

import re

import pytest
import yaml

from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import (
    description_of,
    index_wikitext,
    load_templates,
    page_wikitext,
    placeholder_keys,
    render,
    render_for_instance,
)

EXPECTED_QUERIES = {
    "disagreement-report",
    "person-timeline",
    "places-map",
    "photo-gallery",
    "unreferenced-statements",
    "artifact-assertions",
    "under-witnessed-statements",
}


@pytest.fixture(scope="module")
def templates() -> dict[str, str]:
    return load_templates()


@pytest.fixture(scope="module")
def vocabulary() -> tuple[dict, dict]:
    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


class TestLibrary:
    def test_roadmap_queries_present(self, templates):
        assert set(templates) >= EXPECTED_QUERIES

    def test_every_template_has_description(self, templates):
        for name, template in templates.items():
            assert description_of(template), f"{name} has no leading comment"

    def test_placeholders_reference_real_manifest_keys(self, templates):
        """Templates and model/manifest.yaml must not drift apart."""
        raw = yaml.safe_load(read_manifest_data())
        for name, template in templates.items():
            properties, items = placeholder_keys(template)
            unknown_p = properties - set(raw["properties"])
            unknown_q = items - set(raw["items"])
            assert not unknown_p, f"{name}: unknown property keys {unknown_p}"
            assert not unknown_q, f"{name}: unknown item keys {unknown_q}"

    def test_disagreement_report_uses_provenance(self, templates):
        assert "prov:wasDerivedFrom" in templates["disagreement-report"]
        assert "{P:stated_in}" in templates["disagreement-report"]

    def test_under_witnessed_query_keys_on_provenance(self, templates):
        # ADR-0023: reappraisal is a structural query — machine-written
        # (run reference) and under-witnessed (one source, no curator sibling)
        q = templates["under-witnessed-statements"]
        assert "{P:inferred_from_extraction_run}" in q
        assert "{P:stated_in}" in q
        assert "FILTER NOT EXISTS" in q
        # one row per statement (same-value statements that differ by a
        # qualifier are distinct assertions), and a time value's stated
        # precision travels with it (rule 5) — both live-validated in #23
        projection = q.split("WHERE", 1)[0]
        assert "?statement" in projection
        assert "?precision" in projection


# a VALUES body: text and {P:key} placeholders (braces nest one level)
VALUES_BODY = r"\{((?:[^{}]|\{[PQ]:\w+\})*)\}"


def values_blocks(template: str, variable: str) -> list[set[str]]:
    """The property keys of each ``VALUES ?variable { ... }`` block."""
    return [
        placeholder_keys(body)[0]
        for body in re.findall(
            rf"VALUES\s+\?{variable}\s*" + VALUES_BODY, template
        )
    ]


def values_pairs(template: str, first: str, second: str) -> set[tuple]:
    """The key pairs of a ``VALUES (?first ?second) { ... }`` block."""
    (body,) = re.findall(
        rf"VALUES\s*\(\s*\?{first}\s+\?{second}\s*\)\s*" + VALUES_BODY,
        template,
    )
    return set(re.findall(r"\(\s*wd:\{P:(\w+)\}\s+wd:\{P:(\w+)\}\s*\)", body))


def group_at(template: str, marker: str) -> str:
    """The ``{ ... }`` group that opens just before ``marker``."""
    start = template.rindex("{", 0, template.index(marker))
    depth = 0
    for index in range(start, len(template)):
        depth += {"{": 1, "}": -1}.get(template[index], 0)
        if depth == 0:
            return template[start : index + 1]
    raise ValueError(f"unbalanced group at {marker!r}")


class TestDisagreementReport:
    """The flagship R2 view (ADR-0002): values that different artifacts
    attribute, side by side. Coexistence is cross-source only (ADR-0023
    §4), rank is an optional overlay, and the rows are a resting state.
    tests/integration/test_live_disagreement.py pins the rows live."""

    @pytest.fixture
    def q(self, templates) -> str:
        return templates["disagreement-report"]

    @staticmethod
    def single_valued(q: str) -> set[str]:
        places, others = values_blocks(q, "property")
        return places | others

    def test_single_valued_properties_include_event_dates_and_birth_name(
        self, q
    ):
        assert self.single_valued(q) == {
            "date_of_birth",
            "date_of_death",
            "place_of_birth",
            "place_of_death",
            "father",
            "mother",
            "birth_name",
            # an event's own dates (#136)
            "point_in_time",
            "start_time",
            "end_time",
            "coordinate_location",
            "geonames_id",
            "whg_place_id",
        }

    def test_accumulating_properties_never_compare_values(self, q):
        # spouse and residence accumulate over a life (MODELING.md
        # "Contradictions"), and so do the other typed names
        assert not self.single_valued(q) & {
            "residence",
            "spouse",
            "married_name",
            "nickname",
            "official_name",
            "referred_to_as",
        }

    def test_one_residence_or_spouse_compares_its_start_and_end(self, q):
        # "arrived there in 1938 per one source, in 1939 per another"; a
        # point in time on a residence dates an observation, not the range
        assert values_pairs(q, "property", "qualifier") == {
            ("residence", "start_time"),
            ("residence", "end_time"),
            ("spouse", "start_time"),
            ("spouse", "end_time"),
        }

    def test_containment_is_checked_for_place_values_only(self, q):
        # a place inside the other is a difference of resolution; only the
        # place-valued pairs follow the containment path, starting from a
        # statement's own value so that the path is seeded by it rather
        # than walked over the whole graph (#136)
        places, _ = values_blocks(q, "property")
        assert places == {"place_of_birth", "place_of_death"}
        path = "wdt:{P:location}|wdt:{P:part_of}"
        place_arm = group_at(q, "VALUES ?property { wd:{P:place_of_birth}")
        assert place_arm.count(path) == q.count(path) == 4
        for side in "AB":
            assert f"?rawValue{side} (<http" in place_arm

    def test_only_pairs_from_different_artifacts_count(self, q):
        # a value needs a stated-in source, a page fragment counts as its
        # artifact, and a group's sources must not all be one artifact
        assert "FILTER(BOUND(?sourceA) && BOUND(?sourceB))" in q
        having = " ".join(q.split("HAVING", 1)[1].split("ORDER BY")[0].split())
        artifact = {
            side: f"STR(COALESCE(?whole{side}, ?source{side}))"
            for side in "AB"
        }
        # the sources are all one artifact exactly when the smallest and
        # largest artifact of each side are that same artifact
        assert having == (
            f"(MIN({artifact['A']}) != MAX({artifact['B']}) "
            f"|| MAX({artifact['A']}) != MIN({artifact['B']}))"
        )
        for side in "AB":
            lookup = group_at(
                q, f"?st{side} prov:wasDerivedFrom/pr:{{P:stated_in}}"
            )
            # the source is looked up in an OPTIONAL after the pair
            # sub-select: a required pattern beside a sub-select is
            # evaluated first, across every statement in the graph
            assert lookup.startswith("{") and q[
                : q.index(lookup)
            ].rstrip().endswith("OPTIONAL")
            # the fragment and label lookups sit inside it: at group level
            # they would bind a missing source to any fragment or label
            assert "wd:{Q:artifact_fragment}" in lookup
            assert f"?source{side}LabelRaw" in lookup

    def test_deprecated_values_are_out_and_preferred_is_never_needed(self, q):
        for side in "AB":
            assert f"OPTIONAL {{ ?st{side} wikibase:rank ?rank{side} . }}" in q
        assert (
            "FILTER(?rankA != wikibase:DeprecatedRank "
            "&& ?rankB != wikibase:DeprecatedRank)"
        ) in q
        # rule 4: no preference is read, and no best-rank shortcut hides
        # a normal value beside a preferred one
        assert "PreferredRank" not in q
        assert "BestRank" not in q
        for key in self.single_valued(q) | {"residence", "spouse"}:
            assert f"wdt:{{P:{key}}}" not in q, key

    def test_names_compare_as_text_and_case_variants_share_a_row(self, q):
        # the comparison key: a name's text without case or language tag,
        # an item through its merge redirect, any other value as written
        group_by = q.split("GROUP BY", 1)[1].split("HAVING", 1)[0]
        keys = {}
        for side in "AB":
            keys[side] = key = (
                f"IF(isLiteral(?rawValue{side}) && LANG(?rawValue{side}) "
                f'!= "", LCASE(STR(?rawValue{side})), '
                f"STR(COALESCE(?canonical{side}, ?rawValue{side})))"
            )
            # it orders each pair once, and it groups the rows
            assert q.count(key) == 2, side
            assert f"{key} AS ?key{side})" in group_by
            # spellings that share a key are one row: one spelling shown,
            # the sources of all of them listed
            assert (
                f"(MIN(COALESCE(?canonical{side}, ?rawValue{side})) "
                f"AS ?value{side})"
            ) in q
        # each pair of values once, in key order: never both orientations
        # (two dates stored alike are ordered by their spans and bounds)
        flat = " ".join(q.split())
        assert (
            f"FILTER({keys['A']} < {keys['B']} "
            "|| sameTerm(?rawValueA, ?rawValueB))"
        ) in flat

    def test_a_date_spans_its_precision_or_its_stated_range(self, q):
        # rule 5: a range a source states with earliest and latest dates
        # is its span, so "the 1930s, between 1930 and 1935" and 1937
        # disagree while "between 1938 and 1939" and 3 March 1939 agree
        for key in ("earliest_date", "latest_date"):
            assert f"wd:{{P:{key}}} wikibase:qualifierValue" in q
        for side in "AB":
            # a month, year, decade or century is a span; the units are
            # doubles, since the query service fails on integer constants
            # chosen by a nested IF in a sub-select
            unit = (
                f"IF(?precision{side} >= 11, 1e0, IF(?precision{side} = 10, "
                f"1e2, IF(?precision{side} = 9, 1e4, IF(?precision{side} = 8,"
                " 1e5, 1e6))))"
            )
            assert unit in q
            # a precision coarser than a century names no span
            assert f"BIND(?precision{side} >= 7 &&" in q
        assert not re.search(r"\b(?:100|10000|100000|1000000)\)", q)
        assert (
            "(?fromA <= ?fromB && ?toB <= ?toA) || "
            "(?fromB <= ?fromA && ?toA <= ?toB)"
        ) in q

    def test_dates_stored_alike_are_compared_through_their_ranges(self, q):
        # rule 5: "before 1920" and "after 1920" are both stored as 1920,
        # and 1930 and "the 1930s, between 1934 and 1936" as 1930, yet
        # their spans neither overlap nor nest (#136)
        same = "sameTerm(?rawValueA, ?rawValueB)"
        dated = "DATATYPE(?rawValueA) = <http://www.w3.org/2001/XMLSchema#dateTime>"
        # two statements of a single-valued date stored alike are paired
        single = " ".join(
            group_at(q, "# the other single-valued properties").split()
        )
        assert (
            f"FILTER(!{same} || (!sameTerm(?stA, ?stB) && {dated}))" in single
        )
        # places, and a residence's or spouse's start and end times, still
        # pair different values only
        for marker in ("# places:", "VALUES (?property ?qualifier)"):
            arm = " ".join(group_at(q, marker).split())
            assert f"FILTER(!{same})" in arm, marker
            assert "sameTerm(?stA" not in arm, marker
        # such a pair is kept once, in the order of its spans and then of
        # the bounds its statements state (which of them, at what
        # precision), and left out when both give the same span and bounds:
        # below a century no span is named, so the bounds still count there
        for side in "AB":
            assert (
                f"BIND(COALESCE(?earliestPrecision{side}, -1e0) * 1e2 "
                f"+ COALESCE(?latestPrecision{side}, -1e0) AS ?bounds{side})"
            ) in q
        flat = " ".join(q.split())
        assert (
            f"FILTER(!{same} || ?fromA < ?fromB || (?fromA = ?fromB && "
            "(?toA < ?toB || (?toA = ?toB && ?boundsA < ?boundsB))))"
        ) in flat

    def test_a_residence_or_spouse_time_beside_a_range_is_as_stated(self, q):
        # its earliest and latest dates may date either end of it, so when
        # either statement carries them, no difference of resolution
        # between their start or end times is suppressed
        assert (
            "!BOUND(?qualifier) || !(BOUND(?earliestA) || BOUND(?latestA) "
            "|| BOUND(?earliestB) || BOUND(?latestB))"
        ) in q

    def test_lookups_join_on_values_bound_by_triple_patterns(self, q):
        # a lookup joined on a BIND result makes the query service compare
        # every row with every label in the graph: labels join on the raw
        # value, through its merge redirect
        same_as = "<http://www.w3.org/2002/07/owl#sameAs>"
        for side in "AB":
            assert (
                f"?rawValue{side} {same_as}?/rdfs:label ?value{side}Label"
            ) in q
            assert f"?value{side} rdfs:label" not in q
        assert f"?sharedRawA {same_as}?/rdfs:label ?sharedValueLabel" in q
        assert "?sharedValue rdfs:label" not in q

    def test_projection_carries_what_the_site_renders(self, q):
        projection = q.split("WHERE", 1)[0]
        for column in (
            "?entityLabel",
            "?propertyLabel",
            "?sharedValueLabel",
            "?qualifierLabel",
            "?valueA",
            "?precisionA",
            "?earliestA",
            "?earliestPrecisionA",
            "?latestA",
            "?latestPrecisionA",
            "?sourcesA",
            "?valueB",
            "?precisionB",
            "?earliestB",
            "?earliestPrecisionB",
            "?latestB",
            "?latestPrecisionB",
            "?sourcesB",
        ):
            assert column in projection, column


class TestRendering:
    def test_render_leaves_no_placeholders(self, templates, vocabulary):
        properties, items = vocabulary
        for name, template in templates.items():
            rendered = render(template, properties, items)
            assert "{P:" not in rendered, name
            assert "{Q:" not in rendered, name

    def test_render_substitutes_ids(self, vocabulary):
        properties, items = vocabulary
        rendered = render(
            "?x wdt:{P:instance_of} wd:{Q:human} .", properties, items
        )
        assert rendered == (
            f"?x wdt:{properties['instance_of']} wd:{items['human']} ."
        )

    def test_unknown_key_fails_loudly(self, vocabulary):
        properties, items = vocabulary
        with pytest.raises(KeyError, match="shoe_size"):
            render("wdt:{P:shoe_size}", properties, items)

    def test_instance_render_declares_local_prefixes(
        self, templates, vocabulary
    ):
        """WDQS ships wikidata.org prefixes; queries must redeclare them
        for the local concept URI or silently match nothing."""
        properties, items = vocabulary
        rendered = render_for_instance(
            templates["person-timeline"],
            properties,
            items,
            "http://wikibase.example",
        )
        for prefix in ("wd:", "wdt:", "p:", "ps:", "pq:", "pr:"):
            assert f"PREFIX {prefix} <http://wikibase.example/" in rendered
        assert "PREFIX prov: <http://www.w3.org/ns/prov#>" in rendered


class TestWikitext:
    def test_page_has_run_link_and_highlight(self, templates, vocabulary):
        properties, items = vocabulary
        name = "unreferenced-statements"
        rendered = render(templates[name], properties, items)
        text = page_wikitext(
            name, templates[name], rendered, "http://query.example"
        )
        assert "http://query.example/#" in text
        assert "embed.html#" in text
        assert '<syntaxhighlight lang="sparql">' in text
        assert "do not edit by hand" in text

    def test_index_lists_every_query(self, templates):
        text = index_wikitext(templates)
        for name in EXPECTED_QUERIES:
            assert f"Project:Queries/{name}|{name}" in text
