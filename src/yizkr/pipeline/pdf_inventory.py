"""PDF parts and paint occurrences, before choosing a reading route.

The strict structural parser names original streams and content operators;
PDFium supplies checked image representations. Neither a page rendering nor
its reading route supplies a source identity. The preparation result is an
in-memory admission plan; its durable record is the common ContainerInventory.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    IndirectObject,
    StreamObject,
)

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    Eligibility,
    Placement,
    ReadingOutcome,
    SourcePart,
    SourceRepresentation,
    StructuralLocator,
    structural_id,
)

_PARSE_ERRORS = (
    PyPdfError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    RecursionError,
)
_INLINE_NAMES = {
    "/W": "/Width",
    "/H": "/Height",
    "/BPC": "/BitsPerComponent",
    "/CS": "/ColorSpace",
    "/F": "/Filter",
    "/DP": "/DecodeParms",
    "/D": "/Decode",
    "/IM": "/ImageMask",
}
# Graphics state, color and path construction/clipping do not paint another
# source. Text operators are checked separately for a valid invisible OCR
# object. Path painting, shading and unknown operators are deliberately
# absent: PDFium's object list alone cannot prove they were absent.
_IMAGE_ONLY_OPERATORS = frozenset(
    b"q Q cm w J j M d ri i gs "
    b"CS cs SC SCN sc scn G g RG rg K k "
    b"m l c v y h re W W* n Do "
    b"BT ET Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' \"".split()
) | {b"INLINE IMAGE"}
_SHOW_TEXT_OPERATORS = frozenset((b"Tj", b"TJ", b"'", b'"'))
_TEXT_OBJECT_OPERATORS = frozenset(
    b"Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' \"".split()
)


def _value(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _native_id(value: Any) -> str | None:
    ref = (
        value
        if isinstance(value, IndirectObject)
        else getattr(value, "indirect_reference", None)
    )
    return f"{ref.idnum} {ref.generation} R" if ref is not None else None


def _metadata(value: Any, depth: int = 0) -> Any:
    """Native context, retaining references without expanding their objects."""
    if isinstance(value, IndirectObject):
        return {"reference": _native_id(value)}
    if depth >= 16:
        return {"unexpanded": "metadata nesting limit"}
    if isinstance(value, dict):
        return {
            str(key): _metadata(item, depth + 1)
            for key, item in value.items()
            if str(key) != "/Length"
        }
    if isinstance(value, (list, tuple)):
        return [_metadata(item, depth + 1) for item in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


@dataclass
class PdfInventory:
    """A common inventory and the bytes/admission facts prepared from it.

    ``image_parts`` uses unfiltered PDFium image ordinals solely to adapt the
    existing reader. Durable identities instead name native objects and full
    content-operator occurrence paths, including each enclosing Form paint.
    """

    inventory: ContainerInventory
    data: dict[str, bytes] = field(default_factory=dict)
    page_count: int = 0
    page_parts: dict[int, SourcePart] = field(default_factory=dict)
    image_parts: dict[tuple[int, int], SourcePart] = field(
        default_factory=dict
    )
    image_representations: dict[tuple[int, int], SourceRepresentation] = field(
        default_factory=dict
    )
    _reasons: dict[int, list[str]] = field(default_factory=dict)
    _fatal: str | None = None

    def replace_inventory(self, inventory: ContainerInventory) -> None:
        """Adopt prepared bindings without leaving stale ephemeral indexes."""
        if inventory.source != self.inventory.source:
            raise ValueError("replacement inventory names another PDF")
        parts = {part.id: part for part in inventory.parts}
        self.page_parts = {
            page: parts[part.id] for page, part in self.page_parts.items()
        }
        self.image_parts = {
            key: parts[part.id] for key, part in self.image_parts.items()
        }
        self.image_representations = {
            key: next(
                rep
                for rep in self.image_parts[key].representations
                if rep.byte_object == previous.byte_object
                and rep.method == previous.method
            )
            for key, previous in self.image_representations.items()
        }
        self.inventory = inventory

    def verify(self, source_bytes: bytes) -> None:
        from yizkr.pipeline.textlayer import PdfExclusionError

        if self.inventory.source != ArtifactRef.from_bytes(source_bytes):
            raise PdfExclusionError(
                "PDF inventory belongs to different source bytes"
            )

    def apply_exclusions(self, excluded: Callable[[str], bool]) -> None:
        """A supplied plan cannot bypass a caller's more restrictive policy.

        An already excluded plan remains conservative if the policy relaxes;
        rebuilding is required to prepare its omitted bytes.
        """
        for part in self.inventory.parts:
            if part.kind != "embedded_resource":
                continue
            if any(
                excluded(item.byte_object.sha1)
                for item in (part, *part.representations)
                if item.byte_object is not None
            ) or (
                part.owner_binding is not None
                and excluded(part.owner_binding.artifact.sha1)
            ):
                part.eligibility = Eligibility(
                    state="excluded",
                    reason="original stream or prepared image is excluded",
                )
                part.file = None
                if part.owner_binding is not None:
                    part.owner_binding.file = None
                for representation in part.representations:
                    representation.file = None
                    representation.eligibility = part.eligibility.model_copy()
        affected = {
            page
            for (page, _ordinal), part in self.image_parts.items()
            if part.eligibility.state == "excluded"
        }
        for part in self.inventory.parts:
            if (
                part.kind == "container_source"
                and affected
                or (
                    part.kind == "page_body"
                    and part.locator.position in affected
                )
            ):
                for representation in part.representations:
                    representation.file = None
                    representation.eligibility = Eligibility(
                        state="excluded",
                        reason="representation includes an excluded image",
                    )
        wanted = {
            item.byte_object.sha1
            for part in self.inventory.parts
            for item in (part, *part.representations)
            if item.file is not None
        }
        wanted.update(
            part.owner_binding.artifact.sha1
            for part in self.inventory.parts
            if part.owner_binding is not None
            and part.owner_binding.file is not None
        )
        for digest in set(self.data) - wanted:
            del self.data[digest]

    def guard(self, pages: set[int] | None = None) -> None:
        """Admit checked page representations only."""
        from yizkr.pipeline.textlayer import PdfExclusionError

        if self._fatal is not None:
            raise PdfExclusionError(self._fatal)
        selected = (
            set(range(1, self.page_count + 1)) if pages is None else pages
        )
        if any(
            type(n) is not int or not 1 <= n <= self.page_count
            for n in selected
        ):
            raise PdfExclusionError(
                "PDF inventory has no requested physical page"
            )
        by_page: dict[int, list[SourcePart]] = {}
        for (page, _ordinal), part in self.image_parts.items():
            by_page.setdefault(page, []).append(part)
        for number in sorted(selected):
            reasons = self._reasons.get(number)
            if reasons:
                raise PdfExclusionError(reasons[0])
            for part in by_page.get(number, []):
                if part.eligibility.state == "excluded":
                    raise PdfExclusionError(
                        f"PDF page {number}: excluded embedded image; "
                        "vision is refused"
                    )
                if part.eligibility.state != "allowed":
                    raise PdfExclusionError(
                        f"PDF page {number}: image bytes cannot be checked "
                        "for exclusions"
                    )

    def representation(
        self, part: SourcePart, data: bytes, suffix: str, method: str
    ) -> SourceRepresentation:
        """Record prepared bytes with lineage to their source part."""
        reference = ArtifactRef.from_bytes(data)
        for representation in part.representations:
            if (
                representation.byte_object == reference
                and representation.method == method
            ):
                return representation
        media_type = {
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".pdf": "application/pdf",
        }[suffix]
        result = SourceRepresentation(
            byte_object=reference,
            file=f"representations/{reference.sha1}{suffix}",
            media_type=media_type,
            method=method,
            eligibility=Eligibility(state="allowed"),
        )
        self.data[reference.sha1] = data
        part.representations.append(result)
        return result


class _Builder:
    def __init__(self, source_bytes: bytes, excluded: Callable[[str], bool]):
        self.source_bytes = source_bytes
        self.excluded = excluded
        source = ArtifactRef.from_bytes(source_bytes)
        self.plan = PdfInventory(
            ContainerInventory(source=source, container="pdf")
        )
        self.source = self.part(
            "container_source", StructuralLocator(kind="container")
        )
        self.source.byte_object = source
        self.source.media_type = "application/pdf"
        self.source.eligibility = Eligibility(state="allowed")
        self.resources: dict[str, SourcePart] = {}
        self.originals: dict[str, bytes] = {}
        self.paints: dict[int, list[tuple[SourcePart, Placement]]] = {}
        self.non_image_content: set[int] = set()

    def part(
        self, kind: str, locator: StructuralLocator, **kwargs
    ) -> SourcePart:
        part = SourcePart(
            id=structural_id(self.plan.inventory.source, locator),
            kind=kind,
            locator=locator,
            **kwargs,
        )
        self.plan.inventory.parts.append(part)
        return part

    def problem(
        self, number: int, reason: str, *, path: str = "resources/opaque"
    ):
        reason = f"PDF page {number}: {reason}"
        self.plan._reasons.setdefault(number, []).append(reason)
        self.plan.inventory.complete = False
        locator = StructuralLocator(
            kind="opaque",
            position=number,
            path=path if path.startswith("/") else f"/pages/{number}/{path}",
        )
        ident = structural_id(self.plan.inventory.source, locator)
        if not any(part.id == ident for part in self.plan.inventory.parts):
            self.part(
                "opaque_resource",
                locator,
                reading=ReadingOutcome(state="unsupported", reason=reason),
                limitations=[reason],
            )

    def fail(self, reason: str):
        self.plan._fatal = reason
        self.plan.inventory.complete = False
        self.plan.inventory.limitations.append(reason)
        self.source.reading = ReadingOutcome(state="failed", reason=reason)

    def image(
        self, value: Any, path: str, *, inline: bytes | None = None
    ) -> SourcePart:
        native = _native_id(value)
        key = native or path
        if key in self.resources:
            return self.resources[key]
        obj = _value(value)
        dictionary = _metadata(obj)
        if inline is not None:
            dictionary = {
                _INLINE_NAMES.get(k, k): v for k, v in dictionary.items()
            }
        data = inline if inline is not None else bytes(obj._data)
        reference = ArtifactRef.from_bytes(data)
        part = self.part(
            "embedded_resource",
            StructuralLocator(
                kind="image",
                native_id=native,
                path=f"/objects/{native}" if native else path,
            ),
            byte_object=reference,
            media_type="application/vnd.pdf.image-stream",
            source_role="independent",
            layout=[{"pdf_dictionary": dictionary}],
            eligibility=Eligibility(
                state="excluded"
                if self.excluded(reference.sha1)
                else "undetermined",
                reason="original image stream is excluded"
                if self.excluded(reference.sha1)
                else "image representation has not been prepared",
            ),
        )
        self.resources[key] = part
        self.originals[part.id] = data
        return part

    def resources_in(self, resources: Any, path: str, active: set[int]):
        """Record unpainted images without inventing paint occurrences."""
        from yizkr.pipeline.textlayer import _MAX_FORM_DEPTH, _pdf_dictionary

        resources = _pdf_dictionary(resources)
        if id(resources) in active or len(active) >= _MAX_FORM_DEPTH:
            return  # the structural admission check records the opaque branch
        active.add(id(resources))
        try:
            for name, value in _pdf_dictionary(
                resources.get("/XObject")
            ).items():
                obj = _pdf_dictionary(value)
                kind = _value(obj.get("/Subtype"))
                location = f"{path}/XObject/{name[1:]}"
                if kind == "/Image":
                    self.image(value, location)
                elif kind == "/Form":
                    self.resources_in(
                        obj.get("/Resources", resources), location, active
                    )
        finally:
            active.remove(id(resources))

    def placement(
        self, number: int, part: SourcePart, path: str, layout: list[dict]
    ) -> Placement:
        locator = StructuralLocator(kind="paint", position=number, path=path)
        paints = self.paints.setdefault(number, [])
        placement = Placement(
            id=structural_id(self.plan.inventory.source, locator),
            parent=self.plan.page_parts[number].id,
            child=part.id,
            locator=locator,
            order=len(paints) + 1,
            occurrence="paint",
            layout=layout,
            limitations=[
                "Native graphics operators are uncomposed context; "
                "bounds do not establish clipping or visibility."
            ],
        )
        self.plan.inventory.placements.append(placement)
        paints.append((part, placement))
        return placement

    def content(
        self,
        value: Any,
        resources: Any,
        reader: PdfReader,
        number: int,
        path: str,
        active: set[int],
        inherited: tuple[dict, ...] = (),
    ):
        from yizkr.pipeline.textlayer import _MAX_FORM_DEPTH, _pdf_dictionary

        obj = _value(value)
        if obj is None:
            return
        if id(obj) in active or len(active) >= _MAX_FORM_DEPTH:
            self.problem(number, "nested form inspection limit", path=path)
            return
        if isinstance(obj, ArrayObject):
            # ContentStream preserves operator order across the array. The
            # original array and stream refs remain in the page dictionary.
            stream = ContentStream(obj, reader)
        elif isinstance(obj, StreamObject):
            stream = ContentStream(obj, reader)
        else:
            raise ValueError("PDF content is not a stream")
        resources = _pdf_dictionary(resources)
        active.add(id(obj))
        state: list[dict] = []
        stack: list[list[dict]] = []
        text_mode = 0
        text_modes: list[int] = []
        in_text = False
        try:
            for index, (operands, operator) in enumerate(
                stream.operations, start=1
            ):
                location = f"{path}/op[{index}]"
                # A conservative image-only proof, not a second graphics
                # interpreter. Unknown operators must not disappear merely
                # because PDFium did not expose them as page objects.
                if operator not in _IMAGE_ONLY_OPERATORS:
                    self.non_image_content.add(number)
                if operator in _TEXT_OBJECT_OPERATORS and not in_text:
                    self.non_image_content.add(number)
                if operator == b"BT":
                    if in_text:
                        self.non_image_content.add(number)
                    in_text = True
                elif operator == b"ET":
                    if not in_text:
                        self.non_image_content.add(number)
                    in_text = False
                elif operator == b"Tr":
                    if len(operands) != 1 or operands[0] not in range(8):
                        self.non_image_content.add(number)
                        text_mode = -1
                    else:
                        text_mode = int(operands[0])
                elif operator in _SHOW_TEXT_OPERATORS:
                    if not in_text or text_mode != 3:
                        self.non_image_content.add(number)
                if operator == b"q":
                    stack.append(list(state))
                    text_modes.append(text_mode)
                elif operator == b"Q":
                    state = stack.pop() if stack else []
                    if not text_modes:
                        self.non_image_content.add(number)
                    text_mode = text_modes.pop() if text_modes else 0
                elif operator in (
                    b"cm",
                    b"W",
                    b"W*",
                    b"re",
                    b"m",
                    b"l",
                    b"c",
                    b"v",
                    b"y",
                    b"h",
                ):
                    state.append(
                        {
                            "operator": operator.decode(),
                            "operands": _metadata(operands),
                        }
                    )
                elif operator == b"INLINE IMAGE":
                    part = self.image(
                        operands["settings"], location, inline=operands["data"]
                    )
                    self.placement(
                        number,
                        part,
                        location,
                        [*inherited, {"native_graphics": list(state)}],
                    )
                elif operator == b"Do":
                    name = operands[0]
                    xobjects = _pdf_dictionary(resources.get("/XObject"))
                    if name not in xobjects:
                        self.problem(
                            number,
                            "paint names a missing XObject",
                            path=location,
                        )
                        continue
                    resource = xobjects.raw_get(name)
                    child = _pdf_dictionary(resource)
                    kind = _value(child.get("/Subtype"))
                    if kind == "/Image":
                        part = self.image(
                            resource, f"{path}/XObject/{name[1:]}"
                        )
                        self.placement(
                            number,
                            part,
                            location,
                            [*inherited, {"native_graphics": list(state)}],
                        )
                    elif kind == "/Form":
                        self.content(
                            child,
                            child.get("/Resources", resources),
                            reader,
                            number,
                            f"{location}/form[{_native_id(resource) or name}]",
                            active,
                            (
                                *inherited,
                                {
                                    "native_graphics": list(state),
                                    "form_matrix": _metadata(
                                        child.get("/Matrix")
                                    ),
                                },
                            ),
                        )
            if in_text or text_modes:
                self.non_image_content.add(number)
        finally:
            active.remove(id(obj))

    def annotations(self, page: Any, number: int) -> bool:
        from yizkr.pipeline.textlayer import _pdf_dictionary

        opaque = False
        for index, value in enumerate(_value(page.get("/Annots", [])), 1):
            annotation = _pdf_dictionary(value)
            unsupported = (
                "/AP" in annotation
                or _value(annotation.get("/Subtype")) == "/Widget"
            )
            reason = (
                "PDF annotation appearance cannot be checked for "
                "image exclusions"
                if unsupported
                else None
            )
            path = f"/pages/{number}/Annots[{index}]"
            part = self.part(
                "annotation",
                StructuralLocator(
                    kind="annotation",
                    position=number,
                    path=path,
                    native_id=_native_id(value),
                ),
                text=str(_value(annotation["/Contents"]))
                if "/Contents" in annotation
                else None,
                layout=[{"pdf_dictionary": _metadata(annotation)}],
                eligibility=Eligibility(
                    state="undetermined" if unsupported else "allowed",
                    reason=reason,
                ),
                reading=ReadingOutcome(
                    state="unsupported" if unsupported else "unattempted",
                    reason=reason or "annotation not read independently",
                ),
                limitations=[reason] if reason else [],
            )
            locator = StructuralLocator(
                kind="annotation-placement", position=number, path=path
            )
            self.plan.inventory.placements.append(
                Placement(
                    id=structural_id(self.plan.inventory.source, locator),
                    parent=self.plan.page_parts[number].id,
                    child=part.id,
                    locator=locator,
                    order=index,
                    occurrence="annotation",
                    layout=[{"pdf_rect": _metadata(annotation.get("/Rect"))}],
                )
            )
            opaque = opaque or unsupported
        return opaque

    def structure(self, reader: PdfReader, document: Any):
        from yizkr.pipeline.textlayer import (
            PdfExclusionError,
            _check_resources,
            _pdf_dictionary,
        )

        if (
            reader.is_encrypted
            or len(reader.pages) != len(document)
            or not len(document)
        ):
            raise ValueError(
                "PDF page structure cannot be inspected consistently"
            )
        self.plan.page_count = len(document)
        form = _pdf_dictionary(reader.root_object.get("/AcroForm"))
        dynamic = "/XFA" in form
        for number, page in enumerate(reader.pages, start=1):
            part = self.part(
                "page_body",
                StructuralLocator(
                    kind="page",
                    position=number,
                    path=f"/pages/{number}",
                    native_id=_native_id(page),
                ),
                byte_object=self.plan.inventory.source,
                media_type="application/pdf",
                layout=[{"pdf_dictionary": _metadata(page)}],
                eligibility=Eligibility(state="allowed"),
            )
            self.plan.page_parts[number] = part
            resources = page.get("/Resources")
            try:
                opaque_annotations = self.annotations(page, number)
                if dynamic:
                    raise PdfExclusionError(
                        "PDF dynamic forms cannot be checked for "
                        "image exclusions"
                    )
                _check_resources(resources, set())
                if opaque_annotations:
                    raise PdfExclusionError(
                        "PDF annotation appearance cannot be checked "
                        "for image exclusions"
                    )
            except _PARSE_ERRORS as error:
                self.problem(number, str(error))
            try:
                self.resources_in(
                    resources, f"/pages/{number}/Resources", set()
                )
                self.content(
                    page.get("/Contents"),
                    resources,
                    reader,
                    number,
                    f"/pages/{number}/Contents",
                    set(),
                )
                for placement in self.plan.inventory.placements:
                    if (
                        placement.locator.kind == "annotation-placement"
                        and placement.locator.position == number
                    ):
                        placement.order += len(self.paints.get(number, []))
            except _PARSE_ERRORS as error:
                self.problem(
                    number,
                    f"PDF content structure cannot be inspected: {error}",
                    path="content/opaque",
                )

    def prepare(self, document: Any):
        from yizkr.pipeline.textlayer import (
            _MAX_FORM_DEPTH,
            _clip,
            image_bytes,
        )

        for number in range(1, self.plan.page_count + 1):
            page = document[number - 1]
            try:
                objects = list(page.get_objects(max_depth=_MAX_FORM_DEPTH))
                if any(
                    o.type == pdfium_c.FPDF_PAGEOBJ_FORM
                    and o.level >= _MAX_FORM_DEPTH - 1
                    for o in objects
                ):
                    self.problem(number, "nested form inspection limit")
                images = [
                    obj
                    for obj in objects
                    if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
                ]
                paints = self.paints.get(number, [])
                if len(images) != len(paints):
                    self.problem(
                        number,
                        "PDF image paints cannot be matched to native "
                        "source parts",
                        path="content/image-mapping",
                    )
                    for part, _placement in paints:
                        part.reading = ReadingOutcome(
                            state="unsupported",
                            method="pdfium-image",
                            reason="native and PDFium image paints disagree",
                        )
                    continue
                # A page showing only one original image is a view of that
                # image's source, whatever its size or page coverage. Other
                # painting, annotations and unknown native content prevent
                # that attribution; rendering still has its own rules.
                image_only_page = (
                    len(images) == len(paints) == 1
                    and all(
                        obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
                        or (
                            obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT
                            and pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw)
                            == 3
                        )
                        for obj in objects
                    )
                    and images[0].container is None
                    and _clip(images[0].get_bounds(), page.get_cropbox())
                    is not None
                    and number not in self.non_image_content
                    and number not in self.plan._reasons
                    and not any(
                        p.parent == self.plan.page_parts[number].id
                        and p.occurrence == "annotation"
                        for p in self.plan.inventory.placements
                    )
                )
                for ordinal, (obj, (part, placement)) in enumerate(
                    zip(images, paints, strict=True), start=1
                ):
                    if (
                        bytes(obj.get_data(decode_simple=False))
                        != self.originals[part.id]
                    ):
                        self.problem(
                            number,
                            "PDF image bytes disagree with native "
                            "paint source",
                            path="content/image-mapping",
                        )
                        part.reading = ReadingOutcome(
                            state="unsupported",
                            method="pdfium-image",
                            reason="native and PDFium image bytes disagree",
                        )
                        continue
                    self.plan.image_parts[number, ordinal] = part
                    if image_only_page:
                        self.plan.page_parts[number].layout.append(
                            {
                                "image_only_source_part": part.id,
                                "image_only_placement": placement.id,
                            }
                        )
                    placement.layout.append(
                        {
                            "pdfium_bounds": list(obj.get_bounds()),
                            "pdfium_matrix": list(obj.get_matrix().get()),
                            "coordinate_frame": "parent",
                            "source_role": "independent",
                        }
                    )
                    try:
                        data, suffix = image_bytes(obj)
                        if not data:
                            raise ValueError("image has no inspectable bytes")
                    except (pdfium.PdfiumError, ValueError) as error:
                        part.reading = ReadingOutcome(
                            state="unsupported",
                            method="pdfium-image",
                            reason=f"PDFium image preparation failed: {error}",
                        )
                        self.plan._reasons.setdefault(number, []).append(
                            f"PDF page {number}: image bytes cannot be "
                            "checked for exclusions"
                        )
                        continue
                    representation = self.plan.representation(
                        part, data, suffix, "pdfium-image"
                    )
                    self.plan.image_representations[number, ordinal] = (
                        representation
                    )
                    if placement.id not in representation.placement_ids:
                        representation.placement_ids.append(placement.id)
                    if part.eligibility.state == "excluded" or self.excluded(
                        representation.byte_object.sha1
                    ):
                        part.eligibility = Eligibility(
                            state="excluded",
                            reason="original stream or prepared image "
                            "is excluded",
                        )
                    else:
                        part.eligibility = Eligibility(state="allowed")
            finally:
                page.close()
        # A shared image may have appeared more than once. Decide publication
        # after every occurrence, so one excluded representation suppresses all
        # original/derived bytes of that source part.
        mapped = {part.id for part in self.plan.image_parts.values()}
        for part in self.resources.values():
            original = self.originals[part.id]
            if part.id not in mapped:
                part.limitations.append(
                    "No ordinary page-image paint was mapped to this "
                    "resource; no reading representation was prepared."
                )
            if part.eligibility.state == "excluded":
                for representation in part.representations:
                    representation.file = None
                    representation.eligibility = part.eligibility.model_copy()
            else:
                part.file = (
                    f"container-parts/{part.byte_object.sha1}.pdf-stream"
                )
                self.plan.data[part.byte_object.sha1] = original
        wanted = {
            product.byte_object.sha1
            for part in self.plan.inventory.parts
            for product in (part, *part.representations)
            if product.file is not None
        }
        self.plan.data = {
            digest: data
            for digest, data in self.plan.data.items()
            if digest in wanted
        }

    def build(self, document: Any) -> PdfInventory:
        try:
            reader = PdfReader(io.BytesIO(self.source_bytes), strict=True)
            try:
                self.structure(reader, document)
                self.prepare(document)
            finally:
                reader.close()
        except (*_PARSE_ERRORS, pdfium.PdfiumError) as error:
            self.fail(
                "PDF cannot be inspected for embedded-image exclusions: "
                f"{error}"
            )
        return self.plan


def inventory_pdf(
    source_bytes: bytes,
    excluded: Callable[[str], bool],
    *,
    document: Any | None = None,
) -> PdfInventory:
    """Inventory an immutable PDF snapshot, including failures and exclusions.

    With an open ``document``, the caller holds the PDFium lock and has opened
    precisely ``source_bytes``. Otherwise this function owns that lifetime.
    No files or model calls are made.
    """
    from yizkr.pipeline import textlayer

    builder = _Builder(source_bytes, excluded)
    if document is not None:
        return builder.build(document)
    with textlayer._LOCK:
        textlayer.fresh_start()
        try:
            document = pdfium.PdfDocument(source_bytes)
            try:
                document.init_forms()
                return builder.build(document)
            finally:
                document.close()
        except (pdfium.PdfiumError, ValueError) as error:
            builder.fail(
                "PDF cannot be inspected for embedded-image exclusions: "
                f"{error}"
            )
            return builder.plan
