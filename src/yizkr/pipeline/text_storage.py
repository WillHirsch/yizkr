"""The forms and limits the instance keeps text in.

Wikibase holds at most TEXT_LIMIT (400) characters in the text of a
string, external-id or monolingual text value, whether the value is a
statement's, a qualifier's or a reference's, and no text value that
holds a line break or a tab, or has whitespace at either end
(LINE_BREAKS, WHITESPACE). It cleans a text up (CLEANED_UP): each C0
control character but the tab, the line feed and the carriage return,
and each of the noncharacters U+FFFE and U+FFFF, becomes U+FFFD, in a
string value before it checks and stores it, and in every text its API
gives back, monolingual text included (cleaned_up). It keeps text in
Unicode NFC (stored_string). It trims a label, a description or an
alias, cleans it up and keeps it in a form of its own (stored_term:
trimmed, cleaned up and in NFC, in that order, as Wikibase's trimToNFC
does it), in at most TERM_LIMIT (250) characters. It refuses the whole
edit that sends a text it does not hold.

So a text is sent in the form the instance keeps it in, and none is
sent that the instance would refuse or give back changed:

* text_faults says what keeps the instance from holding a text value as
  it is: its length, a line break or a tab, whitespace at either end, a
  character the instance cleans up, or a character Python's Unicode
  tables do not know (below);
* a text that only places a claim in its source, or helps a reader check
  it there (a reference's page number, printed page number, region or
  excerpt), is put on one line, cleaned up and cut to TEXT_LIMIT
  (locator_text): whitespace at either end goes, a line break inside
  becomes " | ", visibly, a tab a space, and each other character the
  instance cleans up U+FFFD, as the instance would give it back;
* a label, a description or an alias goes in the form the instance keeps
  a term in, and one still over TERM_LIMIT once in that form is cut
  (term_text), so whitespace around a term never counts towards the
  limit.

A text over its limit is cut at a break between words, every letter kept
whole (extends, half_flag), and ends with CUT_MARK (" …"), "sha1" and the
first 12 hex digits of the SHA-1 of the whole text (cut_text). A cut
depends on the text alone, so a re-run sends what the first run sent,
and the digest keeps two texts that agree up to the cut apart.

The instance puts text in NFC by the tables of Unicode 15.0
(INSTANCE_UNICODE: ICU 72.1 in the wikibase/wikibase:7 image, which
UtfNormal 4.0.0 reaches through PHP's intl), and this module by Python's
(unicodedata.unidata_version, 14.0.0 on Python 3.11). By Unicode's
normalization stability policy the two give the same form to every text
whose characters Python's tables assign, so the writer sends no text
that holds a character they do not, noncharacters aside
(unknown_characters): a combining mark assigned since, such as U+1E08F,
may take another place in the instance's NFC, and a re-run would not
find what the first run wrote. Each text is read whole for it, past
TERM_LIMIT or TEXT_LIMIT too, before any cut. The cuts read characters
by Python's tables too (extends).

The writer (yizkr.pipeline.writer) and the registrar
(yizkr.pipeline.registrar) send and compare text in these forms, and the
stages before them compare names in the form the instance keeps a term
in (yizkr.pipeline.names, yizkr.pipeline.clustering). The module depends
on the standard library alone, so those stages read these forms without
the writer's code.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

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
# text (cut_text)
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
# run of whitespace (_hint_text in yizkr.pipeline.writer)
_LINE_BREAK = re.compile("\r\n|[" + LINE_BREAKS + "]")
WHITESPACE_RUN = re.compile("[" + WHITESPACE + "]+")
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


def unknown_fault(chars: list[str]) -> str:
    """What keeps the instance from holding a text that holds the
    characters `chars` (unknown_characters) as the writer sends it: a
    clause that reads after the words naming the text. It names each
    character by its code point, quoting no text."""
    codes = listed([f"U+{ord(char):04X}" for char in chars])
    which = "a character" if len(chars) == 1 else "characters"
    return (
        f"holds {codes}, {which} Python's Unicode "
        f"{unicodedata.unidata_version} tables do not know, which the "
        f"instance may put in another order in NFC"
    )


def stored_string(text: str) -> str:
    """`text` in the form the instance keeps text in: Unicode NFC.
    Wikibase saves a string value (of a `string` or `external-id` snak)
    and an item's labels, descriptions and aliases in NFC, and its API
    gives every text back in NFC, monolingual text included.

    The writer sends the batch's text in this form: a claim's string,
    external-id and monolingual text values and qualifiers, an entity's
    type hint (_hint_text in yizkr.pipeline.writer), and a reference
    block's page number, printed page number, region and excerpt
    (locator_text). A label, description or alias goes in the form the
    instance keeps a term in, which is in NFC too (term_text).
    So what it builds for a claim is what the instance gives back for it,
    and the labels and aliases it looks for are in the form the instance
    holds them in, whatever form the batch gives them in: a re-run finds
    everything the first run wrote (steps 2 and 4 of the module
    docstring of yizkr.pipeline.writer).

    NFC gives a canonically equivalent text, which Unicode defines as
    the same text: a letter and its combining mark become the one
    precomposed character where Unicode has one, and combining marks
    take their canonical order. A combining mark with no precomposed
    form stays a combining mark, and a compatibility character, such as
    the long s, stays as it is, where NFKC would replace it."""
    return unicodedata.normalize("NFC", text)


def listed(things: list[str]) -> str:
    """`things` in prose: "a", "a and b", "a, b and c"."""
    if len(things) == 1:
        return things[0]
    return f"{', '.join(things[:-1])} and {things[-1]}"


def text_faults(text: str) -> list[str]:
    """What keeps the instance from holding `text`, a text value in the
    form the writer sends it in (stored_string), as it is: each fault a
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
            f"holds {listed(shapes)}, which Wikibase refuses or changes in "
            f"a text value"
        )
    if any(char in CLEANED_UP and char not in LINE_BREAKS for char in text):
        faults.append(
            "holds a control character or a noncharacter, which Wikibase "
            "changes into U+FFFD"
        )
    unknown = unknown_characters(text)
    if unknown:
        faults.append(unknown_fault(unknown))
    return faults


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


