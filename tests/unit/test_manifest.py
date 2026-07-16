"""Offline tests for the model manifest and bootstrap helpers (pytest)."""

import pytest
from pydantic import ValidationError

from yizkr.model.bootstrap import build_mapping_wikitext
from yizkr.model.schema import Manifest, load_manifest


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

    def test_schema_source_has_no_superclass(self, manifest):
        """The manifest item is a reference target, not part of the taxonomy."""
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


class TestMappingPage:
    def test_wikitext_contains_ids_and_links(self, manifest):
        property_ids = {key: f"P{n}" for n, key in enumerate(manifest.properties, 1)}
        item_ids = {key: f"Q{n}" for n, key in enumerate(manifest.items, 1)}
        text = build_mapping_wikitext(manifest, property_ids, item_ids)
        assert "do not edit by hand" in text
        assert "[[Property:P1|P1]]" in text
        assert "https://www.wikidata.org/wiki/Property:P31" in text
        # subclass column: photograph names its parent
        assert "photograph || artifact" in text
        # unmapped entries render a dash, not a broken link
        assert "|| —" in text
