"""Offline tests for the run-to-run diff (#198): `yizkr scratch diff`,
what changed between two scratch runs, and why.

The end-to-end tests diff two fictional run directories that the real
scratch driver writes over a small corpus of the example universe
(docs/EXAMPLES.md), with its fake engine behind the real LLM cache
(tests/unit/test_scratch.py): a second run with the extraction role's
epoch raised and one letter's claim read otherwise, and a second run
whose clustering differs while every claim stays the same. The other
tests drive each comparison with batches, alerts and reports of their
own.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_fingerprints import BIRTH, batch, claim, ref
from tests.unit.test_no_wiki_before_write import refusals  # noqa: F401
from tests.unit.test_scratch import (  # noqa: F401
    LETTER,
    Engine,
    cli_run,
    command,
    fakes,
    runs_dir,
    small,
)
from yizkr.pipeline import clustering, llm, report, rundiff, scratch
from yizkr.pipeline.fingerprints import claim_prints
from yizkr.pipeline.rundiff import (
    ABSENT,
    NO_BASELINE,
    NOT_AVAILABLE,
    SAME_KEY,
    Components,
    _Call,
    _Document,
    attribute,
    compare_claims,
    compare_findings,
    findings,
    keyed_calls,
)
from yizkr.pipeline.stages import canonical_json, sha256_text

LABEL = "Eugen Löbig"


@pytest.fixture
def engine(request):
    """The fake engine behind the client factory (test_scratch's
    `fakes`)."""
    return request.getfixturevalue("fakes")


class Later(Engine):
    """The fake engine, reading the marriage certificate's claim with
    another year: the chunk answer gives 1837 for 1836."""

    def complete(self, prompt, image_paths=()):
        answer = super().complete(prompt, image_paths)
        if not image_paths and self.fakes.corpus.about(prompt) == LETTER:
            answer = answer.replace('"year": 1836', '"year": 1837')
        return answer


def two_runs(tmp_path, engine, monkeypatch, **second) -> tuple[Path, Path]:
    """Two scratch runs of the small corpus, the second with `second`'s
    options, and its letter read by Later when `second` raises the
    extraction role's epoch."""
    corpus = small(tmp_path)
    assert command(tmp_path, corpus, engine) == scratch.EXIT_OK
    if second.get("epochs"):

        def make_client(name, *, effort=None, model=None, **kwargs):
            return Later(engine, effort=effort, model=model)

        monkeypatch.setattr(llm, "make_client", make_client)
    assert command(tmp_path, corpus, engine, **second) == scratch.EXIT_OK
    first, latest = scratch.runs_in(runs_dir(tmp_path))
    return first, latest


def strings(value) -> list[str]:
    """Every string a JSON value holds, keys included."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [k, *strings(v)]]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return [value] if isinstance(value, str) else []


def leaks(text: str, run: Path, tmp_path: Path) -> list[str]:
    """What of a run's documents and paths a text names: a file name, a
    document's SHA-1 (or the start of one), the label, or a path."""
    made = report.build(run)
    names = [LABEL, "Eugen", "Löbig", str(tmp_path)]
    for document in made["documents"]:
        names += [*document["files"], document["sha1"][:10]]
        names += [Path(f).stem for f in document["files"]]
    return [name for name in names if name in text]


# -- two runs, end to end -----------------------------------------------------


