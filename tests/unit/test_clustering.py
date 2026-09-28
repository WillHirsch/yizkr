"""Corpus clustering (#105): blocking, cannot-link, classification, the
identity map, and the stage's reuse of it.

Every document is one of the fictional universe's sources, cited by the
SHA-1 of its text, with a manifest an extractor could make of it; the
GEDCOM export is the fixture, adapted by the pipeline's own adapter
(docs/EXAMPLES.md). The universe has no parent and child of one name, so
that case uses placeholder names. No wiki, no model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ArtifactRef, ClaimBatch, TimeValue
from yizkr.pipeline import clustering
from yizkr.pipeline.clustering import (
    AMBIGUOUS,
    CONFIDENT,
    CONFLICTING,
    GEDCOM,
    HARD,
    IDENTITY_MAP,
    SINGLETON,
    SOFT,
    UNCLUSTERED,
    ClusteringConfigError,
    ClusteringInputError,
    Interval,
    Manifest,
    adjudication_calls,
    attention,
    blocked_pairs,
    cluster_corpus,
    date_interval,
    ensure_identity_map,
    identity_hash,
    load_config,
    run_stage,
)
from yizkr.pipeline.fingerprints import cluster_id
from yizkr.pipeline.gedcom import adapt_gedcom

SOURCES = {source.key: source for source in u.SOURCES}
RUN = {
    "model_identifier": "scripted-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}


# -- the fictional documents --------------------------------------------------


def sha1_of(key: str) -> str:
    """A source's artifact: the SHA-1 of its text."""
    return hashlib.sha1(SOURCES[key].text.encode()).hexdigest()


def local(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def item(local_id: str) -> dict:
    return {"type": "item", "value": local(local_id)}


def day(year: int, month: int | None = None, number: int | None = None):
    value = {"type": "time", "year": year}
    if month:
        value["month"] = month
    if number:
        value["day"] = number
    return value


def entity(local_id, label, kind="human", aliases=(), lines=(1,), **extra):
    return {
        "id": local_id,
        "label": label,
        "kind": kind,
        "aliases": list(aliases),
        "mention_lines": list(lines),
        **extra,
    }


@dataclass
class Doc:
    """A document of the test corpus: its source key, its manifest, and
    who in the universe each of its local ids is."""

    key: str
    manifest: Manifest
    truth: dict[str, str]

    @property
    def sha1(self) -> str:
        return self.manifest.sha1

    def fingerprint(self, local_id: str, identity: dict) -> str:
        (found,) = [
            fingerprint
            for fingerprint, entry in identity["mentions"].items()
            if entry["document"] == self.sha1 and entry["local_id"] == local_id
        ]
        return found


def document(key: str, entities, claims, truth, sha1=None) -> Doc:
    sha1 = sha1 or sha1_of(key)
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": list(entities),
            "claims": [
                {
                    "subject": local(subject),
                    "property": prop,
                    "value": value,
                    "qualifiers": [
                        {"property": name, "value": qualifier}
                        for name, qualifier in qualifiers
                    ],
                    "source": {"artifact": {"kind": "artifact", "sha1": sha1}},
                }
                for subject, prop, value, *qualifiers in claims
            ],
        }
    )
    return Doc(key, Manifest(sha1, batch), truth)


def marriage_certificate() -> Doc:
    return document(
        "marriage-cert-1905",
        [
            entity("e1", "Sebastian Löbig"),
            entity("e2", "Eugen Löbig"),
            entity("e3", "Cäcilie Tarle", aliases=["Cäcilie geborene Tarle"]),
            entity("e4", "Hildegard Laudler"),
            entity("e5", "Flörburg", "place"),
            entity("e6", "Fläckfeld", "place"),
            entity("e7", "Kreis Mockburg", "place"),
            # the issuing authority attests the record: boilerplate
            entity("e8", "Standesamt Flörburg", "organization", extract=False),
        ],
        [
            ("e1", "date_of_birth", day(1862, 2, 15)),
            ("e1", "place_of_birth", item("e5")),
            ("e1", "father", item("e2")),
            ("e1", "mother", item("e3")),
            ("e1", "spouse", item("e4")),
            ("e2", "spouse", item("e3")),
            ("e4", "date_of_birth", day(1876, 3, 19)),
            ("e4", "place_of_birth", item("e6")),
            ("e6", "part_of", item("e7")),
        ],
        {
            "e1": "sebastian-loebig",
            "e2": "eugen-loebig",
            "e3": "caecilie-loebig",
            "e4": "hildegard-loebig",
            "e5": "floerburg",
            "e6": "flaeckfeld",
            "e7": "kreis-mockburg",
            "e8": "floerburg-registry",
        },
    )


def birth_extract(*, dated: bool = True) -> Doc:
    claims = [
        ("e1", "father", item("e2")),
        ("e1", "mother", item("e3")),
        ("e1", "place_of_birth", item("e4")),
        ("e2", "residence", item("e5"), ("point_in_time", day(1919, 1, 8))),
        ("e2", "spouse", item("e3")),
    ]
    if dated:
        claims.append(("e1", "date_of_birth", day(1919, 1, 7)))
    return document(
        "birth-extract-1938",
        [
            entity("e1", "Gustav Löbig"),
            entity("e2", "Sebastian Löbig"),
            entity(
                "e3",
                "Hildegard Löbig",
                aliases=["Hildegard Löbig geborene Laudler"],
            ),
            entity("e4", "Flörburg", "place"),
            entity("e5", "Brunnenring 35", "place"),
            entity("e6", "Standesamt Flörburg", "organization", extract=False),
        ],
        claims,
        {
            "e1": "gustav-loebig",
            "e2": "sebastian-loebig",
            "e3": "hildegard-loebig",
            "e4": "floerburg",
            "e5": "brunnenring-35",
            "e6": "floerburg-registry",
        },
    )


