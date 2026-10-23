"""A containment is a source's claim, with the time its source gives
(ADR-0038 §2, §8.4; #515).

The claim schema and the manifest take a `part_of` or a `location` claim
with the qualifiers that date a period (`start_time`, `end_time`), an
observation (`point_in_time`), their ranges and circa, and the writer
writes them as it writes any claim's: a dated containment and an undated
one of the same place and container coexist as two statements, each with
its source and its run. The live test is
tests/integration/test_live_containment.py.

The fixtures are fictional (docs/EXAMPLES.md): Koshnovka, Kaldovsky
District and Chadsk Oblast, with the pattern index's worked examples of a
dated containment ("in 1941 Koshnovka belonged to Kaldovsky District";
"Kaldovsky District, in Chadsk Oblast from 1937"), which no source in the
universe states, beside the 1941 death certificate's district field,
which states no time.
"""

from __future__ import annotations

import pytest
import yaml

from tests.fixtures.identity import write_mapped
from tests.unit.test_pipeline import (
    ARTIFACT,
    ITEMS,
    MEDIA_SHA1,
    PROPERTIES,
    RUN,
    FakeWiki,
)
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.model.schema import read_manifest_data
from yizkr.pipeline.writer import BatchWriter

# the dating qualifiers, each with a value a source might give it
DATING = {
    "point_in_time": 1941,
    "start_time": 1937,
    "end_time": 1957,
    "earliest_date": 1936,
    "latest_date": 1958,
    "latest_start_date": 1938,
    "earliest_end_date": 1956,
}
CIRCA = {
    "property": "sourcing_circumstances",
    "value": {"type": "item", "value": {"kind": "vocab", "key": "circa"}},
}

# the fake vocabulary's properties and items besides test_pipeline's
EXTRA_PROPERTIES = {
    "part_of": "P50",
    "end_time": "P51",
    "earliest_date": "P52",
    "latest_date": "P53",
    "latest_start_date": "P54",
    "earliest_end_date": "P55",
    "sourcing_circumstances": "P56",
}
EXTRA_ITEMS = {"circa": "Q60"}
VOCABULARY = {**PROPERTIES, **EXTRA_PROPERTIES}


class PlaceWiki(FakeWiki):
    """A fake instance whose vocabulary has `part_of`, the dating
    qualifiers and `circa`."""

    def vocabulary(self):
        return dict(VOCABULARY), {**ITEMS, **EXTRA_ITEMS}


def _place(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "place",
    }


KOSHNOVKA = _place("koshnovka", "Koshnovka")
DISTRICT = _place("kaldovsky", "Kaldovsky District")
OBLAST = _place("chadsk", "Chadsk Oblast")


def _time(year: int) -> dict:
    return {"type": "time", "year": year}


def _contained(
    subject: dict,
    container: dict,
    excerpt: str,
    qualifiers: list[dict] = (),
    prop: str = "part_of",
) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": {"type": "item", "value": container},
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": ARTIFACT,
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "en"},
        },
    }


