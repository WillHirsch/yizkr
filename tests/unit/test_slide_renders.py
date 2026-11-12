"""Render products preserve source lineage and re-admit every use."""

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from PIL import Image

from tests.unit.test_slide_admission import deck
from yizkr.claims import ArtifactRef
from yizkr.pipeline import slide_renders as renders
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
)
from yizkr.pipeline.container_inventory import (
    MediaOwnerBinding,
    read_inventory,
)
from yizkr.pipeline.slide_renderer import (
    RenderedSlide,
    SlideRenderResult,
    SlideRenderSettings,
)


def allow(_sha1, _names=()):
    return False


@pytest.fixture
def backend(monkeypatch):
    calls = []

    def identity(settings):
        return {"renderer": "fictional", "long_edge": settings.long_edge}

    def render(original, *, expected_slides, settings):
        calls.append(original)
        pages = []
        for position in range(1, expected_slides + 1):
            output = io.BytesIO()
            Image.new("RGB", (20, 10), (position, 0, 0)).save(output, "PNG")
            pages.append(RenderedSlide(position, output.getvalue(), 20, 10))
        return SlideRenderResult(tuple(pages), identity(settings))

    monkeypatch.setattr(renders.slide_renderer, "renderer_identity", identity)
    monkeypatch.setattr(renders.slide_renderer, "render_slides", render)
    return calls


def prepare(tmp_path, **kwargs):
    source = tmp_path / "source.pptx"
    if not source.exists():
        deck(source)
    return renders.prepare_slide_renders(
        source, tmp_path / "products", excluded=allow, **kwargs
    )


def test_renders_remain_representations_with_separate_notes(tmp_path, backend):
    product = prepare(tmp_path)
    inventory = read_inventory(tmp_path / "products")
    parts = {p.id: p for p in inventory.parts}
    assert [s.admission.position for s in product.slides] == [1, 2]
    for slide in product.slides:
        part = parts[slide.admission.part_id]
        assert part.kind == "slide_body"
        assert part.owner_binding is None
        assert part.reading.method == "slide-xml"
        assert slide.representation in part.representations
        for image in slide.admission.image_part_ids:
            assert parts[image].source_role == "independent"
            assert parts[image].owner_binding is not None
    assert any(p.kind == "speaker_notes" for p in inventory.parts)
    assert all(
        not p.representations
        for p in inventory.parts
        if p.kind == "speaker_notes"
    )
    assert prepare(tmp_path) == product
    assert len(backend) == 1


def test_current_exclusion_blocks_previously_rendered_image(tmp_path, backend):
    product = prepare(tmp_path)
    inventory = read_inventory(tmp_path / "products")
    image = next(
        p
        for p in inventory.parts
        if p.id in product.slides[0].admission.image_part_ids
    )
    with pytest.raises(ValueError, match="exclud|eligible|unresolved"):
        renders.prepare_slide_renders(
            tmp_path / "source.pptx",
            tmp_path / "products",
            excluded=lambda digest, names: digest == image.byte_object.sha1,
        )
    assert len(backend) == 1


def test_filename_policy_is_rechecked_on_reuse(tmp_path, backend):
    prepare(tmp_path)
    with pytest.raises(ValueError, match="exclud"):
        renders.prepare_slide_renders(
            tmp_path / "source.pptx",
            tmp_path / "products",
            excluded=lambda digest, names: "source.pptx" in names,
        )
    assert len(backend) == 1


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("by_name", [False, True])
def test_rendered_output_exclusions_apply_before_publication_and_reuse(
    tmp_path, backend, reuse, by_name
):
    product = prepare(tmp_path)
    rep = product.slides[0].representation
    directory = tmp_path / ("products" if reuse else "new-products")

    def excluded(sha1, names):
        return rep.file in names if by_name else sha1 == rep.byte_object.sha1

    with pytest.raises(ValueError, match="rendered slide is excluded"):
        renders.prepare_slide_renders(
            tmp_path / "source.pptx", directory, excluded=excluded
        )
    if not reuse:
        assert not (directory / renders.RENDER_RECORD).exists()
        assert not (directory / rep.file).exists()
    assert len(backend) == (1 if reuse else 2)


