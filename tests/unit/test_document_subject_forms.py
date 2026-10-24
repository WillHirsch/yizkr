"""What a source's passage conveys about another artifact (#646, ADR-0039
§10): a document-subject claim about the source itself, an artifact it
places, the artifact its structured record makes, or its mention of an
artifact it only writes about.

Validation and the writer take those four subjects on a document-subject
property, and a claim whose subject is not its own source must carry its
passage as an excerpt (Q8). The extraction stage refuses an artifact kind
wherever model output first names one (Q5). The prompts ask for these
claims where the passage, read where it stands, conveys the fact (Q7); the
critic names the source as "this document" and a placed image by its
place; the run report splits the claims by subject form; the
non-privileged export leaves out a container's claims about what it places
(Q9).

The fixtures are the fictional universe's (docs/EXAMPLES.md): *Our Family
from Germany* (`family-booklet-2011`) places the top of Sebastian's letter
to Klara (`letter-excerpt-1939`) on its page 6, under the caption "Wastl
to Klara, July 1939." and beside its translation "Flörburg, Brunnenring
35, 28 July 1939. Dear Pünktchen!"; the 1958 letter to Leopold
(`letter-to-leopold-1958`) encloses Rudolf's death certificate.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from tests.fixtures.identity import write_mapped
from tests.unit.test_chunking import ScriptedLlm, ndjson
from tests.unit.test_container_claims import (
    BOOKLET_REF,
    CAPTION,
    DECK_REF,
    JULY_28,
    LETTER_REF,
    PLACED,
    PORTRAIT_REF,
    STRAY_REF,
    booklet,
    flat,
    local,
    run,
)
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki
from yizkr.backup import (
    PlacedSubjectRule,
    filter_nonprivileged_export,
    placed_subject_rule,
)
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    artifact_kind_problems,
    document_subject_form,
    unplaced_claims,
    unplaced_hint,
    validate_against_manifest,
)
from yizkr.model.schema import load_acceptance
from yizkr.pipeline import critique
from yizkr.pipeline.chunking import (
    ENTITY_ASSIGNMENT_BLOCK,
    LINE_ASSIGNMENT_BLOCK,
    ChunkedClaimExtractor,
    rejected_by_reason,
)
from yizkr.pipeline.extract import (
    DOCUMENT_ITSELF,
    PLACED_ARTIFACTS,
    ClaimFileExtractor,
    document_block,
    salvage_valid_claims,
)
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter, pre_plan_refusals

# the 1958 letter to Leopold, which encloses Rudolf's death certificate
LETTER_1958_REF = ArtifactRef.from_bytes(b"\x89PNG the 1958 letter")
ENCLOSING = "Вы просили прислать свидетельство о его смерти, посылаю."
TRANSLATION = "Flörburg, Brunnenring 35, 28 July 1939"
GREETING = "Dear Pünktchen!"


def claim(
    prop: str,
    value: dict,
    subject: dict,
    source: ArtifactRef = BOOKLET_REF,
    excerpt: str | None = TRANSLATION,
    page: str | None = "6",
    language: str = "en",
) -> dict:
    """A claim of `source` about `subject`, citing `excerpt` on `page`."""
    cited: dict = {"artifact": source.model_dump()}
    if page is not None:
        cited["page_number"] = page
    if excerpt is not None:
        cited["transcription_excerpt"] = {
            "text": excerpt,
            "language": language,
        }
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": cited,
    }


def item(local_id: str, label: str, kind: str) -> dict:
    return {"type": "item", "value": local(local_id, label, kind)}


LETTER = LETTER_REF.model_dump()
PUENKTCHEN = {"id": "e1", "label": "Pünktchen", "kind": "human"}
ADDRESS = {"id": "e2", "label": "Brunnenring 35", "kind": "place"}
WASTL = {"id": "e3", "label": "Wastl", "kind": "human"}
KLARA = {"id": "e4", "label": "Klara", "kind": "human"}
CERTIFICATE = {
    "id": "u1",
    "label": "death certificate",
    "kind": "unclassified_entity",
    "type_hint": "death certificate",
}
DEATH = {"id": "d1", "label": "death of Rudolf Löbig", "kind": "death"}


def translation() -> list[dict]:
    """The booklet's translation of the letter's top, as claims about the
    letter's image (ADR-0039 §10.2)."""
    return [
        claim("inception", JULY_28, LETTER),
        claim(
            "location_of_creation",
            item("e2", "Brunnenring 35", "place"),
            LETTER,
        ),
        claim(
            "addressee",
            item("e1", "Pünktchen", "human"),
            LETTER,
            excerpt=GREETING,
        ),
    ]


def caption_claims() -> list[dict]:
    """What the caption "Wastl to Klara, July 1939." conveys of the
    letter's image: its writer and its addressee, and no date (Q7)."""
    return [
        claim("author", item("e3", "Wastl", "human"), LETTER, excerpt=CAPTION),
        claim(
            "addressee", item("e4", "Klara", "human"), LETTER, excerpt=CAPTION
        ),
    ]


def enclosure() -> dict:
    """The 1958 letter's claim about the death certificate it encloses, on
    its own mention of it (ADR-0039 §10.1(c), route (c4))."""
    return claim(
        "main_subject",
        item("d1", "death of Rudolf Löbig", "death"),
        local("u1", "death certificate", "unclassified_entity"),
        source=LETTER_1958_REF,
        excerpt=ENCLOSING,
        page=None,
        language="ru",
    )


def batch(*claims: dict, entities=(), model: str | None = None):
    data = {"extraction_run": run(), "claims": list(claims)}
    if model is not None:
        data["extraction_run"] = {**run(), "model_identifier": model}
    data["entities"] = list(entities)
    return ClaimBatch.model_validate(data)


def checked(*claims: dict, placed=None, entities=()) -> list[str]:
    return validate_against_manifest(
        batch(*claims, entities=entities), placed=placed
    )


# -- the subject forms --------------------------------------------------------


