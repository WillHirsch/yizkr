"""The corpus location and the never-ingest list.

The source corpus lives outside the working tree, in a private local
folder named by ``$YIZKR_CORPUS_DIR``, which is the default folder for
``yizkr ingest run``. ``$YIZKR_EXCLUDE_FILE`` names the never-ingest
list: a document on it is never registered, transcribed, extracted or
written, whatever folder it turns up in and whatever it is called. Both
variables are read from the environment first, then from deploy/.env.

Every entry point that reads source documents asks one shared check,
:func:`is_excluded`, before it uses a document's content.

The list is a local UTF-8 text file with one entry per line:

- blank lines, and lines starting with ``#``, are ignored;
- a line whose first word is a 40-digit hex SHA-1 excludes those bytes
  under any name. The rest of the line is ignored, so ``sha1sum``
  output can be pasted in as it is;
- any other line is a filename glob (``*``, ``?``, ``[...]``), matched
  against the file's name (never its folder), ignoring case.

A glob also matches an image a deck embeds, by its name in the archive.
Such an image's bytes are kept out of every document that holds them,
not only that deck: before any container's images are read, the run
gathers the SHA-1s of the images the globs name in its decks
(:func:`gather_embedded`) and adds them to the list it asks
(:meth:`ExclusionList.with_embedded`), so a PDF that places the same
bytes, or a file that is them, is excluded too. Only identical bytes
are followed.

The list fails closed. A configured file that is missing or unreadable
is an error, never an empty list. So is a line that would silently
match nothing: a hash that is not a SHA-1 (a pasted SHA-256, a
truncated hash), or a glob followed by an inline comment. Production
and extract-all modes (today ``ingest run``) refuse to run when no list
is configured; an empty file is an explicit configuration and allowed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from yizkr.core.config import ConfigError, env_setting

CORPUS_DIR_VAR = "YIZKR_CORPUS_DIR"
EXCLUDE_FILE_VAR = "YIZKR_EXCLUDE_FILE"

MISSING_LIST = (
    f"refusing to run without a never-ingest list: set {EXCLUDE_FILE_VAR} "
    "to its path, in the environment or in deploy/.env (an empty file is "
    "an explicit, allowed list). See deploy/README.md, 'The corpus and "
    "the never-ingest list'."
)

_SHA1 = re.compile(r"[0-9a-f]{40}")
# a first word this long and all hex digits was meant as a hash
_HASH_LIKE = re.compile(r"[0-9a-f]{32,}")
_INLINE_COMMENT = re.compile(r"\s#")


class ExclusionConfigError(ConfigError):
    """The never-ingest list is missing, unreadable or malformed."""


def _fold(name: str) -> str:
    """Compare names as people read them: one Unicode form, any case."""
    return unicodedata.normalize("NFC", name).casefold()


def file_sha1(path: Path) -> str:
    """Hex SHA-1 of a file's bytes, the content identity (ADR-0010)."""
    digest = hashlib.sha1()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ExclusionList:
    """A parsed never-ingest list; ``source`` is the file it came from.

    ``ExclusionList()`` is the empty list: an explicit configuration
    that excludes nothing, unlike no list at all (``None``)."""

    sha1s: frozenset[str] = frozenset()
    patterns: tuple[str, ...] = ()
    source: Path | None = None
    # the SHA-1s of the images the globs name in the corpus's decks
    # (gather_embedded): excluded wherever their bytes are, as `sha1s`
    # are, but no entry of the file, so the list a run records is the
    # file's alone
    embedded: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, text: str, source: Path | None = None) -> ExclusionList:
        where = source or "the never-ingest list"
        sha1s: set[str] = set()
        patterns: list[str] = []
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            first = line.split()[0].lower()
            if _SHA1.fullmatch(first):
                sha1s.add(first)
            elif _HASH_LIKE.fullmatch(first):
                raise ExclusionConfigError(
                    f"{where}, line {number}: a {len(first)}-digit hash is "
                    "not a SHA-1 (40 hex digits); list the file's SHA-1 "
                    "(`sha1sum FILE`)"
                )
            elif _INLINE_COMMENT.search(line):
                raise ExclusionConfigError(
                    f"{where}, line {number}: a glob cannot carry an inline "
                    "comment; put the comment on its own line (a literal "
                    "' #' in a file name is written ' [#]')"
                )
            else:
                patterns.append(_fold(line))
        return cls(frozenset(sha1s), tuple(patterns), source)

    def match(
        self, *, name: str | None = None, sha1: str | None = None
    ) -> str | None:
        """The entry that excludes a document with this file name and/or
        content SHA-1, or None. Pure: reads no file (see is_excluded)."""
        if name is not None:
            folded = _fold(name)
            for pattern in self.patterns:
                if fnmatch.fnmatchcase(folded, pattern):
                    return f"pattern {pattern!r}"
        if sha1 is not None and sha1.lower() in self.sha1s:
            return f"SHA-1 {sha1.lower()[:12]}"
        if sha1 is not None and sha1.lower() in self.embedded:
            return f"SHA-1 {sha1.lower()[:12]} (a deck's image a glob names)"
        return None

    def with_embedded(self, sha1s: Iterable[str]) -> ExclusionList:
        """This list, excluding also the bytes of the images the globs
        name in the corpus's decks (`gather_embedded`), in place of any
        it excluded so before."""
        return replace(self, embedded=frozenset(s.lower() for s in sha1s))