def test_renderer_settings_invalidate_product(tmp_path, backend):
    first = prepare(tmp_path)
    second = prepare(tmp_path, settings=SlideRenderSettings(long_edge=800))
    assert second.basis_sha256 != first.basis_sha256
    assert second.renderer["long_edge"] == 800
    assert len(backend) == 2


@pytest.mark.parametrize(
    "damage", ["output", "record", "inventory", "missing"]
)
def test_damaged_products_are_not_reused(tmp_path, backend, damage):
    first = prepare(tmp_path)
    directory = tmp_path / "products"
    path = directory / first.slides[0].representation.file
    if damage == "missing":
        path.unlink()
    elif damage == "record":
        (directory / renders.RENDER_RECORD).write_text("{}")
    elif damage == "inventory":
        inventory = read_inventory(directory)
        for p in inventory.parts:
            if p.kind == "slide_body":
                p.representations = []
        (directory / "container-inventory.json").write_text(
            inventory.model_dump_json()
        )
    else:
        path.write_bytes(b"damaged")
    second = prepare(tmp_path)
    assert second == first
    assert len(backend) == 2


def test_changed_input_never_publishes_old_render(
    tmp_path, backend, monkeypatch
):
    original_render = renders.slide_renderer.render_slides

    def changed(original, **kwargs):
        result = original_render(original, **kwargs)
        (tmp_path / "source.pptx").write_bytes(original + b"changed")
        return result

    monkeypatch.setattr(renders.slide_renderer, "render_slides", changed)
    with pytest.raises(ValueError, match="changed during rendering"):
        prepare(tmp_path)
    assert not (tmp_path / "products" / renders.RENDER_RECORD).exists()


def test_policy_change_during_render_prevents_publication(
    tmp_path, backend, monkeypatch
):
    source = deck(tmp_path / "source.pptx")
    blocked = False
    original_render = renders.slide_renderer.render_slides

    def changed(original, **kwargs):
        nonlocal blocked
        result = original_render(original, **kwargs)
        blocked = True
        return result

    monkeypatch.setattr(renders.slide_renderer, "render_slides", changed)
    with pytest.raises(ValueError, match="exclud"):
        renders.prepare_slide_renders(
            source, tmp_path / "products", excluded=lambda *_: blocked
        )
    assert not (tmp_path / "products" / renders.RENDER_RECORD).exists()


@pytest.mark.parametrize("fault", ["count", "order", "identity"])
def test_partial_or_changed_renderer_result_is_refused(
    tmp_path, backend, monkeypatch, fault
):
    original_render = renders.slide_renderer.render_slides

    def damaged(original, **kwargs):
        result = original_render(original, **kwargs)
        if fault == "identity":
            return replace(result, renderer={"renderer": "changed"})
        pages = result.pages[:1] if fault == "count" else result.pages[::-1]
        return replace(result, pages=pages)

    monkeypatch.setattr(renders.slide_renderer, "render_slides", damaged)
    with pytest.raises(ValueError, match="renderer"):
        prepare(tmp_path)
    assert not (tmp_path / "products" / renders.RENDER_RECORD).exists()


def test_interrupted_publication_requires_intact_generation(
    tmp_path, backend, monkeypatch
):
    prepare(tmp_path)
    old_keep = renders._keep

    def interrupted(directory, relative, payload):
        if relative == renders.RENDER_RECORD:
            raise OSError("fictional disk failure")
        old_keep(directory, relative, payload)

    with monkeypatch.context() as patch:
        patch.setattr(renders, "_keep", interrupted)
        with pytest.raises(OSError, match="fictional"):
            prepare(tmp_path, settings=SlideRenderSettings(long_edge=800))
    recovered = prepare(tmp_path, settings=SlideRenderSettings(long_edge=800))
    assert recovered.renderer["long_edge"] == 800
    assert len(backend) == 3


def test_linked_output_directory_is_refused(tmp_path, backend):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    products = tmp_path / "products"
    products.mkdir()
    (products / "slide-renders").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe parent"):
        prepare(tmp_path)
    assert list(outside.iterdir()) == []


