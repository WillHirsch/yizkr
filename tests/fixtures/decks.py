"""The expected text of a fictional deck: what its transcription holds when
it reads no image on its own."""

from __future__ import annotations

import tempfile
from pathlib import Path

from yizkr.pipeline.presentation import slide_pages, unpack_pptx


def deck_text(path: Path) -> str:
    """The deck at `path` slide by slide: each slide's page text, as
    `slide_pages` reads it, behind its `[Slide N]` marker, one line break
    between slides. A deck's transcription holds this text when it reads
    no image on its own; one that reads an image marks it below the
    marker of each slide that paints it. A deck with a slide it cannot
    read raises, as its transcription does."""
    with tempfile.TemporaryDirectory(prefix="yizkr-deck-text-") as folder:
        pages = slide_pages(unpack_pptx(path), Path(folder)).pages
    return "\n".join(
        f"[Slide {page.number}]" + (f"\n{page.text}" if page.text else "")
        for page in pages
    )
