"""Adjudication of ambiguous cluster pairs (#106): which pairs are asked,
what each verdict does in a development-stage scratch run and in keeper
mode, the answer's schema, the stage's reuse of its outputs and of the
LLM cache, and what the prompt holds.

Every document is one of the fictional universe's people
(tests/fixtures/example_universe.py): the two Ursula Löbigs the universe
keeps apart (Sebastian's daughter and the widow of the 1912 directory),
and Gustav Löbig, mentioned in two registers, once with his birth date
and once without. The engine is a fake that answers each pair by who the
universe says its two mentions are. No wiki, no model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_clustering import (
    Doc,
    day,
    directory,
    document,
    entity,
    export,
    family_tree,
    gedcom,
    human,
    item,
    register,
    role,
    union_of,
    vocab,
)
from yizkr.pipeline import adjudication, clustering
from yizkr.pipeline.adjudication import (
    NAME,
    RECORD,
    ROLE,
    adjudicate,
    date_text,
    ensure_adjudicated,
    pack,
    parse_answers,
)
from yizkr.pipeline.clustering import (
    ADJUDICATED,
    AMBIGUOUS,
    CONFLICTING,
    DIFFERENT,
    IDENTITY_MAP,
    SAME,
    UNCERTAIN,
    adjudication_calls,
    cluster_corpus,
)
from yizkr.pipeline.llm import (
    OutputLimitReached,
    cached_client,
    observed_client,
    open_cache,
    read_cache,
)

MODEL = "claude-test-model-0"

# a pair of a prompt: its number and comparison, and its two mentions'
# fingerprints
PAIR = re.compile(r"^(\d+)\. clustering's comparison: .*$", re.M)
MENTION = re.compile(r"Mention ([AB]): fingerprint ([0-9a-f]{64})")


def gustav(name: str, *, dated: bool) -> Doc:
    """A register naming Gustav Löbig, with his birth date or without."""
    claims = [("e1", "date_of_birth", day(1919, 1, 7))] if dated else []
    return register(
        name, [human("e1", "Gustav Löbig")], claims, {"e1": "gustav-loebig"}
    )


def truths(docs: list[Doc]) -> dict[str, str]:
    """Who the universe says each mention is, by its fingerprint."""
    identity = cluster_corpus([doc.manifest for doc in docs])
    by_document = {doc.sha1: doc for doc in docs}
    return {
        fingerprint: by_document[entry["document"]].truth[entry["local_id"]]
        for fingerprint, entry in identity["mentions"].items()
    }


@dataclass
class Engine:
    """A fake engine: answers each pair of an adjudication prompt `same`
    when the universe says its mentions are one person and `different`
    otherwise, unless `verdict` overrides it; `lines` replaces the
    answer's lines, and `sentinel` False leaves its sentinel out; `cut`
    reports its answers stopped at the output limit, and `give_up` gives
    up on them there, as the claude CLI does (OutputLimitReached). It
    records every prompt."""

    who: dict[str, str]
    verdict: str | None = None
    lines: list[str] | None = None
    sentinel: bool = True
    cut: bool = False
    give_up: bool = False
    prompts: list[str] = field(default_factory=list)
    model = MODEL
    model_identifier = MODEL
    effort = "high"
    config_hash = "0" * 64
    cli_version = None

    def last_report(self) -> dict:
        return {"stop_reason": "max_tokens"} if self.cut else {}

    def complete(self, prompt: str, image_paths=()) -> str:
        self.prompts.append(prompt)
        if self.give_up:
            raise OutputLimitReached
        lines = self.lines
        if lines is None:
            lines = []
            starts = [m.start() for m in PAIR.finditer(prompt)] + [None]
            for number, (start, end) in enumerate(
                zip(starts, starts[1:], strict=False), start=1
            ):
                (a, b) = (
                    fp for _letter, fp in MENTION.findall(prompt[start:end])
                )
                verdict = self.verdict or (
                    SAME if self.who[a] == self.who[b] else DIFFERENT
                )
                lines.append(
                    json.dumps(
                        {
                            "pair": number,
                            "verdict": verdict,
                            "reason": "fictional",
                        }
                    )
                )
        if self.sentinel:
            lines = [*lines, '{"done": true}']
        return "\n".join(lines)


def run(docs: list[Doc], engine: Engine, *, merge: bool = True):
    calls = []

    def client():
        calls.append(1)
        return engine

    made = adjudicate([doc.manifest for doc in docs], client, merge=merge)
    return made, calls


def method_of(made, docs: list[Doc], key: str, local_id: str) -> str:
    (doc,) = [doc for doc in docs if doc.key == key or doc.sha1 == key]
    return made.identity["mentions"][doc.fingerprint(local_id, made.identity)][
        "method"
    ]


def cluster_of(made, doc: Doc, local_id: str) -> str:
    return made.identity["mentions"][doc.fingerprint(local_id, made.identity)][
        "cluster"
    ]


# -- verdicts -----------------------------------------------------------------


class TestVerdicts:
    def test_same_merges_in_a_scratch_run(self):
        """Gustav, mentioned twice, once dated: an ambiguous pair, whose
        `same` merges the two, with the method `adjudicated`, counted
        apart from the confident merges."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        before = cluster_corpus([doc.manifest for doc in docs])
        assert before["summary"]["clusters"][AMBIGUOUS] == 1
        made, calls = run(docs, Engine(truths(docs)))
        assert calls == [1]
        assert cluster_of(made, docs[0], "e1") == cluster_of(
            made, docs[1], "e1"
        )
        summary = made.identity["summary"]
        assert summary["clusters"][ADJUDICATED] == 1
        assert summary["cluster_mentions"][ADJUDICATED] == 2
        assert summary["clusters"][AMBIGUOUS] == 0
        assert summary["clusters"]["confident"] == 0
        assert summary["merged_mentions"] == 2
        assert {e["method"] for e in made.identity["mentions"].values()} == {
            ADJUDICATED
        }
        assert summary["adjudication"]["verdicts"] == {
            SAME: 1,
            DIFFERENT: 0,
            UNCERTAIN: 0,
        }
        # the map's decisions hash as any map's, and are read back
        assert made.identity["hash"] == clustering.identity_hash(
            made.identity["mentions"]
        )

    def test_same_does_not_merge_in_keeper_mode(self):
        """In keeper mode the verdict is recorded and nothing merges: the
        map is clustering's own."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        made, _ = run(docs, Engine(truths(docs)), merge=False)
        assert cluster_of(made, docs[0], "e1") != cluster_of(
            made, docs[1], "e1"
        )
        assert (
            made.identity["hash"]
            == cluster_corpus([doc.manifest for doc in docs])["hash"]
        )
        assert [verdict for verdict, _ in made.verdicts.values()] == [SAME]
        assert made.identity["summary"]["clusters"][ADJUDICATED] == 0
        assert made.identity["summary"]["adjudication"]["merge"] is False

    def test_different_keeps_the_namesakes_apart(self):
        """The tree's Ursula and the directory's widow, namesakes the
        universe keeps apart: `different` settles their cluster apart."""
        docs = [family_tree(), directory()]
        made, _ = run(docs, Engine(truths(docs)))
        daughter, widow = (docs[0], "e1"), (docs[1], "e4")
        assert cluster_of(made, *daughter) != cluster_of(made, *widow)
        assert method_of(made, docs, docs[0].key, "e1") == ADJUDICATED
        assert method_of(made, docs, docs[1].key, "e4") == ADJUDICATED
        assert (
            made.identity["summary"]["adjudication"]["verdicts"][DIFFERENT]
            == 1
        )
        # what adjudication merges is one person each
        who = truths(docs)
        for entry in made.identity["clusters"].values():
            assert len({who[member] for member in entry["members"]}) == 1

    def test_uncertain_leaves_the_cluster_apart_and_ambiguous(self):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        made, _ = run(docs, Engine(truths(docs), verdict=UNCERTAIN))
        assert cluster_of(made, docs[0], "e1") != cluster_of(
            made, docs[1], "e1"
        )
        assert method_of(made, docs, docs[0].sha1, "e1") == AMBIGUOUS
        assert made.identity["summary"]["clusters"][AMBIGUOUS] == 1
        # asked once, never again within the invocation
        assert made.counts["asked_pairs"] == 1
        assert made.counts["calls"] == 1

    def test_different_cuts_a_cluster_whose_confident_part_merges(self):
        """Three registers give Gustav's birth date and link confidently;
        a fourth gives none and links ambiguously with each. When every
        ambiguous pair is `different`, the cluster is settled: the three
        merge, on their confident links alone, so with the method
        `confident`, and the fourth stays apart, `adjudicated`."""
        docs = [gustav(n, dated=True) for n in ("one", "two", "three")]
        docs.append(gustav("four", dated=False))
        made, _ = run(docs, Engine(truths(docs), verdict=DIFFERENT))
        dated = {cluster_of(made, doc, "e1") for doc in docs[:3]}
        assert len(dated) == 1
        assert cluster_of(made, docs[3], "e1") not in dated
        assert {method_of(made, docs, d.sha1, "e1") for d in docs[:3]} == {
            "confident"
        }
        assert method_of(made, docs, docs[3].sha1, "e1") == ADJUDICATED
        summary = made.identity["summary"]
        assert summary["clusters"][ADJUDICATED] == 0
        assert summary["clusters"]["confident"] == 1
        assert summary["cluster_mentions"]["confident"] == 3
        assert summary["adjudicated_candidate_clusters"] == 1
        assert summary["adjudicated_apart"] == 1
        assert summary["clusters"][AMBIGUOUS] == 0

    def test_a_part_joined_by_same_and_confident_links_is_adjudicated(self):
        """Two dated registers link confidently, and `same` joins the
        undated one to one of them: the part merges, `adjudicated`."""
        docs = [gustav("one", dated=True), gustav("two", dated=True)]
        docs.append(gustav("three", dated=False))
        made, _ = run(docs, Engine(truths(docs)))
        assert {method_of(made, docs, d.sha1, "e1") for d in docs} == {
            ADJUDICATED
        }
        assert made.identity["summary"]["clusters"][ADJUDICATED] == 1

    def test_a_different_pair_the_closure_joins_keeps_its_part_apart(self):
        """Three undated registers of Gustav: `same` joins the first to
        the second and the second to the third, but the first and the
        third are `different`. The inconsistent verdicts merge nothing:
        each stays apart, counted among the ambiguous."""
        docs = [gustav(n, dated=False) for n in ("one", "two", "three")]
        identity = cluster_corpus([d.manifest for d in docs])
        names = {
            doc.fingerprint("e1", identity): n
            for doc, n in zip(docs, ("one", "two", "three"), strict=True)
        }
        settled = settle_by(
            docs,
            names,
            lambda pair: DIFFERENT if pair == {"one", "three"} else SAME,
        )
        assert {e["method"] for e in settled["mentions"].values()} == {
            AMBIGUOUS
        }
        assert len({e["cluster"] for e in settled["mentions"].values()}) == 3
        (entry,) = settled["review"]
        assert entry["contradiction"]["evidence"] == DIFFERENT
        assert settled["summary"]["clusters"][AMBIGUOUS] == 1
        assert settled["summary"]["cluster_mentions"][AMBIGUOUS] == 3

    def test_a_different_pair_beside_a_confident_link_is_kept_apart(self):
        """Two dated registers link confidently; `same` joins the second to
        an undated one, `different` the first to it: the confident link
        would join the two the adjudicator kept apart, so the part merges
        nothing."""
        docs = [gustav("one", dated=True), gustav("two", dated=True)]
        docs.append(gustav("four", dated=False))
        identity = cluster_corpus([d.manifest for d in docs])
        names = {
            doc.fingerprint("e1", identity): n
            for doc, n in zip(docs, ("one", "two", "four"), strict=True)
        }
        settled = settle_by(
            docs,
            names,
            lambda pair: DIFFERENT if pair == {"one", "four"} else SAME,
        )
        clusters = {
            names[m]: e["cluster"] for m, e in settled["mentions"].items()
        }
        assert clusters["one"] != clusters["four"]
        assert {e["method"] for e in settled["mentions"].values()} == {
            AMBIGUOUS
        }

    def test_same_never_excuses_stated_sexes_that_differ(self):
        """Klara Löbig, born 1917, in two registers, one misreading her
        sex, and a third that states none and links confidently to each:
        the first two's sexes block their pair, which is asked, since it
        sits in a larger cluster, and `same` on it merges none of the
        three.
        Only a date conflict on its own pair is a verdict's to excuse."""
        docs = [
            klara("read", "female"),
            klara("misread", "male"),
            klara("silent", None),
        ]
        before = cluster_corpus([d.manifest for d in docs])
        (entry,) = before["review"]
        blocked = [p for p in entry["pairs"] if p["evidence"].endswith("/sex")]
        assert [p["same_merges"] for p in blocked] == [False]
        made, _ = run(docs, Engine(truths(docs), verdict=SAME))
        assert made.counts["asked_pairs"] == 1
        assert {e["method"] for e in made.identity["mentions"].values()} == {
            AMBIGUOUS
        }
        (entry,) = made.identity["review"]
        assert entry["status"] == ADJUDICATED
        assert entry["contradiction"]["evidence"].endswith("/sex")
        assert made.identity["summary"]["clusters"][AMBIGUOUS] == 1
        assert made.identity["summary"]["clusters"][ADJUDICATED] == 0

    def test_a_pair_no_verdict_could_merge_is_not_asked_while_merging(self):
        """The two registers alone: a cluster of one pair whose sexes block
        it, so no verdict could merge it. A run whose verdicts merge does
        not ask it, and counts it; keeper mode asks it, for the review
        sheet; and a `same` given anyway merges nothing."""
        docs = [klara("read", "female"), klara("misread", "male")]
        engine = Engine(truths(docs), verdict=SAME)
        made, clients = run(docs, engine)
        assert engine.prompts == [] and clients == []
        assert made.counts["unmergeable_pairs_not_asked"] == 1
        assert made.counts["planned_calls"] == 0
        assert {e["method"] for e in made.identity["mentions"].values()} == {
            AMBIGUOUS
        }
        kept, _ = run(docs, engine, merge=False)
        assert kept.counts["asked_pairs"] == 1
        assert kept.counts["unmergeable_pairs_not_asked"] == 0
        manifests = [d.manifest for d in docs]
        (entry,) = cluster_corpus(manifests)["review"]
        forced = cluster_corpus(
            manifests,
            None,
            {clustering._pair(*entry["pairs"][0]["mentions"]): SAME},
        )
        assert {e["method"] for e in forced["mentions"].values()} == {
            AMBIGUOUS
        }

    def test_a_chain_of_same_never_joins_dates_that_conflict(self):
        """Gustav born 1919, a register giving his given names Gustav
        Adolf Emil with no date, and a Gustav Emil born 1819: with a key
        limit that leaves the two dated ones unblocked, `same` joins each
        to the undated one, but the two never paired and their dates
        conflict, so none of the three merges."""

        def gustav_of(name: str, givens: list[str], year: int | None) -> Doc:
            claims = [("e1", "date_of_birth", day(year, 1, 7))] if year else []
            return register(
                name,
                [
                    human(
                        "e1",
                        "Gustav Löbig",
                        givens=givens,
                        surnames=["Löbig"],
                    )
                ],
                claims,
                {"e1": "gustav-loebig"},
            )

        docs = [
            gustav_of("adolf", ["Gustav", "Adolf"], 1919),
            gustav_of("both", ["Gustav", "Adolf", "Emil"], None),
            gustav_of("emil", ["Gustav", "Emil"], 1819),
        ]
        config = clustering.load_config(
            overrides={"blocking": {"max-key-mentions": 2}}
        )
        manifests = [d.manifest for d in docs]
        identity = cluster_corpus(manifests, config)
        (entry,) = [e for e in identity["review"] if e["status"] == AMBIGUOUS]
        assert len(entry["pairs"]) == 2
        verdicts = {
            clustering._pair(*p["mentions"]): SAME
            for p in entry["pairs"]
            if p["link"] == AMBIGUOUS
        }
        assert len(verdicts) == 2
        settled = cluster_corpus(manifests, config, verdicts)
        first = docs[0].fingerprint("e1", settled)
        last = docs[2].fingerprint("e1", settled)
        mentions = settled["mentions"]
        assert mentions[first]["cluster"] != mentions[last]["cluster"]
        assert {e["method"] for e in mentions.values()} == {AMBIGUOUS}
        (entry,) = [e for e in settled["review"] if e["status"] == ADJUDICATED]
        assert entry["contradiction"]["evidence"] == "key/conflict/unknown"

    def test_one_uncertain_pair_keeps_the_whole_cluster_apart(self):
        docs = [gustav(n, dated=True) for n in ("one", "two", "three")]
        docs.append(gustav("four", dated=False))
        identity = cluster_corpus([doc.manifest for doc in docs])
        (entry,) = identity["review"]
        ambiguous = [p for p in entry["pairs"] if p["link"] == AMBIGUOUS]
        verdicts = {
            clustering._pair(*p["mentions"]): DIFFERENT for p in ambiguous
        }
        verdicts[clustering._pair(*ambiguous[0]["mentions"])] = UNCERTAIN
        settled = cluster_corpus(
            [doc.manifest for doc in docs], None, verdicts
        )
        assert {e["method"] for e in settled["mentions"].values()} == {
            AMBIGUOUS
        }
        assert settled["hash"] == identity["hash"]

    def test_a_conflicting_cluster_is_never_asked_and_never_merges(self):
        """The export holds both Ursulas, and each document's Ursula is
        ambiguous with both: a conflicting cluster. None of its pairs is
        asked, and an engine that says `same` to anything merges none of
        it, nor would its verdicts if given."""
        docs = [gedcom(), family_tree(), directory()]
        before = cluster_corpus([doc.manifest for doc in docs])
        (conflicting,) = [
            e for e in before["review"] if e["status"] == CONFLICTING
        ]
        engine = Engine(truths(docs), verdict=SAME)
        made, _ = run(docs, engine)
        asked = "\n".join(engine.prompts)
        for member in conflicting["members"]:
            assert member not in asked
            assert made.identity["mentions"][member]["method"] == CONFLICTING
        forced = {
            clustering._pair(*p["mentions"]): SAME
            for p in conflicting["pairs"]
        }
        again = cluster_corpus([doc.manifest for doc in docs], None, forced)
        for member in conflicting["members"]:
            assert again["mentions"][member]["method"] == CONFLICTING


