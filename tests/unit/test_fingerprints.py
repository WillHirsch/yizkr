"""Mention fingerprints and cluster ids (#197, items 1 and 3; #105).

Every mention comes from the fictional universe (docs/EXAMPLES.md): the
GEDCOM fixture, adapted by the pipeline's own adapter, and batches built
around its sources' text.
"""

from __future__ import annotations

import hashlib
import random

from tests.fixtures import example_universe as u
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.fingerprints import (
    canonical_label,
    cluster_id,
    excerpt_span,
    mention_fingerprint,
    mentions_of,
)
from yizkr.pipeline.gedcom import adapt_gedcom

SOURCES = {source.key: source for source in u.SOURCES}
RUN = {
    "model_identifier": "scripted-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}


def sha1_of(key: str) -> str:
    return hashlib.sha1(SOURCES[key].text.encode()).hexdigest()


BIRTH = sha1_of("birth-extract-1938")
DIRECTORY = sha1_of("directory-1912")


def batch(entities=(), claims=()) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": list(entities),
            "claims": list(claims),
        }
    )


def ref(local_id: str, label=None, kind=None) -> dict:
    found = {"kind": "local", "local_id": local_id}
    if label:
        found["label"] = label
    if kind:
        found["instance_of"] = kind
    return found


def claim(sha1: str, subject: dict, prop: str, value: dict) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": {"artifact": {"kind": "artifact", "sha1": sha1}},
    }


def the_extract(first_id="e1", second_id="e2") -> ClaimBatch:
    """Gustav's birth register extract: the child and his father."""
    return batch(
        [
            {
                "id": first_id,
                "label": "Gustav Löbig",
                "kind": "human",
                "mention_lines": [1],
            },
            {
                "id": second_id,
                "label": "Sebastian Löbig",
                "kind": "human",
                "mention_lines": [1],
            },
        ],
        [
            claim(
                BIRTH,
                ref(first_id),
                "father",
                {"type": "item", "value": ref(second_id)},
            )
        ],
    )


def gedcom():
    raw = u.GEDCOM_FIXTURE.read_bytes()
    artifact = ArtifactRef.from_bytes(raw)
    return artifact.sha1, adapt_gedcom(raw.decode(), artifact).batch


# -- the fingerprint ----------------------------------------------------------


def test_each_component_is_part_of_the_fingerprint():
    base = mention_fingerprint(BIRTH, "Gustav Löbig", "human", [1])
    assert base == mention_fingerprint(
        BIRTH.upper(), "Gustav Löbig", "human", (1,), 0
    )
    others = {
        mention_fingerprint(DIRECTORY, "Gustav Löbig", "human", [1]),
        mention_fingerprint(BIRTH, "Gustave Lobig", "human", [1]),
        mention_fingerprint(BIRTH, "Gustav Löbig", "family", [1]),
        mention_fingerprint(BIRTH, "Gustav Löbig", "human", [1, 2]),
        mention_fingerprint(BIRTH, "Gustav Löbig", "human", []),
        mention_fingerprint(BIRTH, "Gustav Löbig", "human", [1], 1),
    }
    assert base not in others and len(others) == 6


def test_the_label_and_span_are_canonical():
    # a decomposed umlaut and stray whitespace are the same label
    decomposed = "Gustav  Löbig "
    assert canonical_label(decomposed) == "Gustav Löbig"
    assert canonical_label(None) == ""
    assert excerpt_span([3, 1, 3, 0, -2]) == (1, 3)
    assert mention_fingerprint(
        BIRTH, decomposed, "human", [3, 1, 3]
    ) == mention_fingerprint(BIRTH, "Gustav Löbig", "human", [1, 3])
    # case is the document's spelling, and is kept
    assert mention_fingerprint(
        BIRTH, "Gustav LÖBIG", "human", [1]
    ) != mention_fingerprint(BIRTH, "Gustav Löbig", "human", [1])
    # and so is a compatibility character (a ligature, as a scan may
    # give "Stupfield"): NFC keeps it, where NFKC would rewrite it
    ligature = "Stup\ufb01eld"
    assert canonical_label(ligature) == ligature
    assert mention_fingerprint(
        BIRTH, ligature, "place"
    ) != mention_fingerprint(BIRTH, "Stupfield", "place")