def naturalisation() -> Doc:
    return document(
        "naturalisation-1952",
        [
            entity("e1", "Gustave Lobig", aliases=["Gustav Löbig"]),
            entity("e2", "8 Hawthorn Road, Stupfield, Victoria", "place"),
            entity("e3", "Floerburg", "place"),
            entity("e4", "RMS Flund", "vessel"),
            entity("e5", "Hatter", "occupation_class"),
        ],
        [
            ("e1", "date_of_birth", day(1919, 7, 1)),
            ("e1", "residence", item("e2"), ("point_in_time", day(1952))),
            ("e1", "place_of_birth", item("e3")),
            ("e1", "occupation", item("e5")),
        ],
        {
            "e1": "gustav-loebig",
            "e2": "hawthorn-road-8",
            "e3": "floerburg",
            "e4": "rms-flund",
            "e5": "hatter",
        },
    )


def family_tree(*, dated: bool = True) -> Doc:
    """Klara's tree states the parents' children from the parents' side:
    `child` claims, the mirror of the extract's `father` and `mother`."""
    claims = [
        ("e2", "child", item("e1")),
        ("e3", "child", item("e1")),
        ("e2", "child", item("e4")),
        ("e2", "spouse", item("e3")),
    ]
    if dated:
        claims += [
            ("e1", "date_of_birth", day(1911)),
            ("e1", "date_of_death", day(1941)),
        ]
    return document(
        "family-tree-1984",
        [
            entity("e1", "Ursula Löbig", lines=(3,)),
            entity("e2", "Sebastian Löbig", aliases=["Wastl"], lines=(2, 3)),
            entity("e3", "Hildegard Laudler", aliases=["Hilde"], lines=(2,)),
            entity("e4", "Gustav Löbig", aliases=["Gustl"], lines=(3,)),
        ],
        claims,
        {
            "e1": "ursula-loebig",
            "e2": "sebastian-loebig",
            "e3": "hildegard-loebig",
            "e4": "gustav-loebig",
        },
    )


def directory() -> Doc:
    return document(
        "directory-1912",
        [
            entity("e1", "Hutmacherei zum Blauen Hut", "organization"),
            entity("e2", "Sebastian Löbig"),
            entity("e3", "Brunnenring 35", "place"),
            entity(
                "e4",
                "Ursula Löbig",
                aliases=["Löbig, Ursula, Wwe."],
                lines=(2,),
            ),
            entity("e5", "Angerring 57", "place", lines=(2,)),
        ],
        [
            ("e4", "residence", item("e5"), ("point_in_time", day(1912))),
            ("e1", "location", item("e3"), ("point_in_time", day(1912))),
        ],
        {
            "e1": "zum-blauen-hut",
            "e2": "sebastian-loebig",
            "e3": "brunnenring-35",
            "e4": "ursula-loebig-angerring",
            "e5": "angerring-57",
        },
    )


def registration_card() -> Doc:
    return document(
        "registration-card-1945",
        [
            entity("e1", "Ida Löbig", aliases=["LÖBIG, Ida, née SÖRNE"]),
            entity("e2", "Leopold Löbig"),
            entity("e3", "Peiltal court prison", "place"),
            entity("e4", "Peiltal", "place"),
        ],
        [
            ("e2", "mother", item("e1")),
            ("e3", "part_of", item("e4")),
        ],
        {
            "e1": "ida-loebig",
            "e2": "leopold-loebig",
            "e3": "peiltal-court-prison",
            "e4": "peiltal",
        },
    )


def interview() -> Doc:
    return document(
        "interview-2002",
        [entity("e1", "Peiltal", "place")],
        [],
        {"e1": "peiltal"},
    )


def studio_photo() -> Doc:
    """The names pencilled on the portrait's back: one token each."""
    return document(
        "studio-photo-1890s",
        [
            entity("e1", "Muckel"),
            entity("e2", "Cilli"),
            entity("e3", "Wastl"),
            entity("e4", "Rahel"),
            entity("e5", "Felix"),
        ],
        [],
        {
            "e1": "eugen-loebig",
            "e2": "caecilie-loebig",
            "e3": "sebastian-loebig",
            "e4": "rahel-loebig",
            "e5": "felix-loebig",
        },
    )


# where the export's places and objects are in the universe
GEDCOM_TRUTH = {
    "Bad Flörburg, Hesse, Germany": "floerburg",
    "Flörburg, Hesse, Germany": "floerburg",
    "Friedhof Flörburg, Hesse, Germany": "floerburg-cemetery",
    "Kredfeld, Hesse, Germany": "kredfeld",
    "Brunnenring 35, Flörburg, Hesse, Germany": "brunnenring-35",
    "Fläckfeld, Kreis Mockburg, Hesse, Germany": "flaeckfeld",
    "Wasserallee 59, Flörburg, Hesse, Germany": "wasserallee-59",
    "Moshausen, Germany": "moshausen",
    "Stupfield, Victoria, Australia": "stupfield",
    "8 Hawthorn Road, Stupfield, Victoria, Australia": "hawthorn-road-8",
    "31 Juniper Street, Stupfield, Victoria, Australia": "juniper-street-31",
    "Angerring 57, Flörburg, Hesse, Germany": "angerring-57",
    "Master hatter": "master-hatter",
    "Hatter": "hatter",
    "Studio portrait of the Löbig family": "studio-photo-1890s",
}


def gedcom() -> Doc:
    raw = u.GEDCOM_FIXTURE.read_bytes()
    artifact = ArtifactRef.from_bytes(raw)
    batch = adapt_gedcom(raw.decode(), artifact).batch
    people = {
        person.gedcom_xref.strip("@").lower(): person.key
        for person in u.PEOPLE
        if person.gedcom_xref
    }
    truth = {
        record.id: people.get(record.id) or GEDCOM_TRUTH[record.label]
        for record in batch.entities
    }
    return Doc("gedcom", Manifest(artifact.sha1, batch, GEDCOM), truth)


