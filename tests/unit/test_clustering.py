"""Corpus clustering (#105, #409): keys from mention records, blocking,
cannot-link, classification, the identity map, and the stage's reuse of
it.

Every document is one of the fictional universe's sources, cited by the
SHA-1 of its text, with a manifest an extractor could make of it and the
mention records (ADR-0035 §2) its entities carry; the GEDCOM export is
the fixture, adapted by the pipeline's own adapter, which fills its
records from the export's tags (ADR-0035 §6; docs/EXAMPLES.md). The
universe has no parent
and child of one name, so that case uses placeholder names. No wiki, no
model.
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
    CONFIDENT_COMPONENT,
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
    load_config,
    part_keys,
    run_stage,
)
from yizkr.pipeline.fingerprints import cluster_id
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.names import normalize_entity_dict

SOURCES = {source.key: source for source in u.SOURCES}
RUN = {
    "model_identifier": "scripted-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-09-29T09:00:00Z",
}


# -- records ------------------------------------------------------------------


def given(written: str, standard: str | None = None, **extra) -> dict:
    found = {"written": written, **extra}
    if standard:
        found["standard"] = standard
    return found


def person_record(
    givens=(),
    surnames=(),
    *,
    birth=(),
    married=(),
    patronymics=(),
    nicknames=(),
    titles=(),
    suffixes=(),
    unsplit=(),
    status="name",
) -> dict:
    """A person's record: given names (a string, or `given`'s dict),
    surnames typed `other`, `birth` or `married`, and the rest as
    written."""
    return {
        "kind": "human",
        "status": status,
        "given": [g if isinstance(g, dict) else given(g) for g in givens],
        "surnames": [
            {"written": name, "type": kind}
            for kind, names in (
                ("other", surnames),
                ("birth", birth),
                ("married", married),
            )
            for name in names
        ],
        "patronymics": list(patronymics),
        "nicknames": list(nicknames),
        "titles": list(titles),
        "suffixes": list(suffixes),
        "unsplit": list(unsplit),
    }


def place_record(*names, within=(), address=None, status="toponym") -> dict:
    """A place's record: each name as written, or as (written, toponym,
    head)."""
    found = {
        "kind": "place",
        "status": status,
        "names": [
            {"written": name}
            if isinstance(name, str)
            else {
                "written": name[0],
                **({"toponym": name[1]} if name[1] else {}),
                **({"head": name[2]} if name[2] else {}),
            }
            for name in names
        ],
        "within": list(within),
    }
    if address:
        found["address"] = {"written": address}
    return found


def local(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def item(local_id: str) -> dict:
    return {"type": "item", "value": local(local_id)}


def vocab(key: str) -> dict:
    return {"type": "item", "value": {"kind": "vocab", "key": key}}


def role(name: str) -> tuple[str, dict]:
    return ("object_has_role", vocab(name))


def day(year: int, month: int | None = None, number: int | None = None):
    value = {"type": "time", "year": year}
    if month:
        value["month"] = month
    if number:
        value["day"] = number
    return value


def entity(local_id, label, kind="human", aliases=(), lines=(1,), **extra):
    """An entity without a record: recordless, for a kind that takes one."""
    return {
        "id": local_id,
        "label": label,
        "kind": kind,
        "aliases": list(aliases),
        "mention_lines": list(lines),
        **extra,
    }


def human(local_id, label, *, aliases=(), lines=(1,), extract=True, **parts):
    """A person with a record: the parts given, or, for a label of two
    words and no parts, its first as the given name and its second as
    the surname, as an extractor reads a name written given name
    first."""
    if not parts:
        first, last = label.split()
        parts = {"givens": [first], "surnames": [last]}
    return entity(
        local_id,
        label,
        aliases=aliases,
        lines=lines,
        extract=extract,
        structure=person_record(**parts),
    )


def place(local_id, label, *, aliases=(), lines=(1,), names=None, **record):
    """A place with a record: its label its one name, unless `names` says
    otherwise."""
    return entity(
        local_id,
        label,
        "place",
        aliases=aliases,
        lines=lines,
        structure=place_record(*(names or [label]), **record),
    )


def organization(
    local_id, label, *, names=(), kinds=(), places=(), aliases=(), **extra
):
    return entity(
        local_id,
        label,
        "organization",
        aliases=aliases,
        structure={
            "kind": "organization",
            "names": list(names),
            "kinds": list(kinds),
            "places": list(places),
        },
        **extra,
    )


def family(local_id, label, *surnames):
    return entity(
        local_id,
        label,
        "family",
        structure={"kind": "family", "surnames": list(surnames)},
    )


# -- the fictional documents --------------------------------------------------


def sha1_of(key: str) -> str:
    """A source's artifact: the SHA-1 of its text."""
    return hashlib.sha1(SOURCES[key].text.encode()).hexdigest()


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


def union_of(union_id: str, first: str, second: str, kind="spouse"):
    """The claims of a union with its couple (ADR-0036)."""
    return [
        (union_id, "participant", item(first), role(kind)),
        (union_id, "participant", item(second), role(kind)),
    ]


def marriage_certificate() -> Doc:
    return document(
        "marriage-cert-1905",
        [
            human("e1", "Sebastian Löbig"),
            human("e2", "Eugen Löbig"),
            human(
                "e3",
                "Cäcilie Tarle",
                aliases=["Cäcilie geborene Tarle"],
                givens=["Cäcilie"],
                birth=["Tarle"],
            ),
            human("e4", "Hildegard Laudler"),
            place("e5", "Flörburg"),
            place("e6", "Fläckfeld"),
            place(
                "e7",
                "Kreis Mockburg",
                names=[("Kreis Mockburg", "Mockburg", "Kreis")],
            ),
            # the issuing authority attests the record: boilerplate
            organization(
                "e8",
                "Standesamt Flörburg",
                kinds=["Standesamt"],
                places=["Flörburg"],
                extract=False,
            ),
            entity("m1", "Marriage of Sebastian and Hildegard", "marriage"),
            entity("m2", "Marriage of Eugen and Cäcilie", "marriage"),
        ],
        [
            ("e1", "date_of_birth", day(1862, 2, 15)),
            ("e1", "place_of_birth", item("e5")),
            ("e1", "father", item("e2")),
            ("e1", "mother", item("e3")),
            *union_of("m1", "e1", "e4"),
            *union_of("m2", "e2", "e3"),
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
            "m1": "sebastian-and-hildegard",
            "m2": "eugen-and-caecilie",
        },
    )


