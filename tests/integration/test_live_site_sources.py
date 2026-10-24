"""Live static-site build of what sources say about an artifact (#550,
ADR-0039 §7): the site's query of captions and document-subject
statements runs against the running stack, and the pages it builds show
them on data this module seeds itself.

The booklet (*Our Family from Germany*, under a test title) is registered
with the image of the letter it places on its page 6 as its derived
medium, as the write registers a PDF read page by page (ADR-0008, as
amended by #397). One batch writes the booklet's caption over the image,
"Wastl to Klara, July 1939.", stated in the booklet on its page 6, printed
4 (ADR-0037); the image's own dateline (28 July 1939, day precision), its
heading's address and its greeting's addressee, Klara; and the studio
portrait's own studio and two of the names on its reverse, Rahel and
Felix, as `shows`; and Rahel's portrait's own date of making, pencilled
"um 1895" on its mount: 1895 at year precision, with circa as its
sourcing circumstance (ADR-0039 §2.2, #601). The site is built from the
whole instance, and the image's, the booklet's and both portraits' pages
and Klara's, Rahel's and Felix's are read.

Every title, label, caption and media byte carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write. Pages that list the whole instance are read only for the
fixture's own entries, and the generated site is deleted after the
module.
"""

from __future__ import annotations

import html
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from tests.integration.conftest import (
    fictional_png,
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)
from yizkr import staticsite
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.presentation import (
    DERIVED_MEDIA_DIR,
    DerivedMediaRecord,
    DerivedMedium,
)
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import publish_site

pytestmark = pytest.mark.live

UNIVERSE = u.by_key()
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#550: a booklet whose page 6 (printed 4) places an image of a letter, "
    "its caption over the image, the image's own dateline, heading and "
    "greeting, and the studio portrait's studio and two of the names on "
    "its reverse; Rahel's portrait's own circa, year-precision inception"
)
MARK = f"(sources test {fixture_mark(FIXTURE, registrar, writer, staticsite)})"


def marked(key: str) -> str:
    """The universe's label (or title) for `key`, with this module's
    marker."""
    entry = UNIVERSE[key]
    return f"{getattr(entry, 'title', None) or entry.label} {MARK}"


BOOKLET = marked("family-booklet-2011")
PORTRAIT = marked("studio-photo-1890s")
RAHELS_PORTRAIT = marked("studio-photo-1895")
# Rahel's portrait dates its own making, "um 1895" on its mount
(MADE,) = u.about("studio-photo-1895", "inception")
KLARA = marked("klara-loebig")
RAHEL = marked("rahel-loebig")
FELIX = marked("felix-loebig")
ADDRESS = marked("brunnenring-35")
STUDIO = marked("studio-bruesrich")
CAPTION = f"Wastl to Klara, July 1939. {MARK}"


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def item(ref: dict) -> dict:
    return {"type": "item", "value": ref}


def time_value(date: u.Date) -> dict:
    """`date` as a time value at its own precision, decade included. A
    range ("between") needs earliest and latest qualifiers, which this
    fixture does not write, so it is refused rather than narrowed."""
    if date.qualifier == "between":
        raise ValueError(f"a range is no single time value: {date}")
    parts = {"year": date.year, "month": date.month, "day": date.day}
    return {
        "type": "time",
        **{k: v for k, v in parts.items() if v},
        "precision": date.precision,
    }


