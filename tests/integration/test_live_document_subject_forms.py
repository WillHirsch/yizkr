"""Live write path for what a source states of another artifact (#646,
ADR-0039 §10): a container's `inception` of the image it places, and an
export's `inception` of the un-held media item its record makes.

The booklet (*Our Family from Germany*, under a test title) is registered
with the image it places on its page 6 as its derived medium, as the write
registers a PDF read page by page. Its translation of the letter's
dateline gives the image's `inception`, 28 July 1939: written on the
image's item, stated in the booklet with its page and the translation as
its excerpt; a re-run adds nothing. Without the booklet's placed media, or
without its excerpt, the claim is refused before the first write. The
export's object (the Löbig family's studio portrait of the 1890s) dates
itself "ABT 1895": its `inception`, circa, is written on the media item
the export makes, stated in the export. The non-privileged export leaves
the booklet's claim about its image out, whole, failing closed, and keeps
the export's claim about the media item, which is no part of the export.

Every title, label, excerpt and media byte carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write and a re-run within it reuses its items. Like every live test it
writes fictional data to the one instance, so the session that runs it
ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import gzip
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
from yizkr import backup, claims
from yizkr.backup import dump_rdf, export_nonprivileged
from yizkr.claims import ArtifactRef, ClaimBatch, validate_against_manifest
from yizkr.pipeline import gedcom, registrar, writer
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.presentation import (
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
    "#646: a booklet whose page 6 places an image of a letter, the "
    "booklet's translation of the letter's dateline as the image's "
    "inception, and an export whose object dates its own media item"
)
MARK = fixture_mark(FIXTURE, registrar, writer, backup, gedcom, claims)
TITLE = f"Yizkr Placed Inception Test Our Family from Germany {MARK}"
TRANSLATION = f"Flörburg, Brunnenring 35, 28 July 1939 ({MARK})"
DESCRIPTION = "written by tests/integration; safe to delete"
EXPORT = (
    "0 HEAD\n1 CHAR UTF-8\n"
    f"0 @I1@ INDI\n1 NAME Sebastian /Löbig {MARK}/\n1 OBJE @O1@\n"
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
    f"2 TITL Integration-test studio portrait of the Löbig family {MARK}\n"
    "1 DATE ABT 1895\n"
    "0 TRLR\n"
)
DEPLOY = Path("deploy")


def run() -> dict:
    return {
        "model_identifier": "integration-test",
        "pipeline_version": "0.0.0",
        "started_at": datetime.now(UTC).isoformat(),
    }


def translation_batch(
    booklet: ArtifactRef, image: ArtifactRef, excerpt: bool = True
) -> ClaimBatch:
    """The booklet's translation of the letter's dateline, as the image's
    `inception`, on page 6."""
    source: dict = {"artifact": booklet.model_dump(), "page_number": "6"}
    if excerpt:
        source["transcription_excerpt"] = {
            "text": TRANSLATION,
            "language": "en",
        }
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
                    "source": source,
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
    return booklet, image, derived[image.sha1]


@pytest.fixture(scope="module")
def exported(client, tmp_path_factory):
    """The export, registered by its hash and written as `ingest run`
    writes it: (the export's item, the write's result)."""
    path = tmp_path_factory.mktemp("export") / f"integration-{MARK}.ged"
    path.write_bytes(EXPORT.encode())
    registered = Registrar(client).register_artifact(
        path,
        "genealogical_report",
        f"Integration-test export {MARK}",
        description=DESCRIPTION,
    )
    ref = ArtifactRef.from_path(path)
    batch = adapt_gedcom(EXPORT, artifact=ref).batch
    assert validate_against_manifest(batch) == []
    result = write_mapped(
        BatchWriter(client, artifact_qids={ref.sha1: registered.artifact_qid}),
        batch,
    )
    return registered.artifact_qid, result


def statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[key],
        }
    )
    return [claim for found in data["claims"].values() for claim in found]


def cited(client, statement: dict, key: str) -> list:
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties[key], [])
    ]


def test_a_container_s_inception_is_written_on_the_image_it_places(
    client, placed
):
    booklet, image, image_qid = placed
    batch = translation_batch(booklet.ref, image)
    where = {booklet.ref.sha1: {image.sha1}}
    assert validate_against_manifest(batch, placed=where) == []
    result = write_mapped(BatchWriter(client), batch, placed=where)
    assert (result.written + result.skipped, result.refused) == (1, [])
    (statement,) = statements(client, image_qid, "inception")
    value = statement["mainsnak"]["datavalue"]["value"]
    assert (value["time"], value["precision"]) == ("+1939-07-28T00:00:00Z", 11)
    assert statement["rank"] == "normal"
    assert [v["id"] for v in cited(client, statement, "stated_in")] == [
        booklet.qid
    ]
    assert cited(client, statement, "page_number") == ["6"]
    excerpts = cited(client, statement, "transcription_excerpt")
    assert [e["text"] for e in excerpts] == [TRANSLATION]
    # the claim is the image's, never the booklet's
    assert statements(client, booklet.qid, "inception") == []

    # a re-run resolves the image to the same item and finds the claim
    again = write_mapped(
        BatchWriter(client),
        translation_batch(booklet.ref, image),
        placed=where,
    )
    assert (again.written, again.skipped) == (0, 1)
    assert len(statements(client, image_qid, "inception")) == 1


@pytest.mark.parametrize(
    ("excerpt", "placed_given"), [(True, False), (False, True)]
)
def test_without_its_placed_media_or_its_excerpt_it_is_refused(
    client, placed, excerpt, placed_given
):
    """The writer holds both rules itself, as `yizkr write` takes a batch
    without validating it: the image must be among the booklet's placed
    media, and the claim must carry its passage (ADR-0039 §10.4)."""
    booklet, image, image_qid = placed
    before = statements(client, image_qid, "inception")
    batch = translation_batch(booklet.ref, image, excerpt=excerpt)
    where = {booklet.ref.sha1: {image.sha1}} if placed_given else None
    result = write_mapped(BatchWriter(client), batch, placed=where)
    (claim,) = batch.claims
    assert result.refused == [claim_id(claim)]
    assert (result.written, result.skipped, result.referenced) == (0, 0, 0)
    assert statements(client, image_qid, "inception") == before
    assert statements(client, booklet.qid, "inception") == []


def test_the_export_s_inception_is_on_its_unheld_media_item(client, exported):
    export_qid, result = exported
    assert result.refused == []
    media_item = result.created_items["md1"]
    (statement,) = statements(client, media_item, "inception")
    value = statement["mainsnak"]["datavalue"]["value"]
    assert (value["time"][:5], value["precision"]) == ("+1895", 9)
    assert [v["id"] for v in cited(client, statement, "stated_in")] == [
        export_qid
    ]
    assert statements(client, export_qid, "inception") == []


def test_the_nonprivileged_export_leaves_the_container_s_claim_out(
    client, config, placed, exported, tmp_path
):
    if not (DEPLOY / "docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    booklet, image, image_qid = placed
    _export_qid, result = exported
    media_item = result.created_items["md1"]
    where = {booklet.ref.sha1: {image.sha1}}
    write_mapped(
        BatchWriter(client),
        translation_batch(booklet.ref, image),
        placed=where,
    )
    wait_for_query_service(config, client, [image_qid, media_item])
    dump_rdf(DEPLOY, tmp_path / "full.nt.gz")
    export_nonprivileged(config, tmp_path)

    def lines(name: str) -> list[str]:
        with gzip.open(tmp_path / name, "rt", encoding="utf-8") as dump:
            return dump.read().splitlines()

    properties, _ = client.vocabulary()
    inception = properties["inception"]

    def dated(dump: list[str], qid: str) -> list[str]:
        """The triples that give `qid` an inception statement."""
        return [
            line
            for line in dump
            if line.startswith(f"<{config.concept_uri}/entity/{qid}> ")
            and line.split(" ", 2)[1].endswith(f"/{inception}>")
        ]

    full = lines("full.nt.gz")
    kept = lines("nonprivileged.nt.gz")
    # the full dump holds the booklet's claim about its image, so its
    # absence is the export's doing; the image's item itself stays
    assert dated(full, image_qid)
    assert dated(kept, image_qid) == []
    assert not any(TRANSLATION in line for line in kept)
    assert any(
        line.startswith(f"<{config.concept_uri}/entity/{image_qid}> ")
        for line in kept
    )
    # the export's claim about its un-held media item stays
    assert dated(full, media_item)
    assert dated(kept, media_item)
