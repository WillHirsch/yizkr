"""The CLI's exit statuses (yizkr.core.exits): a refusal, a pause and a
failure each end with their message alone and a status of their own, not
a traceback (#256)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.identity import mapped
from tests.unit.test_faces import depicted, portrait_faces, wiki_with_portrait
from tests.unit.test_pipeline import (
    MEDIA_REF,
    MEDIA_SHA1,
    OTHER_SHA1,
    FakeWiki,
    _citing,
)
from yizkr import cli
from yizkr.core.exits import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STOPPED,
)
from yizkr.pipeline import scratch
from yizkr.pipeline.llm import (
    REFUSAL_STOP,
    ClassifierRefused,
    CliConfigEnvironment,
    CliHomeRelative,
    CliToolsMismatch,
    CliVersionMismatch,
    ModelUnsupported,
    OutputLimitReached,
    PassPaused,
    UsageLimitReached,
)
from yizkr.pipeline.llmcache import CacheInUse, CacheRefused
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.rundir import RunSettingsConflict

# one of each pause the engine and its cache raise, each with its message
PAUSES = [
    CliVersionMismatch("the claude CLI is 2.1.0, not 2.0.9, the pinned one"),
    CliConfigEnvironment("the claude CLI's config sets env: remove it"),
    CliToolsMismatch("a call offered the model the tool Bash"),
    CliHomeRelative("HOME is a relative path: home/archivist"),
    ModelUnsupported(
        "the claude CLI does not support claude-example-1",
        model="claude-example-1",
    ),
    UsageLimitReached("the five-hour window is used up", reason="usage"),
    CacheRefused("var/llm-cache cannot be the LLM cache: a git work tree"),
    CacheInUse(Path("var/llm-cache"), {"pid": 4711}),
]


def _subclasses(cls: type) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found |= {sub, *_subclasses(sub)}
    return found


def test_the_statuses_are_distinct():
    statuses = [EXIT_OK, EXIT_FAILED, EXIT_STOPPED, EXIT_REFUSED]
    assert len(set([*statuses, EXIT_INTERRUPTED])) == 5


def test_the_scratch_commands_use_the_same_statuses():
    assert (
        scratch.EXIT_OK,
        scratch.EXIT_FAILED,
        scratch.EXIT_STOPPED,
        scratch.EXIT_INTERRUPTED,
    ) == (EXIT_OK, EXIT_FAILED, EXIT_STOPPED, EXIT_INTERRUPTED)


def test_every_pause_is_tested():
    # a test-only subclass defined elsewhere is not the engine's
    ours = {
        cls
        for cls in _subclasses(PassPaused)
        if cls.__module__.startswith("yizkr.")
    }
    assert ours == {type(pause) for pause in PAUSES}


def test_the_module_docstring_documents_the_statuses():
    doc = cli.__doc__
    for status in (EXIT_FAILED, EXIT_STOPPED, EXIT_REFUSED, EXIT_INTERRUPTED):
        assert f"\n  {status} " in doc


class TestUsage:
    def test_a_usage_error_is_a_refusal_not_a_pause(self, capsys):
        # argparse's own status, 2, is a pause's
        assert cli.main(["ingest", "write"]) == EXIT_REFUSED
        err = capsys.readouterr().err
        assert "the following arguments are required: --batch" in err

    def test_help_exits_0(self, capsys):
        with pytest.raises(SystemExit) as raised:
            cli.main(["--help"])
        assert raised.value.code == 0
        out = capsys.readouterr().out
        # the table keeps its lines: the description is not reflowed
        assert "\n  3    refused, before doing what was asked" in out
        assert "\n  130  interrupted (Ctrl-C)\n" in out

    def test_a_usage_error_of_a_scratch_command_is_a_refusal(self, capsys):
        assert cli.main(["scratch", "watch", "--interval", "x"]) == (
            EXIT_REFUSED
        )


class TestScratch:
    """The scratch commands keep a failure's status for a refusal."""

    def test_a_refusal_is_a_failure(self, capsys):
        status = cli.main(["scratch", "watch", "--interval", "0"])
        assert status == EXIT_FAILED
        assert capsys.readouterr().err == "--interval must be positive\n"


def _no_traceback(err: str) -> None:
    assert "Traceback" not in err


