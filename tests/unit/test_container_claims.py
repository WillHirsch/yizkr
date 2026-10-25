"""Claims a container makes about the artifacts it places, and a source's
claims about itself (#456, ADR-0039).

A container's words about an image it places are one verbatim `caption`
statement on the image's item, its subject the image's artifact reference;
every structured attribute (who made a document, for whom, when, where,
what it is about, whom it shows) is the document's own claim about itself,
its subject the claim's own source. Validation and the writer each check
both rules; the run report counts what is given and why claims were
rejected; the non-privileged export leaves captions out.

The fixtures are the fictional universe's (docs/EXAMPLES.md): *Our Family
from Germany* (`family-booklet-2011`), which places the top of
Sebastian's letter to Klara (`letter-excerpt-1939`) on its page 6 under
the caption "Wastl to Klara, July 1939."; *Where the Lobigs came from*
(`library-talk-2018`), whose slide 2 sets a photograph from the family
album; and the studio portrait (`studio-photo-1890s`), whose reverse names
five people.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.fixtures.identity import write_mapped
from tests.unit.test_chunking import ScriptedLlm, ndjson
from tests.unit.test_pipeline import ITEMS, PROPERTIES, RUN, FakeWiki
from yizkr.backup import LEFT_OUT_PROPERTIES, filter_nonprivileged_export
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    placed_artifacts,
    reject_reasons,
    validate_against_manifest,
)
from yizkr.model.schema import Manifest, load_acceptance, load_manifest
from yizkr.pipeline.chunking import (
    ENTITY_ASSIGNMENT_BLOCK,
    LINE_ASSIGNMENT_BLOCK,
    ChunkedClaimExtractor,
    rejected_by_reason,
)
from yizkr.pipeline.extract import (
    DOCUMENT_ITSELF,
    ClaimFileExtractor,
    ExtractionFailed,
    LlmClaimExtractor,
    document_block,
    placed_by,
    placed_lines,
    run_metadata,
    salvage_valid_claims,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.transcribe import (
    DECK_ENGINE,
    PageProvenance,
    TranscribedPage,
    Transcription,
)
from yizkr.pipeline.writer import BatchWriter, subject_refusal

BOOKLET_REF = ArtifactRef.from_bytes(b"%PDF Our Family from Germany")
LETTER_REF = ArtifactRef.from_bytes(b"\x89PNG the top of the 1939 letter")
DECK_REF = ArtifactRef.from_bytes(b"PK Where the Lobigs came from")
ALBUM_REF = ArtifactRef.from_bytes(b"\x89PNG Wastl and Hilde, the album")
PORTRAIT_REF = ArtifactRef.from_bytes(b"\x89PNG the studio portrait")
# an image no container here places
STRAY_REF = ArtifactRef.from_bytes(b"\x89PNG the shop front")

CAPTION = "Wastl to Klara, July 1939."
LETTER_TEXT = (
    "Flörburg, Brunnenring 35, den 28. Juli 1939.\n"
    "Liebes Pünktchen! Gustl hat endlich sein Visum."
)
SHOWN = ("Muckel", "Cilli", "Wastl", "Rahel", "Felix")

# the seven document-subject properties and the one placed-subject one
DOCUMENT_SUBJECTS = {
    "issued_by",
    "author",
    "addressee",
    "inception",
    "location_of_creation",
    "main_subject",
    "shows",
}


def run() -> dict:
    return run_metadata("recorded").model_dump(mode="json")


def paged(
    artifact: ArtifactRef,
    pages: list[tuple[int, list[str], str]],
    engine: str = "pdf-pages:test",
) -> Transcription:
    """A container read page by page: each page's number, the SHA-1s of the
    images it places and its own text, behind its markers."""
    unit = "Slide" if engine == DECK_ENGINE else "Page"
    text, transcribed = "", []
    for number, images, body in pages:
        marks = [f"[{unit} {number}]"] + [
            f"[{unit} {number}, image {place}: sha1:{sha1}]"
            for place, sha1 in enumerate(images, start=1)
        ]
        opening = ("\n\n" if text else "") + "\n".join(marks) + "\n"
        start = len(text) + len(opening)
        text += opening + body
        transcribed.append(
            TranscribedPage(
                page=number,
                start=start,
                end=len(text),
                images=images,
                status="transcribed",
                provenance=PageProvenance(method="text-layer", engine="t"),
            )
        )
    return Transcription(
        artifact=artifact, text=text, engine=engine, pages=transcribed
    )


def booklet() -> Transcription:
    return paged(
        BOOKLET_REF,
        [
            (1, [], "Our Family from Germany. Compiled by Nicholas Lobig."),
            (6, [LETTER_REF.sha1], CAPTION + "\nTranslation: Flörburg, ..."),
        ],
    )


def letter() -> Transcription:
    """The excerpt's own transcription: the image's text alone."""
    return Transcription(
        artifact=LETTER_REF, text=LETTER_TEXT, language="de", engine="t"
    )


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def caption(
    subject: dict | None = None,
    source: ArtifactRef = BOOKLET_REF,
    text: str = CAPTION,
    page: str = "6",
) -> dict:
    """The booklet's caption over the letter's image, as a claim."""
    return {
        "subject": subject or LETTER_REF.model_dump(),
        "property": "caption",
        "value": {
            "type": "monolingual",
            "value": {"text": text, "language": "en"},
        },
        "source": {
            "artifact": source.model_dump(),
            "page_number": page,
            "transcription_excerpt": {"text": text, "language": "en"},
        },
    }


def itself(
    prop: str,
    value: dict,
    source: ArtifactRef = LETTER_REF,
    subject: dict | None = None,
    excerpt: str = "den 28. Juli 1939",
) -> dict:
    """A claim a document gives about itself."""
    return {
        "subject": subject or source.model_dump(),
        "property": prop,
        "value": value,
        "source": {
            "artifact": source.model_dump(),
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        },
    }


JULY_28 = {"type": "time", "year": 1939, "month": 7, "day": 28}


def checked(*claims: dict, placed=None) -> list[str]:
    batch = ClaimBatch.model_validate(
        {"extraction_run": run(), "claims": list(claims)}
    )
    return validate_against_manifest(batch, placed=placed)


PLACED = {BOOKLET_REF.sha1: {LETTER_REF.sha1}}


# -- the manifest -------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


class TestTheManifest:
    @pytest.mark.parametrize(
        ("key", "datatype", "wikidata", "accepts"),
        [
            (
                "author",
                "wikibase-item",
                "P50",
                ["human", "organization", "family", "unclassified_entity"],
            ),
            (
                "addressee",
                "wikibase-item",
                "P1817",
                ["human", "organization", "family", "unclassified_entity"],
            ),
            ("inception", "time", "P571", None),
            (
                "location_of_creation",
                "wikibase-item",
                "P1071",
                ["place", "unclassified_entity"],
            ),
            ("main_subject", "wikibase-item", "P921", "any"),
            # P180 is depicts: an inscription is never a confirmed depiction
            ("shows", "wikibase-item", None, "any"),
        ],
    )
    def test_a_document_s_own_attributes(
        self, manifest, key, datatype, wikidata, accepts
    ):
        spec = manifest.properties[key]
        assert (spec.datatype, spec.wikidata, spec.accepts) == (
            datatype,
            wikidata,
            accepts,
        )
        assert spec.document_subject and not spec.placed_subject
        assert spec.in_prompts

    def test_the_document_subjects_are_the_seven(self):
        acceptance = load_acceptance()
        assert acceptance.document_subjects == DOCUMENT_SUBJECTS
        assert acceptance.placed_subjects == {"caption"}

    def test_caption_is_a_container_s_monolingual_words(self, manifest):
        spec = manifest.properties["caption"]
        assert (spec.datatype, spec.wikidata) == ("monolingualtext", "P2096")
        assert spec.placed_subject and not spec.document_subject
        assert spec.in_prompts

    def test_depicts_stays_the_faces_flow_s(self, manifest):
        depicts = manifest.properties["depicts"]
        assert depicts.wikidata == "P180"
        assert not depicts.document_subject and not depicts.placed_subject
        mapped = [
            key
            for key, spec in manifest.properties.items()
            if spec.wikidata == "P180"
        ]
        assert mapped == ["depicts"]

    def test_a_curator_s_combination_is_marked(self, manifest):
        method = manifest.properties["determination_method"]
        assert (method.datatype, method.wikidata) == ("wikibase-item", "P459")
        assert method.accepts == ["combined_from_sources"]
        # the pipeline never combines two sources' values (ADR-0039 §4)
        assert not method.in_prompts
        combined = manifest.items["combined_from_sources"]
        assert not combined.entity_kind and not combined.entity_class

    def test_the_prompts_offer_what_a_source_gives(self):
        properties, _ = vocabulary_prompt_lines()
        keys = {line.split(":")[0].strip() for line in properties.splitlines()}
        assert DOCUMENT_SUBJECTS | {"caption"} <= keys
        assert "determination_method" not in keys

    def test_no_property_is_marked_both_ways(self, manifest):
        data = manifest.model_dump()
        data["properties"]["caption"]["document_subject"] = True
        with pytest.raises(ValidationError, match="both document_subject"):
            Manifest.model_validate(data)


# -- validation ---------------------------------------------------------------


class TestValidation:
    def test_a_caption_names_an_image_its_source_places(self):
        assert checked(caption(), placed=PLACED) == []

    @pytest.mark.parametrize(
        ("claim", "placed"),
        [
            # an image the booklet does not place
            (caption(STRAY_REF.model_dump()), PLACED),
            # the booklet itself, which places no copy of itself
            (caption(BOOKLET_REF.model_dump()), PLACED),
            # a container that places nothing
            (caption(), {BOOKLET_REF.sha1: set()}),
            # nothing said of what the source places: it places none
            (caption(), None),
            # an image another container places is not this one's
            (caption(), {DECK_REF.sha1: {LETTER_REF.sha1}}),
            # a person is no artifact
            (caption(local("e1", "Wastl", "human")), PLACED),
        ],
    )
    def test_a_caption_of_anything_else_is_refused(self, claim, placed):
        (problem,) = checked(claim, placed=placed)
        assert problem.endswith("[placed_subject]")
        assert "ADR-0039 §2.1" in problem

    @pytest.mark.parametrize(
        ("prop", "value"),
        [
            ("inception", JULY_28),
            ("location_of_creation", {"type": "item", "value": {}}),
            ("addressee", {"type": "item", "value": {}}),
            ("author", {"type": "item", "value": {}}),
            ("main_subject", {"type": "item", "value": {}}),
            ("shows", {"type": "item", "value": {}}),
        ],
    )
    def test_a_document_states_its_own_attributes(self, prop, value):
        if value["type"] == "item":
            value = {
                "type": "item",
                "value": local("e1", "Pünktchen", "human")
                if prop != "location_of_creation"
                else local("e1", "Brunnenring 35", "place"),
            }
        assert checked(itself(prop, value)) == []

    def test_the_portrait_shows_five_one_claim_each(self):
        claims = [
            itself(
                "shows",
                {"type": "item", "value": local(f"e{n}", name, "human")},
                source=PORTRAIT_REF,
                excerpt=name,
            )
            for n, name in enumerate(SHOWN, start=1)
        ]
        assert checked(*claims) == []

    @pytest.mark.parametrize(
        "claim",
        [
            # the booklet dates no image it does not place (#646)
            itself(
                "inception",
                {"type": "time", "year": 1939, "month": 7},
                source=BOOKLET_REF,
                subject=STRAY_REF.model_dump(),
            ),
            # nor names the writer of one
            itself(
                "author",
                {
                    "type": "item",
                    "value": local("e1", "Sebastian Löbig", "human"),
                },
                source=BOOKLET_REF,
                subject=STRAY_REF.model_dump(),
            ),
            # a document's own attribute is never a person's
            itself(
                "inception",
                JULY_28,
                subject=local("e1", "Sebastian Löbig", "human"),
            ),
        ],
    )
    def test_no_one_states_another_s_attributes(self, claim):
        (problem,) = checked(claim, placed=PLACED)
        assert problem.endswith("[document_subject]")

    def test_no_other_property_takes_a_placed_image(self):
        dated = itself(
            "point_in_time",
            {"type": "time", "year": 1939},
            source=BOOKLET_REF,
            subject=LETTER_REF.model_dump(),
        )
        (problem,) = checked(dated, placed=PLACED)
        assert problem.endswith("[artifact_subject]")

    def test_the_reasons_name_each_rule_once(self):
        assert reject_reasons(
            [
                "x: property 'caption' takes ... [placed_subject]",
                "y: unknown property key 'captoin'",
                "z: ... [placed_subject]",
            ]
        ) == ["placed_subject", "manifest"]

    def test_salvage_counts_each_reject_by_its_reasons(self):
        batch, rejects = salvage_valid_claims(
            [
                caption(),
                caption(STRAY_REF.model_dump()),
                {"subject": "not a claim"},
            ],
            run(),
            placed=PLACED,
        )
        assert [c.property for c in batch.claims] == ["caption"]
        assert [r["reasons"] for r in rejects] == [
            ["placed_subject"],
            ["schema"],
        ]
        assert rejected_by_reason(rejects) == {
            "placed_subject": 1,
            "schema": 1,
        }

    def test_the_placed_artifacts_are_the_pages_images(self):
        assert placed_artifacts(booklet().pages) == {LETTER_REF.sha1}
        assert placed_artifacts(None) == frozenset()
        assert placed_by(booklet(), BOOKLET_REF) == PLACED
        assert placed_by(letter(), LETTER_REF) == {LETTER_REF.sha1: set()}


# -- the prompts --------------------------------------------------------------


def flat(text: str) -> str:
    return " ".join(text.split())


class TestThePrompts:
    def test_a_container_lists_the_images_it_places(self):
        assert placed_lines(booklet()) == [
            f'  {{"kind": "artifact", "sha1": "{LETTER_REF.sha1}"}}: '
            "page 6, image 1"
        ]
        block = flat(document_block(booklet(), BOOKLET_REF))
        assert "as ONE `caption` claim" in block
        assert "its value the passage exactly as written" in block
        # beside the caption, what the passage conveys of the image (#646)
        assert "conveys what a listed image is, give that too" in block
        # named by its bytes, never by a QID
        assert LETTER_REF.sha1 in block and "Q" not in block.split("sha1")[0]

    def test_a_deck_s_slides_place_its_images(self):
        deck = paged(
            DECK_REF,
            [(2, [ALBUM_REF.sha1], "Wastl and Hilde")],
            engine=DECK_ENGINE,
        )
        assert placed_lines(deck) == [
            f'  {{"kind": "artifact", "sha1": "{ALBUM_REF.sha1}"}}: '
            "slide 2, image 1"
        ]
        # a slide's title over its one image is read with a note
        assert "a judgement note of category `relation`" in flat(
            document_block(deck, DECK_REF)
        )

    def test_a_document_that_places_nothing_gives_no_caption(self):
        block = flat(document_block(letter(), LETTER_REF))
        assert "places no image read on its own: give no `caption`" in block
        assert "as ONE `caption` claim" not in block

    def test_the_document_itself_names_every_document_subject(self):
        paragraph = DOCUMENT_ITSELF.format(artifact_sha1=LETTER_REF.sha1)
        for key in load_acceptance().document_subjects:
            assert f"- `{key}`:" in paragraph, key
        assert "never `depicts`" in paragraph
        assert f'"sha1": "{LETTER_REF.sha1}"' in paragraph

    def test_the_chunks_own_what_a_document_says_of_itself(self):
        lines = flat(LINE_ASSIGNMENT_BLOCK)
        assert (
            "A claim about the document itself, a claim about an image it "
            "places (a `caption`, or one of the properties on what a "
            "document is), and a claim about a document it mentions belong "
            "to the unit whose lines hold the passage that states or "
            "conveys it"
        ) in lines
        entities = flat(
            ENTITY_ASSIGNMENT_BLOCK.format(
                roster_block="", entities_block="", first_entity="e1"
            )
        )
        assert (
            "and every `caption` claim) belongs to the chunk assigned e1, "
            "the first entity assigned for extraction, whatever its value"
        ) in entities

    def test_the_owner_chunk_is_one_assigned_for_extraction(self, tmp_path):
        """A registrar the manifest marks extract: false is assigned to no
        chunk, so the claims about the document itself and its captions
        go to the first entity that is: each chunk prompt names it."""
        registrar = {
            "id": "e1",
            "label": "Standesamt Flörburg",
            "kind": "organization",
            "mention_lines": [2],
            "extract": False,
        }
        wastl = {
            "id": "e2",
            "label": "Wastl",
            "kind": "human",
            "mention_lines": [4],
        }
        klara = {
            "id": "e3",
            "label": "Klara",
            "kind": "human",
            "mention_lines": [4],
        }
        llm = ScriptedLlm(
            [
                json.dumps({"entities": [registrar, wastl, klara]}),
                ndjson([]),
                ndjson([]),
            ]
        )
        ChunkedClaimExtractor(llm, state_dir=tmp_path / "state").extract(
            booklet(), BOOKLET_REF
        )
        prompts = [flat(prompt) for _method, prompt in llm.calls[1:]]
        assert prompts
        for prompt in prompts:
            assert "belongs to the chunk assigned e2, the first entity" in (
                prompt
            )
            assert "assigned e1," not in prompt
            assert "assigned e3," not in prompt

    def test_a_container_s_extraction_keeps_its_captions(self, tmp_path):
        llm = ScriptedLlm(
            [ndjson([caption(), caption(STRAY_REF.model_dump())])]
        )
        rejects = tmp_path / "rejects.json"
        batch = LlmClaimExtractor(llm, rejects_dump=rejects).extract(
            booklet(), BOOKLET_REF
        )
        ((_, prompt),) = llm.calls
        assert LETTER_REF.sha1 in prompt
        assert [(c.property, c.subject) for c in batch.claims] == [
            ("caption", LETTER_REF)
        ]
        (reject,) = json.loads(rejects.read_text())
        assert reject["reasons"] == ["placed_subject"]

    def test_a_claims_file_is_validated_with_what_is_placed(self, tmp_path):
        """A prepared claims file read against the booklet's transcription
        keeps its caption of the image the booklet places, and fails on one
        of an image it does not place."""
        kept = tmp_path / "kept.json"
        kept.write_text(json.dumps({"claims": [caption()]}))
        batch = ClaimFileExtractor(kept).extract(booklet(), BOOKLET_REF)
        assert [(c.property, c.subject) for c in batch.claims] == [
            ("caption", LETTER_REF)
        ]
        stray = tmp_path / "stray.json"
        stray.write_text(
            json.dumps({"claims": [caption(STRAY_REF.model_dump())]})
        )
        with pytest.raises(ExtractionFailed, match=r"\[placed_subject\]"):
            ClaimFileExtractor(stray).extract(booklet(), BOOKLET_REF)

    def test_a_chunked_extraction_keeps_its_captions(self, tmp_path):
        """The chunked extractor lists the placed images in its chunk
        prompts and validates with them; a caption of an image the booklet
        does not place is a reject counted by its reason."""
        wastl = {
            "id": "e1",
            "label": "Wastl",
            "kind": "human",
            "mention_lines": [4],
        }
        llm = ScriptedLlm(
            [
                json.dumps({"entities": [wastl]}),
                ndjson([caption(), caption(STRAY_REF.model_dump())]),
            ]
        )
        batch = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state"
        ).extract(booklet(), BOOKLET_REF)
        prompts = [prompt for _method, prompt in llm.calls[1:]]
        assert prompts and all(LETTER_REF.sha1 in p for p in prompts)
        assert [(c.property, c.subject) for c in batch.claims] == [
            ("caption", LETTER_REF)
        ]
        coverage = json.loads(
            (tmp_path / "state" / "coverage.json").read_text()
        )
        assert coverage["rejected_by_reason"] == {"placed_subject": 1}

    def test_the_excerpt_s_extraction_never_reads_the_caption(self):
        """The excerpt's prompt holds the excerpt's text alone: neither the
        container's caption nor its key (ADR-0039 §5)."""
        llm = ScriptedLlm([ndjson([itself("inception", JULY_28)])])
        batch = LlmClaimExtractor(llm).extract(letter(), LETTER_REF)
        ((_, prompt),) = llm.calls
        assert CAPTION not in prompt and "Wastl" not in prompt
        assert BOOKLET_REF.sha1 not in prompt
        assert "places no image read on its own" in prompt
        (dated,) = batch.claims
        assert (dated.property, dated.subject) == ("inception", LETTER_REF)


