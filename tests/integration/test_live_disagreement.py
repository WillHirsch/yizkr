"""Live validation of the disagreement report (ADR-0002, ADR-0023 §4).

A fictional fixture exercises every clause of the report's contract on the
running stack, then pins the report's rows for the fixture subjects:

* a contradiction between two artifacts is listed once, each value with
  the artifacts that state it and its stated precision, even when a
  curator prefers one of the values (nothing needs a preferred rank);
* two readings of one artifact are not a disagreement: a curator's
  correction beside the machine reading it corrects, and a page fragment
  beside the artifact it is part of;
* a deprecated value is left out;
* a date whose span lies inside another's is a refinement, not a
  disagreement. The span is the period the precision names (a bare year
  beside a day in it; the 19th century beside 1850) or the range the
  source states with earliest and latest dates ("between 1938 and 1939"
  beside 22 June 1938 and beside 3 March 1939, the universe's forms,
  docs/EXAMPLES.md). A stated range can make a pair a disagreement: "the
  1930s, between 1930 and 1935" and 1937 disagree, as do a year narrowed
  to March-June and 2 November of it (rule 5), and the report carries the
  range beside the value;
* an event's own date is compared, and so are the start times of one
  residence place and of one spouse, dated two ways; two residence
  observations at different points in time are not;
* birth names compare as text: spellings that differ only in letter case
  and language are one value, listed once with the sources of all of them,
  while nicknames accumulate and are never compared.

The whole graph is queried, and rows are filtered to the fixture subjects,
so unrelated data on the instance cannot make the assertions flaky. Like
every live test it writes fictional data to the one instance, so the
session that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from yizkr.claims import ClaimBatch
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import run_sparql, wait_for_query_service

pytestmark = pytest.mark.live


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


PERSON = local("disagreement-person", "Disagreement Fixture Person", "human")
SPOUSE = local("disagreement-spouse", "Disagreement Fixture Spouse", "human")
RANGES = local("disagreement-ranges", "Disagreement Fixture Ranges", "human")
EMIGRATION = local(
    "disagreement-emigration", "Disagreement Fixture Emigration", "migration"
)
SECOND_EMIGRATION = local(
    "disagreement-second-emigration",
    "Disagreement Fixture Second Emigration",
    "migration",
)
TOWN = local("disagreement-town", "Disagreement Fixture Town", "place")
OTHER_TOWN = local(
    "disagreement-other-town", "Disagreement Fixture Other Town", "place"
)
ROLES = {
    "disagreement-person": "person",
    "disagreement-spouse": "spouse",
    "disagreement-ranges": "ranges",
    "disagreement-emigration": "emigration",
    "disagreement-second-emigration": "second emigration",
    "disagreement-town": "town",
    "disagreement-other-town": "other town",
}
SUBJECTS = ("person", "spouse", "ranges", "emigration", "second emigration")


def date(
    year: int,
    month: int | None = None,
    day: int | None = None,
    precision: str | None = None,
) -> dict:
    value = {"type": "time", "year": year}
    if month:
        value["month"] = month
    if day:
        value["day"] = day
    if precision:
        value["precision"] = precision
    return value


def item(ref: dict) -> dict:
    return {"type": "item", "value": ref}


def text(value: str, language: str = "en") -> dict:
    return {
        "type": "monolingual",
        "value": {"text": value, "language": language},
    }


def claim(
    subject: dict,
    prop: str,
    value: dict,
    artifact_qid: str,
    qualifiers: tuple = (),
    curated: bool = False,
) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "qualifiers": [
            {"property": key, "value": qualifier}
            for key, qualifier in qualifiers
        ],
        "source": {
            "artifact": {"kind": "qid", "qid": artifact_qid},
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional disagreement fixture",
                "language": "en",
            },
        },
        "curated": curated,
    }


def batch(claims: list[dict]) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": claims,
        }
    )


def statements(client, subject: str, prop_key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": subject,
            "property": properties[prop_key],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


def set_rank(client, subject, prop_key, matches, rank: WikibaseRank) -> None:
    """A curator's rank change on one statement: an optional overlay
    (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    (statement,) = [
        s
        for s in entity.claims.get(properties[prop_key])
        if matches(s.mainsnak.datavalue["value"])
    ]
    if statement.rank != rank:
        statement.rank = rank
        entity.write(summary="integration test: curator rank on a fixture")


def between(start: dict, end: dict) -> tuple:
    """The earliest and latest dates a source gives a value."""
    return (("earliest_date", start), ("latest_date", end))


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture (idempotent: re-runs reuse and skip everything),
    apply the curator ranks, and wait until the query service holds the
    subjects' latest revisions. Returns (QIDs by role, source QIDs by
    letter)."""
    registered = {
        letter: register_artifact(
            client,
            media_path=None,
            artifact_type="certificate",
            title=f"Disagreement fixture source {letter}",
            description="written by tests/integration; safe to delete",
            pages=1 if letter == "A" else 0,
        )
        for letter in "ABC"
    }
    a, b, c = (registered[letter].artifact_qid for letter in "ABC")
    page_one_of_a = registered["A"].fragment_qids[1]
    machine = BatchWriter(client).write_batch(
        batch(
            [
                # the universe's contradiction: day and month transposed
                # (7 January per the register, 1 July per the
                # naturalisation), and a bare year that refines both
                claim(PERSON, "date_of_birth", date(1919, 1, 7), a),
                claim(PERSON, "date_of_birth", date(1919, 7, 1), b),
                claim(PERSON, "date_of_birth", date(1919), c),
                # the machine's reading, corrected below from the same
                # artifact
                claim(PERSON, "date_of_death", date(1970), a),
                # a page of an artifact beside the artifact itself
                claim(PERSON, "place_of_death", item(TOWN), page_one_of_a),
                claim(PERSON, "place_of_death", item(OTHER_TOWN), a),
                # the second value is deprecated below
                claim(PERSON, "place_of_birth", item(TOWN), a),
                claim(PERSON, "place_of_birth", item(OTHER_TOWN), b),
                # arrived there in 1939 per one source, in 1940 per another
                claim(
                    PERSON,
                    "residence",
                    item(TOWN),
                    a,
                    (("start_time", date(1939)),),
                ),
                claim(
                    PERSON,
                    "residence",
                    item(TOWN),
                    b,
                    (("start_time", date(1940)), ("end_time", date(1952))),
                ),
                # lived there in 1912 and in 1919: two observations
                claim(
                    PERSON,
                    "residence",
                    item(OTHER_TOWN),
                    a,
                    (("point_in_time", date(1912)),),
                ),
                claim(
                    PERSON,
                    "residence",
                    item(OTHER_TOWN),
                    b,
                    (("point_in_time", date(1919)),),
                ),
                # one birth name, written in two cases and languages
                claim(PERSON, "birth_name", text("Fixture Birth Name"), a),
                claim(
                    PERSON,
                    "birth_name",
                    text("FIXTURE BIRTH NAME", "de"),
                    b,
                ),
                # nicknames accumulate
                claim(PERSON, "nickname", text("Fixture Nick One"), a),
                claim(PERSON, "nickname", text("Fixture Nick Two"), b),
                # two birth names, the first in two spellings
                claim(
                    SPOUSE, "birth_name", text("Fixture Spouse Birth Name"), a
                ),
                claim(
                    SPOUSE,
                    "birth_name",
                    text("FIXTURE SPOUSE BIRTH NAME", "de"),
                    c,
                ),
                claim(
                    SPOUSE, "birth_name", text("Fixture Spouse Maiden Name"), b
                ),
                # the marriage dated two ways on the spouse statement
                claim(
                    SPOUSE,
                    "spouse",
                    item(PERSON),
                    a,
                    (("start_time", date(1947, 2, 8)),),
                ),
                claim(
                    SPOUSE,
                    "spouse",
                    item(PERSON),
                    b,
                    (("start_time", date(1946)),),
                ),
                # a stated range narrows a decade: 1937 lies outside it
                claim(
                    RANGES,
                    "date_of_birth",
                    date(1930, precision="decade"),
                    a,
                    between(date(1930), date(1935)),
                ),
                claim(RANGES, "date_of_birth", date(1937), b),
                # and a year: 2 November lies outside March to June
                claim(
                    RANGES,
                    "date_of_death",
                    date(1935),
                    a,
                    between(date(1935, 3), date(1935, 6)),
                ),
                claim(RANGES, "date_of_death", date(1935, 11, 2), b),
                # an event's own date: 22 June per one source and 22 July
                # per another, while "between 1938 and 1939" refines both
                claim(EMIGRATION, "point_in_time", date(1938, 6, 22), b),
                claim(
                    EMIGRATION,
                    "point_in_time",
                    date(1938),
                    c,
                    between(date(1938), date(1939)),
                ),
                claim(EMIGRATION, "point_in_time", date(1938, 7, 22), a),
                # a day in the stated range's second year refines it too
                claim(
                    SECOND_EMIGRATION,
                    "point_in_time",
                    date(1938),
                    c,
                    between(date(1938), date(1939)),
                ),
                claim(
                    SECOND_EMIGRATION,
                    "point_in_time",
                    date(1939, 3, 3),
                    a,
                ),
                # a century is a span: 1850 lies inside the 19th
                claim(
                    SECOND_EMIGRATION,
                    "start_time",
                    date(1800, precision="century"),
                    a,
                ),
                claim(SECOND_EMIGRATION, "start_time", date(1850), b),
            ]
        )
    )
    BatchWriter(client).write_batch(
        batch(
            [
                # a curator's correction of artifact A's reading
                claim(PERSON, "date_of_death", date(1971), a, curated=True),
            ]
        )
    )
    qids = {
        role: machine.created_items[local_id]
        for local_id, role in ROLES.items()
    }
    set_rank(
        client,
        qids["person"],
        "place_of_birth",
        lambda value: value["id"] == qids["other town"],
        WikibaseRank.DEPRECATED,
    )
    set_rank(
        client,
        qids["person"],
        "date_of_birth",
        lambda value: value["time"] == "+1919-01-07T00:00:00Z",
        WikibaseRank.PREFERRED,
    )
    wait_for_query_service(config, client, [qids[role] for role in SUBJECTS])
    return qids, {"A": a, "B": b, "C": c}


def report_rows(client, config, qids: dict[str, str]) -> list[dict]:
    """The report's rows about the fixture subjects."""
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["disagreement-report"],
        properties,
        items,
        config.concept_uri,
    )
    subjects = {qids[role] for role in SUBJECTS}
    return [
        row
        for row in run_sparql(config, query)
        if row["entity"]["value"].rsplit("/", 1)[-1] in subjects
    ]


