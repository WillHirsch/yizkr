"""How adjudication names an artifact a claim is about (#667): its own
document as "this document", an image the document places by its place,
as the critic names it ("page 6, image 1"), from the transcription view
where the stage has one; and the stage's key and code digest cover both.

The fixtures are the fictional universe's (docs/EXAMPLES.md): *Our Family
from Germany* (`family-booklet-2011`) places the top of Sebastian's
letter to Klara on its page 6, under the caption "Wastl to Klara, July
1939."; a register names Sebastian Löbig with his birth date, so the
booklet's Wastl and the register's Sebastian are a pair to ask.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_adjudication import Engine, context_of, truths
from tests.unit.test_clustering import RUN, Doc, day, human, register
from tests.unit.test_container_claims import (
    BOOKLET_REF,
    CAPTION,
    DECK_REF,
    LETTER_REF,
    STRAY_REF,
    booklet,
    paged,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline import adjudication, critique
from yizkr.pipeline.adjudication import (
    Evidence,
    document_places,
    ensure_adjudicated,
)
from yizkr.pipeline.clustering import Manifest, cluster_corpus
from yizkr.pipeline.transcribe import DECK_ENGINE

SEBASTIAN = "sebastian-loebig"


def about(subject, prop: str, local_id: str) -> dict:
    """A claim of the booklet about `subject` (an artifact) naming the
    local id as its value, citing the caption on page 6."""
    return {
        "subject": subject.model_dump(),
        "property": prop,
        "value": {
            "type": "item",
            "value": {"kind": "local", "local_id": local_id},
        },
        "source": {
            "artifact": BOOKLET_REF.model_dump(),
            "page_number": "6",
            "transcription_excerpt": {"text": CAPTION, "language": "en"},
        },
    }


def the_booklet(*claims: dict) -> Doc:
    """The booklet's batch: Wastl, Sebastian Löbig by the caption, and
    the claims given."""
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "entities": [
                human(
                    "e1",
                    "Sebastian Löbig",
                    aliases=["Wastl"],
                )
            ],
            "claims": list(claims),
        }
    )
    return Doc(
        "family-booklet-2011",
        Manifest(BOOKLET_REF.sha1, batch),
        {"e1": SEBASTIAN},
    )


def the_register() -> Doc:
    return register(
        "a register",
        [human("e1", "Sebastian Löbig")],
        [("e1", "date_of_birth", day(1862, 2, 15))],
        {"e1": SEBASTIAN},
    )


def lines_of(doc: Doc, places=None) -> list[str]:
    return adjudication._Document(doc.manifest, places).lines("e1")


class TestNamed:
    def test_the_document_itself_is_this_document(self):
        doc = the_booklet(about(BOOKLET_REF, "author", "e1"))
        assert "named as the author of this document" in lines_of(doc)

    def test_a_placed_image_is_named_by_its_place(self):
        doc = the_booklet(
            about(LETTER_REF, "author", "e1"),
            about(LETTER_REF, "addressee", "e1"),
        )
        places = critique.artifact_places(booklet())
        found = lines_of(doc, places)
        assert "named as the author of page 6, image 1" in found
        assert "named as the addressee of page 6, image 1" in found
        assert not any("an entity" in line for line in found)

    def test_an_image_without_a_place_is_named_by_its_media(self):
        """Without the transcription's places, or for an image the
        document does not place, the artifact is named by the SHA-1 of
        its media, as the prompt names every document."""
        doc = the_booklet(
            about(LETTER_REF, "author", "e1"),
            about(STRAY_REF, "shows", "e1"),
        )
        unplaced = lines_of(doc)
        assert (
            f"named as the author of the artifact of media SHA-1 "
            f"{LETTER_REF.sha1}"
        ) in unplaced
        placed = lines_of(doc, critique.artifact_places(booklet()))
        assert "named as the author of page 6, image 1" in placed
        assert (
            f"named as the shows of the artifact of media SHA-1 "
            f"{STRAY_REF.sha1}"
        ) in placed

    def test_the_evidence_gives_each_document_its_own_places(self):
        doc = the_booklet(about(LETTER_REF, "author", "e1"))
        other = the_register()
        manifests = [doc.manifest, other.manifest]
        identity = cluster_corpus(manifests)
        places = {BOOKLET_REF.sha1: critique.artifact_places(booklet())}
        evidence = Evidence(manifests, identity["mentions"], places)
        fingerprint = doc.fingerprint("e1", identity)
        shown = "\n".join(evidence.mention(fingerprint, "A"))
        assert "named as the author of page 6, image 1" in shown
        # the register's document has none
        assert evidence._documents[other.sha1].places == {}


def write_view(directory: Path, transcription) -> Path:
    path = directory / "transcription.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcription.model_dump_json())
    return path


class TestDocumentPlaces:
    def test_the_places_of_each_view(self, tmp_path):
        view = write_view(tmp_path / "booklet", booklet())
        broken = tmp_path / "broken.json"
        broken.write_text("{")
        documents = [
            type("D", (), {"sha1": BOOKLET_REF.sha1, "transcription": view}),
            # no view, a missing one, and one that cannot be read
            type("D", (), {"sha1": "a" * 40}),
            type(
                "D",
                (),
                {"sha1": "b" * 40, "transcription": tmp_path / "none.json"},
            ),
            type("D", (), {"sha1": "c" * 40, "transcription": broken}),
        ]
        assert document_places(documents) == {
            BOOKLET_REF.sha1: {LETTER_REF.sha1: "page 6, image 1"}
        }

    def test_a_view_that_places_no_image_gives_none(self, tmp_path):
        view = write_view(tmp_path, paged(BOOKLET_REF, [(1, [], "x")]))
        documents = [
            type("D", (), {"sha1": BOOKLET_REF.sha1, "transcription": view})
        ]
        assert document_places(documents) == {}


def staged(tmp_path, transcription):
    """The stage's context over the booklet and the register, the
    booklet's transcription view beside its batch."""
    docs = [the_booklet(about(LETTER_REF, "author", "e1")), the_register()]
    engine = Engine(truths(docs))
    context = context_of(docs, tmp_path, engine)
    (booklet_doc,) = [
        d for d in context.documents if d.sha1 == BOOKLET_REF.sha1
    ]
    booklet_doc.transcription = write_view(
        booklet_doc.batch.parent, transcription
    )
    return context, engine


