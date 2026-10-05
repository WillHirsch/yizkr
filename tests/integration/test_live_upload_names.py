"""Live upload names and descriptions (#373): the write's registration of
a deck (`Orchestrator._step_register`) whose file name is long, holds
letters beyond ASCII, characters MediaWiki refuses in a file name and
sequences for which it refuses a name (``illegal-filename``: a "%" hex
escape and "~~~"), and makes a description longer than the 250
characters Wikibase takes.

* The deck's bytes are uploaded under its file name in the form MediaWiki
  takes as it is: every letter kept, "-" for each character it refuses
  and at the start of each sequence it refuses, and cut, ending in "…",
  within 231 bytes, its extension kept.
* The image the deck embeds is uploaded under ``"<deck title> <its
  name>"``, its title cut, ending in "…", and its own name whole. Another
  file holds that name, so the image takes the content-tagged name, 9
  bytes longer, within MediaWiki's 240.
* The deck's description, ``"ingested by `yizkr ingest run` from <file
  name>"``, is cut to 250 characters, ending in "…".
* A second write finds both files and every item, and uploads and writes
  nothing.

Fictional synthetic media only (docs/EXAMPLES.md). The deck's title and
file name, and the bytes of its image and of the other file, carry a
marker: eight hex digits of a hash over the fixture, over the source of the
code that names the files and writes the description (the registrar, the
wiki client and the orchestrator) and over eight random bytes drawn once a
session (conftest.fixture_mark). So each session, and each version of the
fixture and of that code, uploads and registers the fixture as a first
write does: a mutant of the harness cannot pass by finding what an earlier
session uploaded, and what a live mutant's run wrote never meets a later
session. A re-run within the session finds its own files and items in
place, and writes nothing. Like every live test it writes fictional data to
the one instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from tests.integration.test_live_presentation import files_with_bytes
from tests.integration.test_live_write_time_registration import (
    FIXED_TIME,
    parts_of,
    revisions,
)
from tests.unit.test_presentation import (
    IMAGE_REL,
    SLIDE_REL,
    rels_xml,
    slide_xml,
)
from yizkr.core import wiki
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import orchestrate, registrar
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.registrar import _upload_name

from .conftest import fictional_png, fixture_mark

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new files and items
FIXTURE = (
    "#373: the family firm's deck, under a long file name in German and "
    "Russian that holds sequences a title refuses, and the shop front it "
    "shows"
)
MARK = fixture_mark(FIXTURE, registrar, wiki, orchestrate)

# the deck's title, its marker and the sequences MediaWiki refuses
# first, so that both cuts leave them: of 235 characters, within the 250
# a label takes; its file name, of 248 bytes, within the 255 a file's
# name takes on disk
DECK_TITLE = f"Лёбиг {MARK}: 100%2F ~~~ " + "; ".join(
    f"Hutmacherei zum Blauen Hut, Brunnenring 35, Flörburg [gegr. 1863] #{n}"
    for n in (1, 2, 3)
)
DECK_NAME = f"{DECK_TITLE}.pptx"
# the shop front, under its name in the deck's archive, and other bytes
# that another file holds under the name the image is uploaded under
IMAGE_NAME = "Flörburg, Brunnenring 35 [1912].png"
IMAGE = fictional_png(f"yizkr live test #373: the shop front {MARK}")
OTHER = fictional_png(f"yizkr live test #373: another file {MARK}")
DESCRIPTION = f"ingested by `yizkr ingest run` from {DECK_NAME}"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def build_deck(path):
    """The deck: one slide, the shop front on it. The same bytes for the
    same marker."""
    presentation = (
        "<p:presentation"
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
        "</p:presentation>"
    )

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=FIXED_TIME), data)

    with zipfile.ZipFile(path, "w") as archive:
        put(archive, "[Content_Types].xml", "<Types/>")
        put(archive, "ppt/presentation.xml", presentation)
        put(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml([("rId1", SLIDE_REL, "slides/slide1.xml")]),
        )
        put(
            archive,
            "ppt/slides/slide1.xml",
            slide_xml(["Hutmacherei zum Blauen Hut, Brunnenring 35"]),
        )
        put(
            archive,
            "ppt/slides/_rels/slide1.xml.rels",
            rels_xml([("rId7", IMAGE_REL, f"../media/{IMAGE_NAME}")]),
        )
        put(archive, f"ppt/media/{IMAGE_NAME}", IMAGE)
    return path


def register(client, where) -> str:
    """The deck's registration by a write of a run of its own: its QID."""
    folder = where / "corpus"
    folder.mkdir(parents=True)
    build_deck(folder / DECK_NAME)
    orchestrator = Orchestrator(
        folder,
        where / "state",
        exclusions=ExclusionList(),
        log=lambda *args, **kwargs: None,
    )
    orchestrator._client = client
    (doc,) = orchestrator.documents()
    return orchestrator._step_register(doc)


def only_file(client, data: bytes) -> str:
    """The name of the one wiki file that holds `data`, with spaces."""
    (name,) = files_with_bytes(client, sha1(data))
    return name.replace("_", " ")


def test_long_names_and_descriptions_fit(client, tmp_path, monkeypatch):
    assert len(DECK_NAME.encode()) == 248
    assert len(DECK_TITLE) == 235 and len(DESCRIPTION) > 250
    # another file holds the name the image is uploaded under: the image
    # takes its content-tagged form, 9 bytes longer
    taken = _upload_name(IMAGE_NAME, DECK_TITLE, "presentation")
    other = tmp_path / "other.png"
    other.write_bytes(OTHER)
    client.upload_file(other, filename=taken)

    deck = register(client, tmp_path / "first")

    deck_bytes = build_deck(tmp_path / "deck.pptx").read_bytes()
    deck_file = only_file(client, deck_bytes)
    # "%2F" and "~~~" are broken, or MediaWiki would refuse the name
    assert deck_file.startswith(
        f"Лёбиг {MARK}- 100-2F -~~ Hutmacherei zum Blauen Hut, "
        f"Brunnenring 35, Flörburg -gegr. 1863- -1; "
    )
    assert deck_file.endswith("….pptx")
    assert len(deck_file.encode()) <= 231
    image_file = only_file(client, IMAGE)
    assert image_file.startswith(
        f"Лёбиг {MARK}- 100-2F -~~ Hutmacherei zum Blauen Hut"
    )
    assert image_file.endswith(
        f"… Flörburg, Brunnenring 35 -1912- {sha1(IMAGE)[:8]}.png"
    )
    assert len(image_file.encode()) <= 240
    assert only_file(client, OTHER) == taken

    entity = client.api_get(
        {"action": "wbgetentities", "ids": deck, "props": "descriptions"}
    )["entities"][deck]
    description = entity["descriptions"]["en"]["value"]
    assert description == DESCRIPTION[:249].rstrip() + "…"

    # a second write finds both files and every item: it uploads nothing
    # and writes nothing
    parts = parts_of(client, deck)
    touched = sorted(
        {deck}.union(*parts["fragment"].values(), *parts["other"].values())
    )
    before = revisions(client, *touched)
    uploads: list[str | None] = []
    upload_file = client.upload_file

    def counted(path, filename=None, **options):
        uploads.append(filename)
        return upload_file(path, filename, **options)

    monkeypatch.setattr(client, "upload_file", counted)
    assert register(client, tmp_path / "second") == deck
    assert uploads == []
    assert revisions(client, *touched) == before
    assert only_file(client, IMAGE) == image_file