class TestTheSubjectForms:
    def test_the_booklet_s_translation_dates_the_image_it_places(self):
        """The translation gives inception, place and addressee on the
        letter image's item, cited to the booklet's page 6 with the
        passage as its excerpt (ADR-0039 §10.2)."""
        assert checked(*translation(), placed=PLACED) == []

    def test_the_caption_gives_writer_and_addressee(self):
        assert checked(*caption_claims(), placed=PLACED) == []

    def test_each_form_is_named(self):
        acceptance = load_acceptance()
        forms = [
            document_subject_form(batch(c).claims[0], acceptance, PLACED, kind)
            for c, kind in (
                (claim("inception", JULY_28, BOOKLET_REF.model_dump()), None),
                (translation()[0], None),
                (
                    claim(
                        "inception", JULY_28, local("m1", "O1", "photograph")
                    ),
                    "photograph",
                ),
                (enclosure(), "unclassified_entity"),
                (claim("inception", JULY_28, STRAY_REF.model_dump()), None),
                (
                    claim("inception", JULY_28, local("p1", "Wastl", "human")),
                    "human",
                ),
            )
        ]
        assert forms == ["source", "placed", "record", "mention", None, None]

    def test_an_image_the_booklet_does_not_place_is_refused(self):
        """A placed-subject claim on an image the booklet's record does not
        name is rejected in validation and refused by the writer."""
        stray = claim("inception", JULY_28, STRAY_REF.model_dump())
        (problem,) = checked(stray, placed=PLACED)
        assert problem.endswith("[document_subject]")
        refusals = pre_plan_refusals(
            batch(stray), {}, PLACED, {STRAY_REF.sha1}
        )
        assert [reason for reason, _ in refusals.values()] == ["subject_rule"]
        (_, warning) = refusals[0]
        assert "neither its own source nor an artifact" in warning

    def test_without_the_booklet_s_placed_media_each_is_refused(self):
        for given in (None, {BOOKLET_REF.sha1: set()}):
            problems = checked(*translation(), placed=given)
            assert len(problems) == 3
            assert all(p.endswith("[document_subject]") for p in problems)

    def test_an_image_no_item_carries_is_refused_by_the_writer(self):
        refusals = pre_plan_refusals(batch(*translation()), {}, PLACED, ())
        assert len(refusals) == 3
        assert all(
            "no item carries its subject's media SHA-1" in warning
            for _, warning in refusals.values()
        )

    def test_the_letter_s_mention_of_its_enclosure_takes_the_claim(self):
        """(c4): the letter's `main_subject` of the death certificate it
        encloses stands on its own mention item, an unclassified entity
        with the type hint "death certificate"."""
        assert checked(enclosure(), entities=[CERTIFICATE, DEATH]) == []
        refusals = pre_plan_refusals(
            batch(enclosure(), entities=[CERTIFICATE, DEATH]), {}
        )
        assert refusals == {}

    def test_an_enclosed_photograph_takes_its_inception(self):
        # (here) a letter says when the photograph it encloses was taken
        photograph = {
            "id": "u2",
            "label": "photograph",
            "kind": "unclassified_entity",
            "type_hint": "photograph",
        }
        dated = claim(
            "inception",
            {"type": "time", "year": 1912},
            local("u2", "photograph", "unclassified_entity"),
            source=LETTER_1958_REF,
            excerpt="Посылаю тебе фотографию 1912 года.",
            page=None,
            language="ru",
        )
        assert checked(dated, entities=[photograph]) == []
        assert pre_plan_refusals(batch(dated, entities=[photograph]), {}) == {}

    @pytest.mark.parametrize("kind", ["human", "artifact_fragment", "death"])
    def test_no_other_kind_of_entity_takes_one(self, kind):
        wrong = claim("inception", JULY_28, local("x1", "x", kind), page=None)
        (problem,) = [
            p for p in checked(wrong) if p.endswith("[document_subject]")
        ]
        assert "an unclassified entity of the batch" in problem
        (refusal,) = pre_plan_refusals(batch(wrong), {}).values()
        assert refusal[0] == "subject_rule"

    @pytest.mark.parametrize(
        "subject",
        [
            {"kind": "qid", "qid": "Q42"},
            {"kind": "vocab", "key": "photograph"},
        ],
    )
    def test_an_item_or_a_class_named_outright_takes_none(self, subject):
        wrong = claim("inception", JULY_28, subject, page=None)
        assert any(p.endswith("[document_subject]") for p in checked(wrong))
        (refusal,) = pre_plan_refusals(batch(wrong), {}).values()
        assert refusal[0] == "subject_rule"

    def test_an_image_another_container_places_is_not_the_booklet_s(self):
        # the deck places the portrait; the booklet does not
        placed = {
            BOOKLET_REF.sha1: {LETTER_REF.sha1},
            DECK_REF.sha1: {PORTRAIT_REF.sha1},
        }
        dated = claim("inception", JULY_28, PORTRAIT_REF.model_dump())
        (problem,) = checked(dated, placed=placed)
        assert problem.endswith("[document_subject]")
        (refusal,) = pre_plan_refusals(
            batch(dated), {}, placed, {LETTER_REF.sha1, PORTRAIT_REF.sha1}
        ).values()
        assert refusal[0] == "subject_rule"


class TestTheExcerpt:
    """A claim about an artifact other than its own source carries the
    passage that conveys it (Q8): validation rejects one without it under
    its own reason, and the writer refuses it with its own message."""

    @pytest.mark.parametrize(
        "bare",
        [
            claim("inception", JULY_28, LETTER, excerpt=None),
            {
                **enclosure(),
                "source": {"artifact": LETTER_1958_REF.model_dump()},
            },
            claim(
                "inception",
                JULY_28,
                local("m1", "O1", "photograph"),
                excerpt=None,
            ),
            claim("inception", JULY_28, LETTER, excerpt="   "),
        ],
    )
    def test_a_claim_about_another_artifact_needs_its_excerpt(self, bare):
        entities = (
            [CERTIFICATE, DEATH] if bare["property"] == "main_subject" else []
        )
        (problem,) = checked(bare, placed=PLACED, entities=entities)
        assert problem.endswith("[excerpt_required]")
        assert "ADR-0039 §10.4" in problem
        refusals = pre_plan_refusals(
            batch(bare, entities=entities), {}, PLACED, {LETTER_REF.sha1}
        )
        ((reason, warning),) = refusals.values()
        assert reason == "subject_rule"
        assert (
            "its subject is not its own source, and it carries no excerpt "
            "of the passage that conveys it (ADR-0039 §10.4)"
        ) in warning

    def test_an_excerpt_the_instance_keeps_blank_is_refused(self):
        # U+180E is no whitespace to Python, but the instance trims it
        blank = claim("inception", JULY_28, LETTER, excerpt="\u180e")
        assert checked(blank, placed=PLACED) == []
        ((reason, warning),) = pre_plan_refusals(
            batch(blank), {}, PLACED, {LETTER_REF.sha1}
        ).values()
        assert reason == "subject_rule" and "no excerpt" in warning

    def test_a_document_s_claim_about_itself_needs_none(self):
        itself = claim(
            "inception", JULY_28, LETTER, source=LETTER_REF, excerpt=None
        )
        assert checked(itself) == []
        assert pre_plan_refusals(batch(itself), {}) == {}

    def test_salvage_counts_the_reason(self):
        _, rejects = salvage_valid_claims(
            [claim("inception", JULY_28, LETTER, excerpt=None)],
            run(),
            placed=PLACED,
        )
        assert rejected_by_reason(rejects) == {"excerpt_required": 1}


