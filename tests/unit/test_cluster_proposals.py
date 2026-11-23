"""An identity map's clusters become explicit per-mention choices.

The registry here is a stand-in that holds accepted assignments and the
bindings of their entities, which is all a derivation reads of it; the
fictional integration lane derives, reviews and applies choices against a
real stack (tests/fictional_integration/test_cluster_proposals.py).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures.semantic_acceptance import (
    homonym_batches,
    review_source,
    source_case,
)
from yizkr.claims import (
    ArtifactRef,
    EntityRecord,
    GivenName,
    PersonStructure,
    Surname,
)
from yizkr.core.identity_assignments import (
    AssignmentRevision,
    AssignmentSnapshot,
    RegistryBinding,
)
from yizkr.core.provenance import ProcessRecord, StageProvenance
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.cluster_proposals import (
    NOT_IN_MAP,
    OTHER_BATCH,
    ClusterProposalRefused,
    derive_cluster_choices,
)
from yizkr.pipeline.clustering import (
    DOCUMENT,
    IdentityMap,
    Manifest,
    cluster_corpus,
)
from yizkr.pipeline.cohort_additions import MAP_PROPOSAL
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import (
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.transcribe import TranscribedPage, Transcription

BIRTH = "birth-extract-1938"
NATURALISATION = "naturalisation-1952"


class Assignments:
    """A registry's accepted assignments, as a derivation reads them: each
    assigned mention's entity, the history's head, and each entity's
    verified binding."""

    def __init__(self, assigned=None):
        self.assigned = dict(assigned or {})

    def assignment_snapshot(self):
        if not self.assigned:
            return AssignmentSnapshot()
        head = hashlib.sha256(
            json.dumps(sorted(self.assigned.items())).encode()
        ).hexdigest()
        return AssignmentSnapshot(
            head=head,
            assignments={
                mention: AssignmentRevision(
                    mention_id=mention,
                    mention_revision=1,
                    entity_id=entity,
                    revision_id=hashlib.sha256(mention.encode()).hexdigest(),
                    decision_references=(),
                )
                for mention, entity in self.assigned.items()
            },
        )

    def binding(self, entity_id):
        number = 100 + sorted(set(self.assigned.values())).index(entity_id)
        return RegistryBinding(
            binding=EntityBinding(
                deployment_id="1" * 32,
                generation="2" * 32,
                entity_id=entity_id,
                intent_key="3" * 64,
                qid=f"Q{number}",
                revision=1,
            ),
            receipt_locator=f"fictional receipt {number}",
            receipt_sha256="4" * 64,
        )


def gustav(key):
    """Gustav's mention in one of his two birth sources, with a record that
    names him by given name and surname, so that clustering keys it."""
    source = review_source(key)
    (entity,) = source.batch.entities
    if key == BIRTH:
        entity.label, entity.aliases = "Gustav Löbig", ["Gustav"]
        given = GivenName(written="Gustav")
        surname = Surname(written="Löbig")
    else:
        given = GivenName(written="Gustave", standard="Gustav")
        surname = Surname(written="LOBIG")
    entity.structure = PersonStructure(given=[given], surnames=[surname])
    return source


def copy_of(source, note):
    """The same reading and batch as another artifact: a fictional copy
    with a note of its own on a line after the mention's."""
    text = f"{source.reading.text}\n{note}"
    artifact = ArtifactRef(sha1=hashlib.sha1(text.encode()).hexdigest())
    batch = source.batch.model_copy(deep=True)
    for claim in batch.claims:
        claim.source = claim.source.model_copy(update={"artifact": artifact})
    reading = Transcription.model_validate(
        source.reading.model_dump()
        | {
            "artifact": artifact.model_dump(),
            "text": text,
            "pages": [
                page.model_dump() | {"end": len(text)}
                for page in source.reading.pages
            ],
        }
    )
    return ReviewSource(artifact.sha1, batch, reading)