def time_shown(value: str, precision: int) -> str:
    """A time at its stated precision (a century as its first year's
    century, a decade as its first year)."""
    day = value.lstrip("+")[:10]
    year = int(day[:4])
    if precision <= 7:
        return f"{year // 100 + 1}th century"
    if precision == 8:
        return f"{year // 10 * 10}s"
    return day[: {9: 4, 10: 7}.get(precision, 10)]


def shown(row: dict, side: str) -> str:
    """A value as its sources state it: a time at its stated precision, an
    item as its QID, a name as its text (in lower case, since the row shows
    one of the spellings that differ only in case)."""
    value = row[f"value{side}"]
    if value.get("datatype", "").endswith("dateTime"):
        return time_shown(
            value["value"], int(row[f"precision{side}"]["value"])
        )
    if value["type"] == "uri":
        return value["value"].rsplit("/", 1)[-1]
    return value["value"].lower()


def stated_range(row: dict, side: str) -> tuple:
    """The earliest and latest dates the report carries beside a value."""
    return tuple(
        time_shown(
            row[f"{name}{side}"]["value"],
            int(row[f"{name}Precision{side}"]["value"]),
        )
        if f"{name}{side}" in row
        else None
        for name in ("earliest", "latest")
    )


def row_key(row: dict, qids: dict[str, str]) -> tuple:
    role_of = {qid: role for role, qid in qids.items()}
    shared = row.get("sharedValue", {}).get("value", "").rsplit("/", 1)[-1]
    return (
        role_of[row["entity"]["value"].rsplit("/", 1)[-1]],
        row["propertyLabel"]["value"],
        role_of.get(shared, shared),
        row.get("qualifierLabel", {}).get("value", ""),
        frozenset({shown(row, "A"), shown(row, "B")}),
    )