# -- the extraction stage's kind check ----------------------------------------


class TestNoExtractionNamesAnArtifactKind:
    """An extraction may not name an artifact kind (Q5, ADR-0039
    §10.1(b)): a manifest record or a local ref of one is refused where
    model output is first read, and counted."""

    @pytest.mark.parametrize("kind", ["photograph", "letter", "artifact"])
    def test_a_manifest_record_of_one_is_refused(self, kind):
        # the 1952 letter's portrait of Florence's daughter, typed as an
        # artifact so that its `shows` would mint a second artifact item
        response = json.dumps(
            {
                "entities": [
                    {"id": "e1", "label": "portret", "kind": kind},
                    {"id": "e2", "label": "Nicholas", "kind": "human"},
                ]
            }
        )
        manifest, problems = ChunkedClaimExtractor._parse_entity_manifest(
            response, 3
        )
        assert manifest is not None
        assert problems == [
            f"entity e1: artifact kind {kind!r}, which no extraction names "
            "[artifact_kind]"
        ]

    def test_an_artifact_fragment_is_no_artifact_kind(self):
        response = json.dumps(
            {
                "entities": [
                    {"id": "e1", "label": "x", "kind": "artifact_fragment"}
                ]
            }
        )
        _, problems = ChunkedClaimExtractor._parse_entity_manifest(response, 1)
        assert problems == []

    @pytest.mark.parametrize(
        "where",
        ["subject", "value", "qualifier", "instance_of", "subclass_of"],
    )
    def test_a_claim_whose_ref_names_one_is_a_counted_reject(self, where):
        photo = local("p1", "portret", "photograph")
        nicholas = item("e2", "Nicholas", "human")
        mention = local("u1", "portret", "unclassified_entity")
        shows = {
            "subject": claim("shows", nicholas, photo),
            "value": claim(
                "main_subject", {"type": "item", "value": photo}, LETTER
            ),
            "qualifier": {
                **claim("shows", nicholas, LETTER),
                "qualifiers": [
                    {
                        "property": "location",
                        "value": {"type": "item", "value": photo},
                    }
                ],
            },
            # a mention classed as an artifact by a claim of its own
            "instance_of": claim(
                "instance_of",
                {
                    "type": "item",
                    "value": {"kind": "vocab", "key": "photograph"},
                },
                mention,
            ),
            "subclass_of": claim(
                "subclass_of",
                {"type": "item", "value": {"kind": "vocab", "key": "letter"}},
                mention,
            ),
        }[where]
        kept, rejects = salvage_valid_claims([shows], run(), placed=PLACED)
        assert kept.claims == []
        (reject,) = rejects
        assert "artifact_kind" in reject["reasons"]
        assert rejected_by_reason(rejects)["artifact_kind"] == 1

    def test_a_class_that_is_no_artifact_kind_passes_the_check(self):
        classed = claim(
            "instance_of",
            {"type": "item", "value": {"kind": "vocab", "key": "human"}},
            local("u1", "portret", "unclassified_entity"),
        )
        assert artifact_kind_problems(batch(classed)) == []

    def test_a_manifest_turn_refused_for_one_is_counted(self, tmp_path):
        # the first answer types the letter's image as a photograph and is
        # refused; the second mends it: the turn stands, and the refusal is
        # counted in the coverage (Q5)
        typed = {"id": "e1", "label": "portret", "kind": "photograph"}
        mended = {**typed, "kind": "unclassified_entity"}
        llm = ScriptedLlm(
            [
                json.dumps({"entities": [typed]}),
                json.dumps({"entities": [mended]}),
                ndjson([]),
            ]
        )
        ChunkedClaimExtractor(llm, state_dir=tmp_path / "state").extract(
            booklet(), BOOKLET_REF
        )
        coverage = json.loads(
            (tmp_path / "state" / "coverage.json").read_text()
        )
        assert coverage["manifest_artifact_kind"] == 1
        assert coverage["fallback"] == {}

    def test_a_manifest_of_one_poisons_salvage(self):
        with pytest.raises(ValueError, match=r"\[artifact_kind\]"):
            salvage_valid_claims(
                [],
                run(),
                entities=[{"id": "m1", "label": "O1", "kind": "photograph"}],
            )

    def test_validation_does_not_apply_it(self):
        # a structured record's or a hand-written batch's entity of an
        # artifact kind is valid; only the extraction stage refuses one
        made = claim("inception", JULY_28, local("m1", "O1", "photograph"))
        assert checked(made) == []
        assert artifact_kind_problems(batch(made)) != []

    def test_a_hand_written_batch_takes_one(self, tmp_path):
        """Rule (b) holds for a batch the adapter did not make: `yizkr
        ingest extract --engine file`, whose run's model is "manual"
        (ADR-0039 §10.6)."""
        made = claim(
            "inception",
            {"type": "time", "year": 1895},
            local("m1", "Studio portrait of the Löbig family", "photograph"),
            source=BOOKLET_REF,
            excerpt="1 DATE ABT 1895",
            page=None,
        )
        claims_file = tmp_path / "claims.json"
        claims_file.write_text(json.dumps({"claims": [made]}))
        extracted = ClaimFileExtractor(claims_file).extract(
            booklet(), BOOKLET_REF
        )
        assert extracted.extraction_run.model_identifier == "manual"
        assert pre_plan_refusals(extracted, {}) == {}


# -- the count and hint for unplaced claims -----------------------------------


class TestUnplaced:
    def test_a_document_subject_claim_about_a_placed_image_counts(self):
        claims = batch(*translation(), *caption_claims())
        assert unplaced_claims(claims, None) == 5
        assert unplaced_claims(claims, PLACED) == 0

    def test_the_hint_is_pinned(self):
        assert unplaced_hint(5) == (
            "5 claim(s) about an artifact other than their source cite a "
            "source whose placed media were not given, so each is refused: "
            "give them with --placed <derived-media.json>, the record of the "
            "media a container places that the pipeline keeps in the "
            "container's document directory (or that directory), or "
            "--placed <source sha1>=<artifact sha1>[,<artifact sha1>...]"
        )


# -- the writer ---------------------------------------------------------------

VOCABULARY = {
    "caption": "P91",
    "inception": "P92",
    "location_of_creation": "P93",
    "addressee": "P94",
    "author": "P95",
    "main_subject": "P96",
}


class ArtifactWiki(FakeWiki):
    """The unit fake, with the document-subject properties and a death's
    kind in its vocabulary."""

    def vocabulary(self):
        return dict(PROPERTIES) | VOCABULARY, dict(ITEMS) | {"death": "Q6"}


