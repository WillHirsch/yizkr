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
  against it;
* a scan under the media object's title and another under that title
  tagged as the writer tags it: the media object is refused, with its
  claim, before the first write, the rest of the export (Hildegard and
  her wedding portrait) is written, and the write flags the first scan
  against the second (decision AK);
* the media object titled with the person's name: the person is the
  item under the name, the media object an item of its own under its
  tagged label, unflagged (decision AM);
* a scan registered under the person's name first: the person is an
  item of its own under its name tagged `" [not an artifact]"`, its
  name an alias, the scan's item unchanged, and resolve's labels leave
  the scan out (decision AN); a ref with no kind naming him, written
  after, joins that item, as the tag is one for every kind (decision
  AZ);
* Sebastian's portrait titled with Hildegard's name, his claim naming
  it before hers names her: the people are placed first, so she keeps
  her name and the portrait takes its tagged label (decision AN);
* Sebastian written, then a scan registered under his name, then an
  export whose media object is titled with his name: the scan is
  content-tagged, the media object takes its tagged label, and the
  write flags the scan against it, as registration would in the other
  order.

Fictional synthetic data only (docs/EXAMPLES.md): Sebastian and
Hildegard Löbig, the Löbig family's studio portrait of the 1890s and
the 1905 wedding portrait. Every title, name and file
carries a marker, eight hex digits of a hash over the fixture, over
the source of the registrar, the writer, resolve, the GEDCOM adapter and
the wiki client, and over eight random bytes drawn once a session, so
each session, and each version of the fixture and of that code, meets the
fixture as a first write does: an earlier session's items would change
what a later one is flagged for, as a scan registered again under a
title an earlier session's media object holds is flagged against it.
Within a session a rewrite finds its own items again. Like every live
test it writes fictional
data to the one instance, so the session that runs it ends by clearing
the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core import wiki
from yizkr.pipeline import gedcom, registrar, resolve, writer
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import UNHASHED_TITLE, Registrar
from yizkr.pipeline.resolve import resolvable_labels
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import ENTITY_TAG, BatchWriter, media_object_label

from .conftest import fictional_png

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#444: Sebastian Löbig's export, its media object the 1890s studio "
    "portrait, and a scan of the portrait under the media object's title, "
    "the scan registered first, then the export written first, each twice, "
    "and a two-page scan before an export titled as its first page; "
    "scans under the title and the tagged title before an export with "
    "Hildegard's wedding portrait too, written twice; an export whose "
    "media object is titled with Sebastian's name, written twice; and a "
    "scan under Sebastian's name before the export, written twice; an "
    "export whose Sebastian's portrait is titled with Hildegard's name, "
    "written twice; and Sebastian written, a scan under his name, then "
    "an export whose media object is titled with his name; a ref with no "
    "kind naming Sebastian after the scan under his name, the one tag "
    '" [not an artifact]" for every kind'
)
MARK = hashlib.sha256(
    FIXTURE.encode()
    + b"".join(
        Path(module.__file__).read_bytes()
        for module in (registrar, writer, resolve, gedcom, wiki)
    )
    + os.urandom(8)
).hexdigest()[:8]

# the export, with {order} and the marker in each label it gives an item
TEMPLATE = (
    "0 HEAD\n1 CHAR UTF-8\n"
    "0 @I1@ INDI\n1 NAME Sebastian /Löbig {order} {mark}/\n1 OBJE @O1@\n"
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
    "2 TITL Integration-test studio portrait, {order} {mark}\n"
    "0 TRLR\n"
)


# Hildegard and her wedding portrait, to add before the export's TRLR
HILDEGARD = (
    "0 @I2@ INDI\n1 NAME Hildegard /Löbig {order} {mark}/\n1 OBJE @O2@\n"
    "0 @O2@ OBJE\n1 FILE wedding-1905.jpg\n"
    "2 TITL Integration-test wedding portrait, {order} {mark}\n"
)


def export(order: str) -> str:
    return TEMPLATE.format(order=order, mark=MARK)


