"""Claim extraction step: transcription -> ClaimBatch JSON.

`LlmClaimExtractor` prompts an LLM with the claim JSON Schema, the
manifest vocabulary, and the transcription; the response is parsed and
validated against the schema AND the model manifest before it goes
anywhere. Validation is per claim: claims that pass proceed (to human
review — nothing machine-written reaches the graph unreviewed), claims
that fail are reported as rejects with their reasons. Malformed machine
output must never limp toward the graph, but one bad claim must not
discard an expensive extraction either.

`ClaimFileExtractor` passes a prepared claim file through the same
validation, so hand-written batches and future extraction tools get
identical treatment.

Every batch records extraction-run metadata (ADR-0003). Subjects the
model proposes are `local` refs; resolution to existing items happens
in the resolve step, never inside extraction. The model-driven
extractors, this module's and yizkr.pipeline.chunking's, set every
claim's source artifact themselves, to the content key of the document
they read, whatever the model wrote there, set each claim's printed
page number from the transcription's record, never from the model, and
keep no review state the model wrote (`cite_artifact`); a prepared
claims file's claims cite their artifacts themselves.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

import yizkr
from yizkr.claims import (
    SCHEMA_REASON,
    ArtifactRef,
    ClaimBatch,
    ExtractionRunMetadata,
    load_vocabulary,
    placed_artifacts,
    reject_reasons,
    validate_against_manifest,
)
from yizkr.model.schema import load_acceptance, load_manifest
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    ClassifierRefused,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)
from yizkr.pipeline.transcribe import DECK_ENGINE, Transcription

EXTRACTION_PROMPT = """\
You extract source-attributed genealogical claims from transcriptions of
historical documents.

Rules — these mirror the archive's non-negotiable conventions:
- Record exactly what the source asserts. Never round dates, never guess,
  never narrow a vague statement. Use precision (year/month/day/decade/
  century), earliest_date/latest_date qualifiers for ranges, and the
  sourcing_circumstances qualifier with the "circa" vocabulary item for
  approximate values.
- A year alone is year precision, never a full date: "1905" is never
  1 January 1905 at day precision. A century or a decade the source
  implies ("in the thirties", "в тридцатые годы") is read in the
  document's own context (the 1930s, in a letter of 1958), never as a
  literal "1st century" or the year 30.
- Every claim MUST include the source reference: the artifact
  {{"kind": "artifact", "sha1": "{artifact_sha1}"}}, plus page number and a
  short verbatim transcription excerpt supporting it. The page number is
  the N of the [Page N] (or [Slide N]) marker the text stands under,
  never a printed page number shown beside that marker.
- Refer to people, places, and events the document mentions with "local"
  references (stable local_id per entity within this batch, label as
  written, instance_of from: {kinds}).
- A kinship word is never the label of a person the document names:
  take the name, never the kinship word. "Aunt Pünktchen" is labelled
  "Pünktchen", or a fuller name the document gives her, and a writer's
  "Dein Vater" ("your father") takes the father's name where the
  document gives it. Only a person the document never
  names is labelled with its kinship words ("Mem").
- Every form the document uses to name or address a person that is no
  plain name form (a name, or a spelling, a shortening, a diminutive or
  an initial of one) is an appellation: a kinship term with a name
  ("Tante Rahel") or alone ("Omama"), a pet name ("Pünktchen") or a
  courtesy name ("Nana Maisie"). Give each as ONE `referred_to_as` claim,
  the form verbatim, with a `perspective` qualifier naming the ego only
  where the source gives it as an entity; a literal kinship term also
  gives the kinship ("Tante Rahel", where Rahel is the aunt, also gives
  `relative` with the kinship_type "Tante" and the kinship_class
  kin_aunt_or_uncle). A description that is no
  form of address ("the deceased", "my husband") gives none.
- Kinds: a group of people is never one human. "The Löbig children" is
  a family where the source presents a family or household, else each
  child it names. A count of children the source states is the parent's
  `number_of_children`, whether or not it names them. A year or a date
  alone is no entity and no event: it dates the event it belongs to.
  `unclassified_entity` is right only where no kind fits: an agency, an
  office or a firm's department is an organization, unless the source
  names it only as where something happened, which makes it a place; a
  cemetery or a building is a place; a named ship a vessel. A named
  aircraft is no vessel: it is `unclassified_entity` with the type_hint
  "aircraft".
