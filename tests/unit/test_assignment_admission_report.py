"""The run report's assignment admission: what the assignment path's own
checks (review_mentions.observe_mentions, claim_review.locate_support) make
of a run's saved views, counted in its aggregates from those views alone.

Each test writes a small run directory as the scratch driver leaves it,
holding only what the report reads: run.json's plan, each completed stage's
event and each document's state and views. Its texts, labels and excerpts
come from the example universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from tests.fixtures import example_universe as u
from yizkr.pipeline import report
from yizkr.pipeline.stage_planning import DOCUMENT_STAGES

SOURCES = {source.key: source for source in u.SOURCES}
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
RUN = {
    "model_identifier": "fictional-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}
# the directory's second line: Ursula and her street, each named once
URSULA = SOURCES["directory-1912"].text.splitlines()[1]
# the certificate, one line: Flörburg three times, Löbig twice
CERTIFICATE = SOURCES["marriage-cert-1905"].text


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def local(key: str) -> dict:
    return {"kind": "local", "local_id": key}


def entity(key: str, label: str, kind: str = "human", lines=(1,)) -> dict:
    return {
        "id": key,
        "label": label,
        "kind": kind,
        "mention_lines": list(lines),
    }


def claim(subject: dict, prop: str, value: dict, excerpt: str, source: str):
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": {
            "artifact": {"kind": "artifact", "sha1": source},
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        },
    }


def placed(prop: str, subject: str, place: str, excerpt: str):
    """A claim that the person `subject` has `place` as `prop`, cited by
    `excerpt`, for the source whose SHA-1 it is given."""
    return lambda source: claim(
        local(subject),
        prop,
        {"type": "item", "value": local(place)},
        excerpt,
        source,
    )


def document(kind, text, entities, claims, *, key=None, done=None) -> dict:
    """A planned document of `kind` whose reading is `text` (None for no
    reading), with a batch of `entities` and `claims`; `done` names the
    stages that completed, by default all of the kind's."""
    stages = list(DOCUMENT_STAGES[kind])
    source = sha1(key or text)
    views = {}
    if text is not None:
        views["transcription.json"] = {
            "artifact": {"kind": "artifact", "sha1": source},
            "text": text,
            "language": "de",
            "engine": "text-file",
        }
    views["batch.json"] = {
        "extraction_run": RUN,
        "entities": entities,
        "claims": [c(source) for c in claims],
    }
    return {
        "sha1": source,
        "kind": kind,
        "stages": stages,
        "done": stages if done is None else done,
        "views": views,
    }


def build(path: Path, *documents: dict) -> dict:
    """The report of a run directory in `path` holding `documents`."""
    path.mkdir(parents=True)
    plan = [
        {
            "sha1": d["sha1"],
            "file": f"document {n}.txt",
            "kind": d["kind"],
            "stages": d["stages"],
        }
        for n, d in enumerate(documents)
    ]
    (path / "run.json").write_text(json.dumps({"documents": plan}))
    events = [
        {
            "at": NOW.isoformat(),
            "event": "done",
            "invocation": "0a1b2c3d4e5f",
            "subject": d["sha1"],
            "kind": d["kind"],
            "stage": stage,
        }
        for d in documents
        for stage in d["done"]
    ]
    (path / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )
    for n, d in enumerate(documents):
        directory = path / f"document-{n}"
        directory.mkdir()
        (directory / "state.json").write_text(
            json.dumps({"content_sha1": d["sha1"], "status": "batched"})
        )
        for name, view in d["views"].items():
            content = view if isinstance(view, str) else json.dumps(view)
            (directory / name).write_text(content, encoding="utf-8")
    return report.build(path, now=NOW)


def admitted(made: dict, kind: str) -> dict:
    return made["aggregate"]["assignment_admission"]["by_kind"][kind]


def section(made: dict) -> str:
    """The section as report.md renders it."""
    text = report.render_markdown(made["aggregate"])
    start = text.index("## Assignment admission")
    return text[start : text.index("\n## ", start)]