def born(name: str, *, dated: bool) -> Doc:
    """A register of Gustav's birth: the event, with Gustav its
    principal, dated or not; its date is his own."""
    claims = [("b1", "participant", item("e1"), role("principal"))]
    if dated:
        claims.append(("b1", "point_in_time", day(1919, 1, 7)))
    return register(
        name,
        [human("e1", "Gustav Löbig"), entity("b1", "Birth", "birth")],
        claims,
        {"e1": "gustav-loebig", "b1": "gustav-loebig-birth"},
    )


def married(name: str, born: int, wedding: int | None = None) -> Doc:
    """A register of Leopold Löbig and Agnes Nertler's marriage, giving
    Leopold's birth year as it reads it, and the wedding's year when
    given."""
    entities = [
        human("e1", "Leopold Löbig"),
        human("e2", "Agnes Nertler", givens=["Agnes"], birth=["Nertler"]),
        entity("m1", "Leopold and Agnes", "marriage"),
    ]
    claims = [
        ("e1", "date_of_birth", day(born)),
        *union_of("m1", "e1", "e2"),
    ]
    truth = {
        "e1": "leopold-loebig",
        "e2": "agnes-loebig",
        "m1": "leopold-and-agnes",
    }
    if wedding:
        entities.append(entity("w1", "Wedding", "wedding"))
        claims += [
            ("m1", "started_by", item("w1")),
            ("w1", "point_in_time", day(wedding)),
        ]
        truth["w1"] = "leopold-and-agnes-wedding"
    return register(name, entities, claims, truth)