def fixture_batch(
    booklet: ArtifactRef,
    image: ArtifactRef,
    portrait: ArtifactRef,
    rahels_portrait: ArtifactRef,
) -> ClaimBatch:
    """The booklet's caption on the image, what the image states of
    itself and what each portrait states of itself, each citing its
    source by the SHA-1 of its media."""
    klara = local("klara", KLARA, "human")
    rahel = local("rahel", RAHEL, "human")
    felix = local("felix", FELIX, "human")
    address = local("address", ADDRESS, "place")
    studio = local("studio", STUDIO, "organization")

    def claim(
        subject, prop, value, source, excerpt, language, qualifiers=(), **where
    ):
        return {
            "subject": subject.model_dump(),
            "property": prop,
            "value": value,
            "qualifiers": list(qualifiers),
            "source": {
                "artifact": source.model_dump(),
                **where,
                "transcription_excerpt": {
                    "text": excerpt,
                    "language": language,
                },
            },
        }

    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                claim(
                    image,
                    "caption",
                    {
                        "type": "monolingual",
                        "value": {"text": CAPTION, "language": "en"},
                    },
                    booklet,
                    CAPTION,
                    "en",
                    page_number="6",
                    printed_page_number="4",
                ),
                claim(
                    image,
                    "inception",
                    {"type": "time", "year": 1939, "month": 7, "day": 28},
                    image,
                    "den 28. Juli 1939",
                    "de",
                ),
                claim(
                    image,
                    "location_of_creation",
                    item(address),
                    image,
                    "Flörburg, Brunnenring 35, den",
                    "de",
                ),
                claim(
                    image,
                    "addressee",
                    item(klara),
                    image,
                    "Liebes Pünktchen",
                    "de",
                ),
                claim(
                    portrait,
                    "author",
                    item(studio),
                    portrait,
                    "Photograph Brüsrich",
                    "de",
                ),
                claim(portrait, "shows", item(rahel), portrait, "Rahel", "de"),
                claim(portrait, "shows", item(felix), portrait, "Felix", "de"),
                claim(
                    rahels_portrait,
                    MADE.property,
                    time_value(MADE.value),
                    rahels_portrait,
                    MADE.excerpt,
                    "de",
                    [
                        {
                            "property": "sourcing_circumstances",
                            "value": item(
                                {"kind": "vocab", "key": MADE.value.qualifier}
                            ),
                        }
                    ],
                ),
            ],
        }
    )


@dataclass(frozen=True)
class Site:
    out: Path
    booklet: str  # the booklet's QID
    image: str  # the letter's image's QID
    image_label: str
    portrait: str
    rahels_portrait: str
    people: dict[str, str]  # "klara", "rahel", "felix" -> QID

    def read(self, relative: str) -> str:
        return (self.out / relative).read_text()


@pytest.fixture(scope="module")
def site(client, config, tmp_path_factory):
    for excerpt in (
        "den 28. Juli 1939",
        "Flörburg, Brunnenring 35, den",
        "Liebes Pünktchen",
    ):
        assert excerpt in UNIVERSE["letter-excerpt-1939"].text
    for excerpt in ("Photograph Brüsrich", "Rahel", "Felix"):
        assert excerpt in UNIVERSE["studio-photo-1890s"].text
    assert MADE.source == "studio-photo-1895"
    assert MADE.value == u.Date(1895, qualifier="circa")
    assert MADE.excerpt == "um 1895"
    assert MADE.excerpt in UNIVERSE["studio-photo-1895"].text
    booklet = register_fictional(client, BOOKLET, "artifact")
    portrait = register_fictional(client, PORTRAIT, "photograph")
    rahels_portrait = register_fictional(client, RAHELS_PORTRAIT, "photograph")
    data = fictional_png(f"yizkr live test: {BOOKLET}, page 6, image 1")
    image = ArtifactRef.from_bytes(data)
    directory = tmp_path_factory.mktemp("derived")
    (directory / DERIVED_MEDIA_DIR).mkdir()
    file = f"{DERIVED_MEDIA_DIR}/{image.sha1}.png"
    (directory / file).write_bytes(data)
    record = DerivedMediaRecord(
        source_sha1=booklet.ref.sha1,
        container="pdf",
        title=BOOKLET,
        media={
            image.sha1: DerivedMedium(
                names=["page 6, image 1"], slides=[6], file=file
            )
        },
    )
    derived = Registrar(client).register_derived_media(record, directory)
    image_qid = derived[image.sha1]
    written = write_mapped(
        BatchWriter(client),
        fixture_batch(booklet.ref, image, portrait.ref, rahels_portrait.ref),
        placed={booklet.ref.sha1: set(derived)},
    )
    assert written.refused == []
    people = {
        local_id: written.created_items[local_id]
        for local_id in ("klara", "rahel", "felix")
    }
    wait_for_query_service(
        config,
        client,
        [
            booklet.qid,
            image_qid,
            portrait.qid,
            rahels_portrait.qid,
            *people.values(),
        ],
    )
    image_label = client.wbi.item.get(image_qid).labels.get("en").value

    out = tmp_path_factory.mktemp("site")
    try:
        publish_site(config, out)
        yield Site(
            out=out,
            booklet=booklet.qid,
            image=image_qid,
            image_label=image_label,
            portrait=portrait.qid,
            rahels_portrait=rahels_portrait.qid,
            people=people,
        )
    finally:
        # the site renders the whole instance, not only this fixture
        shutil.rmtree(out, ignore_errors=True)


