"""Offline tests for `yizkr scratch import-transcriptions` (#104).

The archive is fictional and built here in the format the import reads:
an `ingest run` state directory, one folder per document with its
`state.json` and `transcription.json`, the transcription in the QID
format (`artifact_qid`) the older runs wrote, or in the current one
(`artifact.sha1`). The corpus and the engine are the scratch driver
tests' (tests/unit/test_scratch.py): fictional documents
(docs/EXAMPLES.md), and a fake engine behind the real client factory and
the real LLM cache, so an import keys its adoptions as a scratch run
keys its calls, and a scratch run over the same corpus and cache shows
what it reuses. Nothing calls a model or runs the claude CLI.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tests.unit.test_scratch import (  # noqa: F401
    CLI,
    MODEL,
    PHOTO,
    STONE,
    TEXT,
    Corpus,
    Driver,
    Fakes,
    events,
    fakes,
    small,
)
from yizkr import cli
from yizkr.core.corpus import ExclusionConfigError, ExclusionList
from yizkr.pipeline import (
    estimate,
    llm,
    llmcache,
    orchestrate,
    transcribe,
    transcription_check,
)
from yizkr.pipeline.import_transcriptions import (
    ANSWERED,
    DIFFERING,
    EXCLUDED,
    IMPORTED,
    LEFT_OUT,
    NO_CALL,
    NO_SHA1,
    NOT_IN_CORPUS,
    OTHER_PROMPT,
    REJECTED,
    REJECTED_BEFORE,
    SKIPPED,
    UNKNOWN_PROMPT,
    UNREADABLE,
    ImportRefused,
    archived_client,
    import_transcriptions,
)
from yizkr.pipeline.llmcache import call_key
from yizkr.pipeline.rundir import RunDir, cache_counts
from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT

# the client an older run transcribed with, as its transcriptions name it
ARCHIVED = "llm-vision:claude-code/1.0.44:claude-sonnet-4-20250514"
# a reading of the studio portrait's reverse, as the archive holds it:
# not the words the fake engine reads now
PORTRAIT = (
    "[mount] Photograph Brüsrich, Peiltal\n"
    "[reverse] Muckel / Cilli / Wastl / Rahel / Felix"
)
GRAVESTONE = "EUGEN LÖBIG\n1836 - 1909\nCÄCILIE LÖBIG\nGEB. TARLE\n1833 - 1902"
# what a vision engine answered when its PDF renderer was missing, in the
# engine's own voice: a tool failure, never a transcription
TOOL_FAILURE = (
    "I'll read the image file.\n"
    "The image could not be rendered: pdftoppm: command not found. "
    "Install poppler-utils and try again."
)


@pytest.fixture
def engine(request) -> Fakes:
    """The scratch tests' fake engine behind the real client factory
    (tests/unit/test_scratch.py), with a full model id and a claude CLI
    that the scratch run's preflight finds installed and logged in."""
    return request.getfixturevalue("fakes")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Archive:
    """A fictional archive: an `ingest run` state directory, a folder per
    document holding its state.json and transcription.json."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        folder: str,
        *,
        file: str,
        sha1: str | None,
        text: str,
        engine: str = ARCHIVED,
        qid: str | None = "Q23",
        current_format: bool = False,
        state_sha1: bool = True,
    ) -> Path:
        """A document's folder. The transcription names its artifact by
        `qid` (the QID format), or by `sha1` with `current_format`; its
        state records `sha1` unless `state_sha1` is false."""
        where = self.path / folder
        where.mkdir(parents=True, exist_ok=True)
        state = {
            "file": f"corpus/{file}",
            "title": Path(file).stem,
            "status": "awaiting claim review",
            "artifact_qid": qid,
            "error": None,
            "detail": {"language": {"code": "de", "source": "detected"}},
            "content_sha1": sha1 if state_sha1 else None,
        }
        (where / "state.json").write_text(json.dumps(state, indent=2))
        artifact = (
            {"artifact": {"kind": "artifact", "sha1": sha1}}
            if current_format
            else {"artifact_qid": qid}
        )
        transcription = artifact | {
            "text": text,
            "language": "de",
            "engine": engine,
        }
        (where / "transcription.json").write_text(
            json.dumps(transcription, indent=2)
        )
        return where


def an_archive(tmp_path: Path, corpus: Corpus, **readings: str) -> Archive:
    """An archive holding a vision transcription, in the QID format, of
    each of the corpus's files named: the studio portrait and the
    gravestone by default, read as PORTRAIT and GRAVESTONE."""
    archive = Archive(tmp_path / "archive")
    readings = readings or {PHOTO: PORTRAIT, STONE: GRAVESTONE}
    for name, text in readings.items():
        archive.add(
            Path(name).stem, file=name, sha1=corpus.sha1(name), text=text
        )
    return archive


def import_(tmp_path, archive, corpus, **options):
    """The import into the test's cache, which its scratch runs take."""
    options.setdefault("exclusions", ExclusionList())
    options.setdefault("log", lambda *a, **k: None)
    options.setdefault("cache_dir", tmp_path / "cache")
    return import_transcriptions(archive.path, corpus.folder, **options)


