"""Live check that registration flags, and never adopts, a new item that
may be an artifact registered before content hashing (#371; ADR-0010).

The fixture makes such an item as registration made one before content
hashing: a label, `instance of photograph` and a `media file` naming a
file it uploaded, each stated in the item itself (ADR-0003), and no
`media SHA-1`. Then:

* other bytes registered under its label make a new item, content-tagged,
  flagged `unhashed-title`;
* its own bytes registered under another title and file name upload to
  its file (MediaWiki's `duplicate`), and make a new item that shares the
  file, flagged `shared-file`;

and the unhashed item is left as it is: its revision does not move, and
it never gains a `media SHA-1`. A re-run of the same version finds both
new items by their SHA-1 and flags nothing.

Fictional synthetic media only (docs/EXAMPLES.md): the Löbig family's
studio portrait of the 1890s. Every title, file name and label carries a
marker, eight hex digits of a hash over the fixture and over the
registrar's and the wiki client's source, so each version of the fixture
and of that code meets the fixture as a first registration does: a
mutant of the registrar cannot pass by finding what an earlier version
made. Like every live test it writes fictional data to the one instance,
so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from yizkr.core import wiki
from yizkr.pipeline import registrar
from yizkr.pipeline.registrar import (
    SHARED_FILE,
    UNHASHED_TITLE,
    Registrar,
    _ensure_referenced,
    _item_claim,
    _string_claim,
)

from .conftest import fictional_png

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#371: the Löbig family's 1890s studio portrait, registered before "
    "content hashing, then a re-scan under its title and its own bytes "
    "under another"
)
MARK = hashlib.sha256(
    FIXTURE.encode()
    + Path(registrar.__file__).read_bytes()
    + Path(wiki.__file__).read_bytes()
).hexdigest()[:8]

PORTRAIT = fictional_png(f"yizkr live test #371: the portrait {MARK}")
RESCAN = fictional_png(f"yizkr live test #371: the re-scan {MARK}")
LABEL = f"Integration-test unhashed portrait {MARK}"
FILE_NAME = f"Integration-test unhashed portrait {MARK}.png"
COPY_TITLE = f"Integration-test portrait copy {MARK}"
COPY_NAME = f"integration-test-portrait-copy-{MARK}.png"
RESCAN_NAME = f"integration-test-portrait-rescan-{MARK}.png"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def write(tmp_path, folder: str, name: str, data: bytes) -> Path:
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def labelled(client, label: str) -> list[str]:
    """Every item with this English label, by a lag-free reading."""
    return [
        qid
        for qid, entity in client.iter_entities("labels")
        if qid.startswith("Q")
        and ((entity.get("labels") or {}).get("en") or {}).get("value")
        == label
    ]


def unhashed_item(client, tmp_path) -> str:
    """The fixture's item with no hash, made on the first run and found by
    its label on a re-run; a run cut short between its two writes leaves
    it labelled, and the next gives it its statements."""
    properties, items = client.vocabulary()
    found = labelled(client, LABEL)
    assert len(found) <= 1, found
    if found:
        item = client.wbi.item.get(found[0])
    else:
        item = client.wbi.item.new()
        item.labels.set("en", LABEL)
        item.descriptions.set("en", DESCRIPTION)
        item = item.write(summary="integration test: unhashed artifact")
    page = client.upload_file(
        write(tmp_path, "unhashed", FILE_NAME, PORTRAIT), filename=FILE_NAME
    )
    stated_in = properties["stated_in"]
    changed = False
    for claim in (
        _item_claim(
            properties["instance_of"], items["photograph"], stated_in, item.id
        ),
        _string_claim(properties["media_file"], page, stated_in, item.id),
    ):
        outcome = _ensure_referenced(item, claim, stated_in, item.id)
        changed = changed or outcome != "present"
    if changed:
        item = item.write(summary="integration test: unhashed artifact")
    return item.id


def entity(client, qid: str, props: str = "claims") -> dict:
    return client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": props}
    )["entities"][qid]


def revision(client, qid: str) -> int:
    return entity(client, qid, props="info")["lastrevid"]


def strings(client, qid: str, key: str) -> list[str]:
    properties, _ = client.vocabulary()
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in entity(client, qid)
        .get("claims", {})
        .get(properties[key], [])
    ]


def hashed(client, data: bytes) -> bool:
    properties, _ = client.vocabulary()
    index = client.string_statement_index(properties["media_sha1"])
    return sha1(data) in index


def test_the_fixture_item_is_unhashed(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    assert strings(client, old, "media_sha1") == []
    assert strings(client, old, "media_file") == [f"File:{FILE_NAME}"]
    assert old in Registrar(client)._read().unhashed


def test_other_bytes_under_its_label_are_flagged(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    before = revision(client, old)
    fresh = not hashed(client, RESCAN)
    path = write(tmp_path, "rescan", RESCAN_NAME, RESCAN)

    result = Registrar(client).register_artifact(
        path, "photograph", LABEL, description=DESCRIPTION
    )

    new = result.artifact_qid
    assert new != old
    assert result.created is fresh
    if fresh:
        assert [(f.kind, f.artifact, f.others) for f in result.flags] == [
            (UNHASHED_TITLE, new, (old,))
        ]
    else:
        # a re-run of this version: found by its SHA-1, nothing flagged
        assert result.flags == []
    assert strings(client, new, "media_sha1") == [sha1(RESCAN)]
    assert result.file_page != f"File:{FILE_NAME}"
    assert revision(client, old) == before
    assert strings(client, old, "media_sha1") == []


def test_its_bytes_under_another_title_are_flagged(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    before = revision(client, old)
    fresh = not hashed(client, PORTRAIT)
    path = write(tmp_path, "copy", COPY_NAME, PORTRAIT)

    result = Registrar(client).register_artifact(
        path, "photograph", COPY_TITLE, description=DESCRIPTION
    )

    new = result.artifact_qid
    assert new != old
    assert result.created is fresh
    # the upload is the unhashed item's file: the bytes are stored once
    assert result.file_page == f"File:{FILE_NAME}"
    if fresh:
        assert [(f.kind, f.artifact, f.others) for f in result.flags] == [
            (SHARED_FILE, new, (old,))
        ]
    else:
        assert result.flags == []
    assert strings(client, new, "media_sha1") == [sha1(PORTRAIT)]
    assert strings(client, new, "media_file") == [f"File:{FILE_NAME}"]
    assert revision(client, old) == before
    assert strings(client, old, "media_sha1") == []


def test_a_re_run_finds_both_and_flags_nothing(client, tmp_path):
    """Registered again by a registrar that reads the instance afresh,
    each new item is found by its SHA-1: nothing is written, nothing is
    flagged, and the unhashed item is still left as it is."""
    old = unhashed_item(client, tmp_path)
    before = revision(client, old)
    for name, data, title in (
        (RESCAN_NAME, RESCAN, LABEL),
        (COPY_NAME, PORTRAIT, COPY_TITLE),
    ):
        path = write(tmp_path, "again", name, data)
        first = Registrar(client).register_artifact(
            path, "photograph", title, description=DESCRIPTION
        )
        written = revision(client, first.artifact_qid)
        registrar_ = Registrar(client)

        again = registrar_.register_artifact(
            path, "photograph", title, description=DESCRIPTION
        )

        assert again.artifact_qid == first.artifact_qid != old
        assert not again.created
        assert again.flags == [] and registrar_.flags == []
        assert revision(client, first.artifact_qid) == written
    assert revision(client, old) == before
    assert strings(client, old, "media_sha1") == []
