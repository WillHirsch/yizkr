"""Semantically chunked claim extraction (ADR-0006).

Dense documents overflow a single model response — the constraint is
OUTPUT size (claim JSON amplifies source text ~16x), never input size.
So: one segmentation call maps the transcription to semantic units;
units greedy-pack into chunks; each chunk call keeps the full document
in context (by forking the segmentation conversation where the engine
supports it) but emits claims for its assigned units only; a
deterministic merge namespaces local ids and leaves same-label
unification to the existing label-keyed resolve/write machinery.

Every call goes to the client the extractor is given. Behind the LLM
cache (ADR-0032) a re-run replays the calls that completed and asks the
engine only for the rest, so a failed chunk costs one chunk, not the
document, while parsing, the merge and validation run every time, so a
fix to any of them costs no model call.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import (
    MANIFEST_REASON,
    REJECT_REASONS,
    SCHEMA_REASON,
    STRUCTURED_KINDS,
    ArtifactRef,
    ClaimBatch,
    EntityRecord,
    JudgementNote,
    PersonStructure,
    load_vocabulary,
    set_aside_structure,
)
from yizkr.pipeline.extract import (
    cite_artifact,
    claim_schema_prompt_text,
    document_block,
    entity_kind_prompt_lines,
    placed_by,
    run_metadata,
    salvage_valid_claims,
    scan_json_objects,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    ConversationalLlmClient,
    ConversationChanged,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    for_attempt,
    reject_response,
)
from yizkr.pipeline.stages import atomic_write_text
from yizkr.pipeline.transcribe import Transcription

# Transcriptions above this size use chunked extraction under
# --strategy auto. Conservative on purpose: density (the real driver)
# is unknowable upfront, and chunking a document that would have fit
# costs a few extra calls, never correctness.
AUTO_CHUNK_THRESHOLD = 4_000

# Combined assigned-unit text per chunk. Bounds what a chunk call must
# EMIT claims for (and so its expected output), not what it sees.
DEFAULT_CHUNK_BUDGET = 2_000

# Above this size the transcription is not carried inline per call
# (context mode 3, compressed): long-context attention degradation and
# cost outweigh the benefit.
INLINE_DOCUMENT_LIMIT = 100_000

# The longest a single reason runs inside another message (a chunk's, or
# an attempt's at a fallback tier), so that a failure message, which leads
# with the outcome, stays well within what a stage's failed event keeps.
REASON_LIMIT = 40


def _brief(reason: str) -> str:
    """`reason` on one line, cut to REASON_LIMIT characters."""
    flat = " ".join(reason.split())
    if len(flat) <= REASON_LIMIT:
        return flat
    return flat[: REASON_LIMIT - 1] + "…"


class SegmentationUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: str = "section"
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)
    context: str = ""
    extract: bool = True


class DocumentSegmentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    units: list[SegmentationUnit]

    def problems(self, line_count: int) -> list[str]:
        """Structural violations; empty means usable."""
        errors: list[str] = []
        if not self.units:
            errors.append("no units")
        seen_ids: set[str] = set()
        for unit in self.units:
            if unit.id in seen_ids:
                errors.append(f"duplicate unit id {unit.id!r}")
            seen_ids.add(unit.id)
            if unit.last_line < unit.first_line:
                errors.append(f"unit {unit.id}: last_line < first_line")
            if unit.last_line > line_count:
                errors.append(
                    f"unit {unit.id}: last_line {unit.last_line} beyond "
                    f"document ({line_count} lines)"
                )
        ordered = sorted(self.units, key=lambda u: u.first_line)
        for before, after in zip(ordered, ordered[1:], strict=False):
            if after.first_line <= before.last_line:
                errors.append(f"units {before.id} and {after.id} overlap")
        return errors

    def unsegmented_lines(self, line_count: int) -> list[tuple[int, int]]:
        """Line ranges covered by no unit (gaps are allowed but audited)."""
        gaps: list[tuple[int, int]] = []
        cursor = 1
        for unit in sorted(self.units, key=lambda u: u.first_line):
            if unit.first_line > cursor:
                gaps.append((cursor, unit.first_line - 1))
            cursor = max(cursor, unit.last_line + 1)
        if cursor <= line_count:
            gaps.append((cursor, line_count))
        return gaps


def number_lines(text: str) -> str:
    return "\n".join(
        f"{number}: {line}"
        for number, line in enumerate(text.splitlines(), start=1)
    )


def unit_text(text_lines: list[str], unit: SegmentationUnit) -> str:
    return "\n".join(text_lines[unit.first_line - 1 : unit.last_line])


SEGMENTATION_PROMPT = """\
You segment a document transcription into semantic units for
genealogical claim extraction. A unit is a self-contained region a
person could extract claims from in isolation: one person's entry in a
family tree, one narrative section, one table. Prefer natural units
over fixed sizes.

Output ONE JSON object, no prose, no code fences:
{{"units": [{{"id": "u01", "label": "<short name>",
"kind": "<person entry|narrative section|...>",
"first_line": <int>, "last_line": <int>,
"context": "<what a reader needs from OUTSIDE the unit,
e.g. the ancestor path in a tree: 'child of X and Y (generation 2)'>",
"extract": <true, or false for regions asserting no facts about
people, places or events
(logistics, boilerplate, tables of contents)>}}, ...]}}

Rules:
- Units must not overlap. Cover every line that asserts extractable
  facts; lines you skip are audited as gaps.
- Mark non-assertive regions extract=false rather than skipping them.
- Line numbers refer to the numbering below (1-based, inclusive).

Transcription of artifact sha1:{artifact_sha1} (language: {language}):

{numbered_text}
"""


CHUNK_PROMPT = """\
You extract source-attributed genealogical claims from transcriptions of
historical documents, in whatever language a document is written.

Rules — these mirror the archive's non-negotiable conventions:
- Record exactly what the source asserts. Never round dates, never guess,
  never narrow a vague statement. Use precision (year/month/day/decade/
  century), earliest_date/latest_date qualifiers for ranges, and the
  sourcing_circumstances qualifier with the "circa" vocabulary item for
  approximate values. Never silently disambiguate an ambiguous date
  format — downgrade to the precision actually supported.
