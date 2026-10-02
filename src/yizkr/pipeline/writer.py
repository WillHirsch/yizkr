"""Writer step: approved ClaimBatch -> Wikibase statements (ADR-0003).

Before anything else, the writer refuses a batch that holds a `depicts`
claim no curator approved on its own review-sheet row (ADR-0005): each
depicts claim must carry the row approval `apply_review` records on it,
for the claim and the entity-manifest records its local refs resolve
through as they now read, and no other ref in the batch may change the
item one of its unrecorded local refs becomes (`unapproved_depicts` in
yizkr.pipeline.review). The refusal (UnapprovedDepictsError) names
every such claim by its review-sheet id, and comes before `write_batch`
reads from or writes to the instance, in a dry run too. A batch without
depicts claims is not affected.

Per batch, the writer:

0. resolves every artifact the batch cites, by the SHA-1 of its media,
   to the artifact item carrying that `media SHA-1` (ADR-0010). This
   happens before anything is written: a batch that cites media no item
   carries is refused whole, since its statements would have no source
   to reference. A claim whose subject is its own source document (an
   `artifact` subject, such as an `issued_by` claim; ADR-0035 §3.4) is
   written on that artifact item. A document subject that names any
   other document, or sits on a property the manifest does not mark
   `document_subject`, and a claim of such a property whose subject is
   anything but its own source document, are refused before the first
   write, as a claim whose text the instance cannot hold is (below);
1. mints an extraction-run item from the batch metadata (idempotent:
   matched by a deterministic label containing the batch content hash),
   with its run details in the label/description — items describe
   themselves; statements stay reserved for source-attributed claims.
   Its one statement, `instance of -> extraction run`, cites the run
   item itself, the document of its own execution, so it goes in a
   write after the one that makes the item. A run item found by its
   label without that statement, as a write cut short between the two
   leaves it, gains it then; one that states it already, at any rank
   and under any reference, is left as it is (`ensure_self_typed` in
   yizkr.pipeline.registrar). A dry run writes neither;
2. creates items for unresolved `local` subjects/values (reusing an
   existing item on exact label match, so re-runs never duplicate
   people: the label is matched and written in the form the instance
   keeps labels in, whatever form the batch gives it in: trimmed,
   cleaned up, in Unicode NFC, and cut to the characters the instance
   holds in a label, `_term_text`),
   writing each minted item's `instance of`, and an
   unclassified entity's `type hint`, with two references: stated in ->
   the artifact that mentions the entity, and inferred from extraction
   run -> the run item. An entity's kind is the extraction's proposal,
   a machine write like any claim (ADR-0003). A local ref resolves
   through the batch's entity-manifest record of its id when there is
   one (ADR-0007, read through `entity_records` in yizkr.pipeline.review,
   as the depicts gate reads it): the record gives the label, the
   summary as description, the kind, type hint and aliases; otherwise
   the ref gives its own label, description and kind. A minted item
   whose description would be its label gets none, with a warning, as
   Wikibase refuses an item whose label and description are the same.
   A ref, or its record, whose label holds nothing the instance keeps
   raises ValueError, as one with no label does. The document's
   aliases go onto the item it names, minted or reused, append-only;
   those of an entity an approved equivalence names go on before any
   claim is written. A kind the vocabulary lacks is recorded for no
   item, minted or found: the result's `warnings` name each entity that
   gives one, found before the first write;
3. writes each claim as a statement at NORMAL rank (the contract has
   no rank field — ADR-0002) with TWO references (ADR-0003):
   * stated in -> artifact, with page number / region / excerpt
     qualifiers inside the reference;
   * inferred from extraction run -> the run item;
4. when a statement with the same property, value, AND qualifiers
   already exists, the claim is recorded on that statement rather than
   beside it: an agreeing second source is knowledge, and so is each
   place where one source asserts the value (R2 records who asserts a
   fact, and where, not just the fact). The claim is compared as the
   writer sends it, with every text in it in Unicode NFC, the form the
   instance keeps text in and gives it back in (`_stored_string`): its
   string, external-id and monolingual text values and qualifiers, and
   its source block's page number, region and excerpt, which also go on
   one line and cleaned up as the instance gives them back
   (`_locator_text`). So a claim whose
   text the batch gives in another form, such as the decomposed form
   (NFD), finds the statement written for it. The claim's source block,
   `stated in` with its page number, region and excerpt, is compared
   whole with the statement's reference blocks as the instance stores
   them:
   * the statement already carries that block: the claim is skipped
     (safe re-run);
   * otherwise the claim's blocks are APPENDED to the statement, each
     only if the statement does not already carry it, so a second claim
     of the same run adds its source block but not the run's block
     again. The same artifact under another page, region or excerpt
     thus adds a block of its own beside the first, as another artifact
     does. No block the statement carries is changed or removed.
   A curated statement, one with no extraction-run reference (a
   curator's own write, until a machine claim's blocks join it) or
   whose rank a curator changed, is compared by artifact alone: the
   claim is skipped when any of the statement's blocks cites the
   claim's artifact, under whatever locator, and otherwise its blocks
   are appended as above. Same value but different qualifiers is a
   different assertion and coexists as its own statement (ADR-0002);
5. after the claims, records what the batch says of every item that
   existed before it and that one of its claims mentions: the kind and
   type hint of the manifest record that names the item (by an approved
   equivalence, by exact label here, or by the resolve step's exact-label
   match), and the kind a local ref without a record gives on its
   `instance_of` when the writer matched it by exact label (the kind an
   item minted from it would take). Such a ref's kind is recorded too on
   an item the batch minted from an earlier ref under another local id,
   as each chunk or page of a document can name an entity by an id of
   its own: the item holds the kind it was minted with, and each later
   ref that found it by label witnesses its own, so a second write of
   the batch adds nothing. An item the resolve step matched gains the
   record's aliases here too, as the others have by now. A local ref the
   resolve step matched reaches the writer as the item's bare QID:
   without a record, it brings no kind. A record's kind and type hint
   cite the artifact of the first claim that mentions the item, as a
   minted item's do. A ref's kind cites the artifact of the claim the
   ref is in (the writer reads the first ref to each local id, as it
   does for the label), so each such artifact is a witness of the kind.
   A kind or type hint is compared with the item's unqualified
   statements on the same property (a type hint as free text, ignoring
   case, Unicode form and spacing), so a qualified statement never
   matches:
   * a matching statement already referenced to that artifact: nothing
     to add (safe re-run);
   * a matching machine statement at normal rank: it gains this
     source's two reference blocks, each only if not already there;
   * otherwise (a different value, or a same value held only by curated
     statements): a new statement at normal rank with both references,
     coexisting with the others (ADR-0002).
   A curated statement (one with no extraction-run reference, or whose
   rank a curator changed) is never altered. The claims come first so
   that an explicit `instance of` claim keeps its own locator and
   excerpt.

Wikibase holds at most TEXT_LIMIT (400) characters in the text of a
string, external-id or monolingual text value, whether the value is a
statement's, a qualifier's or a reference's, and no text value that
holds a line break or a tab, or has whitespace at either end
(LINE_BREAKS, WHITESPACE). It cleans a text up (CLEANED_UP): each C0
control character but the tab, the line feed and the carriage return,
and each of the noncharacters U+FFFE and U+FFFF, becomes U+FFFD, in a
string value before it checks and stores it, and in every text its API
gives back, monolingual text included. So it changes a vertical tab or a
form feed in a string value into U+FFFD rather than refusing it. It
trims a label, a description or an alias, cleans it up and keeps it in a
form of its own (`stored_term`), in at most TERM_LIMIT (250)
characters, and refuses an item whose label and description are the
same. It refuses the whole edit that sends a text it does not hold. The
writer checks each text in the form the instance keeps it in, and sends
none that the instance would refuse or give back changed, so that no
text's length or shape stops the batch and a re-run finds what the
first run wrote, with one exception: a local ref whose label holds
nothing the instance keeps raises ValueError when the writer comes to
mint its item, as one with no label does (step 2), after the claims
before it are written:

* a claim whose own text, its value or a qualifier's, is over the limit,
  or holds a line break or a tab, or has whitespace at either end, or
  holds a character the instance cleans up, is refused before the first
  write, and nothing of it is written: no statement, and no item that
  only it mentions. That holds for a vertical tab or a form feed in a
  string value too, which the instance would change. The result's
  `refused` names it by its claim id (`claim_id` in
  yizkr.pipeline.review), its review-sheet id unless the review
  corrected it, and a warning says what keeps each of its texts out,
  quoting none of them. The text is never cut or changed, as that would
  alter what the source says (CLAUDE.md rule 5). Every other claim of
  the batch is written as the steps above say, with its references
  (rules 1 and 2). An entity's type hint is free text that the writer
  compares ignoring its spacing (step 5), so it goes with each run of
  whitespace in it made one space, and each character the instance
  cleans up made U+FFFD, as the instance would give it back
  (`_hint_text`), and one still over the limit is left out in the same
  way, with a warning, wherever the writer would record it;
* a reference's page number and region only place the claim in its
  source, as its excerpt only helps a reader check it there, so each is
  put on one line, cleaned up and cut to the limit rather than refused
  (`_locator_text`): whitespace at either end goes, a line break inside
  becomes " | ", visibly, a tab a space, and each other character the
  instance cleans up U+FFFD, as the instance would give it back. A text
  still over the limit is cut at a break between words and ends with
  CUT_MARK (" …"), "sha1" and the first 12 hex digits of the SHA-1 of
  the whole text (`_cut`). A locator or excerpt with nothing left is
  left out. Each depends on the text alone, so a re-run builds the block
  the instance keeps, and is skipped (step 4). The digest keeps two page
  numbers, regions or excerpts that agree up to the cut apart, so the
  two places a source asserts a value in stay two reference blocks;
* a label, a description or an alias the writer makes goes in the form
  the instance keeps a term in (`stored_term`: trimmed, cleaned up and
  in NFC, in that order, as Wikibase's trimToNFC does it), and one still
  over TERM_LIMIT once in that form is cut in the same way, with its
  digest (`_term_text`), so whitespace around a term never counts
  towards the limit. A label is how the writer finds an item again: a
  label given in another form or at length finds the item written for
  it, and two labels that agree up to the cut still name two items. The
  run item's label, which holds the model's identifier, is made the same
  way. The registrar compares and sets an artifact's title by the same
  rule (yizkr.pipeline.registrar), so a title and a label that the
  instance keeps alike are alike to both.

Nothing here auto-approves: the writer expects the output of
`apply_review` and trusts the human decisions that produced it, except
for depicts, whose per-row approval it checks, as above.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    CoordinateValue,
    EntityRecord,
    EntityRef,
    ExternalIdValue,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    StringValue,
    TimeValue,
    VocabRef,
)
from yizkr.core.wiki import WikiClient
from yizkr.model.schema import load_acceptance
from yizkr.pipeline.review import (
    claim_id,
    entity_records,
    unapproved_depicts,
)

# The most characters (Unicode code points) Wikibase holds in the text of a
# string, external-id or monolingual text value: its `string-limits` for
# the string and the monolingual text value types, an external id being a
# string value. It checks every snak of a statement it is sent, the value,
# each qualifier and each reference, after it has put a string value in
# NFC and cleaned it up (CLEANED_UP), and refuses the whole edit when one
# is longer.
TEXT_LIMIT = 400
# the most characters Wikibase holds in a term, a label, a description or
# an alias (its `multilang` string limit), counted once it has trimmed the
# term (stored_term)
TERM_LIMIT = 250
# what a text cut to its limit ends with, before the digest of the whole
# text (_cut)
CUT_MARK = " …"
# What else Wikibase refuses in a text value, and in a term once it has
# trimmed it: its `illegal-string-chars` check, /^\s|[\v\t]|\s$/u, finds a
# line break or a tab anywhere, or whitespace at either end. PHP matches
# `\v` and `\s` by Unicode property (PCRE2 with UCP): `\v` is LINE_BREAKS,
# and `\s`, the union of `\p{Z}`, `\h` and `\v`, is WHITESPACE: the line
# breaks, the tab, the space separators, and U+180E, which `\h` holds
# though Unicode has made it a format character (Cf). Python's
# str.isspace() differs, counting U+001C to U+001F and not U+180E. Before
# the check, Wikibase cleans a string value up (cleanupToNFC, CLEANED_UP),
# making a vertical tab or a form feed in it U+FFFD, so it refuses those
# two in monolingual text alone; the writer counts them as line breaks in
# every text value, as the instance either refuses or changes them.
LINE_BREAKS = "".join(map(chr, (0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029)))
WHITESPACE = LINE_BREAKS + "".join(
    map(
        chr,
        (0x09, 0x20, 0xA0, 0x1680, 0x180E, *range(0x2000, 0x200B))
        + (0x202F, 0x205F, 0x3000),
    )
)
# a line break in a text value, a CR LF pair being one (_one_line), and a
# run of whitespace (_hint_text)
_LINE_BREAK = re.compile("\r\n|[" + LINE_BREAKS + "]")
_WHITESPACE_RUN = re.compile("[" + WHITESPACE + "]+")
# How Wikibase trims a term (StringNormalizer::trimToNFC, whose
# trimWhitespace comes before its cleanupToNFC): each run of control
# characters (Unicode Cc, a line break or a tab among them) becomes one
# space, and the separators (Unicode Z) at either end go. It refuses a term
# that still holds a line or paragraph separator (U+2028, U+2029), which
# are separators but no control characters, or that begins or ends with a
# Mongolian vowel separator (U+180E): the writer makes each of the two a
# space as well, and trims the third with the separators.
_TERM_CONTROLS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]+"
)
_TERM_EDGES = "".join(
    map(
        chr,
        (0x20, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029)
        + (0x202F, 0x205F, 0x3000, 0x180E),
    )
)
# The characters Wikibase makes U+FFFD, one for one, wherever it cleans a
# text up: UtfNormal's Validator::cleanUp (version 4.0.0, which MediaWiki
# pins), whose replaceForNativeNormalize replaces them before it puts the
# text in NFC. These are every C0 control character but the tab, the line
# feed and the carriage return, and the noncharacters U+FFFE and U+FFFF;
# DEL, the C1 control characters and the other noncharacters stay as they
# are. Wikibase cleans up a string or external-id value before it checks
# and stores it (StringNormalizer::cleanupToNFC), and a term once it has
# trimmed it (trimToNFC, after its trimWhitespace has made every control
# character a space, so only the two noncharacters are left to replace).
# It stores a monolingual text as it is sent, but its API cleans up every
# text it gives back (ApiResult, through Language::normalize), so that
# too reads back cleaned up.
CLEANED_UP = "".join(
    map(
        chr,
        (*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF),
    )
)
_CLEAN_UP = str.maketrans(
    dict.fromkeys(CLEANED_UP, "\N{REPLACEMENT CHARACTER}")
)
# punctuation that joins the letters or digits on its two sides into one
# word, which a cut keeps whole: a point or comma inside a number
# ("1.234", "1,5"), an apostrophe inside a word
INNER_PUNCTUATION = ".,'\u2019"


class UnapprovedDepictsError(ValueError):
    """A batch holds depicts claims that no curator approved on their own
    review-sheet row (ADR-0005)."""

    def __init__(self, claim_ids: list[str]) -> None:
        self.claim_ids = claim_ids
        named = ", ".join(f"claim {cid}" for cid in claim_ids)
        super().__init__(
            f"{len(claim_ids)} depicts claim(s) carry no per-row human "
            f"approval of the item they would depict: {named}. A depicts "
            f"claim is written only after a curator approves it on its own "
            f"row of a review sheet and apply-review records that approval "
            f"(ADR-0005). The record holds while the claim and the entity-"
            f"manifest records its local refs resolve through read as they "
            f"did then, and while no other ref in the batch to one of its "
            f"local ids without a record differs from its own. Export-review "
            f"this batch, decide these rows by hand, apply-review, and write "
            f"the batch apply-review writes. Nothing was written."
        )


class UnregisteredArtifactError(LookupError):
    """A batch cites media that no artifact item on the instance carries."""

    def __init__(self, sha1s: list[str]) -> None:
        self.sha1s = sha1s
        shown = ", ".join(sha1[:12] for sha1 in sha1s)
        super().__init__(
            f"{len(sha1s)} cited artifact(s) not registered on this "
            f"instance: no item carries media SHA-1 {shown}. Register "
            f"the media before writing claims that cite it."
        )


@dataclass
class WriteResult:
    run_qid: str
    written: int = 0
    skipped: int = 0
    # claims recorded on an existing equal statement as reference blocks
    # it gained: an agreeing second source, or the same source in another
    # place (step 4 of the module docstring)
    referenced: int = 0
    created_items: dict[str, str] = field(
        default_factory=dict
    )  # local_id -> QID
    # step 5 of the module docstring, on the `instance of` and `type hint`
    # statements of items a ref or record found: the statements written,
    # and each artifact whose references an existing statement gained (a
    # statement two artifacts witness counts twice)
    type_statements_written: int = 0
    type_statements_referenced: int = 0
    # what the writer left out and why, such as a kind the vocabulary
    # lacks (step 2 of the module docstring), a claim it refused, or the
    # description of an item it minted when that is the item's label
    warnings: list[str] = field(default_factory=list)
    # the claim id (yizkr.pipeline.review.claim_id; its review-sheet id
    # unless the review corrected it) of each claim refused whole because
    # the instance cannot hold its own text as it is (over TEXT_LIMIT,
    # with a line break, a tab or whitespace at either end, or with a
    # character it cleans up into U+FFFD), in batch order; a warning says
    # why for each (the module docstring)
    refused: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _FoundEntity:
    """What a batch says of an item one of its refs or records found
    rather than minted (step 5 of the module docstring): the kind, and
    type hint, its source gives, and the document's aliases when no
    earlier step has appended them.

    `artifact` is the artifact the kind is cited to: for a local ref's
    kind, that of the claim whose ref gives it. It is None for a manifest
    record's, which cites the first claim that mentions the item."""

    qid: str
    kind: str
    type_hint: str | None = None
    aliases: tuple[str, ...] = ()
    artifact: str | None = None


