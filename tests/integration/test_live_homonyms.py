"""Live check that two items under one English label break no command
(#94): bootstrap, write, resolve, backup and the site all run with two
same-labelled people on the instance.

Two people of one name are two items under one label. The fixture is
two Eugen Löbigs, father and son: the master hatter (1836-1909) of the
example universe, and a son of the same name, a homonym this test makes
up. The father is minted by the writer from a batch citing the 2008
family history; the son is made under the same label with a description
of his own, as a curator makes a homonym (Wikibase refuses two items
with one label and description, never one label alone), and a second
batch gives him his birth and his father. A third batch names "Eugen
Löbig" again: resolve leaves its ref local, and the writer, which finds
no item by its label (#109), makes it an item of its own beside the
two.

Every label and title carries a marker, eight hex digits of a hash over
the fixture, over the wiki client's, writer's and resolve step's source
and over eight random bytes drawn once a session (conftest.fixture_mark),
so each session meets the pair as a first write.
"""

from __future__ import annotations

import gzip
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.identity import mapped, write_mapped
from yizkr.backup import run_backup
from yizkr.claims import ClaimBatch
from yizkr.core import wiki
from yizkr.core.wiki import WikiClient
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest
from yizkr.pipeline import resolve, writer
from yizkr.pipeline.clustering import IdentityMap
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import publish_site

from .conftest import (
    Registered,
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#94: two Eugen Löbigs, father and son, under one label, from the "
    "2008 family history"
)
MARK = f"(homonym test {fixture_mark(FIXTURE, wiki, writer, resolve)})"
EUGEN = f"Eugen Löbig {MARK}"
FATHER_DESCRIPTION = "master hatter, 1836-1909"
SON_DESCRIPTION = "son of the master hatter, of the same name"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-06T12:00:00+00:00",
}


def eugen(local_id: str = "eugen") -> dict:
    """Eugen Löbig as a local ref names him, by his label alone."""
    return {
        "kind": "local",
        "local_id": local_id,
        "label": EUGEN,
        "description": FATHER_DESCRIPTION,
        "instance_of": "human",
    }


def born(subject: dict, year: int, source: Registered) -> dict:
    return {
        "subject": subject,
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": {"artifact": source.ref.model_dump(mode="json")},
    }


def batch(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


@dataclass(frozen=True)
class Homonyms:
    father: str
    son: str
    source: Registered
    # the third batch, citing the 1984 family tree, and the identity map
    # the father's batch and it are written with, which keeps the two
    # Eugens it names apart
    again: ClaimBatch
    identity: IdentityMap


@pytest.fixture(scope="module")
def homonyms(client: WikiClient, config) -> Homonyms:
    source = register_fictional(
        client, f"Family history 2008 {MARK}", "genealogical_report"
    )
    tree = register_fictional(
        client, f"Family tree 1984 {MARK}", "genealogical_report"
    )
    first_batch = batch(born(eugen(), 1836, source))
    again = batch(born(eugen("eugen-again"), 1836, tree))
    identity = mapped(first_batch, again)
    first = write_mapped(BatchWriter(client), first_batch, identity=identity)
    father = first.created_items["eugen"]
    son = client.wbi.item.new()
    son.labels.set("en", EUGEN)
    son.descriptions.set("en", SON_DESCRIPTION)
    son = son.write(summary="live test: a homonym, as a curator makes one")
    batch_son = batch(
        born({"kind": "qid", "qid": son.id}, 1870, source),
        {
            "subject": {"kind": "qid", "qid": son.id},
            "property": "father",
            "value": {"type": "item", "value": {"kind": "qid", "qid": father}},
            "source": {"artifact": source.ref.model_dump(mode="json")},
        },
        {
            "subject": {"kind": "qid", "qid": son.id},
            "property": "instance_of",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "human"},
            },
            "source": {"artifact": source.ref.model_dump(mode="json")},
        },
    )
    write_mapped(BatchWriter(client), batch_son)
    wait_for_query_service(config, client, [father, son.id])
    return Homonyms(
        father=father,
        son=son.id,
        source=source,
        again=again,
        identity=identity,
    )


def test_the_label_index_holds_both_and_names_the_earliest(client, homonyms):
    _, items = WikiClient(client.config).label_index()

    assert int(homonyms.father[1:]) < int(homonyms.son[1:])
    assert items[EUGEN] == homonyms.father
    assert items.holders(EUGEN) == (homonyms.father, homonyms.son)
    with pytest.raises(RuntimeError, match="share the English label"):
        client.find_item_by_label(EUGEN)


def test_a_fresh_vocabulary_read_survives(client, homonyms):
    properties, items = WikiClient(client.config).vocabulary()

    assert properties["date_of_birth"].startswith("P")
    assert items["human"].startswith("Q")


def test_the_bootstrap_runs_again(client, homonyms):
    fresh = WikiClient(client.config)
    summary = bootstrap_instance(fresh, load_manifest(), log=lambda *_: None)

    assert summary["created"] == 0
    assert summary["updated"] == 0


def test_neither_resolve_nor_the_writer_joins_either(client, homonyms):
    named = homonyms.again
    fresh = WikiClient(client.config)

    assert resolve_batch(named, homonyms.identity) == named

    written = write_mapped(
        BatchWriter(fresh), named, identity=homonyms.identity
    )

    third = written.created_items["eugen-again"]
    assert third not in (homonyms.father, homonyms.son)
    _, items = WikiClient(client.config).label_index()
    assert items.holders(EUGEN) == (homonyms.father, homonyms.son, third)


def test_the_site_builds_a_page_for_each(config, homonyms, tmp_path):
    out = tmp_path / "site"
    try:
        publish_site(config, out)
        for qid in (homonyms.father, homonyms.son):
            assert EUGEN in (out / "people" / f"{qid}.html").read_text()
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_the_backup_runs(config, homonyms, tmp_path):
    if not Path("deploy/docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    manifest = run_backup(
        config, backup_root=tmp_path, deploy_dir=Path("deploy"), keep=0
    )
    out_dir = tmp_path / manifest["timestamp"]
    for component in ("database", "rdf", "nonprivileged_export"):
        assert manifest["components"][component]["bytes"] > 0
    with gzip.open(out_dir / "full.nt.gz", "rt", encoding="utf-8") as dump:
        text = dump.read()
    for qid in (homonyms.father, homonyms.son):
        assert f"/entity/{qid}>" in text
