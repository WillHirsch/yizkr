"""Document discovery and persisted state through scratch processing."""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from pathlib import Path

import pytest

from tests.fixtures.document_processing import (
    ScriptedDriver,
    collect,
    completed_run,
    make_run,
    quiet,
    read_only,
    sha1_of,
    snapshot,
    steps,
    this_run,
    unreadable,
)
from tests.unit.test_run_provenance import fixed_capture  # noqa: F401
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import monitor
from yizkr.pipeline.document_state import (
    BATCHED,
    DUPLICATE,
    FAILED,
    DocumentRun,
    DocumentState,
    directory_names,
)
from yizkr.pipeline.scratch import ScratchDriver

pytestmark = pytest.mark.usefixtures("fixed_capture")

LETTERS = {
    "a-letter.txt": "Rahel was born in 1871.",
    "b-letter.txt": "Felix was born in 1867.",
    "c-letter.txt": "Klara was born in 1917.",
}


def letters(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    for name, text in LETTERS.items():
        (folder / name).write_text(text)
    return folder


def by_name(orchestrator) -> dict[str, DocumentRun]:
    return {doc.source.name: doc for doc in orchestrator.documents()}


class TestUnreadableDocuments:
    """A document whose source or state.json cannot be read fails alone
    (#215, #237): a `document-failed` event names it and says why, as its
    state.json does where it has a directory, and the run goes on with the
    others. Every document that fails has that event. state.json is
    replaced in one step, and one that cannot be read is left as it is."""

    def test_an_unreadable_source_fails_only_its_document(self, tmp_path):
        folder = letters(tmp_path)
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        with unreadable(folder / "b-letter.txt"):
            assert orchestrator.run() == 1
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "b-letter.txt"
        assert event["kind"] == "text"
        assert "subject" not in event  # its bytes were never read
        assert event["reason"].startswith(
            "its source cannot be read (PermissionError"
        )
        # never registered, transcribed or given a directory
        assert not any(c.endswith("b-letter.txt") for c in orchestrator.calls)
        assert not (tmp_path / "state" / "b-letter").exists()
        docs = by_name(orchestrator)
        assert docs["a-letter.txt"].state.status == BATCHED
        assert docs["c-letter.txt"].state.status == BATCHED
        # it is not planned; the run's events open and close around it
        manifest = orchestrator.rundir.manifest()
        assert [d["file"] for d in manifest["documents"]] == [
            "a-letter.txt",
            "c-letter.txt",
        ]
        events = orchestrator.rundir.events()
        assert events[0]["event"] == "run-start"
        assert events[-1]["event"] == "run-end"
        assert events[-1]["statuses"] == {FAILED: 1, BATCHED: 2}

    def test_an_unreadable_source_says_why_in_its_state(self, tmp_path):
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        files = snapshot(doc.dir)
        with unreadable(doc.source):
            assert orchestrator.run() == 1
        state = json.loads(doc.state_path.read_text())
        assert state["status"] == FAILED
        assert state["error"].startswith(
            "its source cannot be read (PermissionError"
        )
        # what it recorded stays, and nothing else in its directory moved
        assert (state["artifact_qid"], state["content_sha1"]) == (
            None,
            doc.sha1,
        )
        assert snapshot(doc.dir) == files
        (event,) = this_run(orchestrator, "document-failed")
        assert (event["file"], event["reason"]) == (
            "letter.txt",
            state["error"],
        )
        # readable again, it carries on where it stood
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert (doc.state.status, doc.state.error) == (
            BATCHED,
            None,
        )
        assert orchestrator.calls[calls:] == []
        assert this_run(orchestrator, "set-aside", "document-failed") == []

    def test_a_source_the_list_cannot_check_is_never_ingested(self, tmp_path):
        """A never-ingest list of hashes reads every file's bytes: one
        whose bytes cannot be read cannot be checked, so it fails, and it
        is never ingested."""
        folder = letters(tmp_path)
        listed = ExclusionList.parse(sha1_of("Ida was born in 1881.") + "\n")
        orchestrator = ScriptedDriver(
            folder, tmp_path / "state", log=quiet, exclusions=listed
        )
        with unreadable(folder / "b-letter.txt"):
            assert orchestrator.run() == 1
        assert not any(c.endswith("b-letter.txt") for c in orchestrator.calls)
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "b-letter.txt"
        assert "PermissionError" in event["reason"]
        assert this_run(orchestrator, "run-end")[0]["statuses"] == {
            FAILED: 1,
            BATCHED: 2,
        }

    def test_a_source_removed_mid_run_fails_only_its_document(self, tmp_path):
        folder = letters(tmp_path)

        class RemovesTheNext(ScriptedDriver):
            def _step_critique(self, doc, batch):
                if doc.source.name == "a-letter.txt":
                    (folder / "b-letter.txt").unlink()
                return super()._step_critique(doc, batch)

        orchestrator = RemovesTheNext(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 1
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "b-letter.txt"
        assert event["subject"] == sha1_of(LETTERS["b-letter.txt"])
        assert "FileNotFoundError" in event["reason"]
        (stage,) = this_run(orchestrator, "failed")
        assert (stage["stage"], stage["subject"]) == (
            "transcription",
            event["subject"],
        )
        statuses = {
            d.source.name: d.state.status
            for d in by_name(orchestrator).values()
        }
        assert statuses == {
            "a-letter.txt": BATCHED,
            "c-letter.txt": BATCHED,
        }

    @pytest.mark.parametrize(
        "damage",
        [
            "cut short",
            "no object",
            "a foreign field",
            "a field of another type",
            "no permission",
            "deeply nested",
        ],
    )
    def test_an_unreadable_state_json_fails_only_its_document(
        self, tmp_path, damage
    ):
        folder = letters(tmp_path)
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 0
        docs = by_name(orchestrator)
        doc = docs["b-letter.txt"]
        text = doc.state_path.read_text()
        doc.state_path.write_text(
            {
                "cut short": text[: len(text) // 2],
                "no object": "[]",
                "a foreign field": json.dumps(
                    json.loads(text) | {"colour": "blue"}
                ),
                "a field of another type": json.dumps(
                    json.loads(text) | {"status": 5}
                ),
                "no permission": text,
                # json.loads raises RecursionError, not ValueError, past
                # Python's recursion limit (#285)
                "deeply nested": "[" * 100_000,
            }[damage]
        )
        before = snapshot(doc.dir, skip=())
        calls = len(orchestrator.calls)
        with (
            unreadable(doc.state_path)
            if damage == "no permission"
            else contextlib.nullcontext()
        ):
            assert orchestrator.run() == 1
        # left as it is: nothing in its directory is written or moved
        assert snapshot(doc.dir, skip=()) == before
        (event,) = this_run(orchestrator, "document-failed")
        assert (event["file"], event["subject"]) == ("b-letter.txt", doc.sha1)
        assert event["reason"].startswith("its state.json cannot be read (")
        assert "remove it and the document starts afresh" in event["reason"]
        assert not any(
            c.endswith("b-letter.txt") for c in orchestrator.calls[calls:]
        )
        assert this_run(orchestrator, "set-aside") == []
        # the others ran on, and reused their views
        reused = Counter(
            e["subject"] for e in this_run(orchestrator, "reused")
        )
        assert reused == {
            docs["a-letter.txt"].sha1: 3,
            docs["c-letter.txt"].sha1: 3,
        }
        # removed, it starts the document afresh, its directory kept aside
        doc.state_path.unlink()
        assert orchestrator.run() == 0
        (aside,) = this_run(orchestrator, "set-aside")
        assert (aside["file"], aside["kept"]) == (
            "b-letter.txt",
            ".b-letter.superseded-1",
        )
        assert by_name(orchestrator)["b-letter.txt"].state.status == (BATCHED)

    def test_a_save_cut_short_leaves_the_state_it_replaces_whole(
        self, tmp_path, monkeypatch
    ):
        """state.json is replaced in one step, as the views are: a save cut
        short, by a kill say, leaves the old state whole and no other file
        beside it, where a plain write leaves it cut short."""
        from yizkr.pipeline import stages

        path = tmp_path / "letter" / "state.json"
        DocumentState(file="letter.txt", title="letter", status=BATCHED).save(
            path
        )
        before = path.read_bytes()

        def cut_short(target, text):
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text[: len(text) // 2])
            raise OSError("killed while writing")

        monkeypatch.setattr(stages, "write_synced", cut_short)
        failed = DocumentState(
            file="letter.txt", title="letter", status=FAILED
        )
        with pytest.raises(OSError, match="killed while writing"):
            failed.save(path)
        assert path.read_bytes() == before
        assert [p.name for p in path.parent.iterdir()] == ["state.json"]
        assert DocumentState.load(path).status == BATCHED

    def test_a_save_replaces_a_state_that_is_not_text(self, tmp_path):
        """A save compares the state with the one saved, to leave an
        unchanged one untouched; bytes that are not text are replaced."""
        path = tmp_path / "letter" / "state.json"
        path.parent.mkdir()
        path.write_bytes(b"\xff\xfe\x00 not text")
        DocumentState(file="letter.txt", title="letter", status=BATCHED).save(
            path
        )
        assert DocumentState.load(path).status == BATCHED

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("file", 5),
            ("title", None),
            ("status", 5),
            ("artifact_qid", 5),
            ("error", ["a reason"]),
            ("detail", []),
            ("content_sha1", 5),
        ],
    )
    def test_a_state_with_a_field_of_another_type_is_refused(
        self, tmp_path, field, value
    ):
        state = {
            "file": "/docs/letter.txt",
            "title": "letter",
            "status": BATCHED,
            "artifact_qid": "Q23",
            "error": None,
            "detail": {},
            "content_sha1": "0" * 40,
        }
        path = tmp_path / "state.json"
        path.write_text(json.dumps(state))
        assert DocumentState.load(path).status == BATCHED
        path.write_text(json.dumps(state | {field: value}))
        with pytest.raises(ValueError, match="a field of another type"):
            DocumentState.load(path)

    @pytest.mark.parametrize("text", ["null", '"letter"', "5", "[]"])
    def test_a_state_that_holds_no_object_is_refused(self, tmp_path, text):
        path = tmp_path / "state.json"
        path.write_text(text)
        with pytest.raises(ValueError, match="holds no object"):
            DocumentState.load(path)
        # the oldest, smallest state a document has still loads
        path.write_text(json.dumps({"file": "/docs/a.txt", "title": "a"}))
        assert DocumentState.load(path).status == "pending"

    def test_an_unreadable_source_whose_directory_cannot_be_written(
        self, tmp_path
    ):
        """Its failure cannot be saved into its state.json: the reason in
        the event says so too, and the run goes on with the others."""
        folder = letters(tmp_path)
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 0
        doc = by_name(orchestrator)["b-letter.txt"]
        before = snapshot(doc.dir, skip=())
        with read_only(doc.dir), unreadable(doc.source):
            assert orchestrator.run() == 1
        assert snapshot(doc.dir, skip=()) == before
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "b-letter.txt"
        assert event["reason"].startswith(
            "its source cannot be read (PermissionError"
        )
        assert (
            "; its state could not be saved (PermissionError"
            in (event["reason"])
        )
        reused = {e["subject"] for e in this_run(orchestrator, "reused")}
        assert reused == {
            sha1_of(LETTERS["a-letter.txt"]),
            sha1_of(LETTERS["c-letter.txt"]),
        }
        assert this_run(orchestrator, "run-end")[0]["outcome"] == "finished"

    def test_a_failed_check_against_the_list_is_never_ingested(
        self, tmp_path, monkeypatch
    ):
        """The never-ingest check fails closed: a source whose check fails
        (a read error while the list's hashes are compared) fails, and is
        never ingested, even though it can be read afterwards."""
        import yizkr.pipeline.document_state as scratch

        folder = letters(tmp_path)
        check = scratch.is_excluded

        def failing(exclusions, path=None, **kwargs):
            if path is not None and path.name == "b-letter.txt":
                raise OSError("a read error")
            return check(exclusions, path, **kwargs)

        monkeypatch.setattr(scratch, "is_excluded", failing)
        listed = ExclusionList.parse(sha1_of("Ida was born in 1881.") + "\n")
        orchestrator = ScriptedDriver(
            folder, tmp_path / "state", log=quiet, exclusions=listed
        )
        assert orchestrator.run() == 1
        assert not any(c.endswith("b-letter.txt") for c in orchestrator.calls)
        assert not (tmp_path / "state" / "b-letter").exists()
        (event,) = this_run(orchestrator, "document-failed")
        assert (event["file"], event["reason"]) == (
            "b-letter.txt",
            "its source cannot be read (OSError: a read error)",
        )
        assert "subject" not in event
        manifest = orchestrator.rundir.manifest()
        assert "b-letter.txt" not in [d["file"] for d in manifest["documents"]]

    def test_a_directory_that_cannot_be_written_whose_views_stand(
        self, tmp_path
    ):
        """A document whose views all stand has no state to save, so a
        directory it cannot write to changes nothing: it is advanced as
        before. One whose state must change there, and cannot be saved,
        fails alone, and the run goes on."""
        folder = letters(tmp_path)
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 0
        doc = by_name(orchestrator)["b-letter.txt"]
        before = snapshot(doc.dir, skip=())
        with read_only(doc.dir):
            assert orchestrator.run() == 0
        assert snapshot(doc.dir, skip=()) == before
        assert this_run(orchestrator, "document-failed") == []
        reused = Counter(
            e["subject"] for e in this_run(orchestrator, "reused")
        )
        assert reused[doc.sha1] == 3
        # a status that must change, in a directory that cannot take it
        state = json.loads(doc.state_path.read_text())
        doc.state_path.write_text(json.dumps(state | {"status": "pending"}))
        before = snapshot(doc.dir, skip=())
        with read_only(doc.dir):
            assert orchestrator.run() == 1
        assert snapshot(doc.dir, skip=()) == before
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "b-letter.txt"
        assert event["reason"].startswith(
            "its state could not be saved (PermissionError"
        )
        assert this_run(orchestrator, "run-end")[0]["statuses"] == {
            BATCHED: 2,
            FAILED: 1,
        }

    def test_a_first_record_failure_is_an_event(self, tmp_path, monkeypatch):
        import yizkr.pipeline.document_state as scratch

        def refuse(self):
            raise OSError("the disk refused")

        orchestrator = make_run(tmp_path)
        monkeypatch.setattr(scratch.Derivations, "begin", refuse)
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        (event,) = this_run(orchestrator, "document-failed")
        assert {
            k: event[k] for k in ("file", "subject", "kind", "reason")
        } == {
            "file": "letter.txt",
            "subject": doc.sha1,
            "kind": "text",
            "reason": "OSError: the disk refused",
        }
        assert this_run(orchestrator, "failed") == []  # no stage failed

    def test_a_failed_stage_fails_its_document_with_an_event(self, tmp_path):
        """A stage that fails has its own `failed` event, and its document
        the `document-failed` event every failed document has."""
        orchestrator = make_run(tmp_path, fail_step="critique")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        (stage,) = this_run(orchestrator, "failed")
        (event,) = this_run(orchestrator, "document-failed")
        assert stage["stage"] == "critique"
        assert event["subject"] == stage["subject"] == doc.sha1
        assert event["reason"] == stage["reason"] == doc.state.error


class TestStateDirectories:
    """Each source file has a directory of its own (#216): named after its
    stem when no other source in the folder shares the stem, after its
    whole file name when one does, and kept by the source it records when
    sources join or leave the folder."""

    def folder(self, tmp_path, **files: str) -> Path:
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        for name, text in files.items():
            (folder / name.replace("_", ".")).write_text(text)
        return folder

    def dirs(self, orchestrator) -> dict[str, str]:
        return {
            name: doc.dir.name for name, doc in by_name(orchestrator).items()
        }

    def test_same_stem_sources_across_two_runs(self, tmp_path):
        folder = self.folder(
            tmp_path,
            letter_md="Felix was born in 1867.",
            letter_txt="Rahel was born in 1871.",
            note_txt="Klara was born in 1917.",
        )
        first = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert first.run() == 0
        assert self.dirs(first) == {
            "letter.md": "letter.md",
            "letter.txt": "letter.txt",
            "note.txt": "note",  # no clash: its stem's name, as before
        }
        docs = by_name(first)
        for name, person in (("letter.md", "Felix"), ("letter.txt", "Rahel")):
            batch = ClaimBatch.model_validate_json(
                docs[name].batch.read_text()
            )
            assert batch.entities[0].label == person
            assert docs[name].state.content_sha1 == docs[name].sha1
        # the second run reuses both, and sets nothing aside
        again = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert again.run() == 0
        assert again.calls == []
        reused = Counter(e["subject"] for e in this_run(again, "reused"))
        assert reused == {doc.sha1: 3 for doc in docs.values()}
        assert this_run(again, "set-aside") == []
        assert not any(
            path.name.startswith(".") for path in again.state_dir.iterdir()
        )
        # a changed source sets aside its own directory, and no other
        (folder / "letter.md").write_text("Gustav was born in 1919.")
        assert again.run() == 0
        (aside,) = this_run(again, "set-aside")
        assert (aside["file"], aside["kept"]) == (
            "letter.md",
            ".letter.md.superseded-1",
        )
        assert [
            call for call in again.calls if call.startswith("manifest")
        ] == ["manifest:letter.md"]

    def test_a_source_that_joins_or_leaves_moves_no_other(self, tmp_path):
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {"letter.txt": "letter"}
        folder = orchestrator.folder
        # one joins: the recorded directory stays its source's
        (folder / "letter.md").write_text("Felix was born in 1867.")
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {
            "letter.md": "letter.md",
            "letter.txt": "letter",
        }
        assert {c.split(":")[1] for c in orchestrator.calls[calls:]} == {
            "letter.md"
        }
        assert this_run(orchestrator, "set-aside") == []
        # one leaves: the other keeps its directory
        (folder / "letter.txt").unlink()
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {"letter.md": "letter.md"}
        assert orchestrator.calls[calls:] == []
        assert this_run(orchestrator, "set-aside") == []
        # and it finds its own directory when it is back
        (folder / "letter.txt").write_text("Rahel was born in 1871.")
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {
            "letter.md": "letter.md",
            "letter.txt": "letter",
        }
        assert orchestrator.calls[calls:] == []
        assert this_run(orchestrator, "set-aside") == []

    def test_every_source_has_a_name_of_its_own(self, tmp_path):
        """Stems that differ only in case, stems and names whose slugs
        meet, and a stem that names a run file: each source still has a
        directory of its own, however the filesystem treats case, and
        none takes a run file's name or a set-aside one's."""
        sources = [
            Path(name)
            for name in sorted(
                [
                    "Letter.txt",
                    "letter.txt",
                    "letter.md",
                    "a b.txt",
                    "a-b.txt",
                    "!!!.txt",
                    "---.txt",
                    "run.json.txt",
                    "status.txt",
                    "note.txt",
                ]
            )
        ]
        names = directory_names(sources, tmp_path / "state")
        folded = [name.casefold() for name in names.values()]
        assert len(set(folded)) == len(sources)
        assert not set(folded) & {
            "run.json",
            "events.jsonl",
            "alerts.jsonl",
            "control.jsonl",
            "status",  # the live monitor's page directory (#195)
        }
        assert not any(name.startswith(".") for name in names.values())
        assert names[Path("note.txt")] == "note"
        assert names[Path("run.json.txt")] == "run.json.txt"
        assert names[Path("status.txt")] == "status.txt"
        assert names[Path("letter.md")] == "letter.md"
        # the same sources are named the same in every run
        assert directory_names(sources, tmp_path / "state") == names

    @pytest.mark.parametrize("name", [str.lower, str.upper])
    def test_no_directory_takes_the_monitor_s_page_directory(
        self, tmp_path, name
    ):
        """The run monitor writes its status page into STATUS_DIR in the
        run directory, which is the driver's state directory (#195): a
        source whose stem is that name, in any case, takes another."""
        source = Path(f"{name(monitor.STATUS_DIR)}.txt")
        names = directory_names([source], tmp_path / "state")
        assert names[source].casefold() != monitor.STATUS_DIR.casefold()

    def test_long_names_stay_within_the_filesystems_limit(self, tmp_path):
        """Two sources whose stems and whole names clash, both at the
        filesystem's 255-byte limit for a name: the hashed name keeps the
        start of the whole name, so each directory can be made, and the run
        goes through every document."""
        stem = "x" * 245
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / f"{stem} y.txt").write_text("Rahel was born in 1871.")
        (folder / f"{stem}-y.txt").write_text("Felix was born in 1867.")
        (folder / "zz-note.txt").write_text("Klara was born in 1917.")
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 0
        dirs = self.dirs(orchestrator)
        assert sorted(len(name.encode()) for name in dirs.values()) == [
            len("zz-note"),
            200 + 1 + 8,  # the start of the whole name, and a hash
            251,  # the whole name
        ]
        assert this_run(orchestrator, "document-failed") == []
        assert {
            doc.state.status for doc in by_name(orchestrator).values()
        } == {BATCHED}

    def test_a_damaged_state_json_is_reported_where_stems_are_shared(
        self, tmp_path
    ):
        """A state.json cut short in a stem directory whose stem another
        source shares still fails its document, with the reason: its
        source is not given a fresh directory that hides the damage
        (#237). Repaired, the directory is the source's again."""
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 0
        (orchestrator.folder / "letter.md").write_text(
            "Felix was born in 1867."
        )
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {
            "letter.md": "letter.md",
            "letter.txt": "letter",
        }
        state = orchestrator.state_dir / "letter" / "state.json"
        good = state.read_text()
        state.write_text(good[: len(good) // 2])
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 1
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "letter.txt"
        assert event["reason"].startswith("its state.json cannot be read (")
        assert "repair it, or remove it" in event["reason"]
        assert orchestrator.calls[calls:] == []
        assert state.read_text() == good[: len(good) // 2]
        assert not (orchestrator.state_dir / "letter.txt").exists()
        state.write_text(good)
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator)["letter.txt"] == "letter"
        assert orchestrator.calls[calls:] == []

    def test_damage_is_reported_against_the_document_it_names(self, tmp_path):
        """A state.json cut short while, in the same run, a same-stem
        source that sorts first joins: the damaged bytes still name the
        document the directory was kept for, so that document fails, and
        the newcomer gets a directory of its own."""
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 0
        state = orchestrator.state_dir / "letter" / "state.json"
        good = state.read_text()
        cut = good[: len(good) // 2]
        assert '/letter.txt"' in cut  # a state is saved with its file first
        state.write_text(cut)
        (orchestrator.folder / "letter.md").write_text(
            "Felix was born in 1867."
        )
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 1
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "letter.txt"
        assert event["reason"].startswith("its state.json cannot be read (")
        assert not any(
            c.endswith("letter.txt") for c in orchestrator.calls[calls:]
        )
        assert "manifest:letter.md" in orchestrator.calls[calls:]
        assert self.dirs(orchestrator) == {
            "letter.md": "letter.md",
            "letter.txt": "letter",
        }
        assert state.read_text() == cut

    @pytest.mark.parametrize(
        ("damaged", "owner"),
        [
            ('{\n  "file": "/docs/letter.txt",\n  "tit', "letter.txt"),
            ('{\n  "file": "/docs/letter.md",\n  "tit', "letter.md"),
            ('{\n  "fi', "letter.md"),  # names none: the first by name
            ('{\n  "file": "/docs/letter.pdf"', "letter.md"),  # one gone
        ],
        ids=[
            "names-the-later",
            "names-the-first",
            "names-none",
            "names-other",
        ],
    )
    def test_a_damaged_directory_goes_to_the_source_it_names(
        self, tmp_path, damaged, owner
    ):
        state_dir = tmp_path / "state"
        (state_dir / "letter").mkdir(parents=True)
        (state_dir / "letter" / "state.json").write_text(damaged)
        sources = [Path("/docs/letter.md"), Path("/docs/letter.txt")]
        names = directory_names(sources, state_dir)
        other = next(s for s in sources if s.name != owner)
        assert names[Path("/docs") / owner] == "letter"
        assert names[other] == other.name

    def test_a_renamed_source_keeps_its_directory_when_its_name_returns(
        self, tmp_path
    ):
        """A source renamed with its bytes unchanged carries on in its
        directory, whose state then names it; a new source under the old
        name, with other bytes, gets a directory of its own and moves
        nothing."""
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 0
        folder = orchestrator.folder
        (folder / "letter.txt").rename(folder / "letter.md")
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {"letter.md": "letter"}
        assert orchestrator.calls[calls:] == []
        record = orchestrator.state_dir / "letter" / "state.json"
        assert Path(json.loads(record.read_text())["file"]).name == "letter.md"
        (folder / "letter.txt").write_text("Felix was born in 1867.")
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        assert self.dirs(orchestrator) == {
            "letter.md": "letter",
            "letter.txt": "letter.txt",
        }
        assert {c.split(":")[1] for c in orchestrator.calls[calls:]} == {
            "letter.txt"
        }
        assert this_run(orchestrator, "set-aside") == []

    def test_a_newcomer_never_fails_into_a_departed_sources_state(
        self, tmp_path
    ):
        """A new source given the stem directory of a source that left,
        whose state it loads, fails when it cannot be read without writing
        into that state: the departed document's stays as it was, its
        `written` included."""
        orchestrator = completed_run(tmp_path)
        folder = orchestrator.folder
        record = orchestrator.state_dir / "letter" / "state.json"
        before = record.read_bytes()
        assert json.loads(before)["status"] == BATCHED
        (folder / "letter.txt").unlink()
        (folder / "letter.md").write_text("Felix was born in 1867.")
        with unreadable(folder / "letter.md"):
            assert orchestrator.run() == 1
        (event,) = this_run(orchestrator, "document-failed")
        assert event["file"] == "letter.md"
        assert record.read_bytes() == before


class TestUnrecordedDirectory:
    """A document directory without derivations.json, from before the
    records or one that lost its record: nothing says what its files were
    derived from, so it is set aside whole, as a changed source is, and
    the document starts afresh in a new one (#225)."""

    def from_before_the_records(self, tmp_path):
        """A written document's directory as a run before the records left
        it: every file in place, and no derivations.json."""
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        return orchestrator

    def test_it_is_set_aside_whole_and_started_afresh(self, tmp_path):
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        before = snapshot(doc.dir, skip=())
        lines: list[str] = []
        orchestrator.log = collect(lines)
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 0
        kept = doc.dir.with_name(".letter.superseded-1")
        assert snapshot(kept, skip=()) == before
        # the document is derived again, in a new directory
        assert steps(orchestrator.calls[calls:]) == [
            "manifest",
            "chunk",
            "critique",
        ]
        (doc,) = orchestrator.documents()
        assert doc.state.status == BATCHED
        assert (doc.dir / "derivations.json").exists()
        assert not doc.dir.joinpath("equivalences.reviewed.csv").exists()
        assert any(
            "no derivations.json records what the files in its directory "
            "were derived from; the old directory is kept as "
            ".letter.superseded-1" in line
            for line in lines
        )
        (event,) = this_run(orchestrator, "set-aside")
        assert event["subject"] == doc.sha1
        assert event["kept"] == ".letter.superseded-1"
        assert "no derivations.json" in event["reason"]
        # from then on the directory is recorded, and nothing more moves
        assert orchestrator.run() == 0
        assert not doc.dir.with_name(".letter.superseded-2").exists()

    @pytest.mark.parametrize(
        "keep", [(), ("state.json",)], ids=["no-state", "state-only"]
    )
    def test_any_file_without_a_record_sets_it_aside(self, tmp_path, keep):
        """A directory that lost its state.json too, or holds nothing but
        a state.json (a document whose registration failed before the
        records, say), is set aside all the same."""
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        for path in doc.dir.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()
        before = snapshot(doc.dir, skip=())
        assert before
        assert orchestrator.run() == 0
        assert (
            snapshot(doc.dir.with_name(".letter.superseded-1"), skip=())
            == before
        )
        (doc,) = orchestrator.documents()
        assert doc.state.status == BATCHED

    def test_a_gedcom_directory_is_set_aside_too(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "family.ged").write_text(
            "0 @I1@ INDI\n1 NAME Klara /Löbig/\n1 BIRT\n2 DATE 1917\n0 TRLR\n"
        )
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        assert orchestrator.run() == 0
        assert doc.dir.with_name(".family.superseded-1").is_dir()
        (doc,) = orchestrator.documents()
        assert doc.state.status == BATCHED

    def test_a_first_run_that_stopped_before_its_batch_carries_on(
        self, tmp_path
    ):
        """A directory is recorded from its first run: derivations.json is
        written before any other file. A first run that fails before the
        batch leaves transcription.json, and the next run carries on in
        the same directory."""
        orchestrator = make_run(tmp_path, fail_step="manifest")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.transcription.exists()
        assert (doc.dir / "derivations.json").exists()
        orchestrator.fail_step = None
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == BATCHED
        assert this_run(orchestrator, "set-aside") == []

    @pytest.mark.parametrize(
        "error",
        [OSError("the disk refused"), KeyboardInterrupt()],
        ids=["failure", "interrupt"],
    )
    def test_a_stop_before_the_first_record_leaves_nothing_behind(
        self, tmp_path, monkeypatch, error
    ):
        """A run that stops while a new directory's record is being written
        saves no state there, so the next run starts it afresh in place
        rather than setting it aside."""
        import yizkr.pipeline.document_state as scratch

        def refuse(self):
            raise error

        orchestrator = make_run(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(scratch.Derivations, "begin", refuse)
            if isinstance(error, KeyboardInterrupt):
                with pytest.raises(KeyboardInterrupt):
                    orchestrator.run()
            else:
                assert orchestrator.run() == 1
            (doc,) = orchestrator.documents()
            assert not doc.state_path.exists()
            assert not doc.unrecorded
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == BATCHED
        assert not doc.dir.with_name(".letter.superseded-1").exists()

    def test_a_duplicate_is_recorded_from_its_first_run(self, tmp_path):
        """A duplicate's directory gets its record too, so when the file
        it duplicated is gone it is ingested in place, not set aside."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a-original.txt").write_text("Leopold was born in 1914.")
        (folder / "b-copy.txt").write_text("Leopold was born in 1914.")
        orchestrator = ScriptedDriver(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        _, copy = orchestrator.documents()
        assert copy.state.status == DUPLICATE
        (folder / "a-original.txt").unlink()
        assert orchestrator.run() == 0
        (copy,) = orchestrator.documents()
        assert copy.state.status == BATCHED
        assert this_run(orchestrator, "set-aside") == []


class TestTranscribeLanguage:
    """Whole-folder ingestion detects each document's language (offline,
    via the text-file engine — no LLM needed to exercise the wiring)."""

    def _doc(self, tmp_path, name, text):
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text)
        source = folder / name
        doc = DocumentRun(source, tmp_path / "state")
        # the run makes the document's directory before any stage
        doc.dir.mkdir(parents=True, exist_ok=True)
        return doc

    def test_auto_detects_per_document(self, tmp_path):
        from tests.fixtures import example_universe as u

        sources = {source.key: source for source in u.SOURCES}
        german = sources["marriage-cert-1905"].text  # a register entry
        english = sources["interview-2002"].text  # an oral history
        # one orchestrator, default language (auto), two languages in a folder
        orch = ScratchDriver(tmp_path / "docs", tmp_path / "state", log=quiet)
        de = orch._step_transcribe(
            self._doc(tmp_path, "marriage-cert-1905.txt", german)
        )
        en = orch._step_transcribe(
            self._doc(tmp_path, "interview-2002.txt", english)
        )
        assert de.language == "de"
        assert en.language == "en"

    def test_explicit_language_forces_and_records_source(self, tmp_path):
        german = "Liebes Pünktchen! Seit Du am 22. Juni abgereist bist."
        orch = ScratchDriver(
            tmp_path / "docs", tmp_path / "state", language="fr", log=quiet
        )
        doc = self._doc(tmp_path, "postcard-1938.txt", german)
        transcription = orch._step_transcribe(doc)
        assert transcription.language == "fr"  # forced, not detected
        assert doc.state.detail["language"]["source"] == "override"

    def test_short_document_uses_fallback(self, tmp_path):
        orch = ScratchDriver(
            tmp_path / "docs",
            tmp_path / "state",
            language="auto",
            fallback_language="de",
            log=quiet,
        )
        doc = self._doc(tmp_path, "postmark.txt", "19.8.38")  # too short
        transcription = orch._step_transcribe(doc)
        assert transcription.language == "de"
        assert doc.state.detail["language"]["source"] == "fallback"