def batch_fingerprint(batch: ClaimBatch) -> str:
    canonical = json.dumps(batch.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:10]


def normalized_type_hint(text: str) -> str:
    """A type hint's comparison form: free text, so Unicode
    compatibility forms, letter case and runs of whitespace do not
    make two hints different."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _stored_string(text: str) -> str:
    """`text` in the form the instance keeps text in: Unicode NFC.
    Wikibase saves a string value (of a `string` or `external-id` snak)
    and an item's labels, descriptions and aliases in NFC, and its API
    gives every text back in NFC, monolingual text included.

    The writer sends the batch's text in this form: a claim's string,
    external-id and monolingual text values and qualifiers, an entity's
    type hint (_hint_text), and a reference block's page number, region
    and excerpt (_locator_text). A label, description or alias goes in the
    form the instance keeps a term in, which is in NFC too (_term_text).
    So what it builds for a claim is what the instance gives back for it,
    and the labels and aliases it looks for are in the form the instance
    holds them in, whatever form the batch gives them in: a re-run finds
    everything the first run wrote (steps 2 and 4 of the module
    docstring).

    NFC gives a canonically equivalent text, which Unicode defines as
    the same text: a letter and its combining mark become the one
    precomposed character where Unicode has one, and combining marks
    take their canonical order. A combining mark with no precomposed
    form stays a combining mark, and a compatibility character, such as
    the long s, stays as it is, where NFKC would replace it."""
    return unicodedata.normalize("NFC", text)


def _listed(things: list[str]) -> str:
    """`things` in prose: "a", "a and b", "a, b and c"."""
    if len(things) == 1:
        return things[0]
    return f"{', '.join(things[:-1])} and {things[-1]}"


def _faults(text: str) -> list[str]:
    """What keeps the instance from holding `text`, a text value in the
    form the writer sends it in (_stored_string), as it is: each fault a
    clause that reads after the words naming the text ("its value ...").
    None when the instance holds it: it has at most TEXT_LIMIT characters,
    holds no line break (LINE_BREAKS) or tab, has no whitespace
    (WHITESPACE) at either end, and holds no other character the instance
    cleans up into U+FFFD (CLEANED_UP; a vertical tab and a form feed are
    line breaks here)."""
    faults = []
    if len(text) > TEXT_LIMIT:
        faults.append(
            f"has {len(text)} characters in Unicode NFC, over the "
            f"{TEXT_LIMIT} Wikibase holds in a text value"
        )
    shapes = [
        shape
        for shape, found in (
            ("a line break", any(char in LINE_BREAKS for char in text)),
            ("a tab", "\t" in text),
            ("whitespace at its start", bool(text) and text[0] in WHITESPACE),
            ("whitespace at its end", bool(text) and text[-1] in WHITESPACE),
        )
        if found
    ]
    if shapes:
        faults.append(
            f"holds {_listed(shapes)}, which Wikibase refuses or changes in "
            f"a text value"
        )
    if any(char in CLEANED_UP and char not in LINE_BREAKS for char in text):
        faults.append(
            "holds a control character or a noncharacter, which Wikibase "
            "changes into U+FFFD"
        )
    return faults


def _hint_text(text: str) -> str:
    """An entity's type hint as the writer sends it: in the form the
    instance keeps text in (_stored_string), each run of whitespace in it
    (WHITESPACE, a line break or a tab among them) made one space, none at
    either end, and then cleaned up as the instance cleans up a string
    value (_cleaned_up). A type hint is free text, which the writer
    compares ignoring its spacing (normalized_type_hint), so its spacing
    says nothing the writer keeps, and its words go as they are. The hint
    the writer compares with an item's is the one it sends, so it is the
    one the instance gives back, and a re-run finds the statement written
    for it. Empty when the hint is whitespace alone."""
    return _cleaned_up(
        _WHITESPACE_RUN.sub(" ", _stored_string(text)).strip(" ")
    )


def _value_text(value) -> str | None:
    """The text of a string, external-id or monolingual text value in the
    form the writer sends it in (_stored_string); None for other values."""
    if isinstance(value, StringValue | ExternalIdValue):
        return _stored_string(value.value)
    if isinstance(value, MonolingualValue):
        return _stored_string(value.value.text)
    return None


def _is_mark(char: str) -> bool:
    """A combining mark (Unicode category M), which belongs to the
    character before it."""
    return unicodedata.category(char).startswith("M")


# the zero-width joiner, which joins the characters either side of it
# into one, and the regional indicators, two of which make one flag
_JOINER = "\u200d"
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)


# the characters that extend the letter before them, besides combining
# marks: the zero-width non-joiner and joiner, the emoji skin tones, the
# tag characters that spell a subdivision's flag, the halfwidth kana's
# sound marks, and the Hangul vowel and final jamo that NFC leaves apart
_EXTENDERS = (
    range(0x200C, 0x200E),
    range(0x1F3FB, 0x1F400),
    range(0xE0020, 0xE0080),
    range(0xFF9E, 0xFFA0),
    range(0x1160, 0x1200),
    range(0xD7B0, 0xD800),
)
# the canonical combining class of a virama, which joins the letter after
# it to the one before (an Indic conjunct)
_VIRAMA = 9
# an old Hangul syllable's leading jamo, which joins the leading jamo or
# the precomposed syllable after it into one syllable
_LEADING_JAMO = (range(0x1100, 0x1160), range(0xA960, 0xA980))
_SYLLABLES = range(0xAC00, 0xD7A4)


def extends(char: str, before: str) -> bool:
    """Whether `char`, coming right after `before`, belongs to the same
    letter, so that no cut falls between them: a combining mark
    (_is_mark), a character of _EXTENDERS, any character after a
    zero-width joiner, a letter after a virama, or a leading jamo or a
    precomposed Hangul syllable after a leading jamo (_LEADING_JAMO,
    _SYLLABLES). A narrower rule than
    Unicode's grapheme clusters (Unicode Standard Annex 29), which it
    follows for these sequences alone (ADR-0010). The writer's cuts and
    the registrar's keep to it."""
    code = ord(char)
    return (
        _is_mark(char)
        or any(code in extenders for extenders in _EXTENDERS)
        or before == _JOINER
        or (char.isalpha() and unicodedata.combining(before) == _VIRAMA)
        or (
            any(ord(before) in leading for leading in _LEADING_JAMO)
            and (
                code in _SYLLABLES
                or any(code in leading for leading in _LEADING_JAMO)
            )
        )
    )