def outcome_of(report, sha1: str):
    (found,) = [o for o in report.outcomes if o.sha1 == sha1]
    return found


def cache_files(tmp_path: Path, cache: str = "cache") -> dict[str, str]:
    """Every file the cache holds but its lock, by name, with its text."""
    return {
        path.name: path.read_text()
        for path in sorted((tmp_path / cache).rglob("*.json"))
    }


def live(tmp_path: Path, cache: str = "cache") -> list[dict]:
    """The entries the cache serves."""
    return [
        json.loads(path.read_text())
        for path in sorted((tmp_path / cache).glob("*/*.json"))
        if len(path.stem) == 64
    ]


def rejected(tmp_path: Path) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted((tmp_path / "cache").glob("*/*.rejected-*.json"))
    ]


def transcribe_only(tmp_path, corpus, engine, run="run-1", **options) -> Path:
    """A new scratch run over the corpus, in the test's cache, that stops
    after transcription, as #144's dry run does; returns its directory."""
    engine.corpus = corpus
    engine.calls.clear()
    driver = Driver(
        corpus.folder,
        tmp_path / "runs" / run,
        until="transcription",
        **options,
    )
    assert driver.run() == 0
    return driver.state_dir


def view(run: Path, corpus: Corpus, name: str) -> dict:
    return json.loads(
        (run / Path(name).stem / "transcription.json").read_text()
    )


def hits(run: Path, reason: str) -> set[str]:
    return {
        e["subject"]
        for e in events(run, "cache-hit")
        if e["role"] == "transcribe" and e["reason"] == reason
    }


def misses(run: Path, reason: str) -> set[str]:
    return {
        e["subject"]
        for e in events(run, "cache-miss")
        if e["role"] == "transcribe" and e["reason"] == reason
    }


def read_by_the_engine(engine: Fakes) -> set[str]:
    return {name for what, name in engine.calls if what == "vision"}


# -- adoption, and the run that reuses it -------------------------------------


