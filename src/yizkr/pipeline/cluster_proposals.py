"""Identity proposals from an identity map, for durable identity review.

An identity map (``clustering``) puts each source mention in a cluster. This
module turns a map into explicit per-mention choices in the review store,
the proposal step of every increment, whatever its size and whatever the
graph holds: ``assign`` to the entity another member of the mention's
cluster is already assigned to, or ``new_entity`` for one member of a
cluster none of whose members is assigned. An empty graph is the case with
no assigned member. The map is each choice's origin, never its authority:
every choice goes through ``cohort_additions.prepare_cohort_additions``, so
it carries current candidate evidence, and a reviewer decides it as any
other. Deriving and preparing choices records no decision and writes
nothing to the graph.

**Correspondence.** A supplied source's current mentions meet the map's by
their fingerprints (``IdentityMap.clusters_of``), never by local ID or by
order. A source whose batch the map does not hold in full, every mention of
it and no other of its document, is refused with a reason, and the others
proceed. A cluster with a member in a document the map holds but whose
products were not supplied, or were refused, gives no choice, since an
assignment of that member cannot be seen.

**Choices.** Each mention gets at most one, and a mention with an accepted
assignment gets none. A cluster whose members are assigned to one entity
proposes that entity's verified binding for each other member; one whose
members are assigned to more than one entity proposes nothing and is a
conflict. A cluster with no assigned member proposes ``new_entity`` for
the member with the smallest fingerprint among those the assignment path
admits, so the creating mention, whose label the created entity takes, is
chosen from the map and the products alone, never from the order in which
sources are given or written. The other members wait for that entity, and
are proposed for it once it is applied and the proposals are derived
again. A mention the assignment path cannot admit (a limitation of its
source observation, such as no unique located occurrence, or a problem in
its mention record) gets no choice. No target is chosen by label or QID.
The map's clusters are taken as they are: the members of ambiguous and
conflicting entries, and a mention the map leaves unclustered, are each a
cluster of their own and are never merged here.

**Origin.** Each choice's origin names ``MAP_PROPOSAL`` and records the
map's method for the mention, its cluster, the map's hash and inputs key,
the map's producer record (``provenance``) or a limitation saying it has
none, the SHA-1s of the increment's sources, and the head of the
accepted-assignment history it was derived against (null for none).
Preparation keeps it as ``proposal_origin``, and an applied choice's
process record keeps it. Map fields are origin, not basis, so a choice
whose candidate evidence is unchanged is not sent back to review when its
cluster gains a member or the map is made again; the review store then
keeps the origin of the derivation that first proposed that basis.

**Report.** What each mention got, the clusters whose members are assigned
to more than one entity, and the entities whose assigned mentions the map
puts in more than one cluster: by mention, entity, cluster and source ID,
never by label. The last two are where present evidence disagrees with
decisions earlier increments fixed; they are neither proposals nor
corrections.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from yizkr.claims import ArtifactRef, container_problems, structure_problems
from yizkr.core.identity_migration import MigrationRefused
from yizkr.core.write_recovery import CreationRegistry, EntityBinding
from yizkr.pipeline.clustering import IdentityMap, UnmappedMention
from yizkr.pipeline.cohort_additions import (
    MAP_PROPOSAL,
    PreparedCohort,
    prepare_cohort_additions,
)
from yizkr.pipeline.fingerprints import mention_map
from yizkr.pipeline.identity_migration import validated_map
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_mentions import observe_mentions
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)

# what a mention of a supplied, corresponding source got: none, since it is
# assigned; a choice; none yet, as its cluster's creating mention is
# another; or none, since the assignment path cannot admit it, its cluster's
# members are assigned to more than one entity, or its cluster has a member
# whose products were not supplied or were refused
Outcome = Literal[
    "assigned",
    "assign",
    "new_entity",
    "waiting",
    "not_admitted",
    "conflict",
    "unresolved",
]
OUTCOMES: tuple[str, ...] = get_args(Outcome)
# why a supplied source was refused: products review cannot observe, a
# mention the map lacks, or a mention of its document the batch lacks
UNOBSERVABLE = "unobservable_products"
NOT_IN_MAP = "mentions_not_in_map"
OTHER_BATCH = "map_holds_another_batch"
NO_PRODUCER = (
    "identity map producer record is unavailable; no model, actor or time "
    "is inferred"
)


class ClusterProposalRefused(ValueError):
    """The map or the supplied sources cannot support any proposal."""


@dataclass(frozen=True)
class MentionOutcome:
    """What one mention got (OUTCOMES), in which of the map's clusters: the
    entity it is assigned or proposed to, and the reasons it was not
    admitted, as limitation codes."""

    outcome: Outcome
    cluster: str
    entity_id: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClusterReport:
    """A derivation's report, by ID alone: each mention's outcome by its
    durable mention ID; each cluster whose members are assigned to more
    than one entity, with those entities; each entity whose assigned
    mentions the map puts in more than one cluster, with those clusters;
    each refused source by SHA-1, with its reason; each cluster held back
    for a member whose products were not supplied or were refused, with
    those documents; and the map's documents not supplied at all."""

    mentions: dict[str, MentionOutcome]
    conflicts: dict[str, tuple[str, ...]]
    divided: dict[str, tuple[str, ...]]
    refused: dict[str, str]
    unresolved: dict[str, tuple[str, ...]]
    unsupplied: tuple[str, ...]

    @property
    def counts(self) -> dict[str, int]:
        """The mentions by outcome, then the conflicting clusters, divided
        entities, refused sources, held-back clusters and unsupplied
        documents."""
        counted = Counter(item.outcome for item in self.mentions.values())
        return {
            **{outcome: counted[outcome] for outcome in OUTCOMES},
            "conflicting_clusters": len(self.conflicts),
            "divided_entities": len(self.divided),
            "refused_sources": len(self.refused),
            "unresolved_clusters": len(self.unresolved),
            "unsupplied_documents": len(self.unsupplied),
        }


