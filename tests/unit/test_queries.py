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
    # the recorded containment the disagreement comparison reads (#266)
    "place-containment",
    "person-timeline",
    "places-map",
    "photo-gallery",
    "unreferenced-statements",
    "artifact-assertions",
    "under-witnessed-statements",
    "kinship",
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
    def test_expected_queries_present(self, templates):
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

    def test_artifact_assertions_say_where_each_statement_is_cited(
        self, templates
    ):
        # a row for each statement and reference block, with the block's
        # locators and excerpt, so a statement citing one artifact twice
        # gives rows that say where (#351); a fragment folds into its whole
        q = templates["artifact-assertions"]
        query = "\n".join(
            line for line in q.splitlines() if not line.startswith("#")
        )
        projection = set(re.findall(r"\?(\w+)", query.split("WHERE", 1)[0]))
        assert projection >= {
            "artifact",
            "source",
            "statement",
            "reference",
            "precision",
            "page",
            "printedPage",
            "region",
            "excerpt",
            "artifactLabel",
            "entity",
            "entityLabel",
            "propertyLabel",
            "value",
            "valueLabel",
            "sourceLabel",
        }
        assert "?statement prov:wasDerivedFrom ?reference ." in q
        for key, name in (
            ("page_number", "page"),
            ("printed_page_number", "printedPage"),
            ("relative_position_within_image", "region"),
            ("transcription_excerpt", "excerpt"),
        ):
            assert f"?reference pr:{{P:{key}}} ?{name} ." in q
        assert "BIND(COALESCE(?whole, ?source) AS ?artifact)" in q
        # the fold is optional, and only for a fragment part of a whole
        marker = "?source wdt:{P:instance_of} wd:{Q:artifact_fragment}"
        guard = group_at(q, marker)
        assert "wdt:{P:part_of} ?whole" in guard
        assert q[: q.index(guard)].rstrip().endswith("OPTIONAL")
        assert "GROUP_CONCAT" not in q

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
    """The flagship R2 view (ADR-0002), fetched by two flat queries and
    compared in Python (ADR-0033): disagreement-report.rq lists the
    attributed values of the compared properties, place-containment.rq the
    recorded containment, and yizkr.disagreement compares them
    (tests/unit/test_disagreement.py). The published page lists the values
    side by side, not pairs. tests/integration/test_live_disagreement.py
    pins the report's rows live."""

    @pytest.fixture
    def q(self, templates) -> str:
        return templates["disagreement-report"]

    @pytest.fixture
    def containment(self, templates) -> str:
        return templates["place-containment"]

    def test_it_fetches_every_property_the_comparison_compares(self, q):
        from yizkr.disagreement import COMPARED, DATED, SINGLE_VALUED

        (fetched,) = values_blocks(q, "property")
        assert fetched == COMPARED
        # the single-valued properties, an event's own dates among them
        # (#136), a document's date of making (#584), and the two that
        # accumulate but are dated
        assert sorted(DATED) == ["residence", "spouse"]
        assert {
            "birth_name",
            "point_in_time",
            "start_time",
            "inception",
        } <= SINGLE_VALUED

    def test_accumulating_names_are_never_fetched(self, q):
        # married names, nicknames, official names and appellations
        # accumulate over a life (MODELING.md "Contradictions")
        (fetched,) = values_blocks(q, "property")
        assert not fetched & {
            "married_name",
            "nickname",
            "official_name",
            "referred_to_as",
        }

    def test_it_lists_statements_and_pairs_none(self, q):
        # flat and linear: one statement per row, no pairing, grouping or
        # path walked (the comparison pairs them in Python)
        for pairing in ("?stA", "?stB", "GROUP BY", "HAVING", "NOT EXISTS"):
            assert pairing not in q, pairing
        assert re.search(r"\)[*+]", q) is None
        # the only filters pick a label's language
        filters = re.findall(r"FILTER\s*\((.*)", q)
        assert filters
        assert all(f.startswith("LANG(") for f in filters), filters

    def test_every_statement_is_listed_with_its_rank(self, q):
        # rank is an overlay the comparison reads (rule 4): nothing
        # filters on it, and no truthy triple, which holds best-rank values
        # only, stands in for a statement
        projection = q.split("WHERE", 1)[0]
        assert "?rank" in projection
        assert "?statement ?ps ?value ;" in q
        assert "wikibase:rank ?rank" in q
        for rank in ("DeprecatedRank", "PreferredRank", "BestRank"):
            assert rank not in q
        (fetched,) = values_blocks(q, "property")
        for key in fetched:
            assert f"wdt:{{P:{key}}}" not in q, key

    def test_a_time_comes_with_its_precision_range_and_dates(self, q):
        # rule 5: the stated precision and the earliest and latest dates
        # travel with a value, and a residence's or a marriage's start and
        # end with the bounds of each (and a point in time, which the
        # comparison leaves out)
        assert "?valueNode wikibase:timePrecision ?precision" in q
        for key, name in (
            ("earliest_date", "earliest"),
            ("latest_date", "latest"),
            ("latest_start_date", "latestStart"),
            ("earliest_end_date", "earliestEnd"),
            ("start_time", "start"),
            ("end_time", "end"),
            ("point_in_time", "pointInTime"),
        ):
            link = f"wd:{{P:{key}}} wikibase:qualifierValue ?{name}Link ."
            assert link in q, key
            lookup = group_at(q, f"?statement ?{name}Link ?{name}Node")
            assert f"wikibase:timeValue ?{name} ;" in lookup
            assert f"wikibase:timePrecision ?{name}Precision" in lookup
            # the qualifier is bound once, before the statements, so the
            # lookup joins on the statement alone
            assert q.index(link) < q.index("?entity ?p ?statement")
            assert q[: q.index(lookup)].rstrip().endswith("OPTIONAL")

    def test_every_stated_in_source_comes_with_its_artifact(self, q):
        lookup = group_at(q, "?statement prov:wasDerivedFrom ?reference")
        assert "?reference pr:{P:stated_in} ?source ." in lookup
        # optional: a statement with no stated-in source is listed, and the
        # comparison leaves it out
        assert q[: q.index(lookup)].rstrip().endswith("OPTIONAL")
        # a page fragment resolves to the artifact it is part of; the
        # fragment and label lookups sit inside the source's own group,
        # where a missing source cannot bind them to any fragment or label
        assert "wd:{Q:artifact_fragment}" in lookup
        assert "wdt:{P:part_of} ?whole" in lookup
        assert "BIND(COALESCE(?whole, ?source) AS ?artifact)" in lookup
        assert "?source rdfs:label ?sourceLabel" in lookup

    def test_an_item_value_is_labelled_through_its_merge_redirect(self, q):
        same_as = "<http://www.w3.org/2002/07/owl#sameAs>"
        assert f"?value {same_as}?/rdfs:label ?valueLabel" in q

    def test_projection_carries_what_the_comparison_reads(self, q):
        from yizkr.disagreement import TIME_COLUMNS

        projection = set(re.findall(r"\?(\w+)", q.split("WHERE", 1)[0]))
        read = {
            "entity",
            "entityLabel",
            "property",
            "statement",
            "rank",
            "value",
            "valueLabel",
            "precision",
            "source",
            "sourceLabel",
            "artifact",
        }
        for column in TIME_COLUMNS.values():
            read |= {column, f"{column}Precision"}
        assert read <= projection
        assert "propertyLabel" in projection  # for the published page

    def test_containment_lists_location_part_of_and_redirects(
        self, containment
    ):
        links = re.findall(r"VALUES\s+\?link\s*" + VALUES_BODY, containment)
        assert len(links) == 2
        assert placeholder_keys(links[0])[0] == {"location", "part_of"}
        assert links[1].strip() == "<http://www.w3.org/2002/07/owl#sameAs>"
        assert "?item ?link ?target ." in containment
        # every edge, flat: the comparison walks the paths, and the only
        # filters pick a label's language
        assert re.search(r"\)[*+]|GROUP BY", containment) is None
        filters = re.findall(r"FILTER\s*\((.*)", containment)
        assert all(f.startswith("LANG(") for f in filters), filters
        projection = containment.split("WHERE", 1)[0]
        for column in ("?item", "?link", "?target"):
            assert column in projection

    def test_containment_edges_carry_rank_and_whether_a_source_states_them(
        self, containment
    ):
        # ADR-0038 §5: each edge is a source's claim, so the comparison
        # reads its rank and its stated-in source; no truthy triple, which
        # holds best-rank values only, stands in for a statement, and no
        # filter on rank drops one before the comparison reads it
        projection = containment.split("WHERE", 1)[0]
        assert "?rank" in projection and "?stated" in projection
        assert "?item ?p ?statement ." in containment
        assert "?statement ?ps ?target ;" in containment
        assert "wikibase:rank ?rank ." in containment
        for key in ("location", "part_of"):
            assert f"wdt:{{P:{key}}}" not in containment, key
        for rank in ("DeprecatedRank", "PreferredRank", "BestRank"):
            assert rank not in containment
        # at any time (owner question 2): no qualifier, time or exclusion
        # narrows the edges the query lists
        assert (
            re.search(r"\bpqv?:|timeValue|NOT EXISTS|MINUS", containment)
            is None
        )
        stated = group_at(containment, "?statement prov:wasDerivedFrom")
        assert "?reference pr:{P:stated_in} ?source ." in stated
        assert (
            containment[: containment.index(stated)]
            .rstrip()
            .endswith("BIND(EXISTS")
        )
        assert containment[containment.index(stated) :].startswith(
            stated + " AS ?stated)"
        )


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
