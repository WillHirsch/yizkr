"""Corpus clustering (#105): blocking, cannot-link, classification, the
identity map, and the stage's reuse of it.

Every document is one of the fictional universe's sources, cited by the
SHA-1 of its text, with a manifest an extractor could make of it; the
GEDCOM export is the fixture, adapted by the pipeline's own adapter
(docs/EXAMPLES.md). The universe has no parent and child of one name, so
that case uses placeholder names. No wiki, no model.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import unicodedata
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
    HUB,
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
    code_digest,
    date_interval,
    ensure_identity_map,
    identity_hash,
    keys_match,
    load_config,
    person_forms,
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


def register(name: str, entities, claims, truth) -> Doc:
    """A placeholder document, cited by the SHA-1 of its name, for a
    pattern the universe's sources do not show."""
    sha1 = hashlib.sha1(name.encode()).hexdigest()
    return document("a register", entities, claims, truth, sha1=sha1)


def export(records: str, truth: dict[str, str], key: str = "gedcom") -> Doc:
    """A GEDCOM export of `records`, adapted by the pipeline's adapter;
    `truth` names who each person (by id) or place (by string) is."""
    text = "0 HEAD\n1 CHAR UTF-8\n" + records + "0 TRLR\n"
    artifact = ArtifactRef.from_bytes(text.encode())
    batch = adapt_gedcom(text, artifact).batch
    return Doc(
        key,
        Manifest(artifact.sha1, batch, GEDCOM),
        {
            record.id: truth.get(record.id) or truth[record.label]
            for record in batch.entities
        },
    )


def methods(docs, identity, *mentions: tuple[str, str]) -> list[str]:
    return [
        identity["mentions"][mention_of(docs, identity, key, local_id)][
            "method"
        ]
        for key, local_id in mentions
    ]


def nickname(text: str) -> dict:
    return {"type": "monolingual", "value": {"text": text, "language": "de"}}


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
        # 'Blauen Xxx' is looked up by its rarest token, which the hat shop
        # holds, and the hat shop holds no other of its tokens: no pair
        # (placeholders)
        blue, *trades = [
            register(name, [entity("e1", name, "organization")], [], {})
            for name in ("Blauen Xxx", "Xxx Weberei", "Xxx Brauerei")
        ]
        pairs = blocked_pairs(
            [book.manifest, blue.manifest, *(doc.manifest for doc in trades)]
        )
        assert pair(book, "e1", blue, "e1") not in pairs

    def test_given_names_match_by_prefix_not_by_first_letters(self):
        """Gustl and Gustav share three letters, and neither starts the
        other: no key match, and no pair; Hilde starts Hildegard."""
        assert keys_match(
            {"lobig": frozenset({"hilde"})},
            {"lobig": frozenset({"hildegard"})},
        )
        assert not keys_match(
            {"lobig": frozenset({"gustl"})},
            {"lobig": frozenset({"gustav"})},
        )
        short = register(
            "gustl", [entity("e1", "Gustl Löbig")], [], {"e1": "gustav"}
        )
        full = register(
            "gustav", [entity("e1", "Gustav Löbig")], [], {"e1": "gustav"}
        )
        hilde = register(
            "hilde", [entity("e1", "Hilde Löbig")], [], {"e1": "hildegard"}
        )
        hildegard = register(
            "hildegard",
            [entity("e1", "Hildegard Löbig")],
            [],
            {"e1": "hildegard"},
        )
        pairs = blocked_pairs(
            [doc.manifest for doc in (short, full, hilde, hildegard)]
        )
        assert pair(short, "e1", full, "e1") not in pairs
        assert pair(hilde, "e1", hildegard, "e1") in pairs

    def test_a_lone_name_keys_on_its_kins_surnames_only(self):
        """A person named by one token keys with the surname of its kin,
        never with the kin's given name (placeholders)."""
        first = register(
            "lone",
            [entity("e1", "Xxx"), entity("e2", "Yyy Zzz")],
            [("e1", "mother", item("e2"))],
            {"e1": "xxx", "e2": "yyy"},
        )
        by_surname = register(
            "surname", [entity("e1", "Xxx Zzz")], [], {"e1": "xxx"}
        )
        by_given = register(
            "given", [entity("e1", "Xxx Yyy")], [], {"e1": "xxx-yyy"}
        )
        pairs = blocked_pairs(
            [first.manifest, by_surname.manifest, by_given.manifest]
        )
        assert pair(first, "e1", by_surname, "e1") in pairs
        assert pair(first, "e1", by_given, "e1") not in pairs

    def test_a_surname_named_alone_keys_nothing(self):
        """'Frau Löbig', mother of Gustav, and 'Herr Löbig', father of
        Klara: a surname alone, whose only key would pair it with its own
        surname, one shared token."""
        mother = register(
            "frau",
            [entity("e1", "Frau Löbig"), entity("e2", "Gustav Löbig")],
            [("e2", "mother", item("e1"))],
            {"e1": "hildegard-loebig", "e2": "gustav-loebig"},
        )
        father = register(
            "herr",
            [entity("e1", "Herr Löbig"), entity("e2", "Klara Löbig")],
            [("e2", "father", item("e1"))],
            {"e1": "sebastian-loebig", "e2": "klara-loebig"},
        )
        pairs = blocked_pairs([mother.manifest, father.manifest])
        assert pair(mother, "e1", father, "e1") not in pairs

    def test_a_given_name_never_keys_on_its_own_surname(self):
        """Two registers write their people surname first without a
        comma, 'Löbig Ursula' and 'Löbig Gustav', each with kin named
        Löbig: 'Löbig' taken for a given name would key on 'Löbig' as the
        kin's surname, one shared token."""
        ursula = register(
            "ursula",
            [entity("e1", "Löbig Ursula"), entity("e2", "Hildegard Löbig")],
            [("e1", "mother", item("e2"))],
            {"e1": "ursula-loebig", "e2": "hildegard-loebig"},
        )
        gustav = register(
            "gustav",
            [entity("e1", "Löbig Gustav"), entity("e2", "Sebastian Löbig")],
            [("e1", "father", item("e2"))],
            {"e1": "gustav-loebig", "e2": "sebastian-loebig"},
        )
        pairs = blocked_pairs([ursula.manifest, gustav.manifest])
        assert pair(ursula, "e1", gustav, "e1") not in pairs

    def test_generic_place_heads_key_nothing(self):
        """The districts of Mockburg and Peiltal share only their head,
        'Kreis', and never pair; the camp still pairs with Klurnau."""
        certificate = document(
            "marriage-cert-1905",
            [entity("e1", "Kreis Mockburg", "place")],
            [],
            {"e1": "kreis-mockburg"},
        )
        tree = document(
            "family-tree-1984",
            [entity("e1", "Kreis Peiltal", "place")],
            [],
            {"e1": "kreis-peiltal"},
        )
        card = document(
            "registration-card-1945",
            [entity("e1", "Klurnau", "place")],
            [],
            {"e1": "klurnau"},
        )
        compensation = document(
            "compensation-file-1953",
            [entity("e1", "Lager Klurnau", "place")],
            [],
            {"e1": "klurnau-camp"},
        )
        pairs = blocked_pairs(
            [doc.manifest for doc in (certificate, tree, card, compensation)]
        )
        assert pair(certificate, "e1", tree, "e1") not in pairs
        assert pair(card, "e1", compensation, "e1") in pairs

    def test_a_key_too_many_mentions_share_is_blocked_on_by_none(self):
        """Five registers name Gustav alike. Over a hub limit of three,
        the key they share proposes no pair, and the summary and the
        reasons count them; under it they pair and merge."""
        docs = [
            register(
                f"hub-{n}",
                [entity("e1", "Gustav Löbig")],
                [("e1", "date_of_birth", day(1919))],
                {"e1": "gustav-loebig"},
            )
            for n in range(5)
        ]
        manifests = [doc.manifest for doc in docs]
        hub = load_config(overrides={"blocking": {"max-key-mentions": 3}})
        assert blocked_pairs(manifests, hub) == set()
        identity = cluster_corpus(manifests, hub)
        summary = identity["summary"]
        assert (summary["hub_keys"], summary["hub_mentions"]) == (1, 5)
        assert {e["method"] for e in identity["mentions"].values()} == {
            SINGLETON
        }
        assert (
            "hub keys not blocked on: 1 key, held by 5 mentions"
            in attention(summary)
        )
        # the review lists the hub, with the mentions holding it
        (listed,) = identity["review"]
        assert listed == {
            "status": HUB,
            "kind": "human",
            "members": sorted(identity["mentions"]),
            "key": ["key", "gustav", "lobig"],
            "pairs": [],
            "conflict": None,
            "contradiction": None,
        }
        assert len(blocked_pairs(manifests)) == 10
        unguarded = cluster_corpus(manifests)
        assert {e["method"] for e in unguarded["mentions"].values()} == {
            CONFIDENT
        }
        assert unguarded["summary"]["hub_keys"] == 0
        assert unguarded["review"] == []

    def test_a_key_the_export_matches_is_never_a_hub(self):
        """Over a hub limit of three, five registers name Gustav, born
        1919, as the export does: each pairs with the export's Gustav
        alone, a pair each, so the key is no hub, and the six merge. Five
        registers naming him 'Gustl', his nickname, pair with him too: a
        lone token only ever meets the export, and is never a hub."""
        ged = export(
            "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n2 NICK Gustl\n"
            "1 BIRT\n2 DATE 1919\n",
            {"i8": "gustav-loebig"},
        )
        hub = load_config(overrides={"blocking": {"max-key-mentions": 3}})
        dated = [
            register(
                f"gustav-{n}",
                [entity("e1", "Gustav Löbig")],
                [("e1", "date_of_birth", day(1919))],
                {"e1": "gustav-loebig"},
            )
            for n in range(5)
        ]
        manifests = [ged.manifest, *(doc.manifest for doc in dated)]
        assert len(blocked_pairs(manifests, hub)) == 5
        identity = cluster_corpus(manifests, hub)
        assert identity["summary"]["hub_keys"] == 0
        assert merged([ged, *dated], identity) == [{"gustav-loebig"}]
        (cluster,) = [
            entry
            for entry in identity["clusters"].values()
            if len(entry["members"]) > 1
        ]
        assert (cluster["method"], len(cluster["members"])) == (CONFIDENT, 6)
        nicknamed = [
            register(
                f"gustl-{n}",
                [entity("e1", "Gustl")],
                [],
                {"e1": "gustav-loebig"},
            )
            for n in range(5)
        ]
        manifests = [ged.manifest, *(doc.manifest for doc in nicknamed)]
        assert len(blocked_pairs(manifests, hub)) == 5
        identity = cluster_corpus(manifests, hub)
        assert identity["summary"]["hub_keys"] == 0
        # without anchoring the export's key is a hub like any other, and
        # nothing pairs on it
        free = load_config(
            overrides={
                "blocking": {"max-key-mentions": 3, "gedcom-anchored": False}
            }
        )
        manifests = [ged.manifest, *(doc.manifest for doc in dated)]
        assert blocked_pairs(manifests, free) == set()
        assert cluster_corpus(manifests, free)["summary"]["hub_keys"] == 1


