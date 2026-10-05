"""The resolve step finds the item the writer reuses (#476).

`resolve` matches each local entity by the label and the side rules the
writer finds its item by (yizkr.pipeline.writer, step 2): the manifest
record's label where there is a record, and the `[not an artifact]`
tagged label where an artifact holds the plain one (ADR-0010). So the
review sheet shows the QID the write will reuse, and NEW only where the
write makes an item. Each case compares `resolve` with a BatchWriter dry
run of the unresolved batch on the same in-memory wiki.

Fictional examples only (docs/EXAMPLES.md): the Löbig family's GEDCOM
export and the 1890s studio portrait.
"""

from __future__ import annotations

import pytest

from tests.unit.test_registrar import (
    PORTRAIT,
    RESCAN,
    label_of,
    scan,
    strings,
)
from tests.unit.test_registration_flags import (
    TWO_PEOPLE_GEDCOM,
    UNKNOWN_MARK,
    CountingWiki,
    WritingWiki,
    family_gedcom,
    naming,
    needs_unknown_mark,
)
from yizkr.claims import (
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    QidRef,
)
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.writer import ENTITY_TAG, BatchWriter, _term_text

SEBASTIAN = "Sebastian Löbig"
HILDEGARD = "Hildegard Löbig"


def person(wiki, label: str) -> str:
    return wiki.item(label, ("instance_of", WritingWiki.ITEMS["human"], None))


def resolved(wiki, batch: ClaimBatch) -> ClaimBatch:
    return resolve_batch(batch, resolvable_labels(wiki, batch))


def as_resolved(wiki, batch: ClaimBatch) -> dict[str, str]:
    """Local id -> what resolve makes of each entity's refs: the QID it
    resolves to, or "NEW"."""
    out = resolved(wiki, batch)
    seen: dict[str, str] = {}
    for before, after in zip(batch.claims, out.claims, strict=True):
        pairs = [(before.subject, after.subject)]
        if isinstance(before.value, ItemValue):
            pairs.append((before.value.value, after.value.value))
        for old, new in pairs:
            if isinstance(old, LocalRef):
                seen.setdefault(
                    old.local_id,
                    new.qid if isinstance(new, QidRef) else "NEW",
                )
    return seen


def as_written(wiki, batch: ClaimBatch) -> dict[str, str]:
    """Local id -> the item a dry run of the unresolved batch reuses for
    each entity, or "NEW" for one it would make."""
    dry = BatchWriter(wiki, dry_run=True).write_batch(batch)
    return {
        local_id: "NEW" if qid.startswith("(new:") else qid
        for local_id, qid in dry.created_items.items()
    }


def people(batch: ClaimBatch, outcome: dict[str, str]) -> dict[str, str]:
    """`outcome` for the batch's people alone (the media objects are on
    the artifact side, which resolve never resolves)."""
    kinds = {record.id: record.kind for record in batch.entities}
    return {k: v for k, v in outcome.items() if kinds.get(k) == "human"}