def half_flag(text: str) -> bool:
    """Whether `text` ends in an odd run of regional indicators: the
    first half of a flag whose second half was cut off."""
    run = len(text) - len(text.rstrip("".join(map(chr, _REGIONAL_INDICATORS))))
    return run % 2 == 1


def _in_word(text: str, index: int, inner: str = INNER_PUNCTUATION) -> bool:
    """Whether text[index] belongs to a word: a letter, a digit or a
    combining mark, or `inner` punctuation with a letter, digit or mark
    before it and a letter or digit after it."""
    char = text[index]
    if char.isalnum() or _is_mark(char):
        return True
    return (
        char in inner
        and 0 < index < len(text) - 1
        and (text[index - 1].isalnum() or _is_mark(text[index - 1]))
        and text[index + 1].isalnum()
    )


def _joined(text: str, end: int, inner: str = INNER_PUNCTUATION) -> bool:
    """Whether a cut of `text` before text[end] would split what reads as
    one: a letter, from a character that extends it (extends) or half of
    a flag (half_flag), or a word, whose parts `inner` punctuation joins.
    With no `inner` punctuation, a word is a run of letters, digits and
    marks."""
    return (
        extends(text[end], text[end - 1])
        or half_flag(text[:end])
        or (_in_word(text, end - 1, inner) and _in_word(text, end, inner))
    )