def exported_couple(wedding: int | None = None) -> Doc:
    """A GEDCOM export of Leopold and Agnes as a family, their names
    written without surname slashes, so they key only weakly and never
    link confidently; with the wedding's year when given."""
    family = "0 @F1@ FAM\n1 HUSB @I1@\n1 WIFE @I2@\n"
    if wedding:
        family += f"1 MARR\n2 DATE {wedding}\n"
    return export(
        "0 @I1@ INDI\n1 NAME Leopold Löbig\n"
        "0 @I2@ INDI\n1 NAME Agnes Nertler\n" + family,
        {
            "i1": "leopold-loebig",
            "i2": "agnes-loebig",
            "f1": "leopold-and-agnes",
        },
    )


UNION_KINDS = ("a marriage in", "a partnership in")


class TestRounds:
    def test_unions_are_settled_after_their_couples(self):
        """The export's couple and a register's are ambiguous pairs, and so
        is their union. Once `same` merges the couples, the unions link
        confidently, and they are never asked."""
        docs = [exported_couple(), married("register", 1912)]
        before = cluster_corpus([doc.manifest for doc in docs])
        assert {e["kind"] for e in before["review"]} == {
            "human",
            "partnership",
        }
        engine = Engine(truths(docs))
        made, _ = run(docs, engine)
        (prompt,) = engine.prompts
        assert not any(kind in prompt for kind in UNION_KINDS)
        assert made.counts["rounds"] == 1
        unions = [
            entry
            for entry in made.identity["mentions"].values()
            if entry["kind"] in ("marriage", "partnership")
        ]
        assert [entry["method"] for entry in unions] == ["confident"] * 2
        assert len({entry["cluster"] for entry in unions}) == 1

    def test_a_union_still_ambiguous_is_asked_in_a_later_round(self):
        """The same couples, but their weddings' years conflict: the union
        stays ambiguous once its couple merge, so a later round asks it,
        through the one client the stage made."""
        docs = [exported_couple(1947), married("register", 1912, 1974)]
        engine = Engine(truths(docs))
        made, clients = run(docs, engine)
        assert clients == [1]
        # people, then the union, then its wedding, whose union merged
        # but whose year still conflicts
        people, union, wedding = engine.prompts
        assert made.counts["rounds"] == 3
        assert not any(kind in people for kind in UNION_KINDS)
        assert any(kind in union for kind in UNION_KINDS)
        assert "a wedding in" not in union
        assert "a wedding in" in wedding

    def test_people_are_settled_before_their_events(self):
        """Two registers of Gustav's birth, one dated: his two mentions
        are an ambiguous pair, and so are the births, whose principals
        did not merge. The people are asked first; once `same` merges
        them, each birth is its merged principal's own and links
        confidently, so it is never asked."""
        docs = [born("one", dated=True), born("two", dated=False)]
        before = cluster_corpus([doc.manifest for doc in docs])
        assert before["summary"]["ambiguous_pairs"] == 2
        # one call for the people, one for the births, each group apart
        assert (
            before["summary"]["adjudication"]["calls_ambiguous_clusters"] == 2
        )
        engine = Engine(truths(docs))
        made, _ = run(docs, engine)
        (prompt,) = engine.prompts
        assert "a human in" in prompt and "a birth in" not in prompt
        assert made.counts["rounds"] == 1
        assert {e["method"] for e in made.identity["mentions"].values()} == {
            ADJUDICATED,
            "confident",
        }
        assert made.identity["summary"]["clusters"] == {
            "confident": 1,
            ADJUDICATED: 1,
            AMBIGUOUS: 0,
            CONFLICTING: 0,
        }

    def test_keeper_mode_asks_every_pending_pair_at_once(self):
        """In keeper mode one round asks every group's pairs, each group
        packed apart, as the planned count packs them."""
        docs = [born("one", dated=True), born("two", dated=False)]
        engine = Engine(truths(docs))
        made, _ = run(docs, engine, merge=False)
        people, births = engine.prompts
        assert "a human in" in people and "a birth in" not in people
        assert "a birth in" in births and "a human in" not in births
        assert made.counts["asked_pairs"] == 2
        assert made.counts["rounds"] == 1
        assert made.counts["calls"] == made.counts["planned_calls"] == 2


