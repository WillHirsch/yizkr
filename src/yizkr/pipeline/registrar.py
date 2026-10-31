"""Artifact registrar: media upload, and artifact, page-fragment and
derived-media items, each found or made by the SHA-1 of its media
(ADR-0003 §4, ADR-0010).

Registration is part of the write (ADR-0025 §2): `ingest run` registers
a document just before it writes the claims that cite it. The claims cite
an artifact by the SHA-1 of its media, and the writer resolves that key to
the item whose `media SHA-1` statement registration writes. Per artifact,
registration writes:

* the media file, uploaded to the wiki (W1: artifacts as wiki media,
  ADR-0004) under its file name in the form MediaWiki takes as it is
  (_upload_name): every letter kept, each character or sequence
  MediaWiki refuses made "-", as is the dot before an extension it
  refuses within the name, and, when too long, cut, ending in "…", to
  fit MediaWiki's limit with room for a content tag, its extension kept;
* an artifact item: label, description (cut to the 250 characters
  Wikibase takes, ending in "…"), ``instance of -> <type>``,
  ``media file -> File:...`` and ``media SHA-1 -> <hex digest>``;
* optional page-fragment items: ``instance of -> artifact fragment``,
  ``part of -> <artifact>`` with a ``page number`` qualifier;
* for a compound artifact (ADR-0008), the sources it places that its
  common container projection binds to owners
  (yizkr.pipeline.container_sources): each an artifact item of its own,
  with its media file and SHA-1, ``part of`` the container once per slide
  of a deck, or page of a PDF, that paints it, that statement's reference
  carrying the source-part locators.

**Owner lookup uses canonical media SHA-1.** An artifact is found by its
recorded media bytes; a title never finds an item. Common container
projection supplies verified owner bindings separately from current reading
representations. A generated image may reuse an existing graph owner only
with matching original-part composition evidence; identical rendered pixels
alone do not establish source equivalence. A new item whose title is
already another item's label takes
a short content tag, ``"<title> [<sha1[:8]>]"``: a label convention the
writer's flagging of registered artifacts reads, and that keeps clear of
Wikibase's refusal of two items with one label and description, not a
requirement of the wiki client's label index. A page
fragment is keyed by its artifact: the item that is an ``instance of
artifact fragment`` and ``part of`` that artifact with that page's number.
So same-titled artifacts never share a fragment, and a derived medium,
which is ``part of`` its container with a page qualifier too, is never
taken for a page's fragment. A file on the never-ingest list
(yizkr.core.corpus) is never registered or uploaded: registration refuses
it before it asks anything of the wiki, and leaves out an embedded medium
the list names.

**An item that may be an older artifact is flagged, never adopted.**
Each registration of an artifact or a derived medium, of an item made or
found, reports it for a person to look at (RegistrationFlag), raised by
`ingest run` as an alert of the write (docs/ANOMALIES.md), while either
condition holds: another item names as its `media file` a wiki file that
holds its bytes (``shared-file``; every such file, as MediaWiki lists
them by their SHA-1), or its title, its own label before its content
tag, or the label one of its page fragments has before its content tag,
is the label of an item with no hash, as it is or with the tag the
writer gives a media object's item under a label it may not take
(``unhashed-title``). An item
with no hash has no `media SHA-1` and either names a `media file`, as an
artifact registered before content hashing does, or is an `instance of`
an artifact kind, as the item the GEDCOM import makes for a media object
is (a label, no file); an artifact fragment, a run item or a person
naming no file is never one. So a flag is raised again on every write
that registers the item until a curator acts, by hashing, merging or
relabelling the older item, and a run cut short before it raised one
raises it on the next. A flag changes nothing registration finds or
writes: the item is made, and labelled, as any other, and the older item
is left as it is. The flags are sought once the item, and an artifact's
page fragments, are complete, unhashed-title first, as it needs nothing
of the wiki; a file listing that fails then fails the document's
registration for that run, as any other wiki call that fails does, with
unhashed-title gathered already, and the next write raises shared-file.

On a reset instance, where only registration writes `media file`, always
with `media SHA-1`, shared-file is never raised: an item naming a file
that holds these bytes carries this SHA-1, as an upload never overwrites
a file, and is this item. Unhashed-title is raised there only for an
artifact or derived medium registered under exactly the label of a media
object, or an artifact one of whose page fragments has that label, in
either order of the writes. A GEDCOM written after the artifact never
joins its media object to it, or to its fragment, by that label: the
writer makes the media object an item of its own under the label with a
tag at its end (`media_object_label` in yizkr.pipeline.writer) and
flags the pair itself, the artifact against the media object's item, as
registration flags it, and with it every artifact registered under that
label with its own content tag in it ([sha1[:8]] of its media, or of
the fragment's whole), at its end or before a fragment's ", page <n>";
and registration compares each title with that tagged label
too, so the flag is raised on every later write of either. The writer
also raises it for a media object it refuses, as registered items hold
both its label and its tagged label: each artifact registered under the
label, against the item under the tagged label, which is then a
registered item with a hash, not one without.

Provenance: an artifact asserts its own kind and its own composition by
existing, so its `instance of`, `media file` and `media SHA-1`, and its
fragments' `instance of` and `part of`, are referenced ``stated in -> the
artifact item itself``; every statement a container contributes to a
medium it embeds is referenced ``stated in -> that container`` (see
docs/MODELING.md, "Artifacts and registration"). No statement is written
unreferenced (ADR-0003). A run item states its kind by the same
convention, in a write after the one that makes it, and
`ensure_self_typed` gives it its kind, whether it was just made or a
write cut short left it without one.

Idempotent. A Registrar reads the instance once, lag-free
(`WikiClient.iter_entities`), and keeps what it read current with what it
writes. A statement that exists already is skipped when it cites the
source that asserts it, and otherwise gains that source's reference, as
an agreeing source's (ADR-0002); nothing existing is changed or removed
(CLAUDE.md rule 3). A title, or a derived medium's name, that holds a
character Python's Unicode tables do not know never becomes an alias
(writer.unknown_characters): the instance may keep it in another form,
and it would look missing on every registration. A new item's label
made from such a title always takes the media's content tag, as a label
another item has does (Registrar._free_label), so that two same-named
copies never share a label and description the instance refuses. The
bytes are uploaded only when no file of the item holds them, and an
upload never overwrites a wiki file or stores bytes the wiki already
holds (`WikiClient.upload_file`), so every `media file` holds the bytes
its item's `media SHA-1` names.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from yizkr.claims import (
    PlacementSupport,
    SourceSupport,
    source_support_locators,
)
from yizkr.core.corpus import ExclusionList, file_sha1, is_excluded
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.presentation import IMAGE_SUFFIXES, DerivedMediaRecord

# a title is compared with the instance's labels, and set, in the form
# Wikibase stores a label, a description or an alias in, by the writer's
# rule (_stored_term), and TERM_LIMIT is the most characters it takes in
# one: in any other form, a label another item holds would look free, and
# an alias the item has would look missing; a cut keeps whole letters by
# the writer's rule too (extends, half_flag), and a name holding a
# character Python's Unicode tables do not know, which the instance may
# keep in another form, never looks missing (unknown_characters); a
# title, and each fragment's label, is compared with a media object's
# tagged label too (media_object_label)
from yizkr.pipeline.writer import (
    TERM_LIMIT,
    TEXT_LIMIT,
    extends,
    half_flag,
    media_object_label,
    unknown_characters,
)
from yizkr.pipeline.writer import stored_term as _stored_term

if TYPE_CHECKING:
    from yizkr.pipeline.container_sources import ContainerSources


@dataclass
class _RegistrationMedium:
    names: list[str]
    slides: list[int]
    file: str
    locators: dict[int, tuple[str, ...]] = field(default_factory=dict)
    unplaced_locators: tuple[str, ...] = ()


@dataclass
class _RegistrationMedia:
    source_sha1: str
    container: str
    title: str
    media: dict[str, _RegistrationMedium]


# the categories of the characters that show nothing: format characters
# (such as the zero-width space and the bidirectional marks), controls,
# and separators
_UNSEEN = ("Cf", "Cc", "Zs", "Zl", "Zp")


def _shows(name: str) -> bool:
    """Whether `name`, in the form Wikibase stores a term in, holds a
    character that shows: one a label can end in and a reader see."""
    return any(
        unicodedata.category(char) not in _UNSEEN
        for char in _stored_term(name)
    )


def _whole_letters(text: str, cut: str) -> str:
    """`cut`, a start of `text`, backed off until it ends after a whole
    letter: never before a character that extends the letter it ends in
    (writer.extends), which keeps a zero-width joiner with the letters either
    side of it, and never halfway through a pair of regional indicators.
    A cut that would keep a letter without its accents keeps neither,
    and one that keeps no whole letter is empty."""
    while cut and (
        (len(cut) < len(text) and extends(text[len(cut)], cut[-1]))
        or half_flag(cut)
    ):
        cut = cut[:-1]
    return cut


def _fitted(
    text: str, suffix: str = "", *, prefix: str = "", at: str | None = None
) -> str:
    """`prefix + text + suffix`, whose parts are in the stored form
    (_stored_term), within the TERM_LIMIT characters Wikibase takes: when
    the whole is longer, `text` is cut, at the last `at` within the room
    when `at` is given, never within a letter (_whole_letters), and ends
    in "…". The result is in the stored form
    too, and the same parts always give the same result. For text that
    only displays, such as a deck's title in the labels of its slides and
    images: nothing finds an item by it."""
    whole = prefix + text + suffix
    room = TERM_LIMIT - len(prefix) - len(suffix) - 1
    if len(whole) <= TERM_LIMIT or room < 1:
        return whole
    cut = _whole_letters(text, text[:room].rstrip())
    if at is not None and at in cut:
        cut = cut.rsplit(at, 1)[0]
    return prefix + cut.rstrip() + "…" + suffix


# the most bytes MediaWiki takes in an uploaded file's name, counted in
# UTF-8 on the name the upload asks for, once in NFC (UploadBase::getTitle:
# an archived version's name adds a timestamp and "!" within 255 bytes)
FILE_NAME_LIMIT = 240
# the most bytes a name made for an upload takes: when that name holds
# other bytes, WikiClient.upload_file uploads under it with a content tag,
# " <sha1[:8]>", 9 bytes, before its extension, and that name fits too
UPLOAD_NAME_BYTES = FILE_NAME_LIMIT - 9

# the bidirectional marks MediaWiki strips from a title (TitleParser)
_FILE_NAME_BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e]")
# the characters MediaWiki reads as a space in a title, "_" among them
# (TitleParser), and the other whitespace
_FILE_NAME_SPACES = re.compile(
    r"[\s_\u00a0\u1680\u180e\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)
# the characters a file name cannot hold as they are, each made "-":
# * the ASCII outside MediaWiki's title characters, "#<>[]{}|" and the
#   controls ($wgLegalTitleChars), and ":", "/" and "\"
#   ($wgIllegalFileChars), which an upload replaces with "-" or, for most
#   controls, refuses;
# * the replacement character, and U+FFFE and U+FFFF, which the request's
#   normalisation makes one (UtfNormal), and which a title refuses;
# * the controls beyond ASCII, which show as nothing, and lone surrogates,
#   which UTF-8 cannot encode
_FILE_NAME_ILLEGAL = re.compile(
    r"[#<>\[\]{}|:/\\\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffd-\uffff]"
)
# the start of each sequence a title refuses (TitleParser): a "%" hex
# escape, an HTML character reference and three tildes
_FILE_NAME_SEQUENCES = re.compile(
    r"%(?=[0-9A-Fa-f]{2})|&(?=[A-Za-z0-9\u0080-\U0010ffff]+;)|~(?=~~)"
)
# the extensions MediaWiki refuses after any dot of a file's name: the
# default $wgProhibitedFileExtensions of MediaWiki 1.45.4
# (includes/MainConfigSchema.php), which the deploy's settings leave as
# it is
_PROHIBITED_EXTENSIONS = frozenset(
    {
        "html", "htm", "js", "jsb", "mhtml", "mht", "xhtml", "xht",
        "php", "phtml", "php3", "php4", "php5", "phps", "phar",
        "shtml", "jhtml", "pl", "py", "cgi",
        "exe", "scr", "dll", "msi", "vbs", "bat", "com", "pif", "cmd", "vxd",
        "cpl",
        "xml",
    }
)  # fmt: skip


def _file_name_part(text: str) -> str:
    """`text` as part of a file name MediaWiki takes as it is: the
    bidirectional marks MediaWiki strips left out, then in NFC, every
    letter kept (a title takes every character beyond ASCII), each run
    of whitespace and underscores one space, each character or sequence
    it refuses (_FILE_NAME_ILLEGAL, _FILE_NAME_SEQUENCES) made "-", and
    no space at either end. A part that would start with a dot starts
    with "-" before it, its dot kept: a file whose name starts with a dot
    is a hidden one on the wiki's disk, MediaWiki refuses one with
    nothing before its extension's dot (``filename-tooshort``), and the
    dot may be the extension's."""
    text = unicodedata.normalize("NFC", _FILE_NAME_BIDI.sub("", text))
    text = _FILE_NAME_ILLEGAL.sub("-", _FILE_NAME_SPACES.sub(" ", text))
    text = _FILE_NAME_SEQUENCES.sub("-", text).strip(" ")
    return f"-{text}" if text.startswith(".") else text