def _one_line(text: str) -> str:
    """`text` in a shape the instance holds in a text value: with no
    whitespace at either end (WHITESPACE), each line break inside made
    " | " (_LINE_BREAK), so that where a line of the source ends stays
    visible, and each tab made a space. No other character changes, and
    a text of whitespace alone is left empty."""
    text = text.strip(WHITESPACE)
    return _LINE_BREAK.sub(" | ", text).replace("\t", " ")


def _locator_text(text: str) -> str:
    """A reference's page number, region or excerpt as the writer sends
    it: in the form the instance keeps text in (_stored_string), on one
    line (_one_line), cleaned up as the instance gives it back
    (_cleaned_up; a vertical tab or a form feed is a line break by then)
    and, when that is longer than TEXT_LIMIT, cut to it (_cut), with the
    digest of the text as the instance would keep it. It depends on the
    text alone, so a claim written again sends the text the instance
    keeps, and its reference block is found (step 4 of the module
    docstring). Empty when the text is whitespace alone."""
    return _cut(_cleaned_up(_one_line(_stored_string(text))), TEXT_LIMIT)


def _cleaned_up(text: str) -> str:
    """`text` as Wikibase cleans a text up: each character of CLEANED_UP
    made U+FFFD. It replaces one character by one, and a text in NFC
    stays in NFC: neither U+FFFD nor any character it replaces combines
    with a character beside it."""
    return text.translate(_CLEAN_UP)


def stored_term(text: str) -> str:
    """`text` in the form the instance keeps a label, a description or an
    alias in, reached as Wikibase's trimToNFC reaches it: first trimmed,
    each run of control characters and line or paragraph separators in it
    made one space, and the separators at either end dropped
    (_TERM_CONTROLS, _TERM_EDGES); then cleaned up (_cleaned_up), which
    leaves only U+FFFE and U+FFFF to make U+FFFD; and then in Unicode NFC.
    The writer finds, compares and sends every term in this form, and the
    registrar every title. Empty when the text holds nothing the instance
    keeps."""
    text = _TERM_CONTROLS.sub(" ", text).strip(_TERM_EDGES)
    return unicodedata.normalize("NFC", _cleaned_up(text))


def _term_text(text: str) -> str:
    """A label, description or alias as the writer sends it, and as it
    looks for a label: in the form the instance keeps a term in
    (stored_term) and, when that is longer than TERM_LIMIT, cut to it
    (_cut). So the term it sends is the term the instance keeps, and a
    label given in another form, or at any length, finds the item written
    for it."""
    return _cut(stored_term(text), TERM_LIMIT)


def _terms(texts: list[str]) -> list[str]:
    """`texts`, a list of aliases, as the writer sends it: each alias as
    it sends a term (_term_text), in their order, leaving out a repeat and
    an alias with nothing the instance keeps, which it refuses."""
    return [term for term in dict.fromkeys(map(_term_text, texts)) if term]


def _cut(text: str, limit: int) -> str:
    """`text`, in NFC, as it is when it has at most `limit` characters,
    and otherwise cut to the limit (the module docstring).

    A cut keeps the longest start of the text that ends at a break
    between words and leaves room for the mark, and the mark follows it:
    CUT_MARK, then "sha1" and the first 12 hex digits of the SHA-1 of the
    whole text. So no word is kept in part: a page "112" is never kept as
    "11", nor "1.234" as "1" (INNER_PUNCTUATION), and no letter is kept
    in part: none loses a combining mark or another character that
    extends it, and no flag is halved (extends, half_flag). When one word
    fills the room, as a list of pages with
    no spaces ("2,3,4") can, the cut falls at its last point, comma or
    apostrophe, so no number in it is kept in part either; and only when
    no such start holds anything but spaces, as when one run of letters
    and digits fills the room, is the text cut inside the run, each letter
    kept whole. The digest keeps two texts that agree up to the
    cut apart, and apart from a text that reads as the cut's start: the
    places a source asserts a value in stay a reference block each (step 4
    of the module docstring), and two labels that agree up to the cut name
    two items.

    The cut depends on the text alone, and keeps it in NFC: a start of a
    text in NFC is in NFC, and so is it with the mark after it."""
    if len(text) <= limit:
        return text
    digest = hashlib.sha1(text.encode()).hexdigest()[:12]
    mark = f"{CUT_MARK} sha1 {digest}"
    room = limit - len(mark)
    end = room
    while end and _joined(text, end):
        end -= 1
    kept = text[:end].rstrip()
    if not kept:
        # one word fills the room: its last point, comma or apostrophe
        end = room
        while end and _joined(text, end, inner=""):
            end -= 1
        kept = text[:end].rstrip()
    if not kept:
        # one run of letters and digits fills the room
        end = room
        while end and (
            extends(text[end], text[end - 1]) or half_flag(text[:end])
        ):
            end -= 1
        kept = text[:end] or text[:room]
    return kept + mark


