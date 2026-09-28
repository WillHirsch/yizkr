"""Offline tests for the model manifest and bootstrap helpers (pytest)."""

import os
import uuid

import pytest
import yaml
from pydantic import ValidationError

from yizkr.model.bootstrap import build_mapping_wikitext
from yizkr.model.schema import Manifest, load_manifest, read_manifest_data


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


def minimal_raw() -> dict:
    """Smallest valid manifest, for mutation in validator tests."""
    return {
        "version": 1,
        "mapping_page": "Project:Model mapping",
        "schema_source_item": "model_manifest",
        "properties": {
            "subclass_of": {
                "label": "subclass of",
                "description": "d",
                "datatype": "wikibase-item",
            },
            "stated_in": {
                "label": "stated in",
                "description": "d",
                "datatype": "wikibase-item",
            },
        },
        "items": {
            "artifact": {"label": "artifact", "description": "d"},
            "photograph": {
                "label": "photograph",
                "description": "d",
                "subclass_of": "artifact",
            },
            "model_manifest": {"label": "model manifest", "description": "d"},
        },
    }


class TestRealManifest:
    def test_loads_and_validates(self, manifest):
        assert manifest.version == 1
        assert len(manifest.properties) > 20
        assert len(manifest.items) > 15

    def test_provenance_properties_present(self, manifest):
        """The ADR-0003 reference pattern must be expressible."""
        for key in (
            "stated_in",
            "inferred_from_extraction_run",
            "page_number",
            "relative_position_within_image",
            "transcription_excerpt",
        ):
            assert key in manifest.properties, key

    def test_uncertainty_properties_present(self, manifest):
        """Uncertainty is structural (docs/MODELING.md)."""
        for key in ("earliest_date", "latest_date", "sourcing_circumstances"):
            assert key in manifest.properties, key
        assert "circa" in manifest.items

    def test_core_classes_present(self, manifest):
        for key in (
            "human",
            "place",
            "event",
            "artifact",
            "artifact_fragment",
            "extraction_run",
        ):
            assert key in manifest.items, key

    def test_organization_class_and_employer(self, manifest):
        """ADR-0012: organizations are agents, distinct from place."""
        assert "organization" in manifest.items
        assert manifest.items["organization"].subclass_of is None  # top-level
        assert manifest.properties["employer"].datatype == "wikibase-item"

    def test_open_schema_holding_class_and_type_hint(self, manifest):
        """ADR-0013: the escape hatch's holding class and hint property."""
        assert "unclassified_entity" in manifest.items
        assert manifest.properties["type_hint"].datatype == "string"

    def test_kinship_primitives_and_fallback(self, manifest):
        """ADR-0014: kinship is primitives + a generic `relative` fallback."""
        for key in ("father", "mother", "spouse", "sibling", "child"):
            assert manifest.properties[key].datatype == "wikibase-item", key
        assert manifest.properties["relative"].datatype == "wikibase-item"
        assert manifest.properties["kinship_type"].datatype == "string"
        # sex_or_gender + its values, the calculus prerequisite
        assert manifest.properties["sex_or_gender"].datatype == "wikibase-item"
        assert "male" in manifest.items and "female" in manifest.items

    def test_persecution_events_and_new_classes(self, manifest):
        """ADR-0014: persecution is events; recurring hints became classes."""
        for key in ("deportation", "imprisonment", "release"):
            assert manifest.items[key].subclass_of == "event", key
        for key in ("family", "vessel", "legal_instrument", "military_unit"):
            assert key in manifest.items, key

    def test_person_attribute_properties(self, manifest):
        """ADR-0014: attributes/affiliations backed by the gap log."""
        for key in ("country_of_citizenship", "educated_at", "member_of",
                    "gave_refuge_to"):  # fmt: skip
            assert manifest.properties[key].datatype == "wikibase-item", key
        assert manifest.properties["religion"].datatype == "monolingualtext"

    def test_entity_kinds_are_declared_here(self, manifest):
        """The kinds the extractor may type an entity as (ADR-0007) are
        the manifest's own flag, not a list restated in the prompt."""
        kinds = {k for k, spec in manifest.items.items() if spec.entity_kind}
        assert kinds == {
            "human",
            "place",
            "organization",
            "family",
            "vessel",
            "legal_instrument",
            "military_unit",
            "event",
            "birth",
            "death",
            "marriage",
            "migration",
            "military_service",
            "deportation",
            "imprisonment",
            "release",
            "occupation_class",
            "unclassified_entity",
        }

    def test_non_entity_items_are_not_offered_as_kinds(self, manifest):
        """Artifact types, vocabulary values, and infrastructure classes
        are real items the extractor must never type an entity as."""
        for key in (
            "photograph",
            "certificate",
            "circa",
            "male",
            "artifact",
            "artifact_fragment",
            "extraction_run",
            "model_manifest",
            "time_period_class",
        ):
            assert not manifest.items[key].entity_kind, key

    def test_every_entity_kind_has_a_description(self, manifest):
        """Descriptions are prompt surface now: an entity kind without
        one would reach the extractor as a bare key."""
        for key, spec in manifest.items.items():
            if spec.entity_kind:
                assert spec.description.strip(), key

    def test_schema_source_has_no_superclass(self, manifest):
        """The manifest item is a reference target, not part of the
        taxonomy."""
        assert manifest.items[manifest.schema_source_item].subclass_of is None

    def test_wikidata_ids_wellformed(self, manifest):
        # Format is enforced by pydantic; this documents the intent and
        # guards against the field being renamed without validation.
        for spec in manifest.properties.values():
            assert spec.wikidata is None or spec.wikidata.startswith("P")
        for spec in manifest.items.values():
            assert spec.wikidata is None or spec.wikidata.startswith("Q")