def sources(row: dict, side: str) -> set[str]:
    return {
        pair.split("~", 1)[0]
        for pair in row[f"sources{side}"]["value"].split("|")
        if pair
    }


EXPECTED = {
    (
        "person",
        "date of birth",
        "",
        "",
        frozenset({"1919-01-07", "1919-07-01"}),
    ),
    ("person", "residence", "town", "start time", frozenset({"1939", "1940"})),
    (
        "spouse",
        "birth name",
        "",
        "",
        frozenset({"fixture spouse birth name", "fixture spouse maiden name"}),
    ),
    (
        "spouse",
        "spouse",
        "person",
        "start time",
        frozenset({"1946", "1947-02-08"}),
    ),
    ("ranges", "date of birth", "", "", frozenset({"1930s", "1937"})),
    ("ranges", "date of death", "", "", frozenset({"1935", "1935-11-02"})),
    (
        "emigration",
        "point in time",
        "",
        "",
        frozenset({"1938-06-22", "1938-07-22"}),
    ),
}


def test_report_lists_exactly_the_disagreements_between_artifacts(
    client, config, fixture_graph
):
    qids, _ = fixture_graph
    rows = report_rows(client, config, qids)
    assert {row_key(row, qids) for row in rows} == EXPECTED
    assert len(rows) == len(EXPECTED)  # each disagreement listed once