# -- the writer ---------------------------------------------------------------


class CaptionWiki(FakeWiki):
    """The unit fake, with the properties of a container's caption and a
    document's own date in its vocabulary."""

    def vocabulary(self):
        return dict(PROPERTIES) | {"caption": "P91", "inception": "P92"}, dict(
            ITEMS
        )


def artifacts(wiki, *refs: ArtifactRef) -> dict[str, str]:
    qids = {}
    for ref in refs:
        artifact = wiki.wbi.item.new()
        artifact.write()
        wiki.media[ref.sha1] = artifact.id
        qids[ref.sha1] = artifact.id
    return qids


def batch_of(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


class TestTheWriter:
    def test_a_caption_is_written_on_the_image_s_item(self):
        wiki = CaptionWiki()
        qids = artifacts(wiki, BOOKLET_REF, LETTER_REF)
        result = write_mapped(
            BatchWriter(wiki), batch_of(caption()), placed=PLACED
        )
        assert (result.written, result.refused) == (1, [])
        (statement,) = wiki.wbi.item.get(qids[LETTER_REF.sha1]).claims.get(
            "P91"
        )
        assert statement.mainsnak.datavalue["value"] == {
            "text": CAPTION,
            "language": "en",
        }
        blocks = statement.references.references

        def referenced(key: str) -> list:
            return [
                snak.datavalue["value"]
                for block in blocks
                for snak in block.snaks.get(PROPERTIES[key]) or []
            ]

        # stated in the booklet, with its page, beside the run
        assert [v["id"] for v in referenced("stated_in")] == [
            qids[BOOKLET_REF.sha1]
        ]
        assert referenced("page_number") == ["6"]
        assert [
            v["id"] for v in referenced("inferred_from_extraction_run")
        ] == [result.run_qid]
        assert not wiki.wbi.item.get(qids[BOOKLET_REF.sha1]).claims.get("P91")

    @pytest.mark.parametrize(
        ("claim", "placed", "registered", "why"),
        [
            (
                caption(),
                None,
                (BOOKLET_REF, LETTER_REF),
                "its subject is no artifact given to this write as "
                "placed by its source",
            ),
            (
                caption(STRAY_REF.model_dump()),
                PLACED,
                (BOOKLET_REF, LETTER_REF, STRAY_REF),
                "its subject is no artifact given to this write as "
                "placed by its source",
            ),
            (
                caption(),
                PLACED,
                (BOOKLET_REF,),
                "no item carries its subject's media SHA-1",
            ),
            # an image another container placed is not this source's
            (
                caption(),
                {DECK_REF.sha1: {LETTER_REF.sha1}},
                (BOOKLET_REF, LETTER_REF),
                "its subject is no artifact given to this write as "
                "placed by its source",
            ),
            (
                caption(local("e1", "Wastl", "human")),
                PLACED,
                (BOOKLET_REF, LETTER_REF),
                "its property takes only an artifact its source places",
            ),
            # an image the booklet does not place (#646)
            (
                itself(
                    "inception",
                    JULY_28,
                    source=BOOKLET_REF,
                    subject=STRAY_REF.model_dump(),
                ),
                PLACED,
                (BOOKLET_REF, LETTER_REF, STRAY_REF),
                "its subject is neither its own source nor an artifact "
                "given to this write as placed by its source",
            ),
        ],
    )
    def test_it_refuses_what_the_subject_rules_forbid(
        self, claim, placed, registered, why
    ):
        """The writer holds both rules itself, as a batch handed to `yizkr
        write` is never validated against the manifest: the claim is
        refused and counted, and nothing lands on any item."""
        wiki = CaptionWiki()
        qids = artifacts(wiki, *registered)
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [claim],
                "entities": [{"id": "e1", "label": "Wastl", "kind": "human"}],
            }
        )
        result = write_mapped(BatchWriter(wiki), batch, placed=placed)
        (only,) = batch.claims
        assert result.refused == [claim_id(only)]
        assert result.written == 0 and result.created_items == {}
        assert any(why in warning for warning in result.warnings)
        for qid in qids.values():
            for pid in ("P91", "P92"):
                assert not wiki.wbi.item.get(qid).claims.get(pid)

    def test_a_document_s_date_is_written_on_its_own_item(self):
        wiki = CaptionWiki()
        qids = artifacts(wiki, LETTER_REF)
        result = write_mapped(
            BatchWriter(wiki), batch_of(itself("inception", JULY_28))
        )
        assert result.written == 1
        (statement,) = wiki.wbi.item.get(qids[LETTER_REF.sha1]).claims.get(
            "P92"
        )
        assert statement.mainsnak.datavalue["value"]["time"] == (
            "+1939-07-28T00:00:00Z"
        )

    def test_the_rule_reads_the_registered_media_by_source(self):
        claim = batch_of(caption()).claims[0]
        assert subject_refusal(claim, PLACED, {LETTER_REF.sha1}) is None
        reason, rule = subject_refusal(claim, PLACED, ())
        assert rule == "ADR-0039 §2.1"
        assert "no item carries" in reason


