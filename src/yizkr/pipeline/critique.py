"""Extraction critic (ADR-0013 mechanism C).

One adversarial LLM turn that reviews an extracted batch for the
confident-but-wrong assignments the first pass will not self-report — an
agent typed as a place, a role/field label captured as a name, an entity
dumped into the escape hatch that plainly fits a real kind. It FLAGS,
never rewrites: findings are surfaced to the human curator at the
existing review gate. It is the reconciliation adjudicator's pattern
applied to extraction, and it is what catches the "MOTHER as a name" and
"hat shop as a place" errors without a human eyeballing raw output.
It reads the batch beside the document's transcription, each line
numbered, and every citation (a claim's excerpt, a container's evidence)
where it stands, and judges each by one support test (ADR-0003, ADR-0023).

Each finding is a record that names what it concerns by id: the entity
(a manifest id), and optionally one claim (its subject id, property key
and value as the prompt lists them), one alias and one container of a
place's mention record (by its name, which locates the entry's evidence
and the transcription line it was compared with in the extraction's
local `containers.json`); the statement-part it
doubts (`axis`, ADR-0023); a `category` from CATEGORIES; the issue; and a
suggestion whose kind or property, when given, is a manifest key. Code
checks every record against the batch and the model manifest. A record
that fails is kept verbatim with the reasons it failed (`Invalid`),
beside the valid ones, and never repaired: nothing is read from a
finding's wording.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    PersonStructure,
    PlaceStructure,
    QuantityValue,
    load_vocabulary,
)
from yizkr.core.verbatim import Transcript, words
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)
from yizkr.pipeline.review import describe_value
from yizkr.pipeline.transcribe import DECK_ENGINE

logger = logging.getLogger(__name__)

# the universal parts of a statement a finding doubts (ADR-0023)
AXES = ("subject", "predicate", "value", "existence")

# The patterns the critic flags, by key, each with the line the prompt
# gives it. A finding names exactly one of these keys.
CATEGORIES: dict[str, str] = {
    "wrong_kind": (
        "an entity whose kind is wrong: a place typed as `human`, a person "
        "typed as a place, an agent (a company, bank, committee or agency) "
        "typed as `place` or `human`"
    ),
    "role_label_alias": (
        "a ROLE or FORM-FIELD label recorded as an alias, not a name: "
        "MOTHER, FATHER, CHILD, INFORMANT, BRIDE, GROOM, WITNESS"
    ),
    "kinship_term_alias": (
        "a kinship term or other appellation recorded as the label of a "
        'person the document names ("Tante Rahel" as the label of Rahel; '
        '"Omama" as the label of a grandmother the document names), or a '
        'description recorded as an alias ("my husband", "our great '
        'grandmother"): the label takes the name, and an appellation is an '
        "alias and a `referred_to_as` claim (a literal kinship term also "
        "`relative` + `kinship_type`; a spousal term gives a marriage) "
        "(ADR-0018). A kinship term, a pet name or a courtesy name among "
        'the aliases ("Tante Rahel", "Omama", "Pünktchen", "Nana Maisie") '
        "is right and no finding. A person the document never names is "
        "rightly labelled with the document's own words for them (the "
        'postcard\'s writer, who signs only "Dein Vater"; a status other '
        "than `name`): that label is no finding"
    ),
    "deictic_alias": (
        'a deictic or generic reference recorded as an alias ("the '
        'deceased", "the above-named", "the child", "the firm")'
    ),
    "escape_hatch_misused": (
        "an entity marked `unclassified_entity` that clearly fits a real "
        "kind (the escape hatch was used when it should not have been): an "
        "agency, office or firm's department is an `organization`; a "
        "cemetery or a building a `place`; a named ship a `vessel`. A "
        "named aircraft rightly takes it, as does a letter or a "
        "certificate a source mentions, since an artifact kind is no kind "
        "of entity"
    ),
    "misapplied_property": "a claim whose property is plainly misapplied",
    "office_holder_as_organization": (
        "a person who holds an office (a registrar, a mayor, a rabbi) typed "
        "as `organization`"
    ),
    "religion_as_occupation": (
        "a religion or denomination recorded as an `occupation`: suggest "
        "`religion`"
    ),
    "relation_kind_mismatch": (
        "a claim whose relation takes a kind its value is not (ADR-0031: "
        "the relation decides the kind): a relation of place (place of "
        "birth, death or burial, residence, location, citizenship) takes a "
        "place; a "
        "relation of agency (issued by, carried out by, employer, educated "
        "at, member of) takes an organization; a relation of authorship or "
        "address (author, addressee: who wrote or made a document, and "
        "whom it was addressed to, ADR-0039) takes a person, an "
        "organization or a family, so an author or an addressee that is a "
        "place is a finding"
    ),
    "implausible_value": (
        "a value the source is unlikely to say as read, such as a birth "
        "year a century before the parents married"
    ),
    "unsupported_containment": (
        "a place's `part_of` claim (its container, or its country) whose "
        "citation fails the support test, its times included, or that "
        "cites no excerpt, since then no passage conveys it: containment "
        "the source does not convey is never written (ADR-0038); name the "
        "claim"
    ),
    "evidence_does_not_state": (
        "a place's `within` entry whose evidence fails the support test: "
        "name the entry by its container as listed (`container`)"
    ),
    "excerpt_does_not_state": (
        "any other claim whose excerpt fails the support test, on its value "
        "or on its times, or, for a claim about an artifact, on which "
        "artifact it concerns: name the claim. Such a claim that cites no "
        "excerpt is not judged"
    ),
}


# the categories whose finding names the claim it concerns
CLAIM_CATEGORIES = ("unsupported_containment", "excerpt_does_not_state")


class ClaimRef(BaseModel):
    """One claim, as the prompt lists it: subject id, property key, and
    value."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    property: str
    value: str


