"""Live check that registration flags, and never adopts, an item that may
be an artifact registered before content hashing, or a GEDCOM media
object's item (#371; ADR-0010).

The fixture makes three items with no hash, each statement stated in the
item itself (ADR-0003):

* two as registration made them before content hashing: a label,
  `instance of photograph` and a `media file`, and no `media SHA-1`. The
  two files hold the same bytes. MediaWiki stores bytes it holds already
  under a second name only when the upload overrides its warnings, which
  registration's upload never does (`WikiClient.upload_file`), so the
  fixture alone uploads the second file through the client's lower-level
  `_upload` with `ignorewarnings`;
* one as the GEDCOM import makes a media object's item: a label and
  `instance of artifact`, no file and no hash.

Then:

* other bytes registered under the first item's label make a new item,
  content-tagged, flagged `unhashed-title`;
* its bytes registered under another title and file name upload to one
  of the two files (MediaWiki's `duplicate`), and make a new item that
  shares both, flagged `shared-file` against both items, as MediaWiki
  lists every file of those bytes;
* bytes registered under exactly the media object's title make a new
  item flagged `unhashed-title` against it;

and the items with no hash are left as they are: their revisions do not
move, and none gains a `media SHA-1` or a `media file`. Registered again,
by a registrar that reads the instance afresh, each item is found by its
SHA-1, nothing is written, and it is flagged again, as a flag repeats on
every run while its condition holds.

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
    _files_holding,
    _item_claim,
    _string_claim,
)

from .conftest import fictional_png

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#371: the Löbig family's 1890s studio portrait, registered twice "
    "before content hashing under two files of its bytes, and a GEDCOM "
    "media object; then a re-scan under its title, its own bytes under "
    "another, and a scan under the media object's title, each twice"
)
MARK = hashlib.sha256(
    FIXTURE.encode()
    + Path(registrar.__file__).read_bytes()
    + Path(wiki.__file__).read_bytes()
).hexdigest()[:8]

PORTRAIT = fictional_png(f"yizkr live test #371: the portrait {MARK}")
RESCAN = fictional_png(f"yizkr live test #371: the re-scan {MARK}")
GROUP = fictional_png(f"yizkr live test #371: the group photograph {MARK}")
LABEL = f"Integration-test unhashed portrait {MARK}"
FILE_NAME = f"Integration-test unhashed portrait {MARK}.png"
SECOND_LABEL = f"Integration-test unhashed portrait, catalogued {MARK}"
SECOND_FILE_NAME = f"Integration-test unhashed portrait catalogued {MARK}.png"
MEDIA_OBJECT_LABEL = f"Integration-test Löbig family, Flörburg {MARK}"
COPY_TITLE = f"Integration-test portrait copy {MARK}"
COPY_NAME = f"integration-test-portrait-copy-{MARK}.png"
RESCAN_NAME = f"integration-test-portrait-rescan-{MARK}.png"
GROUP_NAME = f"integration-test-group-{MARK}.png"


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


def fixture_item(client, label: str, statements) -> str:
    """The fixture's item labelled `label`, made on the first run and found
    by its label on a re-run, with `statements`, (property key, item key
    or file page) pairs, each stated in the item itself; a run cut short
    between its writes leaves it labelled, and the next gives it its
    statements."""
    properties, items = client.vocabulary()
    found = labelled(client, label)
    assert len(found) <= 1, found
    if found:
        item = client.wbi.item.get(found[0])
    else:
        item = client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set("en", DESCRIPTION)
        item = item.write(summary="integration test: unhashed artifact")
    stated_in = properties["stated_in"]
    changed = False
    for key, value in statements:
        if key == "instance_of":
            claim = _item_claim(
                properties[key], items[value], stated_in, item.id
            )
        else:
            claim = _string_claim(properties[key], value, stated_in, item.id)
        outcome = _ensure_referenced(item, claim, stated_in, item.id)
        changed = changed or outcome != "present"
    if changed:
        item = item.write(summary="integration test: unhashed artifact")
    return item.id


def unhashed_item(client, tmp_path) -> str:
    """The first item with no hash: its file is uploaded as registration
    uploads, once."""
    page = client.upload_file(
        write(tmp_path, "unhashed", FILE_NAME, PORTRAIT), filename=FILE_NAME
    )
    return fixture_item(
        client,
        LABEL,
        [("instance_of", "photograph"), ("media_file", page)],
    )


def second_unhashed_item(client, tmp_path) -> str:
    """The second item with no hash, naming a second file of the same
    bytes: uploaded with MediaWiki's warnings overridden, as registration
    never uploads, and only when that file does not hold them yet."""
    unhashed_item(client, tmp_path)
    page = f"File:{SECOND_FILE_NAME}"
    if not _files_holding(client, {page}, sha1(PORTRAIT)):
        path = write(tmp_path, "second", SECOND_FILE_NAME, PORTRAIT)
        upload = client._upload(
            path,
            SECOND_FILE_NAME,
            "integration test: a second file of the same bytes",
            override=True,
        )
        assert upload["result"] == "Success", upload
        page = f"File:{upload['filename'].replace('_', ' ')}"
    return fixture_item(
        client,
        SECOND_LABEL,
        [("instance_of", "photograph"), ("media_file", page)],
    )


def media_object_item(client) -> str:
    """An item as the GEDCOM import makes one for a media object: a label
    and `instance of artifact`, no file and no hash."""
    return fixture_item(
        client, MEDIA_OBJECT_LABEL, [("instance_of", "artifact")]
    )


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


def by_number(*qids: str) -> tuple[str, ...]:
    return tuple(sorted(qids, key=lambda qid: int(qid[1:])))


def flagged(result) -> list[tuple]:
    return [(f.kind, f.artifact, f.sha1, f.others) for f in result.flags]


def test_the_fixture_items_are_unhashed(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    second = second_unhashed_item(client, tmp_path)
    media_object = media_object_item(client)
    assert strings(client, old, "media_sha1") == []
    assert strings(client, old, "media_file") == [f"File:{FILE_NAME}"]
    assert strings(client, second, "media_file") == [
        f"File:{SECOND_FILE_NAME}"
    ]
    assert strings(client, media_object, "media_file") == []
    assert strings(client, media_object, "media_sha1") == []
    assert {old, second, media_object} <= Registrar(client)._read().unhashed


def test_other_bytes_under_its_label_are_flagged(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    before = revision(client, old)
    path = write(tmp_path, "rescan", RESCAN_NAME, RESCAN)

    result = Registrar(client).register_artifact(
        path, "photograph", LABEL, description=DESCRIPTION
    )

    new = result.artifact_qid
    assert new != old
    # flagged on the run that makes it and on every run that finds it
    assert flagged(result) == [(UNHASHED_TITLE, new, sha1(RESCAN), (old,))]
    assert strings(client, new, "media_sha1") == [sha1(RESCAN)]
    assert result.file_page != f"File:{FILE_NAME}"
    assert revision(client, old) == before
    assert strings(client, old, "media_sha1") == []


def test_its_bytes_under_another_title_are_flagged(client, tmp_path):
    old = unhashed_item(client, tmp_path)
    second = second_unhashed_item(client, tmp_path)
    before = (revision(client, old), revision(client, second))
    path = write(tmp_path, "copy", COPY_NAME, PORTRAIT)

    result = Registrar(client).register_artifact(
        path, "photograph", COPY_TITLE, description=DESCRIPTION
    )

    new = result.artifact_qid
    assert new not in (old, second)
    # the upload is one of the two files: the bytes are stored no third time
    files = {f"File:{FILE_NAME}", f"File:{SECOND_FILE_NAME}"}
    assert result.file_page in files
    # both items are named, whichever file the upload answered with
    assert flagged(result) == [
        (SHARED_FILE, new, sha1(PORTRAIT), by_number(old, second))
    ]
    assert strings(client, new, "media_sha1") == [sha1(PORTRAIT)]
    assert strings(client, new, "media_file") == [result.file_page]
    assert (revision(client, old), revision(client, second)) == before
    assert strings(client, old, "media_sha1") == []
    assert strings(client, second, "media_sha1") == []


def test_bytes_under_a_media_objects_title_are_flagged(client, tmp_path):
    media_object = media_object_item(client)
    before = revision(client, media_object)
    path = write(tmp_path, "group", GROUP_NAME, GROUP)

    result = Registrar(client).register_artifact(
        path, "photograph", MEDIA_OBJECT_LABEL, description=DESCRIPTION
    )

    new = result.artifact_qid
    assert new != media_object
    assert flagged(result) == [
        (UNHASHED_TITLE, new, sha1(GROUP), (media_object,))
    ]
    assert strings(client, new, "media_sha1") == [sha1(GROUP)]
    assert revision(client, media_object) == before
    assert strings(client, media_object, "media_file") == []
    assert strings(client, media_object, "media_sha1") == []


def test_a_re_run_finds_each_and_flags_it_again(client, tmp_path):
    """Registered again by a registrar that reads the instance afresh,
    each item is found by its SHA-1: nothing is written, and it is flagged
    again as it was, while the items with no hash are left as they are."""
    old = unhashed_item(client, tmp_path)
    second = second_unhashed_item(client, tmp_path)
    media_object = media_object_item(client)
    before = [revision(client, q) for q in (old, second, media_object)]
    for name, data, title in (
        (RESCAN_NAME, RESCAN, LABEL),
        (COPY_NAME, PORTRAIT, COPY_TITLE),
        (GROUP_NAME, GROUP, MEDIA_OBJECT_LABEL),
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

        assert again.artifact_qid == first.artifact_qid
        assert again.artifact_qid not in (old, second, media_object)
        assert not again.created
        assert len(again.flags) == 1
        assert flagged(again) == flagged(first)
        assert registrar_.flags == again.flags
        assert revision(client, first.artifact_qid) == written
    assert [revision(client, q) for q in (old, second, media_object)] == before
    assert strings(client, old, "media_sha1") == []