# -- the non-privileged export ------------------------------------------------


W = "http://w.lan"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
REF_CAPTION = "e1" * 20  # the caption's reference, which only it uses
REF_SHARED = "f2" * 20  # a reference the image's own statement uses too


class TestTheExport:
    LINES = [
        # the image's caption, its truthy triple and its references
        f"<{W}/entity/Q10> <{W}/prop/P91> <{W}/entity/statement/Q10-cap> .",
        f'<{W}/entity/statement/Q10-cap> <{W}/prop/statement/P91> "Wastl to Klara, July 1939."@en .',  # noqa: E501
        f'<{W}/entity/Q10> <{W}/prop/direct/P91> "Wastl to Klara, July 1939."@en .',  # noqa: E501
        f"<{W}/entity/statement/Q10-cap> <http://wikiba.se/ontology#rank> <http://wikiba.se/ontology#NormalRank> .",  # noqa: E501
        f"<{W}/entity/statement/Q10-cap> {DERIVED_FROM} <{W}/reference/{REF_CAPTION}> .",  # noqa: E501
        f'<{W}/reference/{REF_CAPTION}> <{W}/prop/reference/P4> "Wastl to Klara, July 1939."@en .',  # noqa: E501
        f"<{W}/entity/statement/Q10-cap> {DERIVED_FROM} <{W}/reference/{REF_SHARED}> .",  # noqa: E501
        # the image's own date, which stays, with the reference it shares
        f"<{W}/entity/Q10> <{W}/prop/P92> <{W}/entity/statement/Q10-inc> .",
        f'<{W}/entity/statement/Q10-inc> <{W}/prop/statement/P92> "+1939-07-28T00:00:00Z" .',  # noqa: E501
        f'<{W}/entity/Q10> <{W}/prop/direct/P92> "+1939-07-28T00:00:00Z" .',
        f"<{W}/entity/statement/Q10-inc> {DERIVED_FROM} <{W}/reference/{REF_SHARED}> .",  # noqa: E501
        f"<{W}/reference/{REF_SHARED}> <{W}/prop/reference/P5> <{W}/entity/Q2> .",  # noqa: E501
    ]

    def filtered(self, tmp_path: Path, left_out_pids) -> str:
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(self.LINES) + "\n")
        filter_nonprivileged_export(
            full, public, set(), left_out_pids=left_out_pids
        )
        with gzip.open(public, "rt", encoding="utf-8") as handle:
            return handle.read()

    def test_a_caption_goes_whole(self, tmp_path):
        content = self.filtered(tmp_path, {"P91"})
        assert "Wastl" not in content
        assert "Q10-cap" not in content and REF_CAPTION not in content
        assert "P91" not in content
        # the image's own statement, and the reference it shares, stay
        assert "+1939-07-28T00:00:00Z" in content
        assert f"<{W}/entity/statement/Q10-inc>" in content
        assert REF_SHARED in content

    def test_without_the_rule_the_caption_would_leave(self, tmp_path):
        assert "Wastl" in self.filtered(tmp_path, frozenset())

    def test_the_export_leaves_captions_out(self):
        assert LEFT_OUT_PROPERTIES == ("caption",)