def birth_extract(*, dated: bool = True) -> Doc:
    claims = [
        ("e1", "father", item("e2")),
        ("e1", "mother", item("e3")),
        ("e1", "place_of_birth", item("e4")),
        ("e2", "residence", item("e5"), ("point_in_time", day(1919, 1, 8))),
    ]
    if dated:
        claims.append(("e1", "date_of_birth", day(1919, 1, 7)))
    return document(
        "birth-extract-1938",
        [
            human("e1", "Gustav Löbig"),
            human("e2", "Sebastian Löbig"),
            human(
                "e3",
                "Hildegard Löbig",
                aliases=["Hildegard Löbig geborene Laudler"],
                givens=["Hildegard"],
                surnames=["Löbig"],
                birth=["Laudler"],
            ),
            place("e4", "Flörburg"),
            place("e5", "Brunnenring 35", names=[], address="Brunnenring 35"),
            organization(
                "e6",
                "Standesamt Flörburg",
                kinds=["Standesamt"],
                places=["Flörburg"],
                extract=False,
            ),
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
            human(
                "e1",
                "Gustave Lobig",
                aliases=["Gustav Löbig"],
                givens=["Gustave", "Gustav"],
                surnames=["Lobig", "Löbig"],
            ),
            place(
                "e2",
                "8 Hawthorn Road, Stupfield, Victoria",
                names=[],
                address="8 Hawthorn Road",
                within=["Stupfield", "Victoria"],
            ),
            place("e3", "Floerburg"),
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
        *union_of("m1", "e2", "e3"),
    ]
    if dated:
        claims += [
            ("e1", "date_of_birth", day(1911)),
            ("e1", "date_of_death", day(1941)),
        ]
    return document(
        "family-tree-1984",
        [
            human("e1", "Ursula Löbig", lines=(3,)),
            human(
                "e2",
                "Sebastian Löbig",
                aliases=["Wastl"],
                lines=(2, 3),
                givens=["Sebastian"],
                surnames=["Löbig"],
                nicknames=["Wastl"],
            ),
            human(
                "e3",
                "Hildegard Laudler",
                aliases=["Hilde"],
                lines=(2,),
                givens=[given("Hildegard"), given("Hilde", "Hildegard")],
                surnames=["Laudler"],
            ),
            human(
                "e4",
                "Gustav Löbig",
                aliases=["Gustl"],
                lines=(3,),
                givens=[given("Gustav"), given("Gustl", "Gustav")],
                surnames=["Löbig"],
            ),
            entity("m1", "Marriage of Sebastian and Hildegard", "marriage"),
        ],
        claims,
        {
            "e1": "ursula-loebig",
            "e2": "sebastian-loebig",
            "e3": "hildegard-loebig",
            "e4": "gustav-loebig",
            "m1": "sebastian-and-hildegard",
        },
    )


def directory() -> Doc:
    return document(
        "directory-1912",
        [
            organization(
                "e1",
                "Hutmacherei zum Blauen Hut",
                names=["zum Blauen Hut"],
                kinds=["Hutmacherei"],
            ),
            human("e2", "Sebastian Löbig"),
            place("e3", "Brunnenring 35", names=[], address="Brunnenring 35"),
            human(
                "e4",
                "Ursula Löbig",
                aliases=["Löbig, Ursula, Wwe."],
                lines=(2,),
                givens=["Ursula"],
                surnames=["Löbig"],
                suffixes=["Wwe."],
            ),
            place("e5", "Angerring 57", names=[], address="Angerring 57"),
        ],
        [
            ("e4", "residence", item("e5"), ("point_in_time", day(1912))),
            (
                "e4",
                "civil_status",
                vocab("widowed"),
                ("point_in_time", day(1912)),
            ),
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
            human(
                "e1",
                "Ida Löbig",
                aliases=["LÖBIG, Ida, née SÖRNE"],
                givens=["Ida"],
                surnames=["Löbig", "LÖBIG"],
                birth=["SÖRNE"],
            ),
            human("e2", "Leopold Löbig"),
            place(
                "e3",
                "Peiltal court prison",
                names=[("Peiltal court prison", "Peiltal", "court prison")],
            ),
            place("e4", "Peiltal"),
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
        [place("e1", "Peiltal")],
        [],
        {"e1": "peiltal"},
    )


def studio_photo() -> Doc:
    """The names pencilled on the portrait's back: one name each."""
    return document(
        "studio-photo-1890s",
        [
            human("e1", "Muckel", nicknames=["Muckel"]),
            human("e2", "Cilli", nicknames=["Cilli"]),
            human("e3", "Wastl", nicknames=["Wastl"]),
            human("e4", "Rahel", givens=["Rahel"]),
            human("e5", "Felix", givens=["Felix"]),
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


# where the jurisdictions of the export's PLACs are in the universe: each
# is a place of its own (#571)
GEDCOM_JURISDICTIONS = {
    "Germany": "germany",
    "Hesse, Germany": "hesse",
    "Kreis Mockburg, Hesse, Germany": "kreis-mockburg",
    "Australia": "australia",
    "Victoria, Australia": "victoria",
    "Stupfield, Victoria, Australia": "stupfield",
    "Flörburg, Hesse, Germany": "floerburg",
}
# where the export's places and objects are in the universe
GEDCOM_TRUTH = {
    **GEDCOM_JURISDICTIONS,
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

GEDCOM_UNION_KINDS = {
    "partnership", "marriage", "wedding", "civil_wedding",
    "religious_wedding", "divorce", "annulment", "event",
}  # fmt: skip
# the export's unions and their events, by the adapter's id, as this
# file's documents name them; any other is the export's own
GEDCOM_UNIONS = {
    "f1": "eugen-and-caecilie",
    "f2": "sebastian-and-hildegard",
    "f5": "leopold-and-agnes",
    "f5-civil-wedding": "leopold-and-agnes-wedding",
    "f5-divorce": "leopold-and-agnes-divorce",
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
    # a union and its events are as this file's documents name them
    # (GEDCOM_UNIONS), or else the export's own (ADR-0036)
    truth = {
        record.id: people.get(record.id)
        or (
            GEDCOM_UNIONS.get(record.id, f"gedcom-{record.id}")
            if record.kind in GEDCOM_UNION_KINDS
            else GEDCOM_TRUTH[record.label]
        )
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
    # two documents of one SHA-1 are one: the second would be dropped, and
    # a test comparing them would compare nothing
    sha1s = [doc.sha1 for doc in docs]
    assert len(set(sha1s)) == len(sha1s), "two documents share a SHA-1"
    config = load_config(overrides=overrides) if overrides else None
    return cluster_corpus([doc.manifest for doc in docs], config)


def pair(first: Doc, first_id: str, second: Doc, second_id: str):
    return frozenset({(first.sha1, first_id), (second.sha1, second_id)})


def sha1_text(name: str) -> str:
    """The SHA-1 a placeholder register of this name is cited by."""
    return hashlib.sha1(name.encode()).hexdigest()


def register(name: str, entities, claims, truth) -> Doc:
    """A placeholder document, cited by the SHA-1 of its name, for a
    pattern the universe's sources do not show."""
    sha1 = hashlib.sha1(name.encode()).hexdigest()
    return document("a register", entities, claims, truth, sha1=sha1)


def export(records: str, truth: dict[str, str], key: str = "gedcom") -> Doc:
    """A GEDCOM export of `records`, adapted by the pipeline's adapter,
    which fills its records; `truth` names who each person (by id) or
    place (by string) is, and any union or event of the export is its
    own."""
    text = "0 HEAD\n1 CHAR UTF-8\n" + records + "0 TRLR\n"
    artifact = ArtifactRef.from_bytes(text.encode())
    batch = adapt_gedcom(text, artifact).batch
    truth = {**GEDCOM_JURISDICTIONS, **truth}
    return Doc(
        key,
        Manifest(artifact.sha1, batch, GEDCOM),
        {
            record.id: truth.get(record.id)
            or (
                f"gedcom-{record.id}"
                if record.kind in GEDCOM_UNION_KINDS
                and record.label not in truth
                else truth[record.label]
            )
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


def links_of(identity: dict) -> dict[frozenset[str], tuple[str, str]]:
    """Every reviewed pair's (link, evidence), by its two mentions."""
    return {
        frozenset(p["mentions"]): (p["link"], p["evidence"])
        for entry in identity["review"]
        for p in entry["pairs"]
    }


def judge_of(*docs):
    """The judge of a corpus of `docs`, and each mention's node by its
    document and local id."""
    config = load_config()
    corpus = clustering._corpus([doc.manifest for doc in docs], config)
    judge = clustering._Judge(corpus.index, {}, config)
    nodes = {(node.sha1, node.mention.local_id): node for node in corpus.nodes}
    return judge, lambda doc, local_id: nodes[(doc.sha1, local_id)]


# -- keys ---------------------------------------------------------------------


class TestKeys:
    def test_people_block_on_a_given_name_with_a_surname_never_a_token(
        self,
    ):
        extract, tree, book = birth_extract(), family_tree(), directory()
        pairs = blocked_pairs([extract.manifest, tree.manifest, book.manifest])
        # the same person by given name and surname
        assert pair(extract, "e2", tree, "e2") in pairs
        assert pair(extract, "e1", tree, "e4") in pairs
        # the extract's birth surname Laudler, the tree's name for her
        assert pair(extract, "e3", tree, "e3") in pairs
        # a surname alone never blocks: Sebastian, Ursula and Gustav share
        # only "Löbig"
        assert pair(book, "e2", tree, "e1") not in pairs
        assert pair(extract, "e1", tree, "e1") not in pairs
        assert pair(book, "e4", extract, "e2") not in pairs

    def test_the_record_says_which_part_is_which_never_the_order(self):
        """The death certificate writes 'Лёбиг Рудольф Феликсович',
        surname first and with no comma; its record gives the given name,
        the surname and the patronymic, so it meets a register's 'Rudolf'
        of that surname, spelled alike, and the patronymic keys as a
        surname does. The same words, read in their order, would key the
        surname as a given name."""
        certificate = document(
            "death-certificate-1941",
            [
                human(
                    "e1",
                    "Лёбиг Рудольф Феликсович",
                    givens=["Рудольф"],
                    surnames=["Лёбиг"],
                    patronymics=["Феликсович"],
                )
            ],
            [],
            {"e1": "rudolf-loebig"},
        )
        letter = register(
            "patronymic",
            [
                human(
                    "e1",
                    "Рудольф Феликсович",
                    givens=["Рудольф"],
                    patronymics=["Феликсович"],
                )
            ],
            [],
            {"e1": "rudolf-loebig"},
        )
        by_surname = register(
            "surname",
            [human("e1", "Рудольф Лёбиг")],
            [],
            {"e1": "rudolf-loebig"},
        )
        reversed_ = register(
            "reversed",
            [human("e1", "Лёбиг Рудольф")],
            [],
            {"e1": "someone"},
        )
        pairs = blocked_pairs(
            [
                doc.manifest
                for doc in (certificate, letter, by_surname, reversed_)
            ]
        )
        assert pair(certificate, "e1", letter, "e1") in pairs
        assert pair(certificate, "e1", by_surname, "e1") in pairs
        assert pair(certificate, "e1", reversed_, "e1") not in pairs

    def test_no_key_pairs_two_parts_that_fold_alike(self):
        """A register writes 'Löbig Ursula' and cannot tell which word is
        which: its unsplit form keys (Löbig, Ursula) and (Ursula, Löbig),
        never (Löbig, Löbig) with a kin's surname, and a given name that
        folds like a surname makes no key with it."""
        ursula = register(
            "ursula",
            [
                human("e1", "Löbig Ursula", unsplit=["Löbig Ursula"]),
                human("e2", "Hildegard Löbig"),
            ],
            [("e1", "mother", item("e2"))],
            {"e1": "ursula-loebig", "e2": "hildegard-loebig"},
        )
        gustav = register(
            "gustav",
            [
                human("e1", "Löbig Gustav", unsplit=["Löbig Gustav"]),
                human("e2", "Sebastian Löbig"),
            ],
            [("e1", "father", item("e2"))],
            {"e1": "gustav-loebig", "e2": "sebastian-loebig"},
        )
        daughter = register(
            "daughter", [human("e1", "Ursula Löbig")], [], {"e1": "ursula"}
        )
        pairs = blocked_pairs(
            [ursula.manifest, gustav.manifest, daughter.manifest]
        )
        assert pair(ursula, "e1", gustav, "e1") not in pairs
        assert pair(ursula, "e1", daughter, "e1") in pairs
        # an unsplit form never pairs with a kin's surname, even one that
        # folds like none of its words: (Ursula, Nertler) is no key
        nertler = register(
            "nertler",
            [
                human("e1", "Löbig Ursula", unsplit=["Löbig Ursula"]),
                human("e2", "Hildegard Nertler"),
            ],
            [("e1", "mother", item("e2"))],
            {"e1": "ursula-loebig", "e2": "hildegard"},
        )
        namesake = register(
            "namesake", [human("e1", "Ursula Nertler")], [], {"e1": "ursula"}
        )
        pairs = blocked_pairs([nertler.manifest, namesake.manifest])
        assert pair(nertler, "e1", namesake, "e1") not in pairs
        # a given name the record's own surname folds to
        same = register(
            "same",
            [human("e1", "Lobig Löbig", givens=["Lobig"], surnames=["Löbig"])],
            [],
            {"e1": "someone"},
        )
        other = register(
            "other",
            [human("e1", "Lobig Löbig", givens=["Lobig"], surnames=["Löbig"])],
            [],
            {"e1": "someone else"},
        )
        assert blocked_pairs([same.manifest, other.manifest]) == set()

    def test_a_given_part_folding_like_a_surname_pairs_with_no_kin(self):
        """Two registers write the sisters 'Löbig Ursula' and 'Löbig
        Klara', each daughter of Sebastian Löbig and Hildegard Laudler,
        and misread 'Löbig' as a given name. A given part that folds like
        an own or a kin's surname pairs with no kin's surname (ADR-0035
        §7, rule 1, as decided 2026-09-29), so neither keys (Löbig,
        Laudler), and the sisters never meet."""

        def sister(name: str) -> Doc:
            return register(
                name,
                [
                    human("e1", f"Löbig {name}", givens=["Löbig", name]),
                    human("e2", "Sebastian Löbig"),
                    human("e3", "Hildegard Laudler"),
                ],
                [
                    ("e1", "father", item("e2")),
                    ("e1", "mother", item("e3")),
                ],
                {
                    "e1": name,
                    "e2": "sebastian-loebig",
                    "e3": "hildegard-loebig",
                },
            )

        docs = [sister("Ursula"), sister("Klara")]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        pairs = blocked_pairs([doc.manifest for doc in docs])
        assert pair(docs[0], "e1", docs[1], "e1") not in pairs
        # a given part that folds like no surname still pairs with kin
        plain = register(
            "plain",
            [
                human("e1", "Ursula", givens=["Ursula"]),
                human("e2", "Sebastian Löbig"),
            ],
            [("e1", "father", item("e2"))],
            {"e1": "Ursula", "e2": "sebastian-loebig"},
        )
        pairs = blocked_pairs([plain.manifest, docs[0].manifest])
        assert pair(plain, "e1", docs[0], "e1") in pairs

        def variant(key: str, name: str, father, own=()) -> Doc:
            return register(
                f"{key} {name}",
                [
                    human(
                        "e1",
                        f"Löbig {name}",
                        givens=["Löbig", name],
                        surnames=list(own),
                    ),
                    human("e3", "Hildegard Laudler"),
                    *([father] if father else []),
                ],
                [("e1", "mother", item("e3"))]
                + ([("e1", "father", item("e2"))] if father else []),
                {"e1": name, "e2": "sebastian-loebig", "e3": "hildegard"},
            )

        for key, father, own in (
            # the father spelt as the naturalisation spells him
            ("Lobig", human("e2", "Sebastian Lobig"), ()),
            # Löbig her own surname too, with only her mother named
            ("own", None, ("Löbig",)),
            # a father without a record: his label's words bar it too
            ("recordless", entity("e2", "Sebastian Löbig"), ()),
        ):
            docs = [variant(key, n, father, own) for n in ("Ursula", "Klara")]
            pairs = blocked_pairs([doc.manifest for doc in docs])
            assert pair(docs[0], "e1", docs[1], "e1") not in pairs, key
        # a given name that folds like a kin's surname still pairs with
        # its own surname: two registers' Zzz Yyy, child of Www Zzz, merge
        zzz = [
            register(
                f"zzz {n}",
                [human("e1", "Zzz Yyy"), human("e2", "Www Zzz")],
                [("e1", "mother", item("e2"))],
                {"e1": "zzz", "e2": "www"},
            )
            for n in range(2)
        ]
        assert {"zzz"} in merged(zzz, identity_of(*zzz))

    def test_a_written_form_two_standard_forms_part_never_decides(self):
        """Two 'Gustl Löbig', sons of Sebastian Löbig, one whose record
        gives the standard form Gustav and one Xxx: they meet on the
        written form, and stay ambiguous, since the standard forms share
        no key (decided 2026-10-02). So do two Gustav Löbigs whose
        fathers are 'Wastl' read as Sebastian and as Xxx: the fathers'
        kin agree on nothing. Two brothers, Xxx and Gustav, each called
        Gustl at home, never merge on the nickname."""

        def son(key: str, child: dict, father: dict) -> Doc:
            return register(
                key,
                [child, father],
                [("e1", "father", item("e2"))],
                {"e1": key, "e2": "sebastian-loebig"},
            )

        def gustl(standard: str) -> dict:
            return human(
                "e1",
                "Gustl Löbig",
                givens=[given("Gustl", standard)],
                surnames=["Löbig"],
            )

        sebastian = human("e2", "Sebastian Löbig")
        docs = [
            son("Gustav", gustl("Gustav"), sebastian),
            son("Xxx", gustl("Xxx"), sebastian),
        ]
        identity = identity_of(*docs)
        sons = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[sons] == (AMBIGUOUS, "weak/unknown/agree")
        assert_no_false_merge(docs, identity)

        def wastl(standard: str) -> dict:
            return human(
                "e2",
                "Wastl Löbig",
                givens=[given("Wastl", standard)],
                surnames=["Löbig"],
            )

        docs = [
            register(
                f"son of {standard}",
                [human("e1", "Gustav Löbig"), wastl(standard)],
                [("e1", "father", item("e2"))],
                {"e1": "gustav-loebig", "e2": f"father {standard}"},
            )
            for standard in ("Sebastian", "Xxx")
        ]
        identity = identity_of(*docs)
        children = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[children][0] == AMBIGUOUS
        assert_no_false_merge(docs, identity)
        brothers = [
            son(
                name,
                human(
                    "e1",
                    f"{name} Löbig",
                    aliases=["Gustl"],
                    givens=[name],
                    surnames=["Löbig"],
                    nicknames=["Gustl"],
                ),
                sebastian,
            )
            for name in ("Gustav", "Xxx")
        ]
        identity = identity_of(*brothers)
        both = frozenset(doc.fingerprint("e1", identity) for doc in brothers)
        assert links_of(identity)[both] == (AMBIGUOUS, "weak/unknown/agree")
        assert_no_false_merge(brothers, identity)

        # rule 3's own case: "Gustav 'Gustl' Löbig" against a "Gustl
        # Löbig" whose record gives no standard form, or Gustav, merges on
        # the father; against one read as Xxx it stays weak. In both
        # document orders.
        called = human(
            "e1",
            "Gustav Löbig",
            aliases=["Gustl"],
            givens=["Gustav"],
            surnames=["Löbig"],
            nicknames=["Gustl"],
        )
        for standard, merges in (
            (None, True),
            ("Gustav", True),
            ("Xxx", False),
        ):
            for keys in (("called", "plain"), ("plain", "called")):
                docs = [
                    son(f"{keys[0]} {standard}", called, sebastian),
                    son(f"{keys[1]} {standard}", gustl(standard), sebastian),
                ]
                identity = identity_of(*docs)
                sons = frozenset(
                    doc.fingerprint("e1", identity) for doc in docs
                )
                if merges:
                    assert {
                        identity["mentions"][m]["method"] for m in sons
                    } == {CONFIDENT}, (standard, keys)
                else:
                    assert links_of(identity)[sons] == (
                        AMBIGUOUS,
                        "weak/unknown/agree",
                    ), keys
        # nor do two fathers named Sebastian Löbig, one with each such son,
        # merge through their sons, in either document order
        for first, second in (("a", "b"), ("b", "a")):
            docs = [
                register(
                    f"father {key}",
                    [human("e1", "Sebastian Löbig"), {**child, "id": "e2"}],
                    [("e2", "father", item("e1"))],
                    {"e1": f"sebastian {key}", "e2": f"son {key}"},
                )
                for key, child in ((first, called), (second, gustl("Xxx")))
            ]
            identity = identity_of(*docs)
            assert merged(docs, identity) == [], (first, second)
        # a nickname holder with no given names imposes no bar: a "Wastl"
        # read as Sebastian against a nickname-only "Wastl Löbig" keeps
        # the key (decided 2026-10-02)
        wastl = register(
            "wastl read as Sebastian",
            [
                human(
                    "e1",
                    "Wastl Löbig",
                    givens=[given("Wastl", "Sebastian")],
                    surnames=["Löbig"],
                )
            ],
            [],
            {"e1": "sebastian-loebig"},
        )
        nickname_holder = register(
            "wastl as a nickname",
            [
                human(
                    "e1",
                    "Wastl Löbig",
                    surnames=["Löbig"],
                    nicknames=["Wastl"],
                )
            ],
            [],
            {"e1": "sebastian-loebig"},
        )
        for docs in ([wastl, nickname_holder], [nickname_holder, wastl]):
            identity = identity_of(*docs)
            both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
            assert links_of(identity)[both] == (
                AMBIGUOUS,
                "key/unknown/unknown",
            )
        # whichever of the two is weighed first
        judge, node = judge_of(wastl, nickname_holder)
        a, b = node(wastl, "e1"), node(nickname_holder, "e1")
        assert judge.name_level(a, b) == judge.name_level(b, a) == "key"
        # a word of a document's unsplit form is a given part with no
        # standard form: against "Sebastian 'Wastl' Löbig" the key counts
        unsplit = register(
            "wastl unsplit",
            [human("e1", "Wastl Löbig", unsplit=["Wastl Löbig"])],
            [],
            {"e1": "sebastian-loebig"},
        )
        called_wastl = register(
            "sebastian called wastl",
            [
                human(
                    "e1",
                    "Sebastian Löbig",
                    aliases=["Wastl"],
                    givens=["Sebastian"],
                    surnames=["Löbig"],
                    nicknames=["Wastl"],
                )
            ],
            [],
            {"e1": "sebastian-loebig"},
        )
        for docs in ([unsplit, called_wastl], [called_wastl, unsplit]):
            identity = identity_of(*docs)
            both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
            assert links_of(identity)[both] == (
                AMBIGUOUS,
                "key/unknown/unknown",
            )
        # the words of an unsplit form are given names the nickname is
        # weighed against: a brother written unsplit "Xxx Löbig", also
        # called Gustl, never merges with "Gustav 'Gustl'", whichever
        # document sorts first
        unsplit_brother = human(
            "e1",
            "Xxx Löbig",
            aliases=["Gustl"],
            unsplit=["Xxx Löbig"],
            nicknames=["Gustl"],
        )
        for keys in (("called", "unsplit"), ("unsplit", "called")):
            docs = [
                son(f"{keys[0]} brother", called, sebastian),
                son(f"{keys[1]} brother", unsplit_brother, sebastian),
            ]
            identity = identity_of(*docs)
            both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
            assert links_of(identity)[both][0] == AMBIGUOUS, keys
            assert_no_false_merge(docs, identity)
        # a nickname alone, against "Gustav 'Gustl'", merges on the father
        nickname_only = human(
            "e1", "Gustl Löbig", surnames=["Löbig"], nicknames=["Gustl"]
        )
        docs = [
            son("called", called, sebastian),
            son("nickname only", nickname_only, sebastian),
        ]
        identity = identity_of(*docs)
        sons = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert {identity["mentions"][m]["method"] for m in sons} == {CONFIDENT}

    def test_the_exports_nickname_holders_are_weighed_by_given_names(self):
        """An export's 'Ida /Löbig/', called Peppi and born 1911, and a
        register's Klara Löbig, called Peppi and born 1911: the two give
        given names that share no key, so the shared nickname never makes
        the link confident, whichever sorts first. Nor does an export's
        slashless 'Xxx Löbig', called Gustl, son of Sebastian Löbig, link
        confidently with the register's 'Gustav 'Gustl' Löbig', his son
        too: the words of a NAME without slashes are its given names."""
        ged = export(
            "0 @I7@ INDI\n1 NAME Ida /Löbig/\n2 NICK Peppi\n"
            "1 BIRT\n2 DATE 1911\n",
            {"i7": "ida-loebig"},
        )
        brothers = export(
            "0 @I3@ INDI\n1 NAME Sebastian /Löbig/\n1 FAMS @F1@\n"
            "0 @I8@ INDI\n1 NAME Xxx Löbig\n2 NICK Gustl\n1 FAMC @F1@\n"
            "0 @F1@ FAM\n1 HUSB @I3@\n1 CHIL @I8@\n",
            {"i3": "sebastian-loebig", "i8": "xxx-loebig"},
            key="brothers",
        )

        def klara(key: str) -> Doc:
            return register(
                key,
                [
                    human(
                        "e1",
                        "Klara Löbig",
                        aliases=["Peppi"],
                        givens=["Klara"],
                        surnames=["Löbig"],
                        nicknames=["Peppi"],
                    )
                ],
                [("e1", "date_of_birth", day(1911))],
                {"e1": "klara-loebig"},
            )

        def gustav(key: str) -> Doc:
            return register(
                key,
                [
                    human(
                        "e1",
                        "Gustav Löbig",
                        aliases=["Gustl"],
                        givens=["Gustav"],
                        surnames=["Löbig"],
                        nicknames=["Gustl"],
                    ),
                    human("e2", "Sebastian Löbig"),
                ],
                [("e1", "father", item("e2"))],
                {"e1": "gustav-loebig", "e2": "sebastian-loebig"},
            )

        for ged_doc, make, ged_id, want in (
            (ged, klara, "i7", "weak/agree/unknown"),
            (brothers, gustav, "i8", "weak/unknown/agree"),
        ):
            # a register whose SHA-1 sorts before the export's, and one after
            keys = [f"{make.__name__} {n}" for n in range(500)]
            before = next(k for k in keys if sha1_text(k) < ged_doc.sha1)
            after = next(k for k in keys if sha1_text(k) > ged_doc.sha1)
            for key in (before, after):
                docs = [ged_doc, make(key)]
                identity = identity_of(*docs)
                both = frozenset(
                    (
                        ged_doc.fingerprint(ged_id, identity),
                        docs[1].fingerprint("e1", identity),
                    )
                )
                assert links_of(identity)[both] == (AMBIGUOUS, want), key
                assert_no_false_merge(docs, identity)

    def test_given_names_match_by_standard_form_never_by_prefix(self):
        """Hilde never meets Hildegard by its first letters, nor Gustl
        Gustav; each meets its full name through the standard form its
        document supports. Gustav and Gustave are two spellings, and meet
        only where one record gives both."""
        names = {
            "gustl": human("e1", "Gustl Löbig"),
            "gustl-standard": human(
                "e1",
                "Gustl Löbig",
                givens=[given("Gustl", "Gustav")],
                surnames=["Löbig"],
            ),
            "gustav": human("e1", "Gustav Löbig"),
            "gustave": human("e1", "Gustave Löbig"),
            "hilde": human("e1", "Hilde Löbig"),
            "hildegard": human("e1", "Hildegard Löbig"),
        }
        docs = {
            key: register(key, [record], [], {"e1": key})
            for key, record in names.items()
        }
        pairs = blocked_pairs([doc.manifest for doc in docs.values()])

        def met(first: str, second: str) -> bool:
            return pair(docs[first], "e1", docs[second], "e1") in pairs

        assert not met("gustl", "gustav")
        assert met("gustl-standard", "gustav")
        assert not met("hilde", "hildegard")
        assert not met("gustav", "gustave")
        both = register(
            "both",
            [naturalisation().manifest.batch.entities[0].model_dump()],
            [],
            {},
        )
        again = blocked_pairs(
            [both.manifest, docs["gustav"].manifest, docs["gustave"].manifest]
        )
        assert pair(both, "e1", docs["gustav"], "e1") in again
        assert pair(both, "e1", docs["gustave"], "e1") in again

    def test_a_relational_appellation_never_keys(self):
        """A name the source gives through a relation ('Рудольфиха',
        Rudolf's wife) is a `referred_to_as` claim, never an alias or a
        record nickname (ADR-0018): clustering keys on neither, so two
        women the sources only call that never meet through it."""

        def widow(key: str, name: str) -> Doc:
            return register(
                key,
                [human("e1", name)],
                [
                    (
                        "e1",
                        "referred_to_as",
                        {
                            "type": "monolingual",
                            "value": {"text": "Рудольфиха", "language": "ru"},
                        },
                    )
                ],
                {"e1": key},
            )

        docs = [widow("nina", "Nina Löbig"), widow("other", "Xxx Yyy")]
        assert blocked_pairs([doc.manifest for doc in docs]) == set()
        # nor without records, where a mention keys on its exact names
        recordless = [
            register(
                key,
                [entity("e1", name)],
                [
                    (
                        "e1",
                        "referred_to_as",
                        {
                            "type": "monolingual",
                            "value": {"text": "Рудольфиха", "language": "ru"},
                        },
                    )
                ],
                {"e1": key},
            )
            for key, name in (("nina bare", "Nina Löbig"), ("xxx", "Xxx Yyy"))
        ]
        assert blocked_pairs([doc.manifest for doc in recordless]) == set()

    def test_a_given_name_keys_as_written_too(self):
        """Two registers write 'Gustl Löbig', son of Sebastian Löbig; one
        record gives Gustl the standard form Gustav, the other none. A
        given name keys by its written form as well as its standard form
        (ADR-0035 §7, rule 3, as decided 2026-09-29), so the two meet.
        Meeting is not merging: the pair is judged, and merges on the
        father they agree on; without him it stays ambiguous."""

        def gustl(key: str, standard: str | None, father: bool) -> Doc:
            return register(
                key,
                [
                    human(
                        "e1",
                        "Gustl Löbig",
                        givens=[given("Gustl", standard)],
                        surnames=["Löbig"],
                    ),
                    human("e2", "Sebastian Löbig"),
                ],
                [("e1", "father", item("e2"))] if father else [],
                {"e1": "gustav-loebig", "e2": "sebastian-loebig"},
            )

        docs = [
            gustl("standard", "Gustav", True),
            gustl("as written", None, True),
        ]
        pairs = blocked_pairs([doc.manifest for doc in docs])
        assert pair(docs[0], "e1", docs[1], "e1") in pairs
        assert {"gustav-loebig"} in merged(docs, identity_of(*docs))
        # in the other document order too
        docs = [
            gustl("as written", "Gustav", True),
            gustl("standard", None, True),
        ]
        assert {"gustav-loebig"} in merged(docs, identity_of(*docs))
        alone = [
            gustl("standard", "Gustav", False),
            gustl("as written", None, False),
        ]
        identity = identity_of(*alone)
        gustls = frozenset(doc.fingerprint("e1", identity) for doc in alone)
        assert links_of(identity)[gustls] == (AMBIGUOUS, "key/unknown/unknown")

    def test_an_umlaut_also_keys_written_out(self):
        """DIN 5007-2: 'Löbig' keys as 'lobig' and 'loebig', so it meets
        'Lobig' and 'Loebig', and 'Flörburg' meets 'Floerburg'; a
        two-letter spelling is never turned back, so 'Loebig' and 'Lobig'
        stay apart."""
        assert part_keys("Löbig") == {"lobig", "loebig"}
        assert part_keys("LÖBIG,") == {"lobig", "loebig"}
        assert part_keys("Loebig") == {"loebig"}
        assert part_keys("Cäcilie") == {"cacilie", "caecilie"}
        # a decomposed umlaut is one too
        assert part_keys(unicodedata.normalize("NFD", "Pünktchen")) == {
            "punktchen",
            "puenktchen",
        }
        assert part_keys("Straße") == {"strasse"}
        docs = {
            spelling: register(
                spelling, [human("e1", f"Gustav {spelling}")], [], {}
            )
            for spelling in ("Löbig", "Lobig", "Loebig")
        }
        pairs = blocked_pairs([doc.manifest for doc in docs.values()])
        assert pair(docs["Löbig"], "e1", docs["Lobig"], "e1") in pairs
        assert pair(docs["Löbig"], "e1", docs["Loebig"], "e1") in pairs
        assert pair(docs["Lobig"], "e1", docs["Loebig"], "e1") not in pairs
        towns = [
            register(name, [place("e1", name)], [], {"e1": "floerburg"})
            for name in ("Flörburg", "Floerburg")
        ]
        identity = identity_of(*towns)
        assert merged(towns, identity) == [{"floerburg"}]

    def test_a_married_name_keys_through_the_kin(self):
        """Agnes by her married name in one document and her birth name in
        another pair through her husband's surname."""
        judgment = document(
            "divorce-judgment-1957",
            [
                human("e1", "Leopold Löbig"),
                human("e2", "Agnes", givens=["Agnes"]),
                entity("m1", "Marriage of Leopold and Agnes", "marriage"),
            ],
            union_of("m1", "e1", "e2"),
            {"e1": "leopold-loebig", "e2": "agnes-loebig"},
        )
        tree = document(
            "family-tree-1984",
            [
                human("e1", "Leopold Löbig"),
                human(
                    "e2", "Agnes Nertler", givens=["Agnes"], birth=["Nertler"]
                ),
                human("e3", "Agnes Löbig"),
            ],
            [],
            {"e1": "leopold-loebig", "e2": "agnes-loebig", "e3": "agnes"},
        )
        pairs = blocked_pairs([judgment.manifest, tree.manifest])
        assert pair(judgment, "e2", tree, "e3") in pairs
        assert pair(judgment, "e2", tree, "e2") not in pairs

    def test_only_a_name_keys(self):
        """A role ('INFORMANT'), a kinship term ('Dein Vater'), a
        courtesy ('Nana Maisie'), a placeholder ('N.N. Löbig') and a
        description ('Klara's father') key nothing, whatever parts they
        hold; nor does a surname with a title ('Frau Löbig')."""
        statuses = (
            "role",
            "kinship",
            "courtesy",
            "placeholder",
            "description",
        )
        docs = [
            register(
                f"{status}-{n}",
                [
                    human(
                        "e1",
                        "Klara Löbig",
                        givens=["Klara"],
                        surnames=["Löbig"],
                        status=status,
                    )
                ],
                [],
                {"e1": f"{status}-{n}"},
            )
            for status in statuses
            for n in range(2)
        ]
        titled = [
            register(
                f"frau-{n}",
                [
                    human(
                        "e1", "Frau Löbig", titles=["Frau"], surnames=["Löbig"]
                    )
                ],
                [],
                {"e1": f"frau-{n}"},
            )
            for n in range(2)
        ]
        assert blocked_pairs([doc.manifest for doc in docs + titled]) == set()

    def test_places_key_on_their_names_never_their_containers(self):
        """The export's towns share only "Hesse, Germany", their
        containers: no pair. Two places named alike pair, whatever their
        containers say."""
        ged, card, talk = gedcom(), registration_card(), interview()
        pairs = blocked_pairs([ged.manifest, card.manifest, talk.manifest])
        places = {
            record.label: record.id
            for record in ged.manifest.batch.entities
            if record.kind == "place"
        }
        kredfeld = places["Kredfeld, Hesse, Germany"]
        floerburg = places["Flörburg, Hesse, Germany"]
        bad = places["Bad Flörburg, Hesse, Germany"]
        assert pair(ged, kredfeld, ged, floerburg) not in pairs
        # a name that holds another's is another name
        assert pair(ged, bad, ged, floerburg) not in pairs
        assert pair(card, "e4", talk, "e1") in pairs
        # the prison's name is its own, its toponym no key
        assert pair(card, "e3", talk, "e1") not in pairs

    def test_a_camps_short_form_keys_nothing(self):
        """The compensation file's camp is 'Lager Klurnau', and its
        manifest keeps a bare 'Klurnau' for it too, the toponym of its
        name: that short form keys nothing, so the camp never meets the
        town; a generic head keys nothing either."""
        camp = document(
            "compensation-file-1953",
            [
                place(
                    "e1",
                    "Lager Klurnau",
                    aliases=["Klurnau"],
                    names=[("Lager Klurnau", "Klurnau", "Lager"), "Klurnau"],
                )
            ],
            [],
            {"e1": "klurnau-camp"},
        )
        card = document(
            "registration-card-1945",
            [place("e1", "Klurnau")],
            [],
            {"e1": "klurnau"},
        )
        other_camp = register(
            "another camp",
            [
                place(
                    "e1",
                    "Lager Klurnau",
                    names=[("Lager Klurnau", "Klurnau", "Lager")],
                )
            ],
            [],
            {"e1": "klurnau-camp"},
        )
        heads = [
            register(
                f"head-{n}",
                [place("e1", "Lager", names=[], status="generic")],
                [],
                {"e1": f"camp-{n}"},
            )
            for n in range(2)
        ]
        pairs = blocked_pairs(
            [doc.manifest for doc in (camp, card, other_camp, *heads)]
        )
        assert pair(camp, "e1", card, "e1") not in pairs
        assert pair(camp, "e1", other_camp, "e1") in pairs
        assert not [p for p in pairs if (heads[0].sha1, "e1") in p]

    def test_a_country_given_by_an_adjective_keys_nothing(self):
        """#573: "an Australian citizen" gives a place labelled with the
        adjective, which is no name of the country, so two such places
        never pair; as toponyms they would."""

        def countries(status: str) -> list[Doc]:
            return [
                register(
                    f"citizen-{status}-{n}",
                    [
                        place(
                            "e1",
                            "Australian",
                            names=["Australian"],
                            status=status,
                        )
                    ],
                    [],
                    {"e1": "australia"},
                )
                for n in range(2)
            ]

        adjectives = countries("adjective")
        pairs = blocked_pairs([doc.manifest for doc in adjectives])
        assert pair(adjectives[0], "e1", adjectives[1], "e1") not in pairs
        toponyms = countries("toponym")
        pairs = blocked_pairs([doc.manifest for doc in toponyms])
        assert pair(toponyms[0], "e1", toponyms[1], "e1") in pairs

    def test_an_address_keys_as_written(self):
        """The naturalisation's address meets the export's place of that
        name, which lies in more containers, and nests in it."""
        ged = gedcom()
        papers = naturalisation()
        identity = identity_of(ged, papers)
        assert {"hawthorn-road-8"} in merged([ged, papers], identity)

    def test_a_toponym_its_written_form_gives_still_keys(self):
        """A record that gives Flörburg the toponym Flörburg (no head)
        still keys on it, and meets a plain Flörburg."""
        toponym = register(
            "toponym",
            [place("e1", "Flörburg", names=[("Flörburg", "Flörburg", None)])],
            [],
            {"e1": "floerburg"},
        )
        plain = register(
            "plain", [place("e1", "Flörburg")], [], {"e1": "floerburg"}
        )
        pairs = blocked_pairs([toponym.manifest, plain.manifest])
        assert pairs == {pair(toponym, "e1", plain, "e1")}

    @pytest.mark.parametrize(
        "within", [["Fryslân"], ["Drakeradiel", "Fryslân"]]
    )
    @pytest.mark.parametrize("toponym", [None, "Snylsjum"])
    def test_a_label_and_container_name_keys_as_its_label(
        self, toponym, within
    ):
        """#683: the 1952 letter's "Snylsjum, Fryslân", labelled Snylsjum
        within Fryslân, keys as Snylsjum, as alias hygiene reads it, and
        meets a Snylsjum written without its container; Fryslân need not
        be the record's first container."""
        letter = register(
            "letter",
            [
                place(
                    "e1",
                    "Snylsjum",
                    names=[("Snylsjum, Fryslân", toponym, None)],
                    within=within,
                )
            ],
            [],
            {"e1": "snylsjum"},
        )
        plain = register(
            "plain", [place("e1", "Snylsjum")], [], {"e1": "snylsjum"}
        )
        pairs = blocked_pairs([letter.manifest, plain.manifest])
        assert pairs == {pair(letter, "e1", plain, "e1")}

    def test_a_later_label_and_container_name_keys_as_its_label(self):
        """A label-and-container name that is not the record's first name
        keys as the label too: the province, named Friesland and
        "Fryslân, Nederlân", meets a Fryslân written without its
        container."""
        province = register(
            "province",
            [
                place(
                    "e1",
                    "Fryslân",
                    aliases=["Friesland"],
                    names=[
                        "Friesland",
                        ("Fryslân, Nederlân", "Fryslân", None),
                    ],
                    within=["Nederlân"],
                )
            ],
            [],
            {"e1": "fryslan"},
        )
        plain = register(
            "plain", [place("e1", "Fryslân")], [], {"e1": "fryslan"}
        )
        pairs = blocked_pairs([province.manifest, plain.manifest])
        assert pairs == {pair(province, "e1", plain, "e1")}

    def test_a_name_that_adds_no_container_keys_as_written(self):
        """A written name that adds a word that is no container of the
        record keys whole, and does not meet the bare label."""
        written = register(
            "written",
            [
                place(
                    "e1",
                    "Snylsjum",
                    names=["Snylsjum, Drakeradiel"],
                    within=["Fryslân"],
                )
            ],
            [],
            {"e1": "snylsjum-drakeradiel"},
        )
        plain = register(
            "plain", [place("e1", "Snylsjum")], [], {"e1": "snylsjum"}
        )
        assert blocked_pairs([written.manifest, plain.manifest]) == set()

    def test_an_address_keys_whole_never_by_its_street(self):
        """Brunnenring 35 and Brunnenring 12 share a street, and are two
        addresses: an address keys as written, never by its street."""

        def address(number: str) -> Doc:
            written = f"Brunnenring {number}"
            record = place_record(address=written)
            record["address"] |= {"street": "Brunnenring", "number": number}
            return register(
                written,
                [entity("e1", written, "place", structure=record)],
                [],
                {"e1": written},
            )

        docs = [address("35"), address("12"), address("35")]
        docs[2] = register(
            "Brunnenring 35 again",
            docs[2].manifest.batch.model_dump(mode="json")["entities"],
            [],
            {"e1": "Brunnenring 35"},
        )
        pairs = blocked_pairs([doc.manifest for doc in docs])
        assert pairs == {pair(docs[0], "e1", docs[2], "e1")}

    def test_a_person_of_one_name_pairs_only_with_the_export(self):
        """The portrait's pencilled names meet the export's people by
        given name or nickname, and nobody else, and never merge on it."""
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
        identity = identity_of(ged, photo)
        assert links_of(identity)[
            frozenset(
                {
                    photo.fingerprint("e3", identity),
                    ged.fingerprint("i3", identity),
                }
            )
        ] == (AMBIGUOUS, "given/unknown/unknown")

    def test_a_person_with_a_surname_never_meets_by_given_name_alone(self):
        """A register's Gustav Löbig meets the export's Gustav Löbig, and
        never its Gustav of another surname: only a person with no
        surname meets the export by given name."""
        ged = export(
            "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n"
            "0 @I2@ INDI\n1 NAME Gustav /Nertler/\n",
            {"i8": "gustav-loebig", "i2": "someone"},
        )
        book = register(
            "gustav",
            [human("e1", "Gustav Löbig")],
            [],
            {"e1": "gustav-loebig"},
        )
        lone = register(
            "lone", [human("e1", "Gustav", givens=["Gustav"])], [], {}
        )
        pairs = blocked_pairs([ged.manifest, book.manifest, lone.manifest])
        assert pair(book, "e1", ged, "i8") in pairs
        assert pair(book, "e1", ged, "i2") not in pairs
        assert pair(lone, "e1", ged, "i2") in pairs

    def test_a_key_only_an_exports_unsplit_form_gives_never_links(self):
        """An export that writes a NAME without slashes, 'Ursula Löbig',
        does not say which word is which: its keys pair with a register's
        Ursula Löbig, born as it says, but never make the link confident;
        the bracketed 'Peppi' meets the portrait's Peppi as a given name,
        and never merges either."""
        ged = export(
            "0 @I10@ INDI\n1 NAME Ursula Löbig\n1 BIRT\n2 DATE 1911\n"
            "0 @I7@ INDI\n1 NAME Ida (Peppi) /Sörne/\n",
            {"i10": "ursula-loebig", "i7": "ida-loebig"},
        )
        book = register(
            "ursula",
            [human("e1", "Ursula Löbig")],
            [("e1", "date_of_birth", day(1911))],
            {"e1": "ursula-loebig"},
        )
        photo = register(
            "peppi",
            [human("e1", "Peppi", nicknames=["Peppi"])],
            [],
            {"e1": "ida-loebig"},
        )
        identity = identity_of(ged, book, photo)
        links = links_of(identity)
        assert links[
            frozenset(
                {
                    book.fingerprint("e1", identity),
                    ged.fingerprint("i10", identity),
                }
            )
        ] == (AMBIGUOUS, "weak/agree/unknown")
        assert links[
            frozenset(
                {
                    photo.fingerprint("e1", identity),
                    ged.fingerprint("i7", identity),
                }
            )
        ] == (AMBIGUOUS, "given/unknown/unknown")
        assert merged([ged, book, photo], identity) == []
        # a document's own unsplit form keys like its typed parts
        other = register(
            "unsplit",
            [human("e1", "Löbig Ursula", unsplit=["Löbig Ursula"])],
            [("e1", "date_of_birth", day(1911))],
            {"e1": "ursula-loebig"},
        )
        identity = identity_of(book, other)
        assert merged([book, other], identity) == [{"ursula-loebig"}]


class TestRecordless:
    @pytest.mark.parametrize("as_label", [False, True], ids=["alias", "label"])
    def test_an_appellation_is_never_an_identity_key(self, as_label):
        """Two people a document each calls "Oma", neither with a record:
        each keys on its exact names, but never on a form its document
        gives as an appellation (a `referred_to_as` claim, ADR-0018 as
        amended by #499), as alias or as label, so they never meet. The
        same form unmarked still keys, as any recordless name does."""

        def grandmother(name: str, label: str, marked: bool) -> Doc:
            claims = [("e1", "date_of_birth", day(1876))]
            if marked:
                said = {"text": "Oma", "language": "de"}
                claims.append(
                    (
                        "e1",
                        "referred_to_as",
                        {"type": "monolingual", "value": said},
                    )
                )
            labelled = "Oma" if as_label else label
            aliases = () if as_label else ("Oma",)
            return register(
                name,
                [entity("e1", labelled, aliases=aliases)],
                claims,
                {"e1": name},
            )

        marked = [
            grandmother("hildegard", "Hildegard Löbig", True),
            grandmother("sipkje", "Sipkje Faxridge", True),
        ]
        assert blocked_pairs([doc.manifest for doc in marked]) == set()
        identity = identity_of(*marked)
        assert merged(marked, identity) == []
        unmarked = [
            grandmother("hildegard", "Hildegard Löbig", False),
            grandmother("sipkje", "Sipkje Faxridge", False),
        ]
        assert blocked_pairs([doc.manifest for doc in unmarked]) == {
            pair(unmarked[0], "e1", unmarked[1], "e1")
        }

    def test_a_kindless_or_unclassified_appellation_keys_nothing(self):
        """A ref with no kind (a line-range batch without `instance_of`)
        and an unclassified entity may each be a person: neither keys on
        a form its document gives as an appellation."""
        said = {
            "type": "monolingual",
            "value": {"text": "Oma", "language": "de"},
        }

        def kindless(name: str) -> Doc:
            sha1 = hashlib.sha1(name.encode()).hexdigest()
            ref = {"kind": "local", "local_id": "p1", "label": "Oma"}
            batch = ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "claims": [
                        {
                            "subject": ref,
                            "property": "referred_to_as",
                            "value": said,
                            "source": {
                                "artifact": {"kind": "artifact", "sha1": sha1}
                            },
                        }
                    ],
                }
            )
            return Doc("a register", Manifest(sha1, batch), {"p1": name})

        bare = [kindless("first"), kindless("second")]
        assert blocked_pairs([doc.manifest for doc in bare]) == set()

        def unclassified(name: str) -> Doc:
            return register(
                name,
                [
                    entity(
                        "e1",
                        "Oma",
                        "unclassified_entity",
                        type_hint="grandmother",
                    )
                ],
                [("e1", "referred_to_as", said)],
                {"e1": name},
            )

        loose = [unclassified("first"), unclassified("second")]
        assert blocked_pairs([doc.manifest for doc in loose]) == set()

    def test_marks_on_an_organisation_change_nothing(self):
        """Appellations are a person's: a recordless organisation keys on
        an alias its document marks as it does on any other."""

        def firm(name: str, label: str) -> Doc:
            said = {"text": "die Firma", "language": "de"}
            return register(
                name,
                [entity("e1", label, "organization", aliases=("die Firma",))],
                [
                    (
                        "e1",
                        "referred_to_as",
                        {"type": "monolingual", "value": said},
                    )
                ],
                {"e1": name},
            )

        firms = [
            firm("hatters", "Hutmacherei zum Blauen Hut"),
            firm("brickworks", "it tichelwurk fan Snylsjum"),
        ]
        assert blocked_pairs([doc.manifest for doc in firms]) == {
            pair(firms[0], "e1", firms[1], "e1")
        }

    def test_a_recordless_mention_keys_on_its_exact_name_alone(self):
        """Two line-range batches that each name 'Hildegard Löbig' pair,
        but never merge, even on a date that agrees; a 'Hilde Löbig'
        meets neither, and nor does a mention with a record."""

        def recordless(name: str, label: str) -> Doc:
            return register(
                name,
                [entity("e1", label)],
                [("e1", "date_of_birth", day(1876))],
                {"e1": "hildegard-loebig"},
            )

        first = recordless("first", "Hildegard Löbig")
        second = recordless("second", "hildegard  LÖBIG")
        short = recordless("short", "Hilde Löbig")
        recorded = register(
            "recorded",
            [human("e1", "Hildegard Löbig")],
            [("e1", "date_of_birth", day(1876))],
            {"e1": "hildegard-loebig"},
        )
        docs = [first, second, short, recorded]
        pairs = blocked_pairs([doc.manifest for doc in docs])
        assert pairs == {pair(first, "e1", second, "e1")}
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        assert links_of(identity) == {
            frozenset(
                {
                    first.fingerprint("e1", identity),
                    second.fingerprint("e1", identity),
                }
            ): (AMBIGUOUS, "exact/agree/unknown")
        }
        summary = identity["summary"]
        assert summary["recordless_mentions"] == 3
        assert summary["recordless_by_kind"] == {"human": 3}
        # its aliases key too
        aliased = register(
            "aliased",
            [entity("e1", "Hilde Löbig", aliases=["Hildegard Löbig"])],
            [],
            {"e1": "hildegard-loebig"},
        )
        pairs = blocked_pairs([first.manifest, aliased.manifest])
        assert pairs == {pair(first, "e1", aliased, "e1")}

    def test_records_that_stand_before_the_cleanup_stay_records(self):
        """ADR-0035 §5's verdict is extraction's, taken on the alias bag
        before the cleanup of §8. The cleanup keeps 'Frau Löbig' beside
        Ida Löbig and the widow's 'Löbig, Ursula, Wwe.', the only forms
        holding her title and the widow's suffix, and drops 'die
        Hutmacherei' beside 'zum Blauen Hut', the only form holding its
        kind: every record still stands in clustering, which reads only
        its kind."""
        ida = human(
            "e1",
            "Ida Löbig",
            aliases=["Frau Löbig"],
            givens=["Ida"],
            surnames=["Löbig"],
            titles=["Frau"],
        )
        widow = human(
            "e2",
            "Ursula Löbig",
            aliases=["Löbig, Ursula, Wwe."],
            givens=["Ursula"],
            surnames=["Löbig"],
            suffixes=["Wwe."],
        )
        shop = organization(
            "e3",
            "zum Blauen Hut",
            aliases=["die Hutmacherei"],
            names=["zum Blauen Hut"],
            kinds=["Hutmacherei"],
        )
        cleaned = [normalize_entity_dict(e) for e in (ida, widow, shop)]
        assert [e["aliases"] for e in cleaned] == [
            ["Frau Löbig"],
            ["Löbig, Ursula, Wwe."],
            [],
        ]
        doc = register(
            "cleaned",
            cleaned,
            [],
            {"e1": "ida-loebig", "e2": "ursula-loebig", "e3": "shop"},
        )
        identity = identity_of(doc)
        assert identity["summary"]["recordless_by_kind"] == {}

        # so a mother whose date conflicts still keeps her sons apart
        def leopold(key: str, born: int, **mother) -> Doc:
            return register(
                key,
                [
                    human("e1", "Leopold Löbig"),
                    normalize_entity_dict(
                        human("e2", "Ida Löbig", **mother)
                        if mother
                        else human("e2", "Ida Löbig")
                    ),
                ],
                [
                    ("e1", "date_of_birth", day(1914)),
                    ("e1", "mother", item("e2")),
                    ("e2", "date_of_birth", day(born)),
                ],
                {"e1": "leopold-loebig", "e2": f"ida {born}"},
            )

        docs = [
            leopold(
                "titled",
                1881,
                aliases=["Frau Löbig"],
                givens=["Ida"],
                surnames=["Löbig"],
                titles=["Frau"],
            ),
            leopold("plain", 1850),
        ]
        identity = identity_of(*docs)
        sons = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[sons][0] == AMBIGUOUS
        assert merged(docs, identity) == []

    def test_a_recordless_place_organisation_or_family_never_links(self):
        docs = [
            register(
                f"{kind}-{n}", [entity("e1", label, kind)], [], {"e1": kind}
            )
            for kind, label in (
                ("place", "Flörburg"),
                ("organization", "Hutmacherei zum Blauen Hut"),
                ("family", "Familie Löbig"),
            )
            for n in range(2)
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        assert {p[0] for p in links_of(identity).values()} == {AMBIGUOUS}
        assert len(links_of(identity)) == 3
        assert identity["summary"]["recordless_by_kind"] == {
            "family": 2,
            "organization": 2,
            "place": 2,
        }
        # nor does one meet a mention of its name with a record
        recorded = [
            register(
                f"recorded {kind}",
                [record],
                [],
                {"e1": kind},
            )
            for kind, record in (
                ("place", place("e1", "Flörburg")),
                (
                    "organization",
                    organization(
                        "e1",
                        "Hutmacherei zum Blauen Hut",
                        names=["zum Blauen Hut"],
                    ),
                ),
                ("family", family("e1", "Familie Löbig", "Löbig")),
            )
        ]
        pairs = blocked_pairs([doc.manifest for doc in docs + recorded])
        assert len(pairs) == 3
        assert not any(
            (doc.sha1, "e1") in found for doc in recorded for found in pairs
        )

    def test_a_kind_without_records_still_links_on_its_exact_name(self):
        """'RMS Flund' meets another 'RMS Flund', written with an
        umlaut-free spelling, never a bare 'Flund'."""
        docs = [
            register(f"ship-{n}", [entity("e1", label, "vessel")], [], {})
            for n, label in enumerate(("RMS Flund", "rms  flund.", "Flund"))
        ]
        identity = identity_of(*docs)
        ships = [doc.fingerprint("e1", identity) for doc in docs]
        entries = [identity["mentions"][ship] for ship in ships]
        assert [e["method"] for e in entries] == [
            CONFIDENT,
            CONFIDENT,
            SINGLETON,
        ]
        assert identity["summary"]["recordless_mentions"] == 0

    def test_a_record_of_another_kind_is_none(self):
        """A place's record on a person leaves the person recordless."""
        docs = [
            register(
                f"wrong-{n}",
                [
                    entity(
                        "e1",
                        "Gustav Löbig",
                        structure=place_record("Gustav Löbig"),
                    )
                ],
                [("e1", "date_of_birth", day(1919))],
                {"e1": "gustav-loebig"},
            )
            for n in range(2)
        ]
        identity = identity_of(*docs)
        assert identity["summary"]["recordless_mentions"] == 2
        assert merged(docs, identity) == []
        # an export's records are read as they stand, a record of
        # another kind apart
        exported = [
            Manifest(doc.sha1, doc.manifest.batch, GEDCOM) for doc in docs
        ]
        summary = cluster_corpus(exported)["summary"]
        assert summary["recordless_mentions"] == 2


# -- blocking -----------------------------------------------------------------


class TestInstanceForm:
    """A mention's aliases leave out each alias the instance would keep
    as its label (writer.stored_term), and every name and record part
    keys in that form (#477). NFD against NFC is a regression guard:
    folding already took the two alike. What the stored form changes is
    a control character, made a space, and the edge characters it
    trims."""

    @staticmethod
    def nfd(text: str) -> str:
        return unicodedata.normalize("NFD", text)

    def aliases(self, doc: Doc) -> list[list[str]]:
        return [node.aliases for node in clustering._nodes(doc.manifest, 0)]

    def test_an_alias_the_label_in_another_form_is_the_label(self):
        decomposed = self.nfd("Ida Sörne")
        assert decomposed != "Ida Sörne"
        doc = register(
            "a register of Ida",
            [
                human(
                    "e1",
                    "Ida Sörne",
                    aliases=[decomposed, "Sörne"],
                    givens=["Ida"],
                    surnames=["Sörne"],
                ),
                human(
                    "e2",
                    decomposed,
                    aliases=["Ida Sörne", "Sörne"],
                    givens=["Ida"],
                    surnames=["Sörne"],
                ),
            ],
            [],
            {"e1": "ida", "e2": "ida"},
        )
        assert self.aliases(doc) == [["Sörne"], ["Sörne"]]

    def test_an_alias_the_instance_keeps_as_the_label_is_the_label(self):
        """A control character the instance keeps as a space, and a
        space at either end it drops, in the alias or in the label."""
        doc = register(
            "a register of Gustav",
            [
                human(
                    "e1",
                    "Gustav Löbig",
                    aliases=["Gustav\x08Löbig", " Gustav Löbig\u3000"],
                ),
                human(
                    "e2",
                    "Gustav\x08Löbig",
                    aliases=["Gustav Löbig", "Gustl"],
                    givens=["Gustav"],
                    surnames=["Löbig"],
                ),
            ],
            [],
            {"e1": "gustav", "e2": "gustav"},
        )
        assert self.aliases(doc) == [[], ["Gustl"]]

    def test_a_batch_without_entities_gives_a_label_once(self):
        """Without a manifest, a local id's aliases are the labels its
        refs give it, each once in the instance's form, the mention's
        own label left out."""
        sha1 = sha1_text("a register of Ida")
        labels = (
            "Ida Löbig",
            self.nfd("Ida Löbig"),
            self.nfd("Ida Sörne"),
            "Ida Sörne",
            "Ida\x08Sörne",
        )
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": {
                            "kind": "local",
                            "local_id": "e1",
                            "label": label,
                        },
                        "property": "date_of_birth",
                        "value": day(1890),
                        "source": {
                            "artifact": {"kind": "artifact", "sha1": sha1}
                        },
                    }
                    for label in labels
                ],
            }
        )
        (node,) = clustering._nodes(Manifest(sha1, batch), 0)
        assert node.label == "Ida Löbig"
        # the first form as given, the others the same once stored
        assert node.aliases == [self.nfd("Ida Sörne")]

    @staticmethod
    def manifestless(name: str, *labels: str) -> Doc:
        """A batch without a manifest whose refs give e1 these labels."""
        sha1 = sha1_text(name)
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": {
                            "kind": "local",
                            "local_id": "e1",
                            "label": label,
                        },
                        "property": "date_of_birth",
                        "value": day(1876),
                        "source": {
                            "artifact": {"kind": "artifact", "sha1": sha1}
                        },
                    }
                    for label in labels
                ],
            }
        )
        return Doc("a register", Manifest(sha1, batch), {"e1": "hildegard"})

    def test_a_recordless_name_keys_as_the_instance_keeps_it(self):
        """A recordless mention whose label holds a control character
        for a space, its alias the label as the instance keeps it, still
        blocks with a recordless "Hildegard Löbig": its names key in the
        stored form, as its aliases are compared in it. So, with a
        batch without a manifest naming "Hildegard Löbig", does one
        giving the two forms."""

        def recordless(name: str, label: str, aliases=()) -> Doc:
            return register(
                name,
                [entity("e1", label, aliases=aliases)],
                [("e1", "date_of_birth", day(1876))],
                {"e1": "hildegard-loebig"},
            )

        plain = recordless("plain", "Hildegard Löbig")
        control = recordless(
            "control", "Hildegard\x08Löbig", aliases=["Hildegard Löbig"]
        )
        bare = self.manifestless(
            "bare", "Hildegard\x08Löbig", "Hildegard Löbig"
        )
        (node,) = clustering._nodes(control.manifest, 0)
        assert node.aliases == []
        pairs = blocked_pairs([plain.manifest, control.manifest])
        assert pairs == {pair(plain, "e1", control, "e1")}
        named = self.manifestless("named", "Hildegard Löbig")
        pairs = blocked_pairs([named.manifest, bare.manifest])
        assert pairs == {pair(named, "e1", bare, "e1")}

    def test_a_record_keys_as_the_instance_keeps_it(self):
        """A record's parts key in the stored form too: an unsplit form
        and a place's name with a control character for a space meet the
        same names written with one."""
        unsplit = register(
            "unsplit",
            [human("e1", "Löbig Ursula", unsplit=["Löbig\x08Ursula"])],
            [],
            {"e1": "ursula"},
        )
        typed = register(
            "typed", [human("e1", "Ursula Löbig")], [], {"e1": "ursula"}
        )
        assert blocked_pairs([unsplit.manifest, typed.manifest]) == {
            pair(unsplit, "e1", typed, "e1")
        }
        district = register(
            "district",
            [place("p1", "Kreis Mockburg", names=["Kreis\x08Mockburg"])],
            [],
            {"p1": "mockburg"},
        )
        written = register(
            "written",
            [place("p1", "Kreis Mockburg")],
            [],
            {"p1": "mockburg"},
        )
        assert blocked_pairs([district.manifest, written.manifest]) == {
            pair(district, "p1", written, "p1")
        }


