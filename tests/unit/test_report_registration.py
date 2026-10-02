"""Offline tests for how the run report counts registration's flags (#371;
docs/ANOMALIES.md, "Registration").

Registration raises its flags as alerts of the write, its checks
`shared-file` and `unhashed-title` (yizkr.pipeline.registrar). The
aggregates, which leave the machine, count them by name, as counts only;
a document's own rows, which stay in the run directory, carry the items
each flag names by QID. Fictional data only (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from yizkr.pipeline import report
from yizkr.pipeline.registrar import REGISTRATION_CHECKS
from yizkr.pipeline.rundir import RunDir

# the Löbig family's studio portrait of the 1890s, as a scan's file
PORTRAIT_FILE = "portrait-1890s.png"
PORTRAIT_SHA1 = hashlib.sha1(b"synthetic studio portrait, 1890s").hexdigest()


def flagged_run(tmp_path):
    """A run directory whose one document's write raised both flags, a
    second time on a later invocation, and an alert of an unknown check
    and stage."""
    rundir = RunDir(tmp_path / "run")
    rundir.start(
        documents=[
            {
                "sha1": PORTRAIT_SHA1,
                "file": PORTRAIT_FILE,
                "kind": "media",
                "stages": ["transcription", "extraction", "critique"],
            }
        ]
    )
    for check in ("shared-file", "unhashed-title", "shared-file"):
        rundir.alert(
            stage="write",
            check=check,
            subject=PORTRAIT_SHA1[:12],
            value=True,
            carried=False,
            kind="media",
            artifact="Q101",
            others=["Q9", "Q100"],
            medium=PORTRAIT_SHA1[:12],
        )
    rundir.alert(
        stage="loebig-stage",
        check="loebig-check",
        subject=PORTRAIT_SHA1[:12],
        value=True,
        carried=False,
    )
    rundir.end("finished")
    return rundir.path


def test_the_report_knows_the_registration_checks():
    assert REGISTRATION_CHECKS == report.REGISTRATION_CHECKS
    assert REGISTRATION_CHECKS <= report.CHECKS


def test_the_aggregates_name_the_checks_and_the_write(tmp_path):
    built = report.build(flagged_run(tmp_path))
    anomalies = built["aggregate"]["anomalies"]
    assert anomalies["by_check"] == {
        "shared-file": {"new": 1, "carried": 0, "alerts": 2},
        "unhashed-title": {"new": 1, "carried": 0, "alerts": 1},
        report.OTHER: {"new": 1, "carried": 0, "alerts": 1},
    }
    assert anomalies["by_stage"] == {
        "write": {"new": 2, "carried": 0},
        report.OTHER: {"new": 1, "carried": 0},
    }


def test_the_aggregates_hold_no_item_and_no_file(tmp_path):
    built = report.build(flagged_run(tmp_path))
    shown = json.dumps(built["aggregate"]) + report.render_markdown(
        built["aggregate"]
    )
    for word in ("Q101", "Q100", "Q9", PORTRAIT_FILE, PORTRAIT_SHA1[:12]):
        assert word not in shown
    assert "shared-file" in shown and "unhashed-title" in shown


def test_a_documents_rows_carry_the_items_by_qid(tmp_path):
    built = report.build(flagged_run(tmp_path))
    (document,) = built["documents"]
    rows = [
        (alert["stage"], alert["check"], alert["artifact"], alert["others"])
        for alert in document["alerts"]
    ]
    assert rows == [
        ("write", "shared-file", "Q101", ["Q9", "Q100"]),
        ("write", "unhashed-title", "Q101", ["Q9", "Q100"]),
        ("write", "shared-file", "Q101", ["Q9", "Q100"]),
        ("loebig-stage", "loebig-check", None, None),
    ]
    page = report.render_html(built)
    assert "Q101 against Q9, Q100" in page


@pytest.mark.parametrize(
    "artifact", ["Studio portrait of the Löbig family", "Q12 portrait", "Q12x"]
)
def test_a_row_keeps_qids_alone(tmp_path, artifact):
    """An `artifact` or `others` that is not an item's QID is left out,
    one that only starts with a QID included."""
    rundir = RunDir(tmp_path / "run")
    rundir.start(
        documents=[{"sha1": PORTRAIT_SHA1, "file": PORTRAIT_FILE}],
    )
    rundir.alert(
        stage="write",
        check="shared-file",
        subject=PORTRAIT_SHA1[:12],
        value=True,
        carried=False,
        artifact=artifact,
        others=[
            "Q7",
            "File:Portrait-1890s.png",
            7,
            "Q07",
            "Q12 portrait",
            "Q12x",
        ],
    )
    rundir.end("finished")
    (document,) = report.build(rundir.path)["documents"]
    (alert,) = document["alerts"]
    assert (alert["artifact"], alert["others"]) == (None, ["Q7"])
