"""Live write path for the GEDCOM adapter's SOUR/OBJE handling (ADR-0021),
a residence's date period (#308), a repeated event's alternatives (#322),
an undated death (#323), a marriage's citation (#324), and a family's
union with its events, the death's principal and the undetermined
language of an export that states none (#408).

A source citation (SOUR) must fold into the birth statement's reference
as a page-number locator, and a multimedia object (OBJE) must become an
un-held media item the person `image`s — an ordinary `artifact` with NO
content hash (its bytes are referenced, never fetched). A residence's
date period (FROM x TO y) must become the residence statement's start and
end time, each with the range its own phrase states (docs/MODELING.md,
"Marriages, residences and other periods"). A repeated BIRT or MARR, as
GEDCOM states an alternative, must become statements of its own, each
with its own date, circa and citation, beside the first. A family with a
MARR must become a marriage item whose couple are its participants with
the role spouse, started by a wedding item that holds each MARR's date
as a statement of its own, the citation beneath a MARR in its reference,
and ended by a divorce item that holds the DIV's date and place
(ADR-0036 §5); a family whose only event is a DIV must become a marriage
item with no wedding. A DEAT that gives
no date, place or cause must become a death event: an item that is an
instance of `death`, with the person as its principal participant, which
is what the possibly-living check reads as death evidence (ADR-0020 §2).
The export states no language, so its excerpts are undetermined (`und`),
which the instance must take (ADR-0035 §6), as it must every code the
adapter maps a GEDCOM language name to. All flow through the generic
writer unchanged; this proves they land in the graph. Every claim cites
the export by the SHA-1 of its bytes (#102)."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef
from yizkr.pipeline import gedcom, writer
from yizkr.pipeline.gedcom import (
    GEDCOM_LANGUAGES,
    UNDETERMINED,
    adapt_gedcom,
)
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0021: a GEDCOM export's person with a cited birth, a portrait, a "
    "residence period, repeated events, an undated death and two families"
)
MARK = fixture_mark(FIXTURE, writer, gedcom)

# The export's text, with {marker} in each label it gives an item: the
# people's (and so the death event's), the media object's and the place's
# (_marker).
TEMPLATE = """\
0 HEAD
1 CHAR UTF-8
0 @S1@ SOUR
1 TITL Integration birth register
0 @S2@ SOUR
1 TITL Integration burial register
0 @S3@ SOUR
1 TITL Integration marriage register
0 @O1@ OBJE
1 FILE https://example.test/portrait.jpg
2 FORM jpg
2 TITL Integration studio portrait {marker}
1 DATE 1956
0 @I1@ INDI
1 NAME Gedcom Live /Tester {marker}/
1 OBJE @O1@
1 BIRT
2 DATE 1950
2 SOUR @S1@
3 PAGE Volume 1, Page 1
1 BIRT
2 DATE ABT 1951
2 SOUR @S1@
3 PAGE Volume 2, Page 7
1 RESI
2 DATE FROM BET 1956 AND 1957 TO BET 1974 AND 1984
2 PLAC Integration Town {marker}
1 FAMS @F1@
1 FAMS @F2@
0 @I2@ INDI
1 NAME Gedcom Spouse /Tester {marker}/
1 DEAT Y
2 SOUR @S2@
3 PAGE Volume 3, Page 9
1 FAMS @F1@
0 @F1@ FAM
1 HUSB @I1@
1 WIFE @I2@
1 MARR
2 DATE 1975
2 SOUR @S3@
3 PAGE Volume 4, Page 2
1 MARR
2 DATE ABT 1974
1 DIV
2 DATE 1980
2 PLAC Integration Court Town {marker}
0 @I3@ INDI
1 NAME Gedcom Former /Tester {marker}/
1 FAMS @F2@
0 @F2@ FAM
1 HUSB @I1@
1 WIFE @I3@
1 DIV
2 DATE 1970
0 TRLR
"""


def _marker() -> str:
    """Six hex digits of a hash over the export's text and the batch the
    adapter makes of it, as test_live_period_dates.py's write_fixture
    marks its labels. The writer finds an item by its label, so a version
    of the fixture, or of the adapter, that gives other statements for
    the export would otherwise write onto the items an earlier version
    wrote, and see their statements beside its own. With the marker each
    version writes items of its own. The hash also covers the module's
    marker (conftest.fixture_mark: the writer's and the adapter's source
    and the session's random bytes), so each session writes items of its
    own: what an earlier session wrote, a live mutant's run included,
    never meets it, and a re-run within the session finds its own items
    again. The batch is made under a fixed artifact key,
    since the export's own key is a hash of the marked text, and is
    hashed without its run details, which differ from run to run."""
    made = adapt_gedcom(TEMPLATE, artifact=ArtifactRef.from_bytes(b"")).batch
    content = made.model_dump(mode="json", exclude={"extraction_run"})
    return hashlib.sha256(
        (TEMPLATE + json.dumps(content, sort_keys=True) + MARK).encode()
    ).hexdigest()[:6]


GEDCOM = TEMPLATE.replace("{marker}", _marker())


@pytest.fixture(scope="module")
def gedcom_artifact(client, tmp_path_factory):
    """(the export's item, the key its claims cite it by)."""
    path = tmp_path_factory.mktemp("gedcom") / f"integration-tree-{MARK}.ged"
    path.write_text(GEDCOM)
    registered = register_artifact(
        client,
        media_path=path,
        artifact_type="genealogical_report",
        title=f"Integration GEDCOM tree {MARK}",
        description="written by tests/integration; safe to delete",
    )
    return registered.artifact_qid, ArtifactRef.from_path(path)


def _claim_values(client, entity_qid, prop_key):
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": entity_qid,
            "property": properties[prop_key],
        }
    )
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claims in data["claims"].values()
        for claim in claims
    ]