class Suggestion(BaseModel):
    """What the critic suggests instead: a kind and a property, each a
    manifest key when given, and a one-line note ("drop" among them)."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    property: str | None = None
    note: str = ""


class Finding(BaseModel):
    """A valid finding: every id it names is in the batch, every key in
    the model manifest."""

    model_config = ConfigDict(extra="forbid")

    entity: str
    claim: ClaimRef | None = None
    alias: str | None = None
    # a container of the entity's place record, by its name as listed
    container: str | None = None
    axis: str
    category: str
    issue: str
    suggestion: Suggestion = Suggestion()


class Invalid(BaseModel):
    """A finding that failed its checks: the object the critic gave,
    verbatim, and why it failed."""

    model_config = ConfigDict(extra="forbid")

    invalid: dict[str, Any]
    why: list[str]


# the fields that make an object in a critique answer a finding: one with
# none of them (a claim or suggestion cut loose from a broken line) is not
FINDING_FIELDS = frozenset(Finding.model_fields)


def entry(stored: Mapping[str, Any]) -> Finding | Invalid:
    """An entry of a critique view: a finding, or one that failed its
    checks. Raises ValidationError for neither."""
    if "invalid" in stored:
        return Invalid.model_validate(stored)
    return Finding.model_validate(stored)


CRITIQUE_PROMPT = """\
You review a genealogical extraction for likely mistakes before a human
curator sees it. You FLAG concerns only; you never rewrite.

Flag a genuine concern only, of one of these categories:
{categories_block}

An appellation (a kinship term, alone or with a name, a pet name or a
courtesy name) among a person's aliases, or recorded as a
`referred_to_as` claim, and a kinship term as `relative` +
`kinship_type`, are right — do not flag them.

The support test. Every citation is a passage of the source: a claim's
excerpt, or the evidence of a container in a place's mention record,
listed below with the lines (and the page) where it stands in the
numbered transcription. Read where it stands in the source, does the
passage convey the fact the claim or container states, its value and any
time it carries? For a claim about an artifact (this document, an image
it places, or a document it mentions), the fact includes which artifact:
the passage must convey it of that one. The passage itself must carry
what the statement adds, such as the container's name or the value; its
context says how to read it: which document or form, which field, whose
entry. So a form's field line conveys the containment of the place the
form gives it for, by its place in the form. It passes: on the 1941
death certificate's line "Место смерти: город, селение д. Кошновка;
район Калдовский; область, край Чадская; республика РСФСР.", the
evidence "район Калдовский" stands in the district slot of a place of
death whose village is Koshnovka, and conveys that Koshnovka is in
Kaldovsky District; so does "District: Kaldovsky District" in the
district field of a form, one field a line, whose village field is
Koshnovka. It fails: a passage that only names both places (a list of
towns); a bare "Fläckfeld" beside "Flörburg, Kreis Peiltal", where
"Kreis Peiltal" is about Flörburg, so Fläckfeld put in Kreis Peiltal on
an excerpt that reads only "Fläckfeld" fails. A citation passes when any
one of its occurrences conveys the fact; for a row that names a page,
the occurrence must be on that page. You may read any occurrence in the
numbered transcription, not only those listed. A citation not in the
transcription is judged on its words alone.

When a citation fails, the suggestion's note names, by its lines,
another passage of the source that conveys the fact ("re-cite line 3"),
and the claim or container stays; it says "drop" only when the source
conveys the fact nowhere, and "drop the qualifier" when only a time
fails. In the 1909 burial register, Eugen's place of death Flörburg cited
by the heading "Friedhof Flörburg, Beerdigungsbuch 1909." (line 1) is
conveyed by line 3, "Gestorben: 16. März 1909 in Flörburg.": re-cite
line 3. Fläckfeld put in Kreis Peiltal on an excerpt that reads only
"Fläckfeld": drop.