def registered(wiki, *refs: ArtifactRef) -> dict[str, str]:
    qids = {}
    for ref in refs:
        artifact = wiki.wbi.item.new()
        artifact.write()
        wiki.media[ref.sha1] = artifact.id
        qids[ref.sha1] = artifact.id
    return qids


def stated(statement) -> list[tuple[str, list[str]]]:
    """Each reference block of `statement` that names its source, as the
    source's QID and the pages it cites, sorted."""
    found = []
    for block in statement.references.references:
        sources = block.snaks.get(PROPERTIES["stated_in"]) or []
        pages = block.snaks.get(PROPERTIES["page_number"]) or []
        for snak in sources:
            found.append(
                (
                    snak.datavalue["value"]["id"],
                    [page.datavalue["value"] for page in pages],
                )
            )
    return sorted(found)


class TestTheWriter:
    def test_the_translation_stands_beside_the_image_s_own_claims(self):
        wiki = ArtifactWiki()
        qids = registered(wiki, BOOKLET_REF, LETTER_REF)
        own = batch(
            claim(
                "inception",
                JULY_28,
                LETTER,
                source=LETTER_REF,
                excerpt="den 28. Juli 1939",
                page=None,
            )
        )
        write_mapped(BatchWriter(wiki), own)
        result = write_mapped(
            BatchWriter(wiki),
            batch(*translation(), entities=[PUENKTCHEN, ADDRESS]),
            placed=PLACED,
        )
        assert result.refused == []
        image = wiki.wbi.item.get(qids[LETTER_REF.sha1])
        for pid in ("P92", "P93", "P94"):
            assert image.claims.get(pid), pid
        # one inception, the day both give, stated in each source
        (dated,) = image.claims.get("P92")
        assert stated(dated) == sorted(
            [(qids[BOOKLET_REF.sha1], ["6"]), (qids[LETTER_REF.sha1], [])]
        )
        # nothing lands on the booklet's own item
        booklet_item = wiki.wbi.item.get(qids[BOOKLET_REF.sha1])
        assert not any(
            booklet_item.claims.get(pid) for pid in VOCABULARY.values()
        )

    def test_the_enclosure_s_claim_is_on_the_letter_s_mention_item(self):
        wiki = ArtifactWiki()
        qids = registered(wiki, LETTER_1958_REF)
        result = write_mapped(
            BatchWriter(wiki),
            batch(enclosure(), entities=[CERTIFICATE, DEATH]),
        )
        assert (result.written, result.refused) == (1, [])
        mention = result.created_items["u1"]
        (about,) = wiki.wbi.item.get(mention).claims.get("P96")
        assert (
            about.mainsnak.datavalue["value"]["id"]
            == (result.created_items["d1"])
        )
        assert not wiki.wbi.item.get(qids[LETTER_1958_REF.sha1]).claims.get(
            "P96"
        )

    def test_a_bare_claim_about_the_image_is_refused_and_counted(self):
        wiki = ArtifactWiki()
        registered(wiki, BOOKLET_REF, LETTER_REF)
        bare = batch(claim("inception", JULY_28, LETTER, excerpt=None))
        result = write_mapped(BatchWriter(wiki), bare, placed=PLACED)
        assert result.refused == [claim_id(bare.claims[0])]
        assert result.written == 0


# -- the prompts --------------------------------------------------------------


class TestThePrompts:
    def test_the_paragraph_on_what_a_document_is(self):
        paragraph = flat(DOCUMENT_ITSELF.format(artifact_sha1=LETTER_REF.sha1))
        for wanted in (
            "Their subject is THIS document, given exactly as the source's "
            f'artifact: {{"kind": "artifact", "sha1": "{LETTER_REF.sha1}"}}; '
            "an image this document places, by its artifact reference "
            "exactly as listed below; or a document this text mentions (a "
            "letter's enclosure), by its local reference, an "
            "`unclassified_entity` whose type_hint (in the entity manifest) "
            'or label says what it is ("death certificate", "photograph"); '
            "and never anything else.",
            "Never give an entity an artifact kind (`photograph`, `letter`, "
            "`certificate` and the like): a document the text mentions is an "
            "`unclassified_entity` with a type_hint.",
            "About THIS document, give each only where the document states "
            "it of itself, never from what another document says of it.",
            "give one only where a passage of THIS document, read where it "
            "stands, conveys that fact of that image or document, with that "
            "passage as the claim's transcription excerpt",
            "a claim about another document always carries its excerpt.",
            "its `main_subject` is his death.",
        ):
            assert wanted in paragraph, wanted

    def test_the_paragraph_on_what_a_document_places(self):
        block = flat(document_block(booklet(), BOOKLET_REF))
        assert flat(PLACED_ARTIFACTS.split("{placed_lines}")[1]) in block
        for wanted in (
            "Where a passage of THIS document, read where it stands, conveys "
            "what a listed image is, give that too",
            "\"Wastl to Klara\" under a letter's image gives the letter's "
            "`author` (Wastl) and `addressee` (Klara)",
            "a slide's title over its one photograph gives one `shows` per "
            "person it names",
            # Q7: the relation note rides with a reading of the title
            "with the same judgement note where the caption has one",
            # the claim's page is the passage's
            "its page number the page the passage stands on",
            # Q7: the caption's date dates nothing it names
            'the caption\'s "July 1939" does not say whether the letter was '
            "written, sent or received then, so it gives no `inception`",
            # a reproduction line gives no claim
            'a line on the reproduction ("Scanned by Nicholas for this '
            'booklet, 2010.") says who copied the image and when, not who '
            "made the original or when, so it gives none of these "
            "properties; where it speaks of the image it stays a caption.",
            # a translation gives the claims and no caption
            "A translation of the image's own text is no caption: it conveys "
            "what that text states (its dateline, its place, its greeting), "
            "so it gives those claims, its subject the image, and no "
            "`caption`.",
        ):
            assert wanted in block, wanted
        assert "Give nothing else about an image" not in block

    def test_a_document_that_places_nothing_names_no_image(self):
        block = flat(
            document_block(
                Transcription(artifact=LETTER_REF, text="x", engine="t"),
                LETTER_REF,
            )
        )
        assert (
            "give no `caption` claim, and no claim whose subject is an "
            "artifact reference other than this document's."
        ) in block

    def test_a_photograph_that_places_nothing_still_states_itself(self):
        # a studio portrait's scan places no image, and still gives its
        # own `author` and `shows`: the paragraph on what it places
        # forbids no claim about the document itself
        block = flat(
            document_block(
                Transcription(artifact=LETTER_REF, text="x", engine="t"),
                LETTER_REF,
            )
        )
        assert "* `shows`: whom or what an image shows" in block
        assert "no claim whose subject is an image" not in block
        forbidding = block.split("places no image read on its own")[1]
        assert "other than this document's" in forbidding

    def test_the_chunks_assign_these_claims(self):
        lines = flat(LINE_ASSIGNMENT_BLOCK)
        assert (
            "a claim about a document it mentions belong to the unit whose "
            "lines hold the passage that states or conveys it"
        ) in lines
        entities = flat(
            ENTITY_ASSIGNMENT_BLOCK.format(
                roster_block="", entities_block="", first_entity="e1"
            )
        )
        assert (
            "Every claim whose subject is the document itself or an image it "
            "places"
        ) in entities
        assert (
            "A claim about a document the text mentions, whose subject is "
            "that document's manifest entity, belongs to the chunk that "
            "entity is assigned to"
        ) in entities