def locator_text(text: str) -> str:
    """A reference's page number, printed page number, region or excerpt
    as the writer sends it: in the form the instance keeps text in
    (stored_string), on one line (_one_line), cleaned up as the instance
    gives it back (cleaned_up; a vertical tab or a form feed is a line
    break by then) and, when that is longer than TEXT_LIMIT, cut to it
    (cut_text), with the digest of the text as the instance would keep it.
    It depends on the text alone, so a claim written again sends the
    text the instance keeps, and its reference block is found (step 4 of
    the module docstring of yizkr.pipeline.writer). Empty when the text is
    whitespace alone."""
    return cut_text(cleaned_up(_one_line(stored_string(text))), TEXT_LIMIT)


def cleaned_up(text: str) -> str:
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
    (_TERM_CONTROLS, _TERM_EDGES); then cleaned up (cleaned_up), which
    leaves only U+FFFE and U+FFFF to make U+FFFD; and then in Unicode NFC.
    The writer finds, compares and sends every term in this form, and the
    registrar every title. Empty when the text holds nothing the instance
    keeps."""
    text = _TERM_CONTROLS.sub(" ", text).strip(_TERM_EDGES)
    return unicodedata.normalize("NFC", cleaned_up(text))


def term_text(text: str) -> str:
    """A label, description or alias as the writer sends it, and as it
    looks for a label: in the form the instance keeps a term in
    (stored_term) and, when that is longer than TERM_LIMIT, cut to it
    (cut_text). So the term it sends is the term the instance keeps, and a
    label given in another form, or at any length, finds the item written
    for it."""
    return cut_text(stored_term(text), TERM_LIMIT)


def cut_text(text: str, limit: int) -> str:
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
    of the module docstring of yizkr.pipeline.writer), and two labels that
    agree up to the cut name two items.

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
