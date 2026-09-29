"""Artifact registrar: media upload, and artifact, page-fragment and
derived-media items, each found or made by the SHA-1 of its media
(ADR-0003 §4, ADR-0010).

Registration is part of the write (ADR-0025 §2): `ingest run` registers
a document just before it writes the claims that cite it. The claims cite
an artifact by the SHA-1 of its media, and the writer resolves that key to
the item whose `media SHA-1` statement registration writes. Per artifact,
registration writes:

* the media file, uploaded to the wiki (W1: artifacts as wiki media,
  ADR-0004);
* an artifact item: label, description, ``instance of -> <type>``,
  ``media file -> File:...`` and ``media SHA-1 -> <hex digest>``;
* optional page-fragment items: ``instance of -> artifact fragment``,
  ``part of -> <artifact>`` with a ``page number`` qualifier;
* for a compound artifact (ADR-0008), the media it embeds, from the
  record the stages before the write keep of them
  (yizkr.pipeline.presentation): each an artifact item of its own, with
  its media file and SHA-1, ``part of`` the container once per slide of a
  deck, or page of a PDF, it is on.

**Identity is the SHA-1 alone.** An artifact is the item whose `media
SHA-1` is the SHA-1 of its file's bytes, and with none a new item is made.
A title never finds an item, so two files that share a title are two
artifacts. Labels stay unique per kind, the wiki client's label index
(yizkr.core.wiki), so a new item whose title is already another item's
label takes a short content tag, ``"<title> [<sha1[:8]>]"``. A page
fragment is keyed by its artifact: the item that is an ``instance of
artifact fragment`` and ``part of`` that artifact with that page's number.
So same-titled artifacts never share a fragment, and a derived medium,
which is ``part of`` its container with a page qualifier too, is never
taken for a page's fragment. A file on the never-ingest list
(yizkr.core.corpus) is never registered or uploaded: registration refuses
it before it asks anything of the wiki, and leaves out an embedded medium
the list names.

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
(CLAUDE.md rule 3). The bytes are uploaded only when no file of
the item holds them, and an upload never overwrites a wiki file or stores
bytes the wiki already holds (`WikiClient.upload_file`), so every `media
file` holds the bytes its item's `media SHA-1` names.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from yizkr.core.corpus import ExclusionList, file_sha1, is_excluded
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.presentation import IMAGE_SUFFIXES, DerivedMediaRecord

# the most characters Wikibase takes in a label, a description or an alias
# (its default limit on a term's length)
TERM_LIMIT = 250

# how Wikibase trims a term before it stores it: the separators and control
# characters at either end go, and each run of control characters inside
# becomes one space. `\s` holds every separator and the controls that are
# whitespace; the other controls are named.
_TERM_EDGES = re.compile(r"^[\s\x00-\x1f\x7f-\x9f]+|[\s\x00-\x1f\x7f-\x9f]+$")
_TERM_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _stored_term(term: str) -> str:
    """`term` in the form Wikibase stores a label, a description or an
    alias in: the separators and control characters at either end
    trimmed, each run of control characters inside made one space, and the
    whole in Unicode NFC. The reading of the instance holds labels in that
    form, so a title is compared with it, and set, in that form too: in any
    other, a label another item holds would look free, and an alias the
    item has would look missing."""
    trimmed = _TERM_CONTROLS.sub(" ", _TERM_EDGES.sub("", term))
    return unicodedata.normalize("NFC", trimmed)


def _fitted(
    text: str, suffix: str = "", *, prefix: str = "", at: str | None = None
) -> str:
    """`prefix + text + suffix`, whose parts are in the stored form
    (_stored_term), within the TERM_LIMIT characters Wikibase takes: when
    the whole is longer, `text` is cut, at the last `at` within the room
    when `at` is given, and ends in "…". The result is in the stored form
    too, and the same parts always give the same result. For text that
    only displays, such as a deck's title in the labels of its slides and
    images: nothing finds an item by it."""
    whole = prefix + text + suffix
    room = TERM_LIMIT - len(prefix) - len(suffix) - 1
    if len(whole) <= TERM_LIMIT or room < 1:
        return whole
    cut = text[:room]
    if at is not None and at in cut:
        cut = cut.rsplit(at, 1)[0]
    return prefix + cut.rstrip() + "…" + suffix


class ExcludedMedia(RuntimeError):
    """Media on the never-ingest list: registration refuses it before it
    reads the wiki, writes to it or uploads anything."""


@dataclass
class RegistrationResult:
    artifact_qid: str
    file_page: str | None
    fragment_qids: dict[int, str] = field(default_factory=dict)
    created: bool = True
    # the SHA-1 of the media's bytes: the artifact's identity (ADR-0010)
    content_sha1: str | None = None


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


def _cites(claim, stated_in_pid: str, source_qid: str) -> bool:
    """Does any reference block on `claim` cite `stated in -> source`?"""
    for reference in claim.references.references:
        for snak in reference.snaks.get(stated_in_pid) or []:
            value = (snak.datavalue or {}).get("value") or {}
            if isinstance(value, dict) and value.get("id") == source_qid:
                return True
    return False


def _ensure_referenced(
    entity, claim, stated_in_pid: str, source_qid: str
) -> str:
    """Make `entity` carry `claim` as asserted by `source_qid`, additively.

    * no equal statement (same property, value AND qualifiers): `claim`
      is appended as a new statement -> "added";
    * an equal statement exists but cites no `stated in -> source`: the
      claim's reference blocks are appended to it, an agreeing second
      source (the writer's rule; ADR-0002, ADR-0003) -> "referenced";
    * already cited -> "present" (safe re-run).

    Existing statements and their references are never removed or
    rewritten (CLAUDE.md rule 3). The caller writes the entity.
    """
    key = _statement_key(claim)
    for existing in entity.claims.get(claim.mainsnak.property_number):
        if _statement_key(existing) != key:
            continue
        if _cites(existing, stated_in_pid, source_qid):
            return "present"
        for reference in claim.references.references:
            existing.references.add(reference)
        return "referenced"
    _append(entity, claim)
    return "added"


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
    part_of: str, page_number: str, stated_in: str, whole: str, page: int
):
    """``part of -> whole`` on page `page`, stated in the whole."""
    from wikibaseintegrator.datatypes import String

    claim = _item_claim(part_of, whole, stated_in, whole)
    claim.qualifiers.add(String(prop_nr=page_number, value=str(page)))
    return claim


# -- an item's kind, stated in the item itself --------------------------------


def ensure_self_typed(
    client: WikiClient,
    qid: str,
    kind: str,
    *,
    summary: str,
    dry_run: bool = False,
) -> bool:
    """Make the item `qid` state its kind, ``instance of -> kind`` (a
    manifest item key), referenced ``stated in -> the item itself``: a run
    item is the document of its own execution, and asserts its kind by
    existing, as an artifact does (docs/MODELING.md, "Artifacts and
    registration").

    The statement cites the item, so it is written once the item has an
    id, in a write after the one that makes it. A write cut short between
    the two leaves an item with no kind, which the public export leaves
    out (yizkr.backup), and the statements that cite it lose that
    reference there. So whatever finds such an item again, as a re-run
    finds its run item by its label, calls this, and the item gets its
    kind then. An item that states ``instance of -> kind`` already, at any
    rank and under any reference, is left as it is: a curator's
    deprecation of that statement, or a reference of their own on it,
    stands (CLAUDE.md rules 3 and 4). Returns whether the item lacked the
    statement; a dry run reads the item and writes nothing.
    """
    properties, items = client.vocabulary()
    instance_of, kind_qid = properties["instance_of"], items[kind]
    item = client.wbi.item.get(qid)
    if kind_qid in _existing_claim_targets(item, instance_of):
        return False
    if not dry_run:
        _append(
            item,
            _item_claim(instance_of, kind_qid, properties["stated_in"], qid),
        )
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


# -- the instance, as registration finds items in it --------------------------


def _value(snak):
    """A snak's value, or None for a snak without one (no value, some
    value)."""
    return ((snak or {}).get("datavalue") or {}).get("value")


def _earliest(qids: list[str]) -> str:
    return min(qids, key=lambda qid: int(qid[1:]))


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

    @classmethod
    def read(
        cls,
        client: WikiClient,
        properties: dict[str, str],
        items: dict[str, str],
    ) -> _Instance:
        """Every item's English label, `media SHA-1` values and, for an
        `instance of artifact fragment`, each `part of` target with each
        `page number` of that statement."""
        instance_of, part_of = properties["instance_of"], properties["part_of"]
        page_number = properties["page_number"]
        media_sha1 = properties["media_sha1"]
        fragment_class = items["artifact_fragment"]
        labels: dict[str, set[str]] = {}
        hashes: dict[str, list[str]] = {}
        fragments: dict[tuple[str, str], list[str]] = {}
        for qid, entity in client.iter_entities("labels|claims"):
            if not qid.startswith("Q"):
                continue  # a property's label is of another kind
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            if isinstance(label, str) and label:
                labels.setdefault(label, set()).add(qid)
            claims = entity.get("claims") or {}
            for claim in claims.get(media_sha1) or []:
                value = _value(claim.get("mainsnak"))
                if isinstance(value, str):
                    hashes.setdefault(value, []).append(qid)
            kinds = {
                value.get("id")
                for claim in claims.get(instance_of) or []
                if isinstance(value := _value(claim.get("mainsnak")), dict)
            }
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
        )

    def label(self, qid: str, label: str) -> None:
        self.labels.setdefault(label, set()).add(qid)


# -- registration -------------------------------------------------------------


class Registrar:
    """Registers artifacts, their page fragments and the media a compound
    artifact embeds on one instance, each by the SHA-1 of its media.

    `exclusions` is the never-ingest list; None registers without one, as
    the single-file steps run when none is configured. The instance is
    read on first need, once, and what was read is kept current with what
    this registrar writes: one registrar serves any number of artifacts,
    while nothing else writes to the instance."""

    def __init__(
        self, client: WikiClient, *, exclusions: ExclusionList | None = None
    ) -> None:
        self.client = client
        self.exclusions = exclusions
        self._instance: _Instance | None = None

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
        self,
        title: str,
        content_sha1: str,
        suffix: str = "",
        *,
        fit: bool = False,
    ) -> str:
        """The label `title + suffix`, or, when another item has that as
        its label, with the media's content tag at its end: labels stay
        unique per kind. `title` and `suffix` are in the form Wikibase
        stores terms in (_stored_term). With `fit`, the title is cut,
        ending in "…", so that the label fits the TERM_LIMIT characters
        Wikibase takes, tagged or not (_fitted). Without it, a label that
        long is refused here, before anything is written, so that a write
        Wikibase would refuse leaves no half-made item behind."""

        def label_for(tag: str) -> str:
            if fit:
                return _fitted(title, suffix + tag)
            return title + suffix + tag

        labels = self._read().labels
        label = label_for("")
        if label in labels:
            tagged = label_for(f" [{content_sha1[:8]}]")
            if tagged in labels:
                raise RuntimeError(
                    f"the labels {label!r} and {tagged!r} both name other "
                    f"items, and labels stay unique: no item was made "
                    f"under either"
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
        name longer than Wikibase takes. `name` is in the stored form
        (_stored_term), as the item's terms are.
        """
        current = str(item.labels.get("en") or "")
        aliases = {str(alias) for alias in item.aliases.get("en") or []}
        return (
            name != current
            and name not in aliases
            and len(name) <= TERM_LIMIT
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
        in (_stored_term), and one blank in that form is refused.
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
        file_page = held[0] if held else self.client.upload_file(media_path)

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

        fragment_qids = {
            page: self._fragment(artifact_qid, label, content_sha1, page)
            for page in range(1, pages + 1)
        }
        return RegistrationResult(
            artifact_qid=artifact_qid,
            file_page=file_page,
            fragment_qids=fragment_qids,
            created=created,
            content_sha1=content_sha1,
        )

    def _new_artifact(self, description: str | None):
        """A new artifact item, with its description alone. Its statements
        cite the item itself, so they are written once it has an id, with
        its label: a registration cut short between the two writes leaves
        an item with no label and no statement, which nothing finds and
        which takes no label from the artifact registered after it."""
        item = self.client.wbi.item.new()
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
            artifact_label, content_sha1, f", page {page}", fit=True
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

    def register_derived_media(
        self, record: DerivedMediaRecord, directory: Path
    ) -> dict[str, str]:
        """Ensure each medium a container embeds has its artifact item and
        its file on the wiki, from the record of them kept in `directory`
        (presentation.record_derived_media, or the record a PDF read page
        by page keeps of the images placed within its pages); returns
        {media SHA-1 -> QID}.

        The container is the item whose `media SHA-1` is the record's
        source SHA-1, registered before its media. Each medium is found by
        its own SHA-1 (ADR-0010), so an image that several decks or PDFs
        embed, or that is also ingested on its own, is one item, `part of`
        each container. It is labelled ``"<container title> — <name>"`` by
        its first name: in the deck's archive (``image1.png``), or its
        first place in the PDF (``page 6, image 1``; ADR-0008), and its
        other names become aliases, unless another item has one as its
        label. Its bytes are uploaded from the file the record keeps, under
        the name ``"<title> <name>"``, with the file's suffix when the name
        has none, only when no file of the item holds them. It is a
        `photograph` when that file is an image's, and an `artifact`
        otherwise.

        Each container asserts its media additively: one ``part of ->
        container`` per slide or page the medium is on, qualified with its
        number, plus `instance of`, `media file` and `media SHA-1`, each
        referenced ``stated in -> the container``. A statement another
        container (or the artifact itself) made already gains this
        container's reference instead of a duplicate. A medium the
        never-ingest list names, by a name or by its SHA-1, is left out:
        neither registered nor uploaded.
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
        instance_of = properties["instance_of"]
        stated_in = properties["stated_in"]
        media_file = properties["media_file"]
        media_sha1 = properties["media_sha1"]
        # MediaWiki refuses some characters in a file name
        prefix = (
            re.sub(r"[^A-Za-z0-9 _.-]+", "", record.title).strip()
            or record.container
        )
        title = _stored_term(record.title)
        derived: dict[str, str] = {}
        for key, medium in sorted(record.media.items()):
            if self._excluded(medium.names, key):
                continue
            path = directory / medium.file
            # the record names the file for the medium's SHA-1 and the
            # suffix of its first name
            suffix = PurePosixPath(medium.file).suffix.lower()
            if path.is_symlink() or file_sha1(path) != key:
                raise RuntimeError(
                    f"{medium.file} does not hold the bytes its record "
                    f"names; nothing of it was registered or uploaded"
                )
            # the container's title is cut to fit, if need be: a medium's
            # labels and aliases only display it
            names = [_stored_term(name) for name in medium.names]
            labels = [_fitted(title, f" — {name}") for name in names]
            found = instance.by_sha1.get(key)
            item = self.client.wbi.item.get(found) if found else None
            changed = item is None
            # a new item's label is settled before anything is uploaded
            new_label = (
                self._free_label(title, key, f" — {names[0]}", fit=True)
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
                name = medium.names[0]
                if not PurePosixPath(name).suffix:
                    name += suffix
                held = [
                    self.client.upload_file(path, filename=f"{prefix} {name}")
                ]
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
                    )
                    for slide in medium.slides
                ),
            ]
            for claim in statements:
                outcome = _ensure_referenced(item, claim, stated_in, container)
                changed = changed or outcome != "present"
            if changed:
                item = item.write(summary="ingest: derived media")
            if item.labels.get("en"):
                instance.label(item.id, str(item.labels.get("en")))
            instance.by_sha1.setdefault(key, item.id)
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
) -> dict[str, str]:
    """Register the media a registered container embeds, from their
    record, reading the instance afresh (Registrar.register_derived_media)."""
    return Registrar(client, exclusions=exclusions).register_derived_media(
        record, directory
    )