Each finding doubts one part of a statement (its axis):
- subject: which entity it is, or its kind;
- predicate: the property a claim uses;
- value: a claim's value, or an alias;
- existence: whether the entity or claim belongs in the extraction at all.

Kinds (manifest item keys): {kinds}

Properties (manifest property keys): {properties}

The source's transcription, each line numbered:
{transcription_block}

Entities (id | kind | label | aliases); a person's kind is followed by the
status of its mention record: `name` when the document names the person,
else what its label is instead (role, kinship, courtesy, placeholder or
description):
{entities_block}

Claims (subject | property | value | times | citation): a subject is an
entity id, "this document" for the source itself, or an image the
document places by its place ("page 6, image 1"); the times a claim
carries ("-" for none), and its citation, the excerpt it cites with where
it occurs: a row that names a page lists only the occurrences on that
page, and otherwise every occurrence is listed, nearest the subject's
mentions first; at most three are shown ("and N more" for the rest);
"not on page N" for an excerpt found only on other pages; "not in the
transcription" for one found nowhere; "clipped" for one cut short
(ending in "…"), located by its words before the "…"; "-" for none:
{claims_block}

Each container a place's mention record puts it in, nearest first, with
the evidence it gives and where it occurs, nearest the place's mentions
first:
{within_block}

