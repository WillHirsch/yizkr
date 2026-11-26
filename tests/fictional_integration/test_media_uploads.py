"""MediaWiki's own handling of registration's uploads, on a lane stack.

Registration uploads each held source's bytes to MediaWiki and names the
file in the item's `media file` (docs/ARTIFACTS.md, "Files, display terms
and collisions"). These cases check what MediaWiki itself does with those
uploads, by its own information about its files: the SHA-1 of every
version of a file (`prop=imageinfo`) and its list of every file that holds
given bytes (`list=allimages` by `aisha1`), beside fresh API reads of the
items.

* A file is never overwritten and the same bytes are stored once:
  MediaWiki's `nochange` and `duplicate` warnings answer an upload with the
  file that holds its bytes, and its `exists` warning sends other bytes
  under the content-tagged name. A registration's upload gives the artifact
  its file page, and each item's `media file` holds the bytes its `media
  SHA-1` names.
* Two decks that place one image share its one file and its one item, each
  deck's `stated in` added to the item's single kind, hash and file
  statements (docs/ARTIFACTS.md, "Container sources").
* A deck under a long file name, with letters beyond ASCII and characters
  and sequences MediaWiki refuses in a file name, registered as a scratch
  run's write registers it, is stored under names MediaWiki takes, within
  its byte limits, and its description, made from its file name, fits the
  250 characters Wikibase takes.
* Bytes that two files hold, the second stored by an upload that overrides
  MediaWiki's warnings as registration's never does, are flagged
  `shared-file` against both items that name those files, from MediaWiki's
  list of the files that hold them, on a first registration and again on a
  re-run.

Each case registers again through a fresh client that counts the uploads
asked of it: the repeat uploads nothing and makes no revision anywhere on
the stack. The cases share their stack with another required case, so their
bytes, titles and file names are this module's own, and the last, which
leaves items with no hash behind, runs last on its stack
(scripts/fictional_integration.py). The media and their names are fictional
(docs/EXAMPLES.md); no model is called.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference, References

from yizkr.core.corpus import ExclusionList
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.container_inventory import keep_inventory
from yizkr.pipeline.container_sources import project_sources
from yizkr.pipeline.media_stages import PreparationRequest, prepare_container
from yizkr.pipeline.registrar import SHARED_FILE, Registrar
from yizkr.pipeline.scratch import ScratchDriver

pytestmark = pytest.mark.live

DESCRIPTION = "fictional lane fixture: media uploads"
# the file types these cases upload, each of which the lane's wiki must
# allow (deploy/config/Uploads.php)
FILE_TYPES = ("png", "pptx")
# MediaWiki takes at most 240 bytes in an uploaded file's name, which an
# archived version prefixes with a timestamp and "!" within 255; a name
# registration makes leaves room for the content tag, " <sha1[:8]>"
FILE_NAME_BYTES = 240
PLAIN_NAME_BYTES = FILE_NAME_BYTES - 9
# Wikibase's most characters in a label or description
TERM_CHARACTERS = 250


@pytest.fixture(scope="module")
def wiki(fictional_client):
    """The lane's client, once its wiki is shown to take the uploads these
    cases make: a case whose upload it would refuse fails here, naming the
    setting the lane's deployment controls lack."""
    found = fictional_client.api_get(
        {
            "action": "query",
            "meta": "siteinfo",
            "siprop": "general|fileextensions",
            "formatversion": "2",
        }
    )["query"]
    allowed = {entry["ext"] for entry in found.get("fileextensions", [])}
    missing = [
        f"$wgFileExtensions has no {kind}"
        for kind in FILE_TYPES
        if kind not in allowed
    ]
    if found.get("general", {}).get("uploadsenabled") is not True:
        missing.insert(0, "$wgEnableUploads is off")
    if missing:
        pytest.fail(
            "The lane's wiki refuses the uploads these cases make: "
            + "; ".join(missing)
            + ". The lane mounts its upload settings from "
            "deploy/config/Uploads.php.",
            pytrace=False,
        )
    return fictional_client


