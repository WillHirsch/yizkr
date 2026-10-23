"""Live: a scratch run's place stage (#201; ADR-0038 §3 and §8.5-8.7;
yizkr.pipeline.scratchwrite.run_places), after the write and before the
site, run over fictional documents on the one instance after a reset.

The stage reads the written graph through the query service and asks a
fake adjudicator, through the run's LLM cache, which existing place each
container a mention record states means, where the container names more
than one place or no claim of the document gives the link. It checks:

* the records' containers carry their evidence (#569), and the stage
  reads them by name, nearest first, each link from the next;
* a link a record alone states (the 1905 marriage certificate's Kreis
  Mockburg within Hesse) is written with no page and no time: `part of`
  the place named, stated in the certificate's artifact and inferred
  from the stage's run item, which names the asking model and is marked
  an unratified scratch run's;
* a link a claim gives (the 1941 death certificate's Koshnovka within
  Kaldovsky District, in 1941), whose container names two places the
  certificate's manifest kept apart, is written to the one the answer
  names with the claim's time;
* an `uncertain` answer (the court prison "in Peiltal", which names the
  town and the district) writes nothing;
* a link that would close a cycle (the 1957 judgment's "Rekau, Rekau"
  read on the urban district, whose town the judgment puts inside it)
  is refused, counted and named in the stage's record;
* the instance-contents record takes the place key, and a second run on
  the same batches resets nothing, asks nothing and adds nothing;
* the test ends by clearing the instance-contents key, as the session
  does: the next scratch run resets first.

It resets the one instance, so it runs only when asked, in a session of
its own, and never on a kept instance (test_live_scratch_reset.py's
`resets`): `YIZKR_LIVE_RESET=1 uv run pytest -m live
tests/integration/test_live_scratch_places.py`. The session holds the
live-stack lock, so the stages run under its hold. The adjudicator is a
fake, so the test makes no model call. Fictional data only
(docs/EXAMPLES.md): the dated containment is the universe's worked
example, which no source in it states; each label and media byte carries
the session's mark.
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
    "the scratch place stage #201, identity questions",
    scratchwrite,
    places,
    writer,
    registrar,
)
MODEL = "claude-sonnet-5"
FLÄCKFELD = f"Fläckfeld {MARK}"
MOCKBURG = f"Kreis Mockburg {MARK}"
HESSE = f"Hesse {MARK}"
KOSHNOVKA = f"Koshnovka {MARK}"
KALDOVSKY = f"Kaldovsky District {MARK}"
CHADSK = f"Chadsk Oblast {MARK}"
PRISON = f"Gerichtsgefängnis Peiltal {MARK}"
PEILTAL = f"Peiltal {MARK}"
KREIS_PEILTAL = f"Kreis Peiltal {MARK}"
REKAU = f"Rekau {MARK}"
STADTKREIS = f"Stadtkreis Rekau {MARK}"
RUN = {
    "model_identifier": MODEL,
    "pipeline_version": "0.1.0",
    "started_at": "2026-10-23T09:00:00Z",
}
# a question of the prompt: its number, the container it asks of, and its
# candidates, each by its number and name
QUESTION = re.compile(
    r"^(\d+)\. A record states .*? Which place does '([^']*)', container "
    r"\d+ of that chain, name\?\n((?:   place \d+: .*\n?)+)",
    re.M,
)
CANDIDATE = re.compile(r"^   place (\d+): (.*)$", re.M)


class Adjudicator:
    """A fake engine: each question of a place prompt answered with its
    one candidate where it has one; with the candidate whose name holds
    `choices[container]`; or `uncertain`."""

    model = MODEL
    model_identifier = MODEL
    effort = "high"
    config_hash = "0" * 64
    cli_version = None

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.choices: dict[str, str] = {}

    def choose(self, container: str, block: str):
        candidates = CANDIDATE.findall(block)
        wanted = self.choices.get(container)
        if wanted is None:
            return 1 if len(candidates) == 1 else places.UNCERTAIN
        for number, name in candidates:
            if wanted in name:
                return int(number)
        return places.UNCERTAIN

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        lines = [
            json.dumps(
                {
                    "question": int(number),
                    "place": self.choose(container, block),
                    "confidence": 0.9,
                    "evidence": "fictional",
                }
            )
            for number, container, block in QUESTION.findall(prompt)
        ]
        return "\n".join([*lines, '{"done": true}'])


def place(local_id: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": "place",
    }


def entity(local_id: str, label: str, within=None, aliases=()) -> dict:
    """A place entity; its record's containers, nearest first, each with
    the mention that states it, the place and its chain, as its evidence
    (ADR-0035 §2.2, #569)."""
    found = {
        "id": local_id,
        "label": label,
        "kind": "place",
        "aliases": list(aliases),
    }
    if within is not None:
        evidence = ", ".join([label, *within])
        found["structure"] = {
            "kind": "place",
            "within": [
                {"name": name, "evidence": evidence} for name in within
            ],
        }
    return found


def part_of(sha1: str, child: dict, parent: dict, *dated) -> dict:
    return {
        "subject": child,
        "property": "part_of",
        "value": {"type": "item", "value": parent},
        "qualifiers": [
            {"property": key, "value": {"type": "time", "year": year}}
            for key, year in dated
        ],
        "source": {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "page_number": "1",
        },
    }


def batch(entities, claims) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "entities": entities, "claims": claims}
    )