class TestAGedcomRefWithNoLabelOfItsOwn:
    def test_the_gedcom_ref_carries_no_label(self):
        """The case the tests below stand on: the person's name is on its
        manifest record, not on its refs."""
        batch = family_gedcom(WritingWiki())
        refs = [
            ref
            for claim in batch.claims
            for ref in (claim.subject, claim.value.value)
            if isinstance(ref, LocalRef) and ref.local_id == "i1"
        ]
        assert refs
        assert all(not ref.label for ref in refs)
        (record,) = [r for r in batch.entities if r.id == "i1"]
        assert record.label == SEBASTIAN

    def test_a_person_on_the_wiki_resolves_to_the_item_written(self):
        wiki = WritingWiki()
        sebastian = person(wiki, SEBASTIAN)
        batch = family_gedcom(wiki)

        assert as_resolved(wiki, batch)["i1"] == sebastian
        assert as_written(wiki, batch)["i1"] == sebastian

    def test_two_people_each_resolve_as_written(self):
        wiki = WritingWiki()
        hildegard = person(wiki, HILDEGARD)
        batch = family_gedcom(wiki, TWO_PEOPLE_GEDCOM)

        got = people(batch, as_resolved(wiki, batch))
        assert got == {"i1": "NEW", "i2": hildegard}
        assert got == people(batch, as_written(wiki, batch))

    def test_the_review_sheet_shows_the_qid(self, tmp_path):
        """The curator sees the item the write reuses, not NEW:i1."""
        wiki = WritingWiki()
        sebastian = person(wiki, SEBASTIAN)
        batch = family_gedcom(wiki)
        path = tmp_path / "review.csv"

        export_review_csv(resolved(wiki, batch), path)

        sheet = path.read_text()
        assert sebastian in sheet
        assert "NEW:i1" not in sheet

    def test_the_records_label_counts_over_the_refs_own(self):
        """A ref whose own label differs from its record's resolves by
        the record's, as the writer finds its item by it."""
        wiki = WritingWiki()
        sebastian = person(wiki, SEBASTIAN)
        person(wiki, HILDEGARD)
        given = family_gedcom(wiki).model_dump(mode="json")
        for claim in given["claims"]:
            for ref in (claim["subject"], claim["value"]["value"]):
                if ref.get("local_id") == "i1":
                    ref["label"] = HILDEGARD
        batch = ClaimBatch.model_validate(given)

        assert as_resolved(wiki, batch)["i1"] == sebastian
        assert as_written(wiki, batch)["i1"] == sebastian