def _batch(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


class TestTheSchemaTakesAContainmentsTime:
    def test_the_manifest_says_a_containment_takes_its_sources_time(self):
        properties = yaml.safe_load(read_manifest_data())["properties"]
        assert properties["part_of"]["description"] == (
            "this entity is a part of that entity; links an artifact "
            "fragment to its artifact, and a place to a container its source "
            "states, with the time the source gives (start_time, end_time or "
            "point_in_time) and none where it gives none"
        )

    @pytest.mark.parametrize("prop", ["part_of", "location"])
    @pytest.mark.parametrize("qualifier", sorted(DATING))
    def test_each_dating_qualifier(self, prop, qualifier):
        claim = _contained(
            KOSHNOVKA,
            DISTRICT,
            "in 1941 Koshnovka belonged to Kaldovsky District",
            [{"property": qualifier, "value": _time(DATING[qualifier])}],
            prop=prop,
        )
        assert validate_against_manifest(_batch(claim)) == []

    @pytest.mark.parametrize("prop", ["part_of", "location"])
    def test_circa(self, prop):
        claim = _contained(
            KOSHNOVKA,
            DISTRICT,
            "about 1941 Koshnovka belonged to Kaldovsky District",
            [{"property": "point_in_time", "value": _time(1941)}, CIRCA],
            prop=prop,
        )
        assert validate_against_manifest(_batch(claim)) == []


# "in 1941 Koshnovka belonged to Kaldovsky District", read between 1940
# and 1941, circa
OBSERVED = [
    {"property": "point_in_time", "value": _time(1941)},
    {"property": "earliest_date", "value": _time(1940)},
    {"property": "latest_date", "value": _time(1941)},
    CIRCA,
]
# "Kaldovsky District, in Chadsk Oblast from 1937 (or 1938) until 1957"
PERIOD = [
    {"property": "start_time", "value": _time(1937)},
    {"property": "earliest_date", "value": _time(1937)},
    {"property": "latest_start_date", "value": _time(1938)},
    {"property": "end_time", "value": _time(1957)},
    {"property": "earliest_end_date", "value": _time(1956)},
]


def _written(wiki, qid: str) -> list[dict]:
    """The part-of statements of an item, as the instance stores them."""
    claims = wiki.wbi.item.get(qid).get_json()["claims"]
    return claims.get(EXTRA_PROPERTIES["part_of"], [])


def _qualifiers(statement: dict) -> dict[str, list]:
    """A statement's qualifiers by manifest key: a time by its year, an
    item by its id."""
    keys = {pid: key for key, pid in VOCABULARY.items()}
    found: dict[str, list] = {}
    for pid, snaks in statement.get("qualifiers", {}).items():
        for snak in snaks:
            value = snak["datavalue"]["value"]
            found.setdefault(keys[pid], []).append(
                int(value["time"][1:5]) if "time" in value else value["id"]
            )
    return found


def _cited(statement: dict) -> set[str]:
    return {
        snak["datavalue"]["value"]["id"]
        for reference in statement["references"]
        for snaks in reference["snaks"].values()
        for snak in snaks
        if snak["datatype"] == "wikibase-item"
    }


class TestTheWriterWritesAContainmentsTime:
    def write(self, *claims: dict):
        wiki = PlaceWiki({MEDIA_SHA1: "Q23"})
        result = write_mapped(BatchWriter(wiki), _batch(*claims))
        return wiki, result

    def test_a_dated_and_an_undated_containment_coexist(self):
        wiki, result = self.write(
            _contained(
                KOSHNOVKA,
                DISTRICT,
                "in 1941 Koshnovka belonged to Kaldovsky District",
                OBSERVED,
            ),
            _contained(KOSHNOVKA, DISTRICT, "Koshnovka, Kaldovsky District"),
        )
        assert result.written == 2
        district = result.created_items["kaldovsky"]
        statements = _written(wiki, result.created_items["koshnovka"])
        assert len(statements) == 2
        for statement in statements:
            assert (
                statement["mainsnak"]["datavalue"]["value"]["id"] == district
            )
            assert statement["rank"] == "normal"
            # the source and the run, as on any claim
            assert _cited(statement) == {"Q23", result.run_qid}
        dated, undated = sorted(
            statements, key=lambda s: -len(s.get("qualifiers", {}))
        )
        assert _qualifiers(dated) == {
            "point_in_time": [1941],
            "earliest_date": [1940],
            "latest_date": [1941],
            "sourcing_circumstances": [EXTRA_ITEMS["circa"]],
        }
        # no time the source does not give
        assert _qualifiers(undated) == {}

    def test_a_period_is_written_with_the_range_of_each_end(self):
        wiki, result = self.write(
            _contained(
                DISTRICT,
                OBLAST,
                "Kaldovsky District, in Chadsk Oblast from 1937",
                PERIOD,
            )
        )
        (statement,) = _written(wiki, result.created_items["kaldovsky"])
        assert _qualifiers(statement) == {
            "start_time": [1937],
            "earliest_date": [1937],
            "latest_start_date": [1938],
            "end_time": [1957],
            "earliest_end_date": [1956],
        }

    def test_a_rerun_writes_neither_again(self):
        claims = (
            _contained(
                KOSHNOVKA,
                DISTRICT,
                "in 1941 Koshnovka belonged to Kaldovsky District",
                OBSERVED,
            ),
            _contained(KOSHNOVKA, DISTRICT, "Koshnovka, Kaldovsky District"),
        )
        wiki = PlaceWiki({MEDIA_SHA1: "Q23"})
        first = write_mapped(BatchWriter(wiki), _batch(*claims))
        again = write_mapped(BatchWriter(wiki), _batch(*claims))
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
        assert len(_written(wiki, first.created_items["koshnovka"])) == 2