class TestTwoRuns:
    def test_each_recompute_is_put_down_to_the_component_that_changed(
        self, tmp_path, engine, monkeypatch
    ):
        first, second = two_runs(
            tmp_path, engine, monkeypatch, epochs={"extract": 1}
        )
        diff = rundiff.build(first, second)
        aggregate = diff["aggregate"]
        stages = aggregate["stages"]
        # the photographs' readings come from the cache; the letters are
        # their own transcriptions, which make no call
        assert stages["transcription"]["run"] == {
            "computed": 2,
            "reused: key match": 2,
        }
        assert stages["transcription"]["recomputed_by"] == {"no call": 2}
        assert stages["transcription"]["views"] == {"same": 4}
        # every extraction call is keyed on the role's epoch
        assert stages["extraction"]["recomputed_by"] == {"epoch": 4}
        assert stages["extraction"]["calls_by"] == {"epoch": 8}
        assert stages["extraction"]["views"] == {"changed": 1, "same": 3}
        # the critic is asked of the letter's new batch: its prompt
        assert stages["critique"]["recomputed_by"] == {"prompt": 1}
        assert aggregate["runs"]["components"] == "read"
        assert aggregate["runs"]["same_cache"] is True
        assert {
            "setting": "roles.extract.epoch",
            "baseline": 0,
            "run": 1,
        } in aggregate["settings"]
        claims = aggregate["claims"]
        assert (claims["matched"], claims["changed"]) == (3, 1)
        assert (claims["added"], claims["removed"]) == (0, 0)
        assert claims["changed_parts"] == {"value": 1}
        assert claims["by_property"] == {
            "date_of_birth": {"added": 0, "removed": 0, "changed": 1}
        }
        assert claims["documents"]["with claim changes"] == 1
        # the detail, per document, holds the values
        (letter,) = [
            d for d in diff["detail"]["documents"].values() if d.get("claims")
        ]
        assert letter["files"] == [LETTER]
        (changed,) = letter["claims"]["changed"]
        assert changed["baseline"]["value"][1] == 1836
        assert changed["run"]["value"][1] == 1837
        assert changed["run"]["subject_label"] == LABEL
        assert letter["stages"]["extraction"]["view"] == "changed"

    def test_a_reclustering_changes_no_claim(
        self, tmp_path, engine, monkeypatch
    ):
        """The second run is answered from the cache throughout, and
        its identity map is then made by another clustering: each
        mention a singleton. The diff finds every claim and mention as
        it was, and the one cluster split."""
        first, second = two_runs(tmp_path, engine, monkeypatch)
        path = second / scratch.CORPUS_DIR / "clustering" / "identity-map.json"
        found = json.loads(path.read_text())
        (merged,) = found["clusters"].values()
        assert len(merged["members"]) == 4
        found["clusters"] = {}
        for fingerprint, mention in found["mentions"].items():
            cid = clustering.cluster_id([fingerprint])
            mention.update(cluster=cid, method="singleton")
            found["clusters"][cid] = {
                "kind": mention["kind"],
                "method": "singleton",
                "members": [fingerprint],
            }
        found["hash"] = clustering.identity_hash(found["mentions"])
        path.write_text(json.dumps(found))
        aggregate = rundiff.build(first, second)["aggregate"]
        claims = aggregate["claims"]
        assert (claims["baseline"], claims["run"], claims["matched"]) == (
            4,
            4,
            4,
        )
        assert claims["added"] == claims["removed"] == claims["changed"] == 0
        assert aggregate["mentions"]["added"] == 0
        assert aggregate["mentions"]["removed"] == 0
        for stage in ("extraction", "critique"):
            assert aggregate["stages"][stage]["recomputed_by"] == {}
        clusters = aggregate["clusters"]
        assert clusters["same_decisions"] is False
        assert (clusters["splits"], clusters["mentions_split"]) == (1, 4)
        assert clusters["splits_by_kind"] == {"human": 1}
        assert clusters["merges"] == 0
        assert clusters["methods_changed"] == {"confident → singleton": 4}
        # the other way round, the same change is a merge
        back = rundiff.build(second, first)["aggregate"]["clusters"]
        assert (back["merges"], back["mentions_merged"]) == (1, 4)
        assert back["splits"] == 0

    def test_the_summary_names_no_document(
        self, tmp_path, engine, monkeypatch
    ):
        first, second = two_runs(
            tmp_path, engine, monkeypatch, epochs={"extract": 1}
        )
        written = rundiff.write_diff(first, second)
        summary = written[rundiff.MARKDOWN_FILE].read_text()
        diff = json.loads(written[rundiff.JSON_FILE].read_text())
        assert leaks(summary, second, tmp_path) == []
        assert (
            leaks(" ".join(strings(diff["aggregate"])), second, tmp_path) == []
        )
        # the detail names them
        assert leaks(
            json.dumps(diff["detail"], ensure_ascii=False), second, tmp_path
        )
        # the documents per language are never set side by side
        figures = [row["figure"] for row in diff["aggregate"]["figures"]]
        assert figures
        assert not [f for f in figures if f.startswith("documents.languages")]
        assert "date_of_birth" in summary and "epoch" in summary

    def test_it_writes_into_the_runs_private_folder(
        self, tmp_path, engine, monkeypatch
    ):
        first, second = two_runs(tmp_path, engine, monkeypatch)
        written = rundiff.write_diff(first, second)
        folder = second / rundiff.DIFF_DIR / first.name
        assert written == {
            name: folder / name
            for name in (rundiff.JSON_FILE, rundiff.MARKDOWN_FILE)
        }
        for path in (second / rundiff.DIFF_DIR, folder):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in written.values():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # written again, each file is replaced whole
        assert rundiff.write_diff(first, second) == written
        assert sorted(p.name for p in folder.iterdir()) == sorted(written)

    def test_it_asks_nothing_of_the_wiki_or_the_engine(
        self, tmp_path, engine, monkeypatch, request
    ):
        first, second = two_runs(tmp_path, engine, monkeypatch)
        asked = request.getfixturevalue("refusals")

        def no_engine(*args, **kwargs):
            raise AssertionError("the diff asked for an engine")

        monkeypatch.setattr(llm, "make_client", no_engine)
        rundiff.write_diff(first, second)
        assert asked.asked == []

    def test_without_the_cache_a_recompute_is_not_attributed(
        self, tmp_path, engine, monkeypatch
    ):
        first, second = two_runs(
            tmp_path, engine, monkeypatch, epochs={"extract": 1}
        )
        aggregate = rundiff.build(
            first, second, cache=tmp_path / "no cache here"
        )["aggregate"]
        assert aggregate["runs"]["components"] == NOT_AVAILABLE
        assert aggregate["stages"]["extraction"]["recomputed_by"] == {
            NOT_AVAILABLE: 4
        }