class TestStage:
    def test_the_prompt_names_the_image_by_its_place(self, tmp_path):
        context, engine = staged(tmp_path, booklet())
        adjudication.run_stage(context)
        (prompt,) = engine.prompts
        assert "named as the author of page 6, image 1" in prompt
        assert "an entity" not in prompt

    def test_a_view_that_places_the_image_elsewhere_is_asked_again(
        self, tmp_path
    ):
        context, engine = staged(tmp_path, booklet())
        adjudication.run_stage(context)
        assert ensure_adjudicated(context).reused
        (booklet_doc,) = [
            d for d in context.documents if d.sha1 == BOOKLET_REF.sha1
        ]
        write_view(
            booklet_doc.batch.parent,
            paged(BOOKLET_REF, [(7, [LETTER_REF.sha1], CAPTION)]),
        )
        output = ensure_adjudicated(context)
        assert not output.reused
        assert len(engine.prompts) == 2
        assert "named as the author of page 7, image 1" in engine.prompts[1]
        record = json.loads(
            (context.directory / adjudication.RECORD).read_text()
        )
        assert record["key"] == output.record["key"]

    def test_the_code_digest_covers_how_an_artifact_is_named(
        self, monkeypatch
    ):
        digest = adjudication.code_digest()
        monkeypatch.setattr(critique, "THIS_DOCUMENT", "the source")
        assert adjudication.code_digest() != digest
        monkeypatch.undo()

        def artifact_places(transcription):
            return {}

        monkeypatch.setattr(critique, "artifact_places", artifact_places)
        assert adjudication.code_digest() != digest


# a second image, the shop front, set on page 6 after the letter's
PHOTO_REF = STRAY_REF


class TestEdges:
    def test_own_document_is_this_document_with_places_given(self):
        doc = the_booklet(about(BOOKLET_REF, "author", "e1"))
        places = {BOOKLET_REF.sha1: "page 1, image 1"}
        assert "named as the author of this document" in lines_of(doc, places)

    def test_an_uppercase_document_sha1_is_its_own(self, tmp_path):
        """The manifest's and a view's document SHA-1 in capitals name
        the same bytes as the claim's lowercase one."""
        doc = the_booklet(about(BOOKLET_REF, "author", "e1"))
        upper = Manifest(BOOKLET_REF.sha1.upper(), doc.manifest.batch)
        lines = adjudication._Document(upper).lines("e1")
        assert "named as the author of this document" in lines
        view = write_view(tmp_path, booklet())
        documents = [
            type(
                "D",
                (),
                {"sha1": BOOKLET_REF.sha1.upper(), "transcription": view},
            )
        ]
        assert set(document_places(documents)) == {BOOKLET_REF.sha1}

    def test_each_image_and_each_view_keeps_its_own_place(self, tmp_path):
        two = paged(
            BOOKLET_REF,
            [(6, [LETTER_REF.sha1, PHOTO_REF.sha1], CAPTION)],
        )
        deck = paged(DECK_REF, [(2, [PHOTO_REF.sha1], "x")], DECK_ENGINE)
        documents = [
            type(
                "D",
                (),
                {
                    "sha1": BOOKLET_REF.sha1,
                    "transcription": write_view(tmp_path / "b", two),
                },
            ),
            type(
                "D",
                (),
                {
                    "sha1": DECK_REF.sha1,
                    "transcription": write_view(tmp_path / "d", deck),
                },
            ),
        ]
        places = document_places(documents)
        assert places == {
            BOOKLET_REF.sha1: {
                LETTER_REF.sha1: "page 6, image 1",
                PHOTO_REF.sha1: "page 6, image 2",
            },
            DECK_REF.sha1: {PHOTO_REF.sha1: "slide 2, image 1"},
        }
        doc = the_booklet(
            about(LETTER_REF, "author", "e1"),
            about(PHOTO_REF, "shows", "e1"),
        )
        found = lines_of(doc, places[BOOKLET_REF.sha1])
        assert "named as the author of page 6, image 1" in found
        assert "named as the shows of page 6, image 2" in found

    def test_a_mention_of_an_artifact_is_named_by_its_label(self):
        """A claim on the document's own mention of an artifact it only
        writes about (ADR-0039 §10.1(c), route (c4)) names that mention
        by its label and kind, as any local entity."""
        claim = about(LETTER_REF, "author", "e1")
        claim["subject"] = {
            "kind": "local",
            "local_id": "u1",
            "label": "death certificate",
            "instance_of": "unclassified_entity",
        }
        found = lines_of(the_booklet(claim))
        assert (
            "named as the author of 'death certificate' (unclassified_entity)"
        ) in found
