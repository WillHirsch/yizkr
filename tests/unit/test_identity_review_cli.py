"""Human review commands use real files and durable SQLite decisions."""

import csv
import json

import pytest

from yizkr import cli
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.exits import EXIT_OK, EXIT_REFUSED
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline import llm
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.transcribe import Transcription


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail(
            "identity review must not load graph or model configuration"
        )

    monkeypatch.setattr(cli.Config, "load", refuse)
    monkeypatch.setattr(cli, "_client", refuse)
    monkeypatch.setattr(llm, "make_client", refuse)


def request_files(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    artifact = ArtifactRef.from_bytes(b"fictional Ursula family record")
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "authored-fixture",
                "pipeline_version": "test",
                "started_at": "2026-01-01T12:00:00Z",
            },
            "entities": [
                {
                    "id": key,
                    "label": "Ursula Löbig",
                    "aliases": ["Ursula Löbig"],
                    "kind": "human",
                    "mention_lines": [line],
                }
                for key, line in (("daughter", 1), ("widow", 2))
            ],
        }
    )
    reading = Transcription(
        artifact=artifact,
        text=(
            "Ursula Löbig, daughter of Sebastian.\n"
            "Ursula Löbig, an unrelated widow in Flörburg."
        ),
        language="de",
        engine="fixture",
    )
    (inputs / "batch.json").write_text(batch.model_dump_json())
    (inputs / "reading.json").write_text(reading.model_dump_json())
    spec = {
        "sources": [
            {
                "artifact_sha1": artifact.sha1,
                "batch": "batch.json",
                "reading": "reading.json",
            }
        ],
        "proposals": [
            {
                "operation": "new_entity",
                "mentions": [
                    {"artifact_sha1": artifact.sha1, "local_id": key}
                ],
            }
            for key in ("daughter", "widow")
        ],
    }
    path = inputs / "request.json"
    save_request(path, spec)
    return path, spec


def save_request(path, spec):
    path.write_text(json.dumps(spec))


def invoke(action, store, **options):
    argv = ["ingest", "identity-review", action, "--store", str(store)]
    for key, value in options.items():
        argv.extend((f"--{key}", str(value)))
    return cli.main(argv)