Output NDJSON, one finding per line, no prose, no code fences:
{{"entity": "<the entity id the finding concerns>",
"claim": {{"subject": "<subject as listed>", "property": "<property as \
listed>", "value": "<value as listed>"}} or null,
"alias": "<the alias as listed>" or null,
"container": "<the container as listed>" or null,
"axis": "<subject | predicate | value | existence>",
"category": "<one category key above>",
"issue": "<one line>",
"suggestion": {{"kind": "<kind key>" or null, "property": "<property key>" \
or null, "note": "<one line, or 'drop'>"}}}}
Name a claim when the finding concerns one, by its first three columns
(subject, property, value) exactly as listed: its entity is then the
claim's subject or the entity its value names. Name an alias only when
the finding concerns one of the entity's aliases, and a container only
when it concerns one of the place's containers. Emit a line ONLY for a
genuine concern; emit NONE if the extraction looks sound. After the last
finding, end with exactly one final line:
{{"done": true}}
"""


def _categories_block() -> str:
    return "\n".join(f"- {key}: {line};" for key, line in CATEGORIES.items())


def _entities_block(batch: ClaimBatch) -> str:
    """One row per entity. A person or a place with a mention record shows
    its status after its kind, so the critic can tell a person the
    document never names, whose label is rightly the document's words for
    them (ADR-0018), from one it names, and a country a source gives only
    by an adjective of nationality from a place it names (ADR-0035
    §2.2)."""
    lines = []
    for entity in batch.entities:
        aliases = ", ".join(entity.aliases) if entity.aliases else "-"
        hint = f" (hint: {entity.type_hint})" if entity.type_hint else ""
        status = (
            f" (status: {entity.structure.status})"
            if isinstance(entity.structure, (PersonStructure, PlaceStructure))
            else ""
        )
        lines.append(
            f"{entity.id} | {entity.kind}{hint}{status} | {entity.label} | "
            f"{aliases}"
        )
    return "\n".join(lines)


# how the prompt names the claim's own source document as a subject
THIS_DOCUMENT = "this document"


def _ref_id(
    ref: Any,
    source: str | None = None,
    places: Mapping[str, str] | None = None,
) -> str:
    """A reference as the prompt lists it: its local id, QID or
    vocabulary key; an artifact subject as THIS_DOCUMENT when it is the
    claim's own source (`source`, its SHA-1), by its place ("page 6,
    image 1") when it is an image the document places (`places`,
    artifact_places), else by its content key (ADR-0039 §10.4)."""
    if isinstance(ref, ArtifactRef):
        if ref.sha1 == source:
            return THIS_DOCUMENT
        return (places or {}).get(ref.sha1) or str(ref)
    for name in ("local_id", "qid", "key"):
        value = getattr(ref, name, None)
        if value:
            return value
    return "?"


# the characters other than a line feed at which str.splitlines() breaks a
# line, each with the escape a prompt listing writes for it
_LINE_BREAKS = str.maketrans(
    {
        "\r": "\\r",
        "\x0b": "\\x0b",
        "\x0c": "\\x0c",
        "\x1c": "\\x1c",
        "\x1d": "\\x1d",
        "\x1e": "\\x1e",
        "\x85": "\\x85",
        "\u2028": "\\u2028",
        "\u2029": "\\u2029",
    }
)


def _value(claim: Claim) -> str:
    """A claim's value as the prompt lists it: an item by its id, which
    the entities block labels; a quantity in full; a text with its
    language; anything else as `review.describe_value` renders it. A line
    feed is listed as a backslash and an n, and every other line break as
    its escape (_LINE_BREAKS), so each claim is one line."""
    value = claim.value
    if isinstance(value, ItemValue):
        rendered = _ref_id(value.value)
    elif isinstance(value, QuantityValue):
        rendered = repr(value.amount)
    else:
        rendered = describe_value(value)
    return (
        rendered.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .translate(_LINE_BREAKS)
    )


def artifact_places(transcription: Any) -> dict[str, str]:
    """The place of each image the transcription places, by its SHA-1, as
    the extraction prompts list it (extract.placed_lines): its first
    place, "page 6, image 1" or "slide 2, image 1". None places none."""
    if transcription is None:
        return {}
    unit = "slide" if transcription.engine == DECK_ENGINE else "page"
    places: dict[str, str] = {}
    for page in transcription.pages or ():
        for place, sha1 in enumerate(page.images, start=1):
            places.setdefault(sha1, f"{unit} {page.page}, image {place}")
    return places


def listings(
    batch: ClaimBatch, places: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """(subject, property, value) for each claim of the batch, in order:
    each claim as the prompt lists it, and as a finding names it, an
    artifact subject named as _ref_id names it, with `places` the places
    of the images the document places (artifact_places). A claim that
    lists as an earlier listing does has its value numbered (`... (#n)`),
    with the first number free, so that every listing names one claim."""
    taken: set[tuple[str, str, str]] = set()
    found = []
    for claim in batch.claims:
        subject = _ref_id(claim.subject, claim.source.artifact.sha1, places)
        listing = (subject, claim.property, _value(claim))
        numbered, n = listing, 1
        while numbered in taken:
            n += 1
            numbered = (*listing[:2], f"{listing[2]} (#{n})")
        taken.add(numbered)
        found.append(numbered)
    return found


# the qualifiers that date a statement, or say how its source dates it
# (ADR-0038 §2): a claim's row lists them, so the critic can check each
# against the citation
CONTAINMENT_TIMES = (
    "point_in_time",
    "start_time",
    "end_time",
    "earliest_date",
    "latest_date",
    "latest_start_date",
    "earliest_end_date",
    "sourcing_circumstances",
)

# the most occurrences of a citation a row shows
OCCURRENCES = 3

# the mark the extraction ends an excerpt it clipped with
# (extract.py's excerpt clipping)
CLIPPED = "…"


def _quoted(text: str) -> str:
    """A citation's text quoted as a JSON string, with the line breaks
    JSON leaves raw escaped (_LINE_BREAKS), so it stays on its row."""
    return json.dumps(text, ensure_ascii=False).translate(_LINE_BREAKS)


def _nearest_first(
    found: list[tuple[int, int]], near: Iterable[int]
) -> list[tuple[int, int]]:
    """Occurrences (first and last line) ordered by how near they lie to
    the nearest of the lines `near`, those as near in document order;
    in document order when `near` is empty."""
    near = list(near)
    if not near:
        return found

    def distance(span: tuple[int, int]) -> int:
        first, last = span
        return min(
            0
            if first <= line <= last
            else min(abs(first - line), abs(last - line))
            for line in near
        )

    return sorted(found, key=distance)


def _lines(found: list[tuple[int, int]]) -> str:
    """Occurrences as a row lists them: `line 4` for one on one line,
    else `lines` and each occurrence's line or range (`4-5`), split by
    `; `, at most OCCURRENCES of them and `and N more`."""
    shown = [
        str(first) if first == last else f"{first}-{last}"
        for first, last in found[:OCCURRENCES]
    ]
    one = len(found) == 1 and found[0][0] == found[0][1]
    text = f"{'line' if one else 'lines'} {'; '.join(shown)}"
    if len(found) > OCCURRENCES:
        text += f" and {len(found) - OCCURRENCES} more"
    return text


def _cited_page(page_number: str | None, transcription: Any) -> int | None:
    """The physical page a claim cites, when its `page_number` names one
    by its digits alone and the transcription has that page; else None,
    and the claim is read wherever its excerpt occurs. An image excerpt's
    claims cite the page of the container it was found on, which its own
    transcription does not number so."""
    if transcription is None:
        return None
    match = re.fullmatch(r"0*([0-9]{1,9})", (page_number or "").strip())
    if match is None:
        return None
    page = int(match.group(1))
    held = {given.page for given in transcription.pages or ()}
    return page if page in held else None


class _Located:
    """A document's citations located in its transcription (ADR-0003):
    each at every place it occurs (core.verbatim.Transcript.spans), with
    the page each lies on."""

    def __init__(self, transcription: Any) -> None:
        self.transcription = transcription
        self.transcript = Transcript(
            transcription.text if transcription is not None else ""
        )
        self.pages = (
            (transcription.pages or ()) if transcription is not None else ()
        )

    def citation(
        self, text: str | None, near: Iterable[int], page: int | None = None
    ) -> str:
        """A citation as a row shows it: where it occurs, then its text
        quoted. Its occurrences are those on `page` alone, when one is
        cited and the excerpt occurs there (`not on page N` and the others
        when it does not), else all of them, nearest `near` first. An
        excerpt the extraction clipped (CLIPPED) is located by its words
        before the mark when it does not occur whole; one that occurs
        nowhere is `not in the transcription`; no excerpt is `-`."""
        if not text or not words(text):
            return "-"
        quoted = _quoted(text)
        found = self.transcript.spans(text)
        mark = ""
        if not found and text.endswith(CLIPPED):
            found = self.transcript.spans(text[: -len(CLIPPED)])
            mark = ", clipped" if found else ""
        if not found:
            return f"not in the transcription: {quoted}"
        if page is None:
            return f"{_lines(_nearest_first(found, near))}{mark}: {quoted}"
        on = [
            span
            for span in found
            if getattr(self.transcript.page(span[0], self.pages), "page", None)
            == page
        ]
        if not on:
            where = _lines(_nearest_first(found, near))
            return f"not on page {page} ({where}){mark}: {quoted}"
        return (
            f"page {page}, {_lines(_nearest_first(on, near))}{mark}: {quoted}"
        )


def _times(claim: Claim) -> str:
    """The time qualifiers a claim carries (CONTAINMENT_TIMES), split by
    `; `, or "-" for none."""
    times = "; ".join(
        f"{qualifier.property} {describe_value(qualifier.value)}"
        for qualifier in claim.qualifiers
        if qualifier.property in CONTAINMENT_TIMES
    )
    if not times:
        return "-"
    return (
        times.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .translate(_LINE_BREAKS)
    )


def _claims_block(batch: ClaimBatch, located: _Located) -> str:
    """One row per claim: its listing (`listings`), the times it carries
    (`_times`) and its citation where it stands (`_Located.citation`),
    nearest the subject's mention lines first."""
    lines_of = {entity.id: entity.mention_lines for entity in batch.entities}
    rows = []
    places = artifact_places(located.transcription)
    for claim, listing in zip(
        batch.claims, listings(batch, places), strict=True
    ):
        excerpt = claim.source.transcription_excerpt
        citation = located.citation(
            excerpt.text if excerpt is not None else None,
            lines_of.get(getattr(claim.subject, "local_id", None), ()),
            _cited_page(claim.source.page_number, located.transcription),
        )
        rows.append(
            " | ".join((*listing, f"times: {_times(claim)}", citation))
        )
    return "\n".join(rows)