# -- the answer's schema ------------------------------------------------------


class TestSchema:
    @pytest.mark.parametrize("how", ["cut", "give_up"])
    def test_an_answer_cut_off_at_the_output_limit_is_refused(self, how):
        """An answer the engine stopped at its output limit, or one the
        claude CLI gave up on there, is refused and counted, and the pair
        stays apart: nothing escapes the stage."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        made, _ = run(docs, Engine(truths(docs), **{how: True}))
        assert made.verdicts == {}
        assert made.counts["refused_calls"] == 1
        assert "cut off" in made.calls[0]["refused"]
        assert method_of(made, docs, docs[0].sha1, "e1") == AMBIGUOUS

    def test_a_give_up_on_the_second_attempt_is_refused(self, tmp_path):
        """A kept answer refused, and the engine giving up at its output
        limit when asked once more: the call is refused and counted, its
        record keeps no rejected answer, and the pair stays apart."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        probe = Engine(truths(docs))
        run(docs, probe)
        broken = Engine(truths(docs), sentinel=False)
        cached_client(
            broken, open_cache(tmp_path / "cache"), role=ROLE
        ).complete(probe.prompts[0])
        gives_up = Engine(truths(docs), give_up=True)
        made, _ = run(
            docs,
            cached_client(gives_up, open_cache(tmp_path / "cache"), role=ROLE),
        )
        assert made.counts["refused_calls"] == 1
        assert made.counts["second_attempts"] == 1
        assert made.calls[0]["response"] == ""
        assert method_of(made, docs, docs[0].sha1, "e1") == AMBIGUOUS

    def test_a_second_bad_answer_is_rejected_too(self, tmp_path):
        """A kept answer refused, asked once more and answered badly again:
        that answer is rejected too, so the next invocation asks the
        engine again."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        probe = Engine(truths(docs))
        run(docs, probe)
        broken = Engine(truths(docs), sentinel=False)
        cached_client(
            broken, open_cache(tmp_path / "cache"), role=ROLE
        ).complete(probe.prompts[0])
        again = Engine(truths(docs), sentinel=False)
        made, _ = run(
            docs,
            cached_client(again, open_cache(tmp_path / "cache"), role=ROLE),
        )
        assert made.counts["second_attempts"] == 1
        assert made.counts["refused_calls"] == 1
        good = Engine(truths(docs))
        made, _ = run(
            docs,
            cached_client(good, open_cache(tmp_path / "cache"), role=ROLE),
        )
        assert len(good.prompts) == 1
        # the cache held no answer to reject: the engine was asked once
        assert made.counts["second_attempts"] == 0
        assert [v for v, _ in made.verdicts.values()] == [SAME]

    def test_an_answer_outside_the_schema_is_refused_and_counted(self):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(
            truths(docs),
            lines=[
                json.dumps({"pair": 1, "verdict": "probably", "reason": "x"}),
                json.dumps({"pair": 2, "verdict": SAME, "reason": "x"}),
                json.dumps({"pair": 1, "verdict": SAME, "reason": 7}),
                json.dumps({"pair": True, "verdict": SAME, "reason": "x"}),
                json.dumps({"pair": 0, "verdict": SAME, "reason": "x"}),
            ],
        )
        made, _ = run(docs, engine)
        assert made.verdicts == {}
        assert made.counts["rejected_lines"] == 5
        assert made.counts["unanswered_pairs"] == 1
        assert method_of(made, docs, docs[0].sha1, "e1") == AMBIGUOUS
        assert any(
            "outside the schema" in reason
            for reason in adjudication.attention(made.identity["summary"])
        )

    def test_a_pair_answered_two_ways_is_refused(self):
        answers = parse_answers(
            "\n".join(
                [
                    json.dumps({"pair": 1, "verdict": SAME, "reason": "a"}),
                    json.dumps(
                        {"pair": 1, "verdict": DIFFERENT, "reason": "b"}
                    ),
                    '{"done": true}',
                ]
            ),
            1,
        )
        assert answers.verdicts == {} and answers.conflicts == [1]

    def test_an_answer_without_its_sentinel_is_refused_whole(self):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        made, _ = run(docs, Engine(truths(docs), sentinel=False))
        assert made.verdicts == {}
        assert made.counts["refused_calls"] == 1
        assert made.calls[0]["refused"]
        assert method_of(made, docs, docs[0].sha1, "e1") == AMBIGUOUS

    def test_a_refused_answer_the_cache_kept_is_asked_once_more(
        self, tmp_path
    ):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        # an answer an earlier invocation kept, which this code refuses
        probe = Engine(truths(docs))
        run(docs, probe)
        broken = Engine(truths(docs), sentinel=False)
        cache = open_cache(tmp_path / "cache")
        cached_client(broken, cache, role=ROLE).complete(probe.prompts[0])
        good = Engine(truths(docs))
        cache = open_cache(tmp_path / "cache")
        made, _ = run(docs, cached_client(good, cache, role=ROLE))
        assert made.counts["second_attempts"] == 1
        assert [v for v, _ in made.verdicts.values()] == [SAME]


# -- batches ------------------------------------------------------------------


def test_packing_matches_the_estimate_of_the_calls():
    """pack makes the calls clustering.adjudication_calls counts: a
    cluster's pairs together, one larger than a call split first."""
    for sizes in ([3, 2, 5], [41], [40, 1], [10] * 9, [1], []):
        groups = [
            [(f"{n}-{i}a", f"{n}-{i}b") for i in range(size)]
            for n, size in enumerate(sizes)
        ]
        calls = pack(groups, 40)
        assert len(calls) == adjudication_calls(sizes, 40)
        assert all(len(call) <= 40 for call in calls)
        assert [p for call in calls for p in call] == [
            p for group in groups for p in group
        ]
    calls = pack([[("a", "b")] * 3, [("c", "d")] * 2], 4)
    # the second cluster does not fit beside the first, so starts a call
    assert [len(call) for call in calls] == [3, 2]