@dataclass(frozen=True)
class DerivedChoices:
    """The derived choices as a request for ``prepare_cohort_additions``,
    over the corresponding sources; the entities an assigned mention among
    them represents (its ``represented``); and the report."""

    request: ReviewRequest
    represented: frozenset[str]
    report: ClusterReport


@dataclass(frozen=True)
class ClusterProposals:
    """Derived choices prepared with current candidate evidence."""

    request: ReviewRequest
    prepared: PreparedCohort
    report: ClusterReport


def _admission(source: ReviewSource, local_id: str, observation) -> list[str]:
    """Why the assignment path cannot admit a mention, as codes: its source
    observation's limitations, and the reason each problem of its mention
    record or a place's container gives first (claims)."""
    reasons = list(observation.limitations)
    entity = next((e for e in source.batch.entities if e.id == local_id), None)
    if entity is not None:
        reasons += [
            "structure: " + problem.split(":", 1)[0]
            for problem in structure_problems(entity)
        ]
        reasons += [
            "container: " + problem.split(":", 1)[0]
            for problem in container_problems(entity)
        ]
    return list(dict.fromkeys(reasons))


def _origin(data: dict[str, Any], increment, head) -> dict[str, Any]:
    origin = {
        "operation": MAP_PROPOSAL,
        "identity_map_hash": data["hash"],
        "identity_map_inputs": data["inputs"],
        "increment": increment,
        "assignment_head": head,
    }
    if "provenance" in data:
        origin["provenance"] = data["provenance"]
    else:
        origin["limitations"] = [NO_PRODUCER]
    return origin