def person(order: str) -> str:
    """The label the export gives Sebastian for `order`."""
    return f"Sebastian Löbig {order} {MARK}"


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


def register_scan(
    client, tmp_path, order: str, pages: int = 0, named: str | None = None
):
    """The scan for `order`, registered under `named`, by default the
    media object's title for `order`; another name gives other bytes."""
    data = scan_bytes(order if named is None else f"{order}, {named}")
    path = write_file(
        tmp_path,
        f"integration-test-portrait-{hashlib.sha1(data).hexdigest()[:8]}.png",
        data,
    )
    return Registrar(client).register_artifact(
        path,
        "photograph",
        title(order) if named is None else named,
        description=DESCRIPTION,
        pages=pages,
    )


def write_export(
    client, tmp_path, order: str, text: str | None = None, kindless=False
):
    """The export, `text` or the template's for `order`, registered by its
    hash, then written as `ingest run` writes it: its batch, citing the
    export by that hash. `kindless` makes its claim's subject a ref with
    no kind and no record, under Sebastian's name, as an extraction may
    give one."""
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
    if kindless:
        given = batch.model_dump(mode="json")
        for claim in given["claims"]:
            claim["subject"] = {
                "kind": "local",
                "local_id": "e1",
                "label": person(order),
            }
        given["entities"] = [
            record for record in given["entities"] if record["id"] != "i1"
        ]
        batch = ClaimBatch.model_validate(given)
    return BatchWriter(
        client, artifact_qids={sha1(text.encode()): registered.artifact_qid}
    ).write_batch(batch)


def naming(order: str, *labels: str) -> ClaimBatch:
    """The export's first claim once for each of `labels`, its subject a
    ref with that label, no kind and no record."""
    text = export(order)
    exported = ArtifactRef.from_bytes(text.encode())
    given = adapt_gedcom(text, artifact=exported).batch.model_dump(mode="json")
    claim = given["claims"][0]
    given["claims"] = [
        dict(
            claim,
            subject={"kind": "local", "local_id": f"e{n}", "label": label},
        )
        for n, label in enumerate(labels)
    ]
    return ClaimBatch.model_validate(given)


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


def test_a_media_object_whose_two_labels_are_registered_is_refused(
    client, tmp_path
):
    order = "both labels"
    first = register_scan(client, tmp_path, order).artifact_qid
    second = register_scan(
        client, tmp_path, order, named=media_object_label(title(order))
    ).artifact_qid
    assert label(client, second) == media_object_label(title(order))
    text = export(order).replace(
        "0 TRLR\n", HILDEGARD.format(order=order, mark=MARK) + "0 TRLR\n"
    )
    batch = adapt_gedcom(text, artifact=ArtifactRef.from_bytes(text.encode()))
    named = [
        claim_id(claim)
        for claim in batch.batch.claims
        if claim.value.value.local_id == "md1"
    ]
    before = {qid: entity(client, qid)["lastrevid"] for qid in (first, second)}

    for _ in range(2):
        written = write_export(client, tmp_path, order, text)

        assert written.refused == named
        assert {"i1", "md1"}.isdisjoint(written.created_items)
        assert values(client, written.created_items["i2"], "image") == [
            written.created_items["md2"]
        ]
        assert flagged(written) == [
            (UNHASHED_TITLE, first, sha1(scan_bytes(order)), (second,))
        ]
        (warning,) = [w for w in written.warnings if "'md1'" in w]
        assert first in warning and second in warning
        assert MARK not in warning
    assert {
        qid: entity(client, qid)["lastrevid"] for qid in (first, second)
    } == before


