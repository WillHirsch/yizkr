"""Decks as paged containers (#502, ADR-0008): a deck is read slide by
slide, as a PDF is read page by page, each slide's text from its XML and
each image it embeds read on its own, as a witness of its own, an
excerpt.

The deck is the fictional library talk of 2018 (docs/EXAMPLES.md): its
second slide shows the top of Sebastian's 1939 letter to Klara, the same
image the booklet places on its page 6 (tests/unit/test_pdf_pages.py).
The orchestrator runs with its real client wiring, and a scripted engine
reads each image a vision call gives it.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from tests.fixtures import pdfs
from tests.unit.test_orchestrate import this_run
from tests.unit.test_pdf_pages import (
    CAPTION,
    COMMENT,
    EXCERPT,
    LETTER_IMAGE,
    LETTER_SHA1,
    REFUSAL,
    SOURCES,
    TRANSLATION,
    Raw,
    Reader,
    RefusingReader,
    booklet,
    document,
    excerpt_of,
    extract,
    run,
    transcription_of,
)
from tests.unit.test_presentation import (
    DECK_TIME,
    IMAGE_REL,
    NOTES_REL,
    SLIDE_REL,
    rels_xml,
)
from tests.unit.test_scratch import Corpus, a_run, fakes  # noqa: F401
from tests.unit.test_scratch_estimate import (  # noqa: F401
    engine,
    estimate_of,
    runs_dir,
)
from tests.unit.test_scratch_estimate import run as run_scratch
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import presentation
from yizkr.pipeline.containers import (
    MIN_IMAGE_PIXELS,
    MIN_IMAGE_SIDE,
    SLIDE_XML,
    image_size,
    is_decoration,
)
from yizkr.pipeline.monitor import reason_label
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    PAGED,
    PDF,
    document_kind,
    route_transcription,
)
from yizkr.pipeline.orchestrate import (
    EXCERPT as EXCERPT_KIND,
)
from yizkr.pipeline.presentation import (
    UNREAD_IMAGE,
    derived_transcription,
    read_derived_media,
    read_slides,
    slide_transcript,
    unpack_pptx,
)
from yizkr.pipeline.report import build
from yizkr.pipeline.scratch import EXIT_OK
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import Transcription

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# the letter's top, as the booklet places it and the deck embeds it
LETTER_PNG = png(300, 120, 1, LETTER_IMAGE.data)
TITLE = "Where the Lobigs came from"
LETTER_SLIDE = ("Wastl to Klara, July 1939", "Gustl finally has his visa.")


def png_of(width: int, height: int, caption: str = "") -> bytes:
    """A grey PNG of this size, its caption in a text chunk so that each
    caption gives other bytes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body))
        )

    rows = (b"\x00" + b"\x80" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + caption.encode())
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# a rule drawn on a slide, too small to hold legible text
RULE = png_of(200, 2, "a rule")