def ursula(key):
    """The daughter in the family tree, or the widow in the directory:
    two Ursula Löbigs, each on the line of her source that names her."""
    daughter, widow = homonym_batches()
    case = source_case(key)
    batch, line, alias = (
        (daughter, 3, "Ursula")
        if key == "family-tree-1984"
        else (widow, 2, "Löbig, Ursula")
    )
    (entity,) = batch.entities
    entity.mention_lines = [line]
    entity.aliases = [alias]
    reading = Transcription(
        artifact=ArtifactRef(sha1=case.sha1),
        text=case.text,
        language=case.language,
        engine="authored-fictional-text",
        pages=[
            TranscribedPage(
                page=1,
                start=0,
                end=len(case.text),
                status="transcribed",
                provenance={
                    "method": "text-layer",
                    "engine": "authored-fictional-text",
                },
            )
        ],
    )
    return ReviewSource(case.sha1, batch, reading)


def map_of(*sources):
    return cluster_corpus(
        [Manifest(s.artifact_sha1, s.batch, DOCUMENT) for s in sources]
    )


def mention_id(store, source):
    """The durable ID review gives the source's one mention."""
    (local_id,) = (entity.id for entity in source.batch.entities)
    prepared = prepare_review(store, ReviewRequest(sources=(source,)))
    return prepared.mentions[source.artifact_sha1, local_id].id


def derive(store, data, sources, registry=None, increment=None):
    return derive_cluster_choices(
        store,
        IdentityMap(data),
        sources,
        registry or Assignments(),
        increment=(
            [s.artifact_sha1 for s in sources]
            if increment is None
            else increment
        ),
    )


def choices(derived):
    """Each proposed choice: its operation, the source and local ID of its
    mention, and its target's entity ID."""
    return [
        (
            proposal.operation,
            proposal.mentions[0].key,
            proposal.target.entity_id if proposal.target else None,
        )
        for proposal in derived.request.proposals
    ]


