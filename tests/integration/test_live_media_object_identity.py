"""Live check that a GEDCOM media object and an artifact registered under
its title are never one item, in either order of the writes, and that the
pair is flagged `unhashed-title` either way (#444; ADR-0010).

A media object's item is the GEDCOM import's: a label, `instance of
artifact`, no file and no hash. An artifact is registered by the SHA-1
of its media, and never matched by its title or label. Each test writes
a fictional export, through the real adapter and writer, beside a
fictional scan registered under exactly its media object's title:

* the scan registered first: the export's write makes the media object
  an item of its own under the title with the writer's tag
  (`media_object_label`), never the scan's; the scan's item gains no
  kind, alias or revision from it; the person's `image` is the media
  object's item; and the write's flags name the scan's item against the
  media object's. Written again, the export finds that item again and
  flags the pair again, and so does the scan's registration;
* the export written first: the scan's registration makes a new item,
  content-tagged, flagged against the media object's item, which is left
  as it is; written again, the export finds its media object's own item;
* the scan registered first with two pages, the export's media object
  titled as its first page's fragment: the media object is an item of
  its own, never the fragment, and the write flags the scan's item
  against it.

Fictional synthetic data only (docs/EXAMPLES.md): Sebastian Löbig and the
Löbig family's studio portrait of the 1890s. Every title, name and file
carries a marker, eight hex digits of a hash over the fixture and over
the source of the registrar, the writer, the GEDCOM adapter and the wiki
client, so each version of the fixture and of that code meets the
fixture as a first write does. Like every live test it writes fictional
data to the one instance, so the session that runs it ends by clearing
the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef
from yizkr.core import wiki
from yizkr.pipeline import gedcom, registrar, writer
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import UNHASHED_TITLE, Registrar
from yizkr.pipeline.writer import BatchWriter, media_object_label

from .conftest import fictional_png

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#444: Sebastian Löbig's export, its media object the 1890s studio "
    "portrait, and a scan of the portrait under the media object's title, "
    "the scan registered first, then the export written first, each twice, "
    "and a two-page scan before an export titled as its first page"
)
MARK = hashlib.sha256(
    FIXTURE.encode()
    + b"".join(
        Path(module.__file__).read_bytes()
        for module in (registrar, writer, gedcom, wiki)
    )
).hexdigest()[:8]

# the export, with {order} and the marker in each label it gives an item
TEMPLATE = (
    "0 HEAD\n1 CHAR UTF-8\n"
    "0 @I1@ INDI\n1 NAME Sebastian /Löbig {order} {mark}/\n1 OBJE @O1@\n"
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
    "2 TITL Integration-test studio portrait, {order} {mark}\n"
    "0 TRLR\n"
)


def export(order: str) -> str:
    return TEMPLATE.format(order=order, mark=MARK)


def title(order: str) -> str:
    return f"Integration-test studio portrait, {order} {MARK}"


def scan_bytes(order: str) -> bytes:
    return fictional_png(f"yizkr live test #444: the portrait, {order} {MARK}")


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def write_file(tmp_path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def register_scan(client, tmp_path, order: str, pages: int = 0):
    path = write_file(
        tmp_path,
        f"integration-test-portrait-{order}-{MARK}.png",
        scan_bytes(order),
    )
    return Registrar(client).register_artifact(
        path, "photograph", title(order), description=DESCRIPTION, pages=pages
    )


def write_export(client, tmp_path, order: str, text: str | None = None):
    """The export, `text` or the template's for `order`, registered by its
    hash, then written as `ingest run` writes it: its batch, citing the
    export by that hash."""
    text = text if text is not None else export(order)
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
    return BatchWriter(
        client, artifact_qids={sha1(text.encode()): registered.artifact_qid}
    ).write_batch(batch)


def entity(client, qid: str, props: str = "claims|aliases|info") -> dict:
    return client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": props}
    )["entities"][qid]


def values(client, qid: str, key: str) -> list:
    properties, _ = client.vocabulary()
    found = []
    for statement in (
        entity(client, qid).get("claims", {}).get(properties[key], [])
    ):
        value = statement["mainsnak"]["datavalue"]["value"]
        found.append(value["id"] if isinstance(value, dict) else value)
    return found


def label(client, qid: str) -> str:
    data = entity(client, qid, props="labels")
    return data["labels"]["en"]["value"]


def flagged(result) -> list[tuple]:
    return [(f.kind, f.artifact, f.sha1, f.others) for f in result.flags]


def test_an_export_after_the_scan_never_joins_it(client, tmp_path):
    order = "scan first"
    scan = register_scan(client, tmp_path, order)
    artifact = scan.artifact_qid
    _, items = client.vocabulary()
    before = entity(client, artifact)

    written = write_export(client, tmp_path, order)

    media_object = written.created_items["md1"]
    assert media_object != artifact
    assert label(client, artifact) == title(order)
    assert label(client, media_object) == media_object_label(title(order))
    assert values(client, media_object, "instance_of") == [items["artifact"]]
    assert values(client, media_object, "media_sha1") == []
    after = entity(client, artifact)
    assert after["lastrevid"] == before["lastrevid"]
    assert after.get("aliases") == before.get("aliases")
    assert values(client, artifact, "instance_of") == [items["photograph"]]
    assert values(client, written.created_items["i1"], "image") == [
        media_object
    ]
    flag = (UNHASHED_TITLE, artifact, sha1(scan_bytes(order)), (media_object,))
    assert flagged(written) == [flag]

    # again: the export finds its media object's item, the scan's
    # registration finds the scan's, and each flags the pair again
    again = write_export(client, tmp_path, order)
    assert again.created_items["md1"] == media_object
    assert flagged(again) == [flag]
    rescan = register_scan(client, tmp_path, order)
    assert rescan.artifact_qid == artifact
    assert not rescan.created
    assert flagged(rescan) == [flag]
    assert entity(client, artifact)["lastrevid"] == before["lastrevid"]


def test_a_scan_after_the_export_never_joins_it(client, tmp_path):
    order = "export first"
    written = write_export(client, tmp_path, order)
    media_object = written.created_items["md1"]
    assert label(client, media_object) == title(order)
    before = entity(client, media_object)["lastrevid"]

    scan = register_scan(client, tmp_path, order)

    artifact = scan.artifact_qid
    assert artifact != media_object
    data = sha1(scan_bytes(order))
    assert label(client, artifact) == f"{title(order)} [{data[:8]}]"
    assert flagged(scan) == [(UNHASHED_TITLE, artifact, data, (media_object,))]
    assert entity(client, media_object)["lastrevid"] == before
    assert values(client, media_object, "media_sha1") == []

    again = write_export(client, tmp_path, order)
    assert again.created_items["md1"] == media_object
    assert flagged(again) == []
    assert flagged(register_scan(client, tmp_path, order)) == flagged(scan)


def test_an_export_after_the_scan_never_joins_its_fragment(client, tmp_path):
    order = "fragment, scan first"
    scan = register_scan(client, tmp_path, order, pages=2)
    artifact = scan.artifact_qid
    page = f"{title(order)}, page 1"
    assert label(client, scan.fragment_qids[1]) == page

    written = write_export(
        client, tmp_path, order, export(order).replace(title(order), page)
    )

    media_object = written.created_items["md1"]
    assert media_object not in {artifact, *scan.fragment_qids.values()}
    assert label(client, media_object) == media_object_label(page)
    assert values(client, media_object, "part_of") == []
    assert values(client, written.created_items["i1"], "image") == [
        media_object
    ]
    flag = (UNHASHED_TITLE, artifact, sha1(scan_bytes(order)), (media_object,))
    assert flagged(written) == [flag]
    assert flagged(register_scan(client, tmp_path, order)) == [flag]