- A year alone is year precision, never a full date: "1905" is never
  1 January 1905 at day precision, and "im Mai 1938" is month
  precision. A century or a decade the source implies ("in the
  thirties", "в тридцатые годы") is read in the document's own context
  (the 1930s, in a letter of 1958), never as a literal "1st century" or
  the year 30, with a date_reading note.
- Every claim MUST include the source reference: the artifact
  {{"kind": "artifact", "sha1": "{artifact_sha1}"}}, plus page number and a
  short verbatim transcription excerpt supporting it. The page number is
  the N of the [Page N] (or [Slide N]) marker the text stands under,
  never a printed page number shown beside that marker.
- Use ONLY these property keys (property key: datatype — what it means),
  for claims and qualifiers alike — a claim naming any other key is
  discarded. The description is authoritative: it says what the VALUE
  must be, and so which way a relation points. If the document asserts
  something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- The relation decides the kind (ADR-0031). A relation of place
  (place_of_birth, place_of_death, burial_place, residence, location)
  takes a place; a relation of agency (issued_by, carried_out_by,
  employer, educated_at, member_of) takes an organization (member_of
  also a family). Pick the relation the source best fits, and point it
  at the manifest entity of that kind: a claim whose value is of a kind
  its property does not accept is refused. Where the manifest holds a
  site and an institution of one name, a relation of place takes the
  site and a relation of agency the institution. A claim's value, and a
  qualifier's, is never its own subject. An item value is an entity or
  a vocabulary item its property names: a kind key ("place",
  "time_period_class") is a value of instance_of or subclass_of alone.
- A person's ordinary name lives in their entity-manifest label and
  aliases, and which part of it is which lives in the manifest's record;
  do NOT restate either as a claim, beyond `name` (below). Use the typed
  name properties only when the source marks a name's TYPE: birth_name
  (a name/maiden name given at birth), married_name (taken on marriage),
  nickname (a name marked as what the person is called, such as an
  "aka"), official_name (a formal or legal change).
  The marker may be CONTEXTUAL (a maiden-name cue such as "geb." or
  "née", a married-name cue such as "по мужу", a stated legal or
  immigration change), not only an explicit tag. Type only what a source
  marks (ADR-0022).
  A distinct name the source gives a person beside the label's, whose
  kind the source leaves open (a bracketed name in a register entry:
  "Ida Sörne (Löbig)"), is given as `name`, verbatim (ADR-0011), and
  is an alias too. A form the source sets apart without saying its
  kind is `name` even where it reads like a nickname; a nickname with
  no `name` is one used in running text or a greeting, and follows the
  nickname and appellation rules below. A shortening, an initial, a
  spelling variant or a part of the label is an alias alone, never
  `name`, and so is a stand-in for an unknown name ("N.N."). Never
  give the label's own form as `name`, and never a form you type.
- A word that states a fact rather than naming someone is a claim, never
  part of a name (ADR-0035 §3.1):
  * a widow's or widower's marker ("Wwe.", "вдова", "widdo"): the
    person's `civil_status` `widowed`; where it names the late spouse
    ("Witwer der Cäcilie Löbig geb. Tarle"), also their marriage, ended
    by that spouse's `death` event (below);
  * "the late" ("verstorben", "покойный", "sillige"): that person's
    `death` event, with them as its principal, dated only as the source
    dates it;
  * a patronymic ("Феликсович", "Васильевна"): `father`, where the
    manifest holds the father it names;
  * a married-name marker: `married_name`, and `birth_name` for the name
    the marker sets apart as the person's own;
  * an occupation beside a name ("Hutmachermeister"): `occupation`.
  Where no listed property expresses what a marker asserts, write a gap
  line (below), never a name part.
- `civil_status` records a civil status the source states (unmarried,
  meaning never married; married; widowed; divorced), with point_in_time
  the date at which the source presents it as holding. It names no
  partner and makes no marriage.
- Record `sex_or_gender` (male/female vocab) only where the source states
  it, in words or in its grammar ("den Schüler"), never from a given
  name.
- Kinship: use the primitives father, mother, sibling, child. Do NOT
  invent properties for compound relations. A literal kinship term that
  reduces to a primitive stays one: "Dein Vater" is `father`. A compound
  term you cannot reduce ("Dad's cousin", "uncle on the father's side",
  a great-grandmother) is `relative`, with the OTHER party (the "our" /
  "my" / "his" ego) as the object, qualified by:
  * `kinship_type`: the term exactly as the source words it;
  * `kinship_class`: its class, one of the kin_ items, or kin_relative
    where the class is unclear;
  * `side` (paternal or maternal), `degree` (how many "great-"s, or a
    cousin's number) and `removes` (a cousin's removes): ONLY where the
    term itself states them, never guessed. Apart from a cousin, a
    kinship term with no "great-" has degree 0.
  "дядя по отцу" (uncle on the father's side) is kin_aunt_or_uncle of
  degree 0 on the paternal side; "двоюродная сестра" (first cousin) is
  kin_cousin of degree 1; "oerbeppe" (great-grandmother) is
  kin_grandparent of degree 1; a bare "cousin" states no degree. A
  spousal term ("my husband") gives a marriage (below), never a kinship
  class.
- Appellations (ADR-0018). Every form a source uses to name or address
  a person is an alias, with no distinction between them: a name form,
  a kinship term with a name ("Tante Rahel"), a kinship term alone
  ("Omama"), a pet name ("Pünktchen", "Bubele") and a courtesy name
  ("Nana Maisie"). A form that is no plain name form (a name, or a
  spelling, a shortening, a diminutive or an initial of one, such as
  "Gustl" for Gustav) is an appellation: it is also one `referred_to_as`
  claim, the form verbatim with its excerpt, with a `perspective`
  qualifier where the source gives the ego as an entity. A description
  that is no form of address is never an alias: "the deceased", "the
  informant", a role or form-field label, "my husband", "our great
  grandmother".
  A name the source gives as what the person is called, by anyone
  ("Peppi", "Gustl", "Pünktchen"), is also a record nickname, and a
  `nickname` claim only where the source marks the type. A form one
  person calls another, or that names the person through a relation
  ("Bubele", "Nana Maisie", "Рудольфиха", "Tante Rahel", "Omama"), is
  never a record nickname. A name a writer simply uses for someone, in
  a greeting ("Liebes Pünktchen!") or a mention, is their name form
  (label or alias), and an appellation too where it is no plain name
  form.
  Give each appellation of the person as ONE `referred_to_as` claim
  (the form verbatim, never cut to its name or its kinship word; a form
  the document repeats is still one claim), and add a `perspective`
  qualifier naming the ego (the person or family whose viewpoint it is)
  ONLY when that ego is an entity you can point to. Omit `perspective`
  when the ego is a bare "our"/"we" with no named person or family —
  never invent an entity to fill it. Then:
  * If it holds a LITERAL kinship term, record the kinship too, as
    above: "Tante Rahel", where Rahel is the aunt, also gives `relative`
    + `kinship_type` "Tante" + `kinship_class` kin_aunt_or_uncle, with
    the ego as the object.
  * If it is NOT a literal relation (a courtesy title such as a "Tante"
    who is no aunt, "Nana Maisie", a pet name, or a name one person
    calls another, "Bubele"), it gives no kinship: `referred_to_as`
    alone, beside a `nickname` claim where the source marks a pet name
    as what the person is called.
  When unsure whether a term is literal kinship, give `referred_to_as`
  alone (it asserts only that the term was used, never a relation). A
  description ("meine Oma", "our great grandmother") gives no
  `referred_to_as`: a literal kinship term in it gives the kinship, a
  spousal term a marriage.
  A kinship word is never the label of a person the document names:
  take the name, never the kinship word. A kinship term alone ("Oma",
  "Omama", "Mum", "Grandma", "Mem") or with a name ("Aunt Pünktchen",
  "Tante Rahel", "дядя Себастьян") is an alias and an appellation, and
  the name beside it follows the label and alias rules: a nickname is an
  alias too ("Pünktchen"), and a given name that is the person's only
  name in the document is the label ("Себастьян"); a writer's "your
  father" ("Dein Vater", "Ваш отец") for a person named elsewhere leaves
  that name the label. Only a person the document never names is
  labelled with its kinship words.
  For example: a letter to Klara that writes "Tante Rahel" and
  elsewhere "Rahel Löbig" gives Rahel the claims `referred_to_as` "Tante
  Rahel", with Klara as its `perspective`, and `relative` to Klara with
  the kinship_type "Tante" and the kinship_class kin_aunt_or_uncle. A
  letter of Nicholas's that calls his grandmother Hildegard "Omama"
  gives her `referred_to_as` "Omama", with Nicholas as its
  `perspective`, and `relative` to Nicholas with the kinship_type
  "Omama" and the kinship_class kin_grandparent, whether it names her
  elsewhere ("Hildegard Löbig", her label) or never (when "Omama" is her
  label). The
  postcard's "Liebes Pünktchen!" gives Klara `referred_to_as`
  "Pünktchen", with its writer as its `perspective`: a pet name, and no
  relation.
- A count of children the source states is the parent's
  `number_of_children`, whether or not it names them.
- Events and their participants (ADR-0035 §3.2): a `participant` claim
  has the EVENT as subject and the person as value, with an
  `object_has_role` qualifier saying how they took part: `principal` (in
  a birth or a death the one born or who died; in any other event the
  person it happens to, such as the deportee, the prisoner or the
  emigrant), `parent`, `informant` (who reported it), `witness` or
  `officiant` (who performed it). One person may take part in several
  roles, each a qualifier on one claim: a father who reports his son's
  birth is `parent` and `informant`. A death's principal is never its
  informant. An institution that carried out an event (a court, an
  authority) is `carried_out_by` on the event, not a participant.
- Marriages and partnerships are entities bounded by events (ADR-0036);
  no property links two spouses directly:
  * the couple take part in the `marriage` entity (a `partnership` where
    the source presents a couple without saying they married): each is
    a `participant` of it with the role `spouse` (`partner` in a
    partnership);
  * its wedding is an event of its own: the union is `started_by` it,
    and the wedding carries its own `point_in_time`, `location`, and
    any witness or officiant as a participant with that role. The couple
    take part in the union, not in its wedding;
  * its end is an event too: the union is `ended_by` a `divorce` or an
    `annulment` (with its own date and place, and `carried_out_by` the
    court or authority that pronounced it), or by a partner's own
    `death` event, or by an `event` where the source does not say how
    the union ended;
  * a source that merely names a spouse dates nothing, and the
    document's own date is no date of the marriage; a directory or a
    census that lists a couple as married gives the union a
    `point_in_time`, an observation, never a wedding date.
{document_paragraphs}
- Residence: an address stated on a form is asserted for the person that
  field names (e.g. the informant/applicant on a certificate), NOT for
  everyone the document mentions. Do not copy one person's stated address
  onto others as their own `residence` — co-residence ("lived with") is
  derived later from separately-sourced residences over overlapping time,
  never asserted here. If you do record a dependent's residence from a
  relative's address (e.g. a newborn at the parents' address), lower its
  confidence to mark the inference.
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
- Give a place's containment (`part_of`, its country among them) only
  where the source states it: in the place's mention ("Fläckfeld, Kreis
  Mockburg"), in the fields a form gives that place (a certificate's
  village, district and oblast fields), or in a chain of containers,
  which states each link ("Snylsjum, gemeente Drakeradiel, Fryslân" also
  puts gemeente Drakeradiel in Fryslân). Containment the source does not
  state is world knowledge, proposed later by the place pass (ADR-0009):
  never infer it from what you know of a place (that Flörburg lies in
  Kreis Peiltal, in Hesse), and never carry it over from another place
  the document names.
  Where a source writes "Flörburg, Kreis Peiltal" and, elsewhere, a bare
  "Fläckfeld", Fläckfeld takes no `part_of` at all.
- Persecution and displacement are EVENTS, not relations and not
  locations on a person (ADR-0014). "Deported to Klurnau in 1942" is a
  `deportation`; "held at", "interned at", "taken to" an
  `imprisonment`; "released from", "freed from" a `release`. Put the
  claims on the EVENT: `participant` (the person, role `principal`),
  `location` (the camp, ghetto, or prison), `point_in_time` (the date) —
  or `start_time`/`end_time` for a period of detention. Never record the
  camp or prison as the person's own `location` or `residence`, and
  never emit a gap line for a relation the event model already
  carries. If no such event is available to you, a gap line is the
  honest fallback — but check first: it should be rare.