def mention_of(docs, identity, key, local_id) -> str:
    (doc,) = [doc for doc in docs if doc.key == key]
    return doc.fingerprint(local_id, identity)


def clusters_by_truth(docs, identity) -> list[set[str]]:
    """The universe keys of each cluster's members, for every cluster of
    two or more."""
    truth = {
        doc.fingerprint(local_id, identity): key
        for doc in docs
        for local_id, key in doc.truth.items()
    }
    return [
        {truth[member] for member in entry["members"]}
        for entry in identity["clusters"].values()
        if len(entry["members"]) > 1
    ]


def merged(docs, identity) -> list[set[str]]:
    return sorted(
        (keys for keys in clusters_by_truth(docs, identity)),
        key=sorted,
    )


def assert_no_false_merge(docs, identity) -> None:
    """No cluster holds mentions of two entities of the universe."""
    for keys in clusters_by_truth(docs, identity):
        assert len(keys) == 1, keys


def identity_of(*docs, **overrides):
    config = load_config(overrides=overrides) if overrides else None
    return cluster_corpus([doc.manifest for doc in docs], config)


def pair(first: Doc, first_id: str, second: Doc, second_id: str):
    return frozenset({(first.sha1, first_id), (second.sha1, second_id)})


# -- blocking -----------------------------------------------------------------


class TestBlocking:
    def test_people_block_on_a_given_name_with_a_surname_never_a_token(
        self,
    ):
        extract, tree, book = birth_extract(), family_tree(), directory()
        pairs = blocked_pairs([extract.manifest, tree.manifest, book.manifest])
        # the same person by given name and surname
        assert pair(extract, "e2", tree, "e2") in pairs
        assert pair(extract, "e1", tree, "e4") in pairs
        # the extract's "Hildegard Löbig geborene Laudler" gives the form
        # "Hildegard Laudler", the tree's name for her
        assert pair(extract, "e3", tree, "e3") in pairs
        # a surname alone never blocks: Sebastian, Ursula and Gustav share
        # only "Löbig"
        assert pair(book, "e2", tree, "e1") not in pairs
        assert pair(extract, "e1", tree, "e1") not in pairs
        assert pair(book, "e4", extract, "e2") not in pairs

    def test_a_married_name_keys_through_the_kin(self):
        """Agnes by her married name in one document and her birth name in
        another pair through her husband's surname."""
        judgment = document(
            "divorce-judgment-1957",
            [entity("e1", "Leopold Löbig"), entity("e2", "Agnes Löbig")],
            [("e1", "spouse", item("e2"))],
            {"e1": "leopold-loebig", "e2": "agnes-loebig"},
        )
        tree = document(
            "family-tree-1984",
            [entity("e1", "Leopold Löbig"), entity("e2", "Agnes Nertler")],
            [("e1", "spouse", item("e2"))],
            {"e1": "leopold-loebig", "e2": "agnes-loebig"},
        )
        pairs = blocked_pairs([judgment.manifest, tree.manifest])
        assert pair(judgment, "e2", tree, "e2") in pairs

    def test_places_key_on_the_first_part_of_their_name_only(self):
        """The export's towns share only "Hesse, Germany" after the first
        comma: no pair. A shared first-part token blocks, whatever the
        rest says."""
        ged, card, talk = gedcom(), registration_card(), interview()
        pairs = blocked_pairs([ged.manifest, card.manifest, talk.manifest])
        places = {
            record.label: record.id
            for record in ged.manifest.batch.entities
            if record.kind == "place"
        }
        kredfeld = places["Kredfeld, Hesse, Germany"]
        floerburg = places["Flörburg, Hesse, Germany"]
        assert pair(ged, kredfeld, ged, floerburg) not in pairs
        assert pair(card, "e4", talk, "e1") in pairs
        assert pair(card, "e3", talk, "e1") in pairs

    def test_gedcom_anchored_mentions_pair_with_their_anchors_only(self):
        ged, extract, papers = gedcom(), birth_extract(), naturalisation()
        manifests = [ged.manifest, extract.manifest, papers.manifest]
        pairs = blocked_pairs(manifests)
        assert pair(extract, "e1", ged, "i8") in pairs
        assert pair(papers, "e1", ged, "i8") in pairs
        # the two documents meet through the export, never directly
        assert pair(extract, "e1", papers, "e1") not in pairs
        unanchored = blocked_pairs(
            manifests,
            load_config(overrides={"blocking": {"gedcom-anchored": False}}),
        )
        assert pair(extract, "e1", papers, "e1") in unanchored

    def test_a_person_of_one_name_token_pairs_only_with_the_export(self):
        """The portrait's pencilled names meet the export's people by
        given name or nickname, and nobody else."""
        ged, photo, tree = gedcom(), studio_photo(), family_tree()
        pairs = blocked_pairs([ged.manifest, photo.manifest, tree.manifest])
        assert pair(photo, "e3", ged, "i3") in pairs  # Wastl
        assert pair(photo, "e2", ged, "i2") in pairs  # Cilli
        assert pair(photo, "e4", ged, "i5") in pairs  # Rahel
        # the tree's Sebastian, "Wastl" too, meets the portrait's through
        # the export alone
        assert pair(photo, "e3", tree, "e2") not in pairs
        alone = blocked_pairs([photo.manifest, tree.manifest])
        assert not [p for p in alone if (photo.sha1, "e3") in p]

    def test_events_block_through_their_participants(self):
        papers = document(
            "naturalisation-1952",
            [
                entity("e1", "Gustave Lobig", aliases=["Gustav Löbig"]),
                entity("e2", "Emigration of Gustave Lobig", "migration"),
                entity("e3", "Birth of Gustave Lobig", "birth"),
            ],
            [
                ("e2", "participant", item("e1")),
                ("e3", "participant", item("e1")),
            ],
            {
                "e1": "gustav-loebig",
                "e2": "emigration-1939",
                "e3": "gustav-loebig",
            },
        )
        # the interview states the participation from the person's side
        talk = document(
            "interview-2002",
            [
                entity("e1", "Gustav Löbig", aliases=["Gus"]),
                entity("e2", "Emigration of Gustav Löbig", "migration"),
            ],
            [("e1", "participant", item("e2"))],
            {"e1": "gustav-loebig", "e2": "emigration-1939"},
        )
        pairs = blocked_pairs([papers.manifest, talk.manifest])
        assert pair(papers, "e2", talk, "e2") in pairs
        # an event of another kind is never its candidate
        assert pair(papers, "e3", talk, "e2") not in pairs

    def test_an_exports_death_event_meets_a_documents_through_people(self):
        """An export that says only that Felix died (DEAT Y) makes a death
        event; the card's death of Felix, named otherwise, meets it through
        the two Felixes, as events meet, and not by its name."""
        text = (
            "0 HEAD\n1 CHAR UTF-8\n"
            "0 @I6@ INDI\n1 NAME Felix /Löbig/\n1 DEAT Y\n0 TRLR\n"
        )
        artifact = ArtifactRef.from_bytes(text.encode())
        adapted = adapt_gedcom(text, artifact).batch
        (event,) = [e for e in adapted.entities if e.kind == "death"]
        export = Doc(
            "gedcom",
            Manifest(artifact.sha1, adapted, GEDCOM),
            {"i6": "felix-loebig", event.id: "felix-loebig"},
        )
        card = document(
            "registration-card-1945",
            [
                entity("e1", "Felix Löbig"),
                entity("e2", "Felix's death", "death"),
            ],
            [
                ("e2", "participant", item("e1")),
                ("e2", "point_in_time", day(1931)),
            ],
            {"e1": "felix-loebig", "e2": "felix-loebig"},
        )
        pairs = blocked_pairs([export.manifest, card.manifest])
        assert pair(card, "e2", export, event.id) in pairs
        assert pair(card, "e1", export, "i6") in pairs

    def test_organizations_block_when_one_name_holds_the_other(self):
        book = directory()
        deck = document(
            "directory-1912",
            [entity("e1", "Hutmacherei zum Blauen Hut", "organization")],
            [],
            {"e1": "zum-blauen-hut"},
            sha1=hashlib.sha1(b"a synthetic deck").hexdigest(),
        )
        talk = document(
            "interview-2002",
            [entity("e1", "the Blue Hat", "organization")],
            [],
            {"e1": "zum-blauen-hut"},
        )
        pairs = blocked_pairs([book.manifest, deck.manifest, talk.manifest])
        assert pair(book, "e1", deck, "e1") in pairs
        assert pair(book, "e1", talk, "e1") not in pairs


