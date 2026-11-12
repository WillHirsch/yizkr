"""Local slide rendering boundaries, without a system office installation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.fixtures import pdfs
from yizkr.pipeline import slide_renderer as renderer
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
)


def _pdf() -> bytes:
    first, second = pdfs.Page(), pdfs.Page()
    first.text(["Clara Lobig"])
    second.text(["Nicholas Lobig"])
    return pdfs.pdf([first, second])


@pytest.fixture
def conversion(monkeypatch):
    """Stand in only for office conversion; rasterize real fictional PDFs."""
    calls = []
    monkeypatch.setattr(renderer, "_identity", lambda *args: {"version": 1})

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "--convert-to" in command:
            directory = Path(command[command.index("--outdir") + 1])
            (directory / "source.pdf").write_bytes(_pdf())
        else:
            renderer._rasterize_pdf(
                Path(command[-4]),
                Path(command[-3]),
                int(command[-2]),
                int(command[-1]),
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(renderer, "_run_process", run)
    return calls


def test_renders_every_page_deterministically_and_cleans_profile(conversion):
    settings = renderer.SlideRenderSettings(long_edge=160)
    first = renderer.render_slides(
        b"admitted fictional package", expected_slides=2, settings=settings
    )
    second = renderer.render_slides(
        b"admitted fictional package", expected_slides=2, settings=settings
    )
    assert first == second
    assert [page.position for page in first.pages] == [1, 2]
    assert len({page.png for page in first.pages}) == 2
    assert all(max(page.width, page.height) == 160 for page in first.pages)
    assert first.renderer == {"version": 1}
    commands = [command for command, _ in conversion]
    profiles = [
        next(arg for arg in command if arg.startswith("-env:"))
        for command in commands
        if "--convert-to" in command
    ]
    assert len(set(profiles)) == 2
    assert all(not kwargs["cwd"].exists() for _, kwargs in conversion)
    for command, kwargs in conversion:
        assert kwargs["env"]["LC_ALL"] == "C.UTF-8"
        assert kwargs["env"]["TZ"] == "UTC"
        assert 0 < kwargs["timeout"] <= settings.timeout
        if "--convert-to" in command:
            conversion = command[command.index("--convert-to") + 1]
            options = json.loads(conversion.split(":", 2)[2])
            assert options["ExportHiddenSlides"]["value"] == "true"
            assert options["ExportNotesPages"]["value"] == "false"


@pytest.mark.parametrize("fault", ["exit", "missing", "timeout", "cancel"])
def test_conversion_failure_returns_no_pages_and_removes_work(
    monkeypatch, conversion, fault
):
    directories = []
    token = Cancellation()

    def failed(command, **kwargs):
        directories.append(kwargs["cwd"])
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if fault == "cancel":
            token.cancel("stop fictional rendering")
            token.check()
        return subprocess.CompletedProcess(
            command, 1 if fault == "exit" else 0, "private source text", ""
        )

    monkeypatch.setattr(renderer, "_run_process", failed)
    expected = RunCancelled if fault == "cancel" else renderer.SlideRenderError
    with cancellation_scope(token), pytest.raises(expected) as caught:
        renderer.render_slides(
            b"admitted fictional package", expected_slides=2
        )
    assert "private source text" not in str(caught.value)
    assert directories and all(not path.exists() for path in directories)


@pytest.mark.parametrize("fault", ["malformed", "page_count"])
def test_invalid_converted_pdf_is_refused(tmp_path, fault):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"not a PDF" if fault == "malformed" else _pdf())
    with pytest.raises(renderer.SlideRenderError):
        renderer._rasterize_pdf(path, tmp_path, 1, 160)
    assert not list(tmp_path.glob("slide-*.png"))


def test_missing_raster_page_is_refused(monkeypatch, conversion):
    run = renderer._run_process

    def missing_page(command, **kwargs):
        result = run(command, **kwargs)
        if "--convert-to" not in command:
            (Path(command[-3]) / "slide-2.png").unlink()
        return result

    monkeypatch.setattr(renderer, "_run_process", missing_page)
    with pytest.raises(renderer.SlideRenderError, match="invalid page"):
        renderer.render_slides(
            b"admitted fictional package", expected_slides=2
        )


def test_renderer_identity_tracks_font_content_and_configuration(
    tmp_path, monkeypatch
):
    font = tmp_path / "fictional-font.ttf"
    font.write_bytes(b"fictional font revision one")
    config = tmp_path / "fonts.conf"
    config.write_text("fictional font substitution one")
    executable = tmp_path / "office"
    executable.write_text("local converter wrapper revision one")
    monkeypatch.setattr(
        renderer.shutil, "which", lambda *args, **kwargs: str(executable)
    )
    directories = []

    def run(command, **kwargs):
        directories.append(kwargs["cwd"])
        if command[0] == "libreoffice":
            stdout, stderr = "LibreOffice 25.2.0.1\n", ""
        elif command == ["fc-list", "--version"]:
            stdout, stderr = "", "fontconfig version 2.15.0\n"
        elif command[0] == "fc-conflist":
            stdout, stderr = f"+ {config}: Test configuration\n", ""
        else:
            stdout, stderr = f"{font}\n", ""
        return subprocess.CompletedProcess(command, 0, stdout, stderr)

    monkeypatch.setattr(renderer, "_run_process", run)
    first = renderer.renderer_identity()
    assert renderer.renderer_identity() == first
    assert str(tmp_path) not in str(first)
    font.write_bytes(b"fictional font revision two")
    second = renderer.renderer_identity()
    assert first["fonts_sha256"] != second["fonts_sha256"]
    assert first["font_config_sha256"] == second["font_config_sha256"]
    config.write_text("fictional font substitution two")
    third = renderer.renderer_identity()
    assert second["font_config_sha256"] != third["font_config_sha256"]
    executable.write_text("local converter wrapper revision two")
    fourth = renderer.renderer_identity()
    assert third["executable_sha256"] != fourth["executable_sha256"]
    assert all(not path.exists() for path in directories)


def test_missing_optional_renderer_is_explicit(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("not installed")

    monkeypatch.setattr(renderer, "_run_process", missing)
    with pytest.raises(renderer.SlideRendererUnavailable):
        renderer.renderer_identity()


@pytest.mark.parametrize("long_edge", [0, -1, 8193, True, 160.5])
def test_invalid_render_dimensions_are_rejected(long_edge):
    with pytest.raises(ValueError, match="long edge"):
        renderer.SlideRenderSettings(long_edge=long_edge)