class TestAPersonUnderAnArtifactsLabel:
    def artifact(self, wiki, tmp_path, label=SEBASTIAN, data=PORTRAIT):
        return (
            Registrar(wiki)
            .register_artifact(
                scan(tmp_path, data, name=f"{len(wiki.entities)}.png"),
                "photograph",
                label,
            )
            .artifact_qid
        )

    def test_resolves_to_the_tagged_item(self, tmp_path):
        wiki = WritingWiki()
        self.artifact(wiki, tmp_path)
        tagged = person(wiki, SEBASTIAN + ENTITY_TAG)
        batch = family_gedcom(wiki)

        assert as_resolved(wiki, batch)["i1"] == tagged
        assert as_written(wiki, batch)["i1"] == tagged

    def test_stays_new_while_no_tagged_item_exists(self, tmp_path):
        wiki = WritingWiki()
        self.artifact(wiki, tmp_path)
        batch = family_gedcom(wiki)

        assert as_resolved(wiki, batch)["i1"] == "NEW"
        assert as_written(wiki, batch)["i1"] == "NEW"

    def test_a_tagged_item_alone_is_not_reused(self):
        """No artifact holds the plain label: the writer makes the person
        an item under it, so a tagged item is not the person's."""
        wiki = WritingWiki()
        person(wiki, SEBASTIAN + ENTITY_TAG)
        batch = family_gedcom(wiki)

        assert as_resolved(wiki, batch)["i1"] == "NEW"
        assert as_written(wiki, batch)["i1"] == "NEW"

    def test_an_artifact_under_the_tagged_label_too_stays_local(
        self, tmp_path
    ):
        """Artifacts hold both labels: the writer refuses the person, and
        resolve gives it no item."""
        wiki = WritingWiki()
        self.artifact(wiki, tmp_path)
        self.artifact(wiki, tmp_path, SEBASTIAN + ENTITY_TAG, RESCAN)
        batch = family_gedcom(wiki)

        assert as_resolved(wiki, batch)["i1"] == "NEW"
        dry = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert "i1" not in dry.created_items
        assert dry.refused

    def test_a_tagged_items_statements_are_read_with_the_plain_ones(
        self, tmp_path
    ):
        """The labels are read once, and the statements of the items
        under both labels in one fetch."""
        wiki = CountingWiki()
        artifact = self.artifact(wiki, tmp_path)
        tagged = person(wiki, SEBASTIAN + ENTITY_TAG)
        batch = family_gedcom(wiki)
        wiki.forget()

        assert resolvable_labels(wiki, batch) == {
            SEBASTIAN: tagged,
            SEBASTIAN + ENTITY_TAG: tagged,
        }
        assert wiki.label_reads == ["label_index"]
        assert wiki.fetches == 1
        assert sorted(wiki.claims_of) == sorted([artifact, tagged])

    def test_writing_the_resolved_batch_gives_the_item_what_the_ref_would(
        self, tmp_path
    ):
        """The record reaches the tagged item through its QID as through
        its local ref: its own label then an alias, and its kind."""

        def instance():
            wiki = WritingWiki()
            self.artifact(wiki, tmp_path)
            return wiki, wiki.item(SEBASTIAN + ENTITY_TAG)

        wiki, tagged = instance()
        other, same = instance()
        assert same == tagged
        batch = family_gedcom(wiki)
        family_gedcom(other)

        BatchWriter(wiki).write_batch(resolved(wiki, batch))
        BatchWriter(other).write_batch(batch)

        for instance in (wiki, other):
            assert label_of(instance, tagged) == SEBASTIAN + ENTITY_TAG
            aliases = instance.entities[tagged]["aliases"].get("en", [])
            assert [alias["value"] for alias in aliases] == [SEBASTIAN]
            assert strings(instance, tagged, "instance_of") == [
                WritingWiki.ITEMS["human"]
            ]

    def test_a_record_pairs_with_a_tagged_item_only_where_placed_there(
        self,
    ):
        """No artifact holds the person's label: a claim naming the tagged
        item by QID gives that item nothing of the person's record, as the
        writer would place the person under the plain label."""
        wiki = WritingWiki()
        tagged = wiki.item(SEBASTIAN + ENTITY_TAG)
        given = family_gedcom(wiki).model_dump(mode="json")
        for claim in given["claims"]:
            for ref in (claim["subject"], claim["value"]["value"]):
                if ref.get("local_id") == "i1":
                    ref.clear()
                    ref.update(kind="qid", qid=tagged)

        BatchWriter(wiki).write_batch(ClaimBatch.model_validate(given))

        assert wiki.entities[tagged]["aliases"].get("en", []) == []
        assert strings(wiki, tagged, "instance_of") == []

    def test_the_records_alias_and_type_hint_reach_the_tagged_item(
        self, tmp_path
    ):
        """Through the QID resolve gives, the record's own label and its
        aliases reach the item, as the unresolved write gives them, and its
        type hint is written."""

        def instance():
            wiki = WritingWiki()
            self.artifact(wiki, tmp_path)
            return wiki, wiki.item(SEBASTIAN + ENTITY_TAG)

        def batch(wiki):
            given = family_gedcom(wiki).model_dump(mode="json")
            for record in given["entities"]:
                if record["id"] == "i1":
                    record.update(aliases=["S. Löbig"], type_hint="hatter")
            return ClaimBatch.model_validate(given)

        wiki, tagged = instance()
        other, same = instance()
        assert same == tagged
        BatchWriter(wiki).write_batch(resolved(wiki, batch(wiki)))
        BatchWriter(other).write_batch(batch(other))

        for written in (wiki, other):
            aliases = written.entities[tagged]["aliases"].get("en", [])
            assert sorted(alias["value"] for alias in aliases) == sorted(
                [SEBASTIAN, "S. Löbig"]
            )
            hints = written.entities[tagged]["claims"].get(
                WritingWiki.PROPERTIES["type_hint"], []
            )
            assert [h["mainsnak"]["datavalue"]["value"] for h in hints] == [
                "hatter"
            ]


def line_range(wiki, *refs: dict):
    """A batch without a manifest, as the line-range tier gives one: the
    export's claim once for each of `refs`, as its subject, its export
    registered on `wiki`."""
    family_gedcom(wiki)
    export = wiki.find_item_by_label("family-tree-export.ged")
    given = naming(*["x"] * len(refs)).model_dump(mode="json")
    for claim, ref in zip(given["claims"], refs, strict=True):
        claim.update(
            subject=ref,
            property="image",
            value={"type": "item", "value": {"kind": "qid", "qid": export}},
            qualifiers=[],
        )
    given["entities"] = []
    return ClaimBatch.model_validate(given)


class PlaceWiki(WritingWiki):
    """The in-memory wiki with a place class in its vocabulary."""

    ITEMS = {**WritingWiki.ITEMS, "place": "Q10"}


