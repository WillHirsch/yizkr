"""Live write path for a container's caption about an image it places,
and for a document's statement about itself (#456, ADR-0039).

The booklet (*Our Family from Germany*, under a test title) is registered
with the image it places on its page 6 as its derived medium, as the
write registers a PDF read page by page (ADR-0008, as amended by #397).
Its caption over the image, "Wastl to Klara, July 1939.", is written as
one `caption` statement on the image's item, its subject the image's
SHA-1, stated in the booklet with its page; the image's own dateline
gives its `inception`, stated in the image itself. A re-run adds
nothing. A caption written without the derived media its container
registered is refused before the first write. `yizkr ingest write` takes
them from the booklet's record of derived media with `--placed` (#551),
and without it refuses the caption and says how to give them. The
non-privileged export leaves the caption out, whole, and keeps the
image's own statement.

Every title, label, caption and media byte carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write and a re-run within it reuses its items. Like every live test it
writes fictional data to the one instance, so the session that runs it
ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import (
    fictional_png,
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)
from yizkr import backup, cli
from yizkr.backup import dump_rdf, export_nonprivileged
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    unplaced_hint,
    validate_against_manifest,
)
from yizkr.core.exits import EXIT_OK
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    DERIVED_MEDIA_DIR,
    DerivedMediaRecord,
    DerivedMedium,
)
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#456: a booklet whose page 6 places an image of a letter, its caption "
    "over the image, and the image's own dateline"
)
MARK = fixture_mark(FIXTURE, registrar, writer, backup)
TITLE = f"Yizkr Caption Test Our Family from Germany {MARK}"
CAPTION = f"Wastl to Klara, July 1939. ({MARK})"
DEPLOY = Path("deploy")


def run() -> dict:
    return {
        "model_identifier": "integration-test",
        "pipeline_version": "0.0.0",
        "started_at": datetime.now(UTC).isoformat(),
    }


def caption_batch(booklet: ArtifactRef, image: ArtifactRef) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": run(),
            "claims": [
                {
                    "subject": image.model_dump(),
                    "property": "caption",
                    "value": {
                        "type": "monolingual",
                        "value": {"text": CAPTION, "language": "en"},
                    },
                    "source": {
                        "artifact": booklet.model_dump(),
                        "page_number": "6",
                        "transcription_excerpt": {
                            "text": CAPTION,
                            "language": "en",
                        },
                    },
                }
            ],
        }
    )


def dateline_batch(image: ArtifactRef) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": run(),
            "claims": [
                {
                    "subject": image.model_dump(),
                    "property": "inception",
                    "value": {
                        "type": "time",
                        "year": 1939,
                        "month": 7,
                        "day": 28,
                    },
                    "source": {
                        "artifact": image.model_dump(),
                        "transcription_excerpt": {
                            "text": "den 28. Juli 1939",
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )


@pytest.fixture(scope="module")
def placed(client, tmp_path_factory):
    """The booklet and the image it places on its page 6, registered as
    the write registers a PDF read page by page: (the booklet, the image's
    key, the image's item)."""
    booklet = register_fictional(client, TITLE, "artifact")
    data = fictional_png(f"yizkr live test: {TITLE}, page 6, image 1")
    image = ArtifactRef.from_bytes(data)
    directory = tmp_path_factory.mktemp("derived")
    (directory / DERIVED_MEDIA_DIR).mkdir()
    file = f"{DERIVED_MEDIA_DIR}/{image.sha1}.png"
    (directory / file).write_bytes(data)
    record = DerivedMediaRecord(
        source_sha1=booklet.ref.sha1,
        container="pdf",
        title=TITLE,
        media={
            image.sha1: DerivedMedium(
                names=["page 6, image 1"], slides=[6], file=file
            )
        },
    )
    derived = Registrar(client).register_derived_media(record, directory)
    return booklet, image, derived


def statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[key],
        }
    )
    return [claim for claims in data["claims"].values() for claim in claims]


def cited(client, statement: dict, key: str) -> list:
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties[key], [])
    ]


def test_a_caption_is_written_on_the_image_its_container_places(
    client, placed
):
    booklet, image, derived = placed
    image_qid = derived[image.sha1]
    batch = caption_batch(booklet.ref, image)
    where = {booklet.ref.sha1: set(derived)}
    assert validate_against_manifest(batch, placed=where) == []
    result = write_mapped(BatchWriter(client), batch, placed=where)
    assert (result.written, result.refused) == (1, [])
    (statement,) = statements(client, image_qid, "caption")
    assert statement["mainsnak"]["datavalue"]["value"] == {
        "text": CAPTION,
        "language": "en",
    }
    assert statement["rank"] == "normal"
    assert [v["id"] for v in cited(client, statement, "stated_in")] == [
        booklet.qid
    ]
    assert cited(client, statement, "page_number") == ["6"]
    runs = cited(client, statement, "inferred_from_extraction_run")
    assert [v["id"] for v in runs] == [result.run_qid]
    # the caption is the image's, never the booklet's
    assert statements(client, booklet.qid, "caption") == []

    # a re-run resolves the image to the same item and finds the caption
    again = write_mapped(
        BatchWriter(client), caption_batch(booklet.ref, image), placed=where
    )
    assert (again.written, again.skipped) == (0, 1)
    assert len(statements(client, image_qid, "caption")) == 1


