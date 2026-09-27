"""Offline tests for the corpus-clustering calibration experiment (#71).

Tiny fictional manifests only; no instance, no LLM.
"""

import csv
import importlib.util
import json
import re
import stat
import sys
from pathlib import Path

import pytest

from yizkr.claims import ClaimBatch
from yizkr.pipeline.reconcile import impossible_pair

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "cluster_calibration.py"
)
_spec = importlib.util.spec_from_file_location("cluster_calibration", SCRIPT)
cc = importlib.util.module_from_spec(_spec)
sys.modules["cluster_calibration"] = cc
_spec.loader.exec_module(cc)

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-13T09:00:00Z",
}
SOURCE = {"artifact": {"kind": "qid", "qid": "Q1"}}

GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @HILDEGARD@ INDI
1 NAME Hildegard /Löbig/
1 BIRT
2 DATE 1876
0 @LEOPOLD@ INDI
1 NAME Leopold /Löbig/
0 @AGNES@ INDI
1 NAME Agnes /Nertler/
1 BIRT
2 PLAC Kredfeld, Hesse
0 @URSULAA@ INDI
1 NAME Ursula /Löbig/
0 @URSULAB@ INDI
1 NAME Ursula /Löbig/
0 @FAMILY@ FAM
1 HUSB @LEOPOLD@
1 WIFE @AGNES@
0 TRLR
"""


def local(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def claim(subject: str, prop: str, value: dict) -> dict:
    return {
        "subject": local(subject),
        "property": prop,
        "value": value,
        "source": SOURCE,
    }


def item(local_id: str) -> dict:
    return {"type": "item", "value": local(local_id)}


def year(value: int, precision: str | None = None) -> dict:
    time = {"type": "time", "year": value}
    if precision:
        time["precision"] = precision
    return time


def person(local_id: str, label: str, **extra) -> dict:
    return {"id": local_id, "label": label, "kind": "human", **extra}


def manifest(sha1: str, entities, claims=(), source="document"):
    batch = ClaimBatch.model_validate(
        {"extraction_run": RUN, "entities": entities, "claims": list(claims)}
    )
    return cc.Manifest(sha1, batch, source)


def mentions(*manifests):
    found = []
    for each in manifests:
        found.extend(cc.mentions_from_manifest(each))
    return found, {m.uid: m for m in found}


def by_label(found, label: str, sha1: str | None = None):
    return next(
        m for m in found if m.label == label and (not sha1 or m.sha1 == sha1)
    )


def gedcom_manifest(tmp_path: Path):
    path = tmp_path / "tree.ged"
    path.write_text(GEDCOM)
    return cc.load_gedcom(path)


# -- keys ---------------------------------------------------------------------


def test_name_forms_drop_titles_and_split_maiden_names():
    assert cc.name_forms("Dr. Hildegard Löbig") == [["hildegard", "lobig"]]
    assert cc.name_forms("Frau Hildegard Löbig geb. Laudler") == [
        ["hildegard", "lobig"],
        ["hildegard", "laudler"],
    ]
    assert cc.name_forms("Cäcilie Löbig") == [["cacilie", "lobig"]]


def test_human_key_variants():
    found, _ = mentions(
        manifest("a" * 40, [person("e1", "Hildegard Laudler Löbig")])
    )
    hildegard = found[0]
    assert cc.human_keys(hildegard, "final-surname") == {
        ("hildegard", "lobig"),
        ("laudler", "lobig"),
    }
    # a maiden name kept in the middle still keys
    assert ("hildegard", "laudler") in cc.human_keys(
        hildegard, "any-later-token"
    )


def test_kin_surnames_key_a_married_name_the_source_omits():
    # the document calls her by her married name; the tree by her birth
    # name, but it records the spouse
    doc = manifest(
        "a" * 40,
        [person("e1", "Agnes Löbig"), person("e2", "Leopold Löbig")],
        [claim("e1", "spouse", item("e2"))],
    )
    tree = manifest(
        "b" * 40,
        [person("i1", "Agnes Nertler"), person("i2", "Leopold Löbig")],
        [claim("i1", "spouse", item("i2"))],
        source="gedcom",
    )
    found, _ = mentions(doc, tree)
    agnes_doc = by_label(found, "Agnes Löbig")
    agnes_tree = by_label(found, "Agnes Nertler")
    assert not cc.keys_match(
        cc.human_keys(agnes_doc, "final-surname"),
        cc.human_keys(agnes_tree, "final-surname"),
    )
    assert cc.keys_match(
        cc.human_keys(agnes_doc, "kin-surnames"),
        cc.human_keys(agnes_tree, "kin-surnames"),
    )


def test_given_names_match_by_prefix_not_by_surname_alone():
    found, _ = mentions(
        manifest(
            "a" * 40,
            [
                person("e1", "Hilde Löbig"),
                person("e2", "Hildegard Löbig"),
                person("e3", "Sebastian Löbig"),
            ],
        )
    )
    hilde, hildegard, sebastian = (cc.human_keys(m) for m in found)
    assert cc.keys_match(hilde, hildegard)
    assert not cc.keys_match(hilde, sebastian)


def test_place_key_variants():
    found, _ = mentions(
        manifest(
            "a" * 40,
            [
                {
                    "id": "e1",
                    "label": "Lager Klurnau, Klurnau",
                    "kind": "place",
                },
                {"id": "e2", "label": "Klurnau", "kind": "place"},
                {
                    "id": "e3",
                    "label": "8 Hawthorn Road, Stupfield",
                    "kind": "place",
                },
            ],
        )
    )
    camp, town, address = found
    assert cc.place_keys(camp, "first-component") == {"lager klurnau"}
    assert cc.place_keys(camp) == {"lager", "klurnau"}
    assert not cc.place_keys(camp, "first-component") & cc.place_keys(
        town, "first-component"
    )
    assert cc.place_keys(camp) & cc.place_keys(town)
    # A token of exactly four characters ("road") is a key.
    assert cc.place_keys(address, "first-component") == {"8 hawthorn road"}
    assert cc.place_keys(address) == {"hawthorn", "road"}


# -- blocking -----------------------------------------------------------------


def two_documents():
    first = manifest(
        "a" * 40,
        [person("e1", "Hildegard Löbig"), person("e2", "Sebastian Löbig")],
    )
    second = manifest("b" * 40, [person("e1", "Hildegard Löbig")])
    return mentions(first, second)


def test_one_token_blocks_a_shared_surname_name_key_does_not():
    found, index = two_documents()
    one_token = cc.block_one_token(found)
    name_key = cc.block_name_key(found)
    assert len(one_token) == 3  # every pair shares "lobig"
    assert name_key == {
        cc.pair_key(f"{'a' * 40}:e1", f"{'b' * 40}:e1"),
    }


def test_events_block_through_their_participants():
    first = manifest(
        "a" * 40,
        [
            person("e1", "Hildegard Löbig"),
            {"id": "e2", "label": "Death of Hildegard", "kind": "death"},
        ],
        [claim("e2", "participant", item("e1"))],
    )
    second = manifest(
        "b" * 40,
        [
            person("e1", "Hildegard Löbig"),
            {"id": "e2", "label": "Burial register", "kind": "death"},
        ],
        [claim("e2", "participant", item("e1"))],
    )
    found, _ = mentions(first, second)
    pairs = cc.block_name_key(found)
    assert cc.pair_key(f"{'a' * 40}:e2", f"{'b' * 40}:e2") in pairs


def test_gedcom_anchored_pairs_mentions_only_with_their_anchor(tmp_path):
    tree = gedcom_manifest(tmp_path)
    first = manifest("a" * 40, [person("e1", "Hildegard Löbig")])
    second = manifest("b" * 40, [person("e1", "Hildegard Löbig")])
    found, index = mentions(first, second, tree)
    name_key = cc.block_name_key(found)
    anchored, anchors = cc.block_gedcom_anchored(found, name_key, index)
    anchor = by_label(found, "Hildegard Löbig", tree.sha1).uid
    first_one, second_one = f"{'a' * 40}:e1", f"{'b' * 40}:e1"
    assert cc.pair_key(first_one, second_one) in name_key
    documents = {
        pair
        for pair in anchored
        if not all(index[uid].source == "gedcom" for uid in pair)
    }
    assert documents == {
        cc.pair_key(first_one, anchor),
        cc.pair_key(second_one, anchor),
    }
    assert anchors == {first_one: {anchor}, second_one: {anchor}}


def test_keyless_people_pair_with_gedcom_given_names(tmp_path):
    tree = gedcom_manifest(tmp_path)
    doc = manifest("a" * 40, [person("e1", "Hildegard")])
    found, _ = mentions(doc, tree)
    assert cc.partial_anchor_pairs(found) == {
        cc.pair_key(f"{'a' * 40}:e1", by_label(found, "Hildegard Löbig").uid)
    }


# -- constraints --------------------------------------------------------------


def father_and_son():
    """One document keeps two same-named people apart by a kin edge
    (placeholder names: the example universe has no same-named father
    and son)."""
    return manifest(
        "a" * 40,
        [person("e1", "Xxx Yyy"), person("e2", "Xxx Yyy")],
        [claim("e2", "father", item("e1"))],
    )


def test_impossible_pair_fires_only_after_the_lift():
    doc = father_and_son()
    found, _ = mentions(doc)
    father, son = found
    assert (
        impossible_pair(cc.lifted_facts(father), cc.lifted_facts(son))
        == "kinship-edge"
    )
    batches = {doc.sha1: doc.batch}
    result = cc.apply_constraints(
        {cc.pair_key(father.uid, son.uid)},
        {m.uid: m for m in found},
        cc.lifted_containment(found),
        batches,
    )
    assert len(result.kin_edge) == 1
    assert result.unlifted_pruned == 0  # local-id kin is invisible unlifted
    assert not result.remaining


def test_gedcom_places_are_not_cannot_link(tmp_path):
    places = manifest(
        "g" * 40,
        [
            {"id": "pl1", "label": "Kredfeld", "kind": "place"},
            {"id": "pl2", "label": "Kredfeld, Hesse", "kind": "place"},
        ],
        source="gedcom",
    )
    people = manifest(
        "a" * 40,
        [person("e1", "Hildegard Löbig"), person("e2", "Sebastian Löbig")],
    )
    found, _ = mentions(places, people)
    kredfeld, kredfeld_hesse, hildegard, sebastian = found
    assert not cc.cannot_link(kredfeld, kredfeld_hesse)
    assert cc.cannot_link(hildegard, sebastian)


def test_lifted_containment_prunes_a_place_against_its_container():
    doc = manifest(
        "a" * 40,
        [
            {"id": "e1", "label": "Lager Klurnau", "kind": "place"},
            {"id": "e2", "label": "Klurnau", "kind": "place"},
        ],
        [claim("e1", "part_of", item("e2"))],
    )
    other = manifest(
        "b" * 40, [{"id": "e1", "label": "Klurnau", "kind": "place"}]
    )
    found, index = mentions(doc, other)
    camp = by_label(found, "Lager Klurnau")
    town = by_label(found, "Klurnau", "b" * 40)
    containment = cc.lifted_containment(found)
    assert (
        impossible_pair(
            cc.lifted_facts(town),
            cc.lifted_facts(camp),
            containment=containment,
        )
        == "place-containment"
    )


# -- evidence -----------------------------------------------------------------


def dated(sha1: str, label: str, born: dict):
    return manifest(
        sha1, [person("e1", label)], [claim("e1", "date_of_birth", born)]
    )


def test_dates_compare_at_the_sources_precision():
    found, index = mentions(
        dated("a" * 40, "Hildegard Löbig", year(1870, "decade")),
        dated("b" * 40, "Hildegard Löbig", year(1876)),
        dated("c" * 40, "Hildegard Löbig", year(1862)),
    )
    decade, exact, early = found
    agree = cc.pair_evidence(decade, exact, index)
    assert agree.dates == "agree"
    assert cc.classify_pair(decade, exact, agree, index) == "confident"
    clash = cc.pair_evidence(exact, early, index)
    assert clash.dates == "conflict"
    # a date conflict is a healthy coexisting state: surfaced, not pruned
    assert cc.classify_pair(exact, early, clash, index) == "ambiguous"


def test_a_bare_name_match_is_ambiguous():
    found, index = two_documents()
    first, _, second = found
    evidence = cc.pair_evidence(first, second, index)
    assert evidence.name == "exact"
    assert cc.classify_pair(first, second, evidence, index) == "ambiguous"


def test_places_nested_parts_are_confident():
    found, index = mentions(
        manifest(
            "a" * 40, [{"id": "e1", "label": "Fläckfeld", "kind": "place"}]
        ),
        manifest(
            "b" * 40,
            [
                {
                    "id": "e1",
                    "label": "Fläckfeld, Kreis Mockburg",
                    "kind": "place",
                }
            ],
        ),
        manifest(
            "c" * 40,
            [
                {
                    "id": "e1",
                    "label": "Fläckfeld, Kreis Peiltal",
                    "kind": "place",
                }
            ],
        ),
    )
    bare, mockburg, peiltal = found
    nested = cc.pair_evidence(bare, mockburg, index)
    rival = cc.pair_evidence(mockburg, peiltal, index)
    assert (nested.name, rival.name) == ("nested", "key")
    assert cc.classify_pair(bare, mockburg, nested, index) == "confident"
    assert cc.classify_pair(mockburg, peiltal, rival, index) == "ambiguous"


# -- closure ------------------------------------------------------------------


def test_closure_that_joins_a_kin_edge_is_conflicting():
    other = manifest("b" * 40, [person("e1", "Xxx Yyy")])
    doc = father_and_son()
    found, index = mentions(doc, other)
    result = cc.run_strategy(
        "name-key",
        cc.block_name_key(found),
        found,
        index,
        cc.lifted_containment(found),
        {doc.sha1: doc.batch, other.sha1: other.batch},
    )
    (cluster,) = [row for row in result.clusters if len(row.members) > 1]
    assert cluster.status == "conflicting-hard"
    assert len(cluster.members) == 3
    (group,) = cc.homonym_groups(found)
    assert group["status"] == "hard"


def test_components_are_transitive_and_keep_singletons():
    assert cc.components(["a", "b", "c", "d"], [("a", "b"), ("b", "c")]) == [
        ["a", "b", "c"],
        ["d"],
    ]


# -- estimates ----------------------------------------------------------------


def test_adjudication_chunks_keep_clusters_together_and_split_big_ones():
    pairs = [(f"x{i}", f"y{i}") for i in range(95)]
    chunks = cc.adjudication_chunks(
        {"big": pairs[:85], "small": pairs[85:]}, 40
    )
    assert [sum(len(p) for p in chunk.values()) for chunk in chunks] == [
        40,
        40,
        15,
    ]


def test_serial_per_artifact_rows_shrink_when_matches_merge():
    docs = [
        manifest(sha * 40, [person("e1", "Hildegard Löbig")]) for sha in "abc"
    ]
    plain = cc.simulate_serial(docs, 40)
    assert plain.rows == 0 + 1 + 2  # every earlier mention is a candidate
    merged = cc.simulate_serial(
        docs, 40, {f"{sha * 40}:e1": "hildegard" for sha in "abc"}
    )
    assert merged.rows == 0 + 1 + 1


# -- end to end ---------------------------------------------------------------


def write_run(root: Path, name: str, sha1: str, doc) -> Path:
    run = root / name
    run.mkdir()
    (run / "batch.json").write_text(doc.batch.model_dump_json())
    (run / "state.json").write_text(json.dumps({"content_sha1": sha1}))
    return run


def corpus(tmp_path: Path) -> tuple[list[Path], Path]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    ged = tmp_path / "tree.ged"
    ged.write_text(GEDCOM)
    runs = [
        write_run(
            runs_root,
            "one",
            "a" * 40,
            manifest(
                "a" * 40,
                [
                    person("e1", "Hildegard Löbig"),
                    person("e2", "Agnes Löbig"),
                    person("e3", "Leopold Löbig"),
                ],
                [claim("e2", "spouse", item("e3"))],
            ),
        ),
        write_run(
            runs_root,
            "two",
            "b" * 40,
            manifest(
                "b" * 40,
                [person("e1", "Hildegard"), person("e2", "Sebastian")],
            ),
        ),
    ]
    equivalences = runs[0] / "equivalences.csv"
    with open(equivalences, "w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["entity_id", "entity_label", "aliases", "qid",
                        "qid_label", "verdict", "confidence", "evidence",
                        "decision"],
        )  # fmt: skip
        writer.writeheader()
        writer.writerow(
            {"entity_id": "e1", "qid": "Q5", "qid_label": "Hildegard Löbig",
             "verdict": "same", "confidence": "0.95"}
        )  # fmt: skip
        writer.writerow(
            {"entity_id": "e2", "qid": "Q6", "qid_label": "Agnes Nertler",
             "verdict": "same", "confidence": "0.85"}
        )  # fmt: skip
    return runs, ged


def test_report_is_aggregate_only(tmp_path):
    runs, ged = corpus(tmp_path)
    summary, _ = cc.run_experiment(runs, ged, runs)
    report = cc.render_report(summary)
    words = set(re.findall(r"[^\W\d_]+", report.lower()))
    names = {"hildegard", "löbig", "lobig", "agnes", "nertler", "leopold"}
    names |= {"ursula", "sebastian", "kredfeld", "hesse"}
    assert "kin" in words  # the report's own vocabulary is seen
    assert not names & words
    assert summary["verdicts"]["rows"] == 2
    kept = summary["verdicts"]["human_key_variants"]
    assert kept["final-surname"] == {"same": 1}
    assert kept["kin-surnames"] == {"same": 2}
    # GEDCOM ids derive from the export's xrefs: refs use positions
    assert summary["homonyms"]["examples"][0].count(":ged") == 2


def test_main_writes_private_outputs_outside_the_repo(tmp_path):
    runs, ged = corpus(tmp_path)
    out = tmp_path / "out"
    argv = ["--runs", *map(str, runs), "--gedcom", str(ged), "--out", str(out)]
    assert cc.main(argv) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert {"report.md", "summary.json", "pairs-name-key.csv"} <= {
        path.name for path in out.iterdir()
    }
    with pytest.raises(SystemExit):
        cc.main([*argv[:-1], str(SCRIPT.parent / "out")])