def is_excluded(
    exclusions: ExclusionList,
    path: Path | None = None,
    *,
    name: str | None = None,
    sha1: str | None = None,
) -> bool:
    """Whether the never-ingest list excludes a document: the one shared
    check every entry point calls before it uses a document's content.

    Give the document's file ``path``: its name is matched against the
    globs and, when the list holds SHA-1s, its bytes are hashed. Where no
    file is at hand (an imported transcription, say), give the original's
    ``name`` and/or content ``sha1`` instead. Every identity given is
    checked, and any match excludes."""
    if path is None and name is None and sha1 is None:
        raise ValueError("is_excluded needs a path, a name or a SHA-1")
    names = {n for n in (name, path.name if path else None) if n}
    if any(exclusions.match(name=n) for n in names):
        return True
    if sha1 is not None and exclusions.match(sha1=sha1):
        return True
    if path is not None and (exclusions.sha1s or exclusions.embedded):
        return exclusions.match(sha1=file_sha1(path)) is not None
    return False


def gather_embedded(
    exclusions: ExclusionList, decks: Iterable[Path]
) -> frozenset[str]:
    """The SHA-1s of the files the `decks` (.pptx archives) hold under a
    name the list's globs match: each member's name in the archive, never
    its folder, as a deck's image is matched where it is read. Every
    member is asked, not only the images the slides place, so nothing a
    glob names is missed, and every deck, the list's own included: a
    member is hashed in memory and nothing of it is kept. Only identical
    bytes are matched where they recur, so an image another document
    holds re-encoded (a PDF's image kept as pixels) is not. A deck that
    cannot be read as an archive fails closed: the bytes its globs would
    name cannot be known, so nothing is to be read until it is repaired
    or removed."""
    if not exclusions.patterns:
        return frozenset()
    found: set[str] = set()
    for deck in decks:
        try:
            with zipfile.ZipFile(deck) as archive:
                for member in archive.infolist():
                    name = PurePosixPath(member.filename).name
                    if member.is_dir() or not exclusions.match(name=name):
                        continue
                    digest = hashlib.sha1()
                    with archive.open(member) as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            digest.update(block)
                    found.add(digest.hexdigest())
        except (
            OSError,
            EOFError,
            RuntimeError,
            NotImplementedError,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            raise ExclusionConfigError(
                f"cannot read the deck {deck} to find the images the "
                f"never-ingest list's globs name in it: {exc}. Refusing to "
                "run: repair the deck, or list it itself."
            ) from exc
    return frozenset(found)


def read_exclusions(path: Path) -> ExclusionList:
    """Parse the list at ``path``; unreadable or malformed is an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExclusionConfigError(
            f"cannot read the never-ingest list {path} ({EXCLUDE_FILE_VAR}): "
            f"{exc}. Refusing to run."
        ) from exc
    return ExclusionList.parse(text, source=path)


def load_exclusions(
    *, required: bool, env_file: Path | None = None
) -> ExclusionList | None:
    """The never-ingest list named by ``$YIZKR_EXCLUDE_FILE``.

    With no list configured, ``required`` (production and extract-all
    modes) refuses to run, and otherwise the result is None. A configured
    list that cannot be read is an error either way: it fails closed."""
    value = env_setting(EXCLUDE_FILE_VAR, env_file)
    if not value:
        if required:
            raise ExclusionConfigError(MISSING_LIST)
        return None
    return read_exclusions(Path(value).expanduser())


def corpus_dir(env_file: Path | None = None) -> Path | None:
    """The corpus folder named by ``$YIZKR_CORPUS_DIR``; None if unset."""
    value = env_setting(CORPUS_DIR_VAR, env_file)
    return Path(value).expanduser() if value else None