def test_a_located_claim_whose_roles_are_anchored_is_within_the_bound(
    tmp_path,
):
    made = build(
        tmp_path / "run",
        document(
            "text",
            URSULA,
            [
                entity("e1", "Ursula"),
                entity("e2", "Angerring 57", "place"),
            ],
            [placed("residence", "e1", "e2", URSULA)],
        ),
    )
    counts = admitted(made, "text")
    assert counts["mentions_by_outcome"] == {"anchored": 2}
    assert counts["claims_by_outcome"] == {"located": 1}
    assert counts["claims_by_references"]["anchored"] == 1
    assert counts["upper_bound"] == 1
    assert made["aggregate"]["assignment_admission"]["all"]["upper_bound"] == 1


def test_a_homonym_on_one_line_anchors_neither_mention(tmp_path):
    """Two mentions of one name the line states once share its one
    occurrence; one name the line states more than once is no location."""
    line = SOURCES["directory-1912"].text.splitlines()[0]
    made = build(
        tmp_path / "run",
        document(
            "text",
            line,
            [
                entity("e1", "Sebastian Löbig"),
                entity("e2", "Sebastian Löbig"),
                entity("e3", "Brunnenring 35", "place"),
            ],
            [placed("residence", "e1", "e3", line)],
        ),
        document(
            "media",
            CERTIFICATE,
            [
                entity("e1", "Hildegard Laudler"),
                entity("e2", "Flörburg", "place"),
            ],
            [placed("residence", "e1", "e2", CERTIFICATE)],
        ),
    )
    text, media = admitted(made, "text"), admitted(made, "media")
    assert text["mentions_by_outcome"] == {
        "shared_source_occurrence": 2,
        "anchored": 1,
    }
    assert media["mentions_by_outcome"] == {
        "anchored": 1,
        "missing_or_ambiguous_literal_mention": 1,
    }
    # each claim is located, but a role of each is not anchored
    for counts in (text, media):
        assert counts["claims_by_outcome"] == {"located": 1}
        assert counts["claims_by_references"]["not_anchored"] == 1
        assert counts["upper_bound"] == 0


def test_a_repeated_quote_is_no_location(tmp_path):
    reading = URSULA + "\n" + URSULA
    made = build(
        tmp_path / "run",
        document(
            "text",
            reading,
            [entity("e1", "Ursula", lines=(2,))],
            [],
        ),
    )
    assert admitted(made, "text")["mentions_by_outcome"] == {
        "repeated_source_quote": 1
    }


def test_an_excerpt_the_reading_holds_twice_is_refused(tmp_path):
    made = build(
        tmp_path / "run",
        document(
            "media",
            CERTIFICATE,
            [
                entity("e1", "Hildegard Laudler"),
                entity("e2", "Fläckfeld", "place"),
            ],
            [
                placed("place_of_birth", "e1", "e2", "zu Fläckfeld"),
                placed("place_of_birth", "e1", "e2", "Flörburg"),
            ],
        ),
    )
    counts = admitted(made, "media")
    assert counts["claims_by_outcome"] == {
        "excerpt_repeated": 1,
        "located": 1,
    }
    assert counts["located_by_references"]["anchored"] == 1
    assert counts["upper_bound"] == 1


def test_a_gedcom_batch_has_no_reading_to_locate_in(tmp_path):
    made = build(
        tmp_path / "run",
        document(
            "gedcom",
            None,
            [entity("I1", "Eugen Löbig"), entity("I2", "Cäcilie Tarle")],
            [
                lambda s: claim(
                    local("I1"),
                    "spouse",
                    {"type": "item", "value": local("I2")},
                    "Cäcilie Tarle",
                    s,
                )
            ],
            key="a fictional family tree",
        ),
    )
    counts = admitted(made, "gedcom")
    assert counts[report.OBSERVED] == 1
    assert counts["mentions_by_outcome"] == {"reading_unavailable": 2}
    assert counts["claims_by_outcome"] == {"no_reading": 1}
    assert counts["upper_bound"] == 0