def test_gedcom_sour_folds_and_obje_becomes_unheld_media(
    client, gedcom_artifact
):
    export_qid, export = gedcom_artifact
    result = adapt_gedcom(GEDCOM, artifact=export)
    write = write_mapped(BatchWriter(client), result.batch)
    person_qid = write.created_items["i1"]
    media_qid = write.created_items["md1"]
    properties, items = client.vocabulary()

    # OBJE -> an un-held media item the person `image`s, typed `artifact`
    image_targets = {
        v["id"] for v in _claim_values(client, person_qid, "image")
    }
    assert media_qid in image_targets
    media_classes = {
        v["id"] for v in _claim_values(client, media_qid, "instance_of")
    }
    assert items["artifact"] in media_classes
    # un-held: no content hash was written (bytes are not held)
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": media_qid,
            "property": properties["media_sha1"],
        }
    )
    assert not data["claims"]

    # SOUR -> the birth statement's reference gains the PAGE as a locator
    dob = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    pages = [
        snak["datavalue"]["value"]
        for claims in dob["claims"].values()
        for claim in claims
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["page_number"], [])
    ]
    assert any("Volume 1" in page for page in pages)
    # the claim cited the export by its bytes' SHA-1, which the writer
    # resolved to the export's item. The person's label carries this
    # version's marker, so each birth statement the person holds is this
    # export's
    stated_in = {
        snak["datavalue"]["value"]["id"]
        for claims in dob["claims"].values()
        for claim in claims
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    assert stated_in == {export_qid}


def test_gedcom_residence_period_is_a_start_and_an_end(
    client, gedcom_artifact
):
    """FROM BET 1956 AND 1957 TO BET 1974 AND 1984 on a RESI: a start
    between 1956 and 1957 and an end between 1974 and 1984, each end's
    range in its own pair of qualifiers, and no point in time."""
    _, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    person_qid = write.created_items["i1"]
    place_qid = write.created_items["pl1"]
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["residence"],
        }
    )
    (residence,) = [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]
    assert residence["mainsnak"]["datavalue"]["value"]["id"] == place_qid
    written = {
        key_of[pid]: [
            (
                snak["datavalue"]["value"]["time"],
                snak["datavalue"]["value"]["precision"],
            )
            for snak in snaks
        ]
        for pid, snaks in residence.get("qualifiers", {}).items()
    }
    year = 9  # Wikibase's precision for a year
    assert written == {
        "start_time": [("+1956-00-00T00:00:00Z", year)],
        "earliest_date": [("+1956-00-00T00:00:00Z", year)],
        "latest_start_date": [("+1957-00-00T00:00:00Z", year)],
        "end_time": [("+1974-00-00T00:00:00Z", year)],
        "earliest_end_date": [("+1974-00-00T00:00:00Z", year)],
        "latest_date": [("+1984-00-00T00:00:00Z", year)],
    }