def derive_cluster_choices(
    store: ReviewStore,
    identity: IdentityMap,
    sources: Sequence[ReviewSource],
    registry: CreationRegistry,
    *,
    increment: Collection[str],
) -> DerivedChoices:
    """The choices the map gives the supplied sources' mentions (the module
    docstring), against the registry's accepted assignments.

    ``sources`` are the current products the map was computed from;
    ``increment`` names, by SHA-1, those that form the increment. Reads
    the registry's accepted assignments and verified bindings, and
    prepares review of the corresponding sources (``prepare_review``) for
    their durable mention IDs; stores none of the choices, captures no
    graph and writes nothing to it.
    """
    try:
        data = validated_map(identity, for_migration=False)
    except MigrationRefused as exc:
        raise ClusterProposalRefused(str(exc)) from exc
    supplied: dict[str, ReviewSource] = {}
    for source in sources:
        if source.artifact_sha1 in supplied:
            raise ClusterProposalRefused(
                "supply one current product per source"
            )
        supplied[source.artifact_sha1] = source
    named = sorted({ArtifactRef(sha1=sha1).sha1 for sha1 in increment})
    if not named or not set(named) <= supplied.keys():
        raise ClusterProposalRefused(
            "the increment must name supplied sources"
        )
    mapped = IdentityMap(data)
    documents: dict[str, set[str]] = defaultdict(set)
    for fingerprint, row in data["mentions"].items():
        documents[row["document"]].add(fingerprint)
    refused: dict[str, str] = {}
    located: dict[str, tuple[ReviewSource, str]] = {}
    corresponding = []
    for sha1, source in supplied.items():
        try:
            observe_mentions(sha1, source.batch, source.reading)
        except ValueError:
            refused[sha1] = UNOBSERVABLE
            continue
        try:
            mapped.clusters_of(source.batch, [sha1])
        except UnmappedMention:
            refused[sha1] = NOT_IN_MAP
            continue
        found = mention_map(sha1, source.batch)
        if set(found.values()) != documents.get(sha1, set()):
            refused[sha1] = OTHER_BATCH
            continue
        located.update(
            (fingerprint, (source, local_id))
            for local_id, fingerprint in found.items()
        )
        corresponding.append(source)
    resolved = {source.artifact_sha1 for source in corresponding}
    review = prepare_review(store, ReviewRequest(sources=tuple(corresponding)))
    mentions = {
        fingerprint: review.mentions[source.artifact_sha1, local_id]
        for fingerprint, (source, local_id) in located.items()
    }
    snapshot = registry.assignment_snapshot()
    base = _origin(data, named, snapshot.head)
    bindings: dict[str, EntityBinding] = {}

    def binding(entity_id: str) -> EntityBinding:
        if entity_id not in bindings:
            registered = registry.binding(entity_id)
            if registered is None:
                raise ClusterProposalRefused(
                    "an accepted entity has no verified binding"
                )
            bindings[entity_id] = registered.binding
        return bindings[entity_id]

    outcomes: dict[str, MentionOutcome] = {}
    proposals: list[ReviewProposal] = []
    conflicts: dict[str, tuple[str, ...]] = {}
    unresolved: dict[str, tuple[str, ...]] = {}
    clusters_of: dict[str, set[str]] = defaultdict(set)
    for cluster_id, cluster in sorted(data["clusters"].items()):
        members = sorted(m for m in cluster["members"] if m in mentions)
        if not members:
            continue
        assigned = {
            member: snapshot.assignments[mentions[member].id].entity_id
            for member in members
            if mentions[member].id in snapshot.assignments
        }
        for member, entity_id in assigned.items():
            clusters_of[entity_id].add(cluster_id)
            outcomes[mentions[member].id] = MentionOutcome(
                "assigned", cluster_id, entity_id
            )
        open_members = [m for m in members if m not in assigned]
        missing = sorted(
            {data["mentions"][m]["document"] for m in cluster["members"]}
            - resolved
        )
        entities = sorted(set(assigned.values()))
        held: Outcome | None = None
        if missing:
            unresolved[cluster_id] = tuple(missing)
            held = "unresolved"
        elif len(entities) > 1:
            conflicts[cluster_id] = tuple(entities)
            held = "conflict"
        admitted = []
        for member in open_members:
            mention = mentions[member]
            if held:
                outcomes[mention.id] = MentionOutcome(held, cluster_id)
                continue
            source, local_id = located[member]
            reasons = _admission(source, local_id, mention.observation)
            if reasons:
                outcomes[mention.id] = MentionOutcome(
                    "not_admitted", cluster_id, reasons=tuple(reasons)
                )
            else:
                admitted.append(member)
        origin = base | {"identity_cluster": cluster_id}
        for index, member in enumerate(admitted):
            mention = mentions[member]
            source, local_id = located[member]
            key = MentionKey(
                artifact_sha1=source.artifact_sha1, local_id=local_id
            )
            method = {"method": data["mentions"][member]["method"]}
            if entities:
                target = binding(entities[0])
                proposals.append(
                    ReviewProposal(
                        operation="assign",
                        mentions=(key,),
                        target=target,
                        origin=origin | method,
                    )
                )
                outcomes[mention.id] = MentionOutcome(
                    "assign", cluster_id, entities[0]
                )
            elif index == 0:
                proposals.append(
                    ReviewProposal(
                        operation="new_entity",
                        mentions=(key,),
                        origin=origin | method,
                    )
                )
                outcomes[mention.id] = MentionOutcome("new_entity", cluster_id)
            else:
                outcomes[mention.id] = MentionOutcome("waiting", cluster_id)
    request = ReviewRequest(
        sources=tuple(corresponding),
        proposals=tuple(
            sorted(proposals, key=lambda proposal: proposal.mentions[0].key)
        ),
        current_targets=tuple(bindings[key] for key in sorted(bindings)),
    )
    report = ClusterReport(
        mentions=dict(sorted(outcomes.items())),
        conflicts=conflicts,
        divided={
            entity_id: tuple(sorted(clusters))
            for entity_id, clusters in sorted(clusters_of.items())
            if len(clusters) > 1
        },
        refused=dict(sorted(refused.items())),
        unresolved=unresolved,
        unsupplied=tuple(sorted(documents.keys() - supplied.keys())),
    )
    return DerivedChoices(request, frozenset(clusters_of), report)


def prepare_cluster_proposals(
    store: ReviewStore,
    identity: IdentityMap,
    sources: Sequence[ReviewSource],
    registry: CreationRegistry,
    *,
    increment: Collection[str],
) -> ClusterProposals:
    """Derive the map's choices (derive_cluster_choices) and prepare them
    with current candidate evidence, each candidate marked with whether an
    assigned mention among the corresponding sources represents it.

    Needs a deployment generation for the graph capture, as any candidate
    preparation does; on an instance with one and no assignment, every
    cluster proposes a new entity. Records no decision. Apply approved
    choices with ``apply_cohort_additions`` and the returned request, one
    creation for each protected call.
    """
    derived = derive_cluster_choices(
        store, identity, sources, registry, increment=increment
    )
    prepared = prepare_cohort_additions(
        store, derived.request, registry, represented=derived.represented
    )
    return ClusterProposals(derived.request, prepared, derived.report)