class TestARefWithoutARecordUnderAnArtifactsLabel:
    """Sebastian named by a ref with no manifest record, his name held by
    a registered scan and a bare item under his tagged label."""

    def instance(self, tmp_path):
        wiki = PlaceWiki()
        Registrar(wiki).register_artifact(
            scan(tmp_path, PORTRAIT), "photograph", SEBASTIAN
        )
        tagged = wiki.item(SEBASTIAN + ENTITY_TAG)
        return wiki, tagged

    @pytest.mark.parametrize("kind", ["human", "place"])
    def test_resolves_and_gives_the_item_what_the_ref_would(
        self, tmp_path, kind
    ):
        ref = {
            "kind": "local",
            "local_id": "e0",
            "label": SEBASTIAN,
            "instance_of": kind,
        }
        wiki, tagged = self.instance(tmp_path)
        other, same = self.instance(tmp_path)
        assert same == tagged
        batch = line_range(wiki, ref)

        assert as_resolved(wiki, batch)["e0"] == tagged
        assert as_written(wiki, batch)["e0"] == tagged
        out = resolved(wiki, batch)
        assert out.entities == [
            EntityRecord(id="e0", label=SEBASTIAN, kind=kind)
        ]
        BatchWriter(wiki).write_batch(out)
        BatchWriter(other).write_batch(line_range(other, ref))

        for instance in (wiki, other):
            aliases = instance.entities[tagged]["aliases"].get("en", [])
            assert [alias["value"] for alias in aliases] == [SEBASTIAN]
            assert strings(instance, tagged, "instance_of") == [
                PlaceWiki.ITEMS[kind]
            ]

    def test_one_of_a_batch_citing_two_artifacts_stays_local(self, tmp_path):
        """Two documents' claims name e0: a record would cite its kind to
        the first claim's artifact alone, so the ref stays local, and the
        writer finds the tagged item itself and cites each."""
        from yizkr.claims import ArtifactRef

        wiki, tagged = self.instance(tmp_path)
        ref = {
            "kind": "local",
            "local_id": "e0",
            "label": SEBASTIAN,
            "instance_of": "human",
        }
        given = line_range(wiki, ref, ref).model_dump(mode="json")
        letter = ArtifactRef.from_bytes(b"synthetic scan of a 1952 letter")
        wiki.item(
            "letter-to-sipkje-1952",
            ("instance_of", WritingWiki.ITEMS["photograph"], None),
            ("media_sha1", letter.sha1, None),
        )
        given["claims"][1]["source"]["artifact"] = letter.model_dump(
            mode="json"
        )
        batch = ClaimBatch.model_validate(given)

        assert as_resolved(wiki, batch)["e0"] == "NEW"
        assert resolved(wiki, batch).entities == []
        assert as_written(wiki, batch)["e0"] == tagged

    def test_one_with_no_kind_stays_local(self, tmp_path):
        """No record can be made without a kind: the ref stays local, and
        the writer finds the tagged item itself."""
        wiki, tagged = self.instance(tmp_path)
        batch = line_range(
            wiki, {"kind": "local", "local_id": "e0", "label": SEBASTIAN}
        )

        assert as_resolved(wiki, batch)["e0"] == "NEW"
        assert resolved(wiki, batch).entities == []
        assert as_written(wiki, batch)["e0"] == tagged


