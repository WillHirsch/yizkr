"""Offline tests for greedy-overlap artifact sequencing (ADR-0019)."""

from yizkr.pipeline.sequencing import sequence_documents


class TestSequenceDocuments:
    def test_hub_goes_first(self):
        # 'tree' shares an entity with each of the two sparse docs; it is the
        # most connective, so it seeds the graph first.
        items = [
            ("letter", {"donald", "rosa"}),
            ("tree", {"donald", "rosa", "anne", "salomon"}),
            ("photo", {"anne", "gaby"}),
        ]
        assert sequence_documents(items)[0] == "tree"

    def test_isolated_document_goes_last(self):
        items = [
            ("a", {"x", "y"}),
            ("b", {"y", "z"}),
            ("island", {"lonely"}),  # overlaps nothing
        ]
        assert sequence_documents(items)[-1] == "island"

    def test_empty_key_set_sorts_last(self):
        items = [("real", {"x", "y"}), ("blank", set())]
        assert sequence_documents(items) == ["real", "blank"]

    def test_ties_broken_by_size_then_order(self):
        # equal overlap (both share only 'x' with the other two); the larger
        # set wins the tie.
        items = [
            ("small", {"x"}),
            ("big", {"x", "p", "q"}),
            ("other", {"x"}),
        ]
        order = sequence_documents(items)
        assert order[0] == "big"

    def test_stable_when_fully_symmetric(self):
        # identical key sets -> overlap and size equal -> original order kept
        items = [("first", {"x"}), ("second", {"x"}), ("third", {"x"})]
        assert sequence_documents(items) == ["first", "second", "third"]

    def test_single_and_empty(self):
        assert sequence_documents([]) == []
        assert sequence_documents([("only", {"x"})]) == ["only"]

    def test_is_a_permutation(self):
        items = [
            ("a", {"1", "2"}),
            ("b", {"2", "3"}),
            ("c", {"3", "4"}),
            ("d", {"9"}),
        ]
        order = sequence_documents(items)
        assert sorted(order) == ["a", "b", "c", "d"]
        assert len(order) == len(set(order))

    def test_order_only_never_drops(self):
        # a document that overlaps nothing is still placed, never lost
        items = [("hub", {"x", "y"}), ("z1", {"x"}), ("z2", {"y"}), ("solo", set())]
        assert set(sequence_documents(items)) == {"hub", "z1", "z2", "solo"}
