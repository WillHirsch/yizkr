"""Live check that two items under one English label break no command
(#94): the backup runs with two same-labelled people on the instance. The
static site's page for each of two such people is checked in the required
lane (tests/fictional_integration/test_static_site.py).

Two people of one name are two items under one label. The fixture is
two Eugen Löbigs, father and son: the master hatter (1836-1909) of the
example universe, and a son of the same name, a homonym this test makes
up. The father is minted by the writer from a batch citing the 2008
family history; the son is made under the same label with a description
of his own, as a curator makes a homonym (Wikibase refuses two items
with one label and description, never one label alone), and a second
batch gives him his birth and his father.

Every label and title carries a marker, eight hex digits of a hash over
the fixture, over the wiki client's and writer's source and over eight
random bytes drawn once a session (conftest.fixture_mark), so each
session meets the pair as a first write.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.backup import run_backup
from yizkr.claims import ClaimBatch
from yizkr.core import wiki
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

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
MARK = f"(homonym test {fixture_mark(FIXTURE, wiki, writer)})"
EUGEN = f"Eugen Löbig {MARK}"
FATHER_DESCRIPTION = "master hatter, 1836-1909"
SON_DESCRIPTION = "son of the master hatter, of the same name"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-06T12:00:00+00:00",
}


def eugen() -> dict:
    """Eugen Löbig as a local ref names him, by his label alone."""
    return {
        "kind": "local",
        "local_id": "eugen",
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


@pytest.fixture(scope="module")
def homonyms(client: WikiClient, config) -> Homonyms:
    source = register_fictional(
        client, f"Family history 2008 {MARK}", "genealogical_report"
    )
    first = write_mapped(
        BatchWriter(client), batch(born(eugen(), 1836, source))
    )
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
    return Homonyms(father=father, son=son.id)


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
