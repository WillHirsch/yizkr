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
    VITAL_KIND,
    ArtifactRef,
    ClaimBatch,
    ExtractionRunMetadata,
    artifact_kind_problems,
    load_vocabulary,
    placed_artifacts,
    reject_reasons,
    validate_against_manifest,
    vital_problems,
)
from yizkr.model.schema import load_acceptance, load_manifest
from yizkr.pipeline.interpretation_context import InterpretationContext
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    ClassifierRefused,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)
from yizkr.pipeline.prompts import load_prompt
from yizkr.pipeline.source_context import (
    bind_source_support,
    placed_owners,
    placement_lines,
    primary_evidence_reject,
    support_failure,
)
from yizkr.pipeline.transcribe import DECK_ENGINE, Transcription

EXTRACTION_PROMPT = load_prompt("extraction.md")


# The paragraph on what a document is, in both extraction prompts
# (ADR-0035 §3.4, ADR-0039 §2.2, §10): the document-subject properties,
# each given about the document itself only where it states it of itself,
# and about an image it places or a document it mentions only where its
# passage conveys it, with that passage as the excerpt.
DOCUMENT_ITSELF = load_prompt("document-itself.md").rstrip("\n")

# The paragraph on what the document places (ADR-0039 §3, §10.5), in both
# extraction prompts: the images its pages or slides place, each by its
# artifact reference and its place, or that it places none.
PLACED_ARTIFACTS = load_prompt("placed-artifacts.md").rstrip("\n")

NO_PLACED_ARTIFACTS = load_prompt("no-placed-artifacts.md").rstrip("\n")


def placed_lines(transcription: Transcription) -> list[str]:
    """Bound source owners and original occurrence locators for the prompt.

    Standalone readings without source context retain their image markers.
    """
    if getattr(transcription, "source_context", None) is not None:
        return placement_lines(transcription)
    unit = "slide" if transcription.engine == DECK_ENGINE else "page"
    return [
        f'  {{"kind": "artifact", "sha1": "{sha1}"}}: {unit} {page.page}, '
        f"image {place}"
        for page in transcription.pages or ()
        for place, sha1 in enumerate(page.images, start=1)
    ]