# -- dates --------------------------------------------------------------------


def time(year, month=None, precision=None) -> TimeValue:
    return TimeValue(year=year, month=month, precision=precision)


def qualifier(prop: str, value) -> SimpleNamespace:
    return SimpleNamespace(property=prop, value=value)


class TestDates:
    def test_a_date_covers_its_precision_never_narrower(self):
        assert date_interval(time(1919, 7), [], 5) == Interval(1919, 1919)
        assert date_interval(time(1890, precision="decade"), [], 5) == (
            Interval(1890, 1899)
        )
        assert date_interval(time(1801, precision="century"), [], 5) == (
            Interval(1801, 1900)
        )

    def test_uncertainty_widens_the_range(self):
        from yizkr.claims import ItemValue

        circa = qualifier(
            "sourcing_circumstances",
            ItemValue.model_validate(
                {"type": "item", "value": {"kind": "vocab", "key": "circa"}}
            ),
        )
        # Cäcilie, c. 1833
        assert date_interval(time(1833), [circa], 5) == Interval(1828, 1838)
        # Klara left between 1938 and 1939
        between = [
            qualifier("earliest_date", time(1938)),
            qualifier("latest_date", time(1939)),
        ]
        assert date_interval(time(1938), between, 5) == Interval(1938, 1939)
        # Rahel died after 1945; the firm was gone before 1926
        after = date_interval(
            time(1945), [qualifier("earliest_date", time(1945))], 5
        )
        assert after.start == 1945 and after.end == float("inf")
        before = date_interval(
            time(1926), [qualifier("latest_date", time(1926))], 5
        )
        assert before.start == float("-inf") and before.end == 1926
        assert after.overlaps(Interval(1960, 1960))
        assert not before.overlaps(Interval(1927, 1927))


# -- the homonym --------------------------------------------------------------