# -- reports ------------------------------------------------------------------


class TestReports:
    def test_an_older_report_counts_its_missing_figures_absent(
        self, tmp_path, engine, monkeypatch
    ):
        """A baseline report.json made before mention records and
        judgement notes were counted holds no `records` or `notes`: the
        diff reads it, and shows those figures absent, never 0."""
        first, second = two_runs(tmp_path, engine, monkeypatch)
        older = report.build(first)
        del older["aggregate"]["content"]["records"]
        del older["aggregate"]["content"]["notes"]
        stored = first / report.REPORT_DIR / report.JSON_FILE
        stored.parent.mkdir()
        stored.write_text(json.dumps(older))
        assert rundiff.read_report(first) == (older, "stored")
        aggregate = rundiff.build(first, second)["aggregate"]
        assert aggregate["runs"]["baseline"]["report"] == "stored"
        assert aggregate["runs"]["run"]["report"] == "built"
        rows = {row["figure"]: row for row in aggregate["figures"]}
        for name in (
            "content.records.given",
            "content.records.recordless",
            "content.records.absent.predating",
            "content.notes.total",
            "content.notes.set_aside",
        ):
            assert rows[name]["baseline"] == ABSENT
            assert rows[name]["delta"] is None
            assert isinstance(rows[name]["run"], int)
        assert rows["content.records.given"]["run"] == 4
        summary = rundiff.render_markdown(aggregate)
        assert "| content.records.given | absent | 4 | n/a |" in summary

    def test_a_name_a_tally_leaves_out_counts_0(self):
        baseline = {
            "calls": {"misses_by_reason": {"absent": 2}, "engine": 2},
            "content": {"claims": {"by_property": {}}},
        }
        run = {
            "calls": {"misses_by_reason": {"refresh": 1}, "engine": 1},
            "content": {"claims": {"by_property": {"father": 1}}},
            "stages": [{"name": "critique", "computed": 3}],
        }
        rows = {r["figure"]: r for r in rundiff.figures(baseline, run)}
        assert rows["calls.misses_by_reason.absent"]["run"] == 0
        assert rows["calls.misses_by_reason.refresh"]["baseline"] == 0
        assert rows["content.claims.by_property.father"]["delta"] == 1
        # a stage, or a section, one report lacks is absent
        assert rows["stages.critique.computed"]["baseline"] == ABSENT
        assert rows["calls.engine"]["delta"] == -1

    def test_a_report_older_than_the_run_is_built_afresh(
        self, tmp_path, engine, monkeypatch
    ):
        first, _ = two_runs(tmp_path, engine, monkeypatch)
        made = report.build(first)
        stale = dict(made, generated_at="2000-01-01T00:00:00+00:00")
        stored = first / report.REPORT_DIR / report.JSON_FILE
        stored.parent.mkdir()
        stored.write_text(json.dumps(stale))
        assert rundiff.read_report(first)[1] == "built"
        stored.write_text(json.dumps(made))
        assert rundiff.read_report(first)[1] == "stored"
        assert rundiff.read_report(first, rebuild=True)[1] == "built"
        stored.write_text("not json")
        assert rundiff.read_report(first)[1] == "built"

    def test_a_directory_with_no_run_is_refused(self, tmp_path):
        (tmp_path / "a").mkdir()
        with pytest.raises(rundiff.DiffError, match="no run"):
            rundiff.build(tmp_path / "a", tmp_path / "a")