class TestAdoption:
    def test_the_run_reuses_each_adopted_reading_with_reason_adopted(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        report = import_(tmp_path, archive, corpus)
        assert report.counts() == {IMPORTED: 2}
        assert {o.detail for o in report.outcomes} == {"adopted"}
        run = transcribe_only(tmp_path, corpus, engine)
        # no scan went to the engine: both calls were answered by the
        # adoptions, each a hit whose reason is `adopted`
        assert read_by_the_engine(engine) == set()
        scans = {corpus.sha1(PHOTO), corpus.sha1(STONE)}
        assert hits(run, "adopted") == scans
        assert misses(run, "absent") == set()
        assert cache_counts(RunDir(run))["transcribe"] == {"hit: adopted": 2}
        # the views hold the archive's readings, name the artifact by its
        # SHA-1 and no QID, and name the client the archive names
        photo = view(run, corpus, PHOTO)
        assert photo["text"] == PORTRAIT
        assert photo["artifact"] == {
            "kind": "artifact",
            "sha1": corpus.sha1(PHOTO),
        }
        assert photo["engine"] == ARCHIVED
        assert "Q23" not in json.dumps(photo)
        assert view(run, corpus, STONE)["text"] == GRAVESTONE

    def test_the_adoption_keeps_its_provenance_and_no_qid(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus, **{PHOTO: PORTRAIT})
        report = import_(tmp_path, archive, corpus)
        (entry,) = live(tmp_path)
        assert entry["origin"] == "adopted"
        assert entry["response"] == PORTRAIT
        # the key's components are kept, and hash to the key
        assert call_key(entry["components"]) == entry["key"]
        assert entry["components"]["model"] == MODEL
        assert entry["components"]["effort"] == "low"
        provenance = entry["provenance"]
        assert provenance["backfilled"] is True
        assert (provenance["role"], provenance["call"]) == (
            "transcribe",
            "complete",
        )
        assert provenance["engine_field"] == ARCHIVED
        assert provenance["engine"] == "claude-code"
        assert provenance["cli_version"] == "1.0.44"
        assert provenance["model"] == "claude-sonnet-4-20250514"
        assert provenance["model_identifier"] == (
            "claude-code/1.0.44:claude-sonnet-4-20250514"
        )
        # the QID format was made with the prompt the run sends
        assert provenance["prompt_sha256"] == sha256(TRANSCRIPTION_PROMPT)
        # the archive gives no time, so none is claimed
        assert provenance["made_at"] is None
        assert provenance["media_sha1"] == corpus.sha1(PHOTO)
        assert provenance["archived_as"] == (
            f"{Path(PHOTO).stem}/transcription.json"
        )
        assert provenance["check"]["passed"] is True
        assert provenance["check"]["version"] == (
            transcription_check.CHECK_VERSION
        )
        assert "adopted_at" in provenance
        # never the current model or CLI: the archive's
        assert provenance["model"] != MODEL
        assert CLI not in json.dumps(provenance)
        # the QID the archive names the artifact by is kept nowhere
        (path,) = [
            p for p in (tmp_path / "cache").glob("*/*.json")
            if len(p.stem) == 64
        ]  # fmt: skip
        assert "Q23" not in path.read_text()
        assert "artifact_qid" not in path.read_text()
        assert report.keyed["client"] == f"claude-code/{CLI}:{MODEL}"
        assert report.keyed["effort"] == "low"

    def test_the_time_it_was_made_comes_from_the_archives_events(
        self, tmp_path, engine
    ):
        """An archive that is a run directory records when each
        transcription was made: the latest `done` event of its stage."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus, **{PHOTO: PORTRAIT})
        photo = corpus.sha1(PHOTO)
        logged = [
            ("2026-09-27T23:10:00+00:00", "done", "transcription", photo),
            ("2026-09-28T08:05:00+00:00", "done", "transcription", photo),
            ("2026-09-28T09:00:00+00:00", "reused", "transcription", photo),
            ("2026-09-28T09:01:00+00:00", "done", "extraction", photo),
            ("2026-09-28T09:02:00+00:00", "done", "transcription", "b" * 40),
        ]
        with (archive.path / "events.jsonl").open("w") as log:
            for at, event, stage, subject in logged:
                line = {"at": at, "event": event, "invocation": "abc"}
                line |= {"subject": subject, "kind": "media", "stage": stage}
                log.write(json.dumps(line) + "\n")
        import_(tmp_path, archive, corpus)
        (entry,) = live(tmp_path)
        assert entry["provenance"]["made_at"] == "2026-09-28T08:05:00+00:00"

    def test_an_adoption_stands_only_for_the_call_it_was_keyed_for(
        self, tmp_path, engine
    ):
        """Another effort gives the run's call another key, under which
        nothing is adopted: the engine transcribes the scan afresh, as it
        would after a claude CLI update, a model or a prompt change."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        import_(tmp_path, archive, corpus)
        run = transcribe_only(tmp_path, corpus, engine, extract_effort="high")
        assert read_by_the_engine(engine) == {PHOTO, STONE}
        assert hits(run, "adopted") == set()
        assert misses(run, "absent") == {
            corpus.sha1(PHOTO),
            corpus.sha1(STONE),
        }
        # the adoptions stay under the keys they were made for, and a run
        # that makes those calls is served them
        run = transcribe_only(tmp_path, corpus, engine, run="run-2")
        assert read_by_the_engine(engine) == set()
        assert hits(run, "adopted") == {corpus.sha1(PHOTO), corpus.sha1(STONE)}

    def test_running_it_again_changes_nothing(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = an_archive(
            tmp_path, corpus, **{PHOTO: PORTRAIT, STONE: TOOL_FAILURE}
        )
        import_(tmp_path, archive, corpus)
        before = cache_files(tmp_path)
        again = import_(tmp_path, archive, corpus)
        assert cache_files(tmp_path) == before
        assert outcome_of(again, corpus.sha1(PHOTO)).detail == (
            "adopted already"
        )
        assert outcome_of(again, corpus.sha1(STONE)).outcome == REJECTED
        assert len(rejected(tmp_path)) == 1

    def test_the_cache_keeps_the_answer_it_holds(self, tmp_path, engine):
        """An adoption never replaces an answer: the engine's, from a run
        made before the import, stands."""
        corpus = small(tmp_path)
        transcribe_only(tmp_path, corpus, engine)
        before = cache_files(tmp_path)
        report = import_(tmp_path, an_archive(tmp_path, corpus), corpus)
        assert cache_files(tmp_path) == before
        found = outcome_of(report, corpus.sha1(PHOTO))
        assert (found.outcome, found.skipped) == (SKIPPED, ANSWERED)
        assert "the engine's answer" in found.detail


# -- the check ----------------------------------------------------------------


class TestTheCheck:
    def test_a_tool_failure_is_rejected_and_the_run_transcribes_it_again(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = an_archive(
            tmp_path, corpus, **{PHOTO: PORTRAIT, STONE: TOOL_FAILURE}
        )
        report = import_(tmp_path, archive, corpus)
        assert report.counts() == {IMPORTED: 1, REJECTED: 1}
        stone = outcome_of(report, corpus.sha1(STONE))
        assert stone.outcome == REJECTED
        assert "tool-failure" in stone.detail
        assert "pdf-renderer" in stone.detail
        # recorded as a rejected answer under the call's key, never live
        (entry,) = rejected(tmp_path)
        assert entry["response"] == TOOL_FAILURE
        assert entry["rejected"]["reason"] == stone.detail
        assert entry["rejected"]["role"] == "transcribe"
        assert entry["provenance"]["backfilled"] is True
        assert entry["provenance"]["check"]["passed"] is False
        assert [e["response"] for e in live(tmp_path)] == [PORTRAIT]
        # the run's call misses as rejected, and the engine reads the scan
        run = transcribe_only(tmp_path, corpus, engine)
        assert read_by_the_engine(engine) == {STONE}
        assert misses(run, "rejected") == {corpus.sha1(STONE)}
        assert hits(run, "adopted") == {corpus.sha1(PHOTO)}
        assert view(run, corpus, STONE)["text"] == corpus.texts[STONE]

    def test_a_refusal_is_rejected(self, tmp_path, engine):
        corpus = small(tmp_path)
        refusal = "I'm sorry, but I can't transcribe this document."
        archive = an_archive(tmp_path, corpus, **{PHOTO: refusal})
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert found.outcome == REJECTED
        assert "(refusal)" in found.detail
        assert live(tmp_path) == []

    def test_a_reading_too_short_for_its_pages_is_rejected(
        self, tmp_path, engine, monkeypatch
    ):
        """The check is given the scan's page count, as the run's is: a
        reading of three pages that holds only editorial marks fails its
        length floor."""
        corpus = small(tmp_path)
        archive = an_archive(
            tmp_path, corpus, **{PHOTO: "[illegible]\n[illegible]"}
        )
        monkeypatch.setattr(
            transcription_check, "media_page_count", lambda path: 3
        )
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert found.outcome == REJECTED
        assert "(too-short)" in found.detail
        assert live(tmp_path) == []

    def test_a_stricter_check_reaches_an_adoption_in_the_run(
        self, tmp_path, engine, monkeypatch
    ):
        """A check made stricter after the import rejects the adoption when
        the run is served it, and the engine is asked; imported again, the
        reading is rejected as the check now fails it."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus, **{PHOTO: PORTRAIT})
        import_(tmp_path, archive, corpus)
        # a signature the archive's reading has, and the engine's has not
        signature = transcription_check._signature(
            "slashed-names", transcription_check.TOOL_FAILURE, r"Muckel /"
        )
        monkeypatch.setattr(
            transcription_check,
            "SIGNATURES",
            (*transcription_check.SIGNATURES, signature),
        )
        run = transcribe_only(tmp_path, corpus, engine)
        (reject,) = events(run, "cache-reject")
        assert reject["subject"] == corpus.sha1(PHOTO)
        assert "slashed-names" in reject["reason"]
        assert misses(run, "rejected") == {corpus.sha1(PHOTO)}
        assert read_by_the_engine(engine) == {PHOTO, STONE}
        assert view(run, corpus, PHOTO)["text"] == corpus.texts[PHOTO]
        again = import_(tmp_path, archive, corpus)
        assert [o.outcome for o in again.outcomes] == [REJECTED]


# -- leaving out, and the never-ingest list ---------------------------------


class TestLeavingOut:
    def test_a_left_out_transcription_is_not_adopted(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        prefix = corpus.sha1(PHOTO)[:10]
        report = import_(tmp_path, archive, corpus, leave_out=[prefix])
        assert report.counts() == {IMPORTED: 1, LEFT_OUT: 1}
        found = outcome_of(report, corpus.sha1(PHOTO))
        assert found.detail == f"--leave-out {prefix}"
        run = transcribe_only(tmp_path, corpus, engine)
        assert read_by_the_engine(engine) == {PHOTO}
        assert misses(run, "absent") == {corpus.sha1(PHOTO)}
        assert hits(run, "adopted") == {corpus.sha1(STONE)}

    def test_an_earlier_adoption_of_it_is_withdrawn(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        import_(tmp_path, archive, corpus)
        prefix = corpus.sha1(PHOTO)[:10].upper()
        report = import_(tmp_path, archive, corpus, leave_out=[prefix])
        found = outcome_of(report, corpus.sha1(PHOTO))
        assert found.outcome == LEFT_OUT
        assert found.detail.endswith("its earlier adoption withdrawn")
        assert outcome_of(report, corpus.sha1(STONE)).detail == (
            "adopted already"
        )
        (aside,) = rejected(tmp_path)
        assert aside["response"] == PORTRAIT
        assert "--leave-out" in aside["rejected"]["reason"]
        run = transcribe_only(tmp_path, corpus, engine)
        assert read_by_the_engine(engine) == {PHOTO}
        assert misses(run, "rejected") == {corpus.sha1(PHOTO)}

    def test_the_engines_answer_is_never_withdrawn(self, tmp_path, engine):
        corpus = small(tmp_path)
        transcribe_only(tmp_path, corpus, engine)
        before = cache_files(tmp_path)
        archive = an_archive(tmp_path, corpus, **{PHOTO: corpus.texts[PHOTO]})
        prefix = corpus.sha1(PHOTO)[:7]
        report = import_(tmp_path, archive, corpus, leave_out=[prefix])
        assert report.outcomes[0].detail == f"--leave-out {prefix}"
        assert cache_files(tmp_path) == before

    @pytest.mark.parametrize(
        "prefix, refusal",
        [
            ("0000000", "no transcription"),
            ("abc123", "7 to 40 hex digits"),
            ("not-a-sha1", "7 to 40 hex digits"),
        ],
    )
    def test_a_prefix_that_matches_nothing_refuses_the_import(
        self, tmp_path, engine, prefix, refusal
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        with pytest.raises(ImportRefused, match=refusal):
            import_(tmp_path, archive, corpus, leave_out=[prefix])
        assert not (tmp_path / "cache").exists()


class TestTheNeverIngestList:
    def test_an_excluded_transcription_is_never_checked_or_recorded(
        self, tmp_path, engine
    ):
        """The list names the gravestone by its SHA-1, and the portrait by
        a glob on the name its archived state records, which the corpus
        has since renamed: both are excluded, before the check could
        reject the gravestone's tool failure."""
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        archive.add(
            "portrait",
            file="loebig-portrait-draft.png",
            sha1=corpus.sha1(PHOTO),
            text=PORTRAIT,
        )
        archive.add(
            "gravestone",
            file=STONE,
            sha1=corpus.sha1(STONE),
            text=TOOL_FAILURE,
        )
        exclusions = ExclusionList.parse(
            f"{corpus.sha1(STONE)}\n*-portrait-draft.png\n"
        )
        report = import_(tmp_path, archive, corpus, exclusions=exclusions)
        assert report.counts() == {EXCLUDED: 2}
        assert {o.sha1 for o in report.outcomes} == {
            corpus.sha1(PHOTO),
            corpus.sha1(STONE),
        }
        assert rejected(tmp_path) == []
        assert live(tmp_path) == []

    def test_one_excluded_transcription_excludes_its_media(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        for folder, name in (("a", "old-name.png"), ("b", PHOTO)):
            archive.add(
                folder, file=name, sha1=corpus.sha1(PHOTO), text=PORTRAIT
            )
        report = import_(
            tmp_path,
            archive,
            corpus,
            exclusions=ExclusionList.parse("old-name.png\n"),
        )
        assert [(o.outcome, o.sha1) for o in report.outcomes] == [
            (EXCLUDED, corpus.sha1(PHOTO))
        ]
        assert live(tmp_path) == []

    def test_without_a_list_nothing_is_read(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        with pytest.raises(ExclusionConfigError, match="never-ingest list"):
            import_(tmp_path, archive, corpus, exclusions=None)
        assert not (tmp_path / "cache").exists()


# -- what is skipped ----------------------------------------------------------


class TestSkipped:
    def test_a_transcription_made_without_a_call_is_skipped(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        archive.add(
            "postcard",
            file=TEXT,
            sha1=corpus.sha1(TEXT),
            text=corpus.texts[TEXT],
            engine=f"text-file:{TEXT}",
        )
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert (found.outcome, found.skipped) == (SKIPPED, NO_CALL)
        assert found.detail == "route text-file"
        assert report.keyed is None
        assert cache_files(tmp_path) == {}

    def test_a_scan_the_corpus_does_not_hold_is_skipped(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        gone = hashlib.sha1(b"a scan no longer in the corpus").hexdigest()
        archive = Archive(tmp_path / "archive")
        archive.add("gone", file="gone.png", sha1=gone, text=PORTRAIT)
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert (found.outcome, found.skipped) == (SKIPPED, NOT_IN_CORPUS)

    def test_differing_transcriptions_of_one_media_are_not_adopted(
        self, tmp_path, engine
    ):
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        sha1 = corpus.sha1(PHOTO)
        archive.add("portrait", file=PHOTO, sha1=sha1, text=PORTRAIT)
        archive.add("portrait-copy", file=PHOTO, sha1=sha1, text=PORTRAIT)
        report = import_(tmp_path, archive, corpus)
        assert [o.outcome for o in report.outcomes] == [IMPORTED]
        archive.add(
            "portrait-again", file=PHOTO, sha1=sha1, text=PORTRAIT + " [?]"
        )
        other = tmp_path / "another-cache"
        report = import_(tmp_path, archive, corpus, cache_dir=other)
        (found,) = report.outcomes
        assert (found.outcome, found.skipped) == (SKIPPED, DIFFERING)
        assert "portrait-again" in found.detail
        assert live(tmp_path, "another-cache") == []

    def test_only_a_reading_made_with_the_runs_prompt_is_adopted(
        self, tmp_path, engine, monkeypatch
    ):
        """A transcription in the current format names no prompt, and one
        in the QID format was made with the prompt the run sends only
        while the run sends it."""
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        archive.add(
            "gravestone",
            file=STONE,
            sha1=corpus.sha1(STONE),
            text=GRAVESTONE,
            current_format=True,
        )
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert (found.outcome, found.skipped) == (SKIPPED, UNKNOWN_PROMPT)
        archive = an_archive(tmp_path, corpus, **{PHOTO: PORTRAIT})
        monkeypatch.setattr(
            transcribe,
            "TRANSCRIPTION_PROMPT",
            TRANSCRIPTION_PROMPT + "Mark each page's start.\n",
        )
        report = import_(tmp_path, archive, corpus)
        found = outcome_of(report, corpus.sha1(PHOTO))
        assert (found.outcome, found.skipped) == (SKIPPED, OTHER_PROMPT)
        assert live(tmp_path) == []

    def test_what_cannot_be_read_or_named_is_skipped(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        broken = archive.add(
            "broken", file=PHOTO, sha1=corpus.sha1(PHOTO), text=PORTRAIT
        )
        (broken / "transcription.json").write_text('{"text": "cut sho')
        archive.add("nameless", file=STONE, sha1=None, text=GRAVESTONE)
        mixed = archive.add(
            "mixed",
            file=STONE,
            sha1=corpus.sha1(STONE),
            text=GRAVESTONE,
            current_format=True,
        )
        state = json.loads((mixed / "state.json").read_text())
        state["content_sha1"] = corpus.sha1(PHOTO)
        (mixed / "state.json").write_text(json.dumps(state))
        # a folder a run set aside is passed over
        archive.add(
            ".portrait.superseded-1",
            file=PHOTO,
            sha1=corpus.sha1(PHOTO),
            text=TOOL_FAILURE,
        )
        report = import_(tmp_path, archive, corpus)
        assert report.transcriptions == 3
        skipped = {o.where: o.skipped for o in report.outcomes}
        assert skipped == {
            "broken": UNREADABLE,
            "nameless": NO_SHA1,
            "mixed": UNREADABLE,
        }
        assert cache_files(tmp_path) == {}

    def test_a_rejected_reading_is_not_adopted_again(self, tmp_path, engine):
        """A reading a caller rejected under its call's key, as the run's
        stricter check did, is not adopted, even by a check that passes
        it."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus, **{PHOTO: PORTRAIT})
        import_(tmp_path, archive, corpus)
        (entry,) = live(tmp_path)
        key = entry["key"]
        from yizkr.pipeline.llm import open_cache

        assert open_cache(tmp_path / "cache").reject(key, PORTRAIT, "test")
        report = import_(tmp_path, archive, corpus)
        (found,) = report.outcomes
        assert (found.outcome, found.skipped) == (SKIPPED, REJECTED_BEFORE)
        assert live(tmp_path) == []


# -- refusals -----------------------------------------------------------------


class TestRefusals:
    def test_no_model_or_an_alias_is_refused(
        self, tmp_path, engine, monkeypatch
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        monkeypatch.delenv("YIZKR_LLM_MODEL")
        with pytest.raises(ImportRefused, match="no model is configured"):
            import_(tmp_path, archive, corpus)
        monkeypatch.setenv("YIZKR_LLM_MODEL", "opus")
        with pytest.raises(ImportRefused, match="an alias"):
            import_(tmp_path, archive, corpus)
        assert not (tmp_path / "cache").exists()

    def test_an_engine_client_that_cannot_start_is_refused(
        self, tmp_path, engine, monkeypatch
    ):
        """The keys name the claude CLI's version, so a CLI that is not
        installed stops the import, in one line, before anything is
        written."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)

        def no_cli(*args, **kwargs):
            raise RuntimeError("the Claude Code CLI is not installed")

        monkeypatch.setattr(llm, "make_client", no_cli)
        with pytest.raises(ImportRefused, match="cannot start.*not installed"):
            import_(tmp_path, archive, corpus)
        assert live(tmp_path) == [] and rejected(tmp_path) == []

    def test_an_archive_that_is_no_folder_is_refused(self, tmp_path, engine):
        corpus = small(tmp_path)
        archive = Archive(tmp_path / "archive")
        archive.path.rmdir()
        archive.path.write_text("a tarball, say")
        with pytest.raises(ImportRefused, match="is not a folder"):
            import_(tmp_path, archive, corpus)

    def test_a_pdf_without_the_renderer_is_refused(
        self, tmp_path, engine, monkeypatch
    ):
        corpus = small(tmp_path)
        (corpus.folder / "report.pdf").write_bytes(b"%PDF-1.4 fictional")
        archive = Archive(tmp_path / "archive")
        archive.add(
            "report",
            file="report.pdf",
            sha1=corpus.sha1("report.pdf"),
            text=GRAVESTONE,
        )
        monkeypatch.setattr(shutil, "which", lambda tool: None)
        with pytest.raises(ImportRefused, match="poppler-utils"):
            import_(tmp_path, archive, corpus)
        assert live(tmp_path) == []


# -- the engine field ---------------------------------------------------------


@pytest.mark.parametrize(
    "engine, found",
    [
        (
            "llm-vision:claude-code/1.0.44:claude-sonnet-4-20250514",
            ("claude-code", "1.0.44", "claude-sonnet-4-20250514"),
        ),
        ("llm-vision:claude-code/1.0.44", ("claude-code", "1.0.44", None)),
        (
            "llm-vision:claude-sonnet-4-20250514",
            ("anthropic", None, "claude-sonnet-4-20250514"),
        ),
        ("llm-vision:some/other:client", (None, None, None)),
    ],
)
def test_the_engine_field_names_the_client_that_answered(engine, found):
    client = archived_client(engine)
    assert (client["engine"], client["cli_version"], client["model"]) == found
    assert client["model_identifier"] == engine.partition(":")[2]


# -- the command --------------------------------------------------------------


def cli_run(*argv: str) -> int:
    args = cli.build_parser().parse_args(list(argv))
    return args.func(args)


class TestTheCommand:
    def test_it_prints_what_it_did_and_the_counts(
        self, tmp_path, engine, monkeypatch, capsys
    ):
        corpus = small(tmp_path)
        archive = an_archive(
            tmp_path,
            corpus,
            **{PHOTO: PORTRAIT, STONE: TOOL_FAILURE, TEXT: "a letter"},
        )
        never = tmp_path / "never-ingest.txt"
        never.write_text(f"{corpus.sha1(TEXT)}\n")
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(never))
        monkeypatch.setenv("YIZKR_CORPUS_DIR", str(corpus.folder))
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))
        argv = ["scratch", "import-transcriptions", str(archive.path)]
        argv += ["--cache", str(tmp_path / "cache")]
        assert cli_run(*argv) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[-1] == (
            "imported 1, rejected 1, excluded 1, left out 0, skipped 0"
        )
        assert f"{corpus.sha1(STONE)[:10]}  rejected" in out
        assert f"claude-code/{CLI}:{MODEL}, effort low" in out
        # the same command with the portrait left out
        leave = ["--leave-out", corpus.sha1(PHOTO)[:8]]
        assert cli_run(*argv, *leave) == 0
        last = capsys.readouterr().out.splitlines()[-1]
        assert last == (
            "imported 0, rejected 1, excluded 1, left out 1, skipped 0"
        )

    def test_it_refuses_without_a_never_ingest_list(
        self, tmp_path, engine, monkeypatch
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        monkeypatch.delenv("YIZKR_EXCLUDE_FILE", raising=False)
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))
        with pytest.raises(SystemExit, match="never-ingest list"):
            cli_run(
                "scratch",
                "import-transcriptions",
                str(archive.path),
                "--corpus",
                str(corpus.folder),
                "--cache",
                str(tmp_path / "cache"),
            )
        assert not (tmp_path / "cache").exists()

    def test_a_refusal_is_one_line_and_exit_1(
        self, tmp_path, engine, monkeypatch, capsys
    ):
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        never = tmp_path / "never-ingest.txt"
        never.write_text("")
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(never))
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))
        argv = ["scratch", "import-transcriptions", str(archive.path)]
        argv += ["--corpus", str(corpus.folder)]
        argv += ["--cache", str(tmp_path / "cache"), "--leave-out", "f" * 9]
        assert cli_run(*argv) == 1
        assert "no transcription" in capsys.readouterr().err

    def test_a_cache_another_process_holds_stops_it_in_one_line(
        self, tmp_path, engine, monkeypatch, capsys
    ):
        """One process at a time uses a cache: while a scratch run holds
        it, the import stops as the run's pause does, with its reason."""
        corpus = small(tmp_path)
        archive = an_archive(tmp_path, corpus)
        never = tmp_path / "never-ingest.txt"
        never.write_text("")
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(never))
        monkeypatch.setenv("YIZKR_ENV_FILE", str(tmp_path / "no.env"))

        def held(self, role, events=None):
            raise llmcache.CacheInUse(tmp_path / "cache", None)

        monkeypatch.setattr(orchestrate.Orchestrator, "role_client", held)
        argv = ["scratch", "import-transcriptions", str(archive.path)]
        argv += ["--corpus", str(corpus.folder)]
        argv += ["--cache", str(tmp_path / "cache")]
        assert cli_run(*argv) == 1
        assert "one process at a time uses a cache" in capsys.readouterr().err