class TestHomonyms:
    def test_the_two_ursulas_never_merge(self):
        """The widow of the 1912 directory and Sebastian's daughter share a
        name, and the export holds both: every mention of either stays
        apart, and nothing merges falsely."""
        docs = [gedcom(), family_tree(), directory()]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        ursulas = [
            mention_of(docs, identity, "gedcom", "i10"),
            mention_of(docs, identity, "gedcom", "i16"),
            mention_of(docs, identity, "family-tree-1984", "e1"),
            mention_of(docs, identity, "directory-1912", "e4"),
        ]
        entries = [identity["mentions"][m] for m in ursulas]
        assert len({entry["cluster"] for entry in entries}) == 4
        assert {entry["method"] for entry in entries} == {CONFLICTING}
        # one candidate cluster holds them: two people of one export, a
        # soft conflict
        (review,) = [
            entry
            for entry in identity["review"]
            if ursulas[0] in entry["members"]
        ]
        assert sorted(review["members"]) == sorted(ursulas)
        assert review["conflict"]["how"] == SOFT
        assert set(review["conflict"]["mentions"]) == set(ursulas[:2])
        # each document's Ursula matches both of the export's: ambiguous
        assert {p["link"] for p in review["pairs"]} == {AMBIGUOUS}
        # Hildegard, whom the tree and the export both give Ursula as a
        # child, does merge
        assert {"hildegard-loebig"} in merged(docs, identity)

    def test_an_uncorroborated_name_match_stays_apart(self):
        """Without the export, the tree's Ursula and the directory's widow
        match by name alone: an ambiguous cluster, not merged."""
        docs = [family_tree(), directory()]
        identity = identity_of(*docs)
        daughter = mention_of(docs, identity, "family-tree-1984", "e1")
        widow = mention_of(docs, identity, "directory-1912", "e4")
        assert identity["mentions"][daughter]["method"] == AMBIGUOUS
        assert identity["mentions"][widow]["method"] == AMBIGUOUS
        assert (
            identity["mentions"][daughter]["cluster"]
            != identity["mentions"][widow]["cluster"]
        )
        assert_no_false_merge(docs, identity)
        assert identity["summary"]["clusters"][AMBIGUOUS] >= 1


# -- across sources -----------------------------------------------------------


class TestAcrossSources:
    def test_people_and_places_merge_across_the_sources_that_name_them(
        self,
    ):
        docs = [
            gedcom(),
            marriage_certificate(),
            birth_extract(),
            naturalisation(),
        ]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == sorted(
            [
                {"brunnenring-35"},
                {"caecilie-loebig"},
                {"eugen-loebig"},
                {"flaeckfeld"},
                {"gustav-loebig"},
                {"hatter"},
                {"hawthorn-road-8"},
                {"hildegard-loebig"},
                {"sebastian-loebig"},
            ],
            key=sorted,
        )
        # Gustav, by his German name, his English one and the export's
        gustav = {
            identity["mentions"][m]["cluster"]
            for m in (
                mention_of(docs, identity, "gedcom", "i8"),
                mention_of(docs, identity, "birth-extract-1938", "e1"),
                mention_of(docs, identity, "naturalisation-1952", "e1"),
            )
        }
        (cluster,) = gustav
        assert identity["clusters"][cluster]["method"] == CONFIDENT
        assert len(identity["clusters"][cluster]["members"]) == 3
        # the registry office is boilerplate in both certificates: neither
        # mention is clustered, and they never meet
        offices = [
            identity["mentions"][mention_of(docs, identity, key, local_id)]
            for key, local_id in (
                ("marriage-cert-1905", "e8"),
                ("birth-extract-1938", "e6"),
            )
        ]
        assert {office["method"] for office in offices} == {UNCLUSTERED}
        assert offices[0]["cluster"] != offices[1]["cluster"]
        # Flörburg matches three of the export's places: ambiguous, so
        # it stays apart; the vessel meets nothing and is a singleton
        town = mention_of(docs, identity, "birth-extract-1938", "e4")
        assert identity["mentions"][town]["method"] == AMBIGUOUS
        ship = mention_of(docs, identity, "naturalisation-1952", "e4")
        assert identity["mentions"][ship]["method"] == SINGLETON

    def test_rival_districts_stay_apart_and_nesting_is_confident(self):
        """Fläckfeld lies in Kreis Mockburg per the certificate and in
        Kreis Peiltal per the tree: the contradiction coexists, so the two
        full names stay apart, while the bare name nests in either."""
        certificate = document(
            "marriage-cert-1905",
            [entity("e1", "Fläckfeld, Kreis Mockburg", "place")],
            [],
            {"e1": "flaeckfeld"},
        )
        tree = document(
            "family-tree-1984",
            [entity("e1", "Fläckfeld, Kreis Peiltal", "place")],
            [],
            {"e1": "flaeckfeld"},
        )
        bare = document(
            "interview-2002",
            [entity("e1", "Fläckfeld", "place")],
            [],
            {"e1": "flaeckfeld"},
        )
        identity = identity_of(certificate, tree, bare)
        (entry,) = identity["review"]
        links = {
            frozenset(p["mentions"]): (p["link"], p["evidence"])
            for p in entry["pairs"]
        }
        docs = [certificate, tree, bare]

        def link(first: Doc, second: Doc):
            return links[
                frozenset(
                    {
                        first.fingerprint("e1", identity),
                        second.fingerprint("e1", identity),
                    }
                )
            ]

        assert link(certificate, tree) == (AMBIGUOUS, "key/unknown/unknown")
        assert link(certificate, bare) == (
            CONFIDENT,
            "nested/unknown/unknown",
        )
        assert entry["status"] == AMBIGUOUS
        assert_no_false_merge(docs, identity)


# -- kinship ------------------------------------------------------------------


