"""Drawable fictional slides with reordered part names and separate notes."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from PIL import Image

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
CONTENT = "application/vnd.openxmlformats-officedocument.presentationml."
COLORS = ("00AA00", "CC0000", "0000CC")
NOTES = "SPEAKER NOTES ONLY"


def _rels(entries):
    return (
        f'<Relationships xmlns="{REL}">'
        + "".join(
            f'<Relationship Id="{key}" Type="{R}/{kind}" Target="{target}"/>'
            for key, kind, target in entries
        )
        + "</Relationships>"
    )


def _body(text, color, *, hidden=False, notes=False, background_fill=None):
    root = "notes" if notes else "sld"
    show = ' show="0"' if hidden else ""
    fill = (
        f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
        if color
        else "<a:noFill/>"
    )
    background = (
        f"<p:bg><p:bgPr>{background_fill}</p:bgPr></p:bg>"
        if background_fill is not None
        else ""
    )
    return f"""<p:{root} xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"{show}>
      <p:cSld>{background}<p:spTree>
        <p:nvGrpSpPr><p:cNvPr id="1" name=""/>
          <p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
        <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>
        </a:xfrm></p:grpSpPr>
        <p:sp><p:nvSpPr><p:cNvPr id="2" name="Fictional slide"/>
          <p:cNvSpPr/><p:nvPr/></p:nvSpPr>
          <p:spPr><a:xfrm><a:off x="0" y="0"/>
            <a:ext cx="9144000" cy="6858000"/></a:xfrm>
            <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
            {fill}
            <a:ln><a:noFill/></a:ln></p:spPr>
          <p:txBody><a:bodyPr anchor="ctr"/><a:lstStyle/>
            <a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="en-US" sz="3200">
              <a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>
              <a:latin typeface="Liberation Sans"/></a:rPr>
              <a:t>{escape(text)}</a:t></a:r></a:p>
          </p:txBody></p:sp>
      </p:spTree></p:cSld></p:{root}>"""


def presentation(*, hidden=True, notes_text=NOTES, background_fill=None):
    """Reordered slides: inherited image, hidden shape, visible shape."""
    order = (3, 1, 2)
    namespace = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'
    picture = io.BytesIO()
    Image.new("RGB", (40, 30), tuple(bytes.fromhex(COLORS[0]))).save(
        picture, "PNG"
    )
    parts = {
        "_rels/.rels": _rels(
            [("rId1", "officeDocument", "ppt/presentation.xml")]
        ),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" '
            'r:id="master"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/>'
            '<p:sldId id="257" r:id="rId2"/>'
            '<p:sldId id="258" r:id="rId3"/></p:sldIdLst>'
            '<p:sldSz cx="9144000" cy="6858000"/>'
            '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            [
                (f"rId{i}", "slide", f"slides/slide{n}.xml")
                for i, n in enumerate(order, 1)
            ]
            + [("master", "slideMaster", "slideMasters/slideMaster1.xml")]
        ),
        "ppt/notesSlides/notesSlide3.xml": _body(
            notes_text, "FFFFFF", notes=True
        ),
        "ppt/notesSlides/_rels/notesSlide3.xml.rels": _rels(
            [("rId1", "slide", "../slides/slide3.xml")]
        ),
        "ppt/slideLayouts/slideLayout1.xml": (
            f'<p:sldLayout {namespace} type="blank"><p:cSld>'
            "<p:spTree/></p:cSld></p:sldLayout>"
        ),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels(
            [("master", "slideMaster", "../slideMasters/slideMaster1.xml")]
        ),
        "ppt/slideMasters/slideMaster1.xml": f"""<p:sldMaster {namespace}>
          <p:cSld><p:bg><p:bgPr><a:blipFill>
            <a:blip r:embed="background"/><a:stretch><a:fillRect/></a:stretch>
          </a:blipFill></p:bgPr></p:bg><p:spTree/></p:cSld>
          <p:clrMap accent1="accent1" accent2="accent2" accent3="accent3"
            accent4="accent4" accent5="accent5" accent6="accent6"
            bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink"
            tx1="dk1" tx2="dk2"/>
          <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="layout"/>
          </p:sldLayoutIdLst></p:sldMaster>""",
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels(
            [
                ("background", "image", "../media/background.png"),
                ("layout", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ]
        ),
        "ppt/media/background.png": picture.getvalue(),
    }
    for i, (number, color) in enumerate(zip(order, COLORS, strict=True), 1):
        parts[f"ppt/slides/slide{number}.xml"] = _body(
            f"Fictional slide {i}",
            color if number != 3 else None,
            hidden=hidden and number == 1,
            background_fill=background_fill if number == 3 else None,
        )
        parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = _rels(
            [
                ("rId1", "notesSlide", "../notesSlides/notesSlide3.xml"),
                ("layout", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ]
            if number == 3
            else []
        )
    kinds = {
        "ppt/presentation.xml": "presentation.main+xml",
        "ppt/notesSlides/notesSlide3.xml": "notesSlide+xml",
        "ppt/slideLayouts/slideLayout1.xml": "slideLayout+xml",
        "ppt/slideMasters/slideMaster1.xml": "slideMaster+xml",
        **{f"ppt/slides/slide{n}.xml": "slide+xml" for n in order},
    }
    parts["[Content_Types].xml"] = (
        f'<Types xmlns="{CT}"><Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" '
        'ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        + "".join(
            f'<Override PartName="/{name}" ContentType="{CONTENT}{kind}"/>'
            for name, kind in kinds.items()
        )
        + "</Types>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, text in parts.items():
            package.writestr(
                zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), text
            )
    return output.getvalue()


def native_presentation(*, notes_text=NOTES):
    """Native vectors/text only; first-slide XML reverses visual columns."""
    with zipfile.ZipFile(
        io.BytesIO(presentation(notes_text=notes_text))
    ) as old:
        parts = {
            name: old.read(name)
            for name in old.namelist()
            if not name.startswith("ppt/media/")
        }
    master = "ppt/slideMasters/slideMaster1.xml"
    text = parts[master].decode()
    start = text.index("<p:bg>")
    end = text.index("</p:bg>", start) + len("</p:bg>")
    parts[master] = text[:start] + text[end:]
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels(
        [("layout", "slideLayout", "../slideLayouts/slideLayout1.xml")]
    )

    body = _body("Right panel", COLORS[0])
    start = body.index("<p:sp>")
    end = body.index("</p:sp>", start) + len("</p:sp>")
    shape = body[start:end]
    right = shape.replace('<a:off x="0"', '<a:off x="4572000"', 1)
    right = right.replace('cx="9144000"', 'cx="4572000"', 1)
    left = (
        right.replace("Right panel", "Left panel")
        .replace('<p:cNvPr id="2"', '<p:cNvPr id="3"', 1)
        .replace('<a:off x="4572000"', '<a:off x="0"', 1)
    )
    parts["ppt/slides/slide3.xml"] = body[:start] + right + left + body[end:]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, value in parts.items():
            package.writestr(
                zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), value
            )
    return output.getvalue()