# -- the baseline -------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def a_manifest(run: Path, commit: str, *, dirty=False, complete=True):
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "format": 1,
                "run_id": run.name,
                "mode": "scratch run",
                "code": {"version": "0.1.0", "commit": commit, "dirty": dirty},
                "scratch": {"complete": complete},
            }
        )
    )


class TestBaseline:
    def test_it_is_the_latest_completed_run_on_main(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        for name in ("user.name", "user.email"):
            git(repo, "config", name, "Ida Sörne")
        git(repo, "commit", "-q", "--allow-empty", "-m", "Flörburg")
        older = git(repo, "rev-parse", "HEAD")
        git(repo, "commit", "-q", "--allow-empty", "-m", "Peiltal")
        newer = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "198-klurnau")
        git(repo, "commit", "-q", "--allow-empty", "-m", "Klurnau")
        branch = git(repo, "rev-parse", "HEAD")
        monkeypatch.chdir(repo)
        runs = tmp_path / "runs"
        a_manifest(runs / "20261001T090000Z-000001", older)
        a_manifest(runs / "20261002T090000Z-000002", newer)
        # not complete, not clean, not on main: none is the baseline
        a_manifest(runs / "20261003T090000Z-000003", newer, complete=False)
        a_manifest(runs / "20261004T090000Z-000004", newer, dirty=True)
        a_manifest(runs / "20261005T090000Z-000005", branch)
        run = runs / "20261005T090000Z-000005"
        assert rundiff.baseline_run(runs, run) == (
            runs / "20261002T090000Z-000002"
        )
        # the run itself is never its baseline, even on main
        assert rundiff.baseline_run(
            runs, runs / "20261002T090000Z-000002"
        ) == (runs / "20261001T090000Z-000001")
        # a run on a branch is no baseline, however late
        assert rundiff.baseline_run(
            runs, runs / "20261004T090000Z-000004"
        ) == (runs / "20261002T090000Z-000002")
        # a ref git does not know is passed over for the next
        assert rundiff.baseline_run(
            runs, run, refs=("origin/nowhere", "main")
        ) == (runs / "20261002T090000Z-000002")
        # the branch as main: its run is the latest on it
        assert rundiff.baseline_run(
            runs, runs / "20261004T090000Z-000004", refs=("198-klurnau",)
        ) == (runs / "20261005T090000Z-000005")
        with pytest.raises(rundiff.DiffError, match="no completed run"):
            rundiff.baseline_run(runs, run, refs=("origin/nowhere",))


# -- attributing a recompute --------------------------------------------------


def components(**changed) -> dict:
    found = {
        "engine": "claude-code",
        "model": "claude-opus-5-5",
        "effort": "low",
        "config": "c" * 64,
        "call": "complete",
        "system": "s" * 64,
        "prompt": "p" * 64,
        "files": [],
        "attempt": 0,
        "epoch": 0,
    }
    return found | changed


def an_entry(cache: Path, found: dict, *, aside: bool = False) -> str:
    key = sha256_text(canonical_json(found))
    folder = cache / key[:2]
    # private to its owner, as the cache makes its folders
    for made in (cache, folder):
        made.mkdir(mode=0o700, exist_ok=True)
        made.chmod(0o700)
    name = (
        f"{key}.replaced-20261001T090000Z-ab.json" if aside else f"{key}.json"
    )
    (folder / name).write_text(
        json.dumps(
            {
                "format": 1,
                "key": key,
                "components": found,
                "response": "Standesamt Flörburg",
            }
        )
    )
    return key