class CountingClient(WikiClient):
    """A fresh client that records the file name of each upload asked of
    it, where registration asks the client to store a file."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self.uploads: list[str] = []

    def upload_file(self, path, *args, **kwargs) -> str:
        self.uploads.append(path.name)
        return super().upload_file(path, *args, **kwargs)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def png(text: str) -> bytes:
    """A valid 4x4 PNG whose text chunk holds `text`: its bytes are this
    text's alone, and the same text always gives the same bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    pixels = b"".join(b"\x00" + b"\x5c\x7a\x3e" * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + text.encode("unicode_escape"))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def put(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


# -- decks --------------------------------------------------------------------

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
PRESENTATIONML = "application/vnd.openxmlformats-officedocument.presentationml"
ZIP_TIME = (2020, 1, 1, 0, 0, 0)  # the same members give the same bytes


def relationships(*entries: tuple[str, str, str]) -> str:
    return (
        f'<Relationships xmlns="{RELATIONSHIPS}">'
        + "".join(
            f'<Relationship Id="{rid}" Type="{R}/{kind}" '
            f'Target="{escape(target)}"/>'
            for rid, kind, target in entries
        )
        + "</Relationships>"
    )


def slide(text: str, *, painted: bool) -> str:
    """A slide of one line of `text`, painting the image its relationship
    `image` names when `painted`."""
    picture = (
        '<p:pic><p:nvPicPr><p:cNvPr id="3" name="photograph"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        '<p:blipFill><a:blip r:embed="image"/></p:blipFill>'
        '<p:spPr><a:xfrm><a:off x="100" y="200"/>'
        '<a:ext cx="300" cy="400"/></a:xfrm></p:spPr></p:pic>'
        if painted
        else ""
    )
    return (
        f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
        '<p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="text"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>"
        f"<a:lstStyle/><a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>"
        f"</p:txBody></p:sp>{picture}</p:spTree></p:cSld></p:sld>"
    )


def deck(
    path: Path,
    texts: list[str],
    painted: int,
    image: bytes,
    image_name: str = "image1.png",
) -> Path:
    """A deck of a slide for each of `texts`, its image painted on slide
    `painted`, under `image_name` in the deck's archive. The same arguments
    give the same bytes."""
    numbers = range(1, len(texts) + 1)
    members = {
        # first in the archive, where MediaWiki looks for it to tell an
        # Office file from any other ZIP archive
        "[Content_Types].xml": (
            f'<Types xmlns="{TYPES}"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/ppt/presentation.xml" '
            f'ContentType="{PRESENTATIONML}.presentation.main+xml"/>'
            + "".join(
                f'<Override PartName="/ppt/slides/slide{n}.xml" '
                f'ContentType="{PRESENTATIONML}.slide+xml"/>'
                for n in numbers
            )
            + "</Types>"
        ),
        "_rels/.rels": relationships(
            ("document", "officeDocument", "ppt/presentation.xml")
        ),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            + "".join(
                f'<p:sldId id="{255 + n}" r:id="slide{n}"/>' for n in numbers
            )
            + "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": relationships(
            *((f"slide{n}", "slide", f"slides/slide{n}.xml") for n in numbers)
        ),
    }
    for n, text in zip(numbers, texts, strict=True):
        image_link = ("image", "image", f"../media/{image_name}")
        members[f"ppt/slides/slide{n}.xml"] = slide(text, painted=n == painted)
        members[f"ppt/slides/_rels/slide{n}.xml.rels"] = (
            relationships(image_link) if n == painted else relationships()
        )
    members[f"ppt/media/{image_name}"] = image
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=ZIP_TIME), payload
            )
    return path


# -- what the wiki holds ------------------------------------------------------