class TestKinship:
    def test_kinship_stated_from_either_end_corroborates(self):
        """The extract gives Gustav's father; the tree gives Sebastian's
        child. Lifted, both say one thing, and with no date at all the two
        Gustavs and the two Sebastians merge on it."""
        docs = [birth_extract(dated=False), family_tree(dated=False)]
        identity = identity_of(*docs)
        assert {"gustav-loebig"} in merged(docs, identity)
        assert {"sebastian-loebig"} in merged(docs, identity)
        gustav = mention_of(docs, identity, "birth-extract-1938", "e1")
        assert identity["mentions"][gustav]["method"] == CONFIDENT
        assert_no_false_merge(docs, identity)

    def test_kinship_in_the_wrong_direction_does_not_corroborate(self):
        """A tree that made Sebastian Gustav's child says another thing
        than the extract: no agreement, so the Gustavs stay apart."""
        extract = birth_extract(dated=False)
        backwards = document(
            "family-tree-1984",
            [entity("e1", "Gustav Löbig"), entity("e2", "Sebastian Löbig")],
            [("e1", "child", item("e2"))],
            {"e1": "gustav-loebig", "e2": "sebastian-loebig"},
        )
        docs = [extract, backwards]
        identity = identity_of(*docs)
        gustav = mention_of(docs, identity, "birth-extract-1938", "e1")
        assert identity["mentions"][gustav]["method"] == AMBIGUOUS

    def test_a_parent_and_child_of_one_name_never_merge(self):
        """Two registers each name a father and his son alike
        (placeholder names: the universe has no such pair). Blocking joins
        all four; the closure joins father and son, a kin edge, so the
        cluster is conflicting (hard), and nothing merges."""

        def register(label: bytes) -> Doc:
            return document(
                "a register",
                [entity("e1", "Xxx Yyy"), entity("e2", "Xxx Yyy")],
                [("e2", "father", item("e1"))],
                {
                    "e1": f"{label.decode()} father",
                    "e2": f"{label.decode()} son",
                },
                sha1=hashlib.sha1(label).hexdigest(),
            )

        docs = [register(b"first"), register(b"second")]
        identity = identity_of(*docs)
        assert all(
            entry["method"] == CONFLICTING
            for entry in identity["mentions"].values()
        )
        (review,) = identity["review"]
        assert review["conflict"]["how"] == HARD
        assert len({e["cluster"] for e in identity["mentions"].values()}) == 4
        summary = identity["summary"]
        assert summary["conflicting_hard"] == 1
        # the within-manifest pairs are father and son: a kin edge each
        assert summary["within_manifest_pairs"] == 2
        assert summary["within_manifest_kin_or_containment"] == 2
        # merging the confident links alone would pair the fathers and the
        # sons, whose kin agree: the count the owner weighs
        assert summary["confident_links_only"] == {
            "clusters": 2,
            "mentions": 4,
            "conflicting": 0,
        }

    def test_dates_that_cannot_overlap_never_prune_a_pair(self):
        """Contradictory dates are a resting state (ADR-0002): the pair
        stays a candidate, ambiguous, for adjudication."""

        def register(label: bytes, year: int) -> Doc:
            return document(
                "a register",
                [entity("e1", "Xxx Yyy")],
                [("e1", "date_of_birth", day(year))],
                {"e1": label.decode()},
                sha1=hashlib.sha1(label).hexdigest(),
            )

        docs = [register(b"first", 1880), register(b"second", 1920)]
        identity = identity_of(*docs)
        (review,) = identity["review"]
        (only,) = review["pairs"]
        assert (only["link"], only["evidence"]) == (
            AMBIGUOUS,
            "exact/conflict/unknown",
        )
        assert identity["summary"]["pruned_pairs"] == {}


class TestCannotLink:
    def test_two_ids_of_one_manifest_conflict_softly(self):
        """A manifest that failed to unify Gustave with Gustav keeps them as
        two ids. The extract's Gustav meets both, so the closure joins two
        ids of one manifest: a soft conflict, left for the owner, and
        nothing merges."""
        extract = birth_extract()
        papers = document(
            "naturalisation-1952",
            [entity("e1", "Gustave Lobig"), entity("e2", "Gustav Löbig")],
            [
                ("e1", "date_of_birth", day(1919, 7, 1)),
                ("e2", "date_of_birth", day(1919)),
            ],
            {"e1": "gustav-loebig", "e2": "gustav-loebig"},
        )
        docs = [extract, papers]
        identity = identity_of(*docs)
        gustavs = [
            mention_of(docs, identity, "birth-extract-1938", "e1"),
            mention_of(docs, identity, "naturalisation-1952", "e1"),
            mention_of(docs, identity, "naturalisation-1952", "e2"),
        ]
        (review,) = [
            entry
            for entry in identity["review"]
            if gustavs[0] in entry["members"]
        ]
        assert sorted(review["members"]) == sorted(gustavs)
        assert review["conflict"]["how"] == SOFT
        assert set(review["conflict"]["mentions"]) == set(gustavs[1:])
        # every pair the closure holds is confident, and still none merges
        assert {p["link"] for p in review["pairs"]} == {CONFIDENT}
        entries = [identity["mentions"][gustav] for gustav in gustavs]
        assert {entry["method"] for entry in entries} == {CONFLICTING}
        assert len({entry["cluster"] for entry in entries}) == 3
        assert identity["summary"]["within_manifest_pairs"] == 1
        assert identity["summary"]["confident_links_only"]["conflicting"] == 1


# -- containment --------------------------------------------------------------


class TestContainment:
    def test_a_place_inside_another_is_never_its_candidate(self):
        """The card puts the court prison in Peiltal; the interview names
        Peiltal. Lifted, the containment rules the prison out of Peiltal's
        cluster (ADR-0017), so the two Peiltals merge."""
        docs = [registration_card(), interview()]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"peiltal"}]
        prison = mention_of(docs, identity, "registration-card-1945", "e3")
        assert identity["mentions"][prison]["method"] == SINGLETON
        summary = identity["summary"]
        assert summary["pruned_pairs"] == {"place-containment": 1}
        assert summary["within_manifest_kin_or_containment"] == 1

    def test_the_manifests_own_places_are_exempt_in_an_export(self):
        """The export makes one place per place string, so two of them
        are no statement that they differ: they may share a cluster's
        candidates without a conflict."""
        docs = [gedcom()]
        identity = identity_of(*docs)
        (review,) = [
            entry for entry in identity["review"] if entry["kind"] == "place"
        ]
        assert review["status"] == AMBIGUOUS
        assert review["conflict"] is None
        assert len(review["members"]) == 3  # the Flörburg strings


# -- the identity map ---------------------------------------------------------