def _cut_to_fit(text: str, end: str) -> str:
    """The longest start of `text`, in whole letters (_whole_letters),
    that leaves room for `end` within UPLOAD_NAME_BYTES, none of it when
    `end` takes all that room or more, its end space trimmed, then
    `end`."""
    room = max(0, UPLOAD_NAME_BYTES - len(end.encode()))
    cut = text.encode()[:room].decode(errors="ignore").rstrip(" ")
    return _whole_letters(text, cut).rstrip(" ") + end


def _unprohibited(name: str) -> str:
    """`name` with the dot before each part MediaWiki refuses, but its
    last, made "-", which takes the dot's one byte. MediaWiki splits a
    file's name at every dot and refuses the upload
    (``filetype-banned``) when a part after the first, as the title
    holds it (each space "_") and in ASCII lower case, is one of
    _PROHIBITED_EXTENSIONS (UploadBase::getTitle, splitExtensions and
    checkFileExtensionList). The last part is the file's extension, its
    type: one MediaWiki refuses is left as it is, and the upload is
    refused."""
    first, *parts = name.split(".")
    kept = first
    for at, part in enumerate(parts, start=1):
        # bytes.lower() changes the ASCII letters alone, as PHP's
        # strtolower does
        key = part.replace(" ", "_").encode().lower().decode()
        refused = at < len(parts) and key in _PROHIBITED_EXTENSIONS
        kept += ("-" if refused else ".") + part
    return kept


