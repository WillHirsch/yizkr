"""Live media upload and registration against MediaWiki's upload warnings
(ADR-0010, #186): a wiki file is never overwritten and the same bytes are
never stored twice, so every item's `media file` holds the bytes its
`media SHA-1` names.

Fictional synthetic images only. Re-runnable against a non-empty graph:
the images are byte-identical on every run, so a re-run finds every file
and item in place, and must store and write nothing."""

from __future__ import annotations

import hashlib

import pytest

from tests.integration.test_live_pipeline import png_bytes
from tests.integration.test_live_presentation import files_with_bytes
from yizkr.pipeline.registrar import register_artifact

pytestmark = pytest.mark.live

UPLOAD_NAME = "Integration-test upload warnings.png"
FIRST = png_bytes(fill=b"\x19\x24\x01")
SECOND = png_bytes(fill=b"\x19\x24\x02")

TITLE = "Integration-test changed source"
SOURCE_NAME = "integration-test-changed-source.png"
ORIGINAL = png_bytes(fill=b"\x19\x24\x03")
CHANGED = png_bytes(fill=b"\x19\x24\x04")
DESCRIPTION = "written by tests/integration; safe to delete"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def write(tmp_path, folder: str, name: str, data: bytes):
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def db_key(page: str) -> str:
    return page.removeprefix("File:").replace(" ", "_")


def history(client, page: str) -> list[str]:
    """The SHA-1 of every version of a wiki file, newest first."""
    data = client.api_get(
        {
            "action": "query",
            "titles": page,
            "prop": "imageinfo",
            "iiprop": "sha1",
            "iilimit": "max",
        }
    )
    (info,) = data["query"]["pages"].values()
    return [version["sha1"] for version in info.get("imageinfo", [])]


def statement_values(client, qid: str, key: str) -> list[str]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claims in data["claims"].values()
        for claim in claims
    ]


def revisions(client, *qids: str) -> dict[str, int]:
    data = client.api_get(
        {"action": "wbgetentities", "ids": "|".join(qids), "props": "info"}
    )
    return {qid: data["entities"][qid]["lastrevid"] for qid in qids}


def test_upload_never_overwrites_or_duplicates(client, tmp_path):
    first = write(tmp_path, "first", UPLOAD_NAME, FIRST)
    page = client.upload_file(first)
    assert page == f"File:{UPLOAD_NAME}"
    assert history(client, page) == [sha1(FIRST)]

    # the same bytes under the same name, then under another: the stored
    # file is reused and nothing is stored
    assert client.upload_file(first) == page
    copy = write(
        tmp_path, "copy", "Integration-test upload warnings copy.png", FIRST
    )
    assert client.upload_file(copy) == page
    assert files_with_bytes(client, sha1(FIRST)) == {db_key(page)}

    # different bytes under the stored name: the stored file keeps its
    # bytes and its one version; the new bytes get a content-tagged name
    second = write(tmp_path, "second", UPLOAD_NAME, SECOND)
    tagged = client.upload_file(second)
    tag = sha1(SECOND)[:8]
    assert tagged == f"File:Integration-test upload warnings {tag}.png"
    assert history(client, page) == [sha1(FIRST)]
    assert history(client, tagged) == [sha1(SECOND)]

    # and again: the tagged file is found, not stored twice
    assert client.upload_file(second) == tagged
    assert history(client, tagged) == [sha1(SECOND)]
    assert files_with_bytes(client, sha1(SECOND)) == {db_key(tagged)}


def test_media_file_and_media_sha1_stay_in_step(client, tmp_path):
    def register(path, title=TITLE):
        return register_artifact(
            client,
            media_path=path,
            artifact_type="photograph",
            title=title,
            description=DESCRIPTION,
        )

    original = write(tmp_path, "original", SOURCE_NAME, ORIGINAL)
    changed = write(tmp_path, "changed", SOURCE_NAME, CHANGED)
    copy = write(
        tmp_path, "copy", "integration-test-changed-source-copy.png", ORIGINAL
    )

    first = register(original)
    # the source changes under the same name and title: a new artifact
    second = register(changed)
    assert second.artifact_qid != first.artifact_qid
    # the same bytes under another name: the one artifact, no new file
    third = register(copy, f"{TITLE} copy")
    assert third.artifact_qid == first.artifact_qid

    # every media file holds exactly the bytes its item's hash names, and
    # no stored file gained a version
    for result, data in ((first, ORIGINAL), (second, CHANGED)):
        qid = result.artifact_qid
        assert statement_values(client, qid, "media_sha1") == [sha1(data)]
        files = statement_values(client, qid, "media_file")
        assert files
        for page in files:
            assert history(client, page) == [sha1(data)], page
        assert files_with_bytes(client, sha1(data)) == {
            db_key(page) for page in files
        }

    # a re-run stores and writes nothing
    items = (first.artifact_qid, second.artifact_qid)
    before = revisions(client, *items)
    assert register(original).artifact_qid == first.artifact_qid
    assert register(changed).artifact_qid == second.artifact_qid
    assert register(copy, f"{TITLE} copy").artifact_qid == first.artifact_qid
    assert revisions(client, *items) == before
    for data in (ORIGINAL, CHANGED):
        (name,) = files_with_bytes(client, sha1(data))
        assert history(client, f"File:{name}") == [sha1(data)]