def deck(
    path: Path,
    slides: list[tuple[tuple[str, ...], list[str], str]],
    media: dict[str, bytes],
    *,
    painted: dict[int, list[str]] | None = None,
) -> Path:
    """A deck of `slides`, each (its paragraphs, the names of the media
    its relationships name, in that order, its speaker notes or ""), the
    media's bytes by name. A slide's XML refers to its pictures in the
    order `painted` gives for it, else in its relationships' order."""

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=DECK_TIME), data)

    presentation_xml = (
        f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}"><p:sldIdLst>'
        + "".join(
            f'<p:sldId id="{255 + n}" r:id="rId{n}"/>'
            for n in range(1, len(slides) + 1)
        )
        + "</p:sldIdLst></p:presentation>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        put(archive, "[Content_Types].xml", "<Types/>")
        put(archive, "ppt/presentation.xml", presentation_xml)
        put(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml(
                [
                    (f"rId{n}", SLIDE_REL, f"slides/slide{n}.xml")
                    for n in range(1, len(slides) + 1)
                ]
            ),
        )
        for n, (paragraphs, names, notes) in enumerate(slides, start=1):
            rid = {name: f"rId{10 + i}" for i, name in enumerate(names)}
            pictures = "".join(
                f'<p:pic><p:blipFill><a:blip r:embed="{rid[name]}"/>'
                "</p:blipFill></p:pic>"
                for name in (painted or {}).get(n, names)
            )
            body = "".join(
                f"<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                "</p:txBody></p:sp>"
                for text in paragraphs
            )
            put(
                archive,
                f"ppt/slides/slide{n}.xml",
                f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}" xmlns:r="{_R}">'
                f"<p:cSld><p:spTree>{body}{pictures}</p:spTree></p:cSld>"
                "</p:sld>",
            )
            links = [
                (rid[name], IMAGE_REL, f"../media/{name}") for name in names
            ]
            if notes:
                links.append(
                    ("rId30", NOTES_REL, f"../notesSlides/notesSlide{n}.xml")
                )
                put(
                    archive,
                    f"ppt/notesSlides/notesSlide{n}.xml",
                    f'<p:notes xmlns:p="{_P}" xmlns:a="{_A}"><p:cSld>'
                    f"<p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{notes}"
                    "</a:t></a:r></a:p></p:txBody></p:sp></p:spTree>"
                    "</p:cSld></p:notes>",
                )
            put(
                archive, f"ppt/slides/_rels/slide{n}.xml.rels", rels_xml(links)
            )
        for name, data in media.items():
            put(archive, f"ppt/media/{name}", data)
    return path


def library_talk(path: Path, *, letter: bytes = LETTER_PNG) -> Path:
    """The talk: a title slide with a rule, the letter's slide with the
    letter's top on it, and a closing slide."""
    return deck(
        path,
        [
            (
                (TITLE, "Nicholas Lobig, library talk, 2018"),
                ["image1.png"],
                "",
            ),
            (LETTER_SLIDE, ["image2.png"], "From the family album"),
            (("Stupfield", "Their son: Nicholas 1950"), [], ""),
        ],
        {"image1.png": RULE, "image2.png": letter},
    )


