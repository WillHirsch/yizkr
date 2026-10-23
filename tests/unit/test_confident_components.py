"""The confident components of an ambiguous candidate cluster (#568): each
closure over the cluster's confident links merges, re-checked with the
member-contradiction and cannot-link checks the cluster check uses, with
the method `confident_component`; only the ambiguous pairs between
components are left for adjudication, in development runs and in keeper
mode alike.

Every document is a register of the fictional universe
(tests/fixtures/example_universe.py): Gustav Löbig, dated, undated or
named with his father Sebastian; Nicholas Lobig, his birth read 1950 and
misread 1850 (the fixture corpus's misread year); and the two Ursula
Löbigs the universe keeps apart, Sebastian's daughter and the widow of
the 1912 directory. No wiki, no model.
"""

from __future__ import annotations

from tests.unit.test_adjudication import (
    Engine,
    context_of,
    gustav,
    klara,
    run,
    truths,
)
from tests.unit.test_clustering import (
    Doc,
    assert_no_false_merge,
    day,
    export,
    human,
    item,
    register,
)
from yizkr.pipeline import adjudication, clustering, report, rundiff
from yizkr.pipeline.clustering import (
    ADJUDICATED,
    AMBIGUOUS,
    CONFIDENT,
    CONFIDENT_COMPONENT,
    CONFLICTING,
    DIFFERENT,
    SAME,
    SOFT,
    UNCERTAIN,
    cluster_corpus,
    pending_pairs,
)
from yizkr.pipeline.fingerprints import cluster_id


def gustav_with(name: str, *, born: int | None, father: bool) -> Doc:
    """A register of Gustav Löbig: his birth year, or none, and his father
    Sebastian, or no kin."""
    entities = [human("e1", "Gustav Löbig")]
    claims = [("e1", "date_of_birth", day(born))] if born else []
    truth = {"e1": "gustav-loebig"}
    if father:
        entities.append(human("e2", "Sebastian Löbig"))
        claims.append(("e1", "father", item("e2")))
        truth["e2"] = "sebastian-loebig"
    return register(name, entities, claims, truth)


def nicholas(name: str, year: int | None) -> Doc:
    """A register of Nicholas Lobig, with his father Gustav, and the birth
    year it gives, or none."""
    claims = [("e1", "father", item("e2"))]
    if year:
        claims.append(("e1", "date_of_birth", day(year)))
    return register(
        name,
        [human("e1", "Nicholas Lobig"), human("e2", "Gustav Löbig")],
        claims,
        {"e1": "nicholas-lobig", "e2": "gustav-loebig"},
    )


def nicholas_alone(name: str, year: int | None) -> Doc:
    """A register of Nicholas Lobig alone, with the birth year it gives,
    or none."""
    claims = [("e1", "date_of_birth", day(year))] if year else []
    return register(
        name,
        [human("e1", "Nicholas Lobig")],
        claims,
        {"e1": "nicholas-lobig"},
    )


def triangle() -> list[Doc]:
    """Gustav thrice: once with his birth year, once with his birth year
    and his father, once with his father alone. The first two link on
    their dates, the last two on their kin, and the first and the last,
    with nothing to corroborate their name, ambiguously."""
    return [
        gustav_with("dated", born=1919, father=False),
        gustav_with("both", born=1919, father=True),
        gustav_with("kin", born=None, father=True),
    ]


def three_with_one_undated() -> list[Doc]:
    """Three registers give Gustav's birth date and link confidently; a
    fourth gives none and links ambiguously with each."""
    docs = [gustav(n, dated=True) for n in ("one", "two", "three")]
    docs.append(gustav("four", dated=False))
    return docs


def identity_of(docs: list[Doc], verdicts=None) -> dict:
    return cluster_corpus([doc.manifest for doc in docs], None, verdicts)


def mention(identity: dict, doc: Doc, local_id: str = "e1") -> dict:
    return identity["mentions"][doc.fingerprint(local_id, identity)]


def entry_of(identity: dict, doc: Doc, local_id: str = "e1") -> dict:
    fingerprint = doc.fingerprint(local_id, identity)
    (found,) = [
        entry
        for entry in identity["review"]
        if fingerprint in entry["members"]
    ]
    return found