def history(client: WikiClient, page: str) -> list[str]:
    """MediaWiki's SHA-1 of each version of the file `page`, newest
    first."""
    found = client.api_get(
        {
            "action": "query",
            "titles": page,
            "prop": "imageinfo",
            "iiprop": "sha1",
            "iilimit": "max",
        }
    )
    (info,) = found["query"]["pages"].values()
    return [version["sha1"] for version in info.get("imageinfo", [])]


def holders(client: WikiClient, data: bytes) -> set[str]:
    """The page of every file MediaWiki lists as holding `data` now."""
    found = client.api_get(
        {
            "action": "query",
            "list": "allimages",
            "aisha1": sha1(data),
            "ailimit": "max",
        }
    )
    return {image["title"] for image in found["query"]["allimages"]}


def watermark(client: WikiClient) -> dict[str, int]:
    """The revision of every entity on the stack."""
    return client.entity_revisions(client.list_entity_ids())


def value(claim: dict):
    """A statement's main value, an item by its QID."""
    found = claim["mainsnak"]["datavalue"]["value"]
    return found["id"] if isinstance(found, dict) else found


def values(statements: dict, pid: str) -> list:
    """The main value of each statement of `pid`."""
    return [value(claim) for claim in statements.get(pid, [])]


def cited(claim: dict, stated_in: str) -> set[str]:
    """The items `claim`'s references say it is stated in."""
    return {
        snak["datavalue"]["value"]["id"]
        for reference in claim.get("references", [])
        for snak in reference["snaks"].get(stated_in, [])
    }


def by_number(*qids: str) -> tuple[str, ...]:
    return tuple(sorted(qids, key=lambda qid: int(qid[1:])))


# -- uploads, hashes and files ------------------------------------------------


def test_a_file_is_never_overwritten_and_bytes_are_stored_once(wiki, tmp_path):
    client = CountingClient(wiki.config)
    stored = png("Gustav's birth register extract, scanned for emigration")
    other = png("Gustav's birth register extract, scanned again")
    name = "Gustav Löbig, birth register extract.png"

    page = client.upload_file(put(tmp_path / "scan", name, stored))
    assert history(client, page) == [sha1(stored)]
    assert holders(client, stored) == {page}
    # the same bytes under the same name, then under another: MediaWiki's
    # nochange and duplicate warnings, each answered with the stored file
    assert client.upload_file(put(tmp_path / "again", name, stored)) == page
    copy = put(tmp_path / "copy", f"{Path(name).stem}, copy.png", stored)
    assert client.upload_file(copy) == page
    assert holders(client, stored) == {page}
    # other bytes under the stored name: MediaWiki's exists warning sends
    # them under the content-tagged name, and the stored file keeps its
    # bytes in its one version
    tagged = client.upload_file(put(tmp_path / "rescan", name, other))
    assert tagged == f"{page.removesuffix('.png')} {sha1(other)[:8]}.png"
    assert history(client, page) == [sha1(stored)]
    assert history(client, tagged) == [sha1(other)]
    # and again: the tagged file is found, not stored twice
    again = put(tmp_path / "rescan-again", name, other)
    assert client.upload_file(again) == tagged
    assert holders(client, other) == {tagged}

    # two scans under one file name and one title, not of the same bytes,
    # each of two pages, and a copy of the first under another name and
    # title
    title = "Gustav's birth register extract"
    scans = (
        png("Gustav's birth register extract, issued for emigration"),
        png("Gustav's birth register extract, the family's copy"),
    )
    sources = (
        (put(tmp_path / "emigration", "gustav-extract.png", scans[0]), title),
        (put(tmp_path / "copies", "gustav-extract.png", scans[1]), title),
        (
            put(tmp_path / "kept", "gustav-extract-copy.png", scans[0]),
            f"{title}, a copy",
        ),
    )

    def register(client):
        registrar = Registrar(client)
        return [
            registrar.register_artifact(
                path, "certificate", named, DESCRIPTION, pages=2
            )
            for path, named in sources
        ]

    first, second, copied = register(client)
    assert first.created and second.created and not copied.created
    assert second.artifact_qid != first.artifact_qid
    assert copied.artifact_qid == first.artifact_qid
    assert copied.file_page == first.file_page
    # the changed scan's file is under the content-tagged name: the first
    # scan's file is never overwritten
    assert second.file_page == (
        f"{first.file_page.removesuffix('.png')} {sha1(scans[1])[:8]}.png"
    )
    # each upload gives its artifact its file page, which holds, alone, the
    # bytes the artifact's media SHA-1 names
    properties, _ = client.vocabulary()
    for result, data in ((first, scans[0]), (second, scans[1])):
        qid = result.artifact_qid
        statements = WikiClient(wiki.config).fetch_claims([qid])[qid]
        assert values(statements, properties["media_sha1"]) == [sha1(data)]
        assert values(statements, properties["media_file"]) == [
            result.file_page
        ]
        assert history(client, result.file_page) == [sha1(data)]
        assert holders(client, data) == {result.file_page}
        assert sorted(result.fragment_qids) == [1, 2]

    # registered again through a fresh client: every artifact and fragment
    # is found, nothing is uploaded and nothing is written
    before = watermark(client)
    fresh = CountingClient(wiki.config)
    again = register(fresh)
    assert [(r.artifact_qid, r.fragment_qids) for r in again] == [
        (r.artifact_qid, r.fragment_qids) for r in (first, second, copied)
    ]
    assert not any(result.created for result in again)
    assert fresh.uploads == []
    assert watermark(fresh) == before
    assert [holders(fresh, data) for data in scans] == [
        {first.file_page},
        {second.file_page},
    ]