class TestTheManifestText:
    """The seven descriptions drop "the subject is the source document
    itself", and those that said "as the document itself states" cover a
    source that conveys them about another artifact (ADR-0039 §10.5)."""

    def test_no_description_says_the_subject_is_the_source(self):
        from yizkr.model.schema import load_manifest

        properties = load_manifest().properties
        for key in load_acceptance().document_subjects:
            assert "the subject is the source document itself" not in (
                properties[key].description
            ), key

    @pytest.mark.parametrize(
        "key",
        [
            "author",
            "addressee",
            "inception",
            "location_of_creation",
            "main_subject",
            "shows",
        ],
    )
    def test_each_covers_what_a_passage_conveys(self, key):
        from yizkr.model.schema import load_manifest

        description = " ".join(
            load_manifest().properties[key].description.split()
        )
        assert "a source's passage conveys of a" in description, key


class TestTheReportForms:
    def test_other_is_given_only_when_counted(self):
        from collections import Counter

        from yizkr.pipeline import report

        vocabulary = report._Vocabulary(
            frozenset(), frozenset({"inception"}), frozenset()
        )
        counted = report._container_claims(
            Counter({"inception": 1}), Counter({"source": 1}), vocabulary
        )
        assert counted["document_subject_by_form"] == {
            "source": 1,
            "placed": 0,
            "record": 0,
            "mention": 0,
        }


# -- the critic ---------------------------------------------------------------


class TestTheCritic:
    """The critic's rows name the source as "this document" and a placed
    image by its place, as the prompts list it, and a finding that names a
    claim so still resolves to it (ADR-0039 §10.4)."""

    def claims(self) -> ClaimBatch:
        return batch(
            *caption_claims(),
            claim(
                "inception",
                {"type": "time", "year": 2011},
                BOOKLET_REF.model_dump(),
                excerpt="Stupfield, 2011",
            ),
            entities=[WASTL, KLARA],
        )

    def test_the_rows_name_the_document_and_the_image_by_its_place(self):
        places = critique.artifact_places(booklet())
        assert places == {LETTER_REF.sha1: "page 6, image 1"}
        assert critique.listings(self.claims(), places) == [
            ("page 6, image 1", "author", "e3"),
            ("page 6, image 1", "addressee", "e4"),
            ("this document", "inception", "2011"),
        ]
        prompt = critique.critique_prompt(
            self.claims(), booklet(), ["author"], ["human"]
        )
        assert "page 6, image 1 | author | e3 | times: -" in prompt
        assert '"this document" for the source itself' in flat(prompt)

    def test_the_support_test_names_the_artifact(self):
        prompt = flat(
            critique.critique_prompt(
                self.claims(), booklet(), ["author"], ["human"]
            )
        )
        assert (
            "For a claim about an artifact (this document, an image it "
            "places, or a document it mentions), the fact includes which "
            "artifact: the passage must convey it of that one."
        ) in prompt
        assert "or, for a claim about an artifact, on which artifact it " in (
            flat(critique.CATEGORIES["excerpt_does_not_state"])
        )

    def test_a_deck_s_image_is_named_by_its_slide_and_first_place(self):
        from tests.unit.test_container_claims import ALBUM_REF, paged
        from yizkr.pipeline.transcribe import DECK_ENGINE

        deck = paged(
            DECK_REF,
            [
                (2, [ALBUM_REF.sha1], "Wastl and Hilde"),
                (3, [PORTRAIT_REF.sha1, ALBUM_REF.sha1], "Stupfield"),
            ],
            engine=DECK_ENGINE,
        )
        assert critique.artifact_places(deck) == {
            ALBUM_REF.sha1: "slide 2, image 1",
            PORTRAIT_REF.sha1: "slide 3, image 1",
        }

    def test_without_its_transcription_an_image_is_its_content_key(self):
        assert critique.listings(self.claims())[0][0] == str(LETTER_REF)

    def test_a_finding_on_the_image_s_claim_resolves_to_it(self):
        finding = {
            "entity": "e3",
            "claim": {
                "subject": "page 6, image 1",
                "property": "author",
                "value": "e3",
            },
            "axis": "value",
            "category": "excerpt_does_not_state",
            "issue": "the caption names a writer",
            "suggestion": {"note": "keep"},
        }
        places = critique.artifact_places(booklet())
        vocabulary = (dict(VOCABULARY), {"human"})
        found = critique.check(finding, self.claims(), vocabulary, places)
        assert isinstance(found, critique.Finding)
        assert critique.claim_of(found, self.claims(), places) == 0
        marked = {
            **finding,
            "claim": {**finding["claim"], "subject": "Page 6, image 1"},
        }
        assert isinstance(
            critique.check(marked, self.claims(), vocabulary, places),
            critique.Finding,
        )
        unresolved = critique.check(finding, self.claims(), vocabulary)
        assert isinstance(unresolved, critique.Invalid)


class FindingLlm:
    """A critic that answers one finding on the letter image's `author`
    claim, named by the image's place, as the prompt lists it."""

    model_identifier = "fake-critic"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return "\n".join(
            [json.dumps(ON_THE_IMAGE), json.dumps({"done": True})]
        )


ON_THE_IMAGE = {
    "entity": "e3",
    "claim": {
        "subject": "page 6, image 1",
        "property": "author",
        "value": "e3",
    },
    "axis": "value",
    "category": "excerpt_does_not_state",
    "issue": "the caption names a writer",
    "suggestion": {"note": "keep"},
}