def ambiguous_pairs(entry: dict) -> list[tuple[str, str]]:
    return [
        clustering._pair(*pair["mentions"])
        for pair in entry["pairs"]
        if pair["link"] == AMBIGUOUS
    ]


class TestTheComponentMerges:
    def test_the_confident_component_of_an_ambiguous_cluster_merges(self):
        """The three dated registers are the confident component of an
        ambiguous candidate cluster: they merge, `confident_component`,
        and the undated one stays apart, its three ambiguous pairs, all
        between the component and it, left pending."""
        docs = three_with_one_undated()
        identity = identity_of(docs)
        assert_no_false_merge(docs, identity)
        dated = {mention(identity, doc)["cluster"] for doc in docs[:3]}
        assert len(dated) == 1
        assert mention(identity, docs[3])["cluster"] not in dated
        assert [mention(identity, doc)["method"] for doc in docs] == [
            CONFIDENT_COMPONENT
        ] * 3 + [AMBIGUOUS]
        (cid,) = dated
        assert identity["clusters"][cid]["method"] == CONFIDENT_COMPONENT
        entry = entry_of(identity, docs[0])
        assert entry["status"] == AMBIGUOUS
        assert entry["contradiction"] is None
        assert [
            pair["inside_component"]
            for pair in entry["pairs"]
            if pair["link"] == AMBIGUOUS
        ] == [False] * 3
        assert [len(pairs) for _group, pairs in pending_pairs([entry])] == [3]
        summary = identity["summary"]
        assert summary["clusters"][CONFIDENT_COMPONENT] == 1
        assert summary["cluster_mentions"][CONFIDENT_COMPONENT] == 3
        assert summary["clusters"][CONFIDENT] == 0
        assert summary["clusters"][AMBIGUOUS] == 1
        assert summary["cluster_mentions"][AMBIGUOUS] == 1
        assert summary["merged_mentions"] == 3

    def test_an_ambiguous_pair_inside_a_merged_component_is_settled(self):
        """The triangle's one ambiguous pair lies inside its confident
        component, which joins all three: the component merges whole, and
        the pair is settled by it, never pending, so adjudication asks
        nothing of the cluster."""
        docs = triangle()
        identity = identity_of(docs)
        assert_no_false_merge(docs, identity)
        assert {mention(identity, doc)["method"] for doc in docs} == {
            CONFIDENT_COMPONENT
        }
        assert len({mention(identity, doc)["cluster"] for doc in docs}) == 1
        entry = entry_of(identity, docs[0])
        assert entry["status"] == AMBIGUOUS
        (pair,) = [p for p in entry["pairs"] if p["link"] == AMBIGUOUS]
        assert pair["inside_component"] is True
        assert pending_pairs([entry]) == []
        assert identity["summary"]["clusters"][AMBIGUOUS] == 0
        assert (
            identity["summary"]["adjudication"]["calls_ambiguous_clusters"]
            == 0
        )
        engine = Engine(truths(docs))
        made, clients = run(docs, engine)
        assert engine.prompts == [] and clients == []
        assert made.identity["hash"] == identity["hash"]