# -- two decks, one image -----------------------------------------------------


def register_deck(client, path: Path, directory: Path, title: str, slides):
    """Register a deck as the write does: its container inventory kept, the
    deck with a page fragment for each slide, then the media its container
    projection binds. Returns (the deck's registration, {media SHA-1 ->
    QID})."""
    prepared = prepare_container(
        PreparationRequest(path, existing_directory=directory)
    )
    keep_inventory(prepared.inventory, prepared.data, directory)
    registrar = Registrar(client)
    registered = registrar.register_artifact(
        path, "presentation", title, DESCRIPTION, pages=slides
    )
    sources = project_sources(
        prepared.inventory, directory, data=prepared.data
    )
    media = registrar.register_container_sources(
        sources, directory, title=title
    )
    return registered, media


def test_two_decks_placing_one_image_share_its_file_and_its_item(
    wiki, tmp_path
):
    client = WikiClient(wiki.config)
    properties, items = client.vocabulary()
    stated_in = properties["stated_in"]
    photograph = png("the album photograph of Wastl and Hilde, 1906")
    decks = (
        (
            deck(
                tmp_path / "where-the-lobigs-came-from.pptx",
                ["Where the Lobigs came from"],
                1,
                photograph,
            ),
            "Where the Lobigs came from",
            1,
        ),
        (
            deck(
                tmp_path / "where-the-lobigs-came-from-handout.pptx",
                ["A handout", "Wastl and Hilde, from the family album"],
                2,
                photograph,
            ),
            "Where the Lobigs came from, a handout",
            2,
        ),
    )

    def register(client, path, title, slides, folder):
        return register_deck(
            client, path, tmp_path / folder / path.stem, title, slides
        )

    talk, media = register(client, *decks[0], "state")
    assert sorted(talk.fragment_qids) == [1]
    (stored,) = holders(client, photograph)
    handout, handout_media = register(client, *decks[1], "state")
    assert sorted(handout.fragment_qids) == [1, 2]
    assert handout.artifact_qid != talk.artifact_qid
    # one item for the one image, and its bytes are stored no second time
    (image,) = media.values()
    assert media == handout_media == {sha1(photograph): image}
    assert holders(client, photograph) == {stored}

    statements = WikiClient(wiki.config).fetch_claims([image])[image]
    for claims in statements.values():
        for claim in claims:
            assert cited(claim, stated_in), claim
    # part of each deck, on the slide that paints the image, stated in
    # that deck
    parts: dict[str, list] = {}
    for claim in statements[properties["part_of"]]:
        whole = claim["mainsnak"]["datavalue"]["value"]["id"]
        slides = [
            snak["datavalue"]["value"]
            for snak in claim.get("qualifiers", {}).get(
                properties["page_number"], []
            )
        ]
        parts.setdefault(whole, []).append((slides, cited(claim, stated_in)))
    assert parts == {
        talk.artifact_qid: [(["1"], {talk.artifact_qid})],
        handout.artifact_qid: [(["2"], {handout.artifact_qid})],
    }
    # its kind, hash and file are one statement each, which the second deck
    # cites beside the first rather than stating again
    both = {talk.artifact_qid, handout.artifact_qid}
    for key, expected in (
        ("instance_of", items["photograph"]),
        ("media_sha1", sha1(photograph)),
        ("media_file", stored),
    ):
        (claim,) = statements[properties[key]]
        assert value(claim) == expected
        assert cited(claim, stated_in) == both

    # both decks registered again through a fresh client: the same items,
    # no upload and no revision
    before = watermark(client)
    fresh = CountingClient(wiki.config)
    again = [register(fresh, *entry, "again") for entry in decks]
    assert [(result.artifact_qid, found) for result, found in again] == [
        (talk.artifact_qid, media),
        (handout.artifact_qid, media),
    ]
    assert fresh.uploads == []
    assert watermark(fresh) == before