class TestIdentityMap:
    def corpus(self) -> list[Doc]:
        return [
            gedcom(),
            marriage_certificate(),
            birth_extract(),
            naturalisation(),
            family_tree(),
            directory(),
            registration_card(),
            interview(),
            studio_photo(),
        ]

    def test_every_mention_has_a_cluster_and_a_method(self):
        docs = self.corpus()
        identity = identity_of(*docs)
        mentions = identity["mentions"]
        assert len(mentions) == sum(
            len(doc.manifest.batch.entities) for doc in docs
        )
        assert identity["hash"] == identity_hash(mentions)
        for fingerprint, entry in mentions.items():
            cluster = identity["clusters"][entry["cluster"]]
            assert fingerprint in cluster["members"]
            assert cluster["method"] == entry["method"]
            assert entry["cluster"] == cluster_id(cluster["members"])
        members = [
            m
            for entry in identity["clusters"].values()
            for m in entry["members"]
        ]
        assert sorted(members) == sorted(mentions)
        assert_no_false_merge(docs, identity)

    def test_cluster_ids_stand_when_it_is_made_again(self):
        """The same manifests, in any order, give the same map: every
        cluster id and the hash."""
        docs = self.corpus()
        first = identity_of(*docs)
        again = identity_of(*reversed(docs))
        assert again == first
        # a manifest whose records come in another order moves no mention
        # to another cluster
        shuffled = []
        for doc in docs:
            batch = doc.manifest.batch.model_copy(
                update={
                    "entities": list(reversed(doc.manifest.batch.entities))
                }
            )
            shuffled.append(Manifest(doc.sha1, batch, doc.manifest.source))
        reordered = cluster_corpus(shuffled)
        assert reordered["hash"] == first["hash"]
        assert reordered["clusters"] == first["clusters"]

    def test_the_hash_covers_the_decisions_alone(self):
        docs = self.corpus()
        identity = identity_of(*docs)
        mentions = json.loads(json.dumps(identity["mentions"]))
        some = next(iter(mentions.values()))
        some["label"] = "another label"
        assert identity_hash(mentions) == identity["hash"]
        some["method"] = (
            CONFIDENT if some["method"] != CONFIDENT else AMBIGUOUS
        )
        assert identity_hash(mentions) != identity["hash"]

    def test_the_summary_estimates_the_adjudication_calls(self):
        identity = identity_of(*self.corpus())
        summary = identity["summary"]
        assert summary["candidate_pairs"] == (
            summary["confident_pairs"] + summary["ambiguous_pairs"]
        )
        estimate = summary["adjudication"]
        assert estimate["pairs_per_call"] == 40
        assert (
            1
            <= estimate["calls_ambiguous_pairs"]
            <= estimate["calls_all_pairs"]
        )
        smaller = identity_of(
            *self.corpus(), adjudication={"pairs-per-call": 1}
        )
        assert (
            smaller["summary"]["adjudication"]["calls_ambiguous_pairs"]
            == (summary["ambiguous_pairs"])
        )
        assert (
            smaller["summary"]["adjudication"]["calls_all_pairs"]
            == (summary["candidate_pairs"])
        )

    def test_the_attention_reasons_are_counts_and_labels(self):
        """They reach the run's events: no name from any document."""
        docs = self.corpus()
        reasons = attention(identity_of(*docs)["summary"])
        assert [reason.split(":")[0] for reason in reasons] == [
            "ambiguous clusters not merged",
            "conflicting clusters not merged",
            "ambiguous pairs to adjudicate",
        ]
        text = " ".join(reasons).casefold()
        names = {
            token.casefold()
            for doc in docs
            for record in doc.manifest.batch.entities
            for name in (record.label, *record.aliases)
            for token in name.replace(",", " ").split()
            if len(token) > 3
        }
        assert names and not {n for n in names if n in text}

    def test_a_batch_without_a_manifest_takes_part_by_its_refs(self):
        """An extraction that fell back to line ranges names entities by
        refs alone, one id per chunk. Its refs are mentions like any
        other, and never kept apart from each other."""
        sha1 = sha1_of("birth-extract-1938")
        source = {"artifact": {"kind": "artifact", "sha1": sha1}}
        chunked = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": {
                            "kind": "local",
                            "local_id": chunk,
                            "label": "Gustav Löbig",
                            "instance_of": "human",
                        },
                        "property": "date_of_birth",
                        "value": day(1919, 1, 7),
                        "source": source,
                    }
                    for chunk in ("c1:e1", "c2:e1")
                ],
            }
        )
        identity = cluster_corpus([Manifest(sha1, chunked)])
        entries = list(identity["mentions"].values())
        assert [
            e["local_id"] for e in sorted(entries, key=lambda e: e["local_id"])
        ] == [
            "c1:e1",
            "c2:e1",
        ]
        assert {e["method"] for e in entries} == {CONFIDENT}
        assert len({e["cluster"] for e in entries}) == 1
        assert identity["summary"]["documents_without_manifest"] == 1


def test_a_corpus_of_no_document_is_an_empty_map():
    """A run whose every document failed reaches the stage with none: an
    empty map, and nothing that needs attention."""
    identity = cluster_corpus([])
    assert (identity["mentions"], identity["clusters"]) == ({}, {})
    assert identity["hash"] == identity_hash({})
    assert identity["summary"]["mentions"] == 0
    assert attention(identity["summary"]) == []


def test_adjudication_keeps_a_clusters_pairs_together():
    # 85 pairs split into 40, 40 and 5; the 10 of another cluster join
    # the 5
    assert adjudication_calls([85, 10], 40) == 3
    assert adjudication_calls([30, 30], 40) == 2
    assert adjudication_calls([], 40) == 0


# -- the thresholds -----------------------------------------------------------