- Give a place's containment (`part_of`, its country among them) only
  where the source states it: in the place's mention ("Fläckfeld, Kreis
  Mockburg"), in the fields a form gives that place, or in a chain of
  containers, which states each link ("Snylsjum, gemeente Drakeradiel,
  Fryslân" also puts gemeente Drakeradiel in Fryslân). Never infer it
  from what you know of a place, and never carry it over from another
  place the document names. Give it the time the source gives it for
  the containment, and none where it gives none: `start_time` or
  `end_time` where the source dates when the place came into or left
  the container ("Kaldovsky District, in Chadsk Oblast from 1937"),
  `point_in_time` where it presents the place as in the container at a
  date ("in 1941 Koshnovka belonged to Kaldovsky District"), each with
  its range and circa as a period's are. The document's own date is
  never the containment's time, nor is the date of an event the source
  places there: a birth "in 1836 at Flörburg, Kreis Peiltal" gives
  Flörburg's `part_of` no time.
- Use ONLY these property keys (property key: datatype — what it means),
  for claims and qualifiers alike — a claim naming any other key is
  discarded. The description is authoritative: it says what the VALUE
  must be, and so which way a relation points. If the document asserts
  something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- Marriages and partnerships are entities bounded by events (ADR-0036);
  no property links two spouses directly: the couple are each a
  `participant` of the `marriage` (or `partnership`) with the
  `object_has_role` `spouse` (or `partner`), and the union is
  `started_by` its wedding and `ended_by` its end, each an event with its
  own date and place.
{document_block}
- Dating a period: a residence, an occupation or a membership is dated
  by its `start_time` and `end_time` qualifiers, a move in and a move out
  as its start and end. `point_in_time` on such a claim only records a
  date at which the source presents it as holding (a census, a
  directory entry). A marriage has no dates of its own: its wedding and
  its end are events, each with its own `point_in_time`. A range bounds
  one end: a start takes `earliest_date` and `latest_start_date`, an end
  takes `earliest_end_date` and `latest_date`. Moved in "between X and
  Y" is start_time X with earliest_date X and latest_start_date Y; moved
  out "before Z" is end_time Z with latest_date Z. These pairs bound
  `start_time` and `end_time` qualifiers; an entity's own `start_time` or
  `end_time` claim (a firm's founding, a detention's start) is a date
  value that `earliest_date` and `latest_date` bound.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of the JSON Schema below. After the last claim, end with
exactly one final line:
{{"done": true}}

{schema}

Transcription of artifact sha1:{artifact_sha1} (language: {language}):

{text}
"""


# The paragraph on the document itself, in both extraction prompts
# (ADR-0035 §3.4, ADR-0039 §2.2): the document-subject properties, each
# given only where the document states it of itself.
DOCUMENT_ITSELF = """\
- The document itself: these properties state what THIS document is,
  each only where the document states it of itself, never from what
  another document says of it. Their subject is the document, given
  exactly as the source's artifact:
  {{"kind": "artifact", "sha1": "{artifact_sha1}"}}, and never anything
  else:
  * `issued_by`: the organization that issued it, where the source shows
    it issuing (the register office of a certificate, the court of a
    judgment);
  * `author`: who wrote, drew or made it, where it names them (a
    letter's signature, a studio's name on a photograph's mount);
  * `addressee`: whom it is addressed to (a letter's greeting, an
    envelope's address);
  * `inception`: when it was made (a letter's dateline), at the
    precision it gives;
  * `location_of_creation`: where it was made (a letter's heading);
  * `main_subject`: what it is about (a letter's topic, the district a
    map covers);
  * `shows`: whom or what an image shows, as its own content names them
    (an inscription on a print: one claim per name, no position read
    into their order); never `depicts`, which only the faces flow
    gives.
  No other property takes the document as its subject."""

# The paragraph on what the document places (ADR-0039 §3), in both
# extraction prompts: the images its pages or slides place, each by its
# artifact reference and its place, or that it places none.
PLACED_ARTIFACTS = """\
- What this document places: the images below stand on its pages or
  slides, each marked where it sits by the SHA-1 of its bytes
  ("[Page 6, image 1: sha1:…]"):
{placed_lines}
  Give each passage of THIS document's own text that speaks of one of
  them (a caption, a heading over it, a sentence of commentary) as ONE
  `caption` claim: its subject that image's artifact reference exactly
  as listed, its value the passage exactly as written, in the
  container's language, never cut, corrected or translated, its page
  number the page the passage stands on. A passage that speaks of two
  images gives one claim for each. Where the text does not say plainly
  that the passage speaks of the image (a slide's title over its one
  image), add a judgement note of category `relation`. Give nothing
  else about an image: who made it, for whom, when, where, what it is
  about or whom it shows is the image's own claim, read from the image
  apart. The people a caption names are entities of this document, with
  what it says of them, as anywhere else."""

NO_PLACED_ARTIFACTS = """\
- This document places no image read on its own: give no `caption`
  claim."""


def placed_lines(transcription: Transcription) -> list[str]:
    """Each image the transcription places, once for each place it has,
    as the prompts list it: its artifact reference and its place ("page 6,
    image 1", "slide 2, image 1"), in the order of the pages and of the
    images on each page, as the transcription's markers give them."""
    unit = "slide" if transcription.engine == DECK_ENGINE else "page"
    return [
        f'  {{"kind": "artifact", "sha1": "{sha1}"}}: {unit} {page.page}, '
        f"image {place}"
        for page in transcription.pages or ()
        for place, sha1 in enumerate(page.images, start=1)
    ]


def document_block(transcription: Transcription, artifact: ArtifactRef) -> str:
    """The prompts' paragraphs on the document itself and on what it
    places (DOCUMENT_ITSELF, PLACED_ARTIFACTS): the images listed are the
    ones the transcription's pages mark, never named by a QID, and never
    with anything another document says of them (ADR-0039 §5)."""
    lines = placed_lines(transcription)
    placed = (
        PLACED_ARTIFACTS.format(placed_lines="\n".join(lines))
        if lines
        else NO_PLACED_ARTIFACTS
    )
    return DOCUMENT_ITSELF.format(artifact_sha1=artifact.sha1) + "\n" + placed


def placed_by(
    transcription: Transcription, artifact: ArtifactRef
) -> dict[str, frozenset[str]]:
    """What validation is handed of the artifacts the document places
    (claims.validate_against_manifest's `placed`): by the document's
    content key, the SHA-1s its transcription's pages mark."""
    return {artifact.sha1: placed_artifacts(transcription.pages)}


def run_metadata(model_identifier: str) -> ExtractionRunMetadata:
    return ExtractionRunMetadata(
        model_identifier=model_identifier,
        pipeline_version=yizkr.__version__,
        software_commit=_software_commit(),
        started_at=datetime.now(UTC),
    )


# The commit git gave for this code's checkout, once it has given one.
_commit: str | None = None


def _software_commit() -> str | None:
    """The short commit of the git checkout this code runs from, or None
    when git gives none (see _ask_git_for_commit).

    Every batch asks git until it answers, so a failed ask costs only its
    own batch the commit. The first answer is kept for the process, and
    later batches name the same commit without starting git; batches that
    ask at the same moment as the first may each start git."""
    global _commit
    if _commit is None:
        commit = _ask_git_for_commit()
        if commit is None:
            return None
        _commit = commit
    return _commit


def _ask_git_for_commit() -> str | None:
    """git's short commit for the checkout this code runs from, or None
    when git cannot be started (it is missing, say, or the process is out
    of processes or descriptors), exits with an error (outside a checkout)
    or prints no commit. A git that runs past the timeout raises
    subprocess.TimeoutExpired."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent,
        )
    except OSError:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


class JsonScan(NamedTuple):
    """What scan_json_answer reads from an answer: its objects, as
    scan_json_objects gives them, and how many trailing commas it read as
    JSON syntax to decode them."""

    objects: list[dict]
    trailing_commas: int


def scan_json_objects(response: str) -> list[dict]:
    """Every JSON object decodable from model output, in order.

    Tolerates code fences, surrounding prose, NDJSON, and corrupt
    regions (scanning resumes after any undecodable span). Objects
    nested inside a decoded object are not yielded separately."""
    return _scan(response, read_trailing_commas=False).objects


def scan_json_answer(response: str) -> JsonScan:
    """The objects of an answer as scan_json_objects reads them, but with
    a single trailing comma directly before a closing `]` or `}`, outside
    any string and with only whitespace between, read as JSON syntax, and
    each one so read counted. The comma is the only thing read past: two
    commas, a comma before a value or after a key, a missing comma or any
    other defect still leaves the span undecodable, and no value is
    added, changed or guessed."""
    return _scan(response, read_trailing_commas=True)


def _scan(response: str, *, read_trailing_commas: bool) -> JsonScan:
    text = response.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    commas = 0
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            break
        try:
            if read_trailing_commas:
                obj, end, read = _decode_past_trailing_commas(
                    decoder, text, start
                )
                commas += read
            else:
                obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        index = end
    return JsonScan(objects, commas)


def _decode_past_trailing_commas(
    decoder: json.JSONDecoder, text: str, start: int
) -> tuple[Any, int, int]:
    """(value, end, commas read): the value decoded at `start`, each
    trailing comma the decoder stops at (_trailing_comma) blanked to a
    space in a copy of the text, so `end` stands in the text as given. The
    value must then decode whole: raises json.JSONDecodeError otherwise,
    and nothing it read counts."""
    read = 0
    while True:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            comma = _trailing_comma(text, exc.pos)
            if comma is None:
                raise
            text = f"{text[:comma]} {text[comma + 1 :]}"
            read += 1
            continue
        return obj, end, read


def _trailing_comma(text: str, stop: int) -> int | None:
    """The index of the trailing comma the decoder stopped at, or None
    when it stopped for anything else. The decoder stops at the comma or
    at the closing bracket or brace after it, depending on the Python
    version, and either stop is outside any string. One rule then holds:
    the stop, or the first character before a closer stop that is not
    whitespace, is a comma; the first after the comma that is not
    whitespace is `]` or `}`; and the first before it is not `,`, `[`,
    `{` or `:`, so a value precedes it."""
    if stop >= len(text):
        return None
    comma = stop
    if text[stop] in "]}":
        comma = _skip_space(text, stop - 1, -1)
    if comma is None or text[comma] != ",":
        return None
    closer = _skip_space(text, comma + 1, 1)
    if closer is None or text[closer] not in "]}":
        return None
    before = _skip_space(text, comma - 1, -1)
    if before is None or text[before] in ",[{:":
        return None
    return comma


def _skip_space(text: str, index: int, step: int) -> int | None:
    """The first index from `index`, moving by `step`, whose character is
    not JSON whitespace, or None when the text ends first."""
    while 0 <= index < len(text):
        if text[index] not in " \t\n\r":
            return index
        index += step
    return None


class ExtractionFailed(RuntimeError):
    """The extraction ended with no claims for want of a usable answer.
    The message is the reason the extraction stage records and
    `ingest extract` reports in one line; any other exception, a bare
    ValueError included, is a bug.

    The single-call extractors raise it for an answer with no claim JSON,
    one that holds no claims, one whose every claim failed validation, or
    a call the engine gave no answer to (llm.OutputLimitReached,
    llm.ClassifierRefused); and for a claims file that cannot be read, is
    not JSON or not a JSON object, or fails schema or manifest validation.

    The chunked extractor (chunking.ChunkedClaimExtractor) raises it when
    a chunk failed, the chunks' answers held objects the parser could not
    read as claims, or validation rejected every claim. The message leads
    with that outcome and its causes, then names any fallback tier taken,
    with the reason each attempt at the refused turn gave. An extraction
    that ends with no claims because the answers it accepted say the
    document holds none, on whichever tier, is not a failure: its batch
    is returned, and the extractor's `attention` says why it is empty."""


def _parse_response(response: str) -> dict:
    """Extract claim JSON from model output.

    Accepts NDJSON claim lines (the format the prompt asks for), a
    wrapped {"claims": [...]} object or several concatenated ones
    (continuation chunks), or any mix. Salvage property: a
    response that spans several turns can carry an inexact seam that
    corrupts an enclosing object — every individual claim object that
    still decodes is collected regardless, so one bad seam cannot
    discard an entire expensive extraction. Duplicates (models overlap
    at resume points) are dropped."""
    merged: list = []
    salvaged: list = []
    found = False
    for obj in scan_json_objects(response):
        if "claims" in obj:
            found = True
            merged.extend(obj.get("claims") or [])
        elif "subject" in obj and "property" in obj:
            salvaged.append(obj)
    if not found and not salvaged:
        raise ExtractionFailed(
            f"no claim JSON in extractor response (neither a 'claims' "
            f"object nor loose claim objects): {response.strip()[:200]!r}"
        )
    claims = merged + salvaged
    deduped: list = []
    for claim in claims:
        if claim not in deduped:
            deduped.append(claim)
    return {"claims": deduped}


# The claim fields that record a curator's review, never the machine's
# extraction: `curated` (a person stated the claim, so it carries no
# extraction-run reference) and `row_approval` (a curator approved the
# claim on its own review-sheet row). The JSON Schema the prompts carry
# shows `curated` with the rest of a claim.
REVIEW_STATE = ("curated", "row_approval")


def cite_artifact(
    claims: Iterable,
    artifact: ArtifactRef,
    printed_pages: Mapping[int, str] | None = None,
) -> list:
    """The claims read from one document, as the machine extracted them:
    every claim's `source.artifact` becomes `artifact`, the document's
    content key, whatever the model wrote there; every claim's
    `source.printed_page_number` is the page number printed on the page it
    cites, from `printed_pages` (the transcription's, by physical page,
    Transcription.printed_pages), or absent (printed_page_number); the
    review-state fields (REVIEW_STATE) are dropped, so every claim is a
    machine claim, which the writer references to its extraction run
    (ADR-0003); and claims that are then the same are kept once, in the
    order first given.

    An extractor reads one document's transcription, so every claim it
    gives is an assertion of that document, and the extractor, handed the
    document's key, names the source itself: no claim cites another
    artifact through a key the model copied wrongly, or through a form of
    reference the claim contract does not have. A claim whose source is
    not an object keeps it, for validation to reject, as a claim without
    its source is."""
    printed = printed_pages or {}
    cited: list = []
    for claim in claims:
        if isinstance(claim, dict):
            claim = {
                key: value
                for key, value in claim.items()
                if key not in REVIEW_STATE
            }
            if isinstance(claim.get("source"), dict):
                source = {
                    key: value
                    for key, value in claim["source"].items()
                    if key != "printed_page_number"
                }
                source["artifact"] = artifact.model_dump(mode="json")
                number = printed_page_number(
                    source.get("page_number"), printed
                )
                if number is not None:
                    source["printed_page_number"] = number
                claim["source"] = source
        if claim not in cited:
            cited.append(claim)
    return cited


def printed_page_number(
    page_number: object, printed_pages: Mapping[int, str]
) -> str | None:
    """The page number printed on the page a claim's `page_number` cites,
    by `printed_pages` (physical page -> printed number, as the
    transcription record gives them): only when `page_number` names one
    physical page, as its digits alone ("6"), and the record gives that
    page a printed number. Anything else, a range ("6-7"), a page given
    in words ("p. 6"), no page, or a page the record gives no number,
    has none: a printed number is read from the record, never guessed.
    A printed number of whitespace alone is none, as the writer would
    write none for it (writer._locator_text). The digits are bounded
    before they become an int, so a runaway `page_number` from the model
    is a page with no number, never an error that would sink the
    document's extraction."""
    if not isinstance(page_number, str):
        return None
    digits = page_number.strip()
    match = re.fullmatch(r"0*([0-9]{1,9})", digits)
    if match is None:
        return None
    printed = printed_pages.get(int(match.group(1)))
    return printed if printed and printed.strip() else None


def _normalize_claim_shape(claim: dict) -> dict:
    """Repair a mechanical output-shape slip models actually make.

    Item values nest under "value" ({"type": "item", "value": {...}}),
    and models sometimes wrap time values the same way:
    {"type": "time", "value": {"year": ...}} instead of the flat
    {"type": "time", "year": ...} the schema requires. The content is
    unambiguous, so flatten rather than reject 10 minutes of output.
    """

    def flatten(value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("type") == "time"
            and isinstance(value.get("value"), dict)
            and "year" in value["value"]
        ):
            return {"type": "time", **value["value"]}
        return value

    fixed = dict(claim)
    fixed["value"] = flatten(claim.get("value"))
    if isinstance(claim.get("qualifiers"), list):
        fixed["qualifiers"] = [
            {**qualifier, "value": flatten(qualifier.get("value"))}
            if isinstance(qualifier, dict)
            else qualifier
            for qualifier in claim["qualifiers"]
        ]
    # excerpts are verification aids ("short" per ADR-0003) and Wikibase
    # caps text values at 400 chars; clip visibly rather than lose the claim
    source = fixed.get("source")
    if isinstance(source, dict):
        excerpt = source.get("transcription_excerpt")
        if isinstance(excerpt, dict) and len(excerpt.get("text") or "") > 400:
            fixed["source"] = {
                **source,
                "transcription_excerpt": {
                    **excerpt,
                    "text": excerpt["text"][:397].rsplit(" ", 1)[0] + "…",
                },
            }
    return fixed


def salvage_valid_claims(
    claims: list,
    extraction_run: dict,
    entities: list[dict] | None = None,
    placed: Mapping[str, Collection[str]] | None = None,
) -> tuple[ClaimBatch, list[dict]]:
    """Partition raw claim dicts into a valid ClaimBatch and rejects.

    Machine output is expensive and reviewed by a human either way, so a
    claim that fails schema or manifest validation must not sink the
    claims that pass: the valid subset proceeds to review, the rejects
    are reported (each with its reasons) for diagnosis. A claim whose
    value is of a kind its property does not accept, or of a retired
    property, is a reject like any other (ADR-0035 §3.5).

    With `entities` (an entity manifest, ADR-0007), local refs naming
    ids outside the manifest are rejected, and the manifest travels in
    the returned batch. `placed` names the artifacts each source places
    (validate_against_manifest): a `caption` claim whose subject is none
    of its source's is a reject (ADR-0039 §2.1).

    Each reject carries its reason codes (`reasons`, claims.REJECT_REASONS),
    for a report to count without quoting it: `schema` for a claim that
    does not parse, else the manifest problems' codes (reject_reasons).
    """
    vocabulary = load_vocabulary()
    acceptance = load_acceptance()
    entities = entities or []
    if entities:
        # manifest-level defects poison every claim; check them once,
        # loudly, before per-claim triage
        manifest_only = ClaimBatch.model_validate(
            {"extraction_run": extraction_run, "entities": entities}
        )
        manifest_problems = validate_against_manifest(
            manifest_only, vocabulary=vocabulary, acceptance=acceptance
        )
        if manifest_problems:
            raise ValueError(
                "entity manifest failed validation:\n  "
                + "\n  ".join(manifest_problems)
            )
    valid: list = []
    rejects: list[dict] = []
    for claim in claims:
        if isinstance(claim, dict):
            claim = _normalize_claim_shape(claim)
        try:
            batch = ClaimBatch.model_validate(
                {
                    "extraction_run": extraction_run,
                    "claims": [claim],
                    "entities": entities,
                }
            )
        except Exception as exc:  # pydantic ValidationError, in short form
            rejects.append(
                {
                    "claim": claim,
                    "problems": [str(exc)],
                    "reasons": [SCHEMA_REASON],
                }
            )
            continue
        problems = validate_against_manifest(
            batch, vocabulary=vocabulary, acceptance=acceptance, placed=placed
        )
        if problems:
            rejects.append(
                {
                    "claim": claim,
                    "problems": problems,
                    "reasons": reject_reasons(problems),
                }
            )
        else:
            valid.append(claim)
    return (
        ClaimBatch.model_validate(
            {
                "extraction_run": extraction_run,
                "claims": valid,
                "entities": entities,
            }
        ),
        rejects,
    )


def _one_line(text: str) -> str:
    """Collapse a folded YAML description to a single prompt line."""
    return " ".join(text.split())


# The renderers below run for every extraction prompt, chunk prompts
# included, and what they render cannot change mid-run: the manifest
# ships as package data, and the claim schema is built from the claim
# models. Rendering parses and validates the whole manifest, or builds
# the whole schema, so each is cached and a hundred-chunk document pays
# for it once.


@lru_cache(maxsize=1)
def vocabulary_prompt_lines() -> tuple[str, str]:
    """Render the manifest vocabulary for the extraction prompt.

    Each property carries its manifest description, because that
    description is where the semantics a model needs to place a claim
    correctly already live: which way a relation points (`employer` is
    the organization, not the employee) and what kind of entity a value
    must be (`location` takes a place). Keys and datatypes alone would
    leave the model to guess both, and the manifest already holds the
    words. A property no claim may use, a retired or a reference
    property, is left out, as is one the manifest holds back from prompts
    (`ManifestProperty.in_prompts`).
    """
    manifest = load_manifest()
    property_lines = "\n".join(
        f"  {key}: {spec.datatype} — {_one_line(spec.description)}"
        for key, spec in sorted(manifest.properties.items())
        if spec.in_prompts
    )
    item_lines = "  " + ", ".join(sorted(manifest.items))
    return property_lines, item_lines


@lru_cache(maxsize=1)
def entity_kind_prompt_lines() -> tuple[str, str]:
    """Render the entity kinds for the manifest pass (ADR-0007).

    Returns (bare comma-joined keys, described block). The kinds are the
    manifest items flagged `entity_kind`, so the offerable set is
    declared in one place rather than restated in the prompt.
    """
    kinds = sorted(
        (
            (key, spec)
            for key, spec in load_manifest().items.items()
            if spec.entity_kind
        ),
        key=lambda entry: entry[0],
    )
    if not kinds:
        raise ValueError("manifest declares no entity_kind items")
    return (
        ", ".join(key for key, _ in kinds),
        "\n".join(
            f"  {key}: {_one_line(spec.description)}" for key, spec in kinds
        ),
    )


@lru_cache(maxsize=1)
def claim_schema_prompt_text() -> str:
    """The claim JSON Schema, as the single-call and chunk prompts carry
    it."""
    return json.dumps(ClaimBatch.model_json_schema(), indent=2)


class LlmClaimExtractor:
    """Single-call extraction: one prompt, one NDJSON answer.

    An answer the extractor cannot fully use is rejected through the
    client (llm.reject_response), so that a client that keeps answers
    asks the engine again next time rather than serving it: one with no
    claim JSON or no valid claim (the extraction then fails, with
    ExtractionFailed), and one cut off, which the engine stopped at its
    output limit (llm.at_output_limit) or which has no terminal sentinel
    (the claims it holds are still returned). An answer the client kept
    from an earlier call is asked for once more when it is rejected, and
    the engine's answer is used in its place, as it would be without the
    cache. A call the engine gave up on at its output limit
    (llm.OutputLimitReached), or one the safety classifier refused
    (llm.ClassifierRefused), gave no answer and so no claim: the
    extraction fails, with ExtractionFailed, and the cache keeps nothing
    for it, so the next run asks again."""

    def __init__(
        self,
        client: LlmClient,
        raw_dump: Path | None = None,
        rejects_dump: Path | None = None,
    ) -> None:
        self.client = client
        self.raw_dump = raw_dump
        self.rejects_dump = rejects_dump

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        """The batch the transcription's document asserts. Every claim
        cites `artifact`, the document's content key, by which the prompt
        names the document too, and the page number printed on the page it
        cites, where the transcription's record gives one (cite_artifact).
        The prompt carries the transcription's text with each printed page
        number beside its page's marker (Transcription.prompt_text)."""
        property_keys, item_keys = vocabulary_prompt_lines()
        prompt = EXTRACTION_PROMPT.format(
            schema=claim_schema_prompt_text(),
            property_keys=property_keys,
            item_keys=item_keys,
            kinds=entity_kind_prompt_lines()[0],
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            text=transcription.prompt_text(),
            document_block=document_block(transcription, artifact),
        )
        printed_pages = transcription.printed_pages()
        placed = placed_by(transcription, artifact)
        try:
            return self._extract(prompt, artifact, printed_pages, placed)
        except _KeptAnswerRejected:
            return self._extract(
                prompt, artifact, printed_pages, placed, again=True
            )

    def _extract(
        self,
        prompt: str,
        artifact: ArtifactRef,
        printed_pages: Mapping[int, str],
        placed: Mapping[str, Collection[str]],
        again: bool = False,
    ) -> ClaimBatch:
        """The batch one answer gives, each claim citing `artifact` and
        the page number printed on its page, where `printed_pages` gives
        one (cite_artifact), validated with the artifacts the document
        places (`placed`, salvage_valid_claims). A rejected answer the
        client kept from an earlier call raises _KeptAnswerRejected,
        unless this is the call made `again` after one."""

        def reject_answer(response: str, reason: str) -> None:
            if reject_response(self.client, response, reason) and not again:
                raise _KeptAnswerRejected(reason)

        try:
            response = self.client.complete(prompt)
        except (OutputLimitReached, ClassifierRefused) as exc:
            raise ExtractionFailed(
                f"the engine gave no answer: {exc}"
            ) from exc
        cut_off = at_output_limit(self.client, response)
        if self.raw_dump is not None:
            # Engine output is expensive; persist it before anything that
            # can fail, so a parse/validation error is debuggable and the
            # response recoverable.
            self.raw_dump.parent.mkdir(parents=True, exist_ok=True)
            self.raw_dump.write_text(response)
        try:
            payload = _parse_response(response)
        except ExtractionFailed:
            reject_answer(response, "extraction: no claim JSON")
            raise
        finished = any(
            obj.get("done") is True for obj in scan_json_objects(response)
        )
        batch, rejects = salvage_valid_claims(
            cite_artifact(payload.get("claims", []), artifact, printed_pages),
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
            placed=placed,
        )
        if self.rejects_dump is not None:
            if rejects:
                self.rejects_dump.parent.mkdir(parents=True, exist_ok=True)
                self.rejects_dump.write_text(json.dumps(rejects, indent=2))
            else:
                self.rejects_dump.unlink(missing_ok=True)  # no stale rejects
        if not batch.claims:
            reject_answer(response, "extraction: no valid claim")
            if not rejects:
                raise ExtractionFailed("the answer holds no claims")
            where = (
                f"; reasons in {self.rejects_dump}"
                if self.rejects_dump is not None
                else ""
            )
            raise ExtractionFailed(
                f"every extracted claim failed validation "
                f"({len(rejects)} rejects{where})"
            )
        if cut_off:
            reject_answer(response, f"extraction: {CUT_OFF_AT_LIMIT}")
        elif not finished:
            reject_answer(response, "extraction: no terminal sentinel")
        return batch


class _KeptAnswerRejected(Exception):
    """An answer the client kept from an earlier call was rejected: the
    engine has not answered the call yet, and is asked once more."""


class ClaimFileExtractor:
    """Validate a prepared claims file into a batch (manual/offline flows).

    The file holds either a full ClaimBatch or a bare {"claims": [...]}
    object; in the latter case run metadata is generated here. Its claims
    cite their artifacts themselves, by content key, and are validated as
    they are, with the artifacts the transcription's pages place taken as
    placed by the transcription's artifact (placed_by): `extract` takes
    the same arguments as the other extractors and uses only that. A
    file that cannot be read, is not JSON or not a JSON object, or fails
    schema or manifest validation raises ExtractionFailed.
    """

    def __init__(
        self, claims_file: Path, model_identifier: str = "manual"
    ) -> None:
        self.claims_file = claims_file
        self.model_identifier = model_identifier

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        try:
            payload = json.loads(self.claims_file.read_text())
        except OSError as exc:
            raise ExtractionFailed(
                f"cannot read the claims file {self.claims_file}: "
                f"{exc.strerror or exc}"
            ) from exc
        except ValueError as exc:  # not UTF-8, or not JSON
            raise ExtractionFailed(
                f"the claims file {self.claims_file} is not JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ExtractionFailed(
                f"the claims file {self.claims_file} is not a JSON object"
            )
        if "extraction_run" not in payload:
            payload = {
                "extraction_run": run_metadata(
                    self.model_identifier
                ).model_dump(mode="json"),
                "claims": payload.get("claims", payload),
            }
        try:
            batch = ClaimBatch.model_validate(payload)
        except ValidationError as exc:
            raise ExtractionFailed(
                f"claims file failed schema validation: {exc}"
            ) from exc
        problems = validate_against_manifest(
            batch, placed=placed_by(transcription, transcription.artifact)
        )
        if problems:
            raise ExtractionFailed(
                "claims file failed manifest validation:\n  "
                + "\n  ".join(problems)
            )
        return batch