def test_a_caption_without_its_registered_media_is_refused(client, placed):
    """The writer holds the placed-subject rule itself: `yizkr write`
    takes a batch without the derived media its container registered."""
    booklet, image, derived = placed
    before = statements(client, derived[image.sha1], "caption")
    batch = caption_batch(booklet.ref, image)
    result = write_mapped(BatchWriter(client), batch)
    (claim,) = batch.claims
    assert result.refused == [claim_id(claim)]
    assert (result.written, result.skipped, result.referenced) == (0, 0, 0)
    assert statements(client, derived[image.sha1], "caption") == before
    assert statements(client, booklet.qid, "caption") == []


def test_ingest_write_takes_the_placed_media_from_their_record(
    client, placed, tmp_path, capsys
):
    """`yizkr ingest write` refuses the caption without the booklet's
    placed media, and says how to give them; with `--placed` naming the
    directory that keeps the booklet's record of derived media, the
    caption is on the image's item, once."""
    booklet, image, derived = placed
    image_qid = derived[image.sha1]
    before = statements(client, image_qid, "caption")
    batch = caption_batch(booklet.ref, image)
    (claim,) = batch.claims
    batch_json = tmp_path / "batch.json"
    batch_json.write_text(batch.model_dump_json())

    status = cli.main(["ingest", "write", "--batch", str(batch_json)])
    out, err = capsys.readouterr()
    assert status == EXIT_OK
    refused = json.loads(out)
    assert refused["refused"] == [claim_id(claim)]
    assert (refused["written"], refused["skipped"]) == (0, 0)
    assert unplaced_hint(1) in err
    assert statements(client, image_qid, "caption") == before

    directory = tmp_path / "booklet"
    directory.mkdir()
    (directory / DERIVED_MEDIA).write_text(
        DerivedMediaRecord(
            source_sha1=booklet.ref.sha1,
            container="pdf",
            title=TITLE,
            media={
                image.sha1: DerivedMedium(
                    names=["page 6, image 1"],
                    slides=[6],
                    file=f"{DERIVED_MEDIA_DIR}/{image.sha1}.png",
                )
            },
        ).model_dump_json(indent=2)
    )
    status = cli.main(
        [
            *("ingest", "write", "--batch", str(batch_json)),
            *("--placed", str(directory)),
        ]
    )
    out, err = capsys.readouterr()
    assert status == EXIT_OK
    result = json.loads(out)
    assert result["refused"] == []
    assert result["written"] + result["skipped"] == 1
    assert "--placed" not in err
    (statement,) = statements(client, image_qid, "caption")
    assert statement["mainsnak"]["datavalue"]["value"] == {
        "text": CAPTION,
        "language": "en",
    }
    assert [v["id"] for v in cited(client, statement, "stated_in")] == [
        booklet.qid
    ]
    assert statements(client, booklet.qid, "caption") == []


def test_a_document_s_date_is_written_on_its_own_item(client, placed):
    _booklet, image, derived = placed
    image_qid = derived[image.sha1]
    batch = dateline_batch(image)
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written + result.skipped, result.refused) == (1, [])
    (statement,) = statements(client, image_qid, "inception")
    value = statement["mainsnak"]["datavalue"]["value"]
    assert (value["time"], value["precision"]) == ("+1939-07-28T00:00:00Z", 11)
    assert [v["id"] for v in cited(client, statement, "stated_in")] == [
        image_qid
    ]


def test_the_nonprivileged_export_leaves_the_caption_out(
    client, config, placed, tmp_path
):
    if not (DEPLOY / "docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    booklet, image, derived = placed
    image_qid = derived[image.sha1]
    where = {booklet.ref.sha1: set(derived)}
    write_mapped(
        BatchWriter(client), caption_batch(booklet.ref, image), placed=where
    )
    write_mapped(BatchWriter(client), dateline_batch(image))
    wait_for_query_service(config, client, [image_qid, booklet.qid])
    dump_rdf(DEPLOY, tmp_path / "full.nt.gz")
    export_nonprivileged(config, tmp_path)

    def lines(name: str) -> list[str]:
        with gzip.open(tmp_path / name, "rt", encoding="utf-8") as dump:
            return dump.read().splitlines()

    properties, _ = client.vocabulary()
    caption_pid, inception_pid = properties["caption"], properties["inception"]
    full = lines("full.nt.gz")
    kept = lines("nonprivileged.nt.gz")
    # the full dump holds the caption, so its absence is the export's doing
    assert any(MARK in line and "Wastl to Klara" in line for line in full)
    assert not any("Wastl to Klara" in line for line in kept)

    # no triple states a caption, as the full dump's do; the property's
    # own declaration may stay
    def captions(dump: list[str]) -> list[str]:
        return [
            line
            for line in dump
            if line.split(" ", 2)[1].endswith(f"/{caption_pid}>")
        ]

    assert captions(full)
    assert captions(kept) == []
    # the image itself, and what it states of itself, stay
    assert any(
        line.startswith(f"<{config.concept_uri}/entity/{image_qid}>")
        and f"/{inception_pid}>" in line
        for line in kept
    )