class TestThresholds:
    def test_the_defaults(self):
        config = load_config()
        assert config.blocking.min_name_token == 3
        assert config.blocking.min_place_token == 4
        assert config.blocking.gedcom_anchored is True
        assert config.evidence.circa_years == 5
        assert config.confident.max_anchors == 1
        assert config.adjudication.pairs_per_call == 40

    def test_a_file_over_the_defaults(self, tmp_path):
        path = tmp_path / "clustering.yaml"
        path.write_text("blocking:\n  min-place-token: 5\n")
        config = load_config(path)
        assert config.blocking.min_place_token == 5
        assert config.digest() != load_config().digest()

    @pytest.mark.parametrize(
        "text",
        [
            "blocking:\n  min-place-tokens: 5\n",
            "confident:\n  max-anchors: 0\n",
            "- a list\n",
        ],
    )
    def test_a_file_it_cannot_take_is_refused(self, tmp_path, text):
        path = tmp_path / "clustering.yaml"
        path.write_text(text)
        with pytest.raises(ClusteringConfigError):
            load_config(path)


# -- the stage ----------------------------------------------------------------


@dataclass
class StoredDoc:
    """A document of a run directory, as the stage reads it."""

    sha1: str
    kind: str
    batch: Path


def stored(tmp_path: Path, docs: list[Doc]) -> list[StoredDoc]:
    found = []
    for doc in docs:
        folder = tmp_path / "docs" / doc.key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "batch.json"
        path.write_text(doc.manifest.batch.model_dump_json(indent=2))
        kind = GEDCOM if doc.manifest.source == GEDCOM else "text"
        found.append(StoredDoc(doc.sha1, kind, path))
    return found


class TestTheStage:
    def docs(self, tmp_path):
        return stored(
            tmp_path, [gedcom(), family_tree(), directory(), birth_extract()]
        )

    def test_it_reuses_its_map_while_its_inputs_stand(self, tmp_path):
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        assert not made.reused
        assert made.path == directory_ / IDENTITY_MAP
        before = made.path.read_bytes()
        inode = made.path.stat().st_ino
        again = ensure_identity_map(directory_, documents)
        assert again.reused
        assert again.identity_map == made.identity_map
        assert made.path.read_bytes() == before
        assert made.path.stat().st_ino == inode
        # a batch made again with other run stamps is the same content
        batch = ClaimBatch.model_validate_json(documents[1].batch.read_text())
        batch.extraction_run.started_at = (
            batch.extraction_run.started_at.replace(year=2027)
        )
        documents[1].batch.write_text(batch.model_dump_json())
        assert ensure_identity_map(directory_, documents).reused

    def test_a_changed_manifest_makes_it_again(self, tmp_path):
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        batch = ClaimBatch.model_validate_json(documents[2].batch.read_text())
        batch.entities[0].aliases.append("the Blue Hat")
        documents[2].batch.write_text(batch.model_dump_json())
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["inputs"] != made.identity_map["inputs"]
        # a document more, or one fewer, too
        assert not ensure_identity_map(directory_, documents[:-1]).reused

    def test_other_thresholds_or_version_make_it_again(
        self, tmp_path, monkeypatch
    ):
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        ensure_identity_map(directory_, documents)
        other = load_config(overrides={"evidence": {"circa-years": 6}})
        remade = ensure_identity_map(directory_, documents, other)
        assert not remade.reused
        assert remade.identity_map["config"] == other.digest()
        made = ensure_identity_map(directory_, documents)
        assert not made.reused
        assert ensure_identity_map(directory_, documents).reused
        # a new version, and nothing else: made again, and a map that
        # comes out the same keeps every cluster id and the hash
        monkeypatch.setattr(clustering, "VERSION", clustering.VERSION + 1)
        bumped = ensure_identity_map(directory_, documents)
        assert not bumped.reused
        assert (
            bumped.identity_map["version"] == made.identity_map["version"] + 1
        )
        assert bumped.identity_map["hash"] == made.identity_map["hash"]
        assert bumped.identity_map["clusters"] == made.identity_map["clusters"]
        assert ensure_identity_map(directory_, documents).reused

    def test_a_damaged_map_is_made_again(self, tmp_path):
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        data = json.loads(made.path.read_text())
        fingerprint = next(iter(data["mentions"]))
        data["mentions"][fingerprint]["cluster"] = "0" * 64
        made.path.write_text(json.dumps(data))
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map == made.identity_map
        made.path.write_text('{"format": 1, "mentions": ')
        assert not ensure_identity_map(directory_, documents).reused
        # a whole map, its decisions hashing to its hash, without the
        # summary the stage reports from
        data = json.loads(made.path.read_text())
        del data["summary"]
        made.path.write_text(json.dumps(data))
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["summary"] == made.identity_map["summary"]

    def test_run_stage_says_what_needs_attention(self, tmp_path):
        logged: list[str] = []
        context = SimpleNamespace(
            directory=tmp_path / "corpus" / "clustering",
            documents=self.docs(tmp_path),
            log=logged.append,
        )
        reasons = run_stage(context)
        written = json.loads((context.directory / IDENTITY_MAP).read_text())
        assert reasons == attention(written["summary"])
        assert [reason.split(":")[0] for reason in reasons] == [
            "ambiguous clusters not merged",
            "conflicting clusters not merged",
            "ambiguous pairs to adjudicate",
        ]
        (line,) = logged
        assert line.startswith("clustering: identity map made: ")
        assert run_stage(context) == reasons
        assert logged[-1].startswith(
            "clustering: identity map reused, its inputs unchanged: "
        )

    def test_an_unreadable_batch_is_named_by_its_sha1(self, tmp_path):
        documents = self.docs(tmp_path)
        documents[1].batch.write_text("{")
        with pytest.raises(ClusteringInputError) as raised:
            ensure_identity_map(tmp_path / "out", documents)
        assert documents[1].sha1[:12] in str(raised.value)
        assert "family-tree" not in str(raised.value)