# -- the prompt ---------------------------------------------------------------


QID = re.compile(r"\bQ[1-9]\d*\b")


class TestPrompt:
    def docs(self) -> list[Doc]:
        """Gustav twice, the undated register also giving a decade, a
        century and a ranged date, and naming an item already in the
        graph by its QID."""
        dated = gustav("one", dated=True)
        undated = document(
            "a register",
            [human("e1", "Gustav Löbig", aliases=["Gustl"])],
            [
                (
                    "e1",
                    "residence",
                    {"type": "item", "value": {"kind": "qid", "qid": "Q31"}},
                    ("point_in_time", day(1930) | {"precision": "decade"}),
                ),
                (
                    "e1",
                    "occupation",
                    {"type": "string", "value": "hatter"},
                    ("point_in_time", day(1901) | {"precision": "century"}),
                    ("earliest_date", day(1938)),
                    ("latest_date", day(1939, 5)),
                ),
            ],
            {"e1": "gustav-loebig"},
            sha1="a" * 40,
        )
        return [dated, undated]

    def test_gives_aliases_events_and_kin_dates(self):
        """Each mention's aliases; an event that names it, with the
        event's own date; a union with the other spouse; and kin with
        their dates."""
        docs = self.docs()
        engine = Engine(truths(docs))
        run(docs, engine)
        (prompt,) = engine.prompts
        assert "aliases: 'Gustl'" in prompt
        births = [born("one", dated=True), born("two", dated=False)]
        engine = Engine(truths(births), verdict=UNCERTAIN)
        run(births, engine)
        assert (
            "named as the participant of 'Birth' (birth) [role: principal]"
            " — point_in_time: 1919-01-07" in engine.prompts[0]
        )
        lines = adjudication._Document(family_tree().manifest).lines("e2")
        assert (
            "named as the participant of 'Marriage of Sebastian and "
            "Hildegard' (marriage) [role: spouse] — participant: "
            "'Hildegard Laudler' (human) [role: spouse]"
        ) in lines
        assert "child: 'Ursula Löbig' (human) — date_of_birth: 1911; " in (
            "\n".join(lines)
        )

    def test_an_event_is_given_up_to_its_claims_limit(self):
        """An event that names a mention is given with at most MAX_CLAIMS
        of its own claims, and says how many more it has."""
        count = adjudication.MAX_CLAIMS + 2
        claims = [("b1", "participant", item("e1"), role("principal"))] + [
            ("b1", "point_in_time", day(1880 + n)) for n in range(count)
        ]
        doc = register(
            "a register",
            [human("e1", "Gustav Löbig"), entity("b1", "Birth", "birth")],
            claims,
            {"e1": "gustav-loebig", "b1": "gustav-loebig-birth"},
        )
        (line,) = [
            line
            for line in adjudication._Document(doc.manifest).lines("e1")
            if line.startswith("named as the participant")
        ]
        assert line.count("point_in_time") == adjudication.MAX_CLAIMS
        assert line.endswith("; and 2 claims more")

    def test_says_how_many_claims_it_leaves_out(self):
        doc = document(
            "a register",
            [human("e1", "Gustav Löbig")],
            [
                ("e1", "residence", {"type": "string", "value": "Flörburg"})
                for _ in range(adjudication.MAX_CLAIMS + 1)
            ],
            {"e1": "gustav-loebig"},
            sha1="d" * 40,
        )
        lines = adjudication._Document(doc.manifest).lines("e1")
        assert lines[-1] == "and 1 claims more"
        assert lines.count("residence: 'Flörburg'") == adjudication.MAX_CLAIMS

    def test_names_no_qid(self):
        docs = self.docs()
        engine = Engine(truths(docs))
        run(docs, engine)
        (prompt,) = engine.prompts
        assert QID.search(prompt) is None
        assert "an item already in the graph" in prompt
        # each mention by its fingerprint, each document by its SHA-1
        assert len(MENTION.findall(prompt)) == 2
        assert docs[0].sha1 in prompt and docs[1].sha1 in prompt

    def test_renders_dates_at_their_precision(self):
        docs = self.docs()
        engine = Engine(truths(docs))
        run(docs, engine)
        (prompt,) = engine.prompts
        assert "point_in_time: the 1930s" in prompt
        assert "point_in_time: the 20th century" in prompt
        assert "earliest_date: 1938; latest_date: 1939-05" in prompt
        assert "date_of_birth: 1919-01-07" in prompt
        assert "1901" not in prompt and "1930]" not in prompt

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ({"year": 1801, "precision": "century"}, "the 19th century"),
            ({"year": 1830, "precision": "decade"}, "the 1830s"),
            ({"year": 1911}, "1911"),
            ({"year": 1952, "month": 3}, "1952-03"),
            ({"year": 1919, "month": 7, "day": 1}, "1919-07-01"),
        ],
    )
    def test_a_date_at_its_precision(self, value, shown):
        from yizkr.claims import TimeValue

        assert date_text(TimeValue.model_validate(value)) == shown

    def test_gives_kin_and_record(self):
        docs = [family_tree(), directory()]
        engine = Engine(truths(docs))
        run(docs, engine)
        (prompt,) = engine.prompts
        assert "named as the child of 'Sebastian Löbig' (human)" in prompt
        assert (
            'record: {"status": "name", "given": [{"written": "Ursula"'
            in prompt
        )
        assert "date_of_birth: 1911" in prompt