def test_gedcom_alternatives_are_statements_of_their_own(
    client, gedcom_artifact
):
    """A repeated BIRT lands as statements of their own, each with its own
    date, circa and citation: a birth in 1950 per page 1 and one about
    1951 per page 7 (#322). The repeated MARR dates the couple's wedding
    (test_gedcom_family_is_a_marriage_bounded_by_its_events), and no
    spouse statement is written (#408)."""
    _, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    person_qid = write.created_items["i1"]
    properties, items = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}

    def statements(prop):
        found = client.api_get(
            {
                "action": "wbgetclaims",
                "entity": person_qid,
                "property": properties[prop],
            }
        )
        return [
            statement
            for statements in found["claims"].values()
            for statement in statements
        ]

    def qualifiers(statement):
        """(property, time or item) pairs, in property order."""
        return sorted(
            (
                key_of[pid],
                snak["datavalue"]["value"].get("time")
                or snak["datavalue"]["value"]["id"],
            )
            for pid, snaks in statement.get("qualifiers", {}).items()
            for snak in snaks
        )

    def pages(statement):
        return sorted(
            snak["datavalue"]["value"]
            for ref in statement.get("references", [])
            for snak in ref["snaks"].get(properties["page_number"], [])
        )

    circa = ("sourcing_circumstances", items["circa"])
    births = sorted(
        (
            statement["mainsnak"]["datavalue"]["value"]["time"],
            qualifiers(statement),
            pages(statement),
        )
        for statement in statements("date_of_birth")
    )
    assert births == [
        ("+1950-00-00T00:00:00Z", [], ["Volume 1, Page 1"]),
        ("+1951-00-00T00:00:00Z", [circa], ["Volume 2, Page 7"]),
    ]
    # the couple's marriage: no spouse statement, but a union each of
    # them takes part in as a spouse
    assert statements("spouse") == []


def _statements(client, entity_qid, prop_key):
    properties, _ = client.vocabulary()
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": entity_qid,
            "property": properties[prop_key],
        }
    )
    return [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]


def _qualifier_values(client, statement, prop_key):
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"].get("time")
        or snak["datavalue"]["value"]["id"]
        for snak in statement.get("qualifiers", {}).get(
            properties[prop_key], []
        )
    ]


def test_gedcom_family_is_a_marriage_bounded_by_its_events(
    client, gedcom_artifact
):
    """The family lands as a marriage item, whose couple take part in it
    with the role spouse; it is started by a wedding item, which holds a
    point in time of 1975, cited to the marriage register at its page, and
    one of about 1974, and ended by a divorce item, which holds a point in
    time of 1980 and its place (ADR-0036 §5)."""
    _, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    union_qid = write.created_items["f1"]
    wedding_qid = write.created_items["f1-wedding"]
    divorce_qid = write.created_items["f1-divorce"]
    court_town_qid = write.created_items["pl2"]
    properties, items = client.vocabulary()

    def kinds(qid):
        return {v["id"] for v in _claim_values(client, qid, "instance_of")}

    assert kinds(union_qid) == {items["marriage"]}
    assert kinds(wedding_qid) == {items["wedding"]}
    assert kinds(divorce_qid) == {items["divorce"]}
    couple = sorted(
        (
            statement["mainsnak"]["datavalue"]["value"]["id"],
            _qualifier_values(client, statement, "object_has_role"),
        )
        for statement in _statements(client, union_qid, "participant")
    )
    assert couple == sorted(
        [
            (write.created_items["i1"], [items["spouse"]]),
            (write.created_items["i2"], [items["spouse"]]),
        ]
    )
    assert {
        v["id"] for v in _claim_values(client, union_qid, "started_by")
    } == {wedding_qid}
    assert {v["id"] for v in _claim_values(client, union_qid, "ended_by")} == {
        divorce_qid
    }

    def pages(statement):
        return sorted(
            snak["datavalue"]["value"]
            for ref in statement.get("references", [])
            for snak in ref["snaks"].get(properties["page_number"], [])
        )

    weddings = sorted(
        (
            statement["mainsnak"]["datavalue"]["value"]["time"],
            _qualifier_values(client, statement, "sourcing_circumstances"),
            pages(statement),
        )
        for statement in _statements(client, wedding_qid, "point_in_time")
    )
    assert weddings == [
        ("+1974-00-00T00:00:00Z", [items["circa"]], []),
        ("+1975-00-00T00:00:00Z", [], ["Volume 4, Page 2"]),
    ]
    assert [
        v["time"] for v in _claim_values(client, divorce_qid, "point_in_time")
    ] == ["+1980-00-00T00:00:00Z"]
    assert [
        v["id"] for v in _claim_values(client, divorce_qid, "location")
    ] == [court_town_qid]


