"""Identity maps for the tests' writes (yizkr.pipeline.clustering).

The writer finds or mints the item of every local entity of a batch
through an identity map alone (yizkr.pipeline.writer, step 2), so every
test that writes a batch naming one gives it a map. `mapped` makes the
map of one or more batches, each the batch of the document its first
claim cites, as the clustering stage makes it of a run's documents; and
`write_mapped` writes a batch with the map of that batch alone, a corpus
of one document, unless the test gives it another.

Items are keyed by their cluster within one map: two batches written
with maps of their own give a cluster of one label, kind and source
descriptor the same key, and the second write finds the first's item.
A test of how a corpus's clusters meet on items gives every batch one
map.
"""

from __future__ import annotations

from yizkr.claims import ClaimBatch
from yizkr.pipeline.clustering import DOCUMENT, IdentityMap, Manifest

# the SHA-1 a batch without claims is mapped under: it names no mention a
# write resolves, so no write looks it up
_NO_CLAIMS = "0" * 40


def document_of(batch: ClaimBatch) -> str:
    """The SHA-1 of the document `batch` is the batch of: the artifact its
    first claim cites."""
    if not batch.claims:
        return _NO_CLAIMS
    return batch.claims[0].source.artifact.sha1


def mapped(*batches: ClaimBatch, source: str = DOCUMENT) -> IdentityMap:
    """The identity map of `batches`, each the batch of its document
    (document_of); of two batches of one document, the first is mapped."""
    return IdentityMap.of(
        [Manifest(document_of(batch), batch, source) for batch in batches]
    )


def write_mapped(writer, batch: ClaimBatch, **options):
    """`writer.write_batch(batch, ...)`, with the map of `batch` alone
    (mapped) unless `options` give one. Its instance may hold the writes of
    other maps (`one_map` false, unless `options` say otherwise): a test
    writes its batches each with its map onto one fake instance, and the
    live tests write onto the one instance every session writes to, their
    labels kept apart by their session marks. The writer's refusal of
    another map on an instance is tested on its own."""
    if options.get("identity") is None:
        options["identity"] = mapped(batch)
    options.setdefault("one_map", False)
    return writer.write_batch(batch, **options)


def joined(
    identity: IdentityMap, *entities: str | tuple[str, ...]
) -> IdentityMap:
    """`identity` with the mentions of each of `entities` in one cluster,
    whatever their kinds and documents, as the clustering stage merges
    the mentions of one entity: an entity is a label, or a tuple of
    labels, and its mentions those under any of them (their canonical
    labels, yizkr.pipeline.fingerprints). The map a test of how a
    corpus's mentions of one entity meet on its item writes each batch
    with."""
    from copy import deepcopy

    from yizkr.pipeline.clustering import CONFIDENT, identity_hash
    from yizkr.pipeline.fingerprints import canonical_label, cluster_id

    data = deepcopy(identity.data)
    mentions, clusters = data["mentions"], data["clusters"]
    for entity in entities:
        labels = {
            canonical_label(label)
            for label in ((entity,) if isinstance(entity, str) else entity)
        }
        members = sorted(
            fingerprint
            for fingerprint, mention in mentions.items()
            if mention["label"] in labels
        )
        if not members:
            raise ValueError(f"no mention of the map is under {entity!r}")
        for fingerprint in members:
            old = clusters.get(mentions[fingerprint]["cluster"])
            if old is not None:
                old["members"].remove(fingerprint)
                if not old["members"]:
                    del clusters[mentions[fingerprint]["cluster"]]
        cid = cluster_id(members)
        clusters[cid] = {
            "kind": mentions[members[0]]["kind"],
            "method": CONFIDENT,
            "members": members,
        }
        for fingerprint in members:
            mentions[fingerprint]["cluster"] = cid
            mentions[fingerprint]["method"] = CONFIDENT
    data["clusters"] = dict(sorted(clusters.items()))
    data["hash"] = identity_hash(mentions)
    return IdentityMap(data)


def corpus(
    *batches: ClaimBatch, entities: tuple[str | tuple[str, ...], ...] = ()
) -> IdentityMap:
    """The map of `batches` (mapped), with every mention under each label
    of `entities` one entity (joined)."""
    return joined(mapped(*batches), *entities)


def by_label(*batches: ClaimBatch) -> IdentityMap:
    """The map of `batches` in which every label is one entity, whatever
    its kinds and documents (joined): the map of a fixture whose batches
    name each of its entities by one label. Two batches of one document
    are mapped as one, their claims and records together."""
    from yizkr.pipeline.fingerprints import canonical_label, local_refs

    documents: dict[str, ClaimBatch] = {}
    for batch in batches:
        document = document_of(batch)
        if document not in documents:
            documents[document] = batch
            continue
        first = documents[document]
        records = {record.id: record for record in first.entities}
        for record in batch.entities:
            records.setdefault(record.id, record)
        documents[document] = first.model_copy(
            update={
                "claims": [*first.claims, *batch.claims],
                "entities": list(records.values()),
            }
        )
    labels = {
        canonical_label(record.label)
        for batch in batches
        for record in batch.entities
    } | {
        canonical_label(ref.label)
        for batch in batches
        for ref in local_refs(batch)
        if ref.label
    }
    return joined(mapped(*documents.values()), *sorted(labels))