class TestAttribution:
    def test_the_closest_baseline_call_names_what_changed(self, tmp_path):
        cache = tmp_path / "cache"
        old_open = an_entry(cache, components(call="open"))
        old_chunk = an_entry(cache, components(prompt="q" * 64), aside=True)
        new_chunk = an_entry(cache, components(prompt="r" * 64, epoch=1))
        new_model = an_entry(cache, components(model="claude-sonnet-5-5"))
        read = Components(cache)
        baseline = [_Call("cache-miss", old_open, "absent")]
        baseline.append(_Call("cache-hit", old_chunk, "key match"))
        joined, calls = attribute(
            [
                _Call("cache-miss", new_chunk, "absent"),
                _Call("cache-miss", new_model, "absent"),
                _Call("cache-hit", old_chunk, "key match"),
            ],
            baseline,
            read,
            "absent",
        )
        # the chunk differs from the old chunk in its prompt and epoch,
        # and the other call from the opening turn in its model and call
        assert calls == {"prompt": 1, "epoch": 1, "model": 1, "call": 1}
        assert joined == "call + epoch + model + prompt"

    def test_what_cannot_be_compared_says_so(self, tmp_path):
        cache = tmp_path / "cache"
        kept = an_entry(cache, components())
        read = Components(cache)
        missing = "f" * 64
        assert attribute(
            [_Call("cache-miss", kept, "absent")],
            [_Call("cache-hit", kept, "key match")],
            read,
            "absent",
        ) == (SAME_KEY, {SAME_KEY: 1})
        assert (
            attribute(
                [_Call("cache-miss", kept, "absent")], [], read, "absent"
            )[0]
            == NO_BASELINE
        )
        assert (
            attribute(
                [_Call("cache-miss", missing, "absent")],
                [_Call("cache-hit", kept, "key match")],
                read,
                "absent",
            )[0]
            == NOT_AVAILABLE
        )
        assert (
            attribute(
                [_Call("cache-miss", kept, "absent")],
                [_Call("cache-hit", missing, "key match")],
                read,
                "absent",
            )[0]
            == NOT_AVAILABLE
        )
        assert attribute(
            [
                _Call("cache-miss", kept, "refresh"),
                _Call("cache-miss", kept, "rejected"),
                _Call("cache-replay", kept, None, matches=False),
                _Call("cache-replay", kept, None, matches=True),
            ],
            [],
            read,
            "absent",
        ) == (
            "refresh + rejected + replay changed",
            {"refresh": 1, "rejected": 1, "replay changed": 1},
        )
        assert attribute([], [], read, "no call")[0] == "no call"
        assert attribute([], [], read, "no cache")[0] == "no cache"

    def test_only_components_that_hash_to_their_key_are_read(self, tmp_path):
        cache = tmp_path / "cache"
        key = an_entry(cache, components())
        entry = cache / key[:2] / f"{key}.json"
        found = json.loads(entry.read_text())
        assert Components(cache)(key) == components()
        found["components"]["epoch"] = 3
        entry.write_text(json.dumps(found))
        assert Components(cache)(key) is None
        assert not Components(tmp_path / "none").available
        # a folder open to other users, which the cache refuses, is not
        # read
        cache.chmod(0o755)
        assert not Components(cache).available
        assert Components(None)(key) is None

    def test_cache_events_are_keyed_to_their_stages_latest_attempt(
        self, tmp_path
    ):
        sha1 = "a" * 40
        keys = [f"{n:x}" * 64 for n in range(1, 6)]
        lines = [
            {"event": "run-start"},
            {"event": "started", "subject": sha1, "stage": "extraction"},
            {"event": "cache-miss", "subject": sha1, "key": keys[0]},
            {"event": "run-start"},
            {"event": "started", "subject": sha1, "stage": "extraction"},
            {"event": "cache-hit", "subject": sha1, "key": keys[1]},
            {"event": "done", "subject": sha1, "stage": "extraction"},
            # no stage in flight: the role's stage
            {
                "event": "cache-miss",
                "subject": sha1,
                "key": keys[2],
                "role": "critique",
                "reason": "absent",
            },
            {"event": "cache-miss", "subject": sha1, "key": "not a key"},
            {"event": "cache-miss", "subject": sha1, "key": keys[3]},
        ]
        (tmp_path / "events.jsonl").write_text(
            "\n".join(map(json.dumps, lines)) + "\n{torn"
        )
        found = keyed_calls(tmp_path)
        assert [c.key for c in found[(sha1, "extraction")]] == [keys[1]]
        assert [c.key for c in found[(sha1, "critique")]] == [keys[2]]
        assert found[(sha1, "critique")][0].reason == "absent"
        assert len(found) == 2