def document_block(transcription: Transcription, artifact: ArtifactRef) -> str:
    """The document and its placed sources, without other sources' claims.

    Located context supplies original occurrences and canonical media owners;
    standalone compatibility readings supply their existing page markers.
    """
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
    """Owners the current source context permits as placed claim subjects."""
    owners = (
        placed_owners(transcription)
        if getattr(transcription, "source_context", None) is not None
        else placed_artifacts(transcription.pages)
    )
    return {artifact.sha1: owners}


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
# extraction-run reference), `row_approval` (a curator approved the
# claim on its own review-sheet row) and `confirmed_from` (the local
# subject a curator's confirmation replaced). The JSON Schema the prompts
# carry shows `curated` with the rest of a claim.
REVIEW_STATE = ("curated", "row_approval", "confirmed_from")
# The claim fields the pipeline sets from a document reading it has
# checked, never the model: `circumstance_source`, where the document
# states the circumstance of its making that a marked claim's qualifier
# records (yizkr.pipeline.preparation, ADR-0008).
PIPELINE_STATE = ("circumstance_source",)


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
    (ADR-0003); the fields the pipeline sets (PIPELINE_STATE) are
    dropped, so no answer cites a passage of its own choosing as a
    marker's evidence; and claims that are then the same are kept once, in the
    order first given.

    An extractor reads one document's transcription, so every claim it
    gives is an assertion of that document, and the extractor, handed the
    document's key, names the source itself: no claim cites another
    artifact through a key the model copied wrongly, or through a form of
    reference the claim contract does not have. Located source support is
    assigned separately from verified reading context; supplied support is
    discarded here. A claim whose source is
    not an object keeps it, for validation to reject, as a claim without
    its source is."""
    printed = printed_pages or {}
    cited: list = []
    for claim in claims:
        if isinstance(claim, dict):
            claim = {
                key: value
                for key, value in claim.items()
                if key not in REVIEW_STATE and key not in PIPELINE_STATE
            }
            if isinstance(claim.get("source"), dict):
                source = {
                    key: value
                    for key, value in claim["source"].items()
                    if key not in {"printed_page_number", "support"}
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


def language_on_source(claim: object) -> bool:
    """Whether `claim` gives its excerpt's language on `source` instead:
    a `language` of non-blank text beside a `transcription_excerpt`
    object that gives none (no key, null or the empty string).
    _normalize_claim_shape moves it into the excerpt, and an extraction's
    coverage counts the claims it moved (`excerpt_language_moved`). A
    `language` on a source whose excerpt gives any other value (its own
    code, or an invalid one such as false, 0 or a list), on a source with
    no excerpt object, or one that is blank or no text, is left where it
    is, and the claim fails validation as written."""
    if not isinstance(claim, dict):
        return False
    source = claim.get("source")
    if not isinstance(source, dict):
        return False
    excerpt = source.get("transcription_excerpt")
    language = source.get("language")
    return (
        isinstance(language, str)
        and bool(language.strip())
        and isinstance(excerpt, dict)
        and excerpt.get("language") in (None, "")
    )


def _normalize_claim_shape(claim: dict) -> dict:
    """Repair mechanical output-shape slips models actually make.

    Item values nest under "value" ({"type": "item", "value": {...}}),
    and models sometimes wrap time values the same way:
    {"type": "time", "value": {"year": ...}} instead of the flat
    {"type": "time", "year": ...} the schema requires. The content is
    unambiguous, so flatten rather than reject 10 minutes of output.

    A model that closes the excerpt one key early gives the excerpt's
    language on `source` ({"transcription_excerpt": {"text": ...},
    "language": "de"}); the language is moved into the excerpt that
    gives none (language_on_source). The value is the model's own,
    moved, never guessed or defaulted.
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
    if language_on_source(fixed):
        source = dict(fixed["source"])
        source["transcription_excerpt"] = {
            **source["transcription_excerpt"],
            "language": source.pop("language"),
        }
        fixed["source"] = source
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
    dropped: Mapping[str, str] | None = None,
    primary_transcription: Transcription | None = None,
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
    (validate_against_manifest): a `caption` or document-subject claim
    whose subject is an artifact other than its source and none of its
    source's is a reject (ADR-0039 §2.1, §10.1). A claim whose local ref
    is of an artifact kind, or a claim of `instance_of` or `subclass_of`
    whose value is one, is a reject too, and an entity manifest that
    gives one is refused: an extraction may not name an artifact kind
    (claims.artifact_kind_problems, ADR-0039 §10.1(b)).

    `dropped` names, by id, the entities the manifest's parse dropped for
    a kind no extraction makes (`birth`, `death`), with that kind: a claim
    whose local ref names one is a reject under VITAL_KIND (ADR-0040 §7),
    as is one whose ref is of such a kind by its `instance_of`. The rules
    that read the whole batch (claims.vital_problems: a union
    `ended_by_death_of` someone the batch calls widowed, or someone who is
    neither of the couple it names) are applied to the claims that pass
    one by one, and a claim they refuse is a reject too.

    Each reject carries its reason codes (`reasons`, claims.REJECT_REASONS),
    for a report to count without quoting it: `schema` for a claim that
    does not parse, else the manifest problems' codes (reject_reasons).

    With interpretation context, `primary_transcription` requires primary
    evidence only for otherwise valid proposals. A malformed placeholder is
    an ordinary reject, not an unresolved attribution. Check the original
    quotation before mechanical excerpt clipping changes its text.
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
        ) + artifact_kind_problems(manifest_only, acceptance)
        if manifest_problems:
            raise ValueError(
                "entity manifest failed validation:\n  "
                + "\n  ".join(manifest_problems)
            )
    valid: list = []
    rejects: list[dict] = []
    dropped = dropped or {}
    for claim in claims:
        original = claim
        if isinstance(claim, dict):
            claim = _normalize_claim_shape(claim)
        named = sorted(
            {local_id for local_id in _local_ids(claim) if local_id in dropped}
        )
        if named:
            rejects.append(
                {
                    "claim": claim,
                    "problems": [
                        f"local id {local_id!r} names a "
                        f"{dropped[local_id]!r} entity, which the entity "
                        f"manifest drops: a source's {dropped[local_id]} is "
                        f"statements on the person (ADR-0040 §7) "
                        f"[{VITAL_KIND}]"
                        for local_id in named
                    ],
                    "reasons": [VITAL_KIND],
                }
            )
            continue
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
        ) + artifact_kind_problems(batch, acceptance)
        if problems:
            rejects.append(
                {
                    "claim": claim,
                    "problems": problems,
                    "reasons": reject_reasons(problems),
                }
            )
        else:
            valid.append((claim, batch.claims[0], original))
    passed = ClaimBatch.model_validate(
        {
            "extraction_run": extraction_run,
            "claims": [parsed for _, parsed, _ in valid],
            "entities": entities,
        }
    )
    refused = vital_problems(passed, acceptance)
    rejects += [
        {
            "claim": valid[index][0],
            "problems": [f"claim{problem}" for problem in problems],
            "reasons": reject_reasons(problems),
        }
        for index, problems in refused.items()
    ]
    accepted = []
    for index, (claim, _, original) in enumerate(valid):
        if index in refused:
            continue
        rejection = (
            primary_evidence_reject(original, primary_transcription)
            if primary_transcription is not None
            else None
        )
        if rejection is not None:
            rejects.append(rejection)
        else:
            accepted.append(claim)
    return (
        ClaimBatch.model_validate(
            {
                "extraction_run": extraction_run,
                "claims": accepted,
                "entities": entities,
            }
        ),
        rejects,
    )


