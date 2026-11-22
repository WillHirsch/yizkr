"""Saved document views enter review only when complete and consistent.

The production scheduler saves each document's views from fixed fictional
answers (tests.fixtures.saved_products). Each test then changes one thing that
a later run, a curator or a copy could change, and admits through the public
loader. Admission writes nothing and calls no model.
"""

import hashlib
import json
import shutil

import pytest

from tests.fixtures.document_processing import contents
from tests.fixtures.saved_products import (
    GRAVESTONE_FILE,
    claim_answer,
    gravestone_answers,
    gravestone_claim,
    gravestone_image,
    saved_document,
)
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList, read_exclusions
from yizkr.pipeline.critique import entries
from yizkr.pipeline.document_products import (
    ProductsRefused,
    UnsupportedKind,
    load_document_products,
)
from yizkr.pipeline.document_state import (
    EXCERPTS_DIR,
    PAUSED,
    TRANSCRIPTION_VOLATILE,
    DocumentState,
)
from yizkr.pipeline.source_context import SourceContext
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.transcribe import Transcription

FINDING = {
    "entity": "eugen",
    "axis": "subject",
    "category": "wrong_kind",
    "issue": "Check the fictional inscription's name.",
}


def gravestone(directory, answers=(), **options):
    data = gravestone_image()
    sha1 = hashlib.sha1(data).hexdigest()
    return saved_document(
        directory,
        GRAVESTONE_FILE,
        data,
        gravestone_answers(sha1) | dict(answers),
        **options,
    )


@pytest.fixture
def image(tmp_path):
    status, doc = gravestone(
        tmp_path, {"critique": [json.dumps(FINDING) + '\n{"done":true}']}
    )
    assert status == 0
    return doc


def admit(doc, source=None, exclusions=None):
    return load_document_products(
        source or doc.source,
        doc.dir,
        exclusions=ExclusionList() if exclusions is None else exclusions,
    )


def test_complete_saved_views_are_admitted_unchanged(image):
    before = contents(image.dir.parent)
    products = admit(image)
    assert contents(image.dir.parent) == before
    assert products.kind == "media"
    assert products.artifact == ArtifactRef.from_path(image.source)
    assert products.source_sha256 == (
        hashlib.sha256(image.source.read_bytes()).hexdigest()
    )
    assert products.reading == Transcription.model_validate_json(
        image.transcription.read_text()
    )
    assert products.batch == ClaimBatch.model_validate_json(
        image.batch.read_text()
    )
    (finding,) = products.critique.findings
    assert finding.issue == FINDING["issue"]
    assert products.critique.findings == tuple(
        entries(json.loads(image.critique.read_text()))
    )
    attention = image.state.detail["attention"]
    assert products.extraction_attention == tuple(attention["extraction"])
    assert products.critique.attention == tuple(attention.get("critique", ()))
    again = admit(image)
    assert again == products and again.digest == products.digest


def test_a_renamed_copy_of_the_source_keeps_its_products(image, tmp_path):
    copy = tmp_path / "copied-gravestone.png"
    shutil.copyfile(image.source, copy)
    assert admit(image, copy) == admit(image)


def test_a_copy_named_as_another_admitted_kind_refuses(image, tmp_path):
    # The same bytes named as plain text are not the image the run read.
    copy = tmp_path / "copied-gravestone.txt"
    shutil.copyfile(image.source, copy)
    with pytest.raises(ProductsRefused):
        admit(image, copy)


def test_source_bytes_that_differ_from_the_saved_artifact_refuse(image):
    image.source.write_bytes(image.source.read_bytes() + b"fictional edit")
    with pytest.raises(ProductsRefused):
        admit(image)


@pytest.mark.parametrize(
    "listed", ["sha1", "current name", "recorded name", "after reading"]
)
def test_an_excluded_source_refuses(image, tmp_path, listed):
    policy = tmp_path / "exclusions.txt"
    policy.write_text("")
    source = image.source
    sha1 = ArtifactRef.from_path(source).sha1
    if listed == "current name":
        source = source.rename(source.with_name("fictional-copy.png"))
        policy.write_text("fictional-copy.png\n")
    elif listed == "recorded name":
        source = source.rename(source.with_name("fictional-copy.png"))
        policy.write_text(f"{GRAVESTONE_FILE}\n")
    elif listed == "sha1":
        policy.write_text(f"{sha1}\n")
    exclusions = read_exclusions(policy)
    if listed == "after reading":
        # The list is read again from its file, not taken as first read.
        policy.write_text(f"{sha1}\n")
    with pytest.raises(ProductsRefused):
        admit(image, source, exclusions)