class TestIngestWrite:
    def test_unapproved_depicts_is_refused_in_its_message(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = wiki_with_portrait()
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        batch = tmp_path / "faces-batch.json"
        batch.write_text(portrait_faces().model_dump_json())

        status = cli.main(["ingest", "write", "--batch", str(batch)])

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err.startswith("2 depicts claim(s) carry no per-row human")
        assert err.endswith("Nothing was written.\n")
        assert err.count("\n") == 1
        assert depicted(wiki) == {}

    def test_an_unregistered_artifact_is_refused_in_its_message(
        self, tmp_path, monkeypatch, capsys
    ):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        claims = _citing(MEDIA_SHA1, OTHER_SHA1)
        batch = tmp_path / "batch.json"
        batch.write_text(claims.model_dump_json())
        identity = tmp_path / "identity-map.json"
        identity.write_text(json.dumps(mapped(claims).data))

        status = cli.main(
            [
                *("ingest", "write", "--batch", str(batch)),
                *("--identity-map", str(identity)),
            ]
        )

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err == (
            f"1 cited artifact(s) not registered on this instance: no item "
            f"carries media SHA-1 {OTHER_SHA1[:12]}. Register the media "
            f"before writing claims that cite it.\n"
        )
        assert wiki.wbi.item.writes == 0

    def test_another_identity_map_is_refused_in_its_message(
        self, tmp_path, monkeypatch, capsys
    ):
        from tests.unit.test_identity_map_writes import _eugens, _tree, _wiki
        from yizkr.pipeline.writer import BatchWriter

        # the family history written with its own map; the grown map of
        # the history and the tree would place the Eugens anew
        wiki = _wiki()
        BatchWriter(wiki).write_batch(_eugens(), identity=mapped(_eugens()))
        writes = wiki.wbi.item.writes
        monkeypatch.setattr(cli, "_client", lambda args: wiki)
        batch = tmp_path / "tree.json"
        batch.write_text(_tree().model_dump_json())
        identity = tmp_path / "identity-map.json"
        identity.write_text(json.dumps(mapped(_eugens(), _tree()).data))

        status = cli.main(
            [
                *("ingest", "write", "--batch", str(batch)),
                *("--identity-map", str(identity)),
            ]
        )

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err.startswith(
            "the instance holds the writes of another identity map"
        )
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize(
        "text",
        ["{not json", '{"claims": "none"}'],
        ids=["not JSON", "not a batch"],
    )
    def test_a_batch_that_does_not_load_is_refused_in_one_line(
        self, tmp_path, capsys, text
    ):
        batch = tmp_path / "batch.json"
        batch.write_text(text)

        status = cli.main(["ingest", "write", "--batch", str(batch)])

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err.startswith("batch.json is not a claim batch this step ")
        assert err.endswith("; nothing was written\n")
        assert err.count("\n") == 1

    def test_a_date_before_the_common_era_is_refused_in_one_line(
        self, tmp_path, capsys
    ):
        # ADR-0034: the schema refuses it, so the batch does not load
        data = json.loads(_citing(MEDIA_SHA1).model_dump_json())
        data["claims"][0]["value"]["year"] = -1836
        batch = tmp_path / "batch.json"
        batch.write_text(json.dumps(data))

        status = cli.main(["ingest", "write", "--batch", str(batch)])

        err = capsys.readouterr().err
        assert status == EXIT_REFUSED
        _no_traceback(err)
        assert err.startswith("batch.json is not a claim batch this step ")
        assert "claims.0.value" in err
        assert err.count("\n") == 1

    def test_a_missing_batch_is_refused_in_one_line(self, tmp_path, capsys):
        missing = tmp_path / "none.json"

        status = cli.main(["ingest", "write", "--batch", str(missing)])

        assert status == EXIT_REFUSED
        assert (
            capsys.readouterr().err
            == f"{missing}: No such file or directory\n"
        )


class TestIngestExtract:
    @pytest.mark.parametrize(
        "collision",
        [
            "claims",
            "media",
            "transcription-symlink",
            "transcription-hardlink",
            "chunk-workspace",
        ],
    )
    def test_output_aliases_are_refused_without_changing_inputs(
        self, tmp_path, monkeypatch, capsys, collision
    ):
        from tests.unit.test_pipeline import MEDIA
        from yizkr.pipeline import llm
        from yizkr.pipeline.transcribe import Transcription

        def no_engine(*args, **kwargs):
            raise AssertionError("an aliased output reached the engine")

        monkeypatch.setattr(llm, "make_client", no_engine)
        out = tmp_path / "batch.json"
        media = out if collision == "media" else tmp_path / "source.png"
        media.write_bytes(MEDIA)
        reading = tmp_path / "transcription.json"
        if collision == "chunk-workspace":
            reading = tmp_path / "batch.json.chunks" / "transcription.json"
            reading.parent.mkdir()
        reading.write_text(
            Transcription(
                artifact=MEDIA_REF,
                text="Leopold Löbig, born May 1914",
                engine="authored-fictional-text",
            ).model_dump_json()
        )
        claims = tmp_path / (
            "batch.json.partial.json"
            if collision == "claims"
            else "claims.json"
        )
        claims.write_text(_citing(MEDIA_SHA1).model_dump_json())
        if collision.startswith("transcription-"):
            raw = tmp_path / "batch.json.raw.txt"
            if collision.endswith("symlink"):
                raw.symlink_to(reading)
            else:
                raw.hardlink_to(reading)
        before = {
            path: path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        engine_args = (
            ["--engine", "file", "--claims-file", str(claims)]
            if collision in {"claims", "media"}
            else [
                "--engine",
                "llm",
                "--strategy",
                "chunked" if collision == "chunk-workspace" else "single",
            ]
        )

        status = cli.main(
            [
                "ingest",
                "extract",
                "--transcription",
                str(reading),
                "--media",
                str(media),
                "--out",
                str(out),
                *engine_args,
            ]
        )

        assert status == EXIT_REFUSED
        assert "refusing" in capsys.readouterr().err
        assert {
            path: path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        } == before

    def test_partial_chunks_are_diagnostic_not_an_accepted_batch(
        self, tmp_path, monkeypatch, capsys
    ):
        from pydantic import ValidationError

        from tests.unit.test_chunking import (
            LINE_LENGTH,
            PlainLlm,
            claim,
            entity_manifest_json,
            ndjson,
        )
        from tests.unit.test_chunking import (
            transcription as reading,
        )
        from yizkr.claims import ClaimBatch
        from yizkr.pipeline import llm

        engine = PlainLlm(
            [
                entity_manifest_json(),
                RuntimeError("engine down"),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        monkeypatch.setattr(llm, "make_client", lambda *a, **kw: engine)
        transcription = tmp_path / "transcription.json"
        transcription.write_text(reading().model_dump_json())
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)

        argv = [
            *("ingest", "extract", "--transcription", str(transcription)),
            *("--engine", "llm", "--strategy", "chunked"),
            *("--chunk-budget", str(LINE_LENGTH), "--chunk-retries", "0"),
            *("--out", str(out)),
        ]
        status = cli.main(argv)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "not a completed batch" in captured.err
        assert not out.exists()
        assert (tmp_path / "batch.json.stale").read_text() == self.EARLIER
        diagnostic = tmp_path / "batch.json.partial.json"
        payload = json.loads(diagnostic.read_text())
        assert payload["status"] == "incomplete"
        salvage = ClaimBatch.model_validate(payload["diagnostic_batch"])
        assert [(c.subject.label, c.value.year) for c in salvage.claims] == [
            ("Sebastian Löbig", 1862)
        ]
        with pytest.raises(ValidationError):
            ClaimBatch.model_validate_json(diagnostic.read_text())

        engine.responses = [
            entity_manifest_json(),
            ndjson([claim("e1", "Hildegard Löbig", 1876)]),
            ndjson([claim("e2", "Sebastian Löbig", 1862)]),
        ]
        assert cli.main(argv) == EXIT_OK
        complete = ClaimBatch.model_validate_json(out.read_text())
        assert {(c.subject.label, c.value.year) for c in complete.claims} == {
            ("Hildegard Löbig", 1876),
            ("Sebastian Löbig", 1862),
        }
        assert not diagnostic.exists()
        assert (tmp_path / "batch.json.stale").read_text() == self.EARLIER

    @staticmethod
    def extract(tmp_path, *argv: str, transcription: bytes | None = None):
        """`ingest extract` from a claims file, on a transcription of
        MEDIA (or on `transcription`'s bytes), with `argv` after it; its
        status and its batch's path."""
        from yizkr.pipeline.transcribe import Transcription

        path = tmp_path / "transcription.json"
        path.write_bytes(
            transcription
            or Transcription(artifact=MEDIA_REF, text="a page", engine="test")
            .model_dump_json()
            .encode()
        )
        out = tmp_path / "batch.json"
        status = cli.main(
            [
                *("ingest", "extract", "--transcription", str(path)),
                *("--claims-file", str(tmp_path / "claims.json")),
                *("--out", str(out)),
                *argv,
            ]
        )
        return status, out

    def test_an_extraction_failure_is_one_line_and_writes_no_batch(
        self, tmp_path, monkeypatch, capsys
    ):
        """An ExtractionFailed ends the step with its reason on one line
        and a failure's status, not a traceback, and no batch that would
        read as a claim-free document's (#301)."""
        from yizkr.pipeline import extract
        from yizkr.pipeline.chunking import ExtractionFailed

        def failed(self, transcription, artifact):
            raise ExtractionFailed(
                "no claims extracted: 2 of 2 chunks failed (c001: the\n"
                "engine stopped)"
            )

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", failed)
        status, out = self.extract(tmp_path)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            "extraction failed: no claims extracted: 2 of 2 chunks failed "
            f"(c001: the engine stopped); {out} not written\n"
        )
        assert captured.out == ""
        assert not out.exists()

    @staticmethod
    def fail_extraction(monkeypatch) -> None:
        from yizkr.pipeline import extract
        from yizkr.pipeline.chunking import ExtractionFailed

        def failed(self, transcription, artifact):
            raise ExtractionFailed("no claims extracted: 1 of 1 chunks failed")

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", failed)

    EARLIER = '{"claims": [], "note": "an earlier run"}'

    def test_a_failed_extraction_moves_an_earlier_batch_aside(
        self, tmp_path, monkeypatch, capsys
    ):
        """An earlier run's batch at --out is moved to the first free
        <out>.stale name, never over an older one, and the one line says
        where: no later step reads it as this run's (#639)."""
        self.fail_extraction(monkeypatch)
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)
        older = tmp_path / "batch.json.stale"
        older.write_text("an older stale batch")
        stale = tmp_path / "batch.json.stale.1"

        status, _ = self.extract(tmp_path)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            "extraction failed: no claims extracted: 1 of 1 chunks failed; "
            f"{out} not written; the earlier batch there moved to {stale}\n"
        )
        assert captured.out == ""
        assert not out.exists()
        assert stale.read_text() == self.EARLIER
        assert older.read_text() == "an older stale batch"

    def test_an_earlier_batch_that_cannot_be_moved_is_named(
        self, tmp_path, monkeypatch, capsys
    ):
        """A denied rename leaves the earlier batch intact and identified
        as stale, while extraction still reports its original failure."""
        self.fail_extraction(monkeypatch)
        from yizkr.pipeline.transcribe import Transcription

        transcription = tmp_path / "transcription.json"
        transcription.write_text(
            Transcription(
                artifact=MEDIA_REF, text="a page", engine="test"
            ).model_dump_json()
        )
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)
        replace = Path.replace

        def refuse_output_move(path, target):
            if path == out:
                raise PermissionError("fictional output is read-only")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", refuse_output_move)

        status = cli.main(
            [
                *("ingest", "extract", "--transcription", str(transcription)),
                *("--claims-file", str(tmp_path / "claims.json")),
                *("--out", str(out)),
            ]
        )

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            "extraction failed: no claims extracted: 1 of 1 chunks failed; "
            f"{out} not written; an earlier batch is still at {out} and "
            "could not be moved aside (fictional output is read-only): "
            "it is not this run's\n"
        )
        assert out.read_text() == self.EARLIER
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            ["transcription.json", out.name]
        )

    def test_any_other_exception_moves_an_earlier_batch_aside_too(
        self, tmp_path, monkeypatch, capsys
    ):
        """A bug's bare ValueError keeps its traceback, but the earlier
        batch is moved aside first and a line says where (#639, #650)."""
        from yizkr.pipeline import extract

        def broken(self, transcription, artifact):
            raise ValueError("an unexpected bug failed validation")

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", broken)
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)
        stale = tmp_path / "batch.json.stale"

        with pytest.raises(ValueError, match="failed validation"):
            self.extract(tmp_path)

        assert not out.exists()
        assert stale.read_text() == self.EARLIER
        assert capsys.readouterr().err == (
            f"{out} not written; the earlier batch there moved to {stale}\n"
        )

    @staticmethod
    def extract_single(
        tmp_path,
        monkeypatch,
        response: str,
        client=None,
        *,
        cache=None,
        strategy="single",
    ):
        """`ingest extract --engine llm --strategy single` on a
        transcription of MEDIA, its engine answering `response` (or
        `client` as its engine); its status and its batch's path."""
        from tests.unit.test_pipeline import FakeLlm
        from yizkr.pipeline import llm
        from yizkr.pipeline.transcribe import Transcription

        engine = client or FakeLlm(response)
        monkeypatch.setattr(llm, "make_client", lambda *args, **kw: engine)
        path = tmp_path / "transcription.json"
        path.write_text(
            Transcription(
                artifact=MEDIA_REF, text="a page", engine="test"
            ).model_dump_json()
        )
        out = tmp_path / "batch.json"
        status = cli.main(
            [
                *("ingest", "extract", "--transcription", str(path)),
                *("--engine", "llm", "--strategy", strategy),
                *("--out", str(out)),
                *(["--cache", str(cache)] if cache is not None else []),
            ]
        )
        return status, out

    @pytest.mark.parametrize("strategy", ["single", "auto"])
    @pytest.mark.parametrize(
        ("sentinel", "stop_reason"),
        [(False, "end_turn"), (True, "max_tokens")],
        ids=["missing-sentinel", "output-limit"],
    )
    def test_unfinished_single_answers_are_only_diagnostic(
        self, tmp_path, monkeypatch, capsys, strategy, sentinel, stop_reason
    ):
        from tests.unit.test_pipeline import CLAIMS, Stopping
        from yizkr.claims import ClaimBatch

        answer = json.dumps(CLAIMS[1])
        if sentinel:
            answer += '\n{"done": true}'
        engine = Stopping(answer, stop_reason)
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)

        status, _ = self.extract_single(
            tmp_path, monkeypatch, answer, client=engine, strategy=strategy
        )

        assert status == EXIT_FAILED
        assert not out.exists()
        assert (tmp_path / "batch.json.stale").read_text() == self.EARLIER
        assert (tmp_path / "batch.json.raw.txt").read_text() == answer
        diagnostic = json.loads(
            (tmp_path / "batch.json.partial.json").read_text()
        )
        assert diagnostic["status"] == "incomplete"
        salvage = ClaimBatch.model_validate(diagnostic["diagnostic_batch"])
        assert len(salvage.claims) == 1
        assert salvage.claims[0].source.artifact == MEDIA_REF
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        ("outcome", "expected", "cleanup_fails"),
        [
            (None, EXIT_OK, False),
            (OutputLimitReached(), EXIT_FAILED, False),
            (KeyboardInterrupt(), EXIT_INTERRUPTED, False),
            (
                UsageLimitReached("fictional quota", reason="usage"),
                EXIT_STOPPED,
                False,
            ),
            (OutputLimitReached(), EXIT_FAILED, True),
            (KeyboardInterrupt(), EXIT_INTERRUPTED, True),
            (
                UsageLimitReached("fictional quota", reason="usage"),
                EXIT_STOPPED,
                True,
            ),
        ],
        ids=[
            "complete",
            "failed",
            "interrupted",
            "paused",
            "failed-cleanup",
            "interrupted-cleanup",
            "paused-cleanup",
        ],
    )
    def test_the_invocation_closes_its_client_behind_the_cache(
        self, tmp_path, monkeypatch, capsys, outcome, expected, cleanup_fails
    ):
        from tests.unit.test_pipeline import CLAIMS, FakeLlm

        class OwnedClient(FakeLlm):
            closed = 0
            config_hash = "fictional-lifecycle-client"

            def complete(self, prompt, image_paths=()):
                if outcome is not None:
                    raise outcome
                return super().complete(prompt, image_paths)

            def close(self):
                self.closed += 1
                if cleanup_fails:
                    raise RuntimeError("fictional cleanup failure")

        answer = json.dumps(CLAIMS[1]) + '\n{"done": true}'
        engine = OwnedClient(answer)
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)

        status, _ = self.extract_single(
            tmp_path,
            monkeypatch,
            answer,
            client=engine,
            cache=tmp_path / "cache",
        )

        assert status == expected
        assert engine.closed == 1
        if cleanup_fails:
            assert "fictional cleanup failure" in capsys.readouterr().err
        if outcome is None:
            assert out.exists()
        else:
            assert not out.exists()
            assert (tmp_path / "batch.json.stale").read_text() == self.EARLIER

    def test_a_single_call_answer_without_claim_json_is_one_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """A single-call answer with no claim JSON ends the step in one
        line, with a failure's status and no batch, as a chunked
        extraction's failure does; an earlier batch is moved aside
        (#650)."""
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)
        stale = tmp_path / "batch.json.stale"

        status, _ = self.extract_single(
            tmp_path, monkeypatch, "no claims here, sorry"
        )

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            "extraction failed: no claim JSON in extractor response "
            "(neither a 'claims' object nor loose claim objects): "
            f"'no claims here, sorry'; {out} not written; the earlier "
            f"batch there moved to {stale}\n"
        )
        assert not out.exists()
        assert stale.read_text() == self.EARLIER

    def test_a_single_call_answer_of_invalid_claims_is_one_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """A single-call answer whose every claim fails validation ends
        the step in one line, with the count of its rejects and the file
        that holds their reasons (#650)."""
        from tests.unit.test_pipeline import CLAIMS

        answer = json.dumps(dict(CLAIMS[1], property="shoe_size"))
        status, out = self.extract_single(tmp_path, monkeypatch, answer)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        rejects = tmp_path / "batch.json.rejected.json"
        assert captured.err == (
            "extraction failed: every extracted claim failed validation "
            f"(1 rejects; reasons in {rejects}); {out} not written\n"
        )
        assert captured.out == ""
        assert not out.exists()
        (reject,) = json.loads(rejects.read_text())
        assert "shoe_size" in reject["problems"][0]

    def test_a_single_call_answer_without_claims_is_one_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """A single-call answer that holds no claims ends the step in one
        line that says so, with no count of rejects it has none of
        (#650)."""
        status, out = self.extract_single(
            tmp_path, monkeypatch, '{"claims": []}\n{"done": true}'
        )

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            f"extraction failed: the answer holds no claims; {out} not "
            "written\n"
        )
        assert not out.exists()

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (OutputLimitReached(), "cut off at max_tokens"),
            (
                ClassifierRefused(REFUSAL_STOP),
                "claude CLI: the safety classifier refused the call",
            ),
        ],
        ids=["output-limit", "classifier"],
    )
    def test_a_single_call_given_no_answer_is_one_line(
        self, tmp_path, monkeypatch, capsys, error, reason
    ):
        """A single call the engine gave no answer to ends the step in
        one line with the engine's reason (#650)."""
        from tests.unit.test_pipeline import FakeLlm

        class NoAnswer(FakeLlm):
            def complete(self, prompt, image_paths=()):
                raise error

        status, out = self.extract_single(
            tmp_path, monkeypatch, "", client=NoAnswer("")
        )

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err.startswith(
            f"extraction failed: the engine gave no answer: {reason}"
        )
        assert captured.err.endswith(f"; {out} not written\n")
        assert captured.err.count("\n") == 1
        assert not out.exists()

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (None, "cannot read the claims file {claims}: "),
            (b"not json at all", "the claims file {claims} is not JSON: "),
            (b"\xff\xfe", "the claims file {claims} is not JSON: "),
            (b"[1, 2]", "the claims file {claims} is not a JSON object"),
            (
                b'{"claims": [{"subject": "Person One"}]}',
                "claims file failed schema validation: ",
            ),
        ],
        ids=["missing", "not-json", "not-utf8", "not-object", "schema"],
    )
    def test_an_unusable_claims_file_is_one_line(
        self, tmp_path, capsys, content, reason
    ):
        """A claims file that is missing, not JSON or not a JSON object,
        or fails the claim schema, ends the step in one line, with a
        failure's status and no batch (#650)."""
        claims = tmp_path / "claims.json"
        if content is not None:
            claims.write_bytes(content)

        status, out = self.extract(tmp_path)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err.startswith(
            "extraction failed: " + reason.format(claims=claims)
        )
        assert captured.err.endswith(f"; {out} not written\n")
        assert captured.err.count("\n") == 1
        assert not out.exists()

    def test_a_claims_file_failing_the_manifest_is_one_line(
        self, tmp_path, capsys
    ):
        """A claims file whose claims fail manifest validation ends the
        step in one line, with a failure's status and no batch (#650)."""
        from tests.unit.test_pipeline import CLAIMS

        (tmp_path / "claims.json").write_text(
            json.dumps({"claims": [dict(CLAIMS[1], property="shoe_size")]})
        )

        status, out = self.extract(tmp_path)

        assert status == EXIT_FAILED
        captured = capsys.readouterr()
        _no_traceback(captured.err)
        assert captured.err == (
            "extraction failed: claims file failed manifest validation: "
            f"claims[0]: unknown property key 'shoe_size'; {out} not "
            "written\n"
        )
        assert not out.exists()

    def test_a_bug_in_the_single_call_extractor_keeps_its_traceback(
        self, tmp_path, monkeypatch, capsys
    ):
        """A bare ValueError raised inside the single-call extractor is a
        bug, not an expected outcome: it propagates, after the line on
        an earlier batch's fate (#650)."""
        from tests.unit.test_pipeline import CLAIMS
        from yizkr.pipeline import extract

        def broken(*args, **kwargs):
            raise ValueError("an unexpected bug")

        monkeypatch.setattr(extract, "cite_artifact", broken)
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)

        with pytest.raises(ValueError, match="unexpected bug") as raised:
            self.extract_single(
                tmp_path, monkeypatch, json.dumps({"claims": CLAIMS})
            )

        assert not isinstance(raised.value, extract.ExtractionFailed)
        assert capsys.readouterr().err == (
            f"{out} not written; the earlier batch there moved to "
            f"{tmp_path / 'batch.json.stale'}\n"
        )

    def test_a_successful_extraction_replaces_an_earlier_batch(
        self, tmp_path, capsys
    ):
        """Only a run that does not complete moves a batch aside: a
        complete one writes its own over it (#639)."""
        (tmp_path / "claims.json").write_text('{"claims": []}')
        out = tmp_path / "batch.json"
        out.write_text(self.EARLIER)

        status, _ = self.extract(tmp_path)

        assert status == EXIT_OK
        assert capsys.readouterr().out == f"wrote {out} (0 claims)\n"
        assert json.loads(out.read_text())["claims"] == []
        assert out.read_text() != self.EARLIER
        assert not list(tmp_path.glob("batch.json.stale*"))

    def test_a_folder_at_out_is_refused_before_the_engine(
        self, tmp_path, monkeypatch, capsys
    ):
        """A folder at --out is refused in one line, before extraction,
        and is never moved aside (#639)."""
        from yizkr.pipeline import extract

        def reached(self, transcription, artifact):
            raise AssertionError("the extractor was reached")

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", reached)
        out = tmp_path / "batch.json"
        out.mkdir()
        (out / "kept.txt").write_text("kept")

        status, _ = self.extract(tmp_path)

        assert status == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == f"--out {out} is a folder, not a batch file; refusing\n"
        assert (out / "kept.txt").read_text() == "kept"
        assert not list(tmp_path.glob("batch.json.stale*"))

    def test_a_claim_free_batch_says_why_it_is_empty(
        self, tmp_path, monkeypatch, capsys
    ):
        """A batch without claims is written, and the extractor's
        attention reasons follow it, one line each (#301)."""
        from yizkr.pipeline import extract

        reasons = [
            "entity manifest fell back to line-range segmentation: no JSON",
            "no claims extracted: 2 chunks answered with none",
        ]

        def empty(self, transcription, artifact):
            self.attention = reasons
            return _citing()

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", empty)
        status, out = self.extract(tmp_path)

        assert status == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == f"wrote {out} (0 claims)\n"
        assert captured.err == "".join(
            f"attention: {reason}\n" for reason in reasons
        )
        assert out.exists()

    def test_another_error_of_the_extractor_is_not_caught(
        self, tmp_path, monkeypatch
    ):
        """Only ExtractionFailed is reported in one line: a bare
        ValueError is the type a bug raises too, and keeps its traceback
        (#301)."""
        from yizkr.pipeline import extract

        def broken(self, transcription, artifact):
            raise ValueError("an unanticipated error")

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", broken)
        with pytest.raises(ValueError, match="an unanticipated error"):
            self.extract(tmp_path)
        assert not (tmp_path / "batch.json").exists()

    def test_an_empty_prepared_batch_reports_its_empty_result(
        self, tmp_path, capsys
    ):
        """An explicitly empty prepared file succeeds with an explanation,
        distinguishable from missing or unusable model output."""
        (tmp_path / "claims.json").write_text('{"claims": []}')
        status, out = self.extract(tmp_path)
        assert status == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == f"wrote {out} (0 claims)\n"
        assert "attention:" in captured.err
        assert "empty" in captured.err
        assert json.loads(out.read_text())["claims"] == []

    def test_a_batch_with_claims_prints_no_attention(
        self, tmp_path, monkeypatch, capsys
    ):
        from yizkr.pipeline import extract

        def fell_back(self, transcription, artifact):
            self.attention = ["segmentation fell back to line windows"]
            return _citing(MEDIA_SHA1)

        monkeypatch.setattr(extract.ClaimFileExtractor, "extract", fell_back)
        status, out = self.extract(tmp_path)

        assert status == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == f"wrote {out} (1 claims)\n"
        assert captured.err == ""

    def test_a_missing_transcription_is_refused_in_one_line(
        self, tmp_path, capsys
    ):
        missing = tmp_path / "no-such-transcription.json"
        status = cli.main(
            ["ingest", "extract", "--transcription", str(missing)]
        )
        assert status == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == (
            f"cannot read the transcription {missing}: No such file or "
            "directory; refusing\n"
        )

    def test_a_transcription_that_cannot_be_read_is_refused(
        self, tmp_path, capsys
    ):
        """A directory where the file should be: unreadable as root too."""
        folder = tmp_path / "transcription.json"
        folder.mkdir()
        status = cli.main(
            ["ingest", "extract", "--transcription", str(folder)]
        )
        assert status == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == (
            f"cannot read the transcription {folder}: Is a directory; "
            "refusing\n"
        )

    def test_a_transcription_that_is_not_utf8_is_refused(
        self, tmp_path, capsys
    ):
        status, out = self.extract(
            tmp_path, transcription="Flörburg".encode("latin-1")
        )
        assert status == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == (
            "transcription.json is not UTF-8 text, so not a transcription "
            "this step reads; refusing\n"
        )
        assert not out.exists()

    def test_missing_media_is_refused_in_one_line(self, tmp_path, capsys):
        missing = tmp_path / "example-marriage-certificate.png"
        status, out = self.extract(tmp_path, "--media", str(missing))
        assert status == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == (
            f"cannot read the media {missing}: No such file or directory; "
            "refusing\n"
        )
        assert not out.exists()