# -- the stage: its outputs and the cache ------------------------------------


@dataclass
class Run:
    """A run directory's manifest and events, as the stage reads them."""

    stage: str = "development"
    events: list[tuple[str, dict]] = field(default_factory=list)

    def manifest(self) -> dict:
        return {"project_stage": self.stage}

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class Driver:
    """The driver as the stage reads it: the role's settings, and its
    client, behind an LLM cache in `cache`, counting the clients made."""

    llm = "claude-code"

    def __init__(self, engine: Engine, cache: Path) -> None:
        self.engine = engine
        self.cache = cache
        self.clients = 0
        self.refresh: tuple[str, ...] = ()
        self.epochs: dict[str, int] = {}

    def _engine_settings(self, role):
        assert role == ROLE
        return "high", None

    def _configured_model(self, role):
        return MODEL, "$YIZKR_LLM_MODEL"

    def role_client(self, role, *, events=None):
        assert role == ROLE
        self.clients += 1
        # as the orchestrator makes it (Orchestrator.role_client)
        observed = observed_client(self.engine, role=role, events=events)
        cache = open_cache(
            self.cache, refresh=self.refresh, epochs=self.epochs
        )
        return cached_client(observed, cache, role=role, events=events)


def context_of(docs, tmp_path, engine, *, stage="development"):
    run = Run(stage)
    documents = []
    for doc in docs:
        path = tmp_path / "views" / doc.sha1 / "batch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc.manifest.batch.model_dump_json())
        documents.append(
            SimpleNamespace(sha1=doc.sha1, kind="text", batch=path)
        )
    logged: list[str] = []
    return SimpleNamespace(
        run=run,
        documents=tuple(documents),
        directory=tmp_path / "corpus" / "clustering",
        env_file=None,
        log=lambda line, **kwargs: logged.append(line),
        driver=Driver(engine, tmp_path / "cache"),
        logged=logged,
    )


class TestStage:
    def test_outputs_persist_and_are_reused(self, tmp_path):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(truths(docs))
        context = context_of(docs, tmp_path, engine)
        first = adjudication.run_stage(context)
        assert context.driver.clients == 1
        record = json.loads((context.directory / RECORD).read_text())
        assert [v["verdict"] for v in record["verdicts"]] == [SAME]
        assert record["verdicts"][0]["reason"] == "fictional"
        assert record["calls"][0]["response"].endswith('{"done": true}')
        assert record["calls"][0]["round"] == 1
        identity = json.loads((context.directory / IDENTITY_MAP).read_text())
        assert identity["summary"]["clusters"][ADJUDICATED] == 1
        # its calls are named apart in the run's events
        roles = {f.get("role") for _e, f in context.run.events}
        assert roles == {NAME}
        assert {f.get("stage") for _e, f in context.run.events} <= {
            None,
            "clustering",
        }
        # a second invocation reuses both, with no client and no call
        second = adjudication.run_stage(context)
        assert context.driver.clients == 1
        assert len(engine.prompts) == 1
        assert "identity map reused" in context.logged[-1]
        assert first == second

    def test_the_cache_answers_a_stage_made_again(self, tmp_path):
        """With the stage's outputs gone, it is made again, and the LLM
        cache answers its calls: the engine is not asked again, and the
        same map results."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(truths(docs))
        context = context_of(docs, tmp_path, engine)
        adjudication.run_stage(context)
        made = (context.directory / IDENTITY_MAP).read_text()
        (context.directory / RECORD).unlink()
        output = ensure_adjudicated(context)
        assert not output.reused
        assert len(engine.prompts) == 1
        assert (context.directory / IDENTITY_MAP).read_text() == made
        events = [e for e, _f in context.run.events]
        assert events.count("cache-hit") == 1
        assert read_cache(tmp_path / "cache") is not None

    def test_a_refused_call_is_asked_again_next_invocation(self, tmp_path):
        """A call refused (an answer without its sentinel) leaves outputs
        the next invocation does not reuse: it asks that call again, and
        the cache answers the rest."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(truths(docs), sentinel=False)
        context = context_of(docs, tmp_path, engine)
        adjudication.run_stage(context)
        record = json.loads((context.directory / RECORD).read_text())
        assert record["counts"]["refused_calls"] == 1
        engine.sentinel = True
        output = ensure_adjudicated(context)
        assert not output.reused
        assert len(engine.prompts) == 2
        assert engine.prompts[0] == engine.prompts[1]
        assert output.identity_map["summary"]["clusters"][ADJUDICATED] == 1
        assert ensure_adjudicated(context).reused

    def test_another_mode_is_made_again(self, tmp_path):
        """Outputs made in keeper mode are not reused by a run of the
        development stage, which merges on the verdicts."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(truths(docs))
        context = context_of(docs, tmp_path, engine, stage="kept")
        adjudication.run_stage(context)
        context.run.stage = "development"
        output = ensure_adjudicated(context)
        assert not output.reused
        assert output.identity_map["summary"]["clusters"][ADJUDICATED] == 1

    def test_another_prompt_or_batch_size_is_made_again(
        self, tmp_path, monkeypatch
    ):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        context = context_of(docs, tmp_path, Engine(truths(docs)))
        adjudication.run_stage(context)
        assert ensure_adjudicated(context).reused
        config = clustering.load_config(
            overrides={"adjudication": {"pairs-per-call": 7}}
        )
        assert not ensure_adjudicated(context, config).reused
        assert ensure_adjudicated(context, config).reused
        monkeypatch.setattr(adjudication, "PROMPT", adjudication.PROMPT + "\n")
        assert not ensure_adjudicated(context, config).reused

    def test_other_code_is_made_again(self, tmp_path, monkeypatch):
        """The stage's outputs are keyed on its own source and the site's
        date formatter (code_digest): other code makes them again."""
        from yizkr import staticsite

        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        context = context_of(docs, tmp_path, Engine(truths(docs)))
        adjudication.run_stage(context)
        digest = adjudication.code_digest()

        def format_time(value, precision, where="a date"):
            return value

        monkeypatch.setattr(staticsite, "format_time", format_time)
        assert adjudication.code_digest() != digest
        monkeypatch.undo()
        # the module's own source: a copy with one line more
        copy = tmp_path / "adjudication.py"
        copy.write_text(
            Path(adjudication.__file__).read_text() + "# one line more\n"
        )
        monkeypatch.setattr(adjudication, "__file__", str(copy))
        assert adjudication.code_digest() != digest
        monkeypatch.undo()
        monkeypatch.setattr(adjudication, "code_digest", lambda: "other")
        assert not ensure_adjudicated(context).reused

    def test_a_map_another_stage_wrote_over_is_made_again(self, tmp_path):
        """When the map in the directory is not the one the record made
        (clustering alone wrote over it), the stage makes its own again."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        context = context_of(docs, tmp_path, Engine(truths(docs)))
        adjudication.run_stage(context)
        clustering.ensure_identity_map(context.directory, context.documents)
        output = ensure_adjudicated(context)
        assert not output.reused
        assert output.identity_map["summary"]["clusters"][ADJUDICATED] == 1

    def test_a_pause_resumes_with_the_calls_answered(self, tmp_path):
        """A subscription limit met on the second call stops the stage,
        leaving nothing written; the next invocation is answered from the
        cache up to there, and asks only the call the pause stopped."""
        from yizkr.pipeline.llm import PassPaused

        docs = [
            gustav("one", dated=True),
            gustav("two", dated=False),
            *three_nicholases(),
        ]
        engine = Engine(truths(docs))
        context = context_of(docs, tmp_path, engine)
        config = clustering.load_config(
            overrides={"adjudication": {"pairs-per-call": 1}}
        )
        answer = engine.complete
        asked: list[str] = []

        def pausing(prompt, image_paths=()):
            asked.append(prompt)
            if len(asked) == 2:
                raise PassPaused("the five-hour window is used up")
            return answer(prompt, image_paths)

        engine.complete = pausing
        with pytest.raises(PassPaused):
            ensure_adjudicated(context, config)
        assert not (context.directory / RECORD).exists()
        made = len(engine.prompts)
        output = ensure_adjudicated(context, config)
        assert not output.reused
        # the first call came from the cache; the stopped one and the
        # rest reached the engine
        assert asked[2] == asked[1]
        assert asked[0] not in engine.prompts[made:]
        # every call of the second invocation but the cached first
        assert output.record["counts"]["calls"] == len(asked[2:]) + 1

    def test_a_refresh_of_the_role_asks_again_once(self, tmp_path):
        """`--refresh critique` makes the stage's outputs again, asking
        the engine afresh; an invocation that refreshes it again reuses
        them. Another epoch of the role makes them again too."""
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        engine = Engine(truths(docs))
        context = context_of(docs, tmp_path, engine)
        adjudication.run_stage(context)
        context.driver.refresh = (ROLE,)
        adjudication.run_stage(context)
        assert len(engine.prompts) == 2
        adjudication.run_stage(context)
        assert len(engine.prompts) == 2
        context.driver.refresh = ()
        context.driver.epochs = {ROLE: 1}
        adjudication.run_stage(context)
        assert len(engine.prompts) == 3

    def test_keeper_mode_records_verdicts_and_merges_nothing(self, tmp_path):
        docs = [gustav("one", dated=True), gustav("two", dated=False)]
        context = context_of(
            docs, tmp_path, Engine(truths(docs)), stage="kept"
        )
        adjudication.run_stage(context)
        record = json.loads((context.directory / RECORD).read_text())
        assert [v["verdict"] for v in record["verdicts"]] == [SAME]
        identity = json.loads((context.directory / IDENTITY_MAP).read_text())
        assert identity["summary"]["clusters"][ADJUDICATED] == 0
        assert identity["summary"]["clusters"][AMBIGUOUS] == 1

    def test_a_corpus_with_nothing_ambiguous_starts_no_client(self, tmp_path):
        docs = [gustav("one", dated=True), gustav("two", dated=True)]
        context = context_of(docs, tmp_path, Engine(truths(docs)))
        adjudication.run_stage(context)
        assert context.driver.clients == 0
        identity = json.loads((context.directory / IDENTITY_MAP).read_text())
        assert identity["summary"]["clusters"]["confident"] == 1