def test_a_manifest_numbered_otherwise_keeps_every_fingerprint():
    """A manifest extracted again may give its entities other ids: the
    fingerprints stay, since no id is part of one."""
    first = mentions_of(BIRTH, the_extract("e1", "e2"))
    again = mentions_of(BIRTH, the_extract("e07", "e01"))
    assert [m.fingerprint for m in first] == [m.fingerprint for m in again]
    assert [m.local_id for m in again] == ["e07", "e01"]
    # the same batch read back from its JSON gives the same fingerprints
    reread = ClaimBatch.model_validate_json(
        the_extract().model_dump_json(indent=2)
    )
    assert mentions_of(BIRTH, reread) == first


def test_a_gedcom_person_is_spanned_by_its_record_and_a_place_by_nothing():
    sha1, adapted = gedcom()
    mentions = {m.local_id: m for m in mentions_of(sha1, adapted)}
    # Eugen's INDI record starts on line 7 of the export
    assert mentions["i1"].span == (7,)
    assert mentions["i1"].label == "Eugen Löbig"
    # a place, an occupation and a media object have no line: an empty
    # span stands in, and their labels tell them apart
    for local_id in ("pl1", "oc1", "md1"):
        assert mentions[local_id].span == ()
        assert mentions[local_id].occurrence == 0
    # the two Ursula Löbigs of the export differ by their records' lines
    assert mentions["i10"].label == mentions["i16"].label
    assert mentions["i10"].fingerprint != mentions["i16"].fingerprint
    assert len({m.fingerprint for m in mentions.values()}) == len(mentions)


def test_equal_mentions_of_one_batch_are_told_apart_by_occurrence():
    """Two records of one manifest with the same label, kind and lines
    (a directory entry naming two people on one line) take occurrences 0
    and 1, in the manifest's order."""
    two = batch(
        [
            {
                "id": "e1",
                "label": "Ursula Löbig",
                "kind": "human",
                "mention_lines": [2],
            },
            {
                "id": "e2",
                "label": "Ursula Löbig",
                "kind": "human",
                "mention_lines": [2],
            },
        ]
    )
    first, second = mentions_of(DIRECTORY, two)
    assert (first.occurrence, second.occurrence) == (0, 1)
    assert first.fingerprint != second.fingerprint
    assert second.fingerprint == mention_fingerprint(
        DIRECTORY, "Ursula Löbig", "human", [2], 1
    )


def test_a_batch_without_a_manifest_takes_its_refs():
    """Each local id the claims name is a mention: its label is the first
    a ref gives, its kind the first instance_of, and its span empty."""
    chunked = batch(
        claims=[
            claim(
                BIRTH,
                ref("c1:e1"),
                "father",
                {
                    "type": "item",
                    "value": ref("c1:e2", "Sebastian Löbig", "human"),
                },
            ),
            claim(
                BIRTH,
                ref("c1:e1", "Gustav Löbig", "human"),
                "date_of_birth",
                {"type": "time", "year": 1919, "month": 1, "day": 7},
            ),
            claim(
                BIRTH,
                ref("c2:e1"),
                "date_of_birth",
                {"type": "time", "year": 1919},
            ),
            claim(
                BIRTH,
                ref("c3:e1"),
                "date_of_birth",
                {"type": "time", "year": 1919},
            ),
        ]
    )
    found = mentions_of(BIRTH, chunked)
    assert [(m.local_id, m.label, m.kind, m.span) for m in found] == [
        ("c1:e1", "Gustav Löbig", "human", ()),
        ("c1:e2", "Sebastian Löbig", "human", ()),
        ("c2:e1", "", "", ()),
        ("c3:e1", "", "", ()),
    ]
    # two refs that give no label or kind are told apart by occurrence
    assert [m.occurrence for m in found] == [0, 0, 0, 1]
    assert len({m.fingerprint for m in found}) == 4
    assert all(m.extract for m in found)


# -- the cluster id -----------------------------------------------------------


def test_a_cluster_id_hashes_its_members_in_any_order():
    fingerprints = [m.fingerprint for m in mentions_of(BIRTH, the_extract())]
    shuffled = list(reversed(fingerprints))
    assert cluster_id(fingerprints) == cluster_id(shuffled)
    assert cluster_id(fingerprints) == cluster_id(fingerprints * 2)
    assert cluster_id(fingerprints) != cluster_id(fingerprints[:1])
    # a cluster of one has an id of its own, apart from its member's
    assert cluster_id(fingerprints[:1]) != fingerprints[0]


def test_a_cluster_id_is_stable_when_found_in_another_order():
    sha1, adapted = gedcom()
    fingerprints = [m.fingerprint for m in mentions_of(sha1, adapted)]
    expected = cluster_id(fingerprints)
    generator = random.Random(1859)
    for _ in range(5):
        generator.shuffle(fingerprints)
        assert cluster_id(fingerprints) == expected