class TestTheRecheck:
    def test_a_member_contradiction_keeps_the_component_apart(self):
        """Nicholas's registers each name his father Gustav: the one read
        1950 and the one misread 1850 each link to the undated one on
        their kin, so the three are one confident component, but the two
        dated ones' years cannot overlap, and the re-check keeps every
        member apart, `ambiguous`. Their pair is not inside a merged
        component, so it stays pending."""
        docs = [
            nicholas("read", 1950),
            nicholas("silent", None),
            nicholas("misread", 1850),
        ]
        identity = identity_of(docs)
        entry = entry_of(identity, docs[0])
        assert sorted(p["link"] for p in entry["pairs"]) == [
            AMBIGUOUS,
            CONFIDENT,
            CONFIDENT,
        ]
        assert {mention(identity, doc)["method"] for doc in docs} == {
            AMBIGUOUS
        }
        assert len({mention(identity, doc)["cluster"] for doc in docs}) == 3
        assert entry["status"] == AMBIGUOUS
        assert entry["contradiction"]["evidence"] == "key/conflict/agree"
        assert set(entry["contradiction"]["mentions"]) == {
            docs[0].fingerprint("e1", identity),
            docs[2].fingerprint("e1", identity),
        }
        assert [len(pairs) for _group, pairs in pending_pairs([entry])] == [1]

    def test_two_members_never_paired_that_contradict_keep_it_apart(self):
        """The export gives Gustav a son, Nicholas Lobig, with no date;
        the register that reads his birth 1950 and the one that misreads
        it 1850 each name his father Gustav, and an undated register names
        Nicholas alone. Each dated register meets the export's Nicholas,
        its anchor, and links to it on their kin, so the three are one
        confident component; the two dated ones are never a pair, but
        their years cannot overlap. The re-check reads every two members
        of a component, not only its links, so the component stays
        apart: a chain of confident links joins no two people that no one
        link would."""
        ged = export(
            "0 @I1@ INDI\n1 NAME Gustav /Löbig/\n1 SEX M\n"
            "0 @I2@ INDI\n1 NAME Nicholas /Lobig/\n1 SEX M\n"
            "0 @F1@ FAM\n1 HUSB @I1@\n1 CHIL @I2@\n",
            {"i1": "gustav-loebig", "i2": "nicholas-lobig"},
        )
        read, misread = nicholas("read", 1950), nicholas("misread", 1850)
        docs = [ged, read, misread, nicholas_alone("undated", None)]
        identity = identity_of(docs)
        assert_no_false_merge(docs, identity)
        dated = {
            read.fingerprint("e1", identity),
            misread.fingerprint("e1", identity),
        }
        entry = entry_of(identity, read)
        assert dated not in [set(p["mentions"]) for p in entry["pairs"]]
        assert entry["status"] == AMBIGUOUS
        assert set(entry["contradiction"]["mentions"]) == dated
        assert {
            p["link"] for p in entry["pairs"] if set(p["mentions"]) & dated
        } == {CONFIDENT}
        assert (
            mention(identity, read)["cluster"]
            != (mention(identity, misread)["cluster"])
        )
        assert {mention(identity, doc)["method"] for doc in docs[1:]} == {
            AMBIGUOUS
        }

    def test_a_same_verdict_joins_a_component_kept_apart(self):
        """A `same` on the pending pair excuses its own dates' conflict
        (ADR-0007, as amended by #106): the part it joins merges,
        `adjudicated`, since a verdict is among its links."""
        docs = [
            nicholas("read", 1950),
            nicholas("silent", None),
            nicholas("misread", 1850),
        ]
        entry = entry_of(identity_of(docs), docs[0])
        settled = identity_of(
            docs, {pair: SAME for pair in ambiguous_pairs(entry)}
        )
        assert {mention(settled, doc)["method"] for doc in docs} == {
            ADJUDICATED
        }
        assert len({mention(settled, doc)["cluster"] for doc in docs}) == 1

    def test_a_component_holding_a_pair_said_different_stays_apart(self):
        """`different` on the triangle's ambiguous pair, though a merged
        component held it: a `different` is a cannot-link, so the
        component holds a pair it may not, and every member stays apart,
        `ambiguous`."""
        docs = triangle()
        entry = entry_of(identity_of(docs), docs[0])
        (pair,) = ambiguous_pairs(entry)
        settled = identity_of(docs, {pair: DIFFERENT})
        assert {mention(settled, doc)["method"] for doc in docs} == {AMBIGUOUS}
        assert len({mention(settled, doc)["cluster"] for doc in docs}) == 3
        settled_entry = entry_of(settled, docs[0])
        assert settled_entry["contradiction"]["evidence"] == DIFFERENT
        assert set(settled_entry["contradiction"]["mentions"]) == set(pair)
        assert settled["summary"]["clusters"][CONFIDENT_COMPONENT] == 0
        assert settled["summary"]["cluster_mentions"][AMBIGUOUS] == 3

    def test_a_verdict_inside_a_merged_component_says_nothing_of_it(self):
        """The triangle and an undated register of Gustav, which links
        ambiguously with each of the three. A `same` given on the pair
        inside the merged component, and `different` on every pair
        between it and the undated one: the component's merge is its
        confident links', so it stays `confident_component`, and the
        undated one is left alone, `adjudicated`."""
        docs = [*triangle(), gustav_with("undated", born=None, father=False)]
        identity = identity_of(docs)
        entry = entry_of(identity, docs[0])
        inside = [
            clustering._pair(*p["mentions"])
            for p in entry["pairs"]
            if p["link"] == AMBIGUOUS and p["inside_component"]
        ]
        between = [p for p in ambiguous_pairs(entry) if p not in inside]
        assert (len(inside), len(between)) == (1, 3)
        settled = identity_of(
            docs,
            {inside[0]: SAME, **{pair: DIFFERENT for pair in between}},
        )
        assert [mention(settled, doc)["method"] for doc in docs] == [
            CONFIDENT_COMPONENT
        ] * 3 + [ADJUDICATED]
        assert settled["summary"]["adjudicated_apart"] == 1
        # the settled review still says which pair the component settled
        settled_entry = entry_of(settled, docs[0])
        assert settled_entry["status"] == ADJUDICATED
        assert {
            clustering._pair(*p["mentions"]): p["inside_component"]
            for p in settled_entry["pairs"]
            if p["link"] == AMBIGUOUS
        } == {inside[0]: True, **{pair: False for pair in between}}

    def test_an_uncertain_pair_inside_a_component_leaves_it_merged(self):
        """`uncertain` is no cannot-link: the component still merges, as
        it does without verdicts."""
        docs = triangle()
        identity = identity_of(docs)
        (pair,) = ambiguous_pairs(entry_of(identity, docs[0]))
        settled = identity_of(docs, {pair: UNCERTAIN})
        assert settled["hash"] == identity["hash"]

    def test_a_conflicting_cluster_never_merges_its_component(self):
        """The register names both Ursulas, the daughter with her father
        Sebastian; two more registers give the daughter with him. The
        three daughters link confidently on their kin, and the widow
        ambiguously with the other registers' daughters, so the closure
        joins the two Ursulas of one manifest: a conflicting cluster
        (soft). Its confident component, the three daughters, merges
        nothing, nor would a `same` on every pair."""
        tree = register(
            "the tree",
            [
                human("e1", "Ursula Löbig"),
                human("e2", "Sebastian Löbig"),
                human("e3", "Ursula Löbig"),
            ],
            [("e1", "father", item("e2"))],
            {
                "e1": "ursula-loebig",
                "e2": "sebastian-loebig",
                "e3": "ursula-loebig-angerring",
            },
        )

        def daughter(name: str) -> Doc:
            return register(
                name,
                [human("e1", "Ursula Löbig"), human("e2", "Sebastian Löbig")],
                [("e1", "father", item("e2"))],
                {"e1": "ursula-loebig", "e2": "sebastian-loebig"},
            )

        docs = [tree, daughter("one"), daughter("two")]
        identity = identity_of(docs)
        assert_no_false_merge(docs, identity)
        entry = entry_of(identity, tree)
        assert entry["status"] == CONFLICTING
        assert entry["conflict"]["how"] == SOFT
        assert (
            sorted(p["link"] for p in entry["pairs"])
            == [AMBIGUOUS] * 2 + [CONFIDENT] * 3
        )
        ursulas = [
            (tree, "e1"),
            (tree, "e3"),
            (docs[1], "e1"),
            (docs[2], "e1"),
        ]
        assert {mention(identity, *u)["method"] for u in ursulas} == {
            CONFLICTING
        }
        assert len({mention(identity, *u)["cluster"] for u in ursulas}) == 4
        assert identity["summary"]["clusters"][CONFIDENT_COMPONENT] == 0
        assert pending_pairs(identity["review"]) == []
        forced = identity_of(
            docs,
            {clustering._pair(*p["mentions"]): SAME for p in entry["pairs"]},
        )
        assert {mention(forced, *u)["method"] for u in ursulas} == {
            CONFLICTING
        }


