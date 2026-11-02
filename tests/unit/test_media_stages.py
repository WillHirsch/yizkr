"""Shared media operations retain the existing readers' evidence and inputs.

Real fictional PDF/PPTX structures exercise preparation and image admission.
Only the model answers are authored; no provider or graph is constructed.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixtures import pdfs
from tests.fixtures.records import record_answer
from tests.unit.test_deck_excerpts import LETTER_PNG, deck
from tests.unit.test_pdf_pages import (
    EXCERPT,
    LETTER_SHA1,
    REFUSAL,
    TITLE,
    booklet,
    renderer,  # noqa: F401
    typed,
    vision_page,
)
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    Eligibility,
    ReadingOutcome,
    keep_inventory,
)
from yizkr.pipeline.container_sources import project_sources, reading_file
from yizkr.pipeline.llm import PassPaused
from yizkr.pipeline.media_stages import (
    ExcerptReadRequest,
    MediaReadRequest,
    PreparationRequest,
    excerpt_dependencies,
    preparation_dependencies,
    prepare_container,
    read_excerpt,
    read_media,
    verify_image_reading,
)
from yizkr.pipeline.textlayer import PdfExclusionError
from yizkr.pipeline.transcribe import (
    DeckTranscriber,
    PdfPageTranscriber,
    Transcription,
    reading_digest,
)


class AuthoredReader:
    model_identifier = "fictional-media-reader"
    config_hash = "authored-media-configuration"

    def __init__(self, answer=None):
        self.answer = answer if answer is not None else record_answer(EXCERPT)
        self.inputs = []
        self.closed = False

    def complete(self, prompt, image_paths=()):
        self.inputs.append(
            (prompt, [Path(p).read_bytes() for p in image_paths])
        )
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def reject(self, _answer, _reason):
        return False

    def close(self):
        self.closed = True


def no_client():
    raise AssertionError("this operation must not construct a model client")


def allowed(_sha1, _names=()):
    return False


def container(tmp_path, kind, *, images=True):
    path = tmp_path / f"family-booklet-2011.{kind}"
    if kind == "pdf":
        path.write_bytes(booklet() if images else pdfs.pdf([typed(TITLE)]))
    else:
        deck(
            path,
            [((TITLE,), ["letter.png"] if images else [], "")],
            {"letter.png": LETTER_PNG} if images else {},
        )
    return path


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_native_reading_is_lazy_and_preparation_does_not_publish(
    tmp_path, kind
):
    path = container(tmp_path, kind, images=False)
    output = tmp_path / "products"
    original = path.read_bytes()
    prepared = prepare_container(
        PreparationRequest(path, existing_directory=output)
    )
    assert not output.exists()
    before = preparation_dependencies(prepared)
    with read_media(
        MediaReadRequest(path, "paged", language="en", prepared=prepared),
        no_client,
    ) as result:
        assert TITLE in result.transcription.text
        assert result.transcription.reading is None
        assert result.products is not None
        folder = Path(result.products.folder.name)
        assert folder.is_dir()
        assert result.prepared is prepared
        assert result.require_complete() is result.transcription
        assert result.status == "complete" and result.artifacts == ()
        assert "model" not in result.dependencies
        assert before == preparation_dependencies(prepared)
    assert not folder.exists()
    assert path.read_bytes() == original
    assert not output.exists()


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_shared_reading_matches_adapter_inputs_and_products(tmp_path, kind):
    path = container(tmp_path, kind)
    direct_client = AuthoredReader()
    adapter = PdfPageTranscriber if kind == "pdf" else DeckTranscriber
    direct = adapter(
        lambda: direct_client, language="en", excluded=allowed
    ).transcribe(path)
    client = AuthoredReader()
    try:
        with read_media(
            MediaReadRequest(path, "paged", language="en", excluded=allowed),
            lambda: client,
        ) as result:
            assert result.transcription.model_dump(
                exclude={"provenance"}
            ) == direct.model_dump(exclude={"provenance"})
            assert result.transcription.provenance is not None
            assert client.inputs == direct_client.inputs
            assert result.products is not None
            assert [image.sha1 for image in result.products.images] == [
                LETTER_SHA1
            ]
            assert [
                image.transcription.model_dump(exclude={"provenance"})
                for image in result.products.images
            ] == [
                image.transcription.model_dump(exclude={"provenance"})
                for image in direct.reading.images
            ]
            assert result.transcription.reading is None
            assert (
                result.dependencies["model"]["model_identifier"]
                == client.model_identifier
            )
            assert not client.closed
        assert not client.closed
    finally:
        direct.reading.close()


@pytest.mark.parametrize("route", ["paged", "vision"])
def test_identity_and_later_page_exclusion_refuse_before_client(
    tmp_path, route
):
    path = tmp_path / "family-booklet-2011.pdf"
    later = pdfs.Page().image(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
    later.frame(20, 20, 500, 700)
    path.write_bytes(pdfs.pdf([typed(TITLE), vision_page(), later]))
    request = MediaReadRequest(
        path, route, expected_artifact=ArtifactRef.from_bytes(b"other")
    )
    with pytest.raises(ValueError, match="no longer matches"):
        read_media(request, no_client)
    blocked = hashlib.sha1(pdfs.JPEG).hexdigest()
    with pytest.raises(PdfExclusionError, match="exclu"):
        read_media(
            replace(
                request,
                expected_artifact=None,
                excluded=lambda sha, names: sha == blocked,
            ),
            no_client,
        )


def test_prepared_other_source_refuses_before_client(tmp_path):
    first = container(tmp_path, "pdf", images=False)
    prepared = prepare_container(PreparationRequest(first))
    other = tmp_path / "birth-extract-1938.pdf"
    other.write_bytes(pdfs.pdf([vision_page()]))
    with pytest.raises(ValueError, match="another source"):
        read_media(
            MediaReadRequest(other, "vision", prepared=prepared), no_client
        )


def test_paged_fallback_retains_checked_pdf_bytes_and_source_owner(tmp_path):
    path = tmp_path / "birth-extract-1938.pdf"
    path.write_bytes(pdfs.pdf([vision_page()]))
    client = AuthoredReader()
    with read_media(
        MediaReadRequest(path, "paged", excluded=allowed), lambda: client
    ) as result:
        assert result.transcription.artifact == ArtifactRef.from_path(path)
        assert result.transcription.input_representation == "rendered-pdf-v1"
        assert len(client.inputs) == 1
        assert client.inputs[0][1][0].startswith(b"%PDF-")
        assert client.inputs[0][1][0] != path.read_bytes()
        assert result.products is None


def test_explicit_unconfigured_whole_vision_keeps_original_input(tmp_path):
    path = tmp_path / "birth-extract-1938.pdf"
    path.write_bytes(pdfs.pdf([vision_page()]))
    client = AuthoredReader()
    with read_media(
        MediaReadRequest(path, "vision"), lambda: client
    ) as result:
        assert client.inputs[0][1] == [path.read_bytes()]
        assert result.transcription.input_representation is None


def test_factory_pause_propagates_without_an_accepted_result(tmp_path):
    path = container(tmp_path, "pptx")
    pause = PassPaused("paused")

    def paused():
        raise pause

    with pytest.raises(PassPaused) as caught:
        read_media(MediaReadRequest(path, "paged"), paused)
    assert caught.value is pause


def test_archive_vision_refuses_before_client(tmp_path):
    path = container(tmp_path, "pptx")
    with pytest.raises(ValueError, match="never sent whole"):
        read_media(MediaReadRequest(path, "vision"), no_client)


def test_failed_image_reading_returns_attention_and_explicit_products(
    tmp_path,
):
    path = container(tmp_path, "pptx")
    client = AuthoredReader(REFUSAL)
    with read_media(MediaReadRequest(path, "paged"), lambda: client) as result:
        assert TITLE in result.transcription.text
        (image,) = result.products.images
        assert image.transcription is None
        assert image.failure
        assert result.attention == result.products.attention()
        assert result.attention
        assert result.transcription.reading is None


def test_interrupted_reading_cleans_temporary_products(tmp_path, monkeypatch):
    import tempfile

    from yizkr.pipeline import transcribe

    path = container(tmp_path, "pptx")
    original = tempfile.TemporaryDirectory
    created = []

    def workspace(*args, **kwargs):
        folder = original(*args, **kwargs)
        created.append(Path(folder.name))
        return folder

    monkeypatch.setattr(transcribe.tempfile, "TemporaryDirectory", workspace)
    client = AuthoredReader(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        read_media(MediaReadRequest(path, "paged"), lambda: client)
    assert created and all(not folder.exists() for folder in created)
    assert not client.closed


def saved_reading(tmp_path, *, kind="pptx"):
    path = container(tmp_path, kind)
    directory = tmp_path / "products"
    prepared = prepare_container(
        PreparationRequest(path, existing_directory=directory)
    )
    client = AuthoredReader()
    with read_media(
        MediaReadRequest(path, "paged", prepared=prepared, language="en"),
        lambda: client,
    ) as result:
        (image,) = result.products.images
        reading = image.transcription
        name = reading_file(image.sha1)
        payload = reading.model_dump_json().encode()
        for part in prepared.inventory.parts:
            if (
                part.owner_binding
                and part.owner_binding.artifact.sha1 == image.sha1
            ):
                part.reading = ReadingOutcome(
                    state="read",
                    method=reading.engine,
                    empty=False,
                    product=name,
                    product_sha256=hashlib.sha256(payload).hexdigest(),
                )
        keep_inventory(prepared.inventory, prepared.data, directory)
        (directory / name).parent.mkdir(exist_ok=True)
        (directory / name).write_bytes(payload)
    projection = project_sources(
        prepared.inventory, directory, data=prepared.data
    )
    return path, directory, prepared, projection


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_verified_excerpt_preserves_text_and_adds_current_support(
    tmp_path, kind
):
    _path, directory, prepared, projection = saved_reading(tmp_path, kind=kind)
    check = verify_image_reading(projection, LETTER_SHA1, directory)
    assert check.failure is None and not check.missing
    verified = check.reading
    before = verified.transcription.model_dump()
    result = read_excerpt(
        ExcerptReadRequest(verified.source.owner, [verified], language="de")
    )
    assert result.transcription.text == verified.transcription.text
    assert result.transcription.pages == verified.transcription.pages
    assert result.transcription.language == "de"
    assert verified.transcription.model_dump() == before
    assert result.products is None
    located = result.transcription.source_context.readings
    assert located and all(
        item.owner == verified.source.owner for item in located
    )
    assert all(item.support.container == prepared.source for item in located)
    assert all(
        item.support.reading_sha256 == reading_digest(result.transcription)
        for item in located
    )


def test_image_verification_separates_absence_tampering_and_ineligibility(
    tmp_path,
):
    _path, directory, prepared, projection = saved_reading(tmp_path)
    path = directory / reading_file(LETTER_SHA1)
    original = path.read_bytes()
    path.unlink()
    missing = verify_image_reading(None, LETTER_SHA1, directory)
    assert (
        missing.missing and missing.failure is None and missing.reading is None
    )
    path.write_bytes(original)
    assert (
        "eligible"
        in verify_image_reading(None, LETTER_SHA1, directory).failure
    )
    kept = Transcription.model_validate_json(original)
    kept.engine = "changed-process-marker"
    path.write_text(kept.model_dump_json())
    assert (
        "has changed"
        in verify_image_reading(projection, LETTER_SHA1, directory).failure
    )
    assert verify_image_reading(
        projection, LETTER_SHA1, directory, verify_product=False
    ).reading
    path.write_bytes(original)
    excluded = project_sources(
        prepared.inventory,
        directory,
        excluded=lambda sha, names: sha == LETTER_SHA1,
    )
    assert (
        "eligible"
        in verify_image_reading(excluded, LETTER_SHA1, directory).failure
    )


def test_preparation_and_excerpt_dependencies_ignore_published_outcomes(
    tmp_path,
):
    source = container(tmp_path, "pdf")
    directory = tmp_path / "products"
    prepared = prepare_container(
        PreparationRequest(source, existing_directory=directory)
    )
    initial = preparation_dependencies(prepared)
    client = AuthoredReader()
    with read_media(
        MediaReadRequest(source, "paged", prepared=prepared), lambda: client
    ):
        assert preparation_dependencies(prepared) == initial
    part = next(
        part for part in prepared.inventory.parts if part.owner_binding
    )
    part.reading = ReadingOutcome(
        state="read", method="fictional-reader", empty=False
    )
    assert preparation_dependencies(prepared) == initial
    part.eligibility = Eligibility(state="excluded", reason="current policy")
    assert preparation_dependencies(prepared) != initial

    other = tmp_path / "other"
    other.mkdir()
    _source, directory, _prepared, projection = saved_reading(other)
    verified = verify_image_reading(projection, LETTER_SHA1, directory).reading
    first = excerpt_dependencies(verified)
    verified.transcription.engine = "other-process-marker"
    assert excerpt_dependencies(verified) == first


def test_ordered_excerpt_candidates_retain_selected_parent_and_skip_drift(
    tmp_path,
):
    candidates = []
    containers = []
    for kind in ("pdf", "pptx"):
        root = tmp_path / kind
        root.mkdir()
        _path, directory, prepared, projection = saved_reading(root, kind=kind)
        candidates.append(
            verify_image_reading(projection, LETTER_SHA1, directory).reading
        )
        containers.append(prepared.source)
    owner = ArtifactRef(sha1=LETTER_SHA1)
    first = read_excerpt(ExcerptReadRequest(owner, candidates))
    assert first.transcription.source_context.container == containers[0]
    reversed_order = read_excerpt(
        ExcerptReadRequest(owner, list(reversed(candidates)))
    )
    assert (
        reversed_order.transcription.source_context.container == containers[1]
    )
    assert first.transcription.text == reversed_order.transcription.text

    # A verified object can be held while the caller changes its input plan.
    # It must not turn that drift into an accepted stale excerpt.
    candidates[0].transcription.reading_input = ArtifactRef.from_bytes(
        b"changed input"
    )
    fallback = read_excerpt(ExcerptReadRequest(owner, candidates))
    assert fallback.transcription.source_context.container == containers[1]
    candidates[1].source.readable = False
    with pytest.raises(RuntimeError, match="passes the transcription check"):
        read_excerpt(ExcerptReadRequest(owner, candidates))


def test_damaged_canonical_owner_cannot_admit_saved_reading(tmp_path):
    _path, directory, prepared, projection = saved_reading(tmp_path)
    source = projection.sources[LETTER_SHA1]
    (directory / source.file).write_bytes(b"damaged canonical image")
    current = project_sources(prepared.inventory, directory)
    check = verify_image_reading(current, LETTER_SHA1, directory)
    assert check.reading is None and "eligible" in check.failure


def test_recovered_binding_proof_does_not_change_reading_dependencies(
    tmp_path,
):
    _path, _directory, prepared, _projection = saved_reading(tmp_path)
    before = preparation_dependencies(prepared)
    part = next(
        part for part in prepared.inventory.parts if part.owner_binding
    )
    part.owner_binding.basis = "verified-legacy-reading"
    assert preparation_dependencies(prepared) == before
    part.owner_binding.artifact = ArtifactRef.from_bytes(b"other owner")
    assert preparation_dependencies(prepared) != before