def test_a_circumstance_source_is_located_after_the_source(tmp_path):
    def marked(source):
        located = placed("residence", "e1", "e2", URSULA)(source)
        located["circumstance_source"] = {
            "artifact": {"kind": "artifact", "sha1": source},
            "transcription_excerpt": {"text": "maschinell", "language": "de"},
        }
        return located

    made = build(
        tmp_path / "run",
        document(
            "text",
            URSULA,
            [entity("e1", "Ursula"), entity("e2", "Angerring 57", "place")],
            [marked],
        ),
    )
    counts = admitted(made, "text")
    assert counts["claims_by_outcome"] == {"excerpt_absent": 1}
    assert counts["upper_bound"] == 0


def test_a_claim_with_no_local_reference_is_counted_apart(tmp_path):
    def of_the_source(source):
        return claim(
            {"kind": "artifact", "sha1": source},
            "instance_of",
            {
                "type": "item",
                "value": {"kind": "vocab", "key": "address_book"},
            },
            URSULA,
            source,
        )

    made = build(
        tmp_path / "run",
        document("text", URSULA, [entity("e1", "Ursula")], [of_the_source]),
    )
    counts = admitted(made, "text")
    assert counts["claims_by_references"] == {
        "anchored": 0,
        "not_anchored": 0,
        "no_local_reference": 1,
    }
    assert counts["located_by_references"]["no_local_reference"] == 1
    assert counts["upper_bound"] == 1


def test_documents_it_cannot_observe_are_counted_never_left_out(tmp_path):
    """A batch that is not the model the assignment path reads, a reading
    missing for a transcription that completed, products observe_mentions
    refuses, and a batch whose stage never completed: each document is
    counted by kind under why, with none of its mentions or claims."""
    mentions = [entity("e1", "Ursula"), entity("e2", "Angerring 57", "place")]
    claims = [placed("residence", "e1", "e2", URSULA)]
    older = document("text", URSULA, mentions, claims, key="older")
    older["views"]["batch.json"]["chunks"] = 2
    unread = document("text", URSULA, mentions, claims, key="unread")
    del unread["views"]["transcription.json"]
    repeated = document(
        "text", URSULA, [*mentions, entity("e1", "Ursula")], claims
    )
    pending = document("media", CERTIFICATE, [], [], done=["transcription"])
    made = build(tmp_path / "run", older, unread, repeated, pending)
    text, media = admitted(made, "text"), admitted(made, "media")
    assert (text["documents"], media["documents"]) == (3, 1)
    assert text[report.UNREADABLE] == 2
    assert text[report.UNOBSERVABLE] == 1
    assert media[report.NO_BATCH] == 1
    assert text[report.OBSERVED] == media[report.OBSERVED] == 0
    assert text["mentions"] == text["claims"] == 0
    # the content figures still read the batch the older run wrote
    assert made["aggregate"]["content"]["batches"] == 3


def test_report_md_gives_the_section_by_kind_and_nothing_of_a_document(
    tmp_path,
):
    homonym = SOURCES["directory-1912"].text.splitlines()[0]
    documents = [
        document(
            "text",
            URSULA,
            [entity("e1", "Ursula"), entity("e2", "Angerring 57", "place")],
            [placed("residence", "e1", "e2", URSULA)],
        ),
        document(
            "media",
            homonym,
            [entity("e1", "Sebastian Löbig"), entity("e2", "Sebastian Löbig")],
            [],
        ),
    ]
    made = build(tmp_path / "run", *documents)
    shown = section(made)
    assert "| text |" in shown and "| media |" in shown and "| all |" in shown
    assert "shared_source_occurrence 2" in shown
    for name in ("Ursula", "Angerring", "Sebastian", "Löbig", "Brunnenring"):
        assert name not in shown
    for d in documents:
        assert d["sha1"][:8] not in shown