class TestNames:
    def test_a_name_written_surname_first_is_read_given_name_first(self):
        """The directory writes 'Löbig, Ursula, Wwe.', the card 'LÖBIG,
        Ida, née SÖRNE', the burial register 'Löbig, Eugen,
        Hutmachermeister': given name first, the widow's title and the
        trade dropped, and the maiden name read as one."""
        forms = person_forms
        assert forms("Löbig, Ursula, Wwe.", 3) == [
            (("ursula", "lobig"), False)
        ]
        assert forms("LÖBIG, Ida, née SÖRNE", 3) == [
            (("ida", "lobig"), False),
            (("ida", "sorne"), False),
        ]
        assert forms("Löbig, Eugen, Hutmachermeister", 3) == [
            (("eugen", "lobig"), False)
        ]
        # a first part of two tokens is no surname: past the comma only a
        # maiden name stays
        assert forms("Hildegard Löbig, geb. Laudler", 3) == [
            (("hildegard", "lobig"), False),
            (("hildegard", "laudler"), False),
        ]

    def test_a_maiden_name_or_an_occupation_past_the_comma(self):
        """A maiden name inside the given names' part, or the surname's,
        is read as one; an occupation where the given names would stand
        gives none, so the surname is named alone, as the directory's
        'Löbig, Hutmacher' would; a marker with no name after it gives no
        second form."""
        forms = person_forms
        hildegard = [
            (("hildegard", "lobig"), False),
            (("hildegard", "laudler"), False),
        ]
        assert forms("Löbig, Hildegard geb. Laudler", 3) == hildegard
        assert forms("Löbig geb. Laudler, Hildegard", 3) == hildegard
        assert forms("Löbig, Hutmacher", 3) == [(("lobig",), True)]
        assert forms("Löbig, Hutmachermeister, Brunnenring 35", 3) == [
            (("lobig",), True)
        ]
        assert forms("Löbig, geb. Laudler", 3) == [
            (("lobig",), True),
            (("laudler",), True),
        ]
        assert forms("Löbig, Ursula, born 1911", 3) == [
            (("ursula", "lobig"), False)
        ]
        # a surname alone keys nothing: two such entries never pair
        entries = [
            register(
                f"hatter-{n}",
                [entity("e1", "Löbig, Hutmacher")],
                [],
                {"e1": truth},
            )
            for n, truth in enumerate(("eugen-loebig", "sebastian-loebig"))
        ]
        assert blocked_pairs([doc.manifest for doc in entries]) == set()

    def test_a_comma_inverted_name_meets_the_same_name(self):
        """The card's Ida, as the card writes her, meets the export's Ida
        Sörne (her nickname written inline) and merges on her son."""
        docs = [gedcom(), registration_card()]
        identity = identity_of(*docs)
        assert {"ida-loebig"} in merged(docs, identity)
        assert_no_false_merge(docs, identity)

    def test_brackets_and_markers_give_forms_of_their_own(self):
        forms = person_forms
        assert forms("Gustave LOBIG (formerly Gustav LÖBIG)", 3) == [
            (("gustave", "lobig"), False),
            (("gustav", "lobig"), False),
        ]
        assert forms("Ida (Peppi) Sörne", 3) == [
            (("ida", "sorne"), False),
            (("peppi",), False),
        ]

    def test_titles_kin_words_and_placeholders_name_no_one(self):
        forms = person_forms
        # a surname alone, after a title, a placeholder or an initial
        assert forms("Frau Löbig", 3) == [(("lobig",), True)]
        assert forms("N.N. Löbig", 3) == [(("lobig",), True)]
        assert forms("Mrs Cutmore", 3) == [(("cutmore",), True)]
        assert forms("G. Löbig", 3) == [(("lobig",), True)]
        # a given name, after a kin or courtesy word
        assert forms("Nana Maisie", 3) == [(("maisie",), False)]
        assert forms("Aunt Pünktchen", 3) == [(("punktchen",), False)]
        # kin words alone are no name at all
        assert forms("Dad", 3) == []
        assert forms("Dein Vater", 3) == []
        # a title before a whole name drops out
        assert forms("Frau Hildegard Löbig", 3) == [
            (("hildegard", "lobig"), False)
        ]

    def test_a_maiden_name_alone_keys_its_bearer(self):
        """The extract's 'Hildegard Löbig geborene Laudler' meets the
        certificate's 'Hildegard Laudler' through its maiden form, with no
        kin to lend a surname."""
        extract = document(
            "birth-extract-1938",
            [entity("e1", "Hildegard Löbig geborene Laudler")],
            [],
            {"e1": "hildegard-loebig"},
        )
        certificate = document(
            "marriage-cert-1905",
            [entity("e1", "Hildegard Laudler")],
            [],
            {"e1": "hildegard-loebig"},
        )
        assert pair(extract, "e1", certificate, "e1") in blocked_pairs(
            [extract.manifest, certificate.manifest]
        )

    def test_a_nickname_keys_with_the_surnames(self):
        """The tree gives Sebastian's nickname Wastl; a register names him
        'Wastl Löbig'."""
        tree = document(
            "family-tree-1984",
            [entity("e1", "Sebastian Löbig")],
            [("e1", "nickname", nickname("Wastl"))],
            {"e1": "sebastian-loebig"},
        )
        book = register(
            "wastl",
            [entity("e1", "Wastl Löbig")],
            [],
            {"e1": "sebastian-loebig"},
        )
        assert pair(tree, "e1", book, "e1") in blocked_pairs(
            [tree.manifest, book.manifest]
        )

    def test_kin_and_courtesy_words_make_no_key_or_name_match(self):
        """Nicholas calls Gustave 'Dad', and Klara Sebastian: an alias of
        a kin word keys nothing and matches nothing, so the two fathers
        never meet."""
        talk = document(
            "interview-2002",
            [
                entity("e1", "Gustave Lobig", aliases=["Dad"]),
                entity("e2", "Nicholas Lobig"),
            ],
            [("e2", "father", item("e1"))],
            {"e1": "gustav-loebig", "e2": "nicholas-lobig"},
        )
        card = document(
            "postcard-1938",
            [
                entity("e1", "Sebastian Löbig", aliases=["Dad"]),
                entity("e2", "Klara Löbig"),
            ],
            [("e2", "father", item("e1"))],
            {"e1": "sebastian-loebig", "e2": "klara-loebig"},
        )
        assert pair(talk, "e1", card, "e1") not in blocked_pairs(
            [talk.manifest, card.manifest]
        )


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

    def test_a_wide_or_open_range_never_corroborates(self):
        """A birth in 'the 19th century' or 'before 1920' fits 1850 and
        1911, and says no more: the pair stays ambiguous. A decade is
        narrow enough to agree (placeholder names)."""

        def born(name: str, value: dict, *qualifiers) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy")],
                [("e1", "date_of_birth", value, *qualifiers)],
                {"e1": name},
            )

        century = {"type": "time", "year": 1801, "precision": "century"}
        wide = identity_of(born("century", century), born("fifty", day(1850)))
        assert {e["method"] for e in wide["mentions"].values()} == {AMBIGUOUS}
        before = identity_of(
            born("before", day(1920), ("latest_date", day(1920))),
            born("eleven", day(1911)),
        )
        assert {e["method"] for e in before["mentions"].values()} == {
            AMBIGUOUS
        }
        decade = {"type": "time", "year": 1910, "precision": "decade"}
        narrow = identity_of(born("decade", decade), born("ten", day(1911)))
        assert {e["method"] for e in narrow["mentions"].values()} == {
            CONFIDENT
        }

    def test_births_and_deaths_are_compared_apart(self):
        """One register gives a birth in 1911, another a death in 1911:
        nothing agrees, since a birth is never held against a death."""
        born = register(
            "born",
            [entity("e1", "Xxx Yyy")],
            [("e1", "date_of_birth", day(1911))],
            {"e1": "born-1911"},
        )
        died = register(
            "died",
            [entity("e1", "Xxx Yyy")],
            [("e1", "date_of_death", day(1911))],
            {"e1": "died-1911"},
        )
        docs = [born, died]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert {e["method"] for e in identity["mentions"].values()} == {
            AMBIGUOUS
        }

    def test_a_birth_events_date_is_its_only_participants(self):
        """The tree dates Klara's birth by an event; the postcard's Klara
        is born in 1917: the event's date agrees, and they merge. A birth
        event naming the mother too gives her no birth date: she merges
        with herself, born 1876, through her daughter."""
        tree = document(
            "family-tree-1984",
            [
                entity("e1", "Klara Löbig"),
                entity("e2", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e2", "participant", item("e1")),
                ("e2", "point_in_time", day(1917)),
            ],
            {"e1": "klara-loebig", "e2": "klara-loebig"},
        )
        card = document(
            "postcard-1938",
            [entity("e1", "Klara Löbig")],
            [("e1", "date_of_birth", day(1917))],
            {"e1": "klara-loebig"},
        )
        docs = [tree, card]
        identity = identity_of(*docs)
        assert {"klara-loebig"} in merged(docs, identity)

        both = register(
            "birth",
            [
                entity("e1", "Klara Löbig"),
                entity("e2", "Hildegard Löbig"),
                entity("e3", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e1", "mother", item("e2")),
                ("e3", "participant", item("e1")),
                ("e3", "participant", item("e2")),
                ("e3", "point_in_time", day(1917)),
            ],
            {
                "e1": "klara-loebig",
                "e2": "hildegard-loebig",
                "e3": "klara-loebig",
            },
        )
        mother = document(
            "marriage-cert-1905",
            [entity("e1", "Hildegard Löbig"), entity("e2", "Klara Löbig")],
            [
                ("e2", "mother", item("e1")),
                ("e1", "date_of_birth", day(1876)),
            ],
            {"e1": "hildegard-loebig", "e2": "klara-loebig"},
        )
        docs = [both, mother]
        identity = identity_of(*docs)
        assert {"hildegard-loebig"} in merged(docs, identity)


# -- events -------------------------------------------------------------------


def arrest_and_visit(*, dated: bool) -> list[Doc]:
    """Leopold's arrest (the card, 1939) and a visit of his (the
    interview): two events of kind `event`, whose one participants merge
    on their mother Ida."""
    card = document(
        "registration-card-1945",
        [
            entity("e1", "Leopold Löbig"),
            entity("e2", "Ida Löbig"),
            entity("e3", "Arrest of Leopold Löbig", "event"),
        ],
        [
            ("e1", "mother", item("e2")),
            ("e3", "participant", item("e1")),
            ("e3", "point_in_time", day(1939, 9, 3)),
        ],
        {"e1": "leopold-loebig", "e2": "ida-loebig", "e3": "arrest-1939"},
    )
    claims = [("e1", "mother", item("e2")), ("e3", "participant", item("e1"))]
    if dated:
        claims.append(("e3", "point_in_time", day(1939, 9)))
    talk = document(
        "interview-2002",
        [
            entity("e1", "Leopold Löbig"),
            entity("e2", "Ida Löbig"),
            entity("e3", "Leopold taken", "event"),
        ],
        claims,
        {
            "e1": "leopold-loebig",
            "e2": "ida-loebig",
            "e3": "arrest-1939" if dated else "visit",
        },
    )
    return [card, talk]


class TestEvents:
    def test_an_event_is_one_when_its_participants_merge(self):
        """Two registers each record a death of a Xxx Yyy in 1941; their
        people, born 1880 and 1920, stay apart, and so do the deaths,
        though their participants' names match (placeholders)."""

        def death(name: str, born: int) -> Doc:
            return register(
                name,
                [
                    entity("e1", "Xxx Yyy"),
                    entity("e2", "Death of Xxx Yyy", "death"),
                ],
                [
                    ("e1", "date_of_birth", day(born)),
                    ("e2", "participant", item("e1")),
                    ("e2", "point_in_time", day(1941)),
                ],
                {"e1": name, "e2": f"{name} death"},
            )

        docs = [death("elder", 1880), death("younger", 1920)]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        deaths = [doc.fingerprint("e2", identity) for doc in docs]
        assert [identity["mentions"][m]["method"] for m in deaths] == [
            AMBIGUOUS,
            AMBIGUOUS,
        ]

    def test_other_events_need_dates_that_agree(self):
        """Leopold's arrest and an undated event of his stay apart, though
        the two Leopolds merge; dated alike, they merge too."""
        docs = arrest_and_visit(dated=False)
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert {"leopold-loebig"} in merged(docs, identity)
        assert methods(
            docs,
            identity,
            ("registration-card-1945", "e3"),
            ("interview-2002", "e3"),
        ) == [AMBIGUOUS, AMBIGUOUS]
        docs = arrest_and_visit(dated=True)
        identity = identity_of(*docs)
        assert {"arrest-1939"} in merged(docs, identity)

    def test_a_birth_or_death_is_decided_by_its_person(self):
        """A person dies once: Felix's death in the card and in the tree,
        one of them undated, is one death when the two Felixes merge."""

        def death(key: str, dated: bool) -> Doc:
            claims = [
                ("e1", "spouse", item("e2")),
                ("e3", "participant", item("e1")),
            ]
            if dated:
                claims.append(("e3", "point_in_time", day(1931)))
            return document(
                key,
                [
                    entity("e1", "Felix Löbig"),
                    entity("e2", "Ida Löbig"),
                    entity("e3", "Death of Felix Löbig", "death"),
                ],
                claims,
                {
                    "e1": "felix-loebig",
                    "e2": "ida-loebig",
                    "e3": "felix-death",
                },
            )

        docs = [
            death("registration-card-1945", True),
            death("family-tree-1984", False),
        ]
        identity = identity_of(*docs)
        assert {"felix-death"} in merged(docs, identity)
        assert {"felix-loebig"} in merged(docs, identity)

    def test_two_births_that_name_one_father_stay_apart(self):
        """Klara's birth and Gustav's each name their father as a
        participant: the Sebastians merge, the children do not, and nor do
        the births."""

        def birth(key: str, child: str, truth: str) -> Doc:
            return document(
                key,
                [
                    entity("e1", child),
                    entity("e2", "Sebastian Löbig"),
                    entity("e3", "Hildegard Löbig"),
                    entity("e4", f"Birth of {child}", "birth"),
                ],
                [
                    ("e1", "father", item("e2")),
                    ("e2", "spouse", item("e3")),
                    ("e4", "participant", item("e1")),
                    ("e4", "participant", item("e2")),
                ],
                {
                    "e1": truth,
                    "e2": "sebastian-loebig",
                    "e3": "hildegard-loebig",
                    "e4": f"{truth} birth",
                },
            )

        docs = [
            birth("postcard-1938", "Klara Löbig", "klara-loebig"),
            birth("birth-extract-1938", "Gustav Löbig", "gustav-loebig"),
        ]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert {"sebastian-loebig"} in merged(docs, identity)
        assert methods(docs, identity, ("postcard-1938", "e4")) == [AMBIGUOUS]

    def test_births_of_one_child_with_dates_that_conflict_stay_apart(self):
        """Two records of Klara's birth, each naming her mother too, give
        1917 and 1919: the dates conflict, and the births stay apart,
        though their people merge."""

        def birth(key: str, year: int) -> Doc:
            return document(
                key,
                [
                    entity("e1", "Klara Löbig"),
                    entity("e2", "Hildegard Löbig"),
                    entity("e3", "Birth of Klara Löbig", "birth"),
                ],
                [
                    ("e1", "mother", item("e2")),
                    ("e3", "participant", item("e1")),
                    ("e3", "participant", item("e2")),
                    ("e3", "point_in_time", day(year)),
                ],
                {
                    "e1": "klara-loebig",
                    "e2": "hildegard-loebig",
                    "e3": "klara-birth",
                },
            )

        docs = [birth("postcard-1938", 1917), birth("family-tree-1984", 1919)]
        identity = identity_of(*docs)
        assert {"klara-loebig"} in merged(docs, identity)
        assert methods(docs, identity, ("postcard-1938", "e3")) == [AMBIGUOUS]
        # the pair's own link is ambiguous, so adjudication counts it
        births = {doc.fingerprint("e3", identity) for doc in docs}
        (review,) = [
            entry
            for entry in identity["review"]
            if set(entry["members"]) == births
        ]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "exact/conflict/unknown")
        ]

    def test_the_deaths_of_a_person_held_for_review_stay_apart(self):
        """The export gives Sebastian, 'Wastl', born 1862; the tree and a
        register each give him born 1862 and his death, the tree's in 1945
        and the register's undated; a photo's 'Wastl' keeps his cluster
        ambiguous. His deaths stay apart while he does, though any two of
        his dated mentions alone would link: an event reads the clusters
        its participants end in, never their links."""
        ged = export(
            "0 @I3@ INDI\n1 NAME Sebastian /Löbig/\n2 NICK Wastl\n"
            "1 SEX M\n1 BIRT\n2 DATE 1862\n",
            {"i3": "sebastian-loebig"},
        )

        def death(make, key: str, dated: bool) -> Doc:
            claims = [
                ("e1", "date_of_birth", day(1862)),
                ("e2", "participant", item("e1")),
            ]
            if dated:
                claims.append(("e2", "point_in_time", day(1945)))
            return make(
                key,
                [
                    entity("e1", "Sebastian Löbig"),
                    entity("e2", "Death of Sebastian Löbig", "death"),
                ],
                claims,
                {"e1": "sebastian-loebig", "e2": "sebastian-death"},
            )

        tree = death(document, "family-tree-1984", True)
        book = death(register, "a death register", False)
        photo = register(
            "wastl", [entity("e1", "Wastl")], [], {"e1": "sebastian-loebig"}
        )
        docs = [ged, tree, book, photo]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        for local_id in ("e1", "e2"):
            assert [
                identity["mentions"][doc.fingerprint(local_id, identity)][
                    "method"
                ]
                for doc in (tree, book)
            ] == [AMBIGUOUS, AMBIGUOUS]
        # without the photo, Sebastian merges, and his deaths with him
        docs = [ged, tree, book]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [
            {"sebastian-death"},
            {"sebastian-loebig"},
        ]

    def test_a_birth_naming_only_the_mother_is_not_her_sons(self):
        """A letter tells of the birth of Hildegard's first child, naming
        only her; the extract gives Gustav's birth on 7 January 1919,
        naming him and her. The Hildegards merge, and the births stay
        apart: two births need dates that agree unless each names one
        participant alone (placeholder letter)."""
        letter = register(
            "first child",
            [
                entity("e1", "Hildegard Löbig"),
                entity("e2", "Birth of Hildegard's first child", "birth"),
            ],
            [
                ("e1", "date_of_birth", day(1876)),
                ("e2", "participant", item("e1")),
            ],
            {"e1": "hildegard-loebig", "e2": "ursula-birth"},
        )
        extract = document(
            "birth-extract-1938",
            [
                entity("e1", "Hildegard Löbig"),
                entity("e2", "Gustav Löbig"),
                entity("e3", "Birth of Gustav Löbig", "birth"),
            ],
            [
                ("e1", "date_of_birth", day(1876)),
                ("e3", "participant", item("e2")),
                ("e3", "participant", item("e1")),
                ("e3", "point_in_time", day(1919, 1, 7)),
            ],
            {
                "e1": "hildegard-loebig",
                "e2": "gustav-loebig",
                "e3": "gustav-birth",
            },
        )
        docs = [letter, extract]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == [{"hildegard-loebig"}]
        births = [
            letter.fingerprint("e2", identity),
            extract.fingerprint("e3", identity),
        ]
        assert [identity["mentions"][m]["method"] for m in births] == [
            AMBIGUOUS,
            AMBIGUOUS,
        ]


