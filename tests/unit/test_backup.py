"""Offline tests for backup/export logic (W1 task 7). No stack, no docker."""

import gzip
from pathlib import Path

from yizkr.backup import (
    filter_public_export,
    is_possibly_living,
    prune_old_backups,
)

CUTOFF = 1926  # as if today were 2026 with the default 100-year window


class TestLivingCall:
    def test_unknown_means_excluded(self):
        possibly, reason = is_possibly_living(None, False, None, CUTOFF)
        assert possibly
        assert "unknown means excluded" in reason

    def test_death_evidence_includes(self):
        assert is_possibly_living(None, True, None, CUTOFF)[0] is False
        assert is_possibly_living(1980, True, None, CUTOFF)[0] is False

    def test_birth_beyond_cutoff_includes(self):
        assert is_possibly_living(1900, False, None, CUTOFF)[0] is False

    def test_recent_birth_without_death_excludes(self):
        assert is_possibly_living(1958, False, None, CUTOFF)[0] is True

    def test_explicit_flag_wins_both_directions(self):
        # flagged living: excluded even with death evidence recorded
        assert is_possibly_living(1900, True, "yes", CUTOFF)[0] is True
        # curator cleared the flag: included even without evidence
        assert is_possibly_living(1980, False, "no", CUTOFF)[0] is False


REF_DEAD = "a" * 40  # reference used only by the excluded person
REF_SHARED = "b" * 40  # reference used by an included person too
VALUE_DEAD = "c" * 40  # value node reachable only via REF_DEAD
VALUE_LIVE = "d" * 40  # value node reachable via REF_SHARED


class TestPublicFilter:
    LINES = [
        # the excluded person's own triples: entity, statement node, sitelink
        '<http://w.lan/entity/Q76> <http://x/p> "Donald" .',
        '<http://w.lan/entity/statement/Q76-aaaa-bbbb> <http://x/p> "1958" .',
        "<http://w.lan/wiki/Item:Q76> <http://schema.org/about> <http://w.lan/entity/Q76> .",
        # someone else's claim ABOUT the excluded person
        "<http://w.lan/entity/Q72> <http://w.lan/prop/direct/P7> <http://w.lan/entity/Q76> .",
        # an includable person whose QID shares a prefix (Q7 vs Q76)
        '<http://w.lan/entity/Q7> <http://x/p> "Samuel" .',
        # unrelated triple
        '<http://w.lan/entity/Q100> <http://x/p> "place" .',
        # provenance: the excluded person's statement uses REF_DEAD
        f"<http://w.lan/entity/statement/Q76-cccc> <http://www.w3.org/ns/prov#wasDerivedFrom> <http://w.lan/reference/{REF_DEAD}> .",
        # REF_DEAD's content quotes source text naming the living person
        f'<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/P8> "5. Donald Hirsch b: 15 Oct 1958"@en .',
        f"<http://w.lan/reference/{REF_DEAD}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_DEAD}> .",
        f'<http://w.lan/value/{VALUE_DEAD}> <http://x/time> "1958" .',
        # provenance shared with an included person's statement
        f"<http://w.lan/entity/statement/Q7-dddd> <http://www.w3.org/ns/prov#wasDerivedFrom> <http://w.lan/reference/{REF_SHARED}> .",
        f'<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/P8> "1. Samuel b: 1853"@en .',
        f"<http://w.lan/reference/{REF_SHARED}> <http://w.lan/prop/reference/value/P9> <http://w.lan/value/{VALUE_LIVE}> .",
        f'<http://w.lan/value/{VALUE_LIVE}> <http://x/time> "1853" .',
    ]

    def filter(self, tmp_path: Path, excluded: set[str], excerpt_pids=frozenset()):
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "public.nt.gz"
        with gzip.open(full, "wt") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        kept, dropped = filter_public_export(full, public, excluded, excerpt_pids)
        with gzip.open(public, "rt") as handle:
            return kept, dropped, handle.read()

    def test_every_mention_is_dropped(self, tmp_path):
        _, _, content = self.filter(tmp_path, {"Q76"})
        assert "Q76" not in content
        # the OTHER person's claim about Q76 is gone, but Q72 itself is
        # only trimmed, not removed wholesale
        assert "Samuel" in content and "Q100" in content

    def test_orphaned_provenance_nodes_are_swept(self, tmp_path):
        """The leak found live: a dropped statement's reference node
        still carried source text naming the living person."""
        _, _, content = self.filter(tmp_path, {"Q76"})
        assert "Donald Hirsch" not in content
        assert REF_DEAD not in content
        assert VALUE_DEAD not in content
        # shared provenance still reachable from Q7's statement survives
        assert REF_SHARED in content
        assert VALUE_LIVE in content

    def test_excerpts_never_enter_the_public_export(self, tmp_path):
        _, _, content = self.filter(tmp_path, set(), excerpt_pids={"P8"})
        assert "Samuel b: 1853" not in content  # excerpt gone even for the dead
        assert REF_SHARED in content  # the reference node itself survives
        assert VALUE_LIVE in content

    def test_qid_prefix_does_not_collide(self, tmp_path):
        _, _, content = self.filter(tmp_path, {"Q7"})
        assert "Samuel" not in content
        assert '"Donald"' in content  # Q76 untouched by excluding Q7

    def test_no_exclusions_passes_everything(self, tmp_path):
        kept, dropped, _ = self.filter(tmp_path, set())
        assert (kept, dropped) == (len(self.LINES), 0)


class TestPruning:
    def test_prunes_oldest_beyond_keep(self, tmp_path):
        for stamp in (
            "20260101T000000Z",
            "20260201T000000Z",
            "20260301T000000Z",
        ):
            (tmp_path / stamp / "x").mkdir(parents=True)
        (tmp_path / "not-a-backup").mkdir()
        doomed = prune_old_backups(tmp_path, keep=2)
        assert doomed == ["20260101T000000Z"]
        assert not (tmp_path / "20260101T000000Z").exists()
        assert (tmp_path / "20260301T000000Z").exists()
        assert (tmp_path / "not-a-backup").exists()

    def test_keep_zero_prunes_nothing(self, tmp_path):
        (tmp_path / "20260101T000000Z").mkdir()
        assert prune_old_backups(tmp_path, keep=0) == []
        assert (tmp_path / "20260101T000000Z").exists()
