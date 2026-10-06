"""Live: a scratch run's place stage (#201; yizkr.pipeline.scratchwrite.
run_places), the place-hierarchy reconciliation after the write and before
the site, run over fictional documents on the one instance after a reset.

* The stage reads the written graph through the query service, asks a
  fake adjudicator, through the run's LLM cache, the questions the
  worklist derives from the chain of containers a mention record states
  (Flörburg in Kreis Peiltal, Hesse, Germany), and applies each
  containment link by link: never the shortcut, and nothing of
  Fläckfeld, which the adjudicator sets apart.
* Each `part_of` is at normal rank, stated in the extract whose record
  states it, and inferred from the stage's run item, which names the
  proposing model and is marked an unratified scratch run's.
* The instance-contents record takes the place key. A second run on the
  same batches resets nothing, writes nothing and asks nothing.
* The test ends by clearing the instance-contents key, as the session
  does: the next scratch run resets first.

It resets the one instance, so it runs only when asked, in a session of
its own, and never on a kept instance (test_live_scratch_reset.py's
`resets`): `YIZKR_LIVE_RESET=1 uv run pytest -m live
tests/integration/test_live_scratch_places.py`. The session holds the
live-stack lock, so the stages run under its hold. The adjudicator is a
fake, so the test makes no model call. Fictional data only
(docs/EXAMPLES.md): Leopold Löbig's birth register extract and a letter
from his brother Gustav, each label and media byte carrying the session's
mark.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from pathlib import Path

import pytest

from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.core.instance import clear_contents, read_record, recorded_contents
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import places, registrar, scratchwrite, writer
from yizkr.pipeline.orchestrate import DocumentRun
from yizkr.pipeline.scratch import CORPUS_DIR, CorpusContext, ScratchDriver

from . import test_live_scratch_reset
from .conftest import LIVE_TESTS, fictional_png, fixture_mark

# the reset of the stack beside the session's env file, as
# test_live_scratch_reset.py's tests take it: only when asked, in a
# session of its own, on an instance that is not kept
resets = test_live_scratch_reset.resets

pytestmark = pytest.mark.live

MARK = fixture_mark(
    "the scratch place stage #201", scratchwrite, places, writer, registrar
)
MODEL = "claude-sonnet-5"
LEOPOLD = f"Leopold Löbig {MARK}"
FLÖRBURG = f"Flörburg {MARK}"
KREIS = f"Kreis Peiltal {MARK}"
HESSE = f"Hesse {MARK}"
GERMANY = f"Germany {MARK}"
FLÄCKFELD = f"Fläckfeld {MARK}"
RUN = {
    "model_identifier": MODEL,
    "pipeline_version": "0.1.0",
    "started_at": "2026-10-06T09:00:00Z",
}
# the fake adjudicator's verdicts, by the two places' labels
VERDICTS = {
    (FLÖRBURG, KREIS): "a_within_b",
    (KREIS, HESSE): "a_within_b",
    (HESSE, GERMANY): "a_within_b",
}
QUESTION = re.compile(r"^(\d+)\. A: '([^']*)'.*?  vs  B: '([^']*)'", re.M)


class Adjudicator:
    """A fake engine: each question of a place prompt answered by the
    labels of its places (VERDICTS, else `uncertain`)."""

    model = MODEL
    model_identifier = MODEL
    effort = "high"
    config_hash = "0" * 64
    cli_version = None

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        lines = [
            json.dumps(
                {
                    "question": int(number),
                    "verdict": VERDICTS.get((a, b), "uncertain"),
                    "confidence": 0.9,
                    "evidence": "fictional",
                }
            )
            for number, a, b in QUESTION.findall(prompt)
        ]
        return "\n".join([*lines, '{"done": true}'])


def place(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "place",
    }


def born_at(sha1: str, where: dict) -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": "leopold",
            "label": LEOPOLD,
            "instance_of": "human",
        },
        "property": "place_of_birth",
        "value": {"type": "item", "value": where},
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "page_number": "1",
        },
        "confidence": 0.9,
    }


def entity(local_id: str, label: str, within=None) -> dict:
    found = {"id": local_id, "label": label, "kind": "place"}
    if within is not None:
        found["structure"] = {"kind": "place", "within": list(within)}
    return found


def documents() -> list[tuple[str, bytes, ClaimBatch]]:
    """(file name, bytes, batch) of each fictional document: the extract,
    which states Flörburg within Kreis Peiltal, Hesse, Germany, and the
    letter, which gives Fläckfeld."""
    extract = fictional_png(f"yizkr live test #201: extract {MARK}")
    letter = fictional_png(f"yizkr live test #201: letter {MARK}")
    one, two = (hashlib.sha1(data).hexdigest() for data in (extract, letter))
    leopold = {"id": "leopold", "label": LEOPOLD, "kind": "human"}
    return [
        (
            f"leopold-extract-{MARK}.png",
            extract,
            ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "entities": [
                        leopold,
                        entity("florburg", FLÖRBURG, [KREIS, HESSE, GERMANY]),
                        entity("kreis", KREIS),
                        entity("hesse", HESSE),
                        entity("germany", GERMANY),
                    ],
                    "claims": [
                        born_at(one, place("florburg", FLÖRBURG)),
                        born_at(one, place("kreis", KREIS)),
                        born_at(one, place("hesse", HESSE)),
                        born_at(one, place("germany", GERMANY)),
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
                    "entities": [leopold, entity("flackfeld", FLÄCKFELD)],
                    "claims": [born_at(two, place("flackfeld", FLÄCKFELD))],
                }
            ),
        ),
    ]


class Run:
    """A scratch run's documents past the barrier, its stages run in
    order with the session's config, under the session's hold on the
    live-stack lock, the reset the session's own (`resets`), and the
    fake adjudicator in the place stage's role, behind the run's LLM
    cache."""

    def __init__(self, tmp_path, config, reset_with, client, run_id):
        self.resets = 0
        self.folder = tmp_path / run_id / "corpus"
        self.folder.mkdir(parents=True, exist_ok=True)
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
        self.engine = Adjudicator()
        self.driver._engines[scratchwrite.PLACE_ROLE] = self.engine
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.documents = []
        for name, data, batch in documents():
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
        for name in ("reset", "register", "write", "places", "site"):
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


def targets(client: WikiClient, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]["id"]
        for statement in statements(client, qid, key)
    ]


def strings(client: WikiClient, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in statements(client, qid, key)
    ]


def test_a_scratch_run_reconciles_its_places_after_a_reset(
    resets,
    config,
    client,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YIZKR_LLM_MODEL", MODEL)
    try:
        first = Run(tmp_path, config, resets, client, f"live-201-{MARK}")
        first.past_the_barrier(monkeypatch)
        assert first.resets == 1
        fresh = WikiClient(config)
        _, labels = fresh.label_index()
        florburg, kreis, hesse, germany, flackfeld = (
            labels.get(label)
            for label in (FLÖRBURG, KREIS, HESSE, GERMANY, FLÄCKFELD)
        )
        # one call, through the cache, whose prompt names no QID
        (prompt,) = first.engine.prompts
        assert not re.search(r"\bQ\d+\b", prompt)

        # each link of the chain, never the shortcut
        assert targets(fresh, florburg, "part_of") == [kreis]
        assert targets(fresh, kreis, "part_of") == [hesse]
        assert targets(fresh, hesse, "part_of") == [germany]
        assert targets(fresh, germany, "part_of") == []
        assert targets(fresh, flackfeld, "part_of") == []

        record = first.record("places")
        run_item = record["run_qid"]
        extract = first.record("register")["documents"][
            first.documents[0].sha1
        ]
        for child in (florburg, kreis, hesse):
            (statement,) = statements(fresh, child, "part_of")
            assert statement["rank"] == "normal"
            assert cited(fresh, statement, "stated_in") == [extract]
            assert cited(fresh, statement, "inferred_from_extraction_run") == [
                run_item
            ]
        assert (
            fresh.find_item_by_label(
                f"place-hierarchy run {MODEL} {record['key'][:10]}"
            )
            == run_item
        )
        assert strings(fresh, run_item, "scratch_run") == [first.driver.run_id]
        counts = record["counts"]
        assert counts["applied"] == 3
        assert counts["cycles"] == counts["conflicts"] == 0
        assert counts["schema_rejected"] == 0

        # the instance holds what the place key makes
        recorded = recorded_contents(fresh)
        assert recorded.key == record["key"]
        assert recorded.run_id == first.driver.run_id
        site = first.run_dir / CORPUS_DIR / "site" / "site"
        assert any(site.glob("*.html"))

        # the same batches again: nothing reset, written or asked
        second = Run(tmp_path, config, resets, client, f"live-201b-{MARK}")
        second.driver._engines[scratchwrite.PLACE_ROLE] = first.engine
        second.past_the_barrier(monkeypatch)
        assert second.resets == 0
        assert second.record("reset")["action"] == scratchwrite.SKIPPED
        assert second.record("places")["skipped"] is True
        assert len(first.engine.prompts) == 1
        assert recorded_contents(WikiClient(config)).key == record["key"]
    finally:
        clear_contents(config, LIVE_TESTS)
    assert read_record(config)["cleared"]["by"] == LIVE_TESTS