def test_a_portrait_titled_as_its_sitter_is_an_item_of_its_own(
    client, tmp_path
):
    order = "sitter"
    _, items = client.vocabulary()
    text = export(order).replace(
        f"2 TITL {title(order)}\n", f"2 TITL {person(order)}\n"
    )

    written = write_export(client, tmp_path, order, text)

    sebastian = written.created_items["i1"]
    media_object = written.created_items["md1"]
    assert sebastian != media_object
    assert label(client, sebastian) == person(order)
    assert label(client, media_object) == media_object_label(person(order))
    assert items["artifact"] not in values(client, sebastian, "instance_of")
    assert values(client, sebastian, "image") == [media_object]
    assert flagged(written) == []
    again = write_export(client, tmp_path, order, text)
    assert again.created_items == written.created_items


def test_a_person_under_a_scans_label_is_an_item_of_its_own(client, tmp_path):
    order = "person after scan"
    scan = register_scan(client, tmp_path, order, named=person(order))
    artifact = scan.artifact_qid
    before = entity(client, artifact)

    written = write_export(client, tmp_path, order)

    sebastian = written.created_items["i1"]
    assert sebastian != artifact
    assert label(client, sebastian) == f"{person(order)}{ENTITY_TAG}"
    assert ENTITY_TAG == " [not an artifact]"
    aliases = entity(client, sebastian).get("aliases", {}).get("en", [])
    assert [alias["value"] for alias in aliases] == [person(order)]
    after = entity(client, artifact)
    assert after["lastrevid"] == before["lastrevid"]
    assert label(client, artifact) == person(order)
    assert flagged(written) == []
    # the refs resolve reads the labels of: Sebastian, under his name and
    # under his tagged label
    named = (person(order), f"{person(order)}{ENTITY_TAG}")
    labels = resolvable_labels(client, naming(order, *named))
    assert person(order) not in labels
    assert labels[f"{person(order)}{ENTITY_TAG}"] == sebastian
    again = write_export(client, tmp_path, order)
    assert again.created_items["i1"] == sebastian
    kindless = write_export(client, tmp_path, order, kindless=True)
    assert kindless.created_items["e1"] == sebastian
    assert kindless.refused == []
    assert entity(client, artifact)["lastrevid"] == before["lastrevid"]
    assert label(client, artifact) == person(order)
    labels = resolvable_labels(client, naming(order, *named))
    assert person(order) not in labels
    assert labels[f"{person(order)}{ENTITY_TAG}"] == sebastian


def test_a_portrait_titled_with_another_persons_name_leaves_her_it(
    client, tmp_path
):
    order = "portrait of another"
    hildegard = f"Hildegard Löbig {order} {MARK}"
    text = (
        export(order)
        .replace(f"2 TITL {title(order)}\n", f"2 TITL {hildegard}\n")
        .replace(
            "0 TRLR\n", HILDEGARD.format(order=order, mark=MARK) + "0 TRLR\n"
        )
    )

    written = write_export(client, tmp_path, order, text)

    her, portrait = (written.created_items[i] for i in ("i2", "md1"))
    assert her != portrait
    assert label(client, her) == hildegard
    assert label(client, portrait) == media_object_label(hildegard)
    assert values(client, written.created_items["i1"], "image") == [portrait]
    assert flagged(written) == []
    again = write_export(client, tmp_path, order, text)
    assert again.created_items == written.created_items


def test_a_scan_under_a_persons_name_is_flagged_against_the_media_object(
    client, tmp_path
):
    order = "person, scan, export"
    first = write_export(client, tmp_path, order)
    sebastian = first.created_items["i1"]
    scan = register_scan(client, tmp_path, order, named=person(order))
    artifact = scan.artifact_qid
    data = sha1(scan_bytes(f"{order}, {person(order)}"))
    assert label(client, artifact) == f"{person(order)} [{data[:8]}]"
    assert flagged(scan) == []
    text = export(order).replace(
        f"2 TITL {title(order)}\n", f"2 TITL {person(order)}\n"
    )

    written = write_export(client, tmp_path, f"{order}, again", text)

    media_object = written.created_items["md1"]
    assert written.created_items["i1"] == sebastian
    assert label(client, sebastian) == person(order)
    assert label(client, media_object) == media_object_label(person(order))
    assert flagged(written) == [
        (UNHASHED_TITLE, artifact, data, (media_object,))
    ]
