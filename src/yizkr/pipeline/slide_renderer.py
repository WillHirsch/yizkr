"""Optional local slide rendering: admitted PPTX bytes -> PDF -> PNG pages.

The caller must first prove that the package is eligible for rendering,
including its external relationships and excluded contributions. This
backend neither interprets that proof nor decides source ownership. It
returns bytes only; the caller owns publication and reuse of the product.

LibreOffice and Fontconfig are optional system dependencies. Conversion
uses an isolated profile, and PDFium runs in its own process so timeout or
run cancellation can stop native rendering as well as conversion. PNG
encoding is the same deterministic encoding used for PDF vision. Layout
still depends on the recorded renderer and font environment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.pipeline import textlayer
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.llm import _run_process

_PDF_OPTIONS = {
    "ExportHiddenSlides": {"type": "boolean", "value": "true"},
    "ExportNotesPages": {"type": "boolean", "value": "false"},
    "ExportOnlyNotesPages": {"type": "boolean", "value": "false"},
    "ExportNotes": {"type": "boolean", "value": "false"},
    "UseLosslessCompression": {"type": "boolean", "value": "true"},
    "ReduceImageResolution": {"type": "boolean", "value": "false"},
}
_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "SAL_USE_VCLPLUGIN": "svp",
    "SAL_DISABLE_OPENCL": "1",
}
_WORKER = (
    "import sys; "
    "from pathlib import Path; "
    "from yizkr.pipeline.slide_renderer import _rasterize_pdf; "
    "_rasterize_pdf(Path(sys.argv[1]), Path(sys.argv[2]), "
    "int(sys.argv[3]), int(sys.argv[4]))"
)


class SlideRenderError(RuntimeError):
    """The renderer did not produce a complete, valid set of slide pages."""


class SlideRendererUnavailable(SlideRenderError):
    """An optional local renderer dependency is not installed."""


@dataclass(frozen=True)
class SlideRenderSettings:
    executable: str = "libreoffice"
    long_edge: int = textlayer.RENDER_LONG_EDGE
    timeout: float = 120.0

    def __post_init__(self) -> None:
        if not self.executable:
            raise ValueError("a LibreOffice executable is required")
        if type(self.long_edge) is not int or not 1 <= self.long_edge <= 8192:
            raise ValueError("slide long edge must be between 1 and 8192")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("slide rendering timeout must be positive")


_DEFAULT_SETTINGS = SlideRenderSettings()


@dataclass(frozen=True)
class RenderedSlide:
    position: int
    png: bytes
    width: int
    height: int


@dataclass(frozen=True)
class SlideRenderResult:
    pages: tuple[RenderedSlide, ...]
    renderer: dict[str, Any]


def _remaining(deadline: float) -> float:
    check_cancelled()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SlideRenderError("slide rendering timed out")
    return remaining


def _environment(directory: Path) -> dict[str, str]:
    environment = {**os.environ, **_ENVIRONMENT}
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        location = directory / name.lower()
        location.mkdir()
        environment[name] = str(location)
    return environment


def _command(
    command: list[str],
    *,
    deadline: float,
    directory: Path,
    environment: dict[str, str],
    stage: str,
) -> subprocess.CompletedProcess:
    try:
        result = _run_process(
            command,
            timeout=_remaining(deadline),
            cwd=directory,
            env=environment,
        )
    except FileNotFoundError as error:
        raise SlideRendererUnavailable(
            f"slide renderer dependency unavailable during {stage}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SlideRenderError("slide rendering timed out") from error
    _remaining(deadline)
    if result.returncode:
        # A converter's output can contain source text and paths. Keep it
        # out of the public error; failures do not publish partial pages.
        raise SlideRenderError(
            f"slide renderer {stage} failed (exit {result.returncode})"
        )
    return result


def _file_digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            _remaining(deadline)
            digest.update(block)
    return digest.hexdigest()


def _identity(
    settings: SlideRenderSettings,
    directory: Path,
    environment: dict[str, str],
    deadline: float,
) -> dict[str, Any]:
    def run(command: list[str], stage: str) -> subprocess.CompletedProcess:
        return _command(
            command,
            deadline=deadline,
            directory=directory,
            environment=environment,
            stage=stage,
        )

    executable = shutil.which(
        settings.executable, path=environment.get("PATH")
    )
    if executable is None:
        raise SlideRendererUnavailable("LibreOffice executable is unavailable")
    try:
        executable_digest = _file_digest(Path(executable), deadline)
    except OSError as error:
        raise SlideRendererUnavailable(
            "LibreOffice executable cannot be fingerprinted"
        ) from error
    profile = (directory / "profile").as_uri()
    version = run(
        [settings.executable, f"-env:UserInstallation={profile}", "--version"],
        "version",
    ).stdout.strip()
    if not version.startswith("LibreOffice "):
        raise SlideRenderError("unrecognized LibreOffice version output")
    font_version = run(["fc-list", "--version"], "font version")
    fonts = run(["fc-list", "--format=%{file}\\n"], "font inventory")
    font_paths = set(fonts.stdout.splitlines())
    if not font_paths:
        raise SlideRenderError("slide renderer has no available fonts")
    # Configuration order can alter substitution even with the same fonts.
    configs = run(["fc-conflist"], "font configuration")
    config_paths = [
        line[2:].split(": ", 1)[0]
        for line in configs.stdout.splitlines()
        if line.startswith("+ ")
    ]
    if not config_paths:
        raise SlideRenderError("font configuration cannot be fingerprinted")
    try:
        font_hashes = sorted(
            {_file_digest(Path(path), deadline) for path in font_paths}
        )
        config_hashes = [
            _file_digest(Path(path), deadline) for path in config_paths
        ]
    except OSError as error:
        raise SlideRenderError(
            "font environment cannot be fingerprinted"
        ) from error

    def aggregate(hashes: list[str]) -> str:
        return hashlib.sha256("\n".join(hashes).encode()).hexdigest()

    return {
        "backend": "libreoffice-pdfium",
        "version": 1,
        "libreoffice": version,
        "executable_sha256": executable_digest,
        "pdfium": textlayer.LIBRARY,
        "fontconfig": (font_version.stdout + font_version.stderr).strip(),
        "fonts_sha256": aggregate(font_hashes),
        "font_config_sha256": aggregate(config_hashes),
        "environment": dict(_ENVIRONMENT),
        "pdf_options": json.loads(json.dumps(_PDF_OPTIONS)),
        "long_edge": settings.long_edge,
        "png_compression": textlayer.PNG_LEVEL,
        "zlib": zlib.ZLIB_RUNTIME_VERSION,
        "draw_annotations": True,
        "bitmap_format": "BGR",
    }


def renderer_identity(
    settings: SlideRenderSettings = _DEFAULT_SETTINGS,
) -> dict[str, Any]:
    """Fingerprint the current renderer/configuration without source input.

    Fonts are hashed by content; their names and local paths are not kept.
    The timeout is an execution bound and does not affect product identity.
    """
    deadline = time.monotonic() + settings.timeout
    with tempfile.TemporaryDirectory(prefix="yizkr-slide-identity-") as root:
        directory = Path(root)
        return _identity(
            settings, directory, _environment(directory), deadline
        )


def render_slides(
    source: bytes,
    *,
    expected_slides: int,
    settings: SlideRenderSettings = _DEFAULT_SETTINGS,
) -> SlideRenderResult:
    """Render an admitted package; return every slide or raise.

    ``expected_slides`` is the admitted inventory's presentation-order
    count, including hidden slides. Notes pages and annotations are not
    exported. All temporary input, profile, PDF and PNG files are removed
    on success, failure or cancellation.
    """
    if not isinstance(source, bytes) or not source:
        raise ValueError("slide rendering requires nonempty package bytes")
    if type(expected_slides) is not int or expected_slides < 1:
        raise ValueError("slide rendering requires a positive slide count")
    deadline = time.monotonic() + settings.timeout
    with tempfile.TemporaryDirectory(prefix="yizkr-slide-render-") as root:
        directory = Path(root)
        environment = _environment(directory)
        identity = _identity(settings, directory, environment, deadline)
        presentation = directory / "source.pptx"
        presentation.write_bytes(source)
        output = directory / "output"
        output.mkdir()
        profile = (directory / "profile").as_uri()
        conversion = "pdf:impress_pdf_Export:" + json.dumps(
            _PDF_OPTIONS, separators=(",", ":"), sort_keys=True
        )
        _command(
            [
                settings.executable,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                conversion,
                "--outdir",
                str(output),
                str(presentation),
            ],
            deadline=deadline,
            directory=directory,
            environment=environment,
            stage="conversion",
        )
        pdf = output / "source.pdf"
        if not pdf.is_file() or pdf.is_symlink() or not pdf.stat().st_size:
            raise SlideRenderError("slide conversion produced no PDF")
        _command(
            [
                sys.executable,
                "-c",
                _WORKER,
                str(pdf),
                str(output),
                str(expected_slides),
                str(settings.long_edge),
            ],
            deadline=deadline,
            directory=directory,
            environment=environment,
            stage="rasterization",
        )
        pages = []
        for position in range(1, expected_slides + 1):
            _remaining(deadline)
            try:
                path = output / f"slide-{position}.png"
                if path.is_symlink():
                    raise ValueError("unexpected symbolic link")
                png = path.read_bytes()
                if png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
                    raise ValueError("missing PNG header")
                width, height = struct.unpack(">II", png[16:24])
                if not 1 <= min(width, height) <= max(width, height):
                    raise ValueError("invalid PNG dimensions")
                if max(width, height) > settings.long_edge + 1:
                    raise ValueError("PNG exceeds configured dimensions")
            except (OSError, ValueError, struct.error) as error:
                raise SlideRenderError(
                    "slide rasterization produced an invalid page"
                ) from error
            pages.append(RenderedSlide(position, png, width, height))
        return SlideRenderResult(tuple(pages), identity)


def _rasterize_pdf(
    path: Path, output: Path, expected_slides: int, long_edge: int
) -> None:
    """Private child-process entry point; no public command or stage."""
    textlayer.fresh_start()
    try:
        document = textlayer.pdfium.PdfDocument(path)
        try:
            if len(document) != expected_slides:
                raise SlideRenderError(
                    "converted PDF page count differs from slide inventory"
                )
            document.init_forms()
            for index in range(len(document)):
                page = document[index]
                try:
                    width, height = page.get_size()
                    if not all(
                        math.isfinite(value) and value > 0
                        for value in (width, height)
                    ):
                        raise SlideRenderError("invalid converted page size")
                    bitmap = page.render(
                        scale=long_edge / max(width, height),
                        force_bitmap_format=textlayer.pdfium_c.FPDFBitmap_BGR,
                        draw_annots=True,
                    )
                    try:
                        png = textlayer.bitmap_png(bitmap)
                    finally:
                        bitmap.close()
                    (output / f"slide-{index + 1}.png").write_bytes(png)
                finally:
                    page.close()
        finally:
            document.close()
    except (textlayer.pdfium.PdfiumError, ValueError) as error:
        raise SlideRenderError("converted PDF cannot be rendered") from error