class BatchWriter:
    def __init__(
        self,
        client: WikiClient,
        dry_run: bool = False,
        artifact_qids: Mapping[str, str] | None = None,
    ) -> None:
        """`artifact_qids` maps media SHA-1 to artifact QID for artifacts
        the caller has just registered. A cited SHA-1 it does not hold is
        looked up in the instance's `media SHA-1` index."""
        self.client = client
        self.dry_run = dry_run
        self.properties, self.items = client.vocabulary()
        self._entity_cache: dict[str, object] = {}
        # entity manifest of the batch being written (ADR-0007):
        # local_id -> EntityRecord; set per write_batch call
        self._entity_records: dict[str, EntityRecord] = {}
        # media SHA-1 -> artifact QID, for every artifact resolved so far
        self._artifact_qids: dict[str, str] = {
            sha1.lower(): qid for sha1, qid in (artifact_qids or {}).items()
        }
        # the instance's lag-free media SHA-1 index, read on first need
        self._media_sha1_index: dict[str, str] | None = None
        # per write_batch call: item QID -> artifact QID of the first
        # claim that mentions the item, the items the batch minted, and
        # item QID -> (kind, artifact QID) for each kind that a local ref
        # without a manifest record gives the item it was matched to by
        # label, on the artifact of the claim the ref is in
        self._mentions: dict[str, str] = {}
        self._minted: set[str] = set()
        self._ref_kinds: dict[str, list[tuple[str, str]]] = {}

    # -- artifact resolution (content-addressed sources) -------------------

    def resolve_artifacts(self, batch: ClaimBatch) -> dict[str, str]:
        """Media SHA-1 -> artifact QID for every artifact `batch` cites.

        Reads only. Raises UnregisteredArtifactError, naming every
        unresolved SHA-1, when any cited media has no artifact item.
        The index is read at most once per call: when a SHA-1 is neither
        supplied by the caller nor in the index read earlier, the index
        is read afresh, so media registered since then still resolves.
        """
        wanted = sorted({claim.source.artifact.sha1 for claim in batch.claims})
        cached = self._media_sha1_index or {}
        if any(
            sha1 not in self._artifact_qids and sha1 not in cached
            for sha1 in wanted
        ):
            self._media_sha1_index = self.client.string_statement_index(
                self.properties["media_sha1"]
            )
        index = self._media_sha1_index or {}
        for sha1 in wanted:
            if sha1 not in self._artifact_qids and sha1 in index:
                self._artifact_qids[sha1] = index[sha1]
        missing = [sha1 for sha1 in wanted if sha1 not in self._artifact_qids]
        if missing:
            raise UnregisteredArtifactError(missing)
        return {sha1: self._artifact_qids[sha1] for sha1 in wanted}

    def _source_artifact_qid(self, ref: ArtifactRef) -> str:
        return self._artifact_qids[ref.sha1]

    # -- reference resolution ---------------------------------------------

    def _resolve_ref(
        self,
        ref: EntityRef | ArtifactRef,
        result: WriteResult,
        source_artifact_qid: str | None,
    ) -> str:
        if isinstance(ref, VocabRef):
            return self.items[ref.key]
        if isinstance(ref, ArtifactRef):
            # a claim's own source document, resolved in step 0
            return self._artifact_qids[ref.sha1]
        if isinstance(ref, QidRef):
            qid = ref.qid
        elif isinstance(ref, LocalRef):
            qid = self._materialize_local(ref, result, source_artifact_qid)
        else:
            raise TypeError(f"unknown ref {ref!r}")
        if source_artifact_qid:
            # the source an existing item's manifest kind is attributed
            # to, as a minted item's is (step 5 of the module docstring)
            self._mentions.setdefault(qid, source_artifact_qid)
        return qid

    def _materialize_local(
        self,
        ref: LocalRef,
        result: WriteResult,
        source_artifact_qid: str | None,
    ) -> str:
        if ref.local_id in result.created_items:
            return result.created_items[ref.local_id]
        record = self._entity_records.get(ref.local_id)
        # the entity manifest (ADR-0007) is authoritative when present:
        # canonical label, kind, summary, and the document's aliases
        label = record.label if record else ref.label
        description = (record.summary or None) if record else ref.description
        instance_of = record.kind if record else ref.instance_of
        aliases = record.aliases if record else []
        if record and record.qid:
            # reviewed equivalence: attach to the existing item and
            # contribute this document's aliases to it
            self._ensure_aliases(record.qid, [record.label, *aliases])
            result.created_items[ref.local_id] = record.qid
            return record.qid
        # looked for, and written, in the form the instance keeps labels
        # in, so a label given in another form, or at length, finds the
        # item written for it (_term_text)
        label = _term_text(label or "")
        if not label:
            raise ValueError(
                f"local ref {ref.local_id!r} has no label, or none but "
                f"whitespace; cannot create an item for it (resolve it to "
                f"a QID or add a label)"
            )
        existing = self.client.find_item_by_label(label)
        if existing:
            if aliases:
                self._ensure_aliases(existing, aliases)
            if record is None and instance_of and source_artifact_qid:
                # a ref without a manifest record gives its kind itself, as
                # it does to an item minted from it, on the artifact of the
                # claim it is in: each such artifact is a witness of the
                # kind (step 5 records them)
                witness = (instance_of, source_artifact_qid)
                witnesses = self._ref_kinds.setdefault(existing, [])
                if witness not in witnesses:
                    witnesses.append(witness)
            result.created_items[ref.local_id] = existing
            return existing
        # the summary is a convenience, not an assertion: it goes as the
        # instance keeps a term, cut to what it holds (_term_text), and not
        # at all when that is the label, as Wikibase refuses an item whose
        # label and description are the same; a warning says so, in a dry
        # run too
        description = _term_text(description or "")
        if description == label:
            description = ""
            result.warnings.append(
                f"entity {ref.local_id!r} has a description that is its "
                f"label: its item is made without one, as Wikibase refuses "
                f"an item whose label and description are the same"
            )
        if self.dry_run:
            result.created_items[ref.local_id] = f"(new:{ref.local_id})"
            return result.created_items[ref.local_id]
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        if description:
            item.descriptions.set("en", description)
        names = _terms(aliases)
        if names:
            item.aliases.set("en", names)
        item = item.write(summary="ingest: entity proposed by extraction")
        qid = item.id
        self._minted.add(qid)
        # The kind and type hint come from the extraction, never from
        # review, so they carry the run reference even when the claim
        # that mentions the entity is a curated one (ADR-0003).
        if instance_of in self.items and source_artifact_qid:
            claim = self._item_claim(
                self.properties["instance_of"], self.items[instance_of]
            )
            claim.references = self._references(
                artifact_qid=source_artifact_qid, run_qid=result.run_qid
            )
            self._append(item, claim)
            item.write(summary="ingest: instance of")
        # open-schema escape hatch (ADR-0013): record what an unclassified
        # entity actually is, so the gap is queryable, not lost. It goes
        # with its spacing made single spaces and cleaned up as the
        # instance gives it back (_hint_text), and a hint the instance
        # cannot hold is left out, with the warning write_batch gave before
        # its first write
        type_hint = _hint_text(record.type_hint or "") if record else ""
        if type_hint and len(type_hint) <= TEXT_LIMIT and source_artifact_qid:
            from wikibaseintegrator.datatypes import String

            hint = String(
                prop_nr=self.properties["type_hint"], value=type_hint
            )
            hint.references = self._references(
                artifact_qid=source_artifact_qid, run_qid=result.run_qid
            )
            self._append(item, hint)
            item.write(summary="ingest: type hint (unclassified entity)")
        result.created_items[ref.local_id] = qid
        return qid

    def _ensure_aliases(self, qid: str, aliases: list[str]) -> None:
        """Append this document's name variants to an existing item.

        Idempotent and append-only (MODELING.md: name variants live in
        aliases; matching quality compounds per ingested document). The
        item's own label never counts as a missing alias.

        The document's aliases are compared with the item's label and
        aliases, and sent, in the form the instance keeps them in
        (_term_text): an alias the item holds, or its label, given in
        another form or at length is not missing, and an alias with
        nothing the instance keeps is left out.

        The item is read afresh, and the writer's later edits to it start
        from that read, or from a read after the aliases are written, so
        none of them sends the aliases as they stood before: an edit sends
        the item's aliases whole.
        """
        if self.dry_run or not aliases:
            return
        item = self.client.wbi.item.get(qid)
        self._entity_cache[qid] = item
        label = str(item.labels.get("en") or "")
        existing = {str(alias) for alias in (item.aliases.get("en") or [])}
        missing = [
            alias
            for alias in _terms(aliases)
            if alias != label and alias not in existing
        ]
        if not missing:
            return
        item.aliases.set("en", sorted(existing | set(missing)))
        item.write(summary="ingest: aliases from document entity manifest")
        self._entity_cache[qid] = self.client.wbi.item.get(qid)

    # -- WBI datavalue construction -----------------------------------------

    @staticmethod
    def _append(entity, claim, beside_equal: bool = False) -> None:
        """Add a claim WITHOUT touching existing ones.

        WikibaseIntegrator's claims.add() defaults to REPLACE_ALL, which
        silently deletes existing statements on the same property — a
        coexistence violation (ADR-0002): a source-attributed statement
        must never be overwritten.
        Every add in the pipeline must go through here.

        `beside_equal` adds the claim as a statement of its own even when
        a statement with the same value and qualifiers exists (a curated
        one the caller must leave as it is): APPEND_OR_REPLACE would
        instead replace that statement's references and rank with the
        new claim's.
        """
        from wikibaseintegrator.wbi_enums import ActionIfExists

        entity.claims.add(
            claim,
            action_if_exists=ActionIfExists.FORCE_APPEND
            if beside_equal
            else ActionIfExists.APPEND_OR_REPLACE,
        )

    @staticmethod
    def _item_claim(pid: str, qid: str):
        from wikibaseintegrator.datatypes import Item

        return Item(prop_nr=pid, value=qid)

    def _to_wbi_claim(
        self,
        prop_key: str,
        value,
        result: WriteResult,
        source_artifact_qid: str | None,
    ):
        from wikibaseintegrator.datatypes import (
            ExternalID,
            GlobeCoordinate,
            Item,
            MonolingualText,
            Quantity,
            String,
            Time,
        )

        pid = self.properties[prop_key]
        if isinstance(value, ItemValue):
            return Item(
                prop_nr=pid,
                value=self._resolve_ref(
                    value.value, result, source_artifact_qid
                ),
            )
        # text, a claim's value or a qualifier's, goes in the form the
        # instance keeps it in, the form a statement is compared in
        # (_stored_string)
        if isinstance(value, StringValue):
            return String(prop_nr=pid, value=_stored_string(value.value))
        if isinstance(value, ExternalIdValue):
            return ExternalID(prop_nr=pid, value=_stored_string(value.value))
        if isinstance(value, MonolingualValue):
            return MonolingualText(
                prop_nr=pid,
                text=_stored_string(value.value.text),
                language=value.value.language,
            )
        if isinstance(value, TimeValue):
            wikibase = value.to_wikibase()
            return Time(
                prop_nr=pid,
                time=wikibase["time"],
                precision=wikibase["precision"],
                calendarmodel=wikibase["calendarmodel"],
            )
        if isinstance(value, CoordinateValue):
            return GlobeCoordinate(
                prop_nr=pid,
                latitude=value.latitude,
                longitude=value.longitude,
                precision=value.precision or 0.0001,
            )
        if isinstance(value, QuantityValue):
            return Quantity(prop_nr=pid, amount=value.to_wikibase()["amount"])
        raise TypeError(f"unhandled value {value!r}")

    def _references(
        self,
        artifact_qid: str,
        run_qid: str | None = None,
        claim: Claim | None = None,
    ):
        from wikibaseintegrator.datatypes import Item, MonolingualText, String
        from wikibaseintegrator.models import Reference, References

        references = References()
        source_ref = Reference()
        source_ref.add(
            Item(prop_nr=self.properties["stated_in"], value=artifact_qid)
        )
        # the locators and the excerpt only place the claim in its source
        # and help a reader check it there: each goes in NFC, on one line,
        # cleaned up as the instance gives it back and cut to what the
        # instance holds, never failing the write, and one of whitespace
        # alone is left out (_locator_text)
        source = claim.source if claim is not None else None
        page = _locator_text(source.page_number or "") if source else ""
        if page:
            source_ref.add(
                String(prop_nr=self.properties["page_number"], value=page)
            )
        if source is not None and source.region:
            source_ref.add(
                String(
                    prop_nr=self.properties["relative_position_within_image"],
                    value=_locator_text(source.region.as_wikibase_string()),
                )
            )
        excerpt = source.transcription_excerpt if source else None
        text = _locator_text(excerpt.text) if excerpt else ""
        if excerpt and text:
            source_ref.add(
                MonolingualText(
                    prop_nr=self.properties["transcription_excerpt"],
                    text=text,
                    language=excerpt.language,
                )
            )
        references.add(source_ref)
        # dry-run mints no run item; its placeholder cannot be referenced
        if run_qid is not None and re.fullmatch(r"Q[1-9]\d*", run_qid):
            run_ref = Reference()
            run_ref.add(
                Item(
                    prop_nr=self.properties["inferred_from_extraction_run"],
                    value=run_qid,
                )
            )
            references.add(run_ref)
        return references

    # -- extraction-run item -------------------------------------------------

    def ensure_run_item(self, batch: ClaimBatch) -> str:
        run = batch.extraction_run
        fingerprint = batch_fingerprint(batch)
        # looked for, and written, in the form the instance keeps labels
        # in, cut with a digest of the whole when a long model identifier
        # takes it past what the instance holds, so each batch's run item
        # keeps a label of its own (_term_text)
        label = _term_text(
            f"extraction run {run.started_at.date().isoformat()} "
            f"{run.model_identifier} {fingerprint}"
        )
        from yizkr.pipeline.registrar import ensure_self_typed

        existing = self.client.find_item_by_label(label)
        if existing:
            # a write cut short between the two below left the run item
            # without its kind, which it gains now; one that states it
            # already, in any way, is left as it is
            ensure_self_typed(
                self.client,
                existing,
                "extraction_run",
                summary="ingest: run item statements",
                dry_run=self.dry_run,
            )
            return existing
        if self.dry_run:
            return "(new:run)"
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set(
            "en",
            _term_text(
                f"pipeline {run.pipeline_version}"
                + (
                    f", commit {run.software_commit}"
                    if run.software_commit
                    else ""
                )
                + f", started {run.started_at.isoformat()}"
            ),
        )
        item = item.write(summary="ingest: extraction run")
        # The run item is the document of its own execution (same
        # convention as the model manifest, docs/MODELING.md): its kind
        # cites the item itself, so it is written once the item has an id.
        ensure_self_typed(
            self.client,
            item.id,
            "extraction_run",
            summary="ingest: run item statements",
        )
        return item.id

    # -- statement existence check -------------------------------------------

    def _entity(self, qid: str):
        """The item as last read or written by this writer."""
        if qid not in self._entity_cache:
            self._entity_cache[qid] = self.client.wbi.item.get(qid)
        return self._entity_cache[qid]

    def _find_matching_statement(self, subject_qid: str, wbi_claim):
        """The existing statement equal to this claim, or None.

        Equal means same property, same mainsnak value, AND the same
        qualifiers: a same-value claim with different qualifiers (a
        marriage date on a spouse statement, a residence date) is a
        DIFFERENT assertion and must coexist as its own statement
        (ADR-0002), never be swallowed by a bare-value match.

        The claim's text, in its value and qualifiers, is in NFC as
        `_to_wbi_claim` builds it, the form the instance gives a
        statement's text back in (_stored_string), so text the batch
        gives in another form still equals the statement written for it.
        """
        if subject_qid.startswith("(new:"):
            return None
        entity = self._entity(subject_qid)
        prop = wbi_claim.mainsnak.property_number
        new_value = wbi_claim.mainsnak.datavalue.get("value")
        new_qualifiers = self._qualifier_snapshot(wbi_claim)
        for existing in entity.claims.get(prop) or []:
            existing_value = (existing.mainsnak.datavalue or {}).get("value")
            if self._values_equal(existing_value, new_value) and (
                self._qualifier_snapshot(existing) == new_qualifiers
            ):
                return existing
        return None

    @classmethod
    def _qualifier_snapshot(cls, wbi_claim) -> list:
        """Order-independent, hash-independent view of the qualifiers."""
        snapshot = []
        for prop, snaks in wbi_claim.qualifiers.qualifiers.items():
            for snak in snaks:
                value = (snak.datavalue or {}).get("value")
                snapshot.append(
                    (prop, json.dumps(cls._value_key(value), sort_keys=True))
                )
        return sorted(snapshot)

    @staticmethod
    def _value_key(value):
        """The identity-relevant part of a datavalue (drops hashes,
        display-only fields, and unset optionals)."""
        if isinstance(value, dict):
            keys = (
                {"id"}
                if "id" in value
                else {"time", "precision", "calendarmodel"}
                if "time" in value
                else {"latitude", "longitude"}
                if "latitude" in value
                else {"text", "language"}
                if "text" in value
                else {"amount", "unit"}
                if "amount" in value
                else set(value)
            )
            return {key: value.get(key) for key in keys}
        return value

    @classmethod
    def _values_equal(cls, a, b) -> bool:
        if isinstance(a, dict) and isinstance(b, dict):
            return all(a.get(k) == b.get(k) for k in cls._value_key(a))
        return a == b

    def _has_artifact_reference(self, wbi_claim, artifact_qid: str) -> bool:
        """Does any reference block cite `stated in -> artifact_qid`?"""
        stated_in = self.properties["stated_in"]
        for reference in wbi_claim.references.references:
            for snak in reference.snaks.get(stated_in) or []:
                value = (snak.datavalue or {}).get("value") or {}
                if isinstance(value, dict) and value.get("id") == artifact_qid:
                    return True
        return False

    def _blocks_to_add(self, statement, references, artifact_qid: str) -> list:
        """The reference blocks of a claim that the equal statement it
        matches gains (step 4 of the module docstring); none when the
        claim is already recorded there.

        `references` are the claim's blocks as `_references` builds them,
        its source block first. The source block is compared whole with
        the statement's blocks: `stated in` with the page number, region
        and excerpt. So the same artifact under another locator or
        excerpt, another place where it asserts the value, adds a block
        of its own. A curated statement is compared by artifact alone: a
        block that cites the claim's artifact under any locator records
        the claim. The blocks returned are those the statement does not
        already carry, so a run's block is not repeated for a second
        claim of the same run.
        """
        present = {
            self._reference_key(block)
            for block in statement.references.references
        }
        source = references.references[0]
        if (
            self._has_artifact_reference(statement, artifact_qid)
            if self._is_curated(statement)
            else self._reference_key(source) in present
        ):
            return []
        return [
            block
            for block in references.references
            if self._reference_key(block) not in present
        ]

    # -- items a ref or record found rather than minted ----------------------

    def _found_entities(self, result: WriteResult) -> list[_FoundEntity]:
        """What the batch says of each item that existed before it and that
        one of its claims mentions, and the kinds that later refs give an
        item it minted (step 5 of the module docstring).

        A manifest record names its item by an approved equivalence
        (`qid`), by the exact-label match that materialized its local refs
        here, or, once the resolve step has replaced those refs with the
        item's QID, by the item's English label being the record's label
        in the form the instance keeps labels in (_term_text): the match
        the resolve step made. Only in that last case have the
        record's aliases not reached the item yet. A record whose label
        holds nothing the instance keeps names no item this way, as an
        item with no English label matches no record. A local ref without
        a manifest record that the writer matched by exact label gives the
        kind on its own `instance_of`, cited to its own claim's artifact.
        """
        found: list[_FoundEntity] = []
        by_label: dict[str, list[EntityRecord]] = {}
        for record in self._entity_records.values():
            qid = record.qid or result.created_items.get(record.id)
            if qid is None:
                stored_label = _term_text(record.label)
                if stored_label:
                    by_label.setdefault(stored_label, []).append(record)
            else:
                found.append(_FoundEntity(qid, record.kind, record.type_hint))
        if by_label:
            for qid in self._mentions:
                if qid in self._minted:
                    continue
                label = str(self._entity(qid).labels.get("en") or "")
                found.extend(
                    _FoundEntity(
                        qid,
                        record.kind,
                        record.type_hint,
                        tuple(record.aliases),
                    )
                    for record in by_label.get(label, [])
                )
        for qid, witnesses in self._ref_kinds.items():
            found.extend(
                _FoundEntity(qid, kind, artifact=artifact)
                for kind, artifact in witnesses
            )
        # an item the batch minted holds the kind of the ref or record that
        # minted it; a later ref that found it by label witnesses its own
        return [
            entity
            for entity in found
            if entity.qid in self._mentions
            and (entity.qid not in self._minted or entity.artifact is not None)
        ]

    def _is_curated(self, statement) -> bool:
        """A human write (no extraction-run reference, ADR-0003) or a
        statement whose rank a curator changed."""
        from wikibaseintegrator.wbi_enums import WikibaseRank

        run = self.properties["inferred_from_extraction_run"]
        machine = any(
            reference.snaks.get(run)
            for reference in statement.references.references
        )
        return not machine or statement.rank != WikibaseRank.NORMAL

    @classmethod
    def _reference_key(cls, reference) -> frozenset:
        """A reference block's snaks, independent of hashes and order."""
        return frozenset(
            (
                prop,
                json.dumps(
                    cls._value_key((snak.datavalue or {}).get("value")),
                    sort_keys=True,
                ),
            )
            for prop, snaks in reference.snaks.snaks.items()
            for snak in snaks
        )

    @staticmethod
    def _type_value(statement):
        """What a type statement says: the item of an `instance of`, the
        normalized text of a `type hint`."""
        value = (statement.mainsnak.datavalue or {}).get("value")
        if isinstance(value, dict):
            return value.get("id")
        if isinstance(value, str):
            return normalized_type_hint(value)
        return None

    def _record_type_statement(
        self, qid: str, claim, artifact_qid: str, result: WriteResult
    ) -> None:
        """Record that `artifact_qid` gives the item `claim`'s kind or
        type hint (step 5 of the module docstring)."""
        entity = self._entity(qid)
        value = self._type_value(claim)
        same = [
            statement
            for statement in entity.claims.get(claim.mainsnak.property_number)
            or []
            if self._type_value(statement) == value
            and not self._qualifier_snapshot(statement)
        ]
        if any(self._has_artifact_reference(s, artifact_qid) for s in same):
            return
        references = self._references(
            artifact_qid=artifact_qid, run_qid=result.run_qid
        )
        target = next((s for s in same if not self._is_curated(s)), None)
        if target is not None:
            present = {
                self._reference_key(reference)
                for reference in target.references.references
            }
            for reference in references.references:
                if self._reference_key(reference) not in present:
                    target.references.add(reference)
            entity.write(
                summary="ingest: additional source for existing type statement"
            )
            result.type_statements_referenced += 1
        else:
            claim.references = references
            self._append(entity, claim, beside_equal=True)
            entity.write(summary="ingest: type statement from another source")
            result.type_statements_written += 1
        self._entity_cache[qid] = self.client.wbi.item.get(qid)

    def _record_found_entities(self, result: WriteResult) -> None:
        from wikibaseintegrator.datatypes import String

        for found in self._found_entities(result):
            if found.aliases:
                self._ensure_aliases(found.qid, list(found.aliases))
            artifact_qid = found.artifact or self._mentions[found.qid]
            # a kind the vocabulary lacks is left out, with the warning
            # write_batch gave before its first write
            if found.kind in self.items:
                self._record_type_statement(
                    found.qid,
                    self._item_claim(
                        self.properties["instance_of"],
                        self.items[found.kind],
                    ),
                    artifact_qid,
                    result,
                )
            # and so is a type hint the instance cannot hold; one it holds
            # goes with its spacing made single spaces and cleaned up as
            # the instance gives it back (_hint_text)
            hint = _hint_text(found.type_hint or "")
            if hint and len(hint) <= TEXT_LIMIT:
                self._record_type_statement(
                    found.qid,
                    String(prop_nr=self.properties["type_hint"], value=hint),
                    artifact_qid,
                    result,
                )

    def _unknown_kinds(
        self, batch: ClaimBatch, refused: Collection[int]
    ) -> list[str]:
        """A warning for each entity whose kind the vocabulary lacks, so
        that no kind is recorded for it. An entity's kind is its manifest
        record's or, for a local id without one, that of the first ref to
        the id in a claim the writer writes, in the order the writer
        resolves refs: each claim's subject, then its value, then its
        qualifiers' values. `refused` holds the places in the batch of
        the claims the writer refuses, whose refs it never resolves."""
        kinds: dict[str, str | None] = {
            record.id: record.kind for record in self._entity_records.values()
        }
        for index, claim in enumerate(batch.claims):
            if index in refused:
                continue
            refs = [claim.subject]
            if isinstance(claim.value, ItemValue):
                refs.append(claim.value.value)
            refs.extend(
                qualifier.value.value
                for qualifier in claim.qualifiers
                if isinstance(qualifier.value, ItemValue)
            )
            for ref in refs:
                if isinstance(ref, LocalRef):
                    kinds.setdefault(ref.local_id, ref.instance_of)
        return [
            f"entity {local_id!r} has the kind {kind!r}, which the "
            f"vocabulary lacks: no kind is recorded for it"
            for local_id, kind in kinds.items()
            if kind and kind not in self.items
        ]

    @staticmethod
    def _refusals(batch: ClaimBatch) -> dict[int, str]:
        """The claims of `batch` the writer refuses, by their place in the
        batch, each with the warning that names it by its claim id and
        property and says why (the module docstring):

        * a document subject that is not the claim's own source, or on a
          property the manifest does not mark `document_subject`, and a
          claim of such a property with any other subject (ADR-0035
          §3.4): the statement would go on the wrong item;
        * a claim whose own text the instance cannot hold as it is: for
          each of its texts, its value or a qualifier's, what keeps the
          instance from holding it in the form the writer sends it in
          (_faults): its length, a line break or a tab, whitespace at
          either end, a control character or a noncharacter the instance
          changes into U+FFFD. The warning quotes none of the texts."""
        document_subjects = load_acceptance().document_subjects
        refusals = {}
        for index, claim in enumerate(batch.claims):
            if isinstance(claim.subject, ArtifactRef):
                reasons = []
                if claim.subject.sha1 != claim.source.artifact.sha1:
                    reasons.append(
                        "its subject is a document other than its own source"
                    )
                if claim.property not in document_subjects:
                    reasons.append(
                        "its property does not take the source document as "
                        "its subject"
                    )
            elif claim.property in document_subjects:
                reasons = ["its property takes only its source document"]
            else:
                reasons = []
            if reasons:
                refusals[index] = (
                    f"claim {claim_id(claim)} ({claim.property}) is "
                    f"refused, and nothing of it is written: "
                    f"{'; '.join(reasons)} (ADR-0035 §3.4)"
                )
                continue
            texts = [("its value", claim.value)]
            texts.extend(
                (f"its {qualifier.property} qualifier", qualifier.value)
                for qualifier in claim.qualifiers
            )
            faults = [
                f"{where} {fault}"
                for where, value in texts
                for fault in _faults(_value_text(value) or "")
            ]
            if faults:
                refusals[index] = (
                    f"claim {claim_id(claim)} ({claim.property}) is "
                    f"refused, and nothing of it is written: "
                    f"{'; '.join(faults)}. A cut or any other change would "
                    f"alter what the source says"
                )
        return refusals

    def _type_hints_over_limit(self) -> list[str]:
        """A warning for each manifest record whose type hint the instance
        cannot hold, as the writer sends it (_hint_text), which is left out
        wherever the writer would record it (the module docstring)."""
        return [
            f"entity {record.id!r} has a type hint of {len(hint)} "
            f"characters in Unicode NFC, its whitespace as single spaces, "
            f"over the {TEXT_LIMIT} Wikibase holds in a text value: it is "
            f"not recorded, as a cut would narrow what the source says"
            for record in self._entity_records.values()
            if len(hint := _hint_text(record.type_hint or "")) > TEXT_LIMIT
        ]

    # -- main entry ----------------------------------------------------------

    def write_batch(
        self, batch: ClaimBatch, bulk_accepted: bool = False
    ) -> WriteResult:
        """Write `batch`. Raises UnapprovedDepictsError, before reading
        from or writing to the instance, when a depicts claim in it lacks
        its row approval (the module docstring), in a dry run too. A
        claim whose own text the instance cannot hold raises nothing: it
        is refused alone, and the rest of the batch is written (the
        module docstring)."""
        unapproved = unapproved_depicts(batch)
        if unapproved:
            raise UnapprovedDepictsError(unapproved)
        # a bulk-accepted batch (whole-sheet accept-all, ADR-0023) gets a
        # distinct edit summary so MediaWiki history is the low-scrutiny
        # reappraisal queue — no graph flag, no staleness.
        claim_summary = (
            "ingest: write bulk-accepted claim"
            if bulk_accepted
            else "ingest: write reviewed claim"
        )
        # every cited artifact must resolve before the first write
        self.resolve_artifacts(batch)
        # the manifest as the depicts gate read it (ADR-0005): one lookup
        self._entity_records = entity_records(batch)
        self._mentions = {}
        self._minted = set()
        self._ref_kinds = {}
        # found before the first write, in a dry run as well: the claims
        # whose own text the instance cannot hold, which the loop below
        # leaves out whole
        refusals = self._refusals(batch)
        # found before the first write too: the kinds the vocabulary lacks,
        # of the claims the writer writes, and the type hints the instance
        # cannot hold, each skipped wherever the writer would record it
        warnings = self._unknown_kinds(batch, refusals)
        warnings += [*refusals.values(), *self._type_hints_over_limit()]
        # reviewed equivalences (ADR-0007): resolve replaced these
        # entities' refs with QIDs, so materialization never sees them —
        # contribute the document's aliases to the existing items here
        if not self.dry_run:
            for record in self._entity_records.values():
                if record.qid:
                    self._ensure_aliases(
                        record.qid, [record.label, *record.aliases]
                    )
        result = WriteResult(
            run_qid=self.ensure_run_item(batch), warnings=warnings
        )
        for index, claim in enumerate(batch.claims):
            if index in refusals:
                # before any of its refs resolves: an item only a refused
                # claim mentions is never minted
                result.refused.append(claim_id(claim))
                continue
            artifact_qid = self._source_artifact_qid(claim.source.artifact)
            subject_qid = self._resolve_ref(
                claim.subject, result, artifact_qid
            )
            if self.dry_run:
                # planning pass: resolve every ref so created_items is
                # complete, then count — WBI datavalues cannot hold the
                # (new:...) placeholders dry-run materialization returns
                if isinstance(claim.value, ItemValue):
                    self._resolve_ref(claim.value.value, result, artifact_qid)
                for qualifier in claim.qualifiers:
                    if isinstance(qualifier.value, ItemValue):
                        self._resolve_ref(
                            qualifier.value.value, result, artifact_qid
                        )
                result.written += 1
                continue
            wbi_claim = self._to_wbi_claim(
                claim.property, claim.value, result, artifact_qid
            )
            for qualifier in claim.qualifiers:
                qualifier_claim = self._to_wbi_claim(
                    qualifier.property, qualifier.value, result, artifact_qid
                )
                wbi_claim.qualifiers.add(qualifier_claim)
            # curated claims are human writes: artifact reference only,
            # no extraction-run reference (ADR-0003 — process provenance
            # is for machine writes)
            run_qid = None if claim.curated else result.run_qid
            references = self._references(
                artifact_qid=artifact_qid, run_qid=run_qid, claim=claim
            )
            existing = self._find_matching_statement(subject_qid, wbi_claim)
            if existing is not None:
                blocks = self._blocks_to_add(
                    existing, references, artifact_qid
                )
                if not blocks:
                    result.skipped += 1
                    continue
                # an agreeing source, or the same source in another place:
                # the statement stays one statement, and gains this
                # claim's blocks (R2 — who asserts a fact, and where, is
                # knowledge, not just the fact)
                for block in blocks:
                    existing.references.add(block)
                entity = self._entity_cache[subject_qid]
                entity.write(
                    summary="ingest: additional source for existing statement"
                )
                self._entity_cache[subject_qid] = self.client.wbi.item.get(
                    subject_qid
                )
                result.referenced += 1
                continue
            wbi_claim.references = references
            if not self.dry_run:
                entity = self._entity_cache.get(
                    subject_qid
                ) or self.client.wbi.item.get(subject_qid)
                self._append(entity, wbi_claim)
                entity.write(summary=claim_summary)
                self._entity_cache[subject_qid] = self.client.wbi.item.get(
                    subject_qid
                )
            result.written += 1
        if not self.dry_run:
            self._record_found_entities(result)
        return result