# -- a deck's long name, through write-time registration ----------------------

# The deck's title, with "{}" where it holds a character or the start of a
# sequence MediaWiki refuses in a file name: ":", a "%" hex escape, "~~~",
# "[" and "]", and "#". In the file name's form each of these is "-", and
# every letter is kept. Its file name is longer than a registered file's
# name may be, and the description made from it longer than Wikibase takes.
TITLE_FORM = "Лёбиг{} 100{}2F {}~~ " + "; ".join(
    "Hutmacherei zum Blauen Hut, Brunnenring 35, Flörburg {}gegr. 1863{} {}"
    + str(n)
    for n in (1, 2, 3)
)
DECK_TITLE = TITLE_FORM.format(":", "%", "~", *"[]#[]#[]#")
DECK_NAME = f"{DECK_TITLE}.pptx"
TITLE_NAME_FORM = TITLE_FORM.format(*"-" * TITLE_FORM.count("{}"))
# the shop front, under its name in the deck's archive
IMAGE_FORM = "Flörburg, Brunnenring 35 {}1912{}.png"
IMAGE_NAME = IMAGE_FORM.format("[", "]")
IMAGE_NAME_FORM = IMAGE_FORM.format("-", "-")


class Write(ScratchDriver):
    """A scratch run over one corpus folder whose write registers through
    `lane_client`, with an empty never-ingest list. Its registration is the
    scratch driver's own register step until write-time registration is a
    component of its own (#865)."""

    def __init__(self, where: Path, lane_client: WikiClient) -> None:
        super().__init__(
            where / "corpus",
            where / "state",
            cache_dir=where / "cache",
            exclusions=ExclusionList(),
            log=lambda *args, **kwargs: None,
        )
        self.lane_client = lane_client

    @property
    def client(self) -> WikiClient:
        return self.lane_client

    def register(self) -> tuple[str, dict[str, str]]:
        """The write's registration of the folder's one document: its QID,
        and its media's QIDs by SHA-1."""
        (document,) = self.documents()
        qid = self._step_register(document)
        return qid, self.registered_media()[document.sha1]