def entries(site: Site, qid: str) -> dict[str, list[str]]:
    """The values of "What sources say about this artifact" on `qid`'s
    artifact page, by property label."""
    page = site.read(f"artifacts/{qid}.html")
    (section,) = re.findall(
        r"<h2>What sources say about this artifact</h2>.*?"
        r'<dl class="statements">(.*?)</dl>',
        page,
        flags=re.S,
    )
    return {
        prop: re.findall(r"<dd>(.*?)</dd>", values)
        for prop, values in re.findall(
            r"<dt>(.*?)</dt>((?:<dd>.*?</dd>)*)", section
        )
    }


def named_in(site: Site, qid: str) -> list[str]:
    """The entries of "Named in artifacts" on `qid`'s person page."""
    page = site.read(f"people/{qid}.html")
    (section,) = re.findall(
        r"<h2>Named in artifacts</h2>.*?<ul>(.*?)</ul>", page, flags=re.S
    )
    return re.findall(r"<li>(.*?)</li>", section)


ITSELF = '<span class="source">this artifact</span>'


def test_the_images_page_shows_the_caption_and_its_own_statements(site):
    booklet = (
        f'<a href="../artifacts/{site.booklet}.html">'
        f"{html.escape(BOOKLET)}</a>"
    )
    klara = site.people["klara"]
    assert entries(site, site.image) == {
        "caption": [
            f'<q lang="en">{html.escape(CAPTION)}</q>'
            f'<span class="source">{booklet}, page 6 (printed 4)</span>'
        ],
        # Klara has no death evidence: she is badged as possibly living
        "addressee": [
            f'<a href="../people/{klara}.html">{html.escape(KLARA)}</a>'
            ' <span class="badge">possibly living</span>' + ITSELF
        ],
        "inception": ["1939-07-28" + ITSELF],
        "location of creation": [html.escape(ADDRESS) + ITSELF],
    }


def test_the_portraits_page_lists_every_name_it_shows(site):
    shown = entries(site, site.portrait)
    assert shown["author"] == [html.escape(STUDIO) + ITSELF]
    assert sorted(shown["shows"]) == sorted(
        f'<a href="../people/{site.people[key]}.html">'
        f"{html.escape(label)}</a>"
        ' <span class="badge">possibly living</span>' + ITSELF
        for key, label in (("rahel", RAHEL), ("felix", FELIX))
    )


def test_a_date_its_artifact_gives_as_circa_shows_its_year_and_circa(site):
    # the query reads the statement's year precision, so the page shows
    # 1895, not a day; and its sourcing circumstance, circa, beside it
    # (ADR-0039 §2.2, §7; #601)
    assert entries(site, site.rahels_portrait) == {
        "inception": [
            '1895 <span class="qualifiers">(sourcing circumstances: '
            "circa)</span>" + ITSELF
        ]
    }


def test_the_caption_is_the_images_never_the_booklets(site):
    assert "What sources say" not in site.read(
        f"artifacts/{site.booklet}.html"
    )


def test_a_person_page_lists_the_artifacts_that_name_them(site):
    itself = '<span class="source">the artifact itself</span>'
    assert named_in(site, site.people["klara"]) == [
        f'addressee of <a href="../artifacts/{site.image}.html">'
        f"{html.escape(site.image_label)}</a>{itself}"
    ]
    for key in ("rahel", "felix"):
        assert named_in(site, site.people[key]) == [
            f'shown in <a href="../artifacts/{site.portrait}.html">'
            f"{html.escape(PORTRAIT)}</a>{itself}"
        ]