def _local_ids(value: object) -> list[str]:
    """The local ids a raw claim names, wherever in it a local ref stands
    (its subject, its value, its qualifiers' values)."""
    if isinstance(value, dict):
        found = (
            [value["local_id"]]
            if value.get("kind") == "local"
            and isinstance(value.get("local_id"), str)
            else []
        )
        for item in value.values():
            found += _local_ids(item)
        return found
    if isinstance(value, list):
        return [found for item in value for found in _local_ids(item)]
    return []


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
    (`ManifestProperty.in_prompts`), and so is an item it holds back
    (`ManifestItem.offered`).
    """
    manifest = load_manifest()
    property_lines = "\n".join(
        f"  {key}: {spec.datatype} — {_one_line(spec.description)}"
        for key, spec in sorted(manifest.properties.items())
        if spec.in_prompts
    )
    item_lines = "  " + ", ".join(
        sorted(key for key, spec in manifest.items.items() if spec.offered)
    )
    return property_lines, item_lines


@lru_cache(maxsize=1)
def entity_kind_prompt_lines() -> tuple[str, str]:
    """Render the entity kinds for the manifest pass (ADR-0007).

    Returns (bare comma-joined keys, described block). The kinds are the
    manifest items flagged `entity_kind`, less those no extraction makes
    (`extracted` false: `birth`, `death`, ADR-0040 §7), so the offerable
    set is declared in one place rather than restated in the prompt.
    """
    kinds = sorted(
        (
            (key, spec)
            for key, spec in load_manifest().items.items()
            if spec.entity_kind and spec.extracted
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
        context: InterpretationContext | None = None,
    ) -> None:
        self.context = context
        self.client = client
        self.raw_dump = raw_dump
        self.rejects_dump = rejects_dump
        self.incomplete_reasons: list[str] = []

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        """Proposed claims with source identity and located support assigned
        from the reading. The prompt names `artifact`; verified source context
        resolves supporting parts and owners independently of model fields.
        Printed page labels come from the reading record (cite_artifact).
        The prompt carries the transcription's text with each printed page
        number beside its page's marker (Transcription.prompt_text)."""
        self.incomplete_reasons = []
        if self.context is not None:
            self.context.validate_target(transcription)
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
        if self.context is not None and self.context.has_context:
            prompt += self.context.prompt_block()
        printed_pages = transcription.printed_pages()
        placed = placed_by(transcription, artifact)
        try:
            return self._extract(
                prompt,
                artifact,
                printed_pages,
                placed,
                transcription=transcription,
            )
        except _KeptAnswerRejected:
            return self._extract(
                prompt,
                artifact,
                printed_pages,
                placed,
                again=True,
                transcription=transcription,
            )

    def _extract(
        self,
        prompt: str,
        artifact: ArtifactRef,
        printed_pages: Mapping[int, str],
        placed: Mapping[str, Collection[str]],
        again: bool = False,
        transcription: Transcription | None = None,
    ) -> ClaimBatch:
        """The batch one answer gives, each claim citing `artifact` and
        the page number printed on its page, where `printed_pages` gives
        one (cite_artifact), validated with the artifacts the document
        places (`placed`, salvage_valid_claims). A rejected answer the
        client kept from an earlier call raises _KeptAnswerRejected,
        unless this is the call made `again` after one."""

        self.incomplete_reasons = []

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
        attributed, attribution_rejects = bind_source_support(
            cite_artifact(payload.get("claims", []), artifact, printed_pages),
            transcription,
            allow_literal_quote=(
                self.context is not None and self.context.has_context
            ),
        )
        batch, rejects = salvage_valid_claims(
            attributed,
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
            placed=placed,
            primary_transcription=(
                transcription
                if self.context is not None and self.context.has_context
                else None
            ),
        )
        rejects = attribution_rejects + rejects
        if reason := support_failure(rejects):
            self.incomplete_reasons.append(reason)
        if self.rejects_dump is not None:
            if rejects:
                self.rejects_dump.parent.mkdir(parents=True, exist_ok=True)
                self.rejects_dump.write_text(json.dumps(rejects, indent=2))
            else:
                self.rejects_dump.unlink(missing_ok=True)  # no stale rejects
        if not batch.claims and not (
            self.context is not None
            and self.context.has_context
            and support_failure(rejects)
        ):
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
            reason = f"extraction: {CUT_OFF_AT_LIMIT}"
            self.incomplete_reasons.append(reason)
            reject_answer(response, reason)
        elif not finished:
            reason = "extraction: no terminal sentinel"
            self.incomplete_reasons.append(reason)
            reject_answer(response, reason)
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
        self,
        claims_file: Path,
        model_identifier: str = "manual",
        *,
        context: InterpretationContext | None = None,
    ) -> None:
        self.context = context
        self.claims_file = claims_file
        self.model_identifier = model_identifier

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        if self.context is not None:
            self.context.validate_target(transcription)
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
        if getattr(transcription, "source_context", None) is not None or (
            self.context is not None and self.context.has_context
        ):
            attributed, rejected = bind_source_support(
                [claim.model_dump(mode="json") for claim in batch.claims],
                transcription,
                allow_literal_quote=(
                    self.context is not None and self.context.has_context
                ),
            )
            if rejected:
                raise ExtractionFailed(support_failure(rejected))
            batch = ClaimBatch.model_validate(
                {**batch.model_dump(mode="json"), "claims": attributed}
            )
        problems = validate_against_manifest(
            batch, placed=placed_by(transcription, transcription.artifact)
        )
        if problems:
            raise ExtractionFailed(
                "claims file failed manifest validation:\n  "
                + "\n  ".join(problems)
            )
        if self.context is not None and self.context.has_context:
            rejected = [
                rejection
                for claim in batch.claims
                if (
                    rejection := primary_evidence_reject(
                        claim.model_dump(mode="json"), transcription
                    )
                )
                is not None
            ]
            if rejected:
                raise ExtractionFailed(support_failure(rejected))
        return batch