# -- the call estimate --------------------------------------------------------


class TestTheEstimate:
    def test_the_estimate_counts_an_adoption_as_the_caches_answer(
        self, tmp_path, engine, monkeypatch
    ):
        """`yizkr scratch estimate` (#101) sees each adoption as the run
        does: a hit whose reason is `adopted`, and no engine call. The
        reading the import rejected is an engine call, a miss whose reason
        is `rejected`; the run then asks the engine as often as counted."""
        corpus = small(tmp_path)
        archive = an_archive(
            tmp_path, corpus, **{PHOTO: PORTRAIT, STONE: TOOL_FAILURE}
        )
        import_(tmp_path, archive, corpus)
        seen = []
        record = estimate._Tally.record

        def spy(self, event, **fields):
            seen.append((event, fields.get("role"), fields.get("reason")))
            return record(self, event, **fields)

        monkeypatch.setattr(estimate._Tally, "record", spy)
        engine.corpus = corpus
        before = cache_files(tmp_path)
        counted = estimate.scratch_estimate(
            corpus.folder,
            runs=tmp_path / "runs",
            until="transcription",
            cache_dir=tmp_path / "cache",
            exclusions=ExclusionList(),
            log=lambda *a, **k: None,
            min_free=0,
        )
        assert cache_files(tmp_path) == before
        stage = counted.stages["transcription"]
        assert (stage.hits, stage.known, stage.estimated) == (1, 1, 0)
        assert sorted(seen) == [
            ("cache-hit", "transcribe", "adopted"),
            ("cache-miss", "transcribe", "rejected"),
        ]
        run = transcribe_only(tmp_path, corpus, engine)
        assert read_by_the_engine(engine) == {STONE}
        assert hits(run, "adopted") == {corpus.sha1(PHOTO)}
