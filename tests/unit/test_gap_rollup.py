"""The cross-document gap rollup (ADR-0013 §B, #91): every relation gap a
run's documents logged, one row per normalised predicate.

The fixture run directory is fictional: documents of the example
universe (docs/EXAMPLES.md), their gaps worded from its sources.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from yizkr.cli import main
from yizkr.pipeline import gaps
from yizkr.pipeline.gaps import COLUMNS, ROLLUP_FILE, predicate_key, rollup

NATURALISATION = "a" * 40
DIRECTORY = "b" * 40
INTERVIEW = "c" * 40
POSTCARD = "d" * 40

ARRIVED = {
    "subject": "gustav",
    "predicate": "arrived per",
    "object": "RMS Flund",
    "evidence": "Arrived in Australia 1939 per RMS Flund.",
}
FOUNDED = {
    "subject": "zum-blauen-hut",
    "predicate": "founded",
    "object": "1863",
    "evidence": "gegr. 1863",
}


def document(
    run: Path,
    name: str,
    sha1: str | None,
    *,
    ndjson: list | None = None,
    coverage: bool = False,
    gaps_csv: list[dict] | None = None,
    status: str = "extracted",
    parent: str | None = None,
) -> Path:
    """A document's directory in the run, with its state and whichever
    gap files are given."""
    directory = (run / parent if parent else run) / name
    directory.mkdir(parents=True)
    state = {"file": f"{name}.txt", "title": name, "status": status}
    if sha1:
        state["content_sha1"] = sha1
    (directory / "state.json").write_text(json.dumps(state))
    extraction = directory / "extraction"
    if ndjson is not None or coverage:
        extraction.mkdir()
    if coverage:
        (extraction / "coverage.json").write_text("{}")
    if ndjson is not None:
        (extraction / "gaps.ndjson").write_text(
            "\n".join(
                line if isinstance(line, str) else json.dumps(line)
                for line in ndjson
            )
        )
    if gaps_csv is not None:
        with open(directory / "gaps.csv", "w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["subject", "predicate", "object", "evidence"],
            )
            writer.writeheader()
            writer.writerows(gaps_csv)
    return directory


@pytest.fixture
def run(tmp_path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text("{}")
    return run


class TestPredicateKey:
    @pytest.mark.parametrize(
        "spelling",
        [
            "arrived per",
            "Arrived Per",
            "arrived_per",
            "arrived-per",
            "  arrived \t per ",
            "ARRIVED__PER",
            "ａｒｒｉｖｅｄ per",  # fullwidth compatibility forms
        ],
    )
    def test_spellings_of_one_predicate_are_one_key(self, spelling):
        assert predicate_key(spelling) == "arrived per"

    def test_other_words_are_another_key(self):
        assert predicate_key("arrived at") != predicate_key("arrived per")


class TestDocumentGaps:
    def test_the_chunked_log_is_read(self, run):
        directory = document(
            run, "naturalisation", NATURALISATION, ndjson=[ARRIVED]
        )
        assert gaps.document_gaps(directory) == [ARRIVED]

    def test_the_chunked_log_wins_over_the_csv_beside_it(self, run):
        # the csv renders the same gaps: read once
        directory = document(
            run,
            "naturalisation",
            NATURALISATION,
            ndjson=[ARRIVED],
            coverage=True,
            gaps_csv=[ARRIVED],
        )
        assert gaps.document_gaps(directory) == [ARRIVED]

    def test_a_chunked_extraction_without_a_log_has_no_gaps(self, run):
        # its coverage report stands, so a gaps.csv left from an earlier
        # extraction is stale
        directory = document(
            run, "directory", DIRECTORY, coverage=True, gaps_csv=[FOUNDED]
        )
        assert gaps.document_gaps(directory) == []

    def test_the_csv_is_read_without_a_chunked_extraction(self, run):
        directory = document(run, "directory", DIRECTORY, gaps_csv=[FOUNDED])
        assert gaps.document_gaps(directory) == [FOUNDED]

    def test_lines_that_are_no_gap_are_passed_over(self, run):
        directory = document(
            run,
            "naturalisation",
            NATURALISATION,
            ndjson=[
                ARRIVED,
                "not json",
                "",
                ["a list"],
                {"subject": "gustav", "predicate": "  "},
                {"subject": "", "predicate": "arrived per"},
                {
                    "subject": "gustav",
                    "predicate": "naturalised",
                    "object": None,
                },
            ],
        )
        assert gaps.document_gaps(directory) == [
            ARRIVED,
            {
                "subject": "gustav",
                "predicate": "naturalised",
                "object": "",
                "evidence": "",
            },
        ]

    def test_csv_rows_that_are_no_gap_are_passed_over(self, run):
        directory = document(
            run,
            "directory",
            DIRECTORY,
            gaps_csv=[
                FOUNDED,
                dict(FOUNDED, predicate=""),
                dict(FOUNDED, subject=""),
            ],
        )
        assert gaps.document_gaps(directory) == [FOUNDED]

    def test_a_line_break_in_csv_evidence_is_kept(self, run):
        evidence = "Hutmacherei zum Blauen Hut\r\ngegr. 1863"
        directory = document(
            run,
            "directory",
            DIRECTORY,
            gaps_csv=[dict(FOUNDED, evidence=evidence)],
        )
        (gap,) = gaps.document_gaps(directory)
        assert gap["evidence"] == evidence

    def test_a_document_without_gap_files_has_none(self, run):
        assert gaps.document_gaps(document(run, "postcard", POSTCARD)) == []


class TestDocumentDirectories:
    def test_documents_and_excerpts_are_read(self, run):
        document(run, "naturalisation", NATURALISATION)
        document(run, DIRECTORY, DIRECTORY, parent="excerpts")
        assert gaps.document_directories(run) == [
            (NATURALISATION, run / "naturalisation"),
            (DIRECTORY, run / "excerpts" / DIRECTORY),
        ]

    def test_what_is_no_document_is_passed_over(self, run):
        document(run, "naturalisation", NATURALISATION)
        document(run, ".set-aside", INTERVIEW)
        document(run, "copy", POSTCARD, status="duplicate")
        (run / "corpus" / "clustering").mkdir(parents=True)
        (run / "unreadable").mkdir()
        (run / "unreadable" / "state.json").write_text("{cut")
        (run / "listed").mkdir()
        (run / "listed" / "state.json").write_text('["state"]')
        (run / "events.jsonl").write_text("")
        assert gaps.document_directories(run) == [
            (NATURALISATION, run / "naturalisation")
        ]

    def test_one_sha1_is_read_once(self, run):
        document(run, "a-naturalisation", NATURALISATION)
        document(run, "b-naturalisation", NATURALISATION)
        assert gaps.document_directories(run) == [
            (NATURALISATION, run / "a-naturalisation")
        ]

    def test_a_state_without_a_sha1_is_keyed_by_its_directory(self, run):
        document(run, "postcard", None)
        assert gaps.document_directories(run) == [
            ("postcard", run / "postcard")
        ]


class TestRollup:
    @pytest.fixture
    def filled(self, run) -> Path:
        document(
            run,
            "naturalisation",
            NATURALISATION,
            coverage=True,
            ndjson=[ARRIVED, dict(ARRIVED, predicate="Arrived_Per")],
        )
        document(
            run,
            "interview",
            INTERVIEW,
            coverage=True,
            ndjson=[
                dict(FOUNDED, evidence="the Blue Hat, founded in 1863"),
                dict(
                    ARRIVED,
                    predicate="arrived  per",
                    evidence="came per RMS Flund",
                ),
            ],
        )
        document(run, "directory", DIRECTORY, gaps_csv=[FOUNDED])
        document(run, "postcard", POSTCARD, coverage=True)
        return run

    def test_one_row_per_key_by_frequency_then_documents(self, filled):
        result = rollup(filled)
        assert [row["predicate"] for row in result.rows] == [
            "arrived per",
            "founded",
        ]
        arrived, founded = result.rows
        assert (arrived["frequency"], arrived["documents"]) == (3, 2)
        assert (founded["frequency"], founded["documents"]) == (2, 2)
        assert (
            result.documents_read,
            result.documents_with_gaps,
            result.gaps,
        ) == (4, 3, 5)

    def test_a_row_says_where_and_how_it_is_spelled(self, filled):
        arrived, founded = rollup(filled).rows
        # the most frequent spelling first, runs of spaces made one
        assert arrived["variants"] == "arrived per|Arrived_Per"
        assert arrived["document_sha1s"] == f"{NATURALISATION}|{INTERVIEW}"
        assert founded["document_sha1s"] == f"{DIRECTORY}|{INTERVIEW}"
        # the first gap in document order is the example: the documents
        # are read in their directories' name order
        assert (
            founded["example_subject"],
            founded["example_object"],
            founded["example_evidence"],
        ) == (FOUNDED["subject"], FOUNDED["object"], FOUNDED["evidence"])
        assert arrived["example_evidence"] == "came per RMS Flund"

    def test_the_decision_is_the_curators(self, filled):
        for row in rollup(filled).rows:
            assert (row["decision"], row["notes"]) == ("", "")
            assert tuple(row) == COLUMNS

    def test_documents_tie_on_frequency_by_key(self, run):
        document(
            run,
            "interview",
            INTERVIEW,
            ndjson=[dict(FOUNDED, predicate="owned by"), FOUNDED],
        )
        assert [row["predicate"] for row in rollup(run).rows] == [
            "founded",
            "owned by",
        ]

    def test_more_documents_rank_first_on_equal_frequency(self, run):
        document(
            run,
            "interview",
            INTERVIEW,
            ndjson=[dict(FOUNDED, predicate="arrived per")] * 2,
        )
        document(run, "directory", DIRECTORY, ndjson=[FOUNDED])
        document(run, "naturalisation", NATURALISATION, ndjson=[FOUNDED])
        assert [row["predicate"] for row in rollup(run).rows] == [
            "founded",
            "arrived per",
        ]

    def test_frequency_ranks_before_documents(self, run):
        document(
            run,
            "interview",
            INTERVIEW,
            ndjson=[dict(FOUNDED, predicate="owned by")] * 3 + [FOUNDED],
        )
        document(run, "directory", DIRECTORY, ndjson=[FOUNDED])
        assert [
            (row["predicate"], row["frequency"], row["documents"])
            for row in rollup(run).rows
        ] == [("owned by", 3, 1), ("founded", 2, 2)]

    def test_an_empty_run_rolls_up_nothing(self, run):
        result = rollup(run)
        assert (result.rows, result.documents_read, result.gaps) == ([], 0, 0)


class TestWrite:
    def test_the_csv_is_written_into_the_run_directory(self, run):
        document(run, "naturalisation", NATURALISATION, ndjson=[ARRIVED])
        path, result = gaps.write_rollup(run)
        assert path == run / ROLLUP_FILE
        with open(path, newline="") as handle:
            reader = csv.DictReader(handle)
            assert tuple(reader.fieldnames) == COLUMNS
            rows = list(reader)
        assert rows == [
            {key: str(value) for key, value in row.items()}
            for row in result.rows
        ]
        assert rows[0]["example_object"] == "RMS Flund"

    def test_out_names_another_file(self, run, tmp_path):
        out = tmp_path / "elsewhere" / "rollup.csv"
        path, _ = gaps.write_rollup(run, out)
        assert path == out
        assert out.read_text().splitlines() == [",".join(COLUMNS)]
        assert not (run / ROLLUP_FILE).exists()


class TestCli:
    def test_rollup_names_a_run_directory(self, run, capsys):
        document(
            run, "naturalisation", NATURALISATION, ndjson=[ARRIVED, ARRIVED]
        )
        document(run, "postcard", POSTCARD)
        assert main(["gaps", "rollup", str(run)]) == 0
        out = capsys.readouterr().out
        assert (
            "2 gap(s) under 1 predicate(s), from 1 of 2 document(s): "
            f"{run / ROLLUP_FILE}" in out
        )
        assert "stays local" in out
        assert (run / ROLLUP_FILE).is_file()

    def test_rollup_takes_the_latest_run_in_runs(self, tmp_path, capsys):
        runs = tmp_path / "runs"
        for name in ("20261001-0900-0", "20261002-0900-0"):
            (runs / name).mkdir(parents=True)
            (runs / name / "run.json").write_text("{}")
        assert main(["gaps", "rollup", "--runs", str(runs)]) == 0
        assert (runs / "20261002-0900-0" / ROLLUP_FILE).is_file()
        assert not (runs / "20261001-0900-0" / ROLLUP_FILE).exists()

    def test_no_run_is_refused(self, tmp_path, capsys):
        from yizkr.core.exits import EXIT_REFUSED

        status = main(["gaps", "rollup", "--runs", str(tmp_path / "none")])
        assert status == EXIT_REFUSED
        assert "no scratch run" in capsys.readouterr().err

    def test_a_file_that_cannot_be_written_is_refused(
        self, run, tmp_path, capsys
    ):
        from yizkr.core.exits import EXIT_REFUSED

        (tmp_path / "a-file").write_text("")
        out = tmp_path / "a-file" / "rollup.csv"
        status = main(["gaps", "rollup", str(run), "--out", str(out)])
        assert status == EXIT_REFUSED
        assert "[Errno 17] File exists" in capsys.readouterr().err

    def test_a_gap_log_that_is_no_utf8_is_refused(self, run, capsys):
        from yizkr.core.exits import EXIT_REFUSED

        directory = document(run, "postcard", POSTCARD, ndjson=[])
        (directory / "extraction" / "gaps.ndjson").write_bytes(b"\xff\xfe")
        assert main(["gaps", "rollup", str(run)]) == EXIT_REFUSED
        assert "can't decode" in capsys.readouterr().err
        assert not (run / ROLLUP_FILE).exists()

    def test_a_gap_csv_that_is_no_csv_is_refused(self, run, capsys):
        from yizkr.core.exits import EXIT_REFUSED

        document(
            run,
            "directory",
            DIRECTORY,
            gaps_csv=[dict(FOUNDED, evidence="gegr. 1863 " * 20000)],
        )
        assert main(["gaps", "rollup", str(run)]) == EXIT_REFUSED
        assert "field limit" in capsys.readouterr().err

    def test_out(self, run, tmp_path, capsys):
        out = tmp_path / "rollup.csv"
        assert main(["gaps", "rollup", str(run), "--out", str(out)]) == 0
        assert out.is_file()
        assert str(out) in capsys.readouterr().out