class TestAsTheWriterReadsAnEntity:
    @needs_unknown_mark
    @pytest.mark.parametrize("unknown_at", ["subject", "value", "qualifier"])
    def test_a_ref_in_a_claim_refused_before_the_plan_is_passed_over(
        self, unknown_at
    ):
        """p1's first ref is in a claim that also names u, as its subject,
        its value or a qualifier's, whose label holds a character Python's
        Unicode tables do not know: the writer refuses that claim before
        its plan and reads p1 by its next ref, and so does resolve."""
        wiki = WritingWiki()
        sebastian = person(wiki, SEBASTIAN)
        person(wiki, HILDEGARD)
        family_gedcom(wiki)
        export = wiki.find_item_by_label("family-tree-export.ged")
        u = {
            "kind": "local",
            "local_id": "u",
            "label": "Gustav" + UNKNOWN_MARK,
        }
        p1 = {"kind": "local", "local_id": "p1", "label": HILDEGARD}
        given = naming("x", "x").model_dump(mode="json")
        first, second = given["claims"]
        first["property"] = "relative"
        first["qualifiers"] = []
        if unknown_at == "subject":
            first["subject"] = u
            first["value"] = {"type": "item", "value": p1}
        else:
            first["subject"] = p1
            if unknown_at == "value":
                first["value"] = {"type": "item", "value": u}
            else:
                first["value"] = {
                    "type": "item",
                    "value": {"kind": "qid", "qid": export},
                }
                first["qualifiers"] = [
                    {
                        "property": "participant",
                        "value": {"type": "item", "value": u},
                    }
                ]
        second["subject"] = dict(p1, label=SEBASTIAN)
        batch = ClaimBatch.model_validate(given)

        assert resolvable_labels(wiki, batch) == {SEBASTIAN: sebastian}
        out = resolved(wiki, batch)
        assert out.claims[1].subject == QidRef(qid=sebastian)
        dry = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert dry.created_items["p1"] == sebastian

    @needs_unknown_mark
    def test_an_equivalence_record_with_an_unknown_character_refuses_nothing(
        self,
    ):
        """The writer finds an approved equivalence's item by its QID, so
        its record's label refuses no claim: p1's first ref, beside it,
        gives p1 its label, in resolve as in the writer."""
        wiki = WritingWiki()
        person(wiki, SEBASTIAN)
        hildegard = person(wiki, HILDEGARD)
        gustav = person(wiki, "Gustav Löbig")
        family_gedcom(wiki)
        given = naming("x", "x").model_dump(mode="json")
        first, second = given["claims"]
        first["property"] = "relative"
        first["subject"] = {"kind": "local", "local_id": "q1"}
        first["value"] = {
            "type": "item",
            "value": {"kind": "local", "local_id": "p1", "label": HILDEGARD},
        }
        second["subject"] = {
            "kind": "local",
            "local_id": "p1",
            "label": SEBASTIAN,
        }
        given["entities"] += [
            {
                "id": "q1",
                "label": "Gustav" + UNKNOWN_MARK,
                "kind": "human",
                "qid": gustav,
            }
        ]
        batch = ClaimBatch.model_validate(given)

        assert as_resolved(wiki, batch)["p1"] == hildegard
        assert as_written(wiki, batch)["p1"] == hildegard

    @needs_unknown_mark
    def test_a_record_with_an_unknown_character_stays_new(self):
        label = SEBASTIAN + UNKNOWN_MARK
        wiki = WritingWiki()
        person(wiki, _term_text(label))
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "i1":
                record["label"] = label
        batch = ClaimBatch.model_validate(given)

        assert as_resolved(wiki, batch)["i1"] == "NEW"
        assert (
            "i1"
            not in BatchWriter(wiki, dry_run=True)
            .write_batch(batch)
            .created_items
        )

    @needs_unknown_mark
    def test_any_ref_with_an_unknown_character_keeps_the_entity_local(self):
        wiki = WritingWiki()
        person(wiki, HILDEGARD)
        batch = line_range(
            wiki,
            {"kind": "local", "local_id": "p1", "label": HILDEGARD},
            {
                "kind": "local",
                "local_id": "p1",
                "label": "Hildegard" + UNKNOWN_MARK,
            },
        )

        out = resolved(wiki, batch)
        assert all(isinstance(c.subject, LocalRef) for c in out.claims)
        dry = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert "p1" not in dry.created_items
        assert "p1: label" in dry.refused_terms

    def test_the_records_kind_counts_over_the_refs_own(self):
        """A photograph's record whose ref says human, under a person's
        name: on the artifact side by its record, as in the writer, so it
        never resolves to the person's item."""
        wiki = WritingWiki()
        hildegard = person(wiki, HILDEGARD)
        given = family_gedcom(wiki).model_dump(mode="json")
        for record in given["entities"]:
            if record["id"] == "i1":
                record.update(label=HILDEGARD, kind="photograph")
        for claim in given["claims"]:
            if claim["subject"].get("local_id") == "i1":
                claim["subject"]["instance_of"] = "human"
        batch = ClaimBatch.model_validate(given)

        assert as_resolved(wiki, batch)["i1"] == "NEW"
        dry = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert dry.created_items.get("i1") != hildegard
