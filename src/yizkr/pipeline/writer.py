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

It refuses just as early, with UnsupportedDate, a batch holding a date
before the common era or in a calendar other than the proleptic Gregorian,
as a claim's value or a qualifier's (ADR-0034): the claim schema refuses
one, so only a batch built around it can hold one, and the writer names
every such claim and writes nothing.

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
   matched by a deterministic label containing the batch content hash,
   which must name one item alone: when two items hold it the write
   fails before it writes any claim, `ItemLabels.sole`),
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
   holds in a label, `_term_text`; the instance's labels are read once a
   write, with the vocabulary known, and the run item and each entity are
   found among them and the items the write makes, `_FoundLabels`; a
   label two or more items hold, as two people of one name do, names
   the numerically earliest of them, `ItemLabels`),
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
   gives one, found before the first write: a manifest record's kind
   whatever its claims, and the kind of a ref without a record only when
   a written claim mentions it.
   An item is reused by its label only on the entity's own side
   (ADR-0010, "What is still matched by label"): an artifact or a part
   of one (`is_artifact_item`: an item with a `media SHA-1` or a `media
   file`, or an `instance of` an artifact kind or of artifact fragment)
   is never any other entity's item, and an artifact-kind entity (of an
   artifact kind or of artifact fragment, `is_artifact_kind`), such as
   the GEDCOM import's media object, reuses only such an item, and
   never one registration made (_registered): an artifact, which
   carries a `media SHA-1`, or its page fragment, `part of` one. An
   artifact-kind entity so reuses an artifact registered before content
   hashing, another media object's item, or a fragment of an artifact
   with no hash. An entity that may not reuse the item under its label
   is found, or made, under its tagged label (`tagged_label`): its label
   with MEDIA_OBJECT_TAG at its end for an artifact-kind entity
   (`media_object_label`), and with ENTITY_TAG for any other, whatever
   its kind, its own label then an alias of the item. Where a media
   object does not take its label, the result's `flags` name, against the
   media object's item, as unhashed-title, every artifact registered
   under the label, whatever item holds it: the artifact under it (the
   fragment's whole, for a fragment) and every artifact under it with
   its own content tag in it ([sha1[:8]] of its media, or of the
   fragment's whole), at its end or before a fragment's ", page <n>"
   (_registered_under), in the order of their items, as the
   registrar flags each when the registration comes second
   (yizkr.pipeline.registrar), on every write that finds them so; a dry
   run names the item's placeholder. Where each entity's item is found
   or made is settled before the first write (_plan), in a dry run too,
   with the labels the batch itself makes items under: the entities not
   on the artifact side are placed first, so of a person and a media
   object of one batch under a label no item holds, the person takes
   it, whichever the batch names first. An entity that may reuse
   neither the item under its label nor the one under its tagged label,
   as when registered artifacts hold both labels of a media object, is
   refused then, with every claim that names it: the result's `refused`
   names those claims and a warning names the entity by its local id,
   its claims by their claim ids and the two items by QID, and the rest
   of the batch is written. For a refused artifact-kind entity the
   `flags` name, against the item under its tagged label, each artifact
   registered under its label; a refusal with no such pair, as of a
   person, place or other entity, or of a media object with no artifact
   registered under its label, is the warning and `refused` alone
   (ADR-0010). The plan is made of the claims the writer writes, without
   those refused before it (pre_plan_refusals: for their text, or as a
   misplaced document subject, ADR-0035 §3.4): an entity, such as a
   media object, that only such claims name is never placed, so it is
   neither blocked nor flagged. Of a manifest record that only refused
   claims name, refused before the plan or with an entity the plan
   blocks, nothing is sent, so none of its description, aliases or type
   hint is named in the result's `refused_terms` (_unsent_records);
3. writes each claim as a statement at NORMAL rank (the contract has
   no rank field — ADR-0002) with TWO references (ADR-0003):
   * stated in -> artifact, with page number / printed page number /
     region / excerpt qualifiers inside the reference, the printed page
     number only where the claim's source gives one (the extractor reads
     it from the transcription's record, ADR-0037);
   * inferred from extraction run -> the run item;
4. when a statement with the same property, value, AND qualifiers
   already exists, the claim is recorded on that statement rather than
   beside it: an agreeing second source is knowledge, and so is each
   place where one source asserts the value (R2 records who asserts a
   fact, and where, not just the fact). The claim is compared as the
   writer sends it, with every text in it in Unicode NFC, the form the
   instance keeps text in and gives it back in (`_stored_string`): its
   string, external-id and monolingual text values and qualifiers, and
   its source block's page number, printed page number, region and
   excerpt, which also go on one line and cleaned up as the instance
   gives them back (`_locator_text`). So a claim whose text the batch
   gives in another form, such as the decomposed form (NFD), finds the
   statement written for it. The claim's source block, `stated in` with
   its page number, printed page number, region and excerpt, is compared
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
   equivalence, by exact label here, or by the resolve step's match,
   which finds an item by the label and side rules step 2 does,
   reused_item), and the kind a local ref without a record gives on its
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
* a reference's page number, printed page number and region only place
  the claim in its source, as its excerpt only helps a reader check it
  there, so each is put on one line, cleaned up and cut to the limit
  rather than refused (`_locator_text`; one holding a character Python's
  Unicode tables do not know refuses its claim, as below): whitespace at
  either end goes, a line break inside becomes " | ", visibly, a tab a
  space, and each other character the instance cleans up U+FFFD, as the
  instance would give it back. A text still over the limit is cut at a
  break between words and ends with CUT_MARK (" …"), "sha1" and the
  first 12 hex digits of the SHA-1 of the whole text (`_cut`). A locator
  or excerpt with nothing left is left out. Each depends on the text
  alone, so a re-run builds the block the instance keeps, and is skipped
  (step 4). The digest keeps two page numbers, printed page numbers,
  regions or excerpts that agree up to the cut apart, so the two places
  a source asserts a value in stay two reference blocks;
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
  instance keeps alike are alike to both;
* the instance puts text in NFC by the tables of Unicode 15.0
  (INSTANCE_UNICODE: ICU 72.1 in the wikibase/wikibase:7 image, which
  UtfNormal 4.0.0 reaches through PHP's intl), and the writer by Python's
  (unicodedata.unidata_version, 14.0.0 on Python 3.11). By Unicode's
  normalization stability policy the two give the same form to every
  text whose characters Python's tables assign, so the writer sends no
  text that holds a character they do not, noncharacters aside
  (`unknown_characters`): a combining mark assigned since, such as
  U+1E08F, may take another place in the instance's NFC, and a re-run
  would not find what the first run wrote. Each text is read whole for
  it, past TERM_LIMIT or TEXT_LIMIT too, before any cut. A claim whose
  own text, or whose source block's page number, printed page number,
  region or excerpt, holds one is refused whole, as above. A
  description, an alias or a type hint that holds one is left out, never
  changed, and the result's
  `refused_terms` names it ("<local id>: <term>"), with a warning that
  names its characters by their code points; a description is named
  only when a claim the writer writes mentions its entity, as it goes
  only on the item such a claim mints: not one only claims refused
  mention, for their text or because they name an entity step 2
  refuses. An entity whose label
  holds one has no item the writer can find or mint by it: each claim
  that mentions it, as its subject, its value or a qualifier's, is
  refused before the first write, and `refused_terms` names the label;
  the label of an entity an approved equivalence names, which goes as an
  alias of its item, is left out as an alias is. A run item's label or
  description that holds one raises ValueError before anything is read
  or written. The cuts read characters by Python's tables too (extends),
  and no cut of such a text is sent.

Nothing here auto-approves: the writer expects the output of
`apply_review` and trusts the human decisions that produced it, except
for depicts, whose per-row approval it checks, as above.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
from yizkr.core.dates import UnsupportedDate
from yizkr.core.wiki import ItemLabels, WikiClient
from yizkr.model.schema import load_acceptance
from yizkr.pipeline.review import (
    claim_id,
    entity_records,
    unapproved_depicts,
)

if TYPE_CHECKING:
    from yizkr.pipeline.registrar import RegistrationFlag

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
# what the label of a media object's item ends with when a registered
# artifact holds the media object's title (media_object_label)
MEDIA_OBJECT_TAG = " [media object]"
# what the label of any other entity's item ends with when an artifact or a
# part of one holds its label: one tag for every kind, so refs of two kinds
# naming one person meet on one item, as they do under a free label
# (tagged_label)
ENTITY_TAG = " [not an artifact]"
# the content tag registration gives a label another item holds: " [", the
# first 8 hex digits of the media's SHA-1, "]" (yizkr.pipeline.registrar)
_CONTENT_TAG = re.compile(r" \[[0-9a-f]{8}\]")
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
# The Unicode version of the tables the instance puts text in NFC by.
# Wikibase does it through UtfNormal 4.0.0, which MediaWiki pins and which
# calls PHP's intl normalizer, and the PHP of the wikibase/wikibase:7
# image (Debian 12) builds intl against ICU 72.1, whose tables are
# Unicode 15.0 (ADR-0010). The writer puts text in NFC by Python's own
# tables (unicodedata.unidata_version), which must be no newer than these.
# Unicode's normalization stability policy then makes the two agree on
# every text whose characters Python's tables assign, and the writer sends
# no other (unknown_characters). A test holds the interpreter to it.
INSTANCE_UNICODE = "15.0.0"


def _noncharacter(code: int) -> bool:
    """Whether the code point `code` is a noncharacter: U+FDD0 to U+FDEF
    and the last two code points of every plane, which Unicode never
    assigns, so every version's NFC leaves them as they are."""
    return 0xFDD0 <= code <= 0xFDEF or code & 0xFFFE == 0xFFFE


def unknown_characters(text: str) -> list[str]:
    """The characters of `text` that Python's Unicode tables do not
    assign (category Cn), noncharacters aside, each once, in the order
    they first come. Unicode may have assigned one since those tables, as
    a combining mark with a combining class of its own, which the
    instance's newer tables (INSTANCE_UNICODE) put in another order in
    NFC than Python's do: U+1E08F, for one, which Unicode 15.0 assigns
    after Python 3.11's 14.0.0. So a text that holds one may be kept in
    another form than the writer sends, and a re-run would not find what
    the first run wrote. Every other text the two put in the same form,
    by Unicode's normalization stability policy, as long as Python's
    tables are no newer than the instance's. The writer sends no text
    that holds one (the module docstring)."""
    return list(
        dict.fromkeys(
            char
            for char in text
            if unicodedata.category(char) == "Cn"
            and not _noncharacter(ord(char))
        )
    )


def _unknown_fault(chars: list[str]) -> str:
    """What keeps the instance from holding a text that holds the
    characters `chars` (unknown_characters) as the writer sends it: a
    clause that reads after the words naming the text. It names each
    character by its code point, quoting no text."""
    codes = _listed([f"U+{ord(char):04X}" for char in chars])
    which = "a character" if len(chars) == 1 else "characters"
    return (
        f"holds {codes}, {which} Python's Unicode "
        f"{unicodedata.unidata_version} tables do not know, which the "
        f"instance may put in another order in NFC"
    )


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


def unsupported_dates(batch: ClaimBatch) -> list[str]:
    """Each date of `batch` the project does not support (ADR-0034), a
    claim's value or a qualifier's: the claim's id and property, which
    part holds it (its value, or the qualifier by its property) and the
    reason (``TimeValue.problem``)."""
    found = []
    for claim in batch.claims:
        parts = [("its value", claim.value)]
        parts.extend(
            (f"its {q.property} qualifier", q.value) for q in claim.qualifiers
        )
        for part, value in parts:
            if isinstance(value, TimeValue) and (problem := value.problem()):
                found.append(
                    f"claim {claim_id(claim)} ({claim.property}), {part}: "
                    f"{problem}"
                )
    return found


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
    # character it cleans up into U+FFFD, with a character Python's
    # Unicode tables do not know in it or its source block, or mentioning
    # an entity whose label holds one), or because it names an entity
    # that may reuse neither the item under its label nor the one under
    # its tagged label (step 2 of the module docstring), in batch order;
    # a warning says why for each (the module docstring)
    refused: list[str] = field(default_factory=list)
    # each registered artifact under the label of an artifact-kind entity
    # of the batch, such as a GEDCOM media object, as it is or content-
    # tagged, its own or a fragment's: the entity's item is never that
    # artifact or fragment, and the pair is flagged unhashed-title, as the
    # registrar flags it, for a person to look at, against the entity's
    # item, its placeholder in a dry run, or, for an entity refused, the
    # item under its tagged label (step 2 of the module docstring;
    # yizkr.pipeline.registrar.RegistrationFlag)
    flags: list[RegistrationFlag] = field(default_factory=list)
    # each label, description, alias or type hint of the batch's entities
    # left out because it holds a character Python's Unicode tables do not
    # know (unknown_characters), named "<local id>: <term>", in the order
    # write_batch finds them before its first write; a warning names each.
    # A description is named only when a claim the writer writes mentions
    # its entity, as only the item such a claim mints would take it
    refused_terms: list[str] = field(default_factory=list)


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
    type hint (_hint_text), and a reference block's page number, printed
    page number, region and excerpt (_locator_text). A label, description
    or alias goes in the form the instance keeps a term in, which is in
    NFC too (_term_text).
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
    (WHITESPACE) at either end, holds no other character the instance
    cleans up into U+FFFD (CLEANED_UP; a vertical tab and a form feed are
    line breaks here), and holds no character Python's Unicode tables do
    not know (unknown_characters)."""
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
    unknown = unknown_characters(text)
    if unknown:
        faults.append(_unknown_fault(unknown))
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
    the registrar's keep to it. It reads `char` by Python's Unicode
    tables, which take a mark assigned since them for no mark; the
    writer sends no cut of a text that holds one (unknown_characters)."""
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
    """A reference's page number, printed page number, region or excerpt
    as the writer sends it: in the form the instance keeps text in
    (_stored_string), on one line (_one_line), cleaned up as the instance
    gives it back (_cleaned_up; a vertical tab or a form feed is a line
    break by then) and, when that is longer than TEXT_LIMIT, cut to it
    (_cut), with the digest of the text as the instance would keep it.
    It depends on the text alone, so a claim written again sends the
    text the instance keeps, and its reference block is found (step 4 of
    the module docstring). Empty when the text is whitespace alone."""
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


def media_object_label(label: str) -> str:
    """The label of the item for an artifact-kind entity, such as a GEDCOM
    media object, whose label `label` (as the writer sends a label,
    _term_text) a registered artifact holds: `label`, cut to leave room
    (_cut), then MEDIA_OBJECT_TAG. It depends on the label alone, so a
    re-run finds the item again, and the registrar looks for it under an
    artifact's title (ADR-0010)."""
    return _cut(label, TERM_LIMIT - len(MEDIA_OBJECT_TAG)) + MEDIA_OBJECT_TAG


def tagged_label(label: str, kind: str | None) -> str:
    """The label of the item for an entity of the kind `kind` whose label
    `label` (as the writer sends a label, _term_text) names an item it
    may not join (step 2 of the module docstring): for an entity on the
    artifact side (is_artifact_kind), `media_object_label`; for any other,
    of whatever kind or none, `label`, cut to leave room (_cut), then
    ENTITY_TAG. It depends on the label and the side alone, so a re-run
    finds the item again, and refs of two kinds naming one entity meet on
    one item (ADR-0010)."""
    if is_artifact_kind(kind):
        return media_object_label(label)
    return _cut(label, TERM_LIMIT - len(ENTITY_TAG)) + ENTITY_TAG


def artifact_kinds() -> frozenset[str]:
    """The manifest keys of the artifact kinds: artifact, and each class
    the manifest makes a `subclass_of` it."""
    from yizkr.pipeline.transcription_record import artifact_classes

    return frozenset({"artifact", *artifact_classes()})


def is_artifact_kind(kind: str | None) -> bool:
    """Is an entity of the kind `kind` on the artifact side: an artifact
    kind (artifact_kinds) or artifact fragment, the kinds whose items are
    artifacts or parts of one (is_artifact_item)?"""
    return kind in artifact_kinds() or kind == "artifact_fragment"


def is_artifact_item(
    claims: Mapping[str, list],
    properties: Mapping[str, str],
    items: Mapping[str, str],
) -> bool:
    """Is the item whose statements, as the API gives them, are `claims`
    an artifact or a part of one: does it carry a `media SHA-1`, name a
    `media file`, or is it an `instance of` an artifact kind
    (artifact_kinds) or of artifact fragment? `properties` and `items`
    map the manifest's keys to the instance's ids. Registration makes
    each such item, but an artifact registered before content hashing and
    a GEDCOM media object's item (ADR-0010). Every statement counts,
    whatever its rank, as for a `media SHA-1` (_registered) and in the
    registrar."""
    from yizkr.pipeline.registrar import _file_key

    def values(key: str) -> list:
        pid = properties.get(key)
        return [
            ((statement.get("mainsnak") or {}).get("datavalue") or {}).get(
                "value"
            )
            for statement in (claims.get(pid) or [] if pid else [])
        ]

    if any(isinstance(value, str) and value for value in values("media_sha1")):
        return True
    if any(
        isinstance(value, str) and _file_key(value)
        for value in values("media_file")
    ):
        return True
    kinds = {
        items[key]
        for key in (*artifact_kinds(), "artifact_fragment")
        if key in items
    }
    return any(
        isinstance(value, dict) and value.get("id") in kinds
        for value in values("instance_of")
    )


@dataclass(frozen=True)
class _Placement:
    """Where step 2 of the module docstring puts a local entity: `label`,
    the label its item is found or made under; `alias`, its own label,
    kept as an alias of that item when `label` is tagged (tagged_label);
    `held`, each registered artifact (artifact, SHA-1) flagged against
    its item (BatchWriter._registered_under); and `blocked`, for an
    entity refused because it may join neither the item under its label
    nor the one under its tagged label, those two items' QIDs, "" for an
    item the batch makes."""

    label: str
    alias: str = ""
    held: tuple[tuple[str, str], ...] = ()
    blocked: tuple[str, str] | None = None


class _FoundLabels:
    """The item each label names on the instance, as the writer finds an
    item by its label, for one write: the instance's item labels read
    once, on first need (`WikiClient.label_index`), with each item the
    write makes for an entity under its label since (`made`). A label two
    or more items hold (homonyms, such as two people of one name) names
    the numerically earliest of them (ItemLabels), so the plan, the write
    and the resolve step all find that one; the run item's label must
    name one item alone (`sole`). The documents of a run are written one
    at a time, and the write itself makes every item it gives a label, so
    the labels read once stay current through the write: the plan
    (BatchWriter._plan), the artifacts registered under a label
    (BatchWriter._registered_under), the items found or made for the
    batch's entities and its run item all read them, and a write reads
    the instance's labels once, with the vocabulary known (a client that
    has not read it yet reads the labels for it when the writer is made,
    WikiClient.vocabulary). It assumes one process writes to the
    instance at a time: an item another process makes under a label
    during the write is not seen until the next write."""

    def __init__(self, client: WikiClient) -> None:
        self._client = client
        self._read: dict[str, str] | None = None
        self._made: dict[str, str] = {}
        self._titles: dict[str, list[tuple[str, str]]] | None = None

    def items(self) -> ItemLabels:
        """The item labels the instance held when first asked, label ->
        QID, a shared label naming its earliest item (ItemLabels)."""
        if self._read is None:
            _, read = self._client.label_index()
            self._read = ItemLabels.of(read)
        return self._read

    def get(self, label: str, default: str | None = None) -> str | None:
        return self._made.get(label) or self.items().get(label) or default

    def sole(self, label: str) -> str | None:
        """The one item under `label`, made by the write or read; raises
        when two or more items held it when read (ItemLabels.sole)."""
        return self._made.get(label) or self.items().sole(label)

    def made(self, label: str, qid: str) -> None:
        """The write made the item `qid` under `label`."""
        self._made[label] = qid

    def titled(self, title: str) -> list[tuple[str, str]]:
        """Each (label, QID) of the labels read (`items`), every holder of
        a shared label included, that is `title` as it is or with one
        content tag taken out of it, wherever it is (_CONTENT_TAG), in the
        order of their items. The labels are indexed once, on first need,
        by each label and by each label with one of its content tags taken
        out, so a lookup costs only what it finds. The items the write
        makes are not in it: the writer makes no artifact, so none of them
        is registered (_registered_under)."""
        if self._titles is None:
            titles: dict[str, list[tuple[str, str]]] = {}
            for label, qid in self.items().pairs():
                keys = {label}
                keys.update(
                    label[: tag.start()] + label[tag.end() :]
                    for tag in _CONTENT_TAG.finditer(label)
                )
                for key in keys:
                    titles.setdefault(key, []).append((label, qid))
            for pairs in titles.values():
                pairs.sort(key=lambda pair: int(pair[1][1:]))
            self._titles = titles
        return self._titles.get(title, [])


def local_refs(claim: Claim) -> list[LocalRef]:
    """The local refs of `claim`, in the order the writer resolves them:
    its subject, its value, then its qualifiers' values."""
    refs = [claim.subject]
    if isinstance(claim.value, ItemValue):
        refs.append(claim.value.value)
    refs.extend(
        qualifier.value.value
        for qualifier in claim.qualifiers
        if isinstance(qualifier.value, ItemValue)
    )
    return [ref for ref in refs if isinstance(ref, LocalRef)]


def unnamed_entities(
    batch: ClaimBatch, records: Mapping[str, EntityRecord]
) -> dict[str, list[str]]:
    """The local ids of the entities the writer can neither find nor
    mint an item for, each with the characters Python's Unicode tables
    do not know that its label holds (unknown_characters): the
    instance may keep such a label in another form, so a re-run would
    not find the item by it. A local id's label is its manifest
    record's when it has one without an approved equivalence (one
    with an equivalence names its item by QID), and otherwise that of
    each ref to the id in the batch, any of which may be the first the
    writer resolves. `records` is the batch's manifest (entity_records).
    The resolve step calls this too, so that it passes over the same
    entities as the writer."""
    unnamed: dict[str, list[str]] = {}
    for record in records.values():
        unknown = unknown_characters(record.label)
        if unknown and not record.qid:
            unnamed[record.id] = unknown
    for claim in batch.claims:
        for ref in local_refs(claim):
            if ref.local_id not in records:
                unknown = unknown_characters(ref.label or "")
                if unknown:
                    known = unnamed.setdefault(ref.local_id, [])
                    known.extend(c for c in unknown if c not in known)
    return unnamed


def pre_plan_refusals(
    batch: ClaimBatch, unnamed: Mapping[str, list[str]]
) -> dict[int, str]:
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
      changes into U+FFFD, a character Python's Unicode tables do not
      know (unknown_characters);
    * a claim whose source block's page number, printed page
      number, region or excerpt holds a character Python's Unicode
      tables do not know: the instance may keep the block in another
      form, and a re-run would add it again beside the first;
    * a claim that mentions an entity whose label keeps the writer
      from finding or minting its item (`unnamed`, from unnamed_entities).

    The warning quotes none of the texts."""
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
        source = claim.source
        locators = [
            ("its page number", source.page_number),
            ("its printed page number", source.printed_page_number),
            (
                "its region",
                source.region.as_wikibase_string() if source.region else None,
            ),
            (
                "its excerpt",
                source.transcription_excerpt.text
                if source.transcription_excerpt
                else None,
            ),
        ]
        faults.extend(
            f"{where} {_unknown_fault(unknown)}"
            for where, text in locators
            if (unknown := unknown_characters(text or ""))
        )
        faults.extend(
            f"it mentions entity {local_id!r}, whose label "
            f"{_unknown_fault(unnamed[local_id])}, so that no item is "
            f"found or made by it"
            for local_id in dict.fromkeys(
                ref.local_id
                for ref in local_refs(claim)
                if ref.local_id in unnamed
            )
        )
        if faults:
            refusals[index] = (
                f"claim {claim_id(claim)} ({claim.property}) is "
                f"refused, and nothing of it is written: "
                f"{'; '.join(faults)}. A cut or any other change would "
                f"alter what the source says"
            )
    return refusals


def entity_terms(
    batch: ClaimBatch,
    records: Mapping[str, EntityRecord],
    refused: Collection[int] = (),
) -> dict[str, tuple[str, str | None]]:
    """Each local entity of `batch` that the writer finds or makes an item
    for by its label, local id -> (label, kind): its manifest record's
    (`records`, entity_records) or, without one, its first ref's, in the
    order the writer resolves the refs of the claims (local_refs), all but
    those at the places `refused`. The label is in the form the writer
    sends and looks for it (_term_text). An entity an approved equivalence
    names, or with no label the instance keeps, has none: the first finds
    its item by QID, the second none at all."""
    entities: dict[str, tuple[str, str | None]] = {}
    for index, claim in enumerate(batch.claims):
        if index in refused:
            continue
        for ref in local_refs(claim):
            if ref.local_id in entities:
                continue
            record = records.get(ref.local_id)
            if record and record.qid:
                continue
            label = _term_text((record.label if record else ref.label) or "")
            if not label:
                continue
            kind = record.kind if record else ref.instance_of
            entities[ref.local_id] = (label, kind)
    return entities


def side_label(
    label: str, kind: str | None, joins: Callable[[str], bool]
) -> tuple[str, bool]:
    """The label the item of an entity with the label `label` (as the
    writer sends a label, _term_text) and the kind `kind` is found or made
    under (step 2 of the module docstring), and whether the entity may
    take the item there: `label` when it may join the item under it
    (`joins`), and otherwise its tagged label (tagged_label), which it
    takes when it may join the item under that one, and is refused at
    otherwise. `joins` holds the side rule (ADR-0010): which item under a
    label the entity may join, and which label nobody holds it may make
    one under."""
    if joins(label):
        return label, True
    tagged = tagged_label(label, kind)
    return tagged, joins(tagged)


def reused_item(
    label: str,
    kind: str | None,
    held: Mapping[str, str],
    artifact_item: Callable[[str], bool],
) -> str | None:
    """The QID of the existing item the writer reuses for an entity not on
    the artifact side (is_artifact_kind) with the label `label` (as the
    writer sends a label, _term_text) and the kind `kind`, as it places
    the entity (side_label): the item under `label` when it is no artifact
    nor a part of one (`artifact_item`, is_artifact_item), and when it is
    one, the item under the tagged label (tagged_label) when that one is
    not. None when the writer makes the entity an item, under either
    label, or refuses it. `held` is the instance's item labels, label ->
    QID, as the writer reads them before its first write."""

    def joins(candidate: str) -> bool:
        qid = held.get(candidate)
        return qid is None or not artifact_item(qid)

    placed, joined = side_label(label, kind, joins)
    return held.get(placed) if joined else None


def _terms(texts: list[str]) -> list[str]:
    """`texts`, a list of aliases, as the writer sends it: each alias as
    it sends a term (_term_text), in their order, leaving out a repeat, an
    alias with nothing the instance keeps, which it refuses, and one that
    holds a character Python's Unicode tables do not know anywhere in it,
    past TERM_LIMIT too, which the instance may keep in another form and
    which no cut of sends altered (unknown_characters; write_batch warns
    of each before its first write)."""
    return [
        term
        for term in dict.fromkeys(
            _term_text(text) for text in texts if not unknown_characters(text)
        )
        if term
    ]


def _hint_sent(text: str) -> str:
    """An entity's type hint as the writer records it (_hint_text), or ""
    when it leaves the hint out: one over TEXT_LIMIT, or holding a
    character Python's Unicode tables do not know (unknown_characters).
    write_batch warns of each before its first write."""
    hint = _hint_text(text)
    if len(hint) > TEXT_LIMIT or unknown_characters(hint):
        return ""
    return hint


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


def _run_terms(batch: ClaimBatch) -> tuple[str, str]:
    """The label and description of the batch's run item, as the writer
    looks for and writes them: in the form the instance keeps a term in,
    cut with a digest of the whole when a long model identifier takes the
    label past what the instance holds, so each batch's run item keeps a
    label of its own (_term_text). Raises ValueError, naming the
    characters, when either holds a character Python's Unicode tables do
    not know (unknown_characters), whole, before any cut: the instance
    may keep it in another form, and a re-run would mint the run item
    again. Both are machine
    text, from the run's own settings, so the batch is refused before
    anything is read or written rather than its run left unlabelled."""
    run = batch.extraction_run
    label = (
        f"extraction run {run.started_at.date().isoformat()} "
        f"{run.model_identifier} {batch_fingerprint(batch)}"
    )
    description = (
        f"pipeline {run.pipeline_version}"
        + (f", commit {run.software_commit}" if run.software_commit else "")
        + f", started {run.started_at.isoformat()}"
    )
    for what, term in (("label", label), ("description", description)):
        unknown = unknown_characters(term)
        if unknown:
            raise ValueError(
                f"the extraction run's {what} {_unknown_fault(unknown)}: "
                f"give the run's model identifier, pipeline version and "
                f"commit without it. Nothing was written"
            )
    return _term_text(label), _term_text(description)


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
        # per write_batch call: local id -> where step 2 puts its item,
        # settled before the first write (_plan)
        self._placements: dict[str, _Placement] = {}
        # per write_batch call: the local ids of the manifest records of
        # which nothing is sent (_unsent_records)
        self._unsent: set[str] = set()
        # per write_batch call: the item each label names, the instance's
        # labels read once (_FoundLabels)
        self._labels = _FoundLabels(client)

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
        # where its item is found or made, settled before the first write
        # (_plan): under its label, or under its tagged label when it may
        # not join the item its label names (step 2 of the module
        # docstring), its own label then an alias
        placement = self._placements[ref.local_id]
        label = placement.label
        if placement.alias:
            aliases = [placement.alias, *aliases]
        existing = self._labels.get(label)
        if existing:
            self._flag_held(placement, existing, result)
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
        # run too. One that holds a character Python's Unicode tables do
        # not know anywhere in it, past TERM_LIMIT too, is left out whole,
        # never sent cut, with the warning write_batch gave before its
        # first write (unknown_characters)
        if unknown_characters(description or ""):
            description = ""
        description = _term_text(description or "")
        if description == label:
            description = ""
            result.warnings.append(
                f"entity {ref.local_id!r} has a description that is its "
                f"label: its item is made without one, as Wikibase refuses "
                f"an item whose label and description are the same"
            )
        if self.dry_run:
            # the pair a write would flag is reported against the item's
            # placeholder
            result.created_items[ref.local_id] = f"(new:{ref.local_id})"
            self._flag_held(placement, f"(new:{ref.local_id})", result)
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
        self._labels.made(label, qid)
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
        # cannot hold, or may keep in another form, is left out, with the
        # warning write_batch gave before its first write (_hint_sent)
        type_hint = _hint_sent(record.type_hint or "") if record else ""
        if type_hint and source_artifact_qid:
            from wikibaseintegrator.datatypes import String

            hint = String(
                prop_nr=self.properties["type_hint"], value=type_hint
            )
            hint.references = self._references(
                artifact_qid=source_artifact_qid, run_qid=result.run_qid
            )
            self._append(item, hint)
            item.write(summary="ingest: type hint (unclassified entity)")
        self._flag_held(placement, qid, result)
        result.created_items[ref.local_id] = qid
        return qid

    def _hash_of(self, qid: str) -> str | None:
        """The `media SHA-1` the item `qid` carries, or None."""
        pid = self.properties.get("media_sha1")
        if pid is None:
            return None
        for statement in self._entity(qid).claims.get(pid) or []:
            value = (statement.mainsnak.datavalue or {}).get("value")
            if isinstance(value, str) and value:
                return value
        return None

    def _registered(self, qid: str) -> tuple[str, str] | None:
        """(the artifact, the SHA-1 it carries) when the item `qid` is one
        registration made: an artifact, which carries a `media SHA-1`, or
        an `instance of artifact fragment` that is `part of` one, for
        which the artifact is its whole. None otherwise."""
        sha1 = self._hash_of(qid)
        if sha1:
            return qid, sha1
        fragment = self.items.get("artifact_fragment")
        part_of = self.properties.get("part_of")
        if fragment is None or part_of is None:
            return None
        claims = self._entity(qid).claims

        def targets(pid: str) -> list[str]:
            values = (
                (statement.mainsnak.datavalue or {}).get("value")
                for statement in claims.get(pid) or []
            )
            return [
                value["id"]
                for value in values
                if isinstance(value, dict) and "id" in value
            ]

        if fragment not in targets(self.properties["instance_of"]):
            return None
        for whole in targets(part_of):
            sha1 = self._hash_of(whole)
            if sha1:
                return whole, sha1
        return None

    def _artifact_item(self, qid: str) -> bool:
        """Is the item `qid` an artifact or a part of one
        (is_artifact_item)?"""
        claims = self._entity(qid).get_json().get("claims") or {}
        return is_artifact_item(claims, self.properties, self.items)

    def _joins(
        self,
        label: str,
        artifact: bool,
        labels: _FoundLabels,
        planned: Mapping[str, bool],
    ) -> bool:
        """May an entity, on the artifact side (is_artifact_kind) when
        `artifact`, take the item under `label`, as the instance holds its
        items' labels (`labels`) and the batch makes items under the labels
        `planned` (label -> whether an entity on the artifact side makes it)? A
        label nobody holds is free. An entity on the artifact side joins an
        item registration did not make (_registered) that is an artifact or a
        part of one (_artifact_item): an artifact registered before content
        hashing, another media object's item, or a fragment of an artifact with
        no hash. Any other entity joins an item that is neither. Each joins an
        item the batch makes for an entity of its own side."""
        qid = labels.get(label)
        if qid is None:
            return planned.get(label, artifact) == artifact
        if artifact:
            return self._registered(qid) is None and self._artifact_item(qid)
        return not self._artifact_item(qid)

    def _place(
        self,
        label: str,
        kind: str | None,
        labels: _FoundLabels,
        planned: dict[str, bool],
    ) -> _Placement:
        """Where the item of an entity with the label `label` and the kind
        `kind` is found or made (_Placement): under its label when it may
        join the item there (_joins), and otherwise under its tagged label
        (tagged_label), when it may join the item there; and when neither,
        it is blocked (side_label, the rule the resolve step reads an item
        by too, reused_item). An entity on the artifact side
        (is_artifact_kind) that does not take its label is flagged against
        every artifact registered under it (_registered_under), whatever
        item holds the label: an artifact registered while a person held it
        is content-tagged under it all the same. `planned` gains the label
        the batch makes an item under."""
        artifact = is_artifact_kind(kind)
        placed, joined = side_label(
            label,
            kind,
            lambda other: self._joins(other, artifact, labels, planned),
        )
        if joined:
            planned.setdefault(placed, artifact)
        if placed == label:
            return _Placement(label)
        held: tuple[tuple[str, str], ...] = ()
        if artifact:
            held = tuple(self._registered_under(label, labels))
        if joined:
            return _Placement(placed, label, held)
        return _Placement(
            placed,
            label,
            held,
            blocked=(labels.get(label, ""), labels.get(placed, "")),
        )

    def _plan(
        self,
        batch: ClaimBatch,
        refused: Collection[int],
        labels: _FoundLabels,
    ) -> dict[str, _Placement]:
        """Where step 2 puts the item of each local entity of `batch` (_place),
        for the claims the writer writes, all but those at the places
        `refused`: an entity's label and kind are its manifest record's or,
        without one, its first ref's, in the order the writer resolves their
        refs (entity_terms, which the resolve step reads them by too). Every
        entity not on the artifact side (is_artifact_kind) is placed first,
        then every entity on it, each in that order, so that of two entities of
        the batch under one label that no item holds, the person, place or
        other entity takes it and the artifact-kind one its tagged label,
        whichever ref comes first. An entity an approved equivalence names, or
        with no label the instance keeps, has none. `labels` finds the items
        under the labels, before the first write."""
        entities = entity_terms(batch, self._entity_records, refused)
        planned: dict[str, bool] = {}
        placements: dict[str, _Placement] = {}
        for artifact in (False, True):
            for local_id, (label, kind) in entities.items():
                if is_artifact_kind(kind) == artifact:
                    placements[local_id] = self._place(
                        label, kind, labels, planned
                    )
        return placements

    def _blocked(
        self, batch: ClaimBatch, refusals: Mapping[int, str]
    ) -> tuple[dict[int, str], list[str], list[RegistrationFlag]]:
        """The entities of `batch` that may join neither the item under their
        label nor the one under their tagged label (_place), each refused with
        every claim that names it, before the first write; the claims the
        writer refuses already are at the places `refusals`. Returns the places
        of the claims so refused, each with the local id of the first entity
        that refuses it; a warning for each entity, once, naming by their claim
        ids the claims that name it of those the plan it is blocked in writes,
        so a claim that names two blocked entities is named in the warning of
        each, and the two items by QID; and, for each artifact registered under
        such an entity's label, an unhashed-title flag against the item under
        its tagged label. The plan is made again without the refused claims
        until no other entity is blocked (_plan), and settles `_placements`."""
        refused = set(refusals)
        labels = self._labels
        stopped: dict[int, str] = {}
        warnings: list[str] = []
        flags: list[RegistrationFlag] = []
        while True:
            placements = self._plan(batch, refused, labels)
            blocked = {
                local_id: placement
                for local_id, placement in placements.items()
                if placement.blocked
            }
            if not blocked:
                break
            written = [
                (index, {ref.local_id for ref in local_refs(claim)})
                for index, claim in enumerate(batch.claims)
                if index not in refused
            ]
            for local_id, placement in blocked.items():
                named = [index for index, ids in written if local_id in ids]
                for index in named:
                    stopped.setdefault(index, local_id)
                refused.update(named)
                plain, tagged = (
                    qid or "an item this batch makes"
                    for qid in placement.blocked
                )
                warnings.append(
                    f"entity {local_id!r} is refused, with the claims "
                    f"that name it "
                    f"({', '.join(claim_id(batch.claims[i]) for i in named)})"
                    f", and nothing of them is written: it may join neither "
                    f"the item under its label ({plain}) nor the one under "
                    f"its tagged label ({tagged}) (ADR-0010)"
                )
                against = placement.blocked[1]
                for held in placement.held if against else ():
                    self._flag(held, against, flags)
        self._placements = placements
        return stopped, warnings, flags

    def _registered_under(
        self, label: str, labels: _FoundLabels
    ) -> list[tuple[str, str]]:
        """Every artifact (_registered) whose item, or a fragment of which,
        has the label `label` as it is or with its own content tag in it,
        " [", the first 8 hex digits of the artifact's SHA-1 and "]", as
        registration tags a label another item holds
        (yizkr.pipeline.registrar), at its end or, for a fragment of a
        content-tagged artifact, before ", page <n>": a label is one when
        taking out that tag, wherever it is, leaves `label`. So a title
        that holds a bracketed run of 8 hex digits of its own, as a date
        can be, is compared whole, and another artifact titled `label`
        with such a date is not one, as the registrar takes out only its
        own tag. The artifacts registered under that title, in the order
        of their items, each once. The labels are those the write read
        (`labels`), looked up in its index of them (_FoundLabels.titled),
        and only the items under a label that is one are read
        (_registered)."""
        found: list[tuple[str, str]] = []
        for other, qid in labels.titled(label):
            # `other` is `label`, or `label` with a content tag in it
            tags = {
                tag.group()
                for tag in _CONTENT_TAG.finditer(other)
                if other[: tag.start()] + other[tag.end() :] == label
            }
            registered = self._registered(qid)
            if not registered or registered in found:
                continue
            if other == label or f" [{registered[1][:8]}]" in tags:
                found.append(registered)
        return found

    @staticmethod
    def _flag(
        held: tuple[str, str], qid: str, flags: list[RegistrationFlag]
    ) -> None:
        """Gather in `flags` the registered artifact `held`, (item,
        SHA-1), registered under the label of the batch's entity whose
        item, found, made or blocking it, is `qid`, as the registrar flags
        an artifact registered under the label of an item with no hash:
        unhashed-title, once a write."""
        from yizkr.pipeline.registrar import UNHASHED_TITLE, RegistrationFlag

        flag = RegistrationFlag(UNHASHED_TITLE, held[0], held[1], (qid,))
        if flag not in flags:
            flags.append(flag)

    def _flag_held(
        self, placement: _Placement, qid: str, result: WriteResult
    ) -> None:
        """Flag each artifact the placement holds against the item `qid`,
        found or made for it, or its placeholder in a dry run (_flag)."""
        for held in placement.held:
            self._flag(held, qid, result.flags)

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
        # instance holds, never failing the write (one holding a
        # character Python's Unicode tables do not know has refused its
        # claim before: pre_plan_refusals), and one of whitespace alone is
        # left out (_locator_text)
        source = claim.source if claim is not None else None
        page = _locator_text(source.page_number or "") if source else ""
        if page:
            source_ref.add(
                String(prop_nr=self.properties["page_number"], value=page)
            )
        # the page number printed on that page, beside the physical one,
        # where the extractor read it from the transcription's record
        printed = (
            _locator_text(source.printed_page_number or "") if source else ""
        )
        if printed:
            source_ref.add(
                String(
                    prop_nr=self.properties["printed_page_number"],
                    value=printed,
                )
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
        label, description = _run_terms(batch)
        from yizkr.pipeline.registrar import ensure_self_typed

        # the run item's label is its key, and names one item alone
        existing = self._labels.sole(label)
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
        item.descriptions.set("en", description)
        item = item.write(summary="ingest: extraction run")
        self._labels.made(label, item.id)
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
        the statement's blocks: `stated in` with the page number, printed
        page number, region and excerpt. So the same artifact under
        another locator or excerpt, another place where it asserts the
        value, adds a block of its own. A curated statement is compared
        by artifact alone: a block that cites the claim's artifact under
        any locator records the claim. The blocks returned are those the
        statement does not already carry, so a run's block is not
        repeated for a second claim of the same run.
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

        A manifest record names its item by an approved equivalence (`qid`), by
        the exact-label match that materialized its local refs here, or, once
        the resolve step has replaced those refs with the item's QID, by the
        item's English label being the record's label in the form the instance
        keeps labels in (_term_text), or its tagged label (tagged_label) where
        an artifact or a part of one holds the record's label and the writer
        would place the record under the tagged one (reused_item): the match
        the resolve step made. Only in that last case have the record's aliases
        not reached the item yet; under the tagged label, the record's own
        label goes among them, as it does on an item the writer finds
        there. That match pairs only a record not on the artifact side
        (is_artifact_kind) with an item that is no artifact nor a part of one
        (_artifact_item), as resolve matches (yizkr.pipeline.resolve;
        ADR-0010), a record that no claim names as much as any other: a
        person's record never names a media object's item by its label, nor a
        media object's record a registered scan. A record whose label holds
        nothing the instance keeps names no item this way, as an item with no
        English label matches no record, and nor does one whose label holds a
        character Python's Unicode tables do not know anywhere in it, past
        TERM_LIMIT too (unknown_characters), which the instance may keep in
        another form. Nor does a record of which nothing is sent
        (_unsent_records). A local ref without a manifest record that the
        writer matched by exact label gives the kind on its own `instance_of`,
        cited to its own claim's artifact.
        """
        found: list[_FoundEntity] = []
        by_label: dict[str, list[EntityRecord]] = {}
        by_tagged: dict[str, list[tuple[EntityRecord, str]]] = {}
        for record in self._entity_records.values():
            if record.id in self._unsent:
                continue
            qid = record.qid or result.created_items.get(record.id)
            if qid is None:
                stored_label = _term_text(record.label)
                if (
                    stored_label
                    and not unknown_characters(record.label)
                    and not is_artifact_kind(record.kind)
                ):
                    by_label.setdefault(stored_label, []).append(record)
                    by_tagged.setdefault(
                        tagged_label(stored_label, record.kind), []
                    ).append((record, stored_label))
            else:
                found.append(_FoundEntity(qid, record.kind, record.type_hint))
        if by_label:
            for qid in self._mentions:
                if qid in self._minted or self._artifact_item(qid):
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
                # under its tagged label only where the writer would place
                # the record there, its own label then an alias, as on an
                # item it finds there itself
                found.extend(
                    _FoundEntity(
                        qid,
                        record.kind,
                        record.type_hint,
                        (stored_label, *record.aliases),
                    )
                    for record, stored_label in by_tagged.get(label, [])
                    if reused_item(
                        stored_label,
                        record.kind,
                        self._labels.items(),
                        self._artifact_item,
                    )
                    == qid
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
            # and so is a type hint the instance cannot hold, or may keep
            # in another form; one it holds goes with its spacing made
            # single spaces and cleaned up as the instance gives it back
            # (_hint_sent)
            hint = _hint_sent(found.type_hint or "")
            if hint:
                self._record_type_statement(
                    found.qid,
                    String(prop_nr=self.properties["type_hint"], value=hint),
                    artifact_qid,
                    result,
                )

    def _unknown_terms(
        self,
        batch: ClaimBatch,
        refused: Collection[int],
        unnamed: Mapping[str, list[str]],
    ) -> tuple[list[str], list[str]]:
        """The terms of the batch's entities the writer leaves out because
        they hold a character Python's Unicode tables do not know
        (unknown_characters), which the instance may keep in another form:
        each named "<local id>: <term>" (label, description, alias or type
        hint), and a warning for each that names the entity and the
        characters, quoting no text. `unnamed` holds the entities whose
        label keeps the writer from finding or minting their item
        (unnamed_entities): their label is named, and nothing else of them,
        as nothing else of them is sent. An approved equivalence's record
        gives its label as an alias of its item, and no description, as
        the writer sends none to an item it names. A description goes
        only on the item a ref mints, so one is named only when a claim
        the writer writes mentions its local id: a record's own, and for
        a local id without a manifest record, that of the first ref to it
        in such a claim, as `_unknown_kinds` reads its kind. A record's
        aliases and type hint are named whether or not such a claim
        mentions it, as the writer may send them to an item the resolve
        step matched the record to, but for a record of which nothing is
        sent (_unsent_records): the claims that name it by a local ref
        are all refused, before the plan (pre_plan_refusals) or with an
        entity the plan blocks.
        `refused` holds the places in the batch of the claims the writer
        refuses."""
        named: list[str] = []
        warnings: list[str] = []

        def leave_out(local_id: str, term: str, text: str | None) -> None:
            unknown = unknown_characters(text or "")
            if unknown:
                named.append(f"{local_id}: {term}")
                article = "an" if term.startswith("a") else "a"
                warnings.append(
                    f"entity {local_id!r} has {article} {term} that "
                    f"{_unknown_fault(unknown)}: it is left out"
                )

        for local_id, unknown in unnamed.items():
            named.append(f"{local_id}: label")
            warnings.append(
                f"entity {local_id!r} has a label that "
                f"{_unknown_fault(unknown)}: no item is found or made by it, "
                f"and any claim that mentions it is refused"
            )
        first: dict[str, LocalRef] = {}
        for index, claim in enumerate(batch.claims):
            if index in refused:
                continue
            for ref in local_refs(claim):
                first.setdefault(ref.local_id, ref)
        for record in self._entity_records.values():
            if record.id in unnamed:
                continue
            if record.qid:
                leave_out(record.id, "label", record.label)
            elif record.id in first:
                leave_out(record.id, "description", record.summary)
            if record.id in self._unsent:
                continue
            for alias in record.aliases:
                leave_out(record.id, "alias", alias)
            leave_out(record.id, "type hint", record.type_hint)
        for local_id, ref in first.items():
            if local_id not in self._entity_records:
                leave_out(local_id, "description", ref.description)
        return named, warnings

    def _unsent_records(
        self, batch: ClaimBatch, refused: Collection[int]
    ) -> set[str]:
        """The local ids of the manifest records without an approved
        equivalence that claims of `batch` name by a local ref, but only claims
        at the places `refused`, which the writer refuses, before the plan
        (pre_plan_refusals: for their text, or as a misplaced document subject)
        or with an entity the plan blocks (step 2 of the module docstring): no
        item is found or made for such a record, and none of its terms, kind or
        type hint is sent anywhere, nor matched to an item by its label
        (_found_entities). A record counts as written only through a local ref
        to it in a claim the writer writes: one that claim names by the QID
        resolve gave it does not count."""
        named: set[str] = set()
        written: set[str] = set()
        for index, claim in enumerate(batch.claims):
            ids = {ref.local_id for ref in local_refs(claim)}
            named |= ids
            if index not in refused:
                written |= ids
        return {
            record.id
            for record in self._entity_records.values()
            if not record.qid
            and record.id in named
            and record.id not in written
        }

    def _unknown_kinds(
        self, batch: ClaimBatch, refused: Collection[int]
    ) -> list[str]:
        """A warning for each entity whose kind the vocabulary lacks, so
        that no kind is recorded for it. An entity's kind is its manifest
        record's, warned of whatever its claims, or, for a local id without
        one, that of the first ref to the id in a claim the writer writes,
        in the order the writer resolves refs: each claim's subject, then
        its value, then its qualifiers' values; such a kind is warned of
        only when a written claim mentions the id. `refused` holds the
        places in the batch of the claims the writer refuses, whose refs it
        never resolves."""
        kinds: dict[str, str | None] = {
            record.id: record.kind for record in self._entity_records.values()
        }
        for index, claim in enumerate(batch.claims):
            if index in refused:
                continue
            for ref in local_refs(claim):
                kinds.setdefault(ref.local_id, ref.instance_of)
        return [
            f"entity {local_id!r} has the kind {kind!r}, which the "
            f"vocabulary lacks: no kind is recorded for it"
            for local_id, kind in kinds.items()
            if kind and kind not in self.items
        ]

    def _type_hints_over_limit(self) -> list[str]:
        """A warning for each manifest record whose type hint the instance
        cannot hold, as the writer sends it (_hint_text), which is left out
        wherever the writer would record it (the module docstring); none
        for a record of which nothing is sent (_unsent_records), as with a
        type hint that holds a character Python's Unicode tables do not
        know (_unknown_terms)."""
        return [
            f"entity {record.id!r} has a type hint of {len(hint)} "
            f"characters in Unicode NFC, its whitespace as single spaces, "
            f"over the {TEXT_LIMIT} Wikibase holds in a text value: it is "
            f"not recorded, as a cut would narrow what the source says"
            for record in self._entity_records.values()
            if record.id not in self._unsent
            and len(hint := _hint_text(record.type_hint or "")) > TEXT_LIMIT
        ]

    # -- main entry ----------------------------------------------------------

    def write_batch(
        self, batch: ClaimBatch, bulk_accepted: bool = False
    ) -> WriteResult:
        """Write `batch`. Raises UnapprovedDepictsError, before reading
        from or writing to the instance, when a depicts claim in it lacks
        its row approval (the module docstring), in a dry run too,
        UnsupportedDate, then too, when a claim holds an unsupported date
        (unsupported_dates), and ValueError, then too, when the run
        item's label or description holds a character Python's Unicode
        tables do not know (_run_terms). A claim whose own text the
        instance cannot hold raises nothing: it is refused alone, and the
        rest of the batch is written (the module docstring); so is an
        entity that may reuse neither the item under its label nor the one
        under its tagged label, refused with the claims that name it (step
        2 of the module docstring)."""
        unapproved = unapproved_depicts(batch)
        if unapproved:
            raise UnapprovedDepictsError(unapproved)
        unsupported = unsupported_dates(batch)
        if unsupported:
            raise UnsupportedDate(
                "the batch holds unsupported dates, and nothing was "
                "written: " + "; ".join(unsupported)
            )
        # the run item's terms are machine text: one the instance may keep
        # in another form refuses the batch before anything is read or
        # written (_run_terms)
        _run_terms(batch)
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
        # the instance's labels, read once for the write, on first need,
        # and its items read afresh: an item read for an earlier write may
        # have been hashed by a registration since (_registered)
        self._labels = _FoundLabels(self.client)
        self._entity_cache = {}
        # found before the first write, in a dry run as well: the claims
        # whose own text the instance cannot hold, which the loop below
        # leaves out whole
        unnamed = unnamed_entities(batch, self._entity_records)
        refusals = pre_plan_refusals(batch, unnamed)
        # settled before the first write too: where each entity's item is
        # found or made, and the entities that may join neither the item
        # under their label nor the one under their tagged label, refused
        # with the claims that name them (step 2 of the module docstring)
        stopped, blocked, flags = self._blocked(batch, refusals)
        refused = {*refusals, *stopped}
        # the manifest records that only refused claims name, of which
        # nothing is sent
        self._unsent = self._unsent_records(batch, refused)
        # found before the first write too: the kinds the vocabulary lacks
        # (a manifest record's whatever its claims, and the kind of a ref
        # without a record only when a claim the writer writes mentions it:
        # all but those refused before the plan and those the plan
        # refuses), the type hints the instance cannot hold, and the terms
        # it may keep in another form, each skipped wherever the writer
        # would record it
        warnings = self._unknown_kinds(batch, refused)
        warnings += [
            *refusals.values(),
            *blocked,
            *self._type_hints_over_limit(),
        ]
        refused_terms, term_warnings = self._unknown_terms(
            batch, refused, unnamed
        )
        warnings += term_warnings
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
            run_qid=self.ensure_run_item(batch),
            warnings=warnings,
            flags=flags,
            refused_terms=refused_terms,
        )
        for index, claim in enumerate(batch.claims):
            if index in refused:
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