# -- the reviewers' false merges ----------------------------------------------


class TestNoFalseMerge:
    """Each case a review found merging two entities of the universe or
    of placeholders; each must stay apart."""

    def test_an_anchor_joins_two_dates_that_conflict(self):
        """The export gives one undated Xxx Yyy, child of Zzz Yyy; two
        registers give a Xxx Yyy, child of Zzz Yyy, born 1911 and 1945.
        Each links to the export's, never to the other."""
        ged = export(
            "0 @I1@ INDI\n1 NAME Zzz /Yyy/\n1 FAMS @F1@\n"
            "0 @I2@ INDI\n1 NAME Xxx /Yyy/\n1 FAMC @F1@\n"
            "0 @F1@ FAM\n1 HUSB @I1@\n1 CHIL @I2@\n",
            {"i1": "zzz", "i2": "xxx-1911"},
        )

        def child(name: str, year: int) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy"), entity("e2", "Zzz Yyy")],
                [
                    ("e1", "father", item("e2")),
                    ("e1", "date_of_birth", day(year)),
                ],
                {"e1": f"xxx-{year}", "e2": "zzz" if year < 1940 else "zzz-2"},
            )

        docs = [ged, child("elder", 1911), child("younger", 1945)]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        (review,) = [
            entry
            for entry in identity["review"]
            if mention_of(docs, identity, "gedcom", "i2") in entry["members"]
        ]
        assert review["status"] == AMBIGUOUS
        assert review["contradiction"]["evidence"] == "exact/conflict/agree"
        assert identity["summary"]["contradicted_clusters"] >= 1

    def test_given_name_prefixes_chain_two_names(self):
        """'Xxx. Yyy' starts both 'Xxxa Yyy' and 'Xxxb Yyy', which never
        match each other: the closure's third member joins them."""

        def child(name: str, given: str, truth: str) -> Doc:
            return register(
                name,
                [entity("e1", f"{given} Yyy"), entity("e2", "Zzz Yyy")],
                [("e1", "father", item("e2"))],
                {"e1": truth, "e2": "zzz"},
            )

        docs = [
            child("a", "Xxxa", "xxxa"),
            child("b", "Xxxb", "xxxb"),
            child("c", "Xxx.", "xxxa-or-xxxb"),
        ]
        identity = identity_of(*docs)
        people = [
            entry["members"]
            for entry in identity["clusters"].values()
            if len(entry["members"]) > 1
            and identity["mentions"][entry["members"][0]]["label"] != "Zzz Yyy"
        ]
        assert people == []

    def test_an_anchor_joins_two_districts(self):
        """The export gives a bare 'Xxxdorf'; two registers place a
        Xxxdorf in two districts. Each nests in the export's, and never in
        the other (placeholders)."""
        ged = export(
            "0 @I1@ INDI\n1 NAME Zzz /Yyy/\n1 BIRT\n2 PLAC Xxxdorf\n",
            {"i1": "zzz", "Xxxdorf": "xxxdorf"},
        )
        one = register(
            "one",
            [entity("e1", "Xxxdorf, Kreis Yyy", "place")],
            [],
            {"e1": "xxxdorf"},
        )
        two = register(
            "two",
            [entity("e1", "Xxxdorf, Kreis Zzz", "place")],
            [],
            {"e1": "xxxdorf-2"},
        )
        docs = [ged, one, two]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)

    def test_organizations_and_families_named_by_a_surname(self):
        """Two firms called 'Firma Löbig' (the second a placeholder), two
        called 'Löbig', and two families called 'Familie Löbig': a name of
        one distinctive token merges none of them."""
        firm = document(
            "directory-1912",
            [
                entity(
                    "e1",
                    "Hutmacherei zum Blauen Hut",
                    "organization",
                    aliases=["Firma Löbig"],
                )
            ],
            [],
            {"e1": "zum-blauen-hut"},
        )
        other = register(
            "weaver",
            [
                entity(
                    "e1",
                    "Xxx Weberei",
                    "organization",
                    aliases=["Firma Löbig"],
                )
            ],
            [],
            {"e1": "weberei"},
        )
        bare = [
            register(
                f"bare-{n}",
                [entity("e1", "Löbig", "organization")],
                [],
                {"e1": f"firm-{n}"},
            )
            for n in range(2)
        ]
        families = [
            register(
                f"family-{n}",
                [entity("e1", "Familie Löbig", "family")],
                [],
                {"e1": f"family-{n}"},
            )
            for n in range(2)
        ]
        docs = [firm, other, *bare, *families]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == []

    def test_an_organization_holding_anothers_name_is_not_it(self):
        """'Blauen Hut' holds tokens of 'Hutmacherei zum Blauen Hut': a
        candidate, never a confident link on that alone."""
        book = directory()
        short = register(
            "short",
            [entity("e1", "Blauen Hut", "organization")],
            [],
            {"e1": "zum-blauen-hut"},
        )
        docs = [book, short]
        identity = identity_of(*docs)
        (review,) = [
            entry
            for entry in identity["review"]
            if entry["kind"] == "organization"
        ]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "key/unknown/unknown")
        ]

    def test_an_institution_named_by_its_kind_needs_a_place(self):
        """'Städtische Realschule' names a school in every town: two
        registers name one in Flörburg and one in Peiltal, which stay
        apart; one at a street in Flörburg merges with Flörburg's, the
        place it lies in being theirs. 'Städtische Realschule Flörburg'
        names its town, and merges on the name alone (placeholders)."""

        def school(key: str, places: list[str], truth: str) -> Doc:
            entities = [
                entity("e1", "Städtische Realschule", "organization"),
                *(
                    entity(f"p{n}", place, "place")
                    for n, place in enumerate(places)
                ),
            ]
            claims = [("e1", "location", item("p0"))] + [
                (f"p{n}", "part_of", item(f"p{n + 1}"))
                for n in range(len(places) - 1)
            ]
            return register(
                key,
                entities,
                claims,
                {
                    "e1": truth,
                    **{f"p{n}": place for n, place in enumerate(places)},
                },
            )

        floerburg = school("in Flörburg", ["Flörburg"], "realschule-f")
        peiltal = school("in Peiltal", ["Peiltal"], "realschule-p")
        street = school(
            "on a street", ["Xxxstrasse 1", "Flörburg"], "realschule-f"
        )
        docs = [floerburg, peiltal]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        (review,) = identity["review"]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "exact/unknown/unknown")
        ]
        docs = [floerburg, street]
        identity = identity_of(*docs)
        assert {"realschule-f"} in merged(docs, identity)
        named = [
            register(
                f"named-{n}",
                [
                    entity(
                        "e1", "Städtische Realschule Flörburg", "organization"
                    )
                ],
                [],
                {"e1": "realschule-f"},
            )
            for n in range(2)
        ]
        assert merged(named, identity_of(*named)) == [{"realschule-f"}]

    def test_placeholders_name_no_one(self):
        """The export's N.N. Yyy (born 1922, child of Zzz Yyy) and a
        register's N.N. Yyy, child of Zzz Yyy too, are two unnamed
        children; so are two registers' own."""
        ged = export(
            "0 @I1@ INDI\n1 NAME Zzz /Yyy/\n1 FAMS @F1@\n"
            "0 @I2@ INDI\n1 NAME N.N. /Yyy/\n1 BIRT\n2 DATE 1922\n"
            "1 FAMC @F1@\n0 @F1@ FAM\n1 HUSB @I1@\n1 CHIL @I2@\n",
            {"i1": "zzz", "i2": "infant-1922"},
        )

        def infant(name: str) -> Doc:
            return register(
                name,
                [entity("e1", "N.N. Yyy"), entity("e2", "Zzz Yyy")],
                [("e1", "father", item("e2"))],
                {"e1": f"infant-{name}", "e2": "zzz"},
            )

        docs = [ged, infant("one"), infant("two")]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)

    def test_kin_named_by_a_surname_alone_agree_on_nothing(self):
        """Two registers name a Xxx Yyy whose mother is 'Frau Yyy'; a
        third names one whose father is 'Herr Yyy', and a fourth one whose
        parent is 'Yyy' alone. No two mothers, fathers or children merge
        on those names (placeholders)."""

        def child(name: str, parent: str, relation: str, truth: str) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy"), entity("e2", parent)],
                [("e1", relation, item("e2"))],
                {"e1": truth, "e2": f"{truth} {relation}"},
            )

        docs = [
            child("one", "Frau Yyy", "mother", "xxx-one"),
            child("two", "Frau Yyy", "mother", "xxx-two"),
            child("three", "Herr Yyy", "father", "xxx-three"),
            child("four", "Yyy", "mother", "xxx-four"),
            child("five", "Yyy", "father", "xxx-five"),
        ]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == []
        # two at a time too: of all five, the pairs no kin corroborates
        # leave the one candidate cluster ambiguous, which would hide a
        # pair that links confidently
        for two in itertools.combinations(docs, 2):
            assert merged(two, identity_of(*two)) == []

    def test_refs_of_no_kind_never_merge(self):
        """Two batches without a manifest each name a Xxx Yyy by a ref of
        no kind: nothing says what they are."""

        def fallback(name: str) -> Manifest:
            sha1 = hashlib.sha1(name.encode()).hexdigest()
            batch = ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "claims": [
                        {
                            "subject": {
                                "kind": "local",
                                "local_id": "c1:e1",
                                "label": "Xxx Yyy",
                            },
                            "property": "date_of_birth",
                            "value": day(1911),
                            "source": {
                                "artifact": {"kind": "artifact", "sha1": sha1}
                            },
                        }
                    ],
                }
            )
            return Manifest(sha1, batch)

        identity = cluster_corpus([fallback("one"), fallback("two")])
        entries = list(identity["mentions"].values())
        assert {entry["method"] for entry in entries} == {AMBIGUOUS}
        assert len({entry["cluster"] for entry in entries}) == 2


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

    def test_a_mention_matching_two_anchors_links_to_neither(self):
        """The export holds two Ursula Löbigs, one born 1911; six
        registers each name an Ursula Löbig born 1911. Each matches both
        anchors, and links to neither confidently, on whichever side of
        the pair its fingerprint sorts."""
        ged = export(
            "0 @I10@ INDI\n1 NAME Ursula /Löbig/\n1 BIRT\n2 DATE 1911\n"
            "0 @I16@ INDI\n1 NAME Ursula /Löbig/\n",
            {"i10": "ursula-loebig", "i16": "ursula-loebig-angerring"},
        )
        registers = [
            register(
                f"ursula-{n}",
                [entity("e1", "Ursula Löbig")],
                [("e1", "date_of_birth", day(1911))],
                {"e1": "ursula-loebig"},
            )
            for n in range(6)
        ]
        identity = identity_of(ged, *registers)
        daughter = ged.fingerprint("i10", identity)
        pairs = [
            p
            for entry in identity["review"]
            for p in entry["pairs"]
            if daughter in p["mentions"]
        ]
        assert len(pairs) == 6
        assert {p["link"] for p in pairs} == {AMBIGUOUS}
        # the anchor sorts first in some pairs and second in others
        assert {p["mentions"][0] == daughter for p in pairs} == {True, False}

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

    def test_one_ambiguous_link_leaves_the_whole_cluster_unmerged(self):
        """Three registers give a Xxx Yyy born 1911, a fourth an undated
        one: the three link confidently, the fourth ambiguously with each.
        A candidate cluster merges whole or not at all, so none of the four
        merges; its confident links alone would join the three
        (placeholders)."""

        def person(name: str, year: int | None) -> Doc:
            claims = [("e1", "date_of_birth", day(year))] if year else []
            return register(
                name, [entity("e1", "Xxx Yyy")], claims, {"e1": "xxx"}
            )

        docs = [person(name, 1911) for name in ("one", "two", "three")]
        docs.append(person("four", None))
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        (review,) = identity["review"]
        assert review["status"] == AMBIGUOUS
        assert sorted(p["link"] for p in review["pairs"]) == (
            [AMBIGUOUS] * 3 + [CONFIDENT] * 3
        )
        assert identity["summary"]["confident_links_only"] == {
            "clusters": 1,
            "mentions": 3,
            "conflicting": 0,
        }


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

    def test_kin_who_share_only_a_surname_do_not_corroborate(self):
        """Two registers name a Xxx Yyy, one the child of Zzz Yyy, the
        other of Www Yyy: the fathers share a surname, not a name, and the
        two stay apart; a third, child of Zzz Yyy too, merges with the
        first (placeholders)."""

        def child(name: str, father: str) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy"), entity("e2", f"{father} Yyy")],
                [("e1", "father", item("e2"))],
                {"e1": f"xxx-of-{father}", "e2": father},
            )

        docs = [child("one", "Zzz"), child("two", "Www")]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        (review,) = identity["review"]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "exact/unknown/unknown")
        ]
        docs = [child("one", "Zzz"), child("three", "Zzz")]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"Zzz"}, {"xxx-of-Zzz"}]

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

    def test_dates_that_conflict_keep_a_pair_apart_whatever_the_kin(self):
        """Two registers name a Xxx Yyy, child of Zzz Yyy, born 1880 and
        1920: the kin agree, the dates conflict, and the pair stays
        ambiguous (placeholders)."""

        def child(name: str, year: int) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy"), entity("e2", "Zzz Yyy")],
                [
                    ("e1", "father", item("e2")),
                    ("e1", "date_of_birth", day(year)),
                ],
                {"e1": f"xxx-{year}", "e2": "zzz"},
            )

        docs = [child("elder", 1880), child("younger", 1920)]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        children = [doc.fingerprint("e1", identity) for doc in docs]
        (review,) = [
            entry
            for entry in identity["review"]
            if children[0] in entry["members"]
        ]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "exact/conflict/agree")
        ]

    def test_kin_of_one_name_whose_dates_conflict_keep_a_pair_apart(self):
        """Two registers name a Xxx Yyy born 1919, one's father a Zzz Yyy
        born 1862, the other's a Zzz Yyy born 1890: the children's dates
        agree, and their fathers, of one name, differ. The link is
        ambiguous (placeholders)."""

        def child(name: str, father_born: int) -> Doc:
            return register(
                name,
                [entity("e1", "Xxx Yyy"), entity("e2", "Zzz Yyy")],
                [
                    ("e1", "father", item("e2")),
                    ("e1", "date_of_birth", day(1919)),
                    ("e2", "date_of_birth", day(father_born)),
                ],
                {"e1": f"xxx-{father_born}", "e2": f"zzz-{father_born}"},
            )

        docs = [child("one", 1862), child("two", 1890)]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        children = [doc.fingerprint("e1", identity) for doc in docs]
        (review,) = [
            entry
            for entry in identity["review"]
            if children[0] in entry["members"]
        ]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "exact/agree/unknown")
        ]

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

    def test_an_exports_death_events_are_kept_apart(self):
        """An export saying of each of its two Ursula Löbigs only that she
        died makes two death events: two ids of one export, never a
        candidate pair, as its people are not."""
        ged = export(
            "0 @I10@ INDI\n1 NAME Ursula /Löbig/\n1 DEAT Y\n"
            "0 @I16@ INDI\n1 NAME Ursula /Löbig/\n1 DEAT Y\n",
            {
                "i10": "ursula-loebig",
                "i16": "ursula-loebig-angerring",
                "i10-death": "ursula-loebig death",
                "i16-death": "ursula-loebig-angerring death",
            },
        )
        identity = identity_of(ged)
        assert identity["summary"]["within_manifest_pairs"] == 2
        assert identity["review"] == []
        assert_no_false_merge([ged], identity)


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

    def test_location_lifts_containment_too(self):
        """The card may put the prison in Peiltal by `location`: the
        prison is ruled out of Peiltal's cluster all the same."""
        card = document(
            "registration-card-1945",
            [
                entity("e3", "Peiltal court prison", "place"),
                entity("e4", "Peiltal", "place"),
            ],
            [("e3", "location", item("e4"))],
            {"e3": "peiltal-court-prison", "e4": "peiltal"},
        )
        docs = [card, interview()]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"peiltal"}]
        assert identity["summary"]["pruned_pairs"] == {"place-containment": 1}

    def test_a_closure_joining_a_place_and_its_container_conflicts_hard(
        self,
    ):
        """A register puts Lager Klurnau in Klurnau; the card names
        Klurnau, the compensation file Lager Klurnau. The closure joins
        all four, the camp and its container among them: a hard
        conflict."""
        record = register(
            "camp",
            [
                entity("e1", "Lager Klurnau", "place"),
                entity("e2", "Klurnau", "place"),
            ],
            [("e1", "part_of", item("e2"))],
            {"e1": "klurnau-camp", "e2": "klurnau"},
        )
        card = document(
            "registration-card-1945",
            [entity("e1", "Klurnau", "place")],
            [],
            {"e1": "klurnau"},
        )
        compensation = document(
            "compensation-file-1953",
            [entity("e1", "Lager Klurnau", "place")],
            [],
            {"e1": "klurnau-camp"},
        )
        docs = [record, card, compensation]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        (review,) = identity["review"]
        assert (review["status"], review["conflict"]["how"]) == (
            CONFLICTING,
            HARD,
        )
        # the camp, and a Klurnau: its container, or one named like it
        camp = record.fingerprint("e1", identity)
        (other,) = set(review["conflict"]["mentions"]) - {camp}
        assert identity["mentions"][other]["label"] == "Klurnau"

    def test_a_place_counts_the_anchors_it_nests_in_as_one(self):
        """The export gives Hildegard's birthplace two ways; the tree's
        bare 'Fläckfeld' nests in both, which count as one anchor, and the
        three merge."""
        ged = export(
            "0 @I4@ INDI\n1 NAME Hildegard /Laudler/\n"
            "1 BIRT\n2 PLAC Fläckfeld, Kreis Mockburg, Hesse, Germany\n"
            "1 BIRT\n2 PLAC Fläckfeld, Hesse, Germany\n",
            {
                "i4": "hildegard-loebig",
                "Fläckfeld, Kreis Mockburg, Hesse, Germany": "flaeckfeld",
                "Fläckfeld, Hesse, Germany": "flaeckfeld",
            },
        )
        tree = document(
            "family-tree-1984",
            [entity("e1", "Fläckfeld", "place")],
            [],
            {"e1": "flaeckfeld"},
        )
        docs = [ged, tree]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"flaeckfeld"}]
        (cluster,) = [
            entry
            for entry in identity["clusters"].values()
            if len(entry["members"]) > 1
        ]
        assert len(cluster["members"]) == 3

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

    def test_the_whole_map_stands_when_it_is_made_again(self):
        """Every part of the map, the review and its conflicting pairs
        included, is the same whatever order the manifests and their
        records come in; only the inputs key follows the batches."""
        docs = self.corpus()
        first = identity_of(*docs)
        shuffled = [
            Manifest(
                doc.sha1,
                doc.manifest.batch.model_copy(
                    update={
                        "entities": list(reversed(doc.manifest.batch.entities))
                    }
                ),
                doc.manifest.source,
            )
            for doc in reversed(docs)
        ]
        again = cluster_corpus(shuffled)
        del first["inputs"], again["inputs"]
        assert again == first
        assert json.dumps(again) == json.dumps(first)

    def test_one_map_whatever_the_hash_seed(self):
        """Sets iterate in an order the hash seed sets: two processes of
        other seeds make the same maps of two synthetic corpora and of the
        siblings, each with conflicting clusters."""
        script = (
            "import hashlib, json, sys\n"
            "sys.path.insert(0, '.')\n"
            "from tests.unit.test_clustering import synthetic_corpus\n"
            "from yizkr.pipeline.clustering import cluster_corpus\n"
            "from tests.unit.test_clustering import siblings_corpus\n"
            "maps = [cluster_corpus(synthetic_corpus(n)) for n in (7, 11)]\n"
            "maps.append(cluster_corpus(siblings_corpus()))\n"
            "assert all(m['summary']['clusters']['conflicting']"
            " for m in maps)\n"
            "print(hashlib.sha256(json.dumps(maps).encode()).hexdigest())\n"
        )
        root = Path(__file__).resolve().parents[2]
        digests = {
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=root,
                env=dict(os.environ, PYTHONHASHSEED=seed),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for seed in ("0", "4242")
        }
        assert len(digests) == 1

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