def _within_block(batch: ClaimBatch, located: _Located) -> str:
    """Each container of each place's mention record, nearest first, with
    its evidence where it stands (`_Located.citation`), nearest the
    place's mention lines first, so the critic can check that the
    evidence conveys the containment (ADR-0003, ADR-0035 §5); each row
    opens with a dash, so it is never taken for a claim or an entity
    row."""
    lines = []
    for entity in batch.entities:
        if not isinstance(entity.structure, PlaceStructure):
            continue
        for container in entity.structure.within:
            citation = located.citation(
                container.evidence, entity.mention_lines
            )
            lines.append(f"- {entity.id} within {container.name} | {citation}")
    return "\n".join(lines)


def _transcription_block(transcription: Any) -> str:
    """The transcription each line numbered, as the extraction prompts
    carry it (chunking.number_lines, Transcription.prompt_text)."""
    from yizkr.pipeline.chunking import number_lines

    if transcription is None:
        return ""
    return number_lines(transcription.prompt_text())


def named(claim: Claim) -> list[str]:
    """The local ids a claim names: its subject, its value and its
    qualifiers' values, in that order."""
    refs = [claim.subject]
    if isinstance(claim.value, ItemValue):
        refs.append(claim.value.value)
    refs.extend(
        qualifier.value.value
        for qualifier in claim.qualifiers
        if isinstance(qualifier.value, ItemValue)
    )
    return [ref.local_id for ref in refs if isinstance(ref, LocalRef)]


def claim_of(
    finding: Finding,
    batch: ClaimBatch,
    places: Mapping[str, str] | None = None,
) -> int | None:
    """The index of the claim of `batch` the finding names, as the prompt
    lists it (listings, with `places`), when that claim names the
    finding's entity; None otherwise. A subject named by an image's place
    is matched whatever its case, as the transcription's markers write
    "Page 6, image 1" where the rows write "page 6, image 1"."""
    if finding.claim is None:
        return None
    named_places = {place.casefold() for place in (places or {}).values()}

    def subject_key(subject: str) -> str:
        folded = subject.casefold()
        return folded if folded in named_places else subject

    wanted = (
        subject_key(finding.claim.subject),
        finding.claim.property,
        finding.claim.value,
    )
    for index, (subject, *rest) in enumerate(listings(batch, places)):
        if (subject_key(subject), *rest) == wanted:
            claim = batch.claims[index]
            return index if finding.entity in named(claim) else None
    return None


