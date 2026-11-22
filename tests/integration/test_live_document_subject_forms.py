"""Live check of what the non-privileged export keeps of what a source
states of another artifact (#646, ADR-0039 §10): a container's
`inception` of the image it places, and an export's `inception` of the
un-held media item its record makes.

The booklet (*Our Family from Germany*, under a test title) is registered
with the image it places on its page 6 as its derived medium, as the write
registers a PDF read page by page. Its translation of the letter's
dateline gives the image's `inception`, 28 July 1939: written on the
image's item, stated in the booklet with its page and the translation as
its excerpt. The export's object (the Löbig family's studio portrait of
the 1890s) dates itself "ABT 1895": its `inception`, circa, is written
on the media item the export makes, stated in the export. The
non-privileged export leaves the booklet's claim about its image out,
whole, failing closed, and keeps the export's claim about the media
item, which is no part of the export.

A second booklet's image also states its own date, its dateline *den 28.
Juli 1939*: the writer folds the two agreeing claims into one statement
with two references, and the non-privileged export keeps that statement
with the image's own reference and excerpt, and leaves out the booklet's
reference and its translation (#669).

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
from tests.fixtures.projected_media import project_media
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
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#646: a booklet whose page 6 places an image of a letter, the "
    "booklet's translation of the letter's dateline as the image's "
    "inception, and an export whose object dates its own media item; "
    "a second booklet's image whose own dateline agrees with the booklet's "
    "translation"
)
MARK = fixture_mark(FIXTURE, registrar, writer, backup, gedcom, claims)
TITLE = f"Yizkr Placed Inception Test Our Family from Germany {MARK}"
TRANSLATION = f"Flörburg, Brunnenring 35, 28 July 1939 ({MARK})"
MERGED_TITLE = f"Yizkr Merged Inception Test Our Family from Germany {MARK}"
# ASCII alone, so a line of the dump holds each as written, however its
# writer escapes a literal
MERGED_TRANSLATION = f"28 July 1939, Brunnenring 35 ({MARK})"
DATELINE = f"Brunnenring 35, den 28. Juli 1939 ({MARK})"
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
    booklet: ArtifactRef, image: ArtifactRef, text: str = TRANSLATION
) -> ClaimBatch:
    """The booklet's translation of the letter's dateline, as the image's
    `inception`, on page 6."""
    source = {
        "artifact": booklet.model_dump(),
        "page_number": "6",
        "transcription_excerpt": {"text": text, "language": "en"},
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


def dateline_batch(image: ArtifactRef) -> ClaimBatch:
    """The letter's image's own dateline, as its own `inception`."""
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
                            "text": DATELINE,
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )


def place(client, tmp_path_factory, title: str):
    """A booklet titled `title` and the image it places on its page 6,
    registered from the booklet's container projection as the write
    registers a PDF read page by page: (the booklet, the image's key, the
    image's item)."""
    booklet = register_fictional(client, title, "artifact")
    data = fictional_png(f"yizkr live test: {title}, page 6, image 1")
    image = ArtifactRef.from_bytes(data)
    directory = tmp_path_factory.mktemp("derived")
    sources = project_media(
        directory, booklet.ref, [("image.png", data, [6])], container="pdf"
    )
    derived = Registrar(client).register_container_sources(
        sources, directory, title=title
    )
    return booklet, image, derived[image.sha1]


@pytest.fixture(scope="module")
def placed(client, tmp_path_factory):
    """The booklet and the image it places on its page 6."""
    return place(client, tmp_path_factory, TITLE)


@pytest.fixture(scope="module")
def merged(client, tmp_path_factory):
    """The second booklet and its image, the booklet's translation of the
    image's dateline and the image's own dateline written as one
    `inception` statement with two references: (the booklet, the image's
    key, the image's item)."""
    booklet, image, image_qid = place(client, tmp_path_factory, MERGED_TITLE)
    where = {booklet.ref.sha1: {image.sha1}}
    by_booklet = write_mapped(
        BatchWriter(client),
        translation_batch(booklet.ref, image, text=MERGED_TRANSLATION),
        placed=where,
    )
    assert by_booklet.refused == []
    own = dateline_batch(image)
    assert validate_against_manifest(own) == []
    by_image = write_mapped(
        BatchWriter(client, artifact_qids={image.sha1: image_qid}), own
    )
    assert by_image.refused == []
    (statement,) = statements(client, image_qid, "inception")
    assert {v["id"] for v in cited(client, statement, "stated_in")} == {
        booklet.qid,
        image_qid,
    }
    return booklet, image, image_qid


@pytest.fixture(scope="module")
def exported(client, tmp_path_factory):
    """The export, registered by its hash and written through BatchWriter:
    (the export's item, the write's result)."""
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


def test_the_nonprivileged_export_leaves_the_container_s_claim_out(
    client, config, placed, exported, merged, tmp_path
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
    merged_booklet, _merged_image, merged_qid = merged
    wait_for_query_service(config, client, [image_qid, media_item, merged_qid])
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

    # the merged statement stays with the image's own reference and
    # excerpt; the second booklet's reference and its translation go
    concept = config.concept_uri
    derived_from = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
    stated_in = properties["stated_in"]
    (claim,) = [
        line.split(" ")[2]
        for line in dated(full, merged_qid)
        if f"/prop/{inception}>" in line.split(" ", 2)[1]
    ]

    def cites(dump: list[str], qid: str) -> set[str]:
        """The merged statement's references stated in `qid`."""
        refs = {
            line.split(" ")[2]
            for line in dump
            if line.startswith(f"{claim} {derived_from} ")
        }
        return {
            line.split(" ")[0]
            for line in dump
            if line.split(" ")[0] in refs
            and line.split(" ", 2)[1].endswith(f"/reference/{stated_in}>")
            and line.split(" ")[2] == f"<{concept}/entity/{qid}>"
        }

    assert cites(full, merged_booklet.qid) and cites(full, merged_qid)
    assert any(line.startswith(f"{claim} ") for line in kept)
    assert cites(kept, merged_qid)
    assert cites(kept, merged_booklet.qid) == set()
    assert not any(
        line.startswith(f"{claim} {derived_from} {ref}")
        for ref in cites(full, merged_booklet.qid)
        for line in kept
    )
    assert any(DATELINE in line for line in kept)
    assert not any(MERGED_TRANSLATION in line for line in kept)
    assert any(
        line.startswith(f"<{concept}/entity/{merged_qid}> ")
        and line.split(" ", 2)[1].endswith(f"/prop/direct/{inception}>")
        for line in kept
    )
