"""Live check that the non-privileged export leaves out a container's
caption about an image it places, and keeps the image's statement about
itself (#456, ADR-0039).

The booklet (*Our Family from Germany*, under a test title) is registered
with the image it places on its page 6 as its derived medium, as the
write registers a PDF read page by page (ADR-0008, as amended by #397).
Its caption over the image, "Wastl to Klara, July 1939.", is written as
one `caption` statement on the image's item, its subject the image's
SHA-1, stated in the booklet with its page; the image's own dateline
gives its `inception`, stated in the image itself. The non-privileged
export leaves the caption out, whole, and keeps the image's own
statement.

Every title, label, caption and media byte carries the session's marker
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
from yizkr import backup
from yizkr.backup import dump_rdf, export_nonprivileged
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.registrar import Registrar
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
    """The booklet and the image it places on its page 6, registered from
    the booklet's container projection as the write registers a PDF read
    page by page: (the booklet, the image's key, {its SHA-1 -> its
    item})."""
    booklet = register_fictional(client, TITLE, "artifact")
    data = fictional_png(f"yizkr live test: {TITLE}, page 6, image 1")
    image = ArtifactRef.from_bytes(data)
    directory = tmp_path_factory.mktemp("derived")
    sources = project_media(
        directory, booklet.ref, [("image.png", data, [6])], container="pdf"
    )
    derived = Registrar(client).register_container_sources(
        sources, directory, title=TITLE
    )
    return booklet, image, derived


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