class TestAdjudication:
    def test_a_part_falls_back_to_its_components_when_contradicted(self):
        """`same` joins the undated register to one dated one, `different`
        to the others: the closure joins a `different` pair, so the part
        does not merge, and falls back to its confident component, which
        holds no pair said `different` and merges as without verdicts."""
        docs = three_with_one_undated()
        identity = identity_of(docs)
        undated = docs[3].fingerprint("e1", identity)
        first = docs[0].fingerprint("e1", identity)
        verdicts = {
            pair: SAME if first in pair else DIFFERENT
            for pair in ambiguous_pairs(entry_of(identity, docs[0]))
        }
        assert all(undated in pair for pair in verdicts)
        settled = identity_of(docs, verdicts)
        assert [mention(settled, doc)["method"] for doc in docs] == [
            CONFIDENT_COMPONENT
        ] * 3 + [AMBIGUOUS]
        assert entry_of(settled, docs[0])["status"] == ADJUDICATED
        assert settled["summary"]["clusters"][AMBIGUOUS] == 1

    def test_an_adjudicated_part_joining_a_component_is_adjudicated(self):
        """`same` on every pair between the component and the undated
        register: the part joins the component and it, and every member
        is `adjudicated`, the component's merge now one the verdict
        made."""
        docs = three_with_one_undated()
        made, _ = run(docs, Engine(truths(docs)))
        assert made.counts["asked_pairs"] == 3
        assert {
            made.identity["mentions"][doc.fingerprint("e1", made.identity)][
                "method"
            ]
            for doc in docs
        } == {ADJUDICATED}
        summary = made.identity["summary"]
        assert summary["clusters"][ADJUDICATED] == 1
        assert summary["clusters"][CONFIDENT_COMPONENT] == 0

    def test_keeper_mode_merges_the_component_and_asks_between(self):
        """In keeper mode the verdicts merge nothing, but the map is
        clustering's own, so the component merges there too, and only
        the pairs between the component and the undated register are
        asked, for the review sheet."""
        docs = three_with_one_undated()
        engine = Engine(truths(docs))
        made, _ = run(docs, engine, merge=False)
        assert made.counts["merge"] is False
        assert made.counts["asked_pairs"] == 3
        assert made.identity["hash"] == identity_of(docs)["hash"]
        assert [
            made.identity["mentions"][doc.fingerprint("e1", made.identity)][
                "method"
            ]
            for doc in docs
        ] == [CONFIDENT_COMPONENT] * 3 + [AMBIGUOUS]
        # the triangle's one pair is inside its merged component: nothing
        # is asked of it in keeper mode either
        docs = triangle()
        engine = Engine(truths(docs))
        made, _ = run(docs, engine, merge=False)
        assert engine.prompts == []
        assert made.counts["asked_pairs"] == 0