# -- the report and the diff --------------------------------------------------


def test_the_report_counts_the_calls_under_adjudication(tmp_path):
    """The stage's calls are named apart in the run's events, and the run
    report counts them under their own role, apart from the critique's."""
    from tests.unit.test_scratch_write import _vocabulary
    from yizkr.pipeline import report

    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    context = context_of(docs, tmp_path, Engine(truths(docs)))
    adjudication.run_stage(context)
    fold = report._Fold()
    for event, fields in context.run.events:
        fold.fold({"event": event, **fields})
    calls = report._aggregate_calls(fold, _vocabulary())
    assert set(calls["by_role"]) == {NAME}
    assert calls["engine_calls"]["by_role"] == {NAME: 1}


def test_the_review_records_each_verdict_and_the_key_its_values():
    """Each ambiguous pair of a map made under verdicts records its
    verdict, and the verdicts' values, not only their pairs, are part of
    the map's key."""
    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    manifests = [doc.manifest for doc in docs]
    (entry,) = cluster_corpus(manifests)["review"]
    pair = clustering._pair(*entry["pairs"][0]["mentions"])
    same = cluster_corpus(manifests, None, {pair: SAME})
    (settled,) = same["review"]
    assert settled["pairs"][0]["verdict"] == SAME
    uncertain = cluster_corpus(manifests, None, {pair: UNCERTAIN})
    assert uncertain["review"][0]["pairs"][0]["verdict"] == UNCERTAIN
    config = clustering.load_config()
    assert clustering.inputs_key(
        manifests, config, {pair: SAME}
    ) != clustering.inputs_key(manifests, config, {pair: DIFFERENT})


def test_the_diff_lists_each_adjudicated_merge_apart():
    """A merge the verdicts make is counted by its method in the diff's
    aggregates, and listed, with its members, in its detail."""
    from yizkr.pipeline import rundiff

    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    baseline = cluster_corpus([doc.manifest for doc in docs])
    made, _ = run(docs, Engine(truths(docs)))
    names = rundiff._Names.of({}, {})
    aggregate, detail = rundiff.compare_clusters(
        baseline, made.identity, names
    )
    assert aggregate["merges"] == 1
    assert aggregate["merges_by_method"] == {ADJUDICATED: 1}
    assert aggregate["methods_changed"] == {"ambiguous → adjudicated": 2}
    (merge,) = detail["merges"]
    assert merge["method"] == ADJUDICATED
    from tests.unit.test_run_diff import an_aggregate

    summary = rundiff.render_markdown(an_aggregate(clusters=aggregate))
    assert "by method: adjudicated 1" in summary
    assert {m["label"] for m in merge["members"]} == {"Gustav Löbig"}
    assert len(merge["members"]) == 2