- A claim may carry a judgement note, "note": {{"category": "<kind,
  relation, date_reading, identity or other>", "text": "<one sentence in
  English>"}}, ONLY where you made a judgement call that a careful reader
  could make differently: a date_reading note such as "the postmark
  '19.8.38' read as 1938", a relation note such as "'Dein Vater' recorded
  as Klara's father, not as a relative". Leave it out for a plain
  reading. A note is your remark on your reading, never a measure of
  certainty.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

{assignment_block}

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of this JSON Schema:

{schema}

If the source asserts a RELATION that no listed property expresses AND
no event kind captures (e.g. "godparent of", "buried alongside"), do
NOT force it into an adjacent-but-wrong property and do NOT silently
drop it: emit a GAP line instead —
{{"gap": {{"subject": "<local_id or name>",
"predicate": "<the relation, verbatim>",
"object": "<local_id, name, or literal>", "evidence": "<short excerpt>"}}}}
Gaps are reviewed to grow the vocabulary; they are never invented, and
never used for something the vocabulary already holds. Check the
property list and the persecution-event rule before emitting one.

After the last line, end with exactly one final line:
{{"done": true}}
{document_block}"""


LINE_ASSIGNMENT_BLOCK = """\
Refer to people, places, organizations, events, unions and occupations
with "local" references (stable local_id per entity within this
response, label as written — prefer the fullest name the document gives
for a person anywhere, never a kinship word where the document names the
person, instance_of from: {kinds}).
A group of people is never one human: it is a family where the source
presents a family or household, else each member it names. Where it
names none of a person's children, their count is that person's
`number_of_children`. A year or a date alone is no entity and no event:
it dates the event it belongs to. `unclassified_entity` is right only
where no kind fits: an agency, an office or a firm's department is an
organization; a cemetery or a building a place; a named ship a vessel.
A named aircraft is no vessel: it is `unclassified_entity` with the
type_hint "aircraft".
The relation decides the kind: an institution named only as where
something happened is a place; one the source shows acting (issuing,
employing, educating, carrying out an event) is an organization.
An occupation value keeps the source's exact wording as its label.

Extract claims ONLY for the assigned units below — nothing from other
parts of the document, even though you can see them. Use the rest of
the document only as context (identity, relationships, spellings).
Ownership rule: every claim whose SUBJECT is a person/place/event/union
of an assigned unit belongs to you, including relations to entities
outside the unit — a unit person's father/mother from the enclosing tree
structure or the unit's context line, their unions, their instance_of,
and the `issued_by` claim of an issuer the unit names. A claim about the
document itself, and a `caption` claim, belongs to the unit whose lines
hold the passage that states it.
Do not skip a claim because its object or evidence lies partly outside
your units.

Assigned units:
{units_block}"""


ENTITY_ASSIGNMENT_BLOCK = """\
Refer to EVERY entity — subjects and objects alike — with a "local"
reference whose local_id is the entity's manifest id and whose label is
the manifest's canonical label. Ids outside the manifest are rejected.
The full manifest (id = canonical label):
{roster_block}

Emit every claim whose SUBJECT is one of your ASSIGNED entities below —
and only those. Relations to entities outside your assignment (their
parents, unions, locations) still belong to you when your entity is
the subject; refer to the other entity by its manifest id. The
`issued_by` claim, whose subject is the document itself, belongs to the
chunk assigned the organization that issued it, and so does every other
claim about the document itself whose value is an entity of the manifest
(its author, addressee, place of creation, subject, whom it shows) to
the chunk assigned that entity. An `inception` claim, and every
`caption` claim, belongs to the chunk assigned {first_entity}, the
manifest's first entity. Do not emit claims whose
subject is not assigned to you, and do not emit instance_of claims (the
manifest's kind already records it).

Assigned entities:
{entities_block}"""


ENTITY_MANIFEST_PROMPT = """\
You are preparing a genealogical claim extraction. First pass: read the
whole transcription and produce a manifest of EVERY distinct entity it
mentions — people, places, organizations, families, events (births,
deaths, weddings, divorces, migrations, military service), marriages and
partnerships, and occupations or professions it names for people (kind
occupation_class, label in the source's exact wording). The document may
be in any language: read it by its own conventions, never by a rule
about a language.

The relation decides the kind (ADR-0031). An entity's kind is the one
the relations this source states of it call for:
- a relation of place (born, died or buried at, lived at, located at,
  deported to, held at) takes a `place`. A camp, a prison, a hospital, a
  school or a congregation named only as WHERE something happened is a
  place: the registration card's "held in Peiltal court prison" names a
  place;
- a relation of agency (issued a document, carried out an event,
  employed, educated, had as a member) takes an `organization`, an
  institution the source shows ACTING: the Landgericht Rekau, which
  divorces Leopold and Agnes; the Städtische Realschule zu Flörburg,
  whose pupil Rudolf was; the Калдовское районное бюро ЗАГС, which issued
  the 1941 death certificate;
- where one source uses one institution both ways, give TWO entities, a
  place for the site and an organization for the body, linked by the
  organization's `location` (the site).
Also:
- a person who holds an office (a consul, a director, a rabbi, a mayor,
  a registrar) is a `human`, never the organization or the office they
  hold;
- a religion, a denomination or an ethnicity is never an
  `occupation_class`;
- a named family or household ("the Löbigs") is a
  `family`; a ship is a `vessel`; a cited law, decree, gazette, or
  register is a `legal_instrument`; a regiment or brigade is a
  `military_unit`;
- a group of people is never one `human`. "The Löbig children" or
  "Sebastian's children" is a `family` where the source presents them as
  a family or household; else each child the source names is a `human`
  of their own (Ursula, Klara, Gustav); where it names none ("drei
  Kinder"), the group is no entity at all. A count of children the
  source states is the parent's `number_of_children` claim at
  extraction, whether or not it names them;
- a year or a date alone ("1938", "im Jahre 1905") is never an entity,
  and never an `event`: it is a time value (`point_in_time`,
  `start_time`, `end_time`) on the event it dates, given at extraction;
- a forced removal is a `deportation` event, a detention an
  `imprisonment` event, a freeing a `release` event (each an event with a
  person participant, a place, and a date) — not a place or a bare
  relation;
- a marriage is an entity, not an event (ADR-0036): a `marriage` for a
  couple the source says married, a `partnership` for a couple it
  presents as a couple without saying they married (two people named
  only as a child's parents are not thereby a couple). Its wedding (a
  `wedding`, or a `civil_wedding` or `religious_wedding` where the source
  states the kind) and a `divorce` or `annulment` that ended it are
  events of their own;
- a marker of a death ("the late", "verstorben", "покойный", "sillige")
  gives that person's `death` event; a widow's or widower's marker that
  names the late spouse ("Witwer der Cäcilie Löbig") gives the marriage
  and the spouse's `death` event, and one that names no spouse
  ("Löbig, Ursula, Wwe.") gives neither.

Unify aliases: one entity that appears as "Cäcilie Löbig", "Cäcilie", and
"C. Löbig" is ONE manifest entry whose label is the fullest name the
document gives and whose aliases list every other form, verbatim.
Every form a source uses to name or address a person is an alias, with
no distinction between them: a name form, a kinship term with a name
("Tante Rahel"), a kinship term alone ("Omama"), a pet name
("Pünktchen", "Bubele") and a courtesy name ("Nana Maisie"). A form that
is no plain name form (a name, or a spelling, a shortening, a diminutive
or an initial of one, such as "Gustl" for Gustav) is an appellation: it
is also one `referred_to_as` claim, the form verbatim with its excerpt,
with a `perspective` qualifier where the source gives the ego as an
entity. A description that is no form of address is never an alias:
"the deceased", "the informant", a role or form-field label, "my
husband", "our great grandmother". So aliases are NOT:
- role or form-field labels — MOTHER, FATHER, CHILD, INFORMANT, BRIDE,
  WITNESS name a ROLE, not the person;
- descriptions of a relationship or viewpoint — "my husband", "our
  great grandmother", "meine Oma", "the deceased", a writer's "your
  father" for a person the document names. They are captured as claims
  at extraction (a literal kinship term becomes `relative` +
  `kinship_type`; a spousal term gives a marriage). Never record any of
  these as an alias (ADR-0018);
- for a place, a deictic or locative reference back to it ("dort",
  "hier", "daselbst", "ebenda", "there") or a generic one ("the
  village"): a place's alias is a toponym;
- for an organization, a generic phrase that names no one ("die Firma",
  "the company", "the bank"): an organization's alias is a name.
A name the source gives as what the person is called, by anyone
("Peppi", "Gustl", "Pünktchen"), is also a record nickname, and a
`nickname` claim only where the source marks the type. A form one
person calls another, or that names the person through a relation
("Bubele", "Nana Maisie", "Рудольфиха", "Tante Rahel", "Omama"), is
never a record nickname. A name a writer simply uses for someone, in a
greeting ("Liebes Pünktchen!") or a mention, is their name form (label
or alias), and an appellation too where it is no plain name form.
A kinship word is never the label of a person the document names: the
label takes the name, never the kinship word, and the form stays an
alias (ADR-0018). Three cases:
- a kinship term alone ("Oma", "Omama", "Mum", "Grandma", "Mem",
  "дядя"): an alias, and the label only of a person the document never
  names (below);
- a kinship term with a name ("Aunt Pünktchen", "Tante Rahel", "дядя
  Себастьян"): the whole phrase is an alias, never the label, and the
  name follows the label and alias rules below: a nickname is an alias
  too ("Pünktchen"), and a given name that is the person's only name in
  the document is the label ("Себастьян");
- a writer's "your father" ("Dein Vater", "Ваш отец", "your aunt") for a
  person the document names elsewhere: the label is that name, and the
  phrase, a description, is neither label nor alias.
A person the document does not name is labelled with the document's own
words for them: the postcard's writer signs "Dein Vater", so that is his
label. Only where the document has no words for a person at all do you
compose a label, such as "Klara's father".
For example: a letter to Klara that writes "Tante Rahel" and elsewhere
"Rahel Löbig" gives one entity labelled "Rahel Löbig" with the alias
"Tante Rahel". A letter of Nicholas's that calls his grandmother
Hildegard "Omama" and names her "Hildegard Löbig" gives the label
"Hildegard Löbig" and the alias "Omama"; one that never names her gives
the label "Omama" (status `kinship`). The postcard's "Liebes Pünktchen!"
gives Klara the alias "Pünktchen", a pet name. Each of these forms is
also a `referred_to_as` claim at extraction.

Name composition and the canonical label (ADR-0022):
- Do NOT enumerate full-name PERMUTATIONS as aliases. A "/" between family
  names ("Ida Sörne/Löbig") is ONE person with alternative family names held
  at different times: give the label ONE clean form (given + one surname),
  list the OTHER surname forms as aliases, and do NOT emit the composed
  cross-products ("Ida Löbig"). Never guess which surname is maiden vs
  married — that is a name TYPE, recorded only when the source marks it.
- Do NOT list a bare given name ("Sebastian") or an initialism ("S. Löbig")
  as an alias — a reader derives those from a fuller form. A genuinely
  distinct informal form ("Wastl") IS an alias.
- Keep the canonical label CLEAN: parenthetical name metadata is NOT part of
  it. "Hildegard Löbig (b. Laudler)" has label "Hildegard Löbig"; "Gustav
  Löbig (later Gustave Lobig)" has label "Gustav Löbig" with "Gustave Lobig"
  as an alias. Keep hyphenated and space-separated compound/particle
  surnames ("A-B", "de la Cruz", "van 't Nijks") WHOLE — never split them.

The mention record (ADR-0035): give each `human`, `place`, `organization`
and `family` entity a "structure", what its names are made of, read from
its label and aliases as the document writes them. Divide and label the
words; never add one that is in neither the label nor an alias (a given
name's standard form aside), and leave out every field the document does
not settle: each is optional. Omit a field you cannot fill, never write it
as an empty string ("").
- A person: {{"kind": "human", "status": "<status>",
  "given": [{{"written": "...", "standard": "...", "initial": false}}],
  "surnames": [{{"written": "...", "type": "<birth, married or other>"}}],
  "patronymics": [...], "nicknames": [...], "titles": [...],
  "suffixes": [...], "unsplit": [...]}}.
  * status says what the label is: `name` where it names the person;
    `role`, a role or form-field label (INFORMANT) or an office the
    source names alone ("the consul"); `kinship`, a literal kinship term
    ("Dein Vater", "Mem", "Omama" for a person the document never
    names); `courtesy`, a courtesy title or other relational
    appellation that is no literal relation ("Nana Maisie");
    `placeholder`, the source's stand-in for a name it does not know
    ("N.N. Löbig"); `description`, a label you composed.
  * The document's conventions decide which part is which. A register,
    card or form that writes the surname first, or in capitals, is read
    so: the card's "LÖBIG, Ida, née SÖRNE" gives the given name Ida and
    the surnames LÖBIG and SÖRNE; the Soviet certificate's "Лёбиг Рудольф
    Феликсович" gives the surname Лёбиг, the given name Рудольф and the
    patronymic Феликсович. Where the document does not settle which word
    is the given name, put the form in `unsplit`.
  * A surname is `birth` or `married` only where the source marks it
    (the card's "née SÖRNE" is `birth`; "по мужу Лёбиг" is `married`),
    else `other`. A title never types a surname. A particle stays in its
    surname: "S. van 't Nijks" gives the initial S. and the surname
    "van 't Nijks".
  * A standard form is the full form of a given name in its usual
    spelling that a diminutive or an abbreviation stands for, given only
    where the document supports it: the 2008 family history's "became
    Gustave Lobig here, Gus to everyone" makes its Gus Gustave. A full
    name is its own standard form; a standard form is never a
    translation; an initial has none.
  * A name the source gives as what the person is called, by anyone
    ("Peppi", "Gustl", "Pünktchen"), is also a record nickname, and a
    `nickname` claim only where the source marks the type. A form one
    person calls another, or that names the person through a relation
    ("Bubele", "Nana Maisie", "Рудольфиха", "Tante Rahel", "Omama"), is
    never a record nickname. A name a writer simply uses for someone, in
    a greeting ("Liebes Pünktchen!") or a mention, is their name form
    (label or alias), and an appellation too where it is no plain name
    form. A nickname that is a form of a given name ("Gustl", "Gus") is
    also that given name, with its standard form where the document
    supports it; one that stands for no given name ("Peppi") has no
    standard form.
  * A kinship or relational word before a name ("Tante", "Onkel", "Oma",
    "Aunt"), literal or a courtesy one, is part of the appellation (an
    alias and a `referred_to_as` claim), never part of the record:
    neither a name part nor a `titles` entry. "Tante Rahel" gives the
    given name Rahel and nothing for "Tante".
  * A patronymic, a name formed from the father's given name, is a part
    of its own, never a surname. Titles are honorifics and ranks (Mrs,
    Dr., Frau, Fräulein, Mefrou, Rabbi), never a kinship word; titles and
    suffixes (generational and status words, such as "Wwe.") never name
    anyone. A word that states a fact (an occupation beside a name, a
    widow's marker, "the late") is no name part: it becomes a claim at
    extraction.
- A place: {{"kind": "place", "status": "<toponym, deictic or generic>",
  "names": [{{"written": "...", "toponym": "...", "head": "..."}}],
  "address": {{"written": "...", "street": "...", "number": "..."}},
  "within": [...]}}.
  * status: `toponym` for a place's own name; `deictic` for a reference
    back ("dort"); `generic` for a head with no toponym ("Lager").
  * a name's toponym and generic head, where they can be told apart:
    "Lager Klurnau" has the head Lager and the toponym Klurnau.
  * the address, a street and a house number as written: "8 Hawthorn
    Road, Stupfield, Victoria" has the address 8 Hawthorn Road (street
    Hawthorn Road, number 8), within Stupfield and Victoria.
  * within: the places the source puts it in, as written, in the
    source's order: "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân"
    is Snylsjum within "gemeente Drakeradiel", "Fryslân" and "Nederlân".
- An organization: {{"kind": "organization", "names": [...],
  "kinds": [...], "places": [...]}}: its proper names, the words naming
  its kind of institution, and the places its names carry. "Hutmacherei
  zum Blauen Hut" has the name "zum Blauen Hut" and the kind Hutmacherei;
  "It tichelwurk fan Snylsjum" has no proper name, the kind tichelwurk
  and the place Snylsjum; "Standesamt Flörburg" has no proper name, the
  kind Standesamt and the place Flörburg. A generic word any
  organization carries (a word for a firm, a legal form) is neither a
  name nor a kind.
- A family: {{"kind": "family", "surnames": [...]}}: "the Löbigs" has the
  surname Löbig.
Other kinds take no structure.

A judgement note: an entity may carry "note": {{"category": "<kind or
identity>", "text": "<one sentence in English>"}}, ONLY where you made a
kind or identity call that a careful reader could make differently, such
as "the court prison typed a place: the card names it only as where
Leopold was held". Leave it out for a plain reading.

Output ONE JSON object, no prose, no code fences:
{{"entities": [{{"id": "e01", "label": "<canonical, fullest form>",
"aliases": ["<other forms, verbatim>", ...], "kind": "<one of: {kinds}>",
"summary": "<one line: who/what this is, with key dates/relations>",
"mention_lines": [<line numbers where it is mentioned>],
"type_hint": "<only when kind is unclassified_entity — see below>",
"extract": <true, or false — see below>,
"structure": {{<the mention record, for the four kinds above>}},
"note": {{<only for a judgement call>}}}}, ...]}}

Rules:
- One entry per real-world entity, however many aliases it has.
- ids must be unique. Do NOT include a "qid" field.
- NEVER force an entity into a wrong-but-adjacent kind. If an entity
  clearly exists but NO kind in the list describes it (an animal the
  source names, a named object that is no document), use kind
  `unclassified_entity` and set `type_hint` to what it actually is, in
  English whatever the document's language ("horse"). This is a LAST
  resort, right only where nothing in the list fits — use a real kind
  whenever one fits; `unclassified_entity` without a type_hint is
  invalid. These always have a real kind, the one the relation calls
  for (above):
  * an agency, a ministry, an office, a chancellery, a registry or a
    firm's department is an `organization` (the Wiedergutmachungsamt
    Peiltal, the Standesamt Flörburg), or a `place` where the source
    names it only as where something happened;
  * a cemetery, a building, an address or another site is a `place`
    (the Friedhof Flörburg, Brunnenring 35), unless the source shows it
    acting (above);
  * a named ship is a `vessel` (the RMS Flund);
  * a law, a decree, a gazette or a register the source cites is a
    `legal_instrument` (above).
  A letter or a certificate the source mentions, other than the document
  you are reading, has no kind in the list: it is `unclassified_entity`
  with its type_hint ("letter", "death certificate"), as the 1958
  letter's enclosed certificate of Rudolf's death is. A named aircraft
  is no `vessel` either: it is `unclassified_entity` with the type_hint
  "aircraft".
- Prefer completeness: an entity mentioned once still gets an entry.
- extract=false for entities that appear ONLY in non-assertive context:
  travel or event logistics (schedules, bookings, contact lists), people
  taking part in present-day travel or events, boilerplate.
  They are recorded for audit but no claims are extracted about them.
  Entities the document asserts historical/genealogical facts about get
  extract=true.
- On official certificates and registers the administrative apparatus is
  boilerplate: the registrar, officiant, or witness who only certifies
  the record, and the register or certificate the document cites as its
  own source, are extract=false. They attest the record — they are not
  its genealogical subject. Whom the source shows acting is the
  exception, extract=true: the officiant or a witness of the event the
  document records (a wedding's registrar), the authority that issued
  the document (a civil registry office such as a Standesamt), and a
  witness or officiant who is ALSO a relative of the subject.
- summary is evidence for later identity matching: pack it with the
  document's key facts (born/died/married when-where, parents, the
  person married).

Entity kinds — pick the one whose meaning actually fits, not the
nearest-sounding one:
{kind_descriptions}

Transcription of artifact sha1:{artifact_sha1} (language: {language}):

{numbered_text}
"""


class ChunkIncomplete(Exception):
    """A chunk answer cut off: stopped at the engine's output limit, or
    without its terminal sentinel."""


class ExtractionFailed(RuntimeError):
    """The extraction ended with no claims for want of a usable answer: a
    chunk failed, the chunks' answers held objects the parser could not
    read as claims, or validation rejected every claim. The message leads
    with that outcome and its causes, then names any fallback tier taken,
    with the reason each attempt at the refused turn gave; it is the
    reason the extraction stage records.

    An extraction that ends with no claims because the answers it accepted
    say the document holds none, on whichever tier, is not a failure: its
    batch is returned, and the extractor's `attention` says why it is
    empty."""


def parse_chunk_response(
    response: str, *, cut_off: bool = False
) -> list[dict]:
    """The claims of a chunk answer: NDJSON claim lines, then the terminal
    sentinel, a line whose `done` is true (`{"done": true}`), which shows
    the answer ran to its end. What else the sentinel carries is not read
    here (parse_chunk_unread reads it).

    An answer is cut off when its engine stopped it at its output limit
    (`cut_off`, llm.at_output_limit), whatever it holds, the sentinel
    included, or when it has no sentinel (silent truncation). Either
    raises ChunkIncomplete, which is cheap to retry, unlike a
    whole-document retry.
    """
    claims: list[dict] = []
    finished = False
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
        elif "subject" in obj and "property" in obj:
            claims.append(obj)
    if cut_off:
        raise ChunkIncomplete(CUT_OFF_AT_LIMIT)
    if not finished:
        raise ChunkIncomplete("no terminal sentinel — response truncated?")
    return claims


def _gap_line(gap: Any) -> bool:
    """Whether the value under a `gap` key is one parse_chunk_gaps reads
    as a gap: an object with a subject and a predicate."""
    return (
        isinstance(gap, dict)
        and bool(gap.get("subject"))
        and bool(gap.get("predicate"))
    )


def parse_chunk_gaps(response: str) -> list[dict]:
    """Gap lines (ADR-0013): relations no property expresses, surfaced
    for review, never written to the graph.

    A gap line is ``{"gap": {"subject", "predicate", "object",
    "evidence"?}}``. Malformed gaps are dropped silently — a gap is an
    advisory signal, never a gate, so it must never fail a chunk (that is
    the sentinel's job).
    """
    gaps: list[dict] = []
    for obj in scan_json_objects(response):
        gap = obj.get("gap")
        if _gap_line(gap):
            gaps.append(
                {
                    "subject": str(gap.get("subject", "")),
                    "predicate": str(gap.get("predicate", "")),
                    "object": str(gap.get("object", "")),
                    "evidence": str(gap.get("evidence", "")),
                }
            )
    return gaps


def _holds(value: Any) -> bool:
    """Whether a JSON value holds something at any depth: an object when
    one of its values does, a list when one of its items does, anything
    else when it is not empty (null, false, 0 or "").

    It walks the value with a stack of its own rather than by recursion,
    so a nest as deep as json decodes never raises RecursionError."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif item:
            return True
    return False


_GAP_FIELDS = frozenset({"subject", "predicate", "object", "evidence"})


def _merged_gap_line(gap: Any) -> bool:
    """Whether the value under the terminal sentinel's `gap` key is a gap
    line merged into it: a gap line (_gap_line) that carries nothing else
    that holds something, no list or object that holds something under a
    key other than the four that parse_chunk_gaps reads."""
    return _gap_line(gap) and not any(
        isinstance(value, (list, dict)) and _holds(value)
        for key, value in gap.items()
        if key not in _GAP_FIELDS
    )


def parse_chunk_unread(response: str) -> list[dict]:
    """The JSON objects of a chunk answer that hold something the claim
    parser cannot read: claims wrapped in one object, say, or written with
    other keys than `subject` and `property`.

    Whether an object holds something (_holds) decides, not its shape. A
    claim and a gap line (well formed or not) are read. The terminal
    sentinel is read too, unless one of its keys holds a list or object
    that holds something, such as claims carried inside it. A note in a
    string on it is harmless, and so is a gap line merged into it
    (_merged_gap_line), which parse_chunk_gaps reads: a `gap` that has a
    subject and a predicate and carries nothing else that holds something
    in a list or object. Anything else under `gap`, such as claims, a gap
    line that carries them or a list of gap lines, is unread like a list
    of notes or of line numbers under any other key, since a failure that
    shows, which `--refresh extract` asks the model again for, is
    preferred to claims lost in silence. Any other object is unread unless
    it holds nothing at any depth, as in `{}`, `{"claims": []}` or
    `{"result": {"claims": [{}]}}`, which say there is nothing.

    They are counted, never guessed at. A chunk whose answer held such
    objects and no claim reads as unreadable rather than as answered with
    none, so an extraction left without claims by them fails
    (_refuse_empty) instead of passing for a claim-free document's.
    """
    unread = []
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            carried = [
                value
                for key, value in obj.items()
                if not (key == "gap" and _merged_gap_line(value))
                and isinstance(value, (list, dict))
                and _holds(value)
            ]
            if carried:
                unread.append(obj)
        elif "gap" in obj or ("subject" in obj and "property" in obj):
            continue
        elif _holds(obj):
            unread.append(obj)
    return unread


def pack_units(
    units: list[SegmentationUnit],
    text_lines: list[str],
    budget: int = DEFAULT_CHUNK_BUDGET,
) -> list[list[SegmentationUnit]]:
    """Greedy-pack extractable units, in document order, under budget.

    A single unit over budget becomes its own chunk (the multi-turn
    reassembly nets are the backstop for that case).
    """
    chunks: list[list[SegmentationUnit]] = []
    current: list[SegmentationUnit] = []
    current_size = 0
    for unit in sorted(units, key=lambda u: u.first_line):
        if not unit.extract:
            continue
        size = len(unit_text(text_lines, unit))
        if current and current_size + size > budget:
            chunks.append(current)
            current, current_size = [], 0
        current.append(unit)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _rewrite_local_refs(claim: dict, fix) -> dict:
    """Deep-copy a claim dict and apply `fix` to every local ref in it:
    its subject's, its item value's and its qualifiers' item values'. A
    local ref the model put in the source is left as it is: the extractor
    replaces the source artifact afterwards (cite_artifact)."""
    fixed = json.loads(json.dumps(claim))  # deep copy, JSON-native

    def visit(ref: Any) -> None:
        if (
            isinstance(ref, dict)
            and ref.get("kind") == "local"
            and isinstance(ref.get("local_id"), str)
        ):
            fix(ref)

    visit(fixed.get("subject"))
    value = fixed.get("value")
    if isinstance(value, dict):
        visit(value.get("value"))
    for qualifier in fixed.get("qualifiers") or []:
        if isinstance(qualifier, dict) and isinstance(
            qualifier.get("value"), dict
        ):
            visit(qualifier["value"].get("value"))
    return fixed


def pack_entities(
    entities: list[EntityRecord],
    text_lines: list[str],
    budget: int = DEFAULT_CHUNK_BUDGET,
) -> list[list[EntityRecord]]:
    """Greedy-pack entities, in manifest order, under the output budget.

    An entity's size is its evidence volume (total text of its mention
    lines) when mention_lines is usable, else a flat estimate — the
    budget bounds what a chunk must emit claims about, not what it
    sees.
    """
    flat_estimate = max(1, budget // 10)
    chunks: list[list[EntityRecord]] = []
    current: list[EntityRecord] = []
    current_size = 0
    for entity in entities:
        if not entity.extract:
            continue
        lines = [
            number
            for number in entity.mention_lines
            if 1 <= number <= len(text_lines)
        ]
        size = (
            sum(len(text_lines[number - 1]) for number in lines)
            if lines
            else flat_estimate
        )
        if current and current_size + size > budget:
            chunks.append(current)
            current, current_size = [], 0
        current.append(entity)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def normalize_to_manifest(
    claim: dict, records: dict[str, EntityRecord]
) -> dict:
    """Normalize local refs to the manifest: canonical label, kind.

    Labels identify entities, so they take the manifest's canonical
    form; transcription excerpts stay verbatim — they quote the source.
    Unknown ids are left untouched for validation to reject.
    """

    def fix(ref: dict) -> None:
        record = records.get(ref.get("local_id"))
        if record is not None:
            ref["label"] = record.label
            ref["instance_of"] = record.kind

    return _rewrite_local_refs(claim, fix)


def prefix_local_ids(claim: dict, prefix: str) -> dict:
    """Chunk-namespace every local id so ids never collide across
    chunks (line-range fallback path only — entity-first ids are
    document-scoped manifest ids). Same-label unification stays with
    the label-keyed resolve/write machinery — identity is not decided
    here."""
    return _rewrite_local_refs(
        claim, lambda ref: ref.update(local_id=f"{prefix}:{ref['local_id']}")
    )


# what a complete extraction writes to its state directory, when it has one
SIDE_OUTPUTS = ("coverage.json", "rejects.json", "gaps.ndjson")


class ChunkedClaimExtractor:
    """Entity manifest -> packed chunks -> per-chunk calls -> merge.

    Every call goes to `client`. A chunk response cut off, at the engine's
    output limit (llm.at_output_limit) or before its terminal sentinel, is
    rejected through it (llm.reject_response): behind the LLM cache
    (ADR-0032) that makes the retry, in this run or the next, ask the
    engine again rather than be answered with the same text, and a chunk
    that failed every retry is asked again in the next run. A
    rejected answer the cache kept from an earlier call spends no retry:
    the engine has not answered the chunk yet, and has as many tries as
    it would without the cache.
    The entity manifest and the segmentation are asked in two attempts
    each, keyed apart behind the cache (llm.for_attempt): a refused
    answer is kept, so a later run replays the same attempts and takes the
    same fallback. An attempt the engine gave up on at its output limit is
    kept alike, as a give-up, so a later run gives up on it again without
    asking the engine, and takes the same fallback. When a conversation behind
    the cache has to be opened again and answers differently
    (llm.ConversationChanged), the extraction starts over from the new
    answer.

    `state_dir`, when given, receives the advisory side outputs of a
    complete extraction: `coverage.json`, and `rejects.json` and
    `gaps.ndjson` when there are any (a file the extraction no longer
    has anything for is removed).

    Matches LlmClaimExtractor's extract() signature so the CLI treats the
    strategies interchangeably. After extract(), `failed_chunks` lists
    chunks that produced nothing despite retries, with their reasons in
    `failure_reasons` — the batch is still returned (partial progress is
    the point) and the caller decides how loudly to fail — and
    `attention` says what a reader of the batch should know: a fallback
    tier, or why it holds no claims. An extraction that would end with no
    claims for want of a usable answer raises ExtractionFailed instead
    (`_refuse_empty`), so a batch without claims is returned only when the
    answers the extractor accepted say the document holds none.
    """

    def __init__(
        self,
        client: LlmClient,
        state_dir: Path | None = None,
        budget: int = DEFAULT_CHUNK_BUDGET,
        retries: int = 2,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.state_dir = state_dir
        self.budget = budget
        self.retries = retries
        # chunks are independent by design (ADR-0006); >1 runs their
        # engine calls in parallel threads. Forks of one session are
        # safe to run concurrently: each fork branches into its own
        # new session without mutating the original.
        self.concurrency = max(1, concurrency)
        # progress feedback (chunked extraction is otherwise silent for
        # minutes on a large document); a no-op when no logger is given.
        self._log = log if log is not None else (lambda _message: None)
        self._chunks_done = 0
        self._state_lock = threading.Lock()
        self.failed_chunks: list[str] = []
        self.failure_reasons: dict[str, str] = {}
        # open-schema gaps (ADR-0013): relations no property expresses,
        # collected per run for review; never written to the graph
        self.gaps: list[dict] = []
        # entity manifest unusable -> line-range segmentation was used
        self.used_fallback_manifest = False
        # line-range segmentation also unusable -> offset windows
        self.used_fallback_segmentation = False
        # why each fallback tier was taken: "manifest", "segmentation"
        self.fallback: dict[str, str] = {}
        # per fallback tier taken, why each of its attempts failed
        self._refusals: dict[str, list[str]] = {}
        # per chunk, the objects in its answer that held something the
        # parser could not read (parse_chunk_unread)
        self._unread: dict[str, list[dict]] = {}
        # the entity manifest's mention records and judgement notes set
        # aside, counted by the category of each reason, and the number of
        # entities whose mention record was set aside
        self._set_aside: dict[str, int] = {}
        self._set_aside_records = 0
        # what settling the manifest's names counted (_settle_names)
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}
        # how chunk calls saw the document: fork, inline or compressed
        self.context_mode: str | None = None
        self.attention: list[str] = []
        self._rejects: list[dict] = []

    # -- helpers ---------------------------------------------------------

    def _note_chunk_done(
        self, total: int, *, ok: bool, claims: int = 0
    ) -> None:
        """Emit a running `chunk k/total` progress line as each chunk
        finishes. Called from worker threads; the counter is locked."""
        with self._state_lock:
            self._chunks_done += 1
            done = self._chunks_done
        if not ok:
            tag = "failed"
        else:
            plural = "" if claims == 1 else "s"
            tag = f"{claims} claim{plural}"
        self._log(f"  chunk {done}/{total} ({tag})")

    # -- phase 1a: entity manifest (ADR-0007, the default) ----------------

    def _first_turn(
        self, prompt: str, parse: Callable[[str], tuple[Any, list[str]]]
    ) -> tuple[Any, Any, list[str], bool]:
        """(parsed answer or None, conversation handle, why each attempt
        that gave no usable answer failed, any call answered) for a turn
        that opens the extraction: the entity manifest, or the
        segmentation behind it.

        Two attempts, each asked through `for_attempt`, so that behind the
        LLM cache a refused answer is kept under its own attempt and a
        later run replays the same attempts to the same outcome. An
        attempt fails when the engine raises or its answer is refused, and
        each gives its own reason. An answer the engine gave up on at its
        output limit (OutputLimitReached) is an answer refused, not a call
        unanswered: the engine answered, past its limit. Behind the cache
        it is kept under its attempt, as a refused answer is, so a later
        run raises it again without asking the engine. No handle
        survives a refused answer: forking would plant known-bad output in
        every chunk's context."""
        handle = None
        answered = False
        reasons: list[str] = []
        for attempt in range(2):
            client = for_attempt(self.client, attempt)
            try:
                if isinstance(client, ConversationalLlmClient):
                    response, handle = client.open_conversation(prompt)
                else:
                    response = client.complete(prompt)
            except OutputLimitReached:
                answered = True
                reasons.append(CUT_OFF_AT_LIMIT)
                handle = None
                continue
            except Exception as exc:
                reasons.append(f"{type(exc).__name__}: {exc}")
                handle = None
                continue
            answered = True
            parsed, problems = parse(response)
            if parsed is not None and not problems:
                return parsed, handle, reasons, answered
            reasons.append("; ".join(problems or ["unparseable"]))
            self._close_conversation(handle)
            handle = None
        return None, None, reasons, answered

    def _entity_manifest(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> tuple[_Manifest | None, Any, list[str], bool]:
        """(manifest, conversation handle, why each attempt failed, any
        call answered).

        No manifest means it was unusable in both attempts; the caller
        falls back to line-range segmentation. A manifest of no entities,
        the engine's word that the document names nothing, is usable, and
        the first attempt that gives one ends the turn."""
        line_count = len(transcription.text.splitlines())
        kinds, kind_descriptions = entity_kind_prompt_lines()
        prompt = ENTITY_MANIFEST_PROMPT.format(
            kinds=kinds,
            kind_descriptions=kind_descriptions,
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            numbered_text=number_lines(transcription.prompt_text()),
        )
        return self._first_turn(
            prompt,
            lambda response: self._parse_entity_manifest(response, line_count),
        )

    @staticmethod
    def _parse_entity_manifest(
        response: str, line_count: int
    ) -> tuple[_Manifest | None, list[str]]:
        """(manifest, problems); the manifest is None when the answer holds
        no entity list. Only a list, or null, is one, and any other value
        is unusable. An empty one, or null, is the engine's word that the
        document names nothing: a manifest of no entities, accepted as it
        stands, so no chunk is asked and no fallback tier is taken.

        An entity's mention record or judgement note that fails validation
        is set aside, never repaired and never the reason the manifest is
        refused (ADR-0035 §5): the entity keeps its label, aliases and
        kind, and the manifest counts each reason by its category, and the
        records set aside (one record can give several reasons, and one
        whose only reasons are name parts dropped for an empty string
        stands)."""
        _, item_keys = load_vocabulary()
        for obj in scan_json_objects(response):
            if "entities" not in obj:
                continue
            listed = obj["entities"]
            if listed is not None and not isinstance(listed, list):
                return None, [
                    f"'entities' is not a list ({type(listed).__name__})"
                ]
            set_aside: dict[str, int] = {}
            set_aside_records = 0
            try:
                records = []
                for raw in listed or []:
                    data = dict(raw)
                    data.pop("qid", None)  # resolution state, never model-set
                    data, reasons = _set_aside_note(data)
                    data, more = set_aside_structure(data)
                    set_aside_records += bool(more) and "structure" not in data
                    for reason in reasons + more:
                        category = reason.split(":", 1)[0]
                        set_aside[category] = set_aside.get(category, 0) + 1
                    records.append(EntityRecord.model_validate(data))
            except Exception as exc:
                return None, [str(exc)[:300]]
            problems: list[str] = []
            seen: set[str] = set()
            for record in records:
                if record.id in seen:
                    problems.append(f"duplicate entity id {record.id!r}")
                seen.add(record.id)
                if record.kind not in item_keys:
                    problems.append(
                        f"entity {record.id}: unknown kind {record.kind!r}"
                    )
                # mention lines are best-effort: clamp, don't reject
                record.mention_lines = [
                    number
                    for number in record.mention_lines
                    if 1 <= number <= line_count
                ]
            return _Manifest(records, set_aside, set_aside_records), problems
        return None, ["no JSON object with an 'entities' array"]

    # -- phase 1b: line-range segmentation (fallback tier) -----------------

    def _segment(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> tuple[DocumentSegmentation, Any, list[str], bool]:
        """(segmentation, handle or None, why each attempt failed when it
        fell back, any call answered).

        A None handle means chunk calls cannot fork (context mode 2/3).
        On segmentation fallback the handle is always None and the reasons
        are set: forking a conversation whose segmentation turn was
        refused would plant known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        prompt = SEGMENTATION_PROMPT.format(
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            numbered_text=number_lines(transcription.prompt_text()),
        )
        segmentation, handle, reasons, answered = self._first_turn(
            prompt,
            lambda response: self._parse_segmentation(response, line_count),
        )
        if segmentation is not None:
            return segmentation, handle, [], answered
        return (
            self._fallback_segmentation(line_count),
            None,
            reasons,
            answered,
        )

    @staticmethod
    def _parse_segmentation(
        response: str, line_count: int
    ) -> tuple[DocumentSegmentation | None, list[str]]:
        for obj in scan_json_objects(response):
            if "units" not in obj:
                continue
            try:
                segmentation = DocumentSegmentation.model_validate(obj)
            except Exception as exc:
                return None, [str(exc)[:300]]
            return segmentation, segmentation.problems(line_count)
        return None, ["no JSON object with a 'units' array"]

    def _fallback_segmentation(self, line_count: int) -> DocumentSegmentation:
        """Degraded offset chunking: fixed line windows, empty context."""
        window = max(1, self.budget // 40)  # ~40 chars/line heuristic
        units = [
            SegmentationUnit(
                id=f"w{start:04d}",
                label=f"lines {start}-{min(start + window - 1, line_count)}",
                kind="window (segmentation fallback)",
                first_line=start,
                last_line=min(start + window - 1, line_count),
                context="",
            )
            for start in range(1, line_count + 1, window)
        ]
        return DocumentSegmentation(units=units)

    def _phase_one(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> tuple[list[EntityRecord] | None, DocumentSegmentation | None, Any]:
        """(entities, segmentation, conversation handle): the entity
        manifest, or the line-range fallback when the manifest was
        unusable.

        A fallback tier is taken only when the engine answered, an answer
        it gave up on at its output limit included (_first_turn). When
        every call of a tier raised otherwise (an engine outage),
        extraction fails instead, so the document is retried rather than
        extracted by a degraded path the engine never chose.
        """
        self._log("  scanning document for entities...")
        manifest, handle, reasons, answered = self._entity_manifest(
            transcription, artifact
        )
        if manifest is not None:
            self._set_aside = manifest.set_aside
            self._set_aside_records = manifest.set_aside_records
            return manifest.entities, None, handle
        if not answered:
            raise RuntimeError(
                f"entity manifest: no engine response ({reasons[-1]})"
            )
        self.used_fallback_manifest = True
        self.fallback["manifest"] = reasons[-1]
        self._refusals["manifest"] = reasons
        segmentation, handle, reasons, answered = self._segment(
            transcription, artifact
        )
        if not answered:
            raise RuntimeError(
                f"segmentation: no engine response ({reasons[-1]})"
            )
        if reasons:
            self.used_fallback_segmentation = True
            self.fallback["segmentation"] = reasons[-1]
            self._refusals["segmentation"] = reasons
        return None, segmentation, handle

    # -- chunk calls -----------------------------------------------------

    def _document_block(
        self, transcription: Transcription, artifact: ArtifactRef, inline: bool
    ) -> str:
        if not inline:
            return ""
        return (
            f"\nFull transcription of artifact sha1:{artifact.sha1} for "
            f"context (extract ONLY for your assignment):\n\n"
            f"{number_lines(transcription.prompt_text())}\n"
        )

    def _render_chunk_prompt(
        self,
        assignment_block: str,
        transcription: Transcription,
        artifact: ArtifactRef,
        inline_document: bool,
    ) -> str:
        property_keys, item_keys = vocabulary_prompt_lines()
        return CHUNK_PROMPT.format(
            artifact_sha1=artifact.sha1,
            property_keys=property_keys,
            item_keys=item_keys,
            assignment_block=assignment_block,
            document_paragraphs=document_block(transcription, artifact),
            schema=claim_schema_prompt_text(),
            document_block=self._document_block(
                transcription, artifact, inline_document
            ),
        )

    def _chunk_prompt(
        self,
        chunk_units: list[SegmentationUnit],
        text_lines: list[str],
        transcription: Transcription,
        artifact: ArtifactRef,
        inline_document: bool,
    ) -> str:
        units_block = "\n\n".join(
            f"- unit {unit.id} ({unit.label}; lines "
            f"{unit.first_line}-{unit.last_line})"
            + (f"\n  context: {unit.context}" if unit.context else "")
            + f"\n  text:\n{unit_text(text_lines, unit)}"
            for unit in chunk_units
        )
        return self._render_chunk_prompt(
            LINE_ASSIGNMENT_BLOCK.format(
                kinds=entity_kind_prompt_lines()[0],
                units_block=units_block,
            ),
            transcription,
            artifact,
            inline_document,
        )

    def _entity_chunk_prompt(
        self,
        chunk_entities: list[EntityRecord],
        all_entities: list[EntityRecord],
        text_lines: list[str],
        transcription: Transcription,
        artifact: ArtifactRef,
        inline_document: bool,
    ) -> str:
        roster_block = "\n".join(
            f"  {entity.id} = {entity.label}" for entity in all_entities
        )

        def mention_text(entity: EntityRecord) -> str:
            lines = [
                f"    {number}: {text_lines[number - 1]}"
                for number in entity.mention_lines
            ]
            return "\n  mentions:\n" + "\n".join(lines) if lines else ""

        entities_block = "\n\n".join(
            f"- {entity.id}: {entity.label}"
            + (
                f" (aliases: {', '.join(entity.aliases)})"
                if entity.aliases
                else ""
            )
            + f"; kind: {entity.kind}"
            + (f"\n  {entity.summary}" if entity.summary else "")
            + mention_text(entity)
            for entity in chunk_entities
        )
        return self._render_chunk_prompt(
            ENTITY_ASSIGNMENT_BLOCK.format(
                roster_block=roster_block,
                entities_block=entities_block,
                first_entity=all_entities[0].id if all_entities else "none",
            ),
            transcription,
            artifact,
            inline_document,
        )

    def _run_chunk(
        self, prompt: str, handle: Any, fallback_prompt: str
    ) -> tuple[list[dict], str]:
        """(claims, response) for one chunk: call (fork if possible) and
        parse, with retries. A response cut off, at the engine's output
        limit or before its sentinel (parse_chunk_response), is rejected,
        so the retry asks again; one the client kept from an earlier call
        is asked for once more without spending a retry
        (llm.reject_response). A call the engine gave up on at its output
        limit (OutputLimitReached) gave no response to reject: it spends
        its try as a cut-off one does, and its reason is the same. A
        conversation that changed (ConversationChanged) is no failure of
        the chunk: it ends the extraction's pass, which starts over."""
        last_error: Exception = ChunkIncomplete("not attempted")
        tries = 1 + self.retries
        kept_rejected = False
        while tries:
            tries -= 1
            try:
                if handle is not None:
                    try:
                        response = self.client.fork(handle, prompt)
                    except (ConversationChanged, OutputLimitReached):
                        raise
                    except Exception:
                        # fork machinery failed (session gone, old CLI):
                        # this chunk drops to inline context
                        handle = None
                        response = self.client.complete(fallback_prompt)
                else:
                    response = self.client.complete(fallback_prompt)
            except ConversationChanged:
                raise
            except OutputLimitReached:
                last_error = ChunkIncomplete(CUT_OFF_AT_LIMIT)
                continue
            except Exception as exc:
                last_error = exc
                continue
            cut = at_output_limit(self.client, response)
            try:
                return parse_chunk_response(response, cut_off=cut), response
            except ChunkIncomplete as exc:
                last_error = exc
                kept = reject_response(self.client, response, f"chunk: {exc}")
                if kept and not kept_rejected:
                    kept_rejected = True
                    tries += 1
        raise last_error

    # -- orchestration ---------------------------------------------------

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        """The document's batch, every claim citing `artifact`, the
        document's content key, by which the prompts name the document too,
        and the page number printed on the page it cites, where the
        transcription's record gives one (cite_artifact). The prompts
        carry the transcription's text with each printed page number
        beside its page's marker, every line where it is in the text
        (Transcription.prompt_text). When its conversation has to be
        opened again and answers differently (ConversationChanged), the
        pass is void and the extraction starts over from the new answer,
        at most twice."""
        for _pass in range(2):
            try:
                return self._extract(transcription, artifact)
            except ConversationChanged as exc:
                self._log(f"  {exc}: extracting again from the new answer")
        return self._extract(transcription, artifact)

    def _extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        text_lines = transcription.prompt_text().splitlines()
        self.failed_chunks = []
        self.failure_reasons = {}
        self.gaps = []
        self.fallback = {}
        self.attention = []
        self._rejects = []
        self.used_fallback_manifest = False
        self.used_fallback_segmentation = False
        self._refusals = {}
        self._unread = {}
        self._set_aside = {}
        self._set_aside_records = 0
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}

        entities, segmentation, handle = self._phase_one(
            transcription, artifact
        )
        inline_document = (
            handle is None and len(transcription.text) <= INLINE_DOCUMENT_LIMIT
        )
        self.context_mode = (
            "fork"
            if handle is not None
            else "inline"
            if inline_document
            else "compressed"
        )

        if entities is not None:
            records = {record.id: record for record in entities}
            chunk_groups: list[list] = pack_entities(
                entities, text_lines, self.budget
            )

            def build_prompt(group: list, inline: bool) -> str:
                return self._entity_chunk_prompt(
                    group,
                    entities,
                    text_lines,
                    transcription,
                    artifact,
                    inline,
                )

            def transform(claim: dict, chunk_id: str) -> dict:
                return normalize_to_manifest(claim, records)

        else:
            chunk_groups = pack_units(
                segmentation.units, text_lines, self.budget
            )

            def build_prompt(group: list, inline: bool) -> str:
                return self._chunk_prompt(
                    group, text_lines, transcription, artifact, inline
                )

            def transform(claim: dict, chunk_id: str) -> dict:
                return prefix_local_ids(claim, chunk_id)

        chunk_ids = [
            f"c{index:03d}" for index in range(1, len(chunk_groups) + 1)
        ]
        total = len(chunk_ids)
        self._chunks_done = 0
        if entities is not None:
            self._log(f"  extract: {len(entities)} entities, {total} chunk(s)")
        else:
            self._log(f"  extract: {total} chunk(s) (line-range fallback)")

        def run_one(chunk_id: str, group: list) -> _Chunk | None:
            """One chunk end to end, or None on failure. Thread-safe:
            shared state goes through the lock."""
            try:
                claims, response = self._run_chunk(
                    build_prompt(group, False),
                    handle,
                    build_prompt(group, inline_document),
                )
            except ConversationChanged:
                raise
            except Exception as exc:
                with self._state_lock:
                    self.failed_chunks.append(chunk_id)
                    self.failure_reasons[chunk_id] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                self._note_chunk_done(total, ok=False)
                return None
            self._note_chunk_done(total, ok=True, claims=len(claims))
            return _Chunk(
                claims,
                parse_chunk_gaps(response),
                parse_chunk_unread(response),
            )

        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                outcomes = list(pool.map(run_one, chunk_ids, chunk_groups))
        finally:
            self._close_conversation(handle)
        self.failed_chunks.sort()

        batch, extras = self._merge(
            chunk_ids,
            outcomes,
            transform,
            entities,
            segmentation,
            text_lines,
            artifact,
            transcription.printed_pages(),
            placed_by(transcription, artifact),
        )
        self._refuse_empty(batch, chunks=total)
        self.attention = self._attention(batch, chunks=total)
        if not self.failed_chunks:
            self._write_side_outputs(extras)
        return batch

    def _close_conversation(self, handle: Any) -> None:
        """Let the client release a conversation no chunk forks any more
        (the claude-code engine keeps its session transcript until then)."""
        close = getattr(self.client, "close_conversation", None)
        if handle is not None and callable(close):
            close(handle)

    def _attention(self, batch: ClaimBatch, *, chunks: int) -> list[str]:
        """What a reader of the batch should know: each fallback tier
        taken, with its reason, and why a batch without claims is empty.
        Only a claim-free batch gets this far without claims
        (_refuse_empty)."""
        reasons = []
        if self.used_fallback_manifest:
            reasons.append(
                "entity manifest fell back to line-range segmentation: "
                f"{self.fallback['manifest']}"
            )
        if self.used_fallback_segmentation:
            reasons.append(
                "segmentation fell back to line windows: "
                f"{self.fallback['segmentation']}"
            )
        if not batch.claims:
            reasons.append(
                "no claims extracted: "
                f"{self._why_empty(chunks, named=len(batch.entities))}"
            )
        return reasons

    def _why_empty(self, chunks: int, *, named: int) -> str:
        """Why an extraction holds no claims when no chunk failed, none
        gave an object the parser could not read and validation rejected
        nothing: what the answers it accepted say, naming the refused
        manifest when the line-range tier stood in for it. `named` is the
        number of entities the batch holds, none when the manifest named
        nothing."""
        if chunks:
            plural = "" if chunks == 1 else "s"
            found = f"{chunks} chunk{plural} answered with none"
        elif self.used_fallback_manifest:
            found = "every unit in the segmentation is extract=false"
        elif not named:
            return "the entity manifest names no entity"
        else:
            return "every entity in the manifest is extract=false"
        if not self.used_fallback_manifest:
            return found
        return (
            "the entity manifest was refused "
            f"({_brief(self.fallback['manifest'])}), and on the line-range "
            f"tier {found}"
        )

    def _write_side_outputs(self, extras: dict[str, str]) -> None:
        if self.state_dir is None:
            return
        for name in SIDE_OUTPUTS:
            path = self.state_dir / name
            if name in extras:
                atomic_write_text(path, extras[name])
            else:
                path.unlink(missing_ok=True)

    def _refuse_empty(self, batch: ClaimBatch, *, chunks: int) -> None:
        """Raise ExtractionFailed when the batch holds no claims because
        the model's answers could not be used, rather than hand on an empty
        batch that reads as a claim-free document's:

        - chunks failed after their retries;
        - the chunks' answers held objects the parser could not read as
          claims (parse_chunk_unread), such as claims wrapped in one
          object or written with other keys;
        - validation rejected every claim the chunks gave.

        The message leads with the outcome and every cause, then names any
        fallback tier taken, with why each attempt at the turn it stood in
        for failed, each reason cut short (REASON_LIMIT). A failure keeps
        no view, so each run makes the extraction again, from the LLM
        cache at no engine cost, and a parser fix reaches it.

        Otherwise a batch without claims is claim-free, on the word of the
        answers the extractor accepted, the line-range tier's included
        when it stood in for a refused manifest (ADR-0032 §5), and
        `attention` says why."""
        if batch.claims:
            return
        causes = []
        if self.failed_chunks:
            first = self.failed_chunks[0]
            plural = "" if chunks == 1 else "s"
            causes.append(
                f"{len(self.failed_chunks)} of {chunks} chunk{plural} failed "
                f"({first}: {_brief(self.failure_reasons.get(first, ''))})"
            )
        if self._unread:
            count = sum(len(objects) for objects in self._unread.values())
            first, objects = next(iter(self._unread.items()))
            keys = _brief(", ".join(map(str, objects[0])))
            plural = "" if count == 1 else "s"
            causes.append(
                f"{count} unreadable object{plural} in the chunk answers "
                f"({first}: keys {keys})"
            )
        if self._rejects:
            causes.append(
                f"validation rejected every claim ({len(self._rejects)})"
            )
        if not causes:
            return
        message = "no claims extracted: " + "; ".join(causes)
        if self.used_fallback_manifest:
            message += (
                "; the entity manifest was refused "
                f"({self._attempts('manifest')})"
            )
        if self.used_fallback_segmentation:
            message += (
                f", and the segmentation ({self._attempts('segmentation')})"
            )
        raise ExtractionFailed(message)

    def _attempts(self, tier: str) -> str:
        """Why each attempt at a refused turn failed, cut short: once for
        them all when every attempt failed the same way, else numbered."""
        reasons = self._refusals[tier]
        if len(set(reasons)) == 1:
            return f"every attempt: {_brief(reasons[0])}"
        return "; ".join(
            f"{number}: {_brief(reason)}"
            for number, reason in enumerate(reasons, start=1)
        )

    def _merge(
        self,
        chunk_ids: list[str],
        outcomes: list[_Chunk | None],
        transform: Callable[[dict, str], dict],
        entities: list[EntityRecord] | None,
        segmentation: DocumentSegmentation | None,
        text_lines: list[str],
        artifact: ArtifactRef,
        printed_pages: dict[int, str],
        placed: Mapping[str, Collection[str]] | None = None,
    ) -> tuple[ClaimBatch, dict[str, str]]:
        """Merge chunk claims into one validated batch, each claim citing
        `artifact`, the page number printed on its page where
        `printed_pages` gives one, and each once (cite_artifact), validated
        with the artifacts the document places (`placed`,
        salvage_valid_claims), with rejects, gaps and the coverage report
        as side outputs: the coverage counts the rejects by reason
        (rejected_by_reason)."""
        raw_claims: list[dict] = []
        transform_rejects: list[dict] = []
        gaps: list[dict] = []
        unread: dict[str, list[dict]] = {}
        for chunk_id, chunk in zip(chunk_ids, outcomes, strict=True):
            if chunk is None:
                continue
            if chunk.unread:
                unread[chunk_id] = chunk.unread
            for claim in chunk.claims:
                # one malformed claim (a double-nested local_id, say)
                # must never sink the whole document's merge — degrade
                # it to a reject, the way salvage_valid_claims does
                # downstream
                try:
                    raw_claims.append(transform(claim, chunk_id))
                except Exception as exc:  # keep the other claims
                    transform_rejects.append(
                        {
                            "claim": claim,
                            "problems": [f"transform failed: {exc}"],
                            "reasons": [SCHEMA_REASON],
                        }
                    )
            gaps.extend(chunk.gaps)
        self.gaps = gaps
        self._unread = unread

        deduped = cite_artifact(raw_claims, artifact, printed_pages)
        run = run_metadata(self.client.model_identifier).model_dump(
            mode="json"
        )

        settled_rejects: list[dict] = []
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}
        if entities is not None:
            settled = _settle_names(deduped, entities, run)
            deduped, entities_payload = settled.claims, settled.entities
            settled_rejects = settled.rejects
            self._appellations = {
                "labels": settled.labels,
                "label_names": settled.label_names,
                "parts": settled.parts,
            }
        else:
            entities_payload = []
        batch, rejects = salvage_valid_claims(
            deduped, run, entities=entities_payload, placed=placed
        )
        rejects = transform_rejects + settled_rejects + rejects
        self._rejects = rejects
        extras: dict[str, str] = {}
        if rejects:
            extras["rejects.json"] = json.dumps(rejects, indent=2)
        if gaps:
            extras["gaps.ndjson"] = "\n".join(map(json.dumps, gaps))
        if entities is not None:
            coverage = self._manifest_coverage(entities, batch)
        else:
            coverage = self._coverage(segmentation, batch, len(text_lines))
        # the extraction's health, on either tier, for the checks that read
        # a stored batch: how many chunks it asked, what they came to, and
        # each fallback tier it took, with the reason
        coverage |= {
            "chunks": len(chunk_ids),
            "claims": len(batch.claims),
            "rejected_claims": len(rejects),
            "rejected_by_reason": rejected_by_reason(rejects),
            "unread_objects": sum(len(found) for found in unread.values()),
            "fallback": dict(self.fallback),
        }
        extras["coverage.json"] = json.dumps(coverage, indent=2)
        return batch, extras

    def _manifest_coverage(
        self, entities: list[EntityRecord], batch: ClaimBatch
    ) -> dict:
        """Advisory completeness report, entity flavour (ADR-0007), in
        place of the line-range path's unit audit (`_coverage`): the
        manifest entities with zero claims, and counts of the mention
        records and judgement notes the extraction gave (ADR-0035 §5):
        the records given, by status, and the unsplit name forms they
        hold; the mentions recordless; the records set aside, and the
        reasons they and the notes were set aside by, by category; the
        labels the appellation label rule replaced with a name
        (names.appellation_label), the `name` claims dropped for
        restating one, and the record name parts that are an
        appellation; and the notes by category. A mention
        of a kind that takes a record but has none is recordless: its
        record was set aside, or never given. Nothing in it quotes a label."""
        subject_ids = {
            claim.subject.local_id
            for claim in batch.claims
            if hasattr(claim.subject, "local_id")
        }
        structured = [e for e in entities if e.kind in STRUCTURED_KINDS]
        statuses: dict[str, int] = {}
        for entity in structured:
            status = getattr(entity.structure, "status", None)
            if entity.structure is not None and status is not None:
                statuses[status] = statuses.get(status, 0) + 1
        notes: dict[str, int] = {}
        noted = [*entities, *batch.claims]
        for note in (fact.note for fact in noted):
            if note is not None:
                notes[note.category] = notes.get(note.category, 0) + 1
        return {
            "strategy": "entity",
            "entities_total": len(entities),
            "entities_not_extracted": [
                entity.id for entity in entities if not entity.extract
            ],
            "entities_without_claims": [
                entity.id
                for entity in entities
                if entity.extract and entity.id not in subject_ids
            ],
            "distinct_subject_ids": sorted(subject_ids),
            "failed_chunks": self.failed_chunks,
            "records": {
                "given": sum(e.structure is not None for e in structured),
                "recordless": sum(e.structure is None for e in structured),
                "by_status": dict(sorted(statuses.items())),
                "unsplit_forms": sum(
                    len(entity.structure.unsplit)
                    for entity in structured
                    if isinstance(entity.structure, PersonStructure)
                ),
                "set_aside_records": self._set_aside_records,
                "set_aside": dict(sorted(self._set_aside.items())),
                "appellation_labels": self._appellations["labels"],
                "appellation_label_names": self._appellations["label_names"],
                "appellation_parts": self._appellations["parts"],
            },
            "notes": dict(sorted(notes.items())),
        }

    def _coverage(
        self,
        segmentation: DocumentSegmentation,
        batch: ClaimBatch,
        line_count: int,
    ) -> dict:
        """Advisory completeness report for the curator (no auto-retry)."""
        subject_labels = {
            claim.subject.label
            for claim in batch.claims
            if hasattr(claim.subject, "label")
        }
        # heuristic: a unit whose label names no claim subject probably
        # yielded nothing — advisory, so false positives are acceptable
        silent_units = [
            unit.id
            for unit in segmentation.units
            if unit.extract
            and not any(label in unit.label for label in subject_labels)
        ]
        return {
            "units_total": len(segmentation.units),
            "units_extractable": sum(
                1 for unit in segmentation.units if unit.extract
            ),
            "units_without_matching_subject": silent_units,
            "unsegmented_line_ranges": segmentation.unsegmented_lines(
                line_count
            ),
            "distinct_subject_labels": sorted(subject_labels),
            "failed_chunks": self.failed_chunks,
        }


class _Settled(NamedTuple):
    """What settling a manifest's names gives (_settle_names): the claims
    to validate, the entities as the batch carries them, the claims
    rejected already, and the counts coverage.json gives: the labels that
    became a name, the `name` claims dropped for restating one, and the
    record name parts that are an appellation."""

    claims: list[dict]
    entities: list[dict]
    rejects: list[dict]
    labels: int
    label_names: int
    parts: int


def rejected_by_reason(rejects: Iterable[dict]) -> dict[str, int]:
    """How many rejects give each reason code (claims.REJECT_REASONS), in
    that order, each reject counted once under each of its reasons: a
    reject that records none counts as `manifest`."""
    counts = Counter(
        reason
        for reject in rejects
        for reason in (reject.get("reasons") or [MANIFEST_REASON])
    )
    return {
        reason: counts[reason] for reason in REJECT_REASONS if counts[reason]
    }


def _name_text(claim: dict) -> str | None:
    """The text a `name` or `referred_to_as` claim dict gives, if any."""
    value = claim.get("value")
    inner = value.get("value") if isinstance(value, dict) else None
    text = inner.get("text") if isinstance(inner, dict) else None
    return text if isinstance(text, str) else None


def _subject_id(claim: dict) -> str | None:
    subject = claim.get("subject")
    if isinstance(subject, dict) and subject.get("kind") == "local":
        local_id = subject.get("local_id")
        return local_id if isinstance(local_id, str) else None
    return None


def _settle_names(
    claims: list[dict], entities: list[EntityRecord], run: dict
) -> _Settled:
    """Each entity's label set and its alias bag cleaned (ADR-0022 §4,
    ADR-0035 §8; names.normalize_entity_dict), once the chunks' claims
    exist: the deterministic safety net over the model's manifest, by
    the forms the document gives as its appellations (ADR-0018). Those
    are the values of the `referred_to_as` claims about it that validate
    against the manifest as the model gave it (names.appellations_of);
    only those claims and the `name` claims are checked here, each on its
    own. A label that changes changes in every local ref to the entity
    too. A `name` claim on such a person that the first check rejected
    (one restating the model's own label) keeps that reject, and one that
    restates the new label is dropped and counted, never a reject of the
    model's."""
    from yizkr.claims import name_form_key
    from yizkr.pipeline.names import (
        appellation_parts,
        appellations_of,
        normalize_entity_dict,
    )

    given = [record.model_dump() for record in entities]
    valid: list[dict] = []
    first_rejects: dict[int, dict] = {}
    for index, claim in enumerate(claims):
        if claim.get("property") not in ("referred_to_as", "name"):
            continue
        checked, rejected = salvage_valid_claims([claim], run, entities=given)
        if checked.claims:
            valid.append(claim)
        else:
            first_rejects[index] = rejected[0]
    forms = appellations_of(
        claim for claim in valid if claim["property"] == "referred_to_as"
    )
    settled = [
        normalize_entity_dict(entity, forms.get(entity["id"], ()))
        for entity in given
    ]
    parts = sum(
        appellation_parts(entity, forms.get(entity["id"], ()))
        for entity in given
    )
    labels = {
        entity["id"]: entity["label"]
        for entity, before in zip(settled, given, strict=True)
        if entity["label"] != before["label"]
    }
    kept: list[dict] = []
    rejects: list[dict] = []
    label_names = 0
    for index, claim in enumerate(claims):
        subject = _subject_id(claim)
        if claim.get("property") == "name" and subject in labels:
            if index in first_rejects:
                rejects.append(first_rejects[index])
                continue
            text = _name_text(claim)
            if text is not None and name_form_key(text) == name_form_key(
                labels[subject]
            ):
                label_names += 1
                continue
        kept.append(claim)
    if labels:

        def fix(ref: dict) -> None:
            if ref["local_id"] in labels:
                ref["label"] = labels[ref["local_id"]]

        kept = [_rewrite_local_refs(claim, fix) for claim in kept]
    return _Settled(kept, settled, rejects, len(labels), label_names, parts)


class _Manifest(NamedTuple):
    """An entity manifest as parsed: its entities; the mention records
    and judgement notes set aside, counted by the category of each
    reason; and the number of entities whose mention record was set
    aside."""

    entities: list[EntityRecord]
    set_aside: dict[str, int]
    set_aside_records: int


def _set_aside_note(entity: dict) -> tuple[dict, list[str]]:
    """An entity of an entity manifest, as the model wrote it, with its
    judgement note set aside when the note does not validate, and the
    reason (ADR-0035 §5): `note` and the first error. The entity keeps
    everything else."""
    note = entity.get("note")
    if note is None:
        return entity, []
    try:
        JudgementNote.model_validate(note)
    except Exception as exc:
        rest = {key: value for key, value in entity.items() if key != "note"}
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return rest, [f"note: {first}"]
    return entity, []


class _Chunk(NamedTuple):
    """One chunk's parsed claims and gaps, and the objects in its answer
    that held something the parser could not read (parse_chunk_unread):
    their count is what tells an unreadable answer from one with no
    claims."""

    claims: list[dict]
    gaps: list[dict]
    unread: list[dict]