def check(
    obj: Mapping[str, Any],
    batch: ClaimBatch,
    vocabulary: tuple[Mapping[str, str], Iterable[str]],
    places: Mapping[str, str] | None = None,
) -> Finding | Invalid:
    """The finding `obj` gives when every check passes; else `obj`
    verbatim, with each reason it fails. `places` names the images the
    document places, as the prompt lists them (artifact_places)."""
    raw = dict(obj)
    try:
        finding = Finding.model_validate(raw)
    except ValidationError as exc:
        why = [
            f"{'.'.join(map(str, error['loc'])) or 'finding'}: {error['msg']}"
            for error in exc.errors()
        ]
        return Invalid(invalid=raw, why=why)
    properties, items = vocabulary
    why: list[str] = []
    if finding.axis not in AXES:
        why.append(f"axis {finding.axis!r} is not one of {', '.join(AXES)}")
    if finding.category not in CATEGORIES:
        why.append(f"category {finding.category!r} is not in the list")
    entities: dict[str, EntityRecord] = {e.id: e for e in batch.entities}
    entity = entities.get(finding.entity)
    if entity is None:
        why.append(f"entity {finding.entity!r} is not in the manifest")
    if finding.claim is not None and claim_of(finding, batch, places) is None:
        why.append("no claim is listed as given that names the entity")
    if (
        finding.alias is not None
        and entity is not None
        and finding.alias not in (entity.label, *entity.aliases)
    ):
        why.append(f"alias {finding.alias!r} is not the entity's")
    if finding.container is not None and entity is not None:
        record = entity.structure
        listed = (
            record.containers() if isinstance(record, PlaceStructure) else []
        )
        if finding.container not in listed:
            why.append(
                f"container {finding.container!r} is not one the entity's "
                "record lists"
            )
    if (
        finding.category == "evidence_does_not_state"
        and finding.container is None
    ):
        why.append("an evidence_does_not_state finding names no container")
    if finding.category in CLAIM_CATEGORIES and finding.claim is None:
        why.append(f"an {finding.category} finding names no claim")
    suggested = finding.suggestion
    if suggested.kind is not None and suggested.kind not in set(items):
        why.append(f"suggested kind {suggested.kind!r} is not a manifest key")
    if suggested.property is not None and suggested.property not in properties:
        why.append(
            f"suggested property {suggested.property!r} is not a manifest key"
        )
    return Invalid(invalid=raw, why=why) if why else finding


# what the critique stage says of a critique the engine gave up on at its
# output limit (run_critique's `attention`)
GAVE_UP = f"critique {CUT_OFF_AT_LIMIT}: no findings"


def offered_properties() -> list[str]:
    """The manifest's property keys the critique prompt lists: every one
    the manifest offers (`offered`), retired and reference properties
    among them, which a suggestion may name as the claim's property to
    leave. A property held back from the prompts (`offered: false`), such
    as a run item's statement, is never listed."""
    from yizkr.model.schema import load_manifest

    return sorted(
        key for key, spec in load_manifest().properties.items() if spec.offered
    )


def critique_prompt(
    batch: ClaimBatch,
    transcription: Any,
    properties: Iterable[str],
    kinds: Iterable[str],
) -> str:
    """The critique prompt for `batch`, the document's `transcription`
    (or None), the property keys it lists and the kind keys."""
    located = _Located(transcription)
    return CRITIQUE_PROMPT.format(
        categories_block=_categories_block(),
        kinds=", ".join(sorted(kinds)),
        properties=", ".join(sorted(properties)),
        transcription_block=_transcription_block(transcription) or "(none)",
        entities_block=_entities_block(batch) or "(none)",
        claims_block=_claims_block(batch, located) or "(none)",
        within_block=_within_block(batch, located) or "(none)",
    )