def test_an_unfinished_critique_is_saved_but_refused(tmp_path):
    status, doc = gravestone(tmp_path, {"critique": ["an unfinished answer"]})
    assert status == 0
    assert doc.critique.is_file()
    assert doc.state.detail["stage_results"]["critique"]["status"] == (
        "incomplete"
    )
    with pytest.raises(ProductsRefused):
        admit(doc)


def test_an_incomplete_refresh_refuses_the_earlier_batch_it_leaves(
    image, tmp_path
):
    sha1 = ArtifactRef.from_path(image.source).sha1
    batch = image.batch.read_bytes()
    extraction = gravestone_answers(sha1)["extract"][:1] + [
        claim_answer(gravestone_claim(sha1), finished=False)
    ]
    _status, doc = gravestone(
        tmp_path, {"extract": extraction}, refresh=("extract",)
    )
    assert doc.batch.read_bytes() == batch
    assert doc.state.detail["stage_results"]["extraction"]["status"] != (
        "complete"
    )
    with pytest.raises(ProductsRefused):
        admit(doc)


def test_a_document_the_run_left_unbatched_refuses(image):
    state = DocumentState.load(image.state_path)
    state.status = PAUSED
    state.save(image.state_path)
    with pytest.raises(ProductsRefused):
        admit(image)


def _reread(image, recorded: bool) -> None:
    """Replace the saved reading by a different one of the same shape,
    as if it had been made again after its batch."""
    reading = Transcription.model_validate_json(
        image.transcription.read_text()
    )
    changed = reading.model_copy(
        update={"text": reading.text.replace("1836", "1837")}
    )
    image.transcription.write_text(changed.model_dump_json(indent=2))
    if recorded:
        image.derivations.set_content(
            image.transcription.name,
            content_hash(
                changed.model_dump(mode="json"),
                volatile=TRANSCRIPTION_VOLATILE,
            ),
        )


@pytest.mark.parametrize(
    "change",
    [
        "reading made after its batch",
        "reading unlike its record",
        "critique of another batch",
    ],
)
def test_views_not_made_from_the_saved_reading_refuse(image, change):
    if change == "critique of another batch":
        image.derivations.record(
            image.critique,
            inputs={
                image.batch.name: "0" * 64,
                image.transcription.name: image.derivations.content(
                    image.transcription.name
                ),
            },
        )
    else:
        _reread(image, recorded=change == "reading made after its batch")
    with pytest.raises(ProductsRefused):
        admit(image)


@pytest.mark.parametrize(
    ("kind", "name", "data"),
    [
        ("pdf", "gravestone-photo-1956.pdf", None),
        ("pdf", GRAVESTONE_FILE, b"%PDF-1.7\nfictional gravestone scan\n"),
        ("presentation", "gravestone-photo-1956.pptx", None),
        ("gedcom", "gravestone-photo-1956.ged", None),
    ],
)
def test_container_and_genealogy_sources_refuse_with_a_reason(
    image, kind, name, data
):
    source = image.source.rename(image.source.with_name(name))
    if data is not None:
        source.write_bytes(data)
    with pytest.raises(UnsupportedKind) as refused:
        admit(image, source)
    assert refused.value.kind == kind
    assert str(refused.value)


@pytest.mark.parametrize("found", ["by its place", "by its reading"])
def test_an_excerpt_refuses_with_a_reason(image, found):
    sha1 = ArtifactRef.from_path(image.source).sha1
    directory = image.dir
    if found == "by its place":
        directory = image.dir.parent / EXCERPTS_DIR / sha1
        shutil.copytree(image.dir, directory)
    else:
        # A reading located in another source's container, wherever its
        # directory is kept, as an excerpt's reading is.
        reading = Transcription.model_validate_json(
            image.transcription.read_text()
        )
        container = ArtifactRef.from_bytes(b"fictional family booklet")
        located = reading.model_copy(
            update={"source_context": SourceContext(container=container)}
        )
        image.transcription.write_text(located.model_dump_json(indent=2))
        image.derivations.set_content(
            image.transcription.name,
            content_hash(
                located.model_dump(mode="json"),
                volatile=TRANSCRIPTION_VOLATILE,
            ),
        )
    with pytest.raises(UnsupportedKind) as refused:
        load_document_products(
            image.source, directory, exclusions=ExclusionList()
        )
    assert refused.value.kind == "excerpt"
    assert str(refused.value)