class TestBlocking:
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

    def test_events_block_through_their_participants(self):
        papers = document(
            "naturalisation-1952",
            [
                human(
                    "e1",
                    "Gustave Lobig",
                    aliases=["Gustav Lobig"],
                    givens=["Gustave", "Gustav"],
                    surnames=["Lobig"],
                ),
                entity("e2", "Emigration of Gustave Lobig", "migration"),
                entity("e3", "Birth of Gustave Lobig", "birth"),
            ],
            [
                ("e2", "participant", item("e1")),
                ("e3", "participant", item("e1"), role("principal")),
            ],
            {
                "e1": "gustav-loebig",
                "e2": "emigration-1939",
                "e3": "gustav-loebig",
            },
        )
        talk = document(
            "interview-2002",
            [
                human("e1", "Gustav Lobig"),
                entity("e2", "Emigration of Gustav Lobig", "migration"),
            ],
            [("e2", "participant", item("e1"))],
            {"e1": "gustav-loebig", "e2": "emigration-1939"},
        )
        pairs = blocked_pairs([papers.manifest, talk.manifest])
        assert pair(papers, "e2", talk, "e2") in pairs
        # an event of another kind is never its candidate
        assert pair(papers, "e3", talk, "e2") not in pairs
        # a participant claim points from the event: one from the person
        # takes no part
        backwards = document(
            "interview-2002",
            [
                human("e1", "Gustav Lobig"),
                entity("e2", "Emigration of Gustav Lobig", "migration"),
            ],
            [("e1", "participant", item("e2"))],
            {"e1": "gustav-loebig", "e2": "emigration-1939"},
        )
        pairs = blocked_pairs([papers.manifest, backwards.manifest])
        assert pair(papers, "e2", backwards, "e2") not in pairs

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
        ged = Doc(
            "gedcom",
            Manifest(artifact.sha1, adapted, GEDCOM),
            {"i6": "felix-loebig", event.id: "felix-loebig"},
        )
        card = document(
            "registration-card-1945",
            [
                human("e1", "Felix Löbig"),
                entity("e2", "Felix's death", "death"),
            ],
            [
                ("e2", "participant", item("e1"), role("principal")),
                ("e2", "point_in_time", day(1931)),
            ],
            {"e1": "felix-loebig", "e2": "felix-loebig"},
        )
        pairs = blocked_pairs([ged.manifest, card.manifest])
        assert pair(card, "e2", ged, event.id) in pairs
        assert pair(card, "e1", ged, "i6") in pairs

    def test_a_key_too_many_mentions_share_is_blocked_on_by_none(self):
        """Five registers name Gustav alike. Over a hub limit of three,
        the key they share proposes no pair, and the summary and the
        reasons count them; under it they pair and merge."""
        docs = [
            register(
                f"hub-{n}",
                [human("e1", "Gustav Loebig")],
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
            "key": ["key", "gustav", "loebig"],
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

    def export_with_gustav(self) -> Doc:
        """The export's Gustav, 'Gustl', born 1919 in Flörburg, and his
        death, which it states undated: a death event of its own."""
        return export(
            "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n2 NICK Gustl\n"
            "1 BIRT\n2 DATE 1919\n2 PLAC Flörburg, Hesse, Germany\n"
            "1 DEAT Y\n",
            {
                "i8": "gustav-loebig",
                "i8-death": "gustav-death",
                "Flörburg, Hesse, Germany": "floerburg",
            },
        )

    @pytest.mark.parametrize(
        ("record", "born", "truth"),
        [
            # born as the export says: the six merge
            (human("e1", "Gustav Löbig"), 1919, "gustav-loebig"),
            # his nickname, with his surname
            (
                human(
                    "e1",
                    "Gustl Löbig",
                    nicknames=["Gustl"],
                    surnames=["Löbig"],
                ),
                None,
                "gustav-loebig",
            ),
            # his nickname alone
            (human("e1", "Gustl", nicknames=["Gustl"]), None, "gustav-loebig"),
            (place("e1", "Flörburg"), None, "floerburg"),
        ],
    )
    def test_a_key_the_export_matches_is_never_a_hub(
        self, record, born, truth
    ):
        """Over a hub limit of three, five registers name what the export
        names: each finds the export's mention and pairs with it alone, a
        pair each, so their key is no hub. A person's lone given name only
        ever meets the export, and is never a hub either."""
        ged = self.export_with_gustav()
        hub = load_config(overrides={"blocking": {"max-key-mentions": 3}})
        claims = [("e1", "date_of_birth", day(born))] if born else []
        docs = [
            register(f"{record['label']}-{n}", [record], claims, {"e1": truth})
            for n in range(5)
        ]
        manifests = [ged.manifest, *(doc.manifest for doc in docs)]
        anchor = ged.manifest.sha1
        pairs = blocked_pairs(manifests, hub)
        assert len(pairs) == 5
        assert all(anchor in {sha1 for sha1, _ in pair} for pair in pairs)
        identity = cluster_corpus(manifests, hub)
        assert identity["summary"]["hub_keys"] == 0
        if born:
            assert merged([ged, *docs], identity) == [{truth}]
            (cluster,) = [
                entry
                for entry in identity["clusters"].values()
                if len(entry["members"]) > 1
            ]
            assert len(cluster["members"]) == 6
        # without anchoring the key is a hub like any other; a lone given
        # name is still none, and pairs with nothing then
        free = load_config(
            overrides={
                "blocking": {"max-key-mentions": 3, "gedcom-anchored": False}
            }
        )
        assert blocked_pairs(manifests, free) == set()
        summary = cluster_corpus(manifests, free)["summary"]
        # a name with an umlaut holds two keys, each a hub
        assert summary["hub_keys"] == (0 if record["label"] == "Gustl" else 2)

    def test_the_exports_event_keys_can_be_hubs(self):
        """Events never anchor: they meet each other through their
        principals, the export's among them. Over a hub limit of three,
        five registers' deaths of Gustav share a key that is a hub, and no
        two deaths pair, while each register's Gustav still pairs with the
        export's."""
        ged = self.export_with_gustav()
        hub = load_config(overrides={"blocking": {"max-key-mentions": 3}})
        docs = [
            register(
                f"death-{n}",
                [
                    human("e1", "Gustav Löbig"),
                    entity("e2", "Death of Gustav Löbig", "death"),
                ],
                [("e2", "participant", item("e1"), role("principal"))],
                {"e1": "gustav-loebig", "e2": "gustav-death"},
            )
            for n in range(5)
        ]
        manifests = [ged.manifest, *(doc.manifest for doc in docs)]
        pairs = blocked_pairs(manifests, hub)
        assert len(pairs) == 5
        assert {local for pair in pairs for _, local in pair} == {"i8", "e1"}
        identity = cluster_corpus(manifests, hub)
        hubs = [e for e in identity["review"] if e["status"] == HUB]
        assert {tuple(e["key"]) for e in hubs} == {
            ("key", "gustav", "lobig"),
            ("key", "gustav", "loebig"),
        }
        assert {e["kind"] for e in hubs} == {"death"}
        assert all(len(e["members"]) == 6 for e in hubs)


# -- dates --------------------------------------------------------------------


def when(year, month=None, precision=None) -> TimeValue:
    return TimeValue(year=year, month=month, precision=precision)


def qualifier(prop: str, value) -> SimpleNamespace:
    return SimpleNamespace(property=prop, value=value)


class TestDates:
    def test_a_date_covers_its_precision_never_narrower(self):
        assert date_interval(when(1919, 7), [], 5) == Interval(1919, 1919)
        assert date_interval(when(1890, precision="decade"), [], 5) == (
            Interval(1890, 1899)
        )
        assert date_interval(when(1801, precision="century"), [], 5) == (
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
        assert date_interval(when(1833), [circa], 5) == Interval(1828, 1838)
        # Klara left between 1938 and 1939
        between = [
            qualifier("earliest_date", when(1938)),
            qualifier("latest_date", when(1939)),
        ]
        assert date_interval(when(1938), between, 5) == Interval(1938, 1939)
        # Rahel died after 1945; the firm was gone before 1926
        after = date_interval(
            when(1945), [qualifier("earliest_date", when(1945))], 5
        )
        assert after.start == 1945 and after.end == float("inf")
        before = date_interval(
            when(1926), [qualifier("latest_date", when(1926))], 5
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
                [human("e1", "Xxx Yyy")],
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
            [human("e1", "Xxx Yyy")],
            [("e1", "date_of_birth", day(1911))],
            {"e1": "born-1911"},
        )
        died = register(
            "died",
            [human("e1", "Xxx Yyy")],
            [("e1", "date_of_death", day(1911))],
            {"e1": "died-1911"},
        )
        docs = [born, died]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert {e["method"] for e in identity["mentions"].values()} == {
            AMBIGUOUS
        }

    def test_a_birth_events_date_is_its_principals(self):
        """The tree dates Klara's birth by an event with her its
        principal; the postcard's Klara is born in 1917: the event's date
        agrees, and they merge. A birth event naming the mother as a
        parent gives her no birth date: she merges with herself, born
        1876, through her daughter."""
        tree = document(
            "family-tree-1984",
            [
                human("e1", "Klara Löbig"),
                entity("e2", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e2", "participant", item("e1"), role("principal")),
                ("e2", "point_in_time", day(1917)),
            ],
            {"e1": "klara-loebig", "e2": "klara-loebig"},
        )
        card = document(
            "postcard-1938",
            [human("e1", "Klara Löbig")],
            [("e1", "date_of_birth", day(1917))],
            {"e1": "klara-loebig"},
        )
        docs = [tree, card]
        identity = identity_of(*docs)
        assert {"klara-loebig"} in merged(docs, identity)
        # with no role, the event is nobody's own
        unroled = document(
            "family-tree-1984",
            [
                human("e1", "Klara Löbig"),
                entity("e2", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e2", "participant", item("e1")),
                ("e2", "point_in_time", day(1917)),
            ],
            {"e1": "klara-loebig", "e2": "klara-loebig"},
        )
        identity = identity_of(unroled, card)
        assert merged([unroled, card], identity) == []

        both = register(
            "birth",
            [
                human("e1", "Klara Löbig"),
                human("e2", "Hildegard Löbig"),
                entity("e3", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e1", "mother", item("e2")),
                ("e3", "participant", item("e1"), role("principal")),
                ("e3", "participant", item("e2"), role("parent")),
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
            [human("e1", "Hildegard Löbig"), human("e2", "Klara Löbig")],
            [
                ("e2", "mother", item("e1")),
                ("e1", "date_of_birth", day(1876)),
            ],
            {"e1": "hildegard-loebig", "e2": "klara-loebig"},
        )
        docs = [both, mother]
        identity = identity_of(*docs)
        assert {"hildegard-loebig"} in merged(docs, identity)

    def test_a_death_naming_only_its_informant_dates_no_ones_death(self):
        """A register records a death in 1941 and names only Sebastian,
        who reported it; he died in 1945, as the tree says. The death is
        not his, so its date conflicts with nothing of his, and the two
        Sebastians, whose births agree, merge."""
        book = register(
            "informant",
            [
                human("e1", "Sebastian Löbig"),
                entity("e2", "A death reported by Sebastian", "death"),
            ],
            [
                ("e1", "date_of_birth", day(1862)),
                ("e2", "participant", item("e1"), role("informant")),
                ("e2", "point_in_time", day(1941)),
            ],
            {"e1": "sebastian-loebig", "e2": "ursula-death"},
        )
        tree = document(
            "family-tree-1984",
            [human("e1", "Sebastian Löbig")],
            [
                ("e1", "date_of_birth", day(1862)),
                ("e1", "date_of_death", day(1945)),
            ],
            {"e1": "sebastian-loebig"},
        )
        docs = [book, tree]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"sebastian-loebig"}]
        # as its principal, the death would be his, and conflict
        principal = register(
            "principal",
            [
                human("e1", "Sebastian Löbig"),
                entity("e2", "Death of Sebastian", "death"),
            ],
            [
                ("e1", "date_of_birth", day(1862)),
                ("e2", "participant", item("e1"), role("principal")),
                ("e2", "point_in_time", day(1941)),
            ],
            {"e1": "sebastian-loebig", "e2": "sebastian-death"},
        )
        identity = identity_of(principal, tree)
        assert merged([principal, tree], identity) == []


# -- events -------------------------------------------------------------------


def arrest_and_visit(*, dated: bool) -> list[Doc]:
    """Leopold's arrest (the card, 1939) and a visit of his (the
    interview): two events of kind `event`, whose one participants merge
    on their mother Ida."""
    card = document(
        "registration-card-1945",
        [
            human("e1", "Leopold Löbig"),
            human("e2", "Ida Löbig"),
            entity("e3", "Arrest of Leopold Löbig", "event"),
        ],
        [
            ("e1", "mother", item("e2")),
            ("e3", "participant", item("e1"), role("principal")),
            ("e3", "point_in_time", day(1939, 9, 3)),
        ],
        {"e1": "leopold-loebig", "e2": "ida-loebig", "e3": "arrest-1939"},
    )
    claims = [
        ("e1", "mother", item("e2")),
        ("e3", "participant", item("e1"), role("principal")),
    ]
    if dated:
        claims.append(("e3", "point_in_time", day(1939, 9)))
    talk = document(
        "interview-2002",
        [
            human("e1", "Leopold Löbig"),
            human("e2", "Ida Löbig"),
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


def births(key: str, child: str, truth: str, when: dict) -> Doc:
    """A birth naming the child as its principal and the father as a
    parent and its informant, with the father's marriage to the child's
    mother."""
    return document(
        key,
        [
            human("e1", child),
            human("e2", "Sebastian Löbig"),
            human("e3", "Hildegard Löbig"),
            entity("e4", f"Birth of {child}", "birth"),
            entity("m1", "Marriage of Sebastian and Hildegard", "marriage"),
        ],
        [
            ("e1", "father", item("e2")),
            *union_of("m1", "e2", "e3"),
            ("e4", "participant", item("e1"), role("principal")),
            (
                "e4",
                "participant",
                item("e2"),
                role("parent"),
                role("informant"),
            ),
            ("e4", "point_in_time", when),
        ],
        {
            "e1": truth,
            "e2": "sebastian-loebig",
            "e3": "hildegard-loebig",
            "e4": f"{truth} birth",
            "m1": "sebastian-and-hildegard",
        },
    )


class TestEvents:
    def test_an_event_is_one_when_its_principals_merge(self):
        """Two registers each record a death of a Xxx Yyy in 1941; their
        people, born 1880 and 1920, stay apart, and so do the deaths,
        though their principals' names match (placeholders)."""

        def death(name: str, born: int) -> Doc:
            return register(
                name,
                [
                    human("e1", "Xxx Yyy"),
                    entity("e2", "Death of Xxx Yyy", "death"),
                ],
                [
                    ("e1", "date_of_birth", day(born)),
                    ("e2", "participant", item("e1"), role("principal")),
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

    def test_a_birth_or_death_is_decided_by_its_principal(self):
        """A person dies once: Felix's death in the card and in the tree,
        one of them undated, is one death when the two Felixes merge."""

        def death(key: str, dated: bool) -> Doc:
            claims = [
                *union_of("m1", "e1", "e2"),
                ("e3", "participant", item("e1"), role("principal")),
            ]
            if dated:
                claims.append(("e3", "point_in_time", day(1931)))
            return document(
                key,
                [
                    human("e1", "Felix Löbig"),
                    human("e2", "Ida Löbig"),
                    entity("e3", "Death of Felix Löbig", "death"),
                    entity("m1", "Marriage of Felix and Ida", "marriage"),
                ],
                claims,
                {
                    "e1": "felix-loebig",
                    "e2": "ida-loebig",
                    "e3": "felix-death",
                    "m1": "felix-and-ida",
                },
            )

        docs = [
            death("registration-card-1945", True),
            death("family-tree-1984", False),
        ]
        identity = identity_of(*docs)
        assert {"felix-death"} in merged(docs, identity)
        assert {"felix-loebig"} in merged(docs, identity)
        assert {"felix-and-ida"} in merged(docs, identity)

    def test_two_births_that_name_one_father_stay_apart(self):
        """Klara's birth and Gustav's each name their father, and each is
        dated to the 1910s: the Sebastians merge, the children do not, and
        nor do the births, since their principals differ."""
        decade = {"type": "time", "year": 1910, "precision": "decade"}
        docs = [
            births("postcard-1938", "Klara Löbig", "klara-loebig", decade),
            births(
                "birth-extract-1938", "Gustav Löbig", "gustav-loebig", decade
            ),
        ]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert {"sebastian-loebig"} in merged(docs, identity)
        assert methods(docs, identity, ("postcard-1938", "e4")) == [SINGLETON]

    def test_births_of_one_child_with_dates_that_conflict_stay_apart(self):
        """Two records of Klara's birth give 1917 and 1919: the dates
        conflict, and the births stay apart, though their people merge."""
        docs = [
            births("postcard-1938", "Klara Löbig", "klara-loebig", day(1917)),
            births(
                "family-tree-1984", "Klara Löbig", "klara-loebig", day(1919)
            ),
        ]
        identity = identity_of(*docs)
        assert {"sebastian-loebig"} in merged(docs, identity)
        assert methods(docs, identity, ("postcard-1938", "e4")) == [AMBIGUOUS]
        # the pair's own link is ambiguous, so adjudication counts it
        found = {doc.fingerprint("e4", identity) for doc in docs}
        (review,) = [
            entry
            for entry in identity["review"]
            if set(entry["members"]) == found
        ]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "key/conflict/unknown")
        ]

    def test_the_deaths_of_a_person_merge_with_his_confident_component(self):
        """The export gives Sebastian, 'Wastl', born 1862; the tree and a
        register each give him born 1862 and his death, the tree's in 1945
        and the register's undated; a photo's 'Wastl' keeps his candidate
        cluster ambiguous. The export's, the tree's and the register's
        mentions are its confident component, which merges (#568), and his
        deaths merge with it, confidently, while the photo's 'Wastl' stays
        apart: an event reads the clusters its principals end in."""
        ged = export(
            "0 @I3@ INDI\n1 NAME Sebastian /Löbig/\n2 NICK Wastl\n"
            "1 SEX M\n1 BIRT\n2 DATE 1862\n",
            {"i3": "sebastian-loebig"},
        )

        def death(make, key: str, dated: bool) -> Doc:
            claims = [
                ("e1", "date_of_birth", day(1862)),
                ("e2", "participant", item("e1"), role("principal")),
            ]
            if dated:
                claims.append(("e2", "point_in_time", day(1945)))
            return make(
                key,
                [
                    human("e1", "Sebastian Löbig"),
                    entity("e2", "Death of Sebastian Löbig", "death"),
                ],
                claims,
                {"e1": "sebastian-loebig", "e2": "sebastian-death"},
            )

        tree = death(document, "family-tree-1984", True)
        book = death(register, "a death register", False)
        photo = register(
            "wastl",
            [human("e1", "Wastl", nicknames=["Wastl"])],
            [],
            {"e1": "sebastian-loebig"},
        )
        docs = [ged, tree, book, photo]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        for local_id, method in (
            ("e1", CONFIDENT_COMPONENT),
            ("e2", CONFIDENT),
        ):
            assert [
                identity["mentions"][doc.fingerprint(local_id, identity)][
                    "method"
                ]
                for doc in (tree, book)
            ] == [method, method]
        assert (
            identity["mentions"][photo.fingerprint("e1", identity)]["method"]
            == AMBIGUOUS
        )
        # without the photo, Sebastian merges, and his deaths with him
        docs = [ged, tree, book]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [
            {"sebastian-death"},
            {"sebastian-loebig"},
        ]

    def test_a_birth_naming_only_the_mother_is_not_hers(self):
        """A letter tells of the birth of Hildegard's first child, naming
        only her, as its parent; the extract gives Gustav's birth on 7
        January 1919. The Hildegards merge, and the births stay apart."""
        letter = register(
            "first child",
            [
                human("e1", "Hildegard Löbig"),
                entity("e2", "Birth of Hildegard's first child", "birth"),
            ],
            [
                ("e1", "date_of_birth", day(1876)),
                ("e2", "participant", item("e1"), role("parent")),
            ],
            {"e1": "hildegard-loebig", "e2": "ursula-birth"},
        )
        extract = document(
            "birth-extract-1938",
            [
                human("e1", "Hildegard Löbig"),
                human("e2", "Gustav Löbig"),
                entity("e3", "Birth of Gustav Löbig", "birth"),
            ],
            [
                ("e1", "date_of_birth", day(1876)),
                ("e3", "participant", item("e2"), role("principal")),
                ("e3", "participant", item("e1"), role("parent")),
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
        found = [
            letter.fingerprint("e2", identity),
            extract.fingerprint("e3", identity),
        ]
        assert [identity["mentions"][m]["method"] for m in found] != [
            CONFIDENT,
            CONFIDENT,
        ]

    def test_a_birth_or_death_of_no_principal_meets_nothing(self):
        """ADR-0035 §3.2 and §7, rule 9: a birth or death with no
        principal is nobody's own, and never meets another through its
        other participants. Two registers each record a death in 1941
        naming only its informant, Leopold; two record a birth naming
        only the mother, Ida, one in the 1910s and one in 1916. Leopold
        and Ida each merge, through their kin; the deaths and the births
        stay apart, ambiguous."""

        def event(key: str, name: str, kind: str, role_name: str, when):
            return register(
                key,
                [
                    human("e1", name),
                    human(
                        "e2",
                        "Felix Löbig" if name == "Ida Löbig" else "Ida Löbig",
                    ),
                    entity("e3", f"{kind.title()} register entry", kind),
                ],
                [
                    (
                        "e1",
                        "spouse" if name == "Ida Löbig" else "mother",
                        item("e2"),
                    ),
                    ("e3", "participant", item("e1"), role(role_name)),
                    ("e3", "point_in_time", when),
                ],
                {"e1": name, "e2": "kin", "e3": f"{key} {kind}"},
            )

        deaths = [
            event(
                f"death {n}", "Leopold Löbig", "death", "informant", day(1941)
            )
            for n in range(2)
        ]
        decade = {"type": "time", "year": 1910, "precision": "decade"}
        births = [
            event("birth 1910s", "Ida Löbig", "birth", "mother", decade),
            event("birth 1916", "Ida Löbig", "birth", "mother", day(1916)),
        ]
        for docs, person in ((deaths, "Leopold Löbig"), (births, "Ida Löbig")):
            identity = identity_of(*docs)
            assert {person} in merged(docs, identity)
            events = frozenset(doc.fingerprint("e3", identity) for doc in docs)
            assert links_of(identity)[events][0] == AMBIGUOUS
            assert {identity["mentions"][m]["method"] for m in events} == {
                AMBIGUOUS
            }

    def test_a_birth_of_several_principals_is_nobodys(self):
        """A register's birth on 4 March 1917 names both Hildegard and
        Klara as its principal (a mother mis-roled): it is nobody's own,
        and dates neither, so neither merges with a Hildegard or a Klara
        born that day."""
        both = register(
            "two principals",
            [
                human("e1", "Hildegard Löbig"),
                human("e2", "Klara Löbig"),
                entity("e3", "Birth of Klara Löbig", "birth"),
            ],
            [
                ("e3", "participant", item("e1"), role("principal")),
                ("e3", "participant", item("e2"), role("principal")),
                ("e3", "point_in_time", day(1917, 3, 4)),
            ],
            {"e1": "hildegard-loebig", "e2": "klara-loebig", "e3": "birth"},
        )
        others = [
            register(
                f"born {name}",
                [human("e1", name)],
                [("e1", "date_of_birth", day(1917, 3, 4))],
                {"e1": name},
            )
            for name in ("Hildegard Löbig", "Klara Löbig")
        ]
        docs = [both, *others]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []

    def test_a_birth_its_principals_own_date_conflicts_with_is_not_hers(
        self,
    ):
        """A birth whose principal is Hildegard, born 1876, but dated 1911
        is not her own: its date does not pass to her, and it takes no
        no-date link. So she and her namesake niece, born 1911, stay
        apart (placeholders)."""

        def principal(key: str, year: int | None, truth: str) -> Doc:
            claims = [
                ("e1", "date_of_birth", day(1876)),
                ("e2", "participant", item("e1"), role("principal")),
            ]
            if year:
                claims.append(("e2", "point_in_time", day(year)))
            return register(
                key,
                [
                    human("e1", "Hildegard Löbig"),
                    entity("e2", "Birth of Hildegard", "birth"),
                ],
                claims,
                {"e1": "hildegard-loebig", "e2": truth},
            )

        docs = [
            principal("dated", 1911, "a-birth"),
            principal("undated", None, "hildegard-birth"),
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"hildegard-loebig"}]
        found = [doc.fingerprint("e2", identity) for doc in docs]
        assert [identity["mentions"][m]["method"] for m in found] == [
            AMBIGUOUS,
            AMBIGUOUS,
        ]
        niece = register(
            "niece",
            [human("e1", "Hildegard Löbig"), human("e2", "Sebastian Löbig")],
            [
                ("e1", "date_of_birth", day(1911)),
                ("e1", "father", item("e2")),
            ],
            {"e1": "hildegard-niece", "e2": "sebastian-loebig"},
        )
        docs = [principal("dated", 1911, "a-birth"), niece]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == []

    def test_a_persons_own_undated_birth_mentioned_twice_merges(self):
        """The tree and the postcard each give Klara born 1917, and her
        birth undated: each birth is hers, so the two merge with her."""
        docs = [
            document(
                key,
                [
                    human("e1", "Klara Löbig"),
                    entity("e2", "Birth of Klara Löbig", "birth"),
                ],
                [
                    ("e1", "date_of_birth", day(1917)),
                    ("e2", "participant", item("e1"), role("principal")),
                ],
                {"e1": "klara-loebig", "e2": "klara-birth"},
            )
            for key in ("family-tree-1984", "postcard-1938")
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"klara-birth"}, {"klara-loebig"}]

    def test_a_persons_own_dates_are_their_claims_alone(self):
        """A register gives Klara two births, of 1917 and of 1919, and no
        birth date of her own: both are hers, in whichever order it lists
        them, since only her claims are hers to conflict with, never a
        date another event passed her. So she merges with a Klara born
        1919 (placeholder register)."""

        def twice(order: list[int]) -> Doc:
            years = {1: 1917, 2: 1919}
            return register(
                "two births",
                [
                    human("e0", "Klara Löbig"),
                    *(
                        entity(f"e{n}", f"Birth of Klara Löbig ({n})", "birth")
                        for n in order
                    ),
                ],
                [
                    claim
                    for n in order
                    for claim in (
                        (
                            f"e{n}",
                            "participant",
                            item("e0"),
                            role("principal"),
                        ),
                        (f"e{n}", "point_in_time", day(years[n])),
                    )
                ],
                {
                    "e0": "klara-loebig",
                    **{f"e{n}": f"klara-birth-{n}" for n in order},
                },
            )

        card = document(
            "postcard-1938",
            [human("e1", "Klara Löbig")],
            [("e1", "date_of_birth", day(1919))],
            {"e1": "klara-loebig"},
        )
        for order in ([1, 2], [2, 1]):
            docs = [twice(order), card]
            identity = identity_of(*docs)
            assert merged(docs, identity) == [{"klara-loebig"}], order


# -- unions (ADR-0036 §3) -----------------------------------------------------


def judgment(**extra) -> Doc:
    """The divorce judgment: Leopold and Agnes's marriage, its civil
    wedding on 8 February 1947 (unless `wedded` is false) and its divorce
    on 2 January 1957, with a witness, Ida, when `witness` is true."""
    wedded = extra.get("wedded", True)
    witness = extra.get("witness", False)
    entities = [
        human("e1", "Leopold Löbig"),
        human(
            "e2",
            "Agnes Löbig",
            aliases=["Agnes Löbig geb. Nertler"],
            givens=["Agnes"],
            surnames=["Löbig"],
            birth=["Nertler"],
        ),
        entity("m1", "Marriage of Leopold and Agnes", "marriage"),
        entity("d1", "Divorce of Leopold and Agnes", "divorce"),
    ]
    claims = [
        *union_of("m1", "e1", "e2"),
        ("m1", "ended_by", item("d1")),
        ("d1", "point_in_time", day(1957, 1, 2)),
    ]
    truth = {
        "e1": "leopold-loebig",
        "e2": "agnes-loebig",
        "m1": "leopold-and-agnes",
        "d1": "leopold-and-agnes-divorce",
    }
    if wedded:
        entities.append(
            entity(
                "w1",
                "Civil wedding of Leopold and Agnes",
                extra.get("wedding", "civil_wedding"),
            )
        )
        claims += [
            ("m1", "started_by", item("w1")),
            ("w1", "point_in_time", day(1947, 2, 8)),
        ]
        truth["w1"] = "leopold-and-agnes-wedding"
    if witness:
        entities.append(human("e3", "Ida Löbig"))
        claims.append(("m1", "participant", item("e3"), role("witness")))
        truth["e3"] = "ida-loebig"
    return document("divorce-judgment-1957", entities, claims, truth)


def tree_union(
    wedding: dict | None = None,
    kind: str = "wedding",
    end: str | None = None,
    union: str = "marriage",
) -> Doc:
    """The 1984 tree's couple, '= (1947)', with a wedding of `kind` when
    it is dated, and an end of kind `end`, dated 1957, when there is
    one."""
    entities = [
        human("e1", "Leopold Löbig"),
        human("e2", "Agnes Nertler", givens=["Agnes"], birth=["Nertler"]),
        entity("m1", "Leopold and Agnes", union),
    ]
    claims = union_of(
        "m1", "e1", "e2", "spouse" if union == "marriage" else "partner"
    )
    truth = {
        "e1": "leopold-loebig",
        "e2": "agnes-loebig",
        "m1": "leopold-and-agnes",
    }
    if wedding:
        entities.append(entity("w1", "Wedding", kind))
        claims += [
            ("m1", "started_by", item("w1")),
            ("w1", "point_in_time", wedding),
        ]
        truth["w1"] = "leopold-and-agnes-wedding"
    if end:
        entities.append(entity("d1", "End", end))
        claims += [
            ("m1", "ended_by", item("d1")),
            ("d1", "point_in_time", day(1957)),
        ]
        truth["d1"] = "leopold-and-agnes-divorce"
    return document("family-tree-1984", entities, claims, truth)


class TestUnions:
    def test_a_union_is_keyed_by_its_couple(self):
        """The judgment's marriage and the tree's partnership of the same
        couple meet through the couple, whatever their class, and merge
        once both spouses have merged; the couple are each other's
        spouses, which corroborates Agnes, named by two surnames."""
        docs = [judgment(), tree_union(union="partnership")]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)
        assert {"agnes-loebig"} in merged(docs, identity)
        assert_no_false_merge(docs, identity)

    def test_a_unions_couple_is_its_spouses_or_partners(self):
        """ADR-0036 §3: a union's couple is its participants as spouses
        or partners. A witness, Ida, at the judgment's marriage is no
        spouse of either, and the union still merges with the tree's."""
        witnessed = judgment(witness=True)
        nodes = {
            node.mention.local_id: node
            for node in clustering._nodes(witnessed.manifest, 0)
        }
        assert nodes["m1"].couple == sorted([nodes["e1"].id, nodes["e2"].id])
        assert not {r for r, _ in nodes["e3"].kin} & {"spouse"}
        assert nodes["e3"].id not in nodes["e1"].kin_ids
        docs = [witnessed, tree_union(union="partnership")]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)
        assert_no_false_merge(docs, identity)

    def test_a_union_is_dated_by_its_weddings_never_its_ends(self):
        """The tree's marriage has its wedding in 1947; a judgment that
        names only its divorce, in 1957, dates its marriage by nothing.
        An end never dates a union, so nothing conflicts, and the two
        merge."""
        docs = [judgment(wedded=False), tree_union(wedding=day(1947))]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)

    def test_a_union_of_another_couple_stays_apart(self):
        """Sebastian and Hildegard's marriage in the certificate and in the
        tree merge; it never meets Eugen and Cäcilie's, which shares a
        spouse's surname."""
        docs = [marriage_certificate(), family_tree()]
        identity = identity_of(*docs)
        assert {"sebastian-and-hildegard"} in merged(docs, identity)
        assert_no_false_merge(docs, identity)

    def test_weddings_that_cannot_overlap_keep_two_unions_apart(self):
        """The judgment dates the wedding 1947; a tree that dates a
        wedding of the couple 1960 may be a remarriage, or the one wedding
        dated two ways: the unions stay apart."""
        docs = [judgment(), tree_union(day(1960))]
        identity = identity_of(*docs)
        assert {"leopold-loebig"} in merged(docs, identity)
        unions = [doc.fingerprint("m1", identity) for doc in docs]
        assert {identity["mentions"][m]["method"] for m in unions} == {
            AMBIGUOUS
        }
        assert links_of(identity)[frozenset(unions)] == (
            AMBIGUOUS,
            "key/conflict/unknown",
        )

    def test_a_union_naming_one_of_its_couple_is_ambiguous(self):
        """A register names Leopold's marriage and not his wife: it meets
        the marriage of Leopold and Agnes through him, and stays apart
        from it (placeholders)."""

        def leopold(name: str, *spouses: dict) -> Doc:
            return register(
                name,
                [
                    human("e1", "Leopold Löbig"),
                    human("e2", "Ida Löbig"),
                    entity("m1", "Leopold's marriage", "marriage"),
                    *spouses,
                ],
                [
                    ("e1", "mother", item("e2")),
                    ("m1", "participant", item("e1"), role("spouse")),
                    *(
                        ("m1", "participant", item(s["id"]), role("spouse"))
                        for s in spouses
                    ),
                ],
                {
                    "e1": "leopold-loebig",
                    "e2": "ida-loebig",
                    "m1": "leopold-and-agnes",
                    **({"e3": "agnes-loebig"} if spouses else {}),
                },
            )

        couple = leopold("couple", human("e3", "Agnes Nertler"))
        single = leopold("single")
        docs = [couple, single]
        identity = identity_of(*docs)
        assert {"leopold-loebig"} in merged(docs, identity)
        unions = [doc.fingerprint("m1", identity) for doc in docs]
        assert {identity["mentions"][m]["method"] for m in unions} == {
            AMBIGUOUS
        }
        # with his wife named in both, the two are one
        both = leopold("both", human("e3", "Agnes Nertler"))
        docs = [couple, both]
        assert {"leopold-and-agnes"} in merged(docs, identity_of(*docs))

    @pytest.mark.parametrize(
        ("kind", "one"),
        [
            ("wedding", True),
            ("civil_wedding", True),
            ("religious_wedding", False),
        ],
    )
    def test_weddings_are_keyed_by_union_kind_and_date(self, kind, one):
        """The tree's wedding of 1947 is the judgment's civil wedding when
        its kind is unstated or civil; a religious wedding is another
        ceremony."""
        docs = [judgment(), tree_union(day(1947), kind)]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)
        weddings = {doc.fingerprint("w1", identity) for doc in docs}
        assert (
            weddings
            in [set(e["members"]) for e in identity["clusters"].values()]
        ) is one
        assert_no_false_merge(docs, identity)

    def test_an_undated_wedding_agrees_with_no_date(self):
        """A wedding is keyed by its union, its kind and a date that
        agrees: the tree's undated wedding of the couple stays apart from
        the judgment's, though the unions merge."""
        docs = [judgment(), tree_union({"type": "time", "year": 1947})]
        dated = identity_of(*docs)
        weddings = {doc.fingerprint("w1", dated) for doc in docs}
        assert weddings in [
            set(e["members"]) for e in dated["clusters"].values()
        ]
        undated = tree_union(day(1947))
        batch = undated.manifest.batch.model_copy(deep=True)
        batch.claims = [
            claim
            for claim in batch.claims
            if claim.property != "point_in_time"
        ]
        docs = [
            judgment(),
            Doc(undated.key, Manifest(undated.sha1, batch), undated.truth),
        ]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)
        assert methods(docs, identity, ("family-tree-1984", "w1")) == [
            AMBIGUOUS
        ]

    def test_a_unions_own_date_is_no_key(self):
        """A directory that lists the couple as married in 1960 observes
        the union then; its date is no wedding's, so it conflicts with
        none, and the unions merge."""
        observed = tree_union()
        batch = observed.manifest.batch.model_copy(deep=True)
        docs = [
            judgment(),
            document(
                "directory-1912",
                [e.model_dump() for e in batch.entities],
                [
                    *union_of("m1", "e1", "e2"),
                    ("m1", "point_in_time", day(1960)),
                ],
                observed.truth,
            ),
        ]
        identity = identity_of(*docs)
        assert {"leopold-and-agnes"} in merged(docs, identity)

    def test_a_wedding_of_another_union_stays_apart(self):
        """Two weddings of 1947 whose unions did not merge stay apart."""
        other = register(
            "another couple",
            [
                human("e1", "Leopold Löbig"),
                human("e2", "Rahel Löbig"),
                entity("m1", "A marriage", "marriage"),
                entity("w1", "A wedding", "wedding"),
            ],
            [
                *union_of("m1", "e1", "e2"),
                ("m1", "started_by", item("w1")),
                ("w1", "point_in_time", day(1947)),
            ],
            {
                "e1": "leopold-loebig",
                "e2": "rahel-loebig",
                "m1": "another",
                "w1": "another-wedding",
            },
        )
        docs = [judgment(), other]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)

    @pytest.mark.parametrize(
        ("end", "one"),
        [("event", True), ("divorce", True), ("annulment", False)],
    )
    def test_an_end_of_unstated_kind_meets_any_end(self, end, one):
        docs = [judgment(), tree_union(end=end)]
        identity = identity_of(*docs)
        ends = {doc.fingerprint("d1", identity) for doc in docs}
        assert (
            ends in [set(e["members"]) for e in identity["clusters"].values()]
        ) is one
        assert_no_false_merge(docs, identity)

    def test_a_death_that_ends_a_union_stays_its_principals(self):
        """The burial register's Cäcilie died, and her death ends her
        marriage: the death is hers, dated with her, never keyed by the
        union."""
        burial = register(
            "burial",
            [
                human("e1", "Eugen Löbig"),
                human("e2", "Cäcilie Löbig"),
                entity("m1", "Marriage of Eugen and Cäcilie", "marriage"),
                entity("d1", "Death of Cäcilie", "death"),
            ],
            [
                *union_of("m1", "e1", "e2"),
                ("m1", "ended_by", item("d1")),
                ("d1", "participant", item("e2"), role("principal")),
                ("d1", "point_in_time", day(1902)),
            ],
            {
                "e1": "eugen-loebig",
                "e2": "caecilie-loebig",
                "m1": "eugen-and-caecilie",
                "d1": "caecilie-death",
            },
        )
        tree = register(
            "tree",
            [
                human("e1", "Cäcilie Löbig"),
                entity("d1", "Death of Cäcilie", "death"),
            ],
            [
                ("e1", "date_of_death", day(1902)),
                ("d1", "participant", item("e1"), role("principal")),
            ],
            {"e1": "caecilie-loebig", "d1": "caecilie-death"},
        )
        docs = [burial, tree]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [
            {"caecilie-death"},
            {"caecilie-loebig"},
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
                [human("e1", "Xxx Yyy"), human("e2", "Zzz Yyy")],
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
        assert review["contradiction"]["evidence"] == "key/conflict/agree"
        assert identity["summary"]["contradicted_clusters"] >= 1

    def test_given_name_prefixes_chain_nothing(self):
        """'Xxx.' starts both 'Xxxa' and 'Xxxb', and a prefix matches
        nothing: none of the three meets another."""

        def child(name: str, first: str, truth: str) -> Doc:
            return register(
                name,
                [human("e1", f"{first} Yyy"), human("e2", "Zzz Yyy")],
                [("e1", "father", item("e2"))],
                {"e1": truth, "e2": "zzz"},
            )

        docs = [
            child("a", "Xxxa", "xxxa"),
            child("b", "Xxxb", "xxxb"),
            child("c", "Xxx.", "xxxa-or-xxxb"),
        ]
        pairs = blocked_pairs([doc.manifest for doc in docs])
        assert {
            frozenset(local for _sha1, local in found) for found in pairs
        } == {frozenset({"e2"})}

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
            [
                place(
                    "e1",
                    "Xxxdorf, Kreis Yyy",
                    names=["Xxxdorf"],
                    within=["Kreis Yyy"],
                )
            ],
            [],
            {"e1": "xxxdorf"},
        )
        two = register(
            "two",
            [
                place(
                    "e1",
                    "Xxxdorf, Kreis Zzz",
                    names=["Xxxdorf"],
                    within=["Kreis Zzz"],
                )
            ],
            [],
            {"e1": "xxxdorf-2"},
        )
        docs = [ged, one, two]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)

    def test_organizations_and_families_named_by_a_surname(self):
        """Two firms called 'Firma Löbig', which give the name Löbig alone,
        two called 'Löbig', and two families called 'Familie Löbig': a
        proper name of one word, with nothing else shared, merges none of
        them, and a family never merges."""
        firm = document(
            "directory-1912",
            [
                organization(
                    "e1",
                    "Hutmacherei zum Blauen Hut",
                    aliases=["Firma Löbig"],
                    names=["zum Blauen Hut", "Löbig"],
                    kinds=["Hutmacherei"],
                )
            ],
            [],
            {"e1": "zum-blauen-hut"},
        )
        other = register(
            "weaver",
            [
                organization(
                    "e1",
                    "Weberei Löbig",
                    aliases=["Firma Löbig"],
                    names=["Löbig"],
                    kinds=["Weberei"],
                )
            ],
            [],
            {"e1": "weberei"},
        )
        bare = [
            register(
                f"bare-{n}",
                [organization("e1", "Löbig", names=["Löbig"])],
                [],
                {"e1": f"firm-{n}"},
            )
            for n in range(2)
        ]
        families = [
            register(
                f"family-{n}",
                [family("e1", "Familie Löbig", "Löbig")],
                [],
                {"e1": f"family-{n}"},
            )
            for n in range(2)
        ]
        docs = [firm, other, *bare, *families]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == []
        family_pairs = [
            p for p in links_of(identity).values() if p[1].startswith("key/")
        ]
        assert family_pairs and {p[0] for p in family_pairs} == {AMBIGUOUS}

    def test_a_one_word_name_links_only_on_places_that_agree(self):
        """Two hat shops named 'Löbig' with no place stated stay apart,
        a shared kind notwithstanding: 'Firma Löbig' may be any Löbig's
        (decided 2026-09-29). Two firms named 'Löbig' in one street link,
        and so do two hat shops named 'Löbig' in one town."""

        def firm(name: str, kinds=(), places=()) -> Doc:
            return register(
                name,
                [
                    organization(
                        "e1",
                        "Löbig",
                        aliases=[" ".join([*kinds, "Löbig"])] if kinds else [],
                        names=["Löbig"],
                        kinds=kinds,
                        places=places,
                    )
                ],
                [],
                {"e1": "firm"},
            )

        shops = [firm(f"shop-{n}", kinds=["Hutmacherei"]) for n in range(2)]
        identity = identity_of(*shops)
        assert merged(shops, identity) == []
        assert set(links_of(identity).values()) == {
            (AMBIGUOUS, "key/unknown/unknown")
        }
        street = [
            firm(f"street-{n}", places=["Brunnenring 35"]) for n in range(2)
        ]
        assert merged(street, identity_of(*street)) == [{"firm"}]
        town = [
            firm(f"town-{n}", kinds=["Hutmacherei"], places=["Flörburg"])
            for n in range(2)
        ]
        assert merged(town, identity_of(*town)) == [{"firm"}]

    def test_places_that_differ_keep_a_long_name_apart(self):
        """Two shops called 'zum Blauen Hut' in two towns stay apart; in
        one town, or with no place stated, they merge."""

        def shop(name: str, places=()) -> Doc:
            return register(
                name,
                [
                    organization(
                        "e1",
                        "zum Blauen Hut",
                        names=["zum Blauen Hut"],
                        places=places,
                    )
                ],
                [],
                {"e1": name},
            )

        apart = [
            shop("in Flörburg", ["Flörburg"]),
            shop("in Rekau", ["Rekau"]),
        ]
        identity = identity_of(*apart)
        assert merged(apart, identity) == []
        assert {p for p in links_of(identity).values()} == {
            (AMBIGUOUS, "exact/unknown/differ")
        }
        together = [
            register(
                f"shop-{n}",
                [
                    organization(
                        "e1",
                        "zum Blauen Hut",
                        names=["zum Blauen Hut"],
                        places=places,
                    )
                ],
                [],
                {"e1": "zum-blauen-hut"},
            )
            for n, places in enumerate(([], ["Flörburg"], ["Floerburg"]))
        ]
        assert merged(together, identity_of(*together)) == [{"zum-blauen-hut"}]

    def test_located_places_agree_only_as_rule_5_or_containment_says(self):
        """ADR-0035 §7, rule 6, for the places location claims name. Two
        nameless civil registries at a Fläckfeld in Kreis Mockburg and at
        one in Kreis Peiltal: the Fläckfelds stay ambiguous under rule 5,
        so the registries' places differ, and they stay apart. At two
        Fläckfelds in Kreis Mockburg they link. A nameless Landgericht at
        Rekau, which its manifest puts in Hesse, and one at Hesse: one
        place lies inside the other, so they agree (decided 2026-09-29).
        One with no place stated stays apart from both."""

        def registry(key: str, district: str, truth: str) -> Doc:
            return register(
                key,
                [
                    organization("e1", "Standesamt", kinds=["Standesamt"]),
                    place(
                        "p0",
                        f"Fläckfeld, {district}",
                        names=["Fläckfeld"],
                        within=[district],
                    ),
                ],
                [("e1", "location", item("p0"))],
                {"e1": truth, "p0": f"flaeckfeld-{truth}"},
            )

        rivals = [
            registry("Mockburg", "Kreis Mockburg", "mockburg"),
            registry("Peiltal", "Kreis Peiltal", "peiltal"),
        ]
        identity = identity_of(*rivals)
        assert merged(rivals, identity) == []
        registries = frozenset(
            doc.fingerprint("e1", identity) for doc in rivals
        )
        assert links_of(identity)[registries] == (
            AMBIGUOUS,
            "none/unknown/differ",
        )
        towns = frozenset(doc.fingerprint("p0", identity) for doc in rivals)
        assert links_of(identity)[towns] == (AMBIGUOUS, "key/unknown/unknown")
        same = [
            registry("Mockburg", "Kreis Mockburg", "mockburg"),
            registry("Mockburg again", "Kreis Mockburg", "mockburg"),
        ]
        assert {"mockburg"} in merged(same, identity_of(*same))

        def court(key: str, places: list[str], truth: str) -> Doc:
            return register(
                key,
                [
                    organization("e1", "Landgericht", kinds=["Landgericht"]),
                    *(place(f"p{n}", name) for n, name in enumerate(places)),
                ],
                [("e1", "location", item("p0"))] * bool(places)
                + [
                    (f"p{n}", "part_of", item(f"p{n + 1}"))
                    for n in range(len(places) - 1)
                ],
                {
                    "e1": truth,
                    **{f"p{n}": name for n, name in enumerate(places)},
                },
            )

        rekau = court("in Rekau", ["Rekau", "Hesse"], "landgericht-rekau")
        hesse = court("in Hesse", ["Hesse"], "landgericht-rekau")
        docs = [rekau, hesse]
        identity = identity_of(*docs)
        assert {"landgericht-rekau"} in merged(docs, identity)
        courts = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert identity["mentions"][min(courts)]["method"] == CONFIDENT
        # in either order
        judge, node = judge_of(*docs)
        a, b = node(rekau, "e1"), node(hesse, "e1")
        assert judge._located(a, b) == judge._located(b, a) == "agree"
        nowhere = court("nowhere", [], "landgericht-somewhere")
        docs = [rekau, nowhere]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        for doc in docs:
            fingerprint = doc.fingerprint("e1", identity)
            assert identity["mentions"][fingerprint]["method"] != CONFIDENT

    def test_sibling_places_in_one_district_differ(self):
        """Flörburg, Kredfeld and Peiltal all lie in Kreis Peiltal. Nameless
        civil registries at Flörburg and at Kredfeld, each of whose
        manifests puts its town in Kreis Peiltal, stay apart: a shared
        container is no shared place. So do two 'zum Blauen Hut' shops at
        Flörburg and at Peiltal. A bare Fläckfeld and one in Kreis
        Mockburg nest, and registries at each merge; one at Kreis
        Mockburg itself, which only the other's record puts Fläckfeld in,
        stays apart."""

        def at(key: str, org: dict, town: str, district=None) -> Doc:
            return register(
                key,
                [org, place("p0", town), place("p1", "Kreis Peiltal")]
                if district is None
                else [
                    org,
                    place(
                        "p0",
                        f"{town}, {district}",
                        names=[town],
                        within=[district],
                    ),
                ],
                [("e1", "location", item("p0"))]
                + (
                    [("p0", "part_of", item("p1"))] if district is None else []
                ),
                {"e1": key, "p0": town, "p1": "kreis-peiltal"}
                if district is None
                else {"e1": "registry", "p0": town},
            )

        def registry() -> dict:
            return organization("e1", "Standesamt", kinds=["Standesamt"])

        def blue_hat() -> dict:
            return organization(
                "e1",
                "Hutmacherei zum Blauen Hut",
                names=["zum Blauen Hut"],
                kinds=["Hutmacherei"],
            )

        docs = [
            at("Flörburg", registry(), "Flörburg"),
            at("Kredfeld", registry(), "Kredfeld"),
        ]
        identity = identity_of(*docs)
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (
            AMBIGUOUS,
            "none/unknown/differ",
        )
        docs = [
            at("shop Flörburg", blue_hat(), "Flörburg"),
            at("shop Peiltal", blue_hat(), "Peiltal"),
        ]
        identity = identity_of(*docs)
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (
            AMBIGUOUS,
            "exact/unknown/differ",
        )
        docs = [
            register(
                "bare Fläckfeld",
                [registry(), place("p0", "Fläckfeld")],
                [("e1", "location", item("p0"))],
                {"e1": "registry", "p0": "Fläckfeld"},
            ),
            at("in Kreis Mockburg", registry(), "Fläckfeld", "Kreis Mockburg"),
        ]
        identity = identity_of(*docs)
        assert {"registry"} in merged(docs, identity)
        # containment that only a record's `within` states does not count
        # for places location claims name: a registry at 'Fläckfeld, Kreis
        # Mockburg' and one at Kreis Mockburg stay apart
        docs = [
            at("in Kreis Mockburg", registry(), "Fläckfeld", "Kreis Mockburg"),
            register(
                "at Kreis Mockburg",
                [registry(), place("p0", "Kreis Mockburg")],
                [("e1", "location", item("p0"))],
                {"e1": "another registry", "p0": "Kreis Mockburg"},
            ),
        ]
        identity = identity_of(*docs)
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (
            AMBIGUOUS,
            "none/unknown/differ",
        )

    def test_a_shop_at_a_recordless_place_is_placed_by_its_name(self):
        """Two registers each name 'Hutmacherei zum Blauen Hut', one at
        Flörburg and one at Fläckfeld, towns without records. The shops'
        places fall back to the towns' exact names: they differ, and the
        two shops stay apart (rule 6's own case). At two recordless
        Flörburgs nothing says the places differ, and the shops link on
        their long name; so does one at a Flörburg with a record. Two
        nameless shops at recordless Flörburgs meet, and stay ambiguous:
        a recordless place agrees with nothing."""

        def shop(key: str, town: str, truth: str, nameless=False) -> Doc:
            return register(
                key,
                [
                    organization("e1", "Hutmacherei", kinds=["Hutmacherei"])
                    if nameless
                    else organization(
                        "e1",
                        "Hutmacherei zum Blauen Hut",
                        names=["zum Blauen Hut"],
                        kinds=["Hutmacherei"],
                    ),
                    entity("p0", town, "place"),
                ],
                [("e1", "location", item("p0"))],
                {"e1": truth, "p0": town},
            )

        docs = [
            shop("in Flörburg", "Flörburg", "zum-blauen-hut"),
            shop("in Fläckfeld", "Fläckfeld", "another-blue-hat"),
        ]
        identity = identity_of(*docs)
        assert identity["summary"]["recordless_by_kind"] == {"place": 2}
        assert merged(docs, identity) == []
        shops = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[shops] == (
            AMBIGUOUS,
            "exact/unknown/differ",
        )
        docs = [
            shop("in Flörburg", "Flörburg", "zum-blauen-hut"),
            shop("in Flörburg again", "Flörburg", "zum-blauen-hut"),
        ]
        identity = identity_of(*docs)
        assert {"zum-blauen-hut"} in merged(docs, identity)
        recorded = register(
            "in Flörburg, Hesse",
            [
                organization(
                    "e1",
                    "Hutmacherei zum Blauen Hut",
                    names=["zum Blauen Hut"],
                    kinds=["Hutmacherei"],
                ),
                place(
                    "p0",
                    "Flörburg, Hesse",
                    names=["Flörburg"],
                    within=["Hesse"],
                ),
            ],
            [("e1", "location", item("p0"))],
            {"e1": "zum-blauen-hut", "p0": "floerburg"},
        )
        docs = [recorded, shop("in Flörburg", "Flörburg", "zum-blauen-hut")]
        identity = identity_of(*docs)
        assert {"zum-blauen-hut"} in merged(docs, identity)
        # nameless, at two recordless Flörburgs: they meet, unknown
        nameless = [
            shop(f"nameless {n}", "Flörburg", "hat shop", nameless=True)
            for n in range(2)
        ]
        identity = identity_of(*nameless)
        pair_ = frozenset(doc.fingerprint("e1", identity) for doc in nameless)
        assert links_of(identity).get(pair_) == (
            AMBIGUOUS,
            "none/unknown/unknown",
        )

    def test_a_recordless_place_never_agrees_with_a_record_string(self):
        """A nameless Hutmacherei at a Flörburg without a record, and one
        whose record names Flörburg: a place without a record agrees with
        nothing, so the two stay ambiguous. So do a Standesamt at a 'Lager
        Klurnau' without a record (known also as Klurnau) and one whose
        record names Klurnau, the camp's short form."""
        cases = [
            ("Hutmacherei", "Flörburg", [], "Flörburg"),
            ("Standesamt", "Lager Klurnau", ["Klurnau"], "Klurnau"),
        ]
        for kind, town, aliases, written in cases:
            located = register(
                f"{kind} at {town}",
                [
                    organization("e1", kind, kinds=[kind]),
                    entity("p0", town, "place", aliases=aliases),
                ],
                [("e1", "location", item("p0"))],
                {"e1": f"{kind} 1", "p0": town},
            )
            listed = register(
                f"{kind} in {written}",
                [organization("e1", kind, kinds=[kind], places=[written])],
                [],
                {"e1": f"{kind} 2"},
            )
            docs = [located, listed]
            identity = identity_of(*docs)
            assert merged(docs, identity) == []
            both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
            assert links_of(identity)[both] == (
                AMBIGUOUS,
                "none/unknown/unknown",
            )

    def test_record_places_and_location_claims_agree_as_rule_5_lets_them(
        self,
    ):
        """A nameless Städtische Realschule whose record names Flörburg,
        and one located at Xxxstrasse 1, which its manifest puts in
        Flörburg: one's places lie among the other's, in either order, and
        they merge. 'zum Blauen Hut' whose record names Flörburg against
        one located at Fläckfeld: the places differ, and they stay apart;
        against one located at Flörburg they merge."""
        listed = register(
            "school in Flörburg",
            [
                organization(
                    "e1",
                    "Städtische Realschule",
                    kinds=["Realschule"],
                    places=["Flörburg"],
                )
            ],
            [],
            {"e1": "realschule-f"},
        )
        street = register(
            "school on a street",
            [
                organization(
                    "e1", "Städtische Realschule", kinds=["Realschule"]
                ),
                place("p0", "Xxxstrasse 1"),
                place("p1", "Flörburg"),
            ],
            [
                ("e1", "location", item("p0")),
                ("p0", "part_of", item("p1")),
            ],
            {"e1": "realschule-f", "p0": "Xxxstrasse 1", "p1": "floerburg"},
        )
        docs = [listed, street]
        judge, node = judge_of(*docs)
        a, b = node(listed, "e1"), node(street, "e1")
        assert judge._organizations(a, b) == ("kind", "agree")
        assert judge._organizations(b, a) == ("kind", "agree")
        assert {"realschule-f"} in merged(docs, identity_of(*docs))

        def shop(key: str, town: str | None, truth: str) -> Doc:
            return register(
                key,
                [
                    organization(
                        "e1",
                        "Hutmacherei zum Blauen Hut",
                        names=["zum Blauen Hut"],
                        kinds=["Hutmacherei"],
                        places=[] if town else ["Flörburg"],
                    ),
                    *([place("p0", town)] if town else []),
                ],
                [("e1", "location", item("p0"))] if town else [],
                {"e1": truth, **({"p0": town} if town else {})},
            )

        named = shop("named Flörburg", None, "zum-blauen-hut")
        docs = [named, shop("at Fläckfeld", "Fläckfeld", "another")]
        identity = identity_of(*docs)
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (
            AMBIGUOUS,
            "exact/unknown/differ",
        )
        docs = [named, shop("at Flörburg", "Flörburg", "zum-blauen-hut")]
        assert {"zum-blauen-hut"} in merged(docs, identity_of(*docs))

    def test_a_located_district_without_a_record_still_differs(self):
        """'zum Blauen Hut' located at a Fläckfeld which part_of puts in a
        Kreis Mockburg without a record, against one whose record names
        'Fläckfeld, Kreis Peiltal': the district counts by its exact
        name, so the places differ, in either order (decided
        2026-10-02). So do nameless civil registries in the same setup,
        and the two variants: a Fläckfeld without a record in a Kreis
        Mockburg with one, and a shop located at both a Fläckfeld without
        a record and a Kreis Mockburg with one. A registry located at
        'Fläckfeld, Kreis Mockburg', whose record states the district as
        `within`, differs from one whose record names Kreis Peiltal."""

        def org(nameless: bool, **record) -> dict:
            if nameless:
                return organization(
                    "e1", "Standesamt", kinds=["Standesamt"], **record
                )
            return organization(
                "e1",
                "Hutmacherei zum Blauen Hut",
                names=["zum Blauen Hut"],
                kinds=["Hutmacherei"],
                **record,
            )

        def town(local_id: str, name: str, recorded: bool) -> dict:
            return (
                place(local_id, name)
                if recorded
                else entity(local_id, name, "place")
            )

        def located(key, nameless, town_recorded, district_recorded, claim):
            return register(
                key,
                [
                    org(nameless),
                    town("p0", "Fläckfeld", town_recorded),
                    town("p1", "Kreis Mockburg", district_recorded),
                ],
                [("e1", "location", item("p0"))]
                + (
                    [("p0", "part_of", item("p1"))]
                    if claim == "part_of"
                    else [("e1", "location", item("p1"))]
                ),
                {"e1": key, "p0": "Fläckfeld", "p1": "Kreis Mockburg"},
            )

        def listed(key, nameless):
            return register(
                key,
                [org(nameless, places=["Fläckfeld", "Kreis Peiltal"])],
                [],
                {"e1": key},
            )

        shapes = [
            (False, True, False, "part_of"),
            (True, True, False, "part_of"),
            (False, False, True, "part_of"),
            (False, False, True, "location"),
        ]
        for nameless, town_recorded, district_recorded, claim in shapes:
            name = "none" if nameless else "exact"
            for keys in (("located", "listed"), ("listed", "located")):
                shape = f"{keys} {nameless} {town_recorded} {claim}"
                docs = [
                    located(
                        f"{keys[0]} {shape}",
                        nameless,
                        town_recorded,
                        district_recorded,
                        claim,
                    ),
                    listed(f"{keys[1]} {shape}", nameless),
                ]
                identity = identity_of(*docs)
                both = frozenset(
                    doc.fingerprint("e1", identity) for doc in docs
                )
                assert links_of(identity)[both] == (
                    AMBIGUOUS,
                    f"{name}/unknown/differ",
                ), shape
        # the located side's `within` counts as a part
        within = register(
            "within",
            [
                org(True),
                place(
                    "p0",
                    "Fläckfeld, Kreis Mockburg",
                    names=["Fläckfeld"],
                    within=["Kreis Mockburg"],
                ),
            ],
            [("e1", "location", item("p0"))],
            {"e1": "mockburg", "p0": "Fläckfeld"},
        )
        docs = [within, listed("peiltal", True)]
        identity = identity_of(*docs)
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (AMBIGUOUS, "none/unknown/differ")

    def test_record_places_in_two_districts_do_not_agree(self):
        """Two nameless civil registries whose records name 'Fläckfeld,
        Kreis Mockburg' and 'Fläckfeld, Kreis Peiltal': the shared name
        is not enough, since neither list lies among the other, and they
        stay apart (decided 2026-10-02). One whose record names Fläckfeld
        alone lies among either, and links."""

        def registry(key: str, places: list[str], truth: str) -> Doc:
            return register(
                key,
                [
                    organization(
                        "e1", "Standesamt", kinds=["Standesamt"], places=places
                    )
                ],
                [],
                {"e1": truth},
            )

        docs = [
            registry("Mockburg", ["Fläckfeld", "Kreis Mockburg"], "m"),
            registry("Peiltal", ["Fläckfeld", "Kreis Peiltal"], "p"),
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        both = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[both] == (AMBIGUOUS, "none/unknown/differ")
        docs = [
            registry("Mockburg", ["Fläckfeld", "Kreis Mockburg"], "m"),
            registry("bare", ["Fläckfeld"], "m"),
        ]
        assert merged(docs, identity_of(*docs)) == [{"m"}]

    def test_districts_from_part_of_claims_keep_namesakes_apart(self):
        """Two Fläckfelds that part_of claims put in Kreis Mockburg and in
        Kreis Peiltal: rule 5 compares the containers their manifests
        give, so the places stay ambiguous, and so do nameless civil
        registries at each (decided 2026-10-02)."""

        def registry(key: str, district: str) -> Doc:
            return register(
                key,
                [
                    organization("e1", "Standesamt", kinds=["Standesamt"]),
                    place("p0", "Fläckfeld"),
                    place("p1", district),
                ],
                [
                    ("e1", "location", item("p0")),
                    ("p0", "part_of", item("p1")),
                ],
                {"e1": key, "p0": f"Fläckfeld {key}", "p1": district},
            )

        docs = [
            registry("Mockburg", "Kreis Mockburg"),
            registry("Peiltal", "Kreis Peiltal"),
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        towns = frozenset(doc.fingerprint("p0", identity) for doc in docs)
        assert links_of(identity)[towns] == (AMBIGUOUS, "key/unknown/unknown")
        registries = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert links_of(identity)[registries] == (
            AMBIGUOUS,
            "none/unknown/differ",
        )

        # the same, with districts that have no record, and with districts
        # whose records both put them in Hesse: a container is one part
        def variant(key: str, district: str, kind: str) -> Doc:
            container = (
                entity("p1", district, "place")
                if kind == "recordless"
                else place(
                    "p1",
                    f"{district}, Hesse",
                    names=[district],
                    within=["Hesse"],
                )
            )
            return register(
                f"{key} {kind}",
                [
                    organization("e1", "Standesamt", kinds=["Standesamt"]),
                    place("p0", "Fläckfeld"),
                    container,
                ],
                [
                    ("e1", "location", item("p0")),
                    ("p0", "part_of", item("p1")),
                ],
                {"e1": key, "p0": f"Fläckfeld {key}", "p1": district},
            )

        for kind in ("recordless", "in Hesse"):
            docs = [
                variant("Mockburg", "Kreis Mockburg", kind),
                variant("Peiltal", "Kreis Peiltal", kind),
            ]
            identity = identity_of(*docs)
            assert merged(docs, identity) == [], kind
            towns = frozenset(doc.fingerprint("p0", identity) for doc in docs)
            assert links_of(identity)[towns] == (
                AMBIGUOUS,
                "key/unknown/unknown",
            ), kind
            registries = frozenset(
                doc.fingerprint("e1", identity) for doc in docs
            )
            assert links_of(identity)[registries] == (
                AMBIGUOUS,
                "none/unknown/differ",
            ), kind

    def test_a_chain_of_part_of_claims_nests_a_place(self):
        """A Fläckfeld that part_of claims put in Kreis Mockburg, which
        they put in Hesse, nests in a 'Fläckfeld, Hesse': its containers
        are the whole chain."""
        chain = register(
            "chain",
            [
                place("p0", "Fläckfeld"),
                place("p1", "Kreis Mockburg"),
                place("p2", "Hesse"),
            ],
            [("p0", "part_of", item("p1")), ("p1", "part_of", item("p2"))],
            {"p0": "flaeckfeld", "p1": "kreis-mockburg", "p2": "hesse"},
        )
        hesse = register(
            "in Hesse",
            [
                place(
                    "p0",
                    "Fläckfeld, Hesse",
                    names=["Fläckfeld"],
                    within=["Hesse"],
                )
            ],
            [],
            {"p0": "flaeckfeld"},
        )
        docs = [chain, hesse]
        assert {"flaeckfeld"} in merged(docs, identity_of(*docs))

    def test_a_proper_name_of_two_words_links_alone(self):
        """Two registers name 'the Blue Hat', a proper name of two words,
        with no kind and no place: they link on the name alone."""
        docs = [
            register(
                f"blue hat {n}",
                [organization("e1", "the Blue Hat", names=["Blue Hat"])],
                [],
                {"e1": "zum-blauen-hut"},
            )
            for n in range(2)
        ]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"zum-blauen-hut"}]
        assert {
            identity["mentions"][doc.fingerprint("e1", identity)]["method"]
            for doc in docs
        } == {CONFIDENT}

    def test_record_places_come_before_location_claims(self):
        """A shop whose record names Flörburg is in Flörburg, whatever
        place a location claim of its manifest names: it links with
        another in Flörburg."""

        def shop(key: str, claimed: str | None) -> Doc:
            return register(
                key,
                [
                    organization(
                        "e1",
                        "Hutmacherei zum Blauen Hut",
                        names=["zum Blauen Hut"],
                        kinds=["Hutmacherei"],
                        places=["Flörburg"],
                    ),
                    *([place("p0", claimed)] if claimed else []),
                ],
                [("e1", "location", item("p0"))] if claimed else [],
                {
                    "e1": "zum-blauen-hut",
                    **({"p0": claimed} if claimed else {}),
                },
            )

        docs = [shop("claimed", "Rekau"), shop("record only", None)]
        identity = identity_of(*docs)
        assert {"zum-blauen-hut"} in merged(docs, identity)
        shops = frozenset(doc.fingerprint("e1", identity) for doc in docs)
        assert identity["mentions"][min(shops)]["method"] == CONFIDENT

    def test_an_organization_holding_anothers_name_is_not_it(self):
        """'Blauen Hut' is part of 'zum Blauen Hut': no pair."""
        book = directory()
        short = register(
            "short",
            [organization("e1", "Blauen Hut", names=["Blauen Hut"])],
            [],
            {"e1": "zum-blauen-hut"},
        )
        assert blocked_pairs([book.manifest, short.manifest]) == set()

    def test_an_institution_named_by_its_kind_needs_a_place(self):
        """'Städtische Realschule' names a school in every town: two
        registers name one in Flörburg and one in Peiltal, which stay
        apart, and so do the schools of the rival districts' Moshausens;
        one at a street in Flörburg merges with Flörburg's, the place it
        lies in being theirs. 'Standesamt Flörburg' names its kind and its
        town, and merges on the two (placeholders)."""

        def school(key: str, places: list[str], truth: str) -> Doc:
            entities = [
                organization(
                    "e1", "Städtische Realschule", kinds=["Realschule"]
                ),
                *(place(f"p{n}", name) for n, name in enumerate(places)),
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
                    **{f"p{n}": name for n, name in enumerate(places)},
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
        assert blocked_pairs([doc.manifest for doc in docs]) == set()
        # two Moshausens their records put in two districts stay
        # ambiguous (rule 5), so the schools' places do not agree
        rivals = [
            register(
                f"in Moshausen, {district}",
                [
                    organization(
                        "e1", "Städtische Realschule", kinds=["Realschule"]
                    ),
                    place(
                        "p0",
                        f"Moshausen, {district}",
                        names=["Moshausen"],
                        within=[district],
                    ),
                ],
                [("e1", "location", item("p0"))],
                {"e1": f"realschule-{n}", "p0": f"moshausen-{n}"},
            )
            for n, district in enumerate(["Kreis Peiltal", "Kreis Mockburg"])
        ]
        identity = identity_of(*rivals)
        assert_no_false_merge(rivals, identity)
        schools = [doc.fingerprint("e1", identity) for doc in rivals]
        assert links_of(identity)[frozenset(schools)] == (
            AMBIGUOUS,
            "none/unknown/differ",
        )
        assert {identity["mentions"][m]["method"] for m in schools} == {
            AMBIGUOUS
        }
        towns = [doc.fingerprint("p0", identity) for doc in rivals]
        assert links_of(identity)[frozenset(towns)][0] == AMBIGUOUS
        docs = [floerburg, street]
        identity = identity_of(*docs)
        assert {"realschule-f"} in merged(docs, identity)
        named = [
            register(
                f"named-{n}",
                [
                    organization(
                        "e1",
                        "Standesamt Flörburg",
                        kinds=["Standesamt"],
                        places=["Flörburg"],
                    )
                ],
                [],
                {"e1": "floerburg-registry"},
            )
            for n in range(2)
        ]
        assert merged(named, identity_of(*named)) == [{"floerburg-registry"}]

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
                [
                    human(
                        "e1",
                        "N.N. Yyy",
                        surnames=["Yyy"],
                        status="placeholder",
                    ),
                    human("e2", "Zzz Yyy"),
                ],
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

        def child(name: str, parent: dict, relation: str, truth: str) -> Doc:
            return register(
                name,
                [human("e1", "Xxx Yyy"), parent],
                [("e1", relation, item("e2"))],
                {"e1": truth, "e2": f"{truth} {relation}"},
            )

        frau = human("e2", "Frau Yyy", titles=["Frau"], surnames=["Yyy"])
        herr = human("e2", "Herr Yyy", titles=["Herr"], surnames=["Yyy"])
        bare = human("e2", "Yyy", surnames=["Yyy"])
        docs = [
            child("one", frau, "mother", "xxx-one"),
            child("two", frau, "mother", "xxx-two"),
            child("three", herr, "father", "xxx-three"),
            child("four", bare, "mother", "xxx-four"),
            child("five", bare, "father", "xxx-five"),
        ]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert merged(docs, identity) == []
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

    def test_stated_sexes_that_differ_keep_a_pair_apart(self):
        """Two registers name a Xxx Yyy born 1911, one a daughter and one
        a son: their sexes only block, and the closure that joins them
        through a third, of no stated sex, contradicts (placeholders)."""

        def child(name: str, sex: str | None) -> Doc:
            claims = [("e1", "date_of_birth", day(1911))]
            if sex:
                claims.append(("e1", "sex_or_gender", vocab(sex)))
            return register(
                name, [human("e1", "Xxx Yyy")], claims, {"e1": name}
            )

        docs = [child("daughter", "female"), child("son", "male")]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        assert set(links_of(identity).values()) == {
            (AMBIGUOUS, "key/agree/unknown/sex")
        }
        three = [*docs, child("either", None)]
        identity = identity_of(*three)
        assert merged(three, identity) == []
        # alike, they merge
        alike = [child("one", "female"), child("two", "female")]
        assert len(merged(alike, identity_of(*alike))) == 1

    def test_civil_statuses_that_cannot_both_hold_keep_a_pair_apart(self):
        """The directory's widow Ursula Löbig (1912) never merges with an
        Ursula Löbig a later source calls never married; one called
        unmarried before 1912 may be her."""

        def later(name: str, year: int) -> Doc:
            return register(
                name,
                [human("e1", "Ursula Löbig")],
                [
                    (
                        "e1",
                        "civil_status",
                        vocab("unmarried"),
                        ("point_in_time", day(year)),
                    ),
                    ("e1", "residence", item("e1")),
                ],
                {"e1": name},
            )

        widow = document(
            "directory-1912",
            [human("e1", "Ursula Löbig")],
            [
                (
                    "e1",
                    "civil_status",
                    vocab("widowed"),
                    ("point_in_time", day(1912)),
                ),
                ("e1", "date_of_birth", day(1850)),
            ],
            {"e1": "ursula-loebig-angerring"},
        )

        def born(name: str, year: int) -> Doc:
            doc = later(name, year)
            batch = doc.manifest.batch.model_copy(deep=True)
            return document(
                "a register",
                [e.model_dump() for e in batch.entities],
                [
                    (
                        "e1",
                        "civil_status",
                        vocab("unmarried"),
                        ("point_in_time", day(year)),
                    ),
                    ("e1", "date_of_birth", day(1850)),
                ],
                {"e1": name},
                sha1=doc.sha1,
            )

        after = born("after", 1930)
        identity = identity_of(widow, after)
        assert merged([widow, after], identity) == []
        assert set(links_of(identity).values()) == {
            (AMBIGUOUS, "key/agree/unknown/civil status")
        }
        # the block reads both ways, whichever of the two sorts first
        (widowed,) = clustering._nodes(widow.manifest, 0)
        (unmarried,) = clustering._nodes(after.manifest, 0)
        blocked = clustering._Judge.blocked
        assert blocked(widowed, unmarried) == "civil status"
        assert blocked(unmarried, widowed) == "civil status"
        before = born("before", 1870)
        identity = identity_of(widow, before)
        assert len(merged([widow, before], identity)) == 1
        # unmarried in the year she is widowed is not wholly after it
        same = born("same year", 1912)
        identity = identity_of(widow, same)
        assert len(merged([widow, same], identity)) == 1
        # the year after is
        next_year = born("next year", 1913)
        identity = identity_of(widow, next_year)
        assert merged([widow, next_year], identity) == []

        # a decade on either side is all its years
        def decade(name: str, status: str, year: int) -> Doc:
            return register(
                name,
                [human("e1", "Ursula Löbig")],
                [
                    (
                        "e1",
                        "civil_status",
                        vocab(status),
                        (
                            "point_in_time",
                            {
                                "type": "time",
                                "year": year,
                                "precision": "decade",
                            },
                        ),
                    ),
                    ("e1", "date_of_birth", day(1850)),
                ],
                {"e1": name},
            )

        unmarried = decade("unmarried 1910s", "unmarried", 1910)
        identity = identity_of(widow, unmarried)
        assert len(merged([widow, unmarried], identity)) == 1
        widowed = decade("widowed 1910s", "widowed", 1910)
        unwed = born("in 1915", 1915)
        identity = identity_of(widowed, unwed)
        assert len(merged([widowed, unwed], identity)) == 1


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
                [human("e1", "Ursula Löbig")],
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

    def test_one_ambiguous_link_leaves_its_confident_component_merged(self):
        """Three registers give a Xxx Yyy born 1911, a fourth an undated
        one: the three link confidently, the fourth ambiguously with each.
        The candidate cluster is ambiguous, but its confident component,
        the three, merges (#568); the fourth stays apart (placeholders)."""

        def person(name: str, year: int | None) -> Doc:
            claims = [("e1", "date_of_birth", day(year))] if year else []
            return register(
                name, [human("e1", "Xxx Yyy")], claims, {"e1": "xxx"}
            )

        docs = [person(name, 1911) for name in ("one", "two", "three")]
        docs.append(person("four", None))
        identity = identity_of(*docs)
        methods = [
            identity["mentions"][doc.fingerprint("e1", identity)]["method"]
            for doc in docs
        ]
        assert methods == [CONFIDENT_COMPONENT] * 3 + [AMBIGUOUS]
        assert (
            len(
                {
                    identity["mentions"][doc.fingerprint("e1", identity)][
                        "cluster"
                    ]
                    for doc in docs
                }
            )
            == 2
        )
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
                {"floerburg"},
                {"gustav-loebig"},
                {"hatter"},
                {"hawthorn-road-8"},
                {"hildegard-loebig"},
                # a jurisdiction of the export's PLACs is a place of its
                # own, and meets the certificate's (#571)
                {"kreis-mockburg"},
                {"sebastian-loebig"},
                # the export's unions (ADR-0036 §5) meet the certificate's
                # through their couples, once both spouses merged
                {"eugen-and-caecilie"},
                {"sebastian-and-hildegard"},
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
        # Flörburg nests in the export's one Flörburg, and "Floerburg"
        # meets it too; the vessel meets nothing and is a singleton
        town = mention_of(docs, identity, "birth-extract-1938", "e4")
        assert identity["mentions"][town]["method"] == CONFIDENT
        ship = mention_of(docs, identity, "naturalisation-1952", "e4")
        assert identity["mentions"][ship]["method"] == SINGLETON

    def test_rival_districts_stay_apart_and_nesting_is_confident(self):
        """Fläckfeld lies in Kreis Mockburg per the certificate and in
        Kreis Peiltal per the tree: the contradiction coexists, so the two
        stay apart, while the bare name nests in either."""
        certificate = document(
            "marriage-cert-1905",
            [
                place(
                    "e1",
                    "Fläckfeld, Kreis Mockburg",
                    names=["Fläckfeld"],
                    within=["Kreis Mockburg"],
                )
            ],
            [],
            {"e1": "flaeckfeld"},
        )
        tree = document(
            "family-tree-1984",
            [
                place(
                    "e1",
                    "Fläckfeld, Kreis Peiltal",
                    names=["Fläckfeld"],
                    within=["Kreis Peiltal"],
                )
            ],
            [],
            {"e1": "flaeckfeld"},
        )
        bare = document(
            "interview-2002",
            [place("e1", "Fläckfeld")],
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

    def test_containers_compare_as_one_set(self):
        """The Frisian letter writes 'Snylsjum, gemeente Drakeradiel,
        Fryslân, Nederlân'; a register gives Snylsjum in Fryslân alone,
        and another in 'Fryslan, Nederlân' spelt otherwise. Each set lies
        among the letter's, whatever its order: they nest. A Snylsjum in
        a province of another name does not."""
        letter = document(
            "letter-to-sipkje-1952",
            [
                place(
                    "e1",
                    "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
                    names=["Snylsjum"],
                    within=["gemeente Drakeradiel", "Fryslân", "Nederlân"],
                )
            ],
            [],
            {"e1": "snylsjum"},
        )

        def snylsjum(name: str, within: list[str], truth: str) -> Doc:
            return register(
                name,
                [
                    place(
                        "e1",
                        ", ".join(["Snylsjum", *within]),
                        names=["Snylsjum"],
                        within=within,
                    )
                ],
                [],
                {"e1": truth},
            )

        province = snylsjum("province", ["Fryslân"], "snylsjum")
        reordered = snylsjum("reordered", ["Nederlân", "fryslan"], "snylsjum")
        other = snylsjum("other", ["Xxxprovinz"], "another-snylsjum")
        for doc in (province, reordered):
            identity = identity_of(letter, doc)
            assert merged([letter, doc], identity) == [{"snylsjum"}]
            assert {p[1] for p in links_of(identity).values()} <= {
                "nested/unknown/unknown"
            }
        identity = identity_of(letter, other)
        assert merged([letter, other], identity) == []
        equal = snylsjum(
            "equal",
            ["Nederlân", "Fryslân", "Gemeente Drakeradiel"],
            "snylsjum",
        )
        identity = identity_of(letter, equal)
        assert merged([letter, equal], identity) == [{"snylsjum"}]


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
            [human("e1", "Gustav Löbig"), human("e2", "Sebastian Löbig")],
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
                [human("e1", "Xxx Yyy"), human("e2", f"{father} Yyy")],
                [("e1", "father", item("e2"))],
                {"e1": f"xxx-of-{father}", "e2": father},
            )

        docs = [child("one", "Zzz"), child("two", "Www")]
        identity = identity_of(*docs)
        assert merged(docs, identity) == []
        (review,) = identity["review"]
        assert [(p["link"], p["evidence"]) for p in review["pairs"]] == [
            (AMBIGUOUS, "key/unknown/unknown")
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
                [human("e1", "Xxx Yyy"), human("e2", "Xxx Yyy")],
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
                [human("e1", "Xxx Yyy"), human("e2", "Zzz Yyy")],
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
            (AMBIGUOUS, "key/conflict/agree")
        ]

    def test_kin_of_one_name_whose_dates_conflict_keep_a_pair_apart(self):
        """Two registers name a Xxx Yyy born 1919, one's father a Zzz Yyy
        born 1862, the other's a Zzz Yyy born 1890: the children's dates
        agree, and their fathers, of one name, differ. The link is
        ambiguous (placeholders)."""

        def child(name: str, father_born: int) -> Doc:
            return register(
                name,
                [human("e1", "Xxx Yyy"), human("e2", "Zzz Yyy")],
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
            (AMBIGUOUS, "key/agree/unknown")
        ]

    def test_dates_that_cannot_overlap_never_prune_a_pair(self):
        """Contradictory dates are a resting state (ADR-0002): the pair
        stays a candidate, ambiguous, for adjudication."""

        def register(label: bytes, year: int) -> Doc:
            return document(
                "a register",
                [human("e1", "Xxx Yyy")],
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
            "key/conflict/unknown",
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
            [
                human(
                    "e1",
                    "Gustave Lobig",
                    aliases=["Gustav Lobig"],
                    givens=["Gustave", "Gustav"],
                    surnames=["Lobig"],
                ),
                human("e2", "Gustav Löbig"),
            ],
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
        Peiltal. The two Peiltals merge, and the prison, which does not
        key on its toponym, meets neither."""
        docs = [registration_card(), interview()]
        identity = identity_of(*docs)
        assert merged(docs, identity) == [{"peiltal"}]
        prison = mention_of(docs, identity, "registration-card-1945", "e3")
        assert identity["mentions"][prison]["method"] == SINGLETON
        summary = identity["summary"]
        assert summary["pruned_pairs"] == {}

    def test_a_containment_rules_a_pair_out(self):
        """A register puts a place named 'Peiltal' inside another it names
        'Peiltal' too, the district of the town, by `location`; lifted,
        the containment rules the two out (ADR-0017), so the interview's
        Peiltal meets both, never as one."""
        card = register(
            "district",
            [
                place("e3", "Peiltal", lines=(1,)),
                place("e4", "Peiltal", lines=(2,)),
            ],
            [("e3", "location", item("e4"))],
            {"e3": "peiltal", "e4": "peiltal-district"},
        )
        docs = [card, interview()]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        assert identity["summary"]["within_manifest_kin_or_containment"] == 1

    def test_a_closure_joining_a_place_and_its_container_conflicts_hard(
        self,
    ):
        """A register puts a place named 'Klurnau' in another it names
        'Klurnau'; the card names Klurnau. The closure joins all three,
        a place and its container among them: a hard conflict."""
        record = register(
            "camp",
            [
                place("e1", "Klurnau", lines=(1,)),
                place("e2", "Klurnau", lines=(2,)),
            ],
            [("e1", "part_of", item("e2"))],
            {"e1": "klurnau-camp", "e2": "klurnau"},
        )
        card = document(
            "registration-card-1945",
            [place("e1", "Klurnau")],
            [],
            {"e1": "klurnau"},
        )
        docs = [record, card]
        identity = identity_of(*docs)
        assert_no_false_merge(docs, identity)
        (review,) = identity["review"]
        assert (review["status"], review["conflict"]["how"]) == (
            CONFLICTING,
            HARD,
        )

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
            [place("e1", "Fläckfeld")],
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
        are no statement that they differ: two that nest merge without a
        conflict."""
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
        identity = identity_of(ged)
        assert merged([ged], identity) == [{"flaeckfeld"}]


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
            judgment(),
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

    def test_the_summary_counts_recordless_mentions(self):
        """Every mention of the corpus has its record, but the export's
        occupations and media, which take none."""
        summary = identity_of(*self.corpus())["summary"]
        assert summary["recordless_mentions"] == 0
        assert summary["recordless_by_kind"] == {}

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
        other, recordless, and never kept apart from each other: they
        pair, and never merge."""
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
        assert {e["method"] for e in entries} == {AMBIGUOUS}
        assert len({e["cluster"] for e in entries}) == 2
        summary = identity["summary"]
        assert summary["documents_without_manifest"] == 1
        assert summary["recordless_mentions"] == 2
        assert summary["conflicting_hard"] == 0
        assert summary["within_manifest_pairs"] == 0


def test_two_test_documents_of_one_sha1_are_refused():
    """The test helper refuses two documents of one SHA-1, which the
    corpus would read as one, so that no test compares nothing."""
    with pytest.raises(AssertionError, match="share a SHA-1"):
        identity_of(directory(), directory())


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
                entities.append(
                    human(
                        f"e{index}",
                        f"{rng.choice(givens)} {rng.choice(surnames)}",
                    )
                )
            else:
                entities.append(place(f"e{index}", rng.choice(towns)))
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
        [human(f"e{n}", "Xxx Yyy") for n in range(7)],
        [
            (f"e{n}", "sibling", item(f"e{m}"))
            for n in range(7)
            for m in range(n + 1, 7)
        ],
        {},
    )
    another = register("another", [human("e1", "Xxx Yyy")], [], {})
    return [siblings.manifest, another.manifest]


# -- the thresholds -----------------------------------------------------------


class TestThresholds:
    def test_the_defaults(self):
        config = load_config()
        assert config.blocking.gedcom_anchored is True
        assert config.blocking.max_key_mentions == 200
        assert config.evidence.circa_years == 5
        assert config.confident.max_anchors == 1
        assert config.adjudication.pairs_per_call == 40

    def test_a_file_over_the_defaults(self, tmp_path):
        path = tmp_path / "clustering.yaml"
        path.write_text("blocking:\n  max-key-mentions: 50\n")
        config = load_config(path)
        assert config.blocking.max_key_mentions == 50
        assert config.digest() != load_config().digest()

    @pytest.mark.parametrize(
        "text",
        [
            # the token lengths are gone: keys are whole parts
            "blocking:\n  min-place-token: 5\n",
            "blocking:\n  min-name-token: 3\n",
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

    def test_a_changed_record_makes_it_again(self, tmp_path):
        """A record read otherwise, the label and aliases the same: the
        batch's hash covers the record, so the map is made again."""
        directory_ = tmp_path / "corpus" / "clustering"
        documents = self.docs(tmp_path)
        made = ensure_identity_map(directory_, documents)
        batch = ClaimBatch.model_validate_json(documents[1].batch.read_text())
        batch.entities[0].structure.status = "description"
        documents[1].batch.write_text(batch.model_dump_json())
        again = ensure_identity_map(directory_, documents)
        assert not again.reused
        assert again.identity_map["inputs"] != made.identity_map["inputs"]

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
        """The digest is of the bytes of clustering.py, fingerprints.py,
        reconcile.py, writer.py and names.py: a byte changed in any of
        them changes it."""
        assert clustering.CODE == (
            "clustering.py",
            "fingerprints.py",
            "reconcile.py",
            "writer.py",
            "names.py",
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