def _upload_name(name: str, title: str = "", fallback: str = "") -> str:
    """The name the bytes of a medium called `name` (with its extension)
    are uploaded under: ``"<title> <name>"``, or `name` alone with no
    title, each part in the form MediaWiki takes as it is
    (_file_name_part), and `fallback` for a title that form leaves
    nothing of. A `name` that form leaves nothing of leaves the title to
    stand as the name, and with no title either the name is "-", never
    empty: WikiClient.upload_file takes the file's own name, as it is,
    for an empty one. The dot before each part MediaWiki refuses as an
    extension, but the last, is "-", the parts those of the whole name
    as it is cut (_unprohibited).

    It takes at most UPLOAD_NAME_BYTES, so its content-tagged form fits
    MediaWiki's limit too (_within_limit). The same parts always give
    the same name."""
    name = _file_name_part(name)
    title = _file_name_part(title) or _file_name_part(fallback)
    if not name:
        title, name = "", title
    return _unprohibited(_within_limit(title, name)) or "-"


def _within_limit(title: str, name: str) -> str:
    """``"<title> <name>"``, or `name` alone with no title, within
    UPLOAD_NAME_BYTES. A longer one is cut, with "…" where the cut is:
    the title, as far as ``"… <name>"``, keeping `name` whole; past that,
    `name` alone, the title left out, when it fits, so a name that fits
    on its own is never cut; and a longer `name` before its extension
    or, when its extension leaves no room, at its end."""
    whole = " ".join(part for part in (title, name) if part)
    if len(whole.encode()) <= UPLOAD_NAME_BYTES:
        return whole
    if title and len(f"… {name}".encode()) <= UPLOAD_NAME_BYTES:
        return _cut_to_fit(title, f"… {name}")
    if len(name.encode()) <= UPLOAD_NAME_BYTES:
        return name
    path = PurePosixPath(name)
    if len(f"…{path.suffix}".encode()) <= UPLOAD_NAME_BYTES:
        return _cut_to_fit(path.stem, f"…{path.suffix}")
    return _cut_to_fit(name, "…")


class ExcludedMedia(RuntimeError):
    """Media on the never-ingest list: registration refuses it before it
    reads the wiki, writes to it or uploads anything."""


# the kinds of RegistrationFlag, each the name of the alert it is raised as
SHARED_FILE = "shared-file"
UNHASHED_TITLE = "unhashed-title"
REGISTRATION_CHECKS = frozenset({SHARED_FILE, UNHASHED_TITLE})


@dataclass(frozen=True)
class RegistrationFlag:
    """An item registration made or found that may be an older artifact,
    for a person to look at; the flag changes nothing registration writes.

    `kind` is SHARED_FILE when the `others` name as their `media file` a
    wiki file that holds the item's bytes, and UNHASHED_TITLE when its
    title, or its own label before its content tag, is the label of the
    `others`, items with no hash (`_Instance.unhashed`); the writer
    raises it too for a media object it refuses, the `others` then the
    item under the media object's tagged label, a registered item with a
    hash (BatchWriter._blocked in yizkr.pipeline.writer). `artifact` is
    the item, and `sha1` the SHA-1 of its media. Every field is an id or
    a hash, none a title or a file name, so a flag can be raised as an
    alert as it is."""

    kind: str
    artifact: str
    sha1: str
    others: tuple[str, ...]


@dataclass
class RegistrationResult:
    artifact_qid: str
    file_page: str | None
    fragment_qids: dict[int, str] = field(default_factory=dict)
    created: bool = True
    # the SHA-1 of the media's bytes: the artifact's identity (ADR-0010)
    content_sha1: str | None = None
    # what the artifact item was flagged for by this registration, made or
    # found: empty when neither condition holds
    flags: list[RegistrationFlag] = field(default_factory=list)


def _append(entity, claim) -> None:
    """Add a claim WITHOUT touching existing ones (attributed statements
    coexist and are never overwritten — ADR-0002): WBI's
    claims.add() defaults to REPLACE_ALL for same-property claims."""
    from wikibaseintegrator.wbi_enums import ActionIfExists

    entity.claims.add(claim, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)


def _existing_claim_targets(entity, pid: str) -> set[str]:
    targets = set()
    for claim in entity.claims.get(pid) or []:
        value = (claim.mainsnak.datavalue or {}).get("value", {})
        if isinstance(value, dict) and "id" in value:
            targets.add(value["id"])
    return targets


def _existing_claim_strings(entity, pid: str) -> set[str]:
    values = set()
    for claim in entity.claims.get(pid) or []:
        value = (claim.mainsnak.datavalue or {}).get("value")
        if isinstance(value, str):
            values.add(value)
    return values


def _statement_key(claim) -> tuple:
    """Property, value and qualifiers of a statement, ignoring its
    references and snak hashes. Same value with different qualifiers is
    a different assertion (ADR-0002), as in the writer's matching."""

    def value_key(datavalue) -> str:
        value = (datavalue or {}).get("value")
        if isinstance(value, dict) and "id" in value:
            return value["id"]
        return json.dumps(value, sort_keys=True)

    qualifiers = sorted(
        (pid, value_key(snak.datavalue))
        for pid, snaks in claim.qualifiers.qualifiers.items()
        for snak in snaks
    )
    return (
        claim.mainsnak.property_number,
        value_key(claim.mainsnak.datavalue),
        tuple(qualifiers),
    )


def _ensure_referenced(
    entity, claim, stated_in_pid: str, source_qid: str
) -> str:
    """Make `entity` carry `claim` as asserted by `source_qid`, additively.

    * no equal statement (same property, value AND qualifiers): `claim`
      is appended as a new statement -> "added";
    * an equal statement exists but lacks a proposed reference's facts:
      that block is appended, preserving earlier evidence while adding a
      source or a more precise native locator -> "referenced";
    * every proposed reference's facts are already held -> "present".

    Existing statements and their references are never removed or
    rewritten (CLAUDE.md rule 3). The caller writes the entity.
    """
    key = _statement_key(claim)
    for existing in entity.claims.get(claim.mainsnak.property_number):
        if _statement_key(existing) != key:
            continue
        added = False
        for reference in claim.references.references:
            wanted = _reference_snaks(reference)
            if any(
                wanted <= _reference_snaks(held)
                for held in existing.references.references
            ):
                continue
            existing.references.add(reference)
            added = True
        return "referenced" if added else "present"
    _append(entity, claim)
    return "added"


def _reference_snaks(reference) -> frozenset[str]:
    """Reference facts, ignoring API hashes and order."""
    return frozenset(
        json.dumps(snak, sort_keys=True, separators=(",", ":"))
        for snaks in reference.get_json()["snaks"].values()
        for snak in snaks
    )


def _composition_source_parts(
    entity, properties: Mapping[str, str], container: str, source_sha1: str
) -> set[str]:
    """Original parts that this item already records as its composition.

    A matching caption or other claim citation is not an ownership binding.
    Only the container's own located ``part of`` evidence establishes it.
    """
    locator_pid = properties.get("source_part_locator")
    if locator_pid is None:
        return set()
    result = set()
    for claim in entity.claims.get(properties["part_of"]) or ():
        target = (claim.mainsnak.datavalue or {}).get("value", {})
        if not isinstance(target, dict) or target.get("id") != container:
            continue
        for reference in claim.references.references:
            if not any(
                isinstance(value := (snak.datavalue or {}).get("value"), dict)
                and value.get("id") == container
                for snak in reference.snaks.get(properties["stated_in"]) or ()
            ):
                continue
            for snak in reference.snaks.get(locator_pid) or ():
                try:
                    value = json.loads((snak.datavalue or {}).get("value"))
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(value, dict)
                    and value.get("container") == source_sha1
                    and isinstance(value.get("part"), str)
                ):
                    result.add(value["part"])
    return result