def test_a_cluster_with_no_assigned_member_creates_one_entity(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    data = map_of(birth, naturalisation)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        derived = derive(store, data, (birth, naturalisation))
        reordered = derive(store, data, (naturalisation, birth))
        ids = {mention_id(store, s): s for s in (birth, naturalisation)}
    # Gustav's two birth sources share one cluster: one of his mentions
    # creates his entity and the other waits for it, whatever the order the
    # sources are given in.
    (cluster,) = {row["cluster"] for row in data["mentions"].values()}
    (created,) = derived.request.proposals
    assert created.operation == "new_entity"
    assert choices(reordered) == choices(derived)
    outcomes = derived.report.mentions
    assert {outcomes[m].outcome for m in ids} == {"new_entity", "waiting"}
    assert {outcomes[m].cluster for m in ids} == {cluster}
    (creator,) = (m for m in ids if outcomes[m].outcome == "new_entity")
    assert created.mentions[0].artifact_sha1 == ids[creator].artifact_sha1
    assert derived.report.counts["new_entity"] == 1
    assert derived.report.counts["waiting"] == 1
    # The map, the increment and the (empty) assignment history are its
    # origin; the map has no producer record, which the origin says.
    origin = dict(created.origin)
    assert origin.pop("limitations")
    assert origin == {
        "operation": MAP_PROPOSAL,
        "method": "confident",
        "identity_cluster": cluster,
        "identity_map_hash": data["hash"],
        "identity_map_inputs": data["inputs"],
        "increment": sorted(s.artifact_sha1 for s in ids.values()),
        "assignment_head": None,
    }
    assert created.evidence == {}
    assert derived.represented == frozenset()


def test_an_assigned_member_gives_its_entity_to_the_others(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    data = map_of(birth, naturalisation)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        first = derive(store, data, (birth, naturalisation))
        (created,) = first.request.proposals
        waiting = next(
            s
            for s in (birth, naturalisation)
            if s.artifact_sha1 != created.mentions[0].artifact_sha1
        )
        creator = next(s for s in (birth, naturalisation) if s is not waiting)
        # The creating mention's entity is applied; the proposals are
        # derived again with the same sources.
        registry = Assignments({mention_id(store, creator): "e" * 32})
        again = derive(store, data, (birth, naturalisation), registry)
        assert choices(again) == [
            ("assign", (waiting.artifact_sha1, "gustav"), "e" * 32)
        ]
        (assigned,) = again.request.proposals
        assert assigned.target == registry.binding("e" * 32).binding
        assert again.request.current_targets == (assigned.target,)
        assert assigned.origin["assignment_head"] == (
            registry.assignment_snapshot().head
        )
        assert again.represented == {"e" * 32}
        assert again.report.counts["assigned"] == 1
        assert again.report.counts["assign"] == 1

        # A later increment adds a source whose mention joins the cluster.
        third = copy_of(birth, "Fictional certified copy.")
        later = derive(
            store,
            map_of(birth, naturalisation, third),
            (birth, naturalisation, third),
            registry,
            increment=[third.artifact_sha1],
        )
        assert ("assign", (third.artifact_sha1, "gustav"), "e" * 32) in (
            choices(later)
        )
        assert all(
            proposal.origin["increment"] == [third.artifact_sha1]
            for proposal in later.request.proposals
        )


def test_the_two_ursula_loebigs_stay_apart(tmp_path):
    daughter, widow = ursula("family-tree-1984"), ursula("directory-1912")
    data = map_of(daughter, widow)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        registry = Assignments({mention_id(store, daughter): "d" * 32})
        derived = derive(
            store,
            data,
            (daughter, widow),
            registry,
            increment=[widow.artifact_sha1],
        )
        widow_id = mention_id(store, widow)
    assert choices(derived) == [
        ("new_entity", (widow.artifact_sha1, "widow"), None)
    ]
    (proposal,) = derived.request.proposals
    assert proposal.origin["method"] == "ambiguous"
    clusters = {row["cluster"] for row in data["mentions"].values()}
    assert len(clusters) == 2
    assert derived.report.mentions[widow_id].cluster in clusters


def test_a_cluster_assigned_to_two_entities_proposes_nothing(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    third = copy_of(birth, "Fictional certified copy.")
    daughter = ursula("family-tree-1984")
    sources = (birth, naturalisation, third, daughter)
    data = map_of(*sources)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        registry = Assignments(
            {
                mention_id(store, birth): "a" * 32,
                mention_id(store, naturalisation): "b" * 32,
                mention_id(store, daughter): "a" * 32,
            }
        )
        derived = derive(
            store,
            data,
            sources,
            registry,
            increment=[third.artifact_sha1],
        )
        third_id = mention_id(store, third)
    gustav_cluster = data["mentions"][
        next(
            fingerprint
            for fingerprint, row in data["mentions"].items()
            if row["document"] == third.artifact_sha1
        )
    ]["cluster"]
    daughter_cluster = next(
        row["cluster"]
        for row in data["mentions"].values()
        if row["document"] == daughter.artifact_sha1
    )
    assert derived.request.proposals == ()
    assert derived.report.mentions[third_id].outcome == "conflict"
    assert derived.report.conflicts == {gustav_cluster: ("a" * 32, "b" * 32)}
    assert derived.report.divided == {
        "a" * 32: tuple(sorted((gustav_cluster, daughter_cluster)))
    }
    assert derived.report.counts["conflicting_clusters"] == 1
    assert derived.report.counts["divided_entities"] == 1


def test_a_conflict_is_reported_beside_a_member_not_supplied(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    third = copy_of(birth, "Fictional certified copy.")
    fourth = copy_of(birth, "Fictional second certified copy.")
    sources = (birth, naturalisation, third)
    data = map_of(*sources, fourth)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        registry = Assignments(
            {
                mention_id(store, birth): "a" * 32,
                mention_id(store, naturalisation): "b" * 32,
            }
        )
        derived = derive(
            store,
            data,
            sources,
            registry,
            increment=[third.artifact_sha1],
        )
    (cluster,) = {row["cluster"] for row in data["mentions"].values()}
    # The supplied members already show the cluster's two entities; the
    # unsupplied copy holds it back as well.
    assert derived.request.proposals == ()
    assert derived.report.conflicts == {cluster: ("a" * 32, "b" * 32)}
    assert derived.report.unresolved == {cluster: (fourth.artifact_sha1,)}
    assert derived.report.counts["conflicting_clusters"] == 1


def test_a_source_the_map_does_not_hold_in_full_is_refused(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    with_father = gustav(NATURALISATION)
    with_father.batch.entities.append(
        EntityRecord(id="father", label="Sebastian Löbig", kind="human")
    )
    renamed = gustav(NATURALISATION)
    renamed.batch.entities[0].label = "Gustave Lobig"
    with ReviewStore(tmp_path / "review.sqlite") as store:
        # The map was made before the naturalisation certificate was read:
        # it is refused and the birth extract proceeds.
        alone = derive(
            store,
            map_of(birth),
            (birth, naturalisation),
            increment=[naturalisation.artifact_sha1],
        )
        assert alone.report.refused == {
            naturalisation.artifact_sha1: NOT_IN_MAP
        }
        assert choices(alone) == [
            ("new_entity", (birth.artifact_sha1, "gustav"), None)
        ]
        assert alone.request.sources == (birth,)

        # The map holds another reading of the certificate: one with a
        # mention the current batch lacks, or a mention of another label.
        for clustered, reason in (
            (with_father, OTHER_BATCH),
            (renamed, NOT_IN_MAP),
        ):
            other = derive(
                store, map_of(birth, clustered), (birth, naturalisation)
            )
            assert other.report.refused == {
                naturalisation.artifact_sha1: reason
            }

        # A cluster with a member whose source was refused, or not supplied,
        # gives no choice: that member may be assigned.
        both = map_of(birth, renamed)
        stale = derive(store, both, (birth, naturalisation))
        unsupplied = derive(store, both, (birth,))
        birth_id = mention_id(store, birth)
    for derived in (stale, unsupplied):
        assert derived.request.proposals == ()
        assert derived.report.mentions[birth_id].outcome == "unresolved"
        assert list(derived.report.unresolved.values()) == [
            (naturalisation.artifact_sha1,)
        ]
    assert unsupplied.report.unsupplied == (naturalisation.artifact_sha1,)
    assert unsupplied.report.refused == {}


def test_a_mention_the_assignment_path_cannot_admit_gets_no_choice(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    unread = ReviewSource(naturalisation.artifact_sha1, naturalisation.batch)
    added = gustav(BIRTH)
    added.batch.entities[0].structure.given.append(GivenName(written="Felix"))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        derived = derive(store, map_of(birth, unread), (birth, unread))
        unread_id = mention_id(store, unread)
        problem = derive(store, map_of(added, unread), (added, unread))
        added_id = mention_id(store, added)
    # The mention without a reading is not admitted, whichever member the
    # cluster would otherwise create its entity from.
    assert choices(derived) == [
        ("new_entity", (birth.artifact_sha1, "gustav"), None)
    ]
    assert derived.report.mentions[unread_id].outcome == "not_admitted"
    assert derived.report.mentions[unread_id].reasons == (
        "reading_unavailable",
    )
    assert problem.request.proposals == ()
    assert problem.report.mentions[added_id].reasons == (
        "structure: addition",
    )
    assert problem.report.counts["not_admitted"] == 2


def test_the_maps_producer_record_is_the_origin_provenance(tmp_path):
    birth = gustav(BIRTH)
    data = map_of(birth)
    producer = ProcessRecord(
        kind="identity-map",
        metadata={"identity_map_hash": data["hash"]},
        stages=(StageProvenance(stage="clustering", process="deterministic"),),
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        derived = derive(
            store,
            data | {"provenance": producer.model_dump(mode="json")},
            (birth,),
        )
        (proposal,) = derived.request.proposals
        assert proposal.origin["provenance"] == producer.model_dump(
            mode="json"
        )
        assert "limitations" not in proposal.origin
        other = producer.model_copy(
            update={"metadata": {"identity_map_hash": "f" * 64}}
        )
        for refused in (
            data | {"provenance": other.model_dump(mode="json")},
            data | {"hash": "f" * 64},
        ):
            with pytest.raises(ClusterProposalRefused):
                derive(store, refused, (birth,))


def test_the_increment_names_supplied_sources(tmp_path):
    birth, naturalisation = gustav(BIRTH), gustav(NATURALISATION)
    data = map_of(birth, naturalisation)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        for increment in ([naturalisation.artifact_sha1], []):
            with pytest.raises(ClusterProposalRefused):
                derive(store, data, (birth,), increment=increment)
        with pytest.raises(ClusterProposalRefused):
            derive(store, data, (birth, birth))