class TestIngestRun:
    @staticmethod
    def configure(tmp_path, monkeypatch) -> None:
        """An empty never-ingest list, and no corpus folder configured."""
        from yizkr.core.corpus import CORPUS_DIR_VAR, EXCLUDE_FILE_VAR

        listed = tmp_path / "exclude.txt"
        listed.write_text("")
        env_file = tmp_path / "test.env"
        env_file.write_text(f"{EXCLUDE_FILE_VAR}={listed}\n")
        monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
        monkeypatch.delenv(EXCLUDE_FILE_VAR, raising=False)
        monkeypatch.delenv(CORPUS_DIR_VAR, raising=False)

    def run(self, tmp_path, monkeypatch, step) -> int:
        """`ingest run` on an empty folder, its run stubbed by `step`."""
        self.configure(tmp_path, monkeypatch)
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        monkeypatch.setattr(Orchestrator, "run", lambda self: step())
        return cli.main(
            [
                *("ingest", "run", str(folder)),
                *("--state", str(tmp_path / "state"), "--no-cache"),
            ]
        )

    @pytest.mark.parametrize(
        "pause", PAUSES, ids=[type(p).__name__ for p in PAUSES]
    )
    def test_a_pause_stops_with_its_message_alone(
        self, tmp_path, monkeypatch, capsys, pause
    ):
        def step():
            raise pause

        assert self.run(tmp_path, monkeypatch, step) == EXIT_STOPPED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == f"{pause}\n"

    def test_other_settings_are_refused_with_their_message(
        self, tmp_path, monkeypatch, capsys
    ):
        conflict = RunSettingsConflict(
            tmp_path / "state",
            [("engine.cli_version", "2.0.9", "2.1.0")],
        )

        def step():
            raise conflict

        assert self.run(tmp_path, monkeypatch, step) == EXIT_REFUSED
        err = capsys.readouterr().err
        _no_traceback(err)
        assert err == f"{conflict}\n"

    def test_ctrl_c_is_interrupted(self, tmp_path, monkeypatch, capsys):
        def step():
            raise KeyboardInterrupt

        assert self.run(tmp_path, monkeypatch, step) == EXIT_INTERRUPTED
        assert capsys.readouterr().err == "interrupted\n"

    @pytest.mark.parametrize(
        "status", [EXIT_OK, EXIT_FAILED, EXIT_STOPPED], ids=str
    )
    def test_the_runs_own_status_is_its_exit_status(
        self, tmp_path, monkeypatch, status
    ):
        assert self.run(tmp_path, monkeypatch, lambda: status) == status

    def test_its_own_refusal_is_a_refusal(self, tmp_path, monkeypatch, capsys):
        self.configure(tmp_path, monkeypatch)
        missing = tmp_path / "no-such-folder"

        status = cli.main(
            ["ingest", "run", str(missing), "--state", str(tmp_path / "s")]
        )

        assert status == EXIT_REFUSED
        assert capsys.readouterr().err == f"{missing} is not a directory\n"

    def test_an_unanticipated_error_is_a_failure(self, tmp_path, monkeypatch):
        def step():
            raise RuntimeError("an unanticipated error")

        # Python prints its traceback and exits 1, EXIT_FAILED
        with pytest.raises(RuntimeError):
            self.run(tmp_path, monkeypatch, step)


class TestAnotherCommand:
    """main is the backstop of every command, not of `ingest run` alone."""

    def test_a_pause_from_a_single_step_stops(self, monkeypatch, capsys):
        pause = CacheInUse(Path("var/llm-cache"), {"pid": 4711})

        def cmd_extract(args):
            raise pause

        monkeypatch.setattr(cli, "cmd_extract", cmd_extract)
        status = cli.main(
            ["ingest", "extract", "--transcription", "page.json"]
        )
        assert status == EXIT_STOPPED
        assert capsys.readouterr().err == f"{pause}\n"