class TestTheCriticWiring:
    """A finding that names a placed image's claim by its place resolves
    to that claim when the critic runs and on the review sheet."""

    def claims(self) -> ClaimBatch:
        """The caption's claims after one of the booklet's own that names
        Wastl too, so that a finding which does not resolve to the image's
        `author` claim falls on the first row naming its entity."""
        return batch(
            claim(
                "main_subject",
                item("e3", "Wastl", "human"),
                BOOKLET_REF.model_dump(),
                excerpt="Our Family from Germany",
                page="1",
            ),
            *caption_claims(),
            entities=[WASTL, KLARA],
        )

    def test_the_critique_run_reads_the_places(self):
        llm = FindingLlm()
        (found,) = critique.run_critique(llm, self.claims(), booklet())
        assert isinstance(found, critique.Finding), found
        assert "page 6, image 1 | author | e3" in llm.prompts[0]

    def test_the_sheet_note_lands_on_the_image_claim_s_row(self):
        finding = critique.Finding.model_validate(ON_THE_IMAGE)
        places = critique.artifact_places(booklet())
        claims = self.claims()
        assert list(
            critique.sheet_notes([finding], claims, claims, places)
        ) == [1]
        assert list(critique.attach([finding], claims, claims, places)) == [1]
        # without the places it names no claim, and falls on its entity's
        # first row
        assert list(critique.sheet_notes([finding], claims, claims)) == [0]

    def notes(self, tmp_path, transcription: str | None) -> dict[int, str]:
        from types import SimpleNamespace

        from yizkr.pipeline.orchestrate import Orchestrator

        (tmp_path / "critique.json").write_text(json.dumps([ON_THE_IMAGE]))
        (tmp_path / "batch.json").write_text(self.claims().model_dump_json())
        if transcription is not None:
            (tmp_path / "transcription.json").write_text(transcription)
        doc = SimpleNamespace(
            critique=tmp_path / "critique.json",
            batch=tmp_path / "batch.json",
            transcription=tmp_path / "transcription.json",
        )
        runner = SimpleNamespace(
            _transcription_view=Orchestrator._transcription_view
        )
        return Orchestrator._critique_notes(runner, doc, self.claims())

    def test_the_review_sheet_reads_the_transcription_view(self, tmp_path):
        notes = self.notes(tmp_path, booklet().model_dump_json())
        assert list(notes) == [1]
        assert "unchecked" not in notes[1]

    @pytest.mark.parametrize("transcription", [None, "{not json"])
    def test_without_a_readable_view_it_names_no_place(
        self, tmp_path, transcription
    ):
        # no places, and no raise: the finding's claim, named by a place,
        # is none the sheet lists, so the note goes on its entity's row
        assert list(self.notes(tmp_path, transcription)) == [0]


# -- the export ---------------------------------------------------------------

W = "http://w.lan"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
PART_OF, STATED_IN, AUTHOR, INCEPTION = "P27", "P2", "P95", "P92"
MEDIA_SHA1 = "P6"
REF_BOOKLET = "b1" * 20  # stated in the booklet (Q20), page 6
REF_LETTER = "c2" * 20  # stated in the letter's image (Q10) itself
REF_EXPORT = "d3" * 20  # stated in the export (Q31)
REF_IMAGE = "e4" * 20  # stated in the image Q14 itself
# stated in the booklet (Q20), page 6, its translation as the excerpt
REF_TRANSLATION = "f5" * 20
REF_RUN = "a6" * 20  # inferred from the extraction run Q50
EXCERPT, RUN, CAPTION_PID = "P90", "P96", "P91"
# a second reference stated in a container: the booklet's page 7, or the
# album Q21
REF_PAGE_7 = "a7" * 20
CAPTION_TEXT = "Wastl to Klara, July 1939."
TRANSLATION = "Flörburg, Brunnenring 35, 28 July 1939"
NORMAL = "<http://wikiba.se/ontology#NormalRank>"
DEPRECATED = "<http://wikiba.se/ontology#DeprecatedRank>"


def st(
    entity: str,
    name: str,
    pid: str,
    value: str,
    *refs: str,
    rank: str = NORMAL,
) -> list[str]:
    """A statement's triples, its truthy one unless it is deprecated."""
    node = f"<{W}/entity/statement/{entity}-{name}>"
    lines = [
        f"<{W}/entity/{entity}> <{W}/prop/{pid}> {node} .",
        f"{node} <{W}/prop/statement/{pid}> {value} .",
        f"{node} <http://wikiba.se/ontology#rank> {rank} .",
        *(f"{node} {DERIVED_FROM} <{W}/reference/{ref}> ." for ref in refs),
    ]
    if rank == NORMAL:
        lines.append(
            f"<{W}/entity/{entity}> <{W}/prop/direct/{pid}> {value} ."
        )
    return lines


def cited_in(ref: str, qid: str) -> str:
    return f"<{W}/reference/{ref}> <{W}/prop/reference/{STATED_IN}> <{W}/entity/{qid}> ."  # noqa: E501


def hashed(entity: str) -> list[str]:
    return st(entity, "sha1", MEDIA_SHA1, f'"{entity}-bytes"', REF_IMAGE)


LINES = [
    # the letter's image is part of the booklet
    *st("Q10", "part", PART_OF, f"<{W}/entity/Q20>", REF_BOOKLET),
    # the booklet's caption conveys the letter's writer: it goes whole
    *st("Q10", "author", AUTHOR, f"<{W}/entity/Q3>", REF_BOOKLET),
    # the image's own date stays
    *st("Q10", "own", INCEPTION, '"+1939-07-28T00:00:00Z"', REF_LETTER),
    # the export dates the un-held media item its record makes: it stays
    *st("Q30", "made", INCEPTION, '"+1895-00-00T00:00:00Z"', REF_EXPORT),
    # the booklet's date of itself stays
    *st("Q20", "own", INCEPTION, '"+2011-00-00T00:00:00Z"', REF_BOOKLET),
    cited_in(REF_BOOKLET, "Q20"),
    f'<{W}/reference/{REF_BOOKLET}> <{W}/prop/reference/P3> "6" .',
    cited_in(REF_LETTER, "Q10"),
    cited_in(REF_EXPORT, "Q31"),
    cited_in(REF_IMAGE, "Q14"),
]
RULE = PlacedSubjectRule(
    document_subjects=frozenset({AUTHOR, INCEPTION}),
    part_of=PART_OF,
    stated_in=STATED_IN,
    media_sha1=MEDIA_SHA1,
)
# the rule as an instance without a `media SHA-1` property reads it
PART_OF_ONLY = PlacedSubjectRule(
    frozenset({AUTHOR, INCEPTION}), PART_OF, STATED_IN
)


def filtered(
    tmp_path: Path, placed: PlacedSubjectRule | None, *more: str
) -> str:
    full = tmp_path / "full.nt.gz"
    public = tmp_path / "nonprivileged.nt.gz"
    with gzip.open(full, "wt", encoding="utf-8") as handle:
        handle.write("\n".join([*LINES, *more]) + "\n")
    filter_nonprivileged_export(full, public, set(), placed=placed)
    with gzip.open(public, "rt", encoding="utf-8") as handle:
        return handle.read()