def run_critique(
    client: LlmClient,
    batch: ClaimBatch,
    transcription: Any = None,
    *,
    attention: list[str] | None = None,
    vocabulary: tuple[Mapping[str, str], Iterable[str]] | None = None,
) -> list[Finding | Invalid]:
    """Review the batch; return its findings (possibly none), each valid
    or kept verbatim as `Invalid`, in the order the critic gave them.
    The prompt carries `transcription`, the document's (a
    transcribe.Transcription), whole, each line numbered, and each
    citation located in it (ADR-0003); with none, it carries no
    transcription, and every citation is one not in it.
    `vocabulary` is the model manifest's (property key -> datatype, item
    keys), loaded when not given; the prompt then lists the properties
    the manifest offers (offered_properties), and the answer is checked
    against them all.

    Advisory only — an unfinished answer never raises (unlike chunk
    extraction), but for a rejection the cache refuses (a pause, as a
    call it refuses is; _reject): the critic must never block the
    pipeline, only inform the reviewer. An answer the engine stopped at
    its output limit (llm.at_output_limit), or one without its terminal
    sentinel (a refusal, an answer cut off, a usage-limit notice), still
    gives the findings it holds, and is rejected through the client
    (llm.reject_response), so that a client that keeps answers asks the
    critic again next time rather than serving it. One the client kept
    from an earlier call is asked for once more, and the engine's answer
    is used in its place, as it would be without the cache. A call the
    engine gave up on at its output limit (llm.OutputLimitReached) gave no
    answer: there are no findings, and nothing to reject, since the cache
    keeps nothing for it. So that an empty critique of that kind never
    passes for a sound extraction's, GAVE_UP is added to `attention`, when
    given, for the stage to show.
    """
    if not batch.entities and not batch.claims:
        return []
    if vocabulary is None:
        properties, items = load_vocabulary()
        listed = offered_properties()
    else:
        properties, items = vocabulary
        listed = list(properties)
    vocabulary = (properties, frozenset(items))
    prompt = critique_prompt(batch, transcription, listed, vocabulary[1])
    places = artifact_places(transcription)
    try:
        response = client.complete(prompt)
        findings, unfinished = _read(
            client, response, batch, vocabulary, places
        )
        if unfinished and _reject(client, response, unfinished):
            response = client.complete(prompt)
            findings, unfinished = _read(
                client, response, batch, vocabulary, places
            )
            if unfinished:
                _reject(client, response, unfinished)
    except OutputLimitReached:
        logger.warning(GAVE_UP)
        if attention is not None:
            attention.append(GAVE_UP)
        return []
    return findings


def _read(
    client: LlmClient,
    response: str,
    batch: ClaimBatch,
    vocabulary: tuple[Mapping[str, str], Iterable[str]],
    places: Mapping[str, str] | None = None,
) -> tuple[list[Finding | Invalid], str]:
    """(the findings a critique answer holds, why the answer is unfinished,
    or "" when it is not): the engine stopped it at its output limit
    (llm.at_output_limit), or it has no terminal sentinel. `response` is
    the answer `client` has just given."""
    findings, finished = _parse_critique(response, batch, vocabulary, places)
    if at_output_limit(client, response):
        return findings, f"critique: {CUT_OFF_AT_LIMIT}"
    if not finished:
        return findings, "critique: no terminal sentinel"
    return findings, ""


def _parse_critique(
    response: str,
    batch: ClaimBatch,
    vocabulary: tuple[Mapping[str, str], Iterable[str]],
    places: Mapping[str, str] | None = None,
) -> tuple[list[Finding | Invalid], bool]:
    """(the findings a critique answer holds, each checked against the
    batch and the manifest, whether it has its terminal sentinel, a line
    whose `done` is true). An object with none of a finding's fields is
    not a finding."""
    findings: list[Finding | Invalid] = []
    finished = False
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        if FINDING_FIELDS.isdisjoint(obj):
            continue
        findings.append(check(obj, batch, vocabulary, places))
    return findings, finished


def _reject(client: LlmClient, response: str, reason: str) -> bool:
    """Reject an unfinished critique answer for `reason`; True when it was
    one the client kept from an earlier call (llm.reject_response). A
    rejection that fails is only a warning: the critic never blocks the
    pipeline. A rejection the cache refuses (llmcache.CacheRefused, a
    pause) ends the pass, as a call it refuses does."""
    try:
        return reject_response(client, response, reason)
    except Exception as exc:
        logger.warning("could not reject a critique answer: %s", exc)
        return False


# -- the review sheet -------------------------------------------------------


def note(finding: Finding) -> str:
    """A finding as the review sheet's `critique_notes` column shows it:
    its axis and category, the entity it concerns (and the alias), the
    issue and the suggestion."""
    where = finding.entity
    if finding.alias is not None:
        where += f" alias {finding.alias!r}"
    if finding.container is not None:
        where += f" within {finding.container!r}"
    text = f"critique [{finding.axis}, {finding.category}] {where}: "
    text += finding.issue
    suggested = finding.suggestion
    parts = [
        f"{name} {value}"
        for name, value in (
            ("kind", suggested.kind),
            ("property", suggested.property),
        )
        if value is not None
    ]
    if suggested.note:
        parts.append(suggested.note)
    if parts:
        text += f" (suggests {'; '.join(parts)})"
    return text


