"""Live check that an interrupted mint leaves no untyped page fragment or
run item behind (#368).

The non-privileged export leaves out every item that states no type, and with a
run item the statements it backs lose their run reference there. Each
test loses the connection to the wiki just after the first write of a
mint reaches it, then runs again:

* a page fragment is made in one write that carries its label, its
  `instance of artifact fragment` and its `part of` the artifact, so the
  connection lost after page 2's write leaves page 2's fragment whole, and
  the re-run takes it as page 2's fragment: every fragment typed, under
  its plain label, `part of` the artifact on its page;
* a run item's kind cites the run item itself, so it goes in a second
  write: the connection lost after the first leaves the run item labelled
  and untyped, and the re-run finds it by its label, types it, stated in
  itself, and writes the batch's claim citing it. A further re-run writes
  nothing to it.

Fictional synthetic media and claims only (docs/EXAMPLES.md): Gustav's
birth register extract of 1938, and his birth as it gives it. Every title,
file name and label carries a marker, eight hex digits of a hash over the
fixture and over the registrar's and the writer's source, so each version
of the fixture and of that code meets the fixture as a first mint does: a
mutant of the harness cannot pass by finding what an earlier version made.
A re-run of one version finds its own items typed, and passes without
cutting anything. Like every live test it writes fictional data to the one
instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from wikibaseintegrator.entities.item import ItemEntity

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.writer import BatchWriter, batch_fingerprint

from .conftest import fictional_png, register_fictional

pytestmark = pytest.mark.live

DESCRIPTION = "written by tests/integration; safe to delete"
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#368: Gustav's birth register extract of 1938, two pages; his birth, "
    "7 January 1919, as its first page gives it"
)
MARK = hashlib.sha256(
    FIXTURE.encode()
    + Path(registrar.__file__).read_bytes()
    + Path(writer.__file__).read_bytes()
).hexdigest()[:8]

# the extract, registered with a page fragment for each of its two pages
EXTRACT = fictional_png(f"yizkr live test #368: the birth extract {MARK}")
TITLE = f"Integration-test birth extract, interrupted mints {MARK}"
FILE_NAME = f"integration-test-birth-extract-mints-{MARK}.png"
PAGES = 2
# the batch whose run item's mint is interrupted: Gustav's birth
PERSON = {
    "kind": "local",
    "local_id": "gustav",
    "label": f"Interrupted Mint Fixture ({MARK}) Gustav Löbig",
    "instance_of": "human",
}
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-29T09:00:00Z",
}


class ConnectionLost(RuntimeError):
    """The connection to the wiki was lost just after a write reached it."""


def lose_after_first_write(patch, summary: str, label: str) -> None:
    """While `patch` holds, the first item write with this edit summary
    and this English label reaches the wiki, and then the connection is
    lost: its caller gets ConnectionLost instead of the item."""
    write = ItemEntity.write
    lost: list[str] = []

    def losing(self, *args, **kwargs):
        sent = str(self.labels.get("en") or "")
        written = write(self, *args, **kwargs)
        if not lost and kwargs.get("summary") == summary and sent == label:
            lost.append(written.id)
            raise ConnectionLost(
                f"the connection was lost after {summary!r} reached the wiki"
            )
        return written

    patch.setattr(ItemEntity, "write", losing)


def entity(client, qid: str, props: str = "claims") -> dict:
    return client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": props}
    )["entities"][qid]


def stated_in(client, statement: dict) -> list[str]:
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties["stated_in"], [])
    ]


def kinds(client, qid: str) -> list[tuple[str, str, list[str]]]:
    """(kind, rank, the items it is stated in) of each `instance of`
    statement of an item, as the API gives it."""
    properties, _ = client.vocabulary()
    return [
        (
            statement["mainsnak"]["datavalue"]["value"]["id"],
            statement["rank"],
            stated_in(client, statement),
        )
        for statement in entity(client, qid)
        .get("claims", {})
        .get(properties["instance_of"], [])
    ]


def parts(client, qid: str) -> dict[tuple[str, str], list[str]]:
    """(whole, page) -> the items it is stated in, for each `part of`
    statement of an item."""
    properties, _ = client.vocabulary()
    return {
        (
            statement["mainsnak"]["datavalue"]["value"]["id"],
            statement["qualifiers"][properties["page_number"]][0]["datavalue"][
                "value"
            ],
        ): stated_in(client, statement)
        for statement in entity(client, qid)
        .get("claims", {})
        .get(properties["part_of"], [])
    }


def revision(client, qid: str) -> int:
    return entity(client, qid, props="info")["lastrevid"]


def register_extract(client, path: Path):
    """Register the extract with its fragments, by a registrar that reads
    the instance afresh, as a later write's does."""
    return Registrar(client).register_artifact(
        path, "certificate", TITLE, description=DESCRIPTION, pages=PAGES
    )