def _item_claim(pid: str, qid: str, stated_in_pid: str, source_qid: str):
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References

    reference = Reference()
    reference.add(Item(prop_nr=stated_in_pid, value=source_qid))
    references = References()
    references.add(reference)
    return Item(prop_nr=pid, value=qid, references=references)


def _string_claim(pid: str, value: str, stated_in_pid: str, source_qid: str):
    from wikibaseintegrator.datatypes import Item, String
    from wikibaseintegrator.models import Reference, References

    reference = Reference()
    reference.add(Item(prop_nr=stated_in_pid, value=source_qid))
    references = References()
    references.add(reference)
    return String(prop_nr=pid, value=value, references=references)


def _part_of_claim(
    part_of: str,
    page_number: str,
    stated_in: str,
    whole: str,
    page: int | None,
    *,
    locator_property: str | None = None,
    locators: Iterable[str] = (),
):
    """``part of -> whole``, located on a page only when known."""
    from wikibaseintegrator.datatypes import String

    claim = _item_claim(part_of, whole, stated_in, whole)
    if page is not None:
        claim.qualifiers.add(String(prop_nr=page_number, value=str(page)))
    for locator in locators:
        if locator_property is None or len(locator) > TEXT_LIMIT:
            raise ValueError(
                "source-part locator cannot be omitted or truncated"
            )
        for reference in claim.references.references:
            reference.add(String(prop_nr=locator_property, value=locator))
    return claim


# -- an item's kind, stated in the item itself --------------------------------


def ensure_self_typed(
    client: WikiClient,
    qid: str,
    kind: str,
    *,
    summary: str,
    dry_run: bool = False,
    marks: Mapping[str, str] | None = None,
) -> bool:
    """Make the item `qid` state its kind, ``instance of -> kind`` (a
    manifest item key), referenced ``stated in -> the item itself``: a run
    item is the document of its own execution, and asserts its kind by
    existing, as an artifact does (docs/MODELING.md, "Artifacts and
    registration").

    The statement cites the item, so it is written once the item has an
    id, in a write after the one that makes it. A write cut short between
    the two leaves an item with no kind, which the non-privileged export leaves
    out (yizkr.backup), and the statements that cite it lose that
    reference there. So whatever finds such an item again, as a re-run
    finds its run item by its label, calls this, and the item gets its
    kind then. An item that states ``instance of -> kind`` already, at any
    rank and under any reference, is left as it is: a curator's
    deprecation of that statement, or a reference of their own on it,
    stands (CLAUDE.md rules 3 and 4).

    `marks` gives string statements the item states of itself as well,
    by property key: a scratch run's run item names the run and the
    hashes of its run manifest and identity map (ADR-0025 §1). Each is
    stated in the item itself, in the same write as its kind, and one the
    item states already with that value, at any rank and under any
    reference, is left as it is. Returns whether the item lacked a
    statement; a dry run reads the item and writes nothing.
    """
    properties, items = client.vocabulary()
    stated_in = properties["stated_in"]
    instance_of, kind_qid = properties["instance_of"], items[kind]
    item = client.wbi.item.get(qid)
    missing = []
    if kind_qid not in _existing_claim_targets(item, instance_of):
        missing.append(_item_claim(instance_of, kind_qid, stated_in, qid))
    for key, value in (marks or {}).items():
        pid = properties[key]
        if value not in _existing_claim_strings(item, pid):
            missing.append(_string_claim(pid, value, stated_in, qid))
    if not missing:
        return False
    if not dry_run:
        for claim in missing:
            _append(item, claim)
        item.write(summary=summary)
    return True


def _files_holding(
    client: WikiClient, file_pages: set[str], content_sha1: str
) -> list[str]:
    """The wiki files among `file_pages` whose current bytes have this
    SHA-1, by MediaWiki's own hash of each file (imageinfo)."""
    if not file_pages:
        return []
    data = client.api_get(
        {
            "action": "query",
            "titles": "|".join(sorted(file_pages)),
            "prop": "imageinfo",
            "iiprop": "sha1",
        }
    )
    query = data.get("query", {})
    # map API-normalised titles back to the statement values asked for
    asked = {
        entry["to"]: entry["from"] for entry in query.get("normalized", [])
    }
    return sorted(
        asked.get(page["title"], page["title"])
        for page in query.get("pages", {}).values()
        if (page.get("imageinfo") or [{}])[0].get("sha1") == content_sha1
    )


def _files_with_bytes(client: WikiClient, content_sha1: str) -> list[str]:
    """Every wiki file whose current bytes have this SHA-1, by MediaWiki's
    own hash of each file (list=allimages), each as its page title."""
    params = {
        "action": "query",
        "list": "allimages",
        "aisha1": content_sha1,
        "ailimit": "max",
    }
    pages: list[str] = []
    seen: set[str] = set()
    while True:
        data = client.api_get(params)
        pages += [
            image["title"]
            for image in (data.get("query") or {}).get("allimages") or []
        ]
        more = (data.get("continue") or {}).get("aicontinue")
        if not more:
            return pages
        # a cursor the listing gave before would ask the same page again,
        # and again: the listing fails, as a wiki call that fails does
        if more in seen:
            raise RuntimeError(
                f"the file listing for SHA-1 {content_sha1[:12]} gave one "
                f"continuation twice: its files could not be read"
            )
        seen.add(more)
        params = {**params, "aicontinue": more}


# -- the instance, as registration finds items in it --------------------------


def _value(snak):
    """A snak's value, or None for a snak without one (no value, some
    value)."""
    return ((snak or {}).get("datavalue") or {}).get("value")


def _earliest(qids: list[str]) -> str:
    return min(qids, key=_order)


def _order(qid: str) -> int:
    return int(qid[1:])


def _file_key(page: str) -> str:
    """A `media file` value in one form for every way of writing the one
    wiki file, as MediaWiki takes a page title: in NFC, the bidirectional
    marks its title parser strips left out (_FILE_NAME_BIDI), without its
    ``File:`` prefix, underscores and runs of spaces made one space, and
    its first letter upper-cased."""
    name = unicodedata.normalize("NFC", _FILE_NAME_BIDI.sub("", page)).strip()
    if name[:5].lower() == "file:":
        name = name[5:]
    name = " ".join(name.replace("_", " ").split())
    return name[:1].upper() + name[1:]