def mixed() -> list[Doc]:
    """Klara twice, dated alike (a confident cluster); Gustav's three
    dated registers and an undated one (a confident component); and
    Nicholas dated 1950 and undated (an ambiguous pair)."""
    return [
        klara("read", None),
        klara("again", None),
        *three_with_one_undated(),
        nicholas_alone("read 1950", 1950),
        nicholas_alone("undated", None),
    ]


def mixed_verdicts(docs: list[Doc], identity: dict) -> dict:
    """`same` on Nicholas's pair, `different` on Gustav's."""
    nick = docs[6].fingerprint("e1", identity)
    return {
        pair: SAME if nick in pair else DIFFERENT
        for entry in identity["review"]
        for pair in ambiguous_pairs(entry)
    }


class TestTheCounts:
    def test_the_summary_counts_each_method_apart(self):
        docs = mixed()
        identity = identity_of(docs)
        summary = identity["summary"]
        assert {
            method: (
                summary["clusters"][method],
                summary["cluster_mentions"][method],
            )
            for method in clustering.SUMMARY_METHODS
        } == {
            CONFIDENT: (1, 2),
            CONFIDENT_COMPONENT: (1, 3),
            ADJUDICATED: (0, 0),
            # Gustav's undated register, and both Nicholases
            AMBIGUOUS: (2, 1 + 2),
            CONFLICTING: (0, 0),
        }
        assert summary["merged_mentions"] == 5
        settled = identity_of(docs, mixed_verdicts(docs, identity))
        summary = settled["summary"]
        assert summary["clusters"][CONFIDENT] == 1
        assert summary["cluster_mentions"][CONFIDENT] == 2
        assert summary["clusters"][CONFIDENT_COMPONENT] == 1
        assert summary["cluster_mentions"][CONFIDENT_COMPONENT] == 3
        assert summary["clusters"][ADJUDICATED] == 1
        assert summary["cluster_mentions"][ADJUDICATED] == 2
        assert summary["adjudicated_apart"] == 1
        assert summary["merged_mentions"] == 7
        # every merged mention is counted under the one method its
        # cluster gives it; the member a `different` left alone is no merge
        merged = [
            entry["method"]
            for entry in settled["clusters"].values()
            for _member in entry["members"]
            if len(entry["members"]) > 1
        ]
        for method in clustering.MERGING:
            assert summary["cluster_mentions"][method] == merged.count(method)

    def test_the_report_counts_confident_component_merges(self):
        summary = identity_of(mixed())["summary"]
        assert clustering.attention(summary)[0].startswith(
            "ambiguous clusters not merged:"
        )

        def counts_of(summary: dict) -> dict:
            from pathlib import Path
            from tempfile import TemporaryDirectory

            from yizkr.pipeline.scratch import CORPUS_DIR

            with TemporaryDirectory() as folder:
                path = Path(folder)
                stage = path / CORPUS_DIR / clustering.STAGE
                stage.mkdir(parents=True)
                (stage / clustering.IDENTITY_MAP).write_text(
                    __import__("json").dumps(
                        {"format": clustering.FORMAT, "summary": summary}
                    )
                )
                return report._clusters(path, report._Fold())["counts"]

        counts = counts_of(summary)
        assert counts["confident component merges"] == 1
        assert counts["mentions merged by confident component"] == 3
        assert counts["confident merges"] == 1
        assert counts["mentions merged"] == 2
        # a component alone: no confident merge to take it for
        counts = counts_of(identity_of(three_with_one_undated())["summary"])
        assert counts["confident component merges"] == 1
        assert counts["mentions merged by confident component"] == 3
        assert counts["confident merges"] == 0
        assert counts["mentions merged"] == 0

    def test_the_diff_lists_every_merge_the_rule_adds(self):
        """Against a baseline that left every mention apart, each
        confident component's merge is a merge of its own method in the
        diff's aggregates, and listed with its members in its detail."""
        docs = three_with_one_undated()
        identity = identity_of(docs)
        baseline = {
            "mentions": {
                fingerprint: {
                    **entry,
                    "cluster": cluster_id([fingerprint]),
                    "method": AMBIGUOUS,
                }
                for fingerprint, entry in identity["mentions"].items()
            },
            "clusters": {
                cluster_id([fingerprint]): {
                    "kind": entry["kind"],
                    "method": AMBIGUOUS,
                    "members": [fingerprint],
                }
                for fingerprint, entry in identity["mentions"].items()
            },
        }
        names = rundiff._Names.of({}, {})
        aggregate, detail = rundiff.compare_clusters(baseline, identity, names)
        assert aggregate["merges"] == 1
        assert aggregate["merges_by_method"] == {CONFIDENT_COMPONENT: 1}
        assert aggregate["methods_changed"] == {
            "ambiguous → confident_component": 3
        }
        (merge,) = detail["merges"]
        assert merge["method"] == CONFIDENT_COMPONENT
        assert len(merge["members"]) == 3
        from tests.unit.test_run_diff import an_aggregate

        summary = rundiff.render_markdown(an_aggregate(clusters=aggregate))
        assert "by method: confident_component 1" in summary
        assert "Gustav" not in summary


