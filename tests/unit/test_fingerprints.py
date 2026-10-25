"""Mention fingerprints, claim fingerprints and cluster ids (#197; #105).

Every mention comes from the fictional universe (docs/EXAMPLES.md): the
GEDCOM fixture, adapted by the pipeline's own adapter, and batches built
around its sources' text.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ArtifactRef, ClaimBatch, UnknownValue
from yizkr.pipeline.clustering import Manifest, cluster_corpus, load_config
from yizkr.pipeline.fingerprints import (
    canonical_label,
    canonical_value,
    claim_fingerprint,
    claim_prints,
    cluster_id,
    excerpt_span,
    locator_text,
    mention_fingerprint,
    mention_map,
    mentions_of,
)
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.stages import canonical_json, sha256_text

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


def test_a_record_leaves_every_fingerprint_as_it_is():
    """A mention record read one way or another, or none, never changes
    a fingerprint: the label stays verbatim (ADR-0035 §8, #409)."""
    bare = the_extract()
    readings = [
        {"kind": "human", "given": [{"written": "Gustav"}]},
        {
            "kind": "human",
            "status": "description",
            "unsplit": ["Gustav Löbig"],
        },
    ]
    for structure in readings:
        entities = [entity.model_dump() for entity in bare.entities]
        entities[0]["structure"] = structure
        read = batch(entities, [c.model_dump() for c in bare.claims])
        assert read.entities[0].structure is not None
        assert mentions_of(BIRTH, read) == mentions_of(BIRTH, bare)


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


# -- the claim fingerprint ----------------------------------------------------


def birth_claim(
    sha1=BIRTH, subject="e1", value=None, qualifiers=(), **source
) -> dict:
    """Gustav's date of birth as the birth register extract gives it."""
    found = claim(
        sha1,
        ref(subject),
        "date_of_birth",
        value or {"type": "time", "year": 1919, "month": 1, "day": 7},
    )
    found["qualifiers"] = list(qualifiers)
    found["source"].update(source)
    return found


def gustav(claims, first_id="e1", sha1=BIRTH) -> ClaimBatch:
    """The extract's Gustav, with a record, and these claims of him."""
    return batch(
        [
            {
                "id": first_id,
                "label": "Gustav Löbig",
                "kind": "human",
                "mention_lines": [1],
                "structure": {
                    "kind": "human",
                    "given": [{"written": "Gustav"}],
                    "surnames": [{"written": "Löbig"}],
                },
            }
        ],
        claims,
    )


def print_of(claim_dict, first_id="e1") -> str:
    (found,) = claim_prints(BIRTH, gustav([claim_dict], first_id))
    return found.fingerprint


def bound(prop: str, year: int) -> dict:
    return {"property": prop, "value": {"type": "time", "year": year}}


def test_an_identical_claim_from_a_recomputed_stage_keeps_its_fingerprint():
    """A stage computed again may number its entities otherwise, order a
    claim's qualifiers otherwise, quote another excerpt, give another
    confidence or note, and write its batch out again: the claim keeps
    its fingerprint."""
    bounds = [bound("earliest_date", 1918), bound("latest_date", 1920)]
    first = birth_claim(
        value={"type": "time", "year": 1919},
        qualifiers=bounds,
        page_number="1",
        transcription_excerpt={"text": "geb. 7. Jan. 1919", "language": "de"},
    )
    again = birth_claim(
        subject="e07",
        value={"type": "time", "year": 1919, "precision": "year"},
        qualifiers=list(reversed(bounds)),
        page_number=" 1 ",
        transcription_excerpt={"text": "geboren 1919", "language": "de"},
    )
    again["confidence"] = 0.4
    again["note"] = {"category": "date_reading", "text": "the year read"}
    assert print_of(first) == print_of(again, "e07")
    reread = ClaimBatch.model_validate_json(
        gustav([first]).model_dump_json(indent=2)
    )
    assert claim_prints(BIRTH, reread) == claim_prints(BIRTH, gustav([first]))


def test_a_narrowed_or_widened_date_does_not_keep_its_fingerprint():
    day = print_of(birth_claim())
    stated = {
        "day": {"type": "time", "year": 1919, "month": 1, "day": 7},
        "another day": {"type": "time", "year": 1919, "month": 7, "day": 1},
        "the next day": {"type": "time", "year": 1919, "month": 1, "day": 8},
        "month": {"type": "time", "year": 1919, "month": 1},
        "another month": {"type": "time", "year": 1919, "month": 2},
        "year": {"type": "time", "year": 1919},
        "the decade's year": {"type": "time", "year": 1910},
        "decade": {"type": "time", "year": 1910, "precision": "decade"},
    }
    prints = {
        name: print_of(birth_claim(value=value))
        for name, value in stated.items()
    }
    assert prints["day"] == day
    assert len(set(prints.values())) == len(prints)
    # the bounds of a range are kept as given: dropped, narrowed or
    # widened, each is another assertion
    year = {"type": "time", "year": 1919}
    ranged = [
        [],
        [bound("earliest_date", 1918)],
        [bound("earliest_date", 1918), bound("latest_date", 1920)],
        [bound("earliest_date", 1919), bound("latest_date", 1920)],
        [bound("earliest_date", 1917), bound("latest_date", 1920)],
        # a bound given twice is kept twice
        [bound("earliest_date", 1918)] * 2,
    ]
    bounded = {
        print_of(birth_claim(value=year, qualifiers=found)) for found in ranged
    }
    assert len(bounded) == len(ranged)
    # an earliest bound is not a latest bound of the same year
    assert print_of(
        birth_claim(value=year, qualifiers=[bound("earliest_date", 1918)])
    ) != print_of(
        birth_claim(value=year, qualifiers=[bound("latest_date", 1918)])
    )
    # qualifiers of one property in either order are the same claim
    both = [bound("earliest_date", 1917), bound("earliest_date", 1918)]
    assert print_of(birth_claim(value=year, qualifiers=both)) == print_of(
        birth_claim(value=year, qualifiers=list(reversed(both)))
    )


def place_born(first_id: str, place_id: str) -> ClaimBatch:
    """Gustav's birth, with a qualifier naming the place he was born in
    by its local id."""
    born = birth_claim(
        subject=first_id,
        qualifiers=[
            {
                "property": "place_of_birth",
                "value": {"type": "item", "value": ref(place_id)},
            }
        ],
    )
    extract = gustav([born], first_id)
    entities = [entity.model_dump() for entity in extract.entities]
    entities.append(
        {
            "id": place_id,
            "label": "Flörburg",
            "kind": "place",
            "mention_lines": [2],
        }
    )
    return batch(entities, [born])


def test_a_qualifier_names_its_local_ref_by_its_mention():
    assert claim_prints(BIRTH, place_born("e1", "e2")) == claim_prints(
        BIRTH, place_born("e7", "e3")
    )


def test_each_component_is_part_of_the_claim_fingerprint():
    base = print_of(birth_claim(page_number="1"))
    others = [
        # another artifact asserts it
        birth_claim(DIRECTORY, page_number="1"),
        # on another page, a printed page beside it, or a region of it
        birth_claim(page_number="2"),
        birth_claim(page_number="1", printed_page_number="17"),
        # of another property
        {**birth_claim(page_number="1"), "property": "date_of_baptism"},
        # of another subject: a QID, or the document itself
        {
            **birth_claim(page_number="1"),
            "subject": {"kind": "qid", "qid": "Q42"},
        },
        {
            **birth_claim(page_number="1"),
            "subject": {"kind": "qid", "qid": "Q43"},
        },
        {
            **birth_claim(page_number="1"),
            "subject": {"kind": "artifact", "sha1": BIRTH},
        },
        {
            **birth_claim(page_number="1"),
            "subject": {"kind": "artifact", "sha1": DIRECTORY},
        },
    ]
    prints = {print_of(found) for found in others}
    assert base not in prints and len(prints) == len(others)


def test_every_number_of_a_region_is_kept_unrounded():
    region = {"x": 10, "y": 20, "width": 30, "height": 5}
    regions = [
        region,
        {**region, "x": 11},
        {**region, "y": 21},
        {**region, "width": 31},
        {**region, "height": 6},
        {**region, "x": 10.4},
    ]
    prints = {
        print_of(birth_claim(page_number="1", region=found))
        for found in regions
    }
    assert len(prints) == len(regions)
    assert print_of(birth_claim(page_number="1")) not in prints


def test_the_locator_is_on_one_line_as_the_writer_stores_it():
    assert locator_text(None) == ""
    assert locator_text(" 17 ") == "17"
    assert locator_text("3\n4") == "3 | 4"
    assert locator_text("3\r\n4\t5") == "3 | 4 5"
    # inner spaces are kept, as the writer keeps them
    assert locator_text("3  4") == "3  4"
    assert print_of(
        birth_claim(page_number="1", printed_page_number=" 17 ")
    ) == print_of(birth_claim(page_number="1", printed_page_number="17"))
    assert print_of(birth_claim(page_number="3\n4")) == print_of(
        birth_claim(page_number="3 | 4")
    )
    assert print_of(birth_claim(page_number="3\n4")) != print_of(
        birth_claim(page_number="3 4")
    )


def test_a_claim_print_holds_the_components_it_hashes():
    """#198 matches claims on fewer components than the whole print; the
    print is the SHA-256 of them, in this order, behind the tag "claim"
    that keeps it from ever equalling a mention's print or a cluster's
    id."""
    found = birth_claim(
        value={"type": "time", "year": 1919},
        qualifiers=[bound("earliest_date", 1918)],
        page_number="1",
        printed_page_number="17",
    )
    extract = gustav([found])
    (printed,) = claim_prints(BIRTH, extract)
    (gustav_mention,) = mentions_of(BIRTH, extract)
    assert printed.artifact == BIRTH
    assert printed.subject == ("mention", gustav_mention.fingerprint)
    assert printed.property == "date_of_birth"
    assert printed.value == ("time", 1919, None, None, "year", "gregorian")
    assert printed.qualifiers == (
        (
            "earliest_date",
            ("time", 1918, None, None, "year", "gregorian"),
        ),
    )
    assert printed.locator == ("1", "17", None)

    def thawed(value):
        if isinstance(value, tuple):
            return [thawed(item) for item in value]
        return value

    assert printed.fingerprint == sha256_text(
        canonical_json(
            [
                "claim",
                printed.artifact,
                thawed(printed.subject),
                printed.property,
                thawed(printed.value),
                thawed(printed.qualifiers),
                thawed(printed.locator),
            ]
        )
    )


def test_the_subject_is_its_mention_never_its_local_id():
    extract = the_extract()
    (father,) = claim_prints(BIRTH, extract)
    mentions = {m.local_id: m.fingerprint for m in mentions_of(BIRTH, extract)}
    assert mention_map(BIRTH, extract) == mentions
    assert claim_fingerprint(extract.claims[0], mentions) == father.fingerprint
    assert father.subject == ("mention", mentions["e1"])
    assert father.value == ("item", ("mention", mentions["e2"]))
    # renumbered, and with the ids swapped: the same claim
    assert claim_prints(BIRTH, the_extract("e2", "e1")) == [father]
    # the father and the son swapped is another claim
    swapped = batch(
        [entity.model_dump() for entity in extract.entities],
        [
            claim(
                BIRTH,
                ref("e2"),
                "father",
                {"type": "item", "value": ref("e1")},
            )
        ],
    )
    assert claim_prints(BIRTH, swapped)[0].fingerprint != father.fingerprint


def test_values_of_every_type_are_canonical():
    pairs = [
        # decomposed accents and an integral float read alike
        (
            {"type": "string", "value": "Hutmacher Lo\u0308big"},
            {"type": "string", "value": "Hutmacher Löbig"},
        ),
        (
            {
                "type": "monolingual",
                "value": {"text": "Lo\u0308big", "language": "de"},
            },
            {
                "type": "monolingual",
                "value": {"text": "Löbig", "language": "de"},
            },
        ),
        (
            {"type": "quantity", "amount": 2.0},
            {"type": "quantity", "amount": 2},
        ),
        (
            {"type": "coordinate", "latitude": 51.0, "longitude": 7.5},
            {"type": "coordinate", "latitude": 51, "longitude": 7.5},
        ),
    ]
    for first, second in pairs:
        assert print_of(birth_claim(value=first)) == print_of(
            birth_claim(value=second)
        )
    distinct = [
        {"type": "string", "value": "Hutmacher"},
        {"type": "string", "value": "hutmacher"},
        # whitespace in a value is kept, as the instance keeps it
        {"type": "string", "value": "Hutmacher Löbig"},
        {"type": "string", "value": "Hutmacher  Löbig"},
        {"type": "string", "value": "Hutmacher "},
        {"type": "external-id", "value": "12 34"},
        {"type": "external-id", "value": "12  34"},
        {
            "type": "monolingual",
            "value": {"text": "Hutmacher Löbig", "language": "de"},
        },
        {
            "type": "monolingual",
            "value": {"text": "Hutmacher  Löbig", "language": "de"},
        },
        {"type": "external-id", "value": "Hutmacher"},
        {
            "type": "monolingual",
            "value": {"text": "Hutmacher", "language": "de"},
        },
        {
            "type": "monolingual",
            "value": {"text": "Hutmacher", "language": "fy"},
        },
        {"type": "quantity", "amount": 2},
        {"type": "quantity", "amount": 2.5},
        {"type": "coordinate", "latitude": 51, "longitude": 7.5},
        {"type": "coordinate", "latitude": 52, "longitude": 7.5},
        {"type": "coordinate", "latitude": 51, "longitude": 7},
        {
            "type": "coordinate",
            "latitude": 51,
            "longitude": 7.5,
            "precision": 0.1,
        },
        {"type": "item", "value": {"kind": "vocab", "key": "human"}},
        {"type": "item", "value": {"kind": "vocab", "key": "place"}},
        {"type": "item", "value": {"kind": "qid", "qid": "Q42"}},
        {"type": "item", "value": {"kind": "qid", "qid": "Q43"}},
        # an unknown value is its tag alone (ADR-0040 §2.2): apart from
        # every date and every other value
        {"type": "unknown"},
        {"type": "time", "year": 1909},
    ]
    prints = {print_of(birth_claim(value=value)) for value in distinct}
    assert len(prints) == len(distinct)


def test_an_unknown_value_is_its_tag_alone():
    # the run diff matches claims through claim_prints, so a value that
    # carries nothing prints as its tag, and two of them print alike
    assert canonical_value(UnknownValue(), {}) == ["unknown"]
    late = {"type": "unknown"}
    assert print_of(birth_claim(value=late)) == print_of(
        birth_claim(value=dict(late))
    )


def test_a_local_ref_without_a_mention_is_refused():
    extract = the_extract()
    with pytest.raises(ValueError, match="'e9' names no mention"):
        claim_fingerprint(
            extract.claims[0].model_copy(
                update={
                    "subject": extract.claims[0].subject.model_copy(
                        update={"local_id": "e9"}
                    )
                }
            ),
            mention_map(BIRTH, extract),
        )


def test_a_local_id_two_mentions_give_is_refused():
    extract = the_extract("e1", "e1")
    with pytest.raises(ValueError, match="'e1' names two mentions"):
        mention_map(BIRTH, extract)


def test_a_qualifiers_ref_is_a_mention_without_a_manifest():
    """In a batch without a manifest, a ref only a qualifier names is a
    mention too, and keys the claim by its mention."""
    chunked = batch(
        claims=[
            {
                **claim(
                    BIRTH,
                    ref("c1:e1", "Gustav Löbig", "human"),
                    "date_of_birth",
                    {"type": "time", "year": 1919},
                ),
                "qualifiers": [
                    {
                        "property": "place_of_birth",
                        "value": {
                            "type": "item",
                            "value": ref("c1:e2", "Flörburg", "place"),
                        },
                    }
                ],
            }
        ]
    )
    (printed,) = claim_prints(BIRTH, chunked)
    mentions = mention_map(BIRTH, chunked)
    assert printed.qualifiers == (
        ("place_of_birth", ("item", ("mention", mentions["c1:e2"]))),
    )


def test_mentions_are_the_batchs_artifacts_and_the_source_the_claims():
    """The batch's artifact keys its mentions; a claim citing another
    artifact keeps that artifact as its own component."""
    cited = gustav([birth_claim(DIRECTORY)])
    (printed,) = claim_prints(BIRTH, cited)
    (mention,) = mentions_of(BIRTH, cited)
    assert printed.artifact == DIRECTORY
    assert printed.subject == ("mention", mention.fingerprint)
    (elsewhere,) = claim_prints(DIRECTORY, cited)
    assert elsewhere.subject != printed.subject
    assert elsewhere.fingerprint != printed.fingerprint


def test_a_batch_without_a_manifest_keys_its_claims_on_its_refs():
    chunked = batch(
        claims=[
            claim(
                BIRTH,
                ref("c1:e1", "Gustav Löbig", "human"),
                "date_of_birth",
                {"type": "time", "year": 1919, "month": 1, "day": 7},
            )
        ]
    )
    renumbered = batch(
        claims=[
            claim(
                BIRTH,
                ref("c4:e2", "Gustav Löbig", "human"),
                "date_of_birth",
                {"type": "time", "year": 1919, "month": 1, "day": 7},
            )
        ]
    )
    assert claim_prints(BIRTH, chunked) == claim_prints(BIRTH, renumbered)


# -- claims and clusters ------------------------------------------------------

THREE = ("birth-extract-1938", "directory-1912", "naturalisation-1952")


def gustavs() -> list[Manifest]:
    """Three documents, each giving Gustav born 7 January 1919."""
    return [
        Manifest(
            sha1_of(key),
            gustav([birth_claim(sha1_of(key))], sha1=sha1_of(key)),
        )
        for key in THREE
    ]


def every_claim_print(manifests) -> list[str]:
    return [
        found.fingerprint
        for manifest in manifests
        for found in claim_prints(manifest.sha1, manifest.batch)
    ]


def memberships(identity) -> set[frozenset[str]]:
    return {
        frozenset(entry["members"]) for entry in identity["clusters"].values()
    }


def test_claim_fingerprints_ignore_a_clustering_with_other_members():
    """The three Gustavs merge by default, and stay apart when their one
    key is a hub: the claims' fingerprints are the same either way, and
    none is a cluster id."""
    manifests = gustavs()
    before = every_claim_print(manifests)
    merged = cluster_corpus(manifests)
    apart = cluster_corpus(
        manifests, load_config(overrides={"blocking": {"max-key-mentions": 2}})
    )
    assert len(memberships(merged)) == 1
    assert len(memberships(apart)) == 3
    assert every_claim_print(manifests) == before
    assert len(set(before)) == 3
    ids = set(merged["clusters"]) | set(apart["clusters"])
    assert not ids & set(before)
    # each claim's subject is a mention the map holds, whatever cluster
    # it ends in
    for manifest in manifests:
        (found,) = claim_prints(manifest.sha1, manifest.batch)
        assert found.subject[1] in merged["mentions"]
        assert found.subject[1] in apart["mentions"]


def test_a_cluster_id_is_stable_when_clustering_is_recomputed_alike():
    manifests = gustavs()
    first = cluster_corpus(manifests)
    again = cluster_corpus(list(reversed(manifests)))
    assert set(first["clusters"]) == set(again["clusters"])
    for cluster, entry in first["clusters"].items():
        assert cluster_id(entry["members"]) == cluster
