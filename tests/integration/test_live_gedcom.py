"""Live write path for a GEDCOM export (ADR-0021): an undated death
(#323), the undetermined language of an export that states none (#408),
and a repeated write of the whole export.

The export gives a person with two cited births, a portrait (an OBJE
with a DATE of its own), a residence period and three families, one of
two WIFEs with a child, and a spouse whose DEAT gives no date, place or
cause. That DEAT must become a `date of death` of unknown value on the
person, which the possibly-living check reads as death evidence
(ADR-0020 §2, ADR-0040 Q4). The export states no language, so its
excerpts are undetermined (`und`), which the instance must take
(ADR-0035 §6), as it must every code the adapter maps a GEDCOM language
name to. Written again, the export writes nothing. Every claim
cites the export by the SHA-1 of its bytes (#102)."""

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
    "residence period, repeated events, an undated death and three "
    "families, one of two WIFEs with a child"
)
MARK = fixture_mark(FIXTURE, writer, gedcom)

# The export's text, with {marker} in each label it gives an item: the
# people's, the media object's and the place's
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
0 @I4@ INDI
1 NAME Gedcom Child /Tester {marker}/
1 FAMC @F3@
0 @F3@ FAM
1 WIFE @I2@
1 WIFE @I3@
1 CHIL @I4@
0 TRLR
"""


def _marker() -> str:
    """Six hex digits of a hash over the export's text and the batch the
    adapter makes of it. The writer finds an item by its label, so a
    version of the fixture, or of the adapter, that gives other statements
    for the export would otherwise write onto the items an earlier version
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


def test_gedcom_undated_death_is_an_unknown_date_of_death(
    client, gedcom_artifact
):
    """`1 DEAT Y`, with a citation and nothing else beneath it, lands as a
    `date of death` of unknown value on the spouse (a somevalue snak), at
    normal rank, cited to the export at the citation's page; no death
    event is made (ADR-0040 Q4, #687). A second write writes no copy."""
    export_qid, export = gedcom_artifact
    batch = adapt_gedcom(GEDCOM, artifact=export).batch
    write = write_mapped(BatchWriter(client), batch)
    spouse_qid = write.created_items["i2"]
    assert "i2-death" not in write.created_items
    properties, _ = client.vocabulary()
    (death,) = _statements(client, spouse_qid, "date_of_death")
    assert death["mainsnak"]["snaktype"] == "somevalue"
    assert "datavalue" not in death["mainsnak"]
    assert death["rank"] == "normal"
    assert not death.get("qualifiers")
    references = death.get("references", [])
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
    # written again, it finds the statement: the snak types compare
    write_mapped(BatchWriter(client), batch)
    assert len(_statements(client, spouse_qid, "date_of_death")) == 1


def test_gedcom_write_is_idempotent(client, gedcom_artifact):
    """Re-adapting and re-writing the same export writes nothing new."""
    _, export = gedcom_artifact
    first = adapt_gedcom(GEDCOM, artifact=export)
    write_mapped(BatchWriter(client), first.batch)
    rerun = write_mapped(
        BatchWriter(client), adapt_gedcom(GEDCOM, artifact=export).batch
    )
    assert rerun.written == 0
