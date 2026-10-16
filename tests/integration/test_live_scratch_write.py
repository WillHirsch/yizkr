"""Live: a scratch run's write side (#200; yizkr.pipeline.scratchwrite),
the corpus stages after clustering, run over fictional documents on the
one instance.

* The reset stage resets the instance, through the reset `yizkr scratch
  reset --yes` runs; register registers each document by its bytes;
  write writes every claim of each batch at normal rank, with both
  references, through the run's one identity map, and leaves the depicts
  claim out, counting it; the site's every page names the scratch run.
* Each run item is marked an unratified scratch run's, naming the run,
  and records the hashes of the run manifest's settings and of the
  identity map, each stated in the run item itself.
* The instance-contents record takes the write key. A second run on the
  same batches resets nothing and writes nothing; a write the stage did
  not make voids the record.
* The test ends by clearing the instance-contents key, as the session
  does: the next scratch run resets first.

It resets the one instance, so it runs only when asked, in a session of
its own, and never on a kept instance (test_live_scratch_reset.py's
`resets`): `YIZKR_LIVE_RESET=1 uv run pytest -m live
tests/integration/test_live_scratch_write.py`. The session holds the
live-stack lock, so the stages run under its hold. Fictional data only
(docs/EXAMPLES.md): Leopold Löbig's birth register extract and a letter
from his brother Gustav, each label and media byte carrying the session's
mark.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.core.instance import (
    clear_contents,
    read_record,
    recorded_contents,
    watermark,
)
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import registrar, scratchwrite, writer
from yizkr.pipeline.orchestrate import DocumentRun
from yizkr.pipeline.scratch import CORPUS_DIR, CorpusContext, ScratchDriver

from . import test_live_scratch_reset
from .conftest import (
    LIVE_TESTS,
    fictional_png,
    fixture_mark,
    register_fictional,
)

# the reset of the stack beside the session's env file, as
# test_live_scratch_reset.py's tests take it: only when asked, in a
# session of its own, on an instance that is not kept
resets = test_live_scratch_reset.resets

pytestmark = pytest.mark.live

MARK = fixture_mark(
    "the scratch write side #200", scratchwrite, writer, registrar
)
LEOPOLD = f"Leopold Löbig {MARK}"
GUSTAV = f"Gustav Löbig {MARK}"
RUN = {
    "model_identifier": "claude-sonnet-5",
    "pipeline_version": "0.1.0",
    "started_at": "2026-10-06T09:00:00Z",
}


def person(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "human",
    }


def claim(sha1: str, subject: dict, prop: str, value: dict) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "page_number": "1",
        },
        "confidence": 0.9,
    }


def documents(folder: Path) -> list[tuple[str, bytes, ClaimBatch]]:
    """(file name, bytes, batch) of each fictional document."""
    import hashlib

    extract = fictional_png(f"yizkr live test #200: extract {MARK}")
    letter = fictional_png(f"yizkr live test #200: letter {MARK}")
    one, two = (hashlib.sha1(data).hexdigest() for data in (extract, letter))
    born = {"type": "time", "year": 1914, "month": 5}
    return [
        (
            f"leopold-extract-{MARK}.png",
            extract,
            ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "claims": [
                        claim(
                            one,
                            person("leopold", LEOPOLD),
                            "date_of_birth",
                            born,
                        )
                    ],
                }
            ),
        ),
        (
            f"gustav-letter-{MARK}.png",
            letter,
            ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "claims": [
                        claim(
                            two,
                            person("gustav", GUSTAV),
                            "sibling",
                            {
                                "type": "item",
                                "value": person("leopold", LEOPOLD),
                            },
                        ),
                        claim(
                            two,
                            {"kind": "artifact", "sha1": two},
                            "depicts",
                            {
                                "type": "item",
                                "value": person("leopold", LEOPOLD),
                            },
                        ),
                    ],
                }
            ),
        ),
    ]


class Run:
    """A scratch run's documents past the barrier, its stages run in
    order with the session's config, under the session's hold on the
    live-stack lock, the reset the session's own (`resets`)."""

    def __init__(self, tmp_path, config, reset_with, client, run_id):
        self.resets = 0
        self.folder = tmp_path / "corpus"
        self.folder.mkdir(exist_ok=True)
        self.run_dir = tmp_path / "runs" / run_id
        self.driver = ScratchDriver(
            self.folder,
            self.run_dir,
            run_id=run_id,
            cache_dir=tmp_path / "cache",
            exclusions=ExclusionList(),
            env_file=Path(config.env_file),
        )
        self.driver._config = config
        self.documents = []
        for name, data, batch in documents(self.folder):
            path = self.folder / name
            path.write_bytes(data)
            doc = DocumentRun(path, self.run_dir)
            doc.dir.mkdir(parents=True, exist_ok=True)
            doc.batch.write_text(batch.model_dump_json(indent=2))
            self.documents.append(doc)
        self._resets = reset_with
        self._client = client

    def reset(self, env_file, log):
        self.resets += 1
        self._resets(self._client)

    def context(self, stage: str) -> CorpusContext:
        return CorpusContext(
            run=self.driver.rundir,
            documents=tuple(self.documents),
            directory=self.run_dir / CORPUS_DIR / stage,
            env_file=self.driver.env_file,
            log=self.driver.log,
            driver=self.driver,
        )

    def past_the_barrier(self, monkeypatch) -> None:
        from yizkr.pipeline.clustering import run_stage

        monkeypatch.setattr(scratchwrite, "reset_instance", self.reset)
        # the session holds the lock: a second hold in this process would
        # wait for it
        monkeypatch.setattr(scratchwrite, "live_stack", contextlib.nullcontext)
        run_stage(self.context("clustering"))
        for name in ("reset", "register", "write", "site"):
            getattr(scratchwrite, f"run_{name}")(self.context(name))

    def record(self, stage: str) -> dict:
        return scratchwrite.read_record(self.run_dir / CORPUS_DIR / stage)


