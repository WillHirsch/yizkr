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
from tests.unit.test_container_claims import (
    BOOKLET_REF,
    CAPTION,
    JULY_28,
    LETTER_REF,
    PLACED,
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
            "its subject is an artifact other than its own source, and it "
            "carries no excerpt of the passage that conveys it "
            "(ADR-0039 §10.4)"
        ) in warning

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

    @pytest.mark.parametrize("where", ["subject", "value"])
    def test_a_claim_whose_ref_names_one_is_a_counted_reject(self, where):
        photo = local("p1", "portret", "photograph")
        nicholas = item("e2", "Nicholas", "human")
        shows = claim("shows", nicholas, photo)
        if where == "value":
            shows = claim(
                "main_subject", {"type": "item", "value": photo}, LETTER
            )
        kept, rejects = salvage_valid_claims([shows], run(), placed=PLACED)
        assert kept.claims == []
        (reject,) = rejects
        assert "artifact_kind" in reject["reasons"]
        assert rejected_by_reason(rejects)["artifact_kind"] == 1

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
            '`unclassified_entity` whose type_hint says what it is ("death '
            'certificate", "photograph"); and never anything else.',
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
            # Q7: the caption's date dates nothing it names
            'the caption\'s "July 1939" does not say whether the letter was '
            "written, sent or received then, so it gives no `inception`",
            # a reproduction line gives no claim
            'a line on the reproduction ("Scanned for this booklet, 2010.") '
            "says who copied the image and when, not who made the original "
            "or when, so it gives none at all.",
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
            "give no `caption` claim, and no claim whose subject is an image"
            in block
        )

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
        unresolved = critique.check(finding, self.claims(), vocabulary)
        assert isinstance(unresolved, critique.Invalid)


# -- the export ---------------------------------------------------------------

W = "http://w.lan"
DERIVED_FROM = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
PART_OF, STATED_IN, AUTHOR, INCEPTION = "P27", "P2", "P95", "P92"
REF_BOOKLET = "b1" * 20  # stated in the booklet (Q20), page 6
REF_LETTER = "c2" * 20  # stated in the letter's image (Q10) itself
REF_EXPORT = "d3" * 20  # stated in the export (Q31)


def st(entity: str, name: str, pid: str, value: str, ref: str) -> list[str]:
    node = f"<{W}/entity/statement/{entity}-{name}>"
    return [
        f"<{W}/entity/{entity}> <{W}/prop/{pid}> {node} .",
        f"{node} <{W}/prop/statement/{pid}> {value} .",
        f"<{W}/entity/{entity}> <{W}/prop/direct/{pid}> {value} .",
        f"{node} <http://wikiba.se/ontology#rank> "
        "<http://wikiba.se/ontology#NormalRank> .",
        f"{node} {DERIVED_FROM} <{W}/reference/{ref}> .",
    ]


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
    f"<{W}/reference/{REF_BOOKLET}> <{W}/prop/reference/{STATED_IN}> <{W}/entity/Q20> .",  # noqa: E501
    f'<{W}/reference/{REF_BOOKLET}> <{W}/prop/reference/P3> "6" .',
    f"<{W}/reference/{REF_LETTER}> <{W}/prop/reference/{STATED_IN}> <{W}/entity/Q10> .",  # noqa: E501
    f"<{W}/reference/{REF_EXPORT}> <{W}/prop/reference/{STATED_IN}> <{W}/entity/Q31> .",  # noqa: E501
]
RULE = PlacedSubjectRule(
    document_subjects=frozenset({AUTHOR, INCEPTION}),
    part_of=PART_OF,
    stated_in=STATED_IN,
)


def filtered(tmp_path: Path, placed: PlacedSubjectRule | None) -> str:
    full = tmp_path / "full.nt.gz"
    public = tmp_path / "nonprivileged.nt.gz"
    with gzip.open(full, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    filter_nonprivileged_export(full, public, set(), placed=placed)
    with gzip.open(public, "rt", encoding="utf-8") as handle:
        return handle.read()


class TestTheExport:
    """The non-privileged export leaves out whole a document-subject
    statement whose subject is `part of` the item its reference's `stated
    in` names, failing closed (Q9, ADR-0039 §10.5)."""

    def test_a_container_s_claim_about_its_image_goes_whole(self, tmp_path):
        content = filtered(tmp_path, RULE)
        assert "Q10-author" not in content
        assert f"<{W}/entity/Q10> <{W}/prop/direct/{AUTHOR}>" not in content
        # the image's own statement, the export's, the booklet's of
        # itself, and the composition link stay
        for kept in ("Q10-own", "Q30-made", "Q20-own", "Q10-part"):
            assert f"<{W}/entity/statement/{kept}>" in content, kept
        assert REF_BOOKLET in content

    def test_without_the_rule_it_would_leave(self, tmp_path):
        assert "Q10-author" in filtered(tmp_path, None)

    def test_the_rule_reads_the_instance_s_ids(self):
        properties = {
            "part_of": "P27",
            "stated_in": "P2",
            "inception": "P92",
            "author": "P95",
            "caption": "P91",
        }
        rule = placed_subject_rule(properties)
        assert rule == PlacedSubjectRule(
            frozenset({"P92", "P95"}), "P27", "P2"
        )
        assert placed_subject_rule({"stated_in": "P2"}) is None
