"""Offline tests for greedy-overlap artifact sequencing (ADR-0019)."""

import json

from yizkr.pipeline.sequencing import sequence_documents


class TestSequenceDocuments:
    def test_hub_goes_first(self):
        # 'tree' shares an entity with each of the two sparse docs; it is the
        # most connective, so it seeds the graph first.
        items = [
            ("letter", {"sebastian", "klara"}),
            ("tree", {"sebastian", "klara", "felix", "gustav"}),
            ("photo", {"felix", "rahel"}),
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
        items = [
            ("hub", {"x", "y"}),
            ("z1", {"x"}),
            ("z2", {"y"}),
            ("solo", set()),
        ]
        assert set(sequence_documents(items)) == {"hub", "z1", "z2", "solo"}


class TestEntityKeys:
    """The serial sequencer keys a document by the whole name forms of its
    extractable entities, read through their mention records, as
    reconcile blocks (#388): a shared surname alone connects nothing."""

    def keys(self, tmp_path, entities, claims=()):
        from types import SimpleNamespace

        from yizkr.pipeline.orchestrate import Orchestrator

        path = tmp_path / "batch.json"
        path.write_text(
            json.dumps(
                {
                    "extraction_run": {
                        "model_identifier": "fake-model",
                        "pipeline_version": "0.1.0",
                        "started_at": "2026-07-13T09:00:00Z",
                    },
                    "claims": list(claims),
                    "entities": entities,
                }
            )
        )
        return Orchestrator._entity_keys(None, SimpleNamespace(batch=path))

    def test_whole_forms_of_extractable_entities(self, tmp_path):
        keys = self.keys(
            tmp_path,
            [
                {
                    "id": "e1",
                    "label": "Gustav Löbig",
                    "aliases": ["Gustl"],
                    "kind": "human",
                },
                {
                    "id": "e2",
                    "label": "Standesamt Flörburg",
                    "kind": "organization",
                    "extract": False,
                },
                {
                    "id": "e3",
                    "label": "Dein Vater",
                    "kind": "human",
                    "structure": {"kind": "human", "status": "kinship"},
                },
            ],
        )
        assert keys == {"gustav lobig", "gustav loebig", "gustl"}

    def test_a_persons_appellation_keys_nothing(self, tmp_path):
        """#681: a person's alias that the batch gives as an appellation
        (ADR-0018) keys nothing, as reconcile blocks given the batch, so
        two documents that share only "Oma" do not connect by it; a place
        keys on every name, and an alias the batch does not mark keys."""
        from yizkr.claims import ArtifactRef

        def called(local_id: str, label: str, text: str) -> dict:
            return {
                "subject": {
                    "kind": "local",
                    "local_id": local_id,
                    "label": label,
                },
                "property": "referred_to_as",
                "value": {
                    "type": "monolingual",
                    "value": {"text": text, "language": "de"},
                },
                "source": {
                    "artifact": ArtifactRef.from_bytes(
                        b"fictional scan: the 2008 family history"
                    ).model_dump()
                },
            }

        entities = [
            {
                "id": "e1",
                "label": "Hildegard Löbig",
                "aliases": ["Oma", "Hilde"],
                "kind": "human",
            },
            {
                "id": "e2",
                "label": "Flörburg",
                "aliases": ["die Stadt"],
                "kind": "place",
            },
        ]
        keys = self.keys(
            tmp_path,
            entities,
            [
                called("e1", "Hildegard Löbig", "Oma"),
                called("e2", "Flörburg", "die Stadt"),
            ],
        )
        assert keys == {
            "hildegard lobig",
            "hildegard loebig",
            "hilde",
            "florburg",
            "floerburg",
            "die stadt",
        }
        assert "oma" in self.keys(tmp_path, entities)

    def test_a_label_and_container_name_keys_as_its_label(self, tmp_path):
        """#683: a place's written name that is its label and one of its
        own containers keys as the label, so it meets the same place
        written without its container."""
        keys = self.keys(
            tmp_path,
            [
                {
                    "id": "e1",
                    "label": "Snylsjum",
                    "kind": "place",
                    "structure": {
                        "kind": "place",
                        "status": "toponym",
                        "names": [
                            {
                                "written": "Snylsjum, Fryslân",
                                "toponym": "Snylsjum",
                            }
                        ],
                        "within": ["Fryslân"],
                    },
                }
            ],
        )
        assert keys == {"snylsjum"}

    def test_no_batch_no_keys(self, tmp_path):
        from types import SimpleNamespace

        from yizkr.pipeline.orchestrate import Orchestrator

        missing = SimpleNamespace(batch=tmp_path / "batch.json")
        assert Orchestrator._entity_keys(None, missing) == set()