def gif_of(width: int, height: int) -> bytes:
    """A GIF's header, which gives its size, and a trailer."""
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def webp_of(width: int, height: int) -> bytes:
    """An extended WebP's header (VP8X), which gives its size."""
    return (
        b"RIFF"
        + struct.pack("<I", 22)
        + b"WEBPVP8X"
        + struct.pack("<I", 10)
        + b"\x00" * 4
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def jpeg_of(width: int, height: int, caption: str = "") -> bytes:
    """A JPEG's start, a comment and a start of frame, which gives its
    size."""
    comment = caption.encode()
    return (
        b"\xff\xd8"
        + b"\xff\xfe"
        + struct.pack(">H", len(comment) + 2)
        + comment
        + b"\xff\xc0"
        + struct.pack(">HBHH", 11, 8, height, width)
        + b"\x01\x01\x11\x00"
        + b"\xff\xd9"
    )


def talk_folder(tmp_path: Path, **files: bytes) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    library_talk(corpus / "library-talk-2018.pptx")
    for name, data in files.items():
        (corpus / name).write_bytes(data)
    return corpus


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class TestImageSize:
    def test_the_header_gives_the_size(self):
        assert image_size(png_of(31, 7)) == (31, 7)
        assert image_size(b"GIF89a" + struct.pack("<HH", 40, 30)) == (40, 30)
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xe0"
            + struct.pack(">H", 4)
            + b"JF"
            + b"\xff\xc0"
            + struct.pack(">HBHH", 11, 8, 120, 300)
            + b"\x03"
        )
        assert image_size(jpeg) == (300, 120)
        webp = (
            b"RIFF"
            + struct.pack("<I", 22)
            + b"WEBPVP8X"
            + struct.pack("<I", 10)
            + b"\x00" * 4
            + (299).to_bytes(3, "little")
            + (119).to_bytes(3, "little")
        )
        assert image_size(webp) == (300, 120)

    def test_bytes_of_no_known_format_give_none(self):
        assert image_size(b"\x89PNG synthetic") is None
        assert image_size(b"II*\x00 a tiff") is None
        assert image_size(b"") is None

    @pytest.mark.parametrize(
        ("size", "decoration"),
        [
            ((MIN_IMAGE_SIDE, 500), False),
            ((MIN_IMAGE_SIDE - 1, 500), True),
            ((100, MIN_IMAGE_PIXELS // 100), False),
            ((100, MIN_IMAGE_PIXELS // 100 - 1), True),
        ],
    )
    def test_decoration_is_the_pdf_s_rule(self, size, decoration):
        assert is_decoration(*size) is decoration


class TestReadSlides:
    def test_each_slide_is_a_page_read_from_its_xml(self, tmp_path):
        """Each slide is a page read from its XML, its text the slide's
        text and notes; the letter's top is read on its own, written once
        into the folder; the rule is decoration, and read by none."""
        path = library_talk(tmp_path / "talk.pptx")
        read = read_slides(path, tmp_path)
        assert [(p.number, p.method) for p in read.pages] == [
            (1, SLIDE_XML),
            (2, SLIDE_XML),
            (3, SLIDE_XML),
        ]
        content = unpack_pptx(path)
        assert [p.text for p in read.pages] == [
            presentation.slide_text(slide) for slide in content.slides
        ]
        assert read.pages[1].text == (
            "\n".join(LETTER_SLIDE)
            + "\n[Slide 2 notes]\nFrom the family album"
        )
        assert [p.images for p in read.pages] == [[], [(1, LETTER_SHA1)], []]
        (image,) = read.images.values()
        assert image.places == [(2, 1)]
        assert image.path == tmp_path / f"{LETTER_SHA1}.png"
        assert image.path.read_bytes() == LETTER_PNG
        assert read.notes == []
        assert read.by_own_text

    def test_images_take_the_order_the_slide_paints_them_in(self, tmp_path):
        """A slide's images are placed in the order its XML refers to
        them, whatever order its relationships list them in; the same
        image twice on a slide, painted twice or held under two names, is
        one place, and on two slides one image with a place on each."""
        first, second = png_of(120, 120, "first"), png_of(120, 120, "second")
        path = deck(
            tmp_path / "talk.pptx",
            [
                (("Wastl and Hilde",), ["a.png", "b.png", "c.png"], ""),
                (("Stupfield",), ["a.png"], ""),
            ],
            {"a.png": first, "b.png": second, "c.png": first},
            painted={1: ["b.png", "a.png", "b.png", "c.png"]},
        )
        (tmp_path / "out").mkdir()
        read = read_slides(path, tmp_path / "out")
        assert read.pages[0].images == [(1, sha1(second)), (2, sha1(first))]
        assert read.pages[1].images == [(1, sha1(first))]
        assert read.images[sha1(first)].places == [(1, 2), (2, 1)]

    def test_an_image_the_engine_cannot_read_is_noted(self, tmp_path):
        """A TIFF, or an image whose header gives no size, is not read on
        its own, and the reading notes it once; an image the never-ingest
        list names is not read, and not noted."""
        tiff = b"II*\x00 the album page as a tiff"
        unsized = b"\x89PNG synthetic: no header"
        path = deck(
            tmp_path / "talk.pptx",
            [
                (("Wastl and Hilde",), ["scan.tif", "odd.png"], ""),
                (("Stupfield",), ["scan.tif", "letter.png"], ""),
            ],
            {"scan.tif": tiff, "odd.png": unsized, "letter.png": LETTER_PNG},
        )
        (tmp_path / "out").mkdir()
        read = read_slides(
            path,
            tmp_path / "out",
            excluded=lambda s, names=(): s == LETTER_SHA1,
        )
        assert read.images == {}
        assert read.notes == [
            f"{UNREAD_IMAGE}: slide 1, scan.tif (sha1 {sha1(tiff)[:12]}): a "
            ".tif image, a format the vision engine does not read, so it is "
            "not read",
            f"{UNREAD_IMAGE}: slide 1, odd.png (sha1 {sha1(unsized)[:12]}): "
            "an image whose size its header does not give, so it is not read",
        ]
        # the monitor shows a fixed phrase, never what the archive calls
        # the image
        assert {reason_label(note) for note in read.notes} == {
            "image not read on its own"
        }
        assert list((tmp_path / "out").iterdir()) == []

    def test_a_folder_of_the_archive_tells_two_images_apart(self, tmp_path):
        """Two images of one basename in two folders of the archive are two
        images, each marked on the slide that embeds it."""
        first, second = png_of(120, 120, "first"), png_of(120, 120, "second")
        path = deck(
            tmp_path / "talk.pptx",
            [
                (("Wastl and Hilde",), ["a/pic.png"], ""),
                (("Stupfield",), ["b/pic.png"], ""),
            ],
            {"a/pic.png": first, "b/pic.png": second},
        )
        (tmp_path / "out").mkdir()
        read = read_slides(path, tmp_path / "out")
        assert [page.images for page in read.pages] == [
            [(1, sha1(first))],
            [(1, sha1(second))],
        ]

    def test_an_image_its_xml_does_not_paint_is_read(self, tmp_path):
        """An image only the slide's relationships name is still the
        slide's, and read."""
        path = deck(
            tmp_path / "talk.pptx",
            [(("Wastl and Hilde",), [], ""), (LETTER_SLIDE, ["l.png"], "")],
            {"l.png": LETTER_PNG},
            painted={2: []},
        )
        (tmp_path / "out").mkdir()
        read = read_slides(path, tmp_path / "out")
        assert read.pages[1].images == [(1, LETTER_SHA1)]

    def test_every_vision_format_is_read(self, tmp_path):
        """A JPEG, a GIF and a WebP of a readable size are each read, with
        no note."""
        images = {
            "a.jpeg": jpeg_of(300, 120),
            "b.gif": gif_of(300, 120),
            "c.webp": webp_of(300, 120),
        }
        path = deck(
            tmp_path / "talk.pptx",
            [(("Wastl and Hilde",), list(images), "")],
            images,
        )
        (tmp_path / "out").mkdir()
        read = read_slides(path, tmp_path / "out")
        assert read.notes == []
        assert [s for _place, s in read.pages[0].images] == [
            sha1(data) for data in images.values()
        ]

    def test_the_same_bytes_under_two_names_take_the_record_s_suffix(
        self, tmp_path
    ):
        """One JPEG held as a.jpeg and as b.jpg is one image, its file
        named with the suffix the record of the deck's derived media takes:
        the first name's."""
        data = jpeg_of(300, 120, "the shop front")
        path = deck(
            tmp_path / "talk.pptx",
            [(("Hutmacherei",), ["a.jpeg", "b.jpg"], "")],
            {"a.jpeg": data, "b.jpg": data},
        )
        (tmp_path / "out").mkdir()
        (image,) = read_slides(path, tmp_path / "out").images.values()
        assert image.suffix == ".jpeg"
        record = presentation.record_derived_media(
            unpack_pptx(path),
            source_sha1=sha1(path.read_bytes()),
            title="Hutmacherei",
            directory=tmp_path / "doc",
        )
        assert record.media[sha1(data)].file.endswith(".jpeg")


class TestTheRoute:
    def test_a_pdf_and_a_deck_take_the_paged_route(self, tmp_path):
        """A PDF is a kind of its own, apart from photographs; it and a
        deck take the paged route, and an image is read whole."""
        assert document_kind(Path("booklet.pdf")) == PDF == "pdf"
        assert document_kind(Path("BOOKLET.PDF")) == PDF
        assert document_kind(Path("talk.pptx")) == "presentation"
        assert document_kind(Path("letter.png")) == "media"
        assert route_transcription(Path("booklet.pdf")) == PAGED
        assert route_transcription(Path("talk.pptx")) == PAGED
        assert route_transcription(Path("letter.png")) == "vision"


class TestADeckSImages:
    def test_the_letter_on_a_slide_is_an_excerpt(self, tmp_path):
        """The deck's own text is its slides', read from the XML; the
        letter's top is marked below its slide's marker by its SHA-1, as
        a PDF's page marks its images, read once on its own, kept beside
        its bytes, and extracted as a document of its own, whose claims
        cite the image while the deck's cite the deck."""
        reader = Reader({LETTER_SHA1: EXCERPT})
        orchestrator = run(tmp_path, talk_folder(tmp_path), reader)
        doc = document(orchestrator, "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert reader.read == [(f"{LETTER_SHA1}.png", LETTER_SHA1)]
        transcription = transcription_of(doc)
        assert transcription.engine == "pptx-adapter"
        assert [
            (page.page, page.provenance.method, page.images)
            for page in transcription.pages
        ] == [
            (1, SLIDE_XML, []),
            (2, SLIDE_XML, [LETTER_SHA1]),
            (3, SLIDE_XML, []),
        ]
        assert (
            f"[Slide 2]\n[Slide 2, image 1: sha1:{LETTER_SHA1}]\n"
            + "\n".join(LETTER_SLIDE)
        ) in transcription.text
        # the image's reading is never part of the deck's text
        assert EXCERPT.splitlines()[0] not in transcription.text
        kept = Transcription.model_validate_json(
            derived_transcription(doc.dir, LETTER_SHA1).read_text()
        )
        assert kept.artifact.sha1 == LETTER_SHA1
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        assert excerpt.kind == EXCERPT_KIND
        assert [c.dir for c in excerpt.containers] == [doc.dir]
        assert excerpt.state.title == ("library-talk-2018 — image2.png")
        assert excerpt.state.status == AWAITING_CLAIMS, excerpt.state.error
        cited = {
            run.sha1: {
                claim.source.artifact.sha1
                for claim in ClaimBatch.model_validate_json(
                    run.batch.read_text()
                ).claims
            }
            for run in (doc, excerpt)
        }
        assert cited == {doc.sha1: {doc.sha1}, LETTER_SHA1: {LETTER_SHA1}}

    def test_every_image_marker_names_an_image_of_the_record(self, tmp_path):
        """Each `[Slide N, image k: sha1:…]` marker of the deck's
        transcription names an image the record of its derived media
        names, on that slide, and the record still names the rule, which
        the write registers though nothing reads it."""
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(
            run(tmp_path, talk_folder(tmp_path), reader),
            "library-talk-2018.pptx",
        )
        record = read_derived_media(doc.dir)
        markers = re.findall(
            r"^\[Slide (\d+), image (\d+): sha1:([0-9a-f]{40})\]$",
            transcription_of(doc).text,
            re.MULTILINE,
        )
        assert markers == [("2", "1", LETTER_SHA1)]
        for slide, _place, marked in markers:
            assert int(slide) in record.media[marked].slides
        assert set(record.media) == {LETTER_SHA1, sha1(RULE)}
        assert record.container == "presentation"

    def test_a_deck_with_no_image_to_read_starts_no_engine(self, tmp_path):
        """A deck whose images are all decoration reads none: its text is
        its slides', as slide_transcript gives it, and it has no
        excerpt."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        path = library_talk(corpus / "talk.pptx", letter=png_of(10, 400))
        orchestrator = run(tmp_path, corpus, RefusingReader({}))
        doc = document(orchestrator, "talk.pptx")
        assert transcription_of(doc).text == slide_transcript(
            unpack_pptx(path)
        )
        assert orchestrator._excerpts == {}

    def test_a_reused_view_stands_without_a_call(self, tmp_path):
        """A second run reads nothing again: the deck's view, its image's
        reading and the excerpt's views stand."""
        corpus = talk_folder(tmp_path)
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        second = run(tmp_path, corpus, RefusingReader({}))
        doc = document(second, "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert excerpt_of(second, LETTER_SHA1).state.status == AWAITING_CLAIMS

    def test_a_view_that_reads_no_image_is_made_again(self, tmp_path):
        """A deck's view that marks none of the images it reads on its
        own, as one made by a reader of its slide text alone, is set
        aside and made again, and the letter is read again with it, as a
        PDF's images are (here with no cache to answer the call)."""
        corpus = talk_folder(tmp_path)
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        doc = document(
            run(tmp_path, corpus, RefusingReader({})),
            "library-talk-2018.pptx",
        )
        old = Transcription(
            artifact=transcription_of(doc).artifact,
            text=slide_transcript(unpack_pptx(doc.source)),
            engine="pptx-adapter",
        )
        doc.transcription.write_text(old.model_dump_json())
        reader = Reader({LETTER_SHA1: EXCERPT})
        doc = document(run(tmp_path, corpus, reader), "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert reader.names() == [f"{LETTER_SHA1}.png"]
        assert transcription_of(doc).pages[1].images == [LETTER_SHA1]
        assert (doc.dir / "transcription.superseded-1.json").exists()

    def test_one_image_in_a_pdf_and_a_deck_is_one_excerpt(self, tmp_path):
        """The booklet places the letter's top and the deck embeds the
        same bytes: one excerpt, placed by both."""
        corpus = talk_folder(tmp_path, **{"booklet.pdf": booklet()})
        orchestrator = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        excerpt = excerpt_of(orchestrator, LETTER_SHA1)
        assert sorted(c.source.name for c in excerpt.containers) == [
            "booklet.pdf",
            "library-talk-2018.pptx",
        ]

    @pytest.mark.parametrize(
        "listed",
        [LETTER_SHA1, "image2.png", "image*"],
        ids=["by sha1", "by name", "by glob"],
    )
    def test_an_image_the_never_ingest_list_names_is_not_read(
        self, tmp_path, listed
    ):
        """By its SHA-1 or by a glob on its name in the archive, as the
        write leaves it out (registrar.register_derived_media): it reaches
        no engine and is no excerpt, though the record keeps it; a second
        run reuses the deck's view, setting nothing aside."""
        corpus = talk_folder(tmp_path)
        exclusions = ExclusionList.parse(f"{listed}\n")
        orchestrator = run(
            tmp_path, corpus, reader := Reader({}), exclusions=exclusions
        )
        doc = document(orchestrator, "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert orchestrator._excerpts == {}
        assert reader.read == []
        assert transcription_of(doc).pages[1].images == []
        assert LETTER_SHA1 in read_derived_media(doc.dir).media
        second = run(
            tmp_path, corpus, RefusingReader({}), exclusions=exclusions
        )
        assert this_run(second, "set-aside") == []
        assert not (doc.dir / "transcription.superseded-1.json").exists()
        reused = [
            event
            for event in this_run(second, "reused")
            if event["subject"] == doc.sha1
        ]
        assert reused[0]["stage"] == "transcription"

    def test_an_image_the_list_names_after_its_reading_is_dropped(
        self, tmp_path
    ):
        """The list names the image by its name in the archive after a run
        read it: the deck's view that marks it is made again without it
        (its text is then extracted again), the image is not read, and the
        reading kept of it makes no excerpt, though it stays on disk."""
        corpus = talk_folder(tmp_path)
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
        second = run(
            tmp_path,
            corpus,
            reader := Reader({}),
            exclusions=ExclusionList.parse("image2.png\n"),
        )
        assert reader.read == []
        doc = document(second, "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert transcription_of(doc).pages[1].images == []
        assert derived_transcription(doc.dir, LETTER_SHA1).exists()
        assert second._excerpts == {}

    def test_a_failed_reading_is_noted_by_its_slide(self, tmp_path):
        """An image whose reading the check fails has no reading of its
        own: the stage's attention names it by its slide, the deck goes on,
        and no excerpt is made."""
        orchestrator = run(
            tmp_path,
            talk_folder(tmp_path),
            Reader({LETTER_SHA1: Raw(REFUSAL)}),
        )
        doc = document(orchestrator, "library-talk-2018.pptx")
        assert doc.state.status == AWAITING_CLAIMS, doc.state.error
        assert orchestrator._excerpts == {}
        (done,) = [
            event
            for event in this_run(orchestrator, "done")
            if event["subject"] == doc.sha1
            and event["stage"] == "transcription"
        ]
        assert any(
            reason.startswith(f"slide 2, image 1 (sha1 {LETTER_SHA1[:12]})")
            for reason in done["attention"]
        )

    def test_a_deck_s_view_without_pages_is_made_again(self, tmp_path):
        """Every deck's view takes the paged shape: one of its slide text
        alone is set aside and made again, with a page per slide, though
        the deck reads no image and no engine is asked."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        path = library_talk(corpus / "talk.pptx", letter=png_of(10, 400))
        doc = document(run(tmp_path, corpus, RefusingReader({})), "talk.pptx")
        old = Transcription(
            artifact=transcription_of(doc).artifact,
            text=slide_transcript(unpack_pptx(path)),
            engine="pptx-adapter",
        )
        doc.transcription.write_text(old.model_dump_json())
        doc = document(run(tmp_path, corpus, RefusingReader({})), "talk.pptx")
        assert [p.provenance.method for p in transcription_of(doc).pages] == [
            SLIDE_XML
        ] * 3
        assert (doc.dir / "transcription.superseded-1.json").exists()

    def test_a_deck_with_little_text_takes_the_fallback_language(
        self, tmp_path
    ):
        """The language is found in the slides' text alone, never in the
        markers: a deck whose slides hold under 12 characters has too
        little to detect a language in, and takes the fallback."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        deck(corpus / "talk.pptx", [(("Oma Hilde",), [], "")], {})
        doc = document(
            run(
                tmp_path,
                corpus,
                RefusingReader({}),
                language="auto",
                fallback_language="de",
            ),
            "talk.pptx",
        )
        assert transcription_of(doc).language == "de"
        assert doc.state.detail["language"]["source"] == "fallback"


class TestTheRunReport:
    def test_a_deck_s_images_count_apart_from_a_pdf_s(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        """The scratch driver takes the deck's excerpt after the deck's
        transcription; the report counts the deck's images, apart from
        the PDFs', and the excerpt among the documents of its own."""
        corpus = Corpus(tmp_path / "corpus")
        path = library_talk(corpus.folder / "library-talk-2018.pptx")
        corpus.texts["library-talk-2018.pptx"] = slide_transcript(
            unpack_pptx(path)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        documents = build(driver.state_dir)["aggregate"]["documents"]
        assert documents["by_kind"] == {"presentation": 1, EXCERPT_KIND: 1}
        assert documents["deck_images"] == {
            "decks": 1,
            "placed": 1,
            "distinct": 1,
            "per_deck": {"1": 1},
        }
        assert documents["pdf_images"]["pdfs"] == 0
        assert documents["excerpts"] == 1

    def test_pdf_pages_count_the_pages_of_pdfs_read_page_by_page(
        self,
        tmp_path,
        fakes,  # noqa: F811
    ):
        """The report's pages by method are those of the PDFs read page by
        page alone: never a deck's slides, a PDF read whole, a
        photograph's page or an excerpt's."""
        corpus = Corpus(tmp_path / "corpus")
        path = library_talk(corpus.folder / "library-talk-2018.pptx")
        corpus.texts["library-talk-2018.pptx"] = slide_transcript(
            unpack_pptx(path)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        (corpus.folder / "booklet.pdf").write_bytes(booklet())
        corpus.texts["booklet.pdf"] = "\n".join(
            pdfs.lines(CAPTION, TRANSLATION, COMMENT)
        )
        (corpus.folder / "extract.pdf").write_bytes(extract())
        corpus.texts["extract.pdf"] = SOURCES["birth-extract-1938"].text
        corpus.image("studio-photo-1890s.png", "studio-photo-1890s")
        driver = a_run(tmp_path, corpus, fakes)
        assert driver.run() == EXIT_OK
        documents = build(driver.state_dir)["aggregate"]["documents"]
        assert documents["by_kind"] == {
            "presentation": 1,
            "pdf": 2,
            "media": 1,
            EXCERPT_KIND: 1,
        }
        (booklet_dir,) = [
            d for d in driver.state_dir.iterdir() if d.name == "booklet"
        ]
        held = Transcription.model_validate_json(
            (booklet_dir / "transcription.json").read_text()
        )
        methods: dict[str, int] = {}
        for page in held.pages:
            method = page.provenance.method
            methods[method] = methods.get(method, 0) + 1
        assert documents["pdf_pages"] == dict(
            sorted(methods.items(), key=lambda item: (-item[1], item[0]))
        )


class TestTheCallEstimate:
    def test_a_deck_s_image_may_be_an_excerpt(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """The estimate reads the deck as the run does, and counts its
        letter as an image that may become an excerpt."""
        corpus = Corpus(tmp_path / "corpus")
        path = library_talk(corpus.folder / "library-talk-2018.pptx")
        corpus.texts["library-talk-2018.pptx"] = slide_transcript(
            unpack_pptx(path)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        excerpts = estimate_of(tmp_path, corpus, engine).excerpts
        assert (excerpts.images, excerpts.containers) == (1, 1)

    def test_a_gif_on_a_slide_is_counted(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A GIF the deck embeds is read as a PNG is, and counted."""
        corpus = Corpus(tmp_path / "corpus")
        gif = gif_of(300, 120)
        path = deck(
            corpus.folder / "talk.pptx",
            [(LETTER_SLIDE, ["letter.gif"], "")],
            {"letter.gif": gif},
        )
        corpus.texts["talk.pptx"] = slide_transcript(unpack_pptx(path))
        corpus.texts[f"{sha1(gif)}.gif"] = EXCERPT
        excerpts = estimate_of(tmp_path, corpus, engine).excerpts
        assert (excerpts.images, excerpts.containers) == (1, 1)

    def test_a_reused_view_is_checked_again(
        self,
        tmp_path,
        engine,  # noqa: F811
    ):
        """A deck's view held from an earlier invocation that marks none
        of the images it reads is made again, as the run would make it:
        it is not counted as reused, and its letter is counted."""
        corpus = Corpus(tmp_path / "corpus")
        path = library_talk(corpus.folder / "library-talk-2018.pptx")
        corpus.texts["library-talk-2018.pptx"] = slide_transcript(
            unpack_pptx(path)
        )
        corpus.texts[f"{LETTER_SHA1}.png"] = EXCERPT
        run_scratch(tmp_path, corpus, engine, until="transcription")
        (view,) = runs_dir(tmp_path).glob(
            "*/library-talk-2018/transcription.json"
        )
        held = Transcription.model_validate_json(view.read_text())
        view.write_text(
            Transcription(
                artifact=held.artifact,
                text=slide_transcript(unpack_pptx(path)),
                engine="pptx-adapter",
            ).model_dump_json()
        )
        counted = estimate_of(tmp_path, corpus, engine)
        assert counted.stages["transcription"].reused == 0
        excerpts = counted.excerpts
        assert (excerpts.images, excerpts.containers) == (1, 1)