def statements(client: WikiClient, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    return client.fetch_claims([qid]).get(qid, {}).get(properties[key], [])


def cited(client: WikiClient, statement: dict, key: str) -> list[str]:
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties[key], [])
    ]


def strings(client: WikiClient, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in statements(client, qid, key)
    ]


def test_a_scratch_run_writes_unattended_after_a_reset(
    resets,
    config,
    client,
    tmp_path,
    monkeypatch,
):
    try:
        first = Run(tmp_path, config, resets, client, f"live-200-{MARK}")
        first.past_the_barrier(monkeypatch)
        fresh = WikiClient(config)
        assert first.resets == 1
        reset = first.record("reset")
        assert reset["action"] == scratchwrite.RESET_DONE

        written = first.record("write")
        assert written["depicts_left_out"] == 1
        assert written["failed"] == {} and written["refused"] == {}
        registered = first.record("register")["documents"]
        identity = scratchwrite.identity_map(first.context("write"))
        settings = scratchwrite.manifest_hash(first.driver.rundir)
        runs = {doc["run_qid"] for doc in written["documents"].values()}
        assert len(runs) == 2
        for run_qid in runs:
            assert strings(fresh, run_qid, "scratch_run") == [
                first.driver.run_id
            ]
            assert strings(fresh, run_qid, "run_manifest_hash") == [settings]
            assert strings(fresh, run_qid, "identity_map_hash") == [
                identity.hash
            ]
            for key in ("scratch_run", "run_manifest_hash", "instance_of"):
                for statement in statements(fresh, run_qid, key):
                    assert cited(fresh, statement, "stated_in") == [run_qid]

        _, labels = fresh.label_index()
        gustav = labels.get(GUSTAV)
        (sibling,) = statements(fresh, gustav, "sibling")
        assert sibling["rank"] == "normal"
        assert cited(fresh, sibling, "stated_in") == [
            registered[first.documents[1].sha1]
        ]
        assert cited(fresh, sibling, "inferred_from_extraction_run")[0] in runs
        # the depicts claim is never written
        for qid in registered.values():
            assert statements(fresh, qid, "depicts") == []

        recorded = recorded_contents(fresh)
        assert recorded.key == reset["key"]
        assert recorded.run_id == first.driver.run_id
        site = first.run_dir / CORPUS_DIR / "site" / "site"
        for page in site.glob("*.html"):
            assert first.driver.run_id in page.read_text()
            assert "development-stage scratch run" in page.read_text()

        # the same batches again: nothing reset, nothing written
        before = watermark(fresh)
        second = Run(tmp_path, config, resets, client, f"live-200b-{MARK}")
        second.past_the_barrier(monkeypatch)
        assert second.resets == 0
        assert second.record("reset")["action"] == scratchwrite.SKIPPED
        assert watermark(WikiClient(config)) == before

        # a write the stage did not make voids the record
        register_fictional(
            WikiClient(config),
            f"yizkr live test #200: a letter from Felix to Rahel {MARK}",
            "letter",
        )
        assert recorded_contents(WikiClient(config)).key is None
    finally:
        clear_contents(config, LIVE_TESTS)
    assert read_record(config)["cleared"]["by"] == LIVE_TESTS