SYLLABLES = ("ka", "lo", "mi", "ne", "ru", "ta", "vi", "zo", "pe", "fu")


def synthetic_corpus(seed: int, documents: int = 40) -> list[Manifest]:
    """Registers of invented syllable names, drawn with a fixed seed:
    homonyms, kin and places enough for clusters that conflict."""
    rng = random.Random(seed)

    def name(parts: int) -> str:
        return "".join(rng.choice(SYLLABLES) for _ in range(parts)).title()

    givens = [name(2) for _ in range(6)]
    surnames = [name(3) for _ in range(3)]
    towns = [name(3) for _ in range(4)]
    manifests = []
    for number in range(documents):
        sha1 = hashlib.sha1(f"synthetic {seed} {number}".encode()).hexdigest()
        entities, claims = [], []
        for index in range(rng.randint(3, 7)):
            if rng.random() < 0.75:
                label, kind = (
                    f"{rng.choice(givens)} {rng.choice(surnames)}",
                    "human",
                )
            else:
                label, kind = rng.choice(towns), "place"
            entities.append(entity(f"e{index}", label, kind))
        people = [e["id"] for e in entities if e["kind"] == "human"]
        places = [e["id"] for e in entities if e["kind"] == "place"]
        for _ in range(rng.randint(1, 4)):
            if len(people) >= 2:
                first, second = rng.sample(people, 2)
                relation = rng.choice(["father", "mother", "spouse", "child"])
                claims.append((first, relation, item(second)))
        for person in people:
            if rng.random() < 0.5:
                claims.append(
                    (person, "date_of_birth", day(rng.randint(1880, 1920)))
                )
        if len(places) >= 2:
            claims.append((places[0], "part_of", item(places[1])))
        doc = document("a register", entities, claims, {}, sha1=sha1)
        manifests.append(doc.manifest)
    return manifests