@dataclass
class _Instance:
    """What registration finds items by, read from the instance once and
    kept current with what the registrar writes. Where two items carry
    one key (a duplicate the keys exist to prevent), the numerically
    earliest is taken, as `WikiClient.string_statement_index` takes it."""

    # English label -> the items that carry it
    labels: dict[str, set[str]] = field(default_factory=dict)
    # media SHA-1 -> the item that carries it
    by_sha1: dict[str, str] = field(default_factory=dict)
    # (artifact, page number) -> the artifact's fragment for that page
    fragments: dict[tuple[str, str], str] = field(default_factory=dict)
    # a wiki file (_file_key) -> the items whose `media file` names it
    files: dict[str, set[str]] = field(default_factory=dict)
    # the items with no hash: each has no `media SHA-1` and names a `media
    # file`, as an artifact registered before content hashing does, or is
    # an `instance of` an artifact kind, as a GEDCOM media object's item is
    unhashed: set[str] = field(default_factory=set)

    @classmethod
    def read(
        cls,
        client: WikiClient,
        properties: dict[str, str],
        items: dict[str, str],
    ) -> _Instance:
        """Every item's English label, `media file` and `media SHA-1`
        values, whether it is an `instance of` an artifact kind (artifact,
        or a class the manifest makes a `subclass_of` it) and, for an
        `instance of artifact fragment`, each `part of` target with each
        `page number` of that statement."""
        from yizkr.pipeline.transcription_record import artifact_classes

        instance_of, part_of = properties["instance_of"], properties["part_of"]
        page_number = properties["page_number"]
        media_file = properties["media_file"]
        media_sha1 = properties["media_sha1"]
        fragment_class = items["artifact_fragment"]
        artifact_kinds = {
            items[key]
            for key in ("artifact", *artifact_classes())
            if key in items
        }
        labels: dict[str, set[str]] = {}
        hashes: dict[str, list[str]] = {}
        fragments: dict[tuple[str, str], list[str]] = {}
        files: dict[str, set[str]] = {}
        unhashed: set[str] = set()
        for qid, entity in client.iter_entities("labels|claims"):
            if not qid.startswith("Q"):
                continue  # a property's label is of another kind
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            if isinstance(label, str) and label:
                labels.setdefault(label, set()).add(qid)
            claims = entity.get("claims") or {}
            hashed = named = False
            for claim in claims.get(media_sha1) or []:
                value = _value(claim.get("mainsnak"))
                if isinstance(value, str):
                    hashes.setdefault(value, []).append(qid)
                    hashed = True
            for claim in claims.get(media_file) or []:
                value = _value(claim.get("mainsnak"))
                if isinstance(value, str) and _file_key(value):
                    files.setdefault(_file_key(value), set()).add(qid)
                    named = True
            kinds = {
                value.get("id")
                for claim in claims.get(instance_of) or []
                if isinstance(value := _value(claim.get("mainsnak")), dict)
            }
            if not hashed and (named or kinds & artifact_kinds):
                unhashed.add(qid)
            if fragment_class not in kinds:
                continue
            for claim in claims.get(part_of) or []:
                whole = _value(claim.get("mainsnak"))
                if not isinstance(whole, dict) or "id" not in whole:
                    continue
                qualifiers = claim.get("qualifiers") or {}
                for snak in qualifiers.get(page_number) or []:
                    page = _value(snak)
                    if isinstance(page, str):
                        fragments.setdefault((whole["id"], page), []).append(
                            qid
                        )
        return cls(
            labels=labels,
            by_sha1={key: _earliest(qids) for key, qids in hashes.items()},
            fragments={
                key: _earliest(qids) for key, qids in fragments.items()
            },
            files=files,
            unhashed=unhashed,
        )

    def label(self, qid: str, label: str) -> None:
        self.labels.setdefault(label, set()).add(qid)

    def file(self, qid: str, page: str) -> None:
        self.files.setdefault(_file_key(page), set()).add(qid)

    def file_flags(
        self, qid: str, sha1: str, pages: Iterable[str]
    ) -> list[RegistrationFlag]:
        """What the item `qid`, of the media with SHA-1 `sha1`, is flagged
        for by its files: the other items that name any of `pages`, the
        wiki files that hold its bytes (shared-file). It changes
        nothing."""
        flags = []
        sharing = set().union(
            *(self.files.get(_file_key(page), set()) for page in pages)
        ) - {qid}
        if sharing:
            flags.append(
                RegistrationFlag(
                    SHARED_FILE, qid, sha1, tuple(sorted(sharing, key=_order))
                )
            )
        return flags

    def title_flags(
        self, qid: str, sha1: str, titles: Iterable[str]
    ) -> list[RegistrationFlag]:
        """What the item `qid`, of the media with SHA-1 `sha1`, is flagged
        for by its names: the items with no hash labelled any of `titles`,
        its title and its label before any content tag (unhashed-title).
        It changes nothing."""
        flags = []
        homonyms = set().union(
            *(self.labels.get(title, set()) for title in titles)
        )
        homonyms = (homonyms & self.unhashed) - {qid}
        if homonyms:
            flags.append(
                RegistrationFlag(
                    UNHASHED_TITLE,
                    qid,
                    sha1,
                    tuple(sorted(homonyms, key=_order)),
                )
            )
        return flags


# -- registration -------------------------------------------------------------