# -- claims -------------------------------------------------------------------


def the_extract(year=1919, excerpt=None, label="Gustav Löbig", twice=False):
    """Gustav's birth register extract: his date of birth, and his
    father."""
    born = claim(
        BIRTH, ref("e1"), "date_of_birth", {"type": "time", "year": year}
    )
    if excerpt:
        born["source"]["transcription_excerpt"] = {
            "text": excerpt,
            "language": "de",
        }
    father = claim(
        BIRTH, ref("e1"), "father", {"type": "item", "value": ref("e2")}
    )
    return batch(
        [
            {
                "id": "e1",
                "label": label,
                "kind": "human",
                "mention_lines": [1],
            },
            {
                "id": "e2",
                "label": "Sebastian Löbig",
                "kind": "human",
                "mention_lines": [1],
            },
        ],
        [born, father, *([father] if twice else [])],
    )


def read(found):
    return [
        (
            p,
            None
            if c.source.transcription_excerpt is None
            else c.source.transcription_excerpt.text,
        )
        for p, c in zip(claim_prints(BIRTH, found), found.claims, strict=True)
    ]


class TestClaims:
    def test_claims_are_matched_by_fingerprint_and_paired_when_changed(self):
        changes = compare_claims(read(the_extract()), read(the_extract(1918)))
        assert changes.matched == 1
        assert (changes.added, changes.removed) == ([], [])
        ((before, after, parts),) = changes.changed
        assert parts == ("value",)
        assert before.value[1] == 1919 and after.value[1] == 1918

    def test_identical_claims_count_as_a_multiset(self):
        changes = compare_claims(
            read(the_extract(twice=True)), read(the_extract())
        )
        assert changes.matched == 2
        (removed,) = changes.removed
        assert removed.property == "father"
        assert changes.added == [] and changes.changed == []

    def test_an_excerpt_is_a_detail_of_a_matched_claim(self):
        changes = compare_claims(
            read(the_extract(excerpt="am siebenten Januar")),
            read(the_extract(excerpt="am  siebenten Januar")),
        )
        assert changes.matched == 2 and changes.excerpts == 1
        assert not (changes.added or changes.removed or changes.changed)

    def test_a_claim_whose_mention_changed_is_removed_and_added(self):
        changes = compare_claims(
            read(the_extract()), read(the_extract(label="Gustav Lobig"))
        )
        assert changes.matched == 0 and changes.changed == []
        assert sorted(f.property for f in changes.added) == [
            "date_of_birth",
            "father",
        ]
        assert len(changes.removed) == 2

    def test_a_changed_claim_pairs_with_the_closest_of_its_group(self):
        one = the_extract(1919)
        other = the_extract(1918)
        # the run gives two dates of birth: one differs in its value,
        # the other in its value and a qualifier
        qualified = other.claims[0].model_copy(deep=True)
        qualified.qualifiers = []
        both = batch(
            [e.model_dump(mode="json") for e in other.entities],
            [
                {
                    **other.claims[0].model_dump(
                        mode="json", exclude_none=True
                    ),
                    "value": {"type": "time", "year": 1920},
                    "qualifiers": [
                        {
                            "property": "earliest_date",
                            "value": {"type": "time", "year": 1910},
                        }
                    ],
                },
                other.claims[0].model_dump(mode="json", exclude_none=True),
            ],
        )
        changes = compare_claims(read(one)[:1], read(both))
        ((before, after, parts),) = changes.changed
        assert after.value[1] == 1918 and parts == ("value",)
        (added,) = changes.added
        assert added.value[1] == 1920