def translated() -> list[str]:
    """The booklet's reference that gives its translation of the letter's
    dateline as the excerpt, on page 6."""
    ref = f"<{W}/reference/{REF_TRANSLATION}>"
    return [
        cited_in(REF_TRANSLATION, "Q20"),
        f'{ref} <{W}/prop/reference/P3> "6" .',
        f'{ref} <{W}/prop/reference/{EXCERPT}> "{TRANSLATION}"@en .',
    ]


def merged(entity: str, *own: str, rank: str = NORMAL) -> list[str]:
    """The image's own date, 28 July 1939, as the writer folds the
    booklet's agreeing translation into it: one statement, its references
    `own` and the booklet's."""
    return st(
        entity,
        "both",
        INCEPTION,
        '"+1939-07-28T00:00:00Z"',
        *own,
        REF_TRANSLATION,
        rank=rank,
    )


def node(name: str) -> str:
    return f"<{W}/entity/statement/{name}>"


class TestTheExport:
    """The non-privileged export leaves out, failing closed (Q9, ADR-0039
    §10.5, §10.6), each reference one source gives a document-subject
    statement about another artifact it places: one whose subject carries
    a media SHA-1 and that is stated in another item, or whose subject is
    `part of` the item it is stated in. A statement left with no
    reference stated in an item goes whole (#669)."""

    def test_a_container_s_claim_about_its_image_goes_whole(self, tmp_path):
        content = filtered(tmp_path, RULE)
        assert "Q10-author" not in content
        assert f"<{W}/entity/Q10> <{W}/prop/direct/{AUTHOR}>" not in content
        # the image's own statement, the export's, the booklet's of
        # itself, and the composition link stay
        for kept in ("Q10-own", "Q30-made", "Q20-own", "Q10-part"):
            assert node(kept) in content, kept
        assert REF_BOOKLET in content

    def test_without_the_rule_it_would_leave(self, tmp_path):
        assert "Q10-author" in filtered(tmp_path, None)

    def test_an_image_placed_by_a_curator_s_pair_goes_whole(self, tmp_path):
        # the image carries its media SHA-1 and no `part of`: the booklet
        # placed it by `--placed <booklet>=<image>`
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *st("Q14", "author", AUTHOR, f"<{W}/entity/Q3>", REF_BOOKLET),
            *st("Q14", "own", INCEPTION, '"+1939-00-00T00:00:00Z"', REF_IMAGE),
        )
        assert "Q14-author" not in content
        assert node("Q14-own") in content and node("Q14-sha1") in content
        assert "Q14-author" in filtered(
            tmp_path,
            PART_OF_ONLY,
            *hashed("Q14"),
            *st("Q14", "author", AUTHOR, f"<{W}/entity/Q3>", REF_BOOKLET),
        )

    def test_an_image_part_of_another_container_goes_whole(self, tmp_path):
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *st("Q14", "part", PART_OF, f"<{W}/entity/Q40>", REF_IMAGE),
            *st("Q14", "author", AUTHOR, f"<{W}/entity/Q3>", REF_BOOKLET),
        )
        assert "Q14-author" not in content
        assert node("Q14-part") in content

    def test_a_deprecated_part_of_still_counts(self, tmp_path):
        content = filtered(
            tmp_path,
            PART_OF_ONLY,
            *st(
                "Q13",
                "part",
                PART_OF,
                f"<{W}/entity/Q20>",
                REF_BOOKLET,
                rank=DEPRECATED,
            ),
            *st("Q13", "author", AUTHOR, f"<{W}/entity/Q3>", REF_BOOKLET),
        )
        assert "Q13-author" not in content

    def test_a_deprecated_claim_about_the_image_goes_too(self, tmp_path):
        content = filtered(
            tmp_path,
            RULE,
            *st(
                "Q10",
                "dep",
                AUTHOR,
                f"<{W}/entity/Q4>",
                REF_BOOKLET,
                rank=DEPRECATED,
            ),
        )
        assert "Q10-dep" not in content

    def test_a_merged_statement_keeps_the_image_s_own_reference(
        self, tmp_path
    ):
        # the writer folds the booklet's agreeing date into the image's own
        # statement: one statement, stated in the image and in the booklet
        # (#669). The statement stays with the image's own reference and
        # its truthy triple; the booklet's reference, and its translation
        # as the excerpt, go
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *merged("Q14", REF_IMAGE),
            *translated(),
        )
        both = node("Q14-both")
        assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
        assert f"{both} {DERIVED_FROM} <{W}/reference/{REF_IMAGE}>" in content
        assert f"<{W}/entity/Q14> <{W}/prop/direct/{INCEPTION}>" in content
        assert REF_TRANSLATION not in content
        assert TRANSLATION not in content
        assert node("Q14-sha1") in content

    def test_an_image_part_of_its_container_keeps_its_own_reference(
        self, tmp_path
    ):
        # the letter's image is `part of` the booklet, and an instance
        # without `media SHA-1` reads it so too
        for rule in (RULE, PART_OF_ONLY):
            content = filtered(
                tmp_path, rule, *merged("Q10", REF_LETTER), *translated()
            )
            both = node("Q10-both")
            assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
            assert (
                f"{both} {DERIVED_FROM} <{W}/reference/{REF_LETTER}>"
                in content
            )
            assert REF_TRANSLATION not in content
            assert TRANSLATION not in content

    def test_the_container_s_claim_with_its_run_alone_goes_whole(
        self, tmp_path
    ):
        # a run's reference names no source: what is left of the booklet's
        # own claim once its reference goes is no source's, so it goes
        # whole, its truthy triple with it
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *st(
                "Q14",
                "both",
                INCEPTION,
                '"+1939-07-28T00:00:00Z"',
                REF_TRANSLATION,
                REF_RUN,
            ),
            *translated(),
            f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/{RUN}> "
            f"<{W}/entity/Q50> .",
        )
        assert "Q14-both" not in content
        assert f"<{W}/entity/Q14> <{W}/prop/direct/{INCEPTION}>" not in content
        assert TRANSLATION not in content and REF_RUN not in content

    def test_the_image_s_reference_and_the_runs_stay(self, tmp_path):
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *merged("Q14", REF_IMAGE, REF_RUN),
            *translated(),
            f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/{RUN}> "
            f"<{W}/entity/Q50> .",
        )
        both = node("Q14-both")
        for ref in (REF_IMAGE, REF_RUN):
            assert f"{both} {DERIVED_FROM} <{W}/reference/{ref}>" in content
        assert f"<{W}/reference/{REF_RUN}> <{W}/prop/reference/{RUN}>" in (
            content
        )
        assert TRANSLATION not in content

    def test_a_shared_container_reference_stays_where_it_is_kept(
        self, tmp_path
    ):
        # the booklet's reference on its own date is the same node: the
        # merged statement loses its link to it, the booklet's statement
        # keeps it, with its lines
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *st(
                "Q14",
                "both",
                INCEPTION,
                '"+1939-07-28T00:00:00Z"',
                REF_IMAGE,
                REF_BOOKLET,
            ),
        )
        both = node("Q14-both")
        assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
        assert f"{both} {DERIVED_FROM} <{W}/reference/{REF_BOOKLET}>" not in (
            content
        )
        assert f"{node('Q20-own')} {DERIVED_FROM} <{W}/reference/" in content
        assert (
            f'<{W}/reference/{REF_BOOKLET}> <{W}/prop/reference/P3> "6" .'
            in (content)
        )

    def test_a_deprecated_merged_statement_loses_the_link_too(self, tmp_path):
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *merged("Q14", REF_IMAGE, rank=DEPRECATED),
            *translated(),
        )
        assert node("Q14-both") in content
        assert TRANSLATION not in content

    def test_a_merged_statement_of_an_item_left_out_goes_whole(self, tmp_path):
        full = tmp_path / "full.nt.gz"
        public = tmp_path / "nonprivileged.nt.gz"
        with gzip.open(full, "wt", encoding="utf-8") as handle:
            handle.write(
                "\n".join(
                    [
                        *LINES,
                        *hashed("Q14"),
                        *st(
                            "Q14",
                            "both",
                            AUTHOR,
                            f"<{W}/entity/Q3>",
                            REF_IMAGE,
                            REF_TRANSLATION,
                        ),
                        *translated(),
                    ]
                )
                + "\n"
            )
        filter_nonprivileged_export(full, public, {"Q3"}, placed=RULE)
        with gzip.open(public, "rt", encoding="utf-8") as handle:
            content = handle.read()
        assert "Q14-both" not in content and TRANSLATION not in content

    @pytest.mark.parametrize(
        ("entity", "own", "rule"),
        [
            ("Q14", REF_IMAGE, RULE),  # by its media SHA-1
            ("Q10", REF_LETTER, PART_OF_ONLY),  # `part of` the booklet
        ],
    )
    @pytest.mark.parametrize(
        "qualifier",
        [
            f'<{W}/prop/qualifier/{CAPTION_PID}> "{CAPTION_TEXT}"@en',
            f'<{W}/prop/qualifier/{EXCERPT}> "{CAPTION_TEXT}"@en',
            f'<{W}/prop/qualifier/{EXCERPT}> "{CAPTION_TEXT}"',
        ],
    )
    def test_a_merged_statement_with_a_qualifier_of_text_goes_whole(
        self, tmp_path, entity, own, rule, qualifier
    ):
        # the writer merges claims whose qualifiers match, so the
        # booklet's caption may stand in a qualifier of the merged
        # statement: which claim's words it is cannot be told (#695)
        content = filtered(
            tmp_path,
            rule,
            *hashed("Q14"),
            *merged(entity, own),
            f"{node(entity + '-both')} {qualifier} .",
            *translated(),
        )
        assert f"{entity}-both" not in content
        assert CAPTION_TEXT not in content and TRANSLATION not in content
        # its truthy triple goes, unless another statement gives it: the
        # letter's image Q10 states the same date once more of itself
        truthy = f"<{W}/entity/{entity}> <{W}/prop/direct/{INCEPTION}>"
        assert (truthy in content) == (entity == "Q10")

    def test_a_qualifier_of_no_text_keeps_the_statement(self, tmp_path):
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *merged("Q14", REF_IMAGE),
            f"{node('Q14-both')} <{W}/prop/qualifier/{PART_OF}> "
            f"<{W}/entity/Q20> .",
            f"{node('Q14-both')} <{W}/prop/qualifier/P93> "
            '"+1939-07-01T00:00:00Z"^^'
            "<http://www.w3.org/2001/XMLSchema#dateTime> .",
            *translated(),
        )
        both = node("Q14-both")
        assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
        assert f"{both} <{W}/prop/qualifier/P93>" in content
        assert TRANSLATION not in content

    def test_each_reference_of_the_container_goes(self, tmp_path):
        # two references stated in the booklet, on pages 6 and 7: both go
        page_7 = f"<{W}/reference/{REF_PAGE_7}>"
        content = filtered(
            tmp_path,
            RULE,
            *hashed("Q14"),
            *merged("Q14", REF_IMAGE, REF_PAGE_7),
            *translated(),
            cited_in(REF_PAGE_7, "Q20"),
            f'{page_7} <{W}/prop/reference/P3> "7" .',
            f'{page_7} <{W}/prop/reference/{EXCERPT}> "{CAPTION_TEXT}"@en .',
        )
        both = node("Q14-both")
        assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
        assert REF_PAGE_7 not in content and REF_TRANSLATION not in content
        assert CAPTION_TEXT not in content and TRANSLATION not in content

    def test_each_container_an_image_is_part_of_counts(self, tmp_path):
        # the letter's image is `part of` the booklet and of the album
        # Q21 too: each one's reference goes
        album = f"<{W}/reference/{REF_PAGE_7}>"
        content = filtered(
            tmp_path,
            PART_OF_ONLY,
            *st("Q10", "album", PART_OF, f"<{W}/entity/Q21>", REF_LETTER),
            *merged("Q10", REF_LETTER, REF_PAGE_7),
            *translated(),
            cited_in(REF_PAGE_7, "Q21"),
            f'{album} <{W}/prop/reference/{EXCERPT}> "{CAPTION_TEXT}"@en .',
        )
        both = node("Q10-both")
        assert f"{both} <{W}/prop/statement/{INCEPTION}>" in content
        assert f"{both} {DERIVED_FROM} <{W}/reference/{REF_LETTER}>" in (
            content
        )
        assert REF_PAGE_7 not in content and REF_TRANSLATION not in content
        assert CAPTION_TEXT not in content and TRANSLATION not in content

    def test_the_rule_reads_the_instance_s_ids(self):
        properties = {
            "part_of": "P27",
            "stated_in": "P2",
            "media_sha1": "P6",
            "inception": "P92",
            "author": "P95",
            "caption": "P91",
        }
        rule = placed_subject_rule(properties)
        assert rule == PlacedSubjectRule(
            frozenset({"P92", "P95"}), "P27", "P2", "P6"
        )
        assert placed_subject_rule({"part_of": "P27"}) is None