def two_components() -> list[Doc]:
    """Gustav in two registers that give his birth year, and in two that
    name his father Sebastian: the dated two link on their dates, the
    other two on their kin, and each dated one ambiguously with each
    of the others, with nothing to corroborate their name."""
    return [
        gustav_with("d1", born=1919, father=False),
        gustav_with("d2", born=1919, father=False),
        gustav_with("k1", born=None, father=True),
        gustav_with("k2", born=None, father=True),
    ]


class TestBetweenComponents:
    def test_the_pairs_between_two_components_are_pending(self):
        docs = two_components()
        identity = identity_of(docs)
        assert_no_false_merge(docs, identity)
        assert {mention(identity, doc)["method"] for doc in docs} == {
            CONFIDENT_COMPONENT
        }
        clusters = [mention(identity, doc)["cluster"] for doc in docs]
        assert clusters[0] == clusters[1] != clusters[2] == clusters[3]
        entry = entry_of(identity, docs[0])
        between = [p for p in entry["pairs"] if p["link"] == AMBIGUOUS]
        assert [p["inside_component"] for p in between] == [False] * 4
        ((_group, pending),) = pending_pairs([entry])
        assert sorted(pending) == sorted(ambiguous_pairs(entry))
        # left split between its components: an ambiguous cluster, with
        # no member left apart alone
        summary = identity["summary"]
        assert summary["clusters"][CONFIDENT_COMPONENT] == 2
        assert summary["clusters"][AMBIGUOUS] == 1
        assert summary["cluster_mentions"][AMBIGUOUS] == 0

    def test_same_between_components_joins_them(self):
        docs = two_components()
        made, _ = run(docs, Engine(truths(docs)))
        assert made.counts["asked_pairs"] == 4
        methods = {
            made.identity["mentions"][doc.fingerprint("e1", made.identity)][
                "method"
            ]
            for doc in docs
        }
        assert methods == {ADJUDICATED}
        assert (
            len(
                {
                    made.identity["mentions"][
                        doc.fingerprint("e1", made.identity)
                    ]["cluster"]
                    for doc in docs
                }
            )
            == 1
        )
        assert made.identity["summary"]["clusters"][AMBIGUOUS] == 0

    def test_mixed_verdicts_between_components_fall_back(self):
        """One `same` and three `different` between the two components:
        the part the `same` makes joins `different` pairs, so it falls
        back to the two components; the cluster is left split, counted,
        and needs attention."""
        docs = two_components()
        identity = identity_of(docs)
        pairs = sorted(ambiguous_pairs(entry_of(identity, docs[0])))
        settled = identity_of(
            docs,
            {pair: SAME if pair == pairs[0] else DIFFERENT for pair in pairs},
        )
        assert {mention(settled, doc)["method"] for doc in docs} == {
            CONFIDENT_COMPONENT
        }
        assert len({mention(settled, doc)["cluster"] for doc in docs}) == 2
        entry = entry_of(settled, docs[0])
        assert entry["status"] == ADJUDICATED
        assert entry["contradiction"]["evidence"] == DIFFERENT
        summary = settled["summary"]
        assert summary["clusters"][CONFIDENT_COMPONENT] == 2
        assert summary["clusters"][AMBIGUOUS] == 1
        assert clustering.attention(summary, adjudicated=True)[0].startswith(
            "ambiguous clusters not merged: 1 cluster"
        )

    def test_uncertain_between_components_leaves_it_split_and_flagged(self):
        docs = two_components()
        made, _ = run(docs, Engine(truths(docs), verdict=UNCERTAIN))
        assert made.counts["asked_pairs"] == 4
        summary = made.identity["summary"]
        assert summary["clusters"][CONFIDENT_COMPONENT] == 2
        assert summary["clusters"][AMBIGUOUS] == 1
        assert adjudication.attention(summary)[0].startswith(
            "ambiguous clusters not merged: 1 cluster"
        )


class TestTheLogLines:
    def test_both_stages_say_how_many_the_components_merged(self, tmp_path):
        docs = three_with_one_undated()
        context = context_of(docs, tmp_path / "own", Engine(truths(docs)))
        clustering.run_stage(context)
        (line,) = context.logged
        assert "3 mentions merged, 3 of them by the confident components" in (
            line
        )
        engine = Engine(truths(docs), verdict=DIFFERENT)
        context = context_of(docs, tmp_path / "adjudicated", engine)
        adjudication.run_stage(context)
        (line,) = [line for line in context.logged if "identity map" in line]
        assert "3 mentions merged, 3 of them by the confident components" in (
            line
        )