class Registrar:
    """Registers artifacts, their page fragments and the media a compound
    artifact embeds on one instance, each by the SHA-1 of its media.

    `exclusions` is the never-ingest list; None registers without one, as
    the single-file steps run when none is configured. The instance is
    read on first need, once, and what was read is kept current with what
    this registrar writes: one registrar serves any number of artifacts,
    while nothing else writes to the instance. `flags` gathers what each
    item it registered, made or found, a derived medium included, was
    flagged for, in the order the items were registered: each kind of flag
    on an item once, however often the item is registered."""

    def __init__(
        self, client: WikiClient, *, exclusions: ExclusionList | None = None
    ) -> None:
        self.client = client
        self.exclusions = exclusions
        self._instance: _Instance | None = None
        self.flags: list[RegistrationFlag] = []

    def _flag(self, flags: list[RegistrationFlag]) -> None:
        """Gather `flags`, but a kind of flag already gathered on its item."""
        raised = {(flag.kind, flag.artifact) for flag in self.flags}
        self.flags.extend(
            flag for flag in flags if (flag.kind, flag.artifact) not in raised
        )

    def _flags(
        self,
        qid: str,
        sha1: str,
        held: Iterable[str],
        titles: Iterable[str],
        pages: Iterable[int] = (),
    ) -> list[RegistrationFlag]:
        """What the item `qid` is flagged for, each flag gathered (_flag)
        as it is found. First by its names (_Instance.title_flags), each
        title compared with and without its content tag, and as the label
        of its fragment for each of `pages` (_fragment) is before its
        content tag, each of those with the writer's tag for a media
        object's item too (media_object_label), which needs nothing of the
        wiki; then by its files
        (_Instance.file_flags), its bytes, of SHA-1 `sha1`, held by the
        files `held` and by every file MediaWiki lists for that SHA-1
        (_files_with_bytes). A listing that fails fails the registration,
        as any other wiki call does, with the flags by its names gathered
        already."""
        instance = self._read()
        tag = f" [{sha1[:8]}]"
        titles = {title for title in titles if title}
        names = {title.removesuffix(tag) for title in titles}
        names |= {
            _fitted(name, f", page {page}")
            for name in names | titles
            for page in pages
        }
        # and under each name as the writer tags the label of an artifact-
        # kind entity's item when a registered item holds that name
        names |= {media_object_label(name) for name in names}
        titled = instance.title_flags(qid, sha1, names)
        self._flag(titled)
        pages = {*held, *_files_with_bytes(self.client, sha1)}
        shared = instance.file_flags(qid, sha1, pages)
        self._flag(shared)
        return [*titled, *shared]

    def _read(self) -> _Instance:
        if self._instance is None:
            properties, items = self.client.vocabulary()
            self._instance = _Instance.read(self.client, properties, items)
        return self._instance

    def _excluded(self, names: list[str], sha1: str) -> bool:
        """Does the never-ingest list name media with any of these file
        names or with this SHA-1? The SHA-1 is the media's own, so the
        file is not hashed again (is_excluded)."""
        if self.exclusions is None:
            return False
        return any(
            is_excluded(self.exclusions, name=name, sha1=sha1)
            for name in names or [None]
        )

    def _free_label(
        self, title: str, content_sha1: str, suffix: str = ""
    ) -> str:
        """The label `title + suffix`, or, when another item has that as
        its label, with the media's content tag at its end: labels stay
        unique per kind. `title` and `suffix` are in the form Wikibase
        stores terms in (_stored_term). A label that holds a character
        Python's Unicode tables do not know (unknown_characters) takes
        the tag too, unless it bears it already, as a fragment label made
        from its artifact's tagged label does: the instance may keep the
        other items' labels in another form than this one, so whether it
        is free cannot be told, and a label and description Wikibase
        refused as another item's would leave an item without a label
        behind. The title is cut, ending in "…", so that the label fits
        the TERM_LIMIT characters Wikibase takes, tagged or not
        (_fitted). A label that still does not fit, its suffix taking all
        the room, or whose tagged form another item has too, is refused
        here, before anything is written, so that a write Wikibase would
        refuse leaves no half-made item behind. The tagged form is looked
        for as written, so a marked one the instance keeps in another
        form is not seen; another item holds it only by a clash of the
        content tag's eight hex digits."""
        labels = self._read().labels
        tag = f"[{content_sha1[:8]}]"
        label = _fitted(title, suffix)
        if label in labels or (unknown_characters(label) and tag not in label):
            tagged = _fitted(title, f"{suffix} {tag}")
            if tagged in labels:
                raise RuntimeError(
                    f"the labels {label!r} and {tagged!r} both name other "
                    f"items, and a content-tagged title needs a free label: "
                    f"no item was made under either"
                )
            label = tagged
        if len(label) > TERM_LIMIT:
            raise ValueError(
                f"the label {label!r} is longer than the {TERM_LIMIT} "
                f"characters Wikibase takes: no item was made"
            )
        return label

    def _takes_alias(self, item, name: str, content_sha1: str) -> bool:
        """Should `name`, another name for an item's bytes, become its alias?

        Name variants live in aliases (MODELING.md), but not a name that
        belongs to other content: another item's label, or the plain form
        of this item's content-tagged label (tagged because that name was
        taken). Aliasing it would cross-link two artifacts. Nor does a
        name longer than Wikibase takes, nor one that holds a character
        Python's Unicode tables do not know (unknown_characters): the
        instance may keep it in another form than the item's label or
        alias that came of it, so it would look missing on every
        registration. `name` is in the stored form (_stored_term), as the
        item's terms are.
        """
        current = str(item.labels.get("en") or "")
        aliases = {str(alias) for alias in item.aliases.get("en") or []}
        return (
            name != current
            and name not in aliases
            and len(name) <= TERM_LIMIT
            and not unknown_characters(name)
            and current != f"{name} [{content_sha1[:8]}]"
            and self._read().labels.get(name, set()) <= {item.id}
        )

    def register_artifact(
        self,
        media_path: Path | None,
        artifact_type: str,
        title: str,
        description: str | None = None,
        pages: int = 0,
        *,
        sha1: str | None = None,
    ) -> RegistrationResult:
        """Ensure the media, the artifact item and `pages` page fragments
        exist, found by the SHA-1 of the media's bytes, and return their
        ids.

        `artifact_type` is a manifest item key (photograph, certificate,
        letter, census_page, newspaper_clipping, presentation,
        genealogical_report, or artifact itself). `sha1` is the SHA-1 the
        caller read the file's bytes as, if it did: registration refuses
        a file that no longer has it, as it refuses media on the
        never-ingest list, before it asks anything of the wiki. Without
        media there is nothing to find an artifact by, so `media_path` is
        required. The title is taken in the form Wikibase stores a label
        in (_stored_term), and one blank in that form is refused. The
        bytes are uploaded under the file's name in the form MediaWiki
        takes as it is (_upload_name). A new item's label is the title,
        content-tagged when another item has it or when it holds a
        character Python's Unicode tables do not know, and cut to fit,
        ending in "…", when longer than the TERM_LIMIT characters
        Wikibase takes (_free_label), and its description is cut so too
        (_new_artifact).
        The result's `flags` are what the item, made or found, is flagged
        for (Registrar._flags), as `self.flags` gathers them.
        """
        if media_path is None:
            raise ValueError(
                "an artifact is registered by the SHA-1 of its media "
                "(ADR-0010): give its media file"
            )
        content_sha1 = file_sha1(media_path)
        if sha1 is not None and sha1 != content_sha1:
            raise RuntimeError(
                f"{media_path.name} no longer has the bytes it was read "
                f"with (SHA-1 {sha1[:12]}, now {content_sha1[:12]}); "
                f"nothing was registered"
            )
        if self._excluded([media_path.name], content_sha1):
            raise ExcludedMedia(
                f"{media_path.name} is on the never-ingest list; nothing "
                f"was registered or uploaded"
            )
        title = _stored_term(title)
        if not title:
            raise ValueError(
                f"{media_path.name} has a blank title: no label to register "
                f"it under"
            )
        properties, items = self.client.vocabulary()
        if artifact_type not in items:
            raise ValueError(
                f"unknown artifact type {artifact_type!r}; expected a "
                f"manifest item key such as photograph or certificate"
            )
        instance_of = properties["instance_of"]
        stated_in = properties["stated_in"]
        media_file = properties["media_file"]
        media_sha1 = properties["media_sha1"]
        instance = self._read()

        found = instance.by_sha1.get(content_sha1)
        artifact = self.client.wbi.item.get(found) if found else None
        created = changed = artifact is None
        # a new item's label is settled before anything is uploaded or
        # written
        new_label = self._free_label(title, content_sha1) if created else None
        held = (
            _files_holding(
                self.client,
                _existing_claim_strings(artifact, media_file),
                content_sha1,
            )
            if artifact is not None
            else []
        )
        # upload before any item is made, and only when no file of the
        # item holds the bytes already
        file_page = (
            held[0]
            if held
            else self.client.upload_file(
                media_path, filename=_upload_name(media_path.name)
            )
        )

        if created:
            artifact = self._new_artifact(description)
            artifact.labels.set("en", new_label)
        elif self._takes_alias(artifact, title, content_sha1):
            # the same bytes under another name: the name is an alias
            aliases = {str(a) for a in (artifact.aliases.get("en") or [])}
            artifact.aliases.set("en", sorted(aliases | {title}))
            changed = True
        artifact_qid = artifact.id

        statements = [
            _item_claim(
                instance_of, items[artifact_type], stated_in, artifact_qid
            ),
            _string_claim(media_file, file_page, stated_in, artifact_qid),
            _string_claim(media_sha1, content_sha1, stated_in, artifact_qid),
        ]
        for claim in statements:
            outcome = _ensure_referenced(
                artifact, claim, stated_in, artifact_qid
            )
            changed = changed or outcome != "present"
        if changed:
            artifact = artifact.write(summary="ingest: artifact statements")
        label = str(artifact.labels.get("en") or title)
        instance.label(artifact_qid, label)
        instance.by_sha1.setdefault(content_sha1, artifact_qid)
        instance.file(artifact_qid, file_page)

        fragment_qids = {
            page: self._fragment(artifact_qid, label, content_sha1, page)
            for page in range(1, pages + 1)
        }
        # flagged on every registration while a condition holds, under its
        # title, its label before its content tag and the labels of its
        # fragments, every fragment it has, once the item and its
        # fragments are complete: a file listing that fails leaves nothing
        # half-registered
        flags = self._flags(
            artifact_qid,
            content_sha1,
            [file_page, *held],
            [title, label],
            sorted(
                int(page)
                for whole, page in instance.fragments
                if whole == artifact_qid
            ),
        )
        return RegistrationResult(
            artifact_qid=artifact_qid,
            file_page=file_page,
            fragment_qids=fragment_qids,
            created=created,
            content_sha1=content_sha1,
            flags=flags,
        )

    def _new_artifact(self, description: str | None):
        """A new artifact item, with its description alone, in the form
        Wikibase stores it in and cut to the TERM_LIMIT characters it
        takes, ending in "…" (_fitted): a longer one, such as one that
        names a long file, would be refused, and no item made. Its
        statements cite the item itself, so they are written once it has
        an id, with its label: a registration cut short between the two
        writes leaves an item with no label and no statement, which
        nothing finds and which takes no label from the artifact
        registered after it."""
        item = self.client.wbi.item.new()
        description = _fitted(_stored_term(description or ""))
        if description:
            item.descriptions.set("en", description)
        return item.write(summary="ingest: register artifact")

    def _fragment(
        self,
        artifact_qid: str,
        artifact_label: str,
        content_sha1: str,
        page: int,
    ) -> str:
        """The artifact's fragment for `page`: the one it has, or a new
        one, made in a single write with its statements, each stated in
        the artifact."""
        instance = self._read()
        key = (artifact_qid, str(page))
        if key in instance.fragments:
            return instance.fragments[key]
        properties, items = self.client.vocabulary()
        stated_in = properties["stated_in"]
        # the artifact's label is cut to fit, if need be: the fragment's
        # label and description only display it
        label = self._free_label(
            artifact_label, content_sha1, f", page {page}"
        )
        fragment = self.client.wbi.item.new()
        fragment.labels.set("en", label)
        fragment.descriptions.set(
            "en", _fitted(artifact_label, prefix=f"page {page} of ")
        )
        _append(
            fragment,
            _item_claim(
                properties["instance_of"],
                items["artifact_fragment"],
                stated_in,
                artifact_qid,
            ),
        )
        _append(
            fragment,
            _part_of_claim(
                properties["part_of"],
                properties["page_number"],
                stated_in,
                artifact_qid,
                page,
            ),
        )
        fragment = fragment.write(summary="ingest: register fragment")
        instance.label(fragment.id, label)
        instance.fragments[key] = fragment.id
        return fragment.id

    def register_container_sources(
        self,
        sources: ContainerSources,
        directory: Path,
        *,
        title: str,
    ) -> dict[str, str]:
        """Register verified bound owners from the common container projection.

        Canonical owner bytes retain the existing media-SHA lookup and upload
        identity even when a current reading representation changes. Native
        source/placement identities enrich composition references; they never
        allocate another owner or rewrite an established artifact binding.
        Unresolved source parts are not in the admitted ``sources`` mapping.
        A prepared image cannot adopt an existing graph owner by matching
        reading bytes alone: its original-part composition must already be
        recorded. Complete original media and verified legacy bindings keep
        their explicit compatibility semantics.
        """
        inventory = sources.inventory
        parts = {part.id: part for part in inventory.parts}
        prepared = {
            key: {
                part_id
                for part_id in bound.part_ids
                if (binding := parts[part_id].owner_binding) is not None
                and binding.basis == "prepared-part"
            }
            for key, bound in sources.sources.items()
        }
        if any(prepared.values()):
            instance = self._read()
            properties, _ = self.client.vocabulary()
            container = instance.by_sha1.get(inventory.source.sha1)
            for key, required in prepared.items():
                found = instance.by_sha1.get(key)
                if not required or found is None:
                    continue
                item = self.client.wbi.item.get(found)
                held = (
                    _composition_source_parts(
                        item, properties, container, inventory.source.sha1
                    )
                    if container is not None
                    else set()
                )
                if not required <= held:
                    raise ValueError(
                        f"prepared source {key} matches "
                        f"existing owner {found} "
                        "without its original source-part binding; "
                        "verify the binding before registration"
                    )
        media = {}
        for key, bound in sources.sources.items():
            if key != bound.owner.sha1:
                raise ValueError("bound source key differs from its owner")
            located: dict[int, set[str]] = {}
            placed_parts: set[str] = set()
            for placement in bound.placements:
                if placement.occurrence != "paint":
                    continue
                page = placement.locator.position
                if page is None:
                    continue
                part = parts[placement.child]
                support = SourceSupport(
                    container=inventory.source,
                    part_id=part.id,
                    locator=part.locator,
                    placements=[
                        PlacementSupport(
                            id=placement.id, locator=placement.locator
                        )
                    ],
                )
                located.setdefault(page, set()).update(
                    source_support_locators(support)
                )
                placed_parts.add(part.id)
            unplaced = {
                locator
                for part_id in bound.part_ids
                if part_id not in placed_parts
                for locator in source_support_locators(
                    SourceSupport(
                        container=inventory.source,
                        part_id=part_id,
                        locator=parts[part_id].locator,
                    )
                )
            }
            if any(
                len(locator) > TEXT_LIMIT
                for values in [*located.values(), unplaced]
                for locator in values
            ):
                raise ValueError("source-part locator cannot be truncated")
            media[key] = _RegistrationMedium(
                names=list(bound.names),
                slides=sorted(located),
                file=bound.file,
                locators={
                    page: tuple(sorted(values))
                    for page, values in located.items()
                },
                unplaced_locators=tuple(sorted(unplaced)),
            )
        record = _RegistrationMedia(
            source_sha1=inventory.source.sha1,
            container=inventory.container,
            title=title,
            media=media,
        )
        return self._register_media(record, directory)

    def register_derived_media(
        self, record: DerivedMediaRecord, directory: Path
    ) -> dict[str, str]:
        """Compatibility adapter for an explicit older byte-owner record.

        It supplies no native occurrence evidence. Container runs use
        ``register_container_sources`` after reconstructing inventory from
        their original bytes, preserving verified existing owner bindings.
        """
        return self._register_media(record, directory)

    def _register_media(
        self, record: DerivedMediaRecord | _RegistrationMedia, directory: Path
    ) -> dict[str, str]:
        """Ensure each medium a container embeds has its artifact item and
        its file on the wiki, from `record`: the bound sources of the
        container projection (register_container_sources), or an explicit
        older derived-media record (register_derived_media), each medium's
        bytes read from the file `record` names under `directory`; returns
        {media SHA-1 -> QID}.

        The container is the item whose `media SHA-1` is the record's
        source SHA-1, registered before its media. Each admitted medium is
        found by its canonical owner's SHA-1 (ADR-0010). Complete original
        image bytes shared across containers retain one item, `part of`
        each container; prepared images reach this loop after their owner
        binding check. It is labelled ``"<container title> — <name>"`` by
        its first name: in the deck's archive (``image1.png``), or its
        first place in the PDF (``page 6, image 1``; ADR-0008), of the
        names that show something once stored (_shows), or ``image N``
        when none does, N its place among the record's media in
        the order of their SHA-1s, excluded media counted; its other
        names become aliases, unless another item has one as its label.
        Its bytes are uploaded from the file the record keeps, under the
        name ``"<title> <name>"``, with the file's suffix when the name has
        none, in the form MediaWiki takes, its title cut or left out to
        fit (_upload_name), only when no file of the item holds them.
        It is a `photograph` when that file is an image's, and an
        `artifact` otherwise.

        Each container asserts its media additively: one ``part of ->
        container`` per slide or page the medium is on, qualified with its
        number, and unqualified composition for original parts with no
        located paint. The latter records original-part provenance without
        inventing a page or placement. `instance of`, `media file` and
        `media SHA-1` are also referenced ``stated in -> the container``.
        A statement another container (or the artifact itself) made gains this
        container's reference instead of a duplicate. A medium the
        never-ingest list names is left out: neither registered nor
        uploaded. A deck's medium is asked of by its SHA-1 and by each of
        its names in the archive, which the list's globs match; a PDF's
        image by its SHA-1 alone, as the reader asks of it, since its
        names are its places in the PDF, no file's (ADR-0008). The run's
        list names the SHA-1 of every image a glob names in any of its
        decks (corpus.gather_embedded), so those bytes are left out of
        every container that holds them, whatever they are called there.
        """
        if not record.media:
            return {}
        instance = self._read()
        container = instance.by_sha1.get(record.source_sha1)
        if container is None:
            raise LookupError(
                f"no item carries media SHA-1 {record.source_sha1[:12]}: "
                f"register the container before the media it embeds"
            )
        properties, items = self.client.vocabulary()
        located = any(
            getattr(medium, "locators", {})
            or getattr(medium, "unplaced_locators", ())
            for medium in record.media.values()
        )
        if located and "source_part_locator" not in properties:
            raise ValueError(
                "source-part provenance needs the current model bootstrap"
            )
        instance_of = properties["instance_of"]
        stated_in = properties["stated_in"]
        media_file = properties["media_file"]
        media_sha1 = properties["media_sha1"]
        title = _stored_term(record.title)
        derived: dict[str, str] = {}
        media = sorted(record.media.items())
        for number, (key, medium) in enumerate(media, start=1):
            # a PDF's image is named for its places ("page 6, image 1"),
            # no file's name, so no glob is matched against them
            globbed = medium.names if record.container != "pdf" else []
            if self._excluded(globbed, key):
                continue
            path = directory / medium.file
            # the file is named for the medium's SHA-1 and a suffix: its
            # archive name's for a deck's medium, the encoding it was
            # extracted in for a PDF's image
            suffix = PurePosixPath(medium.file).suffix.lower()
            if path.is_symlink() or file_sha1(path) != key:
                raise RuntimeError(
                    f"{medium.file} does not hold the bytes its record "
                    f"names; nothing of it was registered or uploaded"
                )
            # the names that show something once stored (_shows), in
            # their order: a medium with none, its names control
            # characters or a zero-width space alone, say, is "image N",
            # N its place among the record's media in the order of their
            # SHA-1s, excluded media counted; its label then ends in a
            # name the instance keeps as sent, and a re-run finds it
            kept = [name for name in medium.names if _shows(name)]
            first = kept[0] if kept else f"image {number}"
            names = list(dict.fromkeys(map(_stored_term, kept))) or [first]
            # the container's title is cut to fit, if need be: a medium's
            # labels and aliases only display it
            labels = [_fitted(title, f" — {name}") for name in names]
            found = instance.by_sha1.get(key)
            item = self.client.wbi.item.get(found) if found else None
            changed = item is None
            # a new item's label is settled before anything is uploaded
            new_label = (
                self._free_label(title, key, f" — {names[0]}")
                if changed
                else None
            )
            held = (
                _files_holding(
                    self.client, _existing_claim_strings(item, media_file), key
                )
                if item is not None
                else []
            )
            # uploaded only when no file of the item holds the bytes, so a
            # medium another container embeds adds no second blob
            if not held:
                name = first
                if not PurePosixPath(name).suffix:
                    name += suffix
                upload = _upload_name(name, record.title, record.container)
                held = [self.client.upload_file(path, filename=upload)]
            if item is None:
                item = self.client.wbi.item.new()
                item.labels.set("en", new_label)
                slides = ", ".join(str(number) for number in medium.slides)
                item.descriptions.set(
                    "en",
                    _fitted(
                        slides,
                        ")",
                        prefix=(
                            "image placed within the PDF (page "
                            if record.container == "pdf"
                            else "media embedded in the presentation (slide "
                        ),
                        at=", ",
                    )
                    if medium.slides
                    else (
                        "image embedded in the PDF"
                        if record.container == "pdf"
                        else "media embedded in the presentation"
                    ),
                )
            for name in labels:
                if self._takes_alias(item, name, key):
                    item.aliases.set("en", name)
                    changed = True
            kind = (
                items["photograph"]
                if suffix in IMAGE_SUFFIXES
                else items["artifact"]
            )
            statements = [
                _item_claim(instance_of, kind, stated_in, container),
                *(
                    _string_claim(media_file, page, stated_in, container)
                    for page in held
                ),
                _string_claim(media_sha1, key, stated_in, container),
                *(
                    _part_of_claim(
                        properties["part_of"],
                        properties["page_number"],
                        stated_in,
                        container,
                        slide,
                        locator_property=properties.get("source_part_locator"),
                        locators=getattr(medium, "locators", {}).get(
                            slide, ()
                        ),
                    )
                    for slide in medium.slides
                ),
            ]
            if unplaced := getattr(medium, "unplaced_locators", ()):
                statements.append(
                    _part_of_claim(
                        properties["part_of"],
                        properties["page_number"],
                        stated_in,
                        container,
                        None,
                        locator_property=properties.get("source_part_locator"),
                        locators=unplaced,
                    )
                )
            for claim in statements:
                outcome = _ensure_referenced(item, claim, stated_in, container)
                changed = changed or outcome != "present"
            if changed:
                item = item.write(summary="ingest: derived media")
            if item.labels.get("en"):
                instance.label(item.id, str(item.labels.get("en")))
            instance.by_sha1.setdefault(key, item.id)
            for page in held:
                instance.file(item.id, page)
            # flagged on every registration while a condition holds, under
            # its first label and its own label before its content tag,
            # once the item is complete
            self._flags(
                item.id,
                key,
                held,
                [labels[0], str(item.labels.get("en") or "")],
            )
            derived[key] = item.id
        return derived


def register_artifact(
    client: WikiClient,
    media_path: Path | None,
    artifact_type: str,
    title: str,
    description: str | None = None,
    pages: int = 0,
    *,
    exclusions: ExclusionList | None = None,
) -> RegistrationResult:
    """Register one artifact and its page fragments, reading the instance
    afresh (Registrar.register_artifact)."""
    return Registrar(client, exclusions=exclusions).register_artifact(
        media_path, artifact_type, title, description, pages
    )


def register_derived_media(
    client: WikiClient,
    record: DerivedMediaRecord,
    directory: Path,
    *,
    exclusions: ExclusionList | None = None,
    flags: list[RegistrationFlag] | None = None,
) -> dict[str, str]:
    """Register the media a registered container embeds, from their
    record, reading the instance afresh (Registrar.register_derived_media).
    What the media were flagged for is added to `flags`, when given."""
    registrar = Registrar(client, exclusions=exclusions)
    derived = registrar.register_derived_media(record, directory)
    if flags is not None:
        flags.extend(registrar.flags)
    return derived