def test_gedcom_divorce_without_a_wedding_is_of_a_marriage(
    client, gedcom_artifact
):
    """A family whose only event is a dated DIV lands as a marriage item,
    its couple spouses, ended by a divorce of 1970 and started by
    nothing: a divorce ends a marriage, whose wedding the export does
    not state (ADR-0036, #408)."""
    _, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    union_qid = write.created_items["f2"]
    divorce_qid = write.created_items["f2-divorce"]
    _, items = client.vocabulary()
    assert {
        v["id"] for v in _claim_values(client, union_qid, "instance_of")
    } == {items["marriage"]}
    assert _claim_values(client, union_qid, "started_by") == []
    assert {v["id"] for v in _claim_values(client, union_qid, "ended_by")} == {
        divorce_qid
    }
    couple = sorted(
        (
            statement["mainsnak"]["datavalue"]["value"]["id"],
            _qualifier_values(client, statement, "object_has_role"),
        )
        for statement in _statements(client, union_qid, "participant")
    )
    assert couple == sorted(
        [
            (write.created_items["i1"], [items["spouse"]]),
            (write.created_items["i3"], [items["spouse"]]),
        ]
    )
    assert [
        v["time"] for v in _claim_values(client, divorce_qid, "point_in_time")
    ] == ["+1970-00-00T00:00:00Z"]


def test_gedcom_excerpts_of_an_export_without_a_language_are_undetermined(
    client, gedcom_artifact
):
    """The export's HEAD has no LANG: the excerpt of each statement lands
    with the language `und`, which the instance takes for a monolingual
    text (ADR-0035 §6)."""
    _, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    properties, _ = client.vocabulary()
    excerpts = [
        snak["datavalue"]["value"]
        for statement in _statements(
            client, write.created_items["i1"], "date_of_birth"
        )
        for ref in statement.get("references", [])
        for snak in ref["snaks"].get(properties["transcription_excerpt"], [])
    ]
    assert excerpts
    assert {excerpt["language"] for excerpt in excerpts} == {"und"}


def test_every_code_the_adapter_maps_to_is_one_the_instance_takes(client):
    """Each code of GEDCOM_LANGUAGES, and `und`, is a language the
    instance takes for a monolingual text: a code it refused would fail
    the write of every statement cited to such an export rather than be
    counted (ADR-0035 §6)."""
    found = client.api_get(
        {
            "action": "query",
            "meta": "wbcontentlanguages",
            "wbclcontext": "monolingualtext",
        }
    )
    accepted = set(found["query"]["wbcontentlanguages"])
    wanted = {*GEDCOM_LANGUAGES.values(), UNDETERMINED}
    assert sorted(wanted - accepted) == []


def test_gedcom_undated_death_is_a_death_event(client, gedcom_artifact):
    """`1 DEAT Y`, with a citation and nothing else beneath it, lands as a
    death event: an item that is an instance of `death`, with the spouse
    as its principal participant at normal rank, cited to the export at
    the citation's page. The spouse has no date of death (#323, #408)."""
    export_qid, export = gedcom_artifact
    write = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    spouse_qid = write.created_items["i2"]
    event_qid = write.created_items["i2-death"]
    properties, items = client.vocabulary()
    kinds = {v["id"] for v in _claim_values(client, event_qid, "instance_of")}
    assert kinds == {items["death"]}
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": event_qid,
            "property": properties["participant"],
        }
    )
    (participant,) = [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]
    assert participant["mainsnak"]["datavalue"]["value"]["id"] == spouse_qid
    assert _qualifier_values(client, participant, "object_has_role") == [
        items["principal"]
    ]
    assert participant["rank"] == "normal"
    references = participant.get("references", [])
    stated_in = {
        snak["datavalue"]["value"]["id"]
        for ref in references
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    assert stated_in == {export_qid}
    pages = [
        snak["datavalue"]["value"]
        for ref in references
        for snak in ref["snaks"].get(properties["page_number"], [])
    ]
    assert pages == ["Volume 3, Page 9"]
    assert _claim_values(client, spouse_qid, "date_of_death") == []


def test_gedcom_write_is_idempotent(client, gedcom_artifact):
    """Re-adapting and re-writing the same export writes nothing new."""
    _, export = gedcom_artifact
    first = adapt_gedcom(GEDCOM, artifact=export)
    write_mapped(BatchWriter(client), first.batch)
    rerun = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    assert rerun.written == 0
