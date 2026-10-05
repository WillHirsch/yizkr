"""Live: name forms of no stated type become referenced `name` statements
(ADR-0011 as amended by #435).

The bootstrap creates the generic `name` property (P2561, monolingual
text) once. A GEDCOM export's bracketed part, a further NAME with no
TYPE and a NAME with a TYPE the adapter does not recognise are each
written as a `name` statement on the person, beside their alias, with the
`stated in` reference to the export and the extraction-run reference; a
form the export types (here a `_MARNM` without slashes, which gives no
typed claim), the label's own form and a NAME that held a placeholder
give none. A re-run writes nothing.

The names are the example universe's (docs/EXAMPLES.md), with a surname
of the tests' own so that the label finds no other test's item.
"""

from __future__ import annotations

import pytest

from yizkr.claims import ArtifactRef
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

GEDCOM = """\
0 HEAD
1 CHAR UTF-8
1 LANG German
0 @I1@ INDI
1 NAME Ida (Peppi) /Untypedcheck/
1 NAME Hilde /Untypedcheck/
1 NAME Agnes /Untypedcheck/
2 TYPE stage
1 NAME Ida /Untypedcheck-Löbig/
2 TYPE married
1 NAME Eugen /?/
1 NAME Ida Löbig
1 _MARNM Ida Löbig
1 BIRT
2 DATE 1881
0 TRLR
"""


def statements(client, qid: str, prop_key: str) -> list[tuple]:
    """`qid`'s `prop_key` statements, sorted, each as (text, language,
    stated-in QIDs, excerpt texts, whether a run reference is among the
    references), so that a duplicated statement shows as two."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[prop_key],
        }
    )
    run_pid = properties["inferred_from_extraction_run"]
    found: list[tuple] = []
    for claim in (c for cs in data["claims"].values() for c in cs):
        value = claim["mainsnak"]["datavalue"]["value"]
        references = [ref["snaks"] for ref in claim.get("references", [])]
        found.append(
            (
                value["text"],
                value["language"],
                tuple(
                    sorted(
                        snak["datavalue"]["value"]["id"]
                        for snaks in references
                        for snak in snaks.get(properties["stated_in"], [])
                    )
                ),
                tuple(
                    sorted(
                        snak["datavalue"]["value"]["text"]
                        for snaks in references
                        for snak in snaks.get(
                            properties["transcription_excerpt"], []
                        )
                    )
                ),
                any(run_pid in snaks for snaks in references),
            )
        )
    return sorted(found)


def test_the_bootstrap_creates_the_name_property_once(client):
    manifest = load_manifest()
    bootstrap_instance(client, manifest, log=lambda *_: None)
    client.forget_vocabulary()
    properties, _ = client.vocabulary()
    snapshot = client.fetch_entities([properties["name"]])
    assert snapshot[properties["name"]]["datatype"] == "monolingualtext"
    second = bootstrap_instance(client, manifest, log=lambda *_: None)
    assert second["created"] == 0
    assert second["updated"] == 0


def test_untyped_name_forms_are_referenced_statements(client, tmp_path):
    export = tmp_path / "integration-untyped-names-tree.ged"
    export.write_text(GEDCOM)
    exported = ArtifactRef.from_path(export)
    artifact_qid = register_artifact(
        client,
        media_path=export,
        artifact_type="genealogical_report",
        title="Integration untyped names GEDCOM tree",
        description="written by tests/integration; safe to delete",
    ).artifact_qid
    result = adapt_gedcom(GEDCOM, artifact=exported)
    (person,) = [e for e in result.batch.entities if e.id == "i1"]
    assert person.label == "Ida Untypedcheck"
    assert "Peppi" in person.aliases
    qid = BatchWriter(client).write_batch(result.batch).created_items["i1"]

    names = statements(client, qid, "name")
    mine = [entry for entry in names if artifact_qid in entry[2]]
    # one statement each, with the export's language, its `stated in`
    # reference whose excerpt is the NAME it cites, and the run
    # reference; neither the label's form, the typed married name, the
    # form the `_MARNM` types nor the placeholder's NAME
    assert mine == sorted(
        [
            (
                "Peppi",
                "de",
                (artifact_qid,),
                ("1 NAME Ida (Peppi) /Untypedcheck/",),
                True,
            ),
            (
                "Hilde Untypedcheck",
                "de",
                (artifact_qid,),
                ("1 NAME Hilde /Untypedcheck/",),
                True,
            ),
            (
                "Agnes Untypedcheck",
                "de",
                (artifact_qid,),
                ("1 NAME Agnes /Untypedcheck/ | 2 TYPE stage",),
                True,
            ),
        ]
    )
    married = statements(client, qid, "married_name")
    assert [entry[0] for entry in married if artifact_qid in entry[2]] == [
        "Ida Untypedcheck-Löbig"
    ]
    # beside the alias, not instead of it
    item = client.wbi.item.get(qid)
    assert "Peppi" in {str(a) for a in item.aliases.get("en") or []}

    rerun = BatchWriter(client).write_batch(
        adapt_gedcom(GEDCOM, artifact=exported).batch
    )
    assert rerun.written == 0
    assert statements(client, qid, "name") == names