def documents() -> list[tuple[str, bytes, ClaimBatch]]:
    """(file name, bytes, batch) of each fictional document."""
    data = {
        name: fictional_png(f"yizkr live test #201: {name} {MARK}")
        for name in ("marriage", "death", "prison", "judgment")
    }
    sha = {
        name: hashlib.sha1(found).hexdigest() for name, found in data.items()
    }
    flackfeld, mockburg, hesse = (
        place("f", FLÄCKFELD),
        place("m", MOCKBURG),
        place("h", HESSE),
    )
    kosh, kald, kald2, chadsk = (
        place("k", KOSHNOVKA),
        place("d", KALDOVSKY),
        place("d2", KALDOVSKY),
        place("c", CHADSK),
    )
    prison, town, district = (
        place("p", PRISON),
        place("t", PEILTAL),
        place("kp", KREIS_PEILTAL),
    )
    rekau, stadtkreis = place("r", REKAU), place("s", STADTKREIS)
    return [
        (
            # Fläckfeld, Kreis Mockburg, Hesse: its claims give Fläckfeld
            # within Kreis Mockburg, and within Hesse; Kreis Mockburg
            # within Hesse the record alone states
            f"marriage-1905-{MARK}.png",
            data["marriage"],
            batch(
                [
                    entity("f", FLÄCKFELD, [MOCKBURG, HESSE]),
                    entity("m", MOCKBURG),
                    entity("h", HESSE),
                ],
                [
                    part_of(sha["marriage"], flackfeld, mockburg),
                    part_of(sha["marriage"], flackfeld, hesse),
                ],
            ),
        ),
        (
            # the worked examples: Koshnovka in Kaldovsky District in
            # 1941, Kaldovsky District in Chadsk Oblast from 1937; a
            # second Kaldovsky District, which the manifest kept apart
            f"death-1941-{MARK}.png",
            data["death"],
            batch(
                [
                    entity("k", KOSHNOVKA, [KALDOVSKY, CHADSK]),
                    entity("d", KALDOVSKY),
                    entity("d2", KALDOVSKY),
                    entity("c", CHADSK),
                ],
                [
                    part_of(sha["death"], kosh, kald, ("point_in_time", 1941)),
                    part_of(sha["death"], kald, chadsk, ("start_time", 1937)),
                    part_of(sha["death"], kald2, chadsk),
                ],
            ),
        ),
        (
            # the court prison "in Peiltal", a name the town and the
            # district both answer to
            f"prison-1939-{MARK}.png",
            data["prison"],
            batch(
                [
                    entity("p", PRISON, [PEILTAL]),
                    entity("t", PEILTAL),
                    entity("kp", KREIS_PEILTAL, aliases=[PEILTAL]),
                ],
                [
                    part_of(sha["prison"], prison, town),
                    part_of(sha["prison"], town, district),
                ],
            ),
        ),
        (
            # the town Rekau in the urban district Rekau; "Rekau, Rekau"
            # read on the district's mention
            f"judgment-1957-{MARK}.png",
            data["judgment"],
            batch(
                [
                    entity("r", REKAU),
                    entity("s", STADTKREIS, [REKAU], aliases=[REKAU]),
                ],
                [part_of(sha["judgment"], rekau, stadtkreis)],
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
        self.engine = Adjudicator()
        self.driver._engines[scratchwrite.PLACE_ROLE] = self.engine
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.documents = []
        for name, data, found in documents():
            path = self.folder / name
            path.write_bytes(data)
            doc = DocumentRun(path, self.run_dir)
            doc.dir.mkdir(parents=True, exist_ok=True)
            doc.batch.write_text(found.model_dump_json(indent=2))
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

    def stages(self, monkeypatch, *names: str) -> None:
        from yizkr.pipeline.clustering import run_stage

        monkeypatch.setattr(scratchwrite, "reset_instance", self.reset)
        # the session holds the lock: a second hold in this process would
        # wait for it
        monkeypatch.setattr(scratchwrite, "live_stack", contextlib.nullcontext)
        for name in names:
            if name == "clustering":
                run_stage(self.context(name))
            else:
                getattr(scratchwrite, f"run_{name}")(self.context(name))

    def record(self, stage: str) -> dict:
        return scratchwrite.read_record(self.run_dir / CORPUS_DIR / stage)

    def created(self, document: int, local_id: str) -> str:
        sha1 = self.documents[document].sha1
        return self.record("write")["documents"][sha1]["created_items"][
            local_id
        ]

    def artifact(self, document: int) -> str:
        return self.record("register")["documents"][
            self.documents[document].sha1
        ]


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


def target(statement: dict) -> str:
    return statement["mainsnak"]["datavalue"]["value"]["id"]


def years(client: WikiClient, statement: dict) -> dict[str, int]:
    """Each qualifier of a statement, by its manifest key, with its year."""
    properties, _ = client.vocabulary()
    keys = {pid: key for key, pid in properties.items()}
    return {
        keys[pid]: int(snaks[0]["datavalue"]["value"]["time"][1:5])
        for pid, snaks in statement.get("qualifiers", {}).items()
    }


def by_run(client: WikiClient, qid: str, run_item: str) -> list[dict]:
    """The part_of statements of `qid` the place stage wrote."""
    return [
        statement
        for statement in statements(client, qid, "part_of")
        if run_item in cited(client, statement, "inferred_from_extraction_run")
    ]


def strings(client: WikiClient, qid: str, key: str) -> list[str]:
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in statements(client, qid, key)
    ]


def description(client: WikiClient, qid: str) -> str:
    entity = client.wbi.item.get(qid)
    return str(entity.descriptions.get("en") or "")


def test_a_scratch_run_asks_and_writes_its_places_after_a_reset(
    resets,
    config,
    client,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YIZKR_LLM_MODEL", MODEL)
    try:
        first = Run(tmp_path, config, resets, client, f"live-201-{MARK}")
        first.stages(monkeypatch, "clustering", "reset", "register", "write")
        assert first.resets == 1
        fresh = WikiClient(config)
        # the answers: Kreis Mockburg, Hesse and Rekau name one place
        # each; Kaldovsky District the second of the two, by its
        # description; "Peiltal" is left uncertain
        namesake = first.created(1, "d2")
        assert description(fresh, namesake) != description(
            fresh, first.created(1, "d")
        )
        first.engine.choices = {
            # the description, as the prompt gives it after the label
            KALDOVSKY: f"({description(fresh, namesake)};",
            PEILTAL: places.UNCERTAIN,
        }
        first.stages(monkeypatch, "places", "site")
        fresh = WikiClient(config)
        record = first.record("places")
        run_item = record["run_qid"]

        # the calls go through the cache, and no prompt names a QID
        assert first.engine.prompts
        for prompt in first.engine.prompts:
            assert not re.search(r"\bQ\d+\b", prompt)

        # a link the record alone states: no page and no time
        mockburg, hesse = first.created(0, "m"), first.created(0, "h")
        (statement,) = statements(fresh, mockburg, "part_of")
        assert target(statement) == hesse
        assert statement["rank"] == "normal"
        assert cited(fresh, statement, "stated_in") == [first.artifact(0)]
        assert cited(fresh, statement, "inferred_from_extraction_run") == [
            run_item
        ]
        properties, _ = fresh.vocabulary()
        assert all(
            set(reference["snaks"])
            <= {
                properties["stated_in"],
                properties["inferred_from_extraction_run"],
            }
            for reference in statement["references"]
        )
        assert years(fresh, statement) == {}

        # a link a claim gives, to the namesake the answer names, with the
        # claim's time; the claim the write wrote stands beside it
        koshnovka = first.created(1, "k")
        (statement,) = by_run(fresh, koshnovka, run_item)
        assert target(statement) == namesake
        assert years(fresh, statement) == {"point_in_time": 1941}
        assert cited(fresh, statement, "stated_in") == [first.artifact(1)]
        assert sorted(
            target(found) for found in statements(fresh, koshnovka, "part_of")
        ) == sorted([first.created(1, "d"), namesake])

        # an uncertain answer writes nothing
        prison = first.created(2, "p")
        assert by_run(fresh, prison, run_item) == []

        # a cycle is refused, counted and named
        stadtkreis, rekau = first.created(3, "s"), first.created(3, "r")
        assert statements(fresh, stadtkreis, "part_of") == []
        (refused,) = record["refused"]
        assert (refused["child_label"], refused["parent_label"]) == (
            STADTKREIS,
            REKAU,
        )
        assert (refused["child"], refused["parent"]) == (stadtkreis, rekau)

        counts = record["counts"]
        assert counts["written"] == 2
        assert counts["record_only"] == 2
        assert counts["record_only_written"] == 1
        assert counts["uncertain"] == 1
        assert counts["cycles"] == 1
        assert counts["conflicts"] == counts["schema_rejected"] == 0

        # the run item names the asking model and the scratch run
        assert (
            fresh.find_item_by_label(
                f"place-hierarchy run {MODEL} {record['key'][:10]}"
            )
            == run_item
        )
        assert strings(fresh, run_item, "scratch_run") == [first.driver.run_id]

        # the instance holds what the place key makes
        recorded = recorded_contents(fresh)
        assert recorded.key == record["key"]
        assert recorded.run_id == first.driver.run_id
        site = first.run_dir / CORPUS_DIR / "site" / "site"
        assert any(site.glob("*.html"))

        # the same batches again: nothing reset, asked or added
        asked = len(first.engine.prompts)
        before = {
            qid: statements(fresh, qid, "part_of")
            for qid in (mockburg, koshnovka, prison, stadtkreis)
        }
        second = Run(tmp_path, config, resets, client, f"live-201b-{MARK}")
        second.driver._engines[scratchwrite.PLACE_ROLE] = first.engine
        second.stages(
            monkeypatch,
            "clustering",
            "reset",
            "register",
            "write",
            "places",
            "site",
        )
        assert second.resets == 0
        assert second.record("reset")["action"] == scratchwrite.SKIPPED
        assert second.record("places")["skipped"] is True
        assert len(first.engine.prompts) == asked
        again = WikiClient(config)
        assert {
            qid: statements(again, qid, "part_of") for qid in before
        } == before
        assert recorded_contents(again).key == record["key"]
    finally:
        clear_contents(config, LIVE_TESTS)
    assert read_record(config)["cleared"]["by"] == LIVE_TESTS
