"""The writer's refusals, counted by reason (#552).

Each claim the writer refuses whole counts under the one reason that
refused it, of REFUSAL_REASONS, in the order the writer looks for them:
a retired or reference property, the subject rules (ADR-0035 §3.4,
ADR-0039 §2), a text the instance cannot hold, an entity the side rule
blocks (ADR-0010; tests/unit/test_registration_flags.py), and an item
value that is its subject's item. The counts keep every reason, in that
order, and sum to the claims refused. Every example comes from the
fictional universe (docs/EXAMPLES.md): the 1957 divorce judgment.
"""

from __future__ import annotations

import pytest

from tests.fixtures.identity import write_mapped
from tests.unit.test_writer_notes_unions import (
    JUDGMENT_SHA1,
    LEOPOLD,
    NotedWiki,
    batch_of,
    source,
    wedding_date,
)
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import (
    REFUSAL_REASONS,
    TEXT_LIMIT,
    BatchWriter,
    WriteResult,
)

JUDGMENT = {"kind": "artifact", "sha1": JUDGMENT_SHA1}
# a text longer than the instance holds, made of the judgment's words
OVER_LIMIT = ("Landgericht Rekau " * (TEXT_LIMIT // 10))[: TEXT_LIMIT + 1]
# an item the ref names by its QID, the same as subject and as value
NAMED = {"kind": "qid", "qid": "Q900"}


def named_as(subject: dict, text: str) -> dict:
    return {
        "subject": subject,
        "property": "referred_to_as",
        "value": {
            "type": "monolingual",
            "value": {"text": text, "language": "de"},
        },
        "source": source(),
    }


# one claim for each reason the writer finds before the plan, and one for
# an item value that is its subject's item; the side rule's is
# test_registration_flags'
CLAIMS = {
    "retired_or_reference_property": {
        "subject": LEOPOLD,
        "property": "spouse",
        "value": {"type": "item", "value": NAMED},
        "source": source(),
    },
    # the judgment as the subject of a property that takes no artifact
    "subject_rule": named_as(JUDGMENT, "Landgericht Rekau"),
    "text": named_as(LEOPOLD, OVER_LIMIT),
    "self_reference": {
        "subject": NAMED,
        "property": "sibling",
        "value": {"type": "item", "value": NAMED},
        "source": source(),
    },
}


def counts(**given: int) -> dict[str, int]:
    return {reason: given.get(reason, 0) for reason in REFUSAL_REASONS}


class TestEachReason:
    @pytest.mark.parametrize(
        "reason", [r for r in REFUSAL_REASONS if r != "side_rule"]
    )
    def test_a_refusal_counts_under_its_reason(self, reason):
        batch = batch_of(CLAIMS[reason], wedding_date())
        result = write_mapped(BatchWriter(NotedWiki()), batch)
        assert result.refused == [claim_id(batch.claims[0])]
        assert result.refusal_reasons == [reason]
        assert result.refused_by_reason() == counts(**{reason: 1})
        assert result.written == 1

    def test_a_batch_counts_each_reason_in_its_order(self):
        # in reverse of the order: the counts keep the fixed order and
        # sum to the claims refused
        reasons = [r for r in reversed(REFUSAL_REASONS) if r != "side_rule"]
        batch = batch_of(
            *(CLAIMS[r] for r in reasons), wedding_date(), CLAIMS["text"]
        )
        result = write_mapped(BatchWriter(NotedWiki()), batch)
        by_reason = result.refused_by_reason()
        assert list(by_reason) == list(REFUSAL_REASONS)
        assert by_reason == counts(
            retired_or_reference_property=1,
            subject_rule=1,
            text=2,
            self_reference=1,
        )
        assert sum(by_reason.values()) == len(result.refused) == 5
        assert result.self_references == [claim_id(batch.claims[0])]
        assert result.written == 1

    def test_a_dry_run_counts_the_same(self):
        batch = batch_of(*CLAIMS.values(), wedding_date())
        wiki = NotedWiki()
        planned = write_mapped(BatchWriter(wiki, dry_run=True), batch)
        written = write_mapped(BatchWriter(wiki), batch)
        assert planned.refused_by_reason() == written.refused_by_reason()
        assert written.refused_by_reason() == counts(
            retired_or_reference_property=1,
            subject_rule=1,
            text=1,
            self_reference=1,
        )


class TestTheFirstReasonCounts:
    """A claim refused for two reasons counts under the first the writer
    looks for, once."""

    def test_a_reference_property_before_its_text(self):
        claim = {
            "subject": LEOPOLD,
            "property": "judgement_note",
            "value": {"type": "string", "value": OVER_LIMIT},
            "source": source(),
        }
        result = write_mapped(BatchWriter(NotedWiki()), batch_of(claim))
        assert result.refused_by_reason() == counts(
            retired_or_reference_property=1
        )

    def test_the_subject_rule_before_its_text(self):
        claim = named_as(JUDGMENT, OVER_LIMIT)
        result = write_mapped(BatchWriter(NotedWiki()), batch_of(claim))
        assert result.refused_by_reason() == counts(subject_rule=1)


def test_nothing_refused_counts_every_reason_zero():
    assert WriteResult(run_qid="Q9").refused_by_reason() == counts()