def written(where: Path, image: bytes, client: WikiClient):
    """The family firm's deck, showing `image` on its one slide, registered
    by a write of a run of its own: (its bytes, its QID, its media's QIDs by
    SHA-1)."""
    path = deck(
        where / "corpus" / DECK_NAME,
        ["Hutmacherei zum Blauen Hut, Brunnenring 35"],
        1,
        image,
        IMAGE_NAME,
    )
    return (path.read_bytes(), *Write(where, client).register())


def test_a_decks_long_file_name_takes_forms_mediawiki_takes(wiki, tmp_path):
    client = WikiClient(wiki.config)
    shop_front = png("the shop front of the Hutmacherei zum Blauen Hut")
    rescanned = png("the shop front of the Hutmacherei zum Blauen Hut, again")

    deck_bytes, qid, media = written(tmp_path / "first", shop_front, client)

    # the deck's file and its image's, under their names in the form
    # MediaWiki takes: each refused character and sequence made "-", every
    # letter kept, cut, ending in "…", within the bytes a name may take,
    # and their extensions kept; the image's after the deck's title
    (deck_file,) = holders(client, deck_bytes)
    (image_file,) = holders(client, shop_front)
    for page, end in (
        (deck_file, "….pptx"),
        (image_file, f"… {IMAGE_NAME_FORM}"),
    ):
        name = page.removeprefix("File:")
        assert name.endswith(end), name
        assert TITLE_NAME_FORM.startswith(name.removesuffix(end)), name
        assert len(name.encode()) <= PLAIN_NAME_BYTES, name
    # the description made from the deck's file name, cut to fit
    ((_, entity),) = WikiClient(wiki.config).entity_data([qid])
    description = entity["descriptions"]["en"]["value"]
    made = f"ingested by `{ScratchDriver.command}` from {DECK_NAME}"
    assert len(made) > TERM_CHARACTERS
    assert description == made[: TERM_CHARACTERS - 1].rstrip() + "…"

    # the deck revised under the same name: both its file and its image's
    # take the content-tagged form of the names the first deck's files
    # hold, within MediaWiki's limit, and those files keep their bytes
    revised_bytes, revised, revised_media = written(
        tmp_path / "second", rescanned, client
    )
    assert revised != qid
    assert set(revised_media) == {sha1(rescanned)}
    for data, plain, held in (
        (revised_bytes, deck_file, deck_bytes),
        (rescanned, image_file, shop_front),
    ):
        stem, suffix = plain.rsplit(".", 1)
        tagged = f"{stem} {sha1(data)[:8]}.{suffix}"
        assert holders(client, data) == {tagged}
        assert len(tagged.removeprefix("File:").encode()) <= FILE_NAME_BYTES
        assert history(client, plain) == [sha1(held)]

    # a third write, through a fresh client, finds both files and every
    # item: it uploads nothing and writes nothing
    before = watermark(client)
    fresh = CountingClient(wiki.config)
    assert written(tmp_path / "third", rescanned, fresh)[1:] == (
        revised,
        revised_media,
    )
    assert fresh.uploads == []
    assert watermark(fresh) == before


# -- bytes two files hold -----------------------------------------------------


def stored_again(client: WikiClient, path: Path) -> str:
    """Upload `path` with MediaWiki's warnings overridden, as no
    registration does, so that MediaWiki stores bytes it holds already under
    a second name; returns the page of the file it stored."""
    with path.open("rb") as handle:
        response = client.login.get_session().post(
            client.api_url,
            data={
                "action": "upload",
                "filename": path.name,
                "comment": "fictional lane fixture: a second file",
                "ignorewarnings": "1",
                "token": client.login.get_edit_token(),
                "format": "json",
            },
            files={"file": (path.name, handle)},
            timeout=300,
        )
    response.raise_for_status()
    answer = response.json()
    upload = answer.get("upload") or {}
    assert upload.get("result") == "Success", answer
    return f"File:{upload['filename'].replace('_', ' ')}"


