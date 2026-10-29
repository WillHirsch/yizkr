"""Container consumers preserve source identity, eligibility and review."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib

import pytest

from tests.fixtures import pdfs
from tests.unit.test_chunking import ScriptedLlm, ndjson
from tests.unit.test_container_claims import (
    CAPTION,
    DECK_REF,
    LETTER_REF,
    STRAY_REF,
    caption,
    paged,
)
from tests.unit.test_deck_excerpts import deck, talk_folder
from tests.unit.test_pdf_pages import (
    EXCERPT,
    LETTER_IMAGE,
    LETTER_SHA1,
    Reader,
    RefusingReader,
    booklet,
    document,
    excerpt_of,
    folder,
    run,
    typed,
)
from yizkr.claims import ArtifactRef, PlacementSupport, SourceSupport
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import transcribe
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    keep_inventory,
    read_inventory,
    structural_id,
)
from yizkr.pipeline.container_sources import prepare_sources, project_sources
from yizkr.pipeline.critique import artifact_places
from yizkr.pipeline.extract import (
    ExtractionFailed,
    cite_artifact,
    placed_by,
    placed_lines,
)
from yizkr.pipeline.orchestrate import WRITTEN
from yizkr.pipeline.pdf_inventory import inventory_pdf
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA,
    DerivedMediaRecord,
    DerivedMedium,
    unpack_pptx,
)
from yizkr.pipeline.review import accept_all
from yizkr.pipeline.source_context import (
    LocatedReading,
    PlacedSource,
    SourceContext,
    bind_source_support,
)
from yizkr.pipeline.text_stages import (
    CritiqueRequest,
    ExtractionRequest,
    critique,
    extract,
)
from yizkr.pipeline.textlayer import png
from yizkr.source_structure import StructuralLocator


@pytest.fixture(autouse=True)
def renderer(monkeypatch):
    """No poppler tool runs: a scripted reader reads every PDF here, as
    in test_pdf_pages.py, whose `renderer` fixture this mirrors."""
    monkeypatch.setattr(transcribe, "require_pdf_renderer", lambda *a: None)


def container_corpus(tmp_path, kind):
    if kind == "pptx":
        return talk_folder(tmp_path), "library-talk-2018.pptx"
    return (
        folder(tmp_path, family_booklet_2011=booklet()),
        "family-booklet-2011.pdf",
    )


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_inventory_discovers_existing_excerpt_without_legacy_media_record(
    tmp_path, kind
):
    corpus, name = container_corpus(tmp_path, kind)
    driver = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    container = document(driver, name)
    original = excerpt_of(driver, LETTER_SHA1)
    assert read_inventory(container.dir) is not None
    (container.dir / DERIVED_MEDIA).unlink(missing_ok=True)
    driver._excerpts = {}
    (found,) = driver._excerpts_of(container, ())
    assert found.sha1 == original.sha1
    assert found.dir == original.dir
    assert found.source.read_bytes() == original.source.read_bytes()


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
@pytest.mark.parametrize("missing", [DERIVED_MEDIA, CONTAINER_INVENTORY])
def test_container_metadata_recovery_preserves_approved_products(
    tmp_path, kind, missing
):
    corpus, name = container_corpus(tmp_path, kind)
    first = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    container = document(first, name)
    excerpt = excerpt_of(first, LETTER_SHA1)
    for subject in (container, excerpt):
        accept_all(subject.review, subject.review_reviewed)
    products = {
        path: path.read_bytes()
        for subject in (container, excerpt)
        for path in (
            subject.transcription,
            subject.batch,
            subject.resolved,
            subject.review,
            subject.review_reviewed,
        )
    }
    (container.dir / missing).unlink(missing_ok=True)
    again = run(tmp_path, corpus, RefusingReader({}))
    restored_container = document(again, name)
    restored_excerpt = excerpt_of(again, LETTER_SHA1)
    assert restored_excerpt.dir == excerpt.dir
    assert restored_container.state.status == WRITTEN
    assert restored_excerpt.state.status == WRITTEN
    assert {path: path.read_bytes() for path in products} == products


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_new_exclusion_blocks_inventory_excerpt_despite_saved_reading(
    tmp_path, kind
):
    corpus, name = container_corpus(tmp_path, kind)
    driver = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    container = document(driver, name)
    assert excerpt_of(driver, LETTER_SHA1).transcription.exists()
    driver._excerpts = {}
    driver.exclusions = ExclusionList.parse(f"{LETTER_SHA1}\n")
    assert driver._excerpts_of(container, ()) == []


def test_legacy_unplaced_owner_does_not_requeue_container_reading(tmp_path):
    """Retaining an old owner does not invent a paint or another read."""
    from yizkr.pipeline.presentation import record_derived_media

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = deck(
        corpus / "talk.pptx",
        [((CAPTION,), ["letter.png"], "")],
        {"letter.png": png(300, 120, 1, LETTER_IMAGE.data)},
        painted={1: []},
    )
    first = run(tmp_path, corpus, Reader({}))
    container = document(first, source.name)
    original = container.transcription.read_bytes()
    record_derived_media(
        unpack_pptx(source),
        source_sha1=container.sha1,
        title=container.state.title,
        directory=container.dir,
    )
    for _ in range(2):
        again = run(tmp_path, corpus, RefusingReader({}))
        current = document(again, source.name)
        sources = again._container_sources(current)
        assert LETTER_SHA1 in sources.sources
        assert not sources.sources[LETTER_SHA1].readable
        assert again._excerpts == {}
        assert current.transcription.read_bytes() == original


def test_presentation_reader_refuses_preparation_from_another_source(tmp_path):
    from yizkr.pipeline.transcribe import DeckTranscriber

    first = deck(tmp_path / "letter.pptx", [((CAPTION,), [], "")], {})
    other = deck(
        tmp_path / "booklet.pptx",
        [(("Our Family from Germany.",), [], "")],
        {},
    )
    reader = DeckTranscriber(
        lambda: RefusingReader({}), content=unpack_pptx(other)
    )
    with pytest.raises(ValueError, match="belongs to another source"):
        reader.transcribe(first)


def test_legacy_excerpt_gains_current_source_support_without_vision(tmp_path):
    """Older readings gain typed support without another vision call."""
    from yizkr.pipeline.orchestrate import (
        BATCH_VOLATILE,
        TRANSCRIPTION_VOLATILE,
    )
    from yizkr.pipeline.presentation import derived_transcription
    from yizkr.pipeline.stages import content_hash
    from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT

    corpus, name = container_corpus(tmp_path, "pptx")
    first = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    container = document(first, name)
    excerpt = excerpt_of(first, LETTER_SHA1)
    image_reading = derived_transcription(container.dir, LETTER_SHA1)
    for path in (image_reading, excerpt.transcription):
        value = json.loads(path.read_text())
        value.pop("source_context", None)
        path.write_text(json.dumps(value))
    # This is a valid old product, not an unrecorded edit of a new one.
    inventory = read_inventory(container.dir)
    for part in inventory.parts:
        if (
            part.reading.product
            == image_reading.relative_to(container.dir).as_posix()
        ):
            part.reading.product_sha256 = hashlib.sha256(
                image_reading.read_bytes()
            ).hexdigest()
    keep_inventory(inventory, {}, container.dir)
    old_reading = json.loads(excerpt.transcription.read_text())
    excerpt.derivations.set_content(
        excerpt.transcription.name,
        content_hash(old_reading, volatile=TRANSCRIPTION_VOLATILE),
    )
    old_batch = json.loads(excerpt.batch.read_text())
    for claim in old_batch["claims"]:
        claim["source"].pop("support", None)
    excerpt.batch.write_text(json.dumps(old_batch))
    excerpt.derivations.set_content(
        excerpt.batch.name, content_hash(old_batch, volatile=BATCH_VOLATILE)
    )
    excerpt.derivations.record(excerpt.batch)
    accept_all(excerpt.review, excerpt.review_reviewed)
    approved = excerpt.review_reviewed.read_bytes()

    class NoVision(Reader):
        def complete(self, prompt, image_paths=()):
            assert prompt != TRANSCRIPTION_PROMPT
            return super().complete(prompt, image_paths)

    again = run(tmp_path, corpus, NoVision({}))
    current = excerpt_of(again, LETTER_SHA1)
    reading = json.loads(current.transcription.read_text())
    assert reading["text"] == old_reading["text"]
    assert reading["source_context"]["readings"]
    batch = json.loads(current.batch.read_text())
    for claim in batch["claims"]:
        support = claim["source"]["support"]
        assert support["container"]["sha1"] == container.sha1
        assert support["placements"]
    assert not current.review_reviewed.exists()
    assert any(
        path.read_bytes() == approved
        for path in current.dir.glob("review.reviewed.superseded-*.csv")
    )


NOTES = "Gustl finally has his visa."


def located_slide(*, notes=NOTES, unresolved=False):
    reading = paged(DECK_REF, [(2, [STRAY_REF.sha1], CAPTION + "\n" + notes)])
    spans = []
    for kind, text, package in (
        ("slide_body", CAPTION, "ppt/slides/slide2.xml"),
        ("speaker_notes", notes, "ppt/notesSlides/notesSlide2.xml"),
    ):
        locator = StructuralLocator(
            kind=kind, position=2, package_part=package
        )
        support = SourceSupport(
            container=DECK_REF,
            part_id=structural_id(DECK_REF, locator),
            locator=locator,
        )
        start = (
            reading.pages[0].start
            if kind == "slide_body"
            else reading.pages[0].end - len(text)
        )
        spans.append(
            LocatedReading(
                start=start,
                end=start + len(text),
                owner=None if unresolved else DECK_REF,
                support=support,
                state="unsupported" if unresolved else "attributed",
                reason="composite reading cannot separate source owners"
                if unresolved
                else None,
            )
        )
    locator = StructuralLocator(
        kind="image", package_part="ppt/media/image2.png"
    )
    placements = []
    for number in (1, 3):
        occurrence = StructuralLocator(
            kind="shape", position=2, path=f"/spTree/pic[{number}]"
        )
        placements.append(
            PlacementSupport(
                id=structural_id(DECK_REF, occurrence), locator=occurrence
            )
        )
    context = SourceContext(
        container=DECK_REF,
        readings=spans,
        placements=[
            PlacedSource(
                owner=LETTER_REF,
                support=SourceSupport(
                    container=DECK_REF,
                    part_id=structural_id(DECK_REF, locator),
                    locator=locator,
                    placements=placements,
                ),
            )
        ],
    )
    return reading.model_copy(update={"source_context": context})


def quoted_claim(text):
    claim = caption()
    claim["source"]["page_number"] = "2"
    claim["source"]["transcription_excerpt"]["text"] = text
    return claim


def located_pages(*, single=False):
    reading = paged(
        DECK_REF,
        ([] if single else [(1, [], NOTES)])
        + [(2, [LETTER_REF.sha1], CAPTION)],
    )
    context = located_slide().source_context
    context.readings = []
    for page in reading.pages:
        locator = StructuralLocator(
            kind="slide_body",
            position=page.page,
            package_part=f"ppt/slides/slide{page.page}.xml",
        )
        context.readings.append(
            LocatedReading(
                start=page.start,
                end=page.end,
                owner=DECK_REF,
                support=SourceSupport(
                    container=DECK_REF,
                    part_id=structural_id(DECK_REF, locator),
                    locator=locator,
                ),
            )
        )
    return reading.model_copy(update={"source_context": context})


@pytest.mark.parametrize("strategy", ["single", "chunked"])
@pytest.mark.parametrize(
    "quote, diagnostic",
    [
        ("Our Family from Germany", "not in the transcription"),
        (NOTES, "not on page 2"),
    ],
)
def test_attributed_claim_with_bad_quote_reaches_critique(
    tmp_path, strategy, quote, diagnostic
):
    reading = located_pages()
    answers = [ndjson([quoted_claim(CAPTION), quoted_claim(quote)])]
    if strategy == "chunked":
        answers.insert(
            0,
            json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": "Wastl",
                            "kind": "human",
                            "mention_lines": [5],
                        }
                    ]
                }
            ),
        )
    result = extract(
        ExtractionRequest(
            reading,
            DECK_REF,
            strategy=strategy,
            read_preparation=False,
            state_dir=tmp_path / "extraction",
        ),
        ScriptedLlm(answers),
    )
    batch = result.require_complete()
    assert len(batch.claims) == 2
    bad_quote = batch.claims[1].source
    assert bad_quote.artifact == DECK_REF
    assert bad_quote.support == reading.source_context.readings[1].support
    assert bad_quote.page_number == "2"
    assert bad_quote.transcription_excerpt.text == quote

    critic = ScriptedLlm(['{"done": true}'])
    review = critique(CritiqueRequest(batch, reading), critic)
    assert review.status == "complete"
    assert diagnostic in critic.calls[0][1]


@pytest.mark.parametrize("quoted", [False, True])
def test_absent_page_uses_quote_or_an_unambiguous_whole_reading(quoted):
    reading = located_pages(single=not quoted)
    claim = quoted_claim(CAPTION if quoted else "Our Family from Germany")
    claim["source"].pop("page_number")
    accepted, rejected = bind_source_support([claim], reading)
    assert rejected == []
    assert accepted[0]["source"]["support"]["locator"]["position"] == 2
    assert "page_number" not in accepted[0]["source"]


def test_bad_quote_without_page_cannot_choose_between_source_parts():
    claim = quoted_claim("Our Family from Germany")
    claim["source"].pop("page_number")
    accepted, rejected = bind_source_support([claim], located_pages())
    assert accepted == []
    assert rejected[0]["reasons"] == ["source_support"]


@pytest.mark.parametrize("problem", [None, "stale-reading", "unsupported"])
def test_bad_quote_requires_every_observation_of_its_part_to_be_valid(problem):
    reading = located_pages(single=True)
    repeated = reading.source_context.readings[0].model_copy(deep=True)
    reading.source_context.readings.append(repeated)
    if problem == "stale-reading":
        repeated.support.reading_sha256 = "0" * 64
    if problem == "unsupported":
        repeated.state = "unsupported"
        repeated.reason = "composite reading cannot separate source owners"
    accepted, rejected = bind_source_support(
        [quoted_claim("Our Family from Germany")], reading
    )
    if problem:
        assert accepted == []
        assert rejected[0]["reasons"] == ["source_support"]
    else:
        assert rejected == []
        assert len(accepted) == 1
        assert accepted[0]["source"]["support"] == repeated.support.model_dump(
            mode="json"
        )


def test_extraction_and_critique_share_original_placement_occurrences():
    reading = located_slide()
    lines = placed_lines(reading)
    assert placed_by(reading, DECK_REF) == {
        DECK_REF.sha1: frozenset({LETTER_REF.sha1})
    }
    assert len(lines) == 2
    assert all(
        LETTER_REF.sha1 in line and STRAY_REF.sha1 not in line
        for line in lines
    )
    for placement in reading.source_context.placements[0].support.placements:
        assert sum(placement.id in line for line in lines) == 1
        assert placement.id in artifact_places(reading)[LETTER_REF.sha1]


def test_claims_keep_distinct_slide_body_and_speaker_notes_support():
    reading = located_slide()
    claims, rejects = bind_source_support(
        cite_artifact([quoted_claim(CAPTION), quoted_claim(NOTES)], DECK_REF),
        reading,
    )
    assert rejects == []
    assert [
        claim["source"]["support"]["locator"]["kind"] for claim in claims
    ] == ["slide_body", "speaker_notes"]
    assert (
        len({claim["source"]["support"]["part_id"] for claim in claims}) == 2
    )
    assert all(
        claim["source"]["artifact"] == DECK_REF.model_dump()
        for claim in claims
    )


@pytest.mark.parametrize(
    "problem",
    ["ambiguous-notes", "unresolved-owner", "wrong-page", "missing-text"],
)
def test_uncertain_source_support_is_rejected_instead_of_relabelled(problem):
    reading = located_slide(
        notes=CAPTION if problem == "ambiguous-notes" else NOTES,
        unresolved=problem == "unresolved-owner",
    )
    claim = quoted_claim(CAPTION)
    if problem == "wrong-page":
        claim["source"]["page_number"] = "3"
    if problem == "missing-text":
        claim["source"]["transcription_excerpt"]["text"] = (
            "Our Family from Germany"
        )
    accepted, rejected = bind_source_support([claim], reading)
    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0]["reasons"] == ["source_support"]
    assert rejected[0]["problems"]


def test_model_cannot_supply_trusted_source_support():
    reading = located_slide()
    claim = quoted_claim(CAPTION)
    forged = reading.source_context.readings[1].support.model_dump(mode="json")
    claim["source"]["support"] = forged
    (cleaned,) = cite_artifact([claim], DECK_REF)
    assert "support" not in cleaned["source"]
    (bound,), rejected = bind_source_support([claim], reading)
    assert rejected == []
    assert bound["source"]["support"]["locator"]["kind"] == "slide_body"
    assert bound["source"]["support"] != forged


def test_changed_reading_cannot_inherit_stale_source_support():
    reading = located_slide()
    reading.source_context.readings[0].support.reading_sha256 = "0" * 64
    accepted, rejected = bind_source_support([quoted_claim(CAPTION)], reading)
    assert accepted == []
    assert rejected[0]["problems"] == [
        "source support names a different reading revision"
    ]


@pytest.mark.parametrize("strategy", ["single", "chunked"])
def test_unattributed_claim_keeps_partial_results_diagnostic(
    tmp_path, strategy
):
    reading = located_slide()
    answer = ndjson(
        [quoted_claim(CAPTION), quoted_claim("Our Family from Germany")]
    )
    answers = [answer]
    if strategy == "chunked":
        answers.insert(
            0,
            json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": "Wastl",
                            "kind": "human",
                            "mention_lines": [3],
                        }
                    ]
                }
            ),
        )
    result = extract(
        ExtractionRequest(
            reading,
            DECK_REF,
            strategy=strategy,
            read_preparation=False,
            state_dir=tmp_path / "extraction",
            rejects_dump=tmp_path / "rejects.json",
            diagnostic_dump=tmp_path / "incomplete.json",
        ),
        ScriptedLlm(answers),
    )
    assert result.status == "incomplete"
    assert result.batch is None
    assert len(result.diagnostic_batch.claims) == 1
    assert (
        result.diagnostic_batch.claims[0].source.support.locator.kind
        == "slide_body"
    )
    assert any(
        "source attribution" in reason for reason in result.incomplete_reasons
    )
    with pytest.raises(ExtractionFailed, match="source attribution"):
        result.require_complete()
    assert (
        json.loads((tmp_path / "incomplete.json").read_text())["status"]
        == "incomplete"
    )


@pytest.mark.parametrize("missing_owner", [False, True])
def test_changed_prepared_encoding_cannot_rename_an_existing_owner(
    tmp_path, missing_owner
):
    data = pdfs.pdf([typed(CAPTION).image(LETTER_IMAGE, 72, 400, 300, 120)])
    plan = inventory_pdf(data, lambda _: False)
    first = prepare_sources(plan.inventory, plan.data, tmp_path)
    keep_inventory(first, plan.data, tmp_path)
    (original,) = [
        part for part in first.parts if part.kind == "embedded_resource"
    ]
    assert original.owner_binding.artifact.sha1 == LETTER_SHA1

    old = plan.data[LETTER_SHA1]
    changed = reencoded_png(old)
    reference = ArtifactRef.from_bytes(changed)
    current = plan.inventory.model_copy(deep=True)
    (part,) = [
        part for part in current.parts if part.kind == "embedded_resource"
    ]
    part.representations[0].byte_object = reference
    part.representations[0].file = f"representations/{reference.sha1}.png"
    prepared_bytes = {
        key: value for key, value in plan.data.items() if key != LETTER_SHA1
    }
    prepared_bytes[reference.sha1] = changed
    if missing_owner:
        (tmp_path / original.owner_binding.file).unlink()
    rebound = prepare_sources(
        current, prepared_bytes, tmp_path, previous=first
    )
    projected = project_sources(rebound, tmp_path, data=prepared_bytes)
    if missing_owner:
        assert projected.sources == {}
        assert any(
            "canonical bytes" in item.reason for item in projected.unresolved
        )
    else:
        assert set(projected.sources) == {LETTER_SHA1}
        source = projected.sources[LETTER_SHA1]
        assert source.part_ids == [original.id]
        assert source.current_input.representation == reference
        assert source.owner == original.owner_binding.artifact
        assert (tmp_path / source.file).read_bytes() == old


def reencoded_png(data):
    """Add PNG metadata without changing its pixels."""
    chunk = b"tEXtComment\x00fictional library talk"
    return (
        data[:-12]
        + struct.pack(">I", len(chunk) - 4)
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + data[-12:]
    )


@pytest.mark.parametrize("missing_owner", [False, True])
def test_surviving_reading_context_recovers_owner_after_inventory_loss(
    tmp_path, monkeypatch, missing_owner
):
    from yizkr.pipeline import textlayer

    corpus, name = container_corpus(tmp_path, "pdf")
    first = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    original = document(first, name)
    excerpt = excerpt_of(first, LETTER_SHA1)
    inventory = read_inventory(original.dir)
    assert inventory is not None
    assert json.loads(original.transcription.read_text())["source_context"]
    (original.dir / CONTAINER_INVENTORY).unlink()
    (original.dir / DERIVED_MEDIA).unlink(missing_ok=True)
    if missing_owner:
        # The original PDF still exists, but no retained canonical PNG does.
        for directory in (
            "derived-media",
            "representations",
            "container-parts",
        ):
            for path in (original.dir / directory).glob(f"{LETTER_SHA1}.*"):
                if not path.name.endswith(".transcription.json"):
                    path.unlink()
    decode = textlayer.image_bytes

    def changed_encoding(image):
        data, suffix = decode(image)
        if ArtifactRef.from_bytes(data).sha1 == LETTER_SHA1:
            return reencoded_png(data), suffix
        return data, suffix

    monkeypatch.setattr(textlayer, "image_bytes", changed_encoding)
    again = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    restored = document(again, name)
    sources = again._container_sources(restored)
    assert sources is not None
    if missing_owner:
        assert sources.sources == {}
        assert any("canonical" in item.reason for item in sources.unresolved)
        assert again._excerpts == {}
    else:
        assert set(sources.sources) == {LETTER_SHA1}
        assert excerpt_of(again, LETTER_SHA1).dir == excerpt.dir
        assert (
            sources.sources[LETTER_SHA1].current_input.representation.sha1
            != LETTER_SHA1
        )
    assert {path.name for path in excerpt.dir.parent.iterdir()} == {
        LETTER_SHA1
    }


def test_ambiguous_legacy_owner_mapping_stays_unresolved(tmp_path):
    compressed = pdfs.Image(
        LETTER_IMAGE.entries + " /Filter /FlateDecode",
        zlib.compress(LETTER_IMAGE.data),
    )
    page = typed(CAPTION).image(LETTER_IMAGE, 72, 400, 300, 120)
    page.image(compressed, 72, 200, 300, 120)
    plan = inventory_pdf(pdfs.pdf([page]), lambda _: False)
    originals = [
        part
        for part in plan.inventory.parts
        if part.kind == "embedded_resource"
    ]
    assert len({part.byte_object.sha1 for part in originals}) == 2
    assert {
        rep.byte_object.sha1
        for part in originals
        for rep in part.representations
    } == {LETTER_SHA1}
    path = tmp_path / "derived-media" / f"{LETTER_SHA1}.png"
    path.parent.mkdir()
    path.write_bytes(plan.data[LETTER_SHA1])
    legacy = DerivedMediaRecord(
        source_sha1=plan.inventory.source.sha1,
        container="pdf",
        title="Our Family from Germany",
        media={
            LETTER_SHA1: DerivedMedium(
                names=["page 1, image 1", "page 1, image 2"],
                slides=[1],
                file=path.relative_to(tmp_path).as_posix(),
            )
        },
    )
    prepared = prepare_sources(
        plan.inventory, plan.data, tmp_path, legacy=legacy
    )
    projected = project_sources(prepared, tmp_path, data=plan.data)
    assert projected.sources == {}
    assert len(projected.unresolved) == 2
    assert all(
        "multiple distinct source parts" in item.reason
        for item in projected.unresolved
    )


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_both_formats_keep_repeats_and_refuse_excluded_or_unplaced_sources(
    tmp_path, kind
):
    other = pdfs.gray(300, 120, shade=9)
    other_png = png(300, 120, 1, other.data)
    excluded_sha1 = ArtifactRef.from_bytes(other_png).sha1
    if kind == "pdf":
        page = typed(CAPTION).image(LETTER_IMAGE, 72, 400, 300, 120)
        page.ops.append(b"q 300 0 0 120 72 250 cm /Im1 Do Q")
        page.image(other, 72, 100, 300, 120)
        page.images["Unused"] = pdfs.jpeg()
        plan = inventory_pdf(pdfs.pdf([page]), lambda _: False)
        inventory, data = plan.inventory, plan.data
    else:
        path = tmp_path / "library-talk-2018.pptx"
        deck(
            path,
            [
                (
                    (CAPTION,),
                    ["letter.png", "excluded.png", "unused.jpg"],
                    NOTES,
                )
            ],
            {
                "letter.png": png(300, 120, 1, LETTER_IMAGE.data),
                "excluded.png": other_png,
                "unused.jpg": pdfs.JPEG,
            },
            painted={1: ["letter.png", "letter.png", "excluded.png"]},
        )
        content = unpack_pptx(path)
        inventory, data = content.inventory, content.inventory_bytes
    before = [placement.id for placement in inventory.placements]
    prepared = prepare_sources(
        inventory,
        data,
        tmp_path,
        excluded=lambda sha1, names=(): sha1 == excluded_sha1,
    )
    projected = project_sources(prepared, tmp_path, data=data)
    assert [placement.id for placement in prepared.placements] == before
    assert set(projected.sources) == {LETTER_SHA1}
    source = projected.sources[LETTER_SHA1]
    assert len(source.placements) == 2
    assert len({placement.id for placement in source.placements}) == 2
    assert (
        len({placement.locator.path for placement in source.placements}) == 2
    )
    context = SourceContext(
        container=prepared.source,
        placements=[
            PlacedSource(
                owner=source.owner, support=projected.support(part_id)
            )
            for part_id in source.part_ids
        ],
    )
    reading = paged(prepared.source, [(1, [], CAPTION)]).model_copy(
        update={"source_context": context}
    )
    lines = placed_lines(reading)
    assert len(lines) == 2
    assert all(LETTER_SHA1 in line for line in lines)
    assert all(
        any(placement.id in line for line in lines)
        for placement in source.placements
    )