def test_the_stage_s_attention_labels_are_the_report_s():
    """Every reason the stage gives is one the run report names, and
    none is the estimate clustering gives before adjudication."""
    from yizkr.pipeline.report import ATTENTION_LABELS

    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    refused, _ = run(docs, Engine(truths(docs), sentinel=False))
    conflicting = Engine(
        truths(docs),
        lines=[
            json.dumps({"pair": 1, "verdict": SAME, "reason": "a"}),
            json.dumps({"pair": 1, "verdict": UNCERTAIN, "reason": "b"}),
            json.dumps({"pair": 9, "verdict": SAME, "reason": "c"}),
        ],
    )
    answered, _ = run(docs, conflicting)
    reasons = adjudication.attention(
        refused.identity["summary"]
    ) + adjudication.attention(answered.identity["summary"])
    labels = {reason.split(":")[0] for reason in reasons}
    assert labels == {
        "ambiguous clusters not merged",
        "adjudicated pairs without a verdict",
        "adjudication answers refused",
        "adjudication answers in conflict",
    }
    assert labels <= ATTENTION_LABELS


def test_the_monitor_counts_the_calls_under_adjudication(tmp_path):
    """The live monitor counts the stage's calls by their name, as it
    counts the place stage's."""
    from yizkr.pipeline.monitor import summarise_run
    from yizkr.pipeline.rundir import RunDir

    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    context = context_of(docs, tmp_path, Engine(truths(docs)))
    rundir = RunDir(tmp_path / "run")
    rundir.path.mkdir()
    (rundir.path / "run.json").write_text("{}")
    context.run.emit = rundir.emit
    adjudication.run_stage(context)
    calls = summarise_run(rundir.path).calls
    assert calls.roles[NAME]["engine"] == 1
    assert set(calls.roles) == {NAME}


# -- what verdicts settle -----------------------------------------------------


def klara(name: str, sex: str | None) -> Doc:
    """A register of Klara Löbig, born 1917, with the sex it states."""
    claims = [("e1", "date_of_birth", day(1917))]
    if sex:
        claims.append(("e1", "sex_or_gender", vocab(sex)))
    return register(
        name, [human("e1", "Klara Löbig")], claims, {"e1": "klara-loebig"}
    )


def three_nicholases() -> list[Doc]:
    """Nicholas Lobig's birth, 1950, as one register gives it and as a
    misreading gives it, 1850 (the fixture corpus's misread year), and a
    register with no date: the undated one pairs ambiguously with each,
    and the two dated ones, whose dates cannot overlap, pair ambiguously
    too."""

    def nicholas(name: str, year: int | None) -> Doc:
        claims = [("e1", "date_of_birth", day(year))] if year else []
        return register(
            name,
            [human("e1", "Nicholas Lobig")],
            claims,
            {"e1": "nicholas-lobig"},
        )

    return [
        nicholas("read", 1950),
        nicholas("undated", None),
        nicholas("misread", 1850),
    ]


def settle(docs: list[Doc], verdict_of) -> dict:
    """The map under verdicts `verdict_of` gives each ambiguous pair of
    three_nicholases, by the two documents' names."""
    identity = cluster_corpus([doc.manifest for doc in docs])
    names = {
        doc.fingerprint("e1", identity): name
        for doc, name in zip(docs, ("read", "undated", "misread"), strict=True)
    }
    return settle_by(docs, names, verdict_of)


def settle_by(docs: list[Doc], names: dict[str, str], verdict_of) -> dict:
    """The map under verdicts `verdict_of` gives each ambiguous pair, by
    its two mentions' `names`."""
    manifests = [doc.manifest for doc in docs]
    identity = cluster_corpus(manifests)
    verdicts = {
        clustering._pair(*p["mentions"]): verdict_of(
            frozenset(names[m] for m in p["mentions"])
        )
        for entry in identity["review"]
        for p in entry["pairs"]
        if p["link"] == AMBIGUOUS
    }
    return cluster_corpus(manifests, None, verdicts)


def test_members_no_same_joined_that_contradict_stay_apart():
    """`same` joins each dated register to the undated one, but the two
    dated ones are `different`: the part the closure makes holds the two
    the adjudicator kept apart, so all three stay apart, and count among
    the ambiguous."""
    settled = settle(
        three_nicholases(),
        lambda pair: DIFFERENT if pair == {"read", "misread"} else SAME,
    )
    assert {e["method"] for e in settled["mentions"].values()} == {AMBIGUOUS}
    (entry,) = settled["review"]
    assert entry["status"] == ADJUDICATED
    assert entry["contradiction"]["evidence"] == DIFFERENT
    assert settled["summary"]["clusters"][AMBIGUOUS] == 1
    assert settled["summary"]["cluster_mentions"][AMBIGUOUS] == 3
    reasons = clustering.attention(settled["summary"], adjudicated=True)
    assert reasons[0].startswith("ambiguous clusters not merged: 1 cluster")


def test_a_same_verdict_overrides_a_date_contradiction():
    """A misreading disagrees on Nicholas's birth: when the adjudicator
    says every pair is one person, the dates' conflict does not keep them
    apart."""
    settled = settle(three_nicholases(), lambda pair: SAME)
    assert {e["method"] for e in settled["mentions"].values()} == {ADJUDICATED}
    assert settled["summary"]["clusters"][ADJUDICATED] == 1
    assert settled["summary"]["cluster_mentions"][ADJUDICATED] == 3


def test_no_qid_reaches_the_evidence_or_the_prompt():
    """An item already in the graph is named as one, and a QID in a
    source's own words is masked in the prompt."""
    doc = document(
        "a register",
        [human("e1", "Gustav Löbig")],
        [
            (
                "e1",
                "residence",
                {"type": "item", "value": {"kind": "qid", "qid": "Q31"}},
            )
        ],
        {"e1": "gustav-loebig"},
        sha1="c" * 40,
    )
    lines = adjudication._Document(doc.manifest).lines("e1")
    assert "residence: an item already in the graph" in lines
    assert not any(QID.search(line) for line in lines)
    other = gustav("one", dated=True)
    identity = cluster_corpus([doc.manifest, other.manifest])
    (pair,) = adjudication._links(identity)
    evidence = adjudication.Evidence(
        [doc.manifest, other.manifest], identity["mentions"]
    )
    evidence._documents[doc.sha1].names["e1"] = ("Gustav Q42 Löbig", "human")
    prompt = adjudication.prompt_for([pair], {}, evidence)
    assert QID.search(prompt) is None
    assert "Gustav an item Löbig" in prompt


def test_an_adjudicated_map_is_never_taken_for_clustering_s_own(tmp_path):
    """The verdicts are part of the key a map is reused under: clustering
    alone, over the same documents, makes its own map again rather than
    read the adjudicated one as its."""
    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    context = context_of(docs, tmp_path, Engine(truths(docs)))
    adjudication.run_stage(context)
    output = clustering.ensure_identity_map(
        context.directory, context.documents
    )
    assert not output.reused
    assert ADJUDICATED not in {
        e["method"] for e in output.identity_map["mentions"].values()
    }