def test_names_the_manifest_lacks_are_other(tmp_path):
    """A property or an entity kind the model manifest does not hold is
    counted as other, so the aggregates never carry it."""

    def a_document(name, found):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "batch.json").write_text(found.model_dump_json())
        return _Document(
            BIRTH,
            "text",
            stages={"extraction": {"status": "done"}},
            directory=folder,
        )

    later = the_extract()
    later.entities[1].kind = "klurnau_inmate"
    number = claim(
        BIRTH,
        ref("e1"),
        "klurnau_number",
        {"type": "string", "value": "Lager Klurnau 17"},
    )
    later = batch(
        [e.model_dump(mode="json") for e in later.entities],
        [
            *(
                c.model_dump(mode="json", exclude_none=True)
                for c in later.claims
            ),
            number,
        ],
    )
    claims, mentions, detail = rundiff._compare_documents(
        (
            {BIRTH: a_document("a", the_extract())},
            {BIRTH: a_document("b", later)},
        ),
        rundiff._Names.of({}, {}),
    )
    assert claims["by_property"]["other"] == {
        "added": 1,
        "removed": 0,
        "changed": 0,
    }
    assert mentions["added_by_kind"] == {"other": 1}
    assert mentions["removed_by_kind"] == {"human": 1}
    assert not [s for s in strings([claims, mentions]) if "lurnau" in s]
    # the detail keeps them
    assert "klurnau_number" in json.dumps(detail)


# -- anomalies, failures, flags -----------------------------------------------