def siblings_corpus() -> list[Manifest]:
    """A register naming seven siblings all called Xxx Yyy, and another
    naming one Xxx Yyy: the eight are one candidate cluster, and each
    sibling in it conflicts hard with six others (placeholders)."""
    siblings = register(
        "siblings",
        [entity(f"e{n}", "Xxx Yyy") for n in range(7)],
        [
            (f"e{n}", "sibling", item(f"e{m}"))
            for n in range(7)
            for m in range(n + 1, 7)
        ],
        {},
    )
    another = register("another", [entity("e1", "Xxx Yyy")], [], {})
    return [siblings.manifest, another.manifest]


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
        inode = made.path.stat().st_ino
        batch = ClaimBatch.model_validate_json(documents[2].batch.read_text())
        batch.entities[0].aliases.append("the Blue Hat")
        documents[2].batch.write_text(batch.model_dump_json())
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["inputs"] != made.identity_map["inputs"]
        # written whole, as a new file put in the old one's place
        assert again.path.stat().st_ino != inode
        # a document more, or one fewer, too
        assert not ensure_identity_map(directory_, documents[:-1]).reused

    def test_a_change_of_claims_alone_makes_it_again(self, tmp_path):
        """The tree's dates dropped, its records untouched: kin, dates and
        containment are claims, and the map reads them."""
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        ensure_identity_map(directory_, documents)
        batch = ClaimBatch.model_validate_json(documents[1].batch.read_text())
        entities = batch.entities
        batch.claims = [
            claim for claim in batch.claims if claim.value.type != "time"
        ]
        documents[1].batch.write_text(batch.model_dump_json())
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert (
            ClaimBatch.model_validate_json(
                documents[1].batch.read_text()
            ).entities
            == entities
        )

    def test_each_documents_sha1_and_source_are_in_the_key(self, tmp_path):
        """A batch read under another SHA-1 (the media scanned again, the
        extraction the same), or a GEDCOM export's read as a document, is
        other input: a mention's fingerprint holds its document's SHA-1."""
        directory_ = tmp_path / "corpus" / "clustering"
        documents = sorted(self.docs(tmp_path), key=lambda doc: doc.sha1)
        made = ensure_identity_map(directory_, documents)
        # the last document by SHA-1 moves to a SHA-1 that sorts last too:
        # the documents keep their order, and only the SHA-1 differs
        last = documents[-1]
        assert last.sha1 < "f" * 40
        moved = [*documents[:-1], StoredDoc("f" * 40, last.kind, last.batch)]
        again = ensure_identity_map(directory_, moved)
        assert not again.reused
        assert set(again.identity_map["mentions"]) != set(
            made.identity_map["mentions"]
        )
        assert ensure_identity_map(directory_, moved).reused
        read_as = [StoredDoc(doc.sha1, "text", doc.batch) for doc in moved]
        assert not ensure_identity_map(directory_, read_as).reused

    def test_other_code_makes_it_again(self, tmp_path, monkeypatch):
        """Another source of the modules that make the map, VERSION
        unchanged: made again, and the same decisions keep their hash."""
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        assert ensure_identity_map(directory_, documents).reused
        monkeypatch.setattr(clustering, "code_digest", lambda: "0" * 64)
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["hash"] == made.identity_map["hash"]
        assert ensure_identity_map(directory_, documents).reused

    def test_the_code_digest_reads_each_module_it_names(
        self, tmp_path, monkeypatch
    ):
        """The digest is of the bytes of clustering.py, fingerprints.py
        and reconcile.py: a byte changed in any of them changes it."""
        assert clustering.CODE == (
            "clustering.py",
            "fingerprints.py",
            "reconcile.py",
        )
        before = code_digest()
        for name in clustering.CODE:
            shutil.copy(clustering.PACKAGE / name, tmp_path / name)
        monkeypatch.setattr(clustering, "PACKAGE", tmp_path)
        assert code_digest() == before
        for name in clustering.CODE:
            copy = tmp_path / name
            original = copy.read_bytes()
            copy.write_bytes(original + b"\n")
            assert code_digest() != before, name
            copy.write_bytes(original)

    def test_another_unicode_database_makes_it_again(
        self, tmp_path, monkeypatch
    ):
        """Names are normalised and folded by the Unicode database the
        interpreter ships, so its version is in the key."""
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        monkeypatch.setattr(unicodedata, "unidata_version", "0.0.0")
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["hash"] == made.identity_map["hash"]

    def test_every_module_it_imports_reaches_its_key(self):
        """Every yizkr module clustering.py and fingerprints.py import is
        in CODE, whose source is in the key, or reaches the key through
        the batches' hashes: claims parses the batches, orchestrate says
        what a hash leaves out, and stages makes it. A module outside both
        could change the map and leave its key standing."""
        through_the_batches = {
            "yizkr.claims",
            "yizkr.pipeline.orchestrate",
            "yizkr.pipeline.stages",
        }
        in_code = {
            f"yizkr.pipeline.{name.removesuffix('.py')}"
            for name in clustering.CODE
        }
        for name in ("clustering.py", "fingerprints.py"):
            tree = ast.parse((clustering.PACKAGE / name).read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = f"yizkr.pipeline.{module}".rstrip(".")
                    imported.add(module)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
            ours = {m for m in imported if m.split(".")[0] == "yizkr"}
            assert ours, name
            assert ours <= in_code | through_the_batches, (name, ours)

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
        # the estimate's threshold too
        calls = load_config(overrides={"adjudication": {"pairs-per-call": 7}})
        assert not ensure_identity_map(directory_, documents, calls).reused
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
        # summary the stage reports from, or without its review
        for section in ("summary", "review"):
            data = json.loads(made.path.read_text())
            del data[section]
            made.path.write_text(json.dumps(data))
            again = ensure_identity_map(directory_, documents)
            assert not again.reused, section
            assert again.identity_map[section] == made.identity_map[section]

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