def bind_legacy_owner(directory, product, *, missing_owner=False):
    inventory = read_inventory(directory)
    image = next(
        p
        for p in inventory.parts
        if p.id in product.slides[0].admission.image_part_ids
    )
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(output, "PNG")
    canonical = output.getvalue()
    owner = ArtifactRef.from_bytes(canonical)
    name = f"representations/{owner.sha1}.png"
    (directory / name).parent.mkdir(exist_ok=True)
    if not missing_owner:
        (directory / name).write_bytes(canonical)
    image.owner_binding = MediaOwnerBinding(
        artifact=owner,
        file=name,
        media_type="image/png",
        basis="verified-legacy-reading",
    )
    (directory / "container-inventory.json").write_text(
        inventory.model_dump_json()
    )
    return image.id, owner


@pytest.mark.parametrize("missing_owner", [False, True])
def test_damaged_render_never_reallocates_existing_owner(
    tmp_path, backend, missing_owner
):
    product = prepare(tmp_path)
    directory = tmp_path / "products"
    part_id, owner = bind_legacy_owner(
        directory, product, missing_owner=missing_owner
    )
    (directory / product.slides[0].representation.file).write_bytes(b"damaged")
    if missing_owner:
        with pytest.raises(ValueError, match="unresolved|eligible"):
            prepare(tmp_path)
        assert len(backend) == 1
    else:
        prepare(tmp_path)
        recovered = read_inventory(directory)
        assert (
            next(
                p for p in recovered.parts if p.id == part_id
            ).owner_binding.artifact
            == owner
        )
        assert len(backend) == 2


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_unavailable_owner_metadata_refuses_regeneration(
    tmp_path, backend, damage
):
    product = prepare(tmp_path)
    directory = tmp_path / "products"
    bind_legacy_owner(directory, product)
    inventory = directory / "container-inventory.json"
    if damage == "missing":
        inventory.unlink()
    else:
        inventory.write_text("{}")
    saved = (directory / renders.RENDER_RECORD).read_bytes()
    with pytest.raises(ValueError, match="prior source bindings"):
        prepare(tmp_path)
    assert (directory / renders.RENDER_RECORD).read_bytes() == saved
    assert len(backend) == 1


@pytest.mark.parametrize("stop_at", ["render", "inventory"])
def test_cancellation_prevents_completed_product(
    tmp_path, backend, monkeypatch, stop_at
):
    cancellation = Cancellation()
    target = renders.slide_renderer if stop_at == "render" else renders
    name = "render_slides" if stop_at == "render" else "keep_inventory"
    original = getattr(target, name)

    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        cancellation.cancel()
        return result

    monkeypatch.setattr(target, name, cancelled)
    with cancellation_scope(cancellation), pytest.raises(RunCancelled):
        prepare(tmp_path)
    assert not (tmp_path / "products" / renders.RENDER_RECORD).exists()


def test_policy_change_during_reuse_is_rechecked(
    tmp_path, backend, monkeypatch
):
    prepare(tmp_path)
    blocked = False
    identity = renders.slide_renderer.renderer_identity

    def changed(settings):
        nonlocal blocked
        blocked = True
        return identity(settings)

    monkeypatch.setattr(renders.slide_renderer, "renderer_identity", changed)
    with pytest.raises(ValueError, match="exclud"):
        renders.prepare_slide_renders(
            tmp_path / "source.pptx",
            tmp_path / "products",
            excluded=lambda *_: blocked,
        )
    assert len(backend) == 1


def test_concurrent_preparation_cannot_share_workspace(
    tmp_path, backend, monkeypatch
):
    entered, release = Event(), Event()
    render = renders.slide_renderer.render_slides

    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release the renderer"
        return render(*args, **kwargs)

    monkeypatch.setattr(renders.slide_renderer, "render_slides", waiting)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(prepare, tmp_path)
        try:
            assert entered.wait(5), "renderer did not start"
            with pytest.raises(ValueError, match="workspace is in use"):
                prepare(tmp_path)
        finally:
            release.set()
        assert len(running.result(timeout=5).slides) == 2
    assert len(backend) == 1