def test_each_value_names_the_artifacts_that_state_it(
    client, config, fixture_graph
):
    qids, source = fixture_graph
    stated_by = {
        "1919-01-07": {source["A"]},
        "1919-07-01": {source["B"]},
        "1939": {source["A"]},
        "1940": {source["B"]},
        # both spellings of one name, listed together
        "fixture spouse birth name": {source["A"], source["C"]},
        "fixture spouse maiden name": {source["B"]},
        "1947-02-08": {source["A"]},
        "1946": {source["B"]},
        "1930s": {source["A"]},
        "1937": {source["B"]},
        "1935": {source["A"]},
        "1935-11-02": {source["B"]},
        "1938-06-22": {source["B"]},
        "1938-07-22": {source["A"]},
    }
    for row in report_rows(client, config, qids):
        for side in "AB":
            assert sources(row, side) == stated_by[shown(row, side)], row_key(
                row, qids
            )


def test_values_keep_their_stated_precision(client, config, fixture_graph):
    """Rule 5: a year-precision date must not display as 1 January."""
    qids, _ = fixture_graph
    (row,) = [
        row
        for row in report_rows(client, config, qids)
        if row["propertyLabel"]["value"] == "spouse"
    ]
    precision = {
        shown(row, side): row[f"precision{side}"]["value"] for side in "AB"
    }
    assert precision == {"1946": "9", "1947-02-08": "11"}


def test_a_stated_range_travels_with_its_value(client, config, fixture_graph):
    """Rule 5: the range a source states is never silently dropped; it is
    why "the 1930s" and 1937 disagree."""
    qids, _ = fixture_graph
    ranges = {}
    for row in report_rows(client, config, qids):
        if row_key(row, qids)[0] != "ranges":
            continue
        for side in "AB":
            ranges[shown(row, side)] = stated_range(row, side)
    assert ranges == {
        "1930s": ("1930", "1935"),
        "1937": (None, None),
        "1935": ("1935-03", "1935-06"),
        "1935-11-02": (None, None),
    }


def test_rank_is_an_overlay_never_a_requirement(client, config, fixture_graph):
    """A preferred value is listed beside the value it outranks; a
    deprecated value is out of contention."""
    qids, _ = fixture_graph
    ranks = {
        (s["mainsnak"]["datavalue"]["value"]["time"][1:11], s["rank"])
        for s in statements(client, qids["person"], "date_of_birth")
    }
    assert ("1919-01-07", "preferred") in ranks
    deprecated = [
        s
        for s in statements(client, qids["person"], "place_of_birth")
        if s["rank"] == "deprecated"
    ]
    assert [s["mainsnak"]["datavalue"]["value"]["id"] for s in deprecated] == [
        qids["other town"]
    ]
    keys = {row_key(row, qids) for row in report_rows(client, config, qids)}
    assert (
        "person",
        "date of birth",
        "",
        "",
        frozenset({"1919-01-07", "1919-07-01"}),
    ) in keys
    assert not [key for key in keys if key[1] == "place of birth"]


def test_what_the_report_leaves_out(client, config, fixture_graph):
    """Each exclusion names statements that exist in the graph, so none of
    them passes by being absent."""
    qids, _ = fixture_graph
    person = qids["person"]
    left_out = {
        "a curator's correction from the same artifact": "date_of_death",
        "a page fragment beside its own artifact": "place_of_death",
        "a deprecated value": "place_of_birth",
        "one birth name in two cases and languages": "birth_name",
        "nicknames, which accumulate": "nickname",
    }
    labels = {
        "date_of_death": "date of death",
        "place_of_death": "place of death",
        "place_of_birth": "place of birth",
        "birth_name": "birth name",
        "nickname": "nickname",
    }
    rows = report_rows(client, config, qids)
    about_person = [row for row in rows if row_key(row, qids)[0] == "person"]
    for reason, prop_key in left_out.items():
        assert len(statements(client, person, prop_key)) == 2, reason
        assert not [
            row
            for row in about_person
            if row["propertyLabel"]["value"] == labels[prop_key]
        ], reason
    # refinements: a span never faces a date inside it ("between 1938 and
    # 1939" and 3 March 1939; the 19th century and 1850)
    second = qids["second emigration"]
    for prop_key in ("point_in_time", "start_time"):
        assert len(statements(client, second, prop_key)) == 2, prop_key
    assert not [
        row for row in rows if row_key(row, qids)[0] == "second emigration"
    ]
    shown_values = {value for row in rows for value in row_key(row, qids)[4]}
    assert not shown_values & {"1919", "1938", "19th century", "1850"}
    # residence observations and a one-sided end time are not compared
    assert len(statements(client, person, "residence")) == 4
    residence_rows = [
        row_key(row, qids)
        for row in about_person
        if row["propertyLabel"]["value"] == "residence"
    ]
    assert residence_rows == [
        (
            "person",
            "residence",
            "town",
            "start time",
            frozenset({"1939", "1940"}),
        )
    ]