def counterparts(extracted: ClaimBatch, rows: ClaimBatch) -> list[int | None]:
    """For each claim of `extracted`, the index of the claim of `rows` it
    became, where `rows` is `extracted` resolved; all None when it is not.

    Resolution has two steps. The approved equivalences replace each
    local ref to an approved entity with its QID, and set that entity's
    `qid`, and a claim they make identical to an earlier one, but for the
    local subject each records it replaced, is dropped
    (reconcile.apply_equivalences, reconcile.collapse). Then each
    remaining local ref may be replaced with a QID by its entity's label
    (resolve.resolve_batch).
    The first step is replayed here exactly, from the entities whose
    `qid` `rows` sets; the second is checked claim by claim, a local ref
    against the ref at its place, so rows in which one local id became
    different QIDs in different claims match too."""
    from yizkr.pipeline.reconcile import apply_equivalences, collapse

    before = {entity.id: entity.qid for entity in extracted.entities}
    approved = {
        entity.id: entity.qid
        for entity in rows.entities
        if entity.qid and before.get(entity.id) != entity.qid
    }
    merged, places = collapse(
        apply_equivalences(
            extracted.model_copy(update={"claims": [claim]}), approved
        ).claims[0]
        for claim in extracted.claims
    )
    found: list[int | None] = list(places)
    if len(merged) != len(rows.claims) or not all(
        _resolved_as(claim, row)
        for claim, row in zip(merged, rows.claims, strict=True)
    ):
        return [None] * len(extracted.claims)
    return found


def _resolved_as(claim: Claim, row: Claim) -> bool:
    """Whether `row` is `claim` with none or more of its local refs
    replaced by QID references."""

    def same(a: Any, b: Any) -> bool:
        if isinstance(a, dict) and isinstance(b, dict):
            if a.get("kind") == "local" and b.get("kind") == "qid":
                return True
            return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(map(same, a, b))
        return a == b

    return same(claim.model_dump(mode="json"), row.model_dump(mode="json"))


def attach(
    findings: Iterable[Finding],
    extracted: ClaimBatch,
    rows: ClaimBatch,
    places: Mapping[str, str] | None = None,
) -> dict[int, list[Finding]]:
    """Each finding by the index of the review row it goes on, each on
    exactly one row. `extracted` is the batch the critic reviewed, and
    `rows` the batch the review sheet lists, `extracted` resolved
    (`counterparts`), so that a finding reaches its row whichever of its
    entities resolved to a QID. `places` names the images the document
    places, as the prompt listed them (artifact_places).

    A finding that names a claim goes on that claim's row. One about an
    entity goes on the row of the first claim whose subject is the
    entity, else of the first claim that names it as a value or a
    qualifier's value. An entity no row names has no row of its own: its
    finding goes on the sheet's first row, and the note names the entity.
    With no rows, nothing is attached."""
    if not rows.claims:
        return {}
    where = counterparts(extracted, rows)
    attached: dict[int, list[Finding]] = {}
    for finding in findings:
        index = claim_of(finding, extracted, places)
        row = where[index] if index is not None else None
        if row is None:
            row = _entity_row(finding.entity, extracted, where)
        attached.setdefault(row, []).append(finding)
    return attached


def _entity_row(
    entity: str, extracted: ClaimBatch, where: list[int | None]
) -> int:
    for as_subject in (True, False):
        for index, claim in enumerate(extracted.claims):
            hit = (
                getattr(claim.subject, "local_id", None) == entity
                if as_subject
                else entity in named(claim)
            )
            if hit and where[index] is not None:
                return where[index]
    return 0


def unchecked(invalid: Invalid) -> str:
    """A finding that failed its checks as the review sheet shows it:
    marked unchecked, as the critic gave it, and why it failed."""
    given = json.dumps(invalid.invalid, ensure_ascii=False)
    return f"critique [unchecked] {given} (fails: {'; '.join(invalid.why)})"


def sheet_notes(
    entries: Iterable[Finding | Invalid],
    extracted: ClaimBatch,
    rows: ClaimBatch,
    places: Mapping[str, str] | None = None,
) -> dict[int, str]:
    """review row index -> the notes on that row, joined: each valid
    finding on its row (`attach`, with `places`), and each finding that
    failed its checks on the first row, marked unchecked, since it names
    nothing a row can be matched by. With no rows, there are no notes."""
    if not rows.claims:
        return {}
    entries = list(entries)
    valid = [entry for entry in entries if isinstance(entry, Finding)]
    notes = {
        row: list(map(note, attached))
        for row, attached in attach(valid, extracted, rows, places).items()
    }
    for entry in entries:
        if isinstance(entry, Invalid):
            notes.setdefault(0, []).append(unchecked(entry))
    return {row: "; ".join(texts) for row, texts in sorted(notes.items())}


def entries(stored: Any) -> list[Finding | Invalid]:
    """A critique view's entries, each a finding or one that failed its
    checks. Raises ValueError when the view holds anything else: a list of
    another shape, such as findings in an earlier format."""
    if not isinstance(stored, list):
        raise ValueError("a critique view holds a list of findings")
    found = []
    for index, item in enumerate(stored):
        try:
            if not isinstance(item, Mapping):
                raise TypeError("not an object")
            found.append(entry(item))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"critique entry {index} is neither a finding nor one "
                f"that failed its checks"
            ) from exc
    return found