def edit_sheet(path, transform):
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames
        rows = list(reader)
    transform(rows)
    with path.open("w", newline="") as target:
        writer = csv.DictWriter(target, fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def approve(rows):
    for row in rows:
        row["decision"] = "approve"


def projection(request, store, tmp_path):
    output = tmp_path / "projection.json"
    assert invoke("project", store, request=request, out=output) == EXIT_OK
    return json.loads(output.read_text())


def prepare(request, store, sheet):
    assert invoke("prepare", store, request=request, out=sheet) == EXIT_OK


def test_reopened_commands_preserve_homonym_decisions_after_id_and_order_swap(
    tmp_path,
):
    request, spec = request_files(tmp_path)
    store, sheet = tmp_path / "review.sqlite", tmp_path / "review.csv"
    prepare(request, store, sheet)

    def choose(rows):
        for row in rows:
            local = json.loads(row["scope"])[0]["observation"]["local_id"]
            row["decision"] = "approve" if local == "daughter" else "reject"

    edit_sheet(sheet, choose)
    assert invoke("record", store, csv=sheet, actor="curator") == EXIT_OK
    before = projection(request, store, tmp_path)
    assert len(before["new_entities"]) == 1
    assert len(before["decision_revisions"]) == 2

    batch_path = request.parent / "batch.json"
    batch = ClaimBatch.model_validate_json(batch_path.read_text())
    batch.entities[0].id = "widow"
    batch.entities[0].label = "Ursula"
    batch.entities[1].id = "daughter"
    batch.entities.reverse()
    batch_path.write_text(batch.model_dump_json())
    # The proposed operations still cover both IDs; source positions establish
    # correspondence, while their identical names cannot establish identity.
    after = projection(request, store, tmp_path)
    assert after["new_entities"] == before["new_entities"]
    assert after["decision_revisions"] == before["decision_revisions"]
    assert all(app["status"] == "applied" for app in after["applications"])
    with ReviewStore(store) as reopened:
        decisions = [
            reopened.get_decision(proposal.id)
            for proposal in reopened.list_proposals()
        ]
        assert {decision.outcome for decision in decisions} == {
            "approve",
            "reject",
        }
        assert all(decision.actor == "curator" for decision in decisions)
        assert all(decision.review_sheet_id for decision in decisions)


def test_new_source_needs_review_without_removing_old_approval(tmp_path):
    request, spec = request_files(tmp_path)
    store, sheet = tmp_path / "review.sqlite", tmp_path / "review.csv"
    prepare(request, store, sheet)
    edit_sheet(sheet, approve)
    assert invoke("record", store, csv=sheet, actor="curator") == EXIT_OK
    before = projection(request, store, tmp_path)

    new_artifact = ArtifactRef.from_bytes(b"fictional later Ursula record")
    original = ClaimBatch.model_validate_json(
        (request.parent / "batch.json").read_text()
    )
    original.entities = original.entities[:1]
    original.entities[0].id = "later"
    (request.parent / "later.json").write_text(original.model_dump_json())
    (request.parent / "later-reading.json").write_text(
        Transcription(
            artifact=new_artifact,
            text="Ursula Löbig, daughter of Sebastian.",
            language="de",
            engine="fixture",
        ).model_dump_json()
    )
    spec["sources"].append(
        {
            "artifact_sha1": new_artifact.sha1,
            "batch": "later.json",
            "reading": "later-reading.json",
        }
    )
    spec["proposals"].append(
        {
            "operation": "new_entity",
            "mentions": [
                {"artifact_sha1": new_artifact.sha1, "local_id": "later"}
            ],
        }
    )
    save_request(request, spec)
    after = projection(request, store, tmp_path)
    assert after["new_entities"] == before["new_entities"]
    assert after["decision_revisions"] == before["decision_revisions"]
    assert sorted(app["status"] for app in after["applications"]) == [
        "applied",
        "applied",
        "needs_review",
    ]


def test_current_blocker_suspends_only_affected_approval(tmp_path):
    request, spec = request_files(tmp_path)
    store, sheet = tmp_path / "review.sqlite", tmp_path / "review.csv"
    prepare(request, store, sheet)
    edit_sheet(sheet, approve)
    assert invoke("record", store, csv=sheet, actor="curator") == EXIT_OK
    before = projection(request, store, tmp_path)
    spec["blocked"] = [
        {
            "mention": spec["proposals"][0]["mentions"][0],
            "reasons": ["unresolved source attribution"],
        }
    ]
    save_request(request, spec)
    after = projection(request, store, tmp_path)
    assert len(after["new_entities"]) == 1
    assert set(after["new_entities"]) < set(before["new_entities"])
    blocked = [
        app for app in after["applications"] if app["status"] == "blocked"
    ]
    assert len(blocked) == 1
    assert "unresolved source attribution" in blocked[0]["reasons"]
    assert blocked[0]["decision"]["outcome"] == "approve"
    spec["blocked"] = []
    save_request(request, spec)
    assert (
        projection(request, store, tmp_path)["new_entities"]
        == before["new_entities"]
    )


@pytest.mark.parametrize(
    "field",
    ["status", "blockers", "decision_revision", "digest", "scope", "sheet_id"],
)
def test_altered_sheet_refuses_entire_packet(tmp_path, capsys, field):
    request, _ = request_files(tmp_path)
    store, sheet = tmp_path / "review.sqlite", tmp_path / "review.csv"
    prepare(request, store, sheet)

    def tamper(rows):
        approve(rows)
        rows[-1][field] += "altered"

    edit_sheet(sheet, tamper)
    assert invoke("record", store, csv=sheet, actor="curator") == EXIT_REFUSED
    assert "Traceback" not in capsys.readouterr().err
    after = projection(request, store, tmp_path)
    assert after["new_entities"] == []
    assert after["decision_revisions"] == []


def test_concurrently_recorded_decision_refuses_whole_stale_sheet(
    tmp_path, capsys
):
    request, _ = request_files(tmp_path)
    store = tmp_path / "review.sqlite"
    stale, concurrent = tmp_path / "stale.csv", tmp_path / "concurrent.csv"
    prepare(request, store, stale)
    concurrent.write_bytes(stale.read_bytes())
    edit_sheet(concurrent, lambda rows: rows[-1].update(decision="approve"))
    assert (
        invoke("record", store, csv=concurrent, actor="first curator")
        == EXIT_OK
    )

    def stale_decisions(rows):
        approve(rows)
        rows[-1]["decision"] = "reject"

    edit_sheet(stale, stale_decisions)
    assert (
        invoke("record", store, csv=stale, actor="second curator")
        == EXIT_REFUSED
    )
    assert "changed since" in capsys.readouterr().err
    after = projection(request, store, tmp_path)
    # The first row must roll back when the later decision has changed.
    assert len(after["new_entities"]) == 1
    assert len(after["decision_revisions"]) == 1
    assert sorted(app["status"] for app in after["applications"]) == [
        "applied",
        "needs_review",
    ]


@pytest.mark.parametrize("target_state", ["changed_generation", "absent"])
def test_assignment_requires_current_generation_qualified_target(
    tmp_path, target_state
):
    request, spec = request_files(tmp_path)
    binding = EntityBinding(
        deployment_id="1" * 32,
        generation="2" * 32,
        entity_id="3" * 32,
        intent_key="4" * 64,
        qid="Q7001",
        revision=1,
    ).model_dump(mode="json")
    spec["proposals"] = [
        dict(spec["proposals"][0], operation="assign", target=binding)
    ]
    spec["current_targets"] = [binding]
    save_request(request, spec)
    store, sheet = tmp_path / "review.sqlite", tmp_path / "review.csv"
    prepare(request, store, sheet)
    edit_sheet(sheet, approve)
    assert invoke("record", store, csv=sheet, actor="curator") == EXIT_OK
    before = projection(request, store, tmp_path)
    assert list(before["assignments"].values()) == [binding]

    spec["current_targets"] = (
        [dict(binding, generation="5" * 32)]
        if target_state == "changed_generation"
        else []
    )
    save_request(request, spec)
    after = projection(request, store, tmp_path)
    assert after["assignments"] == {}
    assert after["applications"][0]["status"] == "blocked"


@pytest.mark.parametrize("failure", ["missing_request", "invalid_database"])
def test_input_and_sqlite_errors_are_clean_refusals(tmp_path, capsys, failure):
    request, _ = request_files(tmp_path)
    store = tmp_path / "review.sqlite"
    if failure == "missing_request":
        request.unlink()
    else:
        store.write_text("not a SQLite database")
    assert (
        invoke("prepare", store, request=request, out=tmp_path / "review.csv")
        == EXIT_REFUSED
    )
    assert not (tmp_path / "review.csv").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("action", ["prepare", "project"])
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_symlinked_database_protects_real_sqlite_sidecars(
    tmp_path,
    action,
    suffix,
):
    request, _ = request_files(tmp_path)
    real = tmp_path / "actual.sqlite"
    with ReviewStore(real):
        pass
    original = real.read_bytes()
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(real)
    sidecar = tmp_path / f"actual.sqlite{suffix}"
    assert invoke(action, alias, request=request, out=sidecar) == EXIT_REFUSED
    assert not sidecar.exists()
    assert real.read_bytes() == original