def test_a_fragment_mint_cut_short_leaves_every_fragment_typed(
    client, tmp_path, monkeypatch
):
    properties, items = client.vocabulary()
    path = tmp_path / "extracts" / FILE_NAME
    path.parent.mkdir()
    path.write_bytes(EXTRACT)
    sha1 = hashlib.sha1(EXTRACT).hexdigest()
    page_two = f"{TITLE}, page 2"
    fresh = sha1 not in client.string_statement_index(properties["media_sha1"])

    if fresh:
        with monkeypatch.context() as patch:
            lose_after_first_write(
                patch, "ingest: register fragment", page_two
            )
            with pytest.raises(ConnectionLost):
                register_extract(client, path)
        artifact = client.string_statement_index(properties["media_sha1"])[
            sha1
        ]
        # the write that reached the wiki made page 2's fragment whole
        left = client.find_item_by_label(page_two)
        assert left is not None
        assert kinds(client, left) == [
            (items["artifact_fragment"], "normal", [artifact])
        ]

    result = register_extract(client, path)

    artifact = result.artifact_qid
    assert sorted(result.fragment_qids) == [1, 2]
    for page, qid in result.fragment_qids.items():
        # under its plain label: no second fragment was made beside it
        assert client.find_item_by_label(f"{TITLE}, page {page}") == qid
        assert kinds(client, qid) == [
            (items["artifact_fragment"], "normal", [artifact])
        ]
        assert parts(client, qid) == {(artifact, str(page)): [artifact]}
    if fresh:
        assert result.fragment_qids[2] == left


def fixture_batch(source: ArtifactRef) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": PERSON,
                    "property": "date_of_birth",
                    "value": {
                        "type": "time",
                        "year": 1919,
                        "month": 1,
                        "day": 7,
                    },
                    "source": {
                        "artifact": source.model_dump(mode="json"),
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": "geboren am siebenten Januar 1919",
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )


def test_a_run_item_mint_cut_short_is_typed_by_the_rerun(client, monkeypatch):
    properties, items = client.vocabulary()
    source = register_fictional(
        client, f"Interrupted mint fixture source {MARK}", "certificate"
    )
    batch = fixture_batch(source.ref)
    fingerprint = batch_fingerprint(batch)
    label = f"extraction run 2026-09-29 integration-test {fingerprint}"
    fresh = client.find_item_by_label(label) is None

    if fresh:
        with monkeypatch.context() as patch:
            lose_after_first_write(patch, "ingest: extraction run", label)
            with pytest.raises(ConnectionLost):
                BatchWriter(client).write_batch(batch)
        # the run item, labelled and untyped: nothing else of the batch
        # was written
        left = client.find_item_by_label(label)
        assert left is not None
        assert kinds(client, left) == []
        assert client.find_item_by_label(PERSON["label"]) is None

    written = BatchWriter(client).write_batch(batch)

    run = written.run_qid
    if fresh:
        assert run == left
    assert client.find_item_by_label(label) == run
    assert kinds(client, run) == [(items["extraction_run"], "normal", [run])]
    # the batch's claim cites the run item
    person = client.find_item_by_label(PERSON["label"])
    (born,) = (
        entity(client, person)
        .get("claims", {})
        .get(properties["date_of_birth"], [])
    )
    runs = [
        snak["datavalue"]["value"]["id"]
        for reference in born.get("references", [])
        for snak in reference["snaks"].get(
            properties["inferred_from_extraction_run"], []
        )
    ]
    assert runs == [run]

    # a further re-run finds the run item typed, and writes nothing to it
    before = revision(client, run)
    again = BatchWriter(client).write_batch(batch)
    assert again.run_qid == run
    assert revision(client, run) == before