def unhashed(client: WikiClient, label: str, page: str) -> str:
    """An item as registration made one before content hashing: a label,
    `instance of photograph` and a `media file` naming `page`, each stated
    in the item itself, and no `media SHA-1`. Written straight through
    WikibaseIntegrator, since no registration writes one."""
    properties, items = client.vocabulary()
    item = client.wbi.item.new()
    item.labels.set("en", label)
    item.descriptions.set("en", DESCRIPTION)
    item = item.write(summary="fictional lane fixture: an item with no hash")

    def stated_in_itself() -> References:
        reference = Reference()
        reference.add(Item(prop_nr=properties["stated_in"], value=item.id))
        references = References()
        references.add(reference)
        return references

    item.claims.add(
        [
            Item(
                prop_nr=properties["instance_of"],
                value=items["photograph"],
                references=stated_in_itself(),
            ),
            String(
                prop_nr=properties["media_file"],
                value=page,
                references=stated_in_itself(),
            ),
        ]
    )
    return item.write(
        summary="fictional lane fixture: an item with no hash"
    ).id


def test_bytes_two_files_hold_are_flagged_against_both_items(wiki, tmp_path):
    client = WikiClient(wiki.config)
    # items made with no registration need the deployment's generation
    # first, which a registration would establish on an empty stack
    CreationRegistry(client).ensure_generation()
    properties, _ = client.vocabulary()
    portrait = png("the Löbig family's studio portrait of the 1890s")
    first_page = client.upload_file(
        put(tmp_path / "scan", "Löbig studio portrait 1890s.png", portrait)
    )
    second_page = stored_again(
        client,
        put(
            tmp_path / "catalogue", "Löbig studio portrait, list.png", portrait
        ),
    )
    old = unhashed(client, "The Löbig family's studio portrait", first_page)
    listed = unhashed(
        client, "The Löbig family's studio portrait, listed", second_page
    )
    # the fixture, read afresh: two files hold the bytes, and each item
    # names one of them and has no hash
    reader = WikiClient(wiki.config)
    assert holders(reader, portrait) == {first_page, second_page}
    statements = reader.fetch_claims([old, listed])
    for qid, page in ((old, first_page), (listed, second_page)):
        assert values(statements[qid], properties["media_file"]) == [page]
        assert values(statements[qid], properties["media_sha1"]) == []

    # the bytes registered under another title and file name
    title = "The Löbig family's studio portrait, a copy"
    copy = put(tmp_path / "copy", "loebig-studio-portrait-copy.png", portrait)
    before = watermark(client)
    counting = CountingClient(wiki.config)
    result = Registrar(counting).register_artifact(
        copy, "photograph", title, DESCRIPTION
    )
    # its one upload meets MediaWiki's duplicate warning, which answers it
    # with a file that holds the bytes: none is stored a third time
    assert counting.uploads == [copy.name]
    assert result.file_page in {first_page, second_page}
    assert holders(reader, portrait) == {first_page, second_page}
    # flagged against both items: the one naming the other file is found
    # by MediaWiki's list of the files that hold the bytes
    assert [(f.kind, f.artifact, f.sha1, f.others) for f in result.flags] == [
        (
            SHARED_FILE,
            result.artifact_qid,
            sha1(portrait),
            by_number(old, listed),
        )
    ]
    # the new item has the hash and the file; nothing else is written
    new = reader.fetch_claims([result.artifact_qid])[result.artifact_qid]
    assert values(new, properties["media_sha1"]) == [sha1(portrait)]
    assert values(new, properties["media_file"]) == [result.file_page]
    after = watermark(reader)
    assert after == {**before, result.artifact_qid: after[result.artifact_qid]}

    # registered again through a fresh client: found by its hash and flagged
    # again as it was, with no upload and no revision
    fresh = CountingClient(wiki.config)
    registrar = Registrar(fresh)
    again = registrar.register_artifact(copy, "photograph", title, DESCRIPTION)
    assert again.artifact_qid == result.artifact_qid and not again.created
    assert again.flags == registrar.flags == result.flags
    assert fresh.uploads == []
    assert watermark(fresh) == after