def alerts(path: Path, *lines: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "alerts.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )


class TestAnomalies:
    def test_findings_are_new_resolved_or_in_both(self, tmp_path):
        names = rundiff._Names.of({}, {})
        one, other = "a" * 40, "b" * 40
        alerts(
            tmp_path / "a",
            {
                "subject": one[:12],
                "stage": "transcription",
                "check": "transcription-sparse",
            },
            {
                "subject": other[:12],
                "stage": "extraction",
                "check": "zero-claims",
                "status": "new",
            },
            {"subject": "run", "stage": "run", "check": "stage-failures"},
        )
        alerts(
            tmp_path / "b",
            {
                "subject": other[:12],
                "stage": "extraction",
                "check": "zero-claims",
                "status": "carried",
            },
            {"subject": one[:12], "stage": "extraction", "check": "Klurnau"},
        )
        aggregate, detail = compare_findings(
            findings(tmp_path / "a", [one, other]),
            findings(tmp_path / "b", [one, other]),
            names,
        )
        assert (aggregate["new"], aggregate["resolved"]) == (1, 2)
        assert aggregate["in_both"] == 1
        # a check the anomaly checks do not have is other
        assert aggregate["new_by_check"] == {"other": 1}
        assert aggregate["resolved_by_check"] == {
            "stage-failures": 1,
            "transcription-sparse": 1,
        }
        assert aggregate["resolved_by_stage"] == {"run": 1, "transcription": 1}
        assert detail["new"] == [[one, "extraction", "Klurnau"]]
        unread = compare_findings(None, set(), names)[0]
        assert unread["status"] == NOT_AVAILABLE

    def test_failures_are_new_or_resolved(self):
        names = rundiff._Names.of({}, {})
        one, other, third = "a" * 40, "b" * 40, "c" * 40
        old = {
            one: _Document(
                one, "text", failed=True, reason="RuntimeError: Peiltal"
            ),
            other: _Document(other, "media"),
            third: _Document(
                third, "media", failed=True, reason="ValueError: x"
            ),
        }
        new = {
            one: _Document(one, "text"),
            other: _Document(
                other,
                "media",
                failed=True,
                reason="yizkr.pipeline.chunking.ExtractionFailed: Klurnau",
            ),
            third: _Document(
                third, "media", failed=True, reason="ValueError: x"
            ),
        }
        aggregate, detail = rundiff._compare_failures(
            (old, new),
            ({"documents": {"failed": 2}}, {}),
            names,
        )
        assert (
            aggregate["new"],
            aggregate["resolved"],
            aggregate["in_both"],
        ) == (1, 1, 1)
        assert aggregate["new_by_label"] == {"ExtractionFailed": 1}
        assert aggregate["resolved_by_kind"] == {"text": 1}
        assert (aggregate["baseline"], aggregate["run"]) == (2, ABSENT)
        assert set(detail) == {one, other}

    def test_flags_and_contradictions_are_compared_only_when_counted(self):
        counted = {
            "content": {
                "flags": {"by_tier": {"hard": 2}, "total": 2},
                "contradictions": {"status": NOT_AVAILABLE, "why": "a graph"},
            }
        }
        later = {"content": {"flags": {"by_tier": {"soft": 1}, "total": 1}}}
        flags = rundiff._counted_section(counted, later, "flags")
        rows = {r["figure"]: r for r in flags["figures"]}
        assert rows["flags.by_tier.hard"] == {
            "figure": "flags.by_tier.hard",
            "baseline": 2,
            "run": 0,
            "delta": -2,
        }
        assert rows["flags.total"]["delta"] == -1
        missing = rundiff._counted_section(counted, later, "contradictions")
        assert missing["status"] == NOT_AVAILABLE
        assert "does not count them" in missing["why"]
        assert "does not hold them" in missing["why"]


# -- settings -----------------------------------------------------------------


def test_settings_show_only_what_may_be_posted():
    baseline = {
        "engine": {"name": "claude-code", "cli_version": "2.1.288"},
        "roles": {"extract": {"model": "claude-opus-5-5", "effort": "low"}},
        "code": {"commit": "a" * 40, "diff_sha256": None},
        "config": {"language": "de", "chunk_concurrency": 2},
        "corpus": {"folder": "/archive/Flörburg"},
    }
    run = {
        "engine": {"name": "claude-code", "cli_version": "2.1.290"},
        "roles": {"extract": {"model": "claude-sonnet-5-5", "effort": "low"}},
        "code": {"commit": "b" * 40, "diff_sha256": "d" * 64},
        "config": {"language": "ru", "chunk_concurrency": 2},
        "corpus": {"folder": "/archive/Peiltal"},
    }
    rows = {r["setting"]: r for r in rundiff.settings(baseline, run)}
    assert set(rows) == {
        "engine.cli_version",
        "roles.extract.model",
        "code.commit",
        "code.diff_sha256",
        "config.language",
    }
    assert rows["roles.extract.model"]["run"] == "claude-sonnet-5-5"
    assert rows["engine.cli_version"]["run"] == "2.1.290"
    # a language, and the hash of the code's difference, are never shown
    assert rows["config.language"] == {
        "setting": "config.language",
        "baseline": "other",
        "run": "other",
    }
    assert rows["code.diff_sha256"]["run"] == "other"


# -- the command --------------------------------------------------------------


class TestCommand:
    def test_it_diffs_the_runs_it_is_given(
        self, tmp_path, engine, monkeypatch, capsys
    ):
        first, second = two_runs(tmp_path, engine, monkeypatch)
        runs = str(runs_dir(tmp_path))
        assert (
            cli_run("scratch", "diff", first.name, second.name, "--runs", runs)
            == 0
        )
        said = capsys.readouterr().out
        folder = second / rundiff.DIFF_DIR / first.name
        assert str(folder / rundiff.MARKDOWN_FILE) in said
        assert (folder / rundiff.JSON_FILE).is_file()
        # by directory, too
        assert cli_run("scratch", "diff", str(first), str(second)) == 0

    def test_the_baseline_defaults_to_the_latest_on_main(
        self, tmp_path, engine, monkeypatch
    ):
        first, second = two_runs(tmp_path, engine, monkeypatch)
        runs = str(runs_dir(tmp_path))
        monkeypatch.setattr(rundiff, "on_main", lambda run, refs: run == first)
        assert cli_run("scratch", "diff", "--runs", runs) == 0
        assert (second / rundiff.DIFF_DIR / first.name / "diff.md").is_file()
        assert cli_run("scratch", "diff", second.name, "--runs", runs) == 0
        monkeypatch.setattr(rundiff, "on_main", lambda run, refs: False)
        with pytest.raises(SystemExit, match="no completed run on main"):
            cli_run("scratch", "diff", "--runs", runs)

    def test_it_refuses_what_it_cannot_diff(
        self, tmp_path, engine, monkeypatch
    ):
        first, _ = two_runs(tmp_path, engine, monkeypatch)
        with pytest.raises(SystemExit, match="baseline itself"):
            cli_run("scratch", "diff", str(first), str(first))
        with pytest.raises(SystemExit, match="two runs at most"):
            cli_run("scratch", "diff", "a", "b", "c")
        with pytest.raises(SystemExit, match="no run there"):
            cli_run("scratch", "diff", str(first), str(tmp_path / "none"))
