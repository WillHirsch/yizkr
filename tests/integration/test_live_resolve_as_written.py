"""Live check that the resolve step finds the item the writer reuses
(#476; ADR-0010).

`resolve` matches an entity by the label and the side rules the writer
finds its item by: the manifest record's label where there is one, and
the `" [not an artifact]"` tagged label where an artifact holds the plain
one. Each test writes a fictional GEDCOM export, through the real
adapter and writer, then resolves the same export's batch against the
instance and compares it with a dry run of the unresolved batch:

* a GEDCOM person, whose refs carry no label of their own and whose name
  is on the record alone: resolve gives the refs the person's item, the
  one the dry run reuses, and the review sheet shows that QID;
* a person under a scan's label: the writer made the person an item
  under the tagged label, and resolve gives the refs that item, as the
  dry run does. The resolved batch, written, gives the item what the
  local refs would: the person's name first among its aliases, its kind
  once, and the scan's item gains nothing.

Fictional synthetic data only (docs/EXAMPLES.md): Sebastian Löbig and the
Löbig family's studio portrait of the 1890s. Every title, name and file
carries the session's marker (`fixture_mark`, docs/TESTING.md), so no
session meets another's items. Like every live test it writes fictional
data to the one instance, so the session that runs it ends by clearing
the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch, LocalRef, QidRef
from yizkr.core import wiki
from yizkr.pipeline import gedcom, registrar, resolve, writer
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.writer import ENTITY_TAG, BatchWriter

from .conftest import fictional_png, fixture_mark

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#476: Sebastian Löbig's export, its media object the 1890s studio "
    "portrait, written, then its batch resolved and dry-run; and a scan "
    "under Sebastian's name before the export, written, then its batch "
    "resolved, dry-run and written resolved"
)
MARK = fixture_mark(FIXTURE, registrar, writer, resolve, gedcom, wiki)

# the export, with {order} and the marker in each label it gives an item
TEMPLATE = (
    "0 HEAD\n1 CHAR UTF-8\n"
    "0 @I1@ INDI\n1 NAME Sebastian /Löbig {order} {mark}/\n1 OBJE @O1@\n"
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
    "2 TITL Integration-test studio portrait, {order} {mark}\n"
    "0 TRLR\n"
)


def person(order: str) -> str:
    """The label the export gives Sebastian for `order`."""
    return f"Sebastian Löbig {order} {MARK}"


def write_file(tmp_path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def exported(client, tmp_path, order: str) -> tuple[ClaimBatch, dict]:
    """The export for `order`, registered by its hash: its batch, citing
    the export by that hash, and the artifact QIDs a writer is given."""
    text = TEMPLATE.format(order=order, mark=MARK)
    path = write_file(
        tmp_path, f"integration-test-{order}-{MARK}.ged", text.encode()
    )
    registered = Registrar(client).register_artifact(
        path,
        "genealogical_report",
        f"Integration-test export, {order} {MARK}",
        description=DESCRIPTION,
    )
    batch = adapt_gedcom(text, artifact=ArtifactRef.from_path(path)).batch
    sha1 = hashlib.sha1(text.encode()).hexdigest()
    return batch, {sha1: registered.artifact_qid}


def refs_to(batch: ClaimBatch, local_id: str, resolved: ClaimBatch) -> list:
    """What `resolved` holds at each place where `batch` has a local ref
    to `local_id`."""
    found = []
    for before, after in zip(batch.claims, resolved.claims, strict=True):
        for old, new in (
            (before.subject, after.subject),
            (before.value.value, after.value.value),
        ):
            if isinstance(old, LocalRef) and old.local_id == local_id:
                found.append(new)
    return found


def entity(client, qid: str, props: str = "claims|aliases|info") -> dict:
    return client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": props}
    )["entities"][qid]


def values(client, qid: str, key: str) -> list:
    properties, _ = client.vocabulary()
    return [
        statement["mainsnak"]["datavalue"]["value"]["id"]
        for statement in entity(client, qid)
        .get("claims", {})
        .get(properties[key], [])
    ]


def test_a_gedcom_person_resolves_to_the_item_written(client, tmp_path):
    order = "gedcom person"
    batch, artifacts = exported(client, tmp_path, order)
    first = BatchWriter(client, artifact_qids=artifacts).write_batch(batch)
    sebastian = first.created_items["i1"]
    assert all(
        not ref.label
        for claim in batch.claims
        for ref in (claim.subject, claim.value.value)
        if isinstance(ref, LocalRef) and ref.local_id == "i1"
    )

    resolved = resolve_batch(batch, resolvable_labels(client, batch))

    refs = refs_to(batch, "i1", resolved)
    assert refs
    assert all(ref == QidRef(qid=sebastian) for ref in refs)
    dry = BatchWriter(
        client, dry_run=True, artifact_qids=artifacts
    ).write_batch(batch)
    assert dry.created_items["i1"] == sebastian
    sheet = tmp_path / "review.csv"
    export_review_csv(resolved, sheet)
    assert sebastian in sheet.read_text()
    assert "NEW:i1" not in sheet.read_text()


def test_a_person_under_a_scans_label_resolves_to_the_tagged_item(
    client, tmp_path
):
    order = "person under scan"
    data = fictional_png(f"yizkr live test #476: the portrait, {MARK}")
    scan = Registrar(client).register_artifact(
        write_file(
            tmp_path,
            f"integration-test-portrait-{hashlib.sha1(data).hexdigest()[:8]}"
            ".png",
            data,
        ),
        "photograph",
        person(order),
        description=DESCRIPTION,
    )
    artifact = scan.artifact_qid
    batch, artifacts = exported(client, tmp_path, order)
    first = BatchWriter(client, artifact_qids=artifacts).write_batch(batch)
    sebastian = first.created_items["i1"]
    assert sebastian != artifact
    before = entity(client, artifact)

    resolved = resolve_batch(batch, resolvable_labels(client, batch))

    refs = refs_to(batch, "i1", resolved)
    assert refs
    assert all(ref == QidRef(qid=sebastian) for ref in refs)
    dry = BatchWriter(
        client, dry_run=True, artifact_qids=artifacts
    ).write_batch(batch)
    assert dry.created_items["i1"] == sebastian

    written = BatchWriter(client, artifact_qids=artifacts).write_batch(
        resolved
    )
    assert written.refused == []
    labels = entity(client, sebastian, props="labels")["labels"]
    assert labels["en"]["value"] == f"{person(order)}{ENTITY_TAG}"
    aliases = entity(client, sebastian).get("aliases", {}).get("en", [])
    assert [alias["value"] for alias in aliases] == [person(order)]
    _, items = client.vocabulary()
    assert values(client, sebastian, "instance_of") == [items["human"]]
    assert entity(client, artifact)["lastrevid"] == before["lastrevid"]