class TestValidators:
    def test_duplicate_property_label_rejected(self):
        raw = minimal_raw()
        raw["properties"]["dupe"] = {
            "label": "stated in",
            "description": "d",
            "datatype": "string",
        }
        with pytest.raises(ValidationError, match="duplicate label"):
            Manifest.model_validate(raw)

    def test_duplicate_item_label_rejected(self):
        raw = minimal_raw()
        raw["items"]["dupe"] = {"label": "artifact", "description": "d"}
        with pytest.raises(ValidationError, match="duplicate label"):
            Manifest.model_validate(raw)

    def test_unknown_subclass_target_rejected(self):
        raw = minimal_raw()
        raw["items"]["photograph"]["subclass_of"] = "no_such_key"
        with pytest.raises(ValidationError, match="subclass_of"):
            Manifest.model_validate(raw)

    def test_unknown_schema_source_rejected(self):
        raw = minimal_raw()
        raw["schema_source_item"] = "no_such_key"
        with pytest.raises(ValidationError, match="schema_source_item"):
            Manifest.model_validate(raw)

    def test_bad_wikidata_id_rejected(self):
        raw = minimal_raw()
        raw["items"]["artifact"]["wikidata"] = "P31"  # Q required for items
        with pytest.raises(ValidationError):
            Manifest.model_validate(raw)

    def test_bad_datatype_rejected(self):
        raw = minimal_raw()
        raw["properties"]["stated_in"]["datatype"] = "boolean"
        with pytest.raises(ValidationError):
            Manifest.model_validate(raw)

    def test_unknown_field_rejected(self):
        raw = minimal_raw()
        raw["properties"]["stated_in"]["datatpye"] = "string"  # typo
        with pytest.raises(ValidationError):
            Manifest.model_validate(raw)

    def test_entity_kind_defaults_off(self):
        """A new item is not an extraction kind until it says so: the
        prompt offers the flagged set, so the safe default is out."""
        manifest = Manifest.model_validate(minimal_raw())
        assert not any(spec.entity_kind for spec in manifest.items.values())

    def test_overlong_description_rejected(self):
        # Wikibase caps descriptions at 250 chars; catch it at load time
        # rather than mid-write against the instance.
        raw = minimal_raw()
        raw["properties"]["stated_in"]["description"] = "x" * 251
        with pytest.raises(ValidationError, match="at most 250"):
            Manifest.model_validate(raw)


class TestLoading:
    """load_manifest parses each distinct text once and hands every
    caller a manifest of its own (#254)."""

    def test_one_text_is_parsed_once_whatever_path_it_comes_from(
        self, tmp_path, monkeypatch
    ):
        # a comment no other load carries, so this text is new to the cache
        text = yaml.safe_dump(minimal_raw()) + f"# {uuid.uuid4().hex}\n"
        first, second = tmp_path / "first.yaml", tmp_path / "second.yaml"
        first.write_text(text)
        second.write_text(text)
        parses = []
        real_safe_load = yaml.safe_load

        def safe_load(stream):
            parses.append(stream)
            return real_safe_load(stream)

        monkeypatch.setattr(yaml, "safe_load", safe_load)
        loads = [load_manifest(first), load_manifest(second)]
        loads.append(load_manifest(first))
        assert len(parses) == 1
        assert loads[0] == loads[1] == loads[2]

    def test_a_manifest_rewritten_on_disk_is_read_again(self, tmp_path):
        path = tmp_path / "manifest.yaml"
        raw = minimal_raw()
        path.write_text(yaml.safe_dump(raw))
        assert load_manifest(path).items["artifact"].label == "artifact"
        before = path.stat()
        raw["items"]["artifact"]["label"] = "artefact"
        path.write_text(yaml.safe_dump(raw))
        # the same size and modification time: only the text has changed
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (
            before.st_size,
            before.st_mtime_ns,
        )
        assert load_manifest(path).items["artifact"].label == "artefact"

    def test_changing_a_loaded_manifest_changes_no_later_load(self):
        text = read_manifest_data()
        pristine = Manifest.model_validate(yaml.safe_load(text))
        first = load_manifest()
        first.version += 1
        first.items["human"].label = "changed"
        first.properties["stated_in"].aliases.append("changed")
        del first.items["place"]
        second = load_manifest()
        assert second is not first
        assert second == pristine


class TestMappingPage:
    def test_wikitext_contains_ids_and_links(self, manifest):
        property_ids = {
            key: f"P{n}" for n, key in enumerate(manifest.properties, 1)
        }
        item_ids = {key: f"Q{n}" for n, key in enumerate(manifest.items, 1)}
        text = build_mapping_wikitext(manifest, property_ids, item_ids)
        assert "do not edit by hand" in text
        assert "[[Property:P1|P1]]" in text
        assert "https://www.wikidata.org/wiki/Property:P31" in text
        # subclass column: photograph names its parent
        assert "photograph || artifact" in text
        # unmapped entries render a dash, not a broken link
        assert "|| —" in text
